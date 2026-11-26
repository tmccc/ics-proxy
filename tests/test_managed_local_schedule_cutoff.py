"""Exercise the overnight cutoff through the physical Stop and independent lifecycle."""

from __future__ import annotations

import asyncio
import sqlite3
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from ics_proxy_core.constants import HUB_PROFILE_ID
from ics_proxy_core.engine import local_schedule, managed_charging
from ics_proxy_core.engine.local_schedule_cutoff import scheduled_cutoff_target
from ics_proxy_core.engine.managed_current import capability_value
from ics_proxy_core.engine.managed_intent import AUTOMATIC_PAUSE_CAUSE, LOCAL_SCHEDULE_OCCURRENCE
from ics_proxy_core.engine.managed_sources import ORDINARY_HOLD
from ics_proxy_core.engine.managed_view import refresh_for_read
from ics_proxy_core.engine.native_http import NativeTransportUnavailableError
from ics_proxy_core.engine.recovery_contracts import Pending
from tests.managed_rig import healthy, introduced, managed_bench, starts
from tests.native_schedule_support import ZONE, SchedulePeer, SchedulerClock
from tests.prototype_rig import BOOT, START
from tests.runtime_rig import until
from tests.test_managed_local_schedule import MIDNIGHT, connection, overnight, status
from tests.test_managed_offline_owner import OWNER_TAG
from tests.test_managed_vendor_catchup import CatchupBench
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


# Keep the native offer independent from the charger wire profile and session marker.
def report_offer(peer, read_limit):
    native = peer.native

    # Override only the reported connector offer when the test supplies a value.
    def observe(key):
        value = native(key)
        if key == "CONNECTOR" and (limit := read_limit()) is not None:
            value[0]["outLimit"] = 10 * limit
        return value

    return observe


# Intent: drive the normal poll through enough native transport failures to claim zero.
# Why: the regressions need the actual durable outage hold and charger profile write.
async def hold_during_native_outage(c, monkeypatch):
    # Keep the simulated native transport unavailable through each triggering poll.
    async def unavailable():
        raise NativeTransportUnavailableError("brief loss of native telemetry")

    with monkeypatch.context() as outage:
        outage.setattr(c.native, "observe", unavailable)
        for _ in range(3):
            await c.tick()


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
            cutoff_poll = await c.tick()
            assert stops(bench) == [expected] and bench.firmware.effective_limit() == 0
            assert j.outcomes.accepted_stop(local) is not reject_first
            assert j.active() == local and not j.outcomes.stop_report_closed(local)
            occurrence = j.get(LOCAL_SCHEDULE_OCCURRENCE)
            assert occurrence["stop_revision"] == c.requests.intent().revision
            assert scheduled_cutoff_target(j) == local
            assert c.requests.offline_pause_current(c.requests.intent())
            view = refresh_for_read(c, cutoff_poll)
            assert view["cloud_connected"] is False and "pause_reason" not in view

            # Rewinding calendar time cannot resurrect the occurrence after its durable cutoff.
            clock.step_wall(MIDNIGHT + 6 * 3600 - 1)
            await c.tick()
            assert stops(bench) == [expected] * (1 + reject_first)
            assert j.active() == local and j.outcomes.accepted_stop(local)
            assert bench.firmware.effective_limit() == 0

            clock.now = MIDNIGHT + 6 * 3600 + 1
            await close(test, clock, local, meter_start)
            settled = await status(test, lambda value: value.get("local_hold_verified") is True)
            assert "pause_reason" not in settled
            assert j.outcomes.stop_report_closed(local) and j.active() is None
            assert stops(bench) == [expected] * (1 + reject_first)

            clock.now = MIDNIGHT + 24 * 3600
            await status(test, lambda value: len(starts(rig)) == 2)
            assert j.get(LOCAL_SCHEDULE_OCCURRENCE)["request"] != occurrence["request"]


# Intent: enforce the same elapsed cutoff when SQLite refuses the scheduled Start's first receipt read.
# Why: a committed Start can charge after that read fails, and a backward clock adjustment cannot extend its window.
@pytest.mark.parametrize("refuse_receipt", [False, True], ids=["read-works", "refused-post-commit-read"])
async def test_cutoff_uses_elapsed_time_after_scheduled_receipt_read(
    tmp_path, tls, unused_tcp_port, monkeypatch, refuse_receipt
):
    async with overnight(tmp_path, tls, unused_tcp_port, monkeypatch) as (rig, clock):
        async with connection(rig, "same-process-cutoff") as test:
            c, j, bench = test.controller, test.session.journal, rig.bench
            insert_seen = False
            denied = []

            # Refuse the first receipt table read while allowing the already committed Start to proceed.
            def authorize(action, table, column, database, trigger):
                if not denied and action == sqlite3.SQLITE_READ and table == "managed_requests":
                    denied.append(table)
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            # Arm the read refusal only during the actual transaction that admits the scheduled Start.
            def trace(sql):
                nonlocal insert_seen
                if sql.startswith("INSERT INTO managed_requests VALUES") and "'start'" in sql:
                    insert_seen = True
                if insert_seen and sql == "COMMIT" and not denied:
                    j.db.set_authorizer(authorize)

            if refuse_receipt:
                j.db.set_trace_callback(trace)
            try:
                local, _ = await begin(test, clock)
            finally:
                j.db.set_trace_callback(None)
                j.db.set_authorizer(None)
            assert denied == (["managed_requests"] if refuse_receipt else [])
            occurrence = j.get(LOCAL_SCHEDULE_OCCURRENCE)
            assert j.active() == bench.firmware.active == occurrence["local"] == local
            assert c.requests.receipt(occurrence["request"])["result"]["local_session_verified"]
            assert bench.firmware.effective_limit() == 6 and not stops(bench)

            # Correct the civil clock backward before the first cutoff poll while elapsed time continues.
            clock.now = MIDNIGHT + 6 * 3600
            clock.step_wall(MIDNIGHT + 6 * 3600 - 60)
            await c.tick()
            assert bench.firmware.effective_limit() == 0
            assert stops(bench) == [{"transactionId": local}]


# Intent: replacement keeps elapsed cutoff authority across a wall rewind and handles uncomparable clock records.
# Why: a positive budget observed under a different or unknown host clock cannot keep the exact session open.
@pytest.mark.parametrize("clock_epoch", ["same", "observed-wall", "changed", "unknown", "unavailable", "legacy"])
async def test_cutoff_retains_elapsed_deadline_across_replacement(
    tmp_path, tls, unused_tcp_port, monkeypatch, clock_epoch
):
    first_clock = None if clock_epoch == "unavailable" else "first-clock"
    monkeypatch.setattr(local_schedule, "host_clock_epoch", lambda: first_clock)
    async with overnight(tmp_path, tls, unused_tcp_port, monkeypatch) as (rig, clock):
        bench = rig.bench
        async with connection(rig, "cutoff-before-replacement") as test:
            local, meter_start = await begin(test, clock)
            j = test.session.journal
            occurrence = j.get(LOCAL_SCHEDULE_OCCURRENCE)
            assert occurrence["cutoff_clock"]["epoch"] == first_clock
            assert occurrence.get("stop_revision") is None and not stops(bench)
            if clock_epoch == "same":
                clock.now = MIDNIGHT + 6 * 3600
                clock.step_wall(MIDNIGHT + 6 * 3600 - 60)
            elif clock_epoch == "observed-wall":
                c = test.controller
                sample = c.current_native_sample()
                assert sample is not None
                clock.step_wall(MIDNIGHT + 6 * 3600)
                assert isinstance(c.commands.decide(lambda: c.local_schedule.positive_decision(sample)), Pending)
                assert j.get(LOCAL_SCHEDULE_OCCURRENCE)["cutoff_reached"] is True and not stops(bench)
                clock.step_wall(MIDNIGHT + 60)
            else:
                clock.now = MIDNIGHT + 60
                if clock_epoch == "legacy":
                    del occurrence["cutoff_clock"]
                    j.put(LOCAL_SCHEDULE_OCCURRENCE, occurrence)

        if clock_epoch in ("changed", "unknown"):
            monkeypatch.setattr(
                local_schedule, "host_clock_epoch", lambda: "next-clock" if clock_epoch == "changed" else None
            )
        async with connection(rig, "cutoff-after-replacement") as test:
            j = test.session.journal
            await status(test, lambda _: bool(stops(bench)))
            assert j.get(LOCAL_SCHEDULE_OCCURRENCE)["request"] == occurrence["request"]
            assert scheduled_cutoff_target(j) == local
            assert stops(bench) == [{"transactionId": local}]
            assert j.active() == local and bench.firmware.effective_limit() == 0
            assert len(starts(rig)) == 1
            await close(test, clock, local, meter_start)
            await status(test, lambda value: value.get("local_hold_verified") is True)
            assert stops(bench) == [{"transactionId": local}]


