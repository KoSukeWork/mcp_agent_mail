import asyncio
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zipfile import ZipFile

import pytest
from sqlmodel import select
from typer.testing import CliRunner

from mcp_agent_mail.cli import app
from mcp_agent_mail.config import clear_settings_cache, get_settings
from mcp_agent_mail.db import ensure_schema, get_session
from mcp_agent_mail.models import (
    Agent,
    AgentConversationBinding,
    FileReservation,
    McpClientPrincipal,
    Project,
)


def test_cli_lint(monkeypatch):
    captured: list[list[str]] = []
    monkeypatch.setattr("mcp_agent_mail.cli._run_command", captured.append)
    assert CliRunner().invoke(app, ["lint"]).exit_code == 0
    assert captured == [["ruff", "check", "--fix", "--unsafe-fixes"]]


def test_cli_typecheck(monkeypatch):
    captured: list[list[str]] = []
    monkeypatch.setattr("mcp_agent_mail.cli._run_command", captured.append)
    assert CliRunner().invoke(app, ["typecheck"]).exit_code == 0
    assert captured == [["uvx", "ty", "check"]]


@pytest.mark.parametrize("command", [
    ["projects", "adopt", "source", "target", "--apply"],
    ["doctor", "repair"], ["doctor", "backups"], ["doctor", "restore", "old-backup"],
])
def test_git_data_commands_are_retired(isolated_env, command):
    original = Path(get_settings().storage.root) / "projects" / "legacy" / "retained.txt"
    original.parent.mkdir(parents=True)
    original.write_text("Retained offline", encoding="utf-8")
    result = CliRunner().invoke(app, command)
    assert result.exit_code == 2
    assert "No such command" in result.output
    assert original.read_text(encoding="utf-8") == "Retained offline"
    assert not (Path(get_settings().storage.root) / ".git").exists()


def test_cli_serve_http_uses_settings(isolated_env, monkeypatch):
    call_args: dict[str, Any] = {}

    def fake_uvicorn_run(app, host, port, log_level="info"):
        call_args.update(app=app, host=host, port=port, log_level=log_level)

    monkeypatch.setattr("uvicorn.run", fake_uvicorn_run)
    result = CliRunner().invoke(app, ["serve-http"])
    assert result.exit_code == 0
    assert call_args["host"] == "127.0.0.1"
    assert call_args["port"] == 8765


def test_cli_config_set_port_clears_cached_settings(tmp_path, monkeypatch):
    runner = CliRunner()
    env_path = tmp_path / ".env"
    env_path.write_text("HTTP_HOST=127.0.0.1\nHTTP_PORT=1111\nHTTP_PATH=/api/\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HTTP_HOST", raising=False)
    monkeypatch.delenv("HTTP_PORT", raising=False)
    monkeypatch.delenv("HTTP_PATH", raising=False)
    clear_settings_cache()
    before = runner.invoke(app, ["config", "show-port"])
    assert before.exit_code == 0 and "1111" in before.stdout
    assert runner.invoke(app, ["config", "set-port", "2222"]).exit_code == 0
    after = runner.invoke(app, ["config", "show-port"])
    assert after.exit_code == 0 and "2222" in after.stdout


def test_cli_serve_stdio(isolated_env, monkeypatch):
    from fastmcp import FastMCP

    call_args: dict[str, Any] = {}

    def fake_run(self, transport="stdio", **kwargs):
        call_args.update(transport=transport, kwargs=kwargs)

    monkeypatch.setattr(FastMCP, "run", fake_run)
    previous_handlers = logging.root.handlers[:]
    previous_level = logging.root.level
    assert CliRunner().invoke(app, ["serve-stdio"]).exit_code == 0
    assert call_args["transport"] == "stdio"
    assert logging.root.handlers == previous_handlers
    assert logging.root.level == previous_level


def test_cli_migrate(monkeypatch):
    invoked = []

    async def fake_migrate(settings):
        invoked.append(settings)

    monkeypatch.setattr("mcp_agent_mail.cli.ensure_schema", fake_migrate)
    assert CliRunner().invoke(app, ["migrate"]).exit_code == 0
    assert len(invoked) == 1


def test_cli_list_projects(isolated_env):
    async def seed():
        await ensure_schema()
        async with get_session() as session:
            project = Project(slug="demo", human_key="Demo")
            session.add(project)
            await session.commit()
            await session.refresh(project)
            assert project.id is not None
            session.add(Agent(project_id=project.id, name="BlueLake", program="codex", model="gpt-5"))
            await session.commit()

    asyncio.run(seed())
    result = CliRunner().invoke(app, ["list-projects", "--include-agents"])
    assert result.exit_code == 0
    assert "demo" in result.stdout
    assert "BlueLake" not in result.stdout


