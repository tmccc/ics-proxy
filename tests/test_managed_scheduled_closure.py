"""Keep verified early closure independent of other scheduled-session publications."""

from __future__ import annotations

import uuid

import pytest

from ics_proxy_core.engine.local_schedule_cutoff import scheduled_cutoff_target
from ics_proxy_core.engine.managed_intent import LOCAL_SCHEDULE_OCCURRENCE
from ics_proxy_core.engine.managed_session_stop import _RETAINED_STOP_CLOSURES
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


# Intent: honor the exact independent checkpoint when both closure publications are refused.
# Why: the spent occurrence must remain closed across replacement while the receipt still awaits storage.
@pytest.mark.parametrize("checkpoint", ["exact", "other-revision", "other-target", "other-report"])
async def test_cutoff_checks_retained_stop_closure_after_replacement(
    tmp_path, tls, unused_tcp_port, monkeypatch, checkpoint
):
    async with overnight(tmp_path, tls, unused_tcp_port, monkeypatch) as (rig, clock):
        bench = rig.bench
        async with connection(rig, "scheduled-closure-dual-refusal") as test:
            local, meter_start = await begin(test, clock)
            c, j = test.controller, test.session.journal
            request, intent = uuid.uuid4().hex, c.requests.intent()
            target = c.session_stop.target()
            c.command(request, "stop_session", intent.revision, target=target)
            await c.session_stop.worker
            assert j.outcomes.accepted_stop(local)

            clock.now = MIDNIGHT + 300
            await close(test, clock, local, meter_start)
            bench.cp, bench.connection = "B", "Plug"
            j.db.execute(
                "CREATE TRIGGER refuse_schedule_closure BEFORE INSERT ON meta "
                f"WHEN NEW.key='{LOCAL_SCHEDULE_OCCURRENCE}' AND json_extract(NEW.value,'$.native_closed') "
                "BEGIN SELECT RAISE(ABORT,'refuse scheduled closure marker'); END"
            )
            j.db.execute(
                "CREATE TRIGGER refuse_stop_closure BEFORE UPDATE ON managed_requests "
                f"WHEN NEW.id='{request}' AND json_extract(NEW.result,'$._session_stop.closure_verified') "
                "BEGIN SELECT RAISE(ABORT,'refuse Stop receipt closure'); END"
            )
            await poll_through_native_timeout(c, monkeypatch)
            receipt = j.outcomes.owner(request)
            assert receipt is not None and receipt.state == "running"
            assert receipt.result["_session_stop"].get("closure_verified") is None
            proof = j.get(_RETAINED_STOP_CLOSURES)[request]
            assert proof[:3] == [intent.revision, target, local]
            assert c.session_stop.closure_verified(local)
            assert not c.session_stop.closure_verified(local + 1)
            assert j.get(LOCAL_SCHEDULE_OCCURRENCE).get("native_closed") is None
            assert c.requests.intent() == intent and bench.firmware.effective_limit() == 6

            if checkpoint != "exact":
                fields = {
                    "other-revision": (0, intent.revision + 1),
                    "other-target": (1, "0" * 64),
                    "other-report": (3, "0" * 64),
                }
                field, other = fields[checkpoint]
                proof[field] = other
                j.put(_RETAINED_STOP_CLOSURES, {request: proof})

        clock.now = MIDNIGHT + 6 * 3600
        async with connection(rig, "closure-dual-refusal-replacement") as test:
            c, j = test.controller, test.session.journal
            assert c.local_schedule.closed_request is None
            assert c.requests.intent() == intent
            assert c.session_stop.closure_verified(local) is (checkpoint == "exact")
            # Isolate the schedule cutoff from the new connection's independent zero hold.
            await c.local_schedule.stop_if_due()
            if checkpoint == "exact":
                assert c.requests.intent() == intent and scheduled_cutoff_target(j) is None
                assert j.get(_RETAINED_STOP_CLOSURES)[request] == proof
            else:
                assert c.requests.intent().mode == "paused" and scheduled_cutoff_target(j) == local
                assert j.get(_RETAINED_STOP_CLOSURES) is None
            assert stops(bench) == [{"transactionId": local}]
