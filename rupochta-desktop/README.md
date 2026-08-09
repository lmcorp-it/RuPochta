# RuPochta Desktop (Tauri 2)

Настольное Windows-приложение: стартовая страница рупочта.рф (вход + регистрация + рабочее
место «Фокус»), упакованная в Tauri 2. Фронтенд — та же V3-сборка, что и веб-версия
(дизайн, состояния, доступность), рантайм — WebView2 (предустановлен в Windows 10/11).

Возможности:
- вход / регистрация / рабочее место «Фокус» (+ переключатель устройств)
- **автообновления** (Tauri updater): проверка при запуске и раз в час, баннер «Доступна новая версия»
- подпись обновлений (minisign-ключ), установщики NSIS + MSI

## Структура

```
rupochta-desktop/
├── package.json              ← @tauri-apps/cli (devDependency), скрипт tauri
├── web/                      ← фронтенд (V3)
│   └── index.html            ← вход + регистрация + Фокус (#login / #signup / #workspace) + updater-баннер
└── src-tauri/
    ├── tauri.conf.json       ← окно, имя продукта, цели (nsis + msi), updater (pubkey, endpoints)
    ├── Cargo.toml            ← tauri 2, tauri-plugin-updater, tauri-plugin-process
    ├── capabilities/default.json ← права: core, updater, process
    ├── build.rs
    ├── src/main.rs, lib.rs   ← точка входа + плагины updater/process
    └── icons/                ← иконки из логотипа (32/128/256/512, .ico)
```

## Сборка с подписью обновлений

Требования: Rust, MSVC Build Tools, WebView2, Node.js (для @tauri-apps/cli).

```bash
npm install
# ключ подписи обновлений — сгенерировать один раз:
#   cargo tauri signer generate -w ~/.rupochta-updater/rupochta-updater.key -p "<пароль>"
$env:TAURI_SIGNING_PRIVATE_KEY = Get-Content "$env:USERPROFILE\.rupochta-updater\rupochta-updater.key" -Raw
$env:TAURI_SIGNING_PRIVATE_KEY_PASSWORD = "<пароль>"
npm run tauri build   # создаёт exe/msi + .sig подписи обновлений
```

Результат:
- `target/release/bundle/nsis/RuPochta_*_x64-setup.exe` (+ `.sig`) — установщик NSIS
- `target/release/bundle/msi/RuPochta_*_x64_en-US.msi` (+ `.sig`) — MSI (WiX)
- `target/release/rupochta-desktop.exe` — портативный exe

## Автообновления (updater)

- Публичный ключ обновлений — в `tauri.conf.json` → `plugins.updater.pubkey`
- Endpoint: `https://github.com/lmcorp-it/RuPochta/releases/latest/download/latest.json`
- Приложение проверяет обновление при старте и раз в час (см. `web/index.html`, блок `updater-banner`):
  баннер с кнопкой «Обновить» → скачивание → установка → перезапуск
- `latest.json` собирает CI на каждый тег `desktop-v*` и публикует в Release

Выпуск новой версии:
```bash
git tag desktop-v0.3.0 && git push origin desktop-v0.3.0
```
CI подпишет артефакты (нужны secrets `TAURI_SIGNING_PRIVATE_KEY` + `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`),
соберёт latest.json и выложит черновик релиза.

### GitHub secrets (обязательно для подписи в CI)
1. `TAURI_SIGNING_PRIVATE_KEY` — содержимое файла `rupochta-updater.key` (целиком)
2. `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` — пароль ключа

## Подпись кода (Authenticode)

Tauri-подпись обновлений ≠ подпись кода Windows. Для снятия предупреждения SmartScreen
нужен сертификат кода (OV/EV или Azure Trusted Signing) и signtool:

```powershell
# пример (Windows SDK):
signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 /sha1 <THUMBPRINT> `
  target/release/rupochta-desktop.exe
```
См. `docs/SIGNING.md` — полный гайд (сертификат, self-signed для теста, CI-интеграция).

## Как менять

| Что | Где |
|---|---|
| Тексты, цвета, шрифты | `web/index.html` (CSS-переменные в `<style>`, подписи форм) |
| Текст updater-баннера | `web/index.html` → блок `updater-banner` |
| Размер/заголовок окна | `src-tauri/tauri.conf.json` → `app.windows[0]` |
| Иконка приложения | заменить файлы в `src-tauri/icons/` (или `tauri icon <source.png>`) |
| Имя продукта/версия | `src-tauri/tauri.conf.json` → `productName` / `version` (+ `package.json`) |
| Публичный ключ обновлений | `src-tauri/tauri.conf.json` → `plugins.updater.pubkey` |
| Endpoint обновлений | `src-tauri/tauri.conf.json` → `plugins.updater.endpoints` |
| Цели установщиков | `bundle.targets` (`nsis`, `msi`) |

## Верификация

- `npm run tauri build` проходит с подписью (появляются `.sig` файлы)
- Приложение открывает V3-страницу: вход → регистрация → «Фокус»; переключатель устройств работает
- Updater: без сети/без новой версии — тихо; с новой версией в latest.json — баннер + установка

## Ограничения

- В демо-режиме: формы валидируются, но реальные ящики не создаются (нужен IMAP/SMTP-бэкенд)
- Сборка выполняется под текущую архитектуру (x64)
- Без Authenticode-подписи Windows может показывать «Неизвестный издатель» (см. docs/SIGNING.md)
