"""Exercise the real display automation in offline Home Assistant Core."""

import asyncio
import re
from datetime import UTC, datetime
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


# Intent: make the touch automation visible to the renderer's entity scan.
# Why: without that state, the display greys out charger buttons even when the automation is enabled.
def test_display_view_declares_touch_automation_to_renderer():
    # GeekMagic prefetches only entity IDs it can read directly from these template calls.
    view = (PROJECT / "view.html.jinja").read_text()
    references = re.findall(r"""\b(?:states|state_attr|is_state)\(\s*['"]([^'"]+)['"]""", view)
    assert AUTOMATION in references


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
        hass.states.async_set(
            IMAGE,
            datetime.now(UTC).isoformat(),
            {
                "assigned_views": [VIEW],
                "current_view_id": VIEW,
                "rendered_view_id": VIEW,
                "notification_active": False,
            },
        )

        # Send the same event shape and rendered marker as the touchscreen.
        def tap(starting, marker=None):
            hass.bus.async_fire(
                "esphome.general_display_touch",
                {
                    "display": "general_display_2",
                    "device_id": config["triggers"][0]["event_data"]["device_id"],
                    "x": "120" if starting else "352",
                    "y": "396",
                    "canvas_age_seconds": "1",
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
        html = rendered.async_render({"now": datetime.now(UTC)}, parse_result=False)
        assert "Paused locally" not in html and "Open session" in html
        # Decode every produced CSS cell after simulating the panel's RGB565 conversion.
        colors = re.findall(r"width:3px;height:3px;background:#([a-f0-9]{6})", html)
        raw = ""
        for color in colors:
            rgb = tuple(int(color[i : i + 2], 16) for i in (0, 2, 4))
            panel = tuple(((value >> bits) * 255) // (255 >> bits) for value, bits in zip(rgb, (3, 2, 3)))
            r, g, b = panel
            assert 40 <= b <= 74
            assert 8 + (r >> 6) * 64 <= r <= 56 + (r >> 6) * 64
            assert 8 + (g >> 6) * 64 <= g <= 56 + (g >> 6) * 64
            raw += format((r >> 6) * 4 + (g >> 6), "x")
        assert raw == "d5192" + TARGET
        tap(False)
        await wait_for(lambda: len(calls) == 2)
        assert calls[-1] == {"action": "stop_session", "target": TARGET}
        gate.set()
        await wait_for(lambda: settled("idle"))
        assert hass.states.get(SESSION).state == "off"

        hass.states.async_set("sensor.ics_proxy_control_mode", "App / schedule (cloud offline)")
        hass.states.async_set("binary_sensor.ics_proxy_problem", "on")
        html = Template((PROJECT / "view.html.jinja").read_text(), hass).async_render(
            {"now": datetime.now(UTC)}, parse_result=False
        )
        assert "Ready to start" in html and html.count("width:3px;height:3px;background:#") == 19
        template = Template((PROJECT / "view.html.jinja").read_text(), hass)
        # An unplugged charger in app/schedule mode can be locally healthy while the cloud is offline.
        hass.states.async_set("binary_sensor.ics_proxy_vehicle_connected", "off")
        hass.states.async_set(SESSION, "off", {"can_start": False, "control_revision": REVISION})
        unplugged = template.async_render({"now": datetime.now(UTC)}, parse_result=False)
        assert "Vendor connection unavailable" in unplugged
        assert "Charger control needs attention" not in unplugged
        assert "width:3px;height:3px;background:#" not in unplugged
        hass.states.async_set("sensor.ics_proxy_control_mode", "Needs attention")
        faulted = template.async_render({"now": datetime.now(UTC)}, parse_result=False)
        assert "Charger control needs attention" in faulted
        hass.states.async_set("binary_sensor.ics_proxy_vehicle_connected", "on")
        hass.states.async_set(SESSION, "off", {"can_start": True, "control_revision": REVISION})
        outcome = False
        gate.clear()
        tap(True)
        await wait_for(lambda: len(calls) == 3)
        # An unrelated session change cannot complete the waiting request.
        hass.states.async_set(SESSION, "on", {"stop_target": TARGET})
        assert hass.states.get(PENDING).state == "starting"
        gate.set()
        await wait_for(lambda: settled("starting_timeout"))
        template = Template((PROJECT / "view.html.jinja").read_text(), hass)
        html = template.async_render({"now": datetime.now(UTC)}, parse_result=False)
        assert "Start not confirmed" not in html and "Open session" in html
        hass.states.async_set(SESSION, "off", {"can_start": True, "control_revision": REVISION})
        assert "Start not confirmed" in template.async_render({"now": datetime.now(UTC)}, parse_result=False)

        hass.states.async_set(SESSION, "unavailable")
        tap(False)
        await asyncio.sleep(0.15)
        assert len(calls) == 3
        # The bounded cleanup is UI-only. A later session must not display the previous failed press.
        await wait_for(lambda: settled("idle"))
        hass.states.async_set(SESSION, "on", {"stop_target": TARGET})
        fresh_session = template.async_render({"now": datetime.now(UTC)}, parse_result=False)
        assert "Open session" in fresh_session and "not confirmed" not in fresh_session

        # An unavailable touch automation cannot honor either rendered charger control.
        await hass.services.async_call("automation", "turn_off", {"entity_id": AUTOMATION}, blocking=True)
        assert hass.states.get(AUTOMATION).state == "off"
        tap(False)
        await hass.async_block_till_done()
        assert len(calls) == 3
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
                html = template.async_render({"now": datetime.now(UTC)}, parse_result=False)
                assert "Charger buttons unavailable" in html
                assert "width:3px;height:3px;background:#" not in html

        # A timed-out helper already present when automation starts can also expire.
        await hass.services.async_call(
            "input_select", "select_option", {"entity_id": PENDING, "option": "stopping_timeout"}, blocking=True
        )
        await asyncio.sleep(1.1)
        assert hass.states.get(PENDING).state == "stopping_timeout"
        await hass.services.async_call("automation", "turn_on", {"entity_id": AUTOMATION}, blocking=True)
        await wait_for(lambda: settled("idle"))
        assert "Ready to start" in template.async_render({"now": datetime.now(UTC)}, parse_result=False)
        assert len(calls) == 3
    finally:
        gate.set()
        await hass.async_stop(force=True)
