# Codex trusted-identity adapter

The Codex adapter is a local STDIO MCP server that proxies to a remote Agent Mail HTTP MCP server. It adds trusted, model-inaccessible identity metadata to each upstream tool call:

```text
Codex task -> local STDIO adapter -> remote Agent Mail HTTP MCP
```

The remote HTTP bearer token still authenticates the connection. It does not identify a Codex task. The adapter separately uses Codex's stable `CODEX_THREAD_ID` as the conversation identity and keeps a high-entropy client credential in the operating-system credential manager.

## 1. Create one env file per Agent Mail service

An env file contains only that service's static connection settings:

```dotenv
MCP_AGENT_MAIL_URL=http://agent-mail.example.internal:8765/api/
MCP_AGENT_MAIL_BEARER_TOKEN=replace-with-that-service-bearer-token
MCP_AGENT_MAIL_CLIENT_LABEL=Codex workstation
```

The label is optional. Do not put `CODEX_THREAD_ID` in this file: Codex supplies it dynamically for the current task. Keep the env file outside a Git repository or ensure it is ignored.

Multiple services are supported. Give each service its own env file and MCP server entry. The adapter scopes its client identity to the normalized upstream URL, so identities and credentials never cross service boundaries.

If a bearer token already lives in a local environment variable, the adapter can reuse it without copying the value into the env file. Add the variable name to the Codex server's `env_vars` list and pass `--bearer-token-env-var VARIABLE_NAME` in `args`. The adapter fails closed when an explicitly named variable is unavailable.

## 2. Configure Codex to start the adapter

Codex supports local STDIO MCP servers configured by command and arguments. Add an entry to `%USERPROFILE%\.codex\config.toml` on Windows or `~/.codex/config.toml` on Linux/macOS.

Windows example using this repository's uv project:

```toml
[mcp_servers.company_agent_mail]
command = "uv"
args = [
  "run",
  "--project",
  'Q:\Temp\Work\mcp_agent_mail',
  "python",
  "-m",
  "mcp_agent_mail",
  "codex-adapter",
  "--env-file",
  'C:\Users\YOUR_NAME\.config\mcp-agent-mail\company.env',
]
startup_timeout_sec = 20
tool_timeout_sec = 120
```

Linux/macOS example:

```toml
[mcp_servers.company_agent_mail]
command = "uv"
args = [
  "run",
  "--project",
  "/opt/mcp_agent_mail",
  "python",
  "-m",
  "mcp_agent_mail",
  "codex-adapter",
  "--env-file",
  "/home/YOUR_NAME/.config/mcp-agent-mail/company.env",
]
startup_timeout_sec = 20
tool_timeout_sec = 120
```

Replace or disable the old direct HTTP entry for the same Agent Mail service. Keeping both entries enabled exposes two copies of every tool, but only the adapter-backed copy has trusted task identity.

Restart Codex after changing `config.toml`. The ChatGPT desktop app, Codex CLI, and IDE extension share the MCP configuration for the same Codex host. See the [official OpenAI MCP documentation](https://learn.chatgpt.com/docs/extend/mcp) for the current STDIO configuration fields.

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

## Troubleshooting

- `CODEX_THREAD_ID` error: the adapter was launched outside a Codex task, the Codex host is too old, or the variable was explicitly removed from the child process. Do not hard-code one thread ID for multiple tasks.
- HTTP 401/403: verify the bearer token in the selected service's env file and the server's RBAC role.
- `UNTRUSTED_CONVERSATION_CONTEXT`: confirm Codex is connected to the STDIO adapter entry, not the old direct HTTP entry.
- Credential-store error: the adapter intentionally fails instead of writing its client secret to a model-visible config file. On Windows, ensure Credential Manager is available for the user running Codex.
- Wrong service or duplicate tools: give every service a distinct MCP name and env file, and disable obsolete direct entries.
