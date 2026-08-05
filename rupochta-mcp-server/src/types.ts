/**
 * Shapes returned by the RuPochta API, plus the normalised shapes this server
 * hands back to callers.
 *
 * Raw types are deliberately loose (every field optional): they describe JSON
 * that crossed the network, and normalisation is what makes them safe to use.
 */

/* ---------------------------------------------------------------- raw API */

export interface RawFolder {
  name?: string;
  label?: string;
  unread?: number;
  total?: number;
}

export interface RawMessageSummary {
  uid?: string;
  from?: string;
  to?: string;
  subject?: string;
  subject_base?: string;
  date?: string;
  message_id?: string;
  in_reply_to?: string;
  references?: string[];
  flags?: string[];
  unread?: boolean;
  flagged?: boolean;
  has_attachments?: boolean;
  snippet?: string;
}

export interface RawMessageList {
  messages?: RawMessageSummary[];
  total?: number;
  page?: number;
  per_page?: number;
  query?: string;
  limit?: number;
}

export interface RawAttachment {
  filename?: string;
  content_type?: string;
  size?: number;
  index?: number;
}

export interface RawMessageDetail extends RawMessageSummary {
  folder?: string;
  cc?: string;
  body_html?: string;
  body_text?: string;
  attachments?: RawAttachment[];
  invite?: Record<string, unknown> | null;
}

export interface RawSharedMailbox {
  id?: number;
  email?: string;
  name?: string;
  display_name?: string;
  can_send?: boolean;
}

export interface RawFilterRule {
  id?: number;
  name?: string;
  enabled?: boolean;
  priority?: number;
  conditions?: Array<Record<string, unknown>>;
  actions?: Array<Record<string, unknown>>;
  created_at?: string;
  updated_at?: string;
}

export interface RawScheduledSend {
  id?: number;
  from_addr?: string;
  to_list?: string | string[];
  cc_list?: string | string[];
  bcc_list?: string | string[];
  subject?: string;
  scheduled_at?: string;
  status?: string;
  attempts?: number;
  last_error?: string;
  created_at?: string;
  sent_at?: string;
  origin?: string;
}

export interface RawSnoozedMessage {
  id?: number;
  message_id?: string;
  subject?: string;
  from_addr?: string;
  original_folder?: string;
  snoozed_at?: string;
  wake_at?: string;
  status?: string;
  last_error?: string;
  woken_at?: string;
}

export interface RawTemplate {
  id?: number;
  name?: string;
  subject?: string;
  body?: string;
  updated_at?: string;
}

export interface RawContact {
  login?: string;
  mail_login?: string;
  email?: string;
  display_name?: string;
  name?: string;
  department?: string;
  title?: string;
  position?: string;
  phone?: string;
  source?: string;
}

export interface RawCalendarEvent {
  uid?: string;
  summary?: string;
  description?: string;
  location?: string;
  start?: string;
  end?: string;
  attendees?: Array<Record<string, string>>;
  href?: string;
}

/* --------------------------------------------------------- normalised out */

export interface Folder {
  name: string;
  label: string;
  unread: number;
  total: number;
}

export interface MessageSummary {
  uid: string;
  folder: string;
  from: string;
  to: string;
  subject: string;
  date: string;
  message_id: string;
  unread: boolean;
  flagged: boolean;
  has_attachments: boolean;
  snippet: string;
}

export interface Attachment {
  index: number;
  filename: string;
  content_type: string;
  size: number | null;
}

export interface MessageDetail extends MessageSummary {
  cc: string;
  in_reply_to: string;
  references: string[];
  body: string;
  body_format: "text" | "html-derived" | "empty";
  body_truncated: boolean;
  attachments: Attachment[];
  has_invite: boolean;
}

export interface SharedMailbox {
  mailbox_id: number;
  mailbox_context: string;
  email: string;
  name: string;
  can_send: boolean;
}

export interface FilterRule {
  id: number;
  name: string;
  enabled: boolean;
  priority: number;
  conditions: Array<Record<string, unknown>>;
  actions: Array<Record<string, unknown>>;
  updated_at: string;
}

export interface ScheduledSend {
  id: number;
  subject: string;
  to: string[];
  scheduled_at: string;
  status: string;
  attempts: number;
  last_error: string;
  sent_at: string;
}

export interface SnoozedMessage {
  id: number;
  subject: string;
  from: string;
  original_folder: string;
  wake_at: string;
  status: string;
}

export interface Template {
  id: number;
  name: string;
  subject: string;
  body: string;
}

export interface Contact {
  name: string;
  email: string;
  login: string;
  department: string;
  title: string;
  source: string;
}

export interface CalendarEvent {
  uid: string;
  summary: string;
  description: string;
  location: string;
  start: string;
  end: string;
  attendees: string[];
}
