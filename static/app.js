/* ============================================================
   RuPochta Webmail - Frontend SPA
   ============================================================ */
(() => {
"use strict";

const API = "/api";
const BRAND_TEMPLATE = Object.freeze({
  id: "rupochta-mail-default",
  backgroundUrl: "https://mail.example.com/static/brand/mail-workspace-background.webp",
  logoUrl: "https://mail.example.com/static/brand/rupochta-wordmark.svg",
  backgroundColor: "#e9eef5",
  surfaceColor: "rgba(255,255,255,.9)",
  borderColor: "#d6e0ee",
  textColor: "#101b33",
  brandColor: "#0b1d45",
  logoWidth: 148,
});

const LM_WORKSPACES = {
  focus: { label: "Фокус", personalOnly: true },
  mail: { label: "Почта" },
  calendar: { label: "Календарь", personalOnly: true, overlay: "calendar-modal" },
  directory: { label: "Контакты", personalOnly: true, overlay: "directory-modal" },
};

const SIDEBAR_FOLDER_ALIASES = {
  outbox: ["Outbox", "Исходящие"],
  sent: ["Sent", "INBOX.Sent", "Sent Items", "INBOX.Sent Items", "Отправленные"],
  archive: ["Archive", "Archives", "INBOX.Archive", "INBOX.Archives", "Архив"],
  all: ["All", "All Mail", "INBOX.All", "INBOX.All Mail", "Все", "Все письма"],
  spam: ["Junk", "Spam", "INBOX.Junk", "INBOX.Spam", "Спам"],
};

const state = {
  me: null,
  sharedMailboxes: [],
  activeMailbox: {
    key: "personal",
    is_shared: false,
    mailbox_id: null,
    email: "",
    from_address: "",
    display_name: "",
    can_send: true,
  },
  mailboxEpoch: 0,
  folders: [],
  currentFolder: "INBOX",
  messages: [],
  totalMessages: 0,
  page: 1,
  perPage: 20,
  threadMode: true,
  selectedUid: null,
  selectedUids: new Set(),
  currentMessage: null,
  currentThreadUids: [],
  currentThreadMessages: [],
  refreshTimer: null,
  searchQuery: "",
  searchTimer: null,
  filters: {
    unread: false,
    flagged: false,
    hasAttachments: false,
  },
  settings: {
    appearance: "workspace",
    theme: "light",
    signature: "",
    templates: [],
    forwarding: {
      enabled: false,
      address: "",
      keep_copy: true,
      active: false,
      sync_message: "",
    },
    autoreply: {
      enabled: false,
      subject: "",
      body: "",
      start_date: null,
      end_date: null,
      repeat_days: 1,
      active: false,
      effective: false,
      sync_message: "",
    },
    notifications: {
      push_enabled: false,
      push_subscriptions: 0,
      telegram_linked: false,
      telegram_username: "",
      telegram_enabled: false,
      telegram_error: "",
    },
    rules: [],
  },
  runtime: {
    loaded: false,
    readyOk: true,
    mode: "primary",
    mailAdminAvailable: true,
    degraded: false,
    checks: null,
  },
  ui: {
    settingsTab: "identity",
    settingsLoading: false,
    forwardingLoaded: false,
    autoreplyLoaded: false,
    profileSettingsLoaded: false,
    accountProfile: null,
    accountAvatarDataUrl: "",
    accountHasPassword: false,
    notificationsSettingsLoaded: false,
    foldersSettingsLoaded: false,
    rulesSettingsLoaded: false,
    modalReturnFocus: null,
    priorityMode: true,
    priorityFocus: "all",
    sidebarFolderKey: "INBOX",
    triageLoadedAt: 0,
    workspace: "focus",
    previousWorkspace: "focus",
    mailListScrollTop: 0,
    readerOpening: 0,
    focusRequestId: 0,
    workspaceRequestId: 0,
    focus: {
      loading: false,
      loadedAt: 0,
      messages: [],
      invitations: [],
      outbox: [],
      returned: [],
      events: [],
      errors: {},
    },
  },
  undo: {
    timer: null,
    payload: null,
  },
  compose: {
    mode: "new",
    inReplyTo: null,
    draftUid: null,
    generation: 0,
    attachments: [],
    autosaveTimer: null,
    recoveryDraft: null,
    bccVisible: false,
    suspended: false,
  },
  requestSeq: {
    folders: 0,
    messages: 0,
    openMessage: 0,
  },
};

const messageDetailCache = new Map();
const messageDetailInflight = new Map();
const MESSAGE_DETAIL_CACHE_LIMIT = 250;
const THEME_STORAGE_KEY = "rupochta-mail-theme";
const APPEARANCE_STORAGE_KEY = "rupochta-mail-appearance";
const THEME_META_LIGHT = "#f4f8ff";
const THEME_META_DARK = "#0b1d45";
let mailboxAccessRefreshPending = false;
let triageClassifyPromise = null;
let telegramAuthGeneration = 0;
const TELEGRAM_POLL_MAX_TRANSIENT_FAILURES = 3;

function $(sel) { return document.querySelector(sel); }
function $$(sel) { return document.querySelectorAll(sel); }
function isMobileLayout() { return window.matchMedia("(max-width: 860px)").matches; }

/* ------------------------------------------------------------------
   Resizable splitter between the message list and the message content.
   The list-pane width is stored as the --mail-list-width CSS variable
   on .mail-workspace and persisted in localStorage so the user's
   preferred size survives reloads. Disabled on mobile (<= 860px),
   where the panes stack vertically.
   ------------------------------------------------------------------ */
const MAIL_LIST_WIDTH_KEY = "rupochta-mail-list-width";
const MAIL_LIST_WIDTH_DEFAULT = 420;
const MAIL_LIST_WIDTH_MIN = 280;
const MAIL_LIST_WIDTH_MAX = 900;

function readStoredListWidth() {
  const raw = Number(localStorage.getItem(MAIL_LIST_WIDTH_KEY));
  if (!Number.isFinite(raw) || raw < MAIL_LIST_WIDTH_MIN || raw > MAIL_LIST_WIDTH_MAX) {
    return null;
  }
  return raw;
}

function applyListWidth(px) {
  const workspace = $("#mail-workspace");
  if (!workspace) return;
  workspace.style.setProperty("--mail-list-width", `${Math.round(px)}px`);
}

function initPaneSplitter() {
  const splitter = $("#pane-splitter");
  const workspace = $("#mail-workspace");
  if (!splitter || !workspace) return;

  const stored = readStoredListWidth();
  if (stored) applyListWidth(stored);

  let dragging = false;
  let startX = 0;
  let startWidth = 0;

  const beginDrag = (clientX) => {
    if (isMobileLayout()) return;
    dragging = true;
    startX = clientX;
    startWidth = $("#message-list-pane").getBoundingClientRect().width;
    splitter.classList.add("is-dragging");
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
  };

  const moveDrag = (clientX) => {
    if (!dragging) return;
    const delta = clientX - startX;
    const next = Math.min(
      MAIL_LIST_WIDTH_MAX,
      Math.max(MAIL_LIST_WIDTH_MIN, startWidth + delta),
    );
    applyListWidth(next);
  };

  const endDrag = () => {
    if (!dragging) return;
    dragging = false;
    splitter.classList.remove("is-dragging");
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
    try {
      localStorage.setItem(
        MAIL_LIST_WIDTH_KEY,
        String($("#message-list-pane").getBoundingClientRect().width | 0),
      );
    } catch (_) {}
  };

  splitter.addEventListener("mousedown", (e) => {
    e.preventDefault();
    beginDrag(e.clientX);
  });
  document.addEventListener("mousemove", (e) => moveDrag(e.clientX));
  document.addEventListener("mouseup", endDrag);

  // Touch support.
  splitter.addEventListener("touchstart", (e) => {
    if (isMobileLayout()) return;
    const t = e.touches[0];
    if (!t) return;
    e.preventDefault();
    beginDrag(t.clientX);
  }, { passive: false });
  document.addEventListener("touchmove", (e) => {
    if (!dragging) return;
    const t = e.touches[0];
    if (t) moveDrag(t.clientX);
  }, { passive: true });
  document.addEventListener("touchend", endDrag);
  document.addEventListener("touchcancel", endDrag);

  // Keyboard: arrows nudge width, Home/Enter resets.
  splitter.addEventListener("keydown", (e) => {
    if (isMobileLayout()) return;
    const current = $("#message-list-pane").getBoundingClientRect().width | 0;
    if (e.key === "ArrowLeft") {
      e.preventDefault();
      applyListWidth(Math.max(MAIL_LIST_WIDTH_MIN, current - 16));
    } else if (e.key === "ArrowRight") {
      e.preventDefault();
      applyListWidth(Math.min(MAIL_LIST_WIDTH_MAX, current + 16));
    } else if (e.key === "Home" || e.key === "Enter") {
      e.preventDefault();
      applyListWidth(MAIL_LIST_WIDTH_DEFAULT);
      try { localStorage.removeItem(MAIL_LIST_WIDTH_KEY); } catch (_) {}
    }
  });

  // Double-click resets to default.
  splitter.addEventListener("dblclick", () => {
    applyListWidth(MAIL_LIST_WIDTH_DEFAULT);
    try { localStorage.removeItem(MAIL_LIST_WIDTH_KEY); } catch (_) {}
  });

  // On resize, drop the override on mobile so the responsive default applies.
  const handleResize = () => {
    if (isMobileLayout()) {
      workspace.style.removeProperty("--mail-list-width");
    } else {
      const s = readStoredListWidth();
      if (s) applyListWidth(s);
    }
  };
  window.addEventListener("resize", handleResize);
  handleResize();
}

function normalizeTheme(theme) {
  return theme === "light" ? "light" : "dark";
}

function updateThemeMeta(theme) {
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", theme === "light" ? THEME_META_LIGHT : THEME_META_DARK);
}

function applyTheme(theme, opts = {}) {
  const nextTheme = normalizeTheme(theme);
  state.settings.theme = nextTheme;
  document.body.dataset.theme = nextTheme;
  updateThemeMeta(nextTheme);
  $$("[data-theme-option]").forEach((item) => {
    item.classList.toggle("active", item.dataset.themeOption === nextTheme);
  });
  if (!opts.skipPersist) {
    try {
      localStorage.setItem(THEME_STORAGE_KEY, nextTheme);
    } catch (_) {}
  }
}

function loadStoredTheme() {
  try {
    return normalizeTheme(localStorage.getItem(THEME_STORAGE_KEY) || "light");
  } catch (_) {
    return "light";
  }
}

function setTheme(theme) {
  applyTheme(theme);
  const status = $("#theme-status");
  if (status) status.textContent = theme === "light" ? "Светлая тема включена" : "Тёмная тема включена";
}

function normalizeAppearance(appearance) {
  return appearance === "classic" ? "classic" : "workspace";
}

function applyAppearance(appearance, opts = {}) {
  const nextAppearance = normalizeAppearance(appearance);
  state.settings.appearance = nextAppearance;
  document.body.dataset.appearance = nextAppearance;
  $$("[data-appearance-option]").forEach((item) => {
    item.classList.toggle("active", item.dataset.appearanceOption === nextAppearance);
  });
  if (!opts.skipPersist) {
    try {
      localStorage.setItem(APPEARANCE_STORAGE_KEY, nextAppearance);
    } catch (_) {}
  }
}

function loadStoredAppearance() {
  try {
    return normalizeAppearance(localStorage.getItem(APPEARANCE_STORAGE_KEY) || "workspace");
  } catch (_) {
    return "workspace";
  }
}

function setAppearance(appearance) {
  applyAppearance(appearance);
  const status = $("#appearance-status");
  if (status) {
    status.textContent = appearance === "classic"
      ? "Классическое оформление включено"
      : "Рабочая очередь включена";
  }
}

function renderRuntimeBanner() {
  const banner = $("#runtime-banner");
  if (!banner) return;
  const runtime = state.runtime || {};
  const checks = runtime.checks || {};
  const smtpClientDegraded = !!(runtime.mode === "backup" && checks.smtp_client && checks.smtp_client.ok === false);
  const shouldShow = !!runtime.loaded && (runtime.mode === "backup" || runtime.degraded);
  if (!shouldShow) {
    banner.classList.add("hidden");
    banner.classList.remove("is-backup", "is-degraded");
    return;
  }
  const badge = $("#runtime-banner-badge");
  const title = $("#runtime-banner-title");
  const text = $("#runtime-banner-text");
  const meta = $("#runtime-banner-meta");
  banner.classList.remove("hidden");
  banner.classList.toggle("is-backup", runtime.mode === "backup");
  banner.classList.toggle("is-degraded", !!runtime.degraded);
  if (badge) badge.textContent = runtime.degraded ? "Degraded" : "Backup";
  if (title) {
    title.textContent = runtime.degraded
      ? "Резервный контур активен, primary path деградирован"
      : "Резервный почтовый контур активен";
  }
  if (text) {
    if (smtpClientDegraded) {
      text.textContent = "Клиентский SMTP path сейчас деградирован. Отправка из Thunderbird и webmail может задерживаться или требовать повтора.";
    } else {
      text.textContent = runtime.degraded
        ? "Публичный IMAP сейчас нестабилен. Почта работает через recovery path и SOC control-plane."
        : "Часть почтовых операций сейчас обслуживается через резервный SOC recovery path.";
    }
  }
  if (meta) {
    const bits = [];
    if (checks.smtp) bits.push(`SMTP: ${checks.smtp.ok ? "ok" : "fail"}`);
    if (checks.smtp_client && runtime.mode === "backup") bits.push(`SMTP clients: ${checks.smtp_client.ok ? "ok" : "degraded"}`);
    if (checks.imap_public) bits.push(`IMAP public: ${checks.imap_public.ok ? "ok" : "degraded"}`);
    if (checks.mail_control && runtime.mode === "backup") bits.push(`SOC control-plane: ${checks.mail_control.ok ? "ok" : "fail"}`);
    if (runtime.mailAdminAvailable === false) bits.push("local mail-admin: off");
    meta.textContent = bits.join(" · ");
  }
}

function renderLoginRuntimeNotice() {
  const hint = $("#login-hint");
  if (!hint) return;
  const runtime = state.runtime || {};
  const checks = runtime.checks || {};
  if (!runtime.loaded) return;
  if (runtime.readyOk === false) {
    const issues = [];
    if (checks.imap_public && checks.imap_public.ok === false) issues.push("IMAP");
    if (runtime.mode === "backup" && checks.mail_control && checks.mail_control.ok === false) issues.push("SOC control-plane");
    if (runtime.mode === "backup" && checks.smtp_client && checks.smtp_client.ok === false) issues.push("SMTP client path");
    const scope = runtime.mode === "backup" ? "Резервный почтовый контур" : "Почтовый контур";
    hint.textContent = issues.length
      ? `${scope} сейчас недоступен для входа: ${issues.join(" + ")}. Это не похоже на неверный пароль.`
      : `${scope} сейчас недоступен для входа.`;
    return;
  }
  if (runtime.mode === "backup") {
    hint.textContent = runtime.degraded
      ? "Сейчас активен резервный контур в degraded-режиме."
      : "Сейчас активен резервный почтовый контур.";
    return;
  }
  // Nothing to announce in the healthy-primary case. Don't clobber a
  // login-failure hint that handleLogin may have just set while this
  // (page-load) runtime fetch was still in flight.
  const loginError = $("#login-error");
  if (!(loginError && loginError.textContent)) {
    hint.textContent = "";
  }
}

function renderComposeRuntimeWarning() {
  const warning = $("#compose-runtime-warning");
  if (!warning) return;
  const runtime = state.runtime || {};
  const checks = runtime.checks || {};
  const smtpClientDegraded = !!(runtime.mode === "backup" && checks.smtp_client && checks.smtp_client.ok === false);
  if (!smtpClientDegraded) {
    warning.classList.add("hidden");
    warning.textContent = "";
    return;
  }
  warning.textContent = "Внимание: путь отправки для клиентов сейчас деградирован. Письмо может уйти с задержкой или попасть в Outbox для повторной отправки.";
  warning.classList.remove("hidden");
}

function renderScheduledBadge(count) {
  const btn = document.getElementById("btn-scheduled");
  if (!btn) return;
  btn.dataset.badge = String(Math.max(0, Number(count || 0)));
  btn.classList.toggle("has-badge", Number(count || 0) > 0);
}

async function loadScheduledBadge() {
  try {
    const res = await apiFetch("/scheduled");
    const items = Array.isArray(res.items) ? res.items : [];
    const failedCount = items.filter((item) => item && item.status === "failed").length;
    renderScheduledBadge(failedCount);
  } catch (_) {
    renderScheduledBadge(0);
  }
}

async function loadRuntimeStatus() {
  try {
    const [healthRes, readyRes] = await Promise.all([
      fetch("/health", { credentials: "same-origin" }),
      fetch("/ready", { credentials: "same-origin" }),
    ]);
    const health = await healthRes.json().catch(() => ({}));
    const ready = await readyRes.json().catch(() => ({}));
    state.runtime = {
      loaded: true,
      readyOk: ready.ok !== false,
      mode: ready.mode || health.mode || "primary",
      mailAdminAvailable: typeof ready.mail_admin_available === "boolean"
        ? ready.mail_admin_available
        : !!health.mail_admin_available,
      degraded: !!ready.degraded,
      checks: ready.checks || null,
    };
  } catch (_) {
    state.runtime = {
      loaded: false,
      readyOk: true,
      mode: "primary",
      mailAdminAvailable: true,
      degraded: false,
      checks: null,
    };
  }
  renderRuntimeBanner();
  renderLoginRuntimeNotice();
  renderComposeRuntimeWarning();
  if (state.me) {
    loadScheduledBadge().catch(() => {});
  }
}

function captureMailboxContext() {
  const key = activeMailboxKey();
  return {
    key,
    isShared: key.startsWith("shared:"),
    epoch: state.mailboxEpoch,
  };
}

function isMailboxContextCurrent(contextOrKey, mailboxEpoch) {
  const context = typeof contextOrKey === "string"
    ? { key: contextOrKey, epoch: mailboxEpoch }
    : contextOrKey;
  return Boolean(
    context
    && context.key === activeMailboxKey()
    && context.epoch === state.mailboxEpoch
  );
}

function invalidateMailboxRequests() {
  state.mailboxEpoch += 1;
  state.requestSeq.folders += 1;
  state.requestSeq.messages += 1;
  state.requestSeq.openMessage += 1;
}

function clearUndo() {
  if (state.undo.timer) clearTimeout(state.undo.timer);
  state.undo.timer = null;
  state.undo.payload = null;
}

function invalidateComposeSession() {
  clearTimeout(state.compose.autosaveTimer);
  state.compose.autosaveTimer = null;
  state.compose.generation += 1;
  state.compose.draftUid = null;
  state.compose.inReplyTo = null;
  state.compose.attachments = [];
  state.compose.recoveryDraft = null;
  state.compose.suspended = false;
}

function messageDetailCacheKey(folder, uid, mailboxKey = activeMailboxKey()) {
  return `${mailboxKey}::${folder || ""}::${uid || ""}`;
}

function putMessageDetailCache(folder, message, mailboxKey = activeMailboxKey()) {
  if (!message || !message.uid) return;
  const key = messageDetailCacheKey(folder, message.uid, mailboxKey);
  if (messageDetailCache.has(key)) messageDetailCache.delete(key);
  messageDetailCache.set(key, message);
  while (messageDetailCache.size > MESSAGE_DETAIL_CACHE_LIMIT) {
    const oldest = messageDetailCache.keys().next().value;
    if (!oldest) break;
    messageDetailCache.delete(oldest);
  }
}

function getMessageDetailCache(folder, uid, mailboxKey = activeMailboxKey()) {
  const key = messageDetailCacheKey(folder, uid, mailboxKey);
  const cached = messageDetailCache.get(key) || null;
  if (cached) {
    messageDetailCache.delete(key);
    messageDetailCache.set(key, cached);
  }
  return cached;
}

function clearMessageDetailCache() {
  messageDetailCache.clear();
  messageDetailInflight.clear();
}

async function fetchMessageDetail(uid, folder = state.currentFolder, opts = {}) {
  const mailboxContext = opts.mailboxContext || captureMailboxContext();
  const key = messageDetailCacheKey(folder, uid, mailboxContext.key);
  if (!opts.force) {
    const cached = getMessageDetailCache(folder, uid, mailboxContext.key);
    if (cached) return cached;
    if (messageDetailInflight.has(key)) return messageDetailInflight.get(key);
  }
  const pending = apiFetch(
    `/messages/${encodeURIComponent(uid)}?folder=${encodeURIComponent(folder)}`,
    { mailboxContext },
  )
    .then((message) => {
      putMessageDetailCache(folder, message, mailboxContext.key);
      return message;
    })
    .finally(() => {
      messageDetailInflight.delete(key);
    });
  messageDetailInflight.set(key, pending);
  return pending;
}

async function refreshMailboxChrome(options = {}) {
  const tasks = [];
  if (options.messages !== false) tasks.push(loadMessages());
  if (options.folders !== false) tasks.push(loadFolders());
  await Promise.all(tasks);
}

function setCurrentFolderLabel(label) {
  const value = label || state.currentFolder || "Входящие";
  if ($("#current-folder-title")) $("#current-folder-title").textContent = value;
  if ($("#topbar-folder-name")) $("#topbar-folder-name").textContent = value;
}

function getFolderByName(name) {
  return state.folders.find((folder) => folder.name === name) || null;
}

function getArchiveFolderName() {
  const direct = ["Archive", "Архив"].find((name) => getFolderByName(name));
  if (direct) return direct;
  const byLabel = state.folders.find((folder) => /архив|archive/i.test(`${folder.name} ${folder.label || ""}`));
  return byLabel ? byLabel.name : null;
}

function getSelfEmails() {
  return new Set([
    (state.activeMailbox && state.activeMailbox.from_address || "").toLowerCase(),
    (state.activeMailbox && state.activeMailbox.email || "").toLowerCase(),
  ].filter(Boolean));
}

function getMessageDateValue(message) {
  const stamp = new Date((message && message.date) || 0).getTime();
  return Number.isFinite(stamp) ? stamp : 0;
}

function hasPriorityKeywords(message) {
  const hay = `${message && message.subject || ""} ${message && message.snippet || ""}`;
  return /(urgent|asap|action required|approval|approve|deadline|reply needed|важно|срочно|соглас|подтверд|требует ответа)/i.test(hay);
}

function isMessageDirectToMe(message) {
  const self = getSelfEmails();
  if (!self.size || !message) return false;
  return parseAddressesFromHeader(message.to).some((email) => self.has(String(email || "").toLowerCase()));
}

function buildPriorityMetaForMessage(message) {
  const unread = !!(message && message.unread);
  const flagged = !!(message && message.flagged);
  const hasAttachments = !!(message && message.has_attachments);
  const direct = isMessageDirectToMe(message);
  const urgent = hasPriorityKeywords(message);
  const ageMs = Date.now() - getMessageDateValue(message);
  const ageHours = ageMs > 0 ? ageMs / 3600000 : 0;
  let score = 0;
  if (direct) score += 6;
  if (unread) score += 5;
  if (flagged) score += 3;
  if (hasAttachments) score += 2;
  if (urgent) score += 3;
  if (ageHours <= 6) score += 3;
  else if (ageHours <= 24) score += 2;
  else if (ageHours <= 72) score += 1;
  return {
    direct,
    unread,
    flagged,
    hasAttachments,
    urgent,
    score,
    priority: urgent || (direct && unread) || score >= 8,
  };
}

function selectFocusMessages(messages, limit = 8) {
  return (messages || [])
    .filter((message) => message && message.unread)
    .map((message) => ({
      ...message,
      focusMeta: buildPriorityMetaForMessage(message),
    }))
    .sort((left, right) => (
      (Number(right.focusMeta.priority) - Number(left.focusMeta.priority))
      || (right.focusMeta.score - left.focusMeta.score)
      || (getMessageDateValue(right) - getMessageDateValue(left))
    ))
    .slice(0, limit);
}

function selectFocusOutbox(items, limit = 6) {
  const order = { failed: 0, sending: 1, pending: 2 };
  return (items || [])
    .filter((item) => item && Object.prototype.hasOwnProperty.call(order, item.status))
    .sort((left, right) => (
      order[left.status] - order[right.status]
      || new Date(right.scheduled_at || 0).getTime() - new Date(left.scheduled_at || 0).getTime()
    ))
    .slice(0, limit);
}

function selectFocusReturned(items, now = Date.now(), limit = 6) {
  const cutoff = now - 24 * 60 * 60 * 1000;
  return (items || [])
    .filter((item) => {
      const wokenAt = new Date(item && item.woken_at || 0).getTime();
      return item
        && item.status === "woken"
        && Number.isFinite(wokenAt)
        && wokenAt >= cutoff;
    })
    .sort((left, right) => (
      new Date(right.woken_at || 0).getTime() - new Date(left.woken_at || 0).getTime()
    ))
    .slice(0, limit);
}

function selectFocusEvents(events, now = Date.now(), limit = 6) {
  const horizon = now + 7 * 24 * 60 * 60 * 1000;
  return (events || [])
    .filter((event) => {
      const start = new Date(event && event.start || 0).getTime();
      const end = new Date(event && (event.end || event.start) || 0).getTime();
      return event
        && String(event.status || "").toUpperCase() !== "CANCELLED"
        && Number.isFinite(start)
        && Number.isFinite(end)
        && end >= now
        && start <= horizon;
    })
    .sort((left, right) => (
      new Date(left.start || 0).getTime() - new Date(right.start || 0).getTime()
    ))
    .slice(0, limit);
}

function isInviteAwaitingResponse(message) {
  const invite = message && message.invite;
  if (!canRsvpInvite(invite)) return false;
  const attendee = findCurrentInviteAttendee(invite);
  const response = normalizeInviteResponse(
    invite.response_status || (attendee && attendee.partstat) || "",
  );
  return !["ACCEPTED", "TENTATIVE", "DECLINED"].includes(response);
}

async function fetchFocusMessages() {
  return apiFetch("/messages?folder=INBOX&page=1&per_page=50&unread=true");
}

async function fetchFocusInvitations(messages) {
  const candidates = (messages || [])
    .filter((message) => (
      message
      && (
        message.has_attachments
        || /(invite|invitation|meeting|встреч|приглаш)/i.test(message.subject || "")
      )
    ))
    .slice(0, 20);
  const details = await Promise.allSettled(
    candidates.map((message) => fetchMessageDetail(message.uid, "INBOX")),
  );
  return details
    .filter((result) => result.status === "fulfilled")
    .map((result) => result.value)
    .filter(isInviteAwaitingResponse)
    .slice(0, 6);
}

function focusErrorMessage(reason) {
  if (!reason) return "Источник временно недоступен";
  return reason.message || String(reason);
}

async function loadFocus({ force = false } = {}) {
  const focus = state.ui.focus;
  if (!force && focus.loadedAt && Date.now() - focus.loadedAt < 60000) {
    renderFocus();
    return;
  }
  const requestId = ++state.ui.focusRequestId;
  focus.loading = true;
  renderFocus();
  const results = await Promise.allSettled([
    fetchFocusMessages(),
    apiFetch("/scheduled"),
    apiFetch("/snoozed"),
    apiFetch("/calendar/events"),
  ]);
  if (requestId !== state.ui.focusRequestId) return;

  const [messagesResult, outboxResult, returnedResult, eventsResult] = results;
  const errors = {};
  if (messagesResult.status === "fulfilled") {
    const sourceMessages = messagesResult.value.messages || [];
    focus.messages = selectFocusMessages(sourceMessages);
    try {
      focus.invitations = await fetchFocusInvitations(sourceMessages);
    } catch (error) {
      errors.invitations = focusErrorMessage(error);
    }
  } else {
    const message = focusErrorMessage(messagesResult.reason);
    errors.messages = message;
    errors.invitations = message;
  }
  if (requestId !== state.ui.focusRequestId) return;

  if (outboxResult.status === "fulfilled") {
    focus.outbox = selectFocusOutbox(outboxResult.value.items || []);
  } else {
    errors.outbox = focusErrorMessage(outboxResult.reason);
  }
  if (returnedResult.status === "fulfilled") {
    focus.returned = selectFocusReturned(returnedResult.value.items || []);
  } else {
    errors.returned = focusErrorMessage(returnedResult.reason);
  }
  if (eventsResult.status === "fulfilled") {
    focus.events = selectFocusEvents(eventsResult.value.events || []);
  } else {
    errors.events = focusErrorMessage(eventsResult.reason);
  }
  focus.errors = errors;
  focus.loading = false;
  focus.loadedAt = Date.now();
  renderFocus();
}

function focusStateMarkup({ empty, error, retrySource }) {
  if (error) {
    return `
      <div class="focus-state focus-state-error">
        <span>${escapeHtml(error)}</span>
        <button class="focus-link" type="button" data-focus-action="retry-source" data-source="${escapeHtml(retrySource)}">Повторить</button>
      </div>
    `;
  }
  return `<div class="focus-state">${escapeHtml(empty)}</div>`;
}

function renderFocusAttention() {
  const host = $("#focus-attention");
  const items = state.ui.focus.messages;
  $("#focus-attention-count").textContent = String(items.length);
  if (state.ui.focus.loading && !state.ui.focus.loadedAt && !items.length) {
    host.innerHTML = '<div class="focus-state">Загружаем важные письма…</div>';
    return;
  }
  if (!items.length) {
    host.innerHTML = focusStateMarkup({
      empty: "Нет непрочитанных писем, требующих внимания.",
      error: state.ui.focus.errors.messages,
      retrySource: "messages",
    });
    return;
  }
  host.innerHTML = items.map((message) => {
    const meta = message.focusMeta || buildPriorityMetaForMessage(message);
    const tags = [
      meta.direct ? "Мне лично" : "",
      meta.urgent ? "Срочно" : "",
      meta.flagged ? "Со звездочкой" : "",
    ].filter(Boolean);
    return `
      <article class="focus-item">
        <div class="focus-item-main">
          <div class="focus-item-title">${escapeHtml(message.subject || "(без темы)")}</div>
          <div class="focus-item-meta">
            <span>${escapeHtml(message.from || "(неизвестный отправитель)")}</span>
            <span>${escapeHtml(formatDate(message.date))}</span>
          </div>
          ${message.snippet ? `<p class="focus-item-copy">${escapeHtml(message.snippet)}</p>` : ""}
          ${tags.length ? `<div class="focus-tags">${tags.map((tag) => `<span>${escapeHtml(tag)}</span>`).join("")}</div>` : ""}
        </div>
        <button class="mini-btn" type="button" data-focus-action="open-message" data-uid="${escapeHtml(message.uid)}">Открыть</button>
      </article>
    `;
  }).join("");
}

function renderFocusInvitations() {
  const host = $("#focus-invitations");
  const items = state.ui.focus.invitations;
  $("#focus-invitations-count").textContent = String(items.length);
  if (state.ui.focus.loading && !state.ui.focus.loadedAt && !items.length) {
    host.innerHTML = '<div class="focus-state">Ищем приглашения…</div>';
    return;
  }
  if (!items.length) {
    host.innerHTML = focusStateMarkup({
      empty: "Нет приглашений, ожидающих ответа.",
      error: state.ui.focus.errors.invitations,
      retrySource: "invitations",
    });
    return;
  }
  host.innerHTML = items.map((message) => {
    const invite = message.invite || {};
    return `
      <article class="focus-item focus-invite-item">
        <div class="focus-item-main">
          <div class="focus-item-title">${escapeHtml(invite.summary || message.subject || "Событие")}</div>
          <div class="focus-item-meta">
            <span>${escapeHtml(buildInviteWhen(invite) || "Время не указано")}</span>
            ${invite.organizer ? `<span>${escapeHtml(invite.organizer.name || invite.organizer.email || "")}</span>` : ""}
          </div>
          ${invite.location ? `<p class="focus-item-copy">${escapeHtml(invite.location)}</p>` : ""}
        </div>
        <div class="focus-item-actions">
          <button class="focus-link" type="button" data-focus-action="open-message" data-uid="${escapeHtml(message.uid)}">Открыть</button>
          <button class="mini-btn" type="button" data-focus-action="invite-rsvp" data-uid="${escapeHtml(message.uid)}" data-response="accepted">Принять</button>
          <button class="mini-btn" type="button" data-focus-action="invite-rsvp" data-uid="${escapeHtml(message.uid)}" data-response="tentative">Возможно</button>
          <button class="mini-btn" type="button" data-focus-action="invite-rsvp" data-uid="${escapeHtml(message.uid)}" data-response="declined">Отклонить</button>
        </div>
      </article>
    `;
  }).join("");
}

function renderFocusOutbox() {
  const host = $("#focus-outbox");
  const items = state.ui.focus.outbox;
  if (state.ui.focus.loading && !state.ui.focus.loadedAt && !items.length) {
    host.innerHTML = '<div class="focus-state">Проверяем исходящие…</div>';
    return;
  }
  if (!items.length) {
    host.innerHTML = focusStateMarkup({
      empty: "Нет писем, ожидающих отправки.",
      error: state.ui.focus.errors.outbox,
      retrySource: "outbox",
    });
    return;
  }
  const labels = { failed: "Ошибка", sending: "Отправляется", pending: "Ожидает" };
  host.innerHTML = items.map((item) => `
    <article class="focus-item">
      <div class="focus-item-main">
        <div class="focus-item-title">${escapeHtml(item.subject || "(без темы)")}</div>
        <div class="focus-item-meta">
          <span class="focus-status focus-status-${escapeHtml(item.status)}">${escapeHtml(labels[item.status] || item.status)}</span>
          <span>${escapeHtml(new Date(item.scheduled_at || 0).toLocaleString("ru-RU"))}</span>
        </div>
        ${item.last_error ? `<p class="focus-item-copy focus-error-copy">${escapeHtml(item.last_error)}</p>` : ""}
      </div>
      <div class="focus-item-actions">
        ${item.status === "failed" ? `<button class="mini-btn" type="button" data-focus-action="retry-outbox" data-id="${escapeHtml(item.id)}">Повторить</button>` : ""}
        ${item.status === "pending" ? `<button class="mini-btn" type="button" data-focus-action="cancel-outbox" data-id="${escapeHtml(item.id)}">Отменить</button>` : ""}
      </div>
    </article>
  `).join("");
}

function renderFocusReturned() {
  const host = $("#focus-returned");
  const items = state.ui.focus.returned;
  $("#focus-returned-count").textContent = String(items.length);
  if (state.ui.focus.loading && !state.ui.focus.loadedAt && !items.length) {
    host.innerHTML = '<div class="focus-state">Проверяем отложенные письма…</div>';
    return;
  }
  if (!items.length) {
    host.innerHTML = focusStateMarkup({
      empty: "За последние сутки ничего не возвращалось.",
      error: state.ui.focus.errors.returned,
      retrySource: "returned",
    });
    return;
  }
  host.innerHTML = items.map((item) => `
    <article class="focus-item">
      <div class="focus-item-main">
        <div class="focus-item-title">${escapeHtml(item.subject || "(без темы)")}</div>
        <div class="focus-item-meta">
          <span>${escapeHtml(item.from_addr || "(неизвестный отправитель)")}</span>
          <span>${escapeHtml(new Date(item.woken_at || 0).toLocaleString("ru-RU"))}</span>
        </div>
      </div>
      <button class="mini-btn" type="button" data-focus-action="find-returned" data-id="${escapeHtml(item.id)}">Найти во входящих</button>
    </article>
  `).join("");
}

function renderFocusEvents() {
  const host = $("#focus-events");
  const items = state.ui.focus.events;
  if (state.ui.focus.loading && !state.ui.focus.loadedAt && !items.length) {
    host.innerHTML = '<div class="focus-state">Загружаем календарь…</div>';
    return;
  }
  if (!items.length) {
    host.innerHTML = focusStateMarkup({
      empty: "В ближайшие семь дней событий нет.",
      error: state.ui.focus.errors.events,
      retrySource: "events",
    });
    return;
  }
  host.innerHTML = items.map((event) => `
    <article class="focus-item">
      <div class="focus-item-main">
        <div class="focus-item-title">${escapeHtml(event.summary || "Событие")}</div>
        <div class="focus-item-meta">
          <span>${escapeHtml(new Date(event.start || 0).toLocaleString("ru-RU"))}</span>
          ${event.location ? `<span>${escapeHtml(event.location)}</span>` : ""}
        </div>
      </div>
      <button class="focus-link" type="button" data-focus-action="open-calendar">Календарь</button>
    </article>
  `).join("");
}

function renderFocus() {
  const focus = state.ui.focus;
  const actionable = (
    focus.messages.length
    + focus.invitations.length
    + focus.outbox.length
    + focus.returned.length
    + focus.events.length
  );
  const summary = $("#focus-summary");
  const badge = $("#focus-nav-count");
  if (focus.loading) {
    summary.textContent = focus.loadedAt ? "Обновляем рабочую очередь…" : "Загружаем рабочую очередь…";
  } else if (actionable) {
    summary.textContent = `${actionable} ${actionable === 1 ? "действие" : "действий"} в рабочей очереди`;
  } else {
    summary.textContent = Object.keys(focus.errors).length
      ? "Часть источников временно недоступна"
      : "Рабочая очередь пуста";
  }
  badge.textContent = String(actionable);
  badge.classList.toggle("hidden", actionable === 0);
  $("#focus-updated-at").textContent = focus.loadedAt
    ? `Обновлено ${new Date(focus.loadedAt).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })}`
    : "";
  $("#focus-live").textContent = summary.textContent;
  renderFocusAttention();
  renderFocusInvitations();
  renderFocusOutbox();
  renderFocusReturned();
  renderFocusEvents();
}

function isPriorityInboxAvailable() {
  return !isSharedMailboxActive() && state.currentFolder === "INBOX";
}

function getBaseItems() {
  return state.threadMode ? buildThreadGroups(state.messages) : state.messages.slice();
}

function getItemPriorityMeta(item) {
  if (!item) return { direct: false, unread: false, flagged: false, hasAttachments: false, urgent: false, score: 0, priority: false };
  return state.threadMode ? (item.priorityMeta || buildPriorityMetaForMessage(item.head)) : buildPriorityMetaForMessage(item);
}

function getItemDateValue(item) {
  if (!item) return 0;
  return getMessageDateValue(state.threadMode ? item.head : item);
}

function applyPriorityInbox(items) {
  if (!isPriorityInboxAvailable() || !state.ui.priorityMode) return items.slice();
  const focus = state.ui.priorityFocus || "all";
  const filtered = items.filter((item) => {
    const meta = getItemPriorityMeta(item);
    if (focus === "priority") return meta.priority;
    if (focus === "direct") return meta.direct;
    if (focus === "unread") return meta.unread;
    if (focus === "attachments") return meta.hasAttachments;
    if (focus === "flagged") return meta.flagged;
    return true;
  });
  return filtered.sort((left, right) => {
    const a = getItemPriorityMeta(left);
    const b = getItemPriorityMeta(right);
    return (
      (b.score - a.score) ||
      (Number(b.unread) - Number(a.unread)) ||
      (Number(b.direct) - Number(a.direct)) ||
      (Number(b.flagged) - Number(a.flagged)) ||
      (getItemDateValue(right) - getItemDateValue(left))
    );
  });
}

function buildPrioritySummary(items) {
  const summary = { priority: 0, direct: 0, unread: 0, attachments: 0, flagged: 0 };
  items.forEach((item) => {
    const meta = getItemPriorityMeta(item);
    if (meta.priority) summary.priority += 1;
    if (meta.direct) summary.direct += 1;
    if (meta.unread) summary.unread += 1;
    if (meta.hasAttachments) summary.attachments += 1;
    if (meta.flagged) summary.flagged += 1;
  });
  return summary;
}

function getVisibleUidsFromItems(items = getVisibleItems()) {
  const out = [];
  items.forEach((item) => {
    if (state.threadMode && item && Array.isArray(item.uids)) out.push(...item.uids);
    else if (item && item.uid) out.push(item.uid);
  });
  return out;
}

function syncResponsiveShell() {
  const app = $("#app");
  if (!app) return;
  if (!isMobileLayout()) {
    app.classList.remove("show-sidebar", "show-reader");
    return;
  }
  if (state.currentMessage) app.classList.add("show-reader");
  else app.classList.remove("show-reader");
}

function normalizeThreadSubject(value) {
  let current = String(value || "").trim();
  if (!current) return "";
  while (true) {
    const next = current.replace(/^\s*((re|fw|fwd|aw|sv)\s*:\s*)+/i, "").trim();
    if (next === current) break;
    current = next;
  }
  return current.toLowerCase();
}

function buildThreadGroups(messages) {
  const groups = new Map();
  const idToKey = new Map();
  const subjectToKey = new Map();
  messages.forEach((message) => {
    const refs = Array.isArray(message.references) ? message.references.filter(Boolean) : [];
    const ids = [message.message_id, message.in_reply_to, ...refs].filter(Boolean).map((item) => String(item).trim());
    const subjectKey = String(message.subject_base || normalizeThreadSubject(message.subject || "")).trim();
    let key = ids.map((id) => idToKey.get(id)).find(Boolean) || null;
    if (!key && !ids.length && subjectKey) key = subjectToKey.get(subjectKey) || null;
    if (!key) key = ids[0] ? `id:${ids[0]}` : `subject:${subjectKey || message.uid}`;
    if (!groups.has(key)) {
      groups.set(key, {
        key,
        messages: [],
        unreadCount: 0,
        flaggedCount: 0,
      });
    }
    const group = groups.get(key);
    group.messages.push(message);
    if (message.unread) group.unreadCount += 1;
    if (message.flagged) group.flaggedCount += 1;
    ids.forEach((id) => idToKey.set(id, key));
    if (subjectKey && !subjectToKey.has(subjectKey)) subjectToKey.set(subjectKey, key);
  });
  return Array.from(groups.values()).map((group) => {
    const latest = group.messages.reduce((best, message) => (
      !best || getMessageDateValue(message) >= getMessageDateValue(best) ? message : best
    ), null);
    const preview = group.messages.find((message) => message.unread) || latest || group.messages[0] || null;
    const messageMeta = group.messages.map((message) => buildPriorityMetaForMessage(message));
    const topScore = messageMeta.reduce((best, meta) => Math.max(best, meta.score), 0);
    group.head = latest || group.messages[0] || null;
    group.preview = preview || group.head;
    group.uids = group.messages.map((message) => message.uid);
    group.subject = (group.head && group.head.subject) || "";
    group.subjectBase = (group.head && group.head.subject_base) || "";
    group.priorityMeta = {
      direct: messageMeta.some((meta) => meta.direct),
      unread: group.unreadCount > 0,
      flagged: group.flaggedCount > 0,
      hasAttachments: group.messages.some((message) => message.has_attachments),
      urgent: messageMeta.some((meta) => meta.urgent),
      score: topScore + Math.min(group.unreadCount, 3) + (group.messages.length > 2 ? 1 : 0),
      priority: false,
    };
    group.priorityMeta.priority = group.priorityMeta.urgent || (group.priorityMeta.direct && group.priorityMeta.unread) || group.priorityMeta.score >= 8;
    return group;
  });
}

function getVisibleItems(baseItems = null) {
  const items = baseItems || getBaseItems();
  return applyPriorityInbox(items);
}

function getCurrentThreadGroup() {
  if (!state.selectedUid) return null;
  return getBaseItems().find((group) => group.uids.includes(state.selectedUid)) || null;
}

async function apiFetch(path, opts = {}) {
  const mailboxContext = opts.mailboxContext || captureMailboxContext();
  const requestOptions = { ...opts };
  delete requestOptions.mailboxContext;
  const headers = { ...(opts.headers || {}) };
  if (!(opts.body instanceof FormData) && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  if (isMailboxScopedPath(path) && mailboxContext.isShared) {
    headers["X-Mailbox-Context"] = mailboxContext.key;
  }
  const res = await fetch(API + path, {
    credentials: "same-origin",
    ...requestOptions,
    headers,
  });
  let payload = {};
  try { payload = await res.json(); } catch (_) {}
  if (!res.ok) {
    if (
      res.status === 403
      && mailboxContext.isShared
      && isMailboxContextCurrent(mailboxContext)
      && isMailboxScopedPath(path)
      && String(payload.detail || payload.error || "").includes("Нет доступа к общему ящику")
    ) {
      queueMicrotask(() => recoverRevokedSharedMailbox());
    }
    const err = new Error(payload.error || payload.detail || `HTTP ${res.status}`);
    err.status = res.status;
    err.payload = payload;
    throw err;
  }
  return payload;
}

function isMailboxScopedPath(path) {
  return /^\/(?:bootstrap|folders|messages)(?:[/?]|$)/.test(String(path || ""));
}

function activeMailboxKey() {
  return (state.activeMailbox && state.activeMailbox.key) || "personal";
}

function isSharedMailboxActive() {
  return Boolean(state.activeMailbox && state.activeMailbox.is_shared);
}

function personalMailboxContext() {
  const me = state.me || {};
  return {
    key: "personal",
    is_shared: false,
    mailbox_id: null,
    email: me.email || "",
    from_address: me.from_address || me.email || "",
    display_name: me.display_name || me.from_address || me.email || "Личный ящик",
    can_send: true,
  };
}

function contextualApiUrl(path) {
  const url = new URL(API + path, window.location.origin);
  if (isSharedMailboxActive()) {
    url.searchParams.set("mailbox_context", activeMailboxKey());
  }
  return `${url.pathname}${url.search}`;
}

function escapeHtml(value) {
  if (value == null) return "";
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function renderUserProfileAvatar(profileCard = null) {
  const employee = (profileCard && profileCard.employee) || {};
  const displayName = employee.fullName
    || (state.me && state.me.display_name)
    || (state.me && state.me.email)
    || "Пользователь";
  renderProfileAvatar($("#user-profile-avatar"), employee.avatarDataUrl, displayName);
}

function renderProfileAvatar(target, avatarDataUrl, displayName) {
  if (!target) return;
  target.replaceChildren();
  if (avatarDataUrl) {
    const image = document.createElement("img");
    image.src = avatarDataUrl;
    image.alt = "";
    image.addEventListener("error", () => {
      target.replaceChildren();
      target.textContent = initials(displayName);
    }, { once: true });
    target.appendChild(image);
    return;
  }
  target.textContent = initials(displayName);
}

const ACCOUNT_PROVIDERS = [
  { key: "telegram", label: "Telegram", mark: "TG" },
  { key: "vk", label: "VK ID", mark: "VK" },
  { key: "yandex", label: "Яндекс", mark: "Я" },
  {
    key: "yandexMail",
    label: "Яндекс Почта",
    mark: "Я",
    linkProvider: "yandex/mail",
  },
  { key: "bitrix24", label: "Bitrix24", mark: "B24" },
];

function accountErrorMessage(code) {
  return ({
    account_session_required: "Войдите через RuPochta ID, чтобы открыть личный кабинет.",
    password_current_invalid: "Текущий пароль введён неверно.",
    password_too_short: "Пароль должен содержать минимум 8 символов.",
    current_password_required: "Введите текущий пароль.",
    too_many_attempts: "Слишком много попыток. Попробуйте позже.",
    last_login_method: "Это последний способ входа. Сначала установите пароль.",
    social_identity_not_linked: "Этот способ входа уже отключён.",
    last_name_required: "Укажите фамилию.",
    first_name_required: "Укажите имя.",
    avatar_invalid: "Не удалось сохранить изображение.",
  })[code] || `Не удалось выполнить действие: ${code}`;
}

function setAccountStatus(target, text = "", kind = "") {
  target.textContent = text;
  target.className = `muted${kind ? ` ${kind}` : ""}`;
}

function renderAccountProviders(account = {}) {
  const linked = new Set(account.authProviders || []);
  const container = $("#account-providers");
  container.replaceChildren();
  for (const provider of ACCOUNT_PROVIDERS) {
    const connected = linked.has(provider.key);
    const row = document.createElement("div");
    row.className = "account-provider";

    const name = document.createElement("div");
    name.className = "account-provider-name";
    const mark = document.createElement("span");
    mark.className = `provider-mark ${provider.key}`;
    mark.setAttribute("aria-hidden", "true");
    mark.textContent = provider.mark;
    name.append(mark, document.createTextNode(provider.label));

    const status = document.createElement("span");
    status.className = `account-provider-status${connected ? " connected" : ""}`;
    status.textContent = connected ? "✓ Подключён" : "Не подключён";

    const actions = document.createElement("div");
    actions.className = "account-provider-actions";
    if (connected) {
      const unlinkProvider = provider.unlinkProvider || provider.key;
      const unlink = document.createElement("button");
      unlink.className = "btn btn-secondary danger";
      unlink.type = "button";
      unlink.textContent = "Отключить";
      unlink.addEventListener("click", async () => {
        if (!confirm(`Отключить ${provider.label}?`)) return;
        unlink.disabled = true;
        try {
          await apiFetch(`/account/identities/${unlinkProvider}/unlink`, { method: "POST" });
          await loadAccount();
          setAccountStatus($("#account-password-status"), `${provider.label} отключён.`, "success");
        } catch (error) {
          setAccountStatus($("#account-password-status"), accountErrorMessage(error.message), "error");
          unlink.disabled = false;
        }
      });
      actions.append(unlink);
    } else {
      const link = document.createElement("a");
      link.className = "btn btn-secondary";
      const linkProvider = provider.linkProvider || provider.key;
      link.href = `https://auth.example.com/oauth/${linkProvider}/start?intent=link&surface=mail`;
      link.textContent = "Подключить";
      actions.append(link);
    }
    row.append(name, status, actions);
    container.append(row);
  }
}

function renderAccount(payload = {}) {
  const profile = payload.profile || {};
  const account = payload.account || {};
  state.ui.accountProfile = profile;
  state.ui.accountAvatarDataUrl = profile.avatarDataUrl || "";
  state.ui.accountHasPassword = Boolean(account.hasPassword);
  $("#account-last-name").value = profile.lastName || "";
  $("#account-first-name").value = profile.firstName || "";
  $("#account-middle-name").value = profile.middleName || "";
  $("#account-email").value = account.email || profile.email || "";
  renderProfileAvatar(
    $("#account-avatar-button"),
    state.ui.accountAvatarDataUrl,
    profile.displayName || account.username || "RuPochta",
  );
  renderProfileAvatar(
    $("#user-profile-avatar"),
    state.ui.accountAvatarDataUrl,
    profile.displayName || account.username || "RuPochta",
  );
  $("#account-avatar-remove").classList.toggle("hidden", !state.ui.accountAvatarDataUrl);
  renderAccountProviders(account);
  $("#account-password-toggle").textContent = state.ui.accountHasPassword
    ? "Сменить пароль"
    : "Установить пароль";
  $("#account-current-password-row").classList.toggle("hidden", !state.ui.accountHasPassword);
  $("#account-current-password").required = state.ui.accountHasPassword;
  $("#account-content").classList.remove("hidden");
  $("#account-loading").classList.add("hidden");
}

async function loadAccount() {
  const payload = await apiFetch("/account");
  renderAccount(payload);
  return payload;
}

async function openAccount(trigger = null) {
  openAccessibleModal("account-modal", trigger);
  $("#account-content").classList.add("hidden");
  $("#account-loading").classList.remove("hidden");
  $("#account-loading").textContent = "Загрузка кабинета…";
  setAccountStatus($("#account-profile-status"));
  setAccountStatus($("#account-password-status"));
  try {
    await loadAccount();
  } catch (error) {
    $("#account-loading").textContent = accountErrorMessage(error.message);
  }
}

function compressAccountAvatar(file) {
  return new Promise((resolve, reject) => {
    if (!file || !["image/png", "image/jpeg", "image/webp"].includes(file.type)) {
      reject(new Error("Выберите PNG, JPEG или WebP."));
      return;
    }
    const image = new Image();
    const reader = new FileReader();
    image.onload = () => {
      try {
        const scale = Math.min(1, 512 / Math.max(image.naturalWidth, image.naturalHeight));
        const canvas = document.createElement("canvas");
        canvas.width = Math.max(1, Math.round(image.naturalWidth * scale));
        canvas.height = Math.max(1, Math.round(image.naturalHeight * scale));
        canvas.getContext("2d").drawImage(image, 0, 0, canvas.width, canvas.height);
        const dataUrl = canvas.toDataURL("image/webp", 0.86);
        if (dataUrl.length > 4 * 1024 * 1024) throw new Error("Изображение слишком большое.");
        resolve(dataUrl);
      } catch (error) {
        reject(error);
      }
    };
    image.onerror = () => reject(new Error("Не удалось прочитать изображение."));
    reader.onload = () => { image.src = String(reader.result || ""); };
    reader.onerror = () => reject(new Error("Не удалось прочитать изображение."));
    reader.readAsDataURL(file);
  });
}

async function saveAccountProfile(event) {
  event.preventDefault();
  const button = $("#account-profile-save");
  button.disabled = true;
  setAccountStatus($("#account-profile-status"), "Сохранение…");
  try {
    const payload = await apiFetch("/account/profile", {
      method: "PATCH",
      body: JSON.stringify({
        lastName: $("#account-last-name").value.trim(),
        firstName: $("#account-first-name").value.trim(),
        middleName: $("#account-middle-name").value.trim(),
        avatarDataUrl: state.ui.accountAvatarDataUrl,
      }),
    });
    const profile = payload.profile || {};
    state.ui.accountProfile = profile;
    state.ui.accountAvatarDataUrl = profile.avatarDataUrl || "";
    $("#account-last-name").value = profile.lastName || "";
    $("#account-first-name").value = profile.firstName || "";
    $("#account-middle-name").value = profile.middleName || "";
    renderProfileAvatar(
      $("#account-avatar-button"),
      state.ui.accountAvatarDataUrl,
      profile.displayName || "RuPochta",
    );
    $("#account-avatar-remove").classList.toggle("hidden", !state.ui.accountAvatarDataUrl);
    if (state.me) {
      state.me.profile_card = {
        employee: {
          fullName: profile.displayName || state.me.display_name,
          avatarDataUrl: profile.avatarDataUrl || "",
        },
      };
      renderUserProfileAvatar(state.me.profile_card);
    }
    setAccountStatus($("#account-profile-status"), "Изменения сохранены.", "success");
  } catch (error) {
    setAccountStatus($("#account-profile-status"), accountErrorMessage(error.message), "error");
  } finally {
    button.disabled = false;
  }
}

async function saveAccountPassword(event) {
  event.preventDefault();
  const current = $("#account-current-password").value;
  const next = $("#account-new-password").value;
  if (next !== $("#account-new-password-confirm").value) {
    setAccountStatus($("#account-password-status"), "Новый пароль и его повтор не совпадают.", "error");
    return;
  }
  const button = event.currentTarget.querySelector('button[type="submit"]');
  button.disabled = true;
  try {
    await apiFetch("/account/password", {
      method: "POST",
      body: JSON.stringify({ currentPassword: current, newPassword: next }),
    });
    state.ui.accountHasPassword = true;
    event.currentTarget.reset();
    event.currentTarget.classList.add("hidden");
    $("#account-password-toggle").textContent = "Сменить пароль";
    $("#account-current-password-row").classList.remove("hidden");
    $("#account-current-password").required = true;
    setAccountStatus($("#account-password-status"), "Пароль сохранён.", "success");
  } catch (error) {
    setAccountStatus($("#account-password-status"), accountErrorMessage(error.message), "error");
  } finally {
    button.disabled = false;
  }
}

function formatDate(raw) {
  if (!raw) return "";
  const d = new Date(raw);
  if (isNaN(d.getTime())) return raw;
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  if (sameDay) return d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  const sameYear = d.getFullYear() === now.getFullYear();
  return d.toLocaleDateString("ru-RU", {
    day: "2-digit",
    month: "short",
    year: sameYear ? undefined : "2-digit",
  });
}

function formatInviteDateTime(point) {
  if (!point || !point.iso) return "";
  if (point.all_day) {
    const d = new Date(point.iso);
    return isNaN(d.getTime()) ? point.iso : d.toLocaleDateString("ru-RU", {
      day: "2-digit",
      month: "long",
      year: "numeric",
    });
  }
  const d = new Date(point.iso);
  return isNaN(d.getTime()) ? point.iso : d.toLocaleString("ru-RU", {
    day: "2-digit",
    month: "long",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function buildInviteWhen(invite) {
  if (!invite || !invite.start) return "";
  const start = formatInviteDateTime(invite.start);
  const end = formatInviteDateTime(invite.end);
  if (!start) return "";
  return end ? `${start} - ${end}` : start;
}

function normalizeInviteResponse(value) {
  return String(value || "").trim().toUpperCase();
}

function inviteResponseMeta(value) {
  const key = normalizeInviteResponse(value);
  if (key === "ACCEPTED") return { key: "accepted", label: "Принято" };
  if (key === "TENTATIVE") return { key: "tentative", label: "Возможно" };
  if (key === "DECLINED") return { key: "declined", label: "Отклонено" };
  if (key === "NEEDS-ACTION") return { key: "pending", label: "Ожидает ответа" };
  return { key: "", label: "" };
}

function findCurrentInviteAttendee(invite) {
  if (!invite || !Array.isArray(invite.attendees) || !state.me) return null;
  const emails = getSelfEmails();
  return invite.attendees.find((attendee) => attendee && emails.has(String(attendee.email || "").toLowerCase())) || null;
}

function canRsvpInvite(invite) {
  if (!invite || !state.me) return false;
  if (isSharedMailboxActive()) return false;
  if (normalizeInviteResponse(invite.method) !== "REQUEST") return false;
  if (!invite.uid) return false;
  if (!(invite.organizer && invite.organizer.email)) return false;
  return normalizeInviteResponse(invite.status) !== "CANCELLED";
}

function renderInviteCard(invite, message) {
  if (!invite) return "";
  const when = buildInviteWhen(invite);
  const attendees = Array.isArray(invite.attendees) ? invite.attendees.filter((a) => a && (a.name || a.email)) : [];
  const attendeesText = attendees.slice(0, 5).map((a) => a.name ? `${a.name} <${a.email}>` : a.email).join(", ");
  const status = invite.status || invite.method || "";
  const isCancelled = status === "CANCELLED" || invite.method === "CANCEL";
  const currentAttendee = findCurrentInviteAttendee(invite);
  const responseMeta = inviteResponseMeta(invite.response_status || (currentAttendee && currentAttendee.partstat) || "");
  const pendingResponse = String(invite.pending_response || "").toLowerCase();
  const canRsvp = canRsvpInvite(invite);
  const attachmentUrl = invite.attachment_index != null
    ? contextualApiUrl(`/messages/${encodeURIComponent(message.uid)}/attachment/${invite.attachment_index}?folder=${encodeURIComponent(state.currentFolder)}`)
    : "";
  return `
    <div class="invite-card ${isCancelled ? "invite-cancelled" : ""}">
      <div class="invite-head">
        <div>
          <div class="invite-kicker">${isCancelled ? "Отмена встречи" : "Приглашение в календарь"}</div>
          <div class="invite-title">${escapeHtml(invite.summary || message.subject || "Событие")}</div>
        </div>
        ${status ? `<span class="invite-pill">${escapeHtml(status)}</span>` : ""}
      </div>
      <div class="invite-grid">
        ${when ? `<div class="label">Когда</div><div>${escapeHtml(when)}</div>` : ""}
        ${invite.location ? `<div class="label">Где</div><div>${escapeHtml(invite.location)}</div>` : ""}
        ${invite.organizer && (invite.organizer.email || invite.organizer.name) ? `<div class="label">Организатор</div><div>${escapeHtml(invite.organizer.name || invite.organizer.email)}${invite.organizer.name && invite.organizer.email ? ` <span class="muted">&lt;${escapeHtml(invite.organizer.email)}&gt;</span>` : ""}</div>` : ""}
        ${attendeesText ? `<div class="label">Участники</div><div>${escapeHtml(attendeesText)}${attendees.length > 5 ? ` <span class="muted">+${attendees.length - 5}</span>` : ""}</div>` : ""}
      </div>
      ${invite.description ? `<div class="invite-desc">${escapeHtml(invite.description)}</div>` : ""}
      <div class="invite-actions">
        ${attachmentUrl ? `<a class="attachment-link" href="${escapeHtml(attachmentUrl)}" download="${escapeHtml(invite.filename || "invite.ics")}">Скачать .ics</a>` : ""}
        ${canRsvp ? `
          <div class="invite-rsvp-row">
            <button class="mini-btn invite-rsvp-btn ${pendingResponse === "accepted" ? "is-pending" : ""}" data-invite-rsvp="accepted" ${pendingResponse ? "disabled" : ""}>Принять</button>
            <button class="mini-btn invite-rsvp-btn ${pendingResponse === "tentative" ? "is-pending" : ""}" data-invite-rsvp="tentative" ${pendingResponse ? "disabled" : ""}>Возможно</button>
            <button class="mini-btn invite-rsvp-btn ${pendingResponse === "declined" ? "is-pending" : ""}" data-invite-rsvp="declined" ${pendingResponse ? "disabled" : ""}>Отклонить</button>
          </div>
        ` : ""}
      </div>
      ${responseMeta.label ? `<div class="invite-rsvp-state invite-rsvp-${responseMeta.key}">Ваш ответ: ${escapeHtml(responseMeta.label)}</div>` : ""}
    </div>
  `;
}

function toast(msg, cls = "", options = {}) {
  const el = $("#toast");
  if (state.undo.timer) {
    clearTimeout(state.undo.timer);
    state.undo.timer = null;
  }
  if (!(options.actionLabel && typeof options.onAction === "function")) {
    state.undo.payload = null;
  }
  el.innerHTML = "";
  el.className = "toast " + cls;
  const text = document.createElement("span");
  text.className = "toast-text";
  text.textContent = msg;
  el.appendChild(text);
  if (options.actionLabel && typeof options.onAction === "function") {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "toast-action";
    btn.textContent = options.actionLabel;
    btn.addEventListener("click", () => {
      options.onAction();
      el.classList.add("hidden");
    });
    el.appendChild(btn);
  }
  el.classList.remove("hidden");
  state.undo.timer = setTimeout(() => {
    el.classList.add("hidden");
    state.undo.timer = null;
  }, options.duration || 3500);
}

function getSelectedUids() {
  return Array.from(state.selectedUids);
}

function clearSelection() {
  state.selectedUids.clear();
  $("#bulk-select-page").checked = false;
  renderBulkToolbar();
}

function composeBodyText() {
  return $("#compose-body").innerText.replace(/\u00a0/g, " ").trim();
}

function sanitizeComposeHtml(root) {
  const allowed = new Set(["DIV", "P", "BR", "B", "STRONG", "I", "EM", "U", "UL", "OL", "LI", "A", "IMG"]);
  const output = document.createElement("div");
  const appendSafe = (source, target) => {
    if (source.nodeType === Node.TEXT_NODE) {
      target.appendChild(document.createTextNode(source.textContent || ""));
      return;
    }
    if (source.nodeType !== Node.ELEMENT_NODE) return;
    if (!allowed.has(source.tagName)) {
      Array.from(source.childNodes).forEach((child) => appendSafe(child, target));
      return;
    }
    const element = document.createElement(source.tagName.toLowerCase());
    if (source.tagName === "A") {
      const href = source.getAttribute("href") || "";
      if (/^(https?:|mailto:)/i.test(href)) {
        element.setAttribute("href", href);
        element.setAttribute("rel", "noopener noreferrer");
      }
    }
    if (source.tagName === "IMG") {
      const src = source.getAttribute("src") || "";
      if (!/^data:image\/(png|jpe?g|webp|gif);base64,/i.test(src)) return;
      const width = Math.max(40, Math.min(900, Number(source.getAttribute("width")) || 320));
      element.setAttribute("src", src);
      element.setAttribute("alt", source.getAttribute("alt") || "");
      element.setAttribute("width", String(width));
      element.setAttribute("style", `display:block;width:${width}px;max-width:100%;height:auto`);
    }
    Array.from(source.childNodes).forEach((child) => appendSafe(child, element));
    target.appendChild(element);
  };
  Array.from(root.childNodes).forEach((child) => appendSafe(child, output));
  return output.innerHTML;
}

function composeBodyHtml() {
  const editor = $("#compose-body");
  const clone = editor.cloneNode(true);
  const originalFrames = editor.querySelectorAll(".compose-inline-image-frame");
  clone.querySelectorAll(".compose-inline-image-frame").forEach((frame, index) => {
    const source = originalFrames[index];
    const image = frame.querySelector("img");
    if (!image || !source) {
      frame.remove();
      return;
    }
    const width = Math.round(source.getBoundingClientRect().width || parseFloat(source.style.width) || 320);
    image.setAttribute("width", String(width));
    frame.replaceWith(image);
  });
  return sanitizeComposeHtml(clone);
}

function plainTextToComposeHtml(value) {
  return escapeHtml(value || "").replace(/\r?\n/g, "<br>");
}

function setComposeBody(value, isHtml = false) {
  const editor = $("#compose-body");
  editor.innerHTML = isHtml
    ? sanitizeComposeHtml(Object.assign(document.createElement("div"), { innerHTML: value || "" }))
    : plainTextToComposeHtml(value);
  editor.querySelectorAll("img").forEach((image) => {
    const frame = document.createElement("span");
    frame.className = "compose-inline-image-frame";
    frame.contentEditable = "false";
    frame.tabIndex = 0;
    frame.title = "Тяните правый край или нажмите Shift+←/→, чтобы изменить размер";
    frame.style.width = `${Math.max(80, Number(image.getAttribute("width")) || 320)}px`;
    image.replaceWith(frame);
    frame.appendChild(image);
  });
}

function insertComposeNode(node) {
  const editor = $("#compose-body");
  editor.focus();
  const selection = window.getSelection();
  const range = selection && selection.rangeCount ? selection.getRangeAt(0) : null;
  if (!range || !editor.contains(range.commonAncestorContainer)) {
    editor.appendChild(node);
    editor.appendChild(document.createElement("br"));
    return;
  }
  range.deleteContents();
  range.insertNode(node);
  const spacer = document.createElement("br");
  node.after(spacer);
  range.setStartAfter(spacer);
  range.collapse(true);
  selection.removeAllRanges();
  selection.addRange(range);
}

async function insertComposeImage(file) {
  if (!file || !/^image\/(png|jpeg|webp|gif)$/i.test(file.type)) throw new Error("Выберите PNG, JPEG, WebP или GIF");
  if (file.size > 4 * 1024 * 1024) throw new Error("Картинка должна быть не больше 4 МБ");
  const currentBytes = Array.from($("#compose-body").querySelectorAll("img"))
    .reduce((total, image) => total + Math.round((image.src.length || 0) * 0.75), 0);
  if (currentBytes + file.size > 12 * 1024 * 1024) throw new Error("Картинки в письме должны занимать не больше 12 МБ");
  const src = await new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(new Error("Не удалось прочитать картинку"));
    reader.readAsDataURL(file);
  });
  const frame = document.createElement("span");
  frame.className = "compose-inline-image-frame";
  frame.contentEditable = "false";
  frame.tabIndex = 0;
  frame.title = "Тяните правый край или нажмите Shift+←/→, чтобы изменить размер";
  const image = document.createElement("img");
  image.src = src;
  image.alt = file.name || "Картинка";
  frame.appendChild(image);
  insertComposeNode(frame);
  queueComposeAutosave();
}

function formatCompose(command) {
  if (command === "createLink") {
    const value = prompt("Адрес ссылки (https:// или mailto:)", "https://");
    if (!value) return;
    if (!/^(https?:|mailto:)/i.test(value)) {
      setComposeStatus("Ссылка должна начинаться с https://, http:// или mailto:", true);
      return;
    }
    document.execCommand(command, false, value);
  } else {
    document.execCommand(command, false, null);
  }
  $("#compose-body").focus();
  queueComposeAutosave();
}

function draftStorageKey() {
  const owner = String(
    (state.me && (state.me.email || state.me.ad_login)) || "anonymous",
  ).trim().toLowerCase();
  return `jmail-compose-${owner}-${activeMailboxKey()}`;
}

function getComposeSnapshot() {
  return {
    to: $("#compose-to").value.trim(),
    cc: $("#compose-cc").value.trim(),
    bcc: $("#compose-bcc").value.trim(),
    subject: $("#compose-subject").value,
    body: composeBodyHtml(),
    bodyText: composeBodyText(),
    bodyIsHtml: true,
    inviteEnabled: $("#compose-invite-enabled").checked,
    inviteStart: $("#compose-invite-start").value,
    inviteEnd: $("#compose-invite-end").value,
    inviteLocation: $("#compose-invite-location").value,
    inviteDescription: $("#compose-invite-description").value,
    draftUid: state.compose.draftUid,
    mode: state.compose.mode,
    inReplyTo: state.compose.inReplyTo,
    bccVisible: state.compose.bccVisible,
    ts: Date.now(),
  };
}

function persistComposeSnapshot() {
  if (!state.me) return;
  localStorage.setItem(draftStorageKey(), JSON.stringify(getComposeSnapshot()));
}

function loadStoredComposeSnapshot() {
  if (!state.me) return null;
  const raw = localStorage.getItem(draftStorageKey());
  if (!raw) return null;
  try { return JSON.parse(raw); } catch (_) { return null; }
}

function clearStoredComposeSnapshot() {
  if (!state.me) return;
  localStorage.removeItem(draftStorageKey());
}

function setComposeStatus(text, isError = false) {
  const el = $("#compose-status");
  el.textContent = text || "";
  el.style.color = isError ? "var(--danger)" : "";
}

function signatureBlock() {
  const employee = state.me && state.me.profile_card && state.me.profile_card.employee;
  const sender = (employee && employee.fullName)
    || (state.activeMailbox && state.activeMailbox.display_name)
    || (state.me && (state.me.display_name || state.me.email))
    || "RuPochta Mail";
  const identity = (!isSharedMailboxActive() && state.settings.signature.trim()) || sender;
  return `\n\nС уважением,\n${identity}\nRuPochta Mail`;
}

function buildReplyBody(message) {
  return `${signatureBlock()}\n\n----- Исходное сообщение -----\nОт: ${message.from}\nДата: ${message.date}\nТема: ${message.subject}\n\n${message.body_text || ""}`.trim();
}

function buildForwardBody(message) {
  return `${signatureBlock()}\n\n----- Пересылаемое сообщение -----\nОт: ${message.from}\nКому: ${message.to}\nДата: ${message.date}\nТема: ${message.subject}\n\n${message.body_text || ""}`.trim();
}

function buildNewBody() {
  return `\n\n${signatureBlock().trim()}`;
}

function buildBrandedEmailHtml(bodyHtml) {
  const content = bodyHtml || "<br>";
  return `<table role="presentation" width="100%" cellpadding="0" cellspacing="0" background="${BRAND_TEMPLATE.backgroundUrl}" style="width:100%;background:${BRAND_TEMPLATE.backgroundColor} url('${BRAND_TEMPLATE.backgroundUrl}') center/cover no-repeat"><tr><td style="padding:40px 28px"><div style="min-height:280px;padding:28px;border:1px solid ${BRAND_TEMPLATE.borderColor};border-radius:14px;background:${BRAND_TEMPLATE.surfaceColor};color:${BRAND_TEMPLATE.textColor};font:16px/1.55 Segoe UI,Arial,sans-serif">${content}</div><div style="display:flex;align-items:center;gap:10px;padding-top:22px"><img src="${BRAND_TEMPLATE.logoUrl}" width="${BRAND_TEMPLATE.logoWidth}" alt="RuPochta" style="display:block;width:${BRAND_TEMPLATE.logoWidth}px;height:auto"><strong style="color:${BRAND_TEMPLATE.brandColor};font:700 13px/1.2 Segoe UI,Arial,sans-serif;letter-spacing:.14em">MAIL</strong></div></td></tr></table>`;
}

function syncBrandedComposeTemplate() {
  const canvas = $(".compose-template-canvas");
  const logo = canvas && canvas.querySelector(".compose-brand-logo img");
  if (!canvas || !logo) return;
  canvas.style.setProperty("--compose-template-background", BRAND_TEMPLATE.backgroundColor);
  canvas.style.setProperty("--compose-template-image", `url("${BRAND_TEMPLATE.backgroundUrl}")`);
  canvas.style.setProperty("--compose-template-surface", BRAND_TEMPLATE.surfaceColor);
  canvas.style.setProperty("--compose-template-border", BRAND_TEMPLATE.borderColor);
  canvas.style.setProperty("--compose-template-text", BRAND_TEMPLATE.textColor);
  canvas.style.setProperty("--compose-template-brand", BRAND_TEMPLATE.brandColor);
  canvas.style.setProperty("--compose-template-logo-width", `${BRAND_TEMPLATE.logoWidth}px`);
  logo.src = BRAND_TEMPLATE.logoUrl;
}

function appendComposeBody(form, bodyHtml = composeBodyHtml(), bodyText = composeBodyText()) {
  form.append("body", buildBrandedEmailHtml(bodyHtml));
  form.append("body_text", bodyText);
}

function syncMoveTargets() {
  const select = $("#bulk-move-target");
  if (!select) return;
  const current = select.value;
  select.innerHTML = '<option value="">Переместить…</option>';
  state.folders.forEach((folder) => {
    if (folder.name === state.currentFolder) return;
    const opt = document.createElement("option");
    opt.value = folder.name;
    opt.textContent = folder.label || folder.name;
    if (folder.name === current) opt.selected = true;
    select.appendChild(opt);
  });
}

async function loadUserSettings() {
  const profile = await apiFetch("/settings/profile");
  state.settings.signature = profile.signature || "";
  state.settings.templates = profile.templates || [];
  renderComposeTemplateOptions();
  if (!$("#settings-modal").classList.contains("hidden")) {
    renderSettings();
  }
}

function normalizeSharedMailbox(mailbox) {
  return {
    key: `shared:${mailbox.id}`,
    is_shared: true,
    mailbox_id: Number(mailbox.id),
    email: mailbox.email || "",
    from_address: mailbox.email || "",
    display_name: mailbox.display_name || mailbox.email || "Общий ящик",
    can_send: Boolean(mailbox.can_send),
    kind: mailbox.kind || "group",
  };
}

async function loadSharedMailboxes(options = {}) {
  const response = await apiFetch("/shared_mailboxes");
  state.sharedMailboxes = (response.mailboxes || []).map(normalizeSharedMailbox);
  if (isSharedMailboxActive()) {
    const activeMailbox = state.sharedMailboxes.find(
      (mailbox) => mailbox.key === activeMailboxKey(),
    );
    if (activeMailbox) {
      const sendPermissionRevoked = (
        Boolean(state.activeMailbox.can_send)
        && !activeMailbox.can_send
      );
      state.activeMailbox = { ...activeMailbox };
      if (sendPermissionRevoked) {
        invalidateComposeSession(activeMailbox.key);
        setComposeOpen(false);
        toast("Право отправки из общего ящика отозвано", "error");
      }
      updateMailboxContextUi();
    }
  }
  renderMailboxList();
  if (
    options.enforceActive !== false
    && isSharedMailboxActive()
    && !state.sharedMailboxes.some((mailbox) => mailbox.key === activeMailboxKey())
  ) {
    toast("Доступ к общему ящику отозван", "error");
    await switchMailboxContext("personal");
    return false;
  }
  return true;
}

async function recoverRevokedSharedMailbox() {
  if (mailboxAccessRefreshPending) return;
  mailboxAccessRefreshPending = true;
  try {
    await loadSharedMailboxes({ enforceActive: false });
    if (
      isSharedMailboxActive()
      && !state.sharedMailboxes.some((mailbox) => mailbox.key === activeMailboxKey())
    ) {
      toast("Доступ к общему ящику отозван", "error");
      await switchMailboxContext("personal");
    }
  } catch (_) {
    // Keep the current error visible; the next refresh retries the access list.
  } finally {
    mailboxAccessRefreshPending = false;
  }
}

function renderMailboxList() {
  const list = $("#mailbox-list");
  if (!list || !state.me) return;
  const mailboxes = [personalMailboxContext(), ...state.sharedMailboxes];
  list.innerHTML = mailboxes.map((mailbox) => {
    const active = mailbox.key === activeMailboxKey();
    const label = mailbox.is_shared
      ? (mailbox.display_name || mailbox.email)
      : "Личный ящик";
    const permission = mailbox.is_shared
      ? `<span class="mailbox-permission">${mailbox.can_send ? "Можно отправлять" : "Только чтение"}</span>`
      : '<span class="mailbox-permission">Основной</span>';
    return `
      <button class="mailbox-switch${active ? " active" : ""}" type="button" data-mailbox-key="${escapeHtml(mailbox.key)}" aria-current="${active ? "page" : "false"}">
        <span class="mailbox-switch-main">
          <strong>${escapeHtml(label)}</strong>
          <span>${escapeHtml(mailbox.email || mailbox.from_address)}</span>
        </span>
        ${permission}
      </button>
    `;
  }).join("");
}

function updateMailboxContextUi() {
  const shared = isSharedMailboxActive();
  const canSend = activeMailboxCanWrite();
  document.body.dataset.mailboxContext = shared ? "shared" : "personal";
  $("#user-email").textContent = state.activeMailbox.from_address || state.activeMailbox.email;
  $$("[data-workspace]").forEach((button) => {
    const workspace = LM_WORKSPACES[button.dataset.workspace];
    if (!workspace) return;
    button.classList.toggle("hidden", shared && workspace.personalOnly);
  });
  $("#btn-settings").classList.toggle("hidden", shared);
  $("#btn-priority-mode").classList.toggle("hidden", shared);
  $("#priority-row").classList.toggle("hidden", shared);
  $("#btn-compose").disabled = !canSend;
  $("#btn-compose").title = canSend ? "Написать" : "Этот ящик доступен только для чтения";
  $("#compose-invite-enabled").disabled = shared;
  const templateTools = $("#compose-template-select").closest(".compose-tool-group");
  if (templateTools) templateTools.classList.toggle("hidden", shared);
  if (shared) {
    $("#compose-invite-enabled").checked = false;
    syncComposeInviteUi();
  }
  const triageTabs = $("#triage-tabs");
  if (triageTabs) triageTabs.classList.toggle("hidden", shared);
  const scheduled = $("#btn-scheduled");
  if (scheduled) scheduled.classList.toggle("hidden", shared);
  const snoozed = $("#btn-snoozed");
  if (snoozed) snoozed.classList.toggle("hidden", shared);
  const scheduleSend = $("#btn-schedule-send");
  if (scheduleSend) scheduleSend.classList.toggle("hidden", shared);
  const bulkSnooze = $("#btn-bulk-snooze");
  if (bulkSnooze) bulkSnooze.classList.toggle("hidden", shared);
  renderMailboxList();
}

function activeMailboxCanWrite() {
  return !isSharedMailboxActive() || Boolean(state.activeMailbox.can_send);
}

async function switchMailboxContext(key) {
  const next = key === "personal"
    ? personalMailboxContext()
    : state.sharedMailboxes.find((mailbox) => mailbox.key === key);
  if (!next) {
    toast("Ящик больше недоступен", "error");
    return;
  }
  if (next.key === activeMailboxKey()) return;
  if (!(await preserveComposeBeforeContextExit())) return;
  invalidateMailboxRequests();
  invalidateComposeSession();
  clearUndo();
  setComposeOpen(false);
  state.activeMailbox = { ...next };
  state.currentFolder = "INBOX";
  state.ui.sidebarFolderKey = "INBOX";
  state.page = 1;
  state.searchQuery = "";
  state.filters = { unread: false, flagged: false, hasAttachments: false };
  state.messages = [];
  state.folders = [];
  state.totalMessages = 0;
  state.selectedUid = null;
  state.currentMessage = null;
  state.currentThreadUids = [];
  state.currentThreadMessages = [];
  state.selectedUids.clear();
  state.ui.triageCategory = "all";
  state.ui.triageData = {};
  state.ui.triageTotals = {};
  state.ui.triageLoadedAt = 0;
  clearMessageDetailCache();
  clearMessageView();
  $("#search-input").value = "";
  updateMailboxContextUi();
  showWorkspace("mail");
  await Promise.all([
    loadFolders(),
    loadMessages(),
  ]);
}

function applyBootstrapPayload(payload) {
  const me = payload && payload.me ? payload.me : null;
  const profile = payload && payload.profile ? payload.profile : null;
  const messagesRes = payload && payload.messages ? payload.messages : null;
  state.me = me;
  state.activeMailbox = personalMailboxContext();
  if (profile) {
    if (state.me && !state.me.profile_card) {
      state.me.profile_card = profile.profile_card || null;
    }
    state.settings.signature = profile.signature || "";
    state.settings.templates = profile.templates || [];
    renderComposeTemplateOptions();
  }
  state.folders = (payload && payload.folders) || [];
  state.messages = (messagesRes && messagesRes.messages) || [];
  state.totalMessages = (messagesRes && messagesRes.total) || 0;
  renderFolders();
  syncMoveTargets();
  renderMessages();
  setCurrentFolderLabel((getFolderByName(state.currentFolder) || {}).label || state.currentFolder);
}

function bindRovingNavigation(selector, activate) {
  $$(selector).forEach((item) => {
    item.addEventListener("keydown", (event) => {
      const items = [...$$(selector)].filter((candidate) => (
        !candidate.disabled && !candidate.classList.contains("hidden")
      ));
      const index = items.indexOf(item);
      if (index < 0 || !items.length) return;
      let next = index;
      if (event.key === "ArrowRight") next = (index + 1) % items.length;
      else if (event.key === "ArrowLeft") next = (index - 1 + items.length) % items.length;
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = items.length - 1;
      else return;
      event.preventDefault();
      items[next].focus();
      activate(items[next]);
    });
  });
}

function activateWorkspaceNavigation(button) {
  navigateWorkspace(button.dataset.workspace).catch((error) => {
    toast(error.message || "Не удалось открыть раздел", "error");
  });
}

function syncWorkspaceNavigation() {
  $$("[data-workspace]").forEach((button) => {
    const active = button.dataset.workspace === state.ui.workspace;
    button.classList.toggle("active", active);
    button.setAttribute("aria-current", active ? "page" : "false");
    button.setAttribute("tabindex", active ? "0" : "-1");
  });
}

function closeWorkspaceOverlay(name, { restoreFocus = false } = {}) {
  const modalId = LM_WORKSPACES[name] && LM_WORKSPACES[name].overlay;
  if (!modalId) return;
  const modal = document.getElementById(modalId);
  if (!modal) return;
  modal.classList.add("hidden");
  modal.classList.remove("workspace-destination");
  modal.setAttribute("aria-hidden", "true");
  if (restoreFocus) {
    const target = document.querySelector(`[data-workspace="${name}"]`);
    if (target) target.focus();
  }
}

function focusWorkspaceControl(name) {
  const candidates = Array.from(
    document.querySelectorAll(`[data-workspace="${name}"]`),
  );
  const target = candidates.find((candidate) => (
    candidate.isConnected
    && !candidate.classList.contains("hidden")
    && candidate.getClientRects().length
  ));
  if (target) target.focus();
}

function restoreOriginatingWorkspace(trigger) {
  const modal = trigger.closest("[data-workspace-surface]");
  if (!modal || !modal.classList.contains("workspace-destination")) return false;
  const previous = state.ui.previousWorkspace;
  const fallback = previous === "calendar" || previous === "directory"
    ? "focus"
    : previous;
  const destination = fallback || "focus";
  navigateWorkspace(destination)
    .then((resolvedWorkspace) => {
      focusWorkspaceControl(resolvedWorkspace || destination);
    })
    .catch((error) => {
      toast(error.message || "Не удалось вернуться в раздел", "error");
    });
  return true;
}

async function navigateWorkspace(name, options = {}) {
  if (!options.keepCompose) suspendCompose();
  let next = LM_WORKSPACES[name] ? name : "focus";
  if (LM_WORKSPACES[next].personalOnly && isSharedMailboxActive()) next = "mail";
  const requestId = ++state.ui.workspaceRequestId;
  const previous = state.ui.workspace;
  if (
    previous !== next
    && !(LM_WORKSPACES[previous] && LM_WORKSPACES[previous].overlay)
  ) {
    state.ui.previousWorkspace = previous;
  }
  if (LM_WORKSPACES[previous] && LM_WORKSPACES[previous].overlay) {
    closeWorkspaceOverlay(previous);
  }

  state.ui.workspace = next;
  const focus = next === "focus";
  const mail = next === "mail";
  $("#focus-workspace").classList.toggle("hidden", !focus);
  $("#mail-workspace").classList.toggle("hidden", !mail);
  $("#app").classList.remove("show-sidebar", "show-reader");
  syncWorkspaceNavigation();
  setCurrentFolderLabel(
    LM_WORKSPACES[next].label
      || ((getFolderByName(state.currentFolder) || {}).label || state.currentFolder),
  );
  if (focus) {
    await loadFocus({ force: Boolean(options.force) });
    if (requestId !== state.ui.workspaceRequestId || state.ui.workspace !== next) return;
  }
  if (next === "calendar") {
    await openCalendar({ asWorkspace: true });
    if (requestId !== state.ui.workspaceRequestId || state.ui.workspace !== next) return;
  }
  if (next === "directory") {
    await openDirectory({ asWorkspace: true });
    if (requestId !== state.ui.workspaceRequestId || state.ui.workspace !== next) return;
  }
  renderFolders();
  $("#lm-workspace-live").textContent = `${LM_WORKSPACES[next].label} открыт`;
  return next;
}

function showWorkspace(name, options = {}) {
  return navigateWorkspace(name, options).catch((error) => {
    toast(error.message || "Не удалось открыть раздел", "error");
  });
}

async function tryBootstrap() {
  try {
    const auth = await apiFetch("/auth/status");
    if (!auth.authenticated) {
      showLogin();
      return;
    }
    const payload = await apiFetch(`/bootstrap?folder=${encodeURIComponent(state.currentFolder)}&page=${state.page}&per_page=${state.perPage}`);
    applyBootstrapPayload(payload);
    try {
      await loadSharedMailboxes({ enforceActive: false });
    } catch (_) {
      state.sharedMailboxes = [];
    }
    showApp();
    loadRuntimeStatus().catch(() => {});
    startAutoRefresh();
    const openedDeepLink = await handleDeepLink();
    if (!openedDeepLink) showWorkspace("focus");
    await handleAccountReturn();
  } catch (_) {
    showLogin();
  }
}

async function handleAccountReturn() {
  const url = new URL(window.location.href);
  if (url.searchParams.get("account") !== "1") return;
  const linked = url.searchParams.get("linked") || "";
  url.searchParams.delete("account");
  url.searchParams.delete("linked");
  history.replaceState({}, "", `${url.pathname}${url.search}${url.hash}`);
  await openAccount($("#user-profile-button"));
  if (linked) toast("Способ входа подключён.", "success");
}

async function handleDeepLink() {
  try {
    const url = new URL(window.location.href);
    const openUid = url.searchParams.get("open");
    if (!openUid) return false;
    const folder = url.searchParams.get("folder") || "INBOX";
    const action = url.searchParams.get("action") || "view";
    showWorkspace("mail");
    if (folder !== state.currentFolder) {
      state.currentFolder = folder;
      state.ui.sidebarFolderKey = sidebarFolderKeyForName(folder);
      state.page = 1;
      await loadMessages();
    }
    await openMessage(openUid);
    if (action === "reply" || action === "forward") {
      setTimeout(() => {
        if (state.currentMessage) openCompose(action);
      }, 150);
    }
    history.replaceState({}, "", window.location.pathname);
    return true;
  } catch (e) {
    console.error("deep-link error", e);
    return false;
  }
}

function showLogin() {
  clearTelegramAuthFlow();
  $("#login-screen").classList.remove("hidden");
  $("#app").classList.add("hidden");
  renderRuntimeBanner();
  renderLoginRuntimeNotice();
  if (!state.runtime.loaded) {
    loadRuntimeStatus().catch(() => {});
  }
}

function applyLoginPrefill() {
  const url = new URL(window.location.href);
  const { searchParams } = url;
  const login = (searchParams.get("login") || "").trim().toLowerCase();
  if (!login) return;
  $("#login-email").value = login;
  searchParams.delete("login");
  history.replaceState(
    {},
    "",
    `${url.pathname}${url.search}${url.hash}`,
  );
  setTimeout(() => $("#login-password").focus(), 0);
}

function showApp() {
  clearTelegramAuthFlow();
  $("#login-screen").classList.add("hidden");
  $("#app").classList.remove("hidden");
  renderUserProfileAvatar(state.me.profile_card);
  loadAccount().catch(() => {});
  setCurrentFolderLabel("Входящие");
  updateMailboxContextUi();
  syncResponsiveShell();
  renderRuntimeBanner();
}

function clearTelegramAuthFlow() {
  // The Telegram challenge flow is gone: Telegram is a provider of the central
  // broker now (ADR-0043). Kept as a no-op hook so the login/logout paths that
  // called it keep one place to reset transient login state.
  telegramAuthGeneration += 1;
}

function clearLoginFeedback() {
  $("#login-error").textContent = "";
  $("#login-hint").textContent = "";
}

async function completeMailLogin(loginRes) {
  state.me = loginRes;
  const payload = await apiFetch(`/bootstrap?folder=${encodeURIComponent(state.currentFolder)}&page=${state.page}&per_page=${state.perPage}`);
  applyBootstrapPayload(payload);
  try {
    await loadSharedMailboxes({ enforceActive: false });
  } catch (_) {
    state.sharedMailboxes = [];
  }
  showApp();
  showWorkspace("focus");
  loadRuntimeStatus().catch(() => {});
  startAutoRefresh();
}

async function handleLogin(e) {
  e.preventDefault();
  clearTelegramAuthFlow();
  const email = $("#login-email").value.trim();
  const password = $("#login-password").value;
  const remember_me = !!$("#login-remember")?.checked;
  const submitButton = e.submitter || $("#login-form button[type='submit']");
  const submitContent = submitButton
    ? Array.from(submitButton.childNodes, (node) => node.cloneNode(true))
    : [];
  clearLoginFeedback();
  if (submitButton) {
    submitButton.disabled = true;
    submitButton.setAttribute("aria-busy", "true");
    submitButton.textContent = "Проверяем доступ…";
  }
  try {
    const loginRes = await apiFetch("/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password, remember_me }),
    });
    await completeMailLogin(loginRes);
  } catch (err) {
    $("#login-error").textContent = err.message;
    const hint = err?.payload?.hint || "";
    $("#login-hint").textContent = hint;
  } finally {
    if (submitButton) {
      submitButton.disabled = false;
      submitButton.removeAttribute("aria-busy");
      submitButton.replaceChildren(...submitContent);
    }
  }
}

async function handleLogout() {
  if (!(await preserveComposeBeforeContextExit())) return;
  stopAutoRefresh();
  try { await apiFetch("/auth/logout", { method: "POST" }); } catch (_) {}
  state.me = null;
  state.sharedMailboxes = [];
  state.activeMailbox = {
    key: "personal",
    is_shared: false,
    mailbox_id: null,
    email: "",
    from_address: "",
    display_name: "",
    can_send: true,
  };
  state.ui.focus.loadedAt = 0;
  state.ui.focus.errors = {};
  state.ui.sidebarFolderKey = "INBOX";
  showLogin();
}

async function loadFolders() {
  const requestId = ++state.requestSeq.folders;
  const mailboxContext = captureMailboxContext();
  try {
    const res = await apiFetch("/folders", { mailboxContext });
    if (
      requestId !== state.requestSeq.folders
      || !isMailboxContextCurrent(mailboxContext)
    ) return;
    state.folders = res.folders || [];
    renderFolders();
    syncMoveTargets();
  } catch (e) {
    if (
      requestId !== state.requestSeq.folders
      || !isMailboxContextCurrent(mailboxContext)
    ) return;
    toast(e.message, "error");
  }
}

function folderNameMatches(name, aliases) {
  const normalized = String(name || "").toLocaleLowerCase();
  return aliases.some((alias) => alias.toLocaleLowerCase() === normalized);
}

function sidebarFolderKeyForName(name) {
  if (folderNameMatches(name, SIDEBAR_FOLDER_ALIASES.sent)) return "__SENT__";
  if (folderNameMatches(name, SIDEBAR_FOLDER_ALIASES.archive)) return "__ARCHIVE__";
  if (folderNameMatches(name, SIDEBAR_FOLDER_ALIASES.all)) return "__ALL__";
  if (folderNameMatches(name, SIDEBAR_FOLDER_ALIASES.spam)) return "__SPAM__";
  return name;
}

function getSidebarFolderEntries() {
  const used = new Set();
  const pick = (aliases) => {
    const folder = state.folders.find((candidate) => folderNameMatches(candidate.name, aliases));
    if (folder) used.add(folder.name);
    return folder || null;
  };
  const inbox = pick(["INBOX"]) || { name: "INBOX", label: "Входящие", unread: 0 };
  pick(SIDEBAR_FOLDER_ALIASES.outbox);
  const sent = pick(SIDEBAR_FOLDER_ALIASES.sent);
  const archive = pick(SIDEBAR_FOLDER_ALIASES.archive);
  const all = pick(SIDEBAR_FOLDER_ALIASES.all);
  const spam = pick(SIDEBAR_FOLDER_ALIASES.spam);
  return [
    { ...inbox, key: "INBOX" },
    { key: "__OUTBOX__", label: "Исходящие", outbox: true, unread: 0 },
    { ...(sent || { name: "Sent", unread: 0 }), key: "__SENT__", label: "Отправленные", placeholder: !sent },
    { ...(archive || { name: "Archive", unread: 0 }), key: "__ARCHIVE__", label: "Архив", placeholder: !archive },
    // ponytail: providers without All Mail use unfiltered INBOX, add server aggregation only for cross-folder results.
    { ...(all || { name: "INBOX", unread: 0 }), key: "__ALL__", label: "Все", allInbox: !all },
    { ...(spam || { name: "Junk", unread: 0 }), key: "__SPAM__", label: "Спам", placeholder: !spam },
    ...state.folders
      .filter((folder) => !used.has(folder.name))
      .map((folder) => ({ ...folder, key: folder.name })),
  ];
}

function openSidebarFolder(folder) {
  showWorkspace("mail");
  state.ui.sidebarFolderKey = folder.key;
  if (folder.outbox) {
    renderFolders();
    $("#app").classList.remove("show-sidebar", "show-reader");
    openScheduledList();
    return;
  }
  state.currentFolder = folder.name;
  state.page = 1;
  state.selectedUid = null;
  state.currentMessage = null;
  clearSelection();
  if (folder.allInbox) {
    resetMailboxFilters();
    state.ui.priorityFocus = "all";
  }
  renderFolders();
  setCurrentFolderLabel(folder.label || folder.name);
  clearMessageView();
  $("#app").classList.remove("show-sidebar", "show-reader");
  if (folder.placeholder) {
    state.messages = [];
    state.totalMessages = 0;
    renderMessages();
    return;
  }
  loadMessages();
}

function renderFolders() {
  const ul = $("#folder-list");
  ul.innerHTML = "";
  getSidebarFolderEntries().forEach((folder) => {
    const li = document.createElement("li");
    if (state.ui.workspace === "mail" && folder.key === state.ui.sidebarFolderKey) li.classList.add("active");
    li.innerHTML = `<span>${escapeHtml(folder.label)}</span>` +
      (folder.unread > 0 ? `<span class="unread-badge">${folder.unread}</span>` : "");
    li.addEventListener("click", () => openSidebarFolder(folder));
    ul.appendChild(li);
  });
}

function buildMessageQuery() {
  const params = new URLSearchParams({
    folder: state.currentFolder,
    page: String(state.page),
    per_page: String(state.perPage),
  });
  if (state.searchQuery) params.set("q", state.searchQuery);
  if (state.filters.unread) params.set("unread", "true");
  if (state.filters.flagged) params.set("flagged", "true");
  if (state.filters.hasAttachments) params.set("has_attachments", "true");
  return params.toString();
}

async function loadMessages() {
  const requestId = ++state.requestSeq.messages;
  const mailboxContext = captureMailboxContext();
  try {
    $("#search-status").textContent = state.searchQuery ? "Поиск…" : "";
    const res = await apiFetch(
      `/messages?${buildMessageQuery()}`,
      { mailboxContext },
    );
    if (
      requestId !== state.requestSeq.messages
      || !isMailboxContextCurrent(mailboxContext)
    ) return;
    state.messages = res.messages || [];
    state.totalMessages = res.total || 0;
    const visible = new Set(getVisibleUidsFromItems(getVisibleItems(getBaseItems())));
    state.selectedUids = new Set(getSelectedUids().filter((uid) => visible.has(uid)));
    if (state.selectedUid && !visible.has(state.selectedUid)) {
      state.selectedUid = null;
      state.currentMessage = null;
      clearMessageView();
    }
    renderMessages();
    const parts = [];
    if (state.searchQuery) parts.push(`поиск: ${state.totalMessages}`);
    if (state.filters.unread || state.filters.flagged || state.filters.hasAttachments) {
      parts.push("фильтры активны");
    }
    if (isPriorityInboxAvailable() && state.ui.priorityMode) {
      parts.push(`priority: ${state.ui.priorityFocus === "all" ? "all" : state.ui.priorityFocus}`);
    }
    $("#search-status").textContent = parts.join(" · ");
  } catch (e) {
    if (
      requestId !== state.requestSeq.messages
      || !isMailboxContextCurrent(mailboxContext)
    ) return;
    toast(e.message, "error");
  }
}

function setMailToolsOpen(open) {
  const tools = $("#mail-tools");
  const button = $("#btn-mail-tools-toggle");
  if (!tools || !button) return;
  tools.classList.toggle("hidden", !open);
  button.setAttribute("aria-expanded", String(open));
  button.textContent = open ? "Скрыть" : "Фильтры";
}

function renderBulkToolbar() {
  const count = state.selectedUids.size;
  const canWrite = activeMailboxCanWrite();
  if (count > 0) setMailToolsOpen(true);
  $("#bulk-selected-count").textContent = `${count} выбрано`;
  const visibleUids = getVisibleUidsFromItems();
  $("#bulk-select-page").checked = !!visibleUids.length && visibleUids.every((uid) => state.selectedUids.has(uid));
  [
    "#btn-bulk-archive",
    "#btn-bulk-read",
    "#btn-bulk-unread",
    "#btn-bulk-star",
    "#btn-bulk-unstar",
    "#btn-bulk-move",
    "#btn-bulk-delete",
    "#bulk-move-target",
  ].forEach((sel) => { $(sel).disabled = count === 0 || !canWrite; });
}

function syncThreadModeButton() {
  const button = $("#btn-thread-mode");
  if (!button) return;
  button.textContent = `Треды: ${state.threadMode ? "вкл" : "выкл"}`;
  button.classList.toggle("active", state.threadMode);
}

function syncPriorityInboxControls(baseItems) {
  const button = $("#btn-priority-mode");
  const row = $("#priority-row");
  const summary = $("#priority-summary");
  if (!button || !row || !summary) return;
  const available = isPriorityInboxAvailable();
  button.disabled = !available;
  button.textContent = `Priority: ${state.ui.priorityMode ? "вкл" : "выкл"}`;
  button.classList.toggle("active", available && state.ui.priorityMode);
  row.classList.toggle("hidden", !available);
  if (!available) return;
  const stats = buildPrioritySummary(baseItems || getBaseItems());
  const focus = state.ui.priorityFocus || "all";
  summary.textContent = `${stats.priority} важных · ${stats.direct} мне лично · ${stats.unread} новых · ${stats.attachments} с вложениями`;
  $$("[data-priority-focus]").forEach((chip) => {
    chip.classList.toggle("active", state.ui.priorityMode && chip.dataset.priorityFocus === focus);
    chip.disabled = !state.ui.priorityMode;
  });
}

function renderMessages() {
  const list = $("#message-list");
  list.innerHTML = "";
  const baseItems = getBaseItems();
  const visibleItems = getVisibleItems(baseItems);
  const visibleUids = new Set(getVisibleUidsFromItems(visibleItems));
  state.selectedUids = new Set(getSelectedUids().filter((uid) => visibleUids.has(uid)));
  if (state.selectedUid && !visibleUids.has(state.selectedUid)) {
    state.selectedUid = null;
    clearMessageView();
  }
  renderBulkToolbar();
  syncThreadModeButton();
  syncPriorityInboxControls(baseItems);
  if (!visibleItems.length) {
    list.innerHTML = `<div class="empty-state" style="padding:30px;text-align:center">${state.ui.priorityMode && isPriorityInboxAvailable() && state.ui.priorityFocus !== "all" ? "Под выбранный приоритет ничего не попало" : "Писем нет"}</div>`;
  }

  visibleItems.forEach((item) => {
    const group = state.threadMode ? item : null;
    const message = state.threadMode ? group.head : item;
    const preview = state.threadMode ? (group.preview || message) : message;
    if (!message) return;
    const priorityMeta = getItemPriorityMeta(item);
    const unreadCount = state.threadMode ? group.unreadCount : (message.unread ? 1 : 0);
    const threadCount = state.threadMode ? group.messages.length : 1;
    const flagged = state.threadMode ? group.flaggedCount > 0 : message.flagged;
    const isSelected = state.threadMode
      ? !!(state.selectedUid && group.uids.includes(state.selectedUid))
      : message.uid === state.selectedUid;
    const isChecked = state.threadMode && group
      ? group.uids.every((uid) => state.selectedUids.has(uid))
      : state.selectedUids.has(message.uid);
    const hasAnySelected = state.threadMode && group
      ? group.uids.some((uid) => state.selectedUids.has(uid))
      : state.selectedUids.has(message.uid);
    const div = document.createElement("div");
    div.className = "message-item";
    if (unreadCount > 0) div.classList.add("unread");
    if (isSelected) div.classList.add("selected");
    if (hasAnySelected) div.classList.add("multi-selected");
    if (priorityMeta.priority) div.classList.add("priority-item");

    div.innerHTML = `
      <div class="message-shell" data-uid="${escapeHtml(message.uid)}">
        <label class="msg-check">
          <input type="checkbox" class="msg-checkbox" ${isChecked ? "checked" : ""}>
        </label>
        <button class="msg-star ${flagged ? "active" : ""}" title="Звезда">${flagged ? "★" : "☆"}</button>
        <div class="msg-main">
          <div class="msg-row-1">
            <span class="msg-from">${escapeHtml(message.from || "(неизв.)")}</span>
            <div class="msg-row-meta">
              ${priorityMeta.priority ? '<span class="priority-pill hot">Важно</span>' : ""}
              ${priorityMeta.direct ? '<span class="priority-pill direct">Мне</span>' : ""}
              ${priorityMeta.urgent ? '<span class="priority-pill urgent">Срочно</span>' : ""}
              ${priorityMeta.hasAttachments ? '<span class="msg-marker" title="Есть вложения">ATT</span>' : ""}
              ${threadCount > 1 ? `<span class="thread-count-pill">${threadCount}</span>` : ""}
              ${unreadCount > 0 ? `<span class="msg-state-pill">${unreadCount === 1 ? "new" : `${unreadCount} new`}</span>` : ""}
              <span class="msg-date">${escapeHtml(formatDate(message.date))}</span>
            </div>
          </div>
          <div class="msg-row-2">
            <div class="msg-subject">${escapeHtml(message.subject || "(без темы)")}</div>
            <div class="msg-inline-actions">
              <button class="msg-inline-btn" data-action="archive" title="Архивировать">🗄</button>
              <button class="msg-inline-btn" data-action="seen" title="${message.unread ? "Отметить прочитанным" : "Отметить непрочитанным"}">${message.unread ? "✓" : "•"}</button>
              <button class="msg-inline-btn danger" data-action="delete" title="Удалить">🗑</button>
            </div>
          </div>
          <div class="msg-snippet">${escapeHtml((preview && preview.snippet) || message.snippet || "")}</div>
        </div>
      </div>
    `;

    div.addEventListener("click", () => openMessage(message.uid));
    div.querySelector(".msg-checkbox").addEventListener("click", (e) => {
      e.stopPropagation();
      const targetUids = state.threadMode && group ? group.uids : [message.uid];
      targetUids.forEach((targetUid) => {
        if (e.target.checked) state.selectedUids.add(targetUid);
        else state.selectedUids.delete(targetUid);
      });
      renderMessages();
    });
    div.querySelector(".msg-star").addEventListener("click", async (e) => {
      e.stopPropagation();
      if (state.threadMode && group && group.uids.length > 1) {
        await runBulkAction(flagged ? "unstar" : "star", { uids: group.uids });
      } else {
        await updateMessageFlags(message.uid, { flagged: !flagged });
      }
    });
    div.querySelectorAll(".msg-inline-btn").forEach((button) => {
      button.addEventListener("click", async (e) => {
        e.stopPropagation();
        const action = button.dataset.action;
        if (action === "archive") {
          if (state.threadMode && group && group.uids.length > 1) {
            const archiveFolder = getArchiveFolderName();
            if (!archiveFolder) {
              toast("Папка Архив не найдена", "error");
              return;
            }
            await runBulkAction("move", { uids: group.uids, toFolder: archiveFolder, label: `Архивировано: ${group.uids.length}` });
          } else {
            await archiveOne(message.uid);
          }
        } else if (action === "seen") {
          if (state.threadMode && group && group.uids.length > 1) {
            await runBulkAction(unreadCount > 0 ? "mark_read" : "mark_unread", { uids: group.uids });
          } else {
            await updateMessageFlags(message.uid, { seen: !!message.unread });
          }
        } else if (action === "delete") {
          state.selectedUid = message.uid;
          if (state.threadMode && group && group.uids.length > 1) {
            await runBulkAction("delete", { uids: group.uids });
          } else {
            await deleteCurrent();
          }
        }
      });
    });
    list.appendChild(div);
  });

  const totalPages = Math.max(1, Math.ceil(state.totalMessages / state.perPage));
  $("#page-info").textContent = state.threadMode
    ? `стр. ${state.page} / ${totalPages} · ${visibleItems.length} тредов на странице`
    : `стр. ${state.page} / ${totalPages} · ${state.totalMessages} писем`;
  $("#message-count").textContent = isPriorityInboxAvailable() && state.ui.priorityMode
    ? `${visibleItems.length} в фокусе`
    : (state.threadMode ? `${state.totalMessages} писем` : `${state.totalMessages}`);
  $("#prev-page").disabled = state.page <= 1;
  $("#next-page").disabled = state.page >= totalPages;
}

function captureMailListState() {
  const list = $("#message-list");
  if (!list) return;
  const app = $("#app");
  if (
    isMobileLayout()
    && (
      state.ui.readerOpening
      || (app && app.classList.contains("show-reader"))
    )
  ) return;
  state.ui.mailListScrollTop = list.scrollTop;
}

function restoreMailListState() {
  const list = $("#message-list");
  if (list) list.scrollTop = state.ui.mailListScrollTop || 0;
}

function closeMobileReader() {
  state.requestSeq.openMessage += 1;
  state.ui.readerOpening = 0;
  state.selectedUid = null;
  clearMessageView();
  renderMessages();
  $("#app").classList.remove("show-reader");
  requestAnimationFrame(restoreMailListState);
}

async function openMessage(uid) {
  captureMailListState();
  const requestId = ++state.requestSeq.openMessage;
  if (isMobileLayout()) state.ui.readerOpening = requestId;
  const mailboxContext = captureMailboxContext();
  state.selectedUid = uid;
  renderMessages();
  try {
    if (state.threadMode) {
      const group = getCurrentThreadGroup();
      const threadUids = group && group.uids.length ? group.uids.slice() : [uid];
      const cachedMessages = threadUids
        .map((threadUid) => getMessageDetailCache(
          state.currentFolder,
          threadUid,
          mailboxContext.key,
        ))
        .filter(Boolean);
      if (cachedMessages.length) {
        cachedMessages.sort((a, b) => new Date(a.date || 0).getTime() - new Date(b.date || 0).getTime());
        state.currentThreadUids = threadUids;
        state.currentThreadMessages = cachedMessages;
        state.currentMessage = cachedMessages.find((entry) => entry.uid === uid) || cachedMessages[cachedMessages.length - 1] || null;
        renderMessageView(state.currentMessage);
      }
      const threadMessages = await Promise.all(threadUids.map(
        (threadUid) => fetchMessageDetail(
          threadUid,
          state.currentFolder,
          { mailboxContext },
        ),
      ));
      if (
        requestId !== state.requestSeq.openMessage
        || !isMailboxContextCurrent(mailboxContext)
      ) return;
      threadMessages.sort((a, b) => new Date(a.date || 0).getTime() - new Date(b.date || 0).getTime());
      state.currentThreadUids = threadUids;
      state.currentThreadMessages = threadMessages;
      state.currentMessage = threadMessages.find((entry) => entry.uid === uid) || threadMessages[threadMessages.length - 1] || null;
      renderMessageView(state.currentMessage);
    } else {
      const cached = getMessageDetailCache(
        state.currentFolder,
        uid,
        mailboxContext.key,
      );
      if (cached) {
        state.currentThreadUids = [uid];
        state.currentThreadMessages = [];
        state.currentMessage = cached;
        renderMessageView(cached);
      }
      const message = await fetchMessageDetail(
        uid,
        state.currentFolder,
        { mailboxContext },
      );
      if (
        requestId !== state.requestSeq.openMessage
        || !isMailboxContextCurrent(mailboxContext)
      ) return;
      state.currentThreadUids = [uid];
      state.currentThreadMessages = [];
      state.currentMessage = message;
      renderMessageView(message);
    }
    if (isMobileLayout()) $("#app").classList.remove("show-sidebar");
    if (isMobileLayout()) $("#app").classList.add("show-reader");
    const touched = new Set(state.currentThreadUids.length ? state.currentThreadUids : [uid]);
    state.messages.forEach((local) => {
      if (!touched.has(local.uid)) return;
      local.unread = false;
      if (!local.flags.includes("\\Seen")) local.flags.push("\\Seen");
    });
    renderMessages();
  } catch (e) {
    if (
      requestId !== state.requestSeq.openMessage
      || !isMailboxContextCurrent(mailboxContext)
    ) return;
    toast(e.message, "error");
  } finally {
    if (state.ui.readerOpening === requestId) {
      state.ui.readerOpening = 0;
    }
  }
}

function clearMessageView() {
  const view = $("#message-view");
  view.classList.add("empty");
  view.innerHTML = `
    <div class="reader-empty-state">
      <div class="reader-empty-kicker">RuPochta Mail</div>
      <div class="empty-state">Выберите письмо для просмотра</div>
      <div class="muted">Откройте письмо слева или начните новое сообщение.</div>
    </div>
  `;
  state.currentMessage = null;
  state.currentThreadUids = [];
  state.currentThreadMessages = [];
  if (isMobileLayout()) $("#app").classList.remove("show-reader");
}

function resetMailboxFilters() {
  state.searchQuery = "";
  state.filters.unread = false;
  state.filters.flagged = false;
  state.filters.hasAttachments = false;
  $("#search-input").value = "";
  $("#chip-unread").classList.remove("active");
  $("#chip-flagged").classList.remove("active");
  $("#chip-attachments").classList.remove("active");
}

async function openFocusMessage(uid) {
  showWorkspace("mail");
  state.currentFolder = "INBOX";
  state.ui.sidebarFolderKey = "INBOX";
  state.page = 1;
  resetMailboxFilters();
  renderFolders();
  setCurrentFolderLabel((getFolderByName("INBOX") || {}).label || "Входящие");
  await loadMessages();
  await openMessage(uid);
}

async function findReturnedMessage(item) {
  showWorkspace("mail");
  state.currentFolder = "INBOX";
  state.ui.sidebarFolderKey = "INBOX";
  state.page = 1;
  resetMailboxFilters();
  state.searchQuery = [item.subject, item.from_addr].filter(Boolean).join(" ");
  $("#search-input").value = state.searchQuery;
  renderFolders();
  setCurrentFolderLabel((getFolderByName("INBOX") || {}).label || "Входящие");
  await loadMessages();
}

function previewAttachment(url, attachment) {
  const box = $("#attachment-preview");
  if (!box) return;
  const ctype = (attachment.content_type || "").toLowerCase();
  if (ctype.startsWith("image/")) {
    box.innerHTML = `<img src="${escapeHtml(url)}" alt="${escapeHtml(attachment.filename)}">`;
  } else if (ctype === "application/pdf") {
    box.innerHTML = `<iframe src="${escapeHtml(url)}"></iframe>`;
  } else if (ctype.startsWith("text/")) {
    box.innerHTML = `<iframe src="${escapeHtml(url)}"></iframe>`;
  } else {
    box.innerHTML = `<div class="muted">Предпросмотр для этого типа не поддерживается.</div>`;
  }
  box.classList.remove("hidden");
}

function parseAddressesFromHeader(value) {
  return parseEmailList(String(value || ""));
}

function buildReplyAllRecipients(message) {
  const self = getSelfEmails();
  const sender = parseAddressesFromHeader(message.from);
  const direct = parseAddressesFromHeader(message.to);
  const copies = parseAddressesFromHeader(message.cc);
  const to = [];
  const cc = [];
  const seen = new Set();
  sender.forEach((email) => {
    const key = email.toLowerCase();
    if (!self.has(key) && !seen.has(key)) {
      seen.add(key);
      to.push(email);
    }
  });
  [...direct, ...copies].forEach((email) => {
    const key = email.toLowerCase();
    if (!self.has(key) && !seen.has(key)) {
      seen.add(key);
      cc.push(email);
    }
  });
  return { to: to.join(", "), cc: cc.join(", ") };
}

function applyInviteResponseToMessage(message, response) {
  if (!message || !message.invite) return;
  const invite = message.invite;
  delete invite.pending_response;
  invite.response_status = String(response || "").toUpperCase();
  const attendee = findCurrentInviteAttendee(invite);
  if (attendee) attendee.partstat = invite.response_status;
}

async function submitInviteResponse(message, response, folder = "INBOX") {
  return apiFetch(`/messages/${encodeURIComponent(message.uid)}/invite/rsvp`, {
    method: "POST",
    body: JSON.stringify({ folder, response }),
  });
}

async function sendInviteRsvp(response) {
  const message = state.currentMessage;
  if (!message || !message.invite) return;
  const invite = message.invite;
  if (invite.pending_response) return;
  invite.pending_response = response;
  renderMessageView(message);
  try {
    const res = await submitInviteResponse(message, response, state.currentFolder);
    applyInviteResponseToMessage(message, res.response || response);
    renderMessageView(message);
    toast(`RSVP отправлен: ${(inviteResponseMeta(res.response).label || response)}`);
  } catch (err) {
    delete invite.pending_response;
    renderMessageView(message);
    toast(err.message || "Ошибка RSVP", "error");
  }
}

function renderThreadTimeline(messages) {
  if (!messages || messages.length <= 1) return "";
  return `
    <div class="thread-timeline">
      <div class="thread-title">Переписка (${messages.length})</div>
      ${messages.map((entry) => {
        const bodyHtml = entry.body_html ? entry.body_html : `<pre>${escapeHtml(entry.body_text || "")}</pre>`;
        const inviteHtml = renderInviteCard(entry.invite || null, entry);
        return `
          <article class="thread-message-card">
            <div class="thread-message-head">
              <div class="thread-message-from">${escapeHtml(entry.from || "(неизв.)")}</div>
              <div class="thread-message-date">${escapeHtml(formatDate(entry.date))}</div>
            </div>
            <div class="thread-message-meta">
              <span>${escapeHtml(entry.subject || "(без темы)")}</span>
              ${entry.to ? `<span class="muted">Кому: ${escapeHtml(entry.to)}</span>` : ""}
            </div>
            ${inviteHtml}
            <div class="thread-message-body msg-body">${bodyHtml}</div>
          </article>
        `;
      }).join("")}
    </div>
  `;
}

function renderMessageView(message) {
  const view = $("#message-view");
  view.classList.remove("empty");
  const canWrite = activeMailboxCanWrite();
  const bodyHtml = message.body_html ? message.body_html : `<pre>${escapeHtml(message.body_text || "")}</pre>`;
  const inviteHtml = renderInviteCard(message.invite || null, message);
  const local = state.messages.find((m) => m.uid === message.uid);
  const flagged = !!(local && local.flagged);
  const unread = !!(local && local.unread);
  const threadCount = state.currentThreadMessages.length || 1;
  const attachmentsHtml = (message.attachments || []).map((attachment, idx) => {
    const url = contextualApiUrl(`/messages/${encodeURIComponent(message.uid)}/attachment/${attachment.index != null ? attachment.index : idx}?folder=${encodeURIComponent(state.currentFolder)}`);
    const sizeKb = Math.max(1, Math.round((attachment.size || 0) / 1024));
    return `
      <div class="attachment-card">
        <a class="attachment-link" href="${escapeHtml(url)}" download="${escapeHtml(attachment.filename)}">${escapeHtml(attachment.filename)} (${sizeKb} KB)</a>
        <button class="mini-btn attachment-preview-btn" data-url="${escapeHtml(url)}" data-index="${idx}">Preview</button>
      </div>
    `;
  }).join("");

  view.innerHTML = `
    <div class="msg-header">
      <div class="reader-mobile-nav">
        <button class="mini-btn" id="btn-reader-back">← К письмам</button>
      </div>
      <h2>${escapeHtml(message.subject || "(без темы)")}</h2>
      <div class="msg-meta">
        <span class="label">От:</span><span>${escapeHtml(message.from)}</span>
        <span class="label">Кому:</span><span>${escapeHtml(message.to)}</span>
        ${message.cc ? `<span class="label">Копия:</span><span>${escapeHtml(message.cc)}</span>` : ""}
        <span class="label">Дата:</span><span>${escapeHtml(formatDate(message.date))}</span>
      </div>
      ${inviteHtml}
      ${attachmentsHtml ? `<div class="attachment-rail">${attachmentsHtml}</div><div id="attachment-preview" class="attachment-preview hidden"></div>` : ""}
      ${canWrite ? `<div class="msg-actions" id="msg-actions">
          <button class="btn btn-secondary" id="btn-reply">↩ Ответить</button>
          <button class="btn btn-secondary" id="btn-reply-all">↺ Ответить всем</button>
          <button class="btn btn-secondary" id="btn-forward">→ Переслать</button>
          <button class="btn btn-secondary" id="btn-archive">🗄 Архив</button>
          <button class="btn btn-secondary" id="btn-toggle-read">${threadCount > 1 ? "✓ Прочитать тред" : (unread ? "✓ Прочитано" : "↺ Непрочитано")}</button>
          <button class="btn btn-secondary" id="btn-toggle-star">${flagged ? "☆ Убрать звезду" : "★ В звёздные"}</button>
          <button class="btn btn-secondary" id="btn-delete">🗑 Удалить</button>
        </div>` : ""}
    </div>
    ${threadCount > 1 ? renderThreadTimeline(state.currentThreadMessages) : `<div class="msg-body">${bodyHtml}</div>`}
  `;

  if (canWrite) {
    $("#btn-reply").addEventListener("click", () => openCompose("reply"));
    $("#btn-reply-all").addEventListener("click", () => openCompose("reply_all"));
    $("#btn-forward").addEventListener("click", () => openCompose("forward"));
    $("#btn-archive").addEventListener("click", archiveCurrent);
  }
  $("#btn-reader-back").addEventListener("click", () => {
    closeMobileReader();
  });
  $$("[data-invite-rsvp]").forEach((button) => {
    button.addEventListener("click", () => sendInviteRsvp(button.dataset.inviteRsvp));
  });
  if (canWrite) {
    $("#btn-delete").addEventListener("click", deleteCurrent);
    $("#btn-toggle-read").addEventListener("click", () => {
      if (threadCount > 1) {
        const anyUnread = state.messages.some((item) => state.currentThreadUids.includes(item.uid) && item.unread);
        runBulkAction(anyUnread ? "mark_read" : "mark_unread", { uids: state.currentThreadUids });
      } else {
        updateMessageFlags(message.uid, { seen: unread });
      }
    });
    $("#btn-toggle-star").addEventListener("click", () => updateMessageFlags(message.uid, { flagged: !flagged }));
  }
  $$(".attachment-preview-btn").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      const index = Number(btn.dataset.index || "0");
      previewAttachment(btn.dataset.url, message.attachments[index]);
    });
  });
  if (canWrite) attachMoveButton();
}

