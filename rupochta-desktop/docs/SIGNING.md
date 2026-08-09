# Подпись кода (Authenticode) — гайд

Tauri-подпись **обновлений** (minisign, `.sig` файлы) и подпись **кода Windows**
(Authenticode) — это разные вещи. Этот документ — про подпись кода: чтобы Windows
не показывал «Неизвестный издатель» и SmartScreen не блокировал установщик.

## Что нужно

Сертификат подписи кода от доверенного центра (CA):

| Вариант | Цена | Время | SmartScreen |
|---|---|---|---|
| **Azure Trusted Signing** (Microsoft) | ~10 $/мес | часы | ✅ снимает (новый стандарт) |
| OV-сертификат (Sectigo, DigiCert и др.) | 150–300 $/год | 2–5 дней | ⚠️ частично (нужна репутация) |
| EV-сертификат | 300–600 $/год | 1–2 дня | ✅ снимает сразу |
| Self-signed (тест) | 0 | 1 мин | ❌ только для внутренних тестов |

## Self-signed (для теста, локально)

```powershell
# 1. Создать самоподписанный сертификат подписи кода
$cert = New-SelfSignedCertificate -Type CodeSigningCert `
  -Subject "CN=RuPochta Test" -CertStoreLocation Cert:\CurrentUser\My `
  -NotAfter (Get-Date).AddYears(1)

# 2. Экспорт в PFX (защитить паролем)
$pw = ConvertTo-SecureString -String "ваш-пароль" -Force -AsPlainText
Export-PfxCertificate -Cert $cert -FilePath rupochta-test.pfx -Password $pw

# 3. Подписать (signtool из Windows SDK — уже установлен)
signtool sign /fd SHA256 /f rupochta-test.pfx /p "ваш-пароль" `
  target/release/rupochta-desktop.exe

# 4. Проверка
signtool verify /pa /v target/release/rupochta-desktop.exe
```

⚠️ Self-signed не снимает SmartScreen: пользователь должен вручную установить
сертификат в «Доверенные корневые центры» (подходит только для внутренних тестов).

## Платный сертификат (релиз)

1. Купить OV/EV-сертификат или Azure Trusted Signing
2. Экспортировать в **PFX** (с паролем) или использовать HSM/облачный signer
3. Подписать все артефакты:
   ```powershell
   signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 `
     /f rupochta.pfx /p "<пароль>" `
     target/release/rupochta-desktop.exe `
     target/release/bundle/nsis/RuPochta_*_x64-setup.exe `
     target/release/bundle/msi/RuPochta_*_x64_en-US.msi
   ```
   `/tr ... /td SHA256` — RFC3161 timestamp (обязателен: подпись не истекает)
4. Проверить: `signtool verify /pa /v <файл>` — должно быть «Verified: Signed»

## Интеграция в CI (GitHub Actions)

Вариант А — **Azure Trusted Signing** (рекомендуется, ключ не покидает Azure):

```yaml
- name: Sign with Azure Trusted Signing
  uses: azure/trusted-signing-action@v0
  with:
    endpoint: https://eus.codesigning.azure.net
    trusted-signing-account-name: ${{ secrets.AZURE_SIGN_ACCOUNT }}
    certificate-profile-name: ${{ secrets.AZURE_SIGN_PROFILE }}
    files: |
      rupochta-desktop/src-tauri/target/release/bundle/nsis/*.exe
      rupochta-desktop/src-tauri/target/release/bundle/msi/*.msi
```

Вариант Б — **PFX-сертификат** (secrets: `CODESIGN_PFX_BASE64`, `CODESIGN_PASSWORD`):

```yaml
- name: Decode PFX
  shell: pwsh
  run: |
    [IO.File]::WriteAllBytes("codesign.pfx", [Convert]::FromBase64String("${{ secrets.CODESIGN_PFX_BASE64 }}"))
- name: Sign
  shell: pwsh
  run: |
    & "C:\Program Files (x86)\Windows Kits\10\bin\*\x64\signtool.exe" sign /fd SHA256 `
      /tr http://timestamp.digicert.com /td SHA256 /f codesign.pfx `
      /p "${{ secrets.CODESIGN_PASSWORD }}" `
      rupochta-desktop/src-tauri/target/release/bundle/nsis/*.exe `
      rupochta-desktop/src-tauri/target/release/bundle/msi/*.msi
```

## Порядок операций

Правильный порядок при выпуске релиза:
1. `cargo tauri build` (с TAURI_SIGNING_PRIVATE_KEY → `.sig` обновлений)
2. **signtool sign** — подпись кода всех exe/msi
3. Публикация в Release (latest.json + установщики + .sig)
4. Проверка на чистой Windows: установка без предупреждений

## Быстрый чек-лист

- [ ] Сертификат куплен/получен (OV/EV или Azure Trusted Signing)
- [ ] PFX экспортирован, пароль в GitHub secrets
- [ ] Подписаны: портативный exe, NSIS setup, MSI
- [ ] RFC3161 timestamp добавлен (`/tr /td SHA256`)
- [ ] `signtool verify /pa` — «Verified: Signed»
- [ ] Установка на чистой машине без SmartScreen-блока
