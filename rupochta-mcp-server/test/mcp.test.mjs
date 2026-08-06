/**
 * End-to-end tests: a real MCP client talks to the built server over stdio,
 * and the server talks to a stand-in RuPochta instance.
 *
 * Run with `npm test` (builds first).
 */

import assert from "node:assert/strict";
import { after, before, describe, it } from "node:test";
import { fileURLToPath } from "node:url";
import path from "node:path";

import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

import { startMockRuPochta } from "./mock-rupochta.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const entryPoint = path.join(here, "..", "dist", "index.js");

let mock;
let client;

async function connect(env) {
  const transport = new StdioClientTransport({
    command: process.execPath,
    args: [entryPoint],
    env: { PATH: process.env.PATH ?? "", ...env },
    stderr: "ignore",
  });
  const mcp = new Client({ name: "rupochta-test-client", version: "1.0.0" });
  await mcp.connect(transport);
  return mcp;
}

function textOf(result) {
  return result.content
    .filter((block) => block.type === "text")
    .map((block) => block.text)
    .join("\n");
}

before(async () => {
  mock = await startMockRuPochta();
  client = await connect({
    RUPOCHTA_BASE_URL: mock.baseUrl,
    RUPOCHTA_EMAIL: mock.credentials.email,
    RUPOCHTA_PASSWORD: mock.credentials.password,
    // The e2e suite below exercises real write tools (send/schedule/flags),
    // so it must opt out of the SEC-006 read-only default explicitly.
    RUPOCHTA_READ_ONLY: "0",
  });
});

after(async () => {
  await client?.close();
  await mock?.close();
});

describe("tool surface", () => {
  it("registers every documented tool with a schema and annotations", async () => {
    const { tools } = await client.listTools();
    const names = tools.map((tool) => tool.name).sort();

    const expected = [
      "rupochta_bulk_message_action",
      "rupochta_cancel_scheduled",
      "rupochta_create_calendar_event",
      "rupochta_create_filter",
      "rupochta_create_folder",
      "rupochta_delete_calendar_event",
      "rupochta_delete_filter",
      "rupochta_delete_folder",
      "rupochta_delete_message",
      "rupochta_delete_template",
      "rupochta_get_attachment",
      "rupochta_get_message",
      "rupochta_get_settings",
      "rupochta_list_calendar_events",
      "rupochta_list_filters",
      "rupochta_list_folders",
      "rupochta_list_messages",
      "rupochta_list_scheduled",
      "rupochta_list_shared_mailboxes",
      "rupochta_list_snoozed",
      "rupochta_login",
      "rupochta_logout",
      "rupochta_move_message",
      "rupochta_retry_scheduled",
      "rupochta_save_draft",
      "rupochta_save_template",
      "rupochta_schedule_send",
      "rupochta_search_contacts",
      "rupochta_search_messages",
      "rupochta_send_message",
      "rupochta_set_autoreply",
      "rupochta_set_forwarding",
      "rupochta_set_message_flags",
      "rupochta_set_signature",
      "rupochta_snooze_messages",
      "rupochta_undo_move",
      "rupochta_unsnooze",
      "rupochta_update_calendar_event",
      "rupochta_update_filter",
      "rupochta_whoami",
    ];
    assert.deepEqual(names, expected);

    for (const tool of tools) {
      assert.ok(tool.description && tool.description.length > 120, `${tool.name} needs a real description`);
      assert.equal(tool.inputSchema.type, "object", `${tool.name} must expose an object input schema`);
      assert.ok(tool.annotations, `${tool.name} must carry annotations`);
      assert.equal(typeof tool.annotations.readOnlyHint, "boolean");
    }
  });

  it("marks destructive tools as destructive and read tools as read-only", async () => {
    const { tools } = await client.listTools();
    const byName = Object.fromEntries(tools.map((tool) => [tool.name, tool]));

    assert.equal(byName.rupochta_list_messages.annotations.readOnlyHint, true);
    assert.equal(byName.rupochta_delete_message.annotations.destructiveHint, true);
    assert.equal(byName.rupochta_delete_folder.annotations.destructiveHint, true);
    // Reading a message marks it \Seen, so it must not claim to be read-only.
    assert.equal(byName.rupochta_get_message.annotations.readOnlyHint, false);
  });
});

