#define MyAppName "Anastasia (Anna)"
#define MyAppExeName "Anastasia.exe"
#define MyAppPublisher "Local project"
#define MyAppVersion GetEnv("ANNA_VERSION")
#if MyAppVersion == ""
  #define MyAppVersion "0.1.0"
#endif

[Setup]
AppId={{BDA93B5A-6B1F-4F21-A9A9-80C3FBE9D219}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\Anastasia
DefaultGroupName=Anastasia
OutputDir=..\dist\installer
OutputBaseFilename=AnastasiaSetup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}

[Files]
Source: "..\dist\Anastasia\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Anastasia"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{autodesktop}\Anastasia"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: checkedonce
; D-0.3 auto-start: OPT-IN, default OFF (the `unchecked` flag, never `checkedonce`).
; Editing machine startup needs the user's explicit consent (Protocol §4).
Name: "autostart"; Description: "Start Anna automatically when I log in"; GroupDescription: "Startup:"; Flags: unchecked

[Run]
; Register the ONLOGON Task Scheduler entry only if the user ticked the box.
Filename: "{app}\{#MyAppExeName}"; Parameters: "--enable-autostart"; Flags: runhidden skipifsilent; Tasks: autostart
Filename: "{app}\{#MyAppExeName}"; Description: "Launch Anastasia"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Always remove the scheduled task on uninstall — leave no machine-startup
; edit behind. `runhidden`, and don't block uninstall if it's already gone.
Filename: "{app}\{#MyAppExeName}"; Parameters: "--disable-autostart"; Flags: runhidden; RunOnceId: "RemoveAnnaAutostart"

[UninstallDelete]
Type: filesandordirs; Name: "{app}"