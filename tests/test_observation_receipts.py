# Modified for the standalone iCS proxy repository.
"""Real SQLite failures and repeated TLS calls preserve complete observation acceptance boundaries."""

import asyncio
import sqlite3
import time
import uuid
from datetime import UTC, datetime

import pytest

from ics_proxy_core.engine import managed_start_delivery
from ics_proxy_core.engine.managed_view import session_history
from ics_proxy_core.frames import build_call
from tests.observation_helpers import ObservationTLS, observation_payload, observation_suffix
from tests.prototype_rig import BOOT, START
from tests.tls_support import tls as tls


# Intent: receipt, publication and local status effects all roll back when BEGIN, statement or COMMIT fails.
# Why: the same real CALL must succeed after repair without a false successful receipt or duplicate queue row.
@pytest.mark.parametrize(
    "kind", ["meter-transaction", "meter-station", "status-timed", "status-untimed", "firmware", "diagnostics"]
)
@pytest.mark.parametrize("failure", ["begin", "statement", "commit"])
async def test_observation_storage_failure_then_retry(tmp_path, tls, kind, failure):
    async with ObservationTLS(tmp_path / "state.db", tls) as peer:
        local = (await peer.call("start", "StartTransaction", START))[2]["transactionId"]
        action, payload = observation_payload(kind, local)
        before = [tuple(r) for r in peer.j.db.execute("SELECT * FROM outbox ORDER BY seq")]
        previous_status = peer.j.get("last_status")
        if failure == "begin":
            # Deny an actual SQLite BEGIN; reads and the protocol's failure reply remain available.
            def deny_begin(operation, arg1, arg2, database, trigger):
                return (
                    sqlite3.SQLITE_DENY
                    if operation == sqlite3.SQLITE_TRANSACTION and arg1 == "BEGIN"
                    else sqlite3.SQLITE_OK
                )

            peer.j.db.set_authorizer(deny_begin)
        elif failure == "statement":
            peer.j.db.execute(
                "CREATE TRIGGER reject_observation BEFORE INSERT ON observation_receipts "
                "BEGIN SELECT RAISE(ABORT,'synthetic observation failure'); END"
            )
        else:
            peer.j.db.executescript("""
                CREATE TABLE observation_parent(id INTEGER PRIMARY KEY);
                CREATE TABLE observation_child(
                    id INTEGER REFERENCES observation_parent(id) DEFERRABLE INITIALLY DEFERRED);
                CREATE TRIGGER reject_observation AFTER INSERT ON observation_receipts
                BEGIN INSERT INTO observation_child VALUES(12345); END;
            """)
        try:
            assert (await peer.call("failed-observation", action, payload))[:3] == [
                4,
                "failed-observation",
                "InternalError",
            ]
        finally:
            peer.j.db.set_authorizer(None)
        assert [tuple(r) for r in peer.j.db.execute("SELECT * FROM outbox ORDER BY seq")] == before
        assert peer.j.db.execute("SELECT COUNT(*) FROM observation_receipts").fetchone()[0] == 0
        assert peer.j.db.execute("SELECT COUNT(*) FROM observation_calls").fetchone()[0] == 0
        assert peer.j.get("last_status") == previous_status and not peer.j.db.in_transaction
        if failure != "begin":
            peer.j.db.execute("DROP TRIGGER reject_observation")
        assert (await peer.call("failed-observation", action, payload))[0] == 3
        assert (await peer.call("failed-observation", action, payload))[0] == 3
        await peer.flush()
        assert len([f for f in peer.cloud_calls if f[2] == action]) == 1
        await observation_suffix(peer, local)


# Intent: repeated replacement Boots cannot pin unlimited aliases for one pending timed publication.
# Why: durable retry protection must preserve the pending row while retaining bounded receipt storage.
async def test_pending_timed_retry_has_one_canonical_receipt(tmp_path, tls):
    async with ObservationTLS(tmp_path / "state.db", tls, capacity=2) as peer:
        local = (await peer.call("start", "StartTransaction", START))[2]["transactionId"]
        action, payload = observation_payload("meter-transaction", local)
        payload["meterValue"][0]["timestamp"] = datetime.now(UTC).isoformat()
        assert (await peer.call("meter", action, payload))[0] == 3
        for _ in range(12):
            await peer.replace(boot=True)
            assert (await peer.call("meter", action, payload))[0] == 3
            assert peer.j.db.execute("SELECT COUNT(*) FROM observation_receipts").fetchone()[0] == 1
            assert peer.j.db.execute("SELECT COUNT(*) FROM observation_calls").fetchone()[0] == 1
        await peer.flush()
        assert len([f for f in peer.cloud_calls if f[2] == "MeterValues"]) == 1
        await observation_suffix(peer, local)