# Intent: a refused cutoff claim cannot make an observed wall cutoff eligible after a correction.
# Why: the next healthy journal turn must still restrict the same open session exactly once.
@pytest.mark.parametrize("reject_marker", [False, True], ids=["cutoff-saved", "cutoff-retained-in-memory"])
async def test_observed_cutoff_survives_refused_claim_and_wall_rewind(
    tmp_path, tls, unused_tcp_port, monkeypatch, reject_marker
):
    async with overnight(tmp_path, tls, unused_tcp_port, monkeypatch) as (rig, clock):
        async with connection(rig, "cutoff-refused-claim") as test:
            local, meter_start = await begin(test, clock)
            c, j, bench = test.controller, test.session.journal, rig.bench
            denied = []

            # Refuse the cutoff claim's BEGIN while optionally refusing the separate expiry observation.
            def authorize(action, table, column, database, trigger):
                if action == sqlite3.SQLITE_TRANSACTION and table == "BEGIN":
                    denied.append(table)
                    return sqlite3.SQLITE_DENY
                if reject_marker and action == sqlite3.SQLITE_INSERT and table == "meta":
                    denied.append(table)
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            clock.step_wall(MIDNIGHT + 6 * 3600)
            j.db.set_authorizer(authorize)
            try:
                await c.local_schedule.stop_if_due()
            finally:
                j.db.set_authorizer(None)
            assert "BEGIN" in denied and ("meta" in denied) is reject_marker
            occurrence = j.get(LOCAL_SCHEDULE_OCCURRENCE)
            assert occurrence.get("cutoff_reached", False) is not reject_marker
            assert scheduled_cutoff_target(j) is None and not stops(bench)
            assert bench.firmware.effective_limit() == 6

            clock.step_wall(MIDNIGHT + 60)
            await c.local_schedule.stop_if_due()
            assert j.get(LOCAL_SCHEDULE_OCCURRENCE)["cutoff_reached"] is True
            assert scheduled_cutoff_target(j) == local
            assert stops(bench) == [{"transactionId": local}] and bench.firmware.effective_limit() == 0
            await close(test, clock, local, meter_start)
            await status(test, lambda value: value.get("local_hold_verified") is True)
            assert stops(bench) == [{"transactionId": local}]


# Intent: a scheduled profile reply arrives after 06:00 without a RemoteStart on the charger wire.
# Why: only the same, now verified hold can permit the next day's plan or Return to iCS.
@pytest.mark.parametrize("timer_first", [False, True], ids=["wire-guard-first", "lease-timer-first"])
@pytest.mark.parametrize("resume", ["next-window", "cloud-restored", "explicit-pause"])
async def test_unsent_schedule_cutoff_recovers_app_authority(
    tmp_path, tls, unused_tcp_port, monkeypatch, timer_first, resume
):
    clock = SchedulerClock(MIDNIGHT - 60)
    base = time.monotonic()
    monkeypatch.setattr(
        local_schedule, "time", SimpleNamespace(time=lambda: clock.now, monotonic=lambda: base + clock.elapsed)
    )
    if timer_first:
        monkeypatch.setattr(
            managed_charging,
            "time",
            SimpleNamespace(time=time.time, monotonic=lambda: time.monotonic() + clock.elapsed),
        )
    async with managed_bench(
        tmp_path, tls, unused_tcp_port, native_peer=SchedulePeer, bench_type=CatchupBench, time_zone=ZONE.key
    ) as rig:
        rig.prepared.config = replace(rig.prepared.config, owner_start_tag=OWNER_TAG, owner_schedule_timezone=ZONE.key)
        async with connection(rig, "cutoff-starting-in-app", online=True) as test:
            c, j, bench = test.controller, test.session.journal, rig.bench
            await healthy(c)
            await bench.vendor_outage()
            await c.tick()
            assert c.requests.intent().mode == "app" and not test.session.runtime.vendor_registered
            reached, release = asyncio.Event(), asyncio.Event()

            # The real positive profile has reached the simulated charger, whose reply is still in flight.
            async def wait_for_profile_reply(action, payload):
                if action == "SetChargingProfile" and not reached.is_set():
                    period = payload["csChargingProfiles"]["chargingSchedule"]["chargingSchedulePeriod"]
                    if period[0]["limit"] > 0:
                        reached.set()
                        await release.wait()

            bench.before_reply = wait_for_profile_reply
            clock.now = MIDNIGHT + 6 * 3600 - 7
            await c.tick()
            try:
                await asyncio.wait_for(reached.wait(), 5)
                assert bench.firmware.effective_limit() == 6 and not starts(rig)
                clock.now = MIDNIGHT + 6 * 3600
                clock.step_wall(MIDNIGHT + 6 * 3600 - 60)
                if timer_first:
                    c.charging.changed.set()
                    await until(lambda: c.requests.intent().mode == "paused")
                    assert j.get(AUTOMATIC_PAUSE_CAUSE) is None
                if resume == "explicit-pause":
                    c.command(uuid.uuid4().hex, "pause", c.requests.intent().revision)
            finally:
                release.set()
            await c.charging.settle()
            bench.before_reply = None
            await status(test, lambda view: view.get("local_hold_verified") is True)
            occurrence, intent = j.get(LOCAL_SCHEDULE_OCCURRENCE), c.requests.intent()
            assert occurrence["local"] is None and j.active() is None
            assert not starts(rig) and not stops(bench) and bench.firmware.effective_limit() == 0
            assert j.starts.receipt()["stage"] == "unsent"
            assert c.requests.offline_pause_current(intent) is (resume != "explicit-pause")

            if resume == "cloud-restored":
                bench.vendor_available = True
                await status(test, lambda view: view.get("owner_control_enabled") is True)
                assert c.requests.intent().mode == "app" and not starts(rig)
            else:
                clock.now = MIDNIGHT + 24 * 3600
                if resume == "next-window":
                    await status(test, lambda view: len(starts(rig)) == 1)
                    await c.charging.settle()
                    assert j.get(LOCAL_SCHEDULE_OCCURRENCE)["request"] != occurrence["request"]
                else:
                    await c.tick()
                    assert c.requests.intent() == intent and not starts(rig)


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
            settled = await status(replacement, lambda value: value["local_hold_verified"] is True)
            assert settled["cloud_connected"] is False and "pause_reason" not in settled
            assert not journal.fault.latched()
            assert bench.firmware.effective_limit() == 0
            assert stops(bench) == [{"transactionId": local}]
            assert len(starts(rig)) == 1


