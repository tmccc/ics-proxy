"""Exercise the real display automation in offline Home Assistant Core."""

import asyncio
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from homeassistant import bootstrap, loader
from homeassistant.config_entries import ConfigEntries
from homeassistant.const import EVENT_CALL_SERVICE
from homeassistant.core import HomeAssistant, SupportsResponse, callback
from homeassistant.helpers import condition, trigger
from homeassistant.helpers.template import Template
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from tests_ha_proxy.conftest import setup_proxy
from tests_ha_proxy.test_client import peer as peer

replacement_peer = peer

PROJECT = Path(__file__).resolve().parents[1] / "examples" / "solar-display-2"
SESSION = "binary_sensor.ics_proxy_session_open"
PENDING = "input_select.general_display_2_ics_request"
AUTOMATION = "automation.general_display_2_ics_charger_buttons"
EXPIRY = "script.general_display_2_ics_feedback_expiry"
EXPIRY_EVENT = "general_display_2_ics_feedback_expired"
CLEANUP_TICK = "test_display_cleanup_tick"
IMAGE = "image.ics_display_preview"
VIEW = "replace-with-editor-view-id"
TARGET = "b" * 64
REVISION = 42
START_MARKER = f"start:{REVISION:014x}"


# These tests of display feedback observe the charger command separately from its image timestamp.
def command_fields(call):
    fields = dict(call.data)
    assert type(fields.pop("canvas_rendered_at_epoch")) is int
    return fields


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


# Intent: make the touch automation visible to the renderer's entity scan.
# Why: without that state, the display greys out charger buttons even when the automation is enabled.
def test_display_view_declares_touch_automation_to_renderer():
    # GeekMagic prefetches only entity IDs it can read directly from these template calls.
    view = (PROJECT / "view.html.jinja").read_text()
    references = re.findall(r"""\b(?:states|state_attr|is_state)\(\s*['"]([^'"]+)['"]""", view)
    assert AUTOMATION in references


# Intent: the display checks for Home Assistant clock corrections as often as it checks for images.
# Why: a long time-sync interval would make fresh images look expired after Home Assistant's clock moves back.
def test_display_resynchronizes_its_clock_at_the_download_interval():
    package = yaml.load((PROJECT / "esphome-canvas.yaml.example").read_text(), Loader=yaml.BaseLoader)
    clock = next(source for source in package["time"] if source["id"] == "canvas_ha_time")
    image = next(source for source in package["image"] if source["id"] == "canvas_image")
    assert clock["platform"] == "homeassistant"
    assert clock["update_interval"] == image["update_interval"] == "10s"


# Intent: the touchscreen reacts to both settled outcomes and the live state of a pending request.
# Why: a renderer running every 25 seconds can miss the entire interval of unconfirmed feedback.
def test_display_feedback_follows_live_home_assistant_subscriptions():
    package = yaml.load((PROJECT / "esphome-canvas.yaml.example").read_text(), Loader=yaml.BaseLoader)
    helper = next(sensor for sensor in package["text_sensor"] if sensor["id"] == "canvas_ics_request_state")
    automation = next(sensor for sensor in package["text_sensor"] if sensor["id"] == "canvas_ics_touch_automation")
    busy = next(sensor for sensor in package["sensor"] if sensor["id"] == "canvas_ics_pending_runs")
    substitutions = package["substitutions"]
    assert helper["platform"] == automation["platform"] == busy["platform"] == "homeassistant"
    assert helper["entity_id"] == "${canvas_ics_request_entity}"
    assert substitutions["canvas_ics_request_entity"] == PENDING
    assert automation["entity_id"] == busy["entity_id"] == "${canvas_ics_automation_entity}"
    assert substitutions["canvas_ics_automation_entity"] == AUTOMATION
    assert "attribute" not in automation and busy["attribute"] == "current"
    for source, valid, reading in (
        (helper, "solar_display::charger_request_known(x)", "REQUEST_STATE"),
        (automation, "!x.empty()", "AUTOMATION_STATE"),
        (busy, "x >= 0", "ACTIVE_RUNS"),
    ):
        record = source["on_value"][0]["lambda"]
        assert f"if (id(canvas_ha_connections) == 1 && {valid})" in record
        assert f"id(canvas_ics_feedback_missing) &= ~solar_display::{reading};" in record
        assert source["on_value"][1] == {"script.execute": "show_ics_request_feedback"}
    scripts = {script["id"]: script["then"] for script in package["script"]}
    feedback = scripts["show_ics_request_feedback"][0]["lambda"]
    assert 'const bool enabled = id(canvas_ics_touch_automation).state == "on";' in feedback
    assert "const bool ready = id(canvas_ha_connections) == 1 && id(canvas_ics_feedback_missing) == 0;" in feedback
    assert "!ready || !enabled || !solar_display::charger_request_known(request) || !(active_runs >= 0)" in feedback
    assert re.search(r'if \(unavailable\)\s*\{\s*feedback = "Charger buttons unavailable";', feedback)
    assert "!solar_display::charger_request_idle(request, active_runs)" in feedback
    assert "id(canvas_pressed_control).clear();" in feedback
    assert "if (!pending && !unavailable)" in feedback
    assert 'const bool starting = showing && !unavailable && request == "starting";' in feedback
    assert 'const bool stopping = showing && !unavailable && request == "stopping";' in feedback
    for state, message in (("starting_timeout", "Start not confirmed"), ("stopping_timeout", "Stop not confirmed")):
        assert re.search(rf'request == "{state}"\)\s*\{{\s*feedback = "{message}";', feedback)
    assert {"script.execute": "show_ics_request_feedback"} in scripts["check_canvas_freshness"][0]["if"]["then"]
    page = next(page for page in package["lvgl"]["pages"] if page["id"] == "canvas_dashboard")
    touch = next(
        widget["button"] for widget in page["widgets"] if widget.get("button", {}).get("id") == "canvas_touch_button"
    )
    press = touch["on_press"][0]["lambda"]
    assert 'id(canvas_ics_touch_automation).state == "on"' in press
    assert "id(canvas_ha_connections) == 1 && id(canvas_ics_feedback_missing) == 0" in press
    assert re.search(
        r"charger_request_idle\(id\(canvas_ics_request_state\)\.state,\s+id\(canvas_ics_pending_runs\)\.state\)",
        press,
    )
    assert "fresh(now, clock.timestamp)" in press
    click = touch["on_click"][0]["if"]["condition"]["lambda"]
    assert 'id(canvas_ics_touch_automation).state != "on"' in click
    assert re.search(
        r"if \(charger_button &&\s*\([^\n]*\n\s*id\(canvas_ha_connections\) != 1 "
        r"\|\| id\(canvas_ics_feedback_missing\) != 0",
        click,
    )
    assert "!solar_display::charger_request_idle(request, id(canvas_ics_pending_runs).state)" in click