describe("session", () => {
  it("signs in automatically and reports the mailbox", async () => {
    const result = await client.callTool({ name: "rupochta_whoami", arguments: {} });
    assert.equal(result.isError, undefined);
    assert.equal(result.structuredContent.email, mock.credentials.email);
    assert.match(textOf(result), /Demo User/);
  });

  it("recovers from an expired session cookie by signing in again", async () => {
    const stale = await connect({
      RUPOCHTA_BASE_URL: mock.baseUrl,
      RUPOCHTA_EMAIL: mock.credentials.email,
      RUPOCHTA_PASSWORD: mock.credentials.password,
      RUPOCHTA_SESSION_COOKIE: "expired-token",
    });
    try {
      const result = await stale.callTool({ name: "rupochta_list_folders", arguments: {} });
      assert.equal(result.isError, undefined);
      assert.equal(result.structuredContent.count, 4);
    } finally {
      await stale.close();
    }
  });

  it("reports bad credentials with the server's own message", async () => {
    const result = await client.callTool({
      name: "rupochta_login",
      arguments: { email: mock.credentials.email, password: "wrong" },
    });
    assert.equal(result.isError, true);
    assert.match(textOf(result), /HTTP 401/);
    assert.match(textOf(result), /Неверный логин или пароль/);
  });
});

describe("folders", () => {
  it("lists folders with counts in markdown", async () => {
    const result = await client.callTool({ name: "rupochta_list_folders", arguments: {} });
    assert.equal(result.structuredContent.count, 4);
    assert.equal(result.structuredContent.total_unread, 13);
    assert.match(textOf(result), /INBOX/);
  });

  it("returns raw JSON on request", async () => {
    const result = await client.callTool({
      name: "rupochta_list_folders",
      arguments: { response_format: "json" },
    });
    const parsed = JSON.parse(textOf(result));
    assert.equal(parsed.folders[0].name, "INBOX");
  });
});

