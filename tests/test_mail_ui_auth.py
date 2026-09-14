"""Mail UI cookie login is independent from MCP bearer tokens."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from mcp_agent_mail import config as _config
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.config import ConfigError
from mcp_agent_mail.http import build_http_app
from mcp_agent_mail.ui_auth import (
    LoginAttemptLimiter,
    parse_session_cookie,
    sanitize_mail_next_path,
    session_signing_key,
    sign_session_payload,
)

_CSRF_INPUT_RE = re.compile(r'name="csrf_token" value="([^"]+)"')
_CSRF_META_RE = re.compile(r'name="csrf-token" content="([^"]+)"')
_SESSION_SECRET = "session-secret-ok-32-chars-min!!"
_RPC_HEALTH = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {"name": "health_check", "arguments": {}},
}


def test_authenticated_ui_external_assets_are_versioned_and_integrity_checked() -> None:
    template = (
        Path(__file__).parents[1] / "src/mcp_agent_mail/templates/base.html"
    ).read_text(encoding="utf-8")
    external_tags = re.findall(
        r'<(?:script|link)\b[^>]*(?:src|href)="https://[^>]+>',
        template,
    )
    assert external_tags
    assert all('integrity="sha384-' in tag for tag in external_tags)
    assert all('crossorigin="anonymous"' in tag for tag in external_tags)
    assert "@latest" not in template
    assert "@3.x.x" not in template
    assert "cdn.tailwindcss.com" not in template


def test_sanitize_mail_next_path_rejects_open_redirects() -> None:
    assert sanitize_mail_next_path(None) == "/mail"
    assert sanitize_mail_next_path("/mail/projects") == "/mail/projects"
    assert sanitize_mail_next_path("/mail?category=trash") == "/mail?category=trash"
    assert sanitize_mail_next_path("//evil.example") == "/mail"
    assert sanitize_mail_next_path("https://evil.example/mail") == "/mail"
    assert sanitize_mail_next_path("/mailbox") == "/mail"
    assert sanitize_mail_next_path("/login") == "/mail"
    assert sanitize_mail_next_path("/mail\\n") == "/mail"
    assert sanitize_mail_next_path("/mail?x=1\r\nX-Evil: 1") == "/mail"


def test_session_cookie_roundtrip_and_tamper(isolated_env, monkeypatch) -> None:
    monkeypatch.setenv("MAIL_UI_PASSWORD", "ui-secret-ok")
    monkeypatch.setenv("MAIL_UI_SESSION_SECRET", _SESSION_SECRET)
    _config.clear_settings_cache()
    settings = _config.get_settings()
    secret = session_signing_key(settings)
    assert secret is not None
    token = sign_session_payload(
        {"v": 1, "u": "operator", "csrf": "c" * 32, "exp": 2_000_000_000},
        secret=secret,
    )
    session = parse_session_cookie(token, secret=secret, now=1_900_000_000)
    assert session is not None
    assert session.username == "operator"
    tampered = ("a" + token[1:]) if token[0] != "a" else ("b" + token[1:])
    assert parse_session_cookie(tampered, secret=secret, now=1_900_000_000) is None
    expired = sign_session_payload(
        {"v": 1, "u": "operator", "csrf": "c" * 32, "exp": 10},
        secret=secret,
    )
    assert parse_session_cookie(expired, secret=secret, now=20) is None


def test_password_rotation_revokes_existing_session(isolated_env, monkeypatch) -> None:
    monkeypatch.setenv("MAIL_UI_PASSWORD", "old-password")
    monkeypatch.setenv("MAIL_UI_SESSION_SECRET", _SESSION_SECRET)
    _config.clear_settings_cache()
    old_key = session_signing_key(_config.get_settings())
    assert old_key is not None
    token = sign_session_payload(
        {"v": 1, "u": "operator", "csrf": "c" * 32, "exp": 2_000_000_000},
        secret=old_key,
    )

    monkeypatch.setenv("MAIL_UI_PASSWORD", "new-password")
    _config.clear_settings_cache()
    new_key = session_signing_key(_config.get_settings())
    assert new_key is not None
    assert new_key != old_key
    assert parse_session_cookie(token, secret=new_key, now=1_900_000_000) is None


def test_password_requires_session_secret(isolated_env, monkeypatch) -> None:
    monkeypatch.setenv("MAIL_UI_PASSWORD", "ui-secret-ok")
    monkeypatch.setenv("MAIL_UI_SESSION_SECRET", "")
    _config.clear_settings_cache()
    with pytest.raises(ConfigError, match="MAIL_UI_SESSION_SECRET"):
        _config.get_settings()


def test_session_secret_rejects_short_value(isolated_env, monkeypatch) -> None:
    monkeypatch.setenv("MAIL_UI_PASSWORD", "ui-secret-ok")
    monkeypatch.setenv("MAIL_UI_SESSION_SECRET", "too-short")
    _config.clear_settings_cache()
    with pytest.raises(ConfigError, match="at least 32"):
        _config.get_settings()


def test_session_secret_requires_password(isolated_env, monkeypatch) -> None:
    monkeypatch.setenv("MAIL_UI_PASSWORD", "")
    monkeypatch.setenv("MAIL_UI_SESSION_SECRET", "x")
    _config.clear_settings_cache()
    with pytest.raises(ConfigError, match="must not be set without MAIL_UI_PASSWORD"):
        _config.get_settings()


def test_username_rejects_spaces(isolated_env, monkeypatch) -> None:
    monkeypatch.setenv("MAIL_UI_USERNAME", "mail admin")
    _config.clear_settings_cache()
    with pytest.raises(ConfigError, match="MAIL_UI_USERNAME"):
        _config.get_settings()


def _csrf_from_html(html: str) -> str:
    match = _CSRF_INPUT_RE.search(html)
    assert match is not None
    return match.group(1)


async def _build_client(monkeypatch: pytest.MonkeyPatch) -> tuple[AsyncClient, _config.Settings]:
    monkeypatch.setenv("MAIL_UI_PASSWORD", "ui-secret-ok")
    monkeypatch.setenv("MAIL_UI_SESSION_SECRET", _SESSION_SECRET)
    monkeypatch.setenv("HTTP_BEARER_TOKEN", "mcp-secret-token")
    monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "false")
    _config.clear_settings_cache()
    settings = _config.get_settings()
    app = build_http_app(settings, build_mcp_server())
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    return client, settings


async def _sign_in(client: AsyncClient) -> None:
    login_page = await client.get("/mail/login")
    csrf = _csrf_from_html(login_page.text)
    submitted = await client.post(
        "/mail/login",
        data={
            "username": "operator",
            "password": "ui-secret-ok",
            "csrf_token": csrf,
            "next": "/mail",
        },
        headers={"Origin": "http://test"},
        follow_redirects=False,
    )
    assert submitted.status_code == 303


@pytest.mark.asyncio
async def test_mail_login_is_reachable_without_bearer(isolated_env, monkeypatch) -> None:
    client, _settings = await _build_client(monkeypatch)
    async with client:
        response = await client.get("/mail/login")
        assert response.status_code == 200
        assert "Sign in to Agent Mail" in response.text
        assert "cdn.tailwindcss.com" not in response.text
        assert "cdn.jsdelivr.net" not in response.text
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"
        blocked = await client.get("/mail", follow_redirects=False)
        assert blocked.status_code == 303
        assert str(blocked.headers.get("location", "")).startswith("/mail/login")


@pytest.mark.asyncio
async def test_mail_login_ignores_jwt_requirement(isolated_env, monkeypatch) -> None:
    monkeypatch.setenv("HTTP_JWT_ENABLED", "true")
    monkeypatch.setenv("HTTP_JWT_SECRET", "0123456789abcdef0123456789abcdef")
    monkeypatch.setenv("HTTP_JWT_ALGORITHMS", "HS256")
    client, settings = await _build_client(monkeypatch)
    async with client:
        login = await client.get("/mail/login")
        assert login.status_code == 200
        mcp = await client.post(settings.http.path, json=_RPC_HEALTH)
        assert mcp.status_code == 401


@pytest.mark.asyncio
async def test_mail_login_success_and_logout(isolated_env, monkeypatch) -> None:
    client, settings = await _build_client(monkeypatch)
    async with client:
        await _sign_in(client)
        home = await client.get("/mail")
        assert home.status_code == 200
        assert "no-store" in (home.headers.get("cache-control") or "")
        assert "connect-src 'self'" in home.headers["content-security-policy"]
        assert "frame-ancestors 'none'" in home.headers["content-security-policy"]
        assert home.headers["x-frame-options"] == "DENY"
        assert "Sign out" in home.text
        mcp = await client.post(settings.http.path, json=_RPC_HEALTH)
        assert mcp.status_code == 401
        mcp_ok = await client.post(
            settings.http.path,
            json=_RPC_HEALTH,
            headers={"Authorization": "Bearer mcp-secret-token"},
        )
        assert mcp_ok.status_code != 401
        meta = _CSRF_META_RE.search(home.text)
        assert meta is not None
        logout = await client.post(
            "/mail/logout",
            data={"csrf_token": meta.group(1)},
            headers={"Origin": "http://test"},
            follow_redirects=False,
        )
        assert logout.status_code == 303
        again = await client.get("/mail", follow_redirects=False)
        assert again.status_code == 303


@pytest.mark.asyncio
async def test_mail_login_rejects_wrong_password(isolated_env, monkeypatch) -> None:
    client, _settings = await _build_client(monkeypatch)
    async with client:
        login_page = await client.get("/mail/login")
        csrf = _csrf_from_html(login_page.text)
        submitted = await client.post(
            "/mail/login",
            data={
                "username": "operator",
                "password": "wrong-password",
                "csrf_token": csrf,
                "next": "/mail",
            },
            headers={"Origin": "http://test"},
        )
        assert submitted.status_code == 200
        assert "Invalid username or password." in submitted.text
        blocked = await client.get("/mail", follow_redirects=False)
        assert blocked.status_code == 303


@pytest.mark.asyncio
async def test_mail_login_rate_limit(isolated_env, monkeypatch) -> None:
    monkeypatch.setenv("MAIL_UI_LOGIN_RATE_LIMIT_PER_MINUTE", "2")
    client, _settings = await _build_client(monkeypatch)
    async with client:
        page = await client.get("/mail/login")
        last = page
        for _ in range(3):
            csrf = _csrf_from_html(last.text)
            last = await client.post(
                "/mail/login",
                data={
                    "username": "operator",
                    "password": "wrong-password",
                    "csrf_token": csrf,
                    "next": "/mail",
                },
                headers={"Origin": "http://test"},
            )
        assert last.status_code == 429
        assert "Too many sign-in attempts" in last.text


def test_login_attempt_limiter_bounds_and_expires_state() -> None:
    now = [0.0]
    limiter = LoginAttemptLimiter(
        per_minute=10,
        max_entries=2,
        state_ttl_seconds=60.0,
        monotonic=lambda: now[0],
    )

    assert limiter.allow("192.0.2.1") is True
    assert limiter.allow("192.0.2.2") is True
    assert limiter.allow("192.0.2.3") is False
    assert limiter.entry_count == 2

    now[0] = 61.0
    assert limiter.allow("192.0.2.3") is True
    assert limiter.entry_count == 1


@pytest.mark.asyncio
async def test_mail_login_rejects_oversized_form(isolated_env, monkeypatch) -> None:
    client, _settings = await _build_client(monkeypatch)
    async with client:
        response = await client.post(
            "/mail/login",
            content=b"password=" + (b"x" * 8192),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert response.status_code == 413


@pytest.mark.asyncio
async def test_mail_login_checks_rate_limit_before_reading_form(
    isolated_env, monkeypatch
) -> None:
    monkeypatch.setenv("MAIL_UI_LOGIN_RATE_LIMIT_PER_MINUTE", "1")
    client, _settings = await _build_client(monkeypatch)
    async with client:
        page = await client.get("/mail/login")
        first = await client.post(
            "/mail/login",
            data={
                "username": "operator",
                "password": "wrong",
                "next": "/mail",
                "csrf_token": _csrf_from_html(page.text),
            },
        )
        assert first.status_code == 200

        limited = await client.post(
            "/mail/login",
            content=b"password=" + (b"x" * 8192),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert limited.status_code == 429


@pytest.mark.asyncio
async def test_mail_api_requires_csrf_after_login(isolated_env, monkeypatch) -> None:
    client, _settings = await _build_client(monkeypatch)
    async with client:
        await _sign_in(client)
        denied = await client.post("/mail/api/delete-messages", json={"message_ids": [1]})
        assert denied.status_code == 403
        home = await client.get("/mail")
        meta = _CSRF_META_RE.search(home.text)
        assert meta is not None
        still_denied = await client.post(
            "/mail/api/delete-messages",
            json={"message_ids": [1]},
            headers={"X-CSRF-Token": meta.group(1)},
        )
        assert still_denied.status_code == 403
        allowed = await client.post(
            "/mail/api/delete-messages",
            json={"message_ids": [1]},
            headers={"Origin": "http://test", "X-CSRF-Token": meta.group(1)},
        )
        assert allowed.status_code == 200
        assert allowed.json().get("success") is True


@pytest.mark.asyncio
async def test_mail_stays_open_on_localhost_without_password(isolated_env, monkeypatch) -> None:
    monkeypatch.setenv("HTTP_BEARER_TOKEN", "mcp-secret-token")
    monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "false")
    _config.clear_settings_cache()
    settings = _config.get_settings()
    app = build_http_app(settings, build_mcp_server())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        home = await client.get("/mail")
        assert home.status_code == 200
        mcp = await client.post(settings.http.path, json=_RPC_HEALTH)
        assert mcp.status_code == 401


@pytest.mark.asyncio
async def test_loopback_without_password_rejects_cross_origin_writes(
    isolated_env, monkeypatch
) -> None:
    monkeypatch.setenv("HTTP_BEARER_TOKEN", "mcp-secret-token")
    monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "false")
    _config.clear_settings_cache()
    settings = _config.get_settings()
    app = build_http_app(settings, build_mcp_server())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        cross_site = await client.post(
            "/mail/api/delete-messages",
            json={"message_ids": [1]},
            headers={"Origin": "https://evil.example"},
        )
        assert cross_site.status_code == 403
        local = await client.post("/mail/api/delete-messages", json={"message_ids": [1]})
        assert local.status_code == 200


@pytest.mark.asyncio
async def test_passwordless_loopback_rejects_dns_rebinding_host(
    isolated_env, monkeypatch
) -> None:
    monkeypatch.setenv("MAIL_UI_PASSWORD", "")
    monkeypatch.setenv("MAIL_UI_SESSION_SECRET", "")
    _config.clear_settings_cache()

    app = build_http_app(_config.get_settings(), build_mcp_server())
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://evil.example:8765",
        follow_redirects=False,
    ) as client:
        read_response = await client.get("/mail")
        assert read_response.status_code == 303
        assert read_response.headers["location"].startswith("/mail/login")

        write_response = await client.post(
            "/mail/api/delete-messages",
            json={"message_ids": [1]},
            headers={"Origin": "http://evil.example:8765"},
        )
        assert write_response.status_code == 401
