import copy
import sys

import pytest

from scripts.tier3_audit_environment import external_network_unchanged, network_snapshot_complete
from scripts.tier3_audit_environment import windows_process_snapshot_csv


def snapshot():
    return dict(state="external_tun_present", errors=[],
                default_route_summary=["physical-default"], fake_ip_route_summary=["tun-route"],
                dns_summary=["local-dns"], system_proxy_enabled=False, system_proxy_digest="hash")


@pytest.mark.parametrize("change", [
    {"state": "indeterminate"}, {"errors": ["route:timeout"]},
    {"errors": None}, {"default_route_summary": []}, {"default_route_summary": None},
    {"system_proxy_enabled": None}, {"system_proxy_enabled": "false"},
])
def test_two_identically_incomplete_snapshots_cannot_prove_no_change(change):
    before = snapshot()
    before.update(change)
    assert not network_snapshot_complete(before)
    assert not external_network_unchanged(before, copy.deepcopy(before))


def test_complete_equal_snapshots_and_actual_route_change():
    before = snapshot()
    assert external_network_unchanged(before, copy.deepcopy(before))
    after = copy.deepcopy(before)
    after["default_route_summary"] = ["different-default"]
    assert not external_network_unchanged(before, after)


def test_owner_tuple_snapshot_matches_serialized_lists():
    before = snapshot()
    after = {k: tuple(v) if isinstance(v, list) else v for k, v in before.items()}
    assert network_snapshot_complete(after)
    assert external_network_unchanged(before, after)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Toolhelp snapshot")
def test_native_process_snapshot_contains_this_process_without_spawning(monkeypatch):
    import csv
    import io
    import os
    import subprocess
    from unittest.mock import Mock
    monkeypatch.setattr(subprocess, "Popen", Mock(side_effect=AssertionError("no subprocess")))
    rows = list(csv.reader(io.StringIO(windows_process_snapshot_csv())))
    assert any(int(row[1]) == os.getpid() and row[0].lower().endswith(".exe") for row in rows)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Toolhelp snapshot")
@pytest.mark.parametrize("failure", ["create", "first", "next", None])
def test_process_snapshot_failures_are_not_reported_as_an_empty_success(monkeypatch, failure):
    import ctypes
    from types import SimpleNamespace
    from unittest.mock import Mock

    def first(_handle, entry):
        if failure == "first":
            ctypes.set_last_error(5)
            return False
        assert entry._obj.dwSize == ctypes.sizeof(entry._obj)
        entry._obj.szExeFile = "owned.exe"
        entry._obj.th32ProcessID = 42
        return True

    def next_entry(*_args):
        ctypes.set_last_error(5 if failure == "next" else 18)
        return False

    ctypes.set_last_error(5)
    api = SimpleNamespace(
        CreateToolhelp32Snapshot=Mock(return_value=ctypes.c_void_p(-1).value if failure == "create" else 99),
        Process32FirstW=Mock(side_effect=first), Process32NextW=Mock(side_effect=next_entry),
        CloseHandle=Mock(return_value=True))
    monkeypatch.setattr(ctypes, "WinDLL", Mock(return_value=api))
    if failure:
        with pytest.raises(OSError):
            windows_process_snapshot_csv()
    else:
        assert windows_process_snapshot_csv().strip() == "owned.exe,42"
    api.CreateToolhelp32Snapshot.assert_called_once_with(2, 0)
    if failure == "create":
        api.CloseHandle.assert_not_called()
    else:
        api.CloseHandle.assert_called_once_with(99)
