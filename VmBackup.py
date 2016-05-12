#!/usr/bin/python

# Copyright (C) 2016  Northern Arizona University

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

# Title: NAUbackup / VmBackup - a XenServer vm-export and vdi-export Backup Script
# Package Contents: README.md, VmBackup.py (this file), example.cfg
# Version History
# - v3.1 2016/05/11 Added the option to re-use retain values for the snapback-script
# - v3.0 2016/01/20 Added vdi-export for VMs with too many/large disks for vm-export
# - v2.1 2014/08/22 Added email status option
# - v2.0 2014/04/09 New VmBackup version (supersedes all previous NAUbackup versions)

# ** DO NOT RUN THIS SCRIPT UNLESS YOU ARE COMFORTABLE WITH THESE ACTIONS. **
# => To accomplish the vm backup this script uses the following xe commands
#   vm-export:  (a) vm-snapshot, (b) template-param-set, (c) vm-export, (d) vm-uninstall on vm-snapshot
#   vdi-export: (a) vdi-snapshot, (b) vdi-param-set, (c) vdi-export, (d) vdi-destroy on vdi-snapshot

# See README for usage and installation documentation.
# See example.cfg for config file example usage.

# Usage w/ vm name for single vm backup, which runs vm-export by default:
#    ./VmBackup.py <vm-name>

# Usage w/ config file for multiple vm backups, where you can specify either vm-export or vdi-export:
#    ./VmBackup.py <config-file-path>

import sys, time, os, datetime, subprocess, re, shutil, XenAPI, smtplib, re, base64
from email.MIMEText import MIMEText
from subprocess import PIPE
from subprocess import STDOUT
from os.path import join
from distutils.version import StrictVersion

############################# HARD CODED DEFAULTS
# modify these hard coded default values, only used if not specified in config file
DEFAULT_POOL_DB_BACKUP = 0
DEFAULT_MAX_BACKUPS = 4
DEFAULT_VDI_EXPORT_FORMAT = 'raw' # xe vdi-export options: 'raw' or 'vhd'
DEFAULT_BACKUP_DIR = '/snapshots/BACKUPS'
## DEFAULT_BACKUP_DIR = '\snapshots\BACKUPS' # alt for CIFS mounts
DEFAULT_STATUS_LOG = '/var/log/NAUbackup.log'
# note - some NAS file servers may fail with ':', so change to your desired format
BACKUP_DIR_PATTERN = '%s/backup-%04d-%02d-%02d-(%02d:%02d:%02d)'

############################# OPTIONAL
# optional email may be triggered by configure next 3 parameters then find MAIL_ENABLE and uncommenting out the desired lines
MAIL_TO_ADDR = 'your-email@your-domain'
# note if MAIL_TO_ADDR has ipaddr then you may need to change the smtplib.SMTP() call
MAIL_FROM_ADDR = 'your-from-address@your-domain'
MAIL_SMTP_SERVER = 'your-mail-server'

config = {}
wildcards = {}
expected_keys = ['pool_db_backup', 'max_backups', 'backup_dir', 'vdi_export_format', 'vm-export', 'vdi-export', 'exclude', 'status_log', 'password', 'password_from_file', 'pool_host']
message = ''
xe_path = '/opt/xensource/bin' 

