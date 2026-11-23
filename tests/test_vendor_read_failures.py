# Modified for the standalone iCS proxy repository.
"""Harmless cloud read failures retain owner control; only exact settled legacy debt is retired."""

from __future__ import annotations

import copy
import uuid
from contextlib import asynccontextmanager

import pytest

from ics_proxy_core.engine.gateway import decode
from ics_proxy_core.engine.managed import ManagedController
from ics_proxy_core.engine.managed_current_prepare import prepare_current_capability
from ics_proxy_core.engine.store import Journal, fingerprint
from ics_proxy_core.frames import build_call, build_call_result
from tests.managed_rig import OwnerBench, healthy, introduced, managed_bench, managed_session
from tests.runtime_rig import until
from tests.tls_support import tls as tls


class LostReplyBench(OwnerBench):
    """The separate physical peer applies ordinary firmware behavior and controls one wire ACK."""

    # Keep ordinary peer effects and reply correlation; omit one ACK or abort only the peer transport.
    async def read_charger(self, charger):
        async for raw in charger:
            frame = decode(raw)
            if frame[0] != 2:
                self.replies[frame[1]].set_result(frame)
                continue
            action, payload = frame[2:]
            self.device_calls.append(copy.deepcopy(frame))
            answer = self.firmware.answer(action, payload)
            if action == "SetChargingProfile":
                self.received_profiles.append(payload["csChargingProfiles"]["chargingProfileId"])
                self.received_limits.append(
                    payload["csChargingProfiles"]["chargingSchedule"]["chargingSchedulePeriod"][0]["limit"]
                )
            elif action == "RemoteStopTransaction":
                self.remote_stop.set()
            if getattr(self, "drop_action", None) == action:
                self.drop_action = None
                self.dropped = copy.deepcopy(frame)
                if self.drop_mode == "link-drop":
                    charger.transport.abort()
                    return
                continue
            await charger.send(build_call_result(frame[1], answer))


# Start a real native-backed managed runtime and always close it before retiring the independent charger reader.
@asynccontextmanager
async def operating(rig, token):
    session = managed_session(rig)
    await session.start()
    controller = ManagedController(session)
    rig.route_to_trial()
    async with rig.charger(session) as charger:
        await introduced(rig, session, charger, token)
        await healthy(controller)
        try:
            yield session, controller
        finally:
            await session.close()


# Send a genuine cloud command and inspect the correlated frame received by the independent vendor.
async def cloud(rig, token, action, payload):
    await rig.bench.cloud.send(build_call(token, action, payload))
    await until(lambda: any(f[0] != 2 and f[1] == token for f in rig.bench.calls), timeout=8)
    return next(f for f in rig.bench.calls if f[0] != 2 and f[1] == token)


# Intent: losing an ACK for a known read must not create physical-write debt or disable healthy owner control.
# Why: an RPC timeout is an availability event, while a read cannot have changed the physical permission or ceiling.
@pytest.mark.parametrize(
    "action,payload",
    [
        ("GetConfiguration", {"key": ["SupportedFeatureProfiles"]}),
        ("GetCompositeSchedule", {"connectorId": 1, "duration": 60, "chargingRateUnit": "A"}),
        ("TriggerMessage", {"requestedMessage": "StatusNotification", "connectorId": 1}),
    ],
)
async def test_lost_read_ack_retains_real_owner_control(tmp_path, tls, unused_tcp_port, monkeypatch, action, payload):

    async with managed_bench(tmp_path, tls, unused_tcp_port, bench_type=LostReplyBench) as rig:
        async with operating(rig, "read-boot") as (session, controller):
            assert (await cloud(rig, "good-read", action, payload))[0] == 3
            assert (await healthy(controller))["owner_control_enabled"]
            before = list(rig.bench.received_limits)
            rig.bench.drop_action, rig.bench.drop_mode = action, "no-reply"
            result = await cloud(rig, "lost-read", action, payload)
            assert result[0] == 4 and result[2] == "InternalError"
            assert rig.bench.dropped[2] == action
            assert session.journal.get("uncertain_vendor_command") is None
            assert not session.journal.db.execute("SELECT 1 FROM mutations WHERE origin='vendor'").fetchone()
            assert (await healthy(controller))["owner_control_enabled"]
            assert rig.bench.firmware.effective_limit() == 6 and rig.bench.received_limits == before


