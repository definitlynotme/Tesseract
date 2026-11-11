"""P1 sidecar bootstrap. Runtime and pipe integrations are intentionally injectable."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import uuid
import threading
import queue
from copy import deepcopy
from collections import deque
from dataclasses import dataclass
from itertools import count
from pathlib import Path
from typing import Callable, Protocol

import bridge_instance_scope as instance_scope
import bridge_cli
import bridge_protocol as protocol
from app_info import APP_VERSION


class Transport(Protocol):
    def send(self, frame: dict) -> None: ...
    def receive(self) -> dict | None: ...
    def close(self) -> None: ...


class StartupError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class SingleInstanceRejected(StartupError):
    pass


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="tesseract-runtime")
    parser.add_argument("--lang", default="en_US")
    parser.add_argument("--ipc-handle", "--pipe-handle", dest="ipc_handle", type=int)
    parser.add_argument("--shell-pid", type=int)
    parser.add_argument("--stdio", action="store_true")
    return bridge_cli.parse_runtime_args(parser, argv)


def is_helper_argv(argv: list[str]) -> bool:
    return any(arg == "--multiprocessing-fork" or arg.startswith("--multiprocessing-fork=")
               or arg == "--validator-helper-smoke" or arg.startswith("--validator-helper-smoke=")
               or arg == "--validator-helper-slots" or arg.startswith("--validator-helper-slots=")
               for arg in argv)


def dispatch_helper(argv: list[str]) -> int:
    """Frozen fork workers are diverted by freeze_support before import."""
    if any(arg == "--multiprocessing-fork" or arg.startswith("--multiprocessing-fork=")
           for arg in argv):
        raise StartupError("unexpected_fork_dispatch")
    parser = argparse.ArgumentParser(prog="tesseract-runtime helper smoke")
    parser.add_argument("--validator-helper-smoke", nargs="?", const=1, type=int,
                        choices=range(1, 17))
    parser.add_argument("--validator-helper-slots", type=int, default=1,
                        choices=range(1, 17))
    args = parser.parse_args(argv)
    if args.validator_helper_smoke is None:
        raise StartupError("helper_smoke_required")
    # Existing smoke needs a writable data directory. Never create one here.
    from app_paths import build_app_paths
    from desktop_app_shell import run_validator_helper_smoke

    paths = build_app_paths()
    if not paths.data_dir.is_dir():
        raise StartupError("helper_data_dir_missing")
    # TODO(P1-15): verify this path in the sidecar-only frozen build.
    return run_validator_helper_smoke(
        paths, worker_count=args.validator_helper_smoke,
        slots_per_process=args.validator_helper_slots,
    )


def unknown_recovery() -> dict:
    return {name: {"state": "unknown", "error_code": None, "remaining_count": None}
            for name in ("proxy", "firewall")}


def attach_audit_guard(scope):
    from bridge_audit import attach_audit_guard as attach
    return attach(scope)


def prepare_startup_audit(scope, guard):
    """T-P2-32: None unless TESSERACT_STARTUP_AUDIT_*/manifest; raises = refuse."""
    from bridge_audit import prepare_startup_audit as prepare
    return prepare(scope, guard)


def recover_network(scope, deadline_seconds=2.0):
    from runtime_network_recovery import recover_network_after_unclean_exit
    from runtime_state import RuntimeStateStore
    started = time.monotonic()
    diagnostics: dict = {}
    report = recover_network_after_unclean_exit(
        RuntimeStateStore(scope.data_dir / "runtime-state.json"), scope=scope,
        deadline_seconds=deadline_seconds, diagnostics=diagnostics)
    # The hello carries only the enums; keep the startup reading in bridge.log
    # so a "not confirmed" banner can be traced to its cause (11-10).
    try:
        firewall_diag = diagnostics.get("firewall") or {}
        logging.getLogger("tesseract.bridge").info(
            "Startup network recovery: proxy=%s proxy_error=%s firewall=%s "
            "firewall_error=%s firewall_detail=%s elapsed_ms=%d",
            report["proxy"].get("state"), report["proxy"].get("error_code") or "-",
            report["firewall"].get("state"), report["firewall"].get("error_code") or "-",
            firewall_diag.get("inspection_error_type")
            or firewall_diag.get("verification_error_type") or "-",
            int((time.monotonic() - started) * 1000))
    except Exception:
        pass
    return report


def open_transport(args):
    if args.stdio and args.ipc_handle is None and args.shell_pid is None:
        from bridge_cli import StdioTransport
        return StdioTransport(sys.stdin.buffer, sys.stdout.buffer)
    if args.ipc_handle is None or args.shell_pid is None or args.stdio:
        raise StartupError("transport_arguments_required")
    from bridge_transport import PipeTransport
    return PipeTransport(args.ipc_handle, args.shell_pid)


def read_loop(transport, epoch):
    """Legacy injected CLI fixture; production runs BridgeSession.run instead."""
    while True:
        frame = transport.receive()
        if frame is None:
            return
        if protocol.validate_frame(frame) or frame.get("epoch") != epoch:
            continue
        now = time.time_ns() // 1_000_000
        if frame["t"] == "ping":
            transport.send(dict(t="pong", epoch=epoch, produced_at_ms=now,
                                sent_at_ms=now, n=frame["n"]))
        elif frame["t"] in ("subscribe", "req", "cancel"):
            error = dict(code="runtime_unavailable", message_key="errors.runtime_unavailable", params={})
            reply = dict(t="ack", accepted=False, error=error) if frame["t"] == "cancel" else dict(t="error", error=error)
            transport.send(dict(reply, epoch=epoch, produced_at_ms=now, sent_at_ms=now, id=frame["id"]))


def prepare_first_run(paths, *, language=None) -> bool:
    """T-P4-C1 #6 (D4): a missing config.yaml is the first run.

    Decided BEFORE app_paths copies config.example.yaml (whose template says
    ``ui.language: en_US``); then the user files are created exactly as Qt's
    startup creates them, and the system UI language (GetUserDefaultUILanguage,
    the shell's LANGID mapping) is recorded in config.local.yaml, the file
    settings.apply{ui.language} writes. An existing configuration is never
    touched here (Qt keeps its own startup; D15). A failed language write
    leaves the template language and never blocks startup. Returns whether
    this was a first run.
    """
    if paths.config_path.exists():
        return False
    # A config.local.yaml that survived (base file deleted) holds user choices;
    # one created now by app_paths holds the template default only.
    local_existed = paths.local_config_path.exists()
    from app_paths import bootstrap_user_files
    from bridge_settings import first_run_language, write_first_run_language
    bootstrap_user_files(paths)
    try:
        write_first_run_language(paths, language or first_run_language(),
                                 template=not local_existed)
    except Exception as exc:
        logging.getLogger("tesseract.bridge").warning(
            "first-run language not recorded: %s", type(exc).__name__)
    return True


def enforce_scope_config(scope, config):
    """In-memory forced values of a scope that may not take over the host.

    The custom scope is a development scope, never a host capture mode:
    system_proxy network mode, system proxy and kill switch off. Applied to
    the loaded configuration on construction and after every reload
    (SidecarRuntime._disable_external_desktop_notifications); never written
    to disk. Other scopes are returned unchanged.
    """
    if (getattr(scope, "may_take_over_host_network", False)
            or getattr(scope, "kind", None) != "custom" or not isinstance(config, dict)):
        return config
    client = config.setdefault("client", {})
    if not isinstance(client, dict):
        client = config["client"] = {}
    client["network_mode"] = "system_proxy"
    proxy = client.setdefault("system_proxy", {})
    if not isinstance(proxy, dict):
        proxy = client["system_proxy"] = {}
    proxy["enabled"] = False
    killswitch = config.setdefault("killswitch", {})
    if not isinstance(killswitch, dict):
        killswitch = config["killswitch"] = {}
    killswitch["enabled"] = False
    return config


def create_runtime(scope, gate, on_lifecycle):
    """Construct only after the original RT-1 gate, with scope-local paths.

    The isolated adapter blocks legacy OS writers too (disabled flags alone do
    not block legacy stale-rule cleanup). No default/Qt module is changed.
    """
    gate.require_idle()
    from app_paths import build_app_paths, apply_release_path_overrides, bootstrap_user_files
    from main import load_config
    import desktop_runtime as module
    paths = build_app_paths(config_dir=scope.config_dir, data_dir=scope.data_dir)
    # T-P2-H2 (33-#10): the first-run decision and its language write stay
    # first; then every start bootstraps/migrates the user files before
    # load_config, as Qt's startup does (a first run already did it).
    if not prepare_first_run(paths):
        bootstrap_user_files(paths)
    config = apply_release_path_overrides(load_config(str(paths.config_path), str(paths.local_config_path)), paths)
    # T-P2-31b (P2-20): the sidecar's kill switch managers keep a bounded RT-5
    # event ring for killswitch.state; Qt keeps the ring-less default.
    if not getattr(module.KillSwitchManager, "_bridge_event_ring", False):
        base_kill_switch = module.KillSwitchManager

        class EventKillSwitch(base_kill_switch):
            _bridge_event_ring = True

            def __init__(self, *args, event_capacity: int = KILLSWITCH_EVENT_CAPACITY, **kwargs):
                super().__init__(*args, event_capacity=event_capacity, **kwargs)
        module.KillSwitchManager = EventKillSwitch
    if not scope.may_take_over_host_network:
        enforce_scope_config(scope, config)
        class IsolatedKillSwitch(module.KillSwitchManager):
            def _cleanup_rules(self, **kwargs):
                return None
            def _ensure_supported(self):
                return False
        # Process-local adapter; audit has its stronger manifest/socket guard.
        module.KillSwitchManager = IsolatedKillSwitch

    revision = ConfigRevisionStore(Path(scope.data_dir) / CONFIG_REVISION_FILE)

    class SidecarRuntime(module.DesktopRuntime):
        _bridge_start_active = False

        def _disable_external_desktop_notifications(self, config):
            # T-P2-H2 (决定_第五波裁定 33-#17): the one load point of both
            # DesktopRuntime.__init__ and every reload_config. Re-impose the
            # scope's forced values after each (re)load from disk, so a
            # tampered or re-bootstrapped sandbox file never re-enables a host
            # capture mode for the custom scope.
            return enforce_scope_config(scope, super()._disable_external_desktop_notifications(config))

        # T-P2-31b (决定_第二波裁定 P2-10 #3): the bridge-controlled config write
        # revision survives a new runtime instance / sidecar epoch (persisted in
        # the scope's data directory); the runtime's in-memory counter is not
        # used by the sidecar.
        def bridge_config_revision(self):
            return revision.current()

        def bridge_commit_config_revision(self):
            return revision.commit()

        def lifecycle_status(self):
            from bridge_runtime_access import frozen_record
            raw = super().lifecycle_status()
            # The sidecar owns this actual transaction-active fact. Both its
            # edges and 1 Hz sampler use this one public projection.
            if (self._bridge_start_active and raw["intent"]["desired_running"]
                    and not raw["intent"]["stop_requested"]
                    and raw["runtime"]["startup_phase"] == "stopped"):
                raw = dict(raw)
                raw["runtime"] = frozen_record(**dict(raw["runtime"], startup_phase="collecting"))
            return raw

        def _start_locked(self, **kwargs):
            gate.require_idle()
            self._bridge_start_active = True
            try:
                on_lifecycle(self.lifecycle_status())
                return super()._start_locked(**kwargs)
            finally:
                self._bridge_start_active = False

        def request_stop(self, **kwargs):
            intent = super().request_stop(**kwargs)
            if intent.accepted:
                # This runs on the control worker after the real intent, before
                # lifecycle teardown. Preserve stopping even when stop is fast.
                on_lifecycle(self.lifecycle_status())
            return intent

        def recover_stale_system_proxy(self):
            gate.require_idle()
            if scope.may_take_over_host_network:
                return super().recover_stale_system_proxy()
            return False

        def enable_system_proxy(self):
            gate.require_idle()
            if scope.may_take_over_host_network:
                return super().enable_system_proxy()
            return False

        def restore_system_proxy(self):
            gate.require_idle()
            if scope.may_take_over_host_network:
                return super().restore_system_proxy()
            return False

    gate.require_idle()
    # T-P2-31b (P2-28): the sidecar's own shell-targeted autostart manager,
    # created before the runtime and injected (Qt keeps its default one).
    from bridge_autostart import isolated_autostart_manager, sidecar_autostart_manager
    autostart_manager = sidecar_autostart_manager()
    if not scope.may_take_over_host_network:
        # T-P2-31b fix round 1: the Run value belongs to the installed app;
        # a custom scope reads it but never writes or removes it.
        autostart_manager = isolated_autostart_manager(autostart_manager)
    runtime = SidecarRuntime(config, app_paths=paths, autostart_manager=autostart_manager)
    if not scope.may_take_over_host_network:
        # An isolated RT-1 deliberately skipped this journal. Legacy stop must
        # not erase recovery evidence that this scope never restored.
        runtime.runtime_state.clear = lambda: None
    return runtime


KILLSWITCH_EVENT_CAPACITY = 64
CONFIG_REVISION_FILE = "bridge-config-revision.json"
# T-P2-31b (决定_第三波裁定 T-P2-31a 第 1 轮 #3): inline control commands
# (alerts.desktopDelivered) have their own small handoff queue; overflow is
# answered job_capacity and never a session fault.
INLINE_QUEUE_CAPACITY = 64


class ConfigRevisionStore:
    """Persisted bridge config write revision (one JSON integer file).

    Read once; every commit advances it and replaces the file atomically. A
    failed write keeps the in-memory value (still monotonic in this process)
    and is retried by the next commit."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._value = self._read()

    def _read(self) -> int:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8")).get("rev")
        except (OSError, ValueError, AttributeError):
            return 0
        return value if type(value) is int and value >= 0 else 0

    def current(self) -> int:
        with self._lock:
            return self._value

    def commit(self) -> int:
        with self._lock:
            self._value += 1
            temporary = self.path.with_name(self.path.name + ".tmp")
            try:
                temporary.write_text(json.dumps({"rev": self._value}), encoding="utf-8")
                os.replace(temporary, self.path)
            except OSError:
                logging.getLogger("tesseract.bridge").warning(
                    "config revision persist failed; kept in memory")
            return self._value


class BridgeSession:
    """One owner-thread writer; workers record DTOs and execute bounded jobs."""
    def __init__(self, scope, report, lease, epoch, *, recovery=None,
                 runtime_factory=create_runtime):
        from bridge_state import PublicationState
        from bridge_jobs import JobEventRecorder, JobExecutor
        from bridge_runtime_access import RuntimeAccess, service_state_data
        self.scope, self.report, self.epoch = scope, report, epoch
        self.recovery = recovery if recovery is not None else self._recover_runtime_network
        self.network_lock = threading.RLock()
        self.state = PublicationState(epoch)
        report.gate.require_idle()
        with self.network_lock:
            self.runtime = runtime_factory(scope, report.gate,
                lambda value: self.state.record_edge("service.state", service_state_data(value)))
        self.access = RuntimeAccess(self.runtime)
        self.recorder = JobEventRecorder()
        # T-P2-31a: every mutation command checks the original RT-1 gate once
        # at the executor's mutation entry (resolved at call time).
        self.executor = JobExecutor(self.recorder, epoch=epoch, require_acceptance=True,
                                    mutation_gate=lambda: self.report.gate.require_idle())
        from bridge_secrets import SecretRegistry
        # Session-level layered secret registry (T-P2-31a); the redactor is
        # attached in connect(). Releases refresh on an ordinary worker.
        self.secrets = SecretRegistry(lambda: self.access.known_secret_values(),
                                      defer=lambda work: self.executor.submit_normal(work))
        self.executor.register("service.start", self.start, cancel=self.cancel_change)
        self.executor.register("service.restart", self.restart,
            before=lambda *_: self.access.core_fact()["core_generation"], cancel=self.cancel_change)
        self.lease = lease
        # T-P2-32: set by main only for a guarded startup audit.
        self.startup_audit = None
        self.publisher = self.sampler = self.logs = self.traffic = self.shutdown = None
        self.wiring = None
        self.exit_code = None
        self.outgoing = deque()
        self.pending_acks = deque()
        self._init_dispatch()
        self.diagnostics_closed = False
        self._diagnostics_stopping = False
        self._diagnostics_flush_thread = None
        self._diagnostics_flush_done = False
        self.subscribed_topics = set()

    def _init_dispatch(self):
        self.pending_responses = deque()
        self._write_receipt = None
        self._queries = {}
        self._owner_revoke_hooks = []
        self._revoked_owners = set()
        self._ingress_stop = threading.Event()
        self._ingress_errors = deque()
        self._ingress_queues = (queue.Queue(maxsize=16), queue.Queue(maxsize=64),
                                queue.Queue(maxsize=1), queue.Queue(maxsize=INLINE_QUEUE_CAPACITY))
        self._ingress_threads = []
        self._ingress_order = count(1)
        self._ingress_context = threading.local()
        self._stop_watermark = (None, 0)

    def abort_before_transport(self):
        """A constructed runtime still needs ordered cleanup if IPC setup fails."""
        from bridge_runtime_access import service_state_data
        self.report.gate.require_idle()
        request = self.runtime.request_stop(actor="app_exit")
        with self.network_lock:
            self.runtime.stop(stop_intent=request)
        self.executor.shutdown(time.monotonic() + 2)
        recovered = self.recover(2)
        while True:
            try:
                recovered.gate.require_idle()
                break
            except Exception:
                time.sleep(.05)

    def cancel_change(self, args, context):
        # The control worker owns request_stop and marks running changes
        # superseded. Never report the running operation as generic cancelled.
        self.shutdown.request_stop()
        return False

    def require_current(self, context):
        from bridge_jobs import JobFailure
        order = getattr(context, "ingress", None)
        if order is not None and self._start_is_stale(*order):
            context.superseded.set()
        if context.superseded.is_set():
            raise JobFailure("superseded")
        if context.cancelled.is_set():
            raise JobFailure("cancelled")

    def refresh_secrets(self):
        # Called synchronously before a config transaction emits logs, and after
        # it finishes. P2 config handlers must use this same boundary. Re-read
        # the saved config; leased transient layers stay (T-P2-31a).
        self.secrets.refresh(reload_base=True, reset_external=True)

    def start(self, args, context):
        from bridge_jobs import JobFailure
        from bridge_messages import classify_start_failure
        from bridge_runtime_access import service_state_data
        failure = None
        try:
            self.require_current(context)
            with self.network_lock:
                self.require_current(context)
                self.report.gate.require_idle()
                self.refresh_secrets()
                try:
                    # T-P2-32: the audit wraps only this real runtime start.
                    audit = getattr(self, "startup_audit", None)
                    result = (self.access.start() if audit is None
                              else audit.run_start(self.access.start))
                    if result["failure_code"] or result["startup_phase"] in {"failed", "stopping", "stopped"}:
                        facts = self.access.lifecycle_status()
                        diagnostic = facts["runtime"].get("failure")
                        if diagnostic is None:
                            diagnostic = ({"reason_code": "cleanup_pending"}
                                          if result["failure_code"] == "lifecycle_busy"
                                          else {"text": result["failure_code"]})
                        failure = classify_start_failure(diagnostic=diagnostic, lifecycle=facts)
                        if context.superseded.is_set() or facts["intent"].get("stop_requested"):
                            failure = classify_start_failure(diagnostic={"reason_code": "superseded"})
                        if failure["code"] == "superseded":
                            context.superseded.set()
                        raise JobFailure(failure["code"],
                            dict(startup_phase=result["startup_phase"], failure_code=failure["code"]),
                            error=failure)
                    return result
                except JobFailure:
                    raise
                except Exception as exc:
                    facts = self.access.lifecycle_status()
                    failure = classify_start_failure(exc,
                        facts["runtime"].get("failure"), facts)
                    if context.superseded.is_set() or facts["intent"].get("stop_requested"):
                        failure = classify_start_failure(diagnostic={"reason_code": "superseded"})
                    if failure["code"] == "superseded":
                        context.superseded.set()
                    raise JobFailure(failure["code"],
                        dict(startup_phase="stopping" if failure["code"] == "superseded" else "failed",
                             failure_code=failure["code"]), error=failure) from exc
                finally:
                    self.refresh_secrets()
        finally:
            if failure is None:
                self.record_service()
            else:
                data = service_state_data(self.access.lifecycle_status())
                data["runtime"].update(failure=failure, failure_code=failure["code"])
                if failure["code"] != "superseded":
                    data["runtime"]["startup_phase"] = "failed"
                self.state.record_edge("service.state", data)

    def restart(self, args, context):
        try:
            self.require_current(context)
            with self.network_lock:
                self.require_current(context)
                self.report.gate.require_idle()
                self.refresh_secrets()
                try:
                    return self.access.restart_core()
                finally:
                    self.refresh_secrets()
        finally:
            self.record_service()

    def record_service(self):
        from bridge_runtime_access import service_state_data
        self.state.record_edge("service.state", service_state_data(self.access.lifecycle_status()))

    def recover(self, budget):
        stop_at = time.monotonic() + max(0.0, budget)
        with self.network_lock:
            self.report.gate.require_idle()
            remaining = max(0.0, stop_at - time.monotonic())
            return self.recovery(self.scope, deadline_seconds=remaining)

    def _recover_runtime_network(self, scope, *, deadline_seconds):
        from runtime_network_recovery import recover_network_after_unclean_exit
        # Runtime construction seals RT-1. Exit uses the same manager's
        # transaction authority; the coordinator retains the returned gate.
        return recover_network_after_unclean_exit(
            self.runtime.runtime_state, scope=scope,
            deadline_seconds=deadline_seconds, proxy_manager=self.runtime.system_proxy)

    def connect(self, transport, finish_audit):
        from bridge_publish import Publisher, SamplingWorker
        from bridge_logs import LogCollector
        from bridge_traffic import TrafficCollector
        from bridge_shutdown import ShutdownCoordinator
        # Stop is serialized with the entire start/restart, but its intent stays
        # on the independent control worker, so it can preempt a long start.
        access = self.access
        original_stop = access.stop
        def stop(**kwargs):
            with self.network_lock:
                self.report.gate.require_idle()
                result = original_stop(**kwargs)
                self.record_service()
                return result
        access.stop = stop
        self.transport = transport
        self.publisher = Publisher(self.state, transport, jobs=self.recorder)
        def client():
            # Read only connection settings; no controller endpoint here.
            if not access.core_fact()["running"] or self.runtime.api_client is None:
                raise ConnectionError("core_stopped")
            return self.runtime.api_client
        self.logs = LogCollector(self.state, logger=self.runtime.logger,
            client_factory=client, known_secret_values=self.secrets.merged(reload_base=True))
        # The registry owns the redactor table: it keeps the original
        # replacement method, and a module that still pushes a whole table
        # through the collector lands in the registry's external layer.
        self.secrets.attach(self.logs.refresh_known_secret_values)
        self.logs.refresh_known_secret_values = self.secrets.push_external
        self.traffic = TrafficCollector(self.state, access)
        self.shutdown = ShutdownCoordinator(self.executor, access, lease=self.lease,
            startup_recovery=self.report, recover_network=self.recover,
            send=transport.send, poll_write=transport.poll_write,
            close_diagnostics=self.close_diagnostics,
            drain_publications=self.drain_publications,
            close_owners=self.close_owners, on_phase=self.shutdown_phase,
            exit_process=lambda code: (finish_audit(), setattr(self, "exit_code", code)))
        self.shutdown.session_end.allowed = bool(getattr(self.scope, "may_take_over_host_network", False))
        self.register_command("app.sessionEnd", self.shutdown.session_end.handle)
        # T-P2-31b: every business module, in dependency order (bridge_wiring).
        from bridge_wiring import Wiring
        self.wiring = Wiring(self, scope_kind=getattr(self.scope, "kind", None)).register()
        self.register_owner_revoke_hook(self.publisher.revoke_owner)
        self.sampler = SamplingWorker(self.state, access, sampled_topics=self.wiring)
        transport.on_disconnect = self.shutdown.on_disconnect
        self._start_ingress()

    def _start_ingress(self):
        # Handoff only on the raw reader. Runtime/executor/publisher locks,
        # registration hooks and shutdown intent all belong to these workers.
        def consume(source):
            while not self._ingress_stop.is_set():
                try:
                    order, frame = source.get(timeout=.05)
                except queue.Empty:
                    continue
                try:
                    self._ingress_context.order = order
                    self.dispatch(frame)
                except Exception as exc:
                    self._ingress_errors.append(exc)
                finally:
                    del self._ingress_context.order
                    source.task_done()
        for source, name in zip(self._ingress_queues, ("control", "requests", "emergency", "inline")):
            worker = threading.Thread(target=consume, args=(source,),
                name="bridge-ingress-" + name, daemon=True)
            self._ingress_threads.append(worker)
            worker.start()

    def enqueue_input(self, frame):
        if frame.get("t") == "ping":
            self.dispatch(frame)
            return
        from bridge_commands import COMMAND_SPECS
        # Executor domain (bridge_commands), so reviewed overrides such as
        # alerts.desktopDelivered (control) use the control handoff too.
        spec = COMMAND_SPECS.get(frame.get("cmd")) if frame.get("t") == "req" else None
        control = frame.get("t") in {"cancel", "owner_revoke"} or spec is not None and spec.domain == "control"
        domain = (2 if frame.get("t") == "req" and frame.get("cmd") == "app.sessionEnd"
                  else 3 if spec is not None and spec.inline else 0 if control else 1)
        order = self._record_ingress(frame)
        # Refuse private delivery immediately, even while a revoke hook waits.
        if (frame.get("t") == "owner_revoke" and not protocol.validate_frame(frame)
                and frame.get("epoch") == self.epoch):
            self._revoked_owners.add(frame["owner"])
        try:
            self._ingress_queues[domain].put_nowait((order, deepcopy(frame)))
        except queue.Full:
            if domain == 3:
                # The inline lane is full: answer job_capacity, keep the session.
                self._reject_inline_overflow(frame)
                return
            if domain != 2:
                raise StartupError("control_capacity")
            # A session-end request is already queued for emergency handling.
            # Overflow must never become a session fault: that would send the
            # ordinary quit, which cancels the queued emergency work. Reject
            # the duplicate with a bounded reply and keep the first one.
            self._reject_duplicate_session_end(frame)

    def _reject_inline_overflow(self, frame):
        if protocol.validate_frame(frame) or frame.get("epoch") != self.epoch:
            return
        reply = self.envelope("res", id=frame["id"], **self.identities(frame), cmd=frame["cmd"],
            ok=False, error=dict(code="job_capacity", message_key="errors.bridge.job_capacity",
                                 params={}))
        try:
            self.require_output_capacity()
        except StartupError:
            return  # Only this reply is dropped; the session continues.
        if not protocol.validate_frame(reply):
            self.outgoing.append((reply, None))

    def _reject_duplicate_session_end(self, frame):
        if protocol.validate_frame(frame) or frame.get("epoch") != self.epoch:
            return
        reply = self.envelope("error", id=frame["id"], **self.identities(frame),
            error=dict(code="conflict", message_key="errors.bridge.session_end_in_progress",
                       params={}))
        try:
            self.require_output_capacity()
        except StartupError:
            return  # The queued emergency request still runs; only this reply is dropped.
        if not protocol.validate_frame(reply):
            self.outgoing.append((reply, None))

    def _record_ingress(self, frame):
        # The pipe reader only publishes an order/watermark; never take an
        # executor/runtime lock or invoke an intent callback on that thread.
        order = next(self._ingress_order)
        if (frame.get("t") == "req" and frame.get("cmd") == "app.sessionEnd"
                and frame.get("epoch") == self.epoch and not protocol.validate_frame(frame)
                and self.executor.is_registered(frame["cmd"])
                and frame.get("owner") not in self._revoked_owners):
            session_end = getattr(self.shutdown, "session_end", None)
            if session_end is not None:
                session_end.received(frame["args"]["budget_ms"])
        if (frame.get("t") == "req" and frame.get("cmd") in {"service.stop", "app.quit"}
                and frame.get("epoch") == self.epoch and not protocol.validate_frame(frame)
                and self.executor.is_registered(frame["cmd"])):
            self._stop_watermark = (self.epoch, order)
        return order

    def _start_is_stale(self, epoch, order):
        stopped_epoch, stopped_order = self._stop_watermark
        return epoch == stopped_epoch and order < stopped_order

    def _stamp_start(self, executor, ack, frame, order):
        # The ingress marker was attached at submit (JobContext.ingress).
        if frame["cmd"] not in {"service.start", "service.restart"} or not ack.accepted:
            return
        if self._start_is_stale(frame["epoch"], order):
            executor.cancel(ack.op, owner=frame.get("owner"))

    def _acceptance_sent(self, executor, op, epoch):
        # Recheck after pending/busy writes too. A stop may arrive after submit
        # but before the ack is reaped; such a start must stay unexecuted.
        executor.acceptance_sent(op, epoch=epoch,
                                 preempt=lambda marker: self._start_is_stale(*marker))

    def register_command(self, name, execute, **options):
        """Business tickets register only exact Python v1 names, at setup."""
        if name in {"service.start", "service.restart"}:
            original = execute
            def execute(args, context):
                self.require_current(context)
                return original(args, context)
        self.executor.register(name, execute, **options)

    def register_query(self, name, execute):
        """Query handler receives (args, JobContext) on a normal worker."""
        if name not in protocol.V1_QUERIES or not callable(execute) or name in self._queries:
            raise ValueError("only unregistered v1 queries may be registered")
        self._queries[name] = execute

    def registered_queries(self):
        """Names of the registered v1 queries (hello caps, app.info)."""
        return frozenset(self._queries)

    def capabilities(self):
        """hello.caps: the v1 set this session actually wired (T-P2-31b)."""
        wiring = getattr(self, "wiring", None)
        if wiring is None:
            return {"commands": sorted(protocol.P1_COMMANDS), "topics": sorted(protocol.P1_TOPICS),
                    "features": []}
        return wiring.capabilities()

    def close_owners(self, budget):
        """Full exit step 3 (bridge_shutdown): close every business owner."""
        wiring = getattr(self, "wiring", None)
        return True if wiring is None else wiring.close(budget)

    def shutdown_phase(self, phase, recovery=None, workers_pending=True):
        wiring = getattr(self, "wiring", None)
        if wiring is not None:
            wiring.shutdown_phase(phase, recovery, workers_pending=workers_pending)

    def register_owner_revoke_hook(self, hook):
        """Setup-time ordered hooks; failure of one never skips the others."""
        if not callable(hook):
            raise ValueError("owner revoke hook must be callable")
        self._owner_revoke_hooks.append(hook)

    def close_diagnostics(self, budget):
        self._diagnostics_stopping = True
        if self.diagnostics_closed:
            return True
        deadline = time.monotonic() + max(0, budget)
        if hasattr(self, "_ingress_stop"):
            self._ingress_stop.set()
        for thread in getattr(self, "_ingress_threads", ()):
            thread.join(max(0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in getattr(self, "_ingress_threads", ())):
            return False
        # Every join shares this deadline, including on later owner retries.
        # Always signal all producers, even if an earlier worker is stuck.
        closed = [worker.stop(max(0, deadline - time.monotonic()))
                  for worker in (self.sampler, self.traffic, self.logs)]
        jobs_closed = self.executor.shutdown(deadline)
        if not all(closed) or not jobs_closed:
            return False
        if self._diagnostics_flush_thread is None:
            def flush():
                while self.logs.flush() is not None:
                    pass
                self._diagnostics_flush_done = True
            self._diagnostics_flush_thread = threading.Thread(target=flush,
                name="bridge-log-final-flush", daemon=True)
            self._diagnostics_flush_thread.start()
        self._diagnostics_flush_thread.join(max(0, deadline - time.monotonic()))
        if self._diagnostics_flush_thread.is_alive() or not self._diagnostics_flush_done:
            return False
        self.diagnostics_closed = True
        return True

    def drain_publications(self):
        # Called only after producers/jobs and the final log flush have ended.
        # Reap every ordinary write before allowing bye to own the transport.
        self.pump(force_publish=True)
        return not (self.pending_acks or self.pending_responses or self.outgoing or self.recorder.pending
                    or self.publisher.pending_write
                    or self.publisher.metrics()["control_depth"]
                    or self.state.pending(1))

    def envelope(self, kind, **fields):
        now = time.time_ns() // 1_000_000
        return dict(t=kind, epoch=self.epoch, produced_at_ms=now, sent_at_ms=now, **fields)

    def require_output_capacity(self):
        if len(self.pending_acks) + len(self.outgoing) + len(getattr(self, "pending_responses", ())) >= 64:
            raise StartupError("control_capacity")

    def queue_ack(self, frame, ack):
        self.require_output_capacity()
        self.pending_acks.append((frame, ack, self.executor))

    @staticmethod
    def identities(frame):
        return {key: frame[key] for key in ("req_id", "owner") if key in frame}

    def reject_request(self, frame, code):
        self.require_output_capacity()
        reply = self.envelope("error", id=frame["id"],
            **self.identities(frame), error=dict(code=code,
            message_key="errors.bridge." + code, params={}))
        if not protocol.validate_frame(reply):
            self.outgoing.append((reply, None))

    def _submit_query(self, frame):
        from bridge_jobs import JobContext, JobFailure, RequestReceipt, Response
        handler = self._queries.get(frame["cmd"])
        if handler is None or frame["owner"] in self._revoked_owners:
            self.reject_request(frame, "not_allowed")
            return
        result = []
        receipt = RequestReceipt(True, "res", frame["owner"], op="query-" + str(frame["id"]),
            _take=lambda *_: result.pop() if result else None)
        def work():
            if (receipt.finished.is_set() or frame["owner"] in self._revoked_owners
                    or frame["epoch"] != self.epoch or not executor.accepting
                    or self.shutdown.quitting):
                receipt.finished.set()
                return
            try:
                data = handler(deepcopy(frame["args"]), JobContext(receipt.op))
                reply = self.envelope("res", id=frame["id"], cmd=frame["cmd"],
                    **self.identities(frame), ok=True, data=data)
                if protocol.validate_frame(reply):
                    raise JobFailure("internal")
                response = Response("res", frame["owner"], frame["req_id"],
                    frame["cmd"], True, data=deepcopy(data), epoch=frame["epoch"])
            except Exception as exc:
                code = exc.code if isinstance(exc, JobFailure) and exc.code in protocol.ERROR_CODES else "internal"
                response = Response("res", frame["owner"], frame["req_id"], frame["cmd"],
                    False, error=dict(code=code, message_key="errors.bridge." + code, params={}),
                    epoch=frame["epoch"])
            if (not receipt.finished.is_set() and frame["owner"] not in self._revoked_owners
                    and frame["epoch"] == self.epoch and not self.shutdown.quitting
                    and executor.accepting):
                result.append(response)
            receipt.finished.set()
        executor = self.executor
        if not executor.submit_normal(work):
            self.reject_request(frame, "job_capacity")
            return
        self.pending_responses.append((frame, receipt))

    def owner_at_capacity(self, owner):
        # Both jobs and one-shot requests are charged until terminal/delivery.
        # This runs on the admission worker, never on the raw pipe reader.
        count = self.executor.owner_load(owner)
        count += sum(request.get("owner") == owner and request["cmd"] in protocol.V1_QUERIES
                     for request, _ in list(self.pending_responses))
        return count >= self.executor.owner_limit

    def diagnostics_stopping(self):
        return (self._diagnostics_stopping or self.diagnostics_closed
                or self.shutdown.quitting)

    def start_diagnostics(self):
        if self.diagnostics_stopping():
            return False
        if self.sampler._thread is None:
            for worker in (self.sampler, self.logs, self.traffic):
                if self.diagnostics_stopping():
                    return False
                worker.start()
            self.runtime.logger.info("Bridge subscription active")
        return True

    def dispatch(self, frame):
        errors = protocol.validate_frame(frame)
        if frame.get("epoch") != self.epoch:
            return
        if errors:
            if frame.get("t") == "req" and protocol._request_id(frame.get("id")):
                # T-P2-31b: a request whose id is trustworthy always gets a
                # structured answer (PROTOCOL §1), never silence; it is not
                # executed.
                self.reject_request(frame, request_error_code(errors))
            return
        kind = frame["t"]
        self.require_output_capacity()
        if kind in {"subscribe", "snapshot_req"} and self.diagnostics_stopping():
            fields = dict(id=frame["id"], error=dict(code="not_allowed",
                message_key="errors.bridge.shutting_down", params={"reason": "shutting_down"}))
            if kind == "snapshot_req":
                fields["topic"] = frame["topic"]
            self.outgoing.append((self.envelope(
                "error" if kind == "subscribe" else "snapshot_res", **fields), None))
            return
        if kind == "ping":
            self.outgoing.append((self.envelope("pong", n=frame["n"]), None))
        elif kind == "subscribe":
            if not set(frame["topics"]) <= set(self.capabilities()["topics"]):
                # PROTOCOL §2: only hello.caps.topics; never a partial subscription.
                self.outgoing.append((self.envelope("error", id=frame["id"], error=dict(
                    code="not_allowed", message_key="errors.bridge.not_allowed", params={})), None))
                return
            self.publisher.subscription_frames(frame)
            self.subscribed_topics.update(frame["topics"])
            if "traffic.history" in frame["topics"]:
                self.traffic.request_history()
            self.start_diagnostics()
        elif kind == "snapshot_req":
            if frame["topic"] not in self.subscribed_topics:
                self.outgoing.append((self.envelope("snapshot_res", id=frame["id"],
                    topic=frame["topic"], error=dict(code="not_allowed",
                    message_key="errors.bridge.not_allowed", params={})), None))
            else:
                self.publisher.snapshot_response(frame)
        elif kind == "cancel":
            self.queue_ack(frame, self.executor.cancel(frame["op"], owner=frame.get("owner")))
        elif kind == "owner_revoke":
            from bridge_jobs import Ack
            self._revoked_owners.add(frame["owner"])
            self.executor.revoke_owner(frame["owner"])
            for hook in tuple(self._owner_revoke_hooks):
                try:
                    hook(frame["owner"])
                except Exception:
                    pass
            self.queue_ack(frame, Ack(True))
        elif kind == "req":
            order = getattr(self._ingress_context, "order", None)
            if order is None:
                order = self._record_ingress(frame)
            if frame["cmd"].startswith("host."):
                self.reject_request(frame, "not_allowed")
                return
            if (frame.get("owner") in self._revoked_owners
                    or frame["cmd"] in protocol.V1_QUERIES and frame["cmd"] not in self._queries
                    or frame["cmd"] in protocol.V1_COMMANDS and not self.executor.is_registered(frame["cmd"])):
                self.reject_request(frame, "not_allowed")
                return
            if (frame.get("owner") is not None and frame["cmd"] not in
                    {"service.stop", "app.quit", "app.sessionEnd"}
                    and self.owner_at_capacity(frame["owner"])):
                self.reject_request(frame, "job_capacity")
                return
            if frame["cmd"] in protocol.V1_QUERIES:
                self._submit_query(frame)
                return
            if (frame["cmd"] in {"service.start", "service.restart"}
                    and self._start_is_stale(frame["epoch"], order)):
                self.reject_request(frame, "superseded")
                return
            executor = self.executor
            # Start/restart carry their ingress marker into admission
            # (JobContext.ingress), atomically with the job's creation.
            marker = ({"ingress": (frame["epoch"], order)} if frame["cmd"] in
                      {"service.start", "service.restart"} else {})
            ack = executor.submit(frame["cmd"], frame["args"],
                req_id=frame.get("req_id"), owner=frame.get("owner"), **marker)
            self._stamp_start(executor, ack, frame, order)
            if frame["cmd"] == "app.quit" and ack.accepted:
                # Coordinator already registered the real handler. Retain the
                # exact submission, including business IDs, for shutdown.
                self.shutdown._quitting.set()
                with self.shutdown._lock:
                    if self.shutdown._quit_ack is None:
                        self.shutdown._quit_ack = ack
                self.executor.prepare_quit()
            if ack.outlet == "job":
                if not ack.accepted and ack.skipped_reason == "not_allowed":
                    self.reject_request(frame, "not_allowed")
                else:
                    self.queue_ack(frame, ack)
            elif ack.accepted:
                self.pending_responses.append((frame, ack))
            else:
                if ack.skipped_reason == "not_allowed":
                    self.reject_request(frame, "not_allowed")
                    return
                self.outgoing.append((self.envelope("res", id=frame["id"],
                    **self.identities(frame), cmd=frame["cmd"], ok=False,
                    error=dict(code=ack.skipped_reason or "internal",
                        message_key="errors.bridge." + (ack.skipped_reason or "internal"), params={})), None))

    def pump(self, *, force_publish=False):
        if getattr(self, "_ingress_errors", None):
            raise self._ingress_errors.popleft()
        for frame, response_receipt in list(getattr(self, "pending_responses", ())):
            if frame["cmd"] in protocol.V1_QUERIES and (
                    frame["epoch"] != self.epoch or frame["owner"] in self._revoked_owners
                    or self.shutdown.quitting or not self.executor.accepting):
                # Bare submit_normal work can be removed by shutdown before
                # it runs. Finish its local waiter without waiting on callback.
                response_receipt.finished.set()
                response_receipt.wait(0)
                self.pending_responses.remove((frame, response_receipt))
                if (frame["epoch"] == self.epoch and frame["owner"] not in self._revoked_owners):
                    self.outgoing.append((self.envelope("res", id=frame["id"],
                        **self.identities(frame), cmd=frame["cmd"], ok=False,
                        error=dict(code="runtime_gone", message_key="errors.runtime_gone", params={})), None))
                continue
            if not response_receipt.finished.is_set():
                continue
            response = response_receipt.wait(0)
            self.pending_responses.remove((frame, response_receipt))
            if (response is None or response.epoch != self.epoch
                    or response.owner in self._revoked_owners):
                continue
            fields = dict(id=frame["id"], **self.identities(frame), cmd=response.name, ok=response.ok)
            fields["data" if response.ok else "error"] = response.data if response.ok else response.error
            self.outgoing.append((self.envelope("res", **fields), None))
        for frame, ack, executor in list(self.pending_acks):
            if frame["epoch"] != self.epoch:
                self.pending_acks.remove((frame, ack, executor))
                continue
            intent = ack.wait_stop_intent(0) if ack.intent_receipt else None
            if ack.intent_receipt and intent is None:
                continue
            accepted = ack.accepted and (intent is None or intent.accepted)
            fields = dict(id=frame["id"], **self.identities(frame), accepted=accepted)
            if accepted and ack.op:
                fields["op"] = ack.op
            elif not accepted:
                code = (intent.skipped_reason if intent else ack.skipped_reason) or "internal"
                fields.update(skipped_reason=code, error=dict(code=code, message_key="errors.bridge."+code, params={}))
            receipt = ((executor, ack.op, frame["epoch"]) if accepted and ack.op
                       and frame.get("cmd") in protocol.V1_COMMANDS else None)
            self.outgoing.append((self.envelope("ack", **fields), receipt))
            self.pending_acks.remove((frame, ack, executor))
        if self.publisher.pending_write:
            outcome = self.transport.poll_write()
            if outcome == "pending":
                return
            self.publisher.pending_write = False
            receipt, self._write_receipt = getattr(self, "_write_receipt", None), None
            if receipt:
                self._acceptance_sent(*receipt)
        while self.outgoing:
            frame, receipt = self.outgoing[0]
            if (frame["epoch"] != self.epoch or frame["t"] == "res"
                    and frame.get("owner") in getattr(self, "_revoked_owners", ())):
                self.outgoing.popleft()
                continue
            frame["sent_at_ms"] = time.time_ns() // 1_000_000
            result = self.transport.send(frame)
            if result == "busy":
                return
            self.outgoing.popleft()
            if result == "pending":
                self._write_receipt = receipt
                self.publisher.pending_write = True
                return
            if receipt:
                self._acceptance_sent(*receipt)
        wiring = getattr(self, "wiring", None)
        if wiring is not None and not force_publish:
            wiring.tick()
        if force_publish or time.monotonic() >= self.publisher.next_tick:
            self.publisher.tick()

    def run(self):
        try:
            while self.exit_code is None:
                frame = self.transport.receive(timeout=.05)
                if self.shutdown.bye_submitted:
                    # Discard late input locally. Ordinary dispatch/pump/ticks
                    # must never reap bye and insert another frame behind it.
                    if self.transport.disconnected:
                        self.shutdown.on_disconnect()
                    self.shutdown.finish_quit(deadline_seconds=.1)
                    continue
                if frame is not None:
                    self.enqueue_input(frame)
                elif self.transport.disconnected:
                    self.shutdown.on_disconnect()
                if not self.transport.disconnected:
                    self.pump()
                if self.shutdown._quit_ack is not None:
                    ack = self.shutdown._quit_ack
                    job = self.executor.get_job(ack.op) if ack.op else None
                    if job and job["state"] in {"done", "failed"}:
                        # The coordinator closes producers, flushes logs, then
                        # drains all publications before submitting the final bye.
                        self.shutdown.finish_quit(deadline_seconds=.1)
        except Exception:
            # A fatal framing/control backlog ends this IPC session. Skip wire
            # delivery and pending writes; ordered runtime/network cleanup still
            # owns the lease. Closing in main finally reaps the transport IO.
            self.shutdown.on_fatal()
            self.shutdown.on_disconnect()
            while not self.shutdown.finish_quit(deadline_seconds=.1):
                time.sleep(.01)
        return self.exit_code


def init_logging(scope: instance_scope.InstanceScope) -> logging.Logger:
    data_dir = scope.data_dir
    if data_dir is None:
        raise StartupError("scope_data_dir_missing")
    data_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("tesseract.bridge")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    for handler in (logging.StreamHandler(sys.stderr),
                    logging.FileHandler(data_dir / "bridge.log", encoding="utf-8")):
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    # T-P2-H7: make the entry's platform seed observable in the field
    # ("seeded", "skipped:<why>", or "not_run" outside runtime_main.pyw).
    import sidecar_platform_seed
    logger.info("Platform uname seed: %s", sidecar_platform_seed.status())
    return logger


def resolve_effective_scope() -> instance_scope.InstanceScope:
    """Pass the same effective directories to scope resolution and logging."""
    config = os.environ.get("TESSERACT_CONFIG_DIR")
    data = os.environ.get("TESSERACT_DATA_DIR")
    if ((config is None) != (data is None)
            or (config is not None and (not config.strip() or not data.strip()))):
        raise StartupError("directory_override_pair_required")
    if config is None:
        config = str(Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))) / "Tesseract")
        data = str(Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "Tesseract")
    else:
        for field, raw in (("config_dir", config), ("data_dir", data)):
            path = Path(raw)
            if not path.is_absolute() or not path.is_dir():
                raise StartupError(f"{field}_invalid")
        config = str(Path(config).resolve(strict=True))
        data = str(Path(data).resolve(strict=True))
    return instance_scope.resolve_scope(config_dir=config, data_dir=data)


