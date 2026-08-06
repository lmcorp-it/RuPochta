/**
 * Environment-driven configuration.
 *
 * Credentials never live in code: the mailbox password comes from the
 * environment (or from an explicit `rupochta_login` call at runtime).
 */

import { DEFAULT_BASE_URL, DEFAULT_TIMEOUT_MS } from "./constants.js";

export interface ServerConfig {
  /** Base URL of the RuPochta instance, without a trailing slash. */
  baseUrl: string;
  /** Origin sent with mutating requests — RuPochta rejects cross-origin writes. */
  origin: string;
  /** Mailbox address used for automatic sign-in, if configured. */
  email?: string;
  /** Mailbox password used for automatic sign-in, if configured. */
  password?: string;
  /** Pre-existing session token (the value of the wmSID cookie), if configured. */
  sessionCookie?: string;
  /** Per-request timeout in milliseconds. */
  timeoutMs: number;
  /**
   * SEC-006/SEC-007: when true (the default) this server refuses to call any
   * mutating (write) RuPochta endpoint — send, delete, move, filter changes,
   * settings changes, etc. This bounds the blast radius of a prompt-injection
   * attack against whatever content the model is asked to read/summarize.
   * Set RUPOCHTA_READ_ONLY=0 (or "false") to explicitly opt in to write
   * tools.
   */
  readOnly: boolean;
}

export class ConfigError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ConfigError";
  }
}

function readTimeout(raw: string | undefined): number {
  if (!raw) {
    return DEFAULT_TIMEOUT_MS;
  }
  const parsed = Number.parseInt(raw, 10);
  if (!Number.isFinite(parsed) || parsed < 1000 || parsed > 300000) {
    throw new ConfigError(
      `RUPOCHTA_TIMEOUT_MS must be an integer between 1000 and 300000, got '${raw}'`,
    );
  }
  return parsed;
}

const LOOPBACK_HOSTNAMES = new Set(["localhost", "127.0.0.1", "::1", "[::1]"]);

/** True for a hostname that can only ever refer to the local machine. */
function isLoopbackHost(hostname: string): boolean {
  const normalized = hostname.toLowerCase();
  if (LOOPBACK_HOSTNAMES.has(normalized)) {
    return true;
  }
  // 127.0.0.0/8
  return /^127\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(normalized);
}

function readBoolEnv(raw: string | undefined, defaultValue: boolean): boolean {
  if (raw === undefined || raw.trim() === "") {
    return defaultValue;
  }
  const value = raw.trim().toLowerCase();
  if (["0", "false", "no", "off"].includes(value)) {
    return false;
  }
  if (["1", "true", "yes", "on"].includes(value)) {
    return true;
  }
  return defaultValue;
}

export function loadConfig(env: NodeJS.ProcessEnv = process.env): ServerConfig {
  const rawBase = (env.RUPOCHTA_BASE_URL || DEFAULT_BASE_URL).trim();
  let parsed: URL;
  try {
    parsed = new URL(rawBase);
  } catch {
    throw new ConfigError(
      `RUPOCHTA_BASE_URL is not a valid URL: '${rawBase}'. Example: http://127.0.0.1:18400`,
    );
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    throw new ConfigError(
      `RUPOCHTA_BASE_URL must be http:// or https://, got '${parsed.protocol}'`,
    );
  }

  // SEC-006: mailbox credentials and session cookies travel with every
  // request. Plain HTTP is only acceptable for a genuinely local instance —
  // anything else must use TLS or an operator must explicitly acknowledge
  // the risk.
  if (
    parsed.protocol === "http:" &&
    !isLoopbackHost(parsed.hostname) &&
    !readBoolEnv(env.RUPOCHTA_ALLOW_INSECURE_HTTP, false)
  ) {
    throw new ConfigError(
      `RUPOCHTA_BASE_URL '${rawBase}' uses plain HTTP against a non-loopback host. ` +
        "Mailbox credentials and session cookies would travel in the clear. " +
        "Use an https:// URL, or set RUPOCHTA_ALLOW_INSECURE_HTTP=1 to explicitly accept the risk.",
    );
  }

  const email = (env.RUPOCHTA_EMAIL || "").trim() || undefined;
  const password = env.RUPOCHTA_PASSWORD || undefined;
  if (email && !password) {
    throw new ConfigError(
      "RUPOCHTA_EMAIL is set but RUPOCHTA_PASSWORD is not — both are required for automatic sign-in.",
    );
  }

  return {
    baseUrl: `${parsed.origin}${parsed.pathname.replace(/\/+$/, "")}`,
    origin: parsed.origin,
    email,
    password,
    sessionCookie: (env.RUPOCHTA_SESSION_COOKIE || "").trim() || undefined,
    timeoutMs: readTimeout(env.RUPOCHTA_TIMEOUT_MS),
    readOnly: readBoolEnv(env.RUPOCHTA_READ_ONLY, true),
  };
}
