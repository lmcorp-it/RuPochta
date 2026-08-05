# rupochta-mcp-server

An [MCP](https://modelcontextprotocol.io) server for [RuPochta](https://github.com/lmcorp-it/RuPochta):
it turns a mailbox into tools an LLM agent can use — read and search mail, compose
and send, organise folders, manage server-side filters, scheduled sends, snoozes
and the calendar.

RuPochta stores no mail of its own; it is a web front end over plain IMAP and SMTP.
This server talks to its HTTP API, so everything here works against a real mail
server through a real session.

## Install

```bash
cd rupochta-mcp-server
npm install
npm run build
```

## Configure

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `RUPOCHTA_BASE_URL` | no | `http://127.0.0.1:18400` | Instance URL. **Must be the URL the instance is actually served on** — writes are rejected otherwise (see below). |
| `RUPOCHTA_EMAIL` | no | — | Mailbox to sign in as automatically. |
| `RUPOCHTA_PASSWORD` | with `RUPOCHTA_EMAIL` | — | Mailbox password. Verified against IMAP. |
| `RUPOCHTA_SESSION_COOKIE` | no | — | Existing `wmSID` session token, instead of signing in. |
| `RUPOCHTA_TIMEOUT_MS` | no | `30000` | Per-request timeout (1000–300000). |

Without `RUPOCHTA_EMAIL`/`RUPOCHTA_PASSWORD` the server starts fine and tools ask
you to call `rupochta_login` first.

### The `Origin` rule

RuPochta accepts writes only when the request looks same-origin: `Origin` must equal
`scheme://host` as the instance sees itself. This server sends `Origin: <RUPOCHTA_BASE_URL origin>`
on every write. If RuPochta sits behind a reverse proxy at `https://mail.example.com`,
point `RUPOCHTA_BASE_URL` there — pointing it at `http://127.0.0.1:18400` makes reads
work and writes fail with 403.

## Run

```json
{
  "mcpServers": {
    "rupochta": {
      "command": "node",
      "args": ["/path/to/RuPochta/rupochta-mcp-server/dist/index.js"],
      "env": {
        "RUPOCHTA_BASE_URL": "https://mail.example.com",
        "RUPOCHTA_EMAIL": "demo@example.com",
        "RUPOCHTA_PASSWORD": "…"
      }
    }
  }
}
```

Interactive inspection: `npm run inspect`.

## Tools

**Session** — `rupochta_login`, `rupochta_whoami`, `rupochta_logout`

**Folders and mailboxes** — `rupochta_list_folders`, `rupochta_create_folder`,
`rupochta_delete_folder`, `rupochta_list_shared_mailboxes`

**Reading** — `rupochta_list_messages`, `rupochta_search_messages`,
`rupochta_get_message`, `rupochta_get_attachment`

**Organising** — `rupochta_set_message_flags`, `rupochta_move_message`,
`rupochta_delete_message`, `rupochta_bulk_message_action`, `rupochta_undo_move`

**Composing** — `rupochta_send_message`, `rupochta_save_draft`, `rupochta_schedule_send`

**Queues** — `rupochta_list_scheduled`, `rupochta_cancel_scheduled`,
`rupochta_retry_scheduled`, `rupochta_snooze_messages`, `rupochta_list_snoozed`,
`rupochta_unsnooze`

**Filters** — `rupochta_list_filters`, `rupochta_create_filter`,
`rupochta_update_filter`, `rupochta_delete_filter`

**Settings** — `rupochta_get_settings`, `rupochta_set_signature`,
`rupochta_set_autoreply`, `rupochta_set_forwarding`, `rupochta_save_template`,
`rupochta_delete_template`

**Directory and calendar** — `rupochta_search_contacts`,
`rupochta_list_calendar_events`, `rupochta_create_calendar_event`,
`rupochta_update_calendar_event`, `rupochta_delete_calendar_event`

Every tool takes `response_format` (`markdown` by default, `json` for the full
payload) and returns `structuredContent` matching its declared output schema.

## Things worth knowing

- **UIDs are per-folder.** A UID from `INBOX` means nothing in `Archive`. Always pass
  the folder a UID came from. `message_id` is the stable identifier across folders,
  and is what `rupochta_undo_move` uses.
- **Reading marks as read.** `rupochta_get_message` sets `\Seen`, which is why it is
  not annotated read-only. `rupochta_set_message_flags` with `seen: false` restores it.
- **Shared mailboxes.** Pass `mailbox_context: "shared:<id>"` (from
  `rupochta_list_shared_mailboxes`) to any folder or message tool.
- **Sending is irreversible.** `rupochta_send_message` delivers immediately. Use
  `rupochta_save_draft` when a human should review first, or `rupochta_schedule_send`
  for later delivery that can still be cancelled.
- **Search covers one folder per call**, because IMAP SEARCH does.
- **Responses are capped at 25 000 characters.** Lists halve themselves and say so;
  message bodies honour `max_body_chars`.

## Test

```bash
npm test
```

The suite builds the server, starts a stand-in RuPochta instance
(`test/mock-rupochta.mjs`) and drives the real MCP protocol over stdio: tool
surface and annotations, offset/limit paging over the server's `page`/`per_page`
API, session renewal after a cookie expires, the `Origin` header on writes,
`X-Mailbox-Context` on shared mailboxes, input validation, and the error text for
missing messages and an unreachable instance.

`evaluations/rupochta_evaluation.xml` holds ten questions answerable against that
fixture mailbox, for measuring how well a model drives these tools.

## Licence

MIT, same as RuPochta.
