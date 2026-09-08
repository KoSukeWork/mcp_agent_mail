from __future__ import annotations

import contextlib
import sys

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlmodel import SQLModel

from mcp_agent_mail import config as _config
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.db import _migrate_sqlite_project_lifecycle, ensure_schema
from mcp_agent_mail.http import build_http_app, main as http_main
from mcp_agent_mail.models import Project


@pytest.mark.parametrize("legacy", [True, False])
def test_project_lifecycle_schema_preserves_existing_data(legacy):
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            if legacy:
                connection.exec_driver_sql(
                    "CREATE TABLE projects (id INTEGER PRIMARY KEY, slug VARCHAR(255) NOT NULL UNIQUE, "
                    "human_key VARCHAR(255) NOT NULL, created_at DATETIME NOT NULL, archived_at DATETIME)"
                )
            else:
                SQLModel.metadata.create_all(connection)
            connection.execute(text(
                "INSERT INTO projects (id, slug, human_key, created_at, archived_at) "
                "VALUES (1, 'existing', :key, '2026-01-01 00:00:00', '2026-02-01 00:00:00')"
            ), {"key": "C:/software/important-project"})
            before = connection.exec_driver_sql(
                "SELECT id, slug, human_key, created_at, archived_at FROM projects"
            ).one()
            _migrate_sqlite_project_lifecycle(connection)
            _migrate_sqlite_project_lifecycle(connection)
            assert connection.exec_driver_sql(
                "SELECT id, slug, human_key, created_at, archived_at FROM projects"
            ).one() == before
            assert connection.exec_driver_sql(
                "SELECT mailbox_type, retention_days, last_activity_at, mailbox_state, trashed_at, purge_after "
                "FROM projects WHERE id = 1"
            ).one() == ("permanent", 30, None, "active", None, None)
            # Newly inserted rows also get safe database defaults, not only ORM defaults.
            connection.exec_driver_sql(
                "INSERT INTO projects (slug, human_key, created_at) "
                "VALUES ('new', '/software/new', '2026-03-01 00:00:00')"
            )
            assert connection.exec_driver_sql(
                "SELECT mailbox_type, mailbox_state, retention_days FROM projects WHERE slug = 'new'"
            ).one() == ("permanent", "active", 30)
            connection.exec_driver_sql(
                "UPDATE projects SET mailbox_type = 'temporary', retention_days = 45, "
                "last_activity_at = '2026-03-02 00:00:00', mailbox_state = 'trash', "
                "trashed_at = '2026-04-16 00:00:00', purge_after = '2026-04-23 00:00:00' WHERE id = 1"
            )
            changed = connection.exec_driver_sql("SELECT * FROM projects WHERE id = 1").one()
            _migrate_sqlite_project_lifecycle(connection)
            assert connection.exec_driver_sql("SELECT * FROM projects WHERE id = 1").one() == changed
            indexes = {index["name"] for index in inspect(connection).get_indexes("projects")}
            assert {"idx_projects_mailbox_activity", "idx_projects_mailbox_purge"} <= indexes
            for assignment in (
                "mailbox_type = 'unknown'", "mailbox_state = 'unknown'",
                "retention_days = 0", "retention_days = 3651",
            ):
                with pytest.raises(IntegrityError):
                    connection.exec_driver_sql(f"UPDATE projects SET {assignment} WHERE id = 1")
    finally:
        engine.dispose()


def test_project_lifecycle_migration_does_not_hide_storage_errors(monkeypatch):
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE projects (id INTEGER PRIMARY KEY)")
            execute = connection.exec_driver_sql

            def fail_alter(statement, *args, **kwargs):
                if statement.startswith("ALTER TABLE"):
                    raise OperationalError(statement, {}, RuntimeError("disk full"))
                return execute(statement, *args, **kwargs)

            monkeypatch.setattr(connection, "exec_driver_sql", fail_alter)
            with pytest.raises(OperationalError, match="disk full"):
                _migrate_sqlite_project_lifecycle(connection)
    finally:
        engine.dispose()


def test_project_lifecycle_orm_defaults_are_non_expiring():
    project = Project(slug="software", human_key="/software")
    assert project.mailbox_type == "permanent"
    assert project.mailbox_state == "active"
    assert project.retention_days == 30
    assert project.last_activity_at is None
    assert project.trashed_at is None
    assert project.purge_after is None


def test_http_main_invokes_uvicorn(monkeypatch):
    # Ensure settings default host/port are used
    with contextlib.suppress(Exception):
        _config.clear_settings_cache()
    calls: dict[str, object] = {}

    def fake_run(app, host, port, log_level="info"):
        calls["host"] = host
        calls["port"] = port
        calls["lv"] = log_level

    monkeypatch.setenv("HTTP_HOST", "127.0.0.1")
    monkeypatch.setenv("HTTP_PORT", "8765")
    monkeypatch.setattr("uvicorn.run", fake_run)
    # Prevent pytest argv from leaking into argparse
    monkeypatch.setattr(sys, "argv", ["mcp-http"])
    http_main()
    assert calls.get("host") == "127.0.0.1"


async def _readiness_ok() -> int:
    # Sanity check app readiness OK path with schema ensured
    await ensure_schema()
    settings = _config.get_settings()
    server = build_mcp_server()
    app = build_http_app(settings, server)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/health/readiness")
        return r.status_code


def test_readiness_ok_status(isolated_env):
    import asyncio

    code = asyncio.run(_readiness_ok())
    assert code in (200, 503)


