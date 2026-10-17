; Inno Setup script for the Lucida Windows installer.
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

#define AppName "Lucida"
; The name before the rename to Lucida, so an upgrade can tidy its old shortcuts away.
#define OldAppName "Forensic Workbench"
#define AppIcon "{app}\lucida.ico"
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
; Use the Lucida Start menu folder even when upgrading an install made under the old name.
UsePreviousGroup=no
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog commandline
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
; Wizard pages: welcome, licence, what is included, install folder, case data folder, port,
; shortcuts and network, ready, installing, finish (with "Start Lucida now").
DisableWelcomePage=no
LicenseFile=licence.txt
InfoBeforeFile=install-notes.txt
DisableReadyPage=no
DisableFinishedPage=no
WizardImageFile=wizard\wizard-large-100.bmp,wizard\wizard-large-150.bmp,wizard\wizard-large-200.bmp
WizardSmallImageFile=wizard\wizard-small-100.bmp,wizard\wizard-small-150.bmp,wizard\wizard-small-200.bmp
OutputDir={#OutputDir}
OutputBaseFilename=Lucida-{#AppVersion}-Setup
SetupIconFile=lucida.ico
UninstallDisplayName={#AppName} {#AppVersion}
UninstallDisplayIcon={#AppIcon}
Compression=lzma2/max
SolidCompression=yes
LZMANumBlockThreads=4
WizardStyle=modern
DiskSpanning=no
CloseApplications=yes
SetupLogging=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
WelcomeLabel2=This will install [name/ver] on your computer.%n%nLucida is an offline forensic image and video workbench. Everything it needs is inside this installer: the Python runtime, FFmpeg, the AI models, the user interface and the guides. No internet connection is needed, now or later.%n%nIt is recommended that you close all other applications before continuing.
FinishedLabel=Setup has finished installing [name] on your computer. Start it from the Start menu or the desktop shortcut; it opens in your browser.

[Tasks]
Name: "startmenu"; Description: "Create Start menu shortcuts"; GroupDescription: "Shortcuts:"
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "labnetwork"; Description: "Let examiners connect from other PCs on the lab network (HTTPS, Lucida sign-in)"; GroupDescription: "Network:"; Flags: unchecked

[InstallDelete]
; An upgrade replaces the runtime wholesale so no file from an older version lingers.
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\frontend"
Type: filesandordirs; Name: "{app}\ffmpeg"
Type: filesandordirs; Name: "{app}\launcher"
; Shortcuts left by an install made under the old name.
Type: filesandordirs; Name: "{autoprograms}\{#OldAppName}"
Type: files; Name: "{autodesktop}\{#OldAppName}.lnk"

[Files]
Source: "{#PayloadDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "lucida.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "licence.txt"; DestDir: "{app}\licences"; DestName: "Lucida-licence.txt"; Flags: ignoreversion

[Dirs]
; Case data stays when Lucida is uninstalled. A per-user install keeps the folder's
; inherited permissions, so only that user (and administrators) can reach it. An all-users
; install limits it to the local "Forensic Examiners" group (see RestrictDataDir below).
Name: "{code:DataDir}"; Flags: uninsneveruninstall
Name: "{code:DataDir}\import"; Flags: uninsneveruninstall

[INI]
Filename: "{app}\workbench.ini"; Section: "workbench"; Key: "data_dir"; String: "{code:DataDir}"; Flags: uninsdeletesection
Filename: "{app}\workbench.ini"; Section: "workbench"; Key: "import_dir"; String: "{code:DataDir}\import"
Filename: "{app}\workbench.ini"; Section: "workbench"; Key: "host"; String: "127.0.0.1"; Flags: createkeyifdoesntexist
Filename: "{app}\workbench.ini"; Section: "workbench"; Key: "port"; String: "{code:Port}"
Filename: "{app}\workbench.ini"; Section: "workbench"; Key: "network"; String: "{code:NetworkMode}"

[Icons]
Name: "{group}\{#AppName}"; Filename: "{#Python}"; Parameters: "-X utf8 ""{#Launcher}"" run"; WorkingDir: "{app}"; IconFilename: "{#AppIcon}"; Comment: "Start Lucida and open it in the browser"; Tasks: startmenu
Name: "{group}\Self-test"; Filename: "{#Python}"; Parameters: "-X utf8 ""{#Launcher}"" selftest --pause"; WorkingDir: "{app}"; IconFilename: "{#AppIcon}"; Comment: "Check the installation and run the acceptance self-test"; Tasks: startmenu
Name: "{group}\Case data folder"; Filename: "{code:DataDir}"; Tasks: startmenu
Name: "{group}\User manual"; Filename: "{app}\docs\user-manual.md"; Tasks: startmenu
Name: "{group}\Algorithm guide"; Filename: "{app}\docs\algorithms.md"; Tasks: startmenu
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"; Tasks: startmenu
Name: "{autodesktop}\{#AppName}"; Filename: "{#Python}"; Parameters: "-X utf8 ""{#Launcher}"" run"; WorkingDir: "{app}"; IconFilename: "{#AppIcon}"; Comment: "Start Lucida and open it in the browser"; Tasks: desktopicon

[Run]
Filename: "{app}\vcredist\vc_redist.x64.exe"; Parameters: "/install /quiet /norestart"; StatusMsg: "Installing the Microsoft Visual C++ runtime..."; Check: NeedsVCRedist; Flags: waituntilterminated
Filename: "{#Python}"; Parameters: "-X utf8 ""{#Launcher}"" selftest --pause"; WorkingDir: "{app}"; Description: "Run the acceptance self-test now (recommended)"; Flags: postinstall skipifsilent
Filename: "{#Python}"; Parameters: "-X utf8 ""{#Launcher}"" run"; WorkingDir: "{app}"; Description: "Start {#AppName} now"; Flags: postinstall nowait unchecked skipifsilent

[UninstallRun]
; Removes the inbound rule an all-users lab-network install added (harmless when there is none).
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Lucida"""; Flags: runhidden; RunOnceId: "DelFirewallRule"; Check: IsAdminInstallMode

[UninstallDelete]
Type: filesandordirs; Name: "{app}\launcher\__pycache__"

[Code]
var
  DataPage: TInputDirWizardPage;
  PortPage: TInputQueryWizardPage;

function DefaultDataDir(): String;
begin
  if IsAdminInstallMode then
    Result := ExpandConstant('{commondocs}\Lucida')
  else
    Result := ExpandConstant('{userdocs}\Lucida');
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
    'Case data folder', 'Where should Lucida keep case data?',
    'Cases, the write-once evidence store, derived outputs, the audit log and reports are kept in this folder. ' +
    'Choose a drive with enough space for your evidence. It is kept when Lucida is uninstalled unless you choose to delete it then.',
    False, '');
  DataPage.Add('');
  // /DATADIR= and /PORT= on the command line set these for a silent install.
  DataPage.Values[0] := ParamOr('DATADIR', GetPreviousData('DataDir', DefaultDataDir()));

  PortPage := CreateInputQueryPage(DataPage.ID,
    'Local web address', 'Which port should Lucida use on this computer?',
    'Lucida runs in your browser at http://127.0.0.1:<port>. Unless you choose the lab-network option on a ' +
    'later page, it only accepts connections from this computer. Keep 8000 unless another program already uses it.');
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

function WebAddress(): String;
begin
  if WizardIsTaskSelected('labnetwork') then
    Result := 'https://' + Lowercase(GetComputerNameString) + ':' + Port('') + ' (lab network)'
  else
    Result := 'http://127.0.0.1:' + Port('') + ' (this computer only)';
end;

function NetworkMode(Param: String): String;
begin
  if WizardIsTaskSelected('labnetwork') then
    Result := 'lab'
  else
    Result := 'local';
end;

function UpdateReadyMemo(Space, NewLine, MemoUserInfoInfo, MemoDirInfo, MemoTypeInfo,
  MemoComponentsInfo, MemoGroupInfo, MemoTasksInfo: String): String;
begin
  Result := MemoDirInfo + NewLine + NewLine +
    'Case data folder:' + NewLine + Space + DataDir('') + NewLine + NewLine +
    'Web address:' + NewLine + Space + WebAddress() + NewLine + NewLine +
    'Included (no internet connection needed):' + NewLine +
    Space + 'Python runtime and all libraries' + NewLine +
    Space + 'FFmpeg and FFprobe' + NewLine +
    Space + 'AI models (super-resolution, face detection), hash-pinned' + NewLine +
    Space + 'Lucida user interface' + NewLine;
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
  RunHidden(Sys + '\net.exe', 'localgroup "' + ExaminersGroup + '" /add /comment:"Can open Lucida case data"', False);
  RunHidden(Sys + '\net.exe', 'localgroup "' + ExaminersGroup + '" "' + GetUserNameString + '" /add', False);
  RunHidden(Sys + '\icacls.exe', '"' + DataDir('') + '" /inheritance:r /grant:r *S-1-5-32-544:(OI)(CI)F *S-1-5-18:(OI)(CI)F "' +
    ExaminersGroup + ':(OI)(CI)M" /T /C /Q', True);
end;

procedure OpenFirewall();
var
  Sys: String;
begin
  // Lab-network install: let examiner PCs reach the workbench port on domain and private
  // networks (never public ones). A per-user install cannot change the firewall; Windows asks
  // the first time the workbench starts instead.
  Sys := ExpandConstant('{sys}');
  RunHidden(Sys + '\netsh.exe', 'advfirewall firewall delete rule name="Lucida"', False);
  if WizardIsTaskSelected('labnetwork') then
    RunHidden(Sys + '\netsh.exe', 'advfirewall firewall add rule name="Lucida" dir=in action=allow ' +
      'protocol=TCP localport=' + Port('') + ' profile=domain,private', False);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and IsAdminInstallMode then
  begin
    RestrictDataDir();
    OpenFirewall();
  end;
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

// ---- Uninstall: case data is kept unless the user chooses to delete it ----

var
  CaseDataDir: String;
  DeleteCaseData: Boolean;

function HasSwitch(Name: String): Boolean;
var
  I: Integer;
begin
  Result := False;
  for I := 1 to ParamCount do
    if CompareText(ParamStr(I), '/' + Name) = 0 then
      Result := True;
end;

function SameFolder(A, B: String): Boolean;
begin
  Result := CompareText(RemoveBackslashUnlessRoot(A), RemoveBackslashUnlessRoot(B)) = 0;
end;

function IsUnsafeToDelete(Dir: String): Boolean;
begin
  // Never delete a drive root or a whole user or system folder, even if it was chosen as the
  // case data folder: only a folder that belongs to Lucida alone may go.
  Result := (Length(RemoveBackslashUnlessRoot(Dir)) <= 3) or
    SameFolder(Dir, ExpandConstant('{userdocs}')) or SameFolder(Dir, ExpandConstant('{commondocs}')) or
    SameFolder(Dir, ExpandConstant('{userdesktop}')) or SameFolder(Dir, ExpandConstant('{commondesktop}')) or
    SameFolder(Dir, ExpandConstant('{%USERPROFILE}')) or SameFolder(Dir, ExpandConstant('{win}')) or
    SameFolder(Dir, ExpandConstant('{commonpf}')) or SameFolder(Dir, ExtractFileDir(ExpandConstant('{app}')));
end;

function AskDeleteCaseData(): Boolean;
begin
  Result := MsgBox('Lucida keeps your case data when it is uninstalled:' + #13#10#13#10 + '    ' + CaseDataDir + #13#10#13#10 +
    'This folder holds every case, the stored evidence, derived outputs, the audit log and reports. ' +
    'Keep it if you may need any of it again, or if you will reinstall Lucida.' + #13#10#13#10 +
    'Do you also want to permanently delete the case data folder?',
    mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES;
  if Result then
    Result := MsgBox('All cases and evidence in' + #13#10#13#10 + '    ' + CaseDataDir + #13#10#13#10 +
      'will be deleted. This cannot be undone. Delete the case data?',
      mbError, MB_YESNO or MB_DEFBUTTON2) = IDYES;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Code: Integer;
begin
  if CurUninstallStep = usUninstall then
  begin
    // Read the folder now: workbench.ini is removed with the programs.
    CaseDataDir := RemoveBackslashUnlessRoot(GetIniString('workbench', 'data_dir', '', ExpandConstant('{app}\workbench.ini')));
    DeleteCaseData := False;
    if (CaseDataDir = '') or not DirExists(CaseDataDir) then
      Exit;
    // A silent uninstall keeps case data unless /DELETECASEDATA is given.
    if UninstallSilent then
      DeleteCaseData := HasSwitch('DELETECASEDATA')
    else
      DeleteCaseData := AskDeleteCaseData();
    if DeleteCaseData and IsUnsafeToDelete(CaseDataDir) then
    begin
      Log('Not deleting case data: ' + CaseDataDir + ' is a drive root or a shared system folder');
      if not UninstallSilent then
        MsgBox(CaseDataDir + ' is not a folder Lucida can delete as a whole, so it was kept. Delete the case files in it yourself.',
          mbInformation, MB_OK);
      DeleteCaseData := False;
    end;
  end
  else if (CurUninstallStep = usPostUninstall) and DeleteCaseData then
  begin
    // Stored evidence is read-only by design; clear that so the folder can be removed.
    Exec(ExpandConstant('{sys}\attrib.exe'), '-R "' + CaseDataDir + '\*" /S /D', '', SW_HIDE, ewWaitUntilTerminated, Code);
    DelTree(CaseDataDir, True, True, True);
    if DirExists(CaseDataDir) then
    begin
      Log('Case data folder could not be removed completely: ' + CaseDataDir);
      if not UninstallSilent then
        MsgBox('Some files in ' + CaseDataDir + ' could not be deleted (they may be open in another program). Delete the folder yourself.',
          mbError, MB_OK);
    end
    else
      Log('Deleted case data folder ' + CaseDataDir);
  end;
end;
