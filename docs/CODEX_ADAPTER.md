# Codex trusted-identity adapter

The Codex adapter is a local STDIO MCP server that proxies to a remote Agent Mail HTTP MCP server. It adds trusted, model-inaccessible identity metadata to each upstream tool call:

```text
Codex task -> local STDIO adapter -> remote Agent Mail HTTP MCP
```

The remote HTTP bearer token still authenticates the connection. It does not identify a Codex task. Adapter 0.1.1 uses Codex's per-request `params._meta.threadId` as the conversation identity and keeps a high-entropy client credential in the operating-system credential manager. A runtime `CODEX_THREAD_ID`, when actually supplied by the host, is also accepted; it is not required at startup. Conflicting IDs fail closed.

Codex can launch/reuse an MCP process without a thread environment. Initialization and tool discovery therefore work before any task ID exists. Each tool call resolves its own host metadata: no last-seen thread is cached, and neither `sessionId` nor model-controlled tool arguments substitute for `threadId`. Calls without either trusted source are rejected locally without forwarding. This matches Codex's [`with_mcp_tool_call_ids_meta`](https://github.com/openai/codex/blob/main/codex-rs/core/src/mcp_tool_call.rs) implementation. The boundary trusts the local Codex host owning STDIO, not arbitrary remote metadata.

## 1. Install the npm adapter / 安装

The distributable adapter is implemented in Node.js, **not a wrapper around Python**. End users need Node.js >=22.13 (Node 24 LTS recommended), but no Python, uv, repository checkout or compiler. Native credential-store modules are downloaded by npm for supported platforms. Windows Credential Manager and macOS Keychain are supported by the dependency; Linux requires a running, unlocked Secret Service (for example GNOME Keyring). Headless Linux without that service fails closed instead of silently losing identity after reboot.

Package name: `@kosukework/agent-mail-adapter`, executable: `agent-mail-adapter`. **The package has not been published to the public npm registry.** The scope is a proposed release name; a maintainer must own it or rename the package before publishing. Do not tell users that a bare registry `npx` command already works.

Install the versioned `.tgz` produced by `npm pack`:

```bash
npm install -g ./kosukework-agent-mail-adapter-0.1.1.tgz
agent-mail-adapter --version
```

Alternatively, npm can install the repository archive directly (no manual clone or chosen installation directory):

```bash
npm install -g https://github.com/KoSukeWork/mcp_agent_mail/archive/refs/heads/main.tar.gz
```

For reproducible company distribution, replace `refs/heads/main` with a reviewed full commit SHA containing adapter 0.1.1 or newer, or host the packed `.tgz` at your company artifact URL. The former `ddc2d4b` archive contains the broken 0.1.0 startup assumption. npm manages the installed program location. Do not run npm as administrator/root merely to work around a misconfigured npm prefix.

After a maintainer publishes an approved version, registry installation becomes:

```bash
npm install -g @kosukework/agent-mail-adapter@0.1.1
```

## 2. Configure a profile and Codex / 配置

Use one profile per endpoint, with **any number of services and arbitrary MCP names**. A profile called `company` lives at `~/.config/mcp-agent-mail/company.toml` on all platforms, including `%USERPROFILE%\.config\mcp-agent-mail\company.toml` on Windows:

```toml
url = "http://agent-mail.example.internal:8765/api/"
token = "REPLACE_WITH_SERVICE_TOKEN"
client_label = "Codex workstation"
request_timeout_seconds = 120
```

Only `url` is mandatory. Keep the file outside repositories; on Unix use mode 600. This file contains the service Bearer Token, not the adapter's generated client secret. It is parsed directly as TOML; it does not require Windows environment-variable setup. It does **not** load a repository `.env` or inherit another service's token implicitly.

If explicitly desired, replace `token` with `token_env = "EXISTING_TOKEN_VARIABLE"` and forward that variable using the MCP entry's `env_vars`. Using both fields, missing explicit variables, unknown fields, or a configured conversation ID is rejected. Never put `CODEX_THREAD_ID` into config files or invent `_meta.threadId` in tool arguments; Codex supplies task metadata.

Run a read-only installation/connection diagnostic before registering the MCP:

```bash
agent-mail-adapter --profile company --check
```

This verifies profile syntax, credential-store readability, MCP initialization and availability of `macro_start_session`. It does not create a client identity, register an Agent or claim a project, and works outside Codex. Success emits a small JSON result without credentials. It does not prove that Codex forwards its task ID; verify that with `identity_status` after a normal task startup.

Add the following to the existing user-level Codex config, preserving all unrelated entries:

