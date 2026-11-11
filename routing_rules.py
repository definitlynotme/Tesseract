"""The user's routing rules as one ordered list (配置整理 P2, 2026-11-04).

``routing.rules.list`` holds the rules in match order. Each row is one
condition and its action::

    {"to": "direct" | "proxy" | "reject", "kind": <kind>, "value": <payload>}

Optional keys:
    not      the row matches when its condition does not hold (Clash NOT)
    resolve  an IP condition also matches host names by resolving them (the
             Clash rule without ``no-resolve``; absent: ``no-resolve``)
    off      the row is kept but not compiled; ``note`` says why (NOTES)

``kind`` ``and`` / ``or`` combines up to MAX_OF conditions in ``of``
([{"kind", "value", "not"?}], one level). ``kind`` ``clash`` keeps a stored
Clash rule the list cannot express (``value`` "TYPE,payload" plus
``params``), compiled as it was written.

Tesseract's own rules are never in the list: clash_formatter puts its system
rules before it and ``MATCH,GENERAL`` after it, so a row only routes traffic
Tesseract does not route itself. The page offers only the three actions; the
proxy action is Tesseract's whole route (GENERAL).

Without the key the legacy layout (the direct / proxy / reject lists in
``routing.rule_order``, then ``rule_sets`` and ``raw``) is read as a list
(``legacy_rule_list``) that compiles to the very rule strings the legacy
compile produced. Two kinds of hand-written raw rule differ, both kept as
``off`` rows: one whose policy the legacy compile dropped (DIRECT,
COMPATIBLE: note ``dropped_policy``) and one aimed at a group the list cannot
name (Tesseract's internal groups: note ``target_gone``, behaviour change,
shown on the page). A raw rule targeting FINAL-EXIT reads as the proxy action
(GENERAL selects FINAL-EXIT).
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit


KIND_TYPES: dict[str, str] = {
    "domain": "DOMAIN",
    "suffix": "DOMAIN-SUFFIX",
    "keyword": "DOMAIN-KEYWORD",
    "regex": "DOMAIN-REGEX",
    "geosite": "GEOSITE",
    "geoip": "GEOIP",
    "ip-cidr": "IP-CIDR",
    "ip-asn": "IP-ASN",
    "src-ip-cidr": "SRC-IP-CIDR",
    "dst-port": "DST-PORT",
    "src-port": "SRC-PORT",
    "process": "PROCESS-NAME",
    "process-path": "PROCESS-PATH",
    "network": "NETWORK",
    "rule-set": "RULE-SET",
}
TYPE_KINDS: dict[str, str] = {
    **{rule_type: kind for kind, rule_type in KIND_TYPES.items()},
    "IP-CIDR6": "ip-cidr",
    "SRC-IP-CIDR6": "src-ip-cidr",
}
# Conditions Clash resolves a host name for unless the rule says no-resolve.
IP_KINDS = frozenset({"geoip", "ip-cidr", "ip-asn"})
LOGIC_KINDS = ("and", "or")
LOGIC_KINDS_UPPER = ("AND", "OR")
TARGETS: dict[str, str] = {"direct": "DIRECT", "proxy": "GENERAL", "reject": "REJECT"}
TARGET_OF: dict[str, str] = {
    "DIRECT": "direct",
    "COMPATIBLE": "direct",
    "GENERAL": "proxy",
    "FINAL-EXIT": "proxy",
    "REJECT": "reject",
    "REJECT-DROP": "reject",
}
NOTES = ("dropped_policy", "target_gone", "unreadable")
MAX_RULES = 2000
MAX_OF = 8
PROVIDER_BEHAVIORS = ("domain", "ipcidr", "classical")
PROVIDER_FORMATS = ("yaml", "text", "mrs")
PROVIDER_INTERVAL_SECONDS = 86400
# The local and LAN destinations clash_formatter.LOCAL_NETWORK_SYSTEM_RULES
# always sends direct before the user's rules.
LOCAL_SUFFIXES = ("localhost", "local", "lan", "home.arpa")
LOCAL_NETWORKS = tuple(ipaddress.ip_network(net) for net in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
    "::1/128", "fc00::/7", "fe80::/10",
))
OWN_PROGRAMS = frozenset({"tesseract.exe", "tesseract-runtime.exe", "mihomo.exe"})

_DOMAIN = re.compile(r"(?=.{1,253}$)[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?(?:\.[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?)*")
_KEYWORD = re.compile(r"[a-z0-9._-]{1,100}")
_GEO = re.compile(r"[A-Za-z0-9!@_-]{1,64}")
_PORTS = re.compile(r"\d{1,5}(?:-\d{1,5})?(?:/\d{1,5}(?:-\d{1,5})?)*")
_PROCESS = re.compile(r"[^\\/:*?\"<>|,()\r\n]{1,255}")
_PROVIDER_NAME = re.compile(r"[^,()\r\n\t]{1,40}")
_CLASH_TYPE = re.compile(r"[A-Z][A-Z0-9-]*")
# Go's RE2 (Mihomo) has no look-around and no back-references.
_RE2_UNSUPPORTED = re.compile(r"\(\?[=!<]|\\[1-9]")


class RuleError(ValueError):
    """An invalid row: ``path`` names it, ``reason`` says why (REASONS)."""

    def __init__(self, path: str, reason: str = "invalid") -> None:
        super().__init__(path)
        self.path = path
        self.reason = reason


# ---------------------------------------------------------------------------
# compile


def _target(row: Mapping[str, Any]) -> str | None:
    return TARGETS.get(str(row.get("to") or ""))


def _condition_parts(item: Mapping[str, Any]) -> tuple[str, str, list[str]] | None:
    """(Clash type, payload, params) of one simple condition, or None."""

    kind = str(item.get("kind") or "")
    value = str(item.get("value") or "").strip()
    rule_type = KIND_TYPES.get(kind)
    if rule_type is None or not value or "," in value or "\n" in value:
        return None
    if kind in {"ip-cidr", "src-ip-cidr"}:
        try:
            network = ipaddress.ip_network(value, strict=False)
        except ValueError:
            return None
        if network.version == 6:
            rule_type = f"{rule_type}6"
    params = ["no-resolve"] if kind in IP_KINDS and not item.get("resolve") else []
    return rule_type, value, params


def _nested(item: Mapping[str, Any]) -> str | None:
    parts = _condition_parts(item)
    if parts is None:
        return None
    rule_type, value, params = parts
    text = ",".join([rule_type, value, *params])
    return f"(NOT,(({text})))" if item.get("not") else f"({text})"


def compile_rule(row: Any) -> str | None:
    """One row → its Clash rule string; None for an off or unusable row."""

    if not isinstance(row, Mapping) or row.get("off"):
        return None
    target = _target(row)
    if target is None:
        return None
    kind = str(row.get("kind") or "")
    if kind == "clash":
        value = str(row.get("value") or "").strip()
        if "," not in value or "\n" in value or not _CLASH_TYPE.fullmatch(value.split(",", 1)[0]):
            return None
        params = [str(p).strip() for p in row.get("params") or () if str(p).strip()]
        return ",".join([value, target, *params])
    if kind in LOGIC_KINDS:
        items = row.get("of")
        if not isinstance(items, list) or not 2 <= len(items) <= MAX_OF:
            return None
        nested = [_nested(item) if isinstance(item, Mapping) else None for item in items]
        if any(part is None for part in nested):
            return None
        return f"{kind.upper()},({','.join(nested)}),{target}"
    parts = _condition_parts(row)
    if parts is None:
        return None
    rule_type, value, params = parts
    if row.get("not"):
        return f"NOT,(({','.join([rule_type, value, *params])})),{target}"
    return ",".join([rule_type, value, target, *params])


def compile_rule_list(rows: Any) -> list[str]:
    """The rows in order, as Clash rules; a repeated rule is kept once."""

    rules: list[str] = []
    seen: set[str] = set()
    for row in rows if isinstance(rows, list) else ():
        text = compile_rule(row)
        if text and text not in seen:
            seen.add(text)
            rules.append(text)
    return rules


# ---------------------------------------------------------------------------
# reading Clash rule strings (the legacy layout, compiled)


def _split_logic(text: str) -> tuple[str, str, list[str]] | None:
    """``TYPE,(…),TARGET[,params]`` → (TYPE, payload with its parentheses, rest)."""

    head, sep, rest = text.partition(",")
    if not sep or not rest.startswith("("):
        return None
    depth = 0
    for index, char in enumerate(rest):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                payload, tail = rest[: index + 1], rest[index + 1:]
                if tail and not tail.startswith(","):
                    return None
                return head.strip().upper(), payload, [p.strip() for p in tail[1:].split(",")] if tail else []
    return None


def _sub_rules(payload: str) -> list[str] | None:
    """``((A,x),(B,y))`` → ["A,x", "B,y"] (each with its own parentheses removed)."""

    if not (payload.startswith("(") and payload.endswith(")")):
        return None
    inner = payload[1:-1]
    items, depth, start = [], 0, None
    for index, char in enumerate(inner):
        if char == "(":
            if depth == 0:
                start = index + 1
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0 and start is not None:
                items.append(inner[start:index])
                start = None
        elif depth == 0 and char not in ", ":
            return None
    return items if depth == 0 and items else None


def _simple_item(text: str) -> dict[str, Any] | None:
    """``TYPE,payload[,no-resolve]`` (inside a logic rule) → a condition, or None."""

    if text.upper().startswith("NOT,"):
        split = _split_logic(text)
        if split is None or split[2]:
            return None
        subs = _sub_rules(split[1])
        if not subs or len(subs) != 1:
            return None
        item = _simple_item(subs[0])
        if item is None or item.get("not"):
            return None
        return {**item, "not": True}
    parts = [p.strip() for p in text.split(",")]
    kind = TYPE_KINDS.get(parts[0].upper()) if parts else None
    if kind is None or len(parts) < 2 or not parts[1]:
        return None
    params = parts[2:]
    if params not in ([], ["no-resolve"]):
        return None
    item: dict[str, Any] = {"kind": kind, "value": parts[1]}
    if kind in IP_KINDS and not params:
        item["resolve"] = True
    elif params and kind not in IP_KINDS:
        return None
    return item


def rule_from_text(text: str, *, dropped: bool = False) -> dict[str, Any]:
    """A compiled (or hand-written) Clash rule → a row that compiles back to it.

    ``dropped``: the legacy compile skipped this rule (a raw rule naming a
    policy the old lists may not use): the row is kept off.
    """

    text = str(text or "").strip()
    head, _, after = text.partition(",")
    head = head.strip().upper()
    # (a payload in parentheses holds commas of its own: AND / OR / NOT, SUB-RULE …)
    if head in LOGIC_KINDS_UPPER or head == "NOT" or after.lstrip().startswith("("):
        split = _split_logic(text)
        if split is not None:
            rule_type, payload, rest = split
            target_name, params = (rest[0] if rest else ""), rest[1:]
            row = _with_target(_clash_row(f"{rule_type},{payload}", params), target_name)
            subs = _sub_rules(payload)
            items = [_simple_item(sub) for sub in subs] if subs else None
            if not params and items and all(items):
                if rule_type == "NOT" and len(items) == 1 and not items[0].get("not"):
                    row = {key: value for key, value in row.items() if key not in {"kind", "value", "params"}}
                    row.update({**items[0], "not": True})
                elif rule_type in {"AND", "OR"} and 2 <= len(items) <= MAX_OF:
                    row = {key: value for key, value in row.items() if key not in {"value", "params"}}
                    row.update({"kind": rule_type.lower(), "of": items})
            return _dropped(row) if dropped else row
        return _off({"to": "proxy", "kind": "clash", "value": text}, "unreadable")
    parts = [p.strip() for p in text.split(",")]
    if len(parts) < 3 or not parts[1]:
        return _off({"to": "proxy", "kind": "clash", "value": text}, "unreadable")
    rule_type, payload, target_name, params = parts[0].upper(), parts[1], parts[2], parts[3:]
    kind = TYPE_KINDS.get(rule_type)
    simple_params = params in ([], ["no-resolve"]) and (kind in IP_KINDS or not params)
    if kind is not None and simple_params and rule_type not in {"MATCH", "FINAL"}:
        row: dict[str, Any] = {"kind": kind, "value": payload}
        if kind in IP_KINDS and not params:
            row["resolve"] = True
    else:
        row = _clash_row(f"{parts[0]},{payload}", params)
    row = _with_target(row, target_name)
    if rule_type in {"MATCH", "FINAL"}:
        return _off(row, "unreadable")
    return _dropped(row) if dropped else row


def _clash_row(value: str, params: list[str]) -> dict[str, Any]:
    row: dict[str, Any] = {"kind": "clash", "value": value}
    if params:
        row["params"] = params
    return row


def _with_target(row: dict[str, Any], target_name: str) -> dict[str, Any]:
    to = TARGET_OF.get(str(target_name or "").strip().upper())
    if to is None:
        return _off({**row, "to": "proxy"}, "target_gone")
    return {**row, "to": to}


def _off(row: dict[str, Any], note: str) -> dict[str, Any]:
    return {**row, "off": True, "note": note}


def _dropped(row: dict[str, Any]) -> dict[str, Any]:
    return row if row.get("off") else _off(row, "dropped_policy")


def _clash_of(text: str) -> dict[str, Any]:
    """The compiled rule as a Clash row (no target): what rule_from_text keeps when it cannot
    name the rule's parts."""

    head, _, after = text.partition(",")
    split = _split_logic(text) if head.strip().upper() in LOGIC_KINDS_UPPER or head.strip().upper() == "NOT" or after.lstrip().startswith("(") else None
    if split is not None:
        rule_type, payload, rest = split
        return _clash_row(f"{rule_type},{payload}", rest[1:])
    parts = [p.strip() for p in text.split(",")]
    return _clash_row(f"{parts[0]},{parts[1]}", parts[3:])