function attachMoveButton() {
  const actions = $("#msg-actions");
  if (!actions || $("#btn-move")) return;
  const wrap = document.createElement("div");
  wrap.className = "move-dropdown";
  wrap.innerHTML = `
    <button class="btn btn-secondary" id="btn-move">📁 Переместить ▾</button>
    <div class="move-menu hidden" id="move-menu"></div>
  `;
  actions.insertBefore(wrap, $("#btn-delete"));
  const menu = $("#move-menu");
  $("#btn-move").addEventListener("click", (e) => {
    e.stopPropagation();
    populateMoveMenu(menu);
    menu.classList.toggle("hidden");
  });
  setTimeout(() => {
    document.addEventListener("click", () => menu.classList.add("hidden"), { once: true });
  }, 0);
}

function populateMoveMenu(menu) {
  menu.innerHTML = "";
  state.folders.forEach((folder) => {
    if (folder.name === state.currentFolder) return;
    const item = document.createElement("div");
    item.className = "move-item";
    item.textContent = folder.label || folder.name;
    item.addEventListener("click", (e) => {
      e.stopPropagation();
      menu.classList.add("hidden");
      moveCurrent(folder.name);
    });
    menu.appendChild(item);
  });
}

function applyMessagePatch(uid, patch) {
  const local = state.messages.find((m) => m.uid === uid);
  if (local) Object.assign(local, patch);
  if (state.currentMessage && state.currentMessage.uid === uid) {
    renderMessageView(state.currentMessage);
  }
  renderMessages();
}

