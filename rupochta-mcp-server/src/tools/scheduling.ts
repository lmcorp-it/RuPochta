/**
 * Scheduled sends and snoozed messages.
 */

import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import type { RuPochtaClient } from "../services/client.js";
import { formatDate, guard, inline, renderList, renderSingle } from "../services/format.js";
import { normalizeScheduledSend, normalizeSnoozed, num } from "../services/normalize.js";
import {
  folderField,
  isoDateTimeField,
  responseFormatField,
  uidListField,
} from "../schemas/common.js";
import type {
  RawScheduledSend,
  RawSnoozedMessage,
  ScheduledSend,
  SnoozedMessage,
} from "../types.js";

const ListInput = z.object({ response_format: responseFormatField }).strict();

const IdInput = z
  .object({
    id: z.number().int().min(1).describe("Row id from the listing tool"),
  })
  .strict();

const SnoozeInput = z
  .object({
    uids: uidListField,
    wake_at: isoDateTimeField.describe(
      "When the messages should return to the inbox, ISO-8601, at least one minute ahead, e.g. '2026-09-01T08:00:00Z'",
    ),
    folder: folderField,
    response_format: responseFormatField,
  })
  .strict();

const ScheduledShape = {
  id: z.number(),
  subject: z.string(),
  to: z.array(z.string()),
  scheduled_at: z.string(),
  status: z.string(),
  attempts: z.number(),
  last_error: z.string(),
  sent_at: z.string(),
};

const SnoozedShape = {
  id: z.number(),
  subject: z.string(),
  from: z.string(),
  original_folder: z.string(),
  wake_at: z.string(),
  status: z.string(),
};

