# Modified for the standalone iCS proxy repository.
"""Real SQLite transaction, storage exhaustion and writer contention failures retain durable recovery semantics."""

import sqlite3
from contextlib import closing

import pytest

from ics_proxy_core.engine.managed_intent import ManagedRequests, insert_request
from ics_proxy_core.engine.store import Journal, fingerprint
from tests.prototype_rig import START


# Intent: SQLite's automatic rollback on storage exhaustion preserves the original error and every prior fact.
# Why: attempting a second rollback must not mask SQLITE_FULL or falsely leave a partial committed transition.
def test_actual_sqlite_full_preserves_cause_and_can_recover(tmp_path):
    j = Journal(tmp_path / "bounded.db", "synthetic")
    try:
        j.put("preserved", {"before": True})
        pages = j.db.execute("PRAGMA page_count").fetchone()[0]
        j.db.execute("PRAGMA max_page_count=" + str(pages))
        with pytest.raises(sqlite3.OperationalError) as error:
            with j.atomic():
                j.put("atomic-first", True)
                j.put("exhaust", "x" * (256 * 1024))
        assert error.value.sqlite_errorcode == sqlite3.SQLITE_FULL
        assert not j.db.in_transaction and j.get("atomic-first") is None and j.get("exhaust") is None
        assert j.get("preserved") == {"before": True}
        j.db.execute("PRAGMA max_page_count=1000000")
        with j.atomic():
            j.put("atomic-first", "recovered")
        assert j.get("atomic-first") == "recovered"
    finally:
        j.close()


# Intent: a genuine deferred constraint fails at COMMIT and rolls back all real changes before a successful retry.
# Why: statement-abort tests alone do not establish commit-time atomicity.
def test_actual_commit_failure_rolls_back_and_retries(tmp_path):
    j = Journal(tmp_path / "commit.db", "synthetic")
    try:
        j.db.execute("CREATE TABLE parent(id INTEGER PRIMARY KEY)")
        j.db.execute("CREATE TABLE child(id INTEGER REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED)")
        with pytest.raises(sqlite3.IntegrityError) as error:
            with j.atomic():
                j.put("commit-transition", True)
                j.db.execute("INSERT INTO child VALUES(1)")
                assert j.db.in_transaction and j.get("commit-transition") is True
        assert error.value.sqlite_errorcode == sqlite3.SQLITE_CONSTRAINT_FOREIGNKEY
        assert not j.db.in_transaction and j.get("commit-transition") is None
        assert j.db.execute("SELECT COUNT(*) FROM child").fetchone()[0] == 0
        j.db.execute("INSERT INTO parent VALUES(1)")
        with j.atomic():
            j.put("commit-transition", True)
            j.db.execute("INSERT INTO child VALUES(1)")
        assert j.get("commit-transition") is True
    finally:
        j.close()


# Intent: real writer contention leaves both owners' transaction state intact and permits later ordinary commit.
# Why: BEGIN failure must not rollback another connection or become a synthetic successful transition.
def test_actual_writer_busy_does_not_change_journal(tmp_path):
    j = Journal(tmp_path / "busy.db", "synthetic")
    other = sqlite3.connect(j.path, isolation_level=None, timeout=0)
    try:
        j.put("preserved", True)
        j.db.execute("PRAGMA busy_timeout=0")
        other.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError) as error:
            with j.atomic():
                j.put("busy-transition", True)
        assert error.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
        assert other.in_transaction and not j.db.in_transaction
        assert j.get("busy-transition") is None and j.get("preserved") is True
        other.execute("ROLLBACK")
        with j.atomic():
            j.put("busy-transition", True)
        assert j.get("busy-transition") is True
    finally:
        other.close()
        j.close()


