/**
 * Server-side filter rules (Sieve).
 */

import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import type { RuPochtaClient } from "../services/client.js";
import { guard, inline, renderList, renderSingle } from "../services/format.js";
import { normalizeFilterRule, num } from "../services/normalize.js";
import { responseFormatField } from "../schemas/common.js";
import type { FilterRule, RawFilterRule } from "../types.js";

const conditionSchema = z
  .object({
    field: z
      .enum(["from", "to", "cc", "bcc", "subject", "body"])
      .describe("Message part to test"),
    op: z
      .enum(["contains", "is"])
      .default("contains")
      .describe("'contains' for a substring match, 'is' for an exact match"),
    value: z.string().min(1, "condition value must not be empty").max(500).describe("Text to match"),
  })
  .strict();

const actionSchema = z
  .object({
    type: z
      .enum(["move", "copy", "redirect", "mark_read", "flag", "discard"])
      .describe(
        "'move' files and stops, 'copy' files a duplicate, 'redirect' forwards to an address, " +
          "'mark_read' sets \\Seen, 'flag' stars, 'discard' drops the message",
      ),
    value: z
      .string()
      .max(255)
      .default("")
      .describe("Folder name for move/copy, address for redirect; ignored by the other types"),
  })
  .strict();

const RuleBody = {
  name: z.string().min(1, "name must not be empty").max(200).describe("Human-readable rule name"),
  enabled: z.boolean().default(true).describe("Whether the rule is active (default true)"),
  priority: z
    .number()
    .int()
    .min(0)
    .max(10000)
    .default(100)
    .describe("Evaluation order — lower runs first (default 100)"),
  conditions: z
    .array(conditionSchema)
    .min(1, "at least one condition is required")
    .max(20)
    .describe("Conditions to test. Multiple conditions are combined with AND"),
  actions: z
    .array(actionSchema)
    .min(1, "at least one action is required")
    .max(10)
    .describe("Actions to apply when the conditions match"),
};

const ListInput = z.object({ response_format: responseFormatField }).strict();
const CreateInput = z.object({ ...RuleBody, response_format: responseFormatField }).strict();
const UpdateInput = z
  .object({
    id: z.number().int().min(1).describe("Rule id from rupochta_list_filters"),
    ...RuleBody,
    response_format: responseFormatField,
  })
  .strict();
const DeleteInput = z
  .object({ id: z.number().int().min(1).describe("Rule id from rupochta_list_filters") })
  .strict();

const FilterShape = {
  id: z.number(),
  name: z.string(),
  enabled: z.boolean(),
  priority: z.number(),
  conditions: z.array(z.record(z.unknown())),
  actions: z.array(z.record(z.unknown())),
  updated_at: z.string(),
};

function describeRule(rule: FilterRule): string {
  const conditions = rule.conditions
    .map((cond) => `${String(cond.field ?? "?")} ${String(cond.op ?? "contains")} "${String(cond.value ?? "")}"`)
    .join(" AND ");
  const actions = rule.actions
    .map((act) => `${String(act.type ?? "?")}${act.value ? ` → ${String(act.value)}` : ""}`)
    .join(", ");
  return `${conditions} ⇒ ${actions}`;
}