# Intent: keep a spontaneously stopped owner session at zero while native closure lags its Stop report.
# Why: operator Pause, cloud handback and the next offline Start all need the independent closure proof.
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

                # Keep the offer positive after the native transaction marker has cleared.
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
                assert j.get(AUTOMATIC_PAUSE_CAUSE) is None
            elif after_closure == "offline-start":
                assert public["local_hold_verified"] and public["station_ceiling_a"] == 0
                assert public["cloud_connected"] is False and "pause_reason" not in public
                assert c.requests.offline_pause_current(c.requests.intent())
                request = c.command(uuid.uuid4().hex, "start", public["control_revision"])
                assert request["state"] == "accepted"
                await until(lambda: len(starts(rig)) == 2)
                await c.charging.settle()
                assert j.get(AUTOMATIC_PAUSE_CAUSE) is None
            else:
                assert public["local_hold_verified"] and public["station_ceiling_a"] == 0
                assert not c.requests.offline_pause_current(c.requests.intent())
                assert c.requests.receipt(pause)["state"] == "completed"


# Intent: handle an unplug, a native close and a genuine Stop in either order.
# Why: the first sign of closure only authorizes the restrictive wait for the other channel.
@pytest.mark.parametrize(
    ("report_order", "offered", "cloud_returned"),
    [
        ("unplug-first", 0, False),
        ("unplug-first", 6, False),
        ("unplug-first", 6, True),
        ("native-first", 0, False),
        ("native-first", 6, False),
        ("native-first", 6, True),
        ("ocpp-first", 6, False),
    ],
)
@pytest.mark.parametrize("operator_pause", [False, True], ids=["automatic", "operator-pause"])
async def test_natural_owner_stop_with_unplugged_connector(
    tmp_path, tls, unused_tcp_port, monkeypatch, report_order, offered, cloud_returned, operator_pause
):
    async with overnight(tmp_path, tls, unused_tcp_port, monkeypatch) as (rig, clock):
        async with connection(rig, "unplugged-owner-stop") as test:
            c, j, bench = test.controller, test.session.journal, rig.bench
            local, meter_start = await begin(test, clock)
            first_intent = c.requests.intent()
            occurrence = j.get(LOCAL_SCHEDULE_OCCURRENCE)
            start_receipt = c.requests.receipt(occurrence["request"])
            if cloud_returned:
                bench.vendor_available = True
                await until(lambda: test.session.runtime.vendor_registered, timeout=20)
            clock.now = MIDNIGHT + 30
            if report_order == "ocpp-first":
                await close(test, clock, local, meter_start)
            if report_order != "unplug-first":
                bench.firmware.active = None
            bench.firmware.requested_current = 0
            bench.cp, bench.connection = "A", "Unplug"
            rig.peer.permission_enabled = False

            with monkeypatch.context() as lag:
                lag.setattr(rig.peer, "native", report_offer(rig.peer, lambda: offered))
                for _ in range(4):
                    waiting = await c.tick()
                    assert waiting["state"] == "reconciling" and not waiting["local_hold_verified"]
                    assert bench.firmware.effective_limit() == 0 and not j.fault.latched()
                assert len(starts(rig)) == 1 and not stops(bench)
                assert c.requests.intent() == first_intent
                hold = j.get(ORDINARY_HOLD)
                pause = None
                if report_order != "ocpp-first":
                    assert j.active() == hold["local"] == local and hold["start"]
                    assert not j.outcomes.stop_report_closed(local)
                    if report_order == "unplug-first" and offered:
                        bench.cp, bench.connection = "B", "Plug"
                        waiting = await c.tick()
                        assert waiting["state"] == "reconciling" and not waiting["local_hold_verified"]
                        assert j.active() == local and j.get(ORDINARY_HOLD) == hold
                        assert bench.firmware.effective_limit() == 0 and not stops(bench)
                        bench.cp, bench.connection = "A", "Unplug"
                    if operator_pause and report_order == "native-first":
                        pause = uuid.uuid4().hex
                        c.command(pause, "pause", first_intent.revision)
                        assert (await c.tick())["state"] == "reconciling"
                        assert c.requests.receipt(pause)["state"] != "completed"
                    await close(test, clock, local, meter_start)
                assert j.active() is None and j.outcomes.stop_report_closed(local)
                assert c.requests.receipt(occurrence["request"]) == start_receipt

                if offered:
                    for _ in range(4):
                        assert (await c.tick())["state"] == "reconciling"
                    if pause is None:
                        assert j.get(ORDINARY_HOLD)["owner_closure"] == local
                if operator_pause and pause is None:
                    pause = uuid.uuid4().hex
                    c.command(pause, "pause", first_intent.revision)
                if offered and pause is not None:
                    assert (await c.tick())["state"] == "reconciling"
                    assert c.requests.receipt(pause)["state"] != "completed"
                assert bench.firmware.effective_limit() == 0
                assert not j.fault.latched() and not stops(bench)

            if cloud_returned and pause is None:
                bench.cp, bench.connection = "B", "Plug"
                await status(test, lambda value: value["normal_start_ready"])
                assert c.requests.intent().mode == "app" and bench.firmware.effective_limit() == 6
                assert len(starts(rig)) == 1 and not stops(bench)
                return
            await status(test, lambda value: value["local_hold_verified"])
            bench.cp, bench.connection = "B", "Plug"
            clock.now = MIDNIGHT + 24 * 3600
            if pause is not None:
                assert c.requests.receipt(pause)["state"] == "completed"
                assert not c.requests.offline_pause_current(c.requests.intent())
                await c.tick()
                assert len(starts(rig)) == 1
            else:
                assert c.requests.offline_pause_current(c.requests.intent())
                await status(test, lambda value: len(starts(rig)) == 2)
                await c.charging.settle()
            assert not stops(bench)


# Intent: reject a different native ID or measured draw while the owner's genuine Stop is outstanding.
# Why: a replug or a returning cloud connection cannot make contradictory native evidence valid.
@pytest.mark.parametrize("activity", ["drawing", "foreign-id"])
@pytest.mark.parametrize("cloud_returned", [False, True], ids=["offline", "cloud-returned"])
@pytest.mark.parametrize("replugged", [False, True], ids=["unplugged", "replugged"])
async def test_unplugged_owner_wait_rejects_other_native_activity(
    tmp_path, tls, unused_tcp_port, monkeypatch, activity, cloud_returned, replugged
):
    async with overnight(tmp_path, tls, unused_tcp_port, monkeypatch) as (rig, clock):
        async with connection(rig, "contradictory-active-owner") as test:
            c, j, bench = test.controller, test.session.journal, rig.bench
            local, _ = await begin(test, clock)
            if cloud_returned:
                bench.vendor_available = True
                await until(lambda: test.session.runtime.vendor_registered, timeout=20)
            clock.now = MIDNIGHT + 30
            bench.cp, bench.connection = "A", "Unplug"
            bench.firmware.requested_current = 0
            rig.peer.permission_enabled = False
            monkeypatch.setattr(rig.peer, "native", report_offer(rig.peer, lambda: 6))
            assert (await c.tick())["state"] == "reconciling"
            assert bench.firmware.effective_limit() == 0

            if replugged:
                bench.cp, bench.connection = ("C", "Charging") if activity == "drawing" else ("B", "Plug")
            if activity == "foreign-id":
                bench.firmware.active = local + 1
            else:
                native = rig.peer.native

                # Report independent draw during the saved zero hold.
                def drawing(key):
                    if key == "METER":
                        return [{"meterData": {"cur": [10, 0, 0], "power": [240, 0, 0, 240]}}]
                    return native(key)

                monkeypatch.setattr(rig.peer, "native", drawing)

            for _ in range(4):
                view = await c.tick()
            assert view["state"] == "attention" and view["fault"] == "native_authority_or_preservation_failed"
            assert j.active() == local and not j.outcomes.stop_report_closed(local)
            assert not stops(bench) and len(starts(rig)) == 1 and bench.firmware.effective_limit() == 0


