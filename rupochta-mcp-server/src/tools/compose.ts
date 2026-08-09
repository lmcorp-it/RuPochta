/**
 * Composing mail: send now, save a draft, send later.
 */

import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import type { RuPochtaClient } from "../services/client.js";
import { guard, keyValueLines, renderSingle } from "../services/format.js";
import { num, str, strList } from "../services/normalize.js";
import {
  emailListField,
  folderField,
  isoDateTimeField,
  mailboxContextField,
  responseFormatField,
  uidField,
} from "../schemas/common.js";

const bodyField = z
  .string()
  .max(500000, "body must not exceed 500000 characters")
  .describe(
    "Message body. Plain text is fine — RuPochta detects whether the value is HTML and builds both " +
      "text and HTML parts either way",
  );

const SendInput = z
  .object({
    to: emailListField.min(3, "at least one recipient is required").describe(
      "Recipients, comma-separated: 'alice@example.com, Bob <bob@example.com>'",
    ),
    subject: z.string().max(998, "subject must not exceed 998 characters").describe("Subject line"),
    body: bodyField,
    cc: emailListField.default("").describe("Cc recipients, comma-separated (optional)"),
    bcc: emailListField.default("").describe("Bcc recipients, comma-separated (optional)"),
    body_text: z
      .string()
      .max(500000)
      .optional()
      .describe("Explicit plain-text alternative. Only needed when 'body' is HTML and the fallback matters"),
    in_reply_to: z
      .string()
      .optional()
      .describe(
        "Message-ID being replied to, from rupochta_get_message. Sets the In-Reply-To header so mail clients thread the reply",
      ),
    draft_uid: uidField
      .optional()
      .describe("UID of a draft in Drafts to delete once the message goes out"),
    mailbox_context: mailboxContextField,
    response_format: responseFormatField,
  })
  .strict();

const DraftInput = z
  .object({
    to: emailListField.default("").describe("Recipients, comma-separated (may be empty in a draft)"),
    subject: z.string().max(998).default("").describe("Subject line"),
    body: bodyField.default(""),
    cc: emailListField.default("").describe("Cc recipients, comma-separated"),
    bcc: emailListField.default("").describe("Bcc recipients, comma-separated"),
    in_reply_to: z.string().optional().describe("Message-ID this draft replies to"),
    replace_uid: uidField
      .optional()
      .describe("UID of an existing draft to replace — the old copy is removed after the new one is stored"),
    mailbox_context: mailboxContextField,
  })
  .strict();

const ScheduleInput = z
  .object({
    to: emailListField.min(3, "at least one recipient is required").describe("Recipients, comma-separated"),
    subject: z.string().max(998).describe("Subject line"),
    body: bodyField,
    scheduled_at: isoDateTimeField.describe(
      "When to send, ISO-8601, at least 10 seconds ahead and at most one year out, e.g. '2026-09-01T09:30:00Z'. " +
        "A value without a timezone is read as UTC",
    ),
    cc: emailListField.default("").describe("Cc recipients, comma-separated"),
    bcc: emailListField.default("").describe("Bcc recipients, comma-separated"),
    in_reply_to: z.string().optional().describe("Message-ID this message replies to"),
    response_format: responseFormatField,
  })
  .strict();

interface SendResponse {
  ok?: boolean;
  smtp_ok?: boolean;
  smtp_status?: string;
  recipient_count?: number;
  sent_copy_ok?: boolean;
  sent_folder?: string;
  draft_deleted?: boolean | null;
  warnings?: string[];
}

