"""The read-only Stop report preserves independent and exactly bound evidence."""

import json
import sqlite3
import uuid
from contextlib import closing
from dataclasses import asdict, replace

import pytest

from ics_proxy_core.engine.command_outcomes import ForwardedReply, OwnerWire
from ics_proxy_core.engine.managed_intent import ManagedRequests, insert_request
from ics_proxy_core.engine.stop_settlement import STOP_COMPLETION_FIELDS, STOP_SESSION
from ics_proxy_core.engine.store import Journal
from ics_proxy_core.engine.trial_session import save_private
from tools.managed_incident import reference
from tools.managed_stop_incident import STOP_ACTION, stop_incident_report

START = {"connectorId": 1, "meterStart": 100, "timestamp": "2026-11-23T17:30:00Z", "idTag": "private-tag"}
ACCEPTED_AT = 1795455001.0
BOOT = "private-charger-boot"
VENDOR_REQUEST = "vendor-original-call-id"


# Create the receipt independently of any wire or lifecycle evidence.
def write_request(journal, local, *, state="running", **binding):
    token = uuid.uuid4().hex
    insert_request(
        journal,
        token,
        STOP_SESSION,
        2,
        {
            "target": "private-stop-target",
            "accepted_at": ACCEPTED_AT,
            **({"classification": "process_replaced"} if state == "uncertain" else {}),
            "_session_stop": {"local": local, **binding},
        },
        state=state,
    )
    return token


# Bind a vendor mutation to an exact charger call under the normal journal schema.
def vendor_attempt(journal, local, wire, *, result="dispatched"):
    token = journal.mutation_intent("vendor", STOP_ACTION, {"transactionId": 4001})
    journal.bind_vendor_command(
        token, request=VENDOR_REQUEST, vendor_epoch=1, boot=BOOT, wire=wire, local=local, profile=None
    )
    if result != "prepared":
        journal.record_vendor_result(token, result)
    if result in ("Accepted", "Rejected"):
        journal.mutation_finished(token, True)
    return token


# Intent: observe an open session with a missing reply, then record closure for the same session.
# Why: the closure and client receipt should remain separate from the missing reply for both command origins.
@pytest.mark.parametrize("vendor", [False, True], ids=["managed", "vendor-app"])
def test_stop_report_distinguishes_lost_reply_from_later_closed_session(tmp_path, vendor):
    path, events = tmp_path / "journal.db", tmp_path / "events.jsonl"
    old_run, next_run, call = (uuid.uuid4().hex for _ in range(3))
    events.write_text(
        "".join(
            json.dumps(
                {"event": "process_started", "run": run, "sequence": 1, "at": ACCEPTED_AT + offset, "secret": BOOT}
            )
            + "\n"
            for run, offset in ((old_run, -5), (next_run, 5))
        )
    )
    events.chmod(0o600)
    save_private(path.with_suffix(".status.json"), {"checked_at": ACCEPTED_AT + 10, "state": "healthy", "tag": BOOT})
    with closing(Journal(path, "private-charger-identity")) as journal:
        ManagedRequests(journal)
        local = journal.start("private-start-token", START)["transactionId"]
        if vendor:
            token = vendor_attempt(journal, local, call)
            journal.mutation_finished(token, False)
        else:
            token = write_request(journal, local, state="uncertain", dispatched=True)
            journal.outcomes.record_owner_wire(OwnerWire(call, token, token, BOOT, STOP_ACTION, local, None))
        snapshot = list(journal.db.iterdump())
        lost = stop_incident_report(path, events, vendor=vendor)
        assert list(journal.db.iterdump()) == snapshot
        assert lost["wire"] == {
            "dispatch": "possible_dispatch",
            "reply": "missing",
            "reply_source": None,
            "joined_attempts": [],
        }
        assert lost["journal"]["request"]["request"] == reference(token)
        assert lost["journal"]["request"]["state"] == "uncertain"
        assert lost["journal"]["lifecycle"] == "open"
        assert lost["journal"]["charger_reported_stop_at"] is None
        assert [row["run"] for row in lost["observations"]] == [reference(old_run), reference(next_run)]
        assert lost["sampled_status"] == {"checked_at": ACCEPTED_AT + 10, "state": "healthy"}
        if vendor:
            assert "accepted_at" not in lost["journal"]["request"]
        else:
            assert lost["journal"]["request"]["accepted_at"] == ACCEPTED_AT
            assert lost["journal"]["request"]["classification"] == "process_replaced"

        journal.stop(
            "private-stop-token",
            {"transactionId": local, "meterStop": 102, "timestamp": "2026-11-23T18:32:00+01:00"},
        )
        closed = stop_incident_report(path, events, vendor=vendor)
        assert closed["journal"]["request"] == lost["journal"]["request"]
        assert closed["wire"] == lost["wire"]
        assert closed["journal"]["lifecycle"] == "closed"
        assert closed["journal"]["charger_reported_stop_at"] == "2026-11-23T17:32:00+00:00"
        assert closed["journal"]["pending_delivery"][-1] == {
            "event": "StopTransaction",
            "state": "queued",
            "reply_saved": False,
        }
        if not vendor:
            journal.db.execute(f"UPDATE managed_requests SET {STOP_COMPLETION_FIELDS} WHERE id=?", (token,))
            completed = stop_incident_report(path, events)
            assert completed["journal"]["request"]["state"] == "completed"
            assert completed["journal"]["request"]["session_stopped"] is True
            assert completed["wire"]["reply"] == "missing"
        report_text = json.dumps(closed)
        for secret in (BOOT, VENDOR_REQUEST, old_run, next_run, call, token, "private-", "meterStop", "transactionId"):
            assert secret not in report_text


