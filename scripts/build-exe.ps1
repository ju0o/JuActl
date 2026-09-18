# JuActl Board exe builder (MainPC PowerShell 5.1+, run line by line)
# Output: dist/JuActlBoard.exe (single file, no console, Python bundled)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { throw "python not found in PATH (need 3.10+)" }
python -m pip show pyinstaller *> $null
if ($LASTEXITCODE -ne 0) {
  python -m pip install pyinstaller
}
python -m PyInstaller --clean --noconfirm --onefile --noconsole --name JuActlBoard --paths src src/juactl-board.py
$artifact = Join-Path $root "dist\JuActlBoard.exe"
if (-not (Test-Path $artifact)) { throw "PyInstaller completed without producing $artifact" }
$hash = (Get-FileHash $artifact -Algorithm SHA256).Hash
"$hash  JuActlBoard.exe" | Set-Content (Join-Path $root "dist\SHA256SUMS.txt") -Encoding ascii
Write-Host "Built: $artifact"
Write-Host "SHA256: $hash"
Write-Host "Run: .\\dist\\JuActlBoard.exe (ssh asus baked in; override with JUACTL_SSH env)"
