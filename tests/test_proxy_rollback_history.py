# Modified for the standalone iCS proxy repository.
"""The rollback reader recognises a settled migrated journal and its archived evidence."""

import json
import shutil
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import pytest

from ics_proxy_core.engine.store import Journal, fingerprint
from tests.process_helpers import process_rig
from tests.tls_support import tls as tls
from tests_installation.migrated_layout import seed_old_layout
from tools import check_proxy_rollback as rollback
from tools.check_proxy_rollback import RollbackRefusedError, check_snapshot, stopped_journal


# Intent: reject a rollback journal if its database, lock, or sidecars have hard links.
# Why: one stopped journal path must identify the SQLite files protected by the lock.
@pytest.mark.parametrize(
    "suffix", ["", ".lock", "-wal", "-shm", "-journal"], ids=["journal", "lock", "wal", "shm", "rollback"]
)
def test_stopped_journal_refuses_hardlinked_state(tmp_path, suffix):
    live = tmp_path / "live.sqlite"
    with closing(Journal(live, "SYNTHETIC")):
        pass
    shared = Path(str(live) + suffix)
    shared.touch(mode=0o600, exist_ok=True)
    alias = tmp_path / "backup.sqlite"
    if not suffix:
        alias.hardlink_to(live)
        Path(str(alias) + ".lock").touch(mode=0o600)
        check = alias
    else:
        alias.hardlink_to(shared)
        check = live

    before = set(tmp_path.iterdir())
    with pytest.raises(RollbackRefusedError, match="^journal_unavailable$"), stopped_journal(check):
        pytest.fail("rollback inspected a hard-linked journal")
    assert set(tmp_path.iterdir()) == before


# Intent: inspect a stopped journal and its uncheckpointed commits without modifying its files or directory.
# Why: SQLite can create sidecars beside a WAL database even when opened in read-only mode.
@pytest.mark.parametrize("committed_wal", [False, True], ids=["checkpointed", "uncheckpointed"])
def test_stopped_journal_does_not_write_sidecars(tmp_path, committed_wal):
    path = tmp_path / "stopped.sqlite"
    if committed_wal:
        source = tmp_path / "fixture.sqlite"
        with closing(Journal(source, "SYNTHETIC")) as journal:
            journal.db.execute("PRAGMA wal_autocheckpoint=0")
            journal.put("example", "checkpointed")
            journal.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            journal.put("example", "committed to WAL")
            shutil.copy2(source, path)
            shutil.copy2(Path(str(source) + "-wal"), Path(str(path) + "-wal"))
        Path(str(path) + ".lock").touch(mode=0o600)
        expected = "committed to WAL"
    else:
        with closing(Journal(path, "SYNTHETIC")) as journal:
            journal.put("example", "checkpointed")
        expected = "checkpointed"

    before = {file.name: file.read_bytes() for file in tmp_path.iterdir()}
    with stopped_journal(path) as db:
        assert db.execute("SELECT value FROM meta WHERE key='example'").fetchone() == (json.dumps(expected),)
    assert {file.name: file.read_bytes() for file in tmp_path.iterdir()} == before


# Save the same immutable timed-Start outcome and later verified Pause found in the installed journal.
def retain_timed_start(db, now):
    start, pause = "a" * 32, "b" * 32
    proof = {
        "prior_intent": {"mode": "app", "revision": 2},
        "prior_fault": {"classification": "native_authority_or_preservation_failed", "at_epoch": now - 20},
        "at_epoch": now - 10,
        "superseded_test_proofs": [{"request_id": start, "verified_pause_id": pause}],
    }
    db.execute("INSERT INTO meta VALUES('native_start_attempt_recovery',?)", (json.dumps(proof),))
    db.execute(
        "INSERT INTO managed_requests VALUES(?,'start_test',0,'uncertain',?)",
        (start, json.dumps({"classification": "start_not_confirmed"})),
    )
    db.execute(
        "INSERT INTO managed_requests VALUES(?,'pause',1,'completed',?)",
        (pause, json.dumps({"pause_verified": True, "completed_at": now - 30})),
    )
    return pause


# Save a synthetic historical charge and the exact reconciliation proof for an old timed Start.
def retain_history(db, now):
    meta = {key: json.loads(value) for key, value in db.execute("SELECT key,value FROM meta")}
    original = {"transactionId": 123, "startTime": 100, "stopTime": 200}
    handover = meta["native_bounded_handover"] | {"original_history": [original]}
    binding = {"identity": fingerprint(meta["identity"]), "original": fingerprint([original])}
    policy = {
        "version": 1,
        "mode": "archive_missing",
        "identity_digest": binding["identity"],
        "spec_digest": meta["managed_mode"]["spec_digest"],
        "original_history_digest": binding["original"],
    }
    entries = {
        "native_bounded_handover": handover,
        "native_history_archive_binding": binding,
        "native_history_policy": policy,
        "native_history_policy_acknowledgement": {
            "policy": policy,
            "at_epoch": now - 60,
            "native_digest": fingerprint([]),
            "charging_started": False,
        },
    }
    rows = [(key, json.dumps(value)) for key, value in entries.items()]
    db.executemany("INSERT OR REPLACE INTO meta VALUES(?,?)", rows)
    db.execute(
        "INSERT INTO native_history_records VALUES(?,?,?,?,?,1)",
        (original["transactionId"], fingerprint(original), json.dumps(original), now - 60, now - 60),
    )
    return retain_timed_start(db, now)


