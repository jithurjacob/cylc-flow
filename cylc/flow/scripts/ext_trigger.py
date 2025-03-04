#!/usr/bin/env python3

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

"""cylc ext-trigger [OPTIONS] ARGS

Report an external event message to a scheduler.

It is expected that a task in the workflow has registered the same message as
an external trigger - a special prerequisite to be satisfied by an external
system, via this command, rather than by triggering off other tasks.

The ID argument should uniquely distinguish one external trigger event from the
next. When a task's external trigger is satisfied by an incoming message, the
message ID is broadcast to all downstream tasks in the cycle point as
$CYLC_EXT_TRIGGER_ID so that they can use it - e.g. to identify a new data file
that the external triggering system is responding to.

Use the retry options in case the target workflow is down or out of contact.

Note: to manually trigger a task use 'cylc trigger', not this command."""

from time import sleep
from typing import TYPE_CHECKING

from cylc.flow import LOG
from cylc.flow.exceptions import CylcError, ClientError
from cylc.flow.network.client_factory import get_client
from cylc.flow.option_parsers import CylcOptionParser as COP
from cylc.flow.terminal import cli_function
from cylc.flow.workflow_files import parse_reg

if TYPE_CHECKING:
    from optparse import Values


MAX_N_TRIES = 5
RETRY_INTVL_SECS = 10.0

MSG_SEND_FAILED = "Send message: try %s of %s failed"
MSG_SEND_RETRY = "Retrying in %s seconds, timeout is %s"
MSG_SEND_SUCCEED = "Send message: try %s of %s succeeded"

MUTATION = '''
mutation (
  $wFlows: [WorkflowID]!,
  $eventMsg: String!,
  $eventId: String!
) {
  extTrigger (
    workflows: $wFlows,
    message: $eventMsg,
    id: $eventId
  ) {
    result
  }
}
'''


def get_option_parser():
    parser = COP(
        __doc__, comms=True,
        argdoc=[("WORKFLOW", "Workflow name or ID"),
                ("MSG", "External trigger message"),
                ("ID", "Unique trigger ID")])

    parser.add_option(
        "--max-tries", help="Maximum number of send attempts "
        "(default %s)." % MAX_N_TRIES, metavar="INT",
        action="store", default=MAX_N_TRIES, dest="max_n_tries")

    parser.add_option(
        "--retry-interval", help="Delay in seconds before retrying "
        "(default %s)." % RETRY_INTVL_SECS, metavar="SEC",
        action="store", default=RETRY_INTVL_SECS, dest="retry_intvl_secs")

    return parser


@cli_function(get_option_parser)
def main(
    parser: COP,
    options: 'Values',
    workflow: str,
    event_msg: str,
    event_id: str
) -> None:
    workflow, _ = parse_reg(workflow)
    LOG.info('Send to workflow %s: "%s" (%s)', workflow, event_msg, event_id)
    pclient = get_client(workflow, timeout=options.comms_timeout)

    max_n_tries = int(options.max_n_tries)
    retry_intvl_secs = float(options.retry_intvl_secs)

    mutation_kwargs = {
        'request_string': MUTATION,
        'variables': {
            'wFlows': [workflow],
            'eventMsg': event_msg,
            'eventId': event_id,
        }
    }

    for i_try in range(max_n_tries):
        try:
            pclient('graphql', mutation_kwargs)
        except ClientError as exc:
            LOG.exception(exc)
            LOG.info(MSG_SEND_FAILED, i_try + 1, max_n_tries)
            if i_try == max_n_tries - 1:  # final attempt
                raise CylcError('send failed')
            LOG.info(MSG_SEND_RETRY, retry_intvl_secs, options.comms_timeout)
            sleep(retry_intvl_secs)
        else:
            if i_try > 0:
                LOG.info(MSG_SEND_SUCCEED, i_try + 1, max_n_tries)
            break
