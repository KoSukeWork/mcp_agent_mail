# MCP conversation identity and mailbox credential plan

Status: proposed for review  
Scope: MCP client identity, per-conversation Agent binding, permanent and temporary mailbox lifecycle, identity transfer, and recovery  
Last updated: 2026-09-10

## 1. Purpose

MCP Agent Mail must not depend on a model remembering an Agent registration token in conversation history. Conversation history can be compacted, replaced, exported, or lost; it is also the wrong security boundary for reusable credentials.

The target system must support one authenticated MCP client hosting many independent conversations, with each conversation acting as a distinct long-lived Agent where appropriate:

```text
Authenticated MCP client
└── Project / mailbox
    ├── Conversation A -> Agent CoralBeacon
    ├── Conversation B -> Agent BlueLake
    └── Conversation C -> Agent GreenCastle
```

The server must resolve the Agent from trusted client and conversation context. Agent names are labels, not proof of identity. The model must neither receive nor retain reusable identity secrets.

## 2. Decisions already approved

1. A project is a mailbox. Permanent or temporary is a mailbox-level lifecycle choice.
2. An Agent is a durable participant inside a mailbox, not a chat session.
3. An MCP session is a transient transport connection, not a durable identity.
4. One MCP client may host many conversations and many Agent identities.
5. Each conversation needs its own stable binding identifier.
6. Agent names are only for display and selection; a name alone can never recover an identity.
7. Each Agent has at most one active conversation owner.
8. Reconnecting the same conversation preserves its binding.
9. A different conversation that takes over an Agent revokes the old conversation binding.
10. Recovery is authorized by the currently bound identity or an identity administrator.
11. Permanent mailbox identity bindings do not expire because of inactivity.
12. Temporary mailbox identities follow the parent mailbox lifecycle.
13. Identity lookup, reconnect, health checks, and background polling do not renew a temporary mailbox.
14. Credentials are managed by the MCP client credential layer and MCP server, never by conversation memory.
15. User confirmation should use native MCP elicitation when available and a browser confirmation flow as the compatibility path.

## 3. Current gaps

The current implementation already persists `Agent.registration_token` in the database and has session/window identity helpers, but it does not satisfy this contract:

- normal registration responses can expose a reusable token to the transcript;
- later calls may still require the model to provide that token;
- MCP session binding is transport-session scoped and is not sufficient for many conversations on one client;
- `WindowIdentity` lacks an authenticated client principal and an Agent foreign key;
- a window UUID alone is not strong authentication and may be supplied or copied incorrectly;
- window identity expiry is currently independent of permanent/temporary mailbox lifecycle;
- there is no binding generation fence for invalidating an old conversation after takeover;
- there is no complete administrator recovery path that preserves the original Agent ID without revealing the old credential;
- clients without native elicitation need a secure confirmation mechanism that does not turn a model-provided string into authorization.

The existing session/window behavior is therefore migration input, not the final security model.

## 4. Identity hierarchy

### 4.1 MCP client principal

The client principal proves which trusted MCP client installation, OAuth subject, or service client is connected.

For HTTP transports, derive the principal only from validated OAuth/JWT claims, such as issuer, subject, client ID, and granted scopes. Do not trust an ordinary header that a caller can set directly.

For local stdio clients, the MCP adapter creates a client key pair:

- the private key remains in the MCP client's secure credential store or operating-system credential manager;
- the server stores only the public key and its fingerprint;
- connection setup proves possession of the private key;
- the private key is never placed in `.env`, tool arguments, prompts, logs, or transcripts.

Initial local client enrollment is an operator bootstrap action. After enrollment, routine identity recovery and transfer occur through MCP management tools.

### 4.2 Conversation binding identity

Each conversation has a stable opaque identifier supplied by the MCP host or adapter. In Pi, the adapter may derive the binding from the durable Pi session identity, but the server must not trust an unsigned environment value by itself.

Requirements:

