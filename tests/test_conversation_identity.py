"""Persistent MCP client and per-conversation Agent identity coverage."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastmcp import Client
from httpx import ASGITransport, AsyncClient
from mcp.types import RequestParams
from sqlmodel import select

from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.config import clear_settings_cache, get_settings
from mcp_agent_mail.db import (
    IdentityWriteFence,
    IdentityWriteFenceError,
    add_identity_write_fence,
    begin_identity_write_fence_scope,
    end_identity_write_fence_scope,
    ensure_schema,
    get_session,
)
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
    request_identity_recovery,
    request_identity_transfer,
    resolve_conversation_identity,
)
from mcp_agent_mail.lifecycle import configure_mailbox, purge_mailbox
from mcp_agent_mail.models import (
    Agent,
    AgentConversationBinding,
    IdentityConfirmationRequest,
    MailboxEvent,
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


@pytest.mark.parametrize(
    "base_url",
    [
        "http://confirmation.example/mcp",
        "https://user:password@confirmation.example/mcp",
        "https://confirmation.example/mcp?challenge=wrong-place",
    ],
)
def test_identity_confirmation_base_url_rejects_unsafe_values(isolated_env, monkeypatch, base_url):
    monkeypatch.setenv("IDENTITY_CONFIRMATION_BASE_URL", base_url)
    clear_settings_cache()

    with pytest.raises(ValueError, match="Invalid IDENTITY_CONFIRMATION_BASE_URL"):
        get_settings()


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
async def test_existing_plaintext_token_is_hashed_after_trusted_binding_enrollment(isolated_env):
    server = build_mcp_server()
    arguments = {
        "project_key": "/identity/token-migration",
        "program": "pi",
        "model": "test",
        "name": "CoralBeacon",
    }
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/identity/token-migration"})
        legacy = await client.call_tool("register_agent", arguments)
        legacy_token = legacy.data["registration_token"]

    async with Client(server) as enrolling_client:
        migrated_raw = await enrolling_client.session.call_tool(
            "register_agent",
            {**arguments, "registration_token": legacy_token},
            meta=identity_meta(),
        )
        migrated = structured_result(migrated_raw)

    assert "registration_token" not in migrated
    async with get_session() as session:
        stored_agent = (
            await session.execute(select(Agent).where(Agent.name == "CoralBeacon"))
        ).scalars().one()
        assert stored_agent.registration_token is None
        assert stored_agent.service_credential_hash is not None
        assert stored_agent.service_credential_hash != legacy_token
        assert stored_agent.service_credential_version == 1


@pytest.mark.asyncio
async def test_ensure_agent_identity_creates_then_reconnects_without_model_token(isolated_env):
    server = build_mcp_server()
    arguments = {
        "project_key": "/identity/ensure",
        "program": "pi",
        "model": "test",
        "name_hint": "CoralBeacon",
    }
    async with Client(server) as client:
        await client.call_tool("ensure_project", {"human_key": "/identity/ensure"})
        created_raw = await client.session.call_tool(
            "ensure_agent_identity",
            arguments,
            meta=identity_meta(),
        )
        created = structured_result(created_raw)

    async with Client(server) as reconnected_client:
        reconnected_raw = await reconnected_client.session.call_tool(
            "ensure_agent_identity",
            arguments,
            meta=identity_meta(),
        )
        reconnected = structured_result(reconnected_raw)

    assert created["identity_action"] == "created"
    assert reconnected["identity_action"] == "reconnected"
    assert created["id"] == reconnected["id"]
    assert "registration_token" not in created
    assert "registration_token" not in reconnected


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
    async with get_session() as session:
        events = (
            await session.execute(select(MailboxEvent).where(MailboxEvent.project_id == project.id))
        ).scalars().all()
    assert {event.event_type for event in events} >= {
        "identity_bound",
        "identity_confirmation_requested",
        "identity_transferred",
    }
    audit_text = "\n".join(event.detail for event in events)
    assert pending.challenge not in audit_text
    assert old_credentials.client_secret not in audit_text
    assert new_credentials.conversation_uid not in audit_text


@pytest.mark.asyncio
async def test_native_mcp_elicitation_approves_transfer_without_browser_action(isolated_env):
    project, agent = await create_project_and_agent()
    await bind_conversation_identity(project, agent, credentials(), allow_client_enrollment=True)
    prompts: list[str] = []

    async def approve(message, response_type, params, context):
        prompts.append(message)
        assert response_type is not None
        assert "Approve transfer" in str(params.requestedSchema)
        assert "Deny" in str(params.requestedSchema)
        assert context is not None
        return {"value": "Approve transfer"}

    server = build_mcp_server()
    async with Client(server, elicitation_handler=approve) as client:
        result_raw = await client.session.call_tool(
            "request_agent_identity_transfer",
            {"project_key": project.human_key, "agent_name": agent.name},
            meta=identity_meta(conversation_uid="conversation-00000002"),
        )
        result = structured_result(result_raw)

    assert prompts
    assert result["status"] == "transferred"
    assert result["binding_generation"] == 2
    assert "_client_action" not in result


@pytest.mark.asyncio
async def test_streamable_http_preserves_trusted_conversation_metadata(isolated_env):
    project, agent = await create_project_and_agent()
    await bind_conversation_identity(project, agent, credentials(), allow_client_enrollment=True)
    app = build_http_app(get_settings())
    request = {
        "jsonrpc": "2.0",
        "id": "identity-http-1",
        "method": "tools/call",
        "params": {
            "name": "identity_status",
            "arguments": {"project_key": project.human_key},
            "_meta": identity_meta(),
        },
    }

    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as client,
    ):
        response = await client.post(
            get_settings().http.path,
            headers={"Accept": "application/json, text/event-stream"},
            json=request,
        )

    assert response.status_code == 200
    payload = response.json()
    result = payload["result"]["structuredContent"]
    result = result.get("result", result)
    assert result["bound"] is True
    assert result["binding_state"] == "active"
    assert result["agent"]["id"] == agent.id


@pytest.mark.asyncio
async def test_release_tool_can_atomically_revoke_its_current_binding(isolated_env):
    project, agent = await create_project_and_agent()
    identity = credentials()
    await bind_conversation_identity(project, agent, identity, allow_client_enrollment=True)
    server = build_mcp_server()

    async with Client(server) as client:
        released_raw = await client.session.call_tool(
            "release_agent_identity",
            {"project_key": project.human_key},
            meta=identity_meta(),
        )
        released = structured_result(released_raw)

    assert released["released"] is True
    assert released["binding_generation"] == 2
    assert await resolve_conversation_identity(project, identity) is None

    async with Client(server) as client:
        rebound_raw = await client.session.call_tool(
            "ensure_agent_identity",
            {
                "project_key": project.human_key,
                "program": "pi",
                "model": "test",
                "name_hint": "SilverHarbor",
            },
            meta=identity_meta(),
        )
        rebound = structured_result(rebound_raw)

    assert rebound["id"] != agent.id
    assert rebound["name"] == "SilverHarbor"
    assert rebound["credential_managed_by_mcp"] is True
    assert "registration_token" not in rebound


@pytest.mark.asyncio
async def test_database_commit_fence_rejects_write_after_concurrent_transfer(isolated_env):
    project, agent = await create_project_and_agent()
    assert project.id is not None
    assert agent.id is not None
    old_credentials = credentials()
    initial = await bind_conversation_identity(project, agent, old_credentials, allow_client_enrollment=True)
    assert initial.principal.id is not None
    new_credentials = credentials(conversation_uid="conversation-beta-123456")
    outer_token = begin_identity_write_fence_scope()
    try:
        add_identity_write_fence(
            IdentityWriteFence(
                principal_id=initial.principal.id,
                project_id=project.id,
                agent_id=agent.id,
                conversation_binding_hash=old_credentials.conversation_hash,
                generation=initial.binding.generation,
            )
        )
        async with get_session() as session:
            stale_agent = await session.get(Agent, agent.id)
            assert stale_agent is not None
            stale_agent.task_description = "must roll back"
            session.add(stale_agent)

            transfer_scope = begin_identity_write_fence_scope()
            try:
                pending = await request_identity_transfer(project, agent, new_credentials, ttl_seconds=300)
                await decide_identity_transfer(pending.request.request_uid, pending.challenge, approve=True)
            finally:
                end_identity_write_fence_scope(transfer_scope)

            with pytest.raises(IdentityWriteFenceError):
                await session.commit()
    finally:
        end_identity_write_fence_scope(outer_token)

    async with get_session() as session:
        stored_agent = await session.get(Agent, agent.id)
        assert stored_agent is not None
        assert stored_agent.task_description == ""


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
async def test_release_preserves_agent_and_leaves_conversation_unbound(isolated_env):
    project, agent = await create_project_and_agent()
    identity = credentials()
    await bind_conversation_identity(project, agent, identity, allow_client_enrollment=True)

    released_agent, next_generation = await release_conversation_identity(project, identity)

    assert released_agent.id == agent.id
    assert next_generation == 2
    assert await resolve_conversation_identity(project, identity) is None
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
    await configure_mailbox(project.id, action="restore", now=now)
    async with Client(server) as client:
        restored_raw = await client.session.call_tool(
            "identity_status",
            {"project_key": project.human_key},
            meta=identity_meta(),
        )
        restored = structured_result(restored_raw)
    assert restored["binding_state"] == "active"

    async with get_session() as session:
        stored_project = await session.get(Project, project.id)
        assert stored_project is not None
        stored_project.mailbox_state = "trash"
        stored_project.trashed_at = now - timedelta(days=8)
        stored_project.purge_after = now - timedelta(days=1)
        session.add(stored_project)
        await session.commit()

    removed = await purge_mailbox(get_settings(), project.id, now=now, force=True)

    assert removed is True
    async with get_session() as session:
        assert (await session.execute(select(AgentConversationBinding))).scalars().all() == []
        assert (await session.execute(select(IdentityConfirmationRequest))).scalars().all() == []
        principals = (await session.execute(select(McpClientPrincipal))).scalars().all()
        assert len(principals) == 1
        assert principals[0].status == "active"


@pytest.mark.asyncio
async def test_identity_admin_can_recover_agent_from_another_client(isolated_env, monkeypatch):
    project, agent = await create_project_and_agent()
    old_credentials = credentials()
    await bind_conversation_identity(project, agent, old_credentials, allow_client_enrollment=True)
    async with get_session() as session:
        stored_agent = await session.get(Agent, agent.id)
        assert stored_agent is not None
        stored_agent.service_credential_hash = "f" * 64
        stored_agent.service_credential_version = 1
        session.add(stored_agent)
        await session.commit()
    admin_credentials = ClientConversationCredentials(
        client_uid="administrator-client-000001",
        client_secret="E" * 43,
        conversation_uid="administrator-conversation-01",
        client_label="Identity administrator",
        browser_confirmation=True,
    )
    monkeypatch.setenv(
        "IDENTITY_ADMIN_PRINCIPAL_CREDENTIAL_HASHES",
        f"{admin_credentials.client_uid}={admin_credentials.credential_hash}",
    )
    clear_settings_cache()
    pending = await request_identity_recovery(
        project,
        agent,
        admin_credentials,
        ttl_seconds=300,
    )
    app = build_http_app(get_settings())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        page = await client.get(f"/identity/confirm/{pending.request.request_uid}")
        details = await client.post(
            f"/api/identity/confirm/{pending.request.request_uid}/details",
            headers={"Origin": "http://test"},
            json={"challenge": pending.challenge},
        )

    assert page.status_code == 200
    assert "recoveryIntro" in page.text
    assert details.status_code == 200
    assert details.json()["action"] == "recover"

    recovered = await decide_identity_transfer(
        pending.request.request_uid,
        pending.challenge,
        approve=True,
    )

    assert recovered is not None
    assert recovered.principal.client_uid == admin_credentials.client_uid
    assert recovered.agent.id == agent.id
    assert recovered.agent.binding_generation == 2
    assert recovered.agent.service_credential_hash is None
    assert recovered.agent.service_credential_version == 2
    with pytest.raises(ConversationIdentityError) as old_error:
        await resolve_conversation_identity(project, old_credentials)
    assert old_error.value.error_type == "IDENTITY_SESSION_REVOKED"
    current = await resolve_conversation_identity(project, admin_credentials)
    assert current is not None
    assert current.agent.id == agent.id
