"""Codex STDIO adapter for trusted Agent Mail conversation identity."""

from __future__ import annotations

import copy
import datetime
import hashlib
import json
import logging
import re
import secrets
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, TypeVar
from urllib.parse import urlsplit, urlunsplit

import keyring
import mcp.types
from decouple import Config as DecoupleConfig, RepositoryEnv
from fastmcp import FastMCP
from fastmcp.client.progress import ProgressHandler
from fastmcp.client.transports import ClientTransport, StreamableHttpTransport
from fastmcp.server.proxy import ProxyClient
from keyring.errors import KeyringError
from pydantic import AnyUrl

from .identity import IDENTITY_META_KEY

MCP_AGENT_MAIL_URL = "MCP_AGENT_MAIL_URL"
MCP_AGENT_MAIL_BEARER_TOKEN = "MCP_AGENT_MAIL_BEARER_TOKEN"
CODEX_THREAD_ID = "CODEX_THREAD_ID"
MCP_AGENT_MAIL_CLIENT_LABEL = "MCP_AGENT_MAIL_CLIENT_LABEL"

_OPAQUE_ID_PATTERN = re.compile(r"^[A-Za-z0-9._~-]{16,256}$")
_CLIENT_SECRET_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,256}$")
_ENV_VAR_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CREDENTIAL_USERNAME = "client-principal"
_CREDENTIAL_VERSION = 1

TransportT = TypeVar("TransportT", bound=ClientTransport)


class CodexAdapterError(RuntimeError):
    """An adapter configuration or credential-store failure safe to show on stderr."""


class CredentialStore(Protocol):
    """Minimal password-store contract used by the adapter and its tests."""

    def get_password(self, service: str, username: str) -> str | None: ...

    def set_password(self, service: str, username: str, password: str) -> None: ...


class SystemCredentialStore:
    """Store adapter client credentials in the operating-system credential manager."""

    def get_password(self, service: str, username: str) -> str | None:
        return keyring.get_password(service, username)

    def set_password(self, service: str, username: str, password: str) -> None:
        keyring.set_password(service, username, password)


@dataclass(frozen=True, slots=True)
class CodexAdapterSettings:
    """Validated static connection config plus one Codex task identity."""

    upstream_url: str
    canonical_upstream_url: str
    bearer_token: str | None = field(repr=False)
    conversation_uid: str = field(repr=False)
    client_label: str = "Codex"


@dataclass(frozen=True, slots=True)
class StoredClientIdentity:
    """Stable, model-inaccessible credentials scoped to one upstream endpoint."""

    client_uid: str = field(repr=False)
    client_secret: str = field(repr=False)