# Intent: accept MeterValues beyond the owner sample cap and retain retries even when their raw payload is evicted.
# Why: long sessions and delayed historical reports must not stop accepting telemetry or interfere with lifecycle data.
async def test_owner_meter_window_rolls_over_without_losing_receipts(tmp_path, tls):
    async with ObservationTLS(tmp_path / "state.db", tls, capacity=2) as peer:
        first = peer.j.start("owner-start", START, owner_authorized=True)["transactionId"]
        action, meter = observation_payload("meter-transaction", first)
        for call_id in ("first-sample", "second-sample"):
            assert (await peer.call(call_id, action, meter))[0] == 3

        peer.j.db.execute(
            "CREATE TEMP TRIGGER refuse_owner_trim BEFORE DELETE ON owner_meter_values "
            "BEGIN SELECT RAISE(ABORT,'synthetic owner-meter trim failure'); END"
        )
        reply = await peer.call("third-sample", action, meter)
        assert reply[:3] == [4, "third-sample", "InternalError"]
        assert [row[0] for row in peer.j.db.execute("SELECT sequence FROM owner_meter_values ORDER BY sequence")] == [
            1,
            2,
        ]
        assert peer.j.db.execute("SELECT 1 FROM observation_receipts WHERE token='third-sample'").fetchone() is None
        peer.j.db.execute("DROP TRIGGER refuse_owner_trim")
        assert (await peer.call("third-sample", action, meter))[0] == 3
        assert [row[0] for row in peer.j.db.execute("SELECT sequence FROM owner_meter_values ORDER BY sequence")] == [
            2,
            3,
        ]

        first_stop = {"transactionId": first, "meterStop": 1300, "timestamp": "2026-01-01T00:02:00Z"}
        assert (await peer.call("owner-stop", "StopTransaction", first_stop))[0] == 3
        second_start = START | {"timestamp": "2026-01-01T00:03:00Z"}
        second = peer.j.start("owner-next-start", second_start, owner_authorized=True)["transactionId"]
        _, second_meter = observation_payload("meter-transaction", second)
        second_meter["meterValue"][0]["timestamp"] = "2026-01-01T00:04:00Z"
        for call_id in ("next-sample", "last-sample"):
            assert (await peer.call(call_id, action, second_meter))[0] == 3
        rows = [
            tuple(row)
            for row in peer.j.db.execute("SELECT local,sequence,token FROM owner_meter_values ORDER BY sequence")
        ]
        assert [(local, sequence) for local, sequence, _ in rows] == [(second, 1), (second, 2)]

        # A late old-session sample is acknowledged with its own token even when its raw row falls outside the window.
        assert (await peer.call("late-sample", action, meter))[0] == 3
        receipt = peer.j.db.execute("SELECT publication FROM observation_receipts WHERE token='late-sample'").fetchone()
        assert receipt["publication"] not in {token for _, _, token in rows}
        await peer.replace(restart=True, boot=True)
        assert (await peer.call("late-sample", action, meter))[0] == 3
        conflicting = meter | {
            "meterValue": [{"timestamp": "2026-01-01T00:01:00Z", "sampledValue": [{"value": "1301"}]}]
        }
        assert (await peer.call("late-sample", action, conflicting))[0] == 4
        assert [
            tuple(row)
            for row in peer.j.db.execute("SELECT local,sequence,token FROM owner_meter_values ORDER BY sequence")
        ] == rows
        assert (
            peer.j.db.execute("SELECT publication FROM observation_receipts WHERE token='late-sample'").fetchone()[0]
            == receipt[0]
        )

        assert session_history(peer.j)[1]["energy_kwh"] == 0.3 and peer.j.active() == second
        assert peer.j.next_delivery() is None


