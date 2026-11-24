"""A bounded support download reads cached evidence and public software metadata, never the charger or journal."""

from __future__ import annotations

import hashlib
import json
import re
from os.path import samestat
from pathlib import Path
from typing import TYPE_CHECKING, Any

from homeassistant.const import __version__ as ha_version
from homeassistant.core import HomeAssistant

from .diagnostic_view import PHASES, STATES, diagnostics, explanation, label, number

if TYPE_CHECKING:
    from . import ProxyEntry

CLIENT_BUILD_INFO = "ics_proxy_client_build_info"


# Read the public build fields independently. A directory replacement during the read can mix two builds.
def client_build_info() -> dict[str, str | None]:
    unknown: dict[str, str | None] = {"version": None, "build": None, "source_sha256": None}
    result = dict(unknown)
    try:
        root = Path(__file__).resolve().parent
        original = root.stat()
    except OSError:
        return unknown
    try:
        manifest = json.loads((root / "manifest.json").read_text())
        version = manifest.get("version") if isinstance(manifest, dict) else None
        if isinstance(version, str) and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-rc[0-9]+)?", version):
            result["version"] = version
    except OSError, ValueError, RecursionError:
        pass
    try:
        build = (root / "BUILD").read_text().strip()
        if re.fullmatch(r"[a-f0-9]{7,40}(?:[+-]dirty)?", build):
            result["build"] = build
    except OSError, ValueError:
        pass
    try:
        if paths := sorted(root.glob("*.py")):
            digest = hashlib.sha256()
            for path in paths:
                digest.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
            result["source_sha256"] = digest.hexdigest()
    except OSError:
        pass
    try:
        current = root.stat()
    except OSError:
        return unknown
    return result if samestat(original, current) and original.st_ctime_ns == current.st_ctime_ns else unknown


# The download never refreshes the coordinator or requests a receipt; failed setup still has a useful empty report.
async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ProxyEntry) -> dict[str, Any]:
    active = hass.data.get(CLIENT_BUILD_INFO, {})
    installed = await hass.async_add_executor_job(client_build_info)
    coordinator = getattr(entry, "runtime_data", None)
    data = (coordinator.data or {}) if coordinator is not None else {}
    status_age = coordinator.age("status_age_seconds") if coordinator is not None else None
    native_age = coordinator.age("native_age_seconds") if coordinator is not None else None
    fresh = (
        coordinator is not None
        and coordinator.last_update_success
        and coordinator.notice.active
        and coordinator.notice.generation == coordinator.notice_generation
        and data.get("running") is True
        and number(status_age, 15) is not None
    )
    native_fresh = fresh and number(native_age, 15) is not None
    public_software = data.get("software")
    public_software = public_software if isinstance(public_software, dict) else {}
    source, python = public_software.get("proxy_source_sha256"), public_software.get("proxy_python")
    return {
        "software": {
            "ha_core": ha_version,
            "client_version": active.get("version"),
            "client_build": active.get("build"),
            "client_source_sha256": active.get("source_sha256"),
            "installed_client": installed,
            "proxy_source_sha256": source
            if isinstance(source, str) and re.fullmatch(r"[a-f0-9]{64}", source)
            else None,
            "proxy_python": python if isinstance(python, str) and re.fullmatch(r"\d+\.\d+\.\d+", python) else None,
        },
        "snapshot_available": bool(data),
        "snapshot_fresh": fresh,
        "native_fresh": native_fresh,
        "status_age_seconds": number(status_age),
        "native_age_seconds": number(native_age),
        "summary": explanation(data, fresh=fresh, native_fresh=native_fresh),
        "last_reported": {
            "state": label(data.get("state"), STATES),
            "phase": label(data.get("phase"), PHASES),
            "desired_control": label(data.get("desired_control"), {"app", "paused"}),
            **{
                key: data.get(key) if type(data.get(key)) is bool else None
                for key in ("healthy", "local_healthy", "cloud_connected", "charger_connected", "local_hold_verified")
            },
        },
        "measurements": {
            key: number(data.get(key)) if native_fresh else None
            for key in ("current_a", "power_w", "station_ceiling_a", "native_limit_a", "requested_current_a")
        },
        "diagnostics": diagnostics(data.get("diagnostics"), timeline=True),
    }
