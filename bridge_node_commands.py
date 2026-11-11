"""P2-14 node / subscription / Gateway / network-mode mutation commands.

Six PROTOCOL section-10 commands, adapted per section 15 and the section-12
nodes.reveal exception. Handlers run on executor workers only, never on the
pipe reader or a publication tick. Runtime access goes through the explicit
``NodeCommandPort``; ``RuntimeNodeCommandAdapter`` only calls public
DesktopRuntime / existing-module surfaces. Facts the runtime cannot yet
provide are port getters that the adapter answers with None (unknown).

Item IDs reuse the P2-04 opaque rules (``bridge_runtime_access.opaque_id``):
loaded nodes are bound to the runtime generation, subscriptions to their
configured tier/index/URL. Unknown or stale IDs are ``conflict``.

No raw exception text, URL, URI or credential reaches a wire result: errors
are registered codes plus a short ``reason_code``. Integration wires the
commands with ``register(session)``; bridge_main.py is not edited here.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager, nullcontext
import copy
from dataclasses import dataclass
import logging
import re
import threading
import time
from typing import Any, Callable, Iterator, Protocol

from bridge_commands import COMMAND_SPECS
from bridge_jobs import JobFailure
from bridge_runtime_access import immutable_record, opaque_id
from bridge_vocab import RUNTIME_MODES, RUNTIME_TIERS


COMMANDS = ("mode.set", "nodes.update", "nodes.delete", "nodes.reveal",
            "subscriptions.refresh", "gateway.select")
REVEAL_FIELDS = ("password", "uuid", "private_key", "uri", "subscription_url")
_OPAQUE_ID = re.compile(r"[0-9a-f]{24}\Z")
# Same UUID mask bridge_nodes applies to displayed node names (T-P2-H5).
_UUID = re.compile(r"\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b")
_SUPERSEDED_TEXT = "superseded"
_EMPTY_SUBSCRIPTION_TEXT = "returned no nodes"

# Plain (non-secret) node.update fields per config item shape. Everything
# else is rejected; credentials only arrive through SecretEdit (section 12).
# ``enabled`` (T-P2-31b): a disabled node is no longer loaded and has no
# P2-04 Node.id, but its stable ConfigItem ID (config.items topic,
# bridge_runtime_access.config_item_id) still addresses it, so the page can
# re-enable it.
_PLAIN_UPDATES = {
    ("tier1", "node"): frozenset({"name", "address", "port", "fallback_order", "enabled"}),
    ("imported", "node"): frozenset({"name", "enabled"}),
    ("imported", "subscription"): frozenset({"enabled"}),
}
# SecretEdit field ID -> config_store update key, and whether clear is legal.
_SECRET_UPDATES = {
    ("tier1", "node"): {"vless_link": ("vless_link", True),
                        "hysteria2_link": ("hysteria2_link", True),
                        "naive_link": ("naive_link", True)},
    ("imported", "node"): {"uri": ("uri", False)},
    ("imported", "subscription"): {"subscription_url": ("url", False)},
}


class StaleItemFacts(RuntimeError):
    """Runtime owner changed while item facts were read; resolve again."""


class RefreshLockBusy(RuntimeError):
    """The runtime refresh/lifecycle lock could not be acquired in time.

    ``NodeCommandPort.refresh_subscription`` raises it when the runtime's
    bounded acquire (``acquire_timeout``, T-P2-RT2) times out; the handler
    reports conflict with reason_code ``lifecycle_busy`` instead of silently
    waiting out a stop.
    """


# Bound on the runtime refresh/lifecycle lock wait for subscriptions.refresh
# (seconds). A refresh or lifecycle transition holding it longer is busy.
REFRESH_ACQUIRE_TIMEOUT = 1.0


# Fact families -> the commands whose closed business-code sets apply.
_DOMAIN_COMMANDS = {"mode": ("mode.set",), "nodes": ("nodes.update", "nodes.delete"),
                    "subscriptions": ("subscriptions.refresh",),
                    "gateway": ("gateway.select",)}


# ---------------------------------------------------------------------------
# P2-04 identity rules (pure)
# ---------------------------------------------------------------------------

def node_identity(node: Any) -> tuple:
    """Exactly the tuple DesktopRuntime.bridge_node_records hashes."""
    return (node.protocol, node.address, node.port, node.uuid, node.password, node.method,
            node.security, node.network, node.host, node.path, node.sni, node.plugin)


def node_item_id(runtime_generation: int, tier: str, node: Any) -> str:
    """P2-04 Node.id: opaque, bound to the runtime generation."""
    return identity_item_id(runtime_generation, tier, node_identity(node))


def identity_item_id(runtime_generation: int, tier: str, identity: tuple) -> str:
    """P2-04 Node.id from an already projected identity tuple."""
    return opaque_id(runtime_generation, tier, identity)


def subscription_item_id(tier: str, index: int, url: str) -> str:
    """P2-04 Subscription.id: opaque_id(runtime tier, config index, URL)."""
    return opaque_id(tier, index, url)


# ---------------------------------------------------------------------------
# Port and adapter
# ---------------------------------------------------------------------------

class NodeCommandPort(Protocol):
    """Runtime surface used by the six commands; every method may block."""

    def core_fact(self) -> Mapping[str, Any]:
        """{running, pid, process_generation, cleanup_pending, core_generation}."""

    def lifecycle_status(self) -> Mapping[str, Any]:
        """RT-3 five-domain sample (intent/runtime/core/readiness/freshness)."""

    def network_mode(self) -> str:
        """Loaded runtime network mode (system_proxy|tun|mixed)."""

    def persisted_network_mode(self) -> str | None:
        """client.network_mode in config.local.yaml; None when absent."""

    def set_network_mode(self, mode: str) -> Mapping[str, Any]:
        """Existing switch: persist, reload, restart Core when it was running."""

    def core_loaded_network_mode(self) -> str | None:
        """NEW runtime getter: the network mode the *running* Core process was
        started with (system_proxy|tun|mixed); None when unknown or no Core.

        Needed because the loaded config changes before the restart, and an
        unclean stop leaves the old Core running under the old mode."""

    def loaded_node_index(self) -> tuple[int, tuple[tuple[str, tuple, Any], ...]]:
        """(runtime_generation, ((runtime tier, P2-04 identity tuple, handle), ...)).

        Only the identity projection happens under manager.lock; ``handle`` is
        an opaque reference that is only passed back to ``detach_node``.
        Raise StaleItemFacts when the runtime generation changed during the read.
        """

    def detach_node(self, generation: int, tier: str, handle: Any) -> Any:
        """Deep copy of exactly one indexed node, taken under manager.lock.

        Raise StaleItemFacts when the generation moved or the node left the tier.
        """

    def subscription_entries(self) -> tuple[tuple[str, int, str], ...]:
        """((runtime tier, config index, URL), ...) from the loaded config."""

    def configured_items(self) -> list[dict[str, Any]]:
        """config_store configured items (tier/kind/key/... rows)."""

    def update_config_item(self, item: dict[str, Any], updates: dict[str, Any]) -> Mapping[str, Any]:
        """Existing node/subscription transaction (persist, reload, strict Core reload, rollback)."""

    def delete_config_item(self, item: dict[str, Any]) -> Mapping[str, Any]:
        """Existing delete: persist and reload runtime config (no Core reload)."""

    def reload_core_config(self, *, reason: Any, user_commit_id: str) -> Mapping[str, Any]:
        """Strict Core reload; the caller declares the reload reason."""

    def refresh_subscription(self, item: dict[str, Any]) -> Mapping[str, Any]:
        """Existing system refresh; returns total/healthy/stored/applied/... facts.

        Raises RefreshLockBusy when the runtime refresh lock is not acquired
        within REFRESH_ACQUIRE_TIMEOUT (runtime ``acquire_timeout``, T-P2-RT2);
        nothing ran in that case."""

    def set_exit_selected_proxy(self, proxy_name: str) -> Mapping[str, Any]:
        """Existing Gateway selection: persist, strict reload, Mihomo PUT.

        The runtime saves the node's identity with the name when the name
        provably denotes one Gateway node; its answer's ``member`` is the
        loaded EXIT-SELECT member it selected."""

    def set_exit_selection(self, proxy_name: str, identity: str) -> Mapping[str, Any]:
        """Gateway selection by node identity (label ``proxy_name``): persist,
        strict reload, then the PUT of the loaded member chosen for that
        identity (``member`` in the answer; apply_error
        ``gateway_member_unavailable`` when the node is not exported)."""

    def persisted_exit_selection_key(self) -> str | None:
        """The saved selection's key ("id:<identity>", "name:<label>", ...;
        "" = none); None when the file cannot be read."""

    def display_proxy_name(self, raw: str) -> str | None:
        """A Mihomo proxy name through gateway.current's display filter."""

    def exit_selection_readback(self) -> str | None:
        """NEW runtime getter: EXIT-SELECT's current member read back from
        Mihomo without side effects; None when unknown (no readback yet)."""

    def persisted_exit_selection(self) -> str | None:
        """tiers.exit selection saved in config.local.yaml ("" = none);
        None when the file cannot be read. Used when no Core runs."""

    def exit_proxy_name(self, node_id: str) -> Mapping[str, Any] | None:
        """T-P2-H5 runtime getter ``bridge_exit_proxy_name(node_id)``: the
        Mihomo proxy name of a Gateway Node.id, named exactly as the renderer
        names it (clash_formatter order, seed and ``-2`` de-duplication).

        {"status": "ok"|"unknown"|"not_gateway"|"unusable"|"stale",
         "proxy_name": str|None (raw, runtime only),
         "display_name": str|None (display-filtered, for the wire),
         "identity": str|None (the node's Gateway identity, runtime only)}.
        None means the runtime has no such getter (unknown)."""

    def subscription_refresh_state(self) -> Mapping[str, Any] | None:
        """NEW runtime getter (P2-06 refresh owner); None means unknown.

        {"generation": runtime_generation, "stale": bool, "items": [
            {"id": Subscription.id, "refreshing": bool, "job_generation": uint,
             "membership_version": uint, "member_ids": [Node.id],
             "check_receipt": {runtime_generation, core_generation, core_pid,
                               membership_version, member_ids,
                               refresh_job_generation, checked_at_ms} | None}]}
        Must cover every configured subscription, including disabled ones.
        """

    def subscription_refresh_busy(self) -> bool | None:
        """NEW runtime getter: a refresh/lifecycle owner currently holds the
        refresh lock. None means "not checked" (the adapter cannot
        try-acquire), never "idle"; the bounded acquire behind
        RefreshLockBusy is the race-free form."""


