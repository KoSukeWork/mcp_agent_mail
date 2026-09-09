"""Regression coverage for retirement of Git-backed mailbox time travel."""

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.config import get_settings
from mcp_agent_mail.db import ensure_schema
from mcp_agent_mail.http import build_http_app


@pytest.mark.asyncio
async def test_time_travel_routes_are_retired_and_legacy_source_is_untouched(isolated_env):
    settings = get_settings()
    await ensure_schema()
    retained = Path(settings.storage.root) / "projects" / "timetravel-test" / "messages" / "retained.md"
    retained.parent.mkdir(parents=True)
    retained.write_text("retained history", encoding="utf-8")

    app = build_http_app(settings, build_mcp_server())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for path in (
            "/mail/archive/time-travel",
            "/mail/archive/time-travel/snapshot",
            "/mail/api/projects/timetravel-test/agents",
        ):
            assert (await client.get(path)).status_code == 404
        assert (await client.get("/mail/activity")).status_code == 200

    assert retained.read_text(encoding="utf-8") == "retained history"
    assert not (Path(settings.storage.root) / ".git").exists()
