import tempfile
import unittest
import socket
import threading
import time
import sys
import subprocess
import os
from pathlib import Path

from core_runner import CoreOutputEvent, MihomoCoreRunner


class FakeStdout:
    def __iter__(self):
        return iter(["mihomo ready\n"])


class FakeProcess:
    next_pid = 1000

    def __init__(self, command, **kwargs):
        self.command = command
        self.kwargs = kwargs
        self.pid = FakeProcess.next_pid
        FakeProcess.next_pid += 1
        self.stdout = FakeStdout()
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


class CoreRunnerTests(unittest.TestCase):
    def test_missing_binary_raises_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            config_path = temp_path / "mihomo.yaml"
            config_path.write_text("mixed-port: 7890\n", encoding="utf-8")
            runner = MihomoCoreRunner(
                binary_path="bin/mihomo.exe",
                runtime_config_path=str(config_path),
                work_dir="data/mihomo",
                project_root=temp_path,
                process_factory=FakeProcess,
            )

            with self.assertRaises(FileNotFoundError) as context:
                runner.start()
            self.assertIn("mihomo binary not found", str(context.exception))

    def test_builds_expected_command_and_starts_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            binary_path = temp_path / "bin" / "mihomo.exe"
            binary_path.parent.mkdir()
            binary_path.write_text("fake", encoding="utf-8")
            config_path = temp_path / "data" / "mihomo-runtime.yaml"
            config_path.parent.mkdir()
            config_path.write_text("mixed-port: 7890\n", encoding="utf-8")
            calls = []

            def factory(command, **kwargs):
                calls.append((command, kwargs))
                return FakeProcess(command, **kwargs)

            runner = MihomoCoreRunner(
                binary_path="bin/mihomo.exe",
                runtime_config_path="data/mihomo-runtime.yaml",
                work_dir="data/mihomo",
                project_root=temp_path,
                process_factory=factory,
            )

            status = runner.start()
            second_status = runner.start()

            self.assertTrue(status["running"])
            self.assertTrue(second_status["running"])
            self.assertEqual(len(calls), 1)
            self.assertEqual(
                calls[0][0],
                [str(binary_path.resolve()), "-f", str(config_path.resolve()), "-d", str((temp_path / "data" / "mihomo").resolve())],
            )
            safe_paths = calls[0][1]["env"]["SAFE_PATHS"].split(os.pathsep)
            self.assertIn(str(config_path.parent.resolve()), safe_paths)
            self.assertIn(str((temp_path / "data" / "mihomo").resolve()), safe_paths)

    def test_output_event_sink_receives_process_bound_raw_lines(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            binary_path = temp_path / "mihomo.exe"
            binary_path.write_text("fake", encoding="utf-8")
            config_path = temp_path / "mihomo.yaml"
            config_path.write_text("mixed-port: 7890\n", encoding="utf-8")
            received: list[CoreOutputEvent] = []
            delivered = threading.Event()

            class EventProcess(FakeProcess):
                def __init__(self, command, **kwargs):
                    super().__init__(command, **kwargs)
                    self.stdout = iter(["first line  \n", "second line\r\n"])

            def receive(event: CoreOutputEvent) -> None:
                received.append(event)
                if len(received) == 2:
                    delivered.set()

            runner = MihomoCoreRunner(
                binary_path=str(binary_path),
                runtime_config_path=str(config_path),
                work_dir="mihomo-work",
                project_root=temp_path,
                process_factory=EventProcess,
                output_event_sink=receive,
            )
            started_at = time.monotonic()

            status = runner.start()
            self.assertTrue(delivered.wait(1.0))
            process = runner.process

            self.assertIsNotNone(process)
            self.assertEqual(
                [event.raw_line for event in received],
                ["first line  ", "second line"],
            )
            self.assertEqual(
                [event.pid for event in received],
                [process.pid, process.pid],
            )
            self.assertEqual(
                [event.process_generation for event in received],
                [1, 1],
            )
            self.assertEqual(status["process_generation"], 1)
            self.assertGreaterEqual(received[0].received_at_monotonic, started_at)
            self.assertGreaterEqual(
                received[1].received_at_monotonic,
                received[0].received_at_monotonic,
            )
            self.assertIn("first line", runner.get_recent_logs())
            self.assertIn("second line", runner.get_recent_logs())
            self.assertTrue(runner.stop()["stopped"])

    def test_output_event_generation_changes_only_for_a_new_process(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            binary_path = temp_path / "mihomo.exe"
            binary_path.write_text("fake", encoding="utf-8")
            config_path = temp_path / "mihomo.yaml"
            config_path.write_text("mixed-port: 7890\n", encoding="utf-8")
            received: list[CoreOutputEvent] = []
            delivered = threading.Event()
            process_count = 0

            class GenerationProcess(FakeProcess):
                def __init__(self, command, **kwargs):
                    nonlocal process_count
                    super().__init__(command, **kwargs)
                    process_count += 1
                    self.stdout = iter([f"process {process_count}\n"])

            def receive(event: CoreOutputEvent) -> None:
                received.append(event)
                delivered.set()

            runner = MihomoCoreRunner(
                binary_path=str(binary_path),
                runtime_config_path=str(config_path),
                work_dir="mihomo-work",
                project_root=temp_path,
                process_factory=GenerationProcess,
                output_event_sink=receive,
            )

            first_status = runner.start()
            repeated_status = runner.start()
            self.assertTrue(delivered.wait(1.0))
            first_pid = received[-1].pid
            self.assertEqual(first_status["process_generation"], 1)
            self.assertEqual(repeated_status["process_generation"], 1)
            self.assertTrue(runner.stop()["stopped"])

            delivered.clear()
            second_status = runner.start()
            self.assertTrue(delivered.wait(1.0))

            self.assertEqual(second_status["process_generation"], 2)
            self.assertEqual(
                [event.process_generation for event in received],
                [1, 2],
            )
            self.assertNotEqual(received[-1].pid, first_pid)
            self.assertTrue(runner.stop()["stopped"])

    def test_output_event_sink_failure_does_not_stop_stdout_drain(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            binary_path = temp_path / "mihomo.exe"
            binary_path.write_text("fake", encoding="utf-8")
            config_path = temp_path / "mihomo.yaml"
            config_path.write_text("mixed-port: 7890\n", encoding="utf-8")
            attempted: list[str] = []
            drained = threading.Event()

            class EventProcess(FakeProcess):
                def __init__(self, command, **kwargs):
                    super().__init__(command, **kwargs)
                    self.stdout = iter(["one\n", "two\n", "three\n"])

            def failing_sink(event: CoreOutputEvent) -> None:
                attempted.append(event.raw_line)
                if len(attempted) == 3:
                    drained.set()
                raise RuntimeError("consumer failed")

            runner = MihomoCoreRunner(
                binary_path=str(binary_path),
                runtime_config_path=str(config_path),
                work_dir="mihomo-work",
                project_root=temp_path,
                process_factory=EventProcess,
                max_log_lines=3,
                output_event_sink=failing_sink,
            )

            runner.start()
            self.assertTrue(drained.wait(1.0))

            self.assertEqual(attempted, ["one", "two", "three"])
            self.assertEqual(runner.get_recent_logs(), ["one", "two", "three"])
            status = runner.stop()
            self.assertTrue(status["stopped"])
            self.assertFalse(status["still_running"])

    def test_stop_terminates_running_process(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            binary_path = temp_path / "mihomo.exe"
            binary_path.write_text("fake", encoding="utf-8")
            config_path = temp_path / "mihomo.yaml"
            config_path.write_text("mixed-port: 7890\n", encoding="utf-8")
            runner = MihomoCoreRunner(
                binary_path=str(binary_path),
                runtime_config_path=str(config_path),
                work_dir="mihomo-work",
                project_root=temp_path,
                process_factory=FakeProcess,
            )

            runner.start()
            process = runner.process
            status = runner.stop()

            self.assertFalse(status["running"])
            self.assertTrue(status["stopped"])
            self.assertFalse(status["killed"])
            self.assertFalse(status["still_running"])
            self.assertIsNotNone(process)
            self.assertTrue(process.terminated)
            self.assertIsNone(runner.process)

    def test_request_stop_signals_once_without_waiting_or_clearing_process(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            binary_path = temp_path / "mihomo.exe"
            binary_path.write_text("fake", encoding="utf-8")
            config_path = temp_path / "mihomo.yaml"
            config_path.write_text("mixed-port: 7890\n", encoding="utf-8")

            class PendingTerminateProcess(FakeProcess):
                def terminate(self):
                    self.terminated = True

                def wait(self, timeout=None):
                    raise AssertionError("request_stop must not wait")

            runner = MihomoCoreRunner(
                binary_path=str(binary_path),
                runtime_config_path=str(config_path),
                work_dir="mihomo-work",
                project_root=temp_path,
                process_factory=PendingTerminateProcess,
            )

            runner.start()
            process = runner.process
            first = runner.request_stop()
            second = runner.request_stop()

            self.assertIsNotNone(process)
            self.assertTrue(first["requested"])
            self.assertTrue(first["signal_sent"])
            self.assertFalse(first["already_requested"])
            self.assertEqual(first["pid"], process.pid)
            self.assertTrue(second["requested"])
            self.assertFalse(second["signal_sent"])
            self.assertTrue(second["already_requested"])
            self.assertIs(runner.process, process)

    def test_request_stop_preempts_process_creation_without_waiting_for_start_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            binary_path = temp_path / "mihomo.exe"
            binary_path.write_text("fake", encoding="utf-8")
            config_path = temp_path / "mihomo.yaml"
            config_path.write_text("mixed-port: 7890\n", encoding="utf-8")
            factory_entered = threading.Event()
            release_factory = threading.Event()
            created: list[FakeProcess] = []
            start_results: list[dict[str, object]] = []

            def blocking_factory(command, **kwargs):
                factory_entered.set()
                release_factory.wait(timeout=2.0)
                process = FakeProcess(command, **kwargs)
                created.append(process)
                return process

            runner = MihomoCoreRunner(
                binary_path=str(binary_path),
                runtime_config_path=str(config_path),
                work_dir="mihomo-work",
                project_root=temp_path,
                process_factory=blocking_factory,
            )
            worker = threading.Thread(
                target=lambda: start_results.append(runner.start()),
                daemon=True,
            )
            worker.start()
            self.assertTrue(factory_entered.wait(1.0))

            started_at = time.monotonic()
            request = runner.request_stop()
            elapsed = time.monotonic() - started_at

            self.assertLess(elapsed, 0.1)
            self.assertTrue(request["requested"])
            self.assertIsNone(request["pid"])
            release_factory.set()
            worker.join(timeout=1.0)

            self.assertFalse(worker.is_alive())
            self.assertEqual(len(created), 1)
            self.assertTrue(created[0].terminated)
            self.assertTrue(start_results[0]["start_cancelled"])
            self.assertTrue(start_results[0]["stopped"])
            self.assertIsNone(runner.process)

    def test_stop_confirms_exit_after_kill_with_a_separate_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            binary_path = temp_path / "mihomo.exe"
            binary_path.write_text("fake", encoding="utf-8")
            config_path = temp_path / "mihomo.yaml"
            config_path.write_text("mixed-port: 7890\n", encoding="utf-8")

            class SlowKillProcess(FakeProcess):
                def __init__(self, command, **kwargs):
                    super().__init__(command, **kwargs)
                    self.wait_timeouts = []

                def terminate(self):
                    self.terminated = True

                def wait(self, timeout=None):
                    self.wait_timeouts.append(timeout)
                    if self.killed:
                        self.returncode = -9
                        return self.returncode
                    raise subprocess.TimeoutExpired(self.command, timeout)

                def kill(self):
                    self.killed = True

            runner = MihomoCoreRunner(
                binary_path=str(binary_path),
                runtime_config_path=str(config_path),
                work_dir="mihomo-work",
                project_root=temp_path,
                process_factory=SlowKillProcess,
            )

            runner.start()
            process = runner.process
            status = runner.stop(timeout=0.1)

            self.assertFalse(status["running"])
            self.assertTrue(status["stopped"])
            self.assertTrue(status["killed"])
            self.assertTrue(status["kill_requested"])
            self.assertFalse(status["still_running"])
            self.assertIsNone(runner.process)
            self.assertIsNotNone(process)
            self.assertEqual(len(process.wait_timeouts), 2)
            self.assertGreater(process.wait_timeouts[0], 0.0)
            self.assertGreater(process.wait_timeouts[1], 0.0)
            self.assertLessEqual(sum(process.wait_timeouts), 0.1)

    def test_stop_retains_process_and_pid_when_kill_cannot_be_confirmed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            binary_path = temp_path / "mihomo.exe"
            binary_path.write_text("fake", encoding="utf-8")
            config_path = temp_path / "mihomo.yaml"
            config_path.write_text("mixed-port: 7890\n", encoding="utf-8")

            class StubbornProcess(FakeProcess):
                def terminate(self):
                    self.terminated = True

                def kill(self):
                    self.killed = True

                def wait(self, timeout=None):
                    raise subprocess.TimeoutExpired(self.command, timeout)

            runner = MihomoCoreRunner(
                binary_path=str(binary_path),
                runtime_config_path=str(config_path),
                work_dir="mihomo-work",
                project_root=temp_path,
                process_factory=StubbornProcess,
            )

            runner.start()
            process = runner.process
            status = runner.stop(timeout=0.01)

            self.assertTrue(status["running"])
            self.assertFalse(status["stopped"])
            self.assertFalse(status["killed"])
            self.assertTrue(status["kill_requested"])
            self.assertTrue(status["still_running"])
            self.assertIsNotNone(process)
            self.assertEqual(status["pid"], process.pid)
            self.assertIs(runner.process, process)
            self.assertIn(
                "mihomo did not exit after kill request",
                runner.get_recent_logs(),
            )

    def test_stop_does_not_block_reader_log_append_while_waiting(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            binary_path = temp_path / "mihomo.exe"
            binary_path.write_text("fake", encoding="utf-8")
            config_path = temp_path / "mihomo.yaml"
            config_path.write_text("mixed-port: 7890\n", encoding="utf-8")
            append_started = threading.Event()
            append_done = threading.Event()
            runner: MihomoCoreRunner

            class LoggingDuringWaitProcess(FakeProcess):
                def terminate(self):
                    self.terminated = True

                def wait(self, timeout=None):
                    def append_from_reader():
                        append_started.set()
                        runner._append_log("reader drained during stop")
                        append_done.set()

                    thread = threading.Thread(target=append_from_reader, daemon=True)
                    thread.start()
                    self.returncode = 0
                    self.assert_append_unblocked()
                    return self.returncode

                def assert_append_unblocked(self):
                    if not append_started.wait(1.0) or not append_done.wait(1.0):
                        raise AssertionError("reader log append was blocked by stop lock")

            runner = MihomoCoreRunner(
                binary_path=str(binary_path),
                runtime_config_path=str(config_path),
                work_dir="mihomo-work",
                project_root=temp_path,
                process_factory=LoggingDuringWaitProcess,
            )

            runner.start()
            status = runner.stop(timeout=1.0)

            self.assertFalse(status["running"])
            self.assertIn("reader drained during stop", runner.get_recent_logs())

    def test_stop_bounds_reader_join_and_reports_undrained_reader(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            binary_path = temp_path / "mihomo.exe"
            binary_path.write_text("fake", encoding="utf-8")
            config_path = temp_path / "mihomo.yaml"
            config_path.write_text("mixed-port: 7890\n", encoding="utf-8")
            runner = MihomoCoreRunner(
                binary_path=str(binary_path),
                runtime_config_path=str(config_path),
                work_dir="mihomo-work",
                project_root=temp_path,
                process_factory=FakeProcess,
            )

            class UndrainedReader:
                def __init__(self) -> None:
                    self.join_timeouts: list[float] = []

                def is_alive(self) -> bool:
                    return True

                def join(self, timeout=None) -> None:
                    self.join_timeouts.append(timeout)

            runner.start()
            process = runner.process
            self.assertIsNotNone(process)
            reader = UndrainedReader()
            runner._reader_thread = reader

            status = runner.stop(timeout=1.0)

            self.assertFalse(status["running"])
            self.assertFalse(status["stopped"])
            self.assertFalse(status["reader_drained"])
            self.assertTrue(status["cleanup_pending"])
            self.assertEqual(status["pid"], process.pid)
            self.assertIs(runner.process, process)
            self.assertIs(runner._reader_thread, reader)
            self.assertEqual(len(reader.join_timeouts), 2)
            self.assertGreater(sum(reader.join_timeouts), 0.0)
            self.assertLessEqual(sum(reader.join_timeouts), 0.1)

    def test_wait_for_proxy_discards_success_that_races_cancellation(self) -> None:
        runner = MihomoCoreRunner(
            binary_path="mihomo.exe",
            runtime_config_path="mihomo.yaml",
            work_dir="mihomo-work",
            project_root=Path.cwd(),
        )
        runner.process = FakeProcess(["mihomo"])
        cancelled = threading.Event()

        def successful_probe(*_args, **_kwargs):
            cancelled.set()
            runner.proxy_ready = True
            return True

        runner.check_proxy_port = successful_probe

        self.assertFalse(
            runner.wait_for_proxy(
                timeout=0.5,
                interval=0.5,
                cancel_event=cancelled,
            )
        )
        self.assertFalse(runner.proxy_ready)
        self.assertIn("cancelled", str(runner.last_health_error))

    def test_restart_does_not_launch_again_when_previous_process_survives_kill(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            binary_path = temp_path / "mihomo.exe"
            binary_path.write_text("fake", encoding="utf-8")
            config_path = temp_path / "mihomo.yaml"
            config_path.write_text("mixed-port: 7890\n", encoding="utf-8")
            calls: list[FakeProcess] = []

            class StubbornProcess(FakeProcess):
                def terminate(self):
                    self.terminated = True

                def kill(self):
                    self.killed = True

                def wait(self, timeout=None):
                    raise subprocess.TimeoutExpired(self.command, timeout)

            def factory(command, **kwargs):
                process = StubbornProcess(command, **kwargs)
                calls.append(process)
                return process

            runner = MihomoCoreRunner(
                binary_path=str(binary_path),
                runtime_config_path=str(config_path),
                work_dir="mihomo-work",
                project_root=temp_path,
                process_factory=factory,
            )
            runner.start()

            result = runner.restart()

            self.assertFalse(result["restarted"])
            self.assertTrue(result["still_running"])
            self.assertEqual(len(calls), 1)
            self.assertIs(runner.process, calls[0])

    def test_get_version_uses_binary_version_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            binary_path = temp_path / "mihomo.exe"
            binary_path.write_text("fake", encoding="utf-8")

            class Result:
                returncode = 0
                stdout = "Mihomo Meta v1.2.3\nUse tags: test\n"
                stderr = ""

            calls = []

            def command_runner(command, **kwargs):
                calls.append((command, kwargs))
                return Result()

            runner = MihomoCoreRunner(
                binary_path=str(binary_path),
                runtime_config_path="missing.yaml",
                work_dir="work",
                project_root=temp_path,
                command_runner=command_runner,
            )

            version = runner.get_version()

            self.assertEqual(version, "Mihomo Meta v1.2.3")
            self.assertEqual(calls[0][0], [str(binary_path), "-v"])
            if sys.platform == "win32":
                self.assertIn("creationflags", calls[0][1])
                self.assertIn("startupinfo", calls[0][1])
            self.assertEqual(runner.status()["version"], "Mihomo Meta v1.2.3")

    def test_check_proxy_port_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            binary_path = temp_path / "mihomo.exe"
            binary_path.write_text("fake", encoding="utf-8")
            config_path = temp_path / "mihomo.yaml"
            config_path.write_text("mixed-port: 7890\n", encoding="utf-8")
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]

            def accept_once():
                conn, _ = listener.accept()
                conn.close()
                listener.close()

            thread = threading.Thread(target=accept_once, daemon=True)
            thread.start()
            runner = MihomoCoreRunner(
                binary_path=str(binary_path),
                runtime_config_path=str(config_path),
                work_dir="work",
                project_root=temp_path,
                process_factory=FakeProcess,
            )
            runner.start()

            self.assertTrue(runner.check_proxy_port(port=port))
            self.assertTrue(runner.status()["proxy_ready"])

    def test_wait_for_proxy_timeout_sets_health_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            binary_path = temp_path / "mihomo.exe"
            binary_path.write_text("fake", encoding="utf-8")
            config_path = temp_path / "mihomo.yaml"
            config_path.write_text("mixed-port: 7890\n", encoding="utf-8")
            runner = MihomoCoreRunner(
                binary_path=str(binary_path),
                runtime_config_path=str(config_path),
                work_dir="work",
                project_root=temp_path,
                process_factory=FakeProcess,
            )
            runner.start()

            self.assertFalse(runner.wait_for_proxy(port=9, timeout=0.2, interval=0.05))
            self.assertFalse(runner.status()["proxy_ready"])
            self.assertTrue(runner.status()["last_health_error"])

    def test_wait_for_proxy_cancel_event_interrupts_interval_wait(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            binary_path = temp_path / "mihomo.exe"
            binary_path.write_text("fake", encoding="utf-8")
            config_path = temp_path / "mihomo.yaml"
            config_path.write_text("mixed-port: 7890\n", encoding="utf-8")
            runner = MihomoCoreRunner(
                binary_path=str(binary_path),
                runtime_config_path=str(config_path),
                work_dir="work",
                project_root=temp_path,
                process_factory=FakeProcess,
            )
            runner.start()
            probe_completed = threading.Event()
            cancel_event = threading.Event()
            result: list[bool] = []

            def failed_probe(*_args, **_kwargs) -> bool:
                probe_completed.set()
                return False

            runner.check_proxy_port = failed_probe
            wait_thread = threading.Thread(
                target=lambda: result.append(
                    runner.wait_for_proxy(
                        timeout=5.0,
                        interval=1.0,
                        cancel_event=cancel_event,
                    )
                ),
                daemon=True,
            )
            wait_thread.start()
            self.assertTrue(probe_completed.wait(1.0))

            cancel_event.set()
            wait_thread.join(timeout=0.25)

            self.assertFalse(wait_thread.is_alive())
            self.assertEqual(result, [False])
            self.assertEqual(
                runner.status()["last_health_error"],
                "mihomo proxy readiness wait cancelled",
            )


if __name__ == "__main__":
    unittest.main()