# Intent: report a missing or changed station profile on either side of the genuine Stop.
# Why: a zero hold needs the commissioned profile, and a protective Stop needs an exact open session.
@pytest.mark.parametrize("station", ["missing", "changed"])
@pytest.mark.parametrize("closure", ["native-open", "native-closed", "ocpp-closed"])
async def test_unplugged_owner_wait_reports_station_fault(
    tmp_path, tls, unused_tcp_port, monkeypatch, station, closure
):
    async with overnight(tmp_path, tls, unused_tcp_port, monkeypatch) as (rig, clock):
        async with connection(rig, "unplugged-owner-station-fault") as test:
            c, j, bench = test.controller, test.session.journal, rig.bench
            local, meter_start = await begin(test, clock)
            clock.now = MIDNIGHT + 30
            bench.cp, bench.connection = "A", "Unplug"
            bench.firmware.requested_current = 0
            rig.peer.permission_enabled = False
            monkeypatch.setattr(rig.peer, "native", report_offer(rig.peer, lambda: 6))
            assert (await c.tick())["state"] == "reconciling"
            assert j.get(ORDINARY_HOLD)["local"] == local and bench.firmware.effective_limit() == 0

            if closure == "native-closed":
                bench.firmware.active = None
            elif closure == "ocpp-closed":
                await close(test, clock, local, meter_start)
                bench.firmware.active = local
                assert (await c.tick())["state"] == "reconciling"
                assert j.get(ORDINARY_HOLD)["owner_closure"] == local

            mark = len(bench.device_calls)
            if station == "missing":
                bench.firmware.profiles.pop(HUB_PROFILE_ID)
            else:
                profile = bench.firmware.profiles[HUB_PROFILE_ID]
                profile["chargingSchedule"]["chargingSchedulePeriod"][0]["limit"] = 6

            for _ in range(4):
                view = await c.tick()
            expected = "station_ceiling_lost" if station == "missing" else "native_authority_or_preservation_failed"
            assert view["state"] == "attention" and view["fault"] == j.fault.classification() == expected
            assert not view["local_hold_verified"] and len(starts(rig)) == 1
            assert j.outcomes.stop_report_closed(local) is (closure == "ocpp-closed")
            protective_stop = station == "missing" and closure == "native-open"
            assert stops(bench) == ([{"transactionId": local}] if protective_stop else [])
            fresh = bench.device_calls[mark:]
            assert not any(frame[2] == "ClearChargingProfile" for frame in fresh)
            writes = [frame[3]["csChargingProfiles"] for frame in fresh if frame[2] == "SetChargingProfile"]
            if station == "missing":
                assert not writes
            assert all(
                profile["chargingProfileId"] == HUB_PROFILE_ID
                and profile["chargingSchedule"]["chargingSchedulePeriod"][0]["limit"] == 0
                for profile in writes
            )


# Intent: report missing and changed station profiles after a generic native outage has claimed the owner session.
# Why: an independently proved station fault must be visible while the charger still owes its real Stop.
@pytest.mark.parametrize("cloud_returned", [False, True], ids=["offline", "cloud-returned"])
@pytest.mark.parametrize("station", ["missing", "changed"])
async def test_native_outage_owner_hold_reports_station_fault(
    tmp_path, tls, unused_tcp_port, monkeypatch, cloud_returned, station
):
    async with overnight(tmp_path, tls, unused_tcp_port, monkeypatch) as (rig, clock):
        async with connection(rig, "native-outage-station-fault") as test:
            c, j, bench = test.controller, test.session.journal, rig.bench
            local, meter_start = await begin(test, clock)
            intent = c.requests.intent()
            clock.now = MIDNIGHT + 30
            if cloud_returned:
                bench.vendor_available = True
                await until(lambda: test.session.runtime.vendor_registered, timeout=20)
            await hold_during_native_outage(c, monkeypatch)
            hold = j.get(ORDINARY_HOLD)
            assert hold["reason"] == "native_observation_unavailable" and hold["local"] == local
            assert bench.firmware.effective_limit() == 0

            bench.cp, bench.connection = "A", "Unplug"
            bench.firmware.active = None
            bench.firmware.requested_current = 0
            rig.peer.permission_enabled = False
            if station == "missing":
                bench.firmware.profiles.pop(HUB_PROFILE_ID)
                expected = "station_ceiling_lost"
            else:
                profile = bench.firmware.profiles[HUB_PROFILE_ID]
                profile["chargingSchedule"]["chargingSchedulePeriod"][0]["limit"] = 6
                expected = "native_authority_or_preservation_failed"

            mark = len(bench.device_calls)
            view = await c.tick()
            assert view["state"] == "attention" and view["fault"] == j.fault.classification() == expected
            assert not view["local_hold_verified"] and c.requests.intent() == intent
            assert j.get(ORDINARY_HOLD) == hold
            assert j.active() == local and not j.outcomes.stop_report_closed(local)
            assert not stops(bench) and len(starts(rig)) == 1
            assert not any(
                frame[2] in ("SetChargingProfile", "ClearChargingProfile") for frame in bench.device_calls[mark:]
            )
            await close(test, clock, local, meter_start)
            assert j.outcomes.stop_report_closed(local) and (await c.tick())["fault"] == expected


# Intent: retain independent closure after a native outage and detect a different session afterward.
# Why: an earlier generic zero hold still needs the exact owner's genuine Stop and independent closure.
@pytest.mark.parametrize("cloud_returned", [False, True], ids=["offline", "cloud-returned"])
@pytest.mark.parametrize("plugged", [False, True], ids=["unplugged", "plugged-native-closed"])
async def test_live_native_outage_recognizes_owner_closure(
    tmp_path, tls, unused_tcp_port, monkeypatch, cloud_returned, plugged
):
    async with overnight(tmp_path, tls, unused_tcp_port, monkeypatch) as (rig, clock):
        async with connection(rig, "outage-before-owner-unplug") as test:
            c, j, bench = test.controller, test.session.journal, rig.bench
            local, _ = await begin(test, clock)
            intent = c.requests.intent()
            request = j.get(LOCAL_SCHEDULE_OCCURRENCE)["request"]
            receipt = c.requests.receipt(request)
            clock.now = MIDNIGHT + 30
            if cloud_returned:
                bench.vendor_available = True
                await until(lambda: test.session.runtime.vendor_registered, timeout=20)
            await hold_during_native_outage(c, monkeypatch)
            hold = j.get(ORDINARY_HOLD)
            assert hold["reason"] == "native_observation_unavailable" and hold["local"] == j.active() == local

            bench.cp, bench.connection = ("B", "Plug") if plugged else ("A", "Unplug")
            if plugged:
                bench.firmware.active = None
            bench.firmware.requested_current = 0
            rig.peer.permission_enabled = False
            view = await c.tick()
            assert view["state"] == "reconciling" and not view["local_hold_verified"]
            reason = "native_closure_pending" if plugged else "unplugged_session_pending"
            assert j.get(ORDINARY_HOLD) == {**hold, "reason": reason}
            assert j.phase.read().value == "recovering" and bench.firmware.effective_limit() == 0

            bench.cp, bench.connection = "B", "Plug"
            assert (await c.tick())["state"] == "reconciling" and not j.fault.latched()
            bench.firmware.active = local + 1
            for _ in range(4):
                view = await c.tick()
            assert view["state"] == "attention" and view["fault"] == "native_authority_or_preservation_failed"
            assert j.active() == local and not j.outcomes.stop_report_closed(local)
            assert c.requests.intent() == intent and c.requests.receipt(request) == receipt
            assert not stops(bench) and len(starts(rig)) == 1 and bench.firmware.effective_limit() == 0


