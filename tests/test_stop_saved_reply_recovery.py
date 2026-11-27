# Modified for the standalone iCS proxy repository.
"""Offline Stop retry must not erase a durable response awaiting ordinary settlement."""

import time
from contextlib import closing
from dataclasses import replace

import pytest

from ics_proxy_core import commands
from ics_proxy_core.engine.command_outcomes import CommandOutcomes
from ics_proxy_core.engine.delivery_reply import DeliveryReply
from ics_proxy_core.engine.managed_debt_recovery import debt_candidate
from ics_proxy_core.engine.managed_intent import ManagedRequests
from ics_proxy_core.engine.managed_recovery_zero import require_original_zero
from ics_proxy_core.engine.native_bounded import capture_native_bounded_baseline
from ics_proxy_core.engine.stop_settlement import StopAttempt, StopPredecessors, predecessor_stop_reply
from ics_proxy_core.engine.store import Journal, RecoveryRequiredError
from ics_proxy_core.engine.trial_allocation import initialize_allocation, observe_history
from ics_proxy_core.engine.trial_evidence import Resource, profile_projection
from ics_proxy_core.engine.trial_resources import ResourceBook
from tests.prototype_rig import START
from tests.test_native_bounded import BOOT, HARDWARE, IDENTITY, native, spec


# Establish the complete original-zero proof and exact mapped closed lifecycle using production constructors.
def saved_stop(path, reply):
    policy = spec()
    original = native("original", 32)
    baseline = capture_native_bounded_baseline(original, IDENTITY, HARDWARE)
    j = Journal(path, IDENTITY)
    book = ResourceBook(j, baseline)
    initialize_allocation(
        j, observe_history(list(original.history), IDENTITY, HARDWARE, time.time()), IDENTITY, HARDWARE
    )
    ManagedRequests(j)
    j.put("trial_phase", "active")
    j.put("boot", BOOT)
    j.put("managed_mode", {"mode": "app_schedule", "spec_digest": policy.digest, "local_start": False})
    j.put(
        "native_bounded_handover",
        {"stage": "admitted", "spec": policy.value(), "original_history": list(original.history)},
    )
    j.put("managed_control_intent", {"mode": "paused", "revision": 1})
    j.put("control", {"owner": "local", "inhibit": True, "ticket": "synthetic-closed-zero", "resume_limit": 6})
    j.put(
        "trial_binding",
        {"configuration_digest": native().physical.configuration_digest, "boot_digest": policy.boot_digest},
    )
    profile = commands.set_charging_profile(0, "ChargePointMaxProfile", 0, 7001)
    book.before_send("SetChargingProfile", profile)
    book.after_result("SetChargingProfile", profile, [3, "synthetic-reply", {"status": "Accepted"}])
    canonical = profile_projection(profile)
    resource = Resource("profile", 7001, {"independent-synthetic-zero-resource": True}, canonical)
    observed = native()
    observed = replace(
        observed, physical=replace(observed.physical, resources=observed.physical.resources + (resource,))
    )
    local = j.start("synthetic-start", START)["transactionId"]
    j.stop("synthetic-stop", {"transactionId": local, "meterStop": 1100, "timestamp": "2026-01-01T00:01:00Z"})
    head = j.next_delivery()
    j.mark_sent(head)
    j.acknowledge(j.next_delivery(), {"transactionId": 9001, "idTagInfo": {"status": "Accepted"}})
    j.put("managed_control_fault", {"classification": "owner_control_or_delivery_uncertain", "at_epoch": time.time()})
    stop = j.next_delivery()
    j.mark_sent(stop)
    stop = j.next_delivery()
    answer = {
        "ack": [3, stop.token, {}],
        "error": [4, stop.token, "InternalError", "synthetic cloud failure", {}],
        "invalid_authorization": [3, stop.token, {"idTagInfo": {"status": "Expired"}}],
        "invalid_shape": [3, stop.token, {"status": "Rejected"}],
    }[reply]
    DeliveryReply.received(stop, answer).save(j, stop)
    return j, baseline, policy, observed, stop.token


