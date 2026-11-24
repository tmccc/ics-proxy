# Modified for the standalone iCS proxy repository.
"""Use the historical rehearsal pair or the exact builds selected by the release validator."""

import os
import sys
from pathlib import Path

import pytest

from tests.core_loader import installed_core, load_test_core

INSTALLED_ROOT = os.environ.get("OCPP_WEEK_COMMISSIONING_ROOT")
if INSTALLED_ROOT:
    load_test_core(Path(INSTALLED_ROOT))

from tests.conftest import unused_tcp_port_factory as unused_tcp_port_factory  # noqa: E402
from tests_installation.migrated_layout import AUDITED_SCHEMA  # noqa: E402


# Keep the two build inputs explicit so the week can be rerun without a Git checkout.
def pytest_addoption(parser):
    parser.addoption("--week-current", type=Path)
    parser.addoption("--week-previous", type=Path)
    parser.addoption("--week-previous-python", type=Path)


# Confirm the two runtime sources before using the simulator.
@pytest.fixture
def builds(pytestconfig):
    current = pytestconfig.getoption("--week-current")
    previous = pytestconfig.getoption("--week-previous")
    previous_python = pytestconfig.getoption("--week-previous-python")
    if previous_python is not None and (not previous_python.is_absolute() or not previous_python.is_file()):
        raise pytest.UsageError("the installation week requires an absolute path to the previous interpreter")
    if not INSTALLED_ROOT:
        from tests_rollback.conftest import verified_builds

        return (*verified_builds(current, previous), AUDITED_SCHEMA, previous_python)
    if (
        current != Path(__file__).resolve().parents[1]
        or previous != Path(INSTALLED_ROOT)
        or Path(sys.modules["ics_proxy_core"].__file__).resolve() != installed_core(previous) / "__init__.py"
    ):
        raise pytest.UsageError(
            "the automatic week must commission with the installed build and test the frozen candidate"
        )
    return current, previous, None, previous_python
