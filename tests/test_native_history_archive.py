# Modified for the standalone iCS proxy repository.
"""Real SQLite preserves complete native history without making its retention a charging permission."""

from __future__ import annotations

import copy
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from ics_proxy_core.engine.managed_view import summarize_charger_history
from ics_proxy_core.engine.native_history import NativeHistoryArchive, latest_completion_available
from ics_proxy_core.engine.security import SecurityError
from ics_proxy_core.engine.store import ConflictError, Journal

IDENTITY = "HISTORY_ARCHIVE001"


# Keep opaque nested native fields so tests exercise complete-body preservation, not a chosen projection.
def row(identifier: int, *, closed: bool = True) -> dict[str, Any]:
    return {
        "transactionId": identifier,
        "connectorId": 1,
        "startTime": 1000 + identifier,
        "stopTime": 1060 + identifier if closed else 0,
        "meterStart": 5000,
        "meterStop": 5500 if closed else 0,
        "MeterValues": [{"opaque": {"samples": [1, 2, 3]}}],
        "filename": "synthetic-history-" + str(identifier) + ".json",
    }


# Use the real durable journal and lock, with no engine mock or charger connection.
@pytest.fixture
def history_journal(tmp_path: Path) -> Iterator[Journal]:
    journal = Journal(tmp_path / "history.sqlite3", IDENTITY)
    yield journal
    journal.close()


# Compare the entire application database around a rejected observation, including any newly inserted rows.
def contents(journal: Journal) -> str:
    return "\n".join(journal.db.iterdump())


# Intent: an archived older session cannot obscure a known newer managed closure after a clock correction.
# Why: assigned IDs establish order even when the charger's two start timestamps run backward.
def test_latest_archive_uses_known_closure_order(history_journal: Journal) -> None:
    older, latest = row(1001), row(1002) | {"startTime": 1000, "stopTime": 1060}
    archive = NativeHistoryArchive(history_journal)
    archive.observe([], [older, latest], allow_missing=True)
    archive.observe([], [latest], allow_missing=True)
    assert latest_completion_available(history_journal, summarize_charger_history((latest,)).completed, 1002)
    assert not latest_completion_available(history_journal, summarize_charger_history((older,)).completed, 1001)


# Intent: identify the latest native completion even when starts and stops have different orders.
# Why: a clock correction can preserve either session's own chronology while reversing the order of their starts.
def test_latest_native_completion_uses_stop_time_without_a_managed_close(history_journal: Journal) -> None:
    stopped_last = row(1001) | {"startTime": 1000, "stopTime": 1250, "meterStop": 12000}
    started_last = row(1002) | {"startTime": 1100, "stopTime": 1200, "meterStop": 7000}
    originals = [stopped_last, started_last]
    archive = NativeHistoryArchive(history_journal)
    archive.observe(originals, originals, allow_missing=True)

    history = summarize_charger_history(tuple(originals))
    assert history.complete and history.latest_completion_known and history.last_energy == 7.0
    assert history.sessions[0]["energy_kwh"] == 2.0

    for visible, expected in ((started_last, False), (stopped_last, True)):
        assert archive.observe(originals, [visible], allow_missing=True)["missing_records"] == 1
        current = summarize_charger_history((visible,))
        assert current.latest_completion_known
        assert latest_completion_available(history_journal, current.completed) is expected

    damaged = summarize_charger_history((stopped_last | {"meterStop": None}, started_last))
    assert not damaged.latest_completion_known


# Intent: resolve equal Stop times by Start time while honoring the exact managed closure.
# Why: native row order cannot distinguish records whose timestamps and energy disagree.
@pytest.mark.parametrize(
    ("start", "meter_stop", "expected_energy"),
    [(1100, 7000, 2.0), (1000, 7000, None), (1000, 12000, 7.0)],
    ids=["later-start", "ambiguous-energy", "same-energy"],
)
def test_native_completion_resolves_timestamp_ties(start: int, meter_stop: int, expected_energy: float | None) -> None:
    rows = (
        row(1002) | {"startTime": 1000, "stopTime": 1250, "meterStop": 12000},
        row(1001) | {"startTime": start, "stopTime": 1250, "meterStop": meter_stop},
    )
    for records in (rows, rows[::-1]):
        history = summarize_charger_history(records)
        assert history.complete and history.latest_completion_known == (expected_energy is not None)
        assert history.last_energy == expected_energy
        managed = summarize_charger_history(records, latest_closed=1002)
        assert managed.latest_completion_known and managed.last_energy == 7.0


