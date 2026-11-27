# Modified for the standalone iCS proxy repository.
"""Persisted retry waits remain bounded across wall-clock corrections and process-local clock replacement."""

from types import SimpleNamespace

from ics_proxy_core.engine import managed_start_delivery as delivery
from ics_proxy_core.engine.store import Journal
from tests.prototype_rig import START


# Intent: wall jumps cannot erase an in-process wait or turn a restarted one-minute backoff into a day-long hold.
# Why: a valid calendar correction must preserve bounded progress without resetting the explicit error budget.
def test_start_retry_clock_corrections_remain_bounded(tmp_path, monkeypatch):
    clock = {"wall": 1000.0, "elapsed": 50.0}
    monkeypatch.setattr(
        delivery, "time", SimpleNamespace(time=lambda: clock["wall"], monotonic=lambda: clock["elapsed"])
    )
    j = Journal(tmp_path / "clock.db", "RETRY_CLOCK")
    try:
        j.start("first", START)
        item = j.next_delivery()
        j.mark_sent(item)
        waits = {}
        assert not delivery.record_start_error(j, item, [4, item.token, "InternalError", "synthetic", {}], waits=waits)
        item = j.next_delivery()
        clock.update(wall=100000.0, elapsed=60.0)
        assert delivery.retry_deferred(j, item, waits)
        clock.update(wall=-100000.0, elapsed=109.0)
        assert delivery.retry_deferred(j, item, waits)
        clock["elapsed"] = 110.0
        assert not delivery.retry_deferred(j, item, waits)
        restarted = {}
        assert delivery.retry_deferred(j, item, restarted)
        assert restarted[item.token][1] == 170.0
        clock["elapsed"] = 170.0
        assert not delivery.retry_deferred(j, item, restarted)
        assert j.get(delivery.RETRIES)[item.token]["errors"] == 1
    finally:
        j.close()
