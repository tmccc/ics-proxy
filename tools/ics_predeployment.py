# Modified for the standalone iCS proxy repository.
"""Run the extra iCS release checks on committed source and retain their test results."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import signal
import subprocess
import tarfile
import tempfile
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from importlib.metadata import DistributionFinder, MetadataPathFinder
from pathlib import Path
from xml.etree import ElementTree

from tools.qualification_snapshot import git

PROXY_INPUTS = (
    "ics_proxy_core",
    "tools/__init__.py",
    "tools/managed_proxy.py",
    "tools/managed_reporting.py",
    "tools/managed_incident.py",
    "tools/managed_stop_incident.py",
    "tools/trial_proxy.py",
    "requirements-offline.txt",
    "requirements-trial.txt",
    "packaging/ics-proxy.service.example",
)
DISCOVERY_SEED = 19
DISCOVERY_CASES = 20


@dataclass(frozen=True)
class Check:
    """One offline command and the exact test it must complete."""

    name: str
    runner: str
    directory: str
    case: str
    seconds: int


DISCOVERY = Check(
    "recovery search",
    "offline_validate.py",
    "tests_discovery",
    "tests_discovery.test_recovery_discovery.test_recovery_discovery",
    300,
)
WEEK = Check(
    "installation week",
    "offline_ha_validate.py",
    "tests_installation",
    "tests_installation.test_installation_week.test_week_on_migrated_installation",
    120,
)


# Require the full installed identity, and schedule the week when shipped proxy bytes or pins differ.
def changed_proxy(repo: Path, candidate: str, installed: str | None) -> bool:
    if not installed or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", installed):
        raise ValueError("provide the verified full installed proxy commit for standalone release validation")
    if git(repo, "rev-parse", "--verify", installed + "^{commit}").decode().strip() != installed:
        raise ValueError("installed proxy commit does not identify committed source in this repository")
    return bool(git(repo, "diff", "--name-only", installed, candidate, "--", *PROXY_INPUTS).strip())


# Compare distribution names using the same normalization for committed pins and installed metadata.
def normalized_package_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.strip().lower())


# Require all candidate standalone pins in each interpreter used to exercise the service.
def require_standalone_pins(source: Path, packages: Iterable[tuple[str, str]], *, runtime: str) -> None:
    expected = [
        line.split("==", 1)
        for line in (source / "requirements-trial.txt").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    installed: dict[str, set[str]] = defaultdict(set)
    for name, version in packages:
        installed[normalized_package_name(name)].add(version)
    if not expected or any(installed[normalized_package_name(name)] != {version.strip()} for name, version in expected):
        raise ValueError(f"standalone runtime pins differ from the {runtime}")


# Export the exact installed commit without opening its running installation or checking out another branch.
def export_installed(repo: Path, commit: str, destination: Path) -> None:
    archive = git(repo, "archive", "--format=tar", commit)
    with tarfile.open(fileobj=io.BytesIO(archive)) as package:
        package.extractall(destination, filter="data")


# Use the selected virtualenv as invoked and reject leftover pytest filters and Python overrides.
def clean_env() -> dict[str, str]:
    return {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("PYTEST_", "PYTHON")) and k not in ("MODEL_WORKER_INDEX", "OCPP_WEEK_COMMISSIONING_ROOT")
    }


# Bind installed importable files to a receipt, including dependencies with unchanged metadata versions.
def runtime_content_digest(import_paths: list[str], runtime_roots: list[str]) -> str:
    paths = [Path(location) for location in import_paths]
    roots = [Path(location) for location in runtime_roots]
    for path in paths:
        resolved = path.resolve()
        if not any(path.is_relative_to(root) for root in roots) or not any(
            resolved.is_relative_to(root) for root in roots
        ):
            raise ValueError(f"unrecognized Python runtime import location: {path}")

    digest = hashlib.sha256(json.dumps(import_paths).encode())

    # An unreadable directory leaves the interpreter's available imports unknown.
    def raise_walk_error(error: OSError) -> None:
        raise error

    for root in sorted(set(paths)):
        if any(parent in paths for parent in root.parents):
            continue
        digest.update(os.fsencode(root) + b"\0")
        if root.is_dir():
            digest.update(b"directory\0")
            for parent, dirs, files in root.walk(on_error=raise_walk_error):
                digest.update(os.fsencode(parent.relative_to(root)) + b"\0")
                dirs[:] = sorted(name for name in dirs if name != "__pycache__")
                for name in sorted(files):
                    path = parent / name
                    if parent in paths and (name.endswith(".egg-link") or name.startswith("__editable__")):
                        raise ValueError(f"editable Python installs are unsupported by release checks: {path}")
                    target = path
                    digest.update(os.fsencode(path.relative_to(root)) + b"\0")
                    if path.is_symlink():
                        digest.update(b"link\0" + os.fsencode(os.readlink(path)) + b"\0")
                        target = path.resolve()
                        bases = paths if target.is_dir() else roots
                        if not any(target.is_relative_to(base) for base in bases):
                            raise ValueError(f"unrecognized Python runtime link: {path}")
                    if target.is_file():
                        with target.open("rb") as stream:
                            digest.update(b"file\0" + hashlib.file_digest(stream, "sha256").digest())
        elif root.is_file():
            with root.open("rb") as stream:
                digest.update(b"file\0" + hashlib.file_digest(stream, "sha256").digest())
        else:
            digest.update(b"missing\0")
    return digest.hexdigest()


# Bind cached results to the exact interpreter, packages and runtime files used by the release test.
def runtime_identity(python: str, source: Path, *, homeassistant: bool = False) -> dict:
    code = """\