def test_cli_list_projects_json_returns_structured_error_on_failure(monkeypatch):
    async def failing_ensure_schema(_settings=None):
        raise RuntimeError("projects exploded")

    monkeypatch.setattr("mcp_agent_mail.cli.ensure_schema", failing_ensure_schema)
    result = CliRunner().invoke(app, ["list-projects", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.stdout) == {"error": "projects exploded"}


def test_archive_save_defaults_to_archive_preset(tmp_path, isolated_env, monkeypatch):
    archive_path = tmp_path / "state.zip"
    archive_path.write_bytes(b"zip")
    captured: dict[str, Any] = {}

    def fake_archive(**kwargs):
        captured.update(kwargs)
        return archive_path, {"scrub_preset": kwargs["scrub_preset"], "projects_requested": list(kwargs["project_filters"])}

    monkeypatch.setattr("mcp_agent_mail.cli._create_mailbox_archive", fake_archive)
    assert CliRunner().invoke(app, ["archive", "save"]).exit_code == 0
    assert captured["scrub_preset"] == "archive"


def test_clear_and_reset_skips_archive_when_disabled(isolated_env, monkeypatch):
    def forbidden(**_kwargs):
        raise AssertionError("archive should not be invoked when --no-archive is supplied")

    monkeypatch.setattr("mcp_agent_mail.cli._create_mailbox_archive", forbidden)
    result = CliRunner().invoke(app, ["clear-and-reset-everything", "--force", "--no-archive"])
    assert result.exit_code == 0


def test_clear_and_reset_preserves_legacy_git_and_backups(isolated_env, tmp_path):
    asyncio.run(ensure_schema())
    root = Path(get_settings().storage.root)
    retained = [root / name for name in ("projects/legacy/history.md", ".git/retained.txt", "backups/original.zip", "notes.txt")]
    for path in retained:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("preserved original", encoding="utf-8")
    managed = root / "mailboxes" / "managed"
    managed.mkdir(parents=True)
    (managed / "data.txt").write_text("runtime", encoding="utf-8")
    result = CliRunner().invoke(app, ["clear-and-reset-everything", "--force", "--no-archive"])
    assert result.exit_code == 0, result.output
    assert not managed.exists()
    assert not (tmp_path / "test.sqlite3").exists()
    assert all(path.read_text(encoding="utf-8") == "preserved original" for path in retained)


@pytest.mark.parametrize("operation", ["reset", "restore"])
def test_reset_and_restore_refuse_registered_source_directory(isolated_env, tmp_path, operation):
    source = Path(get_settings().storage.root) / "mailboxes" / "managed" / "source"
    source.mkdir(parents=True)
    marker = source / "important.py"
    marker.write_text("valuable_source = True\n", encoding="utf-8")

    async def seed():
        await ensure_schema()
        async with get_session() as session:
            session.add(Project(slug="source-owner", human_key=str(source)))
            await session.commit()

    asyncio.run(seed())
    if operation == "restore":
        archive = tmp_path / "restore.zip"
        with ZipFile(archive, "w") as bundle:
            bundle.writestr("metadata.json", "{}")
            bundle.writestr("snapshot/mailbox.sqlite3", b"unused snapshot")
            bundle.writestr("storage_repo/marker.txt", "replacement")
        command = ["archive", "restore", str(archive), "--force"]
    else:
        command = ["clear-and-reset-everything", "--force", "--no-archive"]
    result = CliRunner().invoke(app, command)
    assert result.exit_code == 1
    assert "source repository" in result.output
    assert marker.read_text(encoding="utf-8") == "valuable_source = True\n"
    assert (tmp_path / "test.sqlite3").exists()
    assert "Reset complete" not in result.output


def test_archive_backup_is_non_git_and_never_overwrites_an_existing_backup(isolated_env, tmp_path, monkeypatch):
    import mcp_agent_mail.cli as cli_module

    async def seed():
        await ensure_schema()
        async with get_session() as session:
            session.add(Project(slug="retained", human_key=str(tmp_path / "source")))
            await session.commit()

    asyncio.run(seed())
    root = Path(get_settings().storage.root)
    legacy = root / "projects" / "retained" / "original.txt"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("original", encoding="utf-8")
    git_config = root / ".git" / "config"
    git_config.parent.mkdir()
    git_config.write_text("retained Git metadata", encoding="utf-8")
    backups = tmp_path / "backups"
    backups.mkdir()
    monkeypatch.setattr(cli_module, "_archive_states_dir", lambda create=False: backups)
    destination, metadata = cli_module._create_mailbox_archive(
        project_filters=[], scrub_preset="archive", label="test", status_message="",
    )
    before = destination.read_bytes()
    if os.name == "posix":
        assert destination.stat().st_mode & 0o777 == 0o600
    with ZipFile(destination) as archive:
        assert archive.testzip() is None
        assert "snapshot/mailbox.sqlite3" in archive.namelist()
        assert archive.read("storage_repo/projects/retained/original.txt") == b"original"
        assert not any(".git" in Path(name).parts for name in archive.namelist())
    assert "git_head" not in metadata["storage"]
    monkeypatch.setattr(cli_module, "_ensure_unique_archive_path", lambda *_args: destination)
    with pytest.raises(FileExistsError):
        cli_module._create_mailbox_archive(
            project_filters=[], scrub_preset="archive", label="test", status_message="",
        )
    assert destination.read_bytes() == before
    assert legacy.read_text(encoding="utf-8") == "original"
    assert git_config.read_text(encoding="utf-8") == "retained Git metadata"


def test_clear_and_reset_requires_ownership_database(isolated_env):
    managed = Path(get_settings().storage.root) / "mailboxes"
    managed.mkdir(parents=True)
    marker = managed / "retain.txt"
    marker.write_text("owned data", encoding="utf-8")
    result = CliRunner().invoke(app, ["clear-and-reset-everything", "--force", "--no-archive"])
    assert result.exit_code == 1
    assert "ownership database" in " ".join(result.output.split())
    assert marker.read_text(encoding="utf-8") == "owned data"


@pytest.mark.parametrize("linked_target", ["storage", "database"])
def test_clear_and_reset_rejects_links_before_resolving(isolated_env, tmp_path, monkeypatch, linked_target):
    asyncio.run(ensure_schema())
    root = Path(get_settings().storage.root)
    root.mkdir(parents=True, exist_ok=True)
    marker = root / "preserved.txt"
    marker.write_text("original", encoding="utf-8")
    link = tmp_path / "linked-target"
    target = root if linked_target == "storage" else tmp_path / "test.sqlite3"
    try:
        link.symlink_to(target, target_is_directory=linked_target == "storage")
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Creating symlinks requires Windows developer mode or the symlink privilege")
        raise
    if linked_target == "storage":
        monkeypatch.setenv("STORAGE_ROOT", str(link))
    else:
        monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{link.as_posix()}")
    clear_settings_cache()
    result = CliRunner().invoke(app, ["clear-and-reset-everything", "--force", "--no-archive"])
    assert result.exit_code == 1
    assert "Linked data targets" in result.output
    assert link.is_symlink()
    assert marker.read_text(encoding="utf-8") == "original"
    assert (tmp_path / "test.sqlite3").exists()


def test_cli_hard_delete_uses_authenticated_managed_cleanup(isolated_env):
    async def seed():
        await ensure_schema()
        async with get_session() as session:
            project = Project(slug="delete-me", human_key="/source/delete-me")
            session.add(project)
            await session.flush()
            assert project.id is not None
            session.add(Agent(project_id=project.id, name="BlueLake", program="test", model="test",
                              registration_token="test-owner-token"))
            await session.commit()

    asyncio.run(seed())
    root = Path(get_settings().storage.root)
    legacy = root / "projects" / "delete-me" / "retained.txt"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("original", encoding="utf-8")
    managed = root / "mailboxes" / "delete-me"
    managed.mkdir(parents=True)
    (managed / "data.txt").write_text("runtime", encoding="utf-8")
    runner = CliRunner()
    command = ["hard-delete-project", "delete-me", "--confirm", "I UNDERSTAND"]
    assert runner.invoke(app, [*command, "--token", "wrong-token"]).exit_code == 1
    assert managed.exists()
    result = runner.invoke(app, [*command, "--token", "test-owner-token"])
    assert result.exit_code == 0, result.output
    assert not managed.exists()
    assert legacy.read_text(encoding="utf-8") == "original"
    assert not (root / ".git").exists()


def test_doctor_check_reports_stale_locks(isolated_env):
    lock_path = Path(get_settings().storage.root) / "mailboxes" / "backend" / ".archive.lock"
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text("", encoding="utf-8")
    (lock_path.parent / ".archive.lock.owner.json").write_text(
        json.dumps({"pid": 999999, "created_ts": time.time() - 3600}), encoding="utf-8")
    result = CliRunner().invoke(app, ["doctor", "check", "--json"])
    assert result.exit_code == 0
    locks = next(item for item in json.loads(result.stdout)["diagnostics"] if item["name"] == "Locks")
    assert locks["status"] == "warning" and "stale" in locks["message"].lower()


def test_doctor_check_detects_non_sqlite3_wal_files(tmp_path, monkeypatch):
    database = tmp_path / "mail.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database}")
    clear_settings_cache()
    sqlite3.connect(database).close()
    (tmp_path / "mail.db-wal").write_text("wal", encoding="utf-8")
    result = CliRunner().invoke(app, ["doctor", "check", "--json"])
    assert result.exit_code == 0
    diagnostic = next(item for item in json.loads(result.stdout)["diagnostics"] if item["name"] == "WAL Files")
    assert diagnostic["status"] == "info" and "wal/shm file" in diagnostic["message"].lower()


