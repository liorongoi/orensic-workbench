; Inno Setup script for the Forensic Workbench Windows installer.
; Built by deploy/windows/build-installer.ps1, which assembles the payload folder and runs:
;   ISCC.exe /DAppVersion=0.3.0 /DPayloadDir=<payload> /DOutputDir=<dist> installer.iss
; The installer is fully offline: the payload carries Python, every dependency, FFmpeg,
; the AI models and the built UI. It never downloads anything.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef PayloadDir
  #define PayloadDir "payload"
#endif
#ifndef OutputDir
  #define OutputDir "dist"
#endif

#define AppName "Forensic Workbench"
#define Python "{app}\python\python.exe"
#define Launcher "{app}\launcher\workbench_launcher.py"

[Setup]
; Keep AppId fixed so a newer installer upgrades this installation in place.
AppId={{6E0B2B7C-3A5D-4C1E-9F43-2D8A7C1B5E90}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppName}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog commandline
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
InfoBeforeFile=install-notes.txt
OutputDir={#OutputDir}
OutputBaseFilename=ForensicWorkbench-{#AppVersion}-Setup
UninstallDisplayName={#AppName} {#AppVersion}
UninstallDisplayIcon={app}\python\python.exe
Compression=lzma2/max
SolidCompression=yes
LZMANumBlockThreads=4
WizardStyle=modern
DiskSpanning=no
CloseApplications=yes
SetupLogging=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[InstallDelete]
; An upgrade replaces the runtime wholesale so no file from an older version lingers.
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\frontend"
Type: filesandordirs; Name: "{app}\ffmpeg"
Type: filesandordirs; Name: "{app}\launcher"

[Files]
Source: "{#PayloadDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Dirs]
; Case data stays when the workbench is uninstalled. A per-user install keeps the folder's
; inherited permissions, so only that user (and administrators) can reach it. An all-users
; install limits it to the local "Forensic Examiners" group (see RestrictDataDir below).
Name: "{code:DataDir}"; Flags: uninsneveruninstall
Name: "{code:DataDir}\import"; Flags: uninsneveruninstall

[INI]
Filename: "{app}\workbench.ini"; Section: "workbench"; Key: "data_dir"; String: "{code:DataDir}"; Flags: uninsdeletesection
Filename: "{app}\workbench.ini"; Section: "workbench"; Key: "import_dir"; String: "{code:DataDir}\import"
Filename: "{app}\workbench.ini"; Section: "workbench"; Key: "host"; String: "127.0.0.1"; Flags: createkeyifdoesntexist
Filename: "{app}\workbench.ini"; Section: "workbench"; Key: "port"; String: "{code:Port}"

[Icons]
Name: "{group}\{#AppName}"; Filename: "{#Python}"; Parameters: "-X utf8 ""{#Launcher}"" run"; WorkingDir: "{app}"; Comment: "Start the workbench and open it in the browser"
Name: "{group}\Self-test"; Filename: "{#Python}"; Parameters: "-X utf8 ""{#Launcher}"" selftest --pause"; WorkingDir: "{app}"; Comment: "Check the installation and run the acceptance self-test"
Name: "{group}\Case data folder"; Filename: "{code:DataDir}"
Name: "{group}\User manual"; Filename: "{app}\docs\user-manual.md"
Name: "{group}\Algorithm guide"; Filename: "{app}\docs\algorithms.md"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{#Python}"; Parameters: "-X utf8 ""{#Launcher}"" run"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\vcredist\vc_redist.x64.exe"; Parameters: "/install /quiet /norestart"; StatusMsg: "Installing the Microsoft Visual C++ runtime..."; Check: NeedsVCRedist; Flags: waituntilterminated
Filename: "{#Python}"; Parameters: "-X utf8 ""{#Launcher}"" selftest --pause"; WorkingDir: "{app}"; Description: "Run the acceptance self-test now (recommended)"; Flags: postinstall skipifsilent
Filename: "{#Python}"; Parameters: "-X utf8 ""{#Launcher}"" run"; WorkingDir: "{app}"; Description: "Start the {#AppName}"; Flags: postinstall nowait unchecked skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\launcher\__pycache__"

[Code]
var
  DataPage: TInputDirWizardPage;
  PortPage: TInputQueryWizardPage;

function DefaultDataDir(): String;
begin
  if IsAdminInstallMode then
    Result := ExpandConstant('{commondocs}\Forensic Workbench')
  else
    Result := ExpandConstant('{userdocs}\Forensic Workbench');
end;

function ParamOr(Name, Default: String): String;
var
  I: Integer;
  S: String;
begin
  Result := Default;
  for I := 1 to ParamCount do
  begin
    S := ParamStr(I);
    if CompareText(Copy(S, 1, Length(Name) + 2), '/' + Name + '=') = 0 then
      Result := Copy(S, Length(Name) + 3, MaxInt);
  end;
end;

procedure InitializeWizard;
begin
  DataPage := CreateInputDirPage(wpSelectDir,
    'Case data folder', 'Where should the workbench keep case data?',
    'Cases, the write-once evidence store, derived outputs, the audit log and reports are kept in this folder. ' +
    'Choose a drive with enough space for your evidence. It is never removed when the workbench is uninstalled.',
    False, '');
  DataPage.Add('');
  // /DATADIR= and /PORT= on the command line set these for a silent install.
  DataPage.Values[0] := ParamOr('DATADIR', GetPreviousData('DataDir', DefaultDataDir()));

  PortPage := CreateInputQueryPage(DataPage.ID,
    'Local web address', 'Which port should the workbench use on this computer?',
    'The workbench runs in your browser at http://127.0.0.1:<port>. It only accepts connections from this computer. ' +
    'Keep 8000 unless another program already uses it.');
  PortPage.Add('Port:', False);
  PortPage.Values[0] := ParamOr('PORT', GetPreviousData('Port', '8000'));
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  P: Integer;
begin
  Result := True;
  if (PortPage <> nil) and (CurPageID = PortPage.ID) then
  begin
    P := StrToIntDef(Trim(PortPage.Values[0]), 0);
    if (P < 1024) or (P > 65535) then
    begin
      MsgBox('Enter a port number between 1024 and 65535.', mbError, MB_OK);
      Result := False;
    end;
  end;
end;

procedure RegisterPreviousData(PreviousDataKey: Integer);
begin
  SetPreviousData(PreviousDataKey, 'DataDir', DataPage.Values[0]);
  SetPreviousData(PreviousDataKey, 'Port', Trim(PortPage.Values[0]));
end;

function DataDir(Param: String): String;
begin
  Result := RemoveBackslashUnlessRoot(DataPage.Values[0]);
end;

function Port(Param: String): String;
begin
  Result := Trim(PortPage.Values[0]);
end;

function UpdateReadyMemo(Space, NewLine, MemoUserInfoInfo, MemoDirInfo, MemoTypeInfo,
  MemoComponentsInfo, MemoGroupInfo, MemoTasksInfo: String): String;
begin
  Result := MemoDirInfo + NewLine + NewLine +
    'Case data folder:' + NewLine + Space + DataDir('') + NewLine + NewLine +
    'Web address:' + NewLine + Space + 'http://127.0.0.1:' + Port('') + NewLine + NewLine +
    'Included (no internet connection needed):' + NewLine +
    Space + 'Python runtime and all libraries' + NewLine +
    Space + 'FFmpeg and FFprobe' + NewLine +
    Space + 'AI models (super-resolution, face detection), hash-pinned' + NewLine +
    Space + 'Workbench user interface' + NewLine;
  if MemoTasksInfo <> '' then
    Result := Result + NewLine + MemoTasksInfo;
end;

const
  ExaminersGroup = 'Forensic Examiners';

procedure RunHidden(Exe, Params: String; MustSucceed: Boolean);
var
  Code: Integer;
begin
  if not Exec(Exe, Params, '', SW_HIDE, ewWaitUntilTerminated, Code) or (MustSucceed and (Code <> 0)) then
  begin
    Log(Format('%s %s failed with exit code %d', [Exe, Params, Code]));
    if MustSucceed then
      RaiseException('Could not set the permissions of the case data folder. See the setup log.');
  end;
end;

procedure RestrictDataDir();
var
  Sys: String;
begin
  // All-users install: case data is shared only with members of a local "Forensic Examiners"
  // group (the installing account is added), plus Administrators and SYSTEM. Other accounts on
  // the computer cannot read or change evidence, the case database or the audit log.
  Sys := ExpandConstant('{sys}');
  RunHidden(Sys + '\net.exe', 'localgroup "' + ExaminersGroup + '" /add /comment:"Can open Forensic Workbench case data"', False);
  RunHidden(Sys + '\net.exe', 'localgroup "' + ExaminersGroup + '" "' + GetUserNameString + '" /add', False);
  RunHidden(Sys + '\icacls.exe', '"' + DataDir('') + '" /inheritance:r /grant:r *S-1-5-32-544:(OI)(CI)F *S-1-5-18:(OI)(CI)F "' +
    ExaminersGroup + ':(OI)(CI)M" /T /C /Q', True);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and IsAdminInstallMode then
    RestrictDataDir();
end;

function NeedsVCRedist(): Boolean;
var
  Installed: Cardinal;
begin
  // The bundled DLLs already sit next to python.exe; the system-wide runtime is installed
  // too when Setup runs as administrator and the machine does not have it yet.
  Result := IsAdminInstallMode and FileExists(ExpandConstant('{app}\vcredist\vc_redist.x64.exe')) and
    not (RegQueryDWordValue(HKLM64, 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64', 'Installed', Installed)
         and (Installed = 1));
end;
