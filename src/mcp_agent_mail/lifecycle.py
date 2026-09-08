"""Database-owned mailbox policy and recoverable expiry transitions.

Filesystem reclamation is deliberately separate from transactional deletion.
Only a successfully claimed temporary mailbox can enter the purge phase.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

from sqlalchemy import delete, func, or_, select as sa_select, text, update
from sqlmodel import SQLModel, select

from .config import Settings
from .db import get_session
from .models import MailboxEvent, Project
from .storage import AsyncFileLock

TRASH_DAYS = 7


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def project_expiry(project: Project) -> datetime | None:
    if project.mailbox_type != "temporary" or project.mailbox_state != "active":
        return None
    return (project.last_activity_at or project.created_at) + timedelta(days=project.retention_days)


def mailbox_dict(project: Project) -> dict[str, Any]:
    def timestamp(value: datetime | None) -> str | None:
        if value is None:
            return None
        normalized = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
        return normalized.isoformat().replace("+00:00", "Z")

    return {
        "id": project.id, "slug": project.slug, "human_key": project.human_key,
        "mailbox_type": project.mailbox_type, "mailbox_state": project.mailbox_state,
        "retention_days": project.retention_days,
        "last_activity_at": timestamp(project.last_activity_at or project.created_at),
        "expires_at": timestamp(project_expiry(project)),
        "trashed_at": timestamp(project.trashed_at), "purge_after": timestamp(project.purge_after),
        "cleanup_error": project.cleanup_error,
    }


async def list_mailboxes() -> list[dict[str, Any]]:
    async with get_session() as session:
        projects = (await session.scalars(select(Project).order_by(cast(Any, Project.created_at).desc()))).all()
        return [mailbox_dict(project) for project in projects]


async def require_active_project(project_id: int) -> None:
    async with get_session() as session:
        project = await session.get(Project, project_id)
        if project is None or project.mailbox_state != "active":
            raise ValueError("Mailbox is unavailable; restore it from the recycle bin before use")


async def touch_project(project_id: int, *, now: datetime | None = None) -> None:
    """Call only for explicit human access or an actual message operation."""
    now = now or utcnow()
    async with get_session() as session:
        await session.execute(
            update(Project).where(
                cast(Any, Project.id) == project_id,
                cast(Any, Project.mailbox_type) == "temporary",
                cast(Any, Project.mailbox_state) == "active",
                func.coalesce(Project.last_activity_at, Project.created_at) < now,
            ).values(last_activity_at=now)
        )
        await session.commit()


async def configure_mailbox(
    project_id: int, *, mailbox_type: str | None = None,
    retention_days: int | None = None, action: str = "configure", now: datetime | None = None,
) -> dict[str, Any]:
    if mailbox_type is not None and mailbox_type not in {"permanent", "temporary"}:
        raise ValueError("Invalid mailbox type")
    if retention_days is not None and (isinstance(retention_days, bool) or not 1 <= retention_days <= 3650):
        raise ValueError("Retention must be between 1 and 3650 days")
    if action not in {"configure", "restore", "trash"}:
        raise ValueError("Invalid mailbox action")
    now = now or utcnow()
    async with get_session() as session:
        project = await session.get(Project, project_id)
        if project is None:
            raise LookupError("Mailbox not found")
        if project.mailbox_state == "purging":
            raise ValueError("Mailbox cleanup has already started")
        values: dict[str, Any] = {"cleanup_error": None}
        if mailbox_type is not None:
            values["mailbox_type"] = mailbox_type
        if retention_days is not None:
            values["retention_days"] = retention_days
        if action == "trash":
            if (mailbox_type or project.mailbox_type) != "temporary":
                raise ValueError("Permanent mailboxes cannot expire or enter the recycle bin")
            if project.mailbox_state != "active":
                raise ValueError("Mailbox is already in the recycle bin")
            values.update(mailbox_state="trash", trashed_at=now, purge_after=now + timedelta(days=TRASH_DAYS))
        elif action == "restore" or mailbox_type == "permanent":
            values.update(mailbox_state="active", trashed_at=None, purge_after=None, last_activity_at=now)
        elif project.mailbox_state != "active":
            raise ValueError("Restore the mailbox before changing its settings")
        elif mailbox_type == "temporary" and project.mailbox_type != "temporary":
            values["last_activity_at"] = now
        if values:
            # Conditional update fences restoration against a simultaneous purge claim.
            result = await session.execute(update(Project).where(
                cast(Any, Project.id) == project_id,
                cast(Any, Project.mailbox_state) == project.mailbox_state,
                cast(Any, Project.mailbox_type) == project.mailbox_type,
            ).values(**values))
            if getattr(result, "rowcount", 0) != 1:
                raise ValueError("Mailbox changed concurrently; refresh and retry")
            session.add(MailboxEvent(project_id=project_id, event_type=action, detail=str(values), created_at=now))
            await session.commit()
            await session.refresh(project)
        return mailbox_dict(project)


async def expire_mailboxes(*, now: datetime | None = None, limit: int = 100) -> int:
    """Move only due temporary projects to trash; a fresh activity update wins."""
    now = now or utcnow()
    due = func.julianday(func.coalesce(Project.last_activity_at, Project.created_at)) + Project.retention_days
    async with get_session() as session:
        ids = (await session.scalars(select(Project.id).where(
            cast(Any, Project.mailbox_type) == "temporary",
            cast(Any, Project.mailbox_state) == "active", due <= func.julianday(now),
        ).limit(limit))).all()
        changed = 0
        for project_id in ids:
            result = await session.execute(update(Project).where(
                cast(Any, Project.id) == project_id,
                cast(Any, Project.mailbox_type) == "temporary",
                cast(Any, Project.mailbox_state) == "active", due <= func.julianday(now),
            ).values(mailbox_state="trash", trashed_at=now, purge_after=now + timedelta(days=TRASH_DAYS)))
            if getattr(result, "rowcount", 0) == 1:
                changed += 1
                session.add(MailboxEvent(project_id=cast(int, project_id), event_type="expired", created_at=now))
        await session.commit()
        return changed


def _project_directory(settings: Settings, slug: str, *, legacy: bool = False) -> Path:
    root = Path(settings.storage.root).expanduser().resolve()
    if not slug or Path(slug).name != slug or slug in {".", ".."} or "/" in slug or "\\" in slug:
        raise ValueError("Invalid mailbox storage slug")
    parent = root / ("projects" if legacy else "mailboxes")
    target = parent / slug
    if parent.is_symlink() or parent.is_junction() or target.is_symlink() or target.is_junction():
        raise ValueError("Refusing mailbox cleanup through a filesystem link")
    if target.resolve().parent != parent.resolve():
        raise ValueError("Mailbox path escapes its storage directory")
    return target


def _legacy_messages(directory: Path) -> list[tuple[dict[str, Any], str]]:
    """Read canonical legacy copies only; never rewrite the old archive."""
    messages: list[tuple[dict[str, Any], str]] = []
    root = directory / "messages"
    if root.is_symlink() or root.is_junction():
        raise ValueError("Linked archive content requires manual reconciliation")
    if not root.exists():
        return messages
    for path in root.rglob("*.md"):
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("Linked archive content requires manual reconciliation")
        content = path.read_text(encoding="utf-8")
        if not content.startswith("---json\n"):
            raise ValueError("Unrecognized legacy message format")
        header, separator, body = content[8:].partition("\n---\n")
        if not separator:
            raise ValueError("Incomplete legacy message")
        metadata = json.loads(header)
        if not isinstance(metadata, dict) or type(metadata.get("id")) is not int:
            raise ValueError("Legacy message has no stable numeric identity")
        messages.append((metadata, body.strip()))
    return messages


async def archive_reconciliation(settings: Settings, project: Project) -> dict[str, Any]:
    """A mismatch blocks cleanup instead of silently losing archive-only data."""
    directory = _project_directory(settings, project.slug, legacy=True)
    copies = await asyncio.to_thread(_legacy_messages, directory)
    table = SQLModel.metadata.tables["messages"]
    recipients = SQLModel.metadata.tables["message_recipients"]
    agents = SQLModel.metadata.tables["agents"]
    async with get_session() as session:
        rows = (await session.execute(sa_select(table).where(
            table.c.project_id == project.id,
        ))).mappings().all()
        recipient_rows = (await session.execute(select(
            recipients.c.message_id, recipients.c.kind, agents.c.name,
        ).join(agents, agents.c.id == recipients.c.agent_id).join(
            table, table.c.id == recipients.c.message_id,
        ).where(table.c.project_id == project.id))).all()
    database = {row["id"]: row for row in rows}
    address_book: dict[int, dict[str, list[str]]] = {}
    for mid, kind, name in recipient_rows:
        address_book.setdefault(mid, {}).setdefault(kind, []).append(name)
    mismatches: set[int] = set()
    for metadata, body in copies:
        mid = metadata["id"]
        row = database.get(mid)
        if row is None or row["body_md"].strip() != body:
            mismatches.add(mid)
            continue
        for field in ("subject", "importance", "ack_required", "thread_id", "attachments"):
            if field in metadata and metadata[field] != row[field]:
                mismatches.add(mid)
        for kind in ("to", "cc", "bcc"):
            if kind in metadata and sorted(metadata[kind]) != sorted(address_book.get(mid, {}).get(kind, [])):
                mismatches.add(mid)
    return {"project_id": project.id, "legacy_messages": len(copies), "database_messages": len(rows),
            "mismatched_ids": sorted(mismatches), "safe": not mismatches}


def _protect_source_directories(directory: Path, human_keys: list[str]) -> None:
    target = directory.resolve()
    for key in human_keys:
        source = Path(key).expanduser()
        if source.is_absolute() and source.resolve().is_relative_to(target):
            raise ValueError("A source repository lies inside the cleanup target; refusing deletion")


def _remove_mailbox_directory(directory: Path) -> None:
    if not directory.exists():
        return
    # Preflight before removing anything. Refuse links/junctions, including nested ones.
    pending = [directory]
    while pending:
        current = pending.pop()
        if current.is_symlink() or current.is_junction():
            raise ValueError("Linked mailbox files require manual cleanup")
        if current.is_dir():
            pending.extend(current.iterdir())
    shutil.rmtree(directory)


async def purge_mailbox(settings: Settings, project_id: int, *, now: datetime | None = None) -> bool:
    """Claim, drain writers, reclaim files, then transactionally delete owned rows.

    A failure leaves the project in purging state for retry. The shared legacy
    Git repository is never rewritten or removed by this operation.
    """
    now = now or utcnow()
    async with get_session() as session:
        project = await session.get(Project, project_id)
        if project is None or project.mailbox_type != "temporary":
            return False
        if project.mailbox_state != "purging" and (
            project.mailbox_state != "trash" or project.purge_after is None or project.purge_after > now
        ):
            return False
        slug = project.slug
    directory = _project_directory(settings, slug)
    lock_root = Path(settings.storage.root).expanduser().resolve() / ".mailbox-locks"
    if any(path.is_symlink() or path.is_junction() for path in (lock_root, lock_root / f"{slug}.lock")):
        raise ValueError("Linked mailbox locks are not allowed")
    await asyncio.to_thread(lock_root.mkdir, parents=True, exist_ok=True)
    async with AsyncFileLock(lock_root / f"{slug}.lock"):
        async with get_session() as session:
            project = await session.get(Project, project_id)
            if project is None:
                return False
            if project.mailbox_type != "temporary" or project.mailbox_state not in {"trash", "purging"}:
                return False
        report = await archive_reconciliation(settings, project)
        if not report["safe"]:
            raise ValueError("Legacy archive differs from database; cleanup requires reconciliation")
        # Attachments generated by this system live under the owning project's directory.
        # Refuse cross-project absolute references rather than deleting another mailbox's data.
        tables = SQLModel.metadata.tables
        messages = tables["messages"]
        async with get_session() as session:
            # Serialize the final reference check and purge claim against message inserts.
            await session.execute(text("BEGIN IMMEDIATE"))
            human_keys = list((await session.scalars(select(Project.human_key))).all())
            await asyncio.to_thread(_protect_source_directories, directory, human_keys)
            agents = tables["agents"]
            external_authorship = await session.scalar(select(messages.c.id).where(
                messages.c.project_id != project_id,
                messages.c.sender_id.in_(select(agents.c.id).where(agents.c.project_id == project_id)),
            ).limit(1))
            if external_authorship is not None:
                raise ValueError("Another mailbox retains messages from this project's agents; cleanup is paused")
            shared = await session.scalar(text(
                "SELECT 1 FROM messages m, json_each(m.attachments) a "
                "WHERE m.project_id != :pid AND (instr(replace(json_extract(a.value, '$.path'), char(92), '/'), :path) > 0 "
                "OR instr(replace(json_extract(a.value, '$.path'), char(92), '/'), :legacy) > 0) "
                "LIMIT 1"
            ), {"pid": project_id, "path": f"mailboxes/{slug}/", "legacy": f"projects/{slug}/"})
            if shared is not None:
                raise ValueError("Mailbox has attachments referenced by another project")
            if project.mailbox_state != "purging":
                result = await session.execute(update(Project).where(
                    cast(Any, Project.id) == project_id,
                    cast(Any, Project.mailbox_type) == "temporary",
                    cast(Any, Project.mailbox_state) == "trash",
                    cast(Any, Project.purge_after) <= now,
                ).values(mailbox_state="purging", cleanup_error=None))
                if getattr(result, "rowcount", 0) != 1:
                    return False
            await session.commit()
        await asyncio.to_thread(_remove_mailbox_directory, directory)
        async with get_session() as session:
            agents = tables["agents"]
            agent_ids = select(agents.c.id).where(agents.c.project_id == project_id)
            message_ids = select(messages.c.id).where(messages.c.project_id == project_id)
            recipients = tables["message_recipients"]
            await session.execute(delete(recipients).where(or_(
                recipients.c.message_id.in_(message_ids), recipients.c.agent_id.in_(agent_ids),
            )))
            links = tables["agent_links"]
            await session.execute(delete(links).where(or_(
                links.c.a_project_id == project_id, links.c.b_project_id == project_id,
            )))
            await session.execute(update(messages).where(messages.c.reply_to.in_(message_ids)).values(reply_to=None))
            for name in ("file_reservations", "messages", "agents", "window_identities",
                         "message_summaries", "product_project_links", "mailbox_events"):
                table = tables[name]
                await session.execute(delete(table).where(table.c.project_id == project_id))
            siblings = tables["project_sibling_suggestions"]
            await session.execute(delete(siblings).where(or_(
                siblings.c.project_a_id == project_id, siblings.c.project_b_id == project_id,
            )))
            await session.execute(delete(Project).where(cast(Any, Project.id) == project_id))
            await session.commit()
        return True


async def due_cleanup_ids(*, now: datetime | None = None) -> list[int]:
    async with get_session() as session:
        ids = await session.scalars(select(Project.id).where(
            cast(Any, Project.mailbox_type) == "temporary",
            or_(cast(Any, Project.mailbox_state) == "purging",
                (cast(Any, Project.mailbox_state) == "trash") & (cast(Any, Project.purge_after) <= (now or utcnow()))),
        ).limit(100))
        return [cast(int, value) for value in ids]
