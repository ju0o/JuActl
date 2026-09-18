# JuActl Board exe builder (MainPC PowerShell 5.1+, run line by line)
# Output: dist/JuActlBoard.exe (single file, no console, Python bundled)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { throw "python not found in PATH (need 3.10+)" }
pip install --upgrade pyinstaller
pyinstaller --noconfirm --onefile --noconsole --name JuActlBoard --paths src src/juactl-board.py
Write-Host "Built: $root\dist\JuActlBoard.exe"
Write-Host "Run: .\\dist\\JuActlBoard.exe (ssh asus baked in; override with JUACTL_SSH env)"
