# Modified for the standalone iCS proxy repository.
"""Out-of-order real native reads retain the newest source; current failures still remove freshness."""

import asyncio
import copy
import json
import sqlite3
import uuid
from datetime import UTC, datetime

import pytest

from ics_proxy_core.constants import HUB_PROFILE_ID
from ics_proxy_core.engine.native_bounded_handover import AuditSupersededError
from ics_proxy_core.engine.native_faults import NativeContentError
from ics_proxy_core.engine.native_http import NativeTransportUnavailableError
from ics_proxy_core.engine.ordinary_recovery import SOURCE
from tests import native_schedule_support as schedule_bench
from tests.charge_rig import ready
from tests.managed_rig import healthy, starts
from tests.native_schedule_support import ZONE, SchedulePeer, timer_bench
from tests.prototype_rig import START
from tests.tls_support import tls as tls


async def test_newer_cancelled_audit_fences_an_older_complete_sample(tmp_path, tls, unused_tcp_port, monkeypatch):
    async with ready(tmp_path, tls, unused_tcp_port, monkeypatch) as b:
        c, native = b.controller, b.controller.native
        assert c.current_native_sample() is not None
        observe = native.observe
        entered = (asyncio.Event(), asyncio.Event())
        release_old = asyncio.Event()
        release_new = asyncio.Event()
        gates = iter(zip(entered, (release_old, release_new), strict=True))

        async def hold_complete_observation():
            waiting, release = next(gates)
            sample = await observe()
            waiting.set()
            await release.wait()
            return sample

        with monkeypatch.context() as patch:
            patch.setattr(native, "observe", hold_complete_observation)
            older = asyncio.create_task(native.read())
            newer = None
            try:
                await asyncio.wait_for(entered[0].wait(), 3)
                newer = asyncio.create_task(native.read())
                await asyncio.wait_for(entered[1].wait(), 3)
                sequence = native.read_sequence
                newer.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await newer
                assert native.last_resolved_read == sequence and native.last_verified_at == 0
                assert c.current_native_sample() is None
                release_old.set()
                with pytest.raises(AuditSupersededError):
                    await older
                assert native.last_resolved_read == sequence and native.last_verified_at == 0
                assert c.current_native_sample() is None
            finally:
                release_old.set()
                if newer is not None:
                    newer.cancel()
                    await asyncio.gather(newer, return_exceptions=True)
                await asyncio.gather(older, return_exceptions=True)

        assert (await healthy(c))["owner_control_enabled"]
        assert c.current_native_sample() is not None


