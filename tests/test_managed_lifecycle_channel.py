"""Gate independent real loopback peers across temporary recovery zero confirmation."""

import asyncio
import uuid

import pytest

from ics_proxy_core.engine.gateway import decode
from ics_proxy_core.engine.managed_view import refresh_for_read
from ics_proxy_core.frames import build_call
from tests.current_rig import locally_ready
from tests.managed_rig import healthy, starts
from tests.native_schedule_support import timer_bench
from tests.prototype_rig import BOOT
from tests.runtime_rig import until
from tests.tls_support import tls as tls


# Intent: an intervening Boot change revokes the old registration even if the original body returns before polling.
# Why: comparing only the current body can erase a real A-to-B-to-A introduction revocation.
async def test_intervening_boot_change_revokes_cloud(tmp_path, tls, unused_tcp_port, monkeypatch):
    async with timer_bench(tmp_path, tls, unused_tcp_port) as b:
        gateway, runtime = b.session.gateway, b.session.runtime
        epoch = gateway._vendor_epoch
        assert runtime.status.vendor_connected and gateway.vendor_registration == "Accepted"
        (presence,) = (
            task for task in runtime._session_tasks if task.get_coro().__qualname__.endswith(".charger_presence")
        )
        parked, resume = asyncio.Event(), asyncio.Event()
        sleep = asyncio.sleep

        # Hold just one real presence interval while both charger reports cross the independent receiver.
        async def hold_presence_interval(delay, result=None):
            if asyncio.current_task() is presence and not parked.is_set():
                parked.set()
                await resume.wait()
                return result
            return await sleep(delay, result)

        with monkeypatch.context() as timing:
            timing.setattr(asyncio, "sleep", hold_presence_interval)
            try:
                await asyncio.wait_for(parked.wait(), 3)
                intervening = BOOT | {"chargePointModel": "Unapproved replacement"}
                changed = await b.rig.bench.call(b.sock, "intervening-boot", "BootNotification", intervening)
                restored = await b.rig.bench.call(b.sock, "restored-boot", "BootNotification", BOOT)
                assert changed[0] == restored[0] == 3 and b.session.journal.get("boot") == BOOT
                assert not presence.done() and gateway._vendor_epoch == epoch
                before = starts(b.rig)
                denied = await b.rig.bench.cloud_call(
                    "start-on-old-boot", "RemoteStartTransaction", {"connectorId": 1, "idTag": "CLOUD_OWNER"}
                )
                assert denied[0] == 4 and starts(b.rig) == before
            finally:
                resume.set()
        await until(lambda: not runtime.status.vendor_connected, timeout=1)
        assert gateway._vendor_epoch != epoch and gateway.vendor_registration != "Accepted"
        assert b.session.journal.get("trial_phase") == "recovering" and not gateway.controls.ready


# Intent: a confirmed zero on a replacement charger socket awaits that socket's own registered cloud channel.
# Why: the old channel can still receive correlated replies while the runtime finishes retiring it.
async def test_replacement_zero_waits_for_fresh_cloud_registration(tmp_path, tls, unused_tcp_port, monkeypatch):
    async with timer_bench(tmp_path, tls, unused_tcp_port) as b:
        gateway, runtime = b.session.gateway, b.session.runtime
        published = await healthy(b.c)
        old_charger, old_vendor = gateway._charger_epoch, gateway._vendor_epoch
        draining, retire = asyncio.Event(), asyncio.Event()
        finish = gateway.finish_sent_start

        # Keep the actual old connection open at the point where it can still collect late replies.
        async def drain():
            draining.set()
            await retire.wait()
            await finish()

        monkeypatch.setattr(gateway, "finish_sent_start", drain)
        async with b.rig.charger(b.session) as charger:
            try:
                reply = await b.rig.bench.call(charger, "replacement-boot", "BootNotification", BOOT)
                assert reply[0] == 3
                await asyncio.wait_for(draining.wait(), 3)
                await until(lambda: gateway.controls.ready and not runtime.introduction_pending)
                assert gateway._charger_epoch != old_charger and gateway._vendor_epoch == old_vendor
                assert runtime.status.vendor_connected and gateway.vendor_registration == "Accepted"
                assert refresh_for_read(b.c, published)["cloud_connected"] is False
                mark = len(b.rig.bench.device_calls)
                status = await b.c.tick()
                assert status["state"] == "waiting_for_cloud" and not status["cloud_connected"]
                assert b.rig.bench.firmware.effective_limit() == 0 and not b.rig.bench.device_calls[mark:]
            finally:
                retire.set()
            status = await healthy(b.c)
            assert gateway._vendor_epoch != old_vendor and runtime.vendor_registered
            assert status["owner_control_enabled"] and status["station_ceiling_a"] == 6
            assert b.rig.bench.firmware.effective_limit() == 6 and not starts(b.rig)