# Intent: a held tap is retired when Home Assistant disconnects or the old frame expires before a new download.
# Why: the release of that same tap must not submit a request from a stale frame.
def test_display_retires_held_tap_before_a_reconnect_or_frame_refresh():
    package = yaml.load((PROJECT / "esphome-canvas.yaml.example").read_text(), Loader=yaml.BaseLoader)
    connect = package["api"]["on_client_connected"][0]["lambda"]
    disconnect = package["api"]["on_client_disconnected"][0]["lambda"]
    assert "solar_display::is_home_assistant_client(client_info)" in connect
    assert "solar_display::is_home_assistant_client(client_info)" in disconnect
    assert "++id(canvas_ha_connections);" in connect
    assert "id(canvas_ics_feedback_missing) = solar_display::ALL_FEEDBACK;" in connect
    assert "id(canvas_pressed_control).clear();" in connect
    assert "--id(canvas_ha_connections);" in disconnect
    assert re.search(
        r"if \(id\(canvas_ha_connections\) == 0\) \{[^}]*\bid\(canvas_pressed_control\).clear\(\);", disconnect
    )
    assert 'id(canvas_ics_touch_automation).publish_state("");' in connect
    assert 'id(canvas_ics_request_state).publish_state("");' in connect
    assert "id(canvas_ics_pending_runs).publish_state(std::numeric_limits<float>::quiet_NaN());" in connect
    assert "publish_state" not in disconnect
    download = package["image"][0]["on_download_finished"][0]["lambda"]
    assert re.search(
        r"!frame.freshness\(\).fresh\(now_ms, now.timestamp\)\) \{\s+id\(canvas_pressed_control\).clear\(\);",
        download,
    )
    assert download.index("!frame.freshness().fresh(") < download.index("if (!cached)")


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


# Intent: charger controls wait for the surviving client's values after a connection overlap.
# Why: a sample received during overlap may belong to the connection that has just closed.
def test_display_waits_for_feedback_after_an_overlapping_disconnect():
    package = yaml.load((PROJECT / "esphome-canvas.yaml.example").read_text(), Loader=yaml.BaseLoader)
    disconnect = package["api"]["on_client_disconnected"][0]["lambda"]
    surviving_connection = re.search(r"else if \(id\(canvas_ha_connections\) == 1\) \{([^}]*)\}", disconnect)[1]
    assert "id(canvas_ics_feedback_missing) = solar_display::ALL_FEEDBACK;" in surviving_connection
    assert "id(canvas_ics_feedback_reads) = solar_display::ALL_FEEDBACK;" in surviving_connection
    assert "id(refresh_canvas_ics_feedback).execute();" in surviving_connection
    script = next(script for script in package["script"] if script["id"] == "refresh_canvas_ics_feedback")
    refresh = script["then"][0]["lambda"]
    assert re.findall(r'\{"(\$\{[a-z_]+\})", "([a-z]*)", solar_display::([A-Z_]+)\}', refresh) == [
        ("${canvas_ics_request_entity}", "", "REQUEST_STATE"),
        ("${canvas_ics_automation_entity}", "", "AUTOMATION_STATE"),
        ("${canvas_ics_automation_entity}", "current", "ACTIVE_RUNS"),
    ]
    assert "read.once = true;" in refresh
    assert "if (client->send_message(read)) pending &= ~reading.bit;" in refresh
    assert "canvas_ics_feedback_missing" not in refresh
    interval = next(interval for interval in package["interval"] if interval["interval"] == "1s")
    assert {"script.execute": "refresh_canvas_ics_feedback"} in interval["then"]


