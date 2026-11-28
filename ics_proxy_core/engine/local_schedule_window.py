"""Recognize the commissioned local overnight charging window without authorizing any command."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import TYPE_CHECKING, Any
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

    # Replacing the configured time zone cannot spend an overlapping occurrence of the same plan twice.
    def overlaps(self, saved: dict[str, Any]) -> bool:
        starts_at: float = saved["starts_at"]
        ends_at: float = saved["ends_at"]
        return (
            self.schedule_digest == saved["schedule_digest"]
            and self.plan_id == saved["plan_id"]
            and self.starts_at < ends_at
            and starts_at < self.ends_at
        )


@dataclass(frozen=True)
class ScheduleCutoff:
    """The original elapsed cutoff and the host clock epoch that can compare it."""

    epoch: str | None
    deadline: float


# A forward clock change can skip midnight. The two fold projections bracket the start of the civil date.
def _start_of_day(day: date, zone: ZoneInfo) -> float | None:
    midnight = datetime.combine(day, time.min, zone)
    stamp = midnight.timestamp()
    if datetime.fromtimestamp(stamp, zone) == midnight:
        return stamp

    before, after = sorted((int(midnight.replace(fold=1).timestamp()), int(stamp)))
    if not datetime.fromtimestamp(before, zone).date() < day <= datetime.fromtimestamp(after, zone).date():
        return None
    while after - before > 1:
        middle = (before + after) // 2
        if datetime.fromtimestamp(middle, zone).date() < day:
            before = middle
        else:
            after = middle
    return float(after) if datetime.fromtimestamp(after, zone).date() == day else None


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
        starts_at = _start_of_day(today, zone)
        end = datetime.combine(today, time(6), zone)
        ends_at = end.timestamp()
        # Preserve the exact civil cutoff. A nonexistent 06:00 cannot authorize an occurrence.
        if starts_at is None or datetime.fromtimestamp(ends_at, zone) != end:
            return None
    except OSError, OverflowError, RecursionError, TypeError, ValueError, ZoneInfoNotFoundError:
        return None
    if starts_at <= now < ends_at:
        return ScheduleWindow(plan_id, expected_digest, starts_at, ends_at, limit_a, fingerprint(tag.casefold()))
    return None
