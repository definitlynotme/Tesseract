"""Shell preview dist (T-P6-01, T-P6-06): dist-shell/Tesseract, local only, never published.

    python -B scripts/build_shell_preview.py build [--clean] [--evidence DIR] [--out DIR]
    python -B scripts/build_shell_preview.py smoke [--runs 3] [--label NAME] [--evidence DIR] [--app DIR]
    python -B scripts/build_shell_preview.py load

T-P6-06: the shell is built with `--features distribution` (shell/README.md): double-clicking
Tesseract.exe opens the default scope (the user's real profile, the legacy instance names, so the
Qt app and the preview exclude each other) with the frozen sidecar next to it, and the window is
titled "Tesseract". The build never starts it there: the assembled shell is gated with
`--print-scope` (prints the decision, takes no lock, creates nothing) under a FAKE environment
(APPDATA / LOCALAPPDATA in a temporary directory, no TESSERACT_* variable), and under a custom
one. `--out DIR` assembles into DIR instead of dist-shell/Tesseract and writes the side files
(preview-build.json, run-preview-isolated.cmd, 使用说明.txt) next to DIR, never into dist-shell.
DIR must not be the repository, dist-shell or an ancestor of either, and when it exists it must
be an assembled dist (Tesseract.exe, tesseract-runtime.exe, tier3_build_id.json): it is replaced.
The default target stages in .build/p6-dist-staging (a `--out` target next to itself); when a
program runs from the target the build stops there (or, with `--wait-minutes N`, checks again
every 2 minutes for up to N minutes) and never ends it. One build runs at a time
(.build/p6-build.lock).

Shell unit tests (not run here) use the same target directory, built in this order: first
`cargo build -p bridge_host --bins` (a scheduler test runs bridge_host's echo_sidecar.exe), then
`cargo test --manifest-path app/Cargo.toml`, once without and once with `--features distribution`,
both from shell/ with CARGO_TARGET_DIR=.build/p6-target.

Only the Shell flavour (Tauri shell + PyInstaller sidecar + web UI, plan §9.1-9.3). It never
runs the Qt release script, never downloads anything (npm / cargo / pip use what is already on
this machine: cargo --offline --locked, no npm install, no pip) and never writes the directories
the debug end-to-end runs use:

- web UI: built into .build/p6-ui/app (ui/build.mjs --target app --out), not ui/dist;
- shell: CARGO_TARGET_DIR=.build/p6-target, and TAURI_CONFIG {"build":{"frontendDist": <that
  directory>}} (shell/app/build.rs merges it the way tauri-build does), not shell/app/ui;
- sidecar: PyInstaller --workpath / --distpath under .build/p6-pyi;
- then dist-shell/Tesseract is assembled (a staging directory swapped in at the end).

Every gate failure stops the build with a message saying what is wrong.

`smoke` only runs in isolated custom scopes under a temporary directory (`--sidecar` names the
frozen sidecar; a custom scope of a distribution build accepts it): a hardened sandbox
(tests/test_bridge_p2_33.py prepare_sandbox: every listener on a free high port, empty
sources.yaml with the migration markers, every start-up network action off, system proxy / kill
switch off, APPDATA / LOCALAPPDATA inside the sandbox), the HKCU proxy values and the Tesseract
firewall rules compared before and after. It never uses the default scope or the live profile.
"""
from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
UI_OUT = REPO / ".build" / "p6-ui" / "app"
TARGET = REPO / ".build" / "p6-target"
PYI = REPO / ".build" / "p6-pyi"
DIST_ROOT = REPO / "dist-shell"
APP = DIST_ROOT / "Tesseract"
PYTHON = REPO / ".venv" / "Scripts" / "python.exe"
SHELL_MANIFEST = REPO / "shell" / "app" / "Cargo.toml"
LANGUAGES = {"de_DE", "en_US", "es_ES", "fr_FR", "ja_JP", "ko_KR", "pt_BR", "ru_RU", "zh_CN"}
# Plan §3.13 item 5 without 'unsafe-inline' (§6.12, removed at the end of P4), as in tauri.conf.json.
CSP_REQUIRED = {
    "default-src": ["'self'"], "script-src": ["'self'"], "style-src": ["'self'"], "font-src": ["'self'"],
    "img-src": ["'self'", "data:"], "connect-src": ["'self'", "ipc:", "http://ipc.localhost"],
}
# Markers that must not reach a preview / release artifact (plan §6.7, §9.7, §9.9).
FORBIDDEN_MARKERS = ("__DEMO__", "--fake-runtime", "fake_runtime", "DRILL_UPDATE_SOURCE")
# The VC runtime files the Qt spec copies from PySide6 into the bundle root (Tesseract.spec).
QT_VC_OVERRIDES = ("concrt140.dll", "msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll",
                   "msvcp140_codecvt_ids.dll", "vcruntime140.dll", "vcruntime140_1.dll")
QT_ROOTS = {"pyside", "pyside2", "pyside6", "shiboken2", "shiboken6", "pyqt4", "pyqt5", "pyqt6",
            "qtpy", "qtconsole"}
HEAVY_PROCESSES = ("chrome.exe", "node.exe", "msedgewebview2.exe", "cargo.exe", "rustc.exe")
# T-P6-06: the shell's Cargo features of a dist build (shell/app/Cargo.toml [features]).
SHELL_FEATURES = ["distribution"]
# T-P6-06: the staging directory of the default target (never a runnable copy inside dist-shell),
# the build lock (one build at a time: two -Clean builds would delete each other's .build/p6-target),
# and the files an existing target must have before a build replaces (deletes) it.
STAGING = REPO / ".build" / "p6-dist-staging"
BUILD_LOCK = REPO / ".build" / "p6-build.lock"
ASSEMBLED_MARKERS = ("Tesseract.exe", "tesseract-runtime.exe", "tier3_build_id.json")
RUNNING_POLL_SECONDS = 120


class GateError(RuntimeError):
    pass


def utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_bytes(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def gate(condition: bool, message: str) -> None:
    if not condition:
        raise GateError(message)


def log(message: str) -> None:
    print(f"[shell-preview {utc()}] {message}", flush=True)


def default_evidence() -> Path:
    return REPO / ".build" / "shell-preview-runs" / dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")


# ------------------------------------------------------------------ CPU convention


def cpu_convention() -> dict:
    """The migration CPU convention (.build/<work-order dir>/evidence/CPU_*.txt, kept by the lead
    session; found by wildcard, absent in a release tree): mode, cargo_jobs, cargo_priority."""
    found = sorted((REPO / ".build").glob("*/evidence/CPU_*.txt")) if (REPO / ".build").is_dir() else []
    values = {"mode": "normal", "cargo_jobs": str(os.cpu_count() or 4), "cargo_priority": "Normal", "file": None}
    for path in found[:1]:
        values["file"] = str(path)
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() in values:
                values[key.strip()] = value.strip()
    jobs = int(values["cargo_jobs"])
    priority = values["cargo_priority"]
    if values["mode"] == "timing":
        # T-P6-01: in a timing window cargo runs with -j 2 at BelowNormal, nothing more.
        jobs, priority = min(jobs, 2), "BelowNormal"
    gate(priority in ("Normal", "BelowNormal"), f"unknown cargo_priority {priority!r} in {values['file']}")
    return {"mode": values["mode"], "jobs": jobs, "priority": priority, "file": values["file"]}


def append_cpu_log(evidence: Path, text: str) -> None:
    evidence.mkdir(parents=True, exist_ok=True)
    with (evidence / "cpu-windows.txt").open("a", encoding="utf-8") as handle:
        handle.write(text + "\n")


# ------------------------------------------------------------------ PE helpers


def pe_facts(path: Path) -> dict:
    """Imported DLLs (import and delay-import directories) and the subsystem of a PE32+ file."""
    data = path.read_bytes()
    gate(data[:2] == b"MZ", f"{path.name}: not a PE file")
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    gate(data[pe:pe + 4] == b"PE\0\0", f"{path.name}: no PE signature")
    sections_count, optional_size = struct.unpack_from("<H", data, pe + 6)[0], struct.unpack_from("<H", data, pe + 20)[0]
    optional = pe + 24
    magic = struct.unpack_from("<H", data, optional)[0]
    gate(magic == 0x20B, f"{path.name}: not PE32+ (magic {magic:#x})")
    subsystem = struct.unpack_from("<H", data, optional + 68)[0]
    directories = optional + 112
    sections = []
    for index in range(sections_count):
        at = optional + optional_size + 40 * index
        virtual_size, virtual_address, raw_size, raw_pointer = struct.unpack_from("<IIII", data, at + 8)
        sections.append((virtual_address, max(virtual_size, raw_size), raw_pointer))

    def offset(rva: int) -> int:
        for start, size, raw in sections:
            if start <= rva < start + size:
                return raw + rva - start
        raise GateError(f"{path.name}: RVA {rva:#x} outside every section")

    def name_at(rva: int) -> str:
        at = offset(rva)
        return data[at:data.index(b"\0", at)].decode("ascii")

    imports, delayed = [], []
    rva, size = struct.unpack_from("<II", data, directories + 8 * 1)
    if rva:
        at = offset(rva)
        while True:
            name_rva = struct.unpack_from("<I", data, at + 12)[0]
            if not any(data[at:at + 20]):
                break
            imports.append(name_at(name_rva))
            at += 20
    rva, size = struct.unpack_from("<II", data, directories + 8 * 13)
    if rva:
        at = offset(rva)
        while any(data[at:at + 32]):
            attributes, name_rva = struct.unpack_from("<II", data, at)
            gate(attributes & 1 == 1, f"{path.name}: VA-based delay-load descriptor")
            delayed.append(name_at(name_rva))
            at += 32
    return {"imports": imports, "delay_imports": delayed, "subsystem": subsystem}


def version_resource(path: Path) -> dict:
    """VS_FIXEDFILEINFO, the StringFileInfo strings and the number of RT_VERSION resources."""
    version = ctypes.WinDLL("version", use_last_error=True)
    version.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    version.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    version.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p),
                                       ctypes.POINTER(wintypes.UINT)]
    size = version.GetFileVersionInfoSizeW(str(path), None)
    gate(size > 0, f"{path.name}: no version resource")
    buffer = ctypes.create_string_buffer(size)
    gate(version.GetFileVersionInfoW(str(path), 0, size, buffer), f"{path.name}: GetFileVersionInfoW failed")

    def query(block: str):
        pointer, length = ctypes.c_void_p(), wintypes.UINT()
        if not version.VerQueryValueW(buffer, block, ctypes.byref(pointer), ctypes.byref(length)) or not pointer.value:
            return None, 0
        return pointer.value, length.value

    pointer, length = query("\\")
    gate(pointer is not None and length >= 52, f"{path.name}: no VS_FIXEDFILEINFO")
    words = struct.unpack("<13I", ctypes.string_at(pointer, 52))
    gate(words[0] == 0xFEEF04BD, f"{path.name}: bad VS_FIXEDFILEINFO signature")
    split = lambda ms, ls: [ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF]
    pointer, length = query("\\VarFileInfo\\Translation")
    translations = [struct.unpack("<HH", ctypes.string_at(pointer + 4 * i, 4)) for i in range(length // 4)] if pointer else []
    strings = {}
    for language, codepage in translations or [(0x0409, 0x04B0)]:
        for name in ("FileVersion", "ProductVersion", "ProductName", "FileDescription", "OriginalFilename",
                     "InternalName", "CompanyName", "LegalCopyright"):
            pointer, length = query(f"\\StringFileInfo\\{language:04x}{codepage:04x}\\{name}")
            if pointer and name not in strings:
                strings[name] = ctypes.wstring_at(pointer, max(length - 1, 0)).rstrip("\0")
    return {"file_version": split(words[2], words[3]), "product_version": split(words[4], words[5]),
            "strings": strings, "translations": [f"{a:04x}{b:04x}" for a, b in translations],
            "version_resources": count_version_resources(path)}


def count_version_resources(path: Path) -> int:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LoadLibraryExW.argtypes = [wintypes.LPCWSTR, wintypes.HANDLE, wintypes.DWORD]
    kernel.LoadLibraryExW.restype = wintypes.HMODULE
    kernel.FreeLibrary.argtypes = [wintypes.HMODULE]
    names_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMODULE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
    languages_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMODULE, ctypes.c_void_p, ctypes.c_void_p,
                                        wintypes.WORD, ctypes.c_void_p)
    kernel.EnumResourceNamesW.argtypes = [wintypes.HMODULE, ctypes.c_void_p, names_proc, ctypes.c_void_p]
    kernel.EnumResourceLanguagesW.argtypes = [wintypes.HMODULE, ctypes.c_void_p, ctypes.c_void_p, languages_proc,
                                              ctypes.c_void_p]
    module = kernel.LoadLibraryExW(str(path), None, 0x2 | 0x20)  # AS_DATAFILE | AS_IMAGE_RESOURCE
    gate(bool(module), f"{path.name}: cannot load as a resource file")
    count = 0
    try:
        def language(*_):
            nonlocal count
            count += 1
            return True
        language_callback = languages_proc(language)

        def name(module_handle, kind, resource_name, _):
            kernel.EnumResourceLanguagesW(module_handle, kind, resource_name, language_callback, None)
            return True
        kernel.EnumResourceNamesW(module, ctypes.c_void_p(16), names_proc(name), None)  # RT_VERSION
    finally:
        kernel.FreeLibrary(module)
    return count


def check_version(path: Path, contract: dict, *, product: str, original: str) -> dict:
    facts = version_resource(path)
    expected = contract["windows_version_tuple"]
    strings = facts["strings"]
    gate(facts["version_resources"] == 1, f"{path.name}: {facts['version_resources']} VERSIONINFO resources, expected exactly 1")
    gate(facts["file_version"] == expected, f"{path.name}: fixed FileVersion {facts['file_version']} != {expected}")
    gate(facts["product_version"] == expected, f"{path.name}: fixed ProductVersion {facts['product_version']} != {expected}")
    gate(strings.get("FileVersion") == contract["version"], f"{path.name}: FileVersion string {strings.get('FileVersion')!r} != {contract['version']!r}")
    gate(strings.get("ProductVersion") == contract["version"], f"{path.name}: ProductVersion string {strings.get('ProductVersion')!r} != {contract['version']!r}")
    gate(strings.get("ProductName") == product, f"{path.name}: ProductName {strings.get('ProductName')!r} != {product!r}")
    gate(strings.get("OriginalFilename") == original, f"{path.name}: OriginalFilename {strings.get('OriginalFilename')!r} != {original!r}")
    return facts


# ------------------------------------------------------------------ helpers


def run(command: list[str], *, env: dict | None = None, cwd: Path = REPO, priority: str = "Normal",
        log_path: Path | None = None) -> float:
    started = time.monotonic()
    flags = 0x4000 if priority == "BelowNormal" else 0  # BELOW_NORMAL_PRIORITY_CLASS, inherited by children
    log(" ".join(str(part) for part in command))
    with (log_path.open("w", encoding="utf-8") if log_path else open(os.devnull, "w")) as sink:
        process = subprocess.Popen([str(part) for part in command], cwd=cwd, env=env, creationflags=flags,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                   encoding="utf-8", errors="replace")
        assert process.stdout is not None
        tail = []
        for line in process.stdout:
            sink.write(line)
            tail = (tail + [line.rstrip()])[-40:]
        code = process.wait()
    if code != 0:
        raise GateError(f"command failed (exit {code}): {' '.join(map(str, command))}\n" + "\n".join(tail))
    return round(time.monotonic() - started, 1)


def release_contract() -> dict:
    sys.path.insert(0, str(REPO))
    from app_info import APP_WINDOWS_APP_ID, release_version_contract
    version = (REPO / "app_version.txt").read_text(encoding="ascii").strip()
    contract = release_version_contract(version)
    contract["aumid"] = APP_WINDOWS_APP_ID
    return contract


def preflight() -> dict:
    facts = {"python": sys.version.split()[0]}
    for tool, args in (("node", ["--version"]), ("cargo", ["--version"]), ("rustc", ["--version"])):
        found = shutil.which(tool)
        gate(found is not None, f"{tool} is not on PATH (nothing is downloaded: install it separately)")
        facts[tool] = subprocess.run([found, *args], capture_output=True, text=True, check=True).stdout.strip()
    gate((REPO / "ui" / "node_modules").is_dir(),
         "ui/node_modules is missing; 'npm --prefix ui ci --ignore-scripts' downloads packages and needs approval")
    gate(PYTHON.is_file(), f"missing {PYTHON}")
    probe = subprocess.run([str(PYTHON), "-c", "import PyInstaller, sys; print(PyInstaller.__version__)"],
                           capture_output=True, text=True)
    gate(probe.returncode == 0, "PyInstaller is not installed in .venv (nothing is installed by this script)")
    facts["pyinstaller"] = probe.stdout.strip()
    for name in ("bin/mihomo.exe", "bin/wintun.dll", "release/sbom.cdx.json", "LICENSE", "THIRD_PARTY_NOTICES.md",
                 "app_version.txt", "static/app_icon.ico"):
        gate((REPO / name).is_file(), f"missing build input {name}")
    gate(any((REPO / "build" / "third-party-licenses").rglob("*")), "missing build/third-party-licenses")
    return facts


# ------------------------------------------------------------------ 1. web UI


def build_frontend(evidence: Path) -> dict:
    seconds = run(["node", REPO / "ui" / "build.mjs", "--target", "app", "--out", UI_OUT, "--clean"],
                  log_path=evidence / "ui-build.log")
    return {"seconds": seconds, **gate_frontend(UI_OUT)}


def csp_directives(csp: str) -> dict[str, list[str]]:
    result = {}
    for part in csp.split(";"):
        tokens = part.split()
        if tokens:
            result[tokens[0].lower()] = tokens[1:]
    return result


def gate_csp() -> str:
    config = json.loads((REPO / "shell" / "app" / "tauri.conf.json").read_text(encoding="utf-8"))
    csp = config["app"]["security"]["csp"]
    directives = csp_directives(csp)
    for name, sources in CSP_REQUIRED.items():
        gate(directives.get(name) == sources, f"CSP {name} is {directives.get(name)} (plan §3.13/§9.9: {sources})")
    for name, sources in directives.items():
        for source in sources:
            low = source.lower()
            gate("unsafe" not in low and "*" not in low and not low.startswith(("'nonce-", "'sha")),
                 f"CSP {name} allows {source}")
            gate(low.startswith("'") or low in ("data:", "ipc:", "http://ipc.localhost"),
                 f"CSP {name} names an external source {source}")
    return csp


def gate_frontend(directory: Path) -> dict:
    manifest = json.loads((directory / "build-manifest.json").read_text(encoding="utf-8"))
    gate(manifest.get("target") == "app", f"build-manifest target {manifest.get('target')!r}, expected 'app'")
    gate(manifest.get("demo_free") is True, "build-manifest demo_free is not true (node ui/tools/scan.mjs --rule demo)")
    listed = {entry["path"] for entry in manifest["files"]}
    present = {str(path.relative_to(directory)).replace("\\", "/") for path in directory.rglob("*") if path.is_file()}
    gate(present == listed | {"build-manifest.json"}, f"bundle files differ from the manifest: {sorted(present ^ (listed | {'build-manifest.json'}))}")
    for entry in manifest["files"]:
        gate(sha256(directory / entry["path"]) == entry["sha256"], f"sha256 mismatch: {entry['path']}")
    locales = set(manifest.get("locales") or {})
    gate(locales == LANGUAGES, f"bundle locales {sorted(locales)} (expected the 9 languages; no pseudo locale)")
    scan = subprocess.run(["node", str(REPO / "ui" / "tools" / "scan.mjs"), "--rule", "demo", "--mode", "enforce",
                           "--dist", str(directory)], cwd=REPO, capture_output=True, text=True, encoding="utf-8")
    gate(scan.returncode == 0, f"demo gate failed:\n{scan.stdout}\n{scan.stderr}")
    texts = {}
    for path in directory.rglob("*"):
        if path.suffix.lower() in (".js", ".css", ".html", ".json"):
            texts[str(path.relative_to(directory))] = path.read_text(encoding="utf-8", errors="replace")
    for name, text in texts.items():
        for marker in FORBIDDEN_MARKERS:
            gate(marker not in text, f"{name} contains {marker!r}")
        gate("fonts.googleapis" not in text and "fonts.gstatic" not in text, f"{name} names Google Fonts")
    html = texts["index.html"]
    gate(not re.search(r"<script(?![^>]*\bsrc=)[^>]*>\s*\S", html, re.I), "index.html has an inline script")
    gate(not re.search(r"\son[a-z]+\s*=", html, re.I) and not re.search(r"\sstyle\s*=", html, re.I)
         and not re.search(r"<style", html, re.I), "index.html has inline handlers or styles")
    gate(not re.search(r"(?:src|href)\s*=\s*[\"']?(?:https?:)?//", html, re.I), "index.html loads an external resource")
    fonts = sorted(entry["path"] for entry in manifest["files"] if entry["path"].endswith(".woff2"))
    gate(bool(fonts), "no self-hosted .woff2 font in the bundle")
    for name, text in texts.items():
        if name.endswith(".css"):
            for url in re.findall(r"url\(\s*['\"]?([^'\")]+)", text):
                gate(not re.match(r"(?:https?:)?//", url), f"{name} loads {url}")
    return {"manifest_files": len(manifest["files"]), "bytes": sum(e["bytes"] for e in manifest["files"]),
            "source_digest": manifest.get("source_digest"), "locales": sorted(locales), "fonts": fonts,
            "h_features": manifest.get("h_features"), "csp": gate_csp(),
            "manifest_sha256": sha256(directory / "build-manifest.json")}


# ------------------------------------------------------------------ 2. shell


def build_shell(evidence: Path, cpu: dict, contract: dict) -> dict:
    env = dict(os.environ, CARGO_TARGET_DIR=str(TARGET),
               TAURI_CONFIG=json.dumps({"build": {"frontendDist": str(UI_OUT)}}))
    command = ["cargo", "build", "--release", "--locked", "--offline", "-j", str(cpu["jobs"]),
               "--features", ",".join(SHELL_FEATURES), "--manifest-path", str(SHELL_MANIFEST)]
    start = utc()
    try:
        # cwd = shell/: cargo finds shell/.cargo/config.toml (the static CRT, plan §9.3) from the
        # working directory, not from --manifest-path.
        seconds = run(command, env=env, cwd=REPO / "shell", priority=cpu["priority"],
                      log_path=evidence / "cargo-build.log")
        code = 0
    except GateError:
        code = 1
        raise
    finally:
        append_cpu_log(evidence, f"T-P6-06 cargo build --release --locked --offline -j {cpu['jobs']} "
                                 f"--features {','.join(SHELL_FEATURES)} "
                                 f"priority={cpu['priority']} mode={cpu['mode']} target_dir=.build/p6-target "
                                 f"start={start} end={utc()} exit={code}")
    exe = TARGET / "release" / "tesseract-shell.exe"
    gate(exe.is_file(), f"missing {exe}")
    return {"seconds": seconds, "exe": str(exe), **gate_shell(exe, contract)}


def gate_shell(exe: Path, contract: dict) -> dict:
    pe = pe_facts(exe)
    crt = [name for name in pe["imports"] + pe["delay_imports"]
           if re.match(r"(?i)^(vcruntime|msvcp|concrt)\d", name)]
    gate(not crt, f"shell imports the VC runtime {crt}: the static CRT (shell/.cargo/config.toml) did not apply")
    gate(pe["subsystem"] == 2, f"shell subsystem {pe['subsystem']}, expected 2 (Windows GUI)")
    version = check_version(exe, contract, product="Tesseract", original="Tesseract.exe")
    data = exe.read_bytes()
    gate(contract["aumid"].encode() in data, f"AUMID {contract['aumid']} not in the shell binary")
    webview = (REPO / "shell" / "app" / "src" / "webview.rs").read_text(encoding="utf-8")
    gate(f'pub const AUMID: &str = "{contract["aumid"]}";' in webview, "webview.rs AUMID differs from app_info.APP_WINDOWS_APP_ID (G58)")
    csp = gate_csp()
    gate(csp.encode() in data, "the configured CSP is not embedded in the shell")
    for marker in FORBIDDEN_MARKERS:
        gate(marker.encode() not in data, f"shell binary contains {marker!r}")
    # Developer tools: tauri's `devtools` feature is off and the release policy (window.rs) is off.
    metadata = json.loads(subprocess.run(
        ["cargo", "metadata", "--format-version", "1", "--offline", "--locked", "--manifest-path", str(SHELL_MANIFEST),
         "--filter-platform", "x86_64-pc-windows-msvc"], cwd=REPO, capture_output=True, text=True, check=True,
        encoding="utf-8").stdout)
    tauri = [node for node in metadata["resolve"]["nodes"] if re.search(r"[/#]tauri@2\.", node["id"])]
    gate(len(tauri) == 1, f"expected one resolved tauri package, found {len(tauri)}")
    gate("devtools" not in tauri[0]["features"], f"tauri is built with devtools: {tauri[0]['features']}")
    window = (REPO / "shell" / "app" / "src" / "window.rs").read_text(encoding="utf-8")
    gate("Policy { devtools: debug," in window, "window.rs no longer ties devtools to debug builds")
    # The embedded UI is exactly the private app target (build.rs copies it into OUT_DIR/ui).
    outs = sorted((TARGET / "release" / "build").glob("tesseract-shell-*/out/ui/build-manifest.json"),
                  key=lambda path: path.stat().st_mtime)
    gate(bool(outs), "no OUT_DIR/ui of the shell build")
    embedded = outs[-1].parent
    for path in UI_OUT.rglob("*"):
        if path.is_file():
            twin = embedded / path.relative_to(UI_OUT)
            gate(twin.is_file() and sha256(twin) == sha256(path), f"embedded UI differs: {path.relative_to(UI_OUT)}")
    extra = [str(p.relative_to(embedded)) for p in embedded.rglob("*") if p.is_file() and not (UI_OUT / p.relative_to(embedded)).is_file()]
    gate(not extra, f"embedded UI has files the app target does not: {extra}")
    return {"imports": pe["imports"], "delay_imports": pe["delay_imports"], "version": version,
            "tauri_features": tauri[0]["features"], "embedded_ui": str(embedded), "csp": csp,
            "bytes": exe.stat().st_size, "sha256": sha256(exe)}


# ------------------------------------------------------------------ 3. sidecar


VERSION_TEMPLATE = """VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={tuple}, prodvers={tuple}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0),
  ),
  kids=[
    StringFileInfo([
      StringTable(
        "040904B0",
        [
          StringStruct("CompanyName", "Tesseract contributors"),
          StringStruct("FileDescription", "Tesseract runtime"),
          StringStruct("FileVersion", "{version}"),
          StringStruct("InternalName", "tesseract-runtime"),
          StringStruct("LegalCopyright", "Copyright \\u00a9 2026 Tesseract contributors"),
          StringStruct("OriginalFilename", "tesseract-runtime.exe"),
          StringStruct("ProductName", "Tesseract"),
          StringStruct("ProductVersion", "{version}"),
          StringStruct("Comments", "https://github.com/definitlynotme/Tessract"),
        ],
      )
    ]),
    VarFileInfo([VarStruct("Translation", [1033, 1200])]),
  ],
)
"""


def build_sidecar(evidence: Path, contract: dict) -> dict:
    PYI.mkdir(parents=True, exist_ok=True)
    version_file = PYI / "version_info.runtime.txt"
    version_file.write_text(VERSION_TEMPLATE.replace("{tuple}", repr(tuple(contract["windows_version_tuple"])))
                            .replace("{version}", contract["version"]), encoding="utf-8")
    env = dict(os.environ, TESSERACT_RUNTIME_VERSION_INFO_PATH=str(version_file))
    env.pop("TESSERACT_ISOLATED_AUDIT_MANIFEST", None)
    seconds = run([PYTHON, "-m", "PyInstaller", "--noconfirm", "--workpath", PYI / "work", "--distpath", PYI / "dist",
                   REPO / "tesseract-runtime.spec"], env=env, log_path=evidence / "pyinstaller.log")
    bundle = PYI / "dist" / "tesseract-runtime"
    return {"seconds": seconds, "bundle": str(bundle), **gate_sidecar(bundle, contract)}


def gate_sidecar(bundle: Path, contract: dict) -> dict:
    sys.path.insert(0, str(REPO))
    sys.modules.setdefault("pytest", None)  # the import-graph helpers must not need pytest
    from PyInstaller.archive.readers import CArchiveReader
    from tests.test_bridge_qt_guard import sidecar_modules, sidecar_source_files
    exe = bundle / "tesseract-runtime.exe"
    gate(exe.is_file(), f"missing {exe}")
    pe = pe_facts(exe)
    gate(pe["subsystem"] == 2, f"sidecar subsystem {pe['subsystem']}: console=False expected (plan §9.2)")
    version = check_version(exe, contract, product="Tesseract", original="tesseract-runtime.exe")
    internal = bundle / "_internal"
    forbidden = []
    for path in bundle.rglob("*"):
        parts = [part.casefold() for part in path.relative_to(bundle).parts]
        if any(part in QT_ROOTS or part in {"qt", "qt5", "qt6"} for part in parts) or (
                parts[-1].startswith(("qt", "pyside", "shiboken")) and parts[-1].endswith((".dll", ".pyd"))):
            forbidden.append(str(path.relative_to(bundle)))
    archive = CArchiveReader(str(exe))
    modules = list(archive.toc)
    for name in archive.toc:
        if name.endswith(".pyz"):
            modules.extend(archive.open_embedded_archive(name).toc)
    forbidden += [name for name in modules if name.split(".")[0].casefold() in QT_ROOTS]
    gate(not forbidden, f"Qt artifacts in the sidecar: {forbidden}")
    # The Qt build's root VC runtime overrides (PySide6 copies): none of the C++ ones, and the two
    # vcruntime files only as Python's own copies.
    base = Path(sys.base_prefix)
    vc = {}
    for name in QT_VC_OVERRIDES:
        for found in [p for p in bundle.rglob("*") if p.name.casefold() == name]:
            own = base / found.name
            same = own.is_file() and sha256(own) == sha256(found)
            vc[str(found.relative_to(bundle))] = "python" if same else "foreign"
            gate(name.startswith("vcruntime") and same,
                 f"{found.relative_to(bundle)} is not Python's own VC runtime (a Qt-style override)")
    gate(not any(p.name.casefold() in {"pyside6", "shiboken6"} for p in internal.iterdir()), "PySide6/shiboken6 in _internal")
    gate((internal / "python312.dll").is_file(), "missing _internal/python312.dll")
    for marker in FORBIDDEN_MARKERS:
        gate(not any(marker in name for name in modules), f"sidecar module named {marker!r}")
    closure = sidecar_modules()
    missing = sorted(set(closure) - set(modules))
    gate(not missing, f"sidecar modules missing from the bundle: {missing}")
    for path in sidecar_source_files():
        text = (REPO / path).read_text(encoding="utf-8", errors="replace")
        for marker in ("DRILL_UPDATE_SOURCE", "--fake-runtime"):
            gate(marker not in text, f"{path} contains {marker!r}")
    # T-P2-H9 order, read back from the frozen archive (PYSOURCE entries run in TOC order): the audit
    # hook, then the platform seed hook, then PyInstaller's standard hooks (pyi_rth_setuptools among
    # them, which imports jaraco.context -> platform.system()), then the entry.
    scripts = [name for name, entry in archive.toc.items() if entry[-1] == "s"]
    for name in ("tier3_audit_runtime_hook", "sidecar_platform_seed_rthook", "runtime_main"):
        gate(name in scripts, f"PYSOURCE {name} missing: {scripts}")
    audit, seed, entry = (scripts.index(n) for n in ("tier3_audit_runtime_hook", "sidecar_platform_seed_rthook", "runtime_main"))
    standard = [i for i, name in enumerate(scripts) if name.startswith("pyi_rth_")]
    gate(audit < seed and all(seed < i < entry for i in standard) and entry == len(scripts) - 1,
         f"PYSOURCE order is not audit < seed < pyi_rth_* < runtime_main: {scripts}")
    if "pyi_rth_setuptools" in scripts:
        gate(seed < scripts.index("pyi_rth_setuptools"), f"seed hook after pyi_rth_setuptools: {scripts}")
    manifests = [json.loads((bundle / name).read_bytes()) for name in ("tier3_build_id.json", "_internal/tier3_build_id.json")]
    digest = sha256(exe)
    gate(manifests[0] == manifests[1], "the two sidecar identity manifests differ")
    gate(all(m["declared_executable_sha256"] == m["executable_sha256"] == digest and m["identity_match"] is True
             for m in manifests), "sidecar identity does not match tesseract-runtime.exe")
    undeclared = sorted(set(sidecar_source_files()) - set(manifests[0]["components"]))
    gate(not undeclared, f"closure files missing from the identity components: {undeclared}")
    for resource in ("bin/mihomo.exe", "bin/wintun.dll", "sbom.cdx.json", "app_version.txt", "LICENSE",
                     "THIRD_PARTY_NOTICES.md"):
        gate((internal / resource).is_file(), f"missing _internal/{resource}")
    for directory in ("locales", "static", "third-party-licenses"):
        gate(any((internal / directory).iterdir()), f"empty _internal/{directory}")
    return {"pysource_order": scripts, "pyi_rth_setuptools": "pyi_rth_setuptools" in scripts,
            "imports": pe["imports"], "version": version, "vc_runtime": vc,
            "closure_modules": len(closure), "identity_components": len(manifests[0]["components"]),
            "build_id": manifests[0]["build_id"], "sha256": digest}


# ------------------------------------------------------------------ 4. assemble


LAUNCHER = r"""@echo off
rem Tesseract shell preview (T-P6-01; renamed run-preview-isolated.cmd by T-P6-06). Not the
rem live profile: an explicit custom scope (shell/README.md) with a fresh profile next to this
rem file. Double-click {app}\Tesseract.exe itself for the real profile (see the usage notes).
rem Exit the running Tesseract first: both use the same ports (config.yaml) when the service is
rem started. Delete preview-profile to start over.
setlocal
set "TESSERACT_INSTANCE_SCOPE=shell-preview"
set "TESSERACT_CONFIG_DIR=%~dp0preview-profile\config"
set "TESSERACT_DATA_DIR=%~dp0preview-profile\data"
set "TESSERACT_ISOLATED_AUDIT_MANIFEST="
if not exist "%TESSERACT_CONFIG_DIR%" mkdir "%TESSERACT_CONFIG_DIR%"
if not exist "%TESSERACT_DATA_DIR%" mkdir "%TESSERACT_DATA_DIR%"
start "" "%~dp0{app}\Tesseract.exe" --sidecar "%~dp0{app}\tesseract-runtime.exe"
"""
LAUNCHER_NAME = "run-preview-isolated.cmd"
# T-P6-01's launcher name; removed when the side files are written (T-P6-06 renamed it).
OLD_LAUNCHER_NAME = "run-preview.cmd"
README_NAME = "使用说明.txt"
README = """Tesseract：使用说明
（scripts/build_shell_preview.py 生成；版本与构建信息见同目录的 preview-build.json）

一、双击 {app}\\Tesseract.exe：正式版，用你的真实配置
  - 配置与数据：%APPDATA%\\Tesseract（配置）、%LOCALAPPDATA%\\Tesseract（数据与日志）。
  - 界面自己的浏览器数据在 %LOCALAPPDATA%\\Tesseract\\WebView2\\default
    （以管理员身份运行时是 WebView2-admin\\default）。
  - 后台服务就是同目录的 tesseract-runtime.exe，不会去用别的程序。
  - 同一时间只能运行一个 Tesseract：已经在运行时，再双击只会把它的窗口叫到前面。
  - 启动后可能自动提示有新版本，忽略即可（见第三节）。

二、{launcher}：独立的全新配置，不碰真实配置
  - 配置放在本目录的 preview-profile 里，第一次运行是全新的首启配置（没有你的节点和订阅）。
  - 这种方式下系统代理与熔断被强制关闭，不会改系统代理和防火墙。
  - 它和正式配置的 Tesseract 不互斥：用之前先退出正在运行的 Tesseract，否则启动服务后两者会抢同样的端口。
  - 想从头再来，删掉 preview-profile 文件夹。

三、请注意
  - 不要用应用内更新（检查或安装更新）：线上发布的仍是旧版本。启动约 6 秒后它会自动检查一次，提示有新版本时忽略即可。

四、旧的 Qt 版
  - 2026-11-01 切换到新界面时，原来的 Qt 版 dist 整个备份在仓库的 .build\\claude-shell-dist-before-<交付标签>\\Tesseract。
  - 两者用同一份配置，不能同时运行。想临时用回 Qt 版：先在托盘菜单里点「退出」，再运行备份里的 Tesseract.exe。

五、双击后没有任何反应
  - Tesseract 拒绝启动时不弹窗，也可能来不及写日志。常见原因：
    1. 环境变量里有 TESSERACT_ 开头的变量（例如残留的 TESSERACT_CONFIG_DIR）：
       在「编辑系统环境变量 → 环境变量」里删掉它们，注销或重新打开资源管理器后再双击。
    2. {app} 文件夹里缺了 tesseract-runtime.exe（例如被杀毒软件隔离）：重新复制整个 {app} 目录。
  - 查看判定结果：在本目录的地址栏输入 powershell 回车，运行
      .\\{app}\\Tesseract.exe --print-scope | Out-File scope.txt
    它只打印一行判定结果然后退出，不启动程序、不改任何文件（scope.txt 除外）。
    scope.txt 里 "error" 或 "sidecar_error" 不是 null 时，就是拒绝的原因；
    "sidecar" 里 "exists" 为 false 表示缺 tesseract-runtime.exe。
"""


def remove_inside(path: Path, root: Path) -> None:
    resolved = path.resolve()
    gate(resolved != root.resolve() and root.resolve() in resolved.parents, f"refusing to delete {resolved}")
    if resolved.exists():
        shutil.rmtree(resolved)


def check_target(app: Path) -> None:
    """T-P6-06: the target a build assembles into, and on success replaces (the old one is deleted).
    Never the repository, dist-shell itself, an ancestor of either, a drive root or a private build
    directory; an existing target must look like an assembled dist, so a mistyped `--out` never
    deletes an unrelated directory."""
    resolved = app.resolve()
    protected = [REPO, DIST_ROOT]
    gate(resolved.parent != resolved, f"refusing to assemble into a drive root: {resolved}")
    for path in protected:
        path = path.resolve()
        gate(resolved != path and resolved not in path.parents,
             f"refusing to assemble into {resolved}: it is or contains {path}")
    for path in (UI_OUT.parent, TARGET, PYI, STAGING):
        path = path.resolve()
        gate(resolved != path and path not in resolved.parents,
             f"refusing to assemble into {resolved}: it is inside the private build directory {path}")
    if resolved.exists() or resolved.is_symlink():
        gate(resolved.is_dir() and not resolved.is_symlink(), f"{resolved} exists and is not a directory")
        missing = [name for name in ASSEMBLED_MARKERS if not (resolved / name).is_file()]
        gate(not missing, f"{resolved} exists but is not an assembled dist (lacks {', '.join(missing)}); "
                          "refusing to replace it: choose a new directory for --out")


def staging_for(app: Path) -> tuple[Path, Path]:
    """(staging directory, the directory it must stay inside) for a target. The default target stages
    in .build/p6-dist-staging, so a stopped build never leaves a runnable copy in dist-shell."""
    if same_path(app, APP):
        return STAGING, REPO / ".build"
    return app.parent / f".{app.name}.staging", app.parent


class BuildLock:
    """T-P6-06: one build at a time (.build/p6-build.lock, an OS byte lock: released when the process
    ends, so a crashed build leaves no stale lock)."""
    OFFSET = 1 << 20

    def __init__(self, path: Path = BUILD_LOCK):
        self.path, self.handle = path, None

    def __enter__(self):
        import msvcrt
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        try:
            handle.seek(self.OFFSET)  # a byte past the holder line, which stays readable
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            try:
                handle.seek(0)
                holder = handle.read(200).decode("utf-8", "replace").strip()
            except OSError:
                holder = ""
            handle.close()
            raise GateError(f"another shell preview build is running ({self.path}: {holder or 'holder unknown'})") from None
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid {os.getpid()} since {utc()}\n".encode("utf-8"))
        handle.flush()
        self.handle = handle
        return self

    def __exit__(self, *_):
        import msvcrt
        if self.handle is not None:
            try:
                self.handle.seek(self.OFFSET)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            self.handle.close()
            self.handle = None
        return False


def same_path(left, right) -> bool:
    """Equal after resolving; the shell's custom scope keeps fs::canonicalize's verbatim `\\\\?\\`."""
    def plain(value) -> str:
        text = str(value)
        if text[:8].casefold() == "\\\\?\\unc\\":
            text = "\\\\" + text[8:]
        elif text.startswith("\\\\?\\"):
            text = text[4:]
        return os.path.normcase(str(Path(text).resolve()))
    return plain(left) == plain(right)


def fake_environment(root: Path) -> dict:
    """This process's environment without any TESSERACT_* variable, with APPDATA, LOCALAPPDATA and
    USERPROFILE inside `root` (a temporary directory): the default scope lands there, never in
    the live profile."""
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("TESSERACT_")}
    env.update(APPDATA=str(root / "Roaming"), LOCALAPPDATA=str(root / "Local"), USERPROFILE=str(root / "home"))
    return env


