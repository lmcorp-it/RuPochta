<div align="center">

<img src="static/brand/rupochta-wordmark.svg" alt="RuПочта" width="360">

**Открытая почта для команды — на своём сервере, с приложением на каждой платформе**

[![tests](https://github.com/lmcorp-it/RuPochta/actions/workflows/tests.yml/badge.svg)](https://github.com/lmcorp-it/RuPochta/actions/workflows/tests.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-1750d8.svg)](LICENSE)
[![PRs welcome](https://img.shields.io/badge/PR-приветствуются-ed1b2f.svg)](CONTRIBUTING.md)
![python](https://img.shields.io/badge/python-3.11%20|%203.12%20|%203.13-1750d8.svg)

[![Открыть в Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/lmcorp-it/RuPochta)

[rupochta.tech](https://rupochta.tech) · [рупочта.рф](https://рупочта.рф) ·
[Обсуждения](https://github.com/lmcorp-it/RuPochta/discussions) ·
[Как участвовать](CONTRIBUTING.md)

</div>

---

RuПочта — open-source замена Outlook Web App, «Почты» Windows и мобильных
почтовых клиентов. Один процесс FastAPI/uvicorn отдаёт почтовый интерфейс и
админ-панель поверх обычных IMAP и SMTP: писем у себя не хранит, чужую
инфраструктуру не требует, ставится на свой сервер за вечер.

Работает и как self-hosted инсталляция на одну компанию, и как основа
SaaS-сервиса: каталог сотрудников многодоменный, ящики и алиасы заводятся из
админ-панели, вход — по паролю ящика или через внешний OIDC.

## Домены проекта

У проекта два домена, и они делают разное:

| Домен | Что это | Регистрация ящиков |
|---|---|---|
| **[рупочта.рф](https://рупочта.рф)** | Живой публичный сервис на этом коде | Открытая: ящик `@рупочта.рф` заводит себе любой желающий на `/signup` |
| **[rupochta.tech](https://rupochta.tech)** | Технический сайт проекта: описание, документация, ссылки | Закрытая: почта `@rupochta.tech` только у владельца домена |

Публичная регистрация — отдельная возможность, выключенная по умолчанию
(`MAIL_PUBLIC_SIGNUP`). Корпоративная установка, обновившись, открытой не
становится: без этой переменной формы регистрации просто нет, а `POST
/api/signup` отвечает 403. Порядок вывода публичного сервиса в живой режим —
[docs/deploy-rupochta-rf.md](docs/deploy-rupochta-rf.md).

## Платформы

Веб-интерфейс работает везде уже сейчас, а полноценные нативные клиенты —
следующая большая цель проекта. Мы проектируем настоящие приложения с
установщиками, автообновлением и системной интеграцией, а не ярлык на страницу.

| Платформа | Сейчас | В разработке |
|---|---|---|
| **Windows** | PWA: «Установить приложение» в Chrome/Edge, ярлык в «Пуск» | нативный клиент, установщики `.msi` и `.exe`, автозапуск, обработчик `mailto:` |
| **macOS** | PWA: Safari → «Добавить в Dock» | приложение `.app` в `.dmg`, подпись и нотаризация, Центр уведомлений |
| **Linux** | PWA-ярлык в GNOME и KDE | пакеты `.deb`, `.rpm` и AppImage, интеграция с системным треем |
| **Android** | PWA: «На главный экран», офлайн, push | нативный клиент, `.apk` и публикация в сторах |
| **iOS / iPadOS** | PWA: Safari → «На экран "Домой"» | нативный клиент, TestFlight и App Store |

Офлайн-оболочка и кэш — на service worker, уведомления — на Web Push.
Разрабатывать проект можно с любой из этих систем: нужен только Python 3.11+ и
браузер.

**Нужны руки на клиентах.** Рассматриваем Tauri 2 — одна кодовая база даёт
десктоп и мобильные сборки поверх уже готового интерфейса, — но решение не
принято: аргументы за другой стек примем в
[обсуждении](https://github.com/lmcorp-it/RuPochta/discussions). Ищем тех, кто
работал с Rust, Kotlin, Swift или сборкой и подписью установщиков.

## Что умеет

- Почта поверх IMAP/SMTP: папки, поиск, вложения, inline-картинки, черновики,
  подписи, шаблоны ответов.
- Вход по паролю ящика и через внешний OIDC-провайдер, привязка Telegram.
- Админ-панель на том же процессе (`/admin`): ящики, алиасы, многодоменный
  каталог сотрудников, синхронизация с LDAP/AD.
- Подключение внешнего ящика (Яндекс, Яндекс 360, Mail.ru, VK WorkSpace или
  свой IMAP) рядом с основным: чтение и отправка идут через его собственный
  сервер, а не через локальный релей.
- Самостоятельная регистрация ящиков на публичном домене — с лимитом по IP,
  списком зарезервированных служебных адресов и проверкой пароля. Выключена по
  умолчанию, включается одной переменной.
- MCP-сервер (`rupochta-mcp-server/`): почтовый ящик как набор инструментов для
  LLM-агента — чтение, поиск, отправка, папки, фильтры, отложенная отправка.
- Опционально: приём писем как заявок во внешний helpdesk, CalDAV-проксирование,
  агент управления ботом.

## Быстрый старт

```bash
git clone https://github.com/lmcorp-it/RuPochta.git && cd RuPochta
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
export MAIL_DOMAIN=example.com MAIL_HOST=imap.example.com
export MAIL_ADMIN_KEY=... WEBMAIL_SECRET_KEY=...
export WEBMAIL_DB=/var/lib/rupochta/webmail_aliases.db
.venv/bin/python -m uvicorn rupochta_server:app --host 127.0.0.1 --port 18400
```

Приложение слушает loopback и рассчитано на обратный прокси (nginx, Caddy),
который терминирует TLS и передаёт `X-Real-IP`. Состояние: `/health` — процесс
жив, `/ready` — доступны IMAP и SMTP.

### Docker

```bash
docker build -t rupochta .
docker run -p 18400:18400 -v rupochta-data:/data \
  -e MAIL_DOMAIN=example.com -e MAIL_HOST=imap.example.com \
  -e MAIL_ADMIN_KEY=... -e WEBMAIL_SECRET_KEY=... rupochta
```

### Разработка за 5 минут

Локальный стенд с встроенным IMAP/SMTP-сервером — реальный почтовый сервер не
нужен. Поднимает RuПочту и [Greenmail](https://greenmail-mail-test.github.io/greenmail/)
рядом, с преднастроенными демо-ящиками.

```bash
cp .env.dev.example .env.dev
docker compose -f docker-compose.dev.yml --env-file .env.dev up --build
```

Открыть `http://localhost:18400`, войти как `demo@example.local` /
`demo-password` (заведены также `alice` и `bob`). Отправленные письма
доставляются локально между этими ящиками. Всё состояние — в томе
`rupochta-dev-data`; чтобы начать с чистого листа, остановите стенд и
удалите том: `docker compose -f docker-compose.dev.yml down -v`.

## Конфигурация

Всё задаётся переменными окружения. Дефолты в репозитории нейтральные
(`example.com`, `corp.local`), рабочих значений и секретов здесь нет.

| Переменная | Назначение |
|---|---|
| `MAIL_DOMAIN`, `MAIL_HOST`, `MAIL_IMAP_PORT`, `MAIL_SMTP_PORT` | почтовый контур |
| `WEBMAIL_SECRET_KEY` | подпись сессионных куки (обязательна) |
| `MAIL_ADMIN_KEY` | ключ админского API (обязателен) |
| `WEBMAIL_DB` | путь к SQLite-базе служебного состояния |
| `MAIL_SMTP_VERIFY_TLS` | проверка сертификата при отправке, по умолчанию включена; `0` — только для локального релея с самоподписанным сертификатом |
| `MAIL_SSO_ISSUER`, `MAIL_SSO_CLIENT_ID`, `MAIL_SSO_CLIENT_SECRET` | OIDC-вход |
| `MAILADMIN_LDAPS_URLS`, `MAILADMIN_LDAPS_BASE_DN`, `MAILADMIN_LDAPS_BIND_USER`, `LDAP_BIND_PASS` | каталог AD |
| `MAILADMIN_LDAP_ADMIN_GROUPS` | DN групп админов, через `;` |
| `MAIL_DIRECTORY_PROFILES` | JSON `{"Компания": "ad.domain"}` для мультидоменного каталога |
| `MAIL_PUBLIC_SIGNUP` | публичная регистрация ящиков; по умолчанию выключена, `1` — включить |
| `MAIL_PUBLIC_SIGNUP_DOMAIN`, `MAIL_PUBLIC_SIGNUP_MIN_PASSWORD`, `MAIL_PUBLIC_SIGNUP_PER_HOUR` | домен регистрации (по умолчанию `MAIL_DOMAIN`), минимальная длина пароля, лимит регистраций с одного IP в час |
| `WEBMAIL_LOCAL_MAILSERVER`, `MAILSERVER_CONTAINER` | локальный docker-mailserver: нужен, чтобы сервер заводил ящики сам |
| `MAIL_TICKET_INTAKE_URL`, `INTEGRATION_WEBHOOK_SECRET` | приём писем как заявок |
| `RUPOCHTA_INTERNAL_TOKEN`, `PROXY_PANEL_URL` | внутренние интеграции (необязательны) |

Незаданные интеграции просто выключены: сервер стартует и обслуживает почту.

## Планы

Ближайшее, и по каждому пункту нужны руки:

**Нативные клиенты**

- [ ] Выбрать стек и собрать первый десктопный клиент (кандидат — Tauri 2).
- [ ] Windows: установщики `.msi` и `.exe`, автообновление, `mailto:`.
- [ ] macOS: `.dmg`, подпись и нотаризация.
- [ ] Linux: `.deb`, `.rpm`, AppImage.
- [ ] Android: нативный клиент и публикация.
- [ ] iOS: нативный клиент, TestFlight и App Store.

**Почта и интеграции**

- [x] Подключение Яндекс, Яндекс 360, Mail.ru, VK WorkSpace: пресеты хостов и
      per-mailbox SMTP.
- [ ] OAuth (XOAUTH2) для Яндекс 360 и VK WorkSpace вместо паролей приложений.
- [x] Экран подключения внешнего ящика в интерфейсе — «Настройки → Внешний ящик».
- [ ] Английская локализация интерфейса.
- [x] Готовый `docker compose` с локальным почтовым сервером для разработки —
      см. раздел «Разработка за 5 минут».

## Присоединяйтесь

Проект молодой и открытый — берите задачу, предлагайте свою, приносите баг-репорт
или правку в текстах. Начните с
[good first issue](https://github.com/lmcorp-it/RuPochta/labels/good%20first%20issue)
и [CONTRIBUTING.md](CONTRIBUTING.md); правила общения — в
[кодексе поведения](CODE_OF_CONDUCT.md), уязвимости — в
[SECURITY.md](SECURITY.md).

Понравился проект — поставьте ⭐, так его находят другие.

## Структура

- `rupochta_server.py` — приложение (аутентификация, мост IMAP/SMTP, каталог,
  админ-API, SSO, публичная регистрация).
- `rupochta_control_agent.py` — агент управления ботом.
- `imap_docker_proxy.py` — вспомогательный локальный IMAP-прокси.
- `rupochta-mcp-server/` — MCP-сервер для LLM-агентов (TypeScript).
- `deploy/` — установка публичного сервиса: systemd, nginx, скрипты.
- `static/` — фронтенд почты и админки, service worker.
- `tests/` — тесты: `python3 -m unittest discover -s tests`.
- `DESIGN.md`, `tokens.json` — дизайн-токены интерфейса.

## Лицензия

MIT — см. [LICENSE](LICENSE). Вендорённые библиотеки и их лицензии перечислены
в [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).

---

<details>
<summary>In English</summary>

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
