# Запуск рупочта.рф — публичный сервис

Этот документ — рабочий порядок вывода **рупочта.рф** в живой режим на уже
существующей машине Proxmox (`pve.lets-mobile.ru`, гость `mail.lm.local` /
`mail.lets-mobile.ru`).

Два домена проекта живут раздельно и никогда не смешиваются:

| Домен | Что это | Кто пользуется | Где работает |
|---|---|---|---|
| **рупочта.рф** (`xn--80a1acdmd4a.xn--p1ai`) | Публичный почтовый сервис. Ящик `@рупочта.рф` заводит себе любой желающий | Все | Эта машина: FastAPI + docker-mailserver |
| **rupochta.tech** | Технический сайт проекта: описание, документация, ссылки. Почта `@rupochta.tech` только у владельца домена | Владелец домена | Cloudflare Worker из `docs/` (`wrangler.jsonc`) |

Публичная регистрация включается только на рупочта.рф. В любой другой установке
кода `MAIL_PUBLIC_SIGNUP` не задан, и форма регистрации не существует.

---

## Шаг 0. DNS — без него дальше ничего не работает

**Текущее состояние: домен `рупочта.рф` не делегирован — записей нет вообще.**
Проверка:

```bash
dig +short xn--80a1acdmd4a.xn--p1ai        # сейчас пусто
```

Пока эта команда молчит, ни сертификат не выпустится, ни почта не пойдёт.
Нужны записи (`IP` — внешний адрес почтовой машины):

| Тип | Имя | Значение |
|---|---|---|
| A | `@` | `IP` |
| A | `www` | `IP` |
| A | `mail` | `IP` |
| MX | `@` | `10 mail.xn--80a1acdmd4a.xn--p1ai.` |
| TXT | `@` | `v=spf1 mx -all` |
| TXT | `_dmarc` | `v=DMARC1; p=quarantine; rua=mailto:postmaster@xn--80a1acdmd4a.xn--p1ai` |
| TXT | `mail._domainkey` | DKIM-ключ из docker-mailserver (см. шаг 4) |
| PTR | `IP` | `mail.xn--80a1acdmd4a.xn--p1ai` — заводится у хостера |

Без PTR и DKIM крупные провайдеры (Яндекс, Mail.ru, Google) отправят почту
сервиса в спам. Это не опционально для публичного сервиса.

---

## Шаг 1. Переименовать машину

Имя есть в двух местах, и менять надо оба.

**На гипервизоре** (`pve.lets-mobile.ru`, по SSH или через веб-интерфейс):

```bash
qm list                              # найти VMID нужной машины
qm set <vmid> --name rupochta-rf
qm config <vmid> | grep -E '^name|^description'
```

Если это LXC-контейнер, а не ВМ — `pct set <vmid> --hostname rupochta-rf`.

**Внутри гостя** — это делает скрипт установки (`--rename`), либо вручную:

```bash
sudo hostnamectl set-hostname mail.xn--80a1acdmd4a.xn--p1ai
```

После переименования проверьте, что `postfix` знает новое имя
(`myhostname` в `/etc/postfix/main.cf` или переменная `OVERRIDE_HOSTNAME`
контейнера docker-mailserver) — иначе HELO не совпадёт с PTR.

---

## Шаг 2. Установить сервис

На гостевой машине, из клона репозитория:

```bash
git clone https://github.com/lmcorp-it/RuPochta.git
cd RuPochta
sudo ./deploy/bootstrap-rupochta-rf.sh --rename
```

Скрипт идемпотентный: ставит пакеты, разворачивает приложение в
`/opt/rupochta`, создаёт `/etc/rupochta/rupochta.env` со свежими секретами,
включает `rupochta.service` и раскладывает конфиги nginx.

Что он **не** делает: не трогает DNS и не выпускает сертификат — и то и другое
требует шага 0.

---

### Шаги 1–2 кнопкой в GitHub Actions

Самый короткий путь, если до гипервизора нет доступа с рабочей машины: раннер
GitHub дотягивается до Proxmox сам.

1. Settings → Secrets and variables → Actions → добавить секрет `PVE_PASSWORD`
   (пароль учётной записи из поля `pve_user`).
2. Actions → **deploy рупочта.рф** → Run workflow. По умолчанию это сухой
   прогон: галочка `apply` включает выполнение, отдельная галочка
   `purge_mailboxes` — удаление ящиков lets-mobile.

Воркфлоу запускает `pve-remote-provision.py`, а после — `verify-rupochta-rf.sh`
против публичного адреса. Он объявлен в окружении `production`: если добавить
туда обязательных ревьюеров, запуск потребует подтверждения.

### Шаги 1–2 одной командой, через Proxmox API

Если до гипервизора есть доступ по HTTPS (порт 8006), переименование и
установку можно сделать удалённо, без SSH — через API и гостевой агент:

```bash
export PVE_PASSWORD='...'
./deploy/pve-remote-provision.py \
  --host pve.lets-mobile.ru --user claude@lm.local \
  --vm mail --new-name rupochta-rf --insecure          # сухой прогон
./deploy/pve-remote-provision.py ... --apply            # выполнить
```

