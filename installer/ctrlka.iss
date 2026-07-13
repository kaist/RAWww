#define MyAppName "Контролька"
#define MyAppVersion "0.1.0"
#define MyAppPublisher "Контролька"
#define MyAppExeName "ctrlka.exe"

[Setup]
AppId={{B7C7D83A-8E48-4E44-9A6B-000000000001}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\ctrlka
DefaultGroupName={#MyAppName}
OutputDir=..\dist\installer
OutputBaseFilename=ctrlka-windows-setup
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}

[Files]
Source: "..\dist\ctrlka\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; GroupDescription: "Дополнительные значки:"

[Registry]
Root: HKCU; Subkey: "Software\ctrlka\ctrlka"; Flags: uninsdeletekeyifempty

[UninstallDelete]
Type: filesandordirs; Name: "{app}\work"
Type: filesandordirs; Name: "{localappdata}\ctrlka"
Type: filesandordirs; Name: "{userappdata}\ctrlka"
