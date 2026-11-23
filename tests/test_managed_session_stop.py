# Modified for the standalone iCS proxy repository.
"""Use real management, charger and cloud sockets for a session-only Stop."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from websockets.asyncio.client import connect

from ics_proxy_core.engine.gateway import decode
from ics_proxy_core.engine.managed_api import ManagementServer, ManagementSettings
from ics_proxy_core.engine.managed_session_stop import ManagedSessionStop
from ics_proxy_core.engine.managed_view import refresh_for_read
from ics_proxy_core.frames import build_call_error, build_call_result
from tests.charge_rig import owner_rig, positive_flow
from tests.current_rig import adjustable, locally_ready, select
from tests.managed_rig import healthy
from tests.management_peer import TOKEN, call
from tests.prototype_rig import START
from tests.runtime_rig import until
from tests.test_managed_boot_supersession import delay_reply
from tests.test_managed_normal_start import natural_stop, zero_power_session
from tests.tls_support import tls as tls


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
        assert c.session_stop.worker is not None
        await asyncio.wait_for(c.session_stop.worker, 3)
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
        c.command(uuid.uuid4().hex, "start", c.requests.intent().revision)
        await c.charging.settle()
        local = await zero_power_session(t)
        await healthy(c)
        target, revision = c.session_stop.target(), c.requests.intent().revision
        stop, vendor = uuid.uuid4().hex, j.vendor_id(local)
        assert isinstance(target, str) and vendor is not None
        _, _, entered = delay_reply(monkeypatch, t.rig, t.charger, "RemoteStopTransaction")
        monkeypatch.setattr(t.session.gateway.charger, "timeout", 0.25)
        if first == "vendor":
            cloud = asyncio.create_task(
                bench.cloud_call("lost-cloud-stop", "RemoteStopTransaction", {"transactionId": vendor})
            )
            await asyncio.wait_for(entered.wait(), 3)
        assert c.command(stop, "stop_session", revision, target=target)["state"] == "accepted"
        await asyncio.wait_for(entered.wait(), 3)
        assert c.session_stop.worker is not None
        await asyncio.wait_for(c.session_stop.worker, 3)
        if first == "vendor":
            assert (await cloud)[0] == 4
            c.session_stop = ManagedSessionStop(c)
            assert c.command(stop, "stop_session", revision, target=target)["state"] == "uncertain"
        else:
            c.session_stop = ManagedSessionStop(c)
            assert c.command(stop, "stop_session", revision, target=target)["state"] == "uncertain"
            cloud = await bench.cloud_call("after-local-loss", "RemoteStopTransaction", {"transactionId": vendor})
            assert cloud[0] == 4
        assert sum(frame[2] == "RemoteStopTransaction" for frame in bench.device_calls) == 1
        assert c.requests.receipt(stop)["state"] != "completed"
        await natural_stop(t, local)
        if first == "vendor":
            status = await c.tick()
            assert status["operator_reconciliation_required"] is True
        else:
            await healthy(c)
        assert c.requests.receipt(stop)["state"] == "completed"


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
@pytest.mark.parametrize("first", ["local", "vendor"])
async def test_joined_session_stop_settles_from_late_rejection(tmp_path, tls, unused_tcp_port, monkeypatch, first):
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
        monkeypatch.setattr(t.session.gateway.charger, "timeout", 0.25)
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
            assert (await cloud)[0] == 4
        else:
            c.command(uuid.uuid4().hex, "stop_session", revision, target=target)
            await asyncio.wait_for(entered.wait(), 3)
            assert c.session_stop.worker is not None
            await asyncio.wait_for(c.session_stop.worker, 3)

        receipt = uuid.uuid4().hex
        assert c.command(receipt, "stop_session", revision, target=target)["state"] == "accepted"
        assert c.session_stop.worker is not None
        await asyncio.wait_for(c.session_stop.worker, 3)
        assert sum(frame[2] == "RemoteStopTransaction" for frame in bench.device_calls) == 1
        assert c.requests.receipt(receipt)["state"] == "running"
        await send(build_call_result(decode(held[0])[1], {"status": "Rejected"}))
        if first == "local":
            await until(lambda: not j.outcomes.session_stop_dispatched(local))
        else:
            await until(lambda: not j.outcomes.unanswered_vendor_stop(local))
        c.session_stop.expire()
        result = c.requests.receipt(receipt)
        assert result["state"] == "failed" and result["result"]["classification"] == "stop_rejected"
        assert j.active() == local and c.requests.intent().mode == "app"


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
        assert c.session_stop.worker is not None
        await asyncio.wait_for(c.session_stop.worker, 3)
        assert sum(frame[2] == "RemoteStopTransaction" for frame in bench.device_calls) == 1
        assert c.requests.receipt(receipt)["state"] == "running"
