"""Owner-authorized sessions remain local across a vendor outage and proxy replacement."""

import asyncio
import json
import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, datetime, timedelta

from ics_proxy_core import commands
from ics_proxy_core.engine.auth_info import tag_key
from ics_proxy_core.engine.trial_session import save_private
from tests.managed_rig import starts
from tests.native_schedule_support import SchedulePeer
from tests.process_helpers import process_rig
from tests.prototype_rig import START
from tests.runtime_rig import until
from tests.test_managed_vendor_catchup import CatchupBench
from tests.tls_support import tls as tls

OWNER_TAG = "HOUSEHOLD_LOCAL"


def provision_local_start(child):
    path = child.directory / "trial.json"
    config = json.loads(path.read_text())
    config["owner_start_tag"] = OWNER_TAG
    save_private(path, config)
    expired = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    info = json.dumps({"status": "Accepted", "expiryDate": expired})
    with closing(sqlite3.connect(child.rig.prepared.config.journal)) as db, db:
        for tag in config["local_tags"]:
            db.execute(
                "INSERT OR REPLACE INTO auth_info(digest,info,observed) VALUES(?,?,?)",
                (tag_key(tag), info, datetime.now(UTC).timestamp()),
            )


def local_start_ready(status):
    return status.get("cloud_connected") is False and status.get("can_start") is True


def retained_meters(child, local):
    path = child.rig.prepared.config.journal.as_uri()
    with closing(sqlite3.connect(path + "?mode=ro", uri=True)) as db:
        return [
            json.loads(row[0])
            for row in db.execute("SELECT payload FROM owner_meter_values WHERE local=? ORDER BY sequence", (local,))
        ]


async def check_vendor_profile_blocked(bench):
    before = len(bench.device_calls)
    profile = commands.set_charging_profile(1, commands.PURPOSE_TX_DEFAULT, 16, 1001, stack_level=0)
    answer = await bench.cloud_call(uuid.uuid4().hex, "SetChargingProfile", profile)
    assert answer[0] == 4 and answer[2] == "SecurityError", answer
    assert not any(wire[2] == "SetChargingProfile" for wire in bench.device_calls[before:])


async def charge_locally(child, minute, start_count, *, reconnect_before_report=False):
    bench, peer = child.rig.bench, child.rig.peer
    ready = await child.status(local_start_ready)
    reply = await child.request("start", revision=ready["control_revision"])
    assert reply["ok"], reply
    await until(lambda: len(starts(child.rig)) == start_count)
    assert starts(child.rig)[-1][3]["idTag"] == OWNER_TAG
    if reconnect_before_report:
        bench.vendor_available = True
        await child.status(lambda value: value.get("cloud_connected") is True)
        await check_vendor_profile_blocked(bench)
    authorization = await bench.call(child.socket, uuid.uuid4().hex, "Authorize", {"idTag": OWNER_TAG})
    assert authorization[0] == 3 and authorization[2]["idTagInfo"]["status"] == "Accepted"

    begin = f"2026-01-05T01:{minute:02}:00Z"
    end = f"2026-01-05T01:{minute + 1:02}:00Z"
    meter_start = 10_000 + minute * 100
    start_message = START | {"idTag": OWNER_TAG, "timestamp": begin, "meterStart": meter_start}
    start = await bench.call(child.socket, uuid.uuid4().hex, "StartTransaction", start_message)
    assert start[0] == 3 and start[2]["idTagInfo"]["status"] == "Accepted", start
    local_id = start[2]["transactionId"]
    peer.plan_session = (local_id, 987654)
    peer.paused, peer.permission_enabled = False, True
    bench.cp, bench.connection = "C", "Charging"
    bench.firmware.fire_plan(9)
    active = await child.status(lambda value: value.get("power_w") == 7680 and value.get("stop_target") is not None)
    await child.completed_control(reply["result"]["id"])
    if reconnect_before_report:
        await check_vendor_profile_blocked(bench)
    meter = {
        "connectorId": 1,
        "transactionId": local_id,
        "meterValue": [{"timestamp": begin, "sampledValue": [{"measurand": "Power.Active.Import", "value": "7680"}]}],
    }
    assert (await bench.call(child.socket, uuid.uuid4().hex, "MeterValues", meter))[0] == 3

    bench.remote_stop.clear()
    stop = await child.request("stop_session", revision=active["control_revision"], target=active["stop_target"])
    assert stop["ok"], stop
    await asyncio.wait_for(bench.remote_stop.wait(), 5)
    receipt = await child.request("receipt", request_id=stop["result"]["id"])
    assert receipt["ok"] and receipt["result"]["state"] != "completed"
    stop_targets = [wire[3]["transactionId"] for wire in bench.device_calls if wire[2] == "RemoteStopTransaction"]
    assert stop_targets[-1] == local_id
    stop_message = {"transactionId": local_id, "timestamp": end, "meterStop": meter_start + 128}
    assert (await bench.call(child.socket, uuid.uuid4().hex, "StopTransaction", stop_message))[0] == 3
    peer.permission_enabled = False
    await child.completed_control(stop["result"]["id"])
    if reconnect_before_report:
        await child.status(lambda value: value.get("cloud_connected") is True and value.get("power_w") == 0)
    else:
        await child.status(lambda value: local_start_ready(value) and value.get("power_w") == 0)
    return local_id, start_message, meter, stop_message


