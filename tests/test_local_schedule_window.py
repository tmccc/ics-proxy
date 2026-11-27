"""The local schedule uses only the commissioned plan and the configured civil time zone."""

from __future__ import annotations

import json
import ssl
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from ics_proxy_core.engine import schedule_clock
from ics_proxy_core.engine.local_schedule_window import ScheduleWindow, schedule_window
from ics_proxy_core.engine.security import SecurityError
from ics_proxy_core.engine.store import fingerprint
from ics_proxy_core.engine.trial_config import TrialConfig
from ics_proxy_core.engine.trial_evidence import Evidence
from ics_proxy_core.engine.trial_handover_evidence import HandoverEvidence
from ics_proxy_core.engine.trial_session import save_private
from tests.native_schedule_support import ORIGINAL_PLAN
from tests.tls_support import tls as tls
from tests.trial_helpers import config_files

ZONE = ZoneInfo("Europe/London")


@pytest.mark.parametrize(
    ("state", "synchronized"), [(None, False), (-1, False), (0, True), (4, True), (5, False), (6, False)]
)
def test_host_clock_requires_valid_kernel_sync_state(monkeypatch, state: int | None, synchronized: bool) -> None:
    monkeypatch.setattr(schedule_clock, "_ntp_query", lambda: None if state is None else lambda: state)
    assert schedule_clock.host_clock_synchronized() is synchronized


# Model the native schedule envelope communicated independently by the charger.
def evidence(*plans: dict[str, Any]) -> HandoverEvidence:
    schedule = {"planList": list(plans), "defaultEco": {"mode": "Fast"}}
    physical = Evidence(fingerprint("SYNTHETIC"), 0, "B", "Plug", 0, 0, False, "", ())
    return HandoverEvidence(
        physical,
        "",
        {},
        32,
        32,
        32,
        0,
        (0, 0, 0),
        (0, 0, 0, 0),
        (),
        saved_schedule_digest=fingerprint(schedule),
        saved_schedule=schedule,
    )


# Intent: include midnight and exclude 06:00 local time throughout the year.
# Why: the local tariff endpoints are the same civil times even when the clock changes within that window.
@pytest.mark.parametrize(("month", "day", "elapsed_hours"), [(11, 25, 6), (3, 29, 5), (10, 25, 7)])
def test_commissioned_window_uses_civil_endpoints(month: int, day: int, elapsed_hours: int) -> None:
    native = evidence(ORIGINAL_PLAN | {"limitCurrent": 160})
    starts_at = datetime(2026, month, day, 0, tzinfo=ZONE).timestamp()
    ends_at = datetime(2026, month, day, 6, tzinfo=ZONE).timestamp()
    expected = ScheduleWindow(
        ORIGINAL_PLAN["planId"],
        native.saved_schedule_digest,
        starts_at,
        ends_at,
        16,
        fingerprint(ORIGINAL_PLAN["idTag"].casefold()),
    )
    assert ends_at - starts_at == elapsed_hours * 3600
    assert schedule_window(native, native.saved_schedule_digest, ZONE.key, starts_at - 0.01) is None
    assert schedule_window(native, native.saved_schedule_digest, ZONE.key, starts_at) == expected
    assert schedule_window(native, native.saved_schedule_digest, ZONE.key, ends_at - 0.01) == expected
    assert schedule_window(native, native.saved_schedule_digest, ZONE.key, ends_at) is None


# Intent: refuse other plan shapes even when they are reflected in the commissioned digest supplied by the test.
# Why: the local fallback can enforce the known daily current and cutoff semantics for this native format.
@pytest.mark.parametrize(
    "change",
    [
        {"ConnectorId": True},
        {"startTime": 1},
        {"stopModel": "Full"},
        {"stopValue": 359},
        {"kindType": "Relative"},
        {"recurring": 63},
        {"eco": "Eco"},
        {"limitCurrent": 321},
        {"limitCurrent": 59},
        {"maxEnergy": 1},
        {"idTag": ""},
        {"validTime": "2020-01-01"},
        {"unsupported": True},
    ],
)
def test_local_window_requires_the_supported_plan(change: dict[str, Any]) -> None:
    native = evidence(ORIGINAL_PLAN | change)
    midnight = datetime(2026, 11, 25, tzinfo=ZONE).timestamp()
    assert schedule_window(native, native.saved_schedule_digest, ZONE.key, midnight) is None


# Intent: require a unique original plan and its complete commissioned envelope.
# Why: a partial plan or stale digest cannot identify the local charging occurrence.
def test_local_window_checks_authoritative_schedule_digest() -> None:
    original = evidence(ORIGINAL_PLAN)
    changed_default = replace(original, saved_schedule={"planList": [ORIGINAL_PLAN], "defaultEco": {"mode": "Eco"}})
    midnight = datetime(2026, 11, 25, tzinfo=ZONE).timestamp()
    assert schedule_window(original, "0" * 64, ZONE.key, midnight) is None
    assert schedule_window(changed_default, original.saved_schedule_digest, ZONE.key, midnight) is None
    for native in (evidence(), evidence(ORIGINAL_PLAN, ORIGINAL_PLAN | {"planId": 10})):
        assert schedule_window(native, native.saved_schedule_digest, ZONE.key, midnight) is None


# Intent: load the private schedule setting through normal trial preflight.
# Why: a backward-compatible opt-in still needs a separately configured owner Start identity.
def test_local_schedule_configuration_is_optional(
    tmp_path: Path, tls: tuple[ssl.SSLContext, ssl.SSLContext, Path]
) -> None:
    old, path = config_files(tmp_path, 19876, 19877)
    assert old.owner_start_tag is None and old.owner_schedule_timezone is None
    original = json.loads(path.read_text())
    save_private(path, original | {"owner_schedule_timezone": ZONE.key})
    with pytest.raises(SecurityError, match="configured owner Start tag"):
        TrialConfig.load(path)
    save_private(path, original | {"owner_start_tag": "LOCAL_OWNER", "owner_schedule_timezone": ZONE.key})
    assert TrialConfig.load(path).owner_schedule_timezone == ZONE.key


# Intent: reject a malformed or unavailable time zone during private preflight.
# Why: the proxy may only use explicit civil time when both endpoints can be computed reliably.
@pytest.mark.parametrize("timezone", ["", "European/London", "/etc/localtime", True])
def test_local_schedule_configuration_requires_named_time_zone(
    tmp_path: Path, tls: tuple[ssl.SSLContext, ssl.SSLContext, Path], timezone: str | bool
) -> None:
    _, path = config_files(tmp_path, 19876, 19877)
    original = json.loads(path.read_text())
    save_private(path, original | {"owner_start_tag": "LOCAL_OWNER", "owner_schedule_timezone": timezone})
    with pytest.raises(SecurityError, match="named time zone"):
        TrialConfig.load(path)
