import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtemp, writeFile, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { PassThrough } from 'node:stream';
import { createServer } from 'node:http';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import {
  AdapterError, canonicalizeUrl, credentialService, loadSettings, loadIdentity,
  parseIdentity, injectIdentity, bridge, IDENTITY_META_KEY, withIdentityLock,
  systemStore,
} from './cli.mjs';

const identityJson = JSON.stringify({ version: 1, client_uid: 'codex-client-test-000001', client_secret: 'a'.repeat(43) });
const identity = parseIdentity(identityJson);
const settings = { url: 'http://127.0.0.1:8765/api/', canonicalUrl: 'http://127.0.0.1:8765/api',
  token: 'test-only-token', thread: 'codex-thread-test-000001', clientLabel: 'Test', timeout: 5 };

async function config(toml) {
  // No cleanup: the repository prohibits deleting even newly created test files.
  const dir = await mkdtemp(join(tmpdir(), 'agent-mail-node-test-'));
  const path = join(dir, 'service.toml');
  await writeFile(path, toml, { flag: 'wx' });
  return path;
}

function memoryStore() {
  const values = new Map();
  let writes = 0;
  return { values, get writes() { return writes; }, factory: async key => ({
    get: () => values.get(key) ?? null,
    set: async value => { writes++; await new Promise(r => setTimeout(r, 15)); values.set(key, value); },
  }) };
}

function mockRemote(handler) {
  return { sent: [], protocol: null, async start() {}, async close() {}, setProtocolVersion(v) { this.protocol = v; },
    async send(message) {
      this.sent.push(message);
      if (message.method === 'initialize') this.onmessage({ jsonrpc: '2.0', id: message.id,
        result: { protocolVersion: '2025-06-18', capabilities: { tools: {}, resources: {} }, serverInfo: { name: 'mail', version: '1' } } });
      else await handler?.(message, this);
    },
  };
}

async function harness(t, remote, extra = {}) {
  const input = new PassThrough();
  const output = new PassThrough();
  const messages = [];
  const logs = [];
  let text = '';
  output.on('data', data => {
    text += data.toString();
    let n;
    while ((n = text.indexOf('\n')) >= 0) {
      messages.push(JSON.parse(text.slice(0, n))); text = text.slice(n + 1);
    }
  });
  const session = await bridge({ ...settings, ...extra }, identity, { input, output, remote, log: s => logs.push(s) });
  t.after(() => session.stop());
  const send = message => input.write(`${JSON.stringify({ jsonrpc: '2.0', ...message })}\n`);
  async function wait(predicate) {
    const deadline = Date.now() + 5000;
    while (Date.now() < deadline) {
      const result = messages.find(predicate);
      if (result) return result;
      await new Promise(r => setTimeout(r, 5));
    }
    throw new Error('Timed out waiting for MCP response');
  }
  send({ id: 0, method: 'initialize', params: { protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 'test', version: '1' } } });
  await wait(m => m.id === 0);
  send({ method: 'notifications/initialized' });
  return { input, messages, logs, send, wait, session };
}

test('endpoint normalization, service isolation and Python fingerprint contract', () => {
  assert.equal(canonicalizeUrl('HTTPS://MAIL.example.test:443/api///'), 'https://mail.example.test/api');
  assert.equal(canonicalizeUrl('http://[::1]:80/'), 'http://[::1]/');
  assert.equal(credentialService('https://mail.example.test/api/'), credentialService('https://MAIL.example.test:443/api'));
  assert.notEqual(credentialService('http://mail.example.test/api'), credentialService('https://mail.example.test/api'));
  assert.notEqual(credentialService('https://mail.example.test/a'), credentialService('https://mail.example.test/b'));
  for (const bad of ['file:///etc/passwd', 'http://u:p@mail/a', 'http://mail/a?q=token', 'http://mail/a#x', 'http://mail\\evil/a', 'garbage']) {
    assert.throws(() => canonicalizeUrl(bad), AdapterError);
  }
});