# Intent: settle a definite owner Start rejection from exact evidence after both normal writes fail.
# Why: live polling and process replacement must recover the same reply without inferring a session.
@pytest.mark.parametrize("replace_process", [False, True], ids=["live", "replacement"])
def test_owner_start_rejection_survives_refused_reply_writes(tmp_path, replace_process):
    path = tmp_path / "owner-start.db"
    request, ticket, boot, wire_id = "start-request", "start-ticket", "start-boot", "start-wire"

    # Check both the Start projection and its underlying wire witness.
    def check(recovered):
        receipt = recovered.starts.receipt()
        assert (receipt["stage"], receipt["ticket"], receipt["request_id"]) == ("rejected", ticket, request)
        (record,) = recovered.outcomes.owner_wires("request", request)
        assert (record.wire, record.ticket, record.boot, record.reply) == (wire_id, ticket, boot, "Rejected")
        assert record.local is None and recovered.active() is None

    with closing(Journal(path, "synthetic")) as j:
        j.starts.prepare(ticket, "tag-digest", request)
        j.starts.record(ticket, "issued")
        scope = j.outcomes.owner_scope(request, ticket, boot)
        wire = scope.sent(wire_id, "RemoteStartTransaction", {"idTag": "LOCAL_TEST"})
        assert wire is not None
        j.db.executescript("""
            CREATE TEMP TRIGGER refuse_owner_start_reply BEFORE INSERT ON owner_command_wires
            WHEN NEW.action='RemoteStartTransaction' AND NEW.reply='Rejected'
            BEGIN SELECT RAISE(ABORT, 'owner Start reply storage unavailable'); END;
            CREATE TEMP TRIGGER refuse_owner_start_projection BEFORE INSERT ON meta
            WHEN NEW.key='local_start_receipt' AND json_extract(NEW.value,'$.stage')='rejected'
            BEGIN SELECT RAISE(ABORT, 'owner Start projection storage unavailable'); END;
        """)
        frame = [3, wire_id, {"status": "Rejected"}]
        scope.received(wire, frame)
        with pytest.raises(sqlite3.IntegrityError, match="owner Start projection storage unavailable"):
            j.starts.record_reply_status(ticket, "Rejected", request_id=request)
        assert j.starts.receipt()["stage"] == "issued"
        assert j.db.execute("SELECT reply FROM owner_command_wires WHERE wire=?", (wire_id,)).fetchone()[0] is None
        assert not j.db.in_transaction
        if not replace_process:
            # Isolate the shared wire replay from the direct live callback's retry path.
            j.starts.pending_reply = None
            j.db.execute("DROP TRIGGER refuse_owner_start_reply")
            j.db.execute("DROP TRIGGER refuse_owner_start_projection")
            j.outcomes.retry_owner_writes()
            check(j)
            return

    with closing(Journal(path, "synthetic")) as recovered:
        check(recovered)


# An independent report must not consume a rejected Start whose receipt write still fails after replacement.
@pytest.mark.parametrize("wire_write_refused", [False, True], ids=["wire", "checkpoint"])
def test_retained_start_rejection_does_not_bind_independent_lifecycle(tmp_path, wire_write_refused):
    path = tmp_path / "owner-start-binding.db"
    request, ticket, tag = "start-request", "start-ticket", "LOCAL_TEST"
    with closing(Journal(path, "synthetic")) as j:
        j.put("control", {"ticket": ticket, "inhibit": True})
        j.starts.prepare(ticket, fingerprint(tag.casefold()), request)
        j.starts.armed(ticket)
        j.starts.record(ticket, "issued")
        scope = j.outcomes.owner_scope(request, ticket, "start-boot")
        wire = scope.sent("start-wire", "RemoteStartTransaction", {"idTag": tag})
        assert wire is not None
        if wire_write_refused:
            j.db.execute("""
                CREATE TRIGGER refuse_start_wire_reply BEFORE INSERT ON owner_command_wires
                WHEN NEW.reply='Rejected'
                BEGIN SELECT RAISE(ABORT, 'owner Start reply storage unavailable'); END
            """)
        j.db.execute("""
            CREATE TRIGGER refuse_start_rejection BEFORE INSERT ON meta
            WHEN NEW.key='local_start_receipt' AND json_extract(NEW.value,'$.stage')='rejected'
            BEGIN SELECT RAISE(ABORT, 'owner Start projection storage unavailable'); END
        """)
        scope.received(wire, [3, wire.wire, {"status": "Rejected"}])
        stored_reply = j.db.execute("SELECT reply FROM owner_command_wires WHERE wire=?", (wire.wire,)).fetchone()[0]
        assert stored_reply == (None if wire_write_refused else "Rejected")

    with closing(Journal(path, "synthetic")) as j:
        assert j.starts.receipt()["stage"] == "issued"
        # Exercise the saved wire evidence on its own, independent of any process-local retry marker.
        j.starts.pending_reply = None
        assert not j.starts.pending_local_tag(tag)
        payload = START | {"idTag": tag}
        local = j.start("independent-start", payload, lambda local: j.starts.bind(tag, local))["transactionId"]
        j.outcomes.retain_bound_owner_start()
        assert j.active() == local and j.known_start(payload)
        (wire,) = j.outcomes.owner_wires("request", request)
        assert wire.reply == "Rejected" and wire.local is None
        assert j.starts.receipt()["local"] is None

        j.db.execute("DROP TRIGGER refuse_start_rejection")
        j.outcomes.retry_owner_writes()
        assert j.starts.receipt()["stage"] == "rejected"
        assert j.get("control")["start_pending"] is False
        assert j.active() == local