def _kept_whole(row: dict[str, Any], text: str, hosts: frozenset[str]) -> dict[str, Any]:
    """A row read from a legacy string that the page's checks would refuse stays the Clash
    rule it was: a value the old compile took as written (DOMAIN-SUFFIX,domain-suffix:x), or
    a condition Tesseract's own rules always matched first (a LAN address proxied, one of its
    own hosts or programs). It compiles exactly as before and never blocks a later save."""

    if row.get("kind") == "clash":
        return row
    try:
        for item in row["of"] if row.get("kind") in LOGIC_KINDS else [row]:
            _normal_condition(item, "routing.rule_list", None, hosts, allow_resolve=True, to=row["to"])
    except RuleError:
        kept = {**_clash_of(text), "to": row["to"]}
        if row.get("off"):
            kept.update({"off": True, "note": row["note"]})
        return kept
    return row


def legacy_rule_list(routing_config: Any) -> list[dict[str, Any]]:
    """The legacy layout read as one list (see the module docstring)."""

    from clash_formatter import legacy_routing_rule_entries

    hosts = reserved_hosts()
    return [_kept_whole(rule_from_text(text, dropped=dropped), text.strip(), hosts)
            for text, dropped in legacy_routing_rule_entries(routing_config)]


def stored_rule_list(routing_config: Any) -> list[Any] | None:
    """``routing.rules.list`` when the configuration holds one (a list), else None."""

    if not isinstance(routing_config, Mapping):
        return None
    rules = routing_config.get("rules")
    if isinstance(rules, Mapping) and isinstance(rules.get("list"), list):
        return list(rules["list"])
    return None