def main(session):

    success_cnt = 0
    warning_cnt = 0
    error_cnt = 0

    #setting autoflush on (aka unbuffered)
    sys.stdout = os.fdopen(sys.stdout.fileno(), 'w', 0)

    server_name = os.uname()[1].split('.')[0]
    if config_specified:
        status_log_begin(server_name)

    log('===========================')
    log('VmBackup running on %s ...' % server_name)
    df_snapshots('Space before backups: df -Th %s' % config['backup_dir'])

    if int(config['pool_db_backup']):
        log('*** begin backup_pool_metadata ***')
        if not backup_pool_metadata(server_name):
            error_cnt += 1

            ######################################################################
    # Iterate through all vdi-export= in cfg
    log('************ vdi-export= ***************')
    if not vdi_export_available:
        vm_names = [get_vm_name(vm_parm) for vm_parm in config['vdi-export']]
        log('Error XenVersion lower than 6.5, no vdi-export available. Skipping %s' % (vm_names))

    else:
        for vm_parm in config['vdi-export']:
            log('*** vdi-export begin %s' % vm_parm)
            beginTime = datetime.datetime.now()
            this_status = 'success'

            # get values from vdi-export=
            vm_name = get_vm_name(vm_parm)
            vm_max_backups = get_vm_max_backups(vm_parm)
            log('vdi-export - vm_name: %s max_backups: %s' % (vm_name, vm_max_backups))

            if config_specified:
                status_log_vdi_export_begin(server_name, '%s' % vm_name)

            # verify vm_name exists with only one instance for this name
            #  returns error-message or vm_object if success
            vm_object = verify_vm_name(vm_name)
            if 'ERROR' in vm_object:
                log('verify_vm_name: %s' % vm_object)
                if config_specified:
                    status_log_vdi_export_end(server_name, 'ERROR verify_vm_name %s' % vm_name)
                error_cnt += 1
                # next vm
                continue

            vm_record = session.xenapi.VM.get_record(vm_object)
            # check for custom field "retain"
            if 'XenCenter.CustomFields.retain' in vm_record['other_config'].keys():
                vm_max_backups = int(vm_record['other_config']['XenCenter.CustomFields.retain'])

            vm_backup_dir = os.path.join(config['backup_dir'], vm_name)
            # cleanup any old unsuccessful backups and create new full_backup_dir
            full_backup_dir = process_backup_dir(vm_backup_dir)

            # gather_vm_meta produces status: empty or warning-message
            #   and globals: vm_uuid, xvda_uuid, xvda_uuid
            vm_meta_status = gather_vm_meta(vm_object, full_backup_dir)
            if vm_meta_status != '':
                log('WARNING gather_vm_meta: %s' % vm_meta_status)
                this_status = 'warning'
                # non-fatal - finsh processing for this vm
            # vdi-export only uses xvda_uuid, xvda_uuid
            if xvda_uuid == '':
                log('ERROR gather_vm_meta has no xvda-uuid')
                if config_specified:
                    status_log_vdi_export_end(server_name, 'ERROR xvda-uuid not found %s' % vm_name)
                error_cnt += 1
                # next vm
                continue
            if xvda_name_label == '':
                log('ERROR gather_vm_meta has no xvda-name-label')
                if config_specified:
                    status_log_vdi_export_end(server_name, 'ERROR xvda-name-label not found %s' % vm_name)
                error_cnt += 1
                # next vm
                continue

            # -----------------------------------------
            # --- begin vdi-export command sequence ---
            log ('*** vdi-export begin xe command sequence')
            # is vm currently running?
            cmd = '%s/xe vm-list name-label="%s" params=power-state | /bin/grep running' % (xe_path, vm_name)
            if run_log_out_wait_rc(cmd) == 0:
                log ('vm is running')
            else:
                log ('vm is NOT running')

            # list the vdi we will backup
            cmd = '%s/xe vdi-list uuid=%s' % (xe_path, xvda_uuid)
            log('1.cmd: %s' % cmd)
            if run_log_out_wait_rc(cmd) != 0:
                log('ERROR %s' % cmd)
                if config_specified:
                    status_log_vdi_export_end(server_name, 'VDI-LIST-FAIL %s' % vm_name)
                error_cnt += 1
                # next vm
                continue

            # check for old vdi-snapshot for this xvda
            snap_vdi_name_label = 'SNAP_%s_%s' % (vm_name, xvda_name_label)
            # replace all spaces with '-'
            snap_vdi_name_label = re.sub(r' ', r'-', snap_vdi_name_label)
            log ('check for prev-vdi-snapshot: %s' % snap_vdi_name_label)
            cmd = "%s/xe vdi-list name-label='%s' params=uuid | /bin/awk -F': ' '{print $2}' | /bin/grep '-'" % (xe_path, snap_vdi_name_label)
            old_snap_vdi_uuid = run_get_lastline(cmd)
            if old_snap_vdi_uuid != '':
                log ('cleanup old-snap-vdi-uuid: %s' % old_snap_vdi_uuid)
                # vdi-destroy old vdi-snapshot
                cmd = '%s/xe vdi-destroy uuid=%s' % (xe_path, old_snap_vdi_uuid)
                log('cmd: %s' % cmd)
                if run_log_out_wait_rc(cmd) != 0:
                    log('WARNING %s' % cmd)
                    this_status = 'warning'
                    # non-fatal - finsh processing for this vm

            # take a vdi-snapshot of this vm
            cmd = '%s/xe vdi-snapshot uuid=%s' % (xe_path, xvda_uuid)
            log('2.cmd: %s' % cmd)
            snap_vdi_uuid = run_get_lastline(cmd)
            log ('snap-uuid: %s' % snap_vdi_uuid)
            if snap_vdi_uuid == '':
                log('ERROR %s' % cmd)
                if config_specified:
                    status_log_vdi_export_end(server_name, 'VDI-SNAPSHOT-FAIL %s' % vm_name)
                error_cnt += 1
                # next vm
                continue

            # change vdi-snapshot to unique name-label for easy id and cleanup
            cmd = '%s/xe vdi-param-set uuid=%s name-label="%s"' % (xe_path, snap_vdi_uuid, snap_vdi_name_label)
            log('3.cmd: %s' % cmd)
            if run_log_out_wait_rc(cmd) != 0:
                log('ERROR %s' % cmd)
                if config_specified:
                    status_log_vdi_export_end(server_name, 'VDI-PARAM-SET-FAIL %s' % vm_name)
                error_cnt += 1
                # next vm
                continue

            # actual-backup: vdi-export vdi-snapshot
            cmd = '%s/xe vdi-export format=%s uuid=%s' % (xe_path, config['vdi_export_format'], snap_vdi_uuid)
            full_path_backup_file = os.path.join(full_backup_dir, vm_name + '.%s' % config['vdi_export_format'])
            cmd = '%s filename="%s"' % (cmd, full_path_backup_file)
            log('4.cmd: %s' % cmd)
            if run_log_out_wait_rc(cmd) == 0:
                log('vdi-export success')
            else:
                log('ERROR %s' % cmd)
                if config_specified:
                    status_log_vdi_export_end(server_name, 'VDI-EXPORT-FAIL %s' % vm_name)
                error_cnt += 1
                # next vm
                continue

            # cleanup: vdi-destroy vdi-snapshot
            cmd = '%s/xe vdi-destroy uuid=%s' % (xe_path, snap_vdi_uuid)
            log('5.cmd: %s' % cmd)
            if run_log_out_wait_rc(cmd) != 0:
                log('WARNING %s' % cmd)
                this_status = 'warning'
                # non-fatal - finsh processing for this vm

            log ('*** vdi-export end')
            # --- end vdi-export command sequence ---
            # ---------------------------------------

            elapseTime = datetime.datetime.now() - beginTime
            backup_file_size = os.path.getsize(full_path_backup_file) / (1024 * 1024 * 1024)
            final_cleanup( full_path_backup_file, backup_file_size, full_backup_dir, vm_backup_dir, vm_max_backups)

            if not check_all_backups_success(vm_backup_dir):
                log('WARNING cleanup needed - not all backup history is successful')
                this_status = 'warning'

            if (this_status == 'success'):
                success_cnt += 1
                log('VmBackup vdi-export %s - ***Success*** t:%s' % (vm_name, str(elapseTime.seconds/60)))
                if config_specified:
                    status_log_vdi_export_end(server_name, 'SUCCESS %s,elapse:%s size:%sG' % (vm_name, str(elapseTime.seconds/60), backup_file_size))

            elif (this_status == 'warning'):
                warning_cnt += 1
                log('VmBackup vdi-export %s - ***WARNING*** t:%s' % (vm_name, str(elapseTime.seconds/60)))
                if config_specified:
                    status_log_vdi_export_end(server_name, 'WARNING %s,elapse:%s size:%sG' % (vm_name, str(elapseTime.seconds/60), backup_file_size))

            else:
                # this should never occur since all errors do a continue on to the next vm_name
                error_cnt += 1
                log('VmBackup vdi-export %s - +++ERROR-INTERNAL+++ t:%s' % (vm_name, str(elapseTime.seconds/60)))
                if config_specified:
                    status_log_vdi_export_end(server_name, 'ERROR-INTERNAL %s,elapse:%s size:%sG' % (vm_name, str(elapseTime.seconds/60), backup_file_size))

    # end of for vm_parm in config['vdi-export']:
    ######################################################################


    ######################################################################
    # Iterate through VMs
    log('************ vm-export= ***************')
    for vm_parm in config['vm-export']:
        log('*** vm-export begin %s' % vm_parm)
        beginTime = datetime.datetime.now()
        this_status = 'success'

        # get values from vdi-export=
        vm_name = get_vm_name(vm_parm)
        vm_max_backups = get_vm_max_backups(vm_parm)
        log('vm-export - vm_name: %s max_backups: %s' % (vm_name, vm_max_backups))

        if config_specified:
            status_log_vm_export_begin(server_name, '%s' % vm_name)

        # verify vm_name exists with only one instance for this name
        #  returns error-message or vm_object if success
        vm_object = verify_vm_name(vm_name)
        if 'ERROR' in vm_object:
            log('verify_vm_name: %s' % vm_object)
            if config_specified:
                status_log_vm_export_end(server_name, 'ERROR verify_vm_name %s' % vm_name)
            error_cnt += 1
            # next vm
            continue

        vm_backup_dir = os.path.join(config['backup_dir'], vm_name)
        # cleanup any old unsuccessful backups and create new full_backup_dir
        full_backup_dir = process_backup_dir(vm_backup_dir)

        # gather_vm_meta produces status: empty or warning-message 
        #   and globals: vm_uuid, xvda_uuid, xvda_uuid
        vm_meta_status = gather_vm_meta(vm_object, full_backup_dir)
        if vm_meta_status != '':
            log('WARNING gather_vm_meta: %s' % vm_meta_status)
            this_status = 'warning'
            # non-fatal - finsh processing for this vm
        # vm-export only uses vm_uuid
        if vm_uuid == '':
            log('ERROR gather_vm_meta has no vm-uuid')
            if config_specified:
                status_log_vm_export_end(server_name, 'ERROR vm-uuid not found %s' % vm_name)
            error_cnt += 1
            # next vm
            continue

        # ----------------------------------------
        # --- begin vm-export command sequence ---
        log ('*** vm-export begin xe command sequence')
        # is vm currently running?
        cmd = '%s/xe vm-list name-label="%s" params=power-state | /bin/grep running' % (xe_path, vm_name)
        if run_log_out_wait_rc(cmd) == 0:
            log ('vm is running')
        else:
            log ('vm is NOT running')

        # check for old vm-snapshot for this vm
        snap_name = 'RESTORE_%s' % vm_name
        log ('check for prev-vm-snapshot: %s' % snap_name)
        cmd = "%s/xe vm-list name-label='%s' params=uuid | /bin/awk -F': ' '{print $2}' | /bin/grep '-'" % (xe_path, snap_name)
        old_snap_vm_uuid = run_get_lastline(cmd)
        if old_snap_vm_uuid != '':
            log ('cleanup old-snap-vm-uuid: %s' % old_snap_vm_uuid)
            # vm-uninstall old vm-snapshot
            cmd = '%s/xe vm-uninstall uuid=%s force=true' % (xe_path, old_snap_vm_uuid)
            log('cmd: %s' % cmd)
            if run_log_out_wait_rc(cmd) != 0:
                log('WARNING-ERROR %s' % cmd)
                this_status = 'warning'
                if config_specified:
                    status_log_vm_export_end(server_name, 'VM-UNINSTALL-FAIL-1 %s' % vm_name)
                    # non-fatal - finsh processing for this vm

        # take a vm-snapshot of this vm
        cmd = '%s/xe vm-snapshot vm=%s new-name-label="%s"' % (xe_path, vm_uuid, snap_name)
        log('1.cmd: %s' % cmd)
        snap_vm_uuid = run_get_lastline(cmd)
        log ('snap-uuid: %s' % snap_vm_uuid)
        if snap_vm_uuid == '':
            log('ERROR %s' % cmd)
            if config_specified:
                status_log_vm_export_end(server_name, 'SNAPSHOT-FAIL %s' % vm_name)
            error_cnt += 1
            # next vm
            continue

        # change vm-snapshot so that it can be referenced by vm-export
        cmd = '%s/xe template-param-set is-a-template=false ha-always-run=false uuid=%s' % (xe_path, snap_vm_uuid)
        log('2.cmd: %s' % cmd)
        if run_log_out_wait_rc(cmd) != 0:
            log('ERROR %s' % cmd)
            if config_specified:
                status_log_vm_export_end(server_name, 'TEMPLATE-PARAM-SET-FAIL %s' % vm_name)
            error_cnt += 1
            # next vm
            continue

        vdi_cf = get_custom_field(vm_object, 'vdi_backup')
        if vdi_cf is not None and vdi_cf.lower() == 'true' and vdi_export_available:

            # vdi-export of vm-snapshot

            snap_vm_object = session.xenapi.VM.get_by_uuid(snap_vm_uuid)
            snap_vm_record = session.xenapi.VM.get_record(snap_vm_object)

            for vbd in snap_vm_record['VBDs']:
                vbd_record = session.xenapi.VBD.get_record(vbd)
                # For each vbd, find out if its a disk
                if vbd_record['type'].lower() != 'disk':
                    continue
                vbd_record_device = vbd_record['device']
                vdi_record = session.xenapi.VDI.get_record(vbd_record['VDI'])
                vdi_uuid = vdi_record['uuid']
                vdi_name_label = vdi_record['name_label']

                # actual-backup: vdi-export vdi-snapshot
                cmd = '%s/xe vdi-export format=%s uuid=%s' % (xe_path, config['vdi_export_format'], vdi_uuid)
                full_path_backup_file = os.path.join(
                        full_backup_dir,
                        vm_name + '_' + vbd_record_device + '_' + vdi_name_label + '.%s' % config['vdi_export_format']
                )
                cmd = '%s filename="%s"' % (cmd, full_path_backup_file)
                log('3.cmd: %s' % cmd)
                if run_log_out_wait_rc(cmd) == 0:
                    log('vdi-export success')
                else:
                    log('ERROR %s' % cmd)
                    if config_specified:
                        status_log_vdi_export_end(server_name, 'VDI-EXPORT-FAIL %s' % vm_name)
                    error_cnt += 1
                    # next vm
                    continue

        else:
            if vdi_cf is not None and vdi_cf.lower() == 'true':
                log('WARNING XenVersion lower than 6.5, no vdi-export available, doing vm-export instead')

            # vm-export vm-snapshot
            cmd = '%s/xe vm-export uuid=%s' % (xe_path, snap_vm_uuid)
            if compress:
                full_path_backup_file = os.path.join(full_backup_dir, vm_name + '.xva.gz')
                cmd = '%s filename="%s" compress=true' % (cmd, full_path_backup_file)
            else:
                full_path_backup_file = os.path.join(full_backup_dir, vm_name + '.xva')
                cmd = '%s filename="%s"' % (cmd, full_path_backup_file)
            log('3.cmd: %s' % cmd)
            if run_log_out_wait_rc(cmd) == 0:
                log('vm-export success')
            else:
                log('ERROR %s' % cmd)
                if config_specified:
                    status_log_vm_export_end(server_name, 'VM-EXPORT-FAIL %s' % vm_name)
                error_cnt += 1
                # next vm
                continue

        # vm-uninstall vm-snapshot
        cmd = '%s/xe vm-uninstall uuid=%s force=true' % (xe_path, snap_vm_uuid)
        log('4.cmd: %s' % cmd)
        if run_log_out_wait_rc(cmd) != 0:
            log('WARNING %s' % cmd)
            this_status = 'warning'
            # non-fatal - finsh processing for this vm

        log ('*** vm-export end')
        # --- end vm-export command sequence ---
        # ----------------------------------------

        elapseTime = datetime.datetime.now() - beginTime
        backup_file_size = os.path.getsize(full_path_backup_file) / (1024 * 1024 * 1024)
        final_cleanup( full_path_backup_file, backup_file_size, full_backup_dir, vm_backup_dir, vm_max_backups)

        if not check_all_backups_success(vm_backup_dir):
            log('WARNING cleanup needed - not all backup history is successful')
            this_status = 'warning'

        if (this_status == 'success'):
            success_cnt += 1
            log('VmBackup vm-export %s - ***Success*** t:%s' % (vm_name, str(elapseTime.seconds/60)))
            if config_specified:
                status_log_vm_export_end(server_name, 'SUCCESS %s,elapse:%s size:%sG' % (vm_name, str(elapseTime.seconds/60), backup_file_size))

        elif (this_status == 'warning'):
            warning_cnt += 1
            log('VmBackup vm-export %s - ***WARNING*** t:%s' % (vm_name, str(elapseTime.seconds/60)))
            if config_specified:
                status_log_vm_export_end(server_name, 'WARNING %s,elapse:%s size:%sG' % (vm_name, str(elapseTime.seconds/60), backup_file_size))

        else:
            # this should never occur since all errors do a continue on to the next vm_name
            error_cnt += 1
            log('VmBackup vm-export %s - +++ERROR-INTERNAL+++ t:%s' % (vm_name, str(elapseTime.seconds/60)))
            if config_specified:
                status_log_vm_export_end(server_name, 'ERROR-INTERNAL %s,elapse:%s size:%sG' % (vm_name, str(elapseTime.seconds/60), backup_file_size))

    # end of for vm_parm in config['vm-export']:
    ######################################################################

    log('===========================')
    df_snapshots('Space status: df -Th %s' % config['backup_dir'])

    # gather a final VmBackup.py status
    summary = 'S:%s W:%s E:%s' % (success_cnt, warning_cnt, error_cnt)
    if (error_cnt > 0):
        if config_specified:
            status_log_end(server_name, 'ERROR,%s' % summary)
            # MAIL_ENABLE: optional email may be enabled by uncommenting out the next two lines
            #send_email(MAIL_TO_ADDR, 'ERROR VmBackup.py', status_log)
            #open('%s' % status_log, 'w').close() # trunc status log after email
        log('VmBackup ended - **ERRORS DETECTED** - %s' % summary)
    elif (warning_cnt > 0):
        if config_specified:
            status_log_end(server_name, 'WARNING,%s' % summary)
            # MAIL_ENABLE: optional email may be enabled by uncommenting out the next two lines
            #send_email("%s 'WARNING VmBackup.py' %s" % (MAIL_TO_ADDR, status_log))
            #open('%s' % status_log, 'w').close() # trunc status log after email
        log('VmBackup ended - **WARNING(s)** - %s' % summary)
    else:
        if config_specified:
            status_log_end(server_name, 'SUCCESS,%s' % summary)
            # MAIL_ENABLE: optional email may be enabled by uncommenting out the next two lines
            #send_email(MAIL_TO_ADDR, 'Success VmBackup.py', status_log)
            #open('%s' % status_log, 'w').close() # trunc status log after email
        log('VmBackup ended - Success - %s' % summary)

        # done with main()
        ######################################################################