# Intent: a real write with a lost reply still retains both its mutation and unbound Start grant.
# Why: the harmless-read repair must never authorize a duplicate Start when the first physical outcome is unknown.
async def test_lost_start_ack_keeps_real_ambiguity(tmp_path, tls, unused_tcp_port, monkeypatch):
    from ics_proxy_core.engine.vendor_read_recovery import recover_legacy_vendor_read

    async with managed_bench(tmp_path, tls, unused_tcp_port, bench_type=LostReplyBench) as rig:
        async with operating(rig, "write-boot") as (session, controller):
            rig.bench.drop_action, rig.bench.drop_mode = "RemoteStartTransaction", "no-reply"
            result = await cloud(
                rig, "lost-start", "RemoteStartTransaction", {"connectorId": 1, "idTag": "CLOUD_OWNER"}
            )
            assert result[0] == 4
            assert session.journal.get("uncertain_vendor_command")["action"] == "RemoteStartTransaction"
            assert session.journal.get("vendor_start_grant")
            assert [
                row[0] for row in session.journal.db.execute("SELECT state FROM mutations WHERE origin='vendor'")
            ] == ["uncertain"]
            assert not recover_legacy_vendor_read(session.journal)
            assert (await controller.tick())["state"] in ("degraded", "reconciling")


# Intent: a dropped read during actual TLS loss must recover through genuine Boot and fresh native protection.
# Why: clearing transient connection state must not leave false durable command debt blocking owner control.
async def test_read_link_loss_recovers_on_actual_reconnection(tmp_path, tls, unused_tcp_port, monkeypatch):

    async with managed_bench(tmp_path, tls, unused_tcp_port, bench_type=LostReplyBench) as rig:
        session = managed_session(rig)
        await session.start()
        controller = ManagedController(session)
        rig.route_to_trial()
        try:
            async with rig.charger(session) as charger:
                await introduced(rig, session, charger, "before-link-loss")
                assert (await healthy(controller))["owner_control_enabled"]
                rig.bench.drop_action, rig.bench.drop_mode = "GetConfiguration", "link-drop"
                await rig.bench.cloud.send(
                    build_call("disconnect-read", "GetConfiguration", {"key": ["SupportedFeatureProfiles"]})
                )
                await until(lambda: getattr(rig.bench, "dropped", None) is not None)
                await until(lambda: not session.runtime.status.charger_connected)
                assert session.journal.get("uncertain_vendor_command") is None
            async with rig.charger(session) as charger:
                await introduced(rig, session, charger, "after-link-loss")
                assert (await healthy(controller))["owner_control_enabled"]
                assert rig.bench.firmware.effective_limit() == 6
                assert session.journal.get("uncertain_vendor_command") is None
                assert session.journal.get("managed_control_fault") is None
                await session.close()
        finally:
            await session.close()


