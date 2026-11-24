"""Authoritative command outcomes and their independently observed effects."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal

from ..frames import Frame
from .delivery_reply import DeliveryReply
from .ics import unique_object
from .plans import PlanBook
from .stop_settlement import STOP_SESSION, stop_rejections
from .store import Delivery, RecoveryRequiredError, encoded
from .trial_evidence import profile_projection

if TYPE_CHECKING:
    from .store import Journal

TRANSACTION_COMMANDS = frozenset({"RemoteStartTransaction", "RemoteStopTransaction"})
VENDOR_RESULTS = ("Accepted", "Rejected", "NotSupported", "Unknown", "reply")
type TransactionReplyStatus = Literal["Accepted", "Rejected"]
_LOGGER = logging.getLogger(__name__)


# Only a definite transaction reply settles what the charger said, independently of its actual effect.
def transaction_reply_status(frame: Frame) -> TransactionReplyStatus | None:
    if frame[0] == 3:
        if frame[2] == {"status": "Accepted"}:
            return "Accepted"
        if frame[2] == {"status": "Rejected"}:
            return "Rejected"
    return None


# JSON can contain lone surrogates that cannot be bound to SQLite text parameters.
def _reply_identifier(value: object) -> bool:
    return isinstance(value, str) and value.isprintable()


@dataclass(frozen=True)
class SessionEffect:
    """Only actual retained charger lifecycle reports prove a session opened or closed."""

    local: int
    closed: bool


@dataclass(frozen=True)
class VendorCommand:
    """One issued or prepared cloud command, including its retained charger evidence."""

    token: str
    action: str
    state: str
    wire: str | None
    wire_result: str | None
    status: str | None
    session: SessionEffect | None
    saving_reply: bool

    # A recorded result can finish an outcome while the associated lifecycle remains open.
    @property
    def settled(self) -> bool:
        return self.state == "confirmed" and not self.saving_reply


@dataclass(frozen=True)
class VendorWriteEvidence:
    """The common decision about outstanding vendor work and the reason it is still outstanding."""

    received: int
    uncertain: tuple[str, ...]
    legacy_marker: bool
    unfinished: bool

    # Exact missing outcomes and historical unbound markers can call for a protective hold.
    @property
    def unresolved(self) -> bool:
        return bool(self.uncertain) or self.legacy_marker


@dataclass(frozen=True)
class ForwardedReply:
    """A definite charger answer belongs to this exact issued mutation."""

    mutation: str
    action: str
    wire: str
    status: TransactionReplyStatus


@dataclass(frozen=True)
class VendorProfileWrite:
    """The original translated profile and ownership reservation."""

    vendor: str
    local: int
    transaction: int | None
    allocated: bool


@dataclass
class VendorProfileReply:
    """A known acceptance and the exact resource change it acknowledged."""

    mutation: str
    wire: str
    action: str
    payload: dict[str, Any]
    answer: Frame
    profile: VendorProfileWrite | None
    tracked: bool
    status: Literal["Accepted"] = "Accepted"
    saved: bool = False


@dataclass(frozen=True)
class VendorWriteReply:
    """A classified answer to another forwarded write, without retaining its proprietary body."""

    mutation: str
    action: str
    wire: str
    status: str
    plans: tuple[int, ...] = ()
    clearing: bool = False


@dataclass(frozen=True)
class UnsentVendor:
    """A definite no-send that may need to retire a local reservation."""

    mutation: str
    grant: str | None
    profile: VendorProfileWrite | None


@dataclass(frozen=True)
class UnansweredVendor:
    """An issued command whose outcome is still absent."""

    mutation: str
    action: str
    digest: str


type ReceivedVendor = ForwardedReply | VendorProfileReply | VendorWriteReply
type PendingVendor = ReceivedVendor | UnsentVendor | UnansweredVendor


@dataclass(frozen=True)
class OwnerWire:
    """One actual physical write within an owner command. A rollback keeps a separate wire identity."""

    wire: str
    request: str | None
    ticket: str
    boot: str | None
    action: str
    local: int | None
    limit_a: float | None
    reply: str | None = None


@dataclass(frozen=True)
class OwnerCommand:
    """The API receipt and the actual wire evidence belonging to that exact request."""

    identifier: str
    action: str
    revision: int
    state: str
    result: dict[str, Any]
    wires: tuple[OwnerWire, ...]
    sessions: tuple[SessionEffect, ...]

    # A real reply is reporting evidence even when local completion or physical closure still awaits proof.
    @property
    def reply_known(self) -> bool:
        if self.action in ("start", "start_test"):
            relevant = [wire for wire in self.wires if wire.action == "RemoteStartTransaction"]
        elif self.action == "pause":
            stops = [wire for wire in self.wires if wire.action == "RemoteStopTransaction"]
            relevant = stops or [wire for wire in self.wires if wire.action == "SetChargingProfile"]
        elif self.action == "stop_session":
            relevant = [wire for wire in self.wires if wire.action == "RemoteStopTransaction"]
        else:
            relevant = [wire for wire in self.wires if wire.action == "SetChargingProfile" and wire.limit_a]
        return bool(relevant) and relevant[-1].reply in ("Accepted", "Rejected", "NotSupported", "Unknown")


@dataclass(frozen=True)
class OwnerWireScope:
    """Capture only the hardware work of an already claimed owner operation."""

    outcomes: CommandOutcomes
    request: str | None
    ticket: str
    boot: str | None
    wires: dict[str, OwnerWire] = field(default_factory=dict)

    # Report possible dispatch after the final control-scope and resource checks.
    def sent(self, wire: str, action: str, payload: dict[str, Any]) -> OwnerWire | None:
        if action in ("RemoteStartTransaction", "RemoteStopTransaction", "SetChargingProfile", "ClearChargingProfile"):
            local = payload.get("transactionId") if action == "RemoteStopTransaction" else None
            limit = profile_projection(payload).get("limit") if action == "SetChargingProfile" else None
            sent = OwnerWire(wire, self.request, self.ticket, self.boot, action, local, limit)
            self.wires[wire] = sent
            self.outcomes.record_owner_wire(sent)
            return sent
        return None

    # Match a real reply to its earlier exact wire identity; no response can supply a lifecycle by itself.
    def received(self, wire: OwnerWire, frame: Frame) -> None:
        self.wires[wire.wire] = self.outcomes.record_owner_reply(wire, frame)


@dataclass(frozen=True)
class DeliveryOutcome:
    """A report's cloud acknowledgement is separate from the charger session it describes."""

    item: Delivery
    reply: DeliveryReply | None
    session: SessionEffect | None
    uncertain: bool


