#!/usr/bin/env python3
"""Fail-closed, read-only anonymity and secret audit for public releases.

The auditor intentionally reads only the selected Git repository (tracked
working-tree files plus every object in its object database) and explicitly
named release artifacts inside that repository.  It never reads application
configuration/data directories and never prints matched values.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import ipaddress
import json
import os
import re
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable, Iterator, Sequence
from urllib.parse import parse_qsl, unquote, urlsplit

try:
    from scripts.build_third_party_sources import (
        CPYTHON_WINDOWS_LICENSE_COMPONENT,
        CPYTHON_WINDOWS_LICENSE_MARKERS,
        CPYTHON_WINDOWS_VERSION,
    )
except ModuleNotFoundError:  # Direct ``python scripts/audit_public_release.py``.
    from build_third_party_sources import (  # type: ignore[no-redef]
        CPYTHON_WINDOWS_LICENSE_COMPONENT,
        CPYTHON_WINDOWS_LICENSE_MARKERS,
        CPYTHON_WINDOWS_VERSION,
    )


DEFAULT_ALLOWED_GIT_NAME = "Tesseract contributors"
GITHUB_ID_NOREPLY_RE = re.compile(
    r"[1-9][0-9]*\+[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?"
    r"@users\.noreply\.github\.com",
    re.IGNORECASE,
)
MAX_DENY_FILE_BYTES = 1024 * 1024
DEFAULT_MAX_FILE_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024
DEFAULT_MAX_ARCHIVE_MEMBERS = 100_000
DEFAULT_MAX_ARCHIVE_DEPTH = 6
MAX_REPORTED_FINDINGS = 200
PRINTABLE_UTF16LE_RE = re.compile(rb"(?:[\x20-\x7e]\x00){6,}")
PRINTABLE_UTF16BE_RE = re.compile(rb"(?:\x00[\x20-\x7e]){6,}")

SAFE_LOCAL_USERS = {
    "admin",
    "administrator",
    "alice",
    # CPython's own ntpath documentation example (python312.dll, base_library.zip).
    "barney",
    "bob",
    "default",
    "default user",
    "example",
    "name",
    "path",
    "public",
    "runneradmin",
    # 8.3 short name of the GitHub Actions runneradmin profile, compiled into
    # PyPI wheels (frozenlist, propcache, yarl) built on hosted runners.
    "runner~1",
    "test",
    "tester",
    "user",
    "username",
}
REFUSED_USER_DATA_NAMES = {
    "config.local.yaml",
    "config.yaml",
    "nodes.yaml",
    "sources.yaml",
}
REFUSED_USER_DATA_DIRS = {
    ".git",
    ".venv",
    "data",
    "venv",
}
ZIP_SUFFIXES = {".zip", ".whl", ".pyz"}
ZIP_MAGICS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
SOURCE_MANIFEST_FIELDS = (
    "name",
    "version",
    "source_path",
    "sha256",
    "source_url",
    "license",
)
REQUIRED_SOURCE_COMPONENTS = frozenset(
    {
        "cpython-windows-license",
        "mihomo",
        "pyside-shiboken",
        "qtbase",
        "qtsvg",
        "wintun-license",
    }
)
# The Tauri-shell release bundle (scripts/shell_release_licenses.py source-bundle)
# ships the template's components minus the retired Qt sources, plus the
# MPL-2.0 Rust crates compiled into or used to build Tesseract.exe.
SHELL_DROPPED_SOURCE_COMPONENTS = frozenset({"pyside-shiboken", "qtbase", "qtsvg"})
RUST_SOURCE_COMPONENT_RE = re.compile(r"rust-(?P<crate>[a-z0-9][a-z0-9_-]*)")
SOURCE_BUNDLE_DOCUMENTS = frozenset(
    {
        "LICENSES/GPL-3.0-only.txt",
        "THIRD_PARTY_NOTICES.md",
        "BUILD-AND-BINARY-MAPPING.md",
    }
)
SOURCE_BUNDLE_NAME_RE = re.compile(
    r"^Tesseract-[0-9]+\.[0-9]+\.[0-9]+(?:-rc\.[0-9]+)?-"
    r"third-party-sources\.zip$"
)
PINNED_UPSTREAM_BINARY_HASHES = {
    "mihomo.exe": "842fa17493a82c97148e76e3c523f5058e3cf386fba611cab86b6681e75f2f77",
    "wintun.dll": "e5da8447dc2c320edc0fc52fa01885c103de8c118481f683643cacc3220dafce",
}

LOCAL_PATH_PATTERNS = (
    re.compile(
        r"(?i)(?:[A-Z]:[\\/]+Users[\\/]+|[\\/]+Documents and Settings[\\/]+)"
        r"(?P<user>[^\\/\s:\"'<>]+)"
    ),
    re.compile(
        r"(?i)(?:/" + r"Users/|/" + r"home/)(?P<user>[^/\s:\"'<>]+)"
    ),
)
PROXY_URI_RE = re.compile(
    r"(?i)\b(?:vless|vmess|trojan|ss|ssr|hysteria2|hy2|tuic|naive|"
    r"wireguard|wg|socks5)://[A-Za-z0-9._~:/?#\[\]@!$&()*+,;=%{}-]+"
)
HTTP_URL_RE = re.compile(r"(?i)https?://[^\s\"'<>]+")
SENSITIVE_QUERY_KEYS = {
    "access_token",
    "api_key",
    "auth",
    "key",
    "password",
    "passwd",
    "secret",
    "sub",
    "token",
}

HIGH_CONFIDENCE_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private-key", re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----")),
    ("github-classic-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b")),
    ("github-fine-grained-token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{70,255}\b")),
    ("gitlab-token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,255}\b")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,255}\b")),
    ("stripe-live-secret", re.compile(r"\bsk_live_[0-9A-Za-z]{20,255}\b")),
    ("telegram-bot-token", re.compile(r"\b[0-9]{8,12}:[A-Za-z0-9_-]{35}\b")),
    (
        "jwt",
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\."
            r"[A-Za-z0-9_-]{12,}\b"
        ),
    ),
    (
        "discord-webhook",
        re.compile(
            r"https://(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks/"
            r"[0-9]{8,22}/[A-Za-z0-9_-]{40,}",
            re.IGNORECASE,
        ),
    ),
)


class AuditFailure(RuntimeError):
    """A fail-closed audit error whose code is safe to print."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class DenyValue:
    index: int
    text: str
    byte_variants: tuple[bytes, ...]