def effective_rule_list(routing_config: Any) -> list[Any]:
    """What the user's routing is: the stored list, else the legacy layout read as one."""

    stored = stored_rule_list(routing_config)
    return stored if stored is not None else legacy_rule_list(routing_config)


# ---------------------------------------------------------------------------
# validation (the page's list: settings.validate / settings.apply)


def reserved_hosts() -> frozenset[str]:
    """Host names Tesseract's own system rules route (exit checks, probes, rule data)."""

    from clash_formatter import DEFAULT_GEOX_URL
    from exit_quality_endpoints import EXIT_QUALITY_SYSTEM_HOSTS
    from runtime_settings import DEFAULT_PROBE_URLS

    hosts = set(EXIT_QUALITY_SYSTEM_HOSTS)
    for url in (*DEFAULT_PROBE_URLS, *DEFAULT_GEOX_URL.values()):
        host = urlsplit(url).hostname
        if host:
            hosts.add(host.lower())
    hosts.update(("connectivitycheck.gstatic.com", "www.gstatic.com", "cp.cloudflare.com"))
    return frozenset(host.lower() for host in hosts if host)


def local_only(kind: str, value: str) -> bool:
    """A condition that holds only for local or LAN destinations (always direct)."""

    value = value.strip().lower()
    if kind in {"domain", "suffix"}:
        return any(value == suffix or value.endswith(f".{suffix}") for suffix in LOCAL_SUFFIXES)
    if kind == "ip-cidr":
        try:
            network = ipaddress.ip_network(value, strict=False)
        except ValueError:
            return False
        return any(network.version == local.version and network.subnet_of(local) for local in LOCAL_NETWORKS)
    if kind == "geoip":
        return value in {"private", "lan"}
    if kind == "geosite":
        return value == "private"
    return False


