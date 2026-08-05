<div align="center">

<img src="static/brand/rupochta-wordmark.svg" alt="RuПочта" width="360">

**Открытая почта для команды — на своём сервере, во всех браузерах и на телефоне**

[![tests](https://github.com/lmcorp-it/RuPochta/actions/workflows/tests.yml/badge.svg)](https://github.com/lmcorp-it/RuPochta/actions/workflows/tests.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-1750d8.svg)](LICENSE)
[![PRs welcome](https://img.shields.io/badge/PR-приветствуются-ed1b2f.svg)](CONTRIBUTING.md)
![python](https://img.shields.io/badge/python-3.11%20|%203.12%20|%203.13-1750d8.svg)

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

## Одно приложение на всех платформах

Интерфейс — PWA, поэтому отдельных сборок под каждую систему не нужно:

| Платформа | Как пользоваться |
|---|---|
| **Windows** | любой браузер; «Установить приложение» в Chrome/Edge даёт окно без адресной строки и ярлык в меню «Пуск» |
| **macOS** | Safari («Добавить в Dock»), Chrome, Firefox |
| **Linux** | любой браузер; PWA-ярлык в GNOME/KDE |
| **Android** | Chrome → «Добавить на главный экран», работает офлайн, push-уведомления |
| **iOS / iPadOS** | Safari → «На экран "Домой"» |

Офлайн-оболочка и кэш — на service worker, уведомления — на Web Push. Разрабатывать
проект тоже можно с любой из этих систем: нужен только Python 3.11+ и браузер.

## Что умеет

- Почта поверх IMAP/SMTP: папки, поиск, вложения, inline-картинки, черновики,
  подписи, шаблоны ответов.
- Вход по паролю ящика и через внешний OIDC-провайдер, привязка Telegram.
- Админ-панель на том же процессе (`/admin`): ящики, алиасы, многодоменный
  каталог сотрудников, синхронизация с LDAP/AD.
- Подключение внешнего ящика по своему IMAP-хосту рядом с основным.
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
| `MAIL_TICKET_INTAKE_URL`, `INTEGRATION_WEBHOOK_SECRET` | приём писем как заявок |
| `RUPOCHTA_INTERNAL_TOKEN`, `PROXY_PANEL_URL` | внутренние интеграции (необязательны) |

Незаданные интеграции просто выключены: сервер стартует и обслуживает почту.

## Планы

Ближайшее, и по каждому пункту нужны руки:

- [ ] Подключение Яндекс, Яндекс 360, Mail.ru, VK WorkSpace: пресеты хостов и
      per-mailbox SMTP (сейчас отправка идёт только через глобальный SMTP).
- [ ] OAuth (XOAUTH2) для Яндекс 360 и VK WorkSpace вместо паролей приложений.
- [ ] Экран подключения внешнего ящика в интерфейсе.
- [ ] Английская локализация интерфейса.
- [ ] Готовый `docker compose` с локальным почтовым сервером для разработки.

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
  админ-API, SSO).
- `rupochta_control_agent.py` — агент управления ботом.
- `imap_docker_proxy.py` — вспомогательный локальный IMAP-прокси.
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
mail of its own and needs no external service. The UI is a PWA, so Windows,
macOS, Linux, Android and iOS are covered by one build.

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). Issues and
pull requests in English are fine. Licensed under MIT.

</details>
