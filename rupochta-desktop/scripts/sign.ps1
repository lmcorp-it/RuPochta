# sign.ps1 - Authenticode code signing for RuPochta Desktop artifacts
# Usage:
#   .\sign.ps1 -PfxPath rupochta.pfx -Password "***"
#   .\sign.ps1 -Thumbprint ABC123...   (cert already in store)
#   .\sign.ps1 -SelfSignedTest        (create self-signed cert and sign - TEST ONLY!)
param(
  [string]$PfxPath,
  [string]$Password,
  [string]$Thumbprint,
  [switch]$SelfSignedTest,
  [string]$TimestampUrl = "http://timestamp.digicert.com",
  [string]$TargetDir = "src-tauri\target\release"
)

$ErrorActionPreference = "Stop"

# locate signtool
$signtool = Get-ChildItem "C:\Program Files (x86)\Windows Kits\10\bin" -Recurse -Filter "signtool.exe" -ErrorAction SilentlyContinue |
  Where-Object { $_.FullName -match "x64" } | Select-Object -First 1
if (-not $signtool) { throw "signtool.exe not found - install Windows SDK (Windows Kits 10)" }
Write-Host "signtool: $($signtool.FullName)"

# collect files to sign
$files = @()
$portable = Join-Path $TargetDir "rupochta-desktop.exe"
if (Test-Path $portable) { $files += $portable }
$files += Get-ChildItem (Join-Path $TargetDir "bundle\nsis\*-setup.exe") -ErrorAction SilentlyContinue
$files += Get-ChildItem (Join-Path $TargetDir "bundle\msi\*.msi") -ErrorAction SilentlyContinue
if ($files.Count -eq 0) { throw "No artifacts found in $TargetDir - build first: npm run tauri build" }

# determine certificate argument
$certArg = $null
if ($SelfSignedTest) {
  $cert = New-SelfSignedCertificate -Type CodeSigningCert -Subject "CN=RuPochta Test" `
    -CertStoreLocation Cert:\CurrentUser\My -NotAfter (Get-Date).AddYears(1)
  Write-Host "Self-signed cert created: $($cert.Thumbprint)"
  $certArg = @("/sha1", $cert.Thumbprint)
} elseif ($Thumbprint) {
  $certArg = @("/sha1", $Thumbprint)
} elseif ($PfxPath) {
  $certArg = @("/f", (Resolve-Path $PfxPath).Path)
  if ($Password) { $certArg += @("/p", $Password) }
} else {
  throw "Specify -PfxPath, -Thumbprint or -SelfSignedTest"
}

foreach ($f in $files) {
  Write-Host "Signing: $f"
  & $signtool.FullName sign /fd SHA256 /tr $TimestampUrl /td SHA256 @certArg $f
  if ($LASTEXITCODE -ne 0) { throw "signtool failed with code $LASTEXITCODE on $f" }
  try {
    $v = & $signtool.FullName verify /v $f 2>&1 | Out-String
    if ($v -match "Signed") { Write-Host "  OK: signed" } else { Write-Host "  WARN: verify output did not contain 'Signed'" }
  } catch {
    Write-Host "  INFO: verify chain warning (expected for self-signed / test certs)"
  }
}

Write-Host "Done: signed $($files.Count) file(s)"
