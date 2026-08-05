"use strict";

const API = "";
const PRIMARY_MAIL_DOMAIN = "example.com";
const MAIL_HEALTH_POLL_MS = 45000;
const ADMIN_THEME_STORAGE_KEY = "lm-mailadmin-theme";
const FOCUSABLE_SELECTOR = [
  "button:not([disabled]):not([tabindex='-1'])",
  "a[href]",
  "input:not([disabled]):not([type='hidden'])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "[tabindex]:not([tabindex='-1'])",
].join(",");
const modalReturnFocus = new Map();
let mobileDrawerReturnFocus = null;
const PAGE_META = {
  desk: {
    title: "Управление ящиками",
    subtitle: "Создание, настройка и обслуживание почтовых ящиков сотрудников.",
  },
  accounts: {
    title: "Почтовые ящики",
    subtitle: "Полный список действующих почтовых ящиков и быстрые операции.",
  },
  "managed-addresses": {
    title: "Общие адреса",
    subtitle: "Групповые и сервисные ящики, а также адреса рассылок.",
  },
  queue: {
    title: "Очередь писем",
    subtitle: "Контроль активных, отложенных и удержанных сообщений Postfix.",
  },
  aliases: {
    title: "Алиасы",
    subtitle: "Связь логинов AD с адресами корпоративной почты.",
  },
  audit: {
    title: "Журнал действий",
    subtitle: "История административных операций и их результатов.",
  },
  stats: {
    title: "Статистика",
    subtitle: "Использование хранилища и текущее состояние почтовой системы.",
  },
  settings: {
    title: "Настройки",
    subtitle: "Параметры почтового сервера и оформление консоли.",
  },
};

const ADMIN_SECTIONS = {
  overview: { defaultPage: "desk", pages: ["desk"] },
  mailboxes: { defaultPage: "accounts", pages: ["accounts"] },
  domains: { defaultPage: "domains", pages: ["domains"] },
  addresses: { defaultPage: "managed-addresses", pages: ["managed-addresses", "aliases"] },
  diagnostics: { defaultPage: "queue", pages: ["queue", "stats"] },
  audit: { defaultPage: "audit", pages: ["audit"] },
  settings: { defaultPage: "settings", pages: ["settings"] },
};

const ADMIN_PRIMARY_ACTIONS = {
  overview: { label: "Создать ящик", action: openCreateMailbox },
  mailboxes: { label: "Создать ящик", action: openCreateMailbox },
  domains: { label: "Создать ящик", action: openCreateDomainMailbox },
  addresses: { label: "Создать адрес", action: () => openManagedAddressForm() },
};

const ADMIN_RETRY_ACTIONS = {
  "desk": loadDesk,
  "accounts": loadAccounts,
  "domains": loadDomainMailboxes,
  "managed-addresses": loadManagedAddresses,
  "queue": loadQueue,
  "aliases": loadAliases,
  "audit": loadAudit,
  "stats": loadStats,
  "settings": loadAdminSettings,
};

const state = {
  activeSection: "overview",
  activePage: "desk",
  allAccounts: [],
  accountsLoaded: false,
  employeeOptions: [],
  employeeOptionsLoaded: false,
  employeeOptionsError: "",
  adGroups: [],
  adGroupsLoaded: false,
  adGroupsError: "",
  managedAddresses: [],
  technicalMailboxPresets: {},
  selectedManagedAddressId: null,
  currentManagedAddressDeleteId: null,
  managedAddressGroupPermissions: {},
  managedAddressGroupPermissionOverride: null,
  queueEntries: [],
  supportResults: [],
  selectedSupportKey: "",
  selectedSupportItem: null,
  currentDetail: null,
  currentTimeline: null,
  currentDoctor: null,
  currentTimelineFilter: "all",
  currentWorkspaceTab: "overview",
  workspaceRequestId: 0,
  currentDelEmail: "",
  currentLifecycleAction: "",
  currentLifecycleEmail: "",
  currentCredentialPack: null,
  supportSearchTimer: null,
  onboardingCheckTimer: null,
  mailHealthTimer: null,
  mailHealthKey: "",
  authenticated: false,
  sessionExpiryHandled: false,
  toastTimer: null,
  creatingMailbox: false,
  profileCard: null,
};

function $(selector) {
  return document.querySelector(selector);
}

function $$(selector) {
  return Array.from(document.querySelectorAll(selector));
}

function storedAdminThemeMode() {
  try {
    const mode = localStorage.getItem(ADMIN_THEME_STORAGE_KEY);
    return ["light", "dark", "system"].includes(mode) ? mode : "system";
  } catch (_) {
    return "system";
  }
}

function resolvedAdminTheme(mode) {
  if (mode === "light" || mode === "dark") return mode;
  return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches
    ? "dark"
    : "light";
}

function applyAdminTheme(mode, persist = false) {
  const normalized = ["light", "dark", "system"].includes(mode) ? mode : "system";
  document.documentElement.dataset.theme = resolvedAdminTheme(normalized);
  document.documentElement.dataset.themeMode = normalized;
  if (persist) {
    try {
      localStorage.setItem(ADMIN_THEME_STORAGE_KEY, normalized);
    } catch (_) {}
  }
  const select = $("#adminThemeMode");
  if (select) select.value = normalized;
}

applyAdminTheme(storedAdminThemeMode());

function escapeHtml(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function profileInitials(value) {
  const parts = String(value || "").trim().split(/\s+/).filter(Boolean);
  return parts.slice(0, 2).map((part) => part[0]).join("").toUpperCase() || "LM";
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
      target.textContent = profileInitials(displayName);
    }, { once: true });
    target.appendChild(image);
    return;
  }
  target.textContent = profileInitials(displayName);
}

function renderAdminProfileCard(profileCard = null, username = "") {
  const profileAvailable = Boolean(profileCard && profileCard.employee);
  const employee = (profileCard && profileCard.employee) || {};
  const role = (profileAvailable && profileCard.role) || {
    label: profileAvailable ? "Роль не указана" : "Данные Portal недоступны",
    isItSpecialist: false,
  };
  const integrations = profileAvailable && role.isItSpecialist
    ? profileCard.integrations || {}
    : null;
  const displayName = employee.fullName || username || "Администратор";
  renderProfileAvatar($("#admin-profile-avatar"), employee.avatarDataUrl, displayName);
  renderProfileAvatar($("#admin-profile-dialog-avatar"), employee.avatarDataUrl, displayName);
  $("#adminProfileTitle").textContent = displayName;
  $("#admin-profile-role").textContent = role.label || "Роль не указана";
  $("#admin-profile-details").innerHTML = [
    ["Уникальный ID", employee.id],
    ["Фамилия", employee.lastName],
    ["Имя", employee.firstName],
    ["Должность", employee.title],
    ["Компания", employee.company],
    ["Email", employee.email],
    ["Telegram ID", employee.telegramId],
    ["Учетная запись (логин AD)", employee.account || username],
  ].map(([label, value]) => (
    `<div><span>${escapeHtml(label)}</span><strong>${escapeHtml(value || "Не указано")}</strong></div>`
  )).join("");
  const section = $("#admin-profile-integrations");
  section.classList.toggle("hidden", !integrations);
  if (integrations) {
    const telegram = integrations.telegram || {};
    const bookstack = integrations.bookstack || {};
    const glpi = integrations.glpi || {};
    $("#admin-profile-integration-list").innerHTML = [
      ["Telegram", telegram.linked ? `@${telegram.username || telegram.id}` : "Не привязан"],
      ["BookStack", bookstack.linked ? "Привязан" : "Не привязан"],
      ["GLPI", glpi.linked ? glpi.login : "Не привязан"],
    ].map(([label, value]) => (
      `<div><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`
    )).join("");
  }
}

function humanSize(n) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = Number(n || 0);
  let idx = 0;
  while (size >= 1024 && idx < units.length - 1) {
    size /= 1024;
    idx += 1;
  }
  return `${idx === 0 ? size.toFixed(0) : size.toFixed(1)}${units[idx]}`;
}

function toast(message, ok = true) {
  const el = $("#toast");
  el.textContent = `${ok ? "OK" : "Ошибка"} · ${message}`;
  el.classList.add("show");
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => el.classList.remove("show"), 3200);
}

async function copyText(value, label = "Скопировано") {
  try {
    await navigator.clipboard.writeText(value || "");
    toast(label, true);
  } catch (_) {
    toast("Не удалось скопировать", false);
  }
}

function visibleFocusableElements(container) {
  if (!container) return [];
  return $$(FOCUSABLE_SELECTOR)
    .filter((element) => container.contains(element))
    .filter((element) => element.getClientRects().length > 0);
}

function focusFirstInteractive(container) {
  if (!container) return;
  const preferred = container.querySelector("[data-modal-initial]");
  const target = preferred || visibleFocusableElements(container)[0];
  if (target) target.focus();
}

function trapFocus(container, event) {
  if (!container || event.key !== "Tab") return;
  const focusable = visibleFocusableElements(container);
  if (!focusable.length) {
    event.preventDefault();
    return;
  }
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && (document.activeElement === first || !container.contains(document.activeElement))) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && (document.activeElement === last || !container.contains(document.activeElement))) {
    event.preventDefault();
    first.focus();
  }
}

function rememberFocusTarget(target) {
  if (target && target.element instanceof HTMLElement) return target;
  if (!(target instanceof HTMLElement)) return null;
  return {
    element: target,
    id: target.id || "",
  };
}

function restoreFocusTarget(returnFocus, attempts = 8) {
  if (!returnFocus) return;
  window.setTimeout(() => {
    const targetIds = [returnFocus.id, ...(returnFocus.fallbackIds || [])].filter(Boolean);
    const target = returnFocus.element instanceof HTMLElement && returnFocus.element.isConnected
      ? returnFocus.element
      : targetIds.map((id) => document.getElementById(id)).find(Boolean);
    if (target instanceof HTMLElement && target.getClientRects().length > 0) {
      target.focus();
    } else if (attempts > 0) {
      restoreFocusTarget(returnFocus, attempts - 1);
    }
  }, attempts === 8 ? 0 : 50);
}

function showModal(id, returnFocus = null) {
  const el = document.getElementById(id);
  if (!el) return;
  const trigger = rememberFocusTarget(returnFocus || document.activeElement);
  if (trigger && !el.contains(trigger.element)) {
    modalReturnFocus.set(id, trigger);
  }
  el.classList.remove("hidden");
  el.setAttribute("aria-hidden", "false");
  document.body.classList.add("modal-open");
  window.setTimeout(() => focusFirstInteractive(el), 0);
}

function closeModal(id, restoreFocus = true) {
  const el = document.getElementById(id);
  if (!el || el.classList.contains("hidden")) return;
  el.classList.add("hidden");
  el.setAttribute("aria-hidden", "true");
  if (id === "modalCredentialPack") {
    state.currentCredentialPack = null;
    $("#credentialSummary").textContent = "";
    $("#tbQr").innerHTML = "";
    $("#dcQr").innerHTML = "";
  }
  const returnFocus = modalReturnFocus.get(id);
  modalReturnFocus.delete(id);
  if (!document.querySelector(".modal-shell:not(.hidden)")) {
    document.body.classList.remove("modal-open");
  }
  if (restoreFocus) restoreFocusTarget(returnFocus);
}

function updateQueueBadge(n) {
  const count = Number(n || 0);
  [$("#queueBadge"), ...$$(".mobile-queue-badge")].filter(Boolean).forEach((badge) => {
    badge.textContent = String(count);
    badge.classList.toggle("hidden", count <= 0);
  });
}

function mailboxEmailFromValue(value) {
  const raw = String(value || "").trim().toLowerCase();
  if (!raw) return "";
  const emailMatch = raw.match(/[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}/);
  if (emailMatch) return emailMatch[0];
  const candidate = raw.split("->").map((part) => part.trim()).filter(Boolean).pop() || raw;
  if (/^[a-z0-9][a-z0-9._-]{0,62}$/.test(candidate)) return `${candidate}@${PRIMARY_MAIL_DOMAIN}`;
  return "";
}

function openMailCabinet(email) {
  const url = `https://mail.example.com/?login=${encodeURIComponent(email)}`;
  window.open(url, "_blank", "noopener");
}

function queueStatus(entry) {
  if (entry && entry.hold) return "hold";
  if (entry && entry.active) return "active";
  return "deferred";
}

function queueStatusLabel(entry) {
  const status = typeof entry === "string" ? entry : queueStatus(entry);
  if (status === "hold") return "Удержано";
  if (status === "active") return "Активно";
  return "Отложено";
}

function lifecycleStateLabel(stateValue) {
  return stateValue === "suspended" ? "Ящик приостановлен" : "Ящик активен";
}

function lifecycleStatePill(stateValue) {
  return stateValue === "suspended"
    ? `<span class="status-pill bad">Приостановлен</span>`
    : `<span class="status-pill good">Активен</span>`;
}

function supportStatusPill(item) {
  if (item.lifecycle_state === "suspended") return `<span class="status-pill bad">Приостановлен</span>`;
  if (item.mailbox_exists) return `<span class="status-pill good">Готов</span>`;
  if (item.kind === "employee") return `<span class="status-pill warn">Нужно создать</span>`;
  return `<span class="status-pill bad">Без сотрудника</span>`;
}

function resultQueuePill(summary) {
  const total = Number((summary || {}).total || 0);
  return `<span class="pill">Очередь: ${total}</span>`;
}

function severityPill(severity) {
  const normalized = severity === "error" ? "error" : severity === "warn" ? "warn" : "info";
  const label = normalized === "error" ? "Ошибка" : normalized === "warn" ? "Предупреждение" : "Информация";
  return `<span class="severity-pill ${normalized}">${label}</span>`;
}

function healthPillClass(severity) {
  if (severity === "fail") return "bad";
  if (severity === "warn") return "warn";
  return "good";
}

function mailHealthIcon(severity) {
  if (severity === "fail") return "×";
  if (severity === "warn") return "!";
  if (severity === "ok") return "OK";
  return "…";
}

function timelineCategoryPill(category) {
  const labels = {
    all: "Все",
    auth: "Авторизация",
    delivery: "Доставка",
    lifecycle: "Состояние",
    audit: "Аудит",
  };
  const label = labels[category] || category || labels.all;
  const css = category === "auth" ? "warn" : category === "lifecycle" ? "neutral" : category === "delivery" ? "good" : "";
  return `<span class="status-pill ${css}">${escapeHtml(label)}</span>`;
}

function translateHealthText(value) {
  const raw = String(value || "").trim();
  if (!raw) return "";
  const exact = {
    "Mail health unavailable": "Состояние почтовой системы недоступно",
    "state changed": "состояние изменилось",
    "all mail paths healthy": "Все почтовые сервисы работают",
    "mail delivery degraded": "Доставка почты работает с ограничениями",
  };
  if (exact[raw]) return exact[raw];
  return raw
    .replace(/mail control-plane/gi, "контур управления почтой")
    .replace(/backup webmail/gi, "резервный webmail")
    .replace(/outbox/gi, "исходящую очередь")
    .replace(/unavailable/gi, "недоступен")
    .replace(/degraded/gi, "работает с ограничениями");
}

function openMobileDrawer() {
  const drawer = $("#mobileDrawer");
  if (!drawer || !drawer.classList.contains("hidden")) return;
  if (document.activeElement instanceof HTMLElement) {
    mobileDrawerReturnFocus = document.activeElement;
  }
  drawer.classList.remove("hidden");
  drawer.setAttribute("aria-hidden", "false");
  $("#mobileMenuBtn").setAttribute("aria-expanded", "true");
  document.body.classList.add("drawer-open");
  window.setTimeout(() => focusFirstInteractive($(".mobile-drawer-panel")), 0);
}

function closeMobileDrawer(restoreFocus = true) {
  const drawer = $("#mobileDrawer");
  if (!drawer || drawer.classList.contains("hidden")) return;
  drawer.classList.add("hidden");
  drawer.setAttribute("aria-hidden", "true");
  $("#mobileMenuBtn").setAttribute("aria-expanded", "false");
  document.body.classList.remove("drawer-open");
  const returnFocus = mobileDrawerReturnFocus;
  mobileDrawerReturnFocus = null;
  if (restoreFocus && returnFocus instanceof HTMLElement && returnFocus.isConnected) {
    window.setTimeout(() => returnFocus.focus(), 0);
  }
}

function summaryPillClass(value, warnThreshold = 1) {
  if (Number(value || 0) <= 0) return "good";
  return Number(value || 0) >= warnThreshold ? "bad" : "warn";
}