# Intent: allow a stopped migrated journal only with intact retained history and a proved old timed Start.
# Why: ignoring a genuinely ambiguous receipt would let an older release reopen pending physical work.
async def test_migrated_snapshot_requires_intact_history_and_later_pause(
    tmp_path, tls, unused_tcp_port_factory, monkeypatch
):
    seed_old_layout(tmp_path / "state.db")
    port = unused_tcp_port_factory
    async with process_rig(tmp_path, tls, port(), port(), monkeypatch, timing="fast-poll") as proxy:
        await proxy.start()
        status = await proxy.status()
        paused = await proxy.request("pause", revision=status["control_revision"])
        await proxy.completed_control(paused["result"]["id"])
        status = await proxy.status(lambda state: state["local_hold_verified"])
        await proxy.close()
        config = proxy.rig.prepared.config
        deferred_start = "managed_deferred_start"
        with closing(sqlite3.connect(config.journal)) as db, db:
            # Recreate the historical schema on this test journal after the candidate has stopped.
            # The old build never recorded these per-wire or forwarded outcome rows.
            for table in (
                "vendor_plan_effects",
                "vendor_profile_effects",
                "owner_start_sessions",
                "owner_command_wires",
            ):
                db.execute(f"DROP TABLE {table}")
            for table in (
                "auth_info_order",
                "auth_denial_replies",
                "delivery_reply_authorization",
                "vendor_command_scope",
            ):
                assert db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is None
                db.execute(f"DROP TABLE {table}")
            shutdown = db.execute("SELECT value FROM meta WHERE key='trial_shutdown_attempt'").fetchone()
            assert json.loads(shutdown[0])["state"] == "verified"
            db.execute("DELETE FROM meta WHERE key='trial_shutdown_attempt'")
            old_pause = retain_history(db, time.time())
            # Successful association of a same-tag Start retains the empty marker.
            db.execute("INSERT INTO meta VALUES(?,?)", (deferred_start, "{}"))

        # Even an empty authorization lookup table and its index are outside the historical rehearsal.
        with (
            stopped_journal(config.journal) as db,
            pytest.raises(RollbackRefusedError, match="^journal_schema_unrehearsed$"),
        ):
            check_snapshot(db, config, migrated_only=True)
        with closing(sqlite3.connect(config.journal)) as db, db:
            assert db.execute("SELECT 1 FROM auth_pending_queries LIMIT 1").fetchone() is None
            db.execute("DROP TABLE auth_pending_queries")

        snapshot = proxy.durable()
        with stopped_journal(config.journal) as db:
            assert check_snapshot(db, config, migrated_only=True) == status["control_revision"]
        assert proxy.durable() == snapshot

        current, previous = tmp_path / "current-fixture", tmp_path / "previous-fixture"
        with closing(sqlite3.connect(config.journal)) as db, db:
            db.execute("INSERT INTO meta VALUES('managed_temporary_schedule','[{}]')")
        with monkeypatch.context() as patch:
            patch.setattr(
                rollback,
                "build_matches",
                lambda root, build: (
                    (root, build) in ((current, rollback.MIGRATED_RELEASES[0]), (previous, rollback.PREVIOUS))
                ),
            )
            refusal = rollback.check_rollback(config, current, previous)
        assert not refusal.allowed and refusal.reason == "journal_unavailable_or_malformed"
        with closing(sqlite3.connect(config.journal)) as db, db:
            db.execute("DELETE FROM meta WHERE key='managed_temporary_schedule'")

        with closing(sqlite3.connect(config.journal)) as db, db:
            pending = {"local": 1001, "owner": True, "ticket": "a" * 32, "vendor": None}
            db.execute("UPDATE meta SET value=? WHERE key=?", (json.dumps(pending), deferred_start))
        with stopped_journal(config.journal) as db, pytest.raises(RollbackRefusedError, match="^unresolved_start$"):
            check_snapshot(db, config, migrated_only=True)

        control = snapshot["metadata"]["control"]
        with closing(sqlite3.connect(config.journal)) as db, db:
            db.execute("UPDATE meta SET value='{}' WHERE key=?", (deferred_start,))
            db.execute("UPDATE meta SET value=? WHERE key='control'", (json.dumps(control | {"resume_limit": 6.5}),))
        with (
            stopped_journal(config.journal) as db,
            pytest.raises(RollbackRefusedError, match="^settled_owner_pause_required$"),
        ):
            check_snapshot(db, config)
        with closing(sqlite3.connect(config.journal)) as db, db:
            db.execute("UPDATE meta SET value=? WHERE key='control'", (json.dumps(control),))
            db.execute("UPDATE managed_requests SET result='{}' WHERE id=?", (old_pause,))
        with stopped_journal(config.journal) as db, pytest.raises(RollbackRefusedError, match="^unresolved_request$"):
            check_snapshot(db, config)
        with closing(sqlite3.connect(config.journal)) as db, db:
            old_result = next(row["result"] for row in snapshot["requests"] if row["id"] == old_pause)
            db.execute("UPDATE managed_requests SET result=? WHERE id=?", (old_result, old_pause))
            db.execute("UPDATE native_history_records SET body='{}' WHERE original=1")
        with (
            stopped_journal(config.journal) as db,
            pytest.raises(RollbackRefusedError, match="^native_history_unverified$"),
        ):
            check_snapshot(db, config)