test('TOML file secrets, strict keys, runtime-only thread and no cross-profile env override', async () => {
  const file = await config('url="https://mail.example.test/api/"\ntoken="file-token"\nclient_label="Laptop"\n');
  const env = { CODEX_THREAD_ID: settings.thread, MCP_AGENT_MAIL_BEARER_TOKEN: 'wrong-service-token' };
  const result = await loadSettings(file, { env });
  assert.equal(result.token, 'file-token');
  assert.equal(result.thread, settings.thread);
  assert.equal(result.clientLabel, 'Laptop');
  assert.ok(!JSON.stringify(result).includes('file-token'));
  assert.ok(!JSON.stringify(result).includes(settings.thread));
  await assert.rejects(loadSettings(file, { env: {} }), /CODEX_THREAD_ID/);
  assert.equal((await loadSettings(file, { env: {}, requireThread: false })).thread, undefined);
  const forbidden = await config('url="https://mail.example.test/api/"\nCODEX_THREAD_ID="hardcoded-thread-00001"\n');
  await assert.rejects(loadSettings(forbidden, { env }), /Unknown config key/);
});

test('explicit token environment fallback and invalid configuration fail closed', async () => {
  const file = await config('url="https://mail.example.test/api/"\ntoken_env="MAIL_TOKEN"\n');
  await assert.rejects(loadSettings(file, { env: { CODEX_THREAD_ID: settings.thread } }), /unavailable/);
  assert.equal((await loadSettings(file, { env: { CODEX_THREAD_ID: settings.thread, MAIL_TOKEN: 'forwarded' } })).token, 'forwarded');
  for (const extra of ['token=17', 'token="a"\ntoken_env="B"', 'request_timeout_seconds=0', 'client_label=[]']) {
    await assert.rejects(loadSettings(await config(`url="https://mail.example.test/api/"\n${extra}`), { env: { CODEX_THREAD_ID: settings.thread } }), AdapterError);
  }
  const invalid = await config('token="sensitive-value"\nnot toml');
  await assert.rejects(loadSettings(invalid), e => !e.message.includes('sensitive-value'));
});

test('concurrent first starts retain one principal; endpoint changes isolate it', async () => {
  const store = memoryStore();
  const [a, b] = await Promise.all([loadIdentity(settings.url, store.factory), loadIdentity(settings.url, store.factory)]);
  assert.equal(a.client_uid, b.client_uid);
  assert.equal(a.client_secret, b.client_secret);
  assert.equal(store.writes, 1);
  assert.equal((await loadIdentity(settings.url, store.factory)).client_uid, a.client_uid);
  assert.notEqual((await loadIdentity('https://second.example.test/api/', store.factory)).client_uid, a.client_uid);
  assert.equal(JSON.stringify(a), '{}');
});

test('existing version-1 credentials are reused and corruption never rotates them', async () => {
  const store = memoryStore();
  const key = credentialService(settings.url);
  store.values.set(key, identityJson);
  assert.equal((await loadIdentity(settings.url, store.factory)).client_uid, identity.client_uid);
  assert.equal(store.writes, 0);
  store.values.set(key, '{broken-secret');
  await assert.rejects(loadIdentity(settings.url, store.factory), /Stored identity/);
  assert.equal(store.values.get(key), '{broken-secret');
  await assert.rejects(loadIdentity(settings.url, async () => { throw new Error('sensitive-value'); }), e => !e.message.includes('sensitive-value'));
});

test('Windows credential reads never construct withTarget or write; Python target lookup is exact', async () => {
  const service = credentialService(settings.url);
  let writes = 0;
  let value = null;
  const binding = {
    Entry: class {
      constructor(s, user, options) { assert.equal(s, service); assert.equal(user, 'client-principal'); assert.equal(options.linux.store, 'secret-service'); }
      static withTarget() { throw new Error('Destructive constructor must never be used'); }
      getPassword() { return value; }
      setPassword(v) { writes++; value = v; }
    },
    findCredentials(s, target) { assert.equal(s, service); assert.equal(target, service); return [{ account: 'client-principal', password: identityJson }]; },
  };
  const store = await systemStore(service, binding, 'win32');
  assert.equal(store.get(), identityJson);
  assert.equal(writes, 0);
  store.set(identityJson);
  assert.equal(store.get(), identityJson);
  assert.equal(writes, 1);
});