class CommandOutcomes:
    """Own saved and received command evidence independently of present charging permission."""

    # The journal's mutation capacity also bounds replies waiting for a local write.
    def __init__(self, journal: Journal) -> None:
        self.journal = journal
        self._pending: dict[str, PendingVendor] = {}
        self._owner_writes: dict[str, OwnerWire] = {}
        self._owner_sessions: dict[str, int] = {}
        self._read_binding_pending = False
        self._shutdown_ticket: str | None = None
        self._reported_bad_checkpoints: set[bytes] = set()
        self.delivery_reply: DeliveryReply | None = None

    # Sample delivery certainty from the original report and actual reply, independently of the pump's last error.
    def delivery(self, item: Delivery | None = None) -> DeliveryOutcome | None:
        j = self.journal
        head = j.next_delivery() if item is None else item
        if head is None:
            return None
        reply = self.delivery_reply or DeliveryReply.restore(j, head)
        if reply is not None:
            reply.require(head)
            uncertain = not reply.storage_only(j, head)
        else:
            uncertain = head.action in ("StartTransaction", "StopTransaction") and head.state in (
                "inflight",
                "ambiguous",
                "rejected",
            )
        return DeliveryOutcome(head, reply, self.session(head.transaction), uncertain)

    # Checkpoint a real reply even if the transport is still finishing its outgoing send.
    def capture_delivery(self, item: Delivery, reply: DeliveryReply) -> None:
        self.retain_delivery(reply)
        try:
            reply.save(self.journal, self.journal.next_delivery() or item)
        except sqlite3.Error:
            if self.journal.db.in_transaction:
                raise

    # A known reply is immediately available to readers and retained until the exact head settles.
    def retain_delivery(self, reply: DeliveryReply | None) -> None:
        self.delivery_reply = reply

    # The journal can add these per-wire facts without changing the shape of old management receipts.
    def initialize(self) -> None:
        self.journal.db.executescript("""
            CREATE TABLE IF NOT EXISTS owner_command_wires(
                wire TEXT PRIMARY KEY, request TEXT, ticket TEXT NOT NULL, boot TEXT,
                action TEXT NOT NULL, local INTEGER, limit_a REAL, reply TEXT);
            CREATE INDEX IF NOT EXISTS owner_command_wires_request ON owner_command_wires(request);
            CREATE INDEX IF NOT EXISTS owner_command_wires_ticket ON owner_command_wires(ticket);
            CREATE INDEX IF NOT EXISTS owner_command_wires_accepted_stop ON owner_command_wires(local)
                WHERE action='RemoteStopTransaction' AND reply='Accepted';
            CREATE INDEX IF NOT EXISTS vendor_command_scope_stop ON vendor_command_scope(local)
                WHERE wire_result IN ('Accepted','dispatched');
            CREATE TABLE IF NOT EXISTS owner_start_sessions(
                ticket TEXT PRIMARY KEY, local INTEGER NOT NULL REFERENCES transactions(local));
            CREATE TABLE IF NOT EXISTS vendor_profile_effects(
                mutation TEXT PRIMARY KEY REFERENCES vendor_command_scope(mutation) ON DELETE CASCADE,
                projected TEXT, vendor_alias TEXT, local_session INTEGER, tracked INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS vendor_plan_effects(
                mutation TEXT PRIMARY KEY REFERENCES vendor_command_scope(mutation) ON DELETE CASCADE,
                plans TEXT NOT NULL, clearing INTEGER NOT NULL);
        """)

    # Exact command claims retain their own wire correlation through every separately checked call in the sequence.
    def owner_scope(self, request: str | None, ticket: str, boot: str | None) -> OwnerWireScope:
        return OwnerWireScope(self, request, ticket, boot)

    # Historical receipts without this supplementary evidence remain readable and prove only their stored facts.
    def owner(
        self, identifier: str, *, include_wires: bool = False, row: sqlite3.Row | None = None
    ) -> OwnerCommand | None:
        if row is None:
            row = self.journal.db.execute("SELECT * FROM managed_requests WHERE id=?", (identifier,)).fetchone()
        if row is None:
            return None
        result = json.loads(row["result"])
        if not isinstance(result, dict):
            raise ValueError("owner command metadata is unavailable")
        wires = self.owner_wires("request", identifier) if include_wires else ()
        locals_seen = dict.fromkeys(wire.local for wire in wires if wire.local is not None)
        sessions = tuple(session for local in locals_seen if (session := self.session(local)) is not None)
        return OwnerCommand(identifier, row["action"], row["revision"], row["state"], result, wires, sessions)

    # Merge only buffered evidence for the exact saved call or logical owner ID, including a failed first write.
    def owner_wires(self, key: Literal["request", "ticket"], identifier: str) -> tuple[OwnerWire, ...]:
        rows = self.journal.db.execute(
            "SELECT w.wire,w.request,w.ticket,w.boot,w.action,COALESCE(s.local,w.local),w.limit_a,w.reply "
            "FROM owner_command_wires w LEFT JOIN owner_start_sessions s "
            "ON w.action='RemoteStartTransaction' AND s.ticket=w.ticket "
            f"WHERE w.{key}=? ORDER BY w.rowid",
            (identifier,),
        )
        wires = {row["wire"]: OwnerWire(*row) for row in rows}
        for wire in (*self._retained_owner_replies(), *self._owner_writes.values()):
            if (wire.request if key == "request" else wire.ticket) == identifier:
                previous = wires.get(wire.wire)
                if previous is None or (
                    (previous.request, previous.ticket, previous.boot, previous.action)
                    == (wire.request, wire.ticket, wire.boot, wire.action)
                    and (previous.local == wire.local or wire.action == "RemoteStartTransaction" and wire.local is None)
                    and previous.reply is None
                ):
                    wires[wire.wire] = wire
        return tuple(self._with_bound_session(wire) for wire in wires.values())

    # A reply waiting for storage must still show the actual committed session bound to that exact Start.
    def _with_bound_session(self, wire: OwnerWire) -> OwnerWire:
        if wire.action != "RemoteStartTransaction" or wire.local is not None:
            return wire
        local = self._owner_sessions.get(wire.ticket)
        if local is None:
            row = self.journal.db.execute(
                "SELECT local FROM owner_start_sessions WHERE ticket=?", (wire.ticket,)
            ).fetchone()
            local = row["local"] if row is not None else None
        return wire if local is None else replace(wire, local=local)

    # After the actual Start commits, retain its exact association independently of the wire receipt write.
    def retain_bound_owner_start(self) -> None:
        try:
            saved = self.journal.starts.receipt()
            ticket, local = saved.get("ticket"), saved.get("local")
            if isinstance(ticket, str) and type(local) is int and self.session(local) is not None:
                self._owner_sessions[ticket] = local
            self._read_binding_pending = False
        except sqlite3.Error:
            self._read_binding_pending = True
        self._save_owner_sessions()

    # A link already committed by the real Start can be saved again after temporary storage refusal.
    def _save_owner_sessions(self) -> None:
        for ticket, local in tuple(self._owner_sessions.items()):
            try:
                self.journal.db.execute(
                    "INSERT INTO owner_start_sessions VALUES(?,?) ON CONFLICT(ticket) DO NOTHING", (ticket, local)
                )
            except sqlite3.Error:
                break
            self._owner_sessions.pop(ticket, None)

    # Keep the exact evidence in memory before trying the independent storage write.
    def record_owner_wire(self, wire: OwnerWire) -> None:
        if wire.action == "RemoteStopTransaction" and wire.ticket == self._shutdown_ticket and wire.reply is None:
            self._mark_shutdown_stop(wire)
        self._owner_writes[wire.wire] = wire
        if len(self._owner_writes) > self.journal.capacity:
            self._owner_writes.pop(next(iter(self._owner_writes)))
        self._save_owner_wire(wire)

    # Capture an exact received status before the action-specific result owner can fail while saving it.
    def record_owner_reply(self, wire: OwnerWire, frame: Frame) -> OwnerWire:
        if frame[1] != wire.wire:
            return wire
        definite = transaction_reply_status(frame)
        status: str
        if definite is not None:
            status = definite
        elif frame[0] == 4:
            status = "error"
        else:
            status = next(
                (value for value in ("NotSupported", "Unknown") if frame[2] == {"status": value}),
                "other",
            )
        received = replace(wire, reply=status)
        self.record_owner_wire(received)
        if wire.action in TRANSACTION_COMMANDS and definite is not None and wire.wire in self._owner_writes:
            self._retain_transaction_reply("owner", received)
        return received

    # Preserve the Accepted Stop key for older readers, which treat every record under that key as an acceptance.
    @staticmethod
    def _reply_key(source: Literal["owner", "vendor"], reply: OwnerWire | ForwardedReply) -> str:
        status = reply.reply if isinstance(reply, OwnerWire) else reply.status
        prefix = (
            "retained_stop"
            if reply.action == "RemoteStopTransaction" and status == "Accepted"
            else "retained_transaction_reply"
        )
        return f"{prefix}_{source}:{reply.wire}"

    # An independent checkpoint can succeed when the normal receipt write was refused.
    def _retain_transaction_reply(self, source: Literal["owner", "vendor"], reply: OwnerWire | ForwardedReply) -> None:
        try:
            self.journal.put(self._reply_key(source, reply), asdict(reply))
        except sqlite3.Error:
            if self.journal.db.in_transaction:
                raise

    # Keep damaged evidence available for inspection without logging its contents or blocking other replies.
    def _bad_checkpoint(self, source: Literal["owner", "vendor"], key: bytes) -> None:
        if key not in self._reported_bad_checkpoints:
            self._reported_bad_checkpoints.add(key)
            _LOGGER.warning(
                "Invalid retained %s transaction reply checkpoint. Its outcome requires independent evidence.", source
            )

    # Both formats must describe a definite reply to the exact wire named by the key.
    def _retained_replies[T: (OwnerWire, ForwardedReply)](
        self, source: Literal["owner", "vendor"], decode: Callable[[dict[str, Any]], T | None]
    ) -> tuple[T, ...]:
        rows = self.journal.db.execute(
            "SELECT CAST(key AS BLOB),CAST(value AS BLOB) FROM meta WHERE key GLOB ? OR key GLOB ?",
            (f"retained_stop_{source}:*", f"retained_transaction_reply_{source}:*"),
        ).fetchall()
        replies: dict[str, T] = {}
        conflicts: set[str] = set()
        for raw_key, value in rows:
            try:
                key = raw_key.decode("utf-8")
                payload = json.loads(value.decode("utf-8"), object_pairs_hook=unique_object)
            except ValueError, RecursionError:
                self._bad_checkpoint(source, raw_key)
                continue
            wire = payload.get("wire") if isinstance(payload, dict) else None
            if (
                not isinstance(payload, dict)
                or not isinstance(wire, str)
                or len(wire) > 36
                or not _reply_identifier(wire)
                or key.partition(":")[2] != wire
                or (reply := decode(payload)) is None
                or key != self._reply_key(source, reply)
            ):
                self._bad_checkpoint(source, raw_key)
                continue
            if wire in replies:
                conflicts.add(wire)
                self._bad_checkpoint(source, raw_key)
            replies[wire] = reply
        return tuple(reply for wire, reply in replies.items() if wire not in conflicts)

    # Owner checkpoints retain the bound request, ticket, Boot, and exact wire.
    def _retained_owner_replies(self) -> tuple[OwnerWire, ...]:
        replies = self._retained_replies("owner", self._decode_owner_reply)
        session_stops = Counter(
            reply.ticket
            for reply in replies
            if reply.action == "RemoteStopTransaction" and reply.ticket == reply.request
        )
        valid = []
        for reply in replies:
            if (
                reply.action == "RemoteStopTransaction"
                and reply.ticket == reply.request
                and session_stops[reply.ticket] > 1
            ):
                self._bad_checkpoint("owner", self._reply_key("owner", reply).encode())
            else:
                valid.append(reply)
        return tuple(valid)

    # Validate a retained local reply against the stored wire or its original claim.
    def _decode_owner_reply(self, saved: dict[str, Any]) -> OwnerWire | None:
        if saved.keys() != {"wire", "request", "ticket", "boot", "action", "local", "limit_a", "reply"}:
            return None
        action, local = saved["action"], saved["local"]
        if (
            action not in ("RemoteStartTransaction", "RemoteStopTransaction")
            or saved["reply"] not in ("Accepted", "Rejected")
            or not _reply_identifier(saved["ticket"])
            or any(value is not None and not _reply_identifier(value) for value in (saved["request"], saved["boot"]))
            or saved["limit_a"] is not None
            or (action == "RemoteStartTransaction" and local is not None)
            or (action == "RemoteStopTransaction" and (type(local) is not int or not 0 < local < 2**63))
        ):
            return None
        reply = OwnerWire(**saved)
        primary = self.journal.db.execute(
            "SELECT wire,request,ticket,boot,action,local,limit_a,reply FROM owner_command_wires WHERE wire=?",
            (reply.wire,),
        ).fetchone()
        # Both primary writes can fail. An existing primary row must agree with the checkpoint.
        if primary is not None:
            return reply if OwnerWire(*primary) in (reply, replace(reply, reply=None)) else None
        if action == "RemoteStopTransaction" and not self._owner_stop_claim_matches(reply):
            return None
        return reply

    # A pending session Stop and the latest shutdown retain independent dispatch claims.
    def _owner_stop_claim_matches(self, reply: OwnerWire) -> bool:
        j = self.journal
        if j.db.execute("SELECT 1 FROM sqlite_schema WHERE name='managed_requests'").fetchone():
            claims = j.db.execute(
                "SELECT id,action,CAST(result AS BLOB) FROM managed_requests WHERE id=? OR (id=? AND action=?)",
                (reply.request, reply.ticket, STOP_SESSION),
            ).fetchall()
            for request, action, raw in claims:
                try:
                    result = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)
                except ValueError, RecursionError:
                    return False
                if not isinstance(result, dict):
                    return False
                if action == STOP_SESSION:
                    bound = result.get("_session_stop")
                    if (
                        reply.request != request
                        or reply.ticket != request
                        or not isinstance(bound, dict)
                        or type(bound.get("local")) is not int
                        or bound["local"] != reply.local
                        or type(bound.get("dispatched")) not in (int, bool)
                        or bound["dispatched"] != 1
                        or j.db.execute(
                            "SELECT 1 FROM owner_command_wires WHERE request=? AND ticket=? "
                            "AND action='RemoteStopTransaction' LIMIT 1",
                            (request, request),
                        ).fetchone()
                        is not None
                    ):
                        return False
                elif action == "pause":
                    bound = result.get("_stop")
                    if (
                        isinstance(bound, dict)
                        and bound.get("ticket") == reply.ticket
                        and (bound.get("local"), bound.get("boot")) != (reply.local, reply.boot)
                    ):
                        return False
                else:
                    return False
        if reply.request is None:
            shutdown = j.get("trial_shutdown_attempt", {})
            if isinstance(shutdown, dict) and shutdown.get("ticket") == reply.ticket and "stop_wire" in shutdown:
                return (shutdown["stop_wire"], shutdown.get("boot"), shutdown.get("local")) == (
                    reply.wire,
                    reply.boot,
                    reply.local,
                )
        return True

    # A forwarded checkpoint still belongs to the original vendor mutation and exact charger call.
    def _retained_vendor_replies(self) -> tuple[ForwardedReply, ...]:
        return self._retained_replies("vendor", self._decode_vendor_reply)

    # Accept a definite retained vendor reply only for its original forwarded attempt.
    def _decode_vendor_reply(self, saved: dict[str, Any]) -> ForwardedReply | None:
        if (
            saved.keys() != {"mutation", "action", "wire", "status"}
            or not _reply_identifier(saved["mutation"])
            or saved["action"] not in ("RemoteStartTransaction", "RemoteStopTransaction")
            or saved["status"] not in ("Accepted", "Rejected")
        ):
            return None
        reply = ForwardedReply(**saved)
        return reply if self._forwarded_reply_binding(reply) is not None else None

    # Retire a fallback only after the original outcome is durable.
    def _clear_retained_reply(self, source: Literal["owner", "vendor"], reply: OwnerWire | ForwardedReply) -> None:
        in_transaction = self.journal.db.in_transaction
        try:
            self.journal.db.execute("DELETE FROM meta WHERE key=?", (self._reply_key(source, reply),))
        except sqlite3.Error:
            # A refused cleanup is repeatable. An implicit rollback must end the original transaction.
            if in_transaction and not self.journal.db.in_transaction:
                raise

    # The optional evidence write cannot delay a protective send already authorized by the original journal.
    def _save_owner_wire(self, wire: OwnerWire) -> None:
        try:
            self.journal.db.execute(
                "INSERT INTO owner_command_wires VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(wire) DO UPDATE SET reply=excluded.reply "
                "WHERE request IS excluded.request AND ticket=excluded.ticket AND boot IS excluded.boot "
                "AND action=excluded.action AND local IS excluded.local "
                "AND (reply IS NULL OR reply=excluded.reply)",
                (wire.wire, wire.request, wire.ticket, wire.boot, wire.action, wire.local, wire.limit_a, wire.reply),
            )
        except sqlite3.Error:
            if self.journal.db.in_transaction:
                raise
            return
        self._owner_writes.pop(wire.wire, None)

    # A failed optional evidence write can recover after its caller finishes.
    def retry_owner_writes(self) -> None:
        retained = self._retained_owner_replies()
        for reply in retained:
            buffered = self._owner_writes.get(reply.wire)
            if buffered is None or buffered.reply is None and replace(reply, reply=None) == buffered:
                self._owner_writes[reply.wire] = reply
        for wire in tuple(self._owner_writes.values()):
            self._save_owner_wire(wire)
        for reply in retained:
            if reply.wire not in self._owner_writes:
                self._clear_retained_reply("owner", reply)
        if self._read_binding_pending:
            self.retain_bound_owner_start()
        else:
            self._save_owner_sessions()
        self._publish_owner_start_reply()

    # Receipt settlement sees a refusal while its exact wire reply awaits promotion from memory or the checkpoint.
    def buffered_stop_rejections(self) -> tuple[list[tuple[str, int]], list[tuple[str, str]]]:
        owner = [
            (wire.request, wire.local)
            for wire in (*self._retained_owner_replies(), *self._owner_writes.values())
            if wire.action == "RemoteStopTransaction"
            and wire.reply == "Rejected"
            and wire.request is not None
            and wire.local is not None
        ]
        vendor = [
            (reply.mutation, reply.wire)
            for reply in (*self._retained_vendor_replies(), *self._pending.values())
            if isinstance(reply, ForwardedReply)
            and reply.action == "RemoteStopTransaction"
            and reply.status == "Rejected"
        ]
        return owner, vendor

    # Recover an older Start stage only when a real reply names that exact issued Start.
    def recover_owner_start(self) -> None:
        self.retain_bound_owner_start()
        self._publish_owner_start_reply()

    # Both polling and startup can finish the same issued Start from its exact wire evidence alone.
    def _publish_owner_start_reply(self) -> None:
        try:
            receipt = self.journal.starts.receipt()
            ticket = receipt.get("ticket")
            if not isinstance(ticket, str) or receipt.get("stage") != "issued":
                return
            wires = [
                wire
                for wire in self.owner_wires("ticket", ticket)
                if wire.action == "RemoteStartTransaction"
                and ("request_id" not in receipt or wire.request == receipt["request_id"])
            ]
            if len(wires) == 1 and (status := wires[0].reply) in ("Accepted", "Rejected"):
                self.journal.starts.record_reply_status(ticket, status, request_id=receipt.get("request_id"))
        except sqlite3.Error:
            if self.journal.db.in_transaction:
                raise

    # Only a real received answer takes the place of the existing timeout or unsent bookkeeping.
    def _received(self, mutation: str) -> ReceivedVendor | None:
        pending = self._pending.get(mutation)
        return pending if isinstance(pending, (ForwardedReply, VendorProfileReply, VendorWriteReply)) else None

    # Read the real journal lifecycle without granting authority to a matching number by itself.
    def session(self, local: int | None) -> SessionEffect | None:
        if type(local) is not int:
            return None
        row = self.journal.db.execute("SELECT stop FROM transactions WHERE local=?", (local,)).fetchone()
        return SessionEffect(local, row["stop"] is not None) if row is not None else None

    # Idle requires ledger closure, including the original target if the operation already had one.
    def stop_report_closed(self, local: int | None = None) -> bool:
        if self.journal.active() is not None:
            return False
        session = self.session(local)
        return local is None or (session is not None and session.closed)

    # A matched reply stays with its exact allocated session across Boot and process replacement.
    def accepted_stop(self, local: int | None, *, ticket: str | None = None) -> bool:
        if local is None:
            return False
        if any(
            reply.action == "RemoteStopTransaction"
            and reply.reply == "Accepted"
            and reply.local == local
            and (ticket is None or reply.ticket == ticket)
            for reply in self._retained_owner_replies()
        ):
            return True
        if (
            any(
                wire.action == "RemoteStopTransaction"
                and wire.local == local
                and wire.reply == "Accepted"
                and (ticket is None or wire.ticket == ticket)
                for wire in self._owner_writes.values()
            )
            or self.journal.db.execute(
                "SELECT 1 FROM owner_command_wires WHERE local=:local AND action='RemoteStopTransaction' "
                "AND reply='Accepted' AND (:ticket IS NULL OR ticket=:ticket) LIMIT 1",
                {"local": local, "ticket": ticket},
            ).fetchone()
            is not None
        ):
            return True
        return ticket is None and self._accepted_forwarded_stop(local)

    # A forwarded Stop has the same session effect while its exact reply is being saved or already durable.
    def _accepted_forwarded_stop(self, local: int) -> bool:
        rows = self.journal.db.execute(
            "SELECT token,wire_request,wire_result FROM vendor_command_scope JOIN mutations ON mutation=token "
            "WHERE local=? AND origin='vendor' AND action='RemoteStopTransaction' "
            "AND wire_result IN ('Accepted','dispatched')",
            (local,),
        )
        for row in rows:
            if row["wire_result"] == "Accepted":
                return True
            reply = self._received(row["token"])
            if (
                isinstance(reply, ForwardedReply)
                and reply.action == "RemoteStopTransaction"
                and reply.wire == row["wire_request"]
                and reply.status == "Accepted"
            ):
                return True
        return False

    # Pin the target before any wire work and retain an older unanswered Stop for that same session.
    def claim_shutdown_stop(self, attempt: str, local: int | None, ticket: str, boot: str | None) -> None:
        prior_pending = False
        local_attempts: tuple[str, ...] = ()
        if local is not None and self.journal.active() == local:
            prior_pending = self.accepted_stop(local)
            if not prior_pending:
                prior_pending, inherited = self._shutdown_stop_dependencies(local)
                if not prior_pending:
                    local_attempts = tuple(dict.fromkeys((*inherited, *self.session_stop_dispatches(local))))
        self.journal.put(
            "trial_shutdown_attempt",
            {
                "id": attempt,
                "local": local,
                "ticket": ticket,
                "boot": boot,
                "state": "pending",
                "prior_pending_stop": prior_pending or bool(local_attempts),
                **({"prior_local_attempts": local_attempts} if local_attempts else {}),
                "stop_wire": None,
            },
        )
        self._shutdown_ticket = ticket

    # The sender may run only after the exact possible Stop dispatch has committed, even if wire logging fails.
    def _mark_shutdown_stop(self, wire: OwnerWire) -> None:
        j = self.journal
        latest = j.get("trial_shutdown_attempt", {})
        if (
            j.db.in_transaction
            or latest.get("ticket") != wire.ticket
            or latest.get("local") != wire.local
            or latest.get("boot") != wire.boot
            or latest.get("state") != "pending"
            or "stop_wire" not in latest
            or latest["stop_wire"] is not None
        ):
            raise RecoveryRequiredError("shutdown Stop dispatch requires its original committed claim")
        j.put("trial_shutdown_attempt", {**latest, "stop_wire": wire.wire})

    # Preserve the historical unsent field for older readers when this attempt can prove the Stop never dispatched.
    def record_shutdown_stop(self, attempt: str, state: str) -> None:
        latest = self.journal.get("trial_shutdown_attempt", {})
        if latest.get("id") == attempt:
            unsent = "stop_wire" in latest and latest["stop_wire"] is None
            self.journal.put("trial_shutdown_attempt", {**latest, "state": state, "unsent": unsent})

    # A possibly sent shutdown Stop also retains zero until the exact charger report resolves it.
    def awaits_stop_report(self, local: int | None) -> bool:
        if local is None or self.journal.active() != local:
            return False
        return self.accepted_stop(local) or self.unanswered_shutdown_stop(local) or self.session_stop_dispatched(local)

    # Session-only Stop owns a possible dispatch even if its separate wire record could not be saved.
    def session_stop_dispatches(self, local: int) -> tuple[str, ...]:
        if self.journal.get("managed_mode") is None:
            return ()
        rejections = stop_rejections(self.journal)
        rows = self.journal.db.execute(
            f"{rejections.prefix}SELECT id FROM managed_requests r WHERE r.action='stop_session' "
            "AND r.state IN ('running','uncertain') AND json_extract(r.result,'$._session_stop.local')=? "
            f"AND json_extract(r.result,'$._session_stop.dispatched')=1 "
            f"AND NOT {rejections.owner_refusal('r.id', '?')} ORDER BY id",
            (*rejections.bindings, local, local),
        )
        return tuple(row["id"] for row in rows)

    # Test only a possible local dispatch for the exact session.
    def session_stop_dispatched(self, local: int) -> bool:
        return bool(self.session_stop_dispatches(local))

    # A live session-only receipt can own the closure wait by joining an earlier actual Stop.
    def session_stop_tracks_closure(self, local: int) -> bool:
        if self.session_stop_dispatched(local):
            return True
        if self.journal.get("managed_mode") is None:
            return False
        joined = self.journal.db.execute(
            "SELECT 1 FROM managed_requests WHERE action='stop_session' AND state IN ('running','uncertain') "
            "AND json_extract(result,'$._session_stop.local')=? AND json_extract(result,'$._session_stop.joined')=1 "
            "LIMIT 1",
            (local,),
        ).fetchone()
        return joined is not None and (
            self.accepted_stop(local) or self.unanswered_vendor_stop(local) or self.unanswered_shutdown_stop(local)
        )

    # A possibly sent vendor Stop can still have happened when the device returns an error.
    def vendor_stop_dispatches(self, local: int, *, boot: str | None = None) -> tuple[str, ...]:
        rows = self.journal.db.execute(
            "SELECT token,wire_request FROM vendor_command_scope JOIN mutations ON mutation=token "
            "WHERE local=:local AND origin='vendor' AND action='RemoteStopTransaction' "
            "AND (:boot IS NULL OR charger_boot=:boot) "
            "AND wire_result IN ('dispatched','call_error') AND state IN ('pending','uncertain') ORDER BY token",
            {"local": local, "boot": boot},
        )
        pending = []
        for row in rows:
            reply = self._received(row["token"])
            if (
                not isinstance(reply, ForwardedReply)
                or reply.action != "RemoteStopTransaction"
                or reply.wire != row["wire_request"]
                or reply.status != "Rejected"
            ):
                pending.append(row["token"])
        return tuple(pending)

    # Check the exact forwarded dispatches without treating a prepared request as sent.
    def unanswered_vendor_stop(self, local: int, *, boot: str | None = None) -> bool:
        return bool(self.vendor_stop_dispatches(local, boot=boot))

    # A pending owner wire can cover this session during the same authenticated charger Boot.
    def unanswered_owner_stop(self, local: int, boot: str) -> bool:
        tickets = {
            row[0]
            for row in self.journal.db.execute(
                "SELECT DISTINCT ticket FROM owner_command_wires WHERE action='RemoteStopTransaction' AND local=? "
                "AND boot=? "
                "AND (reply IS NULL OR reply NOT IN ('Accepted','Rejected'))",
                (local, boot),
            )
        }
        tickets.update(
            wire.ticket
            for wire in self._owner_writes.values()
            if wire.action == "RemoteStopTransaction" and wire.local == local and wire.boot == boot
        )
        return any(
            wire.action == "RemoteStopTransaction"
            and wire.local == local
            and wire.boot == boot
            and wire.reply not in ("Accepted", "Rejected")
            for ticket in tickets
            for wire in self.owner_wires("ticket", ticket)
        )

    # This is evidence to wait, without an Accepted reply that can be sent to another caller.
    def unanswered_shutdown_stop(self, local: int) -> bool:
        pending, local_attempts = self._shutdown_stop_dependencies(local)
        return pending or bool(local_attempts)

    # A joined ordinary Stop can settle independently of an older claim or this shutdown's own wire.
    def _shutdown_stop_dependencies(self, local: int) -> tuple[bool, tuple[str, ...]]:
        shutdown = self.journal.get("trial_shutdown_attempt", {})
        ticket = shutdown.get("ticket")
        if (
            shutdown.get("local") != local
            or not isinstance(ticket, str)
            or shutdown.get("state") not in ("pending", "uncertain")
        ):
            return False, ()
        local_attempts: tuple[str, ...] = ()
        if shutdown.get("prior_pending_stop"):
            attempts = shutdown.get("prior_local_attempts")
            if (
                not isinstance(attempts, list)
                or not attempts
                or not all(isinstance(item, str) and item for item in attempts)
            ):
                return True, ()  # Older claims did not identify the underlying Stop.
            rejections = stop_rejections(self.journal)
            local_attempts = tuple(
                row[0]
                for row in self.journal.db.execute(
                    f"{rejections.prefix}SELECT a.value FROM json_each(?) a "
                    f"WHERE NOT {rejections.owner_refusal('a.value', '?')}",
                    (*rejections.bindings, encoded(attempts), local),
                )
            )
        if shutdown.get("unsent"):
            return False, local_attempts
        stops = [
            wire
            for wire in self.owner_wires("ticket", ticket)
            if wire.local == local and wire.action == "RemoteStopTransaction"
        ]
        if any(wire.reply != "Rejected" for wire in stops):
            return True, local_attempts
        # An absent wire row is only conclusive for a new claim that still proves its Stop was never issued.
        if "stop_wire" not in shutdown:
            return not stops, local_attempts
        marked = shutdown["stop_wire"]
        return marked is not None and not any(wire.wire == marked for wire in stops), local_attempts

    # Read one original mutation. Historical unrelated rows cannot lend it a reply or local session.
    def vendor(self, mutation: object) -> VendorCommand | None:
        if not isinstance(mutation, str):
            return None
        row = self.journal.db.execute(
            "SELECT token,action,state,wire_request,wire_result,local FROM mutations "
            "LEFT JOIN vendor_command_scope ON mutation=token WHERE token=? AND origin='vendor'",
            (mutation,),
        ).fetchone()
        if row is None:
            return None
        local = row["local"]
        if local is None and row["action"] == "RemoteStartTransaction":
            # Old builds kept the binding only in the exact unfinished grant.
            grant = self.journal.get("vendor_start_grant", {})
            if grant.get("mutation") == mutation:
                local = grant.get("local")
        pending = self._received(mutation)
        result = row["wire_result"]
        if pending is not None:
            status = pending.status
        elif result in (*VENDOR_RESULTS, "call_error"):
            status = result
        else:
            status = None
        return VendorCommand(
            mutation,
            row["action"],
            row["state"],
            row["wire_request"],
            result,
            status,
            self.session(local),
            pending is not None,
        )

    # The Start's real session link is retained after its single-use authorization grant expires or closes.
    def bind_vendor_start(self, mutation: object, local: int) -> None:
        if not isinstance(mutation, str):
            return
        row = self.journal.db.execute(
            "SELECT action,origin,local FROM mutations JOIN vendor_command_scope ON mutation=token WHERE token=?",
            (mutation,),
        ).fetchone()
        if row is None or (row["action"], row["origin"]) != ("RemoteStartTransaction", "vendor"):
            return  # An older grant without a scoped command can still retain its historical behavior.
        if row["local"] not in (None, local):
            raise RecoveryRequiredError("vendor Start already belongs to a different real session")
        self.journal.db.execute("UPDATE vendor_command_scope SET local=? WHERE mutation=?", (local, mutation))

    # Distinguish physical uncertainty from normal waiting and local settlement, including old journal markers.
    def vendor_evidence(
        self,
        has_pending_call: Callable[[str | None], bool] | None = None,
    ) -> VendorWriteEvidence:
        rows = self.journal.db.execute(
            "SELECT token,state,wire_request,wire_result FROM mutations "
            "LEFT JOIN vendor_command_scope ON mutation=token WHERE origin='vendor' AND state!='confirmed'"
        ).fetchall()
        received: set[str] = set()
        uncertain = []
        for row in rows:
            token = row["token"]
            pending = self._received(token)
            status = pending.status if pending is not None else row["wire_result"]
            if status == "call_error":
                uncertain.append(token)
            elif status in VENDOR_RESULTS:
                received.add(token)
            elif row["state"] == "uncertain" or (
                row["wire_result"] not in ("prepared", "prepared_new_profile")
                and (has_pending_call is None or not has_pending_call(row["wire_request"]))
            ):
                uncertain.append(token)
        marker = self.journal.get("uncertain_vendor_command")
        reference = marker.get("mutation") if isinstance(marker, dict) else None
        legacy = bool(marker) and (
            not isinstance(reference, str) or reference not in uncertain and reference not in received
        )
        return VendorWriteEvidence(len(received), tuple(uncertain), legacy, bool(rows) or legacy)

    # Only one exact unanswered physical command can own a recoverable reply hold.
    def sole_forwarded_timeout(self, evidence: VendorWriteEvidence) -> str | None:
        if evidence.legacy_marker or len(evidence.uncertain) != 1:
            return None
        command = self.vendor(evidence.uncertain[0])
        if (
            command is not None
            and command.action in TRANSACTION_COMMANDS
            and command.wire_result == "dispatched"
            and command.status is None
        ):
            return command.token
        return None

    # Keep the fact before any write, even when cancellation ends the original request handler.
    def capture(self, mutation: str, action: str, wire: str, frame: Frame) -> ForwardedReply | None:
        if action not in TRANSACTION_COMMANDS or frame[1] != wire:
            return None
        status = transaction_reply_status(frame)
        if status is None:
            return None
        previous = self._received(mutation)
        if isinstance(previous, ForwardedReply):
            return previous if (previous.action, previous.wire) == (action, wire) else None
        reply = ForwardedReply(mutation, action, wire, status)
        self._pending[mutation] = reply
        return reply

    # A received outcome is available to lifecycle admission even while its write is pending.
    def status(self, mutation: object) -> Literal["Accepted", "Rejected"] | None:
        result = self.vendor(mutation)
        if result is not None and result.status == "Accepted":
            return "Accepted"
        if result is not None and result.status == "Rejected":
            return "Rejected"
        return None

    # Status can distinguish a known reply awaiting storage from an unanswered command.
    def awaits_storage(self, mutation: str) -> bool:
        return self._received(mutation) is not None

    # Persist the classified answer before finishing its independent local bookkeeping.
    def publish(self, reply: ForwardedReply) -> bool:
        if self._received(reply.mutation) != reply:
            return True
        j = self.journal
        recorded = False
        try:
            with j.atomic():
                needs_settlement = self._record_forwarded_reply(reply)
            recorded = True
            if needs_settlement:
                with j.atomic():
                    self._settle(reply.mutation, reply.action, reply.status)
        except sqlite3.Error:
            if j.db.in_transaction:
                raise
            if not recorded:
                self._retain_transaction_reply("vendor", reply)
            return False
        self._pending.pop(reply.mutation, None)
        self._clear_retained_reply("vendor", reply)
        return True

    # Live publication and recovery use the same original forwarded attempt binding.
    def _forwarded_reply_binding(self, reply: ForwardedReply) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self.journal.db.execute(
            "SELECT origin,action,wire_request,state,wire_result FROM mutations "
            "JOIN vendor_command_scope ON mutation=token WHERE token=?",
            (reply.mutation,),
        ).fetchone()
        if (
            row is None
            or tuple(row)[:3] != ("vendor", reply.action, reply.wire)
            or reply.action not in TRANSACTION_COMMANDS
            or reply.status not in ("Accepted", "Rejected")
            or (row["state"], row["wire_result"])
            not in (
                ("pending", "dispatched"),
                ("uncertain", "dispatched"),
                ("pending", reply.status),
                ("uncertain", reply.status),
                ("confirmed", reply.status),
            )
            and not (
                row["state"] == "confirmed"
                and row["wire_result"] == "dispatched"
                and reply.action == "RemoteStopTransaction"
                and reply.status == "Accepted"
            )
        ):
            return None
        return row

    # Save the exact reply and tell the caller whether that mutation still needs settlement.
    def _record_forwarded_reply(self, reply: ForwardedReply) -> bool:
        j = self.journal
        row = self._forwarded_reply_binding(reply)
        if row is None:
            raise RecoveryRequiredError("charger reply no longer has its original forwarded command")
        if row["wire_result"] == "dispatched":
            j.record_vendor_result(reply.mutation, reply.status)
            if row["state"] != "confirmed":
                j.db.execute("UPDATE mutations SET state='pending' WHERE token=?", (reply.mutation,))
        return bool(row["state"] != "confirmed")

    # Save the exact reply before applying any plan changes or final outcome.
    def save_vendor_write(self, reply: VendorWriteReply) -> bool:
        self._pending[reply.mutation] = reply
        j = self.journal
        try:
            with j.atomic():
                row = j.db.execute(
                    "SELECT action,wire_request,wire_result,state FROM mutations "
                    "JOIN vendor_command_scope ON mutation=token WHERE token=? AND origin='vendor'",
                    (reply.mutation,),
                ).fetchone()
                if row is None or (row["action"], row["wire_request"]) != (reply.action, reply.wire):
                    raise RecoveryRequiredError("charger reply no longer owns its original vendor write")
                state = row["state"], row["wire_result"]
                late_error = (
                    state == ("uncertain", "dispatched")
                    and reply.action in TRANSACTION_COMMANDS
                    and reply.status == "call_error"
                )
                if not late_error and state not in (("pending", "dispatched"), ("pending", reply.status)):
                    raise RecoveryRequiredError("vendor write is no longer awaiting this reply")
                if row["wire_result"] == "dispatched":
                    if reply.plans:
                        j.db.execute(
                            "INSERT INTO vendor_plan_effects VALUES(?,?,?)",
                            (reply.mutation, encoded(reply.plans), reply.clearing),
                        )
                    j.record_vendor_result(reply.mutation, reply.status)
                    if late_error:
                        j.db.execute("UPDATE mutations SET state='pending' WHERE token=?", (reply.mutation,))
            with j.atomic():
                self._settle_vendor_write(reply.mutation, reply.action, reply.status)
        except sqlite3.Error:
            if j.db.in_transaction:
                raise
            return False
        self._pending.pop(reply.mutation, None)
        return True

    # Finish a saved answer without either peer, preserving uncertainty from an actual error reply.
    def _settle_vendor_write(self, mutation: str, action: str, status: str) -> None:
        j = self.journal
        saved = j.db.execute("SELECT plans,clearing FROM vendor_plan_effects WHERE mutation=?", (mutation,)).fetchone()
        if saved is not None:
            PlanBook(j).accepted(tuple(json.loads(saved["plans"])), bool(saved["clearing"]))
            j.db.execute("DELETE FROM vendor_plan_effects WHERE mutation=?", (mutation,))
        confirmed = status != "call_error"
        j.mutation_finished(mutation, confirmed)
        if not confirmed:
            digest = j.db.execute("SELECT digest FROM mutations WHERE token=?", (mutation,)).fetchone()[0]
            j.put("uncertain_vendor_command", {"mutation": mutation, "action": action, "digest": digest})

    # Checkpoint the matched answer before making any change to the profile's resource record.
    def save_profile_reply(self, reply: VendorProfileReply) -> bool:
        self._pending[reply.mutation] = reply
        if reply.saved:
            return True
        j = self.journal
        try:
            with j.atomic():
                row = j.db.execute(
                    "SELECT action,wire_request,wire_result,profile FROM mutations "
                    "JOIN vendor_command_scope ON mutation=token WHERE token=? AND origin='vendor' AND state='pending'",
                    (reply.mutation,),
                ).fetchone()
                if (
                    row is None
                    or (row["action"], row["wire_request"]) != (reply.action, reply.wire)
                    or row["wire_result"] not in ("dispatched", "Accepted")
                ):
                    raise RecoveryRequiredError("profile reply no longer owns its original command")
                if row["wire_result"] == "dispatched":
                    projected = (
                        encoded(profile_projection(reply.payload)) if reply.action == "SetChargingProfile" else None
                    )
                    profile = reply.profile
                    j.db.execute(
                        "INSERT INTO vendor_profile_effects VALUES(?,?,?,?,?)",
                        (
                            reply.mutation,
                            projected,
                            profile.vendor if profile is not None else None,
                            profile.transaction if profile is not None else None,
                            reply.tracked,
                        ),
                    )
                    j.record_vendor_result(reply.mutation, reply.status)
        except sqlite3.Error:
            if j.db.in_transaction:
                raise
            return False
        reply.saved = True
        return True

    # Settle once the actual response is durable. A failed write retains this exact resource for retry.
    def publish_profile(self, reply: VendorProfileReply) -> bool:
        if not self.awaits_storage(reply.mutation):
            return True
        if not self.save_profile_reply(reply):
            return False
        j = self.journal
        try:
            with j.atomic():
                self._settle_profile(reply.mutation)
        except sqlite3.Error:
            if j.db.in_transaction:
                raise
            return False
        self._pending.pop(reply.mutation, None)
        return True

    # Finish the same saved resource on retry or restart. A verified absence wins over an earlier acknowledgement.
    def _settle_profile(self, mutation: str) -> None:
        j = self.journal
        saved = j.db.execute(
            "SELECT action,profile,projected,vendor_alias,local_session,tracked FROM vendor_profile_effects "
            "JOIN vendor_command_scope USING(mutation) JOIN mutations ON token=mutation "
            "WHERE mutation=? AND origin='vendor' AND state='pending' AND wire_result='Accepted'",
            (mutation,),
        ).fetchone()
        if saved is None:
            raise RecoveryRequiredError("saved profile reply is missing its original resource")
        identifier = saved["profile"]
        current = True
        if saved["tracked"]:
            key = "profile:" + str(identifier)
            row = j.db.execute("SELECT state,desired FROM trial_resources WHERE key=?", (key,)).fetchone()
            current = (
                row is not None
                and row["state"] != "absent_verified"
                and (saved["action"] == "ClearChargingProfile" or row["desired"] == saved["projected"])
            )
            if current:
                if saved["action"] == "SetChargingProfile":
                    j.db.execute(
                        "UPDATE trial_resources SET confirmed=desired,state='confirmed',observed_digest=NULL "
                        "WHERE key=? AND state!='confirmed'",
                        (key,),
                    )
                else:
                    j.db.execute(
                        "UPDATE trial_resources SET state='clear_acknowledged' "
                        "WHERE key=? AND state!='clear_acknowledged'",
                        (key,),
                    )
        if current and saved["vendor_alias"] is not None:
            self._accept_profile(VendorProfileWrite(saved["vendor_alias"], identifier, saved["local_session"], False))
        j.mutation_finished(mutation, True)

    # Commit the last accepted definition even if its old transaction has since closed.
    def _accept_profile(self, profile: VendorProfileWrite) -> None:
        j = self.journal
        profiles, transactions = j.get("vendor_profiles", {}), j.get("vendor_profile_transactions", {})
        if profile.transaction is None:
            profiles[profile.vendor] = profile.local
            transactions.pop(profile.vendor, None)
        elif j.active() == profile.transaction:
            profiles[profile.vendor] = profile.local
            transactions[profile.vendor] = profile.transaction
        elif profiles.get(profile.vendor) == profile.local:
            profiles.pop(profile.vendor, None)
            transactions.pop(profile.vendor, None)
        j.put("vendor_profiles", profiles)
        j.put("vendor_profile_transactions", transactions)

    # Retire only this exact unsent reservation, without waiting on or sending any device command.
    def retire_unsent(self, mutation: str | None, grant: str | None, profile: VendorProfileWrite | None) -> bool:
        j = self.journal
        try:
            with j.atomic():
                if mutation is not None:
                    j.db.execute("DELETE FROM mutations WHERE token=?", (mutation,))
                if grant is not None and j.get("vendor_start_grant", {}).get("token") == grant:
                    j.put("vendor_start_grant", {})
                if profile is not None and profile.allocated:
                    profiles = j.get("vendor_profiles", {})
                    if profiles.get(profile.vendor) == profile.local:
                        profiles.pop(profile.vendor)
                        j.put("vendor_profiles", profiles)
        except sqlite3.Error:
            if j.db.in_transaction:
                raise
            if mutation is not None:
                self._pending[mutation] = UnsentVendor(mutation, grant, profile)
            return False
        if mutation is not None:
            self._pending.pop(mutation, None)
        return True

    # Retry the saved timeout without replacing a real answer that may have arrived meanwhile.
    def record_unanswered(self, mutation: str, action: str, digest: str) -> bool:
        if self.awaits_storage(mutation):
            return True
        j = self.journal
        try:
            with j.atomic():
                row = j.db.execute("SELECT state FROM mutations WHERE token=?", (mutation,)).fetchone()
                if row is not None and row[0] == "pending":
                    j.mutation_finished(mutation, False)
                    j.put("uncertain_vendor_command", {"mutation": mutation, "action": action, "digest": digest})
        except sqlite3.Error:
            if j.db.in_transaction:
                raise
            self._pending[mutation] = UnansweredVendor(mutation, action, digest)
            return False
        self._pending.pop(mutation, None)
        return True

    # One command has at most one retained settlement, independent of either connection's lifetime.
    def retry(self) -> None:
        for pending in tuple(self._pending.values()):
            if isinstance(pending, ForwardedReply):
                self.publish(pending)
            elif isinstance(pending, VendorProfileReply):
                self.publish_profile(pending)
            elif isinstance(pending, VendorWriteReply):
                self.save_vendor_write(pending)
            elif isinstance(pending, UnsentVendor):
                self.retire_unsent(pending.mutation, pending.grant, pending.profile)
            else:
                self.record_unanswered(pending.mutation, pending.action, pending.digest)

    # Reuse live publication after initialization so a failed settlement can roll back and retry independently.
    def recover_transaction_replies(self) -> None:
        replies = {reply.mutation: reply for reply in self._retained_vendor_replies()}
        for row in self.journal.db.execute(
            "SELECT token,action,wire_request,wire_result FROM mutations JOIN vendor_command_scope ON mutation=token "
            "WHERE origin='vendor' AND state IN ('pending','uncertain') "
            "AND action IN ('RemoteStartTransaction','RemoteStopTransaction') "
            "AND wire_result IN ('Accepted','Rejected')"
        ).fetchall():
            reply = ForwardedReply(*row)
            prior = replies.get(reply.mutation)
            if prior is not None and prior != reply:
                self._clear_retained_reply("vendor", prior)
            replies[reply.mutation] = reply
        for reply in replies.values():
            self._pending[reply.mutation] = reply
            try:
                self.publish(reply)
            except RecoveryRequiredError:
                # An obsolete checkpoint cannot replace another attempt's answer.
                self._pending.pop(reply.mutation, None)
                self._clear_retained_reply("vendor", reply)

    # Storage-only resource bookkeeping can still join startup's journal initialization transaction.
    def recover_saved(self) -> None:
        for row in self.journal.db.execute(
            "SELECT mutation FROM vendor_profile_effects JOIN vendor_command_scope USING(mutation) "
            "JOIN mutations ON token=mutation WHERE state='pending' AND wire_result='Accepted'"
        ).fetchall():
            self._settle_profile(row["mutation"])
        for row in self.journal.db.execute(
            "SELECT token,action,wire_result FROM mutations JOIN vendor_command_scope ON mutation=token "
            "WHERE origin='vendor' AND state='pending' "
            "AND wire_result IN ('Accepted','Rejected','NotSupported','Unknown','reply','call_error') "
            "AND NOT (action IN ('RemoteStartTransaction','RemoteStopTransaction') "
            "AND wire_result IN ('Accepted','Rejected'))"
        ).fetchall():
            self._settle_vendor_write(row["token"], row["action"], row["wire_result"])

    # A refusal revokes only its own Start grant. A Stop answer never closes the physical session.
    def _settle(self, mutation: str, action: str, status: str) -> None:
        j = self.journal
        if action == "RemoteStartTransaction" and status == "Rejected":
            grant = j.get("vendor_start_grant", {})
            if grant.get("mutation") == mutation:
                j.put("vendor_start_grant", {})
        self.clear_vendor_marker(mutation)
        j.mutation_finished(mutation, True)
        j.retire_reply_fault("forwarded_timeout", mutation)

    # Exact reply or operator evidence can retire only the matching command's old hold marker.
    def clear_vendor_marker(self, mutation: str) -> None:
        j = self.journal
        marker = j.get("uncertain_vendor_command", {})
        if isinstance(marker, dict) and marker.get("mutation") == mutation:
            j.db.execute("DELETE FROM meta WHERE key='uncertain_vendor_command'")
