#!/usr/bin/env bash
# Создание виртуальной машины под почтовый сервер RuПочты на хосте Proxmox VE.
#
# Запускать НА ГИПЕРВИЗОРЕ (PVE 8.0+), от root:
#
#   VMID=210 MAIL_FQDN=mail.example.com IPCONFIG='ip=192.0.2.10/24,gw=192.0.2.1' \
#     ./provision-vm.sh
#
# Скрипт только создаёт и запускает VM с cloud-init. Установка почтового
# стека — bootstrap.sh, который запускается уже внутри гостя.
set -euo pipefail

VMID="${VMID:?укажите VMID, например VMID=210}"
MAIL_FQDN_INPUT="${MAIL_FQDN:?укажите MAIL_FQDN, например MAIL_FQDN=mail.example.com}"
IPCONFIG="${IPCONFIG:-ip=dhcp}"

# Кириллическое имя (mail.рупочта.рф) переводим в punycode: имя VM, searchdomain
# и всё, что попадёт в cloud-init, должно быть ASCII.
if printf '%s' "$MAIL_FQDN_INPUT" | LC_ALL=C grep -q '[^ -~]'; then
  # На минимальной установке PVE python3 может отсутствовать — тогда просим
  # имя сразу в punycode, вместо невнятного «command not found».
  command -v python3 >/dev/null || {
    echo "Для кириллического имени нужен python3, которого нет на этом хосте." >&2
    echo "Поставьте его (apt install python3) или задайте MAIL_FQDN в punycode," >&2
    echo "например MAIL_FQDN=mail.xn--80a1acdmd4a.xn--p1ai" >&2
    exit 1
  }
  MAIL_FQDN="$(printf '%s' "$MAIL_FQDN_INPUT" | python3 -c \
    'import sys; print(sys.stdin.read().strip().encode("idna").decode())')"
  echo "IDN-домен: $MAIL_FQDN_INPUT → $MAIL_FQDN"
else
  MAIL_FQDN="$MAIL_FQDN_INPUT"
fi

VM_NAME="${VM_NAME:-${MAIL_FQDN%%.*}}"
STORAGE="${STORAGE:-local-lvm}"
BRIDGE="${BRIDGE:-vmbr0}"
VLAN="${VLAN:-}"                    # тег VLAN, пусто — без тега
CORES="${CORES:-2}"
MEMORY="${MEMORY:-4096}"            # МиБ; ClamAV в docker-mailserver требует ~3 ГиБ
DISK_GB="${DISK_GB:-60}"
CIUSER="${CIUSER:-rupochta}"
SSHKEYS="${SSHKEYS:-/root/.ssh/authorized_keys}"
NAMESERVER="${NAMESERVER:-1.1.1.1 9.9.9.9}"
IMAGE_URL="${IMAGE_URL:-https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2}"
IMAGE_CACHE="${IMAGE_CACHE:-/var/lib/vz/template/cache}"

command -v qm >/dev/null || { echo "qm не найден — скрипт рассчитан на хост Proxmox VE" >&2; exit 1; }
if qm status "$VMID" >/dev/null 2>&1; then
  echo "VM $VMID уже существует — выберите другой VMID" >&2
  exit 1
fi
[ -r "$SSHKEYS" ] || { echo "Файл с публичными SSH-ключами не найден: $SSHKEYS" >&2; exit 1; }

image_name="$(basename "$IMAGE_URL")"
image_path="$IMAGE_CACHE/$image_name"
mkdir -p "$IMAGE_CACHE"
if [ ! -s "$image_path" ]; then
  echo "==> Скачиваю образ Debian: $IMAGE_URL"
  curl -fL --retry 3 -o "$image_path.part" "$IMAGE_URL"
  mv "$image_path.part" "$image_path"
fi