def print_scope(exe: Path, env: dict, args: tuple[str, ...] = ()) -> tuple[int, dict]:
    process = subprocess.run([str(exe), "--print-scope", *args], env=env, cwd=Path(env["USERPROFILE"]).parent,
                             capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace")
    lines = [line for line in process.stdout.splitlines() if line.strip()]
    gate(len(lines) == 1, f"--print-scope {list(args)} printed {len(lines)} lines (exit {process.returncode}): "
                          f"{process.stdout!r} {process.stderr!r}")
    try:
        return process.returncode, json.loads(lines[0])
    except ValueError as error:
        raise GateError(f"--print-scope printed no JSON: {lines[0]!r}") from error


def gate_print_scope(app: Path, contract: dict) -> dict:
    """T-P6-06 gate: the assembled Tesseract.exe decides the default scope correctly. Only
    `--print-scope` and `--version` run (no lock, no directory, no log, no process), and only under
    fake environments; the custom run follows only after the default run proved the binary is a
    distribution build that knows `--print-scope` (an older shell would start for real there)."""
    sys.path.insert(0, str(REPO))
    from dataclasses import asdict
    from bridge_instance_scope import custom_scope_id, names_for_scope
    exe, sidecar = app / "Tesseract.exe", app / "tesseract-runtime.exe"
    gate(b"--print-scope" in exe.read_bytes(), "Tesseract.exe does not know --print-scope (not a T-P6-06 shell)")
    root = Path(tempfile.mkdtemp(prefix="tesseract-p6-print-scope-")).resolve()
    result: dict = {"fake_root": str(root)}
    try:
        env = fake_environment(root)
        roaming, local = root / "Roaming", root / "Local"
        code, default = print_scope(exe, env)
        result["default"] = default
        gate(code == 0 and default.get("error") is None, f"default scope refused: exit {code} {default}")
        gate(default["build"] == "distribution", f"Tesseract.exe is a {default['build']} build, not the distribution build")
        gate((default["kind"], default["id"]) == ("default", "default"), f"default scope kind/id {default['kind']}/{default['id']}")
        gate(same_path(default["config"], roaming / "Tesseract"), f"default config {default['config']} is not the fake APPDATA\\Tesseract")
        gate(same_path(default["data"], local / "Tesseract"), f"default data {default['data']} is not the fake LOCALAPPDATA\\Tesseract")
        gate(default["names"] == asdict(names_for_scope("default", "default")), f"default names {default['names']} are not the legacy names")
        gate(default["sidecar"]["mode"] == "frozen" and same_path(default["sidecar"]["path"], sidecar)
             and default["sidecar"]["exists"] is True, f"default sidecar {default['sidecar']} is not {sidecar}")
        gate(Path(default["webview2"]).resolve().is_relative_to((local / "Tesseract" / "WebView2").resolve()),
             f"WebView2 folder {default['webview2']} is not under <data>\\WebView2 (plan §9.8)")
        gate(not roaming.exists() and not local.exists(), "--print-scope created a directory")
        code, refused = print_scope(exe, env, ("--sidecar", str(sidecar)))
        result["default_with_sidecar"] = refused
        gate(code == 2 and refused.get("sidecar_error") == "sidecar_override_forbidden",
             f"the default scope accepted --sidecar: exit {code} {refused}")
        version = subprocess.run([str(exe), "--version"], env=env, capture_output=True, text=True, timeout=30)
        result["version"] = version.stdout.strip()
        gate(version.stdout.strip() == f"Tesseract {contract['version']}", f"--version printed {version.stdout!r}")
        config, data = root / "custom" / "config", root / "custom" / "data"
        config.mkdir(parents=True)
        data.mkdir(parents=True)
        custom_env = dict(env, TESSERACT_INSTANCE_SCOPE="p6-print-scope", TESSERACT_CONFIG_DIR=str(config),
                          TESSERACT_DATA_DIR=str(data))
        code, custom = print_scope(exe, custom_env)
        result["custom"] = custom
        scope_id = custom_scope_id(config, data)
        gate(code == 0 and (custom["kind"], custom["id"]) == ("custom", scope_id), f"custom scope: exit {code} {custom} (expected id {scope_id})")
        gate(custom["names"] == asdict(names_for_scope("custom", scope_id)), f"custom names {custom['names']}")
        gate(same_path(custom["config"], config) and same_path(custom["data"], data), f"custom directories {custom}")
        gate(custom["sidecar"]["mode"] == "frozen" and same_path(custom["sidecar"]["path"], sidecar), f"custom sidecar {custom['sidecar']}")
        code, unlabeled = print_scope(exe, dict(env, TESSERACT_CONFIG_DIR=str(config)))
        result["directory_without_label"] = unlabeled
        gate(code == 2 and unlabeled.get("error") == "scope_label_missing", f"a directory without a label: exit {code} {unlabeled}")
        gate(not roaming.exists() and not local.exists(), "--print-scope created a directory")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return result


def wait_until_idle(app: Path, minutes: int) -> list[str]:
    """Programs running from `app` (process_table + image paths); with `minutes` > 0, checked again
    every RUNNING_POLL_SECONDS until none run or the time is up (they are never ended)."""
    sys.path.insert(0, str(REPO))
    from tests.test_bridge_p2_33 import process_table
    deadline = time.monotonic() + minutes * 60
    while True:
        running = dist_processes(process_table, app)
        if not running or time.monotonic() + RUNNING_POLL_SECONDS > deadline:
            return running
        log(f"programs are running from {app}: {running}; checking again in {RUNNING_POLL_SECONDS} s")
        time.sleep(RUNNING_POLL_SECONDS)


def swap_in(staging: Path, app: Path) -> None:
    """Replace `app` with `staging` (two renames); an OSError (a handle held by Explorer, an antivirus
    or a program started meanwhile) becomes a GateError naming where the new build stays, and the
    old directory is put back when the second rename fails."""
    root = app.parent
    old = root / f".{app.name}.old-{os.getpid()}"
    if app.exists():
        try:
            app.rename(old)
        except OSError as error:
            raise GateError(f"could not move {app} aside ({error}); {app} is unchanged and the new build stays "
                            f"in {staging} (close what uses it and build again)") from None
    try:
        staging.rename(app)
    except OSError as error:
        restored = ""
        if old.exists():
            try:
                old.rename(app)
                restored = f"; {app} was put back"
            except OSError as again:
                restored = f"; putting the old build back failed too ({again}): it is in {old}"
        raise GateError(f"could not move the new build to {app} ({error}){restored}; the new build stays in {staging}") from None
    if old.exists():
        remove_inside(old, root)


def assemble(shell: dict, sidecar: dict, contract: dict, frontend: dict, app: Path = APP,
             summary: dict | None = None, wait_minutes: int = 0) -> dict:
    summary = {} if summary is None else summary
    check_target(app)
    root = app.parent
    root.mkdir(parents=True, exist_ok=True)
    staging, staging_root = staging_for(app)
    summary["staging"] = str(staging)
    remove_inside(staging, staging_root)
    legacy = root / f".{app.name}.staging"
    if staging != legacy and legacy.is_dir():
        remove_inside(legacy, root)  # T-P6-06 build-1 staged inside dist-shell; no runnable copy stays there
    shutil.copytree(Path(sidecar["bundle"]), staging)
    shutil.copy2(Path(shell["exe"]), staging / "Tesseract.exe")
    for name in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
        shutil.copy2(REPO / name, staging / name)
    required = ["Tesseract.exe", "tesseract-runtime.exe", "tier3_build_id.json", "LICENSE", "THIRD_PARTY_NOTICES.md",
                "_internal/python312.dll", "_internal/bin/mihomo.exe", "_internal/bin/wintun.dll",
                "_internal/app_version.txt", "_internal/sbom.cdx.json", "_internal/tier3_build_id.json"]
    for name in required:
        gate((staging / name).is_file(), f"assembled dist lacks {name}")
    for name in ("locales", "static", "third-party-licenses"):
        gate((staging / "_internal" / name).is_dir(), f"assembled dist lacks _internal/{name}")
    gate(sha256(staging / "Tesseract.exe") == shell["sha256"], "Tesseract.exe changed while copying")
    gate(sha256(staging / "tesseract-runtime.exe") == sidecar["sha256"], "tesseract-runtime.exe changed while copying")
    # T-P6-06: the default-scope decision of exactly the assembled files, before anything is swapped.
    summary["print_scope"] = gate_print_scope(staging, contract)  # recorded even when a later step stops
    # Never swap a dist a person may be looking at: the new build stays in the staging directory.
    running = wait_until_idle(app, wait_minutes)
    gate(not running, f"programs are running from {app}: {running}; the new build stays in {staging} "
                      "(build again later, with --wait-minutes, or into another directory with --out)")
    check_target(app)  # again: the target may have changed while the build ran
    swap_in(staging, app)
    (root / LAUNCHER_NAME).write_bytes(LAUNCHER.replace("{app}", app.name).replace("\n", "\r\n").encode("ascii"))
    stale = root / OLD_LAUNCHER_NAME
    if stale.is_file() and stale.read_bytes().startswith(b"@echo off\r\nrem Tesseract shell preview (T-P6-01)"):
        stale.unlink()  # T-P6-01's launcher, renamed run-preview-isolated.cmd
    (root / README_NAME).write_bytes(b"\xef\xbb\xbf" + README.replace("{app}", app.name).replace("{launcher}", LAUNCHER_NAME)
                                     .replace("\n", "\r\n").encode("utf-8"))
    sizes = {
        "total_bytes": tree_bytes(app), "shell_exe_bytes": (app / "Tesseract.exe").stat().st_size,
        "sidecar_exe_bytes": (app / "tesseract-runtime.exe").stat().st_size,
        "internal_bytes": tree_bytes(app / "_internal"), "internal_bin_bytes": tree_bytes(app / "_internal" / "bin"),
        "files": sum(1 for p in app.rglob("*") if p.is_file()),
    }
    qt = REPO / "dist" / "Tesseract"
    if qt.is_dir():
        sizes["qt_dist_bytes_for_comparison"] = tree_bytes(qt)
    identity = {"version": contract["version"], "windows_version": contract["windows_version_numeric"],
                "built_utc": utc(), "shell_sha256": shell["sha256"], "sidecar_sha256": sidecar["sha256"],
                "sidecar_build_id": sidecar["build_id"], "ui_source_digest": frontend["source_digest"],
                "ui_manifest_sha256": frontend["manifest_sha256"], "sizes": sizes,
                "distribution": True, "shell_cargo_features": SHELL_FEATURES, "app_dir": app.name}
    (root / "preview-build.json").write_text(json.dumps(identity, indent=2) + "\n", encoding="utf-8")
    return identity


def build(args) -> int:
    evidence = Path(args.evidence).resolve() if args.evidence else default_evidence()
    evidence.mkdir(parents=True, exist_ok=True)
    # T-P6-06: --out assembles elsewhere (T-P6-05's refresh builds a candidate, then delivers it).
    app = Path(args.out).resolve() if args.out else APP
    summary: dict = {"started_utc": utc(), "evidence": str(evidence), "app": str(app)}
    started = time.monotonic()
    try:
        gate(args.wait_minutes >= 0, "--wait-minutes must not be negative")
        # One build at a time; the target is checked before minutes of building (a wrong --out stops here).
        with BuildLock():
            summary["lock"] = str(BUILD_LOCK)
            check_target(app)
            cpu = cpu_convention()
            summary["cpu"] = cpu
            summary["tools"] = preflight()
            contract = release_contract()
            summary["contract"] = contract
            gate(not (args.clean and args.reuse_shell), "--clean and --reuse-shell exclude each other")
            if args.clean:
                for path in (UI_OUT.parent, TARGET, PYI):
                    remove_inside(path, REPO / ".build")
                summary["clean"] = [str(p) for p in (UI_OUT.parent, TARGET, PYI)]
            exe = TARGET / "release" / "tesseract-shell.exe"
            if args.reuse_shell and exe.is_file() and (UI_OUT / "build-manifest.json").is_file():
                # The web UI and the shell of an earlier run (e.g. after the sidecar's input guard
                # stopped a build because a backend file changed), gated again, not rebuilt.
                log("1-2/4 reusing the built web UI and shell (gated again)")
                summary["frontend"] = {"reused": True, **gate_frontend(UI_OUT)}
                summary["shell"] = {"reused": True, "exe": str(exe), **gate_shell(exe, contract)}
            else:
                log("1/4 web UI (private app target)")
                summary["frontend"] = build_frontend(evidence)
                log("2/4 shell (cargo --release --locked --offline)")
                summary["shell"] = build_shell(evidence, cpu, contract)
            log("3/4 sidecar (PyInstaller)")
            summary["sidecar"] = build_sidecar(evidence, contract)
            log(f"4/4 assemble {app} (gated with --print-scope under a fake environment first)")
            summary["dist"] = assemble(summary["shell"], summary["sidecar"], contract, summary["frontend"], app,
                                       summary, args.wait_minutes)
        summary["result"] = "passed"
        return_code = 0
    except GateError as error:
        summary["result"] = "failed"
        summary["error"] = str(error)
        log(f"FAILED: {error}")
        return_code = 1
    summary["seconds"] = round(time.monotonic() - started, 1)
    summary["finished_utc"] = utc()
    (evidence / "build-summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str) + "\n",
                                                 encoding="utf-8")
    if return_code == 0:
        sizes = summary["dist"]["sizes"]
        log(f"{app} ready in {summary['seconds']} s: {sizes['total_bytes'] / 1e6:.1f} MB "
            f"(Tesseract.exe {sizes['shell_exe_bytes'] / 1e6:.1f} MB, _internal {sizes['internal_bytes'] / 1e6:.1f} MB); "
            f"summary {evidence / 'build-summary.json'}")
    return return_code


# ------------------------------------------------------------------ smoke


def _process_api():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    return kernel


def image_path(pid: int) -> str | None:
    kernel = _process_api()
    handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        buffer, size = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
        return buffer.value if kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)) else None
    finally:
        kernel.CloseHandle(handle)


