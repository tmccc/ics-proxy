# Modified for the standalone iCS proxy repository.
"""Independent process cuts and a read-only report used to select the matching replay."""

import asyncio
import fcntl
import hashlib
import json
import shutil
import socket
import uuid
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from ics_proxy_core.engine.managed_intent import ManagedRequests
from ics_proxy_core.engine.store import Journal
from ics_proxy_core.engine.trial_session import save_private
from tests.managed_rig import starts
from tests.model_tls_support import model_tls as model_tls
from tests.model_tls_support import tls_for_case
from tests.peer_mutation_helpers import observe_device_replies
from tests.process_helpers import process_rig
from tests.runtime_rig import until
from tests.tls_support import tls as tls
from tests.trial_helpers import config_files
from tools import managed_incident, managed_proxy
from tools.managed_incident import MAX_REPORT_EVENTS, incident_report, reference
from tools.managed_stop_incident import stop_incident_report


# Obtain the exact same read-only report as the CLI for this independent process fixture.
def report(child):
    configuration = child.rig.prepared.config
    return incident_report(configuration.journal, configuration.incident_events)


# Use only the report's evidence to choose how to reproduce an interrupted Start on fresh peers.
def reconstruct(report_body):
    journal, wire = report_body["journal"], report_body["wire"]
    assert journal["request"]["request"] == wire["request"] and journal["start"]["ticket"] == wire["ticket"]
    assert journal["start"]["authority_published"] is False
    assert wire["send"] in ("send_returned", "matching_reply")
    saved = journal["start"]["saved_reply"]
    if saved == "accepted":
        assert journal["start"]["stage"] == "accepted"
        boundary = "after-accepted-commit"
    else:
        assert journal["start"]["stage"] == "issued" and saved == wire["observed_reply"] == "unknown"
        boundary = "charger-holds-reply"
    return boundary, journal["request"]["selected_current_a"]


# Kill at a proven committed SQLite boundary or after a real receiving charger withholds its matching reply.
async def cut_and_restart(tmp, model_tls, ports, monkeypatch, boundary, selected, *, output_fails=False):
    tmp.mkdir()
    async with process_rig(
        tmp, tls_for_case(tmp, model_tls), ports(), ports(), monkeypatch, selected, timing="fast-poll"
    ) as child:
        if output_fails:
            child.rig.prepared.config.incident_events.mkdir()
        await child.start()
        ready = await child.status()
        seen, release = asyncio.Event(), asyncio.Event()
        bench = child.rig.bench
        replies = observe_device_replies(SimpleNamespace(sock=child.socket), monkeypatch)
        identifier = uuid.uuid4().hex
        held = boundary == "charger-holds-reply"

        # The independent charger receives and handles the Start frame before withholding its actual reply.
        async def hold(action, *_):
            if action == "RemoteStartTransaction":
                seen.set()
                await release.wait()

        if held:
            bench.before_reply = hold
        else:
            await child.arm(boundary, identifier)
        request = asyncio.create_task(child.request("start", id=identifier, revision=ready["control_revision"]))
        try:
            if held:
                await asyncio.wait_for(seen.wait(), 4)
                await until(lambda: report(child)["wire"]["send"] == "send_returned", timeout=2)
            else:
                gate = await child.gate(boundary, identifier)
                assert gate["transaction_open"] is (boundary == "before-accepted-commit")
                if boundary == "before-accepted-commit":
                    before_publication = report(child)["journal"]["start"]
                    assert before_publication["stage"] == "issued" and before_publication["saved_reply"] == "accepted"
            frames = starts(child.rig)
            before = child.durable()["metadata"]["local_start_receipt"]
            assert before["request_id"] == identifier and before["local"] is None
            if boundary == "after-issued-commit":
                assert not frames and before["stage"] == "issued"
            else:
                assert len(frames) == 1
                accepted = [3, frames[0][1], {"status": "Accepted"}] in replies
                assert accepted is not held
                assert before["stage"] == ("accepted" if boundary == "after-accepted-commit" else "issued")
            await child.kill()
        finally:
            release.set()
            bench.before_reply = None
            if not request.done():
                request.cancel()
            await asyncio.gather(request, return_exceptions=True)
        await child.start()
        await child.status(lambda value: value.get("charger_connected") is True)
        if not output_fails:
            await until(
                lambda: len([row for row in report(child)["observations"] if row["event"] == "process_started"]) == 2
            )
        before_report = child.durable()
        learned = report(child)
        expected = {**before, "stage": "accepted"} if boundary == "before-accepted-commit" else before
        assert before_report["metadata"]["local_start_receipt"] == expected
        assert child.durable()["metadata"]["local_start_receipt"] == before_report["metadata"]["local_start_receipt"]
        assert child.durable()["requests"] == before_report["requests"]
        assert starts(child.rig) == frames
        assert len(child.kills) == 1
        assert learned["journal"]["request"]["request"] != identifier
        assert identifier not in json.dumps(learned) and before["ticket"] not in json.dumps(learned)
        save_private(tmp / "incident-report.json", learned)
        return learned