# Образ становится корневой файловой системой почтового сервера, а ссылка ведёт
# в мутабельный каталог /latest/. Сверяем контрольную сумму — в том числе для
# файла из кеша, который мог быть подменён между запусками.
#
# Отдельной подписи Debian в этих каталогах не публикует (рядом с образами
# лежит только SHA512SUMS), поэтому подлинность держится на TLS до
# cloud.debian.org. Кому этого мало — передайте выверенную сумму в IMAGE_SHA512,
# тогда скрипт сверится с ней и в сеть за списком не пойдёт.
echo "==> Проверяю контрольную сумму образа"
if [ -n "${IMAGE_SHA512:-}" ]; then
  sum_line="$IMAGE_SHA512  $image_name"
else
  sums_file="$(mktemp)"
  trap 'rm -f "$sums_file"' EXIT
  curl -fL --retry 3 -o "$sums_file" "${IMAGE_URL%/*}/SHA512SUMS"
  # Строку достаём отдельно: пустой вход sha512sum молча принял бы за «нечего
  # проверять», и несовпадение имени файла прошло бы незамеченным.
  sum_line="$(grep -E "[[:space:]]\*?${image_name}\$" "$sums_file" || true)"
  if [ -z "$sum_line" ]; then
    echo "В SHA512SUMS нет строки для $image_name — проверить образ нечем." >&2
    exit 1
  fi
fi
if ! printf '%s\n' "$sum_line" | (cd "$IMAGE_CACHE" && sha512sum -c --status -); then
  echo "Контрольная сумма образа не совпала с SHA512SUMS — образ повреждён или подменён." >&2
  echo "Удалите $image_path и запустите скрипт заново." >&2
  exit 1
fi
echo "    сумма совпала: $image_name"

net0="virtio,bridge=$BRIDGE"
[ -n "$VLAN" ] && net0="$net0,tag=$VLAN"

echo "==> Создаю VM $VMID ($VM_NAME)"
qm create "$VMID" \
  --name "$VM_NAME" \
  --cores "$CORES" \
  --cpu host \
  --memory "$MEMORY" \
  --balloon 0 \
  --ostype l26 \
  --machine q35 \
  --bios ovmf \
  --scsihw virtio-scsi-single \
  --net0 "$net0" \
  --agent enabled=1 \
  --serial0 socket \
  --vga serial0 \
  --onboot 1 \
  --description "RuПочта: docker-mailserver + webmail ($MAIL_FQDN)"

qm set "$VMID" --efidisk0 "$STORAGE:0,efitype=4m,pre-enrolled-keys=1"
qm set "$VMID" --scsi0 "$STORAGE:0,import-from=$image_path,discard=on,ssd=1"
qm set "$VMID" --ide2 "$STORAGE:cloudinit"
qm set "$VMID" --boot order=scsi0
qm disk resize "$VMID" scsi0 "${DISK_GB}G"

echo "==> Настраиваю cloud-init"
qm set "$VMID" \
  --ciuser "$CIUSER" \
  --sshkeys "$SSHKEYS" \
  --ipconfig0 "$IPCONFIG" \
  --nameserver "$NAMESERVER" \
  --searchdomain "${MAIL_FQDN#*.}" \
  --ciupgrade 1

echo "==> Запускаю VM"
qm start "$VMID"

cat <<EOF

VM $VMID создана и запущена.

Дальше:
  1. Дождитесь загрузки (qm terminal $VMID — выход Ctrl-O) и узнайте адрес:
       qm guest cmd $VMID network-get-interfaces
  2. Скопируйте каталог deploy/proxmox на гостя и запустите установку:
       scp -r deploy/proxmox $CIUSER@<адрес>:/tmp/
       ssh $CIUSER@<адрес> "sudo MAIL_FQDN=$MAIL_FQDN MAIL_DOMAIN=${MAIL_FQDN#*.} \\
         ACME_EMAIL=postmaster@${MAIL_FQDN#*.} bash /tmp/proxmox/bootstrap.sh"
  3. Пропишите DNS-записи из вывода dns-records.sh и снимите блокировку 25/tcp
     у провайдера, если она есть.

Важно для доставляемости: PTR-запись для внешнего IP должна указывать на
$MAIL_FQDN — это настраивается у хостера/провайдера, не в Proxmox.
EOF