def isInt(s):
    try:
        int(s)
        return True
    except ValueError:
        return False

def get_vm_max_backups(vm_parm): 
    # get max_backups from optional vm-export=VM-NAME:MAX-BACKUP override
    # NOTE - if not present then return config['max_backups']
    if vm_parm.find(':') == -1:
        return int(config['max_backups'])
    else:
        (vm_name,tmp_max_backups) = vm_parm.split(':')
        tmp_max_backups = int(tmp_max_backups)
        if (tmp_max_backups > 0):
            return tmp_max_backups
        else:
            return int(config['max_backups'])

def is_vm_backups_valid(vm_parm): 
    if vm_parm.find(':') == -1:
        # valid since we will use config['max_backups']
        return True
    else:
        # a value has been specified - is it valid?
        (vm_name,tmp_max_backups) = vm_parm.split(':')
        if isInt(tmp_max_backups):
            return tmp_max_backups > 0
        else:
            return False

def get_vm_backups(vm_parm): 
    # get max_backups from optional vm-export=VM-NAME:MAX-BACKUP override
    # NOTE - if not present then return empty string '' else return whatever specified after ':'
    if vm_parm.find(':') == -1:
        return ''
    else:
        (vm_name,tmp_max_backups) = vm_parm.split(':')
        return tmp_max_backups

