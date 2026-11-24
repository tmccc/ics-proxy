"""Shared evidence for settling the exact attempts behind a session Stop request."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from .store import encoded

if TYPE_CHECKING:
    from .store import Journal

STOP_SESSION = "stop_session"
STOP_NONTERMINAL_CLASSIFICATIONS = ("stop_not_confirmed", "process_replaced")
STOP_CLOSURE_VERIFIED = "json_type(result,'$._session_stop.closure_verified') IS 'true'"
STOP_CLOSURE_FIELDS = "result=json_set(result,'$._session_stop.closure_verified',json('true'))"
STOP_COMPLETION_FIELDS = (
    "state='completed',result=json_set(json_remove(result,'$.classification'),"
    "'$.session_stopped',json('true'),'$._session_stop.closure_verified',json('true'))"
)
STOP_BUFFERED_CLOSURE = (
    "EXISTS (SELECT 1 FROM json_each(?) proof WHERE proof.key=managed_requests.id "
    "AND proof.value=json_extract(managed_requests.result,'$._session_stop.local'))"
)
_OPEN_REPLY = (
    "EXISTS (SELECT 1 FROM json_each(?) windows "
    "WHERE json_extract(windows.value,'$[2]')=json_extract(managed_requests.result,'$._session_stop.local') AND ("
    "(json_extract(windows.value,'$[0]')='local' AND (json_extract(windows.value,'$[1]')=managed_requests.id "
    "OR EXISTS (SELECT 1 FROM json_each(managed_requests.result,'$._session_stop.local_attempts') a "
    "WHERE a.value=json_extract(windows.value,'$[1]')))) OR "
    "(json_extract(windows.value,'$[0]')='vendor' AND EXISTS ("
    "SELECT 1 FROM json_each(managed_requests.result,'$._session_stop.vendor_attempts') a "
    "WHERE a.value=json_extract(windows.value,'$[1]')))))"
)
_OWNER_REJECTIONS = (
    "SELECT request,local FROM owner_command_wires WHERE action='RemoteStopTransaction' AND reply='Rejected'"
)
_VENDOR_STOP_SCOPE = (
    "SELECT v.mutation,v.local FROM vendor_command_scope v JOIN mutations m ON m.token=v.mutation "
    "WHERE m.origin='vendor' AND m.action='RemoteStopTransaction'"
)
_VENDOR_REJECTIONS = f"{_VENDOR_STOP_SCOPE} AND v.wire_result='Rejected'"
_EFFECTIVE_REJECTIONS = (
    f"WITH owner_rejections(request,local) AS ({_OWNER_REJECTIONS} UNION ALL "
    "SELECT json_extract(value,'$[0]'),json_extract(value,'$[1]') FROM json_each(?)), "
    f"vendor_rejections(mutation,local) AS ({_VENDOR_REJECTIONS} UNION ALL {_VENDOR_STOP_SCOPE} "
    "AND EXISTS (SELECT 1 FROM json_each(?) WHERE json_extract(value,'$[0]')=v.mutation "
    "AND json_extract(value,'$[1]')=v.wire_request)) "
)


@dataclass(frozen=True)
class StopAcceptance:
    """The exact accepted predecessor a joining request used."""

    origin: Literal["local", "vendor"]
    identifier: str
    wire: str | None


# Retain only reply windows that still belong to this receipt or one of its joined Stop attempts.
def stop_reply_window_predicate(journal: Journal, wires: tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
    attempts = journal.outcomes.open_stop_replies(wires) if wires else ()
    return (_OPEN_REPLY, (encoded(attempts),)) if attempts else ("0", ())


@dataclass(frozen=True)
class StopRejections:
    """The same saved and retained replies feed send decisions and receipt publication."""

    prefix: str = ""
    bindings: tuple[str, ...] = ()
    owner: str = f"({_OWNER_REJECTIONS})"
    vendor: str = f"({_VENDOR_REJECTIONS})"

    # A refusal must name both the request and the exact session that the request could stop.
    def owner_refusal(self, request: str, local: str) -> str:
        return f"EXISTS (SELECT 1 FROM {self.owner} w WHERE w.request={request} AND w.local={local})"

    # A vendor refusal belongs to the recorded mutation and its translated local session.
    def vendor_refusal(self, mutation: str, local: str) -> str:
        return f"EXISTS (SELECT 1 FROM {self.vendor} v WHERE v.mutation={mutation} AND v.local={local})"

    # A direct refusal suffices. Every attempt named by a joined request must have refused it.
    @property
    def receipts(self) -> str:
        local = "json_extract(managed_requests.result,'$._session_stop.local')"
        return (
            f"({self.owner_refusal('managed_requests.id', local)} OR "
            "COALESCE(json_array_length(result,'$._session_stop.local_attempts') "
            "+ json_array_length(result,'$._session_stop.vendor_attempts'),0) > 0 "
            "AND NOT EXISTS (SELECT 1 FROM json_each(result,'$._session_stop.local_attempts') a "
            f"WHERE NOT {self.owner_refusal('a.value', local)}) "
            "AND NOT EXISTS (SELECT 1 FROM json_each(result,'$._session_stop.vendor_attempts') a "
            f"WHERE NOT {self.vendor_refusal('a.value', local)}))"
        )

    # A genuine closure settles the waiting request when its exact Stop attempts still permit it.
    @property
    def closed_receipts(self) -> str:
        return (
            f"action=? AND state IN ('accepted','running','uncertain') AND NOT {self.receipts} AND EXISTS ("
            "SELECT 1 FROM transactions WHERE local=json_extract(managed_requests.result,'$._session_stop.local') "
            "AND stop IS NOT NULL)"
        )


SAVED_STOP_REJECTIONS = StopRejections()


# Use retained replies until their original wire rows are available to the same selector.
def stop_rejections(journal: Journal) -> StopRejections:
    owner, vendor = journal.outcomes.buffered_stop_rejections()
    if not owner and not vendor:
        return SAVED_STOP_REJECTIONS
    return StopRejections(
        _EFFECTIVE_REJECTIONS, (encoded(owner), encoded(vendor)), "owner_rejections", "vendor_rejections"
    )
