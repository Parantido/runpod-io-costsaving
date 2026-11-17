import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from runpod_ops.state import StateStore


def _store(tmp_path):
    store = StateStore(tmp_path / "state.db")
    store.init_schema()
    return store


def test_pod_upsert_and_read(tmp_path):
    store = _store(tmp_path)
    store.upsert_pod("riva", "pod-1", "RUNNING")
    pod = store.get_pod("riva")
    assert pod["pod_id"] == "pod-1"
    assert pod["status"] == "RUNNING"

    store.upsert_pod("riva", "pod-2", "STOPPED")
    pod = store.get_pod("riva")
    assert pod["pod_id"] == "pod-2"
    assert pod["status"] == "STOPPED"
    assert store.get_pod("missing") is None


def test_list_pods_sorted(tmp_path):
    store = _store(tmp_path)
    store.upsert_pod("zulu", "p1", "RUNNING")
    store.upsert_pod("alpha", "p2", "STOPPED")
    assert [pod["name"] for pod in store.list_pods()] == ["alpha", "zulu"]


def test_job_lifecycle(tmp_path):
    store = _store(tmp_path)
    job_id = store.create_job("riva", "start", "python -m engine")
    job = store.get_job(job_id)
    assert job["status"] == "queued"
    assert job["command"] == "python -m engine"

    store.start_job(job_id)
    assert store.get_job(job_id)["status"] == "running"

    store.finish_job(job_id, "succeeded", 0)
    job = store.get_job(job_id)
    assert job["status"] == "succeeded"
    assert job["exit_code"] == 0
    assert job["finished_at"]


def test_logs_ordered_and_after_seq(tmp_path):
    store = _store(tmp_path)
    job_id = store.create_job("riva", "start", "cmd")
    store.add_log(job_id, "first")
    store.add_log(job_id, "second")
    store.add_log(job_id, "third")

    logs = store.get_logs(job_id)
    assert [entry["line"] for entry in logs] == ["first", "second", "third"]
    assert [entry["seq"] for entry in logs] == [1, 2, 3]

    tail = store.get_logs(job_id, after_seq=1)
    assert [entry["line"] for entry in tail] == ["second", "third"]


def test_list_jobs_filter_and_limit(tmp_path):
    store = _store(tmp_path)
    first = store.create_job("riva", "start", "a")
    second = store.create_job("llm", "stop", "b")
    listed = store.list_jobs()
    assert {job["id"] for job in listed} == {first, second}
    assert [job["id"] for job in store.list_jobs(pod="llm")] == [second]


def test_audit_insert(tmp_path):
    store = _store(tmp_path)
    store.audit("admin", "start", "riva", "queued job")
    rows = store.list_audit()
    assert rows[0]["actor"] == "admin"
    assert rows[0]["action"] == "start"
    assert rows[0]["pod"] == "riva"
