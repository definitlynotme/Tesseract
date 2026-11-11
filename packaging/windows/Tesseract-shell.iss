; Tesseract installer (Tauri shell + web UI + Python sidecar).
;
; Built by scripts\build_windows.ps1 from dist-release\Tesseract. The retired Qt
; installer (Tesseract.iss) is gone; this script keeps its identity (AppId,
; {autopf}\Tesseract, admin install, asset name, version-info layout), so it
; upgrades a Qt-era install in place, and adds (plan section 9.6):
;   1. WebView2 detection and bootstrap in PrepareToInstall, before any file changes;
;   2. [InstallDelete] of the Qt-only files an in-place upgrade would leave behind;
;   3. AppUserModelID on the Start menu (and desktop) shortcut;
;   4. relaunch after a silent in-app update, as the original user.
; Uninstall deliberately has no [UninstallDelete]: user data and
; %LOCALAPPDATA%\Tesseract\WebView2* are kept, as in the Qt flavor.

#define MyAppName "Tesseract"
#define MyAppPublisher "Tesseract contributors"
#define MyAppURL "https://github.com/definitlynotme/Tessract"
#ifndef AppVersion
#define AppVersion "0.1.0"
#endif
#ifndef AppVersionNumeric
#define AppVersionNumeric "0.1.0.65535"
#endif
#ifndef SourceDir
#define SourceDir "..\..\dist-release\Tesseract"
#endif
#ifndef OutputDir
#define OutputDir "..\..\release"
#endif

; --- WebView2 Evergreen Bootstrapper -------------------------------------
; Not committed. webview2\fetch_webview2_bootstrapper.ps1 downloads it from the
; Microsoft fwlink in webview2\webview2-bootstrapper.manifest.json and verifies
; the hash and the Microsoft Authenticode signer. The pin below must equal the
; manifest's sha256 (the fetch script checks this) and cannot be overridden
; from the command line; only the file location can.
#ifndef WebView2Bootstrapper
#define WebView2Bootstrapper "webview2\cache\MicrosoftEdgeWebview2Setup.exe"
#endif
#define WebView2BootstrapperName "MicrosoftEdgeWebview2Setup.exe"
#define WebView2BootstrapperSha256 "ca4c9016c70e2184c5e231662e0e98abf4826647180ef3faab32fcb1ca2ec15e"

#if Copy(WebView2Bootstrapper, 2, 1) == ":" || Copy(WebView2Bootstrapper, 1, 2) == "\\"
#define WebView2BootstrapperFile WebView2Bootstrapper
#else
#define WebView2BootstrapperFile AddBackslash(SourcePath) + WebView2Bootstrapper
#endif
#if !FileExists(WebView2BootstrapperFile)
#error WebView2 bootstrapper missing: run packaging\windows\webview2\fetch_webview2_bootstrapper.ps1
#endif
#if ExtractFileName(WebView2BootstrapperFile) != WebView2BootstrapperName
#error WebView2 bootstrapper must be named MicrosoftEdgeWebview2Setup.exe
#endif
#if LowerCase(GetSHA256OfFile(WebView2BootstrapperFile)) != WebView2BootstrapperSha256
#error WebView2 bootstrapper SHA-256 does not match the pinned value
#endif

; The manifest's "sha256" line must carry the same pin, so the compile itself
; fails on manifest/.iss drift. (Signer verification needs the fetch script,
; which a release build runs first, without -Force.)
#define WebView2Manifest AddBackslash(SourcePath) + "webview2\webview2-bootstrapper.manifest.json"
#if !FileExists(WebView2Manifest)
#error WebView2 manifest missing: packaging\windows\webview2\webview2-bootstrapper.manifest.json
#endif
#define WebView2ManifestShaLines 0
#define WebView2ManifestPinLines 0
#define WebView2ManifestLine ""
#define WebView2ManifestHandle FileOpen(WebView2Manifest)
#if !WebView2ManifestHandle
#error WebView2 manifest cannot be opened
#endif
; Inside #sub a #define would be local to the sub; #expr assigns the globals.
#sub ScanWebView2ManifestLine
  #expr WebView2ManifestLine = LowerCase(FileRead(WebView2ManifestHandle))
  #if Pos('"sha256"', WebView2ManifestLine) > 0
    #expr WebView2ManifestShaLines = WebView2ManifestShaLines + 1
    #if Pos('"' + WebView2BootstrapperSha256 + '"', WebView2ManifestLine) > 0
      #expr WebView2ManifestPinLines = WebView2ManifestPinLines + 1
    #endif
  #endif
