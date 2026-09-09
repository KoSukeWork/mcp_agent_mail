"""Regression coverage after retiring mailbox Git index operations."""

import asyncio
import json
from pathlib import Path

import pytest

from mcp_agent_mail.config import get_settings
from mcp_agent_mail.storage import ensure_mailbox_storage, write_file_reservation_record


@pytest.mark.asyncio
async def test_concurrent_guard_projections_do_not_create_git_index(isolated_env):
    settings = get_settings()
    storage = await ensure_mailbox_storage(settings, "index-free")

    await asyncio.gather(*(
        write_file_reservation_record(
            storage,
            {"id": index, "agent": f"Agent{index}", "path_pattern": f"src/{index}.py"},
        )
        for index in range(1, 9)
    ))

    records = sorted((storage.root / "file_reservations").glob("id-*.json"))
    assert len(records) == 8
    assert all(json.loads(path.read_text(encoding="utf-8"))["id"] > 0 for path in records)
    assert not (Path(settings.storage.root) / ".git").exists()
    assert not (Path(settings.storage.root) / ".git" / "index.lock").exists()


@pytest.mark.asyncio
async def test_managed_projection_preserves_retained_index_lock(isolated_env):
    settings = get_settings()
    root = Path(settings.storage.root)
    retained_lock = root / "projects" / "legacy" / ".git" / "index.lock"
    retained_lock.parent.mkdir(parents=True)
    retained_lock.write_text("retained", encoding="utf-8")

    storage = await ensure_mailbox_storage(settings, "current")
    await write_file_reservation_record(
        storage,
        {"id": 1, "agent": "BlueLake", "path_pattern": "src/**"},
    )

    assert retained_lock.read_text(encoding="utf-8") == "retained"
