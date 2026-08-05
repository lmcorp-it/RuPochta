/**
 * A stand-in for a RuPochta instance, good enough to exercise the MCP server
 * end to end: session cookies, the same-origin rule on writes, page/per_page
 * listing, search, message bodies, attachments and error shapes.
 *
 * It records every request so tests can assert on headers the real server
 * enforces (Origin, Cookie, X-Mailbox-Context).
 */

import { createServer } from "node:http";

const SESSION_COOKIE = "wmSID";
const VALID_TOKEN = "test-session-token";
const CREDENTIALS = { email: "demo@example.local", password: "demo-password" };

function makeMessages(count) {
  return Array.from({ length: count }, (_, index) => {
    const uid = String(1000 + index);
    return {
      uid,
      from: `sender${index}@example.local`,
      to: CREDENTIALS.email,
      subject: index === 3 ? "Invoice 2026-08 from Acme" : `Test message ${index}`,
      subject_base: `Test message ${index}`,
      date: `Mon, 0${(index % 9) + 1} Aug 2026 10:0${index % 10}:00 +0000`,
      message_id: `msg-${uid}@example.local`,
      in_reply_to: "",
      references: [],
      flags: index % 2 === 0 ? [] : ["\\Seen"],
      unread: index % 2 === 0,
      flagged: index === 5,
      has_attachments: index === 3,
      snippet: `Snippet for message ${index}`,
    };
  });
}

const MESSAGES = makeMessages(25);

function json(res, status, payload, headers = {}) {
  const body = JSON.stringify(payload);
  res.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "content-length": Buffer.byteLength(body),
    ...headers,
  });
  res.end(body);
}

function authorized(req) {
  const cookie = req.headers.cookie ?? "";
  return cookie.includes(`${SESSION_COOKIE}=${VALID_TOKEN}`);
}

async function readBody(req) {
  const chunks = [];
  for await (const chunk of req) {
    chunks.push(chunk);
  }
  if (!chunks.length) {
    return undefined;
  }
  const raw = Buffer.concat(chunks).toString("utf8");
  try {
    return JSON.parse(raw);
  } catch {
    return raw;
  }
}

