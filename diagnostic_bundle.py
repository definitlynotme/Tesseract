"""A diagnostic bundle that is safe to hand to someone else.

One zip holds what a helper needs to understand a problem: the recent
monitor log and structured events, the settings files, the runtime status
and a few small state files.  Anything that would let the recipient use the
user's nodes, subscriptions or alert channels is replaced by an opaque
``[redacted:…]`` marker before it is written.  The same secret gets the same
marker in every file, so a subscription named in the settings can still be
matched to its log lines.  Host names and IP addresses stay: connectivity
problems are made of them, and the export notice says so.
"""

from __future__ import annotations

import ipaddress
import json
import os
import platform
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from diagnostic_privacy import runtime_identity_hash, runtime_scoped_hmac


BUNDLE_SCHEMA_VERSION = 1
# Per log family (current file plus its first rotation).
LOG_TAIL_BYTES = 8 * 1024 * 1024
STATE_FILE_MAX_BYTES = 4 * 1024 * 1024
_MARKER_NAMESPACE = "diagnostic-bundle"
_MIN_LITERAL_LENGTH = 4

# Keys whose value is a credential or points at the user's own endpoint.
# Matched on the lower-cased key with "-" folded to "_" (Mihomo spells
# ``short-id``, settings spell ``chat_id``).
_SENSITIVE_KEY = re.compile(
    r"(?:^|_)(?:password|passwd|passphrase|secret|secret_?key|access_?key|token"
    r"|api_?key|private_?key|public_?key|(?:pre_)?shared_?key|psk|pbk|sid|short_?id"
    r"|uuid|auth|auth_?str|authorization|cookie|credentials?|topic|chat_?id"
    r"|phone(?:_number(?:_id)?)?|username|user|to|address|server)$"
)
_ENDPOINT_KEYS = frozenset({"address", "server"})
# Inside these settings subtrees a link is the credential itself: a
# subscription URL or a node share link.
_LINK_SUBTREES = frozenset({"subscription_urls", "nodes"})
_LINK_KEYS = frozenset({"uri", "url"})
_TRIVIAL_VALUES = frozenset(
    {"true", "false", "none", "null", "auto", "default", "enabled", "disabled"}
)

_URI_RE = re.compile(r"(?i)\b(?P<scheme>[a-z][a-z0-9+.-]{1,15})://(?P<rest>[^\s\"'<>]+)")
_BARE_SHARE_LINK_RE = re.compile(r"(?i)\b(?:ss|ssr|vmess|vless|trojan):(?!//)[^\s\"'<>]+")
_AUTHORITY_RE = re.compile(
    r"(?P<userinfo>[^@/?#\s]*@)?(?P<host>\[[^\]]*\]|[^/?#:@\s]+)(?P<port>:\d+)?(?P<tail>.*)$",
    re.DOTALL,
)
_UUID_RE = re.compile(
    r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b"
)
_EMAIL_RE = re.compile(
    r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b"
)
_BOT_TOKEN_RE = re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")
# Schemes whose authority is only an address: web and DNS transports.  Any
# other scheme (ss://, vmess://, socks5://user:pass@proxy.example) is a credential.
_WEB_SCHEMES = frozenset({"http", "https", "ws", "wss", "tls", "quic", "udp", "tcp", "h3"})
# Paths that name a public service endpoint, never an account.
_PUBLIC_PATHS = frozenset({"", "/", "/dns-query", "/resolve", "/generate_204", "/cdn-cgi/trace"})
# Public code and CDN hosts serve the Tier3 source lists; their paths say
# which list failed and carry no identity once any query is dropped.
_PUBLIC_CODE_HOSTS = frozenset(
    {
        "github.com",
        "raw.githubusercontent.com",
        "gist.githubusercontent.com",
        "gitlab.com",
        "codeberg.org",
        "cdn.jsdelivr.net",
        "fastly.jsdelivr.net",
        "gcore.jsdelivr.net",
        "testingcf.jsdelivr.net",
    }
)
# Hosts where only some paths are public: a Telegram channel page is, an
# invite link (``t.me/+…``, ``t.me/joinchat/…``) is not.
_PUBLIC_PATH_RULES: Mapping[str, re.Pattern[str]] = {
    "t.me": re.compile(r"^/(?:s/)?[A-Za-z][A-Za-z0-9_]{3,}/?$"),
}
_TRAILING_PUNCTUATION = ".,;:!?)]}'\""

