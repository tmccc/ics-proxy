"""Keep verified early closure independent of other scheduled-session publications."""

from __future__ import annotations

import uuid

import pytest

from ics_proxy_core.engine.local_schedule_cutoff import scheduled_cutoff_target
from ics_proxy_core.engine.managed_intent import LOCAL_SCHEDULE_OCCURRENCE
from ics_proxy_core.engine.native_http import NativeTransportUnavailableError
from tests.test_managed_local_schedule import MIDNIGHT, connection, overnight
from tests.test_managed_local_schedule_cutoff import begin, close, stops
from tests.tls_support import tls as tls


# Exercise the real supervision fallback after a single failed full audit.
async def poll_through_native_timeout(c, monkeypatch):
    observe, attempts = c.native.observe, 0

    # Only the full observation fails; the ensuing restrictive read uses the real native fixture.
    async def observe_after_timeout():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise NativeTransportUnavailableError("the full status audit timed out")
        return await observe()

    with monkeypatch.context() as outage:
        outage.setattr(c.native, "observe", observe_after_timeout)
        await c.tick()
    assert attempts == 2


# Intent: keep the spent occurrence closed even while normal status work is interrupted.
# Why: a spontaneous Stop on the charger has no management receipt to trigger a fallback read.
@pytest.mark.parametrize("interruption", ["native-audit", "charge-publication"])
async def test_scheduled_closure_survives_interrupted_poll(tmp_path, tls, unused_tcp_port, monkeypatch, interruption):
    async with overnight(tmp_path, tls, unused_tcp_port, monkeypatch) as (rig, clock):
        async with connection(rig, "scheduled-natural-closure") as test:
            local, meter_start = await begin(test, clock)
            c, j, bench = test.controller, test.session.journal, rig.bench
            intent = c.requests.intent()
            clock.now = MIDNIGHT + 300
            await close(test, clock, local, meter_start)
            bench.cp, bench.connection = "B", "Plug"

            if interruption == "native-audit":
                await poll_through_native_timeout(c, monkeypatch)
            else:
                j.db.execute(
                    "CREATE TEMP TRIGGER refuse_charge_closure BEFORE INSERT ON meta "
                    "WHEN NEW.key='managed_test_charge' AND json_extract(NEW.value,'$.state')='completed' "
                    "BEGIN SELECT RAISE(ABORT,'refuse charge projection'); END"
                )
                await c.tick()
                assert c.charging.state()["state"] == "running"
                j.db.execute("DROP TRIGGER refuse_charge_closure")

            assert j.get(LOCAL_SCHEDULE_OCCURRENCE)["native_closed"] is True
            assert c.requests.intent() == intent and bench.firmware.effective_limit() == 6

            clock.now = MIDNIGHT + 6 * 3600
            await c.tick()
            assert c.requests.intent() == intent and scheduled_cutoff_target(j) is None
            assert bench.firmware.effective_limit() == 6 and not stops(bench)


# Intent: reuse the Stop receipt's separately committed physical proof after process replacement.
# Why: a refused optional occurrence marker cannot turn an already closed session into cutoff work.
async def test_scheduled_closure_survives_marker_refusal_and_replacement(tmp_path, tls, unused_tcp_port, monkeypatch):
    async with overnight(tmp_path, tls, unused_tcp_port, monkeypatch) as (rig, clock):
        bench = rig.bench
        async with connection(rig, "scheduled-closure-marker-refused") as test:
            local, meter_start = await begin(test, clock)
            c, j = test.controller, test.session.journal
            request, intent = uuid.uuid4().hex, c.requests.intent()
            c.command(request, "stop_session", intent.revision, target=c.session_stop.target())
            await c.session_stop.worker
            assert stops(bench) == [{"transactionId": local}]

            clock.now = MIDNIGHT + 300
            await close(test, clock, local, meter_start)
            bench.cp, bench.connection = "B", "Plug"
            j.db.execute(
                "CREATE TEMP TRIGGER refuse_schedule_closure BEFORE INSERT ON meta "
                f"WHEN NEW.key='{LOCAL_SCHEDULE_OCCURRENCE}' AND json_extract(NEW.value,'$.native_closed') "
                "BEGIN SELECT RAISE(ABORT,'refuse scheduled closure marker'); END"
            )
            await poll_through_native_timeout(c, monkeypatch)
            receipt = j.outcomes.owner(request)
            assert receipt is not None and receipt.state == "completed"
            assert receipt.result["_session_stop"]["closure_verified"] is True
            assert j.get(LOCAL_SCHEDULE_OCCURRENCE).get("native_closed") is None
            assert c.requests.intent() == intent and bench.firmware.effective_limit() == 6

        clock.now = MIDNIGHT + 6 * 3600
        async with connection(rig, "scheduled-closure-marker-replacement") as test:
            c, j = test.controller, test.session.journal
            assert c.local_schedule.closed_request is None
            assert c.requests.intent() == intent
            # Isolate the cutoff from the separate, conservative recovery of the new charger connection.
            await c.local_schedule.stop_if_due()
            assert c.requests.intent() == intent and scheduled_cutoff_target(j) is None
            await c.tick()
            assert scheduled_cutoff_target(j) is None
            assert stops(bench) == [{"transactionId": local}]
