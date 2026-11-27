"""An independent connector limit can lag an already applied Pause."""

import uuid

import pytest

from ics_proxy_core.engine.native_bounded import NativeReadinessError
from tests.charge_rig import positive_flow, ready, report_actual_stop
from tests.managed_rig import healthy
from tests.tls_support import tls as tls


async def test_pause_awaits_connector_limit_after_station_zero(tmp_path, tls, unused_tcp_port, monkeypatch):
    async with ready(tmp_path, tls, unused_tcp_port, monkeypatch) as b:
        c, j, bench, peer = b.controller, b.session.journal, b.rig.bench, b.rig.peer
        local = await positive_flow(b, "daily")
        assert (await healthy(c))["current_a"] == 6
        pause, revision = uuid.uuid4().hex, c.requests.intent().revision
        native = peer.native

        def lagging_limit(key):
            value = native(key)
            if key == "CONNECTOR" and bench.firmware.effective_limit() == 0:
                value[0]["outLimit"] = 60
            return value

        with monkeypatch.context() as patch:
            patch.setattr(peer, "native", lagging_limit)
            c.command(pause, "pause", revision)
            await c.pause_task
            assert j.active() == local and bench.firmware.effective_limit() == bench.firmware.current == 0
            with pytest.raises(NativeReadinessError):
                await c.settled_native()
            await report_actual_stop(b, local)
            with monkeypatch.context() as changed:
                changed.setitem(peer.configuration, "untouched", "different commissioned configuration")
                status = await c.tick()
                assert status["fault"] == "native_authority_or_preservation_failed" and c.failures == 1
            for _ in range(4):
                status = await c.tick()
                assert status["state"] == "reconciling" and status.get("fault") is None
                assert not status["owner_control_enabled"] and not status["local_hold_verified"]
                assert not j.fault.latched() and c.failures == 1
                assert c.requests.receipt(pause)["state"] == "accepted"

        status = await healthy(c)
        assert status["local_hold_verified"] and status["station_ceiling_a"] == 0
        assert c.requests.receipt(pause)["state"] == "completed" and c.failures == 0
