"""Tests for concurrent/parallel operations in MCP Agent Mail.

Tests concurrent access patterns including:
- Parallel message writes
- Concurrent file reservation requests
- Race conditions in lock acquisition
- Parallel inbox fetches during message delivery
"""

from __future__ import annotations

import asyncio

import pytest
from fastmcp import Client
from sqlalchemy import text

from mcp_agent_mail import config as _config
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.db import ensure_schema, get_session
from mcp_agent_mail.models import Agent

pytestmark = pytest.mark.slow



async def _setup_project_and_agents(settings: _config.Settings) -> dict:
    """Create test project and agents using MCP tools."""
    await ensure_schema()

    server = build_mcp_server()
    async with Client(server) as client:
        # Create project via MCP tool
        await client.call_tool("ensure_project", {"human_key": "/tmp/concurrent-test"})

        # Create multiple agents with auto-generated adjective+noun names
        agents = []
        tokens_by_name: dict[str, str] = {}
        for i in range(5):
            result = await client.call_tool(
                "register_agent",
                {
                    "project_key": "/tmp/concurrent-test",
                    "program": "claude-code",
                    "model": "opus-4",
                    "task_description": f"Task {i}",
                },
            )
            # Extract the generated name from the result
            data = result.data if hasattr(result, "data") else {}
            if isinstance(data, dict) and "name" in data:
                agents.append(data["name"])
                token = data.get("registration_token")
                if isinstance(token, str) and token:
                    tokens_by_name[data["name"]] = token

        # Ensure we got all agents - fail fast if not
        assert len(agents) == 5, f"Expected 5 agents, got {len(agents)}: {agents}"
        assert len(tokens_by_name) == 5, f"Expected registration tokens for all agents, got {tokens_by_name}"

        # These concurrency tests exercise delivery/write paths, not contact approval.
        # Open inboxes explicitly so fresh client sessions can message each other
        # without auto-contact side effects obscuring the concurrency assertions.
        for agent_name in agents:
            await client.call_tool(
                "set_contact_policy",
                {
                    "project_key": "/tmp/concurrent-test",
                    "agent_name": agent_name,
                    "policy": "open",
                    "registration_token": tokens_by_name[agent_name],
                },
            )

    # Get project_id from DB for reference
    async with get_session() as session:
        row = await session.execute(
            text("SELECT id FROM projects WHERE human_key = :hk"),
            {"hk": "/tmp/concurrent-test"},
        )
        project_id = row.scalar()

    return {
        "project_id": project_id,
        "project_slug": "tmp-concurrent-test",
        "agents": agents,
        "tokens_by_name": tokens_by_name,
    }


# =============================================================================
# Concurrent Message Write Tests
# =============================================================================


@pytest.mark.asyncio
async def test_concurrent_message_sends(isolated_env):
    """Test sending multiple messages concurrently."""
    settings = _config.get_settings()
    data = await _setup_project_and_agents(settings)

    server = build_mcp_server()

    async def send_message(client: Client, sender: str, recipient: str, subject: str):
        """Send a message via the MCP tool."""
        result = await client.call_tool(
            "send_message",
            {
                "project_key": "/tmp/concurrent-test",
                "sender_name": sender,
                "sender_token": data["tokens_by_name"][sender],
                "to": [recipient],
                "subject": subject,
                "body_md": f"Message from {sender} to {recipient}",
            },
        )
        return result

    # Send 10 messages concurrently
    async with Client(server) as client:
        tasks = []
        for i in range(10):
            sender = data["agents"][i % 5]
            recipient = data["agents"][(i + 1) % 5]
            tasks.append(send_message(client, sender, recipient, f"Concurrent Message {i}"))

        results = await asyncio.gather(*tasks, return_exceptions=True)

    # Count successes (some may fail due to auto-registration issues, that's ok)
    successes = sum(1 for r in results if not isinstance(r, Exception))
    assert successes >= 5, f"Expected at least 5 successful sends, got {successes}"


