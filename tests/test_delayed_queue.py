from __future__ import annotations

from time import time

import pytest

from watchdog.utils.delayed_queue import DelayedQueue


@pytest.mark.flaky(max_runs=5, min_passes=1)
def test_delayed_get():
    q = DelayedQueue[str](2)
    q.put("", delay=True)
    inserted = time()
    q.get()
    elapsed = time() - inserted
    # 2.10 instead of 2.05 for slow macOS slaves on Travis
    assert 2.10 > elapsed > 1.99


@pytest.mark.flaky(max_runs=5, min_passes=1)
def test_nondelayed_get():
    q = DelayedQueue[str](2)
    q.put("")
    inserted = time()
    q.get()
    elapsed = time() - inserted
    # Far less than 1 second
    assert elapsed < 1


def test_delayed_queue_find_and_remove():
    q = DelayedQueue[str](2)
    q.put("apple", delay=True)
    q.put("banana", delay=True)
    q.put("cherry", delay=True)

    # Test find
    assert q.find(lambda x: x == "banana") == "banana"
    assert q.find(lambda x: x == "orange") is None

    # Test remove
    removed = q.remove(lambda x: x == "banana")
    assert removed == "banana"
    assert q.find(lambda x: x == "banana") is None

    # Verify remaining
    assert q.find(lambda x: x == "cherry") == "cherry"
    q.close()
    assert q.get() is None