describe("messages", () => {
  it("pages with offset/limit across the server's page/per_page API", async () => {
    const result = await client.callTool({
      name: "rupochta_list_messages",
      arguments: { folder: "INBOX", limit: 5, offset: 7 },
    });
    const payload = result.structuredContent;
    assert.equal(payload.total, 25);
    assert.equal(payload.count, 5);
    assert.equal(payload.offset, 7);
    assert.equal(payload.has_more, true);
    assert.equal(payload.next_offset, 12);
    assert.deepEqual(
      payload.messages.map((message) => message.uid),
      mock.messages.slice(7, 12).map((message) => message.uid),
    );
  });

  it("passes filters through to the API", async () => {
    const result = await client.callTool({
      name: "rupochta_list_messages",
      arguments: { unread: true, limit: 50 },
    });
    assert.ok(result.structuredContent.messages.every((message) => message.unread));
    const call = mock.requests.filter((entry) => entry.path === "/api/messages").at(-1);
    assert.equal(call.query.unread, "true");
  });

  it("explains an empty page instead of returning a bare empty list", async () => {
    const result = await client.callTool({
      name: "rupochta_list_messages",
      arguments: { folder: "Sent" },
    });
    assert.equal(result.structuredContent.count, 0);
    assert.match(textOf(result), /No messages match in Sent/);
  });

  it("searches and reports the match count", async () => {
    const result = await client.callTool({
      name: "rupochta_search_messages",
      arguments: { query: "invoice" },
    });
    assert.equal(result.structuredContent.count, 1);
    assert.match(textOf(result), /Invoice 2026-08 from Acme/);
  });

  it("converts an HTML body to text and lists attachments", async () => {
    const result = await client.callTool({
      name: "rupochta_get_message",
      arguments: { uid: "1003", folder: "INBOX" },
    });
    const payload = result.structuredContent;
    assert.equal(payload.body_format, "html-derived");
    assert.match(payload.body, /Hello there/);
    assert.equal(payload.attachments[0].filename, "invoice.csv");
    assert.equal(payload.has_invite, false);
  });

  it("truncates a long body at max_body_chars", async () => {
    const result = await client.callTool({
      name: "rupochta_get_message",
      arguments: { uid: "1003", max_body_chars: 200 },
    });
    assert.equal(result.structuredContent.body_truncated, false);
    const short = await client.callTool({
      name: "rupochta_get_message",
      arguments: { uid: "1003", max_body_chars: 200, include_html: true },
    });
    assert.equal(short.structuredContent.body_format, "text");
  });

  it("returns an actionable error for a missing message", async () => {
    const result = await client.callTool({
      name: "rupochta_get_message",
      arguments: { uid: "999999" },
    });
    assert.equal(result.isError, true);
    const text = textOf(result);
    assert.match(text, /HTTP 502/);
    assert.match(text, /Message not found/);
    assert.match(text, /upstream mail path/);
  });

  it("downloads a text attachment as readable text", async () => {
    const result = await client.callTool({
      name: "rupochta_get_attachment",
      arguments: { uid: "1003", index: 0 },
    });
    assert.equal(result.structuredContent.filename, "invoice.csv");
    assert.equal(result.structuredContent.encoding, "text");
    assert.match(textOf(result), /ACME-1,1200/);
  });

  it("rejects a UID that is not numeric before calling the API", async () => {
    const result = await client.callTool({
      name: "rupochta_get_message",
      arguments: { uid: "not-a-uid" },
    });
    assert.equal(result.isError, true);
    assert.match(textOf(result), /uid must be the numeric IMAP UID/);
    const calls = mock.requests.filter((entry) => entry.path === "/api/messages/not-a-uid");
    assert.equal(calls.length, 0, "invalid input must never reach the API");
  });

  it("requires to_folder for a bulk move", async () => {
    const result = await client.callTool({
      name: "rupochta_bulk_message_action",
      arguments: { uids: ["1000"], action: "move" },
    });
    assert.equal(result.isError, true);
    assert.match(textOf(result), /requires to_folder/);
  });
});

describe("writes", () => {
  it("sends mail with the Origin header RuPochta demands", async () => {
    const result = await client.callTool({
      name: "rupochta_send_message",
      arguments: {
        to: "alice@example.local",
        subject: "Hello",
        body: "Plain text body",
      },
    });
    assert.equal(result.isError, undefined);
    assert.equal(result.structuredContent.smtp_ok, true);
    assert.equal(result.structuredContent.sent_copy_ok, true);

    const sent = mock.requests.filter((entry) => entry.path === "/api/messages/send").at(-1);
    assert.equal(sent.headers.origin, mock.baseUrl);
    assert.equal(sent.body.subject, "Hello");
    assert.equal(sent.body.to, "alice@example.local");
  });

  it("sends scheduled_at when scheduling a send", async () => {
    const when = "2099-01-01T09:00:00Z";
    const result = await client.callTool({
      name: "rupochta_schedule_send",
      arguments: { to: "alice@example.local", subject: "Later", body: "text", scheduled_at: when },
    });
    assert.equal(result.structuredContent.id, 42);
    const call = mock.requests.filter((entry) => entry.path === "/api/scheduled/send").at(-1);
    assert.equal(call.body.scheduled_at, when);
  });

  it("targets a shared mailbox through X-Mailbox-Context", async () => {
    await client.callTool({
      name: "rupochta_list_messages",
      arguments: { mailbox_context: "shared:4", folder: "INBOX", limit: 1 },
    });
    const call = mock.requests.filter((entry) => entry.path === "/api/messages").at(-1);
    assert.equal(call.headers["x-mailbox-context"], "shared:4");
  });

  it("rejects a malformed mailbox_context locally", async () => {
    const before = mock.requests.length;
    const result = await client.callTool({
      name: "rupochta_list_messages",
      arguments: { mailbox_context: "shared:abc" },
    });
    assert.equal(result.isError, true);
    assert.match(textOf(result), /mailbox_context must be/);
    assert.equal(mock.requests.length, before, "invalid input must never reach the API");
  });

  it("marks a message unread again", async () => {
    const result = await client.callTool({
      name: "rupochta_set_message_flags",
      arguments: { uid: "1003", seen: false },
    });
    assert.equal(result.structuredContent.seen, false);
    assert.match(textOf(result), /now unread/);
  });

  it("refuses a flag call that changes nothing", async () => {
    const result = await client.callTool({
      name: "rupochta_set_message_flags",
      arguments: { uid: "1003" },
    });
    assert.equal(result.isError, true);
    assert.match(textOf(result), /Pass seen and\/or flagged/);
  });
});