Скрипт находит гостя по имени или VMID, переименовывает его, забирает
репозиторий внутрь гостя, запускает `bootstrap-rupochta-rf.sh` и проверяет
`/health`. Без `--apply` он только показывает, что сделает. Ящики lets-mobile
удаляются отдельно — `--purge-mailboxes` поверх `--apply`.

Требования: гость запущен, в нём установлен `qemu-guest-agent`, и агент включён
на ВМ (`qm set <vmid> --agent enabled=1`). Для LXC-контейнера API выполнения
команд нет — скрипт переименует контейнер и скажет запустить bootstrap внутри
через `pct enter`.

## Шаг 3. Сертификат и nginx

После того как `A`-записи резолвятся на эту машину:

```bash
sudo certbot certonly --webroot -w /var/www/certbot \
  -d xn--80a1acdmd4a.xn--p1ai \
  -d www.xn--80a1acdmd4a.xn--p1ai \
  -d mail.xn--80a1acdmd4a.xn--p1ai
sudo nginx -t && sudo systemctl reload nginx
```

Домен везде в punycode: certbot, nginx, postfix и dovecot читают именно его.
`рупочта.рф` — только для людей.

---

## Шаг 4. Почтовый домен и DKIM

docker-mailserver должен обслуживать новый домен:

```bash
docker exec mailserver setup config dkim domain xn--80a1acdmd4a.xn--p1ai
sudo cat /srv/mail/mail-server/config/opendkim/keys/xn--80a1acdmd4a.xn--p1ai/mail.txt
```

Содержимое `mail.txt` — это TXT-запись `mail._domainkey` из шага 0.

Служебный ящик заводим сразу и вручную, чтобы он не достался постороннему
(регистрация такие имена не отдаёт — они в списке зарезервированных):

```bash
docker exec mailserver setup email add postmaster@xn--80a1acdmd4a.xn--p1ai
```

---

## Шаг 5. Убрать ящики lets-mobile

Старые ящики уезжают вместе с доменом. Скрипт сначала показывает список и
ничего не удаляет; архив maildir-ов складывается в `/var/backups/rupochta`.

```bash
./deploy/purge-lets-mobile-mailboxes.sh                 # посмотреть список
./deploy/purge-lets-mobile-mailboxes.sh --apply         # удалить
```

По умолчанию берутся домены `lets-mobile.ru`, `lets-mobile.online` и
`lm.local`; другой набор — через `--domain`.

Удаление ящика уничтожает его почту. Если что-то из этого ещё нужно —
перенесите до запуска с `--apply`.

---

## Шаг 6. Проверка живого сервиса

```bash
./deploy/verify-rupochta-rf.sh
```

Скрипт проверяет по порядку то, что ломается: делегирование домена, MX, SPF,
DKIM, DMARC, PTR, сертификат, `/health`, `/ready`, `/api/signup/config` и
страницу регистрации. Возвращает ненулевой код, если сервис ещё не живой, —
годится и для мониторинга.

Что означают типичные отказы:

- `A ... missing` — домен не делегирован, это шаг 0;
- `/ready did not answer` — приложение поднялось, но IMAP или SMTP молчат;
- `provisioning not ready` — приложение не видит локальный почтовый сервер:
  проверьте `WEBMAIL_LOCAL_MAILSERVER=1`, имя контейнера и то, что пользователь
  `rupochta` состоит в группе `docker`;
- предупреждения про DKIM/SPF/PTR — сервис работает, но исходящая почта будет
  попадать в спам.

Дальше — вручную, один раз:

1. Открыть `https://рупочта.рф/signup`, завести тестовый ящик.
2. Отправить письмо на внешний адрес (Яндекс или Gmail) и убедиться, что оно
   пришло **во «Входящие», а не в спам** — это проверяет SPF, DKIM, DMARC и PTR
   разом.
3. Ответить на него снаружи и убедиться, что письмо видно в веб-интерфейсе.
4. Удалить тестовый ящик: `docker exec mailserver setup email del -y ...`

---

## Эксплуатация

```bash
systemctl status rupochta
journalctl -u rupochta -f
```

**Регистрация**: ограничена `MAIL_PUBLIC_SIGNUP_PER_HOUR` (по умолчанию 3 на
IP в час) и отдельным лимитом nginx. Список занятых служебных имён — в
`_PUBLIC_SIGNUP_RESERVED` (`rupochta_server.py`).

**Выключить регистрацию** (например, при спам-волне): убрать
`MAIL_PUBLIC_SIGNUP` из `/etc/rupochta/rupochta.env` и
`systemctl restart rupochta`. Заведённые ящики продолжают работать, форма
исчезает и API отвечает 403.

**Обновление**: `git pull` в клоне и повторный запуск
`./deploy/bootstrap-rupochta-rf.sh` — существующий `rupochta.env` он не
перезаписывает.