# Intent: test the shipped display tap guards and exact Start/Stop feedback inside Home Assistant.
# Why: a stale frame, duplicate tap or old failure must never send another charger request.
async def test_public_display_example_with_actual_ha(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    loader.async_setup(hass)
    hass.config_entries = ConfigEntries(hass, {})
    assert await bootstrap.async_load_base_functionality(hass)
    config = yaml.safe_load((PROJECT / "controls.yaml").read_text())
    config["triggers"][1]["seconds"] = "/1"
    helper = yaml.safe_load((PROJECT / "request-helper.yaml").read_text())
    helper["script"][EXPIRY.split(".")[1]]["sequence"][0]["delay"]["seconds"] = 1
    calls = []
    gate = asyncio.Event()
    outcome = True

    # Model the proxy response while holding the automation pending until the test releases it.
    async def request(call):
        direction = call.data["action"]
        assert hass.states.get(PENDING).state == ("starting" if direction == "start" else "stopping")
        calls.append(command_fields(call))
        await gate.wait()
        if outcome:
            hass.states.async_set(
                SESSION,
                "on" if direction == "start" else "off",
                {"stop_target": TARGET} if direction == "start" else {"can_start": True, "control_revision": REVISION},
            )
        return {"confirmed": outcome}

    hass.services.async_register("ics_proxy", "session_command", request, supports_response=SupportsResponse.ONLY)
    assert await async_setup_component(hass, "input_select", helper)
    assert await async_setup_component(hass, "script", helper)
    assert await async_setup_component(hass, "automation", {"automation": [config]})
    await hass.async_start()
    await hass.async_block_till_done()
    try:
        for entity, state in {
            "sensor.ics_display_screen_source": "Home Assistant",
            "binary_sensor.ics_proxy_problem": "off",
            "sensor.ics_proxy_control_mode": "App / schedule",
            "binary_sensor.ics_proxy_vehicle_connected": "on",
            "binary_sensor.ics_proxy_charging": "off",
            "sensor.ics_proxy_power": "0",
        }.items():
            hass.states.async_set(entity, state)
        hass.states.async_set(SESSION, "off", {"can_start": True, "control_revision": REVISION})
        image_attributes = {
            "assigned_views": [VIEW],
            "current_view_id": VIEW,
            "rendered_view_id": VIEW,
            "notification_active": False,
        }

        # Send the same event shape and rendered marker as the touchscreen.
        def tap(starting, marker=None, *, age="1", rendered_at=None):
            rendered_at = int(datetime.now(UTC).timestamp()) if rendered_at is None else rendered_at
            hass.bus.async_fire(
                "esphome.general_display_touch",
                {
                    "display": "general_display_2",
                    "device_id": config["triggers"][0]["event_data"]["device_id"],
                    "x": "120" if starting else "352",
                    "y": "396",
                    "canvas_age_seconds": age,
                    "canvas_rendered_at_epoch": str(rendered_at),
                    "frame_control": (START_MARKER if starting else "stop:" + TARGET) if marker is None else marker,
                },
            )

        # Wait for the actual Home Assistant automation to reach the expected state.
        async def wait_for(check):
            async with asyncio.timeout(5):
                while not check():
                    await asyncio.sleep(0.01)

        # A feedback result is settled after Home Assistant has left the automation.
        def settled(result):
            return hass.states.get(PENDING).state == result and hass.states.get(AUTOMATION).attributes["current"] == 0

        # The current renderer timestamp and the age of the physical frame both matter.
        hass.states.async_set(IMAGE, (datetime.now(UTC) - timedelta(seconds=46)).isoformat(), image_attributes)
        tap(True)
        await asyncio.sleep(0.05)
        assert calls == []
        hass.states.async_set(IMAGE, datetime.now(UTC).isoformat(), image_attributes)
        tap(True, age="45")
        # The displayed frame was fresh at release and expired in transit while the renderer refreshed.
        tap(True, age="44", rendered_at=int((datetime.now(UTC) - timedelta(seconds=46)).timestamp()))
        tap(True, rendered_at=0)
        tap(True, rendered_at=int((datetime.now(UTC) + timedelta(seconds=5)).timestamp()))
        tap(True, "")
        tap(True, f"start:{REVISION - 1:014x}")
        await asyncio.sleep(0.05)
        assert calls == []
        tap(True, age="11", rendered_at=int((datetime.now(UTC) - timedelta(seconds=10)).timestamp()))
        await wait_for(lambda: len(calls) == 1)
        tap(True)
        tap(False)
        await asyncio.sleep(0.1)
        assert calls == [{"action": "start", "revision": REVISION}]
        assert hass.states.get(PENDING).state == "starting"
        gate.set()
        await wait_for(lambda: settled("idle"))
        assert hass.states.get(SESSION).state == "on"
        assert hass.states.get("sensor.ics_proxy_power").state == "0"
        gate.clear()

        # Session A is still on the physical frame while the proxy now reports B.
        tap(False, "stop:" + "a" * 64)
        tap(False, "")
        tap(False, age="44", rendered_at=int((datetime.now(UTC) - timedelta(seconds=46)).timestamp()))
        await asyncio.sleep(0.1)
        assert len(calls) == 1
        rendered = Template((PROJECT / "view.html.jinja").read_text(), hass)
        hass.states.async_set("sensor.ics_proxy_control_mode", "Local charging")
        render_time = datetime.now(UTC)
        html = rendered.async_render({"now": render_time}, parse_result=False)
        assert "Paused locally" not in html and "Open session" in html
        assert render_marker(html) == f"d51a2{int(render_time.timestamp()):08x}{TARGET}"
        rerendered = render_time + timedelta(seconds=10)
        html = rendered.async_render({"now": rerendered}, parse_result=False)
        assert render_marker(html) == f"d51a2{int(rerendered.timestamp()):08x}{TARGET}"
        tap(False)
        await wait_for(lambda: len(calls) == 2)
        assert calls[-1] == {"action": "stop_session", "target": TARGET}
        gate.set()
        await wait_for(lambda: settled("idle"))
        assert hass.states.get(SESSION).state == "off"

        hass.states.async_set("sensor.ics_proxy_control_mode", "App / schedule (cloud offline)")
        hass.states.async_set("binary_sensor.ics_proxy_problem", "on")
        template = Template((PROJECT / "view.html.jinja").read_text(), hass)
        html = template.async_render({"now": render_time}, parse_result=False)
        assert "Ready to start" in html
        assert render_marker(html) == f"d51a1{int(render_time.timestamp()):08x}{REVISION:014x}"
        # An unplugged charger in app/schedule mode can be locally healthy while the cloud is offline.
        hass.states.async_set("binary_sensor.ics_proxy_vehicle_connected", "off")
        hass.states.async_set(SESSION, "off", {"can_start": False, "control_revision": REVISION})
        unplugged = template.async_render({"now": render_time}, parse_result=False)
        assert "Vendor connection unavailable" in unplugged
        assert "Charger control needs attention" not in unplugged
        assert render_marker(unplugged) == f"d51a0{int(render_time.timestamp()):08x}"
        hass.states.async_set("sensor.ics_proxy_control_mode", "Needs attention")
        faulted = template.async_render({"now": datetime.now(UTC)}, parse_result=False)
        assert "Charger control needs attention" in faulted
        hass.states.async_set("binary_sensor.ics_proxy_vehicle_connected", "on")
        hass.states.async_set(SESSION, "off", {"can_start": True, "control_revision": REVISION})
        outcome = False
        gate.clear()
        tap(True)
        await wait_for(lambda: len(calls) == 3)
        pending_start = template.async_render({"now": render_time}, parse_result=False)
        assert "Waiting for charger confirmation" not in pending_start
        assert render_marker(pending_start) == f"d51a0{int(render_time.timestamp()):08x}"
        # An unrelated session change cannot complete the waiting request.
        hass.states.async_set(SESSION, "on", {"stop_target": TARGET})
        assert hass.states.get(PENDING).state == "starting"
        gate.set()
        await wait_for(lambda: settled("starting_timeout"))
        html = template.async_render({"now": render_time}, parse_result=False)
        assert "Start not confirmed" not in html and "Open session" in html
        hass.states.async_set(SESSION, "off", {"can_start": True, "control_revision": REVISION})
        start_timeout_frame = template.async_render({"now": render_time}, parse_result=False)
        assert "Start not confirmed" not in start_timeout_frame and "Ready to start" in start_timeout_frame

        hass.states.async_set(SESSION, "unavailable")
        tap(False)
        await asyncio.sleep(0.15)
        assert len(calls) == 3
        # Expiry removes the live feedback even if the same picture is still on screen.
        await wait_for(lambda: settled("idle"))
        hass.states.async_set(SESSION, "off", {"can_start": True, "control_revision": REVISION})
        assert template.async_render({"now": render_time}, parse_result=False) == start_timeout_frame

        hass.states.async_set(SESSION, "on", {"stop_target": TARGET})
        idle_stop_frame = template.async_render({"now": render_time}, parse_result=False)
        assert "Open session" in idle_stop_frame
        gate.clear()
        tap(False)
        await wait_for(lambda: len(calls) == 4)
        assert calls[-1] == {"action": "stop_session", "target": TARGET}
        pending_stop = template.async_render({"now": render_time}, parse_result=False)
        assert "Waiting for charger confirmation" not in pending_stop
        assert render_marker(pending_stop) == f"d51a0{int(render_time.timestamp()):08x}"
        gate.set()
        await wait_for(lambda: settled("stopping_timeout"))
        assert template.async_render({"now": render_time}, parse_result=False) == idle_stop_frame
        await wait_for(lambda: settled("idle"))
        # The next render can come after the entire unconfirmed interval. The helper carried it independently.
        later = render_time + timedelta(seconds=25)
        next_stop_frame = template.async_render({"now": later}, parse_result=False)
        assert "Open session" in next_stop_frame and "Stop not confirmed" not in next_stop_frame
        assert render_marker(next_stop_frame) == f"d51a2{int(later.timestamp()):08x}{TARGET}"

        # An interrupted action has no receipt to report. Its first cleanup tick after enabling restores feedback.
        for starting, pending in ((True, "starting"), (False, "stopping")):
            gate.clear()
            previous_calls = len(calls)
            hass.states.async_set(
                SESSION,
                "off" if starting else "on",
                {"can_start": True, "control_revision": REVISION} if starting else {"stop_target": TARGET},
            )
            tap(starting)
            await wait_for(lambda: len(calls) == previous_calls + 1)
            assert hass.states.get(PENDING).state == pending
            await hass.services.async_call("automation", "turn_off", {"entity_id": AUTOMATION}, blocking=True)
            assert hass.states.get(PENDING).state == pending
            gate.set()
            await hass.services.async_call("automation", "turn_on", {"entity_id": AUTOMATION}, blocking=True)
            await wait_for(lambda: settled(pending + "_timeout"))
            await wait_for(lambda: settled("idle"))
            assert len(calls) == previous_calls + 1

        # An unavailable touch automation cannot honor either rendered charger control.
        await hass.services.async_call("automation", "turn_off", {"entity_id": AUTOMATION}, blocking=True)
        assert hass.states.get(AUTOMATION).state == "off"
        tap(False)
        await hass.async_block_till_done()
        assert len(calls) == 6
        for automation_state in ("off", "unavailable", None):
            if automation_state == "unavailable":
                hass.states.async_set(AUTOMATION, automation_state, {"current": 1})
            elif automation_state is None:
                hass.states.async_remove(AUTOMATION)
            for session_state, attributes in (
                ("on", {"stop_target": TARGET}),
                ("off", {"can_start": True, "control_revision": REVISION}),
            ):
                hass.states.async_set(SESSION, session_state, attributes)
                html = template.async_render({"now": render_time}, parse_result=False)
                assert "Charger buttons unavailable" in html
                assert render_marker(html) == f"d51a0{int(render_time.timestamp()):08x}"

        # A timed-out helper already present when automation starts can also expire.
        await hass.services.async_call(
            "input_select", "select_option", {"entity_id": PENDING, "option": "stopping_timeout"}, blocking=True
        )
        await asyncio.sleep(1.1)
        assert hass.states.get(PENDING).state == "stopping_timeout"
        await hass.services.async_call("automation", "turn_on", {"entity_id": AUTOMATION}, blocking=True)
        await wait_for(lambda: settled("idle"))
        assert "Ready to start" in template.async_render({"now": datetime.now(UTC)}, parse_result=False)
        assert len(calls) == 6
    finally:
        gate.set()
        await hass.services.async_call("automation", "turn_off", {"entity_id": AUTOMATION}, blocking=True)
        await hass.async_stop(force=True)


# Run the checked-in automation and helper in an isolated Home Assistant instance.
@asynccontextmanager
async def run_display_core(config_dir, request, *, expiry_seconds=None, cleanup_event=None):
    hass = HomeAssistant(str(config_dir))
    loader.async_setup(hass)
    hass.config_entries = ConfigEntries(hass, {})
    assert await bootstrap.async_load_base_functionality(hass)
    config = yaml.safe_load((PROJECT / "controls.yaml").read_text())
    helper = yaml.safe_load((PROJECT / "request-helper.yaml").read_text())
    if expiry_seconds is not None:
        helper["script"][EXPIRY.split(".")[1]]["sequence"][0]["delay"]["seconds"] = expiry_seconds
    if cleanup_event is not None:
        config["triggers"][1] = {"trigger": "event", "event_type": cleanup_event, "id": "clear_feedback"}
    hass.services.async_register("ics_proxy", "session_command", request, supports_response=SupportsResponse.ONLY)
    assert await async_setup_component(hass, "input_select", helper)
    assert await async_setup_component(hass, "script", helper)
    assert await async_setup_component(hass, "automation", {"automation": [config]})
    await hass.async_start()
    await hass.async_block_till_done()
    try:
        yield hass
    finally:
        await hass.async_stop(force=True)


# Publish the renderer state independently of touchscreen events.
def publish_display_preview(hass, rendered_at):
    hass.states.async_set(
        IMAGE,
        rendered_at.isoformat(),
        {"assigned_views": [VIEW], "current_view_id": VIEW, "rendered_view_id": VIEW, "notification_active": False},
    )


# Send a tap carrying the displayed control marker and the current Home Assistant state.
def send_display_tap(hass, starting, *, canvas_age=1, frame_rendered_at=None):
    hass.states.async_set("sensor.ics_display_screen_source", "Home Assistant")
    rendered_at = dt_util.now()
    publish_display_preview(hass, rendered_at)
    hass.states.async_set(
        SESSION,
        "off" if starting else "on",
        {"can_start": True, "control_revision": REVISION} if starting else {"stop_target": TARGET},
    )
    hass.bus.async_fire(
        "esphome.general_display_touch",
        {
            "display": "general_display_2",
            "device_id": "replace-with-esphome-device-id",
            "x": "120" if starting else "352",
            "y": "396",
            "canvas_age_seconds": str(canvas_age),
            "canvas_rendered_at_epoch": str(int((frame_rendered_at or rendered_at).timestamp())),
            "frame_control": START_MARKER if starting else "stop:" + TARGET,
        },
    )


# Wait for feedback and for its automation run to finish.
async def wait_for_feedback(hass, feedback):
    async with asyncio.timeout(6):
        while hass.states.get(PENDING).state != feedback or hass.states.get(AUTOMATION).attributes["current"]:
            await asyncio.sleep(0.02)


# Intent: a backward Home Assistant clock step cannot revive a delayed tap whose displayed age exposes the step.
# Why: both reported ages can still look fresh when checked independently.
@pytest.mark.parametrize("starting", [True, False], ids=["start", "stop"])
async def test_display_rejects_expired_tap_after_clock_step(tmp_path, monkeypatch, starting):
    calls = []
    called = asyncio.Event()

    # Record the action only if the actual automation admits the tap.
    async def request(call):
        calls.append(command_fields(call))
        called.set()
        return {"confirmed": True}

    async with run_display_core(tmp_path, request, cleanup_event=CLEANUP_TICK) as hass:
        frame = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=50)
        # The display sent at elapsed 43 seconds and rounded its age up to 44 seconds.
        # Seven seconds later, a 15-second backward clock correction makes the frame look 35 seconds old.
        corrected = frame + timedelta(seconds=35)
        monkeypatch.setattr(dt_util, "now", lambda *args: corrected)
        send_display_tap(hass, starting, canvas_age=44, frame_rendered_at=frame)
        await hass.async_block_till_done()
        assert calls == []

        # A fresh tap still works with the same one-second rounding allowance.
        send_display_tap(hass, starting, canvas_age=12, frame_rendered_at=corrected - timedelta(seconds=11))
        await asyncio.wait_for(called.wait(), 3)
        expected = (
            {"action": "start", "revision": REVISION} if starting else {"action": "stop_session", "target": TARGET}
        )
        assert calls == [expected]