@dataclass(frozen=True)
class Finding:
    rule: str
    source_kind: str
    location_id: str


@dataclass
class AuditReport:
    findings: list[Finding] = field(default_factory=list)
    total_findings: int = 0
    tracked_files: int = 0
    git_objects: int = 0
    artifact_files: int = 0
    archive_members: int = 0
    bytes_scanned: int = 0
    _seen: set[tuple[str, str, str]] = field(default_factory=set, repr=False)

    @property
    def passed(self) -> bool:
        return self.total_findings == 0

    def add(self, rule: str, source_kind: str, location: str) -> None:
        location_id = hashlib.sha256(
            location.encode("utf-8", errors="surrogatepass")
        ).hexdigest()[:16]
        key = (rule, source_kind, location_id)
        if key in self._seen:
            return
        self._seen.add(key)
        self.total_findings += 1
        if len(self.findings) < MAX_REPORTED_FINDINGS:
            self.findings.append(Finding(rule, source_kind, location_id))


@dataclass(frozen=True)
class AuditLimits:
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    max_archive_bytes: int = DEFAULT_MAX_ARCHIVE_BYTES
    max_archive_members: int = DEFAULT_MAX_ARCHIVE_MEMBERS
    max_archive_depth: int = DEFAULT_MAX_ARCHIVE_DEPTH


@dataclass(frozen=True)
class ExpectedSourceBundle:
    canonical_manifest: dict[str, object]
    source_hashes: dict[str, str]


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _git(repo_root: Path, *args: str) -> bytes:
    try:
        completed = subprocess.run(
            ["git", "-C", os.fspath(repo_root), *args],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise AuditFailure("git-unavailable") from exc
    if completed.returncode != 0:
        raise AuditFailure("git-command-failed")
    return completed.stdout


def resolve_repo_root(candidate: Path) -> Path:
    candidate = candidate.resolve(strict=True)
    output = _git(candidate, "rev-parse", "--show-toplevel")
    try:
        discovered = Path(output.decode("utf-8").strip()).resolve(strict=True)
    except (UnicodeDecodeError, OSError, ValueError) as exc:
        raise AuditFailure("invalid-repository-root") from exc
    if candidate != discovered:
        raise AuditFailure("repo-root-must-be-toplevel")
    shallow = _git(discovered, "rev-parse", "--is-shallow-repository")
    if shallow.strip() != b"false":
        raise AuditFailure("shallow-repository-refused")
    if os.environ.get("GIT_ALTERNATE_OBJECT_DIRECTORIES"):
        raise AuditFailure("git-object-alternates-refused")
    git_dir_raw = _git(discovered, "rev-parse", "--absolute-git-dir")
    try:
        git_dir = Path(git_dir_raw.decode("utf-8").strip()).resolve(strict=True)
    except (UnicodeDecodeError, OSError, ValueError) as exc:
        raise AuditFailure("invalid-git-directory") from exc
    alternates = git_dir / "objects" / "info" / "alternates"
    if alternates.exists():
        try:
            if alternates.read_bytes().strip():
                raise AuditFailure("git-object-alternates-refused")
        except AuditFailure:
            raise
        except OSError as exc:
            raise AuditFailure("git-alternates-unreadable") from exc
    config = _git(discovered, "config", "--local", "--list")
    if any(
        line.lower().endswith(b".promisor=true")
        for line in config.splitlines()
    ):
        raise AuditFailure("partial-clone-refused")
    return discovered


def _deny_variants(value: str) -> tuple[bytes, ...]:
    text_variants = {value}
    if "\\" in value:
        text_variants.add(value.replace("\\", "/"))
    if "/" in value:
        text_variants.add(value.replace("/", "\\"))

    variants: set[bytes] = set()
    for item in text_variants:
        raw = item.encode("utf-8")
        variants.update((raw, item.encode("utf-16-le"), item.encode("utf-16-be")))
        if len(raw) >= 8:
            encoded = base64.b64encode(raw)
            variants.add(encoded)
            variants.add(encoded.rstrip(b"="))
            url_encoded = base64.urlsafe_b64encode(raw)
            variants.add(url_encoded)
            variants.add(url_encoded.rstrip(b"="))
    return tuple(sorted((item for item in variants if item), key=lambda item: (len(item), item)))


def load_private_deny_file(path: Path, repo_root: Path) -> tuple[DenyValue, ...]:
    lexical = Path(os.path.abspath(path))
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise AuditFailure("deny-file-unreadable") from exc
    if not resolved.is_file():
        raise AuditFailure("deny-file-not-regular")
    if _is_relative_to(lexical, repo_root) or _is_relative_to(resolved, repo_root):
        raise AuditFailure("deny-file-inside-repository")
    try:
        payload = resolved.read_bytes()
    except OSError as exc:
        raise AuditFailure("deny-file-unreadable") from exc
    if not payload or len(payload) > MAX_DENY_FILE_BYTES:
        raise AuditFailure("deny-file-invalid-size")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise AuditFailure("deny-file-not-utf8") from exc

    values: list[DenyValue] = []
    seen: set[str] = set()
    for raw_line in text.splitlines():
        value = raw_line.strip()
        if not value or value.startswith("#"):
            continue
        if len(value) < 4 or len(value) > 4096 or "\x00" in value:
            raise AuditFailure("deny-file-invalid-entry")
        folded = value.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        values.append(DenyValue(len(values) + 1, value, _deny_variants(value)))
    if not values:
        raise AuditFailure("deny-file-empty")
    return tuple(values)


def _normalize_source_manifest(
    payload: object,
    *,
    required_components: frozenset[str] | None = REQUIRED_SOURCE_COMPONENTS,
) -> ExpectedSourceBundle:
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "components"}:
        raise AuditFailure("source-manifest-shape-invalid")
    if payload.get("schema_version") != 1:
        raise AuditFailure("source-manifest-version-invalid")
    raw_components = payload.get("components")
    if not isinstance(raw_components, list):
        raise AuditFailure("source-manifest-components-invalid")
    canonical_components: list[dict[str, str]] = []
    source_hashes: dict[str, str] = {}
    names: set[str] = set()
    for raw_component in raw_components:
        if not isinstance(raw_component, dict) or set(raw_component) != set(
            SOURCE_MANIFEST_FIELDS
        ):
            raise AuditFailure("source-manifest-component-invalid")
        component = {
            key: str(raw_component.get(key) or "").strip()
            for key in SOURCE_MANIFEST_FIELDS
        }
        if any(not value for value in component.values()):
            raise AuditFailure("source-manifest-component-empty")
        name = component["name"]
        version = component["version"]
        if (
            name in names
            or not re.fullmatch(r"[A-Za-z0-9._-]+", name)
            or not re.fullmatch(r"[A-Za-z0-9._-]+", version)
        ):
            raise AuditFailure("source-manifest-component-name-invalid")
        names.add(name)
        digest = component["sha256"].lower()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise AuditFailure("source-manifest-hash-invalid")
        source_path = PurePosixPath(component["source_path"].replace("\\", "/"))
        if (
            source_path.is_absolute()
            or ".." in source_path.parts
            or not source_path.name
        ):
            raise AuditFailure("source-manifest-path-invalid")
        component["source_path"] = source_path.as_posix()
        component["sha256"] = digest
        member_name = (
            PurePosixPath("sources")
            / f"{name}-{version}"
            / source_path.name
        ).as_posix()
        if member_name in source_hashes:
            raise AuditFailure("source-manifest-member-duplicate")
        source_hashes[member_name] = digest
        canonical_components.append(component)
    if required_components is not None and names != required_components:
        raise AuditFailure("source-manifest-component-set-invalid")
    canonical_components.sort(key=lambda item: item["name"])
    return ExpectedSourceBundle(
        canonical_manifest={"schema_version": 1, "components": canonical_components},
        source_hashes=source_hashes,
    )


