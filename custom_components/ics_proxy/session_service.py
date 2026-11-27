"""Home Assistant action that returns the local proxy's result for one session request."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse

from .api import ProxyError
from .coordinator import DOMAIN


# A response belongs to this exact command. The client reads its receipt and never repeats the write.
def register_session_service(hass: HomeAssistant) -> None:
    # A failed unload can leave the current coordinator available to handle an exact session request.
    async def handle(call: ServiceCall) -> dict[str, bool | str | None]:
        response: dict[str, bool | str | None] = {"confirmed": False}
        include_reference = call.data.get("include_request_reference", False)
        if include_reference:
            response["request_reference"] = None

        # Record the attempted request even if its receipt is lost or the proxy refuses it.
        def record_reference(reference: str) -> None:
            response["request_reference"] = reference

        coordinator = next(
            (
                runtime
                for entry in hass.config_entries.async_entries(DOMAIN)
                if entry.disabled_by is None
                and entry.state in (ConfigEntryState.LOADED, ConfigEntryState.FAILED_UNLOAD)
                and (runtime := getattr(entry, "runtime_data", None)) is not None
            ),
            None,
        )
        if coordinator is None:
            return response
        data = coordinator.data or {}
        action = call.data["action"]
        target = call.data.get("target")
        requested_revision = call.data.get("revision")
        revision = data.get("control_revision")
        if not coordinator.last_update_success or type(revision) is not int:
            return response
        if action == "start":
            if (
                target is not None
                or requested_revision is not None
                and (type(requested_revision) is not int or requested_revision != revision)
                or data.get("can_start") is not True
                or not coordinator.fresh_local_healthy
            ):
                return response
            fields: dict[str, str] = {}
        else:
            if requested_revision is not None or not isinstance(target, str) or target != data.get("stop_target"):
                return response
            fields = {"target": target}
        try:
            await coordinator.command(
                action, revision, report_reference=record_reference if include_reference else None, **fields
            )
            response["confirmed"] = True
        except ProxyError:
            pass  # An unknown or rejected outcome must never appear confirmed to the caller.
        finally:
            await coordinator.async_refresh()
        return response

    hass.services.async_register(
        DOMAIN,
        "session_command",
        handle,
        schema=vol.Schema(
            {
                vol.Required("action"): vol.In(("start", "stop_session")),
                vol.Optional("target"): vol.Match(r"[a-f0-9]{64}\Z"),
                vol.Optional("revision"): vol.All(int, vol.Range(min=0, max=2**53 - 1)),
                vol.Optional("include_request_reference"): bool,
            }
        ),
        supports_response=SupportsResponse.ONLY,
    )
