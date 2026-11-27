"""Entry-owned session command offers and feedback for rendered controls."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from homeassistant.core import callback
from homeassistant.helpers.event import async_call_later

if TYPE_CHECKING:
    from .coordinator import ProxyCoordinator

TIMEOUT_FEEDBACK_SECONDS = 20
type SessionAction = Literal["start", "stop"]


@dataclass(frozen=True, slots=True)
class SessionCommandBinding:
    """The service fields verified by the current status."""

    action: SessionAction
    owner: str
    revision: int
    target: str | None = None

    # Use the action's in-progress phase for display feedback.
    @property
    def pending_feedback(self) -> str:
        return "starting" if self.action == "start" else "stopping"


@dataclass(frozen=True, slots=True)
class SessionCommandOffer:
    """One generation identifies one displayed opportunity to submit."""

    binding: SessionCommandBinding
    generation: str


class SessionCommandOffers:
    # Track one displayed offer and its outstanding claim for this integration entry.
    def __init__(self, coordinator: ProxyCoordinator) -> None:
        self.coordinator = coordinator
        self.offer: SessionCommandOffer | None = None
        self.pending: SessionCommandOffer | None = None
        self._claim_valid = False
        self._timeout: tuple[str | None, str] | None = None
        self._cancel_timeout: Callable[[], None] | None = None
        self._awaiting_refresh: dict[str, Any] | None = None
        self._retired = False

    # Bind display offers to the current management owner and revision.
    def _scope(self) -> tuple[str, int] | None:
        coordinator = self.coordinator
        data = coordinator.data or {}
        owner, revision = coordinator.management_owner, data.get("control_revision")
        if (
            self._retired
            or not coordinator.entry_is_current()
            or not coordinator.last_update_success
            or owner is None
            or data.get("management_owner") != owner
            or type(revision) is not int
            or not 0 <= revision < 2**53
        ):
            return None
        return owner, revision

    # A current local snapshot is required even for a Stop during a cloud outage.
    def _binding(self, scope: tuple[str, int] | None) -> SessionCommandBinding | None:
        if scope is None or not self.coordinator.fresh_native:
            return None
        owner, revision = scope
        data = self.coordinator.data or {}
        session, target = data.get("charging_session_open"), data.get("stop_target")
        if session is True and isinstance(target, str) and re.fullmatch(r"[a-f0-9]{64}", target):
            return SessionCommandBinding("stop", owner, revision, target)
        if session is False and data.get("can_start") is True and self.coordinator.fresh_local_healthy:
            return SessionCommandBinding("start", owner, revision)
        return None

    # Keep a generation across harmless reads. Loss of authority retires it immediately.
    def observe(self) -> None:
        if self._timeout is not None and self._timeout[0] != self.coordinator.management_owner:
            self._clear_timeout()
        if self.coordinator.data is not self._awaiting_refresh:
            self._awaiting_refresh = None
        binding = self._binding(self._scope())
        if self.pending is not None and binding != self.pending.binding:
            self._claim_valid = False
        if binding is None or self.pending is not None or self._awaiting_refresh is not None:
            self.offer = None
        elif self.offer is None or self.offer.binding != binding:
            self.offer = SessionCommandOffer(binding, uuid4().hex)

    # Render the versioned display state, withholding offers while telemetry is unavailable.
    @property
    def state(self) -> str:
        scope = self._scope()
        if scope is None:
            return "v2:unavailable:none:-"
        owner, _ = scope
        timeout = self._timeout
        feedback = timeout[1] if timeout is not None and timeout[0] == owner else None
        if not self.coordinator.fresh_native:
            return f"v2:{feedback or 'unavailable'}:none:-"
        if self.pending is not None:
            return f"v2:{self.pending.binding.pending_feedback}:none:-"
        feedback = feedback or "idle"
        offer = self.offer
        return f"v2:{feedback}:{offer.binding.action}:{offer.generation}" if offer else f"v2:{feedback}:none:-"

    # Consumption and pending feedback occur before the first await of the management request.
    def claim(self, action: SessionAction, generation: str) -> SessionCommandOffer | None:
        self.observe()
        offer = self.offer
        if offer is None or offer.generation != generation or offer.binding.action != action:
            return None
        self._clear_timeout()
        self.offer, self.pending = None, offer
        self._claim_valid = True
        self.coordinator.async_update_listeners()
        return offer

    # Recheck the exact claimed offer and its current authority at the write boundary.
    def owns(self, claim: SessionCommandOffer) -> bool:
        return self.pending is claim and self._claim_valid and self._binding(self._scope()) == claim.binding

    # A new offer needs a status read after the command outcome, even if the same action is still safe.
    def finish(self, claim: SessionCommandOffer, confirmed: bool, before_refresh: dict[str, Any] | None) -> None:
        if self.pending is not claim or self._retired:
            return
        self.pending = None
        self._claim_valid = False
        self._awaiting_refresh = before_refresh
        if not confirmed and claim.binding.owner == self.coordinator.management_owner:
            self._show_timeout(claim.binding.pending_feedback)
        self.coordinator.async_update_listeners()

    # Recover timeout feedback only for a saved pending action on the current owner.
    def restore_interrupted(self, state: str, owner: object, pending_feedback: object) -> None:
        # Older saved state carries the pending phase only in the entity state.
        if pending_feedback is None and state in ("v2:starting:none:-", "v2:stopping:none:-"):
            pending_feedback = state.split(":")[1]
        if (
            isinstance(owner, str)
            and owner == self.coordinator.management_owner
            and isinstance(pending_feedback, str)
            and pending_feedback in ("starting", "stopping")
            and state in (f"v2:{pending_feedback}:none:-", "v2:unavailable:none:-")
        ):
            self._show_timeout(pending_feedback)

    # Show an unconfirmed action for a bounded period for this management owner.
    def _show_timeout(self, pending_feedback: str) -> None:
        self._clear_timeout()
        timeout = (self.coordinator.management_owner, f"{pending_feedback}_timeout")
        self._timeout = timeout

        # Retire feedback only when this callback still owns the active timeout.
        @callback
        def expire(_now: datetime) -> None:
            if self._timeout is timeout:
                self._timeout = None
                self._cancel_timeout = None
                self.coordinator.async_update_listeners()

        self._cancel_timeout = async_call_later(self.coordinator.hass, TIMEOUT_FEEDBACK_SECONDS, expire)

    # Cancel the scheduled expiry when its associated feedback is discarded.
    def _clear_timeout(self) -> None:
        if self._cancel_timeout is not None:
            self._cancel_timeout()
        self._timeout = None
        self._cancel_timeout = None

    # Invalidate current and claimed offers when the integration entry unloads.
    @callback
    def retire(self) -> None:
        self._retired = True
        self.offer = self.pending = None
        self._claim_valid = False
        self._clear_timeout()