REDACTION_POLICY = (
    "Passwords, tokens, secrets, UUIDs, alert channel identifiers, subscription links "
    "and node share links are replaced by [redacted:<id>] markers; one value keeps one "
    "marker across all files.",
    "Web addresses keep their scheme and host; paths and queries are replaced, except "
    "the paths of public code hosts that serve node lists.",
    "The addresses of the user's own nodes and upstream proxy are replaced; loopback "
    "and private addresses are kept.",
    "Host names and IP addresses that appear in log lines are kept.",
    "The home directory is shown as %USERPROFILE%.",
)


def _normalized_key(key: Any) -> str:
    return str(key).strip().lower().replace("-", "_")


def _is_sensitive_key(key: Any) -> bool:
    return bool(_SENSITIVE_KEY.search(_normalized_key(key)))


# Addresses that identify no one outside this machine or LAN.  Spelled out:
# ``ipaddress.is_private`` also covers documentation and benchmark ranges.
_LOCAL_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in (
        "0.0.0.0/32",
        "127.0.0.0/8",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",
        "::/128",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    )
)


def _is_local_address(value: str) -> bool:
    host = str(value or "").strip().lower()
    if host in {"", "localhost"}:
        return True
    if host.startswith("["):
        host = host[1:].split("]", 1)[0]
    elif host.count(":") == 1:
        host = host.split(":", 1)[0]
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host == "localhost"
    return any(address in network for network in _LOCAL_NETWORKS)


def _keeps_path(host: str, path: str, *, userinfo: str) -> bool:
    host = host.lower()
    if path in _PUBLIC_PATHS or _is_local_address(host):
        return True
    if userinfo:
        return False
    rule = _PUBLIC_PATH_RULES.get(host)
    return host in _PUBLIC_CODE_HOSTS or bool(rule is not None and rule.match(path))


def _is_public_link(text: str) -> bool:
    """A link that names a public list and carries no credential."""

    match = _URI_RE.fullmatch(text.strip())
    if match is None or match.group("scheme").lower() not in _WEB_SCHEMES:
        return False
    parts = _AUTHORITY_RE.match(match.group("rest"))
    if parts is None or parts.group("userinfo"):
        return False
    tail = parts.group("tail") or ""
    host = parts.group("host")
    if "?" in tail or "#" in tail or _is_local_address(host):
        return False
    return _keeps_path(host, tail, userinfo="")


def _covered_by_general_rules(text: str) -> bool:
    """Values the text rules already replace whole, with the same marker."""

    if _UUID_RE.fullmatch(text) or _BARE_SHARE_LINK_RE.fullmatch(text):
        return True
    match = _URI_RE.fullmatch(text)
    return bool(
        match is not None
        and match.group("scheme").lower() not in _WEB_SCHEMES
        and text[-1] not in _TRAILING_PUNCTUATION
    )


def _home_pattern(home: str | None) -> re.Pattern[str] | None:
    parts = [part for part in re.split(r"[\\/]+", str(home or "").strip()) if part]
    if len(parts) < 2:
        return None
    body = r"[\\/]+".join(re.escape(part) for part in parts)
    return re.compile(body + r"(?![A-Za-z0-9_])", re.IGNORECASE)


