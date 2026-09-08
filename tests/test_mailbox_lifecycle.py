import asyncio
import json
import re
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text, update
from sqlalchemy.exc import IntegrityError

from mcp_agent_mail import storage
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.config import get_settings
from mcp_agent_mail.db import ensure_schema, get_session
from mcp_agent_mail.http import build_http_app
from mcp_agent_mail.lifecycle import (
    configure_mailbox,
    expire_mailboxes,
    project_expiry,
    purge_mailbox,
    touch_project,
)
from mcp_agent_mail.models import Agent, FileReservation, Message, Project


async def make_project(slug="sample"):
    await ensure_schema()
    async with get_session() as session:
        project = Project(slug=slug, human_key=f"/software/{slug}")
        session.add(project)
        await session.commit()
        await session.refresh(project)
        return project


@pytest.mark.asyncio
async def test_expiry_restore_permanent_and_boundary(isolated_env):
    project = await make_project()
    now = datetime(2026, 1, 1)
    assert project_expiry(project) is None
    await configure_mailbox(project.id, mailbox_type="temporary", now=now)
    assert await expire_mailboxes(now=now + timedelta(days=30, microseconds=-1000)) == 0
    assert await expire_mailboxes(now=now + timedelta(days=30)) == 1
    assert await expire_mailboxes(now=now + timedelta(days=30)) == 0
    async with get_session() as session:
        trashed = await session.get(Project, project.id)
        assert trashed is not None
        assert trashed.mailbox_state == "trash"
        assert trashed.purge_after == now + timedelta(days=37)
    restored = await configure_mailbox(project.id, action="restore", now=now + timedelta(days=31))
    assert restored["mailbox_state"] == "active"
    assert restored["purge_after"] is None
    assert await expire_mailboxes(now=now + timedelta(days=60)) == 0
    permanent = await configure_mailbox(project.id, mailbox_type="permanent", now=now)
    assert permanent["expires_at"] is None
    assert await expire_mailboxes(now=now + timedelta(days=400)) == 0
    with pytest.raises(ValueError, match="Permanent"):
        await configure_mailbox(project.id, action="trash")


@pytest.mark.asyncio
async def test_real_activity_renews_and_cannot_restore_purge_claim(isolated_env):
    project = await make_project()
    now = datetime(2026, 1, 1)
    await configure_mailbox(project.id, mailbox_type="temporary", now=now)
    await touch_project(project.id, now=now + timedelta(days=29))
    assert await expire_mailboxes(now=now + timedelta(days=30)) == 0
    async with get_session() as session:
        await session.execute(update(Project).where(cast(Any, Project.id) == project.id).values(mailbox_state="purging"))
        await session.commit()
    with pytest.raises(ValueError, match="already started"):
        await configure_mailbox(project.id, action="restore")
    with pytest.raises(ValueError, match="already started"):
        await configure_mailbox(project.id, mailbox_type="permanent")


@pytest.mark.asyncio
async def test_cleanup_is_scoped_retryable_and_respects_grace(isolated_env, monkeypatch):
    project = await make_project()
    other = await make_project("important")
    settings = get_settings()
    target = Path(settings.storage.root) / "mailboxes" / project.slug
    target.mkdir(parents=True)
    (target / "attachment.txt").write_text("temporary attachment", encoding="utf-8")
    protected = Path(settings.storage.root) / "projects" / other.slug
    protected.mkdir(parents=True)
    (protected / "important.txt").write_text("keep", encoding="utf-8")
    now = datetime(2026, 1, 1)
    await configure_mailbox(project.id, mailbox_type="temporary", now=now)
    await configure_mailbox(project.id, action="trash", now=now)
    assert not await purge_mailbox(settings, project.id, now=now + timedelta(days=6))
    assert not await purge_mailbox(settings, other.id, now=now + timedelta(days=100))

    from mcp_agent_mail import lifecycle

    remove = lifecycle._remove_mailbox_directory
    def fail_remove(_):
        raise PermissionError("simulated busy attachment")
    monkeypatch.setattr(lifecycle, "_remove_mailbox_directory", fail_remove)
    with pytest.raises(PermissionError):
        await purge_mailbox(settings, project.id, now=now + timedelta(days=7))
    async with get_session() as session:
        claimed = await session.get(Project, project.id)
        assert claimed is not None and claimed.mailbox_state == "purging"
    monkeypatch.setattr(lifecycle, "_remove_mailbox_directory", remove)
    assert await purge_mailbox(settings, project.id, now=now + timedelta(days=7))
    assert not await purge_mailbox(settings, project.id, now=now + timedelta(days=8))
    assert not target.exists()
    assert (protected / "important.txt").read_text(encoding="utf-8") == "keep"