function rememberUndo(payload) {
  state.undo.payload = payload
    ? { ...payload, mailboxKey: activeMailboxKey() }
    : null;
}

async function undoLastAction() {
  if (!state.undo.payload) return;
  const payload = state.undo.payload;
  state.undo.payload = null;
  if (payload.mailboxKey !== activeMailboxKey()) {
    toast("Undo относится к другому ящику", "error");
    return;
  }
  const { mailboxKey: _mailboxKey, ...undoPayload } = payload;
  try {
    await apiFetch("/messages/undo", {
      method: "POST",
      body: JSON.stringify(undoPayload),
    });
    toast("Действие отменено", "success");
    if (state.currentFolder === payload.from_folder || state.currentFolder === payload.to_folder) {
      state.selectedUid = null;
      clearSelection();
      clearMessageDetailCache();
      await refreshMailboxChrome();
    }
  } catch (e) {
    toast("Undo не удалось: " + e.message, "error");
  }
}

function notifyUndo(label, undo) {
  if (!undo || !undo.message_ids || !undo.message_ids.length) {
    toast(label, "success");
    return;
  }
  rememberUndo(undo);
  toast(label, "success", {
    actionLabel: "Отменить",
    duration: 8000,
    onAction: () => { undoLastAction(); },
  });
}

