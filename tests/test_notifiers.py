import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from runpod_ops.config import SMTPConfig
from runpod_ops.notifiers import EmailNotifier


class FakeSMTP:
    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.started_tls = False
        self.logged_in = None
        self.message = None
        self.quit_called = False

    def starttls(self):
        self.started_tls = True

    def login(self, username, password):
        self.logged_in = (username, password)

    def send_message(self, message):
        self.message = message

    def quit(self):
        self.quit_called = True


def _smtp(**overrides):
    values = dict(
        enabled=True,
        host="smtp.example.com",
        port=587,
        username="user",
        password="pass",
        sender="ops@example.com",
        recipient="admin@example.com",
        use_tls=True,
    )
    values.update(overrides)
    return SMTPConfig(**values)


def test_disabled_returns_false():
    sent = []
    notifier = EmailNotifier(_smtp(enabled=False), smtp_factory=lambda *a, **k: sent.append(a))
    assert notifier.send_job_result("riva", "start", "succeeded", "job1") is False
    assert sent == []


def test_incomplete_config_returns_false():
    notifier = EmailNotifier(_smtp(recipient=""))
    assert notifier.send_job_result("riva", "start", "succeeded", "job1") is False


def test_sends_tls_email_with_details():
    server = FakeSMTP("smtp.example.com", 587)
    notifier = EmailNotifier(_smtp(), smtp_factory=lambda host, port, timeout=None: server)
    assert notifier.send_job_result("riva", "recreate", "failed", "job-9") is True
    assert server.started_tls is True
    assert server.logged_in == ("user", "pass")
    assert server.quit_called is True
    assert "riva recreate failed" in server.message["Subject"]
    body = server.message.get_content()
    assert "job-9" in body
    assert "failed" in body
