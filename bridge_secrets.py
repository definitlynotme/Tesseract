"""Session-level layered registry of transient secrets (T-P2-31a).

One registry per BridgeSession feeds the log redactor
(``LogCollector.refresh_known_secret_values``). The redaction table is the
union of three layers, never a wholesale replacement by one module:

* the persistent base: ``RuntimeAccess.known_secret_values()`` (saved config
  and loaded nodes), cached and re-read only on ``refresh(reload_base=True)``
  (BridgeSession.refresh_secrets, after config transactions);
* leases: each module ``lease(owner, values) -> handle`` before anything that
  may print its draft/pairing credentials and ``release(handle)`` afterwards.
  Concurrent leases never overwrite each other; a value held by several
  leases stays until the last one is released (lease counting);
* the external layer: modules that still push a whole table through the
  redactor's refresh method (``push_external``) keep it until the next
  ``refresh(reset_external=True)``, so a concurrent lease refresh cannot drop
  their draft values either.

Values map ``secret text -> category``; the first registration of a value
keeps its category. Nothing here logs, publishes or persists a value.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import count
import threading
from typing import Any, Callable, Mapping


@dataclass(frozen=True)
class SecretLease:
    """Opaque handle of one registered layer."""

    id: int
    owner: str


def _clean(values: Mapping[str, str] | None) -> dict[str, str]:
    table: dict[str, str] = {}
    for value, category in dict(values or {}).items():
        if isinstance(value, str) and value and isinstance(category, str) and category:
            table.setdefault(value, category)
    return table


class SecretRegistry:
    """Layered, lease-counted transient secret table for one session.

    ``base``: callable returning the persistent table (blocking: config read;
    normal/mutation workers only). ``sink``: the redactor's replacement
    method, attached once the LogCollector exists (``attach``). ``defer``:
    ``submit_normal``-style callable used by ``release`` so that a release on
    a control/hook thread never reads config; when it is refused the values
    simply stay registered until the next refresh (over-redaction is safe).
    """

    def __init__(self, base: Callable[[], Mapping[str, str]] | None = None, *,
                 sink: Callable[[dict[str, str]], Any] | None = None,
                 defer: Callable[[Callable[[], Any]], bool] | None = None) -> None:
        self._base_source = base
        self._sink = sink
        self._defer = defer
        self._lock = threading.Lock()
        self._sink_lock = threading.Lock()
        self._ids = count(1)
        self._base: dict[str, str] | None = None
        self._layers: dict[int, tuple[str, dict[str, str]]] = {}
        self._external: dict[str, str] = {}

    # -- wiring ---------------------------------------------------------------

    def attach(self, sink: Callable[[dict[str, str]], Any]) -> None:
        """Install the redactor's replacement method (the original one)."""
        if not callable(sink):
            raise ValueError("secret sink must be callable")
        with self._lock:
            self._sink = sink

    @property
    def attached(self) -> bool:
        with self._lock:
            return self._sink is not None

    # -- layers ---------------------------------------------------------------

    def lease(self, owner: str, values: Mapping[str, str] | None, *,
              refresh: bool = True) -> SecretLease | None:
        """Register one layer; returns None for an empty table. With
        ``refresh`` the redactor is updated synchronously before returning
        (register before anything that may print). Raises when that refresh
        fails, after withdrawing the layer, so callers can abort first."""
        table = _clean(values)
        if not table:
            return None
        handle = SecretLease(next(self._ids), str(owner or "anonymous"))
        with self._lock:
            self._layers[handle.id] = (handle.owner, table)
        if refresh:
            try:
                self.refresh(strict=True)
            except Exception:
                with self._lock:
                    self._layers.pop(handle.id, None)
                raise
        return handle

    def release(self, handle: SecretLease | None, *, refresh: str = "defer") -> bool:
        """Withdraw one layer. ``refresh``: ``defer`` (default, via ``defer``
        or kept registered when refused), ``now`` or ``none``. Unknown or
        repeated handles return False."""
        if not isinstance(handle, SecretLease):
            return False
        with self._lock:
            if self._layers.pop(handle.id, None) is None:
                return False
        if refresh == "now" or (refresh == "defer" and self._defer is None):
            self.refresh()
        elif refresh == "defer":
            try:
                self._defer(self.refresh)
            except Exception:
                pass
        return True

    def push_external(self, values: Mapping[str, str] | None) -> None:
        """Compatibility entry for modules that push a whole table through the
        redactor (bridge_settings, bridge_node_commands): kept as one layer
        until the session's next ``refresh(reset_external=True)``."""
        with self._lock:
            self._external = _clean(values)
        self.refresh()

    # -- reads ----------------------------------------------------------------

    def transient(self) -> dict[str, str]:
        """Union of all leased layers (no base, no external layer)."""
        with self._lock:
            merged: dict[str, str] = {}
            for _owner, table in self._layers.values():
                for value, category in table.items():
                    merged.setdefault(value, category)
            return merged

    def lease_count(self, owner: str | None = None) -> int:
        with self._lock:
            return sum(owner is None or layer_owner == owner
                       for layer_owner, _ in self._layers.values())

    def merged(self, *, reload_base: bool = False) -> dict[str, str]:
        """Base, then leases in registration order, then the external layer."""
        base = self._load_base(reload_base)
        with self._lock:
            merged = dict(base)
            for _owner, table in self._layers.values():
                for value, category in table.items():
                    merged.setdefault(value, category)
            for value, category in self._external.items():
                merged.setdefault(value, category)
            return merged

    def _load_base(self, reload_base: bool) -> dict[str, str]:
        with self._lock:
            cached = self._base
        if cached is not None and not reload_base:
            return cached
        if self._base_source is None:
            fresh: dict[str, str] = {}
        else:
            fresh = _clean(self._base_source())
        with self._lock:
            self._base = fresh
        return fresh

    # -- redactor -------------------------------------------------------------

    def refresh(self, *, reload_base: bool = False, reset_external: bool = False,
                strict: bool = False) -> bool:
        """Install the newest merged table into the redactor.

        Sink calls are serialized and read the newest layers inside, so an
        older generation is never installed after a newer one. Returns False
        when no redactor is attached yet (nothing can print through it; the
        layers are installed by the integration when it attaches) or the sink
        failed; with ``strict`` a failing sink raises.
        """
        if reset_external:
            with self._lock:
                self._external = {}
        with self._sink_lock:
            with self._lock:
                sink = self._sink
            if sink is None:
                return False
            try:
                sink(self.merged(reload_base=reload_base))
            except Exception:
                if strict:
                    raise
                return False
            return True


