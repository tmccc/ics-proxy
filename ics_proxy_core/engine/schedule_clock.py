"""Read the host's kernel synchronization state for civil-time Start decisions."""

from __future__ import annotations

import sys
from collections.abc import Callable
from functools import cache
from typing import Literal

type ClockBlocker = Literal["clock_unsynchronized", "clock_check_unavailable"]


# Resolve the Linux clock query once so a platform without it can fail closed at admission.
@cache
def _ntp_query() -> Callable[[], int] | None:
    if sys.platform != "linux":
        return None
    try:
        from ctypes import CDLL, POINTER, Structure, c_int, c_int64

        query = CDLL(None).ntp_gettimex
    except AttributeError, ImportError, OSError:
        return None

    class NtpTimeval(Structure):
        # glibc writes a timeval and seven longs. Reserve the largest ABI layout because only the state is used.
        _fields_ = [("report", c_int64 * 9)]

    query.argtypes = (POINTER(NtpTimeval),)
    query.restype = c_int
    return lambda: query(NtpTimeval())


def host_clock_blocker() -> ClockBlocker | None:
    """Read current kernel NTP state without modifying the clock or waiting for a service."""
    query = _ntp_query()
    if query is None:
        return "clock_check_unavailable"
    state = query()
    # TIME_OK through TIME_WAIT include normal leap-second handling. TIME_ERROR is unsynchronized.
    if 0 <= state <= 4:
        return None
    return "clock_unsynchronized" if state == 5 else "clock_check_unavailable"


def host_clock_synchronized() -> bool:
    """Require a synchronized clock for automatic overnight Start."""
    return host_clock_blocker() is None
