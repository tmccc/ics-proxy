"""Home Assistant action that returns the local proxy's result for one session request."""

from __future__ import annotations

import time

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse

from .api import ProxyError
from .coordinator import DOMAIN

FRAME_MAX_AGE = 45
FRAME_MAX_EPOCH = 2**32 - 1
WALL_REWIND_TOLERANCE = 0.5


# Pair the wall clock with elapsed time. A small sampling variation must not appear to be a clock step.
def _frame_clock() -> tuple[float, float]:
    before = time.monotonic()
    wall = time.time()
    return wall, (before + time.monotonic()) / 2


# A response belongs to this exact command. The client reads its receipt and never repeats the write.
def register_session_service(hass: HomeAssistant) -> None:
    first_wall, first_elapsed = _frame_clock()
    reject_through = int(first_wall)
    wall_offset = first_wall - first_elapsed

    # A frame epoch in the old wall-clock range can be reused by the renderer after a rewind.
    def frame_is_current(epoch: int) -> bool:
        nonlocal reject_through, wall_offset
        wall, elapsed = _frame_clock()
        offset = wall - elapsed
        if offset < wall_offset - WALL_REWIND_TOLERANCE:
            reject_through = max(reject_through, int(elapsed + wall_offset))
            wall_offset = offset
        else:
            wall_offset = max(wall_offset, offset)
        return (
            type(epoch) is int
            and epoch > reject_through
            and epoch <= wall
            and elapsed + wall_offset - epoch < FRAME_MAX_AGE
        )

    # A failed unload can leave the current coordinator available to handle an exact session request.
    async def handle(call: ServiceCall) -> dict[str, bool | str | None]:
        response: dict[str, bool | str | None] = {"confirmed": False}
        include_reference = call.data.get("include_request_reference", False)
        if include_reference:
            response["request_reference"] = None
        frame_epoch = call.data.get("canvas_rendered_at_epoch")
        if frame_epoch is not None and not frame_is_current(frame_epoch):
            return response

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
            await coordinator.client.command(
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
                vol.Optional("canvas_rendered_at_epoch"): vol.All(int, vol.Range(min=1, max=FRAME_MAX_EPOCH)),
                vol.Optional("include_request_reference"): bool,
            }
        ),
        supports_response=SupportsResponse.ONLY,
    )