function formatTs(value) {
  const raw = String(value || "").trim();
  if (!raw) return "—";
  const dt = new Date(raw);
  if (Number.isNaN(dt.getTime())) return raw;
  return dt.toLocaleString("ru-RU", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

async function apiJson(url, options = {}) {
  const response = await fetch(API + url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401 && state.authenticated) {
      handleAdminSessionExpired();
    }
    const message = data.detail || data.error || "Ошибка запроса";
    throw new Error(message);
  }
  return data;
}

async function withBusyButton(button, busyText, fn) {
  if (!button) return fn();
  const original = Array.from(button.childNodes, (node) => node.cloneNode(true));
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  button.textContent = busyText;
  try {
    return await fn();
  } finally {
    button.disabled = false;
    button.removeAttribute("aria-busy");
    button.replaceChildren(...original);
  }
}

function handleAdminSessionExpired() {
  if (state.sessionExpiryHandled) return;
  state.sessionExpiryHandled = true;
  state.authenticated = false;
  clearTimeout(state.mailHealthTimer);
  state.mailHealthTimer = null;
  clearTimeout(state.supportSearchTimer);
  clearTimeout(state.onboardingCheckTimer);
  $$(".modal-shell:not(.hidden)").forEach((modal) => closeModal(modal.id, false));
  modalReturnFocus.clear();
  $("#loginPass").value = "";
  $("#pwdNew").value = "";
  $("#appPage").classList.add("hidden");
  $("#loginPage").classList.remove("hidden");
  const error = $("#loginError");
  if (error) {
    error.textContent = "Сессия истекла. Войдите снова через AD.";
  }
}

async function loadEmployeeOptions(force = false) {
  if (state.employeeOptionsLoaded && !force) return state.employeeOptions;
  try {
    const data = await apiJson("/api/adminportal/employees");
    state.employeeOptions = data.employees || [];
    state.employeeOptionsLoaded = true;
    state.employeeOptionsError = "";
  } catch (error) {
    state.employeeOptions = [];
    state.employeeOptionsLoaded = false;
    state.employeeOptionsError = error.message || "Каталог сотрудников недоступен";
  }
  return state.employeeOptions;
}

async function loadAdGroups(force = false) {
  if (state.adGroupsLoaded && !force) return state.adGroups;
  try {
    const data = await apiJson(
      `/api/adminportal/directory/groups${force ? "?refresh=1" : ""}`,
    );
    state.adGroups = data.groups || [];
    state.adGroupsLoaded = true;
    state.adGroupsError = "";
  } catch (error) {
    state.adGroups = [];
    state.adGroupsLoaded = false;
    state.adGroupsError = error.message || "Каталог групп AD недоступен";
  }
  return state.adGroups;
}

function renderAdGroupOptions(selectId, selectedGroups = []) {
  const select = $(selectId);
  if (!select) return;
  const selectedByDn = new Map(
    (selectedGroups || []).map((group) => [
      String(group.dn || group).toLowerCase(),
      typeof group === "string" ? { dn: group, name: group } : group,
    ]),
  );
  const catalogByDn = new Map(
    state.adGroups.map((group) => [String(group.dn || "").toLowerCase(), group]),
  );
  selectedByDn.forEach((group, key) => {
    if (!catalogByDn.has(key)) catalogByDn.set(key, group);
  });
  select.innerHTML = Array.from(catalogByDn.values())
    .filter((group) => group && group.dn)
    .sort((left, right) => String(left.name || left.dn).localeCompare(String(right.name || right.dn), "ru"))
    .map((group) => {
      const dn = String(group.dn);
      const details = [
        group.company,
        group.directory_domain || group.domain,
        group.description,
        group.mail,
      ].filter(Boolean).join(" · ");
      const label = details ? `${group.name || dn} · ${details}` : (group.name || dn);
      return `<option value="${escapeHtml(dn)}"${selectedByDn.has(dn.toLowerCase()) ? " selected" : ""}>${escapeHtml(label)}</option>`;
    })
    .join("");
}

function employeeSelectOptions(employee) {
  const selectedId = String((employee && employee.id) || "");
  const employees = [...state.employeeOptions];
  if (selectedId && !employees.some((row) => String(row.id) === selectedId)) {
    employees.push(employee);
  }
  const options = employees
    .filter((row) => row.id)
    .map((row) => {
      const details = [row.ad_login || "без AD", row.email, row.department].filter(Boolean).join(" · ");
      const label = details ? `${row.name || row.ad_login} · ${details}` : (row.name || row.ad_login);
      return `<option value="${escapeHtml(row.id)}" data-ad-login="${escapeHtml(row.ad_login)}"${String(row.id) === selectedId ? " selected" : ""}>${escapeHtml(label)}</option>`;
    });
  return [
    `<option value="">${state.employeeOptionsError ? "Каталог сотрудников недоступен" : "Без привязки"}</option>`,
    ...options,
  ].join("");
}

function renderMailHealthBanner(health, errorMessage = "") {
  const banner = $("#mailHealthBanner");
  if (!banner) return;
  const severity = health && health.severity ? health.severity : "fail";
  const control = (health && health.control) || {};
  const ready = (health && health.ready) || {};
  const delivery = (health && health.delivery) || {};
  const issues = errorMessage
    ? [errorMessage]
    : Array.isArray(health && health.issues) ? health.issues.slice(0, 4) : [];
  const queued = Number(delivery.pending || 0) + Number(delivery.sending || 0);
  const controlOk = Boolean(
    Object.prototype.hasOwnProperty.call(control, "mail_control_ok")
      ? control.mail_control_ok
      : control.mail_vm_ssh_ok
  );
  const primaryMode = ready.mode === "primary";
  const readyChecks = ready.checks || {};
  const adminAvailable = Boolean(ready.mail_admin_available);
  const imapOk = primaryMode
    ? Boolean(readyChecks.imap_public && readyChecks.imap_public.ok)
    : controlOk;
  const smtpOk = primaryMode
    ? Boolean(readyChecks.smtp && readyChecks.smtp.ok)
    : Boolean(control.smtp_proxy_ok);
  const systemOk = severity === "ok" && (!primaryMode || adminAvailable);
  const healthPills = [
    `<span class="status-pill ${systemOk ? "good" : healthPillClass(severity)}"><span>Система</span><span class="health-value">: ${systemOk ? "работает" : severity === "warn" ? "есть замечания" : "ошибка"}</span></span>`,
    `<span class="status-pill ${imapOk ? "good" : "bad"}"><span>IMAP</span><span class="health-value">: ${imapOk ? "работает" : "недоступен"}</span></span>`,
    `<span class="status-pill ${smtpOk ? "good" : "bad"}"><span>SMTP</span><span class="health-value">: ${smtpOk ? "работает" : "недоступен"}</span></span>`,
    `<span class="status-pill ${queued > 0 ? "warn" : "good"}"><span>Очередь</span><span class="health-value">: ${escapeHtml(String(queued))}</span></span>`,
  ];
  banner.className = `mail-health-banner ${severity}`;
  if (!health) banner.classList.add("loading");
  $("#mailHealthIcon").textContent = mailHealthIcon(severity);
  $("#mailHealthHeadline").textContent = errorMessage
    ? translateHealthText(errorMessage)
    : translateHealthText((health && health.headline) || "Mail health unavailable");
  $("#mailHealthCheckedAt").textContent = health && health.checked_at ? formatTs(health.checked_at) : "—";
  $("#mailHealthPills").innerHTML = healthPills.join("");
  const issuesBox = $("#mailHealthIssues");
  if (!issues.length) {
    issuesBox.innerHTML = "";
    issuesBox.classList.add("hidden");
  } else {
    issuesBox.innerHTML = issues.map((issue) => `<span class="issue-pill">${escapeHtml(translateHealthText(issue))}</span>`).join("");
    issuesBox.classList.remove("hidden");
  }
}

function scheduleMailHealthPoll(delay = MAIL_HEALTH_POLL_MS) {
  clearTimeout(state.mailHealthTimer);
  state.mailHealthTimer = setTimeout(() => {
    loadMailHealth({ silent: true });
  }, delay);
}

async function loadMailHealth(options = {}) {
  const silent = Boolean(options.silent);
  try {
    const data = await apiJson("/api/adminportal/mail-health");
    const health = data.health || {};
    const prevKey = state.mailHealthKey;
    renderMailHealthBanner(health);
    if (prevKey && health.key && prevKey !== health.key) {
      toast(`Состояние изменилось: ${translateHealthText(health.headline || "state changed")}`, health.severity === "ok");
    }
    state.mailHealthKey = health.key || "";
  } catch (error) {
    renderMailHealthBanner(null, error.message || "Состояние почтовой системы недоступно");
    if (!silent) toast(translateHealthText(error.message || "Состояние почтовой системы недоступно"), false);
  } finally {
    if (state.authenticated) scheduleMailHealthPoll();
  }
}

function startMailHealthPolling() {
  clearTimeout(state.mailHealthTimer);
  if (!state.authenticated) return;
  loadMailHealth({ silent: true });
}

async function checkAuth() {
  try {
    const auth = await apiJson("/api/adminportal/auth/status");
    if (!auth.authenticated) return;
    const data = await apiJson("/api/adminportal/me");
    showApp(data.username, data.profile_card);
  } catch (_) {}
}

async function doLogin() {
  const username = $("#loginUser").value.trim();
  const password = $("#loginPass").value;
  const btn = $("#loginBtn");
  const err = $("#loginError");
  err.textContent = "";
  if (!username || !password) {
    err.textContent = "Введите логин и пароль";
    return;
  }
  btn.disabled = true;
  btn.textContent = "Проверяю…";
  try {
    const data = await apiJson("/api/adminportal/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    showApp(data.username, data.profile_card);
  } catch (error) {
    err.textContent = error.message || "Ошибка авторизации";
  } finally {
    btn.disabled = false;
    btn.textContent = "Войти с логином AD";
  }
}

async function doLogout() {
  clearTimeout(state.mailHealthTimer);
  $$(".modal-shell:not(.hidden)").forEach((modal) => closeModal(modal.id, false));
  modalReturnFocus.clear();
  $("#loginPass").value = "";
  $("#pwdNew").value = "";
  try {
    const response = await fetch(API + "/api/adminportal/logout", { method: "POST" });
    if (!response.ok && response.status !== 401) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.detail || data.error || "Не удалось завершить сессию");
    }
    state.authenticated = false;
    window.location.reload();
  } catch (error) {
    toast(error.message || "Не удалось завершить сессию", false);
  }
}

function showApp(username, profileCard = null) {
  state.authenticated = true;
  state.sessionExpiryHandled = false;
  $("#loginPass").value = "";
  $("#loginPage").classList.add("hidden");
  $("#appPage").classList.remove("hidden");
  $("#sidebarUser").textContent = username;
  state.profileCard = profileCard;
  renderAdminProfileCard(profileCard, username);
  startMailHealthPolling();
  showAdminSection("overview");
}

function sectionForPage(page) {
  return Object.entries(ADMIN_SECTIONS).find(([, value]) =>
    value.pages.includes(page)
  )?.[0] || "overview";
}

function bindRovingNavigation(selector, activate) {
  $$(selector).forEach((item) => {
    item.addEventListener("keydown", (event) => {
      const items = $$(selector).filter((candidate) => !candidate.disabled);
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

function activateAdminNavigation(button) {
  showAdminSection(button.dataset.section);
}

function announceAdminWorkspace() {
  const label = document.querySelector(
    `.sidebar-nav [data-section="${state.activeSection}"] span`,
  )?.textContent?.trim() || "Раздел";
  const live = $("#admin-workspace-live");
  if (live) live.textContent = `${label} открыт`;
}

function syncAdminPrimaryAction() {
  const config = ADMIN_PRIMARY_ACTIONS[state.activeSection];
  const button = $("#createMailboxBtn");
  button.classList.toggle("hidden", !config);
  if (!config) {
    delete button.dataset.action;
    return;
  }
  button.dataset.action = state.activeSection;
  button.setAttribute("aria-label", config.label);
  button.querySelector(".action-label-full").textContent = config.label;
  button.querySelector(".action-label-short").textContent = config.label.replace("Создать ", "");
}

function showAdminPage(page, loadData = true) {
  state.activePage = page;
  state.activeSection = sectionForPage(page);
  $$(".page").forEach((item) => {
    item.classList.toggle("active", item.id === `page-${page}`);
  });
  $$("[data-section]").forEach((button) => {
    const active = button.dataset.section === state.activeSection;
    button.classList.toggle("active", active);
    button.setAttribute("aria-current", active ? "page" : "false");
    button.setAttribute("tabindex", active ? "0" : "-1");
  });
  $$("[data-admin-page]").forEach((tab) => {
    const active = tab.dataset.adminPage === page;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-current", active ? "page" : "false");
  });
  const meta = PAGE_META[page] || PAGE_META.desk;
  $("#appPageTitle").textContent = meta.title;
  $("#appPageSubtitle").textContent = meta.subtitle;
  syncAdminPrimaryAction();
  const mobileMoreActive = ["audit", "settings"].includes(state.activeSection);
  $("#mobileMoreBtn").classList.toggle("active", mobileMoreActive);
  $("#mobileMoreBtn").setAttribute("aria-current", mobileMoreActive ? "page" : "false");
  closeMobileDrawer();
  if (loadData) loadAdminPageData(page);
  announceAdminWorkspace();
}

function showAdminSection(section, loadData = true) {
  const config = ADMIN_SECTIONS[section] || ADMIN_SECTIONS.overview;
  state.activeSection = ADMIN_SECTIONS[section] ? section : "overview";
  showAdminPage(config.defaultPage, loadData);
}

function showPage(page, loadData = true) {
  showAdminPage(page, loadData);
}

function loadAdminPageData(page) {
  if (page === "desk") loadDesk();
  if (page === "accounts") loadAccounts();
  if (page === "domains") loadDomainMailboxes();
  if (page === "managed-addresses") loadManagedAddresses();
  if (page === "queue") loadQueue();
  if (page === "aliases") loadAliases();
  if (page === "audit") loadAudit();
  if (page === "stats") loadStats();
  if (page === "settings") loadAdminSettings();
}

// ---- Multi-domain mailboxes (additional hosted domains via DMS-direct) ----
async function loadMailDomainsSelect() {
  const select = $("#domainMailboxDomain");
  if (!select) return;
  try {
    const data = await apiJson("/api/adminportal/mail-domains");
    const manageable = (data.domains || []).filter((d) => d.manageable);
    if (!manageable.length) {
      select.innerHTML = `<option value="">Нет дополнительных доменов</option>`;
      select.disabled = true;
      return;
    }
    const prev = select.value;
    select.disabled = false;
    select.innerHTML = manageable
      .map((d) => `<option value="${escapeHtml(d.domain)}">${escapeHtml(d.domain)}</option>`)
      .join("");
    if (prev && manageable.some((d) => d.domain === prev)) select.value = prev;
  } catch (err) {
    select.innerHTML = `<option value="">Ошибка: ${escapeHtml(err.message)}</option>`;
    select.disabled = true;
  }
}

async function loadDomainMailboxes() {
  await loadMailDomainsSelect();
  const select = $("#domainMailboxDomain");
  const body = $("#domainMailboxBody");
  if (!body) return;
  const domain = select && select.value;
  if (!domain) {
    body.innerHTML = `<tr><td colspan="2" class="empty-cell">Выберите дополнительный домен</td></tr>`;
    return;
  }
  body.innerHTML = `<tr><td colspan="2" class="empty-cell">Загрузка…</td></tr>`;
  try {
    const data = await apiJson(`/api/adminportal/domain-mailboxes?domain=${encodeURIComponent(domain)}`);
    const boxes = data.mailboxes || [];
    $("#domainMailboxCount").textContent = String(data.count || boxes.length);
    if (!boxes.length) {
      body.innerHTML = `<tr><td colspan="2" class="empty-cell">Ящиков нет</td></tr>`;
      return;
    }
    body.innerHTML = boxes.map((email) => `
      <tr>
        <td>${escapeHtml(email)}</td>
        <td class="row-actions">
          <button class="ghost-btn compact" data-domain-pw="${escapeHtml(email)}" type="button">Пароль</button>
          <button class="ghost-btn compact danger" data-domain-del="${escapeHtml(email)}" type="button">Удалить</button>
        </td>
      </tr>`).join("");
  } catch (err) {
    body.innerHTML = `<tr><td colspan="2" class="empty-cell">Ошибка: ${escapeHtml(err.message)}</td></tr>`;
  }
}

async function openCreateDomainMailbox() {
  const select = $("#domainMailboxDomain");
  const domain = select && select.value;
  if (!domain) { alert("Сначала выберите дополнительный домен"); return; }
  const local = (prompt(`Локальная часть нового ящика на @${domain}:`) || "").trim();
  if (!local) return;
  try {
    const data = await apiJson("/api/adminportal/domain-mailboxes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ local, domain }),
    });
    alert(`Создан ${data.email}\nПароль: ${data.password}`);
    loadDomainMailboxes();
  } catch (err) { alert(`Ошибка: ${err.message}`); }
}

async function deleteDomainMailbox(email) {
  if (!confirm(`Удалить ящик ${email}? Данные будут безвозвратно удалены.`)) return;
  try {
    await apiJson("/api/adminportal/domain-mailboxes/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email }),
    });
    loadDomainMailboxes();
  } catch (err) { alert(`Ошибка: ${err.message}`); }
}

async function resetDomainMailboxPassword(email) {
  if (!confirm(`Сбросить пароль для ${email}?`)) return;
  try {
    const data = await apiJson("/api/adminportal/domain-mailboxes/password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email }),
    });
    alert(`Новый пароль для ${data.email}:\n${data.password}`);
  } catch (err) { alert(`Ошибка: ${err.message}`); }
}

async function loadAdminSettings() {
  const status = $("#mailSettingsStatus");
  try {
    const [data] = await Promise.all([
      apiJson("/api/adminportal/settings"),
      loadAdGroups(),
    ]);
    const mail = data.mail || {};
    const imap = mail.imap || {};
    const smtp = mail.smtp || {};
    $("#mailSettingsDomain").textContent = mail.domain || "—";
    $("#mailSettingsWebmailUrl").textContent = mail.webmail_url || "—";
    $("#mailSettingsWebmailUrl").href = mail.webmail_url || "#";
    $("#mailSettingsImap").textContent = imap.host
      ? `${imap.host}:${imap.port || "—"} · ${imap.security || "—"}`
      : "—";
    $("#mailSettingsSmtp").textContent = smtp.host
      ? `${smtp.host}:${smtp.port || "—"} · ${smtp.security || "—"}`
      : "—";
    $("#mailSettingsAdminUrl").textContent = mail.admin_url || window.location.origin;
    const identity = data.identity || {};
    const ldaps = identity.ldaps || {};
    $("#identitySsoStatus").textContent = "Через AD";
    $("#identitySsoStatus").className = "identity-ok";
    $("#identitySsoDetails").textContent = "Только корпоративная учётная запись AD";
    $("#identityLdapsStatus").textContent = ldaps.configured ? "Настроено" : "Не настроено";
    $("#identityLdapsStatus").className = ldaps.configured ? "identity-ok" : "identity-bad";
    $("#identityLdapsDetails").textContent = state.adGroupsError
      || [ldaps.servers && ldaps.servers.join(", "), ldaps.base_dn, ldaps.bind_user].filter(Boolean).join(" · ")
      || "Нет публичных параметров";
    renderAdGroupOptions(
      "#identityAdminGroups",
      identity.allowed_admin_groups || [],
    );
    status.className = "result-box hidden";
  } catch (error) {
    renderAdminSectionState(status, {
      message: error.message || "Не удалось загрузить настройки",
      retry: "settings",
    });
  }
  applyAdminTheme(storedAdminThemeMode());
}

async function saveIdentitySettings() {
  const result = $("#identitySettingsResult");
  const button = $("#identitySaveBtn");
  const allowedGroupDns = selectedMailboxValues("#identityAdminGroups");
  if (!allowedGroupDns.length) {
    showResult(result, "Выберите хотя бы одну группу администраторов", true);
    return;
  }
  button.disabled = true;
  try {
    await apiJson("/api/adminportal/settings/identity", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ allowed_group_dns: allowedGroupDns }),
    });
    showResult(result, "Группы доступа сохранены", false);
    await loadAdminSettings();
  } catch (error) {
    showResult(result, error.message || "Не удалось сохранить группы", true);
  } finally {
    button.disabled = false;
  }
}

