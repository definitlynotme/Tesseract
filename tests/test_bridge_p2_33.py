"""T-P2-33: the whole v1 API on real frames, and the frozen-sidecar smoke driver.

pytest runs the *source* sidecar (``runtime_main.pyw``) on a hardened custom
scope: every listener moved to a free high port, every owner that could reach
the network at start-up switched off, ``sources.yaml`` empty, APPDATA and
LOCALAPPDATA redirected into the sandbox.  One session sends one real request
for each of the 38 commands and 10 queries (shell-only commands with the shell
identity), subscribes all 29 topics, and validates every frame against
PROTOCOL.md (bridge_protocol is the executable form of it).

As a script the same scenario drives the *frozen* sidecar::

    python tests/test_bridge_p2_33.py smoke --exe dist-sidecar/tesseract-runtime/tesseract-runtime.exe \
        --sandbox <empty dir> --output <dir>

``scripts/p1_integration_smoke.ps1`` calls it and adds its own HKCU /
firewall before-after checks around both stages.  The driver itself reads
HKCU Internet Settings and the Tesseract / kill-switch firewall rule names
(read only) before the first epoch and after the second, and reads back the
sandbox's sources.yaml / config.yaml / config.local.yaml after the run: the
four source lists stay empty, GitHub discovery stays off and every network
owner stays off, whatever the runtime's reloads and migrations did.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import random
import socket
import subprocess
import sys
import threading
import time
import uuid
from copy import deepcopy
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

import bridge_protocol as protocol  # noqa: E402

TERMINAL = frozenset({"done", "failed", "cancelled", "superseded"})
QT_ROOTS = ("PySide6", "shiboken6", "PySide2", "shiboken2", "PyQt5", "PyQt6")
INTERNET_SETTINGS = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
INTERNET_VALUES = ("ProxyEnable", "ProxyServer", "ProxyOverride", "AutoConfigURL")
# PROTOCOL §10: every command may answer these; structured argument errors use
# the three schema codes.  §11: queries add snapshot_required/io/timeout.
COMMON_CODES = frozenset({"not_allowed", "validation", "conflict", "runtime_unavailable",
                          "runtime_gone", "job_capacity", "internal",
                          "missing_field", "invalid_type", "invalid_enum"})
QUERY_CODES = COMMON_CODES | {"snapshot_required", "io", "timeout"}
# Registered for every command, but in this scenario they can only mean a
# crashed handler or a lost runtime: no request may be answered with them.
FORBIDDEN_CODES = frozenset({"internal", "runtime_unavailable", "runtime_gone"})
# The measured outcome of every request of the catalogue on this scope
# (reply frame, job terminal state, error code, reason_code); any drift fails
# the verdict.  Measured 2026-10-07 on the source and the frozen sidecar.
_RES = ("res", None, None, None)
_HOST = ("error", None, "not_allowed", None)
EXPECTED_OUTCOMES = {
    "alerts.desktopDelivered": ("res", None, "conflict", None),
    # 提醒 redesign 10-30: alerts.test is the settings preview (args {}); it is
    # measured by its round trip only (a bare call would hold the preview slot).
    "alerts.test#desktop": ("ack", "done", None, None),
    "app.info": _RES,
    "app.quit": ("ack", "done", None, None),
    "app.sessionEnd": _RES,
    "app.update.check": ("ack", "failed", "io", None),
    "app.update.install": ("ack", "failed", "not_allowed", None),
    "autostart.state": _RES, "config.snapshot": _RES,
    "exit.verify": ("ack", "failed", "not_running", "service_stopped"),
    "gateway.select": ("ack", "done", None, None),
    "geo.status": _RES,
    # T-P2-H2 (31b-#18): the sandbox disables geo_update; reported as such.
    "geo.update": ("ack", "failed", "conflict", "disabled"),
    "host.openFolder": _HOST, "host.openUrl": _HOST, "host.reveal": _HOST, "host.saveFile": _HOST,
    "import.apply": ("ack", "failed", "conflict", None),
    "import.preview": _RES, "import.specs": _RES,
    "logs.exportBundle": ("ack", "failed", "not_allowed", None),
    "logs.exportFiltered": ("ack", "failed", "not_allowed", None),
    "logs.query": _RES, "logs.setMihomoLevel": _RES, "logs.tail": _RES,
    "mode.set": ("ack", "done", None, None),
    "nodes.cancelDiagnose": ("res", None, "conflict", None),
    "nodes.delete": ("ack", "failed", "conflict", None),
    # T-P2-H2 (C1-#11): an unknown node id in the stopped scope; whether the
    # node projection already holds a sample depends on scheduling (load),
    # so both answers are legitimate.
    "nodes.detail": frozenset({("res", None, "snapshot_required", None),
                               ("res", None, "conflict", None)}),
    "nodes.diagnose": ("ack", "failed", "conflict", "node_not_found"),
    "nodes.reveal": ("res", None, "conflict", None),
    "nodes.update": ("ack", "failed", "conflict", None),
    "service.restart": ("ack", "failed", "conflict", None),
    "service.start": ("ack", "failed", "needs_route", "no_validated_nodes"),
    "service.stop": ("ack", "done", None, None),
    "settings.apply": ("ack", "failed", "conflict", "base_rev_mismatch"),
    "settings.apply#ok": ("ack", "done", None, None),
    "settings.validate": _RES,
    "shell.saveGrant": ("res", None, "not_allowed", None),
    "subscriptions.refresh": ("ack", "failed", "conflict", None),
    "ui.presence": _RES,
    "update.exitResponse": ("res", None, "conflict", None),
    "update.installResult": ("res", None, "conflict", None),
}
ISOLATED_RECOVERY = {key: dict(state="not_needed", error_code="scope_isolated", remaining_count=0)
                     for key in ("proxy", "firewall")}
# One owner per window generation (PROTOCOL §10); the shell uses its own.
PAGE_OWNER = "window-1"
SHELL_OWNER = "shell"
SHUTDOWN_SNAPSHOT_ID = 99
# service.start in this scope: no primary node, Bastion off, GeoData absent with
# the updater off -- every one of these ends before Mihomo is launched.
START_REASONS_IN_SCOPE = frozenset({"no_validated_nodes", "no_sources", "geodata_missing"})
# Snapshot topics without a baseline while the service is stopped: their
# projection is withheld (bridge_tiers.project invalidates it), so
# snapshot_req is answered snapshot_required instead of a snapshot.
# T-P2-H2 (33-#13): gateway.current publishes the saved intent when no Core
# runs, so it now has a baseline in this scope.
STOPPED_UNAVAILABLE = {
    "bastion.pool": "bridge_bastion_pool reports stale while the manager is None",
}
# Event-only or running-only topics this scope never produces; they are
# subscribed and accepted, and their shapes are covered by the v1 fixtures.
NEVER_PUBLISHED_IN_SCOPE = {
    "tier.transition": "needs a valid published route on two tiers",
    "update.prepareExit": "needs a verified installer handoff",
    **STOPPED_UNAVAILABLE,
}


# ------------------------------------------------------------- sandbox config

def _port_free(port: int, udp: bool) -> bool:
    kinds = [socket.SOCK_STREAM] + ([socket.SOCK_DGRAM] if udp else [])
    for kind in kinds:
        for host in ("127.0.0.1", "0.0.0.0"):
            probe = socket.socket(socket.AF_INET, kind)
            try:
                probe.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_EXCLUSIVEADDRUSE", 0x4)
                                 if os.name == "nt" else socket.SO_REUSEADDR, 1)
                probe.bind((host, port))
            except OSError:
                return False
            finally:
                probe.close()
    return True


# Below the Windows dynamic (ephemeral) range 49152-65535, so an outgoing
# connection cannot take a probed port before the sidecar binds it.
PORT_RANGE = (20000, 40000)


def free_high_ports(count: int, *, udp_index: int | None = None) -> list[int]:
    """Distinct, currently unbound ports in PORT_RANGE (outside the ephemeral range)."""
    ports: list[int] = []
    for _ in range(400):
        port = random.randrange(*PORT_RANGE)
        if port in ports:
            continue
        if not _port_free(port, udp=len(ports) == udp_index):
            continue
        ports.append(port)
        if len(ports) == count:
            return ports
    raise RuntimeError("no free high ports")


def hardened_config(ports: dict[str, int]) -> dict:
    """config.example.yaml with every listener moved and every network owner off.

    What could reach the network or the host at start-up, and how it is closed
    (the report lists the code facts behind each line):
      * Mihomo listeners mixed/controller/DNS, subscription server -> free high ports;
      * DNS resolver qualification (probes IP-literal DoH/DoT at Core start)
        -> client.dns.enable=false, so the qualifier has no formal block;
      * GeoData preflight download / periodic refresh -> geo_update.enabled=false
        (should a start ever reach the Core launch, it stops at "GeoData missing
        and updater disabled" before Mihomo runs; in this scope it is already
        deferred earlier: no primary node and Bastion off);
      * Bastion/Tier3 public sources -> tier3.enabled=false, sources.yaml empty;
      * subscriptions -> every subscription_urls list empty;
      * alert channels (incl. desktop/sound) -> enabled=false;
      * automatic exit lookup -> ui.overview_auto_exit_lookup=false;
      * explicit egress proxy -> disabled; system proxy / kill switch -> off
        (the custom scope also forces both off and no-ops the writers);
      * app update check -> the custom scope wires IsolatedUpdater (no network).
    """
    config = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    client = config["client"]
    client["network_mode"] = "system_proxy"
    client["mihomo_mixed_port"] = ports["mixed"]
    client["system_proxy"].update(enabled=False, port=ports["mixed"])
    client["mihomo_controller"]["port"] = ports["controller"]
    client["dns"].update(enable=False, listen=f"127.0.0.1:{ports['dns']}")
    client["egress_recovery"].update(enabled=False, retry_source_refresh=False)
    config["subscription_server"]["port"] = ports["subscription"]
    config["geo_update"]["enabled"] = False
    config["killswitch"]["enabled"] = False
    config["egress_proxy"]["enabled"] = False
    tiers = config["tiers"]
    tiers["tier3"]["enabled"] = False
    for tier in ("tier1", "tier2", "tier3", "exit"):
        tiers[tier]["nodes"] = []
        if "subscription_urls" in tiers[tier]:
            tiers[tier]["subscription_urls"] = []
    config["testing"]["mihomo_helper"]["enabled"] = False
    config["testing"]["xray_helper"]["enabled"] = False
    alerts = config["alerts"]
    for key in ("desktop", "telegram", "whatsapp", "sound"):
        alerts[key]["enabled"] = False
    for channel in alerts["channels"]:
        channel["enabled"] = False
        channel["config"]["enabled"] = False
    config["ui"]["overview_auto_exit_lookup"] = False
    return config


# Empty source lists AND the template's one-shot migration markers: without
# the markers every bootstrap_user_files (desktop_runtime.reload_config runs
# it after gateway.select, settings.apply, ...) merges the default public
# portfolio into this file (app_paths._migrate_tier3_source_portfolio_defaults
# / _migrate_tier3_source_default_delivery).  GitHub discovery stays off.
SOURCE_LISTS = ("github_repos", "telegram_channels", "subscription_urls", "local_files")
SOURCE_MIGRATIONS = ("tier3_source_portfolio_defaults_v1", "tier3_source_portfolio_defaults_v2",
                     "tier3_source_default_delivery_v1")
def hardened_local_config() -> str:
    """The first-run config.local.yaml (app_paths template, with its migration
    markers) with the system proxy off.  Without this file the first
    bootstrap_user_files of a reload_config writes the template, whose
    client.system_proxy.enabled is true, into the sandbox."""
    from app_paths import _local_config_template_text
    local = yaml.safe_load(_local_config_template_text())
    local.setdefault("client", {}).setdefault("system_proxy", {})["enabled"] = False
    return yaml.safe_dump(local, allow_unicode=True, sort_keys=False)


EMPTY_SOURCES = ("migrations:\n" + "".join(f"  {name}: true\n" for name in SOURCE_MIGRATIONS)
                 + "github_discovery:\n  enabled: false\n"
                 + "".join(f"{name}: []\n" for name in SOURCE_LISTS)).encode("ascii")
# The sidecar inherits only these from the driver's environment (no tokens,
# no HTTP(S)_PROXY, no PYTHON*); the sandbox variables are set explicitly.
ENV_ALLOWLIST = frozenset({
    "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "PATH", "PATHEXT", "COMSPEC", "USERPROFILE",
    "HOMEDRIVE", "HOMEPATH", "USERNAME", "USERDOMAIN", "COMPUTERNAME", "OS",
    "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432",
    "COMMONPROGRAMFILES", "COMMONPROGRAMFILES(X86)", "COMMONPROGRAMW6432",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER",
    "PROCESSOR_LEVEL", "PROCESSOR_REVISION",
})


def prepare_sandbox(directory: Path, *, frozen: bool) -> dict:
    """Write the hardened scope into ``directory``; return env + chosen ports."""
    directory = Path(directory)
    config_dir, data_dir, temp = directory / "config", directory / "data", directory / "temp"
    appdata, localappdata = directory / "appdata", directory / "localappdata"
    for path in (config_dir, data_dir, temp, appdata, localappdata):
        path.mkdir(parents=True, exist_ok=True)
    mixed, controller, dns, subscription = free_high_ports(4, udp_index=2)
    ports = dict(mixed=mixed, controller=controller, dns=dns, subscription=subscription)
    text = yaml.safe_dump(hardened_config(ports), allow_unicode=True, sort_keys=False)
    (config_dir / "config.yaml").write_bytes(text.encode("utf-8"))
    (config_dir / "sources.yaml").write_bytes(EMPTY_SOURCES)
    (config_dir / "config.local.yaml").write_bytes(hardened_local_config().encode("utf-8"))
    env = {name: value for name, value in os.environ.items() if name.upper() in ENV_ALLOWLIST}
    env.update(TESSERACT_CONFIG_DIR=str(config_dir), TESSERACT_DATA_DIR=str(data_dir),
               TESSERACT_INSTANCE_SCOPE="p2-33-full-api", PYTHONDONTWRITEBYTECODE="1",
               TEMP=str(temp), TMP=str(temp), APPDATA=str(appdata), LOCALAPPDATA=str(localappdata))
    if not frozen:
        env["PYTHONPATH"] = str(Path(sys.prefix) / "Lib" / "site-packages")
    return dict(env=env, ports=ports, config_dir=config_dir, data_dir=data_dir, temp=temp,
                appdata=appdata, localappdata=localappdata)


def sandbox_readback(sandbox: dict) -> dict:
    """What is on disk in the sandbox now: source lists, discovery, the
    effective (base + local) network owners and listeners, source backups."""
    from main import load_config
    config_dir = Path(sandbox["config_dir"])
    sources = yaml.safe_load((config_dir / "sources.yaml").read_text(encoding="utf-8")) or {}
    config = load_config(str(config_dir / "config.yaml"), str(config_dir / "config.local.yaml"))
    client, alerts, tiers = config["client"], config["alerts"], config["tiers"]
    switched_on = {
        "tiers.tier3.enabled": tiers["tier3"].get("enabled"),
        "geo_update.enabled": config["geo_update"].get("enabled"),
        "client.system_proxy.enabled": client["system_proxy"].get("enabled"),
        "killswitch.enabled": config["killswitch"].get("enabled"),
        "egress_proxy.enabled": config["egress_proxy"].get("enabled"),
        "client.dns.enable": client["dns"].get("enable"),
        "client.egress_recovery.enabled": client["egress_recovery"].get("enabled"),
        "testing.mihomo_helper.enabled": config["testing"]["mihomo_helper"].get("enabled"),
        "testing.xray_helper.enabled": config["testing"]["xray_helper"].get("enabled"),
        "ui.overview_auto_exit_lookup": config["ui"].get("overview_auto_exit_lookup"),
        **{f"alerts.{key}.enabled": alerts[key].get("enabled")
           for key in ("desktop", "telegram", "whatsapp", "sound")},
        "alerts.channels[].enabled": any(channel.get("enabled") or (channel.get("config") or {}).get("enabled")
                                         for channel in alerts.get("channels") or []),
        "tiers.*.subscription_urls": any(tier.get("subscription_urls") for tier in tiers.values()
                                         if isinstance(tier, dict)),
    }
    ports = sandbox["ports"]
    listeners = {
        "mixed": client.get("mihomo_mixed_port") == ports["mixed"] == client["system_proxy"].get("port"),
        "controller": client["mihomo_controller"].get("port") == ports["controller"],
        "dns": str(client["dns"].get("listen", "")).endswith(f":{ports['dns']}"),
        "subscription": config["subscription_server"].get("port") == ports["subscription"],
    }
    backups = Path(sandbox["data_dir"]) / "config-backups"
    return dict(
        source_lists={name: len(sources.get(name) or []) for name in SOURCE_LISTS},
        github_discovery_enabled=(sources.get("github_discovery") or {}).get("enabled"),
        switched_on=sorted(name for name, value in switched_on.items() if value),
        listeners_moved=sorted(name for name, ok in listeners.items() if not ok),
        sources_backups=sorted(path.name for path in backups.glob("sources.yaml*")) if backups.is_dir() else [])


# Written by ``prepare`` next to config/data: what ``readback`` needs (ports,
# directories); never the environment.
SANDBOX_DESCRIPTION = "sandbox.json"


def readback_problems(readback: dict) -> list[str]:
    problems = []
    if any(readback["source_lists"].values()):
        problems.append(f"sandbox sources.yaml was filled: {readback['source_lists']}")
    if readback["github_discovery_enabled"] is not False:
        problems.append(f"sandbox github_discovery.enabled = {readback['github_discovery_enabled']!r}")
    if readback["switched_on"]:
        problems.append(f"sandbox config switched on: {readback['switched_on']}")
    if readback["listeners_moved"]:
        problems.append(f"sandbox listeners no longer on the probed ports: {readback['listeners_moved']}")
    if readback["sources_backups"]:
        problems.append(f"sandbox sources.yaml was rewritten (backups {readback['sources_backups']})")
    return problems


# ------------------------------------------------------------- host readings

FIREWALL_QUERY = (
    "$ErrorActionPreference = 'Stop'; "
    "$names = @(Get-NetFirewallRule -ErrorAction Stop | Where-Object { "
    "$_.Name -like '*Tesseract*' -or $_.DisplayName -like '*Tesseract*' -or "
    "$_.Name -like 'ProxyManager-KillSwitch*' -or $_.DisplayName -like 'ProxyManager-KillSwitch*' } | "
    "ForEach-Object { [string]$_.Name } | Sort-Object); "
    "ConvertTo-Json -InputObject $names -Compress")


def firewall_rules() -> dict:
    """Names of the firewall rules named *Tesseract* or ProxyManager-KillSwitch*
    (killswitch.KILL_SWITCH_RULE_NAMES), read only; a failed query is an error,
    never an empty set."""
    try:
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", FIREWALL_QUERY],
                                capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return dict(error=repr(exc))
    if result.returncode != 0:
        return dict(error=(result.stderr or result.stdout).strip()[-500:])
    try:
        names = json.loads(result.stdout.strip() or "[]")
    except ValueError:
        return dict(error=f"unparsable: {result.stdout[-200:]!r}")
    return dict(names=sorted(names if isinstance(names, list) else [names]))


def host_facts() -> dict:
    return dict(internet_settings=internet_settings(), firewall=firewall_rules())


def internet_settings() -> dict:
    """HKCU proxy values, read only (absent values are recorded as None)."""
    import winreg
    values = {}
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, INTERNET_SETTINGS, 0, winreg.KEY_READ) as key:
        for name in INTERNET_VALUES:
            try:
                values[name] = winreg.QueryValueEx(key, name)[0]
            except FileNotFoundError:
                values[name] = None
    return values


class _ProcessEntry(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]


def _kernel32():
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    api.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    api.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
    api.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    return api


def process_table() -> dict[int, tuple[int, str]]:
    api = _kernel32()
    snapshot = api.CreateToolhelp32Snapshot(0x2, 0)
    table: dict[int, tuple[int, str]] = {}
    if snapshot in (None, wintypes.HANDLE(-1).value):
        return table
    try:
        entry = _ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        ok = api.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            table[int(entry.th32ProcessID)] = (int(entry.th32ParentProcessID), entry.szExeFile)
            ok = api.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        api.CloseHandle(snapshot)
    return table


def process_identity(pid: int) -> tuple[int, bool] | None:
    """(creation time, still running) or None when the PID is gone."""
    api = _kernel32()
    handle = api.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        if not api.GetProcessTimes(handle, *[ctypes.byref(item) for item in times]):
            return None
        created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        code = wintypes.DWORD()
        running = bool(api.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        return created, running
    finally:
        api.CloseHandle(handle)


class DescendantWatch:
    """Polls the process table and remembers every descendant of ``root``."""

    def __init__(self, root: int):
        self.root = root
        self.seen: dict[int, tuple[str, int]] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="p2-33-descendants", daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            self.sample()
            self._stop.wait(.1)

    def sample(self):
        table = process_table()
        tree = {self.root}
        changed = True
        while changed:
            changed = False
            for pid, (parent, name) in table.items():
                if parent in tree and pid not in tree and pid != parent:
                    identity = process_identity(pid)
                    if identity is None:
                        continue
                    tree.add(pid)
                    changed = True
                    self.seen.setdefault(pid, (name, identity[0]))

    def stop(self) -> list[dict]:
        """Stop polling; return the descendants that are still alive."""
        self._stop.set()
        self._thread.join(timeout=5)
        alive = []
        for pid, (name, created) in self.seen.items():
            identity = process_identity(pid)
            if identity is not None and identity[0] == created and identity[1]:
                alive.append(dict(pid=pid, name=name))
        return alive


# --------------------------------------------------------------- the driver

QT_BLOCKING_ENTRY = r"""
import importlib.abc, runpy, sys
class _QtBlock(importlib.abc.MetaPathFinder):
    roots = %r
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in self.roots:
            sys.stderr.write("QT_IMPORT_ATTEMPT " + name + "\n")
            sys.stderr.flush()
            raise ImportError("Qt is blocked in the sidecar: " + name)
        return None