- the same conversation retains the identifier after compaction and reopen;
- a fork or new conversation receives a new identifier;
- concurrent conversations never share an identifier;
- the model cannot read, override, or choose the identifier;
- the identifier is sent in trusted MCP request metadata, not as a normal tool argument;
- the server stores only a keyed hash of the identifier;
- the identifier is meaningful only together with its authenticated client principal and project.

The lookup key is:

```text
(client_principal_id, project_id, conversation_binding_hash) -> agent_id
```

Putting `conversation_binding_id` in a model-controlled JSON argument is explicitly forbidden. The adapter must inject it out of band, for example through MCP request `_meta` handled by trusted middleware.

### 4.3 Agent identity

The Agent owns mailbox-visible state:

- name and public profile;
- inbox and recipient state;
- read and acknowledgement state;
- contacts and routing policy;
- reservations and other attributable actions;
- audit history.

Changing conversations, MCP sessions, client devices, or credentials must not create a new Agent when the operation is a legitimate reconnect, transfer, or recovery.

### 4.4 MCP transport session

An MCP session is used as a short-lived performance and authorization cache only. It may remember the already resolved Agent for the lifetime of that connection, but every protected operation remains rooted in the persistent client/conversation binding and its current generation.

If one MCP connection multiplexes several conversations, session-level current-Agent state is forbidden. Trusted per-call conversation metadata is mandatory.

## 5. Persistent data model

Add the following models to the existing model and migration layers rather than creating a parallel identity subsystem.

### 5.1 `McpClientPrincipal`

Suggested fields:

- `id`
- `principal_type`: `oauth`, `local_key`, or `service`
- `issuer`
- `subject_hash`
- `client_id_hash`
- `public_key`
- `public_key_fingerprint`
- `display_label`
- `status`: `active` or `revoked`
- `scopes`
- `created_at`
- `last_authenticated_at`
- `revoked_at`
- `revocation_reason`

OAuth and local-key uniqueness rules must prevent the same authenticated principal from being enrolled ambiguously.

### 5.2 `AgentConversationBinding`

Suggested fields:

- `id`
- `project_id`
- `agent_id`
- `client_principal_id`
- `conversation_binding_hash`
- `generation`
- `status`: `active` or `revoked`
- `created_at`
- `last_seen_at`
- `revoked_at`
- `revocation_reason`
- `transferred_from_binding_id`

Required constraints:

- one binding for a given client, project, and conversation hash;
- at most one active binding for a given project and Agent;
- a positive monotonically increasing generation;
- foreign-key and project-consistency checks preventing a binding from pointing to an Agent in another project.

Mailbox `trash` is represented as an effective `suspended` binding state derived from the parent Project. It should not require a fragile bulk update of every binding.

### 5.3 `IdentityConfirmationRequest`

Browser and elicitation confirmation requests need persistent, auditable, single-use state:

- `id`
- `action`: transfer, recover, revoke, or rotate
- `requesting_principal_id`
- `project_id`
- `agent_id`
- `source_binding_id`
- `target_conversation_binding_hash`
- `expected_binding_generation`
- `challenge_hash`
- `status`: pending, approved, denied, expired, or consumed
- `created_at`
- `expires_at`
- `decided_at`
- `decided_by_principal_id`

No reusable Agent or client credential is stored in this record.

### 5.4 Agent service credentials

Interactive conversations no longer need a model-visible Agent token. If non-conversation automation still requires a service credential, store only:

- a high-entropy credential hash;
- credential version;
- creation and rotation timestamps;
- revocation state.

Do not store a retrievable plaintext service credential. A lost service credential is rotated, not revealed.

### 5.5 Audit events

Use the existing mailbox event mechanism for identity events where practical:

- `identity_created`
- `identity_bound`
- `identity_reconnected`
- `identity_transfer_requested`
- `identity_transferred`
- `identity_binding_released`
- `identity_binding_revoked`
- `client_principal_revoked`
- `service_credential_rotated`

Audit payloads contain IDs, generations, timestamps, and reasons, never secrets or raw conversation identifiers.

