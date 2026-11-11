; Progress for the Tesseract Setup window (shell/setup, 11-05).
;
; Included by Tesseract-shell.iss (and shell/setup/test/sandbox.iss). Inert unless Setup was
; started with /TSPROGRESS=<hwnd>: the setup window passes the handle of its message-only
; window and runs this package silently and elevated; this code posts WM_APP + $54 to it with
;   wParam = stage: 1 preparing (PrepareToInstall; Inno closes a running app here),
;                   2 installing files (lParam = permille of the file work),
;                   3 after the files (shortcuts, registry, uninstall entry);
;   lParam = permille.
; PostMessage only queues; an elevated process may post to a lower-integrity window, and a
; window that has gone away just drops it. No behaviour of the install depends on it.

[Code]
const
  TsProgressMessage = $8054;

var
  TsProgressWnd: HWND;
  TsProgressRead: Boolean;
  TsLastPermille: Integer;

procedure TsProgressPost(const Stage, Permille: Integer);
begin
  if not TsProgressRead then
  begin
    TsProgressRead := True;
    TsProgressWnd := StrToIntDef(ExpandConstant('{param:TSPROGRESS|0}'), 0);
    TsLastPermille := -1;
  end;
  if TsProgressWnd <> 0 then
    PostMessage(TsProgressWnd, TsProgressMessage, Stage, Permille);
end;

// Called by the including script's PrepareToInstall (an event function with a result, so it is
// not given a second implementation here).
procedure TsProgressPreparing();
begin
  TsProgressPost(1, 0);
end;

<event('CurStepChanged')>
procedure TsProgressStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
    TsProgressPost(2, 0)
  else if CurStep = ssPostInstall then
    TsProgressPost(3, 1000);
end;

<event('CurInstallProgressChanged')>
procedure TsProgressInstallChanged(CurProgress, MaxProgress: Integer);
var
  Permille: Integer;
begin
  if MaxProgress <= 0 then
    Exit;
  Permille := Round(CurProgress * 1000.0 / MaxProgress);
  if Permille > 1000 then
    Permille := 1000;
  // one message per permille step at most
  if Permille <> TsLastPermille then
  begin
    TsLastPermille := Permille;
    TsProgressPost(2, Permille);
  end;
end;
