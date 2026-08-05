import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const source = fs.readFileSync(new URL("../static/app.js", import.meta.url), "utf8");
const start = source.indexOf("async function pollTelegramAuth(flow) {");
const end = source.indexOf("\n\nasync function startTelegramAuth(action)", start);
assert.notEqual(start, -1, "pollTelegramAuth must exist");
assert.notEqual(end, -1, "pollTelegramAuth boundary must exist");

const pollFunction = source.slice(start, end);
const elements = {
  "#login-error": { textContent: "" },
  "#telegram-auth-status": { textContent: "" },
};

const context = {
  Error,
  JSON,
  Promise,
  console,
  elements,
  globalThis: null,
};
context.globalThis = context;
const sandbox = vm.createContext(context);
vm.runInContext(`
  let telegramAuthFlow = null;
  let telegramAuthPollTimer = null;
  let telegramAuthPollInFlight = false;
  const TELEGRAM_POLL_MAX_TRANSIENT_FAILURES = 3;
  const elements = globalThis.elements;

  function $(selector) {
    return elements[selector];
  }

  function clearTelegramAuthFlow() {
    telegramAuthFlow = null;
  }

  function stopTelegramAuthPolling() {
    telegramAuthPollTimer = null;
  }

  function setTelegramAuthStatus(message) {
    elements["#telegram-auth-status"].textContent = message;
  }

  async function apiFetch(path) {
    if (path === "/auth/telegram/status") {
      const error = new Error("internal_error");
      error.status = 500;
      throw error;
    }
    throw new Error("unexpected request: " + path);
  }

  async function completeMailLogin() {}

  ${pollFunction}

  globalThis.setTelegramFlow = (flow) => {
    telegramAuthFlow = flow;
  };
  globalThis.runTelegramPoll = pollTelegramAuth;
  globalThis.telegramPollState = () => ({
    active: telegramAuthFlow !== null,
    polling: telegramAuthPollInFlight,
  });
`, sandbox, { filename: "telegram-polling-harness.js" });

const flow = { action: "login", token: "test-token" };
sandbox.setTelegramFlow(flow);

(async () => {
  await sandbox.runTelegramPoll(flow);
  assert.equal(flow.transientFailures, 1);
  assert.equal(sandbox.telegramPollState().active, true);

  await sandbox.runTelegramPoll(flow);
  assert.equal(flow.transientFailures, 2);
  assert.equal(sandbox.telegramPollState().active, true);

  await sandbox.runTelegramPoll(flow);
  assert.equal(sandbox.telegramPollState().active, false);
  assert.equal(sandbox.telegramPollState().polling, false);
  assert.match(elements["#login-error"].textContent, /Telegram/);
  assert.match(elements["#login-error"].textContent, /недоступен/);
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
