"""Independent projection checks for the current-resolution fast review cases."""

from collections import Counter
from itertools import permutations, product

from tests_model.control_cases import CurrentCase, current_candidates, reviewed_current_resolution_cases

ENDPOINT_PAIRS = ((6, 32), (32, 6))
SENTINEL_PAIRS = ((6, 16), (16, 6), (16, 32), (32, 16))
PROOF_OUTCOMES = {
    "native-before": ("valid", "503", "eof", "bad-json"),
    "native-after": ("valid", "503", "eof", "bad-json"),
    "paused-native": ("valid", "503", "eof", "bad-json"),
    "profile": ("valid", "rejected", "call-error"),
    "composite": ("valid", "rejected", "call-error", "bad-schedule"),
}
EVENTS = ("resolve", "pause", "boot")
ORDERS = tuple(permutations(EVENTS))
DEMAND_STATES = (False, True)


# Intent: release retains the full input product and fast review uses exactly 248 of its original cases in order.
# Why: a smaller CI subset must preserve the independently declared release denominator and normal case identities.
def test_current_review_preserves_full_release_product():
    full, reviewed = current_candidates(), reviewed_current_resolution_cases()
    expected = {
        CurrentCase(initial, target, boundary, outcome, demand, order)
        for initial, target in (*ENDPOINT_PAIRS, *SENTINEL_PAIRS)
        for boundary, outcomes in PROOF_OUTCOMES.items()
        for outcome, demand, order in product(outcomes, DEMAND_STATES, ORDERS)
    }
    assert len(full) == len({case.identifier for case in full}) == 1368
    assert set(full) == expected
    assert len(reviewed) == len({case.identifier for case in reviewed}) == 248
    selected = set(reviewed)
    assert reviewed == tuple(case for case in full if case in selected)
    assert reviewed == reviewed_current_resolution_cases()


# Intent: each exact endpoint variant and order runs once, with both demand states on the shared projections.
# Why: a demand bit tied to one direction, proof or leading event would hide regressions despite the same count.
def test_current_review_covers_endpoint_variants_and_demand_projections():
    endpoints = tuple(case for case in reviewed_current_resolution_cases() if 16 not in (case.initial, case.target))
    variants = {(boundary, outcome) for boundary, outcomes in PROOF_OUTCOMES.items() for outcome in outcomes}
    assert len(endpoints) == 228
    assert Counter(((c.initial, c.target), (c.boundary, c.outcome), c.events) for c in endpoints) == dict.fromkeys(
        product(ENDPOINT_PAIRS, variants, ORDERS), 1
    )
    assert {((c.initial, c.target), (c.boundary, c.outcome), c.events[0], c.demand) for c in endpoints} == set(
        product(ENDPOINT_PAIRS, variants, EVENTS, DEMAND_STATES)
    )
    assert {((c.initial, c.target), c.events, c.demand) for c in endpoints} == set(
        product(ENDPOINT_PAIRS, ORDERS, DEMAND_STATES)
    )
    assert {((c.boundary, c.outcome), c.events, c.demand) for c in endpoints} == set(
        product(variants, ORDERS, DEMAND_STATES)
    )


# Intent: one valid sentinel runs at every boundary for each of the four directed pairs that touch 16 A.
# Why: interior transitions must exercise both demand states and real resolution, Pause and Boot precedence.
def test_current_review_covers_all_interior_sentinels():
    sentinels = tuple(case for case in reviewed_current_resolution_cases() if 16 in (case.initial, case.target))
    assert len(sentinels) == 20
    assert all(case.outcome == "valid" for case in sentinels)
    assert Counter(((c.initial, c.target), c.boundary) for c in sentinels) == dict.fromkeys(
        product(SENTINEL_PAIRS, PROOF_OUTCOMES), 1
    )
    assert {((c.initial, c.target), c.events[0]) for c in sentinels} == set(product(SENTINEL_PAIRS, EVENTS))
    assert {(c.boundary, c.events[0]) for c in sentinels} == set(product(PROOF_OUTCOMES, EVENTS))
    assert {((c.initial, c.target), c.demand) for c in sentinels} == set(product(SENTINEL_PAIRS, DEMAND_STATES))
    assert {(c.boundary, c.demand) for c in sentinels} == set(product(PROOF_OUTCOMES, DEMAND_STATES))
    assert {(c.events, c.demand) for c in sentinels} == set(product(ORDERS, DEMAND_STATES))
