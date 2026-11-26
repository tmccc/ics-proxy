"""Exercise the overnight cutoff through the physical Stop and independent lifecycle."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest

from ics_proxy_core.engine.local_schedule_cutoff import scheduled_cutoff_target
from ics_proxy_core.engine.managed_intent import LOCAL_SCHEDULE_OCCURRENCE
from ics_proxy_core.engine.managed_sources import ORDINARY_HOLD
from ics_proxy_core.engine.managed_view import refresh_for_read
from tests.managed_rig import starts
from tests.prototype_rig import BOOT, START
from tests.runtime_rig import until
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


# A spontaneous Stop must wait at zero when the exact owner's native ID lags its real Stop report.
@pytest.mark.parametrize("positive_offer", [False, True], ids=["zero-offer", "positive-offer"])
@pytest.mark.parametrize("after_closure", ["offline-start", "online-handback", "explicit-pause"])
async def test_natural_owner_stop_waits_for_native_closure(
    tmp_path, tls, unused_tcp_port, monkeypatch, positive_offer, after_closure
):
    async with overnight(tmp_path, tls, unused_tcp_port, monkeypatch) as (rig, clock):
        async with connection(rig, "natural-owner-stop") as test:
            c, j, bench = test.controller, test.session.journal, rig.bench
            local, meter_start = await begin(test, clock)
            clock.now = MIDNIGHT + 30
            await close(test, clock, local, meter_start)
            assert j.active() is None and j.latest_owner_stop_id() == local
            assert j.outcomes.latest_closed_session_stop_id() is None

            bench.firmware.active = local
            bench.firmware.requested_current = 0
            bench.cp, bench.connection = "B", "Plug"
            rig.peer.permission_enabled = positive_offer
            assert rig.peer.native("CONNECTOR")[0]["outLimit"] == (60 if positive_offer else 0)

            before = len(bench.device_calls)
            if after_closure != "offline-start":
                bench.vendor_available = True
                await until(lambda: test.session.runtime.vendor_registered, timeout=20)
            for _ in range(4):
                waiting = await c.tick()
                assert waiting["state"] == "reconciling" and not waiting["local_hold_verified"]
                assert bench.firmware.effective_limit() == 0 and not j.fault.latched()
            assert not stops(bench) and len(starts(rig)) == 1
            assert all(
                frame[3]["csChargingProfiles"]["chargingSchedule"]["chargingSchedulePeriod"][0]["limit"] == 0
                for frame in bench.device_calls[before:]
                if frame[2] == "SetChargingProfile"
            )

            pause = None
            if after_closure == "explicit-pause":
                pause = uuid.uuid4().hex
                c.command(pause, "pause", c.requests.intent().revision)
            bench.firmware.active = None
            if positive_offer and pause is None:
                native = rig.peer.native

                def lingering_offer(key):
                    value = native(key)
                    if key == "CONNECTOR":
                        value[0]["outLimit"] = 60
                    return value

                # The connector offer can clear later than the separate session marker.
                with monkeypatch.context() as lag:
                    lag.setattr(rig.peer, "native", lingering_offer)
                    bench.cp, bench.connection = ("A", "Unplug") if after_closure == "offline-start" else ("B", "Plug")
                    for _ in range(4):
                        assert (await c.tick())["state"] == "reconciling"
                    assert bench.firmware.effective_limit() == 0 and not j.fault.latched()
                bench.cp, bench.connection = "B", "Plug"
            rig.peer.permission_enabled = False
            await status(test, lambda view: view.get("normal_start_ready") is True)
            settled = await c.tick()
            public = refresh_for_read(c, {**settled, "running": True, "certificate_remaining_seconds": 3600})
            assert public["can_start"] and not j.fault.latched() and not stops(bench)
            if after_closure == "online-handback":
                assert public["desired_control"] == "app" and public["station_ceiling_a"] == 6
            elif after_closure == "offline-start":
                assert public["local_hold_verified"] and public["station_ceiling_a"] == 0
                assert c.requests.offline_pause_current(c.requests.intent())
                request = c.command(uuid.uuid4().hex, "start", public["control_revision"])
                assert request["state"] == "accepted"
                await until(lambda: len(starts(rig)) == 2)
                await c.charging.settle()
            else:
                assert public["local_hold_verified"] and public["station_ceiling_a"] == 0
                assert not c.requests.offline_pause_current(c.requests.intent())
                assert c.requests.receipt(pause)["state"] == "completed"


# A process can stop while independently reported closure is pending on the already claimed zero hold.
async def test_natural_owner_stop_retains_its_exact_hold_across_replacement(
    tmp_path, tls, unused_tcp_port, monkeypatch
):
    async with overnight(tmp_path, tls, unused_tcp_port, monkeypatch) as (rig, clock):
        bench = rig.bench
        async with connection(rig, "pending-owner-closure") as test:
            c = test.controller
            local, meter_start = await begin(test, clock)
            original = c.requests.intent()
            clock.now = MIDNIGHT + 30
            await close(test, clock, local, meter_start)
            bench.firmware.active = local
            bench.firmware.requested_current = 0
            bench.cp, bench.connection = "B", "Plug"
            rig.peer.permission_enabled = True
            assert (await c.tick())["state"] == "reconciling"
            assert bench.firmware.effective_limit() == 0

        async with connection(rig, "restored-owner-closure") as replacement:
            c, j = replacement.controller, replacement.session.journal
            assert c.requests.intent() == original
            assert j.active() is None and j.latest_owner_stop_id() == local
            await status(replacement, lambda view: view["state"] == "reconciling")
            assert bench.firmware.effective_limit() == 0 and not stops(bench)
            bench.firmware.active = None
            rig.peer.permission_enabled = False
            settled = await status(replacement, lambda view: view.get("local_hold_verified") is True)
            assert settled["normal_start_ready"] and c.requests.offline_pause_current(c.requests.intent())
            assert not j.fault.latched() and not stops(bench)
            clock.now = MIDNIGHT + 24 * 3600
            await status(replacement, lambda view: len(starts(rig)) == 2)


# The charger can finish while the proxy has only saved the real Stop report, before its next native poll.
@pytest.mark.parametrize("native_lag", [False, True], ids=["already-closed", "native-lags"])
@pytest.mark.parametrize("cloud_returned", [False, True], ids=["next-offline-schedule", "cloud-handback"])
async def test_natural_owner_stop_recovers_after_replacement_before_first_poll(
    tmp_path, tls, unused_tcp_port, monkeypatch, native_lag, cloud_returned
):
    async with overnight(tmp_path, tls, unused_tcp_port, monkeypatch) as (rig, clock):
        bench = rig.bench
        async with connection(rig, "before-owner-stop") as test:
            c, j = test.controller, test.session.journal
            local, meter_start = await begin(test, clock)
            original = c.requests.intent()
            request = j.get(LOCAL_SCHEDULE_OCCURRENCE)["request"]
            receipt = c.requests.receipt(request)
            assert receipt["state"] == "completed" and receipt["result"]["local_session_verified"]
            clock.now = MIDNIGHT + 30
            await close(test, clock, local, meter_start)
            assert j.latest_owner_stop_id() == local and j.get(ORDINARY_HOLD) is None
            bench.firmware.active = local if native_lag else None
            bench.firmware.requested_current = 0
            bench.cp, bench.connection = "B", "Plug"
            rig.peer.permission_enabled = native_lag

        first_reconnect_call = len(bench.device_calls)
        async with connection(rig, "after-owner-stop") as replacement:
            c, j = replacement.controller, replacement.session.journal
            assert c.requests.intent() == original and c.requests.receipt(request) == receipt
            assert j.get(ORDINARY_HOLD)["owner_closure"] == local
            if cloud_returned:
                bench.vendor_available = True
                await until(lambda: replacement.session.runtime.vendor_registered, timeout=20)
            if native_lag:
                for _ in range(3):
                    waiting = await c.tick()
                    assert waiting["state"] == "reconciling" and not waiting["local_hold_verified"]
                    assert bench.firmware.effective_limit() == 0 and not j.fault.latched()
                assert all(
                    frame[3]["csChargingProfiles"]["chargingSchedule"]["chargingSchedulePeriod"][0]["limit"] == 0
                    for frame in bench.device_calls[first_reconnect_call:]
                    if frame[2] == "SetChargingProfile"
                )
                bench.firmware.active = None
                rig.peer.permission_enabled = False

            if cloud_returned:
                shared = await status(replacement, lambda view: view.get("station_ceiling_a") == 6)
                assert shared["desired_control"] == "app" and len(starts(rig)) == 1
            else:
                await status(replacement, lambda view: view.get("local_hold_verified") is True)
                assert c.requests.offline_pause_current(c.requests.intent())
                clock.now = MIDNIGHT + 24 * 3600
                await status(replacement, lambda view: len(starts(rig)) == 2)
            assert not j.fault.latched() and not stops(bench)


# An open session and a newer operator Pause still follow their existing restrictive startup paths.
@pytest.mark.parametrize("operator_pause", [False, True], ids=["open-session", "operator-pause"])
async def test_owner_closure_replacement_keeps_restrictive_winner(
    tmp_path, tls, unused_tcp_port, monkeypatch, operator_pause
):
    async with overnight(tmp_path, tls, unused_tcp_port, monkeypatch) as (rig, clock):
        async with connection(rig, "before-restrictive-replacement") as test:
            c, j = test.controller, test.session.journal
            local, meter_start = await begin(test, clock)
            original = c.requests.intent()
            if operator_pause:
                clock.now = MIDNIGHT + 30
                await close(test, clock, local, meter_start)
                assert j.latest_owner_stop_id() == local
                pause = uuid.uuid4().hex
                c.command(pause, "pause", original.revision)
                await status(test, lambda view: view.get("local_hold_verified") is True)
            else:
                assert j.active() == local
            assert j.get(ORDINARY_HOLD) is None

        async with connection(rig, "after-restrictive-replacement") as replacement:
            c, j = replacement.controller, replacement.session.journal
            intent = c.requests.intent()
            assert intent.mode == "paused" and intent.revision == original.revision + 1
            assert not c.requests.offline_pause_current(intent) and j.get(ORDINARY_HOLD) is None
            if operator_pause:
                assert c.requests.receipt(pause)["state"] == "completed"
                assert (await c.tick())["local_hold_verified"]
            else:
                assert c.charging.state()["state"] == "interrupted"
            assert len(starts(rig)) == 1


# An identical Boot on the same connection must reverify the retained zero for this exact, unplugged session.
async def test_natural_owner_stop_reverifies_zero_after_identical_boot(tmp_path, tls, unused_tcp_port, monkeypatch):
    async with overnight(tmp_path, tls, unused_tcp_port, monkeypatch) as (rig, clock):
        async with connection(rig, "owner-closure-original") as test:
            c, j, g = test.controller, test.session.journal, test.session.gateway
            local, meter_start = await begin(test, clock)
            clock.now = MIDNIGHT + 30
            await close(test, clock, local, meter_start)
            bench = rig.bench
            bench.firmware.active = local
            bench.firmware.requested_current = 0
            bench.cp, bench.connection = "B", "Plug"
            assert (await c.tick())["state"] == "reconciling" and g.controls.ready

            before = g.boot_observation
            bench.cp, bench.connection = "A", "Unplug"
            assert (await bench.call(test.charger, "identical-boot", "BootNotification", BOOT))[0] == 3
            assert g.boot_observation != before and g.controls.ceiling_needs_recheck()
            for _ in range(4):
                assert (await c.tick())["state"] == "reconciling"
                assert bench.firmware.effective_limit() == 0
            assert g.controls.ready and not j.fault.latched()

            bench.firmware.active = None
            await status(test, lambda view: view.get("local_hold_verified") is True)
            assert c.requests.offline_pause_current(c.requests.intent()) and not stops(bench)


# A changed ID or real draw cannot extend the exact closed owner's wait, even once zero is confirmed.
@pytest.mark.parametrize("foreign_id", [False, True], ids=["drawing", "foreign-id"])
async def test_natural_owner_stop_rejects_other_native_activity(
    tmp_path, tls, unused_tcp_port, monkeypatch, foreign_id
):
    async with overnight(tmp_path, tls, unused_tcp_port, monkeypatch) as (rig, clock):
        async with connection(rig, "contradictory-owner-stop") as test:
            c, bench = test.controller, rig.bench
            local, meter_start = await begin(test, clock)
            clock.now = MIDNIGHT + 30
            await close(test, clock, local, meter_start)
            bench.firmware.active = local
            bench.firmware.requested_current = 0
            bench.cp, bench.connection = "B", "Plug"
            assert (await c.tick())["state"] == "reconciling"

            if foreign_id:
                bench.firmware.active = local + 1
            else:
                native = rig.peer.native

                def drawing(key):
                    if key == "METER":
                        return [{"meterData": {"cur": [10, 0, 0], "power": [240, 0, 0, 240]}}]
                    return native(key)

                monkeypatch.setattr(rig.peer, "native", drawing)

            for _ in range(4):
                view = await c.tick()
            assert view["state"] == "attention" and view["fault"] == "native_authority_or_preservation_failed"
            assert bench.firmware.effective_limit() == 0 and not stops(bench)


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