sys.meta_path.insert(0, _QtBlock())
for _root in _QtBlock.roots:
    sys.modules.pop(_root, None)
runpy.run_path("runtime_main.pyw", run_name="__main__")
""" % (QT_ROOTS,)


class Sidecar:
    """A private overlapped pipe, one inherited client handle, one sidecar."""

    def __init__(self, sandbox: dict, *, exe: str | None = None, logs: Path):
        import bridge_transport as bt
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.CreateNamedPipeW.argtypes = [wintypes.LPCWSTR] + [wintypes.DWORD] * 6 + [ctypes.c_void_p]
        api.CreateNamedPipeW.restype = wintypes.HANDLE
        api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                    ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        api.CreateFileW.restype = wintypes.HANDLE
        name = r"\\.\pipe\tesseract-p2-33-" + uuid.uuid4().hex
        # Duplex, overlapped, first instance, reject remote clients.
        server = api.CreateNamedPipeW(name, 3 | 0x40000000 | 0x80000, 8, 1, 65536, 65536, 0, None)
        assert server != wintypes.HANDLE(-1).value
        client = api.CreateFileW(name, 0xC0000000, 0, None, 3, 0x40000000, None)
        assert client != wintypes.HANDLE(-1).value
        assert bt._immediate.SetHandleInformation(client, 1, 1)
        startup = subprocess.STARTUPINFO()
        startup.lpAttributeList = {"handle_list": [client]}
        tail = ["--ipc-handle", str(client), "--shell-pid", str(os.getpid())]
        self.frozen = exe is not None
        argv = ([str(exe), *tail] if exe is not None
                else [sys._base_executable, "-B", "-c", QT_BLOCKING_ENTRY, *tail])
        logs.mkdir(parents=True, exist_ok=True)
        self.stdout_path, self.stderr_path = logs / "sidecar.stdout.log", logs / "sidecar.stderr.log"
        self._stdout = open(self.stdout_path, "wb")
        self._stderr = open(self.stderr_path, "wb")
        self.launched = time.monotonic()
        self.process = subprocess.Popen(argv, env=sandbox["env"], cwd=ROOT, startupinfo=startup,
                                        close_fds=True, stdin=subprocess.DEVNULL,
                                        stdout=self._stdout, stderr=self._stderr)
        bt._immediate.CloseHandle(client)
        self.transport = bt.PipeTransport(server, os.getpid())
        self.watch = DescendantWatch(self.process.pid)
        self.epoch: str | None = None
        self.frames: list[dict] = []
        self.problems: list[str] = []
        self._revs: dict[tuple, int] = {}
        self._seqs: dict[tuple, int] = {}
        self._jobs_seq = 0

    # -- wire
    def send(self, kind: str, **fields) -> dict:
        now = time.time_ns() // 1_000_000
        frame = dict(t=kind, epoch=self.epoch, produced_at_ms=now, sent_at_ms=now, **fields)
        errors = protocol.validate_frame(frame)
        assert not errors, (frame, errors)
        outcome = self.transport.send(frame)
        deadline = time.monotonic() + 5
        while outcome == "pending":
            assert time.monotonic() < deadline, "write timeout"
            time.sleep(.005)
            outcome = self.transport.poll_write()
        assert outcome == "complete", outcome
        return frame

    def receive(self, timeout: float) -> dict:
        frame = self.transport.receive(timeout=timeout)
        if frame is None:
            raise AssertionError(f"frame missing; exit={self.process.poll()}")
        self._check(frame)
        self.frames.append(frame)
        return frame

    def until(self, predicate, timeout: float = 30) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError("deadline")
            frame = self.receive(max(.01, remaining))
            if predicate(frame):
                return frame

    # -- per-frame contract checks (PROTOCOL §2, §4, §6)
    def _check(self, frame: dict) -> None:
        errors = protocol.validate_frame(frame)
        if errors:
            self.problems.append(f"invalid {frame.get('t')}: {errors[:6]}")
        if self.epoch is not None and frame.get("epoch") != self.epoch:
            self.problems.append(f"epoch mismatch on {frame.get('t')}")
        for item in events(frame):
            slot = (item.get("topic"), item.get("key"))
            rev = item.get("rev")
            if isinstance(rev, int):
                if rev <= self._revs.get(slot, 0):
                    self.problems.append(f"rev not increasing on {slot}: {rev}")
                self._revs[slot] = rev
            if item.get("topic") == "jobs":
                if item.get("seq") != self._jobs_seq + 1:
                    self.problems.append(f"jobs seq gap: {self._jobs_seq} -> {item.get('seq')}")
                self._jobs_seq = item.get("seq") or self._jobs_seq
            elif item.get("kind") in {"edge", "gap"} and isinstance(item.get("seq"), int):
                if item["seq"] <= self._seqs.get(slot, 0):
                    self.problems.append(f"seq not increasing on {slot}")
                self._seqs[slot] = item["seq"]

    # -- requests
    def request(self, ident: int, cmd: str, args: dict, *, origin: str) -> tuple[dict, dict]:
        owner = SHELL_OWNER if origin == "shell" else PAGE_OWNER
        request = self.send("req", id=ident, cmd=cmd, args=args, req_id=f"req-{ident}",
                            owner=owner, **{"from": origin})
        reply = self.until(lambda frame: frame.get("id") == ident
                           and frame["t"] in {"ack", "res", "error"})
        return request, reply

    def terminal(self, op: str, timeout: float = 90) -> dict:
        for item in (item for frame in self.frames for item in events(frame)):
            if item["topic"] == "jobs" and item["data"]["op"] == op and item["data"]["state"] in TERMINAL:
                return item
        frame = self.until(lambda frame: any(
            item["topic"] == "jobs" and item["data"]["op"] == op and item["data"]["state"] in TERMINAL
            for item in events(frame)), timeout=timeout)
        return next(item for item in events(frame) if item["topic"] == "jobs"
                    and item["data"]["op"] == op and item["data"]["state"] in TERMINAL)

    def close(self) -> list[dict]:
        self.transport.close()
        if self.process.poll() is None:
            try:
                self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
                self.problems.append("sidecar had to be killed")
        alive = self.watch.stop()
        for stream in (self._stdout, self._stderr):
            stream.close()
        return alive


def events(frame: dict) -> list[dict]:
    return frame["items"] if frame["t"] == "batch" else [frame] if frame["t"] == "evt" else []


# -------------------------------------------------------- request catalogue

def request_args(name: str) -> dict:
    """One real-format, side-effect-safe request for each command and query.

    Unknown ids/refs exercise the refusal paths; nothing here can reach the
    network (the runtime never becomes ready in this scope).
    """
    unknown = "0" * 24
    now_ms = int(time.time() * 1000)
    catalogue = {
        "service.start": {}, "service.stop": {}, "service.restart": {},
        "mode.set": {"mode": "system"},
        "exit.verify": {"trigger": "manual"},
        "settings.apply": {"base_rev": 999999, "changes": [{"section": "ui", "values": {}}]},
        # field is relative to the section (bridge_validation.validate_field).
        "settings.validate": {"section": "network", "field": "mihomo_mixed_port", "value": 7890},
        "nodes.diagnose": {"node_id": unknown, "probe_mode": "direct", "request_id": "diag-p2-33"},
        "nodes.cancelDiagnose": {"request_id": "diag-unknown"},
        "nodes.update": {"item_id": unknown, "updates": {"name": "x"}},
        "nodes.delete": {"item_id": unknown},
        "nodes.reveal": {"item_id": unknown},
        "subscriptions.refresh": {"item_id": unknown},
        "import.preview": {"tier": "sentinel", "text": "not a node"},
        "import.apply": {"tier": "sentinel", "mode": "paste", "plan_id": "plan-unknown"},
        "gateway.select": {"proxy_name": "P2-33-Exit"},
        "geo.update": {"force": True},
        "app.update.check": {}, "app.update.install": {},
        "logs.setMihomoLevel": {"level": "info"},
        "logs.exportBundle": {"save_ref": "save-unknown"},
        "logs.exportFiltered": {"save_ref": "save-unknown", "format": "json", "seqs": [1]},
        "alerts.test": {},
        "alerts.desktopDelivered": {"delivery_id": "desktop-never", "outcome": "delivered",
                                    "surface": "windows"},
        "ui.presence": {"visible": False, "focused": False, "page": "none"},
        "app.quit": {"reason": "user"},
        "shell.saveGrant": {"ref": "save-p2-33", "purpose": "bundle", "format": "zip",
                            "expires_at_ms": now_ms + 60_000, "handle_id": 1234567,
                            "for_owner": PAGE_OWNER},
        "update.exitResponse": {"handoff_id": "handoff-unknown", "accepted": False,
                                "reason_code": "declined"},
        "update.installResult": {"receipt_ref": "receipt-unknown", "launched": False,
                                 "reason_code": "launch_failed"},
        "app.sessionEnd": {"reason": "logoff", "budget_ms": 3000},
        "host.saveFile": {"purpose": "bundle", "format": "zip"},
        "host.openFolder": {"kind": "logs"},
        "host.openUrl": {"url": "https://github.com"},
        "host.reveal": {"ref": "save-p2-33"},
        "app.info": {}, "config.snapshot": {}, "nodes.detail": {"node_id": unknown},
        "import.specs": {},
        "logs.tail": {"n": 5}, "logs.query": {"before_seq": 10, "filter": {}, "n": 5},
        "autostart.state": {}, "geo.status": {},
    }
    return deepcopy(catalogue[name])


def origin_of(name: str) -> str:
    return "shell" if name in protocol.SHELL_COMMANDS else "page"


def allowed_codes(name: str) -> frozenset[str]:
    if name in protocol.V1_QUERIES:
        return QUERY_CODES
    return COMMON_CODES | frozenset(protocol.V1_COMMAND_SPECS[name][6].split())


# ------------------------------------------------------------- the scenario

class Report:
    def __init__(self):
        self.commands: dict[str, dict] = {}
        self.queries: dict[str, dict] = {}
        self.topics: dict[str, dict] = {}
        self.problems: list[str] = []
        self.host: dict = {}

    def bad(self, message: str) -> None:
        self.problems.append(message)


def _error_of(reply: dict) -> dict | None:
    if reply["t"] == "error":
        return reply["error"]
    if reply["t"] == "ack" and not reply.get("accepted"):
        return reply.get("error")
    if reply["t"] == "res" and not reply.get("ok"):
        return reply["error"]
    return None


def exercise(sidecar: Sidecar, report: Report, name: str, ident: int, *, args: dict | None = None,
             keep_data: bool = False) -> dict:
    """Send one request; check the reply and, for a job, its terminal edge."""
    request, reply = sidecar.request(ident, name, request_args(name) if args is None else args,
                                     origin=origin_of(name))
    record = dict(origin=origin_of(name), reply=reply["t"])
    for error in protocol.validate_response(request, reply):
        report.bad(f"{name}: response {error}")
    if name.startswith("host."):
        # §10: host.* run only in the shell; the sidecar refuses them.
        if reply["t"] != "error" or reply["error"]["code"] != "not_allowed":
            report.bad(f"{name}: sidecar must refuse host.* with not_allowed, got {reply}")
        record["code"] = reply.get("error", {}).get("code")
        return record
    mode = (protocol.V1_COMMAND_SPECS[name][2] if name in protocol.V1_COMMANDS else "res")
    error = _error_of(reply)
    if reply["t"] == "ack" and reply.get("accepted"):
        if mode != "job":
            report.bad(f"{name}: ack for a {mode} command")
        job = sidecar.terminal(reply["op"])
        data = job["data"]
        record.update(state=data["state"], op=reply["op"])
        if data["name"] != name:
            report.bad(f"{name}: terminal job named {data['name']}")
        if data.get("req_id") != request["req_id"] or data.get("owner") != request["owner"]:
            report.bad(f"{name}: job lost req_id/owner")
        error = data.get("error")
        if data["state"] == "done":
            record["result"] = data["result"]
    elif reply["t"] == "res" and reply.get("ok"):
        if mode == "job":
            report.bad(f"{name}: res for a job command")
        record["data_keys"] = sorted(reply["data"])
        if keep_data:
            record["data"] = reply["data"]
    if error is not None:
        record["code"] = error["code"]
        record["reason_code"] = (error.get("params") or {}).get("reason_code") or error.get("reason_code")
        if error["code"] not in allowed_codes(name):
            report.bad(f"{name}: error code {error['code']!r} is not registered for it")
        if error["code"] in FORBIDDEN_CODES:
            report.bad(f"{name}: answered {error['code']!r} (a crashed handler or a lost runtime)")
    return record


def desktop_round_trip(sidecar: Sidecar, report: Report, ident: int) -> int:
    """alerts.test of a desktop draft: alerts.desktop (force) -> the shell's
    alerts.desktopDelivered receipt -> the deferred test job finishes ok."""
    args = {}
    request = sidecar.send("req", id=ident, cmd="alerts.test", args=args, req_id=f"req-{ident}",
                           owner=PAGE_OWNER, **{"from": "page"})
    ack = sidecar.until(lambda frame: frame.get("id") == ident and frame["t"] in {"ack", "error"})
    record = dict(origin="page", reply=ack["t"])
    report.commands["alerts.test#desktop"] = record
    for error in protocol.validate_response(request, ack):
        report.bad(f"alerts.test#desktop: response {error}")
    if ack["t"] != "ack" or not ack.get("accepted"):
        report.bad(f"alerts.test#desktop not accepted: {ack}")
        return ident
    notice = sidecar.until(lambda frame: any(item["topic"] == "alerts.desktop" for item in events(frame)))
    item = next(item for item in events(notice) if item["topic"] == "alerts.desktop")
    record["notice"] = dict(force=item["data"].get("force"), target=item["data"]["target"],
                            severity=item["data"]["severity"])
    if item["data"].get("force") is not True:
        report.bad(f"a forced desktop test must carry force=true: {item['data']}")
    ident += 1
    receipt_args = {"delivery_id": item["data"]["delivery_id"], "outcome": "delivered",
                    "surface": "windows"}
    receipt_req = sidecar.send("req", id=ident, cmd="alerts.desktopDelivered", args=receipt_args,
                               req_id=f"req-{ident}", owner=SHELL_OWNER, **{"from": "shell"})
    receipt = sidecar.until(lambda frame: frame.get("id") == ident and frame["t"] in {"res", "error"})
    for error in protocol.validate_response(receipt_req, receipt):
        report.bad(f"alerts.desktopDelivered#receipt: response {error}")
    record["receipt"] = receipt.get("data") if receipt.get("ok") else receipt.get("error")
    job = sidecar.terminal(ack["op"])["data"]
    record.update(state=job["state"], result=job.get("result"))
    if job["state"] != "done" or (job.get("result") or {}).get("delivery_id") != receipt_args["delivery_id"]:
        report.bad(f"alerts.test#desktop ended {job}")
    return ident


def receive_hello(sidecar: Sidecar, report: Report, label: str) -> dict:
    """hello of an epoch: v1 caps (38 commands/queries, 29 topics, features
    []), custom scope, this process, both recovery domains scope_isolated."""
    hello = sidecar.receive(30)
    sidecar.hello_after_s = round(time.monotonic() - sidecar.launched, 3)
    assert hello["t"] == "hello", hello
    sidecar.epoch = hello["epoch"]
    caps = hello["caps"]
    if set(caps["commands"]) != protocol.HELLO_CAP_COMMANDS or len(caps["commands"]) != 38:
        report.bad(f"{label}: hello.caps.commands is not the v1 set: "
                   f"{sorted(set(caps['commands']) ^ protocol.HELLO_CAP_COMMANDS)}")
    if set(caps["topics"]) != protocol.V1_TOPICS or len(caps["topics"]) != 29:
        report.bad(f"{label}: hello.caps.topics is not the v1 set: "
                   f"{sorted(set(caps['topics']) ^ protocol.V1_TOPICS)}")
    if caps.get("features") != []:
        report.bad(f"{label}: hello.caps.features {caps.get('features')!r}")
    if hello["scope"]["kind"] != "custom" or hello["runtime_pid"] != sidecar.process.pid:
        report.bad(f"{label}: hello scope/pid: {hello['scope']} {hello['runtime_pid']}")
    if hello["network_recovery"] != ISOLATED_RECOVERY:
        report.bad(f"{label}: hello.network_recovery {hello['network_recovery']}")
    sidecar.send("hello_ack", proto=1, accepted=True)
    return hello


def receive_bye(sidecar: Sidecar, report: Report, label: str) -> dict:
    """bye is the last frame: nothing may follow it (2 s of silence)."""
    bye = sidecar.until(lambda frame: frame["t"] == "bye", timeout=90)
    try:
        while True:
            extra = sidecar.receive(2)
            report.bad(f"{label}: frame after bye: {extra['t']}")
    except AssertionError:
        pass
    if bye["network_recovery"] != ISOLATED_RECOVERY:
        report.bad(f"{label}: bye.network_recovery {bye['network_recovery']}")
    return bye


def hello_summary(hello: dict) -> dict:
    caps = hello["caps"]
    return dict(caps_commands=len(caps["commands"]), caps_topics=len(caps["topics"]),
                features=caps["features"], scope=hello["scope"]["kind"],
                network_recovery=hello["network_recovery"])


def scenario(sidecar: Sidecar, report: Report, *, read_host: bool = True) -> dict:
    hello = receive_hello(sidecar, report, "full_api")
    caps = hello["caps"]

    # Subscribe every declared topic in one frame (the shell is the subscriber;
    # §13: it never forwards shell topics to a page, which is a shell rule).
    topics = sorted(caps["topics"])
    subscribe = sidecar.send("subscribe", id=1, topics=topics)
    subscribed = sidecar.until(lambda frame: frame.get("id") == 1 and frame["t"] in {"subscribed", "error"})
    for error in protocol.validate_control_reply(subscribe, subscribed):
        report.bad(f"subscribe: {error}")
    # §13: the shutdown topic has no baseline before the quit starts; its
    # snapshot_req is answered once the first phase is published.
    sidecar.send("snapshot_req", id=SHUTDOWN_SNAPSHOT_ID, topic="shutdown")
    expected = set()
    for topic in protocol.V1_SNAPSHOT_TOPICS - set(STOPPED_UNAVAILABLE) - {"shutdown"}:
        keys = sorted({"vanguard", "sentinel", "bastion", "gateway"}) if topic in protocol.KEYED_TOPICS else [None]
        expected |= {(topic, key) for key in keys}
    seen: set[tuple] = set()
    deadline = time.monotonic() + 30
    while not expected <= seen and time.monotonic() < deadline:
        try:
            frame = sidecar.receive(max(.01, deadline - time.monotonic()))
        except AssertionError:
            break
        for item in events(frame):
            if item["kind"] == "snapshot" and not item.get("page", {}).get("index"):
                seen.add((item["topic"], item.get("key")))
    report.topics["initial_snapshots_missing"] = sorted(f"{t}#{k}" if k else t for t, k in expected - seen)

    # §4 / §11: a replacement snapshot for every snapshot topic (keyed per key).
    ident = 10
    stopped = {(topic, None) for topic in STOPPED_UNAVAILABLE}
    for topic, key in sorted(expected | stopped, key=lambda pair: (pair[0], pair[1] or "")):
        ident += 1
        fields = dict(id=ident, topic=topic, **({"key": key} if key else {}))
        req = sidecar.send("snapshot_req", **fields)
        res = sidecar.until(lambda frame: frame.get("id") == ident and frame["t"] in {"snapshot_res", "error"})
        for error in protocol.validate_control_reply(req, res):
            report.bad(f"snapshot_req {topic}#{key}: {error}")
        answer = "snapshot" if "snapshot" in res else res.get("error", {}).get("code")
        report.topics.setdefault(topic, {}).setdefault("snapshot_res", {})[key or "-"] = answer
        if (topic, key) in stopped:
            # Stopped scope: no Core route / no manager, so no baseline; the
            # request is still answered (§2: snapshot or a structured error).
            if answer not in {"snapshot", "snapshot_required"}:
                report.bad(f"snapshot_req {topic}: {res}")
        elif answer != "snapshot":
            report.bad(f"snapshot_req {topic}#{key}: {res.get('error')}")

    # Control frames: ping, cancel of an unknown op, owner_revoke of an unused owner.
    sidecar.send("ping", n=7)
    sidecar.until(lambda frame: frame["t"] == "pong" and frame["n"] == 7)
    ident = 300
    sidecar.send("cancel", id=ident, op="op-never-issued")
    cancel = sidecar.until(lambda frame: frame.get("id") == ident and frame["t"] in {"ack", "error"})
    if cancel["t"] != "ack" or cancel["accepted"] is not False:
        report.bad(f"cancel of an unknown op: {cancel}")
    ident += 1
    revoke = sidecar.send("owner_revoke", id=ident, owner="window-p2-33-unused", **{"from": "shell"})
    revoked = sidecar.until(lambda frame: frame.get("id") == ident and frame["t"] in {"ack", "error"})
    for error in protocol.validate_control_reply(revoke, revoked):
        report.bad(f"owner_revoke: {error}")

    # service.start first: it must run and end (failed is the expected end in
    # this scope: no sources, GeoData absent with the updater off).
    ident = 1000
    if read_host:
        report.host["internet_settings_before_start"] = internet_settings()
    report.commands["service.start"] = exercise(sidecar, report, "service.start", ident)
    if read_host:
        report.host["internet_settings_after_start"] = internet_settings()
    report.commands["service.stop"] = exercise(sidecar, report, "service.stop", ident + 1)
    if report.commands["service.stop"].get("state") != "done":
        report.bad(f"service.stop did not finish done: {report.commands['service.stop']}")
    ident += 10
    for name in sorted(protocol.V1_COMMANDS - {"service.start", "service.stop", "app.quit", "app.sessionEnd",
                                               "alerts.test"}):
        ident += 1
        report.commands[name] = exercise(sidecar, report, name, ident,
                                         keep_data=name == "settings.validate")
    for name in sorted(protocol.V1_QUERIES):
        ident += 1
        report.queries[name] = exercise(sidecar, report, name, ident)
        if report.queries[name].get("reply") != "res":
            report.bad(f"{name}: query answered with {report.queries[name]}")
    # Success paths with no effect outside the sandbox: settings.validate of a
    # legal field (above) is valid; settings.apply of the ui section with the
    # current base_rev (values already on disk) finishes done.
    record = report.commands["settings.validate"]
    if (record.get("data") or {}).get("valid") is not True:
        report.bad(f"settings.validate of a legal field: {record}")
    ident += 1
    snapshot = exercise(sidecar, report, "config.snapshot", ident, keep_data=True)
    ident += 1
    args = {"base_rev": (snapshot.get("data") or {}).get("rev", 0),
            "changes": [{"section": "ui", "values": {"language": "en_US",
                                                      "overview_auto_exit_lookup": False}}]}
    report.commands["settings.apply#ok"] = exercise(sidecar, report, "settings.apply", ident, args=args)
    ident = desktop_round_trip(sidecar, report, ident + 1)
    # The shell's own config.snapshot carries the same contract.
    info = next(frame for frame in reversed(sidecar.frames)
                if frame.get("t") == "res" and frame.get("cmd") == "app.info")
    if info.get("ok") and set(info["data"]["caps"]) != protocol.HELLO_CAP_COMMANDS:
        report.bad("app.info.caps differs from hello.caps.commands")
    # Full exit: app.quit with the shell identity; bye is the last frame.
    ident += 1
    report.commands["app.quit"] = exercise(sidecar, report, "app.quit", ident)
    bye = receive_bye(sidecar, report, "full_api")
    pending = [frame for frame in sidecar.frames
               if frame.get("t") == "snapshot_res" and frame.get("id") == SHUTDOWN_SNAPSHOT_ID]
    phases = [item["data"]["phase"] for frame in sidecar.frames for item in events(frame)
              if item["topic"] == "shutdown"]
    report.topics["shutdown"] = dict(snapshot_req_answered=bool(pending),
                                     answer=("snapshot" if pending and "snapshot" in pending[0]
                                             else pending[0].get("error", {}).get("code") if pending else None),
                                     phases=phases)
    if not phases or phases[-1] != "finished":
        report.bad(f"shutdown phases {phases}")
    if not pending:
        report.bad("snapshot_req for shutdown (sent before the quit) was never answered")
    elif "snapshot" not in pending[0]:
        # T-P2-H2 (33-#11): held behind the barrier, answered from the state
        # at dequeue; the phases exist by then.
        report.bad(f"snapshot_req for shutdown answered without the baseline: {pending[0]}")
    order = ("draining", "stopping", "recovering", "waiting_workers", "finished")
    if any(order.index(a) > order.index(b) for a, b in zip(phases, phases[1:])):
        # T-P2-H2 (33-#12): no older phase may follow a newer snapshot.
        report.bad(f"shutdown phases step back: {phases}")
    return dict(hello=hello_summary(hello),
                bye=dict(exit_code=bye["exit_code"], network_recovery=bye["network_recovery"]),
                topics_seen=sorted({item["topic"] for frame in sidecar.frames for item in events(frame)}))


def session_end_scenario(sidecar: Sidecar, report: Report) -> dict:
    """A second epoch that ends with the shell's logoff fast path."""
    hello = receive_hello(sidecar, report, "session_end")
    sidecar.send("subscribe", id=1, topics=sorted(hello["caps"]["topics"]))
    sidecar.until(lambda frame: frame.get("id") == 1 and frame["t"] == "subscribed")
    record = exercise(sidecar, report, "app.sessionEnd", 2, keep_data=True)
    report.commands["app.sessionEnd"] = record
    bye = receive_bye(sidecar, report, "session_end")
    return dict(hello=hello_summary(hello), record=record,
                bye=dict(exit_code=bye["exit_code"], network_recovery=bye["network_recovery"]))


