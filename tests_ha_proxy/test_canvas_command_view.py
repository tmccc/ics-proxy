"""Actual Home Assistant publishes and consumes one versioned display offer."""

import asyncio
import re
from types import SimpleNamespace

import pytest

from tests_ha_proxy.conftest import setup_proxy
from tests_ha_proxy.test_client import peer as peer

ENTITY = "sensor.ics_proxy_display_command"
OWNER = "a" * 64
NEW_OWNER = "c" * 64
TARGET = "b" * 64


@pytest.fixture
def frame_clock(monkeypatch):
    from custom_components.ics_proxy import coordinator

    clock = SimpleNamespace(now=1000)
    monkeypatch.setattr(coordinator, "frame_clock", lambda: (clock.now, clock.now))
    return clock


def offer(hass, action, feedback="idle"):
    value = hass.states.get(ENTITY).state
    match = re.fullmatch(rf"v2:{feedback}:{action}:([a-f0-9]{{32}})", value)
    assert match is not None, value
    return match[1]


async def submit(hass, action, generation, **fields):
    return await hass.services.async_call(
        "ics_proxy",
        "session_command",
        {"action": action, "canvas_generation": generation, **fields},
        blocking=True,
        return_response=True,
    )


# Intent: retire the exact offer before a network await and regenerate only after updated readiness.
# Why: a Stop can finish without advancing the proxy's control revision.
async def test_canvas_stop_is_single_use_at_the_same_revision(actual_hass, peer, frame_clock):
    peer["state"].update(canvas_owner=OWNER, charging_session_open=True, stop_target=TARGET, can_start=False)
    entry = await setup_proxy(actual_hass, peer["port"])
    previous = offer(actual_hass, "stop")
    await entry.runtime_data.async_refresh()
    assert offer(actual_hass, "stop") == previous
    frame_clock.now = 1002

    entered, release = asyncio.Event(), asyncio.Event()

    async def wait_for_write():
        entered.set()
        await release.wait()

    peer["before_handshake"] = wait_for_write
    frame = {"canvas_rendered_at_epoch": 1001}
    assert await submit(actual_hass, "stop_session", previous) == {"confirmed": False}
    assert await submit(actual_hass, "stop_session", previous, target=TARGET, **frame) == {"confirmed": False}
    command = asyncio.create_task(submit(actual_hass, "stop_session", previous, **frame))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert actual_hass.states.get(ENTITY).state == "v2:stopping:none:-"
        assert await submit(actual_hass, "stop_session", previous, **frame) == {"confirmed": False}
        assert "stop_session" not in peer["commands"]
    finally:
        release.set()
    assert await command == {"confirmed": True}
    assert offer(actual_hass, "start") != previous
    (request,) = [item for item in peer["requests"] if item["command"] == "stop_session"]
    assert (request["revision"], request["target"], request["canvas_owner"]) == (0, TARGET, OWNER)
    assert peer["state"]["control_revision"] == 0

    peer["state"].update(charging_session_open=True, stop_target=TARGET, can_start=False)
    await entry.runtime_data.async_refresh()
    replacement = offer(actual_hass, "stop")
    assert replacement != previous
    assert await submit(actual_hass, "stop_session", previous, **frame) == {"confirmed": False}
    assert peer["commands"].count("stop_session") == 1
    frame_clock.now = 1004
    assert await submit(actual_hass, "stop_session", replacement, canvas_rendered_at_epoch=1003) == {"confirmed": True}
    assert peer["commands"].count("stop_session") == 2


# Intent: invalidate a claimed offer if the observed charger owner changes during the handshake.
# Why: the service must derive and recheck the same exact destination as the displayed generation.
async def test_canvas_claim_keeps_its_owner_at_write(actual_hass, peer, frame_clock):
    peer["state"]["canvas_owner"] = OWNER
    entry = await setup_proxy(actual_hass, peer["port"])
    previous = offer(actual_hass, "start")
    frame_clock.now = 1002

    async def replace_owner():
        peer["state"]["canvas_owner"] = NEW_OWNER
        await entry.runtime_data.async_refresh()

    peer["before_handshake"] = replace_owner
    result = await submit(actual_hass, "start", previous, canvas_rendered_at_epoch=1001)
    assert result == {"confirmed": False}
    assert "start" not in peer["commands"]
    assert offer(actual_hass, "start") != previous


