/**
 * HTTP client for the RuPochta API.
 *
 * Three things about RuPochta shape this client:
 *  - Authentication is a session cookie (`wmSID`) minted by POST /api/auth/login
 *    against the real IMAP server, so there is no API key to pass around.
 *  - Every mutating request must carry an `Origin` matching the host RuPochta
 *    is served on, otherwise `get_session_or_401` answers 403.
 *  - A request can address either the personal mailbox or a shared one, chosen
 *    with the `X-Mailbox-Context` header.
 */

import type { ServerConfig } from "../config.js";
import { SESSION_COOKIE_NAME } from "../constants.js";
import {
  RuPochtaApiError,
  RuPochtaAuthRequiredError,
  RuPochtaTransportError,
} from "./errors.js";

export type HttpMethod = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";

export type QueryValue = string | number | boolean | undefined | null;

export interface RequestOptions {
  method?: HttpMethod;
  query?: Record<string, QueryValue>;
  /** JSON body. Ignored for GET/DELETE without a body. */
  body?: unknown;
  /** Multipart body; takes precedence over `body`. */
  form?: FormData;
  /** "personal" (default) or "shared:<id>". */
  mailboxContext?: string;
  /** Skip the automatic sign-in that normally precedes a request. */
  skipAuth?: boolean;
  timeoutMs?: number;
}

export interface SessionIdentity {
  email: string;
  displayName: string;
  mailLogin: string;
  fromAddress: string;
}

export interface BinaryPayload {
  bytes: Uint8Array;
  contentType: string;
  filename: string;
}

interface LoginResponse {
  ok?: boolean;
  email?: string;
  display_name?: string;
  mail_login?: string;
  from_address?: string;
}

const MUTATING_METHODS = new Set<HttpMethod>(["POST", "PUT", "PATCH", "DELETE"]);

function buildQuery(query: Record<string, QueryValue> | undefined): string {
  if (!query) {
    return "";
  }
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === "") {
      continue;
    }
    params.set(key, String(value));
  }
  const rendered = params.toString();
  return rendered ? `?${rendered}` : "";
}

function extractSessionToken(headers: Headers): string | undefined {
  const cookies =
    typeof headers.getSetCookie === "function"
      ? headers.getSetCookie()
      : [headers.get("set-cookie") ?? ""].filter(Boolean);
  for (const cookie of cookies) {
    const match = new RegExp(`(?:^|,\\s*)${SESSION_COOKIE_NAME}=([^;,]+)`).exec(cookie);
    if (match?.[1]) {
      return match[1];
    }
  }
  return undefined;
}

/** RuPochta reports failures as {"error": "..."} or FastAPI's {"detail": "..."}. */
function extractServerMessage(payload: unknown, fallback: string): string {
  if (payload && typeof payload === "object") {
    const record = payload as Record<string, unknown>;
    for (const key of ["error", "detail", "message"]) {
      const value = record[key];
      if (typeof value === "string" && value.trim()) {
        return value.trim();
      }
    }
    const hint = record.hint;
    if (typeof hint === "string" && hint.trim()) {
      return hint.trim();
    }
  }
  if (typeof payload === "string" && payload.trim()) {
    return payload.trim().slice(0, 500);
  }
  return fallback;
}

export class RuPochtaClient {
  private readonly config: ServerConfig;
  private sessionToken?: string;
  private identity?: SessionIdentity;
  /** Credentials good enough to mint a fresh session after an expiry. */
  private credentials?: { email: string; password: string };

  constructor(config: ServerConfig) {
    this.config = config;
    if (config.sessionCookie) {
      this.sessionToken = config.sessionCookie;
    }
    if (config.email && config.password) {
      this.credentials = { email: config.email, password: config.password };
    }
  }

  get baseUrl(): string {
    return this.config.baseUrl;
  }

  get currentIdentity(): SessionIdentity | undefined {
    return this.identity;
  }

  get hasSession(): boolean {
    return Boolean(this.sessionToken);
  }

  get canAutoLogin(): boolean {
    return Boolean(this.credentials);
  }

  /**
   * Sign in against IMAP and keep the resulting session cookie.
   * Called explicitly by `rupochta_login`, and implicitly on first use.
   */
  async login(email?: string, password?: string): Promise<SessionIdentity> {
    const useEmail = email ?? this.credentials?.email;
    const usePassword = password ?? this.credentials?.password;
    if (!useEmail || !usePassword) {
      throw new RuPochtaAuthRequiredError(
        "no RuPochta credentials available. Call rupochta_login with an email and password, " +
          "or start the server with RUPOCHTA_EMAIL and RUPOCHTA_PASSWORD set.",
      );
    }

    const { payload, headers } = await this.send<LoginResponse>("/api/auth/login", {
      method: "POST",
      body: { email: useEmail, password: usePassword, remember_me: true },
      skipAuth: true,
    });

    const token = extractSessionToken(headers);
    if (!token) {
      throw new RuPochtaApiError({
        status: 500,
        method: "POST",
        endpoint: "/api/auth/login",
        serverMessage: "sign-in succeeded but RuPochta returned no session cookie",
        hint: "The instance may sit behind a proxy that strips Set-Cookie. Check the reverse proxy configuration.",
      });
    }

    this.sessionToken = token;
    this.credentials = { email: useEmail, password: usePassword };
    this.identity = {
      email: payload.email ?? useEmail,
      displayName: payload.display_name ?? "",
      mailLogin: payload.mail_login ?? "",
      fromAddress: payload.from_address ?? payload.email ?? useEmail,
    };
    return this.identity;
  }

