from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from app.internal import auth

router = APIRouter(tags=["ui"])

_TEMPLATE = Path(__file__).parent.parent / "templates" / "index.html"
_SCANNER_TEMPLATE = Path(__file__).parent.parent / "templates" / "scanner.html"
_LOGIN_TEMPLATE = Path(__file__).parent.parent / "templates" / "login.html"


@router.get("/login", response_class=HTMLResponse)
async def login_page(error: Optional[str] = None):
    html = _LOGIN_TEMPLATE.read_text()
    banner = (
        '<div class="error">Invalid username or password.</div>' if error else ""
    )
    return html.replace("<!--ERROR_BANNER-->", banner)


@router.post("/login")
async def login_submit(username: str = Form(...), password: str = Form(...)):
    if not auth.verify_credentials(username, password):
        return RedirectResponse("/login?error=1", status_code=303)

    token = auth.create_session_token(username)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(
        auth.SESSION_COOKIE_NAME,
        token,
        max_age=auth.SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        # No TLS termination on this port (LAN dashboard on :80) — `secure`
        # would make the cookie never get sent at all.
        secure=False,
    )
    return resp


@router.post("/logout")
@router.get("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.SESSION_COOKIE_NAME)
    return resp


@router.get("/", response_class=HTMLResponse)
async def index(authed: bool = Depends(auth.require_session)):
    if not authed:
        return RedirectResponse("/login")
    return _TEMPLATE.read_text()


@router.get("/scanner", response_class=HTMLResponse)
async def scanner(authed: bool = Depends(auth.require_session)):
    if not authed:
        return RedirectResponse("/login")
    return _SCANNER_TEMPLATE.read_text()
