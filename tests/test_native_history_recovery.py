# Modified for the standalone iCS proxy repository.
"""Offline historical-loss acknowledgement keeps actual native and encrypted OCPP recovery authority intact."""

from __future__ import annotations

import copy
import json
import sqlite3
import ssl
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, closing
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest

from ics_proxy_core.engine.managed import ManagedController
from ics_proxy_core.engine.managed_current import capability_value
from ics_proxy_core.engine.managed_intent import OWNER_PAUSE, ManagedRequests
from ics_proxy_core.engine.native_faults import NativeContentError, diagnostic_type
from ics_proxy_core.engine.native_history import audit_history, history_policy
from ics_proxy_core.engine.native_history_recovery import acknowledge_history_policy, matches_history_fault
from ics_proxy_core.engine.security import SecurityError
from ics_proxy_core.engine.store import Journal, RecoveryRequiredError, fingerprint
from ics_proxy_core.engine.trial_resources import ResourceBook
from tests.charge_rig import ChargeRig, positive_flow
from tests.connected_rig import ConnectedBench, strategy_file
from tests.managed_rig import healthy, introduced, managed_bench, managed_session, starts
from tests.prototype_rig import START
from tests.recovery_barriers import completed_control
from tests.runtime_rig import until
from tests.test_managed_normal_start import natural_stop
from tests.test_native_bounded_handover import restore_original_route, verify_original_backend
from tests.test_native_history_archive import unassigned
from tests.tls_support import tls as tls
from tools import acknowledge_native_history as history_tool


@dataclass
class HistoryFault:
    """A stopped service has its real fault, retained zero and independently read native HTTP evidence."""

    rig: ConnectedBench
    journal: Journal
    book: ResourceBook
    original: list[dict[str, Any]]
    before: dict[str, Any]


@pytest.mark.parametrize("source", ["original_history_omission", "startup_history_omission"])
def test_history_fault_matches_the_bound_omission_code_after_rewording(source):
    fault = {"classification": "native_authority_or_preservation_failed", "at_epoch": 2}
    legacy = {
        "type": "SecurityError",
        "detail": "native bounded handover must preserve every original history record",
        "at_epoch": 1,
    }
    failure = {**legacy, "source": source, "detail": "An original native history record is absent."}
    bound = {**fault, "native_failure_digest": fingerprint(failure)}
    assert matches_history_fault(fault, legacy)
    assert matches_history_fault(bound, failure)
    assert not matches_history_fault(bound, failure, attempts=True)
    assert not matches_history_fault(bound, {**failure, "detail": "A different recorded diagnostic."})
    unknown = {**legacy, "source": "other_native_history_failure"}
    assert not matches_history_fault({**fault, "native_failure_digest": fingerprint(unknown)}, unknown)
    assert not matches_history_fault(fault, unknown)


# Read stable private database contents for an exact rollback comparison, without accepting new state.
def database(journal: Journal) -> str:
    return "\n".join(journal.db.iterdump())


# Preserve full historical bodies, including nested data, before commissioning the real managed fixture.
def closed_records() -> list[dict[str, Any]]:
    end = int(time.time()) - 3600
    return [
        {
            "transactionId": 9000000 + index,
            "startTime": end - 3600 + index * 120,
            "stopTime": end - 3540 + index * 120,
            "meterStart": 1000 + index * 100,
            "meterStop": 1010 + index * 100,
            "original_record": {"preserve": [index, {"number": 1, "text": "complete original body"}]},
        }
        for index in range(3)
    ]


# Latch a genuine loss through native HTTP audits and actual TLS peers, then relinquish ownership before ack.
@asynccontextmanager
async def missing_history(
    directory: Path,
    contexts: tuple[ssl.SSLContext, ssl.SSLContext, Path],
    port: int,
    monkeypatch: pytest.MonkeyPatch,
    *,
    paused: bool = False,
    mixed: bool = False,
    closing_stop: str | None = None,
    pause_after_fault: str | None = None,
) -> AsyncIterator[HistoryFault]:
    async with managed_bench(directory, contexts, port) as rig:
        rig.bench.history = closed_records()
        first = managed_session(rig)
        await first.start()
        controller = ManagedController(first)
        rig.route_to_trial()
        async with rig.charger(first) as charger:
            await introduced(rig, first, charger, "history-original-boot")
            await healthy(controller)
            if paused:
                controller.command(uuid.uuid4().hex, "pause", 0)
                await healthy(controller)
            original = copy.deepcopy(first.journal.get("native_bounded_handover")["original_history"])
            before = {
                key: copy.deepcopy(first.journal.get(key))
                for key in ("trial_allocation", "next_transaction", "managed_mode", "managed_control_intent")
            }
            if mixed:
                # Prove a configuration contradiction through the independent native peer first.
                with monkeypatch.context() as config:
                    config.setitem(rig.peer.configuration, "maxOutCurrent0", "31")
                    assert (await controller.tick())["state"] == "degraded"
                    assert "history" not in first.journal.get("managed_native_failure")["detail"]
            if closing_stop is not None:
                charge = ChargeRig(rig, first, controller, charger)
                local = await positive_flow(charge, "daily")
                await healthy(controller)
                target = controller.session_stop.target()
                assert target is not None
                controller.command(closing_stop, "stop_session", controller.requests.intent().revision, target=target)
                await until(lambda: first.journal.outcomes.accepted_stop(local))
                rig.bench.history = copy.deepcopy(original[2:])
                await natural_stop(charge, local)
            else:
                rig.bench.history = copy.deepcopy(original[2:])
            for _ in range(2 if mixed else 3):
                assert (await controller.tick())["state"] == "degraded"
            assert (await controller.tick())["state"] == "attention"
            assert first.journal.get("managed_control_fault")["classification"] == (
                "native_authority_or_preservation_failed"
            )
            assert first.journal.get("managed_native_failure")["detail"] == (
                "native bounded handover must preserve every original history record"
            )
            assert rig.bench.firmware.effective_limit() == 0
            if pause_after_fault is not None:
                controller.command(pause_after_fault, "pause", controller.requests.intent().revision)
                await controller.pause_task
                assert controller.requests.receipt(pause_after_fault)["state"] == "accepted"
            with pytest.raises(RecoveryRequiredError, match="active owner"):
                Journal(first.journal.path, first.journal.identity)
            path, identity = first.journal.path, first.journal.identity
            await first.close()
        journal = Journal(path, identity)
        fault = HistoryFault(rig, journal, ResourceBook(journal, rig.prepared.baseline), original, before)
        try:
            yield fault
        finally:
            fault.journal.close()


