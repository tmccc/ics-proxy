"""Exercise the work queue with real pytest sessions and an intentionally blocked worker."""

import json
import os
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import parallel_worker, qualification_collection
from tools.audit_parallel_qualification import dispatch_grants
from tools.parallel_worker import WorkQueue

ROOT = Path(__file__).resolve().parents[1]
SUITE = """\
import os
import time
from pathlib import Path

import pytest

worker = Path(os.environ["TMPDIR"])

@pytest.fixture(scope="session", autouse=True)
def shared_resource():
    with (worker / "fixture").open("a") as output:
        output.write("setup\\n")
    yield
    with (worker / "fixture").open("a") as output:
        output.write("teardown\\n")

def test_wait():
    (worker / "waiting").touch()
    deadline = time.monotonic() + 20
    while not (worker / "release").exists():
        assert time.monotonic() < deadline
        time.sleep(0.01)

@pytest.mark.parametrize("case", range(6))
def test_short(case):
    pass
"""


# Intent: collection and manifest generation share one monotonic deadline before any containers launch.
# Why: a slow preparation phase previously received a fresh timeout or no bound at all.
def test_preparation_consumes_the_worker_deadline(tmp_path, monkeypatch):
    source = tmp_path / "source"
    (source / "tests_model").mkdir(parents=True)
    (source / "tests_model/catalog.py").touch()
    config = {
        "mode": "full",
        "scheduler": "pull-v1",
        "workers": 2,
        "timeout_seconds": 10,
        "label": "unit",
        "commit": "commit",
        "source_digest": "digest",
        "image": "unused",
        "harness_hashes": {},
        "runner": "tools/offline_validate.py",
        "runner_args": [],
        "specs": ["tests"],
        "suite": "engine-model",
        "expected_packages": {},
    }
    (tmp_path / "config.json").write_text(json.dumps(config))
    (tmp_path / "snapshot.json").write_text(json.dumps({"commit": config["commit"]}))
    clock = SimpleNamespace(seconds=0)
    monkeypatch.setattr(
        parallel_worker, "time", SimpleNamespace(monotonic=lambda: clock.seconds, time=lambda: 300 - clock.seconds)
    )
    monkeypatch.setattr(parallel_worker, "signal", SimpleNamespace(signal=lambda *_: None, SIGTERM=1, SIGINT=2))
    monkeypatch.setattr(parallel_worker, "ROOT", tmp_path)
    monkeypatch.setattr(parallel_worker, "SOURCE", source)
    monkeypatch.setattr(parallel_worker, "ABORT", None)
    monkeypatch.setattr(parallel_worker, "snapshot_digest", lambda _: config["source_digest"])
    monkeypatch.setattr(qualification_collection, "runtime_digest", lambda _: "runtime")
    monkeypatch.setattr(parallel_worker, "container_command", lambda *_: ["docker"])
    monkeypatch.setattr(parallel_worker, "telemetry", lambda _: None)
    monkeypatch.setattr(parallel_worker.os, "sched_getaffinity", lambda _: set(range(6)))
    cleanup = []
    monkeypatch.setattr(parallel_worker, "cleanup_containers", lambda rows: cleanup.extend(rows) or [])
    budgets = []

    # Stub only the child-process boundary to advance the deterministic clock between actual supervisor phases.
    def child(command, **kwargs):
        if not (timeout := kwargs.get("timeout")):
            assert command[2].endswith("/audit_parallel_qualification.py")
            return subprocess.CompletedProcess(command, 1)
        budgets.append(timeout)
        if "--collect-only" in command:
            (tmp_path / "collection/results.jsonl").write_text(
                json.dumps({"event": "collection", "ids": ["tests/test_sample.py::test_first"]}) + "\n"
            )
            clock.seconds = 7
            return subprocess.CompletedProcess(command, 0)
        assert "manifest" in command[-2]
        raise subprocess.TimeoutExpired(command[:1], timeout)

    # Any worker launch means the supervisor ignored its exhausted preparation budget.
    def forbidden(*args, **kwargs):
        pytest.fail("a worker launched after preparation timed out")

    monkeypatch.setattr(
        parallel_worker, "subprocess", SimpleNamespace(run=child, Popen=forbidden, STDOUT=subprocess.STDOUT)
    )
    assert parallel_worker.main() == 1
    result = json.loads((tmp_path / "result.json").read_text())
    assert budgets == [10, 3] and not result["complete"] and result["groups"] == []
    assert result["abort_reason"].startswith("TimeoutExpired")
    assert len(cleanup) == 1 and cleanup[0]["container_name"].endswith("-collection")


