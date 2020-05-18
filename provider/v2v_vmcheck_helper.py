import os
import re
import logging
import time
from distutils.version import LooseVersion  # pylint: disable=E0611

from avocado.core import exceptions
from avocado.utils import process

from virttest import utils_v2v
from virttest import utils_sasl
from virttest import virsh
from virttest import utils_misc
from virttest import xml_utils
from virttest.libvirt_xml import vm_xml

V2V_7_3_VERSION = 'virt-v2v-1.32.1-1.el7'
RETRY_TIMES = 10
# Temporary workaround <Fix in future with a better solution>
FEATURE_SUPPORT = {
    'genid': 'virt-v2v-1.40.1-1.el7',
    'libosinfo': 'virt-v2v-1.40.2-2.el7',
    'virtio_rng': '2.6.26'}


class VMChecker(object):

    """
    Check VM after virt-v2v converted
    """

    def __init__(self, test, params, env):
        self.errors = []
        self.params = params
        self.vm_name = params.get('main_vm')
        self.v2v_cmd = params.get('v2v_command', '')
        self.original_vm_name = params.get('original_vm_name')
        # The expected boottype of guest, default 0 is 'i440fx+bios'
        # Other values are 1 for q35+bios, 2 for q35+uefi, 3 for
        # q35+secure_uefi
        self.boottype = int(params.get("boottype", 0))
        self.hypervisor = params.get("hypervisor")
        self.target = params.get('target')
        self.os_type = params.get('os_type')
        self.os_version = params.get('os_version', 'OS_VERSION_V2V_EXAMPLE')
        self.original_vmxml = params.get('original_vmxml')
        self.vmx_nfs_src = params.get('vmx_nfs_src')
        self.virsh_session = params.get('virsh_session')
        self.virsh_session_id = self.virsh_session.get_id(
            ) if self.virsh_session else params.get('virsh_session_id')
        self.checker = utils_v2v.VMCheck(test, params, env)
        self.setup_session()
        if not self.checker.virsh_session_id:
            self.checker.virsh_session_id = self.virsh_session_id
        if self.v2v_cmd and '-o rhv-upload' in self.v2v_cmd and '--no-copy' in self.v2v_cmd:
            self.vmxml = ''
        else:
            self.vmxml = virsh.dumpxml(
                self.vm_name,
                session_id=self.virsh_session_id).stdout.strip()
        self.xmltree = None
        if self.vmxml:
            self.xmltree = xml_utils.XMLTreeFile(self.vmxml)
        # Save NFS mount records like {0:(src, dst, fstype)}
        self.mount_records = {}

    def cleanup(self):
        self.close_virsh_session()
        try:
            self.checker.cleanup()
        except Exception as e:
            logging.debug("Exception during cleanup:\n%s", e)
            pass

        if len(self.mount_records) != 0:
            for src, dst, fstype in self.mount_records.values():
                utils_misc.umount(src, dst, fstype)

    def close_virsh_session(self):
        logging.debug('virsh session %s is closing', self.virsh_session)
        if not self.virsh_session:
            return
        if self.target == "ovirt":
            self.virsh_session.close()
        else:
            self.virsh_session.close_session()

    def setup_session(self):
        if self.virsh_session and self.virsh_session_id:
            logging.debug(
                'virsh session %s has already been set',
                self.virsh_session)
            return

        for index in range(RETRY_TIMES):
            logging.info('Trying %d times', index + 1)
            try:
                if self.target == "ovirt":
                    self.virsh_session = utils_sasl.VirshSessionSASL(
                        self.params)
                    self.virsh_session_id = self.virsh_session.get_id()
                else:
                    self.virsh_session = virsh.VirshPersistent(auto_close=True)
                    self.virsh_session_id = self.virsh_session.session_id
            except Exception as detail:
                logging.error(detail)
            else:
                break

        logging.debug('new virsh session %s is created', self.virsh_session)
        if not self.virsh_session_id:
            raise exceptions.TestError('Fail to create virsh session')

    def log_err(self, msg):
        logging.error(msg)
        self.errors.append(msg)

    def run(self):
        self.check_metadata_libosinfo()
        self.check_genid()
        if self.os_type == 'linux':
            self.check_linux_vm()
        elif self.os_type == 'windows':
            self.check_windows_vm()
        else:
            logging.warn("Unspported os type: %s", self.os_type)
        return self.errors

    def compare_version(self, compare_version, real_version=None, cmd=None):
        """
        Compare version against given version.

        :param compare_version: The minumum version to be compared
        :param real_version: The real version to compare
        :param cmd: the command to get the real version

        :return: If the real_version is greater equal than minumum version,
                return True, others return False
        """
        if not real_version:
            if not cmd:
                cmd = 'rpm -q virt-v2v|grep virt-v2v'
            real_version = process.run(cmd, shell=True).stdout_text.strip()
        if LooseVersion(real_version) >= LooseVersion(compare_version):
            return True
        return False

    def get_expect_graphic_type(self):
        """
        The graphic type in VM XML is different for different target.
        """
        # 'ori_graphic' only can be set when hypervior is KVM. For Xen and
        # Esx, it will always be 'None' and 'vnc' will be set by default.
        graphic_type = self.params.get('ori_graphic', 'vnc')
        # Video modle will change to QXL if convert target is ovirt/RHEVM
        if self.target == 'ovirt':
            graphic_type = 'spice'
        return graphic_type

    def get_expect_video_model(self):
        """
        The video model in VM XML is different in different situation.
        """
        video_model = 'cirrus'
        # Video modle will change to QXL if convert target is ovirt/RHEVM
        if self.target == 'ovirt':
            video_model = 'qxl'
        # Since RHEL7.3(virt-v2v-1.32.1-1.el7), video model will change to
        # QXL for linux VMs
        if self.os_type == 'linux':
            if self.compare_version(V2V_7_3_VERSION):
                video_model = 'qxl'
        # Video model will change to QXL for Windows2008r2 and windows7
        if self.os_version in ['win7', 'win2008r2']:
            video_model = 'qxl'
            # video mode of windows guest will be cirrus if there is no virtio-win
            # driver installed and environment 'VIRTIO_WIN' is not set on host
            if process.run(
                'rpm -q virtio-win',
                    ignore_status=True).exit_status != 0 and not os.getenv('VIRTIO_WIN'):
                video_model = 'cirrus'
        return video_model

    def check_metadata_libosinfo(self):
        """
        Check if metadata libosinfo attributes value in vm xml match with given param.

        Note: This is not a mandatory checking, if you need to check it, you have to
        set related parameters correctly.
        """
        def _guess_long_id(short_id):
            """
            If libosinfo doesn't have the short_id of an OS, we have to
            guess the final long_id based on the short_id.

            This usually happens when v2v server is on a lower rhel version,
            but the guest has a higher rhel version. On which the libosinfo
            doesn't include the guest info.
            """

            # 'winnt' must precede 'win'
            # 'rhel-atomic' must precede 'rhel'
            os_list = [
                'rhel-atomic',
                'rhel',
                'sles',
                'centos',
                'opensuse',
                'debian',
                'ubuntu',
                'fedora',
                'winnt',
                'win']
            long_id = ''

            for os_i in os_list:
                ptn = r'(%s)(\S+)' % os_i
                res = re.search(ptn, short_id)
                if not res:
                    continue
                os_name, os_ver = res.group(1), res.group(2).lstrip('-')

                if os_name == 'rhel':
                    long_id = 'http://redhat.com/%s/%s' % (os_name, os_ver)
                elif os_name == 'sles':
                    long_id = 'http://suse.com/%s/%s' % (
                        os_name, os_ver.replace('sp', '.'))
                elif os_name == 'centos':
                    long_id = 'http://centos.org/%s/%s' % (os_name, os_ver)
                elif os_name == 'opensuse':
                    long_id = 'http://opensuse.org/%s/%s' % (os_name, os_ver)
                elif os_name == 'debian':
                    long_id = 'http://debian.org/%s/%s' % (os_name, os_ver)
                elif os_name == 'ubuntu':
                    long_id = 'http://ubuntu.com/%s/%s' % (os_name, os_ver)
                elif os_name == 'fedora':
                    long_id = 'http://fedoraproject.org/%s/%s' % (
                        os_name, os_ver)
                elif os_name in ['winnt', 'win']:
                    long_id = 'http://microsoft.com/%s/%s' % (os_name, os_ver)
                else:
                    logging.debug("Guess long id failed")

                break

            if not long_id:
                raise exceptions.TestError(
                    'Cannot guess long id for %s' % short_id)

            return long_id

        def _id_short_to_long(short_id):
            """
            Convert short_id to long_id
            """
            cmd = 'osinfo-query os --fields=short-id | tail -n +3'
            # Too much debug output if verbose is True
            output = process.run(
                cmd,
                timeout=20,
                shell=True,
                ignore_status=True,
                verbose=False)
            short_id_all = output.stdout_text.splitlines()
            if short_id not in [os_id.strip() for os_id in short_id_all]:
                logging.info("Not found shourt_id '%s' on host", short_id)
                long_id = _guess_long_id(short_id)
            else:
                cmd = "osinfo-query os --fields=id short-id='%s'| tail -n +3" % short_id
                output = process.run(
                    cmd,
                    timeout=20,
                    verbose=True,
                    shell=True,
                    ignore_status=True)
                long_id = output.stdout_text.strip()

            return long_id

        logging.info("Checking metadata libosinfo")
        # 'os_short_id' must be set for libosinfo checking, you can query it by
        # 'osinfo-query os'
        short_id = self.params.get('os_short_id')
        if not short_id:
            reason = 'short_id is not set'
            logging.info(
                'Skip Checking metadata libosinfo parameters: %s' %
                reason)
            return

        # Checking if the feature is supported
        if not self.compare_version(FEATURE_SUPPORT['libosinfo']):
            reason = "Unsupported if v2v < %s" % FEATURE_SUPPORT['libosinfo']
            logging.info(
                'Skip Checking metadata libosinfo parameters: %s' %
                reason)
            return

        # Need target or output_mode be set explicitly
        if not self.params.get(
                'target') and not self.params.get('output_mode'):
            reason = 'Both target and output_mode are not set'
            logging.info(
                'Skip Checking metadata libosinfo parameters: %s' %
                reason)
            return

        supported_output = ['libvirt', 'local']
        # Skip checking if any of them is not in supported list
        if self.params.get('target') not in supported_output or self.params.get(
                'output_mode') not in supported_output:
            reason = 'target or output_mode is not in %s' % supported_output
            logging.info(
                'Skip Checking metadata libosinfo parameters: %s' %
                reason)
            return

        long_id = _id_short_to_long(short_id)

        # '<libosinfo:os id' was changed to '<ns0:os id' after calling
        # vm_xml.VMXML.new_from_inactive_dumpxml.
        # It's problably a problem in vm_xml.
        # <TODO>  Fix it
        #libosinfo_pattern = r'<libosinfo:os id="%s"/>' % long_id
        # A temp workaround for above problem
        libosinfo_pattern = r'<.*?:os id="%s"/>' % long_id
        logging.info('libosinfo pattern: %s' % libosinfo_pattern)

        if not re.search(libosinfo_pattern, self.vmxml):
            self.log_err('Not find metadata libosinfo')

    def check_video_model(self, video_type, dev_id):
        """
        Check expected video module on VM
        :param video_type: the expected video type
        :param dev_id: the ID of the video device
        :return: log error will be recored if not found, else return nothing
        """
        # Check by 'lspci' or 'lshw'
        cmd = ["lspci", "lshw"]
        if self.checker.vm_general_search(
            cmd,
            video_type,
            re.IGNORECASE,
                ignore_status=True):
            return
        elif len(dev_id) > 0 and any([self.checker.vm_general_search(cmd, id_i, debug=False, ignore_status=True) for id_i in dev_id]):
            return

        # Check by 'journalctl'
        if self.checker.vm_journal_search(video_type, "--since -20m"):
            return

        # Check by xorg log
        if self.checker.vm_xorg_search(video_type):
            return

        err_msg = "Not find %s device" % video_type
        self.log_err(err_msg)

    def get_device_id_by_name(self, devname):
        """
        Return device id by device name
        :param devname: a device's name provided by RedHat
        """
        # All pci device which provided by Red Hat, Inc.
        # https://devicehunt.com/view/type/pci/vendor/1AF4/
        # https://devicehunt.com/view/type/pci/vendor/1B36
        virtio_name_id_mapping = {
            'Virtio network device': ['1000', '1041'],
            'Virtio block device': ['1001', '1042'],
            'Virtio memory balloon': ['1002', '1045'],
            'Virtio console': ['1003', '1043'],
            'Virtio SCSI': ['1004', '1048'],
            'Virtio RNG': ['1005', '1044'],
            'Virtio filesystem': ['1009', '1049'],
            'Virtio GPU': ['1050'],
            'Virtio input': ['1052'],
            'Inter-VM shared memory': ['1110'],
            # QXL paravirtual graphic card
            'qxl': ['0100'],
            # Cirrus Logic
            'cirrus': ['1100']}

        if devname not in virtio_name_id_mapping.keys():
            logging.debug('Unknown RedHat virtio device: %s' % devname)
            return []
        return virtio_name_id_mapping[devname]

    def get_expected_boottype(self, boottype):
        """
        Return chipset and boottype of the VM.
        :param boottype: a value stands for boottype
        """

        # The value is [chipset, boottype, secure_boot]
        boottype_mapping = {0: ['i440fx', 'bios', False],
                            1: ['q35', 'bios', False],
                            2: ['q35', 'uefi', False],
                            3: ['q35', 'uefi', True]}

        if boottype not in range(4):
            raise exceptions.TestError(
                'Invalid boottype value: %s' %
                str(boottype))

        logging.debug("expected boot type is %s" % boottype_mapping[boottype])
        return boottype_mapping[boottype]

    def check_vm_boottype(self):
        """
        Check boottype of the guest
        """
        if self.boottype in [
                2, 3] and not self.checker.is_uefi_guest() or self.boottype in [
                0, 1] and self.checker.is_uefi_guest():
            err_msg = "Incorrect boottype of VM"
            self.log_err(err_msg)

    def check_vm_xml(self):
        """
        Checking XML info of the VM.
        """
        logging.debug('vmxml is:\n%s' % self.vmxml)

        logging.info("Checking graphic type in VM XML")
        expect_graphic = self.get_expect_graphic_type()
        logging.info("Expect type: %s", expect_graphic)
        pattern = r"<graphics type='(\w+)'"
        vmxml_graphic_type = re.search(pattern, self.vmxml).group(1)
        if vmxml_graphic_type != expect_graphic:
            err_msg = "Not find %s type graphic in VM XML" % expect_graphic
            self.log_err(err_msg)

        logging.info("Checking video model type in VM XML")
        expect_video = self.get_expect_video_model()
        logging.info("Expect driver: %s", expect_video)
        pattern = r"<video>\s+<model type='(\w+)'"
        vmxml_video_type = re.search(pattern, self.vmxml).group(1)
        if vmxml_video_type != expect_video:
            err_msg = "Not find %s type video in VM XML" % expect_video
            self.log_err(err_msg)

        logging.info("Checking boot os info in VM XML")
        chipset, bootinfo, secboot = self.get_expected_boottype(self.boottype)

        chip_pattern = r"machine='pc-%s" % ('q35' if chipset ==
                                            'q35' else 'i440fx')
        if bootinfo == 'uefi':
            boot_pattern = r"secure='%s' type='pflash'" % (
                'yes' if secboot else 'no')
            # v2v doesn't support secure boot to ovirt
            if self.target == "ovirt":
                boot_pattern = boot_pattern.replace('yes', 'no')
        else:
            boot_pattern = None

        pattern_list = [chip_pattern, boot_pattern]
        if not all([re.search(pattern_i, self.vmxml)
                    for pattern_i in pattern_list if pattern_i]):
            err_msg = "Checking boot os info failed"
            self.log_err(err_msg)

    def check_linux_vm(self):
        """
        Check linux VM after v2v convert.
        Only for RHEL VMs(RHEL4 or later)
        """
        self.checker.create_session()
        # Check OS vender and distribution
        logging.info("Checking VM os info")
        os_info = self.checker.get_vm_os_info()
        os_vendor = self.checker.get_vm_os_vendor()
        logging.info("OS: %s", (os_info))
        if os_vendor not in ['Red Hat', 'SUSE', 'Ubuntu', 'Debian']:
            logging.warn("Skip %s VM check" % os_vendor)
            return

        # Check OS kernel
        logging.info("Checking VM kernel")
        kernel_version = self.checker.get_vm_kernel()
        logging.info("Kernel: %s", kernel_version)
        if re.search('xen', kernel_version, re.IGNORECASE):
            err_msg = "xen kernel still exist after convert"
            self.log_err(err_msg)

        # Check virtio module
        logging.info("Checking virtio kernel modules")
        modules = self.checker.get_vm_modules()
        if not re.search("virtio", modules):
            err_msg = "Not find virtio module"
            self.log_err(err_msg)

        # Check boottype of the guest
        self.check_vm_boottype()

        # Check virtio PCI devices
        logging.info("Checking virtio PCI devices")
        pci_devs = self.checker.get_vm_pci_list()
        virtio_devs = ["Virtio network device",
                       "Virtio block device",
                       "Virtio memory balloon"]
        # Virtio RNG supports from kernel-2.6.26
        # https://wiki.qemu.org/Features/VirtIORNG
        if self.compare_version(FEATURE_SUPPORT['virtio_rng'], kernel_version):
            virtio_devs.append("Virtio RNG")
        logging.info("Virtio devices checking list: %s", virtio_devs)
        for dev in virtio_devs:
            if not re.search(dev, pci_devs, re.IGNORECASE):
                # Some devices may not be recognized by old guests.
                # Then lspci will display as 'Unclassifed device' with
                # device ID.
                # e.g.
                # Unclassified device [00ff]: Red Hat, Inc Device 1005
                if not any([re.search(dev_id, pci_devs, re.IGNORECASE)
                            for dev_id in self.get_device_id_by_name(dev)]):
                    err_msg = "Not find %s" % dev
                    self.log_err(err_msg)

        # Check virtio disk partition
        logging.info("Checking virtio disk partition")
        if not self.checker.is_disk_virtio():
            err_msg = "Not found virtio disk"
            self.log_err(err_msg)

        if os_vendor in ['Red Hat', 'SUSE']:
            if not self.checker.is_uefi_guest() and not self.checker.get_grub_device():
                err_msg = "Not find vd? in device.map"
                if self.hypervisor != 'kvm':
                    self.log_err(err_msg)
                else:
                    # Just warning the err if converting from KVM. It may
                    # happen that disk's bus type in xml is not the real bus
                    # type be used when preparing the image. Ex, if the image
                    # is installed with IDE, then you import the image with
                    # bus virtio, the device.map file will be inconsistent with
                    # the xml.
                    # V2V doesn't modify device.map file for this scenario.
                    logging.warning(err_msg)

        # Check graphic/video
        self.check_vm_xml()
        logging.info("Checking video device inside the VM")
        expect_video = self.get_expect_video_model()
        # Since RHEL7, it use 'kms' instead of 'cirrus'
        if 'rhel7' in self.os_version and expect_video == 'cirrus':
            expect_video = 'kms'
        self.check_video_model(
            expect_video,
            self.get_device_id_by_name(expect_video))

    def check_windows_vm(self):
        """
        Check windows guest after v2v convert.
        """
        try:
            # Sometimes windows guests needs >10mins to finish drivers
            # installation
            self.checker.create_session(timeout=900)
        except Exception as detail:
            raise exceptions.TestError(
                'Failed to connect to windows guest: %s' %
                detail)
        logging.info("Wait 60 seconds for installing drivers")
        time.sleep(60)
        # Close and re-create session in case connection reset by peer during
        # sleeping time. Keep trying until the test command runs successfully.
        for retry in range(RETRY_TIMES):
            try:
                self.checker.run_cmd('dir')
            except BaseException:
                self.checker.session.close()
                self.checker.create_session()
            else:
                break

        # Check boottype of the guest
        self.check_vm_boottype()

        # Check viostor file
        logging.info("Checking windows viostor info")
        output = self.checker.get_viostor_info()
        if not output:
            err_msg = "Not find viostor info"
            self.log_err(err_msg)

        # Check Red Hat VirtIO drivers and display adapter
        logging.info("Checking VirtIO drivers and display adapter")
        expect_drivers = ["Red Hat VirtIO SCSI",
                          "Red Hat VirtIO Ethernet Adapte"]
        # Windows display adapter is different for each release
        # Default value
        expect_adapter = 'Basic Display Driver'
        if self.os_version in ['win7', 'win2008r2']:
            expect_adapter = 'QXL'
        if self.os_version in ['win2003', 'win2008']:
            expect_adapter = 'Standard VGA Graphics Adapter'
        bdd_list = [
            'win8',
            'win8.1',
            'win10',
            'win2012',
            'win2012r2',
            'win2016',
            'win2019']
        if self.os_version in bdd_list:
            expect_adapter = 'Basic Display Driver'
        expect_drivers.append(expect_adapter)
        check_drivers = expect_drivers[:]
        for check_times in range(5):
            logging.info('Check drivers for the %dth time', check_times + 1)
            win_dirvers = self.checker.get_driver_info()
            for driver in expect_drivers:
                if driver in win_dirvers:
                    logging.info("Driver %s found", driver)
                    check_drivers.remove(driver)
                else:
                    err_msg = "Driver %s not found" % driver
                    logging.error(err_msg)
            expect_drivers = check_drivers[:]
            if not expect_drivers:
                break
            else:
                wait = 60
                logging.info('Wait another %d seconds...', wait)
                time.sleep(wait)
        if expect_drivers:
            for driver in expect_drivers:
                self.log_err("Not find driver: %s" % driver)

        # Check graphic and video type in VM XML
        if self.compare_version(V2V_7_3_VERSION):
            self.check_vm_xml()

        # Renew network
        logging.info("Renew network for windows guest")
        if not self.checker.get_network_restart():
            err_msg = "Renew network failed"
            self.log_err(err_msg)

    def check_graphics(self, param):
        """
        Check if graphics attributes value in vm xml match with given param.
        """
        logging.info('Check graphics parameters')
        if self.target == 'ovirt':
            xml = virsh.dumpxml(
                self.vm_name,
                extra='--security-info',
                session_id=self.virsh_session_id).stdout
            vmxml = xml_utils.XMLTreeFile(xml)
            graphic = vmxml.find('devices').find('graphics')
        else:
            vmxml = vm_xml.VMXML.new_from_inactive_dumpxml(
                self.vm_name, options='--security-info',
                virsh_instance=self.virsh_session)
            graphic = vmxml.xmltreefile.find('devices').find('graphics')
        status = True
        for key in param:
            logging.debug('%s = %s' % (key, graphic.get(key)))
            if graphic.get(key) != param[key]:
                logging.error('Attribute "%s" match failed' % key)
                status = False
        if not status:
            self.log_err('Graphic parameter check failed')

    def check_genid(self):
        """
        Check genid value in vm xml match with given param.
        """
        def _compose_genid(vm_genid, vm_genidX):
            for index, val in enumerate(
                    map(lambda x: hex(int(x) & ((1 << 64) - 1)), [vm_genid, vm_genidX])):
                # Remove 'L' suffix for python2
                val = val.rstrip('L')
                if index == 0:
                    gen_id = '-'.join([val[n:] if n == -8 else val[n:n + 4]
                                       for n in range(-8, -17, -4)])
                elif index == 1:
                    temp_str = ''.join([val[i:i + 2]
                                        for i in range(0, len(val), 2)][:0:-1])
                    gen_idX = temp_str[:4] + '-' + temp_str[4:]
            return gen_id + '-' + gen_idX

        has_genid = self.params.get('has_genid')
        if not has_genid:
            return

        # Checking if the feature is supported
        if not self.compare_version(FEATURE_SUPPORT['genid']):
            reason = "Unsupported if v2v < %s" % FEATURE_SUPPORT['genid']
            logging.info('Skip Checking genid: %s' % reason)
            return

        supported_output = ['libvirt', 'local', 'qemu']
        # Skip checking if any of them is not in supported list
        if self.params.get('output_mode') not in supported_output:
            reason = 'output_mode is not in %s' % supported_output
            logging.info('Skip Checking genid: %s' % reason)
            return

        logging.info('Checking genid info in xml')
        logging.debug('vmxml is:\n%s' % self.vmxml)
        if has_genid == 'yes':
            mount_point = utils_v2v.v2v_mount(self.vmx_nfs_src, 'vmx_nfs_src')
            # For clean up
            self.mount_records[len(self.mount_records)] = (
                self.vmx_nfs_src, mount_point, None)

            cmd = "cat {}/{name}/{name}.vmx".format(
                mount_point, name=self.original_vm_name)
            cmd_result = process.run(cmd, timeout=20, ignore_status=True)
            cmd_result.stdout = cmd_result.stdout_text
            genid_pattern = r'vm.genid = "(-?\d+)"'
            genidX_pattern = r'vm.genidX = "(-?\d+)"'

            genid_list = [
                re.search(
                    i, cmd_result.stdout).group(1) if re.search(
                    i, cmd_result.stdout) else None for i in [
                    genid_pattern, genidX_pattern]]
            if not all(genid_list):
                logging.info(
                    'vm.genid or vm.genidX is missing:%s' %
                    genid_list)
                # genid will not be in vmxml
                if re.search(r'genid', self.vmxml):
                    self.log_err('Unexpected genid in xml')
                return

            genid_str = _compose_genid(*genid_list)
            logging.debug('genid string is %s' % genid_str)

            if not re.search(genid_str, self.vmxml):
                self.log_err('Not find genid or genid is incorrect')
        elif has_genid == 'no':
            if re.search(r'genid', self.vmxml):
                self.log_err('Unexpected genid in xml')
