#!/usr/bin/env python3
# Modified for the standalone iCS proxy repository.
"""Build the source-linked risk matrix, optionally attaching freshly re-audited complete qualification evidence."""

import argparse
import json
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.qualification_collection import ROOTS as QUALIFICATION_ROOTS  # noqa: E402


# Declared coverage never masquerades as execution; finite denominators and focused-only areas remain distinct.
def matrix():
    from tests_model.catalog import FAMILIES, SCHEMA
    from tests_model.coverage import RISKS

    risks = {risk.identifier: asdict(risk) | {"families": []} for risk in RISKS}
    families = []
    for family in FAMILIES:
        raw, admitted, exclusions = 0, 0, Counter()
        for case in family.inputs():
            raw += 1
            reason = family.excluding(case)
            if reason:
                exclusions[reason] += 1
            else:
                admitted += 1
        if (raw, admitted) != (family.raw, family.admitted):
            raise ValueError("family declaration differs from generated inputs: " + family.name)
        families.append(
            {
                "name": family.name,
                "suites": family.suites,
                "test_function": family.test_function,
                "raw": raw,
                "admitted": admitted,
                "exclusions": dict(exclusions),
                "risks": family.risks,
            }
        )
        for identifier in family.risks:
            risks[identifier]["families"].append(family.name)
    for risk in risks.values():
        risk["coverage_kind"] = "finite histories plus focused tests" if risk["families"] else "focused tests"
    return {
        "schema": SCHEMA,
        "execution_status": "declared only; no qualification inferred",
        "families": families,
        "risks": list(risks.values()),
        "scope": "Finite declared inputs and named failure boundaries; not arbitrary product histories.",
    }


# Full claims require the normal validator to re-audit original raw artifacts, never a copied green flag.
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--qualification-report", type=Path)
    parser.add_argument(
        "--collection-executors", type=Path, help="explicit trusted source-pinned collection executor map"
    )
    args = parser.parse_args()
    if args.qualification_report:
        bundle_path = args.qualification_report.resolve()
        executors = args.collection_executors.resolve() if args.collection_executors else None
        with tempfile.TemporaryDirectory(prefix="recovery-coverage-") as scratch:
            audited = Path(scratch) / "qualification.json"
            subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(ROOT / "tools/validate.py"),
                    "--deployment-target",
                    "qualification-only",
                    "--tests-only",
                    "--qualification-report",
                    str(bundle_path),
                    "--qualification-audit-output",
                    str(audited),
                    "--include-coverage",
                    *(["--collection-executors", str(executors)] if executors else []),
                ],
                cwd=ROOT,
                check=True,
            )
            qualification = json.loads(audited.read_text())
            if (
                not isinstance(qualification, dict)
                or not isinstance(qualification.get("commit"), str)
                or not isinstance(qualification.get("source_digest"), str)
                or not isinstance(qualification.get("phases"), dict)
                or qualification["phases"].keys() != QUALIFICATION_ROOTS.keys()
            ):
                raise ValueError("invalid qualification audit output")
            report = qualification.pop("coverage", None)
            if (
                not isinstance(report, dict)
                or type(report.get("schema")) is not int
                or not isinstance(report.get("families"), list)
                or not isinstance(report.get("risks"), list)
                or not isinstance(report.get("scope"), str)
            ):
                raise ValueError("invalid qualification coverage matrix")
            report["qualification"] = qualification
        report["execution_status"] = "all three complete 20-worker suites freshly re-audited"
    else:
        report = matrix()
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
