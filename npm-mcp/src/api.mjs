import { browserLogin } from './oidc.mjs';

export class ApiError extends Error {}

const statusMessages = {
  401: 'Authentication failed or session expired/revoked. Check credentials and restart the MCP process.',
  403: 'This operation requires a role/permission your account does not have.',
  404: 'The requested object was not found.',
  409: 'State conflict. Read the object again and review its current revision; do not replay the write automatically.',
  422: 'The API rejected the data. Check dates, amount, recipients and operation prerequisites.',
  429: 'API rate limit reached. Wait before trying again.',
};

export class InvoiceKeeperApi {
  #cookie;
  #loginPromise;
  #authFailed = false;
  #closed = false;
  #stop = new AbortController();

  constructor(config, fetchImpl = fetch, loginImpl = browserLogin) {
    this.config = config;
    this.fetch = fetchImpl;
    this.browserLogin = loginImpl;
  }

  async #send(method, path, { body, params, shutdown = false } = {}) {
    const url = new URL('/api/v1' + path, this.config.apiUrl);
    for (const [key, value] of Object.entries(params ?? {})) {
      if (value !== null && value !== undefined) url.searchParams.set(key, String(value));
    }
    const timeout = AbortSignal.timeout(shutdown ? 3000 : this.config.timeoutMs);
    const signal = shutdown ? timeout : AbortSignal.any([timeout, this.#stop.signal]);
    let response;
    try {
      response = await this.fetch(url, {
        method, redirect: 'manual', signal,
        headers: { Accept: 'application/json', ...(body === undefined ? {} : { 'Content-Type': 'application/json' }), ...(this.#cookie ? { Cookie: this.#cookie } : {}) },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      });
    } catch {
      throw new ApiError('API connection failed or timed out. The result of a write may be unknown; inspect current state before retrying. No automatic retry was made.');
    }
    if (response.status === 401) {
      this.#authFailed = true;
      this.#cookie = undefined;
    }
    if (!response.ok) {
      // Never expose upstream response bodies, cookies or URLs in tool errors.
      await response.body?.cancel().catch(() => {});
      throw new ApiError(`HTTP ${response.status}: ${statusMessages[response.status] ?? 'Unexpected API response. Inspect state before retrying a write.'}`);
    }
    let result;
    try {
      result = await response.json();
    } catch {
      throw new ApiError('The API returned invalid JSON. Inspect state before retrying a write.');
    }
    if (path === '/auth/login' || path === '/oidc/mcp/exchange') {
      const cookies = response.headers.getSetCookie();
      const session = cookies.find((cookie) => cookie.startsWith('ik_session='));
      if (!session || !/^ik_session=[A-Za-z0-9_-]+(?:;|$)/.test(session)
          || (this.config.apiUrl.startsWith('http:') && /;\s*secure(?:;|$)/i.test(session))) {
        this.#authFailed = true;
        throw new ApiError('The API did not issue a usable session cookie. Check the API URL and HTTPS configuration.');
      }
      this.#cookie = session.split(';', 1)[0];
    }
    return result;
  }

  async #login() {
    if (this.#authFailed) throw new ApiError('Authentication failed or session revoked. Restart the MCP process to sign in again.');
    if (this.#cookie) return;
    if (!this.#loginPromise) {
      this.#loginPromise = this.config.auth === 'oidc' ? this.#oidcLogin()
        : this.#send('POST', '/auth/login', { body: { login: this.config.login, password: this.config.password } });
    }
    const pending = this.#loginPromise;
    try { await pending; }
    finally { if (this.#loginPromise === pending) this.#loginPromise = undefined; }
  }

  async #oidcLogin() {
    try {
      const proof = await this.browserLogin(this.config.apiUrl, { signal: this.#stop.signal });
      if (this.#closed) throw new Error('closed');
      await this.#send('POST', '/oidc/mcp/exchange', { body: proof });
    } catch {
      this.#authFailed = true;
      throw new ApiError('Corporate sign-in failed, was cancelled or timed out. Restart the MCP process to sign in again.');
    }
  }

  async request(method, path, options = {}) {
    if (this.#closed) throw new ApiError('The MCP connection is closed.');
    if (this.config.readOnly && (method !== 'GET' || options.mutating)) throw new ApiError('This MCP process is configured for read-only access.');
    await this.#login();
    if (this.#closed) throw new ApiError('The MCP connection is closed.');
    return this.#send(method, path, options);
  }

  async close() {
    if (this.#closed) return;
    this.#closed = true;
    this.#stop.abort();
    await this.#loginPromise?.catch(() => {});
    try {
      if (this.#cookie) await this.#send('POST', '/auth/logout', { shutdown: true });
    } catch { /* Best effort revocation; ordinary API expiry applies after an uncertain shutdown. */ }
    finally { this.#cookie = undefined; }
  }
}