# Intent: retain a real charger reply even when the first storage path fails.
# Why: an obsolete or ambiguous checkpoint cannot fill a gap in the original command's record.
@pytest.mark.parametrize("vendor", [False, True], ids=["managed", "vendor-app"])
@pytest.mark.parametrize("status", ["Accepted", "Rejected"])
def test_stop_report_uses_only_exact_retained_checkpoints(tmp_path, vendor, status):
    path, events = tmp_path / "journal.db", tmp_path / "events.jsonl"
    wire = uuid.uuid4().hex
    prefix = "retained_stop" if status == "Accepted" else "retained_transaction_reply"
    with closing(Journal(path, "test-charger")) as journal:
        ManagedRequests(journal)
        local = journal.start("start-token", START)["transactionId"]
        if vendor:
            token = vendor_attempt(journal, local, wire)
            key = f"{prefix}_vendor:{wire}"
            correct = ForwardedReply(token, STOP_ACTION, wire, status)
            wrong = replace(correct, mutation=uuid.uuid4().hex)
            refusal = "BEFORE UPDATE OF wire_result ON vendor_command_scope"
        else:
            token = write_request(journal, local, dispatched=True)
            key = f"{prefix}_owner:{wire}"
            correct = OwnerWire(wire, token, token, BOOT, STOP_ACTION, local, None, status)
            if status == "Accepted":
                wrong = replace(correct, request=uuid.uuid4().hex)
            else:
                wrong = replace(correct, boot="old-boot")
                journal.outcomes.record_owner_wire(replace(correct, reply=None))
            refusal = "BEFORE INSERT ON owner_command_wires"
        journal.put(key, asdict(wrong))
        assert stop_incident_report(path, events, vendor=vendor)["wire"]["reply"] == "missing"
        binding_key = "mutation" if vendor else "request"
        duplicate = json.dumps(asdict(correct))[:-1] + f',"{binding_key}":"{uuid.uuid4().hex}"}}'
        journal.db.execute("UPDATE meta SET value=? WHERE key=?", (duplicate, key))
        assert stop_incident_report(path, events, vendor=vendor)["wire"]["reply"] == "missing"
        journal.db.execute(f"CREATE TEMP TRIGGER refuse_primary {refusal} BEGIN SELECT RAISE(ABORT, 'test'); END")
        frame = [3, wire, {"status": status}]
        if vendor:
            captured = journal.outcomes.capture(token, STOP_ACTION, wire, frame)
            assert captured is not None and not journal.outcomes.publish(captured)
        else:
            journal.outcomes.record_owner_reply(replace(correct, reply=None), frame)
        saved = stop_incident_report(path, events, vendor=vendor)
        assert saved["wire"]["reply"] == status.lower()
        assert saved["wire"]["reply_source"] == "retained_checkpoint"
        assert saved["journal"]["lifecycle"] == "open"