def get_vm_name(vm_parm): 
    # get vm_name from optional vm-export=VM-NAME:MAX-BACKUP override
    if (vm_parm.find(':') == -1):
        return vm_parm
    else:
        (tmp_vm_name,tmp_max_backups) = vm_parm.split(':')
        return tmp_vm_name

def verify_vm_name(tmp_vm_name):
    vm = session.xenapi.VM.get_by_name_label(tmp_vm_name)
    if (len(vm) > 1):
        return 'ERROR more than one vm with the name %s' % tmp_vm_name
    elif (len(vm) == 0):
        return 'ERROR no machines found with the name %s' % tmp_vm_name
    return vm[0]

def gather_vm_meta(vm_object, tmp_full_backup_dir):
    global vm_uuid
    global xvda_uuid
    global xvda_name_label
    vm_uuid = ''
    xvda_uuid = ''
    xvda_name_label = ''
    tmp_error = '';
    vm_record = session.xenapi.VM.get_record(vm_object)

    # Backup vm meta data
    log ('Writing vm config file.')
    vm_out = open ('%s/vm.cfg' % tmp_full_backup_dir, 'w')
    vm_out.write('name_label=%s\n' % vm_record['name_label'])
    vm_out.write('name_description=%s\n' % vm_record['name_description'].encode('utf-8'))
    vm_out.write('memory_dynamic_max=%s\n' % vm_record['memory_dynamic_max'])
    vm_out.write('VCPUs_max=%s\n' % vm_record['VCPUs_max'])
    vm_out.write('VCPUs_at_startup=%s\n' % vm_record['VCPUs_at_startup'])
    # notice some keys are not always available
    try:
        # notice list within list : vm_record['other_config']['base_template_name']
        vm_out.write('base_template_name=%s\n' % vm_record['other_config']['base_template_name'])
    except KeyError:
        # ignore
        pass
    vm_out.write('os_version=%s\n' % get_os_version(vm_record['uuid']))
    # get orig uuid for special metadata disaster recovery
    vm_out.write('orig_uuid=%s\n' % vm_record['uuid'])
    vm_uuid = vm_record['uuid']
    vm_out.close()
       
    # Write metadata files for vdis and vbds.  These end up inside of a DISK- directory.
    log ('Writing disk info')
    vbd_cnt = 0
    for vbd in vm_record['VBDs']:
        log('vbd: %s' % vbd)
        vbd_record = session.xenapi.VBD.get_record(vbd)  
        # For each vbd, find out if its a disk
        if vbd_record['type'].lower() != 'disk':
            continue
        vbd_record_device = vbd_record['device']
        if vbd_record_device == '':
            # not normal - flag as warning. 
            # this seems to occur on some vms that have not been started in a long while,
            #   after starting the vm this blank condition seems to go away.
            tmp_error += 'empty vbd_record[device] on vbd: %s ' % vbd
            # if device is not available then use counter as a alternate reference
            vbd_cnt += 1
            vbd_record_device = vbd_cnt

        vdi_record = session.xenapi.VDI.get_record(vbd_record['VDI'])
        log('disk: %s - begin' % vdi_record['name_label'])

        # now write out the vbd info.
        device_path = '%s/DISK-%s' % (tmp_full_backup_dir,  vbd_record_device)
        os.mkdir(device_path)
        vbd_out = open('%s/vbd.cfg' % device_path, 'w')
        vbd_out.write('userdevice=%s\n' % vbd_record['userdevice'])
        vbd_out.write('bootable=%s\n' % vbd_record['bootable'])
        vbd_out.write('mode=%s\n' % vbd_record['mode'])
        vbd_out.write('type=%s\n' % vbd_record['type'])
        vbd_out.write('unpluggable=%s\n' % vbd_record['unpluggable'])
        vbd_out.write('empty=%s\n' % vbd_record['empty'])
        # get orig uuid for special metadata disaster recovery
        vbd_out.write('orig_uuid=%s\n' % vbd_record['uuid'])
        # other_config and qos stuff is not backed up
        vbd_out.close()

        # now write out the vdi info.
        vdi_out = open('%s/vdi.cfg' % device_path, 'w')
        vdi_out.write('name_label=%s\n' % vdi_record['name_label'])
        vdi_out.write('name_description=%s\n' % vdi_record['name_description'].encode('utf-8'))
        vdi_out.write('virtual_size=%s\n' % vdi_record['virtual_size'])
        vdi_out.write('type=%s\n' % vdi_record['type'])
        vdi_out.write('sharable=%s\n' % vdi_record['sharable'])
        vdi_out.write('read_only=%s\n' % vdi_record['read_only'])
        # get orig uuid for special metadata disaster recovery
        vdi_out.write('orig_uuid=%s\n' % vdi_record['uuid'])
        sr_uuid = session.xenapi.SR.get_record(vdi_record['SR'])['uuid']
        vdi_out.write('orig_sr_uuid=%s\n' % sr_uuid)
        # other_config and qos stuff is not backed up
        vdi_out.close()
        if vbd_record_device == 'xvda':
            xvda_uuid = vdi_record['uuid']
            xvda_name_label = vdi_record['name_label']

    # Write metadata files for vifs.  These are put in VIFs directory
    log ('Writing VIF info')
    for vif in vm_record['VIFs']:
        vif_record = session.xenapi.VIF.get_record(vif)
        log ('Writing VIF: %s' % vif_record['device'])
        device_path = '%s/VIFs' % tmp_full_backup_dir
        if (not os.path.exists(device_path)):
            os.mkdir(device_path)
        vif_out = open('%s/vif-%s.cfg' % (device_path, vif_record['device']), 'w') 
        vif_out.write('device=%s\n' % vif_record['device'])
        network_name = session.xenapi.network.get_record(vif_record['network'])['name_label']
        vif_out.write('network_name_label=%s\n' % network_name)
        vif_out.write('MTU=%s\n' % vif_record['MTU'])
        vif_out.write('MAC=%s\n' % vif_record['MAC'])
        vif_out.write('other_config=%s\n' % vif_record['other_config'])
        vif_out.write('orig_uuid=%s\n' % vif_record['uuid'])
        vif_out.close()

    return tmp_error

