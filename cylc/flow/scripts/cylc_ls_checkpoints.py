#!/usr/bin/env python3

# THIS FILE IS PART OF THE CYLC SUITE ENGINE.
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

"""cylc [info] ls-checkpoints [OPTIONS] ARGS

In the absence of arguments and the --all option, list checkpoint IDs, their
time and events. Otherwise, display the latest and/or the checkpoints of suite
parameters, task pool and broadcast states in the suite runtime database.
"""

import sys
from cylc.flow.remote import remrun
if remrun():
    sys.exit(0)

from cylc.flow.option_parsers import CylcOptionParser as COP
from cylc.flow.pathutil import get_suite_run_pub_db_name
from cylc.flow.rundb import CylcSuiteDAO
from cylc.flow.terminal import cli_function


DELIM = "#" * 71
TITLE_CHECKPOINT_ID = "\n# CHECKPOINT ID (ID|TIME|EVENT)\n"
TITLE_SUITE_PARAMS = "\n# SUITE PARAMS (KEY|VALUE)\n"
TITLE_BROADCAST_STATES = "\n# BROADCAST STATES (POINT|NAMESPACE|KEY|VALUE)\n"
TITLE_TASK_POOL = "\n# TASK POOL (CYCLE|NAME|SPAWNED|STATUS|IS_HELD)\n"


def get_option_parser():
    parser = COP(__doc__, argdoc=[
        ("REG", "Suite name"),
        ("[ID ...]", "Checkpoint ID (default=latest)")])

    parser.add_option(
        "-a", "--all",
        help="Display data of all available checkpoints.",
        action="store_true", default=False, dest="all_mode")

    return parser


@cli_function(get_option_parser)
def main(parser, options, suite, *args):
    """CLI."""
    if options.all_mode:
        dao = _get_dao(suite)
        args = []
        dao.select_checkpoint_id(lambda row_idx, row: args.append(row[0]))
    if args:
        get_checkpoints_details(suite, args, _write_row)
    else:
        list_checkpoints(suite, _write_row)


def get_checkpoints_details(suite, id_keys, callback):
    """Display checkpoints with id_keys of a suite.

    For each row selected in the DB, invoke callback(title, row_idx, row)
    where title is one of TITLE_* constants of this module.
    """
    dao = _get_dao(suite)
    for id_key in id_keys:
        for dao_select, title in [
                (dao.select_checkpoint_id, TITLE_CHECKPOINT_ID),
                (dao.select_suite_params, TITLE_SUITE_PARAMS),
                (dao.select_broadcast_states, TITLE_BROADCAST_STATES),
                (dao.select_task_pool, TITLE_TASK_POOL)]:
            dao_select(
                lambda row_idx, row: callback(title, row_idx, row),
                int(id_key))


def list_checkpoints(suite, callback):
    """List available checkpoints of a suite.

    For each row selected in the DB, invoke callback(title, row_idx, row)
    where title is one of TITLE_* constants of this module.
    """
    dao = _get_dao(suite)
    dao.select_checkpoint_id(
        lambda row_idx, row: callback(TITLE_CHECKPOINT_ID, row_idx, row))


def _get_dao(suite):
    """Return the DAO (public) for suite."""
    return CylcSuiteDAO(get_suite_run_pub_db_name(suite), is_public=True)


def _write_row(title, row_idx, row):
    """Write a row to sys.stdout returned by a DB select.

    Write title if row_idx == 0
    """
    if row_idx == 0:
        if title == TITLE_CHECKPOINT_ID:
            sys.stdout.write(DELIM)
        sys.stdout.write(title)
    items = []
    for item in row:
        if item is None:
            item = ""
        items.append(str(item))
    sys.stdout.write("|".join(items) + "\n")


if __name__ == "__main__":
    main()
