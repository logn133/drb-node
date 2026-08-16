"""
HTTP-level tests for the auth-protected dashboard/API surface: login/logout
flow, session-cookie protection of the HTML pages, and Basic-auth protection
of the JSON API.

Built as a standalone FastAPI app assembling the real api/ui routers — NOT
app.main:app, which wires a lifespan that connects to MQTT, starts the
PulseAudio capture loop, and pings OP25/C2. None of that belongs in a unit
test, and none of it is needed to exercise the auth layer: the auth
dependency runs (and short-circuits with a redirect/401) before any route
body that would touch those services.
"""
import base64

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.internal import auth, credentials
from app.routers import api, ui


@pytest.fixture(autouse=True)
def isolated_credentials(tmp_path, monkeypatch):
    creds_file = tmp_path / "credentials.json"
    monkeypatch.setattr(credentials, "_CREDS_FILE", creds_file)
    monkeypatch.setattr(credentials, "_api_key", None)
    monkeypatch.setattr(credentials, "_auth_salt", None)
    monkeypatch.setattr(credentials, "_session_secret", None)
    monkeypatch.setattr(settings, "dashboard_username", "tester")
    monkeypatch.setattr(settings, "dashboard_password", "s3cret-pass")
    yield


@pytest.fixture
def client():
    test_app = FastAPI()
    test_app.include_router(api.router)
    test_app.include_router(ui.router)
    with TestClient(test_app) as c:
        yield c


def _basic_header(username: str, password: str) -> dict:
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {encoded}"}


# ---------------------------------------------------------------------------
# /api/* — machine-facing JSON API
# ---------------------------------------------------------------------------

def test_api_route_rejects_unauthenticated_requests(client):
    r = client.get("/api/status", follow_redirects=False)
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Basic"


def test_api_route_accepts_valid_basic_auth(client):
    r = client.get("/api/config", headers=_basic_header("tester", "s3cret-pass"))
    assert r.status_code == 200


def test_api_route_rejects_wrong_basic_auth_password(client):
    r = client.get("/api/config", headers=_basic_header("tester", "wrong"))
    assert r.status_code == 401


def test_api_route_accepts_dashboard_session_cookie(client):
    login = client.post(
        "/login", data={"username": "tester", "password": "s3cret-pass"}, follow_redirects=False
    )
    assert login.status_code == 303
    assert auth.SESSION_COOKIE_NAME in login.cookies

    r = client.get("/api/config")  # cookie jar carries the session cookie
    assert r.status_code == 200


def test_every_api_route_is_registered_behind_the_auth_dependency():
    """Structural guard: catches a future route added to api.py that forgets
    the router is meant to protect everything in it."""
    assert any(
        getattr(dep, "dependency", None) is auth.require_auth
        for dep in api.router.dependencies
    )


# ---------------------------------------------------------------------------
# / and /scanner — the HTML dashboard
# ---------------------------------------------------------------------------

def test_index_redirects_to_login_when_unauthenticated(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/login"


def test_scanner_redirects_to_login_when_unauthenticated(client):
    r = client.get("/scanner", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/login"


def test_index_served_with_a_valid_session_cookie(client):
    client.post("/login", data={"username": "tester", "password": "s3cret-pass"})
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


# ---------------------------------------------------------------------------
# /login, /logout
# ---------------------------------------------------------------------------

def test_login_page_loads_without_auth(client):
    r = client.get("/login")
    assert r.status_code == 200


def test_login_with_correct_credentials_sets_cookie_and_redirects_home(client):
    r = client.post(
        "/login", data={"username": "tester", "password": "s3cret-pass"}, follow_redirects=False
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    cookie = r.cookies.get(auth.SESSION_COOKIE_NAME)
    assert cookie
    assert auth._verify_session_token(cookie) == "tester"


def test_login_with_wrong_password_redirects_back_with_error_and_no_cookie(client):
    r = client.post(
        "/login", data={"username": "tester", "password": "wrong"}, follow_redirects=False
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/login?error=1"
    assert auth.SESSION_COOKIE_NAME not in r.cookies


def test_login_error_banner_renders_on_the_login_page(client):
    r = client.get("/login?error=1")
    assert r.status_code == 200
    assert "Invalid username or password" in r.text


def test_logout_clears_the_session_cookie_and_redirects_to_login(client):
    client.post("/login", data={"username": "tester", "password": "s3cret-pass"})
    assert client.get("/").status_code == 200  # confirm we were logged in

    r = client.get("/logout", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"

    r2 = client.get("/", follow_redirects=False)
    assert r2.status_code in (302, 307)  # session cookie was cleared