async function checkIdentitySettings() {
  const result = $("#identitySettingsResult");
  const button = $("#identityCheckBtn");
  button.disabled = true;
  try {
    const data = await apiJson("/api/adminportal/settings/identity/check", {
      method: "POST",
    });
    const message = data.ok
      ? `AD/LDAPS отвечает (${(data.ldaps && data.ldaps.groups_count) || 0} групп)`
      : `Проверка не пройдена: LDAPS ${data.ldaps && data.ldaps.ok ? "готов" : "недоступен"}`;
    showResult(result, message, !data.ok);
    state.adGroupsLoaded = false;
    await loadAdminSettings();
  } catch (error) {
    showResult(result, error.message || "Не удалось проверить интеграцию", true);
  } finally {
    button.disabled = false;
  }
}

async function loadDesk() {
  await Promise.all([loadDeskStats(), runSupportSearch(true)]);
}

async function loadDeskStats() {
  try {
    const stats = await apiJson("/api/adminportal/stats");
    $("#deskMailboxCount").textContent = String(stats.total_mailboxes || 0);
    $("#deskQueueCount").textContent = String(stats.queue_count || 0);
    updateQueueBadge(stats.queue_count || 0);
  } catch (_) {}
}

function scheduleSupportSearch() {
  state.creatingMailbox = false;
  state.selectedSupportKey = "";
  state.selectedSupportItem = null;
  $("#appPage").classList.remove("has-mailbox-selection");
  clearTimeout(state.supportSearchTimer);
  state.supportSearchTimer = setTimeout(() => runSupportSearch(false), 250);
}

async function runSupportSearch(force) {
  const query = $("#supportSearch").value.trim();
  $("#supportMeta").textContent = query ? `Поиск: ${query}` : "Ящики и сотрудники";
  try {
    const data = await apiJson(`/api/adminportal/support/search?q=${encodeURIComponent(query)}&limit=25`);
    state.supportResults = data.results || [];
    $("#supportResultCount").textContent = String(data.total || 0);
    renderSupportResults();
    if (state.selectedSupportKey) {
      const next = state.supportResults.find((item) => item.key === state.selectedSupportKey);
      if (next) {
        state.selectedSupportItem = next;
        await renderSupportWorkspace();
        return;
      }
      state.selectedSupportKey = "";
      state.selectedSupportItem = null;
      $("#appPage").classList.remove("has-mailbox-selection");
    }
    if (!state.creatingMailbox && state.supportResults.length) {
      const firstMailbox = state.supportResults.find((item) => item.mailbox_exists) || state.supportResults[0];
      state.selectedSupportKey = firstMailbox.key;
      state.selectedSupportItem = firstMailbox;
      state.currentWorkspaceTab = "overview";
      renderSupportResults();
      await renderSupportWorkspace();
      return;
    }
    renderSupportWorkspace();
  } catch (error) {
    renderAdminSectionState($("#supportResults"), {
      message: error.message || "Поиск недоступен",
      retry: "desk",
    });
  }
}

function renderSupportResults() {
  const box = $("#supportResults");
  if (!state.supportResults.length) {
    box.innerHTML = `<div class="empty-state"><h3>Ничего не найдено</h3><div class="empty-copy">Введите email, логин или имя сотрудника.</div></div>`;
    return;
  }
  box.innerHTML = state.supportResults.map((item) => {
    const active = item.key === state.selectedSupportKey ? " active" : "";
    const suspended = item.lifecycle_state === "suspended" ? " suspended" : "";
    const title = item.display_name || item.email || item.mail_login || item.ad_login || "Без имени";
    const subtitle = [item.email, item.ad_login, item.department].filter(Boolean).join(" · ");
    const kind = item.kind === "employee" ? "Сотрудник" : "Почтовый ящик";
    const queue = resultQueuePill(item.queue_summary);
    return `
      <article class="support-result${active}${suspended}" data-key="${escapeHtml(item.key)}" tabindex="-1">
        <div class="support-top">
          <div class="support-main">
            <div class="support-title">${escapeHtml(title)}</div>
            <div class="support-copy">${escapeHtml(subtitle || kind)}</div>
          </div>
          ${supportStatusPill(item)}
        </div>
        <div class="support-bottom">
          <span class="pill">${escapeHtml(kind)}</span>
          ${item.mail_login ? `<span class="pill">${escapeHtml(item.mail_login)}</span>` : ""}
          ${queue}
          <button class="chip-btn support-open-btn" data-key="${escapeHtml(item.key)}">Открыть</button>
        </div>
      </article>
    `;
  }).join("");
  $$(".support-result").forEach((result) => {
    result.addEventListener("click", () => {
      selectSupportResult(result.getAttribute("data-key"));
    });
  });
  $$(".support-open-btn").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      selectSupportResult(button.getAttribute("data-key"));
    });
  });
}

function selectSupportResult(key) {
  const item = state.supportResults.find((entry) => entry.key === key);
  if (!item) return;
  state.creatingMailbox = false;
  state.selectedSupportKey = key;
  state.selectedSupportItem = item;
  state.currentWorkspaceTab = "overview";
  $("#appPage").classList.add("has-mailbox-selection");
  renderSupportResults();
  renderSupportWorkspace();
}

function restoreSupportResultList() {
  $("#appPage").classList.remove("has-mailbox-selection");
  const selected = $$(".support-result").find(
    (result) => result.getAttribute("data-key") === state.selectedSupportKey,
  );
  if (!selected) return;
  selected.focus({ preventScroll: true });
  selected.scrollIntoView({ block: "nearest" });
}

function selectSupportMailbox(email) {
  state.creatingMailbox = false;
  $("#supportSearch").value = email;
  showPage("desk", false);
  runSupportSearch(true).then(() => {
    const match = state.supportResults.find((item) => (item.email || "").toLowerCase() === email.toLowerCase());
    if (match) selectSupportResult(match.key);
  });
}

async function openCreateMailbox() {
  state.creatingMailbox = true;
  state.selectedSupportKey = "";
  state.selectedSupportItem = null;
  state.currentDetail = null;
  state.currentTimeline = null;
  state.currentDoctor = null;
  $("#appPage").classList.remove("has-mailbox-selection");
  $("#supportSearch").value = "";
  showPage("desk", false);
  renderSupportResults();
  await renderSupportWorkspace();
  const input = $("#provisionMailLogin");
  if (input) input.focus();
}

async function fetchAccountDetail(email) {
  const data = await apiJson(`/api/adminportal/accounts/detail?email=${encodeURIComponent(email)}`);
  return data.detail || {};
}

async function fetchMailboxTimeline(email, filterName = "all") {
  const params = new URLSearchParams({ email, hours: "72", filter: filterName || "all" });
  return apiJson(`/api/adminportal/mailboxes/timeline?${params.toString()}`);
}

async function fetchMailboxDoctor(email) {
  const data = await apiJson(`/api/adminportal/mailboxes/doctor?email=${encodeURIComponent(email)}`);
  return data.doctor || {};
}

async function renderSupportWorkspace() {
  const box = $("#supportWorkspace");
  const item = state.selectedSupportItem;
  const requestId = ++state.workspaceRequestId;
  if (!item) {
    $("#appPage").classList.remove("has-mailbox-selection");
    state.currentDetail = null;
    state.currentTimeline = null;
    state.currentDoctor = null;
    box.innerHTML = renderProvisionWorkspace(null);
    bindProvisionWorkspace(null);
    return;
  }

  box.innerHTML = `<div class="empty-state"><h3>Загружаю данные</h3><div class="empty-copy">${escapeHtml(item.display_name || item.email || item.key)}</div></div>`;
  if (item.mailbox_exists && item.email) {
    try {
      const detailPromise = fetchAccountDetail(item.email);
      const employeesPromise = loadEmployeeOptions();
      const secondaryPromise = Promise.allSettled([
        fetchMailboxTimeline(item.email, state.currentTimelineFilter || "all"),
        fetchMailboxDoctor(item.email),
      ]);
      const detail = await detailPromise;
      await employeesPromise;
      if (requestId !== state.workspaceRequestId || item.key !== state.selectedSupportKey) return;
      state.currentDetail = detail;
      state.currentTimeline = null;
      state.currentDoctor = null;
      box.innerHTML = renderMailboxWorkspace(item, detail, null, null);
      bindMailboxWorkspace(item, detail, null, null);
      const [timelineResult, doctorResult] = await secondaryPromise;
      if (requestId !== state.workspaceRequestId || item.key !== state.selectedSupportKey) return;
      state.currentTimeline = timelineResult.status === "fulfilled"
        ? timelineResult.value
        : { error: timelineResult.reason?.message || "События недоступны", events: [], summary: {}, filter: state.currentTimelineFilter || "all" };
      state.currentDoctor = doctorResult.status === "fulfilled"
        ? doctorResult.value
        : { error: doctorResult.reason?.message || "Диагностика недоступна" };
      box.innerHTML = renderMailboxWorkspace(item, detail, state.currentTimeline, state.currentDoctor);
      bindMailboxWorkspace(item, detail, state.currentTimeline, state.currentDoctor);
    } catch (error) {
      if (requestId !== state.workspaceRequestId) return;
      renderAdminSectionState($("#supportWorkspace"), {
        message: error.message || "Не удалось открыть ящик",
        retry: "desk",
      });
    }
  } else {
    if (requestId !== state.workspaceRequestId) return;
    state.currentDetail = null;
    state.currentTimeline = null;
    state.currentDoctor = null;
    box.innerHTML = renderProvisionWorkspace(item);
    bindProvisionWorkspace(item);
  }
}

function renderProvisionWorkspace(item) {
  const title = item
    ? `Создание ящика для ${escapeHtml(item.display_name || item.ad_login || item.key)}`
    : "Создание почтового ящика";
  const subtitle = item
    ? "Сотрудник найден, но ящик ещё не создан. Выберите почтовый логин и задайте пароль."
    : "Создайте общий, сервисный или временный почтовый ящик.";
  const suggested = escapeHtml((item && item.suggested_mail_login) || "");
  const identity = item ? `
      <div class="workspace-card-block">
        <div class="workspace-top">
          <div>
            <div class="workspace-title">${escapeHtml(item.display_name || item.ad_login || "Сотрудник")}</div>
            <div class="workspace-copy">${escapeHtml([item.ad_login, item.department].filter(Boolean).join(" · ") || "Почтовый ящик ещё не создан")}</div>
          </div>
          <span class="status-pill warn">Нужно создать</span>
        </div>
        <div class="detail-kv">
          <div class="k">Логин AD</div><div class="v">${escapeHtml(item.ad_login || "—")}</div>
          <div class="k">Подразделение</div><div class="v">${escapeHtml(item.department || "—")}</div>
          <div class="k">Предлагаемый логин</div><div class="v mono">${escapeHtml(item.suggested_mail_login || "—")}</div>
        </div>
      </div>
    ` : `
      <div class="workspace-card-block">
        <div class="workspace-title">Ящик без привязки к сотруднику</div>
        <div class="workspace-copy">Подходит для общей папки, сервисной учётной записи или временного адреса.</div>
      </div>
    `;

  return `
    <div class="workspace-panel">
      <div class="workspace-card-block">
        <div class="workspace-top">
          <div>
            <div class="workspace-title">${title}</div>
            <div class="workspace-copy">${subtitle}</div>
          </div>
          ${item ? `<button class="ghost-btn" id="clearSelectionBtn">Снять выбор</button>` : ""}
        </div>
      </div>
      ${identity}
      <div class="provision-layout">
        <div class="workspace-card-block provision-card">
          <h4>Параметры ящика</h4>
          <div>
            <label class="field-label" for="provisionMailLogin">Почтовый логин</label>
            <input type="text" id="provisionMailLogin" class="text-field mono" placeholder="i.ivanov" value="${suggested}">
          </div>
          <div>
            <label class="field-label" for="provisionPassword">Пароль</label>
            <input type="text" id="provisionPassword" class="text-field mono" placeholder="Оставьте пустым для автогенерации">
          </div>
          <div id="provisionCheckBox" class="result-box hidden"></div>
          <div id="provisionResult" class="result-box hidden"></div>
          <div class="inline-actions">
            <button class="primary-btn" id="provisionSubmitBtn">Создать ящик</button>
            <button class="ghost-btn" id="provisionCheckBtn">Проверить логин</button>
          </div>
        </div>
        <div class="workspace-card-block provision-card">
          <h4>Данные подключения</h4>
          <div class="subtle">После создания панель покажет email, пароль, параметры IMAP/SMTP и QR-коды для Thunderbird и Delta Chat.</div>
          <div class="detail-kv">
            <div class="k">Email</div><div class="v mono" id="provisionPreviewEmail">${suggested ? `${suggested}@${PRIMARY_MAIL_DOMAIN}` : "—"}</div>
            <div class="k">Привязка</div><div class="v">${item ? "К сотруднику" : "Без сотрудника"}</div>
            <div class="k">Результат</div><div class="v">Окно с параметрами ящика</div>
          </div>
        </div>
      </div>
    </div>
  `;
}

