"""Home Assistant action that returns the local proxy's result for one session request."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse

from . import coordinator as proxy_coordinator
from .api import ProxyError
from .coordinator import DOMAIN, ProxyCoordinator

FRAME_MAX_AGE = 45
FRAME_MAX_EPOCH = 2**32 - 1
WALL_STEP_TOLERANCE = 0.5


# A response belongs to this exact command. The client reads its receipt and never repeats the write.
def register_session_service(hass: HomeAssistant) -> None:
    first_wall, first_elapsed = proxy_coordinator.frame_clock()
    reject_through = int(first_wall)
    wall_offset = first_wall - first_elapsed

    # An observed step makes frame epochs through either wall-clock reading ambiguous.
    def frame_is_current(epoch: int, coordinator: ProxyCoordinator, owner: str | None) -> bool:
        nonlocal reject_through, wall_offset
        wall, elapsed = proxy_coordinator.frame_clock()
        offset = wall - elapsed
        if abs(offset - wall_offset) > WALL_STEP_TOLERANCE:
            reject_through = max(reject_through, int(wall), int(elapsed + wall_offset))
            wall_offset = offset
        else:
            wall_offset = max(wall_offset, offset)
        if owner is None or owner != coordinator.canvas_owner or coordinator.canvas_activation is None:
            return False
        activated_wall, activated_elapsed = coordinator.canvas_activation
        # A clock adjustment can occur while the replacement entry is loading.
        activated_through = int(max(activated_wall, activated_elapsed + wall_offset))
        return (
            type(epoch) is int
            and epoch > max(reject_through, activated_through)
            and epoch <= wall
            and elapsed + wall_offset - epoch < FRAME_MAX_AGE
        )

    # A failed unload can leave the current coordinator available to handle an exact session request.
    def active_coordinator() -> ProxyCoordinator | None:
        return next(
            (
                runtime
                for entry in hass.config_entries.async_entries(DOMAIN)
                if entry.disabled_by is None
                and entry.state in (ConfigEntryState.LOADED, ConfigEntryState.FAILED_UNLOAD)
                and (runtime := getattr(entry, "runtime_data", None)) is not None
            ),
            None,
        )

    # A display request carries evidence from the frame where the user pressed the button.
    async def handle(call: ServiceCall) -> dict[str, bool | str | None]:
        response: dict[str, bool | str | None] = {"confirmed": False}
        include_reference = call.data.get("include_request_reference", False)
        if include_reference:
            response["request_reference"] = None
        frame_epoch = call.data.get("canvas_rendered_at_epoch")
        coordinator = active_coordinator()
        if coordinator is None:
            return response
        owner = coordinator.canvas_owner if frame_epoch is not None else None
        if frame_epoch is not None and not frame_is_current(frame_epoch, coordinator, owner):
            return response
        action = call.data["action"]
        target = call.data.get("target")
        requested_revision = call.data.get("revision")
        revision = (coordinator.data or {}).get("control_revision")
        if type(revision) is not int:
            return response

        # Reuse the same current control checks before connecting and after a canvas connection opens.
        def request_is_current() -> bool:
            data = coordinator.data or {}
            current_revision = data.get("control_revision")
            if not coordinator.last_update_success or type(current_revision) is not int or current_revision != revision:
                return False
            if action == "start":
                return (
                    target is None
                    and (
                        (requested_revision is None and frame_epoch is None)
                        or (type(requested_revision) is int and requested_revision == revision)
                    )
                    and data.get("can_start") is True
                    and coordinator.fresh_local_healthy
                )
            return (
                requested_revision is None
                and isinstance(target, str)
                and target == data.get("stop_target")
                and (frame_epoch is None or (data.get("charging_session_open") is True and coordinator.fresh_native))
            )

        if not request_is_current():
            return response

        # Retain the attempted request reference even if the receipt is lost or refused.
        def record_reference(reference: str) -> None:
            response["request_reference"] = reference

        # The frame or its current control can expire while Home Assistant opens the management connection.
        def require_current_canvas() -> None:
            if (
                not frame_is_current(frame_epoch, coordinator, owner)
                or active_coordinator() is not coordinator
                or not request_is_current()
            ):
                raise ProxyError("The display request is no longer current")

        fields = {"target": target} if action == "stop_session" else {}
        try:
            await coordinator.async_command(
                action,
                revision,
                report_reference=record_reference if include_reference else None,
                before_write=require_current_canvas if frame_epoch is not None else None,
                **fields,
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