def dist_processes(process_table, app: Path = APP) -> list[str]:
    """Live processes whose image lies under `app` (dist-shell/Tesseract by default): shell, sidecar,
    helpers, Mihomo."""
    root = str(app.resolve()).casefold().rstrip("\\") + "\\"
    found = []
    for pid, (_, name) in process_table().items():
        path = image_path(pid)
        if path and path.casefold().startswith(root):
            found.append(f"{pid} {name}")
    return found


LOAD_QUERY = (
    "$ErrorActionPreference='Stop'; $r=[ordered]@{test_chrome=0;other_chrome=0;test_node=0;other_node=0}; "
    "foreach($p in @(Get-CimInstance Win32_Process -Filter \"Name='chrome.exe' or Name='node.exe'\")){ "
    "$c=[string]$p.CommandLine; "
    "if($p.Name -eq 'chrome.exe'){ if($c -match 'headless|remote-debugging|playwright|\\\\Temp\\\\'){$r.test_chrome++}else{$r.other_chrome++} } "
    "elseif($c -match 'ui[\\\\/]test|playwright|build\\.mjs|tools[\\\\/]'){$r.test_node++}else{$r.other_node++} }; "
    "ConvertTo-Json -InputObject $r -Compress")


def heavy_load(process_table) -> dict:
    """Counts by image name, and the test load of a parallel UI check: Chrome started headless /
    with remote debugging / from a temporary profile, and node running ui tests or tools (the
    user's own browser, editors and agent runtimes are counted apart and do not make a run
    "loaded")."""
    counts = {name: 0 for name in HEAVY_PROCESSES}
    for _, (_, name) in process_table().items():
        if name.casefold() in counts:
            counts[name.casefold()] += 1
    try:
        query = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", LOAD_QUERY],
                               capture_output=True, text=True, timeout=120)
        counts.update(json.loads(query.stdout))
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        counts["classification_error"] = repr(error)
    return counts


