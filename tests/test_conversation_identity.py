"""Persistent MCP client and per-conversation Agent identity coverage."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastmcp import Client
from httpx import ASGITransport, AsyncClient
from mcp.types import RequestParams
from sqlmodel import select

from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.config import get_settings
from mcp_agent_mail.db import ensure_schema, get_session
from mcp_agent_mail.http import build_http_app
from mcp_agent_mail.identity import (
    IDENTITY_META_KEY,
    ClientConversationCredentials,
    ConversationIdentityError,
    authenticate_client_principal,
    bind_conversation_identity,
    decide_identity_transfer,
    get_identity_confirmation,
    get_identity_confirmation_status,
    list_client_agent_bindings,
    parse_identity_metadata,
    release_conversation_identity,
    request_identity_transfer,
    resolve_conversation_identity,
)
from mcp_agent_mail.lifecycle import purge_mailbox
from mcp_agent_mail.models import (
    Agent,
    AgentConversationBinding,
    IdentityConfirmationRequest,
    McpClientPrincipal,
    Project,
)


def credentials(*, conversation_uid: str = "conversation-00000001") -> ClientConversationCredentials:
    return ClientConversationCredentials(
        client_uid="client-installation-00000001",
        client_secret="A" * 43,
        conversation_uid=conversation_uid,
        client_label="Pi test client",
        browser_confirmation=True,
    )


def identity_meta(*, conversation_uid: str = "conversation-00000001") -> dict[str, object]:
    return {
        IDENTITY_META_KEY: {
            "version": 1,
            "client_uid": "client-installation-00000001",
            "client_secret": "A" * 43,
            "conversation_uid": conversation_uid,
            "client_label": "Pi test client",
            "capabilities": ["browser_confirmation"],
        }
    }


def structured_result(raw: object) -> dict[str, object]:
    structured = getattr(raw, "structuredContent", None)
    assert isinstance(structured, dict)
    nested = structured.get("result")
    return nested if isinstance(nested, dict) else structured


async def create_project_and_agent(*, mailbox_type: str = "permanent") -> tuple[Project, Agent]:
    await ensure_schema()
    async with get_session() as session:
        project = Project(
            slug=f"identity-{mailbox_type}",
            human_key=f"/identity/{mailbox_type}",
            mailbox_type=mailbox_type,
        )
        session.add(project)
        await session.commit()
        await session.refresh(project)
        assert project.id is not None
        agent = Agent(project_id=project.id, name="CoralBeacon", program="test", model="test")
        session.add(agent)
        await session.commit()
        await session.refresh(agent)
        return project, agent


def test_parse_identity_metadata_uses_namespaced_request_meta():
    meta = RequestParams.Meta.model_validate(
        {
            IDENTITY_META_KEY: {
                "version": 1,
                "client_uid": "client-installation-00000001",
                "client_secret": "A" * 43,
                "conversation_uid": "conversation-00000001",
                "client_label": "Pi",
                "capabilities": ["browser_confirmation"],
            }
        }
    )

    parsed = parse_identity_metadata(meta)

    assert parsed is not None
    assert parsed.client_uid == "client-installation-00000001"
    assert parsed.conversation_uid == "conversation-00000001"
    assert parsed.browser_confirmation is True
    assert parsed.credential_hash != parsed.client_secret
    assert parsed.conversation_hash != parsed.conversation_uid


def test_parse_identity_metadata_rejects_model_controlled_or_invalid_data():
    assert parse_identity_metadata(None) is None
    assert parse_identity_metadata(RequestParams.Meta()) is None
    invalid = RequestParams.Meta.model_validate(
        {
            IDENTITY_META_KEY: {
                "version": 1,
                "client_uid": "too-short",
                "client_secret": "not-secret",
                "conversation_uid": "also-short",
            }
        }
    )
    with pytest.raises(ConversationIdentityError, match="valid client identifier"):
        parse_identity_metadata(invalid)


@pytest.mark.asyncio
async def test_client_principal_enrollment_hashes_credentials(isolated_env):
    identity = credentials()

    principal = await authenticate_client_principal(identity, allow_enrollment=True)

    assert principal.id is not None
    assert principal.credential_hash == identity.credential_hash
    assert principal.credential_hash != identity.client_secret
    async with get_session() as session:
        stored = await session.get(McpClientPrincipal, principal.id)
        assert stored is not None
        assert stored.credential_hash == identity.credential_hash
        assert identity.client_secret not in repr(stored)


@pytest.mark.asyncio
async def test_client_principal_rejects_wrong_secret_and_unenrolled_client(isolated_env):
    await authenticate_client_principal(credentials(), allow_enrollment=True)
    wrong = ClientConversationCredentials(
        client_uid="client-installation-00000001",
        client_secret="B" * 43,
        conversation_uid="conversation-00000001",
        client_label="Pi test client",
        browser_confirmation=False,
    )
    with pytest.raises(ConversationIdentityError, match="does not match"):
        await authenticate_client_principal(wrong, allow_enrollment=False)
    with pytest.raises(ConversationIdentityError, match="has not been enrolled"):
        await authenticate_client_principal(
            credentials(conversation_uid="another-conversation-0001").__class__(
                client_uid="another-client-0000000001",
                client_secret="C" * 43,
                conversation_uid="another-conversation-0001",
                client_label="Other",
                browser_confirmation=False,
            ),
            allow_enrollment=False,
        )


@pytest.mark.asyncio
async def test_bind_and_reconnect_preserves_agent_without_touching_mailbox_activity(isolated_env):
    project, agent = await create_project_and_agent()
    identity = credentials()

    first = await bind_conversation_identity(
        project,
        agent,
        identity,
        allow_client_enrollment=True,
    )
    second = await resolve_conversation_identity(project, identity)

    assert second is not None
    assert first.agent.id == second.agent.id == agent.id
    assert first.binding.id == second.binding.id
    assert first.binding.generation == second.binding.generation == 1
    async with get_session() as session:
        stored_project = await session.get(Project, project.id)
        assert stored_project is not None
        assert stored_project.last_activity_at is None


@pytest.mark.asyncio
async def test_different_conversation_requires_explicit_transfer(isolated_env):
    project, agent = await create_project_and_agent()
    await bind_conversation_identity(project, agent, credentials(), allow_client_enrollment=True)

    with pytest.raises(ConversationIdentityError) as raised:
        await bind_conversation_identity(
            project,
            agent,
            credentials(conversation_uid="conversation-00000002"),
            allow_client_enrollment=False,
        )

    assert raised.value.error_type == "IDENTITY_TRANSFER_CONFIRMATION_REQUIRED"
    async with get_session() as session:
        bindings = (await session.execute(select(AgentConversationBinding))).scalars().all()
        assert len(bindings) == 1
        assert bindings[0].status == "active"


@pytest.mark.asyncio
async def test_revoked_or_stale_generation_cannot_reconnect(isolated_env):
    project, agent = await create_project_and_agent()
    resolved = await bind_conversation_identity(project, agent, credentials(), allow_client_enrollment=True)
    async with get_session() as session:
        stored_agent = await session.get(Agent, agent.id)
        assert stored_agent is not None
        stored_agent.binding_generation += 1
        session.add(stored_agent)
        await session.commit()

    with pytest.raises(ConversationIdentityError) as raised:
        await resolve_conversation_identity(project, credentials())

    assert raised.value.error_type == "IDENTITY_SESSION_REVOKED"
    assert resolved.binding.generation == 1


@pytest.mark.asyncio
async def test_register_agent_uses_persistent_request_metadata_across_mcp_sessions(isolated_env):
    server = build_mcp_server()
    arguments = {
        "project_key": "/identity/integration",
        "program": "pi",
        "model": "test",
        "name": "CoralBeacon",
    }
    async with Client(server) as first_client:
        await first_client.call_tool("ensure_project", {"human_key": "/identity/integration"})
        first_raw = await first_client.session.call_tool(
            "register_agent",
            arguments,
            meta=identity_meta(),
        )
        first = structured_result(first_raw)

    assert first["name"] == "CoralBeacon"
    assert first["credential_managed_by_mcp"] is True
    assert "registration_token" not in first
    async with get_session() as session:
        stored_agent = (
            await session.execute(select(Agent).where(Agent.name == "CoralBeacon"))
        ).scalars().one()
        assert stored_agent.registration_token is None

    async with Client(server) as second_client:
        second_raw = await second_client.session.call_tool(
            "register_agent",
            arguments,
            meta=identity_meta(),
        )
        second = structured_result(second_raw)
        status_raw = await second_client.session.call_tool(
            "identity_status",
            {"project_key": "/identity/integration"},
            meta=identity_meta(),
        )
        status = structured_result(status_raw)

    assert second["id"] == first["id"]
    assert "registration_token" not in second
    assert status["bound"] is True
    assert status["binding_state"] == "active"
    assert isinstance(status["agent"], dict)
    assert status["agent"]["name"] == "CoralBeacon"


@pytest.mark.asyncio
async def test_approved_transfer_revokes_old_conversation_and_increments_generation(isolated_env):
    project, agent = await create_project_and_agent()
    old_credentials = credentials()
    new_credentials = credentials(conversation_uid="conversation-00000002")
    old = await bind_conversation_identity(
        project,
        agent,
        old_credentials,
        allow_client_enrollment=True,
    )
    pending = await request_identity_transfer(project, agent, new_credentials, ttl_seconds=300)

    confirmation, confirmed_project, confirmed_agent, principal = await get_identity_confirmation(
        pending.request.request_uid,
        pending.challenge,
    )
    assert confirmation.status == "pending"
    assert confirmed_project.id == project.id
    assert confirmed_agent.id == agent.id
    assert principal.id == old.principal.id

    transferred = await decide_identity_transfer(
        pending.request.request_uid,
        pending.challenge,
        approve=True,
    )

    assert transferred is not None
    assert transferred.agent.id == agent.id
    assert transferred.agent.binding_generation == 2
    assert transferred.binding.generation == 2
    with pytest.raises(ConversationIdentityError) as old_error:
        await resolve_conversation_identity(project, old_credentials)
    assert old_error.value.error_type == "IDENTITY_SESSION_REVOKED"
    current = await resolve_conversation_identity(project, new_credentials)
    assert current is not None
    assert current.binding.id == transferred.binding.id


@pytest.mark.asyncio
async def test_denied_transfer_keeps_old_owner(isolated_env):
    project, agent = await create_project_and_agent()
    old_credentials = credentials()
    await bind_conversation_identity(project, agent, old_credentials, allow_client_enrollment=True)
    pending = await request_identity_transfer(
        project,
        agent,
        credentials(conversation_uid="conversation-00000002"),
        ttl_seconds=300,
    )

    result = await decide_identity_transfer(
        pending.request.request_uid,
        pending.challenge,
        approve=False,
    )

    assert result is None
    current = await resolve_conversation_identity(project, old_credentials)
    assert current is not None
    assert current.agent.binding_generation == 1


@pytest.mark.asyncio
async def test_browser_confirmation_requires_same_origin_and_one_time_challenge(isolated_env):
    project, agent = await create_project_and_agent()
    await bind_conversation_identity(project, agent, credentials(), allow_client_enrollment=True)
    pending = await request_identity_transfer(
        project,
        agent,
        credentials(conversation_uid="conversation-00000002"),
        ttl_seconds=300,
    )
    app = build_http_app(get_settings())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        page = await client.get(f"/identity/confirm/{pending.request.request_uid}")
        missing_origin = await client.post(
            f"/api/identity/confirm/{pending.request.request_uid}/details",
            json={"challenge": pending.challenge},
        )
        wrong_challenge = await client.post(
            f"/api/identity/confirm/{pending.request.request_uid}/details",
            headers={"Origin": "http://test"},
            json={"challenge": "Z" * 43},
        )
        details = await client.post(
            f"/api/identity/confirm/{pending.request.request_uid}/details",
            headers={"Origin": "http://test"},
            json={"challenge": pending.challenge},
        )
        approved = await client.post(
            f"/api/identity/confirm/{pending.request.request_uid}/approve",
            headers={"Origin": "http://test"},
            json={"challenge": pending.challenge},
        )
        replay = await client.post(
            f"/api/identity/confirm/{pending.request.request_uid}/approve",
            headers={"Origin": "http://test"},
            json={"challenge": pending.challenge},
        )

    assert page.status_code == 200
    assert "Cache-Control" in page.headers
    assert "Content-Security-Policy" in page.headers
    assert pending.challenge not in page.text
    assert missing_origin.status_code == 403
    assert wrong_challenge.status_code == 404
    assert details.status_code == 200
    assert details.json()["agent_name"] == "CoralBeacon"
    assert approved.status_code == 200
    assert approved.json()["binding_generation"] == 2
    assert replay.status_code == 404


@pytest.mark.asyncio
async def test_release_preserves_agent_and_revokes_conversation(isolated_env):
    project, agent = await create_project_and_agent()
    identity = credentials()
    await bind_conversation_identity(project, agent, identity, allow_client_enrollment=True)

    released_agent, next_generation = await release_conversation_identity(project, identity)

    assert released_agent.id == agent.id
    assert next_generation == 2
    with pytest.raises(ConversationIdentityError) as raised:
        await resolve_conversation_identity(project, identity)
    assert raised.value.error_type == "IDENTITY_SESSION_REVOKED"
    principal, bindings = await list_client_agent_bindings(project, identity)
    assert principal.status == "active"
    assert len(bindings) == 1
    assert bindings[0][0].status == "revoked"
    async with get_session() as session:
        assert await session.get(Agent, agent.id) is not None


@pytest.mark.asyncio
async def test_confirmation_status_is_scoped_to_requesting_client(isolated_env):
    project, agent = await create_project_and_agent()
    await bind_conversation_identity(project, agent, credentials(), allow_client_enrollment=True)
    pending = await request_identity_transfer(
        project,
        agent,
        credentials(conversation_uid="conversation-00000002"),
        ttl_seconds=300,
    )

    status = await get_identity_confirmation_status(
        project,
        pending.request.request_uid,
        credentials(conversation_uid="conversation-00000002"),
    )

    assert status.status == "pending"
    other_client = ClientConversationCredentials(
        client_uid="different-client-000000001",
        client_secret="D" * 43,
        conversation_uid="different-conversation-0001",
        client_label="Other",
        browser_confirmation=False,
    )
    await authenticate_client_principal(other_client, allow_enrollment=True)
    with pytest.raises(ConversationIdentityError):
        await get_identity_confirmation_status(project, pending.request.request_uid, other_client)


@pytest.mark.asyncio
async def test_temporary_trash_suspends_status_and_purge_removes_identity_records(isolated_env):
    project, agent = await create_project_and_agent(mailbox_type="temporary")
    await bind_conversation_identity(project, agent, credentials(), allow_client_enrollment=True)
    await request_identity_transfer(
        project,
        agent,
        credentials(conversation_uid="conversation-00000002"),
        ttl_seconds=300,
    )
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with get_session() as session:
        stored_project = await session.get(Project, project.id)
        assert stored_project is not None
        stored_project.mailbox_state = "trash"
        stored_project.trashed_at = now - timedelta(days=8)
        stored_project.purge_after = now - timedelta(days=1)
        session.add(stored_project)
        await session.commit()

    server = build_mcp_server()
    async with Client(server) as client:
        status_raw = await client.session.call_tool(
            "identity_status",
            {"project_key": project.human_key},
            meta=identity_meta(),
        )
        status = structured_result(status_raw)
    assert status["binding_state"] == "suspended"

    assert project.id is not None
    removed = await purge_mailbox(get_settings(), project.id, now=now, force=True)

    assert removed is True
    async with get_session() as session:
        assert (await session.execute(select(AgentConversationBinding))).scalars().all() == []
        assert (await session.execute(select(IdentityConfirmationRequest))).scalars().all() == []
        principals = (await session.execute(select(McpClientPrincipal))).scalars().all()
        assert len(principals) == 1
        assert principals[0].status == "active"
