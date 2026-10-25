import { createServer } from 'node:http';
import { createHash, randomBytes, timingSafeEqual } from 'node:crypto';
import { spawn } from 'node:child_process';

function launchBrowser(url) {
  // URL is a separate argv entry: never shell syntax (including on Windows).
  const [command, args] = process.platform === 'darwin' ? ['open', [url]]
    : process.platform === 'win32' ? ['rundll32.exe', ['url.dll,FileProtocolHandler', url]]
      : ['xdg-open', [url]];
  const child = spawn(command, args, { shell: false, stdio: 'ignore' });
  child.on('error', () => {}); // The manual link below remains usable if no launcher exists.
  child.unref();
}

function equalState(actual, expected) {
  const left = Buffer.from(actual ?? ''), right = Buffer.from(expected);
  return left.length === right.length && timingSafeEqual(left, right);
}

export async function browserLogin(apiUrl, { signal, openBrowser = launchBrowser,
  writeError = (message) => process.stderr.write(message), timeoutMs = 300_000 } = {}) {
  signal?.throwIfAborted();
  const verifier = randomBytes(48).toString('base64url');
  const state = randomBytes(32).toString('base64url');
  const challenge = createHash('sha256').update(verifier).digest('base64url');
  let settle, timer, redirectUri, done = false;
  const completed = new Promise((resolve, reject) => { settle = (error, code) => error ? reject(error) : resolve(code); });
  // Install a rejection observer immediately: abort can happen before listen completes.
  completed.catch(() => {});
  const server = createServer({ maxHeaderSize: 4096 }, (request, response) => {
    const reply = (status, message) => {
      response.writeHead(status, { 'Content-Type': 'text/html; charset=utf-8',
        'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer', 'Connection': 'close',
        'Content-Security-Policy': "default-src 'none'; frame-ancestors 'none'", 'X-Content-Type-Options': 'nosniff' });
      response.end('<!doctype html><html lang="en"><title>Invoice Keeper</title><body><p>' + message + '</p></body></html>');
    };
    let url;
    try { url = new URL(request.url, redirectUri); } catch { reply(400, 'Invalid callback.'); return; }
    if (done || request.method !== 'GET' || request.headers.host !== new URL(redirectUri).host
        || !request.url.startsWith('/oidc/callback?') || url.pathname !== '/oidc/callback'
        || [...url.searchParams.keys()].some((key) => !['state', 'code', 'error'].includes(key))
        || ['state', 'code', 'error'].some((key) => url.searchParams.getAll(key).length > 1)
        || !equalState(url.searchParams.get('state'), state)) {
      reply(400, 'Invalid callback.'); return;
    }
    const code = url.searchParams.get('code');
    if (url.searchParams.has('error') || !/^[A-Za-z0-9_-]{64}$/.test(code ?? '')) {
      reply(400, 'Sign-in failed. Restart the MCP connection.');
      finish(new Error('Sign-in denied')); return;
    }
    reply(200, 'Sign-in received. Return to your assistant. You may close this window.');
    finish(null, code);
  });
  server.requestTimeout = 5000;
  server.headersTimeout = 5000;
  server.maxConnections = 8;
  const finish = (error, code) => {
    if (done) return;
    done = true;
    clearTimeout(timer);
    signal?.removeEventListener('abort', abort);
    server.close();
    settle(error, code);
  };
  const abort = () => finish(new Error('Sign-in aborted'));
  server.on('error', () => finish(new Error('Cannot bind login callback')));
  try {
    await new Promise((resolve, reject) => {
      server.once('error', reject);
      server.listen(0, '127.0.0.1', () => { server.removeListener('error', reject); resolve(); });
    });
    redirectUri = `http://127.0.0.1:${server.address().port}/oidc/callback`;
    signal?.addEventListener('abort', abort, { once: true });
    if (signal?.aborted) abort();
    if (!done) {
      timer = setTimeout(() => finish(new Error('Sign-in timed out')), timeoutMs);
      const url = new URL('/api/v1/oidc/mcp/login', apiUrl);
      url.search = new URLSearchParams({ redirect_uri: redirectUri, code_challenge: challenge, state }).toString();
      // This initiation URL has no session token, authorization code, verifier or client secret.
      writeError(`Invoice Keeper: complete corporate sign-in in a browser on this computer (within five minutes).\nIf it does not open, visit:\n${url.href}\n`);
      try { Promise.resolve(openBrowser(url.href)).catch(() => {}); } catch { /* manual fallback */ }
    }
    const code = await completed;
    return { code, code_verifier: verifier, redirect_uri: redirectUri };
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener('abort', abort);
    server.close();
    server.closeAllConnections();
  }
}
