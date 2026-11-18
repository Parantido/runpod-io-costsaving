import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from fastapi.testclient import TestClient

from runpod_ops.auth import AuthManager
from runpod_ops.config import (
    AppConfig,
    NotificationConfig,
    PodProfile,
    TelegramConfig,
)
from runpod_ops.jobs import JobConflict
from runpod_ops.state import StateStore
from runpod_ops.web.app import create_app


class FakeRunner:
    def __init__(self, state):
        self.state = state
        self.calls = []
        self.conflict = False

    def submit(self, profile, action, actor):
        if self.conflict:
            raise JobConflict("busy")
        self.calls.append((profile.name, action, actor))
        job_id = self.state.create_job(profile.name, action, "masked command")
        self.state.start_job(job_id)
        self.state.add_log(job_id, "engine says hello")
        self.state.finish_job(job_id, "succeeded", 0)
        return job_id

    def wait_for(self, job_id, timeout=0):
        return "succeeded"


class FakeProvider:
    def get_pod(self, pod_id):
        return {"id": pod_id, "status": "RUNNING"}

    def list_gpus(self, min_mem, min_vcpu, max_price):
        return []


def _app(tmp_path):
    config = AppConfig(
        data_dir=tmp_path,
        pods=(PodProfile(name="riva", template_id="tpl-1"),),
        secret_key="test-secret",
        admin_password="pw",
        notifications=NotificationConfig(
            telegram=TelegramConfig(enabled=True, bot_token="super-secret-token", chat_id="1")
        ),
    )
    state = StateStore(tmp_path / "state.db")
    state.init_schema()
    state.upsert_pod("riva", "pod-1", "RUNNING")
    runner = FakeRunner(state)
    auth = AuthManager("pw", "test-secret")
    app = create_app(
        config, state=state, runner=runner, provider=FakeProvider(), auth=auth
    )
    return app, state, runner, auth


def _login(client):
    response = client.post("/login", data={"password": "pw"}, follow_redirects=False)
    assert response.status_code == 303
    return client


def _csrf(client):
    html = client.get("/").text
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def test_health_unauthenticated(tmp_path):
    app, *_ = _app(tmp_path)
    client = TestClient(app)
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_dashboard_redirects_to_login(tmp_path):
    app, *_ = _app(tmp_path)
    client = TestClient(app)
    response = client.get("/", follow_redirects=False)
    assert response.status_code in {302, 303, 307}
    assert response.headers["location"] == "/login"


def test_login_failure(tmp_path):
    app, *_ = _app(tmp_path)
    client = TestClient(app)
    response = client.post("/login", data={"password": "wrong"})
    assert response.status_code == 401


def test_login_and_dashboard(tmp_path):
    app, *_ = _app(tmp_path)
    client = _login(TestClient(app))
    response = client.get("/")
    assert response.status_code == 200
    assert "riva" in response.text
    assert "super-secret-token" not in response.text


def test_mutation_requires_csrf(tmp_path):
    app, state, runner, _ = _app(tmp_path)
    client = _login(TestClient(app))
    response = client.post("/pods/riva/start", data={})
    assert response.status_code == 403
    assert runner.calls == []


def test_mutation_queues_job(tmp_path):
    app, state, runner, _ = _app(tmp_path)
    client = _login(TestClient(app))
    token = _csrf(client)
    response = client.post(
        "/pods/riva/recreate",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert runner.calls == [("riva", "recreate", "admin")]


def test_duplicate_mutation_returns_409(tmp_path):
    app, state, runner, _ = _app(tmp_path)
    client = _login(TestClient(app))
    token = _csrf(client)
    runner.conflict = True
    response = client.post(
        "/pods/riva/start", data={"csrf_token": token}, follow_redirects=False
    )
    assert response.status_code == 409


def test_job_page_and_log_fragment(tmp_path):
    app, state, runner, _ = _app(tmp_path)
    client = _login(TestClient(app))
    token = _csrf(client)
    response = client.post(
        "/pods/riva/start", data={"csrf_token": token}, follow_redirects=False
    )
    job_id = state.list_jobs()[0]["id"]
    page = client.get(f"/jobs/{job_id}")
    assert page.status_code == 200
    assert "engine says hello" in page.text
    fragment = client.get(f"/jobs/{job_id}/log")
    assert fragment.status_code == 200
    assert "engine says hello" in fragment.text


def test_api_requires_auth_and_returns_pods(tmp_path):
    app, *_ = _app(tmp_path)
    client = TestClient(app)
    assert client.get("/api/pods").status_code == 401
    _login(client)
    response = client.get("/api/pods")
    assert response.status_code == 200
    assert response.json()[0]["name"] == "riva"


def test_schedules_page_requires_auth(tmp_path):
    app, *_ = _app(tmp_path)
    client = TestClient(app)
    response = client.get("/schedules", follow_redirects=False)
    assert response.status_code in {302, 303, 307}


def test_schedule_create_and_render(tmp_path):
    app, state, *_ = _app(tmp_path)
    client = _login(TestClient(app))
    token = _csrf(client)
    response = client.post(
        "/schedules",
        data={
            "csrf_token": token,
            "name": "riva-up",
            "pod": "riva",
            "action": "recreate",
            "cron": "15 12 * * 1-6",
            "timezone": "Etc/UTC",
            "trigger": "external",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    schedule = state.get_schedule_by_name("riva-up")
    assert schedule["pod"] == "riva"
    assert schedule["enabled"] is False
    page = client.get("/schedules")
    assert "riva-up" in page.text


def test_schedule_create_rejects_bad_cron(tmp_path):
    app, state, *_ = _app(tmp_path)
    client = _login(TestClient(app))
    token = _csrf(client)
    response = client.post(
        "/schedules",
        data={
            "csrf_token": token,
            "name": "bad",
            "pod": "riva",
            "action": "start",
            "cron": "nope",
            "timezone": "Etc/UTC",
            "trigger": "external",
        },
    )
    assert response.status_code == 400
    assert "Cron expression" in response.text
    assert state.list_schedules() == []


def test_schedule_toggle_and_run(tmp_path):
    app, state, runner, *_ = _app(tmp_path)
    client = _login(TestClient(app))
    token = _csrf(client)
    schedule_id = state.create_schedule(
        "riva-up", "riva", "recreate", "15 12 * * 1-6"
    )
    response = client.post(
        f"/schedules/{schedule_id}/toggle",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert state.get_schedule(schedule_id)["enabled"] is True

    response = client.post(
        f"/schedules/{schedule_id}/run",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    for _ in range(50):
        if runner.calls:
            break
        import time
        time.sleep(0.02)
    assert runner.calls[0][0:2] == ("riva", "recreate")


def test_schedule_mutation_requires_csrf(tmp_path):
    app, state, *_ = _app(tmp_path)
    client = _login(TestClient(app))
    schedule_id = state.create_schedule(
        "riva-up", "riva", "recreate", "15 12 * * 1-6"
    )
    response = client.post(f"/schedules/{schedule_id}/delete", data={})
    assert response.status_code == 403
    assert state.get_schedule(schedule_id) is not None