# Intent: an unsuccessful Boot write cannot advance the untimed observation generation.
# Why: returning failure after a partial generation change would duplicate an already accepted report on retry.
async def test_boot_storage_failure_preserves_observation_generation(tmp_path, tls):
    async with ObservationTLS(tmp_path / "state.db", tls) as peer:
        action, payload = observation_payload("status-untimed", None)
        assert (await peer.call("status", action, payload))[0] == 3
        generation = peer.j.get("observation_generation")
        await peer.replace()
        peer.j.db.execute(
            "CREATE TRIGGER reject_boot BEFORE INSERT ON meta WHEN NEW.key='boot' "
            "BEGIN SELECT RAISE(ABORT,'synthetic Boot write failure'); END"
        )
        assert (await peer.call("boot-failed", "BootNotification", BOOT))[0] == 4
        assert peer.j.get("observation_generation") == generation
        assert (await peer.call("status", action, payload))[0] == 3
        peer.j.db.execute("DROP TRIGGER reject_boot")
        assert (await peer.call("boot-repaired", "BootNotification", BOOT))[0] == 3
        assert peer.j.get("observation_generation") == generation + 1
        assert (await peer.call("repeat-boot", "BootNotification", BOOT))[0] == 3
        assert peer.j.get("observation_generation") == generation + 1
        await peer.flush()
        assert len([f for f in peer.cloud_calls if f[2] == action]) == 1


# Intent: bind a buffered untimed report to the generation present before the following Boot arrives.
# Why: the replacement reader can accept both messages before either asynchronous response sends.
@pytest.mark.parametrize("changed_body", [False, True])
async def test_buffered_observation_before_replacement_boot_uses_original_generation(tmp_path, tls, changed_body):
    async with ObservationTLS(tmp_path / "state.db", tls) as peer:
        action, payload = observation_payload("status-untimed", None)
        assert (await peer.call("status", action, payload))[0] == 3
        await peer.replace()
        gateway = peer.g
        await gateway.receive("charger", build_call("status", action, payload))
        await gateway.receive("charger", build_call("new-boot", "BootNotification", BOOT))
        await gateway.drain()
        fresh = payload | {"status": "Charging"} if changed_body else payload
        assert (await peer.call("status", action, fresh))[0] == 3
        assert peer.j.get("last_status") == fresh
        await peer.flush()
        assert [frame[3] for frame in peer.cloud_calls if frame[2] == action] == [payload, fresh]


# Intent: distinct IDs and genuine recurring statuses remain distinct while same-connection ID conflicts fail.
# Why: permanently unique bodies or a cross-connection CALL counter would erase real later departures.
@pytest.mark.parametrize("timed", [False, True])
async def test_distinct_status_identity_and_counter_reuse(tmp_path, tls, timed):
    async with ObservationTLS(tmp_path / "state.db", tls) as peer:
        action, payload = observation_payload("status-timed" if timed else "status-untimed", None)
        payload["status"] = "Available"
        for token, state in [("available", "Available"), ("preparing", "Preparing"), ("available-again", "Available")]:
            assert (await peer.call(token, action, payload | {"status": state}))[0] == 3
        assert (await peer.call("available", action, payload | {"status": "Charging"}))[0] == 4
        await peer.replace()
        assert (await peer.call("available", action, payload | {"status": "Charging"}))[0] == 3
        await peer.flush()
        states = [f[3]["status"] for f in peer.cloud_calls if f[2] == action]
        assert states == ["Available", "Preparing", "Available", "Charging"]


