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
STOP_NATIVE_FIRST_PROCESS = (
    "tests/test_real_process_death.py::test_process_cut_after_queued_stop_observes_native_closure"
)
SCHEDULE = "tests/test_managed_local_schedule.py"
SCHEDULE_CUTOFF = "tests/test_managed_local_schedule_cutoff.py"
CUTOFF_REPLACEMENT = f"{SCHEDULE_CUTOFF}::test_cutoff_retains_elapsed_deadline_across_replacement"
CUTOFF_REWIND = f"{SCHEDULE_CUTOFF}::test_observed_cutoff_survives_refused_claim_and_wall_rewind"
CUTOFF_CLOSED_REPORT = f"{SCHEDULE_CUTOFF}::test_cutoff_holds_zero_when_stop_report_arrives_first"
SCHEDULE_VENDOR_RETURN = f"{SCHEDULE}::test_cloud_returns_before_local_schedule_sends_start"
STOP_INCIDENT = "tests/test_managed_stop_incident.py"
SCHEDULE_CLOSURE = "tests/test_managed_scheduled_closure.py"
CUTOFF_RETAINED_CLOSURE = f"{SCHEDULE_CLOSURE}::test_cutoff_checks_retained_stop_closure_after_replacement"
INCIDENT_WAL = "tests/test_managed_incident.py::test_incident_respects_the_published_live_wal_boundary"
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
HA_DISPLAY = "tests_ha_proxy/test_display_example.py"
HA_DISPLAY_STOP = f"{HA_DISPLAY}::test_canvas_stop_rechecks_the_local_session"
HA_DISPLAY_HANDSHAKE = f"{HA_DISPLAY}::test_canvas_frame_can_expire_during_the_management_handshake"
HA_DISPLAY_CLOCK_STEP = f"{HA_DISPLAY}::test_display_service_rejects_pre_adjustment_frames"
HA_DISPLAY_REPLACEMENT = f"{HA_DISPLAY}::test_display_rejects_start_frame_from_before_entry_replacement"
HA_DISPLAY_DESTINATION = f"{HA_DISPLAY}::test_canvas_rejects_replacement_at_the_same_endpoint"

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
            f"{STOP_NATIVE_FIRST_PROCESS}[checkpoint-saved]",
            f"{STOP_NATIVE_FIRST_PROCESS}[both-writes-refused]",
            f"{STOP_TEST}::test_replacement_validates_retained_stop_closure_binding[legacy]",
            f"{STOP_TEST}::test_replacement_validates_retained_stop_closure_binding[report]",
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
            f"{CUTOFF_REPLACEMENT}[same]",
            f"{CUTOFF_REPLACEMENT}[changed]",
            f"{CUTOFF_REPLACEMENT}[unknown]",
            f"{CUTOFF_REPLACEMENT}[legacy]",
            f"{CUTOFF_REWIND}[at-end-cutoff-saved]",
            f"{CUTOFF_REWIND}[at-end-cutoff-retained-in-memory]",
            f"{CUTOFF_REWIND}[before-start-cutoff-saved]",
            f"{CUTOFF_REWIND}[before-start-cutoff-retained-in-memory]",
            f"{CUTOFF_CLOSED_REPORT}[native-unavailable]",
            f"{SCHEDULE_CUTOFF}::test_cutoff_leaves_settled_early_stop_in_app_mode",
            f"{STOP_TEST}::test_blocked_poll_records_scheduled_session_stop_closure",
            f"{STOP_TEST}::test_management_start_then_session_stop_at_zero_watts",
            f"{SCHEDULE_CLOSURE}::test_scheduled_closure_survives_interrupted_poll[native-audit]",
            f"{SCHEDULE_CLOSURE}::test_scheduled_closure_survives_interrupted_poll[charge-publication]",
            f"{SCHEDULE_CLOSURE}::test_scheduled_closure_survives_marker_refusal_and_replacement",
            f"{CUTOFF_RETAINED_CLOSURE}[accept-before-exact]",
            f"{CUTOFF_RETAINED_CLOSURE}[accept-before-other-report]",
            f"{CUTOFF_RETAINED_CLOSURE}[reject-before-exact]",
            f"{CUTOFF_RETAINED_CLOSURE}[reject-after-exact]",
            f"{CUTOFF_RETAINED_CLOSURE}[reject-after-other-report]",
            "tests/test_managed_api.py::test_management_connections_leave_pause_available[busy]",
            f"{SCHEDULE_VENDOR_RETURN}[vendor-lost-during-rollback-automatic-return]",
            f"{SCHEDULE}::test_overlapping_timezone_change_does_not_repeat_local_occurrence",
            "tests/test_vendor_stop_rotation.py::test_queued_vendor_stop_keeps_reply_through_rotation[Accepted]",
            f"{STOP_INCIDENT}::test_legacy_joined_stop_report_retains_its_primary_reply",
            f"{STOP_INCIDENT}::test_saved_vendor_rejection_survives_joined_receipt_settlement",
            f"{STOP_INCIDENT}::test_joined_owner_checkpoint_must_match_pause_claim[wrong-ticket]",
            f"{STOP_INCIDENT}::test_legacy_shutdown_checkpoint_must_match_original_target[wrong-boot]",
            f"{INCIDENT_WAL}[published]",
        ),
    ),
    "ha": (
        "offline_ha_validate.py",
        (
            f"{HA_TEST}[local]",
            f"{HA_TEST}[vendor]",
            f"{HA_DISPLAY}::test_canvas_start_requires_the_displayed_revision",
            f"{HA_DISPLAY_STOP}[old_native]",
            f"{HA_DISPLAY_STOP}[closed]",
            f"{HA_DISPLAY_HANDSHAKE}[start]",
            f"{HA_DISPLAY_HANDSHAKE}[stop]",
            f"{HA_DISPLAY_CLOCK_STEP}[forward_then_rewind-start]",
            f"{HA_DISPLAY_CLOCK_STEP}[forward_then_rewind-stop]",
            f"{HA_DISPLAY_REPLACEMENT}[reload]",
            f"{HA_DISPLAY_REPLACEMENT}[remove-add]",
            f"{HA_DISPLAY_DESTINATION}[after-poll-start]",
            f"{HA_DISPLAY_DESTINATION}[during-handshake-start]",
            f"{HA_DISPLAY}::test_ordinary_start_does_not_reach_a_replacement_destination[session_action]",
            f"{HA_DISPLAY}::test_ordinary_start_does_not_reach_a_replacement_destination[start_button]",
            f"{HA_DISPLAY}::test_pause_keeps_its_stale_revision_behavior_with_owner_status[all-commands]",
        ),
    ),
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
