/**
 * Response rendering shared by every tool.
 *
 * Tools return two things at once: text (markdown by default, JSON on request)
 * and `structuredContent` matching the tool's declared output schema. Both go
 * through here so truncation, pagination metadata and error text stay
 * consistent across the whole server.
 */

import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";
import { CHARACTER_LIMIT } from "../constants.js";
import { describeError } from "./errors.js";

export const ResponseFormat = {
  MARKDOWN: "markdown",
  JSON: "json",
} as const;

export type ResponseFormatValue = (typeof ResponseFormat)[keyof typeof ResponseFormat];

export type StructuredPayload = Record<string, unknown>;

export function textResult(text: string, structured: StructuredPayload): CallToolResult {
  return {
    content: [{ type: "text", text }],
    structuredContent: structured,
  };
}

export function errorResult(error: unknown): CallToolResult {
  return {
    content: [{ type: "text", text: describeError(error) }],
    isError: true,
  };
}

/** Wrap a tool handler so no thrown value ever escapes as a protocol error. */
export function guard<TArgs>(
  handler: (args: TArgs) => Promise<CallToolResult>,
): (args: TArgs) => Promise<CallToolResult> {
  return async (args: TArgs): Promise<CallToolResult> => {
    try {
      return await handler(args);
    } catch (error) {
      return errorResult(error);
    }
  };
}

export interface SingleRenderOptions {
  format: ResponseFormatValue;
  structured: StructuredPayload;
  markdown: () => string;
}

/** Render one object. Over-long text is cut with an explicit notice. */
export function renderSingle(options: SingleRenderOptions): CallToolResult {
  const text =
    options.format === ResponseFormat.JSON
      ? JSON.stringify(options.structured, null, 2)
      : options.markdown();
  if (text.length <= CHARACTER_LIMIT) {
    return textResult(text, options.structured);
  }
  const cut = `${text.slice(0, CHARACTER_LIMIT - 200)}\n\n[Response truncated at ${CHARACTER_LIMIT} characters. Request narrower data — for example a smaller max_body_chars, or fetch a specific field.]`;
  return textResult(cut, { ...options.structured, truncated: true });
}

export interface ListRenderOptions<T> {
  format: ResponseFormatValue;
  /** Key the items appear under in the structured payload, e.g. "messages". */
  itemsKey: string;
  items: T[];
  /** Fields merged next to the items: total, offset, has_more, … */
  meta: StructuredPayload;
  markdown: (items: T[], meta: StructuredPayload) => string;
}

/**
 * Render a list, halving it until it fits under CHARACTER_LIMIT.
 *
 * Truncation is reported in-band (`truncated`, `truncation_message`) so an
 * agent can react by paging instead of silently losing rows.
 */
export function renderList<T>(options: ListRenderOptions<T>): CallToolResult {
  const original = options.items.length;
  let items = options.items;

  for (;;) {
    const truncated = items.length < original;
    const payload: StructuredPayload = {
      ...options.meta,
      count: items.length,
      [options.itemsKey]: items,
    };
    if (truncated) {
      payload.truncated = true;
      payload.truncation_message =
        `Response truncated from ${original} to ${items.length} items to stay under ${CHARACTER_LIMIT} characters. ` +
        "Use a smaller limit, a higher offset, or a narrower query to see the rest.";
    }
    const text =
      options.format === ResponseFormat.JSON
        ? JSON.stringify(payload, null, 2)
        : options.markdown(items, payload);
    if (text.length <= CHARACTER_LIMIT || items.length <= 1) {
      return textResult(text, payload);
    }
    items = items.slice(0, Math.max(1, Math.floor(items.length / 2)));
  }
}

/** Pagination metadata in the shape the MCP best-practice guide prescribes. */
export function paginationMeta(
  total: number,
  offset: number,
  count: number,
): StructuredPayload {
  const hasMore = total > offset + count;
  const meta: StructuredPayload = { total, offset, has_more: hasMore };
  if (hasMore) {
    meta.next_offset = offset + count;
  }
  return meta;
}

/* ------------------------------------------------------------- markdown */

/** RFC 2822 dates as IMAP returns them are noisy; show something readable. */
export function formatDate(raw: string): string {
  if (!raw) {
    return "unknown date";
  }
  const parsed = new Date(raw);
  if (Number.isNaN(parsed.getTime())) {
    return raw;
  }
  return parsed.toISOString().replace("T", " ").slice(0, 16) + " UTC";
}

export function formatBytes(size: number | null): string {
  if (size === null || !Number.isFinite(size)) {
    return "size unknown";
  }
  if (size < 1024) {
    return `${size} B`;
  }
  if (size < 1024 * 1024) {
    return `${(size / 1024).toFixed(1)} KB`;
  }
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

/** Keep markdown tables and headers from breaking on stray pipes/newlines. */
export function inline(value: string, max = 160): string {
  const flat = value.replace(/\s+/g, " ").trim();
  const clipped = flat.length > max ? `${flat.slice(0, max - 1)}…` : flat;
  return clipped.replace(/\|/g, "\\|");
}

export function markdownSection(title: string, lines: string[]): string {
  return [`## ${title}`, "", ...lines].join("\n");
}

export function keyValueLines(pairs: Array<[string, string | number | boolean]>): string[] {
  return pairs
    .filter(([, value]) => value !== "" && value !== undefined && value !== null)
    .map(([key, value]) => `- **${key}**: ${value}`);
}