# Intent: a fresh row after an older row marks the session live on the first publication of a batched report.
# Why: meter retries and finite receipt retention must preserve genuine liveness and the reserved closure slot.
async def test_pending_receipt_pin_and_meter_retry_liveness(tmp_path, tls):
    async with ObservationTLS(tmp_path / "state.db", tls, capacity=2) as peer:
        local = (await peer.call("start", "StartTransaction", START))[2]["transactionId"]
        action, payload = observation_payload("meter-transaction", local)
        payload["meterValue"].append(payload["meterValue"][0] | {"timestamp": datetime.now(UTC).isoformat()})
        assert (await peer.call("meter", action, payload))[0] == 3
        assert peer.j.db.execute("SELECT live FROM transactions WHERE local=?", (local,)).fetchone()[0] == 1
        peer.j.db.execute("UPDATE transactions SET live=0 WHERE local=?", (local,))
        assert (await peer.call("meter", action, payload))[0] == 3
        assert peer.j.db.execute("SELECT live FROM transactions WHERE local=?", (local,)).fetchone()[0] == 0
        await peer.flush()
        for number in range(5):
            status = {"connectorId": 1, "status": "Charging", "errorCode": "NoError"}
            assert (await peer.call(uuid.uuid4().hex, "StatusNotification", status))[0] == 3
            await peer.flush()
        assert peer.j.db.execute("SELECT COUNT(*) FROM observation_receipts").fetchone()[0] <= 2 * peer.j.capacity
        assert peer.j.db.execute("SELECT COUNT(*) FROM observation_calls").fetchone()[0] <= 2 * peer.j.capacity
        await observation_suffix(peer, local)


# Intent: explicit Start retry waits and counts survive repeated process replacement without sending early.
# Why: restarting a temporarily refused delivery must not reset its budget, identity or linear wait interval.
async def test_start_retry_budget_and_deadline_survive_replacement(tmp_path, tls, monkeypatch):
    monkeypatch.setattr(managed_start_delivery, "RETRY_INTERVAL", 0.2)
    async with ObservationTLS(tmp_path / "retry.db", tls) as peer:
        peer.start_errors = 2
        local = (await peer.call("start", "StartTransaction", START))[2]["transactionId"]
        for count in (1, 2):
            await peer.flush()
            item = peer.j.next_delivery()
            assert item.action == "StartTransaction" and item.state == "queued"
            record = peer.j.get(managed_start_delivery.RETRIES)[item.token]
            assert record["errors"] == count
            await peer.replace(restart=True, boot=True)
            await peer.flush()
            assert len([f for f in peer.cloud_calls if f[2] == "StartTransaction"]) == count
            assert peer.j.get(managed_start_delivery.RETRIES)[item.token] == record
            await asyncio.sleep(max(0, record["next_epoch"] - time.time()) + 0.01)
        await peer.flush()
        assert peer.j.vendor_id(local) is not None and peer.j.next_delivery() is None
        starts = [f for f in peer.cloud_calls if f[2] == "StartTransaction"]
        assert len(starts) == 3 and len({f[1] for f in starts}) == 1
        await observation_suffix(peer, local)


# Intent: two pending publications survive terminal cache pressure and duplicate acceptance at full capacity.
# Why: eviction and a repeated CALL must not consume the transaction's separately reserved genuine Stop slot.
async def test_pending_receipts_survive_pressure_and_reserved_stop(tmp_path, tls):
    async with ObservationTLS(tmp_path / "pressure.db", tls, capacity=2) as peer:
        local = (await peer.call("start", "StartTransaction", START))[2]["transactionId"]
        await peer.flush()
        status = {"connectorId": 1, "status": "Charging", "errorCode": "NoError"}
        for _ in range(2):
            assert (await peer.call(uuid.uuid4().hex, "StatusNotification", status))[0] == 3
            await peer.flush()
        action, payload = observation_payload("meter-transaction", local)
        assert (await peer.call("pending-meter", action, payload))[0] == 3
        assert (await peer.call("pending-status", "StatusNotification", status))[0] == 3
        assert (await peer.call("pending-meter", action, payload))[0] == 3
        assert (await peer.call("over-capacity", "StatusNotification", status))[0] == 4
        assert peer.j.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 2
        assert (
            peer.j.db.execute("SELECT COUNT(*) FROM observation_receipts WHERE token='pending-meter'").fetchone()[0]
            == 1
        )
        assert (
            await peer.call(
                "stop",
                "StopTransaction",
                {"transactionId": local, "meterStop": 1300, "timestamp": "2026-01-01T00:02:00Z"},
            )
        )[0] == 3
        await peer.flush()
        assert (await peer.call("over-capacity", "StatusNotification", status))[0] == 3
        await peer.flush()
        await observation_suffix(peer, local)
