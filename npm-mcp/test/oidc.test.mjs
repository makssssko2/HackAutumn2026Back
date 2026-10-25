import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { request } from 'node:http';
import { test } from 'node:test';
import { browserLogin } from '../src/oidc.mjs';
import { InvoiceKeeperApi } from '../src/api.mjs';
import { readConfig, ConfigurationError } from '../src/config.mjs';

const base = { IK_MCP_API_URL: 'http://127.0.0.1:8000' };
const code = 'c'.repeat(64);
function initiated(options = {}) {
  let open;
  const opened = new Promise((resolve) => { open = resolve; });
  const messages = [];
  const pending = browserLogin(base.IK_MCP_API_URL, { openBrowser: open, writeError: (text) => messages.push(text), ...options });
  return { opened, pending, messages };
}
function callbackUrl(url, params = {}) {
  const source = new URL(url);
  const callback = new URL(source.searchParams.get('redirect_uri'));
  callback.search = new URLSearchParams({ state: source.searchParams.get('state'), code, ...params });
  return callback;
}

test('password remains default; corporate mode needs no credentials', () => {
  const local = readConfig({ ...base, IK_MCP_LOGIN: 'employee', IK_MCP_PASSWORD: 'password' });
  assert.equal(local.auth, 'password');
  assert.equal(local.login, 'employee');
  const oidc = readConfig({ ...base, IK_MCP_AUTH: 'oidc', IK_MCP_PASSWORD: 'unused' });
  assert.equal(oidc.auth, 'oidc');
  assert.equal(oidc.password, undefined);
  assert.throws(() => readConfig(base), ConfigurationError);
  assert.throws(() => readConfig({ ...base, IK_MCP_AUTH: 'other' }), ConfigurationError);
});

test('real loopback callback accepts only the current state, method, host and path', async () => {
  const flow = initiated();
  const url = await flow.opened;
  const callback = callbackUrl(url);
  for (const invalid of [callbackUrl(url, { state: 'wrong' }), new URL(callback.href + '&code=duplicate'),
    new URL(callback.href.replace('/oidc/callback?', '/other?'))]) {
    assert.equal((await fetch(invalid)).status, 400);
  }
  assert.equal((await fetch(callback, { method: 'POST' })).status, 400);
  const hostileHost = await new Promise((resolve, reject) => {
    const req = request(callback, { headers: { Host: 'evil.example' } }, (res) => { res.resume(); resolve(res.statusCode); });
    req.on('error', reject); req.end();
  });
  assert.equal(hostileHost, 400);
  const response = await fetch(callback);
  assert.equal(response.status, 200);
  assert.equal(response.headers.get('cache-control'), 'no-store');
  assert.equal(response.headers.get('referrer-policy'), 'no-referrer');
  const html = await response.text();
  assert.ok(!html.includes(code) && !html.includes(callback.searchParams.get('state')));
  const proof = await flow.pending;
  assert.equal(proof.code, code);
  assert.equal(proof.redirect_uri, new URL(url).searchParams.get('redirect_uri'));
  assert.equal(createHash('sha256').update(proof.code_verifier).digest('base64url'), new URL(url).searchParams.get('code_challenge'));
  assert.ok(!flow.messages.join('').includes(proof.code_verifier));
  await assert.rejects(fetch(callback));
});

test('bound error closes listener and never forwards external error text', async () => {
  const flow = initiated();
  const url = await flow.opened;
  const result = assert.rejects(flow.pending, /Sign-in denied/);
  const response = await fetch(callbackUrl(url, { error: 'private-provider-error' }));
  assert.equal(response.status, 400);
  assert.ok(!(await response.text()).includes('private-provider-error'));
  await result;
  await assert.rejects(fetch(callbackUrl(url)));
});

test('abort and deadline close the actual callback listener', async () => {
  for (const timeout of [false, true]) {
    const abort = new AbortController();
    const flow = initiated({ signal: abort.signal, timeoutMs: timeout ? 25 : 300000 });
    const url = await flow.opened;
    const result = assert.rejects(flow.pending, timeout ? /timed out/ : /aborted/);
    if (!timeout) abort.abort();
    await result;
    await assert.rejects(fetch(callbackUrl(url)));
  }
});

