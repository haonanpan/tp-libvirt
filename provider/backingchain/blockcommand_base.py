import logging
import os

from avocado.utils import process

from virttest import virsh
from virttest import data_dir
from virttest.utils_test import libvirt
from virttest.libvirt_xml import vm_xml
from virttest.libvirt_xml.devices.disk import Disk
from virttest.utils_libvirt import libvirt_secret

LOG = logging.getLogger('avocado.' + __name__)


class BlockCommand(object):
    """
    Prepare data for blockcommand test

    :param test: Test object
    :param vm: A libvirt_vm.VM class instance.
    :param params: Dict with the test parameters.
    """
    def __init__(self, test, vm, params):
        self.test = test
        self.vm = vm
        self.params = params
        self.new_dev = self.params.get('target_disk', 'vda')
        self.src_path = ''
        self.snap_path_list = []
        self.snap_name_list = []
        self.tmp_dir = data_dir.get_data_dir()
        self.new_image_path = ''
        self.old_parts = []
        self.original_disk_source = ''

    def prepare_iscsi(self):
        """
        Prepare iscsi target and set specific params for disk.
        """
        driver_type = self.params.get('driver_type', 'raw')
        disk_type = self.params.get('disk_type', 'block')

        first_disk = self.vm.get_first_disk_devices()
        new_dev = first_disk['target']

        # Setup iscsi target
        device_name = libvirt.setup_or_cleanup_iscsi(is_setup=True)
        # Prepare block type disk params
        self.params['cleanup_disks'] = 'yes'
        self.params.update({'disk_format': driver_type,
                            'disk_type': disk_type,
                            'disk_target': new_dev,
                            'disk_source_name': device_name})
        self.new_dev = new_dev
        self.src_path = device_name

    def update_disk(self):
        """
        Update vm disk with specific params

        """
        # Get disk type
        disk_type = self.params.get('disk_type')

        # Check disk type
        if disk_type == 'block':
            if not self.vm.is_alive():
                self.vm.start()
            self.prepare_iscsi()

        # Update vm disk
        libvirt.set_vm_disk(self.vm, self.params)

    def prepare_snapshot(self, start_num=0, snap_num=3,
                         snap_path="", option='--disk-only', extra=''):
        """
        Prepare domain snapshot

        :params start_num: snap path start index
        :params snap_num: snapshot number, default value is 3
        :params snap_path: path of snap
        :params option: option to create snapshot, default value is '--disk-only'
        :params extra: extra option to create snap
        """
        # Create backing chain
        for i in range(start_num, snap_num):
            if not snap_path:
                path = self.tmp_dir + '%d' % i
            else:
                path = snap_path
            snap_option = "%s %s --diskspec %s,file=%s%s" % \
                          ('snap%d' % i, option, self.new_dev, path, extra)

            virsh.snapshot_create_as(self.vm.name, snap_option,
                                     ignore_status=False,
                                     debug=True)
            self.snap_path_list.append(path)
            self.snap_name_list.append('snap%d' % i)

    def convert_expected_chain(self, expected_chain_index):
        """
        Convert expected chain from "4>1>base" to "[/*snap4, /*snap1, /base.image]"

        :param expected_chain_index: expected chain , such as "4>1>base"
        :return expected chain with list
        """
        expected_chain = []
        for i in expected_chain_index.split('>'):
            expected_chain.append(self.new_image_path) if i == "base"\
                else expected_chain.append(self.snap_path_list[int(i) - 1])
        LOG.debug("Expected chain is : %s", expected_chain)
        return expected_chain

    def backingchain_common_teardown(self):
        """
        Clean all new created snap
        """
        LOG.info('Start cleaning up.')
        for ss in self.snap_name_list:
            virsh.snapshot_delete(self.vm.name, '%s --metadata' % ss, debug=True)
        for sp in self.snap_path_list:
            process.run('rm -f %s' % sp)
        # clean left first disk snap file that created along with new disk
        image_path = os.path.dirname(self.original_disk_source)
        if image_path != '':
            for sf in os.listdir(image_path):
                if 'snap' in sf:
                    process.run('rm -f %s/%s' % (image_path, sf))

    def prepare_secret_disk(self, image_path, secret_disk_dict=None):
        """
        Add secret disk for domain.

        :params image_path: image path for disk source file
        :secret_disk_dict: secret disk dict to add new disk
        """
        vmxml = vm_xml.VMXML.new_from_dumpxml(self.vm.name)
        sec_dict = eval(self.params.get("sec_dict", '{}'))
        device_target = self.params.get("target_disk", "vdd")
        sec_passwd = self.params.get("private_key_password")
        if not secret_disk_dict:
            secret_disk_dict = {'type_name': "file",
                                'target': {"dev": device_target, "bus": "virtio"},
                                'driver': {"name": "qemu", "type": "qcow2"},
                                'source': {'encryption': {"encryption": 'luks',
                                                          "secret": {"type": "passphrase"}}}}
        # Create secret
        libvirt_secret.clean_up_secrets()
        sec_uuid = libvirt_secret.create_secret(sec_dict=sec_dict)
        virsh.secret_set_value(sec_uuid, sec_passwd, encode=True,
                               debug=True)

        secret_disk_dict['source']['encryption']['secret']["uuid"] = sec_uuid
        secret_disk_dict['source']['attrs'] = {'file': image_path}
        new_disk = Disk()
        new_disk.setup_attrs(**secret_disk_dict)
        vmxml.devices = vmxml.devices.append(new_disk)
        vmxml.xmltreefile.write()
        vmxml.sync()