# Intent: report proved station loss when startup restores a saved owner closure hold.
# Why: restarting during either half of closure must preserve the fault and the real Stop evidence.
@pytest.mark.parametrize("ocpp_closed", [False, True], ids=["native-closed", "ocpp-closed"])
async def test_replacement_reports_missing_station_during_owner_wait(
    tmp_path, tls, unused_tcp_port, monkeypatch, ocpp_closed
):
    async with overnight(tmp_path, tls, unused_tcp_port, monkeypatch) as (rig, clock):
        bench = rig.bench
        async with connection(rig, "before-station-loss") as test:
            c, j = test.controller, test.session.journal
            local, meter_start = await begin(test, clock)
            intent = c.requests.intent()
            clock.now = MIDNIGHT + 30
            bench.cp, bench.connection = "A", "Unplug"
            bench.firmware.requested_current = 0
            rig.peer.permission_enabled = False
            monkeypatch.setattr(rig.peer, "native", report_offer(rig.peer, lambda: 6))
            assert (await c.tick())["state"] == "reconciling"
            assert j.get(ORDINARY_HOLD)["local"] == local and bench.firmware.effective_limit() == 0
            if ocpp_closed:
                await close(test, clock, local, meter_start)
                bench.firmware.active = local
                assert (await c.tick())["state"] == "reconciling"
                assert j.get(ORDINARY_HOLD)["owner_closure"] == local
            else:
                bench.firmware.active = None

        mark = len(bench.device_calls)
        bench.firmware.profiles.pop(HUB_PROFILE_ID)
        async with connection(rig, "after-station-loss") as test:
            c, j = test.controller, test.session.journal
            view = await c.tick()
            assert view["state"] == "attention" and view["fault"] == "station_ceiling_lost"
            assert not view["local_hold_verified"] and c.requests.intent() == intent
            assert j.outcomes.stop_report_closed(local) is ocpp_closed
            if not ocpp_closed:
                await close(test, clock, local, meter_start)
            bench.firmware.active = None
            assert j.outcomes.stop_report_closed(local) and j.active() is None
            assert (await c.tick())["fault"] == "station_ceiling_lost"
            assert HUB_PROFILE_ID not in bench.firmware.profiles and not stops(bench) and len(starts(rig)) == 1
            assert not any(frame[2] == "SetChargingProfile" for frame in bench.device_calls[mark:])


# Intent: report station loss on replacement with a generic outage hold or with a genuine Stop alone.
# Why: the fault must retain the owner's app revision and the exact, independent lifecycle evidence.
@pytest.mark.parametrize("ocpp_closed", [False, True], ids=["native-first", "ocpp-first"])
async def test_replacement_first_observes_owner_closure_with_missing_station(
    tmp_path, tls, unused_tcp_port, monkeypatch, ocpp_closed
):
    async with overnight(tmp_path, tls, unused_tcp_port, monkeypatch) as (rig, clock):
        bench = rig.bench
        async with connection(rig, "before-outage-station-loss") as test:
            c, j = test.controller, test.session.journal
            local, meter_start = await begin(test, clock)
            intent = c.requests.intent()
            clock.now = MIDNIGHT + 30
            if ocpp_closed:
                await close(test, clock, local, meter_start)
                assert j.outcomes.stop_report_closed(local) and not j.get(ORDINARY_HOLD)
                bench.cp, bench.connection = "B", "Plug"
                bench.firmware.active = local
            else:
                await hold_during_native_outage(c, monkeypatch)
                hold = j.get(ORDINARY_HOLD)
                assert j.active() == hold["local"] == local and hold["reason"] == "native_observation_unavailable"
                bench.cp, bench.connection = "A", "Unplug"
                bench.firmware.active = None
            bench.firmware.requested_current = 0
            rig.peer.permission_enabled = False

        mark = len(bench.device_calls)
        bench.firmware.profiles.pop(HUB_PROFILE_ID)
        async with connection(rig, "after-outage-station-loss") as test:
            c, j = test.controller, test.session.journal
            view = await c.tick()
            assert view["state"] == "attention" and view["fault"] == "station_ceiling_lost"
            assert not view["local_hold_verified"] and c.requests.intent() == intent
            assert j.outcomes.stop_report_closed(local) is ocpp_closed
            if not ocpp_closed:
                assert j.active() == local
                await close(test, clock, local, meter_start)
            bench.firmware.active = None
            assert j.outcomes.stop_report_closed(local) and j.active() is None
            assert (await c.tick())["fault"] == "station_ceiling_lost"
            assert HUB_PROFILE_ID not in bench.firmware.profiles and not stops(bench) and len(starts(rig)) == 1
            assert not any(
                frame[2] in ("SetChargingProfile", "ClearChargingProfile") for frame in bench.device_calls[mark:]
            )


# Intent: deliver the genuine Stop while a native unplug audit is unresolved.
# Why: the newer ledger event changes which closure proof the existing zero hold needs.
async def test_genuine_owner_stop_can_arrive_during_unplug_audit(tmp_path, tls, unused_tcp_port, monkeypatch):
    async with overnight(tmp_path, tls, unused_tcp_port, monkeypatch) as (rig, clock):
        async with connection(rig, "owner-stop-during-unplug-audit") as test:
            c, j, bench = test.controller, test.session.journal, rig.bench
            local, meter_start = await begin(test, clock)
            bench.vendor_available = True
            await until(lambda: test.session.runtime.vendor_registered, timeout=20)
            clock.now = MIDNIGHT + 30
            bench.cp, bench.connection = "A", "Unplug"
            bench.firmware.active = None
            bench.firmware.requested_current = 0
            rig.peer.permission_enabled = False
            observed, resume = asyncio.Event(), asyncio.Event()
            observe = c.native.observe

            # Let the genuine Stop reach the proxy before the suspended audit resumes.
            async def delay_native():
                observed.set()
                await resume.wait()
                return await observe()

            with monkeypatch.context() as lag:
                lag.setattr(rig.peer, "native", report_offer(rig.peer, lambda: 6))
                assert (await c.tick())["state"] == "reconciling"
                assert j.get(ORDINARY_HOLD)["local"] == local
                lag.setattr(c.native, "observe", delay_native)
                polling = asyncio.create_task(c.tick())
                try:
                    async with asyncio.timeout(5):
                        await observed.wait()
                    await close(test, clock, local, meter_start)
                finally:
                    resume.set()
                assert (await polling)["state"] == "reconciling"
                assert (await c.tick())["state"] == "reconciling"
                assert j.get(ORDINARY_HOLD)["owner_closure"] == local
                assert j.outcomes.stop_report_closed(local) and not j.fault.latched()
                assert len(starts(rig)) == 1 and not stops(bench) and bench.firmware.effective_limit() == 0

            bench.cp, bench.connection = "B", "Plug"
            await status(test, lambda view: view["normal_start_ready"])
            assert c.requests.intent().mode == "app" and bench.firmware.effective_limit() == 6
            assert len(starts(rig)) == 1 and not stops(bench)


