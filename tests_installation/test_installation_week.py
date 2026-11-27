"""One advancing simulated installation across a week and two standalone builds."""

import json
import os
import shutil
import sys
from pathlib import Path

from tests.model_tls_support import model_tls as model_tls
from tests.model_tls_support import tls_for_case
from tests.process_helpers import process_rig
from tests.recovery_barriers import require_zero_held
from tests_ha_proxy.conftest import actual_core, setup_proxy
from tests_installation.migrated_layout import seed_old_layout, verify_layout
from tests_installation.scheduled_week import ScheduledWeek, WeekPeer, press_button


# Check that the actual process and saved file are those used by the rehearsal, rather than backups or copies.
def verify_build(proxy, source, journal, original, layout, python=None):
    process = Path(f"/proc/{proxy.process.pid}")
    assert (process / "cwd").resolve() == source
    assert (process / "cmdline").read_bytes().split(b"\0", 1)[0] == os.fsencode(python or sys.executable)
    current = journal.stat()
    assert (current.st_dev, current.st_ino) == (original.st_dev, original.st_ino)
    verify_layout(journal, layout)


# Wait for the selected current to be both saved and usable and refresh the actual Home Assistant entity.
async def verify_current(proxy, hass, entry, amperes):
    await proxy.status(
        lambda state: (
            state.get("owner_control_enabled") is True
            and state.get("requested_current_a") == state.get("station_ceiling_a") == amperes
            and state.get("can_set_current") is True
        )
    )
    await entry.runtime_data.async_refresh()
    assert hass.states.get("number.ics_proxy_charge_current_a").state == str(amperes)
    assert hass.states.get("sensor.ics_proxy_control_mode").state == "App / schedule"


# Older builds echo private Pause bindings and omit the caller revision from the wire receipt.
def assert_same_pause_receipt(receipt, expected, saved):
    assert expected.get("revision", saved["revision"]) == saved["revision"]
    assert receipt.get("revision", saved["revision"]) == saved["revision"]
    stored_result = json.loads(saved["result"])
    assert stored_result["pause_verified"] is True
    public_result = {key: value for key, value in stored_result.items() if key not in ("_pause", "_stop")}
    normalized = []
    for value in (receipt, expected):
        result = value["result"].copy()
        for key in ("_pause", "_stop"):
            if key in result:
                assert result.pop(key) == stored_result[key]
        assert result == public_result
        normalized.append({**value, "revision": saved["revision"], "result": result})
    assert normalized[0] == normalized[1]


# A stopped fixture must leave a complete main database before copying or replacing it.
def require_checkpointed(journal):
    for suffix in ("-wal", "-shm", "-journal"):
        assert not journal.with_name(journal.name + suffix).exists()


# Rehearse the pre-update backup path while the charger stays closed under the same explicit Pause.
async def rehearse_paused_backup(week, hass, journal, original, builds):
    current, previous, layout, previous_python = builds
    proxy = week.proxy
    request_ids = {row["id"] for row in proxy.durable()["requests"]}
    await press_button(hass, "pause")
    paused = await proxy.status(lambda state: state.get("local_hold_verified") is True and state.get("power_w") == 0)
    snapshot = proxy.durable()
    (saved_pause,) = [row for row in snapshot["requests"] if row["id"] not in request_ids]
    assert saved_pause["action"] == "pause" and saved_pause["state"] == "completed"
    pause = await proxy.completed_control(saved_pause["id"])
    await proxy.close()
    assert proxy.process.returncode == 0
    require_checkpointed(journal)
    backup = journal.with_name("pre-update.db")
    shutil.copy2(journal, backup)

    mark = len(week.bench.device_calls)
    await proxy.start(source=current)
    verify_build(proxy, current, journal, original, layout)
    await proxy.status(lambda state: state.get("local_hold_verified") is True and state.get("power_w") == 0)
    week.verify_history()
    await proxy.close()
    assert proxy.process.returncode == 0
    require_checkpointed(journal)
    # Keep both exact database versions; the earlier service gets a fresh copy of the backup.
    journal.rename(journal.with_name("after-update.db"))
    shutil.copy2(backup, journal)
    restored = journal.stat()

    await proxy.start(source=previous, python=previous_python)
    verify_build(proxy, previous, journal, restored, layout, previous_python)
    recovered = await proxy.status(
        lambda state: state.get("local_hold_verified") is True and state.get("can_return") is True
    )
    assert recovered["control_revision"] == paused["control_revision"]
    assert recovered["requested_current_a"] == paused["requested_current_a"] and recovered["power_w"] == 0
    assert_same_pause_receipt(await proxy.completed_control(pause["id"]), pause, saved_pause)
    assert proxy.durable()["requests"] == snapshot["requests"]
    week.verify_history()
    require_zero_held(proxy.rig, mark)


