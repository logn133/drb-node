"""
Unit tests for local dashboard/API auth (app.internal.auth), plus the
credentials.py additions that persist its signing material (auth_salt,
session_secret) alongside the existing node API key.

This file is pure logic: password hashing/constant-time comparison, session
token signing/expiry, and HTTP Basic header parsing. See test_auth_endpoints.py
for the HTTP-level login/redirect/protection round trip through the routers.
"""
import base64
import secrets
import time
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.config import settings
from app.internal import auth, credentials


@pytest.fixture(autouse=True)
def isolated_credentials(tmp_path, monkeypatch):
    """Every test gets a fresh, on-disk-isolated credentials store so the
    generated auth salt / session secret never leak between tests, and a
    known username/password instead of the shipped default."""
    creds_file = tmp_path / "credentials.json"
    monkeypatch.setattr(credentials, "_CREDS_FILE", creds_file)
    monkeypatch.setattr(credentials, "_api_key", None)
    monkeypatch.setattr(credentials, "_auth_salt", None)
    monkeypatch.setattr(credentials, "_session_secret", None)
    monkeypatch.setattr(settings, "dashboard_username", "tester")
    monkeypatch.setattr(settings, "dashboard_password", "s3cret-pass")
    yield


def _basic_header(username: str, password: str) -> str:
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {encoded}"


# ---------------------------------------------------------------------------
# credentials.py: auth salt / session secret generation + persistence
# ---------------------------------------------------------------------------

def test_auth_material_is_generated_on_first_access():
    salt = credentials.get_auth_salt()
    secret = credentials.get_session_secret()
    assert isinstance(salt, bytes) and len(salt) == 16
    assert isinstance(secret, bytes) and len(secret) == 32


def test_auth_material_is_stable_across_repeated_calls():
    assert credentials.get_auth_salt() == credentials.get_auth_salt()
    assert credentials.get_session_secret() == credentials.get_session_secret()


def test_auth_material_persists_to_disk_and_survives_reload():
    salt = credentials.get_auth_salt()
    secret = credentials.get_session_secret()

    # Simulate a container restart: drop in-memory state, reload from disk.
    credentials._api_key = None
    credentials._auth_salt = None
    credentials._session_secret = None
    credentials.load()

    assert credentials.get_auth_salt() == salt
    assert credentials.get_session_secret() == secret


def test_saving_api_key_does_not_clobber_auth_material():
    """save_api_key() used to json.dumps({"api_key": key}) directly, which
    would have wiped auth_salt/session_secret out of credentials.json the
    moment C2 provisioned an API key after this feature was added."""
    salt = credentials.get_auth_salt()
    secret = credentials.get_session_secret()

    credentials.save_api_key("some-node-api-key")

    assert credentials.get_api_key() == "some-node-api-key"
    assert credentials.get_auth_salt() == salt
    assert credentials.get_session_secret() == secret


# ---------------------------------------------------------------------------
# verify_credentials() — password hashing + constant-time compare
# ---------------------------------------------------------------------------

def test_verify_credentials_accepts_correct_username_and_password():
    assert auth.verify_credentials("tester", "s3cret-pass") is True


def test_verify_credentials_rejects_wrong_password():
    assert auth.verify_credentials("tester", "wrong") is False


def test_verify_credentials_rejects_wrong_username():
    assert auth.verify_credentials("someone-else", "s3cret-pass") is False


def test_verify_credentials_rejects_empty_password():
    assert auth.verify_credentials("tester", "") is False


def test_password_is_hashed_not_compared_in_plaintext():
    with patch.object(auth, "_hash_password", wraps=auth._hash_password) as spy:
        auth.verify_credentials("tester", "s3cret-pass")
    # Once for the configured password, once for the submitted one — neither
    # side is ever compared as a raw string.
    assert spy.call_count == 2


def test_is_using_default_password_detects_the_shipped_default(monkeypatch):
    monkeypatch.setattr(settings, "dashboard_password", auth.DEFAULT_PASSWORD)
    assert auth.is_using_default_password() is True


def test_is_using_default_password_false_once_changed():
    assert auth.is_using_default_password() is False  # fixture already changed it


# ---------------------------------------------------------------------------
# session tokens
# ---------------------------------------------------------------------------

