"""Password, signed session, and CSRF helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time


class AuthManager:
    def __init__(self, password: str, secret: str, ttl_seconds: int = 28800):
        self.password = password
        self.secret = secret.encode()
        self.ttl_seconds = ttl_seconds

    def check_password(self, candidate: str) -> bool:
        if not self.password:
            return False
        return hmac.compare_digest(candidate.encode(), self.password.encode())

    def _sign(self, payload: bytes) -> str:
        signature = hmac.new(self.secret, payload, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(signature).decode()

    def create_session(self) -> str:
        payload = json.dumps(
            {"exp": int(time.time()) + self.ttl_seconds, "nonce": secrets.token_hex(16)},
            separators=(",", ":"),
        ).encode()
        encoded = base64.urlsafe_b64encode(payload).decode()
        return f"{encoded}.{self._sign(payload)}"

    def verify_session(self, token: str) -> bool:
        try:
            encoded, signature = token.split(".", 1)
            payload = base64.urlsafe_b64decode(encoded.encode())
        except (ValueError, TypeError):
            return False
        if not hmac.compare_digest(signature, self._sign(payload)):
            return False
        try:
            data = json.loads(payload)
            return int(data["exp"]) > int(time.time())
        except (ValueError, KeyError, TypeError):
            return False

    def new_csrf_token(self, session: str) -> str:
        return hmac.new(
            self.secret, f"csrf:{session}".encode(), hashlib.sha256
        ).hexdigest()

    def verify_csrf(self, session: str, token: str) -> bool:
        if not token:
            return False
        return hmac.compare_digest(token, self.new_csrf_token(session))
