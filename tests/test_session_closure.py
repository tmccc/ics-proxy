"""Exercise closure interpretation independently of command replies."""

import time

import pytest

from ics_proxy_core.engine.session_closure import ClosurePosition, observe_closure
from ics_proxy_core.engine.trial_evidence import Evidence
from ics_proxy_core.engine.trial_handover_evidence import HandoverEvidence


# Intent: classify journal and native session agreement across closure orderings and draw readings.
# Why: only fresh, consistent observations can verify the session state.
@pytest.mark.parametrize(
    ("active", "native_id", "native_open", "draw", "position"),
    [
        (None, None, False, None, ClosurePosition.IDLE),
        (10, None, False, None, ClosurePosition.NATIVE_FIRST),
        (None, 10, True, None, ClosurePosition.REPORT_FIRST),
        (10, 10, True, "current", ClosurePosition.OPEN),
        (11, 10, True, None, ClosurePosition.UNVERIFIED),
        (None, None, True, None, ClosurePosition.UNVERIFIED),
        (None, 10, False, None, ClosurePosition.UNVERIFIED),
        (None, None, False, "total", ClosurePosition.UNVERIFIED),
        (None, None, False, "current", ClosurePosition.UNVERIFIED),
        (None, None, False, "phase", ClosurePosition.UNVERIFIED),
        (10, None, False, "phase", ClosurePosition.UNVERIFIED),
        (None, 10, True, "current", ClosurePosition.UNVERIFIED),
    ],
)
@pytest.mark.parametrize("fresh", [True, False])
def test_session_closure_requires_agreed_fresh_observations(active, native_id, native_open, draw, position, fresh):
    age = 0 if fresh else 6
    physical = Evidence(
        "identity",
        time.time() - age,
        "C" if native_open else "B",
        "Plug",
        0,
        float(draw == "total"),
        native_open,
        "config",
        (),
        native_transaction_id=native_id,
    )
    native = HandoverEvidence(
        physical,
        "hardware",
        {},
        32,
        32,
        32,
        6,
        (float(draw == "current"), 0, 0),
        (float(draw == "phase"), 0, 0, 0),
        (),
        offered_current_a=6,
        observed_monotonic=time.monotonic() - age,
    )

    observed = observe_closure(active, native)

    assert observed.position is position
    assert observed.fresh is fresh
    assert observed.confirms_idle is (fresh and position is ClosurePosition.IDLE)
    assert observed.matches_journal is (fresh and position in (ClosurePosition.IDLE, ClosurePosition.OPEN))
