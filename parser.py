from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional
from urllib.parse import parse_qs, unquote, urlsplit

from models import Node
from log_event_reports import log_raw_diagnostic
from shadowsocks_capabilities import (
    MIHOMO_SHADOWSOCKS_VERSION,
    SUPPORTED_SHADOWSOCKS_CIPHERS,
    normalize_shadowsocks_cipher,
    validate_shadowsocks_credential,
)


URI_PATTERN = re.compile(
    r"(?:(?:vless|vmess|trojan|ss|ssr|socks|socks5|hysteria2|hy2|anytls|tuic|naive|wireguard|wg|https?)://[^\s\"'<>]+)",
    re.IGNORECASE,
)
GENERIC_URI_PATTERN = re.compile(
    r"\b(?P<scheme>[A-Za-z][A-Za-z0-9+.-]{0,31})://[^\s\"'<>]+",
    re.IGNORECASE,
)
BASE64_LINE_PATTERN = re.compile(r"^[A-Za-z0-9+/_-]+={0,2}$")


class ContentParsingCancelled(RuntimeError):
    pass


def _cancel_requested(cancel_event: Any | None) -> bool:
    is_set = getattr(cancel_event, "is_set", None)
    return bool(callable(is_set) and is_set())

SUPPORTED_URI_SCHEMES = {
    "vless",
    "vmess",
    "trojan",
    "ss",
    "ssr",
    "socks",
    "socks5",
    "hysteria2",
    "hy2",
    "anytls",
    "tuic",
    "naive",
    "wireguard",
    "wg",
    "http",
    "https",
}

_BASE64_URI_PREFIXES = tuple(
    base64.b64encode(f"{scheme}://".encode("ascii")).decode("ascii").rstrip("=")[:8]
    for scheme in sorted(SUPPORTED_URI_SCHEMES - {"http", "https"})
)


class NodeValidationError(ValueError):
    def __init__(self, code: str, message: str, *, params: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message
        self.params = dict(params or {})


@dataclass(frozen=True)
class ContentIssue:
    code: str
    message: str
    scheme: str = ""
    fingerprint: str = ""
    source_kind: str = "raw"
    position: int = -1
    blocking: bool = True
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "reason": self.message,
            "scheme": self.scheme,
            "fingerprint": self.fingerprint,
            "source_kind": self.source_kind,
            "position": self.position,
            "blocking": self.blocking,
            "params": dict(self.params),
        }


@dataclass
class ContentAnalysisReport:
    accepted_nodes: list[Node] = field(default_factory=list)
    candidate_count: int = 0
    duplicate_count: int = 0
    rejected: list[ContentIssue] = field(default_factory=list)
    base64_sources: list[str] = field(default_factory=list)
    ignored_count: int = 0

    @property
    def nodes(self) -> list[Node]:
        return self.accepted_nodes

    @property
    def blocking_errors(self) -> list[ContentIssue]:
        return [issue for issue in self.rejected if issue.blocking]

    @property
    def blocked(self) -> bool:
        return bool(self.blocking_errors)

    @property
    def ok(self) -> bool:
        return bool(self.accepted_nodes) and not self.blocked

    @property
    def accepted_count(self) -> int:
        return len(self.accepted_nodes)

    @property
    def rejected_count(self) -> int:
        return len(self.rejected)

    @property
    def blocking(self) -> bool:
        return self.blocked

    def to_dict(self) -> dict[str, Any]:
        node_summaries = [
            {
                "name": node.name or node.remarks or node.address,
                "protocol": node.protocol,
                "address": node.address,
                "port": node.port,
            }
            for node in self.accepted_nodes
        ]
        rejected = [issue.to_dict() for issue in self.rejected]
        blocking_errors = [issue.to_dict() for issue in self.blocking_errors]
        return {
            "candidate_count": self.candidate_count,
            "accepted_count": self.accepted_count,
            "duplicate_count": self.duplicate_count,
            "rejected_count": self.rejected_count,
            "ignored_count": self.ignored_count,
            "blocking": self.blocked,
            "blocked": self.blocked,
            "ok": self.ok,
            "base64_sources": list(self.base64_sources),
            "nodes": node_summaries,
            "accepted_nodes": node_summaries,
            "rejected": rejected,
            "blocking_errors": blocking_errors,
        }

    def summary(self) -> dict[str, Any]:
        return self.to_dict()


@dataclass(frozen=True)
class _LineRecord:
    text: str
    stripped: str
    start: int
    number: int


@dataclass(frozen=True)
class _AnalysisSegment:
    text: str
    position: int
    source_kind: str


