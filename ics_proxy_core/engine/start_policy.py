"""Ordered read-only checks for managed Start admission."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .managed_facts import ManagedFacts

if TYPE_CHECKING:
    from .managed_charging import ManagedCharging
    from .store import Journal
    from .trial_handover_evidence import HandoverEvidence


ACTIVE = frozenset({"armed", "starting", "awaiting_power", "charging", "running", "stopping"})


def start_refusal(
    journal: Journal,
    charging: ManagedCharging,
    native: HandoverEvidence | None,
    *,
    local: bool,
    normal: bool,
) -> str | None:
    """Return the first failed prerequisite for status or admission."""
    c, j = charging.controller, journal
    g = c.session.gateway
    facts = ManagedFacts(c)

    # First establish that the installation and current intent allow a Start.
    if c.stopping:
        return "service_stopping"
    if not (c.requests.intent().mode == "app" or normal and c.requests.intent().mode == "paused"):
        return "intent_not_startable"
    if j.fault.latched():
        return "fault_latched"
    if not j.phase.read().active:
        return "installation_not_active"
    if not g.controls.ready:
        return "station_ceiling_unconfirmed"
    desired_ceiling = g.controls.desired()
    expected_ceiling = 0 if c.requests.intent().mode == "paused" else c.current.selected()
    if desired_ceiling != expected_ceiling:
        return "station_ceiling_differs_from_selection"
    if not normal and c.current.selected() != 6:
        return "timed_test_requires_six_amperes"
    if c.current.pending():
        return "current_update_pending"

    # A normal Start may request fresh approval online before doing any charger work.
    if g.policy.allow_local_start:
        return "legacy_start_enabled"
    if local and not any(g._local_start_allowed(tag) for tag in g.policy.allowed_tags):
        if not normal or charging.authorization_status() != "renewal_required":
            return "start_authorization_unavailable"

    # Outstanding commands and lifecycle reports can still change the session.
    if j.get("control", {}).get("uncertain"):
        return "control_uncertain"
    if j.get("control", {}).get("start_pending"):
        return "start_pending"
    if j.active() is not None:
        return "transaction_open"
    if j.get("vendor_start_grant"):
        return "vendor_start_pending"
    if not facts.cloud_queue_empty(lifecycle_only=normal):
        return "cloud_delivery_pending"
    if not facts.mutations_confirmed():
        return "mutation_unconfirmed"
    if c.requests.return_in_progress():
        return "return_pending"
    if not c.requests.has_capacity():
        return "request_capacity_exhausted"
    if charging.state().get("state") in ACTIVE:
        return "managed_start_active"

    # With a native sample, also require a freshly observed plugged-in, idle charger.
    if not facts.boot_current():
        return "boot_unconfirmed"
    if not normal and not c.session.runtime.vendor_registered:
        return "timed_test_requires_cloud"
    if native is None:
        return None
    if c.native.last_verified_at == 0 or not native.is_fresh(15):
        return "native_observation_stale"
    if native.physical.connection != "Plug":
        return "vehicle_not_plugged_in"
    if native.physical.transaction_open:
        return "native_transaction_open"
    if native.physical.power_w != 0:
        return "native_power_nonzero"
    return None
