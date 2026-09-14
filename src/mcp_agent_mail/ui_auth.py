"""Cookie-session authentication for the human mail UI.

MCP Streamable HTTP keeps using the static bearer token and/or JWT. The
``/mail`` interface uses an independent operator password and a signed
HttpOnly session cookie so browsers are not blocked by machine tokens.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import importlib
import json
import re
import secrets
import time
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from ipaddress import ip_address
from typing import Any, Final
from urllib.parse import quote

from fastapi import Request, status
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import delete
from sqlmodel import col
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.types import ASGIApp

from .config import MAIL_UI_USERNAME_RE, Settings
from .db import get_session
from .models import MailUISessionRecord

MAIL_UI_SESSION_COOKIE: Final = "agent_mail_ui_session"
MAIL_UI_LOGIN_CSRF_COOKIE: Final = "agent_mail_ui_login_csrf"
MAIL_UI_LOGIN_PATH: Final = "/mail/login"
MAIL_UI_LOGOUT_PATH: Final = "/mail/logout"
MAIL_UI_COOKIE_PATH: Final = "/mail"
SESSION_VERSION: Final = 2
_SESSION_PURPOSE: Final = b"mcp-agent-mail-ui-session-v2"
_UNSAFE_METHODS: Final = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_LOGIN_BACKOFF_AFTER_FAILURES: Final = 5
_LOGIN_BACKOFF_MAX_SECONDS: Final = 16.0
_LOGIN_WINDOW_SECONDS: Final = 60.0
_LOGIN_GLOBAL_WINDOW_SECONDS: Final = 3600.0
_LOGIN_STATE_TTL_SECONDS: Final = 900.0
_LOGIN_MAX_TRACKED_CLIENTS: Final = 10_000
_SESSION_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


def create_rate_limit_redis_client(
    redis_url: str,
    *,
    connect_timeout_seconds: float,
    socket_timeout_seconds: float,
) -> Any:
    """Create a bounded Redis client shared by HTTP and login limiters."""

    try:
        redis_asyncio = importlib.import_module("redis.asyncio")
        return redis_asyncio.Redis.from_url(
            redis_url,
            socket_connect_timeout=max(0.1, float(connect_timeout_seconds)),
            socket_timeout=max(0.1, float(socket_timeout_seconds)),
            health_check_interval=30,
            retry_on_timeout=False,
        )
    except Exception as exc:
        raise RuntimeError("Redis rate limiting backend is unavailable") from exc


@dataclass(frozen=True, slots=True)
class MailUISession:
    """Verified human mail-UI session."""

    session_id: str
    username: str
    csrf: str
    expires_at: int


@dataclass(slots=True)
class _LoginAttemptState:
    window_started: float
    last_seen: float
    attempts: int = 0
    failures: int = 0
    blocked_until: float = 0.0


_current_mail_ui_session: ContextVar[MailUISession | None] = ContextVar(
    "current_mail_ui_session",
    default=None,
)


class LoginAttemptLimiter:
    """Bounded per-IP login limiter with expiry and repeated-failure backoff."""

    def __init__(
        self,
        *,
        per_minute: int,
        max_entries: int = _LOGIN_MAX_TRACKED_CLIENTS,
        state_ttl_seconds: float = _LOGIN_STATE_TTL_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        redis_url: str | None = None,
        redis_prefix: str = "mcp-agent-mail",
        redis_connect_timeout_seconds: float = 2.0,
        redis_socket_timeout_seconds: float = 2.0,
        global_per_hour: int = 200,
        redis_client: Any | None = None,
    ) -> None:
        self._per_minute = max(0, int(per_minute))
        self._global_per_hour = max(0, int(global_per_hour))
        self._max_entries = max(1, int(max_entries))
        self._state_ttl_seconds = max(_LOGIN_WINDOW_SECONDS, float(state_ttl_seconds))
        self._redis_operation_timeout_seconds = max(
            0.1,
            float(redis_socket_timeout_seconds),
        )
        self._monotonic = monotonic
        self._states: dict[str, _LoginAttemptState] = {}
        self._last_cleanup = monotonic()
        self._global_window_started = self._last_cleanup
        self._global_attempts = 0
        self._redis_namespace = f"{{{redis_prefix}:mail-ui-login}}"
        self._redis: Any | None = redis_client
        self._owns_redis = redis_client is None and bool(redis_url)
        if self._redis is None and redis_url:
            self._redis = create_rate_limit_redis_client(
                redis_url,
                connect_timeout_seconds=redis_connect_timeout_seconds,
                socket_timeout_seconds=redis_socket_timeout_seconds,
            )

    @property
    def entry_count(self) -> int:
        """Return tracked client count for diagnostics and bounded-state tests."""

        return len(self._states)

    async def allow(self, client_ip: str) -> bool:
        if self._per_minute <= 0 and self._global_per_hour <= 0:
            return True
        if self._redis is not None:
            async with asyncio.timeout(self._redis_operation_timeout_seconds):
                result = await self._redis.eval(
                """
                local ip_key = KEYS[1]
                local clients_key = KEYS[2]
                local global_key = KEYS[3]
                local now = tonumber(redis.call('TIME')[1])
                local ip_limit = tonumber(ARGV[1])
                local ttl = tonumber(ARGV[2])
                local max_clients = tonumber(ARGV[3])
                local global_limit = tonumber(ARGV[4])
                redis.call('ZREMRANGEBYSCORE', clients_key, '-inf', now - ttl)
                if redis.call('EXISTS', ip_key) == 0 and redis.call('ZCARD', clients_key) >= max_clients then
                    return 0
                end
                local state = redis.call('HMGET', ip_key, 'window_started', 'attempts', 'blocked_until')
                local started = tonumber(state[1]) or now
                local attempts = tonumber(state[2]) or 0
                local blocked_until = tonumber(state[3]) or 0
                if now < blocked_until then
                    return 0
                end
                if now - started >= 60 then
                    started = now
                    attempts = 0
                end
                if ip_limit > 0 and attempts >= ip_limit then
                    return 0
                end
                local global_state = redis.call('HMGET', global_key, 'window_started', 'attempts')
                local global_started = tonumber(global_state[1]) or now
                local global_attempts = tonumber(global_state[2]) or 0
                if now - global_started >= 3600 then
                    global_started = now
                    global_attempts = 0
                end
                if global_limit > 0 and global_attempts >= global_limit then
                    return 0
                end
                redis.call('HSET', ip_key,
                    'window_started', started,
                    'attempts', attempts + (ip_limit > 0 and 1 or 0),
                    'last_seen', now)
                redis.call('EXPIRE', ip_key, ttl)
                redis.call('ZADD', clients_key, now, ip_key)
                redis.call('EXPIRE', clients_key, ttl)
                if global_limit > 0 then
                    redis.call('HSET', global_key,
                        'window_started', global_started,
                        'attempts', global_attempts + 1)
                    redis.call('EXPIRE', global_key, 3600)
                end
                return 1
                """,
                3,
                self._redis_key(client_ip),
                self._redis_clients_key(),
                self._redis_global_key(),
                self._per_minute,
                int(self._state_ttl_seconds),
                self._max_entries,
                self._global_per_hour,
            )
            return bool(result)
        now = self._monotonic()
        self._cleanup_if_needed(now)
        if now - self._global_window_started >= _LOGIN_GLOBAL_WINDOW_SECONDS:
            self._global_window_started = now
            self._global_attempts = 0
        if self._global_per_hour > 0 and self._global_attempts >= self._global_per_hour:
            return False
        if self._per_minute <= 0:
            self._global_attempts += 1
            return True
        state = self._states.get(client_ip)
        if state is None:
            if len(self._states) >= self._max_entries:
                return False
            state = _LoginAttemptState(window_started=now, last_seen=now)
            self._states[client_ip] = state
        state.last_seen = now
        if now < state.blocked_until:
            return False
        if now - state.window_started >= _LOGIN_WINDOW_SECONDS:
            state.window_started = now
            state.attempts = 0
        if state.attempts >= self._per_minute:
            return False
        state.attempts += 1
        if self._global_per_hour > 0:
            self._global_attempts += 1
        return True

    async def record_failure(self, client_ip: str) -> None:
        if self._per_minute <= 0:
            return
        if self._redis is not None:
            async with asyncio.timeout(self._redis_operation_timeout_seconds):
                await self._redis.eval(
                """
                local key = KEYS[1]
                local now = tonumber(redis.call('TIME')[1])
                local ttl = tonumber(ARGV[1])
                local failures = redis.call('HINCRBY', key, 'failures', 1)
                redis.call('HSET', key, 'last_seen', now)
                if redis.call('HEXISTS', key, 'window_started') == 0 then
                    redis.call('HSET', key, 'window_started', now, 'attempts', 1)
                end
                if failures >= 5 then
                    local exponent = math.min(failures - 4, 4)
                    local delay = math.min(16, 2 ^ exponent)
                    redis.call('HSET', key, 'blocked_until', now + delay)
                end
                redis.call('EXPIRE', key, ttl)
                return failures
                """,
                1,
                self._redis_key(client_ip),
                int(self._state_ttl_seconds),
            )
            return
        now = self._monotonic()
        state = self._states.get(client_ip)
        if state is None:
            if len(self._states) >= self._max_entries:
                return
            state = _LoginAttemptState(window_started=now, last_seen=now)
            self._states[client_ip] = state
        state.last_seen = now
        state.failures += 1
        if state.failures >= _LOGIN_BACKOFF_AFTER_FAILURES:
            delay = float(min(_LOGIN_BACKOFF_MAX_SECONDS, 2 ** min(state.failures - 4, 4)))
            state.blocked_until = now + delay

    async def record_success(self, client_ip: str) -> None:
        if self._redis is not None:
            async with asyncio.timeout(self._redis_operation_timeout_seconds):
                await self._redis.eval(
                """
                redis.call('ZREM', KEYS[2], KEYS[1])
                return redis.call('DEL', KEYS[1])
                """,
                2,
                self._redis_key(client_ip),
                self._redis_clients_key(),
            )
            return
        self._states.pop(client_ip, None)

    async def close(self) -> None:
        if self._redis is not None and self._owns_redis:
            close = getattr(self._redis, "aclose", None)
            if close is not None:
                async with asyncio.timeout(self._redis_operation_timeout_seconds):
                    await close()

    def _redis_key(self, client_ip: str) -> str:
        digest = hashlib.sha256(client_ip.encode("utf-8")).hexdigest()
        return f"{self._redis_namespace}:ip:{digest}"

    def _redis_clients_key(self) -> str:
        return f"{self._redis_namespace}:clients"

    def _redis_global_key(self) -> str:
        return f"{self._redis_namespace}:global"

    def _cleanup_if_needed(self, now: float) -> None:
        if (
            now - self._last_cleanup < _LOGIN_WINDOW_SECONDS
            and len(self._states) < self._max_entries
        ):
            return
        stale_before = now - self._state_ttl_seconds
        stale_clients = [
            client_ip
            for client_ip, state in self._states.items()
            if state.last_seen <= stale_before
        ]
        for client_ip in stale_clients:
            self._states.pop(client_ip, None)
        self._last_cleanup = now


def is_mail_ui_path(path: str) -> bool:
    """Return whether ``path`` is the human mail UI, including login."""

    return path == "/mail" or path.startswith("/mail/")


def is_mail_login_path(path: str) -> bool:
    return path == MAIL_UI_LOGIN_PATH


def is_mail_logout_path(path: str) -> bool:
    return path == MAIL_UI_LOGOUT_PATH


def credentials_configured(settings: Settings) -> bool:
    """Return whether a human mail-UI password is configured."""

    return bool(settings.http.mail_ui_password)


def current_mail_ui_username() -> str | None:
    session = _current_mail_ui_session.get()
    return None if session is None else session.username


def current_mail_ui_session() -> MailUISession | None:
    """Return the request's verified Mail UI session, if any."""

    return _current_mail_ui_session.get()