def warm_source_bytecode(sandbox: dict) -> float:
    """T-P2-H2 (决定_第五波裁定 T-P4-C2 #10), source form only: as in the p3
    smokes, one child imports the sidecar's Qt-free closure once, writing
    bytecode into a PYTHONPYCACHEPREFIX inside the sandbox's temp directory
    (never the source tree); both epochs' sidecars keep -B and
    PYTHONDONTWRITEBYTECODE and only read it, so a loaded machine does not
    spend the hello window compiling desktop_runtime.py. Returns seconds."""
    from sidecar_import_graph import sidecar_modules
    cache = Path(sandbox["temp"]) / "pycache-warm"
    cache.mkdir(parents=True, exist_ok=True)
    script = ("import importlib, json, sys\n"
              "for name in json.loads(sys.argv[1]):\n"
              "    try:\n        importlib.import_module(name)\n"
              "    except Exception:\n        pass\n")
    env = {key: value for key, value in sandbox["env"].items() if key != "PYTHONDONTWRITEBYTECODE"}
    env["PYTHONPYCACHEPREFIX"] = str(cache)
    # Imports never write a profile; should one, it lands outside the checked dirs.
    profile = Path(sandbox["temp"]) / "warm-profile"
    profile.mkdir(exist_ok=True)
    env.update(APPDATA=str(profile), LOCALAPPDATA=str(profile))
    started = time.monotonic()
    try:
        subprocess.run([sys._base_executable, "-c", script, json.dumps(sidecar_modules())],
                       env=env, cwd=ROOT, capture_output=True, timeout=900)
    except subprocess.TimeoutExpired:
        pass    # a partial cache still helps; the sidecar compiles the rest
    sandbox["env"]["PYTHONPYCACHEPREFIX"] = str(cache)
    return round(time.monotonic() - started, 1)


