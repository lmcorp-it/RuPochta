/**
 * Shared constants for the RuPochta MCP server.
 */

export const SERVER_NAME = "rupochta-mcp-server";
export const SERVER_VERSION = "1.0.0";

/** RuPochta listens on loopback by default and expects a reverse proxy in front. */
export const DEFAULT_BASE_URL = "http://127.0.0.1:18400";

/** Session cookie issued by POST /api/auth/login (CFG.SESSION_COOKIE in rupochta_server.py). */
export const SESSION_COOKIE_NAME = "wmSID";

/** Maximum size of a single tool response before it gets truncated. */
export const CHARACTER_LIMIT = 25000;

/** Default per-request timeout. IMAP round trips can be slow, so this is generous. */
export const DEFAULT_TIMEOUT_MS = 30000;

/** Attachments larger than this are refused rather than base64-inlined into the context. */
export const MAX_ATTACHMENT_BYTES = 4 * 1024 * 1024;

/** RuPochta caps GET /api/messages at 100 rows per page. */
export const MAX_PAGE_SIZE = 100;

/** Folders RuPochta creates and labels itself. */
export const WELL_KNOWN_FOLDERS = [
  "INBOX",
  "Sent",
  "Drafts",
  "Trash",
  "Junk",
  "Archive",
  "Snoozed",
] as const;
