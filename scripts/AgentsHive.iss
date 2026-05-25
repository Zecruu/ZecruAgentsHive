; v1.10 AgentsHive Desktop installer — Inno Setup 6 script.
;
; Per Planner Q5: per-user install (no admin / UAC), unsigned. Bundles the
; PyInstaller --onedir output at dist\AgentsHive\ into a single .exe installer.
;
; Build with scripts\build_desktop.bat which invokes iscc.exe on this file
; after PyInstaller produces dist\AgentsHive\.

#define MyAppName "AgentsHive"
#define MyAppVersion "1.10.0"
#define MyAppPublisher "Zecru"
#define MyAppURL "https://github.com/Zecruu/ZecruAgentsHive"
#define MyAppExeName "AgentsHive.exe"

[Setup]
AppId={{A6E12F3A-7C9E-4F1A-B2D3-1F9A6C0E5D2B}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
; Per-user install: no admin elevation, less aggressive SmartScreen variant
; (Planner Q5).
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist
OutputBaseFilename=AgentsHive-Setup-{#MyAppVersion}
SetupIconFile=
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; Allow installer to run from a directory without admin write access.
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "startmenuicon"; Description: "Create a Start Menu shortcut"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
; Bundle the entire PyInstaller dist tree. The * + recursesubdirs combo picks
; up python313.dll, the agentshive package, templates, and every transitive dep.
Source: "..\dist\AgentsHive\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: startmenuicon
Name: "{userdesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; Optionally launch right after install — most users want to see the dashboard
; immediately to know it worked.
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Leave %LOCALAPPDATA%\AgentsHive\ (data.db, local.key, project local_paths)
; in place on uninstall so a re-install picks up the existing state. Users
; who want a clean slate can delete that folder manually — documented in README.