# Intent: retain the same week of charging, owner controls, and migrated journal while updating and going back.
# Why: a new journal or a simulated app reply would miss failures that depend on previously saved work.
async def test_week_on_migrated_installation(tmp_path, model_tls, unused_tcp_port_factory, monkeypatch, builds):
    current, previous, layout, previous_python = builds
    journal = tmp_path / "state.db"
    seed_old_layout(journal)
    original = journal.stat()
    port = unused_tcp_port_factory
    async with (
        process_rig(
            tmp_path,
            tls_for_case(tmp_path, model_tls),
            port(),
            port(),
            monkeypatch,
            native_peer=WeekPeer,
            timing="fast-poll",
        ) as proxy,
        actual_core(tmp_path / "home-assistant") as hass,
    ):
        await proxy.start(source=previous, python=previous_python)
        await proxy.status()
        verify_build(proxy, previous, journal, original, layout, previous_python)
        entry = await setup_proxy(hass, proxy.port)
        week = ScheduledWeek(proxy)

        await week.finish(await week.start(0, 6))
        await verify_current(proxy, hass, entry, 6)
        await hass.services.async_call(
            "number", "set_value", {"entity_id": "number.ics_proxy_charge_current_a", "value": 16}, blocking=True
        )
        await verify_current(proxy, hass, entry, 16)
        await week.reconnect_vehicle(16)
        charge = await week.start(1, 16)
        await proxy.native_outage(16, True)
        assert week.firmware.active == charge.local
        await week.finish(charge)

        await proxy.close()
        assert proxy.process.returncode == 0
        await proxy.start(source=current)
        verify_build(proxy, current, journal, original, layout)
        await verify_current(proxy, hass, entry, 16)
        week.verify_history()
        await week.finish(await week.start(2, 16))

        pause = await week.pause_at_start(hass, entry, await week.start(3, 16))
        await proxy.status(lambda state: state.get("local_hold_verified") is True and state.get("power_w") == 0)
        await entry.runtime_data.async_refresh()
        assert hass.states.get("sensor.ics_proxy_control_mode").state == "Paused locally"
        assert week.firmware.active is None
        await week.refuse_app_start("owner-after-closure")
        candidate = proxy.durable()
        (saved_pause,) = [row for row in candidate["requests"] if row["id"] == pause["id"]]
        intent = candidate["metadata"]["managed_control_intent"]
        claim = candidate["metadata"]["managed_owner_pause"]
        assert intent == {"mode": "paused", "revision": claim["revision"]}
        assert all(row["state"] in ("completed", "failed") for row in candidate["requests"])
        week.verify_history()
        mark = len(week.bench.device_calls)
        require_zero_held(proxy.rig, mark)

        await proxy.close()
        assert proxy.process.returncode == 0
        verify_layout(journal, layout)
        await proxy.start(source=previous, python=previous_python)
        verify_build(proxy, previous, journal, original, layout, previous_python)
        recovered = await proxy.status(
            lambda state: state.get("local_hold_verified") is True and state.get("can_return") is True
        )
        assert recovered["control_revision"] == intent["revision"] and recovered["requested_current_a"] == 16
        assert recovered["power_w"] == 0
        assert_same_pause_receipt(await proxy.completed_control(pause["id"]), pause, saved_pause)
        same_file = proxy.durable()
        assert same_file["metadata"]["managed_control_intent"] == intent
        assert same_file["metadata"]["managed_owner_pause"] == claim
        assert same_file["requests"] == candidate["requests"]
        require_zero_held(proxy.rig, mark)
        week.verify_history()
        await entry.runtime_data.async_refresh()
        assert hass.states.get("sensor.ics_proxy_control_mode").state == "Paused locally"

        await press_button(hass, "app_control")
        await verify_current(proxy, hass, entry, 16)
        for day in range(4, 7):
            await week.finish(await week.start(day, 16))
        assert len(week.records) == len({row["local"] for row in week.records}) == 7
        assert len(week.cloud_pairs) == 14
        assert_same_pause_receipt(await proxy.completed_control(pause["id"]), pause, saved_pause)
        assert saved_pause in proxy.durable()["requests"]
        assert len(week.bench.history) == len(week.starting_history) + 7
        verify_build(proxy, previous, journal, original, layout, previous_python)
        await rehearse_paused_backup(week, hass, journal, original, builds)
