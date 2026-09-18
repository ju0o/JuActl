# JuActl Board exe builder (MainPC PowerShell 5.1+, run line by line)
# Output: dist/JuActlBoard.exe + dist/JuActl.exe (single files, Python bundled)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { throw "python not found in PATH (need 3.10+)" }
$buildRoot = Join-Path $env:TEMP "juactl-pyinstaller"
$buildPython = Join-Path $buildRoot "Scripts\python.exe"
if (-not (Test-Path $buildPython)) {
  & $py.Source -m venv $buildRoot
  if ($LASTEXITCODE -ne 0) { throw "could not create isolated PyInstaller environment" }
}
$env:PIP_DISABLE_PIP_VERSION_CHECK = "1"
& $buildPython -m pip install --quiet --upgrade "pyinstaller>=6,<7"
if ($LASTEXITCODE -ne 0) { throw "could not install PyInstaller in isolated environment" }
& $buildPython -m PyInstaller --clean --noconfirm --onefile --noconsole --icon packaging\juactl.ico --name JuActlBoard --paths src src/juactl-board.py
if ($LASTEXITCODE -ne 0) { throw "JuActlBoard packaging failed" }
& $buildPython -m PyInstaller --clean --noconfirm --onefile --console --icon packaging\juactl.ico --name JuActl --paths src src/actl-run.py
if ($LASTEXITCODE -ne 0) { throw "JuActl CLI packaging failed" }
$artifacts = @("JuActlBoard.exe", "JuActl.exe") | ForEach-Object { Join-Path $root ("dist\" + $_) }
foreach ($artifact in $artifacts) {
  if (-not (Test-Path $artifact)) { throw "PyInstaller completed without producing $artifact" }
}
$hashLines = foreach ($artifact in $artifacts) {
  $hash = (Get-FileHash $artifact -Algorithm SHA256).Hash
  "$hash  $([IO.Path]::GetFileName($artifact))"
  Write-Host "Built: $artifact"
  Write-Host "SHA256: $hash"
}
$hashLines | Set-Content (Join-Path $root "dist\SHA256SUMS.txt") -Encoding ascii
Write-Host "Run: .\\dist\\JuActlBoard.exe (GUI; ssh asus baked in)"
Write-Host "Run: .\\dist\\JuActl.exe doctor (CLI)"
