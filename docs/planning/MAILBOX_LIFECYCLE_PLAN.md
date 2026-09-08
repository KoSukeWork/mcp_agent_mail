# Database-first mailbox lifecycle

## Approved behavior

- A mailbox is a project; the UI groups projects into permanent and temporary mailboxes.
- Existing projects default to permanent. Permanent projects never expire automatically.
- Temporary projects expire after 30 days without effective activity. Retention may be configured per project.
- Human viewing and actual message operations renew activity. Health checks, background refreshes and empty polling do not.
- Expiration moves a project into a seven-day trash period. During that period it can be restored or made permanent.
- After the grace period, cleanup removes only system-owned mailbox data and unreferenced attachments. A project's `human_key` is an identifier, NEVER a deletion target.
- The database becomes the authoritative store. Runtime mail operations must not require a Git archive.
- Existing archives remain untouched until migration reconciliation is reviewed. Backups have a separate retention policy.

## Current architecture and consequences

`models.Project` currently has only identity, creation and manual archival fields. Manual `archived_at` is not an expiration or trash marker and must not be repurposed.

`storage.ensure_archive()` opens one shared Git repository at the configured storage root and creates project directories below `projects/`. Removing current files does not remove their Git history. Per-project cleanup must therefore not claim to reclaim historical Git objects.

`db.ensure_schema()` creates SQLModel tables and then runs SQLite index/FTS setup. Existing databases require explicit, idempotent column migration; creating metadata alone does not update existing tables. New lifecycle migrations must inspect columns and propagate unexpected errors, not suppress all exceptions.

`http.py` already has archive/unarchive endpoints and maintenance workers. `app.py` exposes archive/unarchive MCP tools. All paths that can create, access or mutate project data need lifecycle enforcement, not just web routes.

## Implementation sequence and gates

1. **Schema foundation**: add classification, retention, effective activity, lifecycle state and trash deadline fields. Keep existing behavior unchanged. Test old-database migration, idempotence, server defaults, constraints and preservation of messages/identity.
2. **Storage reconciliation**: inventory database and archive data, including message bodies, recipients, read/ack timestamps, attachments and export metadata. Produce a read-only discrepancy report. Import archive-only records with stable identity and explicit conflict reporting; never overwrite newer database data silently.
3. **Database-first I/O**: replace runtime Git writes and archive-dependent reads across messaging, historical views, exports, restoration and CLI operations. Add database audit/history records where needed. Normalize attachment ownership/reference tracking before deletion is possible. Source-code Git identity and reservation guards are distinct features, not mailbox archives.
4. **Lifecycle services and UI**: centralize classification changes, activity renewal, restore and conversion to permanent. Expose bilingual grouped project navigation. Preserve old project identifiers and external tool names. Distinguish explicit human visits from background requests at the route/call-site level; do not use a blanket request middleware that renews on polling.
5. **Expiration worker**: use database-conditional transitions so concurrent workers cannot claim the same project. Recheck kind, state and activity at claim time. Restoration is allowed only before a purge claim succeeds. Reject new writes during purge. Delete related rows transactionally and use a durable, retryable cleanup queue for attachment files after commit.
6. **Retirement and release**: validate migration, restore, concurrency and isolation with targeted tests. Only then enable automatic permanent cleanup. Keep legacy archives and backups until separately authorized retirement; communicate exactly which storage has actually been reclaimed.

## State model

- `mailbox_type`: `permanent` or `temporary`.
- `retention_days`: default 30, positive bounded number.
- `last_activity_at`: UTC; null means no activity has been recorded and expiry calculation falls back to `created_at`. Converting to temporary or restoring explicitly establishes a new activity baseline.
- `mailbox_state`: `active`, `trash`, or `purging`. Manual archival remains independent.
- `trashed_at` and `purge_after`: populated when expiration enters trash; cleared on restoration. Deadline is seven days after trash entry, not seven days after the last access.
- No automatic worker is enabled by the schema foundation alone.