def load_expected_source_bundle(repo_root: Path) -> ExpectedSourceBundle | None:
    manifest_path = repo_root / "packaging" / "third_party_sources.template.json"
    if not manifest_path.exists():
        return None
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise AuditFailure("expected-source-manifest-not-regular")
    try:
        payload = manifest_path.read_bytes()
    except OSError as exc:
        raise AuditFailure("expected-source-manifest-unreadable") from exc
    if not payload or len(payload) > MAX_DENY_FILE_BYTES:
        raise AuditFailure("expected-source-manifest-size-invalid")
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuditFailure("expected-source-manifest-invalid") from exc
    return _normalize_source_manifest(document)


def _safe_host(host: str | None) -> bool:
    if not host:
        return False
    normalized = unquote(host).lower().rstrip(".")
    if "." not in normalized and ":" not in normalized:
        return True
    if (
        normalized in {"localhost", "localhost.localdomain"}
        or normalized.endswith(".localhost")
        or normalized in {"example.com", "example.net", "example.org"}
        or normalized.endswith((".example.com", ".example.net", ".example.org"))
        or normalized.endswith((".example", ".invalid", ".test"))
    ):
        return True
    try:
        address = ipaddress.ip_address(normalized.strip("[]"))
    except ValueError:
        return False
    documentation_ranges = (
        ipaddress.ip_network("192.0.2.0/24"),
        ipaddress.ip_network("198.51.100.0/24"),
        ipaddress.ip_network("203.0.113.0/24"),
        ipaddress.ip_network("2001:db8::/32"),
    )
    return address.is_loopback or any(address in network for network in documentation_ranges)


def _looks_placeholder(value: str) -> bool:
    normalized = value.casefold()
    return any(
        marker in normalized
        for marker in (
            "${",
            "{",
            "}",
            "<token",
            "<secret",
            "<password",
            "example",
            "placeholder",
            "replace-me",
            "your-token",
        )
    )


def _opaque_proxy_host(uri: str) -> str | None:
    scheme, _, payload = uri.partition("://")
    if scheme.casefold() not in {"vmess", "ss", "ssr"} or not payload:
        return None
    encoded = payload.split("#", 1)[0].split("?", 1)[0]
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        decoded = base64.urlsafe_b64decode(padded).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    if scheme.casefold() == "vmess":
        try:
            document = json.loads(decoded)
        except json.JSONDecodeError:
            return None
        host = document.get("add") if isinstance(document, dict) else None
        return str(host) if host else None
    if scheme.casefold() == "ssr":
        if decoded.startswith("[") and "]" in decoded:
            return decoded[1 : decoded.index("]")]
        return decoded.split(":", 1)[0] if ":" in decoded else None
    endpoint = decoded.rsplit("@", 1)[-1]
    endpoint = endpoint.split("/", 1)[0]
    if endpoint.startswith("[") and "]" in endpoint:
        return endpoint[1 : endpoint.index("]")]
    return endpoint.rsplit(":", 1)[0] if ":" in endpoint else None


