# Modified for the standalone iCS proxy repository.
"""Use real management, charger and cloud sockets for a session-only Stop."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from contextlib import closing
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from websockets.asyncio.client import connect

from ics_proxy_core.engine import managed_session_stop, rpc
from ics_proxy_core.engine.command_outcomes import ForwardedReply, OwnerWire
from ics_proxy_core.engine.gateway import Gateway, decode
from ics_proxy_core.engine.managed import ManagedController
from ics_proxy_core.engine.managed_api import ManagementServer, ManagementSettings
from ics_proxy_core.engine.managed_intent import ManagedRequests, insert_request
from ics_proxy_core.engine.managed_session_stop import ManagedSessionStop
from ics_proxy_core.engine.managed_view import refresh_for_read
from ics_proxy_core.engine.security import SecurityError
from ics_proxy_core.engine.store import Journal, RecoveryRequiredError
from ics_proxy_core.frames import build_call_error, build_call_result
from tests.charge_rig import ChargeRig, owner_rig, positive_flow, report_actual_stop
from tests.current_rig import adjustable, locally_ready, select
from tests.handover_helpers import crash
from tests.managed_rig import healthy, introduced, managed_session
from tests.management_peer import TOKEN, call
from tests.native_peer_failures import InterruptedNativePeer
from tests.prototype_rig import BOOT, START
from tests.runtime_rig import until
from tests.service_recovery_helpers import running_service
from tests.test_managed_boot_supersession import delay_reply
from tests.test_managed_normal_start import natural_stop, zero_power_session
from tests.tls_support import tls as tls
from tools import managed_proxy
from tools.managed_incident import reference
from tools.managed_stop_incident import stop_incident_report


# Intent: time an outstanding Stop using actual elapsed time across clock corrections.
# Why: a clock correction could end the client's wait too early or keep it pending too long.
@pytest.mark.parametrize("wall_step", [-30, 30])
async def test_live_stop_confirmation_uses_elapsed_time(tmp_path, tls, unused_tcp_port, monkeypatch, wall_step):
    async with owner_rig(tmp_path, tls, unused_tcp_port, monkeypatch) as rig:
        c, j = rig.controller, rig.session.journal
        c.command(uuid.uuid4().hex, "start", c.requests.intent().revision)
        await c.charging.settle()
        local = await zero_power_session(rig)
        await healthy(c)
        identifier = uuid.uuid4().hex
        start = time.monotonic()
        c.command(identifier, "stop_session", c.requests.intent().revision, target=c.session_stop.target())
        await until(lambda: j.outcomes.accepted_stop(local))
        assert c.requests.receipt(identifier)["state"] == "running"
        clock = SimpleNamespace(time=lambda: time.time() + wall_step, monotonic=lambda: start + 1)
        with monkeypatch.context() as adjustment:
            adjustment.setattr(managed_session_stop, "time", clock)
            c.session_stop.expire()
            assert c.requests.receipt(identifier)["state"] == "running"
            clock.monotonic = lambda: start + managed_session_stop.CONFIRMATION_SECONDS + 1
            c.session_stop.expire()
            receipt = c.requests.receipt(identifier)
            assert receipt["state"] == "uncertain" and receipt["result"]["classification"] == "stop_not_confirmed"
        await natural_stop(rig, local)
        await healthy(c)
        receipt = c.requests.receipt(identifier)
        assert receipt["state"] == "completed" and "classification" not in receipt["result"]
        assert sum(frame[2] == "RemoteStopTransaction" for frame in rig.rig.bench.device_calls) == 1


# Intent: a zero-watt session can be stopped immediately after its Start completes on the management socket.
# Why: a charger reply or 0 W cannot prove closure, and this Stop must retain the app/schedule ceiling.
async def test_management_start_then_session_stop_at_zero_watts(tmp_path, tls, unused_tcp_port_factory, monkeypatch):
    async with owner_rig(tmp_path, tls, unused_tcp_port_factory(), monkeypatch) as t:
        c, j, bench = t.controller, t.session.journal, t.rig.bench
        port = unused_tcp_port_factory()
        published = await healthy(c)

        # Mirror the service's outer status fields without manufacturing native evidence.
        def status():
            return {**published, "running": True, "certificate_remaining_seconds": 86400}

        api = ManagementServer(ManagementSettings(port, tmp_path / "unused-secret", TOKEN), c, status)
        await api.start()
        try:
            async with connect(
                f"ws://127.0.0.1:{port}/management",
                additional_headers={"Authorization": "Bearer " + TOKEN},
                subprotocols=["ics-proxy.v1"],
            ) as socket:
                started = await call(socket, "start", revision=c.requests.intent().revision)
                await c.charging.settle()
                local = await zero_power_session(t)
                published = await healthy(c)
                view = (await call(socket, "status"))["result"]
                assert view["charging_session_open"] is True and view["power_w"] == 0
                assert (await call(socket, "receipt", request_id=started["id"]))["result"]["state"] == "completed"
                target, revision = view["stop_target"], view["control_revision"]
                assert isinstance(target, str)
                mark, limit = len(bench.device_calls), bench.firmware.effective_limit()
                reply = await call(socket, "stop_session", revision=revision, target="b" * 64)
                assert reply["ok"] is False and not bench.remote_stop.is_set()

                stop = uuid.uuid4().hex
                accepted = await call(socket, "stop_session", id=stop, revision=revision, target=target)
                assert accepted["result"]["state"] == "accepted"
                await asyncio.wait_for(bench.remote_stop.wait(), 3)
                await until(lambda: j.outcomes.accepted_stop(local))
                assert (await call(socket, "receipt", request_id=stop))["result"]["state"] != "completed"
                assert bench.firmware.effective_limit() == limit == 6
                assert (await call(socket, "stop_session", id=stop, revision=revision, target="b" * 64))["ok"] is False
                await natural_stop(t, local)
                published = await healthy(c)

                receipt = (await call(socket, "receipt", request_id=stop))["result"]
                assert receipt["state"] == "completed" and receipt["target"] == target
                assert c.requests.intent().mode == "app" and c.requests.intent().revision == revision
                assert published["charging_session_open"] is False and published["station_ceiling_a"] == limit
                assert (await call(socket, "stop_session", id=stop, revision=revision, target=target))["ok"]
                sent = [frame for frame in bench.device_calls[mark:] if frame[2] != "GetCompositeSchedule"]
                assert [(frame[2], frame[3]) for frame in sent] == [("RemoteStopTransaction", {"transactionId": local})]
        finally:
            await api.close()


# Intent: a new native refusal withdraws an ordinary Stop target at admission and after a wire wait.
# Why: an unchanged transaction ID cannot substitute for current verified charger evidence.
@pytest.mark.parametrize("queued", [False, True], ids=["before-admission", "at-dispatch"])
async def test_session_stop_rechecks_native_verification(tmp_path, tls, unused_tcp_port, monkeypatch, queued):
    async with owner_rig(tmp_path, tls, unused_tcp_port, monkeypatch) as t:
        c, j, bench = t.controller, t.session.journal, t.rig.bench
        local = await positive_flow(t, "daily")
        published = dict(await healthy(c), running=True, certificate_remaining_seconds=86400)
        view = refresh_for_read(c, published)
        target, revision = view["stop_target"], view["control_revision"]
        assert target is not None
        request = uuid.uuid4().hex

        with monkeypatch.context() as configuration:
            # Change only the independent charger reading, leaving the current Boot and ledger session intact.
            async def withdraw_verification():
                configuration.setitem(t.rig.peer.configuration, "maxOutCurrent0", "31")
                with pytest.raises(SecurityError):
                    await c.native.read()
                assert c.native.last_verified_at == 0
                assert refresh_for_read(c, published)["stop_target"] is None

            if queued:
                gate = t.session.gateway.charger.gate
                async with gate.enter(10):
                    assert c.command(request, "stop_session", revision, target=target)["state"] == "accepted"
                    await asyncio.wait_for(gate.restrictive_waiting.wait(), 3)
                    await withdraw_verification()
                assert c.session_stop.worker is not None
                await asyncio.wait_for(c.session_stop.worker, 3)
                assert not j.outcomes.session_stop_dispatched(local)
                receipt = c.requests.receipt(request)
                assert receipt["state"] == "failed" and receipt["result"]["classification"] == "stop_not_sent"
            else:
                await withdraw_verification()
                with pytest.raises(RecoveryRequiredError, match="current verified session"):
                    c.command(request, "stop_session", revision, target=target)
                assert c.requests.receipt(request)["state"] == "not_found"
            assert not bench.remote_stop.is_set()

        # A later valid auxiliary read of the same open session restores its current native proof.
        observed_at = c.latest_native.physical.observed_at
        await c.native.read()
        assert c.native.last_verified_at > observed_at
        assert c.command(uuid.uuid4().hex, "stop_session", revision, target=target)["state"] == "accepted"
        await until(lambda: j.outcomes.accepted_stop(local))
        await natural_stop(t, local)


# Intent: delayed receipt publication cannot let an expired Stop reach the charger.
# Why: a queued request's authority ends at its original elapsed deadline even after a storage failure.
async def test_queued_stop_cannot_dispatch_after_expiry_write_rolls_back(tmp_path, tls, unused_tcp_port, monkeypatch):
    async with owner_rig(tmp_path, tls, unused_tcp_port, monkeypatch) as t:
        c, j, bench = t.controller, t.session.journal, t.rig.bench
        local = await positive_flow(t, "daily")
        await healthy(c)
        target, revision = c.session_stop.target(), c.requests.intent().revision
        assert target is not None
        request = uuid.uuid4().hex
        gate = t.session.gateway.charger.gate
        async with gate.enter(10):
            assert c.command(request, "stop_session", revision, target=target)["state"] == "accepted"
            await asyncio.wait_for(gate.restrictive_waiting.wait(), 3)
            elapsed = time.monotonic() + managed_session_stop.CONFIRMATION_SECONDS + 1
            stopped_clock = SimpleNamespace(monotonic=lambda: elapsed, time=time.time)
            monkeypatch.setattr(managed_session_stop, "time", stopped_clock)
            j.db.execute(
                "CREATE TEMP TRIGGER refuse_stop_expiry BEFORE UPDATE ON managed_requests "
                f"WHEN NEW.id='{request}' AND NEW.state='failed' "
                "BEGIN SELECT RAISE(ABORT, 'temporary refusal'); END"
            )
            c.session_stop.expire()
            assert c.requests.receipt(request)["state"] == "accepted"
            j.db.execute("DROP TRIGGER refuse_stop_expiry")
            assert c.session_stop.target() == target

        assert c.session_stop.worker is not None
        await asyncio.wait_for(c.session_stop.worker, 3)
        assert not bench.remote_stop.is_set()
        assert not j.outcomes.session_stop_dispatched(local)
        receipt = c.requests.receipt(request)
        assert receipt["state"] == "failed" and receipt["result"]["classification"] == "stop_not_sent"


# Intent: the original Stop receipt can confirm when its exact session closes before the worker gets the wire.
# Why: a real report withdraws the open target but still needs fresh native proof before the receipt succeeds.
async def test_session_stop_awaits_closure_when_session_closes_in_gate(tmp_path, tls, unused_tcp_port, monkeypatch):
    async with owner_rig(tmp_path, tls, unused_tcp_port, monkeypatch) as t:
        c, j, bench = t.controller, t.session.journal, t.rig.bench
        local = await positive_flow(t, "daily")
        await healthy(c)
        target, intent = c.session_stop.target(), c.requests.intent()
        assert isinstance(target, str)
        request = uuid.uuid4().hex

        gate = t.session.gateway.charger.gate
        async with gate.enter(10):
            assert c.command(request, "stop_session", intent.revision, target=target)["state"] == "accepted"
            await asyncio.wait_for(gate.restrictive_waiting.wait(), 3)
            await natural_stop(t, local)
            assert c.session_stop.target() is None
        assert c.session_stop.worker is not None
        await asyncio.wait_for(c.session_stop.worker, 3)
        assert c.requests.receipt(request)["state"] == "accepted"
        assert not j.outcomes.session_stop_dispatched(local) and not bench.remote_stop.is_set()

        status = await healthy(c)
        receipt = c.requests.receipt(request, include_binding=True)
        assert receipt["state"] == "completed" and receipt["result"]["session_stopped"] is True
        binding = j.outcomes.owner(request).result["_session_stop"]
        assert binding["dispatched"] is False and "joined" not in binding and binding["closure_verified"] is True
        assert receipt["target"] == target and receipt["revision"] == intent.revision
        assert status["charging_session_open"] is False and c.requests.intent() == intent
        assert not bench.remote_stop.is_set()


# Intent: an identical Boot on the same connection withdraws the previous session's Stop target.
# Why: a newer auxiliary read may verify the session, but only supervision can publish a target for that Boot.
async def test_session_stop_target_awaits_current_boot_supervision(tmp_path, tls, unused_tcp_port, monkeypatch):
    async with owner_rig(tmp_path, tls, unused_tcp_port, monkeypatch) as t:
        c, j, bench = t.controller, t.session.journal, t.rig.bench
        local = await positive_flow(t, "daily")
        await healthy(c)
        previous = c.session_stop.target()
        assert previous is not None

        answer = await bench.call(t.charger, "same-socket-boot-before-stop", "BootNotification", BOOT)
        assert answer[0] == 3
        await c.native.read()
        assert c.native.session_stop_verified(local)
        assert c.session_stop.target() is None

        await healthy(c)
        target = c.session_stop.target()
        assert target is not None and target != previous
        revision = c.requests.intent().revision
        with pytest.raises(RecoveryRequiredError, match="current verified session"):
            c.command(uuid.uuid4().hex, "stop_session", revision, target=previous)
        assert c.command(uuid.uuid4().hex, "stop_session", revision, target=target)["state"] == "accepted"
        await until(lambda: j.outcomes.accepted_stop(local))
        await natural_stop(t, local)


# Intent: local and vendor Stop both reuse one physical Stop with a positive station ceiling.
# Why: the session identity is shared even though neither request intends to Pause scheduled charging.
@pytest.mark.parametrize("first", ["local", "vendor"])
@pytest.mark.parametrize("closure_first", ["native", "ocpp"])
async def test_session_stop_shares_the_vendor_wire_in_either_order(
    tmp_path, tls, unused_tcp_port, monkeypatch, first, closure_first
):
    async with owner_rig(tmp_path, tls, unused_tcp_port, monkeypatch) as t:
        c, j, bench = t.controller, t.session.journal, t.rig.bench
        c.command(uuid.uuid4().hex, "start", c.requests.intent().revision)
        await c.charging.settle()
        local = await positive_flow(t)
        status = await healthy(c)
        view = refresh_for_read(c, {**status, "running": True, "certificate_remaining_seconds": 86400})
        target = view["stop_target"]
        assert isinstance(target, str)
        stop, revision, vendor = uuid.uuid4().hex, view["control_revision"], j.vendor_id(local)
        assert vendor is not None

        # Send an independently correlated vendor Stop for the same current cloud mapping.
        async def vendor_stop():
            result = await bench.cloud_call(uuid.uuid4().hex, "RemoteStopTransaction", {"transactionId": vendor})
            assert result[0] == 3 and result[2] == {"status": "Accepted"}

        if first == "vendor":
            await vendor_stop()
        assert c.command(stop, "stop_session", revision, target=target)["state"] == "accepted"
        if first == "local":
            await until(lambda: j.outcomes.accepted_stop(local))
            await vendor_stop()
            await asyncio.wait_for(c.session_stop.worker, 3)
        else:
            assert c.session_stop.worker is None
        mark = len(bench.received_limits)
        if closure_first == "native":
            bench.firmware.active = None
            t.rig.peer.permission_enabled = False
        else:
            await natural_stop(t, local)
            # Keep the separately observed HTTP transaction open until the next poll.
            bench.firmware.active = local
        assert (await c.tick())["state"] != "attention"
        assert c.requests.receipt(stop)["state"] != "completed"
        assert bench.received_limits[mark:] == []
        if closure_first == "native":
            await natural_stop(t, local)
        else:
            bench.firmware.active = None
        await healthy(c)
        assert c.requests.receipt(stop)["state"] == "completed"
        assert sum(frame[2] == "RemoteStopTransaction" for frame in bench.device_calls) == 1
        assert bench.firmware.effective_limit() == 6 and c.requests.intent().mode == "app"


# Intent: a local session can physically close before its real Stop report arrives.
# Why: this is a temporary disagreement about the exact stopped session, not proof of a new identity fault.
@pytest.mark.parametrize("cloud_connected", [False, True])
async def test_session_stop_waits_for_report_after_native_closure(
    tmp_path, tls, unused_tcp_port, monkeypatch, cloud_connected
):
    async with adjustable(tmp_path, tls, unused_tcp_port, monkeypatch) as t:
        c, j, bench = t.controller, t.session.journal, t.rig.bench
        await select(t, 16)
        await healthy(c)
        await until(lambda: j.next_delivery() is None and not t.session.gateway.vendor._pending)
        if not cloud_connected:
            await t.session.runtime.stop_vendor()
        await locally_ready(c)
        c.command(uuid.uuid4().hex, "start", c.requests.intent().revision)
        await c.charging.settle()
        reply = await bench.call(
            t.charger, "unmapped-local-start", "StartTransaction", START | {"idTag": "NATIVE_TIMER"}
        )
        assert reply[2]["idTagInfo"]["status"] == "Accepted"
        local = reply[2]["transactionId"]
        t.rig.peer.plan_session = (local, 987654)
        await locally_ready(c)
        if cloud_connected:
            await until(lambda: j.vendor_id(local) is not None)
        else:
            assert j.vendor_id(local) is None
        target, revision = c.session_stop.target(), c.requests.intent().revision
        assert isinstance(target, str)
        stop = uuid.uuid4().hex
        assert c.command(stop, "stop_session", revision, target=target)["state"] == "accepted"
        await until(lambda: j.outcomes.accepted_stop(local))
        mark = len(bench.received_limits)

        bench.firmware.active = None
        t.rig.peer.permission_enabled = False
        for _ in range(4):
            assert (await c.tick())["state"] != "attention"
        assert c.requests.receipt(stop)["state"] != "completed" and j.active() == local
        reply = await bench.call(
            t.charger,
            "unmapped-local-stop",
            "StopTransaction",
            {
                "transactionId": local,
                "timestamp": START["timestamp"],
                "meterStop": 1100,
            },
        )
        assert reply[0] == 3
        final = await locally_ready(c)
        assert final["charging_session_open"] is False and c.requests.receipt(stop)["state"] == "completed"
        assert c.requests.intent().mode == "app" and bench.firmware.effective_limit() == 16
        assert bench.received_limits[mark:] == []
        assert sum(frame[2] == "RemoteStopTransaction" for frame in bench.device_calls) == 1


# Intent: a lost Stop reply leaves the exact session owned without a new Stop after replacement or a competing request.
# Why: the real closure, and not how the prior connection ended, settles a possibly sent session Stop.
@pytest.mark.parametrize("first", ["local", "vendor"])
async def test_session_stop_reuses_a_wire_with_a_lost_reply(tmp_path, tls, unused_tcp_port, monkeypatch, first):
    async with owner_rig(tmp_path, tls, unused_tcp_port, monkeypatch) as t:
        c, j, bench = t.controller, t.session.journal, t.rig.bench
        peer = t.session.gateway.charger
        c.command(uuid.uuid4().hex, "start", c.requests.intent().revision)
        await c.charging.settle()
        local = await zero_power_session(t)
        await healthy(c)
        target, revision = c.session_stop.target(), c.requests.intent().revision
        stop, vendor = uuid.uuid4().hex, j.vendor_id(local)
        assert isinstance(target, str) and vendor is not None
        _, held, entered = delay_reply(monkeypatch, t.rig, t.charger, "RemoteStopTransaction")
        monkeypatch.setattr(peer, "timeout", 0.25)
        monkeypatch.setattr(rpc, "LATE_REPLY_SECONDS", 1.5)
        if first == "vendor":
            cloud = asyncio.create_task(
                bench.cloud_call("lost-cloud-stop", "RemoteStopTransaction", {"transactionId": vendor})
            )
            await asyncio.wait_for(entered.wait(), 3)
        assert c.command(stop, "stop_session", revision, target=target)["state"] == "accepted"
        await asyncio.wait_for(entered.wait(), 3)
        if first == "vendor":
            assert c.session_stop.worker is None
            assert (await cloud)[0] == 4
            c.session_stop = ManagedSessionStop(c)
            assert c.command(stop, "stop_session", revision, target=target)["state"] == "uncertain"
        else:
            await asyncio.wait_for(c.session_stop.worker, 3)
            c.session_stop = ManagedSessionStop(c)
            assert c.command(stop, "stop_session", revision, target=target)["state"] == "uncertain"
            cloud = await bench.cloud_call("after-local-loss", "RemoteStopTransaction", {"transactionId": vendor})
            assert cloud[0] == 4
        assert sum(frame[2] == "RemoteStopTransaction" for frame in bench.device_calls) == 1
        assert c.requests.receipt(stop)["state"] != "completed"
        await natural_stop(t, local)
        await until(lambda: decode(held[0])[1] not in peer.reply_windows())
        if first == "vendor":
            status = await c.tick()
            assert status["operator_reconciliation_required"] is True
        else:
            await healthy(c)
        assert c.requests.receipt(stop)["state"] == "completed"


# Intent: record the Accepted predecessor used by a joined local Stop as its own incident evidence.
# Why: the joining request sent no RPC, but operators need to trace why it was safe to wait for closure.
@pytest.mark.parametrize("first", ["local", "vendor"])
async def test_joined_stop_report_retains_accepted_predecessor(tmp_path, tls, unused_tcp_port, monkeypatch, first):
    async with owner_rig(tmp_path, tls, unused_tcp_port, monkeypatch) as t:
        c, j, bench = t.controller, t.session.journal, t.rig.bench
        c.command(uuid.uuid4().hex, "start", c.requests.intent().revision)
        await c.charging.settle()
        local = await zero_power_session(t)
        await healthy(c)
        target, revision = c.session_stop.target(), c.requests.intent().revision
        assert isinstance(target, str)
        if first == "local":
            predecessor = uuid.uuid4().hex
            c.command(predecessor, "stop_session", revision, target=target)
            await c.session_stop.worker
        else:
            vendor = j.vendor_id(local)
            assert vendor is not None
            response = await bench.cloud_call("accepted-stop", "RemoteStopTransaction", {"transactionId": vendor})
            assert response[0] == 3 and response[2] == {"status": "Accepted"}
            predecessor = j.db.execute(
                "SELECT token FROM mutations WHERE origin='vendor' AND action='RemoteStopTransaction'"
            ).fetchone()[0]
        assert j.outcomes.accepted_stop(local)

        joined = uuid.uuid4().hex
        c.command(joined, "stop_session", revision, target=target)
        assert j.outcomes.owner(joined).result["_session_stop"]["accepted_attempt"]["identifier"] == predecessor
        await natural_stop(t, local)
        await healthy(c)
        report = stop_incident_report(j.path, tmp_path / "observations.jsonl", request_reference=reference(joined))
        assert report["journal"]["request"]["state"] == "completed"
        assert report["wire"] == {
            "dispatch": "joined",
            "reply": "unknown",
            "reply_source": None,
            "joined_attempts": [
                {
                    "origin": first,
                    "request": reference(predecessor),
                    "reply": "accepted",
                    "reply_source": "joined_receipt",
                }
            ],
        }
        assert sum(frame[2] == "RemoteStopTransaction" for frame in bench.device_calls) == 1


# Intent: an unplugged charger can report Stop before HTTP closes an offline session.
# Why: the exact dispatched Stop is known locally even when it has no cloud transaction number.
async def test_unplugged_offline_stop_waits_for_native_closure(tmp_path, tls, unused_tcp_port, monkeypatch):
    async with adjustable(tmp_path, tls, unused_tcp_port, monkeypatch) as t:
        c, j, bench = t.controller, t.session.journal, t.rig.bench
        await select(t, 16)
        await until(lambda: j.next_delivery() is None and not t.session.gateway.vendor._pending)
        await t.session.runtime.stop_vendor()
        await locally_ready(c)
        c.command(uuid.uuid4().hex, "start", c.requests.intent().revision)
        await c.charging.settle()
        result = await bench.call(t.charger, "offline-start", "StartTransaction", START | {"idTag": "NATIVE_TIMER"})
        assert result[2]["idTagInfo"]["status"] == "Accepted"
        local = result[2]["transactionId"]
        assert j.vendor_id(local) is None
        t.rig.peer.plan_session = (local, 987654)
        await locally_ready(c)
        target, intent = c.session_stop.target(), c.requests.intent()
        assert isinstance(target, str)

        receipt = uuid.uuid4().hex
        c.command(receipt, "stop_session", intent.revision, target=target)
        await until(lambda: j.outcomes.accepted_stop(local))
        mark = len(bench.received_limits)
        bench.cp, bench.connection = "A", "Unplug"
        t.rig.peer.permission_enabled = False
        stop = {"transactionId": local, "timestamp": START["timestamp"], "meterStop": 1100}
        reply = await bench.call(t.charger, "offline-stop", "StopTransaction", stop)
        assert reply[0] == 3 and j.active() is None
        # The HTTP session endpoint has not caught up with the independent OCPP report.
        bench.firmware.active = local
        for _ in range(4):
            assert (await c.tick())["state"] != "attention"
        assert not j.fault.latched() and bench.received_limits[mark:] == []
        assert c.requests.receipt(receipt)["state"] != "completed"

        bench.firmware.active = None
        await locally_ready(c)
        assert c.requests.receipt(receipt)["state"] == "completed"
        assert c.requests.intent() == intent and bench.firmware.effective_limit() == 16
        assert bench.received_limits[mark:] == []
        assert sum(frame[2] == "RemoteStopTransaction" for frame in bench.device_calls) == 1


# Intent: a Stop that joined an earlier send can learn the earlier command's exact late refusal.
# Why: a known rejection must clear the original waiting receipt without sending a duplicate Stop.
@pytest.mark.parametrize(
    ("first", "in_flight"),
    [("local", False), ("vendor", False), ("local", True), ("vendor", True)],
    ids=["local", "vendor", "local-wire-busy", "vendor-wire-busy"],
)
@pytest.mark.parametrize("closure_first", [True, False], ids=["closure-first", "rejection-first"])
async def test_joined_session_stop_settles_from_late_rejection(
    tmp_path, tls, unused_tcp_port, monkeypatch, first, in_flight, closure_first
):
    async with owner_rig(tmp_path, tls, unused_tcp_port, monkeypatch) as t:
        c, j, bench = t.controller, t.session.journal, t.rig.bench
        c.command(uuid.uuid4().hex, "start", c.requests.intent().revision)
        await c.charging.settle()
        local = await zero_power_session(t)
        await healthy(c)
        target, revision = c.session_stop.target(), c.requests.intent().revision
        assert isinstance(target, str)
        vendor = j.vendor_id(local)
        assert vendor is not None
        send, held, entered = delay_reply(monkeypatch, t.rig, t.charger, "RemoteStopTransaction")
        peer = t.session.gateway.charger
        monkeypatch.setattr(peer, "timeout", 5 if in_flight else 0.25)
        direct = None
        if first == "vendor":
            cloud = asyncio.create_task(
                bench.cloud_call(
                    "rejected-stop",
                    "RemoteStopTransaction",
                    {
                        "transactionId": vendor,
                    },
                )
            )
            await asyncio.wait_for(entered.wait(), 3)
            if not in_flight:
                assert (await cloud)[0] == 4
        else:
            direct = uuid.uuid4().hex
            c.command(direct, "stop_session", revision, target=target)
            await asyncio.wait_for(entered.wait(), 3)
            if not in_flight:
                await asyncio.wait_for(c.session_stop.worker, 3)

        previous_worker = c.session_stop.worker
        wire = decode(held[0])[1]
        assert peer.has_pending_call(wire) is in_flight
        receipt = uuid.uuid4().hex
        assert c.command(receipt, "stop_session", revision, target=target)["state"] == "accepted"
        assert c.session_stop.worker is previous_worker
        assert sum(frame[2] == "RemoteStopTransaction" for frame in bench.device_calls) == 1
        assert c.requests.receipt(receipt)["state"] == "running"
        binding = j.outcomes.owner(receipt).result["_session_stop"]
        if first == "vendor":
            assert tuple(binding["vendor_attempts"]) == j.outcomes.vendor_stop_dispatches(local)
            assert len(binding["vendor_attempts"]) == 1
        else:
            assert binding["local_attempts"] == [direct]
        if closure_first:
            await natural_stop(t, local)
            await c.session_stop.confirm_while_blocked()
            assert wire in peer.reply_windows()
            assert j.outcomes.session(local).closed
            assert c.requests.receipt(receipt)["state"] == "running"
            assert j.outcomes.owner(receipt).result["_session_stop"]["closure_verified"] is True
            if direct is not None:
                assert c.requests.receipt(direct)["state"] == "running"
        await send(build_call_result(wire, {"status": "Rejected"}))
        if first == "local":
            await until(lambda: not j.outcomes.session_stop_dispatched(local))
        else:
            await until(lambda: not j.outcomes.unanswered_vendor_stop(local))
            if in_flight:
                assert (await cloud)[2] == {"status": "Rejected"}
        c.session_stop.expire()
        if not closure_first:
            await natural_stop(t, local)
        await c.tick()
        result = c.requests.receipt(receipt)
        assert result["state"] == "failed" and result["result"]["classification"] == "stop_rejected"
        if direct is not None:
            original = c.requests.receipt(direct)
            assert original["state"] == "failed" and original["result"]["classification"] == "stop_rejected"
        assert j.outcomes.session(local).closed and c.requests.intent().mode == "app"
        assert sum(frame[2] == "RemoteStopTransaction" for frame in bench.device_calls) == 1


# Intent: settle a verified closure when its original live reply is accepted, rejected, or can no longer arrive.
# Why: a completed receipt must remain stable and should not depend on a second local reading after closure.
@pytest.mark.parametrize("outcome", ["Accepted", "Rejected", "expired", "disconnected"])
async def test_verified_closure_survives_stop_reply_window(tmp_path, tls, unused_tcp_port, monkeypatch, outcome):
    async with owner_rig(tmp_path, tls, unused_tcp_port, monkeypatch, native_peer=InterruptedNativePeer) as t:
        c, j = t.controller, t.session.journal
        c.command(uuid.uuid4().hex, "start", c.requests.intent().revision)
        await c.charging.settle()
        local = await zero_power_session(t)
        await healthy(c)
        peer = t.session.gateway.charger
        if outcome == "expired":
            monkeypatch.setattr(peer, "timeout", 0.75)
            monkeypatch.setattr(rpc, "LATE_REPLY_SECONDS", 0.75)
        send, held, entered = delay_reply(monkeypatch, t.rig, t.charger, "RemoteStopTransaction")
        request = uuid.uuid4().hex
        c.command(request, "stop_session", c.requests.intent().revision, target=c.session_stop.target())
        await asyncio.wait_for(entered.wait(), 3)
        wire = decode(held[0])[1]
        await natural_stop(t, local)
        assert (await healthy(c))["charging_session_open"] is False
        assert wire in peer.reply_windows()
        assert c.requests.receipt(request)["state"] == "running"
        assert j.outcomes.owner(request).result["_session_stop"]["closure_verified"] is True

        t.rig.peer.unavailable = True
        try:
            if outcome == "disconnected":
                await t.charger.close()
            elif outcome in ("Accepted", "Rejected"):
                await send(build_call_result(wire, {"status": outcome}))
            await until(lambda: wire not in peer.reply_windows())
            await c.tick()
            receipt = c.requests.receipt(request)
            if outcome == "Rejected":
                assert receipt["state"] == "failed" and receipt["result"]["classification"] == "stop_rejected"
            else:
                assert receipt["state"] == "completed" and receipt["result"]["session_stopped"] is True
            if outcome in ("Accepted", "expired"):
                await send(build_call_result(wire, {"status": "Rejected"}))
                await c.tick()
                assert c.requests.receipt(request) == receipt
        finally:
            t.rig.peer.unavailable = False


# Intent: keep independent closure evidence when SQLite temporarily refuses to publish the successful receipt.
# Why: after an Accepted reply retires the wire, readings can become unavailable before publication retries.
@pytest.mark.parametrize("recovery", ["next-poll", "marker-refused", "replacement"])
async def test_accepted_stop_closure_survives_publication_refusal(
    tmp_path, tls, unused_tcp_port, monkeypatch, recovery
):
    async with owner_rig(tmp_path, tls, unused_tcp_port, monkeypatch, native_peer=InterruptedNativePeer) as t:
        c, j, bench = t.controller, t.session.journal, t.rig.bench
        c.command(uuid.uuid4().hex, "start", c.requests.intent().revision)
        await c.charging.settle()
        local = await zero_power_session(t)
        await healthy(c)
        request = uuid.uuid4().hex
        c.command(request, "stop_session", c.requests.intent().revision, target=c.session_stop.target())
        await until(lambda: j.outcomes.accepted_stop(local))
        await c.session_stop.worker
        assert not t.session.gateway.charger.reply_windows()

        blocked_update = "NEW.state='completed'"
        if recovery == "marker-refused":
            blocked_update += " OR json_type(NEW.result,'$._session_stop.closure_verified')='true'"
        j.db.execute(
            "CREATE TEMP TRIGGER refuse_stop_publication BEFORE UPDATE ON managed_requests "
            f"WHEN NEW.id='{request}' AND ({blocked_update}) "
            "BEGIN SELECT RAISE(ABORT, 'Stop receipt unavailable'); END"
        )
        try:
            await natural_stop(t, local)
            assert (await healthy(c))["charging_session_open"] is False
            assert j.outcomes.session(local).closed
            receipt = j.outcomes.owner(request)
            assert receipt.state == "running"
            assert receipt.result["_session_stop"].get("closure_verified") is (
                None if recovery == "marker-refused" else True
            )
        finally:
            j.db.execute("DROP TRIGGER refuse_stop_publication")

        assert sum(frame[2] == "RemoteStopTransaction" for frame in bench.device_calls) == 1
        if recovery == "replacement":
            path, identity = j.path, j.get("identity")
            await crash(t.session)
        else:
            t.rig.peer.unavailable = True
            try:
                with monkeypatch.context() as adjustment:
                    deadline = time.monotonic() + managed_session_stop.CONFIRMATION_SECONDS + 1
                    adjustment.setattr(
                        managed_session_stop, "time", SimpleNamespace(time=time.time, monotonic=lambda: deadline)
                    )
                    await c.tick()
                assert not t.rig.peer.failed_reads.empty()
                saved = c.requests.receipt(request)
                assert saved["state"] == "completed"
                assert saved["result"]["session_stopped"] is True
                assert "classification" not in saved["result"]
            finally:
                t.rig.peer.unavailable = False
    if recovery == "replacement":
        with closing(Journal(path, identity)) as journal:
            requests = ManagedRequests(journal)
            gateway = Gateway(journal)
            ManagedSessionStop(SimpleNamespace(session=SimpleNamespace(journal=journal, gateway=gateway)))
            saved = requests.receipt(request)
            assert saved["state"] == "completed"
            assert saved["result"]["session_stopped"] is True
            assert "classification" not in saved["result"]


# Intent: keep a real late Stop rejection authoritative while SQLite refuses that wire reply.
# Why: expiry and a later independent closure must preserve the original and joined request outcomes.
@pytest.mark.parametrize("first", ["local", "vendor"])
async def test_buffered_late_stop_rejection_fences_receipts(tmp_path, tls, unused_tcp_port, monkeypatch, first):
    async with owner_rig(tmp_path, tls, unused_tcp_port, monkeypatch) as t:
        c, j, bench = t.controller, t.session.journal, t.rig.bench
        c.command(uuid.uuid4().hex, "start", c.requests.intent().revision)
        await c.charging.settle()
        local = await zero_power_session(t)
        await healthy(c)
        target, revision = c.session_stop.target(), c.requests.intent().revision
        assert isinstance(target, str)
        send, held, entered = delay_reply(monkeypatch, t.rig, t.charger, "RemoteStopTransaction")
        monkeypatch.setattr(t.session.gateway.charger, "timeout", 0.2)
        original, joined = uuid.uuid4().hex, uuid.uuid4().hex
        expected_stops = [local]
        if first == "local":
            assert c.command(original, "stop_session", revision, target=target)["state"] == "accepted"
            await asyncio.wait_for(entered.wait(), 3)
            await asyncio.wait_for(c.session_stop.worker, 3)
            affected = (original, joined)
            reject_write = "BEFORE INSERT ON owner_command_wires WHEN NEW.reply='Rejected'"
        else:
            cloud = asyncio.create_task(
                bench.cloud_call(original, "RemoteStopTransaction", {"transactionId": j.vendor_id(local)})
            )
            await asyncio.wait_for(entered.wait(), 3)
            assert (await cloud)[0] == 4
            (mutation,) = j.outcomes.vendor_stop_dispatches(local)
            affected = (joined,)
            reject_write = "BEFORE UPDATE OF wire_result ON vendor_command_scope WHEN NEW.wire_result='Rejected'"
        assert c.command(joined, "stop_session", revision, target=target)["state"] == "accepted"
        assert all(c.requests.receipt(request)["state"] == "running" for request in affected)

        refused = asyncio.Event()
        j.db.create_function("note_stop_reply_refusal", 0, refused.set)
        j.db.execute(
            f"CREATE TEMP TRIGGER refuse_stop_reply {reject_write} "
            "BEGIN SELECT note_stop_reply_refusal(); SELECT RAISE(ABORT, 'Stop reply unavailable'); END"
        )
        try:
            await send(build_call_result(decode(held[0])[1], {"status": "Rejected"}))
            await asyncio.wait_for(refused.wait(), 3)
            assert not j.db.in_transaction
            if first == "local":
                (wire,) = j.outcomes.owner_wires("request", original)
                assert wire.reply == "Rejected" and wire.local == local
            else:
                command = j.outcomes.vendor(mutation)
                assert command.status == "Rejected" and command.wire_result == "dispatched"

            j.db.execute(
                "CREATE TEMP TRIGGER refuse_rejected_receipt BEFORE UPDATE ON managed_requests "
                "WHEN NEW.action='stop_session' AND NEW.state='failed' "
                "BEGIN SELECT RAISE(ABORT, 'Stop receipt unavailable'); END"
            )
            try:
                await natural_stop(t, local)
                c.session_stop.observed(await c.native.read(stop_only=True))
                expired = time.monotonic() + managed_session_stop.CONFIRMATION_SECONDS + 1
                with monkeypatch.context() as clock:
                    clock.setattr(
                        managed_session_stop, "time", SimpleNamespace(time=time.time, monotonic=lambda: expired)
                    )
                    c.session_stop.expire()
                assert all(c.requests.receipt(request)["state"] == "running" for request in affected)
            finally:
                j.db.execute("DROP TRIGGER refuse_rejected_receipt")

            c.session_stop.expire()
            assert all(c.requests.receipt(request)["state"] == "failed" for request in affected)
            if first == "local":
                later = await positive_flow(t, "daily", call_id=uuid.uuid4().hex)
                expected_stops.append(later)
                await healthy(c)
                later_target = c.session_stop.target()
                assert later_target is not None and later_target != target
                unrelated = uuid.uuid4().hex
                assert c.command(unrelated, "stop_session", revision, target=later_target)["state"] == "accepted"
                await until(lambda: j.outcomes.accepted_stop(later))
                await report_actual_stop(t, later)
                await healthy(c)
                assert c.requests.receipt(unrelated)["state"] == "completed"
        finally:
            j.db.execute("DROP TRIGGER refuse_stop_reply")

        await healthy(c)
        for request in affected:
            receipt = c.requests.receipt(request, include_binding=True)
            assert receipt["state"] == "failed" and receipt["result"]["classification"] == "stop_rejected"
            assert receipt["target"] == target and receipt["revision"] == revision
        assert j.active() is None and c.requests.intent().mode == "app"
        assert [frame[3]["transactionId"] for frame in bench.device_calls if frame[2] == "RemoteStopTransaction"] == (
            expected_stops
        )


# Intent: retain an exact Stop rejection across process replacement and a refused receipt write.
# Why: physical closure must not complete the rejected request while the original wire record awaits storage.
@pytest.mark.parametrize("source", ["owner", "vendor"])
def test_exact_stop_rejection_fences_receipt_after_replacement(tmp_path, source):
    path = tmp_path / "stop-journal.sqlite"
    with closing(Journal(path, "synthetic-charger")) as journal:
        requests = ManagedRequests(journal)
        stop = ManagedSessionStop(SimpleNamespace(session=SimpleNamespace(journal=journal)))
        journal.put("managed_mode", {"mode": "app_schedule"})
        local = journal.start("start-report", START)["transactionId"]
        answer = [3, "stop-wire", {"status": "Rejected"}]
        if source == "owner":
            binding = {"local": local, "dispatched": True}
            wire = OwnerWire("stop-wire", "stop-request", "stop-request", "boot", "RemoteStopTransaction", local, None)
            journal.outcomes.record_owner_wire(wire)
            refusal = "BEFORE INSERT ON owner_command_wires WHEN NEW.reply='Rejected'"
        else:
            action = "RemoteStopTransaction"
            token = journal.mutation_intent("vendor", action, {"transactionId": 42})
            journal.bind_vendor_command(
                token, request="vendor-stop", vendor_epoch=1, boot="boot", wire="stop-wire", local=local, profile=None
            )
            journal.record_vendor_result(token, "dispatched")
            binding = {"local": local, "joined": True, "local_attempts": [], "vendor_attempts": [token]}
            refusal = "BEFORE UPDATE OF wire_result ON vendor_command_scope WHEN NEW.wire_result='Rejected'"
        insert_request(journal, "stop-request", "stop_session", 0, {"_session_stop": binding}, state="running")
        journal.db.execute(
            f"CREATE TEMP TRIGGER refuse_primary_reply {refusal} BEGIN SELECT RAISE(ABORT, 'reply unavailable'); END"
        )
        journal.db.execute(
            "CREATE TRIGGER refuse_stop_receipt BEFORE UPDATE ON managed_requests WHEN NEW.state='failed' "
            "BEGIN SELECT RAISE(ABORT, 'receipt unavailable'); END"
        )
        if source == "owner":
            journal.outcomes.record_owner_reply(wire, answer)
            assert not journal.outcomes.session_stop_dispatched(local)
        else:
            reply = journal.outcomes.capture(token, action, "stop-wire", answer)
            assert reply is not None and not journal.outcomes.publish(reply)
            assert not journal.outcomes.unanswered_vendor_stop(local)
        journal.stop("stop-report", {"transactionId": local, "meterStop": 1200, "timestamp": "2026-01-01T00:01:00Z"})
        journal.db.execute(
            "UPDATE managed_requests SET result=json_set(result,'$._session_stop.closure_verified',json('true'))"
        )
        stop.expire()
        assert requests.receipt("stop-request")["state"] == "running"

    with closing(Journal(path, "synthetic-charger")) as journal:
        requests = ManagedRequests(journal)
        stop = ManagedSessionStop(SimpleNamespace(session=SimpleNamespace(journal=journal)))
        assert requests.receipt("stop-request")["state"] == "running"
        journal.db.execute("DROP TRIGGER refuse_stop_receipt")
        stop.expire()
        receipt = requests.receipt("stop-request")
        assert receipt["state"] == "failed"
        assert receipt["result"]["classification"] == "stop_rejected"
        assert journal.outcomes.session(local).closed


# Intent: a replacement can finish a previously verified closure without reopening its retired wire window.
# Why: legacy receipts with only an OCPP closing report still need independent evidence.
@pytest.mark.parametrize("verified", [False, True], ids=["old-receipt", "verified-closure"])
def test_replacement_settles_only_saved_verified_stop_closure(tmp_path, verified):
    path = tmp_path / "verified-closure.sqlite"
    with closing(Journal(path, "synthetic-charger")) as journal:
        ManagedRequests(journal)
        local = journal.start("start-report", START)["transactionId"]
        journal.stop("stop-report", {"transactionId": local, "meterStop": 1200, "timestamp": "2026-01-01T00:01:00Z"})
        binding = {"local": local, "dispatched": True}
        if verified:
            binding["closure_verified"] = True
        insert_request(journal, "stop-request", "stop_session", 0, {"_session_stop": binding}, state="running")

    with closing(Journal(path, "synthetic-charger")) as journal:
        requests = ManagedRequests(journal)
        gateway = Gateway(journal)
        ManagedSessionStop(SimpleNamespace(session=SimpleNamespace(journal=journal, gateway=gateway)))
        receipt = requests.receipt("stop-request")
        assert receipt["state"] == ("completed" if verified else "uncertain")
        assert receipt["result"].get("session_stopped") is (True if verified else None)


# Intent: existing successful Stop receipts may still carry an earlier progress classification on disk.
# Why: both the management API and incident report should show the final result while preserving journal history.
@pytest.mark.parametrize("classification", ["stop_not_confirmed", "process_replaced"])
def test_legacy_completed_stop_receipt_projects_success(tmp_path, classification):
    with closing(Journal(tmp_path / "legacy-closure.sqlite", "synthetic-charger")) as journal:
        requests = ManagedRequests(journal)
        request = uuid.uuid4().hex
        insert_request(
            journal,
            request,
            "stop_session",
            0,
            {"classification": classification, "session_stopped": True},
            state="completed",
        )
        receipt = requests.receipt(request)
        assert receipt["state"] == "completed" and receipt["result"] == {"session_stopped": True}
        report = stop_incident_report(
            journal.path, tmp_path / "observations.jsonl", request_reference=reference(request)
        )
        assert report["journal"]["request"]["classification"] is None
        assert report["journal"]["request"]["session_stopped"] is True
        assert journal.outcomes.owner(request).result["classification"] == classification


# Intent: restart with damaged fallback evidence while preserving a separate valid Stop reply.
# Why: a malformed or mismatched reply may neither settle its original outcome nor block the journal from opening.
@pytest.mark.parametrize("source", ["owner", "vendor"])
@pytest.mark.parametrize("damage", ["json", "encoding", "extra", "duplicate", "key", "binding", "status", "surrogate"])
def test_invalid_retained_reply_is_preserved_without_blocking_restart(tmp_path, caplog, source, damage):
    path = tmp_path / "stop-journal.sqlite"
    action = "RemoteStopTransaction"
    with closing(Journal(path, "synthetic-charger")) as journal:
        local = journal.start("start-report", START)["transactionId"]

        # Build the retained reply for a known local or vendor attempt before applying the selected damage.
        def checkpoint(wire, local):
            if source == "owner":
                sent = OwnerWire(wire, wire, wire, "boot", action, local, None)
                if damage != "surrogate" or wire != "damaged-wire":
                    journal.outcomes.record_owner_wire(sent)
                return asdict(sent) | {"reply": "Accepted"}
            token = journal.mutation_intent("vendor", action, {"transactionId": local})
            journal.bind_vendor_command(
                token, request=wire, vendor_epoch=1, boot="boot", wire=wire, local=local, profile=None
            )
            journal.record_vendor_result(token, "dispatched")
            return asdict(ForwardedReply(token, action, wire, "Accepted"))

        key = f"retained_stop_{source}:damaged-wire"
        saved = checkpoint("damaged-wire", local)
        status = "reply" if source == "owner" else "status"
        binding = "ticket" if source == "owner" else "mutation"
        payload = (
            b"\xff"
            if damage == "encoding"
            else {
                "json": "{",
                "extra": json.dumps(saved | {"extra": True}),
                "duplicate": json.dumps(saved)[:-1] + f',"{status}":"Accepted"}}',
                "key": json.dumps(saved | {"wire": "other-wire"}),
                "binding": json.dumps(saved | {binding: "other-binding"}),
                "status": json.dumps(saved | {status: "Rejected"}),
                "surrogate": json.dumps(saved | {"request" if source == "owner" else "mutation": "\ud800"}),
            }[damage].encode()
        )
        journal.db.execute("INSERT INTO meta VALUES(?,CAST(? AS TEXT))", (key, payload))
        if source == "owner" and damage == "surrogate":
            ManagedRequests(journal)
        journal.stop("first-stop", {"transactionId": local, "meterStop": 1200, "timestamp": "2026-01-01T00:01:00Z"})
        next_local = journal.start("next-start", START | {"timestamp": "2026-01-01T00:02:00Z"})["transactionId"]
        journal.put(f"retained_stop_{source}:valid-wire", checkpoint("valid-wire", next_local))

    with closing(Journal(path, "synthetic-charger")) as journal:
        journal.outcomes.retry_owner_writes()
        if source == "owner":
            assert all(wire.reply is None for wire in journal.outcomes.owner_wires("ticket", "damaged-wire"))
            assert journal.outcomes.owner_wires("ticket", "valid-wire")[0].reply == "Accepted"
        else:
            assert journal.outcomes.vendor(saved["mutation"]).status is None
            assert len(journal.outcomes.vendor_evidence().uncertain) == 1
        assert not journal.outcomes.accepted_stop(local)
        assert journal.outcomes.accepted_stop(next_local)
        assert journal.active() == next_local
        assert journal.db.execute("SELECT CAST(value AS BLOB) FROM meta WHERE key=?", (key,)).fetchone()[0] == payload
        assert journal.get(f"retained_stop_{source}:valid-wire") is None
    assert caplog.text.count(f"Invalid retained {source} transaction reply checkpoint") == 1


# Intent: require the retained Stop reply to match its one possible dispatch, even without a primary wire.
# Why: a damaged request link or multiple possible replies must leave the original attempt unresolved.
@pytest.mark.parametrize("damage", ["none", "missing_request", "different_request", "different_wire", "other_primary"])
def test_retained_session_stop_without_primary_requires_its_exact_claim(tmp_path, damage):
    path = tmp_path / "stop-journal.sqlite"
    request, wire, action = "stop-request", "stop-wire", "RemoteStopTransaction"
    with closing(Journal(path, "synthetic-charger")) as journal:
        ManagedRequests(journal)
        journal.put("managed_mode", {"mode": "app_schedule"})
        local = journal.start("start-report", START)["transactionId"]
        insert_request(
            journal,
            request,
            "stop_session",
            0,
            {"_session_stop": {"local": local, "dispatched": True}},
            state="running",
        )
        saved = asdict(OwnerWire(wire, request, request, "boot", action, local, None, "Accepted"))
        if damage == "missing_request":
            saved["request"] = None
        elif damage == "different_request":
            saved["request"] = "another-request"
        elif damage == "different_wire":
            journal.put(
                "retained_transaction_reply_owner:another-wire", saved | {"wire": "another-wire", "reply": "Rejected"}
            )
        elif damage == "other_primary":
            journal.outcomes.record_owner_wire(OwnerWire("another-wire", request, request, "boot", action, local, None))
        journal.put(f"retained_stop_owner:{wire}", saved)

    with closing(Journal(path, "synthetic-charger")) as journal:
        assert journal.outcomes.session_stop_dispatched(local)
        assert journal.outcomes.accepted_stop(local) is (damage == "none")
        journal.outcomes.retry_owner_writes()
        assert journal.outcomes.accepted_stop(local) is (damage == "none")
        assert journal.active() == local


# Intent: keep definite protective Stop replies even when neither primary wire write succeeded.
# Why: Pause and shutdown have distinct ticket and dispatch bindings, including older shutdown records.
@pytest.mark.parametrize("operation", ["pause", "shutdown", "legacy_shutdown"])
def test_retained_protective_stop_keeps_its_original_binding(tmp_path, operation):
    path = tmp_path / "stop-journal.sqlite"
    ticket, boot, wire = "stop-ticket", "boot", "stop-wire"
    key = f"retained_stop_owner:{wire}"
    with closing(Journal(path, "synthetic-charger")) as journal:
        local = journal.start("start-report", START)["transactionId"]
        binding = {"ticket": ticket, "boot": boot, "local": local}
        request = None
        if operation == "pause":
            ManagedRequests(journal)
            request = "pause-request"
            insert_request(journal, request, "pause", 0, {"_stop": binding}, state="running")
        else:
            if operation == "shutdown":
                binding["stop_wire"] = wire
            journal.put("trial_shutdown_attempt", binding)
        reply = OwnerWire(wire, request, ticket, boot, "RemoteStopTransaction", local, None, "Accepted")
        journal.put(key, asdict(reply))

    with closing(Journal(path, "synthetic-charger")) as journal:
        assert journal.outcomes.accepted_stop(local, ticket=ticket)
        journal.outcomes.retry_owner_writes()
        assert journal.outcomes.owner_wires("ticket", ticket) == (reply,)
        assert journal.get(key) is None


# Intent: leave an orphaned reply in the journal while sending a real Stop for the same open session.
# Why: a checkpoint without a dispatch claim cannot cover a subsequent request.
async def test_orphan_retained_stop_does_not_cover_next_request(tmp_path, tls, unused_tcp_port, monkeypatch, caplog):
    async with owner_rig(tmp_path, tls, unused_tcp_port, monkeypatch) as t:
        j, rig = t.session.journal, t.rig
        local = await positive_flow(t, "daily")
        orphan, wire = uuid.uuid4().hex, uuid.uuid4().hex
        key = f"retained_stop_owner:{wire}"
        saved = asdict(OwnerWire(wire, orphan, orphan, "old-boot", "RemoteStopTransaction", local, None, "Accepted"))
        j.put(key, saved)
        await t.controller.charging.close()
        await crash(t.session)
        caplog.clear()

        session = managed_session(rig)
        j, bench = session.journal, rig.bench
        assert j.active() == local
        j.outcomes.retry_owner_writes()
        assert not j.outcomes.accepted_stop(local)
        await session.start()
        c = ManagedController(session)
        try:
            async with rig.charger(session) as charger:
                await introduced(rig, session, charger, "replacement-stop-boot")
                await healthy(c)
                target, revision = c.session_stop.target(), c.requests.intent().revision
                assert target is not None
                request = uuid.uuid4().hex
                assert c.command(request, "stop_session", revision, target=target)["state"] == "accepted"
                await asyncio.wait_for(bench.remote_stop.wait(), 3)
                await until(lambda: j.outcomes.accepted_stop(local))
                attempt = j.outcomes.accepted_stop_attempt(local)
                assert attempt is not None and attempt.identifier == request
                await natural_stop(ChargeRig(rig, session, c, charger), local)
                await healthy(c)
                assert c.requests.receipt(request)["state"] == "completed"
            stops = [frame[3]["transactionId"] for frame in bench.device_calls if frame[2] == "RemoteStopTransaction"]
            assert stops == [local]
            assert j.get(key) == saved
            assert not j.outcomes.owner_wires("request", orphan)
            assert caplog.text.count("Invalid retained owner transaction reply checkpoint") == 1
        finally:
            await c.charging.close()
            await session.close()


# Intent: the service retries publication of the same closed Stop receipt after SQLite rolls back its write.
# Why: failure to publish an already verified closure must leave charger supervision and app mode running.
async def test_service_retries_closed_stop_receipt_publication(tmp_path, tls, unused_tcp_port_factory, monkeypatch):
    monkeypatch.setattr(managed_proxy, "_SUPERVISION_POLL_SECONDS", 0.2)
    async with running_service(tmp_path, tls, unused_tcp_port_factory, monkeypatch, 6) as service:
        charge = service.charge
        journal, bench = charge.session.journal, charge.rig.bench
        local = await positive_flow(charge, "daily", call_id=uuid.uuid4().hex)
        before = await service.published(state="healthy", power=True)
        view = (await call(service.api, "status"))["result"]
        target, revision = view["stop_target"], view["control_revision"]
        assert isinstance(target, str)
        request = uuid.uuid4().hex
        accepted = await call(service.api, "stop_session", id=request, revision=revision, target=target)
        assert accepted["result"]["state"] == "accepted"
        await until(lambda: journal.outcomes.accepted_stop(local))

        refused = asyncio.Event()
        journal.db.create_function("note_stop_receipt_attempt", 0, refused.set)
        journal.db.execute(
            "CREATE TEMP TRIGGER refuse_closed_stop_receipt BEFORE UPDATE ON managed_requests "
            f"WHEN NEW.id='{request}' AND NEW.state='completed' "
            "BEGIN SELECT note_stop_receipt_attempt(); SELECT RAISE(ABORT, 'receipt storage unavailable'); END"
        )
        try:
            bench.firmware.active = None
            await service.close_daily(local)
            await asyncio.wait_for(refused.wait(), 5)
            await service.published(before["checked_at"], state="healthy")
            pending = (await call(service.api, "receipt", request_id=request))["result"]
            assert pending["state"] == "running" and pending["target"] == target
            assert journal.active() is None and not journal.db.in_transaction
        finally:
            if not service.task.done():
                journal.db.execute("DROP TRIGGER refuse_closed_stop_receipt")

        await until(lambda: charge.controller.requests.receipt(request)["state"] == "completed")
        receipt = (await call(service.api, "receipt", request_id=request))["result"]
        assert receipt["id"] == request and receipt["target"] == target and receipt["revision"] == revision
        assert receipt["result"]["session_stopped"] is True
        final = (await call(service.api, "status"))["result"]
        assert final["charging_session_open"] is False and final["desired_control"] == "app"
        assert final["station_ceiling_a"] == 6 and final["control_revision"] == revision
        assert [frame[3] for frame in bench.device_calls if frame[2] == "RemoteStopTransaction"] == [
            {"transactionId": local}
        ]


# Intent: a rolled-back timeout or rejection receipt must leave service supervision running.
# Why: neither expiry nor a genuine closure can overtake a deferred, already known rejection.
@pytest.mark.parametrize("outcome", ["expiry", "rejection", "joined-rejection"])
async def test_service_retries_session_stop_receipt_updates(
    tmp_path, tls, unused_tcp_port_factory, monkeypatch, outcome
):
    monkeypatch.setattr(managed_proxy, "_SUPERVISION_POLL_SECONDS", 0.05)
    monkeypatch.setattr(managed_session_stop, "CONFIRMATION_SECONDS", 1)
    if outcome == "expiry":
        monkeypatch.setattr(rpc, "LATE_REPLY_SECONDS", 0.5)
    async with running_service(tmp_path, tls, unused_tcp_port_factory, monkeypatch, 6) as service:
        charge = service.charge
        controller, journal, bench = charge.controller, charge.session.journal, charge.rig.bench
        local = await positive_flow(charge, "daily")
        await until(lambda: controller.session_stop.target() is not None)
        target = controller.session_stop.target()
        revision = controller.requests.intent().revision
        dispatched = uuid.uuid4().hex
        request = uuid.uuid4().hex if outcome == "joined-rejection" else dispatched
        send, held, entered = delay_reply(monkeypatch, charge.rig, charge.charger, "RemoteStopTransaction")
        monkeypatch.setattr(charge.session.gateway.charger, "timeout", 0.2)

        refused = asyncio.Event()
        journal.db.create_function("note_stop_receipt_refusal", 0, refused.set)
        expected_state = "uncertain" if outcome == "expiry" else "failed"
        journal.db.execute(
            "CREATE TEMP TRIGGER refuse_stop_receipt_update BEFORE UPDATE ON managed_requests "
            f"WHEN NEW.id='{request}' AND NEW.state='{expected_state}' "
            "BEGIN SELECT note_stop_receipt_refusal(); SELECT RAISE(ABORT, 'receipt storage unavailable'); END"
        )
        try:
            accepted = await call(service.api, "stop_session", id=dispatched, revision=revision, target=target)
            assert accepted["result"]["state"] == "accepted"
            await asyncio.wait_for(entered.wait(), 3)
            await asyncio.wait_for(controller.session_stop.worker, 3)
            if request != dispatched:
                joined = await call(service.api, "stop_session", id=request, revision=revision, target=target)
                assert joined["result"]["state"] == "accepted"
            if outcome != "expiry":
                await send(build_call_result(decode(held[0])[1], {"status": "Rejected"}))

            await asyncio.wait_for(refused.wait(), 5)
            before = await service.published()
            await service.published(before["checked_at"], state="healthy", desired="app")
            assert controller.requests.receipt(request)["state"] == "running"
            assert not journal.db.in_transaction
            if outcome == "expiry":
                assert journal.outcomes.session_stop_dispatches(local) == (dispatched,)
            else:
                # Leave the refusal in place until both closure evidence and the expiry deadline are available.
                await service.close_daily(local)
                await controller.session_stop.confirm_while_blocked()
                assert controller.requests.receipt(request)["state"] == "running"
                await asyncio.sleep(managed_session_stop.CONFIRMATION_SECONDS)
                before = await service.published()
                closed = await service.published(before["checked_at"], state="healthy", desired="app")
                assert closed["charging_session_open"] is False
                assert controller.requests.receipt(request)["state"] == "running"
        finally:
            if not service.task.done():
                journal.db.execute("DROP TRIGGER refuse_stop_receipt_update")

        await until(lambda: controller.requests.receipt(request)["state"] == expected_state)
        saved = (await call(service.api, "receipt", request_id=request))["result"]
        assert saved["id"] == request and saved["target"] == target and saved["revision"] == revision
        if outcome == "expiry":
            assert saved["result"]["classification"] == "stop_not_confirmed"
            assert journal.outcomes.session_stop_dispatches(local) == (dispatched,)
            await service.close_daily(local)
            await until(lambda: controller.requests.receipt(request)["state"] == "completed")
            assert "classification" not in controller.requests.receipt(request)["result"]
        else:
            assert saved["result"]["classification"] == "stop_rejected"
        assert [frame[3] for frame in bench.device_calls if frame[2] == "RemoteStopTransaction"] == [
            {"transactionId": local}
        ]


# Intent: a vendor Stop whose device response was an error still covers the same possible physical effect.
# Why: a response error does not prove the original Stop had no effect while the charger report is pending.
async def test_session_stop_does_not_repeat_vendor_stop_with_a_call_error(tmp_path, tls, unused_tcp_port, monkeypatch):
    async with owner_rig(tmp_path, tls, unused_tcp_port, monkeypatch) as t:
        c, j, bench = t.controller, t.session.journal, t.rig.bench
        c.command(uuid.uuid4().hex, "start", c.requests.intent().revision)
        await c.charging.settle()
        local = await zero_power_session(t)
        await healthy(c)
        target, revision = c.session_stop.target(), c.requests.intent().revision
        assert isinstance(target, str)
        vendor = j.vendor_id(local)
        assert vendor is not None
        send, held, entered = delay_reply(monkeypatch, t.rig, t.charger, "RemoteStopTransaction")
        task = asyncio.create_task(bench.cloud_call("stop-error", "RemoteStopTransaction", {"transactionId": vendor}))
        await asyncio.wait_for(entered.wait(), 3)
        await send(build_call_error(decode(held[0])[1], "InternalError", "synthetic response error"))
        assert (await task)[0] == 4
        assert j.outcomes.unanswered_vendor_stop(local) and j.active() == local
        receipt = uuid.uuid4().hex
        assert c.command(receipt, "stop_session", revision, target=target)["state"] == "accepted"
        assert c.session_stop.worker is None
        assert sum(frame[2] == "RemoteStopTransaction" for frame in bench.device_calls) == 1
        assert c.requests.receipt(receipt)["state"] == "running"
