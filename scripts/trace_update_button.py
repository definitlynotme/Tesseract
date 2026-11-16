"""Record a Tesseract update attempt, including the failure text the app omits.

The installed app writes only an exception class into bridge.log. This script
does not change that. Start it, then press 检查更新 or 立即更新. It keeps the
new log lines, update files, installer process, and connections from that
attempt, and it repeats the release check on the same routes with the full
error. The probe also fetches the tiny signature and checksum, and reads
only the first 64 KiB of the installer to see whether that download can
start. It does not save or launch the installer.

Query strings and proxy usernames are removed before anything is written.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener, getproxies

try:
    import winreg
except ImportError:  # Not Windows.
    winreg = None  # type: ignore[assignment]


API_URL = "https://api.github.com/repos/definitlynotme/Tesseract/releases/latest"
_USERINFO_RE = re.compile(r"(?i)(https?://)[^/\s@]+@")
_QUERY_RE = re.compile(r"(https?://[^\s?#]+)\?[^\s#]*")
_NETSTAT_RE = re.compile(r"^\s*TCP\s+(\S+)\s+(\S+)\s+(\S+)\s+(\d+)\s*$", re.IGNORECASE)
_MIXED_PORT_RE = re.compile(r"(?m)^\s*mihomo_mixed_port:\s*(\d+)\s*$")
_INTERESTING_LOG_RE = re.compile(
    r"update|handoff|installer|install|error|fail|exception|warning",
    re.IGNORECASE,
)
_WATCH_PROCESS_RE = re.compile(r"tesseract|mihomo|clash", re.IGNORECASE)


def redact(text: str) -> str:
    """Drop proxy credentials and URL query strings."""

    cleaned = _USERINFO_RE.sub(r"\1", str(text))
    return _QUERY_RE.sub(r"\1?<redacted>", cleaned)


def semver_from_file_version(text: str) -> str | None:
    """Map a Windows file version back to the app's release version.

    Stable builds store 65535 in the fourth component. An RC stores its
    candidate number there.
    """

    parts = [part for part in str(text).strip().split(".") if part != ""]
    if len(parts) < 3 or not all(part.isdigit() for part in parts[:4]):
        return None
    version = ".".join(parts[:3])
    if len(parts) < 4 or parts[3] == "65535":
        return version
    return f"{version}-rc.{int(parts[3])}"


class Trace:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._file = path.open("a", encoding="utf-8", newline="\n")
        self._lock = threading.Lock()
        self.findings: list[str] = []

    def write(self, message: str) -> None:
        stamp = datetime.now().isoformat(timespec="milliseconds")
        line = f"{stamp} {redact(message)}"
        with self._lock:
            self._file.write(line + "\n")
            self._file.flush()
        print(line, flush=True)

    def finding(self, message: str) -> None:
        cleaned = redact(message)
        with self._lock:
            self.findings.append(cleaned)
        self.write("结论 " + cleaned)

    def close(self) -> None:
        with self._lock:
            self._file.close()


class Tail:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.offset = path.stat().st_size if path.exists() else 0
        self._pending = b""

    def read_new(self) -> list[str]:
        if not self.path.exists():
            return []
        size = self.path.stat().st_size
        if size < self.offset:
            self.offset = 0
            self._pending = b""
        if size == self.offset:
            return []
        with self.path.open("rb") as handle:
            handle.seek(self.offset)
            chunk = handle.read()
            self.offset = handle.tell()
        self._pending += chunk
        lines: list[str] = []
        while b"\n" in self._pending:
            raw, self._pending = self._pending.split(b"\n", 1)
            lines.append(raw.decode("utf-8", "replace").rstrip("\r"))
        return lines


def _no_window_kwargs() -> dict[str, int]:
    flag = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return {"creationflags": flag} if flag else {}


def _file_version(path: Path) -> str:
    if sys.platform != "win32" or not path.exists():
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        size = ctypes.windll.version.GetFileVersionInfoSizeW(str(path), None)
        if not size:
            return ""
        buffer = ctypes.create_string_buffer(size)
        if not ctypes.windll.version.GetFileVersionInfoW(str(path), 0, size, buffer):
            return ""

        class VS_FIXEDFILEINFO(ctypes.Structure):
            _fields_ = [
                ("dwSignature", wintypes.DWORD),
                ("dwStrucVersion", wintypes.DWORD),
                ("dwFileVersionMS", wintypes.DWORD),
                ("dwFileVersionLS", wintypes.DWORD),
                ("dwProductVersionMS", wintypes.DWORD),
                ("dwProductVersionLS", wintypes.DWORD),
                ("dwFileFlagsMask", wintypes.DWORD),
                ("dwFileFlags", wintypes.DWORD),
                ("dwFileOS", wintypes.DWORD),
                ("dwFileType", wintypes.DWORD),
                ("dwFileSubtype", wintypes.DWORD),
                ("dwFileDateMS", wintypes.DWORD),
                ("dwFileDateLS", wintypes.DWORD),
            ]

        pointer = ctypes.c_void_p()
        length = wintypes.UINT()
        if not ctypes.windll.version.VerQueryValueW(
            buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)
        ):
            return ""
        info = ctypes.cast(pointer, ctypes.POINTER(VS_FIXEDFILEINFO)).contents
        numbers = (
            info.dwFileVersionMS >> 16,
            info.dwFileVersionMS & 0xFFFF,
            info.dwFileVersionLS >> 16,
            info.dwFileVersionLS & 0xFFFF,
        )
        return ".".join(str(number) for number in numbers)
    except OSError:
        return ""


def _processes() -> list[tuple[int, int, str]]:
    """Return (pid, parent pid, exe name) for every process."""

    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel = ctypes.windll.kernel32
    kernel.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
    kernel.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    snapshot = kernel.CreateToolhelp32Snapshot(0x00000002, 0)
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        return []
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    found: list[tuple[int, int, str]] = []
    try:
        ok = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            found.append((int(entry.th32ProcessID), int(entry.th32ParentProcessID), entry.szExeFile))
            ok = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    return found


def _image_path(pid: int) -> str:
    if sys.platform != "win32":
        return ""
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.windll.kernel32
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
    ]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buffer))
        if kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value
        return ""
    finally:
        kernel.CloseHandle(handle)


def _connections() -> list[tuple[int, str, str, str]]:
    """Return (pid, state, local, remote) for TCP v4 and v6."""

    if sys.platform != "win32":
        return []
    rows: list[tuple[int, str, str, str]] = []
    for protocol in ("tcp", "tcpv6"):
        try:
            completed = subprocess.run(
                ["netstat", "-ano", "-p", protocol],
                capture_output=True,
                check=False,
                **_no_window_kwargs(),
            )
        except OSError:
            continue
        text = completed.stdout.decode("mbcs", "replace")
        for raw in text.splitlines():
            match = _NETSTAT_RE.match(raw)
            if match is None:
                continue
            local, remote, state, pid = match.groups()
            rows.append((int(pid), state.upper(), local, remote))
    return rows


def _proxy_settings() -> list[str]:
    lines: list[str] = []
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        value = os.environ.get(name) or os.environ.get(name.lower())
        if value:
            lines.append(f"env {name}={redact(value)}")
    try:
        proxies = getproxies()
    except Exception as exc:
        lines.append(f"urllib proxy settings unreadable: {type(exc).__name__}: {exc}")
        proxies = {}
    for scheme, value in sorted(proxies.items()):
        if isinstance(value, str) and value.strip():
            lines.append(f"urllib {scheme}={redact(value)}")
    if winreg is None:
        return lines
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
            lines.append(f"wininet ProxyEnable={enabled} ProxyServer={redact(str(server))}")
    except OSError as exc:
        lines.append(f"wininet proxy unreadable: {type(exc).__name__}")
    return lines


def _mixed_port(config_path: Path) -> int | None:
    try:
        text = config_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = _MIXED_PORT_RE.search(text)
    if match is None:
        return None
    port = int(match.group(1))
    return port if 0 < port < 65536 else None


def _load_updater() -> tuple[object | None, str | None]:
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        import app_updater
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"
    return app_updater, None


def _exception_text(exc: BaseException) -> str:
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(parts) < 6:
        seen.add(id(current))
        detail = f"{type(current).__name__}: {current}"
        if isinstance(current, OSError) and current.winerror:  # type: ignore[attr-defined]
            detail += f" winerror={current.winerror}"
        if isinstance(current, HTTPError):
            detail += f" http={current.code}"
            try:
                body = current.read(800)
            except Exception:
                body = b""
            if body:
                detail += " body=" + body.decode("utf-8", "replace")
        parts.append(redact(detail))
        current = current.__cause__ or current.__context__
    return " <- ".join(parts)


def _routes(updater: object | None, core_proxy: str | None) -> list[tuple[str, object]]:
    if updater is not None:
        return list(updater.network_routes(core_proxy))  # type: ignore[attr-defined]
    routes: list[tuple[str, Callable[..., object]]] = []
    core = str(core_proxy or "").strip()
    if core:
        routes.append(("core proxy", build_opener(ProxyHandler({"http": core, "https": core}))))
    try:
        host = {
            key: value
            for key, value in getproxies().items()
            if key in ("http", "https") and isinstance(value, str) and value.strip()
        }
    except Exception:
        host = {}
    if host and not (core and all(value.strip().rstrip("/").lower() == core.rstrip("/").lower() for value in host.values())):
        routes.append(("system proxy", build_opener(ProxyHandler(host))))
    routes.append(("direct", build_opener(ProxyHandler({}))))
    return routes


def _request(updater: object | None, url: str) -> Request:
    if updater is not None:
        return updater._request(url, accept="application/vnd.github+json")  # type: ignore[attr-defined]
    return Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "Tesseract-update-trace",
        },
    )


def _release_summary(payload: bytes, updater: object | None, current_version: str) -> list[str]:
    try:
        release = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return [f"发布接口返回的不是 JSON：{type(exc).__name__}: {exc}"]
    if not isinstance(release, dict):
        return ["发布接口返回的不是对象。"]
    if updater is not None:
        try:
            info = updater.update_info_from_release(release, current_version=current_version)  # type: ignore[attr-defined]
        except Exception as exc:
            return [f"发布内容无法用于更新：{_exception_text(exc)}"]
        assets = [
            name
            for name, asset in (
                ("installer", info.installer),
                ("checksum", info.checksum),
                ("signature", info.checksum_signature),
            )
            if asset is not None
        ]
        missing = [name for name in ("installer", "checksum", "signature") if name not in assets]
        return [
            f"当前版本 {info.current_version}，最新版本 {info.latest_version}，"
            f"有更新={info.update_available}，可安装={info.installable}",
            "发布文件: " + (", ".join(assets) if assets else "无")
            + ("；缺少 " + ", ".join(missing) if missing else ""),
        ]
    tag = str(release.get("tag_name") or "")
    message = str(release.get("message") or "")
    names = [
        str(item.get("name") or "")
        for item in release.get("assets", [])
        if isinstance(item, dict)
    ]
    lines = [f"tag={tag or '-'} draft={release.get('draft')} prerelease={release.get('prerelease')}"]
    if message:
        lines.append("message=" + message[:400])
    lines.append("assets=" + ", ".join(name for name in names if name))
    return lines


def run_probe(
    trace: Trace,
    *,
    updater: object | None,
    core_proxy: str | None,
    current_version: str,
    timeout: float,
    stop: threading.Event,
) -> None:
    trace.write(
        "探针开始。按应用的顺序尝试核心代理、系统代理、直连。"
        "这里保留异常原文；应用自己的 bridge.log 不会保留。"
    )
    if not core_proxy:
        trace.write("探针没有使用核心代理：没有发现正在监听的 mixed 端口。")
    first_success: tuple[str, bytes] | None = None
    for label, opener in _routes(updater, core_proxy):
        if stop.is_set():
            trace.write("探针在下一条路线前被中止。")
            return
        started = time.perf_counter()
        trace.write(f"探针请求 {label}")
        try:
            response = opener.open(_request(updater, API_URL), timeout=timeout)  # type: ignore[attr-defined]
            try:
                status = getattr(response, "status", None) or response.getcode()
                body = response.read()
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
        except Exception as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            trace.write(f"探针失败 {label} {elapsed}ms {_exception_text(exc)}")
            trace.finding(f"路线 {label} 失败：{_exception_text(exc)}")
            closer = getattr(exc, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:
                    pass
            continue
        elapsed = int((time.perf_counter() - started) * 1000)
        trace.write(f"探针成功 {label} {elapsed}ms http={status} bytes={len(body)}")
        trace.finding(f"路线 {label} 成功，HTTP {status}，{len(body)} 字节，用时 {elapsed}ms。")
        if first_success is None:
            first_success = (label, body)
    if first_success is None:
        trace.finding("三条路线都没有拿到发布信息。上面的异常原文就是检查更新失败的原因。")
        return
    label, body = first_success
    trace.write(f"按应用规则采用第一条成功路线: {label}")
    for line in _release_summary(body, updater, current_version):
        trace.write("发布摘要 " + line)
        trace.finding(line)
    routes = _routes(updater, core_proxy)
    for name, url, limit in _small_assets(body, updater, current_version):
        if stop.is_set():
            trace.write("探针在下载校验文件前被中止。")
            return
        _download_named(
            trace,
            routes=routes,
            updater=updater,
            name=name,
            url=url,
            limit=limit,
            timeout=timeout,
            stop=stop,
            partial_ok=False,
        )
    installer = _installer_asset(body, updater, current_version)
    if installer is not None and not stop.is_set():
        name, url = installer
        _download_named(
            trace,
            routes=routes,
            updater=updater,
            name=name,
            url=url,
            limit=65536,
            timeout=timeout,
            stop=stop,
            partial_ok=True,
        )


def _small_assets(payload: bytes, updater: object | None, current_version: str) -> list[tuple[str, str, int]]:
    """Signature and checksum only. The installer is left to the button."""

    try:
        release = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return []
    if not isinstance(release, dict):
        return []
    if updater is not None:
        try:
            info = updater.update_info_from_release(release, current_version=current_version)  # type: ignore[attr-defined]
        except Exception:
            return []
        found: list[tuple[str, str, int]] = []
        if info.checksum_signature is not None:
            found.append((info.checksum_signature.name, info.checksum_signature.download_url, 4096))
        if info.checksum is not None:
            found.append((info.checksum.name, info.checksum.download_url, 1024 * 1024))
        return found
    names = {
        str(item.get("name") or ""): str(item.get("browser_download_url") or "")
        for item in release.get("assets", [])
        if isinstance(item, dict)
    }
    found = []
    for name, limit in (("SHA256SUMS.txt.sig", 4096), ("SHA256SUMS.txt", 1024 * 1024)):
        url = names.get(name) or ""
        if url:
            found.append((name, url, limit))
    return found


def _installer_asset(payload: bytes, updater: object | None, current_version: str) -> tuple[str, str] | None:
    try:
        release = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(release, dict) or updater is None:
        return None
    try:
        info = updater.update_info_from_release(release, current_version=current_version)  # type: ignore[attr-defined]
    except Exception:
        return None
    if info.installer is None:
        return None
    return info.installer.name, info.installer.download_url


def _download_named(
    trace: Trace,
    *,
    routes: list[tuple[str, object]],
    updater: object | None,
    name: str,
    url: str,
    limit: int,
    timeout: float,
    stop: threading.Event,
    partial_ok: bool,
) -> None:
    trace.write(f"探针下载 {name}")
    for label, opener in routes:
        if stop.is_set():
            trace.write(f"探针下载 {name} 被中止。")
            return
        started = time.perf_counter()
        try:
            if updater is not None:
                request = updater._request(url)  # type: ignore[attr-defined]
            else:
                request = Request(url, headers={"Accept": "application/octet-stream", "User-Agent": "Tesseract-update-trace"})
            response = opener.open(request, timeout=timeout)  # type: ignore[attr-defined]
            try:
                status = getattr(response, "status", None) or response.getcode()
                payload = response.read(limit + 1)
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
        except Exception as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            trace.write(f"探针下载失败 {name} {label} {elapsed}ms {_exception_text(exc)}")
            trace.finding(f"下载 {name} 走 {label} 失败：{_exception_text(exc)}")
            closer = getattr(exc, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:
                    pass
            continue
        elapsed = int((time.perf_counter() - started) * 1000)
        if len(payload) > limit and not partial_ok:
            trace.write(f"探针下载 {name} {label} 超过 {limit} 字节，已丢弃。")
            trace.finding(f"下载 {name} 走 {label} 超过允许大小。")
            return
        if partial_ok:
            trace.write(
                f"探针已确认 {name} 可以开始下载，{label} {elapsed}ms http={status}，"
                f"只读取了前 {min(len(payload), limit)} 字节，没有保存安装包。"
            )
            trace.finding(
                f"安装包 {name} 走 {label} 可以开始下载，HTTP {status}，"
                f"已读取 {min(len(payload), limit)} 字节后停止。"
            )
            return
        trace.write(f"探针下载成功 {name} {label} {elapsed}ms http={status} bytes={len(payload)}")
        trace.finding(f"下载 {name} 走 {label} 成功，HTTP {status}，{len(payload)} 字节。")
        return
    trace.finding(f"下载 {name} 时每一条路线都失败。")


def _select_processes(
    processes: Iterable[tuple[int, int, str]],
    *,
    tesseract_only: bool,
) -> dict[int, tuple[int, str]]:
    selected: dict[int, tuple[int, str]] = {}
    for pid, parent, name in processes:
        if tesseract_only:
            if "tesseract" not in name.lower():
                continue
        elif _WATCH_PROCESS_RE.search(name) is None:
            continue
        selected[pid] = (parent, name)
    return selected


def _directory_listing(path: Path) -> dict[str, tuple[int, int]]:
    if not path.exists():
        return {}
    listing: dict[str, tuple[int, int]] = {}
    for child in path.iterdir():
        try:
            stat = child.stat()
        except OSError:
            continue
        listing[child.name] = (stat.st_size, stat.st_mtime_ns)
    return listing


def _core_proxy(port: int | None, connections: list[tuple[int, str, str, str]], watched: dict[int, tuple[int, str]]) -> str | None:
    if port is None:
        return None
    token = f":{port}"
    for pid, state, local, _remote in connections:
        if state not in {"LISTENING", "LISTEN"} or token not in local:
            continue
        if pid in watched:
            return f"http://127.0.0.1:{port}"
    return None


def record_environment(trace: Trace, data_dir: Path, config_path: Path) -> tuple[str | None, str, dict[int, tuple[int, str]]]:
    processes = _processes()
    watched = _select_processes(processes, tesseract_only=False)
    tesseract = _select_processes(processes, tesseract_only=True)
    connections = _connections()
    port = _mixed_port(config_path)
    core = _core_proxy(port, connections, tesseract)
    installed = Path(r"C:\Program Files\Tesseract\Tesseract.exe")
    file_version = _file_version(installed)
    interpreted = semver_from_file_version(file_version) or ""
    trace.write(f"数据目录 {data_dir}")
    trace.write(f"配置里的 mixed 端口 {port if port is not None else '没有读到'}")
    trace.write(f"安装文件版本 {file_version or '没有读到'}，对应发布版本 {interpreted or '没有读到'}")
    if not tesseract:
        trace.write("当前没有 Tesseract 进程。可以先打开应用再按更新。")
    for pid, (parent, name) in sorted(watched.items()):
        image = _image_path(pid)
        trace.write(f"进程 pid={pid} parent={parent} name={name} path={image or '-'}")
    listening = [
        f"pid={pid} {local}"
        for pid, state, local, _remote in connections
        if pid in watched and state in {"LISTENING", "LISTEN"}
    ]
    trace.write("监听 " + (", ".join(listening) if listening else "无"))
    trace.write("核心代理 " + (core or "无"))
    proxies = _proxy_settings()
    if proxies:
        for line in proxies:
            trace.write("代理 " + line)
    else:
        trace.write("代理 没有读到环境变量、urllib 或 WinINET 代理。")
    state_path = data_dir / "app-update-state.json"
    try:
        trace.write("更新状态文件 " + state_path.read_text(encoding="utf-8"))
    except OSError as exc:
        trace.write(f"更新状态文件不可读：{type(exc).__name__}")
    listing = _directory_listing(data_dir / "updates")
    if listing:
        for name, (size, _mtime) in sorted(listing.items()):
            trace.write(f"已有更新文件 {name} size={size}")
    else:
        trace.write("updates 目录是空的，或不存在。")
    return core, interpreted or "0.0.0", watched


def _button_line(line: str) -> bool:
    return re.search(r"update|handoff|installer", line, re.IGNORECASE) is not None and "update.sources" not in line


def _log_new_lines(trace: Trace, tail: Tail, label: str) -> bool:
    activity = False
    for line in tail.read_new():
        if "update.sources" in line or not _INTERESTING_LOG_RE.search(line):
            continue
        lowered = line.lower()
        relevant = _button_line(line) or (
            label == "bridge.log" and (" WARNING " in line or " ERROR " in line)
        ) or (
            label == "shell.log" and any(word in lowered for word in ("quit", "bye", "fail"))
        )
        if not relevant:
            continue
        shortened = line if len(line) <= 2000 else line[:2000] + "...<truncated>"
        trace.write(f"{label} {shortened}")
        if _button_line(line):
            activity = True
            trace.finding(f"{label} {shortened}")
    return activity


def _log_listing_changes(
    trace: Trace,
    path: Path,
    previous: dict[str, tuple[int, int]],
) -> tuple[dict[str, tuple[int, int]], bool]:
    current = _directory_listing(path)
    activity = False
    for name in sorted(set(previous) | set(current)):
        if previous.get(name) == current.get(name):
            continue
        activity = True
        if name not in current:
            trace.write(f"更新文件消失 {name}")
            trace.finding(f"更新文件消失 {name}")
        else:
            size = current[name][0]
            trace.write(f"更新文件变化 {name} size={size}")
            trace.finding(f"更新文件变化 {name} size={size}")
    return current, activity


def watch(
    trace: Trace,
    *,
    data_dir: Path,
    wait_seconds: float,
    idle_seconds: float,
    stop: threading.Event,
    once: bool,
) -> None:
    bridge = Tail(data_dir / "bridge.log")
    shell = Tail(data_dir / "logs" / "shell.log")
    events = Tail(data_dir / "events.jsonl")
    updates = data_dir / "updates"
    listing = _directory_listing(updates)
    state_path = data_dir / "app-update-state.json"
    try:
        state_text = state_path.read_text(encoding="utf-8")
    except OSError:
        state_text = ""
    seen_processes = set(_select_processes(_processes(), tesseract_only=True))
    seen_connections = {
        (pid, state, local, remote)
        for pid, state, local, remote in _connections()
        if pid in seen_processes
    }
    connection_logs = 0
    connections_capped = False
    started = time.monotonic()
    last_activity: float | None = None
    next_sample = started
    trace.write("开始等候更新按钮。")
    while not stop.is_set():
        activity = False
        activity = _log_new_lines(trace, bridge, "bridge.log") or activity
        activity = _log_new_lines(trace, shell, "shell.log") or activity
        activity = _log_new_lines(trace, events, "events.jsonl") or activity
        listing, listing_changed = _log_listing_changes(trace, updates, listing)
        activity = listing_changed or activity
        try:
            current_state = state_path.read_text(encoding="utf-8")
        except OSError:
            current_state = ""
        if current_state != state_text:
            activity = True
            state_text = current_state
            trace.write("更新状态文件变为 " + (current_state or "<empty>"))
            trace.finding("app-update-state.json 有变化。")
        now = time.monotonic()
        if now >= next_sample:
            next_sample = now + 1
            current_processes = _select_processes(_processes(), tesseract_only=True)
            for pid, (parent, name) in sorted(current_processes.items()):
                if pid in seen_processes:
                    continue
                seen_processes.add(pid)
                activity = True
                trace.write(f"新进程 pid={pid} parent={parent} name={name} path={_image_path(pid) or '-'}")
                trace.finding(f"出现新进程 {name} pid={pid}")
            current_connections = {
                (pid, state, local, remote)
                for pid, state, local, remote in _connections()
                if pid in current_processes
            }
            for row in sorted(current_connections - seen_connections):
                pid, state, local, remote = row
                if state in {"LISTENING", "LISTEN"}:
                    continue
                if connections_capped:
                    break
                connection_logs += 1
                trace.write(f"连接 pid={pid} {state} {local} -> {remote}")
                if connection_logs >= 80:
                    connections_capped = True
                    trace.write("后续连接不再逐条记录。")
                    break
            seen_connections = current_connections
        if activity:
            last_activity = now
        if once:
            trace.write("单次快照结束，不等候按钮。")
            return
        if last_activity is None and now - started >= wait_seconds:
            trace.finding(
                f"{int(wait_seconds)} 秒内没有看到更新日志、更新文件或新的安装进程。"
                "如果刚按过按钮，说明这次失败没有写进 bridge.log。"
            )
            return
        if last_activity is not None and now - last_activity >= idle_seconds:
            trace.write("更新活动已经停下来，结束记录。")
            return
        if now - started >= 15 * 60:
            trace.write("已记录 15 分钟，结束。")
            return
        stop.wait(0.4)


def _self_test() -> int:
    checks = {
        "http://user:secret@127.0.0.1:7890/path": "http://127.0.0.1:7890/path",
        "https://example.com/file?token=abc&sig=def": "https://example.com/file?<redacted>",
        "plain failure": "plain failure",
    }
    for raw, expected in checks.items():
        if redact(raw) != expected:
            print(f"redact failed: {redact(raw)!r} != {expected!r}")
            return 1
    if semver_from_file_version("0.1.0.65535") != "0.1.0":
        print("stable file version was not mapped")
        return 1
    if semver_from_file_version("0.1.1.2") != "0.1.1-rc.2":
        print("rc file version was not mapped")
        return 1
    print("self-test ok")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="按下 Tesseract 更新按钮时，记录完整更新过程。")
    parser.add_argument("--data-dir", type=Path, default=None, help="默认是 %%LOCALAPPDATA%%\\Tesseract")
    parser.add_argument("--wait", type=float, default=180, help="等候按钮的秒数，默认 180")
    parser.add_argument("--idle", type=float, default=20, help="活动停止后继续观察的秒数，默认 20")
    parser.add_argument("--timeout", type=float, default=20, help="每一条更新路线的超时秒数，默认 20")
    parser.add_argument("--once", action="store_true", help="只记录当前快照和探针，不等候按钮")
    parser.add_argument("--no-probe", action="store_true", help="不主动请求发布接口")
    parser.add_argument("--self-test", action="store_true", help="只检查脱敏和版本解析")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.self_test:
        return _self_test()
    if sys.platform != "win32":
        print("这个脚本只记录 Windows 上的 Tesseract 更新。")
        return 2
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    data_dir = args.data_dir or (local / "Tesseract")
    roaming = Path(os.environ.get("APPDATA", ""))
    config_path = roaming / "Tesseract" / "config.yaml"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = data_dir / "logs" / f"update-trace-{stamp}.log"
    trace = Trace(log_path)
    stop = threading.Event()
    print("日志写在: " + str(log_path), flush=True)
    if args.once:
        print("只做一次快照和探针，不等候按钮。", flush=True)
    else:
        print("现在可以去 Tesseract 按「检查更新」或「立即更新」。失败出现后回到这里按 Ctrl+C。", flush=True)
    probe: threading.Thread | None = None
    try:
        trace.write("记录开始。安装版日志只保留异常类型；这份文件保留探针拿到的异常原文。")
        core, current_version, _watched = record_environment(trace, data_dir, config_path)
        updater, import_error = _load_updater()
        if import_error:
            trace.write(f"未能载入应用里的更新代码，探针改用同等的三条路线：{import_error}")
        else:
            source_version = getattr(updater, "APP_VERSION", "")
            trace.write(f"已载入仓库里的更新代码，仓库版本 {source_version}")
        if not args.no_probe:
            probe = threading.Thread(
                target=run_probe,
                kwargs={
                    "trace": trace,
                    "updater": updater,
                    "core_proxy": core,
                    "current_version": current_version,
                    "timeout": args.timeout,
                    "stop": stop,
                },
                name="update-probe",
                daemon=True,
            )
            probe.start()
        watch(
            trace,
            data_dir=data_dir,
            wait_seconds=args.wait,
            idle_seconds=args.idle,
            stop=stop,
            once=args.once,
        )
    except KeyboardInterrupt:
        stop.set()
        trace.write("收到 Ctrl+C，正在结束。")
    finally:
        if probe is not None:
            probe.join(timeout=args.timeout * 9 + 15)
        trace.write("---- 汇总 ----")
        findings = list(trace.findings)
        if findings:
            for item in findings:
                trace.write("汇总 " + item)
        else:
            trace.write("汇总 没有形成结论。")
        trace.write("记录结束 " + str(trace.path))
        trace.close()
    print("记录已写入: " + str(log_path), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
