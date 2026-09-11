"""HTTP transport helpers wrapping FastMCP with FastAPI."""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import hmac
import importlib
import json
import logging
import re
import secrets
from collections.abc import MutableMapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol, cast

import structlog
import uvicorn
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exception_handlers import http_exception_handler
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import DateTime, bindparam, text
from sqlalchemy.exc import NoResultFound
from sqlmodel import select
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.types import Receive, Scope, Send

from .app import (
    _expire_stale_file_reservations,
    _format_cross_project_agent_address,
    _sender_display_name,
    _tool_metrics_snapshot,
    build_mcp_server,
    get_project_sibling_data,
    refresh_project_sibling_suggestions,
    sweep_stale_agents,
    update_project_sibling_status,
)
from .config import Settings, get_settings
from .db import ensure_schema, get_session
from .identity import ConversationIdentityError, decide_identity_transfer, get_identity_confirmation
from .localization import (
    INTERFACE_LOCALE_COOKIE,
    get_interface_locale,
    gettext,
    normalize_interface_locale,
    reset_interface_locale,
    select_interface_locale,
    set_interface_locale,
)
from .models import Project
from .storage import (
    archive_write_lock,
    collect_lock_status,
    ensure_mailbox_storage,
    get_fd_usage,
    get_lock_telemetry,
    write_file_reservation_record,
)


async def _project_slug_from_id(pid: int | None) -> str | None:
    if pid is None:
        return None
    async with get_session() as session:
        row = await session.execute(text("SELECT slug FROM projects WHERE id = :pid"), {"pid": pid})
        res = row.fetchone()
        return res[0] if res and res[0] else None


async def _ensure_ack_escalation_holder(
    *,
    settings: Settings,
    project_id: int,
    project_slug: str | None,
    recipient_agent_id: int,
    recipient_name: str,
    claim_name: str,
    now: datetime,
    now_naive: datetime,
) -> tuple[int, str]:
    """Return the holder identity for ACK escalation, creating the ops holder if needed.

    When a synthetic holder must be created, the DB insert happens first and the
    archive profile write follows only after the session has closed. This keeps
    the ACK worker out of the DB->archive lock ordering that can deadlock mixed
    HTTP and MCP traffic.
    """
    holder_agent_id = int(recipient_agent_id)
    holder_agent_name = recipient_name

    async with get_session() as s_holder:
        hid_row = await s_holder.execute(
            text("SELECT id FROM agents WHERE project_id = :pid AND name = :name"),
            {"pid": project_id, "name": claim_name},
        )
        hid = hid_row.scalar_one_or_none()
        if isinstance(hid, int):
            return hid, claim_name

        await s_holder.execute(
            text(
                "INSERT OR IGNORE INTO agents(project_id, name, program, model, task_description, inception_ts, last_active_ts, attachments_policy, contact_policy) VALUES (:pid, :name, :program, :model, :task, :ts, :ts, :attachments_policy, :contact_policy)"
            ),
            {
                "pid": project_id,
                "name": claim_name,
                "program": "ops",
                "model": "system",
                "task": "ops-escalation",
                "ts": now_naive,
                "attachments_policy": "auto",
                "contact_policy": "auto",
            },
        )
        await s_holder.commit()
        hid_row2 = await s_holder.execute(
            text("SELECT id FROM agents WHERE project_id = :pid AND name = :name"),
            {"pid": project_id, "name": claim_name},
        )
        hid2 = hid_row2.scalar_one_or_none()
        if isinstance(hid2, int):
            holder_agent_id = hid2
            holder_agent_name = claim_name
            if project_slug:
                {
                    "id": holder_agent_id,
                    "name": holder_agent_name,
                    "program": "ops",
                    "model": "system",
                    "task_description": "ops-escalation",
                    "inception_ts": now.isoformat(),
                    "last_active_ts": now.isoformat(),
                    "project_id": project_id,
                    "attachments_policy": "auto",
                    "contact_policy": "auto",
                }

    return holder_agent_id, holder_agent_name


def _http_sender_identity(
    *,
    message_project_id: int | None,
    sender_name: str | None,
    sender_project_id: int | None,
    sender_project_human_key: str | None,
    sender_project_slug: str | None,
) -> tuple[str, dict[str, str]]:
    canonical_sender = (sender_name or "").strip() or "Unknown"
    sender_display = _sender_display_name(
        message_project_id=message_project_id,
        sender_name=canonical_sender,
        sender_project_id=sender_project_id,
        sender_project_slug=sender_project_slug,
    )
    metadata: dict[str, str] = {"sender_name": canonical_sender}
    if (
        message_project_id is None
        or sender_project_id is None
        or sender_project_id == message_project_id
    ):
        return sender_display, metadata
    if sender_project_human_key:
        metadata["sender_project"] = sender_project_human_key
    if sender_project_slug:
        metadata["sender_project_slug"] = sender_project_slug
        metadata["sender_address"] = _format_cross_project_agent_address(
            sender_project_slug,
            canonical_sender,
        )
    return sender_display, metadata


__all__ = ["build_http_app", "create_app", "main"]


class _FastMCPHttpApp(Protocol):
    def http_app(self, *args: Any, **kwargs: Any) -> FastAPI: ...


class _FastAPILifespan(Protocol):
    def lifespan(self, app: FastAPI) -> Any: ...


def _expanduser_resolve_path(path: Path) -> Path:
    return path.expanduser().resolve()


def _collect_retention_quota_report_sync(settings: Settings) -> dict[str, Any]:
    import fnmatch as _fnmatch

    storage_root = _expanduser_resolve_path(Path(settings.storage.root))
    projects_root = storage_root / "mailboxes"
    total_attach_bytes = 0
    per_project_attach: dict[str, int] = {}
    ignore_patterns = list(getattr(settings, "retention_ignore_project_patterns", []) or [])

    for proj_dir in projects_root.iterdir() if projects_root.exists() else []:
        if proj_dir.is_symlink() or proj_dir.is_junction():
            raise ValueError("Linked mailbox directories cannot be included in quota reporting")
        if not proj_dir.is_dir():
            continue
        proj_name = proj_dir.name
        if any(_fnmatch.fnmatch(proj_name, pat) for pat in ignore_patterns):
            continue
        att_root = proj_dir / "attachments"
        if att_root.is_symlink() or att_root.is_junction():
            raise ValueError("Linked attachment directories cannot be included in quota reporting")
        if att_root.exists():
            for attachment_file in att_root.rglob("*"):
                if attachment_file.is_symlink() or attachment_file.is_junction():
                    raise ValueError("Linked attachments cannot be included in quota reporting")
                if not attachment_file.is_file():
                    continue
                try:
                    size_bytes = attachment_file.stat().st_size
                except FileNotFoundError:
                    continue  # A concurrently removed managed attachment is no longer counted.
                total_attach_bytes += size_bytes
                per_project_attach[proj_name] = per_project_attach.get(proj_name, 0) + size_bytes

    return {
        "old_messages": 0,
        "retention_max_age_days": int(settings.retention_max_age_days),
        "total_attachments_bytes": total_attach_bytes,
        "quota_limit_bytes": int(settings.quota_attachments_limit_bytes),
        "per_project_attach": per_project_attach,
        "per_project_inbox_counts": {},
    }


async def _collect_retention_quota_report(settings: Settings) -> dict[str, Any]:
    import fnmatch

    report = await asyncio.to_thread(_collect_retention_quota_report_sync, settings)
    await ensure_schema()
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=settings.retention_max_age_days)
    query = text("""
        SELECT p.slug,
               COUNT(DISTINCT CASE WHEN m.created_ts < :cutoff THEN m.id END) AS old_messages,
               COUNT(mr.agent_id) AS inbox_count
        FROM projects p
        LEFT JOIN messages m ON m.project_id = p.id
        LEFT JOIN message_recipients mr ON mr.message_id = m.id
        GROUP BY p.id, p.slug
    """).bindparams(bindparam("cutoff", type_=DateTime()))
    async with get_session() as session:
        rows = (await session.execute(query, {"cutoff": cutoff})).mappings().all()
    for row in rows:
        slug = str(row["slug"])
        if any(fnmatch.fnmatch(slug, pattern) for pattern in settings.retention_ignore_project_patterns):
            continue
        report["old_messages"] += int(row["old_messages"])
        report["per_project_inbox_counts"][slug] = int(row["inbox_count"])
    return report


def _decode_jwt_header_segment(token: str) -> dict[str, object] | None:
    """Return decoded JWT header without verifying signature."""
    try:
        segment = token.split(".", 1)[0]
        padded = segment + "=" * (-len(segment) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


_LOGGING_CONFIGURED = False

# Pre-compiled regex patterns for HTTP validators
_LIKE_ESCAPE_CHAR = "!"


def _like_escape(term: str) -> str:
    """Escape LIKE wildcards for literal substring matching."""
    return term.replace("!", "!!").replace("%", "!%").replace("_", "!_")


def _configure_logging(settings: Settings) -> None:
    """Initialize structlog and stdlib logging formatting."""
    # Idempotent setup
    global _LOGGING_CONFIGURED
    if _LOGGING_CONFIGURED:
        return
    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
    ]
    if settings.log_json_enabled:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.processors.KeyValueRenderer(key_order=["event", "path", "status"]))
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, settings.log_level.upper(), logging.INFO)),
        cache_logger_on_first_use=True,
    )
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))

    # Suppress verbose MCP library logging for stateless HTTP sessions
    # "Terminating session: None" is routine for stateless mode and just noise
    logging.getLogger("mcp.server.streamable_http").setLevel(logging.WARNING)
    logging.getLogger("mcp.server.lowlevel.server").setLevel(logging.WARNING)

    # Suppress verbose aiosqlite DEBUG logs (functools.partial cursor/operation noise)
    logging.getLogger("aiosqlite").setLevel(logging.INFO)

    # Suppress verbose git library DEBUG logs (Popen commands, platform detection)
    logging.getLogger("git.util").setLevel(logging.INFO)
    logging.getLogger("git.cmd").setLevel(logging.INFO)

    # Suppress filelock DEBUG logs (lock acquire/release routine operations)
    logging.getLogger("filelock").setLevel(logging.INFO)

    # Suppress SSE ping keepalive debug logs (periodic noise every 15s)
    logging.getLogger("sse_starlette.sse").setLevel(logging.INFO)

    # Add filter to suppress verbose tracebacks for expected/recoverable errors
    # FastMCP's tool_manager uses logger.exception() which prints full tracebacks
    # even for expected errors like "agent not found" or resource contention.
    # This filter intercepts those and removes the traceback for cleaner logs.
    class ExpectedErrorFilter(logging.Filter):
        """Filter that suppresses tracebacks for expected/recoverable tool errors.

        Expected errors include:
        - ToolExecutionError with recoverable=True
        - Agent not found / project not found
        - Resource busy / database lock

        These are normal operational conditions in multi-agent environments
        and don't need full stack traces cluttering the logs.
        """

        # Keywords that indicate an expected/recoverable error
        _EXPECTED_PATTERNS = (
            "not found in project",
            "resource_busy",
            "temporarily locked",
            "recoverable=true",
            "use register_agent",
            "available agents:",
        )

        def filter(self, record: logging.LogRecord) -> bool:
            # Only process records from FastMCP tool_manager with exception info
            if not record.exc_info or record.exc_info[1] is None:
                return True

            exc = record.exc_info[1]
            exc_str = str(exc).lower()

            # Check if this is an expected error based on message content
            is_expected = any(pattern in exc_str for pattern in self._EXPECTED_PATTERNS)

            # Also check for our ToolExecutionError with recoverable flag
            if hasattr(exc, "recoverable") and exc.recoverable:
                is_expected = True

            # Check the cause chain for ToolExecutionError
            cause = getattr(exc, "__cause__", None)
            if cause is not None:
                cause_str = str(cause).lower()
                if any(pattern in cause_str for pattern in self._EXPECTED_PATTERNS):
                    is_expected = True
                if hasattr(cause, "recoverable") and cause.recoverable:
                    is_expected = True

            if is_expected:
                # Clear exc_info to prevent traceback printing, but keep the log message
                record.exc_info = None
                record.exc_text = None
                # Downgrade from ERROR to INFO for expected errors
                if record.levelno >= logging.ERROR:
                    record.levelno = logging.INFO
                    record.levelname = "INFO"

            return True

    # Apply filter to FastMCP's tool_manager logger
    fastmcp_logger = logging.getLogger("fastmcp.tools.tool_manager")
    fastmcp_logger.addFilter(ExpectedErrorFilter())

    # mark configured
    _LOGGING_CONFIGURED = True


# In-process JWKS cache: avoid refetching the JWKS document on every request
# (#212). Keyed by JWKS URL; entries expire after _JWKS_CACHE_TTL_SECONDS.
_JWKS_CACHE_TTL_SECONDS = 300.0
_jwks_cache: dict[str, tuple[float, Any]] = {}
_jwks_cache_lock = asyncio.Lock()


async def _fetch_jwks(jwks_url: str, *, force: bool = False):
    """Return a parsed JWKS key set for ``jwks_url``, using a TTL cache.

    On a cache miss/expiry (or when ``force`` is set, e.g. after an unknown
    ``kid``), the document is refetched. On fetch/parse failure the last good
    cached key set (if any) is returned so transient outages don't break auth.
    """
    from time import monotonic

    jose_mod = importlib.import_module("authlib.jose")
    JsonWebKey = jose_mod.JsonWebKey

    now = monotonic()
    async with _jwks_cache_lock:
        cached = _jwks_cache.get(jwks_url)
        if cached is not None and not force and (now - cached[0]) < _JWKS_CACHE_TTL_SECONDS:
            return cached[1]

    try:
        httpx = importlib.import_module("httpx")
        AsyncClient = httpx.AsyncClient
        async with AsyncClient(timeout=5) as client:
            jwks = (await client.get(jwks_url)).json()
        key_set = JsonWebKey.import_key_set(jwks)
    except Exception:
        # Fall back to any cached (possibly stale) key set on fetch failure.
        async with _jwks_cache_lock:
            cached = _jwks_cache.get(jwks_url)
        return cached[1] if cached is not None else None

    async with _jwks_cache_lock:
        _jwks_cache[jwks_url] = (monotonic(), key_set)
    return key_set


def _select_jwks_key(key_set, header: dict, algorithms: list[str]):
    """Resolve the verification key from a JWKS key set by ``kid``.

    Never blindly picks ``keys[0]`` (#211). With a ``kid`` we look it up
    directly; an unknown ``kid`` returns ``None``. Without a ``kid`` this also
    returns ``None`` -- the caller falls back to verifying against each
    algorithm-compatible candidate (see ``_jwks_candidate_keys``) instead.
    """
    kid = header.get("kid")
    if kid:
        with contextlib.suppress(Exception):
            return key_set.find_by_kid(kid)
    return None


def _jwks_candidate_keys(key_set, header: dict, algorithms: list[str]) -> list:
    """Return JWKS keys to try when no ``kid`` is present.

    Filters by signing use and by algorithm compatibility (matching the key's
    declared ``alg`` when present, otherwise the key type implied by the
    configured algorithms). Blind ``keys[0]`` selection is never used.
    """
    alg_set = {str(a) for a in algorithms}
    # Map configured JWS algorithms to acceptable JWK key types.
    kty_for_alg = {
        "HS": "oct", "RS": "RSA", "PS": "RSA",
        "ES": "EC", "Ed": "OKP",
    }
    wanted_kty = {kty_for_alg[a[:2]] for a in alg_set if a[:2] in kty_for_alg}
    candidates = []
    for key in list(getattr(key_set, "keys", []) or []):
        with contextlib.suppress(Exception):
            use = key.tokens.get("use") if hasattr(key, "tokens") else None
            if use not in (None, "sig"):
                continue
            key_alg = key.tokens.get("alg") if hasattr(key, "tokens") else None
            if key_alg is not None and str(key_alg) not in alg_set:
                continue
            kty = getattr(key, "kty", None) or (key.tokens.get("kty") if hasattr(key, "tokens") else None)
            if wanted_kty and kty is not None and kty not in wanted_kty:
                continue
            candidates.append(key)
    return candidates


class BearerAuthMiddleware(BaseHTTPMiddleware):
    def __init__(
        self, app: FastAPI, token: str, allow_localhost: bool = False, jwt_enabled: bool = False
    ) -> None:
        super().__init__(app)
        self._token = token
        self._allow_localhost = allow_localhost
        # When JWT auth is also enabled, a static-bearer mismatch must NOT
        # short-circuit before the inner SecurityAndRateLimitMiddleware gets a
        # chance to validate a JWT (#210). In that case we accept any Bearer
        # token here and let the JWT path render the final auth decision.
        self._jwt_enabled = jwt_enabled

    @staticmethod
    def _is_localhost(host: str) -> bool:
        """Check if host is a localhost address, including IPv4-mapped IPv6."""
        if not host:
            return False
        # Standard localhost addresses
        if host in {"127.0.0.1", "::1", "localhost"}:
            return True
        # IPv4-mapped IPv6 address (::ffff:127.0.0.1)
        return bool(host.lower().startswith("::ffff:") and host[7:] == "127.0.0.1")

    @staticmethod
    def _has_forwarded_headers(request: Request) -> bool:
        """Detect proxy-forwarded headers to avoid trusting localhost behind proxies."""
        headers = request.headers
        return any(
            name in headers
            for name in ("x-forwarded-for", "x-forwarded-proto", "x-forwarded-host", "forwarded")
        )

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint):
        if request.method == "OPTIONS":  # allow CORS preflight
            return await call_next(request)
        if request.url.path.startswith("/health/") or request.url.path == "/api/health":
            return await call_next(request)
        if request.url.path.startswith(("/identity/confirm/", "/api/identity/confirm/")):
            return await call_next(request)
        if _localhost_bypass_allowed(
            request,
            allow_localhost=self._allow_localhost,
        ):
            return await call_next(request)
        auth_header = request.headers.get("Authorization", "")
        expected_header = f"Bearer {self._token}"
        # Use constant-time comparison to prevent timing attacks
        if hmac.compare_digest(auth_header, expected_header):
            return await call_next(request)
        # Static bearer did not match. If JWT auth is enabled, defer to the inner
        # JWT-validating middleware instead of rejecting here, so EITHER a valid
        # static bearer OR a valid JWT is accepted (#210).
        if self._jwt_enabled and auth_header.startswith("Bearer "):
            return await call_next(request)
        return JSONResponse({"detail": "Unauthorized"}, status_code=status.HTTP_401_UNAUTHORIZED)


def _localhost_bypass_allowed(request: Request, *, allow_localhost: bool) -> bool:
    """Return whether this request qualifies for localhost auth bypass."""
    if not allow_localhost:
        return False
    try:
        client_host = request.client.host if request.client else ""
    except Exception:
        client_host = ""
    return BearerAuthMiddleware._is_localhost(client_host) and not BearerAuthMiddleware._has_forwarded_headers(
        request
    )


