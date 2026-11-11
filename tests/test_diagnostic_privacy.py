import collections
import enum
import json
import math
import os
import pickle
import random
import re
import uuid
from pathlib import Path
from typing import Any

import pytest

import diagnostic_privacy as dp
from diagnostic_privacy import (
    runtime_identity_hash,
    runtime_scoped_hmac,
    sanitize_diagnostic_payload,
)


def test_runtime_identity_hash_is_stable_and_redacted() -> None:
    raw = "vless://user:secret@example.invalid:443/path"
    first = runtime_identity_hash(raw)
    second = runtime_identity_hash(raw)

    assert first == second
    assert raw not in first
    assert "secret" not in first
    assert first.startswith("hmac:")


def test_runtime_hmac_namespaces_do_not_cross_correlate() -> None:
    raw = "same-value"
    assert runtime_scoped_hmac(raw, namespace="node-identity") != (
        runtime_scoped_hmac(raw, namespace="tier3-candidate")
    )


def test_diagnostic_payload_removes_identity_uri_and_uuid_material() -> None:
    node_uri = (
        "vless://123e4567-e89b-42d3-a456-426614174000"
        ":secret@example.invalid:443/path"
    )
    payload = {
        "identity": node_uri,
        "leaf_identity": node_uri,
        "admission_identity": node_uri,
        "attempted_identities": [node_uri],
        "commit_by_identity": {node_uri: {"endpoint": "https://example.invalid"}},
        "endpoint_stats": {"https://example.invalid/private": {"passes": 1}},
        "error": f"failed while fetching {node_uri}",
    }

    sanitized = sanitize_diagnostic_payload(payload, namespace="test")
    serialized = json.dumps(sanitized, sort_keys=True)

    assert "identity" not in sanitized
    assert sanitized["identity_hash"].startswith("hmac:")
    assert sanitized["leaf_identity_hash"].startswith("hmac:")
    assert sanitized["admission_identity_hash"].startswith("hmac:")
    assert "leaf_identity" not in sanitized
    assert "admission_identity" not in sanitized
    assert node_uri not in serialized
    assert "123e4567-e89b-42d3-a456-426614174000" not in serialized
    assert "secret" not in serialized
    assert "example.invalid" not in serialized
    assert all(
        str(value).startswith("hmac:")
        for value in sanitized["commit_by_identity"]
    )


def test_diagnostic_payload_identity_tokens_are_idempotent() -> None:
    raw = "vless://123e4567-e89b-42d3-a456-426614174000@example.invalid"
    payload = {
        "identity": raw,
        "committed_identities": [raw],
        "skipped_by_identity": {raw: "duplicate"},
    }

    first = sanitize_diagnostic_payload(payload, namespace="test")
    second = sanitize_diagnostic_payload(first, namespace="test")

    assert second == first


# ---------------------------------------------------------------------------
# Fast-path exactness (scalar shortcut, ":"/"-" prefilter, schema-key memo).
#
# The two functions below are a FROZEN copy of redact_diagnostic_text and
# sanitize_diagnostic_payload as they were before the fast path.  They share
# the module's regexes and HMAC helpers, so they define the reference output
# for whatever token patterns the module uses; the fast path must reproduce it
# type-exactly and key-order-exactly for every input.
# ---------------------------------------------------------------------------


def _frozen_redact_diagnostic_text(value: Any) -> str:
    text = str(value or "")
    text = dp._URI_TOKEN_RE.sub(
        lambda match: dp._redacted_token(match, namespace="diagnostic-uri"),
        text,
    )
    return dp._UUID_TOKEN_RE.sub(
        lambda match: dp._redacted_token(match, namespace="diagnostic-uuid"),
        text,
    )