# Intent: a report alone selects an actual repeatable crash and the original selected current on fresh peers.
# Why: durable acceptance and an unknown reply must not lead a reviewer to reproduce the same interruption.
@pytest.mark.parametrize("boundary,selected", [("after-accepted-commit", 6), ("charger-holds-reply", 16)])
async def test_incident_recreates_interrupted_start(
    tmp_path, model_tls, unused_tcp_port_factory, monkeypatch, boundary, selected
):
    args = (model_tls, unused_tcp_port_factory, monkeypatch)
    original = await cut_and_restart(tmp_path / "observed", *args, boundary, selected)
    exported = json.loads((tmp_path / "observed" / "incident-report.json").read_text())
    assert exported == original
    replay = reconstruct(exported)
    assert replay == (boundary, selected)
    recreated = await cut_and_restart(tmp_path / "recreated", *args, *replay)
    assert reconstruct(recreated) == replay
    for result in (original, recreated):
        assert result["journal"]["start"]["lifecycle"] == "unbound"
        assert result["waiting_for_evidence"] == [
            "start_lifecycle" if boundary == "after-accepted-commit" else "start_outcome_or_lifecycle"
        ]
        runs = [row["run"] for row in result["observations"] if row["event"] == "process_started"]
        sent = [row["run"] for row in result["observations"] if row["event"] == "start_sent"]
        assert len(set(runs)) == 2
        assert sent == [runs[0]]


# Intent: issued is a journal fact even when the process never sends the frame.
# Why: a cut can lie exactly between the receipt commit and completion of the actual send.
async def test_incident_issued_leaves_transmission_unknown(tmp_path, model_tls, unused_tcp_port_factory, monkeypatch):
    learned = await cut_and_restart(
        tmp_path / "issued", model_tls, unused_tcp_port_factory, monkeypatch, "after-issued-commit", 6
    )
    assert learned["journal"]["start"]["stage"] == "issued"
    assert learned["wire"]["send"] == learned["wire"]["observed_reply"] == "unknown"


# Intent: the journal exposes a real Accepted while the separate Start authority write is still uncommitted.
# Why: the reply survives restart independently, and the real Start lifecycle must still arrive.
async def test_incident_durable_reply_visible_before_control_commit(
    tmp_path, model_tls, unused_tcp_port_factory, monkeypatch
):
    learned = await cut_and_restart(
        tmp_path / "before-commit", model_tls, unused_tcp_port_factory, monkeypatch, "before-accepted-commit", 6
    )
    assert learned["journal"]["start"]["stage"] == "accepted"
    assert learned["journal"]["start"]["saved_reply"] == "accepted"
    assert learned["journal"]["start"]["authority_published"] is False
    assert learned["waiting_for_evidence"] == ["start_lifecycle"]


# Intent: a genuine late Accepted is correlated with the exact newer Pause through a process replacement.
# Why: a stale client revision and a real older reply can coexist with a different current request.
async def test_incident_retains_late_reply_and_newer_pause(tmp_path, model_tls, unused_tcp_port_factory, monkeypatch):
    ports = unused_tcp_port_factory
    async with process_rig(
        tmp_path, tls_for_case(tmp_path, model_tls), ports(), ports(), monkeypatch, timing="fast-poll"
    ) as child:
        await child.start()
        ready = await child.status()
        first = (await child.request("pause", revision=ready["control_revision"]))["result"]["id"]
        await child.completed_control(first)
        paused = await child.status(lambda status: status.get("local_hold_verified") is True)
        replies = observe_device_replies(SimpleNamespace(sock=child.socket), monkeypatch)
        seen, release = asyncio.Event(), asyncio.Event()
        bench = child.rig.bench

        # Keep the actual old response awaiting delivery until this newer owner request commits.
        async def hold(action, *_):
            if action == "RemoteStartTransaction":
                seen.set()
                await release.wait()

        bench.before_reply = hold
        start, pause = uuid.uuid4().hex, uuid.uuid4().hex
        try:
            assert (await child.request("start", id=start, revision=paused["control_revision"]))["ok"]
            await asyncio.wait_for(seen.wait(), 4)
            frames = starts(child.rig)
            assert len(frames) == 1 and paused["control_revision"] > 0
            assert (await child.request("pause", id=pause, revision=0))["ok"]
            expected = [3, frames[0][1], {"status": "Accepted"}]
            assert expected not in replies
            release.set()
            await until(lambda: (report(child)["journal"]["start"] or {}).get("saved_reply") == "accepted")
            await until(lambda: report(child)["wire"]["observed_reply"] == "accepted")
            assert expected in replies
        finally:
            release.set()
            bench.before_reply = None
        await child.kill()
        await child.start()
        await child.status(lambda status: status.get("charger_connected") is True)
        learned = report(child)
        intent = learned["journal"]["intent"]
        assert intent["mode"] == "paused" and intent["revision"] == paused["control_revision"] + 1
        assert intent["pause_request"] == hashlib.sha256(pause.encode()).hexdigest()[:32]
        sent, superseding, reply = [
            row
            for row in learned["observations"]
            if row["event"] in ("start_sent", "start_reply") or row.get("request") == intent["pause_request"]
        ]
        assert [row["event"] for row in (sent, superseding, reply)] == ["start_sent", "pause_committed", "start_reply"]
        assert sent["sequence"] < superseding["sequence"] < reply["sequence"]
        assert sent["intent"]["revision"] == paused["control_revision"]
        assert reply["intent"]["revision"] == intent["revision"] and reply["pause_ticket"] == superseding["ticket"]
        assert learned["journal"]["start"]["saved_reply"] == "accepted"
        assert learned["journal"]["start"]["authority_published"] is False
        assert starts(child.rig) == frames
        save_private(tmp_path / "late-incident-report.json", learned)


