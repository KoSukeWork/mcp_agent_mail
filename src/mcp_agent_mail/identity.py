"""Persistent MCP client and per-conversation Agent identity bindings."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, cast

from sqlalchemy import func
from sqlmodel import select

from .db import ensure_schema, get_immediate_session, get_session
from .models import (
    Agent,
    AgentConversationBinding,
    IdentityConfirmationRequest,
    McpClientPrincipal,
    Project,
)

IDENTITY_META_KEY = "io.github.mcp-agent-mail/identity"
_OPAQUE_ID_PATTERN = re.compile(r"^[A-Za-z0-9._~-]{16,256}$")
_CLIENT_SECRET_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,256}$")


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ConversationIdentityError(Exception):
    """Typed identity failure converted to a public MCP tool error by app.py."""

    def __init__(self, error_type: str, message: str, *, data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.data = data or {}


@dataclass(frozen=True, slots=True)
class ClientConversationCredentials:
    """Model-inaccessible identity metadata injected by a trusted MCP adapter."""

    client_uid: str
    client_secret: str
    conversation_uid: str
    client_label: str
    browser_confirmation: bool

    @property
    def credential_hash(self) -> str:
        return hashlib.sha256(self.client_secret.encode("utf-8")).hexdigest()

    @property
    def conversation_hash(self) -> str:
        material = f"{self.client_uid}\0{self.conversation_uid}".encode()
        return hmac.new(self.client_secret.encode("utf-8"), material, hashlib.sha256).hexdigest()


@dataclass(frozen=True, slots=True)
class ResolvedConversationIdentity:
    principal: McpClientPrincipal
    binding: AgentConversationBinding
    agent: Agent


@dataclass(frozen=True, slots=True)
class PendingIdentityTransfer:
    request: IdentityConfirmationRequest
    challenge: str


def parse_identity_metadata(meta: Any) -> ClientConversationCredentials | None:
    """Parse the namespaced request metadata without accepting tool arguments."""
    if meta is None:
        return None
    model_extra = getattr(meta, "model_extra", None)
    if not isinstance(model_extra, dict):
        return None
    raw = model_extra.get(IDENTITY_META_KEY)
    if not isinstance(raw, dict):
        return None
    if raw.get("version") != 1:
        raise ConversationIdentityError(
            "UNTRUSTED_CONVERSATION_CONTEXT",
            "Unsupported MCP conversation identity metadata version.",
        )
    client_uid = str(raw.get("client_uid") or "").strip()
    client_secret = str(raw.get("client_secret") or "").strip()
    conversation_uid = str(raw.get("conversation_uid") or "").strip()
    client_label = str(raw.get("client_label") or "MCP client").strip()[:128]
    if not _OPAQUE_ID_PATTERN.fullmatch(client_uid):
        raise ConversationIdentityError(
            "UNTRUSTED_CONVERSATION_CONTEXT",
            "MCP client identity metadata is missing a valid client identifier.",
        )
    if not _CLIENT_SECRET_PATTERN.fullmatch(client_secret):
        raise ConversationIdentityError(
            "UNTRUSTED_CONVERSATION_CONTEXT",
            "MCP client identity metadata is missing a valid client credential.",
        )
    if not _OPAQUE_ID_PATTERN.fullmatch(conversation_uid):
        raise ConversationIdentityError(
            "UNTRUSTED_CONVERSATION_CONTEXT",
            "MCP client identity metadata is missing a valid conversation identifier.",
        )
    capabilities = raw.get("capabilities")
    browser_confirmation = isinstance(capabilities, list) and "browser_confirmation" in capabilities
    return ClientConversationCredentials(
        client_uid=client_uid,
        client_secret=client_secret,
        conversation_uid=conversation_uid,
        client_label=client_label,
        browser_confirmation=browser_confirmation,
    )


async def authenticate_client_principal(
    credentials: ClientConversationCredentials,
    *,
    allow_enrollment: bool,
) -> McpClientPrincipal:
    """Authenticate a client credential, enrolling it only at an explicit creation boundary."""
    await ensure_schema()
    now = _utcnow_naive()
    async with get_session() as session:
        result = await session.execute(
            select(McpClientPrincipal).where(
                func.lower(McpClientPrincipal.client_uid) == credentials.client_uid.lower()
            )
        )
        principal = result.scalars().first()
        if principal is None:
            if not allow_enrollment:
                raise ConversationIdentityError(
                    "UNTRUSTED_CONVERSATION_CONTEXT",
                    "This MCP client has not been enrolled for persistent Agent identity.",
                )
            principal = McpClientPrincipal(
                client_uid=credentials.client_uid,
                principal_type="local_key",
                credential_hash=credentials.credential_hash,
                display_label=credentials.client_label,
                last_authenticated_at=now,
            )
            session.add(principal)
            await session.commit()
            await session.refresh(principal)
            return principal
        if principal.status != "active":
            raise ConversationIdentityError(
                "CLIENT_PRINCIPAL_REVOKED",
                "This MCP client identity has been revoked.",
                data={"client_uid": credentials.client_uid},
            )
        stored_hash = principal.credential_hash or ""
        if not stored_hash or not hmac.compare_digest(stored_hash, credentials.credential_hash):
            raise ConversationIdentityError(
                "UNTRUSTED_CONVERSATION_CONTEXT",
                "The MCP client credential does not match the enrolled identity.",
            )
        principal.last_authenticated_at = now
        if credentials.client_label and principal.display_label != credentials.client_label:
            principal.display_label = credentials.client_label
        session.add(principal)
        await session.commit()
        await session.refresh(principal)
        return principal


async def resolve_conversation_identity(
    project: Project,
    credentials: ClientConversationCredentials,
) -> ResolvedConversationIdentity | None:
    """Resolve an active binding and reject stale/revoked generations."""
    if project.id is None:
        raise ValueError("Project must have an id before resolving MCP identity.")
    principal = await authenticate_client_principal(credentials, allow_enrollment=False)
    if principal.id is None:
        raise ValueError("MCP client principal must have an id before resolving bindings.")
    async with get_session() as session:
        result = await session.execute(
            select(AgentConversationBinding).where(
                AgentConversationBinding.client_principal_id == principal.id,
                AgentConversationBinding.project_id == project.id,
                AgentConversationBinding.conversation_binding_hash == credentials.conversation_hash,
            )
        )
        binding = result.scalars().first()
        if binding is None:
            return None
        if binding.status != "active":
            raise ConversationIdentityError(
                "IDENTITY_SESSION_REVOKED",
                "This conversation no longer owns its previous Agent identity.",
                data={"project_key": project.human_key},
            )
        agent = await session.get(Agent, binding.agent_id)
        if agent is None or agent.project_id != project.id:
            raise ConversationIdentityError(
                "IDENTITY_BINDING_CONFLICT",
                "The persistent conversation binding no longer points to a valid Agent.",
            )
        if binding.generation != agent.binding_generation:
            raise ConversationIdentityError(
                "IDENTITY_SESSION_REVOKED",
                "This conversation binding was superseded by a newer Agent owner.",
                data={"project_key": project.human_key, "agent_name": agent.name},
            )
        binding.last_seen_at = _utcnow_naive()
        session.add(binding)
        await session.commit()
        await session.refresh(binding)
        return ResolvedConversationIdentity(principal=principal, binding=binding, agent=agent)


async def bind_conversation_identity(
    project: Project,
    agent: Agent,
    credentials: ClientConversationCredentials,
    *,
    allow_client_enrollment: bool,
) -> ResolvedConversationIdentity:
    """Bind an unowned Agent to the current conversation without implicit takeover."""
    if project.id is None or agent.id is None:
        raise ValueError("Project and Agent must have ids before creating an MCP identity binding.")
    if agent.project_id != project.id:
        raise ConversationIdentityError(
            "IDENTITY_BINDING_CONFLICT",
            "Cross-project Agent identity bindings are not allowed.",
        )
    principal = await authenticate_client_principal(credentials, allow_enrollment=allow_client_enrollment)
    if principal.id is None:
        raise ValueError("MCP client principal must have an id before creating bindings.")
    now = _utcnow_naive()
    async with get_session() as session:
        current_agent = await session.get(Agent, agent.id)
        if current_agent is None or current_agent.project_id != project.id:
            raise ConversationIdentityError(
                "IDENTITY_BINDING_CONFLICT",
                "The Agent no longer belongs to this mailbox.",
            )
        conversation_result = await session.execute(
            select(AgentConversationBinding).where(
                AgentConversationBinding.client_principal_id == principal.id,
                AgentConversationBinding.project_id == project.id,
                AgentConversationBinding.conversation_binding_hash == credentials.conversation_hash,
            )
        )
        conversation_binding = conversation_result.scalars().first()
        if conversation_binding is not None:
            if conversation_binding.status != "active" or conversation_binding.agent_id != current_agent.id:
                raise ConversationIdentityError(
                    "IDENTITY_BINDING_CONFLICT",
                    "This conversation is already associated with a different or revoked Agent binding.",
                )
            if conversation_binding.generation != current_agent.binding_generation:
                raise ConversationIdentityError(
                    "IDENTITY_SESSION_REVOKED",
                    "This conversation binding was superseded by a newer Agent owner.",
                )
            conversation_binding.last_seen_at = now
            session.add(conversation_binding)
            await session.commit()
            await session.refresh(conversation_binding)
            return ResolvedConversationIdentity(
                principal=principal,
                binding=conversation_binding,
                agent=current_agent,
            )
        owner_result = await session.execute(
            select(AgentConversationBinding).where(
                AgentConversationBinding.project_id == project.id,
                AgentConversationBinding.agent_id == current_agent.id,
                AgentConversationBinding.status == "active",
            )
        )
        if owner_result.scalars().first() is not None:
            raise ConversationIdentityError(
                "IDENTITY_TRANSFER_CONFIRMATION_REQUIRED",
                "This Agent is owned by another conversation; request an explicit identity transfer.",
                data={"project_key": project.human_key, "agent_name": current_agent.name},
            )
        binding = AgentConversationBinding(
            project_id=project.id,
            agent_id=current_agent.id,
            client_principal_id=principal.id,
            conversation_binding_hash=credentials.conversation_hash,
            generation=current_agent.binding_generation,
            last_seen_at=now,
        )
        session.add(binding)
        await session.commit()
        await session.refresh(binding)
        return ResolvedConversationIdentity(principal=principal, binding=binding, agent=current_agent)


async def request_identity_transfer(
    project: Project,
    agent: Agent,
    credentials: ClientConversationCredentials,
    *,
    ttl_seconds: int,
) -> PendingIdentityTransfer:
    """Create a short-lived transfer request without changing the active owner."""
    if project.id is None or agent.id is None:
        raise ValueError("Project and Agent must have ids before requesting an identity transfer.")
    if project.mailbox_state != "active":
        raise ConversationIdentityError(
            "MAILBOX_UNAVAILABLE",
            "Restore the mailbox from the recycle bin before use",
        )
    principal = await authenticate_client_principal(credentials, allow_enrollment=False)
    if principal.id is None:
        raise ValueError("MCP client principal must have an id before requesting a transfer.")
    now = _utcnow_naive()
    async with get_session() as session:
        current_agent = await session.get(Agent, agent.id)
        if current_agent is None or current_agent.project_id != project.id:
            raise ConversationIdentityError(
                "IDENTITY_BINDING_CONFLICT",
                "The requested Agent does not belong to this mailbox.",
            )
        owner_result = await session.execute(
            select(AgentConversationBinding).where(
                AgentConversationBinding.project_id == project.id,
                AgentConversationBinding.agent_id == current_agent.id,
                AgentConversationBinding.status == "active",
            )
        )
        owner = owner_result.scalars().first()
        if owner is None:
            raise ConversationIdentityError(
                "IDENTITY_BINDING_REQUIRED",
                "This Agent has no active owner; bind it directly instead of requesting a transfer.",
            )
        if owner.client_principal_id != principal.id:
            raise ConversationIdentityError(
                "IDENTITY_TRANSFER_CONFIRMATION_REQUIRED",
                "A different MCP client owns this Agent; the current owner or an identity administrator must recover it.",
            )
        if (
            owner.conversation_binding_hash == credentials.conversation_hash
            and owner.generation == current_agent.binding_generation
        ):
            raise ConversationIdentityError(
                "IDENTITY_BINDING_CONFLICT",
                "This conversation already owns the requested Agent identity.",
            )
        target_result = await session.execute(
            select(AgentConversationBinding).where(
                AgentConversationBinding.client_principal_id == principal.id,
                AgentConversationBinding.project_id == project.id,
                AgentConversationBinding.conversation_binding_hash == credentials.conversation_hash,
                AgentConversationBinding.status == "active",
            )
        )
        target = target_result.scalars().first()
        if target is not None:
            raise ConversationIdentityError(
                "IDENTITY_BINDING_CONFLICT",
                "This conversation already owns another active Agent identity in the mailbox.",
            )
        challenge = secrets.token_urlsafe(32)
        request = IdentityConfirmationRequest(
            request_uid=secrets.token_urlsafe(24),
            action="transfer",
            requesting_principal_id=principal.id,
            project_id=project.id,
            agent_id=current_agent.id,
            source_binding_id=owner.id,
            target_conversation_binding_hash=credentials.conversation_hash,
            expected_binding_generation=current_agent.binding_generation,
            challenge_hash=hashlib.sha256(challenge.encode("utf-8")).hexdigest(),
            expires_at=now + timedelta(seconds=max(60, min(ttl_seconds, 1800))),
        )
        session.add(request)
        await session.commit()
        await session.refresh(request)
        return PendingIdentityTransfer(request=request, challenge=challenge)


async def get_identity_confirmation(
    request_uid: str,
    challenge: str,
) -> tuple[IdentityConfirmationRequest, Project, Agent, McpClientPrincipal]:
    """Return a confirmation request after verifying its one-time browser challenge."""
    await ensure_schema()
    challenge_hash = hashlib.sha256(challenge.encode("utf-8")).hexdigest()
    async with get_session() as session:
        result = await session.execute(
            select(IdentityConfirmationRequest).where(
                IdentityConfirmationRequest.request_uid == request_uid
            )
        )
        request = result.scalars().first()
        if request is None or not hmac.compare_digest(request.challenge_hash, challenge_hash):
            raise ConversationIdentityError(
                "IDENTITY_CONFIRMATION_EXPIRED",
                "The identity confirmation request is invalid or no longer available.",
            )
        now = _utcnow_naive()
        if request.status != "pending" or request.expires_at <= now:
            if request.status == "pending" and request.expires_at <= now:
                request.status = "expired"
                request.decided_at = now
                session.add(request)
                await session.commit()
            raise ConversationIdentityError(
                "IDENTITY_CONFIRMATION_EXPIRED",
                "The identity confirmation request is no longer pending.",
            )
        project = await session.get(Project, request.project_id)
        agent = await session.get(Agent, request.agent_id)
        principal = await session.get(McpClientPrincipal, request.requesting_principal_id)
        if project is None or agent is None or principal is None:
            raise ConversationIdentityError(
                "IDENTITY_BINDING_CONFLICT",
                "The identity confirmation target no longer exists.",
            )
        return request, project, agent, principal


async def decide_identity_transfer(
    request_uid: str,
    challenge: str,
    *,
    approve: bool,
) -> ResolvedConversationIdentity | None:
    """Consume a browser/native confirmation and atomically transfer ownership."""
    await ensure_schema()
    challenge_hash = hashlib.sha256(challenge.encode("utf-8")).hexdigest()
    now = _utcnow_naive()
    async with get_immediate_session() as session:
        result = await session.execute(
            select(IdentityConfirmationRequest).where(
                IdentityConfirmationRequest.request_uid == request_uid
            )
        )
        request = result.scalars().first()
        if request is None or not hmac.compare_digest(request.challenge_hash, challenge_hash):
            raise ConversationIdentityError(
                "IDENTITY_CONFIRMATION_EXPIRED",
                "The identity confirmation request is invalid or no longer available.",
            )
        if request.status != "pending" or request.expires_at <= now:
            if request.status == "pending" and request.expires_at <= now:
                request.status = "expired"
                request.decided_at = now
                session.add(request)
                await session.commit()
            raise ConversationIdentityError(
                "IDENTITY_CONFIRMATION_EXPIRED",
                "The identity confirmation request is no longer pending.",
            )
        if not approve:
            request.status = "denied"
            request.decided_at = now
            request.decided_by_principal_id = request.requesting_principal_id
            session.add(request)
            await session.commit()
            return None
        project = await session.get(Project, request.project_id)
        agent = await session.get(Agent, request.agent_id)
        principal = await session.get(McpClientPrincipal, request.requesting_principal_id)
        source = await session.get(AgentConversationBinding, request.source_binding_id)
        if project is None or agent is None or principal is None or source is None:
            raise ConversationIdentityError(
                "IDENTITY_BINDING_CONFLICT",
                "The identity transfer target no longer exists.",
            )
        if project.mailbox_state != "active":
            raise ConversationIdentityError(
                "MAILBOX_UNAVAILABLE",
                "Restore the mailbox from the recycle bin before use",
            )
        if principal.status != "active":
            raise ConversationIdentityError(
                "CLIENT_PRINCIPAL_REVOKED",
                "The requesting MCP client identity has been revoked.",
            )
        if (
            source.status != "active"
            or source.agent_id != agent.id
            or source.generation != request.expected_binding_generation
            or agent.binding_generation != request.expected_binding_generation
            or source.client_principal_id != principal.id
        ):
            raise ConversationIdentityError(
                "IDENTITY_SESSION_REVOKED",
                "The Agent owner changed before this transfer was approved.",
            )
        target_result = await session.execute(
            select(AgentConversationBinding).where(
                AgentConversationBinding.client_principal_id == principal.id,
                AgentConversationBinding.project_id == project.id,
                AgentConversationBinding.conversation_binding_hash
                == request.target_conversation_binding_hash,
            )
        )
        target = target_result.scalars().first()
        if target is not None and target.status == "active":
            raise ConversationIdentityError(
                "IDENTITY_BINDING_CONFLICT",
                "The target conversation already owns an active Agent identity.",
            )
        next_generation = agent.binding_generation + 1
        source.status = "revoked"
        source.revoked_at = now
        source.revocation_reason = "transferred"
        session.add(source)
        await session.flush()
        if target is None:
            target = AgentConversationBinding(
                project_id=project.id,
                agent_id=agent.id,
                client_principal_id=principal.id,
                conversation_binding_hash=request.target_conversation_binding_hash,
                generation=next_generation,
                transferred_from_binding_id=source.id,
                last_seen_at=now,
            )
        else:
            target.agent_id = agent.id
            target.generation = next_generation
            target.status = "active"
            target.last_seen_at = now
            target.revoked_at = None
            target.revocation_reason = None
            target.transferred_from_binding_id = source.id
        agent.binding_generation = next_generation
        request.status = "consumed"
        request.decided_at = now
        request.decided_by_principal_id = principal.id
        session.add(target)
        session.add(agent)
        session.add(request)
        await session.commit()
        await session.refresh(target)
        await session.refresh(agent)
        return ResolvedConversationIdentity(principal=principal, binding=target, agent=agent)


async def release_conversation_identity(
    project: Project,
    credentials: ClientConversationCredentials,
) -> tuple[Agent, int]:
    """Release the current conversation owner while preserving the Agent and mailbox."""
    resolved = await resolve_conversation_identity(project, credentials)
    if resolved is None or resolved.binding.id is None or resolved.agent.id is None:
        raise ConversationIdentityError(
            "IDENTITY_BINDING_REQUIRED",
            "This conversation is not bound to an Agent identity in the mailbox.",
        )
    now = _utcnow_naive()
    async with get_immediate_session() as session:
        binding = await session.get(AgentConversationBinding, resolved.binding.id)
        agent = await session.get(Agent, resolved.agent.id)
        if binding is None or agent is None:
            raise ConversationIdentityError(
                "IDENTITY_BINDING_CONFLICT",
                "The Agent identity binding no longer exists.",
            )
        if binding.status != "active" or binding.generation != agent.binding_generation:
            raise ConversationIdentityError(
                "IDENTITY_SESSION_REVOKED",
                "This conversation no longer owns the Agent identity.",
            )
        binding.status = "revoked"
        binding.revoked_at = now
        binding.revocation_reason = "released"
        agent.binding_generation += 1
        session.add(binding)
        session.add(agent)
        await session.commit()
        await session.refresh(agent)
        return agent, agent.binding_generation


async def list_client_agent_bindings(
    project: Project,
    credentials: ClientConversationCredentials,
) -> tuple[McpClientPrincipal, list[tuple[AgentConversationBinding, Agent]]]:
    """List bindings owned by the authenticated client principal without exposing secrets."""
    if project.id is None:
        raise ValueError("Project must have an id before listing identity bindings.")
    principal = await authenticate_client_principal(credentials, allow_enrollment=False)
    if principal.id is None:
        raise ValueError("MCP client principal must have an id before listing bindings.")
    async with get_session() as session:
        result = await session.execute(
            select(AgentConversationBinding, Agent)
            .join(Agent, cast(Any, Agent.id == AgentConversationBinding.agent_id))
            .where(
                AgentConversationBinding.client_principal_id == principal.id,
                AgentConversationBinding.project_id == project.id,
                Agent.project_id == project.id,
            )
            .order_by(cast(Any, AgentConversationBinding.created_at).desc())
        )
        rows = [cast(tuple[AgentConversationBinding, Agent], row) for row in result.all()]
        return principal, rows


async def get_identity_confirmation_status(
    project: Project,
    request_uid: str,
    credentials: ClientConversationCredentials,
) -> IdentityConfirmationRequest:
    """Return a confirmation state only to the MCP client that requested it."""
    if project.id is None:
        raise ValueError("Project must have an id before checking identity confirmation status.")
    principal = await authenticate_client_principal(credentials, allow_enrollment=False)
    if principal.id is None:
        raise ValueError("MCP client principal must have an id before checking confirmations.")
    now = _utcnow_naive()
    async with get_session() as session:
        result = await session.execute(
            select(IdentityConfirmationRequest).where(
                IdentityConfirmationRequest.request_uid == request_uid,
                IdentityConfirmationRequest.project_id == project.id,
                IdentityConfirmationRequest.requesting_principal_id == principal.id,
            )
        )
        request = result.scalars().first()
        if request is None:
            raise ConversationIdentityError(
                "IDENTITY_CONFIRMATION_EXPIRED",
                "The identity confirmation request does not exist for this MCP client and mailbox.",
            )
        if request.status == "pending" and request.expires_at <= now:
            request.status = "expired"
            request.decided_at = now
            session.add(request)
            await session.commit()
            await session.refresh(request)
        return request