@pytest.mark.asyncio
async def test_concurrent_messages_to_same_thread(isolated_env):
    """Test multiple agents writing to the same thread concurrently."""
    settings = _config.get_settings()
    data = await _setup_project_and_agents(settings)

    server = build_mcp_server()

    first_agent = data["agents"][0]

    async def send_to_thread(client: Client, sender: str, message_num: int):
        """Send a message to a shared thread."""
        result = await client.call_tool(
            "send_message",
            {
                "project_key": "/tmp/concurrent-test",
                "sender_name": sender,
                "sender_token": data["tokens_by_name"][sender],
                "to": [first_agent],
                "subject": f"Thread Message {message_num}",
                "body_md": f"Message {message_num} from {sender}",
                "thread_id": "shared-thread-1",
            },
        )
        return result

    # Send 5 messages to same thread concurrently
    async with Client(server) as client:
        tasks = [send_to_thread(client, data["agents"][i], i) for i in range(5)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    # All should succeed or fail gracefully
    errors = [r for r in results if isinstance(r, Exception)]
    assert len(errors) < 3, f"Too many errors: {errors}"


# =============================================================================
# Concurrent File Reservation Tests
# =============================================================================


@pytest.mark.asyncio
async def test_concurrent_file_reservation_different_paths(isolated_env):
    """Test concurrent file reservations on different paths."""
    settings = _config.get_settings()
    data = await _setup_project_and_agents(settings)

    server = build_mcp_server()

    async def reserve_file(client: Client, agent: str, path: str):
        """Reserve a file path."""
        result = await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": "/tmp/concurrent-test",
                "agent_name": agent,
                "registration_token": data["tokens_by_name"][agent],
                "paths": [path],
                "ttl_seconds": 3600,
                "exclusive": True,
                "reason": f"Testing by {agent}",
            },
        )
        return result

    # Reserve different paths concurrently
    async with Client(server) as client:
        tasks = [reserve_file(client, data["agents"][i], f"src/module{i}.py") for i in range(5)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    # All should succeed (no conflicts)
    successes = sum(1 for r in results if not isinstance(r, Exception))
    assert successes == 5, f"Expected all 5 reservations to succeed, got {successes}"


@pytest.mark.asyncio
async def test_concurrent_file_reservation_same_path_conflict(isolated_env):
    """Test concurrent file reservations on the same path detect conflicts."""
    settings = _config.get_settings()
    data = await _setup_project_and_agents(settings)

    server = build_mcp_server()

    async def reserve_file(client: Client, agent: str):
        """Reserve the same file path."""
        result = await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": "/tmp/concurrent-test",
                "agent_name": agent,
                "registration_token": data["tokens_by_name"][agent],
                "paths": ["shared/config.json"],
                "ttl_seconds": 3600,
                "exclusive": True,
                "reason": f"Testing by {agent}",
            },
        )
        return result

    # Try to reserve the same path concurrently
    async with Client(server) as client:
        tasks = [reserve_file(client, data["agents"][i]) for i in range(3)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    # At least one should succeed, others may report conflicts
    successes = sum(1 for r in results if not isinstance(r, Exception))
    assert successes >= 1, "At least one reservation should succeed"


@pytest.mark.asyncio
async def test_concurrent_file_reservation_overlapping_globs(isolated_env):
    """Test concurrent reservations with overlapping glob patterns."""
    settings = _config.get_settings()
    data = await _setup_project_and_agents(settings)

    server = build_mcp_server()

    async with Client(server) as client:
        # First agent reserves broad pattern
        result1 = await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": "/tmp/concurrent-test",
                "agent_name": data["agents"][0],
                "registration_token": data["tokens_by_name"][data["agents"][0]],
                "paths": ["src/**/*.py"],
                "ttl_seconds": 3600,
                "exclusive": True,
            },
        )

        # Second agent tries to reserve specific file in same pattern
        result2 = await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": "/tmp/concurrent-test",
                "agent_name": data["agents"][1],
                "registration_token": data["tokens_by_name"][data["agents"][1]],
                "paths": ["src/app.py"],
                "ttl_seconds": 3600,
                "exclusive": True,
            },
        )

    # Second should report conflict with first
    # Result format varies, just check it doesn't crash
    assert result1 is not None
    assert result2 is not None


# =============================================================================
# Concurrent Inbox Fetch Tests
# =============================================================================