def run(sandbox: dict, *, exe: str | None, output: Path) -> dict:
    report = Report()
    summary: dict = dict(form="frozen" if exe else "source", ports=sandbox["ports"])
    if exe is None:
        summary["bytecode_warmup_s"] = warm_source_bytecode(sandbox)
    report.host["run_before"] = host_facts()
    started = time.monotonic()
    for label, body in (("full_api", scenario), ("session_end", session_end_scenario)):
        sidecar = Sidecar(sandbox, exe=exe, logs=output / label)
        try:
            summary[label] = body(sidecar, report)
            summary[label]["exit_code"] = sidecar.process.wait(timeout=60)
            summary[label]["hello_after_s"] = getattr(sidecar, "hello_after_s", None)
        except Exception as exc:  # recorded; the verdict fails on it
            report.bad(f"{label}: aborted: {exc!r}"[:2000])
            summary[label] = {}
        finally:
            alive = sidecar.close()
            report.problems.extend(f"{label}: {problem}" for problem in sidecar.problems)
            summary.setdefault("residual_processes", {})[label] = alive
            summary.setdefault("descendants_seen", {})[label] = sorted(
                {name for name, _created in sidecar.watch.seen.values()})
            with open(output / label / "frames.jsonl", "w", encoding="utf-8", newline="\n") as stream:
                for frame in sidecar.frames:
                    stream.write(json.dumps(frame, ensure_ascii=False, sort_keys=True) + "\n")
            stderr = sidecar.stderr_path.read_text(encoding="utf-8", errors="replace")
            summary.setdefault("qt_import_attempts", {})[label] = [
                line for line in stderr.splitlines() if line.startswith("QT_IMPORT_ATTEMPT")]
    summary["seconds"] = round(time.monotonic() - started, 1)
    report.host["run_after"] = host_facts()
    try:
        summary["sandbox_readback"] = sandbox_readback(sandbox)
    except Exception as exc:  # the verdict fails on it
        summary["sandbox_readback"] = dict(error=repr(exc)[:2000])
    summary["commands"] = report.commands
    summary["queries"] = report.queries
    summary["topics"] = report.topics
    summary["host"] = report.host
    summary["leaked_default_dirs"] = sorted(
        str(path) for base in (sandbox["appdata"], sandbox["localappdata"])
        for path in Path(base).iterdir())
    summary["problems"] = report.problems
    return summary