@pytest.mark.asyncio
async def test_messaging_and_whois_do_not_open_git(isolated_env, monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("Normal mailbox operations must not open Git")
    monkeypatch.setattr(storage, "_ensure_repo", forbidden)
    async with Client(build_mcp_server()) as client:
        result = await client.call_tool("ensure_project", {"human_key": "/mailbox-test"})
        pid = result.data["id"]
        await configure_mailbox(pid, mailbox_type="temporary", now=datetime(2020, 1, 1))
        await client.call_tool("register_agent", {
            "project_key": "/mailbox-test", "program": "tests", "model": "test", "name": "BlueLake",
        })
        sent = await client.call_tool("send_message", {
            "project_key": "/mailbox-test", "sender_name": "BlueLake", "to": ["BlueLake"],
            "subject": "Database message", "body_md": "Permanent history without Git",
        })
        assert sent.data
        mid = sent.data["deliveries"][0]["payload"]["id"]
        legacy = Path(get_settings().storage.root) / "projects" / "mailbox-test" / "messages" / "legacy.md"
        legacy.parent.mkdir(parents=True)
        legacy.write_text(
            f'---json\n{{"id": {mid}, "subject": "Database message"}}\n---\n\nPermanent history without Git\n',
            encoding="utf-8",
        )
        who = await client.call_tool("whois", {"project_key": "/mailbox-test", "agent_name": "BlueLake"})
        assert who.data["recent_activity"][0]["subject"] == "Database message"
        async with get_session() as session:
            project = await session.get(Project, pid)
            assert project is not None
            baseline = project.last_activity_at
            assert baseline is not None and baseline > datetime(2020, 1, 1)
        await client.call_tool("fetch_inbox", {"project_key": "/mailbox-test", "agent_name": "BlueLake"})
        async with get_session() as session:
            polled = await session.get(Project, pid)
            assert polled is not None and polled.last_activity_at == baseline
        await configure_mailbox(pid, action="trash")
        async with AsyncClient(transport=ASGITransport(app=build_http_app(get_settings())), base_url="http://test") as http:
            response = await http.post("/mail/api/delete-messages", json={"message_ids": [mid]})
            assert response.status_code == 409
        async with get_session() as session:
            with pytest.raises(IntegrityError, match=r"[Mm]ailbox is unavailable"):
                await session.execute(text(
                    "INSERT INTO messages (project_id, sender_id, subject, body_md, created_ts, importance, ack_required, attachments) "
                    "SELECT project_id, sender_id, subject, body_md, created_ts, importance, ack_required, attachments FROM messages"
                ))
            await session.rollback()
        with pytest.raises(ToolError, match="Restore the mailbox"):
            await client.call_tool("fetch_inbox", {"project_key": "/mailbox-test", "agent_name": "BlueLake"})
        async with get_session() as session:
            trashed = await session.get(Project, pid)
            assert trashed is not None and trashed.purge_after is not None
            deadline = trashed.purge_after
        assert await purge_mailbox(get_settings(), pid, now=deadline)
        assert legacy.exists()  # Legacy archives have a separate retirement policy.
        async with get_session() as session:
            assert await session.get(Project, pid) is None
    root = Path(get_settings().storage.root)
    assert not (root / ".git").exists()
    assert not list((root / "mailboxes").rglob("messages/**/*.md"))


@pytest.mark.asyncio
async def test_mailbox_http_and_only_navigation_renews(isolated_env):
    project = await make_project()
    await configure_mailbox(project.id, mailbox_type="temporary", now=datetime(2020, 1, 1))
    async with AsyncClient(transport=ASGITransport(app=build_http_app(get_settings())), base_url="http://test") as client:
        response = await client.get("/mail/mailboxes?lang=zh-CN")
        assert response.status_code == 200
        assert "长期邮箱" in response.text and "临时邮箱" in response.text and "回收站" in response.text
        assert (await client.get("/mail/api/unified-inbox")).status_code == 200
        async with get_session() as session:
            polled = await session.get(Project, project.id)
            assert polled is not None and polled.last_activity_at == datetime(2020, 1, 1)
        assert (await client.get(f"/mail/{project.slug}", headers={"sec-fetch-mode": "navigate"})).status_code == 200
        async with get_session() as session:
            visited = await session.get(Project, project.id)
            assert visited is not None and visited.last_activity_at is not None
            assert visited.last_activity_at > datetime(2020, 1, 1)
        response = await client.post(f"/mail/api/mailboxes/{project.id}", json={"action": "trash"})
        assert response.status_code == 200
        assert (await client.get(f"/mail/{project.slug}")).status_code == 410
        response = await client.post(f"/mail/api/mailboxes/{project.id}", json={"mailbox_type": "permanent"})
        assert response.json()["mailbox_state"] == "active"
        assert (await client.post(f"/mail/api/mailboxes/{project.id}", json={"retention_days": True})).status_code == 400
        assert (await client.post(f"/mail/api/mailboxes/{project.id}", json={"mailbox_state": "purging"})).status_code == 400
        assert (await client.post(f"/mail/api/mailboxes/by-slug/{project.slug}/activity")).status_code == 200
        assert (await client.post(f"/mail/api/mailboxes/by-slug/{project.slug}/activity",
                                 headers={"sec-fetch-site": "cross-site"})).status_code == 403
        activity = await client.get("/mail/activity?lang=zh-CN")
        assert activity.status_code == 200 and "消息与活动记录" in activity.text


@pytest.mark.asyncio
async def test_archive_only_data_blocks_cleanup_without_losing_restore(isolated_env):
    project = await make_project()
    settings = get_settings()
    directory = Path(settings.storage.root) / "projects" / project.slug / "messages"
    directory.mkdir(parents=True)
    message = directory / "old.md"
    message.write_text('---json\n{"id": 999, "subject": "Important"}\n---\n\narchive only\n', encoding="utf-8")
    now = datetime(2026, 1, 1)
    await configure_mailbox(project.id, mailbox_type="temporary", now=now)
    await configure_mailbox(project.id, action="trash", now=now)
    with pytest.raises(ValueError, match="differs from database"):
        await purge_mailbox(settings, project.id, now=now + timedelta(days=7))
    assert message.exists()
    restored = await configure_mailbox(project.id, action="restore", now=now)
    assert restored["mailbox_state"] == "active"


@pytest.mark.asyncio
async def test_cleanup_rejects_identifier_traversal(isolated_env, tmp_path):
    project = await make_project("../outside")
    protected = tmp_path / "source-code.txt"
    protected.write_text("software source", encoding="utf-8")
    now = datetime(2026, 1, 1)
    await configure_mailbox(project.id, mailbox_type="temporary", now=now)
    await configure_mailbox(project.id, action="trash", now=now)
    with pytest.raises(ValueError, match="Invalid mailbox storage slug"):
        await purge_mailbox(get_settings(), project.id, now=now + timedelta(days=7))
    assert protected.read_text(encoding="utf-8") == "software source"


@pytest.mark.asyncio
async def test_cleanup_never_removes_a_registered_source_directory(isolated_env):
    project = await make_project()
    settings = get_settings()
    source = Path(settings.storage.root) / "mailboxes" / project.slug / "source"
    source.mkdir(parents=True)
    file = source / "important.py"
    file.write_text("valuable_source = True\n", encoding="utf-8")
    async with get_session() as session:
        await session.execute(update(Project).where(cast(Any, Project.id) == project.id).values(human_key=str(source)))
        await session.commit()
    now = datetime(2026, 1, 1)
    await configure_mailbox(project.id, mailbox_type="temporary", now=now)
    await configure_mailbox(project.id, action="trash", now=now)
    with pytest.raises(ValueError, match="source repository"):
        await purge_mailbox(settings, project.id, now=now + timedelta(days=7))
    assert file.read_text(encoding="utf-8") == "valuable_source = True\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("reference", ["sender", "attachment"])
async def test_cross_mailbox_references_pause_cleanup_and_allow_restore(isolated_env, reference):
    temporary = await make_project("temporary")
    permanent = await make_project("permanent")
    async with get_session() as session:
        sender = Agent(project_id=temporary.id if reference == "sender" else permanent.id,
                       name="BlueLake", program="tests", model="test")
        session.add(sender)
        await session.flush()
        assert sender.id is not None
        message = Message(project_id=permanent.id, sender_id=sender.id, subject="Keep this",
                          body_md="Important permanent mailbox content", attachments=[
                              {"path": "mailboxes/temporary/attachments/important.bin"}
                          ] if reference == "attachment" else [])
        session.add(message)
        await session.commit()
        await session.refresh(message)
        mid = message.id
    settings = get_settings()
    file = Path(settings.storage.root) / "mailboxes" / "temporary" / "attachments" / "important.bin"
    file.parent.mkdir(parents=True)
    file.write_bytes(b"retained content")
    now = datetime(2026, 1, 1)
    await configure_mailbox(temporary.id, mailbox_type="temporary", now=now)
    await configure_mailbox(temporary.id, action="trash", now=now)
    with pytest.raises(ValueError, match=r"retains messages|attachments referenced"):
        await purge_mailbox(settings, temporary.id, now=now + timedelta(days=7))
    assert file.read_bytes() == b"retained content"
    async with get_session() as session:
        assert await session.get(Message, mid) is not None
    assert (await configure_mailbox(temporary.id, action="restore"))["mailbox_state"] == "active"


@pytest.mark.asyncio
async def test_startup_rebuilds_preexisting_guard_reservations(isolated_env):
    from fastmcp import Client

    from mcp_agent_mail.app import build_mcp_server

    project = await make_project()
    async with get_session() as session:
        agent = Agent(project_id=project.id, name="BlueLake", program="tests", model="test")
        session.add(agent)
        await session.flush()
        assert agent.id is not None
        session.add(FileReservation(project_id=project.id, agent_id=agent.id, path_pattern="src/**",
                                    expires_ts=datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)))
        await session.commit()
    async with Client(build_mcp_server()):
        root = Path(get_settings().storage.root)
        records = list((root / "mailboxes" / project.slug / "file_reservations").glob("*.json"))
        assert records
        assert any(json.loads(path.read_text(encoding="utf-8"))["path_pattern"] == "src/**" for path in records)
        assert not (root / ".git").exists()


