/**
 * Contact lookup and calendar access.
 */

import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import type { RuPochtaClient } from "../services/client.js";
import { formatDate, guard, inline, renderList, renderSingle } from "../services/format.js";
import { normalizeCalendarEvent, normalizeContact, str } from "../services/normalize.js";
import { responseFormatField } from "../schemas/common.js";
import type { CalendarEvent, Contact, RawCalendarEvent, RawContact } from "../types.js";

const SearchContactsInput = z
  .object({
    query: z
      .string()
      .min(2, "query must be at least 2 characters — shorter queries return nothing")
      .max(100)
      .describe("Name, login or address fragment to look for"),
    response_format: responseFormatField,
  })
  .strict();

const ListEventsInput = z
  .object({
    limit: z
      .number()
      .int()
      .min(1)
      .max(200)
      .default(50)
      .describe("Maximum events to return (1-200, default 50)"),
    response_format: responseFormatField,
  })
  .strict();

const EventBody = {
  summary: z.string().min(1, "summary must not be empty").max(500).describe("Event title"),
  start: z
    .string()
    .min(4)
    .describe("Start, ISO-8601 datetime ('2026-09-01T09:00:00Z') or all-day date ('2026-09-01')"),
  end: z.string().min(4).describe("End, in the same format as start"),
  description: z.string().max(20000).default("").describe("Event description (optional)"),
  location: z.string().max(500).default("").describe("Event location (optional)"),
  attendees: z
    .array(z.object({ email: z.string(), name: z.string().default("") }).strict())
    .max(100)
    .optional()
    .describe("Attendees to invite, e.g. [{ email: 'alice@example.com', name: 'Alice' }]"),
};

const CreateEventInput = z.object({ ...EventBody }).strict();
const UpdateEventInput = z
  .object({
    uid: z.string().min(1).describe("Event UID from rupochta_list_calendar_events"),
    ...EventBody,
  })
  .strict();
const DeleteEventInput = z
  .object({ uid: z.string().min(1).describe("Event UID from rupochta_list_calendar_events") })
  .strict();

