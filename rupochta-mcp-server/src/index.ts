#!/usr/bin/env node
/**
 * RuPochta MCP server.
 *
 * Exposes a RuPochta instance (https://github.com/lmcorp-it/RuPochta) — webmail
 * over plain IMAP/SMTP — as MCP tools: read and search mail, compose and send,
 * organise folders, manage filters, scheduled sends, snoozes and the calendar.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { loadConfig, ConfigError } from "./config.js";
import { SERVER_NAME, SERVER_VERSION } from "./constants.js";
import { RuPochtaClient } from "./services/client.js";
import { registerComposeTools } from "./tools/compose.js";
import { registerDirectoryTools } from "./tools/directory.js";
import { registerFilterTools } from "./tools/filters.js";
import { registerFolderTools } from "./tools/folders.js";
import { registerMessageTools } from "./tools/messages.js";
import { registerSchedulingTools } from "./tools/scheduling.js";
import { registerSessionTools } from "./tools/session.js";
import { registerSettingsTools } from "./tools/settings.js";

export function createServer(client: RuPochtaClient): McpServer {
  const server = new McpServer(
    { name: SERVER_NAME, version: SERVER_VERSION },
    {
      instructions:
        "Tools for a RuPochta mailbox (webmail over IMAP/SMTP). Start with rupochta_whoami or " +
        "rupochta_list_folders to orient yourself; message UIDs are per-folder, so always pass the " +
        "folder a UID came from. Reading a message marks it as read.",
    },
  );

  registerSessionTools(server, client);
  registerFolderTools(server, client);
  registerMessageTools(server, client);
  registerComposeTools(server, client);
  registerSchedulingTools(server, client);
  registerFilterTools(server, client);
  registerSettingsTools(server, client);
  registerDirectoryTools(server, client);
  return server;
}

async function main(): Promise<void> {
  const config = loadConfig();
  const client = new RuPochtaClient(config);
  const server = createServer(client);
  const transport = new StdioServerTransport();
  await server.connect(transport);
  // stdout carries the protocol; diagnostics must go to stderr.
  console.error(`${SERVER_NAME} ${SERVER_VERSION} ready (RuPochta at ${config.baseUrl})`);
}

main().catch((error: unknown) => {
  if (error instanceof ConfigError) {
    console.error(`Configuration error: ${error.message}`);
    process.exit(2);
  }
  console.error("Fatal error starting rupochta-mcp-server:", error);
  process.exit(1);
});
