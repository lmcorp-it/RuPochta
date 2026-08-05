/**
 * Folder tools and shared-mailbox discovery.
 */

import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import type { RuPochtaClient } from "../services/client.js";
import { guard, inline, renderList, renderSingle } from "../services/format.js";
import { normalizeFolder, normalizeSharedMailbox } from "../services/normalize.js";
import { mailboxContextField, responseFormatField } from "../schemas/common.js";
import type { Folder, RawFolder, RawSharedMailbox, SharedMailbox } from "../types.js";

const ListFoldersInput = z
  .object({
    mailbox_context: mailboxContextField,
    response_format: responseFormatField,
  })
  .strict();

const CreateFolderInput = z
  .object({
    name: z
      .string()
      .min(1, "name must not be empty")
      .max(120, "name must not exceed 120 characters")
      .describe("Folder name. Nested folders use the server's hierarchy separator, e.g. 'Projects/2026'"),
    mailbox_context: mailboxContextField,
    response_format: responseFormatField,
  })
  .strict();

const DeleteFolderInput = z
  .object({
    name: z
      .string()
      .min(1, "name must not be empty")
      .max(255)
      .describe("Exact folder name to delete, as returned by rupochta_list_folders"),
    mailbox_context: mailboxContextField,
  })
  .strict();

const ListSharedInput = z
  .object({
    response_format: responseFormatField,
  })
  .strict();

const FolderShape = {
  name: z.string(),
  label: z.string(),
  unread: z.number(),
  total: z.number(),
};

function folderLines(folders: Folder[]): string {
  const rows = folders.map(
    (folder) =>
      `| ${inline(folder.name, 60)} | ${inline(folder.label, 40)} | ${folder.unread} | ${folder.total} |`,
  );
  return [
    "| Folder | Label | Unread | Total |",
    "| --- | --- | ---: | ---: |",
    ...rows,
  ].join("\n");
}

