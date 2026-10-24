; Inno Setup script for GaugeDeck-Setup.exe. Build dist\GaugeDeck with PyInstaller first, then:
;   iscc /DAppVersion=1.3.0 packaging\windows\gaugedeck.iss
; Installs for the current user (no administrator password needed).
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6B1E2C0A-5F7D-4C8E-9A3B-2D4F6E8A1C57}
AppName=GaugeDeck
AppVersion={#AppVersion}
AppVerName=GaugeDeck {#AppVersion}
AppPublisher=Miguel Ycaza
AppPublisherURL=https://github.com/Cyoulater/gaugedeck
AppSupportURL=https://github.com/Cyoulater/gaugedeck/issues
DefaultDirName={autopf}\GaugeDeck
DefaultGroupName=GaugeDeck
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
LicenseFile=..\..\LICENSE
SetupIconFile=gaugedeck.ico
UninstallDisplayIcon={app}\GaugeDeck.exe
OutputDir=..\..\dist
OutputBaseFilename=GaugeDeck-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\..\dist\GaugeDeck\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\GaugeDeck"; Filename: "{app}\GaugeDeck.exe"
Name: "{autodesktop}\GaugeDeck"; Filename: "{app}\GaugeDeck.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\GaugeDeck.exe"; Description: "{cm:LaunchProgram,GaugeDeck}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; shortcuts GaugeDeck made for gauges pinned to the taskbar
Type: files; Name: "{userprograms}\GaugeDeck\GaugeDeck - *.lnk"
