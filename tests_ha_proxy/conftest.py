"""Minimal actual-core fixture, deliberately independent of the older HA test plugin."""

from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
from aiohttp.resolver import ThreadedResolver
from homeassistant import loader
from homeassistant.config_entries import ConfigEntries, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry, device_registry, entity_registry, frame
from homeassistant.setup import async_setup_component

from tests.conftest import unused_tcp_port as unused_tcp_port
from tests.conftest import unused_tcp_port_factory as unused_tcp_port_factory
from tests.management_peer import TOKEN


class LoopbackResolver(ThreadedResolver):
    """Mirror HA's resolver lifecycle without network discovery in a network-none container."""

    # Keep HA's actual aiohttp resolver cleanup contract while disabling multicast construction in isolation.
    async def real_close(self):
        await super().close()

    # HA retains the shared resolver until its final lifecycle close.
    async def close(self):
        pass


# Use the real import flow without loading newer engine-specific test helpers into a previous build's runner.
async def setup_proxy(hass, port):
    assert await async_setup_component(hass, "persistent_notification", {})
    result = await hass.config_entries.flow.async_init(
        "ics_proxy", context={"source": "import"}, data={"port": port, "token": TOKEN}
    )
    await hass.async_block_till_done()
    assert result["type"] == "create_entry", result
    entry = result["result"]
    assert entry.state is ConfigEntryState.LOADED
    return entry


# Open actual HA registries and config entries, retaining them when another process uses the same directory.
@asynccontextmanager
async def actual_core(config_dir, *, restore=False):
    config_dir.mkdir(exist_ok=True)
    custom = config_dir / "custom_components"
    if not restore:
        (config_dir / "configuration.yaml").write_text("{}\n")
        custom.mkdir()
        (custom / "__init__.py").write_text("")
        (custom / "ics_proxy").symlink_to(
            Path(__file__).resolve().parents[1] / "custom_components/ics_proxy", target_is_directory=True
        )
    hass = HomeAssistant(str(config_dir))
    frame.async_setup(hass)
    loader.async_setup(hass)
    hass.config_entries = ConfigEntries(hass, {})
    await hass.config_entries.async_initialize()
    device_registry.async_setup(hass)
    await area_registry.async_load(hass, load_empty=not restore)
    await device_registry.async_load(hass, load_empty=not restore)
    await entity_registry.async_load(hass, load_empty=not restore)
    try:
        # Loopback-only tests require no multicast interfaces or zeroconf discovery.
        # Keep real HA/aiohttp lifecycle and networking; replace only DNS construction.
        with patch("homeassistant.helpers.aiohttp_client._async_make_resolver", return_value=LoopbackResolver()):
            yield hass
    finally:
        await hass.async_stop(force=True)


# Supply actual HA core without importing the incompatible older HA test plugin.
@pytest.fixture
async def actual_hass(tmp_path):
    async with actual_core(tmp_path) as hass:
        yield hass