# Intent: retry an explicitly requested Pause Stop after the charger rejects it while unplugged.
# Why: the same native ID permits a scoped retry, and the receipt still requires genuine closure.
async def test_explicit_pause_retries_an_unplugged_owner_stop(tmp_path, tls, unused_tcp_port, monkeypatch):
    async with overnight(tmp_path, tls, unused_tcp_port, monkeypatch) as (rig, clock):
        async with connection(rig, "pause-retry-exact-owner") as test:
            c, j, bench = test.controller, test.session.journal, rig.bench
            local, meter_start = await begin(test, clock)
            clock.now = MIDNIGHT + 30
            bench.cp, bench.connection = "A", "Unplug"
            bench.firmware.requested_current = 0
            rig.peer.permission_enabled = False
            answer = bench.firmware.answer
            expected = {"transactionId": local}

            # Refuse just the first wire Stop for the session under test.
            def reject_once(action, payload):
                if action == "RemoteStopTransaction" and stops(bench) == [expected]:
                    return {"status": "Rejected"}
                return answer(action, payload)

            monkeypatch.setattr(bench.firmware, "answer", reject_once)
            request = uuid.uuid4().hex
            c.command(request, "pause", c.requests.intent().revision)
            await asyncio.wait_for(asyncio.shield(c.pause_task), 3)
            assert stops(bench) == [expected] and not j.outcomes.accepted_stop(local)
            assert bench.firmware.effective_limit() == 0

            for _ in range(3):
                assert (await c.tick())["state"] == "reconciling"
            assert stops(bench) == [expected, expected] and j.outcomes.accepted_stop(local)
            assert c.requests.receipt(request)["state"] != "completed"
            await close(test, clock, local, meter_start)
            await status(test, lambda value: value["local_hold_verified"])
            assert c.requests.receipt(request)["state"] == "completed"


# Intent: restore the exact native closure hold before the charger sends its genuine Stop.
# Why: replacing the listener must preserve the next automatic occurrence and any later explicit Pause.
@pytest.mark.parametrize(
    ("connector", "old_poll", "native_open"),
    [
        pytest.param("Unplug", True, False, id="unplug-held-native-closed"),
        pytest.param("Unplug", True, True, id="unplug-held-native-id-retained"),
        pytest.param("Plug", True, False, id="plugged-held-native-closed"),
        pytest.param("Unplug", False, False, id="unplug-startup-native-closed"),
        pytest.param("Plug", False, False, id="plugged-startup-native-closed"),
    ],
)
@pytest.mark.parametrize("operator_pause", [False, True], ids=["next-schedule", "newer-pause"])
async def test_replacement_after_native_closure_before_owner_stop(
    tmp_path, tls, unused_tcp_port, monkeypatch, connector, old_poll, native_open, operator_pause
):
    async with overnight(tmp_path, tls, unused_tcp_port, monkeypatch) as (rig, clock):
        bench = rig.bench
        offered = None
        monkeypatch.setattr(rig.peer, "native", report_offer(rig.peer, lambda: offered))
        async with connection(rig, "before-owner-stop-replacement") as test:
            c, j = test.controller, test.session.journal
            local, meter_start = await begin(test, clock)
            intent = c.requests.intent()
            occurrence = j.get(LOCAL_SCHEDULE_OCCURRENCE)
            receipt = c.requests.receipt(occurrence["request"])
            clock.now = MIDNIGHT + 30
            bench.cp, bench.connection = "A" if connector == "Unplug" else "B", connector
            if not native_open:
                bench.firmware.active = None
            bench.firmware.requested_current = 0
            rig.peer.permission_enabled = False
            offered = 6
            if old_poll:
                assert (await c.tick())["state"] == "reconciling"
            saved = j.get(ORDINARY_HOLD)
            if old_poll:
                assert saved["local"] == local and saved["start"] and bench.firmware.effective_limit() == 0
            else:
                assert saved is None

        async with connection(rig, "before-owner-stop-replacement-boot") as test:
            c, j = test.controller, test.session.journal
            hold = j.get(ORDINARY_HOLD)
            assert hold["local"] == local and hold["start"]
            if saved is not None:
                assert hold == saved
            for _ in range(3):
                waiting = await c.tick()
                assert waiting["state"] == "reconciling" and not waiting["local_hold_verified"]
            assert c.requests.intent() == intent and j.get(ORDINARY_HOLD) == hold
            assert j.active() == local and not j.outcomes.stop_report_closed(local)
            assert len(starts(rig)) == 1 and not stops(bench) and bench.firmware.effective_limit() == 0

            bench.firmware.active = None
            pause = uuid.uuid4().hex if operator_pause else None
            if pause is not None:
                c.command(pause, "pause", intent.revision)
                assert (await c.tick())["state"] == "reconciling"
                assert c.requests.receipt(pause)["state"] != "completed"

            await close(test, clock, local, meter_start)
            offered = None
            await status(test, lambda view: view["local_hold_verified"])
            assert j.outcomes.stop_report_closed(local) and not j.fault.latched()
            assert c.requests.receipt(occurrence["request"]) == receipt
            bench.cp, bench.connection = "B", "Plug"
            clock.now = MIDNIGHT + 24 * 3600
            if pause is not None:
                assert c.requests.receipt(pause)["state"] == "completed"
                assert not c.requests.offline_pause_current(c.requests.intent())
                await c.tick()
                assert len(starts(rig)) == 1
            else:
                assert c.requests.offline_pause_current(c.requests.intent())
                await status(test, lambda view: len(starts(rig)) == 2)
                await c.charging.settle()
            assert not stops(bench)


