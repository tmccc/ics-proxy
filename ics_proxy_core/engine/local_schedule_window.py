"""Recognize the commissioned local overnight charging window without authorizing any command."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .ics_writes import utc_time
from .policy import integer
from .store import fingerprint

if TYPE_CHECKING:
    from .trial_handover_evidence import HandoverEvidence


_DAILY_PLAN = {
    "type": "Plan",
    "ConnectorId": 1,
    "eco": "Fast",
    "kindType": "Recurring",
    "recurring": 127,
    "startTime": 0,
    "stopModel": "Duration",
    "stopValue": 360,
    "maxEnergy": 0,
}
_PLAN_FIELDS = _DAILY_PLAN.keys() | {"planId", "idTag", "validTime", "limitCurrent"}


@dataclass(frozen=True)
class ScheduleWindow:
    """One occurrence of the supported plan and its native current ceiling."""

    plan_id: int
    schedule_digest: str
    starts_at: float
    ends_at: float
    limit_a: float
    tag_digest: str


def schedule_window(native: HandoverEvidence, expected_digest: str, timezone: str, now: float) -> ScheduleWindow | None:
    """Recognize a single intact daily plan during the half-open local midnight–06:00 window."""
    schedule = native.saved_schedule
    if not isinstance(schedule, dict) or native.saved_schedule_digest != expected_digest:
        return None
    plans = schedule.get("planList")
    if not isinstance(plans, list) or len(plans) != 1 or not isinstance(plans[0], dict):
        return None
    plan = plans[0]
    if set(plan) != _PLAN_FIELDS or any(
        type(plan[field]) is not type(value) or plan[field] != value for field, value in _DAILY_PLAN.items()
    ):
        return None
    tag = plan["idTag"]
    if not isinstance(tag, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,20}", tag) is None:
        return None
    try:
        if fingerprint(schedule) != expected_digest:
            return None
        plan_id = integer(plan["planId"], 1)
        limit_a = integer(plan["limitCurrent"], 60, 320) / 10
        utc_time(plan["validTime"])
        zone = ZoneInfo(timezone)
        today = datetime.fromtimestamp(now, zone).date()
        start = datetime.combine(today, time.min, zone)
        end = datetime.combine(today, time(6), zone)
        starts_at, ends_at = start.timestamp(), end.timestamp()
        # Use civil endpoints through clock changes. A nonexistent endpoint cannot authorize this day.
        if datetime.fromtimestamp(starts_at, zone) != start or datetime.fromtimestamp(ends_at, zone) != end:
            return None
    except OSError, OverflowError, RecursionError, TypeError, ValueError, ZoneInfoNotFoundError:
        return None
    if starts_at <= now < ends_at:
        return ScheduleWindow(plan_id, expected_digest, starts_at, ends_at, limit_a, fingerprint(tag.casefold()))
    return None