export function registerSchedulingTools(server: McpServer, client: RuPochtaClient): void {
  server.registerTool(
    "rupochta_list_scheduled",
    {
      title: "List scheduled sends",
      description: `List the mailbox's send-later queue, including messages whose delivery failed.

Statuses: 'pending' (waiting), 'sending', 'sent', 'failed' (SMTP refused it; retry with rupochta_retry_scheduled), 'cancelled'. Failed and pending rows sort first. A send that failed in rupochta_send_message also lands here, so this is where to look after a 502 from sending.

Args:
  - response_format ('markdown' | 'json'): output format (default: 'markdown')

Returns:
  {
    "count": number,
    "items": [
      {
        "id": number,            // pass to rupochta_cancel_scheduled / rupochta_retry_scheduled
        "subject": string,
        "to": string[],
        "scheduled_at": string,  // ISO timestamp
        "status": string,
        "attempts": number,
        "last_error": string,
        "sent_at": string
      }
    ]
  }

Examples:
  - Use when: "Did my scheduled email go out?"
  - Use when: a send returned 502 and you need the queue id to retry
  - Don't use when: looking for drafts (those live in the Drafts folder)

Error Handling:
  - An empty list simply means nothing is queued`,
      inputSchema: ListInput,
      outputSchema: {
        count: z.number(),
        truncated: z.boolean().optional(),
        truncation_message: z.string().optional(),
        items: z.array(z.object(ScheduledShape)),
      },
      annotations: {
        readOnlyHint: true,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: true,
      },
    },
    guard(async (args: z.infer<typeof ListInput>) => {
      const payload = await client.request<{ items?: RawScheduledSend[] }>("/api/scheduled");
      const items = (payload.items ?? []).map(normalizeScheduledSend);
      if (!items.length) {
        return renderSingle({
          format: args.response_format,
          structured: { count: 0, items: [] },
          markdown: () => "Nothing in the send-later queue.",
        });
      }
      return renderList<ScheduledSend>({
        format: args.response_format,
        itemsKey: "items",
        items,
        meta: {},
        markdown: (rows) =>
          [
            `# Scheduled sends (${rows.length})`,
            "",
            "| id | When | Status | To | Subject |",
            "| --- | --- | --- | --- | --- |",
            ...rows.map(
              (row) =>
                `| ${row.id} | ${formatDate(row.scheduled_at)} | ${row.status}${row.attempts ? ` (${row.attempts} attempts)` : ""} | ${inline(row.to.join(", "), 40)} | ${inline(row.subject, 50)} |`,
            ),
            ...(rows.some((row) => row.last_error)
              ? [
                  "",
                  "**Errors:**",
                  ...rows
                    .filter((row) => row.last_error)
                    .map((row) => `- #${row.id}: ${inline(row.last_error, 200)}`),
                ]
              : []),
          ].join("\n"),
      });
    }),
  );

  server.registerTool(
    "rupochta_cancel_scheduled",
    {
      title: "Cancel a scheduled send",
      description: `Cancel a pending send-later message so it is never delivered.

Only rows in status 'pending' can be cancelled — once RuPochta has started delivering, the message is gone.

Args:
  - id (number): queue id from rupochta_list_scheduled

Returns:
  { "ok": boolean, "cancelled": number }

Examples:
  - Use when: "Cancel the email queued for tomorrow" -> id from the listing
  - Don't use when: status is already 'sent' — nothing can be undone at that point

Error Handling:
  - HTTP 404: no pending row with that id (already sent, already cancelled, or wrong id)`,
      inputSchema: IdInput,
      outputSchema: { ok: z.boolean(), cancelled: z.number() },
      annotations: {
        readOnlyHint: false,
        destructiveHint: true,
        idempotentHint: true,
        openWorldHint: true,
      },
    },
    guard(async (args: z.infer<typeof IdInput>) => {
      const payload = await client.request<{ ok?: boolean; cancelled?: number }>(
        `/api/scheduled/${args.id}`,
        { method: "DELETE" },
      );
      const structured = { ok: Boolean(payload.ok ?? true), cancelled: num(payload.cancelled, args.id) };
      return renderSingle({
        format: "markdown",
        structured,
        markdown: () => `Cancelled scheduled send #${structured.cancelled}; it will not be delivered.`,
      });
    }),
  );

  server.registerTool(
    "rupochta_retry_scheduled",
    {
      title: "Retry a failed send",
      description: `Retry a queued message whose delivery failed, using the current session's credentials.

This is the recovery path after rupochta_send_message returns 502 with queued_failed_send=true: the message body is preserved server-side and re-submitted to SMTP immediately.

Args:
  - id (number): queue id of a row in status 'failed'

Returns:
  {
    "ok": boolean,
    "id": number,
    "smtp_ok": boolean,
    "sent_copy_ok": boolean,
    "sent_folder": string,
    "warnings": string[]
  }

Examples:
  - Use when: a send failed and the mail path has since recovered
  - Don't use when: status is 'pending' — it will go out on its own at the scheduled time

Error Handling:
  - HTTP 502: SMTP refused again; the row stays failed and can be retried later`,
      inputSchema: IdInput,
      outputSchema: {
        ok: z.boolean(),
        id: z.number(),
        smtp_ok: z.boolean(),
        sent_copy_ok: z.boolean(),
        sent_folder: z.string(),
        warnings: z.array(z.string()),
      },
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: false,
        openWorldHint: true,
      },
    },
    guard(async (args: z.infer<typeof IdInput>) => {
      const payload = await client.request<{
        ok?: boolean;
        id?: number;
        smtp_ok?: boolean;
        sent_copy_ok?: boolean;
        sent_folder?: string;
        warnings?: string[];
      }>(`/api/scheduled/${args.id}/retry`, { method: "POST" });
      const structured = {
        ok: Boolean(payload.ok ?? true),
        id: num(payload.id, args.id),
        smtp_ok: Boolean(payload.smtp_ok),
        sent_copy_ok: Boolean(payload.sent_copy_ok),
        sent_folder: payload.sent_folder ?? "",
        warnings: Array.isArray(payload.warnings) ? payload.warnings.map(String) : [],
      };
      return renderSingle({
        format: "markdown",
        structured,
        markdown: () =>
          `Retried queued message #${structured.id}: SMTP ${structured.smtp_ok ? "accepted" : "did not accept"} it` +
          (structured.sent_copy_ok ? `, copy stored in ${structured.sent_folder}.` : "."),
      });
    }),
  );

  server.registerTool(
    "rupochta_snooze_messages",
    {
      title: "Snooze messages until later",
      description: `Move messages out of the inbox into the Snoozed folder and bring them back at a set time.

RuPochta's background worker returns each message to its original folder at 'wake_at'. Use this to defer mail an agent should not act on yet.

Args:
  - uids (string[]): 1-200 numeric UIDs from the same folder
  - wake_at (string): ISO-8601 return time, at least one minute in the future
  - folder (string): folder the UIDs are in (default 'INBOX')
  - response_format ('markdown' | 'json'): output format (default: 'markdown')

Returns:
  { "ok": boolean, "count": number, "wake_at": string }

Examples:
  - Use when: "Hide this until Monday" -> wake_at='2026-09-07T06:00:00Z'
  - Don't use when: the message should simply be filed (use rupochta_move_message)

Error Handling:
  - HTTP 400: wake_at is in the past or under a minute away, or uids is empty
  - HTTP 502: the Snoozed folder could not be created or written`,
      inputSchema: SnoozeInput,
      outputSchema: { ok: z.boolean(), count: z.number(), wake_at: z.string() },
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: false,
        openWorldHint: true,
      },
    },
    guard(async (args: z.infer<typeof SnoozeInput>) => {
      const payload = await client.request<{ ok?: boolean; count?: number; wake_at?: string }>(
        "/api/snooze",
        {
          method: "POST",
          body: { folder: args.folder, uids: args.uids, wake_at: args.wake_at },
        },
      );
      const structured = {
        ok: Boolean(payload.ok ?? true),
        count: num(payload.count, args.uids.length),
        wake_at: payload.wake_at ?? args.wake_at,
      };
      return renderSingle({
        format: args.response_format,
        structured,
        markdown: () =>
          `Snoozed ${structured.count} message(s) from ${args.folder}; they return at ${formatDate(structured.wake_at)}.`,
      });
    }),
  );

  server.registerTool(
    "rupochta_list_snoozed",
    {
      title: "List snoozed messages",
      description: `List messages waiting in the Snoozed folder and when each one comes back.

Args:
  - response_format ('markdown' | 'json'): output format (default: 'markdown')

Returns:
  {
    "count": number,
    "items": [
      {
        "id": number,               // pass to rupochta_unsnooze
        "subject": string,
        "from": string,
        "original_folder": string,  // where it will be restored to
        "wake_at": string,
        "status": string            // 'pending', 'waking', 'woken', 'cancelled'
      }
    ]
  }

Examples:
  - Use when: "What have I snoozed?"
  - Use when: you need an id to bring a message back early

Error Handling:
  - An empty list means nothing is snoozed`,
      inputSchema: ListInput,
      outputSchema: {
        count: z.number(),
        truncated: z.boolean().optional(),
        truncation_message: z.string().optional(),
        items: z.array(z.object(SnoozedShape)),
      },
      annotations: {
        readOnlyHint: true,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: true,
      },
    },
    guard(async (args: z.infer<typeof ListInput>) => {
      const payload = await client.request<{ items?: RawSnoozedMessage[] }>("/api/snoozed");
      const items = (payload.items ?? []).map(normalizeSnoozed);
      if (!items.length) {
        return renderSingle({
          format: args.response_format,
          structured: { count: 0, items: [] },
          markdown: () => "No snoozed messages.",
        });
      }
      return renderList<SnoozedMessage>({
        format: args.response_format,
        itemsKey: "items",
        items,
        meta: {},
        markdown: (rows) =>
          [
            `# Snoozed messages (${rows.length})`,
            "",
            "| id | Wakes | Status | From | Subject | Returns to |",
            "| --- | --- | --- | --- | --- | --- |",
            ...rows.map(
              (row) =>
                `| ${row.id} | ${formatDate(row.wake_at)} | ${row.status} | ${inline(row.from, 35)} | ${inline(row.subject, 45)} | ${row.original_folder} |`,
            ),
          ].join("\n"),
      });
    }),
  );

  server.registerTool(
    "rupochta_unsnooze",
    {
      title: "Wake a snoozed message now",
      description: `Bring a snoozed message back immediately instead of waiting for its wake time.

The message returns to the folder it was snoozed from and the queue row is marked cancelled.

Args:
  - id (number): row id from rupochta_list_snoozed

Returns:
  { "ok": boolean, "id": number }

Examples:
  - Use when: "Bring back the message I snoozed until Monday"
  - Don't use when: the row already shows status 'woken'

Error Handling:
  - HTTP 404: no pending snooze with that id
  - HTTP 502: the message was not found in Snoozed (someone moved it manually)`,
      inputSchema: IdInput,
      outputSchema: { ok: z.boolean(), id: z.number() },
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: true,
      },
    },
    guard(async (args: z.infer<typeof IdInput>) => {
      const payload = await client.request<{ ok?: boolean }>(`/api/snoozed/${args.id}`, {
        method: "DELETE",
      });
      const structured = { ok: Boolean(payload.ok ?? true), id: args.id };
      return renderSingle({
        format: "markdown",
        structured,
        markdown: () => `Un-snoozed message #${args.id}; it is back in its original folder.`,
      });
    }),
  );
}