def get_custom_field(vm_object, vm_custom_field):
    vm_record = session.xenapi.VM.get_record(vm_object)
    if 'XenCenter.CustomFields.' + vm_custom_field in vm_record['other_config'].keys():
        return vm_record['other_config']['XenCenter.CustomFields.' + vm_custom_field]

    return None

def final_cleanup( tmp_full_path_backup_file, tmp_backup_file_size, tmp_full_backup_dir, tmp_vm_backup_dir, tmp_vm_max_backups):
    # mark this a successful backup, note: this will 'touch' a file named 'success'
    # if backup size is greater than 60G, then nfs server side compression occurs
    if tmp_backup_file_size > 60:
        log('*** LARGE FILE > 60G: %s : %sG' % (tmp_full_path_backup_file, tmp_backup_file_size))
        # forced compression via background gzip (requires nfs server side script)
        open('%s/success_compress' % tmp_full_backup_dir, 'w').close()
        log('*** success_compress: %s : %sG' % (tmp_full_path_backup_file, tmp_backup_file_size))
    else:
        open('%s/success' % tmp_full_backup_dir, 'w').close()
        log('*** success: %s : %sG' % (tmp_full_path_backup_file, tmp_backup_file_size))

    # Remove oldest if more than tmp_vm_max_backups
    dir_to_remove = get_dir_to_remove(tmp_vm_backup_dir, tmp_vm_max_backups)
    while (dir_to_remove):
        log ('Deleting oldest backup %s/%s ' % (tmp_vm_backup_dir, dir_to_remove))
        # remove dir - if throw exception then stop processing
        shutil.rmtree(tmp_vm_backup_dir + '/' + dir_to_remove)
        dir_to_remove = get_dir_to_remove(tmp_vm_backup_dir, tmp_vm_max_backups)

# cleanup old unsuccessful backup and create new full_backup_dir
def process_backup_dir(tmp_vm_backup_dir):

    if (not os.path.exists(tmp_vm_backup_dir)):
        # Create new dir - if throw exception then stop processing
        os.mkdir(tmp_vm_backup_dir)

    # if last backup was not successful, then delete it
    dir_not_success = get_last_backup_dir_that_failed(tmp_vm_backup_dir)
    if (dir_not_success):
        #if (not os.path.exists(tmp_vm_backup_dir + '/' + dir_not_success + '/fail')):
        log ('Delete last **unsuccessful** backup %s/%s ' % (tmp_vm_backup_dir, dir_not_success))
        # remove last unseccessful backup  - if throw exception then stop processing
        shutil.rmtree(tmp_vm_backup_dir + '/' + dir_not_success)

    # create new backup dir
    return create_full_backup_dir(tmp_vm_backup_dir)

# Setup full backup dir structure
def create_full_backup_dir(vm_base_path):
    # Check that directory exists
    if not os.path.exists(vm_base_path):
        # Create new dir - if throw exception then stop processing
        os.mkdir(vm_base_path)

    date = datetime.datetime.today()
    tmp_backup_dir = BACKUP_DIR_PATTERN \
    % (vm_base_path, date.year, date.month, date.day, date.hour, date.minute, date.second)
    log('new backup_dir: %s' % tmp_backup_dir)

    if not os.path.exists(tmp_backup_dir):
        # Create new dir - if throw exception then stop processing
        os.mkdir(tmp_backup_dir)

    return tmp_backup_dir

# Setup meta dir structure
def get_meta_path(base_path):
    # Check that directory exists
    if not os.path.exists(base_path):
        # Create new dir
        try:
            os.mkdir(base_path)
        except OSError, error:
            log('ERROR creating directory %s : %s' % (base_path, error.as_string()))
            return False

    date = datetime.datetime.today()
    backup_path = '%s/pool_db_%04d%02d%02d-%02d%02d%02d.dump' \
    % (base_path, date.year, date.month, date.day, date.hour, date.minute, date.second)

    return backup_path

def get_dir_to_remove(path, numbackups):
    # Find oldest backup and select for deletion
    dirs = os.listdir(path)
    dirs.sort()
    if (len(dirs) > numbackups and len(dirs) > 1):
        return dirs[0]
    else:
        return False

def get_last_backup_dir_that_failed(path):
    # if the last backup dir was not success, then return that backup dir
    dirs = os.listdir(path)
    if (len(dirs) <= 1):
        return False
    dirs.sort()
    # note: dirs[-1] is the last entry
    if (not os.path.exists(path + '/' + dirs[-1] + '/success')) and \
        (not os.path.exists(path + '/' + dirs[-1] + '/success_restore')) and \
        (not os.path.exists(path + '/' + dirs[-1] + '/success_compress' )) and \
        (not os.path.exists(path + '/' + dirs[-1] + '/success_compressing' )):
        return dirs[-1]
    else:
        return False

def check_all_backups_success(path):
    # expect at least one backup dir, and all should be successful
    dirs = os.listdir(path)
    if (len(dirs) == 0):
        return False
    for dir in dirs:
        if (not os.path.exists(path + '/' + dir + '/success' )) and \
            (not os.path.exists(path + '/' + dir + '/success_restore' )) and \
            (not os.path.exists(path + '/' + dir + '/success_compress' )) and \
            (not os.path.exists(path + '/' + dir + '/success_compressing' )):
            log("WARNING: directory not successful - %s" % dir)
            return False
    return True

def backup_pool_metadata(svr_name):

    # xe-backup-metadata can only run on master
    if not is_xe_master():
        log('** ignore: NOT master')
        return True

    metadata_base = os.path.join(config['backup_dir'], 'METADATA_' + svr_name)
    metadata_file = get_meta_path(metadata_base)

    cmd = "%s/xe pool-dump-database file-name='%s'" % (xe_path, metadata_file)
    log(cmd)
    if run_log_out_wait_rc(cmd) != 0:
        log('ERROR failed to backup pool metadata')
        return False

    return True

# some run notes with xe return code and output examples
#  xe vm-lisX -> error .returncode=1 w/ error msg
#  xe vm-list name-label=BAD-vm-name -> success .returncode=0 with no output
#  xe pool-dump-database file-name=<dup-file-already-exists> 
#     -> error .returncode=1 w/ error msg
def run_log_out_wait_rc(cmd, log_w_timestamp=True):
    child = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, shell=True)
    line = child.stdout.readline()
    while line:
        log(line.rstrip("\n"), log_w_timestamp)
        line = child.stdout.readline()
    return child.wait()

def run_get_lastline(cmd):
    # exec cmd - expect 1 line output from cmd
    # return last line
    f = os.popen(cmd)
    resp = ''
    for line in f.readlines():
        resp = line.rstrip("\n")
    return resp

def get_os_version(uuid):
    cmd = "%s/xe vm-list uuid='%s' params=os-version | /bin/grep 'os-version' | /bin/awk -F'name: ' '{print $2}' | /bin/awk -F'|' '{print $1}' | /bin/awk -F';' '{print $1}'" % (xe_path, uuid)
    return run_get_lastline(cmd)

def df_snapshots(log_msg):
    log(log_msg)
    f = os.popen('df -Th %s' % config['backup_dir'])
    for line in f.readlines():
        line = line.rstrip("\n")
        log(line)

def send_email(to, subject, body_fname):

    message = open('%s' % body_fname, 'r').read()

    msg = MIMEText(message)
    msg['subject'] = subject
    msg['From'] = MAIL_FROM_ADDR
    msg['To'] = to

    # note if using an ipaddress in MAIL_SMTP_SERVER, 
    # then may require smtplib.SMTP(MAIL_SMTP_SERVER, local_hostname="localhost")
    s = smtplib.SMTP(MAIL_SMTP_SERVER)
    s.sendmail(MAIL_FROM_ADDR, to.split(','), msg.as_string())
    s.quit()