@pytest.mark.asyncio
async def test_concurrent_cleanup_has_a_single_winner(isolated_env):
    project = await make_project()
    now = datetime(2026, 1, 1)
    await configure_mailbox(project.id, mailbox_type="temporary", now=now)
    await configure_mailbox(project.id, action="trash", now=now)
    results = await asyncio.gather(
        purge_mailbox(get_settings(), project.id, now=now + timedelta(days=7)),
        purge_mailbox(get_settings(), project.id, now=now + timedelta(days=7)),
    )
    assert sorted(results) == [False, True]


@pytest.mark.asyncio
async def test_restore_during_preflight_wins_over_cleanup(isolated_env, monkeypatch):
    from mcp_agent_mail import lifecycle

    project = await make_project()
    now = datetime(2026, 1, 1)
    await configure_mailbox(project.id, mailbox_type="temporary", now=now)
    await configure_mailbox(project.id, action="trash", now=now)
    ready, proceed = asyncio.Event(), asyncio.Event()
    original = lifecycle.archive_reconciliation
    async def pause(settings, mailbox):
        report = await original(settings, mailbox)
        ready.set()
        await proceed.wait()
        return report
    monkeypatch.setattr(lifecycle, "archive_reconciliation", pause)
    task = asyncio.create_task(purge_mailbox(get_settings(), project.id, now=now + timedelta(days=7)))
    try:
        await asyncio.wait_for(ready.wait(), timeout=5)
        assert (await configure_mailbox(project.id, action="restore"))["mailbox_state"] == "active"
    finally:
        proceed.set()
    assert not await task