# Intent: preserve the post-SIGTERM grace and classify an unexpected exit even when the last worker exits.
# Why: slow host observations must leave time for descendants to exit, and a crash needs an explicit result reason.
@pytest.mark.parametrize("outcome", ["slow-telemetry", "last-worker-crash", "completed-test-failure"])
def test_supervisor_grace_and_final_worker_outcomes(tmp_path, monkeypatch, outcome):
    source = tmp_path / "source"
    source.mkdir()
    config = {
        "mode": "full",
        "scheduler": "pull-v1",
        "workers": 2,
        "timeout_seconds": 10,
        "label": "unit",
        "commit": "commit",
        "source_digest": "digest",
        "image": "unused",
        "harness_hashes": {},
        "runner": "tools/offline_ha_validate.py",
        "runner_args": [],
        "specs": ["tests_ha_proxy"],
        "suite": "ha-proxy",
        "expected_packages": {},
    }
    (tmp_path / "config.json").write_text(json.dumps(config))
    (tmp_path / "snapshot.json").write_text(json.dumps({"commit": config["commit"]}))
    clock = SimpleNamespace(seconds=0.0)
    first, last = SimpleNamespace(returncode=0), SimpleNamespace(returncode=None)
    first.poll = lambda: first.returncode
    last.poll = lambda: last.returncode
    launches = iter((first, last))
    request = tmp_path / "1/request.json"
    terms, kills = [], []
    delayed = False

    # Cross the qualification deadline during the first running-worker telemetry sample.
    def telemetry(workers):
        nonlocal delayed
        if workers and outcome == "slow-telemetry" and not delayed:
            delayed = True
            clock.seconds += 10

    # Deliver the unexpected exit between polls, or let a normal pytest failure consume its final grant.
    def sleep(seconds):
        clock.seconds += seconds
        assert clock.seconds < 20
        if outcome == "last-worker-crash":
            last.returncode = 3
        elif outcome == "completed-test-failure":
            grant = json.loads(request.with_name("grant.json").read_text())
            if grant["id"] is None:
                last.returncode = 1
            else:
                request.write_text(json.dumps(grant["sequence"] + 1))

    # Track the actual time of the signal, after time spent identifying the worker's process tree.
    def terminate(process, signum):
        assert process is last and signum == parallel_worker.signal.SIGTERM
        clock.seconds += 2
        terms.append(clock.seconds)
        return {42: "owned"}

    # Simulate hard termination without sending any real signal or launching a container.
    def kill(identities):
        assert identities == {42: "owned"}
        kills.append(clock.seconds)
        last.returncode = -9

    # Feed the real supervisor a minimal collection and an independent failing audit.
    def child(command, **kwargs):
        if "--collect-only" in command:
            (tmp_path / "collection/results.jsonl").write_text(
                json.dumps({"event": "collection", "ids": ["tests_ha_proxy/test_sample.py::test_first"]}) + "\n"
            )
            return subprocess.CompletedProcess(command, 0)
        assert command[2].endswith("/audit_parallel_qualification.py")
        return subprocess.CompletedProcess(command, 1)

    # Create a work request only for the worker that will finish its test and drain its grants.
    def launch(*args, **kwargs):
        process = next(launches)
        if process is last and outcome == "completed-test-failure":
            request.write_text("0\n")
        return process

    monkeypatch.syspath_prepend(source)
    monkeypatch.setattr(
        parallel_worker,
        "time",
        SimpleNamespace(monotonic=lambda: clock.seconds, time=lambda: 300 + clock.seconds, sleep=sleep),
    )
    monkeypatch.setattr(parallel_worker, "signal", SimpleNamespace(signal=lambda *_: None, SIGTERM=15, SIGINT=2))
    monkeypatch.setattr(parallel_worker, "ROOT", tmp_path)
    monkeypatch.setattr(parallel_worker, "SOURCE", source)
    monkeypatch.setattr(parallel_worker, "ABORT", None)
    monkeypatch.setattr(parallel_worker, "snapshot_digest", lambda _: config["source_digest"])
    monkeypatch.setattr(qualification_collection, "runtime_digest", lambda _: "runtime")
    monkeypatch.setattr(parallel_worker, "container_command", lambda *_: ["docker"])
    monkeypatch.setattr(parallel_worker, "telemetry", telemetry)
    monkeypatch.setattr(parallel_worker.os, "sched_getaffinity", lambda _: set(range(6)))
    monkeypatch.setattr(parallel_worker, "cleanup_containers", lambda _: [])
    monkeypatch.setattr(parallel_worker, "stop_tree", terminate)
    monkeypatch.setattr(parallel_worker, "kill_survivors", kill)
    monkeypatch.setattr(
        parallel_worker, "subprocess", SimpleNamespace(run=child, Popen=launch, STDOUT=subprocess.STDOUT)
    )

    assert parallel_worker.main() == 1
    result = json.loads((tmp_path / "result.json").read_text())
    assert not result["complete"]
    if outcome == "slow-telemetry":
        assert result["abort_reason"] == "qualification_timeout"
        assert len(terms) == len(kills) == 1 and kills[0] - terms[0] >= 3
        assert result["groups"][-1]["rc"] == -9
    else:
        assert not terms and not kills
        if outcome == "last-worker-crash":
            assert result["abort_reason"] == "worker_1_exit_3" and result["groups"][-1]["rc"] == 3
        else:
            assert result["abort_reason"] is None and result["groups"][-1]["rc"] == 1


