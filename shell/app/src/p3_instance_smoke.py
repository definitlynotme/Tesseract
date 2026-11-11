"""Real isolated Win32 interoperability smoke, with per-shell descendant inventories."""
import ctypes as C
from ctypes import wintypes as W
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
import bridge_instance_scope as scope_api


def validate():
    for key in ("TESSERACT_INSTANCE_SCOPE", "TESSERACT_CONFIG_DIR", "TESSERACT_DATA_DIR"):
        if not os.environ.get(key, "").strip():
            raise RuntimeError("custom_scope_required: " + key)
    if os.environ.get("TESSERACT_ISOLATED_AUDIT_MANIFEST"):
        raise RuntimeError("custom_scope_required")
    root = Path(os.environ.get("TESSERACT_P3_SMOKE_TEMP_ROOT", tempfile.gettempdir())).resolve()
    for key in ("TESSERACT_CONFIG_DIR", "TESSERACT_DATA_DIR"):
        p = Path(os.environ[key]).resolve(strict=True)
        if not p.is_dir() or not p.is_relative_to(root):
            raise RuntimeError("temporary_directory_required")
    scope = scope_api.resolve_scope()
    assert scope.kind == "custom"
    return scope


# T-P3-H2 (C3-8): a read that met a sharing violation (PermissionError 13; the harness polls
# shell.log and the watcher's JSON files while other processes write or scan them) is retried
# briefly instead of failing the case. Every such event is kept for the case's result.json with
# the file and the processes holding it (Restart Manager), so the next one is named, not guessed.
TRANSIENT_READS = []
READ_TRIES = 40
READ_DELAY = .05


class RM_UNIQUE_PROCESS(C.Structure):
    _fields_ = [("pid", W.DWORD), ("start", W.FILETIME)]


class RM_PROCESS_INFO(C.Structure):
    _fields_ = [("process", RM_UNIQUE_PROCESS), ("app_name", W.WCHAR * 256), ("service", W.WCHAR * 64),
                ("app_type", C.c_int), ("status", W.ULONG), ("session", W.DWORD), ("restartable", W.BOOL)]


def holders(path):
    """Processes that hold `path` open, via the Restart Manager (no handle enumeration, no
    privileges). [] when none is reported; {"error": code} when the query itself failed."""
    try:
        rm = C.WinDLL("rstrtmgr")
        session = W.DWORD(); key = C.create_unicode_buffer(33)
        error = rm.RmStartSession(C.byref(session), 0, key)
        if error: return {"error": error}
        try:
            names = (W.LPCWSTR * 1)(str(path))
            error = rm.RmRegisterResources(session, 1, names, 0, None, 0, None)
            if error: return {"error": error}
            needed, count, reasons = W.UINT(0), W.UINT(16), W.DWORD(0)
            info = (RM_PROCESS_INFO * 16)()
            error = rm.RmGetList(session, C.byref(needed), C.byref(count), info, C.byref(reasons))
            if error and error != 234: return {"error": error}  # 234: more than 16 holders
            return [{"pid": info[i].process.pid, "app": info[i].app_name, "type": info[i].app_type}
                    for i in range(min(count.value, 16))]
        finally:
            rm.RmEndSession(session)
    except Exception as exc:
        return {"error": repr(exc)}


def read_shared(path, text=False):
    path = Path(path)
    return shared(path, "read", lambda: path.read_text("utf-8") if text else path.read_bytes())


def write_shared(path, data=b""):
    """T-P3-H2 fix 1: the harness's own writes (case markers, evidence) get the same retry and
    holder evidence as its reads (`op: "write"` in transient_reads)."""
    path = Path(path)
    return shared(path, "write", lambda: path.write_bytes(data))


def shared(path, op, action):
    for attempt in range(1, READ_TRIES + 1):
        try:
            return action()
        except PermissionError as exc:
            # open() reports a sharing violation as errno 13 (winerror is unset on this path).
            entry = {"path": str(path), "op": op, "attempt": attempt, "errno": exc.errno, "winerror": getattr(exc, "winerror", None),
                     "at": round(time.time(), 3), "caller": traceback.extract_stack(limit=3)[0].name}
            if attempt in (1, 2, 10, READ_TRIES): entry["holders"] = holders(path)
            TRANSIENT_READS.append(entry)
            if attempt == READ_TRIES: raise
            time.sleep(READ_DELAY)