# Intent: a clock step invalidates frames through both observed wall-clock ranges.
# Why: an earlier forward step can hide a later rewind even when a delayed tap has expired in elapsed time.
@pytest.mark.parametrize("starting", [True, False], ids=["start", "stop"])
@pytest.mark.parametrize("clock_change", ["rewind", "forward", "forward_then_rewind"])
async def test_display_service_rejects_pre_adjustment_frames(actual_hass, peer, monkeypatch, starting, clock_change):
    from custom_components.ics_proxy import session_service

    hass = actual_hass
    base = int(datetime.now(UTC).timestamp())
    wall, elapsed = base, 100
    monkeypatch.setattr(session_service, "_frame_clock", lambda: (wall, elapsed))
    if not starting:
        peer["state"].update(charging_session_open=True, can_start=False, stop_target=TARGET)
    peer["state"]["control_revision"] = REVISION
    await asyncio.gather(condition.async_setup(hass), trigger.async_setup(hass))
    await setup_proxy(hass, peer["port"])
    helper = yaml.safe_load((PROJECT / "request-helper.yaml").read_text())
    config = yaml.safe_load((PROJECT / "controls.yaml").read_text())
    config["triggers"][1] = {"trigger": "event", "event_type": CLEANUP_TICK, "id": "clear_feedback"}
    assert await async_setup_component(hass, "input_select", helper)
    assert await async_setup_component(hass, "script", helper)
    assert await async_setup_component(hass, "automation", {"automation": [config]})
    await hass.async_start()
    await hass.async_block_till_done()

    monkeypatch.setattr(dt_util, "now", lambda *args: datetime.fromtimestamp(wall, UTC))
    action = "start" if starting else "stop_session"
    unconfirmed = "starting_timeout" if starting else "stopping_timeout"
    submitted: asyncio.Queue[int] = asyncio.Queue()

    # Verify the automation passed each exact timestamp to the service, including the refused frames.
    @callback
    def note_action(event):
        data = event.data
        if data["domain"] == "ics_proxy" and data["service"] == "session_command":
            submitted.put_nowait(data["service_data"]["canvas_rendered_at_epoch"])

    hass.bus.async_listen(EVENT_CALL_SERVICE, note_action)

    # Wait for this tap's action and feedback without also waiting for the background feedback expiration.
    async def tap_frame(epoch, age, feedback):
        send_display_tap(hass, starting, canvas_age=age, frame_rendered_at=datetime.fromtimestamp(epoch, UTC))
        assert await asyncio.wait_for(submitted.get(), 3) == epoch
        await wait_for_feedback(hass, feedback)

    # A cached image from before integration setup has no elapsed-time baseline.
    wall, elapsed = base + 2, 102
    await tap_frame(base - 1, 4, unconfirmed)
    assert action not in peer["commands"]

    # Publish the original frame and let its tap travel without an earlier session action observing the clock.
    wall, elapsed = (base + 123 if clock_change == "forward_then_rewind" else base + 3), 103
    original_frame = wall
    publish_display_preview(hass, datetime.fromtimestamp(original_frame, UTC))
    await asyncio.sleep(0)

    # A rewind can leave a misleadingly recent wall timestamp. A partial rewind after a forward step
    # can make a 51-second-old frame look five seconds old while the final offset is still ahead.
    arrival = {"rewind": (base + 33, 154), "forward": (base + 23, 114), "forward_then_rewind": (base + 128, 154)}
    wall, elapsed = arrival[clock_change]
    await tap_frame(original_frame, 2, unconfirmed)
    assert action not in peer["commands"]

    # A marker at either boundary may still identify a frame from before the adjustment.
    fence = {"rewind": base + 54, "forward": base + 23, "forward_then_rewind": base + 128}[clock_change]
    elapsed += fence - wall
    wall = fence
    await tap_frame(fence, 1, unconfirmed)
    assert action not in peer["commands"]

    # The display may legitimately be one download behind the newest preview after the clock catches up.
    wall += 12
    elapsed += 12
    await tap_frame(fence + 1, 12, "idle")
    (command,) = [request for request in peer["requests"] if request["command"] == action]
    assert command["revision"] == REVISION
    assert command.get("target") == (None if starting else TARGET)
    assert "canvas_rendered_at_epoch" not in command
    await hass.services.async_call("script", "turn_off", {"entity_id": EXPIRY}, blocking=True)