## 6. Agent resolution protocol

For every protected MCP operation:

1. Authenticate the MCP client principal.
2. Obtain the trusted conversation binding metadata from middleware.
3. Resolve the requested project.
4. Look up the persistent binding for principal, project, and conversation.
5. Verify that the client principal is active.
6. Verify that the binding is active and its generation is current.
7. Verify that the Agent belongs to the project and is not explicitly revoked.
8. Verify the mailbox lifecycle state.
9. Bind the short-lived MCP session cache to that exact binding and generation.
10. For writes, repeat the binding-generation and mailbox-state checks in the same database transaction immediately before commit.

Agent names may appear in the result for user recognition but do not participate in authorization.

## 7. Single-owner and transfer semantics

### 7.1 Same conversation reconnect

If the client principal, project, and conversation hash match an active binding:

- keep the same Agent;
- keep the same generation;
- replace only the transport session cache;
- update binding `last_seen_at`;
- do not rotate credentials;
- do not count the reconnect as temporary-mailbox activity.

### 7.2 Different conversation requests the same Agent

A different conversation cannot silently share the Agent. It creates an identity transfer request and requires user approval through native elicitation or the browser confirmation flow.

On approval, one database transaction must:

1. lock the Agent and current active binding;
2. verify the confirmation request is pending, unexpired, and bound to the expected generation;
3. verify the mailbox is eligible for transfer;
4. revoke the old binding;
5. increment the Agent binding generation;
6. create or activate the new binding;
7. consume the confirmation request;
8. write an audit event.

The old conversation's next call returns:

```text
IDENTITY_SESSION_REVOKED
This Agent identity was transferred to another conversation.
```

It cannot automatically take the identity back. A new transfer requires a new explicit approval.

### 7.3 In-flight write fence

Checking authorization only at call entry is insufficient. An old conversation may begin a write immediately before transfer and commit afterward.

All identity-attributed writes must compare the request's binding generation with the current generation inside the same transaction that performs the write. A mismatch aborts the operation before commit.

Cancellation or closing the old transport is not a security control.

### 7.4 Release without deletion

An active conversation may release its binding. Release revokes the binding but preserves the Agent, mailbox, messages, contacts, read state, and audit history. The unowned Agent may later be rebound by an authorized client or administrator.

## 8. User confirmation and browser compatibility

### 8.1 Confirmation policy

Use native MCP elicitation when the client supports it. For clients without compatible elicitation UI, use a browser popup controlled by the MCP client adapter.

The browser flow is a compatibility implementation of the same server-side confirmation request. It must not become weaker authorization.

### 8.2 Browser flow

1. The MCP identity tool creates a short-lived `IdentityConfirmationRequest`, normally valid for five minutes.
2. The server emits a structured out-of-band confirmation instruction to the MCP adapter. The model-facing tool result contains at most a non-secret pending request ID.
3. The MCP adapter opens the user's local default browser. A remote MCP server must not attempt to open a browser on the server host.
4. The browser loads an authenticated confirmation page showing:
   - project/mailbox;
   - mailbox type and state;
   - Agent name and ID;
   - old conversation/client label;
   - new conversation/client label;
   - exact effect of approval;
   - whether a credential rotation is included.
5. Approval uses an authenticated POST with CSRF protection. A GET request can never approve an action.
6. The server atomically verifies request expiry, client/admin authority, expected generation, mailbox state, and single-use status.
7. Approval consumes the request and executes the transfer or recovery transaction.
8. The browser displays success or failure and may close itself.
9. The pending MCP call may resume if the transport supports waiting; otherwise the client polls `identity_confirmation_status` using the non-secret request ID.

### 8.3 URL and challenge safety