# Intent: a Pause accepted during history loss stays owned by the proxy during offline acknowledgement.
# Why: history recovery must clear the fault for the normal owner to confirm Pause after restarting at zero.
@pytest.mark.parametrize("same_pause", [True, False], ids=["exact-pause", "unrelated-receipt"])
async def test_history_acknowledgement_retains_pending_pause(
    tmp_path, tls, unused_tcp_port, monkeypatch, same_pause
) -> None:
    request = uuid.uuid4().hex
    async with missing_history(tmp_path, tls, unused_tcp_port, monkeypatch, pause_after_fault=request) as fault:
        rig, journal = fault.rig, fault.journal
        intent = journal.get("managed_control_intent")
        before = journal.outcomes.owner(request)
        assert before is not None and before.state == "accepted"
        native = await rig.prepared.observer.observe_handover()
        calls = list(rig.bench.device_calls)
        if not same_pause:
            journal.put(OWNER_PAUSE, {**journal.get(OWNER_PAUSE), "ticket": uuid.uuid4().hex})
            damaged = database(journal)
            with pytest.raises(RecoveryRequiredError, match="settled journal"):
                acknowledge_history_policy(journal, fault.book, rig.spec, native)
            assert database(journal) == damaged and rig.bench.device_calls == calls
            return
        assert acknowledge_history_policy(journal, fault.book, rig.spec, native)["fault_cleared"] is True
        assert journal.get("managed_control_intent") == intent and journal.outcomes.owner(request) == before
        assert rig.bench.device_calls == calls and rig.bench.firmware.effective_limit() == 0
        journal.close()
        replacement = managed_session(rig)
        try:
            await replacement.start()
            controller = ManagedController(replacement)
            async with rig.charger(replacement) as charger:
                await introduced(rig, replacement, charger, "history-paused-replacement")
                assert (await healthy(controller))["local_hold_verified"] is True
                assert controller.requests.receipt(request)["state"] == "completed"
                assert controller.requests.intent().mode == "paused" and rig.bench.firmware.effective_limit() == 0
                await replacement.close()
        finally:
            await replacement.close()


# Intent: an offline operator acknowledges precisely the missing bodies while keeping allocations, intent and zero.
# Why: acknowledging rolling history is not permission to erase evidence, allocate ids, or initiate charging.
@pytest.mark.parametrize("paused", [False, True], ids=["app-intent", "paused-intent"])
async def test_acknowledgement_archives_exact_bodies_and_preserves_authority(
    tmp_path: Path,
    tls: tuple[ssl.SSLContext, ssl.SSLContext, Path],
    unused_tcp_port: int,
    monkeypatch: pytest.MonkeyPatch,
    paused: bool,
) -> None:
    async with missing_history(tmp_path, tls, unused_tcp_port, monkeypatch, paused=paused) as fault:
        rig, journal = fault.rig, fault.journal
        native = await rig.prepared.observer.observe_handover()
        writes = copy.deepcopy(rig.bench.device_calls)
        native_requests = list(rig.peer.requests)
        old_fault, old_failure = journal.get("managed_control_fault"), journal.get("managed_native_failure")
        result = acknowledge_history_policy(journal, fault.book, rig.spec, native)
        assert result == {"archived_records": 3, "missing_records": 2, "fault_cleared": True, "charging_started": False}
        archive = [json.loads(row[0]) for row in journal.db.execute("SELECT body FROM native_history_records")]
        assert sorted(archive, key=lambda r: r["transactionId"]) == fault.original
        assert journal.get("native_bounded_handover")["original_history"] == fault.original
        assert all(journal.get(key) == value for key, value in fault.before.items())
        assert journal.get("managed_control_fault") is None
        assert journal.get("managed_native_failure") == old_failure
        assert journal.get("trial_phase") == "recovering"
        control = journal.get("control")
        assert control["owner"] == "local" and control["inhibit"] and control["uncertain"]
        receipt = journal.get("native_history_policy_acknowledgement")
        assert receipt["policy"] == history_policy(journal, rig.spec.digest)
        assert journal.get("native_history_policy") == receipt["policy"]
        assert receipt["prior_fault"] == old_fault and receipt["prior_failure"] == old_failure
        assert receipt["charging_started"] is False and receipt["summary"] == journal.get("native_history_summary")
        assert rig.bench.device_calls == writes and rig.peer.requests == native_requests
        assert rig.bench.firmware.effective_limit() == 0 and not starts(rig)


