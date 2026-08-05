/**
 * Turn loose RuPochta JSON into the fixed shapes this server promises.
 *
 * Every tool that declares an `outputSchema` must produce structured content
 * that validates, so normalisation happens once, here, instead of being
 * re-invented per tool.
 */

import type {
  Attachment,
  CalendarEvent,
  Contact,
  FilterRule,
  Folder,
  MessageDetail,
  MessageSummary,
  RawAttachment,
  RawCalendarEvent,
  RawContact,
  RawFilterRule,
  RawFolder,
  RawMessageDetail,
  RawMessageSummary,
  RawScheduledSend,
  RawSharedMailbox,
  RawSnoozedMessage,
  RawTemplate,
  ScheduledSend,
  SharedMailbox,
  SnoozedMessage,
  Template,
} from "../types.js";

export function str(value: unknown, fallback = ""): string {
  if (typeof value === "string") {
    return value;
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return fallback;
}

export function num(value: unknown, fallback = 0): number {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }
  if (typeof value === "string") {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) {
      return parsed;
    }
  }
  return fallback;
}

export function bool(value: unknown, fallback = false): boolean {
  return typeof value === "boolean" ? value : fallback;
}

export function strList(value: unknown): string[] {
  if (Array.isArray(value)) {
    return value.map((item) => str(item)).filter(Boolean);
  }
  if (typeof value === "string" && value.trim()) {
    return value
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean);
  }
  return [];
}

/** Collapse HTML into readable plain text — agents rarely want the markup. */
export function htmlToText(html: string): string {
  return html
    .replace(/<(script|style)[^>]*>[\s\S]*?<\/\1>/gi, " ")
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(/<\/(p|div|tr|li|h[1-6])>/gi, "\n")
    .replace(/<[^>]+>/g, " ")
    .replace(/&nbsp;/gi, " ")
    .replace(/&amp;/gi, "&")
    .replace(/&lt;/gi, "<")
    .replace(/&gt;/gi, ">")
    .replace(/&quot;/gi, '"')
    .replace(/&#39;/gi, "'")
    .replace(/[ \t ]+/g, " ")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

export function normalizeFolder(raw: RawFolder): Folder {
  const name = str(raw?.name);
  return {
    name,
    label: str(raw?.label, name),
    unread: num(raw?.unread),
    total: num(raw?.total),
  };
}

export function normalizeMessageSummary(raw: RawMessageSummary, folder: string): MessageSummary {
  return {
    uid: str(raw?.uid),
    folder,
    from: str(raw?.from),
    to: str(raw?.to),
    subject: str(raw?.subject),
    date: str(raw?.date),
    message_id: str(raw?.message_id),
    unread: bool(raw?.unread),
    flagged: bool(raw?.flagged),
    has_attachments: bool(raw?.has_attachments),
    snippet: str(raw?.snippet),
  };
}

export function normalizeAttachment(raw: RawAttachment, index: number): Attachment {
  const size = raw?.size;
  return {
    index: typeof raw?.index === "number" ? raw.index : index,
    filename: str(raw?.filename, `attachment-${index}`),
    content_type: str(raw?.content_type, "application/octet-stream"),
    size: typeof size === "number" && Number.isFinite(size) ? size : null,
  };
}

export interface NormalizeDetailOptions {
  folder: string;
  maxBodyChars: number;
  preferHtml?: boolean;
}

export function normalizeMessageDetail(
  raw: RawMessageDetail,
  options: NormalizeDetailOptions,
): MessageDetail {
  const plain = str(raw?.body_text).trim();
  const html = str(raw?.body_html);
  let body = plain;
  let format: MessageDetail["body_format"] = plain ? "text" : "empty";
  if ((!plain || options.preferHtml) && html) {
    const derived = options.preferHtml ? html : htmlToText(html);
    if (derived.trim()) {
      body = derived;
      format = options.preferHtml ? "text" : "html-derived";
    }
  }
  const truncated = body.length > options.maxBodyChars;
  if (truncated) {
    body = `${body.slice(0, options.maxBodyChars)}\n…[body truncated; raise max_body_chars to read the rest]`;
  }

  return {
    ...normalizeMessageSummary(raw, str(raw?.folder, options.folder)),
    cc: str(raw?.cc),
    in_reply_to: str(raw?.in_reply_to),
    references: strList(raw?.references),
    body,
    body_format: format,
    body_truncated: truncated,
    attachments: (raw?.attachments ?? []).map((item, index) => normalizeAttachment(item, index)),
    has_invite: Boolean(raw?.invite),
  };
}

export function normalizeSharedMailbox(raw: RawSharedMailbox): SharedMailbox {
  const id = num(raw?.id);
  return {
    mailbox_id: id,
    mailbox_context: `shared:${id}`,
    email: str(raw?.email),
    name: str(raw?.display_name, str(raw?.name)),
    can_send: bool(raw?.can_send),
  };
}

export function normalizeFilterRule(raw: RawFilterRule): FilterRule {
  return {
    id: num(raw?.id),
    name: str(raw?.name),
    enabled: bool(raw?.enabled, true),
    priority: num(raw?.priority, 100),
    conditions: Array.isArray(raw?.conditions) ? raw.conditions : [],
    actions: Array.isArray(raw?.actions) ? raw.actions : [],
    updated_at: str(raw?.updated_at),
  };
}

export function normalizeScheduledSend(raw: RawScheduledSend): ScheduledSend {
  return {
    id: num(raw?.id),
    subject: str(raw?.subject),
    to: strList(raw?.to_list),
    scheduled_at: str(raw?.scheduled_at),
    status: str(raw?.status),
    attempts: num(raw?.attempts),
    last_error: str(raw?.last_error),
    sent_at: str(raw?.sent_at),
  };
}

export function normalizeSnoozed(raw: RawSnoozedMessage): SnoozedMessage {
  return {
    id: num(raw?.id),
    subject: str(raw?.subject),
    from: str(raw?.from_addr),
    original_folder: str(raw?.original_folder),
    wake_at: str(raw?.wake_at),
    status: str(raw?.status),
  };
}

export function normalizeTemplate(raw: RawTemplate): Template {
  return {
    id: num(raw?.id),
    name: str(raw?.name),
    subject: str(raw?.subject),
    body: str(raw?.body),
  };
}

export function normalizeContact(raw: RawContact): Contact {
  return {
    name: str(raw?.display_name, str(raw?.name)),
    email: str(raw?.email),
    login: str(raw?.login, str(raw?.mail_login)),
    department: str(raw?.department),
    title: str(raw?.title, str(raw?.position)),
    source: str(raw?.source, "rupochta"),
  };
}

export function normalizeCalendarEvent(raw: RawCalendarEvent): CalendarEvent {
  const attendees = Array.isArray(raw?.attendees)
    ? raw.attendees
        .map((entry) => str(entry?.email, str(entry?.name)))
        .filter(Boolean)
    : [];
  return {
    uid: str(raw?.uid),
    summary: str(raw?.summary),
    description: str(raw?.description),
    location: str(raw?.location),
    start: str(raw?.start),
    end: str(raw?.end),
    attendees,
  };
}
