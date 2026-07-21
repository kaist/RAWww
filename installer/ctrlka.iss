#define MyAppName "Контролька"
#include "..\build\installer_version.iss"
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
SetupIconFile=..\src\rawww\assets\ctrlka-icon.ico
LanguageDetectionMethod=uilanguage
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "german"; MessagesFile: "compiler:Languages\German.isl"
; Упрощённый китайский не входит в стандартную поставку Inno Setup. Положите
; неофициальный ChineseSimplified.isl рядом с этим .iss — язык подключится сам.
#if FileExists(AddBackslash(SourcePath) + "ChineseSimplified.isl")
Name: "chinesesimplified"; MessagesFile: "ChineseSimplified.isl"
#endif

[CustomMessages]
russian.CreateDesktopIcon=Создать ярлык на рабочем столе
russian.AdditionalIcons=Дополнительные значки:
english.CreateDesktopIcon=Create a desktop shortcut
english.AdditionalIcons=Additional icons:
german.CreateDesktopIcon=Desktop-Verknüpfung erstellen
german.AdditionalIcons=Zusätzliche Symbole:
#if FileExists(AddBackslash(SourcePath) + "ChineseSimplified.isl")
chinesesimplified.CreateDesktopIcon=创建桌面快捷方式
chinesesimplified.AdditionalIcons=附加图标：
#endif

[Files]
Source: "..\dist\ctrlka\*"; DestDir: "{app}"; Excludes: "work\*"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Registry]
Root: HKCU; Subkey: "Software\ctrlka\ctrlka"; Flags: uninsdeletekeyifempty

[UninstallDelete]
Type: filesandordirs; Name: "{app}\work"
Type: filesandordirs; Name: "{localappdata}\ctrlka"
Type: filesandordirs; Name: "{userappdata}\ctrlka"