# Intent: order an absent archive row sharing the visible latest Stop time by its recorded Start.
# Why: a provably earlier record cannot hide the latest energy, while ties and uncertain times still can.
@pytest.mark.parametrize(
    ("archived_start", "available"),
    [(1000, True), (1100, False), (1150, False), (None, False), (1300, False)],
    ids=["earlier", "exact-tie", "later", "missing-start", "reversed-clock"],
)
def test_latest_native_completion_resolves_retained_timestamp_ties(
    history_journal: Journal, archived_start: int | None, available: bool
) -> None:
    archived = row(1001) | {"startTime": archived_start, "stopTime": 1250}
    visible = row(1002) | {"startTime": 1100, "stopTime": 1250, "meterStop": 12000}
    archive = NativeHistoryArchive(history_journal)
    archive.observe([], [archived, visible], allow_missing=True)
    assert archive.observe([], [visible], allow_missing=True)["missing_records"] == 1
    visible_history = summarize_charger_history((visible,))
    assert latest_completion_available(history_journal, visible_history.completed) is available


# Intent: keep the latest energy consistent while an earlier unreadable record enters or leaves the native history.
# Why: valid Stop and Start times still order a completed session when its meter reading is missing.
@pytest.mark.parametrize("present", [True, False], ids=["visible", "rotated-out"])
@pytest.mark.parametrize(
    ("unreadable_start", "available"),
    [(1000, True), (1100, False), (1200, False)],
    ids=["earlier-start", "same-start", "later-start"],
)
def test_unreadable_energy_uses_the_same_completion_order_as_the_archive(
    history_journal: Journal, present: bool, unreadable_start: int, available: bool
) -> None:
    unreadable = row(1001) | {"startTime": unreadable_start, "stopTime": 1250, "meterStop": None}
    latest = row(1002) | {"startTime": 1100, "stopTime": 1250, "meterStop": 12000}
    rows = [unreadable, latest] if present else [latest]
    archive = NativeHistoryArchive(history_journal)
    archive.observe([], [unreadable, latest], allow_missing=True)
    archive.observe([], rows, allow_missing=True)

    summary = summarize_charger_history(tuple(rows))
    assert summary.complete is not present
    known = summary.latest_completion_known and latest_completion_available(history_journal, summary.completed)
    assert known is available
    if known:
        assert summary.last_energy == 7.0


# Intent: any number of absent original records is archived and reported without rewriting original admission data.
# Why: firmware history retention must not force a charging fault or silently erase the complete original archive.
def test_missing_originals_are_retained_with_unchanged_admission_history(history_journal: Journal) -> None:
    originals = [row(1001), row(1002), row(1003)]
    saved = {"stage": "admitted", "original_history": copy.deepcopy(originals)}
    history_journal.put("native_bounded_handover", saved)
    archive = NativeHistoryArchive(history_journal)
    before = copy.deepcopy(originals)

    result = archive.observe(originals, [], allow_missing=True)

    assert result == {"archived_records": 3, "missing_records": 3}
    assert history_journal.db.execute("SELECT COUNT(*) FROM native_history_records").fetchone()[0] == 3
    assert {r[0] for r in history_journal.db.execute("SELECT original FROM native_history_records")} == {1}
    assert history_journal.get("native_bounded_handover") == saved
    assert originals == before
    assert archive.observe(originals, originals, allow_missing=True) == {
        "archived_records": 3,
        "missing_records": 0,
    }


