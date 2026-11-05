; Inno Setup script: wraps dist\py-intercom\ into py-intercom-setup.exe.
; Run through scripts/build_exe.ps1, which passes /DAppVersion and /DSourceDir.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\py-intercom"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif

[Setup]
; Never change AppId: it is how a new version finds and replaces the old one.
AppId={{6F1D3B8A-2C47-4E59-9A0B-7D5E1C3F8A24}
AppName=py-intercom
AppVersion={#AppVersion}
AppVerName=py-intercom {#AppVersion}
AppPublisher=LFPoulain
AppPublisherURL=https://github.com/lfpoulain/py-intercom
AppSupportURL=https://github.com/lfpoulain/py-intercom/issues
DefaultDirName={autopf}\py-intercom
DefaultGroupName=py-intercom
UninstallDisplayIcon={app}\py-intercom.exe
SetupIconFile=..\src\py_intercom\img\logo.ico
OutputDir={#OutputDir}
OutputBaseFilename=py-intercom-setup
VersionInfoVersion={#AppVersion}
LicenseFile=..\LICENSE
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Program Files by default; the wizard offers a per-user install without admin rights.
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog
; Close a running py-intercom so its files can be replaced.
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "french"; MessagesFile: "compiler:Languages\French.isl"

[Tasks]
Name: "desktopicon"; Description: "Créer les raccourcis Régie et Poste sur le &Bureau"; GroupDescription: "Raccourcis :"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion
Source: "..\THIRD_PARTY_NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\licenses\OPUS.txt"; DestDir: "{app}"; DestName: "OPUS_LICENSE.txt"; Flags: ignoreversion

[Icons]
Name: "{group}\py-intercom"; Filename: "{app}\py-intercom.exe"; Comment: "Régie ou Poste, au choix au lancement"
Name: "{group}\py-intercom Régie"; Filename: "{app}\py-intercom.exe"; Parameters: "--role server"; IconFilename: "{app}\_internal\img\logo-server.ico"
Name: "{group}\py-intercom Poste"; Filename: "{app}\py-intercom.exe"; Parameters: "--role client"; IconFilename: "{app}\_internal\img\logo-client.ico"
Name: "{group}\Désinstaller py-intercom"; Filename: "{uninstallexe}"
Name: "{autodesktop}\py-intercom Régie"; Filename: "{app}\py-intercom.exe"; Parameters: "--role server"; IconFilename: "{app}\_internal\img\logo-server.ico"; Tasks: desktopicon
Name: "{autodesktop}\py-intercom Poste"; Filename: "{app}\py-intercom.exe"; Parameters: "--role client"; IconFilename: "{app}\_internal\img\logo-client.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\py-intercom.exe"; Description: "Lancer py-intercom"; Flags: nowait postinstall skipifsilent

; The presets and settings live in the user's py-intercom folder and are kept on uninstall.
