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
from tools.managed_reporting import software


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


# Intent: the interpreter that runs the installation week has every candidate standalone pin before it can pass.
# Why: the old build's pins and a matching Home Assistant version cannot verify the candidate runtime by themselves.
@pytest.mark.parametrize(
    ("ha_version", "websockets_version", "extra_version", "passes"),
    [
        ("2026.10.4", "16.0", "2", True),
        ("2026.11.0", "16.0", "2", True),
        ("2026.11.0", "15.0.1", "2", False),
        ("2026.11.0", "16.0", None, False),
    ],
)
def test_installation_week_requires_candidate_standalone_pins(
    tmp_path, monkeypatch, ha_version, websockets_version, extra_version, passes
):
    source = tmp_path / "candidate"
    source.mkdir()
    (source / "requirements-trial.txt").write_text("websockets==16.0\nfixture_extra==2\n")
    (source / "requirements-ha-proxy.txt").write_text("homeassistant==2026.11.0\n")
    (source / "requirements-ha-proxy-2026.10.txt").write_text("homeassistant==2026.10.4\n")
    previous = tmp_path / "installed"
    previous.mkdir()
    (previous / "requirements-trial.txt").write_text("websockets==15.0.1\n")
    packages = [["homeassistant", ha_version], ["WebSockets", websockets_version]]
    if extra_version is not None:
        packages.append(["fixture.extra", extra_version])
    monkeypatch.setattr(checks, "runtime_identity", lambda python, source: {"packages": packages})
    attempts = []

    # Retain one synthetic passing test report only when the candidate's pins admit the interpreter.
    def run(check, python, source, attempt, installed):
        assert installed == previous
        attempts.append(attempt)
        classname, name = check.case.rsplit(".", 1)
        (attempt / "pytest.xml").write_text(f'<testsuite><testcase classname="{classname}" name="{name}"/></testsuite>')

    monkeypatch.setattr(checks, "run_check", run)
    output = tmp_path / "evidence"
    args = (checks.WEEK, source, output, {"commit": "a", "source_digest": "b"}, sys.executable)
    if passes:
        checks.ensure_check(*args, installed=("previous", previous))
        checks.ensure_check(*args, installed=("previous", previous))
        assert len(attempts) == 1
        assert (attempts[0].parent / "passed.json").is_file()
    else:
        with pytest.raises(ValueError, match="standalone runtime pins differ from the installation week runtime"):
            checks.ensure_check(*args, installed=("previous", previous))
        assert not attempts and not output.exists()


# Intent: the unit shipped to standalone operators counts as a proxy release input.
# Why: a unit-only change can affect how the daemon starts or shuts down in an installation.
def test_proxy_change_detection_includes_packaged_unit(releases):
    repo, _, _, installed = releases
    candidate = commit(repo, "packaging/ics-proxy.service.example", "[Service]\nTimeoutStopSec=45\n")
    assert checks.changed_proxy(repo, candidate, installed) is True


# Intent: include changes to the tools initializer and Stop incident reader in release rehearsal and fingerprinting.
# Why: both are imported by the installed service and its diagnostic command.
@pytest.mark.parametrize("runtime_input", ["__init__.py", "managed_stop_incident.py"])
def test_proxy_release_and_fingerprint_include_imported_tools(releases, runtime_input):
    repo, _, _, _ = releases
    tools = ("__init__.py", "managed_reporting.py", "managed_incident.py", "managed_stop_incident.py", "trial_proxy.py")
    for name in tools:
        if name != runtime_input:
            commit(repo, f"tools/{name}", "# runtime helper\n")
    installed = commit(repo, f"tools/{runtime_input}", "REVISION = 1\n")
    before = software(repo)["proxy_source_sha256"]

    candidate = commit(repo, f"tools/{runtime_input}", "REVISION = 2\n")
    after = software(repo)["proxy_source_sha256"]
    assert checks.changed_proxy(repo, candidate, installed)
    assert before is not None and after is not None and before != after