# Intent: a definite forwarded Stop rejection survives replacement when normal result storage failed.
# Why: after reopening, its outcome can finish recording later without claiming physical closure.
def test_forwarded_stop_rejection_survives_refused_reply_write(tmp_path):
    path = tmp_path / "forwarded-stop.db"
    request, wire_id = "stop-request", "stop-wire"
    with closing(Journal(path, "synthetic")) as j:
        local = j.start("charger-start", START)["transactionId"]
        delivery = j.next_delivery()
        assert delivery is not None
        j.mark_sent(delivery)
        j.acknowledge(delivery, {"transactionId": 9001, "idTagInfo": {"status": "Accepted"}})
        assert j.local_id(9001) == local
        with j.atomic():
            mutation = j.mutation_intent("vendor", "RemoteStopTransaction", {"transactionId": 9001})
            j.bind_vendor_command(
                mutation, request=request, vendor_epoch=1, boot="stop-boot", wire=wire_id, local=local, profile=None
            )
            j.record_vendor_result(mutation, "dispatched")
        j.db.execute(
            "CREATE TEMP TRIGGER refuse_forwarded_stop_reply BEFORE UPDATE OF wire_result ON vendor_command_scope "
            "WHEN NEW.wire_result='Rejected' BEGIN SELECT RAISE(ABORT, 'forwarded Stop reply storage unavailable'); END"
        )
        answer = j.outcomes.capture(mutation, "RemoteStopTransaction", wire_id, [3, wire_id, {"status": "Rejected"}])
        assert answer is not None
        j.outcomes.publish(answer)
        command = j.outcomes.vendor(mutation)
        assert command is not None and (command.status, command.wire_result) == ("Rejected", "dispatched")
        assert not j.db.in_transaction
        j.db.execute(
            "CREATE TRIGGER refuse_forwarded_stop_settlement BEFORE UPDATE OF state ON mutations "
            "WHEN NEW.action='RemoteStopTransaction' AND NEW.state='confirmed' "
            "BEGIN SELECT RAISE(ABORT, 'forwarded Stop settlement storage unavailable'); END"
        )

    with closing(Journal(path, "synthetic")) as recovered:
        command = recovered.outcomes.vendor(mutation)
        assert command is not None and command.saving_reply and command.status == "Rejected"
        assert not recovered.db.in_transaction
        recovered.db.execute("DROP TRIGGER refuse_forwarded_stop_settlement")
        recovered.outcomes.retry()
        command = recovered.outcomes.vendor(mutation)
        assert command is not None and command.settled
        assert (command.action, command.wire, command.status) == ("RemoteStopTransaction", wire_id, "Rejected")
        assert command.session is not None and (command.session.local, command.session.closed) == (local, False)
        assert recovered.active() == local and not recovered.outcomes.unanswered_vendor_stop(local)


