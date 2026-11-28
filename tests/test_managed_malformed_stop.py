"""Reconcile a malformed charger Stop over the real TLS and native links."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from ics_proxy_core.engine.malformed_stop_recovery import _recovered_stop
from ics_proxy_core.engine.rpc import UnavailableError
from tests.charge_rig import positive_flow, ready
from tests.managed_rig import healthy, starts
from tests.prototype_rig import START
from tests.runtime_rig import until
from tests.test_managed_local_schedule import connection, overnight, status
from tests.test_managed_offline_owner import OWNER_TAG
from tests.test_managed_vendor_catchup import CatchupBench
from tests.tls_support import tls as tls


def report_time(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def stop_body(journal, local):
    row = journal.db.execute("SELECT stop FROM transactions WHERE local=?", (local,)).fetchone()
    return json.loads(row[0]) if row[0] is not None else None


def close_native(bench, peer):
    bench.firmware.active = None
    bench.firmware.requested_current = 0
    peer.permission_enabled = False
    peer.plan_session = None
    bench.cp, bench.connection = "B", "Plug"


def native_history_row(start, stop):
    return {
        "transactionId": stop["transactionId"],
        "connectorId": start["connectorId"],
        "idTagStart": start["idTag"],
        "startTime": int(datetime.fromisoformat(start["timestamp"]).timestamp()),
        "startMs": 0,
        "stopTime": int(datetime.fromisoformat(stop["timestamp"]).timestamp()),
        "stopMs": 0,
        "meterStart": start["meterStart"],
        "meterStop": stop["meterStop"],
    }


async def owner_start(test, started_at, count, *, meter_start=START["meterStart"]):
    controller, journal = test.controller, test.session.journal
    bench, peer = test.rig.bench, test.rig.peer
    ready_state = await status(
        test, lambda s: s.get("cloud_connected") is False and s.get("normal_start_ready") is True
    )
    owner = uuid.uuid4().hex
    controller.command(owner, "start", ready_state["control_revision"])
    await controller.charging.settle()
    assert len(starts(test.rig)) == count and starts(test.rig)[-1][3]["idTag"] == OWNER_TAG
    accepted = await bench.call(test.charger, uuid.uuid4().hex, "Authorize", {"idTag": OWNER_TAG})
    assert accepted[0] == 3 and accepted[2]["idTagInfo"]["status"] == "Accepted"
    start = START | {"idTag": OWNER_TAG, "timestamp": started_at, "meterStart": meter_start}
    reply = await bench.call(test.charger, uuid.uuid4().hex, "StartTransaction", start)
    assert reply[0] == 3, reply
    local = reply[2]["transactionId"]
    assert journal.is_owner_session(local)
    peer.plan_session = (local, 987654)
    peer.paused, peer.permission_enabled = False, True
    bench.cp, bench.connection = "C", "Charging"
    bench.firmware.fire_plan(9)
    await status(test, lambda s: s.get("charging_session_open") is True and s.get("power_w", 0) > 0)
    return local, start


@pytest.mark.parametrize("fault", ["meter", "timestamp", "meter_after_rewind"])
async def test_malformed_mapped_stop_waits_for_exact_native_closure_and_cloud_ack(
    tmp_path, tls, unused_tcp_port, monkeypatch, fault
):
    async with ready(tmp_path, tls, unused_tcp_port, monkeypatch, bench_type=CatchupBench) as test:
        controller, journal = test.controller, test.session.journal
        bench, peer = test.rig.bench, test.rig.peer
        original_history = list(bench.history)
        began = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=2)
        clock_rewind = fault == "meter_after_rewind"
        ended = began + timedelta(seconds=-2 if clock_rewind else 60)
        local = await positive_flow(test, "daily", call_id="original-start", timestamp=report_time(began))
        await healthy(controller)
        mapped = journal.vendor_id(local)
        closed = {"transactionId": local, "meterStop": START["meterStart"] + 100, "timestamp": report_time(ended)}
        malformed = closed | (
            {"timestamp": ended.replace(tzinfo=None).isoformat()}
            if fault == "timestamp"
            else {"meterStop": START["meterStart"] - 1}
        )

        async def send_stop(body, token=None):
            return await bench.call(test.charger, token or uuid.uuid4().hex, "StopTransaction", body)

        def vendor_stops():
            return [frame for frame in bench.lifecycle_wires if frame[2] == "StopTransaction"]

        assert (await send_stop(malformed, "original-start"))[0] == 4
        assert journal.malformed_stop(local) is None
        assert (await send_stop(malformed, "faulty-stop"))[0] == 4
        assert journal.get(f"malformed_stop:{local}")["report"] == malformed
        history = native_history_row(START | {"idTag": "NATIVE_TIMER", "timestamp": report_time(began)}, closed)
        bench.history = [*original_history, history]
        await controller.tick()
        assert journal.active() == local and bench.firmware.active == local
        assert stop_body(journal, local) is None and not vendor_stops()

        # Native closure is independently observable before the history contains the exact matching row.
        close_native(bench, peer)
        bench.history = [*original_history, history | {"meterStart": START["meterStart"] + 1}]
        await controller.tick()
        assert journal.active() == local and not vendor_stops()

        # The timer device supplies complete native history without resending its malformed OCPP report.
        disagreement = "meterStop" if fault == "timestamp" else "stopTime"
        bench.history = [*original_history, history | {disagreement: history[disagreement] + 1}]
        await controller.tick()
        assert journal.active() == local and not vendor_stops()
        bench.history = [*original_history, history]
        bench.hold_stop = closed["timestamp"]
        try:
            async with asyncio.timeout(5):
                while not bench.stop_waiting.is_set():
                    await controller.tick()
                    await asyncio.sleep(0.02)
            assert stop_body(journal, local) == closed
            assert journal.get(f"malformed_stop:{local}")["report"] == malformed
            assert journal.active() is None and journal.next_delivery() is not None
            clock_note = journal.db.execute(
                "SELECT start_timestamp,stop_timestamp FROM transaction_clock_discontinuities WHERE local=?", (local,)
            ).fetchone()
            if clock_rewind:
                assert tuple(clock_note) == (report_time(began), report_time(ended))
            else:
                assert clock_note is None
            assert not controller.charging.available(controller.current_native_sample(), normal=True)
            with pytest.raises(UnavailableError, match="cloud Start awaits delivery of the recovered Stop"):
                test.session.gateway.controls.decisions.require_vendor_start_idle()
            next_start = START | {
                "idTag": "NATIVE_TIMER",
                "timestamp": report_time(ended + timedelta(seconds=30)),
                "meterStart": closed["meterStop"],
            }
            refused = await bench.call(test.charger, "before-stop-ack", "StartTransaction", next_start)
            assert refused[:3] == [4, "before-stop-ack", "InternalError"]
            assert len(vendor_stops()) == 1 and vendor_stops()[0][3] == closed | {"transactionId": mapped}
        finally:
            bench.release_stop.set()
        await until(lambda: journal.next_delivery() is None)
        await healthy(controller)

        assert (await send_stop(malformed, "faulty-stop"))[0] == 3
        assert (await send_stop(malformed))[0] == 3
        assert (await send_stop(closed | {"timestamp": ended.isoformat(), "reason": "Local"}))[0] == 3
        assert (await send_stop(closed | {"meterStop": closed["meterStop"] + 1}))[0] == 4
        assert stop_body(journal, local) == closed and len(vendor_stops()) == 1
        assert (
            await positive_flow(
                test,
                "daily",
                call_id="next-native-start",
                timestamp=report_time(ended + timedelta(seconds=30)),
                meter_start=history["meterStop"],
            )
        ) != local


@pytest.mark.parametrize(
    ("reported_time", "recovered"),
    [
        ("2026-01-01T00:00:00.250Z", True),
        ("2026-01-01T00:00:00.250", False),
        ("2026-01-01T00:00:00.251Z", False),
    ],
)
def test_malformed_stop_recovery_requires_corroboration_for_same_second_rewind(reported_time, recovered):
    start = START | {"timestamp": "2026-01-01T00:00:00.500Z"}
    stop = {"transactionId": 1, "meterStop": start["meterStart"] + 100, "timestamp": "2026-01-01T00:00:00.250Z"}
    history = native_history_row(start, stop) | {"startMs": 500, "stopMs": 250}
    report = stop | {"meterStop": start["meterStart"] - 1, "timestamp": reported_time}

    result = _recovered_stop(history, start, report, history["startTime"] + 60)

    assert result == (stop if recovered else None)


async def test_local_owner_malformed_stop_recovery_survives_proxy_replacement(
    tmp_path, tls, unused_tcp_port, monkeypatch
):
    async with overnight(tmp_path, tls, unused_tcp_port, monkeypatch, configured=False) as (rig, _):
        bench = rig.bench
        original_history = list(bench.history)
        began = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=2)
        ended = began + timedelta(minutes=1)
        async with connection(rig, "malformed-owner-boot") as test:
            local, start = await owner_start(test, report_time(began), 1)
            closed = {"transactionId": local, "meterStop": start["meterStart"] + 100, "timestamp": report_time(ended)}
            malformed = closed | {"timestamp": ended.replace(tzinfo=None).isoformat()}
            reply = await bench.call(test.charger, "malformed-owner-stop", "StopTransaction", malformed)
            assert reply[0] == 4 and test.session.journal.active() == local
            assert test.session.journal.get(f"malformed_stop:{local}")["report"] == malformed

        # Replace the actual controller and journal owner before native closure becomes visible.
        close_native(bench, rig.peer)
        bench.history = [*original_history, native_history_row(start, closed)]
        async with connection(rig, "malformed-owner-reboot") as test:
            journal = test.session.journal
            await status(test, lambda s: journal.active() is None and s.get("normal_start_ready") is True)
            assert stop_body(journal, local) == closed
            assert journal.get(f"malformed_stop:{local}")["report"] == malformed
            assert not bench.lifecycle_wires and journal.next_delivery() is None
            assert (await bench.call(test.charger, "owner-stop-retry", "StopTransaction", malformed))[0] == 3
            subsequent, _ = await owner_start(
                test, report_time(ended + timedelta(seconds=30)), 2, meter_start=closed["meterStop"]
            )
            assert subsequent != local and not bench.lifecycle_wires
