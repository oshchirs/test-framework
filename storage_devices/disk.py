#
# Copyright(c) 2019-2021 Intel Corporation
# SPDX-License-Identifier: BSD-3-Clause-Clear
#
import itertools
import json
import re
from datetime import timedelta
from enum import IntEnum

from core.test_run import TestRun
from storage_devices.device import Device
from test_tools import disk_utils, fs_utils
from test_utils import disk_finder
from test_utils.os_utils import wait
from test_utils.output import CmdException
from test_utils.size import Unit


class DiskType(IntEnum):
    hdd = 0
    hdd4k = 1
    sata = 2
    nand = 3
    optane = 4


class DiskTypeSetBase:
    def resolved(self):
        raise NotImplementedError()

    def types(self):
        raise NotImplementedError()

    def json(self):
        return json.dumps({
            "type": "set",
            "values": [t.name for t in self.types()]
        })

    def __lt__(self, other):
        return min(self.types()) < min(other.types())

    def __le__(self, other):
        return min(self.types()) <= min(other.types())

    def __eq__(self, other):
        return min(self.types()) == min(other.types())

    def __ne__(self, other):
        return min(self.types()) != min(other.types())

    def __gt__(self, other):
        return min(self.types()) > min(other.types())

    def __ge__(self, other):
        return min(self.types()) >= min(other.types())


class DiskTypeSet(DiskTypeSetBase):
    def __init__(self, *args):
        self.__types = set(*args)

    def resolved(self):
        return True

    def types(self):
        return self.__types


class DiskTypeLowerThan(DiskTypeSetBase):
    def __init__(self, disk_name):
        self.__disk_name = disk_name

    def resolved(self):
        return self.__disk_name in TestRun.disks

    def types(self):
        if not self.resolved():
            raise LookupError("Disk type not resolved!")
        disk_type = TestRun.disks[self.__disk_name].disk_type
        return set(filter(lambda d: d < disk_type, [*DiskType]))

    def json(self):
        return json.dumps({
            "type": "operator",
            "name": "lt",
            "args": [self.__disk_name]
        })


class Disk(Device):
    def __init__(
        self,
        path,
        disk_type: DiskType,
        serial_number,
        block_size,
        features
    ):
        Device.__init__(self, path)
        self.serial_number = serial_number
        self.block_size = Unit(block_size)
        self.disk_type = disk_type
        self.partitions = []
        self.features = features

    def create_partitions(
            self,
            sizes: [],
            partition_table_type=disk_utils.PartitionTable.gpt):
        disk_utils.create_partitions(self, sizes, partition_table_type)

    def umount_all_partitions(self):
        TestRun.LOGGER.info(
            f"Umounting all partitions from: {self.path}")
        cmd = f'umount -l {fs_utils.readlink(self.path)}*?'
        TestRun.executor.run(cmd)

    def remove_partitions(self):
        for part in self.partitions:
            if part.is_mounted():
                part.unmount()
        if disk_utils.remove_partitions(self):
            self.partitions.clear()

    def is_detected(self):
        if self.serial_number:
            serial_numbers = disk_finder.get_all_serial_numbers()
            if self.serial_number not in serial_numbers:
                return False
            else:
                self.path = serial_numbers[self.serial_number]
                for part in self.partitions:
                    part.path = disk_utils.get_partition_path(
                        part.parent_device.path, part.number)
                return True
        elif self.path:
            output = fs_utils.ls_item(f"{self.path}")
            return fs_utils.parse_ls_output(output)[0] is not None
        raise Exception("Couldn't check if device is detected by the system")

    def wait_for_plug_status(self, should_be_visible):
        if not wait(lambda: should_be_visible == self.is_detected(),
                    timedelta(minutes=1),
                    timedelta(seconds=1)):
            raise Exception(f"Timeout occurred while trying to "
                            f"{'plug' if should_be_visible else 'unplug'} disk.")

    def plug(self):
        if self.is_detected():
            return
        self.execute_plug_command()
        self.wait_for_plug_status(True)

    def unplug(self):
        if not self.is_detected():
            return
        self.execute_unplug_command()
        self.wait_for_plug_status(False)

    def has_features(self, req_features):
        if self.disk_type in req_features:
            feat = set(req_features[self.disk_type])
            return len(feat.intersection(self.features)) == len(feat)
        else:
            return True

    @staticmethod
    def plug_all_disks():
        TestRun.executor.run_expect_success(NvmeDisk.plug_all_command)
        TestRun.executor.run_expect_success(SataDisk.plug_all_command)

    def __str__(self):
        disk_str = f'system path: {self.path}, type: {self.disk_type}, ' \
            f'serial: {self.serial_number}, size: {self.size}, ' \
            f'block size: {self.block_size}, features: {self.features}, partitions:\n'
        for part in self.partitions:
            disk_str += f'\t{part}'
        return disk_str

    @staticmethod
    def create_disk(path,
                    disk_type: DiskType,
                    serial_number,
                    block_size,
                    features):
        if disk_type is DiskType.nand or disk_type is DiskType.optane:
            return NvmeDisk(path, disk_type, serial_number, block_size, features)
        else:
            return SataDisk(path, disk_type, serial_number, block_size, features)


