# Anastasia Installer

This project now has two Windows-friendly install paths.

## How Anna runs (Phase 0 split)

Anna runs as **two processes**: `anna-core` (a headless daemon that owns the
mic, brain, safety validator and a system-tray icon) and `anna-ui` (a thin
window that talks to core over a localhost WebSocket). This is the **default**
now:

```powershell
python app\main.py            # DEFAULT: starts core + tray, opens the window
python app\main.py --core     # headless daemon only (no window; tray "Open Anna" opens one)
python app\main.py --ui       # window only, against an already-running --core
python app\main.py --legacy   # ESCAPE HATCH: the old single-process app (no daemon, no socket)
```

Closing the window leaves core running in the tray; **Quit from the tray** tears
everything down (mic, camera, any metered Gemini Live session, then the event
log). If the split ever misbehaves, `--legacy` restores the exact pre-split
behaviour with one flag. `--doctor` reports the resolved mic device/host API and
any IPC-auth or tray issues.

From-source, the generated `Run-Anna.ps1` wraps these: `.\Run-Anna.ps1`
(single-process), `-Split`, `-Core`, `-Ui`.

## 1. Source install for this machine

Use this when you want Anna to run from this checkout with a virtual environment and a desktop shortcut.

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
.\installer\Install-Anastasia.ps1
```

Useful options:

```powershell
.\installer\Install-Anastasia.ps1 -PullOllamaModel
.\installer\Install-Anastasia.ps1 -DownloadPiperVoice
.\installer\Install-Anastasia.ps1 -NoDesktopShortcut
```

The script creates `.venv`, installs `requirements.txt`, writes `Run-Anna.ps1`, optionally creates a desktop shortcut, and runs `python app\main.py --doctor`.

## 2. Packaged Windows installer

Use this to build `dist\Anastasia` with PyInstaller and, when Inno Setup 6 is installed, `dist\installer\AnastasiaSetup-<version>.exe`.

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
.\installer\Build-Installer.ps1 -InstallBuildTools -Version 0.1.0
```

After the first run, `-InstallBuildTools` is optional:

```powershell
.\installer\Build-Installer.ps1 -Version 0.1.1
```

If Inno Setup 6 is not installed, the script still leaves a portable build at `dist\Anastasia` and prints a warning.

The packaged build bundles the split (`websockets` + `pystray` are in the
PyInstaller spec), so the frozen `Anastasia.exe` runs the default core+window,
`--core`, `--ui` and `--legacy` just like the source build.

**Auto-start (opt-in):** the setup wizard has a **"Start Anna automatically when
I log in"** checkbox, **default OFF**. Ticking it registers a Task Scheduler
`ONLOGON` task that launches `Anastasia.exe --core`; you can also toggle it later
under **Settings → General**. Unticking (or uninstalling) **removes** the task —
nothing edits machine startup without your consent.

## Voice data and secrets

The packaged installer intentionally excludes:

- `app\data\config.json`
- `app\data\memory.json`
- `app\data\history.sqlite`
- API keys and local user history

For a personal offline installer that includes local Piper runtime/model files, run:

```powershell
.\installer\Build-Installer.ps1 -Version 0.1.0 -IncludeLocalVoiceData
```

Even with that flag, config, memory, history, and keys are still excluded.

## Installed app data

When Anna is packaged as an `.exe`, config/history/memory are stored in:

```text
%LOCALAPPDATA%\Anastasia
```

When running from source, Anna keeps using `app\data` in the repo.