def _frozen_sanitize_diagnostic_payload(
    value: Any,
    *,
    namespace: str = "diagnostic",
    key_hint: str = "",
) -> Any:
    normalized_key = str(key_hint or "").strip().lower()
    if isinstance(value, dict):
        identity_keyed = "by_identity" in normalized_key
        sanitized: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            child_key = key.strip().lower()
            if (
                child_key == "identity"
                or (
                    child_key.endswith("_identity")
                    and not child_key.endswith("_identity_hash")
                    and not child_key.endswith("by_identity")
                )
            ):
                output_identity_key = (
                    "identity_hash"
                    if child_key == "identity"
                    else f"{child_key}_hash"
                )
                sanitized[output_identity_key] = dp._sanitized_identity(raw_value)
                continue
            key_contains_sensitive_material = bool(
                dp._URI_TOKEN_RE.search(key) or dp._UUID_TOKEN_RE.search(key)
            )
            output_key = key
            if identity_keyed:
                output_key = dp._sanitized_identity(key)
            elif key_contains_sensitive_material:
                output_key = "hmac:" + dp.runtime_scoped_hmac(
                    key,
                    namespace=namespace + ":key",
                )
            sanitized[output_key] = _frozen_sanitize_diagnostic_payload(
                raw_value,
                namespace=f"{namespace}:{child_key or 'value'}",
                key_hint=child_key,
            )
        return sanitized
    if isinstance(value, list):
        if normalized_key.endswith("_identities"):
            return [dp._sanitized_identity(item) for item in value]
        return [
            _frozen_sanitize_diagnostic_payload(
                item,
                namespace=namespace,
                key_hint=normalized_key,
            )
            for item in value
        ]
    if isinstance(value, tuple):
        return tuple(
            _frozen_sanitize_diagnostic_payload(
                item,
                namespace=namespace,
                key_hint=normalized_key,
            )
            for item in value
        )
    if isinstance(value, str):
        if value.startswith("hmac:") or value.startswith("[redacted:"):
            return value
        if normalized_key in {
            "endpoint",
            "promoted_endpoint",
            "temporary_promoted_endpoint",
            "source_url",
        }:
            return f"hmac:{dp.runtime_scoped_hmac(value, namespace=namespace)}"
        return _frozen_redact_diagnostic_text(value)
    return value


def _assert_same(expected: Any, actual: Any, path: str = "$") -> None:
    """Type-exact, key-order-exact and key-type-exact equality."""

    assert type(actual) is type(expected), (path, type(expected), type(actual))
    if isinstance(expected, dict):
        expected_keys = [(type(key), key) for key in expected]
        actual_keys = [(type(key), key) for key in actual]
        assert actual_keys == expected_keys, path
        for key in expected:
            _assert_same(expected[key], actual[key], f"{path}[{key!r}]")
        return
    if isinstance(expected, (list, tuple)):
        assert len(actual) == len(expected), path
        for index, (left, right) in enumerate(zip(expected, actual)):
            _assert_same(left, right, f"{path}[{index}]")
        return
    if expected is actual:
        return
    if isinstance(expected, float) and math.isnan(expected):
        assert math.isnan(actual), path
        return
    assert actual == expected, (path, expected, actual)


class _Color(enum.IntEnum):
    RED = 1


class _Mode(str, enum.Enum):
    FAST = "fast-probe:x"
    PLAIN = "plain"


class _Tag(str):
    pass


class _BlindStr(str):
    """A str subclass whose ``in`` operator never finds anything."""

    def __contains__(self, item: object) -> bool:
        return False


class _StrSubclassFromStr:
    """An object whose __str__ returns a str subclass instance."""

    def __init__(self, text: str, text_type: type = _Tag) -> None:
        self.text = text
        self.text_type = text_type

    def __str__(self) -> str:
        return self.text_type(self.text)


class _Opaque:
    pass


_Pair = collections.namedtuple("_Pair", "left right")


class _DictSubclass(dict):
    pass


class _ListSubclass(list):
    pass


_SCHEMES = (
    "http", "https", "socks5", "socks5h", "ss", "ssr", "vmess", "vless",
    "trojan", "hysteria", "hysteria2", "hy2", "tuic", "wireguard", "wg",
    "anytls", "naive+https", "file", "ftp", "git+ssh", "a.b-c+d", "HTTPS",
    "VLESS", "Trojan", "x1",
)
_COLON_SCHEMES = ("ss", "vmess", "vless", "trojan", "SS", "VMess", "Trojan",
                  "\u017fs")
_HOSTS = (
    "example.com", "sub.example.co.uk", "10.0.0.1", "127.0.0.1", "[::1]",
    "2001:db8::1", "xn--fiqs8s.cn", "localhost", "host_name",
    "HOST.Example.Invalid", "cp.cloudflare.com", "www.gstatic.com",
)
_SCHEMA_KEYS = (
    "pool_revision", "pool_mutation_ledger", "sources", "units", "error",
    "value", "id", "type", "ok", "node_count", "source_fetch_ok",
    "candidate_scheduler_backlog_count", "source_stream_commit_timeline",
    "tcp_prestart_started_total", "passes_by_root_family",
    "tcp_by_root_family", "endpoint_health", "build_components",
    "x_identity_hash", "identity_hash", "a1", "0", "42",
)
_IDENTITY_KEYS = (
    "identity", "leaf_identity", "admission_identity", "Identity",
    " identity ", "LEAF_IDENTITY", "x_identity_hash", "commit_by_identity",
    "skipped_by_identity", "by_identity", "a_by_identity_map",
    "attempted_identities", "committed_identities", "IDENTITIES",
    "_identities", "endpoint", "promoted_endpoint",
    "temporary_promoted_endpoint", "source_url", "Endpoint", " source_url ",
)