# Intent: recover a definite forwarded reply and retain the independent ambiguous Start report after replacement.
# Why: clearing an exact command timeout must still leave the next unresolved lifecycle delivery protected.
@pytest.mark.parametrize("retry_after_startup", [False, True], ids=["startup", "live-retry"])
def test_forwarded_reply_recovery_preserves_ambiguous_start_delivery(tmp_path, retry_after_startup):
    path = tmp_path / "forwarded-start.db"
    action, wire = "RemoteStartTransaction", "start-wire"
    with closing(Journal(path, "synthetic")) as journal:
        journal.start("start-report", START)
        delivery = journal.next_delivery()
        assert delivery is not None
        journal.mark_sent(delivery)
        journal.put("managed_mode", {"mode": "app_schedule"})
        journal.phase.block()
        with journal.atomic():
            mutation = journal.mutation_intent("vendor", action, {"idTag": START["idTag"]})
            journal.bind_vendor_command(
                mutation,
                request="start-request",
                vendor_epoch=1,
                boot="start-boot",
                wire=wire,
                local=None,
                profile=None,
            )
            journal.record_vendor_result(mutation, "dispatched")
            journal.fault.replace(
                {"classification": "owner_control_or_delivery_uncertain", "forwarded_timeout": mutation}
            )
        temporary = "" if retry_after_startup else "TEMP "
        journal.db.execute(
            f"CREATE {temporary}TRIGGER refuse_forwarded_start_settlement BEFORE UPDATE OF state ON mutations "
            "WHEN NEW.state='confirmed' BEGIN SELECT RAISE(ABORT, 'forwarded Start settlement unavailable'); END"
        )
        reply = journal.outcomes.capture(mutation, action, wire, [3, wire, {"status": "Accepted"}])
        assert reply is not None and not journal.outcomes.publish(reply)

    with closing(Journal(path, "synthetic")) as recovered:
        head = recovered.next_delivery()
        assert head is not None and head.state == "ambiguous" and head.token == delivery.token
        if retry_after_startup:
            command = recovered.outcomes.vendor(mutation)
            assert command is not None and command.saving_reply and command.status == "Accepted"
            assert recovered.fault.read({}).get("forwarded_timeout") == mutation
            recovered.db.execute("DROP TRIGGER refuse_forwarded_start_settlement")
            recovered.outcomes.retry()
        command = recovered.outcomes.vendor(mutation)
        assert command is not None and command.settled and command.status == "Accepted"
        assert recovered.fault.read({}) == {
            "classification": "owner_control_or_delivery_uncertain",
            "delivery_token": delivery.token,
        }


# Intent: only exact prior refusals release an unissued shutdown's wait after journal replacement.
# Why: a legacy dependency or a possible shutdown wire can still own the same open session.
@pytest.mark.parametrize("independent", [None, "own-wire", "legacy"])
def test_shutdown_waits_for_exact_prior_stops_or_an_independent_attempt(tmp_path, independent):
    path = tmp_path / "joined-shutdown.db"
    with closing(Journal(path, "synthetic")) as j:
        j.put("managed_mode", {"mode": "app_schedule"})
        ManagedRequests(j)
        local = j.start("start-report", START)["transactionId"]
        requests = ("original-stop-a", "original-stop-b")
        wires = []
        for request in requests:
            insert_request(j, request, "stop_session", 0, {"_session_stop": {"local": local, "dispatched": True}})
            j.db.execute("UPDATE managed_requests SET state='running' WHERE id=?", (request,))
            scope = j.outcomes.owner_scope(request, request, "boot")
            wire = scope.sent(request, "RemoteStopTransaction", {"transactionId": local})
            assert wire is not None
            wires.append((scope, wire))
        for ticket in ("original-shutdown", "replacement-shutdown"):
            with j.atomic():
                j.outcomes.claim_shutdown_stop(ticket, local, ticket, "boot")
            claim = j.get("trial_shutdown_attempt")
            assert claim["prior_pending_stop"] and claim["prior_local_attempts"] == list(requests)
            assert claim["stop_wire"] is None and j.outcomes.unanswered_shutdown_stop(local)
        if independent == "own-wire":
            scope = j.outcomes.owner_scope(None, ticket, "boot")
            scope.sent("shutdown-wire", "RemoteStopTransaction", {"transactionId": local})
        elif independent == "legacy":
            del claim["prior_local_attempts"]
            j.put("trial_shutdown_attempt", claim)
        unrelated = j.outcomes.owner_scope(requests[1], "another-session", "boot")
        wrong = unrelated.sent("wrong-session-wire", "RemoteStopTransaction", {"transactionId": local + 1})
        assert wrong is not None
        unrelated.received(wrong, [3, wrong.wire, {"status": "Rejected"}])
        first, first_wire = wires[0]
        first.received(first_wire, [3, first_wire.wire, {"status": "Rejected"}])
        assert j.outcomes.unanswered_shutdown_stop(local)
        last, last_wire = wires[1]
        last.received(last_wire, [3, last_wire.wire, {"status": "Rejected"}])
        j.outcomes.record_shutdown_stop(ticket, "uncertain")

    with closing(Journal(path, "synthetic")) as reopened:
        assert reopened.active() == local
        assert not reopened.outcomes.session_stop_dispatched(local)
        assert reopened.outcomes.unanswered_shutdown_stop(local) is (independent is not None)
