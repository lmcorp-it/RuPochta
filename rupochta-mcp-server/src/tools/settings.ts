/**
 * Mailbox settings: signature, auto-reply, forwarding, reply templates.
 */

import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import type { RuPochtaClient } from "../services/client.js";
import { guard, inline, keyValueLines, renderSingle } from "../services/format.js";
import { bool, normalizeTemplate, num, str } from "../services/normalize.js";
import { responseFormatField } from "../schemas/common.js";
import type { RawTemplate, Template } from "../types.js";

const SECTIONS = ["profile", "signature", "forwarding", "autoreply", "templates"] as const;

const GetSettingsInput = z
  .object({
    sections: z
      .array(z.enum(SECTIONS))
      .min(1)
      .default([...SECTIONS])
      .describe("Which settings to read. Defaults to all of them"),
    response_format: responseFormatField,
  })
  .strict();

const SignatureInput = z
  .object({
    signature: z
      .string()
      .max(20000, "signature must not exceed 20000 characters")
      .describe("Signature text or HTML. Pass an empty string to remove it"),
  })
  .strict();

const AutoReplyInput = z
  .object({
    enabled: z.boolean().describe("Whether the auto-reply is active"),
    subject: z.string().max(998).default("").describe("Subject of the automatic reply"),
    body: z.string().max(20000).default("").describe("Body of the automatic reply"),
    start_date: z
      .string()
      .optional()
      .describe("First day the auto-reply runs, 'YYYY-MM-DD' (optional)"),
    end_date: z.string().optional().describe("Last day the auto-reply runs, 'YYYY-MM-DD' (optional)"),
    repeat_days: z
      .number()
      .int()
      .min(1)
      .max(365)
      .default(1)
      .describe("Days to wait before replying to the same sender again (default 1)"),
  })
  .strict();

const ForwardingInput = z
  .object({
    enabled: z.boolean().describe("Whether incoming mail is forwarded"),
    address: z
      .string()
      .max(320)
      .default("")
      .describe("Destination address; required when enabled is true and it must differ from this mailbox"),
    keep_copy: z
      .boolean()
      .default(true)
      .describe("Keep a copy in this mailbox as well as forwarding (default true)"),
  })
  .strict();

const TemplateInput = z
  .object({
    name: z.string().min(1).max(100).describe("Template name"),
    subject: z.string().max(998).default("").describe("Default subject for the template"),
    body: z.string().max(50000).default("").describe("Template body"),
    id: z
      .number()
      .int()
      .min(1)
      .optional()
      .describe("Existing template id to overwrite; omit to create a new one"),
  })
  .strict();

const TemplateIdInput = z
  .object({ id: z.number().int().min(1).describe("Template id from rupochta_get_settings") })
  .strict();

interface SettingsPayload {
  profile?: Record<string, unknown>;
  signature?: string;
  forwarding?: Record<string, unknown>;
  autoreply?: Record<string, unknown>;
  templates?: Template[];
}

