# Anastasia Installer

This project now has two Windows-friendly install paths.

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