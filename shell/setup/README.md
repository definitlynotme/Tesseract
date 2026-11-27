# Tesseract Setup

The installer's own window. A small
WebView2 program around the Inno Setup package (`packaging/windows/Tesseract-shell.iss`):
it embeds that package, runs it silently and elevated, and shows its progress. Installing itself
(rollback, file replacement, shortcuts, AUMID, the uninstall entry, the WebView2 check) stays
Inno's, unchanged.

- **Hand-over**: any command-line switch (`/VERYSILENT`, `/DIR=…`) or a machine without the
  WebView2 runtime runs the embedded Inno package as it is, with its own wizard or silently, and
  returns its exit code. The in-app updater's run is the one exception (next item).
- **The in-app update** (`UpdateRun` in `src/main.rs`, 0.1.9): the updater's run (`/SILENT …
  /RESTARTAPPLICATIONS`, started unelevated) opens this window, in the app's look, on its
  progress: no first screen and nothing to click (the user already chose 更新 in the app). The
  window comes to the foreground and starts the update once it shows, so it owns the elevation
  prompt (the app that started it had the foreground and calls `AllowSetForegroundWindow` for it);
  while the prompt waits the page says what to do in User Account Control. Everything else is the
  hand-over's, unchanged: the full wait for the quitting shell, the same switches (only `/SILENT`
  becomes `/VERYSILENT`, plus `/TSPROGRESS`, so Inno shows no window of its own), the package
  elevating itself (so its `[Run]` entry starts the new version with the updater's token), the
  `Tesseract-Update-*.log`, the reopen rules, and the exit code. Done: 已更新到 x.y.z · 正在打开
  Tesseract…, and the window closes itself 2.4 s later. Not done (declined, failed): the reason,
  重试 (the package again; Tesseract stays closed meanwhile) and 稍后 / × (the window closes and
  opens the version installed now). Before 0.1.9 this run showed `src/handover.rs`'s plain
  window for the elevation (0.1.6; with no window at all Windows showed the request only as a
  flashing taskbar button, field report 11-22) and then Inno's own `/SILENT` progress window, both
  in the system's look. handover.rs stays for an unelevated `/VERYSILENT /RESTARTAPPLICATIONS` run
  and for when this window cannot be made (the update then runs as before 0.1.9). Deployments
  (`/VERYSILENT`, an elevated console) show nothing.
- **The command line** (`sys::params`, 11-19) is split the way Inno splits it: a double quote
  toggles quoting and a backslash is literal. So `/DIR="C:\Program Files\Tesseract\"
  /VERYSILENT` means here what it means to Inno, where the C runtime's rules would read `\"` as a
  quote and swallow `/VERYSILENT`. A line that escapes a quote the C runtime's way (`\"` before a
  non-blank: PowerShell 7.3+, .NET's ArgumentList, Python's subprocess) is split by those rules,
  as 0.1.0–0.1.3 split every line. Each switch is passed on unchanged.
