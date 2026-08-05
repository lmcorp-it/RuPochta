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
  };
}
