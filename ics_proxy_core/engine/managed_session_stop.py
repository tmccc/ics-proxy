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
from .store import JournalStartBusy, RecoveryRequiredError, encoded, fingerprint

if TYPE_CHECKING:
    from .managed import ManagedController
    from .store import Journal
    from .trial_handover_evidence import HandoverEvidence

STOP_SESSION = "stop_session"
CONFIRMATION_SECONDS = 20
STOP_COMPLETION_FIELDS = "state='completed',result=json_set(result,'$.session_stopped',json('true'))"
_OWNER_REJECTIONS = (
    "SELECT request,local FROM owner_command_wires WHERE action='RemoteStopTransaction' AND reply='Rejected'"
)
_VENDOR_STOP_SCOPE = (
    "SELECT v.mutation,v.local FROM vendor_command_scope v JOIN mutations m ON m.token=v.mutation "
    "WHERE m.origin='vendor' AND m.action='RemoteStopTransaction'"
)
_VENDOR_REJECTIONS = f"{_VENDOR_STOP_SCOPE} AND v.wire_result='Rejected'"
_BUFFERED_REJECTIONS = (
    f"WITH owner_rejections(request,local) AS ({_OWNER_REJECTIONS} UNION ALL "
    "SELECT json_extract(value,'$[0]'),json_extract(value,'$[1]') FROM json_each(?)), "
    f"vendor_rejections(mutation,local) AS ({_VENDOR_REJECTIONS} UNION ALL {_VENDOR_STOP_SCOPE} "
    "AND EXISTS (SELECT 1 FROM json_each(?) WHERE json_extract(value,'$[0]')=v.mutation "
    "AND json_extract(value,'$[1]')=v.wire_request)) "
)


# One direct refusal suffices. A joined request needs a refusal from every attempt it actually joined.
def _rejected_attempts(owner: str, vendor: str) -> str:
    local = "json_extract(managed_requests.result,'$._session_stop.local')"
    return (
        f"(EXISTS (SELECT 1 FROM {owner} w WHERE w.request=managed_requests.id AND w.local={local}) OR "
        "COALESCE(json_array_length(result,'$._session_stop.local_attempts') "
        "+ json_array_length(result,'$._session_stop.vendor_attempts'),0) > 0 "
        "AND NOT EXISTS (SELECT 1 FROM json_each(result,'$._session_stop.local_attempts') a "
        f"WHERE NOT EXISTS (SELECT 1 FROM {owner} w WHERE w.request=a.value AND w.local={local})) "
        "AND NOT EXISTS (SELECT 1 FROM json_each(result,'$._session_stop.vendor_attempts') a "
        f"WHERE NOT EXISTS (SELECT 1 FROM {vendor} v WHERE v.mutation=a.value AND v.local={local})))"
    )


def _closed_requests(rejected: str) -> str:
    return (
        f"action=? AND state IN ('accepted','running','uncertain') AND NOT {rejected} AND EXISTS ("
        "SELECT 1 FROM transactions WHERE local=json_extract(managed_requests.result,'$._session_stop.local') "
        "AND stop IS NOT NULL)"
    )


_SAVED_REJECTED_ATTEMPTS = _rejected_attempts(f"({_OWNER_REJECTIONS})", f"({_VENDOR_REJECTIONS})")
_LIVE_REJECTED_ATTEMPTS = _rejected_attempts("owner_rejections", "vendor_rejections")
CLOSED_REQUESTS = _closed_requests(_SAVED_REJECTED_ATTEMPTS)
_LIVE_CLOSED_REQUESTS = _closed_requests(_LIVE_REJECTED_ATTEMPTS)


# Offline and ordinary live checks use only persisted rows. Add exact buffered evidence only when present.
def _buffered_rejection_binding(journal: Journal) -> tuple[str, ...]:
    owner, vendor = journal.outcomes.buffered_stop_rejections()
    return (encoded(owner), encoded(vendor)) if owner or vendor else ()


@dataclass(frozen=True)
class SessionStop:
    """The identity chosen at admission survives later UI and charger updates."""

    identifier: str
    target: str
    local: int
    intent: ControlIntent
    connection: ControlScope