- **A running Tesseract** (`src/rm.rs`, 11-19): closed before the Inno package runs, by the
  window's own install and by a hand-over that installs silently (`/SILENT` or `/VERYSILENT`,
  without `/?` or `/HELP`, and without `/NOCLOSEAPPLICATIONS` unless `/CLOSEAPPLICATIONS`
  overrides it, as in Inno). A wizard keeps Inno's own Preparing page. The folder is `/DIR=` when
  given, else the existing install; a `/DIR=expand:…` (Inno's constants, only Inno can expand
  them) also means the existing install. Inno's own Restart Manager pass cannot close it: the
  windowless sidecar is `RmUnknownApp`, so Inno aborts and rolls back (exit 5). Restart Manager
  is given this session's shell alone, by its pid and start time (`RM_UNIQUE_PROCESS`; 0.1.4
  registered the file `<dir>\Tesseract.exe`, which made `RmShutdown` act on everything that had it
  open), and asks it politely (WM_QUERYENDSESSION /
  WM_ENDSESSION with ENDSESSION_CLOSEAPP: the sidecar restores the proxy and firewall and exits).
  The window then waits until no `.exe`/`.dll` under `<dir>` is in use. Never forced, and never cut short: the sidecar leaves a moment after the
  shell, and Inno must not start before it has. When Restart Manager cannot ask the shell at all
  (it runs elevated and this window does not, it is already leaving and has no window, or no
  shell of this session uses the folder, only another session's copy or what a gone shell left),
  the wait is 5 s (`rm::UNASKED_WAIT`), except for a
  shell that is already quitting (the default scope's `Local\TesseractShellQuitting` is set) and
  the in-app updater's run, which wait the full 30 s. If something still uses the folder, the
  window's own install installs nothing and fails with `running` (the shell itself is still here:
  quit it from the tray) or `busy` (it went, but another program keeps one of the folder's modules
  loaded: the page names the program). The wait counts what Restart Manager lists for those
  files: loaded modules, running images and plain open handles (a scanner, Explorer showing the
  folder), and it reports no session for the windowless sidecar (asked with ProcessIdToSessionId
  then).
- **How the shell takes that close** (`shell/app/src/endsession.rs`, 11-20): from 0.1.4 the shell
  quits as it does from the tray, minus the unsaved-changes question: `app.quit`, the sidecar's
  full exit recovery, the 20 s stall prompt, the bounded wait for the WebView2 browser to exit
  (so the reopened app finds the user data folder free), then a normal exit. Restart Manager
  sends every top-level window WM_QUERYENDSESSION, WM_ENDSESSION and then WM_CLOSE, and
  `RmShutdown` returns once the process has exited, or after 30 s with `ERROR_FAIL_SHUTDOWN`
  (it never kills; seen 11-20). The window's own 30 s wait starts after that. Shells 0.1.0–0.1.3
  take it as a shutdown (`app.sessionEnd`, 4 s budget, no browser wait) and then die in tao's
  panic (`panic_runner.rs_371` in shell.log); this window handles both the same way.
- **Never left without the app** (11-19): the Tesseract this session was running before the
  window closed it opens again after the window's own install, good or not, and after a silent
  install that did not finish, or a good one unless `/NORESTARTAPPLICATIONS` (Inno's own
  `RestartApplications=yes`). One that was not running, or that the user was already quitting,
  is not started. The in-app updater's run (`/RESTARTAPPLICATIONS`) quit the app itself and
  started this window with the app's token (and in the app's job, if any), so after it the
  hand-over opens it with that token whenever it is not running (the `[Run]` relaunch after a
  good install is). That holds only when this window found no running copy to close (0.1.5,
  `after_hand_over`): `/RESTARTAPPLICATIONS` is also Inno's documented switch, and a deployment
  from an elevated console or another account that closes the user's copy opens it again as it
  ran, like any other copy this window closed. When this window's token is not that copy's own,
  Inno runs without `/RESTARTAPPLICATIONS`, so the `[Run]` entry cannot start it with this
  window's token (its runasoriginaluser does nothing for a window that is already elevated).
  Running means a process of this session whose image is
  `<dir>\Tesseract.exe`: Restart Manager also lists other users' copies. Never in a service's
  session (0). What the window closed starts (`routes` in `main.rs`) with this window's token only
  when that is the copy's own (same account, same elevation), and then never inside a launcher's
  job, which would wait for the app (Start-Process -Wait) or end it with the job and skip its
  network restore: outside it with `CREATE_BREAKAWAY_FROM_JOB`. Otherwise, or when the job does
  not allow breaking away, it starts through Explorer (`IShellDispatch2::ShellExecute`, at most
  10 s) as the signed-in user, which fits a copy that ran unelevated (and any copy where UAC is
  off). A copy that ran elevated, closed by SYSTEM or another account, stays closed: never
  started as the wrong account or unelevated. 打开 on the done page also starts through Explorer
  from an elevated window, on a thread of its own; when Explorer cannot be asked or does not
  answer within 10 s, nothing is started (never an elevated copy from here) and the page says to
  open Tesseract from the Start menu (0.1.5).
- **Logs**: the window's own install and the in-app updater's run (unless it passes `/LOG`) log to
  `%TEMP%\Tesseract-Setup-<unix seconds>.log` and `%TEMP%\Tesseract-Update-<unix seconds>.log`,
  when %TEMP% is on a local fixed disk: the elevated Inno does not see this logon's network,
  SUBST or mapped drives, and stops when it cannot create its log.