class BundleRedactor:
    """Replace private values with stable markers across every bundle file."""

    def __init__(self, *, home: str | None = None) -> None:
        self._literals: set[str] = set()
        self._literal_re: re.Pattern[str] | None = None
        self._home_re = _home_pattern(home)
        self.markers = 0

    # ------------------------------------------------------------- learning
    def learn(self, payload: Any) -> None:
        """Collect the private values of a settings document.

        Log lines do not say which words are secrets; the settings do.  Every
        value learnt here is replaced wherever it appears in any file.
        """

        self._learn(payload, key="", secret=False, links=False)
        literals = sorted(self._literals, key=len, reverse=True)
        self._literal_re = (
            re.compile(
                r"(?<![A-Za-z0-9])(?:"
                + "|".join(re.escape(value) for value in literals)
                + r")(?![A-Za-z0-9])"
            )
            if literals
            else None
        )

    def _learn(self, value: Any, *, key: str, secret: bool, links: bool) -> None:
        if isinstance(value, Mapping):
            for raw_key, child in value.items():
                child_key = _normalized_key(raw_key)
                self._learn(
                    child,
                    key=child_key,
                    # A sensitive key marks its value, never a whole section
                    # (``subscription_server`` holds ports and flags too).
                    secret=_is_sensitive_key(child_key) and not isinstance(child, Mapping),
                    links=links or child_key in _LINK_SUBTREES,
                )
            return
        if isinstance(value, (list, tuple)):
            for child in value:
                self._learn(
                    child,
                    key=key,
                    secret=secret and not isinstance(child, Mapping),
                    links=links,
                )
            return
        if value is None or isinstance(value, bool):
            return
        text = str(value).strip()
        if not (secret or (links and (key in _LINK_KEYS or "://" in text))):
            return
        if not secret and _is_public_link(text):
            # A public node list on GitHub says which source failed and
            # holds no credential; the web-address rule still applies.
            return
        if _covered_by_general_rules(text):
            # UUIDs and share links get the same marker from the general
            # rules; keeping them out keeps hundreds of nodes cheap to scan.
            return
        if (
            len(text) < _MIN_LITERAL_LENGTH
            or text.lower() in _TRIVIAL_VALUES
            or (key in _ENDPOINT_KEYS and _is_local_address(text))
        ):
            return
        self._literals.add(text)

    # ------------------------------------------------------------ redaction
    def marker(self, value: str) -> str:
        self.markers += 1
        digest = runtime_scoped_hmac(value, namespace=_MARKER_NAMESPACE, length=12)
        return f"[redacted:{digest}]"

    def text(self, value: Any) -> str:
        text = str(value or "")
        if not text:
            return text
        if self._literal_re is not None:
            text = self._literal_re.sub(lambda match: self.marker(match.group(0)), text)
        text = _URI_RE.sub(self._redact_uri, text)
        text = _BARE_SHARE_LINK_RE.sub(lambda match: self.marker(match.group(0)), text)
        text = _UUID_RE.sub(lambda match: self.marker(match.group(0)), text)
        text = _EMAIL_RE.sub(self._redact_email, text)
        text = _BOT_TOKEN_RE.sub(lambda match: self.marker(match.group(0)), text)
        if self._home_re is not None:
            text = self._home_re.sub("%USERPROFILE%", text)
        return text

    def _redact_email(self, match: re.Match[str]) -> str:
        following = match.string[match.end() : match.end() + 2]
        if len(following) == 2 and following[0] == ":" and following[1].isdigit():
            # "name@host:port" labels a node; a mailbox never has a port.
            return match.group(0)
        return self.marker(match.group(0))

    def _redact_uri(self, match: re.Match[str]) -> str:
        scheme = match.group("scheme")
        rest = match.group("rest")
        trailing = ""
        while rest and rest[-1] in _TRAILING_PUNCTUATION:
            trailing = rest[-1] + trailing
            rest = rest[:-1]
        if not rest:
            return match.group(0)
        if scheme.lower() not in _WEB_SCHEMES:
            # Share links and proxy URLs are credentials from end to end.
            return self.marker(f"{scheme}://{rest}") + trailing
        parts = _AUTHORITY_RE.match(rest)
        if parts is None:
            return self.marker(f"{scheme}://{rest}") + trailing
        userinfo = parts.group("userinfo") or ""
        host = parts.group("host")
        port = parts.group("port") or ""
        tail = parts.group("tail") or ""
        output = f"{scheme}://"
        if userinfo:
            output += self.marker(userinfo[:-1]) + "@"
        output += host + port
        path, query_mark, query = tail.partition("?")
        path, fragment_mark, fragment = path.partition("#")
        if _keeps_path(host, path, userinfo=userinfo):
            output += path
        else:
            output += "/" + self.marker(path)
        if query_mark:
            output += "?" + self.marker(query)
        if fragment_mark:
            output += "#" + self.marker(fragment)
        return output + trailing

    def scrub(self, value: Any, *, key: str = "", private: bool = False) -> Any:
        """Redact a parsed document without changing its shape."""

        if isinstance(value, Mapping):
            identity_keyed = "by_identity" in key
            output: dict[str, Any] = {}
            for raw_key, child in value.items():
                child_key = _normalized_key(raw_key)
                if child_key == "identity" or (
                    child_key.endswith("_identity")
                    and not child_key.endswith("_identity_hash")
                    and not child_key.endswith("by_identity")
                ):
                    text = str(child or "")
                    output[f"{raw_key}_hash"] = (
                        text if text.startswith("hmac:") else runtime_identity_hash(text)
                    )
                    continue
                if identity_keyed:
                    text_key = str(raw_key)
                    output_key = (
                        text_key if text_key.startswith("hmac:") else runtime_identity_hash(text_key)
                    )
                else:
                    output_key = self.text(raw_key)
                output[output_key] = self.scrub(
                    child,
                    key=child_key,
                    private=_is_sensitive_key(child_key) and not isinstance(child, Mapping),
                )
            return output
        if isinstance(value, (list, tuple)):
            if key.endswith("_identities"):
                return [
                    item if str(item).startswith("hmac:") else runtime_identity_hash(item)
                    for item in value
                ]
            return [
                self.scrub(item, key=key, private=private and not isinstance(item, Mapping))
                for item in value
            ]
        if value is None or isinstance(value, bool):
            return value
        if private:
            text = str(value).strip()
            if not text or (key in _ENDPOINT_KEYS and _is_local_address(text)):
                return self.text(value) if isinstance(value, str) else value
            if text.startswith("[redacted:"):
                return text
            return self.marker(text)
        if isinstance(value, str):
            return self.text(value)
        return value


