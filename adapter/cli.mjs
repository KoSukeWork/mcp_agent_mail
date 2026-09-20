#!/usr/bin/env node
// STDIO is protocol-only. Never print configuration, identities or upstream errors.
import { createHash, randomBytes } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { resolve, join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { parseArgs } from 'node:util';
import { createServer } from 'node:net';
import { setTimeout as delay } from 'node:timers/promises';
import { parse } from 'smol-toml';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';

export const IDENTITY_META_KEY = 'io.github.mcp-agent-mail/identity';
const ACCOUNT = 'client-principal';
const OPAQUE_ID = /^[A-Za-z0-9._~-]{16,256}$/;
const SECRET = /^[A-Za-z0-9_-]{32,256}$/;
const USER_AGENT = 'OpenAI File Downloader, XaiImageApiFetch/1.0';
const VERSION = '0.1.0';

export class AdapterError extends Error {}

export function canonicalizeUrl(value) {
  if (typeof value !== 'string' || !/^https?:\/\//i.test(value.trim())) {
    throw new AdapterError('url must be an absolute HTTP(S) MCP endpoint.');
  }
  let url;
  try { url = new URL(value.trim()); } catch { throw new AdapterError('Invalid upstream URL.'); }
  if (url.username || url.password || url.search || url.hash || /[\\\s]/.test(value.trim())) {
    throw new AdapterError('URL must not contain credentials, query, fragment, whitespace or backslashes.');
  }
  const path = url.pathname.replace(/\/+$/, '') || '/';
  return `${url.origin}${path}`;
}

export function credentialService(url) {
  return `mcp-agent-mail/codex-adapter/${createHash('sha256').update(canonicalizeUrl(url)).digest('hex')}`;
}

export async function loadSettings(file, { env = process.env, requireThread = true } = {}) {
  let config;
  try {
    const raw = await readFile(file, 'utf8');
    if (Buffer.byteLength(raw) > 65536) throw new Error();
    config = parse(raw.replace(/^\uFEFF/, ''));
  } catch { throw new AdapterError('Cannot read adapter TOML configuration (missing, oversized or invalid).'); }
  const allowed = new Set(['url', 'token', 'token_env', 'client_label', 'request_timeout_seconds']);
  if (Object.keys(config).some(key => !allowed.has(key))) {
    throw new AdapterError('Unknown config key. Allowed: url, token, token_env, client_label, request_timeout_seconds. Never configure a conversation ID.');
  }
  const canonicalUrl = canonicalizeUrl(config.url);
  if (config.token !== undefined && config.token_env !== undefined) {
    throw new AdapterError('Configure token or token_env, not both.');
  }
  let token = config.token ?? '';
  if (config.token_env !== undefined) {
    if (typeof config.token_env !== 'string' || !/^[A-Za-z_][A-Za-z0-9_]*$/.test(config.token_env)) {
      throw new AdapterError('Invalid token_env variable name.');
    }
    token = env[config.token_env];
    if (!token) throw new AdapterError('The explicitly configured token_env variable is unavailable.');
  }
  if (typeof token !== 'string' || /[\r\n]/.test(token)) throw new AdapterError('Invalid bearer token.');
  const thread = env.CODEX_THREAD_ID?.trim();
  if (requireThread && (!thread || !OPAQUE_ID.test(thread))) {
    throw new AdapterError('Codex must supply CODEX_THREAD_ID at process launch. Do not hard-code a thread ID; --check works outside Codex.');
  }
  if (config.client_label !== undefined && typeof config.client_label !== 'string') {
    throw new AdapterError('client_label must be a string.');
  }
  const timeout = config.request_timeout_seconds ?? 120;
  if (!Number.isInteger(timeout) || timeout < 5 || timeout > 3600) {
    throw new AdapterError('request_timeout_seconds must be an integer from 5 to 3600.');
  }
  // Sensitive values are non-enumerable: diagnostics must never stringify them.
  return Object.defineProperties({ canonicalUrl, url: config.url.trim(), timeout, clientLabel: config.client_label?.trim().slice(0, 128) || 'Codex' }, {
    token: { value: token.trim() }, thread: { value: thread },
  });
}

export function parseIdentity(raw) {
  let value;
  try { value = JSON.parse(raw); } catch { throw new AdapterError('Stored identity is invalid; restore the credential store or request web-admin recovery.'); }
  if (value?.version !== 1 || typeof value.client_uid !== 'string' || !OPAQUE_ID.test(value.client_uid)
      || typeof value.client_secret !== 'string' || !SECRET.test(value.client_secret)) {
    throw new AdapterError('Stored identity has an invalid format; it will not be overwritten.');
  }
  return Object.defineProperties({}, {
    client_uid: { value: value.client_uid }, client_secret: { value: value.client_secret },
  });
}

export async function systemStore(service, binding, platform = process.platform) {
  try {
    const { Entry, findCredentials } = binding ?? await import('@napi-rs/keyring');
    // Never use Entry.withTarget on Windows: keyring 2.1.0 writes an empty
    // credential in that constructor, even when the caller only wants to read.
    const entry = new Entry(service, ACCOUNT, { linux: { store: 'secret-service' } });
    return {
      get() {
        const value = entry.getPassword();
        if (value !== null && value !== undefined) return value;
        if (platform !== 'win32') return null;
        // Read-only import of the Python adapter's exact UTF-16 target. Never
        // enumerate unrelated credentials, export secrets or rewrite that key.
        const previous = findCredentials(service, service).filter(row => row.account === ACCOUNT);
        if (previous.length > 1) throw new AdapterError('Ambiguous previous adapter identity.');
        return previous[0]?.password ?? null;
      },
      set(value) { entry.setPassword(value); },
    };
  } catch { throw new AdapterError('OS credential store unavailable. Linux requires an unlocked Secret Service; no plaintext or temporary-keyring fallback is used.'); }
}

// A short-lived local listener serializes first enrollment across npx processes.
// Windows named pipes avoid Windows/Docker's reserved TCP port ranges. Linux
// abstract sockets leave no filesystem entry; macOS uses a loopback port.
// No data is exchanged; closing the process releases the lock automatically.
export async function withIdentityLock(service, fn) {
  const hash = createHash('sha256').update(`${homedir()}\n${service}`).digest();
  const name = `agent-mail-adapter-${hash.toString('hex')}`;
  const address = process.platform === 'win32' ? { path: `\\\\.\\pipe\\${name}` }
    : process.platform === 'linux' ? { path: `\0${name}` }
    : { host: '127.0.0.1', port: 20000 + hash.readUInt16BE(0) % 20000 };
  const deadline = Date.now() + 10000;
  while (true) {
    const server = createServer(socket => socket.destroy());
    try {
      await new Promise((ok, fail) => {
        server.once('error', fail);
        server.listen({ ...address, exclusive: true }, ok);
      });
    } catch (error) {
      if (error.code !== 'EADDRINUSE' || Date.now() >= deadline) {
        throw new AdapterError('Unable to acquire local identity lock. Retry after other adapter starts finish.');
      }
      await delay(50);
      continue;
    }
    try { return await fn(); }
    finally { await new Promise(done => server.close(done)); }
  }
}

export async function loadIdentity(url, storeFactory = systemStore) {
  const service = credentialService(url);
  return withIdentityLock(service, async () => {
    try {
      const store = await storeFactory(service);
      const existing = await store.get();
      if (existing !== null && existing !== undefined) return parseIdentity(existing);
      const serialized = JSON.stringify({
        version: 1, client_uid: `codex-${randomBytes(24).toString('base64url')}`,
        client_secret: randomBytes(32).toString('base64url'),
      });
      await store.set(serialized);
      const confirmed = await store.get();
      if (confirmed !== serialized) throw new AdapterError('Credential store did not retain identity; refusing to continue.');
      return parseIdentity(confirmed);
    } catch (error) {
      if (error instanceof AdapterError) throw error;
      throw new AdapterError('OS credential store could not read or persist the identity. No credential was exported.');
    }
  });
}

export function injectIdentity(message, settings, identity) {
  if (!message.method) return message; // Responses to upstream requests have no params.
  const meta = { ...message.params?._meta };
  // Never trust an identity supplied over STDIO, including initialize metadata.
  delete meta[IDENTITY_META_KEY];
  if (message.method !== 'initialize') {
    meta[IDENTITY_META_KEY] = {
      version: 1, client_uid: identity.client_uid, client_secret: identity.client_secret,
      conversation_uid: settings.thread, client_label: settings.clientLabel, capabilities: [],
    };
  }
  return { ...message, params: { ...message.params, _meta: meta } };
}

export function upstreamTransport(settings) {
  return new StreamableHTTPClientTransport(new URL(settings.url), {
    requestInit: { headers: { 'User-Agent': USER_AGENT, ...(settings.token ? { Authorization: `Bearer ${settings.token}` } : {}) } },
    // Redirects could forward the identity JSON body to a different service.
    // Do not replay writes after transport errors: their outcome may be unknown.
    fetch: (url, init) => fetch(url, { ...init, redirect: 'error', signal: AbortSignal.any([
      ...(init?.signal ? [init.signal] : []), AbortSignal.timeout(settings.timeout * 1000),
    ]) }),
  });
}

export async function bridge(settings, identity, {
  input = process.stdin, output = process.stdout, remote = upstreamTransport(settings),
  log = text => process.stderr.write(`${text}\n`),
} = {}) {
  const local = new StdioServerTransport(input, output);
  const pending = new Map();
  let initializeId;
  let initialized = false;
  let closed = false;
  let finish;
  const done = new Promise(resolveDone => { finish = resolveDone; });
  const safeError = (id, message) => local.send({ jsonrpc: '2.0', id, error: { code: -32000, message } });
  const stop = async () => {
    if (closed) return;
    closed = true;
    for (const timer of pending.values()) clearTimeout(timer);
    pending.clear();
    input.off('end', stop);
    await Promise.allSettled([remote.close(), local.close()]);
    finish();
  };
  remote.onerror = () => log('Agent Mail transport error; check service availability and authentication. Writes are not automatically replayed.');
  local.onerror = () => { log('Invalid MCP STDIO input.'); void stop(); };
  remote.onclose = () => { void stop(); };
  local.onclose = () => { void stop(); };
  remote.onmessage = message => {
    if (closed) return;
    if (!message.method && message.id !== undefined) {
      if (!pending.has(message.id)) return; // Ignore late responses after a timeout.
      clearTimeout(pending.get(message.id));
      pending.delete(message.id);
      if (message.id === initializeId && message.result?.protocolVersion) {
        remote.setProtocolVersion(message.result.protocolVersion);
        initialized = true;
        message = { ...message, result: { ...message.result, instructions:
          'This adapter supplies trusted per-task identity. Use macro_start_session normally; never request adapter credentials.\n'
          + (message.result.instructions || '') } };
      }
    }
    void local.send(message).catch(() => stop());
  };
  local.onmessage = message => {
    if (closed) return;
    const isRequest = message.method && message.id !== undefined;
    if (message.method === 'initialize') {
      if (initializeId !== undefined) { void safeError(message.id, 'Already initialized.').catch(() => stop()); return; }
      initializeId = message.id;
    } else if (message.method && !initialized) {
      if (isRequest) void safeError(message.id, 'Initialize the adapter first.').catch(() => stop());
      return;
    }
    if (isRequest) {
      if (pending.has(message.id)) { void stop(); return; }
      if (pending.size >= 128) { void safeError(message.id, 'Too many pending requests.').catch(() => stop()); return; }
      pending.set(message.id, setTimeout(() => {
        pending.delete(message.id);
        void safeError(message.id, 'Upstream request timed out; its outcome may be unknown. Do not blindly repeat writes.').catch(() => stop());
      }, settings.timeout * 1000));
    }
    void remote.send(injectIdentity(message, settings, identity)).catch(() => {
      if (isRequest && pending.has(message.id)) {
        clearTimeout(pending.get(message.id));
        pending.delete(message.id);
        void safeError(message.id, 'Upstream request failed; check authentication or reconnect. Its outcome may be unknown.').catch(() => stop());
      }
    });
  };
  input.once('end', stop);
  try { await remote.start(); await local.start(); }
  catch { await stop(); throw new AdapterError('Unable to start MCP transports.'); }
  return { stop, done };
}

export async function checkConnection(settings) {
  // Diagnostic mode does not enroll a principal or create/claim an Agent.
  const store = await systemStore(credentialService(settings.canonicalUrl));
  try { const raw = await store.get(); if (raw !== null && raw !== undefined) parseIdentity(raw); }
  catch { throw new AdapterError('Credential store cannot be read or contains an invalid identity.'); }
  const client = new Client({ name: 'agent-mail-adapter-check', version: VERSION });
  try {
    await client.connect(upstreamTransport(settings), { timeout: settings.timeout * 1000 });
    const result = await client.listTools({}, { timeout: settings.timeout * 1000 });
    if (!result.tools.some(tool => tool.name === 'macro_start_session')) throw new Error();
    return { ok: true, transport: 'stdio-to-streamable-http', credential_store: 'available', identity_enrolled: false };
  } catch { throw new AdapterError('Connection check failed. Check the exact MCP URL, token and server version.'); }
  finally { await client.close(); }
}

export async function main(args = process.argv.slice(2)) {
  let values;
  try { ({ values } = parseArgs({ args, options: {
    config: { type: 'string' }, profile: { type: 'string' }, check: { type: 'boolean' },
    help: { type: 'boolean' }, version: { type: 'boolean' },
  } })); } catch { throw new AdapterError('Invalid arguments. Run agent-mail-adapter --help.'); }
  if (values.help) {
    process.stdout.write('agent-mail-adapter --profile NAME | --config FILE [--check]\n'
      + 'Profiles: ~/.config/mcp-agent-mail/NAME.toml (including Windows).\n'
      + 'Node.js >=22.13; no Python/uv. --check is read-only and works outside Codex.\n');
    return;
  }
  if (values.version) { process.stdout.write(`${VERSION}\n`); return; }
  if (Boolean(values.config) === Boolean(values.profile)) throw new AdapterError('Choose exactly one of --profile NAME or --config FILE.');
  if (values.profile && !/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(values.profile)) throw new AdapterError('Invalid profile name.');
  const file = values.config ? resolve(values.config) : join(homedir(), '.config', 'mcp-agent-mail', `${values.profile}.toml`);
  const settings = await loadSettings(file, { requireThread: !values.check });
  if (values.check) { process.stdout.write(`${JSON.stringify(await checkConnection(settings))}\n`); return; }
  const identity = await loadIdentity(settings.canonicalUrl);
  const session = await bridge(settings, identity);
  const shutdown = () => { void session.stop(); };
  process.once('SIGINT', shutdown);
  process.once('SIGTERM', shutdown);
  await session.done;
  process.off('SIGINT', shutdown);
  process.off('SIGTERM', shutdown);
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  main().catch(error => {
    process.stderr.write(`Agent Mail adapter: ${error instanceof AdapterError ? error.message : 'Startup failed; check configuration and OS credential-store availability.'}\n`);
    process.exitCode = 1;
  });
}