def load_class(load: dict) -> str:
    if "test_chrome" not in load:
        return "unknown"
    return "loaded" if load["test_chrome"] or load["test_node"] or load["rustc.exe"] else "normal"


def shell_log_events(path: Path) -> list[tuple[int, str]]:
    if not path.is_file():
        return []
    events = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stamp, _, rest = line.partition(" ")
        if stamp.isdigit():
            events.append((int(stamp), rest))
    return events


def find_tray(pid: int):
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    found = []

    def visit(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid:
            name = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, name, 256)
            if name.value == "TesseractShellTray":
                found.append(hwnd)
        return True
    user32.EnumWindows(callback_type(visit), 0)
    return found[0] if found else None


def close_dialogs(pid: int) -> int:
    """WM_CLOSE to the shell's visible native boxes (#32770), e.g. the pre-hello dialog of a
    sidecar that missed the 10 s hello window; the shell then exits by itself (exit 1)."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    found = []

    def visit(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        name = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, name, 64)
        if owner.value == pid and name.value == "#32770" and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True
    user32.EnumWindows(callback_type(visit), 0)
    return sum(1 for hwnd in found if user32.PostMessageW(hwnd, 0x10, 0, 0))


def post_tray_quit(pid: int) -> bool:
    """The tray menu's 「退出」: WM_COMMAND 2 to the shell's tray window (ui/test/e2e/native.py)."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    tray = find_tray(pid)
    return bool(tray) and bool(user32.PostMessageW(tray, 0x111, 2, 0))


