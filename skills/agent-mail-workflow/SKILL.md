---
name: agent-mail-workflow
description: Coordinate coding agents through one or more MCP Agent Mail servers by selecting the correct trust boundary, establishing project and agent identity, reserving files, reading and acknowledging messages, maintaining threads, and sending actionable handoffs. Use when agents collaborate through Agent Mail, or when the user asks to create or use an Agent Mail project, coordinate agents, exchange handoffs, or manage Agent Mail file reservations. Do not use for ordinary human email.
---

# Agent Mail Workflow

Use Agent Mail as the coordination and audit layer for collaborating coding agents. The MCP server supplies the operations; this skill supplies the workflow. Preserve the user's task scope and do not create coordination traffic unrelated to the active work.

Tool prefixes vary by MCP configuration. Refer to tools by their logical names, such as `macro_start_session`, `fetch_inbox`, and `send_message`, while treating the MCP server namespace that exposes each tool as part of its identity.

## Select services before any write

Any number of Agent Mail MCP servers may be connected, and their purposes are deployment-specific. Do not assume a fixed count, a shared/private split, or meaning from a server's name. Treat every server namespace as an independent trust, identity, and data boundary even when several expose identical tools or point to related infrastructure.

Build or recover a routing profile for each relevant server using only available, non-secret context:

- MCP server namespace and endpoint identity;
- intended audience or operators;
- projects and agent identities already present there;
- permitted data or confidentiality boundary;
- workflow purpose and any repository/user routing rules.

Do not persist credentials in the routing profile.

Before `ensure_project`, `macro_start_session`, registration, reservation, or messaging:

1. Identify the Agent Mail server namespaces available to the current task.
2. Prefer an explicit server named by the user or repository instructions.
3. Otherwise, continue on the server that already owns the referenced project, identity, message, or thread.
4. Otherwise, select the server whose known routing profile matches the task's project, participants, purpose, and data boundary.
5. If more than one server remains plausible, make only read-only discovery calls and ask one concise routing question before writing.

A task may intentionally use zero, one, or several servers. For every operation, keep its complete scope together:

```text
server namespace + project key + agent identity + registration token + message/thread ID
```

Never take an identity, registration token, message ID, thread ID, contact relationship, or reservation returned by one server and use it against another. Project records are server-local: the same `human_key` on different servers denotes distinct mail projects unless an external system explicitly establishes otherwise.

Multi-server rules:

- Use reservations on every service whose participants rely on those reservations for the active edit scope; do not assume a reservation propagates to another server.
- Qualify ambiguous references as `<server namespace>/<project>/<thread>` when reporting or reasoning about them.
- Fetch only the task-relevant server inboxes. For an intentionally combined view, query servers separately and label every result with its source.
- Do not automatically copy, mirror, summarize, or forward content between servers. Any cross-server transfer is a disclosure action; verify that the task authorizes the source, destination, recipients, and content.
- Switching or adding a server starts a separate Agent Mail session on that server. Run its startup workflow instead of carrying server-local state across.
- Keep per-server failures isolated. A failure on one server does not authorize retrying the operation on another destination.
- A transport bearer token configured for an MCP connection and an Agent Mail registration token are different credentials. Never infer that credentials are reusable across servers.

## Establish the session

After selecting each task-relevant service, prefer one `macro_start_session` call on that server at the beginning of its Agent Mail workflow. It ensures the project, creates or resumes an agent identity, optionally reserves edit paths, and returns a recent inbox snapshot. Do not run this macro on every connected Agent Mail server merely because each exposes it.

Pass:

- `human_key`: the shared absolute path-like project key.
- `program`: the current coding-agent client, such as `codex`.
- `model`: the current model identifier.
- `task_description`: a concise description of the active assignment.
- `file_reservation_paths`: only the files or narrow globs the task is expected to edit, when known.
- `file_reservation_reason`: the issue ID or a short task label.

Project identity depends on the exact key:

