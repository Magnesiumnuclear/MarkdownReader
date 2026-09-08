; Markdown 閱讀器安裝檔（Inno Setup 6）
;
; 【只能經由 build.ps1 -Installer 編譯】版本號從 app\__init__.py 讀出後以 /DAppVersion 傳進來，
; 這裡不留預設值——留了就會做出一個「0.0.0」的安裝檔，比編譯失敗糟得多。
;
; 設計原則（與 tools\install_association.py 對齊，差異處會標明）：
;   * 只寫 HKCU、PrivilegesRequired=lowest：每使用者安裝、免系統管理員。
;   * 關聯指向原生轉交器 MarkdownOpen.exe：本體已開著時再雙擊 .md 約 10ms，
;     指向本體要 ~170ms（onefile 更要先自解壓，~1000ms）。
;   * 反安裝旗標分三種：ProgID 整棵是我們的才 uninsdeletekey；副檔名底下只加
;     OpenWithProgids 的「值」，反安裝只刪值（uninsdeletevalue），鍵空了才順手清
;     （uninsdeletekeyifempty）；副檔名鍵本身絕不寫也絕不刪——那是使用者的。
;   * 升級先整包清 {app}\_internal：Inno 不刪未列出的舊檔，殘留的舊版 Qt 外掛
;     會被掃描載入而當機。
;   * 不寫副檔名的預設值（--set-default 那一招）：Win10/11 由 UserChoice 主導，
;     寫了幾乎無效，反安裝又得寫程式判斷「是不是我們的」才能刪；不做反而乾淨。
;   * Default Programs 登錄（Capabilities）是安裝版特有，讓程式出現在 Windows
;     「預設應用程式」清單供使用者手動選；install_association.py 沒做這段。
;
; 測試用的覆寫：ClassesRoot / AppRegRoot / RegisteredAppsKey / UserDataDir 都可由
; 命令列 /D 改到測試專用子樹，回歸測試靠這個在完全不碰真正關聯的前提下驗證安裝
; 與反安裝。

#ifndef AppVersion
  #error AppVersion 未定義：請用 .\build.ps1 -Installer 編譯，它會從 app\__init__.py 讀版本
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\MarkdownReader-onedir"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif
#ifndef OutputBaseFilename
  #define OutputBaseFilename "MarkdownReader-Setup-" + AppVersion
#endif
#ifndef ClassesRoot
  #define ClassesRoot "Software\Classes"
#endif
#ifndef AppRegRoot
  #define AppRegRoot "Software\SamHo\MarkdownReader"
#endif
#ifndef RegisteredAppsKey
  #define RegisteredAppsKey "Software\RegisteredApplications"
#endif
#ifndef UserDataDir
  #define UserDataDir "{localappdata}\MarkdownReader"
#endif
#define ProgId "MarkdownReader.md"
; AppId 與群組名也可覆寫：回歸測試裝到暫存目錄時要用**不同的** AppId，否則 Inno
; 會把它當成既有安裝的升級（沿用舊安裝位置、覆蓋反安裝登錄項），撞上真實安裝。
#ifndef AppId
  #define AppId "{{7B0D2E9E-5C0F-4B2A-9D3A-2F5C7C1E8A41}"
#endif
#ifndef GroupName
  #define GroupName "{cm:AppName}"
#endif