def current_mail_ui_csrf() -> str | None:
    session = _current_mail_ui_session.get()
    return None if session is None else session.csrf


def sanitize_mail_next_path(raw: str | None) -> str:
    """Allow only relative ``/mail`` paths; reject open redirects."""

    candidate = (raw or "").strip() or "/mail"
    if any(control in candidate for control in ("\\", "\r", "\n", "\x00")):
        return "/mail"
    if "://" in candidate or candidate.startswith("//"):
        return "/mail"
    if not candidate.startswith("/mail"):
        return "/mail"
    if len(candidate) > 5 and candidate[5] not in "/?#":
        return "/mail"
    return candidate


def session_signing_key(settings: Settings) -> bytes | None:
    """Return a cookie key bound to the current operator credentials."""

    explicit = (settings.http.mail_ui_session_secret or "").strip()
    username = _configured_username(settings)
    password = settings.http.mail_ui_password or ""
    if not explicit or not password:
        return None
    return hmac.new(
        explicit.encode("utf-8"),
        _SESSION_PURPOSE
        + b"\0"
        + username.encode("utf-8")
        + b"\0"
        + password.encode("utf-8")
        + b"\0"
        + str(settings.http.mail_ui_session_ttl_seconds).encode("ascii"),
        hashlib.sha256,
    ).digest()


