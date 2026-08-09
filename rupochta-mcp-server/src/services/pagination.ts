/**
 * Offset/limit paging on top of RuPochta's page/per_page listing.
 *
 * `GET /api/messages` pages by (page, per_page) and caps per_page at 100. MCP
 * callers get the offset/limit contract the best-practice guide asks for, so
 * this translates: it fetches the minimum number of server pages that cover
 * the requested window and slices out the exact rows.
 */

import { MAX_PAGE_SIZE } from "../constants.js";
import type { RawMessageList, RawMessageSummary } from "../types.js";
import type { RuPochtaClient } from "./client.js";

export interface MessageWindowRequest {
  folder: string;
  limit: number;
  offset: number;
  query?: string;
  unread?: boolean;
  flagged?: boolean;
  hasAttachments?: boolean;
  mailboxContext?: string;
}

export interface MessageWindow {
  messages: RawMessageSummary[];
  total: number;
}

export async function fetchMessageWindow(
  client: RuPochtaClient,
  request: MessageWindowRequest,
): Promise<MessageWindow> {
  const pageSize = Math.min(Math.max(request.limit, 1), MAX_PAGE_SIZE);
  const firstPage = Math.floor(request.offset / pageSize) + 1;
  const lastPage = Math.ceil((request.offset + request.limit) / pageSize);

  const collected: RawMessageSummary[] = [];
  let total = 0;

  for (let page = firstPage; page <= lastPage; page += 1) {
    const payload = await client.request<RawMessageList>("/api/messages", {
      query: {
        folder: request.folder,
        page,
        per_page: pageSize,
        q: request.query,
        unread: request.unread,
        flagged: request.flagged,
        has_attachments: request.hasAttachments,
      },
      ...(request.mailboxContext ? { mailboxContext: request.mailboxContext } : {}),
    });
    const batch = payload.messages ?? [];
    total = Math.max(total, payload.total ?? 0);
    collected.push(...batch);
    if (batch.length < pageSize) {
      break; // Past the end of the folder — no later page can add rows.
    }
  }

  const sliceStart = request.offset - (firstPage - 1) * pageSize;
  return {
    messages: collected.slice(sliceStart, sliceStart + request.limit),
    total,
  };
}
