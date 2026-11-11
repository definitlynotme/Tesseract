# Third-Party Notices

Tesseract is distributed under GPL-3.0-only; the complete project license is
provided in `LICENSE`. This document identifies third-party software and data
services included in or used by Tesseract.

Windows packages include the applicable license texts and a CycloneDX SBOM.
Corresponding source materials for bundled copyleft components are published
with the related release.

This is the notice for the desktop-shell edition: `Tesseract.exe` is a Tauri
(Rust) shell that hosts the web interface in Microsoft Edge WebView2, and
`tesseract-runtime.exe` is the Python runtime. This edition contains no Qt,
PySide6, or Shiboken components.

## Desktop shell (Tauri) and Rust crates

`Tesseract.exe` is built from `shell/app` with Tauri 2. The Rust crates
compiled into or used to build it are listed in
`third-party-licenses/rust-crates/INDEX.md`; crates marked "build only" (build
scripts and procedural macros) run only while the executable is built and are
recorded in the SBOM with scope `excluded`. Each crate's license text is in
`third-party-licenses/rust-crates/<crate>-<version>/`, and the SBOM lists every
crate with its Cargo.lock SHA-256 checksum. The crates are
available under MIT, Apache-2.0, Unicode-3.0, Zlib, or MPL-2.0 terms (where a
crate offers a choice, the license applied is recorded next to it). A few
crates ship no license file of their own; for them the standard text of the
declared license is included and the index marks it "canonical text".

- MPL-2.0 crates: option-ext (compiled into `Tesseract.exe`) and cssparser,
  cssparser-macros, dtoa-short, and selectors (build only: used by Tauri's
  build-time HTML processing). They are used unmodified; their exact source
  code is included in
  the related release's third-party source archive
  (`Tesseract-<version>-third-party-sources.zip`) under
  `sources/rust-<crate>-<version>/`, as listed in
  `third-party-licenses/rust-crates/MPL-2.0-SOURCES.json`.
  https://www.mozilla.org/MPL/2.0/
- Tauri 2.11.5 and wry: Apache-2.0 or MIT; tao: Apache-2.0.
  https://tauri.app/
- windows-rs: MIT or Apache-2.0; webview2-com and webview2-com-sys 0.38.2:
  MIT. https://github.com/microsoft/windows-rs
  https://github.com/wravery/webview2-rs

## Microsoft Edge WebView2

- `Tesseract.exe` statically links the WebView2 loader
  (`WebView2LoaderStatic.lib`) from the Microsoft WebView2 SDK, as vendored by
  the webview2-com-sys crate. Details and the SDK version are in
  `third-party-licenses/microsoft-webview2/NOTICE.txt`.
  https://www.nuget.org/packages/Microsoft.Web.WebView2/
- The installer may run Microsoft's Evergreen WebView2 Runtime bootstrapper to
  install the WebView2 Runtime when it is missing. The bootstrapper runs only
  from the installer's temporary directory and is not copied into the
  application directory; the WebView2 Runtime is installed and serviced by
  Microsoft under its own terms and is not part of this package.
  https://developer.microsoft.com/microsoft-edge/webview2/

## Desktop and runtime libraries

- PyYAML: MIT License. https://github.com/yaml/pyyaml
- aiohttp and its bundled parser notices: Apache-2.0 and applicable bundled
  component licenses. https://github.com/aio-libs/aiohttp
- Segno: BSD License. https://github.com/heuer/segno
- PyInstaller: GPL-2.0-or-later with the PyInstaller exception.
  https://pyinstaller.org/
- CPython 3.12.7 and its bundled Windows runtime: PSF License and the
  additional notices contained in the CPython license file.
  https://www.python.org/downloads/release/python-3127/
- OpenSSL and libffi, as bundled by CPython: Apache-2.0 and MIT respectively.
- Mihomo 1.19.27: GPL-3.0-only. The corresponding source tag and source
  materials are included with the related Tesseract release.
  https://github.com/MetaCubeX/mihomo/tree/v1.19.27
- WinTun 0.14.1: the unmodified official prebuilt `wintun.dll` is redistributed
  under WireGuard LLC's Prebuilt Binaries License. The full license is included
  in `third-party-licenses/wintun-0.14.1/`.
  https://www.wintun.net/

## Interface fonts

The desktop interface (`ui/`) bundles two web fonts. They are copied at build
time from the pinned Fontsource npm packages listed in `ui/package-lock.json`
and are never loaded from a remote server. Only the latin and latin-ext
subsets (WOFF2) are included; they are embedded in `Tesseract.exe` together
with the interface. Each package's license text ships next to the fonts as
`assets/OFL-<family>.<hash>.txt` and in `third-party-licenses/fonts/`.
The interface has no other production npm dependencies.