```toml
[mcp_servers.company_agent_mail]
command = "agent-mail-adapter"
args = ["--profile", "company"]
startup_timeout_sec = 30
tool_timeout_sec = 150
```

If using a custom config location, replace `--profile company` with `--config` and an absolute TOML file path. The executable location still belongs to npm. If Windows cannot resolve an npm command shim, use the verified `node.exe` executable and installed JS entry path returned by `npm root -g`; do not guess a path or move the package manually. Restart Codex after first installing Node/npm so it picks up PATH.

Once the package has actually been published, the no-global-install alternative is:

```toml
[mcp_servers.company_agent_mail]
command = "npx"
args = ["-y", "@kosukework/agent-mail-adapter@0.1.1", "--profile", "company"]
startup_timeout_sec = 60
tool_timeout_sec = 150
```

Pin reviewed versions; don't silently update a credential-handling program with every MCP start. To update, install the next approved package/version through npm and restart that MCP connection. Profile files and OS credentials are separate from npm's installation/cache and survive package updates.

The MCP name and profile filename do not define the credential boundary: the normalized HTTP URL (scheme, host, port and path) does. Equivalent trailing slashes/default ports share the identity; distinct endpoints don't. Changing a hostname or switching `/api/` to `/mcp/` is a different identity boundary even if both reach the same server. Keep the old URL when switching from Python, or use web-admin recovery. Profiles pointing to the same URL intentionally share one local client identity; use separate OS users for independently owned client credentials on the same endpoint.

Replace or disable the old direct HTTP/Python entry for that endpoint to avoid duplicate tools. The Python developer adapter remains available, but is not shipped in the npm package. Its `.env` settings must be translated to the TOML fields above. On Windows, existing Python version-1 credentials are read from their exact OS-store target without exporting or overwriting them. Do not run Python and Node first enrollment concurrently during a migration.

