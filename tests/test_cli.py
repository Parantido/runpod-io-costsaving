import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from runpod_ops import cli
from runpod_ops.config import AppConfig, PodProfile
from runpod_ops.state import StateStore


class FakeRunner:
    def __init__(self, state, status="succeeded"):
        self.state = state
        self.status = status
        self.submitted = []

    def submit(self, profile, action, actor):
        self.submitted.append((profile.name, action, actor))
        job_id = self.state.create_job(profile.name, action, "cmd")
        self.state.start_job(job_id)
        self.state.add_log(job_id, "engine output")
        self.state.finish_job(job_id, self.status, 0)
        return job_id

    def wait_for(self, job_id, timeout=0):
        return self.status


class FakeComponents:
    def __init__(self, state, runner):
        self.state = state
        self.runner = runner
        self.provider = None


def _components(tmp_path, status="succeeded"):
    config = AppConfig(
        data_dir=tmp_path,
        pods=(PodProfile(name="riva", template_id="tpl-1"),),
        secret_key="s",
        admin_password="pw",
    )
    state = StateStore(tmp_path / "state.db")
    state.init_schema()
    runner = FakeRunner(state, status)
    return config, FakeComponents(state, runner)


def test_start_action(tmp_path, capsys):
    config, components = _components(tmp_path)
    code = cli.main(
        ["start", "riva"],
        components_factory=lambda cfg: components,
        config_loader=lambda path: config,
    )
    assert code == 0
    assert components.runner.submitted == [("riva", "start", "cli")]
    assert "engine output" in capsys.readouterr().out


def test_failed_action_returns_one(tmp_path):
    config, components = _components(tmp_path, status="failed")
    code = cli.main(
        ["start", "riva"],
        components_factory=lambda cfg: components,
        config_loader=lambda path: config,
    )
    assert code == 1


def test_unknown_pod_returns_two(tmp_path, capsys):
    config, components = _components(tmp_path)
    code = cli.main(
        ["start", "missing"],
        components_factory=lambda cfg: components,
        config_loader=lambda path: config,
    )
    assert code == 2
    assert "Unknown pod" in capsys.readouterr().err


def test_serve_delegates(tmp_path):
    config, components = _components(tmp_path)
    calls = []

    def serve(cfg, host, port):
        calls.append((cfg, host, port))

    code = cli.main(
        ["serve", "--host", "127.0.0.1", "--port", "9999"],
        components_factory=lambda cfg: components,
        config_loader=lambda path: config,
        serve_func=serve,
    )
    assert code == 0
    assert calls == [(config, "127.0.0.1", 9999)]


def test_pods_lists_configured_names(tmp_path, capsys):
    config, components = _components(tmp_path)
    code = cli.main(
        ["pods"],
        components_factory=lambda cfg: components,
        config_loader=lambda path: config,
    )
    assert code == 0
    assert "riva" in capsys.readouterr().out