def read_selftest(directory):
    """T-P3-H2: a file held without read sharing for ~0.3 s is read after retries, and the
    evidence names the holder (this process)."""
    directory = Path(directory).resolve(); directory.mkdir(parents=True, exist_ok=True)
    target = directory / ("held-" + uuid.uuid4().hex + ".log")
    target.write_bytes(b"line\n")
    k = C.WinDLL("kernel32", use_last_error=True)
    k.CreateFileW.argtypes = (W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p, W.DWORD, W.DWORD, W.HANDLE)
    k.CreateFileW.restype = W.HANDLE
    handle = k.CreateFileW(str(target), 0x80000000, 0, None, 3, 0x80, None)  # GENERIC_READ, no sharing
    assert handle and handle != W.HANDLE(-1).value, C.get_last_error()
    release = threading.Timer(.3, lambda: k.CloseHandle(handle)); release.start()
    del TRANSIENT_READS[:]
    started = time.monotonic()
    assert read_shared(target) == b"line\n"
    release.join()
    events = list(TRANSIENT_READS)
    assert events and all(e["path"] == str(target) for e in events), events
    first = events[0]
    assert first["errno"] == 13 and first["caller"] == "read_selftest", first
    assert any(h["pid"] == os.getpid() for h in first["holders"]), first
    # Fix 1: a write meets the same retry and evidence.
    handle = k.CreateFileW(str(target), 0x80000000, 0, None, 3, 0x80, None)
    assert handle and handle != W.HANDLE(-1).value, C.get_last_error()
    release = threading.Timer(.3, lambda: k.CloseHandle(handle)); release.start()
    del TRANSIENT_READS[:]
    write_shared(target, b"again\n")
    release.join()
    writes = list(TRANSIENT_READS)
    assert writes and writes[0]["op"] == "write" and writes[0]["caller"] == "read_selftest" and target.read_bytes() == b"again\n", writes
    outcome = {"passed": True, "retries": len(events), "waited_s": round(time.monotonic() - started, 3), "first": first,
               "write_retries": len(writes), "first_write": writes[0]}
    (directory / "read-selftest.json").write_bytes(json.dumps(outcome, ensure_ascii=False, indent=2).encode())
    target.unlink()
    print("PASS: a held file is read after", len(events), "retries; holder named:", first["holders"])


def wait_for(predicate, seconds=15):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(.05)
    raise TimeoutError("smoke condition timed out")


def kernel():
    k = C.WinDLL("kernel32", use_last_error=True)
    k.CreateEventW.argtypes = (C.c_void_p, W.BOOL, W.BOOL, W.LPCWSTR)
    k.CreateEventW.restype = W.HANDLE
    k.OpenEventW.argtypes = (W.DWORD, W.BOOL, W.LPCWSTR)
    k.OpenEventW.restype = W.HANDLE
    k.OpenMutexW.argtypes = (W.DWORD, W.BOOL, W.LPCWSTR)
    k.OpenMutexW.restype = W.HANDLE
    return k


def helper(mode, directory):
    scope = validate()
    api = scope_api.Win32MutexAPI()
    if mode == "inherited":
        result = scope_api.acquire_instance(scope, legacy_gate="inherited", win32=api)
        assert result.status == "acquired"
        try:
            (directory / "sidecar-ready").write_bytes(b"inherited acquired")
            wait_for(lambda: (directory / "sidecar-release").exists())
        finally:
            result.lease.close()
        return
    legacy, error = api.create_mutex(scope.names.legacy_mutex)
    if mode == "qt-probe":
        try:
            assert legacy and error == scope_api.ERROR_ALREADY_EXISTS
            (directory / "qt-blocked").write_bytes(b"ERROR_ALREADY_EXISTS: no Qt runtime started")
        finally:
            if legacy: assert api.close(legacy)[0]
        return
    assert legacy and error == 0
    runtime = event = shell = None
    try:
        if mode == "parent":
            shell, error = api.create_mutex(scope.names.shell_mutex)
            assert shell and error == 0 and api.wait(shell, 0)[0] == 0
        if mode == "qt":
            event = kernel().CreateEventW(None, False, False, scope.names.restore_event)
            assert event
        elif mode == "runtime":
            runtime, error = api.create_mutex(scope.names.runtime_mutex)
            assert runtime and error == 0 and api.wait(runtime, 0)[0] == 0
        (directory / "helper-ready").write_bytes(b"ready")
        if mode == "qt":
            assert api.wait(event, 15000)[0] == 0
            (directory / "restored").write_bytes(b"received")
        elif mode == "runtime":
            wait_for(lambda: (directory / "release-start").exists())
            time.sleep(2)
        else:
            assert mode == "parent"
            wait_for(lambda: (directory / "parent-release").exists())
    finally:
        if runtime:
            assert api.release(runtime)[0]
            assert api.close(runtime)[0]
        if event:
            assert api.close(event)[0]
        assert api.close(legacy)[0]
        if shell:
            assert api.release(shell)[0]
            assert api.close(shell)[0]


def windows_for(pid):
    user = C.WinDLL("user32", use_last_error=True)
    callback_type = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
    user.EnumWindows.argtypes = (callback_type, W.LPARAM)
    user.GetWindowThreadProcessId.argtypes = (W.HWND, C.POINTER(W.DWORD))
    found = []
    def visit(hwnd, _):
        owner = W.DWORD()
        user.GetWindowThreadProcessId(hwnd, C.byref(owner))
        if owner.value == pid:
            found.append(hwnd)
        return True
    user.EnumWindows(callback_type(visit), 0)
    return found


