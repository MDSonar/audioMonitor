; Inno Setup script for Audio Auto-Leveler
; Auto-generated values are injected by build.ps1 via /D defines

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{B63AA54E-4130-4F6F-B902-79EB614B8180}
AppName=Audio Auto-Leveler
AppVersion={#AppVersion}
AppVerName=Audio Auto-Leveler v{#AppVersion}
AppPublisher=AudioLeveler
DefaultDirName={autopf}\AudioLeveler
DefaultGroupName=Audio Auto-Leveler
OutputDir=builds
OutputBaseFilename=AudioLeveler_v{#AppVersion}_Setup
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
UninstallDisplayIcon={app}\AudioLeveler.exe
WizardStyle=modern
SetupIconFile=
LicenseFile=

[Files]
Source: "dist\AudioLeveler\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Audio Auto-Leveler"; Filename: "{app}\AudioLeveler.exe"
Name: "{group}\Uninstall Audio Auto-Leveler"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Audio Auto-Leveler"; Filename: "{app}\AudioLeveler.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Run]
Filename: "{app}\AudioLeveler.exe"; Description: "Launch Audio Auto-Leveler"; Flags: nowait postinstall skipifsilent
