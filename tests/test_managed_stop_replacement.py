# Modified for the standalone iCS proxy repository.
"""A genuine rejected Stop remains independently recoverable across every fault-latch acquisition boundary."""

import asyncio
import copy
import sqlite3
import uuid

import pytest

from ics_proxy_core.engine.gateway import decode
from ics_proxy_core.engine.managed import ManagedController
from ics_proxy_core.engine.native_bounded import NativeReadinessError
from ics_proxy_core.engine.native_bounded_handover import NativeSessionPendingError
from ics_proxy_core.engine.recovery_contracts import Pending, Prerequisite
from ics_proxy_core.engine.store import RecoveryRequiredError
from ics_proxy_core.frames import build_call_error
from tests import managed_rig as mp
from tests.charge_rig import ChargeRig, positive_flow
from tests.current_rig import adjustable, locally_ready, select
from tests.handover_helpers import crash
from tests.managed_rig import ReplyBench
from tests.model_tls_support import listener_port, tls_for_case
from tests.model_tls_support import model_tls as model_tls
from tests.prototype_rig import BOOT, START
from tests.runtime_rig import until
from tests.test_managed_stop_reply_recovery import pause, reconcile, return_after_ack, running


# Intent: restart while native status still shows the exact closed session from a Stop during a vendor outage.
# Why: recovery must keep the listener available at zero while refusing an unrelated session or missing Stop proof.
@pytest.mark.parametrize("connection", [("B", "Plug"), ("A", "Unplug")], ids=["plugged", "unplugged"])
@pytest.mark.parametrize("accepted", [True, False], ids=["exact-stop-proof", "report-only"])
async def test_unmapped_stop_replacement_waits_for_native_closure(
    tmp_path, model_tls, monkeypatch, connection, accepted
):
    async with adjustable(tmp_path, tls_for_case(tmp_path, model_tls), listener_port(), monkeypatch) as t:
        c, j, bench = t.controller, t.session.journal, t.rig.bench
        await select(t, 16)
        await until(lambda: j.next_delivery() is None and not t.session.gateway.vendor._pending)
        await t.session.runtime.stop_vendor()
        await locally_ready(c)
        c.command(uuid.uuid4().hex, "start", c.requests.intent().revision)
        await c.charging.settle()
        start = await bench.call(t.charger, "offline-start", "StartTransaction", START | {"idTag": "NATIVE_TIMER"})
        assert start[2]["idTagInfo"]["status"] == "Accepted"
        local = start[2]["transactionId"]
        assert j.vendor_id(local) is None
        t.rig.peer.plan_session = (local, 987654)
        await locally_ready(c)
        request = uuid.uuid4().hex
        if accepted:
            c.command(request, "stop_session", c.requests.intent().revision, target=c.session_stop.target())
            await until(lambda: j.outcomes.accepted_stop(local))
            await c.session_stop.worker
        bench.cp, bench.connection = connection
        t.rig.peer.permission_enabled = False
        stop = {"transactionId": local, "timestamp": START["timestamp"], "meterStop": 1100}
        assert (await bench.call(t.charger, "offline-stop", "StopTransaction", stop))[0] == 3
        assert j.active() is None and j.outcomes.session(local).closed
        bench.firmware.active = local
        if accepted:
            assert (await c.tick())["state"] != "attention"
            assert not j.fault.latched()
        await crash(t.session)

        replacement = mp.managed_session(t.rig)
        try:
            journal = replacement.journal
            assert journal.vendor_id(local) is None and journal.outcomes.session(local).closed
            assert journal.outcomes.accepted_stop(local) is accepted
            refusal = NativeReadinessError if bench.connection == "Unplug" else NativeSessionPendingError
            bench.firmware.active = local + 1
            with pytest.raises(refusal):
                await replacement.start()
            assert replacement.runtime._server is None
            bench.firmware.active = local
            if not accepted:
                with pytest.raises(refusal):
                    await replacement.start()
                assert replacement.runtime._server is None
                # The schema marker can survive an interrupted older service setup before requests are initialized.
                journal.db.execute("DROP TABLE managed_requests")
                with pytest.raises(refusal):
                    await replacement.start()
                assert replacement.runtime._server is None
                bench.firmware.active = None
                await replacement.start()
                assert replacement.started
                return

            if bench.connection == "Plug":
                t.rig.peer.permission_enabled = True
                with pytest.raises(NativeSessionPendingError):
                    await replacement.start()
                assert replacement.runtime._server is None
                t.rig.peer.permission_enabled = False

            limits_before = len(bench.received_limits)
            calls_before = len(bench.device_calls)
            vendor_enabled = replacement.runtime.vendor_enabled
            replacement.runtime.vendor_enabled = lambda: False
            await replacement.start()
            assert replacement.started and not replacement.gateway.controls.ready
            controller = ManagedController(replacement)
            async with t.rig.charger(replacement) as charger:
                assert (await bench.call(charger, "replacement-boot", "BootNotification", BOOT))[0] == 3
                await until(lambda: replacement.gateway.controls.ready)
                assert journal.vendor_id(local) is None
                native = await replacement.native_bounded.read_result()
                if bench.connection == "Unplug":
                    assert isinstance(native, Pending) and native.reason is Prerequisite.LIFECYCLE
                else:
                    assert not isinstance(native, Pending) and controller.session_stop.closure_pending(native)
                replacement.runtime.vendor_enabled = vendor_enabled
                await until(lambda: replacement.gateway.vendor_registration == "Accepted")
                for _ in range(4):
                    assert (await controller.tick())["state"] == "reconciling"
                assert journal.active() is None and not journal.fault.latched()
                assert controller.requests.receipt(request)["state"] == "uncertain"
                assert bench.firmware.effective_limit() == 0
                assert bench.received_limits[limits_before:] and set(bench.received_limits[limits_before:]) == {0}
                assert not any(
                    call[2] in {"RemoteStartTransaction", "RemoteStopTransaction"}
                    for call in bench.device_calls[calls_before:]
                )
                bench.firmware.active = None
                await mp.healthy(controller)
                assert controller.requests.receipt(request)["state"] == "completed"
        finally:
            await crash(replacement)