class RuntimeNodeCommandAdapter:
    """Public DesktopRuntime surfaces only; no private runtime members."""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    def core_fact(self) -> Mapping[str, Any]:
        return self.runtime.core_fact()

    def lifecycle_status(self) -> Mapping[str, Any]:
        return self.runtime.lifecycle_status()

    def network_mode(self) -> str:
        return str(self.runtime.network_mode)

    def persisted_network_mode(self) -> str | None:
        local = self.runtime.read_local_config()
        client = local.get("client") if isinstance(local, Mapping) else None
        mode = client.get("network_mode") if isinstance(client, Mapping) else None
        return str(mode).strip() if mode else None

    def set_network_mode(self, mode: str) -> Mapping[str, Any]:
        return self.runtime.set_network_mode(mode)

    def _runtime_generation(self) -> int:
        return int(self.runtime.lifecycle_status()["intent"]["runtime_generation"])

    def _optional_getter(self, name: str) -> Any:
        # Forward-compatible: a later runtime ticket adds the public getter.
        getter = getattr(type(self.runtime), name, None)
        return getter(self.runtime) if callable(getter) else None

    def core_loaded_network_mode(self) -> str | None:
        value = self._optional_getter("bridge_core_network_mode")
        return value if value in RUNTIME_MODES.values() else None

    def loaded_node_index(self) -> tuple[int, tuple[tuple[str, tuple, Any], ...]]:
        before = self._runtime_generation()
        manager = self.runtime.manager
        rows: list[tuple[str, tuple, Any]] = []
        if manager is not None:
            # Same lock discipline as bridge_node_records: tuples only.
            with manager.lock:
                if getattr(manager, "runtime_generation", before) != before:
                    raise StaleItemFacts("manager generation changed")
                for tier, values in manager.runtime_nodes.items():
                    rows.extend((str(tier), node_identity(node), node) for node in values)
        else:
            # Service stopped: nodes.list lists the runtime's configured inventory
            # (bridge_node_records), so its ids resolve against that same inventory.
            inventory = self._optional_getter("bridge_stopped_node_inventory") or {}
            for tier, values in inventory.items():
                rows.extend((str(tier), node_identity(node), node) for node in values)
        if self._runtime_generation() != before or self.runtime.manager is not manager:
            raise StaleItemFacts("runtime generation changed")
        return before, tuple(rows)

    def detach_node(self, generation: int, tier: str, handle: Any) -> Any:
        manager = self.runtime.manager
        if manager is None:
            # Stopped inventory: the handle is this read's own fresh copy (no Manager
            # owns it); a start in between invalidates it like any generation change.
            if self._runtime_generation() != generation:
                raise StaleItemFacts("runtime generation changed")
            detached = copy.deepcopy(handle)
            if self._runtime_generation() != generation or self.runtime.manager is not None:
                raise StaleItemFacts("runtime generation changed")
            return detached
        if self._runtime_generation() != generation:
            raise StaleItemFacts("runtime generation changed")
        with manager.lock:
            if getattr(manager, "runtime_generation", generation) != generation:
                raise StaleItemFacts("manager generation changed")
            if not any(node is handle for node in manager.runtime_nodes.get(tier, ())):
                raise StaleItemFacts("node left the runtime")
            detached = copy.deepcopy(handle)
        if self._runtime_generation() != generation or self.runtime.manager is not manager:
            raise StaleItemFacts("runtime generation changed")
        return detached

    def subscription_entries(self) -> tuple[tuple[str, int, str], ...]:
        from config_utils import subscription_entry_url
        config = copy.deepcopy(self.runtime.config)
        tiers = config.get("tiers", {}) if isinstance(config, Mapping) else {}
        rows = []
        for tier in RUNTIME_TIERS:
            block = tiers.get(tier, {}) if isinstance(tiers, Mapping) else {}
            entries = block.get("subscription_urls", ()) if isinstance(block, Mapping) else ()
            for index, entry in enumerate(entries or ()):
                rows.append((tier, index, subscription_entry_url(entry)))
        return tuple(rows)

    def configured_items(self) -> list[dict[str, Any]]:
        return self.runtime.configured_items()

    def update_config_item(self, item, updates):
        return self.runtime.update_config_item(item, updates)

    def delete_config_item(self, item):
        return self.runtime.delete_config_item(item)

    def reload_core_config(self, *, reason: Any, user_commit_id: str) -> Mapping[str, Any]:
        return self.runtime.reload_core_config(
            mode="strict", reason=reason, user_commit_id=user_commit_id)

    def refresh_subscription(self, item):
        try:
            # T-P2-RT2: bounded acquire instead of check-then-call.
            return self.runtime.refresh_subscription(
                item, acquire_timeout=REFRESH_ACQUIRE_TIMEOUT)
        except Exception as exc:
            # desktop_runtime.RefreshLockBusy carries bridge_code.
            if getattr(exc, "bridge_code", None) == "lifecycle_busy":
                raise RefreshLockBusy("refresh lock busy") from exc
            raise

    def set_exit_selected_proxy(self, proxy_name):
        return self.runtime.set_exit_selected_proxy(proxy_name)

    def set_exit_selection(self, proxy_name, identity):
        return self.runtime.set_exit_selection(proxy_name=proxy_name, identity=identity)

    def persisted_exit_selection_key(self) -> str | None:
        from gateway_selection import read_selection

        local = self.runtime.read_local_config()
        tiers = local.get("tiers") if isinstance(local, Mapping) else None
        block = tiers.get("exit") if isinstance(tiers, Mapping) else None
        return read_selection(dict(block)).key if isinstance(block, Mapping) else ""

    def display_proxy_name(self, raw: str) -> str | None:
        getter = getattr(type(self.runtime), "bridge_display_proxy_name", None)
        value = getter(self.runtime, raw) if callable(getter) else None
        return value if isinstance(value, str) and value else None

    def exit_selection_readback(self) -> str | None:
        value = self._optional_getter("bridge_exit_selection_readback")
        return value if isinstance(value, str) and value else None

    def persisted_exit_selection(self) -> str | None:
        local = self.runtime.read_local_config()
        tiers = local.get("tiers") if isinstance(local, Mapping) else None
        block = tiers.get("exit") if isinstance(tiers, Mapping) else None
        if not isinstance(block, Mapping):
            return ""
        # Same precedence as manager.py (selected, then legacy selected_proxy).
        return str(block.get("selected") or block.get("selected_proxy") or "").strip()

    def exit_proxy_name(self, node_id: str) -> Mapping[str, Any] | None:
        getter = getattr(type(self.runtime), "bridge_exit_proxy_name", None)
        return getter(self.runtime, node_id) if callable(getter) else None

    def subscription_refresh_state(self) -> Mapping[str, Any] | None:
        return self._optional_getter("bridge_subscription_refresh_state")

    def subscription_refresh_busy(self) -> bool | None:
        value = self._optional_getter("bridge_subscription_refresh_busy")
        return value if isinstance(value, bool) else None


