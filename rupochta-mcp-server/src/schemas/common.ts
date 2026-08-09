/**
 * Zod fields reused across tools.
 *
 * Constraints mirror what RuPochta itself enforces (page sizes, query length,
 * numeric IMAP UIDs), so bad input fails locally with a readable message
 * instead of becoming a 400 from the mail server.
 */

import { z } from "zod";
import { MAX_PAGE_SIZE } from "../constants.js";

export const responseFormatField = z
  .enum(["markdown", "json"])
  .default("markdown")
  .describe("Output format: 'markdown' for a readable summary, 'json' for the full structured payload");

export const folderField = z
  .string()
  .min(1, "folder must not be empty")
  .max(255, "folder name must not exceed 255 characters")
  .default("INBOX")
  .describe(
    "IMAP folder name, exactly as returned by rupochta_list_folders (e.g. 'INBOX', 'Sent', 'Drafts', 'Trash', 'Archive')",
  );

export const mailboxContextField = z
  .string()
  .regex(
    /^(personal|shared:[1-9][0-9]*)$/,
    "mailbox_context must be 'personal' or 'shared:<id>' with a positive id",
  )
  .default("personal")
  .describe(
    "Which mailbox to act on: 'personal' (default) or 'shared:<id>' for a shared mailbox from rupochta_list_shared_mailboxes",
  );

export const uidField = z
  .string()
  .regex(/^[0-9]+$/, "uid must be the numeric IMAP UID returned by a listing tool")
  .describe("IMAP UID of the message. UIDs are per-folder: a UID from INBOX is meaningless in another folder");

export const uidListField = z
  .array(z.string().regex(/^[0-9]+$/, "each uid must be numeric"))
  .min(1, "at least one uid is required")
  .max(200, "no more than 200 uids per call")
  .describe("IMAP UIDs to act on, all from the same folder");

export function limitField(max: number, fallback: number, what: string) {
  return z
    .number()
    .int()
    .min(1)
    .max(max)
    .default(fallback)
    .describe(`Maximum ${what} to return (1-${max}, default ${fallback})`);
}

export const offsetField = z
  .number()
  .int()
  .min(0)
  .default(0)
  .describe("Number of items to skip, for paging through results (default 0)");

export const pageSizeField = limitField(MAX_PAGE_SIZE, 20, "messages");

export const searchQueryField = z
  .string()
  .max(300, "query must not exceed 300 characters")
  .default("")
  .describe(
    "Search text. Supports RuPochta's filter grammar: bare words match subject/from/to/body, " +
      "'from:alice@example.com', 'to:bob', 'subject:invoice', and the flags 'unread', 'read', " +
      "'flagged', 'unflagged', 'has:attachment', 'has:no-attachment'. Example: 'subject:invoice unread'",
  );

export const isoDateTimeField = z
  .string()
  .min(4)
  .describe("ISO-8601 timestamp, e.g. '2026-09-01T09:30:00Z'. A bare value without a timezone is read as UTC");

export const emailListField = z
  .string()
  .describe("Comma-separated address list, e.g. 'alice@example.com, Bob <bob@example.com>'");
