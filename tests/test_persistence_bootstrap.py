# Modified for the standalone iCS proxy repository.
"""First bootstrap is atomic; only the exact hardware-untouched legacy baseline gap is repairable."""

from __future__ import annotations

import socket
import subprocess
import sys
from pathlib import Path

import pytest

import ics_proxy_core
from ics_proxy_core.engine.managed import ManagedController
from ics_proxy_core.engine.native_faults import NativeContentError
from ics_proxy_core.engine.rpc import InboundCache
from ics_proxy_core.engine.security import SecurityError
from ics_proxy_core.engine.store import Journal, RecoveryRequiredError
from ics_proxy_core.engine.trial_bootstrap import pristine_interrupted_bootstrap
from ics_proxy_core.engine.trial_resources import ResourceBook
from ics_proxy_core.engine.trial_session import TrialSession, validate_state_path
from tests.connected_rig import connected_bench
from tests.managed_rig import healthy, introduced, managed_session
from tests.runtime_rig import FAST
from tests.tls_support import tls as tls

CHILD = """
import json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from ics_proxy_core.engine.store import Journal
from ics_proxy_core.engine.trial_resources import Baseline, ResourceBook
j = Journal(Path(sys.argv[2]), 'OFFLINE001')
def crash(sql):
    if sql.startswith('INSERT OR REPLACE INTO meta') and "'trial_phase'" in sql:
        os._exit(77)
j.db.set_trace_callback(crash)
ResourceBook(j, Baseline.parse(json.loads(Path(sys.argv[3]).read_text())))
raise SystemExit('phase boundary not reached')
"""


# Construct the production managed mode with its normal explicit connected/unplugged constraints.
def construct(rig):
    return TrialSession(
        rig.prepared, FAST, native_bounded=rig.spec, allow_local_start=False, allow_unplugged_native=True
    )


# Intent: refuse state files with more than one filename before opening the journal.
# Why: every journal and its side files must belong to one pathname and one owner lock.
@pytest.mark.parametrize("suffix", ["", ".lock", "-wal", "-shm"], ids=["journal", "lock", "wal", "shm"])
def test_shared_state_files_are_refused(tmp_path, suffix):
    path = tmp_path / "journal.sqlite"
    journal = Journal(path, "OFFLINE001")
    journal.put("probe", "retained")
    journal.close()
    state = path.with_name(path.name + suffix)
    synthetic = not state.exists()
    if synthetic:
        state.touch(mode=0o600)
    extra_name = tmp_path / "extra-name"
    extra_name.hardlink_to(state)
    with pytest.raises(SecurityError, match="without hard links"):
        validate_state_path(path)
    with pytest.raises(RecoveryRequiredError, match="without hard links"):
        Journal(path, "OFFLINE001", require_existing=True)
    extra_name.unlink()
    if synthetic:
        state.unlink()
    validate_state_path(path)
    journal = Journal(path, "OFFLINE001", require_existing=True)
    try:
        assert journal.get("probe") == "retained"
    finally:
        journal.close()


# Intent: process death immediately before phase insertion cannot leave a committed baseline without its phase.
# Why: both new initial metadata writes belong to one durable bootstrap transition, before any hardware effect.
async def test_phase_boundary_crash_rolls_back_first_baseline(tmp_path, tls, unused_tcp_port):
    async with connected_bench(tmp_path, tls, unused_tcp_port) as rig:
        root = Path(ics_proxy_core.__file__).parents[1]
        child = subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                CHILD,
                str(root),
                str(rig.prepared.config.journal),
                str(rig.prepared.config.baseline),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert child.returncode == 77, child.stderr
        journal = Journal(rig.prepared.config.journal, rig.prepared.config.identity)
        try:
            assert journal.get("trial_baseline") is None and journal.get("trial_phase") is None
            assert not journal.db.execute("SELECT 1 FROM transactions").fetchone()
            assert not journal.db.execute("SELECT 1 FROM mutations").fetchone()
        finally:
            journal.close()
        session = construct(rig)
        assert session.journal.get("trial_phase") == "prepared"
        assert session.journal.get("trial_baseline") == rig.prepared.baseline.value()
        assert not session.started and rig.bench.device_calls == []
        await session.close()


