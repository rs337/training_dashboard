"""Single-password login."""

from __future__ import annotations

import time

import pytest

from app import auth

PASSWORD = "correct horse battery staple"


@pytest.fixture
def secured(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", PASSWORD)


def sign_in(client, password=PASSWORD, next_url="/"):
    return client.post("/login", data={"password": password, "next": next_url})


# ---- when it's on ---------------------------------------------------------------------
def test_pages_redirect_to_login(client, secured):
    for path in ("/", "/races", "/activities", "/week/2026-11-16"):
        r = client.get(path)
        assert r.status_code == 303 and r.headers["location"].startswith("/login?next=")


def test_next_url_is_preserved(client, secured):
    r = client.get("/week/2026-11-16")
    assert r.headers["location"] == "/login?next=%2Fweek%2F2026-11-16"


def test_every_write_endpoint_is_blocked_without_login(client, secured):
    posts = ["/refresh", "/sync", "/races", "/races/1", "/races/1/delete", "/plan/settings",
             "/week/2026-11-16/meta", "/week/2026-11-16/day/0", "/week/2026-11-16/swap",
             "/week/2026-11-16/copy-previous", "/week/2026-11-16/clear", "/week/2026-11-16/manual",
             "/activity/1/focus", "/activity/1/category", "/activity/1/day", "/activity/1/delete"]
    for path in posts:
        assert client.post(path, data={}).status_code == 401, path


def test_htmx_requests_get_a_redirect_header_not_a_login_page(client, secured):
    r = client.post("/week/2026-11-16/day/0", headers={"HX-Request": "true"}, data={})
    assert r.status_code == 401 and r.headers["HX-Redirect"] == "/login"


def test_public_paths_stay_public(client, secured):
    assert client.get("/login").status_code == 200
    assert client.get("/healthz").json() == {"ok": True}
    assert client.get("/static/style.css").status_code == 200


def test_login_page_never_contains_the_password(client, secured):
    assert PASSWORD not in client.get("/login").text


def test_wrong_password_is_rejected_without_a_cookie(client, secured):
    r = sign_in(client, "nope")
    assert r.status_code == 401 and "Wrong password" in r.text
    assert auth.COOKIE not in r.cookies
    assert client.get("/").status_code == 303


def test_right_password_sets_a_hardened_cookie_and_unlocks_the_app(client, secured):
    r = sign_in(client, next_url="/races")
    assert r.status_code == 303 and r.headers["location"] == "/races"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and PASSWORD not in cookie
    assert client.get("/races").status_code == 200
    assert client.get("/").status_code == 200


def test_cookie_is_secure_over_https_or_when_forced(client, secured, monkeypatch):
    assert "secure" not in sign_in(client).headers["set-cookie"].lower()
    monkeypatch.setenv("COOKIE_SECURE", "1")
    assert "secure" in sign_in(client).headers["set-cookie"].lower()


@pytest.mark.parametrize("evil", ["//evil.com", "https://evil.com/x", "/\\evil.com", "javascript:alert(1)", ""])
def test_next_cannot_redirect_off_site(client, secured, evil):
    assert sign_in(client, next_url=evil).headers["location"] == "/"


def test_logout_locks_the_app_again(client, secured):
    sign_in(client)
    assert client.get("/").status_code == 200
    r = client.post("/logout")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    client.cookies.clear()
    assert client.get("/").status_code == 303


def test_signed_in_users_skip_the_login_form(client, secured):
    sign_in(client)
    assert client.get("/login?next=/races").headers["location"] == "/races"


def test_sign_out_button_only_shows_when_auth_is_on(client, secured, monkeypatch):
    sign_in(client)
    assert 'action="/logout"' in client.get("/").text
    monkeypatch.delenv("DASHBOARD_PASSWORD")
    assert 'action="/logout"' not in client.get("/").text


# ---- cookie integrity -------------------------------------------------------------------
def test_forged_and_malformed_cookies_are_rejected(client, secured):
    now = int(time.time())
    for token in ("", "garbage", "x.y", f"{now + 9999}.deadbeef", f"{now + 9999}", ".", "-5.abc"):
        client.cookies.set(auth.COOKIE, token)
        assert client.get("/").status_code == 303, token


def test_expired_token_is_rejected(secured):
    old = auth.make_token(now=time.time() - (auth.SESSION_DAYS + 1) * 86400)
    assert not auth.valid_token(old)
    assert auth.valid_token(auth.make_token())


def test_changing_the_password_logs_everyone_out(client, secured, monkeypatch):
    sign_in(client)
    assert client.get("/").status_code == 200
    monkeypatch.setenv("DASHBOARD_PASSWORD", "a new password")
    assert client.get("/").status_code == 303


def test_session_secret_overrides_the_derived_key(secured, monkeypatch):
    token = auth.make_token()
    monkeypatch.setenv("SESSION_SECRET", "something else")
    assert not auth.valid_token(token)


# ---- brute force ----------------------------------------------------------------------
def test_repeated_wrong_passwords_lock_the_client_out_even_for_the_right_one(client, secured):
    for _ in range(auth.MAX_FAILURES):
        assert sign_in(client, "wrong").status_code == 401
    assert sign_in(client, "wrong").status_code == 429
    assert sign_in(client).status_code == 429
    assert client.get("/").status_code == 303


def test_a_successful_login_resets_the_failure_count(client, secured):
    for _ in range(auth.MAX_FAILURES - 1):
        sign_in(client, "wrong")
    assert sign_in(client).status_code == 303
    client.cookies.clear()
    for _ in range(auth.MAX_FAILURES - 1):
        assert sign_in(client, "wrong").status_code == 401


def test_failures_expire_after_the_window(client, secured):
    class Req:  # minimal stand-in: only .client.host is used
        client = type("C", (), {"host": "1.2.3.4"})()

    now = time.time()
    for _ in range(auth.MAX_FAILURES):
        auth.record_failure(Req, now)
    assert auth.throttled(Req, now + 1)
    assert not auth.throttled(Req, now + auth.FAILURE_WINDOW_S + 1)


# ---- when it's off --------------------------------------------------------------------
def test_without_a_password_the_app_is_open_for_local_use(client):
    assert not auth.enabled()
    assert client.get("/").status_code == 200
    assert client.get("/login").headers["location"] == "/"
    assert not auth.password_ok("")  # an empty guess must never count as correct


def test_empty_password_never_authenticates_when_enabled(client, secured):
    assert sign_in(client, "").status_code == 401