def close_dialogs(pid):
    """WM_CLOSE every visible #32770 of `pid` (T-P3-H fix 1, S3). A shell stuck in the native
    sidecar-failure box (e.g. hello_timeout) has no tray and no main window; without this a failed
    case left that box on the user's desktop. Same step as p3_host_smoke.ps1 Cleanup-Shell."""
    user = C.WinDLL("user32", use_last_error=True)
    user.PostMessageW.argtypes = (W.HWND, W.UINT, W.WPARAM, W.LPARAM)
    user.GetClassNameW.argtypes = (W.HWND, W.LPWSTR, C.c_int)
    user.IsWindowVisible.argtypes = (W.HWND,)
    closed = 0
    for hwnd in windows_for(pid):
        name = C.create_unicode_buffer(256)
        user.GetClassNameW(hwnd, name, 256)
        if name.value == "#32770" and user.IsWindowVisible(hwnd):
            closed += bool(user.PostMessageW(hwnd, 0x10, 0, 0))
    return closed


def close_window(pid):
    """Request the shell's real exit. Since P3-03 a window close hides to the tray, so the
    exit request is the tray menu's Quit command; WM_CLOSE is used only before the tray exists."""
    user = C.WinDLL("user32", use_last_error=True)
    user.PostMessageW.argtypes = (W.HWND, W.UINT, W.WPARAM, W.LPARAM)
    user.GetWindowTextW.argtypes = (W.HWND, W.LPWSTR, C.c_int)
    user.GetClassNameW.argtypes = (W.HWND, W.LPWSTR, C.c_int)
    end = time.monotonic() + 5  # the tray window is created right after shell_window_ready
    while time.monotonic() < end:
        for hwnd in windows_for(pid):
            name = C.create_unicode_buffer(256)
            user.GetClassNameW(hwnd, name, 256)
            if name.value == "TesseractShellTray":
                assert user.PostMessageW(hwnd, 0x111, 2, 0)  # WM_COMMAND, tray CMD_QUIT
                return
        time.sleep(.05)
    # No tray: a native failure box is the only thing that can end such a shell.
    close_dialogs(pid)
    for hwnd in windows_for(pid):
        title = C.create_unicode_buffer(256)
        user.GetWindowTextW(hwnd, title, 256)
        # Hidden Tao/WebView control windows are not application close targets.
        # Closing them destroys the event proxy and can force AppHandle::exit's fallback.
        if title.value == "Tesseract Shell (P3)":
            assert user.PostMessageW(hwnd, 0x10, 0, 0)


def main_window(pid):
    user = C.WinDLL("user32", use_last_error=True)
    user.GetWindowTextW.argtypes = (W.HWND, W.LPWSTR, C.c_int)
    for hwnd in windows_for(pid):
        title = C.create_unicode_buffer(256)
        user.GetWindowTextW(hwnd, title, 256)
        if title.value == "Tesseract Shell (P3)": return hwnd
    raise RuntimeError("main_window_missing")


def inspect_names(scope):
    k = kernel()
    api = scope_api.Win32MutexAPI()
    results = {}
    for key, name in vars(scope.names).items():
        try:
            fn = k.OpenEventW if key.endswith("event") else k.OpenMutexW
            handle = fn(0x100000, False, name)
            error = C.get_last_error()
            if handle:
                assert api.close(handle)[0]
            results[key] = {"name": name, "absent": not handle and error == 2, "win32_error": error}
        except Exception as exc:
            results[key] = {"name": name, "absent": False, "error": repr(exc)}
    return results


def foreground_pid():
    user = C.WinDLL("user32", use_last_error=True)
    user.GetForegroundWindow.restype = W.HWND
    user.GetWindowThreadProcessId.argtypes = (W.HWND, C.POINTER(W.DWORD))
    owner = W.DWORD()
    user.GetWindowThreadProcessId(user.GetForegroundWindow(), C.byref(owner))
    return owner.value


# T-P3-H2 (decision wave 6, T-P3-H2 item 1): a locked workstation (LogonUI running in this session,
# or the lock screen in the foreground) covers the driver window, so the real click that grants the
# foreground permission cannot happen. Such a run is "skipped: environment_locked" with evidence,
# not a failure; Claude re-runs it on an unlocked, idle desktop.
LOCK_IMAGES = ("logonui.exe", "lockapp.exe")


class EnvironmentLocked(Exception):
    def __init__(self, evidence):
        super().__init__("environment_locked", evidence)
        self.evidence = evidence


class WTS_PROCESS_INFOW(C.Structure):
    _fields_ = [("session", W.DWORD), ("pid", W.DWORD), ("name", W.LPWSTR), ("sid", C.c_void_p)]