def verdict(summary: dict) -> list[str]:
    """Every acceptance condition of the scenario; empty means pass."""
    failures = list(summary["problems"])
    for label in ("full_api", "session_end"):
        part = summary.get(label) or {}
        if part.get("exit_code") != 0 or (part.get("bye") or {}).get("exit_code") != 0:
            failures.append(f"{label}: exit {part.get('exit_code')} bye {(part.get('bye') or {}).get('exit_code')}")
        hello = part.get("hello") or {}
        if (hello.get("caps_commands"), hello.get("caps_topics"), hello.get("features"),
                hello.get("scope"), hello.get("network_recovery")) != (38, 29, [], "custom", ISOLATED_RECOVERY):
            failures.append(f"{label}: hello {hello}")
        if (part.get("bye") or {}).get("network_recovery") != ISOLATED_RECOVERY:
            failures.append(f"{label}: bye.network_recovery {(part.get('bye') or {}).get('network_recovery')}")
        if summary["residual_processes"].get(label):
            failures.append(f"{label}: residual processes {summary['residual_processes'][label]}")
        if summary["qt_import_attempts"].get(label):
            failures.append(f"{label}: Qt import attempts {summary['qt_import_attempts'][label]}")
    covered = set(summary["commands"]) | set(summary["queries"])
    if "alerts.test#desktop" in covered:   # alerts.test is measured by its round trip only
        covered.add("alerts.test")
    missing = (protocol.V1_COMMANDS | protocol.V1_QUERIES) - covered
    if missing:
        failures.append(f"not exercised: {sorted(missing)}")
    records = {**summary["commands"], **summary["queries"]}
    for name, expected in sorted(EXPECTED_OUTCOMES.items()):
        record = records.get(name) or {}
        outcome = (record.get("reply"), record.get("state"), record.get("code"), record.get("reason_code"))
        if outcome not in (expected if isinstance(expected, frozenset) else {expected}):
            failures.append(f"{name}: outcome {outcome} drifted from {expected}")
    if summary["topics"].get("initial_snapshots_missing"):
        failures.append(f"no initial snapshot: {summary['topics']['initial_snapshots_missing']}")
    seen = set((summary.get("full_api") or {}).get("topics_seen", ()))
    unseen = protocol.V1_TOPICS - seen
    if unseen - set(NEVER_PUBLISHED_IN_SCOPE):
        failures.append(f"topics never published: {sorted(unseen - set(NEVER_PUBLISHED_IN_SCOPE))}")
    desktop = summary["commands"].get("alerts.test#desktop", {})
    # 提醒 redesign 10-30: a preview receipt is answered but never persisted.
    if desktop.get("state") != "done" or (desktop.get("receipt") or {}).get("recorded") is not False:
        failures.append(f"desktop alert round trip: {desktop}")
    start = summary["commands"].get("service.start", {})
    # Empty sources, Bastion off: the start runs, is deferred fail-closed and
    # its job ends failed with a classified reason (PROTOCOL §14); Mihomo is
    # never launched (no descendant process at all).
    if start.get("state") != "failed" or start.get("reason_code") not in START_REASONS_IN_SCOPE:
        failures.append(f"service.start: {start}")
    launched = [name for names in summary["descendants_seen"].values() for name in names
                if "mihomo" in name.lower()]
    if launched:
        failures.append(f"Mihomo was launched: {launched}")
    if summary["commands"].get("service.stop", {}).get("state") != "done":
        failures.append(f"service.stop: {summary['commands'].get('service.stop')}")
    host = summary.get("host") or {}
    if host.get("internet_settings_before_start") != host.get("internet_settings_after_start"):
        failures.append("HKCU Internet Settings changed across service.start")
    before, after = host.get("run_before") or {}, host.get("run_after") or {}
    if not before or not after:
        failures.append("host facts were not read before and after the run")
    else:
        if before["internet_settings"] != after["internet_settings"]:
            failures.append(f"HKCU Internet Settings changed across the run: "
                            f"{before['internet_settings']} -> {after['internet_settings']}")
        for when, facts in (("before", before), ("after", after)):
            if "error" in facts["firewall"]:
                failures.append(f"firewall rules unreadable {when} the run: {facts['firewall']['error']}")
        if before["firewall"] != after["firewall"]:
            failures.append(f"firewall rules changed across the run: {before['firewall']} -> {after['firewall']}")
    if summary["leaked_default_dirs"]:
        failures.append(f"default profile directories were written: {summary['leaked_default_dirs']}")
    readback = summary.get("sandbox_readback") or {}
    if "error" in readback or not readback:
        failures.append(f"sandbox readback failed: {readback}")
    else:
        failures.extend(readback_problems(readback))
    end = (summary.get("session_end") or {}).get("record", {})
    data = end.get("data") or {}
    # §13 logoff fast path in the custom scope: writes revoked, nothing to
    # recover (Recovery DTO not_needed; scope_isolated is in hello/bye).
    if end.get("reply") != "res" or end.get("code") is not None or data.get("writes_revoked") is not True \
            or data.get("recovery") != {"proxy": "not_needed", "firewall": "not_needed"}:
        failures.append(f"app.sessionEnd: {end}")
    return failures