@pytest.mark.asyncio
@pytest.mark.parametrize("locale, label, saved", [("zh-CN", "长期邮箱", "邮箱设置已保存。"),
                                                ("en", "Permanent mailboxes", "Mailbox settings saved.")])
async def test_mailbox_controls_execute_in_both_languages(isolated_env, locale, label, saved):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to execute the rendered JavaScript")
    await make_project()
    async with AsyncClient(transport=ASGITransport(app=build_http_app(get_settings())), base_url="http://test") as client:
        html = (await client.get(f"/mail/mailboxes?lang={locale}")).text
    script = re.search(r"<script>\s*(function mailboxManager\(\).*?)</script>", html, re.S)
    assert script is not None
    program = script.group(1) + "\n" + f"""
const assert = require('node:assert/strict');
(async () => {{
  const manager = mailboxManager();
  assert.equal(manager.tabs[0].label, {json.dumps(label)});
  const original = manager.items[0];
  const updated = {{...original, mailbox_type: 'temporary'}};
  global.fetch = async (url, options) => {{
    assert.equal(url, '/mail/api/mailboxes/' + original.id);
    assert.equal(options.method, 'POST');
    assert.equal(JSON.parse(options.body).mailbox_type, 'temporary');
    return {{ok: true, json: async () => updated}};
  }};
  await manager.change(original, {{mailbox_type: 'temporary'}});
  assert.equal(manager.category, 'temporary');
  assert.equal(manager.items[0].mailbox_type, 'temporary');
  assert.equal(manager.notice, {json.dumps(saved)});
  global.fetch = async () => ({{ok: false}});
  await manager.change(updated, {{action: 'trash'}});
  assert.equal(manager.busy, false);
  assert.equal(manager.category, 'temporary');
  assert.notEqual(manager.notice, {json.dumps(saved)});
}})().catch(error => {{console.error(error); process.exitCode = 1;}});
"""
    result = await asyncio.to_thread(subprocess.run, [node], input=program, text=True, encoding="utf-8",
                                     capture_output=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
