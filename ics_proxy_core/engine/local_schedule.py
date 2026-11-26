"""Admit one commissioned overnight occurrence through the existing Start and Stop owner."""

from __future__ import annotations

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
    from .start_handoff import OwnedStartResult
    from .trial_handover_evidence import HandoverEvidence


class ScheduleCloudReturnedError(DeniedError):
    """The current iCS connection superseded an unissued scheduled Start."""


class LocalSchedule:
    """An outage can delegate an unchanged native plan to the normal local owner once per window."""

    # The occurrence lives in the same locked journal as its management request and charger reports.
    def __init__(self, controller: ManagedController, timezone: str | None) -> None:
        self.controller = controller
        self.timezone = timezone
        self.journal = controller.session.journal
        self.deadline: tuple[str, float] | None = None

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
        if (
            occurrence.get("request") != lease.identifier
            or occurrence.get("window") != asdict(window)
            or lease.owner_start_tag is None
            or self._due(occurrence)
        ):
            raise DeniedError("local schedule occurrence no longer owns Start")
        if c.session.runtime.vendor_registered and not original_dispatched and not finishing_initial_zero:
            raise ScheduleCloudReturnedError("iCS returned before the local schedule issued Start")

    # Verify the exact initial hold or rollback before the shared owner may request automatic Return.
    def verified_unsent_zero(self, lease: ChargeLease, result: OwnedStartResult | None) -> bool:
        c, j = self.controller, self.journal
        gateway = c.session.gateway
        controls = gateway.controls
        occurrence, control = j.get(OCCURRENCE, {}), j.get("control", {})
        receipt, state = j.starts.receipt(), c.charging.state()
        if (
            lease.schedule is None
            or not c.session.runtime.vendor_registered
            or occurrence.get("request") != lease.identifier
            or occurrence.get("window") != asdict(lease.schedule)
            or occurrence.get("local") is not None
            or state.get("id") != lease.identifier
            or state.get("start_rpc_accepted") is not False
            or state.get("local_transaction") is not None
            or j.active() is not None
            or control.get("start_pending")
            or not controls.ready
            or controls.desired() != 0
            or any(
                wire.action == "RemoteStartTransaction" for wire in j.outcomes.owner_wires("request", lease.identifier)
            )
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
            and state.get("start_attempted") is True
            and lease.start_scope is not None
            and control_scope_matches(gateway, (lease.start_scope, lease.control_revision))
            and receipt.get("request_id") == lease.identifier
            and receipt.get("digest") == state.get("tag_digest")
            and receipt.get("owner_authorized") is True
            and receipt.get("stage") == "unsent"
            and receipt.get("local") is None
            and isinstance(receipt.get("ticket"), str)
            and receipt["ticket"] == receipt.get("settled_ticket") == control.get("ticket")
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
        now, window = time.time(), occurrence["window"]
        return (
            occurrence.get("stop_revision") is not None
            or self.timezone is None
            or not window["starts_at"] <= now < window["ends_at"]
            or (
                self.deadline is not None
                and self.deadline[0] == occurrence["request"]
                and time.monotonic() >= self.deadline[1]
            )
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