#endsub
#for {0; !FileEof(WebView2ManifestHandle); 0} ScanWebView2ManifestLine
#expr FileClose(WebView2ManifestHandle)
#if WebView2ManifestShaLines != 1 || WebView2ManifestPinLines != 1
#error WebView2 manifest sha256 does not equal WebView2BootstrapperSha256 in this script
#endif

; --- Flavor guard ---------------------------------------------------------
; Refuse a Qt dist: the Shell installer must carry the sidecar and no PySide6.
#if Copy(SourceDir, 2, 1) == ":" || Copy(SourceDir, 1, 2) == "\\"
#define SourceDirFull SourceDir
#else
#define SourceDirFull AddBackslash(SourcePath) + SourceDir
#endif
#if !FileExists(AddBackslash(SourceDirFull) + "Tesseract.exe")
#error SourceDir has no Tesseract.exe (Shell dist expected)
#endif
#if !FileExists(AddBackslash(SourceDirFull) + "tesseract-runtime.exe")
#error SourceDir has no tesseract-runtime.exe: this is not a Shell-flavor dist
#endif
#if DirExists(AddBackslash(SourceDirFull) + "_internal\PySide6")
#error SourceDir contains _internal\PySide6: this is a Qt-flavor dist
#endif

[Setup]
AppId={{7D0F8A35-2A48-44A8-A75A-1A2F8631BA72}
AppName={#MyAppName}
AppVersion={#AppVersion}
AppVerName={#MyAppName} {#AppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}/releases
AppContact={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir={#OutputDir}
OutputBaseFilename=Tesseract-Setup-{#AppVersion}-win-x64
SetupIconFile=..\..\static\app_icon.ico
LicenseFile=..\..\LICENSE
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
UninstallDisplayIcon={app}\Tesseract.exe
UninstallDisplayName={#MyAppName}
VersionInfoVersion={#AppVersionNumeric}
VersionInfoTextVersion={#AppVersion}
VersionInfoProductVersion={#AppVersionNumeric}
VersionInfoProductTextVersion={#AppVersion}
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppName} Setup
VersionInfoProductName={#MyAppName}
VersionInfoCopyright=Copyright (C) 2026 {#MyAppPublisher}
; CloseApplications / CloseApplicationsFilter are left at Inno's defaults
; (yes; *.exe,*.dll,*.chm), exactly as in the Qt flavor (plan 9.6 item 5).

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
english.WebView2Installing=Installing Microsoft Edge WebView2 Runtime
english.WebView2InstallingDetail=Tesseract needs the WebView2 Runtime. Downloading and installing it from Microsoft; this may take a few minutes.
english.WebView2Failed=Microsoft Edge WebView2 Runtime is required but could not be installed (%1). No files were changed. Connect to the internet (or install the WebView2 Runtime from Microsoft) and run Setup again.

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[InstallDelete]
; Qt-flavor files that [Files] would otherwise leave behind on an in-place
; upgrade (about 55 MB). Listed from the last Qt dist (dist\Tesseract): the two
; Qt package trees plus the VC runtime overrides that Tesseract.spec places in
; the _internal root for PySide6. The sidecar ships its own vcruntime140*.dll,
; which [Files] overwrites, so those are not listed.
Type: filesandordirs; Name: "{app}\_internal\PySide6"
Type: filesandordirs; Name: "{app}\_internal\shiboken6"
Type: filesandordirs; Name: "{app}\_internal\concrt140.dll"
Type: filesandordirs; Name: "{app}\_internal\msvcp140.dll"
Type: filesandordirs; Name: "{app}\_internal\msvcp140_1.dll"
Type: filesandordirs; Name: "{app}\_internal\msvcp140_2.dll"
Type: filesandordirs; Name: "{app}\_internal\msvcp140_codecvt_ids.dll"
; The license tree is reinstalled whole by [Files], so texts of components the
; Shell flavor no longer ships (pyside6-*, shiboken6-*) do not survive an
; upgrade and the tree always matches the bundled sbom.cdx.json.
Type: filesandordirs; Name: "{app}\_internal\third-party-licenses"

[Files]
; Prerequisite first: extracted to {tmp} on demand, never installed into {app}.
Source: "{#WebView2BootstrapperFile}"; Flags: dontcopy noencryption
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; Shortcuts are created with the friendly name (no ".exe"); they target the
; PE binary which Windows requires to keep its .exe extension. The Start menu
; shortcut carries the shell's AUMID for WinRT notifications (G58). The desktop
; shortcut carries the same AUMID so both launch paths group on the taskbar
; with the process AUMID the shell sets (webview.rs).
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\Tesseract.exe"; WorkingDir: "{app}"; IconFilename: "{app}\Tesseract.exe"; Comment: "Launch {#MyAppName}"; AppUserModelID: "Tesseract.ProxyApp"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\Tesseract.exe"; WorkingDir: "{app}"; IconFilename: "{app}\Tesseract.exe"; Comment: "Launch {#MyAppName}"; Tasks: desktopicon; AppUserModelID: "Tesseract.ProxyApp"

[Run]
; Interactive install: optional launch checkbox on the last page.
Filename: "{app}\Tesseract.exe"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent
; Silent in-app update (app_updater passes /RESTARTAPPLICATIONS): reopen the app
; as the original (non-elevated) user. Plain /VERYSILENT deployments and the
; isolated install check do not pass the switch, so they do not start the app.
Filename: "{app}\Tesseract.exe"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall runasoriginaluser; Check: WizardSilent and CmdLineParamExists('/RESTARTAPPLICATIONS')

; Progress for the Tesseract Setup window (shell/setup), which embeds this package and runs it
; silently: inert unless Setup was started with /TSPROGRESS=<hwnd>.
#include "setup_progress.iss"

[Code]
const
  WebView2ClientsKey = 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  WebView2BootstrapperName = '{#WebView2BootstrapperName}';
  WebView2BootstrapperSha256 = '{#WebView2BootstrapperSha256}';
  WebView2BootstrapperArgs = '/silent /install';
  // Held by both the Qt app and the shell for the default instance scope.
  AppSingleInstanceMutex = 'Local\TesseractProxyManagerSingleInstance';
  PreviousAppExitWaitMs = 30000;

// Inno has no built-in for this; exact, case-insensitive match of one switch
// (app_updater passes /RESTARTAPPLICATIONS as a separate argument).
function CmdLineParamExists(const Value: String): Boolean;
var
  I: Integer;
begin
  Result := False;
  for I := 1 to ParamCount do
    if CompareText(ParamStr(I), Value) = 0 then
    begin
      Result := True;
      Exit;
    end;
end;

function WebView2VersionPresent(const RootKey: Integer; const SubKey: String): Boolean;
var
  Version: String;
begin
  Result := False;
  if RegQueryStringValue(RootKey, SubKey, 'pv', Version) then
  begin
    Version := Trim(Version);
    Result := (Version <> '') and (Version <> '0.0.0.0');
  end;
end;

// Microsoft's documented check: "pv" under the EdgeUpdate client key, per-machine
// in the 32-bit view (HKLM\SOFTWARE\WOW6432Node\...) or per-user in HKCU. The
// 64-bit HKLM view is also accepted; it only avoids a needless bootstrap.
function IsWebView2Installed(): Boolean;
begin
  Result := WebView2VersionPresent(HKLM32, WebView2ClientsKey)
    or WebView2VersionPresent(HKCU, WebView2ClientsKey)
    or WebView2VersionPresent(HKLM64, WebView2ClientsKey);
end;

// Extracts the embedded bootstrapper to {tmp}, re-checks its hash and runs it
// elevated (per-machine install). Success means the runtime is detectable
// afterwards, not merely a zero exit code.
function BootstrapWebView2(var Detail: String): Boolean;
var
  Path, Actual: String;
  ResultCode: Integer;
  Started: Boolean;
  Page: TOutputMarqueeProgressWizardPage;
begin
  Result := False;
  Path := ExpandConstant('{tmp}\') + WebView2BootstrapperName;
  try
    ExtractTemporaryFile(WebView2BootstrapperName);
    Actual := GetSHA256OfFile(Path);
  except
    Detail := 'extract: ' + GetExceptionMessage;
    Exit;
  end;
  if CompareText(Actual, WebView2BootstrapperSha256) <> 0 then
  begin
    Detail := 'bootstrapper hash mismatch';
    Exit;
  end;

  Page := CreateOutputMarqueeProgressPage(CustomMessage('WebView2Installing'), CustomMessage('WebView2InstallingDetail'));
  Started := False;
  Page.Show;
  try
    Page.Animate;
    Log('Running WebView2 bootstrapper: ' + Path + ' ' + WebView2BootstrapperArgs);
    Started := Exec(Path, WebView2BootstrapperArgs, ExpandConstant('{tmp}'), SW_HIDE, ewWaitUntilTerminated, ResultCode);
  finally
    Page.Hide;
  end;
  if not Started then
  begin
    Detail := 'start: ' + SysErrorMessage(ResultCode);
    Exit;
  end;
  Log(Format('WebView2 bootstrapper exit code %d', [ResultCode]));
  if ResultCode <> 0 then
  begin
    Detail := Format('bootstrapper exit code %d', [ResultCode]);
    Exit;
  end;
  if not IsWebView2Installed() then
  begin
    Detail := 'runtime not detected after bootstrap';
    Exit;
  end;
  Result := True;
end;

// Updater path only: the old app (Qt or shell) launched this installer and is
// exiting on its own. Wait (up to 30 s) for it to release the single-instance
// mutex, then reopen it as the original user so an aborted update leaves the user where
// they were. Nothing under {app} has been touched at this point.
procedure ReopenPreviousVersion();
var
  Exe: String;
  Waited, ResultCode: Integer;
begin
  Exe := ExpandConstant('{app}\Tesseract.exe');
  if not FileExists(Exe) then
  begin
    Log('No previous Tesseract.exe to reopen: ' + Exe);
    Exit;
  end;
  Waited := 0;
  while CheckForMutexes(AppSingleInstanceMutex) and (Waited < PreviousAppExitWaitMs) do
  begin
    Sleep(250);
    Waited := Waited + 250;
  end;
  if CheckForMutexes(AppSingleInstanceMutex) then
  begin
    // A shell install ({app}\tesseract-runtime.exe present) may still be
    // draining its sidecar. The shell's own single-instance logic waits for a
    // quitting shell and for runtime cleanup (instance_waiting_for_*), and
    // only wakes the old window if it is genuinely running, so hand off to it.
    // The Qt app has no such wait: a second Qt start would just wake the old
    // window and exit, so a still-running Qt is left alone.
    if not FileExists(ExpandConstant('{app}\tesseract-runtime.exe')) then
    begin
      Log('Previous Tesseract (Qt) is still running; not reopening it.');
      Exit;
    end;
    Log('Previous Tesseract (shell) still holds the instance mutex; reopening anyway, the shell waits for its cleanup.');
  end;
  if ExecAsOriginalUser(Exe, '', ExpandConstant('{app}'), SW_SHOWNORMAL, ewNoWait, ResultCode) then
    Log('Reopened previous Tesseract after WebView2 bootstrap failure.')
  else
    Log('Could not reopen previous Tesseract: ' + SysErrorMessage(ResultCode));
end;

// Runs before [InstallDelete] and [Files]: returning a non-empty string aborts
// Setup (exit code 7) with no change to {app}.
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Detail: String;
begin
  Result := '';
  TsProgressPreparing();
  if IsWebView2Installed() then
  begin
    Log('WebView2 Runtime detected.');
    Exit;
  end;
  Log('WebView2 Runtime not detected; bootstrapping.');
  if BootstrapWebView2(Detail) then
  begin
    Log('WebView2 Runtime installed.');
    Exit;
  end;
  Log('WebView2 bootstrap failed: ' + Detail);
  if CmdLineParamExists('/RESTARTAPPLICATIONS') then
    ReopenPreviousVersion();
  Result := FmtMessage(CustomMessage('WebView2Failed'), [Detail]);
end;