def sign_session_payload(payload: dict[str, Any], *, secret: bytes) -> str:
    body = _b64url(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64url(signature)}"


def parse_session_cookie(
    value: str | None, *, secret: bytes, now: int | None = None
) -> MailUISession | None:
    """Return a session if the cookie is authentic, current, and well-formed."""

    if not value or "." not in value:
        return None
    body, _, signature = value.partition(".")
    if not body or not signature:
        return None
    try:
        expected = hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest()
        provided = _unb64url(signature)
    except (ValueError, OSError, UnicodeEncodeError):
        return None
    if not hmac.compare_digest(expected, provided):
        return None
    try:
        payload_obj: Any = json.loads(_unb64url(body).decode("utf-8"))
    except (ValueError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload_obj, dict):
        return None
    session_id = payload_obj.get("sid")
    username = payload_obj.get("u")
    csrf = payload_obj.get("csrf")
    expires_at = payload_obj.get("exp")
    version = payload_obj.get("v")
    if version != SESSION_VERSION:
        return None
    if not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id):
        return None
    if not isinstance(username, str) or not MAIL_UI_USERNAME_RE.fullmatch(username):
        return None
    if not isinstance(csrf, str) or not (16 <= len(csrf) <= 128):
        return None
    if not isinstance(expires_at, int):
        return None
    current = _now_ts() if now is None else now
    if expires_at <= current:
        return None
    return MailUISession(
        session_id=session_id,
        username=username,
        csrf=csrf,
        expires_at=expires_at,
    )