def own_program(kind: str, value: str) -> bool:
    if kind not in {"process", "process-path"}:
        return False
    name = re.split(r"[\\/]", value.strip())[-1].lower()
    return name in OWN_PROGRAMS


def _normal_value(kind: str, value: Any, path: str, providers: set[str] | None) -> str:
    """A condition's payload, checked; kept as written (only a bare IP gets its prefix length,
    an ASN loses its ``AS``), so a row read from the configuration and sent back unchanged
    is the same row."""

    if not isinstance(value, str):
        raise RuleError(path)
    value = value.strip()
    if not value or "," in value or "\n" in value or "\r" in value:
        raise RuleError(path)
    lowered = value.lower()
    if kind in {"domain", "suffix"}:
        if kind == "suffix" and lowered[:2] in {"+.", "*."}:
            value, lowered = value[2:], lowered[2:]
        if not _DOMAIN.fullmatch(lowered):
            raise RuleError(path)
        return value
    if kind == "keyword":
        if not _KEYWORD.fullmatch(lowered):
            raise RuleError(path)
        return value
    if kind == "regex":
        if len(value) > 300 or _RE2_UNSUPPORTED.search(value):
            raise RuleError(path)
        try:
            re.compile(value)
        except re.error as exc:
            raise RuleError(path) from exc
        return value
    if kind in {"geosite", "geoip"}:
        if not _GEO.fullmatch(value):
            raise RuleError(path)
        return value
    if kind in {"ip-cidr", "src-ip-cidr"}:
        try:
            network = ipaddress.ip_network(value, strict=False)
        except ValueError as exc:
            raise RuleError(path) from exc
        return value if "/" in value else network.with_prefixlen
    if kind == "ip-asn":
        digits = lowered[2:] if lowered.startswith("as") else lowered
        if not digits.isdigit() or not 0 < int(digits) < 2**32:
            raise RuleError(path)
        return str(int(digits))
    if kind in {"dst-port", "src-port"}:
        if not _PORTS.fullmatch(value):
            raise RuleError(path)
        for part in value.split("/"):
            low, _, high = part.partition("-")
            low_n, high_n = int(low), int(high or low)
            if not 0 < low_n <= high_n <= 65535:
                raise RuleError(path)
        return value
    if kind == "process":
        if not _PROCESS.fullmatch(value):
            raise RuleError(path)
        return value
    if kind == "process-path":
        if len(value) > 260 or not re.search(r"[\\/]", value) or "(" in value or ")" in value:
            raise RuleError(path)
        return value
    if kind == "network":
        if lowered not in {"tcp", "udp"}:
            raise RuleError(path)
        return value
    if kind == "rule-set":
        # (providers None: an off row, never compiled, may keep naming a removed rule set)
        if not _PROVIDER_NAME.fullmatch(value) or (providers is not None and value not in providers):
            raise RuleError(path, "unknown_set")
        return value
    raise RuleError(path)