function bindProvisionWorkspace(item) {
  const input = $("#provisionMailLogin");
  const clearBtn = $("#clearSelectionBtn");
  if (clearBtn) {
    clearBtn.addEventListener("click", () => {
      state.selectedSupportItem = null;
      state.selectedSupportKey = "";
      renderSupportResults();
      renderSupportWorkspace();
    });
  }
  if (input) {
    input.addEventListener("input", () => {
      $("#provisionPreviewEmail").textContent = input.value.trim()
        ? `${input.value.trim().toLowerCase()}@${PRIMARY_MAIL_DOMAIN}`
        : "—";
      scheduleOnboardingCheck();
    });
  }
  const checkBtn = $("#provisionCheckBtn");
  if (checkBtn) checkBtn.addEventListener("click", () => runOnboardingCheck(true));
  const submitBtn = $("#provisionSubmitBtn");
  if (submitBtn) submitBtn.addEventListener("click", () => submitProvision(item));
  if (input && input.value.trim()) runOnboardingCheck(false);
}

function scheduleOnboardingCheck() {
  clearTimeout(state.onboardingCheckTimer);
  state.onboardingCheckTimer = setTimeout(() => runOnboardingCheck(false), 220);
}

async function runOnboardingCheck(showToast) {
  const login = $("#provisionMailLogin");
  const box = $("#provisionCheckBox");
  if (!login || !box) return;
  const value = login.value.trim();
  box.className = "result-box hidden";
  if (!value) return;
  try {
    const data = await apiJson(`/api/adminportal/onboarding/check?mail_login=${encodeURIComponent(value)}`);
    const check = data.check || {};
    let message = `Email: ${check.email || "—"}`;
    let isError = false;
    if (check.available) {
      message += "\nСтатус: логин свободен";
    } else if (check.mailbox_exists) {
      message += "\nСтатус: почтовый ящик уже существует";
      isError = true;
    } else if (check.alias_exists) {
      message += `\nСтатус: алиас уже принадлежит ${check.owner && check.owner.ad_login ? check.owner.ad_login : "другому сотруднику"}`;
      isError = true;
    }
    showResult(box, message, isError);
    if (showToast) toast(isError ? "Почтовый логин занят" : "Почтовый логин свободен", !isError);
  } catch (error) {
    showResult(box, error.message || "Ошибка проверки", true);
  }
}

