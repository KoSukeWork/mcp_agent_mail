"""Mail UI cookie login is independent from MCP bearer tokens."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from mcp_agent_mail import config as _config, http as http_module, ui_auth as ui_auth_module
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.config import ConfigError
from mcp_agent_mail.db import ensure_schema, get_session
from mcp_agent_mail.http import build_http_app
from mcp_agent_mail.models import MailUISessionRecord
from mcp_agent_mail.ui_auth import (
    MAIL_UI_SESSION_COOKIE,
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


def test_templates_avoid_removed_tailwind_v4_utilities() -> None:
    templates = Path(__file__).parents[1] / "src/mcp_agent_mail/templates"
    removed_prefixes = (
        "flex-shrink-",
        "flex-grow-",
        "overflow-ellipsis",
        "bg-opacity-",
        "text-opacity-",
        "border-opacity-",
        "divide-opacity-",
        "ring-opacity-",
        "placeholder-opacity-",
    )
    violations = [
        f"{path.name}: {utility}"
        for path in templates.glob("*.html")
        for utility in removed_prefixes
        if utility in path.read_text(encoding="utf-8")
    ]
    assert violations == []


def test_templates_nonce_every_inline_script() -> None:
    templates_root = Path(__file__).parents[1] / "src/mcp_agent_mail/templates"
    for template in templates_root.glob("*.html"):
        contents = template.read_text(encoding="utf-8")
        inline_scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>", contents)
        assert all('nonce="{{ csp_nonce }}"' in tag for tag in inline_scripts), template


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


@pytest.mark.asyncio
async def test_login_language_links_preserve_encoded_next_query(
    isolated_env, monkeypatch
) -> None:
    client, _settings = await _build_client(monkeypatch)
    async with client:
        response = await client.get(
            "/mail/login",
            params={"next": "/mail?category=trash&query=foo"},
        )
        assert response.status_code == 200
        assert "next=/mail%3Fcategory%3Dtrash%26query%3Dfoo&amp;lang=en" in response.text


def test_session_cookie_roundtrip_and_tamper(isolated_env, monkeypatch) -> None:
    monkeypatch.setenv("MAIL_UI_PASSWORD", "ui-secret-ok")
    monkeypatch.setenv("MAIL_UI_SESSION_SECRET", _SESSION_SECRET)
    _config.clear_settings_cache()
    settings = _config.get_settings()
    secret = session_signing_key(settings)
    assert secret is not None
    token = sign_session_payload(
        {
            "v": 2,
            "sid": "s" * 43,
            "u": "operator",
            "csrf": "c" * 32,
            "exp": 2_000_000_000,
        },
        secret=secret,
    )
    session = parse_session_cookie(token, secret=secret, now=1_900_000_000)
    assert session is not None
    assert session.username == "operator"
    tampered = ("a" + token[1:]) if token[0] != "a" else ("b" + token[1:])
    assert parse_session_cookie(tampered, secret=secret, now=1_900_000_000) is None
    expired = sign_session_payload(
        {"v": 2, "sid": "s" * 43, "u": "operator", "csrf": "c" * 32, "exp": 10},
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
        {
            "v": 2,
            "sid": "s" * 43,
            "u": "operator",
            "csrf": "c" * 32,
            "exp": 2_000_000_000,
        },
        secret=old_key,
    )

    monkeypatch.setenv("MAIL_UI_PASSWORD", "new-password")
    _config.clear_settings_cache()
    new_key = session_signing_key(_config.get_settings())
    assert new_key is not None
    assert new_key != old_key
    assert parse_session_cookie(token, secret=new_key, now=1_900_000_000) is None


def test_username_rotation_revokes_existing_session(isolated_env, monkeypatch) -> None:
    monkeypatch.setenv("MAIL_UI_USERNAME", "old-user")
    monkeypatch.setenv("MAIL_UI_PASSWORD", "ui-secret-ok")
    monkeypatch.setenv("MAIL_UI_SESSION_SECRET", _SESSION_SECRET)
    _config.clear_settings_cache()
    old_key = session_signing_key(_config.get_settings())
    assert old_key is not None
    token = sign_session_payload(
        {
            "v": 2,
            "sid": "s" * 43,
            "u": "old-user",
            "csrf": "c" * 32,
            "exp": 2_000_000_000,
        },
        secret=old_key,
    )

    monkeypatch.setenv("MAIL_UI_USERNAME", "new-user")
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


@pytest.mark.parametrize("password", ["short", " " * 12])
def test_configured_mail_ui_password_must_be_strong(
    isolated_env,
    monkeypatch,
    password: str,
) -> None:
    monkeypatch.setenv("MAIL_UI_PASSWORD", password)
    monkeypatch.setenv("MAIL_UI_SESSION_SECRET", _SESSION_SECRET)
    _config.clear_settings_cache()
    with pytest.raises(ConfigError, match="MAIL_UI_PASSWORD"):
        _config.get_settings()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MAIL_UI_LOGIN_RATE_LIMIT_PER_MINUTE", "-1"),
        ("MAIL_UI_LOGIN_GLOBAL_RATE_LIMIT_PER_HOUR", "-1"),
        ("MAIL_UI_SESSION_TTL_SECONDS", "299"),
        ("MAIL_UI_SESSION_TTL_SECONDS", "2592001"),
        ("HTTP_RATE_LIMIT_REDIS_CONNECT_TIMEOUT_SECONDS", "0"),
        ("HTTP_RATE_LIMIT_REDIS_SOCKET_TIMEOUT_SECONDS", "31"),
        ("MAIL_UI_LOGIN_MAX_TRACKED_CLIENTS", "99"),
    ],
)
def test_mail_ui_security_limits_reject_out_of_range_values(
    isolated_env,
    monkeypatch,
    name: str,
    value: str,
) -> None:
    monkeypatch.setenv(name, value)
    _config.clear_settings_cache()
    with pytest.raises(ConfigError, match=name):
        _config.get_settings()


def test_redis_rate_limit_prefix_rejects_unsafe_characters(
    isolated_env,
    monkeypatch,
) -> None:
    monkeypatch.setenv("HTTP_RATE_LIMIT_REDIS_PREFIX", "mail ui/production")
    _config.clear_settings_cache()
    with pytest.raises(ConfigError, match="HTTP_RATE_LIMIT_REDIS_PREFIX"):
        _config.get_settings()


def test_redis_rate_limit_backend_requires_url(isolated_env, monkeypatch) -> None:
    monkeypatch.setenv("HTTP_RATE_LIMIT_BACKEND", "redis")
    monkeypatch.setenv("HTTP_RATE_LIMIT_REDIS_URL", "")
    _config.clear_settings_cache()
    with pytest.raises(ConfigError, match="HTTP_RATE_LIMIT_REDIS_URL"):
        _config.get_settings()


def test_session_ttl_rotation_revokes_existing_session(isolated_env, monkeypatch) -> None:
    monkeypatch.setenv("MAIL_UI_PASSWORD", "ui-secret-ok")
    monkeypatch.setenv("MAIL_UI_SESSION_SECRET", _SESSION_SECRET)
    _config.clear_settings_cache()
    old_key = session_signing_key(_config.get_settings())
    assert old_key is not None
    token = sign_session_payload(
        {
            "v": 2,
            "sid": "s" * 43,
            "u": "operator",
            "csrf": "c" * 32,
            "exp": 2_000_000_000,
        },
        secret=old_key,
    )

    monkeypatch.setenv("MAIL_UI_SESSION_TTL_SECONDS", "300")
    _config.clear_settings_cache()
    new_key = session_signing_key(_config.get_settings())
    assert new_key is not None
    assert new_key != old_key
    assert parse_session_cookie(token, secret=new_key, now=1_900_000_000) is None


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
    await ensure_schema(settings)
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
        login_csp = response.headers["content-security-policy"]
        assert "unsafe-inline" not in login_csp
        assert "unsafe-eval" not in login_csp
        login_nonce = re.search(r"style-src 'nonce-([^']+)'", login_csp)
        assert login_nonce is not None
        assert f'nonce="{login_nonce.group(1)}"' in response.text
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
        assert "unsafe-inline" not in home.headers["content-security-policy"].split("style-src", 1)[0]
        assert "unsafe-eval" not in home.headers["content-security-policy"]
        script_nonce = re.search(
            r"script-src 'nonce-([^']+)'",
            home.headers["content-security-policy"],
        )
        assert script_nonce is not None
        assert f'nonce="{script_nonce.group(1)}"' in home.text
        assert home.headers["x-frame-options"] == "DENY"
        assert 'href="/mail/assets/mail-tailwind.css"' in home.text
        assert "@tailwindcss/browser" not in home.text
        stylesheet = await client.get("/mail/assets/mail-tailwind.css")
        assert stylesheet.status_code == 200
        assert stylesheet.headers["content-type"].startswith("text/css")
        assert stylesheet.headers["cross-origin-resource-policy"] == "same-origin"
        assert ".shrink-0" in stylesheet.text
        assert ".bg-primary-500" in stylesheet.text
        for asset_name in ("alpine-collapse.min.js", "alpine-csp.min.js"):
            script_asset = await client.get(f"/mail/assets/{asset_name}")
            assert script_asset.status_code == 200
            assert script_asset.headers["content-type"].startswith("text/javascript")
        assert "alpine-csp.min.js" in home.text
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
        captured_session = client.cookies.get(MAIL_UI_SESSION_COOKIE)
        assert captured_session
        async with get_session() as db_session:
            records = (await db_session.scalars(select(MailUISessionRecord))).all()
        assert len(records) == 1
        assert len(records[0].session_id_hash) == 64
        assert records[0].session_id_hash not in captured_session
        assert records[0].revoked_at is None
        logout = await client.post(
            "/mail/logout",
            data={"csrf_token": meta.group(1)},
            headers={"Origin": "http://test"},
            follow_redirects=False,
        )
        assert logout.status_code == 303
        async with get_session() as db_session:
            revoked = await db_session.get(
                MailUISessionRecord,
                records[0].session_id_hash,
            )
        assert revoked is not None
        assert revoked.revoked_at is not None
        again = await client.get("/mail", follow_redirects=False)
        assert again.status_code == 303
        client.cookies.set(
            MAIL_UI_SESSION_COOKIE,
            captured_session,
            path="/mail",
        )
        replay = await client.get("/mail", follow_redirects=False)
        assert replay.status_code == 303


@pytest.mark.asyncio
async def test_mail_ui_session_database_failures_fail_closed(isolated_env, monkeypatch) -> None:
    client, _settings = await _build_client(monkeypatch)
    async with client:
        await _sign_in(client)

        async def fail_validation(_session):
            raise RuntimeError("database unavailable")

        monkeypatch.setattr(ui_auth_module, "mail_ui_session_is_active", fail_validation)
        response = await client.get("/mail", follow_redirects=False)
        assert response.status_code == 503


@pytest.mark.asyncio
async def test_mail_ui_session_creation_failure_returns_503(isolated_env, monkeypatch) -> None:
    async def fail_issue(*_args, **_kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(http_module, "issue_mail_ui_session", fail_issue)
    client, _settings = await _build_client(monkeypatch)
    async with client:
        page = await client.get("/mail/login")
        response = await client.post(
            "/mail/login",
            data={
                "username": "operator",
                "password": "ui-secret-ok",
                "csrf_token": _csrf_from_html(page.text),
            },
            headers={"Origin": "http://test"},
        )
        assert response.status_code == 503
        assert client.cookies.get(MAIL_UI_SESSION_COOKIE) is None


@pytest.mark.asyncio
async def test_mail_ui_logout_revocation_failure_returns_503(isolated_env, monkeypatch) -> None:
    client, _settings = await _build_client(monkeypatch)
    async with client:
        await _sign_in(client)
        home = await client.get("/mail")
        csrf = _CSRF_META_RE.search(home.text)
        assert csrf is not None

        async def fail_revoke(_session):
            raise RuntimeError("database unavailable")

        monkeypatch.setattr(http_module, "revoke_mail_ui_session", fail_revoke)
        response = await client.post(
            "/mail/logout",
            data={"csrf_token": csrf.group(1)},
            headers={"Origin": "http://test"},
            follow_redirects=False,
        )
        assert response.status_code == 503
        assert client.cookies.get(MAIL_UI_SESSION_COOKIE)


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


@pytest.mark.asyncio
async def test_login_attempt_limiter_bounds_and_expires_state() -> None:
    now = [0.0]
    limiter = LoginAttemptLimiter(
        per_minute=10,
        max_entries=2,
        state_ttl_seconds=60.0,
        monotonic=lambda: now[0],
    )

    assert await limiter.allow("192.0.2.1") is True
    assert await limiter.allow("192.0.2.2") is True
    assert await limiter.allow("192.0.2.3") is False
    assert limiter.entry_count == 2

    now[0] = 61.0
    assert await limiter.allow("192.0.2.3") is True
    assert limiter.entry_count == 1


@pytest.mark.asyncio
async def test_login_attempt_limiter_applies_global_hourly_budget() -> None:
    now = [0.0]
    limiter = LoginAttemptLimiter(
        per_minute=0,
        global_per_hour=2,
        monotonic=lambda: now[0],
    )

    assert await limiter.allow("192.0.2.1") is True
    assert await limiter.allow("192.0.2.2") is True
    assert await limiter.allow("192.0.2.3") is False
    now[0] = 3601.0
    assert await limiter.allow("192.0.2.3") is True


@pytest.mark.asyncio
async def test_login_attempt_limiter_uses_shared_redis_backend(monkeypatch) -> None:
    class FakeRedis:
        def __init__(self) -> None:
            self.eval_calls: list[tuple[object, ...]] = []
            self.closed = False

        async def eval(self, *args):
            self.eval_calls.append(args)
            return 1

        async def aclose(self) -> None:
            self.closed = True

    fake_redis = FakeRedis()
    redis_options: dict[str, object] = {}

    class FakeRedisFactory:
        @staticmethod
        def from_url(url: str, **kwargs: object) -> FakeRedis:
            assert url == "redis://rate-limit.example/0"
            redis_options.update(kwargs)
            return fake_redis

    monkeypatch.setattr(
        "mcp_agent_mail.ui_auth.importlib.import_module",
        lambda name: SimpleNamespace(Redis=FakeRedisFactory),
    )
    limiter = LoginAttemptLimiter(
        per_minute=10,
        redis_url="redis://rate-limit.example/0",
        redis_prefix="mail-prod",
    )

    assert await limiter.allow("203.0.113.7") is True
    await limiter.record_failure("203.0.113.7")
    await limiter.record_success("203.0.113.7")
    await limiter.close()

    assert len(fake_redis.eval_calls) == 3
    first_call = fake_redis.eval_calls[0]
    assert first_call[1] == 3
    assert all("{mail-prod:mail-ui-login}" in str(key) for key in first_call[2:5])
    assert all("203.0.113.7" not in str(key) for key in first_call[2:5])
    assert redis_options["socket_connect_timeout"] == 2.0
    assert redis_options["socket_timeout"] == 2.0
    assert redis_options["health_check_interval"] == 30
    assert redis_options["retry_on_timeout"] is False
    assert fake_redis.closed is True


@pytest.mark.asyncio
async def test_hung_redis_login_limiter_returns_503_promptly(isolated_env, monkeypatch) -> None:
    class HungRedis:
        async def eval(self, *_args):
            await asyncio.Event().wait()

    client, _settings = await _build_client(monkeypatch)
    limiter = cast(Any, client._transport).app.state.mail_ui_login_limiter
    limiter._redis = HungRedis()
    limiter._redis_operation_timeout_seconds = 0.05

    async with client:
        page = await client.get("/mail/login")
        response = await client.post(
            "/mail/login",
            data={
                "username": "operator",
                "password": "wrong-password",
                "csrf_token": _csrf_from_html(page.text),
            },
            headers={"Origin": "http://test"},
        )
    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"


@pytest.mark.asyncio
async def test_mail_login_rejects_oversized_form(isolated_env, monkeypatch) -> None:
    client, _settings = await _build_client(monkeypatch)
    async with client:
        response = await client.post(
            "/mail/login",
            content=b"password=" + (b"x" * 8192),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "http://test",
            },
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
            headers={"Origin": "http://test"},
        )
        assert first.status_code == 200

        limited = await client.post(
            "/mail/login",
            content=b"password=" + (b"x" * 8192),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "http://test",
            },
        )
        assert limited.status_code == 429


@pytest.mark.asyncio
async def test_cross_site_login_does_not_consume_rate_limit(
    isolated_env, monkeypatch
) -> None:
    monkeypatch.setenv("MAIL_UI_LOGIN_RATE_LIMIT_PER_MINUTE", "1")
    client, _settings = await _build_client(monkeypatch)
    async with client:
        attack = await client.post(
            "/mail/login",
            data={"username": "operator", "password": "wrong"},
            headers={"Origin": "https://evil.example"},
        )
        assert attack.status_code == 403

        page = await client.get("/mail/login")
        legitimate = await client.post(
            "/mail/login",
            data={
                "username": "operator",
                "password": "ui-secret-ok",
                "next": "/mail",
                "csrf_token": _csrf_from_html(page.text),
            },
            headers={"Origin": "http://test"},
            follow_redirects=False,
        )
        assert legitimate.status_code == 303


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