- Do not place a reusable Agent credential in the URL.
- Prefer an authenticated browser session plus a non-secret request ID.
- If a one-time browser challenge is required, store only its hash server-side and keep it out of model-visible results and server access logs.
- Use HTTPS for remote confirmation origins.
- A loopback confirmation origin is allowed only when the MCP adapter and browser are on the same trusted machine.
- Apply `SameSite=Strict`, short expiry, CSP, origin checks, CSRF tokens, and no-store cache headers.
- Popup blocking must not cause automatic approval. The adapter may show a trusted “Open confirmation page” control.
- Closing the page, denial, timeout, server restart, or generation change leaves the old binding unchanged.
- Double submission is idempotent: only the first valid decision can consume a request.

### 8.4 Authorization paths

An identity transfer or recovery may be approved by:

- the currently bound identity through its authenticated client; or
- a principal with `mailbox.identity.admin` authority.

Agent name, possession of a conversation transcript, a model-provided confirmation string, or an unverified window UUID is never sufficient.

## 9. Permanent mailbox rules

Permanent mailbox identity is durable until explicit revocation or mailbox deletion.

### 9.1 Creation

- An unbound conversation may explicitly create a new Agent.
- Creation stores the new Agent and conversation binding atomically.
- The response contains public profile and binding status only.
- No reusable Agent secret is returned to the model.

### 9.2 Reconnect

- The same conversation reconnects to the same Agent automatically.
- MCP server restarts do not remove the binding.
- Conversation compaction does not affect the binding.
- The binding does not expire after 30 days or any inactivity interval.

### 9.3 Inactivity and stale Agent handling

Presence and identity validity must be separate concepts:

- inactivity may mark an Agent offline or hide it from active-roster defaults;
- automatic stale-Agent jobs cannot revoke identity, credentials, or conversation bindings;
- returning through a valid binding reactivates the existing Agent ID;
- explicit deregistration or hard deletion does revoke bindings.

### 9.4 Transfer and recovery

- A new conversation requires explicit transfer approval.
- An administrator can recover an unowned or inaccessible Agent without changing its Agent ID.
- Recovery revokes the old binding and increments generation.
- Rotate service credentials when compromise is suspected; a normal same-client transfer need not rotate them.

## 10. Temporary mailbox rules

Temporary mailboxes use the same identity security model, but identity availability is controlled by the parent mailbox lifecycle.

### 10.1 Active temporary mailbox

While `mailbox_state == "active"`:

- Agent creation, reconnect, release, and approved transfer are allowed;
- each Agent still has one active conversation owner;
- bindings have no independent shorter TTL;
- effective binding lifetime is bounded by the mailbox retention and cleanup lifecycle.

An Agent binding must not fail before an otherwise active temporary mailbox simply because a separate window-identity timer elapsed.

### 10.2 Activities that do not renew retention

The following do not update Project effective activity:

- authenticating the MCP client;
- checking identity status;
- resolving a conversation binding;
- reconnecting the same conversation;
- listing Agents or bindings;
- polling a confirmation request;
- health checks;
- background refreshes;
- automatic inbox polling with no effective user/message action.

Existing effective-activity rules remain authoritative: explicit human mailbox viewing and actual message/read/first-ack operations may renew the mailbox.

### 10.3 Expiry into trash

When a temporary mailbox enters `trash`:

- Agent rows and bindings remain for recoverability;
- effective binding state becomes `suspended` through the Project state check;
- existing MCP sessions cannot continue reading or writing;
- no new Agent, reconnect, transfer, or recovery can bypass trash;
- protected operations return the existing `MAILBOX_UNAVAILABLE` and restore guidance;
- background identity traffic cannot restore or renew the mailbox.

The server does not rotate or destroy credentials at trash entry because the mailbox remains recoverable for seven days.

### 10.4 Restore from trash

On explicit restore before purge:

- preserve Project ID, Agent IDs, messages, contacts, and binding generations;
- make existing bindings eligible again;
- let the original conversation reconnect automatically;
- require explicit transfer if a different conversation wants the Agent;
- update the mailbox activity baseline according to the existing explicit restore policy;
- do not automatically bind the administrator who performed the restore to an Agent.

### 10.5 Purge

Permanent cleanup must follow the existing safe lifecycle claim and path checks, with identity revocation included:

1. claim the Project for purge and activate the mailbox write fence;
2. invalidate all binding generations before deleting mutable mailbox data;
3. reject all new identity operations;
4. remove conversation bindings, Agent identity records, credential hashes, and window-identity remnants as part of database cleanup;
5. remove managed mailbox storage and attachments only after existing ownership, symlink/junction, source-path, and shared-reference checks pass;
6. preserve retained legacy archives, shared `.git`, source repositories, external backups, and the legacy import anti-resurrection ledger.

After successful purge, an old conversation binding cannot silently recreate or reclaim the removed Agent. Recreating the same human key later creates a new Project ID and requires explicit new identity creation.

### 10.6 Temporary to permanent

Converting a temporary mailbox to permanent:

- preserves Project and Agent IDs;
- preserves active bindings and generations;
- removes the mailbox expiration deadline;
- makes bindings durable without rotation or re-registration.

### 10.7 Permanent to temporary

Converting a permanent mailbox to temporary:

- preserves existing identities and bindings;
- computes retention from the approved effective-activity baseline;
- makes binding availability conditional on the temporary mailbox lifecycle;
- does not create separate per-Agent expiry deadlines.

## 11. MCP identity tools

The final tool surface should be explicit and must not overload Agent names as authentication.

### 11.1 `ensure_agent_identity`

- resolves the trusted current conversation binding;
- returns the existing Agent when already bound;
- may explicitly create a new Agent when the conversation is unbound;
- never claims an existing Agent by name;
- never returns a reusable secret.

### 11.2 `identity_status`

Returns public Agent data, mailbox type/state, binding generation, effective binding state, and permitted next actions. It returns no credential or raw conversation identifier.

### 11.3 `list_owned_agent_identities`

Lists identities the authenticated client principal may manage in the Project. Names are presentation fields only. Unrelated client principals cannot enumerate private binding details.

### 11.4 `request_agent_identity_transfer`

Creates a pending confirmation request for a different conversation to take over an Agent. It does not change the active binding until approval.

### 11.5 `identity_confirmation_status`

Returns pending, approved, denied, expired, consumed, or superseded using a non-secret request ID.

### 11.6 `release_agent_identity`

Releases the current conversation binding without deleting the Agent or mailbox data.

### 11.7 `recover_agent_identity`

Administrator-only recovery for a lost client credential or inaccessible binding. It preserves Agent ID and data, revokes the old binding, increments generation, and optionally rotates service credentials.

### 11.8 `revoke_client_principal`

Revokes a lost or compromised client installation and its active bindings without deleting the underlying Agents or mailboxes.

### 11.9 `list_identity_bindings`

Administrator/audit view of active and revoked bindings, generations, timestamps, and reasons. All identifiers are minimized and all credentials are redacted.

## 12. Stable errors

Add typed errors with structured recovery data:

- `IDENTITY_BINDING_REQUIRED`
- `IDENTITY_TRANSFER_CONFIRMATION_REQUIRED`
- `IDENTITY_CONFIRMATION_PENDING`
- `IDENTITY_CONFIRMATION_EXPIRED`
- `IDENTITY_SESSION_REVOKED`
- `IDENTITY_BINDING_CONFLICT`
- `CLIENT_PRINCIPAL_REVOKED`
- `UNTRUSTED_CONVERSATION_CONTEXT`

Continue preserving lifecycle errors, including:

- `MAILBOX_UNAVAILABLE`
- `Restore the mailbox from the recycle bin before use`
- `Mailbox is unavailable`
- `Sender mailbox is unavailable`

Identity tools cannot weaken or bypass mailbox lifecycle enforcement.

## 13. Migration plan

### 13.1 Deploy trusted client identity first

Implement and test client principal authentication and trusted conversation metadata before enforcing the new binding model. A server-only change cannot distinguish several conversations multiplexed by one client.

### 13.2 Add schema and migration