# ---------------------------------------------------------------------------
# Small result helpers
# ---------------------------------------------------------------------------

def _restarted_by(raw: Any, before_generation: int, after: Mapping[str, Any]) -> bool:
    """mode.set ``restarted``: the Core generation advanced because of this call.

    T-P2-RT2 (决定_第二波裁定 RG #6): set_network_mode reports its own
    ``core_generation_before/after``. No restart by the call (``restarted``
    false, or no generation step) is False even when another restart moved
    the generation meanwhile; exactly one step is the call's own restart.
    Anything else (more than one step: not attributable; an older runtime
    without the fields; the call raised) keeps the bridge's own samples.
    """
    observed = int(after["core_generation"]) > before_generation
    if not isinstance(raw, Mapping):
        return observed
    start, end = raw.get("core_generation_before"), raw.get("core_generation_after")
    if type(start) is not int or type(end) is not int:
        return observed
    if not raw.get("restarted") or end <= start:
        return False
    if end == start + 1:
        return True
    return observed


def _error(code: str, domain: str, reason: str | None = None, *,
           field: str | None = None) -> dict[str, Any]:
    # adapt_result turns apply_error/error.code into the jobs error code
    # without bridge_jobs._safe_code, so the closed set is enforced here.
    if any(code not in COMMAND_SPECS[name].errors for name in _DOMAIN_COMMANDS[domain]):
        raise AssertionError(f"unregistered business code for {domain}")
    error = {"code": code, "message_key": f"errors.{domain}.{reason or code}", "params": {}}
    if reason:
        error["reason_code"] = reason
    if field:
        error["field"] = field
    return error


def _exception_code(name: str, exc: BaseException) -> str:
    """Map exception classes, never text, onto the command's declared codes."""
    allowed = COMMAND_SPECS[name].errors
    if isinstance(exc, JobFailure):
        code = exc.code
    elif isinstance(exc, KeyError):
        code = "conflict"
    elif isinstance(exc, (ValueError, TypeError)):
        code = "validation"
    elif isinstance(exc, TimeoutError):
        code = "timeout"
    elif isinstance(exc, OSError):
        code = "io"
    else:
        code = "internal"
    if code in allowed:
        return code
    return "io" if isinstance(exc, OSError) and "io" in allowed else "internal"


def _optional(getter: Callable[[], Any]) -> Any:
    """Optional fact: a failing or missing getter is unknown (None)."""
    try:
        return getter()
    except Exception:
        return None


def _uint(value: Any) -> int:
    if type(value) is int and value >= 0:
        return value
    if type(value) is float and value >= 0 and value == value and value != float("inf"):
        return int(value)
    return 0


def _strict_reload_confirmed(result: Any) -> bool:
    """Same rule as DesktopRuntime._strict_core_reload_confirmed (private)."""
    if not isinstance(result, Mapping) or result.get("kept_existing_core"):
        return False
    if result.get("reloaded"):
        return True
    return bool(result.get("running"))


def _shape(item: Mapping[str, Any]) -> tuple[str, str]:
    tier = str(item.get("tier") or "")
    kind = str(item.get("kind") or "")
    return ("tier1" if tier == "tier1" else "imported", kind)


def _secret_values(value: str, category: str) -> dict[str, str]:
    """Pre-registration input for the redactor.

    Uses the canonical config walker (node URI auth, parsed password/uuid,
    node.extra secrets, urlsplit password) through the public
    ``bridge_import.draft_secret_values``; the raw value is always included.
    """
    try:
        from bridge_import import draft_secret_values
        if category == "subscription":
            values = dict(draft_secret_values(urls=[value]))
        else:
            values = dict(draft_secret_values(uris=[value]))
        values[value] = category
        return values
    except Exception:
        pass  # Fall back to the minimal local extraction below.
    values = {value: category}
    try:
        from urllib.parse import unquote, urlsplit
        password = unquote(urlsplit(value).password or "")
        if password:
            values[password] = category
    except ValueError:
        pass
    if "://" in value:
        try:
            from parser import parse_node_uri
            logger = logging.Logger("bridge-node-command-secrets")
            logger.addHandler(logging.NullHandler())
            node = parse_node_uri(value, logger=logger)
        except Exception:
            node = None
        if node is not None:
            for secret in (node.password, node.uuid):
                if isinstance(secret, str) and secret:
                    values[secret] = category
    return values


@dataclass(frozen=True)
class ResolvedItem:
    item_id: str
    kind: str                      # "node" | "subscription"
    tier: str                      # runtime tier key
    node: Any = None               # detached Node copy for loaded nodes
    url: str | None = None         # subscription URL
    config_item: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

