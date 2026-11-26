"""Restrict and Stop the exact owner session of a finished overnight window."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any

from .command_processor import CommandPriority
from .connection_scope import current_control_scope
from .managed_intent import LOCAL_SCHEDULE_OCCURRENCE as OCCURRENCE
from .managed_intent import OFFLINE_PAUSE, decode_intent, record_automatic_pause, write_intent
from .native_faults import _NativeMeasurementError
from .policy import DeniedError
from .rpc import UnavailableError
from .security import SecurityError
from .store import RecoveryRequiredError, fingerprint

if TYPE_CHECKING:
    from .control_decisions import PauseStopClaim
    from .managed import ManagedController
    from .managed_intent import ControlIntent
    from .store import Journal


def scheduled_cutoff_target(journal: Journal) -> int | None:
    """Give the scoped cutoff exclusive automatic Pause handling at its current revision."""
    occurrence = journal.get(OCCURRENCE)
    if not isinstance(occurrence, dict):
        return None
    local, revision = occurrence.get("local"), occurrence.get("stop_revision")
    if type(local) is not int or type(revision) is not int or not journal.is_owner_session(local):
        return None
    intent = decode_intent(journal.get("managed_control_intent"))
    marker = journal.get(OFFLINE_PAUSE)
    return local if intent.mode == "paused" and type(marker) is int and revision == marker == intent.revision else None


# The exact cutoff keeps its Pause ownership across both an ordinary cutoff and startup recovery.
def record_cutoff_pause(c: ManagedController, occurrence: dict[str, Any], intent: ControlIntent) -> dict[str, Any]:
    j = c.session.journal
    paused = write_intent(j, intent, "paused")
    occurrence = {**occurrence, "stop_revision": paused.revision}
    j.put(OCCURRENCE, occurrence)
    record_automatic_pause(j, paused, "local_charge")
    c.session.gateway.controls._claim(True, stopping=True)
    return occurrence


def _claim_cutoff(
    c: ManagedController, local: int, occurrence: dict[str, Any]
) -> tuple[dict[str, Any], PauseStopClaim] | None:
    """Persist the exact automatic Pause and restrictive claim together before any charger RPC."""
    g, j = c.session.gateway, c.session.journal
    with j.atomic(retry_busy=True):
        if (
            c.stopping
            or occurrence != j.get(OCCURRENCE)
            or not j.is_owner_session(local)
            or current_control_scope(g) is None
            or fingerprint(j.get("boot")) != c.native.spec.boot_digest
        ):
            return None
        intent, active = c.requests.intent(), j.active()
        if active != local and (active is not None or j.latest_owner_stop_id() != local):
            return None
        if scheduled_cutoff_target(j) != local:
            if intent.mode != "app" or "stop_revision" in occurrence:
                return None
            if active is None:
                # An independently closed occurrence or a newer release leaves no cutoff work.
                start = c.charging.state()
                if (
                    c.local_schedule.closure_known(occurrence)
                    or start.get("id") != occurrence["request"]
                    or start.get("revision") != intent.revision
                ):
                    return None
            occurrence = record_cutoff_pause(c, occurrence, intent)
        return occurrence, g.controls.decisions.prepare_pause_stop(g, c.requests)


async def cutoff(c: ManagedController, local: int) -> None:
    """Hold zero and request Stop for a due occurrence, leaving real closure to normal supervision."""
    from .control_decisions import StopProof

    g, j = c.session.gateway, c.session.journal
    controls, native = g.controls, c.native
    try:
        occurrence = j.get(OCCURRENCE)
        if not isinstance(occurrence, dict) or occurrence.get("local") != local:
            return
        claimed = c.commands.decide(lambda: _claim_cutoff(c, local, occurrence), priority=CommandPriority.RESTRICT)
        if claimed is None:
            return
        occurrence, scope = claimed

        # The shared ticket guard also fences a replacement Boot and a newer explicit owner Pause.
        def guard() -> None:
            active = j.active()
            if (
                occurrence != j.get(OCCURRENCE)
                or scheduled_cutoff_target(j) != local
                or (active != local and (active is not None or j.latest_owner_stop_id() != local))
            ):
                raise RecoveryRequiredError("the scheduled Stop no longer owns this occurrence")
            controls.decisions.require_pause_stop(g, c.requests, scope, native.spec.boot_digest)

        # The schedule records its own local target and never retargets a user's earlier Pause receipt.
        def bind(ticket: str) -> None:
            c.requests.record_pause_claim(scope.intent, ticket, stopping=True)
            scope.ticket = ticket

        # Use any current draw evidence when deciding whether a prior Stop already covers this hold.
        def station_flowing() -> bool:
            sample = native._current_stop_sample(local)
            return sample is not None and native.still_drawing(sample, native.last_resolved_read)

        # Take independent exact-session evidence after the restrictive zero attempt and before the actual Stop.
        async def prove_stop(target: int, _ceiling_failed: bool, _awaiting_report: bool) -> StopProof:
            try:
                await native.read(closing_local=target, stop_only=True)
            except _NativeMeasurementError as error:
                if error.authority != native.authority() or error.sequence != native.last_resolved_read:
                    raise

            # Stop can proceed on excess draw only while this is the same independently observed live transaction.
            def require_current_target() -> None:
                guard()
                sample = native._current_stop_sample(local)
                if target != local or j.active() != local or sample is None or not sample.physical.transaction_open:
                    raise RecoveryRequiredError("scheduled Stop awaits proof of its exact active transaction")
                sample.physical.require_fresh(j.identity, 5)
                native.require_known_session(sample)

            guard()
            if j.active() is None and j.outcomes.stop_report_closed(local):
                return StopProof(guard)
            require_current_target()
            return StopProof(require_current_target, station_flowing)

        if scope.local is None:
            if controls.ready and controls.desired() == 0 and not j.get("control", {}).get("uncertain"):
                return
        elif not controls.decisions.stop_work_needed(
            local, station_flowing=station_flowing, zero_verified=native.stop_zero_verified(local)
        ):
            return
        await controls.stop(
            expected_local=local,
            guard=guard,
            on_claim=bind,
            prove_stop=prove_stop,
            owner=("schedule", occurrence["request"], scope.intent, local, scope.connection),
            stop_zero=native,
        )
    except DeniedError, SecurityError, RecoveryRequiredError, UnavailableError, TimeoutError, sqlite3.Error:
        return  # The same occurrence can retry without interpreting a failed reply as closure.
