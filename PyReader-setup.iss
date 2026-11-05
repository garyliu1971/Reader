; PyReader — Inno Setup 安装脚本
; 用法：ISCC.exe PyReader-setup.iss
; 产物：release\PyReader-Setup-<版本>.exe

#define MyAppName "PyReader"
#define MyAppVersion "0.4.0"
#define MyAppPublisher "garyliu"
#define MyAppExeName "PyReader.exe"

[Setup]
AppId={{B8C65F35-CB00-4DED-9EBE-AED31487801D}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=release
OutputBaseFilename=PyReader-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; 按用户安装（无需管理员权限），安装到 %LOCALAPPDATA%\Programs\PyReader
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}
SetupIconFile=assets\PyReader.ico
; 允许用户修改安装目录
DirExistsWarning=no
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："; Flags: unchecked
Name: "fileassoc"; Description: "关联电子书文件（epub / mobi / azw / azw3 / prc / md）"; GroupDescription: "文件关联："; Flags: unchecked
Name: "nvsapi"; Description: "安装离线自然语音适配器（NaturalVoiceSAPIAdapter，需管理员权限）"; GroupDescription: "附加组件："; Flags: unchecked

[Files]
Source: "dist\PyReader\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; 离线 TTS 模型（Melo / Kokoro / Piper），随程序一体分发，安装后无需联网即可离线朗读
Source: "installer_models\*"; DestDir: "{app}\models"; Flags: ignoreversion recursesubdirs createallsubdirs nocompression
; 附带说明文档（若存在）
Source: "README.txt"; DestDir: "{app}"; Flags: ignoreversion; 
; AI 密钥配置（DeepSeek 翻译 / Azure Speech 朗读），开箱即用
Source: ".env"; DestDir: "{app}"; Flags: ignoreversion
; 离线自然语音适配器（NaturalVoiceSAPIAdapter，MIT 协议）：把 Windows 11 讲述人自然语音桥接成 SAPI5 音色
Source: "third_party\nvsapi\x64\*"; DestDir: "{app}\nvsapi\x64"; Flags: ignoreversion recursesubdirs createallsubdirs; Tasks: nvsapi
Source: "third_party\nvsapi\LICENSE-NaturalVoiceSAPIAdapter.txt"; DestDir: "{app}\nvsapi"; Flags: ignoreversion; Tasks: nvsapi

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "立即运行 {#MyAppName}"; Flags: nowait postinstall skipifsilent
; 注册离线自然语音适配器（regsvr32 写 HKLM，需管理员：shellexec + runas 触发 UAC；仅勾选 nvsapi 时执行）
Filename: "{sys}\regsvr32.exe"; Parameters: "/s ""{app}\nvsapi\x64\NaturalVoiceSAPIAdapter.dll"""; StatusMsg: "正在注册离线自然语音适配器…"; Flags: runhidden waituntilterminated shellexec; Verb: runas; Tasks: nvsapi

[UninstallRun]
; 卸载时反注册自然语音适配器（仅当 DLL 存在，避免没装过也弹 UAC）
Filename: "{sys}\regsvr32.exe"; Parameters: "/u /s ""{app}\nvsapi\x64\NaturalVoiceSAPIAdapter.dll"""; Flags: runhidden waituntilterminated shellexec; Verb: runas; Check: NvsapiInstalled; RunOnceId: "unreg_nvsapi"

