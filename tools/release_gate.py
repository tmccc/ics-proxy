#!/usr/bin/env python3
"""Check the hosted result for an exact release commit.

The ``checks`` job in ``.github/workflows/checks.yml`` succeeds only once static,
engine and both Home Assistant legs pass. This optional hosted check is separate
from the source-bound qualification audit used by ``tools/validate.py``::

    python tools/release_gate.py <owner/repo> <sha> [--timeout 900]

Use a full commit SHA and an authenticated ``gh`` session with access to the repository.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence

REQUIRED_CHECKS: tuple[str, ...] = ("checks",)


# Reduce the check runs of one commit to pass, fail or pending for the required checks.
# A missing or unfinished check must wait; only a finished check that is not a success fails at once.
def evaluate(check_runs: Sequence[Mapping[str, object]], required: Sequence[str] = REQUIRED_CHECKS) -> tuple[str, str]:
    """Return ``(state, detail)`` where state is ``pass``, ``fail`` or ``pending``."""
    by_name: dict[str, Mapping[str, object]] = {}
    # A check can run more than once on a commit. The newest run owns the current outcome.
    for run in sorted(check_runs, key=lambda item: int(str(item.get("id") or 0))):
        by_name[str(run.get("name"))] = run
    pending: list[str] = []
    for name in required:
        run = by_name.get(name)
        if run is None or run.get("status") != "completed":
            pending.append(name)
            continue
        if run.get("conclusion") != "success":
            return "fail", f"{name} concluded {run.get('conclusion')}"
    if pending:
        return "pending", "waiting for " + ", ".join(pending)
    return "pass", "all required checks succeeded"


# Read all check runs of one commit through the `gh` CLI.
# The gate needs the real result for the tagged commit, not the state of a branch.
def _fetch_check_runs(repo: str, sha: str, *, timeout: float) -> list[dict[str, object]]:
    out = subprocess.run(
        ["gh", "api", f"repos/{repo}/commits/{sha}/check-runs?filter=all", "--paginate", "--jq", ".check_runs[]"],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    ).stdout
    runs = (json.loads(line) for line in out.splitlines() if line.strip())
    return [
        run for run in runs if run.get("head_sha") == sha and (run.get("app") or {}).get("slug") == "github-actions"
    ]


# Poll the check runs until the gate passes, fails or the timeout ends.
# Hosted jobs can finish after the candidate commit is selected.
def wait_for_gate(
    fetch: Callable[[float], Sequence[Mapping[str, object]]],
    timeout: float,
    interval: float = 20.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    required: Sequence[str] = REQUIRED_CHECKS,
) -> tuple[bool, str]:
    deadline = clock() + timeout
    detail = "waiting for " + ", ".join(required)
    while (remaining := deadline - clock()) > 0:
        try:
            state, outcome = evaluate(fetch(remaining), required)
        except subprocess.TimeoutExpired:
            break
        remaining = deadline - clock()
        if remaining <= 0:
            break
        if state != "pending":
            return state == "pass", outcome
        detail = outcome
        sleep(min(interval, remaining))
    return False, f"timeout: {detail}"


# Parse the arguments, run the gate and turn its result into the exit code.
def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo")
    parser.add_argument("sha")
    parser.add_argument("--timeout", type=float, default=900.0)
    args = parser.parse_args(argv)
    sha = args.sha.lower()
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
        parser.error("provide a full release commit SHA")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("provide a finite positive timeout")
    ok, detail = wait_for_gate(lambda remaining: _fetch_check_runs(args.repo, sha, timeout=remaining), args.timeout)
    print(("RELEASE GATE OK: " if ok else "RELEASE GATE FAILED: ") + detail, file=sys.stderr if not ok else sys.stdout)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
