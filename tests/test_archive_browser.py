"""Git-backed history endpoints are retired, not silently backed by a new repo."""

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from mcp_agent_mail import app as app_module, cli as cli_module, http as http_module, storage
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.config import get_settings
from mcp_agent_mail.db import ensure_schema
from mcp_agent_mail.http import build_http_app


@pytest.mark.asyncio
async def test_git_history_routes_are_not_registered(isolated_env, monkeypatch):
    settings = get_settings()
    await ensure_schema()

    async def forbidden(*args, **kwargs):
        raise AssertionError("HTTP data access must not initialize Git")

    monkeypatch.setattr(storage, "_ensure_repo", forbidden)
    original = Path(settings.storage.root) / "projects" / "offline" / "retained.txt"
    original.parent.mkdir(parents=True)
    original.write_text("Retain the original archive", encoding="utf-8")
    app = build_http_app(settings, build_mcp_server())
    for name in (
        "archive_guide",
        "archive_activity",
        "archive_commit",
        "archive_timeline",
        "archive_browser",
        "archive_network",
        "archive_time_travel",
    ):
        assert not hasattr(http_module, name)
    assert not hasattr(app_module, "write_message_bundle")
    assert not hasattr(app_module, "write_agent_profile")
    assert not hasattr(cli_module, "projects_adopt")
    assert not hasattr(cli_module, "doctor_repair")
    assert not hasattr(cli_module, "doctor_backups")
    assert not hasattr(cli_module, "doctor_restore")
    assert not any(getattr(route, "path", "").startswith("/mail/archive/") for route in app.routes)
    paths = ("guide", "activity", "commit/abc123", "timeline", "browser", "browser/offline/file",
             "browser/offline/download", "network", "time-travel", "time-travel/snapshot")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for path in paths:
            response = await client.get(f"/mail/archive/{path}")
            assert response.status_code == 404, (path, response.text)
        assert (await client.get("/mail/api/projects/offline/agents")).status_code == 404
        assert (await client.get("/mail/activity")).status_code == 200
        assert (await client.get("/mail")).status_code == 200
    assert original.read_text(encoding="utf-8") == "Retain the original archive"
    assert not (Path(settings.storage.root) / ".git").exists()