_ATTACHMENT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


@dataclass(frozen=True)
class DiagnosticAttachment:
    """An extra log the caller already holds in memory (shell, sidecar, bridge).

    The writer scrubs it like every other file.  A caller that reads a log
    file outside the writer must redact it line by line before handing it
    over; the writer's own pass is a second line, never the only one.
    """

    name: str
    text: str
    jsonl: bool = False
    source_bytes: int | None = None
    truncated: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _ATTACHMENT_NAME.fullmatch(self.name):
            raise ValueError("attachment name must be a plain file name")
        if not isinstance(self.text, str):
            raise TypeError("attachment text must be a string")


@dataclass(frozen=True)
class DiagnosticBundleInputs:
    """What the window hands to the (background) bundle writer."""

    log_file: Path | None = None
    events_file: Path | None = None
    config_files: Mapping[str, Path] = field(default_factory=dict)
    state_files: Mapping[str, Path] = field(default_factory=dict)
    runtime_status: Mapping[str, Any] | None = None
    key_events_text: str = ""
    app: Mapping[str, Any] = field(default_factory=dict)
    home: str | None = None
    # Optional, additive (P2-19): extra in-memory logs under ``logs/`` and the
    # wall-clock epoch milliseconds at which each input was snapshotted.
    # Both default to empty, which leaves the bundle exactly as before.
    extra_logs: tuple[DiagnosticAttachment, ...] = ()
    snapshot_times: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class DiagnosticBundleResult:
    path: Path
    size: int
    files: tuple[str, ...]
    markers: int


def _read_bytes(path: Path | None) -> bytes | None:
    if path is None:
        return None
    try:
        return Path(path).read_bytes()
    except OSError:
        return None


def _read_tail(path: Path, budget: int) -> tuple[str, int, bool] | None:
    """Last ``budget`` bytes of ``path``, starting on a line boundary."""

    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > budget:
                handle.seek(size - budget)
                data = handle.read()
                newline = data.find(b"\n")
                data = data[newline + 1 :] if newline >= 0 else data
                truncated = True
            else:
                data = handle.read()
                truncated = False
    except OSError:
        return None
    return data.decode("utf-8", errors="replace"), size, truncated


