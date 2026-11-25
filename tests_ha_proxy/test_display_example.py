"""Exercise the real display automation in offline Home Assistant Core."""

import asyncio
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml
from homeassistant import bootstrap, loader
from homeassistant.config_entries import ConfigEntries
from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.helpers.template import Template
from homeassistant.setup import async_setup_component

PROJECT = Path(__file__).resolve().parents[1] / "examples" / "solar-display-2"
SESSION = "binary_sensor.ics_proxy_session_open"
PENDING = "input_select.general_display_2_ics_request"
AUTOMATION = "automation.general_display_2_ics_charger_buttons"
IMAGE = "image.ics_display_preview"
VIEW = "replace-with-editor-view-id"
TARGET = "b" * 64
REVISION = 42
START_MARKER = f"start:{REVISION:014x}"


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
    age_check = config["conditions"][0]["conditions"][0]["conditions"][1]
    age_check["value_template"] = age_check["value_template"].replace(">= 20", ">= 1")
    helper = yaml.safe_load((PROJECT / "request-helper.yaml").read_text())
    calls = []
    gate = asyncio.Event()
    outcome = True

    # Model the proxy response while holding the automation pending until the test releases it.
    async def request(call):
        direction = call.data["action"]
        assert hass.states.get(PENDING).state == ("starting" if direction == "start" else "stopping")
        calls.append(dict(call.data))
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
        def tap(starting, marker=None, *, age="1"):
            hass.bus.async_fire(
                "esphome.general_display_touch",
                {
                    "display": "general_display_2",
                    "device_id": config["triggers"][0]["event_data"]["device_id"],
                    "x": "120" if starting else "352",
                    "y": "396",
                    "canvas_age_seconds": age,
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
        tap(True, "")
        tap(True, f"start:{REVISION - 1:014x}")
        await asyncio.sleep(0.05)
        assert calls == []
        tap(True)
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
                SESSION, "off" if starting else "on",
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