async function submitProvision(item) {
  const mailLogin = $("#provisionMailLogin").value.trim().toLowerCase();
  const password = $("#provisionPassword").value.trim();
  const result = $("#provisionResult");
  result.className = "result-box hidden";
  if (!mailLogin) {
    showResult(result, "Укажите почтовый логин", true);
    return;
  }
  try {
    await withBusyButton($("#provisionSubmitBtn"), "Создаём…", async () => {
      const payload = {
        mail_login: mailLogin,
        password,
        emp_id: item && item.emp_id ? item.emp_id : null,
      };
      const data = await apiJson("/api/adminportal/onboarding/provision", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      showResult(result, `Создан: ${data.email}`, false);
      toast(`Создан ${data.email}`, true);
      openCredentialPack(data.credential_pack, `Параметры ящика · ${data.email}`);
      $("#supportSearch").value = data.email;
      state.selectedSupportItem = null;
      state.selectedSupportKey = "";
      await Promise.all([loadAccounts(), loadDeskStats()]);
      await runSupportSearch(true);
      const match = state.supportResults.find((entry) => (entry.email || "").toLowerCase() === String(data.email || "").toLowerCase());
      if (match) selectSupportResult(match.key);
    });
  } catch (error) {
    showResult(result, error.message || "Ошибка создания", true);
  }
}

function renderMailboxWorkspace(item, detail, timelinePayload, doctorPayload) {
  const employee = detail.employee || null;
  const alias = detail.alias || null;
  const mailbox = detail.mailbox || null;
  const queueSummary = detail.queue_summary || { total: 0, active: 0, deferred: 0, hold: 0 };
  const queueEntries = detail.queue_entries || [];
  const adLogin = (employee && employee.ad_login) || (alias && alias.ad_login) || "";
  const aliasLogin = (alias && alias.mail_login) || detail.mail_login || "";
  const managedAddress = detail.managed_address || null;
  const identityTitle = employee && employee.name ? employee.name : (item.display_name || detail.email || "Почтовый ящик");
  const lifecycleState = detail.lifecycle_state || item.lifecycle_state || "active";
  const timeline = timelinePayload || { events: [], summary: {}, filter: state.currentTimelineFilter || "all" };
  const incidentSummary = timeline.summary || detail.incident_summary || {};
  const timelineFilter = timeline.filter || state.currentTimelineFilter || "all";
  const events = timeline.events || [];
  const timelineLoading = !timelinePayload;
  const timelineError = timelinePayload && timelinePayload.error ? String(timelinePayload.error) : "";
  const doctor = doctorPayload && !doctorPayload.error ? doctorPayload : null;
  const doctorLoading = !doctorPayload;
  const doctorError = doctorPayload && doctorPayload.error ? String(doctorPayload.error) : "";
  const delivery = (doctor && doctor.delivery) || {};
  const deliveryLatest = delivery.latest || null;
  const resetDisabled = lifecycleState === "suspended" || Boolean(managedAddress);
  const latestIssue = incidentSummary.latest_issue || "";
  const activeTab = ["overview", "diagnostics", "events", "settings"].includes(state.currentWorkspaceTab)
    ? state.currentWorkspaceTab
    : "overview";
  const sharedAccess = managedAddress ? [] : (detail.shared_access || []);
  const sharedAccessHtml = sharedAccess.length
    ? sharedAccess.map((access) => {
        const inheritedLabel = access.inherited
          ? `Через AD: ${(access.inherited_groups || []).join(", ") || "группа"}`
          : "";
        const effectiveLabel = access.connected
          ? (access.can_send ? "Доступ и отправка" : "Доступ без отправки")
          : "Нет доступа";
        return `
          <div class="workspace-shared-row" data-mailbox-id="${escapeHtml(access.id)}">
            <div class="workspace-shared-identity">
              <strong>${escapeHtml(access.display_name || access.email)}</strong>
              <span class="mono">${escapeHtml(access.email)}</span>
              <span class="workspace-copy">${escapeHtml(effectiveLabel)}${inheritedLabel ? ` · ${escapeHtml(inheritedLabel)}` : ""}</span>
            </div>
            <label class="workspace-shared-control">
              <input class="workspace-shared-connect" type="checkbox" data-mailbox-id="${escapeHtml(access.id)}" ${access.direct ? "checked" : ""}>
              <span>Прямая привязка</span>
            </label>
            <label class="workspace-shared-control">
              <input class="workspace-shared-send" type="checkbox" data-mailbox-id="${escapeHtml(access.id)}" ${access.direct_can_send ? "checked" : ""} ${access.direct ? "" : "disabled"}>
              <span>Отправка</span>
            </label>
          </div>
        `;
      }).join("")
    : `<div class="empty-copy">Общие и сервисные ящики ещё не созданы.</div>`;

  const queueHtml = queueEntries.length ? queueEntries.slice(0, 5).map((entry) => `
    <article class="queue-preview-row">
      <div class="queue-preview-main">
        <strong class="mono">${escapeHtml(entry.queue_id || "—")}</strong>
        <span>${escapeHtml(entry.time || "")} · ${humanSize(entry.size || 0)}</span>
        <span>От: ${escapeHtml(entry.sender || "—")}</span>
        <span>Кому: ${escapeHtml((entry.recipients || []).join(", ") || "—")}</span>
      </div>
      <span class="queue-status ${queueStatus(entry)}">${queueStatusLabel(entry)}</span>
      ${entry.error ? `<div class="details-code">${escapeHtml(entry.error)}</div>` : ""}
      <div class="inline-actions">
        <button class="ghost-btn workspace-open-queue-btn" data-target="${escapeHtml(entry.queue_id || "")}">Открыть в очереди</button>
        <button class="danger-btn workspace-delete-queue-btn" data-target="${escapeHtml(entry.queue_id || "")}">Удалить запись</button>
      </div>
    </article>
  `).join("") : `<div class="empty-state"><h3>Очередь пуста</h3><div class="empty-copy">Для этого ящика нет сообщений в очереди.</div></div>`;

  const summaryHtml = timelineLoading ? `
    <div class="summary-strip">
      <span class="summary-pill">Загружаю сводку…</span>
    </div>
  ` : `
    <div class="summary-strip">
      <span class="summary-pill ${summaryPillClass(incidentSummary.queue_total || queueSummary.total || 0)}">В очереди: ${escapeHtml(String(incidentSummary.queue_total || queueSummary.total || 0))}</span>
      <span class="summary-pill ${summaryPillClass(incidentSummary.auth_failures || 0)}">Ошибки входа: ${escapeHtml(String(incidentSummary.auth_failures || 0))}</span>
      <span class="summary-pill ${summaryPillClass(incidentSummary.delivery_failures || 0)}">Ошибки доставки: ${escapeHtml(String(incidentSummary.delivery_failures || 0))}</span>
      <span class="summary-pill ${summaryPillClass(incidentSummary.lifecycle_events || 0, 2)}">Изменения состояния: ${escapeHtml(String(incidentSummary.lifecycle_events || 0))}</span>
    </div>
  `;

  const timelineHtml = timelineLoading
    ? `<div class="empty-state"><h3>Загружаю события</h3><div class="empty-copy">Собираю очередь, аудит и события почтового сервера.</div></div>`
    : timelineError
      ? `<div class="empty-state"><h3>События недоступны</h3><div class="empty-copy">${escapeHtml(timelineError)}</div></div>`
      : events.length ? events.map((event) => `
        <article class="timeline-event ${escapeHtml(event.severity || "info")}">
          <div class="workspace-title-row">
            <div>
              <div class="timeline-summary">${escapeHtml(event.summary || "Событие")}</div>
              <div class="workspace-copy">${escapeHtml(formatTs(event.ts))}</div>
            </div>
            <div class="timeline-meta">
              ${timelineCategoryPill(event.category)}
              <span class="pill">${escapeHtml(event.source || "Система")}</span>
              ${severityPill(event.severity)}
            </div>
          </div>
          ${event.details ? `<div class="details-code timeline-details">${escapeHtml(event.details)}</div>` : ""}
        </article>
      `).join("") : `<div class="empty-state"><h3>Событий нет</h3><div class="empty-copy">За последние 72 часа по выбранному фильтру ничего не найдено.</div></div>`;

  const doctorControlOk = Boolean(
    doctor
      && (
        Object.prototype.hasOwnProperty.call(doctor, "mail_control_ok")
          ? doctor.mail_control_ok
          : doctor.mail_vm_ssh_ok
      )
  );
  const doctorControlStatus = doctor
    ? doctor.mail_control_status || doctor.mail_vm_ssh_status || "—"
    : "—";
  const doctorHtml = doctorLoading
    ? `<div class="empty-state"><h3>Выполняю диагностику</h3><div class="empty-copy">Проверяю ящик, пароль, IMAP, наблюдение и контур управления.</div></div>`
    : doctorError
      ? `<div class="empty-state"><h3>Диагностика недоступна</h3><div class="empty-copy">${escapeHtml(doctorError)}</div></div>`
      : `
        <div class="service-check-grid">
          <div class="service-check"><span>IMAP</span><strong class="${doctor.verified ? "good-text" : doctor.hard_failure ? "bad-text" : "warn-text"}">${doctor.verified ? "Работает" : doctor.hard_failure ? "Ошибка входа" : "Требует проверки"}</strong></div>
          <div class="service-check"><span>Сохранённый пароль</span><strong class="${doctor.stored_password ? "good-text" : "bad-text"}">${doctor.stored_password ? "Есть" : "Нет"}</strong></div>
          <div class="service-check"><span>Почтовый ящик</span><strong class="${doctor.mailbox_exists ? "good-text" : "bad-text"}">${doctor.mailbox_exists ? "Найден" : "Не найден"}</strong></div>
          <div class="service-check"><span>Наблюдение</span><strong class="${doctor.mail_notify_enabled ? "good-text" : "warn-text"}">${doctor.mail_notify_enabled ? "Включено" : "Выключено"}</strong></div>
          <div class="service-check"><span>Контур управления</span><strong class="${doctorControlOk ? "good-text" : "bad-text"}">${doctorControlOk ? "Работает" : "Недоступен"}</strong></div>
          <div class="service-check"><span>SMTP-прокси</span><strong class="${doctor.smtp_proxy_ok ? "good-text" : "bad-text"}">${doctor.smtp_proxy_ok ? "Работает" : "Недоступен"}</strong></div>
        </div>
        <div class="summary-strip">
          <span class="status-pill ${Number(delivery.failed || 0) > 0 ? "bad" : "good"}">Ошибки отправки: ${escapeHtml(String(delivery.failed || 0))}</span>
          <span class="status-pill ${Number(delivery.pending || 0) + Number(delivery.sending || 0) > 0 ? "warn" : "good"}">В очереди: ${escapeHtml(String((delivery.pending || 0) + (delivery.sending || 0)))}</span>
          <span class="status-pill ${Number(delivery.recent_sent_24h || 0) > 0 ? "good" : "warn"}">Отправлено за 24 часа: ${escapeHtml(String(delivery.recent_sent_24h || 0))}</span>
        </div>
        <div class="detail-kv">
          <div class="k">Email</div><div class="v mono">${escapeHtml(doctor.email || detail.email || "—")}</div>
          <div class="k">Логин AD</div><div class="v mono">${escapeHtml(doctor.ad_login || adLogin || "—")}</div>
          <div class="k">Почтовый логин</div><div class="v mono">${escapeHtml(doctor.mail_login || detail.mail_login || "—")}</div>
          <div class="k">Результат проверки</div><div class="v">${escapeHtml(doctor.verify_status || (doctor.verified ? "Подтверждён" : "Не проверен"))}</div>
          <div class="k">Критическая ошибка входа</div><div class="v">${doctor.hard_failure ? "Да" : "Нет"}</div>
          <div class="k">Статус ящика</div><div class="v">${escapeHtml(doctor.mailbox_status || "—")}</div>
          <div class="k">Контур управления</div><div class="v">${escapeHtml(doctorControlStatus)}</div>
          <div class="k">SMTP-прокси</div><div class="v">${escapeHtml(doctor.smtp_proxy_status || "—")}</div>
          <div class="k">Статус доставки</div><div class="v">${escapeHtml(delivery.status || "Ожидание")}</div>
          <div class="k">Ожидают / отправляются</div><div class="v">${escapeHtml(String(delivery.pending || 0))} / ${escapeHtml(String(delivery.sending || 0))}</div>
          <div class="k">Ошибки / отправлено</div><div class="v">${escapeHtml(String(delivery.failed || 0))} / ${escapeHtml(String(delivery.sent || 0))}</div>
          <div class="k">Последняя отправка</div><div class="v">${escapeHtml(deliveryLatest ? `${deliveryLatest.status || "—"} · ${formatTs(deliveryLatest.sent_at || deliveryLatest.scheduled_at || deliveryLatest.created_at)}` : "—")}</div>
          <div class="k">Последний получатель</div><div class="v">${escapeHtml(deliveryLatest && deliveryLatest.to_list ? deliveryLatest.to_list : "—")}</div>
          <div class="k">Последняя ошибка</div><div class="v">${escapeHtml(deliveryLatest && deliveryLatest.last_error ? deliveryLatest.last_error : "—")}</div>
        </div>
      `;

  return `
    <div class="workspace-panel">
      <button class="ghost-btn mailbox-list-back-btn" id="mailboxListBackBtn" type="button">К списку</button>
      <header class="mailbox-identity">
        <div class="mailbox-identity-main">
          <span class="mailbox-avatar" aria-hidden="true">${escapeHtml((identityTitle || detail.email || "?").slice(0, 1).toUpperCase())}</span>
          <div>
            <div class="mailbox-title-row">
              <h3>${escapeHtml(identityTitle)}</h3>
              ${lifecycleStatePill(lifecycleState)}
            </div>
            <div class="workspace-copy mono">${escapeHtml(detail.email || item.email || "—")}</div>
          </div>
        </div>
        <div class="action-row">
          <button class="ghost-btn" id="workspaceOpenMailCabinetBtn" type="button" aria-label="Открыть RuPochta Mail">
            <span class="action-label action-label-full" aria-hidden="true">Открыть RuPochta Mail</span>
            <span class="action-label action-label-short" aria-hidden="true">RuPochta Mail</span>
          </button>
          <button class="ghost-btn" id="workspaceResetPasswordBtn" type="button" aria-label="Сбросить пароль" ${resetDisabled ? "disabled" : ""}>
            <span class="action-label action-label-full" aria-hidden="true">Сбросить пароль</span>
            <span class="action-label action-label-short" aria-hidden="true">Пароль</span>
          </button>
          ${managedAddress ? "" : lifecycleState === "suspended"
            ? `<button class="primary-btn" id="workspaceRestoreMailboxBtn" type="button" aria-label="Восстановить почтовый ящик">
                <span class="action-label action-label-full" aria-hidden="true">Восстановить</span>
                <span class="action-label action-label-short" aria-hidden="true">Восстановить</span>
              </button>`
            : `<button class="warning-btn" id="workspaceSuspendMailboxBtn" type="button" aria-label="Приостановить почтовый ящик">
                <span class="action-label action-label-full" aria-hidden="true">Приостановить</span>
                <span class="action-label action-label-short" aria-hidden="true">Пауза</span>
              </button>`}
          <button class="danger-btn" id="workspaceDeleteMailboxBtn" type="button" aria-label="Удалить почтовый ящик" ${managedAddress ? "disabled" : ""}>
            <span class="action-label action-label-full" aria-hidden="true">Удалить</span>
            <span class="action-label action-label-short" aria-hidden="true">Удалить</span>
          </button>
        </div>
        ${managedAddress
          ? `<div class="inline-notice warn">Это ${escapeHtml(managedAddressKindLabel(managedAddress.kind).toLowerCase())}. Пароль, состав и удаление управляются в разделе «Общие адреса».</div>`
          : resetDisabled ? `<div class="inline-notice">Сброс пароля недоступен, пока ящик приостановлен. Сначала восстановите его.</div>` : ""}
      </header>

      <div class="workspace-tabs" role="tablist" aria-label="Разделы почтового ящика">
        <button class="workspace-tab ${activeTab === "overview" ? "active" : ""}" id="workspace-tab-overview" data-workspace-tab="overview" type="button" role="tab" aria-controls="workspace-panel-overview" aria-selected="${activeTab === "overview"}" tabindex="${activeTab === "overview" ? "0" : "-1"}">Обзор</button>
        <button class="workspace-tab ${activeTab === "diagnostics" ? "active" : ""}" id="workspace-tab-diagnostics" data-workspace-tab="diagnostics" type="button" role="tab" aria-controls="workspace-panel-diagnostics" aria-selected="${activeTab === "diagnostics"}" tabindex="${activeTab === "diagnostics" ? "0" : "-1"}">Диагностика</button>
        <button class="workspace-tab ${activeTab === "events" ? "active" : ""}" id="workspace-tab-events" data-workspace-tab="events" type="button" role="tab" aria-controls="workspace-panel-events" aria-selected="${activeTab === "events"}" tabindex="${activeTab === "events" ? "0" : "-1"}">События</button>
        <button class="workspace-tab ${activeTab === "settings" ? "active" : ""}" id="workspace-tab-settings" data-workspace-tab="settings" type="button" role="tab" aria-controls="workspace-panel-settings" aria-selected="${activeTab === "settings"}" tabindex="${activeTab === "settings" ? "0" : "-1"}">Настройки</button>
      </div>

      <section class="workspace-tab-panel ${activeTab === "overview" ? "active" : ""}" id="workspace-panel-overview" data-workspace-panel="overview" role="tabpanel" aria-labelledby="workspace-tab-overview">
        <div class="overview-grid">
          <section class="ops-card">
            <h4>Учётная запись</h4>
            <div class="detail-kv">
              <div class="k">Email</div><div class="v mono">${escapeHtml(detail.email || "—")}</div>
              <div class="k">Почтовый логин</div><div class="v mono">${escapeHtml(detail.mail_login || "—")}</div>
              <div class="k">Логин AD</div><div class="v mono">${escapeHtml(adLogin || "—")}</div>
              <div class="k">Сотрудник</div><div class="v">${escapeHtml((employee && employee.name) || "—")}</div>
              <div class="k">Подразделение</div><div class="v">${escapeHtml((employee && employee.department) || "—")}</div>
              <div class="k">Должность</div><div class="v">${escapeHtml((employee && employee.title) || "—")}</div>
              <div class="k">Источник алиаса</div><div class="v">${escapeHtml((alias && alias.source) || "—")}</div>
            </div>
          </section>
          <section class="ops-card">
            <h4>Хранилище и очередь</h4>
            <div class="storage-facts">
              <div><span>Размер ящика</span><strong>${escapeHtml((mailbox && mailbox.size_human) || "0 B")}</strong></div>
              <div><span>Всего в очереди</span><strong>${queueSummary.total || 0}</strong></div>
              <div><span>Активные</span><strong>${queueSummary.active || 0}</strong></div>
              <div><span>Отложенные</span><strong>${queueSummary.deferred || 0}</strong></div>
              <div><span>Удержанные</span><strong>${queueSummary.hold || 0}</strong></div>
            </div>
          </section>
        </div>
        <section class="ops-card queue-preview">
          <div class="workspace-title-row">
            <div>
              <h4>Последние сообщения в очереди</h4>
              <div class="ops-copy">Текущие записи, связанные с выбранным ящиком.</div>
            </div>
          </div>
          ${queueHtml}
        </section>
      </section>

      <section class="workspace-tab-panel ${activeTab === "diagnostics" ? "active" : ""}" id="workspace-panel-diagnostics" data-workspace-panel="diagnostics" role="tabpanel" aria-labelledby="workspace-tab-diagnostics">
        <section class="ops-card">
          <h4>Диагностика сервисов</h4>
          <div class="ops-copy">Проверка IMAP, сохранённого пароля, наблюдения, почтового ящика и контура управления.</div>
          ${doctorHtml}
        </section>
      </section>

      <section class="workspace-tab-panel ${activeTab === "events" ? "active" : ""}" id="workspace-panel-events" data-workspace-panel="events" role="tabpanel" aria-labelledby="workspace-tab-events">
        <section class="ops-card">
          <div class="workspace-title-row">
            <div>
              <h4>Последние события</h4>
              <div class="ops-copy">Очередь, аудит и события почтового сервера за последние 72 часа.</div>
            </div>
            <div class="timeline-filter-row">
              <button class="timeline-filter-btn ${timelineFilter === "all" ? "active" : ""}" data-filter="all">Все</button>
              <button class="timeline-filter-btn ${timelineFilter === "delivery" ? "active" : ""}" data-filter="delivery">Доставка</button>
              <button class="timeline-filter-btn ${timelineFilter === "auth" ? "active" : ""}" data-filter="auth">Авторизация</button>
              <button class="timeline-filter-btn ${timelineFilter === "lifecycle" ? "active" : ""}" data-filter="lifecycle">Состояние</button>
            </div>
          </div>
          ${summaryHtml}
          ${latestIssue ? `<div class="inline-notice warn">Последняя проблема: ${escapeHtml(latestIssue)}</div>` : ""}
          <div class="timeline-list">${timelineHtml}</div>
        </section>
      </section>

      <section class="workspace-tab-panel ${activeTab === "settings" ? "active" : ""}" id="workspace-panel-settings" data-workspace-panel="settings" role="tabpanel" aria-labelledby="workspace-tab-settings">
        <div class="settings-grid">
          <section class="ops-card">
            <h4>Состояние ящика</h4>
            <div class="ops-copy">Приостановка блокирует вход, но сохраняет ящик и входящую доставку.</div>
            <div class="detail-kv">
              <div class="k">Состояние</div><div class="v">${escapeHtml(lifecycleStateLabel(lifecycleState))}</div>
              <div class="k">Приостановил</div><div class="v">${escapeHtml(detail.suspended_by || "—")}</div>
              <div class="k">Время приостановки</div><div class="v">${escapeHtml(formatTs(detail.suspended_at))}</div>
              <div class="k">Причина</div><div class="v">${escapeHtml(detail.suspension_reason || "—")}</div>
              <div class="k">Восстановил</div><div class="v">${escapeHtml(detail.restored_by || "—")}</div>
              <div class="k">Время восстановления</div><div class="v">${escapeHtml(formatTs(detail.restored_at))}</div>
            </div>
          </section>
          <section class="ops-card provision-card">
            <h4>Привязка к сотруднику</h4>
            <div>
              <label class="field-label" for="workspaceEmployeeSelect">Сотрудник</label>
              <select id="workspaceEmployeeSelect" class="text-field" data-linked-employee-id="${escapeHtml((employee && employee.id) || "")}" ${state.employeeOptionsError || managedAddress ? "disabled" : ""}>
                ${employeeSelectOptions(employee)}
              </select>
            </div>
            <div>
              <label class="field-label" for="workspaceAliasMl">Почтовый логин</label>
              <input type="text" id="workspaceAliasMl" class="text-field mono" value="${escapeHtml(aliasLogin)}" readonly>
            </div>
            ${state.employeeOptionsError ? `<div class="inline-notice warn">${escapeHtml(state.employeeOptionsError)}</div>` : ""}
            <div id="workspaceAliasResult" class="result-box hidden"></div>
            <div class="inline-actions">
              <button class="primary-btn" id="workspaceAliasSaveBtn" ${state.employeeOptionsError || managedAddress ? "disabled" : ""}>Привязать</button>
              <button class="danger-btn" id="workspaceAliasClearBtn" ${managedAddress ? "disabled" : ""}>Отвязать</button>
            </div>
          </section>
          ${managedAddress ? "" : `
          <section class="ops-card" id="workspaceSharedAccess">
            <h4>Общие ящики</h4>
            <div class="ops-copy">Каждый адрес подключается независимо. Доступ через AD-группу остаётся активным, даже если прямая привязка отключена.</div>
            <div class="workspace-shared-list">${sharedAccessHtml}</div>
            <div id="workspaceSharedAccessResult" class="result-box hidden"></div>
          </section>
          `}
          <section class="ops-card">
            <h4>Данные подключения</h4>
            <div class="ops-copy">Сбросьте пароль, чтобы получить свежие параметры IMAP/SMTP и QR-коды настройки.</div>
            <div class="inline-actions">
              <button class="primary-btn" id="workspaceCredentialResetBtn" ${resetDisabled ? "disabled" : ""}>Сформировать новые данные</button>
              <button class="ghost-btn" id="workspaceCopyEmailBtn">Скопировать email</button>
              <button class="ghost-btn" id="workspaceOpenQueueBtn">Открыть очередь</button>
              <button class="ghost-btn" id="workspaceOpenAuditBtn">Открыть журнал</button>
            </div>
          </section>
        </div>
      </section>
    </div>
  `;
}

function activateWorkspaceTab(tabName) {
  const next = ["overview", "diagnostics", "events", "settings"].includes(tabName) ? tabName : "overview";
  state.currentWorkspaceTab = next;
  $$(".workspace-tab").forEach((tab) => {
    const active = tab.getAttribute("data-workspace-tab") === next;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
    tab.setAttribute("tabindex", active ? "0" : "-1");
  });
  $$(".workspace-tab-panel").forEach((panel) => {
    panel.classList.toggle("active", panel.getAttribute("data-workspace-panel") === next);
  });
}

function bindMailboxWorkspace(item, detail) {
  const backButton = $("#mailboxListBackBtn");
  if (backButton) backButton.addEventListener("click", restoreSupportResultList);
  const workspaceTabs = $$(".workspace-tab");
  workspaceTabs.forEach((tab, index) => {
    tab.addEventListener("click", () => activateWorkspaceTab(tab.getAttribute("data-workspace-tab") || "overview"));
    tab.addEventListener("keydown", (event) => {
      let nextIndex = index;
      if (event.key === "ArrowRight") nextIndex = (index + 1) % workspaceTabs.length;
      else if (event.key === "ArrowLeft") nextIndex = (index - 1 + workspaceTabs.length) % workspaceTabs.length;
      else if (event.key === "Home") nextIndex = 0;
      else if (event.key === "End") nextIndex = workspaceTabs.length - 1;
      else return;
      event.preventDefault();
      const nextTab = workspaceTabs[nextIndex];
      activateWorkspaceTab(nextTab.getAttribute("data-workspace-tab") || "overview");
      nextTab.focus();
    });
  });
  const resetBtn = $("#workspaceResetPasswordBtn");
  if (resetBtn && !resetBtn.disabled) resetBtn.addEventListener("click", () => openPwd(detail.email));
  const cabinetBtn = $("#workspaceOpenMailCabinetBtn");
  if (cabinetBtn) cabinetBtn.addEventListener("click", () => openMailCabinet(detail.email));
  const deleteBtn = $("#workspaceDeleteMailboxBtn");
  const queueBtn = $("#workspaceOpenQueueBtn");
  const auditBtn = $("#workspaceOpenAuditBtn");
  if (deleteBtn) deleteBtn.addEventListener("click", () => openDel(detail.email));
  if (queueBtn) queueBtn.addEventListener("click", () => openQueueContext(detail.email));
  if (auditBtn) auditBtn.addEventListener("click", () => openAuditContext(detail.email));
  const credentialBtn = $("#workspaceCredentialResetBtn");
  if (credentialBtn && !credentialBtn.disabled) credentialBtn.addEventListener("click", () => openPwd(detail.email));
  const copyEmailBtn = $("#workspaceCopyEmailBtn");
  const aliasSaveBtn = $("#workspaceAliasSaveBtn");
  const aliasClearBtn = $("#workspaceAliasClearBtn");
  if (copyEmailBtn) copyEmailBtn.addEventListener("click", () => copyText(detail.email, "Email скопирован"));
  if (aliasSaveBtn) aliasSaveBtn.addEventListener("click", saveWorkspaceAlias);
  if (aliasClearBtn) aliasClearBtn.addEventListener("click", clearWorkspaceAlias);
  $$(".workspace-shared-connect").forEach((control) => {
    control.addEventListener("change", async () => {
      const mailboxId = Number(control.dataset.mailboxId || "0");
      const sendControl = $(`.workspace-shared-send[data-mailbox-id="${mailboxId}"]`);
      await updateWorkspaceSharedMembership(
        detail,
        mailboxId,
        control.checked,
        control.checked ? Boolean(sendControl && sendControl.checked) : false,
      );
    });
  });
  $$(".workspace-shared-send").forEach((control) => {
    control.addEventListener("change", async () => {
      const mailboxId = Number(control.dataset.mailboxId || "0");
      await updateWorkspaceSharedMembership(
        detail,
        mailboxId,
        true,
        control.checked,
      );
    });
  });
  const suspendBtn = $("#workspaceSuspendMailboxBtn");
  const restoreBtn = $("#workspaceRestoreMailboxBtn");
  if (suspendBtn) suspendBtn.addEventListener("click", () => openLifecycleModal("suspend", detail.email));
  if (restoreBtn) restoreBtn.addEventListener("click", () => openLifecycleModal("restore", detail.email));
  $$(".timeline-filter-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      state.currentTimelineFilter = btn.getAttribute("data-filter") || "all";
      await renderSupportWorkspace();
    });
  });
  $$(".workspace-open-queue-btn").forEach((btn) => {
    btn.addEventListener("click", () => openQueueContext(btn.getAttribute("data-target") || detail.email));
  });
  $$(".workspace-delete-queue-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const ok = await deleteQueueEntry(btn.getAttribute("data-target") || "");
      if (ok) renderSupportWorkspace();
    });
  });
}

async function updateWorkspaceSharedMembership(detail, mailboxId, connected, canSend) {
  const box = $("#workspaceSharedAccessResult");
  const adLogin = String(
    (detail.employee && detail.employee.ad_login)
    || (detail.alias && detail.alias.ad_login)
    || "",
  ).trim().toLowerCase();
  try {
    await apiJson("/api/adminportal/shared-mailboxes/membership", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        mailbox_id: mailboxId,
        email: detail.email,
        connected,
        can_send: canSend,
        ad_login: adLogin,
      }),
    });
    showResult(
      box,
      connected
        ? `Ящик подключён${canSend ? " с правом отправки" : " без права отправки"}`
        : "Прямая привязка отключена",
      false,
    );
    toast("Доступ к общему ящику обновлён", true);
    await renderSupportWorkspace();
  } catch (error) {
    showResult(box, error.message || "Не удалось обновить доступ", true);
    await renderSupportWorkspace();
  }
}

async function saveWorkspaceAlias() {
  const select = $("#workspaceEmployeeSelect");
  const employeeId = select.value;
  const selected = select.options[select.selectedIndex];
  const adLogin = (selected && selected.dataset.adLogin || "").trim().toLowerCase();
  const mailLogin = $("#workspaceAliasMl").value.trim().toLowerCase();
  const box = $("#workspaceAliasResult");
  if (!employeeId || !mailLogin) {
    showResult(box, "Выберите сотрудника", true);
    return;
  }
  try {
    await withBusyButton($("#workspaceAliasSaveBtn"), "Привязываем…", async () => {
      await apiJson("/api/adminportal/aliases", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          emp_id: Number(employeeId),
          ad_login: adLogin,
          mail_login: mailLogin,
        }),
      });
      showResult(box, `Ящик привязан к ${selected.textContent}`, false);
      toast("Ящик привязан к сотруднику", true);
      state.employeeOptionsLoaded = false;
      await Promise.all([renderSupportWorkspace(), loadAliases(), runSupportSearch(true)]);
    });
  } catch (error) {
    showResult(box, error.message || "Не удалось привязать ящик", true);
  }
}

