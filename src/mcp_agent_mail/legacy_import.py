"""Explicit, transactional import of retained legacy mailbox files.

The source archive is never written. Missing identities, differing messages and
history-only Git data are blockers, not permission to invent or overwrite data.
Normal mailbox operation does not call this migration module.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, cast

from sqlalchemy import text
from sqlmodel import select

from .config import Settings
from .db import ensure_schema, get_session
from .lifecycle import _legacy_file_fingerprints, _legacy_manifest_digest, _legacy_messages
from .models import Agent, LegacyImportRecord, Message, MessageRecipient, Project


def _timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"Missing legacy timestamp: {field}")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Invalid legacy timestamp: {field}") from exc
    if result.tzinfo is not None:
        result = result.astimezone(timezone.utc).replace(tzinfo=None)
    return result


def _inventory(root: Path) -> dict[str, Any]:
    if root.is_symlink() or root.is_junction():
        raise ValueError("Linked archive roots cannot be imported")
    projects_root = root / "projects"
    files = _legacy_file_fingerprints(projects_root)
    projects: list[dict[str, Any]] = []
    if projects_root.exists():
        if projects_root.is_symlink() or projects_root.is_junction():
            raise ValueError("Linked legacy project directories cannot be imported")
        for directory in sorted(projects_root.iterdir()):
            if not directory.is_dir():
                continue
            profiles = []
            for path in sorted(directory.glob("agents/*/profile.json")):
                profile = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(profile, dict) or profile.get("name") != path.parent.name:
                    raise ValueError(f"Invalid legacy agent profile: {path}")
                profiles.append(profile)
            messages = _legacy_messages(directory)
            canonical_hashes = {digest for path, digest in files.items()
                                if path.is_relative_to(directory / "messages") and path.suffix == ".md"}
            for path, digest in files.items():
                if not path.is_relative_to(directory):
                    continue
                parts = path.relative_to(directory).parts
                if (len(parts) >= 4 and parts[0] == "agents" and parts[2] in {"inbox", "outbox"}
                        and path.suffix == ".md" and digest not in canonical_hashes):
                    raise ValueError(f"Unreconciled legacy agent message copy: {path}")
            projects.append({"slug": directory.name, "profiles": profiles, "messages": messages,
                             "source_digest": _legacy_manifest_digest(directory, files)})

    historical_only = []
    if (root / ".git").exists():
        # Inspection only: no checkout, filter invocation, reset, commit or repair.
        result = subprocess.run(
            ["git", "-C", str(root), "log", "--all", "--format=", "--name-only", "--", "projects"],
            check=True, capture_output=True, text=True, encoding="utf-8",
        )
        for name in sorted(set(result.stdout.splitlines())):
            if ("/messages/" in name or "/attachments/" in name or name.endswith("/profile.json")) and not (root / name).is_file():
                historical_only.append(name)
    return {"projects": projects, "fingerprints": files, "historical_only": historical_only}


def _verify_source(root: Path, fingerprints: dict[Path, str]) -> None:
    current = _inventory(root)
    if current["fingerprints"] != fingerprints or current["historical_only"]:
        raise ValueError("Legacy source changed during import")


async def import_legacy_archive(settings: Settings, *, apply: bool = False) -> dict[str, Any]:
    """Validate all projects and import atomically; dry-run rolls back every write."""
    await ensure_schema(settings)
    root = Path(settings.storage.root).expanduser()
    inventory = await asyncio.to_thread(_inventory, root)
    if inventory["historical_only"]:
        raise ValueError("Git history contains files absent from the working archive; explicit historical reconciliation is required: "
                         + ", ".join(inventory["historical_only"]))
    report: dict[str, Any] = {"applied": apply, "projects": 0, "agents_imported": 0,
                              "messages_imported": 0, "messages_verified": 0, "previously_imported": [], "empty_directories": []}
    async with get_session() as session:
        await session.execute(text("BEGIN IMMEDIATE"))
        for legacy in inventory["projects"]:
            slug = legacy["slug"]
            completed = await session.get(LegacyImportRecord, slug)
            if completed is not None:
                if completed.source_digest != legacy["source_digest"]:
                    raise ValueError(f"Legacy archive changed after completed import; explicit reconciliation required: {slug}")
                report["previously_imported"].append(slug)
                continue
            if not legacy["profiles"] and not legacy["messages"]:
                report["empty_directories"].append(slug)
                continue
            project = (await session.scalars(select(Project).where(Project.slug == slug))).one_or_none()
            if project is None:
                raise ValueError(f"Legacy project has no database identity; explicit mapping required: {slug}")
            if project.mailbox_state != "active":
                raise ValueError(f"Restore the mailbox before importing: {slug}")
            report["projects"] += 1
            agents = {agent.name: agent for agent in (await session.scalars(
                select(Agent).where(Agent.project_id == project.id))).all()}
            for profile in legacy["profiles"]:
                name = profile["name"]
                if name in agents:
                    # Current identity and registration credentials remain authoritative.
                    continue
                if not all(isinstance(profile.get(field), str) and profile[field] for field in ("program", "model")):
                    raise ValueError(f"Incomplete legacy identity: {slug}/{name}")
                agent = Agent(project_id=cast(int, project.id), name=name,
                              program=profile["program"], model=profile["model"],
                              task_description=profile.get("task_description", ""),
                              inception_ts=_timestamp(profile.get("inception_ts"), "inception_ts"),
                              last_active_ts=_timestamp(profile.get("last_active_ts"), "last_active_ts"))
                session.add(agent)
                await session.flush()
                agents[name] = agent
                report["agents_imported"] += 1
            for metadata, body in sorted(legacy["messages"], key=lambda item: item[0]["id"]):
                message_id = metadata["id"]
                if message_id <= 0:
                    raise ValueError(f"Invalid legacy message identity: {slug}/{message_id}")
                sender_name = metadata.get("from")
                if not isinstance(sender_name, str) or sender_name not in agents:
                    raise ValueError(f"Unknown legacy sender: {slug}/{sender_name}")
                recipient_rows = []
                for kind in ("to", "cc", "bcc"):
                    names = metadata.get(kind, [])
                    if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
                        raise ValueError(f"Invalid legacy recipients: {slug}/{message_id}/{kind}")
                    for name in names:
                        if name not in agents:
                            raise ValueError(f"Unknown legacy recipient: {slug}/{name}")
                        recipient_rows.append((cast(int, agents[name].id), kind))
                if len({agent_id for agent_id, _kind in recipient_rows}) != len(recipient_rows):
                    raise ValueError(f"Duplicate legacy recipients: {slug}/{message_id}")
                attachments = metadata.get("attachments", [])
                if not isinstance(attachments, list) or any(not isinstance(item, dict) for item in attachments):
                    raise ValueError(f"Invalid legacy attachments: {slug}/{message_id}")
                for attachment in attachments:
                    value = attachment.get("path")
                    if value is None:
                        continue
                    if not isinstance(value, str) or PurePosixPath(value).is_absolute() or PureWindowsPath(value).drive or ".." in value.replace("\\", "/").split("/"):
                        raise ValueError(f"Unsafe legacy attachment: {slug}/{message_id}")
                    candidates = [root / value, root / "projects" / slug / value, root / "mailboxes" / slug / value]
                    if not any(path.is_file() and path.resolve().is_relative_to(root.resolve()) for path in candidates):
                        raise ValueError(f"Missing legacy attachment: {slug}/{message_id}/{value}")
                subject = metadata.get("subject")
                if not isinstance(subject, str):
                    raise ValueError(f"Missing legacy subject: {slug}/{message_id}")
                fields: dict[str, Any] = {
                    "project_id": project.id, "sender_id": agents[sender_name].id,
                    "subject": subject, "body_md": body, "importance": metadata.get("importance", "normal"),
                    "ack_required": metadata.get("ack_required", False), "thread_id": metadata.get("thread_id"),
                    "created_ts": _timestamp(metadata.get("created_ts", metadata.get("created")), "created_ts"),
                    "attachments": attachments,
                }
                if "reply_to" in metadata:
                    fields["reply_to"] = metadata["reply_to"]
                existing = await session.get(Message, message_id)
                if existing is not None:
                    if any(getattr(existing, key) != value for key, value in fields.items()):
                        raise ValueError(f"Legacy message differs from database: {slug}/{message_id}")
                    current_recipients = (await session.scalars(select(MessageRecipient).where(
                        MessageRecipient.message_id == message_id))).all()
                    if {(r.agent_id, r.kind) for r in current_recipients} != set(recipient_rows):
                        raise ValueError(f"Legacy recipients differ from database: {slug}/{message_id}")
                    report["messages_verified"] += 1
                    continue
                session.add(Message.model_validate({"id": message_id, **fields}))
                await session.flush()
                for agent_id, kind in recipient_rows:
                    session.add(MessageRecipient(message_id=message_id, agent_id=agent_id, kind=kind))
                report["messages_imported"] += 1
            session.add(LegacyImportRecord(project_slug=slug, source_digest=legacy["source_digest"],
                                           message_count=len(legacy["messages"])))
        await session.flush()
        await asyncio.to_thread(_verify_source, root, inventory["fingerprints"])
        if apply:
            await session.commit()
        else:
            await session.rollback()
    return report