export function registerDirectoryTools(server: McpServer, client: RuPochtaClient): void {
  server.registerTool(
    "rupochta_search_contacts",
    {
      title: "Search contacts",
      description: `Look up addresses in the RuPochta contact directory.

Queries shorter than two characters return nothing by design. On a self-hosted instance without an external directory, this resolves the signed-in mailbox and any contacts RuPochta knows natively — it is not a full corporate address book unless one is configured.

Args:
  - query (string): name, login or address fragment, at least 2 characters
  - response_format ('markdown' | 'json'): output format (default: 'markdown')

Returns:
  {
    "count": number,
    "query": string,
    "contacts": [
      { "name": string, "email": string, "login": string, "department": string, "title": string, "source": string }
    ]
  }

Examples:
  - Use when: "What is Alice's email address?" -> query='alice'
  - Use when: confirming an address before sending
  - Don't use when: you already have the full address

Error Handling:
  - An empty result means no contact matched, not that the lookup failed`,
      inputSchema: SearchContactsInput,
      outputSchema: {
        count: z.number(),
        query: z.string(),
        truncated: z.boolean().optional(),
        truncation_message: z.string().optional(),
        contacts: z.array(
          z.object({
            name: z.string(),
            email: z.string(),
            login: z.string(),
            department: z.string(),
            title: z.string(),
            source: z.string(),
          }),
        ),
      },
      annotations: {
        readOnlyHint: true,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: true,
      },
    },
    guard(async (args: z.infer<typeof SearchContactsInput>) => {
      const payload = await client.request<{ contacts?: RawContact[] }>("/api/contacts", {
        query: { q: args.query },
      });
      const contacts = (payload.contacts ?? []).map(normalizeContact);
      if (!contacts.length) {
        return renderSingle({
          format: args.response_format,
          structured: { count: 0, query: args.query, contacts: [] },
          markdown: () => `No contacts match '${args.query}'.`,
        });
      }
      return renderList<Contact>({
        format: args.response_format,
        itemsKey: "contacts",
        items: contacts,
        meta: { query: args.query },
        markdown: (rows) =>
          [
            `# Contacts matching '${args.query}' (${rows.length})`,
            "",
            ...rows.map(
              (contact) =>
                `- **${inline(contact.name || contact.login, 60)}** — ${contact.email}` +
                (contact.department ? ` · ${inline(contact.department, 40)}` : "") +
                (contact.title ? ` · ${inline(contact.title, 40)}` : ""),
            ),
          ].join("\n"),
      });
    }),
  );

  server.registerTool(
    "rupochta_list_calendar_events",
    {
      title: "List calendar events",
      description: `List events from the mailbox's default CalDAV calendar.

Requires CalDAV to be enabled on the instance; without it the call returns a 502 rather than an empty calendar. Events come back unsorted from the server, so this tool sorts them by start time.

Args:
  - limit (number): maximum events to return, 1-200 (default 50)
  - response_format ('markdown' | 'json'): output format (default: 'markdown')

Returns:
  {
    "count": number,
    "events": [
      {
        "uid": string,          // pass to update/delete
        "summary": string,
        "description": string,
        "location": string,
        "start": string,        // ISO datetime or date for all-day events
        "end": string,
        "attendees": string[]
      }
    ]
  }

Examples:
  - Use when: "What is on my calendar?"
  - Use when: you need an event UID before changing or deleting it
  - Don't use when: the instance has no CalDAV — the error message will say so

Error Handling:
  - HTTP 502: CalDAV is not configured or unreachable`,
      inputSchema: ListEventsInput,
      outputSchema: {
        count: z.number(),
        truncated: z.boolean().optional(),
        truncation_message: z.string().optional(),
        events: z.array(
          z.object({
            uid: z.string(),
            summary: z.string(),
            description: z.string(),
            location: z.string(),
            start: z.string(),
            end: z.string(),
            attendees: z.array(z.string()),
          }),
        ),
      },
      annotations: {
        readOnlyHint: true,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: true,
      },
    },
    guard(async (args: z.infer<typeof ListEventsInput>) => {
      const payload = await client.request<{ events?: RawCalendarEvent[] }>("/api/calendar/events");
      const events = (payload.events ?? [])
        .map(normalizeCalendarEvent)
        .sort((left, right) => left.start.localeCompare(right.start))
        .slice(0, args.limit);
      if (!events.length) {
        return renderSingle({
          format: args.response_format,
          structured: { count: 0, events: [] },
          markdown: () => "The calendar has no events.",
        });
      }
      return renderList<CalendarEvent>({
        format: args.response_format,
        itemsKey: "events",
        items: events,
        meta: {},
        markdown: (rows) =>
          [
            `# Calendar events (${rows.length})`,
            "",
            ...rows.map(
              (event) =>
                `- **${inline(event.summary || "(no title)", 70)}** — ${formatDate(event.start)} → ${formatDate(event.end)}` +
                (event.location ? ` · ${inline(event.location, 40)}` : "") +
                ` · uid ${event.uid}`,
            ),
          ].join("\n"),
      });
    }),
  );

  server.registerTool(
    "rupochta_create_calendar_event",
    {
      title: "Create a calendar event",
      description: `Create an event in the mailbox's default calendar.

Listing attendees records them on the event; RuPochta does not send separate invitation mail from this endpoint.

Args:
  - summary (string): event title
  - start (string): ISO datetime '2026-09-01T09:00:00Z' or date '2026-09-01' for all-day
  - end (string): same format as start
  - description (string): details (default '')
  - location (string): where (default '')
  - attendees (array, optional): [{ email, name }]

Returns:
  { "ok": boolean, "uid": string }   // uid identifies the new event

Examples:
  - Use when: "Put a 1:1 with Alice on Monday at 10" -> start='2026-09-07T10:00:00Z', end='2026-09-07T10:30:00Z'
  - Don't use when: you are replying to an invitation that arrived by mail

Error Handling:
  - HTTP 502: CalDAV is not configured, or the server rejected the event`,
      inputSchema: CreateEventInput,
      outputSchema: { ok: z.boolean(), uid: z.string() },
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: false,
        openWorldHint: true,
      },
    },
    guard(async (args: z.infer<typeof CreateEventInput>) => {
      const payload = await client.request<{ ok?: boolean; uid?: string }>("/api/calendar/event", {
        method: "POST",
        body: {
          summary: args.summary,
          description: args.description,
          location: args.location,
          start: args.start,
          end: args.end,
          ...(args.attendees ? { attendees: args.attendees } : {}),
        },
      });
      const structured = { ok: Boolean(payload.ok ?? true), uid: str(payload.uid) };
      return renderSingle({
        format: "markdown",
        structured,
        markdown: () => `Created event "${args.summary}" (${args.start} → ${args.end}), uid ${structured.uid}.`,
      });
    }),
  );

  server.registerTool(
    "rupochta_update_calendar_event",
    {
      title: "Update a calendar event",
      description: `Replace an existing calendar event.

Every field is overwritten, so send the complete event — read it with rupochta_list_calendar_events first.

Args:
  - uid (string): event UID to replace
  - summary, start, end, description, location, attendees: the full new event

Returns:
  { "ok": boolean, "uid": string }

Examples:
  - Use when: "Move the 1:1 to 11:00" -> same uid, new start/end
  - Don't use when: creating something new (use rupochta_create_calendar_event)

Error Handling:
  - HTTP 502: CalDAV rejected the update or the UID does not exist`,
      inputSchema: UpdateEventInput,
      outputSchema: { ok: z.boolean(), uid: z.string() },
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: true,
      },
    },
    guard(async (args: z.infer<typeof UpdateEventInput>) => {
      const payload = await client.request<{ ok?: boolean; uid?: string }>(
        `/api/calendar/event/${encodeURIComponent(args.uid)}`,
        {
          method: "PUT",
          body: {
            uid: args.uid,
            summary: args.summary,
            description: args.description,
            location: args.location,
            start: args.start,
            end: args.end,
            ...(args.attendees ? { attendees: args.attendees } : {}),
          },
        },
      );
      const structured = { ok: Boolean(payload.ok ?? true), uid: str(payload.uid, args.uid) };
      return renderSingle({
        format: "markdown",
        structured,
        markdown: () => `Updated event ${structured.uid} ("${args.summary}").`,
      });
    }),
  );

  server.registerTool(
    "rupochta_delete_calendar_event",
    {
      title: "Delete a calendar event",
      description: `Delete an event from the mailbox's default calendar. This cannot be undone.

Args:
  - uid (string): event UID from rupochta_list_calendar_events

Returns:
  { "ok": boolean, "uid": string }

Examples:
  - Use when: "Cancel Friday's review" and the user confirmed
  - Don't use when: the event should move instead (use rupochta_update_calendar_event)

Error Handling:
  - HTTP 502: CalDAV rejected the delete, or no event has that UID`,
      inputSchema: DeleteEventInput,
      outputSchema: { ok: z.boolean(), uid: z.string() },
      annotations: {
        readOnlyHint: false,
        destructiveHint: true,
        idempotentHint: true,
        openWorldHint: true,
      },
    },
    guard(async (args: z.infer<typeof DeleteEventInput>) => {
      const payload = await client.request<{ ok?: boolean }>(
        `/api/calendar/event/${encodeURIComponent(args.uid)}`,
        { method: "DELETE" },
      );
      const structured = { ok: Boolean(payload.ok ?? true), uid: args.uid };
      return renderSingle({
        format: "markdown",
        structured,
        markdown: () => `Deleted calendar event ${args.uid}.`,
      });
    }),
  );
}