# Intent: every static command and the full audit retain the original candidate if HEAD moves during the run.
# Why: a published qualification receipt must describe the same source that passed its checks.
@pytest.mark.parametrize("candidate_passes", [True, False])
def test_release_checks_the_frozen_candidate_after_checkout_changes(tmp_path, monkeypatch, releases, candidate_passes):
    repo, installed, _, candidate = releases
    monkeypatch.setattr(validate, "ROOT", repo)
    snapshot = tmp_path / "validated"
    snapshot.mkdir()
    checked_sources = []
    commands = []

    # Move the checkout on the first check and verify which source each subsequent check sees.
    def step(name, command, results, *, cwd=None, cache_dir=None):
        if not commands:
            subprocess.run(["git", "checkout", "--detach", "-q", installed], cwd=repo, check=True)
        commands.append(command)
        if command[0] == "git":
            assert command == ["git", "show", "--check", "--format=", candidate]
        else:
            assert cwd.name == "source" and cwd.parent.parent == snapshot
            assert not (snapshot / "source").exists()
            assert (cwd / "tools/managed_proxy.py").read_text() == "# candidate\n"
            assert cache_dir != cwd and cache_dir is not None
            checked_sources.append(cwd)
        results[name] = candidate_passes or name != "ruff check"

    # Confirm the audit still receives the candidate while the checkout contains the installed build.
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
        assert (snapshot / "source/tools/managed_proxy.py").read_text() == "# candidate\n"
    else:
        assert list(snapshot.iterdir()) == []


# Intent: a full tests-only audit can pass independently, and the CLI refuses a publication request in this mode.
# Why: an evidence audit can verify the recorded suites, while publication also requires static validation.
def test_tests_only_audits_frozen_source_without_publishing(tmp_path, monkeypatch, releases):
    repo, _, _, candidate = releases
    monkeypatch.setattr(validate, "ROOT", repo)
    audits = []

    # Record which frozen source the tests-only path audits.
    def audit(path, source, metadata, python, results, executors):
        assert source != repo
        assert metadata["commit"] == candidate
        assert (source / "tools/managed_proxy.py").read_text() == "# candidate\n"
        audits.append(metadata["commit"])
        results["three suites"] = True

    # Detect any unexpected static check in the tests-only path.
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
        assert not (snapshot / "source").exists()
        assert source.name == "source" and source.parent.parent == snapshot and metadata["commit"] == candidate
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


# Intent: an unsuccessful release leaves the requested output available for retry with the same exact candidate.
# Why: a failed or repeated attempt must preserve any files the operator already kept in the destination.
def test_failed_validation_can_retry_the_same_snapshot_directory(tmp_path, monkeypatch, releases):
    repo, _, _, candidate = releases
    monkeypatch.setattr(validate, "ROOT", repo)
    monkeypatch.setattr(validate, "run_static_checks", lambda *args, **kwargs: None)
    monkeypatch.setattr(checks, "ensure_check", lambda *args, **kwargs: None)
    fail = True

    # Refuse the first qualification and accept later attempts for the same frozen commit.
    def audit(path, source, metadata, python, results, executors):
        assert metadata["commit"] == candidate
        if fail:
            raise ValueError("synthetic failed audit")
        results["three suites"] = True

    monkeypatch.setattr(validate, "audit_qualification", audit)
    snapshot = tmp_path / "target"
    snapshot.mkdir()
    unrelated = snapshot / "notes.txt"
    unrelated.write_text("operator notes\n")

    # Run each attempt with fresh results and the same publication destination.
    def run():
        results = {}
        validate.run_full_qualification(
            tmp_path / "report.json",
            sys.executable,
            results,
            validated_snapshot=snapshot,
            deployment=validate.Deployment("ics-client", None, None),
        )
        return all(results.values())

    assert not run()
    assert list(snapshot.iterdir()) == [unrelated]
    fail = False
    assert run()
    approved = (snapshot / "snapshot.json").read_bytes()
    assert json.loads(approved)["commit"] == candidate
    assert not run()
    assert (snapshot / "snapshot.json").read_bytes() == approved
    assert (snapshot / "source/tools/managed_proxy.py").read_text() == "# candidate\n"
    assert unrelated.read_text() == "operator notes\n"


# Intent: publication can roll back its staged source when an older or concurrent receipt owns the output name.
# Why: a source snapshot and its receipt must remain paired when another publication already owns the receipt path.
def test_snapshot_publication_preserves_a_preexisting_receipt(tmp_path):
    source = tmp_path / "staging/source"
    source.mkdir(parents=True)
    (source / "candidate").write_text("candidate\n")
    destination = tmp_path / "validated"
    destination.mkdir()
    receipt = destination / "snapshot.json"
    receipt.write_text("original receipt\n")
    with pytest.raises(FileExistsError):
        validate.publish_snapshot(source, destination, {"commit": "candidate"})
    assert (source / "candidate").read_text() == "candidate\n"
    assert list(destination.iterdir()) == [receipt]
    assert receipt.read_text() == "original receipt\n"


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
# Why: a matching hash alone only identifies the cached bytes. The audit also checks the observations they contain.
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