# Intent: send a genuine daily Start while a newer audit completes or a new owner Pause commits.
# Why: an ordinary poll may require fresh proof, whereas a change of authority must refuse the old report.
@pytest.mark.parametrize("concurrent", ["poll", "inventory_refresh", "pause"])
async def test_reported_start_rechecks_a_superseded_profile_read(
    tmp_path, tls, unused_tcp_port, monkeypatch, concurrent
):
    async with timer_bench(tmp_path, tls, unused_tcp_port) as b:
        native = b.c.native
        if concurrent == "inventory_refresh":
            b.c.command(uuid.uuid4().hex, "pause", b.c.requests.intent().revision)
            assert (await healthy(b.c))["local_hold_verified"]
            returning, revision = uuid.uuid4().hex, b.c.requests.intent().revision
            b.c.command(returning, "app_control", revision)
            await b.c.return_requested(returning, revision)
            assert b.c.requests.receipt(returning)["state"] == "completed"
            # Model the release receipt before its optional observation digest has been filled by a later poll.
            b.session.journal.db.execute(
                "UPDATE trial_resources SET observed_digest=NULL WHERE key=?", (f"profile:{HUB_PROFILE_ID}",)
            )
            earlier_sample = await native.observe()
        observe = native.observe_profiles
        observed, release = asyncio.Event(), asyncio.Event()
        observations = []

        # Delay the first actual native profile response at its delivery boundary and allow later reads through.
        async def hold_first_profile_observation():
            profiles = await observe()
            observations.append(profiles)
            if len(observations) == 1:
                observed.set()
                await release.wait()
            return profiles

        monkeypatch.setattr(native, "observe_profiles", hold_first_profile_observation)
        body = START | {
            "idTag": b.device.plans[9]["idTag"],
            "timestamp": datetime.fromtimestamp(b.clock.now, UTC).isoformat(),
        }
        start = asyncio.create_task(b.rig.bench.call(b.sock, uuid.uuid4().hex, "StartTransaction", body))
        try:
            await asyncio.wait_for(observed.wait(), 3)
            before, sequence = native.authority(), native.read_sequence
            if concurrent == "pause":
                b.c.command(uuid.uuid4().hex, "pause", b.c.requests.intent().revision)
                await b.c.pause_task
                assert native.authority() != before
            elif concurrent == "inventory_refresh":
                # The supervision turn can record this earlier sample after the report has begun.
                b.session.gateway.decide(
                    lambda: b.session.book.owned_present(earlier_sample.physical, record_unchanged=False)
                )
                assert native.authority() != before and native.last_resolved_read < sequence
            else:
                await native.read()
                assert native.authority() == before
                assert native.last_resolved_read > sequence
        finally:
            release.set()
        answer = await start
        if concurrent == "pause":
            assert answer[0] == 4, answer
            assert len(observations) == 1
            assert b.session.journal.active() is None and b.device.effective_limit() == 0
            return
        assert answer[0] == 3 and answer[2]["idTagInfo"]["status"] == "Accepted", answer
        local = answer[2]["transactionId"]
        assert len(observations) == (2 if concurrent == "poll" else 1)
        assert b.session.journal.active() == local and b.device.effective_limit() == 6
        await b.close_transaction(local)