def test_doctor_check_scopes_project_specific_findings(isolated_env):
    async def seed():
        await ensure_schema()
        async with get_session() as session:
            for slug, name in (("backend", "BlueLake"), ("frontend", "GreenCastle")):
                project = Project(slug=slug, human_key=f"/{slug}")
                session.add(project)
                await session.flush()
                assert project.id is not None
                agent = Agent(project_id=project.id, name=name, program="codex", model="gpt-5")
                session.add(agent)
                await session.flush()
                assert agent.id is not None
                session.add(FileReservation(project_id=project.id, agent_id=agent.id,
                    path_pattern=f"src/{slug}.py", expires_ts=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)))
            await session.commit()

    asyncio.run(seed())
    for slug in ("backend", "frontend"):
        lock_path = Path(get_settings().storage.root) / "mailboxes" / slug / ".archive.lock"
        lock_path.parent.mkdir(parents=True)
        lock_path.write_text("", encoding="utf-8")
        (lock_path.parent / ".archive.lock.owner.json").write_text(
            json.dumps({"pid": 999999, "created_ts": time.time() - 3600}), encoding="utf-8")
    result = CliRunner().invoke(app, ["doctor", "check", "Backend", "--json"])
    assert result.exit_code == 0
    diagnostics = json.loads(result.stdout)["diagnostics"]
    assert "1 stale lock" in next(item for item in diagnostics if item["name"] == "Locks")["message"].lower()
    assert "1 expired reservation" in next(item for item in diagnostics if item["name"] == "File Reservations")["message"].lower()