# Intent: history that disappears while the service is stopped gains the same recoverable fault on startup.
# Why: the existing offline acknowledgment needs the fault even though the failing startup cannot open a listener.
@pytest.mark.parametrize(
    "case", ["missing", "changed", "restored", "app-intent", "only-attempt-missing", "queued-status"]
)
async def test_cold_start_history_loss_records_repair_before_listening(
    tmp_path, tls, unused_tcp_port, monkeypatch, case
):
    original = closed_records()
    if case == "only-attempt-missing":
        original.insert(0, unassigned())
    async with managed_bench(tmp_path, tls, unused_tcp_port, initial_history=original) as rig:
        strategy = strategy_file(rig, tmp_path)
        first = managed_session(rig)
        await first.start()
        controller = ManagedController(first)
        rig.route_to_trial()
        async with rig.charger(first) as charger:
            await introduced(rig, first, charger, "before-stopped-history-loss")
            await healthy(controller)
            if case != "app-intent":
                request, revision = uuid.uuid4().hex, controller.requests.intent().revision
                controller.command(request, "pause", revision)
                b = ChargeRig(rig, first, controller, charger)
                await completed_control(b, request, mode="paused", revision=revision + 1, ceiling=0)
            await first.close()

        if case == "queued-status":
            report = {"connectorId": 1, "status": "Available", "errorCode": "NoError"}
            with closing(Journal(first.journal.path, first.journal.identity)) as original_journal:
                queued = original_journal.enqueue("StatusNotification", report)
        rig.bench.history = copy.deepcopy(original[1:])
        if case == "changed":
            rig.bench.history[0]["meterStop"] += 1
        before = list(rig.bench.device_calls)
        refused = managed_session(rig)
        try:
            with pytest.raises(SecurityError, match="preserve every original history record"):
                await refused.start()
            assert not refused.started and rig.bench.device_calls == before
            journal = refused.journal
            if case == "changed":
                assert not journal.fault.latched()
                assert journal.get("managed_native_failure") is None
                return
            assert journal.phase.read().held
            assert journal.fault.classification() == "native_authority_or_preservation_failed"
            detail = journal.get("managed_native_failure")
            assert detail["type"] == "SecurityError" and "preserve every original" in detail["detail"]
            assert rig.bench.firmware.effective_limit() == 0
        finally:
            await refused.close()

        if case == "restored":
            rig.bench.history = copy.deepcopy(original)
        preflight = history_tool.preflight_native

        # The real offline tool reads the actual native HTTP peer on its test-assigned loopback port.
        def native_peer(path):
            prepared = preflight(path)
            prepared.observer.port = rig.prepared.observer.port
            return prepared

        monkeypatch.setattr(history_tool, "preflight_native", native_peer)
        assert await history_tool.run("acknowledge", tmp_path / "trial.json", strategy) == 0
        assert rig.bench.device_calls == before and rig.bench.firmware.effective_limit() == 0
        if case == "queued-status":
            with closing(Journal(first.journal.path, first.journal.identity)) as original_journal:
                delivery = original_journal.next_delivery()
                assert delivery.token == queued and delivery.payload == report and delivery.state == "queued"

        repaired = managed_session(rig)
        await repaired.start()
        controller = ManagedController(repaired)
        async with rig.charger(repaired) as charger:
            await introduced(rig, repaired, charger, "after-stopped-history-ack")
            status = await healthy(controller)
            if case == "app-intent":
                assert status["owner_control_enabled"] and rig.bench.firmware.effective_limit() == 6
            else:
                assert status["local_hold_verified"] and rig.bench.firmware.effective_limit() == 0
            if case == "restored":
                assert repaired.journal.get("native_history_policy") is None
                assert repaired.journal.get("native_history_summary") == {"archived_records": 3, "missing_records": 0}
            else:
                assert status["history_warning"] == "records_absent_from_charger"
                summary = repaired.journal.get("native_history_summary")
                if case == "only-attempt-missing":
                    assert summary == {"archived_records": 3, "missing_records": 0, "missing_attempts": 1}
                    retained = repaired.journal.db.execute("SELECT body FROM native_start_attempts").fetchone()
                    assert json.loads(retained["body"]) == original[0]
                else:
                    assert summary == {"archived_records": 3, "missing_records": 1}
            b = ChargeRig(rig, repaired, controller, charger)
            request, revision = uuid.uuid4().hex, controller.requests.intent().revision
            controller.command(request, "pause", revision)
            await completed_control(b, request, mode="paused", revision=revision + 1, ceiling=0)
            request, revision = uuid.uuid4().hex, controller.requests.intent().revision
            controller.command(request, "app_control", revision)
            await completed_control(b, request, mode="app", revision=revision + 1, ceiling=6)
            assert not starts(rig)


# Intent: a startup history omission cannot take ownership from a pending Pause or vendor write.
# Why: if exact history returns, the prior command must still have its normal recovery path.
@pytest.mark.parametrize("debt", ["pause", "vendor-write"])
async def test_cold_start_transient_history_omission_preserves_pending_owner(tmp_path, tls, unused_tcp_port, debt):
    original = closed_records()
    async with managed_bench(tmp_path, tls, unused_tcp_port, initial_history=original) as rig:
        first = managed_session(rig)
        await first.start()
        controller = ManagedController(first)
        rig.route_to_trial()
        async with rig.charger(first) as charger:
            await introduced(rig, first, charger, "before-interrupted-owner")
            await healthy(controller)
            request, revision = uuid.uuid4().hex, controller.requests.intent().revision
            controller.command(request, "pause", revision)
            await completed_control(
                ChargeRig(rig, first, controller, charger), request, mode="paused", revision=revision + 1, ceiling=0
            )
            path, identity = first.journal.path, first.journal.identity
            await first.close()

        with closing(Journal(path, identity)) as journal:
            if debt == "pause":
                requests = ManagedRequests(journal)
                request, revision = uuid.uuid4().hex, requests.intent().revision
                requests.accept(request, "pause", revision)
            else:
                request = journal.mutation_intent("vendor", "DataTransfer", {"vendorId": "IoCharger"})

        rig.bench.history = copy.deepcopy(original[1:])
        refused = managed_session(rig)
        try:
            with pytest.raises(SecurityError, match="preserve every original history record"):
                await refused.start()
            assert not refused.started and not refused.journal.fault.latched()
            assert refused.journal.get("managed_native_failure") is None
        finally:
            await refused.close()

        rig.bench.history = original
        recovered = managed_session(rig)
        await recovered.start()
        assert not recovered.journal.fault.latched() and rig.bench.firmware.effective_limit() == 0
        if debt == "pause":
            controller = ManagedController(recovered)
            async with rig.charger(recovered) as charger:
                await introduced(rig, recovered, charger, "after-interrupted-owner")
                await completed_control(
                    ChargeRig(rig, recovered, controller, charger),
                    request,
                    mode="paused",
                    revision=revision + 1,
                    ceiling=0,
                )
        else:
            row = recovered.journal.db.execute("SELECT state FROM mutations WHERE token=?", (request,)).fetchone()
            assert row["state"] == "pending"
        assert not starts(rig)


