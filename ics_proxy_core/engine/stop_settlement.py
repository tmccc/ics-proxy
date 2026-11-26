"""Shared evidence for settling the exact attempts behind a session Stop request."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, Literal

from .store import encoded

if TYPE_CHECKING:
    from .command_outcomes import CommandOutcomes, TransactionReplyStatus
    from .store import Journal

STOP_SESSION = "stop_session"
STOP_NONTERMINAL_CLASSIFICATIONS = ("stop_not_confirmed", "process_replaced")
STOP_CLOSURE_VERIFIED = "json_type(result,'$._session_stop.closure_verified') IS 'true'"
STOP_CLOSURE_FIELDS = "result=json_set(result,'$._session_stop.closure_verified',json('true'))"
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


# A later reply must belong to one of the specific attempts the receipt joined.
def joined_stop_replies(
    outcomes: CommandOutcomes, request: str, binding: object, status: TransactionReplyStatus
) -> Iterator[StopAttempt]:
    if not isinstance(binding, dict) or (local := joined_stop_session(binding)) is None:
        return
    owner_bindings: tuple[tuple[str, Literal["request", "wire"]], ...] = (
        ("local_attempts", "request"),
        ("owner_wire_attempts", "wire"),
    )
    for field, key in owner_bindings:
        attempts = binding.get(field)
        if not isinstance(attempts, list):
            continue
        for attempt in attempts:
            if not isinstance(attempt, str) or not attempt.isprintable() or attempt == request:
                continue
            for wire in outcomes.owner_wires(key, attempt):
                predecessor = wire.request or wire.ticket
                if (
                    wire.local == local
                    and wire.action == "RemoteStopTransaction"
                    and wire.reply == status
                    and predecessor != request
                ):
                    yield StopAttempt("local", predecessor, wire.wire)
    attempts = binding.get("vendor_attempts")
    if isinstance(attempts, list) and attempts:
        retained_rejections = set(outcomes.buffered_stop_rejections()[1]) if status == "Rejected" else set()
        for attempt in attempts:
            if (
                isinstance(attempt, str)
                and attempt.isprintable()
                and (vendor := outcomes.vendor(attempt)) is not None
                and vendor.action == "RemoteStopTransaction"
                and vendor.wire is not None
                and vendor.session is not None
                and vendor.session.local == local
                and (vendor.status == status or (vendor.token, vendor.wire) in retained_rejections)
            ):
                yield StopAttempt("vendor", vendor.token, vendor.wire)


# One genuine acceptance supplies the wire side of a joined Stop; closure is established independently.
def resolve_joined_stop_acceptance(outcomes: CommandOutcomes, request: str, binding: object) -> StopAttempt | None:
    return joined_stop_acceptance(binding, request) or next(
        joined_stop_replies(outcomes, request, binding, "Accepted"), None
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
        return (
            f"action=? AND state IN ('accepted','running','uncertain') AND NOT {self.receipts} AND EXISTS ("
            "SELECT 1 FROM transactions WHERE local=json_extract(managed_requests.result,'$._session_stop.local') "
            "AND stop IS NOT NULL)"
        )


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
        patch = joined_stop_rejection_patch(journal.outcomes, request, binding)
        accepted = dict.fromkeys(joined_stop_replies(journal.outcomes, request, binding, "Accepted"))
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
def joined_stop_rejection_patch(outcomes: CommandOutcomes, request: str, binding: object) -> dict[str, Any]:
    rejected = dict.fromkeys(joined_stop_replies(outcomes, request, binding, "Rejected"))
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
        if patch := joined_stop_rejection_patch(journal.outcomes, request, json.loads(binding)):
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