test('identity lock excludes another Node process without deleting lock files', async () => {
  const service = `test-${process.pid}`;
  let lockChild;
  await withIdentityLock(service, async () => {
    const code = `import {withIdentityLock} from ${JSON.stringify(new URL('./cli.mjs', import.meta.url).href)}; await withIdentityLock(${JSON.stringify(service)}, async()=>console.log('acquired'));`;
    const child = spawn(process.execPath, ['--input-type=module', '-e', code], { stdio: ['ignore', 'pipe', 'pipe'] });
    let output = '';
    child.stdout.on('data', d => { output += d; });
    await new Promise(r => setTimeout(r, 250));
    assert.equal(output, '');
    // Wait only after the lock is released by the outer finally.
    lockChild = { child, output: () => output };
  });
  const exit = await once(lockChild.child, 'exit');
  assert.equal(exit[0], 0);
  assert.equal(lockChild.output().trim(), 'acquired');
});

test('reserved metadata cannot be spoofed; arguments and progress metadata are preserved', () => {
  const message = { jsonrpc: '2.0', id: 1, method: 'tools/call', params: { name: 'x', arguments: { conversation_uid: 'untrusted' },
    _meta: { progressToken: 'p1', [IDENTITY_META_KEY]: { client_secret: 'spoofed' } } } };
  const sent = injectIdentity(message, settings, identity);
  assert.equal(sent.params._meta[IDENTITY_META_KEY].conversation_uid, settings.thread);
  assert.equal(sent.params._meta[IDENTITY_META_KEY].client_secret, identity.client_secret);
  assert.equal(sent.params._meta.progressToken, 'p1');
  assert.equal(sent.params.arguments.conversation_uid, 'untrusted');
  assert.equal(message.params._meta[IDENTITY_META_KEY].client_secret, 'spoofed');
  assert.equal(injectIdentity({ method: 'initialize', params: message.params }, settings, identity).params._meta[IDENTITY_META_KEY], undefined);
});

test('STDIO forwards capabilities, tool results, resource reads, errors and notifications', async t => {
  const remote = mockRemote((m, r) => {
    if (!m.id) return;
    if (m.method === 'tools/call') {
      r.onmessage({ jsonrpc: '2.0', method: 'notifications/progress', params: { progressToken: 'p', progress: 1 } });
      r.onmessage({ jsonrpc: '2.0', id: m.id, result: { content: [{ type: 'text', text: 'ok' }], structuredContent: { ok: true } } });
    } else if (m.method === 'resources/read') r.onmessage({ jsonrpc: '2.0', id: m.id, result: { contents: [{ uri: m.params.uri, text: 'mail' }] } });
    else r.onmessage({ jsonrpc: '2.0', id: m.id, error: { code: -32601, message: 'missing' } });
  });
  const h = await harness(t, remote);
  assert.equal(remote.protocol, '2025-06-18');
  assert.deepEqual(h.messages[0].result.capabilities, { tools: {}, resources: {} });
  h.send({ id: 1, method: 'tools/call', params: { name: 'x', arguments: {}, _meta: { progressToken: 'p' } } });
  h.send({ id: 2, method: 'resources/read', params: { uri: 'resource://inbox/test' } });
  h.send({ id: 3, method: 'unknown' });
  assert.equal((await h.wait(m => m.id === 1)).result.structuredContent.ok, true);
  assert.equal((await h.wait(m => m.id === 2)).result.contents[0].text, 'mail');
  assert.equal((await h.wait(m => m.id === 3)).error.code, -32601);
  assert.ok(h.messages.some(m => m.method === 'notifications/progress'));
  for (const m of remote.sent.filter(m => ['tools/call', 'resources/read'].includes(m.method))) {
    assert.equal(m.params._meta[IDENTITY_META_KEY].conversation_uid, settings.thread);
  }
  h.input.end();
  await h.session.done;
});