Restart/reload the MCP configuration and test in a Codex task. The [official OpenAI MCP documentation](https://developers.openai.com/codex/mcp/) documents command/args and npm-based STDIO configuration.

### 给 AI 的安装配置提示词

```text
请配置 Codex 的 Agent Mail MCP，保留其他配置，不打印 Token。
MCP 名称：<名称>；Profile：<profile>；服务 URL：<完整 /api/ 地址>；Token：<填写>。
确认 Node >=22.13，用 npm install -g 安装：
https://github.com/KoSukeWork/mcp_agent_mail/archive/refs/heads/main.tar.gz
将 url、token 写入 ~/.config/mcp-agent-mail/<profile>.toml；对应 MCP 使用
command="agent-mail-adapter"、args=["--profile","<profile>"]，启用并设置启动/工具超时为30/150秒。
替换同一服务的旧配置，执行 agent-mail-adapter --profile <profile> --check 验证，提醒重启 Codex。
```

### Maintainer verification and packing

```bash
npm ci --ignore-scripts
npm run check:adapter
npm run test:adapter
uv run --python 3.14 pytest -q --no-cov tests/test_codex_adapter.py
npm pack --dry-run
npm pack --pack-destination dist
```

`files` is an explicit npm allowlist: only the adapter runtime, documentation, license and package metadata ship, not server source, tests, Docker tarballs or config/credential files. The root package retains the existing web-asset development scripts. No install/prepare script runs a Python build. Never overwrite an existing release tarball; bump the package and CLI versions together. Publication is a separate maintainer action, not part of installation or testing.

## 3. Start or resume a task identity

Use the normal startup macro:

```text
macro_start_session(
  human_key=<canonical absolute project key>,
  program="codex",
  model=<current model>,
  task_description=<current assignment>
)
```

On the first call from a Codex task, the macro creates and binds an Agent. Later calls from the same task reconnect that Agent, including after restarting the MCP transport. A different Codex task gets a separate binding. No reusable Agent registration token is returned to the model.

## Migrating an existing Agent such as `SageBay`

An Agent created through the legacy direct HTTP connection is not automatically owned by the new trusted conversation. Migrate it once through the adapter-backed MCP connection:

```text
macro_start_session(
  human_key=<the same project key>,
  program="codex",
  model=<current model>,
  agent_name="SageBay",
  registration_token=<SageBay's Agent registration token>
)
```

The Agent registration token is the one returned when `SageBay` was created; it is not the HTTP bearer token. After a successful migration, future adapter-backed tasks should omit both `agent_name` and `registration_token`. Transitional legacy clients may continue using the server's verification-only form of the old credential.

If the old Agent registration token is unavailable, it cannot be recovered because the server stores only its verification hash. Either let the trusted task create a new Agent, or use the explicit administrator identity-recovery workflow.

## Identity transfer and web administrator recovery

### Identifying client computers

Adapter 0.1.2 automatically reports the OS hostname. Keep `client_label` as a human-friendly note such as `张三·开发机`; no hostname/IP configuration is needed. The administrator client list and pending recovery requests display this note, the reported computer name, a short client ID (hover for the full ID), and the most recent server-observed source IP. Existing last-authenticated timestamps remain visible on the client list.

Computer names are client-reported and are not proof of identity. Source IP comes only from the HTTP request's transport address, never MCP metadata or directly parsed forwarding headers. A reverse proxy/NAT may make multiple computers show the same IP. Configure the ASGI host's trusted proxy handling for the actual proxy addresses if original addresses are required; do not trust arbitrary forwarding headers. Neither hostname nor IP participates in authorization or same-client transfer checks.

Deploy the updated server image and upgrade npm clients to 0.1.2, then reload MCP. Existing credentials and bindings are retained; the database adds display fields automatically. Older clients show no computer name until upgraded, and HTTP IP information updates on successful identity authentication. In-memory/STDIO authentication without an HTTP address does not erase the last observed IP.

Call `recover_agent_identity` or `request_agent_identity_transfer` explicitly when a different task needs an existing Agent. If its active owner belongs to the same authenticated client principal, the transfer completes immediately (`status: transferred`), without browser/native confirmation. The old task loses access, the generation advances, and an audit event is recorded. Repeating the request from the new owner is safe. Ordinary startup still resumes only the current task's own binding; it never silently takes another task's Agent.

“Same client” means the persisted client identity and secret, not the MCP/profile name, client label, or shared HTTP bearer token. A different installation/credential, missing active ownership, or an administrator-revoked ownership requires web recovery. A revoked client cannot transfer; a destination already owning a different Agent must release that identity explicitly first. A former owner may explicitly request a same-client transfer back, but ordinary reconnect never reclaims ownership. Administrator-revoked task bindings require approval.

This policy is server-side: deploy the updated server image. No npm adapter update is required.

1. In the destination Codex task, call `recover_agent_identity(project_key=..., agent_name=...)` through the adapter. The task must not already own a different active Agent in that project. If same-client transfer returns `transferred`, it is complete: skip the approval steps. Otherwise it receives a request ID with `pending`; continue below.
2. Sign in to `/mail` with the web administrator account and open **Identity administration** (`/mail/admin/identity`). Match the request ID, project, Agent and requesting client with the requester. Client labels are self-declared.
3. Choose **Approve recovery / transfer**, enter the current administrator password, and confirm. The old owner immediately loses access; the Agent and its mail remain intact. Requests expire after the configured confirmation TTL (default five minutes); submit again if expired.
4. In the destination task, check `identity_confirmation_status` and then resume with `macro_start_session`. The target is captured from the authenticated request; administrators never need to type a conversation ID or handle a client secret.

The same page manages client revocation, restoration, individual binding revocation and audit history. Restoring a client re-enables authentication only; recover its Agent explicitly to establish a new binding. Requests by a revoked client are cancelled.

The web administrator can change the username/password on this page. A PBKDF2-SHA256 password hash is persisted in the database, overrides the initial environment credentials, and survives container updates. All browser sessions are signed out. Keep `MAIL_UI_SESSION_SECRET` configured.

For emergency password recovery only, run on the server:

```bash
docker compose -f docker-compose.yaml exec agent-mail \
  /app/.venv/bin/mcp-agent-mail identity reset-web-admin
```

The command prompts for the new username and password and invalidates old sessions without restarting the service. Normal identity management requires no shell access or `identity grant-admin` step.

## Troubleshooting

- Startup `Codex must supply CODEX_THREAD_ID` error: update npm adapter 0.1.0 to 0.1.1 or newer and reload MCP. This was an adapter startup bug; editing the server or hard-coding a task ID is not a fix.
- Missing `_meta.threadId` on a tool call: the host did not supply per-request task metadata or a runtime task ID. Update/check the Codex host. `--check` verifies connectivity only, not host task metadata; verify `identity_status` in an actual Codex task. A rejected call does not kill the adapter.
- HTTP 401/403: verify `token` (or the explicit `token_env` variable) in the selected TOML profile and the server's RBAC role.
- `UNTRUSTED_CONVERSATION_CONTEXT`: confirm Codex is connected to the STDIO adapter entry, not the old direct HTTP entry.
- Credential-store error: the adapter intentionally fails instead of writing its client secret to a model-visible config file. On Windows, ensure Credential Manager is available for the user running Codex.
- Wrong service or duplicate tools: give every service a distinct MCP name and TOML profile, and disable obsolete direct/Python entries.
