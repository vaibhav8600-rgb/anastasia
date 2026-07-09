[CmdletBinding()]
param(
    [switch]$PullOllamaModel,
    [switch]$DownloadPiperVoice,
    [switch]$NoDesktopShortcut
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Launcher = Join-Path $Root "Run-Anna.ps1"

function Info([string]$Message) { Write-Host "[Anna install] $Message" -ForegroundColor Cyan }
function Warn([string]$Message) { Write-Warning "[Anna install] $Message" }

Set-Location $Root

if (-not (Test-Path $Python)) {
    Info "Creating .venv"
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) { & py -3 -m venv .venv } else { & python -m venv .venv }
}

Info "Installing Python dependencies"
& $Python -m pip install --upgrade pip
& $Python -m pip install -r requirements.txt

if ($DownloadPiperVoice) {
    Info "Downloading the default Piper voice"
    & $Python -m piper.download_voices --download-dir app\data\voices\piper en_US-lessac-low
}

if ($PullOllamaModel) {
    $ollama = Get-Command ollama -ErrorAction SilentlyContinue
    if ($ollama) {
        Info "Pulling Ollama model llama3.2:3b"
        & ollama pull llama3.2:3b
    } else {
        Warn "Ollama was not found on PATH. Install it from https://ollama.com/download/windows."
    }
}

$launcherText = @"
Set-Location `"$Root`"
& `"$Python`" app\main.py
"@
Set-Content -LiteralPath $Launcher -Value $launcherText -Encoding UTF8

if (-not $NoDesktopShortcut) {
    Info "Creating desktop shortcut"
    $desktop = [Environment]::GetFolderPath("Desktop")
    $shortcutPath = Join-Path $desktop "Anastasia.lnk"
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
    $shortcut.Arguments = "-ExecutionPolicy Bypass -File `"$Launcher`""
    $shortcut.WorkingDirectory = $Root
    $shortcut.IconLocation = $Python
    $shortcut.Save()
}

Info "Running health check"
& $Python app\main.py --doctor

Info "Done. Start Anna with: .\Run-Anna.ps1"