# Intent: old false uncertainty is retired on a genuine engine replacement while preserving a verified zero hold.
# Why: an existing installation must recover without editing its journal or silently raising its selected ceiling.
@pytest.mark.parametrize("selected", [6, 32])
async def test_legacy_read_restart_stays_paused_until_return(tmp_path, tls, unused_tcp_port, monkeypatch, selected):
    from ics_proxy_core.engine.vendor_read_recovery import recover_legacy_vendor_read

    async with managed_bench(tmp_path, tls, unused_tcp_port) as rig:
        async with operating(rig, "initial-boot") as (session, controller):
            if selected == 32:
                controller.command(uuid.uuid4().hex, "pause", controller.requests.intent().revision)
                await healthy(controller)

                # Change only the independent fake native USER scalar, through the real capability preparation path.
                async def patch_user():
                    rig.peer.configuration["AmpLmt"] = "32"
                    rig.peer.runtime_user = 320

                await prepare_current_capability(
                    session.journal, session.book, rig.spec, rig.prepared.observer.observe_handover, patch_user
                )
        async with operating(rig, "before-upgrade-boot") as (session, controller):
            if controller.requests.intent().mode == "paused":
                controller.command(uuid.uuid4().hex, "app_control", controller.requests.intent().revision)
                await healthy(controller)
            if selected == 32:
                controller.command(uuid.uuid4().hex, "set_current", controller.requests.intent().revision, current_a=32)
                await controller.current.settle()
                await healthy(controller)
            assert rig.bench.firmware.effective_limit() == selected
            old_revision = controller.requests.intent().revision
            # Fixture the exact metadata left by the old bug, then let the real supervisor install its zero.
            marker = {"action": "GetConfiguration", "digest": fingerprint({"key": ["SupportedFeatureProfiles"]})}
            session.journal.put("uncertain_vendor_command", marker)
            controller.fault("owner_control_or_delivery_uncertain")
            assert (await controller.tick())["state"] == "attention"
            assert rig.bench.firmware.effective_limit() == 0
        positives = sum(limit > 0 for limit in rig.bench.received_limits)
        async with operating(rig, "upgraded-boot") as (session, controller):
            assert session.journal.get("uncertain_vendor_command") is None
            assert session.journal.get("managed_control_fault") is None
            assert session.journal.get("vendor_read_failure_recovery")["marker"] == marker
            assert controller.requests.intent().mode == "paused"
            assert controller.requests.intent().revision == old_revision + 1
            assert rig.bench.firmware.effective_limit() == 0
            assert sum(limit > 0 for limit in rig.bench.received_limits) == positives
            assert not recover_legacy_vendor_read(session.journal)
            controller.command(uuid.uuid4().hex, "app_control", controller.requests.intent().revision)
            assert (await healthy(controller))["owner_control_enabled"]
            assert rig.bench.firmware.effective_limit() == selected
            assert not any(frame[2] == "RemoteStartTransaction" for frame in rig.bench.device_calls)


# Build only the minimum historical metadata needed to represent the exact legacy read bug.
def legacy(journal):
    journal.put("uncertain_vendor_command", {"action": "GetConfiguration", "digest": "a" * 64})
    journal.put("managed_mode", {"mode": "app_schedule", "local_start": False, "spec_digest": "b" * 64})
    journal.put("managed_control_intent", {"mode": "app", "revision": 7})
    journal.put("trial_phase", "blocked")
    journal.put("control", {"owner": "local", "inhibit": True, "uncertain": True})
    journal.put("managed_control_fault", {"classification": "owner_control_or_delivery_uncertain", "at_epoch": 1})


# Intent: incomplete evidence, true command debt and unrelated faults cannot be erased by the legacy repair.
# Why: old read markers may coexist with a genuinely ambiguous Start, native change or queued lifecycle report.
@pytest.mark.parametrize(
    "blocker",
    [
        "write-marker",
        "opaque-marker",
        "bad-digest",
        "extra-field",
        "vendor-mutation",
        "local-mutation",
        "vendor-grant",
        "pending-start",
        "old-local-start",
        "expired-local-start",
        "queued-event",
        "active-session",
        "current-recovery",
        "uncertain-request",
        "other-fault",
        "preservation-fault",
        "inventory-fault",
        "bounded-handover-fault",
        "native-preparation-intent",
    ],
)
def test_legacy_read_repair_preserves_genuine_debt(tmp_path, blocker):
    from ics_proxy_core.engine.vendor_read_recovery import recover_legacy_vendor_read

    journal = Journal(tmp_path / "legacy.db", "OFFLINE001")
    try:
        legacy(journal)
        if blocker in ("write-marker", "opaque-marker", "bad-digest", "extra-field"):
            marker = journal.get("uncertain_vendor_command")
            if blocker == "write-marker":
                marker["action"] = "RemoteStartTransaction"
            elif blocker == "opaque-marker":
                marker["action"] = "DataTransfer"
            elif blocker == "bad-digest":
                marker["digest"] = "A" * 64
            else:
                marker["unexpected"] = True
            journal.put("uncertain_vendor_command", marker)
        elif blocker in ("vendor-mutation", "local-mutation"):
            journal.mutation_intent("vendor" if blocker == "vendor-mutation" else "local", "RemoteStartTransaction", {})
        elif blocker == "vendor-grant":
            journal.put("vendor_start_grant", {"token": "old", "expires": 0})
        elif blocker == "pending-start":
            journal.put("control", journal.get("control") | {"start_pending": True})
        elif blocker == "old-local-start":
            journal.put("local_start_receipt", {"stage": "accepted", "local": None})
        elif blocker == "expired-local-start":
            journal.put(
                "managed_test_charge", {"state": "expired", "start_rpc_accepted": True, "local_transaction": None}
            )
        elif blocker == "queued-event":
            journal.enqueue("StopTransaction", {})
        elif blocker == "active-session":
            journal.db.execute("INSERT INTO transactions(local,connector,start,vendor) VALUES(1001,1,'{}',9001)")
        elif blocker == "current-recovery":
            journal.put("managed_current_change", {"state": "recovering"})
        elif blocker == "uncertain-request":
            journal.db.execute("CREATE TABLE managed_requests(id TEXT,action TEXT,state TEXT)")
            journal.db.execute("INSERT INTO managed_requests VALUES('old','start','uncertain')")
        elif blocker == "preservation-fault":
            journal.put("trial_preservation_fault", True)
        elif blocker == "inventory-fault":
            journal.put("trial_inventory_fault", True)
        elif blocker == "bounded-handover-fault":
            journal.put("native_bounded_handover", {"stage": "admitted", "fault": "native_changed"})
        elif blocker == "native-preparation-intent":
            journal.put("managed_current_prepare_intent", {"stage": "native_write_intent"})
        else:
            journal.put("managed_control_fault", {"classification": "owner_release_unconfirmed"})
        before = list(journal.db.iterdump())
        assert not recover_legacy_vendor_read(journal)
        assert list(journal.db.iterdump()) == before
    finally:
        journal.close()


