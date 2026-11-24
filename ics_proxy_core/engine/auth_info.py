"""Validated cloud metadata informs existing permission; it never creates a general offline allowlist."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import islice
from typing import Any

from .policy import DeniedError
from .store import Journal, RecoveryRequiredError, encoded, fingerprint

STATUSES = frozenset({"Accepted", "Blocked", "Expired", "Invalid", "ConcurrentTx"})
DENIAL_FLOOR = "auth_denial_sequence_floor"
DENIAL_RETRY_BATCH = 4


# Case-insensitive lookup never changes the tag actually sent on the OCPP wire or in a historical fact.
def tag_key(tag: str) -> str:
    return fingerprint(tag.casefold())


# Preserve only actual protocol fields; parent metadata cannot itself authorize another child tag.
def validate_info(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not {"status"} <= set(value) <= {"status", "expiryDate", "parentIdTag"}:
        raise DeniedError("cloud authorization metadata has unknown or missing fields")
    if not isinstance(value["status"], str) or value["status"] not in STATUSES:
        raise DeniedError("cloud authorization status is invalid")
    if "parentIdTag" in value and (
        not isinstance(value["parentIdTag"], str)
        or not 1 <= len(value["parentIdTag"]) <= 20
        or any(ord(c) < 32 or ord(c) == 127 for c in value["parentIdTag"])
    ):
        raise DeniedError("cloud parent tag is invalid")
    if "expiryDate" in value:
        expiry(value["expiryDate"])
    return dict(value)


# Require an actual timezone-aware RFC3339 instant, avoiding permissive compact/week-date parsing.
def expiry(value: Any) -> float:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value) is None
    ):
        raise DeniedError("cloud authorization expiry is malformed")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        raise DeniedError("cloud authorization expiry is invalid") from None


@dataclass(frozen=True)
class AuthorizationObservation:
    """The wire sequence orders decisions, and the timestamp supports expiry checks."""

    sequence: int
    observed: float


@dataclass(frozen=True)
class AuthorizationRecord:
    """The actual decision and ordering information retained for one tag."""

    info: str
    observed: float
    sequence: int | None
    query_sequence: int | None = None


class InfoBook:
    """Bounded digest-keyed metadata, separately gated by approved local tags or a single-start grant."""

    # The trial's missing-expiry policy requires a fresh online decision; it does not invent a cloud expiry.
    def __init__(self, journal: Journal, capacity: int = 128, protected_tags: frozenset[str] = frozenset()) -> None:
        self.journal, self.capacity = journal, capacity
        self.protected = frozenset(tag_key(tag) for tag in protected_tags)
        self._pending_denials: dict[str, AuthorizationRecord] = {}
        self._live_queries: set[int] = set()
        self._query_replies: dict[int, int] = {}
        self._unsaved_denial_floor = 0
        self._denial_publish_cursor = 0
        journal.db.execute(
            "CREATE TABLE IF NOT EXISTS auth_info(digest TEXT PRIMARY KEY,info TEXT NOT NULL,observed REAL NOT NULL)"
        )
        journal.db.execute(
            "CREATE TABLE IF NOT EXISTS auth_info_order("
            "digest TEXT PRIMARY KEY REFERENCES auth_info(digest) ON DELETE CASCADE,sequence INTEGER NOT NULL)"
        )
        journal.db.execute(
            "CREATE TABLE IF NOT EXISTS auth_denial_replies("
            "digest TEXT PRIMARY KEY,info TEXT NOT NULL,observed REAL NOT NULL,sequence INTEGER NOT NULL)"
        )
        self._open_pending_queries()
        sequence = journal.db.execute(
            "SELECT COALESCE(MAX(sequence), 0) FROM ("
            "SELECT MAX(sequence) AS sequence FROM auth_info_order UNION ALL "
            "SELECT MAX(sequence) FROM delivery_reply_authorization UNION ALL "
            "SELECT MAX(sequence) FROM auth_denial_replies UNION ALL "
            "SELECT MAX(sequence) FROM auth_pending_queries)"
        ).fetchone()[0]
        # An unsequenced historical refusal could follow any previously saved acknowledgement.
        self._sequence = self._legacy_sequence = max(1, sequence, self._saved_floor()[0])

    # Upgrade the prior one-marker-per-tag journal without losing interrupted lookups.
    def _open_pending_queries(self) -> None:
        db = self.journal.db
        with self.journal.atomic():
            legacy = any(
                row["name"] == "digest" and row["pk"] for row in db.execute("PRAGMA table_info(auth_pending_queries)")
            )
            if legacy:
                db.execute("ALTER TABLE auth_pending_queries RENAME TO legacy_auth_pending_queries")
            db.execute(
                "CREATE TABLE IF NOT EXISTS auth_pending_queries("
                "digest TEXT NOT NULL,sequence INTEGER PRIMARY KEY,must_refresh INTEGER NOT NULL DEFAULT 0)"
            )
            if legacy:
                db.execute(
                    "INSERT INTO auth_pending_queries SELECT digest,sequence,must_refresh "
                    "FROM legacy_auth_pending_queries"
                )
                db.execute("DROP TABLE legacy_auth_pending_queries")
            db.execute("CREATE INDEX IF NOT EXISTS auth_pending_query_digest ON auth_pending_queries(digest)")
            # The previous process could have received a refusal while its journal could no longer record it.
            db.execute(
                "DELETE FROM auth_pending_queries WHERE sequence NOT IN "
                "(SELECT MAX(sequence) FROM auth_pending_queries GROUP BY digest)"
            )
            db.execute("UPDATE auth_pending_queries SET must_refresh=1 WHERE must_refresh=0")

    # Read committed ordering and any changes in the caller's current transaction.
    def _saved_floor(self) -> tuple[int, frozenset[str]]:
        floor = self.journal.get(DENIAL_FLOOR, {"sequence": 0, "protected": sorted(self.protected)})
        if (
            not isinstance(floor, dict)
            or set(floor) != {"sequence", "protected"}
            or type(floor["sequence"]) is not int
            or floor["sequence"] < 0
            or not isinstance(floor["protected"], list)
            or any(not isinstance(digest, str) for digest in floor["protected"])
        ):
            raise RecoveryRequiredError("saved authorization ordering is invalid")
        return floor["sequence"], frozenset(floor["protected"])

    # The caller commits the loss of exact evidence and its conservative sequence bound together.
    def _advance_floor(self, sequence: int) -> None:
        current, protected = self._saved_floor()
        retained = protected & self.protected
        if sequence > current or retained != protected:
            self.journal.put(DENIAL_FLOOR, {"sequence": max(current, sequence), "protected": sorted(retained)})

    # Order actual replies inside the wire gate while keeping expiry checks on wall time.
    def observe(self) -> AuthorizationObservation:
        self._sequence += 1
        return AuthorizationObservation(self._sequence, time.time())

    # Record the uncertainty before sending so even a later storage outage cannot revive older approval on restart.
    def begin_query(self, tag: str, *, hold_open: bool = False) -> tuple[int, bool]:
        key = tag_key(tag)
        j = self.journal
        pending: set[int] | None = None
        with j.atomic():
            prior = j.db.execute(
                "SELECT sequence FROM auth_pending_queries WHERE digest=? ORDER BY sequence", (key,)
            ).fetchall()
            retired = [row[0] for row in prior if row[0] not in self._live_queries]
            j.db.executemany("DELETE FROM auth_pending_queries WHERE sequence=?", ((seq,) for seq in retired[:-1]))
            current = j.get("vendor_start_grant", {}).get("digest")
            if key not in self.protected and key != current:
                outstanding = [
                    row["sequence"]
                    for row in j.db.execute("SELECT digest,sequence FROM auth_pending_queries ORDER BY sequence")
                    if row["digest"] not in self.protected
                    and row["digest"] != current
                    and row["sequence"] not in self._live_queries
                ]
                overflow = len(outstanding) - max(1, self.capacity) + 1
                if overflow > 0:
                    # A durable ordering floor must cover an uncertainty before dropping its exact marker.
                    self._advance_floor(self._sequence)
                    j.db.executemany(
                        "DELETE FROM auth_pending_queries WHERE sequence=?",
                        ((seq,) for seq in outstanding[:overflow]),
                    )
            sequence = self.observe().sequence
            j.db.execute("INSERT INTO auth_pending_queries(digest,sequence) VALUES(?,?)", (key, sequence))
            if self._query_replies:
                pending = {row[0] for row in j.db.execute("SELECT sequence FROM auth_pending_queries")}
        if pending is not None:
            self._query_replies = {query: reply for query, reply in self._query_replies.items() if query in pending}
        if hold_open:
            self._live_queries.add(sequence)
        return sequence, not prior

    # A saved decision settles this attempt and earlier uncertainty. Other live wires can still answer later.
    def _finish_query(self, key: str, sequence: int | None, settled_through: int) -> None:
        if sequence is None:
            return
        db = self.journal.db
        prior = db.execute(
            "SELECT sequence FROM auth_pending_queries WHERE digest=? AND sequence<=?", (key, settled_through)
        )
        resolved = [
            (query,)
            for (query,) in prior
            if query == sequence
            or query not in self._live_queries
            and self._query_replies.get(query, query) <= settled_through
        ]
        db.executemany("DELETE FROM auth_pending_queries WHERE sequence=?", resolved)

    # An unresolved lookup from a previous process requires a new decision for this tag.
    def _query_requires_refresh(self, key: str) -> bool:
        return (
            self.journal.db.execute(
                "SELECT 1 FROM auth_pending_queries WHERE digest=? AND must_refresh=1", (key,)
            ).fetchone()
            is not None
        )

    # Only this wire's marker can disappear without a decision. An unsaved result stays recoverable.
    def close_query(self, sequence: int, decision: AuthorizationObservation | None) -> None:
        self._live_queries.discard(sequence)
        db = self.journal.db
        if decision is None:
            db.execute("DELETE FROM auth_pending_queries WHERE sequence=? AND must_refresh=0", (sequence,))
        else:
            self._query_replies[sequence] = decision.sequence
            if db.execute("SELECT 1 FROM auth_pending_queries WHERE sequence=?", (sequence,)).fetchone() is None:
                del self._query_replies[sequence]

    # Publish a checked authorization reply by when it arrived, returning the latest decision for that tag.
    def put(
        self,
        tag: str,
        value: Any,
        observation: AuthorizationObservation | None = None,
        *,
        query_sequence: int | None = None,
    ) -> dict[str, Any]:
        info = validate_info(value)
        observation = observation or self.observe()
        with nullcontext() if self.journal.db.in_transaction else self.journal.atomic():
            self.put_delivery(tag, info, observation, restored=False)
            if query_sequence is not None:
                key = tag_key(tag)
                saved = self._saved(key)
                if saved is not None and saved.sequence is not None and saved.sequence >= observation.sequence:
                    pending = self._pending_denials.get(key)
                    if pending is None or pending.sequence is not None and pending.sequence <= saved.sequence:
                        self._finish_query(key, query_sequence, saved.sequence)
            latest = self.stored(tag)
        if latest is None:
            raise RecoveryRequiredError("an earlier authorization approval requires a fresh decision")
        return latest

    # A matched denial remains effective even if the older cache row cannot be replaced.
    def deny(
        self,
        tag: str,
        info: dict[str, Any],
        observation: AuthorizationObservation | None = None,
        *,
        query_sequence: int | None = None,
    ) -> dict[str, Any]:
        decision = validate_info(info)
        if decision["status"] == "Accepted":
            raise DeniedError("a positive reply requires the current owner check")
        observation = observation or self.observe()
        key = tag_key(tag)
        if key not in self._pending_denials and len(self._pending_denials) >= max(1, self.capacity) + len(
            self.protected
        ):
            oldest = next(candidate for candidate in self._pending_denials if candidate not in self.protected)
            evicted = self._pending_denials.pop(oldest)
            assert evicted.sequence is not None
            # The exact dynamic refusal can be evicted only while its side of the ordering fence remains blocked.
            self._unsaved_denial_floor = max(self._unsaved_denial_floor, evicted.sequence)
        self._pending_denials[key] = AuthorizationRecord(
            encoded(decision), observation.observed, observation.sequence, query_sequence
        )
        self._save_denial_floor()
        if self._checkpoint_denial(key):
            self._publish_saved_denials(key)
        return decision

    # Retry only a bounded amount of recovery work per supervisor turn, independently of new denials.
    def retry_denials(self) -> None:
        self._save_denial_floor()
        for key in tuple(islice(self._pending_denials, DENIAL_RETRY_BATCH)):
            if not self._checkpoint_denial(key):
                self._pending_denials[key] = self._pending_denials.pop(key)
                break
        self._publish_saved_denials()

    # Checkpoint the active ordering fence once storage allows it, so it survives later process replacement.
    def _save_denial_floor(self) -> None:
        j = self.journal
        if not self._unsaved_denial_floor:
            return
        try:
            with j.atomic():
                self._advance_floor(self._unsaved_denial_floor)
        except sqlite3.Error:
            if j.db.in_transaction:
                raise
            return
        self._unsaved_denial_floor = 0

    # Checkpoint only an actual latest denial. An unavailable journal leaves the known reply in memory.
    def _checkpoint_denial(self, key: str) -> bool:
        j, reply = self.journal, self._pending_denials[key]
        assert reply.sequence is not None
        try:
            with j.atomic():
                prior = self._saved(key)
                if prior is None or prior.sequence is None or prior.sequence < reply.sequence:
                    self._make_room(key)
                    j.db.execute(
                        "INSERT OR REPLACE INTO auth_denial_replies VALUES(?,?,?,?)",
                        (key, reply.info, reply.observed, reply.sequence),
                    )
                self._finish_query(key, reply.query_sequence, reply.sequence)
        except sqlite3.Error, RecoveryRequiredError:
            if j.db.in_transaction:
                raise
            return False
        del self._pending_denials[key]
        return True

    # Move durable refusals into the ordinary cache after either the new reply or periodic storage recovery.
    def _publish_saved_denials(self, key: str | None = None) -> None:
        j = self.journal
        try:
            query = "SELECT digest,info,observed,sequence FROM auth_denial_replies"
            rows = (
                j.db.execute(query + " WHERE digest=?", (key,)).fetchall()
                if key is not None
                else j.db.execute(
                    query + " ORDER BY sequence <= ?, sequence LIMIT ?",
                    (self._denial_publish_cursor, DENIAL_RETRY_BATCH),
                ).fetchall()
            )
        except sqlite3.Error:
            if j.db.in_transaction:
                raise
            return
        for row in rows:
            digest, sequence = row["digest"], row["sequence"]
            if key is None:
                self._denial_publish_cursor = sequence
            try:
                with j.atomic():
                    ordinary = j.db.execute("SELECT sequence FROM auth_info_order WHERE digest=?", (digest,)).fetchone()
                    if ordinary is None or ordinary["sequence"] < sequence:
                        self._put(
                            digest,
                            validate_info(json.loads(row["info"])),
                            AuthorizationObservation(sequence, row["observed"]),
                        )
                    j.db.execute("DELETE FROM auth_denial_replies WHERE digest=? AND sequence=?", (digest, sequence))
            except sqlite3.Error:
                if j.db.in_transaction:
                    raise
                break

    # Keep the newest actual decision, whether it came from Authorize or a saved lifecycle acknowledgement.
    def put_delivery(
        self, tag: str, info: dict[str, Any], observation: AuthorizationObservation | None, *, restored: bool
    ) -> None:
        key = tag_key(tag)
        sequence = None if observation is None else observation.sequence
        if info["status"] == "Accepted" and self._approval_precedes_floor(key, sequence):
            return
        latest = self._latest(key)
        if latest is not None:
            sequence = latest.sequence
            # Old journal rows cannot prove order after replacement, even after a rollback and re-upgrade.
            if observation is None or (sequence is None and restored):
                return
            if sequence is not None and sequence >= observation.sequence:
                return
        self._put(key, info, observation)

    # Old approvals remain eligible only for tags whose exact refusals stayed protected throughout eviction.
    def _approval_precedes_floor(self, key: str, sequence: int | None) -> bool:
        floor, protected = self._saved_floor()
        if self._unsaved_denial_floor:
            floor = max(floor, self._unsaved_denial_floor)
            protected &= self.protected
        return floor > 0 and key not in protected and (sequence is None or sequence <= floor)

    # The gateway owns local eligibility; the recorded order can still block a newly configured tag's fallback.
    def permits_local_fallback(self, tag: str) -> bool:
        key = tag_key(tag)
        return not self._query_requires_refresh(key) and not self._approval_precedes_floor(key, None)

    # Keep the same bound across both ordinary decisions and independent refusal receipts.
    def _make_room(self, key: str) -> None:
        j = self.journal
        keys = [
            row[0]
            for row in j.db.execute(
                "SELECT digest FROM (SELECT digest,observed FROM auth_info UNION ALL "
                "SELECT digest,observed FROM auth_denial_replies) GROUP BY digest ORDER BY max(observed)"
            )
        ]
        if key in keys or len(keys) < self.capacity:
            return
        current = j.get("vendor_start_grant", {}).get("digest")
        oldest = next(
            (
                item
                for item in keys
                if item not in self.protected and item != current and item not in self._pending_denials
            ),
            None,
        )
        if oldest is None:
            raise RecoveryRequiredError("authorization metadata capacity reached without an evictable historical tag")
        decision = self._saved(oldest)
        assert decision is not None
        if validate_info(json.loads(decision.info))["status"] != "Accepted":
            self._advance_floor(self._legacy_sequence if decision.sequence is None else decision.sequence)
        j.db.execute("DELETE FROM auth_info WHERE digest=?", (oldest,))
        j.db.execute("DELETE FROM auth_denial_replies WHERE digest=?", (oldest,))

    # Persist the metadata and its order in the same transaction.
    def _put(self, key: str, info: dict[str, Any], observation: AuthorizationObservation | None) -> None:
        self._make_room(key)
        observed = observation.observed if observation is not None else time.time()
        self.journal.db.execute("INSERT OR REPLACE INTO auth_info VALUES(?,?,?)", (key, encoded(info), observed))
        if observation is not None:
            self.journal.db.execute("INSERT INTO auth_info_order VALUES(?,?)", (key, observation.sequence))
            self.journal.db.execute(
                "DELETE FROM auth_denial_replies WHERE digest=? AND sequence<?", (key, observation.sequence)
            )

    # Both kinds of durable reply use the same wire order and preserve legacy entries with no sequence.
    def _saved(self, key: str) -> AuthorizationRecord | None:
        row = self.journal.db.execute(
            "SELECT info,observed,sequence FROM auth_info LEFT JOIN auth_info_order USING(digest) WHERE digest=? "
            "UNION ALL SELECT info,observed,sequence FROM auth_denial_replies WHERE digest=? "
            "ORDER BY sequence DESC LIMIT 1",
            (key, key),
        ).fetchone()
        return AuthorizationRecord(row["info"], row["observed"], row["sequence"]) if row else None

    # A known newer refusal supersedes the earlier cache as soon as the wire reply arrives.
    def _latest(self, key: str) -> AuthorizationRecord | None:
        saved, pending = self._saved(key), self._pending_denials.get(key)
        if pending is not None:
            assert pending.sequence is not None
            if saved is None or saved.sequence is None or pending.sequence > saved.sequence:
                return pending
        return saved

    # Only metadata from an actual correlated response can be returned; an expired decision stays expired.
    def stored(self, tag: str) -> dict[str, Any] | None:
        key = tag_key(tag)
        latest = self._latest(key)
        if latest is None:
            return None
        info = validate_info(json.loads(latest.info))
        if info["status"] == "Accepted" and (
            self._query_requires_refresh(key) or self._approval_precedes_floor(key, latest.sequence)
        ):
            return None
        return info

    # Apply the actual expiry at the moment of use, without losing the original cloud decision.
    def get(self, tag: str) -> dict[str, Any] | None:
        info = self.stored(tag)
        if info is None:
            return None
        if info["status"] == "Accepted" and "expiryDate" in info and expiry(info["expiryDate"]) <= time.time():
            return {**info, "status": "Expired"}
        return info

    # Return the original decision and expiry for an already acknowledged event that occurred while both were valid.
    def accepted_for_delayed_report(self, tag: str, occurred: float, reported: str) -> dict[str, Any] | None:
        key = tag_key(tag)
        record = self._latest(key)
        if record is None or self._query_requires_refresh(key) or self._approval_precedes_floor(key, record.sequence):
            return None
        info = validate_info(json.loads(record.info))
        if info["status"] != "Accepted" or "expiryDate" not in info:
            return None
        end = expiry(info["expiryDate"])
        clock = re.search(r"T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:[.,]([0-9]+))?(?:Z|[+-][0-9]{2}:[0-9]{2})$", reported)
        precision = 10.0 ** -min(6, len(clock[1] or "")) if clock else 0.0
        if (record.observed <= occurred or record.observed - occurred < precision) and occurred < end <= time.time():
            return info
        return None

    # The narrow dynamic-tag trial requires real future cloud expiry before arming a new remote start.
    def require_dynamic(self, info: dict[str, Any]) -> None:
        if info.get("status") != "Accepted" or "expiryDate" not in info or expiry(info["expiryDate"]) <= time.time():
            raise RecoveryRequiredError("dynamic trial start needs actual Accepted metadata with future expiry")

    # Local owner authorization has its own explicit bounded lifetime and is never described as cloud metadata.
    def local(self, seconds: int = 60) -> dict[str, Any]:
        instant = datetime.fromtimestamp(time.time() + seconds, UTC).isoformat().replace("+00:00", "Z")
        return {"status": "Accepted", "expiryDate": instant}
