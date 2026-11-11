from __future__ import annotations

import json
import hashlib
import os
import subprocess
import threading
import socket
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TextIO
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from windows_subprocess import hidden_subprocess_kwargs


ProcessFactory = Callable[..., subprocess.Popen]
CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
_VERSION_CACHE_LOCK = threading.Lock()
_VERSION_CACHE: OrderedDict[tuple[Any, ...], tuple[str, str]] = OrderedDict()


@dataclass(frozen=True, slots=True)
class CoreOutputEvent:
    """One unparsed line received from a specific mihomo process."""

    raw_line: str
    pid: int
    process_generation: int
    received_at_monotonic: float


OutputEventSink = Callable[[CoreOutputEvent], None]


class MihomoCoreRunner:
    def __init__(
        self,
        *,
        binary_path: str,
        runtime_config_path: str,
        work_dir: str,
        project_root: str | Path | None = None,
        process_factory: ProcessFactory = subprocess.Popen,
        command_runner: CommandRunner = subprocess.run,
        max_log_lines: int = 300,
        output_event_sink: OutputEventSink | None = None,
    ) -> None:
        self.project_root = Path(project_root or Path.cwd()).resolve()
        self.binary_path = self._resolve_path(binary_path)
        self.runtime_config_path = self._resolve_path(runtime_config_path)
        self.work_dir = self._resolve_path(work_dir)
        self.process_factory = process_factory
        self.command_runner = command_runner
        # The reader calls this synchronously. Callers must only enqueue or do
        # similarly bounded, non-blocking work here.
        self.output_event_sink = output_event_sink
        self.logs: deque[str] = deque(maxlen=max_log_lines)
        self.process: subprocess.Popen | None = None
        self._process_job: Any = None
        self._process_generation = 0
        self._reader_thread: threading.Thread | None = None
        self._stop_requested_process: subprocess.Popen | None = None
        self._terminate_signal_sent_process: subprocess.Popen | None = None
        self._lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
        self._intent_lock = threading.RLock()
        self._stop_request_event = threading.Event()
        self._stop_cleanup_complete = True
        self._intent_revision = 0
        self.version: str | None = None
        self.proxy_ready = False
        self.last_health_error: str | None = None

    def _resolve_path(self, value: str) -> Path:
        path = Path(value).expanduser()
        if path.is_absolute():
            return path
        return (self.project_root / path).resolve()

    def build_command(self) -> list[str]:
        return [
            str(self.binary_path),
            "-f",
            str(self.runtime_config_path),
            "-d",
            str(self.work_dir),
        ]

    def _build_environment(self) -> dict[str, str]:
        env = os.environ.copy()
        existing = [
            item
            for item in str(env.get("SAFE_PATHS", "")).split(os.pathsep)
            if item
        ]
        safe_paths = [str(self.runtime_config_path.parent), str(self.work_dir)]
        for path in safe_paths:
            if path not in existing:
                existing.append(path)
        env["SAFE_PATHS"] = os.pathsep.join(existing)
        return env

    def is_running(self) -> bool:
        with self._lock:
            return self.process is not None and self.process.poll() is None

    def start(self) -> dict[str, object]:
        with self._lifecycle_lock:
            with self._intent_lock:
                if (
                    self._stop_request_event.is_set()
                    and not self._stop_cleanup_complete
                ):
                    status = self.status()
                    status["start_cancelled"] = True
                    return status
                self._stop_request_event.clear()
                self._stop_cleanup_complete = True
                self._intent_revision += 1
                start_revision = self._intent_revision
            return self._start(start_revision=start_revision)

    def _start_intent_is_current(self, start_revision: int) -> bool:
        with self._intent_lock:
            return bool(
                not self._stop_request_event.is_set()
                and self._intent_revision == int(start_revision)
            )

    def _start(self, *, start_revision: int) -> dict[str, object]:
        if self.is_running():
            return self.status()
        if not self._start_intent_is_current(start_revision):
            status = self.status()
            status["start_cancelled"] = True
            return status
        if not self.binary_path.exists():
            raise FileNotFoundError(f"mihomo binary not found: {self.binary_path}")
        if not self.runtime_config_path.exists():
            raise FileNotFoundError(f"mihomo runtime config not found: {self.runtime_config_path}")

        self.work_dir.mkdir(parents=True, exist_ok=True)
        if not self._start_intent_is_current(start_revision):
            status = self.status()
            status["start_cancelled"] = True
            return status
        command = self.build_command()
        hidden_kwargs = hidden_subprocess_kwargs()
        process = self.process_factory(
            command,
            cwd=str(self.project_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=self._build_environment(),
            **hidden_kwargs,
        )
        self._bind_process_to_job(process)
        with self._lock:
            self.process = process
            self._process_generation += 1
            process_generation = self._process_generation
            self._stop_requested_process = None
            self._terminate_signal_sent_process = None
            self._append_log(f"Started mihomo: {' '.join(command)}")
            self._start_reader_thread(
                process=process,
                process_generation=process_generation,
            )
            self.proxy_ready = False
            self.last_health_error = None
        if not self._start_intent_is_current(start_revision):
            self.request_stop()
            status = self._stop(timeout=0.25)
            status["start_cancelled"] = True
            return status
        return self.status()

    def request_stop(self) -> dict[str, object]:
        """Signal the current core without waiting for process cleanup."""

        with self._intent_lock:
            first_request = not self._stop_request_event.is_set()
            if first_request:
                self._intent_revision += 1
                self._stop_request_event.set()
                self._stop_cleanup_complete = False
            process = self.process
            if process is None:
                return {
                    "requested": True,
                    "signal_sent": False,
                    "already_requested": not first_request,
                    "still_running": False,
                    "pid": None,
                }
            pid = process.pid
            already_requested = self._stop_requested_process is process
            if already_requested:
                return {
                    "requested": True,
                    "signal_sent": False,
                    "already_requested": True,
                    "still_running": process.poll() is None,
                    "pid": pid,
                }
            if process.poll() is not None:
                return {
                    "requested": False,
                    "signal_sent": False,
                    "already_requested": False,
                    "still_running": False,
                    "pid": pid,
                }
            self._stop_requested_process = process
            self.proxy_ready = False
            signal_sent = False
            try:
                process.terminate()
                signal_sent = True
                self._terminate_signal_sent_process = process
            except OSError:
                if process.poll() is None:
                    self._stop_requested_process = None
            return {
                "requested": True,
                "signal_sent": signal_sent,
                "already_requested": False,
                "still_running": process.poll() is None,
                "pid": pid,
            }

    def stop(self, timeout: float = 5.0) -> dict[str, object]:
        self.request_stop()
        with self._lifecycle_lock:
            return self._stop(timeout=timeout)

    def _complete_stop_request(self) -> None:
        with self._intent_lock:
            self._stop_cleanup_complete = True
            self._stop_request_event.clear()
            self._stop_requested_process = None
            self._terminate_signal_sent_process = None

    def _stop(self, *, timeout: float) -> dict[str, object]:
        terminate_timeout, kill_timeout, reader_timeout = self._stop_phase_budgets(
            timeout
        )
        with self._lock:
            process = self.process
            if process is None:
                status = self.status()
                status.update(
                    {
                        "stopped": True,
                        "killed": False,
                        "kill_requested": False,
                        "still_running": False,
                        "reader_drained": True,
                        "cleanup_pending": False,
                    }
                )
                self._complete_stop_request()
                return status
            reader_thread = self._reader_thread
            already_exited = process.poll() is not None
            if not already_exited:
                self.proxy_ready = False
        with self._intent_lock:
            terminate_signal_sent = (
                self._terminate_signal_sent_process is process
            )

        if already_exited:
            reader_drained = self._finalize_confirmed_process(
                process,
                reader_thread=reader_thread,
                reader_timeout=reader_timeout,
            )
            status = self.status()
            cleanup_pending = not reader_drained
            status.update(
                {
                    "stopped": reader_drained,
                    "killed": False,
                    "kill_requested": False,
                    "still_running": False,
                    "reader_drained": reader_drained,
                    "cleanup_pending": cleanup_pending,
                    "pid": process.pid if cleanup_pending else status.get("pid"),
                }
            )
            if reader_drained:
                self._complete_stop_request()
            return status

        process_exited = False
        killed = False
        kill_requested = False
        log_line = "mihomo did not exit after kill request"
        if not terminate_signal_sent:
            try:
                process.terminate()
                with self._intent_lock:
                    if self.process is process:
                        self._stop_requested_process = process
                        self._terminate_signal_sent_process = process
            except OSError:
                pass
        if self._wait_for_process_exit(process, timeout=terminate_timeout):
            process_exited = True
            log_line = "Stopped mihomo"
        else:
            kill_requested = True
            try:
                process.kill()
            except OSError:
                pass
            if self._wait_for_process_exit(process, timeout=kill_timeout):
                process_exited = True
                killed = True
                log_line = "Killed mihomo after graceful stop timed out"

        reader_drained = False
        if process_exited:
            reader_drained = self._finalize_confirmed_process(
                process,
                reader_thread=reader_thread,
                reader_timeout=reader_timeout,
            )
            if not reader_drained:
                log_line = "mihomo exited but its output reader did not drain"
        self._append_log(log_line)
        status = self.status()
        still_running = bool(
            self.process is process and process.poll() is None
        )
        cleanup_pending = bool(process_exited and not reader_drained)
        fully_stopped = bool(process_exited and reader_drained and not still_running)
        status.update(
            {
                "stopped": fully_stopped,
                "killed": killed and not still_running,
                "kill_requested": kill_requested,
                "still_running": still_running,
                "reader_drained": reader_drained,
                "cleanup_pending": cleanup_pending,
                "pid": (
                    process.pid
                    if still_running or cleanup_pending
                    else status.get("pid")
                ),
            }
        )
        if fully_stopped:
            self._complete_stop_request()
        return status

    def restart(self) -> dict[str, object]:
        with self._lifecycle_lock:
            self.request_stop()
            stop_result = self._stop(timeout=5.0)
            if (
                not bool(stop_result.get("stopped"))
                or bool(stop_result.get("still_running"))
                or bool(stop_result.get("cleanup_pending"))
            ):
                stop_result["restarted"] = False
                stop_result["restart_error"] = (
                    "mihomo restart aborted because the previous process "
                    "did not stop cleanly"
                )
                return stop_result
            return self.start()

    def hot_reload(
        self,
        *,
        controller_host: str = "127.0.0.1",
        controller_port: int = 9090,
        controller_secret: str = "",
        timeout: float = 5.0,
    ) -> dict[str, object]:
        """Ask the running mihomo core to swap its runtime config in-place.

        Returns a dict with ``reloaded: bool``. Callers should fall back to
        ``restart()`` when reload returns False (core not running, controller
        disabled, or API rejected the request).
        """
        if not self.is_running():
            return {"reloaded": False, "reason": "core not running"}
        url = f"http://{controller_host}:{int(controller_port)}/configs?force=true"
        body = json.dumps({"path": str(self.runtime_config_path)}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if controller_secret:
            headers["Authorization"] = f"Bearer {controller_secret}"
        request = Request(url, data=body, headers=headers, method="PUT")
        try:
            with urlopen(request, timeout=max(0.5, float(timeout))) as response:
                response.read()
            self._append_log(f"Hot-reloaded config via {url}")
            return {"reloaded": True}
        except HTTPError as exc:
            detail = ""
            try:
                raw_detail = exc.read()
                if raw_detail:
                    detail = raw_detail.decode("utf-8", errors="replace").strip()
            except Exception:
                detail = ""
            reason = f"HTTP Error {exc.code}: {exc.reason}"
            if detail:
                reason = f"{reason}: {detail[:1000]}"
            self._append_log(f"Hot reload failed: {reason}")
            return {"reloaded": False, "reason": reason}
        except (URLError, OSError, ValueError) as exc:
            self._append_log(f"Hot reload failed: {exc}")
            return {"reloaded": False, "reason": str(exc)}

    def lightweight_fact(self):
        """Copy process facts under _lock; no logs, command, or HTTP reads."""
        from types import MappingProxyType

        with self._lock:
            process = self.process
            running = process is not None and process.poll() is None
            reader = self._reader_thread
            cleanup_pending = bool(
                process is not None and not running
                and reader is not None and reader.is_alive()
            )
            return MappingProxyType({
                "running": running,
                "pid": process.pid if process is not None and (running or cleanup_pending) else None,
                "process_generation": self._process_generation,
                "cleanup_pending": cleanup_pending,
            })

    def status(self) -> dict[str, object]:
        with self._lock:
            process = self.process
            running = process is not None and process.poll() is None
            reader = self._reader_thread
            cleanup_pending = bool(
                process is not None
                and not running
                and reader is not None
                and reader.is_alive()
            )
            return {
                "running": running,
                "process_running": running,
                "proxy_ready": self.proxy_ready if running else False,
                "version": self.version,
                "last_health_error": self.last_health_error,
                "pid": process.pid if process is not None and (running or cleanup_pending) else None,
                "process_generation": self._process_generation,
                "cleanup_pending": cleanup_pending,
                "reader_drained": not cleanup_pending,
                "binary_path": str(self.binary_path),
                "runtime_config_path": str(self.runtime_config_path),
                "work_dir": str(self.work_dir),
                "command": self.build_command(),
                "recent_logs": self.get_recent_logs(),
            }

    def get_version(self, timeout: float = 5.0) -> str:
        if not self.binary_path.exists():
            raise FileNotFoundError(f"mihomo binary not found: {self.binary_path}")
        stat = self.binary_path.stat()
        identity = (str(self.binary_path.resolve()), stat.st_dev, stat.st_ino,
                    stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, self.command_runner)
        with _VERSION_CACHE_LOCK:
            cached = _VERSION_CACHE.get(identity)
            if cached is None:
                with self.binary_path.open("rb") as handle:
                    digest = hashlib.file_digest(handle, "sha256").hexdigest()
                version = self._query_version(timeout)
                after = self.binary_path.stat()
                if (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (
                    stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns
                ):
                    raise RuntimeError("Mihomo binary changed during version lookup")
                cached = (version, digest)
                _VERSION_CACHE[identity] = cached
                while len(_VERSION_CACHE) > 16:
                    _VERSION_CACHE.popitem(last=False)
            _VERSION_CACHE.move_to_end(identity)
        with self._lock:
            self.version, self.version_binary_sha256 = cached
        return self.version

    def _query_version(self, timeout: float) -> str:
        result = self.command_runner(
            [str(self.binary_path), "-v"],
            cwd=str(self.project_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            **hidden_subprocess_kwargs(),
        )
        output = "\n".join(part.strip() for part in [result.stdout, result.stderr] if part and part.strip()).strip()
        if result.returncode != 0:
            raise RuntimeError(output or f"mihomo -v failed with exit code {result.returncode}")
        return output.splitlines()[0] if output else ""

    def check_proxy_port(self, host: str = "127.0.0.1", port: int = 7890, timeout: float = 1.0) -> bool:
        try:
            with socket.create_connection((host, int(port)), timeout=timeout):
                pass
            with self._lock:
                self.proxy_ready = True
                self.last_health_error = None
            return True
        except OSError as exc:
            with self._lock:
                self.proxy_ready = False
                self.last_health_error = str(exc)
            return False

    def wait_for_proxy(
        self,
        host: str = "127.0.0.1",
        port: int = 7890,
        timeout: float = 30.0,
        interval: float = 0.05,
        cancel_event: threading.Event | None = None,
    ) -> bool:
        deadline = time.monotonic() + max(0.1, timeout)
        while time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                with self._lock:
                    self.proxy_ready = False
                    self.last_health_error = "mihomo proxy readiness wait cancelled"
                return False
            if not self.is_running():
                with self._lock:
                    self.proxy_ready = False
                    self.last_health_error = "mihomo process exited before proxy port became ready"
                return False
            probe_timeout = min(
                max(0.01, float(interval)),
                max(0.01, deadline - time.monotonic()),
                0.2,
            )
            if self.check_proxy_port(host, port, timeout=probe_timeout):
                if cancel_event is not None and cancel_event.is_set():
                    with self._lock:
                        self.proxy_ready = False
                        self.last_health_error = "mihomo proxy readiness wait cancelled"
                    return False
                return True
            wait_interval = min(
                max(0.0, float(interval)),
                max(0.0, deadline - time.monotonic()),
            )
            if cancel_event is not None:
                if cancel_event.wait(wait_interval):
                    with self._lock:
                        self.proxy_ready = False
                        self.last_health_error = "mihomo proxy readiness wait cancelled"
                    return False
            else:
                time.sleep(wait_interval)
        with self._lock:
            if not self.last_health_error:
                self.last_health_error = f"timed out waiting for {host}:{port}"
            self.proxy_ready = False
        return False

    def get_recent_logs(self, limit: int = 100) -> list[str]:
        with self._lock:
            return list(self.logs)[-max(1, int(limit)) :]

    def _start_reader_thread(
        self,
        *,
        process: subprocess.Popen,
        process_generation: int,
    ) -> None:
        if process.stdout is None:
            return
        self._reader_thread = threading.Thread(
            target=self._read_output,
            args=(process.stdout, int(process.pid), int(process_generation)),
            name="mihomo-output-reader",
            daemon=True,
        )
        self._reader_thread.start()

    def _read_output(
        self,
        stream: TextIO,
        process_pid: int,
        process_generation: int,
    ) -> None:
        try:
            for line in stream:
                self._append_log(line.rstrip())
                sink = self.output_event_sink
                if sink is None:
                    continue
                event = CoreOutputEvent(
                    raw_line=line.rstrip("\r\n"),
                    pid=int(process_pid),
                    process_generation=int(process_generation),
                    received_at_monotonic=time.monotonic(),
                )
                try:
                    sink(event)
                except Exception:
                    # A consumer failure must never stop draining core stdout.
                    continue
        except Exception as exc:
            if not self._stop_request_event.is_set():
                self._append_log(f"mihomo log reader stopped: {exc}")

    @staticmethod
    def _stop_phase_budgets(timeout: float) -> tuple[float, float, float]:
        """Split a bounded stop window without starving kill confirmation."""

        stop_window = min(1.0, max(0.0, float(timeout)))
        if stop_window <= 0.0:
            return 0.0, 0.0, 0.0
        reader_timeout = stop_window * 0.10
        terminate_timeout = min(0.25, stop_window * 0.35)
        kill_timeout = max(0.0, stop_window - terminate_timeout - reader_timeout)
        return terminate_timeout, kill_timeout, reader_timeout

    @staticmethod
    def _wait_for_process_exit(
        process: subprocess.Popen,
        *,
        timeout: float,
    ) -> bool:
        if process.poll() is not None:
            return True
        try:
            process.wait(timeout=max(0.0, float(timeout)))
        except (subprocess.TimeoutExpired, OSError, ValueError):
            pass
        return process.poll() is not None

    def _finalize_confirmed_process(
        self,
        process: subprocess.Popen,
        *,
        reader_thread: threading.Thread | None,
        reader_timeout: float,
    ) -> bool:
        if process.poll() is None:
            return False

        if (
            reader_thread is not None
            and reader_thread is not threading.current_thread()
            and reader_thread.is_alive()
        ):
            first_join = max(0.0, float(reader_timeout)) * 0.5
            reader_thread.join(timeout=first_join)
        stream = process.stdout
        if (
            reader_thread is not None
            and reader_thread is not threading.current_thread()
            and reader_thread.is_alive()
            and stream is not None
        ):
            close_stream = getattr(stream, "close", None)
            try:
                if callable(close_stream):
                    close_stream()
            except (OSError, ValueError):
                pass
            reader_thread.join(
                timeout=max(0.0, float(reader_timeout)) - first_join
            )
        reader_drained = reader_thread is None or not reader_thread.is_alive()
        if reader_drained and stream is not None:
            close_stream = getattr(stream, "close", None)
            try:
                if callable(close_stream):
                    close_stream()
            except (OSError, ValueError):
                pass

        with self._lock:
            if (
                reader_drained
                and self.process is process
                and process.poll() is not None
            ):
                self.process = None
                self._release_process_job()
            if self._reader_thread is reader_thread and reader_drained:
                self._reader_thread = None
            self.proxy_ready = False
        return reader_drained

    def _bind_process_to_job(self, process: subprocess.Popen) -> None:
        """Tie the Core's lifetime to this app's, as helper processes already are.

        Without it the Core is the only child that survives a force-killed or
        crashed app, and it keeps holding the mixed and controller ports, so
        every later start fails its listener preflight until the machine is
        rebooted (observed 2026-09-20 04:31-04:35, 16 consecutive failures).
        The Job sets KILL_ON_JOB_CLOSE, so losing our handle kills the Core.
        """

        pid = getattr(process, "pid", None)
        if os.name != "nt" or not pid:
            return
        try:
            from tier3_windows_job import WindowsJob

            job = WindowsJob()
            job.assign_pid(int(pid))
        except Exception:
            # A Core that cannot be tied to a Job still has to run; the only
            # loss is that a force-kill can orphan it, exactly as before.
            self._append_log("Could not bind mihomo to a Windows job object")
            return
        with self._lock:
            previous, self._process_job = self._process_job, job
        if previous is not None:
            try:
                previous.close()
            except Exception:
                pass

    def _release_process_job(self) -> None:
        """Drop the job handle once its Core is confirmed gone."""

        job, self._process_job = self._process_job, None
        if job is None:
            return
        try:
            job.close()
        except Exception:
            pass

    def _append_log(self, line: str) -> None:
        if not line:
            return
        with self._lock:
            self.logs.append(line)