def verify_mail_ui_credentials(settings: Settings, username: str, password: str) -> bool:
    """Constant-time comparison of the configured operator credentials."""

    expected_user = _configured_username(settings)
    expected_password = settings.http.mail_ui_password or ""
    provided_user = username.strip()
    provided_password = password
    if not expected_password or not provided_password or len(provided_password) > 1024:
        _consteq(expected_user, provided_user)
        return False
    user_ok = _consteq(expected_user, provided_user)
    password_ok = _consteq(expected_password, provided_password)
    return user_ok and password_ok


def _session_id_hash(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("ascii")).hexdigest()


async def _register_mail_ui_session(session: MailUISession) -> None:
    now = _now_ts()
    async with get_session() as db_session:
        await db_session.execute(
            delete(MailUISessionRecord).where(col(MailUISessionRecord.expires_at) <= now)
        )
        db_session.add(
            MailUISessionRecord(
                session_id_hash=_session_id_hash(session.session_id),
                username=session.username,
                created_at=now,
                expires_at=session.expires_at,
            )
        )
        await db_session.commit()


async def mail_ui_session_is_active(session: MailUISession) -> bool:
    """Validate a signed cookie against its persistent revocation record."""

    async with get_session() as db_session:
        record = await db_session.get(
            MailUISessionRecord,
            _session_id_hash(session.session_id),
        )
    return bool(
        record is not None
        and record.revoked_at is None
        and record.username == session.username
        and record.expires_at == session.expires_at
        and record.expires_at > _now_ts()
    )


async def revoke_mail_ui_session(session: MailUISession) -> None:
    """Persistently revoke a Mail UI session so copied cookies stop working."""

    async with get_session() as db_session:
        record = await db_session.get(
            MailUISessionRecord,
            _session_id_hash(session.session_id),
        )
        if record is not None and record.revoked_at is None:
            record.revoked_at = _now_ts()
            db_session.add(record)
            await db_session.commit()