def session_registry(session: Any) -> SecretRegistry | None:
    """The session's registry, when the integration created one."""
    registry = getattr(session, "secrets", None)
    return registry if isinstance(registry, SecretRegistry) else None


def session_secret_hooks(session: Any, owner: str):
    """``(register_secrets, refresh_secrets)`` for a config-transaction module.

    T-P2-31b (决定_第三波裁定 T-P2-31a 第 1 轮 #5, SHOULD-8/18): every module
    registers its draft values as a lease of the session's layered registry
    instead of pushing a whole table. ``register_secrets`` leases (raising
    when the redactor refresh fails, so the caller aborts before writing);
    ``refresh_secrets`` releases this module's leases and re-reads the saved
    config through ``session.refresh_secrets()``. A session without a
    registry keeps the P1-16 table-replacement path.
    """
    import threading
    leases: list[SecretLease] = []
    lock = threading.Lock()

    def register_secrets(values: Mapping[str, str]) -> None:
        registry = session_registry(session)
        if registry is not None:
            handle = registry.lease(owner, values)   # raises: nothing written
            if handle is not None:
                with lock:
                    leases.append(handle)
            return
        logs = getattr(session, "logs", None)
        if logs is None:
            return
        table = dict(session.access.known_secret_values())
        table.update(values)
        logs.refresh_known_secret_values(table)

    def refresh_secrets() -> None:
        registry = session_registry(session)
        with lock:
            held = list(leases)
            leases.clear()
        if registry is not None:
            for handle in held:
                registry.release(handle, refresh="none")
        if getattr(session, "logs", None) is not None:
            session.refresh_secrets()
        elif registry is not None and held:
            registry.refresh()

    return register_secrets, refresh_secrets
