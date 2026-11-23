# Modified for the standalone iCS proxy repository.
"""Loopback port fixtures for standalone proxy tests."""

from __future__ import annotations

import os
from collections.abc import Callable

import pytest

from tests.worker_ports import listener_port_factory


# Intent: parallel workers reserve planned listeners in disjoint ranges, separate from automatic client ports.
# Why: bind-and-release ephemeral fixtures can be reused by another worker before the real server is created.
@pytest.fixture
def unused_tcp_port_factory(unused_tcp_port_factory: Callable[[], int]) -> Callable[[], int]:
    worker = os.environ.get("MODEL_WORKER_INDEX")
    return unused_tcp_port_factory if worker is None else listener_port_factory(int(worker))


# Intent: a single planned listener shares its fixture's collision-free allocation policy.
# Why: both pytest-asyncio port fixtures must follow the same per-worker range during parallel qualification.
@pytest.fixture
def unused_tcp_port(unused_tcp_port_factory: Callable[[], int]) -> int:
    return unused_tcp_port_factory()