- **Progress**: the window passes `/TSPROGRESS=<hwnd>` of a message-only window;
  `packaging/windows/setup_progress.iss` posts stage and permille to it.
- **Elevation**: only the Inno package is elevated (`runas`); this window runs as the user, so
  the app it opens afterwards does too.
- **Page**: `ui/` (index.html, setup.css, setup.js, strings.js in the app's nine languages),
  with `ui/css/tokens.css`, `ui/css/fonts.css` and the fonts served from the binary.
  Opened in a plain browser, `setup.js` plays a stand-in host
  (`?scene=fresh|update|reinstall&running=1&fail=4|disk|decline&lang=en_US`; the in-app update's
  run: `?run=update&fail=4|decline`).

## Design

The app's own language (`ui/DESIGN.md`): one light theme, the app's tokens and fonts, the
「豁然」 mark. One window (560 × 380, no system frame), one action, no "Next" pages: the first
screen installs, with the location and a desktop-shortcut choice as the only options.

- **The button is the progress** (DESIGN §1-4, the run pill): 安装 turns into its own track in
  one frame, fills from the left, never drains, shows ✓ 已安装 and comes back as 打开.
- **The mark tells the launch story** (DESIGN §7-11): the strokes draw when the window opens;
  the light is unlit before installing, only it breathes while installing, and it lands when
  done. 打开 hands over to the app, whose launch screen starts from the same mark.
- **Scenes**: first install, update (`0.0.9 → 0.1.0`), reinstall, app running (said once, at the
  action), failed (the problem, the fix, and whether anything changed), elevation declined, and
  the in-app update's run (opens on the progress, lands, closes itself).
- **Licence**: GPL-3.0 needs no acceptance to install or run, so there is no accept page; a
  link opens the text in a sheet inside the window.
- Not here on purpose: welcome page, illustrations, tips while installing, a close button
  while files are moving. Uninstalling still uses Inno's own dialog.

## Build

```
set TESSERACT_SETUP_PAYLOAD=<path>\Tesseract-Setup-<v>-win-x64.exe   (the Inno package)
set TESSERACT_SETUP_INSTALLED_BYTES=<size of dist-shell\Tesseract>   (free-space check; optional)
cargo build --release --locked --offline
```

`target\release\tesseract-setup.exe` is then the file users download (about 1.1 MB on top of the
package). Its own workspace and Cargo.lock (a pruned copy of `shell/Cargo.lock`, same versions).

`cargo test --offline --locked` and `cargo clippy --offline --locked --all-targets -- -D warnings`
run for every pull request (`.github/workflows/ci.yml`). `explorer_starts_a_program_as_the_desktop_user`
returns early where no desktop Explorer owns a shell window; the CI log says whether the runner had one.

## Test without touching a real install

`test/sandbox.iss` is a stand-in package (own AppId, per-user, under %TEMP%, same progress code);
`test/drive.mjs` drives the window over CDP. Development builds read two overrides:
`TESSERACT_SETUP_NO_ELEVATE=1` and `TESSERACT_SETUP_TEST_KEY` (see sandbox.iss); release builds
ignore them.

```
ISCC /DPayload=<folder with Tesseract.exe> /DAppVersion=0.0.9 test\sandbox.iss
set WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=9333
target\debug\tesseract-setup.exe --payload target\sandbox\sandbox-setup.exe
node test\drive.mjs 9333 target\e2e\fresh install
```

The in-app update's run: install the sandbox at an older version first, then pass the updater's
switches and drive it with `update` (nothing to click; it closes itself), `update-later` or
`update-retry` (`TESSERACT_SANDBOX_FAIL=prepare` makes every attempt stop with exit code 7):

```
target\debug\tesseract-setup.exe --payload target\sandbox\sandbox-setup.exe /SILENT /SUPPRESSMSGBOXES /CLOSEAPPLICATIONS /RESTARTAPPLICATIONS /NORESTART
node test\drive.mjs 9333 target\e2e\update update
```

Not covered by the sandbox: the elevation prompt and a post from an elevated process to this
window (the sandbox runs unelevated) — check once with the real package on a standard account.
