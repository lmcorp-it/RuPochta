/**
 * Reading, searching and organising messages.
 */

import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";
import { z } from "zod";
import { MAX_ATTACHMENT_BYTES } from "../constants.js";
import type { RuPochtaClient } from "../services/client.js";
import {
  formatBytes,
  formatDate,
  guard,
  inline,
  keyValueLines,
  paginationMeta,
  renderList,
  renderSingle,
} from "../services/format.js";
import { normalizeMessageDetail, normalizeMessageSummary, num } from "../services/normalize.js";
import { fetchMessageWindow } from "../services/pagination.js";
import {
  folderField,
  limitField,
  mailboxContextField,
  offsetField,
  responseFormatField,
  searchQueryField,
  uidField,
  uidListField,
} from "../schemas/common.js";
import type { MessageDetail, MessageSummary, RawMessageDetail, RawMessageList } from "../types.js";

const MessageSummaryShape = {
  uid: z.string(),
  folder: z.string(),
  from: z.string(),
  to: z.string(),
  subject: z.string(),
  date: z.string(),
  message_id: z.string(),
  unread: z.boolean(),
  flagged: z.boolean(),
  has_attachments: z.boolean(),
  snippet: z.string(),
};

const ListMessagesInput = z
  .object({
    folder: folderField,
    limit: limitField(100, 20, "messages"),
    offset: offsetField,
    query: searchQueryField,
    unread: z
      .boolean()
      .optional()
      .describe("true = only unread, false = only read, omitted = both"),
    flagged: z
      .boolean()
      .optional()
      .describe("true = only starred/flagged, false = only unflagged, omitted = both"),
    has_attachments: z
      .boolean()
      .optional()
      .describe("true = only messages with attachments, false = only messages without, omitted = both"),
    mailbox_context: mailboxContextField,
    response_format: responseFormatField,
  })
  .strict();

const SearchMessagesInput = z
  .object({
    query: searchQueryField.describe(
      "Search text passed to IMAP SEARCH across subject, from, to and body. " +
        "Also accepts 'from:', 'to:', 'subject:' prefixes and the flags 'unread', 'read', 'flagged', " +
        "'unflagged', 'has:attachment', 'has:no-attachment'. Example: 'from:billing@acme.com has:attachment'",
    ),
    folder: folderField,
    limit: limitField(200, 50, "matches"),
    mailbox_context: mailboxContextField,
    response_format: responseFormatField,
  })
  .strict();

const GetMessageInput = z
  .object({
    uid: uidField,
    folder: folderField,
    max_body_chars: z
      .number()
      .int()
      .min(200)
      .max(200000)
      .default(8000)
      .describe("Maximum characters of message body to return before truncating (default 8000)"),
    include_html: z
      .boolean()
      .default(false)
      .describe(
        "Return the sanitised HTML body instead of plain text. Plain text is the default and is what agents usually want",
      ),
    mailbox_context: mailboxContextField,
    response_format: responseFormatField,
  })
  .strict();

const GetAttachmentInput = z
  .object({
    uid: uidField,
    index: z
      .number()
      .int()
      .min(0)
      .describe("Zero-based position of the attachment in the message's attachments[] list"),
    folder: folderField,
    mailbox_context: mailboxContextField,
  })
  .strict();

const SetFlagsInput = z
  .object({
    uid: uidField,
    folder: folderField,
    seen: z.boolean().optional().describe("true marks the message read, false marks it unread"),
    flagged: z.boolean().optional().describe("true stars the message, false removes the star"),
    mailbox_context: mailboxContextField,
  })
  .strict();

const MoveMessageInput = z
  .object({
    uid: uidField,
    from_folder: folderField.describe("Folder the message is in now"),
    to_folder: z
      .string()
      .min(1)
      .max(255)
      .describe("Destination folder; it must already exist (see rupochta_create_folder)"),
    mailbox_context: mailboxContextField,
  })
  .strict();

const DeleteMessageInput = z
  .object({
    uid: uidField,
    folder: folderField,
    mailbox_context: mailboxContextField,
  })
  .strict();