def _log_family(path: Path | None) -> list[Path]:
    if path is None:
        return []
    path = Path(path)
    return [path, path.with_name(path.name + ".1")]


def _dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n"


def _scrub_jsonl(redactor: BundleRedactor, text: str) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            lines.append(redactor.text(line))
            continue
        lines.append(json.dumps(redactor.scrub(record), ensure_ascii=False, default=str))
    return "\n".join(lines) + ("\n" if lines else "")


def _load_document(raw: bytes, suffix: str) -> Any:
    text = raw.decode("utf-8-sig", errors="replace")
    if suffix == ".json":
        return json.loads(text)
    return yaml.safe_load(text)


def write_diagnostic_bundle(
    target: Path,
    inputs: DiagnosticBundleInputs,
    *,
    now: datetime | None = None,
) -> DiagnosticBundleResult:
    """Write the bundle to ``target`` (atomically) and describe it."""

    target = Path(target)
    now = now or datetime.now().astimezone()
    redactor = BundleRedactor(home=inputs.home)
    files: list[dict[str, Any]] = []
    missing: list[str] = []
    skipped: list[str] = []
    entries: list[tuple[str, bytes]] = []

    def add(name: str, text: str, **details: Any) -> None:
        data = text.encode("utf-8")
        entries.append((name, data))
        files.append({"name": name, "bytes": len(data), **details})

    # Settings first: they teach the redactor which words are private.
    documents: list[tuple[str, bytes | None, Any]] = []
    for name, path in inputs.config_files.items():
        raw = _read_bytes(path)
        parsed: Any = None
        if raw is not None:
            try:
                parsed = _load_document(raw, Path(path).suffix.lower())
            except (ValueError, yaml.YAMLError):
                parsed = None
            if isinstance(parsed, (Mapping, list)):
                redactor.learn(parsed)
        documents.append((name, raw, parsed))
    for name, raw, parsed in documents:
        arcname = f"config/{name}"
        if raw is None:
            missing.append(arcname)
        elif isinstance(parsed, (Mapping, list)):
            add(
                arcname,
                yaml.safe_dump(
                    redactor.scrub(parsed),
                    allow_unicode=True,
                    sort_keys=False,
                    default_flow_style=False,
                ),
            )
        else:
            # Unreadable settings still say something; scrub them as text.
            add(arcname, redactor.text(raw.decode("utf-8", errors="replace")))

    for family, as_jsonl in ((inputs.log_file, False), (inputs.events_file, True)):
        budget = LOG_TAIL_BYTES
        for index, path in enumerate(_log_family(family)):
            if budget <= 0:
                break
            tail = _read_tail(path, budget)
            if tail is None:
                if index == 0:
                    missing.append(f"logs/{path.name}")
                continue
            text, source_bytes, truncated = tail
            budget -= min(source_bytes, budget)
            body = _scrub_jsonl(redactor, text) if as_jsonl else redactor.text(text)
            add(f"logs/{path.name}", body, source_bytes=source_bytes, truncated=truncated)

    duplicates: list[str] = []
    for attachment in inputs.extra_logs:
        arcname = f"logs/{attachment.name}"
        if any(name == arcname for name, _data in entries):
            duplicates.append(arcname)
            continue
        details: dict[str, Any] = {"truncated": bool(attachment.truncated)}
        if attachment.source_bytes is not None:
            details["source_bytes"] = int(attachment.source_bytes)
        body = (
            _scrub_jsonl(redactor, attachment.text)
            if attachment.jsonl
            else redactor.text(attachment.text)
        )
        add(arcname, body, **details)

    for name, path in inputs.state_files.items():
        arcname = f"state/{name}"
        try:
            size = Path(path).stat().st_size
        except OSError:
            missing.append(arcname)
            continue
        if size > STATE_FILE_MAX_BYTES:
            skipped.append(arcname)
            continue
        raw = _read_bytes(path)
        if raw is None:
            missing.append(arcname)
            continue
        suffix = Path(path).suffix.lower()
        try:
            parsed = _load_document(raw, suffix)
        except (ValueError, yaml.YAMLError):
            add(arcname, redactor.text(raw.decode("utf-8", errors="replace")))
            continue
        scrubbed = redactor.scrub(parsed)
        if suffix == ".json":
            add(arcname, _dump_json(scrubbed))
        else:
            add(
                arcname,
                yaml.safe_dump(scrubbed, allow_unicode=True, sort_keys=False, default_flow_style=False),
            )

    if inputs.runtime_status is not None:
        add("runtime-status.json", _dump_json(redactor.scrub(dict(inputs.runtime_status))))
    if inputs.key_events_text.strip():
        add("key-events.txt", redactor.text(inputs.key_events_text.rstrip()) + "\n")

    manifest = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "generated_at": now.isoformat(timespec="seconds"),
        "app": redactor.scrub(dict(inputs.app)),
        "build": _build_details(),
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "files": files,
        "missing": missing,
        "skipped_too_large": skipped,
        "redaction": {"markers": redactor.markers, "policy": list(REDACTION_POLICY)},
    }
    if duplicates:
        manifest["skipped_duplicate"] = duplicates
    snapshot_times = {
        str(name): value
        for name, value in dict(inputs.snapshot_times).items()
        if type(value) is int and value >= 0
    }
    if snapshot_times:
        manifest["snapshot_times"] = snapshot_times
    entries.insert(0, ("manifest.json", _dump_json(manifest).encode("utf-8")))

    folder = target.stem or "tesseract-diagnostics"
    partial = target.with_name(target.name + ".partial")
    try:
        with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for name, data in entries:
                archive.writestr(f"{folder}/{name}", data)
        os.replace(partial, target)
    except BaseException:
        try:
            partial.unlink()
        except OSError:
            pass
        raise
    return DiagnosticBundleResult(
        path=target,
        size=target.stat().st_size,
        files=tuple(name for name, _data in entries),
        markers=redactor.markers,
    )


