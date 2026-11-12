# Tesseract

**English** · [简体中文](README.zh-CN.md)

Tesseract is a Windows desktop client for
[Mihomo (Clash.Meta)](https://github.com/MetaCubeX/mihomo). It puts your own
servers, paid subscriptions, a public emergency pool and an optional fixed
egress into layers, and moves traffic down a layer when the one above stops
working — then back up when it recovers.

> Tesseract is not affiliated with the Tesseract OCR project. It is a proxy
> client only: it does not provide nodes or subscriptions.

## Download

Get the latest build from
[Releases](https://github.com/definitlynotme/Tesseract/releases):

| File | What it is |
|---|---|
| `Tesseract-Setup-<version>-win-x64.exe` | Installer (recommended). Installs to `Program Files`, adds a Start menu entry, and installs the Microsoft Edge WebView2 Runtime if it is missing. |
| `Tesseract-<version>-win-x64-portable.zip` | Portable build. Unzip anywhere and run `Tesseract.exe`. Needs the WebView2 Runtime (preinstalled on current Windows 10/11). |
| `SHA256SUMS.txt` + `SHA256SUMS.txt.sig` | Checksums of every asset and their Ed25519 signature. |
| `sbom.cdx.json`, `Tesseract-<version>-third-party-sources.zip` | Software bill of materials and the third-party sources the licenses require us to publish. |

Requirements: Windows 10 or 11, 64-bit. The builds are not Authenticode
signed, so Windows SmartScreen may ask for confirmation on first launch;
compare the file's SHA-256 with `SHA256SUMS.txt` if in doubt.

## Getting started

1. On the **Tiers** page, choose **Import** and paste node links, a
   subscription URL or text exported from another client.
2. Go back to **Overview** and press **Start service**.
3. When the status shows **Running**, you are connected. Closing the window
   keeps Tesseract running in the notification area.

The first time you open the app, a short letter explains the basics; click the
Tesseract name at the top left to read it again.

## Tiers

| Tier | Put here | Behaviour |
|---|---|---|
| **Vanguard** | Your own servers (VPS, self-hosted) | Used first. One server may offer several protocols with a fallback order. |
| **Sentinel** | Subscription nodes | Takes over when no Vanguard node works. |
| **Bastion** | Public nodes collected from the internet (the automatic pool is called **Garrison**) | Last resort, off by default. Operators are unknown: never sign in or type passwords while traffic goes through Bastion. |
| **Gateway** | A fixed egress | Optional. Makes websites always see the same address; the active layer connects to the Gateway, which connects to the destination. |

The tiers are a priority order, not a fixed multi-hop chain. A node that
offers several protocols counts as one node everywhere.

## Taking over traffic

Settings has two independent switches:

- **System proxy** — enough most of the time: browsers and most applications
  follow the Windows proxy setting.
- **TUN mode** — for programs that ignore the system proxy (some games and
  command-line tools). All traffic of the computer goes through Tesseract.
  Requires starting Tesseract as administrator.

If another proxy program rewrites the system proxy, Overview tells you; close
that program and press **Restart service**.

**Kill switch**: when every tier is down, Windows Firewall rules hold traffic
back so your real IP is not exposed. Its exceptions are configurable in
Settings. Leave it on unless you have a reason not to.

## Routing, DNS and other settings

- **Proxy routing rules** — one ordered list of direct / proxy / reject rules
  (domains, IPs, `geosite:` / `geoip:` sets and rule sets). What the list shows
  is exactly what runs: nothing is appended behind it. New configurations start
  with two regional defaults (`geosite:CN`, `geoip:CN` → direct) that you can
  delete or replace.
- **DNS resolution**, **latency limit**, **Bastion settings**, **alerts and
  notifications**, **update check**.
- Interface languages: English, 简体中文, Deutsch, Español, Français, 日本語,
  한국어, Português (Brasil), Русский.

Supported node protocols include VLESS, VMess, Trojan, Shadowsocks, SSR,
SOCKS5, Hysteria2, TUIC, AnyTLS, Naive (HTTPS) and WireGuard; the available fields
depend on the protocol and the import format.

## Your data

| What | Where |
|---|---|
| Configuration (`config.yaml`, `config.local.yaml`, `sources.yaml`) | `%APPDATA%\Tesseract` |
| Logs, caches, runtime data | `%LOCALAPPDATA%\Tesseract` |

Node links, subscription URLs and exported configurations contain access
credentials — treat them like passwords. A diagnostic bundle (exported from
**Logs**) contains the addresses and IPs you visited; review it before sending
it to anyone. Uninstalling keeps both folders; delete them by hand to remove
everything. Stop the service from the app (not by killing the process) so the
system proxy and firewall rules are restored.

## Building from source

Tesseract has three parts:

| Directory | Part |
|---|---|
| `shell/` | Tauri 2 shell (Rust, WebView2): window, tray, single instance, notifications. Builds `Tesseract.exe`. |
| `ui/` | Web interface (plain JavaScript, bundled with esbuild). |
| repository root | Python sidecar (`runtime_main.pyw`, `bridge_*.py`, `desktop_runtime.py`, `manager.py`, `tier3_*.py`, …) that runs Mihomo, health checks, routing and the layer logic. Frozen with PyInstaller into `tesseract-runtime.exe`. |

Toolchain (Windows x64): Python 3.12.7, Node.js 24, Rust 1.96 (MSVC),
Inno Setup 6.7, and the license tools `cargo-about 0.9.3` (`--features cli`)
and `cargo-deny 0.20.2`.

```powershell
# Full local release: locked dependencies, tests, license/SBOM gate, shell +
# UI + sidecar build, smoke tests, portable zip, installer and checksums.
# Output goes to release\; the candidate tree is dist-release\Tesseract.
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1 -SkipInstallerAcceptance
```

Run it from an elevated shell and drop `-SkipInstallerAcceptance` to also
install the installer into an isolated directory, smoke-test it and uninstall
it again. `-SkipTests`, `-SkipPackagedSmoke` and `-SkipInstaller` make faster,
partial builds. The Python tests run with `python -m pytest tests`; the UI unit
tests with `npm --prefix ui test`. See `shell/README.md` for development builds
of the shell.

Official releases are built by `.github/workflows/release-windows.yml` when a
signed `vX.Y.Z` tag is pushed: the build runs unsigned on GitHub Actions, a
protected environment signs `SHA256SUMS.txt` with the update key, and the
release is published with build provenance attestations. The in-app updater
only accepts releases whose manifest verifies against the public keys built
into the app.

## License

Tesseract is licensed under the [GNU General Public License v3.0 only](LICENSE).
Third-party components and their licenses are listed in
[THIRD_PARTY_NOTICES.shell.md](THIRD_PARTY_NOTICES.shell.md); every release
ships the full license texts and an SBOM.
