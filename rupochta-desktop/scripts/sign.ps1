# sign.ps1 — подпись кода (Authenticode) артефактов RuPochta Desktop
# Использование:
#   .\sign.ps1 -PfxPath rupochta.pfx -Password "***"
#   .\sign.ps1 -Thumbprint ABC123...   (сертификат уже в хранилище)
#   .\sign.ps1 -SelfSignedTest        (создать самоподписанный и подписать — только тест!)
param(
  [string]$PfxPath,
  [string]$Password,
  [string]$Thumbprint,
  [switch]$SelfSignedTest,
  [string]$TimestampUrl = "http://timestamp.digicert.com",
  [string]$TargetDir = "src-tauri\target\release"
)

$ErrorActionPreference = "Stop"

# найти signtool
$signtool = Get-ChildItem "C:\Program Files (x86)\Windows Kits\10\bin" -Recurse -Filter "signtool.exe" -ErrorAction SilentlyContinue |
  Where-Object { $_.FullName -match "x64" } | Select-Object -First 1
if (-not $signtool) { throw "signtool.exe не найден — установите Windows SDK (Windows Kits 10)" }
Write-Host "signtool: $($signtool.FullName)"

# собрать список файлов для подписи
$files = @()
$portable = Join-Path $TargetDir "rupochta-desktop.exe"
if (Test-Path $portable) { $files += $portable }
$files += Get-ChildItem (Join-Path $TargetDir "bundle\nsis\*-setup.exe") -ErrorAction SilentlyContinue
$files += Get-ChildItem (Join-Path $TargetDir "bundle\msi\*.msi") -ErrorAction SilentlyContinue
if ($files.Count -eq 0) { throw "Не найдены артефакты для подписи в $TargetDir — сначала соберите: npm run tauri build" }

# определить параметр сертификата
$certArg = $null
if ($SelfSignedTest) {
  $cert = New-SelfSignedCertificate -Type CodeSigningCert -Subject "CN=RuPochta Test" `
    -CertStoreLocation Cert:\CurrentUser\My -NotAfter (Get-Date).AddYears(1)
  Write-Host "Создан self-signed сертификат: $($cert.Thumbprint)"
  $certArg = @("/sha1", $cert.Thumbprint)
} elseif ($Thumbprint) {
  $certArg = @("/sha1", $Thumbprint)
} elseif ($PfxPath) {
  $certArg = @("/f", (Resolve-Path $PfxPath).Path)
  if ($Password) { $certArg += @("/p", $Password) }
} else {
  throw "Укажите -PfxPath, -Thumbprint или -SelfSignedTest"
}

foreach ($f in $files) {
  Write-Host "Подписываю: $f"
  & $signtool.FullName sign /fd SHA256 /tr $TimestampUrl /td SHA256 @certArg $f
  if ($LASTEXITCODE -ne 0) { throw "signtool завершился с кодом $LASTEXITCODE на $f" }
  & $signtool.FullName verify /pa /v $f | Select-String "Verified: Signed" | ForEach-Object { Write-Host "  OK: $_" }
}

Write-Host "Готово: подписано $($files.Count) файлов"