# Intent: a fresh frame from another management endpoint cannot start the replacement charger.
# Why: independent proxies can publish the same numeric control revision.
@pytest.mark.parametrize("operation", ["reload", "remove-add"])
async def test_display_rejects_start_frame_from_before_entry_replacement(
    actual_hass, peer, replacement_peer, monkeypatch, operation
):
    from custom_components.ics_proxy import session_service

    hass = actual_hass
    base = int(datetime.now(UTC).timestamp())
    wall = base
    monkeypatch.setattr(session_service, "_frame_clock", lambda: (wall, 100 + wall - base))
    peer["state"]["control_revision"] = replacement_peer["state"]["control_revision"] = REVISION
    assert peer["port"] != replacement_peer["port"]
    await asyncio.gather(condition.async_setup(hass), trigger.async_setup(hass))
    entry = await setup_proxy(hass, peer["port"])
    helper = yaml.safe_load((PROJECT / "request-helper.yaml").read_text())
    config = yaml.safe_load((PROJECT / "controls.yaml").read_text())
    config["triggers"][1] = {"trigger": "event", "event_type": CLEANUP_TICK, "id": "clear_feedback"}
    assert await async_setup_component(hass, "input_select", helper)
    assert await async_setup_component(hass, "script", helper)
    assert await async_setup_component(hass, "automation", {"automation": [config]})
    await hass.async_start()
    await hass.async_block_till_done()
    monkeypatch.setattr(dt_util, "now", lambda *args: datetime.fromtimestamp(wall, UTC))

    wall += 2
    old_frame = dt_util.now()
    publish_display_preview(hass, old_frame)
    wall += 3
    if operation == "reload":
        hass.config_entries.async_update_entry(entry, data={**entry.data, "port": replacement_peer["port"]})
        assert await hass.config_entries.async_reload(entry.entry_id)
    else:
        assert not (await hass.config_entries.async_remove(entry.entry_id))["require_restart"]
        entry = await setup_proxy(hass, replacement_peer["port"])
    assert entry.runtime_data.client.port == replacement_peer["port"]
    assert entry.runtime_data.data["control_revision"] == REVISION

    send_display_tap(hass, True, canvas_age=4, frame_rendered_at=old_frame)
    await wait_for_feedback(hass, "starting_timeout")
    assert set(peer["commands"]) == set(replacement_peer["commands"]) == {"status"}

    wall += 2
    send_display_tap(hass, True)
    await wait_for_feedback(hass, "idle")
    (start,) = [request for request in replacement_peer["requests"] if request["command"] == "start"]
    assert start["revision"] == REVISION
    await hass.services.async_call("script", "turn_off", {"entity_id": EXPIRY}, blocking=True)