const BulkActionInput = z
  .object({
    uids: uidListField,
    from_folder: folderField.describe("Folder all the UIDs belong to"),
    action: z
      .enum(["move", "delete", "mark_read", "mark_unread", "star", "unstar"])
      .describe("Operation to apply to every UID"),
    to_folder: z
      .string()
      .min(1)
      .max(255)
      .optional()
      .describe("Destination folder — required when action is 'move', ignored otherwise"),
    mailbox_context: mailboxContextField,
  })
  .strict();

const UndoMoveInput = z
  .object({
    message_ids: z
      .array(z.string().min(1))
      .min(1, "at least one message_id is required")
      .max(200)
      .describe("RFC 5322 Message-IDs from the 'undo' payload of a previous move or delete"),
    from_folder: z
      .string()
      .min(1)
      .max(255)
      .describe("Folder the messages are in now — the destination of the move being undone"),
    to_folder: z
      .string()
      .min(1)
      .max(255)
      .describe("Folder to put them back into — the original source folder"),
    mailbox_context: mailboxContextField,
  })
  .strict();

function summaryTable(items: MessageSummary[]): string[] {
  return [
    "| UID | Date | From | Subject | State |",
    "| --- | --- | --- | --- | --- |",
    ...items.map((message) => {
      const state = [
        message.unread ? "unread" : "read",
        message.flagged ? "starred" : "",
        message.has_attachments ? "attachment" : "",
      ]
        .filter(Boolean)
        .join(", ");
      return `| ${message.uid} | ${formatDate(message.date)} | ${inline(message.from, 40)} | ${inline(message.subject, 70)} | ${state} |`;
    }),
  ];
}

function detailMarkdown(message: MessageDetail): string {
  const header = keyValueLines([
    ["UID", `${message.uid} (folder ${message.folder})`],
    ["From", message.from],
    ["To", message.to],
    ["Cc", message.cc],
    ["Date", formatDate(message.date)],
    ["Message-ID", message.message_id],
    ["In-Reply-To", message.in_reply_to],
    ["State", `${message.unread ? "unread" : "read"}${message.flagged ? ", starred" : ""}`],
  ]);
  const attachments = message.attachments.length
    ? [
        "",
        "## Attachments",
        "",
        ...message.attachments.map(
          (att) =>
            `- [${att.index}] ${att.filename} (${att.content_type}, ${formatBytes(att.size)}) — fetch with rupochta_get_attachment`,
        ),
      ]
    : [];
  const invite = message.has_invite
    ? ["", "> This message carries a calendar invitation."]
    : [];
  return [
    `# ${message.subject || "(no subject)"}`,
    "",
    ...header,
    ...invite,
    ...attachments,
    "",
    "## Body",
    "",
    message.body || "(empty body)",
  ].join("\n");
}

