# JuActl Board exe builder (MainPC PowerShell 5.1+, run line by line)
# Output: dist/JuActlBoard.exe + dist/JuActl.exe (single files, Python bundled)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { throw "python not found in PATH (need 3.10+)" }
python -m pip show pyinstaller *> $null
if ($LASTEXITCODE -ne 0) {
  python -m pip install pyinstaller
}
python -m PyInstaller --clean --noconfirm --onefile --noconsole --icon packaging\juactl.ico --name JuActlBoard --paths src src/juactl-board.py
python -m PyInstaller --clean --noconfirm --onefile --console --icon packaging\juactl.ico --name JuActl --paths src src/actl-run.py
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
