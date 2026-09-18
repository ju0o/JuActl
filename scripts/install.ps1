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
$exe = "$root\dist\JuActlBoard.exe"
$desk = [IO.Path]::Combine([Environment]::GetFolderPath("Desktop"), "JuActl Board.lnk")
$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut($desk)
if (Test-Path $exe) {
  $sc.TargetPath = $exe
  $sc.Description = "JuActl agent board (single exe)"
} else {
  $board = "@echo off`r`nstart `"`" pythonw `"$root\src\juactl-board.py`" gui --ssh asus`r`n"
  $fallback = "$bin\actl-board.cmd"
  Set-Content -Path $fallback -Value $board -Encoding Ascii
  $sc.TargetPath = $fallback
  $sc.Description = "JuActl agent board (python fallback; build exe with scripts/build-exe.ps1)"
}
$sc.WorkingDirectory = $root
$sc.Save()
& python "$root\src\actl-run.py" --init
Write-Host "Installed: $bin\actl.cmd"
Write-Host "Installed: Desktop shortcut JuActl Board"
Write-Host "To build single exe: powershell -ExecutionPolicy Bypass -File scripts/build-exe.ps1"
Write-Host "Add to PATH once: setx PATH `"$env:PATH;$bin`""
