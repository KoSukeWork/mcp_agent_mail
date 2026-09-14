"""Cookie-session authentication for the human mail UI.

MCP Streamable HTTP keeps using the static bearer token and/or JWT. The
``/mail`` interface uses an independent operator password and a signed
HttpOnly session cookie so browsers are not blocked by machine tokens.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import importlib
import json
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
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.types import ASGIApp

from .config import MAIL_UI_USERNAME_RE, Settings

MAIL_UI_SESSION_COOKIE: Final = "agent_mail_ui_session"
MAIL_UI_LOGIN_CSRF_COOKIE: Final = "agent_mail_ui_login_csrf"
MAIL_UI_LOGIN_PATH: Final = "/mail/login"
MAIL_UI_LOGOUT_PATH: Final = "/mail/logout"
MAIL_UI_COOKIE_PATH: Final = "/mail"
SESSION_VERSION: Final = 1
_SESSION_PURPOSE: Final = b"mcp-agent-mail-ui-session-v1"
_UNSAFE_METHODS: Final = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_LOGIN_BACKOFF_AFTER_FAILURES: Final = 5
_LOGIN_BACKOFF_MAX_SECONDS: Final = 16.0
_LOGIN_WINDOW_SECONDS: Final = 60.0
_LOGIN_STATE_TTL_SECONDS: Final = 900.0
_LOGIN_MAX_TRACKED_CLIENTS: Final = 10_000


@dataclass(frozen=True, slots=True)
class MailUISession:
    """Verified human mail-UI session."""

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
    ) -> None:
        self._per_minute = max(0, int(per_minute))
        self._max_entries = max(1, int(max_entries))
        self._state_ttl_seconds = max(_LOGIN_WINDOW_SECONDS, float(state_ttl_seconds))
        self._monotonic = monotonic
        self._states: dict[str, _LoginAttemptState] = {}
        self._last_cleanup = monotonic()
        self._redis: Any | None = None
        if redis_url:
            try:
                redis_asyncio = importlib.import_module("redis.asyncio")
                self._redis = redis_asyncio.Redis.from_url(redis_url)
            except Exception as exc:
                raise RuntimeError("Redis login rate limiting backend is unavailable") from exc

    @property
    def entry_count(self) -> int:
        """Return tracked client count for diagnostics and bounded-state tests."""

        return len(self._states)

    async def allow(self, client_ip: str) -> bool:
        if self._per_minute <= 0:
            return True
        if self._redis is not None:
            result = await self._redis.eval(
                """
                local key = KEYS[1]
                local now = tonumber(redis.call('TIME')[1])
                local limit = tonumber(ARGV[1])
                local ttl = tonumber(ARGV[2])
                local state = redis.call('HMGET', key, 'window_started', 'attempts', 'blocked_until')
                local started = tonumber(state[1]) or now
                local attempts = tonumber(state[2]) or 0
                local blocked_until = tonumber(state[3]) or 0
                if now < blocked_until then
                    redis.call('EXPIRE', key, ttl)
                    return 0
                end
                if now - started >= 60 then
                    started = now
                    attempts = 0
                end
                if attempts >= limit then
                    redis.call('EXPIRE', key, ttl)
                    return 0
                end
                redis.call('HSET', key,
                    'window_started', started,
                    'attempts', attempts + 1,
                    'last_seen', now)
                redis.call('EXPIRE', key, ttl)
                return 1
                """,
                1,
                self._redis_key(client_ip),
                self._per_minute,
                int(self._state_ttl_seconds),
            )
            return bool(result)
        now = self._monotonic()
        self._cleanup_if_needed(now)
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
        return True

    async def record_failure(self, client_ip: str) -> None:
        if self._per_minute <= 0:
            return
        if self._redis is not None:
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
            await self._redis.delete(self._redis_key(client_ip))
            return
        self._states.pop(client_ip, None)

    async def close(self) -> None:
        if self._redis is not None:
            close = getattr(self._redis, "aclose", None)
            if close is not None:
                await close()

    @staticmethod
    def _redis_key(client_ip: str) -> str:
        digest = hashlib.sha256(client_ip.encode("utf-8")).hexdigest()
        return f"mcp-agent-mail:mail-ui-login:{digest}"

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

    return bool((settings.http.mail_ui_password or "").strip())


def current_mail_ui_username() -> str | None:
    session = _current_mail_ui_session.get()
    return None if session is None else session.username


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
    password = (settings.http.mail_ui_password or "").strip()
    if not explicit or not password:
        return None
    return hmac.new(
        explicit.encode("utf-8"),
        _SESSION_PURPOSE
        + b"\0"
        + username.encode("utf-8")
        + b"\0"
        + password.encode("utf-8"),
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
    username = payload_obj.get("u")
    csrf = payload_obj.get("csrf")
    expires_at = payload_obj.get("exp")
    version = payload_obj.get("v")
    if version != SESSION_VERSION:
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
    return MailUISession(username=username, csrf=csrf, expires_at=expires_at)


def verify_mail_ui_credentials(settings: Settings, username: str, password: str) -> bool:
    """Constant-time comparison of the configured operator credentials."""

    expected_user = _configured_username(settings)
    expected_password = (settings.http.mail_ui_password or "").strip()
    provided_user = username.strip()
    provided_password = password
    if not expected_password or not provided_password or len(provided_password) > 1024:
        _consteq(expected_user, provided_user)
        return False
    user_ok = _consteq(expected_user, provided_user)
    password_ok = _consteq(expected_password, provided_password)
    return user_ok and password_ok


def issue_mail_ui_session(
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
    csrf = secrets.token_urlsafe(32)
    expires_at = _now_ts() + int(settings.http.mail_ui_session_ttl_seconds)
    session = MailUISession(username=username, csrf=csrf, expires_at=expires_at)
    token = sign_session_payload(
        {"v": SESSION_VERSION, "u": username, "csrf": csrf, "exp": expires_at},
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