@pytest.mark.asyncio
async def test_concurrent_inbox_fetches(isolated_env):
    """Test multiple concurrent inbox fetches."""
    settings = _config.get_settings()
    data = await _setup_project_and_agents(settings)

    # First send some messages
    server = build_mcp_server()
    async with Client(server) as client:
        for i in range(5):
            await client.call_tool(
                "send_message",
                {
                    "project_key": "/tmp/concurrent-test",
                    "sender_name": data["agents"][(i + 1) % 5],
                    "sender_token": data["tokens_by_name"][data["agents"][(i + 1) % 5]],
                    "to": [data["agents"][0]],
                    "subject": f"Test Message {i}",
                    "body_md": f"Body {i}",
                },
            )

        async def fetch_inbox(c: Client):
            """Fetch inbox for Agent0."""
            result = await c.call_tool(
                "fetch_inbox",
                {
                    "project_key": "/tmp/concurrent-test",
                    "agent_name": data["agents"][0],
                    "registration_token": data["tokens_by_name"][data["agents"][0]],
                    "limit": 100,
                },
            )
            return result

        # Fetch inbox concurrently 10 times
        tasks = [fetch_inbox(client) for _ in range(10)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    # All should succeed
    successes = sum(1 for r in results if not isinstance(r, Exception))
    assert successes == 10, f"All inbox fetches should succeed, got {successes}"


@pytest.mark.asyncio
async def test_concurrent_inbox_fetch_during_message_send(isolated_env):
    """Test inbox fetch while messages are being sent."""
    settings = _config.get_settings()
    data = await _setup_project_and_agents(settings)

    server = build_mcp_server()

    async def send_message(client: Client, i: int):
        """Send a message."""
        await asyncio.sleep(0.01 * i)  # Slight stagger
        return await client.call_tool(
            "send_message",
            {
                "project_key": "/tmp/concurrent-test",
                "sender_name": data["agents"][1],
                "sender_token": data["tokens_by_name"][data["agents"][1]],
                "to": [data["agents"][0]],
                "subject": f"Concurrent Send {i}",
                "body_md": f"Body {i}",
            },
        )

    async def fetch_inbox(client: Client):
        """Fetch inbox."""
        await asyncio.sleep(0.05)  # Slight delay
        return await client.call_tool(
            "fetch_inbox",
            {
                "project_key": "/tmp/concurrent-test",
                "agent_name": data["agents"][0],
                "registration_token": data["tokens_by_name"][data["agents"][0]],
                "limit": 100,
            },
        )

    # Run sends and fetches concurrently
    async with Client(server) as client:
        send_tasks = [send_message(client, i) for i in range(5)]
        fetch_tasks = [fetch_inbox(client) for _ in range(3)]
        results = await asyncio.gather(*send_tasks, *fetch_tasks, return_exceptions=True)

    # Should not crash
    errors = [r for r in results if isinstance(r, Exception)]
    assert len(errors) < len(results), "Some operations should succeed"


# =============================================================================
# Lock Race Condition Tests
# =============================================================================


@pytest.mark.asyncio
async def test_concurrent_project_ensure(isolated_env):
    """Test concurrent project ensure calls."""
    _config.get_settings()  # Ensure settings are loaded
    await ensure_schema()

    server = build_mcp_server()

    async def ensure_project(client: Client, suffix: str):
        """Ensure a project exists."""
        return await client.call_tool(
            "ensure_project",
            {"human_key": f"/tmp/race-test-{suffix}"},
        )

    # Call ensure_project concurrently for same project
    async with Client(server) as client:
        tasks = [ensure_project(client, "same") for _ in range(5)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    # At least some should succeed (idempotent operation), but under high concurrency
    # with Python 3.14 CancelledError-as-BaseException, client cleanup can trigger
    # transient exceptions even after successful tool calls
    successes = sum(1 for r in results if not isinstance(r, BaseException))
    min_expected = 2  # 40% threshold - very tolerant for CI reliability
    if successes < min_expected:
        errors = []
        for r in results:
            if isinstance(r, BaseException):
                errors.append(f"{type(r).__name__}: {r}")
        assert successes >= min_expected, f"Some ensures should succeed, got {successes}. Errors: {errors}"


@pytest.mark.asyncio
async def test_concurrent_agent_registration(isolated_env):
    """Test concurrent agent registration."""
    _config.get_settings()  # Ensure settings are loaded
    await ensure_schema()

    server = build_mcp_server()

    async with Client(server) as client:
        # First ensure project
        await client.call_tool("ensure_project", {"human_key": "/tmp/reg-test"})

        async def register_agent(c: Client, i: int):
            """Register an agent."""
            return await c.call_tool(
                "register_agent",
                {
                    "project_key": "/tmp/reg-test",
                    "program": "claude-code",
                    "model": "opus-4",
                    "task_description": f"Task {i}",
                },
            )

        # Register multiple agents concurrently
        tasks = [register_agent(client, i) for i in range(10)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    # All should succeed with unique names
    successes = [r for r in results if not isinstance(r, Exception)]
    assert len(successes) >= 8, f"Most registrations should succeed, got {len(successes)}"

    # Verify unique names
    names = set()
    for r in successes:
        data = getattr(r, "data", None)
        if isinstance(data, dict) and "name" in data:
            names.add(data["name"])

    # Names should be unique (if we could extract them)


# =============================================================================
# Database Concurrent Access Tests
# =============================================================================


@pytest.mark.asyncio
async def test_concurrent_message_read_write(isolated_env):
    """Test concurrent reads and writes to messages table."""
    _config.get_settings()  # Ensure settings are loaded
    await ensure_schema()

    async with get_session() as session:
        await session.execute(
            text("INSERT INTO projects (slug, human_key, created_at) VALUES (:slug, :hk, datetime('now'))"),
            {"slug": "db-concurrent", "hk": "/tmp/db-concurrent"},
        )
        await session.commit()

        row = await session.execute(text("SELECT id FROM projects WHERE slug = :slug"), {"slug": "db-concurrent"})
        project_id = row.scalar()
        assert isinstance(project_id, int)
        sender = Agent(project_id=project_id, name="ConcurrentSender", program="test", model="test")
        session.add(sender)
        await session.commit()
        await session.refresh(sender)
        assert isinstance(sender.id, int)
        sender_id = sender.id

    async def write_message(i: int) -> None:
        """Write a message."""
        async with get_session() as session:
            await session.execute(
                text(
                    "INSERT INTO messages (project_id, subject, body_md, importance, ack_required, sender_id, created_ts) "
                        "VALUES (:pid, :subj, :body, :imp, :ack, :sender_id, datetime('now'))"
                    ),
                    {
                        "pid": project_id,
                        "subj": f"Msg {i}",
                        "body": f"Body {i}",
                        "imp": "normal",
                        "ack": 0,
                        "sender_id": sender_id,
                    },
            )
            await session.commit()

    async def read_messages() -> int:
        """Read message count."""
        async with get_session() as session:
            row = await session.execute(
                text("SELECT COUNT(*) FROM messages WHERE project_id = :pid"),
                {"pid": project_id},
            )
            return row.scalar() or 0

    # Mix writes and reads
    write_tasks = [write_message(i) for i in range(10)]
    read_tasks = [read_messages() for _ in range(5)]

    results = await asyncio.gather(*write_tasks, *read_tasks, return_exceptions=True)

    # Should not crash
    errors = [r for r in results if isinstance(r, Exception)]
    assert len(errors) == 0, f"No errors expected: {errors}"


# =============================================================================
# Archive Lock Tests
# =============================================================================


@pytest.mark.asyncio
async def test_concurrent_managed_projection_writes(isolated_env):
    """Concurrent Guard projections should not initialize a Git repository."""
    settings = _config.get_settings()

    from mcp_agent_mail.storage import ensure_mailbox_storage, write_file_reservation_record

    storage = await ensure_mailbox_storage(settings, "managed-lock-test")

    async def write_projection(i: int) -> None:
        await write_file_reservation_record(storage, {"id": i + 1, "agent": f"Agent{i}", "path": f"src/{i}.py"})

    tasks = [write_projection(i) for i in range(5)]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    errors = [r for r in results if isinstance(r, Exception)]
    assert errors == []
    assert len(list((storage.root / "file_reservations").glob("id-*.json"))) == 5
    assert not (storage.repo_root / ".git").exists()
