# Modified for the standalone iCS proxy repository.
"""Check when the normal release gate runs and reuses the additional iCS tests."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools import ics_predeployment as checks
from tools import validate


# Commit a tiny source tree so the release gate must select real immutable candidate and predecessor bytes.
def commit(repo, name, content):
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    subprocess.run(["git", "add", name], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", name], cwd=repo, check=True)
    return checks.git(repo, "rev-parse", "HEAD").decode().strip()


# Supply successive commits where only the final one changes the proxy.
@pytest.fixture
def releases(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    for key, value in (("user.name", "Example"), ("user.email", "example@example.invalid")):
        subprocess.run(["git", "config", key, value], cwd=repo, check=True)
    commit(repo, "requirements-trial.txt", "websockets==15.0.1\n")
    installed = commit(repo, "tools/managed_proxy.py", "# installed\n")
    client = commit(repo, "custom_components/ics_proxy/sensor.py", "# client change\n")
    candidate = commit(repo, "tools/managed_proxy.py", "# candidate\n")
    return repo, installed, client, candidate


# Intent: decide on the week from shipped code, with a real older Git commit as the source for the simulator.
# Why: an HA-only commit does not change the standalone build, while a new proxy must use its actual predecessor.
def test_proxy_change_detection_and_export(tmp_path, releases):
    repo, installed, client, candidate = releases
    assert checks.changed_proxy(repo, client, installed) is False
    assert checks.changed_proxy(repo, candidate, installed) is True
    with pytest.raises(ValueError, match="full installed proxy commit"):
        checks.changed_proxy(repo, candidate, installed[:12])
    exported = tmp_path / "installed"
    checks.export_installed(repo, installed, exported)
    assert (exported / "tools/managed_proxy.py").read_text() == "# installed\n"


# Intent: changing only the standalone installation's dependency pin triggers the installation week.
# Why: the service installs from a different requirements file than the offline test environment.
def test_proxy_change_detection_includes_standalone_dependency_pins(releases):
    repo, _, _, old_pin = releases
    new_pin = commit(repo, "requirements-trial.txt", "websockets==16.0\n")
    assert checks.changed_proxy(repo, new_pin, old_pin) is True


# Intent: the unit shipped to standalone operators counts as a proxy release input.
def test_proxy_change_detection_includes_packaged_unit(releases):
    repo, _, _, installed = releases
    candidate = commit(repo, "packaging/ics-proxy.service.example", "[Service]\nTimeoutStopSec=45\n")
    assert checks.changed_proxy(repo, candidate, installed) is True


# Intent: every static command and the full audit retain the original candidate if HEAD moves during the run.
@pytest.mark.parametrize("candidate_passes", [True, False])
def test_release_checks_the_frozen_candidate_after_checkout_changes(tmp_path, monkeypatch, releases, candidate_passes):
    repo, installed, _, candidate = releases
    monkeypatch.setattr(validate, "ROOT", repo)
    snapshot = tmp_path / "validated"
    snapshot.mkdir()
    checked_sources = []
    commands = []

    def step(name, command, results, *, cwd=None, cache_dir=None):
        if not commands:
            subprocess.run(["git", "checkout", "--detach", "-q", installed], cwd=repo, check=True)
        commands.append(command)
        if command[0] == "git":
            assert command == ["git", "show", "--check", "--format=", candidate]
        else:
            assert cwd == snapshot / "source"
            assert (cwd / "tools/managed_proxy.py").read_text() == "# candidate\n"
            assert cache_dir != cwd and cache_dir is not None
            checked_sources.append(cwd)
        results[name] = candidate_passes or name != "ruff check"

    def audit(path, source, metadata, python, results, executors):
        assert source == checked_sources[0]
        assert metadata["commit"] == candidate
        assert (repo / "tools/managed_proxy.py").read_text() == "# installed\n"
        results["three suites"] = True

    monkeypatch.setattr(validate, "run_step", step)
    monkeypatch.setattr(validate, "audit_qualification", audit)
    monkeypatch.setattr(checks, "ensure_check", lambda *args: None)
    results = {}
    validate.run_full_qualification(
        tmp_path / "report.json",
        sys.executable,
        results,
        validated_snapshot=snapshot,
        deployment=validate.Deployment("ics-client", None, None),
    )
    assert len(checked_sources) == 7
    assert results["three suites"] and all(results.values()) is candidate_passes
    receipt = snapshot / "snapshot.json"
    assert receipt.exists() is candidate_passes
    if candidate_passes:
        assert json.loads(receipt.read_text())["commit"] == candidate


# Intent: a full tests-only audit can pass independently, and the CLI refuses a publication request in this mode.
def test_tests_only_audits_frozen_source_without_publishing(tmp_path, monkeypatch, releases):
    repo, _, _, candidate = releases
    monkeypatch.setattr(validate, "ROOT", repo)
    audits = []

    def audit(path, source, metadata, python, results, executors):
        assert source != repo
        assert metadata["commit"] == candidate
        assert (source / "tools/managed_proxy.py").read_text() == "# candidate\n"
        audits.append(metadata["commit"])
        results["three suites"] = True

    def unexpected_static(*args, **kwargs):
        pytest.fail("tests-only mode ran static checks")

    monkeypatch.setattr(validate, "run_static_checks", unexpected_static)
    monkeypatch.setattr(validate, "audit_qualification", audit)
    arguments = [
        "validate",
        "--tests-only",
        "--qualification-report",
        str(tmp_path / "report.json"),
        "--deployment-target",
        "qualification-only",
    ]
    monkeypatch.setattr(sys, "argv", arguments)
    assert validate.main() == 0
    assert audits == [candidate]

    snapshot = tmp_path / "validated"
    monkeypatch.setattr(sys, "argv", [*arguments, "--validated-snapshot", str(snapshot)])
    with pytest.raises(SystemExit) as error:
        validate.main()
    assert error.value.code == 2
    assert audits == [candidate] and not snapshot.exists()


# Intent: exact scoped release checks precede publication of an approved source snapshot.
# Why: a failed week must stop the actual packaging path, even when the normal three suites have already passed.
@pytest.mark.parametrize(
    ("target", "same_proxy", "week_fails", "pin_matches", "expected"),
    [
        ("qualification-only", False, False, True, ()),
        ("ics-client", False, False, True, (checks.DISCOVERY,)),
        ("ics-proxy", True, False, True, (checks.DISCOVERY,)),
        ("ics-proxy", False, False, True, (checks.DISCOVERY, checks.WEEK)),
        ("ics-proxy", False, True, True, (checks.DISCOVERY, checks.WEEK)),
        ("ics-proxy", False, False, False, ()),
    ],
)
def test_release_snapshot_waits_for_required_checks(
    tmp_path, monkeypatch, releases, target, same_proxy, week_fails, pin_matches, expected
):
    repo, installed, _, candidate = releases
    monkeypatch.setattr(validate, "ROOT", repo)
    snapshot = tmp_path / "validated"
    snapshot.mkdir()
    calls = []

    # This test covers focused iCS checks after static checks and audits. It still freezes the real commit.
    def audit(path, source, metadata, python, results, executors):
        assert metadata["commit"] == candidate
        results["three suites"] = True
        return {"websockets": "15.0.1" if pin_matches else "16.0"}

    # Capture the command boundary before the release process can mark a snapshot as approved.
    def invoke(check, source, output, metadata, python, *, installed=None):
        assert not (snapshot / "snapshot.json").exists()
        assert source == snapshot / "source" and metadata["commit"] == candidate
        assert (source / "tools/managed_proxy.py").read_text() == "# candidate\n"
        if installed is not None:
            assert installed[0] == releases[1]
            assert (installed[1] / "tools/managed_proxy.py").read_text() == "# installed\n"
        calls.append(check)
        if week_fails and check == checks.WEEK:
            raise ValueError("synthetic failed week")

    monkeypatch.setattr(validate, "run_static_checks", lambda *args, **kwargs: None)
    monkeypatch.setattr(validate, "audit_qualification", audit)
    monkeypatch.setattr(checks, "ensure_check", invoke)
    results = {}
    validate.run_full_qualification(
        tmp_path / "report.json",
        sys.executable,
        results,
        validated_snapshot=snapshot,
        deployment=validate.Deployment(target, candidate if same_proxy else installed, Path(sys.executable)),
    )
    assert tuple(calls) == expected
    passed = not week_fails and pin_matches
    assert (snapshot / "snapshot.json").exists() is passed
    assert all(results.values()) is passed


# Produce synthetic boundary observations with the same public evidence fields as the discovery simulator.
def discovery_summary(orders):
    runs = []
    for order in orders:
        suffix = ["settle_lifecycle", "later_charge"]
        observations = []
        revision, pause, replied = 1, "original", False
        for event in ["prefix", *order, *suffix]:
            if event in ("pause", "newer_pause"):
                revision += 1
                pause = event
            replied |= event == "reply"
            later = event == "later_charge"
            receipts = {"original": "completed", "start": "running"}
            if pause != "original":
                receipts[pause] = "accepted"
            if event in suffix:
                receipts.update({"start": "completed", pause: "completed"})
            if later:
                receipts["final"] = "completed"
            observations.append(
                {
                    "event": event,
                    "intent": {"mode": "app" if later else "paused", "revision": revision + int(later)},
                    "expected_pause_revision": revision,
                    "old_start_stage": "accepted" if replied else "issued",
                    "old_reply_seen": replied,
                    "start_commands_seen": 1,
                    "receipts": receipts,
                }
            )
        runs.append({"purpose": "search", "status": "passed", "events": order, "observations": observations})
    return {
        "schema_version": 1,
        "status": "passed",
        "seed": 19,
        "planned": 20,
        "observed_events": sorted({row["event"] for run in runs for row in run["observations"]}),
        "runs": runs,
    }


# Intent: require evidence for each exact history even when a cache agrees with a changed artifact's hash.
@pytest.mark.parametrize("damage", ["placeholder", "duplicate", "missing_observation", "incomplete_return"])
def test_reusing_and_rechecking_discovery_results(tmp_path, monkeypatch, damage):
    attempts = []
    source = Path(__file__).resolve().parents[1]
    orders = checks.discovery_orders(sys.executable, source)

    # Write the exact raw artifacts in miniature so the cache treats them the same way as actual child output.
    def run(check, python, source, attempt, previous):
        attempts.append(attempt)
        path = attempt / "discovery"
        path.mkdir()
        (attempt / "pytest.xml").write_text(
            '<testsuite><testcase classname="tests_discovery.test_recovery_discovery" '
            'name="test_recovery_discovery"/></testsuite>'
        )
        (path / "summary.json").write_text(json.dumps(discovery_summary(orders)))

    monkeypatch.setattr(checks, "runtime_identity", lambda python, source: {"python": "fixture"})
    monkeypatch.setattr(checks, "run_check", run)
    args = (checks.DISCOVERY, source, tmp_path, {"commit": "a", "source_digest": "b"}, sys.executable)
    checks.ensure_check(*args)
    checks.ensure_check(*args)
    assert len(attempts) == 1
    path = attempts[0] / "discovery/summary.json"
    summary = json.loads(path.read_text())
    if damage == "placeholder":
        summary["runs"][0] = {"purpose": "search", "status": "passed"}
    elif damage == "duplicate":
        summary["runs"][0] = summary["runs"][1]
    elif damage == "missing_observation":
        summary["runs"][0]["observations"].pop(1)
    else:
        summary["runs"][0]["observations"][-1]["receipts"]["final"] = "accepted"
    path.write_text(json.dumps(summary))
    receipt_path = attempts[0].parent / "passed.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["evidence"]["discovery/summary.json"] = hashlib.sha256(path.read_bytes()).hexdigest()
    receipt_path.write_text(json.dumps(receipt))
    checks.ensure_check(*args)
    assert len(attempts) == 2