def _random_uuid(rng: random.Random) -> str:
    version = rng.choice((0, 1, 2, 3, 4, 5, 6, 7))
    variant = rng.choice("89ab01cf")
    raw = f"{rng.getrandbits(128):032x}"
    text = (
        f"{raw[:8]}-{raw[8:12]}-{version}{raw[13:16]}-"
        f"{variant}{raw[17:20]}-{raw[20:32]}"
    )
    return text.upper() if rng.random() < 0.2 else text


def _random_text(rng: random.Random) -> str:
    host = rng.choice(_HOSTS)
    port = rng.choice((80, 443, 8388, 57111))
    choices = (
        lambda: "plain",
        lambda: "",
        lambda: " ",
        lambda: "with-hyphen",
        lambda: "a:b",
        lambda: "hmac:already",
        lambda: "[redacted:abc]",
        lambda: "hmac",
        lambda: "[redacted",
        lambda: f"{rng.choice(_SCHEMES)}://user:pw@{host}:{port}/p?q=1#frag",
        lambda: f"{rng.choice(_COLON_SCHEMES)}:{rng.getrandbits(40):x}@{host}",
        lambda: f"failed while fetching {rng.choice(_SCHEMES)}://{host} now",
        lambda: _random_uuid(rng),
        lambda: f"x{_random_uuid(rng)}y",
        lambda: f"uuid:{{{_random_uuid(rng)}}}",
        lambda: str(uuid.UUID(int=rng.getrandbits(128), version=4)),
        lambda: host,
        lambda: f"{host}:{port}",
        lambda: f"github:owner/repo{rng.randint(0, 9)}",
        lambda: "gist:abcd",
        lambda: "telegram:@channel",
        lambda: f"subscription_url:http://127.0.0.1:{port}/sub-0.txt|ss",
        lambda: f"https://www.gstatic.com#{rng.getrandbits(48):012x}",
        lambda: "C:\\Users\\user\\AppData\\Roaming\\sources.yaml",
        lambda: "/usr/lib/python3/site.py",
        lambda: "scripts/tier3_audit_entry.py",
        lambda: "tier3_build_identity.py",
        lambda: "ssx:nope",
        lambda: "vmes:nope",
        lambda: "trojan;nope",
        lambda: "http:nope",
        lambda: "mailto:x@y.invalid",
        lambda: "tel:+1-555-0100",
        lambda: "trojan\uff1afullwidth",
        lambda: "ss\u2010unicode-hyphen",
        lambda: "\u8282\u70b9-\u9999\u6e2f-01",
        lambda: "\u30ce\u30fc\u30c9",
        lambda: "\u00dcberpr\u00fcfung",
        lambda: "rocket \U0001f680",
        lambda: "zero\u200bwidth",
        lambda: "\u05e9\u05dc\u05d5\u05dd",
        lambda: "\u212aelvin",
        lambda: "tcp_prestart_started_total",
    )
    return rng.choice(choices)()


def _random_key(rng: random.Random) -> Any:
    roll = rng.random()
    if roll < 0.40:
        return rng.choice(_SCHEMA_KEYS)
    if roll < 0.58:
        return rng.choice(_IDENTITY_KEYS)
    if roll < 0.85:
        return _random_text(rng)
    return rng.choice(
        (
            lambda: rng.randint(-5, 9),
            lambda: 1.5,
            lambda: None,
            lambda: True,
            lambda: (1, "vmess://x"),
            lambda: _Color.RED,
            lambda: _Mode.FAST,
            lambda: _Tag("tag:key"),
            lambda: _StrSubclassFromStr("weird-key"),
            lambda: _StrSubclassFromStr("pool_revision"),
            lambda: _StrSubclassFromStr("vmess://blind", _BlindStr),
            lambda: _StrSubclassFromStr(_random_uuid(rng), _BlindStr),
            lambda: _StrSubclassFromStr("pool_revision", _BlindStr),
        )
    )()


