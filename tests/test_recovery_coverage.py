"""Coverage declarations retain real source links, exact denominators and distinct safety/recovery obligations."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_qualification_evidence import project
from tests_model.catalog import FAMILIES
from tools import recovery_coverage, validate
from tools.qualification_snapshot import git

ROOT = Path(__file__).resolve().parents[1]


# Intent: every declared finite family and named focused risk must resolve to executable, source-pinned tests.
# Why: a matrix with dangling links, uncounted exclusions or a missing risk is not reviewable coverage evidence.
def test_coverage_registry_has_real_tests_and_exact_denominators():
    report = recovery_coverage.matrix()
    risks = {row["identifier"]: row for row in report["risks"]}
    assert len(risks) == len(report["risks"])
    for family in FAMILIES:
        path, function = family.test_function.split("::")
        assert "def " + function + "(" in (ROOT / path).read_text()
        assert set(family.risks) <= set(risks)
    for risk in risks.values():
        assert risk["safety"] and risk["recovery"] and risk["limits"] and risk["focused"]
        assert all((ROOT / path).is_file() for path in risk["focused"])
    assert report["execution_status"] == "declared only; no qualification inferred"


# Intent: failed validation and unavailable audit output preserve the previous report and remove private scratch.
# Why: incomplete evidence must not produce an attached qualification claim.
@pytest.mark.parametrize(
    ("returncode", "receipt_contents", "error"),
    [
        (1, None, subprocess.CalledProcessError),
        (0, None, FileNotFoundError),
        (0, "{", json.JSONDecodeError),
        (0, "{}", ValueError),
        (0, '{"commit": "synthetic", "source_digest": "synthetic", "phases": {}}', ValueError),
        (
            0,
            json.dumps(
                {
                    "commit": "synthetic",
                    "source_digest": "synthetic",
                    "phases": dict.fromkeys(recovery_coverage.QUALIFICATION_ROOTS, {}),
                }
            ),
            ValueError,
        ),
    ],
    ids=["failed-validation", "missing-receipt", "invalid-json", "missing-fields", "missing-suites", "missing-matrix"],
)
def test_coverage_rejects_unavailable_audit_output(tmp_path, monkeypatch, returncode, receipt_contents, error):
    output = tmp_path / "coverage.json"
    output.write_text("previous report")
    receipts = []

    # Simulate the subprocess exit and handoff states without launching a qualification run.
    def validator(command, *, cwd, check):
        receipt = Path(command[command.index("--qualification-audit-output") + 1])
        receipts.append(receipt)
        if receipt_contents is not None:
            receipt.write_text(receipt_contents)
        completed = subprocess.CompletedProcess(command, returncode)
        if check:
            completed.check_returncode()
        return completed

    monkeypatch.setattr(recovery_coverage.subprocess, "run", validator)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "recovery_coverage",
            "--output",
            str(output),
            "--qualification-report",
            str(tmp_path / "bundle.json"),
        ],
    )
    with pytest.raises(error):
        recovery_coverage.main()
    assert output.read_text() == "previous report"
    (receipt,) = receipts
    assert not receipt.parent.exists()


# Intent: qualify the matrix from the same clean commit as its execution evidence while HEAD changes.
# Why: a matrix read before validation or from the live checkout afterward can describe a different commit.
def test_coverage_cli_uses_the_audited_snapshot_when_head_moves(tmp_path, monkeypatch):
    catalog = "tests_model/catalog.py"
    repo = project(
        tmp_path / "repo",
        {
            "tools/__init__.py": "",
            "tools/recovery_coverage.py": (ROOT / "tools/recovery_coverage.py").read_text(),
            "tools/qualification_collection.py": (ROOT / "tools/qualification_collection.py").read_text(),
            "tests_model/__init__.py": "",
            "tests_model/coverage.py": "RISKS = ()\n",
            catalog: "FAMILIES = ()\nSCHEMA = 1\n",
        },
    )
    initial = git(repo, "rev-parse", "HEAD").decode().strip()
    (repo / catalog).write_text("FAMILIES = ()\nSCHEMA = 2\n")
    git(repo, "add", catalog)
    git(repo, "-c", "user.name=Offline Test", "-c", "user.email=offline@example.invalid", "commit", "-qm", "candidate")
    candidate = git(repo, "rev-parse", "HEAD").decode().strip()
    git(repo, "checkout", "--detach", "-q", initial)
    run = subprocess.run
    output = tmp_path / "coverage.json"
    bundle = tmp_path / "bundle.json"
    validator = str(repo / "tools/validate.py")
    audited_snapshots = []

    # Replace only the raw worker audit. Keep actual source capture and the frozen matrix subprocess.
    def audit(path, frozen, metadata, python, results, executors):
        assert path == bundle and metadata["commit"] == candidate
        assert (frozen / catalog).read_text() == "FAMILIES = ()\nSCHEMA = 2\n"
        git(repo, "checkout", "--detach", "-q", initial)
        audited_snapshots.append(frozen)
        results["synthetic raw audit"] = True
        return {}, dict.fromkeys(recovery_coverage.QUALIFICATION_ROOTS, {"synthetic": True})

    # Change HEAD at the validation boundary, and let the frozen matrix subprocess run normally.
    def launch(command, *args, **kwargs):
        if command[:3] != [sys.executable, "-B", validator]:
            return run(command, *args, **kwargs)
        git(repo, "checkout", "--detach", "-q", candidate)
        with monkeypatch.context() as child:
            child.chdir(repo)
            child.setattr(sys, "argv", command[2:])
            assert validate.main() == 0
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(recovery_coverage, "ROOT", repo)
    monkeypatch.setattr(recovery_coverage, "matrix", lambda: {"schema": 1})
    monkeypatch.setattr(validate, "ROOT", repo)
    monkeypatch.setattr(validate, "audit_qualification", audit)
    monkeypatch.setattr(subprocess, "run", launch)
    monkeypatch.setattr(
        sys, "argv", ["recovery_coverage", "--output", str(output), "--qualification-report", str(bundle)]
    )

    assert recovery_coverage.main() == 0
    report = json.loads(output.read_text())
    assert report["schema"] == 2 and report["qualification"]["commit"] == candidate
    assert git(repo, "rev-parse", "HEAD").decode().strip() == initial
    (frozen,) = audited_snapshots
    assert not frozen.parent.exists()
