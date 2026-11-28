"""Exercise an exact charger-side timer before lifting a real loopback zero hold."""

import asyncio
import json
import uuid
from datetime import UTC, datetime

import pytest

from ics_proxy_core.engine.managed_intent import LOCAL_SCHEDULE_OCCURRENCE
from tests.managed_rig import starts
from tests.prototype_rig import START
from tests.runtime_rig import until
from tests.test_managed_local_schedule import MIDNIGHT, connection, overnight, status
from tests.test_managed_offline_owner import OWNER_TAG
from tests.tls_support import tls as tls

CUTOFF = MIDNIGHT + 6 * 3600


class ChargerTimer:
    """The simulated charger keeps the timer in its own state."""

    # Keep the ordinary firmware responder and install a timer only after its command arrives.
    def __init__(self, firmware, reply):
        self.firmware, self.reply = firmware, reply
        self.answer = firmware.answer
        self.armed = None

    # Require the exact active transaction and observe zero current when the timer is armed.
    def respond(self, action, payload):
        if action != "DataTransfer" or payload["messageId"] != "delayStop":
            return self.answer(action, payload)
        assert payload["vendorId"] == "IoCharger"
        data = json.loads(payload["data"])
        assert data["transactionId"] == self.firmware.active
        assert self.firmware.effective_limit() == self.firmware.current == 0
        if self.reply == "rejected":
            return {"status": "Rejected", "data": ""}
        if self.reply == "malformed":
            return {"status": "Accepted", "data": "unexpected"}
        self.armed = data["transactionId"], datetime.fromisoformat(data["stopTime"]).timestamp()
        return {"status": "Accepted", "data": ""}

    # Fire the native timer without involving the proxy or affecting a replacement transaction.
    def expire(self, now):
        assert self.armed is not None
        local, cutoff = self.armed
        if self.firmware.active == local and now >= cutoff:
            self.firmware.active = None
            self.firmware.requested_current = 0


# Poll the controller until a charger-level Stop identifies the target transaction.
async def stop_targets(test):
    bench = test.rig.bench
    async with asyncio.timeout(6):
        while not bench.remote_stop.is_set():
            await test.controller.tick()
            await asyncio.sleep(0.02)
    return {frame[3]["transactionId"] for frame in bench.device_calls if frame[2] == "RemoteStopTransaction"}


# Intent: arm the exact session cutoff before releasing the zero hold, including when replies or authority change.
# Why: verified current must depend on the accepted charger timer, which can expire while the proxy is offline.
@pytest.mark.parametrize(
    "outcome", ["armed", "rejected", "malformed", "lost-ack", "write-refused", "pause", "replacement", "clock-shift"]
)
async def test_charger_local_cutoff_requires_armed_zero_hold(tmp_path, tls, unused_tcp_port, monkeypatch, outcome):
    async with overnight(tmp_path, tls, unused_tcp_port, monkeypatch, charger_cutoff=True) as (rig, clock):
        bench, native = rig.bench, rig.peer
        firmware = bench.firmware
        timer = ChargerTimer(firmware, outcome)
        monkeypatch.setattr(firmware, "answer", timer.respond)
        timer_replied, release_reply = asyncio.Event(), asyncio.Event()

        # Pause the device reply at the timer boundary so the test can inspect or change authority.
        async def hold_reply(action, payload):
            if action == "DataTransfer" and payload["messageId"] == "delayStop":
                timer_replied.set()
                await release_reply.wait()

        bench.before_reply = hold_reply
        try:
            async with connection(rig, "local-cutoff") as test:
                c, j = test.controller, test.session.journal
                await status(test, lambda value: value.get("local_hold_verified") is True)
                sent_limits = len(bench.received_limits)
                clock.now = MIDNIGHT
                await status(test, lambda _: bool(starts(rig)))
                assert firmware.effective_limit() == 0
                assert not any(bench.received_limits[sent_limits:])

                report = START | {"idTag": OWNER_TAG, "timestamp": datetime.fromtimestamp(clock.now, UTC).isoformat()}
                authorised = await bench.call(test.charger, uuid.uuid4().hex, "Authorize", {"idTag": OWNER_TAG})
                assert authorised[2]["idTagInfo"]["status"] == "Accepted"
                opened = await bench.call(test.charger, uuid.uuid4().hex, "StartTransaction", report)
                assert opened[0] == 3 and opened[2]["idTagInfo"]["status"] == "Accepted", opened
                local = opened[2]["transactionId"]
                native.paused, native.permission_enabled = False, True
                bench.cp, bench.connection = "C", "Charging"
                firmware.requested_current = 32

                if outcome == "write-refused":
                    j.db.execute("""
                        CREATE TRIGGER refuse_timer_proof BEFORE INSERT ON meta
                        WHEN NEW.key='managed_owner_schedule_occurrence'
                        AND json_extract(NEW.value,'$.charger_cutoff_armed') IS NOT NULL
                        BEGIN SELECT RAISE(ABORT, 'synthetic journal refusal'); END
                    """)
                await asyncio.wait_for(timer_replied.wait(), 5)
                assert firmware.active == local and firmware.current == 0
                assert not any(bench.received_limits[sent_limits:])
                assert "charger_cutoff_armed" not in j.get(LOCAL_SCHEDULE_OCCURRENCE)
                if outcome == "lost-ack":
                    peer = test.session.gateway.charger
                    timer_wire = next(
                        frame[1]
                        for frame in bench.device_calls
                        if frame[2] == "DataTransfer" and frame[3]["messageId"] == "delayStop"
                    )
                    await until(lambda: timer_wire not in peer._pending, timeout=8)
                elif outcome == "pause":
                    c.command(uuid.uuid4().hex, "pause", c.requests.intent().revision)
                elif outcome in ("armed", "clock-shift"):
                    # Advance elapsed time with no wall change while the exact reply is in flight.
                    clock.elapsed += 2 if outcome == "clock-shift" else 0.001
                release_reply.set()
                await asyncio.wait_for(c.charging.settle(), 8)

                occurrence = j.get(LOCAL_SCHEDULE_OCCURRENCE)
                if outcome not in ("armed", "replacement"):
                    assert firmware.effective_limit() == firmware.current == 0
                    assert not any(bench.received_limits[sent_limits:])
                    assert "charger_cutoff_armed" not in occurrence
                    assert c.requests.receipt(occurrence["request"])["state"] != "completed"
                    assert await stop_targets(test) == {local}
                    return

                assert occurrence["charger_cutoff_armed"]["local"] == local
                assert timer.armed == (local, CUTOFF - 1)
                assert firmware.current > 0
                await status(test, lambda value: value.get("power_w", 0) > 0)

            # The simulated timer can expire without any proxy or charger connection.
            assert firmware.active == local and firmware.current > 0
            if outcome == "replacement":
                before = len(bench.received_limits)
                async with connection(rig, "replacement-before-cutoff") as test:
                    assert await stop_targets(test) == {local}
                    assert firmware.current == 0
                    assert not any(bench.received_limits[before:])
                return
            timer.expire(CUTOFF - 2)
            assert firmware.current > 0
            timer.expire(CUTOFF - 1)
            assert firmware.active is None and firmware.current == 0
            assert not bench.remote_stop.is_set()
        finally:
            release_reply.set()