# ------------------------------------------------------------------- pytest

def test_hardened_config_moves_every_listener_and_disables_network_owners(tmp_path):
    sandbox = prepare_sandbox(tmp_path, frozen=False)
    config = yaml.safe_load((sandbox["config_dir"] / "config.yaml").read_text(encoding="utf-8"))
    ports = sandbox["ports"]
    assert len(set(ports.values())) == 4 and min(ports.values()) >= 20000
    assert {7890, 9090, 1053, 25500}.isdisjoint(ports.values())
    client = config["client"]
    assert client["mihomo_mixed_port"] == client["system_proxy"]["port"] == ports["mixed"]
    assert client["mihomo_controller"]["port"] == ports["controller"]
    assert client["dns"]["listen"].endswith(f":{ports['dns']}") and client["dns"]["enable"] is False
    assert config["subscription_server"]["port"] == ports["subscription"]
    assert config["geo_update"]["enabled"] is False and config["tiers"]["tier3"]["enabled"] is False
    assert config["ui"]["overview_auto_exit_lookup"] is False
    assert not any(channel["enabled"] or channel["config"]["enabled"]
                   for channel in config["alerts"]["channels"])
    assert (sandbox["config_dir"] / "sources.yaml").read_bytes() == EMPTY_SOURCES
    assert min(ports.values()) >= PORT_RANGE[0] and max(ports.values()) < PORT_RANGE[1]
    assert not {"GITHUB_TOKEN", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "PYTHONHOME",
                "TESSERACT_ISOLATED_AUDIT_MANIFEST"} & {name.upper() for name in sandbox["env"]}
    # The same loader the sidecar uses accepts the file and keeps the values.
    from app_paths import build_app_paths
    from main import load_config
    paths = build_app_paths(config_dir=sandbox["config_dir"], data_dir=sandbox["data_dir"])
    loaded = load_config(str(paths.config_path), str(paths.local_config_path))
    assert loaded["client"]["mihomo_controller"]["port"] == ports["controller"]
    assert loaded["geo_update"]["enabled"] is False
    # The DNS qualifier sees no formal block, so it never probes resolvers.
    from clash_formatter import resolved_client_dns_block
    assert resolved_client_dns_block(loaded["client"]["dns"]) is None
    assert readback_problems(sandbox_readback(sandbox)) == []
    # desktop_runtime.reload_config (after gateway.select, settings.apply, ...)
    # runs bootstrap_user_files on this directory: with the migration markers
    # the source lists stay empty and nothing is switched on.
    from app_paths import bootstrap_user_files
    for _ in range(2):
        bootstrap_user_files(paths)
        readback = sandbox_readback(sandbox)
        assert readback_problems(readback) == [], readback
    assert readback["source_lists"] == dict.fromkeys(SOURCE_LISTS, 0)


