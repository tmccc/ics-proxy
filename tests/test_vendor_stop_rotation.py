"""A queued vendor Stop keeps the exact reply to the earlier Stop while the journal rotates."""

import asyncio

import pytest

from ics_proxy_core.engine.gateway import decode
from ics_proxy_core.frames import build_call, build_call_result
from tests.charge_rig import positive_flow, ready
from tests.managed_rig import healthy
from tests.runtime_rig import until
from tests.test_managed_boot_supersession import delay_reply
from tests.test_managed_normal_start import natural_stop
from tests.tls_support import tls as tls


# Intent: rotate independent confirmed writes while a queued Stop still needs a definite earlier reply.
# Why: the physical closure can make the earlier Stop eligible for eviction before the queued caller resumes.
@pytest.mark.parametrize("reply", ["Accepted", "Rejected"])
async def test_queued_vendor_stop_keeps_reply_through_rotation(tmp_path, tls, unused_tcp_port, monkeypatch, reply):
    async with ready(tmp_path, tls, unused_tcp_port, monkeypatch) as rig:
        controller, journal, gateway, bench = rig.controller, rig.session.journal, rig.session.gateway, rig.rig.bench
        peer = gateway.charger
        local = await positive_flow(rig, "daily")
        await healthy(controller)
        vendor = journal.vendor_id(local)
        assert vendor is not None
        monkeypatch.setitem(gateway.policy.extensions, ("Fixture", "IndependentWrite"), "write")

        # Use a real independent mutation so the normal journal rotation chooses the retained records.
        async def independent_write(request):
            answer = await bench.call(
                rig.charger,
                request,
                "DataTransfer",
                {"vendorId": "Fixture", "messageId": "IndependentWrite", "data": request},
            )
            assert answer[:2] == [3, request]
            row = journal.db.execute(
                "SELECT token FROM mutations WHERE action='DataTransfer' AND origin='charger' AND state='confirmed' "
                "ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            assert row is not None
            return row["token"]

        older_write = await independent_write("older-write")
        monkeypatch.setattr(journal, "capacity", 2)
        monkeypatch.setattr(peer, "timeout", 0.25)
        send, held, entered = delay_reply(monkeypatch, rig.rig, rig.charger, "RemoteStopTransaction")
        payload = {"transactionId": vendor}
        answer = await bench.cloud_call("earlier-stop", "RemoteStopTransaction", payload)
        assert answer[:3] == [4, "earlier-stop", "InternalError"]
        (earlier,) = journal.outcomes.vendor_stop_dispatches(local)
        assert entered.is_set()
        wire = decode(held[0])[1]
        assert wire in peer.reply_windows()

        async with peer.gate.enter(0):
            follower = asyncio.create_task(bench.cloud_call("queued-stop", "RemoteStopTransaction", payload))
            await until(lambda: len(peer.gate._waiting) == 1)
            # Cancelling another admitted follower must leave the first follower's predecessor available.
            active_handlers = set(gateway._tasks)
            await gateway.receive("vendor", build_call("cancelled-stop", "RemoteStopTransaction", payload))
            (cancelled,) = gateway._tasks - active_handlers
            await until(lambda: len(peer.gate._waiting) == 2)
            cancelled.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancelled
            await natural_stop(rig, local)
            assert journal.outcomes.session(local).closed
            await send(build_call_result(wire, {"status": reply}))
            await until(lambda: wire not in peer.reply_windows())
            assert journal.outcomes.vendor(earlier).state == "confirmed"
            assert journal.outcomes.status(earlier) == reply
            replacement = await independent_write("first-write-with-follower-queued")
            assert journal.db.execute("SELECT 1 FROM mutations WHERE token=?", (older_write,)).fetchone() is None
            later_write = await independent_write("second-write-with-follower-queued")
            assert journal.db.execute("SELECT 1 FROM mutations WHERE token=?", (replacement,)).fetchone() is None
            assert journal.outcomes.status(earlier) == reply

        assert await follower == [3, "queued-stop", {"status": reply}]
        newest_write = await independent_write("write-after-follower")
        assert journal.outcomes.vendor(earlier) is None
        assert {row[0] for row in journal.db.execute("SELECT token FROM mutations")} == {later_write, newest_write}
        assert [call[3] for call in bench.device_calls if call[2] == "RemoteStopTransaction"] == [
            {"transactionId": local}
        ]