async def issue_mail_ui_session(
    response: Response,
    *,
    request: Request,
    settings: Settings,
    username: str,
) -> MailUISession:
    """Write a signed session cookie onto ``response`` and return the session."""

    secret = session_signing_key(settings)
    if secret is None:
        raise RuntimeError("Mail UI session secret is not configured")
    session_id = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    expires_at = _now_ts() + int(settings.http.mail_ui_session_ttl_seconds)
    session = MailUISession(
        session_id=session_id,
        username=username,
        csrf=csrf,
        expires_at=expires_at,
    )
    await _register_mail_ui_session(session)
    token = sign_session_payload(
        {
            "v": SESSION_VERSION,
            "sid": session_id,
            "u": username,
            "csrf": csrf,
            "exp": expires_at,
        },
        secret=secret,
    )
    response.set_cookie(
        MAIL_UI_SESSION_COOKIE,
        token,
        max_age=int(settings.http.mail_ui_session_ttl_seconds),
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(request),
        path=MAIL_UI_COOKIE_PATH,
    )
    return session


def clear_mail_ui_session_cookie(response: Response) -> None:
    response.delete_cookie(MAIL_UI_SESSION_COOKIE, path=MAIL_UI_COOKIE_PATH)


def new_login_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def set_login_csrf_cookie(response: Response, token: str, *, request: Request) -> None:
    response.set_cookie(
        MAIL_UI_LOGIN_CSRF_COOKIE,
        token,
        max_age=600,
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(request),
        path=MAIL_UI_LOGIN_PATH,
    )


def clear_login_csrf_cookie(response: Response) -> None:
    response.delete_cookie(MAIL_UI_LOGIN_CSRF_COOKIE, path=MAIL_UI_LOGIN_PATH)


def verify_login_csrf(request: Request, provided: str) -> bool:
    expected = request.cookies.get(MAIL_UI_LOGIN_CSRF_COOKIE, "")
    return bool(expected) and bool(provided) and _consteq(expected, provided)


def origin_is_same_site(request: Request) -> bool:
    """Fail closed unless Origin or Referer matches this host."""

    expected = f"{request.url.scheme}://{request.url.netloc}"
    origin = request.headers.get("origin", "").strip()
    if origin:
        return hmac.compare_digest(origin.rstrip("/"), expected.rstrip("/"))
    referer = request.headers.get("referer", "").strip()
    if not referer:
        return False
    prefix = expected.rstrip("/") + "/"
    return referer == expected or referer.startswith(prefix)


def login_redirect_url(request: Request) -> str:
    target = request.url.path
    if request.url.query:
        target = f"{target}?{request.url.query}"
    return f"{MAIL_UI_LOGIN_PATH}?next={quote(sanitize_mail_next_path(target), safe='/')}"


