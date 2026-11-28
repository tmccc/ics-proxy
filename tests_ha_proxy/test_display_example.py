"""Exercise the real display automation in offline Home Assistant Core."""

import asyncio
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import condition, trigger
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.template import Template
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from tests_ha_proxy.conftest import setup_proxy
from tests_ha_proxy.test_client import peer as peer

replacement_peer = peer

PROJECT = Path(__file__).resolve().parents[1] / "examples" / "solar-display-2"
AUTOMATION = "automation.general_display_2_ics_charger_buttons"
DISPLAY_VIEW = "sensor.general_display_2_ics_control"
DISPLAY_REPLY = "sensor.general_display_2_ics_control_reply"
DISPLAY_REQUEST = "esphome.general_display_ics_view_request"
DISPLAY_DEVICE = "replace-with-esphome-device-id"
IMAGE = "image.ics_display_preview"
VIEW = "replace-with-editor-view-id"
TARGET = "b" * 64
CANVAS_OWNER = "c" * 64
REPLACEMENT_OWNER = "d" * 64
REVISION = 42


# Give canvas tests destination evidence while the ordinary client fixture can still model an older proxy.
@pytest.fixture
def canvas_peer(peer):
    peer["state"]["canvas_owner"] = CANVAS_OWNER
    return peer