async function clearWorkspaceAlias() {
  const select = $("#workspaceEmployeeSelect");
  const employeeId = select.dataset.linkedEmployeeId || "";
  const box = $("#workspaceAliasResult");
  if (!employeeId) {
    showResult(box, "Ящик не привязан к сотруднику", true);
    return;
  }
  const linked = state.employeeOptions.find((row) => String(row.id) === String(employeeId));
  if (!window.confirm(`Отвязать ящик от ${(linked && linked.name) || "сотрудника"}?`)) return;
  try {
    await withBusyButton($("#workspaceAliasClearBtn"), "Отвязываем…", async () => {
      await apiJson("/api/adminportal/aliases/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ emp_id: Number(employeeId) }),
      });
      showResult(box, "Ящик отвязан от сотрудника", false);
      toast("Ящик отвязан от сотрудника", true);
      state.employeeOptionsLoaded = false;
      await Promise.all([renderSupportWorkspace(), loadAliases(), runSupportSearch(true)]);
    });
  } catch (error) {
    showResult(box, error.message || "Не удалось отвязать ящик", true);
  }
}

function showResult(el, message, isError) {
  if (!el) return;
  el.textContent = message;
  el.className = `result-box${isError ? " error" : ""}`;
  el.classList.remove("hidden");
}

function renderAdminSectionState(target, {
  kind = "error",
  message,
  retry = state.activePage,
  colspan = 1,
} = {}) {
  if (!target) return;
  target.replaceChildren();
  if (target.classList) target.classList.remove("hidden");

  const box = document.createElement("div");
  box.className = `workspace-state ${kind}`;
  const text = document.createElement("div");
  text.textContent = message || "Раздел временно недоступен";
  box.appendChild(text);

  if (retry && ADMIN_RETRY_ACTIONS[retry]) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "ghost-btn";
    button.dataset.adminRetry = retry;
    button.textContent = "Повторить";
    box.appendChild(button);
  }

  if (target.tagName === "TBODY") {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = colspan;
    cell.appendChild(box);
    row.appendChild(cell);
    target.appendChild(row);
  } else {
    target.appendChild(box);
  }
  bindAdminRetryActions(target);
}

function bindAdminRetryActions(root = document) {
  Array.from(root.querySelectorAll("[data-admin-retry]")).forEach((button) => {
    button.addEventListener("click", () => {
      const action = ADMIN_RETRY_ACTIONS[button.dataset.adminRetry];
      if (action) action();
    }, { once: true });
  });
}

function openQueueContext(target) {
  $("#queueSearch").value = target || "";
  showPage("queue");
}

function openAuditContext(target) {
  $("#auditTarget").value = target || "";
  showPage("audit");
}

async function loadAccounts() {
  const tbody = $("#accountsBody");
  tbody.innerHTML = `<tr><td colspan="3" class="table-meta">Загрузка…</td></tr>`;
  try {
    const data = await apiJson("/api/adminportal/accounts");
    state.allAccounts = data.accounts || [];
    state.accountsLoaded = true;
    $("#statTotal").textContent = String(state.allAccounts.length);
    if (data.source === "directory_fallback") {
      toast("Показан резервный снимок каталога: основной источник недоступен", false);
    }
    renderAccounts();
  } catch (error) {
    renderAdminSectionState(tbody, {
      message: error.message || "Не удалось загрузить почтовые ящики",
      retry: "accounts",
      colspan: 3,
    });
  }
}

function renderAccounts() {
  const tbody = $("#accountsBody");
  const query = $("#accountSearch").value.trim().toLowerCase();
  const list = query ? state.allAccounts.filter((email) => email.toLowerCase().includes(query)) : state.allAccounts;
  if (!list.length) {
    tbody.innerHTML = `<tr><td colspan="3" class="table-meta">Нет ящиков</td></tr>`;
    return;
  }
  tbody.innerHTML = list.map((email) => {
    const domain = email.split("@")[1] || "";
    return `
      <tr>
        <td class="mono">${escapeHtml(email)}</td>
        <td><span class="pill">${escapeHtml(domain)}</span></td>
        <td>
          <div class="table-actions">
            <button class="btn-xs account-open-btn" data-email="${escapeHtml(email)}">Открыть</button>
            <button class="btn-xs account-pwd-btn" data-email="${escapeHtml(email)}">Сбросить пароль</button>
            <button class="btn-xs danger account-del-btn" data-email="${escapeHtml(email)}">Удалить</button>
          </div>
        </td>
      </tr>
    `;
  }).join("");
  $$(".account-open-btn").forEach((btn) => btn.addEventListener("click", () => selectSupportMailbox(btn.getAttribute("data-email"))));
  $$(".account-pwd-btn").forEach((btn) => btn.addEventListener("click", () => openPwd(btn.getAttribute("data-email"))));
  $$(".account-del-btn").forEach((btn) => btn.addEventListener("click", () => openDel(btn.getAttribute("data-email"))));
}

function managedAddressKindLabel(kind) {
  if (kind === "distribution") return "Рассылка";
  if (kind === "service") return "Сервисный ящик";
  if (kind === "technical") return "Технический ящик";
  return "Групповой ящик";
}

function technicalMailboxRoleLabel(role) {
  const labels = {
    bounce: "Сборщик недоставленных сообщений",
    noreply: "No Reply",
    support: "Техническая поддержка",
    alerts: "Системные алерты",
  };
  return labels[role] || role || "—";
}

async function ensureManagedAddressMailboxOptions() {
  if (!state.accountsLoaded) await loadAccounts();
  return state.allAccounts;
}

function renderManagedAddressMailboxOptions(selectId, selectedEmails, excludedEmail = "") {
  const select = $(selectId);
  const selected = new Set((selectedEmails || []).map((email) => String(email).toLowerCase()));
  const excluded = String(excludedEmail || "").toLowerCase();
  select.innerHTML = state.allAccounts
    .map((email) => String(email || "").trim().toLowerCase())
    .filter((email) => email && email !== excluded)
    .sort()
    .map((email) => `<option value="${escapeHtml(email)}"${selected.has(email) ? " selected" : ""}>${escapeHtml(email)}</option>`)
    .join("");
}

function selectedMailboxValues(selectId) {
  return Array.from($(selectId).selectedOptions).map((option) => option.value);
}

function renderManagedAddressResponsible(selectedValue = "") {
  const select = $("#managedAddressResponsibleSelect");
  const selected = String(selectedValue || "").toLowerCase();
  const rows = state.employeeOptions
    .map((employee) => {
      const value = String(employee.ad_login || employee.email || "").trim().toLowerCase();
      if (!value) return null;
      const details = [value, employee.department, employee.title].filter(Boolean).join(" · ");
      return { value, label: `${employee.name || value}${details ? ` · ${details}` : ""}` };
    })
    .filter(Boolean);
  if (selected && !rows.some((row) => row.value === selected)) {
    rows.unshift({ value: selected, label: selected });
  }
  select.innerHTML = [
    `<option value="">Выберите сотрудника</option>`,
    ...rows.map((row) => `<option value="${escapeHtml(row.value)}"${row.value === selected ? " selected" : ""}>${escapeHtml(row.label)}</option>`),
  ].join("");
}

function managedAddressStatusPill(address) {
  if (address.status === "error") {
    return `<span class="status-pill bad">Ошибка синхронизации</span>`;
  }
  return `<span class="status-pill good">Активен</span>`;
}

async function loadManagedAddresses() {
  const list = $("#managedAddressList");
  list.innerHTML = `<div class="empty-state"><div class="empty-copy">Загрузка…</div></div>`;
  try {
    const data = await apiJson("/api/adminportal/managed-addresses");
    state.managedAddresses = data.addresses || [];
    state.technicalMailboxPresets = data.technical_presets || {};
    if (!state.managedAddresses.some((item) => item.id === state.selectedManagedAddressId)) {
      state.selectedManagedAddressId = state.managedAddresses.length ? state.managedAddresses[0].id : null;
    }
    $("#managedGroupCount").textContent = String(state.managedAddresses.filter((item) => item.kind === "group").length);
    $("#managedDistributionCount").textContent = String(state.managedAddresses.filter((item) => item.kind === "distribution").length);
    $("#managedServiceCount").textContent = String(state.managedAddresses.filter((item) => item.kind === "service").length);
    $("#managedTechnicalCount").textContent = String(state.managedAddresses.filter((item) => item.kind === "technical").length);
    $(".managed-address-layout").classList.remove("has-selection");
    renderManagedAddressList();
    renderManagedAddressWorkspace();
  } catch (error) {
    renderAdminSectionState($("#managedAddressList"), {
      message: error.message || "Не удалось загрузить адреса",
      retry: "managed-addresses",
    });
  }
}

function filteredManagedAddresses() {
  const query = $("#managedAddressSearch").value.trim().toLowerCase();
  const kind = $("#managedAddressKindFilter").value;
  const company = $("#managedAddressCompanyFilter").value;
  return state.managedAddresses.filter((address) => {
    if (kind !== "all" && address.kind !== kind) return false;
    if (company !== "all" && address.company !== company) return false;
    if (!query) return true;
    return [
      address.email,
      address.display_name,
      address.description,
      address.responsible_user,
      address.company,
      address.directory_domain,
    ].join(" ").toLowerCase().includes(query);
  });
}

function renderManagedAddressList() {
  const list = $("#managedAddressList");
  const addresses = filteredManagedAddresses();
  $("#managedAddressListMeta").textContent = String(addresses.length);
  if (!addresses.length) {
    list.innerHTML = `<div class="empty-state"><h3>Адресов нет</h3><div class="empty-copy">Измените фильтр или создайте новый общий адрес.</div></div>`;
    return;
  }
  const companyOrder = ["Corp", "Alpha", "Beta", "Gamma"];
  const grouped = new Map();
  addresses.forEach((address) => {
    const company = address.company || "Corp";
    if (!grouped.has(company)) grouped.set(company, []);
    grouped.get(company).push(address);
  });
  list.innerHTML = [...grouped.entries()]
    .sort(([left], [right]) => {
      const leftIndex = companyOrder.indexOf(left);
      const rightIndex = companyOrder.indexOf(right);
      return (leftIndex < 0 ? 99 : leftIndex) - (rightIndex < 0 ? 99 : rightIndex)
        || left.localeCompare(right, "ru");
    })
    .map(([company, companyAddresses]) => `
    <div class="managed-address-company-group">
      <div class="managed-address-company-title">
        <strong>${escapeHtml(company)}</strong>
        <span>${companyAddresses.length}</span>
      </div>
      ${companyAddresses.map((address) => `
    <button class="managed-address-item${address.id === state.selectedManagedAddressId ? " active" : ""}" data-id="${address.id}" type="button">
      <span class="managed-address-item-head">
        <strong>${escapeHtml(address.display_name || address.email)}</strong>
        ${managedAddressStatusPill(address)}
      </span>
      <span class="managed-address-email">${escapeHtml(address.email)}</span>
      <span class="table-meta">${escapeHtml(address.directory_domain || "")}</span>
      <span class="managed-address-item-foot">
        <span class="pill">${escapeHtml(managedAddressKindLabel(address.kind))}</span>
        ${address.kind === "technical"
          ? `<span>${escapeHtml(technicalMailboxRoleLabel(address.technical_role))}</span>`
          : address.kind === "distribution"
          ? `<span>${(address.recipients || []).length} получателей</span>`
          : `<span>${(address.members || []).length} участников</span>`}
      </span>
    </button>
      `).join("")}
    </div>
  `).join("");
  $$(".managed-address-item").forEach((button) => button.addEventListener("click", () => {
    state.selectedManagedAddressId = Number(button.getAttribute("data-id"));
    $(".managed-address-layout").classList.add("has-selection");
    renderManagedAddressList();
    renderManagedAddressWorkspace();
    if (window.matchMedia && window.matchMedia("(max-width: 720px)").matches) {
      $(".managed-address-workspace-panel").scrollIntoView({ block: "start" });
    }
  }));
}

function renderManagedAddressWorkspace() {
  const box = $("#managedAddressWorkspace");
  const address = state.managedAddresses.find((item) => item.id === state.selectedManagedAddressId);
  if (!address) {
    $(".managed-address-layout").classList.remove("has-selection");
    box.innerHTML = `<div class="empty-state"><h3>Выберите адрес</h3><div class="empty-copy">Здесь появятся состав, назначение и доступные действия.</div></div>`;
    return;
  }
  const relations = address.kind === "distribution"
    ? (address.recipients || []).map((recipient) => `<li><span>${escapeHtml(recipient)}</span></li>`).join("")
    : [
        ...(address.members || []).map((member) => `
          <li>
            <span>${escapeHtml(member.email)}</span>
            <span class="pill">${member.can_send ? "Чтение и отправка" : "Только чтение"}</span>
          </li>
        `),
        ...(address.ad_groups || []).map((group) => `
          <li>
            <span><strong>AD</strong> · ${escapeHtml(group.name || group.dn)}</span>
            <span class="pill">${group.can_send ? "Чтение и отправка" : "Только чтение"}</span>
          </li>
        `),
      ].join("");
  const relationTitle = address.kind === "distribution" ? "Получатели" : "Участники";
  box.innerHTML = `
    <button class="ghost-btn managed-address-back-btn" id="managedAddressBackBtn" type="button">К списку</button>
    <div class="managed-address-summary">
      <div>
        <div class="managed-address-kicker">${escapeHtml(managedAddressKindLabel(address.kind))}</div>
        <h3>${escapeHtml(address.display_name || address.email)}</h3>
        <div class="managed-address-email">${escapeHtml(address.email)}</div>
        <div class="table-meta">${escapeHtml(address.company || "Corp")} · ${escapeHtml(address.directory_domain || "corp.local")}</div>
      </div>
      ${managedAddressStatusPill(address)}
    </div>
    ${address.description ? `<p class="managed-address-description">${escapeHtml(address.description)}</p>` : ""}
    ${address.technical_role ? `<div class="managed-address-fact"><span>Техническая роль</span><strong>${escapeHtml(technicalMailboxRoleLabel(address.technical_role))}</strong></div>` : ""}
    ${address.responsible_user ? `<div class="managed-address-fact"><span>Ответственный</span><strong>${escapeHtml(address.responsible_user)}</strong></div>` : ""}
    ${address.last_error ? `<div class="danger-box"><strong>Последняя ошибка</strong><br>${escapeHtml(address.last_error)}</div>` : ""}
    <section class="managed-address-relations">
      <h4>${relationTitle}</h4>
      <ul>${relations || `<li class="table-meta">Не указаны</li>`}</ul>
    </section>
    <div class="action-row managed-address-actions">
      <button class="ghost-btn" id="managedAddressEditBtn" type="button">Изменить</button>
      ${address.kind !== "distribution" ? `<button class="ghost-btn" id="managedAddressPasswordBtn" type="button">Новый пароль</button>` : ""}
      ${address.kind !== "distribution" ? `<button class="ghost-btn" id="managedAddressOpenMailBtn" type="button">Открыть LM Почту</button>` : ""}
      <button class="ghost-btn" id="managedAddressResyncBtn" type="button">Синхронизировать</button>
      <button class="danger-btn" id="managedAddressDeleteBtn" type="button">Удалить</button>
    </div>
  `;
  $("#managedAddressBackBtn").addEventListener("click", () => {
    $(".managed-address-layout").classList.remove("has-selection");
    const selected = $(`.managed-address-item[data-id="${state.selectedManagedAddressId}"]`);
    if (selected) {
      selected.scrollIntoView({ block: "nearest" });
      selected.focus();
    }
  });
  $("#managedAddressEditBtn").addEventListener("click", () => openManagedAddressForm(address));
  const passwordButton = $("#managedAddressPasswordBtn");
  if (passwordButton) passwordButton.addEventListener("click", () => resetManagedAddressPassword(address.id));
  const mailButton = $("#managedAddressOpenMailBtn");
  if (mailButton) mailButton.addEventListener("click", () => openMailCabinet(address.email));
  $("#managedAddressResyncBtn").addEventListener("click", () => resyncManagedAddress(address.id));
  $("#managedAddressDeleteBtn").addEventListener("click", () => openManagedAddressDelete(address));
}

function managedAddressMemberLines(members) {
  return (members || []).map((member) => `${member.can_send ? "" : "read:"}${member.email}`).join("\n");
}