# Intent: a crash before the first fault-publishing tick must not remove the exact retained Stop's recovery route.
# Why: actual closed local Starts and ordinary timetables need the same durable delivery evidence after replacement.
@pytest.mark.parametrize("origin", ["daily", "local", "local-unobserved"])
@pytest.mark.parametrize("boundary", ["before-tick", "after-tick", "after-pause"])
@pytest.mark.parametrize("reply", ["invalid-info", "call-error"])
async def test_stop_rejection_replacement_retains_recovery(
    tmp_path, model_tls, monkeypatch, capsys, origin, boundary, reply
):
    async with mp.managed_bench(
        tmp_path, tls_for_case(tmp_path, model_tls), listener_port(), bench_type=ReplyBench
    ) as rig:
        async with running(rig, "stop-before-crash", owner_tag="APP_OWNER") as (s, c, sock):
            await mp.healthy(c)
            if origin.startswith("local"):
                c.command(uuid.uuid4().hex, "start", c.requests.intent().revision)
                await c.charging.settle()
            local = await positive_flow(ChargeRig(rig, s, c, sock), "local" if origin.startswith("local") else "daily")
            if origin.startswith("local"):
                assert s.journal.get("local_start_receipt")["local"] == local
                assert c.charging.state()["local_transaction"] == local
            if origin != "local-unobserved":
                await mp.healthy(c)
            else:
                rig.peer.permission_enabled = False
                rig.bench.firmware.requested_current = 0
            rig.bench.stop_info = {"status": "invalid"} if reply == "invalid-info" else None
            rig.bench.stop_error = reply == "call-error"
            await rig.bench.call(
                sock,
                "autonomous-stop",
                "StopTransaction",
                {"transactionId": local, "timestamp": START["timestamp"], "meterStop": 1100},
            )
            rig.peer.permission_enabled = False
            await until(lambda: s.runtime.status.delivery_blocked)
            if boundary == "after-tick":
                await c.tick()
            elif boundary == "after-pause":
                await pause(c)
        async with running(rig, "stop-after-crash") as (s, c, sock):
            await until(lambda: s.runtime.status.delivery_blocked)
            await pause(c)
            assert rig.bench.firmware.effective_limit() == 0 and s.journal.active() is None
        await reconcile(rig, tmp_path, capsys)
        rig.bench.stop_error, rig.bench.stop_info = False, None
        async with running(rig, "stop-after-proof") as (s, c, sock):
            await return_after_ack(rig, s, c, expected_starts=int(origin.startswith("local")))


# Intent: an exact genuinely closed bound session can settle locally while preserving the truthful RPC outcome.
# Why: unbound unknown commands retain debt, while a real owned Start/Stop and fresh Pause prove local completion.
@pytest.mark.parametrize("answer", ["accepted", "unknown"])
@pytest.mark.parametrize("replace_process", [False, True])
async def test_bound_lifecycle_before_start_success_publication(
    tmp_path, model_tls, monkeypatch, answer, replace_process
):
    async with mp.managed_bench(tmp_path, tls_for_case(tmp_path, model_tls), listener_port()) as rig:
        identifier, retained = uuid.uuid4().hex, []
        async with running(rig, "held-start-reply", owner_tag="APP_OWNER") as (s, c, sock):
            await mp.healthy(c)
            send = sock.send

            # Hold only the independent charger's actual RemoteStart reply after applying its command.
            async def hold_start(raw):
                frame = decode(raw)
                call = next((row for row in rig.bench.device_calls if row[1] == frame[1]), None)
                if frame[0] == 3 and call is not None and call[2] == "RemoteStartTransaction":
                    retained.append(raw)
                else:
                    await send(raw)

            monkeypatch.setattr(sock, "send", hold_start)
            c.command(identifier, "start", c.requests.intent().revision)
            await until(lambda: bool(retained))
            local = await positive_flow(ChargeRig(rig, s, c, sock))
            assert s.journal.get("local_start_receipt")["local"] == local
            assert c.charging.state()["local_transaction"] == local
            rig.peer.permission_enabled, rig.bench.firmware.requested_current = False, 0
            await rig.bench.call(
                sock,
                "closed-before-start-ack",
                "StopTransaction",
                {"transactionId": local, "timestamp": START["timestamp"], "meterStop": 1100},
            )
            await until(lambda: s.journal.next_delivery() is None)
            c.command(uuid.uuid4().hex, "pause", c.requests.intent().revision)
            raw = retained[0]
            if answer == "unknown":
                raw = build_call_error(decode(raw)[1], "InternalError", "synthetic uncertain Start reply")
            await send(raw)
            await c.charging.settle()
            if c.pause_task is not None:
                await c.pause_task
            assert c.charging.state()["start_rpc_accepted"] is False
            assert s.journal.get("local_start_receipt")["stage"] == ("accepted" if answer == "accepted" else "issued")
            if not replace_process:
                await verify_bound_result(rig, s, c, identifier, answer)
        if replace_process:
            async with running(rig, "replaced-held-start") as (s, c, sock):
                await verify_bound_result(rig, s, c, identifier, answer)


