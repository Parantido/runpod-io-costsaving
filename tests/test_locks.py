import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from runpod_ops.locks import PodLockManager


def test_lock_is_exclusive_across_managers(tmp_path):
    first = PodLockManager(tmp_path)
    second = PodLockManager(tmp_path)

    assert first.acquire("riva") is True
    assert second.acquire("riva") is False

    first.release("riva")
    assert second.acquire("riva") is True
    second.release("riva")


def test_same_manager_rejects_double_acquire(tmp_path):
    locks = PodLockManager(tmp_path)
    assert locks.acquire("riva") is True
    assert locks.acquire("riva") is False
    locks.release("riva")
    assert locks.acquire("riva") is True


def test_different_pods_do_not_block_each_other(tmp_path):
    locks = PodLockManager(tmp_path)
    assert locks.acquire("one") is True
    assert locks.acquire("two") is True
    locks.release("one")
    locks.release("two")