async def test_two_local_sessions_and_process_replacement_with_expired_vendor_approval(
    tmp_path, tls, unused_tcp_port_factory, monkeypatch
):
    async with process_rig(
        tmp_path,
        tls,
        unused_tcp_port_factory(),
        unused_tcp_port_factory(),
        monkeypatch,
        32,
        timing="fast-poll",
        native_peer=SchedulePeer,
        bench_type=CatchupBench,
    ) as child:
        provision_local_start(child)
        await child.start()
        await child.status()
        bench = child.rig.bench
        await bench.vendor_outage()
        await child.status(local_start_ready)
        unprompted = await bench.call(child.socket, uuid.uuid4().hex, "Authorize", {"idTag": OWNER_TAG})
        assert unprompted[0] == 4 or unprompted[2]["idTagInfo"]["status"] != "Accepted"

        prior_rows = child.durable()["transactions"]
        sessions = [await charge_locally(child, minute, cycle) for cycle, minute in enumerate((0, 2), 1)]
        assert sessions[0][0] != sessions[1][0]
        assert [wire[2] for wire in bench.device_calls].count("RemoteStopTransaction") == 2
        snapshot = child.durable()
        assert snapshot["transactions"][: len(prior_rows)] == prior_rows
        for local, start, meter, stop in sessions:
            row = next(row for row in snapshot["transactions"] if row["local"] == local)
            assert row["vendor"] is None and json.loads(row["start"]) == start and json.loads(row["stop"]) == stop
            assert not any(item["local"] == local for item in snapshot["outbox"])
            assert retained_meters(child, local) == [meter]

        await child.kill()
        await child.start()
        recovered = await child.status(local_start_ready)
        assert recovered["local_hold_verified"] and recovered["station_ceiling_a"] == recovered["power_w"] == 0
        assert recovered["requested_current_a"] == recovered["native_limit_a"] == 32
        assert child.durable()["transactions"] == snapshot["transactions"]
        for local, _, meter, _ in sessions:
            assert retained_meters(child, local) == [meter]
        assert len(starts(child.rig)) == 2

        bench.vendor_available = True
        await child.status(lambda value: value.get("cloud_connected") is True and not value["pending_observations"])
        assert child.durable()["transactions"] == snapshot["transactions"]
        assert not [
            wire
            for wire in bench.calls
            if wire[0] == 2 and wire[2] in ("Authorize", "StartTransaction") and wire[3].get("idTag") == OWNER_TAG
        ]