# Intent: show the exact predecessors of a joined Stop, then select newer requests that have no dispatch record.
# Why: receipt overlap or a newer request should never take credit for a different wire reply.
def test_stop_report_keeps_joined_replies_with_their_original_attempt(tmp_path):
    path, events = tmp_path / "journal.db", tmp_path / "events.jsonl"
    with closing(Journal(path, "test-charger")) as journal:
        ManagedRequests(journal)
        local = journal.start("start-token", START)["transactionId"]
        first = write_request(journal, local, dispatched=True)
        wire = uuid.uuid4().hex
        reply = OwnerWire(wire, first, first, BOOT, STOP_ACTION, local, None, "Rejected")
        journal.outcomes.record_owner_wire(reply)
        journal.outcomes.record_owner_wire(replace(reply, request=uuid.uuid4().hex, wire=uuid.uuid4().hex))
        vendor = vendor_attempt(journal, local, uuid.uuid4().hex, result="Accepted")
        wrong_session = vendor_attempt(journal, local + 1, uuid.uuid4().hex, result="Rejected")
        joined = write_request(
            journal,
            local,
            dispatched=False,
            joined=True,
            local_attempts=[first],
            vendor_attempts=[vendor, wrong_session],
        )
        report = stop_incident_report(path, events)
        assert report["journal"]["request"]["request"] == reference(joined)
        assert report["wire"]["dispatch"] == "joined"
        assert report["wire"]["reply"] == "unknown"
        attempts = report["wire"]["joined_attempts"]
        assert [(attempt["origin"], attempt["request"], attempt["reply"]) for attempt in attempts] == [
            ("local", reference(first), "rejected"),
            ("vendor", reference(vendor), "accepted"),
            ("vendor", reference(wrong_session), "unknown"),
        ]
        latest = write_request(journal, local, state="accepted", dispatched=False)
        report = stop_incident_report(path, events)
        assert report["journal"]["request"]["request"] == reference(latest)
        assert report["wire"]["dispatch"] == "not_sent"
        assert report["wire"]["reply"] == "unknown"
        assert not report["wire"]["joined_attempts"]
        prepared = vendor_attempt(journal, local, uuid.uuid4().hex, result="prepared")
        report = stop_incident_report(path, events, vendor=True)
        assert report["journal"]["request"]["request"] == reference(prepared)
        assert report["wire"]["dispatch"] == "not_sent"


# Intent: read the remaining facts in journals with no supplementary outcome tables and refuse an unsafe file.
# Why: reports are also needed after a rollback or when the installation is no longer present.
def test_stop_report_handles_older_and_unavailable_journals(tmp_path):
    path, events = tmp_path / "journal.db", tmp_path / "events.jsonl"
    assert not stop_incident_report(path, events)["journal"]["available"]
    assert not path.exists()
    with closing(sqlite3.connect(path)) as db:
        db.executescript(
            "CREATE TABLE managed_requests(id TEXT PRIMARY KEY,action TEXT,revision INTEGER,state TEXT,result TEXT);"
            "CREATE TABLE transactions(local INTEGER PRIMARY KEY,stop TEXT,vendor INTEGER);"
            "CREATE TABLE mutations(token TEXT PRIMARY KEY,origin TEXT,action TEXT,state TEXT);"
        )
        db.execute(
            "INSERT INTO managed_requests VALUES(?,?,?,?,?)",
            (
                uuid.uuid4().hex,
                STOP_SESSION,
                0,
                "uncertain",
                json.dumps({"_session_stop": {"local": 1000, "dispatched": True}}),
            ),
        )
        db.execute("INSERT INTO transactions VALUES(1000,'{}',NULL)")
        db.execute("INSERT INTO mutations VALUES(?,'vendor',?,'uncertain')", (uuid.uuid4().hex, STOP_ACTION))
        db.commit()
    path.chmod(0o600)
    local = stop_incident_report(path, events)
    assert local["journal"]["available"]
    assert local["journal"]["lifecycle"] == "closed"
    assert local["journal"]["charger_reported_stop_at"] is None
    assert local["wire"]["reply"] == "missing"
    vendor = stop_incident_report(path, events, vendor=True)
    assert vendor["journal"]["available"]
    assert vendor["journal"]["request"]["state"] == "uncertain"
    assert vendor["journal"]["lifecycle"] == "unbound"
    assert vendor["wire"]["dispatch"] == vendor["wire"]["reply"] == "unknown"
    path.chmod(0o644)
    assert not stop_incident_report(path, events, vendor=True)["journal"]["available"]
