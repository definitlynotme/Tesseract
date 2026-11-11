"""Shell-flavor license, SBOM and source inputs (T-P6-02, migration plan §9.5).

The Shell flavor ships the Tauri shell (``Tesseract.exe``, Rust) next to the
Python sidecar (``tesseract-runtime.exe``) and no Qt/PySide6. This module holds
everything that is specific to that flavor so that the Qt path of
``collect_release_licenses.py`` stays unchanged until P8:

* the Rust crate inventory from ``cargo about generate --format json``, checked
  against ``shell/Cargo.lock`` (source + checksum) and against the license
  policy in ``shell/deny.toml`` (the same allow list ``cargo deny`` enforces);
* CycloneDX components for the Rust crates, the self-hosted interface fonts,
  the production npm packages of ``ui/package-lock.json`` and the statically
  linked WebView2 loader, plus their license directories;
* the MPL-2.0 crate source archives taken from ``cargo vendor`` and the Shell
  third-party source bundle (no pyside-setup / qtbase / qtsvg).

Nothing here downloads anything; every input is a local, pinned file.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import tomllib
import zipfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

CRATES_IO_SOURCES = frozenset(
    {
        "registry+https://github.com/rust-lang/crates.io-index",
        "sparse+https://index.crates.io/",
    }
)
# First-party workspace crates (shell/Cargo.toml). They are Tesseract itself
# (GPL-3.0-only, LICENSE) and are not listed as third-party components.
FIRST_PARTY_CRATES = frozenset({"tesseract-shell", "bridge_host", "headless_shell"})
SHELL_OWNER_ID = "tesseract-shell"
RUST_LICENSE_ROOT = "rust-crates"
SUBSTITUTE_MARKER = "tesseract_substitute_generator"
# Release asset that carries the MPL-2.0 crate sources (run_shell_gate.py and
# THIRD_PARTY_NOTICES.shell.md use the same name).
SHELL_SOURCE_BUNDLE_NAME = "Tesseract-{version}-third-party-sources.zip"
# Dependency roles, same vocabulary as the Python side of the SBOM: "runtime"
# crates are compiled into Tesseract.exe, "build" crates only run while it is
# built (build scripts, proc macros and everything only they depend on).
RUNTIME_ROLE = "runtime"
BUILD_ROLE = "build"

# Interface fonts: mirror of FONT_PACKAGES in ui/build/fonts.mjs (a unit test
# keeps the two lists equal). The woff2 files are embedded in Tesseract.exe.
FONT_PACKAGES = (
    {
        "id": "instrument-sans",
        "family": "Instrument Sans",
        "package": "@fontsource-variable/instrument-sans",
        "files": (
            "instrument-sans-latin-ext-wght-normal.woff2",
            "instrument-sans-latin-wght-normal.woff2",
        ),
    },
    {
        "id": "geist-mono",
        "family": "Geist Mono",
        "package": "@fontsource/geist-mono",
        "files": (
            "geist-mono-latin-ext-400-normal.woff2",
            "geist-mono-latin-400-normal.woff2",
            "geist-mono-latin-ext-500-normal.woff2",
            "geist-mono-latin-500-normal.woff2",
        ),
    },
)
# Local fonts: mirror of the FONT_PACKAGES entries with `local: true` in
# ui/build/fonts.mjs. They have no npm package; the woff2 and its OFL text are
# source files in ui/fonts/ (tracked in git), embedded in Tesseract.exe.
LOCAL_FONTS = (
    {
        "id": "tesseract-kai",
        "family": "Tesseract Kai",
        "upstream": "LXGW WenKai GB Screen",
        "version": "1.530",
        "url": "https://github.com/lxgw/LxgwWenKai-Screen",
        "files": ("tesseract-kai-zh.woff2",),
    },
)
OFL_MARKERS = ("SIL OPEN FONT LICENSE", "Version 1.1")

# The WebView2 loader is linked statically into Tesseract.exe from the
# Microsoft WebView2 SDK copy vendored by webview2-com-sys. Each reviewed crate
# version maps to the SDK version and the x64 static library hash.
WEBVIEW2_LOADER_PROVENANCE = {
    "0.38.2": {
        "sdk_version": "1.0.3650.58",
        "static_lib": "x64/WebView2LoaderStatic.lib",
        "static_lib_sha256": (
            "0659b741bde6348d4c4a6ec4ceb9af50e3d0048ed9cd3c8659bccbb61fde55ee"
        ),
    }
}
WEBVIEW2_NOTICE = (
    "Microsoft Edge WebView2\n"
    "=======================\n\n"
    "Tesseract.exe statically links the WebView2 loader "
    "(WebView2LoaderStatic.lib) from the Microsoft WebView2 SDK "
    "{sdk_version}, as vendored by the webview2-com-sys {crate_version} crate "
    "(x64 library SHA-256 {static_lib_sha256}).\n"
    "SDK license: https://www.nuget.org/packages/Microsoft.Web.WebView2/"
    "{sdk_version}/license\n\n"
    "The Windows installer may run Microsoft's Evergreen WebView2 Runtime "
    "bootstrapper to install the WebView2 Runtime when it is missing. The "
    "bootstrapper only runs from the installer's temporary directory ({{tmp}}) "
    "and is never copied into the application directory; Microsoft "
    "distributes it under its WebView2 Runtime distribution terms: "
    "https://developer.microsoft.com/microsoft-edge/webview2/\n"
    "The WebView2 Runtime itself is installed and serviced by Microsoft and is "
    "not part of the Tesseract package.\n"
)


# --------------------------------------------------------------------------
# SPDX expressions
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class _Leaf:
    license_id: str
    exception: str | None = None

    def render(self) -> str:
        if self.exception:
            return f"{self.license_id} WITH {self.exception}"
        return self.license_id


@dataclass(frozen=True)
class _Node:
    operator: str  # "AND" | "OR"
    operands: tuple[object, ...]

    def render(self) -> str:
        parts = []
        for operand in self.operands:
            text = operand.render()  # type: ignore[attr-defined]
            if isinstance(operand, _Node) and operand.operator != self.operator:
                text = f"({text})"
            parts.append(text)
        return f" {self.operator} ".join(parts)


_SPDX_TOKEN = re.compile(r"\(|\)|[^\s()]+")


def parse_spdx_expression(expression: str) -> _Leaf | _Node:
    """Parse an SPDX license expression (and the legacy ``A/B`` form)."""
    text = str(expression or "").strip()
    if not text:
        raise ValueError("empty SPDX license expression")
    tokens = _SPDX_TOKEN.findall(text.replace("/", " OR "))
    position = 0

    def peek() -> str | None:
        return tokens[position] if position < len(tokens) else None

    def take() -> str:
        nonlocal position
        if position >= len(tokens):
            raise ValueError(f"truncated SPDX expression: {expression!r}")
        token = tokens[position]
        position += 1
        return token

    def parse_or() -> _Leaf | _Node:
        operands = [parse_and()]
        while (peek() or "").upper() == "OR":
            take()
            operands.append(parse_and())
        return operands[0] if len(operands) == 1 else _Node("OR", tuple(operands))

    def parse_and() -> _Leaf | _Node:
        operands = [parse_atom()]
        while (peek() or "").upper() == "AND":
            take()
            operands.append(parse_atom())
        return operands[0] if len(operands) == 1 else _Node("AND", tuple(operands))

    def parse_atom() -> _Leaf | _Node:
        token = take()
        if token == "(":
            inner = parse_or()
            if take() != ")":
                raise ValueError(f"unbalanced SPDX expression: {expression!r}")
            return inner
        if token == ")" or token.upper() in {"AND", "OR", "WITH"}:
            raise ValueError(f"malformed SPDX expression: {expression!r}")
        if not re.fullmatch(r"[A-Za-z0-9.+-]+(?::[A-Za-z0-9.+-]+)?", token):
            raise ValueError(f"invalid SPDX identifier {token!r} in {expression!r}")
        exception = None
        if (peek() or "").upper() == "WITH":
            take()
            exception = take()
        return _Leaf(token, exception)

    tree = parse_or()
    if position != len(tokens):
        raise ValueError(f"trailing tokens in SPDX expression: {expression!r}")
    return tree


def normalize_spdx_expression(expression: str) -> str:
    return parse_spdx_expression(expression).render()


def spdx_expression_satisfied(expression: str, allowed: Iterable[str]) -> bool:
    allowed_set = frozenset(allowed)

    def evaluate(node: _Leaf | _Node) -> bool:
        if isinstance(node, _Leaf):
            return node.render() in allowed_set
        results = [evaluate(operand) for operand in node.operands]  # type: ignore[arg-type]
        return any(results) if node.operator == "OR" else all(results)

    return evaluate(parse_spdx_expression(expression))


def spdx_license_ids(expression: str) -> frozenset[str]:
    found: set[str] = set()

    def visit(node: _Leaf | _Node) -> None:
        if isinstance(node, _Leaf):
            found.add(node.license_id)
        else:
            for operand in node.operands:
                visit(operand)  # type: ignore[arg-type]

    visit(parse_spdx_expression(expression))
    return frozenset(found)


# --------------------------------------------------------------------------
# License policy (single source: shell/deny.toml)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class LicensePolicy:
    allow: frozenset[str]
    exceptions: Mapping[str, frozenset[str]] = field(default_factory=dict)
    clarify: Mapping[str, str] = field(default_factory=dict)

    def allowed_for(self, crate_name: str) -> frozenset[str]:
        return self.allow | self.exceptions.get(crate_name, frozenset())

    def check(self, crate_name: str, declared: str | None) -> str:
        """Return the normalized governing expression or raise."""
        expression = self.clarify.get(crate_name) or declared
        if not expression or expression in {"Unknown", "Ignore"}:
            raise RuntimeError(
                f"Rust crate {crate_name!r} has no license expression"
            )
        normalized = normalize_spdx_expression(expression)
        if not spdx_expression_satisfied(normalized, self.allowed_for(crate_name)):
            raise RuntimeError(
                f"Rust crate {crate_name!r} license {normalized!r} is not "
                "satisfied by the shell/deny.toml allow list"
            )
        return normalized


def _spec_name(spec: str) -> str:
    return str(spec).split("@", 1)[0].strip()


def load_license_policy(deny_config: Path) -> LicensePolicy:
    payload = tomllib.loads(deny_config.read_text(encoding="utf-8"))
    licenses = payload.get("licenses") or {}
    allow = frozenset(str(item) for item in licenses.get("allow") or ())
    if not allow:
        raise RuntimeError(f"{deny_config} has an empty [licenses].allow list")
    exceptions: dict[str, frozenset[str]] = {}
    for entry in licenses.get("exceptions") or ():
        name = _spec_name(entry.get("crate") or entry.get("name") or "")
        exceptions[name] = frozenset(str(item) for item in entry.get("allow") or ())
    clarify: dict[str, str] = {}
    for entry in licenses.get("clarify") or ():
        name = _spec_name(entry.get("crate") or entry.get("name") or "")
        clarify[name] = str(entry.get("expression") or "")
    return LicensePolicy(allow=allow, exceptions=exceptions, clarify=clarify)


# --------------------------------------------------------------------------
# Cargo.lock and the cargo-about inventory
# --------------------------------------------------------------------------

def load_cargo_lock(cargo_lock: Path) -> dict[tuple[str, str], dict[str, str]]:
    payload = tomllib.loads(cargo_lock.read_text(encoding="utf-8"))
    packages: dict[tuple[str, str], dict[str, str]] = {}
    for package in payload.get("package") or ():
        key = (str(package["name"]), str(package["version"]))
        packages[key] = {
            "source": str(package.get("source") or ""),
            "checksum": str(package.get("checksum") or ""),
        }
    return packages


@dataclass
class RustCrate:
    name: str
    version: str
    source: str
    checksum: str
    expression: str
    repository: str
    texts: list[tuple[str, str, str]]  # (license id, license name, text)
    role: str = RUNTIME_ROLE
    # License ids whose text is cargo-about's canonical SPDX text (the crate
    # ships no file cargo-about could match, so no copyright line).
    canonical_text_ids: tuple[str, ...] = ()

    @property
    def directory_name(self) -> str:
        return f"{self.name}-{self.version}"

    @property
    def purl(self) -> str:
        return f"pkg:cargo/{self.name}@{self.version}"

    @property
    def license_path(self) -> str:
        return f"third-party-licenses/{RUST_LICENSE_ROOT}/{self.directory_name}"


def load_dependency_roles(
    metadata_json: Path, *, root_name: str = SHELL_OWNER_ID
) -> dict[tuple[str, str], str]:
    """Upper bound of the shell graph from ``cargo metadata`` (with roles).

    ``metadata_json`` is ``cargo metadata --format-version 1 --frozen
    --filter-platform x86_64-pc-windows-msvc --manifest-path shell/app/Cargo.toml``
    output. Walking from ``root_name`` over non-dev edges gives every crate the
    shell *could* pull in. A crate is ``runtime`` when a chain of normal edges
    from the root reaches it without passing through a proc-macro crate; every
    other reachable crate only runs at build time.

    ``cargo metadata`` resolves features for the whole workspace at once
    (``headless_shell`` and ``bridge_host`` turn on extra tokio / serde_with
    features), so this set is a superset of what ``cargo build -p
    tesseract-shell`` compiles. The exact set comes from ``cargo tree``, see
    :func:`load_shell_dependency_roles`; this function bounds it.
    """
    payload = json.loads(metadata_json.read_text(encoding="utf-8"))
    packages = {str(item["id"]): item for item in payload.get("packages") or ()}
    resolve = payload.get("resolve") or {}
    nodes = {str(item["id"]): item for item in resolve.get("nodes") or ()}
    if not packages or not nodes:
        raise RuntimeError(f"{metadata_json} is not `cargo metadata` output with a resolve graph")
    roots = [
        package_id
        for package_id in payload.get("workspace_members") or ()
        if packages.get(package_id, {}).get("name") == root_name
    ]
    if len(roots) != 1:
        raise RuntimeError(f"cargo metadata has no single workspace package {root_name!r}")
    root = roots[0]

    def is_proc_macro(package_id: str) -> bool:
        return any(
            "proc-macro" in (target.get("kind") or ())
            for target in packages[package_id].get("targets") or ()
        )

    def walk(follow) -> set[str]:
        seen = {root}
        queue = [root]
        while queue:
            current = queue.pop()
            if current != root and not follow(current, None):
                continue
            for dep in nodes[current].get("deps") or ():
                kinds = [kind.get("kind") for kind in dep.get("dep_kinds") or ()]
                target = str(dep["pkg"])
                if target not in seen and follow(current, kinds):
                    seen.add(target)
                    queue.append(target)
        return seen

    reachable = walk(lambda _pid, kinds: kinds is None or any(k != "dev" for k in kinds))
    linked = walk(
        lambda pid, kinds: (not is_proc_macro(pid))
        if kinds is None
        else any(k is None for k in kinds)
    )
    roles: dict[tuple[str, str], str] = {}
    for package_id in sorted(reachable):
        package = packages[package_id]
        name = str(package["name"])
        if package.get("source") is None:
            if name not in FIRST_PARTY_CRATES:
                raise RuntimeError(f"unexpected path/workspace crate in the shell graph: {name}")
            continue
        role = (
            RUNTIME_ROLE
            if package_id in linked and not is_proc_macro(package_id)
            else BUILD_ROLE
        )
        roles[(name, str(package["version"]))] = role
    if not roles:
        raise RuntimeError(f"cargo metadata lists no third-party crates under {root_name}")
    return roles


# `cargo tree` runs whose outputs describe the shell graph exactly as
# `cargo build -p tesseract-shell` resolves it (per-package features):
#   cargo tree --frozen --manifest-path shell/app/Cargo.toml -p tesseract-shell
#       --target x86_64-pc-windows-msvc --prefix depth -f {p} -e <edges>
#   all (--cargo-tree):           -e normal,build          (compiled for the build)
#   linked (--cargo-tree-linked): -e normal,no-proc-macro  (inside Tesseract.exe)
_CARGO_TREE_LINE = re.compile(r"^(\d+)(\S+) v(\S+)(.*)$")


def parse_cargo_tree(tree_txt: Path, *, root_name: str = SHELL_OWNER_ID) -> set[tuple[str, str]]:
    """Third-party ``(name, version)`` set of a ``--prefix depth -f {p}`` tree."""
    crates: set[tuple[str, str]] = set()
    roots: list[str] = []
    for number, line in enumerate(tree_txt.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        match = _CARGO_TREE_LINE.match(line.strip())
        if match is None:
            raise RuntimeError(f"{tree_txt}:{number}: not `cargo tree --prefix depth -f {{p}}` output")
        depth, name, version, rest = match.groups()
        rest = rest.strip()
        for marker in ("(*)", "(proc-macro)"):
            if rest.endswith(marker):
                rest = rest[: -len(marker)].strip()
        if depth == "0":
            roots.append(name)
        if name in FIRST_PARTY_CRATES:
            if not rest:
                raise RuntimeError(f"{tree_txt}:{number}: {name} is not a path crate")
            continue
        if rest:
            # crates.io packages carry no annotation; anything else names a
            # path, git or alternative-registry source.
            raise RuntimeError(
                f"{tree_txt}:{number}: Rust crate {name} {version} does not come "
                f"from crates.io ({rest})"
            )
        crates.add((name, version))
    if roots != [root_name]:
        raise RuntimeError(f"{tree_txt} is not the cargo tree of {root_name} (roots: {roots})")
    if not crates:
        raise RuntimeError(f"{tree_txt} lists no third-party crates")
    return crates


def load_shell_dependency_roles(
    *, cargo_tree: Path, cargo_tree_linked: Path, cargo_metadata_json: Path
) -> dict[tuple[str, str], str]:
    """Third-party crates of the shell build and their dependency role.

    The crate set is ``cargo tree -e normal,build`` (exactly what the release
    build compiles); ``runtime`` crates are the ones ``cargo tree -e
    normal,no-proc-macro`` reaches (linked into Tesseract.exe), the rest are
    ``build`` (build scripts, proc macros and their dependencies). Both trees
    must lie inside the ``cargo metadata`` graph (:func:`load_dependency_roles`).
    """
    every = parse_cargo_tree(cargo_tree)
    linked = parse_cargo_tree(cargo_tree_linked)
    if linked - every:
        raise RuntimeError(
            "the linked cargo tree lists crates the full tree does not: "
            + ", ".join(f"{n} {v}" for n, v in sorted(linked - every))
        )
    outside = every - set(load_dependency_roles(cargo_metadata_json))
    if outside:
        raise RuntimeError(
            "cargo tree lists crates outside the cargo metadata graph: "
            + ", ".join(f"{n} {v}" for n, v in sorted(outside))
        )
    return {key: RUNTIME_ROLE if key in linked else BUILD_ROLE for key in sorted(every)}


def _package_key(package: Mapping[str, object]) -> str:
    package_id = str(package.get("id") or "")
    if package_id:
        return package_id
    return f"{package.get('name')}@{package.get('version')}"


def load_rust_inventory(
    about_json: Path,
    *,
    cargo_lock: Path,
    policy: LicensePolicy,
    allow_substitute: bool = False,
    dependency_roles: Mapping[tuple[str, str], str] | None = None,
) -> tuple[list[RustCrate], str]:
    """Load ``cargo about generate --format json`` output.

    Returns the third-party crates (sorted) and the inventory generator label.
    With ``dependency_roles`` (:func:`load_shell_dependency_roles`) the
    inventory must list exactly the third-party crates of the shell build graph,
    in both directions (cargo-about exits 0 even when it drops a crate it cannot
    resolve), and each crate carries its runtime/build role.
    """
    raw = about_json.read_bytes()
    payload = json.loads(raw.decode("utf-8"))
    generator = "cargo-about"
    if SUBSTITUTE_MARKER in payload:
        if not allow_substitute:
            raise RuntimeError(
                "the Rust license inventory was not produced by cargo-about "
                f"({payload[SUBSTITUTE_MARKER]!r}); release builds require "
                "`cargo about generate --format json`"
            )
        generator = f"substitute:{payload[SUBSTITUTE_MARKER]}"
    if not isinstance(payload.get("crates"), list) or not isinstance(
        payload.get("licenses"), list
    ):
        raise RuntimeError(f"{about_json} is not cargo-about JSON output")
    locked = load_cargo_lock(cargo_lock)

    texts_by_package: dict[str, list[tuple[str, str, str]]] = {}
    canonical_by_package: dict[str, set[str]] = {}
    for license_entry in payload["licenses"]:
        license_id = str(license_entry.get("id") or "").strip()
        text = str(license_entry.get("text") or "")
        if not license_id or not text.strip():
            raise RuntimeError("cargo-about reported a license without id or text")
        # cargo-about leaves source_path empty when it fell back to the
        # canonical SPDX text (generate.rs: no matching license file).
        canonical = not license_entry.get("source_path")
        for used_by in license_entry.get("used_by") or ():
            package = used_by.get("crate") or {}
            entry = (license_id, str(license_entry.get("name") or license_id), text)
            bucket = texts_by_package.setdefault(_package_key(package), [])
            if entry not in bucket:
                bucket.append(entry)
            if canonical:
                canonical_by_package.setdefault(_package_key(package), set()).add(license_id)

    crates: dict[tuple[str, str], RustCrate] = {}
    seen_keys: set[str] = set()
    for entry in payload["crates"]:
        package = entry.get("package") or {}
        name = str(package.get("name") or "")
        version = str(package.get("version") or "")
        key = _package_key(package)
        seen_keys.add(key)
        source = package.get("source")
        if source is None:
            if name not in FIRST_PARTY_CRATES:
                raise RuntimeError(
                    f"unexpected path/workspace crate in the shell graph: {name}"
                )
            continue
        source = str(source)
        if source not in CRATES_IO_SOURCES:
            raise RuntimeError(
                f"Rust crate {name} {version} comes from a non-crates.io "
                f"source: {source}"
            )
        lock_entry = locked.get((name, version))
        if lock_entry is None:
            raise RuntimeError(f"Rust crate {name} {version} is not in Cargo.lock")
        if lock_entry["source"] != source or not re.fullmatch(
            r"[0-9a-f]{64}", lock_entry["checksum"]
        ):
            raise RuntimeError(
                f"Rust crate {name} {version} has no crates.io checksum in Cargo.lock"
            )
        declared = package.get("license") or entry.get("license")
        expression = policy.check(name, str(declared) if declared else None)
        texts = texts_by_package.get(key) or []
        if not texts:
            raise RuntimeError(
                f"cargo-about reported no license text for Rust crate {name} {version}"
            )
        crates[(name, version)] = RustCrate(
            name=name,
            version=version,
            source=source,
            checksum=lock_entry["checksum"],
            expression=expression,
            repository=str(package.get("repository") or ""),
            texts=sorted(texts),
            canonical_text_ids=tuple(sorted(canonical_by_package.get(key, ()))),
        )
    stray = sorted(set(texts_by_package) - seen_keys)
    if stray:
        raise RuntimeError(
            "cargo-about license texts reference crates missing from its crate "
            "list: " + ", ".join(stray)
        )
    if not crates:
        raise RuntimeError("the Rust license inventory lists no third-party crates")
    if dependency_roles is not None:
        missing = sorted(set(dependency_roles) - set(crates))
        extra = sorted(set(crates) - set(dependency_roles))
        if missing or extra:
            raise RuntimeError(
                "the Rust license inventory does not match the cargo tree "
                "graph of the shell: missing "
                + (", ".join(f"{n} {v}" for n, v in missing) or "none")
                + "; not in the graph "
                + (", ".join(f"{n} {v}" for n, v in extra) or "none")
            )
        for key, crate in crates.items():
            crate.role = dependency_roles[key]
    return [crates[key] for key in sorted(crates)], generator


# --------------------------------------------------------------------------
# License directories and CycloneDX components
# --------------------------------------------------------------------------

def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_package_metadata(
    directory: Path,
    *,
    name: str,
    version: str,
    license_expression: str,
    homepage: str,
    files: list[str],
    dependency_role: str = "runtime",
    provenance: Mapping[str, object] | None = None,
) -> None:
    # Same document shape as collect_release_licenses._write_package_metadata.
    payload: dict[str, object] = {
        "name": name,
        "version": version,
        "license": license_expression,
        "dependency_role": dependency_role,
        "homepage": homepage,
        "copied_license_files": sorted(files),
    }
    if provenance:
        payload["provenance"] = dict(sorted(provenance.items()))
    _write_json(directory / "PACKAGE-METADATA.json", payload)


def _safe_file_label(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z._-]+", "-", value).strip("-") or "license"


def _properties(**values: str) -> list[dict[str, str]]:
    return [
        {"name": f"tesseract:{key.replace('_', '-')}", "value": value}
        for key, value in values.items()
    ]


def _sha512_hex_from_integrity(integrity: str) -> str | None:
    for item in str(integrity or "").split():
        if item.startswith("sha512-"):
            return base64.b64decode(item[len("sha512-") :]).hex()
    return None


def write_rust_licenses(
    crates: list[RustCrate], license_dir: Path, *, app_version: str
) -> list[dict[str, object]]:
    root = license_dir / RUST_LICENSE_ROOT
    root.mkdir(parents=True, exist_ok=True)
    source_bundle = SHELL_SOURCE_BUNDLE_NAME.format(version=app_version)
    mpl_entries: list[dict[str, str]] = []
    index_lines = [
        "# Rust crates compiled into or used to build Tesseract.exe",
        "",
        "Generated from `cargo about generate --format json` for the Tauri "
        "shell (`shell/app`). Each directory holds the license text(s) that "
        "apply to that crate. Crates marked *build only* run only while "
        "Tesseract.exe is built (build scripts, procedural macros); they are "
        "listed for completeness and are not part of the executable. Crates "
        "marked *canonical text* ship no license file cargo-about could match; "
        "their directory holds the standard SPDX text of that license.",
        "",
    ]
    components: list[dict[str, object]] = []
    for crate in crates:
        directory = root / crate.directory_name
        directory.mkdir()
        copied: list[str] = []
        for license_id, _license_name, text in crate.texts:
            target = directory / f"LICENSE-{_safe_file_label(license_id)}.txt"
            collision = 2
            while target.exists():
                target = directory / (
                    f"LICENSE-{_safe_file_label(license_id)}-{collision}.txt"
                )
                collision += 1
            target.write_text(text, encoding="utf-8")
            copied.append(target.name)
        is_mpl = "MPL-2.0" in spdx_license_ids(crate.expression)
        if is_mpl:
            (directory / "SOURCE-ACCESS.txt").write_text(
                f"{crate.name} {crate.version} is licensed under {crate.expression}.\n"
                "Its exact source (the crates.io package verified against "
                f"Cargo.lock checksum {crate.checksum}) is shipped in the same "
                f"GitHub Release as {source_bundle} "
                f"under sources/rust-{crate.name}-{crate.version}/.\n"
                f"Upstream: https://crates.io/crates/{crate.name}/{crate.version}\n",
                encoding="utf-8",
            )
            copied.append("SOURCE-ACCESS.txt")
            mpl_entries.append(
                {
                    "name": crate.name,
                    "version": crate.version,
                    "license": crate.expression,
                    "cargo_lock_checksum": crate.checksum,
                    "source_url": (
                        f"https://crates.io/api/v1/crates/{crate.name}/"
                        f"{crate.version}/download"
                    ),
                }
            )
        _write_package_metadata(
            directory,
            name=crate.name,
            version=crate.version,
            license_expression=crate.expression,
            homepage=crate.repository,
            files=copied,
            dependency_role=crate.role,
            provenance={
                "cargo_lock_checksum": crate.checksum,
                "source": crate.source,
            },
        )
        index_lines.append(
            f"- {crate.name} {crate.version}: {crate.expression} "
            f"(`{RUST_LICENSE_ROOT}/{crate.directory_name}/`)"
            + (" - build only" if crate.role == BUILD_ROLE else "")
            + (
                f" - canonical {'/'.join(crate.canonical_text_ids)} text"
                if crate.canonical_text_ids
                else ""
            )
        )
        component: dict[str, object] = {
            "type": "library",
            "name": crate.name,
            "version": crate.version,
            "bom-ref": crate.purl,
            "purl": crate.purl,
            "hashes": [{"alg": "SHA-256", "content": crate.checksum}],
            "licenses": [{"expression": crate.expression}],
            "scope": "required" if crate.role == RUNTIME_ROLE else "excluded",
            "properties": _properties(
                runtime_owner=SHELL_OWNER_ID,
                dependency_role=crate.role,
                license_path=crate.license_path,
                version_evidence="cargo-lock-checksum",
                selected_licenses=",".join(
                    sorted({license_id for license_id, _n, _t in crate.texts})
                ),
                license_text_source=(
                    "canonical:" + ",".join(crate.canonical_text_ids)
                    if crate.canonical_text_ids
                    else "crate-files"
                ),
            ),
        }
        if crate.repository:
            component["externalReferences"] = [
                {"type": "vcs", "url": crate.repository}
            ]
        components.append(component)
    (root / "INDEX.md").write_text("\n".join(index_lines) + "\n", encoding="utf-8")
    _write_json(
        root / "MPL-2.0-SOURCES.json",
        {"schema_version": 1, "crates": mpl_entries},
    )
    return components


def mpl_crates(crates: Iterable[RustCrate]) -> list[RustCrate]:
    return [crate for crate in crates if "MPL-2.0" in spdx_license_ids(crate.expression)]


def font_components(ui_root: Path, license_dir: Path) -> list[dict[str, object]]:
    lock = json.loads((ui_root / "package-lock.json").read_text(encoding="utf-8"))
    packages = lock.get("packages") or {}
    root = license_dir / "fonts"
    root.mkdir(parents=True, exist_ok=True)
    components: list[dict[str, object]] = []
    for font in FONT_PACKAGES:
        lock_entry = packages.get(f"node_modules/{font['package']}")
        if not lock_entry:
            raise RuntimeError(f"ui/package-lock.json does not pin {font['package']}")
        version = str(lock_entry.get("version") or "")
        if str(lock_entry.get("license") or "") != "OFL-1.1":
            raise RuntimeError(f"{font['package']} is not locked as OFL-1.1")
        sha512 = _sha512_hex_from_integrity(str(lock_entry.get("integrity") or ""))
        if not version or not sha512:
            raise RuntimeError(f"{font['package']} has no locked version/integrity")
        package_dir = ui_root / "node_modules" / Path(*font["package"].split("/"))
        installed = json.loads((package_dir / "package.json").read_text(encoding="utf-8"))
        if str(installed.get("version")) != version:
            raise RuntimeError(
                f"{font['package']} in node_modules is {installed.get('version')}, "
                f"locked {version}"
            )
        license_text = (package_dir / "LICENSE").read_text(encoding="utf-8")
        if any(marker not in license_text for marker in OFL_MARKERS):
            raise RuntimeError(f"{font['package']} LICENSE is not the OFL 1.1 text")
        file_hashes = []
        for filename in font["files"]:
            font_path = package_dir / "files" / filename
            if not font_path.is_file():
                raise FileNotFoundError(f"missing bundled font file: {font_path}")
            file_hashes.append(
                f"{filename}:{hashlib.sha256(font_path.read_bytes()).hexdigest()}"
            )
        directory = root / f"{font['id']}-{version}"
        directory.mkdir()
        (directory / "OFL-1.1.txt").write_text(license_text, encoding="utf-8")
        _write_package_metadata(
            directory,
            name=f"{font['family']} ({font['package']})",
            version=version,
            license_expression="OFL-1.1",
            homepage=f"https://fontsource.org/fonts/{font['id']}",
            files=["OFL-1.1.txt"],
            provenance={"npm_integrity_sha512": sha512, "bundled_files": file_hashes},
        )
        purl_name = font["package"].replace("@", "%40", 1)
        components.append(
            {
                "type": "library",
                "name": font["family"],
                "version": version,
                "bom-ref": f"pkg:npm/{purl_name}@{version}",
                "purl": f"pkg:npm/{purl_name}@{version}",
                "hashes": [{"alg": "SHA-512", "content": sha512}],
                "licenses": [{"license": {"id": "OFL-1.1"}}],
                "scope": "required",
                "properties": _properties(
                    runtime_owner=SHELL_OWNER_ID,
                    license_path=f"third-party-licenses/fonts/{directory.name}",
                    version_evidence="npm-lock-integrity",
                    bundled_files=",".join(file_hashes),
                ),
            }
        )
    return components


def local_font_components(ui_root: Path, license_dir: Path) -> list[dict[str, object]]:
    """Fonts kept as source files in ui/fonts/ (a renamed OFL subset, no npm package)."""
    root = license_dir / "fonts"
    root.mkdir(parents=True, exist_ok=True)
    components: list[dict[str, object]] = []
    for font in LOCAL_FONTS:
        font_dir = ui_root / "fonts"
        license_path = font_dir / f"OFL-{font['id']}.txt"
        if not license_path.is_file():
            raise FileNotFoundError(f"missing local font license: {license_path}")
        license_text = license_path.read_text(encoding="utf-8")
        if any(marker not in license_text for marker in OFL_MARKERS):
            raise RuntimeError(f"{license_path.name} is not the OFL 1.1 text")
        file_hashes = []
        digests = []
        for filename in font["files"]:
            font_path = font_dir / filename
            if not font_path.is_file():
                raise FileNotFoundError(f"missing bundled font file: {font_path}")
            digest = hashlib.sha256(font_path.read_bytes()).hexdigest()
            digests.append(digest)
            file_hashes.append(f"{filename}:{digest}")
        version = str(font["version"])
        directory = root / f"{font['id']}-{version}"
        directory.mkdir()
        (directory / "OFL-1.1.txt").write_text(license_text, encoding="utf-8")
        _write_package_metadata(
            directory,
            name=f"{font['family']} (subset of {font['upstream']})",
            version=version,
            license_expression="OFL-1.1",
            homepage=str(font["url"]),
            files=["OFL-1.1.txt"],
            provenance={"bundled_files": file_hashes},
        )
        components.append(
            {
                "type": "library",
                "name": font["family"],
                "version": version,
                "bom-ref": f"tesseract-local-font:{font['id']}@{version}",
                "hashes": [{"alg": "SHA-256", "content": digests[0]}],
                "licenses": [{"license": {"id": "OFL-1.1"}}],
                "scope": "required",
                "properties": _properties(
                    runtime_owner=SHELL_OWNER_ID,
                    license_path=f"third-party-licenses/fonts/{directory.name}",
                    version_evidence="local-font-source",
                    upstream=str(font["upstream"]),
                    bundled_files=",".join(file_hashes),
                ),
            }
        )
    return components


def npm_production_components(
    ui_root: Path, license_dir: Path, *, allowed: Iterable[str]
) -> list[dict[str, object]]:
    """Production (non-dev) npm packages bundled into the interface.

    Their license expressions must satisfy ``allowed`` (the shell/deny.toml
    allow list; widen it there together with the package that needs it).
    """
    allowed_set = frozenset(allowed)
    lock = json.loads((ui_root / "package-lock.json").read_text(encoding="utf-8"))
    components: list[dict[str, object]] = []
    for key, entry in sorted((lock.get("packages") or {}).items()):
        if not key or entry.get("dev") or entry.get("devOptional"):
            continue
        if entry.get("link"):
            raise RuntimeError(f"linked npm package in production set: {key}")
        name = key.rsplit("node_modules/", 1)[-1]
        version = str(entry.get("version") or "")
        expression = str(entry.get("license") or "")
        sha512 = _sha512_hex_from_integrity(str(entry.get("integrity") or ""))
        if not version or not expression or not sha512:
            raise RuntimeError(f"npm package {name} lacks version/license/integrity")
        normalized = normalize_spdx_expression(expression)
        if not spdx_expression_satisfied(normalized, allowed_set):
            raise RuntimeError(
                f"npm package {name} {version} license {normalized!r} is not "
                "satisfied by the shell/deny.toml allow list"
            )
        package_dir = ui_root / Path(*key.split("/"))
        license_files = sorted(
            path
            for path in package_dir.iterdir()
            if path.is_file()
            and any(token in path.name.lower() for token in ("license", "licence", "copying", "notice"))
        ) if package_dir.is_dir() else []
        if not license_files:
            raise RuntimeError(f"npm package {name} {version} ships no license file")
        directory = license_dir / "npm" / _safe_file_label(f"{name}-{version}")
        directory.mkdir(parents=True)
        copied = []
        for path in license_files:
            (directory / path.name).write_bytes(path.read_bytes())
            copied.append(path.name)
        _write_package_metadata(
            directory,
            name=name,
            version=version,
            license_expression=normalized,
            homepage="",
            files=copied,
            provenance={"npm_integrity_sha512": sha512},
        )
        purl_name = name.replace("@", "%40", 1)
        components.append(
            {
                "type": "library",
                "name": name,
                "version": version,
                "bom-ref": f"pkg:npm/{purl_name}@{version}",
                "purl": f"pkg:npm/{purl_name}@{version}",
                "hashes": [{"alg": "SHA-512", "content": sha512}],
                "licenses": [{"expression": normalized}],
                "scope": "required",
                "properties": _properties(
                    runtime_owner=SHELL_OWNER_ID,
                    license_path=f"third-party-licenses/npm/{directory.name}",
                    version_evidence="npm-lock-integrity",
                ),
            }
        )
    return components


def webview2_component(crates: Iterable[RustCrate], license_dir: Path) -> dict[str, object]:
    versions = [crate.version for crate in crates if crate.name == "webview2-com-sys"]
    if len(versions) != 1:
        raise RuntimeError("the shell graph must contain exactly one webview2-com-sys")
    provenance = WEBVIEW2_LOADER_PROVENANCE.get(versions[0])
    if provenance is None:
        raise RuntimeError(
            f"webview2-com-sys {versions[0]} has no reviewed WebView2 SDK provenance"
        )
    directory = license_dir / "microsoft-webview2"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "NOTICE.txt").write_text(
        WEBVIEW2_NOTICE.format(crate_version=versions[0], **provenance),
        encoding="utf-8",
    )
    _write_package_metadata(
        directory,
        name="Microsoft Edge WebView2 loader (static)",
        version=provenance["sdk_version"],
        license_expression="LicenseRef-Microsoft-WebView2-SDK",
        homepage="https://developer.microsoft.com/microsoft-edge/webview2/",
        files=["NOTICE.txt"],
        provenance={"webview2_com_sys": versions[0], **provenance},
    )
    return {
        "type": "library",
        "name": "Microsoft Edge WebView2 loader (static)",
        "version": provenance["sdk_version"],
        "hashes": [{"alg": "SHA-256", "content": provenance["static_lib_sha256"]}],
        "licenses": [{"license": {"name": "LicenseRef-Microsoft-WebView2-SDK"}}],
        "scope": "required",
        "properties": _properties(
            runtime_owner=SHELL_OWNER_ID,
            license_path="third-party-licenses/microsoft-webview2",
            version_evidence="reviewed-webview2-com-sys-sdk-mapping",
        ),
        "externalReferences": [
            {
                "type": "website",
                "url": "https://developer.microsoft.com/microsoft-edge/webview2/",
            }
        ],
    }


@dataclass
class ShellInventory:
    components: list[dict[str, object]]
    rust_crates: list[RustCrate]
    font_count: int
    npm_count: int
    generator: str
    inventory_sha256: str

    @property
    def mpl_crates(self) -> list[RustCrate]:
        return mpl_crates(self.rust_crates)

    def metadata_properties(self) -> list[dict[str, str]]:
        return [
            {"name": "tesseract:release-flavor", "value": "shell"},
            {"name": "tesseract:rust-crate-count", "value": str(len(self.rust_crates))},
            {
                "name": "tesseract:rust-build-only-crate-count",
                "value": str(sum(1 for c in self.rust_crates if c.role == BUILD_ROLE)),
            },
            {"name": "tesseract:rust-license-inventory", "value": self.generator},
            {
                "name": "tesseract:rust-license-inventory-sha256",
                "value": self.inventory_sha256,
            },
            {
                "name": "tesseract:rust-mpl-crates",
                "value": ",".join(
                    f"{crate.name}@{crate.version}" for crate in self.mpl_crates
                ),
            },
            {
                "name": "tesseract:rust-dependency-graph",
                "value": "cargo-tree:-p tesseract-shell --target x86_64-pc-windows-msvc",
            },
            {
                "name": "tesseract:rust-canonical-license-text-crates",
                "value": ",".join(
                    f"{crate.name}@{crate.version}:{'/'.join(crate.canonical_text_ids)}"
                    for crate in self.rust_crates
                    if crate.canonical_text_ids
                ),
            },
        ]


def collect_shell_inventory(
    *,
    license_dir: Path,
    app_version: str,
    cargo_about_json: Path,
    cargo_lock: Path,
    deny_config: Path,
    ui_root: Path,
    cargo_metadata_json: Path | None,
    cargo_tree: Path | None = None,
    cargo_tree_linked: Path | None = None,
    allow_substitute: bool = False,
) -> ShellInventory:
    if cargo_metadata_json is None or cargo_tree is None or cargo_tree_linked is None:
        raise RuntimeError(
            "--flavor shell requires the `cargo metadata` graph and both "
            "`cargo tree` graphs of shell/app (--cargo-metadata-json, "
            "--cargo-tree, --cargo-tree-linked) to check the Rust inventory is complete"
        )
    policy = load_license_policy(deny_config)
    crates, generator = load_rust_inventory(
        cargo_about_json,
        cargo_lock=cargo_lock,
        policy=policy,
        allow_substitute=allow_substitute,
        dependency_roles=load_shell_dependency_roles(
            cargo_tree=cargo_tree,
            cargo_tree_linked=cargo_tree_linked,
            cargo_metadata_json=cargo_metadata_json,
        ),
    )
    shell_component: dict[str, object] = {
        "type": "library",
        "name": "Tesseract shell",
        "version": app_version,
        "licenses": [{"license": {"id": "GPL-3.0-only"}}],
        "scope": "required",
        "properties": _properties(
            runtime_owner=SHELL_OWNER_ID,
            license_path="LICENSE",
            version_evidence="release-version-contract",
        ),
    }
    components = [shell_component]
    components.extend(write_rust_licenses(crates, license_dir, app_version=app_version))
    components.append(webview2_component(crates, license_dir))
    fonts = font_components(ui_root, license_dir) + local_font_components(ui_root, license_dir)
    npm =npm_production_components(ui_root, license_dir, allowed=policy.allow)
    components.extend(fonts)
    components.extend(npm)
    return ShellInventory(
        components=components,
        rust_crates=crates,
        font_count=len(fonts),
        npm_count=len(npm),
        generator=generator,
        inventory_sha256=hashlib.sha256(cargo_about_json.read_bytes()).hexdigest(),
    )


# --------------------------------------------------------------------------
# MPL-2.0 sources (cargo vendor) and the Shell third-party source bundle
# --------------------------------------------------------------------------

ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
QT_SOURCE_COMPONENTS = frozenset({"pyside-shiboken", "qtbase", "qtsvg"})
SHELL_BASE_SOURCE_COMPONENTS = frozenset(
    {"mihomo", "cpython-windows-license", "wintun-license"}
)


def _vendored_crate_dir(vendor_dir: Path, name: str, version: str) -> Path:
    for candidate in (vendor_dir / f"{name}-{version}", vendor_dir / name):
        manifest = candidate / "Cargo.toml"
        if not manifest.is_file():
            continue
        package = tomllib.loads(manifest.read_text(encoding="utf-8")).get("package") or {}
        if package.get("name") == name and str(package.get("version")) == version:
            return candidate
    raise FileNotFoundError(f"cargo vendor output has no {name} {version}: {vendor_dir}")


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    return info


def stage_mpl_sources(
    *,
    cargo_lock: Path,
    vendor_dir: Path,
    crates: Iterable[tuple[str, str]],
    output_dir: Path,
) -> list[dict[str, str]]:
    """Archive the vendored source of each (name, version) deterministically.

    The vendored directory must match Cargo.lock: ``.cargo-checksum.json``'s
    package checksum equals the lock checksum and every listed file matches its
    recorded SHA-256. Returns source-manifest components.
    """
    locked = load_cargo_lock(cargo_lock)
    output_dir.mkdir(parents=True, exist_ok=True)
    components: list[dict[str, str]] = []
    for name, version in sorted(set(crates)):
        lock_entry = locked.get((name, version))
        if lock_entry is None or not lock_entry["checksum"]:
            raise RuntimeError(f"{name} {version} has no Cargo.lock checksum")
        crate_dir = _vendored_crate_dir(vendor_dir, name, version)
        checksums = json.loads(
            (crate_dir / ".cargo-checksum.json").read_text(encoding="utf-8")
        )
        if checksums.get("package") != lock_entry["checksum"]:
            raise RuntimeError(
                f"vendored {name} {version} does not match the Cargo.lock checksum"
            )
        files: dict[str, str] = checksums.get("files") or {}
        if not files:
            raise RuntimeError(f"vendored {name} {version} lists no files")
        archive_name = f"rust-{name}-{version}-vendored-source.zip"
        archive_path = output_dir / archive_name
        if archive_path.exists():
            archive_path.unlink()
        prefix = f"{name}-{version}"
        with zipfile.ZipFile(
            archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            for relative in sorted(files):
                path = crate_dir.joinpath(*PurePosixPath(relative).parts)
                payload = path.read_bytes()
                if hashlib.sha256(payload).hexdigest() != files[relative]:
                    raise RuntimeError(
                        f"vendored {name} {version} file {relative} was modified"
                    )
                archive.writestr(_zip_info(f"{prefix}/{relative}"), payload)
            archive.writestr(
                _zip_info(f"{prefix}/.cargo-checksum.json"),
                (crate_dir / ".cargo-checksum.json").read_bytes(),
            )
        components.append(
            {
                "name": f"rust-{name}",
                "version": version,
                "source_path": archive_name,
                "sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
                "source_url": f"https://crates.io/api/v1/crates/{name}/{version}/download",
                "license": "MPL-2.0",
                "cargo_lock_checksum": lock_entry["checksum"],
            }
        )
    return components


def build_shell_source_bundle(
    *,
    manifest_path: Path,
    source_root: Path,
    rust_source_dir: Path,
    rust_components: list[dict[str, str]],
    project_license_path: Path,
    third_party_notices_path: Path,
    binary_mapping_path: Path,
    output_path: Path,
) -> dict[str, object]:
    """Shell variant of build_third_party_sources.build_source_bundle.

    Takes the Qt source manifest, drops pyside-setup / qtbase / qtsvg, verifies
    the remaining pinned inputs the same way, and adds the MPL-2.0 crate
    archives produced by :func:`stage_mpl_sources`.
    """
    try:
        from . import build_third_party_sources as base
    except ImportError:
        import build_third_party_sources as base  # type: ignore[no-redef]

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise ValueError("source manifest schema_version must be 1")
    selected: list[tuple[dict[str, str], Path]] = []
    seen: set[str] = set()
    for raw in payload.get("components") or ():
        name = str(raw.get("name") or "")
        if name in QT_SOURCE_COMPONENTS:
            continue
        component = {
            key: str(raw.get(key) or "").strip()
            for key in ("name", "version", "source_path", "sha256", "source_url", "license")
        }
        if any(not value for value in component.values()):
            raise ValueError(f"source component {name!r} is incomplete")
        relative = base._safe_relative_path(component["source_path"], field=name)
        path = source_root.joinpath(*relative.parts).resolve()
        path.relative_to(source_root.resolve())
        if base._sha256(path) != component["sha256"].lower():
            raise ValueError(f"{name} source hash mismatch")
        if name == base.CPYTHON_WINDOWS_LICENSE_COMPONENT:
            base._validate_cpython_windows_license(path, version=component["version"])
        component["sha256"] = component["sha256"].lower()
        selected.append((component, path))
        seen.add(name)
    if seen != SHELL_BASE_SOURCE_COMPONENTS:
        raise ValueError(
            "shell source components are not exact: "
            + ", ".join(sorted(seen ^ SHELL_BASE_SOURCE_COMPONENTS))
        )
    if not rust_components:
        raise ValueError("the shell source bundle needs the MPL-2.0 crate sources")
    for raw in rust_components:
        component = {
            key: str(raw[key])
            for key in ("name", "version", "source_path", "sha256", "source_url", "license")
        }
        path = rust_source_dir / component["source_path"]
        if base._sha256(path) != component["sha256"]:
            raise ValueError(f"{component['name']} source archive hash mismatch")
        if component["name"] in seen:
            raise ValueError(f"duplicate source component: {component['name']}")
        seen.add(component["name"])
        selected.append((component, path))
    selected.sort(key=lambda item: item[0]["name"])
    documents = (
        base._required_document(
            project_license_path,
            label=base.BUNDLE_DOCUMENTS[0][1],
            expected_sha256=base.GPL_V3_SHA256,
        ),
        base._required_document(third_party_notices_path, label=base.BUNDLE_DOCUMENTS[1][1]),
        base._required_document(binary_mapping_path, label=base.BUNDLE_DOCUMENTS[2][1]),
    )
    canonical = {"schema_version": 1, "components": [item[0] for item in selected]}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        raise FileExistsError(f"refusing to overwrite source bundle: {output_path}")
    with zipfile.ZipFile(
        output_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        archive.writestr(
            _zip_info("SOURCE-MANIFEST.json"),
            (json.dumps(canonical, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
                "utf-8"
            ),
        )
        for (archive_name, _label), document in zip(base.BUNDLE_DOCUMENTS, documents, strict=True):
            archive.writestr(_zip_info(archive_name), document)
        for component, path in selected:
            archive_name = (
                PurePosixPath("sources")
                / f"{component['name']}-{component['version']}"
                / PurePosixPath(component["source_path"]).name
            ).as_posix()
            archive.writestr(_zip_info(archive_name), path.read_bytes())
    return canonical


# Exact crate graph of the shell build, documented above parse_cargo_tree.
SHELL_MANIFEST = "shell/app/Cargo.toml"
SHELL_TARGET = "x86_64-pc-windows-msvc"
CARGO_TREE_COMMAND = (
    "cargo", "tree", "--frozen", "--manifest-path", SHELL_MANIFEST,
    "-p", SHELL_OWNER_ID, "--target", SHELL_TARGET, "--prefix", "depth", "-f", "{p}", "-e",
)
CLARIFICATION_FAILURE = "failed to validate all files specified in clarification"


def collect_cargo_inputs(repo: Path, output_dir: Path, *, vendor_dir: Path | None) -> dict[str, Path]:
    """Run the pinned cargo tools offline and write their outputs byte-exact.

    cargo deny (licenses, bans, sources) must pass; a shell/about.toml
    clarification whose checksum no longer matches fails cargo about. With
    ``vendor_dir`` the locked crate sources are also vendored there (input of
    ``mpl-sources``). Nothing is downloaded: every command runs ``--frozen``
    or ``--offline`` against the crates ``cargo fetch --locked`` cached.
    """
    import subprocess

    output_dir.mkdir(parents=True, exist_ok=True)
    outputs = {
        "cargo_metadata_json": output_dir / "cargo-metadata.json",
        "cargo_tree": output_dir / "cargo-tree.txt",
        "cargo_tree_linked": output_dir / "cargo-tree-linked.txt",
        "cargo_about_json": output_dir / "cargo-about.json",
    }

    def run(label: str, command: list[str], stdout: Path | None = None) -> None:
        print(f"[cargo-inputs] {label}: {' '.join(command)}", flush=True)
        result = subprocess.run(command, cwd=repo, capture_output=True)
        stderr = result.stderr.decode("utf-8", "replace")
        if result.returncode != 0 or CLARIFICATION_FAILURE in stderr:
            raise RuntimeError(f"{label} failed (exit {result.returncode}):\n{stderr[-4000:]}")
        if stdout is not None:
            stdout.write_bytes(result.stdout)

    run("cargo metadata", ["cargo", "metadata", "--format-version", "1", "--frozen",
                           "--filter-platform", SHELL_TARGET, "--manifest-path", SHELL_MANIFEST],
        outputs["cargo_metadata_json"])
    run("cargo tree (normal,build)", [*CARGO_TREE_COMMAND, "normal,build"], outputs["cargo_tree"])
    run("cargo tree (normal,no-proc-macro)", [*CARGO_TREE_COMMAND, "normal,no-proc-macro"],
        outputs["cargo_tree_linked"])
    run("cargo deny", ["cargo", "deny", "--frozen", "--manifest-path", SHELL_MANIFEST,
                       "check", "licenses", "bans", "sources"])
    run("cargo about", ["cargo", "about", "generate", "--frozen", "--manifest-path", SHELL_MANIFEST,
                        "--format", "json", "-o", str(outputs["cargo_about_json"])])
    if vendor_dir is not None:
        run("cargo vendor", ["cargo", "vendor", "--locked", "--offline", "--manifest-path",
                             "shell/Cargo.toml", "--versioned-dirs", str(vendor_dir)])
        outputs["vendor_dir"] = vendor_dir
    return outputs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)

    inputs = commands.add_parser(
        "cargo-inputs",
        help="run cargo metadata/tree/deny/about (offline) and optionally cargo vendor",
    )
    inputs.add_argument("--output-dir", type=Path, required=True)
    inputs.add_argument("--vendor-dir", type=Path)

    mpl = commands.add_parser(
        "mpl-sources",
        help="archive the MPL-2.0 crates of a cargo-about inventory from cargo vendor",
    )
    mpl.add_argument("--cargo-about-json", type=Path, required=True)
    mpl.add_argument("--cargo-lock", type=Path, default=Path("shell/Cargo.lock"))
    mpl.add_argument("--deny-config", type=Path, default=Path("shell/deny.toml"))
    mpl.add_argument("--vendor-dir", type=Path, required=True)
    mpl.add_argument("--output-dir", type=Path, required=True)
    mpl.add_argument("--cargo-metadata-json", type=Path)
    mpl.add_argument("--cargo-tree", type=Path)
    mpl.add_argument("--cargo-tree-linked", type=Path)
    mpl.add_argument("--allow-substitute-rust-inventory", action="store_true")

    bundle = commands.add_parser("source-bundle", help="build the Shell third-party source zip")
    bundle.add_argument("--manifest", type=Path, required=True)
    bundle.add_argument("--source-root", type=Path, required=True)
    bundle.add_argument("--rust-source-dir", type=Path, required=True)
    bundle.add_argument("--project-license", type=Path, required=True)
    bundle.add_argument("--third-party-notices", type=Path, required=True)
    bundle.add_argument("--binary-mapping", type=Path, required=True)
    bundle.add_argument("--output", type=Path, required=True)

    args = parser.parse_args(argv)
    if args.command == "cargo-inputs":
        outputs = collect_cargo_inputs(
            Path.cwd(),
            args.output_dir.resolve(),
            vendor_dir=args.vendor_dir.resolve() if args.vendor_dir else None,
        )
        for name, path in outputs.items():
            print(f"{name}: {path}")
        return 0
    if args.command == "mpl-sources":
        graph = (args.cargo_metadata_json, args.cargo_tree, args.cargo_tree_linked)
        if any(graph) and not all(graph):
            parser.error(
                "--cargo-metadata-json, --cargo-tree and --cargo-tree-linked go together"
            )
        crates, _generator = load_rust_inventory(
            args.cargo_about_json.resolve(),
            cargo_lock=args.cargo_lock.resolve(),
            policy=load_license_policy(args.deny_config.resolve()),
            allow_substitute=args.allow_substitute_rust_inventory,
            dependency_roles=(
                load_shell_dependency_roles(
                    cargo_tree=args.cargo_tree.resolve(),
                    cargo_tree_linked=args.cargo_tree_linked.resolve(),
                    cargo_metadata_json=args.cargo_metadata_json.resolve(),
                )
                if all(graph)
                else None
            ),
        )
        components = stage_mpl_sources(
            cargo_lock=args.cargo_lock.resolve(),
            vendor_dir=args.vendor_dir.resolve(),
            crates=[(crate.name, crate.version) for crate in mpl_crates(crates)],
            output_dir=args.output_dir.resolve(),
        )
        _write_json(
            args.output_dir.resolve() / "RUST-SOURCE-MANIFEST.json",
            {"schema_version": 1, "components": components},
        )
        print(f"Archived {len(components)} MPL-2.0 crate sources into {args.output_dir}.")
        return 0
    rust_source_dir = args.rust_source_dir.resolve()
    rust_manifest = json.loads(
        (rust_source_dir / "RUST-SOURCE-MANIFEST.json").read_text(encoding="utf-8")
    )
    canonical = build_shell_source_bundle(
        manifest_path=args.manifest.resolve(),
        source_root=args.source_root.resolve(),
        rust_source_dir=rust_source_dir,
        rust_components=rust_manifest["components"],
        project_license_path=args.project_license.resolve(),
        third_party_notices_path=args.third_party_notices.resolve(),
        binary_mapping_path=args.binary_mapping.resolve(),
        output_path=args.output.resolve(),
    )
    print(
        f"Wrote {args.output} with {len(canonical['components'])} source components."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
