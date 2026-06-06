; Inno Setup script for VidAudDownloader — builds the next-next-finish Setup.exe.
; Compiled by build.ps1 (ISCC.exe). Produces dist\VidAudDownloader-Setup.exe.
;
; Per-user install (no admin / UAC) into %LOCALAPPDATA%\Programs\VidAudDownloader.
; Per-user matters: the folder stays writable so the app can auto-update yt-dlp
; into lib\ without elevation. Inno automatically gives the user an uninstaller
; (Start-menu shortcut + an entry in Windows "Apps & features").

#define AppName "Video & Audio Downloader"
#define AppShortName "VidAudDownloader"
#define AppVersion "1.0.0"
#define AppPublisher "VidAudDownloader"
#define AppExe "VidAudDownloader.exe"

[Setup]
; A stable, app-unique GUID (keep it the same across versions so upgrades replace).
AppId={{8F2A6C31-7E4D-4B9A-AE10-1C5D9F3B2A47}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\Programs\{#AppShortName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=dist
OutputBaseFilename={#AppShortName}-Setup
SetupIconFile=build\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
; Everything PyInstaller produced + the lib\ / runtime\ / ffmpeg\ that build.ps1
; placed next to the .exe.
Source: "dist\VidAudDownloader\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Remove everything the app creates at runtime (downloaded yt-dlp updates,
; bytecode caches, the log) so uninstall leaves nothing behind. User downloads
; live in a separate folder and are never touched.
Type: filesandordirs; Name: "{app}\lib"
Type: filesandordirs; Name: "{app}\lib_update"
Type: filesandordirs; Name: "{app}\__pycache__"
Type: files; Name: "{app}\downloader.log"

[Registry]
; Drop the app's saved settings (QSettings -> HKCU\Software\VidAudDownloader) on uninstall.
Root: HKCU; Subkey: "Software\VidAudDownloader"; Flags: dontcreatekey uninsdeletekey