def test_identity_admin_cli_grants_and_revokes_client(isolated_env):
    client_uid = "cli-administrator-client-0001"

    async def seed() -> tuple[int, int]:
        await ensure_schema()
        async with get_session() as session:
            project = Project(slug="identity-cli", human_key="/identity/cli")
            session.add(project)
            await session.flush()
            assert project.id is not None
            agent = Agent(
                project_id=project.id,
                name="CoralBeacon",
                program="pi",
                model="test",
                binding_generation=1,
            )
            principal = McpClientPrincipal(
                client_uid=client_uid,
                credential_hash="a" * 64,
                scopes=["mailbox.identity.self"],
            )
            session.add(agent)
            session.add(principal)
            await session.flush()
            assert agent.id is not None
            assert principal.id is not None
            binding = AgentConversationBinding(
                client_principal_id=principal.id,
                project_id=project.id,
                agent_id=agent.id,
                conversation_binding_hash="b" * 64,
                generation=1,
            )
            session.add(binding)
            await session.commit()
            assert binding.id is not None
            return agent.id, binding.id

    agent_id, binding_id = asyncio.run(seed())
    runner = CliRunner()

    listed = runner.invoke(app, ["identity", "list-clients"])
    granted = runner.invoke(app, ["identity", "grant-admin", client_uid])
    revoked = runner.invoke(app, ["identity", "revoke-client", client_uid])

    assert listed.exit_code == 0
    assert "Trusted MCP client principals" in listed.stdout
    assert granted.exit_code == 0
    assert "mailbox.identity.admin" in granted.stdout
    assert revoked.exit_code == 0
    assert "invalidated 1 active" in revoked.stdout
    assert "binding(s)" in revoked.stdout

    async def verify() -> None:
        async with get_session() as session:
            principal = (
                await session.execute(
                    select(McpClientPrincipal).where(McpClientPrincipal.client_uid == client_uid)
                )
            ).scalars().one()
            binding = await session.get(AgentConversationBinding, binding_id)
            agent = await session.get(Agent, agent_id)
            assert principal.status == "revoked"
            assert "mailbox.identity.admin" in principal.scopes
            assert binding is not None and binding.status == "revoked"
            assert agent is not None and agent.binding_generation == 2

    asyncio.run(verify())
