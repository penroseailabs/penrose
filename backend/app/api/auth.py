"""Who may use the API: only this machine, and with the desktop app's per-launch sign-in, only its window.

The Electron app starts the API with a random username and password (LOGIN_USERNAME, LOGIN_PASSWORD), signs
in once, and hands the session cookie to its window. Without them (tests, a bare `relay-server api`) the API
is open to local callers.

Other programs on this computer (the `relay` command, for Claude Code and Cowork) reach the phone API alone,
with a durable integration token made once and kept in the data dir (integration_token)."""

import contextlib
import hashlib
import hmac
import os
import secrets
import time
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from ..bell import INTERNAL_PREFIX, SERVICE_HEADER
from ..config import settings
from . import common

router = APIRouter(prefix="/api/auth")

SESSION_COOKIE = "relay_session"
SESSION_SECONDS = 86400
PUBLIC_PATHS = {"/api/auth/session", "/api/auth/login", "/api/health"}
PHONE_PREFIX = "/api/phone/"  # the one part of the API the integration token opens
INTEGRATION_FILE = "integration_token"
# The Vite dev server, whose /api requests are proxied here (npm run dev).
DEV_ORIGINS = ("http://localhost:8000", "http://127.0.0.1:8000")


def auth_enabled():
    return bool(settings.login_username or settings.login_password)


_integration = None


def integration_token():
    """The token the `relay` command shows: made once, kept in the data dir readable by this user only."""
    global _integration
    if _integration:
        return _integration
    path = settings.data_dir / INTEGRATION_FILE
    try:
        token = path.read_text().strip()
    except OSError:
        token = ""
    if not token:
        token = secrets.token_urlsafe(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(token)
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
    _integration = token
    return token


def bearer_valid(request):
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    return scheme.lower() == "bearer" and bool(token) and hmac.compare_digest(token.encode(), integration_token().encode())


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def session_valid(request):
    """A known, unexpired session. Use keeps it alive, so a window left open for days stays signed in."""
    token = request.cookies.get(SESSION_COOKIE, "")
    if not token:
        return False
    key = token_hash(token)
    rows = common.store.rows("SELECT expires FROM web_sessions WHERE token_hash=?", (key,))
    now = time.time()
    if not rows or rows[0]["expires"] <= now:
        return False
    if rows[0]["expires"] - now < SESSION_SECONDS / 2:
        common.store.execute("UPDATE web_sessions SET expires=? WHERE token_hash=?", (now + SESSION_SECONDS, key))
    return True


def companion_request(request):
    """A linked phone's request, played in by the tunnel (app/companion/tunnel.py) with this launch's token."""
    from ..companion import tunnel as companion_tunnel

    if request.headers.get("host", "") != "companion":
        return False
    token = companion_tunnel.token()
    return bool(token) and hmac.compare_digest(
        request.headers.get(companion_tunnel.COMPANION_HEADER, "").encode(), token.encode()
    )


async def guard(request: Request, call_next):
    """Local hosts only; writes only from the app's own origin (the CSRF boundary for the cookie); and the
    API, though not the UI's files, only with a session."""
    host = request.headers.get("host", "").split(":")[0]
    if host == "companion":
        from ..companion import tunnel as companion_tunnel

        if not companion_request(request):
            return Response("Local access only", status_code=403)
        if companion_tunnel.allowed(request.method, request.url.path):
            return await call_next(request)
        return Response('{"detail":"Not available from the phone"}', status_code=403, media_type="application/json")
    if host not in ("localhost", "127.0.0.1", "testserver", urlparse(settings.public_origin).hostname):
        return Response("Local access only", status_code=403)
    origin = request.headers.get("origin")
    if (
        request.method not in ("GET", "HEAD", "OPTIONS")
        and origin
        and origin not in (settings.public_origin.rstrip("/"), *DEV_ORIGINS)
    ):
        return Response("Cross-origin writes are disabled", status_code=403)
    path = request.url.path
    if path.startswith(INTERNAL_PREFIX):
        # The worker, which has no session: it shows the token the desktop app gave the services this launch.
        token = settings.gateway_token
        if token and hmac.compare_digest(request.headers.get(SERVICE_HEADER, "").encode(), token.encode()):
            return await call_next(request)
        if token or auth_enabled():
            return Response("Service token required", status_code=401)
        return await call_next(request)
    if auth_enabled() and path.startswith("/api/") and path not in PUBLIC_PATHS and not session_valid(request):
        if path.startswith(PHONE_PREFIX) and bearer_valid(request):
            return await call_next(request)
        return Response('{"detail":"Sign in to access your workspace"}', status_code=401, media_type="application/json")
    return await call_next(request)


class LoginInput(BaseModel):
    username: str = Field(max_length=256)
    password: str = Field(max_length=1024)


@router.get("/session")
async def auth_session(request: Request):
    authenticated = not auth_enabled() or session_valid(request) or companion_request(request)
    return {"authenticated": authenticated, "user_id": settings.user_id if authenticated else None}


@router.post("/login")
async def login(body: LoginInput):
    if not settings.login_username or not settings.login_password:
        raise HTTPException(503, "Sign-in has not been configured")
    username_ok = hmac.compare_digest(body.username.encode(), settings.login_username.encode())
    password_ok = hmac.compare_digest(body.password.encode(), settings.login_password.encode())
    if not (username_ok and password_ok):
        raise HTTPException(401, "Invalid username or password")
    token = secrets.token_urlsafe(32)
    common.store.execute("DELETE FROM web_sessions WHERE expires<=?", (time.time(),))
    common.store.execute("INSERT INTO web_sessions VALUES (?,?)", (token_hash(token), time.time() + SESSION_SECONDS))
    response = Response('{"authenticated":true}', media_type="application/json")
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="strict", max_age=SESSION_SECONDS)
    return response
