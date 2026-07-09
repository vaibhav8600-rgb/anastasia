[CmdletBinding()]
param(
    [string]$Version = "0.1.0",
    [switch]$SkipTests,
    [switch]$InstallBuildTools,
    [switch]$IncludeLocalVoiceData
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $Root ".venv\Scripts\python.exe"

function Info([string]$Message) { Write-Host "[Anna installer] $Message" -ForegroundColor Cyan }
function Fail([string]$Message) { throw "[Anna installer] $Message" }

Set-Location $Root

if (-not (Test-Path $Python)) {
    Info "Creating .venv"
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) { & py -3 -m venv .venv } else { & python -m venv .venv }
}
if (-not (Test-Path $Python)) { Fail "Could not find .venv Python at $Python" }

Info "Installing runtime requirements"
& $Python -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { Fail "Could not install runtime requirements." }

if ($InstallBuildTools) {
    Info "Installing PyInstaller"
    & $Python -m pip install pyinstaller
    if ($LASTEXITCODE -ne 0) { Fail "Could not install PyInstaller." }
}

& $Python -m PyInstaller --version | Out-Null
if ($LASTEXITCODE -ne 0) {
    Fail "PyInstaller is missing. Re-run with -InstallBuildTools."
}

if (-not $SkipTests) {
    Info "Running tests before packaging"
    & $Python -m pytest tests -q --basetemp .phase_installer_tmp
    if ($LASTEXITCODE -ne 0) { Fail "Tests failed. Packaging stopped." }
}

if ($IncludeLocalVoiceData) {
    $env:ANNA_INCLUDE_LOCAL_VOICE_DATA = "1"
    Info "Including local voice runtime/model data. Config, memory, history, and keys are still excluded."
} else {
    Remove-Item Env:\ANNA_INCLUDE_LOCAL_VOICE_DATA -ErrorAction SilentlyContinue
}
$env:ANNA_VERSION = $Version

Info "Building PyInstaller app"
& $Python -m PyInstaller --clean --noconfirm packaging\anastasia.spec
if ($LASTEXITCODE -ne 0) { Fail "PyInstaller build failed." }

$InnoCandidates = @(
    "$env:ProgramFiles(x86)\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
)
$ISCC = $InnoCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($ISCC) {
    Info "Building Windows setup with Inno Setup"
    & $ISCC installer\Anastasia.iss
    if ($LASTEXITCODE -ne 0) { Fail "Inno Setup build failed." }
    Info "Installer output: dist\installer"
} else {
    Write-Warning "Inno Setup 6 was not found. The portable app is ready at dist\Anastasia. Install Inno Setup and rerun this script to create Setup.exe."
}