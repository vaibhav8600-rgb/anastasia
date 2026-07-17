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

# Generate Run-Anna.ps1. Default is the original single-process app (unchanged,
# safe). The Phase-0 split is opt-in via switches so you can drive the manual
# passes: -Split (anna-core in its own window + the anna-ui window), -Core
# (headless daemon only — for the D-0.5 windowless voice-confirm test), -Ui
# (window client only, against an already-running --core).
$launcherText = @'
[CmdletBinding()]
param(
    [switch]$Split,   # anna-core (background window) + anna-ui window
    [switch]$Core,    # headless daemon only (no window)
    [switch]$Ui,      # window client only (needs a running --core)
    [int]$Port = 8765
)
$Root = "__ROOT__"
$Python = "__PYTHON__"
Set-Location $Root

if ($Core) { & $Python app\main.py --core --port "$Port"; return }
if ($Ui)   { & $Python app\main.py --ui   --port "$Port"; return }
if ($Split) {
    Write-Host "[Anna] starting anna-core on port $Port ..." -ForegroundColor Cyan
    Start-Process -FilePath $Python `
        -ArgumentList 'app\main.py','--core','--port',"$Port" `
        -WorkingDirectory $Root
    Start-Sleep -Seconds 3          # let core bind the port before the UI dials in
    Write-Host "[Anna] opening the window (close the core window, or use the tray Quit, to stop)" -ForegroundColor Cyan
    & $Python app\main.py --ui --port "$Port"
    return
}
# Default: the original single-process app.
& $Python app\main.py
'@
$launcherText = $launcherText.Replace("__ROOT__", $Root).Replace("__PYTHON__", $Python)
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

Info "Done."
Info "  Start Anna (single process):        .\Run-Anna.ps1"
Info "  Phase-0 split (core + window):       .\Run-Anna.ps1 -Split"
Info "  Headless daemon only (D-0.5 test):   .\Run-Anna.ps1 -Core"
Info "  Window only (against a running core):.\Run-Anna.ps1 -Ui"