def filetime_ms(value: int) -> int:
    return (value - 116444736000000000) // 10000


def isolation_problems(sandbox: dict) -> list[str]:
    """T-P2-33 smoke rules, checked before anything starts; any problem means: do not start."""
    import yaml
    problems = []
    env = sandbox["env"]
    temp_root = Path(tempfile.gettempdir()).resolve()
    for key in ("TESSERACT_CONFIG_DIR", "TESSERACT_DATA_DIR", "APPDATA", "LOCALAPPDATA", "TEMP"):
        path = Path(env.get(key, "")).resolve()
        if temp_root not in path.parents:
            problems.append(f"{key} is not under the temporary directory: {path}")
    for live in (os.environ.get("APPDATA"), os.environ.get("LOCALAPPDATA")):
        if live and any(Path(env[k]).resolve().is_relative_to(Path(live, "Tesseract").resolve())
                        for k in ("TESSERACT_CONFIG_DIR", "TESSERACT_DATA_DIR")):
            problems.append("a scope directory lies in the live profile")
    if not env.get("TESSERACT_INSTANCE_SCOPE", "").strip():
        problems.append("no custom scope label")
    if "TESSERACT_ISOLATED_AUDIT_MANIFEST" in env:
        problems.append("an isolated-audit manifest is set")
    sources = yaml.safe_load((Path(sandbox["config_dir"]) / "sources.yaml").read_text(encoding="utf-8"))
    for name in ("github_repos", "telegram_channels", "subscription_urls", "local_files"):
        if sources.get(name):
            problems.append(f"sources.yaml {name} is not empty")
    for marker in ("tier3_source_portfolio_defaults_v1", "tier3_source_portfolio_defaults_v2",
                   "tier3_source_default_delivery_v1"):
        if (sources.get("migrations") or {}).get(marker) is not True:
            problems.append(f"sources.yaml lacks the migration marker {marker}")
    if (sources.get("github_discovery") or {}).get("enabled") is not False:
        problems.append("GitHub discovery is not off")
    config = yaml.safe_load((Path(sandbox["config_dir"]) / "config.yaml").read_text(encoding="utf-8"))
    off = {"tiers.tier3": config["tiers"]["tier3"]["enabled"], "geo_update": config["geo_update"]["enabled"],
           "system_proxy": config["client"]["system_proxy"]["enabled"], "killswitch": config["killswitch"]["enabled"],
           "egress_proxy": config["egress_proxy"]["enabled"], "dns": config["client"]["dns"]["enable"],
           "auto_exit_lookup": config["ui"]["overview_auto_exit_lookup"]}
    problems += [f"{name} is on" for name, value in off.items() if value]
    for name, port in sandbox["ports"].items():
        if not 20000 <= int(port) < 40000:
            problems.append(f"{name} port {port} is not a free high port")
    return problems


