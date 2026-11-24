"""End one verified charger session without changing the requested control mode."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any

from ..commands import remote_stop_transaction
from .command_processor import CommandPriority
from .connection_scope import ControlScope, control_scope_matches, current_control_scope
from .control_decisions import StopDecision
from .managed_intent import ControlIntent, insert_request, request_pending
from .native_http import NativeTransportUnavailableError
from .policy import DeniedError
from .rpc import UnavailableError
from .security import SecurityError, StaleEvidenceError
from .stop_settlement import (
    SAVED_STOP_REJECTIONS,
    STOP_BUFFERED_CLOSURE,
    STOP_CLOSURE_FIELDS,
    STOP_CLOSURE_VERIFIED,
    STOP_COMPLETION_FIELDS,
    STOP_SESSION,
    StopRejections,
    stop_rejections,
    stop_reply_window_predicate,
)
from .store import JournalStartBusy, RecoveryRequiredError, encoded, fingerprint

if TYPE_CHECKING:
    from .managed import ManagedController
    from .trial_handover_evidence import HandoverEvidence

CONFIRMATION_SECONDS = 20


@dataclass(frozen=True)
class SessionStop:
    """The identity chosen at admission survives later UI and charger updates."""

    identifier: str
    target: str
    local: int
    intent: ControlIntent
    connection: ControlScope
    confirmation_deadline: float


class ManagedSessionStop:
    """Use the shared Stop decision and settle only against the exact session's closure."""

    # Replacement can retire an unsent request. A dispatch marker still owns its original possible effect.
    def __init__(self, controller: ManagedController) -> None:
        self.controller = controller
        self.worker: asyncio.Task[None] | None = None
        self._verified_closures: dict[str, int] = {}
        self.expire(replaced=True)

    # Expose an opaque target only for a verified sample of the exact current open session.
    def target(self) -> str | None:
        c, j, g = self.controller, self.controller.session.journal, self.controller.session.gateway
        native, local, connection = c.current_native_sample(), j.active(), current_control_scope(g)
        if (
            c.stopping
            or not j.phase.read().active
            or local is None
            or connection is None
            or native is None
            or not native.physical.transaction_open
            or native.physical.native_transaction_id != local
            or not native.is_fresh(15)
            or not c.native.session_stop_verified(local)
        ):
            return None
        scope, generation = connection
        return fingerprint((scope.boot, scope.epoch, scope.metadata_revision, generation, local))

    # Persist the exact target and revision before a worker can act, with no change to Pause or Return intent.
    def accept(self, identifier: str, revision: int, target: str) -> dict[str, Any]:
        return self.controller.commands.decide(
            lambda: self._accept(identifier, revision, target), priority=CommandPriority.RESTRICT
        )

    # Admission and predecessor selection share the turn used by charger replies and lifecycle reports.
    def _accept(self, identifier: str, revision: int, target: str) -> dict[str, Any]:
        c, j, g = self.controller, self.controller.session.journal, self.controller.session.gateway
        existing = c.requests.existing(identifier, STOP_SESSION, revision)
        if existing is not None:
            if c.requests.receipt(identifier, include_binding=True).get("target") != target:
                raise SecurityError("request identifier already binds another Stop target")
            return existing
        intent, local, connection = c.requests.intent(), j.active(), current_control_scope(g)
        if revision != intent.revision or local is None or connection is None or self.target() != target:
            raise RecoveryRequiredError("Stop requires the current verified session")
        if not c.requests.has_capacity():
            raise RecoveryRequiredError("management receipt capacity requires operator maintenance")
        accepted = time.time()
        confirmation_deadline = time.monotonic() + CONFIRMATION_SECONDS
        with j.atomic():
            decision, binding = g.controls.decisions.session_stop_decision(local, connection[0].boot)
            if decision is StopDecision.CLOSED:
                raise RecoveryRequiredError("Stop requires the current verified session")
            needs_dispatch = decision is StopDecision.SEND
            if needs_dispatch and self.worker is not None and not self.worker.done():
                raise RecoveryRequiredError("the current session Stop is still being sent")
            insert_request(
                j,
                identifier,
                STOP_SESSION,
                revision,
                {
                    "accepted_at": accepted,
                    "target": target,
                    "_session_stop": {
                        "local": local,
                        "dispatched": False,
                        "confirmation_deadline": confirmation_deadline,
                        **binding,
                    },
                },
                state="accepted" if needs_dispatch else "running",
            )
        if needs_dispatch:
            scope = SessionStop(identifier, target, local, intent, connection, confirmation_deadline)
            self.worker = c.commands.start(partial(self._send, scope), priority=CommandPriority.RESTRICT)
        return {"id": identifier, "action": STOP_SESSION, "state": "accepted", "result": {"accepted_at": accepted}}

    # Only the original owner, Boot and session can dispatch this Stop, even after another command enters the gate.
    async def _send(self, scope: SessionStop) -> None:
        c, j, g = self.controller, self.controller.session.journal, self.controller.session.gateway
        boot = scope.connection[0].boot

        # Reject a new target or an expired/replaced request both before and at the actual write boundary.
        def guard() -> None:
            if (
                c.stopping
                or c.requests.intent() != scope.intent
                or not control_scope_matches(g, scope.connection)
                or not request_pending(j, scope.identifier, STOP_SESSION, scope.intent.revision)
            ):
                raise RecoveryRequiredError("Stop lost its current request")
            if self.target() != scope.target:
                session = j.outcomes.session(scope.local)
                if session is not None and session.closed:
                    raise StopAlreadyHandledError  # The original report still needs independent closure evidence.
                raise RecoveryRequiredError("Stop lost its current session")
            if time.monotonic() >= scope.confirmation_deadline:
                raise RecoveryRequiredError("Stop expired before dispatch")

        # Commit possible dispatch before the sender can write, including when the wire reply is lost.
        def dispatch(_action: str) -> None:
            decision, binding = g.controls.decisions.session_stop_decision(scope.local, boot)
            if decision is StopDecision.CLOSED:
                raise StopAlreadyHandledError
            with j.atomic():
                if decision is StopDecision.SEND:
                    binding["dispatched"] = 1
                j.db.execute(
                    "UPDATE managed_requests SET state='running',result=json_patch(result,?) WHERE id=?",
                    (encoded({"_session_stop": binding}), scope.identifier),
                )
            if decision is not StopDecision.SEND:
                raise StopAlreadyHandledError

        wire_scope = j.outcomes.owner_scope(scope.identifier, scope.identifier, boot)
        try:
            async with g.charger.sequence(
                0, guard, on_dispatch=dispatch, owner_scope=wire_scope, late_reply_action="RemoteStopTransaction"
            ) as call:
                await call("RemoteStopTransaction", remote_stop_transaction(scope.local))
            c.commands.decide(lambda: self._publish_rejections(stop_rejections(j)))
        except StopAlreadyHandledError:
            pass  # The original session's real closure can also satisfy this waiting request.
        except DeniedError, RecoveryRequiredError, SecurityError, UnavailableError, TimeoutError:
            try:
                c.commands.decide(lambda: self._fail_unsent(scope))
            except sqlite3.Error:
                if j.db.in_transaction:
                    raise
        except sqlite3.Error:
            pass  # Preserve the receipt for expiry if the original store operation failed.

    # A pre-dispatch refusal is final only while the original receipt proves no send and no join.
    def _fail_unsent(self, scope: SessionStop) -> None:
        self._update_receipts(
            "state='failed',result=json_set(result,'$.classification','stop_not_sent')",
            (),
            "id=? AND action=? AND revision=? AND state='accepted' "
            "AND json_type(result,'$._session_stop.dispatched')='false' "
            "AND json_type(result,'$._session_stop.joined') IS NULL",
            (scope.identifier, STOP_SESSION, scope.intent.revision),
            retry_after_rollback=True,
        )

    # Use the same rejection evidence for replies that arrive immediately, late, or before a restart.
    def _publish_rejections(self, rejections: StopRejections) -> None:
        self._update_receipts(
            "state='failed',result=json_set(result,'$.classification','stop_rejected')",
            (),
            f"action=? AND state IN ('running','uncertain') AND {rejections.receipts}",
            (STOP_SESSION,),
            rejections=rejections,
            retry_after_rollback=True,
        )

    # Retire the client wait without resending or erasing a Stop that might still take effect.
    def expire(self, *, replaced: bool = False) -> None:
        rejections = stop_rejections(self.controller.session.journal)
        self._publish_rejections(rejections)
        self._settle_closure(rejections)
        verified, proofs = self._closure_proof()
        # The same rejection evidence also excludes a receipt from closure while its failure write awaits retry.
        self._update_receipts(
            "state=CASE WHEN json_extract(result,'$._session_stop.dispatched') "
            "OR json_extract(result,'$._session_stop.joined') "
            f"OR {verified} "
            "THEN 'uncertain' ELSE 'failed' END,result=json_set(result,'$.classification',?)",
            (*proofs, "process_replaced" if replaced else "stop_not_confirmed"),
            f"action=? AND state IN ('accepted','running') AND NOT {rejections.receipts} "
            "AND (? OR COALESCE(json_extract(result,'$._session_stop.confirmation_deadline')<=?, "
            "json_extract(result,'$.accepted_at')<=?))",
            (STOP_SESSION, replaced, time.monotonic(), time.time() - CONFIRMATION_SECONDS),
            rejections=rejections,
            retry_after_rollback=not replaced,
        )

    # Skip receipt writes and optional native probes when none of the exact eligible rows exists.
    def _has_receipts(self, where: str, binding: tuple[Any, ...], rejections: StopRejections) -> bool:
        row = self.controller.session.journal.db.execute(
            f"{rejections.prefix}SELECT 1 FROM managed_requests WHERE {where} LIMIT 1", rejections.bindings + binding
        ).fetchone()
        return row is not None

    # Only a real receipt transition needs a write lock. A busy begin can wait for the next poll.
    def _update_receipts(
        self,
        assignments: str,
        values: tuple[Any, ...],
        where: str,
        binding: tuple[Any, ...],
        *,
        rejections: StopRejections = SAVED_STOP_REJECTIONS,
        retry_after_rollback: bool = False,
    ) -> bool:
        j = self.controller.session.journal
        if not self._has_receipts(where, binding, rejections):
            return True
        transaction_started = False
        try:
            with j.atomic(retry_busy=True):
                transaction_started = True
                j.db.execute(
                    f"{rejections.prefix}UPDATE managed_requests SET {assignments} WHERE {where}",
                    rejections.bindings + values + binding,
                )
        except JournalStartBusy:
            return False  # The existing receipt is still accurate because the write never began.
        except sqlite3.Error:
            if not retry_after_rollback or not transaction_started or j.db.in_transaction:
                raise
            # The original evidence remains intact. The next poll can retry publication.
            return False
        return True

    # A fresh native observation and the genuine Stop report must agree before a receipt completes.
    def observed(self, native: HandoverEvidence) -> None:
        j = self.controller.session.journal
        active = j.active()
        if (
            native.physical.transaction_open != (active is not None)
            or native.physical.native_transaction_id != active
            or not native.is_fresh(5)
            or active is None
            and (native.physical.power_w != 0 or any(value != 0 for value in native.currents_a + native.powers_w))
        ):
            return
        self._settle_closure(stop_rejections(j), observed=True)

    # A refused database write can leave the same exact closure available in memory.
    def _closure_proof(self) -> tuple[str, tuple[str, ...]]:
        if self._verified_closures:
            return f"({STOP_CLOSURE_VERIFIED} OR {STOP_BUFFERED_CLOSURE})", (encoded(self._verified_closures),)
        return STOP_CLOSURE_VERIFIED, ()

    # Save verified closure separately so a refused completion cannot roll back the observation.
    def _checkpoint_closure(self, rejections: StopRejections, *, observed: bool) -> None:
        if not observed and not self._verified_closures:
            return
        where = f"{rejections.closed_receipts} AND NOT {STOP_CLOSURE_VERIFIED}"
        bindings: tuple[str, ...] = (STOP_SESSION,)
        if not observed:
            where += f" AND {STOP_BUFFERED_CLOSURE}"
            bindings += (encoded(self._verified_closures),)
        j = self.controller.session.journal
        # A previously buffered receipt may already have completed or been rejected.
        self._verified_closures = {
            row[0]: row[1]
            for row in j.db.execute(
                f"{rejections.prefix}SELECT id,json_extract(result,'$._session_stop.local') "
                f"FROM managed_requests WHERE {where}",
                (*rejections.bindings, *bindings),
            )
        }
        if self._verified_closures and self._update_receipts(
            STOP_CLOSURE_FIELDS,
            (),
            f"{rejections.closed_receipts} AND {STOP_BUFFERED_CLOSURE} AND NOT {STOP_CLOSURE_VERIFIED}",
            (STOP_SESSION, encoded(self._verified_closures)),
            rejections=rejections,
            retry_after_rollback=True,
        ):
            self._verified_closures.clear()

    # Keep verified closure when an exact normal or late receiver can still deliver a refusal.
    def _settle_closure(self, rejections: StopRejections, *, observed: bool = False) -> None:
        self._checkpoint_closure(rejections, observed=observed)
        proof, proofs = self._closure_proof()
        closed = f"{rejections.closed_receipts} AND {proof}"
        if not self._has_receipts(closed, (STOP_SESSION, *proofs), rejections):
            return
        session = self.controller.session
        pending, windows = stop_reply_window_predicate(session.journal, session.gateway.charger.reply_windows())
        self._update_receipts(
            STOP_COMPLETION_FIELDS,
            (),
            f"{closed} AND NOT {pending}",
            (STOP_SESSION, *proofs, *windows),
            rejections=rejections,
            retry_after_rollback=True,
        )

    # Cloud reconciliation must not erase an independently confirmed local closure.
    async def confirm_while_blocked(self) -> None:
        c, j = self.controller, self.controller.session.journal
        if c.stopping:
            return
        rejections = stop_rejections(j)
        if not self._has_receipts(rejections.closed_receipts, (STOP_SESSION,), rejections):
            return
        try:
            native = await c.native.read(stop_only=True)
            c.native.require_known_session(native)
            c.commands.decide(partial(self.observed, native), priority=CommandPriority.RESTRICT)
        except (
            DeniedError,
            NativeTransportUnavailableError,
            RecoveryRequiredError,
            SecurityError,
            StaleEvidenceError,
            UnavailableError,
            TimeoutError,
        ):
            pass  # Neither an unavailable local reading nor a cloud fault proves closure.

    # During a real Stop, either of the two observations can confirm closure before the other.
    def closure_pending(self, native: HandoverEvidence) -> bool:
        j, physical = self.controller.session.journal, native.physical
        if physical.power_w != 0 or any(value != 0 for value in native.currents_a + native.powers_w):
            return False
        active = j.active()
        if not physical.transaction_open and physical.native_transaction_id is None and active is not None:
            return j.outcomes.session_stop_tracks_closure(active)
        local = physical.native_transaction_id
        if not physical.transaction_open or local is None or active is not None:
            return False
        return j.outcomes.latest_closed_session_stop_id() == local

    # Retire the wire worker before the journal closes. Its dispatch marker survives cancellation.
    async def close(self) -> None:
        if self.worker is not None:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)


class StopAlreadyHandledError(RecoveryRequiredError):
    """A previous dispatch or real closure already owns this session's Stop."""