def test_session_token_round_trips():
    token = auth.create_session_token("tester")
    assert auth._verify_session_token(token) == "tester"


def test_session_token_rejects_tampered_payload():
    token = auth.create_session_token("tester")
    tampered = ("X" if token[0] != "X" else "Y") + token[1:]
    assert auth._verify_session_token(tampered) is None


def test_session_token_rejects_expired_token(monkeypatch):
    token = auth.create_session_token("tester")
    future = time.time() + auth.SESSION_TTL_SECONDS + 1
    monkeypatch.setattr(time, "time", lambda: future)
    assert auth._verify_session_token(token) is None


def test_session_token_rejects_username_mismatch(monkeypatch):
    token = auth.create_session_token("tester")
    monkeypatch.setattr(settings, "dashboard_username", "someone-else")
    assert auth._verify_session_token(token) is None


def test_session_token_garbage_input_does_not_raise():
    assert auth._verify_session_token("not-a-real-token") is None
    assert auth._verify_session_token("") is None


def test_session_token_signed_with_a_different_secret_is_rejected():
    token = auth.create_session_token("tester")
    # As if the node restarted without a persisted credentials.json.
    credentials._session_secret = secrets.token_bytes(32)
    assert auth._verify_session_token(token) is None


# ---------------------------------------------------------------------------
# HTTP Basic parsing
# ---------------------------------------------------------------------------

def test_basic_auth_accepts_valid_header():
    assert auth._verify_basic_auth(_basic_header("tester", "s3cret-pass")) is True


def test_basic_auth_rejects_wrong_credentials():
    assert auth._verify_basic_auth(_basic_header("tester", "wrong")) is False


def test_basic_auth_rejects_non_basic_scheme():
    assert auth._verify_basic_auth("Bearer sometoken") is False


def test_basic_auth_tolerates_garbage_without_raising():
    assert auth._verify_basic_auth("Basic not-valid-base64!!") is False
    assert auth._verify_basic_auth("") is False


# ---------------------------------------------------------------------------
# is_authenticated() — the combined check require_auth is built on
# ---------------------------------------------------------------------------

def test_is_authenticated_true_with_valid_session_cookie():
    token = auth.create_session_token("tester")
    assert auth.is_authenticated(token, None) is True


def test_is_authenticated_true_with_valid_basic_header():
    assert auth.is_authenticated(None, _basic_header("tester", "s3cret-pass")) is True


def test_is_authenticated_false_with_neither():
    assert auth.is_authenticated(None, None) is False


def test_is_authenticated_false_with_invalid_session_and_no_header():
    assert auth.is_authenticated("garbage", None) is False


# ---------------------------------------------------------------------------
# FastAPI dependencies: require_session / require_auth
# ---------------------------------------------------------------------------

async def test_require_session_false_with_no_cookie():
    assert await auth.require_session(None) is False


async def test_require_session_true_with_valid_cookie():
    token = auth.create_session_token("tester")
    assert await auth.require_session(token) is True


async def test_require_auth_raises_401_with_no_credentials():
    with pytest.raises(HTTPException) as exc_info:
        await auth.require_auth(None, None)
    assert exc_info.value.status_code == 401
    assert exc_info.value.headers["WWW-Authenticate"] == "Basic"


async def test_require_auth_passes_with_valid_session_cookie():
    token = auth.create_session_token("tester")
    await auth.require_auth(token, None)  # must not raise


async def test_require_auth_passes_with_valid_basic_header():
    await auth.require_auth(None, _basic_header("tester", "s3cret-pass"))  # must not raise


# ---------------------------------------------------------------------------
# startup warning
# ---------------------------------------------------------------------------

def test_warn_if_default_password_logs_when_default(monkeypatch):
    monkeypatch.setattr(settings, "dashboard_password", auth.DEFAULT_PASSWORD)
    with patch("app.internal.auth.logger") as mock_logger:
        auth.warn_if_default_password()
    mock_logger.warning.assert_called_once()


def test_warn_if_default_password_silent_once_changed():
    with patch("app.internal.auth.logger") as mock_logger:
        auth.warn_if_default_password()
    mock_logger.warning.assert_not_called()