export function registerFolderTools(server: McpServer, client: RuPochtaClient): void {
  server.registerTool(
    "rupochta_list_folders",
    {
      title: "List mail folders",
      description: `List every IMAP folder in the mailbox with its unread and total message counts.

This is the natural first call in a mailbox session: it gives the exact folder names other tools expect and shows where unread mail is sitting. RuPochta labels well-known folders (INBOX, Sent, Drafts, Trash, Junk, Archive, Snoozed) and returns user-created folders alongside them.

Args:
  - mailbox_context (string): 'personal' (default) or 'shared:<id>' from rupochta_list_shared_mailboxes
  - response_format ('markdown' | 'json'): output format (default: 'markdown')

Returns:
  {
    "count": number,             // number of folders
    "total_unread": number,      // unread across all folders
    "folders": [
      {
        "name": string,          // exact IMAP name to pass to other tools, e.g. "INBOX"
        "label": string,         // human label, e.g. "Входящие"
        "unread": number,        // unseen messages
        "total": number          // messages in the folder
      }
    ]
  }

Examples:
  - Use when: "How many unread emails do I have?"
  - Use when: you need the exact spelling of a folder before moving messages
  - Don't use when: you want the messages themselves (use rupochta_list_messages)

Error Handling:
  - HTTP 502 means IMAP could not be reached; the mailbox password may have changed
  - Returns an empty list if the account genuinely has no folders`,
      inputSchema: ListFoldersInput,
      outputSchema: {
        count: z.number(),
        total_unread: z.number(),
        folders: z.array(z.object(FolderShape)),
      },
      annotations: {
        readOnlyHint: true,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: true,
      },
    },
    guard(async (args: z.infer<typeof ListFoldersInput>) => {
      const payload = await client.request<{ folders?: RawFolder[] }>("/api/folders", {
        mailboxContext: args.mailbox_context,
      });
      const folders = (payload.folders ?? []).map(normalizeFolder);
      const totalUnread = folders.reduce((sum, folder) => sum + folder.unread, 0);

      if (!folders.length) {
        return renderSingle({
          format: args.response_format,
          structured: { count: 0, total_unread: 0, folders: [] },
          markdown: () => "No folders found in this mailbox.",
        });
      }

      return renderList<Folder>({
        format: args.response_format,
        itemsKey: "folders",
        items: folders,
        meta: { total_unread: totalUnread },
        markdown: (items) =>
          [
            `# Folders (${items.length}, ${totalUnread} unread)`,
            "",
            folderLines(items),
          ].join("\n"),
      });
    }),
  );

  server.registerTool(
    "rupochta_create_folder",
    {
      title: "Create a mail folder",
      description: `Create a new IMAP folder in the mailbox.

Args:
  - name (string): folder name, 1-120 characters; nested paths use the server separator, e.g. 'Projects/2026'
  - mailbox_context (string): 'personal' (default) or 'shared:<id>'
  - response_format ('markdown' | 'json'): output format (default: 'markdown')

Returns:
  { "ok": boolean, "folder": string }   // folder is the normalised name IMAP accepted

Examples:
  - Use when: "Create a folder called Invoices"
  - Use when: preparing a destination before moving messages in bulk
  - Don't use when: the folder may already exist — check rupochta_list_folders first

Error Handling:
  - HTTP 400: the name is reserved or malformed; the server message says which
  - HTTP 502: IMAP refused the CREATE (often "folder already exists")`,
      inputSchema: CreateFolderInput,
      outputSchema: { ok: z.boolean(), folder: z.string() },
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: false,
        openWorldHint: true,
      },
    },
    guard(async (args: z.infer<typeof CreateFolderInput>) => {
      const payload = await client.request<{ ok?: boolean; folder?: string }>("/api/folders", {
        method: "POST",
        body: { name: args.name },
        mailboxContext: args.mailbox_context,
      });
      const structured = { ok: Boolean(payload.ok), folder: payload.folder ?? args.name };
      return renderSingle({
        format: args.response_format,
        structured,
        markdown: () => `Created folder **${structured.folder}**.`,
      });
    }),
  );

  server.registerTool(
    "rupochta_delete_folder",
    {
      title: "Delete a mail folder",
      description: `Delete an IMAP folder and every message left in it.

This is destructive and cannot be undone from this server: IMAP DELETE removes the mailbox itself. Move anything worth keeping first with rupochta_bulk_message_action. RuPochta refuses to delete a folder that an enabled filter rule still targets.

Args:
  - name (string): exact folder name from rupochta_list_folders
  - mailbox_context (string): 'personal' (default) or 'shared:<id>'

Returns:
  { "ok": boolean, "folder": string }

Examples:
  - Use when: "Delete the old Newsletters folder" and the user confirmed the contents can go
  - Don't use when: the folder still holds mail you have not moved
  - Don't use when: the target is a system folder (INBOX, Sent, Drafts, Trash) — RuPochta rejects those

Error Handling:
  - HTTP 400: the folder is protected, or a filter rule still files mail into it
  - HTTP 502: IMAP refused the DELETE`,
      inputSchema: DeleteFolderInput,
      outputSchema: { ok: z.boolean(), folder: z.string() },
      annotations: {
        readOnlyHint: false,
        destructiveHint: true,
        idempotentHint: true,
        openWorldHint: true,
      },
    },
    guard(async (args: z.infer<typeof DeleteFolderInput>) => {
      const payload = await client.request<{ ok?: boolean; folder?: string }>(
        `/api/folders/${encodeURIComponent(args.name)}`,
        { method: "DELETE", mailboxContext: args.mailbox_context },
      );
      const structured = { ok: Boolean(payload.ok ?? true), folder: payload.folder ?? args.name };
      return renderSingle({
        format: "markdown",
        structured,
        markdown: () => `Deleted folder **${structured.folder}** and its contents.`,
      });
    }),
  );

  server.registerTool(
    "rupochta_list_shared_mailboxes",
    {
      title: "List shared mailboxes",
      description: `List the shared mailboxes this account may open, with the context token needed to address them.

Every message and folder tool takes a 'mailbox_context' argument. Pass the 'mailbox_context' value from this list to work inside a shared mailbox instead of the personal one. Shared mailboxes can be read-only: 'can_send' says whether this account may send from the address.

Args:
  - response_format ('markdown' | 'json'): output format (default: 'markdown')

Returns:
  {
    "count": number,
    "mailboxes": [
      {
        "mailbox_id": number,        // numeric id
        "mailbox_context": string,   // pass this verbatim as mailbox_context, e.g. "shared:4"
        "email": string,             // shared address
        "name": string,              // display name
        "can_send": boolean          // may this account send from the address
      }
    ]
  }

Examples:
  - Use when: "Check the support inbox" -> take mailbox_context and call rupochta_list_messages with it
  - Use when: you need to know whether you may reply from a team address
  - Don't use when: working with the personal mailbox — that is the default

Error Handling:
  - Returns an empty list when the account is a member of no shared mailboxes`,
      inputSchema: ListSharedInput,
      outputSchema: {
        count: z.number(),
        mailboxes: z.array(
          z.object({
            mailbox_id: z.number(),
            mailbox_context: z.string(),
            email: z.string(),
            name: z.string(),
            can_send: z.boolean(),
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
    guard(async (args: z.infer<typeof ListSharedInput>) => {
      const payload = await client.request<{ mailboxes?: RawSharedMailbox[] }>(
        "/api/shared_mailboxes",
      );
      const mailboxes = (payload.mailboxes ?? []).map(normalizeSharedMailbox);
      if (!mailboxes.length) {
        return renderSingle({
          format: args.response_format,
          structured: { count: 0, mailboxes: [] },
          markdown: () => "This account has access to no shared mailboxes.",
        });
      }
      return renderList<SharedMailbox>({
        format: args.response_format,
        itemsKey: "mailboxes",
        items: mailboxes,
        meta: {},
        markdown: (items) =>
          [
            `# Shared mailboxes (${items.length})`,
            "",
            "| Context | Address | Name | Can send |",
            "| --- | --- | --- | --- |",
            ...items.map(
              (item) =>
                `| \`${item.mailbox_context}\` | ${inline(item.email, 60)} | ${inline(item.name, 40)} | ${item.can_send ? "yes" : "no"} |`,
            ),
          ].join("\n"),
      });
    }),
  );
}
