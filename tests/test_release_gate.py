"""The release gate: which checks a release needs and how their results are judged."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from tests.tool_loader import load_tool

gate = load_tool("release_gate")


# Intent: build a minimal check-run record the way the GitHub API returns it.
# Why: the gate reads only name, status and conclusion.
def _run(name: str, status: str = "completed", conclusion: str | None = "success") -> dict[str, object]:
    return {"name": name, "status": status, "conclusion": conclusion}


# Intent: the hosted gate follows the actual aggregate job whose prerequisites cover both matrices.
def test_gate_uses_the_workflows_aggregate_check() -> None:
    workflow = Path(__file__).resolve().parents[1] / ".github/workflows/checks.yml"
    jobs = yaml.safe_load(workflow.read_text())["jobs"]
    assert set(jobs["checks"]["needs"]) == {"static", "engine", "ha-layer"}
    assert gate.REQUIRED_CHECKS == (jobs["checks"].get("name", "checks"),)


# Intent: a missing or unfinished required check waits, a finished failure fails at once, all green passes.
# Why: a missing aggregate check might still run; a completed failure must not pass.
def test_evaluate_waits_fails_or_passes() -> None:
    assert gate.evaluate([_run("checks")]) == ("pass", "all required checks succeeded")
    assert gate.evaluate([_run("validate"), _run("validate-hacs")])[0] == "pending"
    assert gate.evaluate([_run("checks", status="in_progress", conclusion=None)])[0] == "pending"
    assert gate.evaluate([_run("checks", conclusion="failure")])[0] == "fail"


# Intent: the hosted checker polls the exact commit and ignores other providers and other commits.
def test_check_run_fetch_is_bound_to_actions_and_full_commit(monkeypatch) -> None:
    sha = "a" * 40
    other_commit = {**_run("checks"), "head_sha": "b" * 40, "app": {"slug": "github-actions"}}
    other_provider = {**_run("checks"), "head_sha": sha, "app": {"slug": "other-service"}}
    right = {**_run("checks"), "head_sha": sha, "app": {"slug": "github-actions"}}

    def fetch(command, **kwargs):
        assert command[:3] == ["gh", "api", f"repos/example/ics-proxy/commits/{sha}/check-runs?filter=all"]
        return SimpleNamespace(stdout="\n".join(json.dumps(row) for row in [other_commit, other_provider, right]))

    monkeypatch.setattr(gate.subprocess, "run", fetch)
    assert gate._fetch_check_runs("example/ics-proxy", sha) == [right]
    assert gate.main(["example/ics-proxy", sha]) == 0
    with pytest.raises(SystemExit) as error:
        gate.main(["example/ics-proxy", "main"])
    assert error.value.code == 2


# Intent: when a check ran twice on a commit, the newest run decides, whatever order the API lists them in.
# Why: an older successful run cannot grant approval while a newer rerun is pending or has failed.
def test_the_newest_run_of_a_check_decides() -> None:
    skipped = {**_run("checks", conclusion="skipped"), "id": 1}
    passed = {**_run("checks"), "id": 2}
    pending = {**_run("checks", status="queued", conclusion=None), "id": 3}
    assert gate.evaluate([passed, skipped])[0] == "pass"
    assert gate.evaluate([pending, passed, skipped])[0] == "pending"
    assert gate.evaluate([{**skipped, "id": 4}, passed])[0] == "fail"
