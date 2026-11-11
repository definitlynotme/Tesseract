# Tesseract Setup

The installer's own window. A small
WebView2 program around the Inno Setup package (`packaging/windows/Tesseract-shell.iss`):
it embeds that package, runs it silently and elevated, and shows its progress. Installing itself
(rollback, file replacement, shortcuts, AUMID, the uninstall entry, the WebView2 check) stays
Inno's, unchanged.

- **Hand-over**: any command-line switch (`/VERYSILENT`, `/DIR=…`, the in-app updater's
  `/SILENT …`) or a machine without the WebView2 runtime runs the embedded Inno package as it is,
  with its own wizard or silently, and returns its exit code.
- **Progress**: the window passes `/TSPROGRESS=<hwnd>` of a message-only window;
  `packaging/windows/setup_progress.iss` posts stage and permille to it.
- **Elevation**: only the Inno package is elevated (`runas`); this window runs as the user, so
  the app it opens afterwards does too.
- **Page**: `ui/` (index.html, setup.css, setup.js, strings.js in the app's nine languages),
  with `ui/css/tokens.css`, `ui/css/fonts.css` and the fonts served from the binary.
  Opened in a plain browser, `setup.js` plays a stand-in host
  (`?scene=fresh|update|reinstall&running=1&fail=4|disk|decline&lang=en_US`).

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
  action), failed (the problem, the fix, and whether anything changed), elevation declined.
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

Not covered by the sandbox: the elevation prompt and a post from an elevated process to this
window (the sandbox runs unelevated) — check once with the real package on a standard account.
