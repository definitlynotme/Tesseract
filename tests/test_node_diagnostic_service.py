from __future__ import annotations

import io
import logging
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import yaml

from models import Node
from node_diagnostic import (
    EphemeralNodeDiagnosticService,
    _WindowsAtomicJobProcessOwner,
    _cleanup_ephemeral_workdir,
    diagnostic_node_identity,
)


def _node(
    *,
    name: str = "target",
    address: str = "sensitive-target.example",
    tier: str = "tier2",
) -> Node:
    return Node(
        name=name,
        protocol="ss",
        address=address,
        port=8388,
        tier=tier,
        method="aes-256-gcm",
        password="sensitive-password",
    )


class _FakeProcess:
    def __init__(self) -> None:
        self.alive = True
        self.calls: list[str] = []
        self.killed = threading.Event()

    def poll(self) -> int | None:
        return None if self.alive else 0

    def terminate(self) -> None:
        self.calls.append("terminate")

    def wait(self, timeout: float | None = None) -> int:
        self.calls.append("wait")
        if self.alive:
            raise subprocess.TimeoutExpired("ephemeral-mihomo", timeout)
        return 0

    def kill(self) -> None:
        self.calls.append("kill")
        self.alive = False
        self.killed.set()


class _ProcessFactory:
    def __init__(self) -> None:
        self.processes: list[_FakeProcess] = []
        self.launches: list[dict[str, Any]] = []

    def __call__(self, command: list[str], **kwargs: Any) -> _FakeProcess:
        config_path = Path(command[2])
        work_dir = Path(command[4])
        process = _FakeProcess()
        self.processes.append(process)
        self.launches.append(
            {
                "command": list(command),
                "kwargs": dict(kwargs),
                "config_path": config_path,
                "work_dir": work_dir,
                "config": yaml.safe_load(config_path.read_text(encoding="utf-8")),
            }
        )
        return process


class _FailingProcessFactory:
    def __init__(self) -> None:
        self.work_dirs: list[Path] = []

    def __call__(self, command: list[str], **kwargs: Any):
        self.work_dirs.append(Path(command[4]))
        raise OSError("process creation failed")


class _EarlyExitThenProcessFactory(_ProcessFactory):
    def __call__(self, command: list[str], **kwargs: Any) -> _FakeProcess:
        process = super().__call__(command, **kwargs)
        if len(self.processes) == 1:
            process.alive = False
        return process


class _ControllableOwner:
    def __init__(
        self,
        process_factory: _ProcessFactory,
        *,
        launch_gate: threading.Event | None = None,
        cleanup_gate: threading.Event | None = None,
    ) -> None:
        self.process_factory = process_factory
        self.launch_gate = launch_gate
        self.cleanup_gate = cleanup_gate
        self.launch_entered = threading.Event()
        self.process_created = threading.Event()
        self._lock = threading.RLock()
        self._process: _FakeProcess | None = None
        self._launching = False
        self._released = False

    def launch(self, command: list[str], **kwargs: Any) -> _FakeProcess:
        with self._lock:
            self._launching = True
        process = self.process_factory(command, **kwargs)
        with self._lock:
            self._process = process
        self.process_created.set()
        self.launch_entered.set()
        if self.launch_gate is not None:
            self.launch_gate.wait(timeout=2.0)
        with self._lock:
            self._launching = False
        return process

    def terminate_and_wait(self) -> bool:
        with self._lock:
            if self._launching:
                return False
            if self.cleanup_gate is not None and not self.cleanup_gate.is_set():
                return False
            process = self._process
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=0.1)
        with self._lock:
            self._process = None
            self._released = True
            return True

    def resources_released(self) -> bool:
        with self._lock:
            return bool(self._released and not self._launching and self._process is None)


class _Client:
    def __init__(
        self,
        process: _FakeProcess,
        *,
        readiness_error: Exception | None = None,
        delay: Any = 137,
        delay_error: Exception | None = None,
        delay_entered: threading.Event | None = None,
        wait_for_kill: bool = False,
    ) -> None:
        self.process = process
        self.readiness_error = readiness_error
        self.delay = delay
        self.delay_error = delay_error
        self.delay_entered = delay_entered
        self.wait_for_kill = wait_for_kill
        self.calls: list[tuple[Any, ...]] = []

    def version(self) -> dict[str, Any]:
        self.calls.append(("version",))
        if self.readiness_error is not None:
            raise self.readiness_error
        return {"version": "test"}

    def proxy_delay(self, name: str, *, url: str, timeout_ms: int = 5000) -> dict[str, Any]:
        self.calls.append(("proxy_delay", name, url, timeout_ms))
        if self.delay_entered is not None:
            self.delay_entered.set()
        if self.wait_for_kill:
            if not self.process.killed.wait(timeout=2.0):
                raise TimeoutError("test cancellation did not stop process")
        if self.delay_error is not None:
            raise self.delay_error
        return {"delay": self.delay}


