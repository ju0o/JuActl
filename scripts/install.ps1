# JuActl MainPC installer (Windows PowerShell 5.1+, single-line paste safe)
# Usage: git clone https://github.com/ju0o/JuActl.git juactl; cd juactl; powershell -ExecutionPolicy Bypass -File scripts/install.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$bin = "$env:USERPROFILE\.local\bin"
New-Item -ItemType Directory -Force $bin | Out-Null
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { throw "python not found in PATH (need 3.10+)" }
$shim = "@echo off`r`npython `"$root\src\actl-run.py`" %*`r`n"
Set-Content -Path "$bin\actl.cmd" -Value $shim -Encoding Ascii
$board = "@echo off`r`nstart `"`" http://100.82.108.31:8765/`r`n"
Set-Content -Path "$bin\actl-board.cmd" -Value $board -Encoding Ascii
$desk = [IO.Path]::Combine([Environment]::GetFolderPath("Desktop"), "JuActl Board.lnk")
$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut($desk)
$sc.TargetPath = "$bin\actl-board.cmd"
$sc.WorkingDirectory = $root
$sc.Description = "JuActl live board (asus web server)"
$sc.Save()
& python "$root\src\actl-run.py" --init
Write-Host "Installed: $bin\actl.cmd"
Write-Host "Installed: $bin\actl-board.cmd + Desktop shortcut JuActl Board"
Write-Host "Add to PATH once: setx PATH `"$env:PATH;$bin`""