# Intent: record a real delayed cloud Start at zero while the original Pause settles or ordinary polls advance.
# Why: the original zero hold admits its exact report, while a new Pause or newer failed poll invalidates it.
@pytest.mark.parametrize("concurrent", ["pause_settlement", "repeated_polls", "new_pause", "failed_poll"])
async def test_held_cloud_start_survives_fresh_evidence_under_its_pause(
    tmp_path, tls, unused_tcp_port, monkeypatch, concurrent
):
    peer_type = OrderedNativePeer if concurrent == "failed_poll" else SchedulePeer
    async with timer_bench(tmp_path, tls, unused_tcp_port, peer_type=peer_type) as b:
        c, journal = b.c, b.session.journal
        native = c.native
        await b.acknowledge_older_owner_start()
        b.clock.now = int(datetime(2026, 11, 11, 6, tzinfo=ZONE).timestamp())
        await c.tick()
        grant = journal.get("vendor_start_grant")
        before_starts = list(starts(b.rig))
        pause = uuid.uuid4().hex
        closed_sample, finish_closed_sample = asyncio.Event(), asyncio.Event()
        read, observe = native.read, native.observe_profiles
        poll_orders = []

        # The original Pause observes closed zero before the station presents the delayed Start.
        async def hold_closed_observation(*args, **kwargs):
            sample = await read(*args, **kwargs)
            if asyncio.current_task() is c.pause_task:
                assert not sample.physical.transaction_open
                closed_sample.set()
                await finish_closed_sample.wait()
            return sample

        with monkeypatch.context() as patch:
            if concurrent == "pause_settlement":
                patch.setattr(native, "read", hold_closed_observation)
            c.command(pause, "pause", c.requests.intent().revision)
            try:
                if concurrent == "pause_settlement":
                    await asyncio.wait_for(closed_sample.wait(), 3)
                else:
                    await c.pause_task
                control = journal.get("control")
                assert control["uncertain"] is (concurrent == "pause_settlement")
                assert b.session.gateway.controls.ready and b.device.effective_limit() == 0
                assert journal.active() is None and grant.get("local") is None

                # Let real observations or a genuine new owner decision finish after a profile sample was fetched.
                async def interleave_profile_observation():
                    profiles = await observe()
                    if concurrent == "pause_settlement" and not finish_closed_sample.is_set():
                        finish_closed_sample.set()
                        await c.pause_task
                    elif concurrent == "new_pause" and journal.get("control")["ticket"] == control["ticket"]:
                        c.command(uuid.uuid4().hex, "pause", c.requests.intent().revision)
                        await c.pause_task
                    elif concurrent in ("repeated_polls", "failed_poll") and len(poll_orders) < 2:
                        sequence = native.read_sequence
                        if concurrent == "failed_poll":
                            b.rig.peer.unavailable = True
                            try:
                                with pytest.raises(NativeTransportUnavailableError):
                                    await native.read()
                            finally:
                                b.rig.peer.unavailable = False
                        else:
                            await asyncio.create_task(c.tick())
                        poll_orders.append((sequence, native.last_resolved_read, native.last_verified_at))
                    return profiles

                patch.setattr(native, "observe_profiles", interleave_profile_observation)
                b.device.active = 987654
                b.rig.peer.plan_session = (987654, 987655)
                body = START | {
                    "idTag": "CLOUD_OWNER",
                    "timestamp": datetime.fromtimestamp(b.clock.now, UTC).isoformat(),
                }
                reply = await b.rig.bench.call(b.sock, "late-cloud-owner", "StartTransaction", body)
            finally:
                finish_closed_sample.set()

        assert b.device.effective_limit() == 0 and starts(b.rig) == before_starts
        latest = journal.get("control")
        if concurrent in ("new_pause", "failed_poll"):
            if concurrent == "new_pause":
                assert latest["ticket"] != control["ticket"]
            else:
                assert latest == control
                [(sequence, resolved, verified)] = poll_orders
                assert sequence < resolved and verified == 0
            assert reply[0] == 4, reply
            assert journal.active() is None and journal.get("vendor_start_grant") == grant and not b.stops()
            return
        if concurrent == "pause_settlement":
            assert latest == {**control, "uncertain": False}
        else:
            assert poll_orders and all(
                sequence < resolved and verified > 0 for sequence, resolved, verified in poll_orders
            )
        assert reply[0] == 3 and reply[2]["idTagInfo"]["status"] == "Accepted", (reply, poll_orders)
        local = reply[2]["transactionId"]
        b.rig.peer.plan_session = (local, 987655)
        assert journal.active() == local
        async with asyncio.timeout(4):
            while not b.stops():
                await c.tick()
                await asyncio.sleep(0.02)
        assert b.stops() == [local] and c.requests.receipt(pause)["state"] == "accepted"
        await b.close_transaction(local)
        assert (await healthy(c))["local_hold_verified"]
        assert c.requests.receipt(pause)["state"] == "completed"


class OrderedNativePeer(SchedulePeer):
    """Only the independent HTTP peer holds or replaces its selected final native response."""

    # Arm after commissioning so every preflight, profile and genuine TLS lifecycle remains ordinary.
    def __init__(self, bench):
        super().__init__(bench)
        self.mode = None
        self.configuration_reads = 0
        self.entered, self.release, self.finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self.held_request = None
        self.sent_outcomes = []
        self.unavailable = False
        self.unavailable_requests = []

    # Read exact incoming bytes before holding the second getConfig or returning an actual HTTP503.
    async def handle(self, reader, writer):
        if self.mode is None and not self.unavailable:
            await super().handle(reader, writer)
            return
        header = await reader.readuntil(b"\r\n\r\n")
        fields = {
            line.split(b":", 1)[0].lower(): line.split(b":", 1)[1].strip()
            for line in header.split(b"\r\n")[1:]
            if b":" in line
        }
        raw = await reader.readexactly(int(fields[b"content-length"]))
        request = json.loads(raw)
        selected = self.unavailable
        if self.mode is not None and request["action"] == "getConfig":
            self.configuration_reads += 1
            selected = selected or self.configuration_reads == 2
        if not selected:
            replay = asyncio.StreamReader()
            replay.feed_data(header + raw)
            replay.feed_eof()
            await super().handle(replay, writer)
            return
        self.requests.append(request["action"])
        if self.unavailable:
            self.unavailable_requests.append(request)
            response = b"HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n"
        else:
            mode, self.mode = self.mode, None
            self.held_request = request
            body = (
                b'{"code":200,"code":200,"data":{}}'
                if mode == "strict-content"
                else json.dumps({"code": 200, "data": json.dumps(copy.deepcopy(self.configuration))}).encode()
            )
            response = (
                b"HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n"
                if mode == "transport503"
                else b"HTTP/1.0 200 OK\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
            )
            self.entered.set()
            await self.release.wait()
            self.sent_outcomes.append(mode)
        try:
            writer.write(response)
            await writer.drain()
        except ConnectionError:
            pass  # An actually cancelled old reader may already have retired this one fake-peer socket.
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            self.finished.set()


