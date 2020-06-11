import os
import logging

from virttest import libvirt_vm
from virttest import defaults
from virttest import migration
from virttest import libvirt_version
from virttest import virsh
from virttest import utils_conn
from virttest import remote

from virttest.libvirt_xml import vm_xml
from virttest.utils_test import libvirt


def run(test, params, env):
    """
    Test migration with specified max bandwidth
    :param test: test object
    :param params: Dictionary with the test parameters
    :param env: Dictionary with test environment.
    """

    def check_bandwidth(params):
        """
        Check migration bandwidth

        :param params: the parameters used
        :raise: test.fail if migration bandwidth does not match expected values
        """
        pass

    def check_memballoon(vm):

        # Get original memory for later balloon function check
        ori_outside_mem = vm.get_max_mem()
        ori_guest_mem = vm.get_current_memory_size()
        # balloon half of the memory
        ballooned_mem = ori_outside_mem // 2
        # Set memory to test balloon function
        virsh.setmem(vm_name, ballooned_mem)
        # Check if memory is ballooned successfully
        logging.info("Check memory status")
        unusable_mem = ori_outside_mem - ori_guest_mem
        gcompare_threshold = int(
            params.get("guest_compare_threshold", unusable_mem))
        after_mem = vm.get_current_memory_size()
        act_threshold = ballooned_mem - after_mem
        if (after_mem > ballooned_mem) or (
                abs(act_threshold) > gcompare_threshold):
            test.fail("Balloon test failed")

    migr_vm_back = "yes" == params.get("migr_vm_back", "no")
    remote_ip = params.get("remote_ip")
    remote_user = params.get("remote_user")
    remote_pwd = params.get("remote_pwd")
    local_ip = params.get("local_ip")
    local_pwd = params.get("local_pwd")
    virsh_opt = params.get("virsh_opt", "--live")
    ballooned_mem = params.get("ballooned_mem")
    check = params.get("check")
    remote_virsh_dargs = {'remote_ip': remote_ip, 'remote_user': remote_user,
                          'remote_pwd': remote_pwd, 'unprivileged_user': None,
                          'ssh_remote_auth': True}

    migration_test = migration.MigrationTest()
    migration_test.check_parameters(params)

    # Params for NFS shared storage
    shared_storage = params.get("migrate_shared_storage", "")
    if shared_storage == "":
        default_guest_asset = defaults.get_default_guest_os_info()['asset']
        default_guest_asset = "%s.qcow2" % default_guest_asset
        shared_storage = os.path.join(params.get("nfs_mount_dir"),
                                      default_guest_asset)
        logging.debug("shared_storage:%s", shared_storage)

    # Params to update disk using shared storage
    params["disk_type"] = "file"
    params["disk_source_protocol"] = "netfs"
    params["mnt_path_name"] = params.get("nfs_mount_dir")

    # Local variables
    virsh_args = {"debug": True}
    virsh_options = params.get("virsh_options", "")
    options = params.get("virsh_migrate_options", "--live --verbose")
    func_params_exists = "yes" == params.get("func_params_exists", "yes")
    log_file = params.get("libvirt_log", "/var/log/libvirt/libvirtd.log")
    check_str_local_log = params.get("check_str_local_log", "")
    libvirtd_conf_dict = eval(params.get("libvirtd_conf_dict", '{}'))

    func_name = None
    libvirtd_conf = None
    mig_result = None

    if not libvirt_version.version_compare(6, 0, 0):
        test.cancel("This libvirt version doesn't support "
                    "postcopy migration bandwidth function.")

    # params for migration connection
    params["virsh_migrate_desturi"] = libvirt_vm.complete_uri(
        params.get("migrate_dest_host"))
    dest_uri = params.get("virsh_migrate_desturi")

    vm_name = params.get("migrate_main_vm")
    vm = env.get_vm(vm_name)
    vm.verify_alive()

    extra_args = {}
    if func_params_exists:
        extra_args.update({'func_params': params})
    func_name = check_bandwidth

    # For safety reasons, we'd better back up  xmlfile.
    new_xml = vm_xml.VMXML.new_from_inactive_dumpxml(vm_name)
    orig_config_xml = new_xml.copy()

    try:
        # Update memory balloon device to correct model
        membal_dict = {'membal_model': 'virtio',
                       'membal_stats_period': '10'}
        libvirt.update_memballoon_xml(new_xml, membal_dict)

        # Change the disk of the vm
        libvirt.set_vm_disk(vm, params)

        # Update libvirtd configuration
        if libvirtd_conf_dict:
            if os.path.exists(log_file):
                logging.debug("Delete local libvirt log file '%s'", log_file)
                os.remove(log_file)
            logging.debug("Update libvirtd configuration file")
            libvirtd_conf = libvirt.customize_libvirt_config(libvirtd_conf_dict)

        if not vm.is_alive():
            vm.start()

        logging.debug("Guest xml after starting:\n%s",
                      vm_xml.VMXML.new_from_dumpxml(vm_name))

        # Check local guest network connection before migration
        vm.wait_for_login(restart_network=True).close()
        migration_test.ping_vm(vm, params)

        # Execute migration process
        vms = [vm]

        migration_test.do_migration(vms, None, dest_uri, 'orderly',
                                    options, thread_timeout=900,
                                    ignore_status=True, virsh_opt=virsh_options,
                                    func=func_name, **extra_args)

        mig_result = migration_test.ret
        migration_test.check_result(mig_result, params)

        if int(mig_result.exit_status) == 0:
            migration_test.ping_vm(vm, params, uri=dest_uri)

        if check_str_local_log:
            libvirt.check_logfile(check_str_local_log, log_file)

        if check == "mem_balloon":
            remote_virsh_session = virsh.VirshPersistent(**remote_virsh_dargs)
            remote_virsh_session.setmem(vm_name, ballooned_mem, None, None,
                                        False, "", **virsh_args)
            memstat_ouput = remote_virsh_session.dommemstat(vm_name, "",
                                                            **virsh_args)
            memstat_after = memstat_ouput.stdout
            mem_after = memstat_after.splitlines()[0].split()[1]
            remote_virsh_session.close_session()
            if mem_after != ballooned_mem:
                test.fail("memory is not ballooned to the expect size!")

        # Create a remote runner for later use
        runner_on_target = remote.RemoteRunner(host=remote_ip,
                                               username=remote_user,
                                               password=remote_pwd)

        if migr_vm_back:
            ssh_connection = utils_conn.SSHConnection(server_ip=remote_ip,
                                                      server_pwd=remote_pwd,
                                                      client_ip=local_ip,
                                                      client_pwd=local_pwd)
            try:
                ssh_connection.conn_check()
            except utils_conn.ConnectionError:
                ssh_connection.conn_setup()
                ssh_connection.conn_check()

            # Pre migration setup for local machine
            src_full_uri = libvirt_vm.complete_uri(
                params.get("migrate_source_host"))

            migration_test.migrate_pre_setup(src_full_uri, params)
            cmd = "virsh migrate %s %s %s" % (vm_name,
                                              virsh_opt, src_full_uri)
            logging.debug("Start migration: %s", cmd)
            cmd_result = remote.run_remote_cmd(cmd, params, runner_on_target)
            logging.info(cmd_result)
            if cmd_result.exit_status:
                destroy_cmd = "virsh destroy %s" % vm_name
                remote.run_remote_cmd(destroy_cmd, params, runner_on_target,
                                      ignore_status=False)
                test.fail("Failed to run '%s' on remote: %s"
                          % (cmd, cmd_result))
    finally:
        logging.debug("Recover test environment")
        # Clean VM on destination and source
        try:
            migration_test.cleanup_dest_vm(vm, vm.connect_uri, dest_uri)
            if vm.is_alive():
                vm.destroy(gracefully=False)
        except Exception as err:
            logging.error(err)

        logging.info("Recovery VM XML configration")
        orig_config_xml.sync()

        if libvirtd_conf:
            logging.debug("Recover the configurations")
            libvirt.customize_libvirt_config(None, is_recover=True,
                                             config_object=libvirtd_conf)
