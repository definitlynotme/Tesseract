; A stand-in installer for testing Tesseract Setup end to end without touching a real install:
; its own AppId and name, a per-user install under %TEMP%, no elevation, a "desktop shortcut"
; inside its own folder. It carries the same progress code as the release script.
;
;   ISCC /DPayload=<folder with Tesseract.exe and filler> /DAppVersion=0.0.9 sandbox.iss
;   set TESSERACT_SETUP_NO_ELEVATE=1
;   set TESSERACT_SETUP_TEST_KEY=Software\Microsoft\Windows\CurrentVersion\Uninstall\{B6E3C1F0-5D0B-4E39-9C7E-2F1A7C3D9E01}_is1
;   target\debug\tesseract-setup.exe --payload ..\target\sandbox\sandbox-setup.exe
;
; Uninstall afterwards: %TEMP%\TesseractSetupSandbox\app\unins000.exe /VERYSILENT

#ifndef AppVersion
#define AppVersion "0.0.9"
#endif
#ifndef Payload
#error pass /DPayload=<folder>
#endif

[Setup]
AppId={{B6E3C1F0-5D0B-4E39-9C7E-2F1A7C3D9E01}
AppName=Tesseract Setup Sandbox
AppVersion={#AppVersion}
DefaultDirName={%TEMP}\TesseractSetupSandbox\app
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\target\sandbox
OutputBaseFilename=sandbox-setup
Compression=none
WizardStyle=modern
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "{#Payload}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{app}\desktop\Tesseract"; Filename: "{app}\Tesseract.exe"; Tasks: desktopicon

#include "..\..\..\packaging\windows\setup_progress.iss"

[Code]
// TESSERACT_SANDBOX_FAIL=prepare makes PrepareToInstall fail (exit code 7, nothing changed)
function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  TsProgressPreparing();
  Sleep(600);
  if GetEnv('TESSERACT_SANDBOX_FAIL') = 'prepare' then
    Result := 'sandbox: asked to fail'
  else
    Result := '';
end;
