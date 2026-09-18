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
$ws = New-Object -ComObject WScript.Shell
if (Test-Path $exe) {
  $linkTarget = $exe
  $linkDesc = "JuActl agent board (single exe)"
} else {
  $board = "@echo off`r`nstart `"`" pythonw `"$root\src\juactl-board.py`" gui --ssh asus`r`n"
  $fallback = "$bin\actl-board.cmd"
  Set-Content -Path $fallback -Value $board -Encoding Ascii
  $linkTarget = $fallback
  $linkDesc = "JuActl agent board (python fallback; build exe with scripts/build-exe.ps1)"
}
$desk = [IO.Path]::Combine([Environment]::GetFolderPath("Desktop"), "JuActl Board.lnk")
$sc = $ws.CreateShortcut($desk)
$sc.TargetPath = $linkTarget
$sc.WorkingDirectory = $root
$sc.Description = $linkDesc
$sc.Save()
$startDir = [IO.Path]::Combine([Environment]::GetFolderPath("StartMenu"), "Programs", "JuActl")
New-Item -ItemType Directory -Force $startDir | Out-Null
$sc2 = $ws.CreateShortcut([IO.Path]::Combine($startDir, "JuActl Board.lnk"))
$sc2.TargetPath = $linkTarget
$sc2.WorkingDirectory = $root
$sc2.Description = $linkDesc
$sc2.Save()
$uninst = "@echo off`r`n"
$uninst += "del `"$desk`" 2>nul`r`n"
$uninst += "rmdir /s /q `"$startDir`" 2>nul`r`n"
$uninst += "del `"$bin\actl.cmd`" `"$bin\actl-board.cmd`" 2>nul`r`n"
$uninst += "echo JuActl shortcuts removed (repo at $root kept)`r`n"
Set-Content -Path "$bin\juactl-uninstall.cmd" -Value $uninst -Encoding Ascii
& python "$root\src\actl-run.py" --init
Write-Host "Installed: $bin\actl.cmd"
Write-Host "Installed: Desktop shortcut + StartMenu JuActl/JuActl Board"
Write-Host "Uninstall shortcuts: $bin\juactl-uninstall.cmd"
Write-Host "To build single exe: powershell -ExecutionPolicy Bypass -File scripts/build-exe.ps1"
Write-Host "Add to PATH once: setx PATH `"$env:PATH;$bin`""