async function openManagedAddressForm(address = null) {
  await Promise.all([
    ensureManagedAddressMailboxOptions(),
    loadEmployeeOptions(),
    loadAdGroups(),
  ]);
  const editing = Boolean(address);
  const accountEmails = new Set(state.allAccounts.map((email) => String(email).toLowerCase()));
  const members = editing ? address.members || [] : [];
  const selectedMembers = members
    .filter((member) => member.can_send && accountEmails.has(String(member.email).toLowerCase()))
    .map((member) => member.email);
  const manualMembers = members.filter(
    (member) => !member.can_send || !accountEmails.has(String(member.email).toLowerCase()),
  );
  const recipients = editing ? address.recipients || [] : [];
  const selectedRecipients = recipients.filter((email) => accountEmails.has(String(email).toLowerCase()));
  const manualRecipients = recipients.filter((email) => !accountEmails.has(String(email).toLowerCase()));
  $("#managedAddressFormTitle").textContent = editing ? "Изменение общего адреса" : "Создание общего адреса";
  $("#managedAddressSaveBtn").textContent = editing ? "Сохранить" : "Создать";
  $("#managedAddressId").value = editing ? String(address.id) : "";
  $("#managedAddressKind").value = editing ? address.kind : "group";
  $("#managedAddressTechnicalRole").value = editing ? address.technical_role || "" : "";
  $("#managedAddressEmail").value = editing ? address.email : "";
  $("#managedAddressDisplayName").value = editing ? address.display_name || "" : "";
  $("#managedAddressCompany").value = editing ? address.company || "Corp" : "Corp";
  $("#managedAddressDescription").value = editing ? address.description || "" : "";
  renderManagedAddressResponsible(editing ? address.responsible_user || "" : "");
  const selectedGroups = editing ? address.ad_groups || [] : [];
  state.managedAddressGroupPermissions = Object.fromEntries(
    selectedGroups.map((group) => [
      String(group.dn || "").toLowerCase(),
      Boolean(group.can_send),
    ]),
  );
  state.managedAddressGroupPermissionOverride = null;
  renderAdGroupOptions("#managedAddressGroupSelect", selectedGroups);
  const groupSend = $("#managedAddressGroupSend");
  const allGroupsCanSend = !selectedGroups.length
    || selectedGroups.every((group) => group.can_send);
  const noGroupsCanSend = selectedGroups.every((group) => !group.can_send);
  groupSend.checked = allGroupsCanSend;
  groupSend.indeterminate = selectedGroups.length > 0
    && !allGroupsCanSend
    && !noGroupsCanSend;
  groupSend.onchange = () => {
    groupSend.indeterminate = false;
    state.managedAddressGroupPermissionOverride = groupSend.checked;
  };
  renderManagedAddressMailboxOptions(
    "#managedAddressMemberSelect",
    selectedMembers,
    editing ? address.email : "",
  );
  renderManagedAddressMailboxOptions(
    "#managedAddressRecipientSelect",
    selectedRecipients,
    editing ? address.email : "",
  );
  $("#managedAddressMemberLines").value = managedAddressMemberLines(manualMembers);
  $("#managedAddressRecipientLines").value = manualRecipients.join("\n");
  $("#managedAddressKind").disabled = editing;
  $("#managedAddressEmail").disabled = editing;
  $("#managedAddressTechnicalPresets").classList.toggle("hidden", editing);
  $$(".technical-preset-btn").forEach((button) => button.classList.remove("active"));
  $("#managedAddressResult").className = "result-box hidden";
  syncManagedAddressFormKind();
  showModal("modalManagedAddress");
}

function applyTechnicalMailboxPreset(role) {
  const preset = state.technicalMailboxPresets[role];
  if (!preset) {
    toast("Пресет технического ящика недоступен", false);
    return;
  }
  $("#managedAddressKind").value = "technical";
  $("#managedAddressTechnicalRole").value = role;
  $("#managedAddressEmail").value = preset.email || "";
  $("#managedAddressDisplayName").value = preset.display_name || "";
  $("#managedAddressCompany").value = "Corp";
  $("#managedAddressDescription").value = preset.description || "";
  renderManagedAddressResponsible("mailops");
  $("#managedAddressMemberSelect").selectedIndex = -1;
  $("#managedAddressMemberLines").value = "";
  $("#managedAddressGroupSelect").selectedIndex = -1;
  state.managedAddressGroupPermissions = {};
  state.managedAddressGroupPermissionOverride = null;
  $$(".technical-preset-btn").forEach((button) => {
    button.classList.toggle(
      "active",
      button.getAttribute("data-technical-role") === role,
    );
  });
  syncManagedAddressFormKind();
}

function syncManagedAddressFormKind() {
  const kind = $("#managedAddressKind").value;
  const serviceLike = kind === "service" || kind === "technical";
  $("#managedAddressMembers").classList.toggle("hidden", kind === "distribution");
  $("#managedAddressRecipients").classList.toggle("hidden", kind !== "distribution");
  $("#managedAddressServiceFields").classList.toggle("hidden", !serviceLike);
  $("#managedAddressTechnicalRoleField").classList.toggle("hidden", kind !== "technical");
  $("#managedAddressTechnicalRole").required = kind === "technical";
  $("#managedAddressResponsibleSelect").required = serviceLike;
  $("#managedAddressDescription").required = serviceLike;
}

function collectManagedAddressPayload() {
  const kind = $("#managedAddressKind").value;
  const managedAddressId = Number($("#managedAddressId").value || 0);
  const currentAddress = state.managedAddresses.find(
    (address) => Number(address.id) === managedAddressId,
  );
  const selectedMembers = selectedMailboxValues("#managedAddressMemberSelect")
    .map((email) => ({ email, can_send: true }));
  const manualMembers = $("#managedAddressMemberLines").value.split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const readOnly = line.toLowerCase().startsWith("read:");
      return {
        email: (readOnly ? line.slice(5) : line).trim(),
        can_send: !readOnly,
      };
    });
  const selectedRecipients = selectedMailboxValues("#managedAddressRecipientSelect");
  const groupCanSend = $("#managedAddressGroupSend").checked;
  const groupPermissionOverride = state.managedAddressGroupPermissionOverride;
  const selectedGroups = selectedMailboxValues("#managedAddressGroupSelect")
    .map((dn) => {
      const group = state.adGroups.find((row) => String(row.dn).toLowerCase() === String(dn).toLowerCase());
      const originalPermission = state.managedAddressGroupPermissions[
        String(dn).toLowerCase()
      ];
      return {
        dn,
        name: group ? group.name : dn,
        can_send: groupPermissionOverride === null
          && originalPermission !== undefined
          ? originalPermission
          : (groupPermissionOverride ?? groupCanSend),
      };
    });
  const manualRecipients = $("#managedAddressRecipientLines").value.split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
  return {
    kind,
    technical_role: kind === "technical" ? $("#managedAddressTechnicalRole").value : "",
    email: $("#managedAddressEmail").value.trim(),
    revision: currentAddress ? Number(currentAddress.revision) : null,
    company: $("#managedAddressCompany").value,
    display_name: $("#managedAddressDisplayName").value.trim(),
    description: ["service", "technical"].includes(kind) ? $("#managedAddressDescription").value.trim() : "",
    responsible_user: ["service", "technical"].includes(kind) ? $("#managedAddressResponsibleSelect").value.trim() : "",
    members: kind === "distribution" ? [] : [...selectedMembers, ...manualMembers],
    ad_groups: kind === "distribution" ? [] : selectedGroups,
    recipients: kind === "distribution" ? [...selectedRecipients, ...manualRecipients] : [],
  };
}

async function saveManagedAddress() {
  const form = $("#managedAddressForm");
  const result = $("#managedAddressResult");
  if (!form.reportValidity()) return;
  const id = $("#managedAddressId").value;
  const payload = collectManagedAddressPayload();
  const button = $("#managedAddressSaveBtn");
  button.disabled = true;
  try {
    const data = await apiJson(
      id ? `/api/adminportal/managed-addresses/${id}` : "/api/adminportal/managed-addresses",
      {
        method: id ? "PUT" : "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      },
    );
    state.selectedManagedAddressId = Number(id || data.id);
    closeModal("modalManagedAddress", !data.credential_pack);
    toast(id ? "Общий адрес обновлён" : "Общий адрес создан", true);
    await loadManagedAddresses();
    if (data.credential_pack) {
      openCredentialPack(data.credential_pack, `Параметры ящика · ${payload.email}`);
    }
  } catch (error) {
    showResult(result, error.message || "Не удалось сохранить адрес", true);
  } finally {
    button.disabled = false;
  }
}

async function resetManagedAddressPassword(id) {
  const address = state.managedAddresses.find((item) => item.id === id);
  if (!address || !window.confirm(`Создать новый пароль для ${address.email}?`)) return;
  try {
    const data = await apiJson(`/api/adminportal/managed-addresses/${id}/password`, { method: "POST" });
    toast(`Пароль изменён: ${address.email}`, true);
    openCredentialPack(data.credential_pack, `Параметры ящика · ${address.email}`);
    await loadManagedAddresses();
  } catch (error) {
    toast(error.message || "Не удалось изменить пароль", false);
  }
}

async function resyncManagedAddress(id) {
  try {
    await apiJson(`/api/adminportal/managed-addresses/${id}/resync`, { method: "POST" });
    toast("Синхронизация выполнена", true);
    await loadManagedAddresses();
  } catch (error) {
    toast(error.message || "Синхронизация не выполнена", false);
    await loadManagedAddresses();
  }
}

function openManagedAddressDelete(address) {
  state.currentManagedAddressDeleteId = address.id;
  $("#managedAddressDeleteEmail").textContent = address.email;
  $("#managedAddressDeleteCopy").textContent = address.kind === "distribution"
    ? "Адрес рассылки и список получателей будут удалены."
    : "Почтовый ящик, его письма и настройки будут удалены без возможности восстановления.";
  showModal("managedAddressDeleteModal");
}

async function confirmManagedAddressDelete() {
  const id = state.currentManagedAddressDeleteId;
  if (!id) return;
  try {
    await apiJson(`/api/adminportal/managed-addresses/${id}`, { method: "DELETE" });
    state.currentManagedAddressDeleteId = null;
    state.selectedManagedAddressId = null;
    closeModal("managedAddressDeleteModal");
    toast("Общий адрес удалён", true);
    await loadManagedAddresses();
  } catch (error) {
    toast(error.message || "Не удалось удалить адрес", false);
  }
}

async function loadStats() {
  const tbody = $("#statsBody");
  try {
    const data = await apiJson("/api/adminportal/stats");
    $("#statBoxes").textContent = String(data.total_mailboxes || 0);
    $("#statQueue").textContent = String(data.queue_count || 0);
    $("#statDeferred").textContent = String(data.deferred_count || 0);
    const totalBytes = (data.mailboxes || []).reduce((sum, row) => sum + Number(row.size_bytes || 0), 0);
    $("#statSize").textContent = humanSize(totalBytes);
    updateQueueBadge(data.queue_count || 0);
    const rows = data.mailboxes || [];
    if (!rows.length) {
      tbody.innerHTML = `<tr><td colspan="2" class="table-meta">Нет ящиков</td></tr>`;
      return;
    }
    tbody.innerHTML = rows.map((row) => `
      <tr>
        <td><button class="context-link-btn" data-email="${escapeHtml(row.email)}">${escapeHtml(row.email)}</button></td>
        <td style="text-align:right">${escapeHtml(row.size_human)}</td>
      </tr>
    `).join("");
    $$("#statsBody .context-link-btn").forEach((btn) => btn.addEventListener("click", () => selectSupportMailbox(btn.getAttribute("data-email"))));
  } catch (error) {
    renderAdminSectionState($("#statsBody"), {
      message: error.message || "Ошибка статистики",
      retry: "stats",
      colspan: 2,
    });
  }
}

async function loadQueue() {
  const tbody = $("#queueBody");
  tbody.innerHTML = `<tr><td colspan="8" class="table-meta">Загрузка…</td></tr>`;
  try {
    const data = await apiJson("/api/adminportal/queue");
    state.queueEntries = data.entries || [];
    $("#queueCount").textContent = `(${data.total || 0})`;
    $("#queueActiveStat").textContent = String(state.queueEntries.filter((row) => row.active).length);
    $("#queueDeferredStat").textContent = String(state.queueEntries.filter((row) => row.deferred && !row.active && !row.hold).length);
    $("#queueHoldStat").textContent = String(state.queueEntries.filter((row) => row.hold).length);
    updateQueueBadge(data.total || 0);
    renderQueue();
  } catch (error) {
    renderAdminSectionState(tbody, {
      message: error.message || "Не удалось загрузить очередь",
      retry: "queue",
      colspan: 8,
    });
  }
}

function renderQueue() {
  const tbody = $("#queueBody");
  const query = $("#queueSearch").value.trim().toLowerCase();
  const filtered = !query
    ? state.queueEntries
    : state.queueEntries.filter((entry) => [entry.queue_id, entry.sender, (entry.recipients || []).join(" "), entry.error, entry.time].join(" ").toLowerCase().includes(query));
  if (!filtered.length) {
    tbody.innerHTML = `<tr><td colspan="8" class="table-meta">${state.queueEntries.length ? "Ничего не найдено" : "Очередь пуста"}</td></tr>`;
    return;
  }
  tbody.innerHTML = filtered.map((entry) => `
    <tr>
      <td class="mono">${escapeHtml(entry.queue_id || "")}</td>
      <td><span class="queue-status ${queueStatus(entry)}">${queueStatusLabel(entry)}</span></td>
      <td>${escapeHtml(entry.time || "")}</td>
      <td>${humanSize(entry.size || 0)}</td>
      <td><button class="context-link-btn queue-mailbox-btn" data-target="${escapeHtml(entry.sender || "")}">${escapeHtml(entry.sender || "—")}</button></td>
      <td>
        <div class="cell-stack">
          ${(entry.recipients || []).map((rcpt) => `<button class="context-link-btn queue-mailbox-btn" data-target="${escapeHtml(rcpt)}">${escapeHtml(rcpt)}</button>`).join("") || "<span class='table-meta'>—</span>"}
        </div>
      </td>
      <td>${entry.error ? `<div class="queue-error" title="${escapeHtml(entry.error)}">${escapeHtml(entry.error.slice(0, 160))}</div>` : "<span class='table-meta'>—</span>"}</td>
      <td><button class="btn-xs danger queue-delete-btn" data-id="${escapeHtml(entry.queue_id || "")}">Удалить</button></td>
    </tr>
  `).join("");
  $$(".queue-mailbox-btn").forEach((btn) => btn.addEventListener("click", () => {
    const email = mailboxEmailFromValue(btn.getAttribute("data-target") || "");
    if (email) selectSupportMailbox(email);
  }));
  $$(".queue-delete-btn").forEach((btn) => btn.addEventListener("click", () => deleteQueueEntry(btn.getAttribute("data-id") || "")));
}

async function flushQueue() {
  if (!window.confirm("Запустить обработку всех сообщений в очереди?")) return;
  try {
    await apiJson("/api/adminportal/queue/flush", { method: "POST" });
    toast("Очередь обработана", true);
    await loadQueue();
  } catch (error) {
    toast(error.message || "Не удалось запустить обработку очереди", false);
  }
}

async function deleteQueueEntry(queueId) {
  if (!queueId || !window.confirm(`Удалить ${queueId} из очереди?`)) return false;
  try {
    await apiJson("/api/adminportal/queue/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ queue_id: queueId }),
    });
    toast(`Удалено: ${queueId}`, true);
    await loadQueue();
    return true;
  } catch (error) {
    toast(error.message || "Не удалось удалить сообщение из очереди", false);
    return false;
  }
}

async function deleteAllQueue() {
  if (!window.confirm("Удалить ВСЕ сообщения из очереди?")) return;
  try {
    await apiJson("/api/adminportal/queue/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ queue_id: "ALL" }),
    });
    toast("Очередь очищена", true);
    await loadQueue();
  } catch (error) {
    toast(error.message || "Ошибка удаления очереди", false);
  }
}

