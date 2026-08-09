<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="static/brand/rupochta-wordmark.svg">
  <img src="static/brand/rupochta-wordmark.svg" alt="RuПочта" width="420">
</picture>

<h3 style="font-weight:400;margin:8px 0 16px">Открытая почта для команды — на&nbsp;своём сервере, с&nbsp;приложением на&nbsp;каждой платформе</h3>

[![tests](https://github.com/lmcorp-it/RuPochta/actions/workflows/tests.yml/badge.svg)](https://github.com/lmcorp-it/RuPochta/actions/workflows/tests.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-1750d8.svg)](LICENSE)
[![python: 3.11–3.13](https://img.shields.io/badge/python-3.11_|_3.12_|_3.13-1750d8.svg)](#быстрый-старт)
[![PRs welcome](https://img.shields.io/badge/PRs-приветствуются-ed1b2f.svg)](CONTRIBUTING.md)
[![Open in Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/lmcorp-it/RuPochta)

<br>

**[rupochta.tech](https://rupochta.tech)** &nbsp;·&nbsp;
**[рупочта.рф](https://рупочта.рф)** &nbsp;·&nbsp;
**[Обсуждения](https://github.com/lmcorp-it/RuPochta/discussions)** &nbsp;·&nbsp;
**[Как участвовать](CONTRIBUTING.md)**

</div>

---

## Что такое RuПочта

**Один процесс FastAPI** — и полноценный почтовый клиент с админ-панелью у вас на сервере.
Без хранения чужих писем, без внешних сервисов, без лишней инфраструктуры.

Замена Outlook Web App, «Почты» Windows и мобильных клиентов — но под вашим контролем.
Работает как self-hosted для компании и как ядро SaaS-сервиса:
многодоменный каталог сотрудников, ящики и алиасы заводятся из админ-панели,
вход — по паролю ящика или через внешний OIDC.

> **Поставил за вечер. Писем у себя не хранит. Ставится на свой сервер.**

<br>

## 📬 Что внутри

<table>
<tr>
<td width="50%">

### Почта

- Папки, поиск, вложения, inline-картинки
- Черновики, подписи, шаблоны ответов
- Подключение внешнего ящика рядом с основным
- **Яндекс, Яндекс 360, Mail.ru, VK WorkSpace** — пресеты хостов
- Чтение и отправка через собственный сервер каждого ящика

</td>
<td width="50%">

### Управление

- Админ-панель (`/admin`) на том же процессе
- Ящики, алиасы, многодоменный каталог
- Синхронизация с **LDAP / Active Directory**
- Самостоятельная регистрация ящиков (опционально)
- Ограничения по IP, списки зарезервированных адресов

</td>
</tr>
<tr>
<td>

### Вход

- Пароль ящика
- Внешний **OIDC**-провайдер
- Привязка **Telegram**
- Вход через любой из трёх — на выбор

</td>
<td>

### Для разработчиков

- **MCP-сервер** — ящик как набор инструментов для LLM-агента
- Приём писем как заявок во внешний helpdesk
- CalDAV-проксирование
- Агент управления ботом

</td>
</tr>
</table>

<br>

## 🖥 Платформы

Веб-интерфейс (PWA) работает везде уже сейчас. Нативные клиенты —
следующая большая цель.

| Платформа | Сейчас | В разработке |
|:---|:---|:---|
| **Windows** | PWA: установка из Chrome/Edge, ярлык в «Пуск» | `.msi` / `.exe`, автозапуск, обработчик `mailto:` |
| **macOS** | PWA: Safari → «Добавить в Dock» | `.dmg`, подпись, нотаризация, Центр уведомлений |
| **Linux** | PWA-ярлык в GNOME / KDE | `.deb`, `.rpm`, AppImage, системный трей |
| **Android** | PWA: «На главный экран», офлайн, push | нативный клиент, `.apk`, публикация в сторах |
| **iOS / iPadOS** | PWA: Safari → «На экран Домой» | нативный клиент, TestFlight, App Store |

> **Каркас клиента готов** — [`desktop/`](desktop/) на Tauri 2. Собирается под все платформы через CI.
> Ищем людей с опытом в **Rust, Kotlin, Swift** или упаковке установщиков.

<br>

## ⚡ Быстрый старт

```bash
git clone https://github.com/lmcorp-it/RuPochta.git && cd RuPochta
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# Минимум переменных для запуска
export MAIL_DOMAIN=example.com MAIL_HOST=imap.example.com
export MAIL_ADMIN_KEY=*** WEBMAIL_SECRET_KEY=***
export WEBMAIL_DB=/var/lib/rupochta/webmail_aliases.db

.venv/bin/python -m uvicorn rupochta_server:app --host 127.0.0.1 --port 18400
```

Приложение слушает loopback. Рекомендуется обратный прокси (nginx, Caddy)
с терминированием TLS.

| Эндпоинт | Что проверяет |
|:---|:---|
| `/health` | Процесс жив |
| `/ready` | IMAP и SMTP доступны |

### Docker

```bash
docker build -t rupochta .
docker run -p 18400:18400 -v rupochta-data:/data \
  -e MAIL_DOMAIN=example.com -e MAIL_HOST=imap.example.com \
  -e MAIL_ADMIN_KEY=*** -e WEBMAIL_SECRET_KEY=*** rupochta
```

### Proxmox VE

Готовый почтовый сервер «под ключ» в отдельной виртуальной машине PVE:
docker-mailserver (Postfix, Dovecot, Rspamd), сертификат Let's Encrypt, nginx и
RuПочта, настроенные так, что админ-панель сразу заводит ящики.

```bash
# на хосте Proxmox
VMID=210 MAIL_FQDN=mail.example.com IPCONFIG='ip=192.0.2.10/24,gw=192.0.2.1' \
  deploy/proxmox/provision-vm.sh
```

Кириллические домены поддерживаются: `MAIL_FQDN=mail.рупочта.рф` скрипты сами
переводят в punycode.

Дальше — по [deploy/proxmox/README.md](deploy/proxmox/README.md): установка
внутри VM, DNS-записи (MX, SPF, DKIM, DMARC, PTR) и эксплуатация.

> **Два набора скриптов, не перепутайте.** `deploy/proxmox/` — с нуля: создаёт
> виртуальную машину и ставит в неё почтовый сервер. Корневой `deploy/` —
> для уже существующей VM: настраивает приложение (nginx, systemd, окружение) и
> умеет управлять гостем через API Proxmox без SSH.

### Разработка за 5 минут

Локальный стенд с [Greenmail](https://greenmail-mail-test.github.io/greenmail/) —
реальный почтовый сервер не нужен.

```bash
cp .env.dev.example .env.dev
docker compose -f docker-compose.dev.yml --env-file .env.dev up --build
```

Открыть `http://localhost:18400`, войти как `demo@example.local` / `demo-password`
(также заведены `alice` и `bob`).

<br>

## 🔧 Конфигурация

Всё через переменные окружения. Ниже — ключевые; полный список в [`DESIGN.md`](DESIGN.md).

| Переменная | Назначение |
|:---|:---|
| `MAIL_DOMAIN` · `MAIL_HOST` · `MAIL_IMAP_PORT` · `MAIL_SMTP_PORT` | Почтовый контур |
| `WEBMAIL_SECRET_KEY` | Подпись сессионных кук *(обязательна)* |
| `MAIL_ADMIN_KEY` | Ключ админского API *(обязателен)* |
| `WEBMAIL_DB` | Путь к SQLite-базе состояния |
| `MAIL_PUBLIC_SIGNUP` | Публичная регистрация: `0` — выкл, `1` — вкл |
| `MAIL_SSO_ISSUER` · `MAIL_SSO_CLIENT_ID` · `MAIL_SSO_CLIENT_SECRET` | OIDC-вход |
| `MAILADMIN_LDAPS_URLS` · `MAILADMIN_LDAPS_BASE_DN` | Каталог Active Directory |
| `MAIL_TICKET_INTAKE_URL` | Приём писем как заявок в helpdesk |
| `RUPOCHTA_INTERNAL_TOKEN` | Внутренние интеграции |

> Незаданные переменные — интеграция просто выключена. Сервер стартует в любом случае.

<br>

## 🗺 Карта проекта

```
rupochta_server.py          — ядро: аутентификация, IMAP/SMTP, админ-API, SSO
rupochta_control_agent.py   — агент управления ботом
imap_docker_proxy.py        — локальный IMAP-прокси
rupochta-mcp-server/        — MCP-сервер для LLM-агентов (TypeScript)
deploy/                     — установка на живую VM + управление через API Proxmox
deploy/proxmox/             — создание VM и почтового сервера с нуля
static/                     — фронтенд (PWA), service worker
tests/                      — python3 -m unittest discover -s tests
DESIGN.md · tokens.json     — дизайн-токены
```

<br>

## 📋 Что дальше

Ближайшие задачи — и по каждой нужны руки:

| Статус | Задача |
|:---:|:---|
| ✅ | Десктопный каркас на Tauri 2 |
| ✅ | Пресеты Яндекс / Mail.ru / VK WorkSpace |
| ✅ | Экран подключения внешнего ящика |
| ✅ | Docker Compose для разработки |
| ⬜ | OAuth (XOAUTH2) для Яндекс 360 и VK WorkSpace |
| ⬜ | Нативные клиенты: `.msi`/`.exe`, `.dmg`, `.deb`/`.rpm`, Android, iOS |
| ⬜ | Английская локализация интерфейса |

<br>

## 🤝 Присоединяйтесь

Проект молодой и открытый. Можно:

- Взять [good first issue](https://github.com/lmcorp-it/RuPochta/labels/good%20first%20issue)
- Предложить свою задачу
- Принести баг-репорт или правку в текстах

[![PRs welcome](https://img.shields.io/badge/PRs-приветствуются-ed1b2f.svg)](CONTRIBUTING.md)

Правила общения — в [кодексе поведения](CODE_OF_CONDUCT.md).
Уязвимости — в [SECURITY.md](SECURITY.md).

> **Понравилось? Поставьте ⭐ — так проект находят другие.**

<br>

## 📄 Лицензия

MIT — [LICENSE](LICENSE). Вендорённые библиотеки — в [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).

---

<details>
<summary>🇬🇧 In English</summary>

**RuPochta** is an open-source webmail client and admin panel — a self-hosted
alternative to Outlook Web App and the built-in Windows/mobile mail clients. It
is a single FastAPI/uvicorn process on top of plain IMAP and SMTP: it stores no
mail of its own and needs no external service.

The web UI is a PWA and already runs on Windows, macOS, Linux, Android and iOS.
Full native clients — `.msi`/`.exe`, `.dmg`, `.deb`/`.rpm`/AppImage, Android and
iOS builds — are the next milestone, and we are looking for people with Rust,
Kotlin, Swift or installer-packaging experience.

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). Issues and
pull requests in English are fine. Licensed under MIT.

</details>