## Safety and acceptance

- A permanently classified project is never eligible for expiry or purge.
- Activity/restore races cannot recreate or partly purge a claimed project.
- Cleanup accepts only validated, system-owned storage paths and refuses symlinks/path traversal outside that storage; never pass `human_key` to file deletion.
- Deleting one project cannot delete another project's agents, threads, messages, shared attachments or audit history.
- A worker crash is recoverable; incomplete file cleanup is recorded and retried rather than reported as success.
- Background UI refresh and MCP empty inbox polling do not keep temporary projects alive indefinitely.
- Tests remain bounded locally; complete integration/slow coverage belongs in CI. Schema-only success is not end-to-end feature completion.

## Upgrade and acceptance guide

The working implementation now connects the schema, browser UI, real-activity renewal,
database activity history, and HTTP-service maintenance worker. It is no longer a schema-only change.

### Storage ownership and retained legacy data

- SQLite owns projects, agents, messages, recipient read/ack timestamps, attachment metadata,
  and mailbox events. Sending a message commits its activity event in the same transaction.
- Newly managed attachments, reservation projections, and build artifacts use
  `STORAGE_ROOT/mailboxes/<slug>/`. Reservation files are derived guard inputs, not a second database.
  Startup rebuilds active reservation projections from existing database rows.
- `STORAGE_ROOT/projects/` and the shared `.git` directory are **not** deleted by mailbox cleanup.
  They remain legacy archives with a separate, explicitly authorized retirement policy.
  Existing attachments referenced there remain available to database-based exports.
- Normal MCP sends, inbox reads, agent identity lookup, HTTP message display, and overseer sends
  do not open or initialize Git. `/mail/activity` reads database messages and events, including
  messages predating the event table. Legacy archive pages carry a notice linking to this view.
- `whois` now accepts `include_recent_activity` and `activity_limit`, returning `recent_activity`.
  Inbox-resource provenance is `activity`, not a fabricated Git commit. Update clients that
  explicitly requested the old commit-specific arguments.

### Upgrade

1. Stop the old server before upgrading. Take a consistent SQLite backup and retain the
   attachment directories; never copy a live database file without accounting for its WAL.
   Existing share/export tooling remains available; Git bundles alone do not back up new messages.
2. Restart the HTTP service normally. Schema additions are additive; existing projects stay permanent.
   Do not edit or replace `.env` for this migration.
3. If file-reservation hooks are installed, reinstall them after the upgraded server starts:
   `uv run mcp-agent-mail guard install <project_key> <code_repo_path> --prepush`.
   Existing generated hooks point at the old reservation directory; reinstalling points them at
   the database-derived inputs. This does not change the source repository's identity scheme.
4. Open the homepage at `/mail?lang=zh-CN#projects` or `/mail?lang=en#projects`.
   Its existing **Your Projects** section contains All / Permanent / Temporary / Recycle bin
   buttons that filter cards in place, plus search and inline lifecycle controls. The unified
   inbox remains above it. Saving returns to this same homepage section, not `/mail/projects`.
   The old `/mail/mailboxes` URL redirects to the homepage section.

### Manual acceptance

1. Confirm existing projects appear under permanent mailboxes.
2. Convert a disposable test mailbox to temporary. Check the displayed inactivity deadline;
   change its retention to any integer from 1 through 3650 days.
3. Open its project/message page, or explicitly select a message in the unified split view.
   Refresh the classification page and check that activity/deadline advanced. Automatic refresh,
   automatic initial message selection, health checks, and MCP inbox polling must not advance it.
4. Move the disposable temporary mailbox to the recycle bin. Ordinary project access is blocked;
   restore it and verify that messages remain available and its activity baseline restarts.
5. Move it to the recycle bin again and convert it to permanent. It must return to active state
   with no automatic expiration deadline.