# Require the exact bound lifecycle at zero, preserving an issued unknown reply rather than inventing acceptance.
async def verify_bound_result(rig, s, c, identifier, answer):
    await pause(c)
    for _ in range(4):
        await c.tick()
        await asyncio.sleep(0)
    assert rig.bench.firmware.effective_limit() == 0 and len(mp.starts(rig)) == 1
    assert c.charging.state()["state"] == "completed"
    assert c.requests.receipt(identifier)["state"] == "completed"
    assert c.charging.state()["start_rpc_accepted"] is False
    assert s.journal.get("local_start_receipt")["stage"] == ("accepted" if answer == "accepted" else "issued")
    c.command(uuid.uuid4().hex, "app_control", c.requests.intent().revision)
    assert (await mp.healthy(c))["normal_start_ready"]


# Intent: journal failure commits neither half of exact closed-session recovery, and a later retry completes both.
# Why: a recovered command receipt without its matching lifecycle state, or the reverse, can strand later recovery.
@pytest.mark.parametrize("boundary", ["request", "metadata"])
async def test_closed_session_retirement_is_atomic(tmp_path, model_tls, monkeypatch, boundary):
    async with mp.managed_bench(tmp_path, tls_for_case(tmp_path, model_tls), listener_port()) as rig:
        async with running(rig, "closed-atomic-prefix", owner_tag="APP_OWNER") as (s, c, sock):
            await mp.healthy(c)
            identifier = uuid.uuid4().hex
            c.command(identifier, "start", c.requests.intent().revision)
            await c.charging.settle()
            local = await positive_flow(ChargeRig(rig, s, c, sock))
            assert c.charging.state()["local_transaction"] == local
            rig.peer.permission_enabled, rig.bench.firmware.requested_current = False, 0
            await rig.bench.call(
                sock,
                "closed-atomic-stop",
                "StopTransaction",
                {"transactionId": local, "timestamp": START["timestamp"], "meterStop": 1100},
            )
            await until(lambda: s.journal.next_delivery() is None)
        async with running(rig, "closed-atomic-replacement") as (s, c, sock):
            native = await c.native.read()
            c.require_paused(native)
            state, receipt = copy.deepcopy(c.charging.state()), c.requests.receipt(identifier)
            assert receipt["state"] == "uncertain"
            sql = (
                "BEFORE UPDATE ON managed_requests WHEN NEW.id='" + identifier + "'"
                if boundary == "request"
                else "BEFORE INSERT ON meta WHEN NEW.key='managed_test_charge'"
            )
            s.journal.db.execute(
                "CREATE TRIGGER fail_closed_retire "
                + sql
                + " BEGIN SELECT RAISE(ABORT,'synthetic closed retirement write failure'); END"
            )
            with pytest.raises(RecoveryRequiredError, match="closure awaits retirement") as failure:
                c.charging.retire_closed_at_zero(native)
            assert isinstance(failure.value.__cause__, sqlite3.IntegrityError)
            assert c.charging.state() == state and c.requests.receipt(identifier) == receipt
            assert rig.bench.firmware.effective_limit() == 0
            s.journal.db.execute("DROP TRIGGER fail_closed_retire")
            c.charging.retire_closed_at_zero(native)
            assert c.charging.state()["state"] == c.requests.receipt(identifier)["state"] == "completed"
            s.journal.db.execute(
                "CREATE TEMP TRIGGER no_repeat_closed_write BEFORE INSERT ON meta "
                "WHEN NEW.key='managed_test_charge' BEGIN SELECT RAISE(ABORT,'Start is already settled'); END"
            )
            try:
                c.charging.retire_closed_at_zero(native)
            finally:
                s.journal.db.execute("DROP TRIGGER no_repeat_closed_write")
            c.command(uuid.uuid4().hex, "app_control", c.requests.intent().revision)
            assert (await mp.healthy(c))["normal_start_ready"]