export function registerMessageTools(server: McpServer, client: RuPochtaClient): void {
  server.registerTool(
    "rupochta_list_messages",
    {
      title: "List messages in a folder",
      description: `List messages in a folder, newest first, with offset/limit paging and optional filters.

Returns envelope data and a short snippet — not the full body. Use rupochta_get_message once you have picked a UID. Filters combine: 'unread=true' plus 'query="subject:invoice"' returns unread invoices only.

Args:
  - folder (string): folder name (default 'INBOX')
  - limit (number): messages to return, 1-100 (default 20)
  - offset (number): messages to skip for paging (default 0)
  - query (string): optional filter text, same grammar as rupochta_search_messages
  - unread (boolean, optional): true = unread only, false = read only
  - flagged (boolean, optional): true = starred only, false = unstarred only
  - has_attachments (boolean, optional): true = with attachments only, false = without
  - mailbox_context (string): 'personal' (default) or 'shared:<id>'
  - response_format ('markdown' | 'json'): output format (default: 'markdown')

Returns:
  {
    "total": number,            // matches in the folder
    "count": number,            // rows in this response
    "offset": number,           // offset used
    "has_more": boolean,
    "next_offset": number,      // present only when has_more is true
    "folder": string,
    "messages": [
      {
        "uid": string,          // per-folder IMAP UID — pass it back with the same folder
        "folder": string,
        "from": string, "to": string, "subject": string,
        "date": string,         // RFC 2822 date as IMAP reported it
        "message_id": string,   // RFC 5322 Message-ID, stable across folders
        "unread": boolean, "flagged": boolean, "has_attachments": boolean,
        "snippet": string       // first ~200 characters of the body
      }
    ]
  }

Examples:
  - Use when: "What is in my inbox?" -> folder='INBOX'
  - Use when: "Show unread mail from last week" -> unread=true, then filter by the date field
  - Use when: paging -> call again with offset=next_offset
  - Don't use when: you need the message body (use rupochta_get_message)

Error Handling:
  - HTTP 502: IMAP could not select the folder — check the exact name with rupochta_list_folders
  - An empty "messages" array means the filter matched nothing, not that the call failed`,
      inputSchema: ListMessagesInput,
      outputSchema: {
        folder: z.string(),
        total: z.number(),
        count: z.number(),
        offset: z.number(),
        has_more: z.boolean(),
        next_offset: z.number().optional(),
        truncated: z.boolean().optional(),
        truncation_message: z.string().optional(),
        messages: z.array(z.object(MessageSummaryShape)),
      },
      annotations: {
        readOnlyHint: true,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: true,
      },
    },
    guard(async (args: z.infer<typeof ListMessagesInput>) => {
      const window = await fetchMessageWindow(client, {
        folder: args.folder,
        limit: args.limit,
        offset: args.offset,
        query: args.query,
        ...(args.unread !== undefined ? { unread: args.unread } : {}),
        ...(args.flagged !== undefined ? { flagged: args.flagged } : {}),
        ...(args.has_attachments !== undefined ? { hasAttachments: args.has_attachments } : {}),
        mailboxContext: args.mailbox_context,
      });
      const messages = window.messages.map((raw) => normalizeMessageSummary(raw, args.folder));

      if (!messages.length) {
        return renderSingle({
          format: args.response_format,
          structured: {
            folder: args.folder,
            total: window.total,
            count: 0,
            offset: args.offset,
            has_more: false,
            messages: [],
          },
          markdown: () =>
            window.total > 0
              ? `No messages at offset ${args.offset} in ${args.folder} (${window.total} match in total — lower the offset).`
              : `No messages match in ${args.folder}.`,
        });
      }

      return renderList<MessageSummary>({
        format: args.response_format,
        itemsKey: "messages",
        items: messages,
        meta: { folder: args.folder, ...paginationMeta(window.total, args.offset, messages.length) },
        markdown: (items, meta) =>
          [
            `# ${args.folder} — ${items.length} of ${window.total} messages (offset ${args.offset})`,
            "",
            ...summaryTable(items),
            "",
            meta.has_more
              ? `More available — call again with offset=${meta.next_offset}.`
              : "End of results.",
          ].join("\n"),
      });
    }),
  );

  server.registerTool(
    "rupochta_search_messages",
    {
      title: "Search messages",
      description: `Search a folder with IMAP SEARCH across subject, sender, recipient and body text.

Unlike rupochta_list_messages this is a single-shot search capped by 'limit' rather than a paged listing, and it searches message bodies on the server. Search covers one folder per call — to search everything, call it once per folder from rupochta_list_folders.

Args:
  - query (string): what to look for. Bare words match subject/from/to/body. Prefixes: 'from:', 'to:', 'subject:'. Flags: 'unread', 'read', 'flagged', 'unflagged', 'has:attachment', 'has:no-attachment'. Combine freely: 'from:acme.com subject:invoice unread'
  - folder (string): folder to search (default 'INBOX')
  - limit (number): maximum matches, 1-200 (default 50)
  - mailbox_context (string): 'personal' (default) or 'shared:<id>'
  - response_format ('markdown' | 'json'): output format (default: 'markdown')

Returns:
  {
    "query": string,
    "folder": string,
    "total": number,          // matches found (may exceed count)
    "count": number,          // matches returned
    "has_more": boolean,      // true when total exceeded limit
    "messages": [ { ...same message fields as rupochta_list_messages } ]
  }

Examples:
  - Use when: "Find the invoice Acme sent" -> query='from:acme subject:invoice'
  - Use when: "Anything unread with attachments?" -> query='unread has:attachment'
  - Don't use when: you just want the newest mail in a folder (rupochta_list_messages is cheaper)

Error Handling:
  - An empty query with no flags returns zero results rather than the whole folder
  - HTTP 502: the folder could not be selected, or the IMAP server rejected the SEARCH`,
      inputSchema: SearchMessagesInput,
      outputSchema: {
        query: z.string(),
        folder: z.string(),
        total: z.number(),
        count: z.number(),
        has_more: z.boolean(),
        truncated: z.boolean().optional(),
        truncation_message: z.string().optional(),
        messages: z.array(z.object(MessageSummaryShape)),
      },
      annotations: {
        readOnlyHint: true,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: true,
      },
    },
    guard(async (args: z.infer<typeof SearchMessagesInput>) => {
      const payload = await client.request<RawMessageList>("/api/messages/search", {
        query: { folder: args.folder, q: args.query, limit: args.limit },
        mailboxContext: args.mailbox_context,
      });
      const messages = (payload.messages ?? []).map((raw) =>
        normalizeMessageSummary(raw, args.folder),
      );
      const total = num(payload.total, messages.length);

      if (!messages.length) {
        return renderSingle({
          format: args.response_format,
          structured: {
            query: args.query,
            folder: args.folder,
            total: 0,
            count: 0,
            has_more: false,
            messages: [],
          },
          markdown: () =>
            `No messages in ${args.folder} match '${args.query}'. Try a broader query, or search another folder.`,
        });
      }

      return renderList<MessageSummary>({
        format: args.response_format,
        itemsKey: "messages",
        items: messages,
        meta: {
          query: args.query,
          folder: args.folder,
          total,
          has_more: total > messages.length,
        },
        markdown: (items) =>
          [
            `# Search '${args.query}' in ${args.folder} — ${items.length} of ${total} matches`,
            "",
            ...summaryTable(items),
            ...(total > items.length
              ? ["", `Only the first ${items.length} of ${total} matches are shown — raise 'limit' to see more.`]
              : []),
          ].join("\n"),
      });
    }),
  );

  server.registerTool(
    "rupochta_get_message",
    {
      title: "Read a message",
      description: `Fetch one message in full: headers, body and the list of attachments.

Side effect: RuPochta marks the message as read when the personal mailbox fetches it. Call rupochta_set_message_flags with seen=false afterwards to leave the mailbox as you found it.

The body is returned as plain text by default, converting HTML mail when no text part exists. Long bodies are truncated at 'max_body_chars' with a marker.

Args:
  - uid (string): numeric IMAP UID from a listing tool
  - folder (string): the folder that UID came from (default 'INBOX')
  - max_body_chars (number): body character budget, 200-200000 (default 8000)
  - include_html (boolean): return sanitised HTML instead of plain text (default false)
  - mailbox_context (string): 'personal' (default) or 'shared:<id>'
  - response_format ('markdown' | 'json'): output format (default: 'markdown')

Returns:
  {
    "uid": string, "folder": string,
    "from": string, "to": string, "cc": string, "subject": string,
    "date": string, "message_id": string, "in_reply_to": string,
    "references": string[],           // thread chain, oldest first
    "unread": boolean, "flagged": boolean, "has_attachments": boolean,
    "body": string,                   // plain text unless include_html
    "body_format": "text" | "html-derived" | "empty",
    "body_truncated": boolean,
    "has_invite": boolean,            // true when the mail carries a calendar invitation
    "attachments": [
      { "index": number, "filename": string, "content_type": string, "size": number | null }
    ]
  }

Examples:
  - Use when: "What does the message from Acme say?" -> uid from a listing, folder='INBOX'
  - Use when: preparing a reply -> take message_id and pass it as in_reply_to to rupochta_send_message
  - Don't use when: scanning many messages; the snippet from rupochta_list_messages is usually enough

Error Handling:
  - HTTP 502 "Message not found": the UID does not exist in that folder — it may have moved; search by subject to find it again`,
      inputSchema: GetMessageInput,
      outputSchema: {
        uid: z.string(),
        folder: z.string(),
        from: z.string(),
        to: z.string(),
        cc: z.string(),
        subject: z.string(),
        date: z.string(),
        message_id: z.string(),
        in_reply_to: z.string(),
        references: z.array(z.string()),
        unread: z.boolean(),
        flagged: z.boolean(),
        has_attachments: z.boolean(),
        snippet: z.string(),
        body: z.string(),
        body_format: z.string(),
        body_truncated: z.boolean(),
        has_invite: z.boolean(),
        attachments: z.array(
          z.object({
            index: z.number(),
            filename: z.string(),
            content_type: z.string(),
            size: z.number().nullable(),
          }),
        ),
      },
      annotations: {
        // Fetching marks the message \Seen, so this is not strictly read-only.
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: true,
      },
    },
    guard(async (args: z.infer<typeof GetMessageInput>) => {
      const raw = await client.request<RawMessageDetail>(
        `/api/messages/${encodeURIComponent(args.uid)}`,
        { query: { folder: args.folder }, mailboxContext: args.mailbox_context },
      );
      const message = normalizeMessageDetail(raw, {
        folder: args.folder,
        maxBodyChars: args.max_body_chars,
        preferHtml: args.include_html,
      });
      return renderSingle({
        format: args.response_format,
        structured: message as unknown as Record<string, unknown>,
        markdown: () => detailMarkdown(message),
      });
    }),
  );

  server.registerTool(
    "rupochta_get_attachment",
    {
      title: "Download an attachment",
      description: `Download one attachment from a message by its index.

Text attachments come back as readable text; anything else comes back as a base64 resource block. Attachments larger than 4 MB are refused rather than pushed into the conversation — download those through the web UI.

Args:
  - uid (string): numeric IMAP UID of the message
  - index (number): zero-based index from the message's attachments[] list
  - folder (string): folder holding the message (default 'INBOX')
  - mailbox_context (string): 'personal' (default) or 'shared:<id>'

Returns:
  {
    "filename": string,
    "content_type": string,
    "size_bytes": number,
    "encoding": "text" | "base64"
  }
  plus the attachment itself as a text or resource content block.

Examples:
  - Use when: "What is in the CSV attached to that message?" -> index=0
  - Use when: a message reports has_attachments and you need the contents
  - Don't use when: you only need the file name (rupochta_get_message already lists it)

Error Handling:
  - HTTP 404: no attachment at that index — re-check the attachments[] list
  - Returns an error when the attachment exceeds 4 MB`,
      inputSchema: GetAttachmentInput,
      outputSchema: {
        filename: z.string(),
        content_type: z.string(),
        size_bytes: z.number(),
        encoding: z.string(),
      },
      annotations: {
        readOnlyHint: true,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: true,
      },
    },
    guard(async (args: z.infer<typeof GetAttachmentInput>): Promise<CallToolResult> => {
      const payload = await client.requestBinary(
        `/api/messages/${encodeURIComponent(args.uid)}/attachment/${args.index}`,
        { query: { folder: args.folder }, mailboxContext: args.mailbox_context },
      );
      if (payload.bytes.byteLength > MAX_ATTACHMENT_BYTES) {
        return {
          isError: true,
          content: [
            {
              type: "text",
              text:
                `Error: attachment '${payload.filename}' is ${formatBytes(payload.bytes.byteLength)}, ` +
                `over the ${formatBytes(MAX_ATTACHMENT_BYTES)} limit this server inlines. ` +
                "Download it from the RuPochta web interface instead.",
            },
          ],
        };
      }

      const mime = payload.contentType.split(";")[0]?.trim() ?? "application/octet-stream";
      const isText = mime.startsWith("text/") || mime === "application/json";
      const structured = {
        filename: payload.filename,
        content_type: mime,
        size_bytes: payload.bytes.byteLength,
        encoding: isText ? "text" : "base64",
      };
      const uri = `rupochta://${args.mailbox_context}/${encodeURIComponent(args.folder)}/${args.uid}/attachment/${args.index}`;
      const header = `Attachment ${payload.filename} (${mime}, ${formatBytes(payload.bytes.byteLength)})`;

      if (isText) {
        return {
          content: [
            { type: "text", text: header },
            { type: "text", text: Buffer.from(payload.bytes).toString("utf8") },
          ],
          structuredContent: structured,
        };
      }
      return {
        content: [
          { type: "text", text: header },
          {
            type: "resource",
            resource: {
              uri,
              mimeType: mime,
              blob: Buffer.from(payload.bytes).toString("base64"),
            },
          },
        ],
        structuredContent: structured,
      };
    }),
  );

  server.registerTool(
    "rupochta_set_message_flags",
    {
      title: "Mark a message read or starred",
      description: `Set the read and starred flags on a single message.

At least one of 'seen' or 'flagged' must be given. Use this to undo the read-marking side effect of rupochta_get_message.

Args:
  - uid (string): numeric IMAP UID
  - folder (string): folder holding the message (default 'INBOX')
  - seen (boolean, optional): true marks read, false marks unread
  - flagged (boolean, optional): true stars, false unstars
  - mailbox_context (string): 'personal' (default) or 'shared:<id>'

Returns:
  { "ok": boolean, "uid": string, "folder": string, "seen": boolean | null, "flagged": boolean | null }

Examples:
  - Use when: "Mark that as unread again" -> seen=false
  - Use when: "Star the contract email" -> flagged=true
  - Don't use when: changing many messages — rupochta_bulk_message_action does it in one call

Error Handling:
  - HTTP 400 "Nothing to update": neither seen nor flagged was supplied
  - HTTP 502: the UID is not in that folder`,
      inputSchema: SetFlagsInput,
      outputSchema: {
        ok: z.boolean(),
        uid: z.string(),
        folder: z.string(),
        seen: z.boolean().nullable(),
        flagged: z.boolean().nullable(),
      },
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: true,
      },
    },
    guard(async (args: z.infer<typeof SetFlagsInput>) => {
      if (args.seen === undefined && args.flagged === undefined) {
        return {
          isError: true,
          content: [
            {
              type: "text" as const,
              text: "Error: nothing to update. Pass seen and/or flagged, e.g. seen=false to mark the message unread.",
            },
          ],
        };
      }
      await client.request(`/api/messages/${encodeURIComponent(args.uid)}/flags`, {
        method: "POST",
        body: {
          folder: args.folder,
          ...(args.seen !== undefined ? { seen: args.seen } : {}),
          ...(args.flagged !== undefined ? { flagged: args.flagged } : {}),
        },
        mailboxContext: args.mailbox_context,
      });
      const structured = {
        ok: true,
        uid: args.uid,
        folder: args.folder,
        seen: args.seen ?? null,
        flagged: args.flagged ?? null,
      };
      const changes = [
        args.seen !== undefined ? (args.seen ? "read" : "unread") : "",
        args.flagged !== undefined ? (args.flagged ? "starred" : "unstarred") : "",
      ].filter(Boolean);
      return renderSingle({
        format: "markdown",
        structured,
        markdown: () => `Message ${args.uid} in ${args.folder} is now ${changes.join(" and ")}.`,
      });
    }),
  );

  server.registerTool(
    "rupochta_move_message",
    {
      title: "Move a message to another folder",
      description: `Move one message between folders.

The message gets a new UID in the destination folder, so any UID you were holding becomes stale. The response carries an 'undo' payload of Message-IDs that rupochta_undo_move can put back.

Args:
  - uid (string): numeric IMAP UID in from_folder
  - from_folder (string): folder the message is in now (default 'INBOX')
  - to_folder (string): destination folder, which must already exist
  - mailbox_context (string): 'personal' (default) or 'shared:<id>'

Returns:
  {
    "ok": boolean,
    "from_folder": string, "to_folder": string,
    "undo_message_ids": string[]     // pass to rupochta_undo_move to reverse this
  }

Examples:
  - Use when: "File this under Archive" -> to_folder='Archive'
  - Don't use when: deleting (use rupochta_delete_message, which moves to Trash)
  - Don't use when: moving several messages — rupochta_bulk_message_action is one round trip

Error Handling:
  - HTTP 502: destination folder missing (create it first) or UID not in from_folder`,
      inputSchema: MoveMessageInput,
      outputSchema: {
        ok: z.boolean(),
        from_folder: z.string(),
        to_folder: z.string(),
        undo_message_ids: z.array(z.string()),
      },
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: false,
        openWorldHint: true,
      },
    },
    guard(async (args: z.infer<typeof MoveMessageInput>) => {
      const payload = await client.request<{ ok?: boolean; undo?: { message_ids?: string[] } }>(
        `/api/messages/${encodeURIComponent(args.uid)}/move`,
        {
          method: "POST",
          body: { from_folder: args.from_folder, to_folder: args.to_folder },
          mailboxContext: args.mailbox_context,
        },
      );
      const structured = {
        ok: Boolean(payload.ok ?? true),
        from_folder: args.from_folder,
        to_folder: args.to_folder,
        undo_message_ids: payload.undo?.message_ids ?? [],
      };
      return renderSingle({
        format: "markdown",
        structured,
        markdown: () =>
          `Moved message ${args.uid} from ${args.from_folder} to ${args.to_folder}. ` +
          (structured.undo_message_ids.length
            ? `Undo with rupochta_undo_move (message_ids: ${structured.undo_message_ids.join(", ")}).`
            : ""),
      });
    }),
  );

  server.registerTool(
    "rupochta_delete_message",
    {
      title: "Delete a message",
      description: `Delete a message: move it to Trash, or erase it permanently when it is already in Trash.

From any folder other than Trash this is reversible — the response carries Message-IDs for rupochta_undo_move. Called on a message already in Trash it expunges the message for good.

Args:
  - uid (string): numeric IMAP UID
  - folder (string): folder holding the message (default 'INBOX')
  - mailbox_context (string): 'personal' (default) or 'shared:<id>'

Returns:
  { "ok": boolean, "folder": string, "permanent": boolean, "undo_message_ids": string[] }

Examples:
  - Use when: "Delete that newsletter" -> folder='INBOX'
  - Use when: emptying Trash one message at a time -> folder='Trash' (permanent)
  - Don't use when: the user only wants it out of the inbox — rupochta_move_message to Archive keeps it

Error Handling:
  - HTTP 502: the UID is not in that folder, or IMAP refused the expunge`,
      inputSchema: DeleteMessageInput,
      outputSchema: {
        ok: z.boolean(),
        folder: z.string(),
        permanent: z.boolean(),
        undo_message_ids: z.array(z.string()),
      },
      annotations: {
        readOnlyHint: false,
        destructiveHint: true,
        idempotentHint: false,
        openWorldHint: true,
      },
    },
    guard(async (args: z.infer<typeof DeleteMessageInput>) => {
      const payload = await client.request<{ ok?: boolean; undo?: { message_ids?: string[] } }>(
        `/api/messages/${encodeURIComponent(args.uid)}`,
        { method: "DELETE", query: { folder: args.folder }, mailboxContext: args.mailbox_context },
      );
      const permanent = args.folder.toLowerCase() === "trash";
      const structured = {
        ok: Boolean(payload.ok ?? true),
        folder: args.folder,
        permanent,
        undo_message_ids: payload.undo?.message_ids ?? [],
      };
      return renderSingle({
        format: "markdown",
        structured,
        markdown: () =>
          permanent
            ? `Permanently deleted message ${args.uid} from Trash.`
            : `Moved message ${args.uid} from ${args.folder} to Trash.` +
              (structured.undo_message_ids.length
                ? ` Undo with rupochta_undo_move (from_folder='Trash', to_folder='${args.folder}').`
                : ""),
      });
    }),
  );

  server.registerTool(
    "rupochta_bulk_message_action",
    {
      title: "Act on many messages at once",
      description: `Apply one operation to up to 200 messages in a single IMAP round trip.

All UIDs must come from the same folder. 'move' needs a destination; 'delete' moves to Trash unless the messages are already there, in which case it expunges them.

Args:
  - uids (string[]): 1-200 numeric UIDs, all from from_folder
  - from_folder (string): folder holding them (default 'INBOX')
  - action ('move' | 'delete' | 'mark_read' | 'mark_unread' | 'star' | 'unstar')
  - to_folder (string, optional): destination — required for 'move'
  - mailbox_context (string): 'personal' (default) or 'shared:<id>'

Returns:
  {
    "ok": boolean,
    "action": string,
    "count": number,                 // messages affected
    "undo_message_ids": string[]     // non-empty for move/delete
  }

Examples:
  - Use when: "Mark everything in the inbox as read" -> list UIDs, action='mark_read'
  - Use when: "Archive all of these" -> action='move', to_folder='Archive'
  - Don't use when: the UIDs span several folders — call once per folder

Error Handling:
  - HTTP 400: 'move' without to_folder, an empty uids list, or an unsupported action
  - HTTP 502: IMAP could not select from_folder`,
      inputSchema: BulkActionInput,
      outputSchema: {
        ok: z.boolean(),
        action: z.string(),
        count: z.number(),
        undo_message_ids: z.array(z.string()),
      },
      annotations: {
        readOnlyHint: false,
        destructiveHint: true,
        idempotentHint: false,
        openWorldHint: true,
      },
    },
    guard(async (args: z.infer<typeof BulkActionInput>) => {
      if (args.action === "move" && !args.to_folder) {
        return {
          isError: true,
          content: [
            {
              type: "text" as const,
              text: "Error: action='move' requires to_folder. Pass the destination folder name, e.g. to_folder='Archive'.",
            },
          ],
        };
      }
      const payload = await client.request<{
        ok?: boolean;
        count?: number;
        action?: string;
        undo?: { message_ids?: string[] };
      }>("/api/messages/bulk", {
        method: "POST",
        body: {
          from_folder: args.from_folder,
          uids: args.uids,
          action: args.action,
          ...(args.to_folder ? { to_folder: args.to_folder } : {}),
        },
        mailboxContext: args.mailbox_context,
      });
      const structured = {
        ok: Boolean(payload.ok ?? true),
        action: payload.action ?? args.action,
        count: num(payload.count, args.uids.length),
        undo_message_ids: payload.undo?.message_ids ?? [],
      };
      return renderSingle({
        format: "markdown",
        structured,
        markdown: () =>
          `Applied '${structured.action}' to ${structured.count} message(s) in ${args.from_folder}` +
          (args.to_folder ? ` → ${args.to_folder}` : "") +
          "." +
          (structured.undo_message_ids.length
            ? ` ${structured.undo_message_ids.length} Message-ID(s) available for rupochta_undo_move.`
            : ""),
      });
    }),
  );

  server.registerTool(
    "rupochta_undo_move",
    {
      title: "Undo a move or delete",
      description: `Put messages back where they came from, addressing them by Message-ID rather than UID.

Moves and deletes return 'undo_message_ids'. Feed those here with the folders swapped: from_folder is where the messages are now, to_folder is where they should go back to. Message-IDs survive moves, which is why this works when the UID no longer does.

Args:
  - message_ids (string[]): 1-200 RFC 5322 Message-IDs, without angle brackets
  - from_folder (string): folder the messages sit in now
  - to_folder (string): folder to restore them to
  - mailbox_context (string): 'personal' (default) or 'shared:<id>'

Returns:
  { "ok": boolean, "count": number, "from_folder": string, "to_folder": string }

Examples:
  - Use when: a delete was a mistake -> from_folder='Trash', to_folder='INBOX', ids from the delete response
  - Use when: reversing a bulk archive
  - Don't use when: you still have a valid UID — rupochta_move_message is more direct

Error Handling:
  - count=0 means no message with those IDs was found in from_folder; it may have been expunged
  - HTTP 400: empty message_ids list`,
      inputSchema: UndoMoveInput,
      outputSchema: {
        ok: z.boolean(),
        count: z.number(),
        from_folder: z.string(),
        to_folder: z.string(),
      },
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: false,
        openWorldHint: true,
      },
    },
    guard(async (args: z.infer<typeof UndoMoveInput>) => {
      const payload = await client.request<{ ok?: boolean; count?: number }>(
        "/api/messages/undo",
        {
          method: "POST",
          body: {
            from_folder: args.from_folder,
            to_folder: args.to_folder,
            message_ids: args.message_ids,
          },
          mailboxContext: args.mailbox_context,
        },
      );
      const structured = {
        ok: Boolean(payload.ok ?? true),
        count: num(payload.count),
        from_folder: args.from_folder,
        to_folder: args.to_folder,
      };
      return renderSingle({
        format: "markdown",
        structured,
        markdown: () =>
          structured.count
            ? `Restored ${structured.count} message(s) from ${args.from_folder} to ${args.to_folder}.`
            : `No messages with those Message-IDs were found in ${args.from_folder}; nothing was moved.`,
      });
    }),
  );
}