async function updateMessageFlags(uid, patch) {
  try {
    const res = await apiFetch(`/messages/${encodeURIComponent(uid)}/flags`, {
      method: "POST",
      body: JSON.stringify({ folder: state.currentFolder, ...patch }),
    });
    const message = res.message || {};
    applyMessagePatch(uid, {
      flags: message.flags || [],
      unread: !!message.unread,
      flagged: !!message.flagged,
      has_attachments: !!message.has_attachments,
    });
    await loadFolders();
  } catch (e) {
    toast(e.message, "error");
  }
}

async function moveCurrent(toFolder) {
  if (!state.selectedUid) return;
  try {
    const res = await apiFetch(`/messages/${encodeURIComponent(state.selectedUid)}/move`, {
      method: "POST",
      body: JSON.stringify({ from_folder: state.currentFolder, to_folder: toFolder }),
    });
    notifyUndo(`Перемещено в ${(getFolderByName(toFolder) || {}).label || toFolder}`, res.undo);
    state.selectedUid = null;
    clearMessageView();
    clearMessageDetailCache();
    await refreshMailboxChrome();
  } catch (e) {
    toast(e.message, "error");
  }
}

async function archiveOne(uid) {
  const archiveFolder = getArchiveFolderName();
  if (!archiveFolder) {
    toast("Папка Архив не найдена", "error");
    return;
  }
  if (archiveFolder === state.currentFolder) {
    toast("Письмо уже в архиве");
    return;
  }
  try {
    const res = await apiFetch(`/messages/${encodeURIComponent(uid)}/move`, {
      method: "POST",
      body: JSON.stringify({ from_folder: state.currentFolder, to_folder: archiveFolder }),
    });
    notifyUndo("Архивировано", res.undo);
    if (state.selectedUid === uid) {
      state.selectedUid = null;
      clearMessageView();
    }
    clearSelection();
    clearMessageDetailCache();
    await refreshMailboxChrome();
  } catch (e) {
    toast(e.message, "error");
  }
}