@dataclass
class Steps:
    parse: Callable[[list[str]], argparse.Namespace] = parse_args
    scope: Callable[[], instance_scope.InstanceScope] = resolve_effective_scope
    acquire: Callable[[instance_scope.InstanceScope], instance_scope.AcquisitionResult] = instance_scope.acquire_instance
    logging: Callable[[instance_scope.InstanceScope], logging.Logger] = init_logging
    audit: Callable[[instance_scope.InstanceScope], None] = attach_audit_guard
    recovery: Callable[[instance_scope.InstanceScope], dict] = recover_network
    transport: Callable[[argparse.Namespace], Transport] = open_transport
    loop: Callable[[Transport, str], None] = read_loop
    helper: Callable[[list[str]], int] = dispatch_helper
    # T-P2-32: None = production default (prepare_startup_audit) when steps
    # are not injected; injected Steps opt in explicitly.
    startup_audit: Callable[..., object] | None = None


def request_error_code(errors) -> str:
    """Public error code of a request frame that failed validation."""
    if any(error in {"enum:cmd", "not_allowed:cmd"} or error.startswith("not_allowed:")
           for error in errors):
        return "not_allowed"
    if any(error.startswith("missing:") for error in errors):
        return "missing_field"
    if any(error.startswith("enum:") for error in errors):
        return "invalid_enum"
    if any(error.startswith("type:") for error in errors):
        return "invalid_type"
    return "validation"