  async logout(): Promise<void> {
    if (!this.sessionToken) {
      return;
    }
    try {
      await this.request("/api/auth/logout", { method: "POST", skipAuth: true });
    } finally {
      this.sessionToken = undefined;
      this.identity = undefined;
    }
  }

  /** Drop cached credentials as well as the session (used by rupochta_logout). */
  forgetCredentials(): void {
    this.credentials = undefined;
  }

  /**
   * Perform an API call, signing in first when needed and once more if the
   * session turns out to be expired.
   */
  async request<T>(endpoint: string, options: RequestOptions = {}): Promise<T> {
    if (!options.skipAuth) {
      await this.ensureSession();
    }
    try {
      const { payload } = await this.send<T>(endpoint, options);
      return payload;
    } catch (error) {
      if (
        error instanceof RuPochtaApiError &&
        error.status === 401 &&
        !options.skipAuth &&
        this.credentials
      ) {
        // The session TTL is 8 hours; a long-lived MCP server will outlive it.
        this.sessionToken = undefined;
        await this.login();
        const { payload } = await this.send<T>(endpoint, options);
        return payload;
      }
      throw error;
    }
  }

  /** Fetch a binary endpoint (attachment download). */
  async requestBinary(endpoint: string, options: RequestOptions = {}): Promise<BinaryPayload> {
    if (!options.skipAuth) {
      await this.ensureSession();
    }
    const response = await this.dispatch(endpoint, options);
    if (!response.ok) {
      throw await this.toApiError(response, endpoint, options.method ?? "GET");
    }
    const buffer = new Uint8Array(await response.arrayBuffer());
    return {
      bytes: buffer,
      contentType: response.headers.get("content-type") ?? "application/octet-stream",
      filename: parseFilename(response.headers.get("content-disposition")),
    };
  }

  async ensureSession(): Promise<void> {
    if (this.sessionToken) {
      return;
    }
    if (!this.credentials) {
      throw new RuPochtaAuthRequiredError(
        "not signed in to RuPochta. Call rupochta_login with a mailbox address and password, " +
          "or start the server with RUPOCHTA_EMAIL and RUPOCHTA_PASSWORD set.",
      );
    }
    await this.login();
  }

  private async send<T>(
    endpoint: string,
    options: RequestOptions,
  ): Promise<{ payload: T; headers: Headers }> {
    const response = await this.dispatch(endpoint, options);
    const raw = await response.text();
    let parsed: unknown = undefined;
    if (raw) {
      try {
        parsed = JSON.parse(raw);
      } catch {
        parsed = raw;
      }
    }
    if (!response.ok) {
      throw this.buildApiError(response.status, endpoint, options.method ?? "GET", parsed);
    }
    return { payload: (parsed ?? {}) as T, headers: response.headers };
  }

  private async dispatch(endpoint: string, options: RequestOptions): Promise<Response> {
    const method = options.method ?? "GET";
    const url = `${this.config.baseUrl}${endpoint}${buildQuery(options.query)}`;
    const headers: Record<string, string> = { Accept: "application/json" };

    if (this.sessionToken) {
      headers.Cookie = `${SESSION_COOKIE_NAME}=${this.sessionToken}`;
    }
    if (MUTATING_METHODS.has(method)) {
      // Without this RuPochta answers 403: writes must look same-origin.
      headers.Origin = this.config.origin;
      headers.Referer = `${this.config.origin}/`;
    }
    if (options.mailboxContext && options.mailboxContext !== "personal") {
      headers["X-Mailbox-Context"] = options.mailboxContext;
    }

    let body: string | FormData | undefined;
    if (options.form) {
      body = options.form;
    } else if (options.body !== undefined) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(options.body);
    }

    try {
      return await fetch(url, {
        method,
        headers,
        body,
        redirect: "manual",
        signal: AbortSignal.timeout(options.timeoutMs ?? this.config.timeoutMs),
      });
    } catch (error) {
      const reason =
        error instanceof Error && error.name === "TimeoutError"
          ? `request timed out after ${options.timeoutMs ?? this.config.timeoutMs}ms`
          : error instanceof Error
            ? error.message
            : String(error);
      throw new RuPochtaTransportError(reason, endpoint, this.config.baseUrl);
    }
  }

  private async toApiError(
    response: Response,
    endpoint: string,
    method: HttpMethod,
  ): Promise<RuPochtaApiError> {
    let parsed: unknown;
    try {
      parsed = JSON.parse(await response.text());
    } catch {
      parsed = undefined;
    }
    return this.buildApiError(response.status, endpoint, method, parsed);
  }

  private buildApiError(
    status: number,
    endpoint: string,
    method: HttpMethod,
    parsed: unknown,
  ): RuPochtaApiError {
    const init: {
      status: number;
      method: HttpMethod;
      endpoint: string;
      serverMessage: string;
      hint?: string;
    } = {
      status,
      method,
      endpoint,
      serverMessage: extractServerMessage(parsed, `HTTP ${status}`),
    };
    if (status === 403 && MUTATING_METHODS.has(method)) {
      init.hint =
        `RuPochta accepts writes only from its own origin. This server sent Origin: ${this.config.origin}; ` +
        "set RUPOCHTA_BASE_URL to the URL the instance is actually served on.";
    }
    return new RuPochtaApiError(init);
  }
}

function parseFilename(disposition: string | null): string {
  if (!disposition) {
    return "attachment";
  }
  const utf8 = /filename\*=UTF-8''([^;]+)/i.exec(disposition);
  if (utf8?.[1]) {
    try {
      return decodeURIComponent(utf8[1]);
    } catch {
      /* fall through to the ASCII form */
    }
  }
  const ascii = /filename="?([^";]+)"?/i.exec(disposition);
  return ascii?.[1] ?? "attachment";
}
