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
"""Set up the cylc environment."""

import os
import logging
import pkg_resources


CYLC_LOG = 'cylc'
FILE_INSTALL_LOG = 'cylc-rsync'

LOG = logging.getLogger(CYLC_LOG)
RSYNC_LOG = logging.getLogger(FILE_INSTALL_LOG)
# Start with a null handler
for log in (LOG, RSYNC_LOG):
    log.addHandler(logging.NullHandler())

LOG_LEVELS = {
    "INFO": logging.INFO,
    "NORMAL": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
    "DEBUG": logging.DEBUG,
}

# Used widely with data element ID (internally and externally),
# scope may widen further with internal and CLI adoption.
ID_DELIM = '|'


def environ_init():
    """Initialise cylc environment."""
    # Python output buffering delays appearance of stdout and stderr
    # when output is not directed to a terminal (this occurred when
    # running pre-5.0 cylc via the posix nohup command; is it still the
    # case in post-5.0 daemon-mode cylc?)
    os.environ['PYTHONUNBUFFERED'] = 'true'


environ_init()

__version__ = '8.0b3.dev'


def iter_entry_points(entry_point_name):
    """Iterate over Cylc entry points."""
    yield from (
        entry_point
        for entry_point in pkg_resources.iter_entry_points(entry_point_name)
        # filter out the cylc namespace as it should be empty
        # all cylc packages should take the form cylc-<name>
        if entry_point.dist.key != 'cylc'
    )