def is_xe_master():
    # test to see if we are running on xe master

    cmd = '%s/xe pool-list params=master --minimal' % xe_path
    master_uuid = run_get_lastline(cmd)

    hostname = os.uname()[1]
    cmd = '%s/xe host-list name-label=%s --minimal' % (xe_path, hostname)
    host_uuid = run_get_lastline(cmd)

    if host_uuid == master_uuid:
        return True

    return False

def is_config_valid():

    if not isInt(config['pool_db_backup']):
        print 'ERROR: config pool_db_backup non-numeric -> %s' % config['pool_db_backup']
        return False
 
    if int(config['pool_db_backup']) != 0 and int(config['pool_db_backup']) != 1:
        print 'ERROR: config pool_db_backup out of range -> %s' % config['pool_db_backup']
        return False
 
    if not isInt(config['max_backups']):
        print 'ERROR: config max_backups non-numeric -> %s' % config['max_backups']
        return False
 
    if int(config['max_backups']) < 1:
        print 'ERROR: config max_backups out of range -> %s' % config['max_backups']
        return False

    if config['vdi_export_format'] != 'raw' and config['vdi_export_format'] != 'vhd':
        print 'ERROR: config vdi_export_format invalid -> %s' % config['vdi_export_format']
        return False

    if not os.path.exists(config['backup_dir']):
        print 'ERROR: config backup_dir does not exist -> %s' % config['backup_dir']
        return False

    tmp_return = True
    for vm_parm in config['vdi-export']:
        if not is_vm_backups_valid(vm_parm):
            print 'ERROR: vm_max_backup is invalid - %s' % vm_parm
            tmp_return = False

    for vm_parm in config['vm-export']:
        if not is_vm_backups_valid(vm_parm):
            print 'ERROR: vm_max_backup is invalid - %s' % vm_parm
            tmp_return = False

    return tmp_return

def config_load(path):
    return_value = True
    config_file = open(path, 'r')
    for line in config_file:
        if (not line.startswith('#') and len(line.strip()) > 0):
            (key,value) = line.strip().split('=')
            key = key.strip()
            value = value.strip()

            # check for valid keys
            if not key in expected_keys:
                if ignore_extra_keys:
                    log('ignoring config key: %s' % key)
                else:
                    print '***unexpected config key: %s' % key
                    return_value = False
            
            # save wildcards in wildcards[]
            if key in ['vm-export','vdi-export'] and get_vm_name(value).endswith('*'):
                # at this point value has form PRD* or PRD*:5
                if type(wildcards[key]) is list:
                    wildcards[key].append(value)
                else:
                    wildcards[key] = [wildcards[key], value]
                continue

            # save key/value in config[]
            save_to_config( key, value)

    return return_value

def save_to_config( key, value):
    # save key/value in config[]
    if key in config.keys():
        if type(config[key]) is list:
            config[key].append(value)
        else:
            config[key] = [config[key], value]
    else:
        config[key] = value

def verify_config_vms_exist():

    all_vms_exist = True
    # verify all VMs in vm/vdi-export exist
    vm_export_errors = verify_export_vms_exist()
    if vm_export_errors != '':
        all_vms_exist = False
        log('ERROR - vm(s) List does not exist: %s' % vm_export_errors)

    # verify all VMs in exclude exist
    vm_exclude_errors = verify_exclude_vms_exist()
    if vm_exclude_errors != '':
        all_vms_exist = False
        log('ERROR - vm(s) Exclude does not exist: %s' % vm_exclude_errors)

    return all_vms_exist

def verify_export_vms_exist():

    vm_error = ''
    for vm_parm in config['vdi-export']:
        # verify vm exists
        vm_name_part = get_vm_name(vm_parm)
        if not verify_vm_exist(vm_name_part):
            vm_error += vm_name_part + ' '

    for vm_parm in config['vm-export']:
        # verify vm exists
        vm_name_part = get_vm_name(vm_parm)
        if not verify_vm_exist(vm_name_part):
            vm_error += vm_name_part + ' '

    return vm_error

def verify_exclude_vms_exist():

    vm_error = ''
    for vm_parm in config['exclude']:
        # verify vm exists
        vm_name_part = get_vm_name(vm_parm)
        if not verify_vm_exist(vm_name_part):
            vm_error += vm_name_part + ' '

    return vm_error

def verify_vm_exist(vm_name):

    vm = session.xenapi.VM.get_by_name_label(vm_name)
    if (len(vm) == 0):
        return False
    else:
        return True

def expand_wildcards():

    for wildcard in wildcards['vdi-export']:
        convert_wildcard_to_config('vdi-export', wildcard)

    for wildcard in wildcards['vm-export']:
        convert_wildcard_to_config('vm-export', wildcard)

def convert_wildcard_to_config( key, wildcard):

    # wildcard has form PRD* or PRD*:5
    vm_name_part = get_vm_name(wildcard)
    vm_name_part = vm_name_part[:-1]  # remove last char '*'
    vm_backups_part = get_vm_backups(wildcard)
    if vm_backups_part != '':
        vm_backups_part = ':%s' % vm_backups_part

    # check if associated wildcard VMs are already in config[key] list
    if vm_name_part != '':
        # find VMs w/ prefix-wildcard
        vm_list = get_wildcard_vms(vm_name_part)
    else:
        # single '*' with no prefix - get all VMs
        vm_list = get_all_vms()
    for vm_new_candidate in vm_list:
        #log("<%s>" % vm_new_candidate) #debug
        if not is_name_in_list(vm_new_candidate, config[key]):
            # add this wildcard vm to config[key]
            save_to_config( key, '%s%s' % (vm_new_candidate, vm_backups_part))

def is_name_in_list(name, list):
    for item in list:
        if item.startswith(name): return True
    return False

def get_wildcard_vms(vm_wildcard):
    wildcard_vms = []
    cmd = "%s/xe vm-list is-control-domain=false params=name-label | /bin/grep ': %s' | /bin/awk -F': ' '{print $2}'" % (xe_path, vm_wildcard)
    #log('cmd: %s' % cmd) #debug
    f = os.popen(cmd)
    for line in f.readlines():
        resp = line.rstrip("\n")
        #log(resp) #debug
        wildcard_vms.append(resp)
    wildcard_vms.sort(key=str.lower)
    return wildcard_vms

def get_all_vms():
    all_vms = []
    cmd = "%s/xe vm-list is-control-domain=false params=name-label | /bin/grep ':' | /bin/awk -F': ' '{print $2}'" % xe_path
    #log('cmd: %s' % cmd) #debug
    f = os.popen(cmd)
    for line in f.readlines():
        resp = line.rstrip("\n")
        #log(resp) #debug
        all_vms.append(resp)
    all_vms.sort(key=str.lower)
    return all_vms

def show_vms_not_in_backup():
    # show all vm's not in backup scope
    all_vms = get_all_vms()
    for vm_parm in config['vdi-export']:
        # remove from all_vms
        vm_name_part = get_vm_name(vm_parm)
        if vm_name_part in all_vms:
            all_vms.remove(vm_name_part)

    for vm_parm in config['vm-export']:
        # remove from all_vms
        vm_name_part = get_vm_name(vm_parm)
        if vm_name_part in all_vms:
            all_vms.remove(vm_name_part)

    vms_not_in_backup = ''
    for vm_name in all_vms:
        vms_not_in_backup += vm_name + ' '
    log('VMs-not-in-backup: %s' % vms_not_in_backup)

