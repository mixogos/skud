; Inno Setup: установщик СКУД. Собирает seture.exe из готовой папки dist\SKUD.
; Сборка:  "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging\installer.iss
; (сначала должна быть собрана папка dist\SKUD через PyInstaller)

#define AppName "СКУД — распознавание лиц"
#define AppVersion "0.9.1"
#define AppExe "SKUD.exe"

[Setup]
; Постоянный идентификатор приложения — чтобы обновления ставились поверх, а не рядом.
AppId={{A3F1C2E7-9B4D-4E6A-8C12-7D5E0F3A1B29}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=СКУД
; При обновлении закрыть работающую программу и запустить её снова после установки.
AppMutex=Global\SKUD_App_Mutex
CloseApplications=yes
RestartApplications=yes
DefaultDirName={autopf}\SKUD
DefaultGroupName=СКУД
DisableProgramGroupPage=yes
; Установщик называется seture.exe и кладётся в папку dist.
OutputDir=..\dist
OutputBaseFilename=seture
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Без прав администратора — ставится в папку пользователя.
PrivilegesRequired=lowest
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExe}

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "Создать значок на рабочем столе"; GroupDescription: "Дополнительно:"

[Files]
Source: "..\dist\SKUD\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\СКУД"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\СКУД"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Запустить СКУД сейчас"; Flags: nowait postinstall skipifsilent
