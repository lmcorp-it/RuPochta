/**
 * Error types and agent-facing error text.
 *
 * Every message here is written for the model that will read it: it says what
 * failed, why, and what to try next.
 */

export interface ApiErrorInit {
  status: number;
  method: string;
  endpoint: string;
  serverMessage: string;
  hint?: string;
}

/** A non-2xx answer from RuPochta. */
export class RuPochtaApiError extends Error {
  readonly status: number;
  readonly method: string;
  readonly endpoint: string;
  readonly serverMessage: string;
  readonly hint?: string;

  constructor(init: ApiErrorInit) {
    super(init.serverMessage || `HTTP ${init.status} from ${init.endpoint}`);
    this.name = "RuPochtaApiError";
    this.status = init.status;
    this.method = init.method;
    this.endpoint = init.endpoint;
    this.serverMessage = init.serverMessage;
    if (init.hint !== undefined) {
      this.hint = init.hint;
    }
  }
}

/** The RuPochta instance could not be reached at all. */
export class RuPochtaTransportError extends Error {
  readonly endpoint: string;
  readonly baseUrl: string;

  constructor(message: string, endpoint: string, baseUrl: string) {
    super(message);
    this.name = "RuPochtaTransportError";
    this.endpoint = endpoint;
    this.baseUrl = baseUrl;
  }
}

/** No usable session and no credentials to make one. */
export class RuPochtaAuthRequiredError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "RuPochtaAuthRequiredError";
  }
}

const STATUS_GUIDANCE: Record<number, string> = {
  400: "The request was rejected as malformed. Check folder names, UIDs and date formats, then retry.",
  401: "The RuPochta session is missing or expired. Call rupochta_login with a mailbox address and password, or set RUPOCHTA_EMAIL and RUPOCHTA_PASSWORD in the server environment.",
  403: "RuPochta refused this write. It only accepts writes whose Origin matches the host it is served on, so RUPOCHTA_BASE_URL must be the same URL users open in a browser (for example https://mail.example.com, not http://127.0.0.1:18400 behind a proxy).",
  404: "Nothing matched. Re-check the identifier: message UIDs are per-folder, so a UID from INBOX is meaningless in Archive. Use rupochta_list_messages or rupochta_search_messages to get a fresh UID.",
  409: "This action needs a RuPochta ID (SSO) session; a plain mailbox password login cannot perform it.",
  413: "The payload was too large. Send fewer or smaller attachments.",
  429: "Too many failed sign-in attempts from this address. RuPochta locks the bucket for about five minutes — wait, then retry.",
  500: "RuPochta hit an internal error. Retry once; if it persists the server log has the detail.",
  502: "RuPochta could reach neither IMAP nor SMTP successfully. This is an upstream mail path failure, not a bad request — check /ready on the instance.",
  503: "The mail path is temporarily unavailable, or password login is disabled in favour of SSO. Retry later.",
};

/**
 * Render any thrown value as the text an agent should see.
 *
 * Keeps the server-supplied message (RuPochta answers in Russian) and adds an
 * English next step, so the model always has something actionable.
 */
export function describeError(error: unknown): string {
  if (error instanceof RuPochtaApiError) {
    const parts = [`Error: RuPochta returned HTTP ${error.status} for ${error.method} ${error.endpoint}.`];
    if (error.serverMessage) {
      parts.push(`Server said: ${error.serverMessage}`);
    }
    const guidance = error.hint ?? STATUS_GUIDANCE[error.status];
    if (guidance) {
      parts.push(guidance);
    }
    return parts.join(" ");
  }
  if (error instanceof RuPochtaTransportError) {
    return (
      `Error: could not reach RuPochta at ${error.baseUrl} (${error.message}). ` +
      "Check that the instance is running and that RUPOCHTA_BASE_URL points at it — " +
      "GET /health should answer on that URL."
    );
  }
  if (error instanceof RuPochtaAuthRequiredError) {
    return `Error: ${error.message}`;
  }
  if (error instanceof Error) {
    return `Error: ${error.message}`;
  }
  return `Error: ${String(error)}`;
}
