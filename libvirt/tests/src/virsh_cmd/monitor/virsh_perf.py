import logging
from virttest.libvirt_xml import vm_xml
from virttest import virsh
from virttest import virt_vm


def check_event_value(vm_name, perf_option, event):
    """
    Check if the domstats output has a line for the event
    1. if perf_option == --disable, there isn't a line
    2. if perf_option == --enable, there is a line
    """
    logging.debug("check_event_value: vm_name= %s, perf_option=%s, event=%s"
                  % (vm_name, perf_option, event))

    ret = False
    result = virsh.domstats(vm_name, "--perf", ignore_status=True,
                            debug=True)
    status = result.exit_status
    if status:
        logging.error("Run command failed in check_event_value")
        return ret
    output = result.stdout.strip()
    logging.debug("domstats output is %s" % output)

    if perf_option != '--disable':
        for line in output.split('\n'):
            if '.' in line and event == (line.split('.')[1]).split('=')[0]:
                ret = True
                return ret
    else:
        ret = True
        for line in output.split('\n'):
            if '.' in line and event == (line.split('.')[1]).split('=')[0]:
                ret = False
                return ret
    return ret


def check_perf_result(vm_name, perf_option, events):
    """
    Check if the perf events enabled/disabled as expected and return the events
    in wrong state
    """
    ret_event = ""
    # if there is a event list, get the first events group
    events_list = events.strip().split(' ')
    for event in events_list[0].split(','):
        if not check_event_value(vm_name, perf_option, event):
            print ("event:%s, perf_option:%s" % (event, perf_option))
            ret_event = ret_event + event
            return ret_event
    if len(events_list) > 1:
        for event in events_list[1].split(','):
            if perf_option.strip() == '--enable':
                perf_opt = '--disable'
            else:
                perf_opt = '--enable'
            if not check_event_value(vm_name, perf_opt, event):
                print ("evt:%s, perf_opt:%s" % (event, perf_opt))
                ret_event = ret_event + event
                return ret_event
    return ret_event


def run(test, params, env):
    """
    Test command: virsh perf
    1. prepare vm
    2  Perform virsh perf operation.
    3. Confirm the test result
    4. Recover test environment
    """

    vm_name = params.get("main_vm", "avocado-vt-vm1")
    test_vm = env.get_vm(vm_name)
    perf_option = params.get("perf_option")
    events = params.get("events")
    virsh_opt = params.get("virsh_opt")
    vm_active = params.get("vm_active")
    status_error = (params.get("status_error", "no") == "yes")

    try:
        vmxml = vm_xml.VMXML.new_from_inactive_dumpxml(test_vm.name)
        backup_xml = vmxml.copy()

        # To test disable action, enable the events first
        if perf_option == '--disable':
            result = virsh.perf(vm_name, "--enable", events, "",
                                ignore_status=True, debug=True)
            status = result.exit_status
            # output = result.stdout.strip()
            if status_error:
                test.fail("Failed to enable the events before disable them!")

        result = virsh.perf(vm_name, perf_option, events, virsh_opt,
                            ignore_status=True, debug=True)
        status = result.exit_status
        # output = result.stdout.strip()

        if status_error:
            if not status:
                # if "argument unsupported: parameter" in result.stderr:
                #    test.cancel(result.stderr)
                test.fail("Run successfully with wrong command!")
        else:
            if status:
                if ("unable to enable/disable perf events" in
                   result.stderr.lower()):
                    test.cancel("Some of the events is not supported")
                else:
                    test.fail("Run failed with right command")
            else:
                # "--config" and "--live" can be used together, we need to
                # check the effect for both two parameters
                if (virsh_opt.strip().find('--config') != -1 and
                   virsh_opt.strip().find('--live') != -1):
                    result = check_perf_result(vm_name, perf_option, events)
                    if (result != ""):
                        test.fail("Check domstats output failed for " + result
                                  + "with --config --live and no vm restarted")
                # Event is enabled/disabled immediately when vm is active and
                # "--config" is not used, or otherwise it affects when vm start
                # or restart
                if not vm_active:
                    try:
                        test_vm.start()
                    except virt_vm.VMStartError as info:
                        if (str(info).find("unable to enable host cpu") != 1):
                            test.cancel("Some of the events is not supported")
                elif vm_active and virsh_opt.strip().find('--config') != -1:
                    test_vm.destroy()
                    try:
                        test_vm.start()
                    except virt_vm.VMStartError as info:
                        if (str(info).find("unable to enable host cpu") != 1):
                            test.cancel("Some of the events is not supported")
                result = check_perf_result(vm_name, perf_option, events)
                if (result != ""):
                    test.fail("Check domstats output failed for " + result)
                # print "the ouput is : %s" % output
    finally:
        try:
            test_vm.destroy(gracefully=False)
        except AttributeError:
            pass
        # for backup_xml in backup_xml_list:
        backup_xml.sync()
