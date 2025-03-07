# THIS FILE IS PART OF THE CYLC WORKFLOW ENGINE.
# Copyright (C) NIWA & British Crown (Met Office) & Contributors.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""Manage task jobs.

This module provides logic to:
* Set up the directory structure on remote job hosts.
  * Copy workflow service files to remote job hosts for communication clients.
  * Clean up of service files on workflow shutdown.
* Prepare task job files.
* Prepare task jobs submission, and manage the callbacks.
* Prepare task jobs poll/kill, and manage the callbacks.
"""

from contextlib import suppress
import json
import os
from copy import deepcopy
from logging import (
    CRITICAL,
    DEBUG,
    INFO,
    WARNING
)
from shutil import rmtree
from time import time

from cylc.flow import LOG
from cylc.flow.job_runner_mgr import JobPollContext
from cylc.flow.exceptions import (
    PlatformLookupError,
    WorkflowConfigError,
    TaskRemoteMgmtError,
    NoPlatformsError,
    NoHostsError
)
from cylc.flow.hostuserutil import (
    get_host,
    is_remote_platform
)
from cylc.flow.job_file import JobFileWriter
from cylc.flow.parsec.util import (
    pdeepcopy,
    poverride
)
from cylc.flow.pathutil import get_remote_workflow_run_job_dir
from cylc.flow.platforms import (
    HOST_REC_COMMAND,
    PLATFORM_REC_COMMAND,
    get_host_from_platform,
    get_install_target_from_platform,
    get_localhost_install_target,
    get_platform,
)
from cylc.flow.remote import construct_ssh_cmd
from cylc.flow.subprocctx import SubProcContext
from cylc.flow.subprocpool import SubProcPool
from cylc.flow.task_action_timer import (
    TaskActionTimer,
    TimerFlags
)
from cylc.flow.task_events_mgr import (
    TaskEventsManager,
    log_task_job_activity
)
from cylc.flow.task_job_logs import (
    JOB_LOG_JOB,
    NN,
    get_task_job_activity_log,
    get_task_job_id,
    get_task_job_job_log,
    get_task_job_log
)
from cylc.flow.task_message import FAIL_MESSAGE_PREFIX
from cylc.flow.task_outputs import (
    TASK_OUTPUT_FAILED,
    TASK_OUTPUT_STARTED,
    TASK_OUTPUT_SUBMITTED,
    TASK_OUTPUT_SUCCEEDED
)
from cylc.flow.task_remote_mgr import (
    REMOTE_FILE_INSTALL_DONE,
    REMOTE_FILE_INSTALL_FAILED,
    REMOTE_FILE_INSTALL_IN_PROGRESS,
    REMOTE_INIT_IN_PROGRESS,
    REMOTE_INIT_255,
    REMOTE_FILE_INSTALL_255,
    REMOTE_INIT_DONE, REMOTE_INIT_FAILED,
    TaskRemoteMgr
)
from cylc.flow.task_state import (
    TASK_STATUS_PREPARING,
    TASK_STATUS_SUBMITTED,
    TASK_STATUS_RUNNING,
    TASK_STATUSES_ACTIVE
)
from cylc.flow.wallclock import (
    get_current_time_string,
    get_utc_mode
)
from cylc.flow.cfgspec.globalcfg import SYSPATH


class TaskJobManager:
    """Manage task job submit, poll and kill.

    This class provides logic to:
    * Submit task jobs.
    * Poll task jobs.
    * Kill task jobs.
    * Set up the directory structure on job hosts.
    * Install workflow communicate client files on job hosts.
    * Remove workflow contact files on job hosts.
    """

    JOBS_KILL = 'jobs-kill'
    JOBS_POLL = 'jobs-poll'
    JOBS_SUBMIT = SubProcPool.JOBS_SUBMIT
    POLL_FAIL = 'poll failed'
    REMOTE_SELECT_MSG = 'waiting for remote host selection'
    REMOTE_INIT_MSG = 'remote host initialising'
    REMOTE_FILE_INSTALL_MSG = 'file installation in progress'
    REMOTE_INIT_255_MSG = 'remote init failed with an unreachable host'
    KEY_EXECUTE_TIME_LIMIT = TaskEventsManager.KEY_EXECUTE_TIME_LIMIT

    IN_PROGRESS = {
        REMOTE_FILE_INSTALL_IN_PROGRESS: REMOTE_FILE_INSTALL_MSG,
        REMOTE_INIT_IN_PROGRESS: REMOTE_INIT_MSG
    }

    def __init__(self, workflow, proc_pool, workflow_db_mgr,
                 task_events_mgr, data_store_mgr, bad_hosts):
        self.workflow = workflow
        self.proc_pool = proc_pool
        self.workflow_db_mgr = workflow_db_mgr
        self.task_events_mgr = task_events_mgr
        self.data_store_mgr = data_store_mgr
        self.job_file_writer = JobFileWriter()
        self.job_runner_mgr = self.job_file_writer.job_runner_mgr
        self.bad_hosts = bad_hosts
        self.bad_hosts_to_clear = set()
        self.task_remote_mgr = TaskRemoteMgr(
            workflow, proc_pool, self.bad_hosts)

    def check_task_jobs(self, workflow, task_pool):
        """Check submission and execution timeout and polling timers.

        Poll tasks that have timed out and/or have reached next polling time.
        """
        now = time()
        poll_tasks = set()
        for itask in task_pool.get_tasks():
            if self.task_events_mgr.check_job_time(itask, now):
                poll_tasks.add(itask)
                if itask.poll_timer.delay is not None:
                    LOG.info(
                        '[%s] -poll now, (next in %s)',
                        itask, itask.poll_timer.delay_timeout_as_str())
        if poll_tasks:
            self.poll_task_jobs(workflow, poll_tasks)

    def kill_task_jobs(self, workflow, itasks):
        """Kill jobs of active tasks, and hold the tasks.

        If items is specified, kill active tasks matching given IDs.

        """
        to_kill_tasks = []
        for itask in itasks:
            if itask.state(*TASK_STATUSES_ACTIVE):
                itask.state.reset(is_held=True)
                self.data_store_mgr.delta_task_held(itask)
                to_kill_tasks.append(itask)
            else:
                LOG.warning('skipping %s: task not killable' % itask.identity)
        self._run_job_cmd(
            self.JOBS_KILL, workflow, to_kill_tasks,
            self._kill_task_jobs_callback,
            self._kill_task_jobs_callback_255
        )

    def poll_task_jobs(self, workflow, itasks, msg=None):
        """Poll jobs of specified tasks.

        This method uses _poll_task_jobs_callback() and
        _manip_task_jobs_callback() as help/callback methods.

        _poll_task_job_callback() executes one specific job.
        """
        if itasks:
            if msg is not None:
                LOG.info(msg)
            self._run_job_cmd(
                self.JOBS_POLL, workflow, itasks,
                self._poll_task_jobs_callback,
                self._poll_task_jobs_callback_255
            )

    def prep_submit_task_jobs(self, workflow, itasks, check_syntax=True):
        """Prepare task jobs for submit.

        Prepare tasks where possible. Ignore tasks that are waiting for host
        select command to complete. Bad host select command or error writing to
        a job file will cause a bad task - leading to submission failure.

        Return [list, list]: list of good tasks, list of bad tasks
        """
        prepared_tasks = []
        bad_tasks = []
        for itask in itasks:
            if itask.state.reset(TASK_STATUS_PREPARING):
                self.data_store_mgr.delta_task_state(itask)
                self.workflow_db_mgr.put_update_task_state(itask)
            prep_task = self._prep_submit_task_job(
                workflow, itask, check_syntax=check_syntax)
            if prep_task:
                prepared_tasks.append(itask)
            elif prep_task is False:
                bad_tasks.append(itask)
        return [prepared_tasks, bad_tasks]

    def submit_task_jobs(self, workflow, itasks, curve_auth,
                         client_pub_key_dir, is_simulation=False):
        """Prepare for job submission and submit task jobs.

        Preparation (host selection, remote host init, and remote install)
        is done asynchronously. Newly released tasks may be sent here several
        times until these init subprocesses have returned. Failure during
        preparation is considered to be job submission failure.

        Once preparation has completed or failed, reset .waiting_on_job_prep in
        task instances so the scheduler knows to stop sending them back here.

        This method uses prep_submit_task_job() as helper.

        Return (list): list of tasks that attempted submission.
        """
        if is_simulation:
            return self._simulation_submit_task_jobs(itasks)
        # Prepare tasks for job submission
        prepared_tasks, bad_tasks = self.prep_submit_task_jobs(
            workflow, itasks)
        # Reset consumed host selection results
        self.task_remote_mgr.subshell_eval_reset()

        if not prepared_tasks:
            return bad_tasks
        auth_itasks = {}  # {platform: [itask, ...], ...}

        for itask in prepared_tasks:
            platform_name = itask.platform['name']
            auth_itasks.setdefault(platform_name, [])
            auth_itasks[platform_name].append(itask)
        # Submit task jobs for each platform
        done_tasks = bad_tasks

        for _, itasks in sorted(auth_itasks.items()):
            # Find the first platform where >1 host has not been tried and
            # found to be unreachable.
            # If there are no good hosts for a task then the task submit-fails.
            for itask in itasks:
                # If there are any hosts left for this platform which we
                # have not previously failed to contact with a 255 error.
                if (
                    set(itask.platform['hosts']) -
                    self.task_remote_mgr.bad_hosts
                ):
                    platform = itask.platform
                    out_of_hosts = False
                    break
                else:
                    # If there are no hosts left for this platform.
                    # See if you can get another platform from the group or
                    # else set task to submit failed.
                    LOG.warning(TaskRemoteMgmtError(
                        (
                            'Tried all the hosts on platform.'
                        ), itask.platform['name'], [], 1, '', '',
                    ))
                    # Get another platform, if task config platform is a group
                    use_next_platform_in_group = False
                    try:
                        platform = get_platform(
                            itask.tdef.rtconfig['platform'],
                            bad_hosts=self.bad_hosts
                        )
                        # If were able to select a new platform;
                        if platform and platform != itask.platform:
                            use_next_platform_in_group = True
                    except NoPlatformsError:
                        use_next_platform_in_group = False

                    if use_next_platform_in_group:
                        # store the previous platform's hosts so that when
                        # we record a submit fail we can clear all hosts
                        # from all platforms from bad_hosts.
                        for host_ in itask.platform['hosts']:
                            self.bad_hosts_to_clear.add(host_)
                        itask.platform = platform
                        out_of_hosts = False
                        break
                    else:
                        itask.waiting_on_job_prep = False
                        itask.local_job_file_path = None
                        self._prep_submit_task_job_error(
                            workflow, itask, '(remote init)', ''
                        )
                        # Now that all hosts on all platforms in platform
                        # group selected in task config are exhausted we clear
                        # bad_hosts or all the hosts we have
                        # tried for this platform or group.
                        self.bad_hosts = (
                            self.bad_hosts - set(itask.platform['hosts']))
                        self.bad_hosts = (
                            self.bad_hosts - self.bad_hosts_to_clear)
                        self.bad_hosts_to_clear.clear()
                        LOG.critical(TaskRemoteMgmtError(
                            (
                                'Initialisation on platform did not complete:'
                                'no hosts were reachable.'
                            ), itask.tdef.rtconfig['platform'], [], 1, '', '',
                        ))
                        out_of_hosts = True
                        done_tasks.append(itask)

            if out_of_hosts is True:
                continue
            install_target = get_install_target_from_platform(platform)
            ri_map = self.task_remote_mgr.remote_init_map

            if (ri_map.get(install_target) != REMOTE_FILE_INSTALL_DONE):
                if install_target == get_localhost_install_target():
                    # Skip init and file install for localhost.
                    LOG.debug(f"REMOTE INIT NOT REQUIRED for {install_target}")
                    ri_map[install_target] = (REMOTE_FILE_INSTALL_DONE)

                elif install_target not in ri_map:
                    # Remote init not in progress for target, so start it.
                    self.task_remote_mgr.remote_init(
                        platform, curve_auth, client_pub_key_dir)
                    for itask in itasks:
                        self.data_store_mgr.delta_job_msg(
                            get_task_job_id(
                                itask.point,
                                itask.tdef.name,
                                itask.submit_num),
                            self.REMOTE_INIT_MSG)
                    continue

                elif (ri_map[install_target] == REMOTE_INIT_DONE):
                    # Already done remote init so move on to file install
                    self.task_remote_mgr.file_install(platform)
                    continue

                elif (ri_map[install_target] in self.IN_PROGRESS):
                    # Remote init or file install in progress.
                    for itask in itasks:
                        msg = self.IN_PROGRESS[ri_map[install_target]]
                        self.data_store_mgr.delta_job_msg(
                            get_task_job_id(
                                itask.point,
                                itask.tdef.name,
                                itask.submit_num),
                            msg)
                    continue
                elif (ri_map[install_target] == REMOTE_INIT_255):
                    # Remote init previously failed becase a host was
                    # unreachable, so start it again.
                    del ri_map[install_target]
                    self.task_remote_mgr.remote_init(
                        platform, curve_auth, client_pub_key_dir)
                    for itask in itasks:
                        self.data_store_mgr.delta_job_msg(
                            get_task_job_id(
                                itask.point,
                                itask.tdef.name,
                                itask.submit_num),
                            self.REMOTE_INIT_MSG)
                    continue

            # Ensure that localhost background/at jobs are recorded as running
            # on the host name of the current workflow host, rather than just
            # "localhost". On restart on a different workflow host, this
            # allows the restart logic to correctly poll the status of the
            # background/at jobs that may still be running on the previous
            # workflow host.
            try:
                host = get_host_from_platform(
                    platform,
                    bad_hosts=self.task_remote_mgr.bad_hosts
                )
            except NoHostsError:
                del ri_map[install_target]
                self.task_remote_mgr.remote_init(
                    platform, curve_auth, client_pub_key_dir)
                for itask in itasks:
                    self.data_store_mgr.delta_job_msg(
                        get_task_job_id(
                            itask.point,
                            itask.tdef.name,
                            itask.submit_num),
                        self.REMOTE_INIT_MSG)
                continue

            if (
                self.job_runner_mgr.is_job_local_to_host(
                    itask.summary['job_runner_name']
                ) and
                not is_remote_platform(platform)
            ):
                host = get_host()

            now_str = get_current_time_string()
            done_tasks.extend(itasks)
            for itask in itasks:
                # Log and persist
                LOG.info(
                    '[%s] -submit-num=%02d, host=%s',
                    itask, itask.submit_num, host)
                self.workflow_db_mgr.put_insert_task_jobs(itask, {
                    'is_manual_submit': itask.is_manual_submit,
                    'try_num': itask.get_try_num(),
                    'time_submit': now_str,
                    'platform_name': itask.platform['name'],
                    'job_runner_name': itask.summary['job_runner_name'],
                })
                itask.is_manual_submit = False

            if ri_map[install_target] == REMOTE_FILE_INSTALL_255:
                del ri_map[install_target]
                self.task_remote_mgr.file_install(
                    platform)
                for itask in itasks:
                    self.data_store_mgr.delta_job_msg(
                        get_task_job_id(
                            itask.point,
                            itask.tdef.name,
                            itask.submit_num),
                        REMOTE_FILE_INSTALL_IN_PROGRESS)
                continue

            if (ri_map[install_target] in [REMOTE_INIT_FAILED,
                                           REMOTE_FILE_INSTALL_FAILED]):
                # Remote init or install failed. Set submit-failed for all
                # affected tasks and remove target from remote init map
                # - this enables new tasks to re-initialise that target
                init_error = (ri_map[install_target])
                del ri_map[install_target]
                for itask in itasks:
                    itask.waiting_on_job_prep = False
                    itask.local_job_file_path = None  # reset for retry
                    log_task_job_activity(
                        SubProcContext(
                            self.JOBS_SUBMIT,
                            '(init %s)' % host,
                            err=init_error,
                            ret_code=1),
                        workflow, itask.point, itask.tdef.name)
                    self._prep_submit_task_job_error(
                        workflow, itask, '(remote init)', ''
                    )
                continue

            # Build the "cylc jobs-submit" command
            cmd = [self.JOBS_SUBMIT]
            if LOG.isEnabledFor(DEBUG):
                cmd.append('--debug')
            if get_utc_mode():
                cmd.append('--utc-mode')
            if is_remote_platform(itask.platform):
                remote_mode = True
                cmd.append('--remote-mode')
            else:
                remote_mode = False
            if itask.platform[
                    'clean job submission environment']:
                cmd.append('--clean-env')
            for var in itask.platform[
                    'job submission environment pass-through']:
                cmd.append(f"--env={var}")
            for path in itask.platform[
                    'job submission executable paths'] + SYSPATH:
                cmd.append(f"--path={path}")
            cmd.append('--')
            cmd.append(get_remote_workflow_run_job_dir(workflow))
            # Chop itasks into a series of shorter lists if it's very big
            # to prevent overloading of stdout and stderr pipes.
            itasks = sorted(itasks, key=lambda itask: itask.identity)
            chunk_size = (
                len(itasks) // (
                    (len(itasks) // platform['max batch submit size']) + 1
                ) + 1
            )
            itasks_batches = [
                itasks[i:i + chunk_size]
                for i in range(0, len(itasks), chunk_size)
            ]
            LOG.debug(
                '%s ... # will invoke in batches, sizes=%s',
                cmd, [len(b) for b in itasks_batches])

            if remote_mode:
                host = get_host_from_platform(
                    platform, bad_hosts=self.task_remote_mgr.bad_hosts
                )
                cmd = construct_ssh_cmd(
                    cmd, platform, host
                )
            else:
                cmd = ['cylc'] + cmd

            for itasks_batch in itasks_batches:
                stdin_files = []
                job_log_dirs = []
                for itask in itasks_batch:
                    if remote_mode:
                        stdin_files.append(
                            os.path.expandvars(
                                get_task_job_job_log(
                                    workflow, itask.point, itask.tdef.name,
                                    itask.submit_num
                                )
                            )
                        )
                    job_log_dirs.append(get_task_job_id(
                        itask.point, itask.tdef.name, itask.submit_num))
                    # The job file is now (about to be) used: reset the file
                    # write flag so that subsequent manual retrigger will
                    # generate a new job file.
                    itask.local_job_file_path = None
                    if itask.state.outputs.has_custom_triggers():
                        self.workflow_db_mgr.put_update_task_outputs(itask)

                    itask.waiting_on_job_prep = False
                self.proc_pool.put_command(
                    SubProcContext(
                        self.JOBS_SUBMIT,
                        cmd + job_log_dirs,
                        stdin_files=stdin_files,
                        job_log_dirs=job_log_dirs,
                        host=host
                    ),
                    bad_hosts=self.task_remote_mgr.bad_hosts,
                    callback=self._submit_task_jobs_callback,
                    callback_args=[workflow, itasks_batch],
                    callback_255=self._submit_task_jobs_callback_255,
                )
        return done_tasks

    @staticmethod
    def _create_job_log_path(workflow, itask):
        """Create job log directory for a task job, etc.

        Create local job directory, and NN symbolic link.
        If NN => 01, remove numbered directories with submit numbers greater
        than 01.
        Return a string in the form "POINT/NAME/SUBMIT_NUM".

        """
        job_file_dir = get_task_job_log(
            workflow, itask.point, itask.tdef.name, itask.submit_num)
        job_file_dir = os.path.expandvars(job_file_dir)
        task_log_dir = os.path.dirname(job_file_dir)
        if itask.submit_num == 1:
            try:
                names = os.listdir(task_log_dir)
            except OSError:
                pass
            else:
                for name in names:
                    if name not in ["01", NN]:
                        rmtree(
                            os.path.join(task_log_dir, name),
                            ignore_errors=True)
        else:
            rmtree(job_file_dir, ignore_errors=True)

        os.makedirs(job_file_dir, exist_ok=True)
        target = os.path.join(task_log_dir, NN)
        source = os.path.basename(job_file_dir)
        try:
            prev_source = os.readlink(target)
        except OSError:
            prev_source = None
        if prev_source == source:
            return
        try:
            if prev_source:
                os.unlink(target)
            os.symlink(source, target)
        except OSError as exc:
            if not exc.filename:
                exc.filename = target
            raise exc

    @staticmethod
    def _get_job_scripts(itask, rtconfig):
        """Return pre-script, script, post-script for a job."""
        script = rtconfig['script']
        pre_script = rtconfig['pre-script']
        post_script = rtconfig['post-script']
        if itask.tdef.workflow_polling_cfg:
            # Automatic workflow state polling script
            comstr = (
                "cylc workflow-state "
                + " --task=" + itask.tdef.workflow_polling_cfg['task']
                + " --point=" + str(itask.point)
            )
            if LOG.isEnabledFor(DEBUG):
                comstr += ' --debug'
            for key, fmt in [
                    ('user', ' --%s=%s'),
                    ('host', ' --%s=%s'),
                    ('interval', ' --%s=%d'),
                    ('max-polls', ' --%s=%s'),
                    ('run-dir', ' --%s=%s')]:
                if rtconfig['workflow state polling'][key]:
                    comstr += fmt % (key,
                                     rtconfig['workflow state polling'][key])
            if rtconfig['workflow state polling']['message']:
                comstr += " --message='%s'" % (
                    rtconfig['workflow state polling']['message'])
            else:
                comstr += (" --status=" +
                           itask.tdef.workflow_polling_cfg['status'])
            comstr += " " + itask.tdef.workflow_polling_cfg['workflow']
            script = "echo " + comstr + "\n" + comstr
        return pre_script, script, post_script

    @staticmethod
    def _job_cmd_out_callback(workflow, itask, cmd_ctx, line):
        """Callback on job command STDOUT/STDERR."""
        if cmd_ctx.cmd_kwargs.get("host"):
            host = "(%(host)s) " % cmd_ctx.cmd_kwargs
        else:
            host = ""
        try:
            timestamp, _, content = line.split("|")
        except ValueError:
            pass
        else:
            line = "%s %s" % (timestamp, content)
        job_activity_log = get_task_job_activity_log(
            workflow, itask.point, itask.tdef.name)
        try:
            with open(os.path.expandvars(job_activity_log), "ab") as handle:
                if not line.endswith("\n"):
                    line += "\n"
                handle.write((host + line).encode())
        except IOError as exc:
            LOG.warning("%s: write failed\n%s" % (job_activity_log, exc))
            LOG.warning("[%s] -%s%s", itask, host, line)

    def _kill_task_jobs_callback(self, ctx, workflow, itasks):
        """Callback when kill tasks command exits."""
        self._manip_task_jobs_callback(
            ctx,
            workflow,
            itasks,
            self._kill_task_job_callback,
            {self.job_runner_mgr.OUT_PREFIX_COMMAND:
                self._job_cmd_out_callback}
        )

    def _kill_task_jobs_callback_255(self, ctx, workflow, itasks):
        """Callback when kill tasks command exits."""
        self._manip_task_jobs_callback(
            ctx,
            workflow,
            itasks,
            self._kill_task_job_callback_255,
            {self.job_runner_mgr.OUT_PREFIX_COMMAND:
                self._job_cmd_out_callback}
        )

    def _kill_task_job_callback_255(self, workflow, itask, cmd_ctx, line):
        """Helper for _kill_task_jobs_callback, on one task job."""
        self.kill_task_jobs(workflow, [itask])

    def _kill_task_job_callback(self, workflow, itask, cmd_ctx, line):
        """Helper for _kill_task_jobs_callback, on one task job."""
        ctx = SubProcContext(self.JOBS_KILL, None)
        ctx.out = line
        try:
            ctx.timestamp, _, ctx.ret_code = line.split("|", 2)
        except ValueError:
            ctx.ret_code = 1
            ctx.cmd = cmd_ctx.cmd  # print original command on failure
        else:
            ctx.ret_code = int(ctx.ret_code)
            if ctx.ret_code:
                ctx.cmd = cmd_ctx.cmd  # print original command on failure
        log_task_job_activity(ctx, workflow, itask.point, itask.tdef.name)
        log_lvl = INFO
        log_msg = 'killed'
        if ctx.ret_code:  # non-zero exit status
            log_lvl = WARNING
            log_msg = 'kill failed'
            itask.state.kill_failed = True
        elif itask.state(TASK_STATUS_SUBMITTED):
            self.task_events_mgr.process_message(
                itask, CRITICAL, self.task_events_mgr.EVENT_SUBMIT_FAILED,
                ctx.timestamp)
        elif itask.state(TASK_STATUS_RUNNING):
            self.task_events_mgr.process_message(
                itask, CRITICAL, TASK_OUTPUT_FAILED)
        else:
            log_lvl = DEBUG
            log_msg = (
                'ignoring job kill result, unexpected task state: %s' %
                itask.state.status)
        self.data_store_mgr.delta_job_msg(
            get_task_job_id(itask.point, itask.tdef.name, itask.submit_num),
            log_msg)
        LOG.log(log_lvl, "[%s] -job(%02d) %s" % (
            itask.identity, itask.submit_num, log_msg))

    def _manip_task_jobs_callback(
            self, ctx, workflow, itasks, summary_callback,
            more_callbacks=None):
        """Callback when submit/poll/kill tasks command exits."""
        # Swallow SSH 255 (can't contact host) errors unless debugging.
        if (
            (ctx.ret_code and LOG.isEnabledFor(DEBUG))
            or (ctx.ret_code and ctx.ret_code != 255)
        ):
            LOG.error(ctx)
        else:
            LOG.debug(ctx)
        # A dict for easy reference of (CYCLE, NAME, SUBMIT_NUM) -> TaskProxy
        #
        # Note for "reload": A TaskProxy instance may be replaced on reload, so
        # the "itasks" list may not reference the TaskProxy objects that
        # replace the old ones. The .reload_successor attribute provides the
        # link(s) for us to get to the latest replacement.
        #
        # Note for "kill": It is possible for a job to trigger its trap and
        # report back to the workflow before (or after?) this logic is called.
        # If so, it will no longer be status SUBMITTED or RUNNING, and
        # its output line will be ignored here.
        tasks = {}
        for itask in itasks:
            while itask.reload_successor is not None:
                itask = itask.reload_successor
            if itask.point is not None and itask.submit_num:
                submit_num = "%02d" % (itask.submit_num)
                tasks[(str(itask.point), itask.tdef.name, submit_num)] = itask
        handlers = [(self.job_runner_mgr.OUT_PREFIX_SUMMARY, summary_callback)]
        if more_callbacks:
            for prefix, callback in more_callbacks.items():
                handlers.append((prefix, callback))
        out = ctx.out
        if not out:
            out = ""
        bad_tasks = dict(tasks)
        for line in out.splitlines(True):
            for prefix, callback in handlers:
                if line.startswith(prefix):
                    line = line[len(prefix):].strip()
                    try:
                        path = line.split("|", 2)[1]  # timestamp, path, status
                        point, name, submit_num = path.split(os.sep, 2)
                        if prefix == self.job_runner_mgr.OUT_PREFIX_SUMMARY:
                            del bad_tasks[(point, name, submit_num)]
                        itask = tasks[(point, name, submit_num)]
                        callback(workflow, itask, ctx, line)
                    except (LookupError, ValueError) as exc:
                        LOG.warning(
                            'Unhandled %s output: %s', ctx.cmd_key, line)
                        LOG.exception(exc)
        # Task jobs that are in the original command but did not get a status
        # in the output. Handle as failures.
        for key, itask in sorted(bad_tasks.items()):
            line = (
                "|".join([ctx.timestamp, os.sep.join(key), "1"]) + "\n")
            summary_callback(workflow, itask, ctx, line)

    def _poll_task_jobs_callback(self, ctx, workflow, itasks):
        """Callback when poll tasks command exits."""
        self._manip_task_jobs_callback(
            ctx,
            workflow,
            itasks,
            self._poll_task_job_callback,
            {self.job_runner_mgr.OUT_PREFIX_MESSAGE:
             self._poll_task_job_message_callback})

    def _poll_task_jobs_callback_255(self, ctx, workflow, itasks):
        """Callback when poll tasks command exits."""
        self._manip_task_jobs_callback(
            ctx,
            workflow,
            itasks,
            self._poll_task_job_callback_255,
            {self.job_runner_mgr.OUT_PREFIX_MESSAGE:
             self._poll_task_job_message_callback})

    def _poll_task_job_callback_255(self, workflow, itask, cmd_ctx, line):
        self.poll_task_jobs(workflow, [itask])

    def _poll_task_job_callback(self, workflow, itask, cmd_ctx, line):
        """Helper for _poll_task_jobs_callback, on one task job."""
        ctx = SubProcContext(self.JOBS_POLL, None)
        ctx.out = line
        ctx.ret_code = 0
        # See cylc.flow.job_runner_mgr.JobPollContext
        job_d = get_task_job_id(itask.point, itask.tdef.name, itask.submit_num)
        try:
            job_log_dir, context = line.split('|')[1:3]
            items = json.loads(context)
            jp_ctx = JobPollContext(job_log_dir, **items)
        except TypeError:
            self.data_store_mgr.delta_job_msg(job_d, self.POLL_FAIL)
            ctx.cmd = cmd_ctx.cmd  # print original command on failure
            return
        except ValueError:
            self.data_store_mgr.delta_job_msg(job_d, self.POLL_FAIL)
            ctx.cmd = cmd_ctx.cmd  # print original command on failure
            return
        finally:
            log_task_job_activity(ctx, workflow, itask.point, itask.tdef.name)

        flag = self.task_events_mgr.FLAG_POLLED
        if jp_ctx.run_status == 1 and jp_ctx.run_signal in ["ERR", "EXIT"]:
            # Failed normally
            self.task_events_mgr.process_message(
                itask, INFO, TASK_OUTPUT_FAILED, jp_ctx.time_run_exit, flag)
        elif jp_ctx.run_status == 1 and jp_ctx.job_runner_exit_polled == 1:
            # Failed by a signal, and no longer in job runner
            self.task_events_mgr.process_message(
                itask, INFO, TASK_OUTPUT_FAILED, jp_ctx.time_run_exit, flag)
            self.task_events_mgr.process_message(
                itask, INFO, FAIL_MESSAGE_PREFIX + jp_ctx.run_signal,
                jp_ctx.time_run_exit,
                flag)
        elif jp_ctx.run_status == 1:  # noqa: SIM114
            # The job has terminated, but is still managed by job runner.
            # Some job runners may restart a job in this state, so don't
            # mark as failed yet.
            self.task_events_mgr.process_message(
                itask, INFO, TASK_OUTPUT_STARTED, jp_ctx.time_run, flag)
        elif jp_ctx.run_status == 0:
            # The job succeeded
            self.task_events_mgr.process_message(
                itask, INFO, TASK_OUTPUT_SUCCEEDED, jp_ctx.time_run_exit,
                flag)
        elif jp_ctx.time_run and jp_ctx.job_runner_exit_polled == 1:
            # The job has terminated without executing the error trap
            self.task_events_mgr.process_message(
                itask, INFO, TASK_OUTPUT_FAILED, get_current_time_string(),
                flag)
        elif jp_ctx.time_run:
            # The job has started, and is still managed by job runner
            self.task_events_mgr.process_message(
                itask, INFO, TASK_OUTPUT_STARTED, jp_ctx.time_run, flag)
        elif jp_ctx.job_runner_exit_polled == 1:
            # The job never ran, and no longer in job runner
            self.task_events_mgr.process_message(
                itask, INFO, self.task_events_mgr.EVENT_SUBMIT_FAILED,
                jp_ctx.time_submit_exit, flag)
        else:
            # The job never ran, and is in job runner
            self.task_events_mgr.process_message(
                itask, INFO, TASK_STATUS_SUBMITTED, jp_ctx.time_submit_exit,
                flag)

    def _poll_task_job_message_callback(self, workflow, itask, cmd_ctx, line):
        """Helper for _poll_task_jobs_callback, on message of one task job."""
        ctx = SubProcContext(self.JOBS_POLL, None)
        ctx.out = line
        try:
            event_time, severity, message = line.split("|")[2:5]
        except ValueError:
            ctx.ret_code = 1
            ctx.cmd = cmd_ctx.cmd  # print original command on failure
        else:
            ctx.ret_code = 0
            self.task_events_mgr.process_message(
                itask, severity, message, event_time,
                self.task_events_mgr.FLAG_POLLED)
        log_task_job_activity(ctx, workflow, itask.point, itask.tdef.name)

    def _run_job_cmd(
        self, cmd_key, workflow, itasks, callback, callback_255=None
    ):
        """Run job commands, e.g. poll, kill, etc.

        Group itasks with their platform_name and host.
        Put a job command for each group to the multiprocess pool.

        """
        if not itasks:
            return
        # sort itasks into lists based upon where they were run.
        auth_itasks = {}
        for itask in itasks:
            platform_n = itask.platform['name']
            if platform_n not in auth_itasks:
                auth_itasks[platform_n] = []
            auth_itasks[platform_n].append(itask)

        # Go through each list of itasks and carry out commands as required.
        for platform_n, itasks in sorted(auth_itasks.items()):
            platform = get_platform(platform_n)
            if is_remote_platform(platform):
                remote_mode = True
                cmd = [cmd_key]
            else:
                cmd = ["cylc", cmd_key]
                remote_mode = False
            if LOG.isEnabledFor(DEBUG):
                cmd.append("--debug")
            cmd.append("--")
            cmd.append(get_remote_workflow_run_job_dir(workflow))
            job_log_dirs = []
            host = 'localhost'
            if remote_mode:
                host = get_host_from_platform(
                    platform, bad_hosts=self.task_remote_mgr.bad_hosts
                )
                cmd = construct_ssh_cmd(
                    cmd, platform, host
                )
            for itask in sorted(itasks, key=lambda itask: itask.identity):
                job_log_dirs.append(get_task_job_id(
                    itask.point, itask.tdef.name, itask.submit_num))
            cmd += job_log_dirs
            self.proc_pool.put_command(
                SubProcContext(
                    cmd_key, cmd, host=host
                ),
                bad_hosts=self.task_remote_mgr.bad_hosts,
                callback=callback,
                callback_args=[workflow, itasks],
                callback_255=callback_255,
            )

    @staticmethod
    def _set_retry_timers(itask, rtconfig=None, retry=True):
        """Set try number and retry delays."""
        if rtconfig is None:
            rtconfig = itask.tdef.rtconfig
        if (
            itask.tdef.run_mode + ' mode' in rtconfig and
            'disable retries' in rtconfig[itask.tdef.run_mode + ' mode']
        ):
            retry = False

        if retry:
            if rtconfig['submission retry delays']:
                submit_delays = rtconfig['submission retry delays']
            else:
                submit_delays = itask.platform['submission retry delays']
            # TODO: same for execution delays?

            for key, delays in [
                    (
                        TimerFlags.SUBMISSION_RETRY,
                        submit_delays
                    ),
                    (
                        TimerFlags.EXECUTION_RETRY,
                        rtconfig['execution retry delays']
                    )
            ]:
                if delays is None:
                    delays = []
                try:
                    itask.try_timers[key].set_delays(delays)
                except KeyError:
                    itask.try_timers[key] = TaskActionTimer(delays=delays)

    def _simulation_submit_task_jobs(self, itasks):
        """Simulation mode task jobs submission."""
        for itask in itasks:
            itask.waiting_on_job_prep = False
            self._set_retry_timers(itask)
            itask.platform = 'SIMULATION'
            itask.summary['job_runner_name'] = 'SIMULATION'
            itask.summary[self.KEY_EXECUTE_TIME_LIMIT] = (
                itask.tdef.rtconfig['job']['simulated run length'])
            self.task_events_mgr.process_message(
                itask, INFO, TASK_OUTPUT_SUBMITTED)
        return itasks

    def _submit_task_jobs_callback(self, ctx, workflow, itasks):
        """Callback when submit task jobs command exits."""
        self._manip_task_jobs_callback(
            ctx,
            workflow,
            itasks,
            self._submit_task_job_callback,
            {self.job_runner_mgr.OUT_PREFIX_COMMAND:
                self._job_cmd_out_callback}
        )

    def _submit_task_jobs_callback_255(self, ctx, workflow, itasks):
        """Callback when submit task jobs command exits."""
        self._manip_task_jobs_callback(
            ctx,
            workflow,
            itasks,
            self._submit_task_job_callback_255,
            {self.job_runner_mgr.OUT_PREFIX_COMMAND:
                self._job_cmd_out_callback}
        )

    def _submit_task_job_callback_255(
        self, workflow, itask, cmd_ctx, line
    ):
        """Helper for _submit_task_jobs_callback, on one task job."""
        itask.submit_num -= 1
        self.task_events_mgr._retry_task(
            itask, time(), submit_retry=True
        )
        return

    def _submit_task_job_callback(self, workflow, itask, cmd_ctx, line):
        """Helper for _submit_task_jobs_callback, on one task job."""
        ctx = SubProcContext(self.JOBS_SUBMIT, None, cmd_ctx.host)
        ctx.out = line
        items = line.split("|")
        try:
            ctx.timestamp, _, ctx.ret_code = items[0:3]
        except ValueError:
            ctx.ret_code = 1
            ctx.cmd = cmd_ctx.cmd  # print original command on failure
        else:
            ctx.ret_code = int(ctx.ret_code)
            if ctx.ret_code:
                ctx.cmd = cmd_ctx.cmd  # print original command on failure
        if cmd_ctx.ret_code != 255:
            log_task_job_activity(ctx, workflow, itask.point, itask.tdef.name)
        if ctx.ret_code == SubProcPool.RET_CODE_WORKFLOW_STOPPING:
            return

        job_d = get_task_job_id(itask.point, itask.tdef.name, itask.submit_num)
        try:
            itask.summary['submit_method_id'] = items[3]
            self.data_store_mgr.delta_job_attr(job_d, 'job_id', items[3])
        except IndexError:
            itask.summary['submit_method_id'] = None
        if itask.summary['submit_method_id'] == "None":
            itask.summary['submit_method_id'] = None
        if itask.summary['submit_method_id'] and ctx.ret_code == 0:
            self.task_events_mgr.process_message(
                itask, INFO, TASK_OUTPUT_SUBMITTED, ctx.timestamp)
        else:
            self.task_events_mgr.process_message(
                itask, CRITICAL, self.task_events_mgr.EVENT_SUBMIT_FAILED,
                ctx.timestamp)

    def _prep_submit_task_job(self, workflow, itask, check_syntax=True):
        """Prepare a task job submission.

        Return itask on a good preparation.

        """
        if itask.local_job_file_path:
            return itask

        # Handle broadcasts
        overrides = self.task_events_mgr.broadcast_mgr.get_broadcast(
            itask.identity)
        if overrides:
            rtconfig = pdeepcopy(itask.tdef.rtconfig)
            poverride(rtconfig, overrides, prepend=True)
        else:
            rtconfig = itask.tdef.rtconfig

        # BACK COMPAT: host logic
        # Determine task host or platform now, just before job submission,
        # because dynamic host/platform selection may be used.
        # cases:
        # - Platform exists, host does = throw error here:
        #    Although errors of this sort should ideally be caught on config
        #    load this cannot be done because inheritance may create conflicts
        #    which appear later. Although this error is also raised
        #    by the platforms module it's probably worth putting it here too
        #    to prevent trying to run the remote_host/platform_select logic for
        #    tasks which will fail anyway later.
        # - Platform exists, host doesn't = eval platform_n
        # - host exists - eval host_n
        # remove at:
        #     Cylc9
        if (
            rtconfig['platform'] is not None and
            rtconfig['remote']['host'] is not None
        ):
            raise WorkflowConfigError(
                "A mixture of Cylc 7 (host) and Cylc 8 (platform) "
                "logic should not be used. In this case for the task "
                f"\"{itask.identity}\" the following are not compatible:\n"
            )

        host_n, platform_n = None, None
        try:
            if rtconfig['remote']['host'] is not None:
                host_n = self.task_remote_mgr.subshell_eval(
                    rtconfig['remote']['host'], HOST_REC_COMMAND
                )
            else:
                platform_n = self.task_remote_mgr.subshell_eval(
                    rtconfig['platform'], PLATFORM_REC_COMMAND
                )
        except TaskRemoteMgmtError as exc:
            # Submit number not yet incremented
            itask.waiting_on_job_prep = False
            itask.submit_num += 1
            itask.summary['platforms_used'][itask.submit_num] = ''
            # Retry delays, needed for the try_num
            self._create_job_log_path(workflow, itask)
            self._set_retry_timers(itask, rtconfig)
            self._prep_submit_task_job_error(
                workflow, itask, '(remote host select)', exc)
            return False
        else:
            # host/platform select not ready
            if host_n is None and platform_n is None:
                return
            elif (
                host_n is None
                and rtconfig['platform']
                and rtconfig['platform'] != platform_n
            ):
                LOG.debug(
                    f"for task {itask.identity}: platform = "
                    f"{rtconfig['platform']} evaluated as {platform_n}"
                )
                rtconfig['platform'] = platform_n
            elif platform_n is None and rtconfig['remote']['host'] != host_n:
                LOG.debug(
                    f"for task {itask.identity}: host = "
                    f"{rtconfig['remote']['host']} evaluated as {host_n}"
                )
                rtconfig['remote']['host'] = host_n

            try:
                platform = get_platform(rtconfig, self.bad_hosts)

            except PlatformLookupError as exc:
                # Submit number not yet incremented
                itask.waiting_on_job_prep = False
                itask.submit_num += 1
                itask.summary['platforms_used'][itask.submit_num] = ''
                # Retry delays, needed for the try_num
                self._create_job_log_path(workflow, itask)
                self._set_retry_timers(itask, rtconfig, False)
                self._prep_submit_task_job_error(
                    workflow, itask, '(platform not defined)', exc
                )
                return False
            else:
                itask.platform = platform
                # Submit number not yet incremented
                itask.submit_num += 1
                # Retry delays, needed for the try_num
                self._set_retry_timers(itask, rtconfig)

        try:
            job_conf = self._prep_submit_task_job_impl(
                workflow, itask, rtconfig)

            # Job pool insertion
            job_config = deepcopy(job_conf)
            job_config['logfiles'] = deepcopy(itask.summary['logfiles'])
            itask.jobs.append(job_config['job_d'])
            self.data_store_mgr.insert_job(
                itask.tdef.name, itask.point, job_config)

            local_job_file_path = get_task_job_job_log(
                workflow, itask.point, itask.tdef.name, itask.submit_num)
            self.job_file_writer.write(local_job_file_path, job_conf,
                                       check_syntax=check_syntax)
        except Exception as exc:
            # Could be a bad command template, IOError, etc
            itask.waiting_on_job_prep = False
            self._prep_submit_task_job_error(
                workflow, itask, '(prepare job file)', exc)
            return False

        itask.local_job_file_path = local_job_file_path
        return itask

    def _prep_submit_task_job_error(self, workflow, itask, action, exc):
        """Helper for self._prep_submit_task_job. On error."""
        LOG.debug("submit_num %s" % itask.submit_num)
        log_task_job_activity(
            SubProcContext(self.JOBS_SUBMIT, action, err=exc, ret_code=1),
            workflow,
            itask.point,
            itask.tdef.name,
            submit_num=itask.submit_num
        )
        # Persist
        self.workflow_db_mgr.put_insert_task_jobs(itask, {
            'is_manual_submit': itask.is_manual_submit,
            'try_num': itask.get_try_num(),
            'time_submit': get_current_time_string(),
            'job_runner_name': itask.summary.get('job_runner_name'),
        })
        itask.is_manual_submit = False
        self.task_events_mgr.process_message(
            itask, CRITICAL, self.task_events_mgr.EVENT_SUBMIT_FAILED)

    def _prep_submit_task_job_impl(self, workflow, itask, rtconfig):
        """Helper for self._prep_submit_task_job."""

        itask.summary['platforms_used'][
            itask.submit_num] = itask.platform['name']

        itask.summary['job_runner_name'] = itask.platform['job runner']
        with suppress(TypeError):
            itask.summary[self.KEY_EXECUTE_TIME_LIMIT] = float(
                rtconfig['execution time limit']
            )

        scripts = self._get_job_scripts(itask, rtconfig)

        # Location of job file, etc
        self._create_job_log_path(workflow, itask)
        job_d = get_task_job_id(
            itask.point, itask.tdef.name, itask.submit_num)
        job_file_path = get_remote_workflow_run_job_dir(
            workflow, job_d, JOB_LOG_JOB)
        return {
            'job_runner_name': itask.platform['job runner'],
            'job_runner_command_template': (
                itask.platform['job runner command template']
            ),
            'dependencies': itask.state.get_resolved_dependencies(),
            'directives': rtconfig['directives'],
            'environment': rtconfig['environment'],
            'execution_time_limit': itask.summary[self.KEY_EXECUTE_TIME_LIMIT],
            'env-script': rtconfig['env-script'],
            'err-script': rtconfig['err-script'],
            'exit-script': rtconfig['exit-script'],
            'platform': itask.platform,
            'init-script': rtconfig['init-script'],
            'job_file_path': job_file_path,
            'job_d': job_d,
            'namespace_hierarchy': itask.tdef.namespace_hierarchy,
            'param_var': itask.tdef.param_var,
            'post-script': scripts[2],
            'pre-script': scripts[0],
            'script': scripts[1],
            'submit_num': itask.submit_num,
            'flow_label': itask.flow_label,
            'workflow_name': workflow,
            'task_id': itask.identity,
            'try_num': itask.get_try_num(),
            'uuid_str': self.task_remote_mgr.uuid_str,
            'work_d': rtconfig['work sub-directory'],
        }