export async function startMockRuPochta() {
  const requests = [];

  const server = createServer(async (req, res) => {
    const url = new URL(req.url, "http://127.0.0.1");
    const body = await readBody(req);
    requests.push({
      method: req.method,
      path: url.pathname,
      query: Object.fromEntries(url.searchParams),
      headers: req.headers,
      body,
    });

    // Mutating requests must look same-origin, exactly like get_session_or_401.
    if (!["GET", "HEAD", "OPTIONS"].includes(req.method) && url.pathname !== "/api/auth/login") {
      const expected = `http://127.0.0.1:${server.address().port}`;
      if (req.headers.origin !== expected) {
        json(res, 403, { detail: "Forbidden" });
        return;
      }
    }

    if (url.pathname === "/api/auth/login" && req.method === "POST") {
      if (body?.email !== CREDENTIALS.email || body?.password !== CREDENTIALS.password) {
        json(res, 401, { error: "Неверный логин или пароль почтового ящика." });
        return;
      }
      json(
        res,
        200,
        {
          ok: true,
          email: CREDENTIALS.email,
          display_name: "Demo User",
          mail_login: "demo",
          from_address: CREDENTIALS.email,
        },
        { "set-cookie": `${SESSION_COOKIE}=${VALID_TOKEN}; Path=/; HttpOnly; SameSite=lax` },
      );
      return;
    }

    if (!authorized(req)) {
      json(res, 401, { detail: "Не авторизован" });
      return;
    }

    if (url.pathname === "/api/me") {
      json(res, 200, {
        ad_login: "demo",
        email: CREDENTIALS.email,
        display_name: "Demo User",
        mail_login: "demo",
        from_address: CREDENTIALS.email,
        telegram_sso_account_available: false,
      });
      return;
    }

    if (url.pathname === "/api/folders" && req.method === "GET") {
      json(res, 200, {
        folders: [
          { name: "INBOX", label: "Входящие", unread: 13, total: 25 },
          { name: "Sent", label: "Отправленные", unread: 0, total: 4 },
          { name: "Drafts", label: "Черновики", unread: 0, total: 1 },
          { name: "Trash", label: "Корзина", unread: 0, total: 2 },
        ],
      });
      return;
    }

    if (url.pathname === "/api/messages" && req.method === "GET") {
      const folder = url.searchParams.get("folder") ?? "INBOX";
      if (folder !== "INBOX") {
        json(res, 200, { messages: [], total: 0, page: 1, per_page: 20 });
        return;
      }
      const page = Number(url.searchParams.get("page") ?? "1");
      const perPage = Number(url.searchParams.get("per_page") ?? "20");
      const unread = url.searchParams.get("unread");
      let pool = MESSAGES;
      if (unread === "true") {
        pool = pool.filter((message) => message.unread);
      } else if (unread === "false") {
        pool = pool.filter((message) => !message.unread);
      }
      const start = (page - 1) * perPage;
      json(res, 200, {
        messages: pool.slice(start, start + perPage),
        total: pool.length,
        page,
        per_page: perPage,
      });
      return;
    }

    if (url.pathname === "/api/messages/search" && req.method === "GET") {
      const query = (url.searchParams.get("q") ?? "").toLowerCase();
      const limit = Number(url.searchParams.get("limit") ?? "50");
      const hits = query
        ? MESSAGES.filter(
            (message) =>
              message.subject.toLowerCase().includes(query) ||
              message.from.toLowerCase().includes(query),
          )
        : [];
      json(res, 200, { messages: hits.slice(0, limit), total: hits.length, query, limit });
      return;
    }

    const attachmentMatch = /^\/api\/messages\/(\d+)\/attachment\/(\d+)$/.exec(url.pathname);
    if (attachmentMatch && req.method === "GET") {
      if (attachmentMatch[2] !== "0") {
        json(res, 404, { error: "Attachment not found" });
        return;
      }
      const payload = "invoice,amount\nACME-1,1200\n";
      res.writeHead(200, {
        "content-type": "text/csv",
        "content-disposition": "attachment; filename=\"invoice.csv\"; filename*=UTF-8''invoice.csv",
        "content-length": Buffer.byteLength(payload),
      });
      res.end(payload);
      return;
    }

    const flagsMatch = /^\/api\/messages\/(\d+)\/flags$/.exec(url.pathname);
    if (flagsMatch && req.method === "POST") {
      json(res, 200, { ok: true, message: { uid: flagsMatch[1], ...body } });
      return;
    }

    const messageMatch = /^\/api\/messages\/(\d+)$/.exec(url.pathname);
    if (messageMatch && req.method === "GET") {
      const found = MESSAGES.find((message) => message.uid === messageMatch[1]);
      if (!found) {
        json(res, 502, { error: "Message not found" });
        return;
      }
      json(res, 200, {
        ...found,
        folder: url.searchParams.get("folder") ?? "INBOX",
        cc: "",
        body_html: "<p>Hello <b>there</b>.</p><p>Second paragraph.</p>",
        body_text: "",
        attachments: found.has_attachments
          ? [{ filename: "invoice.csv", content_type: "text/csv", size: 27 }]
          : [],
        invite: null,
      });
      return;
    }

    if (url.pathname === "/api/messages/send" && req.method === "POST") {
      json(res, 200, {
        ok: true,
        smtp_ok: true,
        smtp_status: "submitted",
        recipient_count: String(body?.to ?? "").split(",").filter(Boolean).length,
        sent_copy_ok: true,
        sent_folder: "Sent",
        draft_deleted: null,
        warnings: [],
      });
      return;
    }

    if (url.pathname === "/api/scheduled/send" && req.method === "POST") {
      if (!body?.scheduled_at) {
        json(res, 400, { error: "scheduled_at must be a valid ISO datetime" });
        return;
      }
      json(res, 200, {
        ok: true,
        id: 42,
        scheduled_at: body.scheduled_at,
        to: [body.to],
        subject: body.subject,
      });
      return;
    }

    if (url.pathname === "/api/sieve/rules" && req.method === "GET") {
      json(res, 200, {
        ok: true,
        rules: [
          {
            id: 7,
            name: "Acme invoices",
            enabled: true,
            priority: 10,
            conditions: [{ field: "from", op: "contains", value: "acme.com" }],
            actions: [{ type: "move", value: "Invoices" }],
            created_at: "2026-07-01 10:00:00",
            updated_at: "2026-07-02 11:00:00",
          },
        ],
        count: 1,
      });
      return;
    }

    if (url.pathname === "/api/settings/profile" && req.method === "GET") {
      json(res, 200, {
        ok: true,
        email: CREDENTIALS.email,
        display_name: "Demo User",
        mail_login: "demo",
        from_address: CREDENTIALS.email,
        signature: "— Demo User",
        templates: [{ id: 3, name: "Onboarding", subject: "Welcome", body: "Hi there" }],
      });
      return;
    }

    if (url.pathname === "/api/shared_mailboxes" && req.method === "GET") {
      json(res, 200, {
        ok: true,
        mailboxes: [{ id: 4, email: "support@example.local", display_name: "Support", can_send: true }],
      });
      return;
    }

    json(res, 404, { detail: "Not Found" });
  });

  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address();

  return {
    baseUrl: `http://127.0.0.1:${port}`,
    credentials: CREDENTIALS,
    messages: MESSAGES,
    requests,
    close: () => new Promise((resolve) => server.close(resolve)),
  };
}
