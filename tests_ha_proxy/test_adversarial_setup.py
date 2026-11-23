"""Actual first/reload failures retain notices through malformed decoding and numeric evidence recovery."""

import importlib
import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.setup import async_setup_component

from tests_ha_proxy.test_client import TOKEN, setup
from tests_ha_proxy.test_client import peer as peer
from tests_ha_proxy.test_setup_outage_notice import _async_get_or_create_notifications, retry


# Intent: both setup and control stop at a redirect from the configured management listener.
# Why: each WebSocket handshake must use the exact configured path and port.
@pytest.mark.parametrize("different_port", [False, True], ids=["different-path", "different-port"])
async def test_management_never_follows_websocket_redirects(actual_hass, aiohttp_server, different_port):
    from custom_components.ics_proxy.api import ProxyClient, ProxyError, UnknownCommandOutcomeError

    initial, redirected = [], []

    # Record any handshake that reaches the redirected destination.
    async def destination(request):
        redirected.append(request.path)
        return web.Response(status=400)

    # Check that authentication reached the original listener before sending the redirect.
    async def redirect(request):
        initial.append(request.headers.get("Authorization") == "Bearer " + TOKEN)
        return web.Response(status=307, headers={"Location": target})

    source = web.Application()
    source.router.add_get("/management", redirect)
    if different_port:
        other = web.Application()
        other.router.add_get("/other", destination)
        listener = await aiohttp_server(other)
        target = str(listener.make_url("/other"))
    else:
        source.router.add_get("/other", destination)
        target = "/other"
    listener = await aiohttp_server(source)

    result = await actual_hass.config_entries.flow.async_init(
        "ics_proxy", context={"source": "import"}, data={"port": listener.port, "token": TOKEN}
    )
    assert result["type"] == "form" and result["errors"]["base"] == "cannot_connect"
    client = ProxyClient(async_get_clientsession(actual_hass), listener.port, TOKEN)
    with pytest.raises(ProxyError) as failure:
        await client.command("pause", 0)
    assert not isinstance(failure.value, UnknownCommandOutcomeError)
    assert initial == [True, True] and redirected == []


# Intent: the very first coordinator status or a genuine reload starts an outage even when no good coordinator exists.
# Why: loaded-poll recovery cannot prove notification ownership survives failed first refresh and real HA setup retry.
@pytest.mark.parametrize("origin", ["first", "reload"])
@pytest.mark.parametrize("shape", ["recursive-json", "huge-native-age", "huge-status-age"])
async def test_adversarial_setup_outage_and_recovery(actual_hass, peer, monkeypatch, origin, shape):
    hass = actual_hass
    module = importlib.import_module("custom_components.ics_proxy.coordinator")
    notices = importlib.import_module("custom_components.ics_proxy.notification")
    clock = SimpleNamespace(value=1000.0)
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock.value))
    if origin == "reload":
        entry = await setup(hass, peer)
    validation = {"allow": origin == "first"}

    # For first setup the first snapshot is genuinely valid; arm the bad numeric field only after that copy exists.
    def arm_numeric():
        if shape.startswith("huge-"):
            key = "native_age_seconds" if shape == "huge-native-age" else "status_age_seconds"
            peer["state"][key] = 10**400

    # Raw text avoids exercising the independent peer's own JSON encoder recursion limit.
    def raw(command, request, result):
        if command != "status":
            return None
        if validation["allow"]:
            assert result["native_age_seconds"] == result["status_age_seconds"] == 0
            validation["allow"] = False
            arm_numeric()
            return None
        if shape == "recursive-json":
            return '{"id":' + json.dumps(request["id"]) + ',"ok":true,"result":' + "[" * 25000 + "0" + "]" * 25000 + "}"
        return None

    peer["raw_response"] = raw
    if origin == "first":
        assert await async_setup_component(hass, "persistent_notification", {})
        result = await hass.config_entries.flow.async_init(
            "ics_proxy", context={"source": "import"}, data={"port": peer["port"], "token": TOKEN}
        )
        assert result["type"] == "create_entry", result
        entry = result["result"]
    else:
        arm_numeric()
        await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert not validation["allow"]
    if shape == "recursive-json":
        assert entry.state is ConfigEntryState.SETUP_RETRY
        entry.async_cancel_retry_setup()
    else:
        assert entry.state is ConfigEntryState.LOADED and not entry.runtime_data.fresh_healthy
    notice = hass.data[notices.NOTICES][entry.entry_id]
    assert notice.problem_since == clock.value
    assert notice.notification_id not in _async_get_or_create_notifications(hass)
    for key in ("start", "app_control", "start_test"):
        state = hass.states.get("button.ics_proxy_" + key)
        assert state is None or state.state == "unavailable"
    clock.value += 61
    if entry.state is ConfigEntryState.SETUP_RETRY:
        await retry(hass, entry)
        entry.async_cancel_retry_setup()
    else:
        await entry.runtime_data.async_refresh()
    assert notice.notification_id in _async_get_or_create_notifications(hass)
    peer["raw_response"] = None
    peer["state"].update(native_age_seconds=0, status_age_seconds=0)
    if entry.state is ConfigEntryState.SETUP_RETRY:
        await retry(hass, entry)
    else:
        await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED and entry.runtime_data.fresh_healthy
    assert notice.problem_since is None and notice.notification_id not in _async_get_or_create_notifications(hass)
    assert set(peer["commands"]) == {"status"}
