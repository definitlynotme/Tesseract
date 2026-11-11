"""T-P6-02: Shell-flavor license / SBOM gate (collect_release_licenses --flavor shell)."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

from scripts import collect_release_licenses as crl
from scripts import shell_release_licenses as srl


ROOT = Path(__file__).resolve().parents[1]
CRATES_IO = "registry+https://github.com/rust-lang/crates.io-index"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def _checksum(name: str, version: str) -> str:
    return hashlib.sha256(f"{name}-{version}".encode()).hexdigest()


THIRD_PARTY = (
    ("serde", "1.0.0", "MIT OR Apache-2.0", ["MIT"]),
    ("cssparser", "0.36.0", "MPL-2.0", ["MPL-2.0"]),
    ("unicode-ident", "1.0.24", "(MIT OR Apache-2.0) AND Unicode-3.0", ["MIT", "Unicode-3.0"]),
    ("same-file", "1.0.6", "Unlicense/MIT", ["MIT"]),
    ("webview2-com-sys", "0.38.2", "MIT", ["MIT"]),
)


def _package(name: str, version: str, license_expr: str | None, source: str | None = CRATES_IO,
             publish: list[str] | None = None) -> dict[str, object]:
    return {
        "name": name,
        "version": version,
        "id": f"{name} {version}",
        "license": license_expr,
        "source": source,
        "repository": f"https://example.invalid/{name}",
        "publish": publish,
    }


def _about_json(path: Path, crates=THIRD_PARTY, *, extra_packages=(), substitute: bool = False,
                drop_text_for: str | None = None, canonical_for: tuple[str, ...] = ()) -> Path:
    licenses: dict[tuple[str, str], dict[str, object]] = {}
    entries = [
        {"package": _package("tesseract-shell", "0.1.0", None, None, []), "license": "Ignore"},
        {"package": _package("bridge_host", "0.1.0", None, None), "license": "Unknown"},
    ]
    for name, version, expression, chosen in crates:
        package = _package(name, version, expression)
        entries.append({"package": package, "license": expression})
        if name == drop_text_for:
            continue
        for license_id in chosen:
            # cargo-about: source_path is null when it fell back to the canonical text.
            canonical = name in canonical_for
            text = (f"canonical {license_id} text\n" if canonical
                    else f"{license_id} license text for {name}\n")
            entry = licenses.setdefault(
                (license_id, text),
                {"name": license_id, "id": license_id, "first_of_kind": True, "text": text,
                 "source_path": None if canonical else f"/registry/{name}-{version}/LICENSE",
                 "used_by": []},
            )
            entry["used_by"].append({"crate": package, "path": None})
    for package in extra_packages:
        entries.append({"package": package, "license": package.get("license") or "Unknown"})
    payload: dict[str, object] = {
        "overview": [],
        "licenses": list(licenses.values()),
        "crates": entries,
    }
    if substitute:
        payload[srl.SUBSTITUTE_MARKER] = "test"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _cargo_lock(path: Path, crates=THIRD_PARTY) -> Path:
    lines = ["version = 4", ""]
    for name, version, _expr, _chosen in crates:
        lines += [
            "[[package]]",
            f'name = "{name}"',
            f'version = "{version}"',
            f'source = "{CRATES_IO}"',
            f'checksum = "{_checksum(name, version)}"',
            "",
        ]
    lines += ["[[package]]", 'name = "tesseract-shell"', 'version = "0.1.0"', ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _cargo_metadata(path: Path, crates=THIRD_PARTY, *, drop: str | None = None) -> Path:
    """cargo metadata fixture: root -> serde, cssparser, webview2-com-sys (normal),
    same-file (build), bridge_host (path), dev-only (dev); serde -> unicode-ident,
    which is a proc-macro crate, so it is build-only."""
    def pid(name: str, version: str, source: str | None = CRATES_IO) -> str:
        return f"{source or 'path+file:///shell'}#{name}@{version}"

    def package(name: str, version: str, source: str | None = CRATES_IO, *, macro=False):
        return {
            "id": pid(name, version, source), "name": name, "version": version, "source": source,
            "targets": [{"kind": ["proc-macro"] if macro else ["lib"]}],
        }

    versions = {name: version for name, version, _e, _c in crates}
    packages = [package("tesseract-shell", "0.1.0", None), package("bridge_host", "0.1.0", None),
                package("dev-only", "1.0.0")]
    packages += [package(n, v, macro=(n == "unicode-ident")) for n, v in versions.items() if n != drop]
    root = pid("tesseract-shell", "0.1.0", None)

    def dep(name: str, kinds, source: str | None = CRATES_IO, version: str | None = None):
        return {"name": name, "pkg": pid(name, version or versions.get(name, "1.0.0"), source),
                "dep_kinds": [{"kind": kind, "target": None} for kind in kinds]}

    root_deps = [dep("bridge_host", [None], None, "0.1.0"), dep("dev-only", ["dev"], version="1.0.0")]
    for name, kinds in (("serde", [None]), ("cssparser", [None, "build"]),
                        ("webview2-com-sys", [None]), ("same-file", ["build"])):
        if name in versions and name != drop:
            root_deps.append(dep(name, kinds))
    nodes = [{"id": root, "deps": root_deps}]
    for item in packages[1:]:
        deps = []
        if item["name"] == "serde" and "unicode-ident" in versions and drop != "unicode-ident":
            deps.append(dep("unicode-ident", [None]))
        nodes.append({"id": item["id"], "deps": deps})
    path.write_text(json.dumps({
        "packages": packages, "workspace_members": [root, pid("bridge_host", "0.1.0", None)],
        "resolve": {"root": root, "nodes": nodes},
    }), encoding="utf-8")
    return path


def _cargo_trees(directory: Path, *, drop: str | None = None,
                 extra: tuple[str, ...] = ()) -> tuple[Path, Path]:
    """`cargo tree --prefix depth -f {p}` fixtures matching _cargo_metadata: the full
    tree (-e normal,build) and the linked tree (-e normal,no-proc-macro)."""
    directory.mkdir(parents=True, exist_ok=True)
    full = [
        "0tesseract-shell v0.1.0 (C:\\repo\\shell\\app)",
        "1bridge_host v0.1.0 (C:\\repo\\shell\\bridge_host)",
        "1cssparser v0.36.0",
        "1serde v1.0.0",
        "2unicode-ident v1.0.24 (proc-macro)",
        "1webview2-com-sys v0.38.2",
        "1same-file v1.0.6",
        "2serde v1.0.0 (*)",
        *extra,
    ]
    linked = [line for line in full
              if not line.startswith(("1same-file", "2unicode-ident", "2serde"))]

    def keep(line: str) -> bool:
        return drop is None or line.lstrip("0123456789").split(" ")[0] != drop

    paths = (directory / "cargo-tree.txt", directory / "cargo-tree-linked.txt")
    for path, lines in zip(paths, (full, linked)):
        path.write_text("\n".join(line for line in lines if keep(line)) + "\n", encoding="utf-8")
    return paths


def _shell_roles(tmp_path: Path, *, drop: str | None = None, extra: tuple[str, ...] = ()):
    tree, linked = _cargo_trees(tmp_path / "tree", drop=drop, extra=extra)
    return srl.load_shell_dependency_roles(
        cargo_tree=tree, cargo_tree_linked=linked,
        cargo_metadata_json=_cargo_metadata(tmp_path / "metadata.json"),
    )


def _ui_root(root: Path) -> Path:
    packages: dict[str, object] = {"": {"name": "tesseract-ui", "version": "0.1.0", "license": "UNLICENSED"}}
    for font in srl.FONT_PACKAGES:
        blob = f"{font['package']} tarball".encode()
        integrity = "sha512-" + base64.b64encode(hashlib.sha512(blob).digest()).decode()
        packages[f"node_modules/{font['package']}"] = {
            "version": "5.3.0", "dev": True, "license": "OFL-1.1", "integrity": integrity,
        }
        package_dir = root / "node_modules" / Path(*font["package"].split("/"))
        (package_dir / "files").mkdir(parents=True)
        (package_dir / "package.json").write_text(json.dumps({"version": "5.3.0"}), encoding="utf-8")
        (package_dir / "LICENSE").write_text(
            "Copyright 2024 The Authors\n\nSIL OPEN FONT LICENSE Version 1.1 - 26 February 2007\n",
            encoding="utf-8",
        )
        for filename in font["files"]:
            (package_dir / "files" / filename).write_bytes(filename.encode())
    for font in srl.LOCAL_FONTS:
        (root / "fonts").mkdir(parents=True, exist_ok=True)
        (root / "fonts" / f"OFL-{font['id']}.txt").write_text(
            "Copyright 2026 LXGW\n\nSIL OPEN FONT LICENSE Version 1.1 - 26 February 2007\n",
            encoding="utf-8",
        )
        for filename in font["files"]:
            (root / "fonts" / filename).write_bytes(filename.encode())
    (root / "package-lock.json").write_text(
        json.dumps({"lockfileVersion": 3, "packages": packages}), encoding="utf-8"
    )
    return root


def _shell_dist(root: Path, *, extra: tuple[str, ...] = ()) -> Path:
    files = (
        "Tesseract.exe",
        "tesseract-runtime.exe",
        "_internal/bin/mihomo.exe",
        "_internal/bin/wintun.dll",
        "_internal/python312.dll",
        "_internal/cryptography/hazmat/bindings/_rust.pyd",
        "_internal/aiohttp/_http_parser.cp312-win_amd64.pyd",
        "_internal/_cffi_backend.cp312-win_amd64.pyd",
        "_internal/vcruntime140.dll",
        "_internal/libcrypto-3.dll",
        "_internal/libffi-8.dll",
        *extra,
    )
    for relative in files:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if relative == "_internal/bin/wintun.dll":
            shutil.copy2(ROOT / "bin" / "wintun.dll", path)
        elif relative == "_internal/libcrypto-3.dll":
            path.write_bytes(b"frozen test fixture OpenSSL 3.0.15\x00")
        else:
            path.write_bytes(relative.encode("utf-8"))
    return root


def _shell_owner_mapping() -> dict[str, dict[str, object]]:
    versions = {
        "tesseract": "0.1.0",
        "tesseract-shell": "0.1.0",
        "mihomo": "1.19.27",
        "wintun": "0.14.1",
        "cpython": "3.12.7",
        "cryptography": "46.0.7",
        "aiohttp": "3.14.1",
        "cffi": "2.0.0",
        "openssl": "3.0.15",
        "libffi": "3.4.4",
        "microsoft-windows-runtime": None,
    }
    mapping = {
        owner: {
            "version": version,
            "license_expression": "Test-License",
            "license_path": f"third-party-licenses/{owner}",
            "version_evidence": "test-evidence",
        }
        for owner, version in versions.items()
    }
    mapping["libffi"]["dll_abi"] = "8"
    return mapping


# ---------------------------------------------------------------------------
# SPDX expressions and the license policy
# ---------------------------------------------------------------------------

def test_spdx_expressions_normalize_and_evaluate() -> None:
    assert srl.normalize_spdx_expression("MIT/Apache-2.0") == "MIT OR Apache-2.0"
    assert srl.normalize_spdx_expression("Apache-2.0 / MIT") == "Apache-2.0 OR MIT"
    assert (
        srl.normalize_spdx_expression("(MIT OR Apache-2.0) AND Unicode-3.0")
        == "(MIT OR Apache-2.0) AND Unicode-3.0"
    )
    allowed = {"MIT", "Unicode-3.0"}
    assert srl.spdx_expression_satisfied("(MIT OR Apache-2.0) AND Unicode-3.0", allowed)
    assert not srl.spdx_expression_satisfied("Apache-2.0 AND MIT", allowed)
    assert srl.spdx_expression_satisfied("CC0-1.0 OR MIT-0 OR Apache-2.0", {"Apache-2.0"})
    assert not srl.spdx_expression_satisfied("GPL-3.0-only", {"MIT", "MPL-2.0"})
    assert srl.spdx_license_ids("0BSD OR MIT OR Apache-2.0") == {"0BSD", "MIT", "Apache-2.0"}
    for bad in ("", "MIT OR", "(MIT", "MIT AND AND Apache-2.0", "MIT) OR (Zlib"):
        with pytest.raises(ValueError):
            srl.parse_spdx_expression(bad)


def test_deny_policy_is_the_reviewed_allow_list_and_matches_cargo_about() -> None:
    policy = srl.load_license_policy(ROOT / "shell" / "deny.toml")
    assert policy.allow == {"MIT", "Apache-2.0", "Unicode-3.0", "Zlib", "MPL-2.0"}
    assert not {"GPL-3.0-only", "GPL-3.0", "LGPL-3.0-only", "AGPL-3.0-only"} & policy.allow
    # First-party workspace crates are the project itself (GPL-3.0-only).
    assert policy.clarify == {"tesseract-shell": "GPL-3.0-only", "bridge_host": "GPL-3.0-only"}
    assert policy.exceptions == {
        "tesseract-shell": {"GPL-3.0-only"},
        "bridge_host": {"GPL-3.0-only"},
    }
    deny = tomllib.loads((ROOT / "shell" / "deny.toml").read_text(encoding="utf-8"))
    about = tomllib.loads((ROOT / "shell" / "about.toml").read_text(encoding="utf-8"))
    assert about["accepted"] == deny["licenses"]["allow"]
    assert about["targets"] == deny["graph"]["targets"] == ["x86_64-pc-windows-msvc"]
    assert deny["sources"]["unknown-registry"] == "deny"
    assert deny["sources"]["unknown-git"] == "deny"
    assert deny["sources"]["allow-registry"] == ["https://github.com/rust-lang/crates.io-index"]
    assert deny["bans"]["wildcards"] == "deny"


def test_font_mirror_matches_the_ui_build_plugin() -> None:
    source = (ROOT / "ui" / "build" / "fonts.mjs").read_text(encoding="utf-8")
    packages = re.findall(r"\{ id: '([^']+)', family: '([^']+)', pkg: '([^']+)', license: 'OFL-1.1' \}", source)
    faces = re.findall(r"\{ pkg: '([^']+)', family: '[^']+', subset: '[^']+', weight: '[^']+', file: '([^']+)' \}", source)
    assert [(f["id"], f["family"], f["package"]) for f in srl.FONT_PACKAGES] == packages
    for font in srl.FONT_PACKAGES:
        assert sorted(font["files"]) == sorted(file for pkg, file in faces if pkg == font["package"])
    local = re.findall(r"\{ id: '([^']+)', family: '([^']+)', pkg: null, local: true, license: 'OFL-1.1',\s*"
                       r"upstream: '([^']+)', version: '([^']+)', url: '([^']+)'", source)
    assert [(f["id"], f["family"], f["upstream"], f["version"], f["url"]) for f in srl.LOCAL_FONTS] == local
    local_faces = re.findall(r"\{ local: true, family: '([^']+)', subset: '[^']+', weight: '[^']+', file: '([^']+)' \}",
                             source)
    for font in srl.LOCAL_FONTS:
        assert sorted(font["files"]) == sorted(file for family, file in local_faces if family == font["family"])


def test_local_font_is_inventoried_with_its_ofl_text(tmp_path: Path) -> None:
    ui_root = _ui_root(tmp_path / "ui")
    license_dir = tmp_path / "licenses"
    components = srl.local_font_components(ui_root, license_dir)
    assert [c["name"] for c in components] == [f["family"] for f in srl.LOCAL_FONTS]
    for font, component in zip(srl.LOCAL_FONTS, components):
        directory = license_dir / "fonts" / f"{font['id']}-{font['version']}"
        assert "SIL OPEN FONT LICENSE" in (directory / "OFL-1.1.txt").read_text(encoding="utf-8")
        assert component["licenses"] == [{"license": {"id": "OFL-1.1"}}]


def test_real_local_font_sources_are_present() -> None:
    for font in srl.LOCAL_FONTS:
        text = (ROOT / "ui" / "fonts" / f"OFL-{font['id']}.txt").read_text(encoding="utf-8")
        assert all(marker in text for marker in srl.OFL_MARKERS)
        for filename in font["files"]:
            assert (ROOT / "ui" / "fonts" / filename).stat().st_size > 0


# ---------------------------------------------------------------------------
# Rust inventory (cargo-about JSON)
# ---------------------------------------------------------------------------

def _load(tmp_path: Path, about: Path, **kwargs):
    return srl.load_rust_inventory(
        about,
        cargo_lock=_cargo_lock(tmp_path / "Cargo.lock"),
        policy=srl.load_license_policy(ROOT / "shell" / "deny.toml"),
        **kwargs,
    )


def test_rust_inventory_checks_lock_policy_and_texts(tmp_path: Path) -> None:
    crates, generator = _load(tmp_path, _about_json(tmp_path / "about.json"))
    assert generator == "cargo-about"
    assert [(c.name, c.version) for c in crates] == sorted((n, v) for n, v, _e, _c in THIRD_PARTY)
    by_name = {c.name: c for c in crates}
    assert by_name["same-file"].expression == "Unlicense OR MIT"
    assert by_name["serde"].checksum == _checksum("serde", "1.0.0")
    assert [c.name for c in srl.mpl_crates(crates)] == ["cssparser"]
    assert "tesseract-shell" not in by_name and "bridge_host" not in by_name


@pytest.mark.parametrize(
    ("kwargs", "message"),
    (
        ({"extra_packages": [_package("evil", "1.0.0", "GPL-3.0-only")]}, "not in Cargo.lock"),
        ({"extra_packages": [_package("vendored", "0.1.0", "MIT", source=None)]}, "unexpected path"),
        ({"extra_packages": [_package("gitdep", "0.1.0", "MIT", source="git+https://example.invalid/x")]}, "non-crates.io"),
        ({"drop_text_for": "serde"}, "no license text"),
        ({"substitute": True}, "not produced by cargo-about"),
    ),
)
def test_rust_inventory_rejects_unsafe_inputs(tmp_path: Path, kwargs, message) -> None:
    with pytest.raises(RuntimeError, match=message):
        _load(tmp_path, _about_json(tmp_path / "about.json", **kwargs))


def test_rust_inventory_rejects_licenses_outside_the_allow_list(tmp_path: Path) -> None:
    crates = (*THIRD_PARTY, ("copyleft", "1.0.0", "GPL-3.0-only", ["GPL-3.0-only"]))
    about = _about_json(tmp_path / "about.json", crates=crates)
    with pytest.raises(RuntimeError, match="not satisfied by the shell/deny.toml allow list"):
        srl.load_rust_inventory(
            about,
            cargo_lock=_cargo_lock(tmp_path / "Cargo.lock", crates=crates),
            policy=srl.load_license_policy(ROOT / "shell" / "deny.toml"),
        )


def test_cargo_metadata_roles_separate_build_only_crates(tmp_path: Path) -> None:
    roles = srl.load_dependency_roles(_cargo_metadata(tmp_path / "metadata.json"))
    assert roles == {
        ("serde", "1.0.0"): "runtime",
        ("cssparser", "0.36.0"): "runtime",
        ("webview2-com-sys", "0.38.2"): "runtime",
        ("same-file", "1.0.6"): "build",        # build-dependency edge only
        ("unicode-ident", "1.0.24"): "build",   # proc-macro crate
    }
    crates, _generator = _load(tmp_path, _about_json(tmp_path / "about.json"), dependency_roles=roles)
    assert {c.name: c.role for c in crates}["same-file"] == "build"


@pytest.mark.parametrize(
    ("about_kwargs", "drop", "message"),
    (
        ({}, "serde", r"not in the graph serde 1\.0\.0"),
        ({"crates": THIRD_PARTY[1:]}, None, r"missing serde 1\.0\.0"),
    ),
)
def test_rust_inventory_must_match_the_cargo_graph_both_ways(
    tmp_path: Path, about_kwargs, drop, message
) -> None:
    roles = _shell_roles(tmp_path, drop=drop)
    with pytest.raises(RuntimeError, match=message):
        _load(tmp_path, _about_json(tmp_path / "about.json", **about_kwargs), dependency_roles=roles)


def test_cargo_tree_roles_are_the_exact_build_graph(tmp_path: Path) -> None:
    roles = _shell_roles(tmp_path)
    # Same split as the metadata walk on this fixture, but from cargo's own
    # per-package resolution: proc macros and build-only edges are "build".
    assert roles == srl.load_dependency_roles(_cargo_metadata(tmp_path / "m2.json"))
    crates, _generator = _load(tmp_path, _about_json(tmp_path / "about.json"), dependency_roles=roles)
    assert {c.name: c.role for c in crates} == {
        "serde": "runtime", "cssparser": "runtime", "webview2-com-sys": "runtime",
        "same-file": "build", "unicode-ident": "build",
    }


def test_cargo_tree_narrower_than_metadata_is_the_truth(tmp_path: Path) -> None:
    # cargo metadata unifies features over the whole workspace and may list
    # crates the shell build never compiles; the tree wins, metadata only bounds it.
    roles = _shell_roles(tmp_path, drop="webview2-com-sys")
    assert ("webview2-com-sys", "0.38.2") not in roles
    crates = THIRD_PARTY[:4]
    about = _about_json(tmp_path / "about.json", crates=crates)
    loaded, _generator = _load(tmp_path, about, dependency_roles=roles)
    assert {c.name for c in loaded} == {n for n, _v, _e, _c in crates}


@pytest.mark.parametrize(
    ("extra", "message"),
    (
        (("1rogue v1.0.0",), "outside the cargo metadata graph: rogue 1.0.0"),
        (("1gitdep v0.1.0 (https://example.invalid/x#abc)",), "does not come from crates.io"),
        (("1headless_shell v0.1.0",), "headless_shell is not a path crate"),
        (("0other-root v0.1.0",), "not the cargo tree of tesseract-shell"),
        (("not a tree line",), "not `cargo tree"),
    ),
)
def test_cargo_tree_rejects_unexpected_graphs(tmp_path: Path, extra, message) -> None:
    with pytest.raises(RuntimeError, match=re.escape(message)):
        _shell_roles(tmp_path, extra=extra)


def test_canonical_license_texts_are_marked(tmp_path: Path) -> None:
    about = _about_json(tmp_path / "about.json", canonical_for=("webview2-com-sys",))
    crates, _generator = _load(tmp_path, about)
    by_name = {c.name: c for c in crates}
    assert by_name["webview2-com-sys"].canonical_text_ids == ("MIT",)
    assert by_name["serde"].canonical_text_ids == ()
    components = srl.write_rust_licenses(crates, tmp_path / "licenses", app_version="0.1.0")
    sources = {c["name"]: {p["name"]: p["value"] for p in c["properties"]}
               ["tesseract:license-text-source"] for c in components}
    assert sources["webview2-com-sys"] == "canonical:MIT"
    assert sources["serde"] == "crate-files"
    index = (tmp_path / "licenses" / "rust-crates" / "INDEX.md").read_text(encoding="utf-8")
    assert "webview2-com-sys 0.38.2: MIT (`rust-crates/webview2-com-sys-0.38.2/`) - canonical MIT text" in index


def test_npm_production_packages_must_satisfy_the_allow_list(tmp_path: Path) -> None:
    ui = _ui_root(tmp_path / "ui")
    lock = json.loads((ui / "package-lock.json").read_text(encoding="utf-8"))
    blob = base64.b64encode(hashlib.sha512(b"lib").digest()).decode()
    lock["packages"]["node_modules/copyleft-lib"] = {
        "version": "1.0.0", "license": "GPL-3.0-only", "integrity": f"sha512-{blob}",
    }
    (ui / "package-lock.json").write_text(json.dumps(lock), encoding="utf-8")
    package_dir = ui / "node_modules" / "copyleft-lib"
    package_dir.mkdir(parents=True)
    (package_dir / "LICENSE").write_text("GPL text", encoding="utf-8")
    with pytest.raises(RuntimeError, match="copyleft-lib 1.0.0 license 'GPL-3.0-only' is not"):
        srl.npm_production_components(ui, tmp_path / "licenses", allowed={"MIT"})
    assert len(srl.npm_production_components(ui, tmp_path / "ok", allowed={"GPL-3.0-only"})) == 1


def test_substitute_inventory_is_labelled_when_explicitly_allowed(tmp_path: Path) -> None:
    _crates, generator = _load(
        tmp_path, _about_json(tmp_path / "about.json", substitute=True), allow_substitute=True
    )
    assert generator == "substitute:test"


# ---------------------------------------------------------------------------
# frozen distribution audit (shell layout)
# ---------------------------------------------------------------------------

def test_shell_audit_maps_shell_and_sidecar_without_qt(tmp_path: Path) -> None:
    components = crl.audit_frozen_distribution(
        _shell_dist(tmp_path / "dist"), runtime_owners=_shell_owner_mapping(), flavor="shell"
    )
    owners = {
        prop["value"]: component["name"]
        for component in components
        for prop in component["properties"]
        if prop["name"] == "tesseract:runtime-owner"
    }
    assert owners["tesseract-shell"] == "Tesseract.exe"
    assert owners["tesseract"] == "tesseract-runtime.exe"
    assert not {"qt", "pyside6", "shiboken6"} & set(owners)
    assert len(components) == 11


@pytest.mark.parametrize(
    "relative",
    ("_internal/PySide6/QtCore.pyd", "_internal/PySide6/Qt6Core.dll", "_internal/shiboken6/shiboken6.abi3.dll"),
)
def test_shell_audit_rejects_qt_payload(tmp_path: Path, relative: str) -> None:
    dist = _shell_dist(tmp_path / "dist", extra=(relative,))
    with pytest.raises(RuntimeError, match="forbidden Qt artifacts"):
        crl.audit_frozen_distribution(dist, runtime_owners=_shell_owner_mapping(), flavor="shell")


@pytest.mark.parametrize("relative", ("vcruntime140.dll", "WebView2Loader.dll", "msvcp140.dll"))
def test_shell_audit_rejects_root_level_runtime_overrides(tmp_path: Path, relative: str) -> None:
    dist = _shell_dist(tmp_path / "dist", extra=(relative,))
    with pytest.raises(RuntimeError, match="unmapped executable artifacts"):
        crl.audit_frozen_distribution(dist, runtime_owners=_shell_owner_mapping(), flavor="shell")


def test_shell_audit_requires_both_executables(tmp_path: Path) -> None:
    dist = _shell_dist(tmp_path / "dist")
    (dist / "tesseract-runtime.exe").unlink()
    with pytest.raises(RuntimeError, match="missing required runtime components: tesseract$"):
        crl.audit_frozen_distribution(dist, runtime_owners=_shell_owner_mapping(), flavor="shell")


def test_qt_default_audit_still_rejects_the_shell_layout(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="unmapped executable artifacts.*tesseract-runtime.exe"):
        crl.audit_frozen_distribution(_shell_dist(tmp_path / "dist"), runtime_owners=_shell_owner_mapping())


@pytest.mark.parametrize(
    "option",
    (["--cargo-about-json", "a.json"], ["--cargo-metadata-json", "m.json"],
     ["--cargo-tree", "t.txt"], ["--cargo-tree-linked", "l.txt"],
     ["--cargo-lock", "Cargo.lock"], ["--deny-config", "deny.toml"], ["--ui-root", "ui"],
     ["--third-party-notices", "n.md"], ["--allow-substitute-rust-inventory"]),
)
def test_cli_rejects_shell_options_without_shell_flavor(monkeypatch, capsys, option) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["collect_release_licenses.py", "--lock", "x", "--license-dir", "y", "--sbom", "z",
         "--mihomo-version", "1", "--app-version", "1", *option],
    )
    with pytest.raises(SystemExit) as raised:
        crl.main()
    assert raised.value.code == 2
    assert "require --flavor shell" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# end-to-end shell collection
# ---------------------------------------------------------------------------

def test_shell_collection_writes_shell_sbom_licenses_and_notices(tmp_path: Path) -> None:
    license_dir = tmp_path / "licenses"
    sbom_path = tmp_path / "sbom.cdx.json"
    dist = _shell_dist(tmp_path / "dist")
    crl.collect(
        lock_path=ROOT / "requirements-windows.lock.txt",
        runtime_requirements_path=ROOT / "requirements.txt",
        build_requirements_path=ROOT / "requirements-build.txt",
        license_dir=license_dir,
        sbom_path=sbom_path,
        mihomo_version="1.19.27",
        app_version="0.1.0",
        dist_dir=dist,
        flavor="shell",
        cargo_about_json=_about_json(tmp_path / "about.json"),
        cargo_lock_path=_cargo_lock(tmp_path / "Cargo.lock"),
        ui_root=_ui_root(tmp_path / "ui"),
        cargo_metadata_json=_cargo_metadata(tmp_path / "metadata.json"),
        cargo_tree=_cargo_trees(tmp_path / "tree")[0],
        cargo_tree_linked=_cargo_trees(tmp_path / "tree")[1],
    )
    sbom = json.loads(sbom_path.read_text(encoding="utf-8"))
    properties = {p["name"]: p["value"] for p in sbom["metadata"]["properties"]}
    assert properties["tesseract:release-flavor"] == "shell"
    assert properties["tesseract:rust-crate-count"] == str(len(THIRD_PARTY))
    assert properties["tesseract:rust-build-only-crate-count"] == "2"
    assert properties["tesseract:rust-license-inventory"] == "cargo-about"
    assert properties["tesseract:rust-mpl-crates"] == "cssparser@0.36.0"
    assert properties["tesseract:rust-dependency-graph"].startswith("cargo-tree:")
    assert properties["tesseract:rust-canonical-license-text-crates"] == ""
    assert properties["tesseract:frozen-binary-count"] == "11"
    # PySide6 left the release lock with the Qt flavor (P8): nothing to exclude.
    excluded = set(filter(None, properties["tesseract:excluded-lock-distributions"].split(",")))
    assert not excluded & {"pyside6", "shiboken6", "pyside6-essentials", "pyside6-addons"}

    names = {str(c["name"]).casefold() for c in sbom["components"]}
    assert not {"qt", "pyside6", "shiboken6", "pyside6-essentials", "pyside6-addons"} & names
    assert {"mihomo", "wintun", "cpython", "tesseract shell", "instrument sans", "geist mono",
            "microsoft edge webview2 loader (static)"} <= names
    rust = {c["name"]: c for c in sbom["components"] if str(c.get("purl", "")).startswith("pkg:cargo/")}
    assert set(rust) == {n for n, _v, _e, _c in THIRD_PARTY}
    assert rust["cssparser"]["hashes"] == [{"alg": "SHA-256", "content": _checksum("cssparser", "0.36.0")}]
    assert rust["unicode-ident"]["licenses"] == [{"expression": "(MIT OR Apache-2.0) AND Unicode-3.0"}]
    for name, component in rust.items():
        role = {p["name"]: p["value"] for p in component["properties"]}["tesseract:dependency-role"]
        assert role == ("build" if name in {"same-file", "unicode-ident"} else "runtime"), name
        assert component["scope"] == ("excluded" if role == "build" else "required"), name
    same_file = json.loads((license_dir / "rust-crates" / "same-file-1.0.6" / "PACKAGE-METADATA.json")
                           .read_text(encoding="utf-8"))
    assert same_file["dependency_role"] == "build"
    index = (license_dir / "rust-crates" / "INDEX.md").read_text(encoding="utf-8")
    assert index.startswith("# Rust crates compiled into or used to build Tesseract.exe")
    assert "same-file 1.0.6: Unlicense OR MIT (`rust-crates/same-file-1.0.6/`) - build only" in index
    source_access = (license_dir / "rust-crates" / "cssparser-0.36.0" / "SOURCE-ACCESS.txt").read_text(
        encoding="utf-8")
    assert srl.SHELL_SOURCE_BUNDLE_NAME.format(version="0.1.0") in source_access
    assert "<version>" not in source_access
    fonts = [c for c in sbom["components"] if c["name"] in {"Instrument Sans", "Geist Mono"}]
    assert all(c["licenses"] == [{"license": {"id": "OFL-1.1"}}] for c in fonts)
    assert all(c["hashes"][0]["alg"] == "SHA-512" for c in fonts)
    for component in sbom["components"]:
        props = {p["name"]: p["value"] for p in component["properties"]}
        license_path = props["tesseract:license-path"]
        assert license_path == "LICENSE" or (dist / license_path).exists(), license_path

    assert not list(license_dir.glob("qt-*")) and not list(license_dir.glob("pyside6*"))
    mpl = json.loads((license_dir / "rust-crates" / "MPL-2.0-SOURCES.json").read_text(encoding="utf-8"))
    assert [c["name"] for c in mpl["crates"]] == ["cssparser"]
    assert (license_dir / "rust-crates" / "cssparser-0.36.0" / "SOURCE-ACCESS.txt").is_file()
    assert (license_dir / "rust-crates" / "unicode-ident-1.0.24" / "LICENSE-Unicode-3.0.txt").is_file()
    assert "SIL OPEN FONT LICENSE Version 1.1" in (
        license_dir / "fonts" / "geist-mono-5.3.0" / "OFL-1.1.txt"
    ).read_text(encoding="utf-8")
    assert "1.0.3650.58" in (license_dir / "microsoft-webview2" / "NOTICE.txt").read_text(encoding="utf-8")
    runtime_notice = (license_dir / "microsoft-windows-runtime" / "NOTICE.txt").read_text(encoding="utf-8")
    assert "PySide6" not in runtime_notice and "redistributed with CPython." in runtime_notice
    assert (dist / "THIRD_PARTY_NOTICES.md").read_bytes() == (ROOT / "THIRD_PARTY_NOTICES.shell.md").read_bytes()
    assert (dist / "sbom.cdx.json").read_bytes() == sbom_path.read_bytes()


def test_shell_collection_requires_the_cargo_tree_graphs(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="both cargo tree graphs"):
        crl.collect(
            lock_path=ROOT / "requirements-windows.lock.txt",
            runtime_requirements_path=ROOT / "requirements.txt",
            build_requirements_path=ROOT / "requirements-build.txt",
            license_dir=tmp_path / "licenses",
            sbom_path=tmp_path / "sbom.cdx.json",
            mihomo_version="1.19.27",
            app_version="0.1.0",
            flavor="shell",
            cargo_about_json=_about_json(tmp_path / "about.json"),
            cargo_metadata_json=_cargo_metadata(tmp_path / "metadata.json"),
            cargo_tree=_cargo_trees(tmp_path / "tree")[0],
        )
    assert not (tmp_path / "licenses").exists()


def test_shell_collection_requires_the_cargo_metadata_graph(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cargo metadata graph"):
        crl.collect(
            lock_path=ROOT / "requirements-windows.lock.txt",
            runtime_requirements_path=ROOT / "requirements.txt",
            build_requirements_path=ROOT / "requirements-build.txt",
            license_dir=tmp_path / "licenses",
            sbom_path=tmp_path / "sbom.cdx.json",
            mihomo_version="1.19.27",
            app_version="0.1.0",
            flavor="shell",
            cargo_about_json=_about_json(tmp_path / "about.json"),
        )
    assert not (tmp_path / "licenses").exists()


def test_shell_notices_drop_qt_and_name_rust_fonts_and_webview2() -> None:
    text = (ROOT / "THIRD_PARTY_NOTICES.shell.md").read_text(encoding="utf-8")
    assert "Qt for Python / PySide6" not in text
    assert "qtbase" not in text.lower() and "Qt SVG" not in text
    for needle in ("cssparser", "selectors", "dtoa-short", "option-ext", "OFL-1.1",
                   "WebView2", "rust-crates/INDEX.md", "temporary directory"):
        assert needle in text
    assert (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8").count("Qt for Python / PySide6") == 1


# ---------------------------------------------------------------------------
# MPL sources (cargo vendor) and the shell source bundle
# ---------------------------------------------------------------------------

def _vendor(root: Path, name: str, version: str, *, package_checksum: str) -> Path:
    crate = root / f"{name}-{version}"
    (crate / "src").mkdir(parents=True)
    files = {
        "Cargo.toml": f'[package]\nname = "{name}"\nversion = "{version}"\n'.encode(),
        "LICENSE": b"Mozilla Public License Version 2.0\n",
        "src/lib.rs": b"pub fn f() {}\n",
    }
    for relative, payload in files.items():
        (crate / relative).write_bytes(payload)
    (crate / ".cargo-checksum.json").write_text(
        json.dumps({"files": {k: hashlib.sha256(v).hexdigest() for k, v in files.items()},
                    "package": package_checksum}),
        encoding="utf-8",
    )
    return crate


def test_mpl_sources_are_verified_against_cargo_lock_and_deterministic(tmp_path: Path) -> None:
    lock = _cargo_lock(tmp_path / "Cargo.lock")
    vendor = tmp_path / "vendor"
    _vendor(vendor, "cssparser", "0.36.0", package_checksum=_checksum("cssparser", "0.36.0"))
    first = srl.stage_mpl_sources(cargo_lock=lock, vendor_dir=vendor,
                                  crates=[("cssparser", "0.36.0")], output_dir=tmp_path / "a")
    second = srl.stage_mpl_sources(cargo_lock=lock, vendor_dir=vendor,
                                   crates=[("cssparser", "0.36.0")], output_dir=tmp_path / "b")
    assert first == second
    assert first[0]["name"] == "rust-cssparser" and first[0]["license"] == "MPL-2.0"
    with zipfile.ZipFile(tmp_path / "a" / first[0]["source_path"]) as archive:
        assert sorted(archive.namelist()) == [
            "cssparser-0.36.0/.cargo-checksum.json",
            "cssparser-0.36.0/Cargo.toml",
            "cssparser-0.36.0/LICENSE",
            "cssparser-0.36.0/src/lib.rs",
        ]

    (vendor / "cssparser-0.36.0" / "src" / "lib.rs").write_bytes(b"patched\n")
    with pytest.raises(RuntimeError, match="was modified"):
        srl.stage_mpl_sources(cargo_lock=lock, vendor_dir=vendor,
                              crates=[("cssparser", "0.36.0")], output_dir=tmp_path / "c")


def test_mpl_sources_reject_a_vendor_dir_that_differs_from_the_lock(tmp_path: Path) -> None:
    vendor = tmp_path / "vendor"
    _vendor(vendor, "cssparser", "0.36.0", package_checksum="0" * 64)
    with pytest.raises(RuntimeError, match="does not match the Cargo.lock checksum"):
        srl.stage_mpl_sources(cargo_lock=_cargo_lock(tmp_path / "Cargo.lock"), vendor_dir=vendor,
                              crates=[("cssparser", "0.36.0")], output_dir=tmp_path / "out")


def test_shell_source_bundle_drops_qt_and_adds_mpl_crates(tmp_path: Path) -> None:
    source_root = tmp_path / "sources"
    source_root.mkdir()
    cpython_text = (
        "PYTHON SOFTWARE FOUNDATION LICENSE VERSION 2\n"
        "Additional Conditions for this Windows binary build\n"
        'This program, "bzip2", the associated library "libbzip2"\n'
        "libffi - Copyright\n"
    )
    payloads = {
        "mihomo-src.zip": b"mihomo source",
        "cpython-license.txt": cpython_text.encode(),
        "wintun-license.txt": b"wintun license",
    }
    for filename, payload in payloads.items():
        (source_root / filename).write_bytes(payload)

    def component(name, version, filename, license_id):
        return {"name": name, "version": version, "source_path": filename,
                "sha256": hashlib.sha256(payloads.get(filename, b"absent")).hexdigest(),
                "source_url": f"https://example.invalid/{name}", "license": license_id}

    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"schema_version": 1, "components": [
        component("mihomo", "1.19.27", "mihomo-src.zip", "GPL-3.0-only"),
        component("pyside-shiboken", "6.11.0", "pyside.tar.xz", "GPL-3.0-only"),
        component("qtbase", "6.11.0", "qtbase.tar.xz", "GPL-3.0-only"),
        component("qtsvg", "6.11.0", "qtsvg.tar.xz", "GPL-3.0-only"),
        component("cpython-windows-license", "3.12.7", "cpython-license.txt", "PSF-2.0"),
        component("wintun-license", "0.14.1", "wintun-license.txt", "LicenseRef-WinTun-Prebuilt"),
    ]}), encoding="utf-8")
    vendor = tmp_path / "vendor"
    _vendor(vendor, "cssparser", "0.36.0", package_checksum=_checksum("cssparser", "0.36.0"))
    rust_dir = tmp_path / "rust"
    rust_components = srl.stage_mpl_sources(
        cargo_lock=_cargo_lock(tmp_path / "Cargo.lock"), vendor_dir=vendor,
        crates=[("cssparser", "0.36.0")], output_dir=rust_dir,
    )
    output = tmp_path / "out" / "third-party-sources.zip"
    canonical = srl.build_shell_source_bundle(
        manifest_path=manifest,
        source_root=source_root,
        rust_source_dir=rust_dir,
        rust_components=rust_components,
        project_license_path=ROOT / "LICENSE",
        third_party_notices_path=ROOT / "THIRD_PARTY_NOTICES.shell.md",
        binary_mapping_path=ROOT / "packaging" / "THIRD_PARTY_BUILD_AND_BINARY_MAPPING.md",
        output_path=output,
    )
    assert [c["name"] for c in canonical["components"]] == [
        "cpython-windows-license", "mihomo", "rust-cssparser", "wintun-license",
    ]
    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()
        assert "sources/rust-cssparser-0.36.0/rust-cssparser-0.36.0-vendored-source.zip" in names
        assert not [n for n in names if "qt" in n.lower() or "pyside" in n.lower()]
        assert archive.read("THIRD_PARTY_NOTICES.md") == (ROOT / "THIRD_PARTY_NOTICES.shell.md").read_bytes()
