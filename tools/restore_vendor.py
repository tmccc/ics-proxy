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
from ics_proxy_core.engine.managed_current import managed_current_ceiling  # noqa: E402
from ics_proxy_core.engine.managed_recovery_zero import require_original_zero, require_station_zero  # noqa: E402
from ics_proxy_core.engine.native_bounded import ROUTE_FIELDS, NativeBoundedSpec  # noqa: E402
from ics_proxy_core.engine.native_history import audit_history  # noqa: E402
from ics_proxy_core.engine.native_history_recovery import history_recovery_idle  # noqa: E402
from ics_proxy_core.engine.native_http import read_native  # noqa: E402
from ics_proxy_core.engine.security import SecurityError  # noqa: E402
from ics_proxy_core.engine.store import RecoveryRequiredError  # noqa: E402
from ics_proxy_core.engine.trial_handover_evidence import HandoverEvidence  # noqa: E402
from ics_proxy_core.engine.trial_resources import ResourceBook  # noqa: E402
from ics_proxy_core.engine.trial_session import TrialSession, open_native_journal, preflight  # noqa: E402
from tools.trial_proxy import emit, native_spec  # noqa: E402

_RESTORE_INTENT = "managed_vendor_restore"
type Observe = Callable[[], Awaitable[HandoverEvidence]]
type Patch = Callable[[dict[str, Any]], Awaitable[None]]


# Freeze managed operation before lowering the independently enforced USER limit or removing any profile.
async def prepare_cleanup(book: ResourceBook, spec: NativeBoundedSpec, observe: Observe, patch: Patch) -> None:
    journal = book.journal
    phase = journal.phase.read()
    intent = journal.get(_RESTORE_INTENT)
    if phase.cleanup and intent != spec.digest:
        raise RecoveryRequiredError("cleanup does not belong to this managed restoration")
    if phase.value in ("backend_restore_pending", "complete"):
        return

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
        require_original_zero(journal, book, spec, native)
        with journal.atomic():
            journal.put(_RESTORE_INTENT, spec.digest)
            journal.phase.begin_cleanup()

    if ceiling == 32:
        if native.saved_user_a == 32:
            spec.require_managed(native, journal.identity, closed=True, allow_unplugged=True)
            audit_history(journal, spec.digest, list(native.history), False)
            require_station_zero(journal, book, book.owned_present(native.physical))
            await patch({"AmpLmt": "6"})
            native = await observe()
        # A replacement process may find the write already applied. Confirm the saved and effective limits.
        spec.require(native, journal.identity, "trial", closed=True, allow_unplugged=True)
        audit_history(journal, spec.digest, list(native.history), False)
        require_station_zero(journal, book, book.owned_present(native.physical))
        journal.db.execute("DELETE FROM meta WHERE key='managed_current_capability'")


# While the maintenance listener owns the journal, the existing cleanup is the only available charger action.
async def restore(session: TrialSession, observe: Observe, patch: Patch) -> dict[str, Any]:
    handover, journal = session.native_bounded, session.journal
    if journal.phase.read().value == "complete":
        handover.require_normal_restoration(await observe())
        return {"original_configuration_restored": True, "native_backend_connected": True, "complete": True}

    if journal.phase.read().value == "rollback":
        emit({"restore_stage": "verify_6_a_and_remove_proxy_profiles"})
        async with asyncio.timeout(90):
            await session.start()
            connection = None
            if session.started:
                gateway = session.gateway
                while session.runtime.introduction_pending or not gateway.current_boot_seen(gateway._charger_epoch):
                    await asyncio.sleep(0.25)
                boot = gateway.boot_observation
                assert boot is not None
                handover.spec.require_boot(journal.get("boot"))
                connection = (gateway._charger_epoch, boot)
            await handover.cleanup(session.runtime.stop_vendor, connection=connection)

    # Retire the listener and wait for independent offline evidence before changing the route.
    async def retire() -> None:
        await session.runtime.close()
        while (await observe()).physical.upstream_connected:
            await asyncio.sleep(1)

    async with asyncio.timeout(90):
        state = journal.get("native_bounded_handover", {})
        if not state.get("route_restored"):
            emit({"restore_stage": "restore_original_route"})
            await handover.restore_route(patch, retire)
        if journal.get("native_bounded_handover", {}).get("stage") != "settings_intent":
            emit({"restore_stage": "verify_vendor_connection"})
            while not (await handover.observe_backend())["original_backend_connected"]:
                await asyncio.sleep(1)
        emit({"restore_stage": "restore_original_current"})
        return await handover.restore_settings(patch)


# Both stages use one private installation, and a stopped journal remains exclusive during every write.
async def run(action: str, config_path: Path, spec_path: Path, native_port: int = 80) -> int:
    prepared = preflight(config_path)
    spec = native_spec(spec_path, prepared)
    if action == "check":
        emit({"offline_only": True, "inputs_valid": True, "charger_changed": False})
        return 0
    if action != "restore" or type(native_port) is not int or not 1 <= native_port <= 65535:
        raise SecurityError("invalid charger restoration action or native port")
    config, observer = prepared.config, prepared.observer
    observer.port = native_port

    # Keep a physically unplugged charger throughout cleanup, routing, and current restoration.
    async def observe() -> HandoverEvidence:
        native = await observer.observe_handover()
        native.physical.require_disconnected(config.identity, max_age=5)
        if native.physical.transaction_open or native.output_limit_a != 0:
            raise RecoveryRequiredError("restoration requires the charger unplugged with closed zero output")
        return native

    # Only the commissioned USER limit and original four route fields may be written by this tool.
    async def patch(values: dict[str, Any]) -> None:
        original_route = {key: spec.original_configuration[key] for key in ROUTE_FIELDS}
        if values not in ({"AmpLmt": "6"}, {"AmpLmt": "32"}, original_route):
            raise SecurityError("unexpected charger restoration settings")
        await read_native(config.charger_ip, observer.port, observer.authorization, "setConfig", values)

    if not config.journal.is_file():
        raise RecoveryRequiredError("restoration requires the existing managed journal")
    emit({"restore_stage": "verify_unplugged_managed_pause"})
    with closing(open_native_journal(config.journal, config.identity, spec)) as journal:
        await prepare_cleanup(ResourceBook(journal, prepared.baseline), spec, observe, patch)
    # The durable cleanup phase also fences a service restart between the two exclusive journal opens.
    session = TrialSession(
        prepared,
        native_bounded=spec,
        handover_observe=observe,
        allow_local_start=False,
        allow_unplugged_native=True,
    )
    try:
        result = await restore(session, observe, patch)
    finally:
        await session.close()
    emit(result)
    return 0


# Configuration and native exception bodies can contain private values, so report only the failure class.
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "restore"), nargs="?", default="check")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--native-bounded-spec", type=Path, required=True)
    parser.add_argument("--native-port", type=int, default=80)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        return asyncio.run(run(args.action, args.config, args.native_bounded_spec, args.native_port))
    except Exception as error:
        emit({"restore_failed": type(error).__name__, "complete": False})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