async function archiveCurrent() {
  if (!state.selectedUid) return;
  if (state.threadMode && state.currentThreadUids.length > 1) {
    const archiveFolder = getArchiveFolderName();
    if (!archiveFolder) {
      toast("Папка Архив не найдена", "error");
      return;
    }
    if (archiveFolder === state.currentFolder) {
      toast("Текущая папка уже Архив");
      return;
    }
    await runBulkAction("move", { uids: state.currentThreadUids, toFolder: archiveFolder, label: `Архивирован тред (${state.currentThreadUids.length})` });
    return;
  }
  await archiveOne(state.selectedUid);
}

async function deleteCurrent() {
  if (!state.selectedUid) return;
  if (state.threadMode && state.currentThreadUids.length > 1) {
    if (!confirm(`Переместить тред из ${state.currentThreadUids.length} писем в корзину?`)) return;
    await runBulkAction("delete", { uids: state.currentThreadUids, label: `Удалён тред (${state.currentThreadUids.length})`, skipConfirm: true });
    return;
  }
  if (!confirm("Переместить письмо в корзину?")) return;
  try {
    const res = await apiFetch(`/messages/${encodeURIComponent(state.selectedUid)}?folder=${encodeURIComponent(state.currentFolder)}`, {
      method: "DELETE",
    });
    notifyUndo("Удалено", res.undo);
    state.selectedUid = null;
    clearMessageView();
    clearMessageDetailCache();
    await refreshMailboxChrome();
  } catch (e) {
    toast(e.message, "error");
  }
}

async function runBulkAction(action, options = {}) {
  const uids = (options.uids && options.uids.length ? options.uids : getSelectedUids()).map(String);
  if (!uids.length) return;
  if (action === "delete" && !options.skipConfirm && !confirm(`Переместить ${uids.length} писем в корзину?`)) return;
  try {
    const res = await apiFetch("/messages/bulk", {
      method: "POST",
      body: JSON.stringify({
        from_folder: state.currentFolder,
        uids,
        action,
        to_folder: options.toFolder || null,
      }),
    });
    const actionLabel = options.label || (action === "move"
      ? `Перемещено: ${uids.length}`
      : action === "delete"
        ? `Удалено: ${uids.length}`
        : `Готово: ${uids.length}`);
    notifyUndo(actionLabel, res.undo);
    clearSelection();
    if (action === "move" || action === "delete") {
      clearMessageView();
      state.selectedUid = null;
    }
    if (action === "move" || action === "delete") clearMessageDetailCache();
    await refreshMailboxChrome();
  } catch (e) {
    toast(e.message, "error");
  }
}

function hasBlockingModalOpen() {
  return ["#compose-modal", "#settings-modal", "#account-modal", "#directory-modal"]
    .some((selector) => !$(selector).classList.contains("hidden"));
}

const ACCESSIBLE_MODAL_IDS = new Set(["settings-modal", "account-modal"]);

function openAccessibleModal(id, trigger = null) {
  const modal = $("#" + id);
  if (!modal) return;
  suspendCompose();
  state.ui.modalReturnFocus = trigger || document.activeElement;
  modal.classList.remove("hidden");
  requestAnimationFrame(() => {
    const target = modal.querySelector(
      'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])',
    );
    if (target) target.focus();
  });
}

function closeAccessibleModal(id) {
  const modal = $("#" + id);
  if (!modal) return;
  modal.classList.add("hidden");
  const returnFocus = state.ui.modalReturnFocus;
  state.ui.modalReturnFocus = null;
  if (returnFocus && typeof returnFocus.focus === "function" && returnFocus.isConnected) {
    returnFocus.focus();
  }
}

function handleAccessibleModalKeydown(event) {
  const modal = [...ACCESSIBLE_MODAL_IDS]
    .map((id) => $("#" + id))
    .find((candidate) => candidate && !candidate.classList.contains("hidden"));
  if (!modal || modal.classList.contains("hidden")) return false;
  if (event.key === "Escape") {
    event.preventDefault();
    closeAccessibleModal(modal.id);
    return true;
  }
  if (event.key !== "Tab") return false;
  const focusable = Array.from(modal.querySelectorAll(
    'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
  )).filter((item) => item.getClientRects().length);
  if (!focusable.length) {
    event.preventDefault();
    return true;
  }
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
    return true;
  }
  if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
    return true;
  }
  return false;
}

function shouldHandleGlobalShortcut(event) {
  if (!state.me || $("#app").classList.contains("hidden")) return false;
  if (hasBlockingModalOpen()) return false;
  const target = event.target;
  if (!target) return true;
  const tag = String(target.tagName || "").toLowerCase();
  if (["input", "textarea", "select", "button"].includes(tag)) return false;
  if (target.isContentEditable) return false;
  return true;
}

function selectMessageByOffset(offset) {
  const items = getVisibleItems();
  if (!items.length) return;
  const currentIndex = items.findIndex((item) => {
    if (state.threadMode) return item.uids.includes(state.selectedUid);
    return item.uid === state.selectedUid;
  });
  const nextIndex = currentIndex >= 0
    ? Math.max(0, Math.min(items.length - 1, currentIndex + offset))
    : (offset > 0 ? 0 : items.length - 1);
  state.selectedUid = state.threadMode ? items[nextIndex].head.uid : items[nextIndex].uid;
  renderMessages();
  const selected = document.querySelector(".message-item.selected");
  if (selected) selected.scrollIntoView({ block: "nearest" });
}

async function openSelectedMessage() {
  if (!state.selectedUid) {
    const items = getVisibleItems();
    if (items[0]) state.selectedUid = state.threadMode ? items[0].head.uid : items[0].uid;
    else return;
  }
  await openMessage(state.selectedUid);
}

async function toggleSelectedUnread() {
  if (!state.selectedUid) return;
  if (state.threadMode && state.currentThreadUids.length > 1) {
    const anyUnread = state.messages.some((message) => state.currentThreadUids.includes(message.uid) && message.unread);
    await runBulkAction(anyUnread ? "mark_read" : "mark_unread", { uids: state.currentThreadUids });
    return;
  }
  const local = state.messages.find((message) => message.uid === state.selectedUid);
  const currentUnread = !!(local && local.unread);
  await updateMessageFlags(state.selectedUid, { seen: currentUnread });
}

async function handleGlobalShortcuts(event) {
  if (!shouldHandleGlobalShortcut(event)) return;
  const key = event.key;
  if (key === "j") {
    event.preventDefault();
    selectMessageByOffset(1);
    if (state.currentMessage) await openSelectedMessage();
  } else if (key === "k") {
    event.preventDefault();
    selectMessageByOffset(-1);
    if (state.currentMessage) await openSelectedMessage();
  } else if (key === "Enter") {
    event.preventDefault();
    await openSelectedMessage();
  } else if (key === "r") {
    if (!state.currentMessage) return;
    event.preventDefault();
    openCompose("reply");
  } else if (key === "a") {
    if (!state.currentMessage) return;
    event.preventDefault();
    openCompose("reply_all");
  } else if (key === "f") {
    if (!state.currentMessage) return;
    event.preventDefault();
    openCompose("forward");
  } else if (key === "e") {
    event.preventDefault();
    await archiveCurrent();
  } else if (key === "u") {
    event.preventDefault();
    await toggleSelectedUnread();
  } else if (key === "#" || (event.shiftKey && event.code === "Digit3")) {
    event.preventDefault();
    await deleteCurrent();
  } else if (key === "p") {
    if (!isPriorityInboxAvailable()) return;
    event.preventDefault();
    state.ui.priorityMode = !state.ui.priorityMode;
    if (!state.ui.priorityMode) state.ui.priorityFocus = "all";
    renderMessages();
  }
}

function renderComposeTemplateOptions() {
  const select = $("#compose-template-select");
  if (!select) return;
  select.innerHTML = `<option value="${BRAND_TEMPLATE.id}">RuPochta Mail · по умолчанию</option>`;
  state.settings.templates.forEach((template) => {
    const opt = document.createElement("option");
    opt.value = String(template.id);
    opt.textContent = template.name;
    select.appendChild(opt);
  });
}

function renderComposeAttachments() {
  const box = $("#compose-attachments");
  if (!state.compose.attachments.length) {
    box.innerHTML = "";
    box.classList.add("hidden");
    return;
  }
  box.classList.remove("hidden");
  box.innerHTML = state.compose.attachments.map((file, index) => `
    <div class="compose-attachment-pill ${file.generatedInvite ? "invite-pill" : ""}">
      <span>${file.generatedInvite ? "[ICS] " : ""}${escapeHtml(file.name)} · ${Math.max(1, Math.round(file.size / 1024))} KB</span>
      <button class="icon-btn compose-attachment-remove" data-index="${index}" title="Убрать">×</button>
    </div>
  `).join("");
  $$(".compose-attachment-remove").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      const index = Number(btn.dataset.index || "0");
      const removed = state.compose.attachments[index];
      state.compose.attachments.splice(index, 1);
      if (removed && removed.generatedInvite) {
        $("#compose-invite-enabled").checked = false;
        syncComposeInviteUi();
      }
      renderComposeAttachments();
      queueComposeAutosave();
    });
  });
}

function toggleComposeBcc(force) {
  state.compose.bccVisible = typeof force === "boolean" ? force : !state.compose.bccVisible;
  const show = state.compose.bccVisible || !!$("#compose-bcc").value.trim();
  state.compose.bccVisible = show;
  $("#compose-bcc-row").classList.toggle("hidden", !show);
  $("#btn-show-bcc").textContent = show ? "Скрыть Bcc" : "Показать Bcc";
}

function syncRecipientChips(field) {
  const input = $(`#compose-${field}`);
  const box = $(`#compose-${field}-chips`);
  if (!input || !box) return;
  const addresses = parseEmailList(input.value);
  if (!addresses.length) {
    box.innerHTML = "";
    box.classList.add("hidden");
    return;
  }
  box.classList.remove("hidden");
  box.innerHTML = addresses.map((email, index) => `
    <button type="button" class="recipient-chip" data-field="${field}" data-index="${index}">
      <span>${escapeHtml(email)}</span>
      <span class="recipient-chip-x">×</span>
    </button>
  `).join("");
  box.querySelectorAll(".recipient-chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      const idx = Number(chip.dataset.index || "0");
      const next = addresses.filter((_, current) => current !== idx);
      input.value = next.join(", ");
      syncRecipientChips(field);
      if (field === "bcc") toggleComposeBcc(!!next.length);
      queueComposeAutosave();
      input.focus();
    });
  });
}

function syncAllRecipientChips() {
  ["to", "cc", "bcc"].forEach(syncRecipientChips);
}

// --- Calendar attendee picker -------------------------------------------------
// Builds on top of the /api/contacts directory so participants resolve to the
// same employees as on it.example.com. Maintains calendarState.attendees
// (objects) and keeps the plain input value in sync for saveCalendarEvent.
// L2: _attendeePicking guards the blur/commit path against double-add when a
// mousedown pick races the 180ms blur timeout.
let _attendeePicking = false;
function _attendeeKey(email) {
  return String(email || "").trim().toLowerCase();
}

function _attendeeMatchesContact(contact, email) {
  return _attendeeKey(contact.email) === _attendeeKey(email);
}

function _attendeeSubtitle(contact) {
  return [contact.title, contact.department, contact.company]
    .map((s) => String(s || "").trim())
    .filter(Boolean)
    .join(" · ");
}

function _attendeeInitials(text) {
  const parts = String(text || "").split(/\s+/).filter(Boolean);
  if (!parts.length) return "?";
  return parts.slice(0, 2).map((p) => p[0]).join("").toUpperCase();
}

function renderAttendeeChips() {
  const box = $("#cal-ev-attendees-chips");
  const input = $("#cal-ev-attendees");
  if (!box || !input) return;
  const attendees = calendarState.attendees || [];
  if (!attendees.length) {
    box.innerHTML = "";
    box.classList.add("hidden");
    return;
  }
  box.classList.remove("hidden");
  box.innerHTML = attendees.map((a, idx) => {
    const label = a.name ? `${escapeHtml(a.name)} <span class="att-chip-email">${escapeHtml(a.email)}</span>` : escapeHtml(a.email);
    return `
      <span class="recipient-chip att-chip" data-idx="${idx}" title="${escapeHtml(a.email)}">
        <span class="att-chip-avatar" aria-hidden="true">${escapeHtml(_attendeeInitials(a.name || a.email))}</span>
        <span class="att-chip-label">${label}</span>
        <button type="button" class="recipient-chip-x att-chip-x" aria-label="Убрать участника">×</button>
      </span>`;
  }).join("");
  box.querySelectorAll(".att-chip").forEach((chip) => {
    chip.querySelector(".att-chip-x").addEventListener("click", (e) => {
      e.stopPropagation();
      const idx = Number(chip.dataset.idx || "0");
      calendarState.attendees = calendarState.attendees.filter((_, i) => i !== idx);
      renderAttendeeChips();
      syncAttendeeInput();
      input.focus();
    });
  });
  syncAttendeeInput();
}

function syncAttendeeInput() {
  const input = $("#cal-ev-attendees");
  if (!input) return;
  input.value = (calendarState.attendees || [])
    .map((a) => a.email)
    .filter(Boolean)
    .join(", ");
}

function setAttendeesFromEvent(attendees) {
  calendarState.attendees = (attendees || [])
    .map((a) => {
      const email = typeof a === "string" ? a : a.email;
      if (!email) return null;
      return { email: String(email).trim(), name: (a && a.name) || "", department: "", title: "", company: "" };
    })
    .filter(Boolean);
  renderAttendeeChips();
}

function addAttendee(contact) {
  const email = String(contact.email || "").trim();
  if (!email) return;
  const key = _attendeeKey(email);
  if ((calendarState.attendees || []).some((a) => _attendeeKey(a.email) === key)) return;
  calendarState.attendees.push({
    email,
    name: contact.name || "",
    department: contact.department || "",
    title: contact.title || "",
    company: contact.company || "",
  });
  renderAttendeeChips();
}

function _renderAttendeeSuggestions(listEl, contacts, activeIdx, currentTerm) {
  if (!contacts.length) {
    listEl.classList.add("hidden");
    listEl.innerHTML = "";
    return;
  }
  listEl.innerHTML = "";
  contacts.forEach((contact, idx) => {
    const item = document.createElement("div");
    item.className = "autocomplete-item att-item" + (idx === activeIdx ? " active" : "");
    item.dataset.idx = String(idx);
    const sub = _attendeeSubtitle(contact);
    // L1: only allow data:image/ avatars — defends against javascript:/data:text/
    // injection if avatar_data_url ever becomes attacker-influenced.
    const avatarUrl = String(contact.avatar_data_url || "");
    const hasAvatar = avatarUrl.startsWith("data:image/");
    const avatar = hasAvatar
      ? `<img class="att-item-avatar" src="${escapeHtml(avatarUrl)}" alt="">`
      : `<span class="att-item-avatar" aria-hidden="true">${escapeHtml(_attendeeInitials(contact.name || contact.email))}</span>`;
    const emailLine = escapeHtml(contact.email);
    item.innerHTML = `
      ${avatar}
      <div class="att-item-body">
        <div class="ac-name">${escapeHtml(contact.name || contact.email)}${sub ? " <span class=\"att-item-sub\">" + escapeHtml(sub) + "</span>" : ""}</div>
        <div class="ac-email">${emailLine}</div>
      </div>`;
    item.addEventListener("mousedown", (e) => {
      e.preventDefault();
      _pickAttendee(contact);
    });
    listEl.appendChild(item);
  });
  listEl.classList.remove("hidden");
}

function _pickAttendee(contact) {
  _attendeePicking = true;
  addAttendee(contact);
  const input = $("#cal-ev-attendees");
  const list = $("#ac-cal-attendees");
  if (input) { input.value = ""; input.focus(); }
  if (list) list.classList.add("hidden");
  _attendeePicking = false;
}

function wireAttendeePicker() {
  const input = $("#cal-ev-attendees");
  const list = $("#ac-cal-attendees");
  if (!input || !list) return;
  let timer = null;
  let lastContacts = [];
  let activeIdx = -1;

  function close() {
    list.classList.add("hidden");
    activeIdx = -1;
  }

  async function runSearch(term) {
    if (term.length < 2) { close(); return; }
    try {
      const res = await apiFetch(`/contacts?q=${encodeURIComponent(term)}`);
      lastContacts = res.contacts || [];
      activeIdx = lastContacts.length ? 0 : -1;
      _renderAttendeeSuggestions(list, lastContacts, activeIdx, term);
    } catch (_) {
      close();
    }
  }

  function commitTypedInput() {
    // Commit any free-text email the user typed but did not pick from dropdown.
    const raw = input.value.trim();
    input.value = "";
    if (!raw) { close(); return; }
    const emails = parseEmailList(raw);
    emails.forEach((email) => {
      const existing = lastContacts.find((c) => _attendeeMatchesContact(c, email));
      addAttendee(existing ? {
        email,
        name: existing.name || "",
        department: existing.department || "",
        title: existing.title || "",
        company: existing.company || "",
      } : { email, name: "", department: "", title: "", company: "" });
    });
    close();
  }

  input.addEventListener("input", () => {
    clearTimeout(timer);
    // If the user keeps typing after a comma, commit the segment as typed email.
    const value = input.value;
    if (/[;,]/.test(value)) {
      commitTypedInput();
      return;
    }
    const term = value.trim();
    if (term.length < 2) { close(); return; }
    timer = setTimeout(() => runSearch(term), 200);
  });

  input.addEventListener("keydown", (e) => {
    const open = !list.classList.contains("hidden");
    if (e.key === "ArrowDown" && open) {
      e.preventDefault();
      activeIdx = Math.min(activeIdx + 1, lastContacts.length - 1);
      _renderAttendeeSuggestions(list, lastContacts, activeIdx, input.value.trim());
    } else if (e.key === "ArrowUp" && open) {
      e.preventDefault();
      activeIdx = Math.max(activeIdx - 1, 0);
      _renderAttendeeSuggestions(list, lastContacts, activeIdx, input.value.trim());
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (open && activeIdx >= 0 && lastContacts[activeIdx]) {
        _pickAttendee(lastContacts[activeIdx]);
      } else if (input.value.trim()) {
        commitTypedInput();
      }
    } else if (e.key === "Escape") {
      if (open) { e.preventDefault(); close(); }
    } else if (e.key === "Backspace" && input.value === "" && (calendarState.attendees || []).length) {
      calendarState.attendees.pop();
      renderAttendeeChips();
    }
  });

  input.addEventListener("blur", () => {
    setTimeout(() => {
      // L2: if a mousedown pick is in flight (fired during the blur window),
      // skip the trailing commit — the pick already cleared the input.
      if (_attendeePicking) { close(); return; }
      // Commit any trailing typed email when the field loses focus.
      if (input.value.trim()) commitTypedInput();
      close();
    }, 180);
  });
}

function showComposeRecovery(show) {
  $("#compose-recovery").classList.toggle("hidden", !show);
}

function populateComposeFields(values) {
  $("#compose-to").value = values.to || "";
  $("#compose-cc").value = values.cc || "";
  $("#compose-bcc").value = values.bcc || "";
  $("#compose-subject").value = values.subject || "";
  setComposeBody(values.body || "", !!values.bodyIsHtml);
  $("#compose-invite-enabled").checked = !!values.inviteEnabled;
  $("#compose-invite-start").value = values.inviteStart || "";
  $("#compose-invite-end").value = values.inviteEnd || "";
  $("#compose-invite-location").value = values.inviteLocation || "";
  $("#compose-invite-description").value = values.inviteDescription || "";
  state.compose.draftUid = values.draftUid || null;
  state.compose.bccVisible = !!(values.bccVisible || values.bcc);
  toggleComposeBcc(state.compose.bccVisible);
  syncAllRecipientChips();
  syncComposeInviteUi();
}

function removeGeneratedInviteAttachment() {
  state.compose.attachments = state.compose.attachments.filter((file) => !file.generatedInvite);
}

function composeInviteValues() {
  return {
    enabled: $("#compose-invite-enabled").checked,
    start: $("#compose-invite-start").value,
    end: $("#compose-invite-end").value,
    location: $("#compose-invite-location").value.trim(),
    description: $("#compose-invite-description").value.trim(),
  };
}

function parseEmailList(value) {
  return String(value || "")
    .split(/[,;]/)
    .map((item) => {
      const raw = item.trim();
      if (!raw) return "";
      const match = raw.match(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/i);
      return match ? match[0] : (raw.includes("@") ? raw : "");
    })
    .filter(Boolean);
}

