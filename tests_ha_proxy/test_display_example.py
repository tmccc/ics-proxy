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
    assert helper["platform"] == automation["platform"] == busy["platform"] == "homeassistant"
    assert helper["entity_id"] == PENDING
    assert automation["entity_id"] == AUTOMATION and "attribute" not in automation
    assert busy["entity_id"] == AUTOMATION and busy["attribute"] == "current"
    assert helper["on_value"] == automation["on_value"] == busy["on_value"] == [
        {"script.execute": "show_ics_request_feedback"}
    ]
    scripts = {script["id"]: script["then"] for script in package["script"]}
    feedback = scripts["show_ics_request_feedback"][0]["lambda"]
    assert 'const bool enabled = id(canvas_ics_touch_automation).state == "on";' in feedback
    assert 'const bool unavailable = showing && !enabled;' in feedback
    assert re.search(r'if \(unavailable\)\s*\{\s*feedback = "Charger buttons unavailable";', feedback)
    assert "if (!enabled) id(canvas_pressed_control).clear();" in feedback
    assert "if (!pending && !unavailable)" in feedback
    for state, message in (("starting_timeout", "Start not confirmed"), ("stopping_timeout", "Stop not confirmed")):
        assert re.search(rf'request == "{state}"\)\s*\{{\s*feedback = "{message}";', feedback)
    assert {"script.execute": "show_ics_request_feedback"} in scripts["check_canvas_freshness"][0]["if"]["then"]
    page = next(page for page in package["lvgl"]["pages"] if page["id"] == "canvas_dashboard")
    touch = next(
        widget["button"] for widget in page["widgets"] if widget.get("button", {}).get("id") == "canvas_touch_button"
    )
    assert 'id(canvas_ics_touch_automation).state == "on"' in touch["on_press"][0]["lambda"]
    assert 'id(canvas_ics_touch_automation).state != "on"' in touch["on_click"][0]["if"]["condition"]["lambda"]


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

        # An unavailable touch automation cannot honor either rendered charger control.
        await hass.services.async_call("automation", "turn_off", {"entity_id": AUTOMATION}, blocking=True)
        assert hass.states.get(AUTOMATION).state == "off"
        tap(False)
        await hass.async_block_till_done()
        assert len(calls) == 4
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
        assert len(calls) == 4
    finally:
        gate.set()
        await hass.async_stop(force=True)
