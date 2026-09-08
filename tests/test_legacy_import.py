"""Legacy import is explicit, transactional, repeatable and source-preserving."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlmodel import select

from mcp_agent_mail.config import get_settings
from mcp_agent_mail.db import ensure_schema, get_session
from mcp_agent_mail.legacy_import import import_legacy_archive
from mcp_agent_mail.lifecycle import hard_delete_mailbox
from mcp_agent_mail.models import Agent, LegacyImportRecord, Message, Project


async def legacy_fixture() -> Path:
    await ensure_schema()
    async with get_session() as session:
        project = Project(slug="legacy-example", human_key="/software/legacy-example")
        session.add(project)
        await session.commit()
    root = Path(get_settings().storage.root) / "projects" / "legacy-example"
    for name in ("Alice", "Bob"):
        directory = root / "agents" / name
        directory.mkdir(parents=True)
        (directory / "profile.json").write_text(json.dumps({
            "name": name, "program": "test", "model": "test",
            "inception_ts": "2025-01-01T00:00:00Z", "last_active_ts": "2025-01-02T00:00:00Z",
        }), encoding="utf-8")
    directory = root / "messages" / "2025" / "01"
    directory.mkdir(parents=True)
    path = directory / "message.md"
    path.write_text("---json\n" + json.dumps({
        "id": 123, "from": "Alice", "to": ["Bob"], "subject": "Legacy example",
        "created_ts": "2025-01-02T00:00:00Z", "attachments": [],
    }) + "\n---\nOriginal message body\n", encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_import_dry_run_apply_and_repeat_preserve_source(isolated_env):
    path = await legacy_fixture()
    original = path.read_bytes()
    settings = get_settings()
    report = await import_legacy_archive(settings)
    assert report["messages_imported"] == 1 and not report["applied"]
    async with get_session() as session:
        assert await session.get(Message, 123) is None
        assert not (await session.scalars(select(Agent))).all()
        assert await session.get(LegacyImportRecord, "legacy-example") is None
    report = await import_legacy_archive(settings, apply=True)
    assert report["messages_imported"] == 1 and report["agents_imported"] == 2
    async with get_session() as session:
        message = await session.get(Message, 123)
        assert message is not None and message.body_md == "Original message body"
    repeated = await import_legacy_archive(settings, apply=True)
    assert repeated["messages_imported"] == 0
    assert repeated["previously_imported"] == ["legacy-example"]
    assert path.read_bytes() == original
    assert not (Path(settings.storage.root) / ".git").exists()


@pytest.mark.asyncio
async def test_import_ledger_survives_purge_and_prevents_resurrection(isolated_env):
    path = await legacy_fixture()
    original = path.read_bytes()
    settings = get_settings()
    await import_legacy_archive(settings, apply=True)
    async with get_session() as session:
        project = (await session.scalars(select(Project))).one()
        assert project.id is not None
        project_id = project.id
    await hard_delete_mailbox(settings, project_id)
    async with get_session() as session:
        assert await session.get(Project, project_id) is None
        assert await session.get(Message, 123) is None
        assert await session.get(LegacyImportRecord, "legacy-example") is not None
    repeated = await import_legacy_archive(settings, apply=True)
    assert repeated["messages_imported"] == 0
    assert repeated["previously_imported"] == ["legacy-example"]
    assert path.read_bytes() == original


@pytest.mark.asyncio
async def test_changed_completed_archive_is_not_silently_reimported(isolated_env):
    path = await legacy_fixture()
    settings = get_settings()
    await import_legacy_archive(settings, apply=True)
    path.write_text(path.read_text(encoding="utf-8").replace("Original message body", "Changed source body"), encoding="utf-8")
    with pytest.raises(ValueError, match="changed after completed import"):
        await import_legacy_archive(settings, apply=True)
    async with get_session() as session:
        message = await session.get(Message, 123)
        assert message is not None and message.body_md == "Original message body"
        project_id = message.project_id
    with pytest.raises(ValueError, match="Legacy archive differs"):
        await hard_delete_mailbox(settings, project_id)
    async with get_session() as session:
        pending = await session.get(Project, project_id)
        assert pending is not None and pending.mailbox_state == "trash" and pending.cleanup_error


@pytest.mark.asyncio
async def test_unreconciled_inbox_copy_blocks_import(isolated_env):
    await legacy_fixture()
    path = Path(get_settings().storage.root) / "projects" / "legacy-example" / "agents" / "Alice" / "inbox" / "orphan.md"
    path.parent.mkdir(parents=True)
    path.write_text("unreconciled older copy", encoding="utf-8")
    with pytest.raises(ValueError, match="Unreconciled legacy agent message copy"):
        await import_legacy_archive(get_settings(), apply=True)
    async with get_session() as session:
        assert await session.get(Message, 123) is None
        assert await session.get(LegacyImportRecord, "legacy-example") is None


@pytest.mark.asyncio
async def test_source_addition_during_import_rolls_back(isolated_env, monkeypatch):
    from mcp_agent_mail import legacy_import

    path = await legacy_fixture()
    verify = legacy_import._verify_source

    def changed_source(root, fingerprints):
        (path.parent / "added.txt").write_text("late arrival", encoding="utf-8")
        verify(root, fingerprints)

    monkeypatch.setattr(legacy_import, "_verify_source", changed_source)
    with pytest.raises(ValueError, match="source changed during import"):
        await import_legacy_archive(get_settings(), apply=True)
    async with get_session() as session:
        assert await session.get(Message, 123) is None
        assert not (await session.scalars(select(Agent))).all()
        assert await session.get(LegacyImportRecord, "legacy-example") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("completed", [False, True])
async def test_existing_database_message_is_not_overwritten(isolated_env, completed):
    path = await legacy_fixture()
    settings = get_settings()
    await import_legacy_archive(settings, apply=True)
    async with get_session() as session:
        message = await session.get(Message, 123)
        assert message is not None
        message.body_md = "Newer database message"
        if not completed:
            # Model an existing database whose legacy import has not been completed.
            checkpoint = await session.get(LegacyImportRecord, "legacy-example")
            assert checkpoint is not None
            await session.delete(checkpoint)
        await session.commit()
    if completed:
        report = await import_legacy_archive(settings, apply=True)
        assert report["previously_imported"] == ["legacy-example"]
    else:
        with pytest.raises(ValueError, match="differs from database"):
            await import_legacy_archive(settings, apply=True)
    async with get_session() as session:
        message = await session.get(Message, 123)
        assert message is not None and message.body_md == "Newer database message"
    assert "Original message body" in path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_unknown_recipient_rolls_back_new_identities(isolated_env):
    path = await legacy_fixture()
    path.write_text(path.read_text(encoding="utf-8").replace('"Bob"', '"Unknown"'), encoding="utf-8")
    with pytest.raises(ValueError, match="Unknown legacy recipient"):
        await import_legacy_archive(get_settings(), apply=True)
    async with get_session() as session:
        assert await session.get(Message, 123) is None
        assert not (await session.scalars(select(Agent))).all()