# Run the real offline runner and queue plugin against a small local synthetic test source.
def start_worker(source, directory, output):
    directory.mkdir()
    return subprocess.Popen(
        [
            sys.executable,
            "-B",
            str(source / "tools/offline_validate.py"),
            "-p",
            "tools.qualification_evidence",
            "-x",
            "--qualification-work-queue",
            "cases",
        ],
        cwd=source,
        env={
            **os.environ,
            "TMPDIR": str(directory),
            "SCENARIO_RESULTS": str(directory / "results.jsonl"),
            "QUALIFICATION_EXPECTED_PACKAGES": "{}",
            "MODEL_WORKER_INDEX": directory.name,
            "PYTEST_ADDOPTS": "",
            "PYTEST_PLUGINS": "",
        },
        stdout=output,
        stderr=subprocess.STDOUT,
    )


# Pump the same supervisor used in qualification, with a short diagnostic deadline.
def run_until(condition, queue, workers):
    deadline = time.monotonic() + 15
    while not condition():
        assert time.monotonic() < deadline, "worker did not reach its expected marker"
        for index, process in enumerate(workers):
            assert process.poll() in (None, 0)
            if process.poll() is None:
                queue.dispatch(index)
        time.sleep(0.01)


# Intent: a free worker completes queued cases while another is still executing its first case.
# Why: fixed shards cannot help a blocked worker, and restarting pytest would recreate the session fixture.
def test_idle_worker_drains_queue_without_restarting_pytest(tmp_path):
    source = tmp_path / "source"
    (source / "tools").mkdir(parents=True)
    for name in ("offline_validate.py", "qualification_evidence.py", "qualification_collection.py"):
        shutil.copyfile(ROOT / "tools" / name, source / "tools" / name)
    (source / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    (source / "cases").mkdir()
    (source / "cases/test_sample.py").write_text(SUITE)
    prefix = "cases/test_sample.py::"
    selected = [prefix + "test_wait", *(prefix + f"test_short[{i}]" for i in range(6))]
    workers = []
    with (tmp_path / "dispatch.jsonl").open("w") as ledger, (tmp_path / "pytest.log").open("w") as output:
        queue = WorkQueue(tmp_path, selected, ledger)
        try:
            workers.append(start_worker(source, tmp_path / "0", output))
            run_until((tmp_path / "0/waiting").exists, queue, workers)
            workers.append(start_worker(source, tmp_path / "1", output))
            run_until(lambda: workers[1].poll() is not None, queue, workers)
            assert workers[1].returncode == 0 and workers[0].poll() is None
            assert queue.assigned[1] == selected[2:]
            (tmp_path / "0/release").touch()
            run_until(lambda: workers[0].poll() is not None, queue, workers)
            assert workers[0].returncode == 0
        finally:
            for process in workers:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)

    grants, errors = dispatch_grants(tmp_path, selected, 2)
    assert not errors
    for index in range(2):
        directory = tmp_path / str(index)
        records = [json.loads(line) for line in (directory / "results.jsonl").read_text().splitlines()]
        assert [row["ids"] for row in records if row["event"] == "collection"] == [selected]
        assert [(row["sequence"], row["id"]) for row in records if row["event"] == "grant"] == grants[index]
        assert [row["id"] for row in records if row["event"] == "start"] == queue.assigned[index]
        reports = [row for row in records if row["event"] == "report"]
        assert Counter((row["id"], row["phase"], row["outcome"]) for row in reports) == Counter(
            (identifier, phase, "passed")
            for identifier in queue.assigned[index]
            for phase in ("setup", "call", "teardown")
        )
        assert (directory / "fixture").read_text() == "setup\nteardown\n"