async def test_vendor_reconnect_before_owner_report_cannot_change_the_local_attempt(
    tmp_path, tls, unused_tcp_port_factory, monkeypatch
):
    async with process_rig(
        tmp_path,
        tls,
        unused_tcp_port_factory(),
        unused_tcp_port_factory(),
        monkeypatch,
        32,
        timing="fast-poll",
        native_peer=SchedulePeer,
        bench_type=CatchupBench,
    ) as child:
        provision_local_start(child)
        await child.start()
        await child.status()
        await child.rig.bench.vendor_outage()
        local, _, _, _ = await charge_locally(child, 0, 1, reconnect_before_report=True)
        row = next(row for row in child.durable()["transactions"] if row["local"] == local)
        assert row["vendor"] is None and row["stop"] is not None
        assert not [
            wire
            for wire in child.rig.bench.calls
            if wire[0] == 2 and wire[2] in ("Authorize", "StartTransaction") and wire[3].get("idTag") == OWNER_TAG
        ]


async def test_rejected_owner_start_cannot_authorize_or_open_a_session(
    tmp_path, tls, unused_tcp_port_factory, monkeypatch
):
    async with process_rig(
        tmp_path,
        tls,
        unused_tcp_port_factory(),
        unused_tcp_port_factory(),
        monkeypatch,
        32,
        timing="fast-poll",
        native_peer=SchedulePeer,
        bench_type=CatchupBench,
    ) as child:
        provision_local_start(child)
        await child.start()
        await child.status()
        bench = child.rig.bench
        await bench.vendor_outage()
        ready = await child.status(local_start_ready)
        original = bench.firmware.answer

        def reject_start(action, payload):
            return {"status": "Rejected"} if action == "RemoteStartTransaction" else original(action, payload)

        monkeypatch.setattr(bench.firmware, "answer", reject_start)
        transactions = child.durable()["transactions"]
        assert (await child.request("start", revision=ready["control_revision"]))["ok"]
        await until(lambda: len(starts(child.rig)) == 1)
        await until(lambda: bench.firmware.effective_limit() == 0)
        authorization = await bench.call(child.socket, uuid.uuid4().hex, "Authorize", {"idTag": OWNER_TAG})
        assert authorization[0] == 3 and authorization[2]["idTagInfo"]["status"] == "Invalid"
        reported = await bench.call(child.socket, uuid.uuid4().hex, "StartTransaction", START | {"idTag": OWNER_TAG})
        assert reported[0] == 4 and child.durable()["transactions"] == transactions


async def test_pause_revokes_owner_authorize_but_retains_the_issued_report_at_zero(
    tmp_path, tls, unused_tcp_port_factory, monkeypatch
):
    async with process_rig(
        tmp_path,
        tls,
        unused_tcp_port_factory(),
        unused_tcp_port_factory(),
        monkeypatch,
        32,
        timing="fast-poll",
        native_peer=SchedulePeer,
        bench_type=CatchupBench,
    ) as child:
        provision_local_start(child)
        await child.start()
        await child.status()
        bench = child.rig.bench
        await bench.vendor_outage()
        ready = await child.status(local_start_ready)
        assert (await child.request("start", revision=ready["control_revision"]))["ok"]
        accepted = await child.status(
            lambda value: (value.get("local_charge") or {}).get("state") == "awaiting_session"
        )
        pause = await child.request("pause", revision=accepted["control_revision"])
        assert pause["ok"], pause
        await until(lambda: bench.firmware.effective_limit() == 0)
        authorization = await bench.call(child.socket, uuid.uuid4().hex, "Authorize", {"idTag": OWNER_TAG})
        assert authorization[0] == 3 and authorization[2]["idTagInfo"]["status"] == "Invalid"

        report = await bench.call(child.socket, uuid.uuid4().hex, "StartTransaction", START | {"idTag": OWNER_TAG})
        assert report[0] == 3 and report[2]["idTagInfo"]["status"] == "Accepted", report
        local = report[2]["transactionId"]
        child.rig.peer.plan_session = (local, 987654)
        assert bench.firmware.effective_limit() == 0
        await asyncio.wait_for(bench.remote_stop.wait(), 5)
        stop_targets = [wire[3]["transactionId"] for wire in bench.device_calls if wire[2] == "RemoteStopTransaction"]
        assert stop_targets == [local]
        stop = {"transactionId": local, "timestamp": "2026-01-01T00:01:00Z", "meterStop": START["meterStart"]}
        assert (await bench.call(child.socket, uuid.uuid4().hex, "StopTransaction", stop))[0] == 3
        await child.completed_control(pause["result"]["id"])
        assert len(starts(child.rig)) == 1
