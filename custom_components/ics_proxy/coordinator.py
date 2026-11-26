"""Polling and user notifications for the existing proxy, without owning any charger connection."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from datetime import timedelta
from sys import float_info
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import ProxyClient, ProxyError
from .canvas_command import CanvasCommands
from .diagnostic_view import explanation
from .notification import notice_for

DOMAIN = "ics_proxy"
LOGGER = logging.getLogger(__name__)
AGE_FIELDS = ("status_age_seconds", "native_age_seconds")


# Pair wall time with elapsed time so sampling variation does not look like a clock step.
def frame_clock() -> tuple[float, float]:
    before = time.monotonic()
    wall = time.time()
    return wall, (before + time.monotonic()) / 2


# Bound before any float arithmetic: JSON integers can exceed the representable range without exceeding message size.
def usable_age(value: Any) -> bool:
    return type(value) in (int, float) and 0 <= value <= float_info.max


class ProxyCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """A deliberate pause is normal; prolonged faults produce one persistent HA notification until recovery."""

    # Setup and refresh only read status; neither HA restart nor unload sends any control command.
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        # Reports can already be several seconds old; poll before their strict 15-second native expiry.
        super().__init__(hass, LOGGER, config_entry=entry, name=DOMAIN, update_interval=timedelta(seconds=5))
        self.client = ProxyClient(async_get_clientsession(hass), entry.data["port"], entry.data["token"])
        self.notice = notice_for(hass, entry)
        self.notice_generation = self.notice.claim(lambda: time.monotonic())
        self.notification_id = self.notice.notification_id
        self.received_at = time.monotonic()
        self.canvas_owner: str | None = None
        self.canvas_activation: tuple[float, float] | None = None
        self.canvas_commands = CanvasCommands(self)
        self.cancel_timer = None
        entry.async_on_unload(self.cancel_expiry)
        entry.async_on_unload(self.canvas_commands.retire)

    # Require a new frame after entry activation or the first observation of a different owner.
    def mark_canvas_activation(self) -> None:
        self.canvas_activation = frame_clock() if self.canvas_owner is not None else None

    # Availability and actual writes use the same entry and notification generation.
    def entry_is_current(self) -> bool:
        entry = self.config_entry
        return (
            entry is not None
            and entry.disabled_by is None
            and self.hass.config_entries.async_get_entry(entry.entry_id) is entry
            and self.notice.active
            and self.notice.generation == self.notice_generation
        )

    @callback
    def async_update_listeners(self) -> None:
        self.canvas_commands.observe()
        super().async_update_listeners()

    # Keep a write bound to its original status and integration generation across the WebSocket handshake.
    async def async_command(
        self, action: str, revision: int, *, before_write: Callable[[], None] | None = None, **fields: Any
    ) -> None:
        status = self.data or {}
        owner = status.get("canvas_owner")
        if owner is not None and (
            action in ("start", "stop_session") or status.get("management_owner_binding") is True
        ):
            fields["canvas_owner"] = owner

        # HA records a disable before queued unload work can invalidate the coordinator generation.
        def require_current_entry() -> None:
            if not self.entry_is_current():
                raise ProxyError("The charger control entry is no longer active")
            if before_write is not None:
                before_write()

        await self.client.command(action, revision, before_write=require_current_entry, **fields)

    # Keep the existing coordinator inspection surface while its debounce belongs to the entry.
    @property
    def problem_since(self) -> float | None:
        return self.notice.problem_since

    # A controlled diagnostic clock adjustment changes the shared outage, never an independent new deadline.
    @problem_since.setter
    def problem_since(self, value: float | None) -> None:
        self.notice.problem_since = value

    # Entity/tests can inspect the same notification state through any current coordinator.
    @property
    def notified(self) -> bool:
        return self.notice.notified

    # Snapshot ages continue advancing while awaiting a poll or a failed network request.
    def age(self, key: str) -> float | None:
        value = (self.data or {}).get(key)
        if not usable_age(value):
            return None
        return value + max(0, time.monotonic() - self.received_at)

    # Physical session evidence can stay current when the vendor connection is unavailable.
    @property
    def fresh_native(self) -> bool:
        ages = [self.age(key) for key in AGE_FIELDS]
        return (self.data or {}).get("running") is True and all(a is not None and 0 <= a <= 15 for a in ages)

    # Health cannot outlive either the independent native reading or the service report which verified it.
    @property
    def fresh_healthy(self) -> bool:
        return self.fresh_native and (self.data or {}).get("healthy") is True

    # Explicit local health can retain safe native control during a manufacturer-cloud outage.
    @property
    def fresh_local_healthy(self) -> bool:
        data = self.data or {}
        return self.fresh_native and data.get("local_healthy", data.get("healthy")) is True

    # Retire a previous snapshot expiry when new data arrives or HA unloads the entry.
    @callback
    def cancel_expiry(self) -> None:
        if self.cancel_timer is not None:
            self.cancel_timer()
            self.cancel_timer = None

    # Schedule the next boundary of this snapshot. Service-only entities can outlive the native readings.
    @callback
    def schedule_expiry(self, remaining: list[float]) -> None:
        until_expiry = min((age for age in remaining if age >= 0), default=None)
        if until_expiry is not None:
            self.cancel_timer = async_call_later(self.hass, max(0.1, until_expiry) + 0.05, self.expire)

    # Entity invalidation runs on time without performing another request or issuing a charger command.
    @callback
    def expire(self, now: Any) -> None:
        self.cancel_timer = None
        self.schedule_expiry([15 - age for key in AGE_FIELDS if (age := self.age(key)) is not None])
        self.update_notification(self.fresh_healthy)
        self.async_update_listeners()

    # Short reading/cloud blips settle quietly; sustained trouble is visible without repeatedly creating alerts.
    @callback
    def update_notification(self, okay: bool, data: dict[str, Any] | None = None, *, unavailable: bool = False) -> None:
        current = self.data or {} if data is None else data
        age = self.age("status_age_seconds") if data is None else data.get("status_age_seconds")
        native_age = self.age("native_age_seconds") if data is None else data.get("native_age_seconds")
        fresh = (
            not unavailable
            and (data is not None or self.last_update_success)
            and current.get("running") is True
            and usable_age(age)
            and 0 <= age <= 15
        )
        offline_pause = (
            fresh
            and current.get("desired_control") == "paused"
            and current.get("pause_reason") == "cloud_connection_failed"
        )
        native_fresh = usable_age(native_age) and 0 <= native_age <= 15
        if offline_pause and current.get("state") != "attention":
            detail = "The proxy requested Pause after it could not connect to the charger cloud. "
            if native_fresh and current.get("local_hold_verified") is True:
                detail += "App and scheduled charging are still paused."
            else:
                detail += "Waiting for the charger to confirm the pause."
        else:
            detail = explanation(current, fresh=fresh, native_fresh=native_fresh)
        self.notice.update(
            okay and fresh and native_fresh and not offline_pause,
            self.notice_generation,
            detail,
            immediate=offline_pause,
        )

    # API errors are classified before HA logs them; tokens and server payloads never appear in UpdateFailed.
    async def _async_update_data(self) -> dict[str, Any]:
        started = time.monotonic()
        try:
            value = dict(await self.client.request("status"))
        except ProxyError:
            self.update_notification(False, unavailable=True)
            raise UpdateFailed("The local charger proxy is unavailable") from None
        if not self.notice.active or self.notice.generation != self.notice_generation:
            raise UpdateFailed("The charger proxy status belongs to an inactive entry")
        owner = value.get("canvas_owner")
        if not isinstance(owner, str) or not re.fullmatch(r"[a-f0-9]{64}", owner):
            owner = None
        value["canvas_owner"] = owner
        if owner != self.canvas_owner:
            self.canvas_owner = owner
            self.mark_canvas_activation()
        self.received_at = time.monotonic()
        transit = max(0, self.received_at - started)
        for key in AGE_FIELDS:
            age = value.get(key)
            if usable_age(age):
                value[key] = age + transit
        self.cancel_expiry()
        ages = [value.get(key) for key in AGE_FIELDS]
        remaining = [15 - age for age in ages if usable_age(age)]
        self.schedule_expiry(remaining)
        self.update_notification(value.get("healthy") is True, value)
        return value