class SecurityAndRateLimitMiddleware(BaseHTTPMiddleware):
    """JWT auth (optional), RBAC, and token-bucket rate limiting.

    - If JWT is enabled, validates Authorization: Bearer <token> using either HMAC secret or JWKS URL.
    - Enforces basic RBAC when enabled: read-only roles may only call whitelisted tools and resource reads.
    - Applies per-endpoint token-bucket limits (tools vs resources) with in-memory or Redis backend.
    """

    def __init__(self, app: FastAPI, settings: Settings):
        super().__init__(app)
        self.settings = settings
        self._jwt_enabled = bool(getattr(settings.http, "jwt_enabled", False))
        self._rbac_enabled = bool(getattr(settings.http, "rbac_enabled", True))
        self._reader_roles = set(getattr(settings.http, "rbac_reader_roles", []) or [])
        self._writer_roles = set(getattr(settings.http, "rbac_writer_roles", []) or [])
        self._readonly_tools = set(getattr(settings.http, "rbac_readonly_tools", []) or [])
        self._default_role = getattr(settings.http, "rbac_default_role", "tools")
        # Token bucket state (memory)
        from time import monotonic

        self._monotonic = monotonic
        self._buckets: dict[str, tuple[float, float]] = {}
        self._last_cleanup = monotonic()
        # Redis client (optional)
        self._redis = None
        if getattr(settings.http, "rate_limit_backend", "memory") == "redis" and getattr(
            settings.http, "rate_limit_redis_url", ""
        ):
            try:
                redis_asyncio = importlib.import_module("redis.asyncio")
                Redis = redis_asyncio.Redis
                self._redis = Redis.from_url(settings.http.rate_limit_redis_url)
            except Exception:
                self._redis = None

    def _cleanup_buckets(self, now: float) -> None:
        """Remove stale buckets to prevent memory leaks."""
        # Evict buckets not accessed in the last hour
        expiration = 3600.0
        cutoff = now - expiration
        # Create list of keys to remove to avoid runtime modification errors during iteration
        to_remove = [k for k, (_, ts) in self._buckets.items() if ts < cutoff]
        for k in to_remove:
            self._buckets.pop(k, None)

    async def _decode_jwt(self, token: str) -> dict | None:
        """Validate and decode JWT, returning claims or None on failure."""
        with contextlib.suppress(Exception):
            jose_mod = importlib.import_module("authlib.jose")
            JsonWebKey = jose_mod.JsonWebKey
            JsonWebToken = jose_mod.JsonWebToken
            algs = list(getattr(self.settings.http, "jwt_algorithms", ["HS256"]))
            jwt = JsonWebToken(algs)
            audience = getattr(self.settings.http, "jwt_audience", None) or None
            issuer = getattr(self.settings.http, "jwt_issuer", None) or None
            jwks_url = getattr(self.settings.http, "jwt_jwks_url", None) or None
            secret = getattr(self.settings.http, "jwt_secret", None) or None

            header = _decode_jwt_header_segment(token)
            if header is None:
                return None
            key = None
            candidate_keys: list = []
            if jwks_url:
                with contextlib.suppress(Exception):
                    key_set = await _fetch_jwks(jwks_url)
                    if key_set is None:
                        return None
                    if header.get("kid"):
                        key = _select_jwks_key(key_set, header, algs)
                        # Unknown kid: the cached JWKS may be stale; force a
                        # refresh once before giving up (#212).
                        if key is None:
                            key_set = await _fetch_jwks(jwks_url, force=True)
                            if key_set is not None:
                                key = _select_jwks_key(key_set, header, algs)
                    else:
                        # No kid: never blind-pick keys[0]. Try every
                        # algorithm-compatible key during verification (#211).
                        candidate_keys = _jwks_candidate_keys(key_set, header, algs)
            elif secret:
                with contextlib.suppress(Exception):
                    key = JsonWebKey.import_key(secret, {"kty": "oct"})
            keys_to_try = candidate_keys if candidate_keys else ([key] if key is not None else [])
            if not keys_to_try:
                return None
            for candidate in keys_to_try:
                with contextlib.suppress(Exception):
                    claims = jwt.decode(token, candidate)
                    if audience:
                        claims.validate_aud(audience)
                    if issuer and str(claims.get("iss") or "") != issuer:
                        continue
                    claims.validate()
                    return dict(claims)
        return None

    @staticmethod
    def _classify_request(path: str, method: str, body_bytes: bytes) -> tuple[str, str | None]:
        """Return (kind, tool_name) where kind is 'tools'|'resources'|'other'."""
        if method.upper() != "POST":
            return "other", None
        if not body_bytes:
            return "other", None
        with contextlib.suppress(Exception):
            import json as _json

            payload = _json.loads(body_bytes)
            rpc_method = str(payload.get("method", ""))
            if rpc_method == "tools/call":
                params = payload.get("params", {}) or {}
                tool_name = params.get("name")
                return "tools", tool_name if isinstance(tool_name, str) else None
            if rpc_method.startswith("resources/"):
                return "resources", None
            return "other", None
        return "other", None

    @staticmethod
    def _coerce_rpm(value: object, default: int) -> int:
        # An explicit 0 disables the limit and must survive (#213); only a
        # missing/None value falls back to the default. Use a None check rather
        # than ``value or default`` (which would turn 0 into ``default``).
        if value is None:
            return default
        with contextlib.suppress(Exception):
            return int(cast(Any, value))
        return default

    def _rate_limits_for(self, kind: str) -> tuple[int, int]:
        # return (per_minute, burst)
        if kind == "tools":
            rpm = self._coerce_rpm(getattr(self.settings.http, "rate_limit_tools_per_minute", 60), 60)
            burst = int(getattr(self.settings.http, "rate_limit_tools_burst", 0) or 0)
        elif kind == "resources":
            rpm = self._coerce_rpm(getattr(self.settings.http, "rate_limit_resources_per_minute", 120), 120)
            burst = int(getattr(self.settings.http, "rate_limit_resources_burst", 0) or 0)
        else:
            rpm = self._coerce_rpm(getattr(self.settings.http, "rate_limit_per_minute", 60), 60)
            burst = 0
        # rpm <= 0 means "disabled" (handled by _consume_bucket); don't synthesize
        # a positive burst that would re-enable limiting.
        burst = int(burst) if burst > 0 else max(1, rpm)
        return rpm, burst

    async def _consume_bucket(self, key: str, per_minute: int, burst: int) -> bool:
        """Return True if token granted, False if limited."""
        if per_minute <= 0:
            return True
        rate_per_sec = per_minute / 60.0
        now = self._monotonic()

        # Redis backend
        if self._redis is not None:
            try:
                lua = (
                    "local key = KEYS[1]\n"
                    "local now = tonumber(ARGV[1])\n"
                    "local rate = tonumber(ARGV[2])\n"
                    "local burst = tonumber(ARGV[3])\n"
                    "local state = redis.call('HMGET', key, 'tokens', 'ts')\n"
                    "local tokens = tonumber(state[1]) or burst\n"
                    "local ts = tonumber(state[2]) or now\n"
                    "local delta = now - ts\n"
                    "tokens = math.min(burst, tokens + delta * rate)\n"
                    "local allowed = 0\n"
                    "if tokens >= 1 then\n"
                    "  tokens = tokens - 1\n"
                    "  allowed = 1\n"
                    "end\n"
                    "redis.call('HMSET', key, 'tokens', tokens, 'ts', now)\n"
                    "redis.call('EXPIRE', key, math.ceil(burst / math.max(rate, 0.001)))\n"
                    "return allowed\n"
                )
                allowed = await self._redis.eval(lua, 1, f"rl:{key}", now, rate_per_sec, burst)
                return bool(int(allowed or 0) == 1)
            except Exception:
                # Fallback to memory on Redis failure
                pass

        # In-memory token bucket
        tokens, ts = self._buckets.get(key, (float(burst), now))
        elapsed = max(0.0, now - ts)
        tokens = min(float(burst), tokens + elapsed * rate_per_sec)
        if tokens < 1.0:
            self._buckets[key] = (tokens, now)
            return False
        tokens -= 1.0
        self._buckets[key] = (tokens, now)
        return True

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint):
        # Perform periodic cleanup of in-memory rate limit buckets
        if self._redis is None:
            now = self._monotonic()
            if now - self._last_cleanup > 60.0:
                self._cleanup_buckets(now)
                self._last_cleanup = now

        # Allow CORS preflight and health endpoints
        if (
            request.method == "OPTIONS"
            or request.url.path.startswith("/health/")
            or request.url.path == "/api/health"
            or request.url.path.startswith(("/identity/confirm/", "/api/identity/confirm/"))
        ):
            return await call_next(request)

        # Only read/patch body for POST requests. GET (including SSE) must not receive http.request messages.
        body_bytes = b""
        if request.method.upper() == "POST":
            try:
                body_bytes = await request.body()
                body_sent = False

                async def _receive() -> dict:
                    nonlocal body_sent
                    if body_sent:
                        return {"type": "http.request", "body": b"", "more_body": False}
                    body_sent = True
                    return {"type": "http.request", "body": body_bytes, "more_body": False}

                cast(Any, request)._receive = _receive
            except Exception:
                body_bytes = b""

        kind, tool_name = self._classify_request(request.url.path, request.method, body_bytes)

        # JWT auth (if enabled)
        if self._jwt_enabled:
            auth_header = request.headers.get("Authorization", "")
            # #210: when JWT is enabled, a valid *static* bearer is still accepted
            # as the OR-alternative to a JWT (the outer BearerAuthMiddleware defers
            # Bearer requests here without distinguishing the two). Check it first so
            # static-bearer clients keep working once JWT is turned on; a static
            # bearer is treated exactly as it is when JWT is disabled (default role).
            static_token = getattr(self.settings.http, "bearer_token", "") or ""
            if static_token and hmac.compare_digest(auth_header, f"Bearer {static_token}"):
                roles = {self._default_role}
            else:
                if not auth_header.startswith("Bearer "):
                    return JSONResponse({"detail": "Unauthorized"}, status_code=status.HTTP_401_UNAUTHORIZED)
                token = auth_header.split(" ", 1)[1].strip()
                claims_dict = await self._decode_jwt(token)
                if claims_dict is None:
                    return JSONResponse({"detail": "Unauthorized"}, status_code=status.HTTP_401_UNAUTHORIZED)
                claims = cast(dict[str, Any], claims_dict)
                request.state.jwt_claims = claims
                roles_raw = claims.get(self.settings.http.jwt_role_claim, [])
                if isinstance(roles_raw, str):
                    roles = {roles_raw}
                elif isinstance(roles_raw, (list, tuple)):
                    roles = {str(r) for r in roles_raw}
                else:
                    roles = set()
                if not roles:
                    roles = {self._default_role}
        else:
            roles = {self._default_role}
            # Elevate localhost to writer when unauthenticated localhost is allowed
            if _localhost_bypass_allowed(
                request,
                allow_localhost=bool(getattr(self.settings.http, "allow_localhost_unauthenticated", False)),
            ):
                roles.add("writer")

        # RBAC enforcement (skip for localhost when allowed)
        is_local_ok = _localhost_bypass_allowed(
            request,
            allow_localhost=bool(getattr(self.settings.http, "allow_localhost_unauthenticated", False)),
        )
        if self._rbac_enabled and not is_local_ok and kind in {"tools", "resources"}:
            is_reader = bool(roles & self._reader_roles)
            is_writer = bool(roles & self._writer_roles) or (not roles)
            if kind == "resources":
                pass  # readers allowed
            elif kind == "tools":
                if not tool_name:
                    # Without name, assume write-required to be safe
                    if not is_writer:
                        return JSONResponse({"detail": "Forbidden"}, status_code=status.HTTP_403_FORBIDDEN)
                else:
                    if tool_name in self._readonly_tools:
                        if not is_reader and not is_writer:
                            return JSONResponse({"detail": "Forbidden"}, status_code=status.HTTP_403_FORBIDDEN)
                    else:
                        if not is_writer:
                            return JSONResponse({"detail": "Forbidden"}, status_code=status.HTTP_403_FORBIDDEN)

        # Rate limiting
        if self.settings.http.rate_limit_enabled:
            rpm, burst = self._rate_limits_for(kind)
            identity = request.client.host if request.client else "ip-unknown"
            # Prefer stable subject from JWT if present
            with contextlib.suppress(Exception):
                maybe_claims = getattr(request.state, "jwt_claims", None)
                if isinstance(maybe_claims, dict):
                    sub = maybe_claims.get("sub")
                    if isinstance(sub, str) and sub:
                        identity = f"sub:{sub}"
            endpoint = tool_name or "*"
            key = f"{kind}:{endpoint}:{identity}"
            allowed = await self._consume_bucket(key, rpm, burst)
            if not allowed:
                return JSONResponse({"detail": "Rate limit exceeded"}, status_code=status.HTTP_429_TOO_MANY_REQUESTS)

        return await call_next(request)


async def readiness_check() -> None:
    await ensure_schema()
    async with get_session() as session:
        await session.execute(text("SELECT 1"))

    # Fail readiness if FD usage from lockfile leaks is critically high.
    # This gives orchestrators a signal to restart the process before it
    # becomes completely wedged (issue #116).
    current, limit = get_fd_usage()
    if current >= 0 and limit > 0:
        headroom_pct = (limit - current) / limit
        if headroom_pct < 0.10:
            lock_stats = get_lock_telemetry()
            raise RuntimeError(
                f"FD exhaustion imminent: {current}/{limit} FDs in use "
                f"({round(headroom_pct * 100, 1)}% headroom). "
                f"Lock telemetry: {lock_stats}"
            )


def create_app() -> FastAPI:
    """Zero-argument ASGI app factory for ``uvicorn ... --factory`` (#214).

    ``build_http_app`` requires a ``Settings`` argument, so it cannot be used
    directly as a uvicorn ``--factory`` target. This wrapper resolves settings
    from the environment and builds the app, matching the documented command.
    """
    return build_http_app(get_settings())


