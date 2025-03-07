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
"""Default fixtures for functional tests."""

import asyncio
from functools import partial
from pathlib import Path
import pytest
from shutil import rmtree
from typing import List, TYPE_CHECKING, Tuple

from cylc.flow.cfgspec.glbl_cfg import glbl_cfg
from cylc.flow.config import WorkflowConfig
from cylc.flow.pathutil import get_cylc_run_dir
from cylc.flow.rundb import CylcWorkflowDAO
from cylc.flow.scripts.validate import ValidateOptions
from cylc.flow.wallclock import get_current_time_string

from .utils import _rm_if_empty
from .utils.flow_tools import (
    _make_flow,
    _make_scheduler,
    _run_flow
)

if TYPE_CHECKING:
    from cylc.flow.scheduler import Scheduler


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Expose the result of tests to their fixtures.

    This will add a variable to the "node" object which differs depending on
    the scope of the test.

    scope=function
        `_function_outcome` will be set to the result of the test function.
    scope=module
        `_module_outcome will be set to a list of all test results in
        the module.

    https://github.com/pytest-dev/pytest/issues/230#issuecomment-402580536

    """
    outcome = yield
    rep = outcome.get_result()

    # scope==function
    item._function_outcome = rep

    # scope==module
    _module_outcomes = getattr(item.module, '_module_outcomes', {})
    _module_outcomes[(item.nodeid, rep.when)] = rep
    item.module._module_outcomes = _module_outcomes


def _pytest_passed(request: pytest.FixtureRequest) -> bool:
    """Returns True if the test(s) a fixture was used in passed."""
    if hasattr(request.node, '_function_outcome'):
        return request.node._function_outcome.outcome in {'passed', 'skipped'}
    return all((
        report.outcome in {'passed', 'skipped'}
        for report in request.node.obj._module_outcomes.values()
    ))


@pytest.fixture(scope='session')
def run_dir():
    """The cylc run directory for this host."""
    path = Path(get_cylc_run_dir())
    path.mkdir(exist_ok=True)
    yield path


@pytest.fixture(scope='session')
def ses_test_dir(request, run_dir):
    """The root reg dir for test flows in this test session."""
    timestamp = get_current_time_string(use_basic_format=True)
    uuid = f'cit-{timestamp}'
    path = Path(run_dir, uuid)
    path.mkdir(exist_ok=True)
    yield path
    _rm_if_empty(path)


@pytest.fixture(scope='module')
def mod_test_dir(request, ses_test_dir):
    """The root reg dir for test flows in this test module."""
    path = Path(ses_test_dir, request.module.__name__)
    path.mkdir(exist_ok=True)
    yield path
    if _pytest_passed(request):
        # test passed -> remove all files
        rmtree(path, ignore_errors=False)
    else:
        # test failed -> remove the test dir if empty
        _rm_if_empty(path)


@pytest.fixture
def test_dir(request, mod_test_dir):
    """The root reg dir for test flows in this test function."""
    path = Path(mod_test_dir, request.function.__name__)
    path.mkdir(parents=True, exist_ok=True)
    yield path
    if _pytest_passed(request):
        # test passed -> remove all files
        rmtree(path, ignore_errors=False)
    else:
        # test failed -> remove the test dir if empty
        _rm_if_empty(path)


@pytest.fixture(scope='module')
def mod_flow(run_dir, mod_test_dir):
    """A function for creating module-level flows."""
    yield partial(_make_flow, run_dir, mod_test_dir)


@pytest.fixture
def flow(run_dir, test_dir):
    """A function for creating function-level flows."""
    yield partial(_make_flow, run_dir, test_dir)


@pytest.fixture(scope='module')
def mod_scheduler():
    """Return a Scheduler object for a flow.

    Usage: see scheduler() below
    """
    with _make_scheduler() as _scheduler:
        yield _scheduler


@pytest.fixture
def scheduler():
    """Return a Scheduler object for a flow.

    Args:
        reg (str): Workflow name.
        **opts (Any): Options to be passed to the Scheduler.
    """
    with _make_scheduler() as _scheduler:
        yield _scheduler


@pytest.fixture(scope='module')
def mod_run(run_dir: Path):
    """Run a module-level flow."""
    return partial(_run_flow, run_dir, None)


@pytest.fixture
def run(run_dir: Path, caplog: pytest.LogCaptureFixture):
    """Run a function-level flow."""
    return partial(_run_flow, run_dir, caplog)


@pytest.fixture
def one_conf():
    return {
        'scheduler': {
            'allow implicit tasks': True
        },
        'scheduling': {
            'graph': {
                'R1': 'one'
            }
        }
    }


@pytest.fixture(scope='module')
def mod_one_conf():
    return {
        'scheduler': {
            'allow implicit tasks': True
        },
        'scheduling': {
            'graph': {
                'R1': 'one'
            }
        }
    }


@pytest.fixture
def one(one_conf, flow, scheduler):
    reg = flow(one_conf)
    schd = scheduler(reg)
    return schd


@pytest.fixture(scope='module')
def mod_one(mod_one_conf, mod_flow, mod_scheduler):
    reg = mod_flow(mod_one_conf)
    schd = mod_scheduler(reg)
    return schd


@pytest.fixture(scope='session')
def port_range():
    ports = glbl_cfg().get(['scheduler', 'run hosts', 'ports'])
    return min(ports), max(ports)


@pytest.fixture(scope='module')
def event_loop():
    """This fixture defines the event loop used for each test.

    The default scoping for this fixture is "function" which means that all
    async fixtures must have "function" scoping.

    Defining `event_loop` as a module scoped fixture opens the door to
    module scoped fixtures but means all tests in a module will run in the same
    event loop. This is fine, it's actually an efficiency win but also
    something to be aware of.

    See: https://github.com/pytest-dev/pytest-asyncio/issues/171

    """
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    # gracefully exit async generators
    loop.run_until_complete(loop.shutdown_asyncgens())
    # cancel any tasks still running in this event loop
    for task in asyncio.all_tasks(loop):
        task.cancel()
    loop.close()


@pytest.fixture
def db_select():
    """Select columns from workflow database.

    Args:
        schd: The Scheduler object for the workflow.
        process_db_queue: Whether to process the scheduler's db queue before
            querying.
        table: The name of the database table to query.
        *columns (optional): The columns to select from the table. To select
            all columns, omit or use '*'.
        **where (optional): Kwargs specifying <column>='<value>' for use in
            WHERE clauses. If more than one specified, they will be chained
            together using an AND operator.
    """

    def _check_columns(table: str, *columns: str) -> None:
        all_columns = [x[0] for x in CylcWorkflowDAO.TABLES_ATTRS[table]]
        for col in columns:
            if col not in all_columns:
                raise ValueError(f"Column '{col}' not in table '{table}'")

    def _inner(
        schd: 'Scheduler',
        process_db_queue: bool,
        table: str,
        *columns: str,
        **where: str
    ) -> List[Tuple[str, ...]]:

        if process_db_queue:
            schd.process_workflow_db_queue()

        if table not in CylcWorkflowDAO.TABLES_ATTRS:
            raise ValueError(f"Table '{table}' not in database")
        if not columns:
            columns = ('*',)
        elif columns != ('*',):
            _check_columns(table, *columns)

        stmt = f'SELECT {",".join(columns)} FROM {table}'
        stmt_args = []
        if where:
            _check_columns(table, *where.keys())
            where_stmt = ' AND '.join([
                f'{col}=?' for col in where.keys()
            ])
            stmt += f' WHERE {where_stmt}'
            stmt_args = list(where.values())

        pri_dao = schd.workflow_db_mgr.get_pri_dao()
        try:
            return list(pri_dao.connect().execute(stmt, stmt_args))
        finally:
            pri_dao.close()

    return _inner


@pytest.fixture
def validate(run_dir):
    """Provides a function for validating workflow configurations.

    Attempts to load the configuration, will raise exceptions if there are
    errors.

    Args:
        reg - The flow to validate
        kwargs - Arguments to pass to ValidateOptions
    """
    def _validate(reg: str, **kwargs) -> None:
        WorkflowConfig(
            reg,
            str(Path(run_dir, reg, 'flow.cylc')),
            ValidateOptions(**kwargs)
        )

    return _validate
