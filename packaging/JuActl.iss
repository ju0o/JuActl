; JuActl single-click Windows installer.
#define AppVersion "0.1.1"

[Setup]
AppId={{B87D4C1D-4EA1-4F4D-9B0D-7F5CB2C2D7C1}
AppName=JuActl
AppVersion={#AppVersion}
AppPublisher=JuActl
SetupIconFile=..\packaging\juactl.ico
DefaultDirName={localappdata}\Programs\JuActl
DefaultGroupName=JuActl
OutputDir=..\dist
OutputBaseFilename=JuActl-Setup
PrivilegesRequired=lowest
UninstallDisplayIcon={app}\JuActlBoard.exe
Compression=lzma
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible

[Files]
Source: "..\dist\JuActlBoard.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\JuActl.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\SHA256SUMS.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autodesktop}\JuActl Board"; Filename: "{app}\JuActlBoard.exe"; WorkingDir: "{app}"
Name: "{group}\JuActl Board"; Filename: "{app}\JuActlBoard.exe"; WorkingDir: "{app}"
Name: "{group}\JuActl Doctor"; Filename: "{app}\JuActl.exe"; Parameters: "doctor"; WorkingDir: "{app}"

[Run]
Filename: "{app}\JuActlBoard.exe"; Description: "JuActl Board 실행"; Flags: nowait postinstall skipifsilent