# Preserve the result of the real old task without accidentally cancelling its newer peer or swallowing success.
async def old_result(task, mode, *, superseded):
    result = (await asyncio.gather(task, return_exceptions=True))[0]
    if mode == "valid-other-source" and not superseded:
        assert not isinstance(result, BaseException)
        return result
    expected = (
        asyncio.CancelledError
        if mode == "cancelled"
        else AuditSupersededError
        if superseded
        else NativeContentError
        if mode == "strict-content"
        else NativeTransportUnavailableError
    )
    assert isinstance(result, expected), result
    return result


# After the witnessed order, finish this genuine daily and prove the next original occurrence remains usable.
async def later_daily(b, local, selected):
    b.rig.peer.unavailable = False
    b.rig.peer.plan_session = (local, 9)
    assert (await healthy(b.c))["station_ceiling_a"] == selected
    await b.close_transaction(local)
    await healthy(b.c)
    assert set(b.device.plans) == {9}
    following = await b.advance_idle_to(int(datetime(2026, 11, 12, tzinfo=ZONE).timestamp()))
    assert following not in (None, local)
    assert (await healthy(b.c))["current_a"] == selected
    assert not b.stops() and not starts(b.rig)


# Intent: obsolete read outcomes cannot erase a newer fresh daily source; current failures still require zero.
# Why: handling an old exception before checking sequence can create an unnecessary hold or borrowed Stop at cutoff.
@pytest.mark.parametrize("selected", [6, 32])
@pytest.mark.parametrize("newer_read", [False, True], ids=["current-failure-control", "obsolete-finishes-last"])
@pytest.mark.parametrize("mode", ["valid-other-source", "strict-content", "transport503", "cancelled"])
async def test_native_read_outcome_belongs_to_its_sequence(
    tmp_path, tls, unused_tcp_port, monkeypatch, selected, newer_read, mode
):
    monkeypatch.setattr(schedule_bench, "SchedulePeer", OrderedNativePeer)
    async with timer_bench(tmp_path, tls, unused_tcp_port) as b:
        if selected != 6:
            b.c.command(uuid.uuid4().hex, "set_current", b.c.requests.intent().revision, current_a=selected)
            await b.c.current.settle()
            await healthy(b.c)
        b.clock.now = b.start
        local = await b.start_plan(9, demand=True)
        await healthy(b.c)
        c, peer, j = b.c, b.rig.peer, b.session.journal
        intent = c.requests.intent()
        source = j.get(SOURCE)
        assert source.get("local") == local and source.get("source", {}).get("plan") == 9
        assert c.native.last_verified_at > 0 and b.device.current == selected
        peer.mode = mode
        if mode == "valid-other-source":
            peer.plan_session = (local, 987654)
        task = asyncio.create_task(c.native.read())
        try:
            await asyncio.wait_for(peer.entered.wait(), 3)
            sequence = c.native.read_sequence
            assert not task.done()
            if newer_read:
                peer.plan_session = (local, 9)
                newest = await c.native.read()
                newest_source = copy.deepcopy(j.get(SOURCE))
                verified_at, resolved = c.native.last_verified_at, c.native.last_resolved_read
                assert resolved > sequence and verified_at == newest.physical.observed_at > 0
                assert newest_source.get("local") == local and newest_source.get("source", {}).get("plan") == 9
            if mode == "cancelled":
                task.cancel()
            peer.release.set()
            result = await old_result(task, mode, superseded=newer_read)
            await asyncio.wait_for(peer.finished.wait(), 3)
            witness = {
                "mode": mode,
                "newer_read": newer_read,
                "selected_a": selected,
                "old_sequence": sequence,
                "last_resolved_read": c.native.last_resolved_read,
                "last_verified_at": c.native.last_verified_at,
                "result_type": type(result).__name__,
                "current_source": j.get(SOURCE),
                "physical_limit_a": b.device.effective_limit(),
                "physical_current_a": b.device.current,
                "remote_stops": b.stops(),
            }
            (tmp_path / "read-order-witness.json").write_text(json.dumps(witness, indent=2) + "\n")
            assert peer.held_request == {"action": "getConfig"}
            assert peer.sent_outcomes == [mode]
            if newer_read:
                assert c.native.last_verified_at == verified_at, (
                    "obsolete read erased the newer verified native timestamp"
                )
                assert c.native.last_resolved_read == resolved and j.get(SOURCE) == newest_source
                assert j.get(SOURCE).get("source")
            elif mode == "valid-other-source":
                assert result.session_plan_id == 987654 and c.native.last_verified_at > 0
                assert j.get(SOURCE).get("source") is None
            else:
                assert c.native.last_verified_at == 0, "a genuine current failure must invalidate earlier freshness"
            assert c.requests.intent() == intent and j.active() == local and not b.stops()
            await c.ordinary.hold("native_observation_unavailable")
            peer.unavailable = not newer_read
            b.clock.now = int(datetime(2026, 11, 11, 6, tzinfo=ZONE).timestamp())
            await c.tick()
            assert not b.stops() and b.device.effective_limit() == (selected if newer_read else 0)
            await later_daily(b, local, selected)
        finally:
            peer.release.set()
            await asyncio.gather(task, return_exceptions=True)
            peer.unavailable = False