function inviteUtcStamp(raw) {
  const d = new Date(raw);
  if (isNaN(d.getTime())) return "";
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getUTCFullYear()}${pad(d.getUTCMonth() + 1)}${pad(d.getUTCDate())}T${pad(d.getUTCHours())}${pad(d.getUTCMinutes())}${pad(d.getUTCSeconds())}Z`;
}

function localDateTimeValue(date) {
  const d = new Date(date);
  if (isNaN(d.getTime())) return "";
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function inviteEscape(text) {
  return String(text || "")
    .replace(/\\/g, "\\\\")
    .replace(/\r?\n/g, "\\n")
    .replace(/,/g, "\\,")
    .replace(/;/g, "\\;");
}

function syncComposeInviteUi() {
  const enabled = $("#compose-invite-enabled").checked;
  $("#compose-invite-fields").classList.toggle("hidden", !enabled);
  if (!enabled) {
    removeGeneratedInviteAttachment();
    renderComposeAttachments();
    $("#compose-invite-status").textContent = "";
    return;
  }
  const subject = $("#compose-subject").value.trim() || "Meeting";
  const start = $("#compose-invite-start").value;
  const end = $("#compose-invite-end").value;
  $("#compose-invite-status").textContent = start
    ? `Будет создан .ics invite для "${subject}"${end ? "" : " (если конец пустой, поставим +30 минут)"}.`
    : "Укажите хотя бы начало встречи.";
}

function autoAdjustInviteEnd() {
  const startRaw = $("#compose-invite-start").value;
  if (!startRaw) return;
  const endInput = $("#compose-invite-end");
  const start = new Date(startRaw);
  const end = endInput.value ? new Date(endInput.value) : null;
  if (!end || isNaN(end.getTime()) || end.getTime() <= start.getTime()) {
    endInput.value = localDateTimeValue(start.getTime() + 30 * 60000);
  }
}

function buildGeneratedInviteFile() {
  const invite = composeInviteValues();
  if (!invite.enabled) return { ok: true, file: null };
  if (!invite.start) return { ok: false, error: "Укажите начало встречи" };
  autoAdjustInviteEnd();
  const startRaw = $("#compose-invite-start").value;
  const endRaw = $("#compose-invite-end").value;
  const startStamp = inviteUtcStamp(startRaw);
  const endStamp = inviteUtcStamp(endRaw);
  if (!startStamp || !endStamp) return { ok: false, error: "Некорректные дата или время invite" };
  const startDate = new Date(startRaw);
  const endDate = new Date(endRaw);
  if (endDate.getTime() <= startDate.getTime()) return { ok: false, error: "Окончание invite должно быть позже начала" };
  const attendees = Array.from(new Set([
    ...parseEmailList($("#compose-to").value),
    ...parseEmailList($("#compose-cc").value),
  ]));
  const summary = $("#compose-subject").value.trim() || "Meeting";
  const description = invite.description || composeBodyText();
  const organizerName = (state.activeMailbox && state.activeMailbox.display_name)
    || (state.activeMailbox && state.activeMailbox.from_address)
    || "Organizer";
  const organizerEmail = (state.activeMailbox && state.activeMailbox.from_address) || "";
  const uid = `${Date.now()}-${Math.random().toString(16).slice(2)}@example.com`;
  const lines = [
    "BEGIN:VCALENDAR",
    "PRODID:-//RuPochta//RuPochta Mail//RU",
    "VERSION:2.0",
    "CALSCALE:GREGORIAN",
    "METHOD:REQUEST",
    "BEGIN:VEVENT",
    `UID:${uid}`,
    `DTSTAMP:${inviteUtcStamp(new Date().toISOString())}`,
    `DTSTART:${startStamp}`,
    `DTEND:${endStamp}`,
    `SUMMARY:${inviteEscape(summary)}`,
    `LOCATION:${inviteEscape(invite.location)}`,
    `DESCRIPTION:${inviteEscape(description)}`,
    `ORGANIZER;CN=${inviteEscape(organizerName)}:mailto:${organizerEmail}`,
    ...attendees.map((email) => `ATTENDEE;CN=${inviteEscape(email)};ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:${email}`),
    "END:VEVENT",
    "END:VCALENDAR",
    "",
  ];
  const blob = new Blob([lines.join("\r\n")], { type: "text/calendar;charset=utf-8" });
  const safeBase = summary.toLowerCase().replace(/[^a-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 32) || "invite";
  const file = new File([blob], `${safeBase || "invite"}.ics`, { type: "text/calendar" });
  file.generatedInvite = true;
  return { ok: true, file };
}

function ensureGeneratedInviteAttachment() {
  removeGeneratedInviteAttachment();
  const result = buildGeneratedInviteFile();
  if (!result.ok) {
    $("#compose-invite-status").textContent = result.error;
    setComposeStatus(result.error, true);
    renderComposeAttachments();
    return false;
  }
  if (result.file) state.compose.attachments.push(result.file);
  renderComposeAttachments();
  syncComposeInviteUi();
  return true;
}

function mountComposeSurface() {
  const compose = $("#compose-modal");
  const workspace = $("#mail-workspace");
  if (compose && workspace && compose.parentElement !== workspace) workspace.appendChild(compose);
}

function setComposeOpen(open) {
  const compose = $("#compose-modal");
  compose.classList.toggle("hidden", !open);
  if (open) {
    showWorkspace("mail", { keepCompose: true });
    $("#app").classList.remove("show-sidebar", "show-reader");
  } else {
    syncResponsiveShell();
  }
}

function suspendCompose() {
  const compose = $("#compose-modal");
  if (!compose || compose.classList.contains("hidden")) return;
  state.compose.suspended = true;
  setComposeOpen(false);
}

function closeComposeSession() {
  state.compose.suspended = false;
  setComposeOpen(false);
}

function openOrResumeCompose() {
  if (!state.compose.suspended) {
    openCompose("new");
    return;
  }
  state.compose.suspended = false;
  setComposeOpen(true);
  requestAnimationFrame(() => $("#compose-to").focus());
}

async function preserveComposeBeforeContextExit() {
  const compose = $("#compose-modal");
  if (!state.compose.suspended && (!compose || compose.classList.contains("hidden"))) return true;
  if (state.compose.attachments.length && !(await saveDraft(true))) {
    state.compose.suspended = false;
    setComposeOpen(true);
    toast("Не удалось сохранить вложения. Переход отменён.", "error");
    return false;
  }
  suspendCompose();
  return true;
}

function openCompose(mode) {
  if (isSharedMailboxActive() && !state.activeMailbox.can_send) {
    toast("Отправка из этого общего ящика запрещена", "error");
    return;
  }
  invalidateComposeSession();
  state.compose.mode = mode;
  state.compose.inReplyTo = state.currentMessage ? state.currentMessage.message_id : null;
  state.compose.bccVisible = false;
  renderComposeAttachments();
  showComposeRecovery(false);
  renderComposeRuntimeWarning();
  setComposeOpen(true);
  $("#compose-from").textContent = state.activeMailbox.from_address || state.activeMailbox.email;
  $("#compose-status").textContent = "";
  toggleComposeBcc(false);

  let initial = { to: "", cc: "", bcc: "", subject: "", body: "", bccVisible: false };
  if (mode === "reply" && state.currentMessage) {
    const msg = state.currentMessage;
    initial = {
      to: msg.from || "",
      cc: "",
      bcc: "",
      subject: msg.subject && !/^re:/i.test(msg.subject) ? "Re: " + msg.subject : (msg.subject || ""),
      body: buildReplyBody(msg),
    };
    $("#compose-title").textContent = "Ответ";
  } else if (mode === "reply_all" && state.currentMessage) {
    const msg = state.currentMessage;
    const recipients = buildReplyAllRecipients(msg);
    initial = {
      to: recipients.to,
      cc: recipients.cc,
      bcc: "",
      subject: msg.subject && !/^re:/i.test(msg.subject) ? "Re: " + msg.subject : (msg.subject || ""),
      body: buildReplyBody(msg),
    };
    $("#compose-title").textContent = "Ответить всем";
  } else if (mode === "forward" && state.currentMessage) {
    const msg = state.currentMessage;
    initial = {
      to: "",
      cc: "",
      bcc: "",
      subject: msg.subject && !/^fwd:/i.test(msg.subject) ? "Fwd: " + msg.subject : (msg.subject || ""),
      body: buildForwardBody(msg),
    };
    $("#compose-title").textContent = "Пересылка";
  } else {
    initial.body = buildNewBody();
    $("#compose-title").textContent = "Новое письмо";
  }

  populateComposeFields(initial);
  $("#compose-template-select").value = BRAND_TEMPLATE.id;
  const stored = loadStoredComposeSnapshot();
  state.compose.recoveryDraft = stored;
  if (mode === "new" && stored && (stored.to || stored.cc || stored.bcc || stored.subject || stored.body)) {
    showComposeRecovery(true);
  }
  requestAnimationFrame(() => $("#compose-to").focus());
}

function restoreComposeDraft() {
  if (!state.compose.recoveryDraft) return;
  populateComposeFields(state.compose.recoveryDraft);
  showComposeRecovery(false);
  queueComposeAutosave();
}

function discardComposeDraft() {
  clearStoredComposeSnapshot();
  state.compose.recoveryDraft = null;
  showComposeRecovery(false);
}

async function saveDraft(manual = false) {
  const mailboxContext = captureMailboxContext();
  const composeGeneration = state.compose.generation;
  const snapshot = getComposeSnapshot();
  if (!snapshot.to && !snapshot.cc && !snapshot.bcc && !snapshot.subject && !snapshot.body) {
    setComposeStatus("");
    return null;
  }
  if (manual && !ensureGeneratedInviteAttachment()) return null;
  const form = new FormData();
  form.append("to", snapshot.to);
  form.append("cc", snapshot.cc);
  form.append("bcc", snapshot.bcc);
  form.append("subject", snapshot.subject);
  appendComposeBody(form, snapshot.body, snapshot.bodyText);
  if (state.compose.inReplyTo) form.append("in_reply_to", state.compose.inReplyTo);
  if (state.compose.draftUid) form.append("replace_uid", state.compose.draftUid);
  if (manual) {
    state.compose.attachments.forEach((file) => form.append("attachment", file, file.name));
  }
  try {
    setComposeStatus(manual ? "Сохраняю черновик…" : "Автосохранение…");
    const res = await apiFetch("/messages/draft", {
      method: "POST",
      body: form,
      mailboxContext,
    });
    if (
      composeGeneration !== state.compose.generation
      || !isMailboxContextCurrent(mailboxContext)
    ) return null;
    state.compose.draftUid = res.uid || state.compose.draftUid;
    persistComposeSnapshot();
    setComposeStatus(manual ? "Черновик сохранён" : `Автосохранено ${new Date().toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })}`);
    return res;
  } catch (e) {
    if (
      composeGeneration !== state.compose.generation
      || !isMailboxContextCurrent(mailboxContext)
    ) return null;
    setComposeStatus(`Ошибка черновика: ${e.message}`, true);
    return null;
  }
}

function queueComposeAutosave() {
  clearTimeout(state.compose.autosaveTimer);
  persistComposeSnapshot();
  state.compose.autosaveTimer = setTimeout(() => saveDraft(false), 2000);
}

async function sendCompose() {
  const to = $("#compose-to").value.trim();
  const bcc = $("#compose-bcc").value.trim();
  if (!to) {
    setComposeStatus("Укажите получателя", true);
    return;
  }
  if (!ensureGeneratedInviteAttachment()) return;
  const form = new FormData();
  form.append("to", to);
  form.append("cc", $("#compose-cc").value.trim());
  form.append("bcc", bcc);
  form.append("subject", $("#compose-subject").value.trim());
  appendComposeBody(form);
  if (state.compose.inReplyTo) form.append("in_reply_to", state.compose.inReplyTo);
  if (state.compose.draftUid) form.append("draft_uid", state.compose.draftUid);
  state.compose.attachments.forEach((file) => form.append("attachment", file, file.name));
  try {
    setComposeStatus("Отправка…");
    const res = await apiFetch("/messages/send", { method: "POST", body: form });
    clearTimeout(state.compose.autosaveTimer);
    clearStoredComposeSnapshot();
    state.compose.draftUid = null;
    state.compose.attachments = [];
    renderComposeAttachments();
    closeComposeSession();
    const sentSaved = !!res.sent_copy_ok;
    const sentFolder = res.sent_folder || "Sent";
    const warnings = Array.isArray(res.warnings) ? res.warnings.filter(Boolean) : [];
    const summary = sentSaved
      ? `Письмо отправлено · копия сохранена в «${sentFolder}»`
      : "Письмо отправлено, но копия в «Отправленные» не сохранена";
    toast(
      warnings.length ? `${summary}. ${warnings.join(". ")}` : summary,
      warnings.length ? "error" : "success",
    );
    setComposeStatus(summary, !sentSaved);
    await loadFolders();
    loadScheduledBadge().catch(() => {});
  } catch (e) {
    const queuedFailedSend = !!(e.payload && e.payload.queued_failed_send && e.payload.queue_id);
    if (queuedFailedSend) {
      const summary = "SMTP не ответил. Письмо сохранено в Outbox для повторной отправки.";
      toast(summary, "error");
      setComposeStatus(summary, true);
      loadScheduledBadge().catch(() => {});
      return;
    }
    setComposeStatus("Ошибка: " + e.message, true);
  }
}

function applySelectedTemplateToCompose() {
  const selected = $("#compose-template-select").value;
  if (selected === BRAND_TEMPLATE.id) {
    setComposeBody(buildNewBody());
    queueComposeAutosave();
    return;
  }
  const id = Number(selected || "0");
  const template = state.settings.templates.find((item) => item.id === id);
  if (!template) return;
  if (!$("#compose-subject").value.trim()) $("#compose-subject").value = template.subject || "";
  const currentBody = composeBodyText();
  setComposeBody(currentBody ? `${currentBody}\n\n${template.body || ""}` : (template.body || ""));
  queueComposeAutosave();
}

function switchSettingsTab(tab) {
  state.ui.settingsTab = tab;
  $$(".settings-tab").forEach((item) => {
    const active = item.dataset.settingsTab === tab;
    item.classList.toggle("active", active);
    item.setAttribute("aria-selected", active ? "true" : "false");
    item.setAttribute("tabindex", active ? "0" : "-1");
    if (active) item.scrollIntoView({ block: "nearest", inline: "nearest" });
  });
  $$("[data-settings-panel]").forEach((item) => item.classList.toggle("hidden", item.dataset.settingsPanel !== tab));
}

function handleSettingsTabKeydown(event) {
  const tabs = Array.from($$(".settings-tab"));
  const current = tabs.indexOf(event.currentTarget);
  if (current < 0) return;
  let next = current;
  if (event.key === "ArrowRight") next = (current + 1) % tabs.length;
  else if (event.key === "ArrowLeft") next = (current - 1 + tabs.length) % tabs.length;
  else if (event.key === "Home") next = 0;
  else if (event.key === "End") next = tabs.length - 1;
  else return;
  event.preventDefault();
  switchSettingsTab(tabs[next].dataset.settingsTab);
  tabs[next].focus();
}

function renderSettings() {
  applyAppearance(state.settings.appearance, { skipPersist: true });
  applyTheme(state.settings.theme, { skipPersist: true });
  $("#signature-input").value = state.settings.signature || "";
  $("#settings-email-address").textContent = state.me?.from_address || state.me?.email || "—";
  $("#theme-status").textContent = state.settings.theme === "light"
    ? "Светлая тема включена"
    : "Тёмная тема включена";
  $("#appearance-status").textContent = state.settings.appearance === "classic"
    ? "Классическое оформление включено"
    : "Рабочая очередь включена";
  renderForwardingSettings();
  renderAutoReplySettings();
  renderNotificationSettings();
  renderSettingsFolders();
  renderSieveRules();
  renderTemplateList();
  switchSettingsTab(state.ui.settingsTab || "identity");
}

function syncForwardingControls() {
  const enabled = $("#forwarding-enabled").checked;
  const ready = state.ui.forwardingLoaded && !state.ui.settingsLoading;
  $("#forwarding-enabled").disabled = !ready;
  $("#forwarding-save").disabled = !ready;
  $("#forwarding-address").disabled = !ready || !enabled;
  $("#forwarding-address").required = enabled;
  $("#forwarding-keep-copy").disabled = !ready || !enabled;
}

function renderForwardingSettings(forwarding = state.settings.forwarding) {
  state.settings.forwarding = {
    enabled: false,
    address: "",
    keep_copy: true,
    active: false,
    sync_message: "",
    ...(forwarding || {}),
  };
  const current = state.settings.forwarding;
  $("#forwarding-enabled").checked = Boolean(current.enabled);
  $("#forwarding-address").value = current.address || "";
  $("#forwarding-keep-copy").checked = current.keep_copy !== false;
  syncForwardingControls();
  const status = $("#forwarding-status");
  status.textContent = current.enabled
    ? (current.active
      ? "Пересылка включена"
      : "Настройка сохранена, но пока не применена на почтовом сервере")
    : "Пересылка отключена";
  status.classList.toggle("warning", Boolean(current.enabled && !current.active));
}

function syncAutoReplyControls() {
  const enabled = $("#autoreply-enabled").checked;
  const ready = state.ui.autoreplyLoaded && !state.ui.settingsLoading;
  $("#autoreply-enabled").disabled = !ready;
  $("#autoreply-save").disabled = !ready;
  for (const selector of [
    "#autoreply-start-date",
    "#autoreply-end-date",
    "#autoreply-subject",
    "#autoreply-body",
    "#autoreply-repeat-days",
  ]) {
    $(selector).disabled = !ready || !enabled;
  }
  $("#autoreply-subject").required = enabled;
  $("#autoreply-body").required = enabled;
}

function renderAutoReplySettings(autoreply = state.settings.autoreply) {
  state.settings.autoreply = {
    enabled: false,
    subject: "",
    body: "",
    start_date: null,
    end_date: null,
    repeat_days: 1,
    active: false,
    effective: false,
    sync_message: "",
    ...(autoreply || {}),
  };
  const current = state.settings.autoreply;
  $("#autoreply-enabled").checked = Boolean(current.enabled);
  $("#autoreply-start-date").value = current.start_date || "";
  $("#autoreply-end-date").value = current.end_date || "";
  $("#autoreply-subject").value = current.subject || "";
  $("#autoreply-body").value = current.body || "";
  $("#autoreply-repeat-days").value = String(current.repeat_days || 1);
  syncAutoReplyControls();
  const status = $("#autoreply-status");
  if (!current.enabled) {
    status.textContent = "Автоответчик отключен";
  } else if (!current.active) {
    status.textContent = "Настройка сохранена, но пока не применена на почтовом сервере";
  } else if (current.effective) {
    status.textContent = "Автоответчик включён";
  } else {
    status.textContent = "Автоответчик сохранён и включится по расписанию";
  }
  status.classList.toggle("warning", Boolean(current.enabled && !current.active));
}

function renderNotificationSettings() {
  const current = state.settings.notifications || {};
  const pushSupported = "serviceWorker" in navigator && "PushManager" in window;
  const ready = state.ui.notificationsSettingsLoaded && !state.ui.settingsLoading;
  $("#notification-push-toggle").checked = Boolean(current.push_enabled);
  $("#notification-push-toggle").disabled = !ready || !pushSupported;
  $("#notification-push-test").disabled = !ready || !current.push_enabled;
  $("#notification-push-status").textContent = pushSupported
    ? (current.push_enabled
      ? `Включено на устройствах: ${current.push_subscriptions || 1}`
      : "Выключено")
    : "Этот браузер не поддерживает Push";

  $("#notification-telegram-toggle").checked = Boolean(current.telegram_enabled);
  $("#notification-telegram-toggle").disabled = !ready || !current.telegram_linked;
  if (current.telegram_linked) {
    const username = current.telegram_username ? ` (@${current.telegram_username})` : "";
    $("#notification-telegram-status").textContent = current.telegram_enabled
      ? `Включено${username}`
      : `Привязано${username}, уведомления выключены`;
  } else {
    $("#notification-telegram-status").textContent = current.telegram_error
      ? "Состояние Telegram временно недоступно"
      : "Сначала привяжите Telegram в профиле RuPochta";
  }
}

async function reloadNotificationSettings() {
  const value = await apiFetch("/settings/notifications");
  state.settings.notifications = {
    ...state.settings.notifications,
    ...value,
    telegram_error: value.telegram_error || "",
  };
  renderNotificationSettings();
  return value;
}

async function reloadForwardingSettings() {
  const value = await apiFetch("/settings/forwarding");
  renderForwardingSettings(value);
  return value;
}

async function reloadAutoReplySettings() {
  const value = await apiFetch("/settings/autoreply");
  renderAutoReplySettings(value);
  return value;
}

async function reloadSignatureSettings() {
  const value = await apiFetch("/settings/signature");
  state.settings.signature = value.signature || "";
  $("#signature-input").value = state.settings.signature;
  return value;
}

async function reloadTemplateSettings() {
  const value = await apiFetch("/settings/templates");
  state.settings.templates = value.templates || [];
  renderTemplateList();
  renderComposeTemplateOptions();
  return value;
}

async function setBrowserPushEnabled(enabled) {
  const toggle = $("#notification-push-toggle");
  toggle.disabled = true;
  $("#notification-status").textContent = enabled ? "Подключаем Push…" : "Отключаем Push…";
  try {
    if (enabled) {
      const result = await initPushSubscription(true);
      if (!result.ok) throw new Error(result.reason || "Push недоступен");
    } else {
      const registration = await navigator.serviceWorker.ready;
      const subscription = await registration.pushManager.getSubscription();
      const endpoint = subscription?.endpoint || "";
      await apiFetch("/push/unsubscribe", {
        method: "POST",
        body: JSON.stringify({ endpoint }),
      });
      if (subscription) await subscription.unsubscribe();
    }
    await reloadNotificationSettings();
    $("#notification-status").textContent = enabled ? "Push включён" : "Push выключен";
  } catch (error) {
    toggle.checked = !enabled;
    $("#notification-status").textContent = `Ошибка: ${error.message}`;
  } finally {
    renderNotificationSettings();
  }
}

async function setTelegramNotifications(enabled) {
  const toggle = $("#notification-telegram-toggle");
  toggle.disabled = true;
  $("#notification-status").textContent = "Сохраняем настройки Telegram…";
  try {
    await apiFetch("/settings/notifications", {
      method: "PUT",
      body: JSON.stringify({ telegram_enabled: enabled }),
    });
    const readback = await reloadNotificationSettings();
    if (readback.telegram_error) {
      throw new Error(readback.telegram_error);
    }
    if (Boolean(readback.telegram_enabled) !== enabled) {
      throw new Error("Почтовый сервер не подтвердил настройку Telegram");
    }
    $("#notification-status").textContent = enabled
      ? "Оповещения в Telegram включены"
      : "Оповещения в Telegram выключены";
  } catch (error) {
    toggle.checked = !enabled;
    $("#notification-status").textContent = `Ошибка: ${error.message}`;
  } finally {
    renderNotificationSettings();
  }
}

const SETTINGS_PROTECTED_FOLDERS = new Set(["inbox", "sent", "drafts", "trash", "junk", "archive"]);

function renderSettingsFolders() {
  const list = $("#settings-folder-list");
  list.innerHTML = "";
  const customFolders = state.folders.filter(
    (folder) => !SETTINGS_PROTECTED_FOLDERS.has(String(folder.name || "").toLowerCase()),
  );
  if (!customFolders.length) {
    list.innerHTML = '<div class="muted">Пользовательских папок пока нет</div>';
  } else {
    customFolders.forEach((folder) => {
      const row = document.createElement("div");
      row.className = "settings-folder-row";
      row.innerHTML = `
        <span>${escapeHtml(folder.label || folder.name)}</span>
        <button class="mini-btn danger settings-folder-delete" type="button" data-folder="${escapeHtml(folder.name)}">Удалить</button>
      `;
      list.appendChild(row);
    });
  }
  const target = $("#sieve-action-value");
  const selected = target.value;
  target.innerHTML = state.folders
    .map((folder) => `<option value="${escapeHtml(folder.name)}">${escapeHtml(folder.label || folder.name)}</option>`)
    .join("");
  if (state.folders.some((folder) => folder.name === selected)) target.value = selected;
}

async function createFolder(name) {
  await apiFetch("/folders", {
    method: "POST",
    body: JSON.stringify({ name }),
  });
  await loadFolders();
}

async function createSettingsFolder() {
  const input = $("#settings-folder-name");
  const name = input.value.trim();
  if (!name) {
    $("#settings-folder-status").textContent = "Укажите название папки";
    return;
  }
  try {
    await createFolder(name);
    input.value = "";
    renderSettingsFolders();
    $("#settings-folder-status").textContent = "Папка создана";
  } catch (error) {
    $("#settings-folder-status").textContent = `Ошибка: ${error.message}`;
  }
}

async function createSidebarFolder(event) {
  event.preventDefault();
  const input = $("#sidebar-folder-name");
  const status = $("#sidebar-folder-status");
  const name = input.value.trim();
  if (!name) return;
  status.textContent = "Создаём…";
  try {
    await createFolder(name);
    input.value = "";
    status.textContent = "Папка создана";
  } catch (error) {
    status.textContent = `Ошибка: ${error.message}`;
  }
}

async function deleteSettingsFolder(name) {
  if (!confirm(`Удалить папку «${name}»?`)) return;
  try {
    await apiFetch(`/folders/${encodeURIComponent(name)}`, { method: "DELETE" });
    const result = await apiFetch("/folders");
    state.folders = result.folders || [];
    renderFolders();
    renderSettingsFolders();
    $("#settings-folder-status").textContent = "Папка удалена";
  } catch (error) {
    $("#settings-folder-status").textContent = `Ошибка: ${error.message}`;
  }
}

function setSettingsLoading(loading) {
  state.ui.settingsLoading = loading;
  $("#signature-save").disabled = loading || !state.ui.profileSettingsLoaded;
  $("#template-new").disabled = loading || !state.ui.profileSettingsLoaded;
  $("#template-save").disabled = loading || !state.ui.profileSettingsLoaded;
  $("#template-delete").disabled = loading || !state.ui.profileSettingsLoaded;
  $("#template-use").disabled = loading || !state.ui.profileSettingsLoaded;
  $("#settings-folder-create").disabled = loading || !state.ui.foldersSettingsLoaded;
  $("#sieve-new").disabled = loading || !state.ui.rulesSettingsLoaded;
  $("#sieve-save").disabled = loading || !state.ui.rulesSettingsLoaded;
  $$("#settings-folder-list button").forEach((button) => {
    button.disabled = loading || !state.ui.foldersSettingsLoaded;
  });
  $$("#sieve-list button, #sieve-list input").forEach((control) => {
    control.disabled = loading || !state.ui.rulesSettingsLoaded;
  });
  syncForwardingControls();
  syncAutoReplyControls();
  renderNotificationSettings();
}

async function openSettings(trigger = null) {
  openAccessibleModal("settings-modal", trigger);
  state.ui.forwardingLoaded = false;
  state.ui.autoreplyLoaded = false;
  state.ui.profileSettingsLoaded = false;
  state.ui.notificationsSettingsLoaded = false;
  state.ui.foldersSettingsLoaded = false;
  state.ui.rulesSettingsLoaded = false;
  setSettingsLoading(true);
  $("#signature-status").textContent = "";
  $("#template-status").textContent = "";
  $("#forwarding-status").textContent = "Загрузка настройки…";
  $("#autoreply-status").textContent = "Загрузка настройки…";
  $("#notification-status").textContent = "";
  $("#settings-folder-status").textContent = "";
  $("#sieve-status").textContent = "";
  $("#forwarding-status").classList.remove("warning");
  $("#autoreply-status").classList.remove("warning");
  try {
    const [profileResult, forwardingResult, autoreplyResult, notificationsResult, foldersResult, rulesResult] = await Promise.allSettled([
      apiFetch("/settings/profile"),
      apiFetch("/settings/forwarding"),
      apiFetch("/settings/autoreply"),
      apiFetch("/settings/notifications"),
      apiFetch("/folders"),
      apiFetch("/sieve/rules"),
    ]);
    state.settings.appearance = loadStoredAppearance();
    state.settings.theme = loadStoredTheme();
    if (profileResult.status === "fulfilled") {
      state.ui.profileSettingsLoaded = true;
      state.settings.signature = profileResult.value.signature || "";
      state.settings.templates = profileResult.value.templates || [];
    }
    if (forwardingResult.status === "fulfilled") {
      state.ui.forwardingLoaded = true;
      state.settings.forwarding = {
        ...state.settings.forwarding,
        ...forwardingResult.value,
      };
    }
    if (autoreplyResult.status === "fulfilled") {
      state.ui.autoreplyLoaded = true;
      state.settings.autoreply = {
        ...state.settings.autoreply,
        ...autoreplyResult.value,
      };
    }
    if (notificationsResult.status === "fulfilled") {
      state.ui.notificationsSettingsLoaded = true;
      state.settings.notifications = {
        ...state.settings.notifications,
        ...notificationsResult.value,
      };
    }
    if (foldersResult.status === "fulfilled") {
      state.ui.foldersSettingsLoaded = true;
      state.folders = foldersResult.value.folders || [];
    }
    if (rulesResult.status === "fulfilled") {
      state.ui.rulesSettingsLoaded = true;
      state.settings.rules = rulesResult.value.rules || [];
    }
    renderComposeTemplateOptions();
    renderSettings();
    if (profileResult.status === "rejected") {
      $("#signature-status").textContent = "Не удалось загрузить профиль";
      $("#template-status").textContent = "Не удалось загрузить шаблоны";
    }
    if (forwardingResult.status === "rejected") {
      $("#forwarding-status").textContent = "Не удалось загрузить настройку пересылки";
      $("#forwarding-status").classList.add("warning");
    }
    if (autoreplyResult.status === "rejected") {
      $("#autoreply-status").textContent = "Не удалось загрузить настройку автоответчика";
      $("#autoreply-status").classList.add("warning");
    }
    if (notificationsResult.status === "rejected") {
      $("#notification-status").textContent = "Не удалось загрузить настройки оповещений";
      $("#notification-push-toggle").disabled = true;
      $("#notification-push-test").disabled = true;
      $("#notification-telegram-toggle").disabled = true;
    }
    if (foldersResult.status === "rejected") {
      $("#settings-folder-status").textContent = "Не удалось загрузить папки";
    }
    if (rulesResult.status === "rejected") {
      $("#sieve-status").textContent = "Не удалось загрузить правила";
    }
    const failed = [profileResult, forwardingResult, autoreplyResult, notificationsResult, foldersResult, rulesResult]
      .filter((result) => result.status === "rejected").length;
    if (failed) toast(`Не загружено разделов настроек: ${failed}`, "error");
    if (notificationsResult.status === "fulfilled" && state.settings.notifications.push_enabled) {
      initPushSubscription(false).catch(() => {});
    }
  } catch (e) {
    toast(e.message, "error");
  } finally {
    setSettingsLoading(false);
  }
}

async function saveAutoReply() {
  if (!state.ui.autoreplyLoaded) return;
  const button = $("#autoreply-save");
  const enabled = $("#autoreply-enabled").checked;
  const subject = $("#autoreply-subject").value.trim();
  const body = $("#autoreply-body").value.replace(/\r\n?/g, "\n");
  const startDate = $("#autoreply-start-date").value || null;
  const endDate = $("#autoreply-end-date").value || null;
  const repeatDays = Number($("#autoreply-repeat-days").value);
  const status = $("#autoreply-status");
  button.disabled = true;
  try {
    if (enabled && (!subject || !body.trim())) {
      throw new Error("Укажите тему и текст автоответа");
    }
    if (!Number.isInteger(repeatDays) || repeatDays < 1 || repeatDays > 30) {
      throw new Error("Интервал должен быть от 1 до 30 дней");
    }
    if (startDate && endDate && startDate > endDate) {
      throw new Error("Дата начала не может быть позже даты окончания");
    }
    await apiFetch("/settings/autoreply", {
      method: "PUT",
      body: JSON.stringify({
        enabled,
        subject,
        body,
        start_date: startDate,
        end_date: endDate,
        repeat_days: repeatDays,
      }),
    });
    await reloadAutoReplySettings();
  } catch (error) {
    status.textContent = "Ошибка: " + error.message;
    status.classList.add("warning");
  } finally {
    syncAutoReplyControls();
  }
}

async function saveForwarding() {
  if (!state.ui.forwardingLoaded) return;
  const button = $("#forwarding-save");
  const input = $("#forwarding-address");
  const enabled = $("#forwarding-enabled").checked;
  const address = input.value.trim();
  const keepCopy = $("#forwarding-keep-copy").checked;
  button.disabled = true;
  try {
    if (enabled && (!address || !input.checkValidity())) {
      throw new Error("Укажите корректный email");
    }
    await apiFetch("/settings/forwarding", {
      method: "PUT",
      body: JSON.stringify({ enabled, address, keep_copy: keepCopy }),
    });
    await reloadForwardingSettings();
  } catch (e) {
    const status = $("#forwarding-status");
    status.textContent = "Ошибка: " + e.message;
    status.classList.add("warning");
  } finally {
    syncForwardingControls();
  }
}

async function saveSignature() {
  $("#signature-status").textContent = "Сохранение...";
  try {
    await apiFetch("/settings/signature", {
      method: "PUT",
      body: JSON.stringify({ signature: $("#signature-input").value }),
    });
    await reloadSignatureSettings();
    $("#signature-status").textContent = "Сохранено";
    toast("Подпись сохранена", "success");
  } catch (e) {
    $("#signature-status").textContent = "Ошибка: " + e.message;
  }
}

function clearTemplateEditor() {
  $("#template-id").value = "";
  $("#template-name").value = "";
  $("#template-subject").value = "";
  $("#template-body").value = "";
}

function editTemplate(template) {
  $("#template-id").value = template.id || "";
  $("#template-name").value = template.name || "";
  $("#template-subject").value = template.subject || "";
  $("#template-body").value = template.body || "";
  $$(".template-item").forEach((item) => item.classList.toggle("active", Number(item.dataset.id) === template.id));
}

function renderTemplateList() {
  const list = $("#template-list");
  list.innerHTML = "";
  if (!state.settings.templates.length) {
    list.innerHTML = `<div class="muted">Шаблонов пока нет</div>`;
    clearTemplateEditor();
    return;
  }
  state.settings.templates.forEach((template, index) => {
    const row = document.createElement("button");
    row.className = "template-item";
    row.dataset.id = String(template.id);
    row.innerHTML = `
      <strong>${escapeHtml(template.name)}</strong>
      <span>${escapeHtml(template.subject || "(без темы)")}</span>
    `;
    row.addEventListener("click", () => editTemplate(template));
    list.appendChild(row);
    if (index === 0) editTemplate(template);
  });
}

async function saveTemplate() {
  const payload = {
    id: $("#template-id").value ? Number($("#template-id").value) : null,
    name: $("#template-name").value.trim(),
    subject: $("#template-subject").value,
    body: $("#template-body").value,
  };
  if (!payload.name) {
    $("#template-status").textContent = "Укажите название";
    return;
  }
  $("#template-status").textContent = "Сохранение...";
  try {
    await apiFetch("/settings/templates", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    await reloadTemplateSettings();
    $("#template-status").textContent = "Сохранено";
    toast("Шаблон сохранён", "success");
  } catch (e) {
    $("#template-status").textContent = "Ошибка: " + e.message;
  }
}

async function deleteTemplate() {
  const id = Number($("#template-id").value || "0");
  if (!id) return;
  if (!confirm("Удалить шаблон?")) return;
  try {
    await apiFetch(`/settings/templates/${id}`, { method: "DELETE" });
    await reloadTemplateSettings();
    $("#template-status").textContent = "Удалено";
    toast("Шаблон удалён", "success");
  } catch (e) {
    $("#template-status").textContent = "Ошибка: " + e.message;
  }
}

function useTemplateFromSettings() {
  const id = Number($("#template-id").value || "0");
  const template = state.settings.templates.find((item) => item.id === id);
  if (!template) return;
  closeAccessibleModal("settings-modal");
  openCompose("new");
  $("#compose-subject").value = template.subject || "";
  setComposeBody(template.body || "");
  queueComposeAutosave();
}

function handleComposeDrop(files) {
  const nextFiles = Array.from(files || []).filter((file) => file && file.name);
  if (!nextFiles.length) return;
  state.compose.attachments.push(...nextFiles);
  renderComposeAttachments();
  queueComposeAutosave();
}

function wireAutocomplete(inputEl, listEl, onPick) {
  let timer = null;
  inputEl.addEventListener("input", () => {
    clearTimeout(timer);
    const parts = inputEl.value.split(/[,;]/);
    const last = parts[parts.length - 1].trim();
    if (last.length < 2) {
      listEl.classList.add("hidden");
      return;
    }
    timer = setTimeout(async () => {
      try {
        const res = await apiFetch(`/contacts?q=${encodeURIComponent(last)}`);
        const contacts = res.contacts || [];
        if (!contacts.length) {
          listEl.classList.add("hidden");
          return;
        }
        listEl.innerHTML = "";
        contacts.forEach((contact) => {
          const item = document.createElement("div");
          item.className = "autocomplete-item";
          item.innerHTML = `<div class="ac-name">${escapeHtml(contact.name)}${contact.department ? " · " + escapeHtml(contact.department) : ""}</div><div class="ac-email">${escapeHtml(contact.email)}</div>`;
          item.addEventListener("click", () => {
            parts[parts.length - 1] = " " + contact.email;
            inputEl.value = parts.join(",").replace(/^\s+/, "");
            listEl.classList.add("hidden");
            if (onPick) {
              onPick(contact);
            } else {
              const field = inputEl.id.replace("compose-", "");
              syncRecipientChips(field);
              if (field === "bcc") toggleComposeBcc(true);
              queueComposeAutosave();
            }
            inputEl.focus();
          });
          listEl.appendChild(item);
        });
        listEl.classList.remove("hidden");
      } catch (_) {
        listEl.classList.add("hidden");
      }
    }, 240);
  });
  inputEl.addEventListener("blur", () => {
    setTimeout(() => listEl.classList.add("hidden"), 150);
  });
}

function startAutoRefresh() {
  stopAutoRefresh();
  state.refreshTimer = setInterval(async () => {
    if ($("#compose-modal").classList.contains("hidden")) {
      try {
        const activeStillAvailable = await loadSharedMailboxes();
        if (!activeStillAvailable) return;
        await refreshMailboxChrome();
      } catch (_) {}
    }
  }, 60000);
}

function stopAutoRefresh() {
  if (state.refreshTimer) clearInterval(state.refreshTimer);
  state.refreshTimer = null;
}

function bindSearch() {
  $("#search-input").addEventListener("input", (e) => {
    clearTimeout(state.searchTimer);
    state.searchTimer = setTimeout(async () => {
      state.searchQuery = e.target.value.trim();
      state.page = 1;
      await loadMessages();
    }, 250);
  });
  $("#search-input").addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      $("#search-input").value = "";
      state.searchQuery = "";
      state.page = 1;
      loadMessages();
    }
  });
  $("#btn-search-clear").addEventListener("click", async () => {
    $("#search-input").value = "";
    state.searchQuery = "";
    state.page = 1;
    await loadMessages();
  });
}

function bindFilterChips() {
  const chips = [
    ["#chip-unread", "unread"],
    ["#chip-flagged", "flagged"],
    ["#chip-attachments", "hasAttachments"],
  ];
  chips.forEach(([selector, key]) => {
    $(selector).addEventListener("click", async () => {
      state.filters[key] = !state.filters[key];
      $(selector).classList.toggle("active", state.filters[key]);
      state.page = 1;
      await loadMessages();
    });
  });
}

function bindComposeAutosave() {
  ["#compose-to", "#compose-cc", "#compose-bcc"].forEach((selector) => {
    $(selector).addEventListener("input", () => {
      const field = selector.replace("#compose-", "");
      syncRecipientChips(field);
      if (field === "bcc") toggleComposeBcc(!!$(selector).value.trim());
      queueComposeAutosave();
    });
  });
  ["#compose-subject", "#compose-body"].forEach((selector) => {
    $(selector).addEventListener("input", queueComposeAutosave);
  });
  ["#compose-invite-start", "#compose-invite-end", "#compose-invite-location", "#compose-invite-description"].forEach((selector) => {
    $(selector).addEventListener("input", () => {
      syncComposeInviteUi();
      queueComposeAutosave();
    });
  });
  $("#compose-invite-enabled").addEventListener("change", () => {
    syncComposeInviteUi();
    queueComposeAutosave();
  });
  $("#btn-show-bcc").addEventListener("click", () => {
    toggleComposeBcc();
    if (state.compose.bccVisible) $("#compose-bcc").focus();
    queueComposeAutosave();
  });
  $("#compose-invite-start").addEventListener("change", () => {
    autoAdjustInviteEnd();
    syncComposeInviteUi();
  });
  $("#compose-subject").addEventListener("input", syncComposeInviteUi);
  $(".compose-format-toolbar").addEventListener("mousedown", (event) => event.preventDefault());
  $(".compose-format-toolbar").addEventListener("click", (event) => {
    const button = event.target.closest("[data-compose-command]");
    if (button) formatCompose(button.dataset.composeCommand);
  });
  $("#btn-compose-inline-image").addEventListener("click", () => $("#compose-inline-image-input").click());
  $("#compose-inline-image-input").addEventListener("change", async (event) => {
    try {
      await insertComposeImage(event.target.files?.[0]);
    } catch (error) {
      setComposeStatus(error.message, true);
    } finally {
      event.target.value = "";
    }
  });
  $("#compose-body").addEventListener("paste", async (event) => {
    const image = Array.from(event.clipboardData?.files || []).find((file) => file.type.startsWith("image/"));
    event.preventDefault();
    if (image) {
      try { await insertComposeImage(image); } catch (error) { setComposeStatus(error.message, true); }
      return;
    }
    document.execCommand("insertText", false, event.clipboardData?.getData("text/plain") || "");
  });
  $("#compose-body").addEventListener("keydown", (event) => {
    const frame = event.target.closest?.(".compose-inline-image-frame");
    if (!frame || !event.shiftKey || !["ArrowLeft", "ArrowRight"].includes(event.key)) return;
    event.preventDefault();
    const delta = event.key === "ArrowRight" ? 40 : -40;
    const width = Math.max(80, Math.min(900, frame.getBoundingClientRect().width + delta));
    frame.style.width = `${width}px`;
    queueComposeAutosave();
  });
}

async function handleFocusAction(event) {
  const button = event.target.closest("[data-focus-action]");
  if (!button) return;
  const action = button.dataset.focusAction;
  button.disabled = true;
  try {
    if (action === "retry-source") {
      await loadFocus({ force: true });
      return;
    }
    if (action === "open-message") {
      await openFocusMessage(button.dataset.uid);
      return;
    }
    if (action === "find-returned") {
      const item = state.ui.focus.returned.find((entry) => String(entry.id) === String(button.dataset.id));
      if (item) await findReturnedMessage(item);
      return;
    }
    if (action === "open-calendar") {
      await navigateWorkspace("calendar");
      return;
    }
    if (action === "invite-rsvp") {
      const message = state.ui.focus.invitations.find((entry) => String(entry.uid) === String(button.dataset.uid));
      if (!message) return;
      const response = button.dataset.response;
      const res = await submitInviteResponse(message, response, "INBOX");
      applyInviteResponseToMessage(message, res.response || response);
      messageDetailCache.delete(messageDetailCacheKey("INBOX", message.uid));
      toast(`RSVP отправлен: ${inviteResponseMeta(res.response || response).label || response}`, "success");
      await loadFocus({ force: true });
      return;
    }
    if (action === "cancel-outbox") {
      if (!(await cancelScheduledItem(button.dataset.id))) return;
      toast("Отправка отменена", "success");
      await Promise.all([loadScheduledBadge(), loadFocus({ force: true })]);
      return;
    }
    if (action === "retry-outbox") {
      const res = await retryScheduledItem(button.dataset.id);
      const sentFolder = res.sent_folder || "Sent";
      toast(
        res.sent_copy_ok
          ? `Письмо отправлено, копия сохранена в «${sentFolder}»`
          : "Письмо отправлено, но копия в «Отправленные» не сохранена",
        Array.isArray(res.warnings) && res.warnings.length ? "error" : "success",
      );
      await Promise.all([
        loadScheduledBadge(),
        loadFolders(),
        loadFocus({ force: true }),
      ]);
    }
  } catch (error) {
    toast(error.message || "Действие не выполнено", "error");
  } finally {
    button.disabled = false;
  }
}

function bindEvents() {
  $("#login-form").addEventListener("submit", handleLogin);
  $("#btn-logout").addEventListener("click", handleLogout);
  $("#mailbox-list").addEventListener("click", (event) => {
    const button = event.target.closest("[data-mailbox-key]");
    if (button) {
      switchMailboxContext(button.dataset.mailboxKey).catch((error) => {
        toast(error.message || "Не удалось открыть ящик", "error");
      });
    }
  });
  $("#sidebar-folder-create").addEventListener("submit", createSidebarFolder);
  $("#btn-compose").addEventListener("click", openOrResumeCompose);
  $$("[data-workspace]").forEach((button) => {
    button.addEventListener("click", () => activateWorkspaceNavigation(button));
  });
  bindRovingNavigation("#lm-primary-nav [data-workspace]", activateWorkspaceNavigation);
  bindRovingNavigation("#lm-mobile-nav [data-workspace]", activateWorkspaceNavigation);
  $("#focus-refresh").addEventListener("click", () => loadFocus({ force: true }));
  $("#focus-open-outbox").addEventListener("click", openScheduledList);
  $("#focus-open-calendar").addEventListener("click", () => navigateWorkspace("calendar"));
  $("#focus-workspace").addEventListener("click", handleFocusAction);
  $("#btn-mobile-back").addEventListener("click", () => {
    if (state.currentMessage) {
      closeMobileReader();
      return;
    }
    const fallback = state.ui.previousWorkspace || "focus";
    navigateWorkspace(fallback);
  });
  $("#btn-settings").addEventListener("click", (event) => openSettings(event.currentTarget));
  $("#user-profile-button").addEventListener("click", (event) => openAccount(event.currentTarget));
  $("#account-profile-form").addEventListener("submit", saveAccountProfile);
  $("#account-avatar-button").addEventListener("click", () => $("#account-avatar-input").click());
  $("#account-avatar-input").addEventListener("change", async (event) => {
    try {
      state.ui.accountAvatarDataUrl = await compressAccountAvatar(event.target.files?.[0]);
      renderProfileAvatar(
        $("#account-avatar-button"),
        state.ui.accountAvatarDataUrl,
        state.ui.accountProfile?.displayName || "RuPochta",
      );
      $("#account-avatar-remove").classList.remove("hidden");
      setAccountStatus($("#account-profile-status"), "Фото готово к сохранению.");
    } catch (error) {
      setAccountStatus($("#account-profile-status"), error.message, "error");
    } finally {
      event.target.value = "";
    }
  });
  $("#account-avatar-remove").addEventListener("click", () => {
    state.ui.accountAvatarDataUrl = "";
    renderProfileAvatar(
      $("#account-avatar-button"),
      "",
      state.ui.accountProfile?.displayName || "RuPochta",
    );
    $("#account-avatar-remove").classList.add("hidden");
    setAccountStatus($("#account-profile-status"), "Фото будет удалено после сохранения.");
  });
  $("#account-password-toggle").addEventListener("click", () => {
    const form = $("#account-password-form");
    const opening = form.classList.contains("hidden");
    form.classList.toggle("hidden", !opening);
    $("#account-password-toggle").textContent = opening
      ? "Отмена"
      : state.ui.accountHasPassword ? "Сменить пароль" : "Установить пароль";
    if (opening) $("#account-new-password").focus();
    else form.reset();
  });
  $("#account-password-form").addEventListener("submit", saveAccountPassword);
  $("#btn-mail-tools-toggle").addEventListener("click", () => {
    setMailToolsOpen($("#mail-tools").classList.contains("hidden"));
  });
  $("#btn-refresh").addEventListener("click", async () => {
    await refreshMailboxChrome();
    toast("Обновлено", "success");
  });
  $("#btn-priority-mode").addEventListener("click", () => {
    if (!isPriorityInboxAvailable()) return;
    state.ui.priorityMode = !state.ui.priorityMode;
    if (!state.ui.priorityMode) state.ui.priorityFocus = "all";
    renderMessages();
  });
  $("#btn-thread-mode").addEventListener("click", () => {
    state.threadMode = !state.threadMode;
    state.selectedUid = null;
    clearSelection();
    clearMessageView();
    renderMessages();
  });
  $$("[data-priority-focus]").forEach((chip) => {
    chip.addEventListener("click", () => {
      if (!isPriorityInboxAvailable() || !state.ui.priorityMode) return;
      state.ui.priorityFocus = chip.dataset.priorityFocus || "all";
      clearSelection();
      renderMessages();
    });
  });
  $("#btn-send").addEventListener("click", sendCompose);
  $("#btn-save-draft").addEventListener("click", () => saveDraft(true));
  $("#btn-compose-apply-template").addEventListener("click", applySelectedTemplateToCompose);
  $("#btn-compose-add-attachment").addEventListener("click", () => $("#compose-attachments-input").click());
  $("#compose-attachments-input").addEventListener("change", (e) => {
    handleComposeDrop(e.target.files || []);
    e.target.value = "";
  });
  $("#btn-restore-draft").addEventListener("click", restoreComposeDraft);
  $("#btn-discard-draft").addEventListener("click", discardComposeDraft);

  $("#forwarding-enabled").addEventListener("change", syncForwardingControls);
  $("#forwarding-save").addEventListener("click", saveForwarding);
  $("#autoreply-enabled").addEventListener("change", syncAutoReplyControls);
  $("#autoreply-save").addEventListener("click", saveAutoReply);
  $("#notification-push-toggle").addEventListener("change", (event) => {
    setBrowserPushEnabled(event.target.checked);
  });
  $("#notification-push-test").addEventListener("click", sendTestPush);
  $("#notification-telegram-toggle").addEventListener("change", (event) => {
    setTelegramNotifications(event.target.checked);
  });
  $("#settings-folder-create").addEventListener("click", createSettingsFolder);
  $("#settings-folder-name").addEventListener("keydown", (event) => {
    if (event.key === "Enter") createSettingsFolder();
  });
  $("#settings-folder-list").addEventListener("click", (event) => {
    const button = event.target.closest(".settings-folder-delete");
    if (button) deleteSettingsFolder(button.dataset.folder);
  });
  $("#sieve-new").addEventListener("click", () => showSieveEditor());
  $("#sieve-save").addEventListener("click", saveSieveRule);
  $("#sieve-cancel").addEventListener("click", () => $("#sieve-editor").classList.add("hidden"));
  $("#sieve-action-type").addEventListener("change", syncSieveActionTarget);
  $("#sieve-list").addEventListener("click", handleSieveListClick);
  $("#sieve-list").addEventListener("change", handleSieveListChange);
  $("#theme-dark").addEventListener("click", () => setTheme("dark"));
  $("#theme-light").addEventListener("click", () => setTheme("light"));
  $("#appearance-workspace").addEventListener("click", () => setAppearance("workspace"));
  $("#appearance-classic").addEventListener("click", () => setAppearance("classic"));
  $("#signature-save").addEventListener("click", saveSignature);
  $("#template-new").addEventListener("click", clearTemplateEditor);
  $("#template-save").addEventListener("click", saveTemplate);
  $("#template-delete").addEventListener("click", deleteTemplate);
  $("#template-use").addEventListener("click", useTemplateFromSettings);

  $("#prev-page").addEventListener("click", () => {
    if (state.page > 1) {
      state.page -= 1;
      loadMessages();
    }
  });
  $("#next-page").addEventListener("click", () => {
    const total = Math.max(1, Math.ceil(state.totalMessages / state.perPage));
    if (state.page < total) {
      state.page += 1;
      loadMessages();
    }
  });

  $("#bulk-select-page").addEventListener("change", (e) => {
    if (e.target.checked) {
      const items = getVisibleItems();
      items.forEach((item) => {
        const uids = state.threadMode ? item.uids : [item.uid];
        uids.forEach((uid) => state.selectedUids.add(uid));
      });
    } else {
      state.messages.forEach((message) => state.selectedUids.delete(message.uid));
    }
    renderMessages();
  });
  $("#btn-bulk-read").addEventListener("click", () => runBulkAction("mark_read"));
  $("#btn-bulk-unread").addEventListener("click", () => runBulkAction("mark_unread"));
  $("#btn-bulk-archive").addEventListener("click", () => {
    const archiveFolder = getArchiveFolderName();
    if (!archiveFolder) {
      toast("Папка Архив не найдена", "error");
      return;
    }
    if (archiveFolder === state.currentFolder) {
      toast("Текущая папка уже Архив");
      return;
    }
    runBulkAction("move", { toFolder: archiveFolder, label: `Архивировано: ${getSelectedUids().length}` });
  });
  $("#btn-bulk-star").addEventListener("click", () => runBulkAction("star"));
  $("#btn-bulk-unstar").addEventListener("click", () => runBulkAction("unstar"));
  $("#btn-bulk-delete").addEventListener("click", () => runBulkAction("delete"));
  $("#btn-bulk-move").addEventListener("click", () => {
    const target = $("#bulk-move-target").value;
    if (target) runBulkAction("move", { toFolder: target });
  });

  $("#toggle-sidebar").addEventListener("click", () => {
    suspendCompose();
    $("#app").classList.toggle("show-sidebar");
  });

  $$("[data-close-modal]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = btn.getAttribute("data-close-modal");
      const modal = $("#" + id);
      if (
        btn.hasAttribute("data-workspace-back")
        && modal.classList.contains("workspace-destination")
      ) {
        return;
      }
      if (ACCESSIBLE_MODAL_IDS.has(id)) closeAccessibleModal(id);
      else if (id === "compose-modal") suspendCompose();
      else {
        modal.classList.add("hidden");
        if (modal.hasAttribute("data-workspace-surface")) {
          modal.setAttribute("aria-hidden", "true");
        }
      }
    });
  });
  $$("[data-workspace-back]").forEach((btn) => {
    btn.addEventListener("click", () => {
      restoreOriginatingWorkspace(btn);
    });
  });
  $$(".modal-backdrop").forEach((backdrop) => {
    backdrop.addEventListener("click", () => {
      const modal = backdrop.closest(".modal");
      if (restoreOriginatingWorkspace(backdrop)) return;
      if (ACCESSIBLE_MODAL_IDS.has(modal.id)) closeAccessibleModal(modal.id);
      else if (modal.id === "compose-modal") suspendCompose();
      else {
        modal.classList.add("hidden");
        if (modal.hasAttribute("data-workspace-surface")) {
          modal.setAttribute("aria-hidden", "true");
        }
      }
    });
  });

  wireAutocomplete($("#compose-to"), $("#ac-to"));
  wireAutocomplete($("#compose-cc"), $("#ac-cc"));
  wireAutocomplete($("#compose-bcc"), $("#ac-bcc"));
  $$(".settings-tab").forEach((item) => {
    item.addEventListener("click", () => switchSettingsTab(item.dataset.settingsTab));
    item.addEventListener("keydown", handleSettingsTabKeydown);
  });
  const dropzone = $("#compose-dropzone");
  ["dragenter", "dragover"].forEach((eventName) => {
    dropzone.addEventListener(eventName, (e) => {
      e.preventDefault();
      dropzone.classList.add("is-dragover");
    });
  });
  ["dragleave", "dragend", "drop"].forEach((eventName) => {
    dropzone.addEventListener(eventName, (e) => {
      e.preventDefault();
      dropzone.classList.remove("is-dragover");
    });
  });
  dropzone.addEventListener("drop", (e) => handleComposeDrop(e.dataTransfer && e.dataTransfer.files));
  window.addEventListener("resize", syncResponsiveShell);
  document.addEventListener("keydown", (e) => {
    if (!handleAccessibleModalKeydown(e)) handleGlobalShortcuts(e);
  });
  bindSearch();
  bindFilterChips();
  bindComposeAutosave();
}

const directoryState = {
  contacts: [],
  filtered: [],
  deptFilter: null,
  searchQ: "",
  searchTimer: null,
  selected: null,
  loaded: false,
};

async function openDirectory({ asWorkspace = false } = {}) {
  const modal = $("#directory-modal");
  modal.classList.toggle("workspace-destination", asWorkspace);
  modal.classList.remove("hidden");
  modal.setAttribute("aria-hidden", "false");
  if (!directoryState.loaded) await loadDirectory();
}

async function loadDirectory(force) {
  const loading = $("#dir-loading");
  const grid = $("#dir-grid");
  loading.textContent = "Загрузка…";
  loading.style.display = "block";
  if (!directoryState.loaded) {
    grid.innerHTML = "";
  }
  try {
    const res = await apiFetch("/directory" + (force ? "?refresh=1" : ""));
    directoryState.contacts = res.contacts || [];
    directoryState.loaded = true;
    $("#dir-total").textContent = `(${res.total})`;
    renderDepartments(res.departments || []);
    applyDirectoryFilter();
    loading.style.display = "none";
  } catch (e) {
    loading.textContent = directoryState.loaded
      ? "Не удалось обновить справочник. Показаны последние загруженные данные."
      : "Ошибка: " + e.message;
    if (directoryState.loaded) {
      renderDepartments([]);
      applyDirectoryFilter();
    }
  }
}

function renderDepartments(departments) {
  const ul = $("#dir-departments");
  ul.innerHTML = "";
  const liAll = document.createElement("li");
  liAll.className = directoryState.deptFilter === null ? "active" : "";
  liAll.innerHTML = `<span>Все</span><span class="count">${directoryState.contacts.length}</span>`;
  liAll.addEventListener("click", () => {
    directoryState.deptFilter = null;
    applyDirectoryFilter();
  });
  ul.appendChild(liAll);

  departments.forEach((dept) => {
    const li = document.createElement("li");
    if (directoryState.deptFilter === dept.name) li.classList.add("active");
    li.innerHTML = `<span>${escapeHtml(dept.name)}</span><span class="count">${dept.count}</span>`;
    li.addEventListener("click", () => {
      directoryState.deptFilter = dept.name;
      applyDirectoryFilter();
    });
    ul.appendChild(li);
  });
}

function initials(name) {
  if (!name) return "?";
  return name.split(/\s+/).filter(Boolean).slice(0, 2).map((part) => part[0]).join("").toUpperCase();
}

function applyDirectoryFilter() {
  const q = directoryState.searchQ.toLowerCase();
  directoryState.filtered = directoryState.contacts.filter((contact) => {
    if (directoryState.deptFilter && (contact.department || "(без отдела)") !== directoryState.deptFilter) return false;
    if (!q) return true;
    const hay = [
      contact.name, contact.email, contact.login, contact.mail_login,
      contact.department, contact.title, contact.phone, contact.mobile,
    ].filter(Boolean).join(" ").toLowerCase();
    return hay.includes(q);
  });
  renderDirectoryGrid();
}

function renderDirectoryGrid() {
  const grid = $("#dir-grid");
  grid.innerHTML = "";
  if (!directoryState.filtered.length) {
    grid.innerHTML = `<div class="empty-state">Ничего не найдено</div>`;
    return;
  }
  directoryState.filtered.forEach((contact) => {
    const card = document.createElement("div");
    card.className = "dir-card";
    const avatar = document.createElement("div");
    avatar.className = "dir-avatar";
    if (contact.avatar_data_url) {
      const img = document.createElement("img");
      img.src = contact.avatar_data_url;
      img.onerror = () => {
        img.remove();
        avatar.textContent = initials(contact.name);
      };
      img.style.cssText = "width:100%;height:100%;object-fit:cover;border-radius:50%";
      avatar.appendChild(img);
    } else if (contact.login && contact.source === "ad") {
      const img = document.createElement("img");
      img.src = `/api/directory/${encodeURIComponent(contact.login)}/photo`;
      img.onerror = () => {
        img.remove();
        avatar.textContent = initials(contact.name);
      };
      img.style.cssText = "width:100%;height:100%;object-fit:cover;border-radius:50%";
      avatar.appendChild(img);
    } else {
      avatar.textContent = initials(contact.name);
    }
    const info = document.createElement("div");
    info.className = "dir-info";
    info.innerHTML = `
      <div class="dir-name">${escapeHtml(contact.name || "(без имени)")}</div>
      ${contact.title ? `<div class="dir-title">${escapeHtml(contact.title)}</div>` : ""}
      <div class="dir-meta">${escapeHtml(contact.email || "")}</div>
    `;
    card.appendChild(avatar);
    card.appendChild(info);
    card.addEventListener("click", () => openContactDetails(contact));
    grid.appendChild(card);
  });
}

function openContactDetails(contact) {
  directoryState.selected = contact;
  $("#dir-details").classList.remove("hidden");
  const photo = $("#dir-detail-photo");
  photo.style.display = "none";
  photo.removeAttribute("src");
  if (contact.avatar_data_url || (contact.login && contact.source === "ad")) {
    photo.src = contact.avatar_data_url || `/api/directory/${encodeURIComponent(contact.login)}/photo`;
    photo.onload = () => { photo.style.display = "block"; };
    photo.onerror = () => { photo.style.display = "none"; };
  }
  $("#dir-detail-name").textContent = contact.name || "(без имени)";
  $("#dir-detail-title").textContent = contact.title || "";
  $("#dir-detail-dept").textContent = contact.department || "";
  $("#dir-detail-email").textContent = contact.email || "—";
  $("#dir-act-email").disabled = !contact.email;
}

function bindDirectory() {
  $("#dir-search").addEventListener("input", (e) => {
    clearTimeout(directoryState.searchTimer);
    directoryState.searchQ = e.target.value.trim();
    directoryState.searchTimer = setTimeout(applyDirectoryFilter, 180);
  });
  $("#dir-detail-close").addEventListener("click", () => {
    $("#dir-details").classList.add("hidden");
    directoryState.selected = null;
  });
  $("#dir-act-email").addEventListener("click", () => {
    const contact = directoryState.selected;
    if (!contact || !contact.email) return;
    const directoryModal = $("#directory-modal");
    if (!directoryModal.classList.contains("workspace-destination")) {
      directoryModal.classList.add("hidden");
      directoryModal.setAttribute("aria-hidden", "true");
    }
    openCompose("new");
    $("#compose-to").value = contact.email;
    syncRecipientChips("to");
    queueComposeAutosave();
  });
  $("#dir-act-copy-email").addEventListener("click", () => {
    const contact = directoryState.selected;
    if (!contact || !contact.email) return;
    navigator.clipboard.writeText(contact.email).then(
      () => toast("Email скопирован", "success"),
      () => toast("Не удалось скопировать", "error"),
    );
  });
}

// ==================== CALENDAR ====================
const calendarState = {
  events: [],
  byDate: {},              // "YYYY-MM-DD" -> [event, ...]
  currentMonth: null,      // Date pointing to first day of currently-shown month
  loaded: false,
  editingUid: null,
  attendees: [],           // [{email,name,department,title,company}] — calendar invite participants
};

async function openCalendar({ asWorkspace = false } = {}) {
  const modal = $("#calendar-modal");
  modal.classList.toggle("workspace-destination", asWorkspace);
  modal.classList.remove("hidden");
  modal.setAttribute("aria-hidden", "false");
  if (!calendarState.currentMonth) {
    const now = new Date();
    calendarState.currentMonth = new Date(now.getFullYear(), now.getMonth(), 1);
  }
  await reloadCalendar();
}

async function reloadCalendar() {
  try {
    const res = await apiFetch("/calendar/events");
    calendarState.events = res.events || [];
    calendarState.loaded = true;
    $("#cal-meta").textContent = "(" + calendarState.events.length + ")";
    indexCalendarEvents();
    renderCalendarMonth();
    renderUpcoming();
  } catch (e) {
    toast("Ошибка загрузки календаря: " + e.message, "error");
  }
}

function indexCalendarEvents() {
  calendarState.byDate = {};
  calendarState.events.forEach((ev) => {
    const key = (ev.start || "").split("T")[0];
    if (!key) return;
    if (!calendarState.byDate[key]) calendarState.byDate[key] = [];
    calendarState.byDate[key].push(ev);
  });
  Object.values(calendarState.byDate).forEach((list) => {
    list.sort((a, b) => (a.start || "").localeCompare(b.start || ""));
  });
}

function fmtDateKey(d) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${dd}`;
}

