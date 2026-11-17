"""Optional job-result email notifications."""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from runpod_ops.config import SMTPConfig

logger = logging.getLogger(__name__)


class EmailNotifier:
    def __init__(self, smtp: SMTPConfig, smtp_factory=None):
        self.smtp = smtp
        self.smtp_factory = smtp_factory

    def _factory(self, host: str, port: int):
        factory = self.smtp_factory
        if factory is not None:
            return factory(host, port, timeout=10)
        if port == 465:
            return smtplib.SMTP_SSL(host, port, timeout=10)
        return smtplib.SMTP(host, port, timeout=10)

    def send_job_result(
        self, pod: str, action: str, status: str, job_id: str
    ) -> bool:
        smtp = self.smtp
        if not smtp.enabled or not smtp.host or not smtp.recipient or not smtp.sender:
            return False

        message = EmailMessage()
        message["Subject"] = f"[runpod-ops] {pod} {action} {status}"
        message["From"] = smtp.sender
        message["To"] = smtp.recipient
        message.set_content(
            f"Pod: {pod}\nAction: {action}\nStatus: {status}\nJob: {job_id}\n"
        )

        try:
            server = self._factory(smtp.host, smtp.port)
            if smtp.use_tls and smtp.port != 465:
                server.starttls()
            if smtp.username:
                server.login(smtp.username, smtp.password)
            server.send_message(message)
            server.quit()
            return True
        except Exception:
            logger.exception("failed to send job-result email")
            return False