- When collaborators share the same checkout path, use the absolute repository root.
- When collaborators run on machines with different paths, reuse the canonical project key already agreed by the team. It may be an absolute path-like opaque key that does not exist locally.
- Never invent a new slug or switch between path spellings for the same project.

Omit `agent_name` unless a stable identity is intentionally being resumed. Resuming an existing identity requires its registration token. Treat registration tokens as secrets: keep them out of repositories, messages, logs, and final responses. If the token is unavailable, register a new identity instead of impersonating the old one.

If `macro_start_session` is unavailable, use this granular fallback:

1. `ensure_project(human_key=<shared project key>)`.
2. `register_agent(project_key=<same key>, program=<client>, model=<model>, task_description=<task>)`.
3. Reserve planned edit paths when the task writes files.
4. Fetch the inbox with message bodies.

`ensure_project` is idempotent and safe to call again for the same key.

## Read before acting

The startup macro returns inbox metadata without full bodies. Follow it with:

```text
fetch_inbox(
  project_key=<project key>,
  agent_name=<registered identity>,
  unread_only=true,
  include_bodies=true
)
```

Check the inbox at meaningful boundaries: before editing shared surfaces, after completing a substantial step, and before handoff. Do not busy-poll.

For a reply or continuation, recover the relevant context before acting:

- Prefer the thread resource with `include_bodies=true` when the complete visible thread is needed.
- Use `macro_prepare_thread` or `summarize_thread` when a compact digest is sufficient.
- Remember that summaries are not the full thread and messages never contain the sender's hidden model context automatically.

Call `acknowledge_message` after actually reading an `ack_required` message. A plain inbox fetch does not mark it read or acknowledged.

## Coordinate edits

Reserve files before editing when other agents may work in the same project:

- Use exact paths or the narrowest practical globs.
- Include a meaningful reason, preferably the shared issue or thread ID.
- Treat conflicts as coordination signals. Read the conflicting agent's message/thread or contact them; do not silently overwrite their work.
- Renew a reservation when legitimate work outlasts its lease.
- Release reservations promptly when the edit scope is abandoned or completed.

Reservations are advisory and do not authorize reverting, deleting, or replacing another agent's changes.

Do not use `force_release_file_reservation`, destructive project/agent deletion, message purging, or archival operations without explicit user authorization for that action.

## Keep communication coherent

Discover recipients through the project agent directory or `whois`; do not guess agent names.

Use a stable `thread_id` for one work item. Prefer an existing issue ID when available. Continue discussions with `reply_message`, which preserves the thread and reply relationship. A `thread_id` groups messages but does not copy earlier messages into the new body.

Send only useful coordination messages. Avoid broadcasts unless the active task genuinely requires notifying every registered agent.

Every request to another agent should state:

- the outcome needed;
- relevant files, symbols, or artifacts;
- constraints and decisions already made;
- expected verification;
- the requested next action.

If contact policy blocks a message, follow the returned contact workflow. Do not claim delivery when the server reports `CONTACT_REQUIRED`; approve/contact first, then send the original payload again.

## Hand off completed or partial work

Before handoff, perform verification appropriate to the task. Then reply in the existing thread or send a message with a stable thread ID using this compact structure:

```markdown
## Status
Completed | Partial | Blocked

## Work completed
- Concrete changes and decisions

## Files / artifacts
- Paths, commits, PRs, or generated outputs

## Verification
- Commands run and results

## Remaining work
- Unfinished items, blockers, risks, and exact next step
```

Do not send vague messages such as “done” or “continue from here” when the receiver would need unstated local context. Include enough information for a fresh agent session to proceed without guessing.

After the handoff:

1. Release owned file reservations.
2. Acknowledge any handled ack-required messages.
3. Check once for a directly relevant late reply when the workflow requires it; do not wait indefinitely unless the user asked for monitoring.

## Authorization boundary

Agent Mail does not broaden the user's authorization. It may communicate and reserve files inside the active collaboration scope, but it must not assign materially different work, expose secrets, broadcast sensitive information, or perform destructive administrative operations without the authority required for those actions.