# Intent: the exact baseline-only state left by older code becomes a fresh admission with genuine Boot/native checks.
# Why: repairing metadata must neither fabricate hardware protection nor permanently lock out a pristine installation.
async def test_legacy_pristine_baseline_gap_reintroduces_over_real_tls(tmp_path, tls, unused_tcp_port):
    async with connected_bench(tmp_path, tls, unused_tcp_port) as rig:
        journal = Journal(rig.prepared.config.journal, rig.prepared.config.identity)
        journal.put("trial_baseline", rig.prepared.baseline.value())
        InboundCache(journal=journal)
        journal.close()
        session = managed_session(rig)
        assert session.journal.get("trial_phase") == "prepared"
        assert session.gateway.charger.sender is None and not session.gateway.controls.ready
        assert rig.bench.device_calls == []
        await session.start()
        controller = ManagedController(session)
        rig.route_to_trial()
        async with rig.charger(session) as charger:
            await introduced(rig, session, charger, "repaired-bootstrap")
            assert (await healthy(controller))["owner_control_enabled"]
            assert rig.bench.received_limits[0] == 0 and rig.bench.received_limits[-1] == 6
            assert len(rig.peer.requests) > 0
            await session.close()


# Intent: an occupied first listener leaves the same admission retryable on either approved route.
# Why: opening a listener and receiving a genuine Boot must stay separate from the durable admission commit.
@pytest.mark.parametrize("route", ["original", "trial"])
async def test_first_listener_retries_unchanged_admission(tmp_path, tls, unused_tcp_port, route):
    async with connected_bench(tmp_path, tls, unused_tcp_port) as rig:
        original_configuration = dict(rig.peer.configuration)
        first = managed_session(rig)
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", unused_tcp_port))
            occupied.listen()
            with pytest.raises(OSError):
                await first.start()
        assert not first.started and first.journal.phase.read().active and first.journal.phase.starting is True
        assert first.journal.get("trial_binding") is None and first.journal.get("boot") is None
        admission = first.journal.get("native_bounded_handover")
        allocation = first.journal.get("trial_allocation"), first.journal.get("next_transaction")
        stat = first.journal.path.stat()
        with pytest.raises(RecoveryRequiredError, match="active owner"):
            construct(rig)
        await first.close()
        assert rig.peer.configuration == original_configuration and not rig.bench.device_calls

        if route == "trial":
            rig.route_to_trial()
        replacement = managed_session(rig)
        await replacement.start()
        assert replacement.started and replacement.runtime.port == unused_tcp_port
        reopened = replacement.journal.path.stat()
        assert (reopened.st_dev, reopened.st_ino) == (stat.st_dev, stat.st_ino)
        assert replacement.journal.get("native_bounded_handover") == admission
        assert (replacement.journal.get("trial_allocation"), replacement.journal.get("next_transaction")) == allocation
        assert replacement.journal.get("trial_binding") is None and replacement.journal.get("boot") is None
        assert replacement.journal.phase.starting is True and replacement.gateway.controls.desired() == 0
        assert not replacement.gateway.controls.ready and not rig.bench.device_calls and not rig.bench.calls

        ManagedController(replacement)
        rig.route_to_trial()
        async with rig.charger(replacement) as charger:
            await introduced(rig, replacement, charger, "first-listener-retry")
            assert replacement.journal.get("trial_binding") is not None
            assert rig.bench.received_limits and set(rig.bench.received_limits) == {0}
            await replacement.close()

        rig.peer.configuration = original_configuration
        commissioned = managed_session(rig)
        with pytest.raises(NativeContentError, match="configuration"):
            await commissioned.start()
        assert not commissioned.started and commissioned.runtime._server is None
        assert set(rig.bench.received_limits) == {0}