def _scan_structured_text(
    text: str,
    *,
    report: AuditReport,
    source_kind: str,
    location: str,
    allowed_local_users: frozenset[str],
) -> None:
    if not text:
        return
    for rule, pattern in HIGH_CONFIDENCE_RULES:
        if pattern.search(text):
            report.add(rule, source_kind, location)

    _scan_local_paths(
        text,
        report=report,
        source_kind=source_kind,
        location=location,
        allowed_local_users=allowed_local_users,
    )

    for match in PROXY_URI_RE.finditer(text):
        uri = match.group(0).rstrip(".,);]")
        if uri.endswith("://") or _looks_placeholder(uri):
            continue
        scheme = uri.split("://", 1)[0].casefold()
        try:
            host = urlsplit(uri).hostname
        except ValueError:
            host = None
        if scheme in {"vmess", "ss", "ssr"}:
            host = _opaque_proxy_host(uri) or host
        if not _safe_host(host):
            report.add("real-proxy-uri", source_kind, location)
            break

    for match in HTTP_URL_RE.finditer(text):
        url = match.group(0).rstrip(".,);]")
        if _looks_placeholder(url):
            continue
        try:
            parsed = urlsplit(url)
        except ValueError:
            continue
        if _safe_host(parsed.hostname):
            continue
        sensitive = False
        if parsed.username or parsed.password:
            sensitive = True
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            if key.casefold() in SENSITIVE_QUERY_KEYS and len(value) >= 6:
                sensitive = True
                break
        path_segments = [segment for segment in parsed.path.split("/") if segment]
        if (
            any(marker in parsed.path.casefold() for marker in ("/sub/", "/subscribe/"))
            and path_segments
            and len(path_segments[-1]) >= 16
        ):
            sensitive = True
        if sensitive:
            report.add("subscription-secret-url", source_kind, location)
            break


def _scan_local_paths(
    text: str,
    *,
    report: AuditReport,
    source_kind: str,
    location: str,
    allowed_local_users: frozenset[str],
) -> None:
    for pattern in LOCAL_PATH_PATTERNS:
        for match in pattern.finditer(text):
            user = match.group("user").casefold()
            if user not in SAFE_LOCAL_USERS and user not in allowed_local_users:
                report.add("local-user-path", source_kind, location)
                break


def _scan_deny_values(
    data: bytes,
    *,
    deny_values: Sequence[DenyValue],
    report: AuditReport,
    source_kind: str,
    location: str,
) -> None:
    lowered = data.lower()
    for item in deny_values:
        matched = False
        for variant in item.byte_variants:
            if variant in data or (variant.isascii() and variant.lower() in lowered):
                matched = True
                break
        if matched:
            report.add(f"caller-forbidden-{item.index}", source_kind, location)


def _utf16_printable_texts(data: bytes) -> Iterator[str]:
    """Yield only UTF-16 strings not already visible in UTF-8 decoding.

    ASCII runs survive ``decode(errors="ignore")`` even in arbitrary binary
    payloads, so scanning them again used to repeat every detector over most
    Git blobs and large executables.  UTF-16 strings still need their own pass.
    """

    for match in PRINTABLE_UTF16LE_RE.finditer(data):
        yield match.group(0).decode("utf-16-le")
    for match in PRINTABLE_UTF16BE_RE.finditer(data):
        yield match.group(0).decode("utf-16-be")


def scan_bytes(
    data: bytes,
    *,
    deny_values: Sequence[DenyValue],
    report: AuditReport,
    source_kind: str,
    location: str,
    allowed_local_users: frozenset[str],
    detector_mode: str = "full",
) -> None:
    if detector_mode not in {"full", "pinned-upstream", "pinned-binary"}:
        raise AuditFailure("detector-mode-invalid")
    report.bytes_scanned += len(data)
    _scan_deny_values(
        data,
        deny_values=deny_values,
        report=report,
        source_kind=source_kind,
        location=location,
    )
    try:
        decoded = data.decode("utf-8")
    except UnicodeDecodeError:
        decoded = data.decode("utf-8", errors="ignore")
    # Exact hash-pinned upstream bytes (reviewed source archives and binaries)
    # carry their authors' build and test paths (e.g. a CI home directory in
    # Mihomo's test configs); like pinned binaries they are checked for private deny
    # values only, which already ran above.
    detector = _scan_structured_text if detector_mode == "full" else None
    if detector is None:
        return
    detector(
        decoded,
        report=report,
        source_kind=source_kind,
        location=location,
        allowed_local_users=allowed_local_users,
    )
    for printable in _utf16_printable_texts(data):
        detector(
            printable,
            report=report,
            source_kind=source_kind,
            location=location,
            allowed_local_users=allowed_local_users,
        )


def _safe_archive_name(name: str) -> bool:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    return not path.is_absolute() and ".." not in path.parts and "\x00" not in name


def _is_hash_pinned_upstream_binary(data: bytes, name_hint: str) -> bool:
    digest = hashlib.sha256(data).hexdigest()
    expected = PINNED_UPSTREAM_BINARY_HASHES.get(Path(name_hint).name.casefold())
    if expected is not None:
        return digest == expected
    # Git's object database exposes a blob without its tree path.  The exact
    # reviewed upstream bytes are still identifiable by the pinned digest;
    # private deny values continue to be scanned in ``pinned-binary`` mode.
    return (
        name_hint == "object.blob"
        and digest in PINNED_UPSTREAM_BINARY_HASHES.values()
    )


