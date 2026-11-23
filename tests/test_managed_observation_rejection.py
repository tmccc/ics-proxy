# Modified for the standalone iCS proxy repository.
"""Routine report rejection is terminal and audited while real lifecycle delivery retains its semantics."""

import json

import pytest

from ics_proxy_core.engine.managed import ManagedController
from ics_proxy_core.engine.store import Journal
from ics_proxy_core.frames import build_call_error
from tests.managed_rig import healthy, introduced, managed_bench, managed_session
from tests.prototype_rig import START
from tests.runtime_rig import until
from tests.tls_support import tls as tls
from tests.trial_peer import Bench


class ReportReplyFilter:
    """Return a correlated error only for the named report, while the independent cloud handles lifecycle normally."""

    # Track actual received frames independently from the engine and retain normal cloud allocation.
    def __init__(self, connection, action):
        self.connection, self.action = connection, action
        self.targets = set()

    # Observe requests on the real upstream TLS socket.
    def __aiter__(self):
        # All unselected requests pass through to the real fake-cloud implementation.
        async def frames():
            async for raw in self.connection:
                frame = json.loads(raw)
                if frame[0] == 2 and frame[2] == self.action:
                    self.targets.add(frame[1])
                yield raw

        return frames()

    # A correlated rejection is a terminal outcome for telemetry, without inventing successful delivery.
    async def send(self, raw):
        frame = json.loads(raw)
        if frame[0] == 3 and frame[1] in self.targets:
            raw = build_call_error(frame[1], "NotSupported", "synthetic unsupported report")
        await self.connection.send(raw)


REPORTS = [
    ("StatusNotification", {"connectorId": 1, "status": "Available", "errorCode": "NoError"}),
    (
        "MeterValues",
        {"connectorId": 1, "meterValue": [{"timestamp": START["timestamp"], "sampledValue": [{"value": "1000"}]}]},
    ),
    (
        "MeterValues",
        {
            "connectorId": 1,
            "meterValue": [
                {
                    "timestamp": START["timestamp"],
                    "sampledValue": [
                        {"value": "1000", "format": "Raw"},
                        {"value": "00AA", "format": "SignedData"},
                    ],
                }
            ],
        },
    ),
    ("FirmwareStatusNotification", {"status": "Downloaded"}),
    ("DiagnosticsStatusNotification", {"status": "Uploaded"}),
]


# Intent: rejected routine messages retire audibly and do not poison later real Start and Stop delivery.
# Why: Start/Stop share a FIFO with telemetry but have materially different acknowledgement semantics.
@pytest.mark.parametrize("action,payload", REPORTS)
async def test_rejected_report_does_not_block_lifecycle(tmp_path, tls, unused_tcp_port, monkeypatch, action, payload):
    actual = Bench.vendor

    # Modify only the cloud peer's reply transport, not gateway/RPC/journal methods.
    async def cloud(bench, connection):
        await actual(bench, ReportReplyFilter(connection, action))

    monkeypatch.setattr(Bench, "vendor", cloud)
    async with managed_bench(tmp_path, tls, unused_tcp_port) as rig:
        s = managed_session(rig)
        await s.start()
        c = ManagedController(s)
        rig.route_to_trial()
        async with rig.charger(s) as charger:
            await introduced(rig, s, charger, "report-rejection-boot")
            await healthy(c)
            assert (await rig.bench.call(charger, "report", action, payload))[0] == 3
            await until(lambda: s.journal.next_delivery() is None)
            assert any(frame[0] == 2 and frame[2:] == [action, payload] for frame in rig.bench.calls)
            audit = s.journal.db.execute("SELECT * FROM rejected_observations").fetchall()
            assert len(audit) == 1 and audit[0]["action"] == action and audit[0]["code"] == "NotSupported"
            assert not s.runtime.status.delivery_blocked and not s.journal.get("managed_control_fault")
            local = (
                await rig.bench.call(charger, "real-start", "StartTransaction", START | {"idTag": "NATIVE_TIMER"})
            )[2]["transactionId"]
            await until(lambda: s.journal.vendor_id(local) is not None)
            assert (
                await rig.bench.call(
                    charger,
                    "real-stop",
                    "StopTransaction",
                    {"transactionId": local, "meterStop": 1100, "timestamp": START["timestamp"]},
                )
            )[0] == 3
            await until(lambda: s.journal.next_delivery() is None)
            assert (await healthy(c))["owner_control_enabled"]
            assert len(s.journal.db.execute("SELECT * FROM rejected_observations").fetchall()) == 1


# Intent: repeated ordinary rejection records rotate without losing the evidence of a supervised repair.
# Why: routine telemetry should not grow the journal forever or block subsequent lifecycle delivery.
async def test_routine_rejection_audit_is_bounded(tmp_path, tls, unused_tcp_port, monkeypatch):
    actual = Bench.vendor
    action, payload = REPORTS[0]

    # Keep normal lifecycle answers while refusing ordinary status reports on the real TLS connection.
    async def cloud(bench, connection):
        await actual(bench, ReportReplyFilter(connection, action))

    monkeypatch.setattr(Bench, "vendor", cloud)
    async with managed_bench(tmp_path, tls, unused_tcp_port) as rig:
        s = managed_session(rig)
        await s.start()
        c = ManagedController(s)
        j = s.journal
        rig.route_to_trial()
        async with rig.charger(s) as charger:
            await introduced(rig, s, charger, "bounded-report-boot")
            await healthy(c)
            j.capacity = 3
            j.db.execute(
                "INSERT INTO rejected_observations VALUES('repair','StatusNotification','archived',NULL,NULL,"
                "'rejected',1)"
            )
            for i in range(6):
                report = {**payload, "timestamp": f"2026-01-01T00:00:0{i}Z"}
                assert (await rig.bench.call(charger, f"report-{i}", action, report))[0] == 3
                await until(lambda: j.next_delivery() is None)
            rows = list(j.db.execute("SELECT token, prior_state FROM rejected_observations ORDER BY rowid"))
            assert len(rows) == 4 and tuple(rows[0]) == ("repair", "rejected")
            local = (
                await rig.bench.call(charger, "later-start", "StartTransaction", START | {"idTag": "NATIVE_TIMER"})
            )[2]["transactionId"]
            await until(lambda: j.vendor_id(local) is not None)
            stop = {"transactionId": local, "meterStop": 1100, "timestamp": START["timestamp"]}
            assert (await rig.bench.call(charger, "later-stop", "StopTransaction", stop))[0] == 3
            await until(lambda: j.next_delivery() is None)
            path, identity = j.path, j.identity
            await s.close()
        reopened = Journal(path, identity)
        try:
            assert (
                list(reopened.db.execute("SELECT token, prior_state FROM rejected_observations ORDER BY rowid")) == rows
            )
        finally:
            reopened.close()