# Intent: unrelated metadata, receipt/debt rows or tables cannot turn an ordinary journal into connected admission.
# Why: a strict pristine repair must not grant fresh authority to an already used or ambiguous installation.
@pytest.mark.parametrize(
    "blocker", ["metadata", "receipt", "outbox", "resource", "rpc", "clock", "report-audit", "unknown-table"]
)
async def test_legacy_baseline_repair_rejects_nonpristine_state(tmp_path, tls, unused_tcp_port, blocker):
    async with connected_bench(tmp_path, tls, unused_tcp_port) as rig:
        journal = Journal(rig.prepared.config.journal, rig.prepared.config.identity)
        # Create the real current schema, then fixture only the historical missing-phase gap.
        ResourceBook(journal, rig.prepared.baseline)
        journal.db.execute("DELETE FROM meta WHERE key='trial_phase'")
        if blocker == "metadata":
            journal.put("control", {"owner": "shared"})
        elif blocker == "receipt":
            journal.db.execute("INSERT INTO receipts VALUES('old','StartTransaction','digest','{}')")
        elif blocker == "outbox":
            journal.enqueue("Heartbeat", {})
        elif blocker == "resource":
            journal.db.execute("INSERT INTO trial_resources VALUES('profile:1','{}',NULL,'pending',NULL)")
        elif blocker == "rpc":
            InboundCache(journal=journal)
            journal.db.execute("INSERT INTO inbound_receipts VALUES('vendor',1,'old','digest',NULL)")
        elif blocker == "clock":
            journal.db.execute(
                "INSERT INTO transactions(local,connector,start,stop,vendor) VALUES(1001,1,'{}','{}',9001)"
            )
            journal.db.execute("INSERT INTO transaction_clock_discontinuities VALUES(1001,'raw-start','raw-stop',1)")
        elif blocker == "report-audit":
            journal.db.execute(
                "INSERT INTO rejected_observations VALUES('old','StatusNotification','digest',NULL,'NotSupported',"
                "'inflight',1)"
            )
        else:
            journal.db.execute("CREATE TABLE unrelated_ordinary_state(value TEXT)")
        before = sorted(journal.db.iterdump())
        journal.close()
        with pytest.raises((SecurityError, RecoveryRequiredError)):
            construct(rig)
        journal = Journal(rig.prepared.config.journal, rig.prepared.config.identity)
        try:
            assert journal.get("trial_phase") is None
            assert sorted(journal.db.iterdump()) == before
        finally:
            journal.close()
        assert rig.bench.device_calls == []


# Intent: only an empty known reply table is permitted; an orphan or unknown empty table remains blocking.
# Why: these direct predicate controls isolate the reply-table guard from the already-blocking nonempty outbox.
@pytest.mark.parametrize("blocker", ["orphan-reply", "unknown-empty-table"])
def test_pristine_bootstrap_reply_schema_guard_is_independent(tmp_path, blocker):
    journal = Journal(tmp_path / "shape-only.db", "OFFLINE001")
    try:
        # This is a predicate-only shape fixture, never a baseline admitted through ResourceBook or TrialSession.
        journal.put("trial_baseline", {"fixture": "schema predicate only"})
        assert pristine_interrupted_bootstrap(journal)
        assert journal.db.execute("SELECT COUNT(*) FROM delivery_replies").fetchone()[0] == 0
        if blocker == "orphan-reply":
            # Deliberately create an invalid orphan to ensure reply rows themselves, not another table, block repair.
            journal.db.execute("PRAGMA foreign_keys=OFF")
            try:
                journal.db.execute(
                    "INSERT INTO delivery_replies VALUES(?,?,?,?,?)",
                    ("orphan", "StopTransaction", "{}", None, '[3,"orphan",{}]'),
                )
            finally:
                journal.db.execute("PRAGMA foreign_keys=ON")
            assert journal.db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            assert journal.db.execute("SELECT COUNT(*) FROM delivery_replies").fetchone()[0] == 1
            assert journal.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
        else:
            journal.db.execute("CREATE TABLE unknown_empty_feature(value TEXT)")
            assert journal.db.execute("SELECT COUNT(*) FROM unknown_empty_feature").fetchone()[0] == 0
        # Every other known debt table is empty, so neither negative can pass via an unrelated debt check.
        for table in (
            "transactions",
            "receipts",
            "outbox",
            "mutations",
            "transaction_clock_discontinuities",
            "rejected_observations",
        ):
            assert journal.db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] == 0
        before = sorted(journal.db.iterdump())
        assert pristine_interrupted_bootstrap(journal) is False
        assert sorted(journal.db.iterdump()) == before
        assert journal.get("trial_phase") is None and not journal.db.in_transaction
    finally:
        journal.close()