def default_bundle_name(now: datetime | None = None) -> str:
    moment = now or datetime.now()
    return f"tesseract-diagnostics-{moment.strftime('%Y%m%d-%H%M%S')}.zip"


def format_bundle_size(size: int) -> str:
    size = max(0, int(size))
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _build_details() -> dict[str, Any]:
    """Which build wrote the bundle: the identity dist comparisons use."""

    try:
        from tier3_build_identity import tier3_build_identity

        identity = tier3_build_identity()
    except Exception:
        return {}
    return {
        key: identity[key]
        for key in ("build_id", "executable_sha256", "identity_match")
        if key in identity
    }


def state_file_candidates(data_dir: Path, extra: Iterable[tuple[str, Path]] = ()) -> dict[str, Path]:
    """The small state files worth reading when something went wrong."""

    data_dir = Path(data_dir)
    candidates = {
        "runtime-lifecycle.json": data_dir / "runtime-lifecycle.json",
        "runtime-state.json": data_dir / "runtime-state.json",
        "dns-resolver-memory.json": data_dir / "dns-resolver-memory.json",
        "geo-metadata.json": data_dir / "geo-metadata.json",
        "alert-delivery-history.json": data_dir / "alert-delivery-history.json",
        "tier3-source-state.json": data_dir / "tier3-source-state.json",
        "tier3-control-state.json": data_dir / "tier3-control-state.json",
        "mihomo-runtime.yaml": data_dir / "mihomo-runtime.yaml",
    }
    for name, path in extra:
        candidates[str(name)] = Path(path)
    return candidates


__all__ = [
    "BUNDLE_SCHEMA_VERSION",
    "BundleRedactor",
    "DiagnosticAttachment",
    "DiagnosticBundleInputs",
    "DiagnosticBundleResult",
    "LOG_TAIL_BYTES",
    "REDACTION_POLICY",
    "STATE_FILE_MAX_BYTES",
    "default_bundle_name",
    "format_bundle_size",
    "state_file_candidates",
    "write_diagnostic_bundle",
]
