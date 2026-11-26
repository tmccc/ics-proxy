#!/usr/bin/env python3
"""Restore the commissioned vendor route and current after verifying an unplugged managed charger."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Awaitable, Callable
from contextlib import closing
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ics_proxy_core.engine.gateway import Gateway  # noqa: E402
from ics_proxy_core.engine.managed_current import managed_current_ceiling  # noqa: E402
from ics_proxy_core.engine.managed_recovery_zero import require_original_zero, require_station_zero  # noqa: E402
from ics_proxy_core.engine.native_bounded import MANAGED_RESTORE_INTENT, ROUTE_FIELDS, NativeBoundedSpec  # noqa: E402
from ics_proxy_core.engine.native_bounded_handover import NativeBoundedHandover, NativePatch  # noqa: E402
from ics_proxy_core.engine.native_history import audit_history  # noqa: E402
from ics_proxy_core.engine.native_history_recovery import history_recovery_idle  # noqa: E402
from ics_proxy_core.engine.native_http import NativeTransportUnavailableError, read_native  # noqa: E402
from ics_proxy_core.engine.policy import Policy  # noqa: E402
from ics_proxy_core.engine.security import SecurityError, StaleEvidenceError  # noqa: E402
from ics_proxy_core.engine.station import StationChoice  # noqa: E402
from ics_proxy_core.engine.store import RecoveryRequiredError  # noqa: E402
from ics_proxy_core.engine.trial_config import prepare_tls  # noqa: E402
from ics_proxy_core.engine.trial_handover_evidence import HandoverEvidence, ObserveHandover  # noqa: E402
from ics_proxy_core.engine.trial_resources import ResourceBook  # noqa: E402
from ics_proxy_core.engine.trial_session import (  # noqa: E402
    PreparedTrial,
    TrialSession,
    open_native_journal,
    preflight_native,
)
from tools.trial_proxy import emit, native_spec  # noqa: E402


# Publish the current step without including private configuration or journal data.
def emit_stage(stage: str) -> None:
    emit({"restore_stage": stage})


# Match only known failures. Native and configuration exception text can contain private values.
RESTORE_GUIDANCE = {
    (RecoveryRequiredError, "journal already has an active owner"): (
        "journal_in_use",
        "Verify the car is unplugged and the charger is idle. Stop the proxy service or wait for the other "
        "restore to finish before retrying.",
    ),
    (SecurityError, "trial requires fresh matching unplugged zero-power evidence and a closed session"): (
        "unplugged_idle_required",
        "Verify the session is closed and the charger is at zero output, then unplug the car before retrying.",
    ),
    (RecoveryRequiredError, "restoration requires the charger unplugged with closed zero output"): (
        "unplugged_idle_required",
        "Keep the car unplugged and confirm the session is closed and the charger is at zero output before retrying.",
    ),
    (RecoveryRequiredError, "restoration needs a settled managed Pause at a selected 6 A"): (
        "settled_pause_required",
        "Resolve any pending recovery and verify a settled Pause with 6 A selected before running restore again.",
    ),
    (RecoveryRequiredError, "restoration needs an explicit managed Pause"): (
        "settled_pause_required",
        "Keep the car unplugged. Start the proxy service and request Pause charger in Home Assistant. "
        "When Home Assistant reports Paused locally, stop the proxy service and rerun restore.",
    ),
    (RecoveryRequiredError, "cleanup does not belong to this managed restoration"): (
        "restore_record_mismatch",
        "Check that the journal and commissioning files are the matching set for this installation.",
    ),
}


# Map known refusals to an operator action and keep unexpected exception details private.
def restore_guidance(stage: str, error: Exception) -> tuple[str, str]:
    detail = error.args[0] if len(error.args) == 1 and isinstance(error.args[0], str) else ""
    known = RESTORE_GUIDANCE.get((type(error), detail))
    if known is not None:
        return known
    if isinstance(error, StaleEvidenceError | NativeTransportUnavailableError):
        return (
            "native_evidence_unavailable",
            "Check that the charger is powered and reachable locally. Keep the car unplugged and verify the "
            "charger is idle before retrying.",
        )
    if stage == "verify_vendor_connection" and isinstance(error, TimeoutError):
        return (
            "vendor_connection_unverified",
            "Keep the proxy stopped and the car unplugged. Check that the charger can reach iCS, then retry "
            "with the same restore files and journal.",
        )
    if stage == "validate_inputs":
        return "inputs_unverified", "Check that the configuration and commissioning files are present and valid."
    if stage == "validate_maintenance_credentials":
        return (
            "maintenance_credentials_unverified",
            "Check that the proxy service account can read the TLS certificate, private key, and any configured "
            "CA or OCPP credential files. Verify the certificate is valid for the listener, then retry.",
        )
    return (
        "restore_stage_failed",
        "Keep the proxy stopped and the car unplugged. Review the restore instructions for this stage before retrying.",
    )


# Freeze managed operation before lowering the independently enforced USER limit or removing any profile.
async def prepare_cleanup(
    book: ResourceBook,
    spec: NativeBoundedSpec,
    observe: ObserveHandover,
    patch: NativePatch,
    *,
    report_stage: Callable[[str], None] = emit_stage,
) -> None:
    journal = book.journal
    phase = journal.phase.read()
    intent = journal.get(MANAGED_RESTORE_INTENT)
    if phase.cleanup and intent != spec.digest:
        raise RecoveryRequiredError("cleanup does not belong to this managed restoration")
    if phase.value in ("backend_restore_pending", "complete"):
        return

    cleanup_stage = "verify_6_a_and_remove_proxy_profiles"
    if phase.cleanup:
        report_stage(cleanup_stage)
    ceiling = managed_current_ceiling(journal, spec)
    native = await observe()
    if not phase.cleanup:
        if (
            not phase.operating
            or journal.phase.starting
            or journal.fault.latched()
            or not journal.current_change.read().settled_or_missing
            or not history_recovery_idle(journal)
            or journal.get("control", {}).get("resume_limit") != 6
        ):
            raise RecoveryRequiredError("restoration needs a settled managed Pause at a selected 6 A")
        if journal.get("managed_control_intent", {}).get("mode") == "app":
            raise RecoveryRequiredError("restoration needs an explicit managed Pause")
        require_original_zero(journal, book, spec, native)
        with journal.atomic():
            journal.put(MANAGED_RESTORE_INTENT, spec.digest)
            journal.phase.begin_cleanup()
        report_stage(cleanup_stage)

    if ceiling == 32:
        if native.saved_user_a == 32:
            spec.require_managed(native, journal.identity, closed=True, allow_unplugged=True)
            audit_history(journal, spec.digest, list(native.history), False)
            require_station_zero(journal, book, book.owned_present(native.physical))
            await patch({"AmpLmt": "6"}, native)
            native = await observe()
        # A replacement process may find the write already applied. Confirm the saved and effective limits.
        spec.require(native, journal.identity, "trial", closed=True, allow_unplugged=True)
        audit_history(journal, spec.digest, list(native.history), False)
        require_station_zero(journal, book, book.owned_present(native.physical))
        journal.db.execute("DELETE FROM meta WHERE key='managed_current_capability'")


# A restart after profile cleanup can finish on native HTTP without loading either OCPP peer's TLS files.
async def finish_restore(
    handover: NativeBoundedHandover,
    patch: NativePatch,
    close_listener: Callable[[], Awaitable[None]] | None = None,
    *,
    report_stage: Callable[[str], None] = emit_stage,
) -> dict[str, Any]:
    journal = handover.gateway.journal
    phase = journal.phase.read().value
    if phase == "complete":
        report_stage("verify_restored_configuration")
        handover.require_normal_restoration(await handover.observe())
        return {"original_configuration_restored": True, "native_backend_connected": True, "complete": True}
    if phase != "backend_restore_pending":
        raise RecoveryRequiredError("restoration requires verified profile cleanup")

    # Retire the listener and wait for independent offline evidence before changing the route.
    async def retire() -> None:
        if close_listener is not None:
            await close_listener()
        while (await handover.observe()).physical.upstream_connected:
            await asyncio.sleep(1)

    async with asyncio.timeout(90):
        state = journal.get("native_bounded_handover", {})
        if not state.get("route_restored"):
            report_stage("restore_original_route")
            await handover.restore_route(patch, retire)
        if journal.get("native_bounded_handover", {}).get("stage") != "settings_intent":
            report_stage("verify_vendor_connection")
            while not (await handover.observe_backend())["original_backend_connected"]:
                await asyncio.sleep(1)
        report_stage("restore_original_current")
        return await handover.restore_settings(patch)


# While the maintenance listener owns the journal, the existing cleanup is the only available charger action.
async def cleanup_profiles(session: TrialSession) -> None:
    handover, gateway = session.native_bounded, session.gateway
    async with asyncio.timeout(90):
        await session.start()
        connection = None
        if session.started:
            while session.runtime.introduction_pending or not gateway.current_boot_seen(gateway._charger_epoch):
                await asyncio.sleep(0.25)
            boot = gateway.boot_observation
            assert boot is not None
            handover.spec.require_boot(session.journal.get("boot"))
            connection = (gateway._charger_epoch, boot)
        await handover.cleanup(session.runtime.stop_vendor, connection=connection)


# Both stages use one private installation, and a stopped journal remains exclusive during every write.
async def run(
    action: str,
    config_path: Path,
    spec_path: Path,
    native_port: int = 80,
    *,
    report_stage: Callable[[str], None] = emit_stage,
) -> int:
    prepared = preflight_native(config_path)
    spec = native_spec(spec_path, prepared)
    if action == "check":
        emit({"offline_only": True, "inputs_valid": True, "charger_changed": False})
        return 0
    if action != "restore" or type(native_port) is not int or not 1 <= native_port <= 65535:
        raise SecurityError("invalid charger restoration action or native port")
    config, observer = prepared.config, prepared.observer
    observer.port = native_port

    # Keep a physically unplugged charger throughout cleanup, routing, and current restoration.
    def require_unplugged(native: HandoverEvidence) -> None:
        if not native.is_fresh(5):
            raise StaleEvidenceError("restoration requires fresh native evidence")
        native.physical.require_disconnected(config.identity, max_age=5)
        if native.physical.transaction_open or native.output_limit_a != 0:
            raise RecoveryRequiredError("restoration requires the charger unplugged with closed zero output")

    # All restoration stages use the same unplugged and fresh zero checks.
    async def observe() -> HandoverEvidence:
        native = await observer.observe_handover()
        require_unplugged(native)
        return native

    # Only the commissioned USER limit and original four route fields may be written by this tool.
    async def patch(values: dict[str, Any], native: HandoverEvidence) -> None:
        original_route = {key: spec.original_configuration[key] for key in ROUTE_FIELDS}
        if values not in ({"AmpLmt": "6"}, {"AmpLmt": "32"}, original_route):
            raise SecurityError("unexpected charger restoration settings")
        await read_native(
            config.charger_ip,
            observer.port,
            observer.authorization,
            "setConfig",
            values,
            before_send=lambda: require_unplugged(native),
        )

    report_stage("open_restore_journal")
    with closing(open_native_journal(config.journal, config.identity, spec, require_existing=True)) as journal:
        book = ResourceBook(journal, prepared.baseline)
        phase = journal.phase.read()
        if phase.value in ("backend_restore_pending", "complete"):
            if journal.get(MANAGED_RESTORE_INTENT) != spec.digest:
                raise RecoveryRequiredError("cleanup does not belong to this managed restoration")
            gateway = Gateway(journal, Policy(allow_local_start=False))
            handover = NativeBoundedHandover(
                gateway,
                book,
                observe,
                spec,
                StationChoice(config.station_profile_kind, config.station_stack_level, config.station_profile_start),
                observe_profiles=observer.observe_profiles,
                allow_unplugged=True,
            )
            emit(await finish_restore(handover, patch, report_stage=report_stage))
            return 0
        try:
            tls = prepare_tls(config)
        except SecurityError:
            report_stage("validate_maintenance_credentials")
            raise
        if not phase.cleanup:
            report_stage("verify_unplugged_managed_pause")
        await prepare_cleanup(book, spec, observe, patch, report_stage=report_stage)
    # The durable cleanup phase also fences a service restart between the two exclusive journal opens.
    session = TrialSession(
        PreparedTrial(config, prepared.baseline, observer, tls),
        native_bounded=spec,
        handover_observe=observe,
        allow_local_start=False,
        allow_unplugged_native=True,
        require_existing_journal=True,
    )
    restore_failed = False
    try:
        if session.journal.get(MANAGED_RESTORE_INTENT) != spec.digest:
            raise RecoveryRequiredError("cleanup does not belong to this managed restoration")
        if session.journal.phase.read().value == "rollback":
            await cleanup_profiles(session)
        result = await finish_restore(session.native_bounded, patch, session.runtime.close, report_stage=report_stage)
    except BaseException:
        restore_failed = True
        raise
    finally:
        try:
            await session.close()
        except Exception:
            # Keep the stage and error that first interrupted restoration.
            if not restore_failed:
                report_stage("close_maintenance_session")
                raise
    emit(result)
    return 0


# Run the requested operation and report failures with their current restore step.
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "restore"), nargs="?", default="check")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--native-bounded-spec", type=Path, required=True)
    parser.add_argument("--native-port", type=int, default=80)
    args = parser.parse_args()
    os.umask(0o077)
    stage = "validate_inputs"

    # Retain the most recent step for a structured failure report.
    def report_stage(current: str) -> None:
        nonlocal stage
        stage = current
        emit_stage(stage)

    try:
        return asyncio.run(
            run(args.action, args.config, args.native_bounded_spec, args.native_port, report_stage=report_stage)
        )
    except Exception as error:
        reason, next_step = restore_guidance(stage, error)
        emit({
            "restore_failed": type(error).__name__,
            "complete": False,
            "restore_stage": stage,
            "restore_reason": reason,
            "next_step": next_step,
        })
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
