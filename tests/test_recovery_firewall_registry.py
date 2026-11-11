"""Startup firewall inventory from the registry (11-10).

A clean install showed "firewall: not confirmed" because the PowerShell
inventory (spawn + Get-NetFirewallRule over every rule) overran the 2 s
startup budget. With the shipped runner the inventory now reads the
persistent rule store in-process; injected runners keep the PowerShell path.
"""

import json
import logging
import subprocess
import sys
import types
from types import SimpleNamespace

import pytest

import killswitch
from runtime_network_recovery import recover_startup_firewall

GUID_A = "{12345678-1234-1234-1234-123456789abc}"
BLOCK = killswitch.KILL_SWITCH_RULE_NAMES["block"]


def fake_winreg(values, *, open_error=None):
    module = types.ModuleType("winreg")
    module.HKEY_LOCAL_MACHINE = object()
    module.KEY_READ = 1
    module.KEY_WOW64_64KEY = 2
    opened = []

    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def open_key(root, path, reserved, access):
        if open_error is not None:
            raise open_error
        opened.append((root, path, access))
        return Key()

    module.OpenKey = open_key
    module.QueryInfoKey = lambda key: (0, len(values), 0)
    module.EnumValue = lambda key, index: (values[index][0], values[index][1], 1)
    module.opened = opened
    return module


def rule_data(display, extra=""):
    return f"v2.33|Action=Block|Active=TRUE|Dir=Out|{extra}Name={display}|Desc=x|"


def test_display_name_is_the_name_field():
    assert killswitch._registry_rule_display_name(rule_data(BLOCK)) == BLOCK
    assert killswitch._registry_rule_display_name("v2.33|Action=Allow|") is None
    assert killswitch._registry_rule_display_name(b"Name=x") is None
    # App/Svc paths come before Name and cannot hold "|"; Desc after it is ignored.
    data = "v2.33|App=C:\\x.exe|Name=Other|Desc=Name=" + BLOCK + "|"
    assert killswitch._registry_rule_display_name(data) == "Other"


def test_registry_rows_match_the_powershell_prefix(monkeypatch):
    values = [
        (GUID_A, rule_data(BLOCK)),
        ("other", rule_data("proxymanager-killswitch-Foreign")),  # -like is case-insensitive
        ("NETDIS", rule_data("@FirewallAPI.dll,-32765")),
        ("QQ", rule_data("QQ")),
        ("binary", b"\x00\x01"),
    ]
    module = fake_winreg(values)
    monkeypatch.setitem(sys.modules, "winreg", module)
    rows = killswitch.registry_firewall_rules(timeout=1.0)
    assert rows == [{"name": GUID_A, "display_name": BLOCK},
                    {"name": "other", "display_name": "proxymanager-killswitch-Foreign"}]
    assert module.opened[0][1] == killswitch._FIREWALL_RULES_KEY
    assert module.opened[0][2] == module.KEY_READ | module.KEY_WOW64_64KEY


def test_registry_read_honours_an_exhausted_budget(monkeypatch):
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg([]))
    with pytest.raises(TimeoutError):
        killswitch.registry_firewall_rules(timeout=0.0)


def test_shipped_runner_reads_the_registry_without_spawning(monkeypatch):
    monkeypatch.setattr(killswitch.platform, "system", lambda: "Windows")
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg([(GUID_A, rule_data(BLOCK))]))
    monkeypatch.setattr(killswitch.subprocess, "run",
                        lambda *a, **k: pytest.fail("PowerShell spawned"))
    rows = killswitch.recovery_firewall_rules(killswitch._SHIPPED_RECOVERY_RUNNER, timeout=1.0)
    assert rows == [{"name": GUID_A, "display_name": BLOCK}]


def test_unreadable_store_falls_back_to_powershell(monkeypatch):
    monkeypatch.setattr(killswitch.platform, "system", lambda: "Windows")
    monkeypatch.setitem(sys.modules, "winreg",
                        fake_winreg([], open_error=PermissionError("denied")))
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(
            command, 0, json.dumps([{"name": GUID_A, "display_name": BLOCK}]), "")

    monkeypatch.setattr(killswitch.subprocess, "run", run)
    rows = killswitch.recovery_firewall_rules(killswitch._SHIPPED_RECOVERY_RUNNER, timeout=1.0)
    assert rows == [{"name": GUID_A, "display_name": BLOCK}]
    assert calls and calls[0][0] == "powershell.exe"


def test_injected_runner_never_reads_the_registry(monkeypatch):
    monkeypatch.setattr(killswitch, "registry_firewall_rules",
                        lambda **_: pytest.fail("registry read with an injected runner"))
    runner = lambda command, **_: subprocess.CompletedProcess(command, 0, "[]", "")
    assert killswitch.recovery_firewall_rules(runner, timeout=1.0) == []


def test_clean_install_startup_is_not_needed_without_powershell(monkeypatch):
    monkeypatch.setattr(killswitch.platform, "system", lambda: "Windows")
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg([("QQ", rule_data("QQ"))]))
    monkeypatch.setattr(killswitch.subprocess, "run",
                        lambda *a, **k: pytest.fail("PowerShell spawned"))
    result = recover_startup_firewall(
        may_take_over_host_network=True, admin_available=False,
        command_runner=killswitch._SHIPPED_RECOVERY_RUNNER)
    assert result == {"state": "not_needed", "error_code": None, "remaining_count": 0}
    result.gate.require_idle()


def test_startup_recovery_reading_is_logged(monkeypatch, tmp_path, caplog):
    import bridge_main
    import runtime_network_recovery as recovery

    def fake(store, *, scope, deadline_seconds, diagnostics):
        diagnostics["firewall"] = {"inspection_error_type": "RuntimeError"}
        return {"proxy": {"state": "not_needed", "error_code": None},
                "firewall": {"state": "unknown", "error_code": "inspection_failed"}}

    monkeypatch.setattr(recovery, "recover_network_after_unclean_exit", fake)
    logger = logging.getLogger("tesseract.bridge")
    monkeypatch.setattr(logger, "propagate", True)
    with caplog.at_level(logging.INFO, logger="tesseract.bridge"):
        report = bridge_main.recover_network(SimpleNamespace(data_dir=tmp_path))
    assert report["firewall"]["state"] == "unknown"
    line = [r.getMessage() for r in caplog.records if "Startup network recovery" in r.getMessage()]
    assert line and ("proxy=not_needed proxy_error=- firewall=unknown "
                     "firewall_error=inspection_failed firewall_detail=RuntimeError") in line[0]