def test_sandbox_readback_catches_a_filled_source_file(tmp_path):
    """The verdict's readback is not vacuous: the pre-fix sources.yaml (no
    migration markers) gets the default portfolio on the first bootstrap."""
    sandbox = prepare_sandbox(tmp_path, frozen=False)
    sources = sandbox["config_dir"] / "sources.yaml"
    sources.write_bytes(b"github_repos: []\ntelegram_channels: []\nsubscription_urls: []\nlocal_files: []\n")  # sources-marker: intentional
    from app_paths import bootstrap_user_files, build_app_paths
    bootstrap_user_files(build_app_paths(config_dir=sandbox["config_dir"], data_dir=sandbox["data_dir"]))
    problems = readback_problems(sandbox_readback(sandbox))
    assert any("sources.yaml was filled" in problem for problem in problems), problems


def test_verdict_rejects_drift_crashes_and_unchecked_claims():
    """Pinned outcomes, forbidden codes and the session-end / bye / hello
    claims are enforced by the verdict itself."""
    def record(outcome):
        reply, state, code, reason = outcome
        return {key: value for key, value in dict(reply=reply, state=state, code=code,
                                                  reason_code=reason).items() if value is not None}
    hello = dict(caps_commands=38, caps_topics=29, features=[], scope="custom",
                 network_recovery=ISOLATED_RECOVERY)
    bye = dict(exit_code=0, network_recovery=ISOLATED_RECOVERY)
    facts = dict(internet_settings={"ProxyEnable": 0}, firewall=dict(names=["Tesseract-x"]))
    records = {name: record(sorted(outcome)[0] if isinstance(outcome, frozenset) else outcome)
               for name, outcome in EXPECTED_OUTCOMES.items()}
    records["alerts.test#desktop"].update(receipt=dict(recorded=False))
    good = dict(
        problems=[], residual_processes={}, qt_import_attempts={}, descendants_seen={},
        commands={name: value for name, value in records.items() if name not in protocol.V1_QUERIES},
        queries={name: value for name, value in records.items() if name in protocol.V1_QUERIES},
        topics=dict(initial_snapshots_missing=[]),
        full_api=dict(exit_code=0, hello=hello, bye=bye, topics_seen=sorted(protocol.V1_TOPICS)),
        session_end=dict(exit_code=0, hello=hello, bye=bye,
                         record=dict(reply="res", data=dict(writes_revoked=True, workers_pending=True,
                                                            recovery=dict(proxy="not_needed",
                                                                          firewall="not_needed")))),
        host=dict(run_before=facts, run_after=deepcopy(facts)), leaked_default_dirs=[],
        sandbox_readback=dict(source_lists=dict.fromkeys(SOURCE_LISTS, 0), github_discovery_enabled=False,
                              switched_on=[], listeners_moved=[], sources_backups=[]))
    assert verdict(good) == []

    def broken(change) -> list[str]:
        summary = deepcopy(good)
        change(summary)
        return verdict(summary)

    assert broken(lambda s: s["commands"]["gateway.select"].update(state="failed", code="internal"))
    # Each listed alternative passes; anything else still drifts (T-P2-H2).
    assert broken(lambda s: s["queries"]["nodes.detail"].update(code="conflict")) == []
    assert broken(lambda s: s["queries"]["nodes.detail"].update(code="io"))
    assert broken(lambda s: s["session_end"]["record"].update(code="io"))
    assert broken(lambda s: s["session_end"]["record"]["data"]["recovery"].update(proxy="failed"))
    assert broken(lambda s: s["session_end"]["hello"].update(features=["x"]))
    assert broken(lambda s: s["session_end"]["bye"].update(network_recovery={}))
    assert broken(lambda s: s["host"]["run_after"].update(firewall=dict(error="denied")))
    assert broken(lambda s: s["host"]["run_after"]["internet_settings"].update(ProxyEnable=1))
    assert broken(lambda s: s["sandbox_readback"]["source_lists"].update(github_repos=12))
    assert broken(lambda s: s["sandbox_readback"].update(switched_on=["tiers.tier3.enabled"]))


