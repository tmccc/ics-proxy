#!/usr/bin/env python3
"""Rehearse an exact proxy upgrade and rollback using synthetic charger state."""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import ics_predeployment  # noqa: E402
from tools.qualification_snapshot import freeze  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installed-proxy-commit", required=True, help="verified full commit of the installed proxy")
    parser.add_argument("--ics-ha-python", type=Path, required=True, help="pinned Home Assistant interpreter")
    parser.add_argument("--ics-installed-python", type=Path, help="previous interpreter if the standalone pins differ")
    parser.add_argument("--output", type=Path, required=True, help="directory for the local rehearsal evidence")
    args = parser.parse_args()
    previous_commit = args.installed_proxy_commit

    try:
        with tempfile.TemporaryDirectory(prefix="ics-rollback-rehearsal-") as directory:
            scratch = Path(directory)
            source, previous = scratch / "candidate", scratch / "previous"
            metadata = freeze(ROOT, source)
            ics_predeployment.changed_proxy(ROOT, metadata["commit"], previous_commit)
            if metadata["commit"] == previous_commit:
                raise ValueError("the installed and candidate commits must differ")
            ics_predeployment.export_installed(ROOT, previous_commit, previous)
            ics_predeployment.ensure_check(
                ics_predeployment.WEEK,
                source,
                args.output.resolve(),
                metadata,
                str(args.ics_ha_python.absolute()),
                installed=(previous_commit, previous),
                installed_python=str(args.ics_installed_python.absolute()) if args.ics_installed_python else None,
            )
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Rollback rehearsal failed. {error}\n")

    print(f"Passed simulated upgrade and rollback from {metadata['commit']} to {previous_commit}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