export function registerSettingsTools(server: McpServer, client: RuPochtaClient): void {
  server.registerTool(
    "rupochta_get_settings",
    {
      title: "Read mailbox settings",
      description: `Read mailbox settings in one call: profile, signature, forwarding, auto-reply and reply templates.

Each section is fetched independently, so ask only for what you need. A section that this instance has not configured comes back empty rather than failing the whole call.

Args:
  - sections (string[]): any of 'profile', 'signature', 'forwarding', 'autoreply', 'templates' (default: all)
  - response_format ('markdown' | 'json'): output format (default: 'markdown')

Returns:
  {
    "profile":  { "email": string, "display_name": string, "from_address": string, "mail_login": string },
    "signature": string,
    "forwarding": { "enabled": boolean, "address": string, "keep_copy": boolean },
    "autoreply": { "enabled": boolean, "subject": string, "body": string, "start_date": string, "end_date": string, "repeat_days": number },
    "templates": [ { "id": number, "name": string, "subject": string, "body": string } ]
  }
  Only requested sections are present.

Examples:
  - Use when: "Is my out-of-office on?" -> sections=['autoreply']
  - Use when: "What is my signature?" -> sections=['signature']
  - Use when: before editing any setting, to see the current value
  - Don't use when: you need filter rules (use rupochta_list_filters)

Error Handling:
  - HTTP 502 on the autoreply or forwarding section means the mail control plane is unreachable`,
      inputSchema: GetSettingsInput,
      outputSchema: {
        profile: z.record(z.unknown()).optional(),
        signature: z.string().optional(),
        forwarding: z.record(z.unknown()).optional(),
        autoreply: z.record(z.unknown()).optional(),
        templates: z.array(z.record(z.unknown())).optional(),
      },
      annotations: {
        readOnlyHint: true,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: true,
      },
    },
    guard(async (args: z.infer<typeof GetSettingsInput>) => {
      const wanted = new Set(args.sections);
      const structured: SettingsPayload = {};

      if (wanted.has("profile") || wanted.has("signature") || wanted.has("templates")) {
        const profile = await client.request<Record<string, unknown>>("/api/settings/profile");
        if (wanted.has("profile")) {
          structured.profile = {
            email: str(profile.email),
            display_name: str(profile.display_name),
            from_address: str(profile.from_address),
            mail_login: str(profile.mail_login),
          };
        }
        if (wanted.has("signature")) {
          structured.signature = str(profile.signature);
        }
        if (wanted.has("templates")) {
          const templates = Array.isArray(profile.templates) ? (profile.templates as RawTemplate[]) : [];
          structured.templates = templates.map(normalizeTemplate) as unknown as Template[];
        }
      }
      if (wanted.has("forwarding")) {
        const forwarding = await client.request<Record<string, unknown>>("/api/settings/forwarding");
        structured.forwarding = {
          enabled: bool(forwarding.enabled),
          address: str(forwarding.address),
          keep_copy: bool(forwarding.keep_copy, true),
        };
      }
      if (wanted.has("autoreply")) {
        const autoreply = await client.request<Record<string, unknown>>("/api/settings/autoreply");
        structured.autoreply = {
          enabled: bool(autoreply.enabled),
          subject: str(autoreply.subject),
          body: str(autoreply.body),
          start_date: str(autoreply.start_date),
          end_date: str(autoreply.end_date),
          repeat_days: num(autoreply.repeat_days, 1),
        };
      }

      return renderSingle({
        format: args.response_format,
        structured: structured as unknown as Record<string, unknown>,
        markdown: () => {
          const lines: string[] = ["# Mailbox settings"];
          if (structured.profile) {
            lines.push(
              "",
              "## Profile",
              "",
              ...keyValueLines([
                ["Mailbox", str(structured.profile.email)],
                ["Display name", str(structured.profile.display_name)],
                ["Sends as", str(structured.profile.from_address)],
              ]),
            );
          }
          if (structured.signature !== undefined) {
            lines.push(
              "",
              "## Signature",
              "",
              structured.signature ? inline(structured.signature, 400) : "(none)",
            );
          }
          if (structured.forwarding) {
            lines.push(
              "",
              "## Forwarding",
              "",
              ...keyValueLines([
                ["Enabled", bool(structured.forwarding.enabled) ? "yes" : "no"],
                ["Address", str(structured.forwarding.address) || "(none)"],
                ["Keeps a copy", bool(structured.forwarding.keep_copy, true) ? "yes" : "no"],
              ]),
            );
          }
          if (structured.autoreply) {
            lines.push(
              "",
              "## Auto-reply",
              "",
              ...keyValueLines([
                ["Enabled", bool(structured.autoreply.enabled) ? "yes" : "no"],
                ["Subject", str(structured.autoreply.subject) || "(none)"],
                ["Window", `${str(structured.autoreply.start_date) || "—"} … ${str(structured.autoreply.end_date) || "—"}`],
                ["Repeats after", `${num(structured.autoreply.repeat_days, 1)} day(s)`],
              ]),
            );
          }
          if (structured.templates) {
            lines.push(
              "",
              `## Reply templates (${structured.templates.length})`,
              "",
              ...(structured.templates.length
                ? structured.templates.map(
                    (tpl) => `- **${inline(tpl.name, 60)}** (id ${tpl.id}) — ${inline(tpl.subject || "(no subject)", 60)}`,
                  )
                : ["(none)"]),
            );
          }
          return lines.join("\n");
        },
      });
    }),
  );

  server.registerTool(
    "rupochta_set_signature",
    {
      title: "Set the mail signature",
      description: `Replace the signature appended to outgoing mail.

Args:
  - signature (string): the new signature (plain text or HTML). An empty string removes it

Returns:
  { "ok": boolean, "length": number }

Examples:
  - Use when: "Update my signature to include my phone number"
  - Use when: clearing a signature -> signature=''
  - Don't use when: you have not read the current one and the user asked for a tweak — read it first with rupochta_get_settings

Error Handling:
  - HTTP 401: the session expired; sign in again`,
      inputSchema: SignatureInput,
      outputSchema: { ok: z.boolean(), length: z.number() },
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: true,
      },
    },
    guard(async (args: z.infer<typeof SignatureInput>) => {
      await client.request("/api/settings/signature", {
        method: "PUT",
        body: { signature: args.signature },
      });
      const structured = { ok: true, length: args.signature.length };
      return renderSingle({
        format: "markdown",
        structured,
        markdown: () =>
          args.signature
            ? `Signature updated (${args.signature.length} characters).`
            : "Signature removed.",
      });
    }),
  );

  server.registerTool(
    "rupochta_set_autoreply",
    {
      title: "Configure the auto-reply",
      description: `Turn the out-of-office auto-reply on or off and set its text and date window.

RuPochta pushes this into the server-side Sieve script, so replies go out even when no client is connected. Each sender gets at most one reply per 'repeat_days'.

Args:
  - enabled (boolean): whether the auto-reply runs
  - subject (string): subject of the reply (default '')
  - body (string): body of the reply (default '')
  - start_date (string, optional): 'YYYY-MM-DD' first active day
  - end_date (string, optional): 'YYYY-MM-DD' last active day
  - repeat_days (number): days before replying to the same sender again (default 1)

Returns:
  { "ok": boolean, "enabled": boolean, "subject": string, "start_date": string, "end_date": string, "repeat_days": number }

Examples:
  - Use when: "Set an out-of-office until the 20th" -> enabled=true, end_date='2026-08-20'
  - Use when: "Turn off my auto-reply" -> enabled=false
  - Don't use when: you want to forward mail instead (use rupochta_set_forwarding)

Error Handling:
  - HTTP 400: malformed dates, or enabled=true with an empty body`,
      inputSchema: AutoReplyInput,
      outputSchema: {
        ok: z.boolean(),
        enabled: z.boolean(),
        subject: z.string(),
        start_date: z.string(),
        end_date: z.string(),
        repeat_days: z.number(),
      },
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: true,
      },
    },
    guard(async (args: z.infer<typeof AutoReplyInput>) => {
      const payload = await client.request<Record<string, unknown>>("/api/settings/autoreply", {
        method: "PUT",
        body: {
          enabled: args.enabled,
          subject: args.subject,
          body: args.body,
          ...(args.start_date ? { start_date: args.start_date } : {}),
          ...(args.end_date ? { end_date: args.end_date } : {}),
          repeat_days: args.repeat_days,
        },
      });
      const structured = {
        ok: true,
        enabled: bool(payload.enabled, args.enabled),
        subject: str(payload.subject, args.subject),
        start_date: str(payload.start_date, args.start_date ?? ""),
        end_date: str(payload.end_date, args.end_date ?? ""),
        repeat_days: num(payload.repeat_days, args.repeat_days),
      };
      return renderSingle({
        format: "markdown",
        structured,
        markdown: () =>
          structured.enabled
            ? `Auto-reply is on ("${structured.subject || "(no subject)"}")${structured.end_date ? `, until ${structured.end_date}` : ""}.`
            : "Auto-reply is off.",
      });
    }),
  );

  server.registerTool(
    "rupochta_set_forwarding",
    {
      title: "Configure mail forwarding",
      description: `Forward incoming mail to another address, optionally keeping a copy locally.

This changes where the user's mail goes — confirm the destination address with the user before enabling it.

Args:
  - enabled (boolean): whether forwarding is active
  - address (string): destination address; required when enabled is true and must differ from this mailbox
  - keep_copy (boolean): also keep the message in this mailbox (default true)

Returns:
  { "ok": boolean, "enabled": boolean, "address": string, "keep_copy": boolean }

Examples:
  - Use when: "Forward my mail to my personal address while I'm away" -> enabled=true, address='me@example.org'
  - Use when: "Stop forwarding" -> enabled=false
  - Don't use when: an auto-reply is what was asked for (use rupochta_set_autoreply)

Error Handling:
  - HTTP 400: the address is malformed, empty while enabled, or points back at this mailbox`,
      inputSchema: ForwardingInput,
      outputSchema: {
        ok: z.boolean(),
        enabled: z.boolean(),
        address: z.string(),
        keep_copy: z.boolean(),
      },
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: true,
      },
    },
    guard(async (args: z.infer<typeof ForwardingInput>) => {
      const payload = await client.request<Record<string, unknown>>("/api/settings/forwarding", {
        method: "PUT",
        body: { enabled: args.enabled, address: args.address, keep_copy: args.keep_copy },
      });
      const structured = {
        ok: true,
        enabled: bool(payload.enabled, args.enabled),
        address: str(payload.address, args.address),
        keep_copy: bool(payload.keep_copy, args.keep_copy),
      };
      return renderSingle({
        format: "markdown",
        structured,
        markdown: () =>
          structured.enabled
            ? `Forwarding to ${structured.address}${structured.keep_copy ? ", keeping a local copy" : ", without keeping a local copy"}.`
            : "Forwarding is off.",
      });
    }),
  );

  server.registerTool(
    "rupochta_save_template",
    {
      title: "Create or update a reply template",
      description: `Store a reusable reply template. Pass 'id' to overwrite an existing one.

Args:
  - name (string): template name
  - subject (string): default subject (default '')
  - body (string): template body (default '')
  - id (number, optional): existing template id to overwrite

Returns:
  { "ok": boolean, "template": { "id": number, "name": string, "subject": string, "body": string } }

Examples:
  - Use when: "Save this as a template called 'Onboarding'"
  - Use when: updating existing wording -> pass the id from rupochta_get_settings
  - Don't use when: the text is a one-off (use rupochta_save_draft)

Error Handling:
  - HTTP 404: the supplied id does not belong to this mailbox`,
      inputSchema: TemplateInput,
      outputSchema: {
        ok: z.boolean(),
        template: z.object({
          id: z.number(),
          name: z.string(),
          subject: z.string(),
          body: z.string(),
        }),
      },
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: false,
        openWorldHint: true,
      },
    },
    guard(async (args: z.infer<typeof TemplateInput>) => {
      const payload = await client.request<{ ok?: boolean; template?: RawTemplate }>(
        "/api/settings/templates",
        {
          method: "POST",
          body: {
            name: args.name,
            subject: args.subject,
            body: args.body,
            ...(args.id ? { id: args.id } : {}),
          },
        },
      );
      const template = normalizeTemplate(payload.template ?? {});
      const structured = { ok: Boolean(payload.ok ?? true), template };
      return renderSingle({
        format: "markdown",
        structured,
        markdown: () =>
          `${args.id ? "Updated" : "Created"} template "${template.name}" (id ${template.id}).`,
      });
    }),
  );

  server.registerTool(
    "rupochta_delete_template",
    {
      title: "Delete a reply template",
      description: `Delete a stored reply template.

Args:
  - id (number): template id from rupochta_get_settings

Returns:
  { "ok": boolean, "deleted": number }

Examples:
  - Use when: "Remove the old onboarding template"
  - Don't use when: you only want to change its text (use rupochta_save_template with the same id)

Error Handling:
  - HTTP 404: no template with that id belongs to this mailbox`,
      inputSchema: TemplateIdInput,
      outputSchema: { ok: z.boolean(), deleted: z.number() },
      annotations: {
        readOnlyHint: false,
        destructiveHint: true,
        idempotentHint: true,
        openWorldHint: true,
      },
    },
    guard(async (args: z.infer<typeof TemplateIdInput>) => {
      await client.request(`/api/settings/templates/${args.id}`, { method: "DELETE" });
      const structured = { ok: true, deleted: args.id };
      return renderSingle({
        format: "markdown",
        structured,
        markdown: () => `Deleted template ${args.id}.`,
      });
    }),
  );
}