async function loadAliases() {
  const tbody = $("#aliasesBody");
  tbody.innerHTML = `<tr><td colspan="6" class="table-meta">Загрузка…</td></tr>`;
  try {
    const data = await apiJson("/api/adminportal/aliases");
    $("#aliasesCount").textContent = `(${data.total || 0})`;
    const rows = data.aliases || [];
    if (!rows.length) {
      tbody.innerHTML = `<tr><td colspan="6" class="table-meta">Нет алиасов</td></tr>`;
      return;
    }
    tbody.innerHTML = rows.map((row) => `
      <tr>
        <td class="mono">${escapeHtml(row.ad_login || "без AD")}</td>
        <td><span class="pill">${escapeHtml(row.mail_login || "")}</span></td>
        <td>
          <div class="cell-stack">
            <span>${escapeHtml(row.employee_name || "—")}</span>
            <span class="table-meta">${escapeHtml(row.department || "—")}</span>
          </div>
        </td>
        <td>${escapeHtml(row.source || "—")}</td>
        <td>${escapeHtml(String(row.created_at || "").split("T")[0] || "—")}</td>
        <td>
          <div class="table-actions">
            <button class="btn-xs alias-open-btn" data-target="${escapeHtml(row.mail_login || "")}">Открыть ящик</button>
            <button class="btn-xs danger alias-del-btn" data-ad="${escapeHtml(row.ad_login || "")}" data-emp-id="${escapeHtml(row.id || "")}">Удалить</button>
          </div>
        </td>
      </tr>
    `).join("");
    $$(".alias-open-btn").forEach((btn) => btn.addEventListener("click", () => {
      const email = mailboxEmailFromValue(btn.getAttribute("data-target") || "");
      if (email) selectSupportMailbox(email);
    }));
    $$(".alias-del-btn").forEach((btn) => btn.addEventListener("click", () => deleteAlias(
      btn.getAttribute("data-ad") || "",
      btn.getAttribute("data-emp-id") || "",
    )));
  } catch (error) {
    renderAdminSectionState(tbody, {
      message: error.message || "Не удалось загрузить алиасы",
      retry: "aliases",
      colspan: 6,
    });
  }
}

async function upsertAlias() {
  const adLogin = $("#aliasAd").value.trim().toLowerCase();
  const mailLogin = $("#aliasMl").value.trim().toLowerCase();
  if (!adLogin || !mailLogin) {
    toast("Заполните оба поля", false);
    return;
  }
  try {
    await apiJson("/api/adminportal/aliases", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ad_login: adLogin, mail_login: mailLogin }),
    });
    toast("Алиас сохранён", true);
    $("#aliasAd").value = "";
    $("#aliasMl").value = "";
    await Promise.all([loadAliases(), runSupportSearch(true)]);
  } catch (error) {
    toast(error.message || "Не удалось сохранить алиас", false);
  }
}

async function deleteAlias(adLogin, employeeId = "") {
  const target = adLogin || `сотрудника #${employeeId}`;
  if (!target || !window.confirm(`Удалить алиас для ${target}?`)) return;
  try {
    const body = adLogin ? { ad_login: adLogin } : {};
    if (employeeId) body.emp_id = Number(employeeId);
    await apiJson("/api/adminportal/aliases/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    toast("Алиас удалён", true);
    await Promise.all([loadAliases(), runSupportSearch(true)]);
  } catch (error) {
    toast(error.message || "Не удалось удалить алиас", false);
  }
}

async function loadAudit() {
  const tbody = $("#auditBody");
  tbody.innerHTML = `<tr><td colspan="7" class="table-meta">Загрузка…</td></tr>`;
  const params = new URLSearchParams({ limit: "100" });
  const adminUser = $("#auditAdmin").value.trim();
  const action = $("#auditAction").value.trim();
  const target = $("#auditTarget").value.trim();
  if (adminUser) params.set("admin_user", adminUser);
  if (action) params.set("action", action);
  if (target) params.set("target", target);
  try {
    const data = await apiJson(`/api/adminportal/audit?${params.toString()}`);
    $("#auditCount").textContent = `(${data.total || 0})`;
    const rows = data.rows || [];
    if (!rows.length) {
      tbody.innerHTML = `<tr><td colspan="7" class="table-meta">Нет записей</td></tr>`;
      return;
    }
    tbody.innerHTML = rows.map((row) => `
      <tr>
        <td>${escapeHtml(row.ts || "")}</td>
        <td>${escapeHtml(row.admin_user || "")}</td>
        <td><span class="pill">${escapeHtml(row.action || "")}</span></td>
        <td>${mailboxEmailFromValue(row.target || "") ? `<button class="context-link-btn audit-target-btn" data-target="${escapeHtml(row.target || "")}">${escapeHtml(row.target || "—")}</button>` : escapeHtml(row.target || "—")}</td>
        <td>${escapeHtml(row.ip || "—")}</td>
        <td><div class="details-code">${escapeHtml(row.details ? JSON.stringify(row.details, null, 2) : "—")}</div></td>
        <td><span class="audit-pill ${row.ok ? "ok" : "fail"}">${row.ok ? "Успешно" : "Ошибка"}</span></td>
      </tr>
    `).join("");
    $$(".audit-target-btn").forEach((btn) => btn.addEventListener("click", () => {
      const email = mailboxEmailFromValue(btn.getAttribute("data-target") || "");
      if (email) selectSupportMailbox(email);
    }));
  } catch (error) {
    renderAdminSectionState(tbody, {
      message: error.message || "Не удалось загрузить журнал",
      retry: "audit",
      colspan: 7,
    });
  }
}

function openLifecycleModal(action, email) {
  state.currentLifecycleAction = action || "";
  state.currentLifecycleEmail = email || "";
  $("#lifecycleEmail").value = email || "";
  $("#lifecycleReason").value = "";
  $("#lifecycleResult").className = "result-box hidden";
  $("#lifecycleTitle").textContent = action === "restore" ? "Восстановление почтового ящика" : "Приостановка почтового ящика";
  $("#lifecycleCopy").textContent = action === "restore"
    ? "Восстановит прежние данные авторизации и снова откроет IMAP, SMTP и webmail."
    : "Заблокирует вход, но сохранит почтовый ящик и входящую доставку.";
  $("#lifecycleConfirmBtn").textContent = action === "restore" ? "Восстановить" : "Приостановить";
  $("#lifecycleConfirmBtn").className = action === "restore" ? "primary-btn" : "danger-btn";
  showModal("modalLifecycle");
}

async function submitLifecycleAction() {
  const action = state.currentLifecycleAction;
  const email = state.currentLifecycleEmail;
  const reason = $("#lifecycleReason").value.trim();
  const result = $("#lifecycleResult");
  if (!action || !email) return;
  try {
    const data = await apiJson(`/api/adminportal/mailboxes/${action}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, reason }),
    });
    showResult(result, `${action === "restore" ? "Восстановлен" : "Приостановлен"}: ${data.email}`, false);
    toast(action === "restore" ? `Ящик восстановлен: ${email}` : `Ящик приостановлен: ${email}`, true);
    const returnFocus = modalReturnFocus.get("modalLifecycle") || null;
    if (returnFocus) {
      returnFocus.element = null;
      returnFocus.id = action === "restore" ? "workspaceSuspendMailboxBtn" : "workspaceRestoreMailboxBtn";
      returnFocus.fallbackIds = [action === "restore" ? "workspaceRestoreMailboxBtn" : "workspaceSuspendMailboxBtn"];
    }
    closeModal("modalLifecycle", false);
    await Promise.all([runSupportSearch(true), loadAccounts(), loadDeskStats(), loadAudit()]);
    restoreFocusTarget(returnFocus);
  } catch (error) {
    showResult(result, error.message || "Не удалось изменить состояние ящика", true);
  }
}

function openPwd(email) {
  $("#pwdEmail").value = email || "";
  $("#pwdNew").value = "";
  $("#pwdResult").className = "result-box hidden";
  showModal("modalPwd");
}

async function confirmPwd() {
  const email = $("#pwdEmail").value.trim();
  const password = $("#pwdNew").value.trim();
  const result = $("#pwdResult");
  try {
    const data = await apiJson("/api/adminportal/accounts/password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });
    showResult(result, `Новый пароль: ${data.password}`, false);
    toast(`Пароль изменён: ${email}`, true);
    const returnFocus = modalReturnFocus.get("modalPwd") || null;
    closeModal("modalPwd", !data.credential_pack);
    openCredentialPack(data.credential_pack, `Параметры ящика · ${email}`, returnFocus);
  } catch (error) {
    showResult(result, error.message || "Не удалось сбросить пароль", true);
  }
}

function openDel(email) {
  state.currentDelEmail = email || "";
  $("#delEmailDisplay").textContent = state.currentDelEmail;
  showModal("modalDel");
}

async function confirmDelete() {
  const email = state.currentDelEmail;
  if (!email) return;
  try {
    await withBusyButton($("#delConfirmBtn"), "Удаляем…", async () => {
      await apiJson("/api/adminportal/accounts/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email }),
      });
      toast(`Удалён ${email}`, true);
      closeModal("modalDel");
      if (state.selectedSupportItem && String(state.selectedSupportItem.email || "").toLowerCase() === email.toLowerCase()) {
        state.selectedSupportItem = null;
        state.selectedSupportKey = "";
        renderSupportWorkspace();
      }
      await Promise.all([loadAccounts(), runSupportSearch(true), loadDeskStats()]);
    });
  } catch (error) {
    toast(error.message || "Ошибка удаления", false);
  }
}

function openCredentialPack(pack, title, returnFocus = null) {
  if (!pack) return;
  state.currentCredentialPack = pack;
  $("#credentialTitle").textContent = title || `Параметры ящика · ${pack.email || ""}`;
  $("#credentialSummary").textContent = pack.summary_text || "";
  renderQr("#tbQr", pack.thunderbird_mobile && pack.thunderbird_mobile.qr_text);
  renderQr("#dcQr", pack.delta_chat && pack.delta_chat.qr_text);
  showModal("modalCredentialPack", returnFocus);
}

function renderQr(selector, text) {
  const container = $(selector);
  if (!container) return;
  container.innerHTML = "";
  if (!text) {
    container.textContent = "QR-код недоступен";
    return;
  }
  if (typeof QRCode === "undefined") {
    container.textContent = "Не удалось загрузить модуль QR-кода";
    return;
  }
  new QRCode(container, {
    text,
    width: 170,
    height: 170,
    correctLevel: QRCode.CorrectLevel.M,
  });
}

function bindEvents() {
  $("#adminLoginForm").addEventListener("submit", (event) => {
    event.preventDefault();
    doLogin();
  });
  $("#logoutBtn").addEventListener("click", doLogout);
  $("#admin-profile-button").addEventListener("click", async () => {
    renderAdminProfileCard(state.profileCard, $("#sidebarUser").textContent);
    showModal("admin-profile-modal");
    try {
      const data = await apiJson("/api/adminportal/me");
      state.profileCard = data.profile_card || state.profileCard;
      renderAdminProfileCard(state.profileCard, data.username || $("#sidebarUser").textContent);
    } catch (_) {}
  });
  $("#mobileLogoutBtn").addEventListener("click", doLogout);
  $("#mailHealthRefreshBtn").addEventListener("click", () => loadMailHealth({ silent: false }));
  $$("[data-section]").forEach((button) => {
    button.addEventListener("click", () => activateAdminNavigation(button));
  });
  bindRovingNavigation(".sidebar-nav [data-section]", activateAdminNavigation);
  bindRovingNavigation(".mobile-drawer-nav [data-section]", activateAdminNavigation);
  bindRovingNavigation(".mobile-bottom-nav [data-section]", activateAdminNavigation);
  $$("[data-admin-page]").forEach((button) => {
    button.addEventListener("click", () => showAdminPage(button.dataset.adminPage));
  });
  $("#createMailboxBtn").addEventListener("click", () => {
    const config = ADMIN_PRIMARY_ACTIONS[state.activeSection];
    if (config) config.action();
  });
  $("#mobileMenuBtn").addEventListener("click", openMobileDrawer);
  $("#mobileMoreBtn").addEventListener("click", openMobileDrawer);
  $$("[data-mobile-close]").forEach((btn) => btn.addEventListener("click", closeMobileDrawer));
  $("#supportSearch").addEventListener("input", scheduleSupportSearch);
  $("#deskRefreshBtn").addEventListener("click", loadDesk);
  $("#accountsRefreshBtn").addEventListener("click", loadAccounts);
  $("#accountSearch").addEventListener("input", renderAccounts);
  const domainSelect = $("#domainMailboxDomain");
  if (domainSelect) domainSelect.addEventListener("change", loadDomainMailboxes);
  const domainRefresh = $("#domainMailboxRefreshBtn");
  if (domainRefresh) domainRefresh.addEventListener("click", loadDomainMailboxes);
  const domainBody = $("#domainMailboxBody");
  if (domainBody) domainBody.addEventListener("click", (event) => {
    const del = event.target.closest("[data-domain-del]");
    const pw = event.target.closest("[data-domain-pw]");
    if (del) deleteDomainMailbox(del.getAttribute("data-domain-del"));
    if (pw) resetDomainMailboxPassword(pw.getAttribute("data-domain-pw"));
  });
  $("#managedAddressRefreshBtn").addEventListener("click", loadManagedAddresses);
  $("#managedAddressCreateBtn").addEventListener("click", () => openManagedAddressForm());
  $("#managedAddressSearch").addEventListener("input", renderManagedAddressList);
  $("#managedAddressKindFilter").addEventListener("change", renderManagedAddressList);
  $("#managedAddressCompanyFilter").addEventListener("change", renderManagedAddressList);
  $("#managedAddressKind").addEventListener("change", syncManagedAddressFormKind);
  $$(".technical-preset-btn").forEach((button) => button.addEventListener("click", () => {
    applyTechnicalMailboxPreset(button.getAttribute("data-technical-role") || "");
  }));
  $("#managedAddressSaveBtn").addEventListener("click", saveManagedAddress);
  $("#managedAddressDeleteConfirmBtn").addEventListener("click", confirmManagedAddressDelete);
  $("#queueRefreshBtn").addEventListener("click", loadQueue);
  $("#queueSearch").addEventListener("input", renderQueue);
  $("#queueFlushBtn").addEventListener("click", flushQueue);
  $("#queueDeleteAllBtn").addEventListener("click", deleteAllQueue);
  $("#aliasesRefreshBtn").addEventListener("click", loadAliases);
  $("#aliasSaveBtn").addEventListener("click", upsertAlias);
  $("#auditRefreshBtn").addEventListener("click", loadAudit);
  $("#auditApplyBtn").addEventListener("click", loadAudit);
  $("#statsRefreshBtn").addEventListener("click", loadStats);
  $("#settingsRefreshBtn").addEventListener("click", () => {
    state.adGroupsLoaded = false;
    loadAdminSettings();
  });
  $("#identitySaveBtn").addEventListener("click", saveIdentitySettings);
  $("#identityCheckBtn").addEventListener("click", checkIdentitySettings);
  $("#adminThemeMode").addEventListener("change", (event) => {
    applyAdminTheme(event.target.value, true);
  });
  $("#lifecycleConfirmBtn").addEventListener("click", submitLifecycleAction);
  $("#pwdConfirmBtn").addEventListener("click", confirmPwd);
  $("#delConfirmBtn").addEventListener("click", confirmDelete);
  $("#copyCredentialSummaryBtn").addEventListener("click", () => copyText((state.currentCredentialPack && state.currentCredentialPack.summary_text) || "", "Параметры скопированы"));
  $("#copyCredentialPasswordBtn").addEventListener("click", () => copyText((state.currentCredentialPack && state.currentCredentialPack.password) || "", "Пароль скопирован"));
  $("#copyThunderbirdPayloadBtn").addEventListener("click", () => copyText((state.currentCredentialPack && state.currentCredentialPack.thunderbird_mobile && state.currentCredentialPack.thunderbird_mobile.payload) || "", "Данные Thunderbird скопированы"));
  $("#copyDeltaPayloadBtn").addEventListener("click", () => copyText((state.currentCredentialPack && state.currentCredentialPack.delta_chat && state.currentCredentialPack.delta_chat.setup_url) || "", "Ссылка Delta Chat скопирована"));
  $$("[data-close]").forEach((btn) => btn.addEventListener("click", () => closeModal(btn.getAttribute("data-close"))));
  $$(".modal-shell").forEach((modal) => modal.addEventListener("click", (event) => {
    if (event.target === modal) closeModal(modal.id);
  }));
  document.addEventListener("keydown", (event) => {
    const modal = document.querySelector(".modal-shell:not(.hidden)");
    if (modal) {
      if (event.key === "Escape") {
        event.preventDefault();
        closeModal(modal.id);
      } else {
        trapFocus(modal, event);
      }
      return;
    }
    const drawer = $("#mobileDrawer");
    if (drawer && !drawer.classList.contains("hidden")) {
      if (event.key === "Escape") {
        event.preventDefault();
        closeMobileDrawer();
      } else {
        trapFocus($(".mobile-drawer-panel"), event);
      }
      return;
    }
    if (event.key === "Escape") {
      closeMobileDrawer();
    }
  });
  if (window.matchMedia) {
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
      if (storedAdminThemeMode() === "system") applyAdminTheme("system");
    });
  }
}

bindEvents();
checkAuth();
