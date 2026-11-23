#!/usr/bin/env python3
# Modified for the standalone iCS proxy repository.
"""Run static checks and re-audit complete 20-worker qualification, or run an explicit targeted test selection.

    python tools/validate.py --qualification-report /absolute/path/bundle.json
    python tools/validate.py --quick    # skip pytest
    python tools/validate.py --tests-only -k issue171   # targeted only, not full qualification

On Windows tests that fail for environment reasons only (for example CRLF checkout changes to fixture digests or
the bash-based search tool tests) are listed in ``tools/known_env_failures.txt`` and ignored there; any other
failure fails the run. The list is empty at the moment. On Linux/macOS the baseline is not applied.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import ics_predeployment  # noqa: E402
from tools.qualification_snapshot import freeze  # noqa: E402

BASELINE_FILE = ROOT / "tools" / "known_env_failures.txt"
# Keep in sync with the "Format check" and "Type check" steps of .github/workflows/checks.yml.
FORMAT_TARGETS = ["ics_proxy_core", "custom_components"]
MYPY_TARGETS = ["ics_proxy_core", "custom_components/ics_proxy/api.py"]


@dataclass(frozen=True)
class Deployment:
    """Identify the rollout so that only the relevant iCS checks are required."""

    target: str
    installed: str | None
    ha_python: Path | None


# Pick the repository virtualenv interpreter for the current OS, falling back to the running Python.
# Tools must run the same ruff/pytest versions the project is developed with.
def venv_python() -> str:
    for candidate in (ROOT / ".venv" / "Scripts" / "python.exe", ROOT / ".venv" / "bin" / "python"):
        if candidate.exists():
            return str(candidate)
    return sys.executable


# Read the baseline of environment-only failing test ids (prefix match, comments allowed).
# Windows checkouts fail a handful of tests for reasons unrelated to the change under test.
def load_baseline(path: Path = BASELINE_FILE) -> list[str]:
    if not path.exists():
        return []
    lines = (line.strip() for line in path.read_text(encoding="utf-8").splitlines())
    return [line for line in lines if line and not line.startswith("#")]


# Split failing test ids into known-environment and new failures.
# Only new failures should fail a local run.
def classify_failures(failures: list[str], baseline: list[str]) -> tuple[list[str], list[str]]:
    known = [item for item in failures if any(item.startswith(prefix) for prefix in baseline)]
    return known, [item for item in failures if item not in known]


# Extract failing node ids from pytest's short summary.
# The baseline comparison needs stable ids, not the raw log.
def parse_failures(output: str) -> list[str]:
    return [match.group(1).strip() for match in re.finditer(r"^(?:FAILED|ERROR) (\S+)", output, re.MULTILINE)]


# A known assertion failure may be waived on Windows; collection, usage and internal errors never become success.
def pytest_succeeded(completed: subprocess.CompletedProcess[str], known: list[str], new: list[str]) -> bool:
    if completed.returncode == 0:
        return True
    output = completed.stdout + completed.stderr
    return completed.returncode == 1 and bool(known) and not new and not re.search(r"^ERROR ", output, re.MULTILINE)


# Run one named step and report pass or fail without aborting the remaining steps.
# A single run should list every problem, not only the first.
def run_step(
    name: str,
    command: list[str],
    results: dict[str, bool],
    *,
    cwd: Path | None = None,
    cache_dir: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    print(f"\n== {name}")
    env = dict(os.environ)
    tmp = cache_dir or (Path(env["TMPDIR"]) if env.get("TMPDIR") else None)
    if tmp is not None:
        for variable, child in (
            ("MYPY_CACHE_DIR", "mypy"),
            ("RUFF_CACHE_DIR", "ruff"),
            ("PYTHONPYCACHEPREFIX", "compiled"),
        ):
            if cache_dir is not None:
                env[variable] = str(tmp / child)
            else:
                env.setdefault(variable, str(tmp / child))
    completed = subprocess.run(command, cwd=cwd or ROOT, text=True, capture_output=True, env=env)
    output = (completed.stdout + completed.stderr).strip()
    print(output[-2500:] if output else "(no output)")
    results[name] = completed.returncode == 0
    return completed


# For a release, source checks read the frozen candidate and Git checks address its immutable commit.
def run_static_checks(
    python: str,
    results: dict[str, bool],
    *,
    source: Path | None = None,
    commit: str | None = None,
    cache_dir: Path | None = None,
) -> None:
    yaml_check = (
        "import pathlib,yaml,sys\n"
        "bad=[]\n"
        "for pat in ('*.yml','*.yaml'):\n"
        "    for p in pathlib.Path('.').rglob(pat):\n"
        "        if {'.venv','.venv-ha','.git'} & set(p.parts): continue\n"
        "        try: yaml.safe_load(p.read_text(encoding='utf-8'))\n"
        "        except Exception as e: bad.append(f'{p}: {str(e)[:80]}')\n"
        "print('\\n'.join(bad)); sys.exit(1 if bad else 0)\n"
    )
    for name, args in (
        ("ruff check", ["-m", "ruff", "check", "."]),
        ("ruff format", ["-m", "ruff", "format", "--check", *FORMAT_TARGETS]),
        ("mypy --strict", ["-m", "mypy", "--strict", "--follow-imports=skip", *MYPY_TARGETS]),
        ("version", ["tools/version.py", "check"]),
        ("translations (hassfest rules)", ["tools/check_translations.py"]),
        ("yaml", ["-c", yaml_check]),
        ("compileall", ["-m", "compileall", "-q", "-f", *FORMAT_TARGETS]),
    ):
        run_step(name, [python, *args], results, cwd=source, cache_dir=cache_dir)
    if commit is None:
        run_step("git diff --check", ["git", "diff", "--check", "HEAD"], results)
    run_step("committed whitespace", ["git", "show", "--check", "--format=", commit or "HEAD"], results)


# Check all three raw qualification phases against the exact clean source.
def audit_qualification(path, source, metadata, python, results, collection_executors):
    bundle = json.loads(path.read_text())
    required = {"engine-model", "ha-proxy", "ha-proxy-2026.10"}
    if (
        bundle.get("commit") != metadata["commit"]
        or bundle.get("source_digest") != metadata["source_digest"]
        or set(bundle.get("phases", {})) != required
    ):
        raise ValueError("qualification bundle does not cover this exact source and every required suite")
    engine_packages = {}
    for suite in sorted(required):
        batch = Path(bundle["phases"][suite]).resolve()
        config = json.loads((batch / "config.json").read_text())
        if config.get("suite") != suite or config.get("workers") != 20 or config.get("mode") != "full":
            raise ValueError("qualification phase requires its entire suite in full mode with 20 workers")
        completed = run_step(
            f"qualification ({suite})",
            [
                python,
                "-B",
                str(source / "tools/audit_parallel_qualification.py"),
                str(batch),
                "--expected-commit",
                metadata["commit"],
                "--expected-source-digest",
                metadata["source_digest"],
                *(["--collection-executors", str(collection_executors.resolve())] if collection_executors else []),
            ],
            results,
            cwd=source,
        )
        audited = json.loads(completed.stdout)
        if (
            completed.returncode != 0
            or audited.get("full_qualification_passed") is not True
            or audited.get("mode") != "full"
            or audited.get("suite") != suite
            or audited.get("workers") != 20
            or audited.get("commit") != metadata["commit"]
            or audited.get("source_digest") != metadata["source_digest"]
        ):
            results[f"qualification ({suite})"] = False
            raise ValueError("fresh raw audit did not establish full qualification for this exact suite/source")
        if suite == "engine-model":
            engine_packages = config["expected_packages"]
    return engine_packages


# Give the approved receipt its final name after the exact staged source is in place.
def publish_snapshot(source: Path, destination: Path, metadata: dict) -> None:
    pending = source.parent / "snapshot.json"
    pending.write_text(json.dumps(metadata) + "\n")
    installed = destination / "source"
    receipt = destination / "snapshot.json"
    installed.mkdir()
    try:
        if receipt.exists() or receipt.is_symlink():
            raise FileExistsError("the destination already has a snapshot receipt")
        source.replace(installed)
        pending.replace(receipt)
    except BaseException:
        # Release only the output path this invocation reserved. An earlier snapshot is still intact.
        if source.exists():
            installed.rmdir()
        else:
            installed.rename(source)
        raise


# Finish the normal gate and the focused iCS checks before publishing a deployable snapshot.
def run_full_qualification(
    path: Path | None,
    python: str,
    results: dict[str, bool],
    collection_executors: Path | None = None,
    *,
    validated_snapshot: Path | None = None,
    deployment: Deployment | None = None,
    include_static: bool = True,
) -> None:
    if path is None:
        print("Full qualification requires --qualification-report from tools/parallel_qualify.py (20 workers).")
        results["full qualification"] = False
        return
    try:
        if validated_snapshot is not None and not include_static:
            raise ValueError("a deployment snapshot requires static checks")
        destination = validated_snapshot.resolve() if validated_snapshot is not None else None
        with tempfile.TemporaryDirectory(prefix="ocpp-release-", dir=destination) as tmp:
            scratch = Path(tmp)
            target = deployment.target if deployment is not None else "qualification-only"
            ics_release = target in ("ics-proxy", "ics-client")
            source = scratch / "source"
            metadata = freeze(ROOT, source)
            if include_static:
                run_static_checks(python, results, source=source, commit=metadata["commit"], cache_dir=scratch)
            week = target == "ics-proxy" and ics_predeployment.changed_proxy(
                ROOT, metadata["commit"], deployment.installed
            )
            if week and (deployment.ha_python is None or not deployment.ha_python.is_file()):
                raise ValueError(
                    "provide --ics-ha-python from a pinned Home Assistant runtime for the installation week"
                )
            engine_packages = audit_qualification(path, source, metadata, python, results, collection_executors)
            if target == "ics-proxy":
                ics_predeployment.require_standalone_pins(source, engine_packages)
            if ics_release and all(results.values()):
                output = path.resolve().parent / "ics-predeployment"
                ics_predeployment.ensure_check(ics_predeployment.DISCOVERY, source, output, metadata, python)
                results["recovery search"] = True
                if week:
                    previous = scratch / "installed"
                    ics_predeployment.export_installed(ROOT, deployment.installed, previous)
                    ics_predeployment.ensure_check(
                        ics_predeployment.WEEK,
                        source,
                        output,
                        metadata,
                        str(deployment.ha_python.absolute()),
                        installed=(deployment.installed, previous),
                    )
                    results["installation week"] = True
            if destination is not None and all(results.values()):
                publish_snapshot(source, destination, metadata)
    except (
        OSError,
        ValueError,
        KeyError,
        RuntimeError,
        ElementTree.ParseError,
        subprocess.SubprocessError,
    ) as error:
        print(f"Full qualification is invalid: {type(error).__name__}: {error}")
        results["full qualification"] = False


# Orchestrate the CI-equivalent checks.
# Release candidates need CI parity before a tag is pushed (a failing hassfest once shipped in a tag).
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--quick", action="store_true", help="skip pytest")
    mode.add_argument("--tests-only", action="store_true", help="run only pytest")
    parser.add_argument("-k", dest="expression", default="", help="engine pytest -k expression; not full qualification")
    parser.add_argument("--qualification-report", type=Path, default=os.environ.get("OCPP_QUALIFICATION_REPORT"))
    parser.add_argument(
        "--collection-executors", type=Path, help="explicit trusted source-pinned collection executor map"
    )
    parser.add_argument("--validated-snapshot", type=Path, help="retain the source that passed full qualification")
    parser.add_argument(
        "--deployment-target",
        choices=("ics-proxy", "ics-client", "qualification-only"),
        default="ics-proxy",
        help="release target, or qualification-only for an evidence audit",
    )
    parser.add_argument(
        "--installed-proxy-commit",
        default=os.environ.get("OCPP_INSTALLED_PROXY_COMMIT"),
        help="verified full commit currently installed on this standalone proxy target",
    )
    parser.add_argument(
        "--ics-ha-python",
        type=Path,
        default=os.environ.get("OCPP_ICS_HA_PYTHON"),
        help="pinned Home Assistant interpreter used for a changed standalone proxy",
    )
    args = parser.parse_args()
    if args.validated_snapshot and (args.quick or args.tests_only or args.expression):
        parser.error("--validated-snapshot requires full validation")
    if args.validated_snapshot and args.deployment_target == "qualification-only":
        parser.error("an evidence audit cannot produce a deployment snapshot")
    python = venv_python()
    results: dict[str, bool] = {}

    if not args.tests_only and (args.quick or args.expression):
        run_static_checks(python, results)

    if not args.quick and args.expression:
        command = [python, "-m", "pytest", "-q", "-rf", "-p", "no:cacheprovider", "tests/", "-k", args.expression]
        completed = run_step("pytest", command, results)
        failures = parse_failures(completed.stdout + completed.stderr)
        baseline = load_baseline() if sys.platform == "win32" else []
        known, new = classify_failures(failures, baseline)
        results["pytest"] = pytest_succeeded(completed, known, new)
        print(f"pytest: {len(failures)} failed, {len(known)} known environment failures, {len(new)} new")
        for item in new:
            print(f"  NEW FAILURE: {item}")
        print("Targeted engine tests only; full qualification requires all three 20-worker suites.")
    elif not args.quick:
        run_full_qualification(
            args.qualification_report,
            python,
            results,
            validated_snapshot=args.validated_snapshot,
            collection_executors=args.collection_executors,
            deployment=Deployment(args.deployment_target, args.installed_proxy_commit, args.ics_ha_python),
            include_static=not args.tests_only,
        )

    print("\nSummary")
    for name, ok in results.items():
        print(f"  {'ok  ' if ok else 'FAIL'} {name}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
