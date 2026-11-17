import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from runpod_ops.auth import AuthManager


def _auth(ttl=3600):
    return AuthManager("hunter2", "test-secret", ttl_seconds=ttl)


def test_password_check():
    auth = _auth()
    assert auth.check_password("hunter2") is True
    assert auth.check_password("wrong") is False


def test_empty_password_never_authenticates():
    auth = AuthManager("", "secret")
    assert auth.check_password("") is False


def test_session_roundtrip():
    auth = _auth()
    token = auth.create_session()
    assert auth.verify_session(token) is True


def test_tampered_session_rejected():
    auth = _auth()
    token = auth.create_session()
    assert auth.verify_session(token + "x") is False
    assert auth.verify_session("garbage") is False


def test_expired_session_rejected():
    auth = _auth(ttl=-1)
    assert auth.verify_session(auth.create_session()) is False


def test_csrf_roundtrip():
    auth = _auth()
    session = auth.create_session()
    token = auth.new_csrf_token(session)
    assert auth.verify_csrf(session, token) is True
    assert auth.verify_csrf(session, "wrong") is False
    assert auth.verify_csrf(session, "") is False