describe("read-only mode (SEC-006/SEC-007)", () => {
  it("blocks write tools by default and still allows reads", async () => {
    const readOnly = await connect({
      RUPOCHTA_BASE_URL: mock.baseUrl,
      RUPOCHTA_EMAIL: mock.credentials.email,
      RUPOCHTA_PASSWORD: mock.credentials.password,
      // RUPOCHTA_READ_ONLY intentionally unset: must default to read-only.
    });
    try {
      const before = mock.requests.length;
      const sendBefore = mock.requests.filter((entry) => entry.path === "/api/messages/send").length;
      const send = await readOnly.callTool({
        name: "rupochta_send_message",
        arguments: { to: "alice@example.local", subject: "Hi", body: "text" },
      });
      assert.equal(send.isError, true);
      assert.match(textOf(send), /read-only mode/);
      assert.match(textOf(send), /RUPOCHTA_READ_ONLY=0/);
      const sendAfter = mock.requests.filter((entry) => entry.path === "/api/messages/send").length;
      assert.equal(sendAfter, sendBefore, "read-only mode must never reach a mutating endpoint");

      const list = await readOnly.callTool({ name: "rupochta_list_folders", arguments: {} });
      assert.equal(list.isError, undefined, "reads must still work in read-only mode");
      assert.ok(mock.requests.length > before);
    } finally {
      await readOnly.close();
    }
  });
});

describe("settings and filters", () => {
  it("reads several settings sections in one call", async () => {
    const result = await client.callTool({
      name: "rupochta_get_settings",
      arguments: { sections: ["profile", "signature", "templates"] },
    });
    assert.equal(result.structuredContent.signature, "— Demo User");
    assert.equal(result.structuredContent.templates[0].name, "Onboarding");
    assert.equal(result.structuredContent.forwarding, undefined);
  });

  it("describes filter rules in readable form", async () => {
    const result = await client.callTool({ name: "rupochta_list_filters", arguments: {} });
    assert.equal(result.structuredContent.count, 1);
    assert.match(textOf(result), /from contains "acme.com"/);
    assert.match(textOf(result), /move → Invoices/);
  });

  it("lists shared mailboxes with their context token", async () => {
    const result = await client.callTool({ name: "rupochta_list_shared_mailboxes", arguments: {} });
    assert.equal(result.structuredContent.mailboxes[0].mailbox_context, "shared:4");
  });
});

describe("transport failures", () => {
  it("explains an unreachable instance", async () => {
    const offline = await connect({
      RUPOCHTA_BASE_URL: "http://127.0.0.1:1",
      RUPOCHTA_EMAIL: mock.credentials.email,
      RUPOCHTA_PASSWORD: mock.credentials.password,
    });
    try {
      const result = await offline.callTool({ name: "rupochta_list_folders", arguments: {} });
      assert.equal(result.isError, true);
      assert.match(textOf(result), /could not reach RuPochta at http:\/\/127\.0\.0\.1:1/);
      assert.match(textOf(result), /GET \/health/);
    } finally {
      await offline.close();
    }
  });
});