class NodeCommands:
    """Handlers for the six P2-14 commands plus the refresh-owner view."""

    def __init__(self, port: NodeCommandPort, *,
                 network_guard: Callable[[], Any] | None = None,
                 recovery_gate: Callable[[], None] | None = None,
                 register_secrets: Callable[[dict[str, str]], None] | None = None,
                 refresh_secrets: Callable[[], None] | None = None,
                 clock: Callable[[], int] = time.monotonic_ns,
                 scope_isolated: bool = False) -> None:
        self.port = port
        # T-P2-H2 (决定_第五波裁定 33-#17): a custom (development) scope never
        # captures the host: mode.set to anything but system_proxy fails
        # closed before any runtime call or disk write.
        self._scope_isolated = bool(scope_isolated)
        self._network_guard = network_guard or nullcontext
        self._recovery_gate = recovery_gate
        self._register_secrets = register_secrets
        self._refresh_secrets = refresh_secrets
        self._clock = clock
        self._lock = threading.Lock()
        self._listeners: list[Callable[[str], None]] = []
        self._refreshing: dict[str, int] = {}
        self._refresh_jobs: dict[str, int] = {}
        # (target mode, Core generation) of the last saved-but-not-applied
        # mode.set: that Core still runs the previous mode (section 15).
        self._mode_unapplied: tuple[str, int] | None = None

    def _require_idle(self, partial: dict[str, Any]) -> None:
        """Recovery gate (no network lock): mutations that may strictly reload
        or stop Core never start while startup recovery is pending."""
        if self._recovery_gate is None:
            return
        try:
            self._recovery_gate()
        except Exception as exc:
            raise JobFailure("conflict", partial) from exc

    # -- integration hooks -------------------------------------------------

    def add_listener(self, callback: Callable[[str], None]) -> None:
        """Called after each mutation with a fact family: mode/nodes/
        subscriptions/gateway. Listeners schedule re-projection; they must
        not sample on this worker. Failures never change a command result."""
        if not callable(callback):
            raise ValueError("listener must be callable")
        with self._lock:
            self._listeners.append(callback)

    def _notify(self, family: str) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(family)
            except Exception:
                pass

    @contextmanager
    def _secret_boundary(self, values: dict[str, str] | None = None) -> Iterator[None]:
        """Section 12: register new values before any action that may log;
        afterwards atomically replace the table from the committed config."""
        if values and self._register_secrets is not None:
            try:
                self._register_secrets(dict(values))
            except Exception as exc:
                raise JobFailure("internal") from exc
        try:
            yield
        finally:
            if self._refresh_secrets is not None:
                try:
                    self._refresh_secrets()
                except Exception:
                    pass  # The union table remains a safe superset.

    def refresh_owner_snapshot(self) -> Mapping[str, Any] | None:
        """P2-06 NodeProjection(refresh_owner=...) input; None means unknown.

        The bridge only sees its own refresh jobs, not manager/import refreshes,
        so it never synthesizes refreshing=false. Only a complete runtime
        owner fact (port getter) is returned, as an immutable copy.
        """
        try:
            state = self.port.subscription_refresh_state()
        except Exception:
            return None
        if not isinstance(state, Mapping):
            return None
        generation, items = state.get("generation"), state.get("items")
        if (type(generation) is not int or generation < 0
                or not isinstance(state.get("stale"), bool)
                or not isinstance(items, (list, tuple))):
            return None
        for row in items:
            if (not isinstance(row, Mapping) or not isinstance(row.get("id"), str)
                    or not _OPAQUE_ID.fullmatch(row["id"])
                    or type(row.get("refreshing")) is not bool
                    or type(row.get("job_generation")) is not int or row["job_generation"] < 0):
                return None
        try:
            return immutable_record(copy.deepcopy(dict(state)))
        except TypeError:
            return None

    def bridge_refresh_activity(self) -> dict[str, dict[str, Any]]:
        """Local diagnostics: refreshes started through this bridge only."""
        with self._lock:
            return {item: {"refreshing": item in self._refreshing, "job_generation": job}
                    for item, job in self._refresh_jobs.items()}

    # -- item resolution ---------------------------------------------------

    def resolve(self, item_id: Any, *, need_config: bool) -> ResolvedItem:
        if not isinstance(item_id, str) or not _OPAQUE_ID.fullmatch(item_id):
            raise JobFailure("conflict")
        try:
            match = None
            for tier, index, url in self.port.subscription_entries():
                if url and subscription_item_id(tier, index, url) == item_id:
                    match = ResolvedItem(item_id, "subscription", tier, url=url)
                    break
            if match is None:
                generation, index = self.port.loaded_node_index()
                for tier, identity, handle in index:
                    if identity_item_id(generation, tier, identity) == item_id:
                        # Only the matched node is deep-copied.
                        node = self.port.detach_node(generation, tier, handle)
                        match = ResolvedItem(item_id, "node", tier, node=node)
                        break
        except StaleItemFacts as exc:
            raise JobFailure("conflict") from exc
        if match is None and need_config:
            # T-P2-31b: a ConfigItem ID (config.items) addresses configured
            # items, loaded or not (e.g. disabled). Ambiguous IDs (duplicate
            # entries share one) are conflict, never the first match.
            return self._resolve_config_item(item_id)
        if match is None:
            raise JobFailure("conflict")
        if not need_config:
            return match
        config = self._config_item(match, self.port.configured_items())
        if config is None:
            # Subscription members and automatic-pool nodes are not user config.
            raise JobFailure("conflict")
        return ResolvedItem(match.item_id, match.kind, match.tier, match.node, match.url, config)

    def _resolve_config_item(self, item_id: str) -> ResolvedItem:
        from bridge_runtime_access import config_item_id
        found = [item for item in self.port.configured_items()
                 if isinstance(item, Mapping) and item.get("kind") in {"node", "subscription"}
                 and str(item.get("tier") or "") in RUNTIME_TIERS
                 and config_item_id(item) == item_id]
        if len(found) != 1:
            raise JobFailure("conflict")
        item = copy.deepcopy(dict(found[0]))
        url = item.get("url") if item["kind"] == "subscription" else None
        return ResolvedItem(item_id, str(item["kind"]), str(item["tier"]),
                            url=url if isinstance(url, str) else None, config_item=item)

    @staticmethod
    def _config_item(match: ResolvedItem, items: list[Any]) -> dict[str, Any] | None:
        if match.kind == "subscription":
            keys = {match.url}
        elif match.tier == "tier1":
            from tier1_protocols import physical_node_name
            keys = {physical_node_name(match.node), match.node.name}
        else:
            keys = {str(match.node.uri or "").strip()}
        keys.discard("")
        keys.discard(None)
        found = [item for item in items if isinstance(item, Mapping)
                 and item.get("tier") == match.tier and item.get("kind") == match.kind
                 and str(item.get("key") or "") in keys]
        return copy.deepcopy(dict(found[0])) if len(found) == 1 else None

    @staticmethod
    def _families(resolved: ResolvedItem | None) -> tuple[str, ...]:
        """Fact families a config-item mutation can change."""
        families = ["nodes"]
        if resolved is not None and resolved.kind == "subscription":
            families.append("subscriptions")
        if resolved is not None and resolved.tier == "exit":
            # Disabling/deleting an exit item may clear the exit selection.
            families.append("gateway")
        return tuple(families)

    # -- mode.set ----------------------------------------------------------

    def mode_set(self, args: dict[str, Any], context: Any) -> dict[str, Any]:
        """changed/saved/restarted/applied are post-hoc facts (section 15).

        restarted = Core generation advanced across the operation, never the
        pre-operation running flag the runtime returns; with the runtime's
        own before/after generations it is attributed to this call
        (``_restarted_by``). saved = the persisted
        local config (or, unchanged, the loaded config) holds the mode.
        applied = loaded mode matches and the Core that runs (if any) was
        started under it without pending cleanup; with no Core before or
        after, the mode is effective at next start. saved and not applied
        keeps apply_error and fails the job.

        "Unchanged" only means the *loaded config* already holds the mode
        (the runtime reloads config before restarting Core), so the unchanged
        branch re-checks Core facts too: a Core left running by a failed
        restart still runs the previous mode.

        applied covers Core only. It does not claim the system-proxy / TUN
        takeover took effect (system_proxy capture follows exit admission
        asynchronously); that remains a service/route fact.
        """
        mode = args["mode"]
        target = RUNTIME_MODES[mode]
        result = {"mode": mode, "changed": False, "saved": False, "restarted": False,
                  "applied": False, "apply_error": None}
        if self._scope_isolated and target != "system_proxy":
            # Nothing is attempted: no lock, no runtime call, no write.
            error = _error("not_allowed", "mode", "scope_isolated")
            raise JobFailure("not_allowed", result, error=error)
        try:
            guard = self._network_guard()
            guard.__enter__()
        except Exception as exc:
            raise JobFailure("conflict", result) from exc
        try:
            with self._secret_boundary():
                try:
                    before = self.port.core_fact()
                    before_generation = int(before["core_generation"])
                except Exception as exc:  # nothing attempted yet
                    raise JobFailure("io", result) from exc
                persisted_before = _optional(self.port.persisted_network_mode)
                try:
                    raw = self.port.set_network_mode(target)
                    failure = None
                except Exception as exc:  # facts below decide what happened
                    raw, failure = None, exc
                # From here on set_network_mode may already have reloaded the
                # runtime's mode: every "not applied" exit records the target
                # so a retry of the same mode cannot pass as unchanged+applied.
                # The recorded generation is the pre-switch Core's: only that
                # Core is known to run the previous mode; a Core whose
                # generation advanced was started after the reload.
                stale = {"core_generation": before_generation}
                try:
                    after = self.port.core_fact()
                    result["restarted"] = _restarted_by(raw, before_generation, after)
                    persisted = self.port.persisted_network_mode()
                    loaded = self.port.network_mode()
                except Exception as exc:
                    # Keep what is already confirmed (restarted); saved and
                    # applied cannot be confirmed without the readback.
                    result.update(saved=False, applied=False,
                                  apply_error=_error("io", "mode", "readback_failed"))
                    self._remember_unapplied(target, stale)
                    raise JobFailure("io", result) from (failure or exc)
                core_mode = _optional(self.port.core_loaded_network_mode)
                if failure is not None:
                    wrote = persisted != persisted_before
                    saved = persisted == target
                    result.update(changed=wrote and saved, saved=saved)
                    admin = (isinstance(failure, PermissionError)
                             and not getattr(failure, "filename", None)
                             and target in {"tun", "mixed"} and not wrote)
                    code = "admin_required" if admin else _exception_code("mode.set", failure)
                    if not saved:
                        self._remember_unapplied(target, stale)
                        raise JobFailure(code, result) from failure
                    result["apply_error"] = _error(code, "mode",
                                                   "restart_failed" if wrote else "apply_failed")
                    if not (result["restarted"] and after["running"]
                            and not after["cleanup_pending"]
                            and core_mode in (None, target)):
                        self._remember_unapplied(target, stale)
                    return result
                changed = bool(raw.get("changed")) if isinstance(raw, Mapping) else False
                result["changed"] = changed
                if not changed:
                    if loaded != target:
                        result["apply_error"] = _error("conflict", "mode", "mode_unconfirmed")
                        raise JobFailure("conflict", result)
                    # Nothing written now; the loaded config holds the mode.
                    result["saved"] = True
                    if not self._core_runs_mode(target, after, core_mode):
                        result["apply_error"] = _error("io", "mode", "restart_unconfirmed")
                        self._remember_unapplied(target, after)
                        raise JobFailure("io", result)
                    result["applied"] = True
                    self._remember_unapplied(None, after)
                    return result
                result["saved"] = persisted == target
                restart_needed = bool(raw.get("restarted"))
                core_ok = not after["cleanup_pending"] and core_mode in (None, target)
                if restart_needed:
                    applied = result["restarted"] and bool(after["running"]) and core_ok
                    reason = "restart_unconfirmed"
                else:
                    applied = core_ok and (not after["running"] or core_mode == target)
                    reason = "core_not_restarted"
                result["applied"] = bool(result["saved"] and loaded == target and applied)
                if not result["applied"]:
                    result["apply_error"] = _error("io" if restart_needed else "conflict",
                                                   "mode", reason if result["saved"] else "save_unconfirmed")
                self._remember_unapplied(None if result["applied"] else target, stale)
                if not result["saved"]:
                    raise JobFailure("io", result)
                return result
        finally:
            try:
                guard.__exit__(None, None, None)
            finally:
                self._notify("mode")

    def _remember_unapplied(self, target: str | None, after: Mapping[str, Any]) -> None:
        with self._lock:
            if target is None:
                self._mode_unapplied = None
            else:
                self._mode_unapplied = (target, _uint(after.get("core_generation")))

    def _core_runs_mode(self, target: str, after: Mapping[str, Any],
                        core_mode: str | None) -> bool:
        """Running Core (if any) was started under target, nothing pending.

        Prefers the runtime's Core-mode getter; until it exists, a Core whose
        generation still equals the one a failed switch to target left
        running is known to run the previous mode.
        """
        if after["cleanup_pending"]:
            return False
        if not after["running"]:
            return True  # effective at next start
        if core_mode is not None:
            return core_mode == target
        with self._lock:
            return self._mode_unapplied != (target, _uint(after.get("core_generation")))

    # -- nodes.update ------------------------------------------------------

    def _updates(self, item: Mapping[str, Any], args: Mapping[str, Any]
                 ) -> tuple[dict[str, Any], dict[str, str]]:
        shape = _shape(item)
        plain = _PLAIN_UPDATES.get(shape)
        secret_map = _SECRET_UPDATES.get(shape)
        if plain is None or secret_map is None:
            raise JobFailure("validation")
        updates: dict[str, Any] = {}
        for key, value in args["updates"].items():
            if key not in plain:
                raise JobFailure("validation")
            if key == "enabled" and type(value) is not bool:
                raise JobFailure("validation")
            if key in {"name", "address"} and (not isinstance(value, str) or "://" in value
                                                or (key == "name" and not value.strip())):
                raise JobFailure("validation")
            if key == "port" and (type(value) is not int or not 1 <= value <= 65535):
                raise JobFailure("validation")
            if key == "fallback_order" and (not isinstance(value, list) or not all(
                    isinstance(entry, str) and entry and "://" not in entry for entry in value)):
                raise JobFailure("validation")
            updates[key] = copy.deepcopy(value)
        secrets: dict[str, str] = {}
        seen: set[str] = set()
        for edit in args.get("secrets", ()):
            field = edit["field"]
            if field not in secret_map or field in seen:
                raise JobFailure("validation")
            if "target_id" in edit and edit["target_id"] != args["item_id"]:
                raise JobFailure("validation")
            seen.add(field)
            target, clearable = secret_map[field]
            action = edit["action"]
            if action == "keep":
                continue
            if action == "clear":
                if not clearable:
                    raise JobFailure("validation")
                updates[target] = ""
                continue
            value = edit.get("value")
            if not isinstance(value, str) or not value.strip():
                raise JobFailure("validation")
            updates[target] = value.strip()
            secrets.update(_secret_values(value.strip(),
                "subscription" if field == "subscription_url" else "node"))
        if not updates:
            raise JobFailure("validation")
        return updates, secrets

    def nodes_update(self, args: dict[str, Any], context: Any) -> dict[str, Any]:
        """Reuse the existing node transaction once; never reload again after it.

        Success means the transaction persisted, reloaded runtime config and,
        when Core ran, confirmed the strict reload. A raised transaction
        either never wrote or rolled back; saved reports whether a persisted
        change survived (rollback unconfirmed), compared on configured items.
        """
        unsaved = {"saved": False, "applied": False, "apply_error": None, "pending": None}
        self._require_idle(dict(unsaved))
        try:
            resolved = self.resolve(args["item_id"], need_config=True)
            item = resolved.config_item
            updates, secrets = self._updates(item, args)
        except JobFailure as exc:
            raise JobFailure(exc.code, dict(unsaved)) from None
        try:
            with self._secret_boundary(secrets):
                before = self.port.configured_items()
                try:
                    self.port.update_config_item(item, updates)
                except Exception as exc:
                    try:
                        after = self.port.configured_items()
                    except Exception:
                        raise JobFailure("io") from exc
                    rolled_back = isinstance(exc, RuntimeError) and exc.__cause__ is not None
                    if after != before:
                        return {"saved": True, "applied": False, "pending": None,
                                "apply_error": _error("io", "nodes", "rollback_unconfirmed")}
                    code = "io" if rolled_back else _exception_code("nodes.update", exc)
                    raise JobFailure(code, dict(unsaved, apply_error=(
                        _error("io", "nodes", "rolled_back") if rolled_back else None))) from exc
                return {"saved": True, "applied": True, "apply_error": None, "pending": None}
        finally:
            for family in self._families(resolved):
                self._notify(family)

    # -- nodes.delete ------------------------------------------------------

    @staticmethod
    def _present(item: Mapping[str, Any], items: list[Any]) -> bool:
        return any(isinstance(row, Mapping) and row.get("tier") == item.get("tier")
                   and row.get("kind") == item.get("kind") and row.get("key") == item.get("key")
                   for row in items)

    def nodes_delete(self, args: dict[str, Any], context: Any) -> dict[str, Any]:
        """saved = the item left the configuration; then apply to a running Core.

        The existing delete reloads runtime config only; the strict Core
        reload is added here (ASK-02). A failed apply is reported, not hidden.
        """
        unsaved = {"saved": False, "applied": False, "apply_error": None, "pending": None}
        self._require_idle(dict(unsaved))
        try:
            resolved = self.resolve(args["item_id"], need_config=True)
            item = resolved.config_item
        except JobFailure as exc:
            raise JobFailure(exc.code, dict(unsaved)) from None
        try:
            with self._secret_boundary():
                try:
                    self.port.delete_config_item(item)
                except Exception as exc:
                    try:
                        gone = not self._present(item, self.port.configured_items())
                    except Exception:
                        raise JobFailure("io") from exc
                    if isinstance(exc, KeyError) or not gone:
                        raise JobFailure(_exception_code("nodes.delete", exc), dict(unsaved)) from exc
                    return {"saved": True, "applied": False, "pending": None,
                            "apply_error": _error("io", "nodes", "runtime_reload_failed")}
                result = {"saved": True, "applied": False, "apply_error": None, "pending": None}
                if not self.port.core_fact()["running"]:
                    result["applied"] = True  # runtime config reloaded; no Core to apply
                    return result
                try:
                    from runtime_reload_gate import RuntimeReloadReason
                    reload = self.port.reload_core_config(
                        reason=RuntimeReloadReason.USER_GRAPH_CONFIG,
                        user_commit_id=f"node-delete:{self._clock()}")
                except Exception:
                    # Core-apply stage only: the deletion itself is committed.
                    result["apply_error"] = _error("io", "nodes", "core_reload_failed")
                    return result
                if _strict_reload_confirmed(reload):
                    result["applied"] = True
                else:
                    result["apply_error"] = _error("io", "nodes", "reload_unconfirmed")
                return result
        finally:
            for family in self._families(resolved):
                self._notify(family)

    # -- nodes.reveal (private) --------------------------------------------

    def nodes_reveal(self, args: dict[str, Any], context: Any) -> dict[str, Any]:
        """Private once-only data; never retained here, never a jobs result.

        The executor delivers only to the active requesting owner; a revoked
        owner gets nothing and this handler stops before reading secrets.
        """
        revoked = getattr(context, "owner_revoked", None)
        if revoked is not None and revoked.is_set():
            raise JobFailure("not_allowed")
        wanted = args.get("fields") or list(REVEAL_FIELDS)
        resolved = self.resolve(args["item_id"], need_config=False)
        if resolved.kind == "subscription":
            available = {"subscription_url": resolved.url}
        else:
            from node_status_view import node_connection_snapshot
            snapshot = node_connection_snapshot(resolved.node)
            available = {name: snapshot.get(name) for name in ("password", "uuid", "private_key", "uri")}
        fields = {name: available[name] for name in wanted
                  if isinstance(available.get(name), str) and available[name]}
        if revoked is not None and revoked.is_set():
            raise JobFailure("not_allowed")
        return {"item_id": resolved.item_id, "fields": fields}

    # -- subscriptions.refresh ---------------------------------------------

    def subscriptions_refresh(self, args: dict[str, Any], context: Any) -> dict[str, Any]:
        """Keep stored (cache) and applied (Core) separate (section 15).

        Lock contention is reported, not waited out silently. The section-10
        business codes are closed (io/timeout/conflict), so a stop in progress
        or a RefreshLockBusy from the runtime's bounded acquire is
        ``conflict`` with reason_code ``lifecycle_busy``; a second bridge
        refresh of the same subscription is conflict/refresh_in_progress.
        The busy getter is no longer consulted (T-P2-RT2, 决定_第二波裁定 RG
        #7): the bounded acquire is the race-free check, not check-then-call.
        """
        idle = {"total": 0, "healthy": 0, "stored": False, "applied": False,
                "retained_previous": True, "apply_error": None, "error": None}
        unknown = dict(idle, error=_error("conflict", "subscriptions", "unknown_item"))
        busy_result = dict(idle, error=_error("conflict", "subscriptions", "lifecycle_busy"))
        self._require_idle(dict(idle, error=_error("conflict", "subscriptions", "recovery_pending")))
        try:
            resolved = self.resolve(args["item_id"], need_config=True)
        except JobFailure as exc:
            raise JobFailure(exc.code, unknown) from None
        if resolved.kind != "subscription":
            raise JobFailure("conflict", unknown)
        lifecycle = self.port.lifecycle_status()
        if (lifecycle["intent"].get("stop_requested") is True
                or lifecycle["runtime"].get("startup_phase") == "stopping"):
            raise JobFailure("conflict", busy_result)
        item_id = resolved.item_id
        with self._lock:
            if item_id in self._refreshing:
                raise JobFailure("conflict", dict(idle, error=_error(
                    "conflict", "subscriptions", "refresh_in_progress")))
            job = self._refresh_jobs.get(item_id, 0) + 1
            self._refresh_jobs[item_id] = job
            self._refreshing[item_id] = job
        self._notify("subscriptions")
        try:
            with self._secret_boundary():
                try:
                    raw = self.port.refresh_subscription(resolved.config_item)
                except RefreshLockBusy as exc:
                    raise JobFailure("conflict", busy_result) from exc
                except Exception as exc:
                    code = _exception_code("subscriptions.refresh", exc)
                    if code == "validation":
                        code = "conflict"
                    raise JobFailure(code, dict(idle, error=_error(
                        code, "subscriptions", "refresh_rejected"))) from exc
                return self._refresh_result(raw)
        finally:
            with self._lock:
                self._refreshing.pop(item_id, None)
            self._notify("subscriptions")

    @staticmethod
    def _refresh_result(raw: Any) -> dict[str, Any]:
        if not isinstance(raw, Mapping):
            raise JobFailure("internal")
        stored = raw.get("stored") is True
        # No Core running: nothing to apply now, the next start loads the
        # stored subscription, so a save is done with applied=true
        # (决定_第一波裁定2 cross-ticket 1; the page shows "next start"
        # from service.state).
        core_stopped = (raw.get("core_running") is False and not raw.get("apply_error")
                        and not raw.get("error"))
        applied = stored and (raw.get("applied") is True or core_stopped)
        retained = raw.get("retained_previous")
        result = {"total": _uint(raw.get("total")), "healthy": _uint(raw.get("healthy")),
                  "stored": stored, "applied": applied,
                  "retained_previous": retained if isinstance(retained, bool) else not stored,
                  "apply_error": None, "error": None}
        if raw.get("error"):
            empty = _EMPTY_SUBSCRIPTION_TEXT in str(raw["error"])
            result["error"] = _error("io", "subscriptions",
                                     "empty_subscription" if empty else "refresh_failed")
        elif not stored:
            result["error"] = _error("io", "subscriptions", "refresh_unconfirmed")
        if stored and not applied:
            if raw.get("apply_error"):
                result["apply_error"] = _error("io", "subscriptions", "core_reload_failed")
            else:
                result["apply_error"] = _error("io", "subscriptions", "apply_unconfirmed")
        return result

    # -- gateway.select ----------------------------------------------------

    def _gateway_deactivate(self, result: dict[str, Any]) -> dict[str, Any]:
        """``gateway.select{proxy_name:""}`` (T-P4-C2 #9); same reporting rules."""
        with self._secret_boundary():
            try:
                raw = self.port.set_exit_selected_proxy("")
            except Exception as exc:
                result["apply_error"] = _error("io", "gateway", "apply_failed")
                raise JobFailure("io", result) from exc
            if not isinstance(raw, Mapping):
                raise JobFailure("internal")
            # Echo of the accepted PUT (mihomo_api.select_proxy), not a readback.
            selection = raw.get("selection")
            fallback = (isinstance(selection, Mapping) and selection.get("group") == "FINAL-EXIT"
                        and selection.get("selected") == "PROXY-FALLBACK")
            if raw.get("applied") is True and fallback:
                result["applied"] = True
            elif raw.get("applied") is True:
                result["apply_error"] = _error("io", "gateway", "selection_unconfirmed")
            elif raw.get("apply_error"):
                superseded = _SUPERSEDED_TEXT in str(raw["apply_error"])
                result["apply_error"] = (_error("conflict", "gateway", "superseded")
                                         if superseded else
                                         _error("io", "gateway", "reload_unconfirmed"))
            elif raw.get("core_running") is False:
                saved = _optional(lambda: self.port.persisted_exit_selection())
                if saved is None or saved == "":
                    result["applied"] = True
                else:
                    result["apply_error"] = _error("io", "gateway", "selection_unconfirmed")
            else:
                result["apply_error"] = _error("io", "gateway", "apply_unconfirmed")
            return result

    def gateway_select(self, args: dict[str, Any], context: Any) -> dict[str, Any]:
        """selected is the intent; selection is the actual fact.

        selection is the side-effect-free EXIT-SELECT readback once the
        runtime offers it (port getter); until then it means "Mihomo accepted
        the PUT for this name" (select_proxy echoes the request, no readback).
        applied=true only when the runtime confirmed the strict reload and
        selection equals exactly the requested proxy, or, with no Core
        running, when the saved selection is the requested proxy (effective
        at next start; selection is then the saved fact). Validation happens
        before the call; any exception after it (the runtime writes
        config.local before reloading) is io with apply_error.

        T-P4-C2 #9 (K-10): ``proxy_name=""`` deactivates the Gateway: the
        runtime removes the saved selection and points FINAL-EXIT back at
        PROXY-FALLBACK (``set_exit_selected_proxy("")``). Done means no Gateway
        is selected: ``selected=""``, ``selection=null``, ``applied=true``
        when, after the confirmed strict reload, the PUT FINAL-EXIT ->
        PROXY-FALLBACK was accepted (the runtime's selection dict is
        select_proxy's echo of that PUT target, not a FINAL-EXIT readback),
        or, with no Core, when the saved selection is empty.

        T-P2-H5: ``node_id`` (preferred) selects a Gateway row by its P2-04
        Node.id; see ``_gateway_select_node``. ``proxy_name`` stays for
        compatibility; both together never reach a handler (the request
        validator answers invalid_type) and are validation here.
        """
        if "node_id" in args:
            if "proxy_name" in args:
                raise JobFailure("validation")
            return self._gateway_select_node(args["node_id"])
        name = args["proxy_name"]
        result = {"selected": name, "applied": False, "apply_error": None,
                  "selection": None, "pending": None}
        if name == "":
            self._require_idle(dict(result))
            try:
                return self._gateway_deactivate(result)
            finally:
                self._notify("gateway")
        if not name or "://" in name or name != name.strip():
            raise JobFailure("validation", result)
        self._require_idle(dict(result))
        try:
            return self._gateway_apply(name, result)
        finally:
            self._notify("gateway")

    def _gateway_select_node(self, node_id: Any) -> dict[str, Any]:
        """T-P2-H5: select a Gateway row by its P2-04 Node.id.

        The runtime getter names the node exactly as the renderer does and
        gives its Gateway identity; the selection is saved as that identity
        (with the name as label), so it follows the node through latency
        reorders, namesakes in any tier and the Tier3 naming switch, and is
        applied as the member the reloaded EXIT-SELECT holds for that node.

        An unknown or stale id, an id of another tier, or a Gateway node with
        no proxy in Mihomo (or several identities behind one id) is
        ``conflict`` with reason_code ``unknown_node``, ``not_gateway_node``
        or ``node_unusable``; nothing is written. A name with outer
        whitespace is ``node_unusable`` too. Raw names only go to the
        runtime: on the wire ``selected`` and ``selection`` are display names
        (what gateway.current and nodes.list show); ``selection`` is null
        unless the pick was applied.
        """
        if not isinstance(node_id, str) or not _OPAQUE_ID.fullmatch(node_id):
            raise JobFailure("conflict", error=_error("conflict", "gateway", "unknown_node"))
        try:
            facts = self.port.exit_proxy_name(node_id)
        except Exception as exc:
            raise JobFailure("internal") from exc
        status = facts.get("status") if isinstance(facts, Mapping) else None
        if status == "not_gateway":
            raise JobFailure("conflict", error=_error("conflict", "gateway", "not_gateway_node"))
        if status == "unusable":
            raise JobFailure("conflict", error=_error("conflict", "gateway", "node_unusable"))
        if status != "ok":  # unknown, stale, or no runtime getter
            raise JobFailure("conflict", error=_error("conflict", "gateway", "unknown_node"))
        raw, shown = facts.get("proxy_name"), facts.get("display_name")
        identity = facts.get("identity")
        if not (isinstance(raw, str) and raw and isinstance(shown, str) and shown):
            raise JobFailure("internal")
        if not (isinstance(identity, str) and identity.startswith("v1:")):
            raise JobFailure("internal")
        if raw != raw.strip():
            # The runtime strips the name it saves; " Tokyo " would then name
            # no proxy (EXIT-SELECT [REJECT]) and "   " would deactivate the
            # Gateway. Same guard as the proxy_name path, before any write.
            raise JobFailure("conflict", error=_error("conflict", "gateway", "node_unusable"))
        shown = _UUID.sub("[REDACTED]", shown)  # same as nodes.list names

        def wire(value: dict[str, Any]) -> dict[str, Any]:
            member = value.get("selection")
            if value.get("applied") is True and isinstance(member, str) and member:
                if member == raw:
                    value["selected"] = value["selection"] = shown
                else:
                    # The node was loaded under another name (it moved).
                    moved = _UUID.sub("[REDACTED]", self._display_name(member) or "")
                    if not moved:
                        raise JobFailure("internal")
                    value["selected"] = value["selection"] = moved
            else:
                value["selected"] = shown
                value["selection"] = None
            return value

        result = {"selected": raw, "applied": False, "apply_error": None,
                  "selection": None, "pending": None}
        self._require_idle(wire(dict(result)))
        try:
            return wire(self._gateway_apply(raw, result, identity=identity))
        except JobFailure as exc:
            if isinstance(exc.partial_result, dict):
                wire(exc.partial_result)
            raise
        finally:
            self._notify("gateway")

    def _display_name(self, member: str) -> str | None:
        display = getattr(self.port, "display_proxy_name", None)
        value = _optional(lambda: display(member)) if callable(display) else None
        return value if isinstance(value, str) and value else None

    def _gateway_apply(self, name: str, result: dict[str, Any], *,
                       identity: str | None = None) -> dict[str, Any]:
        """Select a non-empty proxy name (shared by proxy_name and node_id).

        With ``identity`` (node_id path) the selection is saved as that node;
        otherwise by name (the runtime adds the identity when the name
        provably denotes one Gateway node). applied=true only when the
        runtime confirmed the strict reload and the selection (readback, else
        the PUT echo) is exactly the member the runtime selected for the
        saved node; a node that is no longer exported is selection_unconfirmed
        with selection unknown.
        """
        with self._secret_boundary():
            try:
                raw = (self.port.set_exit_selection(name, identity) if identity
                       else self.port.set_exit_selected_proxy(name))
            except Exception as exc:
                result["apply_error"] = _error("io", "gateway", "apply_failed")
                raise JobFailure("io", result) from exc
            if not isinstance(raw, Mapping):
                raise JobFailure("internal")
            member = raw.get("member")
            expected = member if isinstance(member, str) and member else name
            selection = raw.get("selection")
            selected = selection.get("selected") if isinstance(selection, Mapping) else None
            selected = selected if isinstance(selected, str) and selected else None
            if raw.get("applied") is True and selected is not None:
                readback = _optional(self.port.exit_selection_readback)
                if isinstance(readback, str) and readback:
                    selected = readback
            result["selection"] = selected
            if raw.get("applied") is True and result["selection"] == expected:
                result["applied"] = True
            elif raw.get("applied") is True:
                result["apply_error"] = _error("io", "gateway", "selection_unconfirmed")
            elif raw.get("apply_error") == "gateway_member_unavailable":
                # Saved, reloaded, but the picked node is not exported now:
                # EXIT-SELECT fails closed, nothing was applied.
                result["selection"] = None
                result["apply_error"] = _error("io", "gateway", "selection_unconfirmed")
            elif raw.get("apply_error"):
                superseded = _SUPERSEDED_TEXT in str(raw["apply_error"])
                result["apply_error"] = (_error("conflict", "gateway", "superseded")
                                         if superseded else
                                         _error("io", "gateway", "reload_unconfirmed"))
            elif raw.get("core_running") is False:
                # Saved and runtime config reloaded, no Core to apply to:
                # done with applied=true, effective at the next start
                # (决定_第一波裁定2 cross-ticket 1). selection is the
                # saved fact; a readable file holding another selection is
                # not confirmed, an unreadable one falls back to the
                # runtime's completed write.
                # A name pick may be saved under the label of the node the
                # name denotes ('Tokyo' -> 'Tokyo-2'): confirm what the
                # runtime reports it saved, not the request.
                saved_label = raw.get("selected")
                saved_label = saved_label if isinstance(saved_label, str) and saved_label else name
                saved_key = raw.get("selection_key")
                if identity:
                    saved = _optional(lambda: self.port.persisted_exit_selection_key())
                    confirmed = saved is None or saved == "id:" + identity
                elif isinstance(saved_key, str) and saved_key:
                    saved = _optional(lambda: self.port.persisted_exit_selection_key())
                    confirmed = saved is None or saved == saved_key
                else:
                    saved = _optional(lambda: self.port.persisted_exit_selection())
                    confirmed = saved is None or saved == saved_label
                if confirmed:
                    result["selection"] = name if identity else saved_label
                    result["applied"] = True
                else:
                    result["apply_error"] = _error("io", "gateway", "selection_unconfirmed")
            else:
                result["apply_error"] = _error("io", "gateway", "apply_unconfirmed")
            return result


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------