def smoke(args) -> int:
    app = Path(args.app).resolve() if args.app else APP
    sys.path.insert(0, str(REPO))
    from tests.test_bridge_p2_33 import host_facts, prepare_sandbox, process_table, readback_problems, sandbox_readback
    evidence = (Path(args.evidence).resolve() if args.evidence else default_evidence()) / f"smoke-{args.label}"
    evidence.mkdir(parents=True, exist_ok=True)
    for name in ("Tesseract.exe", "tesseract-runtime.exe"):
        gate((app / name).is_file(), f"build first: missing {app / name}")
    runtime, shell = app / "tesseract-runtime.exe", app / "Tesseract.exe"
    temp_root = Path(tempfile.mkdtemp(prefix="tesseract-p6-preview-"))
    result: dict = {"label": args.label, "started_utc": utc(), "sandbox_root": str(temp_root), "runs": [],
                    "app": str(app),
                    "dist": json.loads((app.parent / "preview-build.json").read_text(encoding="utf-8"))
                    if (app.parent / "preview-build.json").is_file() else None}
    failures: list[str] = []
    gate(not dist_processes(process_table, app), f"a process is already running from {app}")
    before = host_facts()
    result["host_before"] = before
    gate("error" not in before["firewall"], f"no firewall baseline: {before['firewall']}")
    try:
        # 1. --validator-helper-smoke 16 (plan §9.9), in its own sandbox.
        helper = prepare_sandbox(temp_root / "helper", frozen=True)
        helper["env"]["TESSERACT_INSTANCE_SCOPE"] = "p6-preview-helper-smoke"
        problems = isolation_problems(helper)
        gate(not problems, f"helper smoke not started: {problems}")
        if args.skip_helper:
            result["helper_smoke"] = "skipped"
        else:
            started = time.monotonic()
            process = subprocess.run([str(runtime), "--validator-helper-smoke", "16"], env=helper["env"],
                                     cwd=helper["temp"], capture_output=True, text=True, timeout=240,
                                     encoding="utf-8", errors="replace")
            marker = "Tesseract validator helper smoke test passed"
            result["helper_smoke"] = {"exit": process.returncode, "marker": marker in process.stdout,
                                      "seconds": round(time.monotonic() - started, 1),
                                      "stdout_tail": process.stdout.splitlines()[-5:],
                                      "stderr_tail": process.stderr.splitlines()[-10:]}
            if process.returncode != 0 or marker not in process.stdout:
                failures.append(f"helper smoke: exit {process.returncode}, marker {marker in process.stdout}")
            deadline = time.monotonic() + 20
            while dist_processes(process_table, app) and time.monotonic() < deadline:
                time.sleep(0.5)
            left = dist_processes(process_table, app)
            result["helper_smoke"]["residual"] = left
            if left:
                failures.append(f"helper smoke left processes: {left}")
        # 2. Shell cold starts with the frozen sidecar.
        for index in range(1, args.runs + 1):
            run_result = shell_cold_start(index, temp_root, shell, runtime, evidence, prepare_sandbox,
                                          sandbox_readback, readback_problems, process_table)
            result["runs"].append(run_result)
            failures += [f"run {index}: {problem}" for problem in run_result["failures"]]
            time.sleep(3)
    finally:
        after = host_facts()
        result["host_after"] = after
        if before != after:
            failures.append(f"host facts changed: {before} -> {after}")
        left = dist_processes(process_table, app)
        if left:
            failures.append(f"residual dist processes: {left}")
        result["residual"] = left
        result["failures"] = failures
        result["finished_utc"] = utc()
        (evidence / "smoke-result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str) + "\n",
                                                    encoding="utf-8")
        for _ in range(5):
            shutil.rmtree(temp_root, ignore_errors=True)
            if not temp_root.exists():
                break
            time.sleep(2)
    summary = [{key: run_result.get(key) for key in ("run", "load_class", "hello_ms", "reveal", "reveal_ms",
                                                     "first_state_before_timeout", "exit_code", "passed")}
               for run_result in result["runs"]]
    print(json.dumps({"failures": failures, "helper_smoke": result.get("helper_smoke"), "runs": summary},
                     ensure_ascii=False, indent=1, default=str))
    return 1 if failures else 0


