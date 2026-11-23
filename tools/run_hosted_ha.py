#!/usr/bin/env python3
# Modified for the standalone iCS proxy repository.
"""Run a complete hosted Home Assistant suite and verify that every collected test passed."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.qualification_collection import ROOTS  # noqa: E402
from tools.run_model_shard import hosted_collection, verify_execution  # noqa: E402
from tools.schedule_sequence_manifest import manifest  # noqa: E402

HA_SERIES = {"ha-proxy": "2026.11", "ha-proxy-2026.10": "2026.10"}


# Collect independently, preserve pytest's exit status, and verify every test's execution record.
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", required=True, choices=HA_SERIES)
    parser.add_argument("--expected-ha-version", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    series, _, patch = args.expected_ha_version.rpartition(".")
    if series != HA_SERIES[args.suite] or not patch.isdecimal():
        parser.error("the Home Assistant version must match the selected suite's release series")
    if not args.output.is_absolute():
        parser.error("use an absolute output directory")
    try:
        installed_ha = version("homeassistant")
    except PackageNotFoundError:
        parser.error("Home Assistant is missing from the hosted test environment")
    if installed_ha != args.expected_ha_version:
        parser.error(f"the hosted environment has Home Assistant {installed_ha}. Expected {args.expected_ha_version}")
    args.output.mkdir(parents=True, exist_ok=False)
    plan = args.output / "collection.json"
    evidence = args.output / "results.jsonl"
    pins = {"homeassistant": args.expected_ha_version}
    env = {
        **os.environ,
        "TMPDIR": str(args.output),
        "SCENARIO_RESULTS": str(evidence),
        "QUALIFICATION_EXPECTED_PACKAGES": json.dumps(pins),
    }
    collected = subprocess.run(
        [
            sys.executable,
            "-B",
            "tools/qualification_collection.py",
            "--source",
            str(ROOT),
            "--suite",
            args.suite,
            "--output",
            str(plan),
        ],
        cwd=ROOT,
        env=env,
    )
    if collected.returncode:
        return collected.returncode
    selected = hosted_collection(plan, args.suite)
    runtime = json.loads(plan.read_text())["runtime"]
    if runtime["packages"] != pins:
        raise RuntimeError("hosted collection did not use the expected Home Assistant version")
    cases = args.output / "sequence-manifest.jsonl"
    manifest(cases, args.suite)
    models = {
        row["node_id"]: row for line in cases.read_text().splitlines() if (row := json.loads(line))["exclusion"] is None
    }
    if not models.keys() <= set(selected):
        raise RuntimeError("hosted Home Assistant plan omitted registered test cases")

    command = [sys.executable, "-B", "tools/offline_ha_validate.py", "-p", "tools.qualification_evidence"]
    result = subprocess.run([*command, *ROOTS[args.suite]], cwd=ROOT, env=env)
    if result.returncode:
        return result.returncode
    rows = [json.loads(line) for line in evidence.read_text().splitlines()]
    observed = [
        (row["event"], row.get("runtime")) for row in rows if row["event"] in ("worker_namespace", "session_finish")
    ]
    if observed != [("worker_namespace", runtime), ("session_finish", runtime)]:
        raise RuntimeError("hosted execution did not retain the pinned collection runtime")
    verify_execution(rows, selected, models)
    print(f"Verified every phase for {len(selected)} Home Assistant tests.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