def _session_recovery_gate(session: Any) -> Callable[[], None]:
    """report.gate.require_idle(), resolved at call time (may be None)."""
    def gate() -> None:
        current = getattr(getattr(session, "report", None), "gate", None)
        if current is not None:
            current.require_idle()
    return gate


def _session_network_guard(session: Any) -> Callable[[], Any]:
    """Same boundary as BridgeSession.restart: network lock + recovery gate."""
    recovery_gate = _session_recovery_gate(session)

    @contextmanager
    def guard():
        lock = getattr(session, "network_lock", None)
        with lock if lock is not None else nullcontext():
            recovery_gate()
            yield
    return guard


def _session_secret_hooks(session: Any):
    """T-P2-31b: draft values are a lease of the session's layered registry
    (bridge_secrets.session_secret_hooks); never a whole-table push."""
    from bridge_secrets import session_secret_hooks
    return session_secret_hooks(session, "node_commands")


def session_scope_isolated(session: Any) -> bool:
    """T-P2-H2 (33-#17): the session's scope is the custom development scope
    (same condition as bridge_main.enforce_scope_config). No scope (doubles,
    the default scope) keeps the previous behaviour."""
    scope = getattr(session, "scope", None)
    return (scope is not None and getattr(scope, "kind", None) == "custom"
            and not getattr(scope, "may_take_over_host_network", False))