export function registerComposeTools(server: McpServer, client: RuPochtaClient): void {
  server.registerTool(
    "rupochta_send_message",
    {
      title: "Send an email",
      description: `Send an email through the mailbox's own SMTP path and file a copy in Sent.

This delivers mail to real recipients immediately and cannot be recalled — confirm the address list and content before calling it. When SMTP fails, RuPochta queues the message so it can be retried with rupochta_retry_scheduled instead of losing it.

To reply in-thread, pass the original 'message_id' from rupochta_get_message as 'in_reply_to' and prefix the subject with 'Re: ' yourself. Attachments are not supported by this tool.

Args:
  - to (string): recipients, comma-separated — required
  - subject (string): subject line; empty becomes '(без темы)'
  - body (string): message text, plain or HTML
  - cc (string): Cc recipients, comma-separated (default '')
  - bcc (string): Bcc recipients, comma-separated (default '')
  - body_text (string, optional): plain-text alternative when body is HTML
  - in_reply_to (string, optional): Message-ID being replied to
  - draft_uid (string, optional): draft to delete once sent
  - mailbox_context (string): 'personal' (default) or 'shared:<id>' with can_send=true
  - response_format ('markdown' | 'json'): output format (default: 'markdown')

Returns:
  {
    "ok": boolean,
    "smtp_ok": boolean,          // SMTP accepted the message
    "smtp_status": string,       // e.g. "submitted"
    "recipient_count": number,   // envelope recipients
    "sent_copy_ok": boolean,     // copy stored in the Sent folder
    "sent_folder": string,
    "draft_deleted": boolean | null,
    "warnings": string[]         // non-fatal issues, e.g. Sent copy missing
  }

Examples:
  - Use when: "Email alice@example.com the meeting notes" -> to='alice@example.com', subject='Meeting notes', body='...'
  - Use when: replying -> in_reply_to=<original message_id>, subject='Re: ...'
  - Don't use when: the message should go out later (use rupochta_schedule_send)
  - Don't use when: you are drafting for review (use rupochta_save_draft)

Error Handling:
  - HTTP 400: empty recipient list
  - HTTP 502 with queued_failed_send=true: SMTP refused delivery and the message was queued; inspect rupochta_list_scheduled
  - HTTP 403 in a shared mailbox: this account may not send from that address`,
      inputSchema: SendInput,
      outputSchema: {
        ok: z.boolean(),
        smtp_ok: z.boolean(),
        smtp_status: z.string(),
        recipient_count: z.number(),
        sent_copy_ok: z.boolean(),
        sent_folder: z.string(),
        draft_deleted: z.boolean().nullable(),
        warnings: z.array(z.string()),
      },
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: false,
        openWorldHint: true,
      },
    },
    guard(async (args: z.infer<typeof SendInput>) => {
      const payload = await client.request<SendResponse>("/api/messages/send", {
        method: "POST",
        body: {
          to: args.to,
          cc: args.cc,
          bcc: args.bcc,
          subject: args.subject,
          body: args.body,
          ...(args.body_text !== undefined ? { body_text: args.body_text } : {}),
          ...(args.in_reply_to ? { in_reply_to: args.in_reply_to } : {}),
          ...(args.draft_uid ? { draft_uid: args.draft_uid } : {}),
        },
        mailboxContext: args.mailbox_context,
      });
      const structured = {
        ok: Boolean(payload.ok ?? true),
        smtp_ok: Boolean(payload.smtp_ok),
        smtp_status: str(payload.smtp_status, "submitted"),
        recipient_count: num(payload.recipient_count),
        sent_copy_ok: Boolean(payload.sent_copy_ok),
        sent_folder: str(payload.sent_folder),
        draft_deleted: payload.draft_deleted ?? null,
        warnings: strList(payload.warnings),
      };
      return renderSingle({
        format: args.response_format,
        structured,
        markdown: () =>
          [
            `# Sent: ${args.subject || "(no subject)"}`,
            "",
            ...keyValueLines([
              ["To", args.to],
              ["Cc", args.cc],
              ["Recipients accepted", structured.recipient_count],
              ["SMTP", structured.smtp_ok ? `ok (${structured.smtp_status})` : structured.smtp_status],
              [
                "Copy in Sent",
                structured.sent_copy_ok ? structured.sent_folder || "yes" : "not stored",
              ],
            ]),
            ...(structured.warnings.length
              ? ["", "**Warnings:**", ...structured.warnings.map((warning) => `- ${warning}`)]
              : []),
          ].join("\n"),
      });
    }),
  );

  server.registerTool(
    "rupochta_save_draft",
    {
      title: "Save a draft",
      description: `Store a message in the Drafts folder without sending it.

Nothing leaves the mailbox. Use this to prepare a message for a human to review, or to park work in progress. Pass 'replace_uid' to update a draft you saved earlier instead of piling up copies.

Args:
  - to (string): recipients, comma-separated (may be empty)
  - subject (string): subject line (may be empty)
  - body (string): message text, plain or HTML
  - cc (string), bcc (string): additional recipients, comma-separated
  - in_reply_to (string, optional): Message-ID this draft replies to
  - replace_uid (string, optional): UID of the draft this one supersedes
  - mailbox_context (string): 'personal' (default) or 'shared:<id>'

Returns:
  { "ok": boolean, "uid": string, "folder": string }   // uid identifies the stored draft

Examples:
  - Use when: "Draft a reply for me to review" -> save it, then report the UID
  - Use when: iterating on wording -> pass replace_uid with the previous draft's UID
  - Don't use when: the message should actually go out (use rupochta_send_message)

Error Handling:
  - HTTP 502 "Drafts: ...": the Drafts folder could not be written; check rupochta_list_folders`,
      inputSchema: DraftInput,
      outputSchema: {
        ok: z.boolean(),
        uid: z.string(),
        folder: z.string(),
      },
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: false,
        openWorldHint: true,
      },
    },
    guard(async (args: z.infer<typeof DraftInput>) => {
      const payload = await client.request<{ ok?: boolean; uid?: string; folder?: string }>(
        "/api/messages/draft",
        {
          method: "POST",
          body: {
            to: args.to,
            cc: args.cc,
            bcc: args.bcc,
            subject: args.subject,
            body: args.body,
            ...(args.in_reply_to ? { in_reply_to: args.in_reply_to } : {}),
            ...(args.replace_uid ? { replace_uid: args.replace_uid } : {}),
          },
          mailboxContext: args.mailbox_context,
        },
      );
      const structured = {
        ok: Boolean(payload.ok ?? true),
        uid: str(payload.uid),
        folder: str(payload.folder, "Drafts"),
      };
      return renderSingle({
        format: "markdown",
        structured,
        markdown: () =>
          `Saved draft "${args.subject || "(no subject)"}" in ${structured.folder}` +
          (structured.uid ? ` as UID ${structured.uid}.` : "."),
      });
    }),
  );

  server.registerTool(
    "rupochta_schedule_send",
    {
      title: "Schedule an email for later",
      description: `Queue an email for delivery at a future time (RuPochta's "send later").

The message is stored server-side and delivered by RuPochta's background worker at 'scheduled_at'. Until then it can be cancelled with rupochta_cancel_scheduled. Scheduling only works on the personal mailbox.

Args:
  - to (string): recipients, comma-separated — required
  - subject (string): subject line
  - body (string): message text, plain or HTML
  - scheduled_at (string): ISO-8601 send time, ≥10 seconds ahead and ≤1 year out; no timezone means UTC
  - cc (string), bcc (string): additional recipients, comma-separated
  - in_reply_to (string, optional): Message-ID this message replies to
  - response_format ('markdown' | 'json'): output format (default: 'markdown')

Returns:
  {
    "ok": boolean,
    "id": number,               // queue id — pass to rupochta_cancel_scheduled
    "scheduled_at": string,     // normalised UTC timestamp
    "to": string[],             // parsed recipients
    "subject": string
  }

Examples:
  - Use when: "Send this tomorrow at 9" -> scheduled_at='2026-09-01T09:00:00Z'
  - Use when: avoiding out-of-hours delivery
  - Don't use when: the message should go now (use rupochta_send_message)

Error Handling:
  - HTTP 400: scheduled_at is unparseable, in the past, under 10 seconds away, or more than a year out
  - HTTP 400: empty recipient list`,
      inputSchema: ScheduleInput,
      outputSchema: {
        ok: z.boolean(),
        id: z.number(),
        scheduled_at: z.string(),
        to: z.array(z.string()),
        subject: z.string(),
      },
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: false,
        openWorldHint: true,
      },
    },
    guard(async (args: z.infer<typeof ScheduleInput>) => {
      const payload = await client.request<{
        ok?: boolean;
        id?: number;
        scheduled_at?: string;
        to?: string[];
        subject?: string;
      }>("/api/scheduled/send", {
        method: "POST",
        body: {
          to: args.to,
          cc: args.cc,
          bcc: args.bcc,
          subject: args.subject,
          body: args.body,
          scheduled_at: args.scheduled_at,
          ...(args.in_reply_to ? { in_reply_to: args.in_reply_to } : {}),
        },
      });
      const structured = {
        ok: Boolean(payload.ok ?? true),
        id: num(payload.id),
        scheduled_at: str(payload.scheduled_at, args.scheduled_at),
        to: strList(payload.to),
        subject: str(payload.subject, args.subject),
      };
      return renderSingle({
        format: args.response_format,
        structured,
        markdown: () =>
          `Queued "${structured.subject}" for ${structured.scheduled_at} (queue id ${structured.id}). ` +
          "Cancel with rupochta_cancel_scheduled while it is still pending.",
      });
    }),
  );
}