class MailUIAuthMiddleware(BaseHTTPMiddleware):
    """Require a cookie session for ``/mail`` without touching MCP HTTP auth."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        super().__init__(app)
        self._settings = settings

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not is_mail_ui_path(request.url.path):
            return await call_next(request)

        secret = session_signing_key(self._settings)
        session = None
        if credentials_configured(self._settings) and secret is not None:
            session = parse_session_cookie(
                request.cookies.get(MAIL_UI_SESSION_COOKIE),
                secret=secret,
            )
        if session is not None and not _consteq(
            _configured_username(self._settings), session.username
        ):
            session = None
        if session is not None:
            try:
                active = await mail_ui_session_is_active(session)
            except Exception:
                return _no_store(
                    JSONResponse(
                        {"detail": "Mail UI session validation is temporarily unavailable"},
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        headers={"Retry-After": "5"},
                    )
                )
            if not active:
                session = None
        token = _current_mail_ui_session.set(session)
        try:
            return _no_store(await self._enforce(request, call_next, session))
        finally:
            _current_mail_ui_session.reset(token)

    async def _enforce(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
        session: MailUISession | None,
    ) -> Response:
        path = request.url.path
        method = request.method.upper()

        if is_mail_login_path(path):
            if method == "GET" and session is not None:
                return RedirectResponse("/mail", status_code=status.HTTP_303_SEE_OTHER)
            return await call_next(request)

        if is_mail_logout_path(path):
            if method in _UNSAFE_METHODS and session is not None:
                csrf_ok = await _csrf_matches(request, session.csrf)
                if not csrf_ok or not origin_is_same_site(request):
                    return _forbidden("CSRF validation failed")
            return await call_next(request)

        if session is not None:
            if method in _UNSAFE_METHODS:
                csrf_ok = await _csrf_matches(request, session.csrf)
                if not csrf_ok or not origin_is_same_site(request):
                    return _forbidden("CSRF validation failed")
            return await call_next(request)

        if credentials_configured(self._settings):
            return _unauthenticated(request)

        if _is_direct_loopback(request) and _request_host_is_loopback(
            request,
            allow_test_host=self._settings.environment.lower() == "test",
        ):
            # No human password configured: keep local-dev GET convenience.
            # Requiring a literal loopback Host prevents DNS rebinding from
            # turning an attacker-controlled origin into an apparent same-origin
            # request to a service bound on localhost.
            if method in _UNSAFE_METHODS and not _loopback_write_allowed(request):
                return _forbidden("Cross-origin mailbox writes are not allowed")
            return await call_next(request)
        return _unauthenticated(
            request,
            message="Mail UI login is not configured. Set MAIL_UI_PASSWORD in the server environment.",
        )


def _loopback_write_allowed(request: Request) -> bool:
    """Allow non-browser clients that omit Origin; reject cross-site browser POSTs."""

    origin = request.headers.get("origin", "").strip()
    referer = request.headers.get("referer", "").strip()
    if origin or referer:
        return origin_is_same_site(request)
    return True


def _unauthenticated(request: Request, message: str = "Authentication required") -> Response:
    if _wants_json(request):
        return JSONResponse({"detail": message}, status_code=status.HTTP_401_UNAUTHORIZED)
    return RedirectResponse(login_redirect_url(request), status_code=status.HTTP_303_SEE_OTHER)


def _forbidden(message: str) -> Response:
    return JSONResponse({"detail": message}, status_code=status.HTTP_403_FORBIDDEN)


def _wants_json(request: Request) -> bool:
    path = request.url.path
    if path.startswith("/mail/api/"):
        return True
    if request.method.upper() in _UNSAFE_METHODS:
        accept = request.headers.get("accept", "")
        return "text/html" not in accept
    accept = request.headers.get("accept", "")
    return "application/json" in accept and "text/html" not in accept


async def _csrf_matches(request: Request, expected: str) -> bool:
    header = request.headers.get("x-csrf-token", "").strip()
    if header:
        return _consteq(expected, header)
    content_type = request.headers.get("content-type", "")
    if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
        form = await request.form()
        provided = str(form.get("csrf_token") or "").strip()
        return bool(provided) and _consteq(expected, provided)
    return False


def _cookie_secure(request: Request) -> bool:
    if request.url.scheme == "https":
        return True
    forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return forwarded == "https"


def _is_direct_loopback(request: Request) -> bool:
    """Return True for a direct localhost client that is not behind a proxy."""

    try:
        client_host = request.client.host if request.client else ""
    except Exception:
        client_host = ""
    if not _is_localhost_host(client_host):
        return False
    headers = request.headers
    return not any(
        name in headers
        for name in ("x-forwarded-for", "x-forwarded-proto", "x-forwarded-host", "forwarded")
    )


def _is_localhost_host(host: str) -> bool:
    if not host:
        return False
    normalized = host.rstrip(".").lower()
    if normalized == "localhost":
        return True
    try:
        address = ip_address(normalized)
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return address.is_loopback or bool(mapped is not None and mapped.is_loopback)


def _configured_username(settings: Settings) -> str:
    return (settings.http.mail_ui_username or "operator").strip() or "operator"


def _request_host_is_loopback(request: Request, *, allow_test_host: bool = False) -> bool:
    """Reject attacker-controlled Host names in passwordless loopback mode."""

    try:
        hostname = (request.url.hostname or "").rstrip(".").lower()
    except (AttributeError, ValueError):
        return False
    return (allow_test_host and hostname == "test") or _is_localhost_host(hostname)


def _now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64url(data: str) -> bytes:
    pad = "=" * ((4 - len(data) % 4) % 4)
    return base64.urlsafe_b64decode(data + pad)


def _consteq(left: str, right: str) -> bool:
    left_b = left.encode("utf-8")
    right_b = right.encode("utf-8")
    if len(left_b) != len(right_b):
        hmac.compare_digest(left_b, left_b)
        return False
    return hmac.compare_digest(left_b, right_b)


def _no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response