def session_of(pid):
    k = C.WinDLL("kernel32", use_last_error=True)
    k.ProcessIdToSessionId.argtypes = (W.DWORD, C.POINTER(W.DWORD))
    session = W.DWORD()
    return session.value if k.ProcessIdToSessionId(pid, C.byref(session)) else None


def session_processes(image):
    """PIDs of `image` running in this process's session; None when the list is unavailable.
    WTSEnumerateProcesses names each process's session without opening it (LogonUI runs as
    SYSTEM: ProcessIdToSessionId on it is denied)."""
    wts = C.WinDLL("wtsapi32", use_last_error=True)
    wts.WTSEnumerateProcessesW.argtypes = (W.HANDLE, W.DWORD, W.DWORD, C.POINTER(C.POINTER(WTS_PROCESS_INFOW)), C.POINTER(W.DWORD))
    wts.WTSFreeMemory.argtypes = (C.c_void_p,)
    table, count = C.POINTER(WTS_PROCESS_INFOW)(), W.DWORD()
    if not wts.WTSEnumerateProcessesW(None, 0, 1, C.byref(table), C.byref(count)): return None  # current server
    mine = session_of(os.getpid())
    try:
        return [table[i].pid for i in range(count.value)
                if (table[i].name or "").lower() == image.lower() and table[i].session == mine]
    finally:
        wts.WTSFreeMemory(table)


def image_of(pid):
    k = C.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.argtypes = (W.DWORD, W.BOOL, W.DWORD); k.OpenProcess.restype = W.HANDLE
    k.QueryFullProcessImageNameW.argtypes = (W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD))
    k.CloseHandle.argtypes = (W.HANDLE,)
    handle = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle: return None
    try:
        size = W.DWORD(1024); name = C.create_unicode_buffer(1024)
        return Path(name.value).name if k.QueryFullProcessImageNameW(handle, 0, name, C.byref(size)) else None
    finally:
        k.CloseHandle(handle)


