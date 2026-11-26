"""Relate independent native closure evidence to the charger transaction journal."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .trial_handover_evidence import HandoverEvidence


class ClosurePosition(Enum):
    """The two observations can arrive in either order."""

    OPEN = auto()
    IDLE = auto()
    NATIVE_FIRST = auto()
    REPORT_FIRST = auto()
    UNVERIFIED = auto()


@dataclass(frozen=True, slots=True)
class ClosingReport:
    """The original closing report for an allocated local session."""

    local: int
    body: str

    @classmethod
    def from_record(cls, local: object, body: object) -> ClosingReport | None:
        if type(local) is int and 0 < local < 2**63 and isinstance(body, str):
            return cls(local, body)
        return None


@dataclass(frozen=True, slots=True)
class ClosureObservation:
    """A transaction comparison, independent of wire replies and station limits."""

    position: ClosurePosition
    fresh: bool

    @property
    def confirms_idle(self) -> bool:
        return self.fresh and self.position is ClosurePosition.IDLE

    @property
    def matches_journal(self) -> bool:
        return self.fresh and self.position in (ClosurePosition.OPEN, ClosurePosition.IDLE)


def observe_closure(active: int | None, native: HandoverEvidence) -> ClosureObservation:
    """Compare the native session and measured draw with the journal's active ID."""
    physical = native.physical
    local = physical.native_transaction_id
    fresh = native.is_fresh(5)
    if physical.transaction_open and local is not None and local == active:
        return ClosureObservation(ClosurePosition.OPEN, fresh)

    if any((physical.power_w, *native.currents_a, *native.powers_w)):
        return ClosureObservation(ClosurePosition.UNVERIFIED, fresh)
    if not physical.transaction_open and local is None:
        return ClosureObservation(ClosurePosition.IDLE if active is None else ClosurePosition.NATIVE_FIRST, fresh)
    if physical.transaction_open and local is not None and active is None:
        return ClosureObservation(ClosurePosition.REPORT_FIRST, fresh)
    return ClosureObservation(ClosurePosition.UNVERIFIED, fresh)
