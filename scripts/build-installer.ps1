# Build the single-click JuActl-Setup.exe installer.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$iscc = Get-Command iscc.exe -ErrorAction SilentlyContinue
if (-not $iscc) {
  $candidates = @(
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
  )
  $isccPath = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
  if ($isccPath) { $iscc = @{ Source = $isccPath } }
}
if (-not $iscc) {
  $choco = Get-Command choco.exe -ErrorAction SilentlyContinue
  if ($choco) {
    Write-Host "Inno Setup 6 not found; installing through Chocolatey..."
    & $choco.Source install innosetup --yes --no-progress
    $isccPath = @(
      "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
      "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1
    if ($isccPath) { $iscc = @{ Source = $isccPath } }
  }
}
if (-not $iscc) {
  throw "Inno Setup 6 not found. Install it from https://jrsoftware.org/isinfo.php, then rerun scripts\build-installer.ps1"
}

& "$PSScriptRoot\build-exe.ps1"
& $iscc.Source /Qp "$root\packaging\JuActl.iss"

$setup = Join-Path $root "dist\JuActl-Setup.exe"
if (-not (Test-Path $setup)) { throw "Installer was not produced: $setup" }
$hashLines = @("JuActlBoard.exe", "JuActl.exe", "JuActl-Setup.exe") | ForEach-Object {
  $path = Join-Path $root ("dist\" + $_)
  if (-not (Test-Path $path)) { throw "Missing release artifact: $path" }
  "$((Get-FileHash $path -Algorithm SHA256).Hash)  $_"
}
$hashLines | Set-Content (Join-Path $root "dist\SHA256SUMS.txt") -Encoding ascii
Write-Host "Installer: $setup"
Write-Host "SHA256SUMS: $root\dist\SHA256SUMS.txt"