# Submit a real integration action and ask for its exact attempted request reference.
async def call_session_command(hass, action, **fields):
    return await hass.services.async_call(
        "ics_proxy",
        "session_command",
        {"action": action, "include_request_reference": True, **fields},
        blocking=True,
        return_response=True,
    )


# Intent: only an ordinary Start may use the coordinator's current revision when no revision is supplied.
# Why: a canvas Start must use the control the user actually saw.
async def test_canvas_start_requires_the_displayed_revision(actual_hass, peer, monkeypatch):
    from custom_components.ics_proxy import session_service

    wall = 1000
    monkeypatch.setattr(session_service, "_frame_clock", lambda: (wall, wall))
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


# Intent: require fresh native session evidence for the displayed Stop target, even with the vendor offline.
# Why: target equality alone does not show that the original session is still open.
@pytest.mark.parametrize("invalid", ["closed", "unknown", "old_native", "old_status"])
async def test_canvas_stop_rechecks_the_local_session(actual_hass, peer, monkeypatch, invalid):
    from custom_components.ics_proxy import session_service

    wall = 1000
    monkeypatch.setattr(session_service, "_frame_clock", lambda: (wall, wall))
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
async def test_canvas_frame_can_expire_during_the_management_handshake(actual_hass, peer, monkeypatch, starting):
    from custom_components.ics_proxy import session_service

    wall = 1000
    monkeypatch.setattr(session_service, "_frame_clock", lambda: (wall, wall))
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


