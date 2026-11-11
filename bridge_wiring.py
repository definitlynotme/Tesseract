"""T-P2-31b: full module wiring of one BridgeSession.

``Wiring.register()`` calls every business module's ``register(session)`` in
dependency order (see ``REGISTRATION_ORDER``) and adds the owners the module
tickets left to integration:

* ``nodes.list`` / ``subscriptions.list`` (P2-06 NodeProjection on a single
  flight of the normal domain, never per tick), ``nodes.detail`` and the
  ``config.items`` list (ConfigItem IDs, bridge_runtime_access.config_item_id);
* ``killswitch.state`` (P2-20 RT-5 adapter), ``alerts.history`` (P2-21 RT-6
  adapter), ``logs.feed`` and the log queries/command (P2-18), ``geo.update`` /
  ``geo.status``, ``ui.language`` seed and the shell-only ``shutdown`` phases;
* controlled-write revisions: every bridge write that may change the saved
  configuration (mode, nodes, import, subscriptions, gateway) commits the
  persisted ``bridge_config_revision`` and records ``config.changed``
  (决定_第二波裁定 P2-10 #3); settings.apply does both itself (P2-10).

Threading:
* ``tick()`` runs on the session owner thread (``BridgeSession.pump``), at
  most every TICK_SECONDS, memory only: P2-22 ``poll()`` and the P2-26 update
  ``tick`` (both documented as tick-safe) plus cheap flight requests.
* ``sample(observe)`` runs on the 1 Hz SamplingWorker after the first
  subscription; it only calls cheap public getters and hands every heavier
  read (node projection, config items, kill switch manager lock) to a single
  flight on the normal domain.
* ``close(budget)`` is the ordered owner close of the full exit (bridge_shutdown).

``capabilities()`` is the v1 hello set: every registered command and query
(queries share ``req.cmd``) and every topic an owner here publishes.
"""

from __future__ import annotations

from collections import deque
from copy import deepcopy
from pathlib import Path
import logging
import os
import threading
import time
from typing import Any, Callable, Mapping

import bridge_protocol as protocol
from bridge_jobs import JobFailure, Pending


LOGGER = logging.getLogger("tesseract.bridge")

# P2-17 automatic exit lookups: the Fallback 10-06 ruling (T-P2-RT2) makes the
# bridge's automatic path read the published route snapshot only (0 x
# GET /proxies), which was the condition of 决定_第二波裁定 P2-17 #5 / RG #1.
# Flip to False to keep only the manual exit.verify (no automatic runner).
EXIT_AUTO_LOOKUP = True
TICK_SECONDS = 0.25
NODE_RESAMPLE_SECONDS = 10.0
CONFIG_ITEMS_RECHECK_SECONDS = 30.0
ALERT_HISTORY_LIMIT = 100
GEO_UPDATE_TIMEOUT_SECONDS = 600.0
OWNER_CLOSE_SECONDS = 3.0
# PROTOCOL §11 ``shutdown.phase`` in the order the full exit passes them.
_SHUTDOWN_PHASES = ("draining", "stopping", "recovering", "waiting_workers", "finished")

REGISTRATION_ORDER = (
    "queries", "catalog", "settings_apply", "sources", "settings", "node_commands",
    "nodes", "config_items", "import", "diagnostics", "exit", "update",
    "alerts_desktop", "alerts", "alerts_history", "killswitch", "exports",
    "logs", "geo", "language", "shutdown",
)

# Bridge writes that commit a config revision (family, files).
_WRITE_COMMANDS = {
    "mode.set": ("network", ("local",)),
    "nodes.update": ("nodes", ("local",)),
    "nodes.delete": ("nodes", ("local",)),
    "subscriptions.refresh": ("subscriptions", ()),
    "gateway.select": ("gateway", ("local",)),
    "import.apply": ("nodes", ("local",)),
}
_TIER_KEYS = ("vanguard", "sentinel", "bastion", "gateway")


def _ms() -> int:
    return time.time_ns() // 1_000_000


class Flight:
    """Single-flight normal-domain work: at most one queued/running, a request
    while it runs schedules exactly one rerun. A refused submission is
    retried by the next request."""

    def __init__(self, submit: Callable[[Callable[[], Any]], bool],
                 work: Callable[[], Any], name: str) -> None:
        self._submit, self._work, self.name = submit, work, name
        self._lock = threading.Lock()
        self._running = False
        self._dirty = False
        self.failures = 0

    def request(self) -> bool:
        with self._lock:
            if self._running:
                self._dirty = True
                return True
            self._running = True
            self._dirty = False
        try:
            submitted = bool(self._submit(self._run))
        except Exception:
            submitted = False
        if not submitted:
            with self._lock:
                self._running = False
                self._dirty = True
        return submitted

    @property
    def pending(self) -> bool:
        with self._lock:
            return self._dirty and not self._running

    def _run(self) -> None:
        while True:
            try:
                self._work()
            except Exception as exc:
                self.failures += 1
                LOGGER.warning("bridge wiring %s failed: %s", self.name, type(exc).__name__)
            with self._lock:
                if not self._dirty:
                    self._running = False
                    return
                self._dirty = False


