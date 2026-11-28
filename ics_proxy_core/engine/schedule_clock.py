"""Read host clock evidence for local scheduling and saved status."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from functools import cache
from pathlib import Path
from typing import Literal
from uuid import UUID

from .store import fingerprint

type ClockBlocker = Literal["clock_unsynchronized", "clock_check_unavailable"]


def host_clock_epoch() -> str | None:
    """Identify the boot and time namespace in which an elapsed reading was taken."""
    try:
        boot = UUID(Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip())
        namespace = Path("/proc/self/ns/time").stat()
    except OSError, ValueError:
        return None
    return fingerprint([boot.hex, namespace.st_dev, namespace.st_ino])


def host_boottime() -> float | None:
    """Read elapsed time since boot, including time spent suspended."""
    try:
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    except AttributeError, OSError:
        return None


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