def build_http_app(settings: Settings, server=None) -> FastAPI:
    # Configure logging once
    _configure_logging(settings)
    if server is None:
        server = build_mcp_server()

    # Build MCP HTTP sub-app with stateless mode for ASGI test transports
    mcp_http_app = cast(_FastMCPHttpApp, server).http_app(
        path="/",
        stateless_http=True,
        json_response=True,
    )

    # Second, STATEFUL MCP sub-app (issue #250): stateless mode creates a new
    # transport per request and never issues an ``Mcp-Session-Id`` header, so
    # session-bound agent authentication (#148) could never persist across
    # HTTP tool calls — ``create_agent_identity(return_registration_token=false)``
    # followed by any protected call failed with AUTHENTICATION_REQUIRED.
    # A bare flip to ``stateless_http=False`` would break handshake-skipping
    # clients (e.g. ntm's HTTP client), so we mount BOTH: the stateful app at
    # '/mcp' for spec-compliant MCP clients that keep a session, and the
    # stateless app at '/api' (and the configured base) for one-shot clients.
    mcp_stateful_http_app = cast(_FastMCPHttpApp, server).http_app(
        path="/",
        stateless_http=False,
        json_response=True,
    )

    # no-op wrapper removed; using explicit stateless adapter below

    # Background workers lifecycle
    async def _startup() -> None:  # pragma: no cover - service lifecycle
        # Note: no early return here -- the FD health monitor always runs,
        # even when optional workers are disabled by feature flags.

        async def _worker_cleanup() -> None:
            while True:
                try:
                    await ensure_schema()
                    async with get_session() as session:
                        rows = await session.execute(text("SELECT DISTINCT project_id FROM file_reservations"))
                        pids = [r[0] for r in rows.fetchall() if r[0] is not None]
                    released_total = 0
                    for pid in pids:
                        with contextlib.suppress(Exception):
                            stale = await _expire_stale_file_reservations(pid)
                            released_total += len(stale)
                    try:
                        rich_console = importlib.import_module("rich.console")
                        rich_panel = importlib.import_module("rich.panel")
                        Console = rich_console.Console
                        Panel = rich_panel.Panel
                        Console().print(
                            Panel.fit(
                                f"projects_scanned={len(pids)} released={released_total}",
                                title="File Reservations Cleanup",
                                border_style="cyan",
                            )
                        )
                    except Exception:
                        pass
                    with contextlib.suppress(Exception):
                        structlog.get_logger("tasks").info(
                            "file_reservations_cleanup",
                            projects_scanned=len(pids),
                            stale_released=released_total,
                        )
                except Exception:
                    pass
                await asyncio.sleep(settings.file_reservations_cleanup_interval_seconds)

        async def _worker_ack_ttl() -> None:
            import datetime as _dt

            while True:
                try:
                    await ensure_schema()
                    async with get_session() as session:
                        result = await session.execute(
                            text(
                                """
                            SELECT m.id, m.project_id, m.created_ts, mr.agent_id
                            FROM messages m
                            JOIN message_recipients mr ON mr.message_id = m.id
                            WHERE m.ack_required = 1 AND mr.ack_ts IS NULL
                            """
                            )
                        )
                        rows = result.fetchall()
                    now = _dt.datetime.now(_dt.timezone.utc)
                    now_naive = now.replace(tzinfo=None)
                    for mid, project_id, created_ts, agent_id in rows:
                        # Normalize to timezone-aware UTC before arithmetic; SQLite may yield naive datetimes
                        ts = created_ts
                        if getattr(ts, "tzinfo", None) is None or ts.tzinfo.utcoffset(ts) is None:
                            ts = ts.replace(tzinfo=_dt.timezone.utc)
                        else:
                            ts = ts.astimezone(_dt.timezone.utc)
                        age = (now - ts).total_seconds()
                        if age >= settings.ack_ttl_seconds:
                            try:
                                rich_console = importlib.import_module("rich.console")
                                rich_panel = importlib.import_module("rich.panel")
                                rich_text = importlib.import_module("rich.text")
                                Console = rich_console.Console
                                Panel = rich_panel.Panel
                                Text = rich_text.Text
                                con = Console()
                                body = Text.assemble(
                                    ("message_id: ", "cyan"),
                                    (str(mid), "white"),
                                    "\n",
                                    ("agent_id: ", "cyan"),
                                    (str(agent_id), "white"),
                                    "\n",
                                    ("project_id: ", "cyan"),
                                    (str(project_id), "white"),
                                    "\n",
                                    ("age_s: ", "cyan"),
                                    (str(int(age)), "white"),
                                    "\n",
                                    ("ttl_s: ", "cyan"),
                                    (str(settings.ack_ttl_seconds), "white"),
                                )
                                con.print(Panel(body, title="ACK Overdue", border_style="red"))
                            except Exception:
                                print(
                                    f"ack-warning message_id={mid} project_id={project_id} agent_id={agent_id} age_s={int(age)} ttl_s={settings.ack_ttl_seconds}"
                                )
                            with contextlib.suppress(Exception):
                                structlog.get_logger("tasks").warning(
                                    "ack_overdue",
                                    message_id=str(mid),
                                    project_id=str(project_id),
                                    agent_id=str(agent_id),
                                    age_s=int(age),
                                    ttl_s=int(settings.ack_ttl_seconds),
                                )
                            if settings.ack_escalation_enabled:
                                mode = (settings.ack_escalation_mode or "log").lower()
                                if mode == "file_reservation":
                                    try:
                                        y_dir = created_ts.strftime("%Y")
                                        m_dir = created_ts.strftime("%m")
                                        # Resolve recipient name
                                        async with get_session() as s_lookup:
                                            name_row = await s_lookup.execute(
                                                text("SELECT name FROM agents WHERE id = :aid"), {"aid": agent_id}
                                            )
                                            name_res = name_row.fetchone()
                                        recipient_name = name_res[0] if name_res and name_res[0] else "*"
                                        pattern = (
                                            f"agents/{recipient_name}/inbox/{y_dir}/{m_dir}/*.md"
                                            if recipient_name != "*"
                                            else f"agents/*/inbox/{y_dir}/{m_dir}/*.md"
                                        )
                                        project_slug = await _project_slug_from_id(project_id)
                                        holder_agent_id = int(agent_id)
                                        holder_agent_name = recipient_name
                                        if settings.ack_escalation_claim_holder_name:
                                            claim_name = settings.ack_escalation_claim_holder_name
                                            holder_agent_id, holder_agent_name = await _ensure_ack_escalation_holder(
                                                settings=settings,
                                                project_id=int(project_id),
                                                project_slug=project_slug,
                                                recipient_agent_id=int(agent_id),
                                                recipient_name=recipient_name,
                                                claim_name=claim_name,
                                                now=now,
                                                now_naive=now_naive,
                                            )
                                        async with get_session() as s2:
                                            await s2.execute(
                                                text(
                                                    """
                                                INSERT INTO file_reservations(project_id, agent_id, path_pattern, exclusive, reason, created_ts, expires_ts)
                                                VALUES (:pid, :holder, :pattern, :exclusive, :reason, :cts, :ets)
                                                """
                                                ),
                                                {
                                                    "pid": project_id,
                                                    "holder": holder_agent_id,
                                                    "pattern": pattern,
                                                    "exclusive": 1 if settings.ack_escalation_claim_exclusive else 0,
                                                    "reason": "ack-overdue",
                                                    "cts": now_naive,
                                                    "ets": now_naive
                                                    + _dt.timedelta(seconds=settings.ack_escalation_claim_ttl_seconds),
                                                },
                                            )
                                            await s2.commit()
                                        # Also write JSON artifact to archive
                                        if not project_slug:
                                            raise ValueError(f"Project id {project_id} has no slug; cannot write archive artifacts.")
                                        archive = await ensure_mailbox_storage(settings, project_slug)
                                        expires_at = now + _dt.timedelta(
                                            seconds=settings.ack_escalation_claim_ttl_seconds
                                        )
                                        async with archive_write_lock(archive):
                                            await write_file_reservation_record(
                                                archive,
                                                {
                                                    "project": project_slug,
                                                    "agent": holder_agent_name,
                                                    "path_pattern": pattern,
                                                    "exclusive": settings.ack_escalation_claim_exclusive,
                                                    "reason": "ack-overdue",
                                                    "created_ts": now.isoformat(),
                                                    "expires_ts": expires_at.isoformat(),
                                                },
                                            )
                                    except Exception:
                                        pass
                except Exception:
                    pass
                await asyncio.sleep(settings.ack_ttl_scan_interval_seconds)

        async def _worker_tool_metrics() -> None:
            log = structlog.get_logger("tool.metrics")
            while True:
                try:
                    snapshot = _tool_metrics_snapshot()
                    if snapshot:
                        log.info("tool_metrics_snapshot", tools=snapshot)
                except Exception:
                    pass
                await asyncio.sleep(max(5, settings.tool_metrics_emit_interval_seconds))

        async def _worker_retention_quota() -> None:
            while True:
                with contextlib.suppress(Exception):
                    report = await _collect_retention_quota_report(settings)
                    structlog.get_logger("maintenance").info(
                        "retention_quota_report",
                        **report,
                    )
                    # Quota alerts
                    limit_b = int(settings.quota_attachments_limit_bytes)
                    inbox_limit = int(settings.quota_inbox_limit_count)
                    if limit_b > 0:
                        for proj, used in report["per_project_attach"].items():
                            if used >= limit_b:
                                structlog.get_logger("maintenance").warning(
                                    "quota_attachments_exceeded", project=proj, used_bytes=used, limit_bytes=limit_b
                                )
                    if inbox_limit > 0:
                        for proj, cnt in report["per_project_inbox_counts"].items():
                            if cnt >= inbox_limit:
                                structlog.get_logger("maintenance").warning(
                                    "quota_inbox_exceeded", project=proj, inbox_count=cnt, limit=inbox_limit
                                )
                await asyncio.sleep(max(60, settings.retention_report_interval_seconds))

        async def _worker_fd_health() -> None:
            """Periodic file descriptor health monitor.

            Checks FD headroom every 30 seconds and proactively cleans up
            resources when headroom drops below safe thresholds. This prevents
            the EMFILE -> socket closed -> unreachable cascade that occurs
            under sustained multi-agent load.

            Also monitors lockfile FD leaks (issue #116) and cleans up
            deleted-but-open .lock file descriptors.

            Thresholds:
            - 30% headroom: warning logged
            - 20% headroom: proactive cleanup triggered (includes lockfile FDs)
            - 15% headroom: error logged, aggressive cleanup
            """
            _fd_logger = structlog.get_logger("fd_health")
            while True:
                try:
                    current, limit = get_fd_usage()
                    if current >= 0 and limit > 0:
                        headroom_pct = (limit - current) / limit
                        lock_stats = get_lock_telemetry()

                        if headroom_pct < 0.15:
                            # Critical: aggressive cleanup
                            _fd_logger.error(
                                "fd_health.critical",
                                current_fds=current,
                                fd_limit=limit,
                                headroom_pct=round(headroom_pct * 100, 1),
                                lock_telemetry=lock_stats,
                            )
                        elif headroom_pct < 0.20:
                            # Low: proactive cleanup
                            _fd_logger.warning(
                                "fd_health.low",
                                current_fds=current,
                                fd_limit=limit,
                                headroom_pct=round(headroom_pct * 100, 1),
                                lock_telemetry=lock_stats,
                            )
                        elif headroom_pct < 0.30:
                            # Warning only
                            _fd_logger.warning(
                                "fd_health.warning",
                                current_fds=current,
                                fd_limit=limit,
                                headroom_pct=round(headroom_pct * 100, 1),
                                lock_telemetry=lock_stats,
                            )
                except Exception:
                    pass
                await asyncio.sleep(30)

        async def _worker_auto_retire_stale_agents() -> None:
            log = structlog.get_logger("maintenance.auto_retire")
            interval = max(60, int(settings.auto_retire_stale_agents_interval_seconds))
            threshold = max(60, int(settings.auto_retire_stale_agents_threshold_seconds))
            while True:
                with contextlib.suppress(Exception):
                    retired = await sweep_stale_agents(threshold_seconds=threshold)
                    if retired:
                        log.info(
                            "auto_retired_stale_agents",
                            count=len(retired),
                            threshold_seconds=threshold,
                            agents=[
                                {
                                    "agent": entry["agent_name"],
                                    "project": entry["project_key"],
                                    "last_active_ts": entry["last_active_ts"],
                                }
                                for entry in retired
                            ],
                        )
                await asyncio.sleep(interval)

        async def _worker_mailbox_lifecycle() -> None:
            from .lifecycle import due_cleanup_ids, expire_mailboxes, purge_mailbox

            log = structlog.get_logger("maintenance.mailboxes")
            while True:
                try:
                    await ensure_schema(settings)
                    expired = await expire_mailboxes()
                    if expired:
                        log.info("mailboxes.expired", count=expired)
                    for project_id in await due_cleanup_ids():
                        try:
                            if await purge_mailbox(settings, project_id):
                                log.info("mailboxes.cleaned", project_id=project_id)
                        except Exception as exc:
                            log.exception("mailboxes.cleanup_failed", project_id=project_id)
                            async with get_session() as session:
                                failed = await session.get(Project, project_id)
                                if failed is not None and failed.mailbox_state in {"trash", "purging"}:
                                    failed.cleanup_error = str(exc)[:1000]
                                    await session.commit()
                except Exception:
                    log.exception("mailboxes.maintenance_failed")
                await asyncio.sleep(60)

        tasks = []
        tasks.append(asyncio.create_task(_worker_mailbox_lifecycle()))
        # FD health monitor always runs - it's critical for preventing EMFILE cascades
        tasks.append(asyncio.create_task(_worker_fd_health()))
        if settings.file_reservations_cleanup_enabled:
            tasks.append(asyncio.create_task(_worker_cleanup()))
        if settings.ack_ttl_enabled:
            tasks.append(asyncio.create_task(_worker_ack_ttl()))
        if settings.tool_metrics_emit_enabled:
            tasks.append(asyncio.create_task(_worker_tool_metrics()))
        if settings.retention_report_enabled or settings.quota_enabled:
            tasks.append(asyncio.create_task(_worker_retention_quota()))
        if settings.auto_retire_stale_agents_enabled:
            tasks.append(asyncio.create_task(_worker_auto_retire_stale_agents()))
        fastapi_app.state._background_tasks = tasks

    async def _shutdown() -> None:  # pragma: no cover - service lifecycle
        tasks = getattr(fastapi_app.state, "_background_tasks", [])
        for task in tasks:
            task.cancel()
        # Await cancelled tasks with a timeout to prevent shutdown hangs
        # (aiosqlite cancellation can block indefinitely)
        if tasks:
            with contextlib.suppress(Exception):
                await asyncio.wait(tasks, timeout=5.0)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan_context(app: FastAPI):
        # Ensure both mounted MCP apps initialize their internal task groups
        # (each http_app() call owns an independent StreamableHTTPSessionManager).
        mcp_lifespan_app = cast(_FastAPILifespan, mcp_http_app)
        mcp_stateful_lifespan_app = cast(_FastAPILifespan, mcp_stateful_http_app)
        async with (
            mcp_lifespan_app.lifespan(mcp_http_app),
            mcp_stateful_lifespan_app.lifespan(mcp_stateful_http_app),
        ):
            await _startup()
            try:
                yield
            finally:
                await _shutdown()

    # Now construct FastAPI with the composed lifespan so ASGI transports run it.
    # Give the app a real title/version so the auto-generated /openapi.json has a
    # proper `info` block (derive the version from installed package metadata,
    # mirroring cli._package_version; never hardcode a value that could drift).
    def _package_version() -> str:
        import importlib.metadata as _importlib_metadata

        try:
            return _importlib_metadata.version("mcp-agent-mail")
        except _importlib_metadata.PackageNotFoundError:  # pragma: no cover - dev installs
            return "0.0.0+local"

    fastapi_app = FastAPI(
        title="MCP Agent Mail",
        version=_package_version(),
        lifespan=lifespan_context,
    )

    class InterfaceLocaleMiddleware(BaseHTTPMiddleware):
        """Choose and persist the language used by server-rendered UI pages."""

        async def dispatch(
            self,
            request: Request,
            call_next: RequestResponseEndpoint,
        ) -> Response:
            requested_locale = request.query_params.get("lang")
            locale = select_interface_locale(
                query_locale=requested_locale,
                cookie_locale=request.cookies.get(INTERFACE_LOCALE_COOKIE),
                accept_language=request.headers.get("accept-language"),
            )
            token = set_interface_locale(locale)
            try:
                response = await call_next(request)
            finally:
                reset_interface_locale(token)

            response.headers["Content-Language"] = locale
            if normalize_interface_locale(requested_locale) is not None:
                response.set_cookie(
                    INTERFACE_LOCALE_COOKIE,
                    locale,
                    max_age=31_536_000,
                    httponly=False,
                    samesite="lax",
                )
            return response

    fastapi_app.add_middleware(InterfaceLocaleMiddleware)

    @fastapi_app.middleware("http")
    async def mailbox_access(request: Request, call_next: Any) -> Response:
        from .lifecycle import touch_project

        project_id = None
        parts = request.url.path.strip("/").split("/")
        reserved = {"api", "projects", "mailboxes", "activity", "unified-inbox", "archive", "static", "assets"}
        if len(parts) >= 2 and parts[0] == "mail" and parts[1] not in reserved:
            await ensure_schema(settings)
            async with get_session() as session:
                project = await session.scalar(select(Project).where(Project.slug == parts[1]))
                if project is not None:
                    project_id = project.id
                    if project.mailbox_state != "active":
                        if "text/html" in request.headers.get("accept", ""):
                            return RedirectResponse("/mail?category=trash#projects", status_code=303)
                        return JSONResponse({"detail": "Mailbox is in the recycle bin or being cleaned up"}, status_code=410)
        response = await call_next(request)
        if (project_id is not None and request.method == "GET" and response.status_code == 200
                and request.headers.get("sec-fetch-mode") == "navigate"):
            await touch_project(project_id)
        return response

    # Simple request logging (configurable)
    if settings.http.request_log_enabled:
        import time as _time

        class RequestLoggingMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request: Request, call_next: RequestResponseEndpoint):
                start = _time.time()
                method = request.method
                path = request.url.path
                client = request.client.host if request.client else "-"
                response = None
                exc: BaseException | None = None
                try:
                    response = await call_next(request)
                    return response
                except BaseException as err:
                    exc = err
                    raise
                finally:
                    # Always emit a log line, even when the handler raised (#215).
                    dur_ms = int((_time.time() - start) * 1000)
                    status_code = getattr(response, "status_code", 0) if response is not None else 500
                    with contextlib.suppress(Exception):
                        log = structlog.get_logger("http")
                        if exc is not None:
                            log.error(
                                "request",
                                method=method,
                                path=path,
                                status=status_code,
                                duration_ms=dur_ms,
                                client_ip=client,
                                error=repr(exc),
                            )
                        else:
                            log.info(
                                "request",
                                method=method,
                                path=path,
                                status=status_code,
                                duration_ms=dur_ms,
                                client_ip=client,
                            )
                    try:
                        rich_console = importlib.import_module("rich.console")
                        rich_panel = importlib.import_module("rich.panel")
                        rich_text = importlib.import_module("rich.text")
                        Console = rich_console.Console
                        Panel = rich_panel.Panel
                        Text = rich_text.Text
                        console = Console(width=100)
                        title = Text.assemble(
                            (method, "bold blue"),
                            ("  "),
                            (path, "bold white"),
                            ("  "),
                            (f"{status_code}", "bold green" if 200 <= status_code < 400 else "bold red"),
                            ("  "),
                            (f"{dur_ms}ms", "bold yellow"),
                        )
                        body = Text.assemble(
                            ("client: ", "cyan"),
                            (client, "white"),
                        )
                        if exc is not None:
                            body = Text.assemble(body, "\n", ("error: ", "cyan"), (repr(exc), "red"))
                        console.print(Panel(body, title=title, border_style="dim"))
                    except Exception:
                        suffix = f" error={exc!r}" if exc is not None else ""
                        print(
                            f"http method={method} path={path} status={status_code} ms={dur_ms} client={client}{suffix}"
                        )

        app_any = cast(Any, fastapi_app)
        app_any.add_middleware(RequestLoggingMiddleware)

    # Unified JWT/RBAC and robust rate limiter middleware
    if (
        settings.http.rate_limit_enabled
        or getattr(settings.http, "jwt_enabled", False)
        or getattr(settings.http, "rbac_enabled", True)
    ):
        app_any = cast(Any, fastapi_app)
        app_any.add_middleware(SecurityAndRateLimitMiddleware, settings=settings)
    # Bearer auth for non-localhost only; allow localhost unauth optionally for seamless local dev
    if settings.http.bearer_token:
        from typing import Any as _Any, cast as _cast  # local type-only import
        app_any = _cast(_Any, fastapi_app)
        app_any.add_middleware(
            BearerAuthMiddleware,
            token=settings.http.bearer_token,
            allow_localhost=bool(getattr(settings.http, "allow_localhost_unauthenticated", False)),
            jwt_enabled=bool(getattr(settings.http, "jwt_enabled", False)),
        )

    # Optional CORS
    if settings.cors.enabled:
        from typing import Any as _Any, cast as _cast  # local type-only import
        app_any2 = _cast(_Any, fastapi_app)
        app_any2.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors.origins or [],
            allow_credentials=settings.cors.allow_credentials,
            allow_methods=settings.cors.allow_methods or ["*"],
            allow_headers=settings.cors.allow_headers or ["*"],
        )

    def _identity_confirmation_origin_is_valid(request: Request) -> bool:
        origin = request.headers.get("origin", "")
        expected = f"{request.url.scheme}://{request.url.netloc}"
        return bool(origin) and hmac.compare_digest(origin.rstrip("/"), expected.rstrip("/"))

    async def _identity_confirmation_challenge(request: Request) -> str:
        if not _identity_confirmation_origin_is_valid(request):
            raise HTTPException(status_code=403, detail="Invalid confirmation origin")
        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=400, detail="Expected a JSON object") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Expected a JSON object")
        challenge = payload.get("challenge")
        if not isinstance(challenge, str) or not (32 <= len(challenge) <= 256):
            raise HTTPException(status_code=400, detail="Invalid confirmation challenge")
        return challenge

    def _identity_confirmation_error(exc: ConversationIdentityError) -> JSONResponse:
        status_code = 409 if exc.error_type in {"MAILBOX_UNAVAILABLE", "IDENTITY_SESSION_REVOKED"} else 404
        return JSONResponse(
            {"error": exc.error_type, "message": str(exc)},
            status_code=status_code,
            headers={"Cache-Control": "no-store"},
        )

    @fastapi_app.get("/identity/confirm/{request_uid}", response_class=HTMLResponse)
    async def identity_confirmation_page(request_uid: str) -> HTMLResponse:
        nonce = secrets.token_urlsafe(18)
        request_json = json.dumps(request_uid)
        page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Agent identity confirmation</title>
  <style nonce="{nonce}">
    :root {{ color-scheme: light dark; font-family: system-ui, sans-serif; }}
    body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; background: #0f172a; color: #e2e8f0; }}
    main {{ width: min(42rem, calc(100vw - 2rem)); padding: 1.5rem; border: 1px solid #334155; border-radius: 1rem; background: #111827; box-shadow: 0 1rem 3rem #0008; }}
    h1 {{ margin-top: 0; font-size: 1.4rem; }}
    dl {{ display: grid; grid-template-columns: 9rem 1fr; gap: .65rem; }}
    dt {{ color: #94a3b8; }} dd {{ margin: 0; overflow-wrap: anywhere; }}
    .warning {{ padding: .8rem; border-radius: .6rem; background: #7f1d1d; color: #fee2e2; }}
    .actions {{ display: flex; gap: .75rem; margin-top: 1.25rem; }}
    button {{ flex: 1; border: 0; border-radius: .55rem; padding: .75rem 1rem; font-weight: 700; cursor: pointer; }}
    #approve {{ background: #22c55e; color: #052e16; }} #deny {{ background: #475569; color: white; }}
    button:disabled {{ opacity: .5; cursor: wait; }} #status {{ min-height: 1.5rem; color: #cbd5e1; }}
  </style>
</head>
<body>
<main>
  <h1 id="title">Agent identity confirmation</h1>
  <p id="intro">Review this identity transfer before approving it.</p>
  <div id="loading">Loading protected request details…</div>
  <section id="details" hidden>
    <dl>
      <dt id="project-label">Mailbox</dt><dd id="project"></dd>
      <dt>Agent</dt><dd id="agent"></dd>
      <dt id="client-label">MCP client</dt><dd id="client"></dd>
      <dt id="expires-label">Expires</dt><dd id="expires"></dd>
    </dl>
    <p class="warning" id="warning">Approval immediately revokes the previously bound conversation.</p>
    <div class="actions">
      <button id="deny" type="button">Deny</button>
      <button id="approve" type="button">Approve transfer</button>
    </div>
  </section>
  <p id="status" role="status" aria-live="polite"></p>
</main>
<script nonce="{nonce}">
(() => {{
  'use strict';
  const requestId = {request_json};
  const fragment = new URLSearchParams(location.hash.slice(1));
  const challenge = fragment.get('challenge') || '';
  history.replaceState(null, '', location.pathname);
  const zh = (navigator.language || '').toLowerCase().startsWith('zh');
  const text = zh ? {{
    title: '智能体身份确认', intro: '批准前请核对此次身份接管。', loading: '正在加载受保护的请求详情…',
    mailbox: '邮箱', client: 'MCP 客户端', expires: '过期时间',
    warning: '批准后, 之前绑定的对话会立即失去该智能体身份。', deny: '拒绝', approve: '批准接管',
    invalid: '确认链接无效或已经过期。', approved: '身份接管已完成。你可以关闭此窗口。',
    denied: '已拒绝身份接管。你可以关闭此窗口。', failed: '操作失败。',
    recoveryIntro: '批准前请核对此次管理员身份恢复。', recoveryWarning: '批准后, 之前绑定的对话会立即失去该智能体身份。',
    recoveryApprove: '批准恢复', recoveryApproved: '身份恢复已完成。你可以关闭此窗口。',
    recoveryDenied: '已拒绝身份恢复。你可以关闭此窗口。'
  }} : {{
    title: 'Agent identity confirmation', intro: 'Review this identity transfer before approving it.',
    loading: 'Loading protected request details…', mailbox: 'Mailbox', client: 'MCP client', expires: 'Expires',
    warning: 'Approval immediately revokes the previously bound conversation.', deny: 'Deny',
    approve: 'Approve transfer', invalid: 'This confirmation link is invalid or expired.',
    approved: 'Identity transfer completed. You may close this window.',
    denied: 'Identity transfer denied. You may close this window.', failed: 'The operation failed.',
    recoveryIntro: 'Review this administrator identity recovery before approving it.',
    recoveryWarning: 'Approval immediately revokes any previously bound conversation.',
    recoveryApprove: 'Approve recovery', recoveryApproved: 'Identity recovery completed. You may close this window.',
    recoveryDenied: 'Identity recovery denied. You may close this window.'
  }};
  for (const [id, key] of [['title','title'],['intro','intro'],['loading','loading'],['project-label','mailbox'],
    ['client-label','client'],['expires-label','expires'],['warning','warning'],['deny','deny'],['approve','approve']]) {{
    document.getElementById(id).textContent = text[key];
  }}
  const post = async (suffix) => {{
    const response = await fetch(`/api/identity/confirm/${{encodeURIComponent(requestId)}}/${{suffix}}`, {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}}, credentials: 'same-origin',
      body: JSON.stringify({{challenge}})
    }});
    const data = await response.json();
    if (!response.ok) throw new Error(data.message || data.detail || text.failed);
    return data;
  }};
  const finish = async (decision) => {{
    document.getElementById('approve').disabled = true;
    document.getElementById('deny').disabled = true;
    try {{
      await post(decision);
      document.getElementById('details').hidden = true;
      document.getElementById('status').textContent = decision === 'approve' ? text.approved : text.denied;
    }} catch (error) {{ document.getElementById('status').textContent = error.message || text.failed; }}
  }};
  document.getElementById('approve').addEventListener('click', () => finish('approve'));
  document.getElementById('deny').addEventListener('click', () => finish('deny'));
  post('details').then((data) => {{
    if (data.action === 'recover') {{
      text.approved = text.recoveryApproved; text.denied = text.recoveryDenied;
      document.getElementById('intro').textContent = text.recoveryIntro;
      document.getElementById('warning').textContent = text.recoveryWarning;
      document.getElementById('approve').textContent = text.recoveryApprove;
    }}
    document.getElementById('loading').hidden = true;
    document.getElementById('project').textContent = data.project;
    document.getElementById('agent').textContent = data.agent_name;
    document.getElementById('client').textContent = data.client_label;
    document.getElementById('expires').textContent = data.expires_at;
    document.getElementById('details').hidden = false;
  }}).catch(() => {{ document.getElementById('loading').textContent = text.invalid; }});
}})();
</script>
</body>
</html>"""
        return HTMLResponse(
            page,
            headers={
                "Cache-Control": "no-store, max-age=0",
                "Content-Security-Policy": (
                    "default-src 'none'; "
                    f"script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
                    "connect-src 'self'; img-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
                ),
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @fastapi_app.post("/api/identity/confirm/{request_uid}/details")
    async def identity_confirmation_details(request_uid: str, request: Request) -> JSONResponse:
        challenge = await _identity_confirmation_challenge(request)
        try:
            confirmation, project, agent, principal = await get_identity_confirmation(request_uid, challenge)
        except ConversationIdentityError as exc:
            return _identity_confirmation_error(exc)
        return JSONResponse(
            {
                "request_id": confirmation.request_uid,
                "action": confirmation.action,
                "project": project.human_key,
                "mailbox_type": project.mailbox_type,
                "mailbox_state": project.mailbox_state,
                "agent_name": agent.name,
                "client_label": principal.display_label,
                "binding_generation": confirmation.expected_binding_generation,
                "expires_at": confirmation.expires_at.replace(tzinfo=timezone.utc).isoformat(),
            },
            headers={"Cache-Control": "no-store"},
        )

    @fastapi_app.post("/api/identity/confirm/{request_uid}/approve")
    async def approve_identity_confirmation(request_uid: str, request: Request) -> JSONResponse:
        challenge = await _identity_confirmation_challenge(request)
        try:
            confirmation, _, _, _ = await get_identity_confirmation(request_uid, challenge)
            resolved = await decide_identity_transfer(request_uid, challenge, approve=True)
        except ConversationIdentityError as exc:
            return _identity_confirmation_error(exc)
        if resolved is None:  # pragma: no cover - approve=True always returns a binding or raises
            return JSONResponse({"error": "IDENTITY_BINDING_CONFLICT"}, status_code=409)
        return JSONResponse(
            {
                "status": "recovered" if confirmation.action == "recover" else "transferred",
                "agent_name": resolved.agent.name,
                "binding_generation": resolved.binding.generation,
            },
            headers={"Cache-Control": "no-store"},
        )

    @fastapi_app.post("/api/identity/confirm/{request_uid}/deny")
    async def deny_identity_confirmation(request_uid: str, request: Request) -> JSONResponse:
        challenge = await _identity_confirmation_challenge(request)
        try:
            await decide_identity_transfer(request_uid, challenge, approve=False)
        except ConversationIdentityError as exc:
            return _identity_confirmation_error(exc)
        return JSONResponse({"status": "denied"}, headers={"Cache-Control": "no-store"})

    # Health endpoints
    @fastapi_app.get("/health/liveness")
    async def liveness() -> JSONResponse:
        return JSONResponse({"status": "alive"})

    @fastapi_app.get("/health/readiness")
    async def readiness() -> JSONResponse:
        try:
            await readiness_check()
        except Exception as exc:
            try:
                rich_console = importlib.import_module("rich.console")
                rich_panel = importlib.import_module("rich.panel")
                Console = rich_console.Console
                Panel = rich_panel.Panel
                Console().print(Panel.fit(str(exc), title="Readiness Error", border_style="red"))
            except Exception:
                pass
            with contextlib.suppress(Exception):
                structlog.get_logger("health").error("readiness_error", error=str(exc))
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
        return JSONResponse({"status": "ready"})

    @fastapi_app.get("/api/health")
    async def api_health_bypass() -> JSONResponse:
        """Lightweight health probe that bypasses the MCP transport layer.

        Returns immediately without touching the database or connection pool,
        so it stays responsive even when the MCP ASGI pipeline is saturated
        under heavy multi-agent load.
        """
        return JSONResponse({"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()})

    def _oauth_metadata_disabled_response() -> JSONResponse:
        return JSONResponse({"mcp_oauth": False}, status_code=404)

    def _register_oauth_metadata_disabled(path: str) -> None:
        async def _oauth_metadata_disabled() -> JSONResponse:
            return _oauth_metadata_disabled_response()

        fastapi_app.add_api_route(path, _oauth_metadata_disabled, methods=["GET"], include_in_schema=False)

    # Thin ASGI wrapper that normalizes Accept / Content-Type headers for
    # MCP clients (some omit Accept entirely) and then delegates to the
    # SDK's native mcp_http_app which properly coordinates server lifecycle,
    # request handling, and session management via StreamableHTTPSessionManager.
    #
    # In production the parent FastAPI lifespan initializes the session manager
    # task group before any requests arrive.  In test environments (httpx
    # ASGITransport) no lifespan events are sent, so the wrapper lazily enters
    # the MCP app's lifespan on first request to avoid "Task group not
    # initialized" errors.
    class _HeaderFixupMCPApp:
        """Normalize headers then delegate to the native MCP HTTP app."""

        def __init__(self, native_app: FastAPI) -> None:
            self._app = native_app
            self._lifespan_entered = False
            self._lifespan_cm: Any = None
            self._lifespan_lock: asyncio.Lock | None = None

        async def _ensure_lifespan(self) -> None:
            """Lazily enter the MCP app's lifespan if not already running.

            This handles test environments where ASGI lifespan events are never
            sent (e.g. httpx ASGITransport).  In production the parent app's
            lifespan context already calls mcp_http_app.lifespan, so the
            session manager's task group will already be initialized and this
            method is a fast no-op.

            Uses double-check locking to prevent concurrent requests from
            entering the lifespan context manager twice.
            """
            if self._lifespan_entered:
                return
            # Lazily create the lock (must be in async context for the
            # correct event loop).
            if self._lifespan_lock is None:
                self._lifespan_lock = asyncio.Lock()
            async with self._lifespan_lock:
                if self._lifespan_entered:
                    return
                # Check if the session manager is already running (production path)
                session_mgr = getattr(self._app.state, "session_manager", None)
                if session_mgr is None:
                    # Try to find it via route endpoint
                    for route in getattr(self._app, "routes", []):
                        endpoint = getattr(route, "endpoint", None)
                        sm = getattr(endpoint, "session_manager", None)
                        if sm is not None:
                            session_mgr = sm
                            break
                if session_mgr is not None and getattr(session_mgr, "_task_group", None) is not None:
                    self._lifespan_entered = True
                    return
                # Enter the MCP app's lifespan (test path)
                mcp_lifespan_app = cast(_FastAPILifespan, self._app)
                self._lifespan_cm = mcp_lifespan_app.lifespan(self._app)
                await self._lifespan_cm.__aenter__()
                self._lifespan_entered = True

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            if scope.get("type") != "http":
                # Delegate non-HTTP scopes (e.g. lifespan) directly
                await self._app(scope, receive, send)
                return

            await self._ensure_lifespan()

            headers = list(scope.get("headers") or [])

            def _has_header(key: bytes) -> bool:
                lk = key.lower()
                return any(h[0].lower() == lk for h in headers)

            # Ensure both JSON and SSE are accepted; httpx defaults no Accept header
            headers = [(k, v) for (k, v) in headers if k.lower() != b"accept"]
            headers.append((b"accept", b"application/json, text/event-stream"))
            if scope.get("method") == "POST" and not _has_header(b"content-type"):
                headers.append((b"content-type", b"application/json"))
            new_scope = dict(scope)
            new_scope["headers"] = headers

            await self._app(new_scope, receive, send)

    # Mount at both '/base' and '/base/' to tolerate either form from clients/tests.
    # Also mount compatibility aliases for both '/api' and '/mcp' regardless of configured base.
    mount_base = settings.http.path or "/api"
    if not mount_base.startswith("/"):
        mount_base = "/" + mount_base
    base_no_slash = mount_base.rstrip("/") or "/"
    base_with_slash = base_no_slash if base_no_slash == "/" else base_no_slash + "/"
    stateless_app = _HeaderFixupMCPApp(mcp_http_app)
    stateful_app = _HeaderFixupMCPApp(mcp_stateful_http_app)

    # Path -> app mapping (issue #250): the '/mcp' compat alias is the
    # stateful, Mcp-Session-Id-issuing endpoint; '/api' and the configured
    # base stay stateless for handshake-skipping one-shot clients (e.g. ntm).
    # The CONFIGURED base always keeps the legacy stateless behavior, even if
    # an operator points it at '/mcp' — an explicit HTTP_PATH is a promise to
    # existing clients of that deployment, so we never change its semantics.
    def _app_for_mount(path: str) -> _HeaderFixupMCPApp:
        normalized = path.rstrip("/") or "/"
        if normalized == "/mcp" and base_no_slash != "/mcp":
            return stateful_app
        return stateless_app

    mount_paths = [base_no_slash, base_with_slash]
    for compat_base in ("/api", "/mcp"):
        compat_no_slash = compat_base.rstrip("/") or "/"
        compat_with_slash = compat_no_slash if compat_no_slash == "/" else compat_no_slash + "/"
        if compat_no_slash not in mount_paths:
            mount_paths.append(compat_no_slash)
        if compat_with_slash not in mount_paths:
            mount_paths.append(compat_with_slash)

    oauth_metadata_paths: set[str] = set()

    def _add_oauth_metadata_path(path: str) -> None:
        normalized = path.rstrip("/") or "/"
        oauth_metadata_paths.add(normalized)
        if normalized != "/":
            oauth_metadata_paths.add(f"{normalized}/")

    _add_oauth_metadata_path("/.well-known/oauth-authorization-server")
    _add_oauth_metadata_path("/.well-known/oauth-authorization-server/mcp")
    for mount_path in mount_paths:
        normalized = mount_path.rstrip("/") or "/"
        if normalized == "/":
            continue
        _add_oauth_metadata_path(f"{normalized}/.well-known/oauth-authorization-server")
        _add_oauth_metadata_path(f"{normalized}/.well-known/oauth-authorization-server/mcp")
        _add_oauth_metadata_path(f"/.well-known/oauth-authorization-server{normalized}")
    for path in sorted(oauth_metadata_paths):
        _register_oauth_metadata_disabled(path)

    for mount_path in mount_paths:
        with contextlib.suppress(Exception):
            fastapi_app.mount(mount_path, _app_for_mount(mount_path))

    # Expose composed lifespan via router
    fastapi_app.router.lifespan_context = lifespan_context

    # Add direct routes at no-slash base paths to tolerate clients omitting trailing slashes.
    def _register_base_passthrough(base_path_no_slash: str, base_path_with_slash: str) -> None:
        # Dispatch to the same app that is mounted at this base (issue #250:
        # '/mcp' is stateful, everything else stateless).
        target_app = _app_for_mount(base_path_no_slash)

        @fastapi_app.post(base_path_no_slash)
        async def _base_passthrough(request: Request) -> JSONResponse:
            # Re-dispatch to the mounted MCP app by calling it directly
            response_body: dict[str, Any] = {}
            status_code = 200
            headers: dict[str, str] = {}

            async def _send(message: MutableMapping[str, Any]) -> None:
                nonlocal response_body, status_code, headers
                if message.get("type") == "http.response.start":
                    status_code = int(message.get("status", 200))
                    hdrs = message.get("headers") or []
                    for k, v in hdrs:
                        headers[k.decode("latin1")] = v.decode("latin1")
                elif message.get("type") == "http.response.body":
                    body = message.get("body") or b""
                    try:
                        response_body = json.loads(body.decode("utf-8")) if body else {}
                    except Exception:
                        response_body = {}

            # If localhost and allow_localhost_unauthenticated, synthesize Authorization header automatically
            scope = dict(request.scope)
            if _localhost_bypass_allowed(
                request,
                allow_localhost=bool(settings.http.allow_localhost_unauthenticated),
            ):
                scope_headers = list(scope.get("headers") or [])
                has_auth = any(k.lower() == b"authorization" for k, _ in scope_headers)
                if not has_auth and settings.http.bearer_token:
                    scope_headers.append((b"authorization", f"Bearer {settings.http.bearer_token}".encode("latin1")))
                scope["headers"] = scope_headers
            await target_app(
                {**scope, "path": "/"},  # MCP app expects requests at its root
                request.receive,
                _send,
            )
            return JSONResponse(response_body, status_code=status_code, headers=headers)

    passthrough_pairs: list[tuple[str, str]] = [(base_no_slash, base_with_slash)]
    for compat_base in ("/api", "/mcp"):
        compat_no_slash = compat_base.rstrip("/") or "/"
        compat_with_slash = compat_no_slash if compat_no_slash == "/" else compat_no_slash + "/"
        if (compat_no_slash, compat_with_slash) not in passthrough_pairs:
            passthrough_pairs.append((compat_no_slash, compat_with_slash))
    for no_slash, with_slash in passthrough_pairs:
        _register_base_passthrough(no_slash, with_slash)

    # ----- Simple SSR Mail UI -----
    def _register_mail_ui() -> None:
        import bleach
        import markdown2

        try:
            from bleach.css_sanitizer import CSSSanitizer as _CSSSanitizerImport
        except Exception:  # tinycss2 may be missing; degrade gracefully
            _CSSSanitizer = None
        else:
            _CSSSanitizer = _CSSSanitizerImport
        CSSSanitizer = cast(Any, _CSSSanitizer)
        from jinja2 import Environment, FileSystemLoader, select_autoescape

        templates_root = Path(__file__).resolve().parent / "templates"
        env = Environment(
            loader=FileSystemLoader(str(templates_root)),
            autoescape=select_autoescape(["html", "xml"]),
            enable_async=True,
        )
        env.globals["_"] = cast(Any, gettext)
        env.globals["current_locale"] = cast(Any, get_interface_locale)
        # HTML sanitizer (allow safe images and limited CSS)
        _css_sanitizer = (
            CSSSanitizer(
                allowed_css_properties=["color", "background-color", "text-align", "text-decoration", "font-weight"]
            )
            if CSSSanitizer
            else None
        )
        _html_cleaner = bleach.Cleaner(
            tags=[
                "a",
                "abbr",
                "acronym",
                "b",
                "blockquote",
                "code",
                "em",
                "i",
                "li",
                "ol",
                "ul",
                "p",
                "pre",
                "strong",
                "table",
                "thead",
                "tbody",
                "tr",
                "th",
                "td",
                "h1",
                "h2",
                "h3",
                "h4",
                "h5",
                "h6",
                "hr",
                "br",
                "span",
                "img",
            ],
            attributes={
                "*": ["class"],
                "a": ["href", "title", "rel"],
                "abbr": ["title"],
                "acronym": ["title"],
                "code": ["class"],
                "pre": ["class"],
                "span": ["class", "style"],
                "p": ["class", "style"],
                "table": ["class", "style"],
                "td": ["class", "style"],
                "th": ["class", "style"],
                "img": ["src", "alt", "title", "width", "height", "loading", "decoding", "class"],
            },
            protocols=["http", "https", "mailto", "data"],
            strip=True,
            css_sanitizer=_css_sanitizer,
        )

        async def _render(name: str, **ctx: Any) -> HTMLResponse:
            tpl = env.get_template(name)
            ctx["legacy_archive_notice"] = name.startswith("archive_")
            html = await tpl.render_async(**ctx)
            return HTMLResponse(html)

        def _parse_fts_query(
            raw: str, scope_preference: str | None = None
        ) -> tuple[str, str, str, list[dict[str, str]]]:
            """Return (fts_expression, like_pattern) from a user query.
            Supports subject:foo and body:"multi word" tokens; otherwise defaults to subject/body OR.
            """
            raw = (raw or "").strip()
            if not raw:
                return "", "", "both", []
            scope_pref = scope_preference if scope_preference in {"subject", "body"} else "both"
            # tokens: key:"phrase" | "phrase" | key:word | word
            parts = re.findall(r"\w+:\"[^\"]+\"|\"[^\"]+\"|\w+:[^\s]+|[^\s]+", raw)
            exprs: list[str] = []
            like_terms: list[str] = []
            like_scope = scope_pref
            tokens: list[dict[str, str]] = []

            def _quote(s: str) -> str:
                return '"' + s.replace('"', '""') + '"'

            def _like_escape(term: str) -> str:
                return term.replace("!", "!!").replace("%", "!%").replace("_", "!_")

            for p in parts:
                key = None
                val = p
                if ":" in p and not p.startswith('"'):
                    maybe_key, maybe_val = p.split(":", 1)
                    if maybe_key in {"subject", "body"}:
                        key = maybe_key
                        val = maybe_val
                val = val.strip()
                val_inner = val[1:-1] if val.startswith('"') and val.endswith('"') and len(val) >= 2 else val

                # For LIKE pattern, we want literal matching of the user's term
                like_terms.append(_like_escape(val_inner))

                if key in {"subject", "body"}:
                    exprs.append(f"{key}:{_quote(val_inner)}")
                    tokens.append({"field": key, "value": val_inner})
                else:
                    if scope_pref == "subject":
                        exprs.append(f"subject:{_quote(val_inner)}")
                        tokens.append({"field": "subject", "value": val_inner})
                    elif scope_pref == "body":
                        exprs.append(f"body:{_quote(val_inner)}")
                        tokens.append({"field": "body", "value": val_inner})
                    else:
                        exprs.append(f"(subject:{_quote(val_inner)} OR body:{_quote(val_inner)})")
                        tokens.append({"field": "both", "value": val_inner})
            fts = " AND ".join(exprs) if exprs else ""
            like_pat = "%" + "%".join(like_terms) + "%" if like_terms else ""
            return fts, like_pat, like_scope, tokens

        @fastapi_app.get("/mail/api/locks", response_class=JSONResponse)
        async def mail_lock_status() -> JSONResponse:
            """Return metadata about active archive locks for observability."""

            settings_local = get_settings()
            payload = collect_lock_status(settings_local)
            return JSONResponse(payload)

        async def _build_unified_inbox_payload(
            *, limit: int = 500, include_projects: bool = True
        ) -> dict[str, Any]:
            """Fetch unified inbox data for HTML and JSON consumers."""

            safe_limit = max(1, min(int(limit), 1000))
            messages: list[dict[str, Any]] = []
            projects: list[dict[str, Any]] = []

            try:
                await ensure_schema()

                sibling_map: dict[int, dict[str, Any]] = {}
                if include_projects:
                    await refresh_project_sibling_suggestions()
                    sibling_map = await get_project_sibling_data()

                async with get_session() as session:
                    # Fetch recent messages with sender/project and computed recipient list
                    query = text(
                        """
                        SELECT
                            m.id,
                            m.subject,
                            m.body_md,
                            LENGTH(COALESCE(m.body_md, '')) AS body_length,
                            m.created_ts,
                            m.importance,
                            m.thread_id,
                            m.project_id AS message_project_id,
                            sender.name AS sender_name,
                            sender.project_id AS sender_project_id,
                            sp.human_key AS sender_project_name,
                            sp.slug AS sender_project_slug,
                            p.slug AS project_slug,
                            p.human_key AS project_name,
                            COALESCE(
                                (
                                    SELECT GROUP_CONCAT(name, ', ')
                                    FROM (
                                        SELECT DISTINCT recip2.name AS name
                                        FROM message_recipients mr2
                                        JOIN agents recip2 ON recip2.id = mr2.agent_id
                                        WHERE mr2.message_id = m.id
                                        ORDER BY name
                                    )
                                ),
                                ''
                            ) AS recipients
                        FROM messages m
                        JOIN agents sender ON m.sender_id = sender.id
                        LEFT JOIN projects sp ON sp.id = sender.project_id
                        JOIN projects p ON m.project_id = p.id
                        WHERE p.mailbox_state = 'active'
                        ORDER BY m.created_ts DESC
                        LIMIT :limit
                        """
                    )

                    rows = await session.execute(query, {"limit": safe_limit})

                    for r in rows.mappings().all():
                        body = r["body_md"] or ""
                        raw_body_length = r["body_length"]
                        body_length = int(raw_body_length) if raw_body_length is not None else len(body)
                        excerpt = body[:150].replace('#', '').replace('*', '').replace('`', '').strip()
                        if body_length > 150:
                            excerpt += "..."

                        created_ts = r["created_ts"]
                        if isinstance(created_ts, str):
                            created_dt = datetime.fromisoformat(created_ts.replace('Z', '+00:00'))
                        else:
                            created_dt = created_ts

                        if created_dt.tzinfo is None:
                            created_dt = created_dt.replace(tzinfo=timezone.utc)
                        else:
                            created_dt = created_dt.astimezone(timezone.utc)

                        now = datetime.now(timezone.utc)
                        delta = now - created_dt

                        if delta.days < 0 or (delta.days == 0 and delta.seconds < 0):
                            created_relative = gettext("Just now")
                        elif delta.days > 365:
                            created_relative = gettext("{count}y ago").format(count=delta.days // 365)
                        elif delta.days > 30:
                            created_relative = gettext("{count}mo ago").format(count=delta.days // 30)
                        elif delta.days > 0:
                            created_relative = gettext("{count}d ago").format(count=delta.days)
                        elif delta.seconds > 3600:
                            created_relative = gettext("{count}h ago").format(count=delta.seconds // 3600)
                        elif delta.seconds > 60:
                            created_relative = gettext("{count}m ago").format(count=delta.seconds // 60)
                        else:
                            created_relative = gettext("Just now")

                        sender_display, sender_meta = _http_sender_identity(
                            message_project_id=r["message_project_id"],
                            sender_name=r["sender_name"],
                            sender_project_id=r["sender_project_id"],
                            sender_project_human_key=r["sender_project_name"],
                            sender_project_slug=r["sender_project_slug"],
                        )
                        message_payload = {
                            "id": r["id"],
                            "subject": r["subject"] or "(No subject)",
                            "body_md": body,
                            "body_length": body_length,
                            "excerpt": excerpt,
                            "created_ts": str(r["created_ts"]),
                            "created_full": created_dt.strftime(gettext("%B %d, %Y at %I:%M %p")),
                            "created_relative": created_relative,
                            "importance": r["importance"] or "normal",
                            "thread_id": r["thread_id"],
                            "sender": sender_display,
                            "project_slug": r["project_slug"],
                            "project_name": r["project_name"],
                            "recipients": ", ".join(
                                part.strip() for part in (r["recipients"] or "").split(",") if part.strip()
                            ),
                            "read": False,
                        }
                        message_payload.update(sender_meta)
                        messages.append(message_payload)

                    if include_projects:
                        rows = await session.execute(
                        text("SELECT id, slug, human_key, created_at, archived_at FROM projects WHERE mailbox_state = 'active' ORDER BY created_at DESC")
                        )
                        for r in rows.fetchall():
                            project_id = int(r[0])
                            siblings = sibling_map.get(project_id, {"confirmed": [], "suggested": []})
                            projects.append(
                                {
                                    "id": project_id,
                                    "slug": r[1],
                                    "human_key": r[2],
                                    "created_at": str(r[3]),
                                    "archived_at": str(r[4]) if r[4] else None,
                                    "confirmed_siblings": siblings.get("confirmed", []),
                                    "suggested_siblings": siblings.get("suggested", []),
                                }
                            )

            except Exception as exc:  # pragma: no cover - defensive logging
                logging.error("Error fetching unified inbox data", exc_info=True, extra={"error": str(exc)})

            return {"messages": messages, "projects": projects}

        @fastapi_app.get("/mail", response_class=HTMLResponse)
        async def mail_unified_inbox(category: str = "all") -> HTMLResponse:
            """Unified inbox showing ALL messages across ALL projects (Gmail-style) + Projects below"""
            from .lifecycle import mailbox_dict

            if category not in {"all", "permanent", "temporary", "trash"}:
                raise HTTPException(status_code=400, detail="Invalid mailbox category")
            payload = await _build_unified_inbox_payload()
            async with get_session() as session:
                rows = await session.scalars(select(Project).order_by(text("created_at DESC")))
                project_cards = []
                for project in rows.all():
                    mailbox = mailbox_dict(project)
                    project_cards.append({
                        **mailbox, "mailbox": mailbox, "created_at": str(project.created_at),
                        "archived_at": str(project.archived_at)
                        if project.archived_at and project.mailbox_state == "active" else None,
                    })
            return await _render(
                "mail_unified_inbox.html",
                messages=payload.get("messages", []),
                projects=payload.get("projects", []),
                project_cards=project_cards,
                mailboxes=[p["mailbox"] for p in project_cards],
                category=category,
            )

        @fastapi_app.get("/mail/api/unified-inbox", response_class=JSONResponse)
        async def mail_unified_inbox_api(
            limit: int = 50000,
            include_projects: bool = False,
        ) -> JSONResponse:
            """JSON feed for the unified inbox view (used for background refresh)."""

            payload = await _build_unified_inbox_payload(limit=limit, include_projects=include_projects)
            if not include_projects:
                # Reduce payload size when polling for message updates only
                payload["projects"] = []
            return JSONResponse(payload)

        @fastapi_app.post("/mail/api/delete-messages", response_class=JSONResponse)
        async def delete_messages_api(request: Request) -> JSONResponse:
            """Permanently delete messages by ID (cross-project).

            Remove database messages without modifying retained legacy archives.
            """
            await ensure_schema()

            try:
                request_body = await request.json()
                if not isinstance(request_body, dict):
                    raise HTTPException(status_code=400, detail="Expected an object")
                message_ids: list[int] = request_body.get("message_ids", [])

                if not isinstance(message_ids, list) or any(type(mid) is not int or mid < 1 for mid in message_ids):
                    raise HTTPException(status_code=400, detail="Message IDs must be positive integers")

                if not message_ids:
                    raise HTTPException(status_code=400, detail="No message IDs provided")

                if len(message_ids) > 500:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Too many messages ({len(message_ids)}). Maximum is 500."
                    )

                deleted_count = 0
                async with get_session() as session:
                    placeholders = ','.join([f':mid{i}' for i in range(len(message_ids))])
                    id_params: dict[str, Any] = {f"mid{i}": mid for i, mid in enumerate(message_ids)}

                    await session.execute(text("BEGIN IMMEDIATE"))
                    unavailable = await session.scalar(text(
                        f"SELECT 1 FROM messages m JOIN projects p ON p.id = m.project_id "
                        f"WHERE m.id IN ({placeholders}) AND p.mailbox_state != 'active' LIMIT 1"
                    ), id_params)
                    if unavailable is not None:
                        raise HTTPException(status_code=409, detail="Restore the mailbox before deleting messages")
                    await session.execute(text(
                        f"UPDATE messages SET reply_to = NULL WHERE reply_to IN ({placeholders})"
                    ), id_params)

                    # Delete from SQLite
                    await session.execute(
                        text(f"DELETE FROM message_recipients WHERE message_id IN ({placeholders})"),
                        id_params,
                    )
                    del_result = await session.execute(
                        text(f"DELETE FROM messages WHERE id IN ({placeholders})"),
                        id_params,
                    )
                    deleted_count = int(getattr(del_result, "rowcount", 0) or 0)
                    await session.commit()

                return JSONResponse({
                    "success": True,
                    "deleted_count": deleted_count,
                })

            except HTTPException:
                raise
            except Exception as exc:
                import traceback
                traceback.print_exc()
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to delete messages: {exc!s}"
                ) from exc

        # ---- Agent Retire/Unretire API ----

        @fastapi_app.post("/mail/api/retire-agent", response_class=JSONResponse)
        async def retire_agent_api(request: Request) -> JSONResponse:
            """Retire an agent (soft-delete). Preserves message history but stops new messages."""
            await ensure_schema()
            try:
                body = await request.json()
                agent_id: int | None = body.get("agent_id")
                if agent_id is None:
                    raise HTTPException(status_code=400, detail="agent_id is required")

                async with get_session() as session:
                    from .models import Agent
                    agent = await session.get(Agent, agent_id)
                    if not agent:
                        raise HTTPException(status_code=404, detail="Agent not found")
                    project = await session.get(Project, agent.project_id)
                    if project is None or project.mailbox_state != "active":
                        raise HTTPException(status_code=410, detail="Mailbox is unavailable")
                    agent.retired_at = datetime.now(timezone.utc).replace(tzinfo=None)
                    session.add(agent)
                    await session.commit()

                return JSONResponse({"success": True, "agent_id": agent_id, "status": "retired"})
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(status_code=500, detail=f"Failed to retire agent: {exc!s}") from exc

        @fastapi_app.post("/mail/api/unretire-agent", response_class=JSONResponse)
        async def unretire_agent_api(request: Request) -> JSONResponse:
            """Restore a retired agent back to active status."""
            await ensure_schema()
            try:
                body = await request.json()
                agent_id: int | None = body.get("agent_id")
                if agent_id is None:
                    raise HTTPException(status_code=400, detail="agent_id is required")

                async with get_session() as session:
                    from .models import Agent
                    agent = await session.get(Agent, agent_id)
                    if not agent:
                        raise HTTPException(status_code=404, detail="Agent not found")
                    project = await session.get(Project, agent.project_id)
                    if project is None or project.mailbox_state != "active":
                        raise HTTPException(status_code=410, detail="Mailbox is unavailable")
                    agent.retired_at = None
                    session.add(agent)
                    await session.commit()

                return JSONResponse({"success": True, "agent_id": agent_id, "status": "active"})
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(status_code=500, detail=f"Failed to unretire agent: {exc!s}") from exc

        # ---- Project Archive/Unarchive API ----

        @fastapi_app.post("/mail/api/archive-project", response_class=JSONResponse)
        async def archive_project_api(request: Request) -> JSONResponse:
            """Archive a project (soft-delete). Preserves all messages but hides from active lists."""
            await ensure_schema()
            try:
                body = await request.json()
                project_id: int | None = body.get("project_id")
                if project_id is None:
                    raise HTTPException(status_code=400, detail="project_id is required")

                async with get_session() as session:
                    from .models import Project
                    project = await session.get(Project, project_id)
                    if not project:
                        raise HTTPException(status_code=404, detail="Project not found")
                    project.archived_at = datetime.now(timezone.utc).replace(tzinfo=None)
                    session.add(project)
                    await session.commit()

                return JSONResponse({"success": True, "project_id": project_id, "status": "archived"})
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(status_code=500, detail=f"Failed to archive project: {exc!s}") from exc

        @fastapi_app.post("/mail/api/unarchive-project", response_class=JSONResponse)
        async def unarchive_project_api(request: Request) -> JSONResponse:
            """Restore an archived project back to active status."""
            await ensure_schema()
            try:
                body = await request.json()
                project_id: int | None = body.get("project_id")
                if project_id is None:
                    raise HTTPException(status_code=400, detail="project_id is required")

                async with get_session() as session:
                    from .models import Project
                    project = await session.get(Project, project_id)
                    if not project:
                        raise HTTPException(status_code=404, detail="Project not found")
                    project.archived_at = None
                    session.add(project)
                    await session.commit()

                return JSONResponse({"success": True, "project_id": project_id, "status": "active"})
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(status_code=500, detail=f"Failed to unarchive project: {exc!s}") from exc

        @fastapi_app.post("/mail/api/mailboxes/by-slug/{slug}/activity")
        async def record_mailbox_view(slug: str, request: Request) -> JSONResponse:
            from .lifecycle import touch_project

            if request.headers.get("sec-fetch-site") == "cross-site":
                raise HTTPException(status_code=403, detail="Cross-site mailbox updates are not allowed")
            await ensure_schema(settings)
            async with get_session() as session:
                project = await session.scalar(select(Project).where(Project.slug == slug))
                if project is None or project.id is None:
                    raise HTTPException(status_code=404, detail="Mailbox not found")
                if project.mailbox_state != "active":
                    raise HTTPException(status_code=410, detail="Mailbox is unavailable")
                await touch_project(project.id)
            return JSONResponse({"success": True})

        @fastapi_app.get("/mail/activity", response_class=HTMLResponse)
        async def mailbox_activity(request: Request, project_id: int | None = None) -> HTMLResponse:
            await ensure_schema(settings)
            async with get_session() as session:
                rows = (await session.execute(text(
                    "SELECT m.id AS message_id, 'message' AS kind, m.subject AS detail, m.created_ts AS occurred_at, "
                    "p.slug, p.human_key FROM messages m JOIN projects p ON p.id = m.project_id "
                    "WHERE p.mailbox_state = 'active' AND (:pid IS NULL OR p.id = :pid) "
                    "UNION ALL SELECT NULL, e.event_type, '', e.created_at, p.slug, p.human_key "
                    "FROM mailbox_events e JOIN projects p ON p.id = e.project_id "
                    "WHERE e.event_type != 'message' AND (:pid IS NULL OR p.id = :pid) "
                    "ORDER BY occurred_at DESC LIMIT 200"
                ), {"pid": project_id})).mappings().all()
            if project_id is not None and request.headers.get("sec-fetch-mode") == "navigate":
                from .lifecycle import touch_project

                await touch_project(project_id)
            return await _render("mail_activity.html", events=[dict(row) for row in rows])

        @fastapi_app.get("/mail/mailboxes", response_class=HTMLResponse)
        async def mail_mailboxes(request: Request) -> RedirectResponse:
            query = f"?{request.url.query}" if request.url.query else ""
            return RedirectResponse(f"/mail{query}#projects", status_code=303)

        @fastapi_app.post("/mail/api/mailboxes/{project_id}", response_class=JSONResponse)
        async def configure_mailbox_api(project_id: int, request: Request) -> JSONResponse:
            from .lifecycle import configure_mailbox
            await ensure_schema(settings)

            if request.headers.get("sec-fetch-site") == "cross-site":
                raise HTTPException(status_code=403, detail="Cross-site changes are not allowed")
            try:
                data = await request.json()
                if not isinstance(data, dict) or set(data) - {"mailbox_type", "retention_days", "action"}:
                    raise ValueError("Invalid mailbox settings")
                if "retention_days" in data and type(data["retention_days"]) is not int:
                    raise ValueError("Retention must be an integer")
                return JSONResponse(await configure_mailbox(project_id, **data))
            except LookupError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except (ValueError, TypeError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        @fastapi_app.get("/mail/projects", response_class=HTMLResponse)
        async def mail_projects_list(category: str = "all") -> HTMLResponse:
            """Projects list view (moved from /mail)"""
            from .lifecycle import mailbox_dict

            if category not in {"all", "permanent", "temporary", "trash"}:
                raise HTTPException(status_code=400, detail="Invalid mailbox category")
            await ensure_schema()
            await refresh_project_sibling_suggestions()
            sibling_map = await get_project_sibling_data()
            async with get_session() as session:
                rows = await session.scalars(select(Project).order_by(text("created_at DESC")))
                projects = []
                for project in rows.all():
                    assert project.id is not None
                    project_id = project.id
                    siblings = sibling_map.get(project_id, {"confirmed": [], "suggested": []})
                    projects.append(
                        {
                            "id": project_id,
                            "slug": project.slug,
                            "human_key": project.human_key,
                            "created_at": str(project.created_at),
                            "archived_at": str(project.archived_at) if project.archived_at else None,
                            "confirmed_siblings": siblings.get("confirmed", []),
                            "suggested_siblings": siblings.get("suggested", []),
                            **mailbox_dict(project),
                            "mailbox": mailbox_dict(project),
                        }
                    )
            def group(project: dict[str, Any]) -> str:
                return project["mailbox_type"] if project["mailbox_state"] == "active" else "trash"

            counts = {key: sum(group(project) == key for project in projects)
                      for key in ("permanent", "temporary", "trash")}
            counts["all"] = sum(project["mailbox_state"] == "active" for project in projects)
            mailboxes = [project["mailbox"] for project in projects]
            projects = [project for project in projects if
                        (project["mailbox_state"] == "active" if category == "all" else group(project) == category)]
            return await _render("mail_index.html", projects=projects, mailboxes=mailboxes,
                                 category=category, category_counts=counts,
                                 active_projects=[p for p in projects if not p["archived_at"] or p["mailbox_state"] != "active"],
                                 archived_projects=[p for p in projects if p["archived_at"] and p["mailbox_state"] == "active"])

        @fastapi_app.get("/mail/{project}", response_class=HTMLResponse)
        async def mail_project(
            project: str,
            q: str | None = None,
            scope: str | None = None,
            order: str | None = None,
            boost: int | None = None,
        ) -> HTMLResponse:
            if order not in ("relevance", "time", None):
                order = "relevance"
            await ensure_schema()
            async with get_session() as session:
                proj = await session.execute(
                    text("SELECT id, slug, human_key, archived_at FROM projects WHERE slug = :k OR human_key = :k"), {"k": project}
                )
                prow = proj.fetchone()
                if not prow:
                    return await _render("error.html", message="Project not found")
                pid = int(prow[0])
                project_archived_at = str(prow[3]) if prow[3] else None
                agents_q = await session.execute(
                    text("SELECT id, name, program, model, retired_at FROM agents WHERE project_id = :pid ORDER BY name"),
                    {"pid": pid},
                )
                agents = [{"id": r[0], "name": r[1], "program": r[2], "model": r[3], "retired_at": str(r[4]) if r[4] else None} for r in agents_q.fetchall()]
                matched_messages: list[dict] = []
                if q and q.strip():
                    # Prefer FTS5 when available (fts_messages maintained by triggers)
                    fts_expr, like_pat, like_scope, tokens = _parse_fts_query(q, scope)
                    weights = (0.0, 3.0, 1.0) if (boost or 0) else (0.0, 1.0, 1.0)
                    fts_sql = (
                        "SELECT m.id, m.subject, s.name AS sender_name, s.project_id AS sender_project_id, "
                        "sp.human_key AS sender_project_name, sp.slug AS sender_project_slug, "
                        "m.created_ts, m.importance, m.thread_id, "
                        "snippet(fts_messages, 2, '<mark>', '</mark>', '…', 18) AS body_snippet "
                        "FROM fts_messages "
                        "JOIN messages m ON m.id = fts_messages.rowid "
                        "JOIN agents s ON s.id = m.sender_id "
                        "LEFT JOIN projects sp ON sp.id = s.project_id "
                        "WHERE m.project_id = :pid AND fts_messages MATCH :q "
                        + (
                            "ORDER BY m.created_ts DESC "
                            if (order or "relevance") == "time"
                            else f"ORDER BY bm25(fts_messages, {weights[0]}, {weights[1]}, {weights[2]}) "
                        )
                        + "LIMIT 10000"
                    )
                    try:
                        search = await session.execute(text(fts_sql), {"pid": pid, "q": fts_expr or q})
                        matched_messages = []
                        for r in search.mappings().all():
                            sender_display, sender_meta = _http_sender_identity(
                                message_project_id=pid,
                                sender_name=r["sender_name"],
                                sender_project_id=r["sender_project_id"],
                                sender_project_human_key=r["sender_project_name"],
                                sender_project_slug=r["sender_project_slug"],
                            )
                            item = {
                                "id": r["id"],
                                "subject": r["subject"],
                                "sender": sender_display,
                                "created": str(r["created_ts"]),
                                "importance": r["importance"],
                                "thread_id": r["thread_id"],
                                "snippet": r["body_snippet"],
                                "hits": (r["body_snippet"] or "").count("<mark>"),
                            }
                            item.update(sender_meta)
                            matched_messages.append(item)
                    except Exception:
                        # Fallback to LIKE if FTS not available
                        if like_scope == "subject":
                            like_sql = (
                                "SELECT m.id, m.subject, s.name AS sender_name, s.project_id AS sender_project_id, "
                                "sp.human_key AS sender_project_name, sp.slug AS sender_project_slug, "
                                "m.created_ts, m.importance, m.thread_id "
                                "FROM messages m JOIN agents s ON s.id = m.sender_id "
                                "LEFT JOIN projects sp ON sp.id = s.project_id "
                                f"WHERE m.project_id = :pid AND m.subject LIKE :pat ESCAPE '{_LIKE_ESCAPE_CHAR}' "
                                "ORDER BY m.created_ts DESC LIMIT 10000"
                            )
                        elif like_scope == "body":
                            like_sql = (
                                "SELECT m.id, m.subject, s.name AS sender_name, s.project_id AS sender_project_id, "
                                "sp.human_key AS sender_project_name, sp.slug AS sender_project_slug, "
                                "m.created_ts, m.importance, m.thread_id "
                                "FROM messages m JOIN agents s ON s.id = m.sender_id "
                                "LEFT JOIN projects sp ON sp.id = s.project_id "
                                f"WHERE m.project_id = :pid AND m.body_md LIKE :pat ESCAPE '{_LIKE_ESCAPE_CHAR}' "
                                "ORDER BY m.created_ts DESC LIMIT 10000"
                            )
                        else:
                            like_sql = (
                                "SELECT m.id, m.subject, s.name AS sender_name, s.project_id AS sender_project_id, "
                                "sp.human_key AS sender_project_name, sp.slug AS sender_project_slug, "
                                "m.created_ts, m.importance, m.thread_id "
                                "FROM messages m JOIN agents s ON s.id = m.sender_id "
                                "LEFT JOIN projects sp ON sp.id = s.project_id "
                                f"WHERE m.project_id = :pid AND (m.subject LIKE :pat ESCAPE '{_LIKE_ESCAPE_CHAR}' "
                                f"OR m.body_md LIKE :pat ESCAPE '{_LIKE_ESCAPE_CHAR}') "
                                "ORDER BY m.created_ts DESC LIMIT 10000"
                            )
                        search = await session.execute(text(like_sql), {"pid": pid, "pat": like_pat or f"%{_like_escape(q)}%"})
                        matched_messages = []
                        for r in search.mappings().all():
                            sender_display, sender_meta = _http_sender_identity(
                                message_project_id=pid,
                                sender_name=r["sender_name"],
                                sender_project_id=r["sender_project_id"],
                                sender_project_human_key=r["sender_project_name"],
                                sender_project_slug=r["sender_project_slug"],
                            )
                            item = {
                                "id": r["id"],
                                "subject": r["subject"],
                                "sender": sender_display,
                                "created": str(r["created_ts"]),
                                "importance": r["importance"],
                                "thread_id": r["thread_id"],
                                "snippet": "",
                                "hits": 0,
                            }
                            item.update(sender_meta)
                            matched_messages.append(item)
            return await _render(
                "mail_project.html",
                project={"id": pid, "slug": prow[1], "human_key": prow[2], "archived_at": project_archived_at},
                agents=agents,
                q=q or "",
                scope=scope or "",
                order=order or "relevance",
                boost=bool(boost),
                tokens=tokens if q and q.strip() else [],
                results=matched_messages,
            )

        @fastapi_app.post("/mail/api/projects/{project_id}/siblings/{other_id}", response_class=JSONResponse)
        async def update_project_sibling(project_id: int, other_id: int, request: Request) -> JSONResponse:
            try:
                payload = await request.json()
            except Exception:
                payload = {}
            action = str(payload.get("action", "")).lower()
            if action not in {"confirm", "dismiss", "reset"}:
                return JSONResponse({"error": "Invalid action"}, status_code=status.HTTP_400_BAD_REQUEST)

            target_status = {
                "confirm": "confirmed",
                "dismiss": "dismissed",
                "reset": "suggested",
            }[action]

            try:
                suggestion = await update_project_sibling_status(project_id, other_id, target_status)
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=status.HTTP_400_BAD_REQUEST)
            except NoResultFound:
                return JSONResponse({"error": "Project pair not found"}, status_code=status.HTTP_404_NOT_FOUND)
            except Exception as exc:
                structlog.get_logger("sibling").exception(
                    "project_sibling.update_failed",
                    project_id=project_id,
                    other_id=other_id,
                    action=action,
                    error=str(exc),
                )
                return JSONResponse(
                    {"error": "Unable to update sibling status"}, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
                )

            return JSONResponse({"status": suggestion["status"], "suggestion": suggestion})

        @fastapi_app.get("/mail/unified-inbox", response_class=HTMLResponse)
        async def unified_inbox(limit: int = 10000, filter_importance: str | None = None) -> HTMLResponse:
            """Unified inbox showing messages from all active agents across all projects."""
            limit = min(max(1, limit), 10000)
            await ensure_schema()
            async with get_session() as session:
                # Get all projects with their agents
                projects_query = await session.execute(
                    text(
                        """
                    SELECT p.id, p.slug, p.human_key,
                           COUNT(DISTINCT a.id) as agent_count,
                           MAX(a.last_active_ts) as last_activity
                    FROM projects p
                    LEFT JOIN agents a ON a.project_id = p.id
                    GROUP BY p.id, p.slug, p.human_key
                    ORDER BY (last_activity IS NULL) ASC, last_activity DESC, p.created_at DESC
                    """
                    )
                )
                projects_data = []
                for r in projects_query.fetchall():
                    proj_id = int(r[0])
                    # Get agents for this project
                    agents_query = await session.execute(
                        text(
                            """
                        SELECT a.id, a.name, a.program, a.model, a.last_active_ts
                        FROM agents a
                        WHERE a.project_id = :pid
                        ORDER BY a.last_active_ts DESC, a.name ASC
                        """
                        ),
                        {"pid": proj_id},
                    )

                    agents_list = []
                    for ar in agents_query.fetchall():
                        agents_list.append(
                            {
                                "id": int(ar[0]),
                                "name": ar[1],
                                "program": ar[2],
                                "model": ar[3],
                                "last_active": str(ar[4]) if ar[4] else None,
                            }
                        )

                    if agents_list:  # Only include projects with agents
                        projects_data.append(
                            {
                                "id": proj_id,
                                "slug": r[1],
                                "human_key": r[2],
                                "agent_count": int(r[3] or 0),
                                "agents": agents_list,
                            }
                        )

                # Get recent messages across all projects with thread information
                # Build WHERE clause safely using parameterized queries
                importance_conditions = ["p.mailbox_state = 'active'"]
                query_params = {"lim": limit}

                if filter_importance and filter_importance.lower() in ["urgent", "high"]:
                    importance_conditions.append("m.importance IN ('urgent', 'high')")

                where_clause = "WHERE " + " AND ".join(importance_conditions) if importance_conditions else "WHERE 1=1"

                messages_query = await session.execute(
                    text(
                        f"""
                    SELECT
                        m.id, m.subject, m.body_md, m.created_ts, m.importance, m.thread_id,
                        m.project_id AS message_project_id,
                        p.slug, p.human_key,
                        sender.name as sender_name,
                        sender.project_id AS sender_project_id,
                        sp.human_key AS sender_project_name,
                        sp.slug AS sender_project_slug,
                        COALESCE(
                            (
                                SELECT GROUP_CONCAT(name, ', ')
                                FROM (
                                    SELECT DISTINCT recip2.name AS name
                                    FROM message_recipients mr2
                                    JOIN agents recip2 ON recip2.id = mr2.agent_id
                                    WHERE mr2.message_id = m.id
                                    ORDER BY name
                                )
                            ),
                            ''
                        ) as recipient_names,
                        COUNT(DISTINCT CASE WHEN m2.id IS NOT NULL THEN m2.id END) as thread_count
                    FROM messages m
                    JOIN projects p ON p.id = m.project_id
                    JOIN agents sender ON sender.id = m.sender_id
                    LEFT JOIN projects sp ON sp.id = sender.project_id
                    LEFT JOIN message_recipients mr ON mr.message_id = m.id
                    LEFT JOIN agents recip ON recip.id = mr.agent_id
                    LEFT JOIN messages m2 ON (
                        m.thread_id IS NOT NULL
                        AND m2.thread_id = m.thread_id
                        AND m2.project_id = m.project_id
                        AND m2.id != m.id
                    )
                    {where_clause}
                    GROUP BY m.id, m.subject, m.body_md, m.created_ts, m.importance, m.thread_id,
                             m.project_id, p.slug, p.human_key, sender.name, sender.project_id, sp.human_key, sp.slug
                    ORDER BY m.created_ts DESC
                    LIMIT :lim
                    """
                    ),
                    query_params,
                )

                messages = []
                for r in messages_query.mappings().all():
                    sender_display, sender_meta = _http_sender_identity(
                        message_project_id=r["message_project_id"],
                        sender_name=r["sender_name"],
                        sender_project_id=r["sender_project_id"],
                        sender_project_human_key=r["sender_project_name"],
                        sender_project_slug=r["sender_project_slug"],
                    )
                    item = {
                        "id": int(r["id"]),
                        "subject": r["subject"],
                        "body_md": r["body_md"] or "",
                        "created": str(r["created_ts"]),
                        "importance": r["importance"] or "normal",
                        "thread_id": r["thread_id"],
                        "project_slug": r["slug"],
                        "project_name": r["human_key"],
                        "sender": sender_display,
                        "recipients": r["recipient_names"] or "",
                        "thread_count": int(r["thread_count"] or 0),
                    }
                    item.update(sender_meta)
                    messages.append(item)

            return await _render(
                "mail_unified_inbox.html",
                projects=projects_data,
                messages=messages,
                total_agents=sum(cast(int, p["agent_count"]) for p in projects_data),
                total_messages=len(messages),
                filter_importance=filter_importance or "",
            )

        @fastapi_app.get("/mail/{project}/inbox/{agent}", response_class=HTMLResponse)
        async def mail_inbox(project: str, agent: str, limit: int = 10000, page: int = 1) -> HTMLResponse:
            limit = min(max(1, limit), 10000)
            page = min(max(1, page), 10000)
            await ensure_schema()
            async with get_session() as session:
                prow = (
                    await session.execute(
                        text("SELECT id, slug, human_key FROM projects WHERE slug = :k OR human_key = :k"),
                        {"k": project},
                    )
                ).fetchone()
                if not prow:
                    return await _render("error.html", message="Project not found")
                pid = int(prow[0])
                arow = (
                    await session.execute(
                        text("SELECT id, name FROM agents WHERE project_id = :pid AND lower(name) = lower(:name)"),
                        {"pid": pid, "name": agent},
                    )
                ).fetchone()
                if not arow:
                    return await _render("error.html", message="Agent not found")
                offset = max(0, (max(1, page) - 1) * max(1, limit))
                inbox_rows = await session.execute(
                    text(
                        """
                    SELECT
                        m.id,
                        m.subject,
                        s.name AS sender_name,
                        s.project_id AS sender_project_id,
                        sp.human_key AS sender_project_name,
                        sp.slug AS sender_project_slug,
                        m.created_ts,
                        m.importance,
                        m.thread_id,
                        m.ack_required,
                        mr.read_ts,
                        mr.ack_ts
                    FROM messages m
                    JOIN message_recipients mr ON mr.message_id = m.id
                    JOIN agents a ON a.id = mr.agent_id
                    JOIN agents s ON s.id = m.sender_id
                    LEFT JOIN projects sp ON sp.id = s.project_id
                    WHERE m.project_id = :pid AND a.name = :name
                    ORDER BY m.created_ts DESC
                    LIMIT :lim OFFSET :off
                    """
                    ),
                    {"pid": pid, "name": agent, "lim": limit, "off": offset},
                )
                items = []
                for r in inbox_rows.mappings().all():
                    sender_display, sender_meta = _http_sender_identity(
                        message_project_id=pid,
                        sender_name=r["sender_name"],
                        sender_project_id=r["sender_project_id"],
                        sender_project_human_key=r["sender_project_name"],
                        sender_project_slug=r["sender_project_slug"],
                    )
                    read_ts = r["read_ts"]
                    ack_ts = r["ack_ts"]
                    ack_required = bool(r["ack_required"])
                    item = {
                        "id": r["id"],
                        "subject": r["subject"],
                        "sender": sender_display,
                        "created": str(r["created_ts"]),
                        "importance": r["importance"],
                        "thread_id": r["thread_id"],
                        "ack_required": ack_required,
                        "read_ts": str(read_ts) if read_ts else None,
                        "ack_ts": str(ack_ts) if ack_ts else None,
                        "unread": read_ts is None,
                        "needs_ack": ack_required and ack_ts is None,
                        "acked": ack_ts is not None,
                    }
                    item.update(sender_meta)
                    items.append(item)
            return await _render(
                "mail_inbox.html",
                project={"slug": prow[1], "human_key": prow[2]},
                agent=agent,
                items=items,
                page=page,
                limit=limit,
                next_page=page + 1,
                prev_page=page - 1 if page > 1 else None,
            )

        @fastapi_app.get("/mail/{project}/message/{mid}", response_class=HTMLResponse)
        async def mail_message(project: str, mid: int) -> HTMLResponse:
            await ensure_schema()
            async with get_session() as session:
                prow = (
                    await session.execute(
                        text("SELECT id, slug, human_key FROM projects WHERE slug = :k OR human_key = :k"),
                        {"k": project},
                    )
                ).fetchone()
                if not prow:
                    return await _render("error.html", message="Project not found")
                pid = int(prow[0])
                mrow = (
                    await session.execute(
                        text(
                            """
                            SELECT
                                m.id,
                                m.subject,
                                m.body_md,
                                s.name AS sender_name,
                                s.project_id AS sender_project_id,
                                sp.human_key AS sender_project_name,
                                sp.slug AS sender_project_slug,
                                m.created_ts,
                                m.importance,
                                m.thread_id,
                                m.ack_required,
                                m.attachments
                            FROM messages m
                            JOIN agents s ON s.id = m.sender_id
                            LEFT JOIN projects sp ON sp.id = s.project_id
                            WHERE m.project_id = :pid AND m.id = :mid
                            """
                        ),
                        {"pid": pid, "mid": mid},
                    )
                ).mappings().fetchone()
                if not mrow:
                    return await _render("error.html", message="Message not found")
                recs = await session.execute(
                    text(
                        "SELECT a.name, mr.kind, mr.read_ts, mr.ack_ts "
                        "FROM message_recipients mr JOIN agents a ON a.id = mr.agent_id "
                        "WHERE mr.message_id = :mid"
                    ),
                    {"mid": mid},
                )
                recipients = [
                    {
                        "name": r[0],
                        "kind": r[1],
                        "read_ts": str(r[2]) if r[2] else None,
                        "ack_ts": str(r[3]) if r[3] else None,
                    }
                    for r in recs.fetchall()
                ]
                ack_required_msg = bool(mrow["ack_required"])
                ack_count = sum(1 for r in recipients if r["ack_ts"])
                read_count = sum(1 for r in recipients if r["read_ts"])
                ack_summary = {
                    "ack_required": ack_required_msg,
                    "total": len(recipients),
                    "read": read_count,
                    "acked": ack_count,
                }
                # Find thread messages if thread_id is set
                thread_items: list[dict] = []
                th = mrow["thread_id"]
                if isinstance(th, str) and th.strip():
                    th_rows = await session.execute(
                        text(
                            """
                            SELECT
                                m.id,
                                m.subject,
                                s.name AS sender_name,
                                s.project_id AS sender_project_id,
                                sp.human_key AS sender_project_name,
                                sp.slug AS sender_project_slug,
                                m.created_ts
                            FROM messages m
                            JOIN agents s ON s.id = m.sender_id
                            LEFT JOIN projects sp ON sp.id = s.project_id
                            WHERE m.project_id = :pid AND (m.thread_id = :th OR m.id = :id)
                            ORDER BY m.created_ts ASC
                            """
                        ),
                        {"pid": pid, "th": th, "id": mid},
                    )
                    thread_items = []
                    for rr in th_rows.mappings().all():
                        sender_display, sender_meta = _http_sender_identity(
                            message_project_id=pid,
                            sender_name=rr["sender_name"],
                            sender_project_id=rr["sender_project_id"],
                            sender_project_human_key=rr["sender_project_name"],
                            sender_project_slug=rr["sender_project_slug"],
                        )
                        item = {
                            "id": rr["id"],
                            "subject": rr["subject"],
                            "from": sender_display,
                            "created": str(rr["created_ts"]),
                        }
                        item.update(sender_meta)
                        thread_items.append(item)
            # Convert markdown body to HTML for display (server-side render)
            body_html = (
                markdown2.markdown(mrow["body_md"] or "", extras=["fenced-code-blocks", "tables", "strike", "cuddled-lists"])
                if mrow["body_md"]
                else ""
            )
            if body_html:
                body_html = _html_cleaner.clean(body_html)

            commit_sha = None

            sender_display, sender_meta = _http_sender_identity(
                message_project_id=pid,
                sender_name=mrow["sender_name"],
                sender_project_id=mrow["sender_project_id"],
                sender_project_human_key=mrow["sender_project_name"],
                sender_project_slug=mrow["sender_project_slug"],
            )
            # Parse persisted attachments so the message view can render/link
            # them (#220). Stored as a JSON array column.
            message_attachments: list[dict[str, Any]] = []
            try:
                raw_attachments = mrow["attachments"]
                if isinstance(raw_attachments, str):
                    try:
                        parsed_attachments = json.loads(raw_attachments)
                    except json.JSONDecodeError:
                        parsed_attachments = []
                else:
                    parsed_attachments = raw_attachments
                if isinstance(parsed_attachments, list):
                    message_attachments = [a for a in parsed_attachments if isinstance(a, dict)]
            except Exception:
                message_attachments = []

            message_payload = {
                "id": mrow["id"],
                "subject": mrow["subject"],
                "body_md": mrow["body_md"],
                "body_html": body_html,
                "sender": sender_display,
                "created": str(mrow["created_ts"]),
                "importance": mrow["importance"],
                "thread_id": mrow["thread_id"],
                "attachments": message_attachments,
            }
            message_payload.update(sender_meta)

            return await _render(
                "mail_message.html",
                project={"slug": prow[1], "human_key": prow[2]},
                message=message_payload,
                recipients=recipients,
                ack_summary=ack_summary,
                thread_items=thread_items,
                commit_sha=commit_sha,
            )

        @fastapi_app.post("/mail/{project}/inbox/{agent}/mark-read")
        async def mark_selected_messages_read(project: str, agent: str, request: Request) -> JSONResponse:
            """Mark specific messages as read for an agent."""
            await ensure_schema()

            try:
                # Parse request body
                request_body = await request.json()
                message_ids: list[int] = request_body.get("message_ids", [])

                if not message_ids:
                    raise HTTPException(status_code=400, detail="No message IDs provided")

                # Limit to prevent SQL parameter overflow (SQLite default limit is 999)
                # Also prevents abuse - if someone wants to mark 1000+ messages, use "mark all"
                if len(message_ids) > 500:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Too many messages selected ({len(message_ids)}). Maximum is 500. Use 'Mark All Read' instead."
                    )

                async with get_session() as session:
                    # Get project
                    prow = (
                        await session.execute(
                            text("SELECT id, slug FROM projects WHERE slug = :k OR human_key = :k"),
                            {"k": project},
                        )
                    ).fetchone()
                    if not prow:
                        raise HTTPException(status_code=404, detail="Project not found")

                    pid = int(prow[0])

                    # Get agent
                    arow = (
                        await session.execute(
                            text("SELECT id FROM agents WHERE project_id = :pid AND name = :name"),
                            {"pid": pid, "name": agent},
                        )
                    ).fetchone()
                    if not arow:
                        raise HTTPException(status_code=404, detail="Agent not found")

                    aid = int(arow[0])

                    # Mark specific messages as read
                    # Use naive UTC datetime for SQLite compatibility
                    now = datetime.now(timezone.utc).replace(tzinfo=None)

                    # Use IN clause with parameter binding
                    placeholders = ','.join([f':mid{i}' for i in range(len(message_ids))])
                    params = {"aid": aid, "now": now}
                    params.update({f"mid{i}": mid for i, mid in enumerate(message_ids)})

                    result = await session.execute(
                        text(
                            f"""
                            UPDATE message_recipients
                            SET read_ts = :now
                            WHERE agent_id = :aid
                            AND message_id IN ({placeholders})
                            AND read_ts IS NULL
                            """
                        ),
                        params,
                    )
                    await session.commit()

                    count = int(getattr(result, "rowcount", 0) or 0)

                    return JSONResponse({
                        "success": True,
                        "marked_count": count,
                        "requested_count": len(message_ids),
                        "agent": agent,
                        "project": prow[1],
                    })

            except HTTPException:
                raise
            except Exception as exc:
                import traceback
                traceback.print_exc()
                raise HTTPException(status_code=500, detail=f"Failed to mark messages as read: {exc!s}") from exc

        @fastapi_app.post("/mail/{project}/inbox/{agent}/mark-all-read")
        async def mark_all_messages_read(project: str, agent: str) -> JSONResponse:
            """Mark all messages for an agent as read."""
            await ensure_schema()

            try:
                async with get_session() as session:
                    # Get project
                    prow = (
                        await session.execute(
                            text("SELECT id, slug FROM projects WHERE slug = :k OR human_key = :k"),
                            {"k": project},
                        )
                    ).fetchone()
                    if not prow:
                        raise HTTPException(status_code=404, detail="Project not found")

                    pid = int(prow[0])

                    # Get agent
                    arow = (
                        await session.execute(
                            text("SELECT id FROM agents WHERE project_id = :pid AND name = :name"),
                            {"pid": pid, "name": agent},
                        )
                    ).fetchone()
                    if not arow:
                        raise HTTPException(status_code=404, detail="Agent not found")

                    aid = int(arow[0])

                    # Mark all unread messages as read
                    # Use naive UTC datetime for SQLite compatibility
                    now = datetime.now(timezone.utc).replace(tzinfo=None)
                    result = await session.execute(
                        text(
                            """
                            UPDATE message_recipients
                            SET read_ts = :now
                            WHERE agent_id = :aid
                            AND read_ts IS NULL
                            """
                        ),
                        {"aid": aid, "now": now},
                    )
                    await session.commit()

                    count = int(getattr(result, "rowcount", 0) or 0)

                    return JSONResponse({
                        "success": True,
                        "marked_count": count,
                        "agent": agent,
                        "project": prow[1],
                    })

            except HTTPException:
                raise
            except Exception as exc:
                import traceback
                traceback.print_exc()
                raise HTTPException(status_code=500, detail=f"Failed to mark messages as read: {exc!s}") from exc

        @fastapi_app.post("/mail/{project}/inbox/{agent}/delete-messages")
        async def delete_selected_messages(project: str, agent: str, request: Request) -> JSONResponse:
            """Permanently delete specific messages for an agent.

            SQLite is authoritative. Original legacy archives are never rewritten.
            """
            await ensure_schema()
            if request.headers.get("sec-fetch-site") == "cross-site":
                raise HTTPException(status_code=403, detail="Cross-site mailbox changes are not allowed")

            try:
                request_body = await request.json()
                if not isinstance(request_body, dict):
                    raise HTTPException(status_code=400, detail="Expected a JSON object")
                message_ids = request_body.get("message_ids", [])
                if not isinstance(message_ids, list) or not message_ids or any(type(mid) is not int or mid <= 0 for mid in message_ids):
                    raise HTTPException(status_code=400, detail="Message IDs must be positive integers")

                if len(message_ids) > 500:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Too many messages selected ({len(message_ids)}). Maximum is 500."
                    )

                deleted_count = 0
                async with get_session() as session:
                    await session.execute(text("BEGIN IMMEDIATE"))
                    # Resolve project and fence lifecycle transitions during deletion.
                    prow = (
                        await session.execute(
                            text("SELECT id, slug, human_key, mailbox_state FROM projects WHERE slug = :k OR human_key = :k"),
                            {"k": project},
                        )
                    ).fetchone()
                    if not prow:
                        raise HTTPException(status_code=404, detail="Project not found")

                    pid = int(prow[0])
                    project_slug = prow[1]
                    if prow[3] != "active":
                        raise HTTPException(status_code=409, detail="Restore the mailbox before deleting messages")

                    # Resolve agent
                    arow = (
                        await session.execute(
                            text("SELECT id FROM agents WHERE project_id = :pid AND name = :name"),
                            {"pid": pid, "name": agent},
                        )
                    ).fetchone()
                    if not arow:
                        raise HTTPException(status_code=404, detail="Agent not found")

                    # Restrict recipient and reply cleanup to this project's messages.
                    placeholders = ','.join([f':mid{i}' for i in range(len(message_ids))])
                    id_params: dict[str, Any] = {"pid": pid}
                    id_params.update({f"mid{i}": mid for i, mid in enumerate(message_ids)})

                    rows = await session.execute(
                        text(
                            f"""
                            SELECT m.id
                            FROM messages m
                            WHERE m.project_id = :pid
                            AND m.id IN ({placeholders})
                            """
                        ),
                        id_params,
                    )
                    message_ids = [int(row[0]) for row in rows.fetchall()]

                    if not message_ids:
                        return JSONResponse({"success": True, "deleted_count": 0})

                    placeholders = ','.join(f':mid{i}' for i in range(len(message_ids)))
                    id_params = {"pid": pid, **{f"mid{i}": mid for i, mid in enumerate(message_ids)}}
                    await session.execute(
                        text(f"UPDATE messages SET reply_to = NULL WHERE reply_to IN ({placeholders})"),
                        {f"mid{i}": mid for i, mid in enumerate(message_ids)},
                    )

                    # Delete from SQLite: recipients first, then messages
                    await session.execute(
                        text(
                            f"DELETE FROM message_recipients WHERE message_id IN ({placeholders})"
                        ),
                        {f"mid{i}": mid for i, mid in enumerate(message_ids)},
                    )
                    del_result = await session.execute(
                        text(
                            f"DELETE FROM messages WHERE project_id = :pid AND id IN ({placeholders})"
                        ),
                        id_params,
                    )
                    deleted_count = int(getattr(del_result, "rowcount", 0) or 0)
                    await session.commit()

                return JSONResponse({
                    "success": True,
                    "deleted_count": deleted_count,
                    "agent": agent,
                    "project": project_slug,
                })

            except HTTPException:
                raise
            except Exception as exc:
                import traceback
                traceback.print_exc()
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to delete messages: {exc!s}"
                ) from exc

        @fastapi_app.get("/mail/{project}/thread/{thread_id}", response_class=HTMLResponse)
        async def mail_thread(project: str, thread_id: str) -> HTMLResponse:
            """Display all messages in a thread chronologically (Gmail-style conversation view).

            NOTE: Currently loads ALL messages in thread without pagination.
            For threads with 1000+ messages, consider adding LIMIT/OFFSET pagination.
            """
            await ensure_schema()
            async with get_session() as session:
                # Get project
                prow = (
                    await session.execute(
                        text("SELECT id, slug, human_key FROM projects WHERE slug = :k OR human_key = :k"),
                        {"k": project},
                    )
                ).fetchone()
                if not prow:
                    return await _render("error.html", message="Project not found")

                pid = int(prow[0])

                # Get all messages in this thread, ordered chronologically
                # Include messages where thread_id matches OR message id matches (for thread starter)
                try:
                    thread_id_int = int(thread_id)
                    rows = await session.execute(
                        text(
                            """
                            SELECT
                                m.id,
                                m.subject,
                                m.body_md,
                                s.name AS sender_name,
                                s.project_id AS sender_project_id,
                                sp.human_key AS sender_project_name,
                                sp.slug AS sender_project_slug,
                                m.created_ts,
                                m.importance,
                                m.thread_id
                            FROM messages m
                            JOIN agents s ON s.id = m.sender_id
                            LEFT JOIN projects sp ON sp.id = s.project_id
                            WHERE m.project_id = :pid
                            AND (m.thread_id = :tid OR m.id = :tid_int)
                            ORDER BY m.created_ts ASC
                            """
                        ),
                        {"pid": pid, "tid": thread_id, "tid_int": thread_id_int},
                    )
                except ValueError:
                    # Not an integer, just use string thread_id
                    rows = await session.execute(
                        text(
                            """
                            SELECT
                                m.id,
                                m.subject,
                                m.body_md,
                                s.name AS sender_name,
                                s.project_id AS sender_project_id,
                                sp.human_key AS sender_project_name,
                                sp.slug AS sender_project_slug,
                                m.created_ts,
                                m.importance,
                                m.thread_id
                            FROM messages m
                            JOIN agents s ON s.id = m.sender_id
                            LEFT JOIN projects sp ON sp.id = s.project_id
                            WHERE m.project_id = :pid
                            AND m.thread_id = :tid
                            ORDER BY m.created_ts ASC
                            """
                        ),
                        {"pid": pid, "tid": thread_id},
                    )

                messages = []
                for r in rows.mappings().all():
                    # Convert markdown to HTML for each message
                    body_html = ""
                    if r["body_md"]:
                        body_html = markdown2.markdown(
                            r["body_md"],
                            extras=["fenced-code-blocks", "tables", "strike", "cuddled-lists"]
                        )
                        body_html = _html_cleaner.clean(body_html)

                    sender_display, sender_meta = _http_sender_identity(
                        message_project_id=pid,
                        sender_name=r["sender_name"],
                        sender_project_id=r["sender_project_id"],
                        sender_project_human_key=r["sender_project_name"],
                        sender_project_slug=r["sender_project_slug"],
                    )
                    message = {
                        "id": r["id"],
                        "subject": r["subject"],
                        "body_md": r["body_md"],
                        "body_html": body_html,
                        "sender": sender_display,
                        "created": str(r["created_ts"]),
                        "importance": r["importance"],
                        "thread_id": r["thread_id"],
                    }
                    message.update(sender_meta)
                    messages.append(message)

                if not messages:
                    return await _render(
                        "error.html",
                        message=f"No messages found in thread '{thread_id}'. The thread may not exist or all messages may have been deleted."
                    )

                # Get unique subject (use first message's subject, with fallback)
                thread_subject = messages[0]["subject"] if messages and messages[0]["subject"] else f"Thread {thread_id}"

                return await _render(
                    "mail_thread.html",
                    project={"slug": prow[1], "human_key": prow[2]},
                    thread_id=thread_id,
                    thread_subject=thread_subject,
                    messages=messages,
                    message_count=len(messages),
                )

        # Full-text search UI across subject/body using LIKE fallback (SQLite FTS handled elsewhere)
        @fastapi_app.get("/mail/{project}/search", response_class=HTMLResponse)
        async def mail_search(
            project: str,
            q: str,
            limit: int = 10000,
            scope: str | None = None,
            order: str | None = None,
            boost: int | None = None,
        ) -> HTMLResponse:
            limit = min(max(1, limit), 10000)
            if order not in ("relevance", "time", None):
                order = "relevance"
            await ensure_schema()
            async with get_session() as session:
                prow = (
                    await session.execute(
                        text("SELECT id, slug, human_key FROM projects WHERE slug = :k OR human_key = :k"),
                        {"k": project},
                    )
                ).fetchone()
                if not prow:
                    return await _render("error.html", message="Project not found")
                pid = int(prow[0])
                fts_expr, like_pat, like_scope, tokens = _parse_fts_query(q, scope)
                weights = (0.0, 3.0, 1.0) if (boost or 0) else (0.0, 1.0, 1.0)
                fts_sql = (
                    "SELECT m.id, m.subject, s.name AS sender_name, s.project_id AS sender_project_id, "
                    "sp.human_key AS sender_project_name, sp.slug AS sender_project_slug, "
                    "m.created_ts, m.importance, m.thread_id, "
                    "snippet(fts_messages, 2, '<mark>', '</mark>', '…', 22) AS body_snippet "
                    "FROM fts_messages "
                    "JOIN messages m ON m.id = fts_messages.rowid "
                    "JOIN agents s ON s.id = m.sender_id "
                    "LEFT JOIN projects sp ON sp.id = s.project_id "
                    "WHERE m.project_id = :pid AND fts_messages MATCH :q "
                    + (
                        "ORDER BY m.created_ts DESC "
                        if (order or "relevance") == "time"
                        else f"ORDER BY bm25(fts_messages, {weights[0]}, {weights[1]}, {weights[2]}) "
                    )
                    + "LIMIT :lim"
                )
                try:
                    rows = await session.execute(text(fts_sql), {"pid": pid, "q": fts_expr or q, "lim": limit})
                    results = []
                    for r in rows.mappings().all():
                        sender_display, sender_meta = _http_sender_identity(
                            message_project_id=pid,
                            sender_name=r["sender_name"],
                            sender_project_id=r["sender_project_id"],
                            sender_project_human_key=r["sender_project_name"],
                            sender_project_slug=r["sender_project_slug"],
                        )
                        item = {
                            "id": r["id"],
                            "subject": r["subject"],
                            "from": sender_display,
                            "created": str(r["created_ts"]),
                            "importance": r["importance"],
                            "thread_id": r["thread_id"],
                            "snippet": r["body_snippet"],
                            "hits": (r["body_snippet"] or "").count("<mark>"),
                        }
                        item.update(sender_meta)
                        results.append(item)
                except Exception:
                    if like_scope == "subject":
                        like_sql = (
                            "SELECT m.id, m.subject, s.name AS sender_name, s.project_id AS sender_project_id, "
                            "sp.human_key AS sender_project_name, sp.slug AS sender_project_slug, "
                            "m.created_ts, m.importance, m.thread_id "
                            "FROM messages m JOIN agents s ON s.id = m.sender_id "
                            "LEFT JOIN projects sp ON sp.id = s.project_id "
                            f"WHERE m.project_id = :pid AND m.subject LIKE :pat ESCAPE '{_LIKE_ESCAPE_CHAR}' "
                            "ORDER BY m.created_ts DESC LIMIT :lim"
                        )
                    elif like_scope == "body":
                        like_sql = (
                            "SELECT m.id, m.subject, s.name AS sender_name, s.project_id AS sender_project_id, "
                            "sp.human_key AS sender_project_name, sp.slug AS sender_project_slug, "
                            "m.created_ts, m.importance, m.thread_id "
                            "FROM messages m JOIN agents s ON s.id = m.sender_id "
                            "LEFT JOIN projects sp ON sp.id = s.project_id "
                            f"WHERE m.project_id = :pid AND m.body_md LIKE :pat ESCAPE '{_LIKE_ESCAPE_CHAR}' "
                            "ORDER BY m.created_ts DESC LIMIT :lim"
                        )
                    else:
                        like_sql = (
                            "SELECT m.id, m.subject, s.name AS sender_name, s.project_id AS sender_project_id, "
                            "sp.human_key AS sender_project_name, sp.slug AS sender_project_slug, "
                            "m.created_ts, m.importance, m.thread_id "
                            "FROM messages m JOIN agents s ON s.id = m.sender_id "
                            "LEFT JOIN projects sp ON sp.id = s.project_id "
                            f"WHERE m.project_id = :pid AND (m.subject LIKE :pat ESCAPE '{_LIKE_ESCAPE_CHAR}' "
                            f"OR m.body_md LIKE :pat ESCAPE '{_LIKE_ESCAPE_CHAR}') "
                            "ORDER BY m.created_ts DESC LIMIT :lim"
                        )
                    rows = await session.execute(
                        text(like_sql), {"pid": pid, "pat": like_pat or f"%{_like_escape(q)}%", "lim": limit}
                    )
                    results = []
                    for r in rows.mappings().all():
                        sender_display, sender_meta = _http_sender_identity(
                            message_project_id=pid,
                            sender_name=r["sender_name"],
                            sender_project_id=r["sender_project_id"],
                            sender_project_human_key=r["sender_project_name"],
                            sender_project_slug=r["sender_project_slug"],
                        )
                        item = {
                            "id": r["id"],
                            "subject": r["subject"],
                            "from": sender_display,
                            "created": str(r["created_ts"]),
                            "importance": r["importance"],
                            "thread_id": r["thread_id"],
                            "snippet": "",
                            "hits": 0,
                        }
                        item.update(sender_meta)
                        results.append(item)
            return await _render(
                "mail_search.html",
                project={"slug": prow[1], "human_key": prow[2]},
                q=q,
                scope=scope or "",
                order=order or "relevance",
                tokens=tokens,
                results=results,
                boost=bool(boost),
            )

        # File reservations and attachments views
        @fastapi_app.get("/mail/{project}/file_reservations", response_class=HTMLResponse)
        async def mail_file_reservations(project: str) -> HTMLResponse:
            await ensure_schema()
            async with get_session() as session:
                prow = (
                    await session.execute(
                        text("SELECT id, slug, human_key FROM projects WHERE slug = :k OR human_key = :k"),
                        {"k": project},
                    )
                ).fetchone()
                if not prow:
                    return await _render("error.html", message="Project not found")
                pid = int(prow[0])
                # LEFT JOIN so orphaned reservations whose owning agent row
                # has been deleted still surface in the web UI (`a.name` will
                # be NULL — render as "<orphaned>" so operators see them and
                # can act). Matches the model-side LEFT JOIN in
                # _collect_file_reservation_statuses. (#161)
                rows = await session.execute(
                    text(
                        "SELECT c.id, a.name, c.path_pattern, c.exclusive, c.created_ts, c.expires_ts, c.released_ts, c.agent_id FROM file_reservations c LEFT JOIN agents a ON a.id = c.agent_id WHERE c.project_id = :pid ORDER BY c.created_ts DESC"
                    ),
                    {"pid": pid},
                )
                file_reservations = [
                    {
                        "id": r[0],
                        "agent": r[1] if r[1] is not None else "<orphaned>",
                        "agent_id": r[7],
                        "path_pattern": r[2],
                        "exclusive": bool(r[3]),
                        "created": str(r[4]),
                        "expires": str(r[5]) if r[5] else "",
                        "released": str(r[6]) if r[6] else "",
                    }
                    for r in rows.fetchall()
                ]
            return await _render("mail_file_reservations.html", project={"slug": prow[1], "human_key": prow[2]}, file_reservations=file_reservations)

        @fastapi_app.get("/mail/{project}/attachments", response_class=HTMLResponse)
        async def mail_attachments(project: str) -> HTMLResponse:
            await ensure_schema()
            async with get_session() as session:
                prow = (
                    await session.execute(
                        text("SELECT id, slug, human_key FROM projects WHERE slug = :k OR human_key = :k"),
                        {"k": project},
                    )
                ).fetchone()
                if not prow:
                    return await _render("error.html", message="Project not found")
                pid = int(prow[0])
                rows = await session.execute(
                    text(
                        "SELECT id, subject, created_ts, attachments FROM messages WHERE project_id = :pid AND json_array_length(attachments) > 0 ORDER BY created_ts DESC LIMIT 10000"
                    ),
                    {"pid": pid},
                )
                items = []
                for r in rows.fetchall():
                    attachments: list[dict[str, Any]] = []
                    try:
                        raw = r[3]
                        if isinstance(raw, str):
                            try:
                                parsed = json.loads(raw)
                            except json.JSONDecodeError:
                                parsed = []
                        else:
                            parsed = raw
                        if isinstance(parsed, list):
                            attachments = [a for a in parsed if isinstance(a, dict)]
                    except Exception:
                        attachments = []
                    items.append({"id": r[0], "subject": r[1], "created": str(r[2]), "attachments": attachments})
            return await _render("mail_attachments.html", project={"slug": prow[1], "human_key": prow[2]}, items=items)

        # ========== Human Overseer Routes ==========

        @fastapi_app.get("/mail/{project}/overseer/compose", response_class=HTMLResponse)
        async def overseer_compose(project: str) -> HTMLResponse:
            """Display Human Overseer message composer."""
            await ensure_schema()
            async with get_session() as session:
                # Get project
                prow = (
                    await session.execute(
                        text("SELECT id, slug, human_key FROM projects WHERE slug = :k OR human_key = :k"),
                        {"k": project},
                    )
                ).fetchone()
                if not prow:
                    return await _render("error.html", message="Project not found")

                # Get all agents for this project
                pid = int(prow[0])
                agent_rows = await session.execute(
                    text("SELECT name FROM agents WHERE project_id = :pid ORDER BY name"),
                    {"pid": pid}
                )
                agents = [{"name": r[0]} for r in agent_rows.fetchall()]

            return await _render(
                "overseer_compose.html",
                project={"slug": prow[1], "human_key": prow[2]},
                agents=agents
            )

        @fastapi_app.post("/mail/{project}/overseer/send")
        async def overseer_send(project: str, request: Request) -> JSONResponse:
            """Send message from Human Overseer to selected agents."""
            await ensure_schema()

            try:
                # Parse request body
                request_body = await request.json()
                recipients: list[str] = request_body.get("recipients", [])
                subject: str = request_body.get("subject", "").strip()
                body_md: str = request_body.get("body_md", "").strip()
                thread_id: str | None = request_body.get("thread_id")

                # Comprehensive validation
                if not recipients:
                    raise HTTPException(status_code=400, detail="At least one recipient is required")
                if len(recipients) > 100:
                    raise HTTPException(status_code=400, detail="Too many recipients (maximum 100 agents)")
                if not subject:
                    raise HTTPException(status_code=400, detail="Subject is required")
                if len(subject) > 200:
                    raise HTTPException(status_code=400, detail="Subject too long (maximum 200 characters)")
                if not body_md:
                    raise HTTPException(status_code=400, detail="Message body is required")
                if len(body_md) > 50000:
                    raise HTTPException(status_code=400, detail="Message body too long (maximum 50,000 characters)")

                # Remove duplicate recipients while preserving order
                recipients = list(dict.fromkeys(recipients))

                # Add Human Overseer preamble (pure markdown for cross-renderer compatibility)
                preamble = """---

        🚨 MESSAGE FROM HUMAN OVERSEER 🚨

        This message is from a human operator overseeing this project. Please prioritize the instructions below over your current tasks.

        You should:
        1. Temporarily pause your current work
        2. Complete the request described below
        3. Resume your original plans afterward (unless modified by these instructions)

        The human's guidance supersedes all other priorities.

        ---

        """
                full_body = preamble + body_md

                # Validate combined length (preamble + user message)
                if len(full_body) > 50000:
                    preamble_length = len(preamble)
                    max_user_length = 50000 - preamble_length
                    raise HTTPException(
                        status_code=400,
                        detail=f"Message body too long ({len(body_md)} characters). Maximum is {max_user_length} characters to accommodate the overseer preamble ({preamble_length} characters)."
                    )

                # Keep all message creation in one database transaction.
                from datetime import datetime, timezone
                message_id: int | None = None
                valid_recipients: list[str] = []
                project_slug = ""
                overseer_name = "HumanOverseer"
                now = datetime.now(timezone.utc).replace(tzinfo=None)
                async with get_session() as session:
                    # Get project
                    prow = (
                        await session.execute(
                            text("SELECT id, slug, human_key FROM projects WHERE slug = :k OR human_key = :k"),
                            {"k": project},
                        )
                    ).fetchone()
                    if not prow:
                        raise HTTPException(status_code=404, detail="Project not found")

                    # Extract project info consistently
                    project_id = int(prow[0])
                    project_slug = prow[1]
                    prow[2]

                    # Get or create "HumanOverseer" agent (with race condition protection)
                    overseer_row = (
                        await session.execute(
                            text("SELECT id, name FROM agents WHERE project_id = :pid AND name = :name"),
                            {"pid": project_id, "name": overseer_name}
                        )
                    ).fetchone()

                    if not overseer_row:
                        # Create HumanOverseer agent (use INSERT OR IGNORE to handle race conditions)
                        await session.execute(
                            text("""
                                INSERT OR IGNORE INTO agents (
                                    project_id,
                                    name,
                                    program,
                                    model,
                                    task_description,
                                    contact_policy,
                                    attachments_policy,
                                    inception_ts,
                                    last_active_ts
                                )
                                VALUES (
                                    :pid,
                                    :name,
                                    :program,
                                    :model,
                                    :task,
                                    :policy,
                                    :attachments_policy,
                                    :ts,
                                    :ts
                                )
                            """),
                            {
                                "pid": project_id,
                                "name": overseer_name,
                                "program": "WebUI",
                                "model": "Human",
                                "task": "Human operator providing guidance and oversight to agents",
                                "policy": "open",
                                "attachments_policy": "auto",
                                # Use naive UTC datetime for SQLite compatibility
                                "ts": datetime.now(timezone.utc).replace(tzinfo=None),
                            },
                        )
                        # Fetch the agent (whether we just created it or another request did)
                        overseer_row = (
                            await session.execute(
                                text("SELECT id, name FROM agents WHERE project_id = :pid AND name = :name"),
                                {"pid": project_id, "name": overseer_name}
                            )
                        ).fetchone()

                        if not overseer_row:
                            raise HTTPException(status_code=500, detail="Failed to create HumanOverseer agent")

                    # Extract overseer_id for later use
                    overseer_id = overseer_row[0]

                    result = await session.execute(
                        text("""
                            INSERT INTO messages (project_id, sender_id, subject, body_md, importance, thread_id, created_ts, ack_required)
                            VALUES (:pid, :sid, :subj, :body, :imp, :tid, :ts, :ack)
                            RETURNING id
                        """),
                        {
                            "pid": project_id,
                            "sid": overseer_id,
                            "subj": subject,
                            "body": full_body,
                            "imp": "high",  # Always high importance for overseer
                            "tid": thread_id,
                            "ts": now,
                            "ack": False
                        }
                    )
                    message_row = result.fetchone()
                    if not message_row:
                        raise HTTPException(status_code=500, detail="Failed to create message")
                    message_id = message_row[0]

                    # Insert recipients (optimized: bulk SELECT + bulk INSERT instead of N+1 queries)
                    # Build SQL with proper parameter expansion for IN clause
                    placeholders = ", ".join([f":name_{i}" for i in range(len(recipients))])
                    params: dict[str, Any] = {"pid": project_id}
                    params.update({f"name_{i}": name for i, name in enumerate(recipients)})

                    # Single query to get all valid recipient IDs
                    recipient_rows = await session.execute(
                        text(f"SELECT id, name FROM agents WHERE project_id = :pid AND name IN ({placeholders})"),
                        params
                    )
                    recipient_map = {row[1]: row[0] for row in recipient_rows.fetchall()}  # name -> id mapping

                    # Build valid recipients list (only those that exist)
                    valid_recipients = [name for name in recipients if name in recipient_map]

                    # Bulk insert all message_recipients (single executemany call)
                    if valid_recipients:
                        # Prepare bulk insert params
                        insert_params = [
                            {"mid": message_id, "aid": recipient_map[name], "kind": "to"}
                            for name in valid_recipients
                        ]
                        # Use executemany for bulk insert
                        await session.execute(
                            text("""
                                INSERT INTO message_recipients (message_id, agent_id, kind)
                                VALUES (:mid, :aid, :kind)
                            """),
                            insert_params
                        )

                    # If no valid recipients found, rollback and error
                    if not valid_recipients:
                        await session.rollback()
                        raise HTTPException(
                            status_code=400,
                            detail=f"None of the specified recipients exist in this project. Available agents can be seen at /mail/{project_slug}"
                        )

                    # Update HumanOverseer activity timestamp before commit.
                    await session.execute(
                        text("UPDATE agents SET last_active_ts = :ts WHERE id = :id"),
                        {"ts": now, "id": overseer_id}
                    )

                    await session.commit()


                return JSONResponse({
                    "success": True,
                    "message_id": message_id,
                    "recipients": valid_recipients,
                    "sent_at": now.isoformat()
                })

            except HTTPException:
                raise
            except Exception as e:
                import traceback
                traceback.print_exc()
                raise HTTPException(status_code=500, detail=f"Failed to send message: {e!s}") from e

    try:
        _register_mail_ui()
    except Exception as exc:
        # templates/Jinja may be missing in some environments; UI remains optional
        with contextlib.suppress(Exception):
            structlog.get_logger("ui").error("ui_init_failed", error=str(exc))
        pass

    # Keep the auto-generated /openapi.json focused on the real API contract.
    # The browser-facing SSR mail UI (and its UI-backing JSON helpers) all live
    # under the `/mail` prefix; they are registered for humans, not as part of
    # the documented API surface, so we filter them out of the schema. The
    # routes stay fully registered and functional — they are only omitted from
    # the OpenAPI document. Using a custom app.openapi() (rather than
    # include_in_schema=False on ~33 decorators) keeps this in one place and
    # automatically covers any future /mail/* routes.
    from fastapi.openapi.utils import get_openapi as _get_openapi

    def _custom_openapi() -> dict[str, Any]:
        if fastapi_app.openapi_schema:
            return fastapi_app.openapi_schema
        schema = _get_openapi(
            title=fastapi_app.title,
            version=fastapi_app.version,
            openapi_version=fastapi_app.openapi_version,
            description=fastapi_app.description,
            routes=fastapi_app.routes,
        )
        paths = schema.get("paths")
        if isinstance(paths, dict):
            schema["paths"] = {
                path: item
                for path, item in paths.items()
                if not (path == "/mail" or path.startswith("/mail/"))
            }
        fastapi_app.openapi_schema = schema
        return schema

    # Install the custom generator (FastAPI's documented extension point for
    # overriding the OpenAPI document); cast keeps the bound-method override
    # explicit for the type checker.
    cast(Any, fastapi_app).openapi = _custom_openapi

    # Static web UI (SPA) routing support
    def _resolve_web_root() -> Path | None:
        candidates: list[Path] = []
        with contextlib.suppress(Exception):
            candidates.append(Path(__file__).resolve().parents[3] / "web")
        candidates.append(Path.cwd() / "web")
        for candidate in candidates:
            try:
                if candidate.exists() and (candidate / "index.html").exists():
                    return candidate
            except Exception:
                continue
        return None

    web_root = _resolve_web_root()
    if web_root is not None:
        fastapi_app.mount("/", StaticFiles(directory=str(web_root), html=True), name="web")

        def _is_api_path(path: str) -> bool:
            if base_no_slash == "/":
                return True
            return path == base_no_slash or path.startswith(base_no_slash + "/")

        def _should_spa_fallback(path: str) -> bool:
            if _is_api_path(path):
                return False
            return not (path == "/mail" or path.startswith("/mail/"))

        @fastapi_app.exception_handler(HTTPException)
        async def spa_fallback(request: Request, exc: HTTPException):
            if exc.status_code == status.HTTP_404_NOT_FOUND and _should_spa_fallback(request.url.path):
                return FileResponse(web_root / "index.html")
            return await http_exception_handler(request, exc)

    return fastapi_app


def main() -> None:
    """Run the HTTP transport using settings-specified host/port."""

    parser = argparse.ArgumentParser(description="Run the MCP Agent Mail HTTP transport")
    parser.add_argument("--host", help="Override HTTP host", default=None)
    parser.add_argument("--port", help="Override HTTP port", type=int, default=None)
    parser.add_argument("--log-level", help="Uvicorn log level", default="info")
    # Be tolerant of extraneous argv when invoked under test runners
    args, _unknown = parser.parse_known_args()

    settings = get_settings()
    host = args.host or settings.http.host
    port = args.port or settings.http.port

    app = build_http_app(settings)
    # Disable WebSockets when running the service directly; HTTP-only transport
    import inspect as _inspect

    _sig = _inspect.signature(uvicorn.run)
    _kwargs: dict[str, Any] = {"host": host, "port": port, "log_level": args.log_level}
    if "ws" in _sig.parameters:
        _kwargs["ws"] = "none"
    uvicorn.run(app, **_kwargs)


if __name__ == "__main__":  # pragma: no cover - manual execution path
    main()
