# Modified for the standalone iCS proxy repository.
"""A genuine terminal Stop retains raw clock discontinuity without weakening meter/identity/duplicate checks."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

import pytest

from ics_proxy_core.engine.store import ConflictError, Journal
from tests.charge_rig import positive_flow, ready
from tests.managed_rig import healthy
from tests.prototype_rig import START
from tests.runtime_rig import until
from tests.tls_support import tls as tls


# Construct the exact reported terminal body; only its device timestamp moves behind the original raw Start.
def backwards_stop(local):
    instant = datetime.fromisoformat(START["timestamp"].replace("Z", "+00:00")) - timedelta(seconds=1)
    return {"transactionId": local, "meterStop": START["meterStart"] + 1, "timestamp": instant.isoformat()}


# Intent: authenticated correct-ID Stop terminalizes with increasing meter and exact raw reversed timestamps.
# Why: the physical session's closure must not become an invented continuing session because the device clock changed.
async def test_real_backward_clock_stop_closes_and_retains_exact_raw_history(
    tmp_path, tls, unused_tcp_port, monkeypatch
):
    async with ready(tmp_path, tls, unused_tcp_port, monkeypatch) as test:
        local = await positive_flow(test, source="daily")
        assert (await healthy(test.controller))["current_a"] == 6
        body = backwards_stop(local)
        answer = await test.rig.bench.call(test.charger, "clock-corrected-stop", "StopTransaction", body)
        assert answer[0] == 3
        test.rig.peer.plan_session = None
        test.rig.peer.permission_enabled = False
        await until(lambda: test.session.journal.active() is None and test.session.journal.next_delivery() is None)
        journal = test.session.journal
        assert (
            json.loads(journal.db.execute("SELECT stop FROM transactions WHERE local=?", (local,)).fetchone()[0])
            == body
        )
        anomaly = dict(
            journal.db.execute("SELECT * FROM transaction_clock_discontinuities WHERE local=?", (local,)).fetchone()
        )
        assert anomaly["start_timestamp"] == START["timestamp"] and anomaly["stop_timestamp"] == body["timestamp"]
        assert any(
            frame[0] == 2 and frame[2] == "StopTransaction" and frame[3]["timestamp"] == body["timestamp"]
            for frame in test.rig.bench.calls
        )
        replay = await test.rig.bench.call(test.charger, "clock-corrected-replay", "StopTransaction", body)
        assert replay[0] == 3
        assert (
            dict(
                journal.db.execute("SELECT * FROM transaction_clock_discontinuities WHERE local=?", (local,)).fetchone()
            )
            == anomaly
        )
        for token, changed in (
            ("different-stop", body | {"meterStop": body["meterStop"] + 1}),
            ("lower-meter", body | {"meterStop": START["meterStart"] - 1}),
            ("unknown-stop", body | {"transactionId": local + 999}),
        ):
            assert (await test.rig.bench.call(test.charger, token, "StopTransaction", changed))[0] == 4
        assert (
            json.loads(journal.db.execute("SELECT stop FROM transactions WHERE local=?", (local,)).fetchone()[0])
            == body
        )
        assert journal.db.execute("SELECT COUNT(*) FROM transaction_clock_discontinuities").fetchone()[0] == 1
        assert (await healthy(test.controller))["owner_control_enabled"]


# Intent: queue/storage failure rolls back the terminal Stop and its discontinuity annotation together.
# Why: a diagnostic row must not claim that a Stop was durably accepted if its actual delivery record was not committed.
def test_stop_clock_annotation_rolls_back_with_delivery_failure(tmp_path):
    journal = Journal(tmp_path / "clock.db", "OFFLINE001")
    try:
        local = journal.start("start", START)["transactionId"]
        journal.db.execute(
            "CREATE TRIGGER no_stop BEFORE INSERT ON outbox WHEN NEW.action='StopTransaction' "
            "BEGIN SELECT RAISE(ABORT,'synthetic disk refusal'); END"
        )
        with pytest.raises(sqlite3.IntegrityError, match="synthetic disk refusal"):
            journal.stop("failed-stop", backwards_stop(local))
        assert journal.active() == local
        assert journal.db.execute("SELECT stop FROM transactions WHERE local=?", (local,)).fetchone()[0] is None
        assert not journal.db.execute("SELECT 1 FROM transaction_clock_discontinuities").fetchone()
        assert journal.db.execute("SELECT action FROM outbox").fetchone()[0] == "StartTransaction"
    finally:
        journal.close()


# Intent: raw-clock tolerance never permits a lower meter or guessed transaction identity at the durable boundary.
# Why: a clock discontinuity is separate from contradictory energy or lifecycle correlation.
@pytest.mark.parametrize("bad", ["meter", "identity"])
def test_stop_clock_relaxation_preserves_durable_identity_and_meter_guards(tmp_path, bad):
    journal = Journal(tmp_path / "clock.db", "OFFLINE001")
    try:
        local = journal.start("start", START)["transactionId"]
        body = backwards_stop(local)
        body.update({"meterStop": START["meterStart"] - 1} if bad == "meter" else {"transactionId": local + 1})
        with pytest.raises(ConflictError):
            journal.stop("invalid", body)
        assert journal.active() == local
        assert not journal.db.execute("SELECT 1 FROM transaction_clock_discontinuities").fetchone()
    finally:
        journal.close()


# Give the native proof fractional precision beyond the charger's ordinary Stop report.
def recoverable_stop(local):
    instant = datetime.fromisoformat(START["timestamp"]) + timedelta(minutes=1, milliseconds=123)
    corrected = {"transactionId": local, "meterStop": START["meterStart"] + 1, "timestamp": instant.isoformat()}
    malformed = corrected | {"meterStop": START["meterStart"] - 1}
    native = {
        "transactionId": local,
        "meterStop": corrected["meterStop"],
        "stopTime": int(instant.timestamp()),
        "stopMs": 123,
    }
    return malformed, corrected, native


# Intent: roll back reconstructed closure when its vendor Stop cannot be enqueued, then retry once.
# Why: a closed local row without the matching vendor delivery would strand the two histories.
def test_malformed_stop_recovery_rolls_back_with_delivery_failure(tmp_path):
    journal = Journal(tmp_path / "recovery.db", "OFFLINE001")
    try:
        local = journal.start("start", START)["transactionId"]
        malformed, corrected, native = recoverable_stop(local)
        journal.record_malformed_stop(local, malformed, "meter_regression")
        candidate = journal.malformed_stop(local)
        journal.db.execute(
            "CREATE TRIGGER no_stop BEFORE INSERT ON outbox WHEN NEW.action='StopTransaction' "
            "BEGIN SELECT RAISE(ABORT,'synthetic disk refusal'); END"
        )
        with pytest.raises(sqlite3.IntegrityError, match="synthetic disk refusal"):
            journal.stop("native:recovery", corrected, recovered_native=native)
        assert journal.active() == local
        assert journal.malformed_stop(local) == candidate
        assert journal.receipt("native:recovery", "StopTransaction", corrected) is None
        assert journal.acknowledge_recovered_stop("bad-stop", malformed) is None
        assert not journal.pending_recovered_cloud_stop()

        journal.db.execute("DROP TRIGGER no_stop")
        assert journal.stop("native:recovery", corrected, recovered_native=native) == {}
        assert journal.active() is None and journal.pending_recovered_cloud_stop()
        assert journal.acknowledge_recovered_stop("bad-stop", malformed) == {}
        assert (
            journal.acknowledge_recovered_stop(
                "late-corrected", corrected | {"timestamp": "2026-01-01T01:01:00+01:00", "reason": "Local"}
            )
            == {}
        )
        assert (
            journal.acknowledge_recovered_stop(
                "late-conflicting", corrected | {"timestamp": "2026-01-01T00:01:00.124Z"}
            )
            is None
        )
        start = journal.next_delivery()
        assert start is not None and start.action == "StartTransaction"
        journal.acknowledge(start, {"transactionId": 42, "idTagInfo": {"status": "Accepted"}})
        stop = journal.next_delivery()
        assert stop is not None and stop.action == "StopTransaction" and stop.payload == corrected
        journal.acknowledge(stop, {})
        assert journal.next_delivery() is None and not journal.pending_recovered_cloud_stop()
    finally:
        journal.close()


# Intent: let a genuine corrected report close the session before a pending native recovery does.
# Why: independent recovery must not overwrite an accepted charger report or duplicate vendor delivery.
def test_genuine_stop_wins_before_malformed_report_recovery(tmp_path):
    journal = Journal(tmp_path / "recovery.db", "OFFLINE001")
    try:
        local = journal.start("start", START)["transactionId"]
        malformed, corrected, native = recoverable_stop(local)
        journal.record_malformed_stop(local, malformed, "meter_regression")
        actual = corrected | {"reason": "Local"}
        assert journal.stop("corrected-stop", actual) == {}
        assert journal.malformed_stop(local) is None and not journal.pending_recovered_cloud_stop()
        with pytest.raises(ConflictError, match="previously accepted stop"):
            journal.stop("native:recovery", corrected, recovered_native=native)
        stop_reports = journal.db.execute("SELECT payload FROM outbox WHERE action='StopTransaction'").fetchall()
        assert [json.loads(row[0]) for row in stop_reports] == [actual]
    finally:
        journal.close()