test('concurrent tools share browser sign-in, exchange cookie, then revoke on close', async () => {
  let calls = [], opened = 0, release;
  const gate = new Promise((resolve) => { release = resolve; });
  const fetchImpl = async (url, options) => {
    calls.push([url.pathname, options]);
    if (url.pathname.endsWith('/mcp/exchange')) return new Response('{"ok":true}', { headers: { 'set-cookie': 'ik_session=session-value; Path=/api; HttpOnly' } });
    assert.equal(options.headers.Cookie, 'ik_session=session-value');
    return new Response('{"ok":true}');
  };
  const api = new InvoiceKeeperApi(readConfig({ ...base, IK_MCP_AUTH: 'oidc' }), fetchImpl, async () => {
    opened++; await gate; return { code, code_verifier: 'v'.repeat(64), redirect_uri: 'http://127.0.0.1:45678/oidc/callback' };
  });
  const requests = [api.request('GET', '/auth/me'), api.request('GET', '/payment-occurrences')];
  assert.equal(opened, 1);
  release(); await Promise.all(requests); await api.close();
  assert.deepEqual(calls.map(([path]) => path), ['/api/v1/oidc/mcp/exchange', '/api/v1/auth/me', '/api/v1/payment-occurrences', '/api/v1/auth/logout']);
  assert.ok(!JSON.stringify(calls).includes('password'));
});

test('API shutdown aborts real waiting browser login promptly', async () => {
  let urlReady;
  const ready = new Promise((resolve) => { urlReady = resolve; });
  const api = new InvoiceKeeperApi(readConfig({ ...base, IK_MCP_AUTH: 'oidc' }), async () => { throw new Error('no API call expected'); },
    (url, options) => browserLogin(url, { ...options, openBrowser: urlReady, writeError: () => {} }));
  const pending = assert.rejects(api.request('GET', '/auth/me'), /Corporate sign-in failed/);
  const url = await ready;
  await api.close(); await pending;
  await assert.rejects(fetch(callbackUrl(url)));
});

test('OIDC denial is terminal, does not reopen browser or fall back to passwords', async () => {
  let opened = 0, requests = 0;
  const api = new InvoiceKeeperApi(readConfig({ ...base, IK_MCP_AUTH: 'oidc' }), async () => {
    requests++; return new Response('{}', { status: 401 });
  }, async () => { opened++; return { code }; });
  await assert.rejects(api.request('GET', '/auth/me'));
  await assert.rejects(api.request('GET', '/auth/me'));
  assert.equal(opened, 1); assert.equal(requests, 1); await api.close();
});

test('password login, read-only guard and terminal session expiry stay compatible', async () => {
  let calls = [];
  const api = new InvoiceKeeperApi(readConfig({ ...base, IK_MCP_LOGIN: 'employee', IK_MCP_PASSWORD: 'password', IK_MCP_READ_ONLY: 'true' }), async (url, options) => {
    calls.push(url.pathname);
    if (url.pathname.endsWith('/auth/login')) {
      assert.deepEqual(JSON.parse(options.body), { login: 'employee', password: 'password' });
      return new Response('{}', { headers: { 'set-cookie': 'ik_session=local-session; Path=/api; HttpOnly' } });
    }
    return new Response('{}', { status: 401 });
  });
  await assert.rejects(api.request('POST', '/one-time-payments'), /read-only/);
  await assert.rejects(api.request('GET', '/payments/budget', { mutating: true }), /read-only/);
  assert.equal(calls.length, 0);
  await assert.rejects(api.request('GET', '/auth/me'), /401/);
  await assert.rejects(api.request('GET', '/auth/me'), /Restart/);
  assert.deepEqual(calls, ['/api/v1/auth/login', '/api/v1/auth/me']); await api.close();
});