# Decode the produced cells at the firmware's sample locations after RGB565 conversion.
def render_marker(html):
    cells = re.findall(r"left:(\d+)px;top:237px;width:3px;height:3px;background:#([a-f0-9]{6})", html)
    raw = ""
    for index, (left, color) in enumerate(cells):
        assert 2 * int(left) + 3 == 7 + 6 * index < 480
        rgb = tuple(int(color[i : i + 2], 16) for i in (0, 2, 4))
        r, g, b = (((value >> bits) * 255) // (255 >> bits) for value, bits in zip(rgb, (3, 2, 3)))
        assert 40 <= b <= 74
        assert 8 + (r >> 6) * 64 <= r <= 56 + (r >> 6) * 64
        assert 8 + (g >> 6) * 64 <= g <= 56 + (g >> 6) * 64
        raw += format((r >> 6) * 4 + (g >> 6), "x")
    return raw


# Intent: a same-control refresh preserves the render epoch and age of the original press.
# Why: replacing either value with the new frame's can make an expired or rewound tap look current.
def test_display_sends_the_freshness_of_the_pressed_frame():
    package = yaml.load((PROJECT / "esphome-canvas.yaml.example").read_text(), Loader=yaml.BaseLoader)
    page = next(page for page in package["lvgl"]["pages"] if page["id"] == "canvas_dashboard")
    touch = next(
        widget["button"] for widget in page["widgets"] if widget.get("button", {}).get("id") == "canvas_touch_button"
    )
    press = touch["on_press"][0]["lambda"]
    assert press.index("freshness.fresh(now, clock.timestamp)") < press.index("frame.pressed_freshness() = freshness")
    click = touch["on_click"][0]["if"]
    guard = click["condition"]["lambda"]
    assert "charger_button ? frame.pressed_freshness() : frame.freshness()" in guard
    assert "const uint32_t touch_age = touch_frame.render_age_ms(now, clock.timestamp);" in guard
    assert guard.index("touch_age >= solar_display::CanvasFreshness::MAX_AGE_MS") < guard.index("return true")
    assert "id(canvas_touch_age_seconds) = touch_age / 1000;" in guard
    assert "id(canvas_touch_rendered_at_epoch) = touch_frame.rendered_at();" in guard
    event = click["then"][0]["homeassistant.event"]["data"]
    assert event["canvas_age_seconds"] == "return to_string(id(canvas_touch_age_seconds));"
    assert event["canvas_rendered_at_epoch"] == "return to_string(id(canvas_touch_rendered_at_epoch));"


# The renderer prefetches only entity IDs it reads directly from its template.
# Intent: confirm the renderer and the ESPHome configuration consume the same command entity.
# Why: the pictured control and its submitted generation must come from one view.
def test_renderer_and_firmware_use_the_same_command_entity():
    view = (PROJECT / "view.html.jinja").read_text()
    references = re.findall(r"""\b(?:states|state_attr|is_state)\(\s*['"]([^'"]+)['"]""", view)
    assert DISPLAY_VIEW in references
    assert AUTOMATION not in references
    assert IMAGE not in references
    assert "input_select.general_display_2_ics_request" not in references
    package = yaml.load((PROJECT / "esphome-canvas.yaml.example").read_text(), Loader=yaml.BaseLoader)
    assert package["substitutions"]["canvas_ics_view_entity"] == DISPLAY_VIEW
    assert package["substitutions"]["canvas_ics_view_reply_entity"] == DISPLAY_REPLY
    command_sources = [sensor for sensor in package["text_sensor"] if sensor["platform"] == "homeassistant"]
    assert [sensor["entity_id"] for sensor in command_sources] == [
        "${canvas_ics_view_entity}",
        "${canvas_ics_view_reply_entity}",
    ]
    refresh = next(script for script in package["script"] if script["id"] == "refresh_canvas_ics_view")
    event = refresh["then"][0]["if"]["then"][0]["homeassistant.event"]
    assert event["event"] == DISPLAY_REQUEST
    assert event["data"]["display"] == "${canvas_display_id}"
    assert not any(sensor["platform"] == "homeassistant" for sensor in package.get("sensor", []))


# Intent: keep the Home Assistant time sync at the same interval as the image download.
# Why: the firmware compares a rendered frame's timestamp against that synchronized clock.
def test_display_resynchronizes_its_clock_at_the_download_interval():
    package = yaml.load((PROJECT / "esphome-canvas.yaml.example").read_text(), Loader=yaml.BaseLoader)
    clock = next(source for source in package["time"] if source["id"] == "canvas_ha_time")
    image = next(source for source in package["image"] if source["id"] == "canvas_image")
    assert clock["platform"] == "homeassistant"
    assert clock["update_interval"] == image["update_interval"] == "10s"


# Load the published automation and readiness helper into real Home Assistant.
async def install_display(hass):
    await asyncio.gather(condition.async_setup(hass), trigger.async_setup(hass))
    config = yaml.safe_load((PROJECT / "controls.yaml").read_text())
    helper = yaml.safe_load((PROJECT / "request-helper.yaml").read_text())
    assert await async_setup_component(hass, "automation", {"automation": [config]})
    assert await async_setup_component(hass, "template", helper)
    await hass.async_start()
    await hass.async_block_till_done()
    assert hass.states.get(AUTOMATION).state == "on"
    hass.states.async_set("sensor.ics_display_screen_source", "Home Assistant")


# Intent: echo the requesting device's valid token together with the current display offer.
# Why: old replies and events from another display cannot establish freshness after reconnecting.
async def test_display_refresh_replies_with_the_current_view_and_request_identifier(actual_hass):
    hass = actual_hass
    await install_display(hass)
    before = hass.states.get(DISPLAY_REPLY).state
    request = "c" * 32
    event = {"display": "general_display_2", "device_id": DISPLAY_DEVICE, "request": request}
    for override in ({"device_id": "other"}, {"display": "other"}, {"request": "A" * 32}, {"request": 3}):
        hass.bus.async_fire(DISPLAY_REQUEST, event | override)
    await hass.async_block_till_done()
    assert hass.states.get(DISPLAY_REPLY).state == before

    stop = "v2:idle:stop:" + "b" * 32
    hass.states.async_set(DISPLAY_VIEW, stop)
    hass.bus.async_fire(DISPLAY_REQUEST, event)
    await hass.async_block_till_done()
    assert hass.states.get(DISPLAY_REPLY).state == f"{request}:{stop}"

    followup = "d" * 32
    hass.states.async_set(DISPLAY_VIEW, "unknown")
    hass.bus.async_fire(DISPLAY_REQUEST, event | {"request": followup})
    await hass.async_block_till_done()
    assert hass.states.get(DISPLAY_REPLY).state == f"{followup}:v2:unavailable:none:-"


# Provide a renderer frame with optional routing or freshness variations.
def publish_display_preview(hass, rendered_at, **overrides):
    attributes = {
        "assigned_views": [VIEW],
        "current_view_id": VIEW,
        "rendered_view_id": VIEW,
        "notification_active": False,
    }
    hass.states.async_set(IMAGE, rendered_at.isoformat(), attributes | overrides)


# Send a touch event with the timestamp and generation of the pressed frame.
def send_display_tap(hass, action, generation, *, age=1, frame=None, **overrides):
    stamp = int(dt_util.now().timestamp()) if frame is None else frame
    data = {
        "display": "general_display_2",
        "device_id": DISPLAY_DEVICE,
        "x": "120" if action == "start" else "352",
        "y": "396",
        "canvas_age_seconds": str(age),
        "canvas_rendered_at_epoch": str(stamp),
        "frame_control": f"v2:{action}:{generation}",
    }
    hass.bus.async_fire("esphome.general_display_touch", data | overrides)


# Wait for the display helper's expected feedback and validate its action generation.
async def display_state(hass, feedback, action):
    async with asyncio.timeout(5):
        while True:
            state = hass.states.get(DISPLAY_VIEW)
            if state is not None and state.state.startswith(f"v2:{feedback}:{action}:"):
                generation = state.state.rsplit(":", 1)[1]
                if action == "none":
                    assert generation == "-"
                else:
                    assert re.fullmatch(r"[a-f0-9]{32}", generation)
                return generation
            await asyncio.sleep(0.01)


# Intent: exercise the published renderer and touch automation through the real integration service.
# Why: readiness shown on screen must match what the integration can send to the management peer.
async def test_public_display_example_with_actual_ha(actual_hass, canvas_peer, monkeypatch):
    from custom_components.ics_proxy import coordinator

    hass, peer = actual_hass, canvas_peer
    base = int(datetime.now(UTC).timestamp())
    wall = base
    monkeypatch.setattr(coordinator, "frame_clock", lambda: (wall, 100 + wall - base))
    peer["state"]["control_revision"] = REVISION
    await setup_proxy(hass, peer["port"])
    await install_display(hass)
    monkeypatch.setattr(dt_util, "now", lambda *args: datetime.fromtimestamp(wall, UTC))
    template = Template((PROJECT / "view.html.jinja").read_text(), hass)
    wall += 3
    frame = dt_util.now()
    await display_state(hass, "unavailable", "none")
    html = template.async_render({"now": frame}, parse_result=False)
    assert "Charger buttons unavailable" in html and render_marker(html) == f"d51a0{wall:08x}"

    publish_display_preview(hass, frame)
    start = await display_state(hass, "idle", "start")
    html = template.async_render({"now": frame}, parse_result=False)
    assert "Ready to start" in html
    assert render_marker(html) == f"d51a0{wall:08x}d52a1{start}"

    # New preview timestamps must not change the helper and trigger another render.
    control_changes = []

    # Observe publications of the helper state during unrelated preview updates.
    @callback
    def record_control_change(event):
        control_changes.append(event)

    unsubscribe = async_track_state_change_event(hass, DISPLAY_VIEW, record_control_change)
    publish_display_preview(hass, frame - timedelta(seconds=46))
    send_display_tap(hass, "start", start)
    await hass.async_block_till_done()
    unsubscribe()
    assert control_changes == []
    assert set(peer["commands"]) == {"status"}

    for wrong_route in (
        {"notification_active": True},
        {"assigned_views": [VIEW, "other-view"]},
        {"current_view_id": "other-view"},
        {"rendered_view_id": "other-view"},
    ):
        publish_display_preview(hass, frame, **wrong_route)
        send_display_tap(hass, "start", start)
        await display_state(hass, "unavailable", "none")
        await hass.async_block_till_done()
        assert set(peer["commands"]) == {"status"}, wrong_route
        html = template.async_render({"now": frame}, parse_result=False)
        assert "Charger buttons unavailable" in html and render_marker(html) == f"d51a0{wall:08x}"
        publish_display_preview(hass, frame)
        assert await display_state(hass, "idle", "start") == start

    for bad in (
        {"device_id": "another-display"},
        {"x": "352"},
        {"canvas_age_seconds": "45"},
        {"canvas_age_seconds": "44", "canvas_rendered_at_epoch": str(wall - 35)},
        {"canvas_rendered_at_epoch": str(wall - 46)},
        {"canvas_rendered_at_epoch": str(wall + 5)},
        {"frame_control": f"start:{REVISION:014x}"},
        {"frame_control": "v2:start:" + "0" * 32},
    ):
        send_display_tap(hass, "start", start, **bad)
        await hass.async_block_till_done()
    assert set(peer["commands"]) == {"status"}

    peer.update(pending_receipt=True, receipt_delay=0.25)
    send_display_tap(hass, "start", start)
    await display_state(hass, "starting", "none")
    assert render_marker(template.async_render({"now": frame}, parse_result=False)) == f"d51a0{wall:08x}"
    send_display_tap(hass, "start", start)
    stop = await display_state(hass, "idle", "stop")
    assert stop != start
    assert peer["commands"].count("start") == 1
    [start_wire] = [request for request in peer["requests"] if request["command"] == "start"]
    assert start_wire["canvas_owner"] == CANVAS_OWNER and start_wire["revision"] == REVISION

    wall += 2
    frame = dt_util.now()
    publish_display_preview(hass, frame)
    html = template.async_render({"now": frame}, parse_result=False)
    assert "Open session" in html and render_marker(html) == f"d51a0{wall:08x}d52a2{stop}"
    send_display_tap(hass, "start", start)
    await hass.async_block_till_done()
    send_display_tap(hass, "stop", stop)
    await display_state(hass, "stopping", "none")
    new_start = await display_state(hass, "idle", "start")
    assert new_start not in (start, stop)
    [stop_wire] = [request for request in peer["requests"] if request["command"] == "stop_session"]
    assert stop_wire["target"] == TARGET and stop_wire["canvas_owner"] == CANVAS_OWNER
    assert peer["commands"].count("start") == 1

    await hass.services.async_call("automation", "turn_off", {"entity_id": AUTOMATION}, blocking=True)
    await display_state(hass, "unavailable", "none")
    html = template.async_render({"now": frame}, parse_result=False)
    assert "Charger buttons unavailable" in html and render_marker(html) == f"d51a0{wall:08x}"
    send_display_tap(hass, "start", new_start)
    await hass.async_block_till_done()
    assert peer["commands"].count("start") == 1


# Intent: reject Start and Stop taps rendered before a wall-clock adjustment.
# Why: a clock jump can make an old tap look current under the same displayed generation.
@pytest.mark.parametrize("starting", [True, False], ids=["start", "stop"])
@pytest.mark.parametrize("clock_change", ["rewind", "forward", "forward_then_rewind"])
async def test_display_service_rejects_pre_adjustment_frames(
    actual_hass, canvas_peer, monkeypatch, starting, clock_change
):
    from custom_components.ics_proxy import coordinator

    hass, peer = actual_hass, canvas_peer
    base = int(datetime.now(UTC).timestamp())
    wall, elapsed = base, 100
    monkeypatch.setattr(coordinator, "frame_clock", lambda: (wall, elapsed))
    if not starting:
        peer["state"].update(charging_session_open=True, can_start=False, stop_target=TARGET)
    peer["state"]["control_revision"] = REVISION
    await setup_proxy(hass, peer["port"])
    await install_display(hass)
    publish_display_preview(hass, datetime.fromtimestamp(wall, UTC))
    action = "start" if starting else "stop_session"
    generation = await display_state(hass, "idle", "start" if starting else "stop")

    # Submit the same offer with each frame timestamp through the real service.
    async def tap_frame(epoch):
        result = await call_session_command(hass, action, canvas_generation=generation, canvas_rendered_at_epoch=epoch)
        return result["confirmed"]

    wall, elapsed = base + 2, 102
    assert not await tap_frame(base - 1)
    original_frame = base + 123 if clock_change == "forward_then_rewind" else base + 3
    arrival = {"rewind": (base + 33, 154), "forward": (base + 23, 114), "forward_then_rewind": (base + 128, 154)}
    wall, elapsed = arrival[clock_change]
    assert not await tap_frame(original_frame)
    fence = {"rewind": base + 54, "forward": base + 23, "forward_then_rewind": base + 128}[clock_change]
    elapsed += fence - wall
    wall = fence
    assert not await tap_frame(fence)
    assert action not in peer["commands"]

    wall += 12
    elapsed += 12
    assert await tap_frame(fence + 1)
    [command] = [request for request in peer["requests"] if request["command"] == action]
    assert command["revision"] == REVISION and command["canvas_owner"] == CANVAS_OWNER
    assert command.get("target") == (None if starting else TARGET)


# Intent: require a new displayed Start generation after a reload or entry replacement.
# Why: the replacement may report the same owner and revision while old touch frames are still in transit.
@pytest.mark.parametrize("operation", ["reload", "remove-add"])
async def test_display_rejects_start_frame_from_before_entry_replacement(
    actual_hass, canvas_peer, replacement_peer, monkeypatch, operation
):
    from custom_components.ics_proxy import coordinator

    hass, peer = actual_hass, canvas_peer
    base = int(datetime.now(UTC).timestamp())
    wall = base
    monkeypatch.setattr(coordinator, "frame_clock", lambda: (wall, 100 + wall - base))
    monkeypatch.setattr(dt_util, "now", lambda *args: datetime.fromtimestamp(wall, UTC))
    peer["state"]["control_revision"] = replacement_peer["state"]["control_revision"] = REVISION
    replacement_peer["state"]["canvas_owner"] = CANVAS_OWNER
    entry = await setup_proxy(hass, peer["port"])
    await install_display(hass)
    publish_display_preview(hass, dt_util.now())
    old_generation = await display_state(hass, "idle", "start")

    wall += 3
    if operation == "reload":
        hass.config_entries.async_update_entry(entry, data={**entry.data, "port": replacement_peer["port"]})
        assert await hass.config_entries.async_reload(entry.entry_id)
    else:
        assert not (await hass.config_entries.async_remove(entry.entry_id))["require_restart"]
        await setup_proxy(hass, replacement_peer["port"])

    new_generation = await display_state(hass, "idle", "start")
    assert new_generation != old_generation
    wall += 2
    publish_display_preview(hass, dt_util.now())
    send_display_tap(hass, "start", old_generation)
    await hass.async_block_till_done()
    assert set(peer["commands"]) == set(replacement_peer["commands"]) == {"status"}
    send_display_tap(hass, "start", new_generation)
    await display_state(hass, "idle", "stop")
    [start] = [request for request in replacement_peer["requests"] if request["command"] == "start"]
    assert start["revision"] == REVISION and start["canvas_owner"] == CANVAS_OWNER


# Submit a real integration action and ask for its exact attempted request reference.
async def call_session_command(hass, action, **fields):
    return await hass.services.async_call(
        "ics_proxy",
        "session_command",
        {"action": action, "include_request_reference": True, **fields},
        blocking=True,
        return_response=True,
    )


# Intent: a management command already in flight stays tied to the status that enabled it.
# Why: a replacement proxy can advertise the same revision and Start capability at the same address.
@pytest.mark.parametrize("surface", ["session_action", "start_button"])
async def test_ordinary_start_does_not_reach_a_replacement_destination(actual_hass, canvas_peer, surface):
    hass, peer = actual_hass, canvas_peer
    peer["state"]["control_revision"] = REVISION
    entry = await setup_proxy(hass, peer["port"])

    # The active coordinator can learn the new owner before the waiting connection can write.
    async def replace_owner():
        peer["state"]["canvas_owner"] = REPLACEMENT_OWNER
        await entry.runtime_data.async_refresh()

    peer["before_handshake"] = replace_owner
    if surface == "session_action":
        result = await call_session_command(hass, "start", revision=REVISION)
        assert result["confirmed"] is False and result["request_reference"] is not None
    else:
        with pytest.raises(HomeAssistantError, match="could not confirm this request"):
            await hass.services.async_call("button", "press", {"entity_id": "button.ics_proxy_start"}, blocking=True)
    (refused,) = [request for request in peer["requests"] if request["command"] == "start"]
    assert refused["canvas_owner"] == CANVAS_OWNER and refused["revision"] == REVISION
    assert entry.runtime_data.management_owner == REPLACEMENT_OWNER
    assert peer["state"]["charging_session_open"] is False

    assert (await call_session_command(hass, "start"))["confirmed"] is True
    assert peer["state"]["charging_session_open"] is True
    assert [request["canvas_owner"] for request in peer["requests"] if request["command"] == "start"] == [
        CANVAS_OWNER,
        REPLACEMENT_OWNER,
    ]


# Intent: revoke an ordinary Start whose management handshake still belongs to a retired integration.
# Why: the original endpoint can remain reachable and ready after a local disable or retarget.
@pytest.mark.parametrize("surface", ["session_action", "start_button"])
@pytest.mark.parametrize("change", ["retarget", "disable"])
async def test_ordinary_start_cannot_write_after_entry_retirement(
    actual_hass, canvas_peer, replacement_peer, surface, change
):
    hass, peer = actual_hass, canvas_peer
    entry = await setup_proxy(hass, peer["port"])
    original = entry.runtime_data

    # Retire the entry before the original socket can write a Start.
    async def retire_entry():
        if change == "retarget":
            hass.config_entries.async_update_entry(entry, data={**entry.data, "port": replacement_peer["port"]})
            assert await hass.config_entries.async_reload(entry.entry_id)
            assert entry.runtime_data is not original
            assert entry.runtime_data.client.port == replacement_peer["port"]
        else:
            assert await hass.config_entries.async_set_disabled_by(entry.entry_id, ConfigEntryDisabler.USER)
            assert entry.state is ConfigEntryState.NOT_LOADED
        assert "start" not in peer["commands"]

    peer["before_handshake"] = retire_entry
    if surface == "session_action":
        result = await call_session_command(hass, "start", revision=0)
        assert result == {"confirmed": False, "request_reference": None}
    else:
        with pytest.raises(HomeAssistantError, match="charger control entry is no longer active"):
            await hass.services.async_call("button", "press", {"entity_id": "button.ics_proxy_start"}, blocking=True)
    assert "before_handshake" not in peer
    assert "start" not in peer["commands"] and not peer["state"]["charging_session_open"]
    assert "start" not in replacement_peer["commands"]


# Intent: a recorded disable revokes a pending ordinary Start even while the entry is still loaded.
# Why: Home Assistant can be waiting for the entry lock before the coordinator learns it is being unloaded.
@pytest.mark.parametrize("surface", ["session_action", "start_button"])
async def test_ordinary_start_cannot_write_while_disable_waits_for_unload(actual_hass, canvas_peer, surface):
    hass, peer = actual_hass, canvas_peer
    entry = await setup_proxy(hass, peer["port"])
    original = entry.runtime_data
    disable_task = None

    # Hold the management handshake until HA records the disable and waits to unload.
    async def disable_entry():
        nonlocal disable_task
        disable_task = asyncio.create_task(
            hass.config_entries.async_set_disabled_by(entry.entry_id, ConfigEntryDisabler.USER)
        )
        await asyncio.sleep(0)
        assert entry.disabled_by is ConfigEntryDisabler.USER
        assert entry.state is ConfigEntryState.LOADED and entry.runtime_data is original
        assert original.notice.active and not disable_task.done()

    try:
        async with entry.setup_lock:
            peer["before_handshake"] = disable_entry
            if surface == "session_action":
                result = await call_session_command(hass, "start", revision=0)
                assert result == {"confirmed": False, "request_reference": None}
            else:
                with pytest.raises(HomeAssistantError, match="charger control entry is no longer active"):
                    await hass.services.async_call(
                        "button", "press", {"entity_id": "button.ics_proxy_start"}, blocking=True
                    )
            assert disable_task is not None
            assert set(peer["commands"]) == {"status"}
            assert peer["state"]["charging_session_open"] is False
    finally:
        if disable_task is not None:
            assert await disable_task
    assert entry.state is ConfigEntryState.NOT_LOADED


# Intent: an already-issued command keeps its original receipt lookup across entry retirement.
# Why: a change of integration endpoint cannot undo a proxy write or transfer its receipt to another endpoint.
async def test_ordinary_start_reads_its_receipt_after_entry_retirement(actual_hass, canvas_peer, replacement_peer):
    hass, peer = actual_hass, canvas_peer
    entry = await setup_proxy(hass, peer["port"])
    original = entry.runtime_data
    peer["pending_receipt"] = True

    # Schedule the reload on the next handshake once the old peer accepts the command.
    async def retarget_entry():
        hass.config_entries.async_update_entry(entry, data={**entry.data, "port": replacement_peer["port"]})
        assert await hass.config_entries.async_reload(entry.entry_id)
        assert entry.runtime_data is not original

    # The accepted reply schedules the reload before the old client's first receipt read.
    def retarget_after_start(command, _request, result):
        if command == "start":
            peer["before_handshake"] = retarget_entry
        return result

    peer["result_filter"] = retarget_after_start
    result = await call_session_command(hass, "start", revision=0)
    assert result["confirmed"] is True and result["request_reference"] is not None
    assert entry.runtime_data.client.port == replacement_peer["port"]
    assert peer["commands"].count("start") == peer["commands"].count("receipt") == 1
    assert set(replacement_peer["commands"]) == {"status"}


# Intent: each other HA command keeps the commissioned destination in its initiating status.
# Why: Stop, Pause, Return, and timed Start can all affect a replacement with the same revision and readiness.
@pytest.mark.parametrize(
    "action,initial,effect,value",
    [
        ("stop_session", {"charging_session_open": True, "stop_target": TARGET}, "charging_session_open", False),
        ("pause", {}, "desired_control", "paused"),
        ("app_control", {"desired_control": "paused", "can_return": True}, "desired_control", "app"),
        (
            "start_test",
            {},
            "test_charge",
            {"source": "local", "state": "armed", "duration_seconds": 120, "seconds_remaining": 120},
        ),
    ],
)
@pytest.mark.parametrize("refresh", [False, True], ids=["unobserved", "observed"])
async def test_other_ordinary_commands_keep_the_initiating_destination(
    actual_hass, canvas_peer, refresh, action, initial, effect, value
):
    hass, peer = actual_hass, canvas_peer
    state = peer["state"]
    state.update(control_revision=REVISION, management_owner_binding=True, **initial)
    entry = await setup_proxy(hass, peer["port"])
    original = state[effect]
    assert original != value

    # The replacement advertises the same revision and capabilities before the pending write reaches it.
    async def replace_owner():
        state["canvas_owner"] = REPLACEMENT_OWNER
        if refresh:
            await entry.runtime_data.async_refresh()

    # Exercise each ordinary HA service instead of calling the underlying client directly.
    async def issue():
        if action == "stop_session":
            return await call_session_command(hass, action, target=TARGET)
        await hass.services.async_call("button", "press", {"entity_id": f"button.ics_proxy_{action}"}, blocking=True)
        return None

    peer["before_handshake"] = replace_owner
    if action == "stop_session":
        result = await issue()
        assert result["confirmed"] is False and result["request_reference"] is not None
    else:
        with pytest.raises(HomeAssistantError, match="could not confirm this request"):
            await issue()
    assert state[effect] == original
    assert entry.runtime_data.management_owner == REPLACEMENT_OWNER

    result = await issue()
    if action == "stop_session":
        assert result["confirmed"] is True and result["request_reference"] is not None
    assert state[effect] == value
    requests = [request for request in peer["requests"] if request["command"] == action]
    assert [request["canvas_owner"] for request in requests] == [CANVAS_OWNER, REPLACEMENT_OWNER]
    assert all(request["revision"] == REVISION for request in requests)


# Intent: a bound Pause still uses the restrictive path when the proxy revision has advanced.
# Why: a stale dashboard can still pause the same charger, including with earlier proxy versions.
@pytest.mark.parametrize("binding", [True, None], ids=["all-commands", "canvas-only"])
async def test_pause_keeps_its_stale_revision_behavior_with_owner_status(actual_hass, canvas_peer, binding):
    hass, peer = actual_hass, canvas_peer
    if binding:
        peer["state"]["management_owner_binding"] = True
    await setup_proxy(hass, peer["port"])
    peer["state"]["control_revision"] = REVISION

    await hass.services.async_call("button", "press", {"entity_id": "button.ics_proxy_pause"}, blocking=True)
    (pause,) = [request for request in peer["requests"] if request["command"] == "pause"]
    assert pause["revision"] == 0
    assert pause.get("canvas_owner") == (CANVAS_OWNER if binding else None)
    assert peer["state"]["local_hold_verified"] is True


# Intent: an unchanged HA entry rejects a fresh frame or an in-flight write for its previous proxy.
# Why: independent destinations at the same address can advertise the same revision and capabilities.
@pytest.mark.parametrize("starting", [True, False], ids=["start", "stop"])
@pytest.mark.parametrize("observed", [True, False], ids=["after-poll", "during-handshake"])
async def test_canvas_rejects_replacement_at_the_same_endpoint(
    actual_hass, canvas_peer, monkeypatch, starting, observed
):
    from custom_components.ics_proxy import coordinator

    peer = canvas_peer
    wall = 1000
    monkeypatch.setattr(coordinator, "frame_clock", lambda: (wall, wall))
    peer["state"].update(control_revision=REVISION, charging_session_open=not starting, can_start=starting)
    if not starting:
        peer["state"]["stop_target"] = TARGET
    entry = await setup_proxy(actual_hass, peer["port"])
    runtime = entry.runtime_data
    action = "start" if starting else "stop_session"
    fields = {"revision": REVISION} if starting else {"target": TARGET}
    old_frame = wall + 1
    wall = old_frame + 1

    # Swap the commissioned peer during the management upgrade when the caller still has the previous status.
    async def replace_owner():
        peer["state"]["canvas_owner"] = REPLACEMENT_OWNER

    if observed:
        await replace_owner()
        await runtime.async_refresh()
    else:
        peer["before_handshake"] = replace_owner

    result = await call_session_command(actual_hass, action, canvas_rendered_at_epoch=old_frame, **fields)
    assert result["confirmed"] is False
    assert (result["request_reference"] is None) is observed
    assert peer["state"]["charging_session_open"] is (not starting)
    assert entry.runtime_data is runtime and runtime.client.port == peer["port"]
    assert runtime.management_owner == REPLACEMENT_OWNER

    # A frame stamped in the observation's whole second can still precede it.
    result = await call_session_command(actual_hass, action, canvas_rendered_at_epoch=wall, **fields)
    assert result == {"confirmed": False, "request_reference": None}

    # A subsequent poll of the same owner must preserve the observation floor.
    wall += 2
    new_frame = wall
    wall += 1
    await runtime.async_refresh()
    result = await call_session_command(actual_hass, action, canvas_rendered_at_epoch=new_frame, **fields)
    assert result["confirmed"] is True
    requests = [request for request in peer["requests"] if request["command"] == action]
    assert [request["canvas_owner"] for request in requests] == (
        [REPLACEMENT_OWNER] if observed else [CANVAS_OWNER, REPLACEMENT_OWNER]
    )
    assert all(request["revision"] == REVISION for request in requests)


# Intent: a proxy without a valid owner marker still supports ordinary HA controls.
# Why: only canvas requests rely on a commissioned destination to interpret the displayed render time.
@pytest.mark.parametrize("owner", [None, "malformed"], ids=["legacy", "malformed"])
async def test_canvas_requires_a_valid_status_owner(actual_hass, peer, monkeypatch, owner):
    from custom_components.ics_proxy import coordinator

    wall = 1000
    monkeypatch.setattr(coordinator, "frame_clock", lambda: (wall, wall))
    if owner is not None:
        peer["state"]["canvas_owner"] = owner
    await setup_proxy(actual_hass, peer["port"])
    wall += 2

    result = await call_session_command(actual_hass, "start", revision=0, canvas_rendered_at_epoch=wall - 1)
    assert result == {"confirmed": False, "request_reference": None}
    assert set(peer["commands"]) == {"status"}

    assert (await call_session_command(actual_hass, "start"))["confirmed"] is True
    (command,) = [request for request in peer["requests"] if request["command"] == "start"]
    assert "canvas_owner" not in command


# Intent: only an ordinary Start may use the coordinator's current revision when no revision is supplied.
# Why: a canvas Start must use the control the user actually saw.
async def test_canvas_start_requires_the_displayed_revision(actual_hass, canvas_peer, monkeypatch):
    from custom_components.ics_proxy import coordinator

    peer = canvas_peer
    wall = 1000
    monkeypatch.setattr(coordinator, "frame_clock", lambda: (wall, wall))
    await setup_proxy(actual_hass, peer["port"])
    wall += 2
    result = await call_session_command(actual_hass, "start", canvas_rendered_at_epoch=wall - 1)
    assert result == {"confirmed": False, "request_reference": None}
    assert set(peer["commands"]) == {"status"}

    # An ordinary HA call may still use the current revision.
    result = await call_session_command(actual_hass, "start")
    assert result["confirmed"] is True
    assert result["request_reference"] is not None
    (command,) = [request for request in peer["requests"] if request["command"] == "start"]
    assert command["revision"] == 0
    assert command["canvas_owner"] == CANVAS_OWNER


# Intent: require fresh native session evidence for the displayed Stop target, even with the vendor offline.
# Why: target equality alone does not show that the original session is still open.
@pytest.mark.parametrize("invalid", ["closed", "unknown", "old_native", "old_status"])
async def test_canvas_stop_rechecks_the_local_session(actual_hass, canvas_peer, monkeypatch, invalid):
    from custom_components.ics_proxy import coordinator as proxy_coordinator

    peer = canvas_peer
    wall = 1000
    monkeypatch.setattr(proxy_coordinator, "frame_clock", lambda: (wall, wall))
    peer["state"].update(charging_session_open=True, can_start=False, stop_target=TARGET)
    entry = await setup_proxy(actual_hass, peer["port"])
    wall += 2
    # Leave the old target in the coordinator, as it can remain between timer and poll updates.
    coordinator = entry.runtime_data
    if invalid in ("old_native", "old_status"):
        key = "native_age_seconds" if invalid == "old_native" else "status_age_seconds"
        coordinator.data[key] = 15
        coordinator.received_at -= 0.1
    else:
        coordinator.data["charging_session_open"] = False if invalid == "closed" else None
    result = await call_session_command(actual_hass, "stop_session", target=TARGET, canvas_rendered_at_epoch=wall - 1)
    assert result == {"confirmed": False, "request_reference": None}
    assert set(peer["commands"]) == {"status"}

    peer["state"].update(healthy=False, local_healthy=True, cloud_connected=False)
    await coordinator.async_refresh()
    result = await call_session_command(actual_hass, "stop_session", target=TARGET, canvas_rendered_at_epoch=wall - 1)
    assert result["confirmed"] is True
    assert result["request_reference"] is not None
    (command,) = [request for request in peer["requests"] if request["command"] == "stop_session"]
    assert command["target"] == TARGET


# Intent: stop an expired display action before its first management write and omit the attempt reference.
# Why: a slow handshake can outlive a frame that was fresh when the action began.
@pytest.mark.parametrize("starting", [True, False], ids=["start", "stop"])
async def test_canvas_frame_can_expire_during_the_management_handshake(actual_hass, canvas_peer, monkeypatch, starting):
    from custom_components.ics_proxy import coordinator

    peer = canvas_peer
    wall = 1000
    monkeypatch.setattr(coordinator, "frame_clock", lambda: (wall, wall))
    if not starting:
        peer["state"].update(charging_session_open=True, can_start=False, stop_target=TARGET)
    await setup_proxy(actual_hass, peer["port"])
    frame = wall + 1
    wall = frame + 44
    entered_handshake = False

    # Advance the simulated frame clock just before the real protocol upgrade.
    async def expire_frame():
        nonlocal wall, entered_handshake
        entered_handshake = True
        wall += 1

    # The real management socket upgrades only after the original frame has expired.
    peer["before_handshake"] = expire_frame
    action = "start" if starting else "stop_session"
    fields = {"revision": 0} if starting else {"target": TARGET}
    result = await call_session_command(actual_hass, action, canvas_rendered_at_epoch=frame, **fields)
    assert entered_handshake
    assert result == {"confirmed": False, "request_reference": None}
    assert set(peer["commands"]) == {"status"}

    result = await call_session_command(actual_hass, action, canvas_rendered_at_epoch=wall, **fields)
    assert result["confirmed"] is True
    assert result["request_reference"] is not None
    assert peer["commands"].count(action) == 1
    assert "receipt" not in peer["commands"]


# Intent: recheck paired wall and elapsed time at the real WebSocket write boundary.
# Why: a sizable clock change can arrive during handshake while both reported ages still appear fresh.
@pytest.mark.parametrize("starting", [True, False], ids=["start", "stop"])
@pytest.mark.parametrize(
    "step,allowed",
    [(-4, False), (-0.5, True), (0.5, True), (4, False)],
    ids=["rewind", "backward-tolerance", "forward-tolerance", "forward"],
)
async def test_canvas_clock_step_during_handshake(actual_hass, canvas_peer, monkeypatch, starting, step, allowed):
    from custom_components.ics_proxy import coordinator

    peer = canvas_peer
    wall, elapsed = 1000, 100
    monkeypatch.setattr(coordinator, "frame_clock", lambda: (wall, elapsed))
    if not starting:
        peer["state"].update(charging_session_open=True, can_start=False, stop_target=TARGET)
    await setup_proxy(actual_hass, peer["port"])
    wall, elapsed = 1002, 102

    # The epoch remains in the past and within the normal age limit across every simulated change.
    async def adjust_clock():
        nonlocal wall, elapsed
        wall += 6 + step
        elapsed += 6

    peer["before_handshake"] = adjust_clock
    action = "start" if starting else "stop_session"
    fields = {"revision": 0} if starting else {"target": TARGET}
    result = await call_session_command(actual_hass, action, canvas_rendered_at_epoch=1001, **fields)
    assert elapsed == 108
    assert result["confirmed"] is allowed
    assert (result["request_reference"] is not None) is allowed
    assert peer["commands"].count(action) == int(allowed)
    if not allowed:
        # Later frames must clear both the old clock and the adjusted clock's observed range.
        advance = max(108 + 900, wall) - wall + 2
        wall += advance
        elapsed += advance
        result = await call_session_command(actual_hass, action, canvas_rendered_at_epoch=wall - 1, **fields)
        assert result["confirmed"] is True and result["request_reference"] is not None
        assert peer["commands"].count(action) == 1


# Intent: Start and Stop require the same fresh authority immediately before the first write.
# Why: a frame can remain fresh while a newer status revokes the particular control it displayed.
@pytest.mark.parametrize("starting", [True, False], ids=["start", "stop"])
@pytest.mark.parametrize("interruption", ["control", "revision", "status-failure", "old-native", "old-status", "owner"])
async def test_canvas_rechecks_observed_control_at_the_management_write(
    actual_hass, canvas_peer, monkeypatch, starting, interruption
):
    from custom_components.ics_proxy import coordinator

    peer = canvas_peer
    wall = 1000
    monkeypatch.setattr(coordinator, "frame_clock", lambda: (wall, wall))
    state = peer["state"]
    state.update(control_revision=REVISION, charging_session_open=not starting, can_start=starting)
    if not starting:
        state["stop_target"] = TARGET
    entry = await setup_proxy(actual_hass, peer["port"])
    runtime = entry.runtime_data
    wall += 2
    previous = state.copy()
    updates = {
        "control": {"can_start": False} if starting else {"stop_target": "e" * 64},
        "revision": {"control_revision": REVISION + 1},
        "status-failure": {},
        "old-native": {"native_age_seconds": 16},
        "old-status": {"status_age_seconds": 16},
        "owner": {"canvas_owner": REPLACEMENT_OWNER},
    }
    action = "start" if starting else "stop_session"
    fields = {"revision": REVISION} if starting else {"target": TARGET}
    observed = False

    # Publish the superseding state before the socket upgrade lets the original command write.
    async def supersede_control():
        nonlocal observed
        state.update(updates[interruption])
        peer["status_failure"] = interruption == "status-failure"
        await runtime.async_refresh()
        observed = True
        assert runtime.last_update_success is (interruption != "status-failure")

    peer["before_handshake"] = supersede_control
    result = await call_session_command(actual_hass, action, canvas_rendered_at_epoch=wall - 1, **fields)
    assert observed
    assert result == {"confirmed": False, "request_reference": None}
    assert set(peer["commands"]) == {"status"}
    assert state["charging_session_open"] is (not starting)

    # Confirm that normal refreshed control remains usable after the refused write.
    state.update(previous)
    peer["status_failure"] = False
    await runtime.async_refresh()
    wall += 2
    result = await call_session_command(actual_hass, action, canvas_rendered_at_epoch=wall - 1, **fields)
    assert result["confirmed"] is True and result["request_reference"] is not None
    (command,) = [request for request in peer["requests"] if request["command"] == action]
    assert command["revision"] == REVISION and command["canvas_owner"] == CANVAS_OWNER
    assert command.get("target") == (None if starting else TARGET)


# Intent: a waiting canvas operation cannot write after HA replaces its integration instance.
# Why: the new runtime can publish the same owner, revision, and target while the old request is still in flight.
@pytest.mark.parametrize("starting", [True, False], ids=["start", "stop"])
async def test_canvas_write_rejects_inflight_entry_reload(actual_hass, canvas_peer, monkeypatch, starting):
    from custom_components.ics_proxy import coordinator

    hass, peer = actual_hass, canvas_peer
    wall = 1000
    monkeypatch.setattr(coordinator, "frame_clock", lambda: (wall, wall))
    peer["state"].update(control_revision=REVISION, charging_session_open=not starting, can_start=starting)
    if not starting:
        peer["state"]["stop_target"] = TARGET
    entry = await setup_proxy(hass, peer["port"])
    original = entry.runtime_data
    wall += 2

    # Use a real reload on the same endpoint before the original websocket handshake can finish.
    async def reload_entry():
        assert await hass.config_entries.async_reload(entry.entry_id)
        assert entry.runtime_data is not original
        assert entry.runtime_data.management_owner == CANVAS_OWNER
        assert entry.runtime_data.data["control_revision"] == REVISION

    peer["before_handshake"] = reload_entry
    action = "start" if starting else "stop_session"
    fields = {"revision": REVISION} if starting else {"target": TARGET}
    result = await call_session_command(hass, action, canvas_rendered_at_epoch=wall - 1, **fields)
    assert entry.runtime_data is not original
    assert result == {"confirmed": False, "request_reference": None}
    assert set(peer["commands"]) == {"status"}

    wall += 2
    result = await call_session_command(hass, action, canvas_rendered_at_epoch=wall - 1, **fields)
    assert result["confirmed"] is True and result["request_reference"] is not None
    assert peer["commands"].count(action) == 1