# Intent: a normal Start can use its bounded local approval while the previous vendor connection retires.
# Why: a live return path for old replies must not prevent the new charger from using local approval.
async def test_nested_local_authorize_while_old_cloud_drains(tmp_path, tls, unused_tcp_port, monkeypatch):
    async with timer_bench(tmp_path, tls, unused_tcp_port) as b:
        c, g, runtime = b.c, b.session.gateway, b.session.runtime
        old_vendor = g._vendor_epoch
        c.command(uuid.uuid4().hex, "pause", c.requests.intent().revision)
        assert (await healthy(c))["local_hold_verified"]
        draining, retire = asyncio.Event(), asyncio.Event()
        finish = g.finish_sent_start

        # Hold the retirement wait while the replacement charger sends its local authorization request.
        async def hold_retirement():
            draining.set()
            await retire.wait()
            await finish()

        monkeypatch.setattr(g, "finish_sent_start", hold_retirement)
        async with b.rig.charger(b.session) as charger:
            try:
                assert (await b.rig.bench.call(charger, "replacement-boot", "BootNotification", BOOT))[0] == 3
                await asyncio.wait_for(draining.wait(), 3)
                await until(lambda: g.controls.ready and not runtime.introduction_pending)
                status = await locally_ready(c)
                assert status["normal_start_ready"] and status["local_hold_verified"] and not status["cloud_connected"]
                assert runtime.status.vendor_connected and g.vendor_registration == "Accepted"
                assert not runtime.vendor_registered and g._vendor_epoch == old_vendor

                authorization = asyncio.get_running_loop().create_future()
                cloud_calls = len(b.rig.bench.calls)

                # Ask the proxy for the same local tag while the charger is answering the actual Start request.
                async def authorize_during_start(action, payload):
                    if action == "RemoteStartTransaction":
                        request = build_call("nested-owner-authorize", "Authorize", {"idTag": payload["idTag"]})
                        await charger.send(request)
                        async with asyncio.timeout(2):
                            authorization.set_result(decode(await charger.recv()))

                monkeypatch.setattr(b.rig.bench, "before_reply", authorize_during_start)
                c.command(uuid.uuid4().hex, "start", c.requests.intent().revision)
                await c.charging.settle()
                assert len(starts(b.rig)) == 1
                answer = await asyncio.wait_for(authorization, 3)
                assert answer[:2] == [3, "nested-owner-authorize"], answer
                assert answer[2]["idTagInfo"]["status"] == "Accepted"
                assert all(frame[0] != 2 or frame[2] != "Authorize" for frame in b.rig.bench.calls[cloud_calls:])
            finally:
                retire.set()


# Intent: changed Boot metadata revokes the accepted cloud introduction, while an identical retry retains it.
# Why: temporary zero verification may retain a session only while its actual admitted Boot binding agrees.
@pytest.mark.parametrize("changed", [False, True])
async def test_cloud_registration_remains_bound_to_boot(tmp_path, tls, unused_tcp_port, monkeypatch, changed):
    async with timer_bench(tmp_path, tls, unused_tcp_port) as b:
        gateway, runtime = b.session.gateway, b.session.runtime
        epoch = gateway._vendor_epoch
        cloud_boots = [frame for frame in b.rig.bench.calls if frame[0] == 2 and frame[2] == "BootNotification"]
        assert runtime.status.vendor_connected and gateway.vendor_registration == "Accepted"
        payload = BOOT | {"chargePointModel": "Unapproved replacement"} if changed else BOOT
        result = await b.rig.bench.call(b.sock, "later-real-boot", "BootNotification", payload)
        assert result[0] == 3
        await b.c.tick()
        if changed:
            await until(lambda: not runtime.status.vendor_connected, timeout=1)
            assert gateway._vendor_epoch != epoch and gateway.vendor_registration != "Accepted"
            assert b.session.journal.get("trial_phase") == "recovering" and not gateway.controls.ready
        else:
            await asyncio.sleep(10 * runtime.settings.pump_interval)
            assert runtime.status.vendor_connected and gateway._vendor_epoch == epoch
            assert gateway.vendor_registration == "Accepted"
        assert [f for f in b.rig.bench.calls if f[0] == 2 and f[2] == "BootNotification"] == cloud_boots