def _random_leaf(rng: random.Random) -> Any:
    return rng.choice(
        (
            lambda: rng.randint(-5, 10**6),
            lambda: 2**70,
            lambda: rng.random(),
            lambda: float("nan"),
            lambda: float("inf"),
            lambda: -0.0,
            lambda: True,
            lambda: False,
            lambda: None,
            lambda: _Color.RED,
            lambda: _Mode.FAST,
            lambda: _Mode.PLAIN,
            lambda: _Tag("t-a:g"),
            lambda: _Tag("plain"),
            lambda: b"bytes:with-colon",
            lambda: bytearray(b"x"),
            lambda: {1, 2},
            lambda: frozenset({"vmess://x"}),
            lambda: _Opaque(),
            lambda: _StrSubclassFromStr("vless://x"),
            lambda: _BlindStr("trojan://blind@h:443"),
            lambda: _random_text(rng),
            lambda: _random_text(rng),
            lambda: _random_text(rng),
        )
    )()


def _random_payload(rng: random.Random, depth: int = 0) -> Any:
    roll = rng.random()
    if depth > 3 or roll < 0.35:
        return _random_leaf(rng)
    if roll < 0.70:
        factory = rng.choice((dict, dict, dict, collections.OrderedDict,
                              _DictSubclass))
        out = factory()
        for _ in range(rng.randint(0, 7)):
            out[_random_key(rng)] = _random_payload(rng, depth + 1)
        return out
    if roll < 0.88:
        items = [_random_payload(rng, depth + 1) for _ in range(rng.randint(0, 5))]
        return _ListSubclass(items) if rng.random() < 0.1 else items
    if rng.random() < 0.1:
        return _Pair(_random_payload(rng, depth + 1), _random_leaf(rng))
    return tuple(_random_payload(rng, depth + 1) for _ in range(rng.randint(0, 3)))


_KEY_HINTS = (
    "", "commit_by_identity", "attempted_identities", "endpoint",
    "Source_URL ", "sources", "x_by_identity_map", "passes_by_root_family",
)
_NAMESPACES = ("diagnostic", "tier3-collection-report", "runtime-route-audit",
               "test")


def test_sanitize_fast_path_matches_frozen_reference_on_random_payloads() -> None:
    dp._KEY_CLASS_CACHE.clear()
    rng = random.Random(20260924)
    for case in range(4000):
        payload = _random_payload(rng)
        namespace = rng.choice(_NAMESPACES)
        key_hint = rng.choice(_KEY_HINTS)
        expected = _frozen_sanitize_diagnostic_payload(
            payload, namespace=namespace, key_hint=key_hint,
        )
        # Twice: the second call runs with the key memo warm.
        for _attempt in range(2):
            actual = sanitize_diagnostic_payload(
                payload, namespace=namespace, key_hint=key_hint,
            )
            _assert_same(expected, actual, f"case {case}")
        # Already-sanitized output must also be handled identically.
        again_expected = _frozen_sanitize_diagnostic_payload(
            expected, namespace=namespace, key_hint=key_hint,
        )
        again_actual = sanitize_diagnostic_payload(
            expected, namespace=namespace, key_hint=key_hint,
        )
        _assert_same(again_expected, again_actual, f"case {case} (again)")


def test_redact_fast_path_matches_frozen_reference() -> None:
    rng = random.Random(7)
    values: list[Any] = [
        None, 0, 1, 0.0, False, True, "", [], {}, b"", b"ss:x", _Color.RED,
        _Mode.FAST, _Mode.PLAIN, _Tag("plain"), _Tag("vmess:x"),
        _StrSubclassFromStr("plain"), _StrSubclassFromStr("vless://x"),
        _StrSubclassFromStr("vless://x", _BlindStr), _BlindStr("ss:blind"),
        ValueError("failed for trojan://p@h:443"), ValueError("plain"),
        _Opaque(),
    ]
    values.extend(_random_text(rng) for _ in range(3000))
    values.extend(_random_leaf(rng) for _ in range(1000))
    for value in values:
        _assert_same(
            _frozen_redact_diagnostic_text(value),
            dp.redact_diagnostic_text(value),
            repr(value)[:80],
        )