def test_request_catalogue_is_valid_for_every_command_and_query():
    for name in sorted(protocol.V1_COMMANDS | protocol.V1_QUERIES):
        origin = origin_of(name)
        frame = dict(t="req", epoch="e", produced_at_ms=1, sent_at_ms=1, id=1, cmd=name,
                     args=request_args(name), req_id="r", owner="o", **{"from": origin})
        assert protocol.validate_frame(frame) == [], name
        if origin == "page":
            assert protocol.validate_page_request(frame) == [], name
    assert len(protocol.V1_COMMANDS) == 34 and len(protocol.V1_QUERIES) == 8
    assert len(protocol.V1_TOPICS) == 29


def test_v1_fixture_inventory_has_a_sample_for_every_topic_and_reply():
    valid = ROOT / "bridge_schema" / "fixtures" / "v1" / "valid"
    names = {path.stem for path in valid.glob("*.json")}
    assert {f"topic_{topic}" for topic in protocol.V1_TOPICS} <= names
    for stem in ("subscribe_v1_all", "subscribed_v1_all", "snapshot_req_nodes.list_key",
                 "snapshot_res_nodes.list_key", "snapshot_res_gateway.current_stopped",
                 "error_host_command_refused", "jobs_service.start_failed_isolated",
                 "res_alerts.desktopDelivered_recorded", "res_app.sessionEnd_isolated",
                 "bye_isolated"):
        assert stem in names, stem
    subscribe = json.loads((valid / "subscribe_v1_all.json").read_bytes())
    subscribed = json.loads((valid / "subscribed_v1_all.json").read_bytes())
    assert set(subscribe["topics"]) == protocol.V1_TOPICS
    assert protocol.validate_control_reply(subscribe, subscribed) == []
    request = json.loads((valid / "snapshot_req_nodes.list_key.json").read_bytes())
    reply = json.loads((valid / "snapshot_res_nodes.list_key.json").read_bytes())
    assert protocol.validate_control_reply(request, reply) == []
    for path in sorted(valid.glob("*.json")):
        frame = json.loads(path.read_bytes())
        assert protocol.validate_frame(frame) == [], path.name


def _skip_unless_windows():
    import pytest
    if os.name != "nt":
        pytest.skip("Windows integration")


def test_source_sidecar_answers_the_whole_v1_api_on_real_frames(tmp_path):
    """34 commands, 8 queries, 29 topics, on the real pipe; no Qt import."""
    _skip_unless_windows()
    sandbox = prepare_sandbox(tmp_path / "sandbox", frozen=False)
    summary = run(sandbox, exe=None, output=tmp_path / "out")
    failures = verdict(summary)
    assert not failures, json.dumps(dict(failures=failures, commands=summary["commands"],
                                         queries=summary["queries"]), ensure_ascii=False, indent=1)


# ------------------------------------------------------------------ script

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="test_bridge_p2_33.py")
    sub = parser.add_subparsers(dest="command", required=True)
    smoke = sub.add_parser("smoke", help="drive a sidecar through the whole v1 API")
    smoke.add_argument("--exe", help="frozen tesseract-runtime.exe; omit for the source entry")
    smoke.add_argument("--sandbox", required=True, type=Path)
    smoke.add_argument("--output", required=True, type=Path)
    prepare = sub.add_parser("prepare", help="only write a hardened sandbox and print it")
    prepare.add_argument("--sandbox", required=True, type=Path)
    prepare.add_argument("--frozen", action="store_true")
    readback = sub.add_parser("readback", help="judge a prepared sandbox after a run (T-P2-H2)")
    readback.add_argument("--sandbox", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.command == "prepare":
        sandbox = prepare_sandbox(args.sandbox, frozen=args.frozen)
        described = {key: str(value) if isinstance(value, Path) else value
                     for key, value in sandbox.items() if key != "env"}
        # T-P2-H2 (33 smoke): the P1 stage re-reads this sandbox after its run.
        (Path(args.sandbox) / SANDBOX_DESCRIPTION).write_text(json.dumps(described), encoding="utf-8")
        # The allowlisted child environment (ENV_ALLOWLIST plus the sandbox
        # variables), so the P1 stage can launch headless_shell with it.
        print(json.dumps({**described, "env": sandbox["env"]}))
        return 0
    if args.command == "readback":
        described = json.loads((Path(args.sandbox) / SANDBOX_DESCRIPTION).read_text(encoding="utf-8"))
        result = sandbox_readback(described)
        problems = readback_problems(result)
        print(json.dumps(dict(readback=result, problems=problems), ensure_ascii=False))
        return 1 if problems else 0
    args.output.mkdir(parents=True, exist_ok=True)
    sandbox = prepare_sandbox(args.sandbox, frozen=bool(args.exe))
    (args.output / "ports.json").write_text(json.dumps(sandbox["ports"]), encoding="utf-8")
    summary = run(sandbox, exe=args.exe, output=args.output)
    summary["failures"] = verdict(summary)
    text = json.dumps(summary, ensure_ascii=False, indent=1, sort_keys=True, default=str)
    (args.output / "summary.json").write_text(text, encoding="utf-8")
    print(json.dumps(dict(failures=summary["failures"], seconds=summary["seconds"],
                          form=summary["form"], ports=summary["ports"]), ensure_ascii=False))
    return 1 if summary["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
