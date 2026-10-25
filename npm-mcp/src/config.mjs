import { existsSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { parseArgs, parseEnv } from 'node:util';

export class ConfigurationError extends Error {
  constructor() {
    super('Invalid MCP configuration. Check IK_MCP_API_URL, IK_MCP_AUTH, IK_MCP_LOGIN, IK_MCP_PASSWORD, IK_MCP_READ_ONLY and IK_MCP_TIMEOUT_SECONDS.');
  }
}

export function readConfig(env = process.env) {
  try {
    const rawUrl = env.IK_MCP_API_URL || 'http://127.0.0.1:8000';
    const url = new URL(rawUrl);
    const loopback = url.hostname === 'localhost' || url.hostname === '[::1]' || /^127(?:\.\d{1,3}){3}$/.test(url.hostname);
    if (!/^https?:\/\/[^/?#\s@]+\/?$/.test(rawUrl) || !url.hostname || url.username || url.password
        || (url.protocol === 'http:' && !loopback) || url.pathname !== '/' || url.search || url.hash) throw new ConfigurationError();
    const login = env.IK_MCP_LOGIN;
    const password = env.IK_MCP_PASSWORD;
    const auth = env.IK_MCP_AUTH || 'password';
    if (!['password', 'oidc'].includes(auth)) throw new ConfigurationError();
    if (auth === 'password' && (!login || login.length > 80 || !password || password.length > 200)) throw new ConfigurationError();
    const readOnly = (env.IK_MCP_READ_ONLY || 'false').toLowerCase();
    if (!['true', 'false', '1', '0'].includes(readOnly)) throw new ConfigurationError();
    const timeoutSeconds = Number(env.IK_MCP_TIMEOUT_SECONDS ?? 30);
    if (!Number.isFinite(timeoutSeconds) || timeoutSeconds <= 0 || timeoutSeconds > 120) throw new ConfigurationError();
    return { apiUrl: url.origin, auth, ...(auth === 'password' ? { login, password } : {}), readOnly: ['true', '1'].includes(readOnly), timeoutMs: Math.ceil(timeoutSeconds * 1000) };
  } catch {
    // Never print parser errors: they may contain credential input values.
    throw new ConfigurationError();
  }
}

export function loadCliConfig(argv = process.argv.slice(2), env = process.env, cwd = process.cwd()) {
  let values;
  try {
    ({ values } = parseArgs({ args: argv, options: { 'env-file': { type: 'string' }, help: { type: 'boolean', short: 'h' } } }));
  } catch {
    throw new ConfigurationError();
  }
  if (values.help) return { help: true };
  const file = values['env-file'] ? resolve(cwd, values['env-file']) : resolve(cwd, '.env.mcp');
  let fromFile = {};
  try {
    if (values['env-file'] || existsSync(file)) fromFile = parseEnv(readFileSync(file, 'utf8'));
  } catch {
    throw new ConfigurationError();
  }
  return { config: readConfig({ ...fromFile, ...env }) };
}