- Instrument Sans (variable, weight axis 400–700), font version 1.000, from
  `@fontsource-variable/instrument-sans` 5.3.0: SIL Open Font License 1.1
  (OFL-1.1). Copyright 2022 The Instrument Sans Project Authors
  (https://github.com/Instrument/instrument-sans).
- Geist Mono (weights 400 and 500), font version 1.701, from
  `@fontsource/geist-mono` 5.3.0: SIL Open Font License 1.1 (OFL-1.1).
  Copyright 2024 The Geist Project Authors
  (https://github.com/vercel/geist-font).

Neither font declares a Reserved Font Name. The license text, identical for
both fonts, is reproduced below.

```text
-----------------------------------------------------------
SIL OPEN FONT LICENSE Version 1.1 - 26 February 2007
-----------------------------------------------------------

PREAMBLE
The goals of the Open Font License (OFL) are to stimulate worldwide
development of collaborative font projects, to support the font creation
efforts of academic and linguistic communities, and to provide a free and
open framework in which fonts may be shared and improved in partnership
with others.

The OFL allows the licensed fonts to be used, studied, modified and
redistributed freely as long as they are not sold by themselves. The
fonts, including any derivative works, can be bundled, embedded,
redistributed and/or sold with any software provided that any reserved
names are not used by derivative works. The fonts and derivatives,
however, cannot be released under any other type of license. The
requirement for fonts to remain under this license does not apply
to any document created using the fonts or their derivatives.

DEFINITIONS
"Font Software" refers to the set of files released by the Copyright
Holder(s) under this license and clearly marked as such. This may
include source files, build scripts and documentation.

"Reserved Font Name" refers to any names specified as such after the
copyright statement(s).

"Original Version" refers to the collection of Font Software components as
distributed by the Copyright Holder(s).

"Modified Version" refers to any derivative made by adding to, deleting,
or substituting -- in part or in whole -- any of the components of the
Original Version, by changing formats or by porting the Font Software to a
new environment.

"Author" refers to any designer, engineer, programmer, technical
writer or other person who contributed to the Font Software.

PERMISSION & CONDITIONS
Permission is hereby granted, free of charge, to any person obtaining
a copy of the Font Software, to use, study, copy, merge, embed, modify,
redistribute, and sell modified and unmodified copies of the Font
Software, subject to the following conditions:

1) Neither the Font Software nor any of its individual components,
in Original or Modified Versions, may be sold by itself.

2) Original or Modified Versions of the Font Software may be bundled,
redistributed and/or sold with any software, provided that each copy
contains the above copyright notice and this license. These can be
included either as stand-alone text files, human-readable headers or
in the appropriate machine-readable metadata fields within text or
binary files as long as those fields can be easily viewed by the user.

3) No Modified Version of the Font Software may use the Reserved Font
Name(s) unless explicit written permission is granted by the corresponding
Copyright Holder. This restriction only applies to the primary font name as
presented to the users.

4) The name(s) of the Copyright Holder(s) or the Author(s) of the Font
Software shall not be used to promote, endorse or advertise any
Modified Version, except to acknowledge the contribution(s) of the
Copyright Holder(s) and the Author(s) or with their explicit written
permission.

5) The Font Software, modified or unmodified, in part or in whole,
must be distributed entirely under this license, and must not be
distributed under any other license. The requirement for fonts to
remain under this license does not apply to any document created
using the Font Software.

TERMINATION
This license becomes null and void if any of the above conditions are
not met.

DISCLAIMER
THE FONT SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO ANY WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT
OF COPYRIGHT, PATENT, TRADEMARK, OR OTHER RIGHT. IN NO EVENT SHALL THE
COPYRIGHT HOLDER BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
INCLUDING ANY GENERAL, SPECIAL, INDIRECT, INCIDENTAL, OR CONSEQUENTIAL
DAMAGES, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF THE USE OR INABILITY TO USE THE FONT SOFTWARE OR FROM
OTHER DEALINGS IN THE FONT SOFTWARE.
```

## Babel and Unicode CLDR

Tesseract uses Babel 2.18.0 to display country names in the selected interface language. Babel is distributed under the BSD 3-Clause License and includes locale data derived from the Unicode Common Locale Data Repository (CLDR).

- Babel: https://babel.pocoo.org/
- Babel license: https://github.com/python-babel/babel/blob/master/LICENSE
- Unicode data license: https://www.unicode.org/license.txt

## Spamhaus DROP data

DROP, DROPv6, and ASN-DROP are provided by The Spamhaus Project under the Spamhaus DROP Fair Use Policy. The list content is protected by copyright and database right. The copyright, attribution, data source, and terms are recorded here rather than repeated in the exit-check interface.

- Copyright: © 2026 The Spamhaus Project SLU
- Terms: https://www.spamhaus.org/blocklists/drop-fair-use-policy/
- Data: https://www.spamhaus.org/drop/

## Feodo Tracker

The recommended active botnet C2 blocklist is provided by abuse.ch Feodo Tracker under CC0. Feodo Tracker recommends refreshing the active list at least every 15 minutes.

- Terms and data: https://feodotracker.abuse.ch/blocklist/
- CC0: https://creativecommons.org/publicdomain/zero/1.0/

## proxycheck.io

Tesseract uses the anonymous v3 endpoint without an API key and caches each queried address for 24 hours. Only a small normalized subset needed for the on-screen assessment is retained; the complete API response is neither stored nor republished.

- API documentation and limits: https://proxycheck.io/api/
- Terms: https://proxycheck.io/terms/
- Privacy: https://proxycheck.io/privacy/

## Other runtime sources

- ipify public IP API: https://www.ipify.org/
- ident.me public IP API: https://api.ident.me/
- AWS checkip (public IP echo): https://checkip.amazonaws.com/

The overview's exit-IP lookup sends these IP services requests only through the current proxy exit, so they see the exit address, never this device's own address. Automatic lookups can be turned off in Settings.
- Team Cymru IP-to-ASN mapping: https://www.team-cymru.com/ip-asn-mapping
- Tor Onionoo: https://metrics.torproject.org/onionoo.html
- AWS IP ranges: https://ip-ranges.amazonaws.com/ip-ranges.json
- Google Cloud IP ranges: https://www.gstatic.com/ipranges/cloud.json
- Microsoft Azure service tags: https://www.microsoft.com/en-us/download/details.aspx?id=56519
- Oracle Cloud IP ranges: https://docs.oracle.com/en-us/iaas/tools/public_ip_ranges.json

Each source remains subject to its publisher's current terms and availability.
