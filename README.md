# RuPochta

Корпоративный веб-клиент почты: один процесс FastAPI/uvicorn, который отдаёт
почтовый интерфейс и админ-панель поверх обычных IMAP/SMTP, без своего
хранилища писем.

Проект — open-source-форк рабочего веб-клиента, из которого убраны имена,
адреса и настройки конкретной инсталляции. Лицензия — MIT.

## Что умеет

- Веб-клиент почты поверх IMAP/SMTP: папки, поиск, вложения, inline-картинки,
  черновики, подписи, оффлайн-оболочка (service worker, PWA-манифест).
- Вход по паролю почтового ящика и по SSO (OIDC), включая привязку Telegram.
- Админ-панель на том же процессе (`/admin`): управление ящиками и алиасами,
  многодоменный каталог сотрудников, синхронизация с LDAP/AD.
- Опциональные интеграции: приём писем как заявок во внешний helpdesk,
  контрольный агент бота, CalDAV-проксирование.

## Быстрый старт

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
export MAIL_DOMAIN=example.com MAIL_HOST=127.0.0.1
export MAIL_ADMIN_KEY=... WEBMAIL_SECRET_KEY=...
export WEBMAIL_DB=/var/lib/rupochta/webmail_aliases.db
.venv/bin/python -m uvicorn rupochta_server:app --host 127.0.0.1 --port 18400
```

Приложение слушает только loopback и рассчитано на обратный прокси (nginx,
Caddy), который терминирует TLS и передаёт `X-Real-IP`. Проверки состояния —
`/health` (процесс жив) и `/ready` (доступны IMAP/SMTP).

## Конфигурация

Всё задаётся переменными окружения, дефолты в репозитории — нейтральные
(`example.com`, `corp.local`), рабочих значений и секретов здесь нет.

| Переменная | Назначение |
|---|---|
| `MAIL_DOMAIN`, `MAIL_HOST`, `MAIL_IMAP_PORT`, `MAIL_SMTP_PORT` | почтовый контур |
| `WEBMAIL_SECRET_KEY` | подпись сессионных куки (обязательна) |
| `MAIL_ADMIN_KEY` | ключ админского API (обязателен) |
| `WEBMAIL_DB` | путь к SQLite-базе служебного состояния |
| `MAIL_SSO_ISSUER`, `MAIL_SSO_CLIENT_ID`, `MAIL_SSO_CLIENT_SECRET` | OIDC-вход |
| `MAILADMIN_LDAPS_URLS`, `MAILADMIN_LDAPS_BASE_DN`, `MAILADMIN_LDAPS_BIND_USER`, `LDAP_BIND_PASS` | каталог AD |
| `MAILADMIN_LDAP_ADMIN_GROUPS` | DN групп админов, через `;` |
| `MAIL_DIRECTORY_PROFILES` | JSON `{"Компания": "ad.domain"}` для мультидоменного каталога |
| `MAIL_TICKET_INTAKE_URL`, `INTEGRATION_WEBHOOK_SECRET` | приём писем как заявок |
| `RUPOCHTA_INTERNAL_TOKEN`, `PROXY_PANEL_URL` | внутренние интеграции (необязательны) |

Незаданные интеграции просто выключены: сервер стартует и обслуживает почту.

## Структура

- `rupochta_server.py` — приложение (аутентификация, мост IMAP/SMTP,
  каталог, админ-API, SSO).
- `rupochta_control_agent.py` — агент контрольного плана для бота.
- `imap_docker_proxy.py` — вспомогательный локальный IMAP-прокси.
- `static/` — фронтенд почты и админки, service worker.
- `tests/` — тесты: `python3 -m unittest discover -s tests`.
- `DESIGN.md`, `tokens.json` — дизайн-токены интерфейса.

## Лицензия

MIT — см. [LICENSE](LICENSE).