def shell_cold_start(index, temp_root, shell, runtime, evidence, prepare_sandbox, sandbox_readback,
                     readback_problems, process_table) -> dict:
    out = evidence / f"run{index}"
    out.mkdir(parents=True, exist_ok=True)
    sandbox = prepare_sandbox(temp_root / f"run{index}", frozen=True)
    env = dict(sandbox["env"], TESSERACT_INSTANCE_SCOPE="p6-preview-smoke")
    sandbox["env"] = env
    result: dict = {"run": index, "failures": [], "ports": sandbox["ports"]}
    problems = isolation_problems(sandbox)
    if problems:
        result["failures"] = [f"not started (isolation): {problems}"]
        return result
    load = heavy_load(process_table)
    result["load"] = load
    result["load_class"] = load_class(load)
    data = Path(sandbox["data_dir"])
    shell_log = data / "logs" / "shell.log"
    ready, inventory = out / "root.json", out / "process-inventory.json"
    for stale in (ready, inventory):
        stale.unlink(missing_ok=True)
    watcher = subprocess.Popen([str(PYTHON), "-B", str(REPO / "shell" / "app" / "process_watch.py"), "--output",
                                str(inventory), "--ready", str(ready), "--", str(shell), "--sidecar", str(runtime)],
                               env=env, cwd=sandbox["temp"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 30
        while not ready.is_file() and watcher.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if not ready.is_file():
            result["failures"].append("the shell did not start under process_watch")
            return result
        root = json.loads(ready.read_text(encoding="utf-8"))
        pid = int(root["pid"])
        created = filetime_ms(int(root["created_filetime"])) if root.get("created_filetime") else None
        result["shell_pid"] = pid
        deadline = time.monotonic() + 60
        events = []
        while time.monotonic() < deadline:
            events = shell_log_events(shell_log)
            names = [text.split(" ")[0] for _, text in events]
            if "sidecar_ready" in names and ({"window_revealed", "window_reveal_timeout"} & set(names)):
                break
            if "sidecar_prehello_dialog" in names or watcher.poll() is not None:
                break
            time.sleep(0.05)
        stamp = lambda code: next((t for t, text in events if text.split(" ")[0] == code), None)
        reveal = next((text for _, text in events if text.startswith(("window_revealed", "window_reveal_timeout"))), None)
        hello_at, reveal_at, start_at = stamp("sidecar_ready"), (stamp("window_revealed") or stamp("window_reveal_timeout")), stamp("aumid_set")
        result.update(reveal=reveal, created_ms=created,
                      hello_ms=(hello_at - created) if hello_at and created else None,
                      hello_after_shell_start_ms=(hello_at - start_at) if hello_at and start_at else None,
                      reveal_ms=(reveal_at - created) if reveal_at and created else None,
                      window_shown_ms=(stamp("window_shown") - created) if stamp("window_shown") and created else None,
                      first_state_before_timeout=bool(reveal and reveal.startswith("window_revealed reason=ready")))
        if hello_at is None:
            result["failures"].append("no hello (sidecar_ready) in shell.log")
        if not result["first_state_before_timeout"]:
            result["failures"].append(f"window not revealed by host_ready before the 1.5 s timeout: {reveal}")
        if any(text.startswith("sidecar_prehello_dialog") for _, text in events):
            # The shell's pre-hello box waits for a person; close it, the shell exits 1.
            time.sleep(1.0)
            result["prehello_dialog_closed"] = close_dialogs(pid)
            result["failures"].append("pre-hello dialog: " + " ".join(t for _, t in events if t.startswith(("hello_timeout", "sidecar_start_failed", "sidecar_prehello_dialog"))))
        else:
            # Let the page settle (host_ready, the first rt seed), then the tray menu's 「退出」.
            time.sleep(3.0)
            settled = shell_log_events(shell_log)
            host_ready = next((t for t, text in settled if text.split(" ")[0] == "host_ready"), None)
            seed = next((text for _, text in settled if text.startswith("rt_seed_done")), None)
            result.update(host_ready_ms=(host_ready - created) if host_ready and created else None, rt_seed=seed)
            result["tray_quit_posted"] = post_tray_quit(pid)
            if not result["tray_quit_posted"]:
                result["failures"].append("tray window not found for the quit command")
        try:
            watcher.wait(timeout=150)
        except subprocess.TimeoutExpired:
            result["failures"].append("process_watch did not finish (shell still running?)")
            result["late_dialogs_closed"] = close_dialogs(pid)
            post_tray_quit(pid)
            try:
                watcher.wait(timeout=60)
            except subprocess.TimeoutExpired:
                pass
    finally:
        for name in ("bridge.log", "monitor.log"):
            if (data / name).is_file():
                shutil.copy2(data / name, out / name)
        if watcher.poll() is None:
            # Never kill the shell or the sidecar: only the harness's own watcher.
            watcher.kill()
        watch_output = watcher.stdout.read().decode("utf-8", "replace") if watcher.stdout else ""
        (out / "watch-output.log").write_text(watch_output, encoding="utf-8")
        if shell_log.is_file():
            shutil.copy2(shell_log, out / "shell.log")
    events = shell_log_events(shell_log)
    codes = [text.split(" ")[0] for _, text in events]
    exit_lines = [text for text in codes if text.startswith("sidecar_exit_")]
    result["sidecar_exit"] = exit_lines
    if exit_lines != ["sidecar_exit_0"]:
        result["failures"].append(f"sidecar exit {exit_lines}, expected one sidecar_exit_0")
    if "bye_missing" in codes:
        result["failures"].append("bye_missing")
    if inventory.is_file():
        audit = json.loads(inventory.read_text(encoding="utf-8"))
        result["process_audit_passed"] = audit.get("passed")
        result["exit_code"] = audit.get("root_exit_code", audit.get("exit_code"))
        if not audit.get("passed"):
            result["failures"].append(f"process audit failed: {audit.get('error') or audit.get('residue_class')}")
    else:
        result["failures"].append("no process inventory")
    check = sandbox_readback({k: str(v) if isinstance(v, Path) else v for k, v in sandbox.items() if k != "env"})
    result["readback"] = check
    result["failures"] += [f"sandbox: {p}" for p in readback_problems(check)]
    result["passed"] = not result["failures"]
    (out / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    log(f"run {index}: hello {result.get('hello_ms')} ms, reveal {result.get('reveal')}, passed={result['passed']}")
    return result


def load(_args) -> int:
    sys.path.insert(0, str(REPO))
    from tests.test_bridge_p2_33 import process_table
    load = heavy_load(process_table)
    print(json.dumps({"utc": utc(), "class": load_class(load), **load}))
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="replace")  # evidence paths may not fit a legacy console code page
    parser = argparse.ArgumentParser(prog="build_shell_preview.py")
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="build and gate dist-shell/Tesseract (or --out)")
    b.add_argument("--clean", action="store_true", help="delete .build/p6-ui, .build/p6-target and .build/p6-pyi first")
    b.add_argument("--evidence")
    b.add_argument("--out", help="assemble into this directory instead of dist-shell/Tesseract; the side files "
                                 "(preview-build.json, launcher, usage notes) go to its parent")
    b.add_argument("--wait-minutes", type=int, default=0,
                   help="when programs run from the target, check again every 2 minutes for up to this long "
                        "(never ending them); default 0: stop at once, the new build stays in the staging directory")
    b.add_argument("--reuse-shell", action="store_true",
                   help="keep the built web UI and shell (gated again); rebuild the sidecar and reassemble")
    s = sub.add_parser("smoke", help="isolated helper smoke and shell cold starts")
    s.add_argument("--runs", type=int, default=3)
    s.add_argument("--label", default="run")
    s.add_argument("--evidence")
    s.add_argument("--skip-helper", action="store_true")
    s.add_argument("--app", help="the assembled directory to smoke (default dist-shell/Tesseract)")
    sub.add_parser("load", help="print the counts of heavy test processes")
    args = parser.parse_args(argv)
    try:
        return {"build": build, "smoke": smoke, "load": load}[args.command](args)
    except GateError as error:
        log(f"FAILED: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
