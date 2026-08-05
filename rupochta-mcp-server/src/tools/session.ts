/**
 * Session tools: sign in, inspect the signed-in mailbox, sign out.
 */

import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import type { RuPochtaClient } from "../services/client.js";
import { guard, keyValueLines, renderSingle, ResponseFormat } from "../services/format.js";
import { bool, str } from "../services/normalize.js";
import { responseFormatField } from "../schemas/common.js";

interface RawMe {
  email?: string;
  display_name?: string;
  mail_login?: string;
  from_address?: string;
  ad_login?: string;
  telegram_sso_account_available?: boolean;
}

const LoginInput = z
  .object({
    email: z
      .string()
      .min(3, "email must be at least 3 characters")
      .max(320, "email must not exceed 320 characters")
      .describe(
        "Mailbox address or login, e.g. 'demo@example.local'. A bare login is completed with the instance's MAIL_DOMAIN",
      ),
    password: z
      .string()
      .min(1, "password must not be empty")
      .describe("Mailbox password — verified against IMAP, never stored on disk by this server"),
    response_format: responseFormatField,
  })
  .strict();

const WhoamiInput = z
  .object({
    response_format: responseFormatField,
  })
  .strict();

const LogoutInput = z
  .object({
    forget_credentials: z
      .boolean()
      .default(false)
      .describe(
        "Also drop credentials held in memory, so later tool calls cannot sign back in automatically (default false)",
      ),
  })
  .strict();

const IdentityOutput = {
  ok: z.boolean(),
  email: z.string(),
  display_name: z.string(),
  mail_login: z.string(),
  from_address: z.string(),
  base_url: z.string(),
};

export function registerSessionTools(server: McpServer, client: RuPochtaClient): void {
  server.registerTool(
    "rupochta_login",
    {
      title: "Sign in to RuPochta",
      description: `Authenticate a mailbox against the RuPochta instance and keep the session for later tool calls.

RuPochta verifies the password against the real IMAP server, so this proves the mailbox works end to end. The session cookie lives in this process's memory only, and is reused (and silently renewed) by every other rupochta_* tool.

Call this only when the server was started without RUPOCHTA_EMAIL / RUPOCHTA_PASSWORD, or to switch to a different mailbox.

Args:
  - email (string): mailbox address or login, e.g. 'demo@example.local'
  - password (string): the mailbox password
  - response_format ('markdown' | 'json'): output format (default: 'markdown')

Returns:
  {
    "ok": boolean,             // true when the session was established
    "email": string,           // the address RuPochta resolved the login to
    "display_name": string,    // human-readable sender name
    "mail_login": string,      // local part used for IMAP
    "from_address": string,    // address outgoing mail is sent from
    "base_url": string         // RuPochta instance this session belongs to
  }

Examples:
  - Use when: "Log in as demo@example.local" -> email='demo@example.local', password='...'
  - Use when: another tool answered "not signed in to RuPochta"
  - Don't use when: RUPOCHTA_EMAIL/RUPOCHTA_PASSWORD are already configured — sign-in then happens automatically

Error Handling:
  - HTTP 401: wrong mailbox login or password; RuPochta may suggest a resolved login in the message
  - HTTP 429: too many failed attempts from this address — wait about five minutes
  - HTTP 503: password login is disabled on this instance (SSO only), or IMAP is unreachable`,
      inputSchema: LoginInput,
      outputSchema: IdentityOutput,
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: true,
      },
    },
    guard(async (args: z.infer<typeof LoginInput>) => {
      const identity = await client.login(args.email, args.password);
      const structured = {
        ok: true,
        email: identity.email,
        display_name: identity.displayName,
        mail_login: identity.mailLogin,
        from_address: identity.fromAddress,
        base_url: client.baseUrl,
      };
      return renderSingle({
        format: args.response_format,
        structured,
        markdown: () =>
          [
            `# Signed in to RuPochta`,
            "",
            ...keyValueLines([
              ["Mailbox", identity.email],
              ["Display name", identity.displayName],
              ["Sends as", identity.fromAddress],
              ["Instance", client.baseUrl],
            ]),
          ].join("\n"),
      });
    }),
  );

  server.registerTool(
    "rupochta_whoami",
    {
      title: "Show the signed-in mailbox",
      description: `Return the mailbox identity of the current RuPochta session.

Use this to confirm which mailbox subsequent tools will act on, and to check that the session is still alive before a long sequence of calls. Signs in first when credentials are configured.

Args:
  - response_format ('markdown' | 'json'): output format (default: 'markdown')

Returns:
  {
    "ok": boolean,
    "email": string,           // mailbox address
    "display_name": string,    // name shown to recipients
    "mail_login": string,      // IMAP login local part
    "from_address": string,    // address outgoing mail uses
    "base_url": string         // instance URL
  }

Examples:
  - Use when: "Which mailbox am I working with?"
  - Use when: verifying a session before sending mail
  - Don't use when: you need folder counts (use rupochta_list_folders instead)

Error Handling:
  - Returns "not signed in to RuPochta" if no session and no credentials are configured — call rupochta_login`,
      inputSchema: WhoamiInput,
      outputSchema: IdentityOutput,
      annotations: {
        readOnlyHint: true,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: true,
      },
    },
    guard(async (args: z.infer<typeof WhoamiInput>) => {
      const me = await client.request<RawMe>("/api/me");
      const structured = {
        ok: true,
        email: str(me.email),
        display_name: str(me.display_name),
        mail_login: str(me.mail_login),
        from_address: str(me.from_address),
        base_url: client.baseUrl,
      };
      return renderSingle({
        format: args.response_format,
        structured,
        markdown: () =>
          [
            "# Current RuPochta session",
            "",
            ...keyValueLines([
              ["Mailbox", structured.email],
              ["Display name", structured.display_name],
              ["Sends as", structured.from_address],
              ["Directory login", str(me.ad_login)],
              ["Telegram sign-in available", bool(me.telegram_sso_account_available) ? "yes" : "no"],
              ["Instance", client.baseUrl],
            ]),
          ].join("\n"),
      });
    }),
  );

  server.registerTool(
    "rupochta_logout",
    {
      title: "End the RuPochta session",
      description: `Invalidate the current RuPochta session server-side and drop it from memory.

Args:
  - forget_credentials (boolean): also discard in-memory credentials so later calls cannot sign back in (default: false)

Returns:
  { "ok": boolean, "credentials_retained": boolean }

Examples:
  - Use when: "Log out of the mailbox"
  - Use when: switching between mailboxes on a shared machine -> forget_credentials=true
  - Don't use when: you simply finished a task; sessions expire on their own after 8 hours

Error Handling:
  - Never fails on an already-expired session; it just clears local state`,
      inputSchema: LogoutInput,
      outputSchema: {
        ok: z.boolean(),
        credentials_retained: z.boolean(),
      },
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: true,
      },
    },
    guard(async (args: z.infer<typeof LogoutInput>) => {
      await client.logout();
      if (args.forget_credentials) {
        client.forgetCredentials();
      }
      const structured = { ok: true, credentials_retained: !args.forget_credentials };
      return renderSingle({
        format: ResponseFormat.MARKDOWN,
        structured,
        markdown: () =>
          args.forget_credentials
            ? "Signed out. Credentials were discarded — call rupochta_login before using other tools."
            : "Signed out. Configured credentials are still available, so the next tool call signs in again.",
      });
    }),
  );
}
