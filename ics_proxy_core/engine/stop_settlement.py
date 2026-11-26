"""Shared evidence for settling the exact attempts behind a session Stop request."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, Literal

from .store import encoded

if TYPE_CHECKING:
    from .command_outcomes import CommandOutcomes, OwnerWire, TransactionReplyStatus
    from .store import Journal

PAUSE_STOP_HISTORY = "_stop_history"
STOP_SESSION = "stop_session"
STOP_NONTERMINAL_CLASSIFICATIONS = ("stop_not_confirmed", "process_replaced")
STOP_CLOSURE_VERIFIED = "json_type(result,'$._session_stop.closure_verified') IS 'true'"
STOP_CLOSURE_FIELDS = "result=json_set(result,'$._session_stop.closure_verified',json('true'))"
STOP_REPORT_CLOSED = (
    "EXISTS (SELECT 1 FROM transactions WHERE local=json_extract(managed_requests.result,'$._session_stop.local') "
    "AND stop IS NOT NULL)"
)
STOP_OBSERVABLE_CLOSURE = (
    "action=? AND json_type(result,'$._session_stop.local')='integer' "
    "AND (state IN ('accepted','running','uncertain') "
    "OR (state='failed' AND json_extract(result,'$.classification')='stop_rejected')) "
    f"AND {STOP_REPORT_CLOSED} AND NOT {STOP_CLOSURE_VERIFIED}"
)
_REJECTED_STOP_RESULT = "json_set(result,'$.classification','stop_rejected')"
_COMPLETED_STOP_RESULT = (
    "json_set(json_remove(result,'$.classification'),"
    "'$.session_stopped',json('true'),'$._session_stop.closure_verified',json('true'))"
)
STOP_COMPLETION_FIELDS = f"state='completed',result={_COMPLETED_STOP_RESULT}"
STOP_BUFFERED_CLOSURE = (
    "EXISTS (SELECT 1 FROM json_each(?) proof WHERE proof.key=managed_requests.id "
    "AND proof.value=json_extract(managed_requests.result,'$._session_stop.local'))"
)
_OPEN_REPLY = (
    "EXISTS (SELECT 1 FROM json_each(?) windows "
    "WHERE json_extract(windows.value,'$[2]')=json_extract(managed_requests.result,'$._session_stop.local') AND ("
    "(json_extract(windows.value,'$[0]')='local' AND (json_extract(windows.value,'$[1]')=managed_requests.id "
    "OR EXISTS (SELECT 1 FROM json_each(managed_requests.result,'$._session_stop.local_attempts') a "
    "WHERE a.value=json_extract(windows.value,'$[1]')))) OR "
    "(json_extract(windows.value,'$[0]')='vendor' AND EXISTS ("
    "SELECT 1 FROM json_each(managed_requests.result,'$._session_stop.vendor_attempts') a "
    "WHERE a.value=json_extract(windows.value,'$[1]'))) OR "
    "(json_extract(windows.value,'$[0]')='owner_wire' AND EXISTS ("
    "SELECT 1 FROM json_each(managed_requests.result,'$._session_stop.owner_wire_attempts') a "
    "WHERE a.value=json_extract(windows.value,'$[1]')))))"
)
_OWNER_REJECTIONS = (
    "SELECT request,local,wire FROM owner_command_wires WHERE action='RemoteStopTransaction' AND reply='Rejected'"
)
_VENDOR_STOP_SCOPE = (
    "SELECT v.mutation,v.local FROM vendor_command_scope v JOIN mutations m ON m.token=v.mutation "
    "WHERE m.origin='vendor' AND m.action='RemoteStopTransaction'"
)
_VENDOR_REJECTIONS = f"{_VENDOR_STOP_SCOPE} AND v.wire_result='Rejected'"
_EFFECTIVE_REJECTIONS = (
    f"WITH owner_rejections(request,local,wire) AS ({_OWNER_REJECTIONS} UNION ALL "
    "SELECT json_extract(value,'$[0]'),json_extract(value,'$[1]'),json_extract(value,'$[2]') FROM json_each(?)), "
    f"vendor_rejections(mutation,local) AS ({_VENDOR_REJECTIONS} UNION ALL {_VENDOR_STOP_SCOPE} "
    "AND EXISTS (SELECT 1 FROM json_each(?) WHERE json_extract(value,'$[0]')=v.mutation "
    "AND json_extract(value,'$[1]')=v.wire_request)) "
)


@dataclass(frozen=True)
class StopAttempt:
    """The exact predecessor a joining request used."""

    origin: Literal["local", "vendor"]
    identifier: str
    wire: str | None


@dataclass(frozen=True)
class StopPredecessors:
    """The earlier Stop attempts to which a joining request was bound."""

    local: int
    accepted: StopAttempt | None = None
    local_requests: tuple[str, ...] = ()
    owner_wires: tuple[str, ...] = ()
    vendor_mutations: tuple[str, ...] = ()
    unidentified_shutdown_stop: bool = False


# The joining receipt owns a single session and must have delegated its send.
def joined_stop_session(binding: object) -> int | None:
    if not isinstance(binding, dict):
        return None
    local, joined, dispatched = binding.get("local"), binding.get("joined"), binding.get("dispatched")
    if (
        type(local) is not int
        or not 0 < local < 2**63
        or type(joined) not in (int, bool)
        or joined != 1
        or (dispatched is not None and dispatched is not False)
    ):
        return None
    return local


# Read one exact predecessor saved on a joining receipt.
def saved_stop_attempt(attempt: object, request: str) -> StopAttempt | None:
    if not isinstance(attempt, dict) or attempt.keys() != {"origin", "identifier", "wire"}:
        return None
    origin, predecessor, wire = attempt["origin"], attempt["identifier"], attempt["wire"]
    if (
        origin not in ("local", "vendor")
        or not isinstance(predecessor, str)
        or predecessor == request
        or re.fullmatch(r"[a-f0-9]{32}", predecessor) is None
        or not isinstance(wire, str)
        or not 0 < len(wire) <= 36
    ):
        return None
    return StopAttempt(origin, predecessor, wire)


# Read the exact predecessor that this receipt has already saved as accepted.
def joined_stop_acceptance(binding: object, request: str) -> StopAttempt | None:
    if not isinstance(binding, dict) or joined_stop_session(binding) is None:
        return None
    return saved_stop_attempt(binding.get("accepted_attempt"), request)


# Decode the supplemental accepted and rejected replies with the same binding rules as the primary reply.
def saved_stop_replies(
    binding: dict[str, Any], request: str, *, limit: int
) -> Iterator[tuple[StopAttempt, TransactionReplyStatus]]:
    if joined_stop_session(binding) is None:
        return
    fields: tuple[tuple[str, TransactionReplyStatus], ...] = (
        ("additional_accepted_attempts", "Accepted"),
        ("rejected_attempts", "Rejected"),
    )
    for field, reply in fields:
        items = binding.get(field)
        if isinstance(items, list):
            for item in items[:limit]:
                if (attempt := saved_stop_attempt(item, request)) is not None:
                    yield attempt, reply


# Decode only the original attempt identities named by the joining receipt.
def bound_stop_ids(binding: dict[str, Any], field: str, request: str) -> tuple[str, ...]:
    items = binding.get(field)
    if not isinstance(items, list):
        return ()
    return tuple(
        dict.fromkeys(item for item in items if isinstance(item, str) and item.isprintable() and item != request)
    )


# A logical owner request can have several sends. Restrict each one to this exact action and local session.
def bound_owner_stops(
    outcomes: CommandOutcomes, local: int, key: Literal["request", "wire"], identifier: str
) -> tuple[OwnerWire, ...]:
    return tuple(
        wire
        for wire in outcomes.owner_wires(key, identifier)
        if wire.action == "RemoteStopTransaction" and wire.local == local
    )


# A prepared or unrelated vendor mutation cannot lend its reply to a joined Stop.
def bound_vendor_stop(
    outcomes: CommandOutcomes,
    local: int,
    identifier: str,
    retained: dict[tuple[str, str], TransactionReplyStatus],
) -> tuple[StopAttempt, TransactionReplyStatus] | None:
    vendor = outcomes.vendor(identifier)
    if (
        vendor is None
        or vendor.action != "RemoteStopTransaction"
        or vendor.wire is None
        or vendor.session is None
        or vendor.session.local != local
    ):
        return None
    reply = vendor.status
    if reply == "Accepted":
        return StopAttempt("vendor", identifier, vendor.wire), "Accepted"
    if reply == "Rejected":
        return StopAttempt("vendor", identifier, vendor.wire), "Rejected"
    if reply is None and (saved := retained.get((identifier, vendor.wire))) is not None:
        return StopAttempt("vendor", identifier, vendor.wire), saved
    return None


# A logical predecessor with several wires refuses only when every represented wire has refused.
def owner_stop_reply(wires: tuple[OwnerWire, ...]) -> TransactionReplyStatus | None:
    replies = {wire.reply for wire in wires}
    if "Accepted" in replies:
        return "Accepted"
    return "Rejected" if replies == {"Rejected"} else None


# A vendor follower can borrow a definite reply only from the exact attempts bound at admission.
def predecessor_stop_reply(
    outcomes: CommandOutcomes, local: int, earlier: StopPredecessors
) -> TransactionReplyStatus | None:
    if earlier.local != local:
        return None
    if earlier.accepted is not None:
        return "Accepted"
    retained = outcomes.retained_vendor_stop_replies() if earlier.vendor_mutations else {}
    replies = [
        *(
            owner_stop_reply(bound_owner_stops(outcomes, local, "request", request))
            for request in earlier.local_requests
        ),
        *(owner_stop_reply(bound_owner_stops(outcomes, local, "wire", wire)) for wire in earlier.owner_wires),
        *(
            reply[1] if (reply := bound_vendor_stop(outcomes, local, token, retained)) is not None else None
            for token in earlier.vendor_mutations
        ),
    ]
    if "Accepted" in replies:
        return "Accepted"
    if replies and not earlier.unidentified_shutdown_stop and all(reply == "Rejected" for reply in replies):
        return "Rejected"
    return None


# A later reply must belong to one of the specific attempts the receipt joined.
def joined_stop_replies(
    outcomes: CommandOutcomes, request: str, binding: object
) -> Iterator[tuple[StopAttempt, TransactionReplyStatus]]:
    if not isinstance(binding, dict) or (local := joined_stop_session(binding)) is None:
        return
    owner_bindings: tuple[tuple[str, Literal["request", "wire"]], ...] = (
        ("local_attempts", "request"),
        ("owner_wire_attempts", "wire"),
    )
    for field, key in owner_bindings:
        for attempt in bound_stop_ids(binding, field, request):
            for wire in bound_owner_stops(outcomes, local, key, attempt):
                predecessor = wire.request or wire.ticket
                if predecessor != request:
                    if wire.reply == "Accepted":
                        yield StopAttempt("local", predecessor, wire.wire), "Accepted"
                    elif wire.reply == "Rejected":
                        yield StopAttempt("local", predecessor, wire.wire), "Rejected"
    attempts = bound_stop_ids(binding, "vendor_attempts", request)
    if attempts:
        retained = outcomes.retained_vendor_stop_replies()
        for attempt in attempts:
            reply = bound_vendor_stop(outcomes, local, attempt, retained)
            if reply is not None:
                yield reply


# One genuine acceptance supplies the wire side of a joined Stop; closure is established independently.
def resolve_joined_stop_acceptance(outcomes: CommandOutcomes, request: str, binding: object) -> StopAttempt | None:
    return joined_stop_acceptance(binding, request) or next(
        (attempt for attempt, reply in joined_stop_replies(outcomes, request, binding) if reply == "Accepted"), None
    )


# Retain only reply windows that still belong to this receipt or one of its joined Stop attempts.
def stop_reply_window_predicate(journal: Journal, wires: tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
    attempts = journal.outcomes.open_stop_replies(wires) if wires else ()
    return (_OPEN_REPLY, (encoded(attempts),)) if attempts else ("0", ())


@dataclass(frozen=True)
class StopRejections:
    """The same saved and retained replies feed send decisions and receipt publication."""

    prefix: str = ""
    bindings: tuple[str, ...] = ()
    owner: str = f"({_OWNER_REJECTIONS})"
    vendor: str = f"({_VENDOR_REJECTIONS})"

    # A refusal must name both the request and the exact session that the request could stop.
    def owner_refusal(self, request: str, local: str) -> str:
        return f"EXISTS (SELECT 1 FROM {self.owner} w WHERE w.request={request} AND w.local={local})"

    # An owner Stop can have multiple charger calls under the same request.
    def owner_wire_refusal(self, wire: str, local: str) -> str:
        return f"EXISTS (SELECT 1 FROM {self.owner} w WHERE w.wire={wire} AND w.local={local})"

    # A vendor refusal belongs to the recorded mutation and its translated local session.
    def vendor_refusal(self, mutation: str, local: str) -> str:
        return f"EXISTS (SELECT 1 FROM {self.vendor} v WHERE v.mutation={mutation} AND v.local={local})"

    # A direct refusal suffices. Every attempt named by a joined request must have refused it.
    @property
    def receipts(self) -> str:
        local = "json_extract(managed_requests.result,'$._session_stop.local')"
        return (
            f"({self.owner_refusal('managed_requests.id', local)} OR "
            "NOT COALESCE(json_extract(result,'$._session_stop.unidentified_shutdown_stop'),0) AND "
            "(COALESCE(json_array_length(result,'$._session_stop.local_attempts'),0) "
            "+ COALESCE(json_array_length(result,'$._session_stop.vendor_attempts'),0) "
            "+ COALESCE(json_array_length(result,'$._session_stop.owner_wire_attempts'),0)) > 0 "
            "AND NOT EXISTS (SELECT 1 FROM json_each(result,'$._session_stop.local_attempts') a "
            f"WHERE NOT {self.owner_refusal('a.value', local)}) "
            "AND NOT EXISTS (SELECT 1 FROM json_each(result,'$._session_stop.vendor_attempts') a "
            f"WHERE NOT {self.vendor_refusal('a.value', local)}) "
            "AND NOT EXISTS (SELECT 1 FROM json_each(result,'$._session_stop.owner_wire_attempts') a "
            f"WHERE NOT {self.owner_wire_refusal('a.value', local)}))"
        )

    # A definite Stop rejection settles receipts that have begun and still await a result.
    @property
    def rejected_receipts(self) -> str:
        return f"action=? AND state IN ('running','uncertain') AND {self.receipts}"

    # A genuine closure settles the waiting request when its exact Stop attempts still permit it.
    @property
    def closed_receipts(self) -> str:
        rejected = self.receipts
        return f"action=? AND state IN ('accepted','running','uncertain') AND NOT {rejected} AND {STOP_REPORT_CLOSED}"


SAVED_STOP_REJECTIONS = StopRejections()


# Preserve the exact replies in the same update as closure, before the original vendor mutations can rotate out.
def stop_completion_fields(
    journal: Journal, rejections: StopRejections, where: str, bindings: tuple[Any, ...]
) -> tuple[str, tuple[str, ...]]:
    rows = journal.db.execute(
        f"{rejections.prefix}SELECT id,json_extract(result,'$._session_stop') FROM managed_requests WHERE {where} "
        "AND json_extract(result,'$._session_stop.joined')=1",
        (*rejections.bindings, *bindings),
    )
    patches = {}
    for request, raw_binding in rows:
        binding = json.loads(raw_binding)
        replies = dict.fromkeys(joined_stop_replies(journal.outcomes, request, binding))
        patch = joined_stop_rejection_patch(replies)
        accepted = dict.fromkeys(attempt for attempt, reply in replies if reply == "Accepted")
        primary = joined_stop_acceptance(binding, request)
        if primary is None and (primary := next(iter(accepted), None)) is not None:
            patch["accepted_attempt"] = asdict(primary)
        if primary is not None:
            accepted.pop(primary, None)
        if accepted:
            patch["additional_accepted_attempts"] = [asdict(attempt) for attempt in accepted]
        if patch:
            patches[request] = {"_session_stop": patch}
    if not patches:
        return STOP_COMPLETION_FIELDS, ()
    return (
        f"state='completed',result=json_patch({_COMPLETED_STOP_RESULT},"
        "COALESCE((SELECT value FROM json_each(?) WHERE key=managed_requests.id),'{}'))",
        (encoded(patches),),
    )


# Use retained replies until their original wire rows are available to the same selector.
def stop_rejections(journal: Journal) -> StopRejections:
    owner, vendor = journal.outcomes.buffered_stop_rejections()
    if not owner and not vendor:
        return SAVED_STOP_REJECTIONS
    return StopRejections(
        _EFFECTIVE_REJECTIONS, (encoded(owner), encoded(vendor)), "owner_rejections", "vendor_rejections"
    )


# Preserve proved refusals even when a distinct attempt accepted the Stop.
def joined_stop_rejection_patch(
    replies: Iterable[tuple[StopAttempt, TransactionReplyStatus]],
) -> dict[str, Any]:
    rejected = dict.fromkeys(attempt for attempt, reply in replies if reply == "Rejected")
    return {"rejected_attempts": [asdict(attempt) for attempt in rejected]} if rejected else {}


# Save the exact rejected predecessors in the same update that makes the receipt terminal.
def stop_rejection_fields(journal: Journal, rejections: StopRejections) -> tuple[str, tuple[str, ...]]:
    rows = journal.db.execute(
        f"{rejections.prefix}SELECT id,json_extract(result,'$._session_stop') FROM managed_requests "
        f"WHERE {rejections.rejected_receipts} AND json_extract(result,'$._session_stop.joined')=1",
        (*rejections.bindings, STOP_SESSION),
    )
    patches = {}
    for request, binding in rows:
        replies = joined_stop_replies(journal.outcomes, request, json.loads(binding))
        if patch := joined_stop_rejection_patch(replies):
            patches[request] = {"_session_stop": patch}
    if not patches:
        return f"state='failed',result={_REJECTED_STOP_RESULT}", ()
    return (
        f"state='failed',result=json_patch({_REJECTED_STOP_RESULT},"
        "COALESCE((SELECT value FROM json_each(?) WHERE key=managed_requests.id),'{}'))",
        (encoded(patches),),
    )


# Project the same definite refusal when its receipt write still awaits a retry.
def stop_receipt_rejected(journal: Journal, request: str) -> bool:
    rejections = stop_rejections(journal)
    row = journal.db.execute(
        f"{rejections.prefix}SELECT 1 FROM managed_requests WHERE id=? AND {rejections.rejected_receipts}",
        (*rejections.bindings, request, STOP_SESSION),
    ).fetchone()
    return row is not None


# Publish Stop rejections already proved by their exact charger replies in the caller's transaction if present.
def settle_saved_stop_rejections(journal: Journal) -> None:
    db = journal.db
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='managed_requests'").fetchone():
        return
    rejections = stop_rejections(journal)
    where = rejections.rejected_receipts
    bindings = (*rejections.bindings, STOP_SESSION)
    if db.execute(f"{rejections.prefix}SELECT 1 FROM managed_requests WHERE {where} LIMIT 1", bindings).fetchone():
        with nullcontext() if db.in_transaction else journal.atomic():
            fields, rejected = stop_rejection_fields(journal, rejections)
            db.execute(
                f"{rejections.prefix}UPDATE managed_requests SET {fields} WHERE {where}",
                (*rejections.bindings, *rejected, STOP_SESSION),
            )