async function httpUpstream(t, handler) {
  const server = createServer(async (req, res) => {
    if (req.method === 'GET') { res.writeHead(405).end(); return; }
    let body = '';
    for await (const chunk of req) body += chunk;
    const message = JSON.parse(body);
    if (message.method === 'initialize') {
      res.writeHead(200, { 'Content-Type': 'application/json', 'Mcp-Session-Id': 'session-test' });
      res.end(JSON.stringify({ jsonrpc: '2.0', id: message.id, result: { protocolVersion: '2025-06-18', capabilities: { tools: {} }, serverInfo: { name: 'test', version: '1' } } }));
    } else if (!message.id) res.writeHead(202).end();
    else await handler(req, res, message);
  });
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  t.after(async () => { server.closeAllConnections(); await new Promise(r => server.close(r)); });
  return `http://127.0.0.1:${server.address().port}/api/`;
}

test('real Streamable HTTP session headers and SSE response with progress', async t => {
  let captured;
  const url = await httpUpstream(t, (req, res, m) => {
    assert.equal(req.headers.authorization, `Bearer ${settings.token}`);
    assert.equal(req.headers['mcp-session-id'], 'session-test');
    assert.equal(req.headers['mcp-protocol-version'], '2025-06-18');
    captured = m.params._meta[IDENTITY_META_KEY];
    res.writeHead(200, { 'Content-Type': 'text/event-stream' });
    res.write(`event: message\ndata: ${JSON.stringify({ jsonrpc: '2.0', method: 'notifications/progress', params: { progressToken: 1, progress: 1 } })}\n\n`);
    res.end(`event: message\ndata: ${JSON.stringify({ jsonrpc: '2.0', id: m.id, result: { content: [{ type: 'text', text: 'live' }] } })}\n\n`);
  });
  const h = await harness(t, undefined, { url });
  h.send({ id: 1, method: 'tools/call', params: { name: 'capture', arguments: {} } });
  assert.equal((await h.wait(m => m.id === 1)).result.content[0].text, 'live');
  assert.equal(captured.client_secret, identity.client_secret);
  assert.ok(h.messages.some(m => m.method === 'notifications/progress'));
});

test('HTTP failures do not leak upstream bodies or replay writes', async t => {
  let writes = 0;
  const url = await httpUpstream(t, (_req, res) => { writes++; res.writeHead(403).end(`${settings.token} ${identity.client_secret}`); });
  const h = await harness(t, undefined, { url });
  h.send({ id: 1, method: 'tools/call', params: { name: 'send_message', arguments: {} } });
  assert.ok((await h.wait(m => m.id === 1)).error);
  assert.equal(writes, 1);
  assert.ok(!JSON.stringify([h.messages, h.logs]).includes(settings.token));
  assert.ok(!JSON.stringify([h.messages, h.logs]).includes(identity.client_secret));
});

test('HTTP redirects are rejected before forwarding credentials', async t => {
  let redirected = false;
  const target = await httpUpstream(t, (_req, res) => { redirected = true; res.end(); });
  const url = await httpUpstream(t, (_req, res) => { res.writeHead(307, { Location: target }).end(); });
  const h = await harness(t, undefined, { url });
  h.send({ id: 1, method: 'tools/call', params: { name: 'x', arguments: {} } });
  assert.ok((await h.wait(m => m.id === 1)).error);
  assert.equal(redirected, false);
});

test('timeouts ignore late replies and never retry writes', async t => {
  const remote = mockRemote();
  const h = await harness(t, remote, { timeout: 0.03 });
  h.send({ id: 1, method: 'tools/call', params: { name: 'x', arguments: {} } });
  assert.match((await h.wait(m => m.id === 1)).error.message, /timed out/);
  remote.onmessage({ jsonrpc: '2.0', id: 1, result: {} });
  assert.equal(h.messages.filter(m => m.id === 1).length, 1);
  assert.equal(remote.sent.filter(m => m.id === 1).length, 1);
});

test('package bin version matches package metadata and help does not require Python', async () => {
  const pkg = JSON.parse(await readFile(new URL('../package.json', import.meta.url), 'utf8'));
  const child = spawn(process.execPath, ['adapter/cli.mjs', '--version'], { stdio: ['ignore', 'pipe', 'pipe'] });
  let out = '';
  child.stdout.on('data', b => { out += b; });
  const [code] = await once(child, 'exit');
  assert.equal(code, 0);
  assert.equal(out.trim(), pkg.version);
});