def canonicalize_upstream_url(value: str) -> str:
    """Normalize an HTTP MCP endpoint for credential-store scoping."""
    raw = value.strip()
    if not raw:
        raise CodexAdapterError(f"{MCP_AGENT_MAIL_URL} is required.")
    parsed = urlsplit(raw)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise CodexAdapterError("Agent Mail upstream URL must be an absolute HTTP or HTTPS URL.")
    if parsed.username is not None or parsed.password is not None:
        raise CodexAdapterError("Agent Mail upstream URL must not contain credentials.")
    if parsed.query or parsed.fragment:
        raise CodexAdapterError("Agent Mail upstream URL must not contain a query string or fragment.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise CodexAdapterError("Agent Mail upstream URL contains an invalid port.") from exc

    scheme = parsed.scheme.lower()
    host = parsed.hostname.casefold()
    rendered_host = f"[{host}]" if ":" in host else host
    default_port = 80 if scheme == "http" else 443
    netloc = rendered_host if port in {None, default_port} else f"{rendered_host}:{port}"
    path = (parsed.path or "/").rstrip("/") or "/"
    return urlunsplit((scheme, netloc, path, "", ""))


def load_adapter_settings(
    env_file: Path,
    *,
    upstream_url: str | None = None,
    bearer_token_env_var: str | None = None,
) -> CodexAdapterSettings:
    """Load adapter configuration from one explicit env file and its process environment."""
    if not env_file.is_file():
        raise CodexAdapterError(f"Adapter env file does not exist: {env_file}")

    decouple_config = DecoupleConfig(RepositoryEnv(str(env_file)))
    resolved_url = (upstream_url or decouple_config(MCP_AGENT_MAIL_URL, default="")).strip()
    canonical_url = canonicalize_upstream_url(resolved_url)
    token_setting = bearer_token_env_var or MCP_AGENT_MAIL_BEARER_TOKEN
    if not _ENV_VAR_NAME_PATTERN.fullmatch(token_setting):
        raise CodexAdapterError("Bearer-token environment variable name is invalid.")
    bearer_token = decouple_config(token_setting, default="").strip() or None
    if bearer_token_env_var is not None and bearer_token is None:
        raise CodexAdapterError(f"Bearer-token environment variable is unavailable: {bearer_token_env_var}")
    conversation_uid = decouple_config(CODEX_THREAD_ID, default="").strip()
    if not _OPAQUE_ID_PATTERN.fullmatch(conversation_uid):
        raise CodexAdapterError(
            "Codex did not provide a valid CODEX_THREAD_ID to the adapter process; "
            "persistent conversation identity cannot be established."
        )
    client_label = decouple_config(MCP_AGENT_MAIL_CLIENT_LABEL, default="Codex").strip()[:128] or "Codex"
    return CodexAdapterSettings(
        upstream_url=resolved_url,
        canonical_upstream_url=canonical_url,
        bearer_token=bearer_token,
        conversation_uid=conversation_uid,
        client_label=client_label,
    )


def credential_service_name(canonical_upstream_url: str) -> str:
    """Return a non-secret, endpoint-specific keyring service name."""
    fingerprint = hashlib.sha256(canonical_upstream_url.encode("utf-8")).hexdigest()
    return f"mcp-agent-mail/codex-adapter/{fingerprint}"


def _parse_stored_identity(raw: str) -> StoredClientIdentity:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CodexAdapterError("The stored Codex adapter credential is not valid JSON.") from exc
    if not isinstance(payload, dict) or payload.get("version") != _CREDENTIAL_VERSION:
        raise CodexAdapterError("The stored Codex adapter credential has an unsupported format.")
    client_uid = str(payload.get("client_uid") or "").strip()
    client_secret = str(payload.get("client_secret") or "").strip()
    if not _OPAQUE_ID_PATTERN.fullmatch(client_uid) or not _CLIENT_SECRET_PATTERN.fullmatch(client_secret):
        raise CodexAdapterError("The stored Codex adapter credential is malformed.")
    return StoredClientIdentity(client_uid=client_uid, client_secret=client_secret)


def load_or_create_client_identity(
    canonical_upstream_url: str,
    *,
    credential_store: CredentialStore | None = None,
) -> StoredClientIdentity:
    """Load or create a stable client principal for exactly one upstream endpoint."""
    store = credential_store or SystemCredentialStore()
    service = credential_service_name(canonical_upstream_url)
    try:
        stored = store.get_password(service, _CREDENTIAL_USERNAME)
        if stored:
            return _parse_stored_identity(stored)

        generated = StoredClientIdentity(
            client_uid=f"codex-{secrets.token_urlsafe(24)}",
            client_secret=secrets.token_urlsafe(32),
        )
        serialized = json.dumps(
            {
                "version": _CREDENTIAL_VERSION,
                "client_uid": generated.client_uid,
                "client_secret": generated.client_secret,
            },
            separators=(",", ":"),
        )
        store.set_password(service, _CREDENTIAL_USERNAME, serialized)
        confirmed = store.get_password(service, _CREDENTIAL_USERNAME)
    except KeyringError as exc:
        raise CodexAdapterError(
            "The operating-system credential store is unavailable; the Codex adapter cannot safely persist its identity."
        ) from exc
    if not confirmed:
        raise CodexAdapterError("The operating-system credential store did not retain the Codex adapter identity.")
    return _parse_stored_identity(confirmed)


def build_identity_metadata(
    settings: CodexAdapterSettings,
    identity: StoredClientIdentity,
) -> dict[str, Any]:
    """Build the reserved model-inaccessible metadata injected into every tool call."""
    return {
        IDENTITY_META_KEY: {
            "version": 1,
            "client_uid": identity.client_uid,
            "client_secret": identity.client_secret,
            "conversation_uid": settings.conversation_uid,
            "client_label": settings.client_label,
            "capabilities": [],
        }
    }


class CodexIdentityProxyClient(ProxyClient[TransportT]):
    """FastMCP proxy client that adds trusted identity metadata out of band."""

    def __init__(
        self,
        transport: TransportT,
        *,
        identity_metadata: dict[str, Any],
        **kwargs: Any,
    ) -> None:
        self._identity_metadata = copy.deepcopy(identity_metadata)
        super().__init__(transport, **kwargs)

    async def call_tool_mcp(
        self,
        name: str,
        arguments: dict[str, Any],
        progress_handler: ProgressHandler | None = None,
        timeout: datetime.timedelta | float | int | None = None,  # noqa: ASYNC109 - required by FastMCP override
    ) -> mcp.types.CallToolResult:
        """Call the upstream tool with adapter-owned request metadata."""
        if isinstance(timeout, int | float):
            timeout = datetime.timedelta(seconds=float(timeout))
        return await self.session.call_tool(
            name=name,
            arguments=arguments,
            read_timeout_seconds=timeout,
            progress_callback=progress_handler or self._progress_handler,
            meta=self._identity_metadata,
        )

    async def read_resource_mcp(self, uri: AnyUrl | str) -> mcp.types.ReadResourceResult:
        """Read an upstream resource with the same trusted identity metadata."""
        if isinstance(uri, str):
            try:
                uri = AnyUrl(uri)
            except Exception as exc:
                raise ValueError(f"Provided resource URI is invalid: {uri!r}") from exc
        params = mcp.types.ReadResourceRequestParams(
            uri=uri,
            _meta=mcp.types.RequestParams.Meta.model_validate(self._identity_metadata),
        )
        return await self.session.send_request(
            mcp.types.ClientRequest(mcp.types.ReadResourceRequest(params=params)),
            mcp.types.ReadResourceResult,
        )


def build_codex_adapter_server(
    settings: CodexAdapterSettings,
    *,
    credential_store: CredentialStore | None = None,
    transport: ClientTransport | None = None,
) -> FastMCP:
    """Build the local STDIO proxy without starting it."""
    identity = load_or_create_client_identity(
        settings.canonical_upstream_url,
        credential_store=credential_store,
    )
    resolved_transport = transport or StreamableHttpTransport(
        settings.upstream_url,
        auth=settings.bearer_token,
    )
    client = CodexIdentityProxyClient(
        resolved_transport,
        identity_metadata=build_identity_metadata(settings, identity),
    )
    return FastMCP.as_proxy(
        client,
        name="MCP Agent Mail Codex Adapter",
        instructions=(
            "This local adapter supplies trusted per-task identity metadata to Agent Mail. "
            "Use macro_start_session or ensure_agent_identity normally; never request or expose adapter credentials."
        ),
    )


def run_codex_adapter(settings: CodexAdapterSettings) -> None:
    """Run the adapter over STDIO, keeping protocol stdout free of diagnostics."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        stream=sys.stderr,
        force=True,
    )
    server = build_codex_adapter_server(settings)
    server.run(transport="stdio", show_banner=False)