# Intent: the persisted decision permits fresh real Boot/reconciliation and later retention loss without Start.
# Why: offline acknowledgement cannot bypass introduction, promote a stale physical zero, or clear future drift.
async def test_real_tls_recovery_warns_on_later_absence_but_latches_changed_original(
    tmp_path: Path,
    tls: tuple[ssl.SSLContext, ssl.SSLContext, Path],
    unused_tcp_port: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with missing_history(tmp_path, tls, unused_tcp_port, monkeypatch) as fault:
        rig, journal = fault.rig, fault.journal
        native = await rig.prepared.observer.observe_handover()
        acknowledge_history_policy(journal, fault.book, rig.spec, native)
        saved = copy.deepcopy(journal.get("native_history_policy_acknowledgement"))
        journal.close()
        # Reopen this same handle for context cleanup after the independently owned replacement retires.
        try:
            replacement = managed_session(rig)
            await replacement.start()
            controller = ManagedController(replacement)
            assert replacement.journal.get("native_history_policy_acknowledgement") == saved
            assert replacement.journal.get("trial_phase") == "recovering"
            assert rig.bench.firmware.effective_limit() == 0
            async with rig.charger(replacement) as charger:
                assert (await controller.tick())["state"] == "starting"
                assert rig.bench.firmware.effective_limit() == 0
                await introduced(rig, replacement, charger, "history-replacement-boot")
                status = await healthy(controller)
                assert status["owner_control_enabled"] and rig.bench.firmware.effective_limit() == 6
                assert status["charger_history_complete"]
                assert replacement.journal.get("native_history_summary") == {
                    "archived_records": 3,
                    "missing_records": 2,
                }
                # An older row can still be shown, but it must never stand in for the archived latest completion.
                rig.bench.history = fault.original[:1]
                status = await healthy(controller)
                assert status["history_warning"] == "records_absent_from_charger"
                assert len(status["charger_sessions"]) == 1
                assert not status["charger_history_complete"]
                rig.bench.history = fault.original[-1:]
                assert (await healthy(controller))["charger_history_complete"]
                # A later completed row is archived, then reported as open under its existing transaction ID.
                last = fault.original[-1]
                later = last | {
                    "transactionId": last["transactionId"] + 1,
                    "startTime": last["startTime"] + 120,
                    "stopTime": last["stopTime"] + 120,
                }
                rig.bench.history = [*fault.original, later]
                assert (await healthy(controller))["charger_history_complete"]
                # A damaged new version under the same ID must not make an older session look latest.
                rig.bench.history[-1] = later | {
                    "startTime": last["startTime"] - 10,
                    "stopTime": last["startTime"] - 20,
                }
                status = await healthy(controller)
                assert not status["charger_latest_completion_known"]
                rig.bench.history[-1] = later | {"stopTime": 0, "meterStop": 0}
                status = await healthy(controller)
                assert status["history_warning"] is None
                assert not status["charger_history_complete"]
                # The last remaining original can later leave native retention, while its full archive remains.
                rig.bench.history = []
                assert (await healthy(controller))["state"] == "healthy"
                assert replacement.journal.get("native_history_summary")["missing_records"] == 4
                # Reappearing original ids must still carry their exact original bodies, including numeric types.
                changed = copy.deepcopy(fault.original[-1])
                changed["original_record"]["preserve"][1]["number"] = 1.0
                rig.bench.history = [changed]
                for _ in range(3):
                    assert (await controller.tick())["state"] == "degraded"
                assert (await controller.tick())["state"] == "attention"
                assert rig.bench.firmware.effective_limit() == 0
                assert not starts(rig) and replacement.journal.active() is None
                assert replacement.journal.get("native_bounded_handover")["original_history"] == fault.original
                await replacement.close()
        finally:
            replacement = next((s for s in reversed(rig.sessions) if not s.closed), None)
            if replacement is not None:
                await replacement.close()
            fault.journal = Journal(journal.path, journal.identity)


# Alter only explicitly named persistent preconditions; the independent physical peer stays at confirmed zero.
def corrupt_guard(journal: Journal, guard: str) -> None:
    if guard == "managed-mode":
        journal.put("managed_mode", journal.get("managed_mode") | {"mode": "other"})
    elif guard == "local-start":
        journal.put("managed_mode", journal.get("managed_mode") | {"local_start": True})
    elif guard == "other-fault":
        journal.put("managed_control_fault", journal.get("managed_control_fault") | {"classification": "other"})
    elif guard == "failure-kind":
        journal.put("managed_native_failure", journal.get("managed_native_failure") | {"type": "OtherError"})
    elif guard == "failure-time":
        journal.put("managed_native_failure", journal.get("managed_native_failure") | {"at_epoch": 1})
    elif guard in ("trial_preservation_fault", "trial_inventory_fault", "uncertain_vendor_command"):
        journal.put(guard, {"reason": "independently unresolved"})
    elif guard == "outbox":
        journal.enqueue("Heartbeat", {})
    elif guard == "mutation":
        journal.mutation_intent("vendor", "DataTransfer", {"unresolved": True})
    elif guard in ("request-accepted", "request-running", "request-uncertain"):
        journal.db.execute(
            "INSERT INTO managed_requests VALUES(?,?,?,?,?)",
            (uuid.uuid4().hex, "app_control", 0, guard.removeprefix("request-"), "{}"),
        )
    elif guard == "active-transaction":
        journal.start("offline-unresolved-start", START)
        journal.db.execute("DELETE FROM outbox")
    elif guard == "unbound-start-grant":
        journal.put("vendor_start_grant", {"local": None, "tag": "CLOUD_OWNER"})
    elif guard == "not-inhibited":
        journal.put("control", journal.get("control") | {"inhibit": False})
    else:
        raise AssertionError(guard)


# Intent: every unrelated uncertain state or fault refuses recovery without a partial acknowledgement.
# Why: narrowly acknowledging missing historical bodies cannot launder live sessions or unresolved operations.
@pytest.mark.parametrize(
    "guard",
    [
        "managed-mode",
        "local-start",
        "other-fault",
        "failure-kind",
        "failure-time",
        "trial_preservation_fault",
        "trial_inventory_fault",
        "uncertain_vendor_command",
        "outbox",
        "mutation",
        "request-accepted",
        "request-running",
        "request-uncertain",
        "active-transaction",
        "unbound-start-grant",
        "not-inhibited",
    ],
)
async def test_offline_acknowledgement_refuses_unrelated_or_unresolved_state(
    tmp_path: Path,
    tls: tuple[ssl.SSLContext, ssl.SSLContext, Path],
    unused_tcp_port: int,
    monkeypatch: pytest.MonkeyPatch,
    guard: str,
) -> None:
    async with missing_history(tmp_path, tls, unused_tcp_port, monkeypatch) as fault:
        native = await fault.rig.prepared.observer.observe_handover()
        corrupt_guard(fault.journal, guard)
        before = database(fault.journal)
        with pytest.raises((SecurityError, RecoveryRequiredError)):
            acknowledge_history_policy(fault.journal, fault.book, fault.rig.spec, native)
        assert database(fault.journal) == before


# Mutate the independent charger peer rather than substituting the recovery function's native checks.
def change_native(rig: ConnectedBench, guard: str) -> None:
    if guard == "user-limit":
        rig.peer.configuration["AmpLmt"], rig.peer.runtime_user = "7", 70
    elif guard == "hardware-limit":
        rig.peer.hardware = 310
    elif guard == "configuration":
        rig.peer.configuration["untouched"] = "changed"
    elif guard == "saved-plan":
        rig.bench.firmware.plans[9]["original"] = "changed"
    elif guard == "owned-profile":
        rig.bench.firmware.profiles[7001]["chargingSchedule"]["chargingSchedulePeriod"][0]["limit"] = 1
    elif guard == "other-phase-current":
        rig.peer.phase_fault = True
    elif guard == "original-body":
        rig.bench.history[0]["original_record"]["preserve"][0] = "changed"
    elif guard == "no-missing":
        raise AssertionError("handled with retained complete original bodies")
    else:
        raise AssertionError(guard)


# Intent: current native6/HW32/configuration/plans/owned0/all-phase closure remain mandatory for acknowledgement.
# Why: an old historical absence never excuses a concurrent physical or preservation failure.
@pytest.mark.parametrize(
    "guard",
    [
        "user-limit",
        "hardware-limit",
        "configuration",
        "saved-plan",
        "owned-profile",
        "other-phase-current",
        "original-body",
        "stale-observation",
        "no-missing",
    ],
)
async def test_independent_native_guards_still_refuse_recovery(
    tmp_path: Path,
    tls: tuple[ssl.SSLContext, ssl.SSLContext, Path],
    unused_tcp_port: int,
    monkeypatch: pytest.MonkeyPatch,
    guard: str,
) -> None:
    async with missing_history(tmp_path, tls, unused_tcp_port, monkeypatch) as fault:
        if guard == "no-missing":
            fault.rig.bench.history = copy.deepcopy(fault.original)
        elif guard != "stale-observation":
            change_native(fault.rig, guard)
        native = await fault.rig.prepared.observer.observe_handover()
        if guard == "stale-observation":
            native = replace(native, physical=replace(native.physical, observed_at=time.time() - 30))
        before = database(fault.journal)
        with pytest.raises((SecurityError, RecoveryRequiredError)):
            acknowledge_history_policy(fault.journal, fault.book, fault.rig.spec, native)
        assert database(fault.journal) == before


# Intent: failure writing the final operator receipt rolls archive, policy, resource observations and fault back.
# Why: a crash or SQLite error cannot publish missing-history tolerance without its complete auditable decision.
async def test_receipt_write_failure_rolls_back_all_recovery_changes(
    tmp_path: Path,
    tls: tuple[ssl.SSLContext, ssl.SSLContext, Path],
    unused_tcp_port: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with missing_history(tmp_path, tls, unused_tcp_port, monkeypatch) as fault:
        native = await fault.rig.prepared.observer.observe_handover()
        fault.journal.db.execute(
            "CREATE TRIGGER reject_history_receipt BEFORE INSERT ON meta "
            "WHEN NEW.key='native_history_policy_acknowledgement' "
            "BEGIN SELECT RAISE(ABORT,'synthetic recovery receipt failure'); END"
        )
        before = database(fault.journal)
        with pytest.raises(sqlite3.IntegrityError, match="synthetic recovery receipt failure"):
            acknowledge_history_policy(fault.journal, fault.book, fault.rig.spec, native)
        assert database(fault.journal) == before
        assert fault.journal.get("managed_control_fault") is not None
        assert fault.journal.get("native_history_policy") is None


# Intent: a matching policy row alone cannot activate retention tolerance without the atomic operator receipt.
# Why: the exceptional permission must be evidenced by the complete accepted recovery, rather than a lone flag.
async def test_policy_without_operator_receipt_cannot_enable_missing_history(
    tmp_path: Path,
    tls: tuple[ssl.SSLContext, ssl.SSLContext, Path],
    unused_tcp_port: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with missing_history(tmp_path, tls, unused_tcp_port, monkeypatch) as fault:
        fault.journal.put("native_history_policy", history_policy(fault.journal, fault.rig.spec.digest))
        before = database(fault.journal)
        with pytest.raises(SecurityError):
            audit_history(fault.journal, fault.rig.spec.digest, list(fault.rig.bench.history), False)
        assert database(fault.journal) == before


# Intent: approved archived absence survives real cleanup and original32 restoration, with current HW still checked.
# Why: a recovery exception must permit the ordinary exit path without weakening its independent native safeguards.
async def test_managed_history_policy_allows_complete_original32_restoration(
    tmp_path: Path,
    tls: tuple[ssl.SSLContext, ssl.SSLContext, Path],
    unused_tcp_port: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with missing_history(tmp_path, tls, unused_tcp_port, monkeypatch) as fault:
        rig, journal = fault.rig, fault.journal
        native = await rig.prepared.observer.observe_handover()
        acknowledge_history_policy(journal, fault.book, rig.spec, native)
        original_plans = copy.deepcopy(rig.bench.firmware.plans)
        remaining_history = copy.deepcopy(rig.bench.history)
        journal.close()
        replacement = None
        try:
            replacement = managed_session(rig)
            await replacement.start()
            controller = ManagedController(replacement)
            async with rig.charger(replacement) as charger:
                await introduced(rig, replacement, charger, "history-original32-boot")
                assert (await healthy(controller))["owner_control_enabled"]
                assert rig.bench.firmware.effective_limit() == 6
                assert (await replacement.command({"command": "native-bounded-handover"}))["profiles_restored"]
                assert not rig.bench.firmware.profiles
                await restore_original_route(rig, replacement, charger)
                await verify_original_backend(rig, replacement)
                writes = []

                # Only the independent fake native peer receives the authorized original USER scalar.
                async def patch(values: dict[str, Any]) -> None:
                    writes.append(copy.deepcopy(values))
                    rig.peer.configuration.update(values)
                    rig.peer.runtime_user = 320

                rig.peer.hardware = 310
                assert replacement.native_bounded is not None
                with pytest.raises(SecurityError):
                    await replacement.native_bounded.restore_settings(patch)
                assert not writes and rig.peer.configuration["AmpLmt"] == "6"
                assert replacement.journal.get("trial_phase") == "backend_restore_pending"
                rig.peer.hardware = 320
                assert (await replacement.native_bounded.restore_settings(patch))["complete"]
                assert writes == [{"AmpLmt": "32"}]
                assert rig.peer.configuration == rig.spec.original_configuration
                assert replacement.journal.get("trial_phase") == "complete"
                assert replacement.journal.get("native_bounded_handover")["stage"] == "complete"
                assert replacement.journal.get("native_bounded_handover")["original_history"] == fault.original
                archive = [
                    json.loads(row[0])
                    for row in replacement.journal.db.execute("SELECT body FROM native_history_records")
                ]
                assert sorted(archive, key=lambda r: r["transactionId"]) == fault.original
                assert rig.bench.history == remaining_history and rig.bench.firmware.plans == original_plans
                assert replacement.journal.active() is None and not starts(rig)
                assert all(replacement.journal.get(key) == value for key, value in fault.before.items())
        finally:
            if replacement is not None:
                await replacement.close()
            fault.journal = Journal(journal.path, journal.identity)


# Intent: recover only the expected unassigned attempts under proven zero and retain paused intent across restart.
# Why: repaired parsing cannot itself clear an unrelated fault or release power at service replacement.
@pytest.mark.parametrize("native_limit", [6, 32])
async def test_recover_unassigned_start_to_verified_pause(
    tmp_path: Path,
    tls: tuple[ssl.SSLContext, ssl.SSLContext],
    unused_tcp_port: int,
    monkeypatch: pytest.MonkeyPatch,
    native_limit: int,
) -> None:
    from ics_proxy_core.engine.native_history_recovery import acknowledge_start_attempts

    async with missing_history(tmp_path, tls, unused_tcp_port, monkeypatch) as fault:
        rig, journal = fault.rig, fault.journal
        native = await rig.prepared.observer.observe_handover()
        acknowledge_history_policy(journal, fault.book, rig.spec, native)
        if native_limit == 32:
            journal.put("managed_current_capability", capability_value(journal, rig.spec))
            rig.peer.configuration["AmpLmt"] = "32"
            rig.peer.runtime_user = 320
        now = time.time()
        fault_record = {"at_epoch": now, "classification": "native_authority_or_preservation_failed"}
        failure = {
            "at_epoch": now,
            "type": "SecurityError",
            "detail": "complete connected-handover native evidence is required",
        }
        journal.put("managed_control_fault", fault_record)
        journal.put("managed_native_failure", failure)
        journal.put("trial_phase", "blocked")
        attempt = unassigned()
        attempt["startTime"] = int(now) - 6
        attempt["MeterValues"][0]["timestamp"] = attempt["startTime"]
        rig.bench.history.append(attempt)
        native = await rig.prepared.observer.observe_handover()
        journal.put("uncertain_vendor_command", True)
        before = database(journal)
        with pytest.raises(RecoveryRequiredError):
            acknowledge_start_attempts(journal, fault.book, rig.spec, native)
        assert database(journal) == before
        journal.db.execute("DELETE FROM meta WHERE key='uncertain_vendor_command'")
        initial = {"mode": "app", "revision": 5}
        journal.put("managed_control_intent", {**initial, "unexpected": True})
        before = database(journal)
        with pytest.raises(RecoveryRequiredError):
            acknowledge_start_attempts(journal, fault.book, rig.spec, native)
        assert database(journal) == before
        journal.put("managed_control_intent", initial)
        audit_history(journal, rig.spec.digest, list(native.history), False)
        before = database(journal)
        with pytest.raises(RecoveryRequiredError):
            acknowledge_start_attempts(journal, fault.book, rig.spec, native)
        assert database(journal) == before
        attempt["MeterValues"][0]["Offer"] += 1
        changed_attempt = await rig.prepared.observer.observe_handover()
        with pytest.raises(RecoveryRequiredError):
            acknowledge_start_attempts(journal, fault.book, rig.spec, changed_attempt)
        attempt["MeterValues"][0]["Offer"] -= 1
        journal.db.execute("DELETE FROM native_start_attempts")
        journal.put("native_start_attempt_recovery", {"prior_fault": {"retained": True}})
        before = database(journal)
        with pytest.raises(RecoveryRequiredError):
            acknowledge_start_attempts(journal, fault.book, rig.spec, native)
        assert database(journal) == before
        journal.db.execute("DELETE FROM meta WHERE key='native_start_attempt_recovery'")
        uncertain_id, pause_id = uuid.uuid4().hex, uuid.uuid4().hex
        uncertain_result = json.dumps({"classification": "start_not_confirmed"})
        journal.db.execute(
            "INSERT INTO managed_requests VALUES(?, 'start_test', 2, 'uncertain', ?)", (uncertain_id, uncertain_result)
        )
        insert_pause = "INSERT INTO managed_requests VALUES(?, 'pause', ?, 'completed', ?)"
        for pause_revision, when, verified in ((1, now - 1, True), (3, now + 1, True), (3, now - 1, False)):
            journal.db.execute(
                insert_pause, (pause_id, pause_revision, json.dumps({"completed_at": when, "pause_verified": verified}))
            )
            before = database(journal)
            with pytest.raises(RecoveryRequiredError):
                acknowledge_start_attempts(journal, fault.book, rig.spec, native)
            assert database(journal) == before
            journal.db.execute("DELETE FROM managed_requests WHERE id=?", (pause_id,))
        # An older dashboard sent revision 2, but the proxy bound this newer Pause to its actual generation.
        pause_result = {
            "completed_at": now - 1,
            "pause_verified": True,
            "_pause": {"revision": 5, "hold": uuid.uuid4().hex, "local": None},
        }
        journal.db.execute(insert_pause, (pause_id, 2, json.dumps(pause_result)))
        for action, state in (("start", "uncertain"), ("start_test", "accepted"), ("start_test", "running")):
            journal.db.execute("UPDATE managed_requests SET action=?,state=? WHERE id=?", (action, state, uncertain_id))
            with pytest.raises(RecoveryRequiredError):
                acknowledge_start_attempts(journal, fault.book, rig.spec, native)
        journal.db.execute(
            "UPDATE managed_requests SET action='start_test',state='uncertain' WHERE id=?", (uncertain_id,)
        )
        journal.put("managed_native_failure", {**failure, "at_epoch": now - 60})
        with pytest.raises(RecoveryRequiredError):
            acknowledge_start_attempts(journal, fault.book, rig.spec, native)
        journal.put("managed_native_failure", failure)
        attempt["startTime"] -= 60
        old_attempt = await rig.prepared.observer.observe_handover()
        with pytest.raises(RecoveryRequiredError):
            acknowledge_start_attempts(journal, fault.book, rig.spec, old_attempt)
        attempt["startTime"] += 60
        original_native = rig.peer.native
        with monkeypatch.context() as patch:
            patch.setattr(rig.peer, "native", lambda key: [] if key == "STATE" else original_native(key))
            with pytest.raises(NativeContentError) as refusal:
                await rig.prepared.observer.observe_handover()
        modern_failure = {
            "at_epoch": now,
            "type": diagnostic_type(refusal.value),
            "detail": str(refusal.value),
        }
        assert modern_failure == failure
        journal.put("managed_native_failure", modern_failure)
        journal.put("managed_control_fault", fault_record | {"native_failure_digest": fingerprint(modern_failure)})
        before = database(journal)
        with pytest.raises(RecoveryRequiredError):
            acknowledge_start_attempts(journal, fault.book, rig.spec, native)
        assert database(journal) == before
        journal.put("managed_control_fault", fault_record)
        journal.db.execute(
            "CREATE TEMP TRIGGER reject_recovery BEFORE INSERT ON meta WHEN NEW.key='native_start_attempt_recovery' "
            "BEGIN SELECT RAISE(ABORT,'failed receipt write'); END"
        )
        before = database(journal)
        with pytest.raises(sqlite3.IntegrityError):
            acknowledge_start_attempts(journal, fault.book, rig.spec, native)
        assert database(journal) == before
        journal.db.execute("DROP TRIGGER reject_recovery")
        result = acknowledge_start_attempts(journal, fault.book, rig.spec, native)
        assert result["unassigned_attempts"] == 1
        assert journal.get("managed_control_intent") == {"mode": "paused", "revision": initial["revision"] + 1}
        assert journal.get("native_start_attempt_recovery")["prior_failure"] == failure
        assert journal.get("native_start_attempt_recovery")["superseded_test_proofs"] == [
            {"request_id": uncertain_id, "verified_pause_id": pause_id}
        ]
        assert tuple(
            journal.db.execute("SELECT state,result FROM managed_requests WHERE id=?", (uncertain_id,)).fetchone()
        ) == ("uncertain", uncertain_result)
        assert journal.get("control")["inhibit"] and journal.active() is None
        assert not starts(rig)
        journal.close()
        replacement = managed_session(rig)
        try:
            await replacement.start()
            async with rig.charger(replacement) as charger:
                await introduced(rig, replacement, charger, "attempt-recovery-boot")
                state = await healthy(ManagedController(replacement))
                assert state["local_hold_verified"] and state["station_ceiling_a"] == 0 and not starts(rig)
                assert state["native_limit_a"] == native_limit
        finally:
            await replacement.close()
            fault.journal = Journal(journal.path, journal.identity)


# Intent: a history repair cannot clear a fault whose earlier observation proved a different contradiction.
# Why: the final missing-history message alone does not establish what the original fault covered.
async def test_history_ack_refuses_a_mixed_native_fault(tmp_path, tls, unused_tcp_port, monkeypatch):
    async with missing_history(tmp_path, tls, unused_tcp_port, monkeypatch, paused=True, mixed=True) as fault:
        journal, rig = fault.journal, fault.rig
        saved = journal.fault.read()
        diagnostic = journal.get("managed_native_failure")
        assert diagnostic.get("source") is None
        native = await rig.prepared.observer.observe_handover()
        with pytest.raises(RecoveryRequiredError, match="matching native fault"):
            acknowledge_history_policy(journal, fault.book, rig.spec, native)
        assert journal.fault.read() == saved and journal.get("managed_native_failure") == diagnostic
        assert journal.get("native_history_policy") is None


# Intent: an independent history repair can settle only a genuine, physically closed Stop.
# Why: the missing history otherwise blocks the Stop receipt and that receipt blocks the repair.
async def test_history_acknowledgment_settles_exact_closed_stop(tmp_path, tls, unused_tcp_port, monkeypatch):
    receipt = uuid.uuid4().hex
    async with missing_history(tmp_path, tls, unused_tcp_port, monkeypatch, closing_stop=receipt) as fault:
        journal, rig = fault.journal, fault.rig
        native = await rig.prepared.observer.observe_handover()
        requests = ManagedRequests(journal)
        assert requests.receipt(receipt)["state"] in ("running", "uncertain")
        unrelated = uuid.uuid4().hex
        requests.accept(unrelated, "app_control", requests.intent().revision)
        before = database(journal)
        with pytest.raises(RecoveryRequiredError, match="settled journal"):
            acknowledge_history_policy(journal, fault.book, rig.spec, native)
        assert database(journal) == before

        journal.db.execute("DELETE FROM managed_requests WHERE id=?", (unrelated,))
        calls = copy.deepcopy(rig.bench.device_calls)
        assert acknowledge_history_policy(journal, fault.book, rig.spec, native)["fault_cleared"] is True
        after = requests.receipt(receipt)
        assert after["state"] == "completed" and after["result"]["session_stopped"] is True
        assert rig.bench.device_calls == calls and rig.bench.firmware.effective_limit() == 0


# Intent: repair history while leaving the real mapped Stop queued for the normal cloud delivery owner.
# Why: the restarted proxy cannot deliver its recorded Stop until it admits the independently checked history.
@pytest.mark.parametrize("vendor_id", [0, 9001])
@pytest.mark.parametrize("queued_sample", ["none", "before-stop", "after-stop", "station-meter", "diagnostics"])
async def test_cold_history_acknowledgement_keeps_queued_exact_stop(
    tmp_path, tls, unused_tcp_port, vendor_id, queued_sample
):
    from ics_proxy_core.engine.native_history import MissingOriginalHistoryError

    originals = closed_records()
    async with managed_bench(tmp_path, tls, unused_tcp_port, initial_history=originals) as rig:
        first = managed_session(rig)
        await first.start()
        c = ManagedController(first)
        rig.route_to_trial()
        async with rig.charger(first) as charger:
            await introduced(rig, first, charger, "before-queued-stop")
            await healthy(c)
            rig.bench.next_vendor = vendor_id - 1
            local = await positive_flow(ChargeRig(rig, first, c, charger), "daily")
            await healthy(c)
            target = c.session_stop.target()
            assert target is not None
            j = first.journal
            path, identity = j.path, j.identity
            assert j.vendor_id(local) == vendor_id
            await first.runtime.stop_vendor()
            meter = {
                "connectorId": 1,
                "transactionId": local,
                "meterValue": [{"timestamp": START["timestamp"], "sampledValue": [{"value": "1100"}]}],
            }
            if queued_sample == "before-stop":
                assert (await rig.bench.call(charger, "actual-meter", "MeterValues", meter))[0] == 3
            elif queued_sample == "station-meter":
                station = {"connectorId": 0, "meterValue": meter["meterValue"]}
                assert (await rig.bench.call(charger, "station", "MeterValues", station))[0] == 3
            elif queued_sample == "diagnostics":
                report = await rig.bench.call(charger, "diag", "DiagnosticsStatusNotification", {"status": "Idle"})
                assert report[0] == 3
            request = uuid.uuid4().hex
            c.command(request, "stop_session", c.requests.intent().revision, target=target)
            await until(lambda: j.outcomes.accepted_stop(local))
            rig.peer.permission_enabled = False
            stop = {"transactionId": local, "timestamp": START["timestamp"], "meterStop": 1100}
            assert (await rig.bench.call(charger, "actual-queued-stop", "StopTransaction", stop))[0] == 3
            if queued_sample == "after-stop":
                assert (await rig.bench.call(charger, "actual-meter", "MeterValues", meter))[0] == 3
            queued = [tuple(row) for row in j.db.execute("SELECT * FROM outbox ORDER BY seq")]
            expected = {
                "before-stop": ["MeterValues", "StopTransaction"],
                "after-stop": ["StopTransaction", "MeterValues"],
                "station-meter": ["MeterValues", "StopTransaction"],
                "diagnostics": ["DiagnosticsStatusNotification", "StopTransaction"],
                "none": ["StopTransaction"],
            }
            assert [row[2] for row in queued] == expected[queued_sample]
            stop_token = next(row[1] for row in queued if row[2] == "StopTransaction")
            await first.close()

        rig.bench.history = originals[1:]
        replacement = managed_session(rig)
        try:
            with pytest.raises(MissingOriginalHistoryError):
                await replacement.start()
            assert not replacement.started
        finally:
            await replacement.close()
        j = Journal(path, identity)
        try:
            assert j.fault.classification() == "native_authority_or_preservation_failed"
            assert [tuple(row) for row in j.db.execute("SELECT * FROM outbox ORDER BY seq")] == queued
            sample = await rig.prepared.observer.observe_handover()
            book = ResourceBook(j, rig.prepared.baseline)
            j.db.execute("UPDATE outbox SET payload='{}' WHERE token=?", (stop_token,))
            before = database(j)
            with pytest.raises(RecoveryRequiredError):
                acknowledge_history_policy(j, book, rig.spec, sample)
            assert database(j) == before

            payload = j.db.execute("SELECT stop FROM transactions WHERE local=?", (local,)).fetchone()[0]
            j.db.execute("UPDATE outbox SET payload=? WHERE token=?", (payload, stop_token))
            if queued_sample == "station-meter":
                meter_token, meter_payload = queued[0][1], queued[0][3]
                j.db.execute("UPDATE outbox SET payload='{}' WHERE token=?", (meter_token,))
                before = database(j)
                with pytest.raises(RecoveryRequiredError):
                    acknowledge_history_policy(j, book, rig.spec, sample)
                assert database(j) == before
                j.db.execute("UPDATE outbox SET payload=? WHERE token=?", (meter_payload, meter_token))
            assert acknowledge_history_policy(j, book, rig.spec, sample)["fault_cleared"] is True
            assert [tuple(row) for row in j.db.execute("SELECT * FROM outbox ORDER BY seq")] == queued
            assert ManagedRequests(j).receipt(request)["state"] == "completed"
        finally:
            j.close()

        recovered = managed_session(rig)
        await recovered.start()
        try:
            async with rig.charger(recovered) as charger:
                await introduced(rig, recovered, charger, "after-queued-stop")
                await until(lambda: recovered.journal.next_delivery() is None)
                wires = [frame for frame in rig.bench.calls if frame[2] in expected[queued_sample]]
                assert [(wire[1], wire[2], wire[3].get("transactionId")) for wire in wires] == [
                    (row[1], row[2], None if row[4] is None else vendor_id) for row in queued
                ]
                assert not starts(rig)
        finally:
            await recovered.close()
