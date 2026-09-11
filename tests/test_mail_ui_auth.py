"""Mail UI cookie login is independent from MCP bearer tokens."""

from __future__ import annotations

import re

import pytest
from httpx import ASGITransport, AsyncClient

from mcp_agent_mail import config as _config
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.http import build_http_app
from mcp_agent_mail.ui_auth import (
    parse_session_cookie,
    sanitize_mail_next_path,
    session_signing_key,
    sign_session_payload,
)

_CSRF_INPUT_RE = re.compile(r'name="csrf_token" value="([^"]+)"')
_CSRF_META_RE = re.compile(r'name="csrf-token" content="([^"]+)"')
_RPC_HEALTH = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {"name": "health_check", "arguments": {}},
}


def test_sanitize_mail_next_path_rejects_open_redirects() -> None:
    assert sanitize_mail_next_path(None) == "/mail"
    assert sanitize_mail_next_path("/mail/projects") == "/mail/projects"
    assert sanitize_mail_next_path("/mail?category=trash") == "/mail?category=trash"
    assert sanitize_mail_next_path("//evil.example") == "/mail"
    assert sanitize_mail_next_path("https://evil.example/mail") == "/mail"
    assert sanitize_mail_next_path("/mailbox") == "/mail"
    assert sanitize_mail_next_path("/login") == "/mail"
    assert sanitize_mail_next_path("/mail\\n") == "/mail"


def test_session_cookie_roundtrip_and_tamper(isolated_env, monkeypatch) -> None:
    monkeypatch.setenv("MAIL_UI_PASSWORD", "ui-secret-ok")
    monkeypatch.setenv("MAIL_UI_SESSION_SECRET", "session-secret-ok")
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


def _csrf_from_html(html: str) -> str:
    match = _CSRF_INPUT_RE.search(html)
    assert match is not None
    return match.group(1)


async def _build_client(monkeypatch: pytest.MonkeyPatch) -> tuple[AsyncClient, _config.Settings]:
    monkeypatch.setenv("MAIL_UI_PASSWORD", "ui-secret-ok")
    monkeypatch.setenv("MAIL_UI_SESSION_SECRET", "session-secret-ok")
    monkeypatch.setenv("HTTP_BEARER_TOKEN", "mcp-secret-token")
    monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "false")
    _config.clear_settings_cache()
    settings = _config.get_settings()
    app = build_http_app(settings, build_mcp_server())
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    return client, settings


@pytest.mark.asyncio
async def test_mail_login_is_reachable_without_bearer(isolated_env, monkeypatch) -> None:
    client, _settings = await _build_client(monkeypatch)
    async with client:
        response = await client.get("/mail/login")
        assert response.status_code == 200
        assert "Sign in to Agent Mail" in response.text
        blocked = await client.get("/mail", follow_redirects=False)
        assert blocked.status_code == 303
        assert str(blocked.headers.get("location", "")).startswith("/mail/login")


@pytest.mark.asyncio
async def test_mail_login_success_and_logout(isolated_env, monkeypatch) -> None:
    client, settings = await _build_client(monkeypatch)
    async with client:
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
        assert submitted.headers.get("location") == "/mail"
        home = await client.get("/mail")
        assert home.status_code == 200
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
async def test_mail_api_requires_csrf_after_login(isolated_env, monkeypatch) -> None:
    client, _settings = await _build_client(monkeypatch)
    async with client:
        login_page = await client.get("/mail/login")
        csrf = _csrf_from_html(login_page.text)
        await client.post(
            "/mail/login",
            data={
                "username": "operator",
                "password": "ui-secret-ok",
                "csrf_token": csrf,
                "next": "/mail",
            },
            headers={"Origin": "http://test"},
        )
        denied = await client.post("/mail/api/delete-messages", json={"ids": [1]})
        assert denied.status_code == 403
        home = await client.get("/mail")
        meta = _CSRF_META_RE.search(home.text)
        assert meta is not None
        still_denied = await client.post(
            "/mail/api/delete-messages",
            json={"ids": [1]},
            headers={"X-CSRF-Token": meta.group(1)},
        )
        assert still_denied.status_code == 403
        allowed = await client.post(
            "/mail/api/delete-messages",
            json={"ids": [1]},
            headers={"Origin": "http://test", "X-CSRF-Token": meta.group(1)},
        )
        assert allowed.status_code != 403


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