class _RegisterTap:
    """Executor facade used only while a module registers: wraps the write
    commands' handlers (controlled-write revision); everything else delegates
    to the real executor (which the facade holds for good)."""

    def __init__(self, executor: Any, wrap: Callable[[str, Callable], Callable]) -> None:
        self._executor, self._wrap = executor, wrap

    def register(self, name: str, execute: Callable, **options: Any) -> None:
        self._executor.register(name, self._wrap(name, execute), **options)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._executor, name)


class Wiring:
    """All module owners of one BridgeSession (one epoch)."""

    def __init__(self, session: Any, *, scope_kind: str | None = None,
                 exit_auto_lookup: bool = EXIT_AUTO_LOOKUP) -> None:
        self.session = session
        self.scope_kind = scope_kind
        self.exit_auto_lookup = exit_auto_lookup
        self.owners: dict[str, Any] = {}
        self.topics: set[str] = set(protocol.P1_TOPICS)
        self.registered: list[str] = []
        self.failed: list[str] = []
        self._closed = False
        self._next_tick = 0.0
        self._last_route_revision: Any = None
        self._last_lifecycle_owner: Any = None
        self._node_due = 0.0
        self._config_items_due = 0.0
        self._feed_last: Any = None
        self._geo_last: Any = None
        self._ks_manager: Any = None
        self._ks_adapter: Any = None
        self._ks_last: Any = None
        # Last tiers.summary the 提醒 policy saw (kept across an owner change).
        self._notice_tiers: Any = None
        self._history_source: Any = None
        self._history_adapter: Any = None
        self._history_items: deque = deque(maxlen=ALERT_HISTORY_LIMIT)
        self._shutdown_phase: str | None = None
        self._flights: dict[str, Flight] = {}
        # The sampling worker, mutation workers and module listeners may all
        # create the same named flight first (T-P2-31b fix round 1).
        self._flights_lock = threading.Lock()
        self.readonly_topics: Any = None

    # ------------------------------------------------------------------ helpers

    @property
    def runtime(self) -> Any:
        return self.session.runtime

    @property
    def access(self) -> Any:
        return self.session.access

    @property
    def state(self) -> Any:
        return self.session.state

    def _submit(self, work: Callable[[], Any]) -> bool:
        return bool(self.session.executor.submit_normal(work))

    def _flight(self, name: str, work: Callable[[], Any]) -> Flight:
        with self._flights_lock:
            flight = self._flights.get(name)
            if flight is None:
                flight = self._flights[name] = Flight(self._submit, work, name)
            return flight

    def _subscribed(self, topic: str) -> bool:
        return topic in getattr(self.session, "subscribed_topics", ())

    def _record(self, topic: str, dto: dict[str, Any], *, edge: bool) -> None:
        if edge:
            self.state.record_edge(topic, dto)
        else:
            self.state.record_state(topic, dto)

    def _step(self, name: str, action: Callable[[], Any]) -> None:
        """One module. A failing step is logged and its capabilities stay
        undeclared (hello.caps lists only what this instance wired)."""
        try:
            action()
        except Exception as exc:
            self.failed.append(name)
            LOGGER.warning("bridge wiring step %s failed: %s", name, type(exc).__name__)
            return
        self.registered.append(name)

    # ------------------------------------------------------------- registration

    def register(self) -> "Wiring":
        """Every module in REGISTRATION_ORDER (dependencies first)."""
        steps = {
            "queries": self._queries, "catalog": self._catalog,
            "settings_apply": self._settings_apply, "sources": self._sources,
            "settings": self._settings, "node_commands": self._node_commands,
            "nodes": self._nodes, "config_items": self._config_items,
            "import": self._import, "diagnostics": self._diagnostics,
            "exit": self._exit, "update": self._update,
            "alerts_desktop": self._alerts_desktop, "alerts": self._alerts,
            "alerts_history": self._alerts_history, "killswitch": self._killswitch,
            "exports": self._exports, "logs": self._logs, "geo": self._geo,
            "language": self._language, "shutdown": self._shutdown,
        }
        for name in REGISTRATION_ORDER:
            self._step(name, steps[name])
        return self

    def _queries(self) -> None:
        from bridge_queries import ReadQueries
        from bridge_runtime_access import config_query_owners
        from bridge_tiers import SampledTopics
        from bridge_autostart import SidecarAutostartManager
        revision, pending = config_query_owners(self.access)
        try:  # literal attribute: the bridge gate forbids getattr on the runtime
            manager = self.runtime.autostart
        except AttributeError:
            manager = None
        queries = ReadQueries(self.access, revision=revision, pending=pending,
                              autostart_manager=manager if isinstance(
                                  manager, SidecarAutostartManager) else None)
        self.owners["queries"] = queries

        def app_info(args, context):
            data = queries.app_info(args, context)
            data["caps"] = sorted(self.capabilities()["commands"])
            return data
        self.session.register_query("app.info", app_info)
        self.session.register_query("autostart.state", queries.autostart_state)
        self.session.register_query("config.snapshot", queries.config_snapshot)
        self.session.register_query("geo.status", queries.geo_status)
        self.readonly_topics = SampledTopics(self.state, self.access,
            submit_normal=self.session.executor.submit_normal,
            config_revision=queries.revision)
        self.topics |= {"service.collect", "tier.transition", "protocols.mix",
                        "network.mode", "bastion.pool", "gateway.current"}

    def _catalog(self) -> None:
        import bridge_catalog
        self.owners["catalog"] = bridge_catalog.register(self.session)

    def _settings_apply(self) -> None:
        import bridge_settings_apply
        coordinator = bridge_settings_apply.register(self.session)
        self.owners["settings_apply"] = coordinator
        coordinator.add_listener(self._on_pending_change)

    def _sources(self) -> None:
        import bridge_sources
        saver = bridge_sources.register(self.session)
        self.owners["sources"] = saver
        self.topics.add("bastion.sources")
        self._flight("bastion.sources", saver.publish).request()

    def _settings(self) -> None:
        import bridge_settings
        tap = _RegisterTap(self.session.executor, self._settings_wrap)
        with self._tapped(tap):
            self.owners["settings"] = bridge_settings.register(
                self.session, sources=self.owners["sources"])
        self.topics |= {"config.changed", "ui.language"}

    def _node_commands(self) -> None:
        import bridge_node_commands
        tap = _RegisterTap(self.session.executor, self._write_wrap)
        with self._tapped(tap):
            commands = bridge_node_commands.register(self.session)
        self.owners["node_commands"] = commands
        commands.add_listener(lambda _family: self._nodes_changed())

    def _nodes(self) -> None:
        from bridge_nodes import NodeProjection
        commands = self.owners["node_commands"]
        self.owners["nodes"] = NodeProjection(self.access,
                                              refresh_owner=commands.refresh_owner_snapshot)
        self.owners["nodes_latest"] = None
        self.topics |= {"nodes.list", "subscriptions.list"}
        self.session.register_query("nodes.detail", self._nodes_detail)

    def _config_items(self) -> None:
        self.topics.add("config.items")
        self._flight("config.items", self._publish_config_items).request()

    def _import(self) -> None:
        import bridge_import
        tap = _RegisterTap(self.session.executor, self._write_wrap)
        with self._tapped(tap):
            self.owners["import"] = bridge_import.register(self.session)

    def _diagnostics(self) -> None:
        import bridge_diagnostics
        self.owners["diagnostics"] = bridge_diagnostics.register(self.session)

    def _exit(self) -> None:
        import bridge_exit
        # Without automatic lookups ui.presence stays unregistered and no
        # route is fed to the owner, so it never schedules one.
        owner = bridge_exit.register(self.session, presence=self.exit_auto_lookup)
        self.owners["exit"] = owner
        self.topics.add("exit.identity")

    def _update(self) -> None:
        import app_update_state
        import bridge_update
        scope_dir = getattr(getattr(self.session, "scope", None), "data_dir", None)
        data_dir = Path(scope_dir) if scope_dir is not None else Path(self.runtime.app_paths.data_dir)
        kwargs = {}
        if self.scope_kind != "default":
            # Development/audit scopes never reach the release endpoint.
            kwargs["updater"] = IsolatedUpdater()
        owner = bridge_update.UpdateOwner(app_update_state.update_state_path(data_dir),
                                          data_dir / "updates", **kwargs)
        bridge_update.register(self.session, owner)
        self.owners["update"] = owner
        self.topics |= {"update.state", "update.prepareExit"}
        if self.scope_kind == "default":
            owner.on_ready()

    def _alerts_desktop(self) -> None:
        import bridge_alerts_desktop
        self.owners["alerts_desktop"] = bridge_alerts_desktop.register(
            self.session, update_owner=self.owners["update"])
        self.topics.add("alerts.desktop")

    def _alerts(self) -> None:
        import bridge_alerts
        self.owners["alerts"] = bridge_alerts.register(
            self.session, desktop=self.owners["alerts_desktop"])

    def _alerts_history(self) -> None:
        self.topics.add("alerts.history")

    def _killswitch(self) -> None:
        self.topics.add("killswitch.state")

    def _exports(self) -> None:
        import bridge_exports
        targets = bridge_exports.WindowsHandleSaveTargets() if os.name == "nt" else None
        self.owners["exports"] = bridge_exports.register(self.session, targets=targets)

    def _logs(self) -> None:
        import bridge_logs
        logs = self.session.logs
        from bridge_logs import LogNotRunning
        # T-P2-H2 (C1-#1, C2 #12): the language source (key-event titles follow
        # ui.language) and the logs.feed.reason runtime facts.
        bridge_logs.register(self.session)
        from bridge_log_view import SnapshotRequired

        def page(method):
            def handler(args, context):
                try:
                    return method(args, context)
                except SnapshotRequired as exc:
                    raise JobFailure("snapshot_required") from exc
                except (ValueError, TypeError) as exc:
                    raise JobFailure("validation") from exc
            return handler

        def set_level(args, context):
            try:
                return logs.set_mihomo_level(args, context)
            except LogNotRunning as exc:
                raise JobFailure("not_running") from exc
            except OSError as exc:
                raise JobFailure("io") from exc

        self.session.register_query("logs.tail", page(logs.tail))
        self.session.register_query("logs.query", page(logs.query))
        self.session.register_command("logs.setMihomoLevel", set_level)
        self.topics.add("logs.feed")

    def _geo(self) -> None:
        self.session.register_command("geo.update", self._geo_update)
        self.topics.add("geo.status")

    def _language(self) -> None:
        self._flight("ui.language", self._seed_language).request()

    def _shutdown(self) -> None:
        self.topics.add("shutdown")

    def _tapped(self, tap: _RegisterTap):
        session = self.session

        class _Swap:
            def __enter__(self_inner):
                self_inner.real = session.executor
                session.executor = tap

            def __exit__(self_inner, *exc):
                session.executor = self_inner.real
                return False
        return _Swap()

    # ------------------------------------------------------------ capabilities

    def capabilities(self) -> dict[str, list[str]]:
        executor = self.session.executor
        commands = {name for name in protocol.HELLO_CAP_COMMANDS
                    if name in protocol.V1_COMMANDS and executor.is_registered(name)}
        queries = set(self.session.registered_queries())
        topics = {topic for topic in self.topics if topic in protocol.V1_TOPICS}
        return {"commands": sorted(commands | queries), "topics": sorted(topics), "features": []}

    # --------------------------------------------------- controlled-write revision

    def _write_wrap(self, name: str, execute: Callable) -> Callable:
        if name not in _WRITE_COMMANDS:
            return execute

        def handler(args, context):
            try:
                result = execute(args, context)
            except JobFailure as exc:
                self._config_written(name, exc.partial_result, failed=True)
                raise
            except Exception:
                self._config_written(name, None, failed=True)
                raise
            self._config_written(name, result, failed=False)
            return result
        return handler

    def _settings_wrap(self, name: str, execute: Callable) -> Callable:
        if name != "settings.apply":
            return execute

        def handler(args, context):
            try:
                return execute(args, context)
            finally:
                # settings.apply commits its own revision and config.changed;
                # only the derived lists are re-projected here.
                self._flight("config.items", self._publish_config_items).request()
        return handler

    @staticmethod
    def _wrote(name: str, result: Any) -> bool:
        """Whether the saved configuration may have changed (conservative:
        an unknown outcome counts as written)."""
        if not isinstance(result, Mapping):
            return True
        if name == "mode.set":
            return bool(result.get("changed")) or (result.get("saved") is True
                                                   and result.get("applied") is not True)
        if name in {"nodes.update", "nodes.delete", "import.apply"}:
            return result.get("saved") is True
        if name == "subscriptions.refresh":
            return result.get("stored") is True
        if name == "gateway.select":
            return (result.get("selection") is not None or result.get("applied") is True
                    or isinstance(result.get("apply_error"), Mapping))
        return False

    def _config_written(self, name: str, result: Any, *, failed: bool) -> None:
        try:
            if not self._wrote(name, result):
                return
            family, files = _WRITE_COMMANDS[name]
            revision = int(self.runtime.bridge_commit_config_revision())
            known = isinstance(result, Mapping)
            data = result if known else {}
            applied = data.get("applied") is True
            error = data.get("apply_error") if isinstance(data.get("apply_error"), Mapping) else None
            if error is None and isinstance(data.get("error"), Mapping):
                error = data["error"]
            effect = ("restart" if data.get("restarted") is True
                      else "reload" if applied else "none")
            section = {"section": family, "saved": True, "applied": applied,
                       "effect": effect, "error": deepcopy(error)}
            # An unknown outcome (no result, no partial facts) only advances
            # the revision: claiming saved=true would misstate it (fix round 1).
            dto = {"rev": revision, "files": list(files), "sections": [section] if known else [],
                   "pending": self._pending_fact(),
                   "external_detection": "bridge_writes_only"}
            if protocol.validate_topic_payload("config.changed", dto):
                section["error"] = None
            self.state.record_edge("config.changed", dto)
        except Exception as exc:
            LOGGER.warning("config.changed publication failed: %s", type(exc).__name__)
        finally:
            self._nodes_changed()
            self._flight("config.items", self._publish_config_items).request()

    def _pending_fact(self) -> Any:
        coordinator = self.owners.get("settings_apply")
        try:
            pending = coordinator.pending() if coordinator is not None else None
        except Exception:
            return None
        return deepcopy(pending) if isinstance(pending, Mapping) else None

    def _on_pending_change(self, pending: Any) -> None:
        """P2-11 deferred-apply intent changed: config.changed carries the new
        PendingApply and gateway.current re-reads its pending fact."""
        try:
            revision = int(self.runtime.bridge_config_revision())
            dto = {"rev": revision, "files": [], "sections": [],
                   "pending": deepcopy(pending) if isinstance(pending, Mapping) else None,
                   "external_detection": "bridge_writes_only"}
            if not protocol.validate_topic_payload("config.changed", dto):
                self.state.record_edge("config.changed", dto)
        except Exception as exc:
            LOGGER.warning("pending publication failed: %s", type(exc).__name__)
        if self.readonly_topics is not None:
            try:
                self.readonly_topics.invalidate("gateway.current")
            except Exception:
                pass

    # ------------------------------------------------------------- node lists

    def _nodes_changed(self) -> None:
        self._node_due = 0.0
        if self._subscribed("nodes.list") or self._subscribed("subscriptions.list"):
            self._flight("nodes", self._project_nodes).request()

    def _project_nodes(self) -> None:
        from bridge_nodes import IncompleteNodeSample, StaleNodeSample
        projection = self.owners["nodes"]
        try:
            result = projection.sample()
        except (StaleNodeSample, IncompleteNodeSample):
            self._node_due = 0.0   # retried at the next sample tick
            return
        self.owners["nodes_latest"] = result
        groups: dict[str, list] = {key: [] for key in _TIER_KEYS}
        for item in result["nodes"]["items"]:
            groups.setdefault(item["tier"], []).append(item)
        for key in _TIER_KEYS:
            self.state.record_list("nodes.list", groups[key], key=key)
        if result.get("subscriptions") is not None:
            subs: dict[str, list] = {key: [] for key in _TIER_KEYS}
            for item in result["subscriptions"]["items"]:
                subs.setdefault(item["tier"], []).append(item)
            for key in _TIER_KEYS:
                self.state.record_list("subscriptions.list", subs[key], key=key)

    def _nodes_detail(self, args: Mapping[str, Any], context: Any) -> dict[str, Any]:
        from bridge_vocab import PROTOCOL_TIERS
        node_id = args.get("node_id")
        latest = self.owners.get("nodes_latest")
        if latest is None:
            from bridge_nodes import IncompleteNodeSample, StaleNodeSample
            try:
                latest = self.owners["nodes"].sample()
            except (StaleNodeSample, IncompleteNodeSample) as exc:
                raise JobFailure("snapshot_required") from exc
            self.owners["nodes_latest"] = latest
        node = next((deepcopy(item) for item in latest["nodes"]["items"]
                     if item["id"] == node_id), None)
        if node is None:
            raise JobFailure("conflict")
        runtime_tier = PROTOCOL_TIERS.get(node["tier"], "")
        config_item = None
        commands = self.owners.get("node_commands")
        if commands is not None and node["kind"] == "manual":
            try:
                resolved = commands.resolve(node_id, need_config=True)
                config_item = config_item_dto(resolved.config_item)
            except Exception:
                config_item = None
        diagnostics = self.owners.get("diagnostics")
        last = None
        if diagnostics is not None:
            try:
                last = diagnostics.last_diagnostic(node_id)
            except Exception:
                last = None
        data = {"node": node, "config_item": config_item,
                "last_diagnostic": deepcopy(last) if isinstance(last, Mapping) else None,
                "check_interval_s": self._check_interval(runtime_tier)}
        # T-P4-C2 #3/#5 (K-1, K-3), wired by T-P2-H2: masked endpoint and the
        # protocol chain; unknown/stale/frozen runtime -> {} (fields omitted).
        from bridge_nodes import node_detail_extras
        data.update(node_detail_extras(self.access, node_id))
        return data

    def _check_interval(self, runtime_tier: str) -> int:
        """T-P2-H2 (决定_第五波裁定 T-P4-C2 #3): the Manager's own rule
        (``_tier_interval``), as tiers.summary.check_interval_s uses."""
        from config_utils import coerce_positive_int
        from runtime_settings import tier_block
        try:
            config = self.runtime.config
        except Exception:
            config = None
        return coerce_positive_int(
            tier_block(config if isinstance(config, dict) else None, runtime_tier).get("check_interval", 60),
            60)

    def _publish_config_items(self) -> None:
        items = config_items_projection(self.runtime.configured_items())
        self.state.record_list("config.items", items)

    # ---------------------------------------------------------------- language

    def _seed_language(self) -> None:
        """ui.language snapshot (config_owner): the configured language;
        later changes are recorded by bridge_settings as edges."""
        raw = self.access.app_info()
        language = str(dict(raw).get("language") or "")
        if language not in _LANGUAGES:
            return
        self.state.record_state("ui.language", {
            "language": language, "config_rev": int(self.runtime.bridge_config_revision())})

    # ---------------------------------------------------------------- geo

    def _geo_dto(self) -> dict[str, Any] | None:
        queries = self.owners.get("queries")
        try:
            return queries.geo_status({}, None)
        except Exception:
            return None

    def _geo_update(self, args: Mapping[str, Any], context: Any) -> Any:
        """geo.update (normal, job/queued): the download runs on its own
        thread; the job waits through deferred completion (no worker)."""
        if not self._geo_enabled():
            # T-P2-H2 (决定_第五波裁定 31b-#18): Geo updates are disabled in the
            # configuration; the updater would return without downloading, so
            # this is reported as is, never as a completed update.
            status = self._geo_dto() or {"running": False, "updated_at_ms": None, "errors": []}
            raise JobFailure("conflict", {"triggered": False, "completed": False, "status": status},
                             error={"code": "conflict", "message_key": "errors.geo.disabled",
                                    "params": {}, "reason_code": "disabled"})
        pending = Pending(timeout=GEO_UPDATE_TIMEOUT_SECONDS)

        def run() -> None:
            status = self._geo_dto()
            try:
                self.runtime.trigger_geo_update(force=True)
            except RuntimeError as exc:
                status = self._geo_dto() or {"running": False, "updated_at_ms": None, "errors": []}
                code = "needs_route" if "route" in str(exc).lower() else "io"
                pending.fail(JobFailure(code, {"triggered": False, "completed": False,
                                               "status": status}))
                return
            except Exception:
                status = self._geo_dto() or {"running": False, "updated_at_ms": None, "errors": []}
                pending.fail(JobFailure("io", {"triggered": True, "completed": False,
                                               "status": status}))
                return
            status = self._geo_dto() or {"running": False, "updated_at_ms": None, "errors": []}
            result = {"triggered": True, "completed": not status["running"], "status": status}
            self._sample_geo()
            if status["errors"]:
                pending.fail(JobFailure("io", result))
            elif not result["completed"]:
                # update_now gave up waiting for the action lock (another
                # update still runs): done requires completed=true.
                pending.fail(JobFailure("timeout", result))
            else:
                pending.complete(result)

        threading.Thread(target=run, name="bridge-geo-update", daemon=True).start()
        return pending

    def _geo_enabled(self) -> bool:
        """``geo_update.enabled`` of the loaded configuration (default true,
        the updater's own rule: geo_updater.GeoUpdater.reconfigure)."""
        from config_utils import coerce_bool
        from runtime_settings import geo_update_block
        try:
            config = self.runtime.config
        except Exception:
            return True
        block = geo_update_block(config if isinstance(config, dict) else None)
        return coerce_bool(block.get("enabled", True), True)

    def _sample_geo(self) -> None:
        dto = self._geo_dto()
        if dto is None or dto == self._geo_last:
            return
        self._record("geo.status", dto, edge=self._geo_last is not None)
        self._geo_last = deepcopy(dto)

    # --------------------------------------------------------- kill switch

    def _sample_killswitch(self) -> None:
        """RT-5 (P2-20): one adapter per manager lifetime; edges in order,
        a source gap withdraws the baseline (gap) and reseeds a snapshot.
        Without a manager (service stopped) the configured switch is shown
        inactive."""
        from bridge_killswitch import KillSwitchAdapter
        try:
            manager = self.runtime.kill_switch
        except AttributeError:
            manager = None
        if manager is not self._ks_manager:
            self._ks_manager = manager
            self._ks_adapter = KillSwitchAdapter(manager) if manager is not None else None
            if self._ks_adapter is not None:
                frame = self._ks_adapter.snapshot()
                self._emit_ks(frame["data"], snapshot=True)
                return
        if self._ks_adapter is None:
            self._emit_ks(self._configured_killswitch(), snapshot=False)
            return
        for frame in self._ks_adapter.drain():
            if frame["kind"] == "gap":
                self.state.invalidate_state("killswitch.state", notify=True)
                self._ks_last = None
            elif frame["kind"] == "snapshot":
                self._emit_ks(frame["data"], snapshot=True)
            else:
                self._emit_ks(frame["data"], snapshot=False)

    def _configured_killswitch(self) -> dict[str, Any]:
        from config_utils import coerce_bool
        try:
            block = self.runtime.config.get("killswitch", {})
            enabled = coerce_bool(block.get("enabled", False), False) if isinstance(block, Mapping) else False
        except Exception:
            enabled = False
        try:
            admin = bool(dict(self.access.network_mode()).get("admin"))
        except Exception:
            admin = False
        return {"enabled": enabled, "active": False, "admin": admin, "error": None}

    def _emit_ks(self, dto: dict[str, Any], *, snapshot: bool) -> None:
        if dto == self._ks_last and not snapshot:
            return
        self._record("killswitch.state", dto, edge=self._ks_last is not None and not snapshot)
        self._ks_last = deepcopy(dto)

    # ---------------------------------------------------------- 提醒 policy

    def _sample_notice(self) -> None:
        """提醒 (plan §10.2): what the overview shows, once a second, to the
        desktop-notice owner's policy. Public getters only: the lifecycle and
        tiers samples, the last published kill switch DTO and the HKCU proxy
        drift of ``network.mode`` (read at most every 5 s by the runtime)."""
        owner = self.owners.get("alerts_desktop")
        if owner is None:
            return
        from alert_policy import facts_from
        raw = self.access.tiers_summary_status()
        if raw is not None:
            self._notice_tiers = raw[1]
        owner.observe(facts_from(self.access.lifecycle_status(), self._notice_tiers,
                                 self._ks_last, self.access.network_mode()))

    # ---------------------------------------------------------- alert history

    def _sample_history(self) -> None:
        """RT-6 (P2-21): snapshot then ordered increments; an edge carries the
        complete DTO (the retained recent list) as PROTOCOL §4 requires."""
        from bridge_alerts_history import AlertsHistoryAdapter
        try:
            source = self.runtime.alerts
        except AttributeError:
            source = None
        if source is None:
            return
        if source is not self._history_source:
            self._history_source = source
            self._history_adapter = AlertsHistoryAdapter(source)
            frame = self._history_adapter.snapshot()
            self._history_items = deque(frame["data"]["items"][-ALERT_HISTORY_LIMIT:],
                                        maxlen=ALERT_HISTORY_LIMIT)
            self.state.record_state("alerts.history", {
                "items": list(self._history_items), "last_seq": frame["data"]["last_seq"]})
            return
        for frame in self._history_adapter.poll():
            if frame["kind"] == "gap":
                self.state.invalidate_state("alerts.history", notify=True)
            elif frame["kind"] == "snapshot":
                self._history_items = deque(frame["data"]["items"][-ALERT_HISTORY_LIMIT:],
                                            maxlen=ALERT_HISTORY_LIMIT)
                self.state.record_state("alerts.history", {
                    "items": list(self._history_items), "last_seq": frame["data"]["last_seq"]})
            else:
                self._history_items.extend(frame["data"]["items"])
                self.state.record_edge("alerts.history", {
                    "items": list(self._history_items), "last_seq": frame["data"]["last_seq"]})

    # -------------------------------------------------------------- log feed

    def _sample_feed(self) -> None:
        logs = getattr(self.session, "logs", None)
        if logs is None:
            return
        dto = logs.feed()
        semantic = {key: value for key, value in dto.items() if key != "updated_at_ms"}
        if semantic == self._feed_last:
            return
        self._record("logs.feed", dto, edge=self._feed_last is not None)
        self._feed_last = deepcopy(semantic)

    # ------------------------------------------------------------- shutdown

    def shutdown_phase(self, phase: str, recovery: Any = None, *,
                       workers_pending: bool = True) -> None:
        """Shell-only ``shutdown`` state (bridge_shutdown calls this).

        Phases only move forward: ``finish_quit`` is retried every 0.1 s and
        reports ``draining`` before it learns the stop job is still running,
        which must not alternate draining/stopping edges."""
        rank = _SHUTDOWN_PHASES.index(phase) if phase in _SHUTDOWN_PHASES else -1
        current = (_SHUTDOWN_PHASES.index(self._shutdown_phase)
                   if self._shutdown_phase in _SHUTDOWN_PHASES else -1)
        if rank < 0 or rank <= current:
            return
        report = {}
        if isinstance(recovery, Mapping):
            report = recovery
        elif recovery is not None:
            try:
                report = dict(recovery)
            except Exception:
                report = {}
        dto = {"phase": phase, "recovery": {
            domain: (report.get(domain) or {}).get("state", "unknown")
            if isinstance(report.get(domain), Mapping) else "unknown"
            for domain in ("proxy", "firewall")}, "workers_pending": bool(workers_pending)}
        try:
            self._record("shutdown", dto, edge=self._shutdown_phase is not None)
            self._shutdown_phase = phase
        except Exception as exc:
            LOGGER.warning("shutdown publication failed: %s", type(exc).__name__)

    # ------------------------------------------------------------ periodic

    def tick(self) -> None:
        """Owner thread (BridgeSession.pump); memory-only work."""
        if self._closed:
            return
        now = time.monotonic()
        if now < self._next_tick:
            return
        self._next_tick = now + TICK_SECONDS
        desktop = self.owners.get("alerts_desktop")
        if desktop is not None:
            try:
                desktop.poll()
            except Exception as exc:
                LOGGER.warning("desktop alert poll failed: %s", type(exc).__name__)
        update = self.owners.get("update")
        if update is not None:
            try:
                update.tick(self.state.record_state, self.session.executor.submit_normal)
            except Exception as exc:
                LOGGER.warning("update tick failed: %s", type(exc).__name__)
        for flight in list(self._flights.values()):
            if flight.pending:
                flight.request()

    def sample(self, observe: Callable[[str, Callable[[], Any]], Any]) -> None:
        """SamplingWorker thread (1 Hz after the first subscription)."""
        if self._closed:
            return
        if self.readonly_topics is not None:
            self.readonly_topics.sample(observe)
        observe("p2.exit", self._sample_exit)
        observe("p2.killswitch", lambda: self._flight("killswitch", self._sample_killswitch).request())
        observe("p2.alerts.notice", self._sample_notice)
        observe("p2.alerts.history", self._sample_history)
        observe("p2.logs.feed", self._sample_feed)
        observe("p2.geo.status", self._sample_geo)
        now = time.monotonic()
        if ((self._subscribed("nodes.list") or self._subscribed("subscriptions.list"))
                and now >= self._node_due):
            self._node_due = now + NODE_RESAMPLE_SECONDS
            self._flight("nodes", self._project_nodes).request()
        if self._subscribed("config.items") and now >= self._config_items_due:
            self._config_items_due = now + CONFIG_ITEMS_RECHECK_SECONDS
            self._flight("config.items", self._publish_config_items).request()

    def _sample_exit(self) -> None:
        owner = self.owners.get("exit")
        if owner is None:
            return
        lifecycle = self.access.lifecycle_status()
        owner.notify_lifecycle(lifecycle)
        key = (lifecycle["intent"]["runtime_generation"], lifecycle["core"]["core_generation"],
               lifecycle["intent"].get("stop_requested"))
        if key != self._last_lifecycle_owner:
            self._last_lifecycle_owner = key
            self._nodes_changed()
        if not self.exit_auto_lookup:
            return
        route = dict(self.access.published_route_status())
        revision = (route.get("publication_revision"), route.get("valid"))
        if revision != self._last_route_revision:
            self._last_route_revision = revision
            owner.observe_route(route)

    # ---------------------------------------------------------------- close

    def close(self, budget: float = OWNER_CLOSE_SECONDS) -> bool:
        """Ordered business-owner close of the full exit (bridge_shutdown
        step 3). Bounded; every step runs even if an earlier one fails.
        Returns False when a bounded wait (node diagnostics, deferred apply)
        did not finish in time."""
        deadline = time.monotonic() + max(0.0, budget)
        remaining = lambda: max(0.0, deadline - time.monotonic())
        self._closed = True
        ok = True

        def guarded(name: str, action: Callable[[], Any]) -> None:
            nonlocal ok
            try:
                if action() is False:
                    ok = False
            except Exception as exc:
                ok = False
                LOGGER.warning("owner close %s failed: %s", name, type(exc).__name__)

        runtime = self.runtime
        guarded("node_diagnostics", lambda: runtime.close_node_diagnostics(
            timeout=min(3.0, remaining())))
        guarded("subscription_diagnostics", lambda: runtime.cancel_subscription_diagnostics() and None)
        exit_owner = self.owners.get("exit")
        if exit_owner is not None:
            guarded("exit", lambda: exit_owner.observe_presence(
                {"visible": False, "focused": False, "page": "none"}) and None)
        desktop = self.owners.get("alerts_desktop")
        if desktop is not None and callable(getattr(desktop, "close", None)):
            guarded("alerts_desktop", desktop.close)
        update = self.owners.get("update")
        if update is not None:
            guarded("update_handoff", lambda: self._close_update_handoff(update))
        exports = self.owners.get("exports")
        if exports is not None:
            guarded("exports", exports.close)
        coordinator = self.owners.get("settings_apply")
        if coordinator is not None:
            guarded("settings_apply", lambda: coordinator.close(timeout=min(1.0, remaining())))
        return ok

    def _close_update_handoff(self, update: Any) -> None:
        """No new update.prepareExit after quit: an install that verifies
        during the exit keeps its receipt (verified, io) instead of offering
        a hand-off the quitting shell would only ignore. A hand-off already
        accepted by the shell (this exit is the update quit) is untouched;
        its exit observation keeps reading the same coordinator."""
        import bridge_update
        update.attach_handoff(_ClosedHandoffPublisher(),
                              bridge_update.ShutdownExitAdapter(lambda: self.session.shutdown))


