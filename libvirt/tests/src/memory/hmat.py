import logging

from virttest import virsh
from virttest.libvirt_xml import vm_xml
from virttest.utils_libvirt import libvirt_cpu
from virttest.utils_test import libvirt


def run(test, params, env):
    """
    Test hmat of memory
    """
    vm_name = params.get('main_vm')

    qemu_checks = []
    for i in range(1, 5):
        qemu_checks.extend(params.get('qemu_checks%d' % i, '').split('`'))

    def check_numainfo_in_guest(check_list, content):
        """
        Check if numa information in guest is correct
        """
        content_str = ' '.join(content.split())
        logging.debug("content_str:%s" % content_str)
        for item in check_list:
            item_str = ' '.join(item.split(' '))
            if content_str.find(item_str) != -1:
                logging.info(item)
            else:
                test.fail('Last item %s not in content' % item_str)

    def check_list_in_content(check_list, content):
        """
        Check if items in check_list are in content
        """
        for item in check_list:
            logging.debug("item:%s" % item)
            if item in content:
                logging.info(item)
            else:
                logging.error(item)
                test.fail('Last item not in content')

    def create_cell_distances_xml():
        """
        Create cell distances xml for test
        """
        cpu_xml = vmxml.cpu
        i = 0
        cells = []

        for numacell_xml in cpu_xml.numa_cell:
            logging.debug("numacell_xml:%s" % numacell_xml)
            cell_distances_xml = numacell_xml.CellDistancesXML()
            cell_distances_xml.update({'sibling': eval(params.get('sibling%s' % i))})
            numacell_xml.distances = cell_distances_xml
            i = i + 1
            cells.append(numacell_xml)
        cpu_xml.numa_cell = cells
        logging.debug(cpu_xml)
        vmxml.cpu = cpu_xml
        vmxml.sync()

    def create_hmat_xml():
        """
        Create hmat xml for test
        """
        cpu_xml = vmxml.cpu
        i = 0
        cells = []

        for numacell_xml in cpu_xml.numa_cell:
            logging.debug("numacell_xml:%s" % numacell_xml)
            caches = []
            cell_cache = eval(params.get('cell_cache%s' % i))
            cellcache_xml = vm_xml.CellCacheXML()
            cellcache_xml.update(cell_cache)
            logging.debug("cellcach_xml:%s" % cellcache_xml)
            caches.append(cellcache_xml)
            numacell_xml.caches = caches
            logging.debug("numacell_xml:%s" % numacell_xml)
            i = i + 1
            cells.append(numacell_xml)
        cpu_xml.numa_cell = cells

        latency_list = eval(params.get('latency'))
        bandwidth_list = eval(params.get('bandwidth'))
        interconnects_xml = vm_xml.VMCPUXML().InterconnectsXML()
        interconnects_xml.latency = latency_list
        interconnects_xml.bandwidth = bandwidth_list

        cpu_xml.interconnects = interconnects_xml
        logging.debug(cpu_xml)
        vmxml.cpu = cpu_xml
        vmxml.sync()

    bkxml = vm_xml.VMXML.new_from_inactive_dumpxml(vm_name)

    chk_case = params.get('chk')
    try:
        vm = env.get_vm(vm_name)
        vmxml = vm_xml.VMXML.new_from_inactive_dumpxml(vm_name)

        # Set cpu according to params
        libvirt_cpu.add_cpu_settings(vmxml, params)
        if chk_case == "hmat":
            create_hmat_xml()
        if chk_case == "cell_distances":
            create_cell_distances_xml()
        logging.debug(virsh.dumpxml(vm_name))

        virsh.start(vm_name, debug=True, ignore_status=False)

        # Check qemu command line one by one
        for item in qemu_checks:
            libvirt.check_qemu_cmd_line(item)

        vm_session = vm.wait_for_login()
        if chk_case == "hmat":
            dmsg_list = []
            for i in range(1, 5):
                dmsg_list.extend(params.get('dmsg_checks%d' % i, '').split('`'))
            content = vm_session.cmd('dmesg').strip()
            check_list_in_content(dmsg_list, content)

        if chk_case == "cell_distances":
            # Install numactl in guest
            vm_session.cmd('yum install -y numactl')
            check_list = []
            for i in range(1, 4):
                check_list.extend(params.get('numactl_exp%d' % i, '').split('`'))
            numactl_output = vm_session.cmd('numactl -H').strip()
            logging.debug("numactl_output: %s" % type(numactl_output))
            check_numainfo_in_guest(check_list, numactl_output)

    finally:
        if vm.is_alive():
            vm.destroy(gracefully=False)
        bkxml.sync()