- add client principal, binding, confirmation, and credential-version fields/tables;
- install project/Agent consistency constraints and active-binding uniqueness;
- keep migrations explicit and idempotent for existing SQLite databases;
- keep PostgreSQL migration behavior explicit rather than depending on SQLite exception handling.

### 13.3 Enroll existing identities

- inventory existing Agents and `WindowIdentity` rows;
- do not treat an old UUID or Agent name as sufficient proof;
- let a currently authenticated Agent or administrator perform a one-time enrollment into a trusted client/conversation binding;
- preserve the existing Agent ID and all mailbox state;
- trash mailboxes may be inventoried but cannot activate bindings until restored;
- purging mailboxes cannot be enrolled.

### 13.4 Retire plaintext registration tokens

After a trusted binding exists:

- rotate or convert old reusable registration tokens to verification-only hashes where service access is still required;
- stop returning registration tokens from Agent creation/registration tools;
- remove ordinary model-facing `registration_token` parameters;
- remove token values from examples, logs, snapshots, and error data;
- retain no permanent compatibility shim that asks a model to remember a token.

### 13.5 Replace `WindowIdentity`

Migrate confirmed relationships into the new binding model, mark unconfirmed rows as requiring enrollment, then remove the old UUID-only authorization and independent TTL behavior. Do not leave two competing identity authorities.

## 14. Implementation phases

### Phase A: server identity foundation

- add models and migrations;
- implement client-principal middleware;
- implement trusted conversation context extraction;
- implement centralized binding resolution and generation checks;
- add redacted audit events.

### Phase B: MCP adapter support

- add a conversation identity provider for Pi and equivalent hosts;
- inject trusted per-call metadata;
- add local client key enrollment and secure storage;
- ensure reconnect preserves and fork changes the conversation identity;
- prove that one MCP connection can safely multiplex different conversations.

### Phase C: browser confirmation broker

- add persistent confirmation requests;
- add native elicitation support;
- add the client-triggered browser popup fallback;
- add authenticated confirmation routes, CSRF/origin/CSP/no-store protections, timeout, denial, and idempotent consumption;
- ensure no browser capability secret appears in model-visible output.

### Phase D: permanent mailbox behavior

- add create, reconnect, release, transfer, and administrator recovery;
- remove automatic identity expiry;
- separate Agent presence from identity validity;
- enforce one active owner and generation fencing.

### Phase E: temporary mailbox integration

- derive suspended state from mailbox trash/purging state;
- prevent identity operations from renewing retention;
- preserve bindings through trash/restore;
- revoke and remove bindings during purge;
- test permanent/temporary conversion in both directions.

### Phase F: controlled migration

- enroll current identities without changing Agent IDs;
- rotate/hash old credentials;
- migrate or reject untrusted window identity records;
- remove old token and UUID-only paths in the same delivery rather than maintaining indefinite compatibility code.

### Phase G: documentation and release

- update README, identity contract, lifecycle plan, MCP tool documentation, client adapter documentation, and administrator recovery runbook;
- run bounded tests locally and full cross-platform suites in CI;
- commit and push server and adapter changes with explicit coordination across repositories.

## 15. Test plan

### 15.1 Multi-conversation isolation

- one client, one Project, several conversations, different Agents;
- one client, several Projects, independent bindings;
- the same MCP connection multiplexes calls without current-Agent leakage;
- model-supplied conversation IDs are ignored or rejected;
- a new/forked conversation cannot inherit an Agent silently.

### 15.2 Reconnect and transfer

- the same conversation reconnects to the same Agent after MCP and server restart;
- transfer approval revokes the old binding;
- the old session receives `IDENTITY_SESSION_REVOKED`;
- concurrent transfer attempts produce exactly one winner;
- generation fencing rejects writes started before transfer but committed afterward;
- denial, timeout, stale generation, and closed browser leave the old binding unchanged.

### 15.3 Browser confirmation

- native elicitation and browser fallback execute the same confirmation transaction;
- remote servers cause the client, not the server host, to open the browser;
- GET cannot approve;
- CSRF, origin, expired request, replay, and double-submit attempts fail;
- popup blocking never becomes implicit approval;
- request IDs and browser URLs expose no reusable credential;
- the browser page correctly identifies old and new bindings before approval.

