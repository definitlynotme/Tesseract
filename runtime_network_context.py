from __future__ import annotations

import csv
import hashlib
import io
import os
import subprocess
import tempfile
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from typing import Callable


_EXTERNAL_PROXY_PROCESS_NAMES = {
    "clash verge.exe",
    "clash-verge.exe",
    "clash-verge-service.exe",
    "verge-mihomo.exe",
}


@dataclass(frozen=True, slots=True)
class RuntimeNetworkContextSnapshot:
    captured_at: str
    captured_monotonic_ns: int
    runtime_generation: str
    state: str
    related_processes: tuple[dict[str, object], ...]
    adapter_summary: tuple[str, ...]
    default_route_summary: tuple[str, ...]
    fake_ip_route_summary: tuple[str, ...]
    dns_summary: tuple[str, ...]
    system_proxy_enabled: bool | None
    system_proxy_digest: str
    errors: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _run_readonly_command(
    argv: tuple[str, ...], timeout: float = 1.5,
    *, cancel_event: threading.Event | None = None,
) -> str:
    allowed = {
        ("tasklist.exe", "/fo", "csv", "/nh"),
        ("ipconfig.exe", "/all"),
        ("route.exe", "print", "-4"),
    }
    normalized = tuple(str(part).lower() if index == 0 else str(part).lower() for index, part in enumerate(argv))
    if normalized not in allowed:
        raise ValueError(f"network context command is not read-only allowlisted: {argv!r}")
    creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    deadline = time.monotonic() + max(0.1, float(timeout))
    if cancel_event is not None and cancel_event.is_set():
        raise RuntimeError("network context capture cancelled")
    # Windows communicate() can join pipe reader threads without a deadline
    # after timeout. Files avoid that unbounded cleanup during helper startup.
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen(
            list(argv), stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
            creationflags=creationflags,
        )
        try:
            while process.poll() is None:
                if cancel_event is not None and cancel_event.is_set():
                    raise RuntimeError("network context capture cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, timeout)
                try:
                    process.wait(timeout=min(0.05, remaining))
                except subprocess.TimeoutExpired:
                    pass
            stdout.seek(0)
            stderr.seek(0)
            if process.returncode != 0:
                detail = stderr.read(160).decode("utf-8", "replace")
                raise RuntimeError(f"{argv[0]} returned {process.returncode}: {detail}")
            return stdout.read().decode("utf-8", "replace")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=0.25)


def _related_processes(tasklist_text: str) -> tuple[dict[str, object], ...]:
    found: list[dict[str, object]] = []
    reader = csv.reader(io.StringIO(tasklist_text))
    for row in reader:
        if len(row) < 2:
            continue
        name = str(row[0]).strip()
        if name.lower() not in _EXTERNAL_PROXY_PROCESS_NAMES:
            continue
        try:
            pid = int(str(row[1]).replace(",", "").strip())
        except ValueError:
            pid = 0
        found.append({"name": name, "pid": pid})
    return tuple(sorted(found, key=lambda item: (str(item["name"]), int(item["pid"]))))


def _matching_lines(text: str, predicates: tuple[str, ...], limit: int = 32) -> tuple[str, ...]:
    values: list[str] = []
    for raw in text.splitlines():
        line = " ".join(raw.strip().split())
        lowered = line.lower()
        if line and any(token in lowered for token in predicates):
            values.append(line[:240])
        if len(values) >= limit:
            break
    return tuple(values)


def _read_system_proxy() -> tuple[bool | None, str, str | None]:
    if os.name != "nt":
        return None, "", None
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            try:
                enabled = bool(int(winreg.QueryValueEx(key, "ProxyEnable")[0]))
            except OSError:
                enabled = False
            try:
                server = str(winreg.QueryValueEx(key, "ProxyServer")[0] or "")
            except OSError:
                server = ""
        digest = hashlib.sha256(server.encode("utf-8")).hexdigest()[:16] if server else ""
        return enabled, digest, None
    except (OSError, ValueError) as exc:
        return None, "", f"system_proxy:{type(exc).__name__}:{exc}"


