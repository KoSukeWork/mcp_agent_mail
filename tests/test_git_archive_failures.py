"""Storage regressions for retired Git-backed mailbox persistence."""

import json
from pathlib import Path

import pytest

from mcp_agent_mail.config import get_settings
from mcp_agent_mail.storage import (
    collect_lock_status,
    ensure_mailbox_storage,
    write_file_reservation_record,
)


@pytest.mark.asyncio
async def test_managed_storage_never_initializes_or_changes_retained_git_archive(isolated_env):
    settings = get_settings()
    root = Path(settings.storage.root)
    retained = root / "projects" / "legacy" / "message.md"
    retained.parent.mkdir(parents=True)
    retained.write_text("retained original", encoding="utf-8")

    storage = await ensure_mailbox_storage(settings, "current")
    await write_file_reservation_record(
        storage,
        {"id": 42, "agent": "BlueLake", "path_pattern": "src/**", "exclusive": True},
    )

    record = json.loads((storage.root / "file_reservations" / "id-42.json").read_text(encoding="utf-8"))
    assert record["path_pattern"] == "src/**"
    assert retained.read_text(encoding="utf-8") == "retained original"
    assert not (root / ".git").exists()


@pytest.mark.asyncio
async def test_lock_status_ignores_retained_legacy_lock_files(isolated_env):
    settings = get_settings()
    root = Path(settings.storage.root)
    legacy_lock = root / "projects" / "legacy" / ".archive.lock"
    legacy_lock.parent.mkdir(parents=True)
    legacy_lock.write_text("do not alter", encoding="utf-8")

    report = collect_lock_status(settings)

    assert report["locks"] == []
    assert legacy_lock.read_text(encoding="utf-8") == "do not alter"
