# Windows source/package QA. Full tmux runtime tests remain Linux-only until
# a Windows tmux/SSH fixture is provided.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host "[1/4] compile"
$files = Get-ChildItem src,tests -Recurse -Filter *.py
python -m py_compile $files.FullName

Write-Host "[2/4] package build"
$build = Join-Path $PSScriptRoot "build-exe.ps1"
& $build
$artifacts = @("JuActlBoard.exe", "JuActl.exe") | ForEach-Object { Join-Path $root ("dist\" + $_) }
foreach ($artifact in $artifacts) { if (-not (Test-Path $artifact)) { throw "missing $artifact" } }

Write-Host "[3/4] checksum"
$hashLines = foreach ($artifact in $artifacts) {
  "$((Get-FileHash $artifact -Algorithm SHA256).Hash)  $([IO.Path]::GetFileName($artifact))"
}
$hashLines | Set-Content (Join-Path $root "dist\SHA256SUMS.txt") -Encoding ascii

Write-Host "[4/5] local doctor JSON smoke"
$doctor = & (Join-Path $root "dist\JuActl.exe") doctor --json | ConvertFrom-Json
if ($null -eq $doctor.ok) { throw "doctor did not return a JSON contract" }

Write-Host "[5/5] source package smoke"
python src/actl-run.py --version
Write-Host "PACKAGE_QA_COMPLETE"