### 15.4 Permanent mailbox

- identity remains valid beyond current stale/window TTLs;
- automatic stale-Agent work changes presence only;
- compaction and reconnect do not require token recovery;
- release preserves Agent and mailbox data;
- administrator recovery preserves Agent ID.

### 15.5 Temporary mailbox

- identity resolution and reconnect do not renew retention;
- active mailbox supports normal identity operations;
- trash makes every binding effectively suspended;
- trash blocks read, send, ack, transfer, and recovery until restore;
- restore re-enables the original binding;
- a different conversation still requires transfer after restore;
- temporary-to-permanent makes bindings durable;
- permanent-to-temporary binds availability to retention;
- purge revokes generations and removes identity data without touching retained legacy/source storage;
- old conversation metadata cannot recreate a purged Agent silently.

### 15.6 Credential and authorization security

- database, logs, tool results, errors, browser history, and transcripts contain no reusable Agent credential;
- local private keys never enter server records or model context;
- invalid OAuth principal, client signature, scope, Project, Agent, or generation is rejected;
- a revoked client principal cannot operate through an existing transport session;
- names and old window UUIDs cannot authenticate;
- cross-project and cross-client binding attempts fail.

### 15.7 Quality gates

- `ruff check --fix --unsafe-fixes`
- `uvx ty check --output-format concise`
- `git diff --check`
- Python 3.14 targeted server, database, lifecycle, HTTP, and CLI tests
- MCP stdio and Streamable HTTP tests
- MCP adapter multi-session/multi-conversation tests
- Windows and Linux CI coverage
- complete slow/functional/CI-regression suites remain reported separately from bounded local evidence

## 16. Acceptance criteria

The feature is complete only when all of the following are true:

1. One authenticated MCP client can host several simultaneous conversation Agents safely.
2. Every protected call resolves the Agent from trusted principal, Project, and conversation context.
3. Agent names cannot authenticate or recover an identity.
4. The model never needs to know or repeat a reusable identity credential.
5. The same conversation reconnects without transfer or credential recovery.
6. A different conversation needs explicit user-approved transfer.
7. Transfer atomically revokes the old binding and fences in-flight writes.
8. Permanent mailbox identities do not expire through inactivity.
9. Temporary mailbox identities suspend in trash, resume after restore, and disappear on successful purge.
10. Identity plumbing and background reconnects do not renew temporary-mailbox retention.
11. A bound identity or authorized administrator can recover an inaccessible Agent without changing Agent ID.
12. Native elicitation and browser popup confirmation provide equivalent authorization guarantees.
13. Clients lacking both supported confirmation paths cannot perform transfer; they do not receive a weaker fallback.
14. Existing Agent/mailbox data is migrated without relying on names or unverified UUIDs.
15. Plaintext model-managed registration tokens and UUID-only identity recovery are removed.
16. Retained legacy archives, shared `.git`, source repositories, managed-storage safety boundaries, and external backups remain unaffected.

## 17. Non-goals

- Do not make an Agent name a password.
- Do not store reusable credentials in conversation memory.
- Do not use a client-global “last selected Agent”.
- Do not allow two active conversation owners for one Agent.
- Do not let browser popup compatibility bypass authentication or explicit approval.
- Do not make identity reconnect count as mailbox activity.
- Do not restore any Git-backed mailbox identity, message, or history management.
- Do not add a new primary web navigation destination solely for this feature; browser pages are focused confirmation and administrator surfaces.

## 18. Delivery boundary

This feature crosses two components:

1. MCP Agent Mail server: persistence, lifecycle, authorization, transfer, browser confirmation, and recovery.
2. MCP client adapter: secure client principal, per-conversation metadata injection, browser launch, and confirmation status handling.

Neither half alone satisfies the plan. Server implementation must not claim completion until a supported adapter proves multi-conversation isolation end to end.
