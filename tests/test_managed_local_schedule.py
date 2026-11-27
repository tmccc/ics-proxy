"""Drive the proxy's opted-in overnight fallback over real loopback charger and native connections."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from ics_proxy_core.engine import local_schedule
from ics_proxy_core.engine.auth_info import tag_key
from ics_proxy_core.engine.managed import ManagedController
from ics_proxy_core.engine.managed_intent import AUTOMATIC_PAUSE_CAUSE, LOCAL_SCHEDULE_OCCURRENCE
from ics_proxy_core.engine.managed_view import refresh_for_read
from ics_proxy_core.engine.policy import DeniedError
from tests.current_rig import ChargeRig
from tests.handover_helpers import crash
from tests.managed_rig import healthy, introduced, managed_bench, managed_session, starts
from tests.native_schedule_support import ORIGINAL_PLAN, ZONE, SchedulePeer, SchedulerClock
from tests.prototype_rig import BOOT, START
from tests.runtime_rig import until
from tests.test_managed_offline_owner import OWNER_TAG
from tests.test_managed_vendor_catchup import CatchupBench
from tests.tls_support import tls as tls

NATIVE_TAG = ORIGINAL_PLAN["idTag"]
MIDNIGHT = datetime(2026, 11, 25, tzinfo=ZONE).timestamp()


# Replace the real proxy owner without changing either physical peer or the durable journal.
@asynccontextmanager
async def connection(rig, token, *, online=False):
    session = managed_session(rig)
    await session.start()
    if online:
        rig.route_to_trial()
    async with rig.charger(session) as charger:
        if online:
            await introduced(rig, session, charger, token)
        else:
            assert (await rig.bench.call(charger, token, "BootNotification", BOOT))[0] == 3
            await until(lambda: session.runtime.status.vendor_failed_charger_epoch == session.gateway._charger_epoch)
        controller = ManagedController(session)
        try:
            yield ChargeRig(rig, session, controller, charger)
        finally:
            await controller.current.close()
            await controller.charging.close()
            if controller.pause_task is not None:
                await asyncio.gather(controller.pause_task, return_exceptions=True)
            await crash(session)


# Poll the real controller while only its schedule's civil clock is simulated.
async def status(test, predicate):
    async with asyncio.timeout(6):
        while True:
            current = refresh_for_read(test.controller, await test.controller.tick())
            if predicate(current):
                return current
            assert current["state"] not in ("attention", "stopping"), current
            await asyncio.sleep(0.02)


def simulate_schedule_clock(monkeypatch):
    clock = SchedulerClock(MIDNIGHT - 60)
    monotonic = time.monotonic()
    monkeypatch.setattr(
        local_schedule, "time", SimpleNamespace(time=lambda: clock.now, monotonic=lambda: monotonic + clock.elapsed)
    )
    monkeypatch.setattr(local_schedule, "host_clock_synchronized", lambda: clock.synchronized)
    return clock


# Exercise a cold outage with an actual cached decision for the native timer identity.
@asynccontextmanager
async def overnight(tmp_path, tls, unused_tcp_port, monkeypatch, *, manual_pause=False, expired=True):
    clock = simulate_schedule_clock(monkeypatch)
    async with managed_bench(
        tmp_path, tls, unused_tcp_port, native_peer=SchedulePeer, bench_type=CatchupBench, time_zone=ZONE.key
    ) as rig:
        rig.prepared.config = replace(rig.prepared.config, owner_start_tag=OWNER_TAG, owner_schedule_timezone=ZONE.key)
        async with connection(rig, "online-introduction", online=True) as initial:
            c, j = initial.controller, initial.session.journal
            await healthy(c)
            expiry = (datetime.now(UTC) + timedelta(hours=-1 if expired else 1)).isoformat()
            j.db.execute(
                "INSERT OR REPLACE INTO auth_info(digest,info,observed) VALUES (?,?,?)",
                (tag_key(NATIVE_TAG), json.dumps({"status": "Accepted", "expiryDate": expiry}), time.time()),
            )
            assert initial.session.gateway.auth_info.get(NATIVE_TAG)["status"] == ("Expired" if expired else "Accepted")
            if manual_pause:
                c.command(uuid.uuid4().hex, "pause", c.requests.intent().revision)
                await status(initial, lambda value: value.get("local_hold_verified") is True)
            await rig.bench.vendor_outage()
        yield rig, clock


# Intent: a clean automatic outage Pause allows exactly one local session for the saved recurring plan.
# Why: neither stale approval nor a native retry with current approval can reopen the scheduled owner occurrence.
@pytest.mark.parametrize("expired", [True, False], ids=["expired-vendor-approval", "native-retry-with-cached-approval"])
async def test_offline_schedule_uses_exact_owner_session_once(tmp_path, tls, unused_tcp_port, monkeypatch, expired):
    async with overnight(tmp_path, tls, unused_tcp_port, monkeypatch, expired=expired) as (rig, clock):
        bench, peer = rig.bench, rig.peer
        async with connection(rig, "first-offline-boot") as test:
            c, j = test.controller, test.session.journal
            paused = await status(test, lambda value: value.get("local_hold_verified") is True)
            assert c.requests.offline_pause_current(c.requests.intent())
            assert paused["cloud_connected"] is False
            assert paused["station_ceiling_a"] == paused["power_w"] == 0
            assert not starts(rig) and j.get(LOCAL_SCHEDULE_OCCURRENCE) is None

            # Cross midnight and let the independent device answer the proxy's actual exact owner Start.
            clock.now = MIDNIGHT
            await status(test, lambda value: bool(starts(rig)))
            await c.charging.settle()
            assert [frame[3] for frame in starts(rig)] == [{"connectorId": 1, "idTag": OWNER_TAG}]
            assert test.session.gateway.auth_info.get(NATIVE_TAG)["status"] == ("Expired" if expired else "Accepted")
            invalid = await bench.call(test.charger, uuid.uuid4().hex, "Authorize", {"idTag": NATIVE_TAG})
            assert invalid[2]["idTagInfo"]["status"] == "Invalid"
            authorized = await bench.call(test.charger, uuid.uuid4().hex, "Authorize", {"idTag": OWNER_TAG})
            assert authorized[2]["idTagInfo"]["status"] == "Accepted"
            requested = START | {"idTag": OWNER_TAG, "timestamp": datetime.fromtimestamp(clock.now, UTC).isoformat()}
            started = await bench.call(test.charger, uuid.uuid4().hex, "StartTransaction", requested)
            assert started[0] == 3 and started[2]["idTagInfo"]["status"] == "Accepted", started
            local = started[2]["transactionId"]
            peer.plan_session = (local, 987654)
            peer.paused, peer.permission_enabled = False, True
            bench.cp, bench.connection = "C", "Charging"
            bench.firmware.requested_current = 32
            active = await status(test, lambda value: value.get("power_w") == 1440 and value.get("stop_target"))
            occurrence = j.get(LOCAL_SCHEDULE_OCCURRENCE)
            assert occurrence["local"] == local and j.is_owner_session(local)
            assert c.requests.receipt(occurrence["request"])["result"]["local_session_verified"] is True
            assert j.vendor_id(local) is None
            assert not j.db.execute("SELECT 1 FROM outbox WHERE local=?", (local,)).fetchone()

            # Stop the exact session early and wait for the separate real lifecycle and native closure.
            stop = uuid.uuid4().hex
            c.command(stop, "stop_session", active["control_revision"], target=active["stop_target"])
            await asyncio.wait_for(bench.remote_stop.wait(), 5)
            assert c.requests.receipt(stop)["state"] != "completed" and j.active() == local
            peer.permission_enabled = False
            clock.now = MIDNIGHT + 300
            stopped = {
                "transactionId": local,
                "timestamp": datetime.fromtimestamp(clock.now, UTC).isoformat(),
                "meterStop": requested["meterStart"] + 120,
            }
            assert (await bench.call(test.charger, uuid.uuid4().hex, "StopTransaction", stopped))[0] == 3
            await status(test, lambda value: c.requests.receipt(stop)["state"] == "completed")
            assert j.active() is None and len(starts(rig)) == 1
            if not expired:
                # A retry from the real plan cannot reopen this already used window, even with usable cache.
                invalid = await bench.call(test.charger, uuid.uuid4().hex, "Authorize", {"idTag": NATIVE_TAG})
                retry = await bench.call(
                    test.charger,
                    uuid.uuid4().hex,
                    "StartTransaction",
                    requested | {"idTag": NATIVE_TAG, "meterStart": stopped["meterStop"]},
                )
                assert invalid[2]["idTagInfo"]["status"] == "Invalid" and retry[0] == 4
                assert j.active() is None

        async with connection(rig, "second-offline-boot") as replacement:
            recovered = await status(replacement, lambda value: value.get("local_hold_verified") is True)
            assert recovered["cloud_connected"] is False and recovered["station_ceiling_a"] == 0
            assert replacement.controller.requests.offline_pause_current(replacement.controller.requests.intent())
            assert replacement.session.journal.get(LOCAL_SCHEDULE_OCCURRENCE)["local"] == local
            for _ in range(3):
                await replacement.controller.tick()
            assert len(starts(rig)) == 1 and replacement.session.journal.active() is None


async def test_offline_schedule_waits_for_host_clock_synchronization(tmp_path, tls, unused_tcp_port, monkeypatch):
    async with overnight(tmp_path, tls, unused_tcp_port, monkeypatch) as (rig, clock):
        async with connection(rig, "unsynchronized-offline-boot") as test:
            c, j = test.controller, test.session.journal
            await status(test, lambda value: value.get("local_hold_verified") is True)
            clock.synchronized = False
            clock.step_wall(MIDNIGHT)
            for _ in range(3):
                await c.tick()
            assert j.get(LOCAL_SCHEDULE_OCCURRENCE) is None and not starts(rig)
            assert rig.bench.firmware.effective_limit() == 0

            clock.synchronized = True
            await status(test, lambda _: bool(starts(rig)))
            await c.charging.settle()
            assert len(starts(rig)) == 1 and j.get(LOCAL_SCHEDULE_OCCURRENCE)["window"]["starts_at"] == MIDNIGHT


async def test_offline_schedule_rechecks_host_clock_at_start_wire(tmp_path, tls, unused_tcp_port, monkeypatch):
    async with overnight(tmp_path, tls, unused_tcp_port, monkeypatch) as (rig, clock):
        async with connection(rig, "clock-lost-before-start") as test:
            c, j, bench = test.controller, test.session.journal, rig.bench
            await status(test, lambda value: value.get("local_hold_verified") is True)

            # The charger applies the positive profile before answering. The actual Start is still unsent.
            async def lose_clock_after_profile(action, payload):
                if action == "SetChargingProfile":
                    schedule = payload["csChargingProfiles"]["chargingSchedule"]
                    if schedule["chargingSchedulePeriod"][0]["limit"] > 0:
                        clock.synchronized = False

            bench.before_reply = lose_clock_after_profile
            clock.now = MIDNIGHT
            await c.tick()
            await c.charging.settle()
            assert not clock.synchronized
            assert not starts(rig) and j.active() is None
            assert j.starts.receipt()["stage"] == "unsent"
            assert bench.firmware.effective_limit() == 0


# Intent: a completed same-tag run can use the fallback window even when no audit saw its native plan marker.
# Why: an outage must not restart the day's run; only an independently identified different source can leave it unused.
@pytest.mark.parametrize("restart", [False, True], ids=["same-process", "replacement"])
@pytest.mark.parametrize(
    ("source", "app_stop"),
    [("different", True), ("native", True), ("unobserved", True), ("unobserved", False)],
    ids=["different-plan-marker", "native-schedule", "unobserved-app-stop", "unobserved-natural-stop"],
)
async def test_native_daily_start_uses_the_outage_window(
    tmp_path, tls, unused_tcp_port, monkeypatch, restart, source, app_stop
):
    used_window = source != "different"
    clock = simulate_schedule_clock(monkeypatch)
    async with (
        managed_bench(
            tmp_path, tls, unused_tcp_port, native_peer=SchedulePeer, bench_type=CatchupBench, time_zone=ZONE.key
        ) as rig,
        AsyncExitStack() as connections,
    ):
        rig.prepared.config = replace(rig.prepared.config, owner_start_tag=OWNER_TAG, owner_schedule_timezone=ZONE.key)
        bench, peer = rig.bench, rig.peer
        test = await connections.enter_async_context(connection(rig, "online-for-native", online=True))
        c, j = test.controller, test.session.journal
        await healthy(c)
        clock.now = MIDNIGHT
        start = START | {"idTag": NATIVE_TAG, "timestamp": datetime.fromtimestamp(clock.now, UTC).isoformat()}
        reported = await bench.call(test.charger, uuid.uuid4().hex, "StartTransaction", start)
        assert reported[0] == 3 and reported[2]["idTagInfo"]["status"] == "Accepted"
        local = reported[2]["transactionId"]
        peer.plan_session = (local, ORIGINAL_PLAN["planId"] if used_window else 987654)
        peer.paused, peer.permission_enabled = False, True
        bench.cp, bench.connection = "C", "Charging"
        bench.firmware.requested_current = 32
        await until(lambda: j.vendor_id(local) is not None)
        if source == "unobserved":
            assert j.get(local_schedule.NATIVE_OCCURRENCE) is None
        else:
            assert (await healthy(c))["power_w"] == 1440
            witness = j.get(local_schedule.NATIVE_OCCURRENCE)
            if used_window:
                assert witness["local"] == local
            else:
                assert set(witness["unrelated"]) == {str(local)}
        assert j.get(LOCAL_SCHEDULE_OCCURRENCE) is None

        clock.now = MIDNIGHT + 60
        if app_stop:
            stopped = await bench.cloud_call(
                "vendor-app-stop", "RemoteStopTransaction", {"transactionId": j.vendor_id(local)}
            )
            assert stopped[0] == 3 and stopped[2]["status"] == "Accepted"
        peer.permission_enabled = False
        bench.cp, bench.connection = "B", "Plug"
        stop = {
            "transactionId": local,
            "meterStop": start["meterStart"],
            "timestamp": datetime.fromtimestamp(clock.now, UTC).isoformat(),
        }
        assert (await bench.call(test.charger, uuid.uuid4().hex, "StopTransaction", stop))[0] == 3
        bench.firmware.requested_current = 0
        await until(lambda: j.next_delivery() is None)
        await healthy(c)
        assert j.active() is None and not starts(rig)
        if source == "unobserved":
            assert j.get(local_schedule.NATIVE_OCCURRENCE) is None
        await bench.vendor_outage()
        if restart:
            await connections.aclose()
            test = await connections.enter_async_context(connection(rig, "offline-after-native"))
            c, j = test.controller, test.session.journal

        if used_window:
            ready = await status(
                test, lambda value: starts(rig) or (not value["cloud_connected"] and value.get("normal_start_ready"))
            )
            assert ready["normal_start_ready"] and not starts(rig)
            for _ in range(3):
                await c.tick()
            assert j.get(LOCAL_SCHEDULE_OCCURRENCE) is None
            assert not starts(rig)
            clock.now = MIDNIGHT + 24 * 3600

        await status(test, lambda _: bool(starts(rig)))
        occurrence = j.get(LOCAL_SCHEDULE_OCCURRENCE)
        assert occurrence["window"]["starts_at"] == MIDNIGHT + (24 * 3600 if used_window else 0)
        assert [frame[3] for frame in starts(rig)] == [{"connectorId": 1, "idTag": OWNER_TAG}]
        expected_stops = [{"transactionId": local}] if app_stop else []
        assert [frame[3] for frame in bench.device_calls if frame[2] == "RemoteStopTransaction"] == expected_stops


# Intent: the freshly rechecked connector briefly refuses a midnight Start before any command is attempted.
# Why: the proved-unsent request can retire while the same approved occurrence receives one fresh attempt.
async def test_offline_schedule_retries_an_unsent_native_preflight(tmp_path, tls, unused_tcp_port, monkeypatch):
    async with overnight(tmp_path, tls, unused_tcp_port, monkeypatch) as (rig, clock):
        async with connection(rig, "offline-retry-boot") as test:
            c, j = test.controller, test.session.journal
            await status(test, lambda value: value.get("local_hold_verified") is True)
            decisions = test.session.gateway.controls.decisions
            require_idle = decisions.require_idle_start
            refused_request = None

            # The later independent preflight can briefly see a different connector state from admission.
            def refuse_first_preflight(charging, lease, native):
                nonlocal refused_request
                if refused_request is None and lease.schedule is not None:
                    refused_request = lease.identifier
                    rig.bench.cp, rig.bench.connection = "A", "Unplug"
                    raise DeniedError("test requires a freshly connected idle vehicle")
                require_idle(charging, lease, native)

            monkeypatch.setattr(decisions, "require_idle_start", refuse_first_preflight)
            clock.now = MIDNIGHT + 3600
            refused = await status(
                test,
                lambda value: (
                    refused_request is not None
                    and c.requests.receipt(refused_request)["state"] == "failed"
                    and value.get("local_hold_verified") is True
                    and value.get("vehicle_connected") is False
                ),
            )
            assert c.requests.receipt(refused_request)["result"] == {"classification": "preflight_refused_unsent"}
            original = j.get(LOCAL_SCHEDULE_OCCURRENCE)
            assert original["request"] == refused_request and original["local"] is None
            assert refused["local_hold_verified"] and refused["station_ceiling_a"] == refused["power_w"] == 0
            assert not starts(rig)
            assert not j.db.execute(
                "SELECT 1 FROM owner_command_wires WHERE request=? AND action='RemoteStartTransaction'",
                (refused_request,),
            ).fetchone()

            rig.bench.cp, rig.bench.connection = "B", "Plug"
            clock.step_wall(MIDNIGHT + 20)
            await status(test, lambda value: bool(starts(rig)))
            await c.charging.settle()
            retry = j.get(LOCAL_SCHEDULE_OCCURRENCE)
            assert retry["request"] != refused_request and retry["window"] == original["window"]
            assert retry["cutoff_clock"] == original["cutoff_clock"]
            assert [frame[3] for frame in starts(rig)] == [{"connectorId": 1, "idTag": OWNER_TAG}]
            assert (
                j.db.execute(
                    "SELECT reply FROM owner_command_wires WHERE request=? AND action='RemoteStartTransaction'",
                    (retry["request"],),
                ).fetchone()[0]
                == "Accepted"
            )


# Intent: a time zone adjustment cannot spend overlapping bounds of the same plan a second time.
# Why: the earlier exact local Start remains authoritative after Stop and process replacement.
async def test_overlapping_timezone_change_does_not_repeat_local_occurrence(
    tmp_path, tls, unused_tcp_port, monkeypatch
):
    async with overnight(tmp_path, tls, unused_tcp_port, monkeypatch) as (rig, clock):
        bench = rig.bench
        async with connection(rig, "original-timezone") as test:
            c, j = test.controller, test.session.journal
            await status(test, lambda value: value.get("local_hold_verified") is True)
            clock.now = MIDNIGHT
            await status(test, lambda _: bool(starts(rig)))
            await c.charging.settle()
            requested = START | {"idTag": OWNER_TAG, "timestamp": datetime.fromtimestamp(clock.now, UTC).isoformat()}
            started = await bench.call(test.charger, uuid.uuid4().hex, "StartTransaction", requested)
            local = started[2]["transactionId"]
            clock.now = MIDNIGHT + 120
            stopped = {
                "transactionId": local,
                "meterStop": requested["meterStart"],
                "timestamp": datetime.fromtimestamp(clock.now, UTC).isoformat(),
            }
            assert (await bench.call(test.charger, uuid.uuid4().hex, "StopTransaction", stopped))[0] == 3
            await status(test, lambda _: j.starts.receipt().get("local") == local and j.active() is None)
            original = j.get(LOCAL_SCHEDULE_OCCURRENCE)
            assert original["local"] == local and original["window"]["starts_at"] == MIDNIGHT

        rig.prepared.config = replace(rig.prepared.config, owner_schedule_timezone="Atlantic/Azores")
        clock.now = MIDNIGHT + 2 * 3600
        async with connection(rig, "adjusted-timezone") as test:
            c, j = test.controller, test.session.journal
            await status(test, lambda value: value.get("normal_start_ready") is True)
            for _ in range(3):
                await c.tick()
            assert j.get(LOCAL_SCHEDULE_OCCURRENCE) == original
            assert len(starts(rig)) == 1 and j.active() is None

            clock.now += 24 * 3600
            await status(test, lambda _: len(starts(rig)) == 2)
            assert j.get(LOCAL_SCHEDULE_OCCURRENCE)["window"]["starts_at"] == MIDNIGHT + 25 * 3600


# Intent: return the verified hold to iCS if it reconnects before the scheduled Start is sent.
# Why: an unsent Start should permit normal app authority unless a newer explicit Pause supersedes it.
@pytest.mark.parametrize("explicit_pause", [False, True], ids=["automatic-return", "new-explicit-pause"])
@pytest.mark.parametrize(
    ("reply_limit", "flap"),
    [(None, False), (0, False), (6, False), (6, True)],
    ids=["initial-zero-confirmed", "zero-reply", "positive-reply", "vendor-lost-during-rollback"],
)
async def test_cloud_returns_before_local_schedule_sends_start(
    tmp_path, tls, unused_tcp_port, monkeypatch, explicit_pause, reply_limit, flap
):
    clock = simulate_schedule_clock(monkeypatch)
    async with managed_bench(
        tmp_path, tls, unused_tcp_port, native_peer=SchedulePeer, bench_type=CatchupBench, time_zone=ZONE.key
    ) as rig:
        rig.prepared.config = replace(rig.prepared.config, owner_start_tag=OWNER_TAG, owner_schedule_timezone=ZONE.key)
        async with connection(rig, "cloud-return-before-scheduled-start", online=True) as test:
            c, j, bench = test.controller, test.session.journal, rig.bench
            await healthy(c)
            await bench.vendor_outage()
            assert not test.session.runtime.vendor_registered
            await c.tick()
            assert c.requests.intent().mode == "app"
            controls = test.session.gateway.controls
            stop = controls.stop
            reached_boundary, release = asyncio.Event(), asyncio.Event()
            rollback_reached, rollback_release = asyncio.Event(), asyncio.Event()

            # Pause the scheduled worker after the initial zero is verified and before it can send Start.
            async def wait_after_initial_zero(**kwargs):
                result = await stop(**kwargs)
                lease = c.charging.lease
                if not reached_boundary.is_set() and lease is not None and lease.schedule is not None:
                    reached_boundary.set()
                    await release.wait()
                return result

            # The independent charger applies the profile before releasing its actual TLS reply.
            async def wait_for_profile_reply(action, payload):
                if action != "SetChargingProfile":
                    return
                schedule = payload["csChargingProfiles"]["chargingSchedule"]
                limit = schedule["chargingSchedulePeriod"][0]["limit"]
                if not reached_boundary.is_set() and limit == reply_limit:
                    reached_boundary.set()
                    await release.wait()
                elif flap and reached_boundary.is_set() and limit == 0 and not rollback_reached.is_set():
                    rollback_reached.set()
                    await rollback_release.wait()

            if reply_limit is None:
                monkeypatch.setattr(controls, "stop", wait_after_initial_zero)
            else:
                bench.before_reply = wait_for_profile_reply
            clock.now = MIDNIGHT
            await c.tick()
            try:
                await asyncio.wait_for(reached_boundary.wait(), 5)
                assert bench.firmware.effective_limit() == (reply_limit or 0)
                if reply_limit is None:
                    assert controls.ready
                assert not starts(rig)
                if explicit_pause:
                    pause = uuid.uuid4().hex
                    c.command(pause, "pause", c.requests.intent().revision)
                bench.vendor_available = True
                await until(lambda: test.session.runtime.vendor_registered, timeout=20)
                if flap:
                    release.set()
                    await asyncio.wait_for(rollback_reached.wait(), 5)
                    await bench.vendor_outage()
                    await until(lambda: not test.session.runtime.vendor_registered)
            finally:
                release.set()
                rollback_release.set()

            await c.charging.settle()
            if reply_limit == 6 and not explicit_pause:
                pending_intent = c.requests.intent()
                assert pending_intent.mode == "paused" and c.requests.offline_pause_current(pending_intent)
                assert j.get(AUTOMATIC_PAUSE_CAUSE) == {"revision": pending_intent.revision, "cause": "local_charge"}
            if flap:
                assert not test.session.runtime.vendor_registered
                bench.vendor_available = True
            expected = "paused" if explicit_pause else "app"
            result = await status(
                test,
                lambda value: (
                    value.get("cloud_connected") is True
                    and c.requests.intent().mode == expected
                    and value.get("station_ceiling_a") == (0 if explicit_pause else 6)
                ),
            )
            occurrence = j.get(LOCAL_SCHEDULE_OCCURRENCE)
            assert occurrence["local"] is None and not starts(rig) and j.active() is None
            assert c.charging.state()["start_attempted"] is (reply_limit == 6)
            assert c.charging.state()["start_rpc_accepted"] is False
            assert not any(
                wire.action == "RemoteStartTransaction"
                for wire in j.outcomes.owner_wires("request", occurrence["request"])
            )
            if reply_limit == 6:
                receipt = j.starts.receipt()
                assert receipt["request_id"] == occurrence["request"] and receipt["stage"] == "unsent"
            assert result["power_w"] == 0
            if explicit_pause:
                assert not c.requests.offline_pause_current(c.requests.intent())
                assert c.requests.receipt(pause)["state"] == "completed"


# Intent: pass through an eligible civil boundary without local authorization if the owner paused or it is too late.
# Why: schedule configuration cannot override the current manual hold or begin a session at the tariff cutoff.
@pytest.mark.parametrize(("manual_pause", "offset"), [(True, 0), (False, 6 * 3600)], ids=["manual-pause", "cutoff"])
async def test_offline_schedule_requires_current_permission_and_window(
    tmp_path, tls, unused_tcp_port, monkeypatch, manual_pause, offset
):
    async with overnight(tmp_path, tls, unused_tcp_port, monkeypatch, manual_pause=manual_pause) as (rig, clock):
        async with connection(rig, "offline-hold") as test:
            c, j = test.controller, test.session.journal
            await status(test, lambda value: value.get("local_hold_verified") is True)
            assert c.requests.offline_pause_current(c.requests.intent()) is not manual_pause
            clock.now = MIDNIGHT + offset
            for _ in range(3):
                current = await c.tick()
                assert current["local_hold_verified"] and current["station_ceiling_a"] == current["power_w"] == 0
            assert not starts(rig) and j.get(LOCAL_SCHEDULE_OCCURRENCE) is None