# Intent: retire a pending write when status is lost even if exactly the same offer returns during the handshake.
# Why: a previously claimed generation cannot regain authority after an observed status interruption.
async def test_canvas_claim_stays_retired_after_status_reconnect(actual_hass, peer, frame_clock):
    peer["state"]["canvas_owner"] = OWNER
    entry = await setup_proxy(actual_hass, peer["port"])
    previous = offer(actual_hass, "start")
    frame_clock.now = 1002

    async def reconnect():
        peer["status_failure"] = True
        await entry.runtime_data.async_refresh()
        assert actual_hass.states.get(ENTITY).state == "v2:unavailable:none:-"
        peer["status_failure"] = False
        await entry.runtime_data.async_refresh()
        assert actual_hass.states.get(ENTITY).state == "v2:starting:none:-"

    peer["before_handshake"] = reconnect
    assert await submit(actual_hass, "start", previous, canvas_rendered_at_epoch=1001) == {"confirmed": False}
    assert "start" not in peer["commands"]
    assert offer(actual_hass, "start", feedback="starting_timeout") != previous


# Intent: keep unconfirmed feedback for the elapsed display interval, then retire only its message.
# Why: wall clock changes do not extend feedback, and a fresh valid offer remains usable.
async def test_canvas_feedback_expires_after_an_unconfirmed_same_revision(actual_hass, peer, frame_clock, monkeypatch):
    from custom_components.ics_proxy import canvas_command

    timers = []

    def schedule(hass, elapsed, expire):
        assert elapsed == 20
        timers.append(expire)
        return lambda: None

    monkeypatch.setattr(canvas_command, "async_call_later", schedule)
    peer["state"].update(canvas_owner=OWNER, charging_session_open=True, stop_target=TARGET, can_start=False)
    await setup_proxy(actual_hass, peer["port"])
    previous = offer(actual_hass, "stop")
    frame_clock.now = 1002
    peer["pre_admission_close"] = "Busy"
    assert await submit(actual_hass, "stop_session", previous, canvas_rendered_at_epoch=1001) == {"confirmed": False}
    replacement = offer(actual_hass, "stop", feedback="stopping_timeout")
    assert replacement != previous
    assert peer["state"]["control_revision"] == 0
    (expire,) = timers
    frame_clock.now = 900
    expire(None)
    assert offer(actual_hass, "stop") == replacement


# Intent: retain interrupted feedback across reload while retiring the original generation and queued write.
# Why: a replacement entry may only publish offers backed by its own fresh status.
async def test_canvas_reload_restores_only_interrupted_feedback(actual_hass, peer, frame_clock):
    peer["state"]["canvas_owner"] = OWNER
    entry = await setup_proxy(actual_hass, peer["port"])
    old_coordinator = entry.runtime_data
    previous = offer(actual_hass, "start")
    frame_clock.now = 1002
    entered, release = asyncio.Event(), asyncio.Event()

    async def wait_for_write():
        entered.set()
        await release.wait()

    peer["before_handshake"] = wait_for_write
    command = asyncio.create_task(submit(actual_hass, "start", previous, canvas_rendered_at_epoch=1001))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert actual_hass.states.get(ENTITY).state == "v2:starting:none:-"
        await actual_hass.config_entries.async_reload(entry.entry_id)
        assert entry.runtime_data is not old_coordinator
        assert offer(actual_hass, "start", feedback="starting_timeout") != previous
    finally:
        release.set()
    assert await command == {"confirmed": False}
    assert "start" not in peer["commands"]