# Intent: failed optional event writes cannot prevent a genuine reply from being saved.
# Why: the recovery journal remains useful when the reporting destination is unavailable.
async def test_incident_output_failure_keeps_committed_reply(tmp_path, model_tls, unused_tcp_port_factory, monkeypatch):
    learned = await cut_and_restart(
        tmp_path / "failed-output",
        model_tls,
        unused_tcp_port_factory,
        monkeypatch,
        "after-accepted-commit",
        6,
        output_fails=True,
    )
    assert learned["observations"] == []
    assert learned["wire"]["send"] == "matching_reply" and learned["wire"]["observed_reply"] == "unknown"
    assert learned["journal"]["start"]["saved_reply"] == "accepted"


# Intent: an incident export can read a private configuration without networking or creating a new journal.
# Why: an absent installation should be reported as unknown without changing its state.
@pytest.mark.parametrize(
    "arguments,kind", [([], None), (["--kind", "stop"], "stop-session"), (["--kind", "vendor-stop"], "vendor-stop")]
)
def test_incident_cli_uses_local_read_only_sources(tmp_path, tls, capsys, monkeypatch, arguments, kind):
    config, file = config_files(tmp_path, 21010, 21011)

    # Fail immediately if incident constructs a charger transport or resolves any connection.
    def forbidden(*args, **kwargs):
        raise AssertionError("incident attempted networking")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    before = sorted(tmp_path.iterdir())
    assert managed_proxy.main(["incident", "--config", str(file), *arguments]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["schema_version"] == 1 and result["observations"] == []
    assert result["journal"]["available"] is False and result["journal"]["request"] is None
    if kind is None:
        assert "kind" not in result and result["journal"]["start"] is None
        assert result["wire"] == {"request": None, "ticket": None, "send": "unknown", "observed_reply": "unknown"}
    else:
        assert result["kind"] == kind and result["journal"]["lifecycle"] == "unknown"
        assert result["wire"] == {
            "dispatch": "unknown",
            "reply": "unknown",
            "reply_source": None,
            "joined_attempts": [],
        }
    assert not config.journal.exists() and sorted(tmp_path.iterdir()) == before


# Intent: read stopped journal snapshots with and without uncheckpointed records.
# Why: reporting must retain committed WAL records without creating sidecars or changing source files.
@pytest.mark.parametrize("read_only", [False, True], ids=["writable", "read-only"])
@pytest.mark.parametrize(
    "sidecars",
    [(), ("-wal",), ("-shm",), ("-wal", "-shm"), ("-wal", "-shm", ".lock")],
    ids=["checkpointed", "uncheckpointed", "index-only", "paired-backup", "stopped-owner"],
)
def test_incident_reads_stopped_journal_without_writes(tmp_path, read_only, sidecars):
    archive = tmp_path / "backup"
    archive.mkdir(mode=0o700)
    path = archive / "journal.sqlite"
    request = uuid.uuid4().hex
    source = tmp_path / "fixture.sqlite" if sidecars else path
    journal = Journal(source, "OFFLINE001")
    try:
        requests = ManagedRequests(journal)
        journal.db.execute("PRAGMA wal_autocheckpoint=0")
        if "-wal" in sidecars:
            journal.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        requests.accept(request, "start", 0)
        if sidecars == ("-shm",):
            journal.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        if sidecars:
            shutil.copy2(source, path)
            for suffix in sidecars:
                shutil.copy2(Path(str(source) + suffix), Path(str(path) + suffix))
    finally:
        journal.close()
    files_before = {file.name: file.read_bytes() for file in archive.iterdir()}
    if read_only:
        archive.chmod(0o500)
    try:
        events = archive / "events.jsonl"
        saved = incident_report(path, events)["journal"]
        assert saved["available"] is True and saved["request"]["request"] == reference(request)
        assert {file.name: file.read_bytes() for file in archive.iterdir()} == files_before
        assert stop_incident_report(path, events)["journal"]["available"] is True
        assert {file.name: file.read_bytes() for file in archive.iterdir()} == files_before
    finally:
        archive.chmod(0o700)


# Intent: retry the snapshot when the owner closes the journal during the copy.
# Why: a clean exit checkpoints the journal and removes sidecars the reader may have observed.
def test_incident_reads_journal_as_owner_closes(tmp_path, monkeypatch):
    path = tmp_path / "journal.sqlite"
    request = uuid.uuid4().hex
    journal = Journal(path, "OFFLINE001")
    files_after_close = {}

    # Trigger checkpoint and sidecar removal after the first database copy.
    def close_during_copy(source, target):
        shutil.copyfile(source, target)
        if source == path and not files_after_close:
            journal.close()
            files_after_close.update({file.name: file.read_bytes() for file in tmp_path.iterdir()})

    try:
        ManagedRequests(journal).accept(request, "start", 0)
        monkeypatch.setattr(managed_incident, "shutil", SimpleNamespace(copyfile=close_during_copy))
        saved = incident_report(path, tmp_path / "events.jsonl")["journal"]
    finally:
        journal.close()
    assert saved["available"] is True and saved["request"]["request"] == reference(request)
    assert set(files_after_close) == {"journal.sqlite", "journal.sqlite.lock"}
    assert {file.name: file.read_bytes() for file in tmp_path.iterdir()} == files_after_close


# Intent: report the latest committed request after the source WAL changes during a copy.
# Why: a copy that missed a commit must be retried without updating the original SQLite index.
def test_incident_retries_when_live_wal_changes_during_copy(tmp_path, monkeypatch):
    path = tmp_path / "journal.sqlite"
    wal = path.with_name(path.name + "-wal")
    request = uuid.uuid4().hex
    journal = Journal(path, "OFFLINE001")
    files_after_commit = {}

    # Commit another request at the point where the reader has just copied the old WAL.
    def commit_during_wal_copy(source, target):
        shutil.copyfile(source, target)
        if source == wal and not files_after_commit:
            requests.accept(request, "start", 0)
            files_after_commit.update({file.name: file.read_bytes() for file in tmp_path.iterdir()})

    try:
        requests = ManagedRequests(journal)
        journal.db.execute("PRAGMA wal_autocheckpoint=0")
        journal.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        requests.accept(uuid.uuid4().hex, "start", 0)
        monkeypatch.setattr(managed_incident, "shutil", SimpleNamespace(copyfile=commit_during_wal_copy))
        saved = incident_report(path, tmp_path / "events.jsonl")["journal"]
        assert saved["available"] is True and saved["request"]["request"] == reference(request)
        assert {file.name: file.read_bytes() for file in tmp_path.iterdir()} == files_after_commit
    finally:
        journal.close()


# Intent: use the active SQLite writer's published commit boundary when the WAL already contains a later commit frame.
# Why: rebuilding the index from the untrimmed copy can show a request before the owner's commit finishes.
@pytest.mark.parametrize("index_state", ["published", "checkpointed", "missing", "torn"])
def test_incident_respects_the_published_live_wal_boundary(tmp_path, index_state):
    source = tmp_path / "source.sqlite"
    archive = tmp_path / "archive"
    archive.mkdir(mode=0o700)
    path = archive / "journal.sqlite"
    wal, index, lock = (Path(str(path) + suffix) for suffix in ("-wal", "-shm", ".lock"))
    source_index = Path(str(source) + "-shm")
    first, next_request = (uuid.uuid4().hex for _ in range(2))
    journal = Journal(source, "OFFLINE001")
    try:
        journal.db.execute("PRAGMA wal_autocheckpoint=0")
        requests = ManagedRequests(journal)
        requests.accept(first, "start", 0)
        if index_state == "checkpointed":
            journal.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        published = source_index.read_bytes()
        requests.accept(next_request, "start", 0)
        shutil.copy2(source, path)
        shutil.copy2(Path(str(source) + "-wal"), wal)
        if index_state != "missing":
            shutil.copy2(source_index, index)
            if index_state == "torn":
                published = published[:48] + source_index.read_bytes()[48:]
            index.write_bytes(published)
        shutil.copy2(Path(str(source) + ".lock"), lock)
    finally:
        journal.close()

    files = {file.name: file.read_bytes() for file in archive.iterdir()}
    events = archive / "events.jsonl"
    # Simulate the active owner with the two independently generated on-disk commit boundaries.
    with lock.open("rb") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        live = incident_report(path, events)["journal"]
        if index_state in ("missing", "torn"):
            assert live == {"available": False, "intent": None, "request": None, "start": None}
        else:
            assert live["available"] is True and live["request"]["request"] == reference(first)
    stopped = incident_report(path, events)["journal"]
    assert stopped["available"] is True and stopped["request"]["request"] == reference(next_request)
    assert {file.name: file.read_bytes() for file in archive.iterdir()} == files


# Intent: refuse journal aliases that lack the live owner's path-specific lock and WAL index.
# Why: raw recovery of a hard-linked live WAL can report a commit the original writer has yet to publish.
def test_incident_refuses_an_aliased_live_journal(tmp_path):
    path, alias = tmp_path / "journal.sqlite", tmp_path / "alias.sqlite"
    request = uuid.uuid4().hex
    journal = Journal(path, "OFFLINE001")
    try:
        ManagedRequests(journal).accept(request, "start", 0)
        alias.hardlink_to(path)
        Path(str(alias) + "-wal").hardlink_to(Path(str(path) + "-wal"))
        for candidate in (path, alias):
            assert incident_report(candidate, tmp_path / "events.jsonl")["journal"]["available"] is False
    finally:
        journal.close()


# Intent: the incident command selects a retained local Stop by its redacted request reference.
# Why: the most recent Stop may belong to a different operator request.
def test_incident_cli_selects_the_exact_stop_reference(tmp_path, tls, capsys):
    config, file = config_files(tmp_path, 21010, 21011)
    first, later = uuid.uuid4().hex, uuid.uuid4().hex
    journal = Journal(config.journal, "OFFLINE001")
    try:
        requests = ManagedRequests(journal)
        requests.accept(first, "stop_session", 0)
        requests.finish(first, "failed", {"classification": "stop_not_sent"})
        requests.accept(later, "stop_session", 0)
    finally:
        journal.close()
    arguments = ["incident", "--config", str(file), "--kind", "stop"]
    assert managed_proxy.main([*arguments, "--request-reference", reference(first)]) == 0
    output = capsys.readouterr().out
    assert json.loads(output)["journal"]["request"]["request"] == reference(first)
    assert first not in output and later not in output


# Intent: a newer unsent local Start remains the incident selected after cloud test metadata replaces it.
# Why: the report must never attach an older command's physical reply to the latest owner request.
@pytest.mark.parametrize("damaged_state", [False, True])
def test_incident_uses_latest_local_request_even_without_wire_receipt(tmp_path, damaged_state):
    path = tmp_path / "journal.db"
    event_path = tmp_path / "journal.start-events.jsonl"
    old, new, cloud, ticket = (uuid.uuid4().hex for _ in range(4))
    journal = Journal(path, "OFFLINE001")
    try:
        requests = ManagedRequests(journal)
        requests.accept(old, "start", 0)
        requests.finish(old, "completed", {"local_session_verified": True})
        journal.put("local_start_receipt", {"request_id": old, "ticket": ticket, "stage": "accepted"})
        requests.accept(new, "start", 1)
        requests.finish(new, "failed", {"classification": "preflight_refused_unsent"})
        if damaged_state:
            journal.db.execute("UPDATE managed_requests SET state=CAST(? AS TEXT) WHERE id=?", (b"\xff", new))
        requests.accept(cloud, "arm_cloud_test", 1)
        journal.put("managed_test_charge", {"id": cloud, "source": "cloud", "state": "armed"})
        managed_proxy.event(
            event_path,
            {
                "run": uuid.uuid4().hex,
                "sequence": 1,
                "at": 1,
                "event": "start_reply",
                "request": reference(old),
                "ticket": reference(ticket),
                "reply": "accepted",
            },
        )
        learned = incident_report(path, event_path)
        saved = learned["journal"]
        assert saved["request"] == {
            "request": hashlib.sha256(new.encode()).hexdigest()[:32],
            "action": "start",
            "revision": 1,
            "state": "unknown" if damaged_state else "failed",
        }
        assert saved["start"] is None
        assert learned["wire"] == {
            "request": reference(new),
            "ticket": None,
            "send": "unknown",
            "observed_reply": "unknown",
        }
    finally:
        journal.close()


# Intent: a committed charger refusal or error is visible when the optional incident log is absent.
# Why: a real reply and actual session completion are separate evidence for the original request.
@pytest.mark.parametrize(
    "wire_reply,shown,waiting",
    [
        ("error", "call_error", "start_outcome_or_lifecycle"),
        ("Rejected", "rejected", "saved_start_reply_settlement"),
    ],
)
def test_incident_reads_the_exact_saved_start_reply(tmp_path, wire_reply, shown, waiting):
    path = tmp_path / "journal.db"
    events = tmp_path / "journal.start-events.jsonl"
    journal = Journal(path, "OFFLINE001")
    request, ticket = uuid.uuid4().hex, uuid.uuid4().hex
    try:
        ManagedRequests(journal)
        journal.db.execute("INSERT INTO managed_requests VALUES(?,'start',0,'uncertain','{}')", (request,))
        journal.put("local_start_receipt", {"request_id": request, "ticket": ticket, "stage": "issued", "local": None})
        journal.db.execute(
            "INSERT INTO owner_command_wires VALUES(?,?,?,NULL,'RemoteStartTransaction',NULL,NULL,'Accepted')",
            (uuid.uuid4().hex, request, uuid.uuid4().hex),
        )
        assert incident_report(path, events)["journal"]["start"]["saved_reply"] == "unknown"
        journal.db.execute(
            "INSERT INTO owner_command_wires VALUES(?,?,?,NULL,'RemoteStartTransaction',NULL,NULL,?)",
            (uuid.uuid4().hex, request, ticket, wire_reply),
        )
    finally:
        journal.close()

    result = incident_report(path, events)
    assert result["journal"]["start"]["saved_reply"] == shown
    assert result["journal"]["start"]["lifecycle"] == "unbound"
    assert result["wire"]["send"] == "matching_reply" and result["wire"]["observed_reply"] == "unknown"
    assert result["waiting_for_evidence"] == [waiting]


# Intent: reject stored Start replies whose wire, Boot, session or limit fields fail validation.
# Why: a matching request and ticket alone cannot prove that the charger replied to the original Start.
@pytest.mark.parametrize(
    "override",
    [{"wire": "bad-wire"}, {"boot": "bad-boot"}, {"local": 42}, {"limit": 6}],
    ids=["wire", "boot", "local", "limit"],
)
def test_incident_rejects_a_misbound_primary_start_reply(tmp_path, override):
    path, events = tmp_path / "journal.db", tmp_path / "events.jsonl"
    request, ticket = uuid.uuid4().hex, uuid.uuid4().hex
    with closing(Journal(path, "OFFLINE001")) as journal:
        ManagedRequests(journal).accept(request, "start", 0)
        journal.put("local_start_receipt", {"request_id": request, "ticket": ticket, "stage": "issued", "local": None})
        fields = {"wire": "l-" + uuid.uuid4().hex, "boot": uuid.uuid4().hex, "local": None, "limit": None}
        journal.db.execute(
            "INSERT INTO owner_command_wires VALUES(:wire,:request,:ticket,:boot,"
            "'RemoteStartTransaction',:local,:limit,'Accepted')",
            {**fields, **override, "request": request, "ticket": ticket},
        )

    report = incident_report(path, events)
    assert report["journal"]["start"]["saved_reply"] == "unknown"
    assert report["wire"]["send"] == "unknown"
    assert report["waiting_for_evidence"] == ["start_outcome_or_lifecycle"]


# Intent: show a real reply checkpoint before the normal wire receipt or Start result is published.
# Why: a refused write must preserve matched evidence without implying control authority or a real session.
@pytest.mark.parametrize("dispatch_saved", [True, False], ids=["saved-dispatch", "refused-dispatch"])
@pytest.mark.parametrize(
    "reply,waiting", [("Accepted", "start_lifecycle"), ("Rejected", "saved_start_reply_settlement")]
)
def test_incident_reads_retained_owner_start_reply(tmp_path, dispatch_saved, reply, waiting):
    path = tmp_path / "journal.db"
    events = tmp_path / "journal.start-events.jsonl"
    request, ticket, boot = (uuid.uuid4().hex for _ in range(3))
    wire_id = "l-" + uuid.uuid4().hex
    key = "retained_transaction_reply_owner:" + wire_id
    journal = Journal(path, "OFFLINE001")
    try:
        ManagedRequests(journal).accept(request, "start", 0)
        journal.put("managed_test_charge", {"id": request, "start_rpc_accepted": False})
        journal.starts.prepare(ticket, "tag-digest", request)
        journal.starts.record(ticket, "issued")
        scope = journal.outcomes.owner_scope(request, ticket, boot)
        if dispatch_saved:
            wire = scope.sent(wire_id, "RemoteStartTransaction", {})
        journal.db.execute(
            "CREATE TEMP TRIGGER refuse_start_wire BEFORE INSERT ON owner_command_wires "
            "BEGIN SELECT RAISE(ABORT, 'owner wire storage unavailable'); END"
        )
        if not dispatch_saved:
            wire = scope.sent(wire_id, "RemoteStartTransaction", {})
        assert wire is not None
        scope.received(wire, [3, wire_id, {"status": reply}])
        checkpoint = journal.get(key)
        assert checkpoint == {**asdict(wire), "reply": reply}
        stored = journal.db.execute("SELECT reply FROM owner_command_wires WHERE wire=?", (wire_id,)).fetchall()
        assert [tuple(row) for row in stored] == ([(None,)] if dispatch_saved else [])

        report = incident_report(path, events)
        assert report["journal"]["start"] == {
            "ticket": hashlib.sha256(ticket.encode()).hexdigest()[:32],
            "stage": "issued",
            "saved_reply": reply.lower(),
            "authority_published": False,
            "lifecycle": "unbound",
            "cloud_mapping": "unknown",
            "pending_delivery": [],
        }
        assert report["wire"]["send"] == "matching_reply" and report["wire"]["observed_reply"] == "unknown"
        assert report["waiting_for_evidence"] == [waiting]
        assert wire_id not in json.dumps(report) and journal.get(key) == checkpoint
        assert journal.starts.receipt()["stage"] == "issued"

        journal.db.execute("DROP TRIGGER refuse_start_wire")
        journal.outcomes.retry_owner_writes()
        assert journal.get(key) is None
        assert incident_report(path, events)["journal"]["start"]["stage"] == reply.lower()
    finally:
        journal.close()


# Intent: recover an issued Start using only a valid retained reply when its primary write was refused.
# Why: malformed checkpoint data must not prevent opening the journal or prove that the charger replied.
@pytest.mark.parametrize("damaged", [False, True])
def test_restart_checks_retained_start_reply_when_primary_write_was_refused(tmp_path, damaged):
    path = tmp_path / "journal.db"
    request, ticket, boot = (uuid.uuid4().hex for _ in range(3))
    wire_id = "l-" + uuid.uuid4().hex
    key = "retained_transaction_reply_owner:" + wire_id
    journal = Journal(path, "OFFLINE001")
    try:
        ManagedRequests(journal).accept(request, "start", 0)
        journal.starts.prepare(ticket, "tag-digest", request)
        journal.starts.record(ticket, "issued")
        scope = journal.outcomes.owner_scope(request, ticket, boot)
        journal.db.execute(
            "CREATE TEMP TRIGGER refuse_start_wire BEFORE INSERT ON owner_command_wires "
            "BEGIN SELECT RAISE(ABORT, 'owner wire storage unavailable'); END"
        )
        wire = scope.sent(wire_id, "RemoteStartTransaction", {})
        scope.received(wire, [3, wire_id, {"status": "Rejected"}])
        saved = journal.get(key)
        assert saved is not None
        if damaged:
            journal.put(key, saved | {"unexpected": "field"})
    finally:
        journal.close()

    journal = Journal(path, "OFFLINE001")
    try:
        assert journal.starts.receipt()["stage"] == ("issued" if damaged else "rejected")
        assert journal.active() is None
        if damaged:
            assert journal.get(key) == saved | {"unexpected": "field"}
    finally:
        journal.close()


# Intent: ignore a checkpoint from another binding or one that contradicts the durable wire.
# Why: only the original issued Start can lend its reply to this report.
@pytest.mark.parametrize(
    "field,value",
    [
        ("request", "0" * 32),
        ("ticket", "1" * 32),
        ("action", "RemoteStopTransaction"),
        ("wire", "l-" + "2" * 32),
        ("boot", "3" * 32),
        ("local", 42),
        ("limit_a", 6),
        ("reply", "other"),
    ],
)
def test_incident_ignores_malformed_or_unrelated_start_checkpoint(tmp_path, field, value):
    path = tmp_path / "journal.db"
    events = tmp_path / "journal.start-events.jsonl"
    request, ticket, boot = (uuid.uuid4().hex for _ in range(3))
    wire_id = "l-" + uuid.uuid4().hex
    key = "retained_transaction_reply_owner:" + wire_id
    journal = Journal(path, "OFFLINE001")
    try:
        ManagedRequests(journal).accept(request, "start", 0)
        journal.starts.prepare(ticket, "tag-digest", request)
        journal.starts.record(ticket, "issued")
        wire = journal.outcomes.owner_scope(request, ticket, boot).sent(wire_id, "RemoteStartTransaction", {})
        assert wire is not None
        checkpoint = {**asdict(wire), "reply": "Accepted"}
        journal.put(key, {**checkpoint, field: value})
        other = "l-" + uuid.uuid4().hex
        journal.put("retained_transaction_reply_owner:" + other, {**checkpoint, "wire": other})

        report = incident_report(path, events)
        assert report["journal"]["start"]["saved_reply"] == "unknown"
        assert report["wire"]["send"] == "unknown"
        assert report["waiting_for_evidence"] == ["start_outcome_or_lifecycle"]
    finally:
        journal.close()


# Intent: preserve a completed Start stage when reading a journal from before per-wire records.
# Why: an older installation can have a definite result even when the newer receipt table is absent.
@pytest.mark.parametrize("stage", ["issued", "accepted", "rejected"])
def test_incident_preserves_historical_start_receipt(tmp_path, stage):
    path = tmp_path / "journal.db"
    journal = Journal(path, "OFFLINE001")
    request, ticket = uuid.uuid4().hex, uuid.uuid4().hex
    try:
        ManagedRequests(journal).accept(request, "start", 0)
        journal.put("local_start_receipt", {"request_id": request, "ticket": ticket, "stage": stage, "local": None})
        journal.db.execute("DROP TABLE owner_command_wires")
        report = incident_report(path, tmp_path / "journal.start-events.jsonl")
        assert report["journal"]["start"]["saved_reply"] == ("unknown" if stage == "issued" else stage)
        assert report["journal"]["start"]["lifecycle"] == "unbound"
    finally:
        journal.close()


# Intent: keep the exact Start and its charge evidence when optional metadata cannot be decoded.
# Why: a saved intent or owner state can be damaged independently of the receipt and transaction.
@pytest.mark.parametrize("key", ["managed_control_intent", "managed_test_charge"])
def test_incident_keeps_bound_start_with_unreadable_metadata(tmp_path, key):
    path, events = tmp_path / "journal.db", tmp_path / "missing-events.jsonl"
    request, ticket = uuid.uuid4().hex, uuid.uuid4().hex
    with closing(Journal(path, "OFFLINE001")) as journal:
        ManagedRequests(journal).accept(request, "start", 0)
        journal.put(
            "local_start_receipt", {"request_id": request, "ticket": ticket, "stage": "accepted", "local": None}
        )
        journal.db.execute("INSERT OR REPLACE INTO meta VALUES(?,CAST(? AS TEXT))", (key, b'{"bad":"\xff"}'))

    report = incident_report(path, events)
    assert report["journal"]["available"]
    assert report["journal"]["request"]["request"] == reference(request)
    assert report["journal"]["start"]["ticket"] == reference(ticket)
    assert report["journal"]["start"]["saved_reply"] == "accepted"


# Intent: keep readable lifecycle and queued evidence when an optional journal table is absent or has an old schema.
# Why: the Start and Stop reports gather independent facts from the same private snapshot.
@pytest.mark.parametrize(
    "missing", ["outbox", "replies", "reply_fields", "delivery_state", "transactions", "mapping", "closure"]
)
def test_incident_keeps_independent_lifecycle_and_delivery(tmp_path, missing):
    path, events = tmp_path / "journal.db", tmp_path / "missing-events.jsonl"
    start, ticket, stop = (uuid.uuid4().hex for _ in range(3))
    with closing(Journal(path, "OFFLINE001")) as journal:
        requests = ManagedRequests(journal)
        requests.accept(start, "start", 0)
        requests.finish(start, "completed", {})
        requests.accept(stop, "stop_session", 1)
        requests.finish(stop, "completed", {"session_stopped": True, "_session_stop": {"local": 42}})
        journal.put("local_start_receipt", {"request_id": start, "ticket": ticket, "stage": "accepted", "local": 42})
        journal.db.execute(
            "INSERT INTO transactions(local,connector,start,stop,vendor) VALUES(42,1,'{}',?,99)",
            (json.dumps({"transactionId": 42, "timestamp": "2026-11-01T01:00:00Z"}),),
        )
        if missing == "outbox":
            journal.db.execute("DROP TABLE outbox")
        else:
            journal.db.execute(
                "INSERT INTO outbox(token,action,payload,local,state) "
                "VALUES('delivery','StopTransaction','{}',42,'queued')"
            )
        if missing in ("outbox", "replies", "reply_fields"):
            journal.db.execute("DROP TABLE delivery_replies")
        if missing == "reply_fields":
            journal.db.execute("CREATE TABLE delivery_replies(token TEXT PRIMARY KEY)")
            journal.db.execute("INSERT INTO delivery_replies VALUES('delivery')")
        if missing == "delivery_state":
            journal.db.execute(
                "INSERT INTO outbox(token,action,payload,local,state) "
                "VALUES('other','StartTransaction','{}',42,CAST(? AS TEXT))",
                (b"\xff",),
            )
        if missing == "transactions":
            journal.db.execute("DROP TABLE transactions")
        if missing == "mapping":
            journal.db.execute("ALTER TABLE transactions RENAME COLUMN vendor TO old_vendor")
        if missing == "closure":
            journal.db.execute("ALTER TABLE transactions RENAME COLUMN stop TO old_stop")

    start_report = incident_report(path, events)
    stop_report = stop_incident_report(path, events)
    delivery = [] if missing == "outbox" else [{"event": "StopTransaction", "state": "queued", "reply_saved": False}]
    if missing == "delivery_state":
        delivery.append({"event": "StartTransaction", "state": "unknown", "reply_saved": False})
    for report, request in ((start_report, start), (stop_report, stop)):
        assert report["journal"]["available"]
        assert report["journal"]["request"]["request"] == reference(request)
        assert report["observations"] == [] and report["sampled_status"] is None
    lifecycle = start_report["journal"]["start"]
    assert lifecycle["saved_reply"] == "accepted"
    for facts in (lifecycle, stop_report["journal"]):
        assert facts["lifecycle"] == ("unknown" if missing in ("transactions", "closure") else "closed")
        assert facts["cloud_mapping"] == ("unknown" if missing in ("transactions", "mapping") else "recorded")
        assert facts["pending_delivery"] == delivery
    expected_timestamp = None if missing in ("transactions", "closure") else "2026-11-01T01:00:00+00:00"
    assert stop_report["journal"]["charger_reported_stop_at"] == expected_timestamp
    assert start_report["waiting_for_evidence"] == (["cloud_delivery"] if delivery else [])


# Intent: project only exact retained reply and lifecycle records, with a bounded and redacted event excerpt.
# Why: saved cloud reply settlement, unknown cloud delivery and unrelated stored observations mean different things.
def test_incident_binds_only_exact_bounded_records(tmp_path):
    path = tmp_path / "journal.db"
    event_path = tmp_path / "journal.start-events.jsonl"
    token, start, ticket = "private-token", uuid.uuid4().hex, uuid.uuid4().hex
    journal = Journal(path, "OFFLINE001")
    try:
        ManagedRequests(journal)
        journal.db.execute("INSERT INTO managed_requests VALUES(?,'start',0,'uncertain','{}')", (start,))
        journal.put("local_start_receipt", {"request_id": start, "ticket": ticket, "stage": "accepted", "local": 42})
        journal.db.execute(
            "INSERT INTO transactions(local,connector,start,stop,vendor) VALUES(42,1,'private-start','private-stop',99)"
        )
        journal.db.execute(
            "INSERT INTO outbox(token,action,payload,local,state) "
            "VALUES(?,'StopTransaction','private-body',42,'inflight')",
            (token,),
        )
        journal.db.execute(
            "INSERT INTO delivery_replies VALUES(?,'StopTransaction','different-body',42,'private-reply')", (token,)
        )
        assert incident_report(path, event_path)["waiting_for_evidence"] == ["cloud_delivery"]
        journal.db.execute("UPDATE delivery_replies SET payload='private-body' WHERE token=?", (token,))

        row = {
            "run": uuid.uuid4().hex,
            "at": 1,
            "event": "start_sent",
            "request": hashlib.sha256(start.encode()).hexdigest()[:32],
            "ticket": hashlib.sha256(ticket.encode()).hexdigest()[:32],
            "free_text": "private-endpoint",
        }
        for n in range(MAX_REPORT_EVENTS + 3):
            managed_proxy.event(event_path, {**row, "sequence": n + 1})
        managed_proxy.event(
            event_path,
            {
                **row,
                "sequence": MAX_REPORT_EVENTS + 4,
                "event": "start_reply",
                "ticket": uuid.uuid4().hex,
                "reply": "rejected",
            },
        )
        with event_path.open("ab") as stream:
            stream.write(b'{"private-torn-field":')
        learned = incident_report(path, event_path)
        assert len(learned["observations"]) == MAX_REPORT_EVENTS
        assert learned["wire"]["observed_reply"] == "unknown"
        assert learned["journal"]["start"]["pending_delivery"] == [
            {"event": "StopTransaction", "state": "inflight", "reply_saved": True}
        ]
        assert learned["journal"]["start"]["lifecycle"] == "closed"
        assert learned["waiting_for_evidence"] == ["saved_cloud_reply_settlement"]
        assert "private-" not in json.dumps(learned)
        managed_proxy.event(
            event_path,
            {**row, "sequence": MAX_REPORT_EVENTS + 5, "event": "start_reply", "reply": "accepted"},
        )
        learned = incident_report(path, event_path)
        assert learned["wire"]["observed_reply"] == "accepted"
        assert "private-" not in json.dumps(learned)
        journal.db.execute("DELETE FROM delivery_replies WHERE token=?", (token,))
        journal.db.execute("UPDATE outbox SET state='rejected' WHERE token=?", (token,))
        rejected = incident_report(path, event_path)["journal"]["start"]["pending_delivery"]
        assert rejected == [{"event": "StopTransaction", "state": "rejected", "reply_saved": False}]
    finally:
        journal.close()