[Registry]
; ---- 文件关联（可选任务）----
Root: HKA; Subkey: "Software\Classes\.epub\OpenWithProgids"; ValueType: string; ValueName: "PyReader.epub"; ValueData: ""; Flags: uninsdeletevalue; Tasks: fileassoc
Root: HKA; Subkey: "Software\Classes\PyReader.epub"; ValueType: string; ValueName: ""; ValueData: "EPUB 电子书"; Flags: uninsdeletekey; Tasks: fileassoc
Root: HKA; Subkey: "Software\Classes\PyReader.epub\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\{#MyAppExeName},0"; Tasks: fileassoc
Root: HKA; Subkey: "Software\Classes\PyReader.epub\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" ""%1"""; Tasks: fileassoc

Root: HKA; Subkey: "Software\Classes\.mobi\OpenWithProgids"; ValueType: string; ValueName: "PyReader.mobi"; ValueData: ""; Flags: uninsdeletevalue; Tasks: fileassoc
Root: HKA; Subkey: "Software\Classes\PyReader.mobi"; ValueType: string; ValueName: ""; ValueData: "MOBI 电子书"; Flags: uninsdeletekey; Tasks: fileassoc
Root: HKA; Subkey: "Software\Classes\PyReader.mobi\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\{#MyAppExeName},0"; Tasks: fileassoc
Root: HKA; Subkey: "Software\Classes\PyReader.mobi\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" ""%1"""; Tasks: fileassoc

Root: HKA; Subkey: "Software\Classes\.azw\OpenWithProgids"; ValueType: string; ValueName: "PyReader.azw"; ValueData: ""; Flags: uninsdeletevalue; Tasks: fileassoc
Root: HKA; Subkey: "Software\Classes\PyReader.azw"; ValueType: string; ValueName: ""; ValueData: "AZW 电子书"; Flags: uninsdeletekey; Tasks: fileassoc
Root: HKA; Subkey: "Software\Classes\PyReader.azw\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\{#MyAppExeName},0"; Tasks: fileassoc
Root: HKA; Subkey: "Software\Classes\PyReader.azw\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" ""%1"""; Tasks: fileassoc

Root: HKA; Subkey: "Software\Classes\.azw3\OpenWithProgids"; ValueType: string; ValueName: "PyReader.azw3"; ValueData: ""; Flags: uninsdeletevalue; Tasks: fileassoc
Root: HKA; Subkey: "Software\Classes\PyReader.azw3"; ValueType: string; ValueName: ""; ValueData: "AZW3 电子书"; Flags: uninsdeletekey; Tasks: fileassoc
Root: HKA; Subkey: "Software\Classes\PyReader.azw3\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\{#MyAppExeName},0"; Tasks: fileassoc
Root: HKA; Subkey: "Software\Classes\PyReader.azw3\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" ""%1"""; Tasks: fileassoc

Root: HKA; Subkey: "Software\Classes\.prc\OpenWithProgids"; ValueType: string; ValueName: "PyReader.prc"; ValueData: ""; Flags: uninsdeletevalue; Tasks: fileassoc
Root: HKA; Subkey: "Software\Classes\PyReader.prc"; ValueType: string; ValueName: ""; ValueData: "PRC 电子书"; Flags: uninsdeletekey; Tasks: fileassoc
Root: HKA; Subkey: "Software\Classes\PyReader.prc\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\{#MyAppExeName},0"; Tasks: fileassoc
Root: HKA; Subkey: "Software\Classes\PyReader.prc\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" ""%1"""; Tasks: fileassoc

Root: HKA; Subkey: "Software\Classes\.md\OpenWithProgids"; ValueType: string; ValueName: "PyReader.md"; ValueData: ""; Flags: uninsdeletevalue; Tasks: fileassoc
Root: HKA; Subkey: "Software\Classes\PyReader.md"; ValueType: string; ValueName: ""; ValueData: "Markdown 文档"; Flags: uninsdeletekey; Tasks: fileassoc
Root: HKA; Subkey: "Software\Classes\PyReader.md\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\{#MyAppExeName},0"; Tasks: fileassoc
Root: HKA; Subkey: "Software\Classes\PyReader.md\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" ""%1"""; Tasks: fileassoc

[Code]
function NvsapiInstalled(): Boolean;
begin
  Result := FileExists(ExpandConstant('{app}\nvsapi\x64\NaturalVoiceSAPIAdapter.dll'));
end;
