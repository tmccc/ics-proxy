# Modified for the standalone iCS proxy repository.
"""Receipt truthfulness and transactional Pause acceptance under a real SQLite journal."""

from __future__ import annotations

import json
import sqlite3
import ssl
import uuid
from contextlib import closing
from pathlib import Path

import pytest

from ics_proxy_core.engine.managed import ManagedController
from ics_proxy_core.engine.managed_view import charger_history, session_history, summarize_charger_history
from ics_proxy_core.engine.store import Journal, RecoveryRequiredError
from tests.managed_rig import healthy, introduced, managed_bench, managed_session
from tests.prototype_rig import START
from tests.tls_support import tls as tls


# Intent: a completed session is not cloud acknowledged while its actual Stop remains in the delivery queue.
# Why: a Start mapping alone says nothing about delivery of the final meter and session closure.
def test_completed_history_requires_stop_delivery_not_just_start_mapping(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "state.db", "SYNTHETIC")
    start = json.dumps({"timestamp": "2026-01-01T00:00:00Z", "meterStart": 1000})
    stop = json.dumps({"timestamp": "2026-01-01T01:00:00Z", "meterStop": 2000})
    try:
        journal.db.execute(
            "INSERT INTO transactions(local,connector,start,stop,vendor) VALUES(1,1,?,?,99)", (start, stop)
        )
        journal.db.execute(
            "INSERT INTO outbox(token,action,payload,local) VALUES('stop','StopTransaction',?,1)", (stop,)
        )
        assert session_history(journal)[0]["cloud_acknowledged"] is False
        journal.db.execute("DELETE FROM outbox WHERE token='stop'")
        assert session_history(journal)[0]["cloud_acknowledged"] is True
        assert session_history(journal)[0]["energy_kwh"] == 1
    finally:
        journal.close()


# Intent: the journal adds owner tables only with the first successful owner-authorized Start.
# Why: a failed commit must leave the legacy schema and transaction allocation intact for recovery.
def test_owner_session_schema_follows_committed_start(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    with closing(Journal(path, "SYNTHETIC")) as journal:
        original_tables = {row[0] for row in journal.db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
        assert "owner_sessions" not in original_tables and "owner_meter_values" not in original_tables
        journal.db.execute("CREATE TEMP TABLE parent(id INTEGER PRIMARY KEY)")
        journal.db.execute("CREATE TEMP TABLE child(id INTEGER REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED)")

        # The deferred reference fails COMMIT after the optional schema and session can be read in the transaction.
        def fail_commit(local: int) -> None:
            assert journal.is_owner_session(local)
            journal.db.execute("INSERT INTO child VALUES(1)")

        with pytest.raises(sqlite3.IntegrityError):
            journal.start("owner-failed", START, fail_commit, owner_authorized=True)
        assert journal.active() is None and not journal.has_unsettled_sessions()
        assert not journal.is_owner_session(1001) and journal.unmapped_cloud_sessions().fetchone() is None
        assert {
            row[0] for row in journal.db.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        } == original_tables
        assert journal.receipt("owner-failed", "StartTransaction", START) is None

        reply = journal.start("owner-completed", START, owner_authorized=True)
        local = reply["transactionId"]
        journal.stop("owner-stop", {"transactionId": local, "timestamp": START["timestamp"], "meterStop": 2000})
        assert journal.next_delivery() is None and not journal.has_unsettled_sessions()

    with closing(Journal(path, "SYNTHETIC")) as journal:
        assert journal.is_owner_session(local) and journal.vendor_id(local) is None
        journal.db.execute("DROP TABLE owner_meter_values")
    with pytest.raises(RecoveryRequiredError, match="owner session schema"):
        Journal(path, "SYNTHETIC")


# Intent: the native history view preserves measured values, strips private fields and rejects invalid rows.
# Why: replacing the cloud cache must not leak tags or display guessed/stale session values.
def test_native_history_is_redacted_bounded_and_validated() -> None:
    record = {
        "startTime": 1767225600,
        "stopTime": 1767229200,
        "meterStart": 1000,
        "meterStop": 2200,
        "idTagStart": "SYNTHETIC_PRIVATE_TAG",
        "transactionId": 99,
    }
    live = record | {"startTime": 1767232800, "stopTime": 0, "meterStop": 0}
    invalid = record | {"stopTime": 1767225500}
    result = charger_history((record, live, invalid))
    assert not summarize_charger_history((record, live, invalid)).complete
    assert summarize_charger_history((record, live)).complete
    assert len(result) == 2 and result[0]["stopped_at"] is None and result[0]["energy_kwh"] is None
    assert result[1]["energy_kwh"] == 1.2
    assert all(set(r) == {"started_at", "stopped_at", "energy_kwh"} for r in result)
    assert "SYNTHETIC_PRIVATE_TAG" not in json.dumps(result)
    for bad in (record | {"startTime": True}, record | {"meterStart": -1}, record | {"meterStop": "2200"}):
        assert charger_history((bad,)) == []
    assert len(charger_history(tuple(record | {"startTime": 1767225600 + i} for i in range(30)))) == 20


# Intent: a failed hold-claim commit rolls back both the Pause intent and command acceptance.
# Why: a recorded user pause must not coexist with a silently unchanged shared positive authority after a DB failure.
@pytest.mark.parametrize("failure", ["statement", "commit"])
async def test_pause_and_hold_claim_are_atomic(
    tmp_path: Path,
    tls: tuple[ssl.SSLContext, ssl.SSLContext, Path],
    unused_tcp_port: int,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    async with managed_bench(tmp_path, tls, unused_tcp_port) as rig:
        session = managed_session(rig)
        await session.start()
        controller = ManagedController(session)
        rig.route_to_trial()
        async with rig.charger(session) as charger:
            await introduced(rig, session, charger, "storage-boot")
            await healthy(controller)
            j = session.journal
            before = j.get("control"), controller.requests.intent(), list(rig.bench.device_calls)
            if failure == "statement":
                effect = "SELECT RAISE(ABORT,'hold claim unavailable');"
            else:
                j.db.execute("CREATE TABLE review_claim_parent(id INTEGER PRIMARY KEY)")
                j.db.execute(
                    "CREATE TABLE review_claim_child("
                    "id REFERENCES review_claim_parent(id) DEFERRABLE INITIALLY DEFERRED)"
                )
                effect = "INSERT INTO review_claim_child VALUES(123);"
            j.db.execute(
                "CREATE TEMP TRIGGER review_hold_claim BEFORE INSERT ON meta "
                "WHEN NEW.key='control' AND json_extract(NEW.value,'$.owner')='local' "
                "AND json_extract(NEW.value,'$.inhibit')=1 BEGIN " + effect + " END"
            )
            request = uuid.uuid4().hex
            with pytest.raises(sqlite3.IntegrityError):
                controller.command(request, "pause", 0)
            assert (j.get("control"), controller.requests.intent(), rig.bench.device_calls) == before
            assert controller.requests.receipt(request)["state"] == "not_found"
            assert j.get("managed_owner_pause") is None and controller.pause_task is None
            j.db.execute("DROP TRIGGER review_hold_claim")
            controller.command(request, "pause", 0)
            assert (await healthy(controller))["local_hold_verified"]
