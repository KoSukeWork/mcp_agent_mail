"""Edge coverage for database-owned managed mailbox storage."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import pytest

from mcp_agent_mail.config import get_settings
from mcp_agent_mail.storage import (
    AsyncFileLock,
    collect_lock_status,
    ensure_mailbox_storage,
    process_attachments,
    write_file_reservation_record,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("slug", ("", ".", "..", "../escape", "nested/name", "nested\\name"))
async def test_ensure_mailbox_storage_rejects_unsafe_slugs(isolated_env, slug):
    with pytest.raises(ValueError, match="Invalid mailbox storage slug"):
        await ensure_mailbox_storage(get_settings(), slug)


@pytest.mark.asyncio
async def test_managed_projection_is_id_keyed_and_does_not_touch_legacy(isolated_env):
    settings = get_settings()
    root = Path(settings.storage.root)
    retained = root / "projects" / "legacy" / "reservation.json"
    retained.parent.mkdir(parents=True)
    retained.write_text("retained", encoding="utf-8")
    storage = await ensure_mailbox_storage(settings, "current")

    await write_file_reservation_record(
        storage,
        {"id": 17, "agent": "BlueLake", "path_pattern": "src/**", "exclusive": True},
    )

    projected = json.loads(
        (storage.root / "file_reservations" / "id-17.json").read_text(encoding="utf-8")
    )
    assert projected["path_pattern"] == "src/**"
    assert retained.read_text(encoding="utf-8") == "retained"
    assert not (root / ".git").exists()


@pytest.mark.asyncio
async def test_async_file_lock_serializes_managed_writes(tmp_path):
    lock_path = tmp_path / "managed.lock"
    entered: list[str] = []

    async def worker(name: str) -> None:
        async with AsyncFileLock(lock_path, timeout_seconds=2):
            entered.append(f"{name}:start")
            await asyncio.sleep(0.01)
            entered.append(f"{name}:end")

    await asyncio.gather(worker("a"), worker("b"))

    assert entered in (
        ["a:start", "a:end", "b:start", "b:end"],
        ["b:start", "b:end", "a:start", "a:end"],
    )
    assert not lock_path.exists()


@pytest.mark.asyncio
async def test_collect_lock_status_ignores_preserved_legacy_tree(isolated_env):
    settings = get_settings()
    root = Path(settings.storage.root)
    legacy_lock = root / "projects" / "legacy" / ".archive.lock"
    legacy_lock.parent.mkdir(parents=True)
    legacy_lock.write_text("preserve", encoding="utf-8")

    report = collect_lock_status(settings)

    assert report["locks"] == []
    assert legacy_lock.read_text(encoding="utf-8") == "preserve"


@pytest.mark.asyncio
async def test_attachment_paths_are_confined_to_managed_mailbox(isolated_env):
    storage = await ensure_mailbox_storage(get_settings(), "attachments")
    source = storage.root / "source.png"
    source.write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
    )

    body, metadata, files = await process_attachments(
        storage,
        "body",
        ["source.png"],
        convert_markdown=False,
        embed_policy="file",
    )

    assert body == "body"
    assert len(metadata) == 1
    assert files
    assert all((storage.repo_root / path).is_relative_to(storage.root) for path in files)
    assert all((storage.repo_root / str(item["path"])).is_relative_to(storage.root) for item in metadata)
    assert not (storage.repo_root / ".git").exists()
