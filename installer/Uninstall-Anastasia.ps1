[CmdletBinding()]
param(
    [switch]$RemoveVenv,
    [switch]$RemoveLocalAppData
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$desktopShortcut = Join-Path ([Environment]::GetFolderPath("Desktop")) "Anastasia.lnk"
$launcher = Join-Path $Root "Run-Anna.ps1"

if (Test-Path $desktopShortcut) { Remove-Item -LiteralPath $desktopShortcut -Force }
if (Test-Path $launcher) { Remove-Item -LiteralPath $launcher -Force }
if ($RemoveVenv) {
    $venv = Join-Path $Root ".venv"
    if (Test-Path $venv) { Remove-Item -LiteralPath $venv -Recurse -Force }
}
if ($RemoveLocalAppData) {
    $data = Join-Path $env:LOCALAPPDATA "Anastasia"
    if (Test-Path $data) { Remove-Item -LiteralPath $data -Recurse -Force }
}
Write-Host "Anastasia shortcuts removed. Project files were left in place."