function fmtMonthYearRu(d) {
  const months = ["Январь","Февраль","Март","Апрель","Май","Июнь",
                  "Июль","Август","Сентябрь","Октябрь","Ноябрь","Декабрь"];
  return `${months[d.getMonth()]} ${d.getFullYear()}`;
}

function renderCalendarMonth() {
  const grid = $("#cal-month-grid");
  grid.innerHTML = "";
  const label = $("#cal-current-label");
  const month = calendarState.currentMonth;
  if (!month) return;
  label.textContent = fmtMonthYearRu(month);

  // Day-of-week headers (Mon-first)
  const dow = ["Пн","Вт","Ср","Чт","Пт","Сб","Вс"];
  dow.forEach((d) => {
    const h = document.createElement("div");
    h.className = "cal-dow-header";
    h.textContent = d;
    grid.appendChild(h);
  });

  const firstDay = new Date(month.getFullYear(), month.getMonth(), 1);
  // Get weekday 0=Sun → convert to Mon-first: 0=Mon..6=Sun
  const jsDow = firstDay.getDay();
  const monDow = (jsDow + 6) % 7;
  const gridStart = new Date(firstDay);
  gridStart.setDate(1 - monDow);

  const today = new Date();
  const todayKey = fmtDateKey(today);

  for (let i = 0; i < 42; i++) {
    const d = new Date(gridStart);
    d.setDate(gridStart.getDate() + i);
    const cell = document.createElement("div");
    cell.className = "cal-day";
    if (d.getMonth() !== month.getMonth()) cell.classList.add("other-month");
    const key = fmtDateKey(d);
    if (key === todayKey) cell.classList.add("today");

    const num = document.createElement("div");
    num.className = "cal-day-num";
    num.textContent = d.getDate();
    cell.appendChild(num);

    const eventsWrap = document.createElement("div");
    eventsWrap.className = "cal-day-events";
    const dayEvents = calendarState.byDate[key] || [];
    dayEvents.slice(0, 3).forEach((ev) => {
      const pill = document.createElement("div");
      pill.className = "cal-day-event";
      const time = (ev.start || "").includes("T")
        ? ev.start.split("T")[1].substring(0, 5) + " "
        : "";
      pill.textContent = time + (ev.summary || "(без названия)");
      if ((ev.start || "").localeCompare(new Date().toISOString()) < 0) {
        pill.classList.add("past");
      }
      pill.addEventListener("click", (e) => {
        e.stopPropagation();
        openCalendarEditor(ev);
      });
      eventsWrap.appendChild(pill);
    });
    if (dayEvents.length > 3) {
      const more = document.createElement("div");
      more.className = "cal-day-event";
      more.style.background = "var(--text2)";
      more.textContent = `+ ещё ${dayEvents.length - 3}`;
      eventsWrap.appendChild(more);
    }
    cell.appendChild(eventsWrap);

    cell.addEventListener("click", () => {
      const dStart = new Date(d);
      dStart.setHours(10, 0, 0, 0);
      const dEnd = new Date(d);
      dEnd.setHours(11, 0, 0, 0);
      openCalendarEditor({
        uid: null,
        summary: "",
        description: "",
        location: "",
        start: dStart.toISOString(),
        end: dEnd.toISOString(),
        attendees: [],
      });
    });
    grid.appendChild(cell);
  }
}

function renderUpcoming() {
  const ul = $("#cal-upcoming");
  ul.innerHTML = "";
  const now = new Date().toISOString();
  const upcoming = (calendarState.events || [])
    .filter((e) => (e.start || "") >= now)
    .sort((a, b) => (a.start || "").localeCompare(b.start || ""))
    .slice(0, 15);
  if (!upcoming.length) {
    const empty = document.createElement("li");
    empty.className = "empty-state";
    empty.textContent = "Нет ближайших событий";
    ul.appendChild(empty);
    return;
  }
  upcoming.forEach((ev) => {
    const li = document.createElement("li");
    const date = document.createElement("div");
    date.className = "cal-up-date";
    const d = new Date(ev.start);
    date.textContent = d.toLocaleDateString("ru-RU",
      { day: "2-digit", month: "short", weekday: "short" });
    li.appendChild(date);
    const title = document.createElement("div");
    title.className = "cal-up-title";
    title.textContent = ev.summary || "(без названия)";
    li.appendChild(title);
    const time = document.createElement("div");
    time.className = "cal-up-time";
    if (!ev.all_day && ev.start.includes("T")) {
      time.textContent = ev.start.split("T")[1].substring(0, 5)
        + " — "
        + (ev.end && ev.end.includes("T") ? ev.end.split("T")[1].substring(0, 5) : "");
    } else {
      time.textContent = "Весь день";
    }
    li.appendChild(time);
    if (ev.location) {
      const loc = document.createElement("div");
      loc.className = "cal-up-time";
      loc.textContent = "📍 " + ev.location;
      li.appendChild(loc);
    }
    li.addEventListener("click", () => openCalendarEditor(ev));
    ul.appendChild(li);
  });
}