def workstation_lock():
    """Evidence whether this session's workstation is locked (decision wave 6: LogonUI running, or
    the lock screen as the foreground window)."""
    user = C.WinDLL("user32", use_last_error=True)
    user.GetForegroundWindow.restype = W.HWND
    user.GetWindowThreadProcessId.argtypes = (W.HWND, C.POINTER(W.DWORD))
    user.GetWindowTextW.argtypes = (W.HWND, W.LPWSTR, C.c_int); user.GetClassNameW.argtypes = (W.HWND, W.LPWSTR, C.c_int)
    hwnd = user.GetForegroundWindow(); owner = W.DWORD()
    title, name = C.create_unicode_buffer(256), C.create_unicode_buffer(256)
    if hwnd:
        user.GetWindowThreadProcessId(hwnd, C.byref(owner))
        user.GetWindowTextW(hwnd, title, 256); user.GetClassNameW(hwnd, name, 256)
    image = image_of(owner.value) if owner.value else None
    logonui = session_processes("LogonUI.exe")
    lock_screen = bool(hwnd) and (image or "").lower() in LOCK_IMAGES
    return {"locked": bool(logonui) or lock_screen, "logonui_pids": logonui, "foreground_is_lock_screen": lock_screen,
            "foreground": {"pid": owner.value or None, "image": image, "class": name.value, "title": title.value},
            "session": session_of(os.getpid()), "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def lock_caused(result):
    """A case that did not pass only because the lock screen covered the driver window (the
    foreground grant), and whose cleanup was clean. Any other failure stays a failure, locked or
    not (fix3r2: hello_timeout failures while locked must not hide behind the skip)."""
    return (not result["passed"] and result.get("skipped") == "environment_locked" and not result.get("cleanup_errors")
            and not result.get("live_watchers_helpers") and result.get("names_absent", True))


def smoke_status(results):
    """passed: every case passed. skipped: every case that did not pass was lock_caused.
    failed: otherwise."""
    if not results: return "failed"
    if all(r["passed"] for r in results): return "passed"
    if all(r["passed"] or lock_caused(r) for r in results): return "skipped"
    return "failed"


def lock_selftest(directory):
    directory = Path(directory).resolve(); directory.mkdir(parents=True, exist_ok=True)
    ok, bad = {"passed": True}, {"passed": False}
    locked = dict(bad, skipped="environment_locked", lock={"locked": True}, cleanup_errors=[], live_watchers_helpers=[], names_absent=True)
    assert smoke_status([ok, ok]) == "passed" and smoke_status([]) == "failed"
    assert smoke_status([ok, bad]) == "failed"
    assert smoke_status([locked, ok, ok]) == "skipped"
    # Locked, but failed for another reason (e.g. hello_timeout): still a failure.
    assert smoke_status([locked, dict(bad, lock={"locked": True})]) == "failed"
    # The lock-caused case whose cleanup left something behind: a failure.
    assert smoke_status([dict(locked, cleanup_errors=[{"object": "A"}]), ok]) == "failed"
    assert smoke_status([dict(locked, names_absent=False)]) == "failed"
    now = workstation_lock()
    assert isinstance(now["locked"], bool) and now["session"] is not None, now
    (directory / "lock-selftest.json").write_bytes(json.dumps({"passed": True, "now": now}, ensure_ascii=False, indent=2).encode())
    print("PASS: smoke_status rules; workstation now:", json.dumps(now, ensure_ascii=False))


class ForegroundHarness:
    """Receive test input in our own window, then grant the second launch ASFW_ANY."""
    def __init__(self):
        import tkinter
        self.window = tkinter.Tk()
        self.window.title("P3-02 foreground test driver")
        self.window.geometry("280x80+40+40")
        self.window.attributes("-topmost", True)
        self.window.update()

    def grant(self):
        user = C.WinDLL("user32", use_last_error=True)
        user.WindowFromPoint.argtypes = (W.POINT,)
        user.WindowFromPoint.restype = W.HWND
        user.GetWindowThreadProcessId.argtypes = (W.HWND, C.POINTER(W.DWORD))
        user.AllowSetForegroundWindow.argtypes = (W.DWORD,)
        point = W.POINT(self.window.winfo_rootx() + 40, self.window.winfo_rooty() + 40)
        owner = W.DWORD()
        covering = user.WindowFromPoint(point)
        user.GetWindowThreadProcessId(covering, C.byref(owner))
        if owner.value != os.getpid():
            # T-P3-H2 fix 1: name what covers the driver (fix1 rounds: the Windows lock screen, i.e.
            # a locked workstation, where no real click can reach the desktop).
            user.GetWindowTextW.argtypes = (W.HWND, W.LPWSTR, C.c_int); user.GetClassNameW.argtypes = (W.HWND, W.LPWSTR, C.c_int)
            title, name = C.create_unicode_buffer(256), C.create_unicode_buffer(256)
            user.GetWindowTextW(covering, title, 256); user.GetClassNameW(covering, name, 256)
            covered = {"covering_pid": owner.value, "covering_image": image_of(owner.value), "class": name.value, "title": title.value}
            lock = workstation_lock()
            if lock["locked"]: raise EnvironmentLocked(dict(lock, driver_covered_by=covered))
            raise AssertionError("test click must target driver window", covered)
        previous = W.POINT(); assert user.GetCursorPos(C.byref(previous))
        try:
            assert user.SetCursorPos(point.x, point.y)
            user.mouse_event(2, 0, 0, 0, 0); user.mouse_event(4, 0, 0, 0, 0)
            wait_for(lambda: foreground_pid() == os.getpid(), 3)
            assert user.AllowSetForegroundWindow(0xffffffff), ("foreground_permission_unavailable", C.get_last_error())
        finally:
            user.SetCursorPos(previous.x, previous.y)
        self.window.withdraw(); self.window.update()

    def close(self):
        self.window.destroy()


class Shell:
    def __init__(self, case, tag, env):
        self.case, self.tag = case, tag
        self.ready = case / (tag + "-root.json")
        self.inventory = case / (tag + "-inventory.json")
        self.log = (case / (tag + "-watch.log")).open("wb")
        self.process = subprocess.Popen([sys.executable, "-B", str(REPO / "shell/app/process_watch.py"),
            "--output", str(self.inventory), "--ready", str(self.ready), "--",
            str(REPO / "shell/target/debug/tesseract-shell.exe")], env=env, cwd=REPO,
            stdout=self.log, stderr=self.log, creationflags=0x08000000)
        self.pid = None

    def started(self):
        wait_for(self.ready.exists)
        self.pid = json.loads(read_shared(self.ready))["pid"]
        return self

    # process_watch gives this scope's msedgewebview2 20 s after the shell exits (decision P3-06
    # item 6, T-P3-H) and the shell itself may wait 12 s for BrowserProcessExited: 45 s in total.
    # T-P3-H2: a failed audit watches its residue 20 s more before it writes the class, so the
    # watcher may take up to ~65 s on the failure path.
    def finish(self, seconds=75):
        assert self.process.wait(timeout=seconds) == 0
        self.log.close()
        inventory = json.loads(read_shared(self.inventory))
        assert inventory["passed"] and inventory["active_processes"] == 0
        assert inventory["root_exit_code"] == 0


def shell_alive(pid, created):
    """Whether the shell `pid` (created at FILETIME `created`, so never a reused pid) still runs."""
    k = C.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.argtypes = (W.DWORD, W.BOOL, W.DWORD); k.OpenProcess.restype = W.HANDLE
    k.WaitForSingleObject.argtypes = (W.HANDLE, W.DWORD); k.WaitForSingleObject.restype = W.DWORD
    k.GetProcessTimes.argtypes = (W.HANDLE,) + (C.POINTER(W.FILETIME),) * 4
    k.CloseHandle.argtypes = (W.HANDLE,)
    handle = k.OpenProcess(0x00101000, False, pid)  # query limited + synchronize
    if not handle: return False
    try:
        if created is not None:
            birth, end, kernel_time, user_time = (W.FILETIME() for _ in range(4))
            if not k.GetProcessTimes(handle, C.byref(birth), C.byref(end), C.byref(kernel_time), C.byref(user_time)): return False
            if (birth.dwHighDateTime << 32 | birth.dwLowDateTime) != created: return False
        return k.WaitForSingleObject(handle, 0) == 258  # WAIT_TIMEOUT: still running
    finally:
        k.CloseHandle(handle)


def cleanup(shells, children):
    errors = []
    for shell in shells:
        try:
            created = None
            if shell.pid is None and not shell.ready.exists():
                # T-P3-H2 fix 3: the launch gave up before root.json (Shell.started timed out), but
                # the watcher may still start the shell afterwards: give it the chance to say so.
                end = time.monotonic() + 30
                while not shell.ready.exists() and shell.process.poll() is None and time.monotonic() < end: time.sleep(.2)
            if shell.ready.exists():
                root = json.loads(read_shared(shell.ready))
                shell.pid = shell.pid or root["pid"]; created = root.get("created_filetime")
            if shell.pid:
                # A case that failed early may have left the shell in its native failure box.
                # T-P3-H2 fix 1: that box can open after the case gave up (hello_timeout comes
                # 10-17 s after the start on a loaded machine) and the watcher may be gone already
                # (its 90 s audit deadline), so keep closing while the shell itself runs - not only
                # while its watcher does. reviewA3 left a shell in the box on the desktop this way.
                end = time.monotonic() + 45; asked = False
                while shell_alive(shell.pid, created) and time.monotonic() < end:
                    if close_dialogs(shell.pid):
                        time.sleep(.5); continue
                    if not asked:
                        asked = True
                        try: close_window(shell.pid)
                        except AssertionError: pass
                    time.sleep(.25)
                if shell_alive(shell.pid, created):
                    errors.append({"object": shell.tag, "error": "shell_still_running", "pid": shell.pid})
            if shell.process.poll() is None:
                shell.process.wait(timeout=75)
        except Exception as exc:
            errors.append({"object": shell.tag, "error": repr(exc)})
        finally:
            try:
                shell.log.close()
            except Exception as exc:
                errors.append({"object": shell.tag + "-log", "error": repr(exc)})
    for child in children:
        try:
            if child.poll() is None:
                # All helpers only hold test mutexes/events, never host network ownership.
                child.terminate(); child.wait(timeout=5)
        except Exception as exc:
            errors.append({"object": "helper", "pid": child.pid, "error": repr(exc)})
    return errors


def run(cases=("second-shell", "qt", "residual", "quitting", "inherited-tail")):
    validate()
    import yaml
    # TESSERACT_P3_EVIDENCE_ROOT (T-P3-H): a later ticket re-running this smoke keeps its evidence
    # (and single-instance-summary.json) in its own directory.
    evidence = Path(os.environ.get("TESSERACT_P3_EVIDENCE_ROOT") or REPO / ".build/新界面迁移_工单/evidence/T-P3-02")
    evidence.mkdir(parents=True, exist_ok=True)
    skipped_marker = evidence / "smoke-skipped.json"
    if skipped_marker.exists(): skipped_marker.unlink()
    lock_before = workstation_lock()
    print("workstation before the cases:", json.dumps(lock_before, ensure_ascii=False))
    # Bytecode warm-up as in p3_exit_smoke.ps1 (T-P3-H fix 1, S7): the real sidecar runs with -B, so
    # without a cache every start compiles desktop_runtime.py (~2 MB) again, which alone can pass the
    # shell's 10 s hello window on a loaded machine. The cache lives in the scope's temporary data
    # directory (never the source tree); the sidecars only read it. The hello timeout is unchanged.
    pycache = Path(os.environ["TESSERACT_DATA_DIR"]) / "pycache-warm"
    pycache.mkdir(exist_ok=True)
    warm_env = dict(os.environ, PYTHONPYCACHEPREFIX=str(pycache)); warm_env.pop("PYTHONDONTWRITEBYTECODE", None)
    started = time.monotonic()
    warm = subprocess.run([sys.executable, "-c", "import bridge_main, desktop_runtime"], env=warm_env, cwd=REPO, creationflags=0x08000000)
    print("warm-up import rc=%d %.1f s (bytecode cache %s)" % (warm.returncode, time.monotonic() - started, pycache))
    results = []
    for mode in cases:
        tag = "p3-02-" + uuid.uuid4().hex
        config = Path(os.environ["TESSERACT_CONFIG_DIR"]) / tag
        data = Path(os.environ["TESSERACT_DATA_DIR"]) / tag
        config.mkdir(); data.mkdir()
        case = evidence / (mode + "-" + tag)
        case.mkdir()
        c = yaml.safe_load((REPO / "config.example.yaml").read_bytes())
        c["tiers"]["tier3"]["enabled"] = False
        for tier in ("tier1", "tier2"):
            c["tiers"][tier]["nodes"] = []; c["tiers"][tier]["subscription_urls"] = []
        c["client"]["system_proxy"]["enabled"] = False
        c["alerts"]["enabled"] = False
        if isinstance(c.get("killswitch"), dict): c["killswitch"]["enabled"] = False
        (config / "config.yaml").write_bytes(yaml.safe_dump(c, allow_unicode=True).encode())
        (config / "sources.yaml").write_bytes(b"migrations:\n  tier3_source_portfolio_defaults_v1: true\n  tier3_source_portfolio_defaults_v2: true\n  tier3_source_default_delivery_v1: true\ngithub_repos: []\ntelegram_channels: []\nsubscription_urls: []\nlocal_files: []\n")
        temp = data / "temp"; temp.mkdir()
        env = dict(os.environ, TESSERACT_CONFIG_DIR=str(config), TESSERACT_DATA_DIR=str(data),
            TESSERACT_INSTANCE_SCOPE=tag, TEMP=str(temp), TMP=str(temp), PYTHONDONTWRITEBYTECODE="1",
            TESSERACT_P3_SMOKE_TEMP_ROOT=tempfile.gettempdir(), PYTHONPYCACHEPREFIX=str(pycache))
        env.pop("TESSERACT_SHELL_DEV_MARK_QUITTING", None)
        shell_log = data / "logs/shell.log"
        contains = lambda text: shell_log.exists() and text in read_shared(shell_log, text=True)
        shells = []; children = []
        result = {"case": mode, "passed": False, "data": str(data), "evidence": str(case), "checks": {}}
        del TRANSIENT_READS[:]
        checks = result["checks"]
        def launch(tag):
            shell = Shell(case, tag, env); shells.append(shell)
            return shell.started()
        def launch_helper(kind):
            child = subprocess.Popen([sys.executable, "-B", __file__, "--helper", kind, str(case)], env=env, creationflags=0x08000000)
            children.append(child)
            return child
        try:
            if mode == "inherited-tail":
                parent = launch_helper("parent")
                wait_for(lambda: (case / "helper-ready").exists())
                tail = launch_helper("inherited")
                wait_for(lambda: (case / "sidecar-ready").exists())
                write_shared(case / "parent-release"); assert parent.wait(timeout=5) == 0
                assert tail.poll() is None
                checks["parent_exited_sidecar_alive"] = True
                probe = launch_helper("qt-probe"); assert probe.wait(timeout=5) == 0
                assert (case / "qt-blocked").exists()
                checks["old_qt_blocked_by_sidecar_gate"] = True
                write_shared(case / "sidecar-release"); assert tail.wait(timeout=5) == 0
                checks["sidecar_cleanup_exit_code"] = tail.returncode
            else:
                run_shell_case(mode, case, env, contains, shell_log, launch, launch_helper, checks)
            result["passed"] = True
        except Exception as exc:
            result["error"] = repr(exc)
            # T-P3-H2: where it failed (file:line of the harness frames), not only the exception.
            result["error_where"] = ["%s:%d %s" % (Path(f.filename).name, f.lineno, f.name) for f in traceback.extract_tb(exc.__traceback__)][-4:]
            # Decision wave 6: whether the workstation was locked when the case failed.
            if isinstance(exc, EnvironmentLocked):
                result["lock"] = exc.evidence; result["skipped"] = "environment_locked"
            else:
                try: result["lock"] = workstation_lock()
                except Exception as lock_exc: result["lock"] = {"locked": False, "error": repr(lock_exc)}
        finally:
            result["cleanup_errors"] = cleanup(shells, children)
            result["live_watchers_helpers"] = [p.pid for p in [*(s.process for s in shells), *children] if p.poll() is None]
            result["helpers"] = [{"pid": p.pid, "exit_code": p.poll()} for p in children]
            result["inventories"] = []
            for shell in shells:
                try:
                    result["inventories"].append(json.loads(read_shared(shell.inventory)))
                except Exception as exc:
                    result["cleanup_errors"].append({"object": shell.tag + "-inventory", "error": repr(exc)})
            result["names"] = inspect_names(scope_api.resolve_scope(environ=env))
            result["names_absent"] = all(n["absent"] for n in result["names"].values())
            result["passed"] &= not result["cleanup_errors"] and not result["live_watchers_helpers"] and result["names_absent"]
            if shell_log.exists(): write_shared(case / "shell.log", read_shared(shell_log))
            for name in ("helper-ready", "restored", "qt-blocked", "sidecar-ready"):
                if (case / name).exists(): checks[name] = read_shared(case / name, text=True)
            result["transient_reads"] = list(TRANSIENT_READS)
            write_shared(case / "result.json", json.dumps(result, ensure_ascii=False, indent=2).encode())
            results.append(result)
            write_shared(evidence / "single-instance-summary.json", json.dumps(results, ensure_ascii=False, indent=2).encode())
        # Stop at the first real failure as before. A case the lock screen blocked does not stop
        # the run: the remaining cases still run and must pass (decision wave 6).
        if not result["passed"] and not lock_caused(result):
            raise RuntimeError("smoke failed; inspect " + str(case / "result.json"))
    status = smoke_status(results)
    if status == "skipped":
        marker = {"status": "skipped", "reason": "environment_locked", "lock_before": lock_before,
                  "cases": [{"case": r["case"], "passed": r["passed"], "skipped": r.get("skipped"), "error": r.get("error"),
                             "lock": r.get("lock"), "evidence": r["evidence"]} for r in results]}
        write_shared(skipped_marker, json.dumps(marker, ensure_ascii=False, indent=2).encode())
        print("P3-02 smoke skipped: environment_locked (the lock screen blocked: %s; every other case passed); evidence %s"
              % (", ".join(r["case"] for r in results if not r["passed"]), skipped_marker))
        return
    if status != "passed":
        raise RuntimeError("smoke failed; inspect " + str(evidence / "single-instance-summary.json"))
    print("P3-02 smoke passed: " + ", ".join(cases) + "; no descendants or named objects remain")


def run_shell_case(mode, case, env, contains, shell_log, launch, launch_helper, checks):
    foreground = None
    try:
        if mode in ("qt", "residual"):
            child = launch_helper(mode if mode=="qt" else "runtime")
            wait_for(lambda: (case / "helper-ready").exists())
        if mode == "quitting": env["TESSERACT_SHELL_DEV_MARK_QUITTING"] = "1"
        first = launch("A")
        if mode == "qt":
            first.finish(5); wait_for(lambda: (case / "restored").exists()); assert child.wait(timeout=5)==0
            checks["restore_received_shell_exit_zero"] = True
        elif mode == "residual":
            wait_for(lambda: contains("instance_waiting_for_runtime_cleanup"))
            started = time.monotonic()
            write_shared(case / "release-start", b"go")
            assert child.wait(timeout=5)==0
            wait_for(lambda: contains("shell_window_ready"))
            checks["wait_then_start_seconds"] = time.monotonic() - started
            assert checks["wait_then_start_seconds"] >= 2
            close_window(first.pid); first.finish()
        else:
            wait_for(lambda: contains("shell_window_ready"))
            if mode == "quitting": wait_for(lambda: contains("dev_mark_quitting"))
            if mode == "second-shell":
                hwnd = main_window(first.pid)
                user = C.WinDLL("user32", use_last_error=True)
                user.ShowWindow.argtypes = (W.HWND, C.c_int)
                user.IsWindowVisible.argtypes = (W.HWND,)
                user.IsIconic.argtypes = (W.HWND,)
                user.ShowWindow(hwnd, 6)  # Minimize, then hide (future tray case).
                user.ShowWindow(hwnd, 0)
                assert not user.IsWindowVisible(hwnd)
                foreground = ForegroundHarness(); foreground.grant()
                checks["foreground_permission_granted"] = True
            env.pop("TESSERACT_SHELL_DEV_MARK_QUITTING", None)
            started = time.monotonic()
            second = launch("B")
            if mode == "second-shell":
                second.finish(5)
                checks["second_exit_seconds"] = time.monotonic() - started
                assert checks["second_exit_seconds"] < 5
                wait_for(lambda: contains("restore_event_received"), 5)
                wait_for(lambda: user.IsWindowVisible(hwnd) and not user.IsIconic(hwnd), 5)
                wait_for(lambda: foreground_pid() == first.pid, 5)
                checks["restored_visible_unminimized_foreground_pid"] = foreground_pid()
                assert checks["restored_visible_unminimized_foreground_pid"] == first.pid
                close_window(first.pid); first.finish()
            else:
                wait_for(lambda: contains("instance_waiting_for_quitting_shell"))
                time.sleep(.3)
                assert not second.inventory.exists(), "B exited before A cleanup"
                assert read_shared(shell_log, text=True).count("shell_window_ready") == 1
                checks["B_waited_while_A_alive"] = True
                close_window(first.pid); first.finish()
                wait_for(lambda: read_shared(shell_log, text=True).count("shell_window_ready") == 2)
                checks["B_started_after_A_exit"] = True
                close_window(second.pid); second.finish()
    finally:
        if foreground: foreground.close()


if __name__ == "__main__":
    if len(sys.argv)>1 and sys.argv[1]=="--helper": helper(sys.argv[2], Path(sys.argv[3]))
    elif len(sys.argv)>1 and sys.argv[1]=="--read-selftest": read_selftest(sys.argv[2])
    elif len(sys.argv)>1 and sys.argv[1]=="--lock-selftest": lock_selftest(sys.argv[2])
    else: run()
