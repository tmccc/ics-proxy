# Modified for the standalone iCS proxy repository.
"""Rehearse managed handback through the same HTTP and TLS boundaries as the operator command."""

from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
import sys
import time
import uuid
from contextlib import closing
from functools import partial
from types import SimpleNamespace

import pytest

from ics_proxy_core.engine import trial_handover_evidence
from ics_proxy_core.engine.native_bounded_handover import NativeBoundedHandover
from ics_proxy_core.engine.native_http import NativeTransportUnavailableError, read_native
from ics_proxy_core.engine.security import SecurityError, StaleEvidenceError
from ics_proxy_core.engine.store import RecoveryRequiredError
from ics_proxy_core.engine.trial_session import save_private
from ics_proxy_core.frames import basic_auth_header
from tests.charge_rig import actual_stop, positive_flow, ready
from tests.current_rig import commissioned
from tests.handover_helpers import NativeVehiclePeer
from tests.managed_rig import healthy, starts
from tests.prototype_rig import BOOT
from tests.tls_support import tls as tls
from tools import restore_vendor
from tools.managed_proxy import serve
from tools.restore_vendor import run


class RestorePeer(NativeVehiclePeer):
    """The simulated charger applies and reports its own settings across real HTTP requests."""

    # Record the device's state at each settings write and optionally lose one reply after applying the setting.
    def __init__(self, bench):
        super().__init__(bench)
        self.writes = []
        self.user_limits_at_write = []
        self.lost_patch = None
        self.route_changed = False
        self.read_original_offline = asyncio.Event()
        self.empty_connection = asyncio.Event()

    # Handle the ordinary observation protocol plus native saved-setting writes from the real restore tool.
    async def handle(self, reader, writer):
        try:
            try:
                headers = await reader.readuntil(b"\r\n\r\n")
            except asyncio.IncompleteReadError as error:
                assert not error.partial
                self.empty_connection.set()
                return
            fields = {
                line.split(b":", 1)[0].lower(): line.split(b":", 1)[1].strip()
                for line in headers.split(b"\r\n")[1:]
                if b":" in line
            }
            assert fields[b"authorization"].decode() == basic_auth_header("admin", "NATIVE_TEST_SECRET")[1]
            request = json.loads(await reader.readexactly(int(fields[b"content-length"])))
            action, data = request["action"], request.get("data")
            self.requests.append(action)
            if action == "setConfig":
                self.writes.append((data, self.bench.firmware.effective_limit(), self.bench.online))
                self.user_limits_at_write.append(self.runtime_user)
                self.configuration.update(data)
                if "AmpLmt" in data:
                    self.runtime_user = int(data["AmpLmt"]) * 10
                if "sDomain" in data:
                    self.route_changed = True
                if data == self.lost_patch:
                    self.lost_patch = None
                    return
                value = None
            else:
                assert action in ("getConfig", "statistics", "getTransaction")
                key = "config" if action == "getConfig" else "TRANSACTIONS" if action == "getTransaction" else data
                value = self.native(key)
                if key == "OCPP" and self.route_changed and not value["online"]:
                    self.read_original_offline.set()
            body = json.dumps({"code": 200, "data": json.dumps(value)}).encode()
            writer.write(b"HTTP/1.0 200 OK\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()


# Save the same approved specification used for the journal so CLI preflight tests its actual listener binding.
def spec_file(directory, rig):
    path = directory / "native-spec.json"
    save_private(path, rig.spec.value())
    return path


# Reconnect the simulated charger to the real cleanup listener and observe the transition when it retires.
async def cleanup_charger(test, *, boot=BOOT, connected=None, on_disconnect=None):
    bench = test.rig.bench
    async with asyncio.timeout(8):
        while True:
            try:
                charger = await bench.connection_to(test.session)
                break
            except ConnectionRefusedError:
                await asyncio.sleep(0.02)
    bench.online = True
    reader = asyncio.create_task(bench.read_charger(charger))
    try:
        assert (await bench.call(charger, "restore-cleanup-boot", "BootNotification", boot))[0] == 3
        if connected is not None:
            connected.set_result(charger)
        await reader
        if on_disconnect is not None:
            on_disconnect()
    finally:
        bench.online = False
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
        await charger.close()


# Stop an actual session through managed Pause before checking deployment prerequisites on the stopped proxy.
async def stop_paused(test, *, unplug=True):
    local = await positive_flow(test, "daily")
    controller = test.controller
    pause = uuid.uuid4().hex
    controller.command(pause, "pause", controller.requests.intent().revision)
    assert (await actual_stop(test, local))["local_hold_verified"]
    assert controller.requests.receipt(pause)["state"] == "completed"
    assert test.session.journal.get("control")["resume_limit"] == 6
    if unplug:
        test.rig.bench.cp, test.rig.bench.connection = "A", "Unplug"
    await test.session.close()
    test.rig.bench.online = False


# Read the same database directly once the tool has closed it and released its lock.
def journal_facts(path):
    with closing(sqlite3.connect(path)) as db:
        rows = db.execute("SELECT * FROM transactions ORDER BY local").fetchall()
        phase = db.execute("SELECT value FROM meta WHERE key='trial_phase'").fetchone()
    return rows, json.loads(phase[0])


# Read the real CLI failure report and check that it leaves no private paths in the output.
async def failed_restore_cli(action, config, spec, native_port=80):
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-B",
        restore_vendor.__file__,
        action,
        "--config",
        str(config),
        "--native-bounded-spec",
        str(spec),
        "--native-port",
        str(native_port),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(child.communicate(), 8)
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()
    assert child.returncode == 1 and not stderr
    assert str(config).encode() not in stdout and str(spec).encode() not in stdout
    report = json.loads(stdout.splitlines()[-1])
    assert report["complete"] is False
    return report


# Intent: give useful restore guidance for known refusals while keeping unclassified error details private.
# Why: journal or native failures can embed private data, including during an interrupted restoration.
@pytest.mark.parametrize(
    ("stage", "error", "reason", "hint"),
    [
        (
            "verify_unplugged_managed_pause",
            RecoveryRequiredError("restoration needs a settled managed Pause at a selected 6 A"),
            "settled_pause_required",
            "settled Pause with 6 A selected",
        ),
        (
            "verify_unplugged_managed_pause",
            RecoveryRequiredError("cleanup does not belong to this managed restoration"),
            "restore_record_mismatch",
            "matching set for this installation",
        ),
        (
            "verify_vendor_connection",
            TimeoutError("private fixture detail"),
            "vendor_connection_unverified",
            "Check that the charger can reach iCS",
        ),
        (
            "restore_original_route",
            TimeoutError("private fixture detail"),
            "restore_stage_failed",
            "Review the restore instructions for this stage",
        ),
        (
            "verify_unplugged_managed_pause",
            StaleEvidenceError("private fixture detail"),
            "native_evidence_unavailable",
            "reachable locally",
        ),
        (
            "restore_original_current",
            NativeTransportUnavailableError("private fixture detail"),
            "native_evidence_unavailable",
            "reachable locally",
        ),
    ],
)
def test_restore_guidance_matches_observed_stage_without_private_details(stage, error, reason, hint):
    actual_reason, next_step = restore_vendor.restore_guidance(stage, error)
    assert actual_reason == reason and hint in next_step
    assert "private fixture detail" not in next_step


# Intent: report invalid restore inputs with the stage and a corrective action.
# Why: the operator needs enough information to fix the configuration without exposing private paths.
async def test_invalid_restore_inputs_report_the_stage_without_private_paths(tmp_path):
    report = await failed_restore_cli("check", tmp_path / "private-config.json", tmp_path / "private-spec.json")
    assert report["restore_stage"] == "validate_inputs"
    assert report["restore_reason"] == "inputs_unverified"
    assert "configuration and commissioning files" in report["next_step"]


# Intent: input checks run offline and restoration refuses an already-owned journal before contacting the charger.
# Why: a running proxy can keep operating while an installer checks inputs, and it must remain the only owner.
async def test_restore_check_is_offline(tmp_path, tls, unused_tcp_port, monkeypatch):
    async with commissioned(
        tmp_path, tls, unused_tcp_port, monkeypatch, 6, cli_route=True, native_peer=RestorePeer
    ) as test:
        peer = test.rig.peer
        reads = list(peer.requests)
        config, spec = tmp_path / "trial.json", spec_file(tmp_path, test.rig)
        port = test.rig.prepared.observer.port
        assert await run("check", config, spec, port) == 0
        with pytest.raises(RecoveryRequiredError, match="journal already has an active owner"):
            await run("restore", config, spec, port)
        report = await failed_restore_cli("restore", config, spec, port)
        assert report["restore_failed"] == "RecoveryRequiredError"
        assert report["restore_stage"] == "open_restore_journal"
        assert report["restore_reason"] == "journal_in_use"
        assert "Stop the proxy service" in report["next_step"]
        assert peer.requests == reads and not peer.writes


# Intent: identify missing maintenance credentials in the restore CLI before it contacts the charger.
# Why: the offline native check succeeds independently and later restore stages can resume without TLS files.
async def test_restore_reports_missing_maintenance_credentials(tmp_path, tls, unused_tcp_port, monkeypatch):
    async with commissioned(
        tmp_path, tls, unused_tcp_port, monkeypatch, 6, cli_route=True, native_peer=RestorePeer
    ) as test:
        await stop_paused(test)
        rig = test.rig
        config, spec = tmp_path / "trial.json", spec_file(tmp_path, rig)
        private_key = rig.prepared.config.private_key
        private_key.unlink()
        port = rig.prepared.observer.port
        reads = list(rig.peer.requests)

        assert await run("check", config, spec, port) == 0
        report = await failed_restore_cli("restore", config, spec, port)
        assert report["restore_stage"] == "validate_maintenance_credentials"
        assert report["restore_reason"] == "maintenance_credentials_unverified"
        assert "private key" in report["next_step"]
        assert str(private_key) not in json.dumps(report)
        assert rig.peer.requests == reads and not rig.peer.writes


# Intent: tell the operator how to establish Pause before any restore change has been made.
# Why: starting the proxy to request Pause is only possible before restoration begins.
async def test_restore_reports_missing_managed_pause(tmp_path, tls, unused_tcp_port, monkeypatch):
    async with commissioned(
        tmp_path, tls, unused_tcp_port, monkeypatch, 6, cli_route=True, native_peer=RestorePeer
    ) as test:
        rig = test.rig
        assert (await healthy(test.controller))["owner_control_enabled"]
        await test.session.close()
        rig.bench.online = False
        rig.bench.cp, rig.bench.connection = "A", "Unplug"
        records, phase = journal_facts(rig.prepared.config.journal)

        report = await failed_restore_cli(
            "restore", tmp_path / "trial.json", spec_file(tmp_path, rig), rig.prepared.observer.port
        )

        assert report["restore_stage"] == "verify_unplugged_managed_pause"
        assert report["restore_reason"] == "settled_pause_required"
        assert "Start the proxy service and request Pause charger in Home Assistant" in report["next_step"]
        assert "stop the proxy service and rerun restore" in report["next_step"]
        assert journal_facts(rig.prepared.config.journal) == (records, phase)
        assert not rig.peer.writes


# Intent: keep the service retired when a resumed restore cannot verify the native current.
# Why: an interrupted current write has already committed cleanup intent even if it never returned a reply.
async def test_restore_keeps_proxy_stopped_after_interrupted_write(tmp_path, tls, unused_tcp_port, monkeypatch):
    async with commissioned(
        tmp_path, tls, unused_tcp_port, monkeypatch, 6, cli_route=True, native_peer=RestorePeer
    ) as test:
        rig = test.rig
        await stop_paused(test)
        peer = rig.peer
        args = ("restore", tmp_path / "trial.json", spec_file(tmp_path, rig), rig.prepared.observer.port)
        peer.lost_patch = {"AmpLmt": "6"}
        report = await failed_restore_cli(*args)
        assert report["restore_stage"] == "verify_6_a_and_remove_proxy_profiles"
        assert report["restore_reason"] == "native_evidence_unavailable"
        records, phase = journal_facts(rig.prepared.config.journal)
        assert phase == "rollback"

        assert peer.configuration["AmpLmt"] == "6"
        peer.runtime_user = 320
        report = await failed_restore_cli(*args)

        assert report["restore_stage"] == "verify_6_a_and_remove_proxy_profiles"
        assert report["restore_reason"] == "restore_stage_failed"
        assert "Keep the proxy stopped" in report["next_step"]
        assert journal_facts(rig.prepared.config.journal) == (records, phase)
        assert [patch for patch, _, _ in peer.writes] == [{"AmpLmt": "6"}]


# Intent: a plugged vehicle blocks managed restoration before any saved configuration or profile changes.
# Why: the original backend may independently allow charging as soon as the proxy hands control back.
async def test_restore_refuses_plugged_vehicle_before_any_write(tmp_path, tls, unused_tcp_port, monkeypatch):
    async with commissioned(
        tmp_path, tls, unused_tcp_port, monkeypatch, 6, cli_route=True, native_peer=RestorePeer
    ) as test:
        await stop_paused(test, unplug=False)
        peers_before = list(test.rig.bench.device_calls)
        config, spec = tmp_path / "trial.json", spec_file(tmp_path, test.rig)
        port = test.rig.prepared.observer.port
        with pytest.raises((SecurityError, RecoveryRequiredError)):
            await run("restore", config, spec, port)
        report = await failed_restore_cli("restore", config, spec, port)
        assert report["restore_failed"] == "SecurityError"
        assert report["restore_stage"] == "verify_unplugged_managed_pause"
        assert report["restore_reason"] == "unplugged_idle_required"
        assert "unplug the car" in report["next_step"]
        assert not test.rig.peer.writes and test.rig.bench.device_calls == peers_before
        assert test.rig.peer.configuration["AmpLmt"] == "32"


# Intent: a charger that sends a different Boot cannot clear the proxy's verified zero profile or switch routes.
# Why: an exact commissioning match must still hold when the maintenance listener takes over after shutdown.
async def test_restore_refuses_a_changed_cleanup_boot(tmp_path, tls, unused_tcp_port, monkeypatch):
    async with commissioned(
        tmp_path, tls, unused_tcp_port, monkeypatch, 6, cli_route=True, native_peer=RestorePeer
    ) as test:
        rig = test.rig
        await stop_paused(test)
        config, spec = tmp_path / "trial.json", spec_file(tmp_path, rig)
        restore = asyncio.create_task(run("restore", config, spec, rig.prepared.observer.port))
        charger = asyncio.create_task(cleanup_charger(test, boot=BOOT | {"firmwareVersion": "other"}))
        try:
            with pytest.raises(SecurityError, match="current-limit persistence was not proved"):
                await asyncio.wait_for(restore, 8)
            await asyncio.wait_for(charger, 3)
        finally:
            for task in (restore, charger):
                task.cancel()
            await asyncio.gather(restore, charger, return_exceptions=True)

        assert [patch for patch, _, _ in rig.peer.writes] == [{"AmpLmt": "6"}]
        assert rig.peer.configuration == rig.spec.configuration("trial")
        assert rig.bench.firmware.effective_limit() == 0 and 7001 in rig.bench.firmware.profiles
        assert journal_facts(rig.prepared.config.journal)[1] == "rollback" and not starts(rig)


# Intent: force a genuine charger connection replacement after the original Boot check but before any clear frame.
# Why: even an identical Boot on a new connection cannot borrow the original cleanup authority.
async def test_restore_refuses_a_cleanup_connection_replacement(tmp_path, tls, unused_tcp_port, monkeypatch):
    async with commissioned(
        tmp_path, tls, unused_tcp_port, monkeypatch, 6, cli_route=True, native_peer=RestorePeer
    ) as test:
        rig = test.rig
        await stop_paused(test)
        begin_gate, release_gate = asyncio.Event(), asyncio.Event()
        cleanup = NativeBoundedHandover.cleanup

        # Delay the real cleanup after vendor quiescence, while the test replaces the real charger TLS socket.
        async def gate(handover, quiesce, *, connection):
            # Pause after real cloud transport retirement and before the first physical profile clear.
            async def held_quiesce():
                await quiesce()
                begin_gate.set()
                await release_gate.wait()

            return await cleanup(handover, held_quiesce, connection=connection)

        monkeypatch.setattr(NativeBoundedHandover, "cleanup", gate)
        config, spec = tmp_path / "trial.json", spec_file(tmp_path, rig)
        restore = asyncio.create_task(run("restore", config, spec, rig.prepared.observer.port))
        original = asyncio.get_running_loop().create_future()
        replacement = asyncio.get_running_loop().create_future()
        tasks = [restore, asyncio.create_task(cleanup_charger(test, connected=original))]
        first_call = len(rig.bench.device_calls)
        try:
            await asyncio.wait_for(begin_gate.wait(), 8)
            await (await original).close()
            await asyncio.wait_for(tasks[-1], 3)
            tasks.append(asyncio.create_task(cleanup_charger(test, connected=replacement)))
            await asyncio.wait_for(replacement, 3)
            release_gate.set()
            with pytest.raises(RecoveryRequiredError, match="cleanup connection or Boot changed"):
                await asyncio.wait_for(restore, 8)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        assert [patch for patch, _, _ in rig.peer.writes] == [{"AmpLmt": "6"}]
        assert all(frame[2] != "ClearChargingProfile" for frame in rig.bench.device_calls[first_call:])
        assert 7001 in rig.bench.firmware.profiles and rig.bench.firmware.effective_limit() == 0


# Intent: refuse a profile that reappears before routing, then resume after independent cleanup.
# Why: the route change requires verified absence even after a previously confirmed clear.
@pytest.mark.parametrize(
    "stage", ["awaiting_backend", "route_intent"], ids=["before-route-check", "listener-retirement"]
)
async def test_restore_refuses_a_profile_reappearing_after_cleanup(tmp_path, tls, unused_tcp_port, monkeypatch, stage):
    async with commissioned(
        tmp_path, tls, unused_tcp_port, monkeypatch, 6, cli_route=True, native_peer=RestorePeer
    ) as test:
        rig = test.rig
        peer, bench = rig.peer, rig.bench
        await stop_paused(test)
        saved_profile = copy.deepcopy(bench.firmware.profiles[7001])

        # Model the charger independently resurrecting a previously cleared profile.
        def reappear():
            assert 7001 not in bench.firmware.profiles
            bench.firmware.profiles[7001] = saved_profile

        if stage == "awaiting_backend":
            complete_cleanup = NativeBoundedHandover._complete_cleanup

            # The cleanup journal commits before the first route check can observe the profile again.
            def complete_and_reappear(handover):
                complete_cleanup(handover)
                reappear()

            monkeypatch.setattr(NativeBoundedHandover, "_complete_cleanup", complete_and_reappear)

        # Inspect the saved fault after the restoration tool closes its journal.
        def inventory_fault():
            with closing(sqlite3.connect(rig.prepared.config.journal)) as db:
                row = db.execute("SELECT value FROM meta WHERE key='trial_inventory_fault'").fetchone()
                return json.loads(row[0])

        config, spec = tmp_path / "trial.json", spec_file(tmp_path, rig)
        args = ("restore", config, spec, rig.prepared.observer.port)
        restore = asyncio.create_task(run(*args))
        on_disconnect = reappear if stage == "route_intent" else None
        charger = asyncio.create_task(cleanup_charger(test, on_disconnect=on_disconnect))
        try:
            with pytest.raises(SecurityError, match="resource_reappeared"):
                await asyncio.wait_for(restore, 8)
            await asyncio.wait_for(charger, 3)
        finally:
            for task in (restore, charger):
                task.cancel()
            await asyncio.gather(restore, charger, return_exceptions=True)

        records, phase = journal_facts(rig.prepared.config.journal)
        assert phase == "backend_restore_pending"
        with closing(sqlite3.connect(rig.prepared.config.journal)) as db:
            row = db.execute("SELECT value FROM meta WHERE key='native_bounded_handover'").fetchone()
            handover = json.loads(row[0])
        assert handover["stage"] == stage and not handover.get("route_restored")
        fault = {"reason": "resource_reappeared", "resource": "profile:7001"}
        assert inventory_fault() == fault
        rig.prepared.config.certificate.unlink()
        rig.prepared.config.private_key.unlink()
        peers_before = list(bench.device_calls)
        with pytest.raises(SecurityError, match="native inventory reconciliation is still required"):
            await asyncio.wait_for(run(*args), 5)
        saved_profile["chargingSchedule"]["chargingSchedulePeriod"][0]["limit"] = 6
        with pytest.raises(SecurityError, match="native inventory reconciliation is still required"):
            await asyncio.wait_for(run(*args), 5)
        del bench.firmware.profiles[7001]
        bench.firmware.profiles[7002] = saved_profile | {"chargingProfileId": 7002}
        with pytest.raises(SecurityError, match="native inventory reconciliation is still required"):
            await asyncio.wait_for(run(*args), 5)
        assert [patch for patch, _, _ in peer.writes] == [{"AmpLmt": "6"}]
        assert peer.configuration == rig.spec.configuration("trial")
        assert inventory_fault() == fault
        del bench.firmware.profiles[7002]

        # Reconnect only once the original route is observed offline at 6 A.
        async def reconnect_vendor():
            await peer.read_original_offline.wait()
            assert peer.runtime_user == 60 and not bench.online
            bench.online = True

        restore = asyncio.create_task(run(*args))
        vendor = asyncio.create_task(reconnect_vendor())
        try:
            result, _ = await asyncio.wait_for(asyncio.gather(restore, vendor), 5)
            assert result == 0
        finally:
            for task in (restore, vendor):
                task.cancel()
            await asyncio.gather(restore, vendor, return_exceptions=True)

        route = {key: rig.spec.original_configuration[key] for key in rig.spec.trial_route}
        assert [patch for patch, _, _ in peer.writes] == [{"AmpLmt": "6"}, route, {"AmpLmt": "32"}]
        assert peer.writes[1][2] is False and peer.writes[-1][2] is True
        assert peer.configuration == rig.spec.original_configuration and peer.runtime_user == 320
        assert bench.device_calls == peers_before and not bench.firmware.profiles
        assert journal_facts(rig.prepared.config.journal) == (records, "complete")
        assert inventory_fault() == fault


# Advance only the evidence clock after TCP opens for one specific settings write.
def expire_evidence_on_connect(monkeypatch, target):
    open_connection = asyncio.open_connection
    expire = True

    # Apply the clock change to the first matching write so a fresh retry can finish.
    async def request(host, port, authorization, action, data, **kwargs):
        nonlocal expire
        if not expire or action != "setConfig" or data != target:
            return await read_native(host, port, authorization, action, data, **kwargs)
        expire = False
        offset = 0

        # Open a real loopback stream before making the last sample expire.
        async def delayed_connection(*args, **kwargs):
            nonlocal offset
            streams = await open_connection(*args, **kwargs)
            offset = 6
            return streams

        with monkeypatch.context() as timing:
            timing.setattr(asyncio, "open_connection", delayed_connection)
            timing.setattr(
                trial_handover_evidence,
                "time",
                SimpleNamespace(time=time.time, monotonic=lambda: time.monotonic() + offset),
            )
            return await read_native(host, port, authorization, action, data, **kwargs)

    monkeypatch.setattr(restore_vendor, "read_native", request)


# Intent: rehearse handback from each managed native bound and interrupt every applicable write.
# Why: an applied write must survive a lost reply, and the 6 A bound needs no preliminary current write.
@pytest.mark.parametrize(
    ("native_bound", "interruption", "stage"),
    [
        pytest.param(bound, kind, stage, id=f"{bound}a-{kind or 'uninterrupted'}-{stage or 'all'}")
        for bound in (6, 32)
        for kind, stage in [
            (None, None),
            *((kind, stage) for kind in ("reply", "connect") for stage in ("lower", "route", "raise")),
        ]
        if bound == 32 or stage != "lower"
    ],
)
async def test_restore_keeps_history_for_each_managed_bound(
    tmp_path, tls, unused_tcp_port, monkeypatch, capsys, native_bound, interruption, stage
):
    prepare = partial(commissioned, current=6) if native_bound == 32 else ready
    async with prepare(tmp_path, tls, unused_tcp_port, monkeypatch, cli_route=True, native_peer=RestorePeer) as test:
        rig = test.rig
        peer, bench = rig.peer, rig.bench
        assert peer.runtime_user == native_bound * 10
        assert (test.session.journal.get("managed_current_capability") is not None) is (native_bound == 32)
        await stop_paused(test)
        db_path = rig.prepared.config.journal
        records, _ = journal_facts(db_path)
        assert records
        history = copy.deepcopy(bench.history)
        plans = copy.deepcopy(bench.firmware.plans)
        config, spec = tmp_path / "trial.json", spec_file(tmp_path, rig)
        args = ("restore", config, spec, rig.prepared.observer.port)
        first_call = len(bench.device_calls)
        first_limit = len(bench.received_limits)
        route = {key: rig.spec.original_configuration[key] for key in rig.spec.trial_route}
        lower_writes = [{"AmpLmt": "6"}] if native_bound == 32 else []
        target = {"lower": {"AmpLmt": "6"}, "route": route, "raise": {"AmpLmt": "32"}}.get(stage)
        failure = NativeTransportUnavailableError
        if interruption == "reply":
            peer.lost_patch = target
        if interruption == "connect":
            expire_evidence_on_connect(monkeypatch, target)
            failure = StaleEvidenceError
        if stage == "lower":
            with pytest.raises(failure):
                await asyncio.wait_for(run(*args), 5)
            if interruption == "connect":
                await asyncio.wait_for(peer.empty_connection.wait(), 1)
                assert not peer.writes
            assert peer.runtime_user == (60 if interruption == "reply" else 320)
            assert bench.firmware.effective_limit() == 0 and not peer.route_changed
            with pytest.raises(RecoveryRequiredError, match="restoration has begun"):
                await asyncio.wait_for(serve(config, spec), 3)
            assert not bench.online
        restore_task = asyncio.create_task(run(*args))
        charger_task = asyncio.create_task(cleanup_charger(test))
        try:
            if stage == "route":
                with pytest.raises(failure):
                    await asyncio.wait_for(restore_task, 10)
                if interruption == "connect":
                    await asyncio.wait_for(peer.empty_connection.wait(), 1)
                    assert [patch for patch, _, _ in peer.writes] == lower_writes
                assert peer.route_changed == (interruption == "reply") and peer.runtime_user == 60
                rig.prepared.config.certificate.unlink()
                rig.prepared.config.private_key.unlink()
                restore_task = asyncio.create_task(run(*args))
            await asyncio.wait_for(peer.read_original_offline.wait(), 10)
            assert not restore_task.done() and peer.runtime_user == 60
            assert not bench.firmware.profiles and not bench.online
            await asyncio.wait_for(charger_task, 3)
            bench.online = True
            if stage == "raise":
                with pytest.raises(failure):
                    await asyncio.wait_for(restore_task, 5)
                if interruption == "connect":
                    await asyncio.wait_for(peer.empty_connection.wait(), 1)
                    assert [patch for patch, _, _ in peer.writes] == [*lower_writes, route]
                    assert peer.configuration == rig.spec.configuration("original")
                else:
                    assert peer.configuration == rig.spec.original_configuration
                assert peer.runtime_user == (320 if interruption == "reply" else 60)
                rig.prepared.config.certificate.unlink()
                rig.prepared.config.private_key.unlink()
                restore_task = asyncio.create_task(run(*args))
            assert await asyncio.wait_for(restore_task, 5) == 0
        finally:
            for task in (restore_task, charger_task):
                task.cancel()
            await asyncio.gather(restore_task, charger_task, return_exceptions=True)

        assert [patch for patch, _, _ in peer.writes] == [*lower_writes, route, {"AmpLmt": "32"}]
        if lower_writes:
            assert peer.writes[0][1] == 0
        assert peer.user_limits_at_write == ([320, 60, 60] if lower_writes else [60, 60])
        assert peer.writes[-2][2] is False and peer.writes[-1][2] is True
        assert peer.configuration == rig.spec.original_configuration and peer.runtime_user == 320
        assert not bench.firmware.profiles and bench.firmware.plans == plans and bench.history == history
        assert all(limit == 0 for limit in bench.received_limits[first_limit:])
        assert [frame[3] for frame in bench.device_calls[first_call:] if frame[2] == "ClearChargingProfile"] == [
            {"id": 7001}
        ]
        assert journal_facts(db_path) == (records, "complete") and not starts(rig)
        output = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert output[-1] == {
            "original_configuration_restored": True,
            "native_backend_connected": True,
            "complete": True,
        }
        stages = {
            "open_restore_journal",
            "verify_unplugged_managed_pause",
            "verify_6_a_and_remove_proxy_profiles",
            "restore_original_route",
            "verify_vendor_connection",
            "restore_original_current",
        }
        assert all(report == {"restore_stage": report.get("restore_stage")} for report in output[:-1])
        assert {report["restore_stage"] for report in output[:-1]} == stages

        recheck_stages = []
        writes = list(peer.writes)
        assert await run(*args, report_stage=recheck_stages.append) == 0
        assert recheck_stages == ["open_restore_journal", "verify_restored_configuration"]
        assert peer.writes == writes and journal_facts(db_path) == (records, "complete")


# Intent: resume a route write that succeeded before a lost reply and a backward host clock correction.
# Why: the journal and fresh independent observations establish order even when their wall times reverse.
async def test_restore_reconnect_after_backward_host_clock_correction(tmp_path, tls, unused_tcp_port, monkeypatch):
    async with commissioned(
        tmp_path, tls, unused_tcp_port, monkeypatch, 6, cli_route=True, native_peer=RestorePeer
    ) as test:
        rig = test.rig
        await stop_paused(test)
        records, _ = journal_facts(rig.prepared.config.journal)
        wall_time, clock_offset = time.time, 120
        monkeypatch.setattr(time, "time", lambda: wall_time() + clock_offset)
        peer, bench = rig.peer, rig.bench
        route = {key: rig.spec.original_configuration[key] for key in rig.spec.trial_route}
        args = ("restore", tmp_path / "trial.json", spec_file(tmp_path, rig), rig.prepared.observer.port)
        peer.lost_patch = route
        async with asyncio.timeout(10), asyncio.TaskGroup() as tasks:
            tasks.create_task(cleanup_charger(test))
            with pytest.raises(NativeTransportUnavailableError):
                await run(*args)
        assert peer.route_changed and not bench.online and peer.runtime_user == 60
        assert [patch for patch, _, _ in peer.writes] == [{"AmpLmt": "6"}, route]

        bench.online = True
        clock_offset = 0
        resume_stages = []
        assert await asyncio.wait_for(run(*args, report_stage=resume_stages.append), 5) == 0
        assert resume_stages == [
            "open_restore_journal",
            "restore_original_route",
            "verify_vendor_connection",
            "restore_original_current",
        ]
        assert [patch for patch, _, _ in peer.writes] == [{"AmpLmt": "6"}, route, {"AmpLmt": "32"}]
        assert peer.runtime_user == 320 and journal_facts(rig.prepared.config.journal) == (records, "complete")