# Intent: strict callers still reject missing history and commit none of the otherwise valid new records.
# Why: this managed-mode capability must not weaken ordinary commissioning or leave a partial archive on failure.
@pytest.mark.parametrize("all_missing", [False, True])
def test_missing_history_is_still_rejected_without_opt_in(history_journal: Journal, all_missing: bool) -> None:
    archive = NativeHistoryArchive(history_journal)
    before = contents(history_journal)

    with pytest.raises(SecurityError):
        archive.observe([row(1001), row(1002)], [] if all_missing else [row(1001), row(1003)], allow_missing=False)

    assert contents(history_journal) == before


# Intent: a historical record's nested fields remain immutable even when its absence would be tolerated.
# Why: retaining records cannot turn permission to omit an old row into permission to alter its existing full body.
@pytest.mark.parametrize("change", ["meter", "nested", "extra", "numeric_type"])
def test_present_original_body_changes_are_rejected_atomically(history_journal: Journal, change: str) -> None:
    original = row(1001)
    archive = NativeHistoryArchive(history_journal)
    archive.observe([original], [original], allow_missing=True)
    altered = copy.deepcopy(original)
    if change == "meter":
        altered["meterStop"] += 1
    elif change == "nested":
        altered["MeterValues"][0]["opaque"]["samples"][1] = 99
    elif change == "extra":
        altered["unobserved_field"] = "changed"
    else:
        altered["MeterValues"][0]["opaque"]["samples"][0] = 1.0
    before = contents(history_journal)

    with pytest.raises(SecurityError):
        archive.observe([original], [row(1002), altered], allow_missing=True)

    assert contents(history_journal) == before
    assert archive.observe([original], [original], allow_missing=True)["missing_records"] == 0


# Intent: non-original active rows may evolve and disappear; genuinely closed versions are retained without overwrite.
# Why: freezing an active row would reject normal meter updates or invent a completed session from a partial read.
def test_open_history_can_evolve_until_actual_completion(history_journal: Journal) -> None:
    original, opened = row(1001), row(1002, closed=False)
    archive = NativeHistoryArchive(history_journal)
    assert archive.observe([original], [original, opened], allow_missing=True) == {
        "archived_records": 1,
        "missing_records": 0,
    }
    advanced = copy.deepcopy(opened)
    advanced["MeterValues"].append({"opaque": {"samples": [4, 5]}})
    assert archive.observe([original], [original, advanced], allow_missing=True)["archived_records"] == 1
    assert archive.observe([original], [original], allow_missing=True)["missing_records"] == 0

    completed = copy.deepcopy(advanced) | {"stopTime": 2062, "meterStop": 5731}
    assert archive.observe([original], [original, completed], allow_missing=True) == {
        "archived_records": 2,
        "missing_records": 0,
    }
    assert archive.observe([original], [original], allow_missing=True)["missing_records"] == 1
    assert archive.observe([original], [original, completed], allow_missing=True)["missing_records"] == 0

    changed = copy.deepcopy(completed) | {"meterStop": 5732, "syncStop": 2}
    assert archive.observe([original], [original, changed], allow_missing=True) == {
        "archived_records": 2,
        "missing_records": 0,
    }
    versions = history_journal.db.execute(
        "SELECT COUNT(*) FROM native_history_records WHERE transaction_id=1002"
    ).fetchone()[0]
    assert versions == 2
    assert {
        r[0]
        for r in history_journal.db.execute("SELECT original FROM native_history_records WHERE transaction_id=1002")
    } == {0}


# Intent: records first seen after admission remain archived and missing-state reporting survives a real restart.
# Why: an empty firmware list after replacement must not erase old evidence or permit an altered original to reappear.
def test_closed_archive_survives_process_replacement(tmp_path: Path) -> None:
    path = tmp_path / "replacement.sqlite3"
    original, later = row(1001), row(1002)
    journal = Journal(path, IDENTITY)
    try:
        archive = NativeHistoryArchive(journal)
        archive.observe([original], [original, later], allow_missing=True)
        assert archive.observe([original], [], allow_missing=True)["missing_records"] == 2
    finally:
        journal.close()

    replacement = Journal(path, IDENTITY)
    try:
        archive = NativeHistoryArchive(replacement)
        assert archive.observe([original], [], allow_missing=True) == {
            "archived_records": 2,
            "missing_records": 2,
        }
        altered = copy.deepcopy(original)
        altered["MeterValues"][0]["opaque"]["samples"].append(4)
        before = contents(replacement)
        with pytest.raises(SecurityError):
            archive.observe([original], [later, altered], allow_missing=True)
        assert contents(replacement) == before
        assert archive.observe([original], [original, later], allow_missing=True)["missing_records"] == 0
    finally:
        replacement.close()


