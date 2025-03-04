#!/usr/bin/env bash
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
#-------------------------------------------------------------------------------
# Test restarting a simple workflow with a task still running (orphaned)
. "$(dirname "$0")/test_header"
set_test_number 5
init_workflow "${TEST_NAME_BASE}" <<'__FLOW_CONFIG__'
[scheduler]
    [[events]]
        abort on stall timeout = True
        stall timeout = PT0S
        abort on inactivity timeout = True
        inactivity timeout = PT3M
[scheduling]
    [[graph]]
        R1 = t1
[runtime]
    [[t1]]
        script = """
            cylc__job__wait_cylc_message_started
            cylc stop --now "${CYLC_WORKFLOW_NAME}"
            cylc__job__poll_grep_workflow_log -F 'Run: (re)start=1'
            # Should be good to send succeeded message at this point
        """
__FLOW_CONFIG__
#-------------------------------------------------------------------------------
run_ok "${TEST_NAME_BASE}-validate" cylc validate "${WORKFLOW_NAME}"
workflow_run_ok "${TEST_NAME_BASE}-run" \
    cylc play --debug --no-detach "${WORKFLOW_NAME}"
sqlite3 "${WORKFLOW_RUN_DIR}/log/db" 'SELECT cycle, name, status FROM task_pool' >'sqlite3.out'
cmp_ok 'sqlite3.out' <<'__DB_DUMP__'
1|t1|running
__DB_DUMP__
workflow_run_ok "${TEST_NAME_BASE}-restart" \
    cylc play --debug --no-detach "${WORKFLOW_NAME}"
sqlite3 "${WORKFLOW_RUN_DIR}/log/db" 'SELECT * FROM task_pool' >'sqlite3.out'
cmp_ok 'sqlite3.out' <'/dev/null'
#-------------------------------------------------------------------------------
purge
exit
