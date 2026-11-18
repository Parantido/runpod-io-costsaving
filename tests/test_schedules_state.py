import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from runpod_ops.state import StateStore


def _store(tmp_path):
    store = StateStore(tmp_path / "state.db")
    store.init_schema()
    return store


def test_create_and_read_schedule(tmp_path):
    store = _store(tmp_path)
    schedule_id = store.create_schedule(
        "riva-02-up",
        "riva-02",
        "recreate",
        "15 12 * * 1-6",
        post_hooks=[["/app/hooks/register.sh", "--pod", "riva-02"]],
    )
    schedule = store.get_schedule(schedule_id)
    assert schedule["name"] == "riva-02-up"
    assert schedule["pod"] == "riva-02"
    assert schedule["action"] == "recreate"
    assert schedule["cron"] == "15 12 * * 1-6"
    assert schedule["timezone"] == "Etc/UTC"
    assert schedule["trigger"] == "external"
    assert schedule["enabled"] is False
    assert schedule["post_hooks"] == [["/app/hooks/register.sh", "--pod", "riva-02"]]
    assert store.get_schedule_by_name("riva-02-up")["id"] == schedule_id


def test_create_duplicate_name_rejected(tmp_path):
    store = _store(tmp_path)
    store.create_schedule("dup", "riva", "start", "* * * * *")
    with pytest.raises(Exception):
        store.create_schedule("dup", "riva", "stop", "* * * * *")


def test_update_schedule_fields(tmp_path):
    store = _store(tmp_path)
    schedule_id = store.create_schedule("s1", "riva", "start", "0 1 * * *")
    store.update_schedule(
        schedule_id,
        action="stop",
        cron="30 4 * * *",
        trigger="app",
        enabled=True,
        pre_hooks=["/app/hooks/remove.sh"],
    )
    schedule = store.get_schedule(schedule_id)
    assert schedule["action"] == "stop"
    assert schedule["cron"] == "30 4 * * *"
    assert schedule["trigger"] == "app"
    assert schedule["enabled"] is True
    assert schedule["pre_hooks"] == ["/app/hooks/remove.sh"]


def test_delete_schedule(tmp_path):
    store = _store(tmp_path)
    schedule_id = store.create_schedule("s1", "riva", "start", "0 1 * * *")
    store.delete_schedule(schedule_id)
    assert store.get_schedule(schedule_id) is None
    assert store.list_schedules() == []


def test_list_schedules_sorted(tmp_path):
    store = _store(tmp_path)
    store.create_schedule("zulu", "riva", "start", "0 1 * * *")
    store.create_schedule("alpha", "riva", "start", "0 1 * * *")
    assert [item["name"] for item in store.list_schedules()] == ["alpha", "zulu"]


def test_record_schedule_run(tmp_path):
    store = _store(tmp_path)
    schedule_id = store.create_schedule("s1", "riva", "start", "0 1 * * *")
    store.record_schedule_run(schedule_id, "job-1", "succeeded", "2026-11-18T01:00:00+00:00")
    schedule = store.get_schedule(schedule_id)
    assert schedule["last_job_id"] == "job-1"
    assert schedule["last_status"] == "succeeded"
    assert schedule["last_run_at"]
    assert schedule["next_run_at"] == "2026-11-18T01:00:00+00:00"