def cleanup_vmexport_vdiexport_dups():
    # if any vdi-export's exist in vm-export's then remove from vm-export
    for vdi_parm in config['vdi-export']:
        # vdi_parm has form PRD-name or PRD-name:5
        tmp_vdi_parm = get_vm_name(vdi_parm)
        for vm_parm in config['vm-export']:
            tmp_vm_parm = get_vm_name(vm_parm)
            if tmp_vm_parm == tmp_vdi_parm:
                #log('vdi-export dup - remove vm-export=%s' % vm_parm) #debug
                config['vm-export'].remove(vm_parm)

def remove_excludes():
    for vm_name in config['exclude']:
        for vm_parm in config['vm-export']:
            tmp_vm_parm = get_vm_name(vm_parm)
            if tmp_vm_parm == vm_name:
                #log('exclude remove vm-export=%s' % vm_parm) #debug
                config['vm-export'].remove(vm_parm)
        for vdi_parm in config['vdi-export']:
            tmp_vdi_parm = get_vm_name(vdi_parm)
            if tmp_vdi_parm == vm_name:
                #log('exclude remove vdi-export=%s' % vdi_parm) #debug
                config['vdi-export'].remove(vdi_parm)

def config_load_defaults():
    # init config param not already loaded then load with default values
    if not 'pool_db_backup' in config.keys():
        config['pool_db_backup'] = str(DEFAULT_POOL_DB_BACKUP)
    if not 'max_backups' in config.keys():
        config['max_backups'] = str(DEFAULT_MAX_BACKUPS)
    if not 'vdi_export_format' in config.keys():
        config['vdi_export_format'] = str(DEFAULT_VDI_EXPORT_FORMAT)
    if not 'backup_dir' in config.keys():
        config['backup_dir'] = str(DEFAULT_BACKUP_DIR)
    if not 'status_log' in config.keys():
        config['status_log'] = str(DEFAULT_STATUS_LOG)
    if not 'pool_host' in config.keys():
        config['pool_host'] = False

def config_print():
    log('VmBackup.py running with these settings:')
    log('  backup_dir        = %s' % config['backup_dir'])
    log('  compress          = %s' % compress)
    log('  max_backups       = %s' % config['max_backups'])
    log('  vdi_export_format = %s' % config['vdi_export_format'])
    log('  pool_db_backup    = %s' % config['pool_db_backup'])
    log('  status_log     = %s' % config['status_log'])
    if config['pool_host']:
        log('  pool_host      = %s' % config['pool_host'])
    log('  exclude (cnt)= %s' % len(config['exclude']))
    str = ''
    for vm_parm in config['exclude']:
        str += '%s, ' % vm_parm
    if len(str) > 1:
        str = str[:-2]
    log('  exclude: %s' % str)

    log('  vdi-export (cnt)= %s' % len(config['vdi-export']))
    str = ''
    for vm_parm in config['vdi-export']:
        str += '%s, ' % vm_parm
    if len(str) > 1:
        str = str[:-2]
    log('  vdi-export: %s' % str)

    log('  vm-export (cnt)= %s' % len(config['vm-export']))
    str = ''
    print type(config['vm-export'])
    for vm_parm in config['vm-export']:
        str += '%s, ' % vm_parm
    if len(str) > 1:
        str = str[:-2]
    log('  vm-export: %s' % str)

def status_log_begin(server):
    rec_begin = '%s,vmbackup.py,%s,begin\n' % (fmtDateTime(), server)
    open(status_log,'a',0).write(rec_begin)

def status_log_end(server, status):
    rec_end = '%s,vmbackup.py,%s,end,%s\n' % (fmtDateTime(), server, status)
    open(status_log,'a',0).write(rec_end)

def status_log_vm_export_begin(server, status):
    rec_begin = '%s,vm-export,%s,begin,%s\n' % (fmtDateTime(), server, status)
    open(status_log,'a',0).write(rec_begin)

def status_log_vm_export_end(server, status):
    rec_end = '%s,vm-export,%s,end,%s\n' % (fmtDateTime(), server, status)
    open(status_log,'a',0).write(rec_end)

def status_log_vdi_export_begin(server, status):
    rec_begin = '%s,vdi-export,%s,begin,%s\n' % (fmtDateTime(), server, status)
    open(status_log,'a',0).write(rec_begin)

def status_log_vdi_export_end(server, status):
    rec_end = '%s,vdi-export,%s,end,%s\n' % (fmtDateTime(), server, status)
    open(status_log,'a',0).write(rec_end)

def fmtDateTime():
    date = datetime.datetime.today()
    str = '%02d/%02d/%02d %02d:%02d:%02d' \
        % (date.year, date.month, date.day, date.hour, date.minute, date.second)
    return str

def log(mes, log_w_timestamp=True):
    # note - send_email uses message
    global message

    date = datetime.datetime.today()
    if log_w_timestamp:
        str = '%02d-%02d-%02d-(%02d:%02d:%02d) - %s\n' \
            % (date.year, date.month, date.day, date.hour, date.minute, date.second, mes)
    else:
        str = '%s\n' % mes
    message += str

    #if verbose: (old option, now always verbose)
    str = str.rstrip("\n")
    print str
    sys.stdout.flush()
    sys.stderr.flush()

def usage():
    print 'Usage-basic:'
    print sys.argv[0], ' <config-file|vm-name> [compress=True|False] [ignore_extra_keys=True|False] \
                       [backup_running_vms=True|False]'
    print
    print 'see also: VmBackup.py help    - for additional parameter usage'
    print '      or: VmBackup.py config  - for config-file parameter usage'
    print '      or: VmBackup.py example - for some simple example usage'
    print

def usage_help():
    print 'Usage-help:'
    print sys.argv[0], ' <password|password-file> <config-file|vm-selector> [preview] [other optional params]'
    print
    print 'required params:'
    print '  <password|password-file> - xenserver password or obscured password stored in password-file'
    print '  <config-file|vm-selector> - several options:'
    print '    config-file - a common choice for production crontab execution'
    print '    vm-selector - a single vm name or vm prefix wildcard that defaults to vm-export'
    print '      note with vm-selector then config defaults are set from VmBackup.py default constantants'
    print '    vm-export=vm-selector  - explicit vm-export'
    print '    vdi-export=vm-selector - explicit vdi-export'
    print
    print 'optional params:'
    print '  [preview] - preview/validate VmBackup config parameters and xenserver password'
    print '  [compress=True|False] - only for vm-export functions automatic compression (default: False)'
    print '  [ignore_extra_keys=True|False] - some config files may have extra params (default: False)'
    print
    print 'alternate form - create-password-file:'
    print sys.argv[0], ' <password> create-password-file=filename'
    print
    print '  create-password-file=filename - create an obscured password file with the specified password'
    print '  note - password filename is relative to current path or absolute path.'
    print

def usage_config_file():
    print 'Usage-config-file:'
    print 
    print '  # Example config file for VmBackup.py'
    print
    print '  #### high level VmBackup settings ################'
    print '  #### note - if any of these are not specified ####'
    print '  ####   then VmBackup uses default constants   ####'
    print
    print '  # Take Xen Pool DB backup: 0=No, 1=Yes (script default to 0=No)'
    print '  pool_db_backup=0'
    print
    print '  # How many backups to keep for each vm (script default to 4)'
    print '  max_backups=3'
    print
    print '  #Backup Directory path (script default /snapshots/BACKUPS)'
    print '  backup_dir=/path/to/backupspace'
    print
    print '  # applicable if vdi-export is used'
    print '  # vdi_export_format either raw or vhd (script default to raw)'
    print '  vdi_export_format=raw'
    print
    print '  #### specific VMs backup settings ####'
    print
    print '  # vm-export VM name-label of vm to backup. One per line - notice :max_backups override.'
    print '  vm-export=my-vm-name'
    print '  vm-export=my-second-vm'
    print '  vm-export=my-third-vm:3'
    print
    print '  # special vdi-export - only backs up first disk. See README Documenation!'
    print '  vdi-export=my-vm-name'
    print
    print '  # vm-export using VM prefix wildcard - notice DEV* has :max_backups overide'
    print '  vm-export=PROD*'
    print '  vm-export=DEV*:2'
    print
    print '  # exclude selected VMs from VM prefix wildcards'
    print '  exclude=PROD-WinDomainController'
    print '  exclude=DEV-DestructiveTest'
    print