def session_capabilities(session) -> dict:
    """hello.caps of a production session (T-P2-31b: the wired v1 set)."""
    capabilities = getattr(session, "capabilities", None)
    if callable(capabilities):
        return capabilities()
    return {"commands": sorted(protocol.P1_COMMANDS), "topics": sorted(protocol.P1_TOPICS),
            "features": []}


def _error(code: str, **fields: object) -> None:
    print(json.dumps({"code": code, **fields}, separators=(",", ":")), file=sys.stderr)


def main(argv: list[str] | None = None, *, steps: Steps | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    work = Steps() if steps is None else steps
    if is_helper_argv(args_list):
        try:
            return work.helper(args_list)
        except StartupError as exc:
            _error(exc.code)
            return 2
    lease = None
    transport = None
    result = 0
    session = None
    guard = None
    report = None
    startup_audit = None

    def finish_audit():
        # T-P2-32: final audit sample/flush first, then the guard receipts.
        if startup_audit is not None:
            startup_audit.finish()
        if guard is not None:
            guard.finish()

    try:
        args = work.parse(args_list)
        scope = work.scope()
        acquisition = work.acquire(scope, legacy_gate=args.legacy_gate) if getattr(args, "legacy_gate", "create") != "create" else work.acquire(scope)
        if acquisition.status != "acquired" or acquisition.lease is None:
            _error("single_instance_rejected", status=acquisition.status,
                   reason=acquisition.reason_code, win32_error=acquisition.win32_error)
            raise SingleInstanceRejected("single_instance_rejected")
        lease = acquisition.lease
        work.logging(scope)
        guard = work.audit(scope)
        # T-P2-32: refuse a requested startup audit before RT-1 (missing
        # guard, non-audit scope, invalid settings, frozen identity mismatch).
        prepare = work.startup_audit if work.startup_audit is not None else (
            prepare_startup_audit if steps is None else None)
        audit_plan = None
        if prepare is not None:
            try:
                audit_plan = prepare(scope, guard)
            except (PermissionError, ValueError) as exc:
                raise StartupError("startup_audit_refused") from exc
        report = work.recovery(scope)
        epoch = uuid.uuid4().hex
        if steps is None:
            # If a bounded RT-1 call returned with an active worker, retain
            # ownership until it is idle; do not construct or start a runtime.
            while True:
                try:
                    report.gate.require_idle()
                    break
                except Exception:
                    time.sleep(.05)
            session = BridgeSession(scope, report, lease, epoch)
            if audit_plan is not None:
                from bridge_audit import create_startup_audit
                startup_audit = create_startup_audit(scope, audit_plan)
                session.startup_audit = startup_audit
        transport = work.transport(args)
        if steps is None:
            session.connect(transport, finish_audit)
            if startup_audit is not None:
                from bridge_audit import BridgeSessionAuditPort
                startup_audit.bind(BridgeSessionAuditPort(session))
        now = int(time.time() * 1000)
        hello = {"t": "hello", "epoch": epoch, "produced_at_ms": now,
                 "sent_at_ms": now, "proto": protocol.PROTOCOL_VERSION,
                 "app_version": APP_VERSION, "runtime_pid": os.getpid(),
                 "lang": args.lang, "caps": (session_capabilities(session) if steps is None else {"commands": [], "topics": [], "features": []}),
                 "scope": scope.hello_scope(), "network_recovery": report}
        outcome = transport.send(hello)
        while outcome == "pending":
            time.sleep(.005)
            outcome = transport.poll_write()
        ack = transport.receive(timeout=5) if steps is None else transport.receive()
        if (ack is None or protocol.validate_frame(ack) or ack.get("t") != "hello_ack"
                or ack.get("epoch") != epoch or ack.get("proto") != protocol.PROTOCOL_VERSION
                or ack.get("accepted") is not True):
            raise StartupError("hello_ack_rejected")
        if steps is None:
            if startup_audit is not None:
                startup_audit.begin()
            result = session.run()
        else:
            work.loop(transport, epoch)
    except SingleInstanceRejected:
        result = 3
    except StartupError as exc:
        _error(exc.code)
        result = 4
    except Exception as exc:
        _error("startup_failed", type=type(exc).__name__)
        result = 4
    finally:
        if session is not None and session.shutdown is None:
            session.abort_before_transport()
        if session is not None and session.exit_code is None and session.shutdown is not None:
            session.shutdown.on_fatal()
            session.shutdown.on_disconnect()
            while not session.shutdown.finish_quit(deadline_seconds=.1):
                time.sleep(.01)
        if report is not None and hasattr(report, "gate"):
            while True:
                try:
                    report.gate.require_idle()
                    break
                except Exception:
                    time.sleep(.05)
        finish_audit()
        try:
            if transport is not None:
                transport.close()
        except Exception as exc:
            _error("transport_close_failed", type=type(exc).__name__)
            if result == 0:
                result = 4
        finally:
            if lease is not None:
                try:
                    lease.close()
                except Exception as exc:
                    _error("lease_close_failed", type=type(exc).__name__)
                    if result == 0:
                        result = 4
    return result
