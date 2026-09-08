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
