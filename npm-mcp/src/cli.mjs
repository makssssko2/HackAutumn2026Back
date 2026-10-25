#!/usr/bin/env node
import { serveStdio } from '@modelcontextprotocol/server/stdio';
import { loadCliConfig } from './config.mjs';
import { createServer } from './server.mjs';

try {
  const { help, config } = loadCliConfig();
  if (help) {
    console.log('Invoice Keeper MCP (stdio). Requires Node.js 22.18+.\nUsage: invoice-keeper-mcp [--env-file PATH]\nSet IK_MCP_API_URL and IK_MCP_AUTH=oidc for corporate browser sign-in; otherwise IK_MCP_LOGIN and IK_MCP_PASSWORD. Optionally IK_MCP_READ_ONLY=true.');
  } else {
    const { server, api } = createServer(config);
    const handle = serveStdio(() => server, { onerror: () => console.error('MCP protocol error.') });
    let shuttingDown = false;
    const shutdown = async () => {
      if (shuttingDown) return;
      shuttingDown = true;
      await api.close();
      await handle.close();
    };
    process.stdin.once('end', () => { void shutdown(); });
    process.once('SIGINT', () => { void shutdown(); });
    process.once('SIGTERM', () => { void shutdown(); });
  }
} catch {
  // Avoid printing exceptions containing configuration input or credentials.
  console.error('Cannot start Invoice Keeper MCP. Check Node.js version, --env-file and IK_MCP_* configuration.');
  process.exitCode = 2;
}