class NvmeDisk(Disk):
    plug_all_command = "echo 1 > /sys/bus/pci/rescan"

    def __init__(self, path, disk_type, serial_number, block_size, features):
        Disk.__init__(self, path, disk_type, serial_number, block_size, features)

    def execute_plug_command(self):
        TestRun.executor.run_expect_success(NvmeDisk.plug_all_command)

    def execute_unplug_command(self):
        if TestRun.executor.run(
                f"echo 1 > /sys/block/{self.get_device_id()}/device/remove").exit_code != 0:
            output = TestRun.executor.run(
                f"echo 1 > /sys/block/{self.get_device_id()}/device/device/remove")
            if output.exit_code != 0:
                raise CmdException(f"Failed to unplug PCI disk using sysfs.", output)


class SataDisk(Disk):
    plug_all_command = "for i in $(find -H /sys/devices/ -path '*/scsi_host/*/scan' -type f); " \
                       "do echo '- - -' > $i; done;"

    def __init__(self, path, disk_type, serial_number, block_size, features):
        self.plug_command = SataDisk.plug_all_command
        Disk.__init__(self, path, disk_type, serial_number, block_size, features)

    def execute_plug_command(self):
        TestRun.executor.run_expect_success(self.plug_command)

    def execute_unplug_command(self):
        TestRun.executor.run_expect_success(
            f"echo 1 > {self.get_sysfs_properties(self.get_device_id()).full_path}/device/delete")

    def get_sysfs_properties(self, device_id):
        ls_command = f"$(find -H /sys/devices/ -name {device_id} -type d)"
        output = fs_utils.ls_item(f"{ls_command}")
        sysfs_addr = fs_utils.parse_ls_output(output)[0]
        if not sysfs_addr:
            raise Exception(f"Failed to find sysfs address: ls -l {ls_command}")
        dirs = sysfs_addr.full_path.split('/')
        scsi_address = dirs[-3]
        matches = re.search(
            r"^(?P<controller>\d+)[-:](?P<port>\d+)[-:](?P<target>\d+)[-:](?P<lun>\d+)$",
            scsi_address)
        controller_id = matches["controller"]
        port_id = matches["port"]
        target_id = matches["target"]
        lun = matches["lun"]

        host_path = "/".join(itertools.takewhile(lambda x: not x.startswith("host"), dirs))
        self.plug_command = f"echo '{port_id} {target_id} {lun}' > " \
            f"{host_path}/host{controller_id}/scsi_host/host{controller_id}/scan"
        return sysfs_addr
