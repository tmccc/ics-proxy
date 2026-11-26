"""Project retained Stop evidence without opening a service or changing the journal."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from ics_proxy_core.engine.command_outcomes import decode_retained_owner_reply
from ics_proxy_core.engine.gateway import timestamp as charger_timestamp
from ics_proxy_core.engine.ics import unique_object
from ics_proxy_core.engine.managed_diagnostics import timestamp as receipt_timestamp
from ics_proxy_core.engine.policy import DeniedError
from ics_proxy_core.engine.stop_settlement import (
    STOP_NONTERMINAL_CLASSIFICATIONS,
    STOP_SESSION,
    bound_stop_ids,
    joined_stop_acceptance,
    saved_stop_replies,
)
from tools.managed_incident import (
    MAX_REPORT_EVENTS,
    UNKNOWN,
    identifier,
    lifecycle_fields,
    observations,
    read_journal,
    reference,
    sampled_status,
)

STOP_ACTION = "RemoteStopTransaction"
DEFINITE_REPLIES = {"Accepted": "accepted", "Rejected": "rejected"}
REPLIES = {
    **DEFINITE_REPLIES,
    "call_error": "call_error",
    "error": "call_error",
    "NotSupported": "other",
    "Unknown": "other",
    "other": "other",
    "reply": "other",
}


# Duplicate JSON keys could make SQLite and Python choose different values for a request binding.
def object_fields(value: object) -> dict[str, Any]:
    try:
        parsed = json.loads(value, object_pairs_hook=unique_object) if isinstance(value, (str, bytes)) else None
    except ValueError, RecursionError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


# A lifecycle query requires a positive SQLite transaction ID.
def local_id(value: object) -> int | None:
    return value if type(value) is int and 0 < value < 2**63 else None


# The transport bounds CALL identifiers before dispatch.
def wire_id(value: object) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= 36 else None


# Dispatch, an exact reply, and predecessor evidence are independently available.
def wire_fields(dispatch: str = UNKNOWN, reply: str = UNKNOWN, source: str | None = None) -> dict[str, Any]:
    return {"dispatch": dispatch, "reply": reply, "reply_source": source, "joined_attempts": []}


# Older journals keep accepted Stops under their original key.
def checkpoint_keys(origin: str, wire: str) -> tuple[str, str]:
    return f"retained_stop_{origin}:{wire}", f"retained_transaction_reply_{origin}:{wire}"


# A retained owner reply can survive refusal of both primary writes. It must still match the original claim.
def owner_reply(db: sqlite3.Connection, tables: set[str], request: str, local: int) -> dict[str, Any]:
    primary = []
    if "owner_command_wires" in tables:
        primary = db.execute(
            "SELECT wire,boot,reply,local FROM owner_command_wires WHERE request=? AND ticket=? AND action=? LIMIT 2",
            (request, request, STOP_ACTION),
        ).fetchall()
    if len(primary) > 1 or primary and (wire_id(primary[0][0]) is None or local_id(primary[0][3]) != local):
        return wire_fields("possible_dispatch")
    if primary and primary[0][2] is not None:
        primary_reply = REPLIES.get(primary[0][2], UNKNOWN)
        return wire_fields("possible_dispatch", primary_reply, "wire_journal" if primary_reply != UNKNOWN else None)

    if not primary:
        claim = db.execute(
            "SELECT CASE WHEN length(result)<=65536 THEN CAST(result AS BLOB) END FROM managed_requests "
            "WHERE id=? AND action=?",
            (request, STOP_SESSION),
        ).fetchone()
        binding = object_fields(claim[0]).get("_session_stop") if claim is not None else None
        if (
            not isinstance(binding, dict)
            or local_id(binding.get("local")) != local
            or type(binding.get("dispatched")) not in (int, bool)
            or binding["dispatched"] != 1
        ):
            return wire_fields()

    checkpoints = []
    if "meta" in tables:
        checkpoints = db.execute(
            "WITH candidates AS (SELECT key,CASE WHEN length(value)<=8192 AND json_valid(value) THEN value END AS v "
            "FROM meta WHERE key GLOB 'retained_stop_owner:*' OR key GLOB 'retained_transaction_reply_owner:*') "
            "SELECT key,v FROM candidates WHERE json_extract(v,'$.request')=? AND json_extract(v,'$.ticket')=? "
            "AND json_extract(v,'$.action')=? AND json_extract(v,'$.local')=? LIMIT 3",
            (request, request, STOP_ACTION, local),
        ).fetchall()
    if len(checkpoints) == 3:
        return wire_fields("possible_dispatch")
    matches: set[tuple[str, str]] = set()
    for key, value in checkpoints:
        row = object_fields(value)
        wire, boot, reply = wire_id(row.get("wire")), row.get("boot"), row.get("reply")
        if (
            row.keys() != {"wire", "request", "ticket", "boot", "action", "local", "limit_a", "reply"}
            or (row["request"], row["ticket"], row["action"]) != (request, request, STOP_ACTION)
            or row["limit_a"] is not None
            or wire is None
            or not isinstance(boot, str)
            or not boot
            or not isinstance(reply, str)
            or reply not in DEFINITE_REPLIES
            or local_id(row.get("local")) != local
        ):
            continue
        legacy, current = checkpoint_keys("owner", wire)
        if key != (legacy if reply == "Accepted" else current):
            continue
        if primary and (wire, boot) != tuple(primary[0][:2]):
            continue
        if (
            not primary
            and "owner_command_wires" in tables
            and db.execute("SELECT 1 FROM owner_command_wires WHERE wire=?", (wire,)).fetchone()
        ):
            continue  # This wire is already reserved for a different binding.
        matches.add((wire, reply))
    if len(matches) == 1:
        _, reply = matches.pop()
        return wire_fields("possible_dispatch", DEFINITE_REPLIES[reply], "retained_checkpoint")
    return wire_fields("possible_dispatch", UNKNOWN if matches else "missing")


# The vendor checkpoint carries the original mutation and charger CALL ID. The scope supplies its exact session.
def vendor_reply(db: sqlite3.Connection, tables: set[str], token: str, wire: object, result: object) -> dict[str, Any]:
    if result in ("prepared", "prepared_new_profile"):
        return wire_fields("not_sent")
    exact_wire = wire_id(wire)
    if exact_wire is None:
        return wire_fields()
    if isinstance(result, str) and result in REPLIES:
        return wire_fields("possible_dispatch", REPLIES[result], "wire_journal")
    if result != "dispatched":
        return wire_fields()
    if "meta" in tables:
        legacy, current = checkpoint_keys("vendor", exact_wire)
        matches: set[str] = set()
        for key, value in db.execute(
            "SELECT key,value FROM meta WHERE key IN (?,?) AND length(value)<=8192", (legacy, current)
        ):
            saved = object_fields(value)
            status = saved.get("status")
            if (
                saved.keys() == {"mutation", "action", "wire", "status"}
                and (saved["mutation"], saved["action"], saved["wire"]) == (token, STOP_ACTION, exact_wire)
                and isinstance(status, str)
                and status in DEFINITE_REPLIES
                and key == (legacy if status == "Accepted" else current)
            ):
                matches.add(status)
        if len(matches) == 1:
            return wire_fields("possible_dispatch", DEFINITE_REPLIES[matches.pop()], "retained_checkpoint")
        if matches:
            return wire_fields("possible_dispatch")
    return wire_fields("possible_dispatch", "missing")


# Only the scope of this retained mutation can identify the target session and charger call.
def vendor_scope(db: sqlite3.Connection, tables: set[str], token: str) -> tuple[Any, Any, Any]:
    if "vendor_command_scope" in tables:
        row = db.execute(
            "SELECT local,wire_request,wire_result FROM vendor_command_scope WHERE mutation=?", (token,)
        ).fetchone()
        if row is not None:
            return row[0], row[1], row[2]
    return None, None, None


# A Pause's request can own several sends. Its incident evidence must name the exact charger call.
def joined_owner_wire(db: sqlite3.Connection, tables: set[str], wire: str, local: int) -> dict[str, Any] | None:
    if "owner_command_wires" not in tables:
        return None
    primary = db.execute(
        "SELECT request,ticket,action,local,reply FROM owner_command_wires WHERE wire=?", (wire,)
    ).fetchone()
    token = None
    if primary is not None:
        request, ticket, action, target, reply = primary
        if action != STOP_ACTION or target != local:
            return None
        token = identifier(request) or identifier(ticket)
        if reply is not None:
            result = REPLIES.get(reply, UNKNOWN)
            return {
                "request": reference(token),
                "reply": result,
                "reply_source": "wire_journal" if result != UNKNOWN else None,
            }
    matches: set[tuple[str, str]] = set()
    legacy, current = checkpoint_keys("owner", wire)
    if "meta" in tables:
        for key, value in db.execute(
            "SELECT key,CAST(value AS BLOB) FROM meta WHERE key IN (?,?) AND length(value)<=8192", (legacy, current)
        ):
            saved = decode_retained_owner_reply(db, object_fields(value))
            if (
                saved is not None
                and (saved.wire, saved.action, saved.local) == (wire, STOP_ACTION, local)
                and saved.reply in DEFINITE_REPLIES
                and key == (legacy if saved.reply == "Accepted" else current)
                and (retained := (identifier(saved.request) or identifier(saved.ticket))) is not None
            ):
                matches.add((retained, saved.reply))
    if len(matches) == 1:
        token, reply = matches.pop()
        return {"request": reference(token), "reply": DEFINITE_REPLIES[reply], "reply_source": "retained_checkpoint"}
    if token is not None:
        return {"request": reference(token), "reply": UNKNOWN if matches else "missing", "reply_source": None}
    return None


# Terminal replies survive journal rotation only for the original joined request or wire named by the receipt.
def saved_joined_replies(
    binding: dict[str, Any], request: str
) -> tuple[dict[tuple[str, str], str], dict[str, tuple[str, str]]]:
    requests: dict[tuple[str, str], str] = {}
    wires: dict[str, tuple[str, str]] = {}
    for attempt, reply in saved_stop_replies(binding, request, limit=MAX_REPORT_EVENTS):
        status = DEFINITE_REPLIES[reply]
        requests.setdefault((attempt.origin, attempt.identifier), status)
        if attempt.origin == "local" and attempt.wire is not None:
            wires.setdefault(attempt.wire, (attempt.identifier, status))
    return requests, wires


# An overlap borrows evidence only from attempts explicitly named in this receipt and bound to the same session.
def joined_attempts(
    db: sqlite3.Connection, tables: set[str], request: str, local: int, binding: dict[str, Any]
) -> list[dict[str, Any]]:
    attempts = []
    saved = None
    if (accepted := joined_stop_acceptance(binding, request)) is not None:
        saved = {"request": reference(accepted.identifier), "reply": "accepted", "reply_source": "joined_receipt"}
    saved_requests, saved_wires = saved_joined_replies(binding, request)
    for origin, key in (("local", "local_attempts"), ("vendor", "vendor_attempts")):
        tokens = tuple(item for item in bound_stop_ids(binding, key, request) if identifier(item) is not None)
        for token in tokens[:MAX_REPORT_EVENTS]:
            reply = wire_fields()
            if origin == "local":
                reply = owner_reply(db, tables, token, local)
            elif (
                "mutations" in tables
                and db.execute(
                    "SELECT 1 FROM mutations WHERE token=? AND origin='vendor' AND action=?", (token, STOP_ACTION)
                ).fetchone()
            ):
                scope_local, call, result = vendor_scope(db, tables, token)
                if scope_local == local:
                    reply = vendor_reply(db, tables, token, call, result)
            if accepted is not None and (accepted.origin, accepted.identifier) == (origin, token):
                if saved is None:
                    continue
                if reply["reply"] != "accepted":
                    reply = saved
                saved = None
            elif (known := saved_requests.get((origin, token))) is not None and reply["reply"] != known:
                reply = {"reply": known, "reply_source": "joined_receipt"}
            attempts.append(
                {
                    "origin": origin,
                    "request": reference(token),
                    "reply": reply["reply"],
                    "reply_source": reply["reply_source"],
                }
            )
    wires = tuple(item for item in bound_stop_ids(binding, "owner_wire_attempts", request) if wire_id(item) is not None)
    for wire in wires[:MAX_REPORT_EVENTS]:
        evidence = joined_owner_wire(db, tables, wire, local)
        if accepted is not None and accepted.origin == "local" and accepted.wire == wire:
            if saved is None:
                continue
            if evidence is None or evidence["reply"] != "accepted":
                evidence = saved
            saved = None
        elif (checkpoint := saved_wires.get(wire)) is not None:
            predecessor, status = checkpoint
            if evidence is None or evidence["reply"] != status:
                evidence = {"request": reference(predecessor), "reply": status, "reply_source": "joined_receipt"}
        if evidence is not None:
            attempts.append({"origin": "local", **evidence})
    if accepted is not None and saved is not None:
        attempts.insert(0, {"origin": accepted.origin, **saved})
    return attempts


# Keep the latest receipt by default. An exact reference can only select a single retained local Stop.
def local_stop(
    db: sqlite3.Connection, tables: set[str], request_reference: str | None
) -> tuple[dict[str, Any] | None, int | None, dict[str, Any]]:
    if "managed_requests" not in tables:
        return None, None, wire_fields()
    db.create_function("incident_reference", 1, reference, deterministic=True)
    rows = db.execute(
        "SELECT id,revision,state,CASE WHEN length(result)<=65536 THEN result END FROM managed_requests "
        "WHERE action=? AND (? IS NULL OR incident_reference(id)=?) ORDER BY rowid DESC LIMIT ?",
        (STOP_SESSION, request_reference, request_reference, 1 if request_reference is None else 2),
    ).fetchall()
    if len(rows) != 1 or (token := identifier(rows[0][0])) is None:
        return None, None, wire_fields()
    row = rows[0]
    revision, state = row[1:3]
    result = object_fields(row[3])
    classification = result.get("classification")
    if state == "completed" and classification in STOP_NONTERMINAL_CLASSIFICATIONS:
        classification = None
    stopped = result.get("session_stopped")
    request = {
        "request": reference(token),
        "action": STOP_SESSION,
        "revision": revision if type(revision) is int and 0 <= revision < 2**53 else None,
        "state": state if state in ("accepted", "running", "uncertain", "completed", "failed") else UNKNOWN,
        "accepted_at": receipt_timestamp(result.get("accepted_at")),
        "classification": classification
        if classification in ("stop_not_sent", "stop_rejected", *STOP_NONTERMINAL_CLASSIFICATIONS)
        else None,
        "session_stopped": stopped if type(stopped) is bool else None,
    }
    binding = result.get("_session_stop")
    if not isinstance(binding, dict) or (local := local_id(binding.get("local"))) is None:
        return request, None, wire_fields()
    dispatched, joined = binding.get("dispatched"), binding.get("joined")
    if type(dispatched) in (int, bool) and dispatched == 1 and joined is None:
        return request, local, owner_reply(db, tables, token, local)
    if type(joined) in (int, bool) and joined == 1 and (dispatched is False or dispatched is None):
        wire = wire_fields("joined")
        wire["joined_attempts"] = joined_attempts(db, tables, token, local, binding)
        return request, local, wire
    return request, local, wire_fields("not_sent" if dispatched is False and joined is None else UNKNOWN)


# Vendor mutations lack receipt timestamps and retain their own insertion order.
def vendor_stop(db: sqlite3.Connection, tables: set[str]) -> tuple[dict[str, Any] | None, int | None, dict[str, Any]]:
    row = None
    if "mutations" in tables:
        row = db.execute(
            "SELECT token,state FROM mutations WHERE origin='vendor' AND action=? ORDER BY rowid DESC LIMIT 1",
            (STOP_ACTION,),
        ).fetchone()
    if row is None or (token := identifier(row[0])) is None:
        return None, None, wire_fields()
    request = {
        "request": reference(token),
        "action": STOP_ACTION,
        "state": row[1] if row[1] in ("pending", "uncertain", "confirmed") else UNKNOWN,
    }
    local, wire, result = vendor_scope(db, tables, token)
    return request, local_id(local), vendor_reply(db, tables, token, wire, result)


# The bound session's recorded closure and cloud delivery are evidence independent of any command outcome.
def journal_fields(
    db: sqlite3.Connection, *, vendor: bool, request_reference: str | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    tables = {name for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    request, local, wire = vendor_stop(db, tables) if vendor else local_stop(db, tables, request_reference)
    lifecycle = "unbound" if request is not None and local is None else UNKNOWN
    mapping, stop_at = UNKNOWN, None
    delivery: list[dict[str, Any]] = []
    if local is not None and "transactions" in tables:
        row = db.execute(
            "SELECT stop IS NOT NULL,vendor IS NOT NULL,CASE WHEN json_valid(stop) THEN "
            "CASE WHEN json_extract(stop,'$.transactionId')=local THEN json_extract(stop,'$.timestamp') END "
            "END FROM transactions WHERE local=?",
            (local,),
        ).fetchone()
        if row is not None:
            lifecycle, mapping = "closed" if row[0] else "open", "recorded" if row[1] else "unmapped"
            if isinstance(row[2], str) and len(row[2]) <= 64:
                try:
                    stop_at = charger_timestamp(row[2]).isoformat()
                except DeniedError:
                    pass
            if {"outbox", "delivery_replies"} <= tables:
                _, _, delivery = lifecycle_fields(db, local)
    return {
        "available": True,
        "request": request,
        "lifecycle": lifecycle,
        "charger_reported_stop_at": stop_at,
        "cloud_mapping": mapping,
        "pending_delivery": delivery,
    }, wire


def stop_incident_report(
    journal: Path, event_path: Path, *, vendor: bool = False, request_reference: str | None = None
) -> dict[str, Any]:
    """Read a retained Stop and show independent lifecycle and service evidence."""
    if request_reference is not None and (vendor or identifier(request_reference) is None):
        raise ValueError("An exact reference must identify a local Stop")
    processes = [
        {**row, "run": reference(row["run"])} for row in observations(event_path) if row["event"] == "process_started"
    ]
    try:
        with read_journal(journal) as db:
            saved, wire = journal_fields(db, vendor=vendor, request_reference=request_reference)
    except OSError, ValueError, sqlite3.Error:
        saved = {
            "available": False,
            "request": None,
            "lifecycle": UNKNOWN,
            "charger_reported_stop_at": None,
            "cloud_mapping": UNKNOWN,
            "pending_delivery": [],
        }
        wire = wire_fields()
    return {
        "schema_version": 1,
        "kind": "vendor-stop" if vendor else "stop-session",
        "journal": saved,
        "wire": wire,
        "observations": processes[-MAX_REPORT_EVENTS:],
        "sampled_status": sampled_status(journal.with_suffix(".status.json")),
    }