def _normal_condition(item: Any, path: str, providers: set[str] | None, hosts: frozenset[str],
                      *, allow_resolve: bool, to: str) -> dict[str, Any]:
    """One condition, checked. The conflict guard refuses what would contradict Tesseract's
    own rules, which match first: a local or LAN destination with an action other than direct
    (they are always direct), a host Tesseract routes itself with an action other than proxy,
    and Tesseract's own programs with any action. A negated condition matches the rest of the
    traffic, never one of these."""

    if not isinstance(item, Mapping):
        raise RuleError(path)
    kind = item.get("kind")
    if kind not in KIND_TYPES:
        raise RuleError(f"{path}.kind")
    negated = item.get("not", False)
    if not isinstance(negated, bool):
        raise RuleError(f"{path}.not")
    value = _normal_value(kind, item.get("value"), f"{path}.value", providers)
    if not negated:
        if to != "direct" and local_only(kind, value):
            raise RuleError(f"{path}.value", "local_only")
        if to != "proxy" and kind in {"domain", "suffix"} and value.lower() in hosts:
            raise RuleError(f"{path}.value", "reserved_host")
        if own_program(kind, value):
            raise RuleError(f"{path}.value", "own_program")
    out: dict[str, Any] = {"kind": kind, "value": value}
    if negated:
        out["not"] = True
    resolve = item.get("resolve", False)
    if not isinstance(resolve, bool) or (resolve and (kind not in IP_KINDS or not allow_resolve)):
        raise RuleError(f"{path}.resolve")
    if resolve:
        out["resolve"] = True
    return out


