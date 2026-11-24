#!/usr/bin/env python3
"""Run the focused offline control settlement cases for the engine or Home Assistant."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
STOP_TEST = "tests/test_managed_session_stop.py"
STOP_JOIN = f"{STOP_TEST}::test_joined_session_stop_settles_from_late_rejection"
STOP_PUBLICATION = f"{STOP_TEST}::test_accepted_stop_closure_survives_publication_refusal"
STOP_RESTART = "tests/test_managed_stop_replacement.py::test_unmapped_stop_replacement_waits_for_native_closure"
PAUSE_OVERLAP = (
    "tests/test_managed_cloud_zero_controls.py::test_local_pause_and_cloud_stop_share_unanswered_stop_at_zero"
)
PAUSE_RECONNECT = (
    "tests/test_issued_pause_reconnect.py::test_newer_pause_stops_open_session_after_charger_reconnect"
)
REBASE_TEST = (
    "tests/test_managed_observed_freshness.py::test_expired_session_stop_sample_stays_expired_after_wall_rebase"
)
SHUTDOWN_TEST = "tests/test_real_process_death.py::test_process_cut_during_shutdown_stop"
JOURNAL_TEST = "tests/test_journal_fault_recovery.py::test_forwarded_reply_recovery_preserves_ambiguous_start_delivery"
SHUTDOWN_JOIN = (
    "tests/test_journal_fault_recovery.py::test_shutdown_waits_for_exact_prior_stops_or_an_independent_attempt"
)
HA_TEST = "tests_ha_proxy/test_client.py::test_session_stop_shares_the_vendor_wire_in_either_order"

SUITES = {
    "engine": (
        "offline_validate.py",
        (
            f"{STOP_TEST}::test_session_stop_shares_the_vendor_wire_in_either_order[native-local]",
            f"{STOP_TEST}::test_session_stop_shares_the_vendor_wire_in_either_order[native-vendor]",
            f"{STOP_TEST}::test_session_stop_shares_the_vendor_wire_in_either_order[ocpp-local]",
            f"{STOP_TEST}::test_session_stop_shares_the_vendor_wire_in_either_order[ocpp-vendor]",
            f"{STOP_TEST}::test_session_stop_reuses_a_wire_with_a_lost_reply[local]",
            f"{STOP_TEST}::test_session_stop_reuses_a_wire_with_a_lost_reply[vendor]",
            f"{STOP_JOIN}[closure-first-local]",
            f"{STOP_JOIN}[closure-first-vendor]",
            f"{STOP_JOIN}[rejection-first-local]",
            f"{STOP_JOIN}[rejection-first-vendor]",
            f"{STOP_JOIN}[closure-first-local-wire-busy]",
            f"{STOP_JOIN}[closure-first-vendor-wire-busy]",
            f"{STOP_JOIN}[rejection-first-local-wire-busy]",
            f"{STOP_JOIN}[rejection-first-vendor-wire-busy]",
            f"{STOP_TEST}::test_session_stop_awaits_closure_when_session_closes_in_gate",
            f"{STOP_TEST}::test_verified_closure_survives_stop_reply_window[Accepted]",
            f"{STOP_TEST}::test_verified_closure_survives_stop_reply_window[Rejected]",
            f"{STOP_TEST}::test_verified_closure_survives_stop_reply_window[expired]",
            f"{STOP_TEST}::test_verified_closure_survives_stop_reply_window[disconnected]",
            f"{STOP_TEST}::test_joined_stop_report_retains_accepted_predecessor[local]",
            f"{STOP_TEST}::test_joined_stop_report_retains_accepted_predecessor[vendor]",
            f"{STOP_TEST}::test_buffered_late_stop_rejection_fences_receipts[local]",
            f"{STOP_TEST}::test_buffered_late_stop_rejection_fences_receipts[vendor]",
            f"{STOP_TEST}::test_exact_stop_rejection_fences_receipt_after_replacement[owner]",
            f"{STOP_TEST}::test_exact_stop_rejection_fences_receipt_after_replacement[vendor]",
            f"{STOP_TEST}::test_replacement_settles_only_saved_verified_stop_closure[old-receipt]",
            f"{STOP_TEST}::test_replacement_settles_only_saved_verified_stop_closure[verified-closure]",
            f"{STOP_PUBLICATION}[next-poll]",
            f"{STOP_PUBLICATION}[marker-refused]",
            f"{STOP_PUBLICATION}[replacement]",
            f"{STOP_RESTART}[exact-stop-proof-plugged]",
            f"{STOP_RESTART}[exact-stop-proof-unplugged]",
            f"{STOP_RESTART}[report-only-plugged]",
            f"{STOP_RESTART}[report-only-unplugged]",
            f"{STOP_TEST}::test_live_stop_confirmation_uses_elapsed_time[-30]",
            f"{STOP_TEST}::test_live_stop_confirmation_uses_elapsed_time[30]",
            f"{STOP_TEST}::test_queued_stop_cannot_dispatch_after_expiry_write_rolls_back",
            f"{PAUSE_OVERLAP}[local]",
            f"{PAUSE_OVERLAP}[vendor]",
            f"{PAUSE_RECONNECT}[joined-foreground-stop]",
            f"{REBASE_TEST}[admission]",
            f"{REBASE_TEST}[dispatch]",
            f"{STOP_TEST}::test_session_stop_target_awaits_current_boot_supervision",
            f"{SHUTDOWN_TEST}[sent]",
            f"{SHUTDOWN_TEST}[unissued]",
            f"{SHUTDOWN_JOIN}[None]",
            f"{SHUTDOWN_JOIN}[own-wire]",
            f"{SHUTDOWN_JOIN}[legacy]",
            f"{JOURNAL_TEST}[startup]",
            f"{JOURNAL_TEST}[live-retry]",
        ),
    ),
    "ha": ("offline_ha_validate.py", (f"{HA_TEST}[local]", f"{HA_TEST}[vendor]")),
}


# Pytest can succeed with skipped cases, so verify each named case and the suite totals.
def check_report(path: Path, node_ids: tuple[str, ...]) -> None:
    report = ElementTree.parse(path)
    suites = list(report.iter("testsuite"))
    cases = list(report.iter("testcase"))
    qualified_names = (node.split("::", 1) for node in node_ids)
    expected = Counter((file.removesuffix(".py").replace("/", "."), name) for file, name in qualified_names)
    observed = Counter((case.get("classname", ""), case.get("name", "")) for case in cases)
    counts = {"tests": str(len(node_ids)), "skipped": "0", "failures": "0", "errors": "0"}
    if observed != expected:
        missing = list((expected - observed).elements())
        extra = list((observed - expected).elements())
        raise ValueError(f"missing cases {missing!r}, extra cases {extra!r}")
    if (
        len(suites) != 1
        or any(suites[0].get(key) != value for key, value in counts.items())
        or any(child.tag in {"skipped", "failure", "error"} for case in cases for child in case)
    ):
        raise ValueError("every selected case must pass, with no skips, failures or errors")


# Run only the named node IDs with the matching offline runner and a fresh scratch directory.
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", choices=SUITES)
    args = parser.parse_args()
    scratch = Path(os.environ.get("TMPDIR", ""))
    if not scratch.is_absolute() or not scratch.is_dir():
        parser.error("set TMPDIR to an existing absolute scratch directory")
    runner, node_ids = SUITES[args.suite]
    with tempfile.TemporaryDirectory(dir=scratch, prefix="control-settlement-") as directory:
        work = Path(directory)
        report = work / "results.xml"
        env = {**os.environ, "TMPDIR": directory, "PYTEST_ADDOPTS": "", "PYTEST_PLUGINS": ""}
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                f"tools/{runner}",
                "-o",
                "addopts=",
                "-o",
                "junit_family=xunit2",
                f"--basetemp={work / 'pytest'}",
                f"--junitxml={report}",
                *node_ids,
            ],
            cwd=ROOT,
            env=env,
        )
        if result.returncode:
            return result.returncode
        try:
            check_report(report, node_ids)
        except (OSError, ValueError, ElementTree.ParseError) as error:
            print(f"Control settlement gate could not verify {args.suite} results: {error}", file=sys.stderr)
            return 1
    print(f"Verified {len(node_ids)} control settlement cases in {args.suite}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