# Intent: recognize owner closure at startup after an ordinary native telemetry outage.
# Why: the saved hold identifies the owner's Start while independent closure and its genuine Stop settle separately.
@pytest.mark.parametrize(
    "connector", ["plugged-id-retained", "plugged-native-closed", "unplugged-native-closed", "unplugged-id-retained"]
)
async def test_replacement_recognizes_owner_closure_after_native_outage(
    tmp_path, tls, unused_tcp_port, monkeypatch, connector
):
    async with overnight(tmp_path, tls, unused_tcp_port, monkeypatch) as (rig, clock):
        bench = rig.bench
        offered = None
        monkeypatch.setattr(rig.peer, "native", report_offer(rig.peer, lambda: offered))
        async with connection(rig, "owner-unplug-outage-original") as test:
            c, j = test.controller, test.session.journal
            local, meter_start = await begin(test, clock)
            intent = c.requests.intent()
            occurrence = j.get(LOCAL_SCHEDULE_OCCURRENCE)
            receipt = c.requests.receipt(occurrence["request"])
            clock.now = MIDNIGHT + 30

            await hold_during_native_outage(c, monkeypatch)
            saved = j.get(ORDINARY_HOLD)
            assert saved["reason"] == "native_observation_unavailable"
            assert saved["local"] == local and saved["start"]
            assert bench.firmware.effective_limit() == 0
            assert j.active() == local and not stops(bench)

            bench.firmware.requested_current = 0
            rig.peer.permission_enabled = False
            if connector.startswith("plugged-"):
                bench.cp, bench.connection = "B", "Plug"
            else:
                bench.cp, bench.connection = "A", "Unplug"
            if connector.endswith("native-closed"):
                bench.firmware.active = None
            if connector == "plugged-native-closed":
                offered = 6

        async with connection(rig, "owner-unplug-outage-replacement") as test:
            c, j = test.controller, test.session.journal
            assert j.active() == local and not j.outcomes.stop_report_closed(local)
            if connector == "plugged-id-retained":
                assert c.requests.intent().mode == "paused"
                assert c.charging.state()["state"] == "interrupted"
                assert j.get(ORDINARY_HOLD) == saved
                return

            assert c.requests.intent() == intent
            reason = "native_closure_pending" if connector == "plugged-native-closed" else "unplugged_session_pending"
            assert j.get(ORDINARY_HOLD) == {**saved, "reason": reason}
            assert (await c.tick())["state"] == "reconciling"
            assert bench.firmware.effective_limit() == 0 and not stops(bench)
            await close(test, clock, local, meter_start)
            bench.firmware.active = None
            if offered is not None:
                assert (await c.tick())["state"] == "reconciling"
                assert j.outcomes.stop_report_closed(local) and not stops(bench)
                assert c.requests.intent() == intent and bench.firmware.effective_limit() == 0
                offered = None
            await status(test, lambda view: view["local_hold_verified"])
            assert j.outcomes.stop_report_closed(local) and not j.fault.latched()
            assert c.requests.receipt(occurrence["request"]) == receipt
            assert c.requests.offline_pause_current(c.requests.intent())

            bench.cp, bench.connection = "B", "Plug"
            clock.now = MIDNIGHT + 24 * 3600
            await status(test, lambda view: len(starts(rig)) == 2)
            await c.charging.settle()
            assert not stops(bench)


# Intent: keep either closure order at zero through a charger reconnect and another Boot on its new socket.
# Why: a failed vendor registration must leave the next scheduled occurrence available once closure settles.
@pytest.mark.parametrize("closure_order", ["native-first", "report-first", "after-audit"])
async def test_owner_closure_during_charger_reconnect(tmp_path, tls, unused_tcp_port, monkeypatch, closure_order):
    async with overnight(tmp_path, tls, unused_tcp_port, monkeypatch) as (rig, clock):
        bench = rig.bench
        async with connection(rig, "before-owner-closure-reconnect") as test:
            c, j, g = test.controller, test.session.journal, test.session.gateway
            local, meter_start = await begin(test, clock)
            original = c.requests.intent()
            occurrence = j.get(LOCAL_SCHEDULE_OCCURRENCE)
            receipt = c.requests.receipt(occurrence["request"])
            assert j.get(ORDINARY_HOLD) is None
            clock.now = MIDNIGHT + 30
            reported_first = closure_order == "report-first"
            if reported_first:
                await close(test, clock, local, meter_start)
            bench.cp, bench.connection = "B", "Plug"
            bench.firmware.active = None if closure_order == "native-first" else local
            bench.firmware.requested_current = 0
            rig.peer.permission_enabled = False

            await test.charger.close()
            await until(lambda: not test.session.runtime.status.charger_connected)
            limits = len(bench.received_limits)
            async with rig.charger(test.session) as charger:
                reconnected = replace(test, charger=charger)
                assert (await bench.call(charger, "owner-closure-reconnect", "BootNotification", BOOT))[0] == 3
                await until(lambda: test.session.runtime.status.vendor_failed_charger_epoch == g._charger_epoch)
                if closure_order == "after-audit":
                    assert g.controls.ready and j.get(ORDINARY_HOLD) is None
                    bench.firmware.active = None
                view = await c.tick()
                assert view["state"] == "reconciling" and not view["local_hold_verified"]
                hold = j.get(ORDINARY_HOLD)
                if not reported_first:
                    assert c.requests.intent() == original and j.active() == local
                    assert hold["local"] == local and hold["reason"] == "native_closure_pending"
                    assert not j.outcomes.stop_report_closed(local)

                boot = g.boot_observation
                assert (await bench.call(charger, "owner-closure-repeat-boot", "BootNotification", BOOT))[0] == 3
                assert g.boot_observation != boot
                assert (await c.tick())["state"] == "reconciling"
                if not reported_first:
                    assert j.get(ORDINARY_HOLD) == hold and c.requests.intent() == original
                    await close(reconnected, clock, local, meter_start)
                bench.firmware.active = None
                await status(reconnected, lambda value: value["local_hold_verified"])
                assert j.outcomes.stop_report_closed(local) and not j.fault.latched()
                assert c.requests.receipt(occurrence["request"]) == receipt
                assert c.requests.offline_pause_current(c.requests.intent())
                assert bench.received_limits[limits:] and set(bench.received_limits[limits:]) == {0}
                assert len(starts(rig)) == 1 and not stops(bench)

                clock.now = MIDNIGHT + 24 * 3600
                await status(reconnected, lambda value: len(starts(rig)) == 2)
                await c.charging.settle()


# Intent: recognize native closure when a previous reconnect had already saved a hold for the exact owner Start.
# Why: that hold must preserve the completed Start and the following schedule even across process replacement.
async def test_owner_closure_replacement_upgrades_existing_reconnect_hold(tmp_path, tls, unused_tcp_port, monkeypatch):
    async with overnight(tmp_path, tls, unused_tcp_port, monkeypatch) as (rig, clock):
        bench = rig.bench
        async with connection(rig, "before-first-owner-reconnect") as test:
            c, j = test.controller, test.session.journal
            local, meter_start = await begin(test, clock)
            original = c.requests.intent()
            occurrence = j.get(LOCAL_SCHEDULE_OCCURRENCE)
            receipt = c.requests.receipt(occurrence["request"])
            bench.vendor_available = True
            await test.charger.close()
            await until(lambda: not test.session.runtime.status.charger_connected)
            async with rig.charger(test.session) as charger:
                await introduced(rig, test.session, charger, "reconnect-while-owner-session-open")
                assert j.active() == bench.firmware.active == local
                assert (await c.tick())["state"] == "reconciling"
                hold = j.get(ORDINARY_HOLD)
                assert hold["local"] == local and hold["reason"] == "restricted_introduction"
                assert bench.firmware.effective_limit() == 0

                clock.now = MIDNIGHT + 30
                bench.cp, bench.connection = "B", "Plug"
                bench.firmware.active = None
                bench.firmware.requested_current = 0
                rig.peer.permission_enabled = False
                await bench.vendor_outage()

        async with connection(rig, "replacement-after-native-close") as replacement:
            c, j = replacement.controller, replacement.session.journal
            assert c.requests.intent() == original and c.charging.state()["state"] == "running"
            assert j.get(ORDINARY_HOLD) == {**hold, "reason": "native_closure_pending"}
            assert (await c.tick())["state"] == "reconciling"
            assert j.active() == local and not j.outcomes.stop_report_closed(local)
            assert bench.firmware.effective_limit() == 0 and not stops(bench)

            await close(replacement, clock, local, meter_start)
            await status(replacement, lambda value: value["local_hold_verified"])
            assert j.outcomes.stop_report_closed(local) and not j.fault.latched()
            assert c.requests.receipt(occurrence["request"]) == receipt
            assert c.requests.offline_pause_current(c.requests.intent())
            assert len(starts(rig)) == 1 and not stops(bench)
            clock.now = MIDNIGHT + 24 * 3600
            await status(replacement, lambda value: len(starts(rig)) == 2)
            await c.charging.settle()


