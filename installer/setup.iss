; autoReel Windows installer.
;
; Packages the already-built payload (Flutter release exe + PyInstaller
; backend exe + bundled ffmpeg/ffprobe) from installer\build\payload into a
; single self-contained setup exe. Build the payload first (see
; installer\README.md), then compile this with Inno Setup:
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\setup.iss
;
; Per-user install (no admin/UAC prompt needed) - PrivilegesRequired=lowest
; and DefaultDirName under {localappdata}, matching how the app already
; stores its own data (see backend/app/config.py's _default_data_dir, which
; also uses %APPDATA%).

#define AppName "autoReel"
#define AppVersion "1.0.0"
#define AppPublisher "odmundurmedia"
#define AppExeName "ai_video_editor_frontend.exe"
#define PayloadDir "build\payload"

[Setup]
AppId={{B6C8B4B1-6C0E-4A4B-9B36-3E7B6C6F8A5A}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=build\output
OutputBaseFilename={#AppName}-Setup-{#AppVersion}
SetupIconFile=..\frontend\windows\runner\resources\app_icon.ico
UninstallDisplayIcon={app}\{#AppExeName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "{#PayloadDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

; Note: the backend writes its own .env (API keys) and the app stores
; generated project data under %APPDATA%\AIVideoEditor, not under {app} - so
; a normal uninstall (which only removes {app}) never touches either.
; Intentionally not adding an [UninstallDelete] entry for them.
