import { readFileSync } from 'node:fs';
import { McpServer, ResourceTemplate, fromJsonSchema } from '@modelcontextprotocol/server';
import * as z from 'zod/v4';
import { ApiError, InvoiceKeeperApi } from './api.mjs';

export const contract = JSON.parse(readFileSync(new URL('./contract.json', import.meta.url), 'utf8'));
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function dates(args) {
  if (args.date_from && args.date_to && args.date_from > args.date_to) throw new ApiError('date_from must be on or before date_to.');
  return { date_from: args.date_from, date_to: args.date_to };
}

function collection(items, limit) {
  if (!Array.isArray(items)) throw new ApiError('Unexpected API collection response.');
  return { items, count: items.length, ...(limit === undefined ? {} : { limit, possibly_truncated: items.length >= limit }) };
}

export function createServer(config, options = {}) {
  const api = new InvoiceKeeperApi(config, options.fetchImpl);
  const server = new McpServer({ name: 'Invoice Keeper', version: '1.0.0' }, { instructions: contract.instructions });
  const handlers = {
    get_current_user: () => api.request('GET', '/auth/me'),
    list_invoices: async (args) => {
      const params = { ...args, ...dates(args), limit: args.limit ?? 100 };
      return collection(await api.request('GET', '/payment-occurrences', { params }), params.limit);
    },
    get_invoice: ({ occurrence_id }) => api.request('GET', `/payment-occurrences/${occurrence_id}`),
    list_recurring_payments: async () => collection(await api.request('GET', '/recurring-payments')),
    get_summary: (args) => api.request('GET', '/payments/summary', { params: dates(args) }),
    list_groups: async () => collection(await api.request('GET', '/groups')),
    list_channels: async () => {
      const rows = await api.request('GET', '/channels');
      const fields = ['id', 'name', 'kind', 'active', 'connection_id', 'remote_chat_id'];
      return collection(rows.map((row) => Object.fromEntries(fields.filter((key) => key in row).map((key) => [key, row[key]]))));
    },
    list_deliveries: async () => collection(await api.request('GET', '/notifications'), 300),
    list_audit: async () => collection(await api.request('GET', '/audit'), 500),
    get_budget: ({ year }) => api.request('GET', '/payments/budget', { params: { year }, mutating: true }),
    create_one_time_payment: ({ payment }) => api.request('POST', '/one-time-payments', { body: { description: '', group_ids: [], ...payment } }),
    create_recurring_payment: ({ payment }) => api.request('POST', '/recurring-payments', { body: { description: '', group_ids: [], frequency: 'monthly', ...payment } }),
    set_invoice_payment: ({ occurrence_id, paid, expected_revision, paid_at = null }) => {
      if (!paid && paid_at !== null) throw new ApiError('paid_at must be omitted when undoing payment.');
      return api.request('PATCH', `/payment-occurrences/${occurrence_id}`, { body: { paid, paid_at, expected_revision, demo: true } });
    },
    stop_recurring_payment: async ({ payment_id }) => {
      const templates = await api.request('GET', '/recurring-payments');
      if (!templates.some((row) => row.id === payment_id)) throw new ApiError('Recurring template not found. Use cancel_one_time_payment for a one-time invoice.');
      return api.request('DELETE', `/recurring-payments/${payment_id}`);
    },
    cancel_one_time_payment: ({ payment_id }) => api.request('DELETE', `/one-time-payments/${payment_id}`),
  };
  for (const tool of contract.tools) {
    if (config.readOnly && !tool.annotations.readOnlyHint) continue;
    server.registerTool(tool.name, {
      description: tool.description, annotations: tool.annotations,
      inputSchema: fromJsonSchema(tool.inputSchema),
      outputSchema: fromJsonSchema(tool.outputSchema),
    }, async (args) => {
      try {
        const data = await handlers[tool.name](args);
        return { content: [{ type: 'text', text: JSON.stringify(data) }], structuredContent: data };
      } catch (error) {
        const message = error instanceof ApiError ? error.message : 'Unexpected MCP adapter error. Inspect state before retrying a write.';
        return { isError: true, content: [{ type: 'text', text: message }] };
      }
    });
  }
  server.registerResource('guide', 'invoicekeeper://guide', { mimeType: 'text/plain' }, (uri) => ({ contents: [{ uri: uri.href, text: contract.instructions, mimeType: 'text/plain' }] }));
  server.registerResource('invoice', new ResourceTemplate('invoicekeeper://invoices/{occurrence_id}', { list: undefined }), { mimeType: 'application/json' }, async (uri, { occurrence_id }) => {
    if (typeof occurrence_id !== 'string' || !uuid.test(occurrence_id)) throw new ApiError('Invalid occurrence ID.');
    return { contents: [{ uri: uri.href, text: JSON.stringify(await handlers.get_invoice({ occurrence_id })), mimeType: 'application/json' }] };
  });
  server.registerPrompt('review_expenses', { description: 'Review expenses over an ISO date interval, keeping each currency separate.', argsSchema: z.object({ date_from: z.iso.date(), date_to: z.iso.date() }) }, (args) => {
    dates(args);
    return { messages: [{ role: 'user', content: { type: 'text', text: `Review Invoice Keeper expenses from ${args.date_from} to ${args.date_to}. Use get_summary and list_invoices; distinguish unpaid debt and paid-late invoices. Keep currencies separate, state list truncation and do not modify invoices.` } }] };
  });
  return { server, api };
}