# Intent: retain an exact zero hold across replacement while native closure is pending.
# Why: the genuine Stop remains valid after replacement and must still wait for independent closure.
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


# Intent: replace the proxy after the genuine Stop and before the next native poll.
# Why: closure may already be complete or the transaction marker or offer may still lag.
@pytest.mark.parametrize(
    "native_lag", ["none", "session-id", "offer"], ids=["already-closed", "native-lags", "offer-lags"]
)
@pytest.mark.parametrize("cloud_returned", [False, True], ids=["next-offline-schedule", "cloud-handback"])
async def test_natural_owner_stop_recovers_after_replacement_before_first_poll(
    tmp_path, tls, unused_tcp_port, monkeypatch, native_lag, cloud_returned
):
    async with overnight(tmp_path, tls, unused_tcp_port, monkeypatch) as (rig, clock):
        bench = rig.bench
        offered = None
        monkeypatch.setattr(rig.peer, "native", report_offer(rig.peer, lambda: offered))
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
            bench.firmware.active = local if native_lag == "session-id" else None
            bench.firmware.requested_current = 0
            bench.cp, bench.connection = ("A", "Unplug") if native_lag == "offer" else ("B", "Plug")
            rig.peer.permission_enabled = native_lag == "session-id"
            if native_lag == "offer":
                offered = 6

        first_reconnect_call = len(bench.device_calls)
        async with connection(rig, "after-owner-stop") as replacement:
            c, j = replacement.controller, replacement.session.journal
            assert c.requests.intent() == original and c.requests.receipt(request) == receipt
            assert j.get(ORDINARY_HOLD)["owner_closure"] == local
            if cloud_returned:
                bench.vendor_available = True
                await until(lambda: replacement.session.runtime.vendor_registered, timeout=20)
            if native_lag != "none":
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
                offered = None

            if cloud_returned:
                shared = await status(replacement, lambda view: view.get("station_ceiling_a") == 6)
                assert shared["desired_control"] == "app" and len(starts(rig)) == 1
            else:
                await status(replacement, lambda view: view.get("local_hold_verified") is True)
                assert c.requests.offline_pause_current(c.requests.intent())
                bench.cp, bench.connection = "B", "Plug"
                clock.now = MIDNIGHT + 24 * 3600
                await status(replacement, lambda view: len(starts(rig)) == 2)
            assert not j.fault.latched() and not stops(bench)


# Intent: keep the restrictive startup outcome for an open session or a newer explicit Pause.
# Why: the earlier Start or Stop cannot replace a current operator Pause or prove that an open session closed.
@pytest.mark.parametrize("situation", ["open-session", "car-paused", "operator-pause"])
async def test_owner_closure_replacement_keeps_restrictive_winner(
    tmp_path, tls, unused_tcp_port, monkeypatch, situation
):
    async with overnight(tmp_path, tls, unused_tcp_port, monkeypatch) as (rig, clock):
        async with connection(rig, "before-restrictive-replacement") as test:
            c, j = test.controller, test.session.journal
            local, meter_start = await begin(test, clock)
            original = c.requests.intent()
            if situation == "operator-pause":
                clock.now = MIDNIGHT + 30
                await close(test, clock, local, meter_start)
                assert j.latest_owner_stop_id() == local
                pause = uuid.uuid4().hex
                c.command(pause, "pause", original.revision)
                await status(test, lambda view: view.get("local_hold_verified") is True)
            else:
                assert j.active() == local
                if situation == "car-paused":
                    bench = rig.bench
                    bench.cp, bench.connection = "B", "Plug"
                    bench.firmware.requested_current = 0
                    rig.peer.permission_enabled = False
                    view = await status(test, lambda value: value["power_w"] == 0)
                    assert view["charging_session_open"] and bench.firmware.active == local
            assert j.get(ORDINARY_HOLD) is None

        async with connection(rig, "after-restrictive-replacement") as replacement:
            c, j = replacement.controller, replacement.session.journal
            intent = c.requests.intent()
            assert intent.mode == "paused" and intent.revision == original.revision + 1
            assert not c.requests.offline_pause_current(intent) and j.get(ORDINARY_HOLD) is None
            if situation == "operator-pause":
                assert c.requests.receipt(pause)["state"] == "completed"
                assert (await c.tick())["local_hold_verified"]
            else:
                assert c.charging.state()["state"] == "interrupted"
            assert len(starts(rig)) == 1


# Intent: reverify the retained zero for the exact unplugged session after an identical Boot.
# Why: a new Boot observation expires the previous proof even when the payload and connection match.
@pytest.mark.parametrize("native_lag", ["session-id", "offer"])
async def test_natural_owner_stop_reverifies_zero_after_identical_boot(
    tmp_path, tls, unused_tcp_port, monkeypatch, native_lag
):
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

            offered = None
            monkeypatch.setattr(rig.peer, "native", report_offer(rig.peer, lambda: offered))
            if native_lag == "offer":
                bench.firmware.active = None
                offered = 6
            before = g.boot_observation
            bench.cp, bench.connection = "A", "Unplug"
            assert (await bench.call(test.charger, "identical-boot", "BootNotification", BOOT))[0] == 3
            assert g.boot_observation != before and g.controls.ceiling_needs_recheck()
            for _ in range(4):
                assert (await c.tick())["state"] == "reconciling"
                assert bench.firmware.effective_limit() == 0
            assert g.controls.ready and not j.fault.latched()

            bench.firmware.active = None
            offered = None
            await status(test, lambda view: view.get("local_hold_verified") is True)
            assert c.requests.offline_pause_current(c.requests.intent()) and not stops(bench)


# Intent: reject contradictory native activity during the exact owner closure hold.
# Why: a different ID, measured draw or excessive offer invalidates the prerequisites for the wait.
@pytest.mark.parametrize("activity", ["drawing", "drawing-after-native-close", "foreign-id", "offer-above-selected"])
async def test_natural_owner_stop_rejects_other_native_activity(tmp_path, tls, unused_tcp_port, monkeypatch, activity):
    async with overnight(tmp_path, tls, unused_tcp_port, monkeypatch) as (rig, clock):
        async with connection(rig, "contradictory-owner-stop") as test:
            c, bench = test.controller, rig.bench
            if activity == "offer-above-selected":
                gateway, j = test.session.gateway, test.session.journal
                j.put("managed_current_capability", capability_value(j, rig.spec))
                gateway.policy = replace(gateway.policy, max_current_a=32)
                gateway.controls.policy = gateway.policy
                rig.peer.configuration["AmpLmt"] = "32"
                rig.peer.runtime_user = 320
                assert c.current.selected() == 6
            local, meter_start = await begin(test, clock)
            clock.now = MIDNIGHT + 30
            await close(test, clock, local, meter_start)
            bench.firmware.active = local
            bench.firmware.requested_current = 0
            bench.cp, bench.connection = "B", "Plug"
            assert (await c.tick())["state"] == "reconciling"

            if activity == "foreign-id":
                bench.firmware.active = local + 1
            elif activity == "offer-above-selected":
                bench.firmware.active = None
                bench.cp, bench.connection = "A", "Unplug"
                monkeypatch.setattr(rig.peer, "native", report_offer(rig.peer, lambda: 7))
            else:
                if activity == "drawing-after-native-close":
                    bench.firmware.active = None
                    bench.cp, bench.connection = "A", "Unplug"
                    monkeypatch.setattr(rig.peer, "native", report_offer(rig.peer, lambda: 6))
                native = rig.peer.native

                # Report independent draw while the owner waits for native closure or zero.
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
