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
# Test deprecation notice for Cylc 7 simple graph (no recurrence section)

. "$(dirname "$0")/test_header"
set_test_number 2

init_workflow "${TEST_NAME_BASE}" << __FLOW__
[scheduler]
    allow implicit tasks = True
[scheduling]
    [[dependencies]]
        graph = foo
__FLOW__

TEST_NAME="${TEST_NAME_BASE}-validate"
run_ok "$TEST_NAME" cylc validate -v "$WORKFLOW_NAME"

TEST_NAME="${TEST_NAME_BASE}-cmp"
cylc validate "$WORKFLOW_NAME" 2> 'val.out'
cmp_ok val.out <<__END__
WARNING - deprecated graph items were automatically upgraded in "workflow definition":
${LOG_INDENT} * (8.0.0) [scheduling][dependencies][X]graph -> [scheduling][graph]X - for X in:
${LOG_INDENT}       graph
__END__

purge
