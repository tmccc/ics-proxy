# Modified for the standalone iCS proxy repository.
"""Outage and reconnect decisions under the shared charging controller."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from ..constants import HUB_PROFILE_ID
from .connection_scope import CloudAttemptScope, control_scope_matches, current_control_scope, current_scope
from .control_state import ControlState
from .managed_intent import OFFLINE_PAUSE, ControlIntent, decode_intent, record_automatic_pause, write_intent
from .managed_recovery import local_recovery_binding
from .managed_settlement import require_settled_debt
from .managed_sources import ORDINARY_HOLD as KEY
from .managed_sources import ORDINARY_SOURCE as SOURCE
from .managed_sources import recorded_station_profile
from .native_faults import STATION_CEILING_LOST
from .recovery_contracts import ALLOW, GateDecision, Guard, Pending, Prerequisite
from .recovery_policy import RecoveryFacts, RecoveryObligation, RecoveryWork
from .rpc import IntroductionDeferredError, UnavailableError
from .security import SecurityError
from .store import RecoveryRequiredError, fingerprint
from .trial_allocation import verify_initial_history

if TYPE_CHECKING:
    from collections.abc import Callable

    from .control_decisions import ControlDecisions
    from .gateway import Gateway
    from .managed import ManagedController
    from .managed_charging import ManagedCharging
    from .native_bounded import NativeReadinessError
    from .native_bounded_handover import NativeBoundedHandover
    from .ordinary_recovery import OrdinaryRecovery
    from .recovery_contracts import AuthorityStamp
    from .runtime import ConnectionRuntime
    from .trial_handover_evidence import HandoverEvidence
    from .trial_session import TrialSession


@dataclass(frozen=True)
class ResumeClaim:
    """The restricted introduction and zero claim a native read may acknowledge."""

    scope: CloudAttemptScope
    revision: int
    ticket: object
    local_binding: str | None


@dataclass(frozen=True)
class IntroductionClaim:
    """The original connection and installation binding for the native introduction."""

    epoch: int
    binding: dict[str, Any] | None
    scope: CloudAttemptScope | None
    first_zero: bool


@dataclass(frozen=True)
class IntroductionZeroClaim:
    """One zero proof and the metadata that can permit an identical new Boot to retry it."""

    epoch: int
    boot: str | None
    metadata_revision: int
    control_revision: int
    ticket: object


class RecoveryControl:
    """Decide which recovery hold or release may use the current evidence."""

    # Use the existing owner and journal for all recovery decisions.
    def __init__(self, owner: ControlDecisions) -> None:
        self.journal = owner.journal

    # Registration and a live cloud connection must refer to the current gateway.
    def cloud_ready(self, c: ManagedController) -> bool:
        return c.session.runtime.vendor_registered

    # Only a current revision-bound outage Pause can automatically request release.
    def offline_restore_intent(self, c: ManagedController) -> ControlIntent | None:
        intent = c.requests.intent()
        return intent if c.requests.offline_pause_current(intent) and self.cloud_ready(c) else None

    # An acknowledged connection can enter preflight if its exact epoch still needs release.
    def connection_restore(self, c: ManagedController) -> bool | Pending:
        if not self.cloud_ready(c):
            return Pending(Prerequisite.CLOUD, "connection restoration awaits the current cloud acknowledgement")
        return c.released_epoch != c.session.gateway._charger_epoch

    # Offline replacement holds zero; only a clean app interruption keeps permission to restore the schedule.
    def prepare_offline_pause(self, c: ManagedController) -> None:
        g, j = c.session.gateway, c.session.journal
        intent = c.requests.intent()
        scope = current_scope(g)
        if (
            j.phase.read().value == "recovering"
            and not self.cloud_ready(c)
            and scope is not None
            and c.session.runtime.status.vendor_failed_scope == scope
            and c.native.introduction_lineage == scope.lineage
            and c.native.acknowledged_lineage != scope.lineage
            and intent.mode == "app"
            and self.held_owner_closure(c) is None
            and not self.held_owner_stop_pending(c)
        ):
            with j.atomic(retry_busy=True):
                can_restore = not isinstance(self.offline_return_decision(c), Pending)
                paused = write_intent(j, intent, "paused")
                if can_restore:
                    record_automatic_pause(j, paused, "cloud_connection_failed")
                else:
                    j.put(OFFLINE_PAUSE, True)
                g.controls._claim(True, stopping=True)

    # Automatic Return never assumes authority over an outstanding owner operation or an unsettled session.
    def offline_return_decision(self, c: ManagedController) -> GateDecision:
        j, g = c.session.journal, c.session.gateway
        if (
            j.fault.latched()
            or g.vendor_write_evidence().unfinished
            or c.charging.return_blocked()
            or (c.charging.worker is not None and not c.charging.worker.done())
            or (c.current.worker is not None and not c.current.worker.done())
            or j.current_change.read().unresolved
        ):
            return Pending(Prerequisite.RECONCILIATION, "offline Pause awaits existing control work")
        try:
            require_settled_debt(c)
        except RecoveryRequiredError as error:
            return Pending(Prerequisite.LIFECYCLE, "offline Pause awaits settled charging records", error)
        return ALLOW

    # After verified Pause, local-only acknowledgement retains queued cloud reports.
    def pause_resume(self, c: ManagedController) -> bool | None:
        if self.journal.phase.read().value != "recovering":
            return None
        if not self.cloud_ready(c):
            return True
        return None if self.journal.next_delivery() else False

    # Source and Start obligations remain ordinary Pending data; strict introduction refusals remain failures.
    def positive_decision(self, c: ManagedController, native: HandoverEvidence) -> GateDecision:
        if c.session._introduction_refusal is not None:
            raise RecoveryRequiredError("strict introduction refusal awaits durable diagnostic")
        decision = c.charging.positive_decision(native)
        if isinstance(decision, Pending):
            return decision
        if c.ordinary.active():
            local = c.session.journal.active()
            decision = c.ordinary.authority_decision(local)
            if isinstance(decision, Pending):
                return decision
            if local is not None:
                return c.continuation_decision(native, local)
            elif native.physical.transaction_open:
                return Pending(Prerequisite.LIFECYCLE, "ordinary idle recovery cannot adopt another native session")
        return ALLOW

    # Project simultaneous domain duties without replacing their authoritative durable records with one phase.
    def obligations(self, c: ManagedController) -> RecoveryFacts:
        j, g = self.journal, c.session.gateway
        intent = c.requests.intent()
        change, start = j.current_change.read().value, c.charging.state()
        pending_return = c.requests.pending_return()
        obligations = []
        if change.get("state") == "recovering":
            obligations.append(RecoveryObligation(RecoveryWork.CURRENT, str(change.get("id"))))
        if c.charging.scoped_normal_stop():
            obligations.append(RecoveryObligation(RecoveryWork.EXPIRED_START, str(start.get("id"))))
        if intent.mode == "paused":
            if pending_return is not None:
                obligations.append(RecoveryObligation(RecoveryWork.RETURN, pending_return[0]))
            obligations.append(RecoveryObligation(RecoveryWork.PAUSE, str(intent.revision)))
        if c.ordinary.needed():
            obligations.append(RecoveryObligation(RecoveryWork.ORDINARY, str(intent.revision)))
        if c.released_epoch != g._charger_epoch:
            obligations.append(RecoveryObligation(RecoveryWork.CONNECTION, str(g._charger_epoch)))
        return RecoveryFacts(tuple(obligations), intent.mode == "paused")

    # A verified normal local Start may join ordinary supervision only after its exact request really completed.
    def verified_normal(self, charging: ManagedCharging) -> bool:
        return charging.lease is None and self._verified_normal_start(charging.state()) is not None

    # Cold startup can verify the same durable Start before the managed controller is constructed.
    def _verified_normal_start(self, state: dict[str, Any]) -> str | None:
        j = self.journal
        receipt = j.starts.receipt()
        local = state.get("local_transaction")
        if (
            state.get("kind") != "normal"
            or state.get("source") != "local"
            or state.get("state") != "running"
            or state.get("start_rpc_accepted") is not True
            or type(local) is not int
            or receipt.get("local") != local
            or receipt.get("stage") != "accepted"
            or receipt.get("digest") != state.get("tag_digest")
            or receipt.get("request_id") not in (None, state.get("id"))
            or j.get("control", {}).get("start_pending")
        ):
            return None
        request = j.db.execute(
            "SELECT revision,result FROM managed_requests WHERE id=? AND action='start' AND state='completed'",
            (state.get("id"),),
        ).fetchone()
        transaction = j.db.execute("SELECT start FROM transactions WHERE local=?", (local,)).fetchone()
        if request is None or transaction is None or request["revision"] + 1 != state.get("revision"):
            return None
        result, start = json.loads(request["result"]), json.loads(transaction["start"])
        if (
            result.get("local_session_verified") is not True
            or result.get("timed") is not False
            or fingerprint(start["idTag"].casefold()) != state.get("tag_digest")
        ):
            return None
        return fingerprint(start)

    # An active owner Start may wait at zero after native closure without adopting a different session.
    def _closing_owner_start(
        self, handover: NativeBoundedHandover, native: HandoverEvidence, local: int | None
    ) -> str | None:
        j, physical = self.journal, native.physical
        plugged = physical.cp == "B" and physical.connection == "Plug"
        if local is None or not self.owner_closure_gap(handover, native, local, allow_plugged_close=plugged):
            return None
        state = j.get("managed_test_charge", {})
        if (
            state.get("local_transaction") != local
            or j.get("managed_control_intent") != {"mode": "app", "revision": state.get("revision")}
            or j.starts.lease is not None
            or j.current_change.read().unresolved
        ):
            return None
        return self._verified_normal_start(state)

    # Timer/current/test state machines retain exclusive ownership of their own restoration obligations.
    def ordinary(self, charging: ManagedCharging) -> bool:
        c, j = charging.controller, self.journal
        return (
            not c.stopping
            and (not j.fault.read())
            and (j.phase.read().operating)
            and (c.requests.intent().mode == "app")
            and (not c.current.pending())
            and (charging.lease is None)
            and (charging.state().get("state") in (None, "completed") or self.verified_normal(charging))
            and (j.current_change.read().state not in ("interrupted", "uncertain"))
        )

    # A newer explicit Pause or control revision immediately invalidates the saved positive recovery authority.
    def ordinary_active(self, c: ManagedController) -> bool:
        saved = self.journal.get(KEY, {})
        intent = c.requests.intent()
        return saved.get("state") == "holding" and intent.mode == "app" and saved.get("revision") == intent.revision

    # Publish or revoke source after every successful full native audit; an older proof cannot survive contradiction.
    def observe_ordinary(self, ordinary: OrdinaryRecovery, native: HandoverEvidence) -> None:
        c, j, g = ordinary.c, self.journal, ordinary.c.session.gateway
        if not ordinary.ordinary() or not g.current_boot_seen(g._charger_epoch):
            return
        ordinary.source_published = False
        local = j.active()
        source = {
            "revision": c.requests.intent().revision,
            "local": local,
            "start": ordinary.start_digest(local),
            "source": ordinary.source(native),
            "boot": g.boot_observation,
            "epoch": g._charger_epoch,
            "observed_at": native.physical.observed_at,
            "native_digest": native.digest,
        }
        try:
            j.put(SOURCE, source)
        except sqlite3.Error:
            return  # The previous proof cannot authorize recovery while publication is unavailable.
        ordinary.source_published = True

    # The shared zero-only recovery works after reconnect as well as after a bounded ordinary telemetry outage.
    def ordinary_needed(self, ordinary: OrdinaryRecovery) -> bool:
        j = self.journal
        return ordinary.ordinary() and (ordinary.active() or j.phase.read().value == "recovering")

    # A real session Stop can explain an unmapped native marker while it waits at zero for closure.
    def closed_session_stop_wait(self, native: HandoverEvidence) -> bool:
        j, physical = self.journal, native.physical
        local = physical.native_transaction_id
        return (
            physical.transaction_open
            and type(local) is int
            and j.active() is None
            and native.output_limit_a == 0
            and physical.power_w == 0
            and all(value == 0 for value in native.currents_a + native.powers_w)
            and j.outcomes.latest_closed_session_stop_id() == local
            and j.vendor_id(local) is None
        )

    # A known owner session can explain disagreement between the native connector and the Stop report.
    def owner_closure_gap(
        self,
        handover: NativeBoundedHandover,
        native: HandoverEvidence,
        local: int | None = None,
        *,
        allow_plugged_close: bool = False,
    ) -> bool:
        j, physical = self.journal, native.physical
        active = j.active()
        closed = j.latest_owner_stop_id() if active is None else None
        if local is None:
            local = closed
        if type(local) is not int or local not in (active, closed):
            return False
        unplugged = handover.allow_unplugged and physical.cp == "A" and physical.connection == "Unplug"
        active_unplugged_owner = local == active and unplugged and j.is_owner_session(local)
        if physical.transaction_open:
            if physical.native_transaction_id != local or (local != closed and not active_unplugged_owner):
                return False
        elif (
            physical.native_transaction_id is not None
            or not (
                active_unplugged_owner
                or (local == closed and unplugged)
                or (allow_plugged_close and local == active and j.is_owner_session(local))
            )
            or (local == closed and native.output_limit_a == 0)
        ):
            return False
        selected = j.get("control", {}).get("resume_limit", 6)
        return (
            native.is_fresh(5)
            and type(selected) in (int, float)
            and 6 <= selected <= handover.gateway.policy.max_current_a
            and 0 <= native.output_limit_a <= selected
            and physical.power_w == 0
            and all(value == 0 for value in native.currents_a + native.powers_w)
            and handover.commissioned_binding_matches(native)
        )

    # Either channel can close a proven session first; disagreement grants only a restrictive wait.
    def ordinary_closure_gap(self, ordinary: OrdinaryRecovery, native: HandoverEvidence) -> bool:
        j, physical = self.journal, native.physical
        local, known = physical.native_transaction_id, j.active()
        if (
            not ordinary.ordinary()
            or physical.power_w != 0
            or any(value != 0 for value in native.currents_a + native.powers_w)
        ):
            return False
        if self.owner_closure_gap(ordinary.c.native, native, known):
            return True
        if not physical.transaction_open and local is None and type(known) is int:
            if self._closing_owner_start(ordinary.c.native, native, known) is not None:
                return True
            return (
                j.db.execute(
                    "SELECT 1 FROM transactions WHERE local=? AND stop IS NULL AND vendor IS NOT NULL", (known,)
                ).fetchone()
                is not None
            )
        if not physical.transaction_open or known is not None:
            return False
        # OCPP closed first. Only the latest connector lifecycle can explain an open native report at zero.
        return j.latest_mapped_stop(local) or self.closed_session_stop_wait(native)

    # The same exact ordinary source and owner prerequisites apply before and inside the outgoing gate.
    def ordinary_authority(
        self, ordinary: OrdinaryRecovery, local: int | None, *, introduced: bool = True
    ) -> GateDecision:
        c, j, g = ordinary.c, self.journal, ordinary.c.session.gateway
        hold, source = j.get(KEY, {}), j.get(SOURCE, {})
        grant, local_start = j.get("vendor_start_grant", {}), j.starts.receipt()
        if (
            not ordinary.source_published
            or not ordinary.ordinary()
            or not ordinary.active()
            or hold.get("local") != local
            or j.active() != local
            or hold.get("start") != ordinary.start_digest(local)
            or hold.get("selected_a") != c.current.selected()
            or source.get("revision") != c.requests.intent().revision
            or source.get("local") != local
            or source.get("start") != hold.get("start")
            or not source.get("source")
            or source.get("boot") != g.boot_observation
            or source.get("epoch") != g._charger_epoch
            or not g.current_boot_seen(g._charger_epoch)
            or not self.cloud_ready(c)
            or local is not None
            and j.vendor_id(local) is None
            or introduced
            and c.native.introduced_epoch != g._charger_epoch
            or introduced
            and c.native.introduced_revision != g.controls._revision
            or not 0 <= time.time() - source.get("observed_at", 0) <= 5
            or j.get("trial_preservation_fault")
            or j.get("trial_inventory_fault")
            or g.vendor_write_evidence().unresolved
            or j.get("control", {}).get("start_pending")
            or grant
            and (local is None or grant.get("local") != local)
            or local_start.get("local") is None
            and local_start.get("stage") in ("prepared", "issued", "accepted")
            or j.db.execute("SELECT 1 FROM mutations WHERE state!='confirmed' LIMIT 1").fetchone()
            or j.has_unsettled_sessions(continuing_local=local)
        ):
            return Pending(
                Prerequisite.SOURCE, "ordinary continuation awaits fresh source and reconciled owner authority"
            )
        c.native.spec.require_boot(j.get("boot"))
        return ALLOW

    # Native and journal source observations must still identify the same transaction at this effect boundary.
    def ordinary_session(self, ordinary: OrdinaryRecovery, native: HandoverEvidence, local: int) -> GateDecision:
        decision = ordinary.authority_decision(local)
        if isinstance(decision, Pending):
            return decision
        if (
            not native.physical.transaction_open
            or native.physical.native_transaction_id != local
            or ordinary.source(native) != self.journal.get(SOURCE, {}).get("source")
        ):
            return Pending(Prerequisite.SOURCE, "ordinary continuation source changed during verification")
        return ALLOW

    # Journal the same restrictive hold whether startup or a live observation first finds the closure.
    @staticmethod
    def _new_ordinary_hold(
        reason: str, revision: int, local: int | None, start: str | None, selected_a: int
    ) -> dict[str, Any]:
        return {
            "state": "holding",
            "revision": revision,
            "local": local,
            "start": start,
            "selected_a": selected_a,
            "reason": reason,
            "at_epoch": time.time(),
        }

    # Maintain the existing hold while its exact pending lifecycle advances.
    def _save_ordinary_hold(
        self, c: ManagedController, reason: str, local: int | None, start: str | None, owner_closure: int | None
    ) -> dict[str, Any]:
        j, g = self.journal, c.session.gateway
        previous: dict[str, Any] = j.get(KEY, {})
        current = previous
        if not self.ordinary_active(c):
            current = self._new_ordinary_hold(reason, c.requests.intent().revision, local, start, c.current.selected())
        elif current.get("local") is not None and local is None:
            old = j.db.execute("SELECT stop FROM transactions WHERE local=?", (current["local"],)).fetchone()
            if old is not None and old["stop"] is not None:
                current = {**current, "local": None, "start": None}
        if owner_closure is not None and current.get("owner_closure") != owner_closure:
            current = {**current, "owner_closure": owner_closure}
        with j.atomic(retry_busy=True):
            if current != previous:
                j.put(KEY, current)
            if not ControlState(j.get("control", {})).local_hold:
                g.controls._claim(True, stopping=True)
            j.phase.recover()
        c.released_epoch = None
        return current

    # Claim the exact zero before its independent charger write.
    def claim_ordinary_hold(
        self, ordinary: OrdinaryRecovery, reason: str, *, owner_closure: int | None = None
    ) -> Callable[[], None]:
        c, j, g = ordinary.c, self.journal, ordinary.c.session.gateway
        if not ordinary.ordinary() or not g.current_boot_seen(g._charger_epoch):
            raise RecoveryRequiredError("ordinary recovery awaits admitted current owner authority")
        c.native.spec.require_boot(j.get("boot"))
        if not recorded_station_profile(j, c.native.spec.digest, c.native.spec.boot_digest):
            raise RecoveryRequiredError("ordinary zero hold requires the existing admitted station receipt")
        local = j.active()
        if owner_closure is not None and (local is not None or j.latest_owner_stop_id() != owner_closure):
            raise RecoveryRequiredError("owner closure hold lost its latest closed session")
        epoch, boot = g._charger_epoch, g.boot_observation
        connection = current_control_scope(g)
        current = self._save_ordinary_hold(c, reason, local, ordinary.start_digest(local), owner_closure)

        # A new owner intent, source epoch or overlapping state machine cannot inherit this station write.
        def guard() -> None:
            c.native.spec.require_boot(j.get("boot"))
            if (
                not ordinary.ordinary()
                or not ordinary.active()
                or j.get(KEY) != current
                or g._charger_epoch != epoch
                or g.boot_observation != boot
                or not g.current_boot_seen(epoch)
                or not recorded_station_profile(j, c.native.spec.digest, c.native.spec.boot_digest)
                or g.controls.desired() != 0
                or owner_closure is not None
                and (
                    not control_scope_matches(g, connection)
                    or j.active() is not None
                    or j.latest_owner_stop_id() != owner_closure
                )
            ):
                raise RecoveryRequiredError("ordinary zero hold was superseded")

        return guard

    # Recover the exact owner closure on either side of its genuine Stop report.
    def restore_owner_closure(self, charging: ManagedCharging, saved: dict[str, Any]) -> bool:
        c, j = charging.controller, self.journal
        local, revision = saved.get("local_transaction"), saved.get("revision")
        if (
            type(local) is not int
            or type(revision) is not int
            or c.requests.intent() != ControlIntent("app", revision)
            or not self.verified_normal(charging)
        ):
            return False
        if not self.ordinary(charging):
            return False
        active = j.active()
        if active is not None:
            if active != local or not self._held_active_owner(c):
                return False
        elif j.latest_owner_stop_id() != local:
            return False
        if self._latch_owner_station_fault(c):
            return True
        if active is not None and not self.held_owner_stop_pending(c):
            return False
        if not recorded_station_profile(j, c.native.spec.digest, c.native.spec.boot_digest):
            return False
        if active is None and (not self.ordinary_active(c) or j.get(KEY, {}).get("owner_closure") != local):
            self._save_ordinary_hold(c, "native_closure_pending", None, None, local)
        return True

    # The existing hold must describe the immutable Start that the owner actually recorded.
    def _owner_hold_matches(self, hold: dict[str, Any], local: int | None) -> bool:
        if type(local) is not int or hold.get("local") != local:
            return False
        session = self.journal.db.execute("SELECT start FROM transactions WHERE local=?", (local,)).fetchone()
        return session is not None and hold.get("start") == fingerprint(json.loads(session["start"]))

    # Only the saved closure of the latest owner session can use this zero hold without cloud registration.
    def held_owner_closure(self, c: ManagedController) -> int | None:
        j = self.journal
        hold, intent = j.get(KEY, {}), c.requests.intent()
        if (
            hold.get("state") != "holding"
            or intent != ControlIntent("app", hold.get("revision"))
            or j.active() is not None
            or (local := j.latest_owner_stop_id()) is None
        ):
            return None
        # A prior unplugged observation can own the same session before its genuine Stop is recorded.
        return local if hold.get("owner_closure") == local or self._owner_hold_matches(hold, local) else None

    # An active hold must belong to the current revision and the exact owner Start.
    def _held_active_owner(self, c: ManagedController) -> bool:
        j = self.journal
        local = j.active()
        return self.ordinary_active(c) and j.is_owner_session(local) and self._owner_hold_matches(j.get(KEY, {}), local)

    # An owner closure still needs its genuine Stop even while iCS is unavailable.
    def held_owner_stop_pending(self, c: ManagedController) -> bool:
        return self._held_active_owner(c) and self.journal.get(KEY, {}).get("reason") in (
            "unplugged_session_pending",
            "native_closure_pending",
        )

    # Independently recorded station faults also apply to a hold first taken during a native outage.
    def record_owner_hold_station_fault(self, c: ManagedController) -> None:
        if not self.journal.fault.latched() and (self._held_active_owner(c) or self.held_owner_closure(c) is not None):
            self._latch_owner_station_fault(c)

    # Preserve the independently proved station failure under the exact owner's existing control intent.
    def _latch_owner_station_fault(self, c: ManagedController) -> bool:
        if c.session.book.station_ceiling_lost():
            c.fault(STATION_CEILING_LOST)
        elif self.journal.get("trial_inventory_fault", {}).get("resource") == f"profile:{HUB_PROFILE_ID}":
            c.fault("native_authority_or_preservation_failed")
        else:
            return False
        return True

    # Independent closed zero and settled reports can hand the exact hold over to the usual automatic Pause.
    def complete_owner_closure(
        self,
        ordinary: OrdinaryRecovery,
        local: int,
        native: HandoverEvidence,
        authority: AuthorityStamp | None,
        sequence: int,
    ) -> Pending | None:
        c, j = ordinary.c, self.journal
        if (
            authority is None
            or authority.ticket is None
            or c.authority() != authority
            or c.native.last_resolved_read != sequence
            or not ordinary.ordinary()
            or self.held_owner_closure(c) != local
        ):
            return Pending(Prerequisite.SUPERSEDED, "owner closure hold was superseded")
        if native.physical.transaction_open:
            if self.owner_closure_gap(c.native, native):
                return Pending(Prerequisite.LIFECYCLE, "owner session Stop awaits independent native closure")
            raise SecurityError("managed native transaction has no exact accepted local counterpart")
        if not native.is_fresh(5) or not c.native.commissioned_binding_matches(native):
            return Pending(Prerequisite.NATIVE, "owner closure awaits fresh commissioned native evidence")
        if native.output_limit_a > c.current.selected():
            raise SecurityError("actual native output limit exceeds the selected current")
        if native.output_limit_a != 0 or c.native.stop_zero_refusal(native, local=local):
            return Pending(Prerequisite.ZERO_PROOF, "owner closure awaits the current station zero")
        decision = self.offline_return_decision(c)
        if isinstance(decision, Pending):
            return decision
        with j.atomic(retry_busy=True):
            paused = write_intent(j, authority.intent, "paused")
            record_automatic_pause(j, paused, "local_charge")
            j.control.complete(authority.ticket, uncertain=False)
        return None

    # Record the zero proof and publish its exact Boot revision together.
    def confirm_ordinary_zero(
        self, ordinary: OrdinaryRecovery, native: HandoverEvidence, local: int | None
    ) -> GateDecision:
        c, j, g = ordinary.c, self.journal, ordinary.c.session.gateway
        # A superseded zero proof can leave the audit complete without publishing its introduction.
        if c.native.introduction_pending or c.native.audited_epoch != g._charger_epoch:
            return Pending(Prerequisite.ZERO_PROOF, "ordinary recovery awaits a completed charger audit")
        decision = ordinary.authority_decision(local, introduced=False)
        if isinstance(decision, Pending):
            return decision
        c.native.require(native, closed=local is None)
        decision = c.native.session_decision(native)
        if isinstance(decision, Pending):
            return decision
        control = j.get("control", {})
        epoch, boot, revision, ticket = (
            g._charger_epoch,
            g.boot_observation,
            g.controls._revision,
            control.get("ticket"),
        )
        present = c.session.book.owned_present(native.physical)
        station = [r for r in present if r.kind == "profile" and r.identifier == HUB_PROFILE_ID]
        row = j.db.execute(
            "SELECT state,confirmed FROM trial_resources WHERE key=?", ("profile:" + str(HUB_PROFILE_ID),)
        ).fetchone()
        from .trial_rollback import lifecycle_decision

        decision = lifecycle_decision(g, continuing_local=local)
        if isinstance(decision, Pending):
            return decision
        if (
            any(v != 0 for v in native.currents_a + native.powers_w)
            or not ControlState(control).explicit_local_hold
            or not g.controls.ready
            or g.controls.desired() != 0
            or not c.session.book.confirmed_station_zero(station, row)
            or j.get("trial_binding")
            != {
                "configuration_digest": c.native.binding_digest(native),
                "station": c.native.choice.value(),
                "boot_digest": c.native.spec.boot_digest,
            }
            or j.db.execute("SELECT 1 FROM outbox LIMIT 1").fetchone()
            or g.controls._operations
            or g.charger._pending
            or g.vendor._pending
        ):
            return Pending(Prerequisite.CLOUD, "ordinary introduction awaits confirmed zero and reconciled binding")
        with j.atomic():
            decision = ordinary.authority_decision(local, introduced=False)
            if isinstance(decision, Pending):
                return decision
            if (g._charger_epoch, g.boot_observation, g.controls._revision, j.get("control", {}).get("ticket")) != (
                epoch,
                boot,
                revision,
                ticket,
            ):
                return Pending(Prerequisite.SUPERSEDED, "ordinary zero introduction was superseded")
            j.put(
                "managed_ordinary_zero_introduction",
                {
                    "native_digest": native.digest,
                    "boot": boot,
                    "epoch": epoch,
                    "revision": revision,
                    "at_epoch": time.time(),
                },
            )
            j.phase.introduced()
        c.native.introduced_epoch, c.native.introduced_revision = epoch, revision
        return ALLOW

    # Recheck current closure, introduction and source before launching the owned release.
    def ordinary_preflight(
        self, ordinary: OrdinaryRecovery, native: HandoverEvidence
    ) -> tuple[dict[str, Any], int | None] | Pending:
        c, j = ordinary.c, self.journal
        local = j.active()
        if self.held_owner_stop_pending(c):
            physical = native.physical
            if (
                physical.native_transaction_id not in (None, local)
                or physical.power_w != 0
                or any(native.currents_a + native.powers_w)
                or native.output_limit_a > c.current.selected()
            ):
                raise SecurityError("native evidence contradicts the held owner session at zero")
            return Pending(Prerequisite.LIFECYCLE, "ordinary zero hold awaits exact native closure")
        unplugged_hold = j.get(KEY, {}).get("reason") == "unplugged_session_pending"
        if ordinary.closure_gap(native) or (unplugged_hold and native.physical.transaction_open):
            return Pending(Prerequisite.LIFECYCLE, "ordinary zero hold awaits exact native closure")
        decision = ordinary.zero_introduction_decision(native, local)
        if isinstance(decision, Pending):
            return decision
        decision = ordinary.authority_decision(local)
        if isinstance(decision, Pending):
            return decision
        if local is not None:
            decision = ordinary.session_decision(native, local)
            if isinstance(decision, Pending):
                return decision
        elif native.physical.transaction_open:
            return Pending(Prerequisite.LIFECYCLE, "ordinary idle recovery cannot adopt a new native transaction")
        return j.get(KEY), local

    # Settle the recovery receipt only for the release that completed.
    def settle_ordinary(self, ordinary: OrdinaryRecovery) -> bool:
        completed = ordinary.completion
        if completed is None:
            return False
        c, j, g = ordinary.c, self.journal, ordinary.c.session.gateway
        try:
            with j.atomic():
                if (
                    not c.stopping
                    and j.phase.read().active
                    and not j.fault.latched()
                    and c.publication_is_current(completed.authority)
                    and c.released_epoch == completed.authority.scope.epoch
                    and g.controls.ready
                    and g.controls.desired() > 0
                    and j.get(KEY) == completed.hold
                ):
                    j.put(KEY, {**completed.hold, "state": "complete", "completed_at": completed.completed_at})
        except sqlite3.Error:
            return True
        ordinary.completion = None
        return False

    # Start a zero acknowledgement only under the current admitted and audited introduction.
    def prepare_resume(self, handover: NativeBoundedHandover, local_only: bool) -> ResumeClaim:
        gateway, journal = handover.gateway, self.journal
        local_binding = local_recovery_binding(gateway, handover.spec.digest) if local_only else None
        epoch, revision = gateway._charger_epoch, gateway.controls._revision
        scope = current_scope(gateway)
        ticket = journal.get("control", {}).get("ticket")
        if (
            journal.phase.read().value != "recovering"
            or journal.get("native_bounded_handover", {}).get("stage") != "admitted"
            or gateway.charger.sender is None
            or not gateway.current_boot_seen(epoch)
            or scope is None
            or handover.introduced_epoch != epoch
            or handover.introduced_revision != revision
        ):
            raise RecoveryRequiredError("bounded resume requires the current restricted recovery introduction")
        return ResumeClaim(scope, revision, ticket, local_binding)

    # Keep the zero hold and publish the acknowledged lineage only after COMMIT.
    def acknowledge_resume(
        self,
        handover: NativeBoundedHandover,
        claim: ResumeClaim,
        native: HandoverEvidence,
        sequence: int,
        local_only: bool,
        continuing_local: int | None,
        preserve_expired_start: Callable[[HandoverEvidence, int | None], bool] | None,
    ) -> dict[str, Any] | Pending:
        from .trial_rollback import lifecycle_decision

        gateway, journal = handover.gateway, self.journal
        scope, revision, ticket = claim.scope, claim.revision, claim.ticket
        epoch = scope.epoch
        if not control_scope_matches(gateway, (scope, revision)) or handover.last_resolved_read != sequence:
            return Pending(Prerequisite.SUPERSEDED, "resume observation was superseded")
        if continuing_local is not None:
            if handover.continuation_guard is None:
                decision: GateDecision = Pending(Prerequisite.SOURCE, "continuation requires a managed source guard")
            else:
                decision = handover.continuation_guard(native, continuing_local)
            if isinstance(decision, Pending):
                return decision
            if local_only or any(v != 0 for v in native.currents_a + native.powers_w):
                raise RecoveryRequiredError("original session continuation requires online verified zero")
        gateway._require_epoch("charger", epoch)
        handover.spec.require_boot(journal.get("boot"))
        if journal.get("trial_binding") != {
            "configuration_digest": handover.binding_digest(native),
            "station": handover.choice.value(),
            "boot_digest": handover.spec.boot_digest,
        }:
            raise SecurityError("bounded recovery binding changed before acknowledgement")
        present = handover.book.owned_present(native.physical)
        receipt = journal.db.execute(
            "SELECT state,confirmed FROM trial_resources WHERE key=?", ("profile:" + str(HUB_PROFILE_ID),)
        ).fetchone()
        station = [r for r in present if r.identifier == HUB_PROFILE_ID]
        control = journal.get("control", {})
        # Evaluate managed debt against this read, after any waits; an earlier source cannot permit recovery.
        decision = lifecycle_decision(
            gateway,
            continuing_local=continuing_local,
            preserve_expired_start=not local_only
            and preserve_expired_start is not None
            and preserve_expired_start(native, continuing_local),
        )
        if isinstance(decision, Pending):
            return decision
        if (
            journal.phase.read().value != "recovering"
            or journal.get("native_bounded_handover", {}).get("stage") != "admitted"
            or not control_scope_matches(gateway, (scope, revision))
            or ticket != control.get("ticket")
            or not gateway.current_boot_seen(epoch)
            or not control.get("inhibit")
            or gateway.controls.desired() != 0
            or not gateway.controls.ready
            or not handover.book.confirmed_station_zero(station, receipt)
            or not local_only
            and journal.db.execute("SELECT 1 FROM outbox LIMIT 1").fetchone()
            or gateway.controls._operations
            or gateway.charger._pending
            or gateway.vendor._pending
            or journal.get("uncertain_vendor_command")
        ):
            return Pending(Prerequisite.ZERO_PROOF, "bounded recovery awaits confirmed zero and history reconciliation")
        if local_only and local_recovery_binding(gateway, handover.spec.digest) != claim.local_binding:
            raise RecoveryRequiredError("local recovery intent changed during native verification")
        journal.require_allocation()
        with journal.atomic():
            journal.phase.resume(control)
        handover.acknowledged_lineage = scope.lineage
        return {"recovery_acknowledged": True, "zero_hold_retained": True, "charging_resumed": False}

    # Restrict new transports or changed introductions before they can authorize any charging-capable request.
    def restrict_introduction(self) -> None:
        if self.journal.phase.read().active and self.journal.get("trial_binding") is not None:
            with self.journal.atomic():
                control = self.journal.get("control", {})
                self.journal.phase.restrict(control)

    # Establish startup's actual session and resources before publishing the restricted introduction.
    def accept_startup(self, session: TrialSession, native: HandoverEvidence, *, route: str = "trial") -> None:
        if self.journal.get("trial_binding") is None:
            session.native_bounded.require_known_session(native)
        else:
            session.native_bounded.require_recovery_session(native)
        session.book.owned_present(native.physical)
        self.journal.control.hold(self.journal.get("control", {}))
        if self.journal.get("trial_binding") is None:
            session.native_bounded.spec.require_admission(native, self.journal.identity, route=route)
            if self.journal.db.execute("SELECT 1 FROM trial_resources LIMIT 1").fetchone():
                raise RecoveryRequiredError("unintroduced connected recovery cannot adopt existing resources")
            self.journal.require_allocation()
            self.journal.phase.activate()
            self.journal.phase.begin_introduction()
        else:
            with self.journal.atomic():
                self.recognize_owner_closure(
                    session.native_bounded,
                    native,
                    inventory_audited=True,
                    startup_current=session._commissioned_selection(),
                )
                self.journal.phase.recover()

    # A fresh audit can recognize closure during startup or after a native outage has already claimed this session.
    def recognize_owner_closure(
        self,
        handover: NativeBoundedHandover,
        native: HandoverEvidence,
        *,
        inventory_audited: bool,
        startup_current: int | None = None,
    ) -> None:
        j = self.journal
        hold, local = j.get(KEY, {}), j.active()
        intent, revision = j.get("managed_control_intent"), hold.get("revision")
        plugged = native.physical.cp == "B" and native.physical.connection == "Plug"
        reason = "native_closure_pending" if plugged else "unplugged_session_pending"
        holding = (
            hold.get("state") == "holding"
            and type(revision) is int
            and intent is not None
            and decode_intent(intent) == ControlIntent("app", revision)
        )
        if (
            holding
            and hold.get("reason") == "native_observation_unavailable"
            and self._owner_hold_matches(hold, local)
            and self.owner_closure_gap(handover, native, local, allow_plugged_close=plugged)
        ):
            if not inventory_audited:
                handover.book.owned_present(native.physical)
            if recorded_station_profile(j, handover.spec.digest, handover.spec.boot_digest):
                j.put(KEY, {**hold, "reason": reason})
        elif not holding and startup_current is not None:
            start = self._closing_owner_start(handover, native, local)
            if start is not None and recorded_station_profile(j, handover.spec.digest, handover.spec.boot_digest):
                revision = decode_intent(intent).revision
                j.put(KEY, self._new_ordinary_hold(reason, revision, local, start, startup_current))

    # Bind the new observation to the admitted installation before any native wait.
    def begin_introduction(self, handover: NativeBoundedHandover) -> IntroductionClaim:
        gateway, journal = handover.gateway, self.journal
        epoch = gateway._charger_epoch
        if not journal.phase.read().supervised:
            raise RecoveryRequiredError("native bounded introduction cannot rearm frozen cleanup")
        if not gateway.current_boot_seen(epoch):
            raise IntroductionDeferredError("native bounded introduction awaits a saved Boot")
        if fingerprint(journal.get("boot")) != handover.spec.boot_digest:
            raise SecurityError("native bounded introduction requires the commissioned actual Boot")
        binding = journal.get("trial_binding")
        scope = current_scope(gateway)
        if binding is not None and scope is not None:
            handover.introduction_lineage = scope.lineage
        first_zero = binding is None or bool(journal.phase.starting)
        return IntroductionClaim(epoch, binding, scope, first_zero)

    # Preserve the original binding and enter the restrictive hold before verifying zero.
    def prepare_introduction(
        self, handover: NativeBoundedHandover, claim: IntroductionClaim, native: HandoverEvidence, sequence: int
    ) -> bool:
        from .native_bounded_handover import NativeSessionPendingError

        gateway, journal = handover.gateway, self.journal
        binding, scope, epoch = claim.binding, claim.scope, claim.epoch
        gateway._require_epoch("charger", epoch)
        if current_scope(gateway) != scope or handover.last_resolved_read != sequence:
            raise UnavailableError("introduction evidence belongs to a superseded Boot or observation")
        if binding is None:
            if (
                not journal.phase.read().active
                or journal.db.execute("SELECT 1 FROM trial_resources LIMIT 1").fetchone()
            ):
                raise RecoveryRequiredError("initial native bounded introduction cannot adopt existing resources")
            if native.physical.history is None:
                raise SecurityError("actual Boot introduction requires complete current history")
            verify_initial_history(journal, native.physical.history, journal.identity, handover.spec.hardware_identity)
        elif binding != {
            "configuration_digest": handover.binding_digest(native),
            "station": handover.choice.value(),
            "boot_digest": handover.spec.boot_digest,
        }:
            raise SecurityError("native bounded introduction binding changed")
        try:
            if binding is None:
                handover.require_known_session(native)
            else:
                handover.require_recovery_session(native)
        except NativeSessionPendingError as error:
            raise IntroductionDeferredError("introduction awaits native lifecycle reconciliation") from error
        handover.book.owned_present(native.physical)
        if binding is None:
            journal.put(
                "trial_binding",
                {
                    "configuration_digest": handover.binding_digest(native),
                    "station": handover.choice.value(),
                    "boot_digest": handover.spec.boot_digest,
                },
            )
        if binding is not None or journal.get("control", {}).get("uncertain"):
            control = journal.get("control", {})
            journal.control.hold(control)
            return True
        return False

    # Confirm the same introduction after its optional restrictive zero write.
    def confirm_introduction(self, handover: NativeBoundedHandover, claim: IntroductionClaim, recovering: bool) -> int:
        gateway, journal = handover.gateway, self.journal
        epoch, scope = claim.epoch, claim.scope
        gateway._require_epoch("charger", epoch)
        if current_scope(gateway) != scope:
            raise UnavailableError("the verified charger introduction was superseded")
        if recovering:
            journal.phase.recover()
        revision = gateway.controls._revision
        journal.phase.introduced()
        handover.audited_epoch = epoch
        return revision

    # Select the restrictive work for an already open session after reconnect.
    def reconnect_protection(
        self, handover: NativeBoundedHandover, claim: IntroductionClaim, native: HandoverEvidence
    ) -> Literal["zero", "missing_station", "stop"] | None:
        j = self.journal
        if claim.binding is None or not native.physical.transaction_open or j.current_change.read().recovering:
            return None
        if (
            self.closed_session_stop_wait(native)
            or self.owner_closure_gap(handover, native)
            or j.get("managed_mode")
            and j.get("managed_control_intent", {}).get("mode") == "app"
            or handover.recovery_zero_only is not None
            and handover.recovery_zero_only()
        ):
            return "zero"
        if handover.stop_missing_station is not None and handover.book.station_ceiling_lost():
            return "missing_station"
        return "stop"

    # Publish the audited revision before the runtime confirms zero against the latest actual Boot.
    def publish_introduction(self, handover: NativeBoundedHandover, epoch: int, verified_revision: int) -> None:
        gateway = handover.gateway
        gateway._require_epoch("charger", epoch)
        if verified_revision != gateway.controls._revision:
            raise UnavailableError("the verified charger introduction was superseded")
        handover.introduced_epoch, handover.introduced_revision = epoch, verified_revision

    # A current recovery with matching source and lifecycle can reprove a superseded zero introduction.
    def confirm_current_introduction(self, c: ManagedController, native: HandoverEvidence) -> GateDecision:
        g, j = c.session.gateway, self.journal
        scope = current_scope(g)
        if (
            scope is None
            or c.native.introduction_pending
            or c.native.audited_epoch != scope.epoch
            or not j.phase.read().operating
            or j.fault.latched()
            or not c.native.commissioned_binding_matches(native)
            or not j.control.read().explicit_local_hold
            or native.output_limit_a != 0
            or any(value != 0 for value in native.currents_a + native.powers_w)
            or g.controls._operations
            or g.charger._pending
        ):
            return Pending(Prerequisite.ZERO_PROOF, "current recovery awaits its audited zero introduction")
        if not self.cloud_ready(c):
            return Pending(Prerequisite.CLOUD, "current recovery awaits the current registered cloud connection")
        present = c.session.book.owned_present(native.physical)
        if (refusal := c.native.station_zero_refusal(present)) is not None:
            return Pending(Prerequisite.ZERO_PROOF, refusal)
        self.publish_introduction(c.native, scope.epoch, g.controls._revision)
        return ALLOW

    # Record the Boot, metadata and zero ticket from the runtime's current connection.
    def introduction_zero_claim(self, gateway: Gateway, epoch: int) -> IntroductionZeroClaim:
        return IntroductionZeroClaim(
            epoch,
            gateway.boot_observation,
            gateway.boot_metadata_revision,
            gateway.controls._revision,
            self.journal.get("control", {}).get("ticket"),
        )

    # Only identical metadata under the same admitted owner can start another complete zero proof.
    def same_zero_authority(self, runtime: ConnectionRuntime, claim: IntroductionZeroClaim) -> bool:
        gateway, journal = runtime.gateway, self.journal
        return (
            not runtime._closing
            and runtime.ceiling_enabled()
            and gateway.current_boot_seen(claim.epoch)
            and claim.boot is not None
            and gateway.boot_metadata_revision == claim.metadata_revision
            and gateway.controls._revision == claim.control_revision
            and journal.get("control", {}).get("ticket") == claim.ticket
            and journal.get("native_bounded_handover", {}).get("stage") == "admitted"
            and journal.phase.read().operating
            and not journal.phase.starting
            and journal.get("control", {}).get("inhibit") is True
            and gateway.controls.desired() == 0
        )

    # The first release sample must prove the appropriate session and zero before a phase resume.
    def release_observation(
        self, c: ManagedController, guard: Guard, native: HandoverEvidence | Pending, continuing_local: int | None
    ) -> GateDecision:
        decision = guard()
        if isinstance(decision, Pending):
            return decision
        if isinstance(native, Pending):
            return native
        decision = c.native.session_decision(native)
        if isinstance(decision, Pending):
            return decision
        if native.physical.transaction_open and continuing_local is None:
            return Pending(Prerequisite.LIFECYCLE, "owner release awaits actual transaction closure")
        return c.zero_decision(native, continuing_local=continuing_local)

    # A bounded run of availability failures can claim ordinary zero under the current app intent.
    def transport_needs_hold(self, c: ManagedController) -> bool:
        return c.native_unavailable >= 3 and c.ordinary.ordinary()

    # Finish a paused introduction only from a fresh complete closed-zero proof, never a failed RPC or marker alone.
    def confirm_paused_introduction(self, handover: NativeBoundedHandover, native: HandoverEvidence) -> None:
        g, j = handover.gateway, self.journal
        epoch, revision = g._charger_epoch, g.controls._revision
        zero_refusal = handover.stop_zero_refusal(native)
        binding = {
            "configuration_digest": handover.binding_digest(native),
            "station": handover.choice.value(),
            "boot_digest": handover.spec.boot_digest,
        }
        if (
            handover.audited_epoch != epoch
            or handover.introduction_pending
            or j.phase.starting
            or not j.phase.read().operating
            or j.get("native_bounded_handover", {}).get("stage") != "admitted"
            or j.get("trial_binding") != binding
            or j.get("managed_control_intent", {}).get("mode") != "paused"
            or j.fault.latched()
            or fingerprint(j.get("boot")) != handover.spec.boot_digest
            or zero_refusal is not None
            or not ControlState(j.get("control", {})).local_hold
            or j.get("control", {}).get("start_pending")
            or j.get("uncertain_vendor_command")
            or g.charger._pending
            or g.controls._operations
        ):
            raise RecoveryRequiredError("paused introduction awaits current independently confirmed closed zero")
        # An earlier HTTP audit must finish its own initial claim; only its later failed restrictive RPC is resumable.
        handover.introduced_epoch, handover.introduced_revision = epoch, revision

    # A genuine Stop may have occurred while the process was absent; retain the ledger debt until its real replay.
    def require_recovery_session(self, handover: NativeBoundedHandover, native: HandoverEvidence) -> None:
        if native.physical.transaction_open:
            j = self.journal
            control = j.get("control", {})
            active = j.active()
            if (
                active is None
                and j.get("managed_mode")
                and j.latest_mapped_stop(native.physical.native_transaction_id)
                and native.physical.power_w == 0
                and all(value == 0 for value in native.currents_a + native.powers_w)
            ):
                return  # Keep reception and restrictive recovery available while native closure catches up.
            if (
                handover.commissioned_binding_matches(native)
                and self.closed_session_stop_wait(native)
                or self.owner_closure_gap(handover, native)
            ):
                return
            if (
                j.current_change.read().recovering
                and recorded_station_profile(j, handover.spec.digest, handover.spec.boot_digest)
                and control.get("inhibit")
            ):
                return  # Introduce only the restricted listener; no native ID is adopted or given Stop authority.
            if (
                active is None
                and j.get("managed_control_intent", {}).get("mode") == "paused"
                and control.get("start_pending")
                and control.get("inhibit")
                and control.get("owner") == "local"
                and j.starts.receipt().get("stage") in ("issued", "accepted")
                and recorded_station_profile(j, handover.spec.digest, handover.spec.boot_digest)
                and all(v == 0 for v in native.currents_a + native.powers_w)
                and any(
                    r.identifier == HUB_PROFILE_ID and r.canonical.get("limit") == 0
                    for r in handover.book.owned_present(native.physical)
                )
            ):
                return  # The verified zero listener can receive the real lifecycle without adopting this native ID.
            handover.require_known_session(native)
        elif native.physical.native_transaction_id is not None:
            raise RecoveryRequiredError("restricted recovery cannot adopt contradictory native session evidence")

    # A refused initial zero can retry only if a newer owner Pause has already taken the hold.
    def initial_zero_superseded(self, gateway: Gateway, intent: dict[str, Any], ticket: object) -> bool:
        latest = self.journal.get("managed_control_intent", {})
        control = self.journal.get("control", {})
        return not (
            latest.get("mode") != "paused"
            or latest.get("revision", 0) <= intent.get("revision", 0)
            or control.get("ticket") == ticket
            or not ControlState(control).explicit_local_hold
            or gateway.controls.desired() != 0
        )

    # A known unplugged session at zero may wait for its exact device and ledger closure.
    def unplugged_closure(self, c: ManagedController, error: NativeReadinessError) -> bool:
        active = self.journal.active()
        return self.owner_closure_gap(c.native, error.native, active) or (
            error.unplugged_at_zero
            and (error.native.physical.native_transaction_id == active or c.ordinary.closure_gap(error.native))
        )