export function registerFilterTools(server: McpServer, client: RuPochtaClient): void {
  server.registerTool(
    "rupochta_list_filters",
    {
      title: "List filter rules",
      description: `List the mailbox's server-side filter rules (Sieve), in evaluation order.

These rules run on the mail server as messages arrive, independently of any client. Lower 'priority' runs first, and a 'move' action stops later rules from firing.

Args:
  - response_format ('markdown' | 'json'): output format (default: 'markdown')

Returns:
  {
    "count": number,
    "rules": [
      {
        "id": number,
        "name": string,
        "enabled": boolean,
        "priority": number,            // lower runs first
        "conditions": [ { "field": string, "op": string, "value": string } ],
        "actions": [ { "type": string, "value": string } ],
        "updated_at": string
      }
    ]
  }

Examples:
  - Use when: "What filters do I have?"
  - Use when: before editing a rule, to get its exact id and current shape
  - Don't use when: looking for the auto-reply or forwarding settings (use rupochta_get_settings)

Error Handling:
  - An empty list means no rules are defined`,
      inputSchema: ListInput,
      outputSchema: {
        count: z.number(),
        truncated: z.boolean().optional(),
        truncation_message: z.string().optional(),
        rules: z.array(z.object(FilterShape)),
      },
      annotations: {
        readOnlyHint: true,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: true,
      },
    },
    guard(async (args: z.infer<typeof ListInput>) => {
      const payload = await client.request<{ rules?: RawFilterRule[] }>("/api/sieve/rules");
      const rules = (payload.rules ?? []).map(normalizeFilterRule);
      if (!rules.length) {
        return renderSingle({
          format: args.response_format,
          structured: { count: 0, rules: [] },
          markdown: () => "No filter rules are defined for this mailbox.",
        });
      }
      return renderList<FilterRule>({
        format: args.response_format,
        itemsKey: "rules",
        items: rules,
        meta: {},
        markdown: (rows) =>
          [
            `# Filter rules (${rows.length})`,
            "",
            ...rows.map(
              (rule) =>
                `- **${inline(rule.name, 60)}** (id ${rule.id}, priority ${rule.priority}, ${rule.enabled ? "enabled" : "disabled"})\n  ${inline(describeRule(rule), 300)}`,
            ),
          ].join("\n"),
      });
    }),
  );

  server.registerTool(
    "rupochta_create_filter",
    {
      title: "Create a filter rule",
      description: `Create a server-side filter rule and push the whole rule set to the mail server.

The destination folder of a 'move' or 'copy' action must already exist — RuPochta validates it and rejects the rule otherwise. Multiple conditions are combined with AND.

Args:
  - name (string): rule name
  - conditions (array): [{ field: 'from'|'to'|'cc'|'bcc'|'subject'|'body', op: 'contains'|'is', value: string }]
  - actions (array): [{ type: 'move'|'copy'|'redirect'|'mark_read'|'flag'|'discard', value: string }]
  - enabled (boolean): active on creation (default true)
  - priority (number): evaluation order, lower first (default 100)
  - response_format ('markdown' | 'json'): output format (default: 'markdown')

Returns:
  {
    "ok": boolean,
    "id": number,                  // id of the new rule
    "sync_ok": boolean,            // whether the rule set reached the ManageSieve server
    "sync_detail": string          // sync status text when the upload had trouble
  }

Examples:
  - Use when: "File everything from billing@acme.com into Invoices" -> conditions=[{field:'from',op:'contains',value:'billing@acme.com'}], actions=[{type:'move',value:'Invoices'}]
  - Use when: "Star anything with 'urgent' in the subject" -> actions=[{type:'flag',value:''}]
  - Don't use when: the folder does not exist yet — create it first with rupochta_create_folder

Error Handling:
  - HTTP 400: an action targets a folder that does not exist
  - sync_ok=false: the rule is stored but ManageSieve did not accept it; new mail keeps using the old script`,
      inputSchema: CreateInput,
      outputSchema: {
        ok: z.boolean(),
        id: z.number(),
        sync_ok: z.boolean(),
        sync_detail: z.string(),
      },
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: false,
        openWorldHint: true,
      },
    },
    guard(async (args: z.infer<typeof CreateInput>) => {
      const payload = await client.request<{
        ok?: boolean;
        id?: number;
        sync?: Record<string, unknown>;
      }>("/api/sieve/rules", {
        method: "POST",
        body: {
          name: args.name,
          enabled: args.enabled,
          priority: args.priority,
          conditions: args.conditions,
          actions: args.actions,
        },
      });
      const sync = payload.sync ?? {};
      const structured = {
        ok: Boolean(payload.ok ?? true),
        id: num(payload.id),
        sync_ok: Boolean(sync.ok ?? sync.synced ?? false),
        sync_detail: typeof sync.error === "string" ? sync.error : String(sync.status ?? ""),
      };
      return renderSingle({
        format: args.response_format,
        structured,
        markdown: () =>
          `Created filter "${args.name}" as rule ${structured.id}. ` +
          (structured.sync_ok
            ? "The rule set was uploaded to the mail server."
            : `ManageSieve sync did not confirm${structured.sync_detail ? `: ${structured.sync_detail}` : ""} — the rule is stored but may not be active yet.`),
      });
    }),
  );

  server.registerTool(
    "rupochta_update_filter",
    {
      title: "Update a filter rule",
      description: `Replace an existing filter rule in place.

Every field is overwritten, so send the complete rule — read it first with rupochta_list_filters and modify what you need. Use this to enable or disable a rule without deleting it.

Args:
  - id (number): rule id to replace
  - name, conditions, actions, enabled, priority: the full new rule (same shape as rupochta_create_filter)
  - response_format ('markdown' | 'json'): output format (default: 'markdown')

Returns:
  { "ok": boolean, "id": number, "sync_ok": boolean, "sync_detail": string }

Examples:
  - Use when: "Turn off the newsletter filter" -> read it, resend with enabled=false
  - Use when: changing the destination folder of an existing rule
  - Don't use when: you only want to reorder rules — change 'priority' instead of recreating them

Error Handling:
  - HTTP 404: no rule with that id belongs to this mailbox
  - HTTP 400: an action targets a folder that does not exist`,
      inputSchema: UpdateInput,
      outputSchema: {
        ok: z.boolean(),
        id: z.number(),
        sync_ok: z.boolean(),
        sync_detail: z.string(),
      },
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: true,
      },
    },
    guard(async (args: z.infer<typeof UpdateInput>) => {
      const payload = await client.request<{ ok?: boolean; id?: number; sync?: Record<string, unknown> }>(
        `/api/sieve/rules/${args.id}`,
        {
          method: "PUT",
          body: {
            name: args.name,
            enabled: args.enabled,
            priority: args.priority,
            conditions: args.conditions,
            actions: args.actions,
          },
        },
      );
      const sync = payload.sync ?? {};
      const structured = {
        ok: Boolean(payload.ok ?? true),
        id: num(payload.id, args.id),
        sync_ok: Boolean(sync.ok ?? sync.synced ?? false),
        sync_detail: typeof sync.error === "string" ? sync.error : String(sync.status ?? ""),
      };
      return renderSingle({
        format: args.response_format,
        structured,
        markdown: () => `Updated filter rule ${structured.id} ("${args.name}", ${args.enabled ? "enabled" : "disabled"}).`,
      });
    }),
  );

  server.registerTool(
    "rupochta_delete_filter",
    {
      title: "Delete a filter rule",
      description: `Delete a filter rule and re-upload the remaining rule set.

Mail already filed by this rule stays where it is; only future deliveries change.

Args:
  - id (number): rule id from rupochta_list_filters

Returns:
  { "ok": boolean, "deleted": number }

Examples:
  - Use when: "Remove the rule that archives newsletters"
  - Don't use when: you may want it back — rupochta_update_filter with enabled=false is reversible

Error Handling:
  - HTTP 404: no rule with that id belongs to this mailbox`,
      inputSchema: DeleteInput,
      outputSchema: { ok: z.boolean(), deleted: z.number() },
      annotations: {
        readOnlyHint: false,
        destructiveHint: true,
        idempotentHint: true,
        openWorldHint: true,
      },
    },
    guard(async (args: z.infer<typeof DeleteInput>) => {
      const payload = await client.request<{ ok?: boolean; deleted?: number }>(
        `/api/sieve/rules/${args.id}`,
        { method: "DELETE" },
      );
      const structured = { ok: Boolean(payload.ok ?? true), deleted: num(payload.deleted, args.id) };
      return renderSingle({
        format: "markdown",
        structured,
        markdown: () => `Deleted filter rule ${structured.deleted}.`,
      });
    }),
  );
}