def usage_examples():
    print 'Usage-examples:'
    print
    print '  # config file'
    print '  ./VmBackup.py password weekend.cfg'
    print
    print '  # single VM name, which is case sensitive'
    print '  ./VmBackup.py password DEV-mySql'
    print
    print '  # single VM name using vdi-export instead of vm-export'
    print '  ./VmBackup.py password vdi-export=DEV-mySql'
    print
    print '  # single VM name with spaces in name'
    print '  ./VmBackup.py password "DEV mySql"'
    print
    print '  # VM prefix wildcard - which may be more than one VM'
    print '  ./VmBackup.py password DEV-my*'
    print
    print '  # all VMs in pool'
    print '  ./VmBackup.py password "*"'
    print
    print 'Alternate form - create-password-file:'
    print '  # create password file from command line password'
    print '  ./VmBackup.py password create-password-file=/root/VmBackup.pass'
    print
    print '  # use password file + config file'
    print '  ./VmBackup.py /root/VmBackup.pass monthly.cfg'
    print

if __name__ == '__main__':
    if 'help' in sys.argv or 'config' in sys.argv or 'example' in sys.argv:
        if 'help' in sys.argv: usage_help() 
        if 'config' in sys.argv: usage_config_file() 
        if 'example' in sys.argv: usage_examples() 
        sys.exit(1)
    if len(sys.argv) < 2:
        usage()
        sys.exit(1)
    cfg_file = sys.argv[1]

    # load optional params
    preview = False                 # default
    compress = False                # default
    ignore_extra_keys = False       # default
    backup_running_vms = False      # default

    # loop through remaining optional args
    arg_range = range(2,len(sys.argv))
    for arg_ix in arg_range:
        array = sys.argv[arg_ix].strip().split('=')
        if array[0].lower() == 'preview':
            preview = True
        elif array[0].lower() == 'compress':
            compress = (array[1].lower() == 'true')
        elif array[0].lower() == 'ignore_extra_keys':
            ignore_extra_keys = (array[1].lower() == 'true')
        elif array[0].lower() == 'backup_running_vms':
            backup_running_vms = (array[1].lower() == 'true')
        else:
            print 'ERROR invalid parm: %s' % sys.argv[arg_ix]
            usage()
            sys.exit(1)

    # init vm-export/vdi-export/exclude in config list
    if not 'vm-export' in config:
        config['vm-export'] = []
    if not 'vdi-export' in config:
        config['vdi-export'] = []
    if not 'exclude' in config:
        config['exclude'] = []

    # init vm-export/vdi-export in wildcards list
    if not 'vm-export' in wildcards:
        wildcards['vm-export'] = []
    if not 'vdi-export' in wildcards:
        wildcards['vdi-export'] = []

    # process config file
    if (os.path.exists(cfg_file)):
        # config file exists
        config_specified = 1
        if config_load(cfg_file):
            expand_wildcards() # calls convert_wildcard_to_config()
            remove_excludes()
            cleanup_vmexport_vdiexport_dups()
        else:
            print 'ERROR in config_load, consider ignore_extra_keys=true'
            sys.exit(1)
    else:
        # no config file exists - so cfg_file is actual vm_name/prefix
        config_specified = 0
        cmd_option = 'vm-export' # default
        cmd_vm_name = cfg_file
        if cmd_vm_name.count('=') == 1:
            (cmd_option,cmd_vm_name) = cmd_vm_name.strip().split('=')
        if cmd_option != 'vm-export' and cmd_option != 'vdi-export':
            print 'ERROR invalid config/vm_name: %s' % cfg_file
            usage()
            sys.exit(1)
        if cmd_vm_name.endswith('*'):
            # wildcard specified
            convert_wildcard_to_config( cmd_option, cmd_vm_name)
        else:
            # single vm specified
            save_to_config( cmd_option, cmd_vm_name)

    config_load_defaults()  # set defaults that are not already loaded
    log('VmBackup config loaded from: %s' % cfg_file)

    if not is_config_valid():
        log('ERROR in configuration settings...')
        sys.exit(1)

    status_log = config['status_log']

    if len(config['vm-export']) == 0 and len(config['vdi-export']) == 0 :
        log('ERROR no VMs loaded')
        sys.exit(1)

    # obscure password support
    if 'password' in config:
        password = config['password']
    else:
        if 'password_from_file' in config:
            infile = open(config['password_from_file'], 'r')
            password = infile.readline().strip()
            infile.close()
        else:
            print 'ERROR in configuration: No password provided'
            sys.exit(1)

    if (os.path.exists(password)):
        password = base64.b64decode(open(password, 'r').read())
    if cfg_file.lower().startswith('create-password-file'):
        array = sys.argv[2].strip().split('=')
        open(array[1], 'w').write(base64.b64encode(password))
        print 'password file saved to: %s' % array[1]
        sys.exit(0)

    # acquire a xapi session by logging in
    try:
        username = 'root'
        session = XenAPI.Session('https://localhost/')
        session.xenapi.login_with_password(username, password)
        hosts = session.xenapi.host.get_all()
    except XenAPI.Failure, e:
        print e
        if e.details[0] == 'HOST_IS_SLAVE':
            session = XenAPI.Session('https://' + e.details[1])
            session.xenapi.login_with_password(username, password)
            hosts = session.xenapi.host.get_all()
        else:
            print 'ERROR - XenAPI authentication error'
            sys.exit(1)

    if backup_running_vms:
        if config['pool_host']:
        # get running VMs for given host in XenPool
            host = session.xenapi.host.get_by_name_label(config['pool_host'])

            if len(host) > 1:
                print 'ERROR - more than one host with name %s' % config['pool_host']
                sys.exit(1)

            if len(host) < 1:
                print 'ERROR - no host with name %s' % config['pool_host']
                sys.exit(1)

            host_record = session.xenapi.host.get_record(host[0])
            vms = host_record['resident_VMs']
        else:
            # get running VMs directly from current XenServer
            vms = session.xenapi.VM.get_all()

        if (len(vms) == 0):
             print 'ERROR - no runnings VMs found'
             sys.exit(1)

        for vm in vms:
             record = session.xenapi.VM.get_record(vm)
             if not(record["is_a_template"]) and not(record["is_control_domain"]) and (record["power_state"] == "Running"):
                 config['vm-export'].append(record['name_label'])

    config_print()     # show fully loaded config

    vdi_export_available = True
    xen_version = session.xenapi.host.get_record(hosts[0])["software_version"]["product_version_text_short"]
    if StrictVersion(xen_version) < StrictVersion("6.5"):
        vdi_export_available = False

    if not verify_config_vms_exist():
        # error message(s) printed in verify_config_vms_exist
        sys.exit(1)
    # OPTIONAL
    #show_vms_not_in_backup()

    if preview:
        log('SUCCESS preview parameters')
        sys.exit(1)

    try:
        main(session)

    except Exception, e:
        print e
        log('***ERROR EXCEPTION - %s' % sys.exc_info()[0])
        log('***ERROR NOTE: see VmBackup output for details')
        raise
    session.logout