6. Open its database activity view. Message history and mailbox changes must appear without
   requiring a Git repository. Do not wait 37 days locally: deadline boundaries and reclamation
   are verified with fixed-time, isolated tests instead of modifying real mailbox timestamps.

### Cleanup behavior and safety holds

- While the HTTP service runs, maintenance checks once per minute. A due temporary mailbox
  enters the recycle bin; its seven-day grace period starts when that transition actually occurs.
- The final reference check and `purging` claim are serialized against SQLite writes. A restore
  that wins before the claim prevents cleanup. Once destructive work has started, restore and
  conversion are blocked. Failed cleanup remains retryable, with a visible warning and server log.
- Only the claimed mailbox's managed directory and database relationships are reclaimed.
  `human_key` is never used as a deletion path. Traversal, symlinks, and junctions are rejected.
- Read-only legacy reconciliation checks stable message IDs, bodies, subjects, recipient lists,
  importance, acknowledgement requirements, thread IDs, and attachment metadata when present.
  It never silently imports or overwrites divergent records. Archive-only or inconsistent data
  pauses cleanup **before** the destructive claim, so the mailbox can still be restored.
- References retained by another mailbox also pause cleanup: this includes messages authored by
  the temporary project's agents and shared attachment paths. These cases deliberately require
  operator resolution rather than deleting another mailbox's history. They are not reported as
  successful purges.
- Backups and legacy archives have separate retention rules. This feature is not a secure-erasure
  promise for historical Git objects, SQLite free pages, filesystem snapshots, or external backups.

### Bounded verification

`tests/test_mailbox_lifecycle.py` covers deadlines, restore/conversion, polling vs human access,
Git-independent messaging, preserved legacy copies, failed/retried cleanup, cross-mailbox references,
concurrent purge claims, restore during preflight, write fences, path traversal, and execution of the
rendered Chinese/English controls in Node.js. Related migration, HTTP localization, attachment,
share/export, guard, and activity-touch regressions are tested separately. Full slow/performance
coverage remains a CI responsibility; these checks are not a claim of a complete green CI run.

### Git data retirement and legacy import

- Public Git archive browser routes and `projects adopt` / `doctor repair|backups|restore`
  commands are retired. Messaging, inbox/global deletion, activity, and retention reporting use
  the database; quota scans inspect managed attachments rather than retained Git copies.
- `archive import-legacy` is a transactional dry run. `--apply` first creates a non-Git ZIP
  backup, then imports atomically. Conflicts, unknown projects, unmatched copies, linked paths,
  and history-only content block import rather than overwrite originals. Empty directories are
  reported without creating mailboxes.
- `legacy_import_records` stores source digests, not message contents or credentials. Completed
  unchanged sources are skipped even after mailbox purge; changed sources require reconciliation.
  A full database reset removes this ledger too, but startup never automatically imports archives.
- Hard deletion uses lifecycle ownership checks and write fences. Reset/restore reject linked
  targets before resolving paths, protect registered source directories, and require ownership
  evidence. Purge/reset cannot delete their ownership database as part of managed storage.
- ZIP backups exclude `.git`, preserve existing ZIPs through exclusive creation, and use mode
  `0600` on POSIX. The archive preset contains credentials and must remain private. Restore retains
  pre-restore originals; it does not reconstruct Git history.

Deployment check on 2026-09-08: preflight and backed-up `--apply` both found zero importable
messages/identities and three empty/test legacy directories. SHA-256 checks of 25 retained
legacy/Git files found no changes. The resulting ZIP passed CRC validation, contained the database,
excluded Git metadata, and was confirmed ignored by source control. This is a verified empty
import, not evidence of a nonempty production migration.

Retirement cleanup is still in progress: unregistered legacy implementations in CLI/HTTP/storage
and their obsolete tests need further removal. Do not treat registration removal alone as complete
code retirement. Full CI and the latest homepage's complete real-browser acceptance remain pending.
