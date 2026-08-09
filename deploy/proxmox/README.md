# Почтовый сервер RuПочты на Proxmox VE

Разворачивает полноценный почтовый сервер в отдельной виртуальной машине PVE:
приём и отправка почты, антиспам, TLS — и веб-интерфейс RuПочты сверху.

RuПочта письма не хранит и своего MTA не содержит: это клиент поверх IMAP/SMTP.
Здесь под неё поднимается [docker-mailserver](https://docker-mailserver.github.io/docker-mailserver/latest/)
(Postfix + Dovecot + Rspamd в одном контейнере) — тот бэкенд, с которым админ-панель
умеет работать напрямую: заводит ящики через `docker exec mailserver`, читает
`postfix-accounts.cf` и показывает журнал доставки.

```
Proxmox VE
└── VM «mail» (Debian 13)
    ├── nginx            :80/:443  → TLS, обратный прокси
    ├── rupochta.service :18400    → веб-интерфейс и /admin (loopback)
    └── docker: mailserver         → 25, 465, 587, 993
        /srv/mail/mail-server/{config,mail-data,mail-state,mail-logs}
```

## Что понадобится

- Proxmox VE 8.0+ (используется `qm set --scsi0 ... import-from`).
- Домен и доступ к его DNS.
- Внешний IPv4 с **открытым исходящим 25/tcp** и возможностью прописать
  **PTR-запись** на `mail.example.com`. Без PTR почта будет уходить в спам;
  многие облачные провайдеры 25-й порт блокируют — тогда настраивайте
  `RELAY_HOST` в `env/mailserver.env.example` — и не забудьте добавить механизм
  релея в SPF, иначе своя же почта получит SPF-fail (`dns-records.sh` про это
  напомнит).
- ~4 ГиБ RAM и 60 ГиБ диска на VM (с ClamAV; без него хватит 2 ГиБ).

## Установка

**1. Создать VM на гипервизоре** (от root на хосте PVE):

```bash
VMID=210 MAIL_FQDN=mail.example.com IPCONFIG='ip=192.0.2.10/24,gw=192.0.2.1' STORAGE=local-lvm ./provision-vm.sh
```

Скрипт скачает cloud-образ Debian 13, создаст VM с cloud-init, пробросит ваши
SSH-ключи из `/root/.ssh/authorized_keys` и запустит её. Параметры — переменными
окружения: `VM_NAME`, `CORES`, `MEMORY`, `DISK_GB`, `BRIDGE`, `VLAN`, `CIUSER`,
`SSHKEYS`, `NAMESERVER`, `IMAGE_URL`.

Скачанный образ сверяется с `SHA512SUMS` из того же каталога — в том числе если
он взят из кеша. Отдельной подписи Debian рядом с образами не публикует, так
что подлинность держится на TLS до `cloud.debian.org`; если этого мало,
передайте выверенную сумму в `IMAGE_SHA512` и зафиксируйте `IMAGE_URL` на
датированный каталог вместо `latest`.

**2. Прописать A-запись** `mail.example.com` на внешний адрес VM и дождаться её
распространения — иначе Let's Encrypt не выдаст сертификат.

**3. Установить почтовый стек внутри VM:**

```bash
scp -r deploy/proxmox rupochta@192.0.2.10:/tmp/
ssh rupochta@192.0.2.10 "sudo MAIL_FQDN=mail.example.com MAIL_DOMAIN=example.com ACME_EMAIL=postmaster@example.com bash /tmp/proxmox/bootstrap.sh"
```

`bootstrap.sh` идемпотентен: повторный запуск обновляет конфиги и код, но не
трогает почту, ящики и уже сгенерированные секреты. Полезные переменные:
`DMS_IMAGE` (по умолчанию docker-mailserver 15.1.0, закреплённый по digest),
`RUPOCHTA_REF`, `TZ_NAME`, `SKIP_TLS=1`.

`SKIP_TLS=1` — это режим доводки, а не рабочая установка: интерфейс отдаётся по
HTTP, но почта не работает совсем (приложение ходит в IMAP только по TLS, а
docker-mailserver поднят без сертификата), и `/ready` будет красным. Делегируйте
домен и перезапустите `bootstrap.sh` уже без этого флага.

**4. Завести DNS-записи** — готовый список печатает:

```bash
ssh rupochta@192.0.2.10 "sudo MAIL_FQDN=mail.example.com /tmp/proxmox/dns-records.sh"
```

MX, SPF, DKIM, DMARC и напоминание про PTR. Пока их нет, почта уходить будет,
но принимать её станут неохотно.

**5. Первый ящик и вход:**

```bash
# -t обязателен: без псевдотерминала docker exec -ti не дойдёт до запроса пароля
ssh -t rupochta@192.0.2.10 "sudo docker exec -ti mailserver setup email add admin@example.com"
```

Дальше — `https://mail.example.com`, вход по адресу и паролю ящика.

**Про `/admin` без каталога.** Форма входа в админ-панель проверяет учётные
данные **только через LDAP/AD** (`_ldap_check_admin`). `MAIL_ADMIN_KEY` — ключ
для API, браузерного входа он не даёт, а маршруты админского OIDC
(`/admin/sso/login`, `/admin/sso/callback`) в текущем коде отдают 404. На
установке без каталога панель останется недоступной, и ящики заводятся с
сервера:

```bash
sudo docker exec -ti mailserver setup email add user@example.com
```

Чтобы открыть панель, задайте в `/etc/rupochta/rupochta.env` группу
`MAILADMIN_LDAPS_*`. Мастер
«Создать ящик» дополнительно опирается на каталог сотрудников
(`PROXY_PANEL_URL`) и без него отвечает «нужен точный сотрудник»; само создание
ящика при `WEBMAIL_LOCAL_MAILSERVER=1` идёт локально, через `docker exec`.

## Кириллический домен: рупочта.рф

Скрипты принимают имя как есть и сами переводят его в punycode — Postfix,
Dovecot, Let's Encrypt и `/etc/hosts` работают только с ASCII:

```
рупочта.рф        →  xn--80a1acdmd4a.xn--p1ai
mail.рупочта.рф   →  mail.xn--80a1acdmd4a.xn--p1ai
```

Развёртывание один в один как выше, домен пишется по-русски:

```bash
# на хосте PVE
VMID=210 MAIL_FQDN=mail.рупочта.рф IPCONFIG='ip=192.0.2.10/24,gw=192.0.2.1' ./provision-vm.sh

# внутри VM
sudo MAIL_FQDN=mail.рупочта.рф MAIL_DOMAIN=рупочта.рф \
     ACME_EMAIL=postmaster@xn--80a1acdmd4a.xn--p1ai bash /tmp/proxmox/bootstrap.sh
```

Что важно знать про IDN:

- **Ящики заводятся только в punycode**: `admin@xn--80a1acdmd4a.xn--p1ai`.
  Адрес с кириллической локальной частью (`админ@рупочта.рф`) требует SMTPUTF8
  на всей цепочке доставки и здесь не поддерживается — русские имена задавайте
  в отображаемом имени сотрудника, а не в адресе.
- Сертификат Let's Encrypt выпускается на punycode-имя; браузер покажет
  `https://mail.рупочта.рф` — это одно и то же имя.
- В панели регистратора `.рф` записи можно вводить кириллицей, но
  `dns-records.sh` печатает ASCII-вид: он принимается везде и совпадает с
  сертификатом.
- Валидация адресов в `rupochta_server.py` до недавнего времени требовала TLD
  из одних букв и отвергала `xn--p1ai`; регрессия закрыта тестом
  [`tests/test_idn_mailbox.py`](../../tests/test_idn_mailbox.py).

## Как это связано с настройками RuПочты

`bootstrap.sh` кладёт `/etc/rupochta/rupochta.env` так, чтобы сойтись с
дефолтами `rupochta_server.py`:

| Переменная | Значение | Зачем |
|---|---|---|
| `WEBMAIL_LOCAL_MAILSERVER=1` | | включает управление ящиками из админки |
| `MAILSERVER_CONTAINER` | `mailserver` | цель для `docker exec` |
| `MAILSERVER_ACCOUNTS_FILE` | `/srv/mail/mail-server/config/postfix-accounts.cf` | прямая правка учёток вместо медленного `setup email add` |
| `MAIL_HOST`, `MAIL_IMAP_HOST`, `MAIL_SMTP_HOST` | `mail.example.com` | имя из сертификата; в `/etc/hosts` оно указывает на `127.0.0.1`, так что трафик не выходит наружу и `MAIL_SMTP_VERIFY_TLS` остаётся включённым |

Сервис `rupochta.service` работает от пользователя `rupochta` в группе `docker`
— это и есть право заводить ящики. Доступ к сокету Docker равносилен root на
машине, поэтому VM отдана только под почту.

## Эксплуатация

```bash
systemctl status rupochta nginx
curl -sf http://127.0.0.1:18400/ready          # доступны ли IMAP и SMTP
docker exec mailserver setup debug show-mail-logs
docker exec mailserver postqueue -p            # очередь отправки
docker exec mailserver setup email list
certbot renew --dry-run                        # обновление сертификата
```

Про файрвол: `ufw` фильтрует только то, что слушает сам хост (SSH, nginx).
Порты почтового контейнера Docker публикует в обход ufw, поэтому наружу открыто
ровно то, что перечислено в `docker-compose.mail.yml` — 25, 465, 587, 993;
незашифрованный 143 привязан к петле. Если нужен внешний фильтр — используйте
файрвол Proxmox на уровне VM.

Сертификат один на всё: deploy-hook `/etc/letsencrypt/renewal-hooks/deploy/rupochta.sh`
после обновления перезагружает nginx и перезапускает контейнер.

**Обновление RuПочты** — повторный `bootstrap.sh` либо вручную:

```bash
sudo git -C /opt/rupochta/app pull && sudo systemctl restart rupochta
```

**Резервная копия.** Достаточно снапшота VM в PVE плюс регулярного бэкапа
`/srv/mail/mail-server/mail-data` (письма), `/srv/mail/mail-server/config` (ящики, DKIM,
фильтры), `/var/lib/rupochta` (алиасы, привязки) и `/etc/rupochta` (секреты).
Снимайте бэкап PVE в режиме `snapshot` — контейнер останавливать не требуется.

## Почему VM, а не LXC

docker-mailserver в LXC требует привилегированного контейнера с `nesting=1`,
`keyctl=1` и правками AppArmor; Dovecot и fail2ban в такой связке ломаются на
обновлениях ядра хоста. Почтовый сервер смотрит в интернет 25-м портом —
изоляция VM здесь дешевле, чем разбор регрессий. Если LXC всё же нужен,
поднимайте Postfix/Dovecot пакетами вместо Docker; тогда админ-панель RuПочты
ящиками управлять не сможет (`WEBMAIL_LOCAL_MAILSERVER=0`), и заводить их
придётся вручную.

## Файлы

| Файл | Где выполняется | Что делает |
|---|---|---|
| `provision-vm.sh` | хост PVE | создаёт VM из cloud-образа Debian |
| `bootstrap.sh` | гость | ставит Docker, DMS, TLS, RuПочту, nginx, ufw |
| `docker-compose.mail.yml` | гость | описание контейнера `mailserver` |
| `env/mailserver.env.example` | шаблон | переменные docker-mailserver |
| `env/rupochta.env.example` | шаблон | переменные веб-интерфейса |
| `systemd/rupochta.service` | гость | юнит веб-интерфейса |
| `nginx/rupochta.conf` | гость | TLS и обратный прокси |
| `dns-records.sh` | гость | печатает MX/SPF/DKIM/DMARC/PTR |
