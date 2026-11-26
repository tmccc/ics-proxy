"""Admit one commissioned overnight occurrence through the existing Start and Stop owner."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from .connection_scope import control_scope_matches
from .local_schedule_cutoff import cutoff
from .local_schedule_window import schedule_window
from .managed_intent import LOCAL_SCHEDULE_OCCURRENCE as OCCURRENCE
from .policy import DeniedError
from .recovery_contracts import ALLOW, GateDecision, Pending, Prerequisite
from .store import RecoveryRequiredError, fingerprint

if TYPE_CHECKING:
    from .local_schedule_window import ScheduleWindow
    from .managed import ManagedController
    from .managed_charging import ChargeLease
    from .managed_intent import ControlIntent
    from .start_handoff import OwnedStartResult
    from .trial_handover_evidence import HandoverEvidence


NATIVE_OCCURRENCE = "managed_native_schedule_occurrence"
SCHEDULE_CUTOFF = "schedule_cutoff"


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
        self.deadline: tuple[str, float] | None = None

    # An independently identified native run consumes the window without giving the fallback ownership of its Stop.
    def observe_native_occurrence(self, native: HandoverEvidence) -> None:
        from .gateway import timestamp

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
        window = schedule_window(native, c.native.spec.saved_schedule_digest, self.timezone, time.time())
        if window is None or native.session_plan_id != window.plan_id:
            return
        saved = j.get(NATIVE_OCCURRENCE)
        if saved is not None and saved["window"]["starts_at"] >= window.starts_at:
            return
        start = json.loads(j.db.execute("SELECT start FROM transactions WHERE local=?", (local,)).fetchone()[0])
        occurred = timestamp(start["timestamp"]).timestamp()
        tag = fingerprint(start["idTag"].casefold())
        if window.starts_at <= occurred < window.ends_at and tag == window.tag_digest:
            j.put(NATIVE_OCCURRENCE, {"window": asdict(window), "local": local, "start": fingerprint(start)})

    # Admission is only from shared control or the exact clean automatic Pause caused by an outage.
    def consider(self, native: HandoverEvidence) -> None:
        c = self.controller
        intent, now = c.requests.intent(), time.time()
        if (
            self.timezone is None
            or c.session.runtime.vendor_registered
            or intent.mode != "app"
            and not c.requests.offline_pause_current(intent)
        ):
            return
        window = schedule_window(native, c.native.spec.saved_schedule_digest, self.timezone, now)
        if window is None or c.current.selected() > window.limit_a or not c.charging.available(native, normal=True):
            return
        native_occurrence = self.journal.get(NATIVE_OCCURRENCE)
        if native_occurrence is not None and native_occurrence["window"] == asdict(window):
            return
        identifier = fingerprint(["local-schedule", c.native.spec.digest, asdict(window)])[:32]
        previous = self.journal.get(OCCURRENCE, {})
        if previous and previous["window"]["starts_at"] >= window.starts_at:
            if (
                previous["window"] != asdict(window)
                or previous.get("local") is not None
                or previous.get("request") != identifier
                or not self._proved_unsent(identifier, intent.revision)
            ):
                return
            identifier = fingerprint(["local-schedule-unsent-retry", identifier])[:32]
        try:
            c.charging.accept(identifier, "start", intent.revision, schedule=window)
        except DeniedError, RecoveryRequiredError:
            return  # The expected authority or time can expire before admission.
        except sqlite3.Error:
            if self.journal.db.in_transaction:
                raise
            return  # A committed admission already owns its worker and cutoff.

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
    def sample_cutoff(self, window: ScheduleWindow) -> tuple[float, float]:
        elapsed = time.monotonic()
        remaining = window.ends_at - time.time()
        return remaining, elapsed + remaining

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
        if now < window.starts_at:
            raise DeniedError("local schedule occurrence no longer owns Start")
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
        if not occurrence or self.timezone is None:
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
            and (self._due(occurrence) or c.current.selected() > occurrence["window"]["limit_a"])
        ):
            return Pending(Prerequisite.SOURCE, "the local schedule does not permit positive current")
        return ALLOW

    # A backward clock step cannot extend an admitted occurrence while the same process is running.
    def _due(self, occurrence: dict[str, Any]) -> bool:
        now = time.time()
        return (
            occurrence.get("stop_revision") is not None
            or self.timezone is None
            or now < occurrence["window"]["starts_at"]
            or self._elapsed(occurrence, now)
        )

    # Either the scheduled wall end or the same occurrence's elapsed deadline ends the window.
    def _elapsed(self, occurrence: dict[str, Any], now: float) -> bool:
        return now >= occurrence["window"]["ends_at"] or (
            self.deadline is not None
            and self.deadline[0] == occurrence["request"]
            and time.monotonic() >= self.deadline[1]
        )

    # Periodic restrictive work can stop its own session even when no complete native poll is available.
    async def stop_if_due(self) -> None:
        c, j = self.controller, self.journal
        occurrence = j.get(OCCURRENCE, {})
        local = occurrence.get("local")
        active = j.active()
        if c.stopping or type(local) is not int or not self._due(occurrence):
            return
        if active != local and not (active is None and occurrence.get("stop_revision") is not None):
            return
        if not j.is_owner_session(local):
            raise RecoveryRequiredError("local schedule lost its original owner session")
        await cutoff(c, local)