def normalize_rule_list(rows: Any, *, providers: Iterable[str] = (),
                        hosts: frozenset[str] | None = None) -> list[dict[str, Any]]:
    """The page's list, normalized; raises RuleError(path, reason) on the first bad row.

    ``providers``: the rule-set names the list may use. ``hosts``: the host
    names Tesseract routes itself (reserved_hosts()).
    """

    if not isinstance(rows, list):
        raise RuleError("routing.rule_list")
    if len(rows) > MAX_RULES:
        raise RuleError("routing.rule_list", "too_many")
    names = set(providers)
    hosts = reserved_hosts() if hosts is None else hosts
    out: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        path = f"routing.rule_list[{index}]"
        if not isinstance(row, Mapping):
            raise RuleError(path)
        if row.get("to") not in TARGETS:
            raise RuleError(f"{path}.to")
        off = row.get("off", False)
        if not isinstance(off, bool):
            raise RuleError(f"{path}.off")
        sets = None if off else names
        kind = row.get("kind")
        if kind == "clash":
            value = row.get("value")
            params = row.get("params", [])
            if (not isinstance(value, str) or not value.strip() or len(value) > 600 or "\n" in value
                    or not isinstance(params, list) or not all(isinstance(p, str) and "," not in p for p in params)):
                raise RuleError(f"{path}.value")
            normalized: dict[str, Any] = {"to": row["to"], "kind": "clash", "value": value.strip()}
            if params:
                normalized["params"] = [p.strip() for p in params]
            if not off and compile_rule(normalized) is None:
                raise RuleError(f"{path}.value")
        elif kind in LOGIC_KINDS:
            items = row.get("of")
            if not isinstance(items, list) or not 2 <= len(items) <= MAX_OF:
                raise RuleError(f"{path}.of")
            normalized = {"to": row["to"], "kind": kind,
                          "of": [_normal_condition(item, f"{path}.of[{n}]", sets, hosts, allow_resolve=True, to=row["to"])
                                 for n, item in enumerate(items)]}
        else:
            normalized = {"to": row["to"],
                          **_normal_condition(row, path, sets, hosts, allow_resolve=True, to=row["to"])}
        if off:
            note = row.get("note")
            normalized["off"] = True
            if note is not None:
                if note not in NOTES:
                    raise RuleError(f"{path}.note")
                normalized["note"] = note
        out.append(normalized)
    return out


