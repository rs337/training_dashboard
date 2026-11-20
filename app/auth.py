"""Single-password login for the whole app.

Set DASHBOARD_PASSWORD to turn it on (leave it unset for local-only use: auth is then off and a warning is logged).
Optional: SESSION_SECRET (signing key; defaults to one derived from the password, so changing the password
logs every browser out) and COOKIE_SECURE=1 (force the Secure flag when TLS is terminated by a proxy).

Sessions are a signed, expiring cookie, so nothing is stored server-side. This is deliberately temporary:
the multi-user version replaces it with per-user accounts.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import RedirectResponse, Response

log = logging.getLogger("dashboard.auth")

COOKIE = "session"
SESSION_DAYS = 30
PUBLIC_PREFIXES = ("/login", "/static/", "/healthz")
MAX_FAILURES, FAILURE_WINDOW_S = 5, 300  # 5 wrong passwords per client per 5 minutes

_failures: dict[str, list[float]] = {}


def password() -> str:
    return os.environ.get("DASHBOARD_PASSWORD", "")


def enabled() -> bool:
    return bool(password())


def _key() -> bytes:
    secret = os.environ.get("SESSION_SECRET") or ("derived:" + password())
    return hashlib.sha256(secret.encode()).digest()


def _sign(expires: int) -> str:
    return hmac.new(_key(), f"v1:{expires}".encode(), hashlib.sha256).hexdigest()


def make_token(now: float | None = None) -> str:
    expires = int((now or time.time()) + SESSION_DAYS * 86400)
    return f"{expires}.{_sign(expires)}"


def valid_token(token: str | None, now: float | None = None) -> bool:
    if not token or "." not in token:
        return False
    expires_text, signature = token.split(".", 1)
    if not expires_text.isdigit():
        return False
    if not hmac.compare_digest(signature, _sign(int(expires_text))):
        return False
    return int(expires_text) > (now or time.time())


def password_ok(candidate: str) -> bool:
    return enabled() and hmac.compare_digest(
        hashlib.sha256(candidate.encode()).digest(), hashlib.sha256(password().encode()).digest()
    )


def safe_next(target: str | None) -> str:
    """Only ever redirect to a path on this site (blocks //evil.com and https://evil.com)."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return "/"


# ---- brute-force throttle ------------------------------------------------------------
def _client(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def throttled(request: Request, now: float | None = None) -> bool:
    now = now or time.time()
    recent = [t for t in _failures.get(_client(request), []) if now - t < FAILURE_WINDOW_S]
    _failures[_client(request)] = recent
    return len(recent) >= MAX_FAILURES


def record_failure(request: Request, now: float | None = None) -> None:
    _failures.setdefault(_client(request), []).append(now or time.time())


def clear_failures(request: Request) -> None:
    _failures.pop(_client(request), None)


def reset_throttle() -> None:
    _failures.clear()


# ---- cookie + middleware ---------------------------------------------------------------
def set_session_cookie(request: Request, response: Response) -> None:
    response.set_cookie(
        COOKIE,
        make_token(),
        max_age=SESSION_DAYS * 86400,
        httponly=True,
        samesite="lax",  # also stops other sites from making the browser POST to us
        secure=request.url.scheme == "https" or os.environ.get("COOKIE_SECURE") == "1",
    )


def is_public(path: str) -> bool:
    return path == "/login" or any(path.startswith(p) for p in PUBLIC_PREFIXES)


async def require_login(request: Request, call_next):
    if not enabled() or is_public(request.url.path) or valid_token(request.cookies.get(COOKIE)):
        return await call_next(request)
    if request.headers.get("HX-Request"):  # htmx: tell it to navigate the whole page
        return Response(status_code=401, headers={"HX-Redirect": "/login"})
    if request.method != "GET":
        return Response("Login required", status_code=401)
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(f"/login?next={quote(target, safe='')}", status_code=303)


def login_redirect(request: Request, next_url: str) -> RedirectResponse:
    response = RedirectResponse(safe_next(next_url), status_code=303)
    set_session_cookie(request, response)
    return response


def logged_out() -> RedirectResponse:
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE)
    return response


def disabled_warning() -> None:
    if not enabled():
        log.warning("DASHBOARD_PASSWORD is not set: the dashboard has NO login. Fine on localhost only.")