import hashlib
import json
import site
import sys
import sysconfig
from pathlib import Path

stdlib = Path(sysconfig.get_path('stdlib'))
archive = stdlib.with_name(f'python{sys.version_info.major}{sys.version_info.minor}.zip')
roots = [stdlib, archive, *map(Path, site.getsitepackages()), Path(site.getusersitepackages())]
with Path(sys.executable).open('rb') as stream:
    executable_sha256 = hashlib.file_digest(stream, 'sha256').hexdigest()
print(json.dumps({
    'python': sys.version,
    'executable': sys.executable,
    'executable_sha256': executable_sha256,
    'import_paths': sys.path,
    'runtime_roots': list(map(str, roots)),
}))
"""
    with tempfile.TemporaryDirectory(prefix="release-runtime-") as cache:
        command = [python, "-E", "-P", "-B", "-X", f"pycache_prefix={cache}"]
        env = clean_env()
        identity = json.loads(subprocess.check_output([*command, "-c", code], cwd=source, env=env, timeout=20))
        paths = identity.pop("import_paths")
        identity["runtime_sha256"] = runtime_content_digest(paths, identity.pop("runtime_roots"))
        packages = []
        for distribution in MetadataPathFinder.find_distributions(DistributionFinder.Context(path=paths)):
            fields = distribution.metadata
            name = fields.get("Name")
            if (record := distribution.read_text("direct_url.json")) is not None:
                origin = json.loads(record)
                if not isinstance(origin, dict):
                    raise ValueError(f"invalid Python installation metadata: {name}")
                directory = origin.get("dir_info")
                if isinstance(directory, dict) and directory.get("editable") is True:
                    raise ValueError(f"editable Python installs are unsupported by release checks: {name}")
            if name and (version := fields.get("Version")):
                packages.append([name.lower(), version])
        identity["packages"] = sorted(packages)
        if homeassistant:
            identity["homeassistant_version"] = subprocess.check_output(
                [*command, str(source / "tools" / WEEK.runner), "--print-ha-version"],
                cwd=source,
                env=env,
                timeout=20,
                text=True,
            ).strip()
    return identity


# Regenerate the search orders from the frozen candidate using the same interpreter as the search.
def discovery_orders(python: str, source: Path) -> list[list[str]]:
    code = (
        "import json, runpy, sys; "
        "select = runpy.run_path('tests_discovery/schedules.py')['selected_orders']; "
        "print(json.dumps(select(int(sys.argv[1]), int(sys.argv[2]))))"
    )
    with tempfile.TemporaryDirectory(prefix="release-discovery-") as cache:
        command = [python, "-I", "-B", "-X", f"pycache_prefix={cache}", "-c", code]
        command += [str(DISCOVERY_SEED), str(DISCOVERY_CASES)]
        orders = json.loads(subprocess.check_output(command, cwd=source, env=clean_env(), timeout=20))
    if (
        not isinstance(orders, list)
        or len(orders) != DISCOVERY_CASES
        or not all(isinstance(order, list) and all(isinstance(event, str) for event in order) for order in orders)
        or len({tuple(order) for order in orders}) != DISCOVERY_CASES
    ):
        raise ValueError("recovery search did not generate all distinct expected histories")
    return orders


# A passed history must record each boundary and both closure and subsequent charge evidence.
def verified_observations(order: list[str], observations: object) -> bool:
    events = ["prefix", *order, "settle_lifecycle", "later_charge"]
    if (
        not isinstance(observations, list)
        or not all(isinstance(row, dict) for row in observations)
        or [row.get("event") for row in observations] != events
    ):
        return False
    revision = observations[0].get("expected_pause_revision")
    if type(revision) is not int or revision < 0:
        return False
    pause, reply_seen = "original", False
    for event, row in zip(events, observations, strict=True):
        if event in ("pause", "newer_pause"):
            revision += 1
            pause = event
        reply_seen = reply_seen or event == "reply"
        later_charge = event == "later_charge"
        settled = event in ("settle_lifecycle", "later_charge")
        pause_state = "completed" if settled or pause == "original" else "accepted"
        receipts = row.get("receipts")
        intent = row.get("intent")
        if (
            type(row.get("expected_pause_revision")) is not int
            or row["expected_pause_revision"] != revision
            or not isinstance(intent, dict)
            or type(intent.get("revision")) is not int
            or intent != {"mode": "app" if later_charge else "paused", "revision": revision + int(later_charge)}
            or row.get("old_reply_seen") is not reply_seen
            or row.get("old_start_stage") != ("accepted" if reply_seen else "issued")
            or type(row.get("start_commands_seen")) is not int
            or row["start_commands_seen"] != 1
            or not isinstance(receipts, dict)
            or receipts.get("original") != "completed"
            or receipts.get(pause) != pause_state
        ):
            return False
        if settled:
            if receipts.get("start") != "completed":
                return False
        elif receipts.get("start") not in ("accepted", "running", "failed", "uncertain"):
            return False
        if later_charge and receipts.get("final") != "completed":
            return False
    return True


# Confirm the expected pytest case and independently audit discovery's recorded histories.
def evidence(check: Check, attempt: Path, orders: list[list[str]] | None) -> dict[str, str]:
    xml = attempt / "pytest.xml"
    cases = ElementTree.parse(xml).findall(".//testcase")
    if (
        len(cases) != 1
        or cases[0].get("classname", "") + "." + cases[0].get("name", "") != check.case
        or any(cases[0].find(name) is not None for name in ("skipped", "failure", "error"))
    ):
        raise ValueError(f"{check.name} did not pass the required test")
    artifacts = [xml]
    if check == DISCOVERY:
        path = attempt / "discovery" / "summary.json"
        summary = json.loads(path.read_text())
        if (
            orders is None
            or not isinstance(summary, dict)
            or type(summary.get("schema_version")) is not int
            or summary.get("schema_version") != 1
            or summary.get("status") != "passed"
            or summary.get("seed") != DISCOVERY_SEED
            or summary.get("planned") != DISCOVERY_CASES
            or not isinstance(runs := summary.get("runs"), list)
            or len(runs) != len(orders)
            or not all(
                isinstance(run, dict)
                and run.get("purpose") == "search"
                and run.get("status") == "passed"
                and run.get("events") == order
                and verified_observations(order, run.get("observations"))
                for order, run in zip(orders, runs, strict=True)
            )
            or summary.get("observed_events") != sorted({row["event"] for run in runs for row in run["observations"]})
        ):
            raise ValueError("recovery search evidence does not match every expected history and observation")
        artifacts.append(path)
    return {str(path.relative_to(attempt)): hashlib.sha256(path.read_bytes()).hexdigest() for path in artifacts}


# Retain failures as well as successes, and stop child processes if a check exhausts its time.
def run_check(
    check: Check,
    python: str,
    source: Path,
    attempt: Path,
    previous: Path | None,
    *,
    ha_version: str | None = None,
) -> None:
    scratch = attempt / "scratch"
    scratch.mkdir()
    cache = scratch.resolve() / "python-bytecode"
    env = clean_env() | {"TMPDIR": str(scratch), "PYTHONPYCACHEPREFIX": str(cache)}
    command = [python, "-E", "-B"]
    if check == WEEK:
        assert ha_version is not None
        command.append("-P")
    command += [
        "-X",
        f"pycache_prefix={cache}",
        str(source / "tools" / check.runner),
    ]
    if ha_version is not None:
        command.append(f"--expected-ha-version={ha_version}")
    command += [check.directory, "--junitxml=" + str(attempt / "pytest.xml")]
    if check == DISCOVERY:
        command += [
            "--discovery-output=" + str(attempt / "discovery"),
            f"--discovery-seed={DISCOVERY_SEED}",
            f"--discovery-cases={DISCOVERY_CASES}",
        ]
    else:
        assert previous is not None
        env["OCPP_WEEK_COMMISSIONING_ROOT"] = str(previous)
        command += [f"--week-current={source}", f"--week-previous={previous}"]
    log = attempt / "test.log"
    with (
        log.open("w") as output,
        subprocess.Popen(
            command, cwd=source, env=env, stdout=output, stderr=subprocess.STDOUT, start_new_session=True
        ) as process,
    ):
        try:
            code = process.wait(timeout=check.seconds)
        except subprocess.TimeoutExpired, KeyboardInterrupt:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            raise
    if code:
        raise ValueError(f"{check.name} failed. See {log}")


# A matching successful run can be reused only while its required test evidence still verifies.
def passed_attempt(check: Check, directory: Path, inputs: dict, orders: list[list[str]] | None) -> Path | None:
    try:
        receipt = json.loads((directory / "passed.json").read_text())
        name = receipt["attempt"]
        if receipt["inputs"] != inputs or not re.fullmatch(r"attempt-[a-z0-9_]+", name):
            return None
        attempt = directory / name
        return attempt if receipt["evidence"] == evidence(check, attempt, orders) else None
    except OSError, ValueError, TypeError, KeyError, ElementTree.ParseError:
        return None


# Run each check once for matching source, installed build and runtime within a qualification bundle.
def ensure_check(
    check: Check,
    source: Path,
    output: Path,
    metadata: dict,
    python: str,
    *,
    installed: tuple[str, Path] | None = None,
) -> None:
    import fcntl

    homeassistant = check == WEEK
    runtime = runtime_identity(python, source, homeassistant=homeassistant)
    orders = discovery_orders(python, source) if check == DISCOVERY else None
    ha_version = None
    if check == WEEK:
        pins = (
            "requirements-ha-proxy.txt",
            "requirements-ha-proxy-2026.10.txt",
        )
        versions = {
            line.removeprefix("homeassistant==")
            for pin in pins
            for line in (source / pin).read_text().splitlines()
            if line.startswith("homeassistant==")
        }
        ha_version = runtime["homeassistant_version"]
        if ha_version not in versions:
            raise ValueError("installation week requires a pinned Home Assistant runtime")
        require_standalone_pins(source, runtime["packages"], runtime="installation week runtime")
    inputs = {
        "check": check.name,
        "commit": metadata["commit"],
        "source_digest": metadata["source_digest"],
        "runtime": runtime,
        "installed": installed[0] if installed is not None else None,
    }
    key = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
    directory = output / key
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "run.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if attempt := passed_attempt(check, directory, inputs, orders):
            if runtime_identity(python, source, homeassistant=homeassistant) != runtime:
                raise ValueError(f"{check.name} runtime changed during verification")
            print(f"{check.name}: reused passing results at {attempt}", flush=True)
            return
        attempt = Path(tempfile.mkdtemp(dir=directory, prefix="attempt-"))
        print(f"{check.name}: running on {inputs['commit']} (log: {attempt / 'test.log'})", flush=True)
        previous = installed[1] if installed is not None else None
        run_check(check, python, source, attempt, previous, ha_version=ha_version)
        receipt = {"inputs": inputs, "attempt": attempt.name, "evidence": evidence(check, attempt, orders)}
        if runtime_identity(python, source, homeassistant=homeassistant) != runtime:
            raise ValueError(f"{check.name} runtime changed during verification")
        pending = directory / "passed.new"
        pending.write_text(json.dumps(receipt, indent=2) + "\n")
        pending.replace(directory / "passed.json")