# Intent: a valid first new record is not committed when a later row fails immutable-history validation.
# Why: an error halfway through observation must not leave a partially accepted native history or missing flags.
def test_late_archive_conflict_rolls_back_new_rows_and_missing_state(history_journal: Journal) -> None:
    archive = NativeHistoryArchive(history_journal)
    originals = [row(1001), row(1002)]
    archive.observe(originals, originals, allow_missing=True)
    archive.observe(originals, [originals[0]], allow_missing=True)
    changed = copy.deepcopy(originals[1]) | {"meterStop": 6000}
    before = contents(history_journal)

    with pytest.raises(SecurityError):
        archive.observe(originals, [row(1003), originals[0], changed], allow_missing=True)

    assert contents(history_journal) == before
    assert archive.observe(originals, [originals[0]], allow_missing=True) == {
        "archived_records": 2,
        "missing_records": 1,
    }


# Intent: the original admission list cannot be shortened, expanded or changed after its first binding.
# Why: relaxing rolling-history retention must not create a way to replace the immutable original archive.
@pytest.mark.parametrize("change", ["remove", "add", "alter"])
def test_original_archive_binding_cannot_be_replaced(history_journal: Journal, change: str) -> None:
    originals = [row(1001), row(1002)]
    archive = NativeHistoryArchive(history_journal)
    archive.observe(originals, originals, allow_missing=True)
    altered = copy.deepcopy(originals)
    if change == "remove":
        altered.pop()
    elif change == "add":
        altered.append(row(1003))
    else:
        altered[0]["MeterValues"][0]["opaque"]["samples"].append(4)
    before = contents(history_journal)

    with pytest.raises(SecurityError):
        archive.observe(altered, altered, allow_missing=True)

    assert contents(history_journal) == before


# Intent: an original open-looking historical body is retained exactly, unlike new active rows seen after admission.
# Why: a rolling list must not silently adopt or revise an unknown pre-existing session when it later disappears.
def test_original_open_body_is_archived_and_remains_immutable(history_journal: Journal) -> None:
    original = row(1001, closed=False)
    archive = NativeHistoryArchive(history_journal)
    assert archive.observe([original], [original], allow_missing=True)["archived_records"] == 1
    assert archive.observe([original], [], allow_missing=True)["missing_records"] == 1
    assert archive.observe([original], [original], allow_missing=True)["missing_records"] == 0
    changed = copy.deepcopy(original) | {"stopTime": 2061, "meterStop": 5500}
    before = contents(history_journal)

    with pytest.raises(SecurityError):
        archive.observe([original], [changed], allow_missing=True)

    assert contents(history_journal) == before


# Intent: duplicate identifiers and malformed scalar types fail before any archive state can be accepted.
# Why: dictionary coalescing, boolean integers and non-finite values could conceal record replacement or absence.
@pytest.mark.parametrize(
    "fault",
    ["duplicate", "boolean_id", "float_id", "zero_id", "boolean_stop", "nan_stop", "negative_stop", "text_stop"],
)
def test_invalid_current_history_does_not_partially_commit(history_journal: Journal, fault: str) -> None:
    archive = NativeHistoryArchive(history_journal)
    original = row(1001)
    archive.observe([original], [original], allow_missing=True)
    bad = row(1003)
    if fault == "boolean_id":
        bad["transactionId"] = True
    elif fault == "float_id":
        bad["transactionId"] = 1003.0
    elif fault == "zero_id":
        bad["transactionId"] = 0
    elif fault == "boolean_stop":
        bad["stopTime"] = True
    elif fault == "nan_stop":
        bad["stopTime"] = float("nan")
    elif fault == "negative_stop":
        bad["stopTime"] = -1
    elif fault == "text_stop":
        bad["stopTime"] = "2063"
    current = [row(1002), bad, bad] if fault == "duplicate" else [row(1002), bad]
    before = contents(history_journal)

    with pytest.raises(SecurityError):
        archive.observe([original], current, allow_missing=True)

    assert contents(history_journal) == before


