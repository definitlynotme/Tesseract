# Third-Party Source and Binary Mapping

This document identifies the source materials and corresponding binaries used
in the Tesseract Windows package. The per-file software inventory is provided
separately in `sbom.cdx.json`.

## Mihomo 1.19.27

- Source: upstream tag `v1.19.27`, included as
  `sources/mihomo-1.19.27/mihomo-v1.19.27-source.zip`.
- Source archive SHA-256:
  `1ceb58df46e52900f9f4c9bcb4e6bad19a5fb138ed131b60dca793845893d709`.
- Upstream Windows amd64 v1 archive SHA-256:
  `0f67ea6452545a5bbd192def9456ab970aedcbeea27161a04b764eb680927af2`.
- Distributed `mihomo.exe` SHA-256:
  `842fa17493a82c97148e76e3c523f5058e3cf386fba611cab86b6681e75f2f77`.
- Package path: `_internal/bin/mihomo.exe`.
- License: GPL-3.0-only; the GPL v3 text is included at
  `LICENSES/GPL-3.0-only.txt`.

The upstream project specifies Go 1.20 or newer and documents `go mod download`
followed by `go build`. The Tesseract package contains the upstream prebuilt
Windows executable; a local source build is not represented as byte-for-byte
identical to that upstream binary.

Upstream references:

- <https://github.com/MetaCubeX/mihomo/tree/v1.19.27>
- <https://github.com/MetaCubeX/mihomo/blob/v1.19.27/README.md>
- <https://github.com/MetaCubeX/mihomo/blob/v1.19.27/go.mod>

## WinTun 0.14.1

- Official prebuilt archive:
  <https://www.wintun.net/builds/wintun-0.14.1.zip>.
- Archive SHA-256:
  `07c256185d6ee3652e09fa55c0b673e2624b565e02c4b9091c79ca7d2f24ef51`.
- Distributed amd64 `wintun.dll` SHA-256:
  `e5da8447dc2c320edc0fc52fa01885c103de8c118481f683643cacc3220dafce`.
- Package path: `_internal/bin/wintun.dll`.
- Upstream signature: valid Authenticode signature by `WireGuard LLC`,
  certificate thumbprint `DF98E075A012ED8C86FBCF14854B8F9555CB3D45`.
- License: `LicenseRef-WinTun-Prebuilt`; the official prebuilt-binary license is
  included under `sources/wintun-license-0.14.1/`.

The Windows package contains the amd64 DLL from the listed official archive.

## Qt for Python, Qt, and CPython

- PySide6/Shiboken 6.11.0 source SHA-256:
  `48d5c44d7c3ed861055d5491486e6a220ef5006573cae01a5fae3fb69d786336`.
- Qt Base 6.11.0 source SHA-256:
  `231ad85979864d914dc9568a1b71c91d6cf20d7b2021d059103bf0eb51cb755e`.
- Qt SVG 6.11.0 source SHA-256:
  `dfa8d653be07087d9407ed4a4ebae847f8953e0b7abd829f089803ab652a30e6`.
- CPython 3.12.7 Windows `LICENSE.txt` SHA-256:
  `e502c6b880ff58d614901495a9009c136539cd0b1e2a2abb8fc00b934c203419`.
- CPython 3.12.7 embeddable archive SHA-256:
  `0d57bb6cb078b74d23dbfe91f77d6780d45bed328911609f1f7ee2ba1606bf44`.
- CPython archive SPDX manifest:
  <https://www.python.org/ftp/python/3.12.7/python-3.12.7-embed-amd64.zip.spdx.json>.

The included CPython Windows license contains the PSF License, Windows binary
redistribution conditions, and acknowledgements for incorporated software,
including bzip2 and libffi. CPython v3.12.7 identifies OpenSSL 3.0.15 and
libffi 3.4.4 in
<https://github.com/python/cpython/blob/v3.12.7/PCbuild/python.props>.

The Tesseract Windows package uses QtCore, QtGui, QtWidgets, and QtSvg. It does
not include Qt PDF, Qt Quick/QML, or Qt Virtual Keyboard. The release SBOM maps
each distributed executable file to its component, version, license, source,
and SHA-256 hash.

Microsoft runtime files are represented as a distribution snapshot because
they do not share a single upstream package version. Each file retains its own
PE version, when available, and its SHA-256 hash.

## Reproducibility scope

The source archive identifies the listed source and license materials. Exact
Windows binaries also depend on the corresponding Python wheels, Windows
toolchain, and the per-file SBOM. The hashes in this document apply only to the
versions and files listed above.