# Intent: a legacy read marker cannot acknowledge an independent denied Start.
# Why: its queued lifecycle can finish delivering before the owner verifies the denial.
def test_legacy_read_marker_does_not_retire_a_later_start_denial(tmp_path):
    from ics_proxy_core.engine.managed_start_delivery import record_start_denial
    from ics_proxy_core.engine.managed_stop_delivery import record_stop_ack
    from ics_proxy_core.engine.vendor_read_recovery import recover_legacy_vendor_read
    from tests.prototype_rig import START

    path = tmp_path / "legacy-denial.db"
    journal = Journal(path, "OFFLINE001")
    try:
        local = journal.start("start", START)["transactionId"]
        journal.stop("stop", {"transactionId": local, "meterStop": 1100, "timestamp": START["timestamp"]})
        legacy(journal)
        journal.fault.clear()
        assert not recover_legacy_vendor_read(journal)

        head = journal.next_delivery()
        assert head is not None
        denied = {"transactionId": 0, "idTagInfo": {"status": "Invalid"}}
        journal.acknowledge(head, denied, lambda: record_start_denial(journal, head, denied), allow_denied=True)
        head = journal.next_delivery()
        assert head is not None
        journal.acknowledge(head, {}, lambda: record_stop_ack(journal, head, {}))
        assert journal.next_delivery() is None
        denial = journal.get("managed_start_denials")[str(local)]
        assert denial["completed"] is None and denial["fault"] == journal.fault.read()
    finally:
        journal.close()

    journal = Journal(path, "OFFLINE001")
    try:
        assert not recover_legacy_vendor_read(journal)
        assert journal.fault.read() == denial["fault"]
        assert journal.get("uncertain_vendor_command")["action"] == "GetConfiguration"
    finally:
        journal.close()


# Intent: an accepted vendor read stays on the connection where it arrived.
# Why: its handler may run after the charger reconnects but before the new Boot.
async def test_waiting_vendor_read_cannot_move_to_new_charger(tmp_path):
    from tests.prototype_rig import Rig

    rig = Rig(tmp_path / "read-reconnect.db")
    try:
        rig.store.put("trial_phase", "active")
        rig.store.put("trial_binding", {"boot_digest": "synthetic"})
        await rig.boot()
        await rig.cloud()
        payload = {"requestedMessage": "BootNotification"}
        assert (await rig.vendor("before-reconnect", "TriggerMessage", payload))[0] == 3

        await rig.gateway.receive("vendor", build_call("waiting-read", "TriggerMessage", payload))
        before = len(rig.charger_frames)
        rig.gateway.attach_charger(rig.charger_send)
        await rig.gateway.drain()

        assert len(rig.charger_frames) == before
        assert rig.vendor_frames[-1][:3] == [4, "waiting-read", "InternalError"]
    finally:
        await rig.close()
