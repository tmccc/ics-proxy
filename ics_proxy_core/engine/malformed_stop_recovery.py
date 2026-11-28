"""Close a rejected Stop only from the matching commissioned native transaction record."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from .events import event_time
from .native_history import NativeHistoryReading
from .native_start_attempts import start_millisecond_range
from .policy import DeniedError, number
from .session_closure import ClosurePosition, observe_closure
from .store import fingerprint

if TYPE_CHECKING:
    from .native_bounded_handover import NativeBoundedHandover
    from .trial_handover_evidence import HandoverEvidence


# A still-valid field cannot contradict native evidence. A clock reversal needs a matching wire timestamp.
def _agrees_with_report(
    report: dict[str, Any], meter_start: int, meter_stop: int, stop_milliseconds: int, *, timestamp_required: bool
) -> bool:
    try:
        meter = number(report.get("meterStop"), 10**12)
    except DeniedError:
        meter = None
    if meter is not None and meter >= meter_start and meter != meter_stop:
        return False
    stamp = report.get("timestamp")
    try:
        occurred = event_time(stamp)
    except DeniedError:
        return not timestamp_required
    assert isinstance(stamp, str)
    return stop_milliseconds in start_millisecond_range(stamp, occurred)


# Bind the native row to the accepted Start at its original precision before using its Stop fields.
def _recovered_stop(
    record: dict[str, Any], start: dict[str, Any], report: dict[str, Any], observed_at: float
) -> dict[str, Any] | None:
    reading = NativeHistoryReading.read(record, observed_at)
    start_ms, stop_ms = record.get("startMs"), record.get("stopMs")
    record_tag, start_tag = record.get("idTagStart"), start.get("idTag")
    connector = record.get("connectorId")
    if (
        reading.identifier is None
        or reading.start is None
        or not reading.stop
        or reading.meter_start is None
        or reading.meter_stop is None
        or reading.meter_stop < reading.meter_start
        or type(start_ms) is not int
        or not 0 <= start_ms <= 999
        or type(stop_ms) is not int
        or not 0 <= stop_ms <= 999
        or type(connector) is not int
        or connector != 1
        or start.get("connectorId") != connector
        or not isinstance(record_tag, str)
        or not isinstance(start_tag, str)
        or record_tag.casefold() != start_tag.casefold()
        or reading.meter_start != start.get("meterStart")
        or ("reservationId" in start and record.get("reservationId") != start["reservationId"])
    ):
        return None
    stamp = start.get("timestamp")
    try:
        started = event_time(stamp)
    except DeniedError:
        return None
    assert isinstance(stamp, str)
    start_milliseconds, stop_milliseconds = reading.start * 1000 + start_ms, reading.stop * 1000 + stop_ms
    if start_milliseconds not in start_millisecond_range(stamp, started) or not _agrees_with_report(
        report,
        reading.meter_start,
        reading.meter_stop,
        stop_milliseconds,
        timestamp_required=stop_milliseconds < start_milliseconds,
    ):
        return None
    stopped = datetime.fromtimestamp(reading.stop, UTC).replace(microsecond=stop_ms * 1000)
    return {
        "transactionId": reading.identifier,
        "timestamp": stopped.isoformat(timespec="milliseconds" if stop_ms else "seconds").replace("+00:00", "Z"),
        "meterStop": reading.meter_stop,
    }


# Called inside the full native audit's owned decision while its Boot, resources and lifecycle remain current.
def recover_malformed_stop(handover: NativeBoundedHandover, native: HandoverEvidence) -> None:
    gateway, physical = handover.gateway, native.physical
    plugged = physical.cp == "B" and physical.connection == "Plug"
    unplugged = handover.allow_unplugged and physical.cp == "A" and physical.connection == "Unplug"
    if (
        physical.transaction_open
        or physical.current_a != 0
        or len(native.currents_a) != 3
        or len(native.powers_w) != 4
        or not (plugged or unplugged)
    ):
        return
    journal = gateway.journal
    local = journal.active()
    if local is None or not (marker := journal.malformed_stop(local)) or marker.get("recovered") is not False:
        return
    observation = observe_closure(local, native)
    if (
        not observation.fresh
        or observation.position is not ClosurePosition.NATIVE_FIRST
        or not gateway.current_boot_seen(gateway._charger_epoch)
        or not handover.commissioned_binding_matches(native)
    ):
        return
    row = journal.db.execute(
        "SELECT start,vendor FROM transactions WHERE local=? AND stop IS NULL", (local,)
    ).fetchone()
    if row is None:
        return
    report, start = marker.get("report"), json.loads(row["start"])
    if (
        not isinstance(report, dict)
        or type(report.get("transactionId")) is not int
        or report["transactionId"] != local
        or fingerprint(start) != marker.get("start")
    ):
        return
    matching = [record for record in native.history if record.get("transactionId") == local]
    if len(matching) != 1 or not (stop := _recovered_stop(matching[0], start, report, physical.observed_at)):
        return
    # A locally authorized session can close without iCS. A mapped session needs its restrictive station hold.
    present = handover.book.owned_present(physical, record_unchanged=False)
    if not journal.is_owner_session(local) and (
        row["vendor"] is None or native.output_limit_a != 0 or handover.station_zero_refusal(present) is not None
    ):
        return
    record = matching[0]
    journal.stop(
        f"malformed-stop:{local}",
        stop,
        gateway.authorisation.retire,
        recovered_native={
            **{
                key: record[key] for key in ("transactionId", "meterStop", "startTime", "startMs", "stopTime", "stopMs")
            },
            "identity_digest": physical.identity_digest,
            "hardware_digest": native.hardware_digest,
            "observed_at": physical.observed_at,
            "record_digest": fingerprint(record),
        },
    )
