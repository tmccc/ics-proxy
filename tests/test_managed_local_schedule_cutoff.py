"""Exercise the overnight cutoff through the physical Stop and independent lifecycle."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest

from ics_proxy_core.engine.local_schedule_cutoff import scheduled_cutoff_target
from ics_proxy_core.engine.managed_intent import LOCAL_SCHEDULE_OCCURRENCE
from tests.managed_rig import starts
from tests.prototype_rig import START
from tests.test_managed_local_schedule import MIDNIGHT, connection, overnight, status
from tests.test_managed_offline_owner import OWNER_TAG
from tests.tls_support import tls as tls


# Issue a real scheduled Start and leave the independent simulated charger drawing power.
async def begin(test, clock):
    c, rig = test.controller, test.rig
    peer, bench = rig.peer, rig.bench
    await status(test, lambda value: value.get("local_hold_verified") is True)
    clock.now = MIDNIGHT
    await status(test, lambda value: bool(starts(rig)))
    await c.charging.settle()
    authorization = await bench.call(test.charger, uuid.uuid4().hex, "Authorize", {"idTag": OWNER_TAG})
    assert authorization[0] == 3 and authorization[2]["idTagInfo"]["status"] == "Accepted"
    report = START | {"idTag": OWNER_TAG, "timestamp": datetime.fromtimestamp(clock.now, UTC).isoformat()}
    response = await bench.call(test.charger, uuid.uuid4().hex, "StartTransaction", report)
    assert response[0] == 3 and response[2]["idTagInfo"]["status"] == "Accepted"
    local = response[2]["transactionId"]
    peer.plan_session = (local, 987654)
    peer.paused, peer.permission_enabled = False, True
    bench.cp, bench.connection = "C", "Charging"
    bench.firmware.requested_current = 32
    await status(test, lambda value: value.get("power_w") == 1440 and value.get("stop_target"))
    return local, report["meterStart"]


# The physical peer reports the genuine close independently from its RemoteStop reply.
async def close(test, clock, local, meter_start):
    test.rig.peer.permission_enabled = False
    report = {
        "transactionId": local,
        "timestamp": datetime.fromtimestamp(clock.now, UTC).isoformat(),
        "meterStop": meter_start + 120,
    }
    response = await test.rig.bench.call(test.charger, uuid.uuid4().hex, "StopTransaction", report)
    assert response[0] == 3


# Inspect actual charger CALLs, without substituting the wire or outcome handling.
def stops(bench):
    return [frame[3] for frame in bench.device_calls if frame[2] == "RemoteStopTransaction"]


# Intent: at 06:00 the proxy restricts the exact local session and waits for its separate closure.
# Why: a missing or rejected Stop must retain zero and cannot block a following night's genuine schedule.
@pytest.mark.parametrize("reject_first", [False, True], ids=["accepted", "rejected-first"])
async def test_cutoff_owns_exact_stop_until_real_closure(tmp_path, tls, unused_tcp_port, monkeypatch, reject_first):
    async with overnight(tmp_path, tls, unused_tcp_port, monkeypatch) as (rig, clock):
        async with connection(rig, "cutoff-offline") as test:
            c, j, bench = test.controller, test.session.journal, rig.bench
            local, meter_start = await begin(test, clock)
            expected = {"transactionId": local}
            answer = bench.firmware.answer

            # Refuse once on the actual charger wire while keeping the elected session open.
            def reply(action, request):
                if reject_first and action == "RemoteStopTransaction" and stops(bench) == [expected]:
                    return {"status": "Rejected"}
                return answer(action, request)

            monkeypatch.setattr(bench.firmware, "answer", reply)
            clock.now = MIDNIGHT + 6 * 3600 - 1
            await c.tick()
            assert not stops(bench) and bench.firmware.effective_limit() > 0

            clock.now = MIDNIGHT + 6 * 3600
            await c.tick()
            assert stops(bench) == [expected] and bench.firmware.effective_limit() == 0
            assert j.outcomes.accepted_stop(local) is not reject_first
            assert j.active() == local and not j.outcomes.stop_report_closed(local)
            occurrence = j.get(LOCAL_SCHEDULE_OCCURRENCE)
            assert occurrence["stop_revision"] == c.requests.intent().revision
            assert scheduled_cutoff_target(j) == local
            assert c.requests.offline_pause_current(c.requests.intent())

            # Rewinding calendar time cannot resurrect the occurrence after its durable cutoff.
            clock.step_wall(MIDNIGHT + 6 * 3600 - 1)
            await c.tick()
            assert stops(bench) == [expected] * (1 + reject_first)
            assert j.active() == local and j.outcomes.accepted_stop(local)
            assert bench.firmware.effective_limit() == 0

            clock.now = MIDNIGHT + 6 * 3600 + 1
            await close(test, clock, local, meter_start)
            await status(test, lambda value: value.get("local_hold_verified") is True)
            assert j.outcomes.stop_report_closed(local) and j.active() is None
            assert stops(bench) == [expected] * (1 + reject_first)

            clock.now = MIDNIGHT + 24 * 3600
            await status(test, lambda value: len(starts(rig)) == 2)
            assert j.get(LOCAL_SCHEDULE_OCCURRENCE)["request"] != occurrence["request"]


# Intent: the native session ID can lag behind the genuine cutoff Stop report, even across process replacement.
# Why: the exact closed owner session should wait at zero without an additional Stop or a lasting fault.
@pytest.mark.parametrize("unplugged", [False, True], ids=["plugged", "unplugged"])
async def test_cutoff_waits_for_native_closure_across_replacement(
    tmp_path, tls, unused_tcp_port, monkeypatch, unplugged
):
    async with overnight(tmp_path, tls, unused_tcp_port, monkeypatch) as (rig, clock):
        bench = rig.bench
        async with connection(rig, "cutoff-before-native-close") as test:
            local, meter_start = await begin(test, clock)
            clock.now = MIDNIGHT + 6 * 3600
            await test.controller.local_schedule.stop_if_due()
            assert stops(bench) == [{"transactionId": local}]
            assert test.session.journal.outcomes.accepted_stop(local)
            await close(test, clock, local, meter_start)

            # The separate native HTTP getter can still carry this ID with zero physical draw.
            bench.firmware.active = local
            bench.cp, bench.connection = ("A", "Unplug") if unplugged else ("B", "Plug")
            for _ in range(3):
                assert (await test.controller.tick())["state"] == "reconciling"
            assert not test.session.journal.fault.latched()
            assert bench.firmware.effective_limit() == 0
            assert stops(bench) == [{"transactionId": local}]

        first_reconnect_call = len(bench.device_calls)
        async with connection(rig, "cutoff-native-close-pending") as replacement:
            journal = replacement.session.journal
            assert journal.active() is None and journal.outcomes.stop_report_closed(local)
            assert journal.latest_owner_stop_id() == local
            assert journal.outcomes.latest_closed_session_stop_id() is None
            waiting = await status(replacement, lambda value: value["state"] == "reconciling")
            assert not waiting["can_start"] and waiting["stop_target"] is None
            assert not waiting["local_hold_verified"]
            assert bench.firmware.effective_limit() == 0
            assert all(
                frame[3]["csChargingProfiles"]["chargingSchedule"]["chargingSchedulePeriod"][0]["limit"] == 0
                for frame in bench.device_calls[first_reconnect_call:]
                if frame[2] == "SetChargingProfile"
            )

            bench.firmware.active = None
            await status(replacement, lambda value: value["local_hold_verified"] is True)
            assert not journal.fault.latched()
            assert bench.firmware.effective_limit() == 0
            assert stops(bench) == [{"transactionId": local}]
            assert len(starts(rig)) == 1


# Intent: a different active journal session can arrive while the exact cutoff is verifying its charger proof.
# Why: neither the old cutoff nor its automatic Pause may inherit a new transaction as their Stop target.
async def test_cutoff_cannot_stop_replacement_while_reading_native(tmp_path, tls, unused_tcp_port, monkeypatch):
    async with overnight(tmp_path, tls, unused_tcp_port, monkeypatch) as (rig, clock):
        async with connection(rig, "cutoff-superseded") as test:
            c, j, bench = test.controller, test.session.journal, rig.bench
            local, meter_start = await begin(test, clock)
            reading, resume = asyncio.Event(), asyncio.Event()
            read = c.native.read

            # Pause once between the real 0 A profile and the final independent session observation.
            async def read_target(*, closing_local=None, stop_only=False, **kwargs):
                if closing_local == local and stop_only:
                    reading.set()
                    await resume.wait()
                return await read(closing_local=closing_local, stop_only=stop_only, **kwargs)

            monkeypatch.setattr(c.native, "read", read_target)
            clock.now = MIDNIGHT + 6 * 3600
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(c.local_schedule.stop_if_due())
                try:
                    await asyncio.wait_for(reading.wait(), 5)
                    assert bench.firmware.effective_limit() == 0 and not stops(bench)
                    await close(test, clock, local, meter_start)
                    assert j.latest_owner_stop_id() == local

                    # Inject a competing journal report at this boundary to exercise the last-resort exact guard.
                    replacement = j.start(
                        uuid.uuid4().hex,
                        START
                        | {
                            "timestamp": datetime.fromtimestamp(clock.now + 1, UTC).isoformat(),
                            "meterStart": meter_start + 120,
                        },
                    )["transactionId"]
                    assert local != replacement and j.active() == replacement
                    assert j.latest_owner_stop_id() is None
                finally:
                    resume.set()
            await c._stop_requested()
            assert scheduled_cutoff_target(j) == local and j.active() == replacement
            assert not stops(bench)
