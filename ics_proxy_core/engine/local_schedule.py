"""Admit one commissioned overnight occurrence through the existing Start and Stop owner."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import UTC, datetime
from functools import partial
from math import floor, isfinite
from typing import TYPE_CHECKING, Any

from .command_outcomes import transaction_reply_status
from .command_processor import CommandPriority
from .connection_scope import ControlScope, control_scope_matches, current_control_scope
from .local_schedule_cutoff import cutoff
from .local_schedule_window import ScheduleCutoff, schedule_window
from .managed_intent import LOCAL_SCHEDULE_OCCURRENCE as OCCURRENCE
from .managed_intent import ControlIntent, automatic_pause_cause
from .policy import DeniedError
from .recovery_contracts import ALLOW, GateDecision, Pending, Prerequisite, require_allowed
from .rpc import UnavailableError
from .schedule_clock import host_clock_blocker, host_clock_epoch, host_clock_synchronized
from .security import SecurityError
from .session_closure import observe_closure
from .start_handoff import ScheduledStop
from .store import RecoveryRequiredError, encoded, fingerprint

if TYPE_CHECKING:
    from ..frames import Frame
    from .local_schedule_window import ScheduleWindow
    from .managed import ManagedController
    from .managed_charging import ChargeLease
    from .schedule_clock import ClockBlocker
    from .start_handoff import OwnedStartResult
    from .trial_handover_evidence import HandoverEvidence


NATIVE_OCCURRENCE = "managed_native_schedule_occurrence"
SCHEDULE_CUTOFF = "schedule_cutoff"
_SCHEDULE_CLOSURE = "managed_local_schedule_native_closure"


class ScheduleCloudReturnedError(DeniedError):
    """The current iCS connection superseded an unissued scheduled Start."""


class ScheduleCutoffError(DeniedError):
    """The exact admitted overnight window reached its cutoff."""


class LocalSchedule:
    """An outage can delegate an unchanged native plan to the normal local owner once per window."""

    # The occurrence lives in the same locked journal as its management request and charger reports.
    def __init__(self, controller: ManagedController, timezone: str | None) -> None:
        self.controller = controller
        self.timezone = timezone
        self.journal = controller.session.journal
        self.process_deadline: tuple[str, float] | None = None
        self.clock_epoch = host_clock_epoch() if timezone is not None else None
        self.expired_request: str | None = None
        self.closed_request: str | None = None
        self.preparing_charger_cutoff: tuple[str, str, ControlScope] | None = None
        self.armed_charger_cutoff: tuple[dict[str, Any], ControlScope] | None = None

    # Only an automatic scheduled Start needs to arm a charger-side timer.
    def cutoff_preparation(self, lease: ChargeLease) -> Callable[[str], Awaitable[ScheduledStop]] | None:
        if lease.schedule is None:
            return None
        return partial(self.prepare_charger_cutoff, lease)

    # Mark the occurrence in the same transaction as the exact local Start receipt.
    def mark_charger_cutoff_required(self, lease: ChargeLease) -> None:
        c, j = self.controller, self.journal
        occurrence = j.get(OCCURRENCE, {})
        scope = current_control_scope(c.session.gateway)
        ticket = j.get("control", {}).get("ticket")
        if (
            not j.db.in_transaction
            or scope is None
            or not isinstance(ticket, str)
            or occurrence.get("request") != lease.identifier
        ):
            raise RecoveryRequiredError("charger cutoff preparation lost its scheduled Start")
        j.put(OCCURRENCE, {**occurrence, "charger_cutoff_required": True})
        self.preparing_charger_cutoff = lease.identifier, ticket, scope

    # Start's reply identifies no session. Wait for the exact journalled StartTransaction.
    async def prepare_charger_cutoff(self, lease: ChargeLease, ticket: str) -> ScheduledStop:
        c, j = self.controller, self.journal
        gateway = c.session.gateway
        preparation = self.preparing_charger_cutoff
        if preparation is None or preparation[:2] != (lease.identifier, ticket):
            raise RecoveryRequiredError("charger cutoff has no live preparation")
        _, _, scope = preparation

        # Require the same accepted Start receipt and control scope before using the occurrence.
        def current() -> dict[str, Any]:
            c.charging.guard(lease)
            occurrence: dict[str, Any] = j.get(OCCURRENCE, {})
            receipt = j.starts.receipt()
            if (
                not control_scope_matches(gateway, scope)
                or j.get("control", {}).get("ticket") != ticket
                or occurrence.get("request") != lease.identifier
                or occurrence.get("charger_cutoff_required") is not True
                or receipt.get("ticket") != ticket
                or receipt.get("request_id") != lease.identifier
                or receipt.get("stage") != "accepted"
            ):
                raise RecoveryRequiredError("charger cutoff lost the exact accepted Start")
            return occurrence

        while (local := current().get("local")) is None:
            if j.active() is not None:
                raise RecoveryRequiredError("an unrelated session arrived before the scheduled Start")
            await asyncio.sleep(0.05)
        if type(local) is not int:
            raise RecoveryRequiredError("charger cutoff has an invalid local session")

        occurrence = current()
        deadline = self._elapsed_deadline(occurrence)
        if deadline is None:
            raise RecoveryRequiredError("charger cutoff has no comparable elapsed deadline")
        now = time.time()
        remaining = deadline - time.monotonic()
        # The charger accepts whole seconds. Leave a second for clock sampling differences at later guards.
        stop_at = floor(min(occurrence["window"]["ends_at"], now + remaining) - 1)

        # Keep the cutoff bound to the live owner session and original elapsed deadline.
        def guard() -> None:
            occurrence = current()
            if (
                occurrence.get("local") != local
                or j.active() != local
                or j.starts.receipt().get("local") != local
                or not j.is_owner_session(local)
            ):
                raise RecoveryRequiredError("charger cutoff lost its exact active session")
            now = time.time()
            if not 0 < stop_at - now <= deadline - time.monotonic():
                raise ScheduleCutoffError("charger cutoff no longer fits the original deadline")

        guard()
        native = await c.native.read()
        guard()
        require_allowed(c.native._confirm_send_zero(native, local))
        stop_time = datetime.fromtimestamp(stop_at, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        payload = {
            "vendorId": "IoCharger",
            "messageId": "delayStop",
            "data": encoded({"transactionId": local, "stopTime": stop_time}),
        }

        # Retain the reply before any positive cap. Its authority is intentionally process-local.
        def record(frame: Frame) -> None:
            guard()
            proof = {"request": lease.identifier, "local": local, "stop_at": stop_at, "wire": frame[1]}
            j.put(OCCURRENCE, {**current(), "charger_cutoff_armed": proof})
            self.armed_charger_cutoff = proof, scope

        return ScheduledStop(payload, guard, record)

    # Match the durable timer proof to the process-local charger scope that holds it.
    def _current_charger_cutoff(self, occurrence: dict[str, Any]) -> bool:
        armed = self.armed_charger_cutoff
        return (
            armed is not None
            and occurrence.get("charger_cutoff_armed") == armed[0]
            and occurrence.get("request") == armed[0]["request"]
            and occurrence.get("local") == armed[0]["local"]
            and control_scope_matches(self.controller.session.gateway, armed[1])
        )

    # A replacement controller cannot continue the former lease's in-flight timer preparation.
    def _cutoff_preparation_current(self, occurrence: dict[str, Any]) -> bool:
        c = self.controller
        lease, preparation = c.charging.lease, self.preparing_charger_cutoff
        return (
            lease is not None
            and preparation is not None
            and lease.identifier == occurrence.get("request") == preparation[0]
            and self.journal.get("control", {}).get("ticket") == preparation[1]
            and c.requests.intent() == ControlIntent(lease.mode, lease.revision)
            and control_scope_matches(c.session.gateway, preparation[2])
        )

    # Record the exact source of a same-tag session while its independent plan marker is available.
    def observe_native_occurrence(self, native: HandoverEvidence) -> None:
        if self.timezone is None or native.session_plan_id is None or not native.physical.transaction_open:
            return
        c, j = self.controller, self.journal
        local = j.active()
        if (
            local is None
            or native.physical.native_transaction_id != local
            or not native.is_fresh(5)
            or j.is_owner_session(local)
        ):
            return
        from .gateway import timestamp

        start = json.loads(j.db.execute("SELECT start FROM transactions WHERE local=?", (local,)).fetchone()[0])
        occurred = timestamp(start["timestamp"]).timestamp()
        window = schedule_window(native, c.native.spec.saved_schedule_digest, self.timezone, occurred)
        if window is None:
            return
        saved, bounds = j.get(NATIVE_OCCURRENCE, {}), asdict(window)
        if saved and (
            saved["window"]["starts_at"] > window.starts_at or "local" in saved and window.overlaps(saved["window"])
        ):
            return
        if saved.get("window") != bounds:
            saved = {"window": bounds}
        if fingerprint(start["idTag"].casefold()) != window.tag_digest:
            return
        digest = fingerprint(start)
        if native.session_plan_id == window.plan_id:
            j.put(NATIVE_OCCURRENCE, {"window": bounds, "local": local, "start": digest})
        else:
            unrelated = saved.get("unrelated", {})
            if unrelated.get(str(local)) != digest:
                j.put(NATIVE_OCCURRENCE, {**saved, "unrelated": {**unrelated, str(local): digest}})

    # Use the same accepted report timestamp and tag for both witnessed and unwitnessed sessions.
    @staticmethod
    def _matches_window(window: ScheduleWindow, start: dict[str, Any]) -> bool:
        from .gateway import timestamp

        if fingerprint(start["idTag"].casefold()) != window.tag_digest:
            return False
        occurred = timestamp(start["timestamp"]).timestamp()
        return window.starts_at <= occurred < window.ends_at

    # A same-tag Start can use the window even if it closes before an audit sees its native plan marker.
    def _native_window_used(self, window: ScheduleWindow) -> bool:
        j = self.journal
        saved = j.get(NATIVE_OCCURRENCE, {})
        if "local" in saved and window.overlaps(saved["window"]):
            return True
        if saved.get("window") != asdict(window):
            saved = {}
        unrelated = saved.get("unrelated", {})
        transactions = j.db.execute("SELECT local,start FROM transactions WHERE connector=1 ORDER BY local DESC")
        for local, payload in transactions:
            start = json.loads(payload)
            if (
                self._matches_window(window, start)
                and not j.is_owner_session(local)
                and unrelated.get(str(local)) != fingerprint(start)
            ):
                return True
        return False

    # Outage fallback can use shared control or the exact automatic Pause for the current revision.
    def _outage_permission(self, intent: ControlIntent) -> bool:
        c = self.controller
        return not c.session.runtime.vendor_registered and (
            intent.mode == "app" or c.requests.offline_pause_current(intent)
        )

    # Civil time cannot establish whether a scheduled window is due until the clock is trusted.
    def clock_blocker(self, intent: ControlIntent) -> ClockBlocker | None:
        if self.timezone is not None and self._outage_permission(intent):
            return host_clock_blocker()
        return None

    # Admission is only from shared control or the exact clean automatic Pause caused by an outage.
    def consider(self, native: HandoverEvidence) -> None:
        if self.timezone is None:
            return
        c = self.controller
        intent, now = c.requests.intent(), time.time()
        if not self._outage_permission(intent) or not host_clock_synchronized():
            return
        window = schedule_window(native, c.native.spec.saved_schedule_digest, self.timezone, now)
        if window is None or c.current.selected() > window.limit_a or not c.charging.available(native, normal=True):
            return
        identifier = fingerprint(["local-schedule", c.native.spec.digest, asdict(window)])[:32]
        previous = self.journal.get(OCCURRENCE, {})
        if previous and (previous["window"]["starts_at"] >= window.starts_at or window.overlaps(previous["window"])):
            if (
                previous["window"] != asdict(window)
                or previous.get("local") is not None
                or previous.get("request") != identifier
                or not self._proved_unsent(identifier, intent.revision)
            ):
                return
            identifier = fingerprint(["local-schedule-unsent-retry", identifier])[:32]
        try:
            # This owner turn checks earlier reports and installs the lease that fences later reports.
            if self._native_window_used(window):
                return
            c.charging.accept(identifier, "start", intent.revision, schedule=window)
        except DeniedError, RecoveryRequiredError:
            return  # The expected authority or time can expire before admission.
        except sqlite3.Error:
            if self.journal.db.in_transaction:
                raise
            return  # Retry a failed read on the next poll. A committed admission retains its worker and cutoff.

    # A single replacement admission is safe only when the original Start had no physical effects or wire.
    def _proved_unsent(self, identifier: str, revision: int) -> bool:
        j = self.journal
        request, state = j.outcomes.owner(identifier), self.controller.charging.state()
        failure = state.get("failure", {})
        return bool(
            request is not None
            and request.action == "start"
            and request.revision == revision
            and request.state == "failed"
            and request.result.get("classification") == "preflight_refused_unsent"
            and state.get("id") == identifier
            and state.get("state") == "completed"
            and state.get("start_attempted") is False
            and state.get("start_rpc_accepted") is False
            and state.get("local_transaction") is None
            and isinstance(failure, dict)
            and failure.get("id") == identifier
            and failure.get("effects_started") is False
            and j.starts.receipt().get("request_id") != identifier
            and not j.outcomes.owner_wires("request", identifier)
        )

    # Sample both clocks before admission so a later wall-clock correction cannot extend this occurrence.
    def sample_cutoff(self, window: ScheduleWindow) -> tuple[float, ScheduleCutoff]:
        if self.timezone is None:
            raise DeniedError("automatic overnight Start requires a configured time zone")
        if not host_clock_synchronized():
            raise DeniedError("the host clock is not synchronized for scheduled Start")
        elapsed = time.monotonic()
        now = time.time()
        if not window.starts_at <= now < window.ends_at:
            raise DeniedError("the scheduled Start window changed before admission")
        deadline = elapsed + (window.ends_at - now)
        previous = self.journal.get(OCCURRENCE, {})
        if previous.get("window") == asdict(window):
            original = self._elapsed_deadline(previous)
            if self._elapsed(previous, now) or original is None:
                raise DeniedError("the original scheduled Start window reached its cutoff")
            deadline = min(deadline, original)
        return deadline - elapsed, ScheduleCutoff(self.clock_epoch, deadline)

    # The startup watchdog may expire before the overnight cutoff. Only the exact latter owns automatic recovery.
    def cutoff_elapsed(self, lease: ChargeLease) -> bool:
        occurrence = self.journal.get(OCCURRENCE, {})
        return (
            lease.schedule is not None
            and self.timezone is not None
            and occurrence.get("request") == lease.identifier
            and occurrence.get("window") == asdict(lease.schedule)
            and occurrence.get("stop_revision") is None
            and self._elapsed(occurrence, time.time())
        )

    # This restriction runs at each existing lease boundary, including nested charger authorization.
    def require_lease(self, lease: ChargeLease, *, finishing_initial_zero: bool = False) -> None:
        window = lease.schedule
        if window is None:
            return
        c, occurrence = self.controller, self.journal.get(OCCURRENCE, {})
        receipt = self.journal.starts.receipt()
        original_dispatched = receipt.get("request_id") == lease.identifier and receipt.get("stage") in (
            "issued",
            "accepted",
        )
        now = time.time()
        if (
            occurrence.get("request") != lease.identifier
            or occurrence.get("window") != asdict(window)
            or lease.owner_start_tag is None
            or self.timezone is None
            or occurrence.get("stop_revision") is not None
        ):
            raise DeniedError("local schedule occurrence no longer owns Start")
        if self._elapsed(occurrence, now):
            raise ScheduleCutoffError("local schedule window reached its cutoff")
        if not finishing_initial_zero and not host_clock_synchronized():
            raise DeniedError("the host clock is not synchronized for scheduled Start")
        if c.session.runtime.vendor_registered and not original_dispatched and not finishing_initial_zero:
            raise ScheduleCloudReturnedError("iCS returned before the local schedule issued Start")

    # A receipt that may have reached the charger never qualifies as a clean scheduled cancellation.
    def _unsent(self, identifier: str, state: dict[str, Any]) -> bool:
        j = self.journal
        return (
            j.get(OCCURRENCE, {}).get("local") is None
            and state.get("id") == identifier
            and state.get("start_rpc_accepted") is False
            and state.get("local_transaction") is None
            and j.active() is None
            and not j.get("control", {}).get("start_pending")
            and not any(
                wire.action == "RemoteStartTransaction" for wire in j.outcomes.owner_wires("request", identifier)
            )
        )

    # An exact rejected or issued receipt cannot serve as proof of an unissued Start.
    def _unsent_receipt(self, identifier: str, state: dict[str, Any], receipt: dict[str, Any]) -> bool:
        return (
            state.get("start_attempted") is True
            and receipt.get("request_id") == identifier
            and receipt.get("digest") == state.get("tag_digest")
            and receipt.get("owner_authorized") is True
            and receipt.get("stage") == "unsent"
            and receipt.get("local") is None
            and isinstance(receipt.get("ticket"), str)
        )

    # Verify the exact initial hold or rollback before the shared owner may request automatic Return.
    def verified_unsent_zero(self, lease: ChargeLease, result: OwnedStartResult | None) -> bool:
        c, j = self.controller, self.journal
        gateway = c.session.gateway
        controls = gateway.controls
        occurrence, control = j.get(OCCURRENCE, {}), j.get("control", {})
        receipt, state = j.starts.receipt(), c.charging.state()
        if (
            lease.schedule is None
            or occurrence.get("request") != lease.identifier
            or occurrence.get("window") != asdict(lease.schedule)
            or not self._unsent(lease.identifier, state)
            or not controls.ready
            or controls.desired() != 0
        ):
            return False
        if result is None:
            return (
                state.get("start_attempted") is False
                and receipt.get("request_id") != lease.identifier
                and lease.initial_zero_ticket is not None
                and control.get("ticket") == lease.initial_zero_ticket
                and controls.boot() == lease.initial_zero_boot
            )
        return (
            result.zero_verified
            and not result.remote_start_dispatched
            and result.answer is None
            and result.publication is None
            and lease.start_scope is not None
            and control_scope_matches(gateway, (lease.start_scope, lease.control_revision))
            and self._unsent_receipt(lease.identifier, state, receipt)
            and receipt["ticket"] == receipt.get("settled_ticket") == control.get("ticket")
        )

    # A failed Start belongs to the schedule only while the accepted session and its rollback still agree.
    def verified_failed_schedule_zero(self, lease: ChargeLease, result: OwnedStartResult | None) -> bool:
        c, j = self.controller, self.journal
        gateway = c.session.gateway
        if (
            lease.schedule is None
            or lease.mode != "app"
            or c.requests.intent() != ControlIntent("app", lease.revision)
            or result is None
            or not result.remote_start_dispatched
            or result.answer is None
            or transaction_reply_status(result.answer) != "Accepted"
            or not result.zero_verified
            or result.publication is not None
            or lease.start_scope is None
            or not control_scope_matches(gateway, (lease.start_scope, lease.control_revision))
            or j.fault.latched()
        ):
            return False
        occurrence, receipt = j.get(OCCURRENCE, {}), j.starts.receipt()
        local, ticket = occurrence.get("local"), receipt.get("ticket")
        return (
            occurrence.get("request") == receipt.get("request_id") == lease.identifier
            and occurrence.get("window") == asdict(lease.schedule)
            and occurrence.get("charger_cutoff_required") is True
            and occurrence.get("stop_revision") is None
            and receipt.get("stage") == "accepted"
            and receipt.get("owner_authorized") is True
            and receipt.get("local") == local
            and j.is_owner_session(local)
            and j.active() in (None, local)
            and isinstance(ticket, str)
            and j.get("control", {}).get("ticket") == ticket
            and gateway.controls.ready
            and gateway.controls.desired() == 0
        )

    # Only an exact accepted scheduled Start issued under zero can carry its unresolved report forward.
    def _unreported_timer_start(
        self, identifier: str, occurrence: dict[str, Any], receipt: dict[str, Any], control: dict[str, Any]
    ) -> bool:
        return (
            occurrence.get("request") == receipt.get("request_id") == identifier
            and occurrence.get("charger_cutoff_required") is True
            and occurrence.get("local") is None
            and occurrence.get("stop_revision") is None
            and receipt.get("stage") == "accepted"
            and receipt.get("owner_authorized") is True
            and receipt.get("held_at_zero") is True
            and receipt.get("local") is None
            and control.get("start_pending") is True
            and control.get("owner") == "local"
            and control.get("inhibit") is True
            and self.journal.active() is None
            and not self.journal.fault.latched()
        )

    # Remember that the schedule owns this Pause while the accepted Start still owes its report.
    def accepted_unreported_at_zero(self, lease: ChargeLease) -> bool:
        if lease.schedule is None:
            return False
        c, j = self.controller, self.journal
        occurrence, receipt, control = j.get(OCCURRENCE, {}), j.starts.receipt(), j.get("control", {})
        controls = c.session.gateway.controls
        return (
            self._cutoff_preparation_current(occurrence)
            and occurrence.get("window") == asdict(lease.schedule)
            and receipt.get("ticket") == control.get("ticket")
            and self._unreported_timer_start(lease.identifier, occurrence, receipt, control)
            and controls.ready
            and controls.desired() == 0
        )

    # A restart can renew the exact saved restrictive claim without changing its automatic Pause revision.
    def unreported_pause_current(self, saved: dict[str, Any], intent: ControlIntent) -> bool:
        c, j = self.controller, self.journal
        if not c.requests.offline_pause_current(intent) or automatic_pause_cause(j, intent) != "local_charge":
            return False
        identifier, hold = saved.get("id"), j.get("managed_internal_pause", {})
        control = j.get("control", {})
        return (
            isinstance(identifier, str)
            and saved.get("kind") == "normal"
            and saved.get("source") == "local"
            and saved.get("state") == "stopping"
            and saved.get("expired_normal") is True
            and type(saved.get("revision")) is int
            and saved["revision"] + 1 == intent.revision
            and hold.get("id") == identifier
            and hold.get("source") == "test"
            and hold.get("revision") == intent.revision
            and isinstance(hold.get("ticket"), str)
            and hold["ticket"] == control.get("ticket")
            and self._unreported_timer_start(identifier, j.get(OCCURRENCE, {}), j.starts.receipt(), control)
        )

    # If expiry won the wire race, the existing internally owned Pause can prove its own fresh zero later.
    def cutoff_pause_unsent(self, intent: ControlIntent) -> bool:
        c, j = self.controller, self.journal
        state, receipt = c.charging.state(), j.starts.receipt()
        identifier = state.get("id")
        if (
            state.get("stop_reason") != SCHEDULE_CUTOFF
            or state.get("kind") != "normal"
            or state.get("source") != "local"
            or state.get("revision") != intent.revision - 1
            or state.get("accepted_boot") != c.session.gateway.boot_observation
            or not isinstance(identifier, str)
            or j.get(OCCURRENCE, {}).get("request") != identifier
            or c.requests.offline_pause_current(intent)
            or c.requests.return_in_progress()
            or not self._unsent(identifier, state)
        ):
            return False
        request = j.outcomes.owner(identifier)
        return (
            request is not None
            and request.action == "start"
            and request.revision == intent.revision - 1
            and (
                (state.get("start_attempted") is False and receipt.get("request_id") != identifier)
                or self._unsent_receipt(identifier, state, receipt)
            )
        )

    # Fence only the competing native plan tag while the local schedule owns its Start handoff.
    def blocks_tag(self, tag: str, occurred: float | None) -> bool:
        occurrence = self.journal.get(OCCURRENCE, {})
        if not occurrence:
            return False
        window, lease = occurrence["window"], self.controller.charging.lease
        when = time.time() if occurred is None else occurred
        return bool(
            window["tag_digest"] == fingerprint(tag.casefold())
            and window["starts_at"] <= when < window["ends_at"]
            and (occurrence.get("local") is not None or lease is not None and lease.identifier == occurrence["request"])
        )

    # The exact authenticated firmware schedule must still match on a fresh pre-send sample.
    def positive_decision(self, native: HandoverEvidence) -> GateDecision:
        c = self.controller
        lease = c.charging.lease
        if lease is not None and lease.schedule is not None:
            self.require_lease(lease)
            if (
                self.timezone is None
                or schedule_window(native, c.native.spec.saved_schedule_digest, self.timezone, time.time())
                != lease.schedule
            ):
                return Pending(Prerequisite.SOURCE, "the exact commissioned local schedule is no longer current")
        occurrence = self.journal.get(OCCURRENCE, {})
        local = occurrence.get("local")
        if (
            local is not None
            and local == self.journal.active()
            and occurrence.get("charger_cutoff_required") is True
            and not self._current_charger_cutoff(occurrence)
        ):
            return Pending(Prerequisite.SOURCE, "the exact charger cutoff is unconfirmed on this connection")
        if (
            local is not None
            and local == self.journal.active()
            and (self._due(occurrence) or c.current.selected() > occurrence["window"]["limit_a"])
        ):
            return Pending(Prerequisite.SOURCE, "the local schedule does not permit positive current")
        return ALLOW

    # A backward clock step cannot extend an admitted occurrence.
    def _due(self, occurrence: dict[str, Any]) -> bool:
        return (
            occurrence.get("stop_revision") is not None
            or self.timezone is None
            or self._elapsed(occurrence, time.time())
        )

    # Keep the exact occurrence eligible only inside its wall window and within its original elapsed budget.
    def _elapsed(self, occurrence: dict[str, Any], now: float) -> bool:
        request = occurrence["request"]
        if occurrence.get("cutoff_reached") is True:
            return True
        window = occurrence["window"]
        if self.expired_request != request and window["starts_at"] <= now < window["ends_at"]:
            deadline = self._elapsed_deadline(occurrence)
            if deadline is not None and time.monotonic() < deadline:
                return False
        self.expired_request = request
        self._retain_cutoff(occurrence)
        return True

    # A legacy occurrence or a different host clock epoch has no comparable elapsed deadline.
    def _elapsed_deadline(self, occurrence: dict[str, Any]) -> float | None:
        if self.process_deadline is not None and self.process_deadline[0] == occurrence["request"]:
            return self.process_deadline[1]
        clock = occurrence.get("cutoff_clock")
        if isinstance(clock, dict) and self.clock_epoch is not None and clock.get("epoch") == self.clock_epoch:
            deadline = clock.get("deadline")
            if type(deadline) is float and isfinite(deadline):
                return deadline
        return None

    # Retain an observed cutoff even if its exact Stop claim cannot be written yet.
    def _retain_cutoff(self, occurrence: dict[str, Any]) -> None:
        j = self.journal
        if j.db.in_transaction:
            return  # The surrounding ownership transaction retains its own cutoff or Start settlement.

        # The current journal row may already contain independent Start or Stop evidence.
        def retain() -> None:
            latest = j.get(OCCURRENCE, {})
            if latest.get("request") == occurrence["request"] and latest.get("cutoff_reached") is not True:
                j.put(OCCURRENCE, {**latest, "cutoff_reached": True})

        try:
            self.controller.commands.decide(retain, priority=CommandPriority.RESTRICT)
        except sqlite3.Error:
            if j.db.in_transaction:
                raise

    # Claim only the interrupted Start's exact still-open occurrence when its cutoff already applies.
    def due_owner_occurrence(self, identifier: str) -> dict[str, Any] | None:
        j = self.journal
        occurrence: dict[str, Any] = j.get(OCCURRENCE, {})
        local = occurrence.get("local")
        if (
            occurrence.get("request") == identifier
            and type(local) is int
            and local == j.active()
            and j.is_owner_session(local)
            and "stop_revision" not in occurrence
            and self._due(occurrence)
        ):
            return occurrence
        return None

    # Only the latest owner Stop can meet the exact occurrence without an intervening session.
    def _closed_occurrence(self) -> dict[str, Any] | None:
        j = self.journal
        if self.timezone is None or j.active() is not None:
            return None
        occurrence: dict[str, Any] = j.get(OCCURRENCE, {})
        local = occurrence.get("local")
        if type(local) is int and "stop_revision" not in occurrence and j.latest_owner_stop_id() == local:
            return occurrence
        return None

    # Restrictive polls can retry a refused closure write from a fresh sample.
    def closure_probe_needed(self) -> bool:
        occurrence = self._closed_occurrence()
        return (
            occurrence is not None
            and current_control_scope(self.controller.session.gateway) is not None
            and not self.closure_known(occurrence, require_durable=True)
        )

    # Remember settled early closure so its spent window has no physical work left at cutoff.
    def observe_closed(self, native: HandoverEvidence) -> None:
        if not observe_closure(None, native).confirms_idle:
            return
        occurrence = self._closed_occurrence()
        if (
            occurrence is None
            or occurrence.get("native_closed") is True
            or native is not self.controller.native._current_stop_sample(None)
        ):
            return
        j = self.journal
        self.closed_request = occurrence["request"]
        try:
            j.put(OCCURRENCE, {**occurrence, "native_closed": True})
        except sqlite3.Error:
            if j.db.in_transaction:
                raise
            if not self.controller.session_stop.retain_scheduled_closure(occurrence["local"]):
                self._retain_closed_occurrence(occurrence)

    # A spontaneous Stop has no receipt. The latest occurrence needs one independent journal slot.
    def _retain_closed_occurrence(self, occurrence: dict[str, Any]) -> None:
        try:
            if (proof := self._closure_binding(occurrence)) is not None:
                self.journal.put(_SCHEDULE_CLOSURE, proof)
        except sqlite3.Error:
            if self.journal.db.in_transaction:
                raise

    # A replacement can reuse only the proof for the exact scheduled request and its genuine closing report.
    def _closure_binding(self, occurrence: dict[str, Any]) -> str | None:
        report = self.journal.outcomes.closing_report(occurrence["local"])
        if report is None:
            return None
        return fingerprint((occurrence["request"], report.local, report.body))

    # Either journal path can preserve the same physical proof across process replacement.
    def closure_known(self, occurrence: dict[str, Any], *, require_durable: bool = False) -> bool:
        if (
            occurrence.get("native_closed") is True
            or (not require_durable and self.closed_request == occurrence["request"])
            or self.controller.session_stop.closure_verified(occurrence["local"], require_durable=require_durable)
        ):
            return True
        try:
            proof = self.journal.get(_SCHEDULE_CLOSURE)
        except ValueError, TypeError, RecursionError:
            return False
        return isinstance(proof, str) and proof == self._closure_binding(occurrence)

    # An issued Start can lose its positive authority before there is a transaction number to Stop.
    async def hold_due_unbound_start(self) -> None:
        c, j = self.controller, self.journal

        # Retire the exact pending wire in the same turn that checks its occurrence and current owner.
        def expire() -> bool:
            lease = c.charging.lease
            if (
                c.stopping
                or lease is None
                or lease.schedule is None
                or c.requests.intent() != ControlIntent(lease.mode, lease.revision)
                or c.requests.return_in_progress()
                or j.active() is not None
                or j.get(OCCURRENCE, {}).get("local") is not None
            ):
                return False
            receipt = j.starts.receipt()
            if (
                receipt.get("request_id") != lease.identifier
                or receipt.get("owner_authorized") is not True
                or receipt.get("stage") not in ("issued", "accepted")
                or receipt.get("local") is not None
                or j.get("control", {}).get("start_pending") is not True
                or not self.cutoff_elapsed(lease)
            ):
                return False
            c.charging.stop(lease, SCHEDULE_CUTOFF)
            return c.charging.scoped_normal_stop()

        try:
            if c.commands.decide(expire, priority=CommandPriority.RESTRICT):
                await c.charging.hold_expired()
        except DeniedError, SecurityError, RecoveryRequiredError, UnavailableError, TimeoutError, sqlite3.Error:
            if j.db.in_transaction:
                raise
            # A refused claim or zero attempt remains eligible for restrictive supervision.

    # Periodic restrictive work can stop its own session even when no complete native poll is available.
    async def stop_if_due(self) -> None:
        c, j = self.controller, self.journal
        occurrence = j.get(OCCURRENCE, {})
        if not occurrence:
            return
        local = occurrence.get("local")
        if local is None:
            await self.hold_due_unbound_start()
            return
        active = j.active()
        abandoned_cutoff = (
            active == local
            and occurrence.get("charger_cutoff_required") is True
            and not self._current_charger_cutoff(occurrence)
            and not self._cutoff_preparation_current(occurrence)
        )
        if c.stopping or type(local) is not int or not (abandoned_cutoff or self._due(occurrence)):
            return
        if active != local and (active is not None or j.latest_owner_stop_id() != local):
            return
        if not j.is_owner_session(local):
            raise RecoveryRequiredError("local schedule lost its original owner session")
        await cutoff(c, local)