[Setup]
; AppId 固定：同一個 GUID 再裝就是就地升級，換了就會變成兩份並存
AppId={#AppId}
AppName=Markdown Reader
AppVersion={#AppVersion}
; 版本資源只吃四段數字；顯示用的 AppVersion 維持三段
VersionInfoVersion={#AppVersion}.0
AppPublisher=SamHo
DefaultDirName={localappdata}\Programs\MarkdownReader
DefaultGroupName={#GroupName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
ChangesAssociations=yes
; 靠 Restart Manager 偵測本體佔用的 DLL 來提示關閉；本專案沒有常駐 mutex，
; 不設 AppMutex（設了也是空包彈，只會讓人以為有在保護）
CloseApplications=yes
RestartApplications=no
OutputDir={#OutputDir}
OutputBaseFilename={#OutputBaseFilename}
SetupIconFile=..\assets\app.ico
UninstallDisplayIcon={app}\MarkdownReader.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ShowLanguageDialog=auto

[Languages]
; 繁中語言檔 vendored 在本目錄：安裝器本身不附帶它，靠 compiler:Languages 會編不過
Name: "zh_TW"; MessagesFile: "ChineseTraditional.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
zh_TW.AppName=Markdown 閱讀器
en.AppName=Markdown Reader
zh_TW.ProgIdDescription=Markdown 文件
en.ProgIdDescription=Markdown document
zh_TW.OpenVerb=以 Markdown 閱讀器開啟(&M)
en.OpenVerb=Open with Markdown &Reader
zh_TW.RemoveSettings=要一併移除使用者設定（主題、字級、工作階段）與錯誤紀錄嗎？%n%n選「否」會保留，下次安裝仍可沿用。
en.RemoveSettings=Also remove your settings (theme, font size, session) and the error log?%n%nChoose No to keep them for a future install.

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; 明列兩個 exe 而不用萬用字元：轉交器是條件式編譯（缺 g++ 就不會有），
; 來源缺檔時要讓 ISCC 在這裡直接失敗，而不是做出一個關聯指向空檔的安裝檔
Source: "{#SourceDir}\MarkdownReader.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\MarkdownOpen.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; 升級前整包清掉舊的 _internal（Inno 已先透過 CloseApplications 關掉本體，檔案才刪得掉）
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{group}\{cm:AppName}"; Filename: "{app}\MarkdownReader.exe"
Name: "{group}\{cm:UninstallProgram,{cm:AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{cm:AppName}"; Filename: "{app}\MarkdownReader.exe"; Tasks: desktopicon

[Registry]
; --- ProgID：整棵是我們的，反安裝整棵刪 ---
Root: HKCU; Subkey: "{#ClassesRoot}\{#ProgId}"; ValueType: string; ValueName: ""; ValueData: "{cm:ProgIdDescription}"; Flags: uninsdeletekey
Root: HKCU; Subkey: "{#ClassesRoot}\{#ProgId}\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\MarkdownOpen.exe,0"
Root: HKCU; Subkey: "{#ClassesRoot}\{#ProgId}\shell\open"; ValueType: string; ValueName: ""; ValueData: "{cm:OpenVerb}"
Root: HKCU; Subkey: "{#ClassesRoot}\{#ProgId}\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\MarkdownOpen.exe"" ""%1"""
; --- 六個副檔名：只加 OpenWithProgids 的值；副檔名鍵本身不寫、不刪 ---
; （清單必須與 app\config.py 的 MARKDOWN_SUFFIXES 一致，回歸測試會逐一比對）
Root: HKCU; Subkey: "{#ClassesRoot}\.md\OpenWithProgids"; ValueType: string; ValueName: "{#ProgId}"; ValueData: ""; Flags: uninsdeletevalue uninsdeletekeyifempty
Root: HKCU; Subkey: "{#ClassesRoot}\.markdown\OpenWithProgids"; ValueType: string; ValueName: "{#ProgId}"; ValueData: ""; Flags: uninsdeletevalue uninsdeletekeyifempty
Root: HKCU; Subkey: "{#ClassesRoot}\.mdown\OpenWithProgids"; ValueType: string; ValueName: "{#ProgId}"; ValueData: ""; Flags: uninsdeletevalue uninsdeletekeyifempty
Root: HKCU; Subkey: "{#ClassesRoot}\.mkd\OpenWithProgids"; ValueType: string; ValueName: "{#ProgId}"; ValueData: ""; Flags: uninsdeletevalue uninsdeletekeyifempty
Root: HKCU; Subkey: "{#ClassesRoot}\.mkdn\OpenWithProgids"; ValueType: string; ValueName: "{#ProgId}"; ValueData: ""; Flags: uninsdeletevalue uninsdeletekeyifempty
Root: HKCU; Subkey: "{#ClassesRoot}\.mdtxt\OpenWithProgids"; ValueType: string; ValueName: "{#ProgId}"; ValueData: ""; Flags: uninsdeletevalue uninsdeletekeyifempty
; --- Default Programs：讓程式出現在 Windows「預設應用程式」清單 ---
; Capabilities 住在 QSettings 的同一棵樹下（AppRegRoot），所以只能砍 Capabilities
; 子樹，絕不能對父鍵用 uninsdeletekey——那會把承諾要保留的使用者設定一起刪掉。
; ApplicationName／Description 用固定字串：這是給系統讀的，不能隨介面語言變。
Root: HKCU; Subkey: "{#AppRegRoot}\Capabilities"; ValueType: string; ValueName: "ApplicationName"; ValueData: "Markdown Reader"; Flags: uninsdeletekey
Root: HKCU; Subkey: "{#AppRegRoot}\Capabilities"; ValueType: string; ValueName: "ApplicationDescription"; ValueData: "Lightweight Markdown reader for Windows"
Root: HKCU; Subkey: "{#AppRegRoot}\Capabilities\FileAssociations"; ValueType: string; ValueName: ".md"; ValueData: "{#ProgId}"
Root: HKCU; Subkey: "{#AppRegRoot}\Capabilities\FileAssociations"; ValueType: string; ValueName: ".markdown"; ValueData: "{#ProgId}"
Root: HKCU; Subkey: "{#AppRegRoot}\Capabilities\FileAssociations"; ValueType: string; ValueName: ".mdown"; ValueData: "{#ProgId}"
Root: HKCU; Subkey: "{#AppRegRoot}\Capabilities\FileAssociations"; ValueType: string; ValueName: ".mkd"; ValueData: "{#ProgId}"
Root: HKCU; Subkey: "{#AppRegRoot}\Capabilities\FileAssociations"; ValueType: string; ValueName: ".mkdn"; ValueData: "{#ProgId}"
Root: HKCU; Subkey: "{#AppRegRoot}\Capabilities\FileAssociations"; ValueType: string; ValueName: ".mdtxt"; ValueData: "{#ProgId}"
Root: HKCU; Subkey: "{#RegisteredAppsKey}"; ValueType: string; ValueName: "MarkdownReader"; ValueData: "{#AppRegRoot}\Capabilities"; Flags: uninsdeletevalue

[Code]
{ 反安裝時問要不要連設定一起清；預設「否」（保留）。
  靜默反安裝（/VERYSILENT）時 SuppressibleMsgBox 直接回預設值，所以也是保留。
  清的範圍：QSettings 整棵（AppRegRoot，含 Capabilities）與 %LOCALAPPDATA%\MarkdownReader
  （error.log 所在）。Capabilities 子樹另有 uninsdeletekey 兜底，重複刪無妨。 }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
  begin
    if SuppressibleMsgBox(CustomMessage('RemoveSettings'), mbConfirmation,
                          MB_YESNO or MB_DEFBUTTON2, IDNO) = IDYES then
    begin
      RegDeleteKeyIncludingSubkeys(HKCU, '{#AppRegRoot}');
      DelTree(ExpandConstant('{#UserDataDir}'), True, True, True);
    end;
  end;
end;