function isoToLocalInput(iso) {
  if (!iso) return "";
  // datetime-local wants "YYYY-MM-DDTHH:MM" without timezone
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "";
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function localInputToIso(v) {
  if (!v) return "";
  const d = new Date(v);
  if (isNaN(d.getTime())) return v;
  return d.toISOString();
}

function openCalendarEditor(ev) {
  calendarState.editingUid = ev.uid || null;
  $("#cal-event-title").textContent = ev.uid ? "Событие" : "Новое событие";
  $("#cal-ev-summary").value = ev.summary || "";
  $("#cal-ev-description").value = ev.description || "";
  $("#cal-ev-location").value = ev.location || "";
  $("#cal-ev-start").value = isoToLocalInput(ev.start);
  $("#cal-ev-end").value = isoToLocalInput(ev.end);
  setAttendeesFromEvent(ev.attendees || []);
  $("#cal-ev-status").textContent = "";
  $("#cal-ev-delete").style.display = ev.uid ? "inline-block" : "none";
  $("#cal-event-modal").classList.remove("hidden");
}

async function saveCalendarEvent() {
  const summary = $("#cal-ev-summary").value.trim();
  const start = $("#cal-ev-start").value;
  const end = $("#cal-ev-end").value;
  if (!summary) { $("#cal-ev-status").textContent = "Введите название"; return; }
  if (!start || !end) { $("#cal-ev-status").textContent = "Укажите дату начала и окончания"; return; }
  const body = {
    summary,
    description: $("#cal-ev-description").value,
    location: $("#cal-ev-location").value,
    start: localInputToIso(start),
    end: localInputToIso(end),
    attendees: (calendarState.attendees || [])
      .map((a) => ({ email: a.email }))
      .filter((a) => a.email),
  };
  $("#cal-ev-status").textContent = "Сохранение…";
  try {
    if (calendarState.editingUid) {
      await apiFetch("/calendar/event/" + encodeURIComponent(calendarState.editingUid), {
        method: "PUT",
        body: JSON.stringify(body),
      });
    } else {
      await apiFetch("/calendar/event", {
        method: "POST",
        body: JSON.stringify(body),
      });
    }
    $("#cal-event-modal").classList.add("hidden");
    toast("Событие сохранено", "success");
    await reloadCalendar();
  } catch (e) {
    $("#cal-ev-status").textContent = "Ошибка: " + e.message;
  }
}

async function deleteCalendarEvent() {
  if (!calendarState.editingUid) return;
  if (!confirm("Удалить событие?")) return;
  try {
    await apiFetch("/calendar/event/" + encodeURIComponent(calendarState.editingUid), {
      method: "DELETE",
    });
    $("#cal-event-modal").classList.add("hidden");
    toast("Событие удалено", "success");
    await reloadCalendar();
  } catch (e) {
    $("#cal-ev-status").textContent = "Ошибка: " + e.message;
  }
}

async function showCalendarConnectInfo() {
  try {
    const cfg = await apiFetch("/calendar/config");
    $("#cal-connect-url").value = cfg.calendar_url;
    $("#cal-connect-user").value = cfg.username;
    $("#cal-connect-modal").classList.remove("hidden");
  } catch (e) {
    toast("Ошибка: " + e.message, "error");
  }
}

function bindCalendar() {
  $("#cal-btn-today").addEventListener("click", () => {
    const now = new Date();
    calendarState.currentMonth = new Date(now.getFullYear(), now.getMonth(), 1);
    renderCalendarMonth();
  });
  $("#cal-btn-prev").addEventListener("click", () => {
    const m = calendarState.currentMonth;
    calendarState.currentMonth = new Date(m.getFullYear(), m.getMonth() - 1, 1);
    renderCalendarMonth();
  });
  $("#cal-btn-next").addEventListener("click", () => {
    const m = calendarState.currentMonth;
    calendarState.currentMonth = new Date(m.getFullYear(), m.getMonth() + 1, 1);
    renderCalendarMonth();
  });
  $("#cal-btn-new").addEventListener("click", () => {
    const now = new Date();
    now.setMinutes(0, 0, 0);
    now.setHours(now.getHours() + 1);
    const end = new Date(now);
    end.setHours(end.getHours() + 1);
    openCalendarEditor({
      uid: null,
      summary: "",
      description: "",
      location: "",
      start: now.toISOString(),
      end: end.toISOString(),
      attendees: [],
    });
  });
  $("#cal-btn-connect").addEventListener("click", showCalendarConnectInfo);
  $("#cal-ev-save").addEventListener("click", saveCalendarEvent);
  $("#cal-ev-delete").addEventListener("click", deleteCalendarEvent);
  wireAttendeePicker();
}

// ============================================================================
// J1-J7 feature modules (Smart Reply, Triage, Send Later, Snooze, Sieve, PWA,
// Shared Mailboxes). Each module exposes bindX() to register DOM listeners
// and its own loaders. All use the existing apiFetch/toast infrastructure.
// ============================================================================

// ---- J2: AI Smart Reply -----------------------------------------------------
async function openSmartReply() {
  if (isSharedMailboxActive()) {
    toast("Умные ответы доступны только в личном ящике", "error");
    return;
  }
  const message = state.currentMessage;
  if (!message) return;
  // Reuse the compose modal as a preview area? No — show suggestions inline
  // above the reply actions inside the reader pane.
  let host = document.getElementById("smart-reply-host");
  if (!host) {
    host = document.createElement("div");
    host.id = "smart-reply-host";
    host.className = "smart-reply-host";
    const actions = document.getElementById("msg-actions");
    if (actions) actions.parentNode.insertBefore(host, actions.nextSibling);
  }
  host.innerHTML = '<div class="smart-reply-loading">Генерация вариантов ответа… (до 30 сек)</div>';
  try {
    const uid = encodeURIComponent(message.uid);
    const folder = encodeURIComponent(state.currentFolder);
    const res = await apiFetch(`/messages/${uid}/smart_reply?folder=${folder}`, { method: "POST" });
    const list = res.suggestions || [];
    if (!list.length) {
      host.innerHTML = '<div class="smart-reply-loading">Не удалось сгенерировать варианты.</div>';
      return;
    }
    host.innerHTML =
      '<div class="smart-reply-title">Варианты ответа</div>' +
      '<div class="smart-reply-list">' +
      list.map((s, i) =>
        `<div class="smart-reply-card" data-index="${i}">
           <div class="smart-reply-head">
             <span class="smart-reply-tone smart-reply-tone-${escapeHtml(s.tone || 'neutral')}">${escapeHtml(s.label || 'Вариант')}</span>
           </div>
           <div class="smart-reply-body">${escapeHtml(s.text || '')}</div>
           <button class="mini-btn smart-reply-use" data-index="${i}">Использовать</button>
         </div>`
      ).join("") +
      '</div>';
    host.querySelectorAll(".smart-reply-use").forEach((btn) => {
      btn.addEventListener("click", () => {
        const idx = Number(btn.dataset.index || "0");
        const suggestion = list[idx];
        if (!suggestion) return;
        openCompose("reply");
        setTimeout(() => {
          const bodyEl = document.getElementById("compose-body");
          if (bodyEl) {
            setComposeBody(suggestion.text + "\n\n" + composeBodyText());
            bodyEl.focus();
          }
        }, 100);
      });
    });
  } catch (e) {
    host.innerHTML = `<div class="smart-reply-loading error">Ошибка: ${escapeHtml(e.message)}</div>`;
  }
}

// Observe #msg-actions to inject a Smart Reply button whenever a message is rendered.
function _jmailInstallSmartReplyObserver() {
  const view = document.getElementById("message-view");
  if (!view) return;
  const attachBtn = () => {
    if (isSharedMailboxActive()) {
      const staleButton = document.getElementById("btn-smart-reply");
      if (staleButton) staleButton.remove();
      const staleHost = document.getElementById("smart-reply-host");
      if (staleHost) staleHost.remove();
      return;
    }
    const actions = document.getElementById("msg-actions");
    if (!actions || document.getElementById("btn-smart-reply")) return;
    const btn = document.createElement("button");
    btn.className = "btn btn-secondary";
    btn.id = "btn-smart-reply";
    btn.innerHTML = "Подсказать ответ";
    btn.title = "Сгенерировать варианты ответа с помощью AI";
    const replyBtn = document.getElementById("btn-reply");
    if (replyBtn && replyBtn.nextSibling) {
      actions.insertBefore(btn, replyBtn.nextSibling);
    } else {
      actions.appendChild(btn);
    }
    btn.addEventListener("click", openSmartReply);
  };
  attachBtn();
  const observer = new MutationObserver(() => {
    // Remove stale smart-reply host from previous message
    const host = document.getElementById("smart-reply-host");
    if (host && !document.getElementById("btn-smart-reply")) host.remove();
    attachBtn();
  });
  observer.observe(view, { childList: true, subtree: true });
}

// ---- J3: AI Inbox Triage ---------------------------------------------------
const TRIAGE_CATEGORIES = [
  { key: "all",         label: "Все" },
  { key: "important",   label: "Важное" },
  { key: "updates",     label: "Обновления" },
  { key: "newsletters", label: "Рассылки" },
  { key: "promo",       label: "Промо" },
];

state.ui = state.ui || {};
state.ui.triageCategory = "all";
state.ui.triageData = {};  // message_id -> {category, confidence}
state.ui.triageTotals = {};

function renderTriageTabs() {
  let host = document.getElementById("triage-tabs");
  if (!host) {
    host = document.createElement("div");
    host.id = "triage-tabs";
    host.className = "triage-tabs";
    const filter = document.querySelector(".filter-row") || document.querySelector(".message-list-toolbar");
    if (filter && filter.parentNode) {
      filter.parentNode.insertBefore(host, filter.nextSibling);
    } else {
      const msgList = document.getElementById("message-list-pane") || document.getElementById("message-list");
      if (msgList) msgList.insertBefore(host, msgList.firstChild);
    }
  }
  const totals = state.ui.triageTotals || {};
  host.innerHTML = TRIAGE_CATEGORIES.map((c) => {
    const count = c.key === "all" ? (Object.values(totals).reduce((a, b) => a + Number(b || 0), 0)) : Number(totals[c.key] || 0);
    const active = state.ui.triageCategory === c.key ? "active" : "";
    return `<button class="triage-tab ${active}" data-cat="${c.key}">${c.label}${count ? ' <span class="triage-count">' + count + '</span>' : ''}</button>`;
  }).join("");
  host.querySelectorAll(".triage-tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.ui.triageCategory = btn.dataset.cat || "all";
      renderTriageTabs();
      renderMessages();
    });
  });
}

async function loadTriageForFolder() {
  if (isSharedMailboxActive() || state.currentFolder !== "INBOX") {
    state.ui.triageData = {};
    state.ui.triageTotals = {};
    state.ui.triageLoadedAt = 0;
    renderTriageTabs();
    return;
  }
  const now = Date.now();
  if (now - (state.ui.triageLoadedAt || 0) < 10000) return;
  try {
    const res = await apiFetch("/triage?folder=INBOX");
    state.ui.triageLoadedAt = now;
    state.ui.triageData = res.categories || {};
    state.ui.triageTotals = res.totals || {};
    renderTriageTabs();
    // Keep at most one classification request in flight across refresh cycles.
    if (!triageClassifyPromise) {
      triageClassifyPromise = apiFetch("/triage/classify", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ folder: "INBOX", limit: 20 }),
      }).then((r) => {
        if (r && r.classified > 0) {
          // Refresh after classification completes
          setTimeout(loadTriageForFolder, 2000);
        }
      }).catch(() => {}).finally(() => {
        triageClassifyPromise = null;
      });
    }
  } catch (e) {
    // Triage unavailable — hide tabs silently
    const host = document.getElementById("triage-tabs");
    if (host) host.innerHTML = "";
  }
}

// Override renderMessages to filter by triage category when a non-all tab is active.
// Reassign the local function declaration (works because function declarations
// are mutable bindings inside the IIFE closure).
const _jmailOrigRenderMessages = renderMessages;
renderMessages = function patchedRenderMessages() {
  const cat = state.ui.triageCategory || "all";
  if (cat !== "all" && state.currentFolder === "INBOX") {
    const origMessages = state.messages;
    state.messages = state.messages.filter((m) => {
      const entry = state.ui.triageData[m.message_id];
      return entry && entry.category === cat;
    });
    _jmailOrigRenderMessages();
    state.messages = origMessages;
  } else {
    _jmailOrigRenderMessages();
  }
};

// ---- J4: Send Later ---------------------------------------------------------
function pickSchedulePreset() {
  const now = new Date();
  const presets = [];
  // Сегодня вечером 18:00 (если сейчас до 17:00)
  if (now.getHours() < 17) {
    const d = new Date(now);
    d.setHours(18, 0, 0, 0);
    presets.push({ label: "Сегодня в 18:00", value: d });
  }
  // Завтра 9:00
  {
    const d = new Date(now);
    d.setDate(d.getDate() + 1);
    d.setHours(9, 0, 0, 0);
    presets.push({ label: "Завтра в 9:00", value: d });
  }
  // Понедельник 9:00
  {
    const d = new Date(now);
    const dayOfWeek = d.getDay();
    const daysUntilMonday = (8 - dayOfWeek) % 7 || 7;
    d.setDate(d.getDate() + daysUntilMonday);
    d.setHours(9, 0, 0, 0);
    presets.push({ label: "В понедельник в 9:00", value: d });
  }
  // Через час
  {
    const d = new Date(now);
    d.setHours(d.getHours() + 1);
    presets.push({ label: "Через час", value: d });
  }
  return presets;
}

function openScheduleDialog() {
  const existing = document.getElementById("schedule-dialog");
  if (existing) existing.remove();
  const dialog = document.createElement("div");
  dialog.id = "schedule-dialog";
  dialog.className = "modal";
  const presets = pickSchedulePreset();
  const defaultLocal = new Date(Date.now() + 60 * 60 * 1000);
  const defaultValue = new Date(defaultLocal.getTime() - defaultLocal.getTimezoneOffset() * 60000)
    .toISOString().slice(0, 16);
  dialog.innerHTML = `
    <div class="modal-card">
      <div class="modal-header"><h3>Отправить позже</h3><button class="modal-close" data-close>&times;</button></div>
      <div class="modal-body">
        <p class="hint">Когда отправить это письмо?</p>
        <div class="schedule-presets">
          ${presets.map((p, i) => `<button class="btn btn-secondary schedule-preset" data-iso="${p.value.toISOString()}">${escapeHtml(p.label)}</button>`).join("")}
        </div>
        <div class="form-group">
          <label>Или выберите вручную</label>
          <input type="datetime-local" id="schedule-input" value="${defaultValue}">
        </div>
        <div id="schedule-error" class="error-msg"></div>
      </div>
      <div class="modal-footer">
        <button class="btn btn-secondary" data-close>Отмена</button>
        <button class="btn btn-primary" id="schedule-confirm">Запланировать</button>
      </div>
    </div>
  `;
  document.body.appendChild(dialog);
  dialog.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", () => dialog.remove()));
  dialog.querySelectorAll(".schedule-preset").forEach((b) => {
    b.addEventListener("click", () => {
      const iso = b.dataset.iso;
      scheduleCompose(iso).then(() => dialog.remove()).catch(() => {});
    });
  });
  document.getElementById("schedule-confirm").addEventListener("click", () => {
    const val = document.getElementById("schedule-input").value;
    if (!val) {
      document.getElementById("schedule-error").textContent = "Укажите дату/время";
      return;
    }
    const local = new Date(val);
    scheduleCompose(local.toISOString()).then(() => dialog.remove()).catch((e) => {
      document.getElementById("schedule-error").textContent = e.message || "Ошибка";
    });
  });
}

async function scheduleCompose(isoString) {
  const to = document.getElementById("compose-to").value.trim();
  if (!to) { toast("Укажите получателя", "error"); return; }
  if (!ensureGeneratedInviteAttachment()) return;
  const form = new FormData();
  form.append("to", to);
  form.append("cc", document.getElementById("compose-cc").value.trim());
  form.append("bcc", document.getElementById("compose-bcc").value.trim());
  form.append("subject", document.getElementById("compose-subject").value.trim());
  appendComposeBody(form);
  form.append("scheduled_at", isoString);
  if (state.compose.inReplyTo) form.append("in_reply_to", state.compose.inReplyTo);
  if (state.compose.draftUid) form.append("draft_uid", state.compose.draftUid);
  state.compose.attachments.forEach((file) => form.append("attachment", file, file.name));
  try {
    await apiFetch("/scheduled/send", { method: "POST", body: form });
    clearTimeout(state.compose.autosaveTimer);
    clearStoredComposeSnapshot();
    state.compose.draftUid = null;
    state.compose.attachments = [];
    closeComposeSession();
    const when = new Date(isoString).toLocaleString("ru-RU");
    toast(`Запланировано на ${when}`, "success");
  } catch (e) {
    toast("Ошибка: " + e.message, "error");
    throw e;
  }
}

async function cancelScheduledItem(id) {
  if (!confirm("Отменить запланированную отправку?")) return false;
  await apiFetch(`/scheduled/${id}`, { method: "DELETE" });
  return true;
}

async function retryScheduledItem(id) {
  return apiFetch(`/scheduled/${id}/retry`, { method: "POST" });
}

async function openScheduledList() {
  try {
    const res = await apiFetch("/scheduled");
    const items = res.items || [];
    let dialog = document.getElementById("scheduled-dialog");
    if (dialog) dialog.remove();
    dialog = document.createElement("div");
    dialog.id = "scheduled-dialog";
    dialog.className = "modal";
    dialog.innerHTML = `
      <div class="modal-card wide">
        <div class="modal-header"><h3>Outbox / Запланированные</h3><button class="modal-close" data-close>&times;</button></div>
        <div class="modal-body">
          ${items.length === 0 ? '<p class="muted">Нет queued или запланированных писем.</p>' :
            '<div class="scheduled-list">' + items.map((item) => `
              <div class="scheduled-row status-${escapeHtml(item.status)}">
                <div class="scheduled-head">
                  <strong>${escapeHtml(item.subject || '(без темы)')}</strong>
                  <span class="scheduled-time">${new Date(item.scheduled_at).toLocaleString("ru-RU")}</span>
                </div>
                <div class="scheduled-meta">
                  <span>Кому: ${escapeHtml((item.to || []).join(", "))}</span>
                  <span class="scheduled-status">${escapeHtml(item.origin === "immediate" ? `outbox · ${item.status}` : item.status)}</span>
                </div>
                ${item.status === 'pending' ? `<button class="mini-btn scheduled-cancel" data-id="${item.id}">Отменить</button>` : ''}
                ${item.status === 'failed' ? `<button class="mini-btn scheduled-retry" data-id="${item.id}">Повторить</button>` : ''}
                ${item.last_error ? `<div class="error-msg">${escapeHtml(item.last_error)}</div>` : ''}
              </div>
            `).join("") + '</div>'}
        </div>
      </div>
    `;
    document.body.appendChild(dialog);
    dialog.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", () => dialog.remove()));
    dialog.querySelectorAll(".scheduled-cancel").forEach((b) => {
      b.addEventListener("click", async () => {
        try {
          if (!(await cancelScheduledItem(b.dataset.id))) return;
          toast("Отменено", "success");
          dialog.remove();
          loadScheduledBadge().catch(() => {});
          openScheduledList();
        } catch (e) {
          toast("Ошибка: " + e.message, "error");
        }
      });
    });
    dialog.querySelectorAll(".scheduled-retry").forEach((b) => {
      b.addEventListener("click", async () => {
        try {
          const res = await retryScheduledItem(b.dataset.id);
          const sentSaved = !!res.sent_copy_ok;
          const sentFolder = res.sent_folder || "Sent";
          const summary = sentSaved
            ? `Письмо повторно отправлено · копия сохранена в «${sentFolder}»`
            : "Письмо повторно отправлено, но копия в «Отправленные» не сохранена";
          toast(summary, Array.isArray(res.warnings) && res.warnings.length ? "error" : "success");
          dialog.remove();
          loadScheduledBadge().catch(() => {});
          openScheduledList();
          loadFolders().catch(() => {});
        } catch (e) {
          toast("Ошибка: " + e.message, "error");
        }
      });
    });
  } catch (e) {
    toast("Ошибка: " + e.message, "error");
  }
}

// ---- J5: Snooze -------------------------------------------------------------
function openSnoozeDialog() {
  const uids = getSelectedUids();
  const selectedSingle = state.currentMessage && (!uids || uids.length === 0) ? [state.currentMessage.uid] : uids;
  if (!selectedSingle || selectedSingle.length === 0) {
    toast("Выберите письмо(а)", "error");
    return;
  }
  const existing = document.getElementById("snooze-dialog");
  if (existing) existing.remove();
  const dialog = document.createElement("div");
  dialog.id = "snooze-dialog";
  dialog.className = "modal";
  const presets = pickSchedulePreset();
  dialog.innerHTML = `
    <div class="modal-card">
      <div class="modal-header"><h3>Отложить ${selectedSingle.length} ${selectedSingle.length === 1 ? 'письмо' : 'писем'}</h3><button class="modal-close" data-close>&times;</button></div>
      <div class="modal-body">
        <p class="hint">Когда вернуть в входящие?</p>
        <div class="schedule-presets">
          ${presets.map((p) => `<button class="btn btn-secondary snooze-preset" data-iso="${p.value.toISOString()}">${escapeHtml(p.label)}</button>`).join("")}
        </div>
        <div class="form-group">
          <label>Или выберите вручную</label>
          <input type="datetime-local" id="snooze-input">
        </div>
        <div id="snooze-error" class="error-msg"></div>
      </div>
      <div class="modal-footer">
        <button class="btn btn-secondary" data-close>Отмена</button>
        <button class="btn btn-primary" id="snooze-confirm">Отложить</button>
      </div>
    </div>
  `;
  document.body.appendChild(dialog);
  dialog.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", () => dialog.remove()));
  const doSnooze = async (iso) => {
    try {
      await apiFetch("/snooze", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ folder: state.currentFolder, uids: selectedSingle, wake_at: iso }),
      });
      toast("Отложено", "success");
      dialog.remove();
      clearSelection();
      await loadMessages();
    } catch (e) {
      document.getElementById("snooze-error").textContent = e.message || "Ошибка";
    }
  };
  dialog.querySelectorAll(".snooze-preset").forEach((b) => {
    b.addEventListener("click", () => doSnooze(b.dataset.iso));
  });
  document.getElementById("snooze-confirm").addEventListener("click", () => {
    const val = document.getElementById("snooze-input").value;
    if (!val) { document.getElementById("snooze-error").textContent = "Укажите время"; return; }
    const local = new Date(val);
    doSnooze(local.toISOString());
  });
}

async function openSnoozedList() {
  try {
    const res = await apiFetch("/snoozed");
    const items = res.items || [];
    let dialog = document.getElementById("snoozed-dialog");
    if (dialog) dialog.remove();
    dialog = document.createElement("div");
    dialog.id = "snoozed-dialog";
    dialog.className = "modal";
    dialog.innerHTML = `
      <div class="modal-card wide">
        <div class="modal-header"><h3>Отложенные</h3><button class="modal-close" data-close>&times;</button></div>
        <div class="modal-body">
          ${items.length === 0 ? '<p class="muted">Нет отложенных писем.</p>' :
            '<div class="scheduled-list">' + items.map((item) => `
              <div class="scheduled-row status-${escapeHtml(item.status)}">
                <div class="scheduled-head">
                  <strong>${escapeHtml(item.subject || '(без темы)')}</strong>
                  <span class="scheduled-time">Вернётся: ${new Date(item.wake_at).toLocaleString("ru-RU")}</span>
                </div>
                <div class="scheduled-meta">
                  <span>От: ${escapeHtml(item.from_addr || '')}</span>
                  <span class="scheduled-status">${escapeHtml(item.status)}</span>
                </div>
                ${item.status === 'pending' ? `<button class="mini-btn snoozed-cancel" data-id="${item.id}">Вернуть сейчас</button>` : ''}
              </div>
            `).join("") + '</div>'}
        </div>
      </div>
    `;
    document.body.appendChild(dialog);
    dialog.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", () => dialog.remove()));
    dialog.querySelectorAll(".snoozed-cancel").forEach((b) => {
      b.addEventListener("click", async () => {
        try {
          await apiFetch(`/snoozed/${b.dataset.id}`, { method: "DELETE" });
          toast("Возвращено в ящик", "success");
          dialog.remove();
          await loadMessages();
        } catch (e) {
          toast("Ошибка: " + e.message, "error");
        }
      });
    });
  } catch (e) {
    toast("Ошибка: " + e.message, "error");
  }
}

// ---- J1: Sieve Filter Rules (settings tab) ---------------------------------
const SIEVE_ACTION_LABELS = {
  move: "Переместить",
  copy: "Копировать",
  mark_read: "Пометить прочитанным",
  flag: "Поставить флажок",
  discard: "Удалить",
};

function sieveRulePayload(rule, enabled = rule.enabled) {
  return {
    name: rule.name,
    enabled,
    priority: rule.priority || 100,
    match_all: rule.match_all !== false,
    conditions: rule.conditions || [],
    actions: rule.actions || [],
  };
}

function renderSieveRules() {
  const list = $("#sieve-list");
  const rules = state.settings.rules || [];
  list.innerHTML = rules.length
    ? rules.map((rule) => `
        <div class="sieve-rule ${rule.enabled ? "" : "disabled"}" data-id="${rule.id}">
          <div class="sieve-rule-head">
            <strong>${escapeHtml(rule.name)}</strong>
            <label class="toggle"><input type="checkbox" class="sieve-toggle" data-id="${rule.id}" ${rule.enabled ? "checked" : ""}> Вкл</label>
            <button class="mini-btn sieve-edit" type="button" data-id="${rule.id}">Изменить</button>
            <button class="mini-btn danger sieve-delete" type="button" data-id="${rule.id}">Удалить</button>
          </div>
          <div class="sieve-rule-body">
            ${(rule.conditions || []).map((condition) => `<span class="sieve-pill">${escapeHtml(condition.field)} ${escapeHtml(condition.op)} «${escapeHtml(condition.value)}»</span>`).join(" И ")}
            ${(rule.actions || []).map((action) => `<span class="sieve-pill sieve-action">${escapeHtml(SIEVE_ACTION_LABELS[action.type] || action.type)}${action.value ? `: ${escapeHtml(action.value)}` : ""}</span>`).join(" ")}
          </div>
        </div>
      `).join("")
    : '<div class="muted">Правил пока нет</div>';
}

function syncSieveActionTarget() {
  const needsFolder = ["move", "copy"].includes($("#sieve-action-type").value);
  $("#sieve-action-value-row").classList.toggle("hidden", !needsFolder);
  $("#sieve-action-value").disabled = !needsFolder;
}

function showSieveEditor(rule = null) {
  renderSettingsFolders();
  $("#sieve-editor").classList.remove("hidden");
  $("#sieve-editor-title").textContent = rule ? "Редактировать правило" : "Новое правило";
  $("#sieve-rule-id").value = rule?.id || "";
  $("#sieve-name").value = rule?.name || "";
  const condition = rule?.conditions?.[0] || { field: "from", op: "contains", value: "" };
  $("#sieve-field").value = condition.field || "from";
  $("#sieve-op").value = condition.op || "contains";
  $("#sieve-value").value = condition.value || "";
  const action = rule?.actions?.[0] || { type: "move", value: state.folders[0]?.name || "" };
  $("#sieve-action-type").value = action.type || "move";
  if (action.value && state.folders.some((folder) => folder.name === action.value)) {
    $("#sieve-action-value").value = action.value;
  }
  $("#sieve-status").textContent = "";
  syncSieveActionTarget();
}

async function refreshSieveRules() {
  const result = await apiFetch("/sieve/rules");
  state.settings.rules = result.rules || [];
  renderSieveRules();
}

async function saveSieveRule() {
  const id = $("#sieve-rule-id").value;
  const actionType = $("#sieve-action-type").value;
  const payload = {
    name: $("#sieve-name").value.trim(),
    enabled: true,
    priority: 100,
    match_all: true,
    conditions: [{
      field: $("#sieve-field").value,
      op: $("#sieve-op").value,
      value: $("#sieve-value").value.trim(),
    }],
    actions: [{
      type: actionType,
      value: ["move", "copy"].includes(actionType) ? $("#sieve-action-value").value : "",
    }],
  };
  if (!payload.name || !payload.conditions[0].value) {
    $("#sieve-status").textContent = "Заполните название и условие";
    return;
  }
  if (["move", "copy"].includes(actionType) && !payload.actions[0].value) {
    $("#sieve-status").textContent = "Выберите папку";
    return;
  }
  try {
    await apiFetch(id ? `/sieve/rules/${id}` : "/sieve/rules", {
      method: id ? "PUT" : "POST",
      body: JSON.stringify(payload),
    });
    await refreshSieveRules();
    $("#sieve-editor").classList.add("hidden");
    toast("Правило сохранено", "success");
  } catch (error) {
    $("#sieve-status").textContent = `Ошибка: ${error.message}`;
  }
}

async function openSieveRules() {
  if ($("#settings-modal").classList.contains("hidden")) await openSettings();
  switchSettingsTab("folders-rules");
}

async function handleSieveListClick(event) {
  const editButton = event.target.closest(".sieve-edit");
  if (editButton) {
    const rule = state.settings.rules.find(
      (item) => String(item.id) === editButton.dataset.id,
    );
    if (rule) showSieveEditor(rule);
    return;
  }
  const deleteButton = event.target.closest(".sieve-delete");
  if (!deleteButton || !confirm("Удалить правило?")) return;
  try {
    await apiFetch(`/sieve/rules/${deleteButton.dataset.id}`, { method: "DELETE" });
    await refreshSieveRules();
    toast("Правило удалено", "success");
  } catch (error) {
    $("#sieve-status").textContent = `Ошибка: ${error.message}`;
  }
}

async function handleSieveListChange(event) {
  const toggle = event.target.closest(".sieve-toggle");
  if (!toggle) return;
  const rule = state.settings.rules.find(
    (item) => String(item.id) === toggle.dataset.id,
  );
  if (!rule) return;
  toggle.disabled = true;
  try {
    await apiFetch(`/sieve/rules/${rule.id}`, {
      method: "PUT",
      body: JSON.stringify(sieveRulePayload(rule, toggle.checked)),
    });
    await refreshSieveRules();
  } catch (error) {
    toggle.checked = !toggle.checked;
    $("#sieve-status").textContent = `Ошибка: ${error.message}`;
  } finally {
    toggle.disabled = false;
  }
}

// ---- J7: PWA Web Push -------------------------------------------------------
function urlBase64ToUint8Array(base64String) {
  const padding = '='.repeat((4 - base64String.length % 4) % 4);
  const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
  const rawData = window.atob(base64);
  const outputArray = new Uint8Array(rawData.length);
  for (let i = 0; i < rawData.length; ++i) outputArray[i] = rawData.charCodeAt(i);
  return outputArray;
}

async function initPushSubscription(autoPrompt = false) {
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
    return { ok: false, reason: "Не поддерживается браузером" };
  }
  try {
    const reg = await navigator.serviceWorker.ready;
    let sub = await reg.pushManager.getSubscription();
    if (!sub) {
      if (!autoPrompt) return { ok: false, reason: "Не подписан" };
      const permission = await Notification.requestPermission();
      if (permission !== "granted") return { ok: false, reason: "Нет разрешения" };
      const vapidRes = await apiFetch("/push/vapid_key");
      if (!vapidRes.public_key) return { ok: false, reason: "Сервер не настроен" };
      sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(vapidRes.public_key),
      });
    }
    const subJson = sub.toJSON();
    await apiFetch("/push/subscribe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        endpoint: subJson.endpoint,
        keys: subJson.keys,
        user_agent: navigator.userAgent.slice(0, 200),
      }),
    });
    return { ok: true };
  } catch (e) {
    return { ok: false, reason: e.message };
  }
}

async function enablePushPrompt() {
  const res = await initPushSubscription(true);
  if (res.ok) toast("Push-уведомления включены", "success");
  else toast("Push недоступен: " + (res.reason || ""), "error");
}

async function sendTestPush() {
  try {
    const res = await apiFetch("/push/test", { method: "POST" });
    if (res.sent) toast(`Отправлено ${res.sent} уведомлений`, "success");
    else toast("Нет подписок", "error");
  } catch (e) {
    toast("Ошибка: " + e.message, "error");
  }
}

// ---- New topbar buttons and bindings ---------------------------------------
function ensureFeatureButtons() {
  // Add "Schedule send" button to compose modal footer
  const composeFooter = document.querySelector("#compose-modal .modal-footer") || document.querySelector("#compose-modal");
  if (composeFooter && !document.getElementById("btn-schedule-send")) {
    const btn = document.createElement("button");
    btn.id = "btn-schedule-send";
    btn.className = "btn btn-secondary";
    btn.innerHTML = "Отправить позже";
    btn.title = "Отправить позже";
    btn.addEventListener("click", openScheduleDialog);
    const sendBtn = document.getElementById("btn-send");
    if (sendBtn && sendBtn.parentNode) {
      sendBtn.parentNode.insertBefore(btn, sendBtn.nextSibling);
    } else {
      composeFooter.appendChild(btn);
    }
  }
  // Add "Snooze" button to bulk actions toolbar
  const bulkToolbar = document.querySelector(".bulk-actions") || document.getElementById("bulk-actions");
  if (bulkToolbar && !document.getElementById("btn-bulk-snooze")) {
    const btn = document.createElement("button");
    btn.id = "btn-bulk-snooze";
    btn.className = "mini-btn";
    btn.innerHTML = "Отложить";
    btn.addEventListener("click", openSnoozeDialog);
    bulkToolbar.appendChild(btn);
  }
  if (state.me) updateMailboxContextUi();
}

// Patch loadMessages to also kick triage classification
const _jmailOrigLoadMessages = loadMessages;
loadMessages = async function patchedLoadMessages(...args) {
  const result = await _jmailOrigLoadMessages.apply(this, args);
  if (!isSharedMailboxActive()) {
    setTimeout(() => loadTriageForFolder().catch(() => {}), 0);
  }
  return result;
};

document.addEventListener("DOMContentLoaded", () => {
  applyAppearance(loadStoredAppearance(), { skipPersist: true });
  applyTheme(loadStoredTheme(), { skipPersist: true });
  applyLoginPrefill();
  mountComposeSurface();
  syncBrandedComposeTemplate();
  bindEvents();
  bindDirectory();
  bindCalendar();
  initPaneSplitter();
  tryBootstrap();
  // Enhance UI with new feature buttons after initial render
  setTimeout(() => {
    ensureFeatureButtons();
    _jmailInstallSmartReplyObserver();
  }, 500);
  // Re-run ensureFeatureButtons periodically (handles login → app transition)
  setInterval(ensureFeatureButtons, 15000);
});

})();