# Intent: a genuine current read's real SQL authority failure still invalidates its earlier positive source proof.
# Why: preserving obsolete completion must not suppress a new unknown outcome or authorize cutoff continuation.
@pytest.mark.parametrize("selected", [6, 32])
async def test_current_native_authority_sql_refusal_removes_freshness(
    tmp_path, tls, unused_tcp_port, monkeypatch, selected
):
    monkeypatch.setattr(schedule_bench, "SchedulePeer", OrderedNativePeer)
    async with timer_bench(tmp_path, tls, unused_tcp_port) as b:
        if selected != 6:
            b.c.command(uuid.uuid4().hex, "set_current", b.c.requests.intent().revision, current_a=selected)
            await b.c.current.settle()
            await healthy(b.c)
        b.clock.now = b.start
        local = await b.start_plan(9, demand=True)
        await healthy(b.c)
        c, peer, j = b.c, b.rig.peer, b.session.journal
        source, intent = copy.deepcopy(j.get(SOURCE)), c.requests.intent()
        assert source.get("local") == local and c.native.last_verified_at > 0
        attempts = []

        # Deny only the first actual authority SELECT after the already-started full native HTTP observation.
        def deny_authority(action, first, second, database, trigger):
            if action == sqlite3.SQLITE_READ and first == "trial_resources" and not attempts:
                attempts.append((first, second))
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        peer.mode = "valid-other-source"
        task = asyncio.create_task(c.native.read())
        try:
            await asyncio.wait_for(peer.entered.wait(), 3)
            j.db.set_authorizer(deny_authority)
            peer.release.set()
            try:
                with pytest.raises(sqlite3.DatabaseError):
                    await task
            finally:
                j.db.set_authorizer(None)
            assert attempts and not j.db.in_transaction
            assert c.native.last_verified_at == 0
            assert j.get(SOURCE) == source and c.requests.intent() == intent
            assert j.active() == local and not b.stops()
            await c.ordinary.hold("native_observation_unavailable")
            peer.unavailable = True
            b.clock.now = int(datetime(2026, 11, 11, 6, tzinfo=ZONE).timestamp())
            await c.tick()
            assert b.device.effective_limit() == 0 and not b.stops()
            await later_daily(b, local, selected)
        finally:
            j.db.set_authorizer(None)
            peer.release.set()
            peer.unavailable = False
            await asyncio.gather(task, return_exceptions=True)
