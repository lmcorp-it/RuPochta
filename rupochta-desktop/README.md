# RuPochta Desktop (Tauri 2)

Настольное Windows-приложение: стартовая страница рупочта.рф (вход + регистрация + рабочее
место «Фокус»), упакованная в Tauri 2. Фронтенд — та же V3-сборка, что и веб-версия
(дизайн, состояния, доступность), рантайм — WebView2 (предустановлен в Windows 10/11).

## Структура

```
rupochta-desktop/
├── web/                      ← фронтенд (V3)
│   ├── index.html            ← вход + регистрация + Фокус (#login / #signup / #workspace)
│   └── canvas.html           ← дизайн-холст (в приложении не используется, для справки)
└── src-tauri/
    ├── tauri.conf.json       ← окно, имя продукта, цели сборки (nsis + msi), иконки
    ├── Cargo.toml            ← tauri 2, serde
    ├── build.rs
    ├── src/main.rs, lib.rs   ← точка входа
    └── icons/                ← иконки из логотипа (32/128/256/512, .ico)
```

## Сборка

Требования: Rust (rustup), MSVC Build Tools, WebView2 (есть в Windows 11).
Tauri автоматически скачает WiX (для .msi) и NSIS (для .exe-установщика) при первой сборке.

```bash
cargo install tauri-cli --version "^2" --locked   # один раз
cd src-tauri
cargo tauri build                                  # release: .exe + .msi в target/release/bundle/
```

Результат:
- `target/release/bundle/nsis/RuPochta_0.2.0_x64-setup.exe` — установщик (NSIS, русский язык)
- `target/release/bundle/msi/RuPochta_0.2.0_x64_en-US.msi` — MSI-пакет (WiX)
- `target/release/rupochta-desktop.exe` — портативный exe (без установки)

## Запуск без сборки

```bash
cd src-tauri
cargo tauri dev               # окно с живой перезагрузкой
```

## Как менять

| Что | Где |
|---|---|
| Тексты, цвета, шрифты | `web/index.html` (CSS-переменные в `<style>`, подписи форм) |
| Размер/заголовок окна | `src-tauri/tauri.conf.json` → `app.windows[0]` |
| Иконка приложения | заменить файлы в `src-tauri/icons/` (или `tauri icon <source.png>`) |
| Имя продукта/версия | `src-tauri/tauri.conf.json` → `productName` / `version` |
| Цели установщиков | `bundle.targets` (`nsis`, `msi`) |

## Верификация

- `cargo tauri build` проходит без ошибок (компиляция + NSIS + WiX)
- Приложение открывает V3-страницу: вход → регистрация → «Фокус»; переключатель устройств работает
- Иконка и заголовок окна применяются из конфига

## Ограничения

- В демо-режиме: формы валидируются, но реальные ящики не создаются (нужен IMAP/SMTP-бэкенд)
- Сборка выполняется под текущую архитектуру (x64)