def _captured_payloads() -> list[tuple[str, Any]]:
    """Real reports captured by the local pipeline probe, when present.

    Optional corpus: ``DIAGNOSTIC_PRIVACY_CAPTURE_DIR`` overrides the default
    ``.build/claude-probes/out``; the test skips when nothing is found.
    """

    root = Path(
        os.environ.get("DIAGNOSTIC_PRIVACY_CAPTURE_DIR")
        or Path(__file__).resolve().parents[1] / ".build" / "claude-probes" / "out"
    )
    payloads: list[tuple[str, Any]] = []
    for run in ("rpt-probe-new-c-s1b", "rpt-pilot-A-new-c-s1"):
        capture = root / run / "reporter_capture.pickle"
        if capture.is_file():
            with capture.open("rb") as handle:
                captured = pickle.load(handle)
            for heartbeat, entry in sorted(captured.items()):
                for part in ("report_before", "progress_fields",
                             "refresh_progress_before"):
                    if isinstance(entry.get(part), dict):
                        payloads.append((f"{run}:{heartbeat}:{part}", entry[part]))
        session = root / run / "session"
        for name, section in (("tier3-source-state.json", "collection"),
                              ("nodes.json", None)):
            path = session / name
            if not path.is_file():
                continue
            metadata = json.loads(path.read_text(encoding="utf-8")).get("metadata")
            value = metadata.get(section) if section else metadata
            if isinstance(value, dict):
                # Persisted reports are already sanitized: idempotence inputs.
                payloads.append((f"{run}:{name}", value))
    return payloads


def test_sanitize_fast_path_matches_frozen_reference_on_captured_reports() -> None:
    payloads = _captured_payloads()
    if not payloads:
        pytest.skip("no captured reports in the optional local corpus")
    dp._KEY_CLASS_CACHE.clear()
    for label, payload in payloads:
        expected = _frozen_sanitize_diagnostic_payload(
            payload, namespace="tier3-collection-report",
        )
        for _attempt in range(2):
            actual = sanitize_diagnostic_payload(
                payload, namespace="tier3-collection-report",
            )
            _assert_same(expected, actual, label)
        _assert_same(
            _frozen_sanitize_diagnostic_payload(
                expected, namespace="tier3-collection-report",
            ),
            sanitize_diagnostic_payload(
                expected, namespace="tier3-collection-report",
            ),
            f"{label} (again)",
        )
    assert all(dp._SCHEMA_KEY_RE.fullmatch(key) for key in dp._KEY_CLASS_CACHE)


def _classification_is_cached(key: str) -> bool:
    cached = dp._KEY_CLASS_CACHE.get(key)
    return cached is not None and (
        dp._classify_diagnostic_key(key, cacheable=True) is cached
    )


def test_key_class_cache_holds_only_schema_shaped_keys() -> None:
    dp._KEY_CLASS_CACHE.clear()
    node_uri = "vless://123e4567-e89b-42d3-a456-426614174000@example.invalid"
    payload = {
        "pool_revision": 1,
        "identity": node_uri,
        "leaf_identity": node_uri,
        node_uri: 1,
        "123e4567-e89b-42d3-a456-426614174000": 2,
        "ss:secret": 3,
        "example.com": 4,
        "example.com:443": 5,
        "github:owner/repo": 6,
        "subscription_url:http://127.0.0.1:57111/sub-0.txt|ss": 7,
        "https://www.gstatic.com#9db3ec07b794": 8,
        "scripts/tier3_audit_entry.py": 9,
        "manager.py": 10,
        "\u8282\u70b9": 11,
        "Mixed_Case": 12,
        " padded ": 13,
        "passes_by_root_family": {"github:owner/repo": 1, "local": 2},
        "commit_by_identity": {"plainidentitykey": 1, node_uri: 2},
    }
    sanitize_diagnostic_payload(payload, namespace="test")

    cached = set(dp._KEY_CLASS_CACHE)
    assert cached, "schema keys must be memoised"
    assert all(re.fullmatch(r"[a-z0-9_]+", key) for key in cached)
    assert all(type(key) is str for key in cached)
    assert not any(
        dp._URI_TOKEN_RE.search(key) or dp._UUID_TOKEN_RE.search(key)
        for key in cached
    )
    # Keys of *by_identity* maps are node identities: never retained, even
    # when schema-shaped.
    assert "plainidentitykey" not in cached
    assert {"pool_revision", "passes_by_root_family", "local"} <= cached
    for key, classification in dp._KEY_CLASS_CACHE.items():
        assert dp._classify_diagnostic_key(key, cacheable=False) == classification