def _safe_b64decode(value: str) -> bytes:
    normalized = value.strip().replace("-", "+").replace("_", "/")
    padding = (-len(normalized)) % 4
    normalized += "=" * padding
    try:
        return base64.b64decode(normalized, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise NodeValidationError(
            "invalid_base64",
            "The node URI contains invalid Base64 data.",
        ) from exc


def _strict_b64decode_text(value: str) -> str:
    normalized = value.strip().replace("-", "+").replace("_", "/")
    if not normalized or not BASE64_LINE_PATTERN.fullmatch(normalized):
        raise ValueError("invalid base64 alphabet or padding")
    if len(normalized) % 4 == 1:
        raise ValueError("invalid base64 length")
    normalized += "=" * ((-len(normalized)) % 4)
    decoded = base64.b64decode(normalized, validate=True)
    return decoded.decode("utf-8", errors="strict")


def _first(values: dict[str, list[str]], key: str, default: Optional[str] = None) -> Optional[str]:
    items = values.get(key, [])
    return items[0] if items else default


def _query_extra(values: dict[str, list[str]]) -> dict[str, Any]:
    return {key: value[0] if len(value) == 1 else value for key, value in values.items()}


def _truthy(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _split_user_host(uri: str, default_port: int) -> tuple[str, str, int]:
    parsed = urlsplit(uri)
    if "@" not in parsed.netloc:
        raise ValueError("missing userinfo separator")
    userinfo = parsed.netloc.rsplit("@", 1)[0]
    address = parsed.hostname or ""
    if not address:
        raise ValueError("missing host")
    return unquote(userinfo), address, int(parsed.port or default_port)


def extract_uris(text: str) -> list[str]:
    return list(dict.fromkeys(match.group(0) for match in URI_PATTERN.finditer(text or "")))


def _candidate_fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:12]


def _line_records(
    text: str,
    *,
    cancel_event: Any | None = None,
) -> list[_LineRecord]:
    if _cancel_requested(cancel_event):
        raise ContentParsingCancelled
    records: list[_LineRecord] = []
    offset = 0
    raw_lines = text.splitlines(keepends=True)
    if _cancel_requested(cancel_event):
        raise ContentParsingCancelled
    if not raw_lines and text:
        raw_lines = [text]
    for number, raw_line in enumerate(raw_lines, start=1):
        if number % 64 == 1 and _cancel_requested(cancel_event):
            raise ContentParsingCancelled
        line_text = raw_line.rstrip("\r\n")
        records.append(_LineRecord(line_text, line_text.strip(), offset, number))
        offset += len(raw_line)
    return records


def _is_base64_token(value: str) -> bool:
    return len(value) >= 24 and bool(BASE64_LINE_PATTERN.fullmatch(value))


def _is_base64_line(value: str) -> bool:
    return bool(value) and bool(BASE64_LINE_PATTERN.fullmatch(value))


def _is_base64ish_wrapped_line(value: str, *, allow_short: bool = False) -> bool:
    candidate = str(value or "").strip()
    minimum_length = 4 if allow_short else 24
    if len(candidate) < minimum_length or "://" in candidate:
        return False
    allowed = sum(
        char in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/_=-"
        for char in candidate
    )
    return allowed / len(candidate) >= 0.9


def _decoded_is_complete_uri_lines(text: str) -> bool:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    return bool(lines) and all(GENERIC_URI_PATTERN.fullmatch(line) for line in lines)


def _base64_chunk_is_self_contained(records: list[_LineRecord]) -> bool:
    if not records:
        return False
    try:
        decoded = _strict_b64decode_text("".join(record.stripped for record in records))
    except (ValueError, UnicodeDecodeError):
        return False
    if not _decoded_content_is_relevant(decoded):
        return False
    if len(records) == 1:
        value = records[0].stripped
        if len(value) in {64, 76} and not value.endswith("=") and not decoded.endswith(("\n", "\r")):
            # One full conventional wrap-width line can be only the prefix of
            # a larger export even when those decoded bytes happen to end in
            # a parseable-looking, truncated URI.
            return False
        return _decoded_is_complete_uri_lines(decoded) or bool(_detect_structured_format(decoded))

    first_width = len(records[0].stripped)
    last_value = records[-1].stripped
    has_encoded_terminator = (
        last_value.endswith("=")
        or len(last_value) < first_width
        or decoded.endswith(("\n", "\r"))
    )
    return has_encoded_terminator


def _decoded_base64_records(records: list[_LineRecord]) -> str:
    try:
        return _strict_b64decode_text("".join(record.stripped for record in records))
    except (ValueError, UnicodeDecodeError):
        return ""


def _blank_join_loses_base64_candidates(
    left_records: list[_LineRecord],
    right_records: list[_LineRecord],
) -> bool:
    left_decoded = _decoded_base64_records(left_records)
    right_decoded = _decoded_base64_records(right_records)
    if not (_decoded_content_is_relevant(left_decoded) and _decoded_content_is_relevant(right_decoded)):
        return False
    joined_decoded = _decoded_base64_records([*left_records, *right_records])
    left_count = len(list(GENERIC_URI_PATTERN.finditer(left_decoded)))
    right_count = len(list(GENERIC_URI_PATTERN.finditer(right_decoded)))
    joined_count = len(list(GENERIC_URI_PATTERN.finditer(joined_decoded)))
    return left_count > 0 and right_count > 0 and joined_count < left_count + right_count


def _looks_like_wrapped_base64(records: list[_LineRecord]) -> bool:
    if len(records) < 2 or any(not _is_base64_line(record.stripped) for record in records):
        return False
    return _looks_like_wrapped_base64_shape(records)


def _looks_like_wrapped_base64_shape(records: list[_LineRecord]) -> bool:
    if len(records) < 2:
        return False
    if any("=" in record.stripped for record in records[:-1]):
        return False
    width = len(records[0].stripped)
    if width < 32:
        return False
    if any(len(record.stripped) != width for record in records[:-1]):
        return False
    return len(records[-1].stripped) <= width


def _looks_like_damaged_standard_wrapped_base64_shape(records: list[_LineRecord]) -> bool:
    if len(records) < 2:
        return False
    lengths = [len(record.stripped) for record in records]
    for width in (64, 76):
        two_line_near_standard_width = (
            len(records) == 2
            and width - 10 <= lengths[0] <= width + 10
            and 4 <= lengths[1] <= width + 2
        )
        standard_width_evidence = (
            any(length == width for length in lengths[:-1])
            or two_line_near_standard_width
        )
        plausible_standard_shape = (
            standard_width_evidence
            and all(24 <= length <= width + 10 for length in lengths[:-1])
            and 4 <= lengths[-1] <= width + 2
        )
        if plausible_standard_shape and any("=" in record.stripped for record in records[:-1]):
            # Padding is only legal at the end of the complete Base64 value.
            # Treat an early '=' in a conventional 64/76-column export as a
            # damaged wrapped body instead of decoding the remaining lines as
            # unrelated, potentially truncated node candidates.
            return True
        if plausible_standard_shape and any(length != width for length in lengths[:-1]):
            return True
        if plausible_standard_shape and lengths[-1] > width:
            return True
        if plausible_standard_shape:
            try:
                _strict_b64decode_text("".join(record.stripped for record in records))
            except (ValueError, UnicodeDecodeError):
                return True
    return False


def _detect_structured_format(text: str) -> str | None:
    stripped = str(text or "").strip()
    if not stripped:
        return None
    if stripped[0] in "[{":
        try:
            decoded = json.loads(stripped)
        except (TypeError, ValueError, json.JSONDecodeError):
            decoded = None
        if isinstance(decoded, dict):
            proxy_keys = {
                "outbounds",
                "inbounds",
                "proxies",
                "proxy-groups",
                "mixed-port",
                "socks-port",
                "redir-port",
                "tproxy-port",
            }
            if proxy_keys.intersection(str(key).lower() for key in decoded):
                return "json"
        elif isinstance(decoded, list) and any(
            isinstance(item, dict)
            and len({"type", "server", "protocol"}.intersection(str(key).lower() for key in item)) >= 2
            for item in decoded
        ):
            return "json"
    yaml_markers = (
        r"(?im)^\s*(?:proxies|proxy-groups|outbounds|inbounds|mixed-port|socks-port|redir-port|tproxy-port)\s*:",
        r"(?im)^\s*-\s*(?:name|type|server|protocol)\s*:",
    )
    if any(re.search(pattern, stripped) for pattern in yaml_markers):
        return "yaml"
    return None


def _decoded_content_is_relevant(text: str) -> bool:
    return bool(GENERIC_URI_PATTERN.search(text or "") or _detect_structured_format(text))


def _looks_intentional_invalid_base64(value: str) -> bool:
    candidate = str(value or "").strip()
    # An explicit URI belongs to URI validation, not repaired Base64 probing.
    if GENERIC_URI_PATTERN.match(candidate):
        return False
    if len(candidate) < 24 or any(char.isspace() for char in candidate):
        return False
    prefix = candidate[:8]
    if any(prefix.startswith(known[:6]) for known in _BASE64_URI_PREFIXES) or candidate.startswith(("eyJ", "W3s")):
        return True

    # A provider may corrupt or truncate one character in an otherwise valid
    # Base64 subscription body.  Probe sanitized variants only to decide that
    # the input is node-like and must block the batch; never import the probed
    # text itself.
    filtered = re.sub(r"[^A-Za-z0-9+/_=-]", "", candidate)
    replaced = re.sub(r"[^A-Za-z0-9+/_=-]", "A", candidate)
    probes = [filtered, replaced]
    if len(candidate) % 4 == 1 and len(candidate) > 24:
        # A single copied/pasted Base64 character at either edge makes the
        # otherwise valid value undecodable.  Probe both one-character trims
        # only to identify node intent; neither repaired value is imported.
        probes.extend((candidate[:-1], candidate[1:]))
    if "=" in candidate:
        first_padding = candidate.find("=")
        padding_suffix = candidate[first_padding:]
        valid_terminal_padding = (
            1 <= len(padding_suffix) <= 2
            and set(padding_suffix) == {"="}
        )
        if not valid_terminal_padding:
            # This probe is only evidence that a syntactically damaged token
            # was intended to contain node content.  It is never imported.
            probes.append(candidate.replace("=", "A"))
    for probe in dict.fromkeys(probes):
        if len(probe) < 24:
            continue
        try:
            decoded = _strict_b64decode_text(probe)
        except (ValueError, UnicodeDecodeError):
            continue
        if _decoded_content_is_relevant(decoded):
            return True
    return False


def _base64_source_label(records: list[_LineRecord], *, whole_document: bool) -> str:
    if whole_document:
        return "whole" if len(records) == 1 else "wrapped"
    if len(records) == 1:
        return f"line:{records[0].number}"
    return f"wrapped:{records[0].number}-{records[-1].number}"


def _build_analysis_segments(
    text: str,
    *,
    cancel_event: Any | None = None,
) -> tuple[list[_AnalysisSegment], list[ContentIssue], list[str], int]:
    if _cancel_requested(cancel_event):
        raise ContentParsingCancelled
    records = _line_records(text, cancel_event=cancel_event)
    nonblank_records: list[_LineRecord] = []
    for record_index, record in enumerate(records):
        if record_index % 64 == 0 and _cancel_requested(cancel_event):
            raise ContentParsingCancelled
        if record.stripped:
            nonblank_records.append(record)
    segments: list[_AnalysisSegment] = []
    issues: list[ContentIssue] = []
    base64_sources: list[str] = []
    base64_candidate_count = 0
    nonblank_numbers: set[int] = set()
    for record_index, record in enumerate(nonblank_records):
        if record_index % 64 == 0 and _cancel_requested(cancel_event):
            raise ContentParsingCancelled
        nonblank_numbers.add(record.number)

    index = 0
    while index < len(records):
        if _cancel_requested(cancel_event):
            raise ContentParsingCancelled
        record = records[index]
        if not record.stripped:
            index += 1
            continue

        if _is_base64_token(record.stripped) or _is_base64ish_wrapped_line(record.stripped):
            block = [record]
            cursor = index + 1
            ambiguous_blank_boundary = False
            while cursor < len(records):
                if _cancel_requested(cancel_event):
                    raise ContentParsingCancelled
                if not records[cursor].stripped:
                    next_cursor = cursor
                    while next_cursor < len(records) and not records[next_cursor].stripped:
                        if (
                            (next_cursor - cursor) % 64 == 0
                            and _cancel_requested(cancel_event)
                        ):
                            raise ContentParsingCancelled
                        next_cursor += 1
                    next_block: list[_LineRecord] = []
                    probe_cursor = next_cursor
                    while (
                        probe_cursor < len(records)
                        and records[probe_cursor].stripped
                        and _is_base64ish_wrapped_line(records[probe_cursor].stripped, allow_short=True)
                    ):
                        if (
                            (probe_cursor - next_cursor) % 64 == 0
                            and _cancel_requested(cancel_event)
                        ):
                            raise ContentParsingCancelled
                        next_block.append(records[probe_cursor])
                        probe_cursor += 1
                    if (
                        next_block
                        and _base64_chunk_is_self_contained(block)
                        and _base64_chunk_is_self_contained(next_block)
                    ):
                        # A blank line between two complete Base64 exports is
                        # a real source boundary.  Process each export once so
                        # their decoded tail/head cannot be fused into one URI.
                        break
                    if next_block and _blank_join_loses_base64_candidates(block, next_block):
                        # The line boundary is intrinsically ambiguous: joining
                        # swallows a URI that exists when the two strict Base64
                        # values are decoded separately.  Block the candidate
                        # instead of silently importing a smaller batch.
                        block.extend(next_block)
                        cursor = probe_cursor
                        ambiguous_blank_boundary = True
                        break
                    # Otherwise the blank is formatting inside one wrapped
                    # export and must not split it into truncated candidates.
                    cursor = next_cursor
                    continue
                if not _is_base64ish_wrapped_line(records[cursor].stripped, allow_short=True):
                    break
                block.append(records[cursor])
                cursor += 1

            whole_document = {item.number for item in block} == nonblank_numbers
            if ambiguous_blank_boundary:
                joined = "".join(item.stripped for item in block)
                label = _base64_source_label(block, whole_document=whole_document)
                base64_sources.append(label)
                issues.append(
                    ContentIssue(
                        code="invalid_base64",
                        message="Blank-line-separated Base64 content has an ambiguous node boundary.",
                        fingerprint=_candidate_fingerprint(joined),
                        source_kind=f"base64:{label}",
                        position=block[0].start,
                    )
                )
                base64_candidate_count += 1
                index = cursor
                continue
            if (
                _looks_like_wrapped_base64_shape(block)
                and any(not _is_base64_line(item.stripped) for item in block)
            ):
                joined = "".join(item.stripped for item in block)
                label = _base64_source_label(block, whole_document=whole_document)
                base64_sources.append(label)
                issues.append(
                    ContentIssue(
                        code="invalid_base64",
                        message="Base64 node content is invalid or is not UTF-8 text.",
                        fingerprint=_candidate_fingerprint(joined),
                        source_kind=f"base64:{label}",
                        position=block[0].start,
                    )
                )
                base64_candidate_count += 1
                index = cursor
                continue
            individually_decoded: dict[int, str] = {}
            for item in block:
                if _cancel_requested(cancel_event):
                    raise ContentParsingCancelled
                try:
                    individually_decoded[item.number] = _strict_b64decode_text(item.stripped)
                except (ValueError, UnicodeDecodeError):
                    individually_decoded[item.number] = ""
            independent_uri_lines = len(block) > 1 and all(
                _decoded_is_complete_uri_lines(individually_decoded.get(item.number, ""))
                for item in block
            )

            if (
                not independent_uri_lines
                and _looks_like_damaged_standard_wrapped_base64_shape(block)
            ):
                joined = "".join(item.stripped for item in block)
                label = _base64_source_label(block, whole_document=whole_document)
                base64_sources.append(label)
                issues.append(
                    ContentIssue(
                        code="invalid_base64",
                        message="Base64 node content is invalid or is not UTF-8 text.",
                        fingerprint=_candidate_fingerprint(joined),
                        source_kind=f"base64:{label}",
                        position=block[0].start,
                    )
                )
                base64_candidate_count += 1
                index = cursor
                continue

            if _looks_like_wrapped_base64(block) and not independent_uri_lines:
                if _cancel_requested(cancel_event):
                    raise ContentParsingCancelled
                joined = "".join(item.stripped for item in block)
                label = _base64_source_label(block, whole_document=whole_document)
                try:
                    decoded = _strict_b64decode_text(joined)
                except (ValueError, UnicodeDecodeError):
                    decoded = ""
                if decoded and _decoded_content_is_relevant(decoded):
                    segments.append(_AnalysisSegment(decoded, block[0].start, f"base64:{label}"))
                    base64_sources.append(label)
                elif _looks_intentional_invalid_base64(joined):
                    base64_sources.append(label)
                    issues.append(
                        ContentIssue(
                            code="invalid_base64",
                            message="Base64 node content is invalid or is not UTF-8 text.",
                            fingerprint=_candidate_fingerprint(joined),
                            source_kind=f"base64:{label}",
                            position=block[0].start,
                        )
                    )
                    base64_candidate_count += 1
                # A block identified as wrapped Base64 is consumed exactly once;
                # it is never decoded again line-by-line.
                index = cursor
                continue

            for item in block:
                if _cancel_requested(cancel_event):
                    raise ContentParsingCancelled
                label = _base64_source_label([item], whole_document=whole_document and len(block) == 1)
                decoded = individually_decoded.get(item.number, "")
                if decoded and _decoded_content_is_relevant(decoded):
                    segments.append(_AnalysisSegment(decoded, item.start, f"base64:{label}"))
                    base64_sources.append(label)
                elif _looks_intentional_invalid_base64(item.stripped):
                    base64_sources.append(label)
                    issues.append(
                        ContentIssue(
                            code="invalid_base64",
                            message="Base64 node content is invalid or is not UTF-8 text.",
                            fingerprint=_candidate_fingerprint(item.stripped),
                            source_kind=f"base64:{label}",
                            position=item.start,
                        )
                    )
                    base64_candidate_count += 1
            index = cursor
            continue

        if _cancel_requested(cancel_event):
            raise ContentParsingCancelled
        if _looks_intentional_invalid_base64(record.stripped):
            label = f"line:{record.number}"
            base64_sources.append(label)
            issues.append(
                ContentIssue(
                    code="invalid_base64",
                    message="Base64 node content is invalid or is not UTF-8 text.",
                    fingerprint=_candidate_fingerprint(record.stripped),
                    source_kind=f"base64:{label}",
                    position=record.start,
                )
            )
            base64_candidate_count += 1
        else:
            segments.append(_AnalysisSegment(record.text, record.start, "raw"))
        index += 1

    if _cancel_requested(cancel_event):
        raise ContentParsingCancelled
    segments.sort(key=lambda item: item.position)
    if _cancel_requested(cancel_event):
        raise ContentParsingCancelled
    issues.sort(key=lambda item: item.position)
    if _cancel_requested(cancel_event):
        raise ContentParsingCancelled
    return segments, issues, base64_sources, base64_candidate_count


def parse_content_blob(
    text: str,
    source: str,
    tier: str,
    logger: Optional[logging.Logger] = None,
    cancel_event: Any | None = None,
    node_template_cache: dict[str, Node] | None = None,
    node_template_cache_lock: Any | None = None,
    node_template_cache_limit: int = 0,
) -> list[Node]:
    if _cancel_requested(cancel_event):
        return []
    try:
        report = _analyze_content_blob(
            text,
            source=source,
            tier=tier,
            allow_ambiguous_http=True,
            deduplicate=False,
            cancel_event=cancel_event,
            node_template_cache=node_template_cache,
            node_template_cache_lock=node_template_cache_lock,
            node_template_cache_limit=node_template_cache_limit,
        )
    except ContentParsingCancelled:
        return []
    if logger:
        for issue in report.rejected:
            logger.debug("Skipped node candidate from %s (%s)", source, issue.code)
    return report.accepted_nodes


def analyze_content_blob(
    text: str,
    source: str = "",
    tier: str = "",
) -> ContentAnalysisReport:
    """Analyze pasted node content without silently accepting a partial batch.

    The returned nodes are safe to preview and are de-duplicated in first-seen
    order.  Callers must treat ``report.blocked`` as an all-or-nothing import
    failure.  ``to_dict`` intentionally omits URIs and credentials.
    """

    return _analyze_content_blob(
        text,
        source=source,
        tier=tier,
        allow_ambiguous_http=False,
        deduplicate=True,
        cancel_event=None,
    )


def _analyze_content_blob(
    text: str,
    *,
    source: str,
    tier: str,
    allow_ambiguous_http: bool,
    deduplicate: bool,
    cancel_event: Any | None = None,
    node_template_cache: dict[str, Node] | None = None,
    node_template_cache_lock: Any | None = None,
    node_template_cache_limit: int = 0,
) -> ContentAnalysisReport:
    segments, initial_issues, base64_sources, candidate_count = _build_analysis_segments(
        str(text or ""),
        cancel_event=cancel_event,
    )
    rejected = list(initial_issues)
    accepted_nodes: list[Node] = []
    duplicate_count = 0
    seen_node_keys: set[tuple[str, ...]] = set()
    seen_legacy_uris: set[tuple[str, str]] = set()
    ignored_count = 0

    structure_sources: list[tuple[str, int, str]] = [(str(text or ""), 0, "raw")]
    structure_sources.extend(
        (segment.text, segment.position, segment.source_kind)
        for segment in segments
        if segment.source_kind.startswith("base64:")
    )
    seen_structures: set[tuple[str, str]] = set()
    for structured_text, position, source_kind in structure_sources:
        if _cancel_requested(cancel_event):
            raise ContentParsingCancelled
        structured_format = _detect_structured_format(structured_text)
        if not structured_format:
            continue
        structure_key = (structured_format, source_kind)
        if structure_key in seen_structures:
            continue
        seen_structures.add(structure_key)
        rejected.append(
            ContentIssue(
                code="unsupported_structured_format",
                message=f"{structured_format.upper()} proxy configuration is not supported by node paste import.",
                scheme=structured_format,
                fingerprint=_candidate_fingerprint(structured_text),
                source_kind=source_kind,
                position=position,
            )
        )

    for segment in segments:
        if _cancel_requested(cancel_event):
            raise ContentParsingCancelled
        matched_any = False
        for match in GENERIC_URI_PATTERN.finditer(segment.text):
            if _cancel_requested(cancel_event):
                raise ContentParsingCancelled
            matched_any = True
            uri = match.group(0)
            scheme = match.group("scheme").lower()
            position = segment.position + match.start()

            if not deduplicate:
                source_group = "base64" if segment.source_kind.startswith("base64:") else "raw"
                legacy_uri_key = (source_group, uri)
                if legacy_uri_key in seen_legacy_uris:
                    continue
                seen_legacy_uris.add(legacy_uri_key)

            if scheme in {"http", "https"}:
                parsed_http = urlsplit(uri)
                if "@" not in parsed_http.netloc:
                    if allow_ambiguous_http:
                        ignored_count += 1
                        continue
                    candidate_count += 1
                    rejected.append(
                        ContentIssue(
                            code="subscription_url_not_node",
                            message="HTTP(S) URLs are not node content; use Subscription URL import.",
                            scheme=scheme,
                            fingerprint=_candidate_fingerprint(uri),
                            source_kind=segment.source_kind,
                            position=position,
                        )
                    )
                    continue
                candidate_count += 1
                if not allow_ambiguous_http:
                    rejected.append(
                        ContentIssue(
                            code="ambiguous_authenticated_http",
                            message="Authenticated HTTP(S) is ambiguous between Naive and a subscription URL; use naive://, manual Naive, or Subscription URL.",
                            scheme=scheme,
                            fingerprint=_candidate_fingerprint(uri),
                            source_kind=segment.source_kind,
                            position=position,
                        )
                    )
                    continue
            else:
                candidate_count += 1

            if scheme not in SUPPORTED_URI_SCHEMES:
                rejected.append(
                    ContentIssue(
                        code="unsupported_uri_scheme",
                        message="This URI scheme is not supported by node paste import.",
                        scheme=scheme,
                        fingerprint=_candidate_fingerprint(uri),
                        source_kind=segment.source_kind,
                        position=position,
                    )
                )
                continue

            cached_template: Node | None = None
            if node_template_cache is not None:
                if node_template_cache_lock is None:
                    cached_template = node_template_cache.get(uri)
                else:
                    with node_template_cache_lock:
                        cached_template = node_template_cache.get(uri)
            try:
                if cached_template is not None:
                    node = cached_template.clone()
                    node.source = source
                    node.tier = tier
                else:
                    node = _parse_node_uri_or_raise(uri, source=source, tier=tier)
            except NodeValidationError as exc:
                rejected.append(
                    ContentIssue(
                        code=exc.code,
                        message=exc.safe_message,
                        scheme=scheme,
                        fingerprint=_candidate_fingerprint(uri),
                        source_kind=segment.source_kind,
                        position=position,
                        params=exc.params,
                    )
                )
                continue

            except ValueError as exc:
                invalid_port = "port" in str(exc).lower()
                rejected.append(
                    ContentIssue(
                        code="invalid_port" if invalid_port else "invalid_node_uri",
                        message=(
                            "Node port must be between 1 and 65535."
                            if invalid_port
                            else "The node URI is malformed or is missing required fields."
                        ),
                        scheme=scheme,
                        fingerprint=_candidate_fingerprint(uri),
                        source_kind=segment.source_kind,
                        position=position,
                    )
                )
                continue
            except (KeyError, TypeError, UnicodeError):
                rejected.append(
                    ContentIssue(
                        code="invalid_node_uri",
                        message="The node URI is malformed or is missing required fields.",
                        scheme=scheme,
                        fingerprint=_candidate_fingerprint(uri),
                        source_kind=segment.source_kind,
                        position=position,
                    )
                )
                continue
            except Exception:
                rejected.append(
                    ContentIssue(
                        code="invalid_node_uri",
                        message="The node URI could not be parsed safely.",
                        scheme=scheme,
                        fingerprint=_candidate_fingerprint(uri),
                        source_kind=segment.source_kind,
                        position=position,
                    )
                )
                continue

            if node_template_cache is not None and cached_template is None:
                template = node.clone()
                template.source = ""
                template.tier = ""
                template.sources = []
                if node_template_cache_lock is None:
                    if (
                        node_template_cache_limit <= 0
                        or len(node_template_cache) < node_template_cache_limit
                    ):
                        node_template_cache.setdefault(uri, template)
                else:
                    with node_template_cache_lock:
                        if (
                            node_template_cache_limit <= 0
                            or len(node_template_cache) < node_template_cache_limit
                        ):
                            node_template_cache.setdefault(uri, template)

            node_key = node.dedupe_key
            if deduplicate and node_key in seen_node_keys:
                duplicate_count += 1
                continue
            seen_node_keys.add(node_key)
            accepted_nodes.append(node)

        if segment.text.strip() and not matched_any and segment.source_kind == "raw":
            ignored_count += 1

    if _cancel_requested(cancel_event):
        raise ContentParsingCancelled
    rejected.sort(key=lambda item: item.position)
    if _cancel_requested(cancel_event):
        raise ContentParsingCancelled
    return ContentAnalysisReport(
        accepted_nodes=accepted_nodes,
        candidate_count=candidate_count,
        duplicate_count=duplicate_count,
        rejected=rejected,
        base64_sources=base64_sources,
        ignored_count=ignored_count,
    )


def parse_node_uri(
    uri: str,
    source: str = "",
    tier: str = "",
    logger: Optional[logging.Logger] = None,
) -> Optional[Node]:
    try:
        scheme, separator, _payload = uri.partition("://")
        if not separator:
            raise ValueError("missing URI scheme separator")
        scheme = scheme.lower()
    except (AttributeError, ValueError):
        if logger:
            log_raw_diagnostic(logger, "warning", "Skipping malformed URI from %s", source)
        return None

    try:
        if scheme in {"http", "https"}:
            parsed = urlsplit(uri)
            if "@" not in parsed.netloc:
                return None
        return _parse_node_uri_or_raise(uri, source=source, tier=tier)
    except Exception:
        if logger:
            log_raw_diagnostic(logger, "warning", "Failed to parse %s URI from %s", scheme, source)
        return None


def _parse_node_uri_or_raise(uri: str, *, source: str, tier: str) -> Node:
    scheme, separator, _payload = uri.partition("://")
    if not separator:
        raise ValueError("missing URI scheme separator")
    scheme = scheme.lower()
    if scheme == "vless":
        node = _parse_vless(uri, source, tier)
    elif scheme == "vmess":
        node = _parse_vmess(uri, source, tier)
    elif scheme == "trojan":
        node = _parse_trojan(uri, source, tier)
    elif scheme == "ss":
        node = _parse_ss(uri, source, tier)
    elif scheme == "ssr":
        node = _parse_ssr(uri, source, tier)
    elif scheme in {"socks", "socks5"}:
        node = _parse_socks5(uri, source, tier)
    elif scheme in {"hysteria2", "hy2"}:
        node = _parse_hysteria2(uri, source, tier)
    elif scheme == "anytls":
        node = _parse_anytls(uri, source, tier)
    elif scheme == "tuic":
        node = _parse_tuic(uri, source, tier)
    elif scheme in {"http", "https", "naive"}:
        node = _parse_naive(uri, source, tier)
    elif scheme in {"wireguard", "wg"}:
        node = _parse_wireguard(uri, source, tier)
    else:
        raise NodeValidationError("unsupported_uri_scheme", "This URI scheme is not supported by node paste import.")
    _validate_node_for_runtime(node)
    return node


def _validate_node_for_runtime(node: Node) -> None:
    if not str(node.address or "").strip():
        raise NodeValidationError("missing_address", "Node address is required.")
    try:
        port = int(node.port)
    except (TypeError, ValueError):
        raise NodeValidationError("invalid_port", "Node port must be between 1 and 65535.") from None
    if not 1 <= port <= 65535:
        raise NodeValidationError("invalid_port", "Node port must be between 1 and 65535.")
    protocol = str(node.protocol or "").strip().lower()
    if protocol in {"vless", "vmess"} and not str(node.uuid or "").strip():
        raise NodeValidationError("missing_uuid", f"{protocol.upper()} requires a UUID.")
    if protocol == "vless" and str(node.security or "").strip().lower() == "reality":
        if not str(node.extra.get("pbk") or "").strip() or not str(node.extra.get("sid") or "").strip():
            raise NodeValidationError(
                "missing_reality_keys",
                "VLESS Reality requires a public key and short ID.",
            )
    if protocol == "vmess":
        alter_id = node.extra.get("aid", node.extra.get("alterId", 0))
        try:
            normalized_alter_id = int(alter_id or 0)
        except (TypeError, ValueError):
            raise NodeValidationError(
                "invalid_vmess_alter_id",
                "VMess alterId must be a non-negative integer.",
            ) from None
        if normalized_alter_id < 0:
            raise NodeValidationError(
                "invalid_vmess_alter_id",
                "VMess alterId must be a non-negative integer.",
            )
        node.extra["aid"] = normalized_alter_id
    if protocol == "trojan" and not str(node.password or "").strip():
        raise NodeValidationError("missing_password", "Trojan requires a password.")
    if protocol == "ss":
        method = normalize_shadowsocks_cipher(node.method)
        if not method:
            raise NodeValidationError("missing_cipher_or_password", "Shadowsocks requires a cipher and password.")
        credential_issue = validate_shadowsocks_credential(method, node.password)
        if credential_issue is not None:
            issue_messages = {
                "unsupported_cipher": (
                    f'The Shadowsocks cipher "{method}" is not supported by bundled '
                    f"Mihomo v{MIHOMO_SHADOWSOCKS_VERSION}."
                ),
                "missing_ss_password": "Shadowsocks requires a password for this cipher.",
                "missing_ss_2022_key": "Shadowsocks 2022 requires a Base64-encoded key.",
                "invalid_ss_2022_key_base64": "The Shadowsocks 2022 key is not valid Base64.",
                "invalid_ss_2022_key_length": "The Shadowsocks 2022 key has an invalid decoded length.",
            }
            raise NodeValidationError(
                credential_issue.code,
                issue_messages.get(credential_issue.code, "The Shadowsocks credential is invalid."),
                params=credential_issue.params,
            )
        node.method = method
    if protocol == "ssr" and (not str(node.method or "").strip() or not str(node.password or "").strip()):
        raise NodeValidationError("missing_cipher_or_password", "ShadowsocksR requires a cipher and password.")
    if protocol == "hysteria2" and not str(node.password or "").strip():
        raise NodeValidationError("missing_password", "Hysteria2 requires a password.")
    if protocol == "anytls" and not str(node.password or "").strip():
        raise NodeValidationError("missing_password", "AnyTLS requires a password.")
    if protocol == "tuic":
        token = node.extra.get("token")
        if not str(token or "").strip() and (
            not str(node.uuid or "").strip() or not str(node.password or "").strip()
        ):
            raise NodeValidationError("missing_tuic_credentials", "TUIC requires a token or a UUID and password.")
    if protocol == "socks5":
        username = str(node.extra.get("username") or node.uuid or "").strip()
        password = str(node.password or "").strip()
        if bool(username) != bool(password):
            raise NodeValidationError(
                "incomplete_socks5_credentials",
                "SOCKS5 username and password must be provided together.",
                params={"has_username": bool(username), "has_password": bool(password)},
            )
    if protocol == "wireguard":
        private_key = node.password or node.extra.get("private-key") or node.extra.get("private_key")
        public_key = node.extra.get("public-key") or node.extra.get("public_key") or node.extra.get("peer-public-key")
        if not str(private_key or "").strip() or not str(public_key or "").strip():
            raise NodeValidationError("missing_wireguard_keys", "WireGuard requires private and public keys.")
        local_address = node.extra.get("ip") or node.extra.get("ipv6") or node.extra.get("address")
        if not str(local_address or "").strip():
            raise NodeValidationError(
                "missing_wireguard_address",
                "WireGuard requires a local IP address.",
            )


def _parse_vless(uri: str, source: str, tier: str) -> Node:
    parsed = urlsplit(uri)
    userinfo, address, port = _split_user_host(uri, 443)
    query = parse_qs(parsed.query)
    remarks = unquote(parsed.fragment) if parsed.fragment else address
    return Node(
        name=remarks or address,
        protocol="vless",
        uri=uri,
        address=address,
        port=port,
        source=source,
        tier=tier,
        encryption=_first(query, "encryption", "none"),
        security=_first(query, "security"),
        network=_first(query, "type"),
        host=_first(query, "host"),
        path=_first(query, "path"),
        sni=_first(query, "sni"),
        uuid=userinfo,
        tls=_first(query, "security") in {"tls", "reality"},
        remarks=remarks,
        extra=_query_extra(query),
    )


def _parse_trojan(uri: str, source: str, tier: str) -> Node:
    parsed = urlsplit(uri)
    password, address, port = _split_user_host(uri, 443)
    query = parse_qs(parsed.query)
    remarks = unquote(parsed.fragment) if parsed.fragment else address
    return Node(
        name=remarks or address,
        protocol="trojan",
        uri=uri,
        address=address,
        port=port,
        source=source,
        tier=tier,
        password=password,
        security="tls",
        network=_first(query, "type"),
        host=_first(query, "host"),
        path=_first(query, "path"),
        sni=_first(query, "sni"),
        tls=True,
        remarks=remarks,
        extra=_query_extra(query),
    )


def _parse_vmess(uri: str, source: str, tier: str) -> Node:
    payload = uri.split("://", 1)[1]
    decoded = json.loads(_safe_b64decode(payload).decode("utf-8"))
    address = decoded["add"]
    port = int(decoded["port"])
    tls_enabled = str(decoded.get("tls", "")).lower() in {"tls", "true", "1"}
    return Node(
        name=decoded.get("ps", address),
        protocol="vmess",
        uri=uri,
        address=address,
        port=port,
        source=source,
        tier=tier,
        encryption=decoded.get("scy") or "auto",
        security=decoded.get("tls"),
        network=decoded.get("net"),
        host=decoded.get("host"),
        path=decoded.get("path"),
        sni=decoded.get("sni"),
        uuid=decoded.get("id"),
        tls=tls_enabled,
        remarks=decoded.get("ps"),
        extra=decoded,
    )


def _parse_hysteria2(uri: str, source: str, tier: str) -> Node:
    parsed = urlsplit(uri)
    address = parsed.hostname or ""
    if not address:
        raise ValueError("missing host")
    query = parse_qs(parsed.query)
    remarks = unquote(parsed.fragment) if parsed.fragment else address
    extra = _query_extra(query)
    if "insecure" in query:
        extra["skip-cert-verify"] = _truthy(_first(query, "insecure"))
    return Node(
        name=remarks or address,
        protocol="hysteria2",
        uri=uri,
        address=address,
        port=int(parsed.port or 443),
        source=source,
        tier=tier,
        password=unquote(parsed.username or _first(query, "password", "") or ""),
        sni=_first(query, "sni") or _first(query, "peer"),
        tls=True,
        remarks=remarks,
        extra=extra,
    )


def _parse_anytls(uri: str, source: str, tier: str) -> Node:
    parsed = urlsplit(uri)
    address = parsed.hostname or ""
    if not address:
        raise ValueError("missing host")
    query = parse_qs(parsed.query)
    remarks = unquote(parsed.fragment) if parsed.fragment else address
    extra = _query_extra(query)
    if "insecure" in query:
        extra["skip-cert-verify"] = _truthy(_first(query, "insecure"))
    return Node(
        name=remarks or address,
        protocol="anytls",
        uri=uri,
        address=address,
        port=int(parsed.port or 443),
        source=source,
        tier=tier,
        password=unquote(parsed.username or parsed.password or _first(query, "password", "") or ""),
        sni=_first(query, "sni") or _first(query, "server_name"),
        tls=True,
        remarks=remarks,
        extra=extra,
    )


def _parse_tuic(uri: str, source: str, tier: str) -> Node:
    parsed = urlsplit(uri)
    address = parsed.hostname or ""
    if not address:
        raise ValueError("missing host")
    query = parse_qs(parsed.query)
    remarks = unquote(parsed.fragment) if parsed.fragment else address
    extra = _query_extra(query)
    if "allow_insecure" in query:
        extra["skip-cert-verify"] = _truthy(_first(query, "allow_insecure"))
    if "insecure" in query and "skip-cert-verify" not in extra:
        extra["skip-cert-verify"] = _truthy(_first(query, "insecure"))
    for alias_key, canonical_key in {
        "udp_relay_mode": "udp-relay-mode",
        "congestion_control": "congestion-controller",
        "congestion_controller": "congestion-controller",
        "disable_sni": "disable-sni",
        "reduce_rtt": "reduce-rtt",
        "fast_open": "fast-open",
        "heartbeat_interval": "heartbeat-interval",
        "request_timeout": "request-timeout",
        "max_udp_relay_packet_size": "max-udp-relay-packet-size",
        "max_open_streams": "max-open-streams",
    }.items():
        if alias_key in query and canonical_key not in extra:
            extra[canonical_key] = _first(query, alias_key)
    version_value = _first(query, "version")
    if version_value:
        try:
            extra["version"] = int(version_value)
        except (TypeError, ValueError):
            extra["version"] = version_value
    return Node(
        name=remarks or address,
        protocol="tuic",
        uri=uri,
        address=address,
        port=int(parsed.port or 443),
        source=source,
        tier=tier,
        uuid=unquote(parsed.username or _first(query, "uuid", "") or ""),
        password=unquote(parsed.password or _first(query, "password", "") or _first(query, "token", "") or ""),
        sni=_first(query, "sni") or _first(query, "server_name"),
        tls=True,
        remarks=remarks,
        extra=extra,
    )


def _split_csv(value: Optional[str]) -> list[str]:
    if value is None:
        return []
    return [chunk.strip() for chunk in str(value).split(",") if chunk.strip()]


def _parse_reserved_value(value: Any) -> list[int] | None:
    if value is None:
        return None
    if isinstance(value, list):
        raw = value
    else:
        raw = _split_csv(str(value))
    result: list[int] = []
    for item in raw:
        try:
            result.append(int(str(item).strip()))
        except (TypeError, ValueError):
            return None
    return result if len(result) == 3 else None


def _parse_wireguard(uri: str, source: str, tier: str) -> Node:
    parsed = urlsplit(uri)
    address = parsed.hostname or ""
    if not address:
        raise ValueError("missing host")
    if parsed.username:
        private_key = unquote(parsed.username)
    elif "@" in parsed.netloc:
        private_key = unquote(parsed.netloc.split("@", 1)[0])
    else:
        private_key = ""
    query = parse_qs(parsed.query)
    remarks = unquote(parsed.fragment) if parsed.fragment else address
    extra: dict[str, Any] = {}
    peer_public = (
        _first(query, "publickey")
        or _first(query, "public_key")
        or _first(query, "peer_public_key")
        or _first(query, "peer-public-key")
        or ""
    )
    if peer_public:
        extra["public-key"] = unquote(peer_public)
    preshared = _first(query, "presharedkey") or _first(query, "preshared_key") or _first(query, "preshared-key")
    if preshared:
        extra["pre-shared-key"] = unquote(preshared)
    allowed = _first(query, "allowed_ips") or _first(query, "allowed-ips") or _first(query, "allowedips")
    extra["allowed-ips"] = _split_csv(allowed) or ["0.0.0.0/0", "::/0"]
    address_field = _first(query, "address") or _first(query, "ip") or _first(query, "ipv4")
    addresses = _split_csv(address_field)
    for local_ip in addresses:
        if ":" in local_ip:
            extra.setdefault("ipv6", local_ip.split("/", 1)[0])
        else:
            extra.setdefault("ip", local_ip.split("/", 1)[0])
    reserved = _parse_reserved_value(_first(query, "reserved"))
    if reserved is not None:
        extra["reserved"] = reserved
    mtu_raw = _first(query, "mtu")
    if mtu_raw:
        try:
            extra["mtu"] = int(mtu_raw)
        except (TypeError, ValueError):
            pass
    keepalive = _first(query, "keepalive") or _first(query, "persistent_keepalive")
    if keepalive:
        try:
            extra["persistent-keepalive"] = int(keepalive)
        except (TypeError, ValueError):
            pass
    # AmneziaWG obfuscation params - transparently forward to Mihomo.
    for key in ("jc", "jmin", "jmax", "s1", "s2", "h1", "h2", "h3", "h4"):
        value = _first(query, key)
        if value is None:
            continue
        try:
            extra[key] = int(value)
        except (TypeError, ValueError):
            extra[key] = value
    dns_raw = _first(query, "dns")
    if dns_raw:
        extra["dns"] = _split_csv(dns_raw) or [dns_raw]
    workers_raw = _first(query, "workers")
    if workers_raw:
        try:
            extra["workers"] = int(workers_raw)
        except (TypeError, ValueError):
            pass
    return Node(
        name=remarks or address,
        protocol="wireguard",
        uri=uri,
        address=address,
        port=int(parsed.port or 51820),
        source=source,
        tier=tier,
        password=private_key,
        remarks=remarks,
        extra=extra,
    )


def _parse_naive(uri: str, source: str, tier: str) -> Node:
    parsed = urlsplit(uri)
    if "@" not in parsed.netloc:
        raise ValueError("missing userinfo separator")
    address = parsed.hostname or ""
    if not address:
        raise ValueError("missing host")
    query = parse_qs(parsed.query)
    scheme = parsed.scheme.lower()
    if scheme == "naive":
        security = str(_first(query, "security", "tls") or "tls").strip().lower()
        tls_enabled = security not in {"none", "http", "plain", "false", "0"}
    else:
        tls_enabled = scheme == "https"
    remarks = unquote(parsed.fragment) if parsed.fragment else address
    extra = _query_extra(query)
    extra["username"] = unquote(parsed.username or "")
    extra["skip-cert-verify"] = _truthy(
        _first(query, "insecure") or _first(query, "skip-cert-verify") or "false"
    )
    return Node(
        name=remarks or address,
        protocol="http",
        uri=uri,
        address=address,
        port=int(parsed.port or (443 if tls_enabled else 80)),
        source=source,
        tier=tier,
        password=unquote(parsed.password or ""),
        sni=_first(query, "sni") or _first(query, "server_name"),
        tls=tls_enabled,
        remarks=remarks,
        extra=extra,
    )


def _parse_ss(uri: str, source: str, tier: str) -> Node:
    payload = uri.split("://", 1)[1]
    if "#" in payload:
        payload, fragment = payload.split("#", 1)
        remarks = unquote(fragment)
    else:
        remarks = ""

    query = ""
    if "?" in payload:
        payload, query = payload.split("?", 1)

    if "@" not in payload:
        decoded_payload = _safe_b64decode(payload).decode("utf-8", errors="ignore")
        userinfo, hostinfo = decoded_payload.rsplit("@", 1)
    else:
        userinfo, hostinfo = payload.rsplit("@", 1)
        if ":" not in userinfo:
            userinfo = _safe_b64decode(userinfo).decode("utf-8", errors="ignore")

    method, password = userinfo.split(":", 1)
    address, port = hostinfo.rsplit(":", 1)
    if address.startswith("[") and address.endswith("]"):
        address = address[1:-1]
    query_values = parse_qs(query)
    return Node(
        name=remarks or address,
        protocol="ss",
        uri=uri,
        address=address,
        port=int(port),
        source=source,
        tier=tier,
        method=method,
        password=password,
        plugin=_first(query_values, "plugin"),
        remarks=remarks,
        extra={key: value[0] if len(value) == 1 else value for key, value in query_values.items()},
    )


def _parse_ssr(uri: str, source: str, tier: str) -> Node:
    payload = uri.split("://", 1)[1]
    decoded = _safe_b64decode(payload).decode("utf-8")
    main_part, _, param_part = decoded.partition("/?")
    address, port, protocol, method, obfs, password_b64 = main_part.split(":", 5)
    params = parse_qs(param_part)
    remarks = ""
    if "remarks" in params and params["remarks"]:
        remarks = _safe_b64decode(params["remarks"][0]).decode("utf-8", errors="ignore")
    password = _safe_b64decode(password_b64).decode("utf-8", errors="ignore")
    extra = {key: value[0] if len(value) == 1 else value for key, value in params.items()}
    for raw_key, normalized_key in {
        "obfsparam": "obfs-param",
        "protoparam": "protocol-param",
        "group": "group",
    }.items():
        if raw_key in params and params[raw_key]:
            extra[normalized_key] = _safe_b64decode(params[raw_key][0]).decode("utf-8", errors="ignore")
    return Node(
        name=remarks or address,
        protocol="ssr",
        uri=uri,
        address=address,
        port=int(port),
        source=source,
        tier=tier,
        method=method,
        password=password,
        plugin=obfs,
        remarks=remarks,
        extra={
            "protocol": protocol,
            "obfs": obfs,
            **extra,
        },
    )


def _parse_socks5(uri: str, source: str, tier: str) -> Node:
    parsed = urlsplit(uri)
    address = parsed.hostname or ""
    if not address:
        raise ValueError("missing host")
    query = parse_qs(parsed.query)
    remarks = unquote(parsed.fragment) if parsed.fragment else address
    extra = _query_extra(query)
    username = unquote(parsed.username or _first(query, "username", ""))
    password = unquote(parsed.password or _first(query, "password", ""))
    if username:
        extra["username"] = username
    if "insecure" in query:
        extra["skip-cert-verify"] = _truthy(_first(query, "insecure"))
    return Node(
        name=remarks or address,
        protocol="socks5",
        uri=uri,
        address=address,
        port=int(parsed.port or 1080),
        source=source,
        tier=tier,
        password=password,
        sni=_first(query, "sni"),
        tls=_truthy(_first(query, "tls", "false")),
        remarks=remarks,
        extra=extra,
    )


def parse_many_uris(
    uris: Iterable[str],
    source: str = "",
    tier: str = "",
    logger: Optional[logging.Logger] = None,
) -> list[Node]:
    nodes: list[Node] = []
    for uri in uris:
        node = parse_node_uri(uri, source=source, tier=tier, logger=logger)
        if node:
            nodes.append(node)
    return nodes