class _ClientFactory:
    def __init__(self, process_factory: _ProcessFactory, **client_kwargs: Any) -> None:
        self.process_factory = process_factory
        self.client_kwargs = client_kwargs
        self.clients: list[_Client] = []
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> _Client:
        self.calls.append(dict(kwargs))
        client = _Client(self.process_factory.processes[-1], **self.client_kwargs)
        self.clients.append(client)
        return client


class _AdvancingClock:
    def __init__(self, step: float = 0.06) -> None:
        self.value = 0.0
        self.step = step

    def __call__(self) -> float:
        self.value += self.step
        return self.value


class EphemeralNodeDiagnosticServiceTests(unittest.TestCase):
    def _run(
        self,
        *,
        client_kwargs: dict[str, Any] | None = None,
        mode: str = "direct",
        upstream_nodes: list[Node] | None = None,
        startup_timeout: float = 0.2,
        monotonic=None,
        request_id: str = "request-1",
    ):
        parent_handle = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(parent_handle.cleanup)
        parent = Path(parent_handle.name)
        binary = parent / "diagnostic-mihomo.exe"
        binary.write_bytes(b"fake binary")
        process_factory = _ProcessFactory()
        client_factory = _ClientFactory(process_factory, **(client_kwargs or {}))
        service = EphemeralNodeDiagnosticService(
            process_factory=process_factory,
            client_factory=client_factory,
            monotonic=monotonic or _AdvancingClock(0.001),
        )
        result = service.diagnose(
            _node(),
            node_identity=diagnostic_node_identity(_node()),
            binary_path=binary,
            probe_url="https://sensitive-probe.example/generate_204",
            request_id=request_id,
            mode=mode,
            upstream_nodes=upstream_nodes or [],
            timeout_ms=900,
            startup_timeout=startup_timeout,
            temp_parent=parent,
        )
        return result, service, process_factory, client_factory, parent

    def _assert_process_destroyed_and_workdir_removed(
        self,
        process_factory: _ProcessFactory,
    ) -> None:
        self.assertEqual(len(process_factory.processes), 1)
        process = process_factory.processes[0]
        self.assertFalse(process.alive)
        self.assertEqual(process.calls, ["terminate", "wait", "kill", "wait"])
        self.assertFalse(process_factory.launches[0]["work_dir"].exists())

    def test_success_uses_only_request_owned_process_ports_secret_and_controller_delay(self) -> None:
        stream = io.StringIO()
        logger = logging.getLogger(f"test-node-diagnostic-{id(self)}")
        logger.handlers[:] = [logging.StreamHandler(stream)]
        logger.setLevel(logging.INFO)
        parent_handle = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(parent_handle.cleanup)
        parent = Path(parent_handle.name)
        binary = parent / "diagnostic-mihomo.exe"
        binary.write_bytes(b"fake binary")
        process_factory = _ProcessFactory()
        client_factory = _ClientFactory(process_factory, delay=137)
        service = EphemeralNodeDiagnosticService(
            logger=logger,
            process_factory=process_factory,
            client_factory=client_factory,
            monotonic=_AdvancingClock(0.001),
        )

        node = _node()
        formal_mixed = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        formal_controller = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        formal_mixed.bind(("127.0.0.1", 0))
        formal_controller.bind(("127.0.0.1", 0))
        formal_mixed.listen(1)
        formal_controller.listen(1)
        formal_ports = {
            int(formal_mixed.getsockname()[1]),
            int(formal_controller.getsockname()[1]),
        }
        try:
            result = service.diagnose(
                node,
                node_identity=diagnostic_node_identity(node),
                binary_path=binary,
                probe_url="https://sensitive-probe.example/generate_204",
                request_id="sensitive-request-id",
                mode="all",
                timeout_ms=900,
                temp_parent=parent,
            )
        finally:
            formal_mixed.close()
            formal_controller.close()

        self.assertEqual(
            set(result),
            {
                "request_id",
                "node_identity",
                "config_fingerprint",
                "mode",
                "started_at",
                "completed_at",
                "state",
                "latency_ms",
                "reason",
            },
        )
        self.assertEqual(result["state"], "success")
        self.assertEqual(result["reason"], "ok")
        self.assertEqual(result["latency_ms"], 137.0)
        self.assertEqual(result["mode"], "direct")
        self.assertNotIn("probes", result)
        launch = process_factory.launches[0]
        config = launch["config"]
        ephemeral_ports = {
            int(config["mixed-port"]),
            int(config["external-controller"].rsplit(":", 1)[1]),
        }
        self.assertEqual(len(ephemeral_ports), 2)
        self.assertTrue(ephemeral_ports.isdisjoint(formal_ports))
        self.assertTrue(config["secret"])
        self.assertNotEqual(config["secret"], "FORMAL-CORE-SECRET")
        self.assertEqual(config["secret"], client_factory.calls[0]["secret"])
        self.assertEqual(
            client_factory.calls[0]["base_url"],
            f"http://{config['external-controller']}",
        )
        self.assertEqual(launch["kwargs"]["cwd"], str(launch["work_dir"]))
        self.assertEqual(launch["command"][2], str(launch["config_path"]))
        self.assertEqual(launch["command"][4], str(launch["work_dir"]))
        self.assertNotIn("FORMAL-CORE-CONFIG", " ".join(launch["command"]))
        self.assertNotIn("FORMAL-CORE-WORKDIR", " ".join(launch["command"]))
        self.assertEqual(config["proxies"][0]["name"], "DIAG-TARGET")
        self.assertNotIn("dialer-proxy", config["proxies"][0])
        self.assertEqual(
            client_factory.clients[0].calls[-1],
            (
                "proxy_delay",
                "DIAG-TARGET",
                "https://sensitive-probe.example/generate_204",
                900,
            ),
        )
        logs = stream.getvalue()
        for sensitive in (
            "sensitive-target.example",
            "sensitive-password",
            "sensitive-probe.example",
            "sensitive-request-id",
            str(config["secret"]),
            str(config["external-controller"]),
        ):
            self.assertNotIn(sensitive, logs)
        self._assert_process_destroyed_and_workdir_removed(process_factory)

    def test_chain_config_copies_upstreams_in_order_without_formal_group_references(self) -> None:
        upstreams = [
            _node(name="upstream-a", address="upstream-a.example", tier="tier1"),
            _node(name="upstream-b", address="upstream-b.example", tier="tier3"),
        ]
        result, _service, processes, _clients, _parent = self._run(
            mode="routed",
            upstream_nodes=upstreams,
        )

        self.assertEqual(result["state"], "success")
        self.assertEqual(result["mode"], "chain")
        config = processes.launches[0]["config"]
        self.assertEqual(
            [proxy["name"] for proxy in config["proxies"]],
            ["DIAG-UPSTREAM-001", "DIAG-UPSTREAM-002", "DIAG-TARGET"],
        )
        self.assertEqual(
            config["proxy-groups"][0]["proxies"],
            ["DIAG-UPSTREAM-001", "DIAG-UPSTREAM-002"],
        )
        self.assertEqual(
            config["proxy-groups"][0]["empty-fallback"],
            "REJECT",
        )
        self.assertEqual(config["proxies"][-1]["dialer-proxy"], "DIAG-UPSTREAM")
        rendered = yaml.safe_dump(config)
        self.assertNotIn("PROXY-FALLBACK", rendered)
        self.assertNotIn("FINAL-EXIT", rendered)
        self._assert_process_destroyed_and_workdir_removed(processes)

    def test_readiness_failure_stops_process_and_removes_request_directory(self) -> None:
        result, _service, processes, _clients, _parent = self._run(
            client_kwargs={"readiness_error": OSError("controller unavailable")},
            startup_timeout=0.1,
            monotonic=_AdvancingClock(0.06),
        )

        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["reason"], "startup_failed")
        self._assert_process_destroyed_and_workdir_removed(processes)

    def test_controller_timeout_never_publishes_tcp_timing_and_cleans_up(self) -> None:
        result, _service, processes, _clients, _parent = self._run(
            client_kwargs={"delay_error": TimeoutError("request timed out")},
        )

        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["reason"], "timeout")
        self.assertIsNone(result["latency_ms"])
        self.assertNotIn("probes", result)
        self._assert_process_destroyed_and_workdir_removed(processes)

    def test_controller_failure_returns_stable_reason_and_cleans_up(self) -> None:
        result, _service, processes, _clients, _parent = self._run(
            client_kwargs={"delay_error": OSError("raw endpoint detail must not escape")},
        )

        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["reason"], "probe_failed")
        self.assertNotIn("raw endpoint", str(result))
        self._assert_process_destroyed_and_workdir_removed(processes)

    def test_cancel_by_request_id_kills_active_process_and_returns_cancelled(self) -> None:
        parent_handle = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(parent_handle.cleanup)
        parent = Path(parent_handle.name)
        binary = parent / "diagnostic-mihomo.exe"
        binary.write_bytes(b"fake binary")
        process_factory = _ProcessFactory()
        delay_entered = threading.Event()
        client_factory = _ClientFactory(
            process_factory,
            delay_entered=delay_entered,
            wait_for_kill=True,
            delay_error=OSError("controller stopped"),
        )
        service = EphemeralNodeDiagnosticService(
            process_factory=process_factory,
            client_factory=client_factory,
            monotonic=_AdvancingClock(0.001),
        )
        results: list[dict[str, Any]] = []

        worker = threading.Thread(
            target=lambda: results.append(
                service.diagnose(
                    _node(),
                    node_identity=diagnostic_node_identity(_node()),
                    binary_path=binary,
                    probe_url="https://sensitive-probe.example/generate_204",
                    request_id="cancel-me",
                    temp_parent=parent,
                )
            )
        )
        worker.start()
        self.assertTrue(delay_entered.wait(timeout=1.0))
        self.assertFalse(service.cancel("wrong-request"))
        self.assertTrue(service.cancel("cancel-me"))
        worker.join(timeout=2.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["state"], "cancelled")
        self.assertEqual(results[0]["reason"], "cancelled")
        self._assert_process_destroyed_and_workdir_removed(process_factory)

    def test_missing_upstream_fails_without_starting_any_process(self) -> None:
        result, _service, processes, clients, _parent = self._run(mode="chain")

        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["reason"], "upstream_missing")
        self.assertEqual(processes.processes, [])
        self.assertEqual(clients.clients, [])

    def test_process_creation_failure_removes_written_config_directory(self) -> None:
        parent_handle = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(parent_handle.cleanup)
        parent = Path(parent_handle.name)
        binary = parent / "diagnostic-mihomo.exe"
        binary.write_bytes(b"fake binary")
        process_factory = _FailingProcessFactory()
        service = EphemeralNodeDiagnosticService(process_factory=process_factory)

        result = service.diagnose(
            _node(),
            node_identity=diagnostic_node_identity(_node()),
            binary_path=binary,
            probe_url="https://sensitive-probe.example/generate_204",
            request_id="startup-failure",
            temp_parent=parent,
        )

        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["reason"], "startup_failed")
        self.assertEqual(len(process_factory.work_dirs), 1)
        self.assertFalse(process_factory.work_dirs[0].exists())

    def test_cleanup_failure_overrides_success_and_never_publishes_latency(self) -> None:
        parent_handle = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(parent_handle.cleanup)
        parent = Path(parent_handle.name)
        binary = parent / "diagnostic-mihomo.exe"
        binary.write_bytes(b"fake binary")
        process_factory = _ProcessFactory()
        client_factory = _ClientFactory(process_factory, delay=88)
        service = EphemeralNodeDiagnosticService(
            process_factory=process_factory,
            client_factory=client_factory,
            monotonic=_AdvancingClock(0.001),
        )

        from node_diagnostic import _cleanup_ephemeral_workdir as real_cleanup

        def clean_but_report_failure(path: Path | None) -> bool:
            real_cleanup(path)
            return False

        with patch(
            "node_diagnostic._cleanup_ephemeral_workdir",
            side_effect=clean_but_report_failure,
        ):
            result = service.diagnose(
                _node(),
                node_identity=diagnostic_node_identity(_node()),
                binary_path=binary,
                probe_url="https://sensitive-probe.example/generate_204",
                request_id="cleanup-failure",
                temp_parent=parent,
            )

        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["reason"], "cleanup_failed")
        self.assertIsNone(result["latency_ms"])
        self.assertFalse(process_factory.launches[0]["work_dir"].exists())

    def test_close_cancels_request_destroys_core_and_seals_service(self) -> None:
        parent_handle = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(parent_handle.cleanup)
        parent = Path(parent_handle.name)
        binary = parent / "diagnostic-mihomo.exe"
        binary.write_bytes(b"fake binary")
        process_factory = _ProcessFactory()
        delay_entered = threading.Event()
        client_factory = _ClientFactory(
            process_factory,
            delay_entered=delay_entered,
            wait_for_kill=True,
            delay_error=OSError("controller stopped"),
        )
        service = EphemeralNodeDiagnosticService(
            process_factory=process_factory,
            client_factory=client_factory,
            monotonic=_AdvancingClock(0.001),
        )
        results: list[dict[str, Any]] = []
        worker = threading.Thread(
            target=lambda: results.append(
                service.diagnose(
                    _node(),
                    node_identity=diagnostic_node_identity(_node()),
                    binary_path=binary,
                    probe_url="https://sensitive-probe.example/generate_204",
                    request_id="close-me",
                    temp_parent=parent,
                )
            )
        )
        worker.start()
        self.assertTrue(delay_entered.wait(timeout=1.0))

        self.assertTrue(service.close(timeout=2.0))
        worker.join(timeout=1.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0]["state"], "cancelled")
        self._assert_process_destroyed_and_workdir_removed(process_factory)
        sealed = service.diagnose(
            _node(),
            node_identity=diagnostic_node_identity(_node()),
            binary_path=binary,
            probe_url="https://sensitive-probe.example/generate_204",
            request_id="after-close",
            temp_parent=parent,
        )
        self.assertEqual(sealed["reason"], "service_closed")

    def test_close_during_launch_retains_owner_and_directory_until_launch_reaped(self) -> None:
        parent_handle = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(parent_handle.cleanup)
        parent = Path(parent_handle.name)
        binary = parent / "diagnostic-mihomo.exe"
        binary.write_bytes(b"fake binary")
        process_factory = _ProcessFactory()
        client_factory = _ClientFactory(process_factory)
        launch_gate = threading.Event()
        owners: list[_ControllableOwner] = []

        def owner_factory() -> _ControllableOwner:
            owner = _ControllableOwner(process_factory, launch_gate=launch_gate)
            owners.append(owner)
            return owner

        service = EphemeralNodeDiagnosticService(
            process_owner_factory=owner_factory,
            client_factory=client_factory,
            monotonic=_AdvancingClock(0.001),
        )
        results: list[dict[str, Any]] = []
        worker = threading.Thread(
            target=lambda: results.append(
                service.diagnose(
                    _node(),
                    node_identity=diagnostic_node_identity(_node()),
                    binary_path=binary,
                    probe_url="https://probe.example/generate_204",
                    request_id="close-during-launch",
                    temp_parent=parent,
                )
            )
        )
        worker.start()
        self.assertTrue(owners or self._wait_for(lambda: bool(owners)))
        self.assertTrue(owners[0].process_created.wait(timeout=1.0))
        work_dir = process_factory.launches[0]["work_dir"]

        self.assertFalse(service.close(timeout=0.05))
        self.assertTrue(work_dir.exists())
        self.assertIsNotNone(service._active)

        launch_gate.set()
        worker.join(timeout=2.0)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0]["state"], "cancelled")
        self.assertTrue(service.close(timeout=1.0))
        self.assertFalse(work_dir.exists())
        self.assertFalse(process_factory.processes[0].alive)

    def test_failed_process_cleanup_retains_owner_and_workdir_for_close_retry(self) -> None:
        parent_handle = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(parent_handle.cleanup)
        parent = Path(parent_handle.name)
        binary = parent / "diagnostic-mihomo.exe"
        binary.write_bytes(b"fake binary")
        process_factory = _ProcessFactory()
        client_factory = _ClientFactory(process_factory, delay=91)
        cleanup_gate = threading.Event()

        service = EphemeralNodeDiagnosticService(
            process_owner_factory=lambda: _ControllableOwner(
                process_factory,
                cleanup_gate=cleanup_gate,
            ),
            client_factory=client_factory,
            monotonic=_AdvancingClock(0.001),
        )
        result = service.diagnose(
            _node(),
            node_identity=diagnostic_node_identity(_node()),
            binary_path=binary,
            probe_url="https://probe.example/generate_204",
            request_id="retained-owner",
            temp_parent=parent,
        )
        work_dir = process_factory.launches[0]["work_dir"]

        self.assertEqual(result["reason"], "cleanup_failed")
        self.assertTrue(process_factory.processes[0].alive)
        self.assertTrue(work_dir.exists())
        self.assertIsNotNone(service._active)
        self.assertFalse(service.close(timeout=0.05))

        cleanup_gate.set()
        self.assertTrue(service.close(timeout=1.0))
        self.assertFalse(process_factory.processes[0].alive)
        self.assertFalse(work_dir.exists())
        self.assertIsNone(service._active)

    def test_failed_directory_cleanup_is_retained_and_close_reports_truthfully(self) -> None:
        parent_handle = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(parent_handle.cleanup)
        parent = Path(parent_handle.name)
        binary = parent / "diagnostic-mihomo.exe"
        binary.write_bytes(b"fake binary")
        process_factory = _ProcessFactory()
        client_factory = _ClientFactory(process_factory, delay=73)
        service = EphemeralNodeDiagnosticService(
            process_factory=process_factory,
            client_factory=client_factory,
            monotonic=_AdvancingClock(0.001),
        )
        allow_cleanup = threading.Event()

        def controlled_cleanup(path: Path | None) -> bool:
            if not allow_cleanup.is_set():
                return False
            return _cleanup_ephemeral_workdir(path)

        with patch(
            "node_diagnostic._cleanup_ephemeral_workdir",
            side_effect=controlled_cleanup,
        ):
            result = service.diagnose(
                _node(),
                node_identity=diagnostic_node_identity(_node()),
                binary_path=binary,
                probe_url="https://probe.example/generate_204",
                request_id="retained-directory",
                temp_parent=parent,
            )
            work_dir = process_factory.launches[0]["work_dir"]
            self.assertEqual(result["reason"], "cleanup_failed")
            self.assertTrue(work_dir.exists())
            self.assertFalse(service.close(timeout=0.05))
            allow_cleanup.set()
            self.assertTrue(service.close(timeout=1.0))
            self.assertFalse(work_dir.exists())

    def test_cleanup_helper_never_raises_when_path_state_is_unreadable(self) -> None:
        class _UnreadablePath:
            def exists(self) -> bool:
                raise PermissionError("blocked")

        self.assertFalse(_cleanup_ephemeral_workdir(_UnreadablePath()))

    def test_early_child_exit_retries_with_fresh_ports_and_owner(self) -> None:
        parent_handle = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(parent_handle.cleanup)
        parent = Path(parent_handle.name)
        binary = parent / "diagnostic-mihomo.exe"
        binary.write_bytes(b"fake binary")
        process_factory = _EarlyExitThenProcessFactory()
        client_factory = _ClientFactory(process_factory, delay=64)
        service = EphemeralNodeDiagnosticService(
            process_factory=process_factory,
            client_factory=client_factory,
            monotonic=_AdvancingClock(0.001),
        )

        result = service.diagnose(
            _node(),
            node_identity=diagnostic_node_identity(_node()),
            binary_path=binary,
            probe_url="https://probe.example/generate_204",
            request_id="port-race-retry",
            temp_parent=parent,
            excluded_ports={7890, 9090},
        )

        self.assertEqual(result["state"], "success")
        self.assertEqual(len(process_factory.launches), 2)
        port_pairs = [
            (
                launch["config"]["mixed-port"],
                int(launch["config"]["external-controller"].rsplit(":", 1)[1]),
            )
            for launch in process_factory.launches
        ]
        self.assertTrue(all(7890 not in pair and 9090 not in pair for pair in port_pairs))
        self.assertNotEqual(
            process_factory.launches[0]["config"]["secret"],
            process_factory.launches[1]["config"]["secret"],
        )
        self.assertFalse(process_factory.processes[1].alive)
        self.assertFalse(process_factory.launches[0]["work_dir"].exists())

    @staticmethod
    def _wait_for(predicate, timeout: float = 1.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return bool(predicate())

    @unittest.skipUnless(os.name == "nt", "Windows Job ownership is Windows-only")
    def test_windows_owner_returns_only_after_process_is_atomically_in_job(self) -> None:
        owner = _WindowsAtomicJobProcessOwner()
        process = None
        try:
            process = owner.launch(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                cwd=str(Path.cwd()),
            )
            snapshot = owner._job.snapshot()
            self.assertIn(process.pid, snapshot.process_ids)
            self.assertGreaterEqual(snapshot.active_processes, 1)
        finally:
            self.assertTrue(owner.terminate_and_wait())
        self.assertIsNotNone(process)
        self.assertIsNotNone(process.poll())


if __name__ == "__main__":
    unittest.main()