# Intent: persisted archive data remains bound to the existing journal's charger identity after reopening.
# Why: history retained for one physical charger must not seed another charger's proof or allocation context.
def test_archive_cannot_reopen_under_another_journal_identity(tmp_path: Path) -> None:
    path = tmp_path / "identity.sqlite3"
    original = row(1001)
    journal = Journal(path, IDENTITY)
    try:
        NativeHistoryArchive(journal).observe([original], [original], allow_missing=True)
    finally:
        journal.close()

    with pytest.raises(ConflictError):
        Journal(path, "ANOTHER_SYNTHETIC_CHARGER")

    reopened = Journal(path, IDENTITY)
    try:
        assert NativeHistoryArchive(reopened).observe([original], [], allow_missing=True) == {
            "archived_records": 1,
            "missing_records": 1,
        }
    finally:
        reopened.close()


# Model the single start sample recorded by the iCS before it obtains a transaction number.
def unassigned(name: str = "attempt.json") -> dict[str, Any]:
    return {
        "transactionId": -1,
        "filename": name,
        "connectorId": 1,
        "idTagStart": "NATIVE_TIMER",
        "startTime": 1000,
        "startMs": 5,
        "meterStart": 9000,
        "startQ4": 6,
        "syncStart": 1,
        "syncStop": 0,
        "stopTime": 0,
        "stopMs": 0,
        "meterStop": 0,
        "stopQ4": 0,
        "idTagStop": "",
        "stopTimeStr": "",
        "reasonStr": "",
        "reason": 0,
        "idTypeStop": 0,
        "MeterValues": [
            {"syncMeter": 1, "timestamp": 1001, "context": 5, "A": [0, 0, 0], "energy": 9000, "Q4dot": 6, "Offer": 320}
        ],
    }


# Intent: retain all native attempt bodies across reads, counting only assigned IDs for allocation and HA sessions.
# Why: several failed starts can share -1 without representing either an accepted transaction or delivered energy.
def test_repeated_unassigned_history(history_journal: Journal) -> None:
    import json

    from ics_proxy_core.engine.managed_view import charger_history
    from ics_proxy_core.engine.trial_allocation import observe_history

    numbered = row(1234)
    attempts = [unassigned("a"), unassigned("b")]
    records = [numbered, *attempts]
    sample = observe_history(records, IDENTITY, "synthetic", 1002)
    assert sample.floor == 1234 and sample.count == 3
    assert charger_history(tuple(records)) == charger_history((numbered,))
    archive = NativeHistoryArchive(history_journal)
    assert archive.observe([numbered], records, allow_missing=True)["archived_records"] == 1
    archive.observe([numbered], [numbered], allow_missing=True)
    assert [json.loads(r[0]) for r in history_journal.db.execute("SELECT body FROM native_start_attempts")] == attempts


# Intent: ambiguous or energised unassigned records still reject the complete observation.
# Why: missing transaction identity cannot hide activity or make a positive operation safe.
@pytest.mark.parametrize(
    "change", ["energy", "current", "missing", "duplicate", "syncStop", "fractional", "reason", "idTypeStop", "power"]
)
def test_inconsistent_unassigned_history(history_journal: Journal, change: str) -> None:
    attempt = unassigned()
    records = [attempt]
    if change == "energy":
        attempt["MeterValues"][0]["energy"] += 1
    elif change == "current":
        attempt["MeterValues"][0]["A"][0] = 1
    elif change in ("reason", "idTypeStop"):
        attempt[change] = 1
    elif change == "power":
        attempt["MeterValues"][0]["power"] = 3000
    elif change == "missing":
        attempt.pop("MeterValues")
    elif change == "duplicate":
        records.append(copy.deepcopy(attempt))
    elif change == "fractional":
        attempt["transactionId"] = -1.0
    else:
        attempt["syncStop"] = 1
    archive = NativeHistoryArchive(history_journal)
    before = contents(history_journal)
    with pytest.raises(SecurityError):
        archive.observe([], records, allow_missing=True)
    assert contents(history_journal) == before