# Intent: a Home Assistant restart carries an interrupted request into the existing unconfirmed feedback.
# Why: the independent proxy can still settle that request after the original Home Assistant run ends.
@pytest.mark.parametrize("starting", [True, False], ids=["start", "stop"])
async def test_display_restores_interrupted_feedback_after_ha_restart(tmp_path, starting):
    calls = []
    request_started = asyncio.Event()
    pending = "starting" if starting else "stopping"
    original_request = (
        {"action": "start", "revision": REVISION} if starting else {"action": "stop_session", "target": TARGET}
    )

    # Hold the service response until Core shuts down this automation run.
    async def request(call):
        calls.append(command_fields(call))
        request_started.set()
        return await asyncio.Future()

    async with run_display_core(tmp_path, request) as hass:
        assert hass.states.get(PENDING).state == "idle"
        send_display_tap(hass, starting)
        await asyncio.wait_for(request_started.wait(), 3)
        assert hass.states.get(PENDING).state == pending
        assert hass.states.get(AUTOMATION).attributes["current"] == 1

    unconfirmed = pending + "_timeout"
    async with run_display_core(tmp_path, request, cleanup_event=CLEANUP_TICK) as hass:
        assert hass.states.get(PENDING).state == pending
        hass.bus.async_fire(CLEANUP_TICK)
        await wait_for_feedback(hass, unconfirmed)
        assert hass.states.get(PENDING).context.id == hass.states.get(AUTOMATION).context.id
        hass.bus.async_fire(CLEANUP_TICK)
        await asyncio.sleep(0.1)
        assert hass.states.get(PENDING).state == unconfirmed
        assert calls == [original_request]