# ---------------------------------------------------------------------------
# rule sets (routing.rule_providers), as the page lists them


def public_url(value: Any) -> bool:
    """An http(s) address with no credentials and no query: shown and edited on the page."""

    if not isinstance(value, str):
        return False
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return False
    return (parts.scheme in {"http", "https"} and bool(parts.hostname) and not parts.username
            and not parts.password and not parts.query and not parts.fragment)


def provider_format(url: str) -> str:
    path = urlsplit(url).path.lower()
    if path.endswith(".mrs"):
        return "mrs"
    if path.endswith((".txt", ".list", ".conf")):
        return "text"
    return "yaml"


def provider_rows(providers: Any) -> list[dict[str, Any]]:
    """``routing.rule_providers`` → the page's rows: name, type, behavior, format, the
    address when it is public (url) or only that one is stored (url_hidden)."""

    rows: list[dict[str, Any]] = []
    for name, block in (providers.items() if isinstance(providers, Mapping) else ()):
        block = block if isinstance(block, Mapping) else {}
        row: dict[str, Any] = {"name": str(name)}
        for key in ("type", "behavior", "format"):
            if isinstance(block.get(key), str):
                row[key] = block[key]
        url = block.get("url")
        if public_url(url):
            row["url"] = url.strip()
        elif url:
            row["url_hidden"] = True
        rows.append(row)
    return rows


def normalize_providers(items: Any, stored: Any) -> dict[str, dict[str, Any]]:
    """The page's rule-set rows → ``routing.rule_providers``; raises RuleError.

    A row without ``url`` keeps the stored provider of that name as it is
    (an address withheld from the page, or a provider that is not an http
    download); a new row needs a public http(s) address.
    """

    if not isinstance(items, list) or len(items) > 200:
        raise RuleError("routing.providers")
    stored = stored if isinstance(stored, Mapping) else {}
    out: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    for index, item in enumerate(items):
        path = f"routing.providers[{index}]"
        if not isinstance(item, Mapping):
            raise RuleError(path)
        name = item.get("name")
        if not isinstance(name, str) or not _PROVIDER_NAME.fullmatch(name) or name != name.strip():
            raise RuleError(f"{path}.name")
        if name.casefold() in seen:
            raise RuleError(f"{path}.name", "duplicate")
        seen.add(name.casefold())
        url = item.get("url")
        previous = stored.get(name)
        if url is None:
            if not isinstance(previous, Mapping):
                raise RuleError(f"{path}.url")
            block = dict(previous)
        else:
            if not public_url(url):
                raise RuleError(f"{path}.url", "url")
            url = url.strip()
            behavior = item.get("behavior")
            if behavior not in PROVIDER_BEHAVIORS:
                raise RuleError(f"{path}.behavior")
            fmt = item.get("format") or provider_format(url)
            if fmt not in PROVIDER_FORMATS or (fmt == "mrs" and behavior == "classical"):
                raise RuleError(f"{path}.format")
            block = dict(previous) if isinstance(previous, Mapping) and previous.get("type") == "http" else {}
            block.update({"type": "http", "url": url, "behavior": behavior, "format": fmt})
            block.setdefault("interval", PROVIDER_INTERVAL_SECONDS)
        out[name] = block
    return out