def capture_runtime_network_context(
    runtime_generation: str,
    *,
    command_runner: Callable[[tuple[str, ...], float], str] | None = None,
    cancel_event: threading.Event | None = None,
) -> RuntimeNetworkContextSnapshot:
    errors: list[str] = []
    tasklist_text = ""
    ipconfig_text = ""
    route_text = ""
    for argv, target in (
        (("tasklist.exe", "/fo", "csv", "/nh"), "tasklist"),
        (("ipconfig.exe", "/all"), "ipconfig"),
        (("route.exe", "print", "-4"), "route"),
    ):
        if cancel_event is not None and cancel_event.is_set():
            errors.append("capture_cancelled")
            break
        try:
            value = (
                command_runner(argv, 1.5) if command_runner is not None
                else _run_readonly_command(argv, 1.5, cancel_event=cancel_event)
            )
        except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as exc:
            errors.append(f"{target}:{type(exc).__name__}:{exc}")
            value = ""
        if target == "tasklist":
            tasklist_text = value
        elif target == "ipconfig":
            ipconfig_text = value
        else:
            route_text = value

    related = _related_processes(tasklist_text)
    adapters = _matching_lines(
        ipconfig_text,
        ("adapter meta tunnel", "adapter mihomo", "adapter tailscale"),
    )
    dns = _matching_lines(ipconfig_text, ("dns servers", "198.18."))
    defaults = _matching_lines(route_text, ("0.0.0.0 0.0.0.0",))
    fake_routes = _matching_lines(route_text, ("198.18.", "198.19."))
    proxy_enabled, proxy_digest, proxy_error = _read_system_proxy()
    if proxy_error:
        errors.append(proxy_error)

    tun_signature = bool(fake_routes) or any(
        "meta tunnel" in line.lower() or "mihomo" in line.lower()
        for line in adapters
    )
    if errors and not tasklist_text:
        state = "indeterminate"
    elif tun_signature:
        state = "external_tun_present" if related else "indeterminate"
    elif related or proxy_enabled:
        state = "external_proxy_present"
    else:
        state = "external_proxy_absent"

    return RuntimeNetworkContextSnapshot(
        captured_at=datetime.now(timezone.utc).isoformat(),
        captured_monotonic_ns=time.monotonic_ns(),
        runtime_generation=str(runtime_generation),
        state=state,
        related_processes=related,
        adapter_summary=adapters,
        default_route_summary=defaults,
        fake_ip_route_summary=fake_routes,
        dns_summary=dns,
        system_proxy_enabled=proxy_enabled,
        system_proxy_digest=proxy_digest,
        errors=tuple(errors),
    )


class _DefaultNetworkContextSampler:
    """One process-wide read-only scan, with no queue of stale generations."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._latest: RuntimeNetworkContextSnapshot | None = None
        self._finished_at = 0.0

    def capture(
        self, generation: str, cancelled: threading.Event,
    ) -> RuntimeNetworkContextSnapshot | None:
        if cancelled.is_set() or not self._lock.acquire(blocking=False):
            return None
        try:
            if cancelled.is_set():
                return None
            if self._latest is None or time.monotonic() - self._finished_at >= 2.0:
                result = capture_runtime_network_context(generation, cancel_event=cancelled)
                if cancelled.is_set():
                    return None
                self._latest = result
                self._finished_at = time.monotonic()
            # Preserve capture age; a new owner must not relabel stale evidence
            # as a new observation or use diagnostics as runtime authority.
            return replace(self._latest, runtime_generation=generation)
        finally:
            self._lock.release()


_DEFAULT_SAMPLER = _DefaultNetworkContextSampler()


class RuntimeNetworkContextObserver:
    """Read-only diagnostic sampler. Its output never controls runtime policy."""

    def __init__(
        self,
        runtime_generation: str,
        *,
        capture: Callable[[str], RuntimeNetworkContextSnapshot] = capture_runtime_network_context,
        interval_seconds: float = 2.0,
    ) -> None:
        self.runtime_generation = str(runtime_generation)
        self.capture = capture
        self.interval_seconds = max(0.5, float(interval_seconds))
        self._lock = threading.Lock()
        self._history: deque[RuntimeNetworkContextSnapshot] = deque(maxlen=8)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None or self._stop.is_set():
            return
        self._thread = threading.Thread(
            target=self._run,
            name="tier3-runtime-network-context",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout_seconds: float = 1.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.0, float(timeout_seconds)))

    def latest(self) -> RuntimeNetworkContextSnapshot | None:
        with self._lock:
            return self._history[-1] if self._history else None

    def history(self) -> tuple[RuntimeNetworkContextSnapshot, ...]:
        with self._lock:
            return tuple(self._history)

    def _sample(self) -> None:
        if self._stop.is_set():
            return
        try:
            snapshot = (
                _DEFAULT_SAMPLER.capture(self.runtime_generation, self._stop)
                if self.capture is capture_runtime_network_context
                else self.capture(self.runtime_generation)
            )
        except Exception:
            return
        with self._lock:
            if snapshot is None or self._stop.is_set():
                return
            if self._history and self._history[-1].captured_monotonic_ns == snapshot.captured_monotonic_ns:
                return
            self._history.append(snapshot)

    def _run(self) -> None:
        while not self._stop.is_set():
            self._sample()
            if self._stop.wait(self.interval_seconds):
                break