# Intent: startup discards messages left by a previous HA process while preserving pending requests.
# Why: an abrupt exit can leave an old saved timeout for a different request.
@pytest.mark.parametrize("feedback", ["starting_timeout", "stopping_timeout"])
async def test_display_discards_restored_unconfirmed_message(tmp_path, feedback):
    # Startup cleanup must never send the saved request again.
    async def request(call):
        pytest.fail("Cleanup submitted a charger request")

    async with run_display_core(tmp_path, request) as hass:
        await hass.services.async_call(
            "input_select",
            "select_option",
            {"entity_id": PENDING, "option": feedback},
            blocking=True,
        )
        assert hass.states.get(PENDING).state == feedback

    async with run_display_core(tmp_path, request) as hass:
        assert hass.states.get(PENDING).state == "idle"


# Intent: a disabled touch automation retires old feedback when it resumes after a Core restart.
# Why: the input select restores its old value with a new state timestamp.
async def test_display_discards_restored_feedback_after_reenabling(tmp_path):
    calls = []

    # Save a real unconfirmed result before disabling the automation.
    async def request(call):
        calls.append(command_fields(call))
        return {"confirmed": False}

    async with run_display_core(tmp_path, request) as hass:
        send_display_tap(hass, starting=True)
        await wait_for_feedback(hass, "starting_timeout")
        assert hass.states.get(EXPIRY).state == "on"
        await hass.services.async_call("automation", "turn_off", {"entity_id": AUTOMATION}, blocking=True)

    async with run_display_core(tmp_path, request) as hass:
        assert hass.states.get(AUTOMATION).state == "off"
        assert hass.states.get(PENDING).state == "starting_timeout"
        assert hass.states.get(EXPIRY).state == "off"
        await hass.services.async_call("automation", "turn_on", {"entity_id": AUTOMATION}, blocking=True)
        await wait_for_feedback(hass, "idle")
        assert calls == [{"action": "start", "revision": REVISION}]


# Intent: a live timeout expires with elapsed time after a backward clock step.
# Why: the written timestamp can stay in the past while still appearing younger than 20 seconds.
async def test_display_feedback_uses_elapsed_time_after_clock_step(tmp_path, monkeypatch):
    # Publish unconfirmed feedback so the elapsed timer can run.
    async def request(call):
        return {"confirmed": False}

    async with run_display_core(tmp_path, request, expiry_seconds=1, cleanup_event=CLEANUP_TICK) as hass:
        send_display_tap(hass, starting=True)
        await wait_for_feedback(hass, "starting_timeout")
        assert hass.states.get(EXPIRY).state == "on"

        # The corrected wall clock remains just after the publication timestamp throughout the delay.
        corrected = hass.states.get(PENDING).last_changed + timedelta(milliseconds=100)
        monkeypatch.setattr(dt_util, "now", lambda *args: corrected)
        hass.bus.async_fire(CLEANUP_TICK)
        await asyncio.sleep(0.1)
        assert hass.states.get(PENDING).state == "starting_timeout"
        await wait_for_feedback(hass, "idle")


# Intent: a cleanup tick cannot interrupt publication, and an old delay cannot clear a newer request.
# Why: the touch automation stays available while the feedback delay runs in the background.
async def test_display_expiry_follows_only_its_timeout(tmp_path):
    calls = []
    release_stop = asyncio.Event()
    expiry_starting = asyncio.Event()
    release_expiry_start = asyncio.Event()
    first_expiry = asyncio.Event()

    # Keep the replacement Stop pending until the first expiry has fired.
    async def request(call):
        calls.append(command_fields(call))
        if call.data["action"] == "stop_session":
            await release_stop.wait()
        return {"confirmed": False}

    async with run_display_core(tmp_path, request, expiry_seconds=2, cleanup_event=CLEANUP_TICK) as hass:
        original = hass.services.async_services()["script"]["turn_on"]

        # Exercise cleanup while the starting timeout is visible and its expiry script has yet to run.
        async def hold_first_expiry_start(call):
            if not expiry_starting.is_set():
                expiry_starting.set()
                await release_expiry_start.wait()
            await original.job.target(call)

        # Observe the first expiry without changing its normal delivery.
        async def note_expiry(event):
            first_expiry.set()

        hass.services.async_register("script", "turn_on", hold_first_expiry_start, schema=original.schema)
        hass.bus.async_listen(EXPIRY_EVENT, note_expiry)
        try:
            send_display_tap(hass, starting=True)
            await asyncio.wait_for(expiry_starting.wait(), 3)
            first_timeout = hass.states.get(PENDING)
            assert first_timeout.state == "starting_timeout"
            assert hass.states.get(EXPIRY).state == "off"
            assert hass.states.get(AUTOMATION).attributes["current"] == 1
            hass.bus.async_fire(CLEANUP_TICK)
            await asyncio.sleep(0.1)
            assert hass.states.get(PENDING) is first_timeout
            release_expiry_start.set()
            await wait_for_feedback(hass, "starting_timeout")

            send_display_tap(hass, starting=False)
            async with asyncio.timeout(1):
                while len(calls) != 2:
                    await asyncio.sleep(0.02)
            assert not first_expiry.is_set()
            await asyncio.wait_for(first_expiry.wait(), 3)
            assert hass.states.get(PENDING).state == "stopping"

            release_stop.set()
            await wait_for_feedback(hass, "stopping_timeout")
            hass.bus.async_fire(EXPIRY_EVENT, {"feedback_context": first_timeout.context.id})
            await asyncio.sleep(0.1)
            assert hass.states.get(PENDING).state == "stopping_timeout"
            await wait_for_feedback(hass, "idle")
            assert calls == [
                {"action": "start", "revision": REVISION},
                {"action": "stop_session", "target": TARGET},
            ]
        finally:
            release_expiry_start.set()
            release_stop.set()