def _validate_pinned_source_bundle(
    archive: zipfile.ZipFile,
    infos: Sequence[zipfile.ZipInfo],
    expected: ExpectedSourceBundle | None,
) -> dict[str, str]:
    if expected is None:
        raise AuditFailure("expected-source-manifest-missing")
    by_name = {info.filename: info for info in infos}
    if "SOURCE-MANIFEST.json" not in by_name:
        raise AuditFailure("pinned-source-member-set-mismatch")
    manifest_info = by_name["SOURCE-MANIFEST.json"]
    if (
        manifest_info.is_dir()
        or manifest_info.flag_bits & 0x1
        or manifest_info.file_size <= 0
        or manifest_info.file_size > MAX_DENY_FILE_BYTES
    ):
        raise AuditFailure("bundled-source-manifest-invalid")
    try:
        raw_manifest = archive.read(manifest_info)
        payload = json.loads(raw_manifest.decode("utf-8"))
    except (OSError, RuntimeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuditFailure("bundled-source-manifest-invalid") from exc
    actual = _normalize_source_manifest(payload, required_components=None)
    bundled = actual.canonical_manifest["components"]
    if {component["name"] for component in bundled} == REQUIRED_SOURCE_COMPONENTS:
        # Full template bundle: exactly the pinned manifest.
        if actual.canonical_manifest != expected.canonical_manifest:
            raise AuditFailure("bundled-source-manifest-mismatch")
        if actual.source_hashes != expected.source_hashes:
            raise AuditFailure("bundled-source-member-map-mismatch")
    else:
        # Shell bundle: the pinned components minus the Qt sources, byte-for-byte
        # as in the template, plus MPL-2.0 crates.io crates in their fixed layout.
        pinned = [
            component
            for component in expected.canonical_manifest["components"]
            if component["name"] not in SHELL_DROPPED_SOURCE_COMPONENTS
        ]
        base = [
            component
            for component in bundled
            if RUST_SOURCE_COMPONENT_RE.fullmatch(component["name"]) is None
        ]
        if base != pinned:
            raise AuditFailure("bundled-source-manifest-mismatch")
        for component in bundled:
            match = RUST_SOURCE_COMPONENT_RE.fullmatch(component["name"])
            if match is None:
                continue
            crate, version = match.group("crate"), component["version"]
            if (
                component["license"] != "MPL-2.0"
                or component["source_url"]
                != f"https://crates.io/api/v1/crates/{crate}/{version}/download"
                or component["source_path"]
                != f"{component['name']}-{version}-vendored-source.zip"
            ):
                raise AuditFailure("bundled-rust-source-component-invalid")
        pinned_hashes = {
            member: digest
            for member, digest in expected.source_hashes.items()
            if not any(
                member.startswith(f"sources/{name}-")
                for name in SHELL_DROPPED_SOURCE_COMPONENTS
            )
        }
        if any(actual.source_hashes.get(member) != digest for member, digest in pinned_hashes.items()):
            raise AuditFailure("bundled-source-member-map-mismatch")
    required_names = {
        "SOURCE-MANIFEST.json",
        *SOURCE_BUNDLE_DOCUMENTS,
        *actual.source_hashes,
    }
    if set(by_name) != required_names:
        raise AuditFailure("pinned-source-member-set-mismatch")
    cpython_prefix = (
        f"sources/{CPYTHON_WINDOWS_LICENSE_COMPONENT}-"
        f"{CPYTHON_WINDOWS_VERSION}/"
    )
    cpython_members = [
        name for name in expected.source_hashes if name.startswith(cpython_prefix)
    ]
    if len(cpython_members) != 1:
        raise AuditFailure("cpython-windows-license-member-invalid")
    try:
        cpython_license = archive.read(by_name[cpython_members[0]]).decode("utf-8")
    except (OSError, RuntimeError, UnicodeDecodeError) as exc:
        raise AuditFailure("cpython-windows-license-member-invalid") from exc
    if any(marker not in cpython_license for marker in CPYTHON_WINDOWS_LICENSE_MARKERS):
        raise AuditFailure("cpython-windows-license-acknowledgements-missing")
    return actual.source_hashes


def scan_zip_bytes(
    data: bytes,
    *,
    name_hint: str,
    deny_values: Sequence[DenyValue],
    report: AuditReport,
    source_kind: str,
    location: str,
    allowed_local_users: frozenset[str],
    limits: AuditLimits,
    depth: int = 0,
    detector_mode: str = "full",
    expected_source_bundle: ExpectedSourceBundle | None = None,
) -> None:
    if depth > limits.max_archive_depth:
        raise AuditFailure("archive-depth-limit")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (OSError, zipfile.BadZipFile) as exc:
        raise AuditFailure("malformed-zip") from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > limits.max_archive_members:
            raise AuditFailure("archive-member-limit")
        names: set[str] = set()
        for info in infos:
            if info.filename in names:
                raise AuditFailure("duplicate-archive-member")
            names.add(info.filename)
        is_pinned_source_bundle = (
            depth == 0
            and source_kind == "artifact"
            and SOURCE_BUNDLE_NAME_RE.fullmatch(Path(name_hint).name) is not None
        )
        pinned_source_hashes = (
            _validate_pinned_source_bundle(archive, infos, expected_source_bundle)
            if is_pinned_source_bundle
            else {}
        )
        total_uncompressed = 0
        scan_bytes(
            archive.comment,
            deny_values=deny_values,
            report=report,
            source_kind="archive-comment",
            location=f"{location}:comment",
            allowed_local_users=allowed_local_users,
            detector_mode=detector_mode,
        )
        for index, info in enumerate(infos):
            if not _safe_archive_name(info.filename):
                raise AuditFailure("unsafe-archive-member")
            if info.flag_bits & 0x1:
                raise AuditFailure("encrypted-archive-member")
            total_uncompressed += info.file_size
            if (
                info.file_size > limits.max_file_bytes
                or total_uncompressed > limits.max_archive_bytes
            ):
                raise AuditFailure("archive-size-limit")
            member_location = f"{location}:member:{index}"
            scan_bytes(
                info.filename.encode("utf-8", errors="surrogatepass"),
                deny_values=deny_values,
                report=report,
                source_kind="archive-name",
                location=member_location,
                allowed_local_users=allowed_local_users,
            )
            if info.is_dir():
                continue
            try:
                with archive.open(info, "r") as member:
                    payload = member.read(limits.max_file_bytes + 1)
            except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                raise AuditFailure("archive-member-unreadable") from exc
            if len(payload) != info.file_size or len(payload) > limits.max_file_bytes:
                raise AuditFailure("archive-member-size-mismatch")
            report.archive_members += 1
            member_detector_mode = (
                # Template-pinned upstream sources only; the shell bundle's Rust
                # crates are hash-checked against the bundle manifest but scanned fully.
                "pinned-upstream"
                if info.filename in pinned_source_hashes
                and not info.filename.startswith("sources/rust-")
                else "pinned-binary"
                if _is_hash_pinned_upstream_binary(payload, info.filename)
                else detector_mode
            )
            if info.filename in pinned_source_hashes:
                actual_hash = hashlib.sha256(payload).hexdigest()
                if actual_hash != pinned_source_hashes[info.filename]:
                    raise AuditFailure("pinned-source-hash-mismatch")
            scan_bytes(
                payload,
                deny_values=deny_values,
                report=report,
                source_kind="archive-member",
                location=member_location,
                allowed_local_users=allowed_local_users,
                detector_mode=member_detector_mode,
            )
            if payload.startswith(ZIP_MAGICS):
                scan_zip_bytes(
                    payload,
                    name_hint=info.filename,
                    deny_values=deny_values,
                    report=report,
                    source_kind="nested-archive",
                    location=member_location,
                    allowed_local_users=allowed_local_users,
                    limits=limits,
                    depth=depth + 1,
                    detector_mode=member_detector_mode,
                    expected_source_bundle=expected_source_bundle,
                )


def scan_payload(
    data: bytes,
    *,
    name_hint: str,
    deny_values: Sequence[DenyValue],
    report: AuditReport,
    source_kind: str,
    location: str,
    allowed_local_users: frozenset[str],
    limits: AuditLimits,
    detector_mode: str = "full",
    expected_source_bundle: ExpectedSourceBundle | None = None,
) -> None:
    if detector_mode == "full" and _is_hash_pinned_upstream_binary(data, name_hint):
        detector_mode = "pinned-binary"
    scan_bytes(
        data,
        deny_values=deny_values,
        report=report,
        source_kind=source_kind,
        location=location,
        allowed_local_users=allowed_local_users,
        detector_mode=detector_mode,
    )
    suffix = Path(name_hint).suffix.casefold()
    if suffix in {".key", ".p12", ".pfx"}:
        report.add("private-key-container", source_kind, location)
    is_zip = data.startswith(ZIP_MAGICS)
    if suffix in ZIP_SUFFIXES and not is_zip:
        raise AuditFailure("zip-extension-without-zip-data")
    if is_zip:
        scan_zip_bytes(
            data,
            name_hint=name_hint,
            deny_values=deny_values,
            report=report,
            source_kind=source_kind,
            location=location,
            allowed_local_users=allowed_local_users,
            limits=limits,
            detector_mode=detector_mode,
            expected_source_bundle=expected_source_bundle,
        )


def audit_git_identities(
    repo_root: Path,
    *,
    report: AuditReport,
    allowed_names: frozenset[str],
    allowed_emails: frozenset[str],
) -> None:
    payload = _git(
        repo_root,
        "log",
        "--all",
        "-z",
        "--format=%H%x1f%an%x1f%ae%x1f%cn%x1f%ce",
    )
    for record_index, raw_record in enumerate(payload.split(b"\x00")):
        if not raw_record:
            continue
        try:
            fields = raw_record.decode("utf-8").split("\x1f")
        except UnicodeDecodeError as exc:
            raise AuditFailure("git-identity-not-utf8") from exc
        if len(fields) != 5:
            raise AuditFailure("git-identity-malformed")
        _, author_name, author_email, committer_name, committer_email = fields
        location = f"git-identity:{record_index}"
        if author_name not in allowed_names:
            report.add("git-author-name", "git-identity", location)
        if committer_name not in allowed_names:
            report.add("git-committer-name", "git-identity", location)
        for role, email in (("author", author_email), ("committer", committer_email)):
            if email.casefold() in allowed_emails:
                continue
            if not GITHUB_ID_NOREPLY_RE.fullmatch(email):
                report.add(f"git-{role}-email", "git-identity", location)


def audit_tracked_tree(
    repo_root: Path,
    *,
    deny_values: Sequence[DenyValue],
    report: AuditReport,
    allowed_local_users: frozenset[str],
    limits: AuditLimits,
) -> None:
    entries = _git(repo_root, "ls-files", "-z", "--cached").split(b"\x00")
    for index, raw_name in enumerate(entry for entry in entries if entry):
        try:
            relative = raw_name.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AuditFailure("tracked-path-not-utf8") from exc
        logical_location = f"tracked:{index}"
        scan_bytes(
            raw_name,
            deny_values=deny_values,
            report=report,
            source_kind="tracked-path",
            location=logical_location,
            allowed_local_users=allowed_local_users,
        )
        candidate = (repo_root / relative)
        try:
            candidate.relative_to(repo_root)
        except ValueError as exc:
            raise AuditFailure("unsafe-tracked-path") from exc
        if candidate.is_symlink():
            try:
                target = os.readlink(candidate)
            except OSError as exc:
                raise AuditFailure("tracked-symlink-unreadable") from exc
            scan_bytes(
                os.fsencode(target),
                deny_values=deny_values,
                report=report,
                source_kind="tracked-symlink",
                location=logical_location,
                allowed_local_users=allowed_local_users,
            )
            report.tracked_files += 1
            continue
        if not candidate.is_file():
            raise AuditFailure("tracked-file-missing-or-nonregular")
        try:
            size = candidate.stat().st_size
            if size > limits.max_file_bytes:
                raise AuditFailure("tracked-file-size-limit")
            data = candidate.read_bytes()
        except AuditFailure:
            raise
        except OSError as exc:
            raise AuditFailure("tracked-file-unreadable") from exc
        if len(data) != size:
            raise AuditFailure("tracked-file-changed-during-audit")
        report.tracked_files += 1
        scan_payload(
            data,
            name_hint=relative,
            deny_values=deny_values,
            report=report,
            source_kind="tracked-file",
            location=logical_location,
            allowed_local_users=allowed_local_users,
            limits=limits,
        )


def _all_git_objects(repo_root: Path) -> list[tuple[str, str, int]]:
    payload = _git(
        repo_root,
        "cat-file",
        "--batch-all-objects",
        "--batch-check=%(objectname) %(objecttype) %(objectsize)",
    )
    objects: list[tuple[str, str, int]] = []
    for line in payload.splitlines():
        fields = line.split(b" ")
        if len(fields) != 3:
            raise AuditFailure("git-object-list-malformed")
        try:
            oid = fields[0].decode("ascii")
            object_type = fields[1].decode("ascii")
            size = int(fields[2])
        except (UnicodeDecodeError, ValueError) as exc:
            raise AuditFailure("git-object-list-malformed") from exc
        if not re.fullmatch(r"[0-9a-f]{40,64}", oid):
            raise AuditFailure("git-object-id-malformed")
        if object_type not in {"blob", "commit", "tag", "tree"} or size < 0:
            raise AuditFailure("git-object-type-unsupported")
        objects.append((oid, object_type, size))
    return objects


def _read_exact(stream: io.BufferedReader, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise AuditFailure("git-object-truncated")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def audit_all_git_objects(
    repo_root: Path,
    *,
    deny_values: Sequence[DenyValue],
    report: AuditReport,
    allowed_local_users: frozenset[str],
    limits: AuditLimits,
) -> None:
    objects = _all_git_objects(repo_root)
    try:
        process = subprocess.Popen(
            ["git", "-C", os.fspath(repo_root), "cat-file", "--batch"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise AuditFailure("git-unavailable") from exc
    if process.stdin is None or process.stdout is None:
        process.kill()
        raise AuditFailure("git-batch-unavailable")
    try:
        for index, (oid, expected_type, expected_size) in enumerate(objects):
            if expected_size > limits.max_file_bytes:
                raise AuditFailure("git-object-size-limit")
            process.stdin.write(oid.encode("ascii") + b"\n")
            process.stdin.flush()
            header = process.stdout.readline().rstrip(b"\n")
            fields = header.split(b" ")
            if len(fields) != 3:
                raise AuditFailure("git-object-header-malformed")
            try:
                actual_oid = fields[0].decode("ascii")
                actual_type = fields[1].decode("ascii")
                actual_size = int(fields[2])
            except (UnicodeDecodeError, ValueError) as exc:
                raise AuditFailure("git-object-header-malformed") from exc
            if (actual_oid, actual_type, actual_size) != (
                oid,
                expected_type,
                expected_size,
            ):
                raise AuditFailure("git-object-header-mismatch")
            data = _read_exact(process.stdout, actual_size)
            if process.stdout.read(1) != b"\n":
                raise AuditFailure("git-object-protocol-error")
            report.git_objects += 1
            scan_payload(
                data,
                name_hint=f"object.{actual_type}",
                deny_values=deny_values,
                report=report,
                source_kind="git-object",
                location=f"git-object:{index}:{oid}",
                allowed_local_users=allowed_local_users,
                limits=limits,
            )
    finally:
        try:
            process.stdin.close()
        except OSError:
            pass
        try:
            return_code = process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
            raise AuditFailure("git-batch-timeout")
    if return_code != 0:
        raise AuditFailure("git-batch-failed")


def _refuses_user_data(path: Path, repo_root: Path) -> bool:
    relative = path.relative_to(repo_root)
    folded_parts = tuple(part.casefold() for part in relative.parts)
    if any(part in REFUSED_USER_DATA_DIRS for part in folded_parts):
        return True
    return path.name.casefold() in REFUSED_USER_DATA_NAMES


def _artifact_files(path: Path, repo_root: Path) -> Iterator[Path]:
    lexical = Path(os.path.abspath(path))
    if not _is_relative_to(lexical, repo_root) or lexical == repo_root:
        raise AuditFailure("artifact-must-be-inside-repository")
    cursor = repo_root
    for part in lexical.relative_to(repo_root).parts:
        cursor /= part
        if cursor.is_symlink():
            raise AuditFailure("artifact-symlink-refused")
    try:
        resolved = lexical.resolve(strict=True)
    except OSError as exc:
        raise AuditFailure("artifact-unreadable") from exc
    if not _is_relative_to(resolved, repo_root) or resolved == repo_root:
        raise AuditFailure("artifact-must-be-inside-repository")
    if _refuses_user_data(resolved, repo_root):
        raise AuditFailure("artifact-user-data-refused")
    if resolved.is_symlink():
        raise AuditFailure("artifact-symlink-refused")
    if resolved.is_file():
        yield resolved
        return
    if not resolved.is_dir():
        raise AuditFailure("artifact-not-regular")
    for current_root, dir_names, file_names in os.walk(resolved, followlinks=False):
        current = Path(current_root)
        for name in tuple(dir_names):
            child = current / name
            if child.is_symlink():
                raise AuditFailure("artifact-symlink-refused")
            if _refuses_user_data(child, repo_root):
                raise AuditFailure("artifact-user-data-refused")
        for name in sorted(file_names):
            child = current / name
            if child.is_symlink() or not child.is_file():
                raise AuditFailure("artifact-nonregular-refused")
            if _refuses_user_data(child, repo_root):
                raise AuditFailure("artifact-user-data-refused")
            yield child


def audit_artifacts(
    repo_root: Path,
    artifact_paths: Sequence[Path],
    *,
    deny_values: Sequence[DenyValue],
    report: AuditReport,
    allowed_local_users: frozenset[str],
    limits: AuditLimits,
    expected_source_bundle: ExpectedSourceBundle | None,
) -> None:
    seen: set[Path] = set()
    artifact_index = 0
    for requested in artifact_paths:
        candidate = requested if requested.is_absolute() else repo_root / requested
        for path in _artifact_files(candidate, repo_root):
            resolved = path.resolve(strict=True)
            if resolved in seen:
                continue
            seen.add(resolved)
            try:
                size = resolved.stat().st_size
                if size > limits.max_file_bytes:
                    raise AuditFailure("artifact-file-size-limit")
                data = resolved.read_bytes()
            except AuditFailure:
                raise
            except OSError as exc:
                raise AuditFailure("artifact-unreadable") from exc
            if len(data) != size:
                raise AuditFailure("artifact-changed-during-audit")
            relative = resolved.relative_to(repo_root).as_posix()
            report.artifact_files += 1
            scan_payload(
                data,
                name_hint=relative,
                deny_values=deny_values,
                report=report,
                source_kind="artifact",
                location=f"artifact:{artifact_index}",
                allowed_local_users=allowed_local_users,
                limits=limits,
                expected_source_bundle=expected_source_bundle,
            )
            artifact_index += 1


def audit_public_release(
    *,
    repo_root: Path,
    deny_file: Path,
    artifact_paths: Sequence[Path] = (),
    allowed_git_names: Iterable[str] = (DEFAULT_ALLOWED_GIT_NAME,),
    allowed_git_emails: Iterable[str] = (),
    allowed_local_users: Iterable[str] = (),
    limits: AuditLimits = AuditLimits(),
) -> AuditReport:
    root = resolve_repo_root(repo_root)
    deny_values = load_private_deny_file(deny_file, root)
    expected_source_bundle = load_expected_source_bundle(root)
    names = frozenset(name for name in allowed_git_names if name)
    if not names:
        raise AuditFailure("allowed-git-name-empty")
    emails = frozenset(email.casefold() for email in allowed_git_emails if email)
    if any(not GITHUB_ID_NOREPLY_RE.fullmatch(email) for email in emails):
        raise AuditFailure("allowed-git-email-not-id-noreply")
    local_users = frozenset(user.casefold() for user in allowed_local_users if user)
    report = AuditReport()
    audit_git_identities(
        root,
        report=report,
        allowed_names=names,
        allowed_emails=emails,
    )
    audit_tracked_tree(
        root,
        deny_values=deny_values,
        report=report,
        allowed_local_users=local_users,
        limits=limits,
    )
    audit_all_git_objects(
        root,
        deny_values=deny_values,
        report=report,
        allowed_local_users=local_users,
        limits=limits,
    )
    audit_artifacts(
        root,
        artifact_paths,
        deny_values=deny_values,
        report=report,
        allowed_local_users=local_users,
        limits=limits,
        expected_source_bundle=expected_source_bundle,
    )
    return report


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only, fail-closed anonymity/secret audit. Matched values are never "
            "printed. The required private deny-file must be outside the repository."
        )
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--deny-file", type=Path, required=True)
    parser.add_argument(
        "--artifact",
        dest="artifacts",
        action="append",
        type=Path,
        default=[],
        help=(
            "Explicit release file/directory inside the repository; repeat for dist, "
            "ZIP, installer, logs, and other release evidence."
        ),
    )
    parser.add_argument(
        "--allowed-git-name",
        action="append",
        default=[],
        help=(
            "Exact project pseudonym. Defaults to 'Tesseract contributors'; repeat "
            "only for an approved project identity."
        ),
    )
    parser.add_argument(
        "--allowed-git-email",
        action="append",
        default=[],
        help=(
            "Additional exact project noreply address. GitHub numeric-ID noreply "
            "addresses are accepted automatically."
        ),
    )
    parser.add_argument(
        "--allowed-local-user",
        action="append",
        default=[],
        help="Additional non-personal build username allowed in embedded local paths.",
    )
    parser.add_argument(
        "--max-file-bytes", type=_positive_int, default=DEFAULT_MAX_FILE_BYTES
    )
    parser.add_argument(
        "--max-archive-bytes", type=_positive_int, default=DEFAULT_MAX_ARCHIVE_BYTES
    )
    parser.add_argument(
        "--max-archive-members", type=_positive_int, default=DEFAULT_MAX_ARCHIVE_MEMBERS
    )
    parser.add_argument(
        "--max-archive-depth", type=_positive_int, default=DEFAULT_MAX_ARCHIVE_DEPTH
    )
    return parser


def _emit_report(report: AuditReport) -> None:
    status = "PASS" if report.passed else "FAIL"
    print(
        "PUBLIC_RELEASE_AUDIT="
        f"{status} findings={report.total_findings} "
        f"tracked_files={report.tracked_files} git_objects={report.git_objects} "
        f"artifact_files={report.artifact_files} "
        f"archive_members={report.archive_members} bytes_scanned={report.bytes_scanned}"
    )
    for finding in report.findings:
        print(
            f"finding rule={finding.rule} source={finding.source_kind} "
            f"location_id={finding.location_id}"
        )
    omitted = report.total_findings - len(report.findings)
    if omitted > 0:
        print(f"findings_omitted={omitted}")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    names = args.allowed_git_name or [DEFAULT_ALLOWED_GIT_NAME]
    limits = AuditLimits(
        max_file_bytes=args.max_file_bytes,
        max_archive_bytes=args.max_archive_bytes,
        max_archive_members=args.max_archive_members,
        max_archive_depth=args.max_archive_depth,
    )
    try:
        report = audit_public_release(
            repo_root=args.repo_root,
            deny_file=args.deny_file,
            artifact_paths=args.artifacts,
            allowed_git_names=names,
            allowed_git_emails=args.allowed_git_email,
            allowed_local_users=args.allowed_local_user,
            limits=limits,
        )
    except AuditFailure as exc:
        print(f"PUBLIC_RELEASE_AUDIT=ERROR code={exc.code}", file=sys.stderr)
        return 2
    except Exception:
        print("PUBLIC_RELEASE_AUDIT=ERROR code=internal-error", file=sys.stderr)
        return 2
    _emit_report(report)
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
