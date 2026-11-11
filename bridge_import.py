"""import.preview / import.apply adapters for the v1 bridge (T-P2-15).

PROTOCOL §10/§12/§15: preview runs on the normal domain and returns a plan
bound to owner, tier, config rev and a 10 minute expiry; apply runs in the
mutation domain and re-checks the plan. Paste is accepted or rejected as one
batch through the existing import service (``expected_plan_id``); manual and
chain build URIs in Python with ``structured_node_uri``; subscriptions report
one RefreshResult per URL. Inputs may hold secrets: nothing from them reaches
logs, errors or job results, which carry only counts, opaque IDs and codes.

Runtime access goes through ``ImportRuntimePort``; the adapter only calls
public runtime methods. With a running Core a saved import is applied by one
strict Core reload (same rule as P2-14 nodes.delete); facts that cannot be
observed are reported as unknown instead of being guessed.

``items`` are opaque per-batch tokens (count / de-duplication / display), not
``Node.id`` / ``Subscription.id``: those are bound to the runtime generation
and the config index, so the page re-reads nodes.list / subscriptions.list.
``subscriptions[i]`` always answers ``urls[i]``.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Protocol


PREVIEW_COMMAND = "import.preview"
APPLY_COMMAND = "import.apply"

# Bounded inputs. The frame limit is 4 MiB; a paste above 1 MiB is refused
# before parsing so one request cannot monopolise a normal worker.
MAX_PREVIEW_TEXT_BYTES = 1024 * 1024
MAX_REJECTED = 200
PLAN_TTL_SECONDS = 600.0
MAX_PLANS_PER_OWNER = 4
MAX_PLANS = 64
MAX_PLAN_BYTES = 8 * 1024 * 1024  # Global bound on retained preview text.
MAX_SUBSCRIPTION_URLS = 32
MAX_URL_CHARS = 4096
MAX_FIELD_CHARS = 4096
MIN_CHAIN_PROTOCOLS = 2
MAX_CHAIN_PROTOCOLS = 16

PROTOCOL_LAYERS = {"vanguard": "tier1", "sentinel": "tier2", "bastion": "tier3", "gateway": "exit"}
_SUBSCRIPTION_LAYERS = frozenset({"tier2", "tier3", "exit"})
_CONFLICT_REASONS = frozenset({"duplicate_node", "duplicate_subscription", "import_plan_changed"})
_SAFE_SCHEME = re.compile(r"[a-z][a-z0-9+.-]{0,15}")
_INTEGER_TEXT = re.compile(r"[+-]?\d{1,12}")


# --- safe error objects ----------------------------------------------------

def _reason(value: Any, fallback: str) -> str:
    text = re.sub(r"[^a-z0-9_]+", "_", str(value or "").strip().lower()).strip("_")
    return text if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", text) else fallback


def import_error(code: str, reason: str, *, field: str | None = None,
                 params: Mapping[str, Any] | None = None, hold: bool | None = None) -> dict[str, Any]:
    """Error DTO with a stable message ID; never exception or input text."""
    reason = _reason(reason, "import_failed")
    error: dict[str, Any] = {"code": code, "message_key": f"errors.import.{reason}",
                             "params": dict(params or {}), "reason_code": reason}
    if field is not None:
        error["field"] = field
    if hold is not None:
        error["hold"] = bool(hold)
    return error


def _issue_error(issue: Any) -> dict[str, Any]:
    """Import-service issue -> Error. Messages, positions and fingerprints are dropped."""
    issue = issue if isinstance(issue, Mapping) else {}
    params: dict[str, Any] = {}
    scheme = str(issue.get("scheme") or "").strip().lower()
    if _SAFE_SCHEME.fullmatch(scheme):
        params["scheme"] = scheme
    return import_error("validation", _reason(issue.get("code"), "invalid_node"), params=params, hold=True)


def _empty_result() -> dict[str, Any]:
    return {"saved": False, "applied": False, "items": [], "subscriptions": [], "apply_error": None}


def _refresh_placeholder(*, error: dict[str, Any] | None = None) -> dict[str, Any]:
    """RefreshResult for a URL with nothing stored (yet)."""
    return {"total": 0, "healthy": 0, "stored": False, "applied": False,
            "retained_previous": False, "apply_error": None, "error": error}


class ImportRejected(Exception):
    """Business rejection carrying the complete safe partial result."""

    def __init__(self, code: str, result: dict[str, Any]):
        super().__init__(code)
        self.code = code
        self.result = result


def _reject(code: str, reason: str, result: dict[str, Any] | None = None, **error: Any) -> ImportRejected:
    result = dict(result or _empty_result())
    result["apply_error"] = import_error(code, reason, **error)
    return ImportRejected(code, result)


# --- physical node groups (T-P4-C2 #13, G38) --------------------------------

MAX_GROUP_LABEL_CHARS = 128
# Same UUID shape nodes.list names lose (bridge_nodes._UUID).
_LABEL_UUID = re.compile(r"\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b")


def _label_filter(nodes: list[Any]) -> Callable[[str], str]:
    """At least the nodes.list name filter, plus the parsed hosts.

    ``display_filter`` over the parsed nodes' own secrets (uuid, password,
    URI...) and the UUID substitution, as for ``Node.name``; and any of the
    parsed nodes' addresses in the text (the endpoint only ever leaves
    masked). A caller keeps a label only when this returns it unchanged.
    """
    try:
        from bridge_runtime_access import display_filter
        facts = [node.to_dict() for node in nodes]
        redact = display_filter({"nodes": facts})
        hosts = {str(row.get("address") or "").strip().lower() for row in facts} - {""}
    except Exception:
        return lambda _text: ""          # unfilterable: no label at all

    def clean(text: str) -> str:
        try:
            value = _LABEL_UUID.sub("[REDACTED]", redact(text))
        except Exception:
            return ""
        lowered = value.lower()
        return "" if any(host in lowered for host in hosts) else value
    return clean


def physical_groups(text: str, *, source: str, layer: str, accepted: int,
                    physical_count: Any = None) -> list[dict[str, Any]] | None:
    """``import.preview.groups``: the accepted nodes partitioned by physical node.

    Vanguard (``tier1``) only, where the import service itself merges the
    protocol rows of one server into one multi-protocol entry
    (``import_node_rules.tier1_import_entries_from_nodes``: same address,
    every row a URI with a distinct protocol key). That same function decides
    each group here, over the same parse (``parser.analyze_content_blob``,
    the call the service and import.apply make). ``row_indexes`` index the
    accepted nodes in recognition order (0 .. accepted-1); every node is in
    exactly one group, a single-protocol node in its own. ``key`` is the
    group's position (``g0``, ``g1``...), never derived from the address.
    ``label`` is the merged entry's name only when it comes from the node
    names (not the address fallback), the frame scanner accepts it and the
    nodes.list name filter leaves it unchanged (``_label_filter``).
    Any disagreement with the service's own counts (accepted, physical node
    count), another tier, or a parse failure -> None (field omitted).
    The parse runs a second time on this normal worker (<= 1 MiB input).
    """
    if layer != "tier1" or type(accepted) is not int or accepted <= 0:
        return None
    try:
        from bridge_protocol import contains_sensitive_text
        from import_node_rules import tier1_import_entries_from_nodes
        from parser import analyze_content_blob
        nodes = list(analyze_content_blob(text, source=source, tier=layer).accepted_nodes)
        if len(nodes) != accepted:
            return None
        order: list[str] = []
        buckets: dict[str, list[int]] = {}
        for index, node in enumerate(nodes):
            # = import_node_rules._tier1_physical_group_key
            key = str(node.address or "").strip().lower() or "|".join(node.dedupe_key)
            if key not in buckets:
                buckets[key] = []
                order.append(key)
            buckets[key].append(index)
        groups: list[dict[str, Any]] = []
        clean = None
        for key in order:
            rows = buckets[key]
            entries = tier1_import_entries_from_nodes([nodes[index] for index in rows])
            if len(rows) > 1 and len(entries) == 1:
                group: dict[str, Any] = {"key": f"g{len(groups)}", "row_indexes": list(rows)}
                name = str(entries[0].get("name") or "").strip()
                address = str(entries[0].get("address") or "").strip()
                if (name and name != address and len(name) <= MAX_GROUP_LABEL_CHARS
                        and not contains_sensitive_text(name)
                        and not any(ord(ch) < 32 for ch in name)):
                    if clean is None:
                        clean = _label_filter(nodes)
                    if clean(name) == name:
                        group["label"] = name
                groups.append(group)
            else:
                for index in rows:
                    groups.append({"key": f"g{len(groups)}", "row_indexes": [index]})
    except Exception:
        return None
    if type(physical_count) is int and physical_count != len(groups):
        return None
    return groups


# --- ports -------------------------------------------------------------------

class ImportRuntimePort(Protocol):
    def config_revision(self) -> int:
        """Bridge-controlled config rev (the config.snapshot rev)."""

    def preview_nodes(self, layer: str, text: str, source: str) -> Mapping[str, Any]:
        """Existing import_service.preview(); parse only, no config write."""

    def import_nodes(self, layer: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """Existing per-tier node import transaction (write + runtime reload)."""

    def import_chain(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """Existing Tier1 multi-protocol (fallback chain) import transaction."""

    def import_subscription(self, layer: str, url: str) -> Mapping[str, Any]:
        """Existing subscription import with its initial system refresh."""

    def layer_state(self, layer: str) -> str | None:
        """Read-only digest of the saved ``tiers.<layer>`` nodes/subscriptions.

        Compared before/after a failed transaction: the runtime entries write
        first and reload second (``_reload_after``), so a changed digest means
        "saved, reload failed". None means unknown. Never leaves the process.
        """

    def core_running(self) -> bool | None:
        """core_fact().running; None when unknown."""

    def reload_core_strict(self, *, user_commit_id: str) -> Mapping[str, Any]:
        """Public strict USER_GRAPH_CONFIG Core reload (P2-14 nodes.delete rule)."""


class SecretRegistryPort(Protocol):
    def register_draft(self, values: Mapping[str, str]) -> None:
        """Add draft secrets to the log redaction table before any write."""

    def restore(self) -> None:
        """Swap back to the table built from the (possibly new) saved config."""


class RuntimeImportAdapter:
    """Calls only public runtime entry points used by the Qt import workspace."""

    def __init__(self, runtime: Callable[[], Any]):
        self._runtime = runtime

    def config_revision(self) -> int:
        getter = getattr(self._runtime(), "bridge_config_revision", None)
        value = getter() if callable(getter) else 0
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

    def preview_nodes(self, layer: str, text: str, source: str) -> Mapping[str, Any]:
        return self._runtime().import_service.preview({"text": text, "tier": layer, "source": source})

    def import_nodes(self, layer: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        runtime = self._runtime()
        entry = {"tier1": runtime.import_tier1_nodes, "tier2": runtime.import_tier2_node,
                 "tier3": runtime.import_tier3_node, "exit": runtime.import_exit_nodes}[layer]
        return entry(dict(payload))

    def import_chain(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return self._runtime().import_tier1_multiprotocol(dict(payload))

    def import_subscription(self, layer: str, url: str) -> Mapping[str, Any]:
        runtime = self._runtime()
        entry = {"tier2": runtime.import_tier2_subscription, "tier3": runtime.import_tier3_subscription,
                 "exit": runtime.import_exit_subscription}[layer]
        return entry({"url": url})

    def layer_state(self, layer: str) -> str | None:
        try:
            local = self._runtime().read_local_config()
            tier = (local.get("tiers") or {}).get(layer) or {}
            body = {"nodes": tier.get("nodes"), "subscription_urls": tier.get("subscription_urls")}
            text = json.dumps(body, sort_keys=True, default=str, ensure_ascii=False)
        except Exception:
            return None
        return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()

    def core_running(self) -> bool | None:
        try:
            value = self._runtime().core_fact()["running"]
        except Exception:
            return None
        return value if isinstance(value, bool) else None

    def reload_core_strict(self, *, user_commit_id: str) -> Mapping[str, Any]:
        from runtime_reload_gate import RuntimeReloadReason
        return self._runtime().reload_core_config(
            mode="strict", reason=RuntimeReloadReason.USER_GRAPH_CONFIG,
            user_commit_id=user_commit_id)


def _strict_reload_confirmed(result: Any) -> bool:
    """Same rule as DesktopRuntime._strict_core_reload_confirmed (private)."""
    if not isinstance(result, Mapping) or result.get("kept_existing_core"):
        return False
    if result.get("reloaded"):
        return True
    return bool(result.get("running"))


class SessionSecretRegistry:
    """Draft secrets as leases of the session's layered registry (T-P2-31a).

    ``register_draft`` leases one layer (the redactor is refreshed before it
    returns; a failed refresh raises, so nothing is written); ``restore``
    releases this module's leases and re-reads the saved config through
    ``session.refresh_secrets()``. Other modules' leases are never touched.
    A session without a registry keeps the P1-16 table-replacement path.
    """

    def __init__(self, session: Any):
        self._session = session
        self._lock = threading.Lock()
        self._leases: list[Any] = []

    def register_draft(self, values: Mapping[str, str]) -> None:
        from bridge_secrets import session_registry
        session = self._session
        registry = session_registry(session)
        if registry is None:
            merged = dict(session.access.known_secret_values())
            merged.update(values)
            session.logs.refresh_known_secret_values(merged)
            return
        handle = registry.lease("import", values)
        if handle is not None:
            with self._lock:
                self._leases.append(handle)

    def restore(self) -> None:
        from bridge_secrets import session_registry
        registry = session_registry(self._session)
        with self._lock:
            leases, self._leases = self._leases, []
        if registry is not None:
            for handle in leases:
                registry.release(handle, refresh="none")
        self._session.refresh_secrets()


def draft_secret_values(*, uris: Iterable[str] = (), urls: Iterable[str] = (),
                        fields: Iterable[str] = ()) -> dict[str, str]:
    """Registry input for not-yet-saved drafts, via the config secret walker."""
    from bridge_runtime_access import _config_secret_values
    draft = {"tiers": {"draft": {"nodes": [{"uri": uri} for uri in uris if uri],
                                 "subscription_urls": [url for url in urls if url]}}}
    values = _config_secret_values(draft)
    for value in fields:
        if isinstance(value, str) and value:
            values.setdefault(value, "secret")
    return values


def executor_owner_resolver(executor: Any) -> Callable[[Any], str | None]:
    """JobContext carries no owner: the executor's public ``owner_of(op)``
    (T-P2-31a); executor internals are never read."""
    return lambda context: executor.owner_of(getattr(context, "op", None))


# --- plan store ----------------------------------------------------------------

@dataclass(frozen=True)
class ImportPlan:
    plan_id: str
    owner: str
    tier: str
    layer: str
    text: str
    service_plan_id: str
    importable: bool
    accepted: int
    config_rev: int
    deadline: float
    expires_at_ms: int
    size: int = 0


class PlanStore:
    """Bounded in-memory plans; never persisted, never logged, never published."""

    def __init__(self, *, ttl: float = PLAN_TTL_SECONDS, per_owner: int = MAX_PLANS_PER_OWNER,
                 capacity: int = MAX_PLANS, max_bytes: int = MAX_PLAN_BYTES,
                 monotonic: Callable[[], float] = time.monotonic,
                 wall_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000):
        self.ttl, self.per_owner, self.capacity, self.max_bytes = ttl, per_owner, capacity, max_bytes
        self._monotonic, self._wall_ms = monotonic, wall_ms
        self._lock = threading.Lock()
        self._plans: OrderedDict[str, ImportPlan] = OrderedDict()

    def _expire_locked(self, now: float) -> None:
        for plan_id in [key for key, plan in self._plans.items() if plan.deadline <= now]:
            del self._plans[plan_id]

    def create(self, *, owner: str, tier: str, layer: str, text: str, service_plan_id: str,
               importable: bool, accepted: int, config_rev: int) -> ImportPlan:
        now = self._monotonic()
        plan = ImportPlan(secrets.token_urlsafe(18), owner, tier, layer, text, service_plan_id,
                          importable, accepted, config_rev, now + self.ttl,
                          self._wall_ms() + int(self.ttl * 1000),
                          len(text.encode("utf-8", "surrogatepass")))
        with self._lock:
            self._expire_locked(now)
            owned = [key for key, item in self._plans.items() if item.owner == owner]
            for key in owned[:max(0, len(owned) - self.per_owner + 1)]:
                del self._plans[key]
            while len(self._plans) >= self.capacity:
                self._plans.popitem(last=False)
            total = sum(item.size for item in self._plans.values())
            while self._plans and total + plan.size > self.max_bytes:
                total -= self._plans.popitem(last=False)[1].size
            self._plans[plan.plan_id] = plan
        return plan

    def discard(self, plan_id: str) -> None:
        with self._lock:
            self._plans.pop(plan_id, None)

    def take(self, plan_id: str, *, owner: str, tier: str, config_rev: int) -> tuple[ImportPlan | None, str]:
        """Single use: a plan presented by its owner is consumed whatever the outcome."""
        now = self._monotonic()
        with self._lock:
            plan = self._plans.get(plan_id)
            if plan is None:
                return None, "plan_missing"
            if plan.owner != owner:
                return None, "plan_owner_mismatch"  # Another owner cannot burn this plan.
            del self._plans[plan_id]
        if plan.deadline <= now:
            return None, "plan_expired"
        if plan.tier != tier:
            return None, "plan_tier_mismatch"
        if plan.config_rev != config_rev:
            return None, "plan_config_changed"
        return plan, ""

    def revoke_owner(self, owner: str) -> None:
        with self._lock:
            for key in [key for key, plan in self._plans.items() if plan.owner == owner]:
                del self._plans[key]

    def __len__(self) -> int:
        with self._lock:
            return len(self._plans)


# --- structured (manual / chain) inputs ---------------------------------------

_FORM_CACHE: list[Any] = []


@dataclass(frozen=True)
class _FieldRule:
    """The published FieldSpec subset that apply enforces (PROTOCOL §16)."""
    kind: str
    choices: tuple[str, ...]
    minimum: int | None
    maximum: int | None


def _protocol_forms() -> dict[str, dict[str, _FieldRule]]:
    """Field ID -> rule per protocol, from the published import.specs catalog."""
    from bridge_catalog import CatalogCache, build_import_specs
    if not _FORM_CACHE:
        _FORM_CACHE.append(CatalogCache())
    specs = _FORM_CACHE[0].get("import.specs", build_import_specs)
    return {spec["id"]: {field["id"]: _FieldRule(field["type"], tuple(field["choices"]),
                                                 field["conditions"].get("min"),
                                                 field["conditions"].get("max"))
                         for field in spec["fields"] if field["id"] != "tier"}
            for spec in specs["protocols"]}


def _field_value(value: Any, rule: _FieldRule) -> str | None:
    """Catalog-checked text for the URI builder; None means field_invalid.

    An empty value is "not filled" and is left to the required check.
    """
    if rule.kind == "integer":
        if isinstance(value, bool):
            return None
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return ""
            if not _INTEGER_TEXT.fullmatch(text):
                return None
            value = int(text)
        if not isinstance(value, int):
            return None
        if (rule.minimum is not None and value < rule.minimum) or (rule.maximum is not None and value > rule.maximum):
            return None
        return str(value)
    if rule.kind == "boolean":
        if isinstance(value, bool):
            return "true" if value else "false"
        return value if value in ("true", "false") else None
    if not isinstance(value, str) or len(value) > MAX_FIELD_CHARS:
        return None
    text = value.strip()
    if rule.kind == "choice" and text and text not in rule.choices:
        return None
    return text


@dataclass(frozen=True)
class _Draft:
    protocol: str
    values: dict[str, str]


def _parse_draft(item: Any, *, allowed: Iterable[str], index: int | None = None) -> _Draft:
    where = "manual" if index is None else f"chain.{index}"
    if not isinstance(item, Mapping) or set(item) - {"protocol", "fields"}:
        raise _reject("validation", "draft_invalid", field=where)
    protocol = item.get("protocol")
    forms = _protocol_forms()
    if not isinstance(protocol, str) or protocol not in forms or protocol not in set(allowed):
        raise _reject("validation", "protocol_not_allowed", field=f"{where}.protocol")
    raw_fields = item.get("fields", {})
    if not isinstance(raw_fields, Mapping):
        raise _reject("validation", "draft_invalid", field=f"{where}.fields")
    values: dict[str, str] = {}
    for key, raw in raw_fields.items():
        if not isinstance(key, str) or key not in forms[protocol]:
            raise _reject("validation", "field_unknown", field=f"{where}.fields")
        text = _field_value(raw, forms[protocol][key])
        if text is None:
            raise _reject("validation", "field_invalid", field=key)
        values[key] = text
    return _Draft(protocol, values)


def _reader(draft: _Draft, shared: Mapping[str, str] | None = None) -> Callable[..., str]:
    """Field reader that ignores builder defaults: no placeholder ever becomes a value."""
    def read(key: str, _default: str = "") -> str:
        value = draft.values.get(key, "")
        if not value and shared and key in {"name", "server"}:
            value = shared.get(key, "")
        return value
    return read


def _validate_draft(draft: _Draft, read: Callable[..., str]) -> None:
    from shadowsocks_capabilities import DEFAULT_SHADOWSOCKS_CIPHER, validate_shadowsocks_credential
    from structured_import_validation import first_invalid_port_value, structured_required_fields
    field_value = lambda _protocol, key: read(key)  # noqa: E731
    for key in sorted(structured_required_fields([draft.protocol], field_value=field_value)):
        if not read(key):
            raise _reject("validation", "field_required", field=key)
    if first_invalid_port_value([("port", read("port"))]) is not None:
        raise _reject("validation", "port_invalid", field="port")
    if draft.protocol == "ss":
        issue = validate_shadowsocks_credential(read("method") or DEFAULT_SHADOWSOCKS_CIPHER, read("password"))
        if issue is not None:
            field = "method" if issue.code == "unsupported_cipher" else "password"
            raise _reject("validation", _reason(issue.code, "ss_credential_invalid"), field=field)


def _secret_field_values(draft: _Draft) -> list[str]:
    form = _protocol_forms()[draft.protocol]
    return [value for key, value in draft.values.items()
            if key in form and form[key].kind == "secret" and value]


# --- command handlers -------------------------------------------------------------

class _RegistryFailure(Exception):
    """Draft secrets could not be registered: nothing was written."""


class ImportCommands:
    def __init__(self, port: ImportRuntimePort, *, owner_of: Callable[[Any], str | None],
                 secrets_registry: SecretRegistryPort | None = None, plans: PlanStore | None = None):
        self.port = port
        self.owner_of = owner_of
        self.secrets = secrets_registry
        self.plans = plans if plans is not None else PlanStore()

    # import.preview (normal domain, res) --------------------------------------
    def preview(self, args: Mapping[str, Any], context: Any) -> dict[str, Any]:
        from bridge_jobs import JobFailure
        from import_mode_view import node_import_apply_payload
        owner = self.owner_of(context)
        if not isinstance(owner, str) or not owner:
            raise JobFailure("not_allowed")
        tier, text = args["tier"], args["text"]
        if len(text.encode("utf-8", "surrogatepass")) > MAX_PREVIEW_TEXT_BYTES:
            raise JobFailure("validation")
        layer = PROTOCOL_LAYERS[tier]
        source = node_import_apply_payload(f"{layer}_nodes", "")["source"]
        config_rev = self.port.config_revision()  # Read before parsing; apply compares.
        try:
            raw = self.port.preview_nodes(layer, text, source)
        except ValueError as exc:
            raise JobFailure("validation") from exc
        issues = list(raw.get("blocking_errors") or ())
        rejected = [_issue_error(issue) for issue in issues[:MAX_REJECTED]]
        if len(issues) > MAX_REJECTED:
            rejected.append(import_error("validation", "rejected_truncated",
                                         params={"omitted_count": len(issues) - MAX_REJECTED}, hold=True))
        accepted = int(raw.get("node_count") or 0)
        revoked = getattr(context, "owner_revoked", None)
        if revoked is not None and revoked.is_set():
            raise JobFailure("not_allowed")
        plan = self.plans.create(owner=owner, tier=tier, layer=layer, text=text,
                                 service_plan_id=str(raw.get("plan_id") or ""),
                                 importable=bool(raw.get("can_import")) and not issues,
                                 accepted=accepted, config_rev=config_rev)
        if revoked is not None and revoked.is_set():
            # The owner revoke hook may have run before create: no orphan plan.
            self.plans.discard(plan.plan_id)
            raise JobFailure("not_allowed")
        reply = {"plan_id": plan.plan_id, "expires_at_ms": plan.expires_at_ms,
                 "accepted": accepted, "rejected": rejected}
        groups = physical_groups(text, source=source, layer=layer, accepted=accepted,
                                 physical_count=raw.get("physical_node_count"))
        if groups is not None:
            reply["groups"] = groups
        return reply

    # import.apply (mutation domain, job) --------------------------------------
    def apply(self, args: Mapping[str, Any], context: Any) -> dict[str, Any]:
        from bridge_jobs import JobFailure
        owner = self.owner_of(context)
        if not isinstance(owner, str) or not owner:
            raise JobFailure("not_allowed")
        try:
            handler = {"paste": self._paste, "manual": self._manual, "chain": self._chain,
                       "subscriptions": self._subscriptions}[args["mode"]]
            return handler(args, owner)
        except ImportRejected as exc:
            raise JobFailure(exc.code, exc.result) from None

    def _guarded(self, values: Mapping[str, str], action: Callable[[], Any]) -> Any:
        """Register draft secrets before any runtime call that may log
        (P2-14 ``_secret_boundary``): a failed registration aborts before any
        write; a failed restore is swallowed, the union table being a safe
        superset, and never changes the business outcome."""
        if self.secrets is not None:
            try:
                self.secrets.register_draft(values)
            except Exception:
                raise _RegistryFailure() from None
        try:
            return action()
        finally:
            if self.secrets is not None:
                try:
                    self.secrets.restore()
                except Exception:
                    pass

    def _layer_state(self, layer: str) -> str | None:
        try:
            value = self.port.layer_state(layer)
        except Exception:
            return None
        return value if isinstance(value, str) else None

    def _failure_outcome(self, layer: str, before: str | None, exc: BaseException) -> tuple[bool, dict[str, Any]]:
        """(saved, error) for a runtime entry that raised a non-diagnostic error."""
        after = self._layer_state(layer)
        if before is None or after is None:
            return False, import_error("io", "save_outcome_unknown")
        if after != before:
            return True, import_error("io", "runtime_reload_failed")
        if isinstance(exc, (ValueError, KeyError)):
            return False, import_error("validation", "import_rejected")
        return False, import_error("io", "import_failed")

    def _saved_nodes(self, layer: str, call: Callable[[], Mapping[str, Any]],
                     items: list[str], secret_values: Mapping[str, str]) -> dict[str, Any]:
        from import_service import ImportDiagnosticError
        result = _empty_result()
        before = self._layer_state(layer)
        try:
            self._guarded(secret_values, call)
        except _RegistryFailure:
            raise ImportRejected("internal", result) from None
        except ImportDiagnosticError as exc:
            reason = _reason(exc.diagnostic.get("code"), "import_rejected")
            raise _reject("conflict" if reason in _CONFLICT_REASONS else "validation", reason, result) from None
        except Exception as exc:
            saved, error = self._failure_outcome(layer, before, exc)
            if saved:
                result.update(saved=True, items=items)
            result["apply_error"] = error
            raise ImportRejected(error["code"], result) from None
        result.update(saved=True, items=items)
        return self._finish_apply(layer, result)

    def _core_apply(self, layer: str) -> tuple[bool, dict[str, Any] | None]:
        """Apply a saved import to the Core (P2-14 nodes.delete precedent)."""
        running = self.port.core_running()
        if running is None:
            return False, import_error("io", "apply_unconfirmed")
        if not running:
            return True, None  # Runtime config reloaded; the next start uses it.
        try:
            receipt = self.port.reload_core_strict(user_commit_id=f"import-apply:{layer}:{time.time_ns()}")
        except Exception:
            return False, import_error("io", "core_reload_failed")
        if _strict_reload_confirmed(receipt):
            return True, None
        return False, import_error("io", "reload_unconfirmed")

    def _finish_apply(self, layer: str, result: dict[str, Any]) -> dict[str, Any]:
        applied, error = self._core_apply(layer)
        result["applied"] = applied
        if error is not None:
            result["apply_error"] = error
            raise ImportRejected(error["code"], result)
        return result

    def _paste(self, args: Mapping[str, Any], owner: str) -> dict[str, Any]:
        from bridge_runtime_access import opaque_id
        from import_mode_view import node_import_apply_payload
        from parser import analyze_content_blob
        tier = args["tier"]
        plan, reason = self.plans.take(args["plan_id"], owner=owner, tier=tier,
                                       config_rev=self.port.config_revision())
        if plan is None:
            raise _reject("conflict", reason)
        if not plan.importable:
            raise _reject("validation", "plan_rejected")
        layer = plan.layer
        uris = [node.uri for node in analyze_content_blob(plan.text, tier=layer).accepted_nodes if node.uri]
        payload = node_import_apply_payload(f"{layer}_nodes", plan.text, plan.service_plan_id)
        return self._saved_nodes(layer, lambda: self.port.import_nodes(layer, payload),
                                 [opaque_id("import", layer, uri) for uri in uris],
                                 draft_secret_values(uris=uris))

    def _manual(self, args: Mapping[str, Any], owner: str) -> dict[str, Any]:
        from bridge_runtime_access import opaque_id
        from import_mode_view import manual_protocol_options
        from structured_node_uri import build_structured_node_uri
        layer = PROTOCOL_LAYERS[args["tier"]]
        allowed = [value for _label, value in manual_protocol_options(layer, "single")]
        draft = _parse_draft(args["manual"], allowed=allowed)
        read = _reader(draft)
        _validate_draft(draft, read)
        if draft.protocol == "argo_xhttp":
            # Qt routes a single Argo node through the Tier1 multi-protocol block.
            return self._import_block(layer, [draft], args)
        uri = build_structured_node_uri(draft.protocol, read)
        payload = {"text": uri, "source": f"native-app-{layer}-structured-import"}
        return self._saved_nodes(layer, lambda: self.port.import_nodes(layer, payload),
                                 [opaque_id("import", layer, uri)],
                                 draft_secret_values(uris=[uri], fields=_secret_field_values(draft)))

    def _chain(self, args: Mapping[str, Any], owner: str) -> dict[str, Any]:
        from import_mode_view import manual_protocol_options
        layer = PROTOCOL_LAYERS[args["tier"]]
        chain = args["chain"]
        if layer != "tier1":
            raise _reject("validation", "chain_tier_not_allowed", field="tier")
        # Qt: a multi-protocol node needs at least two protocols; a single
        # protocol (Argo included) goes through mode="manual".
        if not MIN_CHAIN_PROTOCOLS <= len(chain) <= MAX_CHAIN_PROTOCOLS:
            raise _reject("validation", "chain_length_invalid", field="chain")
        allowed = [value for _label, value in manual_protocol_options(layer, "multi")]
        drafts = [_parse_draft(item, allowed=allowed, index=index) for index, item in enumerate(chain)]
        if len({draft.protocol for draft in drafts}) != len(drafts):
            raise _reject("validation", "chain_duplicate_protocol", field="chain")
        return self._import_block(layer, drafts, args)

    def _import_block(self, layer: str, drafts: list[_Draft], args: Mapping[str, Any]) -> dict[str, Any]:
        from bridge_runtime_access import opaque_id
        from import_mode_view import structured_multiprotocol_block
        from structured_node_uri import build_structured_node_uri
        shared = {key: drafts[0].values.get(key, "") for key in ("name", "server")}
        readers = {draft.protocol: _reader(draft, shared) for draft in drafts}
        for draft in drafts:
            _validate_draft(draft, readers[draft.protocol])
        protocols = [draft.protocol for draft in drafts]
        uris = [build_structured_node_uri(protocol, readers[protocol])
                for protocol in protocols if protocol != "argo_xhttp"]
        text = structured_multiprotocol_block(
            protocols, fallback_order=protocols,
            uri_factory=lambda protocol: build_structured_node_uri(protocol, readers[protocol]),
            field_value=lambda protocol, key, _default="": readers[protocol](key))
        payload = {"name": shared["name"], "text": text, "fallback_order": " -> ".join(protocols)}
        fields = [value for draft in drafts for value in _secret_field_values(draft)]
        return self._saved_nodes(layer, lambda: self.port.import_chain(payload),
                                 [opaque_id("import", layer, "chain", *protocols, text)],
                                 draft_secret_values(uris=uris, fields=fields))

    def _subscriptions(self, args: Mapping[str, Any], owner: str) -> dict[str, Any]:
        from bridge_runtime_access import opaque_id
        from import_service import ImportDiagnosticError
        layer = PROTOCOL_LAYERS[args["tier"]]
        if layer not in _SUBSCRIPTION_LAYERS:
            raise _reject("validation", "subscription_tier_not_allowed", field="tier")
        urls = [url.strip() for url in args["urls"]]
        if (len(urls) > MAX_SUBSCRIPTION_URLS or any(not url or len(url) > MAX_URL_CHARS for url in urls)
                or len(set(urls)) != len(urls)):
            raise _reject("validation", "subscription_urls_invalid", field="urls")
        result = _empty_result()
        for index, url in enumerate(urls):
            item = _refresh_placeholder()
            result["subscriptions"].append(item)  # subscriptions[i] <-> urls[i]
            before = self._layer_state(layer)
            try:
                raw = self._guarded(draft_secret_values(urls=[url]),
                                    lambda url=url: self.port.import_subscription(layer, url))
            except _RegistryFailure:
                # Nothing was written for this URL and the rest are not
                # attempted. Placeholders keep subscriptions[i] <-> urls[i];
                # subscriptions already stored still get the Core apply below.
                item["error"] = import_error("internal", "secret_registry_failed")
                result["subscriptions"].extend(
                    _refresh_placeholder(error=import_error("internal", "batch_aborted"))
                    for _url in urls[index + 1:])
                break
            except ImportDiagnosticError as exc:
                reason = _reason(exc.diagnostic.get("code"), "import_rejected")
                item["error"] = import_error("conflict" if reason in _CONFLICT_REASONS else "validation", reason)
                continue
            except Exception as exc:
                saved, item["error"] = self._failure_outcome(layer, before, exc)
                if saved:
                    result["saved"] = True
                    result["items"].append(opaque_id(layer, url))
                continue
            result["saved"] = True
            result["items"].append(opaque_id(layer, url))  # == subscription record source_id
            refresh = raw.get("refresh") if isinstance(raw, Mapping) else None
            refresh = refresh if isinstance(refresh, Mapping) else {}
            for key in ("total", "healthy"):
                value = refresh.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    item[key] = value
            failed = isinstance(raw, Mapping) and bool(raw.get("refresh_error"))
            if refresh.get("stored") is True and not failed:
                item["stored"] = True
            else:
                # Saved, but the first refresh failed: the job fails and
                # keeps this per-item fact (PROTOCOL §15).
                item["error"] = import_error("io", "initial_refresh_failed")
        stored = [item for item in result["subscriptions"] if item["stored"]]
        if stored:
            # One Core apply covers every stored subscription of this batch.
            applied, error = self._core_apply(layer)
            for item in stored:
                item["applied"], item["apply_error"] = applied, error
            result["apply_error"] = error
        codes = [item["error"]["code"] if item["error"] is not None else item["apply_error"]["code"]
                 for item in result["subscriptions"]
                 if item["error"] is not None or item["apply_error"] is not None]
        result["applied"] = result["saved"] and not codes
        if codes:
            code = ("internal" if "internal" in codes else "io" if "io" in codes
                    else "conflict" if "conflict" in codes else "validation")
            raise ImportRejected(code, result)
        return result


def register(session: Any, *, port: ImportRuntimePort | None = None,
             owner_of: Callable[[Any], str | None] | None = None,
             secrets_registry: SecretRegistryPort | None = None) -> ImportCommands:
    """Integration hook (not wired by this ticket): commands + owner revoke hook."""
    commands = ImportCommands(
        port if port is not None else RuntimeImportAdapter(lambda: session.access.runtime),
        owner_of=owner_of if owner_of is not None else executor_owner_resolver(session.executor),
        secrets_registry=secrets_registry if secrets_registry is not None else SessionSecretRegistry(session))
    session.executor.register(PREVIEW_COMMAND, commands.preview)
    session.executor.register(APPLY_COMMAND, commands.apply)
    hook = getattr(session, "register_owner_revoke_hook", None)
    if callable(hook):
        hook(commands.plans.revoke_owner)
    return commands