def test_key_class_cache_survives_dynamic_key_floods() -> None:
    dp._KEY_CLASS_CACHE.clear()
    limit = dp._KEY_CLASS_CACHE_LIMIT
    dynamic = {}
    for index in range(limit + 1000):
        dynamic[f"host-{index}.example.invalid:443"] = index
        dynamic[f"github:owner/repo{index}"] = index
        dynamic[f"\u8282\u70b9{index}"] = index
    sanitize_diagnostic_payload({"endpoint_health": dynamic}, namespace="test")
    assert set(dp._KEY_CLASS_CACHE) == {"endpoint_health"}

    schema_payload = {key: 1 for key in _SCHEMA_KEYS}
    sanitize_diagnostic_payload(schema_payload, namespace="test")
    assert all(_classification_is_cached(key) for key in _SCHEMA_KEYS)

    # Dynamic keys that happen to be schema-shaped (numeric map keys) are
    # bounded, and a full memo cannot disable it: it is cleared when full, so
    # schema keys seen afterwards are memoised again.
    for round_index in range(3):
        numeric = {str(round_index * limit + index): index for index in range(limit)}
        sanitize_diagnostic_payload(numeric, namespace="test")
        assert len(dp._KEY_CLASS_CACHE) <= limit
    dp._KEY_CLASS_CACHE.clear()
    sanitize_diagnostic_payload(
        {str(index): index for index in range(limit)}, namespace="test",
    )
    assert len(dp._KEY_CLASS_CACHE) == limit
    assert not any(key in dp._KEY_CLASS_CACHE for key in _SCHEMA_KEYS[:5])
    sanitize_diagnostic_payload(schema_payload, namespace="test")
    assert all(_classification_is_cached(key) for key in _SCHEMA_KEYS)
    assert len(dp._KEY_CLASS_CACHE) <= limit


def _must_contain(items: Any, char: str) -> bool:
    """Sound check that every match of a parsed pattern contains ``char``."""

    from re import _constants as constants

    target = ord(char)
    for op, av in items:
        if op is constants.LITERAL and av == target:
            return True
        if op is constants.SUBPATTERN and _must_contain(av[-1], char):
            return True
        if op is constants.ATOMIC_GROUP and _must_contain(av, char):
            return True
        if op is constants.BRANCH and all(
            _must_contain(branch, char) for branch in av[1]
        ):
            return True
        if (
            op in (constants.MAX_REPEAT, constants.MIN_REPEAT,
                   constants.POSSESSIVE_REPEAT)
            and av[0] >= 1
            and _must_contain(av[2], char)
        ):
            return True
        if op is constants.IN and list(av) == [(constants.LITERAL, target)]:
            return True
    return False


def test_prefilter_characters_are_required_by_the_token_patterns() -> None:
    from re import _parser

    uri = _parser.parse(dp._URI_TOKEN_RE.pattern, dp._URI_TOKEN_RE.flags)
    uuid_pattern = _parser.parse(dp._UUID_TOKEN_RE.pattern, dp._UUID_TOKEN_RE.flags)
    assert _must_contain(uri, ":")
    assert _must_contain(uuid_pattern, "-")

    # Case-insensitive matching must not let another code point stand in for
    # ":" or "-" (both patterns use (?i)).
    everything = "".join(
        chr(code)
        for code in range(0x110000)
        if not 0xD800 <= code <= 0xDFFF and chr(code) not in ":-"
    )
    assert re.search(r"(?i):", everything) is None
    assert re.search(r"(?i)-", everything) is None


def test_text_without_prefilter_characters_never_matches_a_token_pattern() -> None:
    rng = random.Random(11)
    alphabet = (
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        "/.+@_?#=&%~ \t\n'\"<>[]{}()|\\\u017f\u212a\u2010\u2212\uff1a\u8282"
    )
    fragments = ("ss", "vmess", "vless", "trojan", "http", "//",
                 "123e4567", "e89b", "42d3", "a456", "426614174000")
    for _ in range(5000):
        text = "".join(
            rng.choice(fragments) if rng.random() < 0.3 else rng.choice(alphabet)
            for _ in range(rng.randint(0, 40))
        )
        assert ":" not in text and "-" not in text
        assert dp._URI_TOKEN_RE.search(text) is None, text
        assert dp._UUID_TOKEN_RE.search(text) is None, text
        assert dp.redact_diagnostic_text(text) == text
        assert type(dp.redact_diagnostic_text(_Tag(text))) is str

    weird = _StrSubclassFromStr("plain")
    assert type(str(weird)) is _Tag
    assert type(dp.redact_diagnostic_text(weird)) is str
    assert dp.redact_diagnostic_text(None) == ""
    assert dp.redact_diagnostic_text(0) == ""
    assert dp.redact_diagnostic_text(12) == "12"
    assert dp.redact_diagnostic_text(b"x") == "b'x'"