class ManagedSessionStop:
    """Use the shared Stop decision and settle only against the exact session's closure."""

    # Replacement can retire an unsent request. A dispatch marker still owns its original possible effect.
    def __init__(self, controller: ManagedController) -> None:
        self.controller = controller
        self.worker: asyncio.Task[None] | None = None
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
        if self.worker is not None and not self.worker.done():
            raise RecoveryRequiredError("the current session Stop is still being sent")
        accepted = time.time()
        confirmation_deadline = time.monotonic() + CONFIRMATION_SECONDS
        with j.atomic():
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
                    },
                },
            )
        scope = SessionStop(identifier, target, local, intent, connection)
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

        # Commit possible dispatch before the sender can write, including when the wire reply is lost.
        def dispatch(_action: str) -> None:
            decision = g.controls.decisions.session_stop_send_decision(scope.local, boot)
            if decision is StopDecision.CLOSED:
                raise StopAlreadyHandledError
            with j.atomic():
                binding: dict[str, Any] = {"dispatched": 1} if decision is StopDecision.SEND else {"joined": 1}
                if decision is StopDecision.WAIT_FOR_REPORT and not j.outcomes.unanswered_shutdown_stop(scope.local):
                    binding["local_attempts"] = j.outcomes.session_stop_dispatches(scope.local)
                    binding["vendor_attempts"] = j.outcomes.vendor_stop_dispatches(scope.local)
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
                answer = await call("RemoteStopTransaction", remote_stop_transaction(scope.local))
            if answer[0] == 3 and answer[2] == {"status": "Rejected"}:
                c.commands.decide(partial(self._fail, scope.identifier, "stop_rejected"))
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

    # A known rejection permits another deliberate attempt without forgetting the original wire result.
    def _fail(self, identifier: str, reason: str) -> None:
        self.controller.session.journal.db.execute(
            "UPDATE managed_requests SET state='failed',result=json_set(result,'$.classification',?) "
            "WHERE id=? AND state IN ('accepted','running')",
            (reason, identifier),
        )

    # Retire the client wait without resending or erasing a Stop that might still take effect.
    def expire(self, *, replaced: bool = False) -> None:
        buffered = _buffered_rejection_binding(self.controller.session.journal)
        rejected = _LIVE_REJECTED_ATTEMPTS if buffered else _SAVED_REJECTED_ATTEMPTS
        self._update_receipts(
            "state='failed',result=json_set(result,'$.classification','stop_rejected')",
            (),
            f"action=? AND state IN ('running','uncertain') AND {rejected}",
            (STOP_SESSION,),
            buffered=buffered,
            retry_after_rollback=not replaced,
        )
        # The same rejection evidence also excludes a receipt from closure while its failure write awaits retry.
        self._update_receipts(
            "state=CASE WHEN json_extract(result,'$._session_stop.dispatched') "
            "OR json_extract(result,'$._session_stop.joined') "
            "THEN 'uncertain' ELSE 'failed' END,result=json_set(result,'$.classification',?)",
            ("process_replaced" if replaced else "stop_not_confirmed",),
            f"action=? AND state IN ('accepted','running') AND NOT {rejected} "
            "AND (? OR COALESCE(json_extract(result,'$._session_stop.confirmation_deadline')<=?, "
            "json_extract(result,'$.accepted_at')<=?))",
            (STOP_SESSION, replaced, time.monotonic(), time.time() - CONFIRMATION_SECONDS),
            buffered=buffered,
            retry_after_rollback=not replaced,
        )

    # Skip receipt writes and optional native probes when none of the exact eligible rows exists.
    def _has_receipts(self, where: str, binding: tuple[Any, ...], buffered: tuple[str, ...]) -> bool:
        prefix = _BUFFERED_REJECTIONS if buffered else ""
        row = self.controller.session.journal.db.execute(
            f"{prefix}SELECT 1 FROM managed_requests WHERE {where} LIMIT 1", buffered + binding
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
        buffered: tuple[str, ...] = (),
        retry_after_rollback: bool = False,
    ) -> None:
        j = self.controller.session.journal
        if not self._has_receipts(where, binding, buffered):
            return
        prefix = _BUFFERED_REJECTIONS if buffered else ""
        transaction_started = False
        try:
            with j.atomic(retry_busy=True):
                transaction_started = True
                j.db.execute(
                    f"{prefix}UPDATE managed_requests SET {assignments} WHERE {where}", buffered + values + binding
                )
        except JournalStartBusy:
            return  # The existing receipt is still accurate because the write never began.
        except sqlite3.Error:
            if not retry_after_rollback or not transaction_started or j.db.in_transaction:
                raise
            # The original evidence remains intact. The next poll can retry publication.

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
        buffered = _buffered_rejection_binding(j)
        self._update_receipts(
            STOP_COMPLETION_FIELDS,
            (),
            _LIVE_CLOSED_REQUESTS if buffered else CLOSED_REQUESTS,
            (STOP_SESSION,),
            buffered=buffered,
            retry_after_rollback=True,
        )

    # Cloud reconciliation must not erase an independently confirmed local closure.
    async def confirm_while_blocked(self) -> None:
        c, j = self.controller, self.controller.session.journal
        if c.stopping:
            return
        buffered = _buffered_rejection_binding(j)
        if not self._has_receipts(_LIVE_CLOSED_REQUESTS if buffered else CLOSED_REQUESTS, (STOP_SESSION,), buffered):
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
        latest = j.db.execute("SELECT local,stop FROM transactions ORDER BY local DESC LIMIT 1").fetchone()
        return bool(
            latest is not None
            and latest["local"] == local
            and latest["stop"] is not None
            and j.outcomes.session_stop_tracks_closure(local)
        )

    # Retire the wire worker before the journal closes. Its dispatch marker survives cancellation.
    async def close(self) -> None:
        if self.worker is not None:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)


class StopAlreadyHandledError(RecoveryRequiredError):
    """A previous dispatch or real closure already owns this session's Stop."""