def register(session: Any, *, port: NodeCommandPort | None = None) -> NodeCommands:
    """Register the six commands on session.executor (specs from bridge_commands).

    All six use the contract's domain/reply/cancellation: mode.set,
    nodes.update/delete, subscriptions.refresh, gateway.select are mutation
    jobs cancellable only while queued (no cancel callback); nodes.reveal is
    a normal-domain private reply revoked with its owner by the executor.
    mode.set holds session.network_lock plus the recovery gate; the other
    four mutations check the recovery gate only (no network lock).
    Returns the handler so integration can pass refresh_owner_snapshot to
    NodeProjection and attach re-projection listeners via add_listener().
    """
    runtime = getattr(session, "runtime", None)
    if runtime is None:
        runtime = session.access.runtime
    register_secrets, refresh_secrets = _session_secret_hooks(session)
    commands = NodeCommands(port or RuntimeNodeCommandAdapter(runtime),
                            network_guard=_session_network_guard(session),
                            recovery_gate=_session_recovery_gate(session),
                            register_secrets=register_secrets,
                            refresh_secrets=refresh_secrets,
                            scope_isolated=session_scope_isolated(session))
    record_service = getattr(session, "record_service", None)
    if callable(record_service):
        def service_listener(family: str) -> None:
            if family == "mode":
                record_service()
        commands.add_listener(service_listener)
    executor = session.executor
    executor.register("mode.set", commands.mode_set)
    executor.register("nodes.update", commands.nodes_update)
    executor.register("nodes.delete", commands.nodes_delete)
    executor.register("nodes.reveal", commands.nodes_reveal)
    executor.register("subscriptions.refresh", commands.subscriptions_refresh)
    executor.register("gateway.select", commands.gateway_select)
    return commands