class _ClosedHandoffPublisher:
    """HandoffPublisher of a closed session (full exit step 3)."""

    def publish_prepare_exit(self, data: dict[str, Any]) -> None:
        raise RuntimeError("update hand-off closed by the full exit")


class IsolatedUpdater:
    """Updater of a development/audit scope: never contacts the release
    endpoint (tests and smoke runs must not touch the network). Checks and
    downloads fail ``io`` with a fixed reason."""

    def check_latest_update(self, *, current_version: str) -> Any:
        import app_updater
        raise app_updater.AppUpdateError("update checks are disabled in an isolated scope")

    def download_update(self, info: Any, download_dir: Any) -> Any:
        import app_updater
        raise app_updater.AppUpdateError("update downloads are disabled in an isolated scope")


_LANGUAGES = frozenset(("zh_CN", "en_US", "ja_JP", "ko_KR", "fr_FR", "de_DE",
                        "es_ES", "pt_BR", "ru_RU"))


def config_item_dto(item: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """PROTOCOL §16 ConfigItem of one configured item (config_store row)."""
    from bridge_runtime_access import config_item_id
    from bridge_vocab import RUNTIME_TIERS
    if not isinstance(item, Mapping):
        return None
    tier = str(item.get("tier") or "")
    kind = str(item.get("kind") or "")
    if tier not in RUNTIME_TIERS or kind not in {"node", "subscription"}:
        return None
    if kind == "subscription":
        secret = bool(str(item.get("url") or "").strip())
    elif tier == "tier1":
        secret = any(str(item.get(key) or "").strip()
                     for key in ("vless_link", "hysteria2_link", "naive_link"))
    else:
        secret = bool(str(item.get("uri") or "").strip())
    label = item.get("label") if kind == "subscription" else item.get("name") or item.get("label")
    name = str(label or "").strip()
    dto = {"id": config_item_id(item), "name": name or kind, "kind": kind,
           "section": RUNTIME_TIERS[tier], "enabled": item.get("enabled", True) is not False,
           "secret_set": secret}
    if "://" in name or protocol.validate_topic_payload("config.items", {"items": [dto]}):
        dto["name"] = kind   # never a URL/credential-looking label on the wire
    return dto


def config_items_projection(items: Any) -> list[dict[str, Any]]:
    """config.items: user-configured nodes and subscriptions (Bastion sources
    are bastion.sources). Duplicate IDs (identical entries) appear once; the
    commands treat such an ID as ambiguous (conflict)."""
    result, seen = [], set()
    for item in items or ():
        dto = config_item_dto(item)
        if dto is None or dto["id"] in seen:
            continue
        seen.add(dto["id"])
        result.append(dto)
    return result