# Intent: a retained original attempt must match its entire body whenever its filename is still present.
# Why: retention tolerance permits disappearance while preserving evidence of a changed or ambiguous record.
def test_admitted_attempt_body_is_immutable(history_journal: Journal) -> None:
    original = unassigned()
    archive = NativeHistoryArchive(history_journal)
    archive.observe([original], [original], allow_missing=True)
    changed = copy.deepcopy(original)
    changed["MeterValues"][0]["Offer"] += 1
    replacement = row(1101) | {"filename": original["filename"]}
    for records in ([changed], [replacement], [original, replacement]):
        with pytest.raises(SecurityError):
            archive.observe([original], records, allow_missing=True)
    assert archive.observe([original], [], allow_missing=True)["archived_records"] == 0
    assert history_journal.db.execute("SELECT COUNT(*) FROM native_start_attempts").fetchone()[0] == 1


# Intent: a numbered original reappearing at its own filename as an unassigned attempt blocks reconciliation.
# Why: retention tolerance cannot turn a changed original into a missing transaction plus a new zero-draw attempt.
def test_numbered_original_cannot_reappear_as_unassigned(history_journal: Journal) -> None:
    original = row(1101)
    archive = NativeHistoryArchive(history_journal)
    archive.observe([original], [original], allow_missing=True)
    before = contents(history_journal)
    with pytest.raises(SecurityError):
        archive.observe([original], [unassigned(original["filename"])], allow_missing=True)
    assert contents(history_journal) == before


# Intent: ordinary strict audits also archive observed start attempts without granting missing-history tolerance.
# Why: a failed start can subsequently roll out of the firmware's history on either retention policy.
def test_strict_audit_retains_attempts(history_journal: Journal) -> None:
    import json

    from ics_proxy_core.engine.native_history import audit_history

    original, attempt = row(1101), unassigned()
    history_journal.put("native_bounded_handover", {"original_history": [original]})
    audit_history(history_journal, "synthetic", [original, attempt], False)
    audit_history(history_journal, "synthetic", [original], False)
    with pytest.raises(SecurityError):
        audit_history(history_journal, "synthetic", [attempt], False)
    changed = original | {"meterStart": float(original["meterStart"])}
    assert changed == original
    before = contents(history_journal)
    with pytest.raises(SecurityError):
        audit_history(history_journal, "synthetic", [changed, unassigned("different")], False)
    assert contents(history_journal) == before
    assert [json.loads(r[0]) for r in history_journal.db.execute("SELECT body FROM native_start_attempts")] == [attempt]
    assert history_journal.get("native_history_policy") is None


# Intent: a repeated complete archive observation uses the retained records without making a new write.
# Why: an idle poll must not depend on writing already-stored history every time it sees the same charger records.
def test_unchanged_archive_observation_uses_saved_history(history_journal: Journal) -> None:
    original, later, attempt = row(1001), row(1002), unassigned()
    archive = NativeHistoryArchive(history_journal)
    current = [later, attempt]
    expected = archive.observe([original], current, allow_missing=True)
    before = contents(history_journal)
    history_journal.db.execute(
        "CREATE TEMP TRIGGER no_repeat_archive BEFORE INSERT ON native_history_records "
        "BEGIN SELECT RAISE(ABORT,'unchanged record already archived'); END"
    )
    history_journal.db.execute(
        "CREATE TEMP TRIGGER no_repeat_attempt BEFORE INSERT ON native_start_attempts "
        "BEGIN SELECT RAISE(ABORT,'unchanged attempt already archived'); END"
    )
    assert archive.observe([original], current, allow_missing=True) == expected
    assert contents(history_journal) == before