# Intent: every saved correlated Stop outcome blocks operator retry both before and after a journal replacement.
# Why: changing inflight to queued deletes the response, which ordinary settlement must consume first.
@pytest.mark.parametrize("reply", ["ack", "error", "invalid_authorization"])
@pytest.mark.parametrize("reopen", [False, True])
def test_saved_stop_reply_cannot_become_no_reply_candidate(tmp_path, reply, reopen):
    path = tmp_path / "journal.db"
    j, baseline, policy, observed, token = saved_stop(path, reply)
    if reopen:
        j.close()
        j = Journal(path, IDENTITY)
    try:
        book = ResourceBook(j, baseline)
        require_original_zero(j, book, policy, observed)
        before = list(j.db.iterdump())
        assert j.next_delivery().state == "inflight"
        with pytest.raises(RecoveryRequiredError, match="ordinary settlement of its saved reply"):
            debt_candidate(j, book, policy, observed, "stop-delivery")
        assert list(j.db.iterdump()) == before
        assert j.db.execute("SELECT token FROM delivery_replies").fetchone()[0] == token
    finally:
        j.close()


# Intent: a saved Stop reply with invalid top-level fields cannot be classified as only a local storage failure.
# Why: process replacement must distinguish a valid cloud ACK from a correlated but invalid result.
def test_invalid_saved_stop_result_is_not_storage_only(tmp_path):
    path = tmp_path / "journal.db"
    j, _, _, _, _ = saved_stop(path, "invalid_shape")
    j.close()
    j = Journal(path, IDENTITY)
    try:
        reply = DeliveryReply.restore(j, j.next_delivery())
        assert reply is not None and not reply.storage_only(j)
    finally:
        j.close()


# Intent: both Stop readers recognize the same exact Accepted reply before its primary write is restored.
# Why: a reopen may leave the original charger reply available only in a validated retained checkpoint.
def test_retained_forwarded_stop_is_visible_to_both_reply_readers(tmp_path):
    path = tmp_path / "forwarded-stop.sqlite"
    action, wire = "RemoteStopTransaction", "charger-stop-wire"
    with closing(Journal(path, IDENTITY)) as journal:
        local = journal.start("original-start", START)["transactionId"]
        mutation = journal.mutation_intent("vendor", action, {"transactionId": 42})
        journal.bind_vendor_command(
            mutation, request="vendor-stop", vendor_epoch=1, boot="charger-boot", wire=wire, local=local, profile=None
        )
        journal.record_vendor_result(mutation, "dispatched")
        journal.db.execute(
            "CREATE TRIGGER refuse_stop_reply BEFORE UPDATE OF wire_result ON vendor_command_scope "
            "WHEN NEW.wire_result='Accepted' BEGIN SELECT RAISE(ABORT, 'reply write unavailable'); END"
        )
        reply = journal.outcomes.capture(mutation, action, wire, [3, wire, {"status": "Accepted"}])
        assert reply is not None and not journal.outcomes.publish(reply)
        expected = StopAttempt("vendor", mutation, wire)
        earlier = StopPredecessors(local, vendor_mutations=(mutation,))
        assert journal.outcomes.accepted_stop_attempt(local) == expected
        assert predecessor_stop_reply(journal.outcomes, local, earlier) == "Accepted"

    with closing(Journal(path, IDENTITY)) as journal:
        # An evidence reader on the reopened journal sees only what was saved, before promotion can succeed.
        outcomes = CommandOutcomes(journal)
        command = outcomes.vendor(mutation)
        assert command is not None and command.wire_result == "dispatched" and not command.saving_reply
        assert predecessor_stop_reply(outcomes, local, earlier) == "Accepted"
        assert outcomes.accepted_stop_attempt(local) == expected
        assert outcomes.accepted_stop_attempt(local + 1) is None
        wrong_session = StopPredecessors(local + 1, vendor_mutations=(mutation,))
        assert predecessor_stop_reply(outcomes, local + 1, wrong_session) is None
        assert not outcomes.stop_report_closed(local)

        journal.db.execute("DROP TRIGGER refuse_stop_reply")
        journal.outcomes.retry()
        promoted = outcomes.vendor(mutation)
        assert promoted is not None and promoted.wire_result == "Accepted"
        assert outcomes.accepted_stop_attempt(local) == expected
        assert predecessor_stop_reply(outcomes, local, earlier) == "Accepted"
