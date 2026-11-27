"""Read the host's kernel synchronization state for civil-time Start decisions."""

from __future__ import annotations

import sys
from collections.abc import Callable
from functools import cache


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


def host_clock_synchronized() -> bool:
    """Require current kernel NTP state without modifying the clock or waiting for a service."""
    query = _ntp_query()
    # TIME_OK through TIME_WAIT include normal leap-second handling. TIME_ERROR is unsynchronized.
    return query is not None and 0 <= query() <= 4
