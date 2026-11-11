import io
import json
import subprocess
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bridge_protocol import validate_hello
from killswitch import (
    _RECOVERY_DELETE_SCRIPT, recovery_command_runner,
    recovery_delete_owned_firewall_rule,
)
from runtime_network_recovery import (
    RecoveryStillRunning, _clear_proxy_entry, recover_startup_system_proxy,
    recover_startup_firewall, recover_network_after_unclean_exit,
)
from system_proxy import ProxySettings, _proxy_transaction


TARGET = "127.0.0.1:7890"
ORIGINAL = ProxySettings(True, "old:8080", "<local>")
OWNED = ProxySettings(True, TARGET, "localhost")
GUID_A = "{12345678-1234-1234-1234-123456789abc}"
GUID_B = "{abcdefab-cdef-cdef-cdef-abcdefabcdef}"


class FakeRegistry:
    def __init__(self, current=OWNED):
        self.current = current
        self.read_error = None
        self.write_error = None
        self.verify_override = None
        self.reads = 0
        self.writes = []

    def read(self):
        self.reads += 1
        if self.read_error:
            raise self.read_error
        if self.reads > 1 and self.verify_override is not None:
            return self.verify_override
        return self.current

    def write(self, settings):
        self.writes.append(settings)
        if self.write_error:
            raise self.write_error
        self.current = settings


class FakeJournalPath:
    def __init__(self):
        self.data = None
        self.read_error = None
        self.read_hook = None

    def __str__(self):
        return "fake:/runtime-state.json"

    @property
    def parent(self):
        return "fake:/"

    @property
    def name(self):
        return "runtime-state.json"

    def read_bytes(self):
        if self.read_hook is not None:
            self.read_hook()
        if self.read_error:
            raise self.read_error
        if self.data is None:
            raise FileNotFoundError(str(self))
        return self.data

    def write_bytes(self, data):
        self.data = data

    def read_text(self):
        return self.read_bytes().decode("utf-8")

    def write_text(self, value, encoding="utf-8"):
        self.write_bytes(value.encode(encoding))

    def exists(self):
        return self.data is not None

    def stat(self):
        if self.data is None:
            raise FileNotFoundError(str(self))
        return SimpleNamespace(st_size=len(self.data), st_mtime_ns=1)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        # RT-1 tests model a fresh process, independent of prior runtime tests.
        for name, value in (("_PROXY_RUNTIME_CONSTRUCTED", False),
                            ("_PROXY_WRITES_REVOKED", threading.Event())):
            patcher = patch("system_proxy." + name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.path = FakeJournalPath()
        self.store = SimpleNamespace(path=self.path)
        self.backend = FakeRegistry()
        self.temp_handle = None
        self.replace_hook = None
        for target, replacement in (
            ("runtime_network_recovery.tempfile.mkstemp", lambda **_kwargs: (1, "fake.tmp")),
            ("runtime_network_recovery.os.fdopen", self.fake_fdopen),
            ("runtime_network_recovery.os.fsync", lambda _fd: None),
            ("runtime_network_recovery.os.replace", self.fake_replace),
            ("runtime_network_recovery.os.unlink", lambda _name: None),
        ):
            patcher = patch(target, side_effect=replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def fake_fdopen(self, _fd, _mode):
        class FakeHandle(io.BytesIO):
            def fileno(self):
                return 1

            def close(self):
                pass

        self.temp_handle = FakeHandle()
        return self.temp_handle

    def fake_replace(self, _source, _target):
        if self.replace_hook is not None:
            self.replace_hook()
        self.path.write_bytes(self.temp_handle.getvalue())

    def test_real_clear_logic_with_fake_filesystem(self):
        self.journal()
        original = self.path.read_bytes()
        payload = json.loads(original)
        with _proxy_transaction():
            self.assertTrue(_clear_proxy_entry(self.path, original, payload, lambda: False, lambda: True))
        self.assertEqual(json.loads(self.path.read_text()), {"other": {"keep": True}})

    def journal(self, **extra):
        payload = {"system_proxy": {"active": True, "target": TARGET,
                                    "original": {"enabled": True, "server": "old:8080", "override": "<local>"}},
                   "other": {"keep": True}}
        payload.update(extra)
        self.path.write_text(json.dumps(payload), encoding="utf-8")

    def assert_protocol_result(self, result):
        hello = {"t": "hello", "epoch": "e", "produced_at_ms": 1, "sent_at_ms": 1,
                 "proto": 1, "app_version": "test",
                 "runtime_pid": 1, "lang": "en_US", "caps": {"commands": [], "topics": [], "features": []},
                 "scope": {"kind": "default", "id": "default"},
                 "network_recovery": {"proxy": result, "firewall": {"state": "not_needed", "error_code": None, "remaining_count": 0}}}
        self.assertEqual(validate_hello(hello), [])

    def recover(self, **kwargs):
        result = recover_startup_system_proxy(
            self.store, may_take_over_host_network=True, backend=self.backend, **kwargs)
        self.assert_protocol_result(result)
        return result

    def test_old_port_is_used_and_only_proxy_record_is_cleared(self):
        self.journal()
        result = self.recover()
        self.assertEqual(result, {"state": "restored", "error_code": None, "remaining_count": 0})
        self.assertEqual(self.backend.current, ORIGINAL)
        self.assertEqual(json.loads(self.path.read_text()), {"other": {"keep": True}})

    def test_taken_over_does_not_write_or_clear(self):
        self.journal()
        self.backend.current = ProxySettings(True, "other:1080", "")
        self.assertEqual(self.recover()["state"], "not_owned")
        self.assertEqual(self.recover()["state"], "not_owned")
        self.assertEqual(self.backend.writes, [])
        self.assertIn("system_proxy", json.loads(self.path.read_text()))

    def test_read_failure_keeps_journal(self):
        self.journal()
        self.backend.read_error = OSError("denied")
        self.assertEqual(self.recover(), {"state": "unknown", "error_code": "registry_read_failed", "remaining_count": None})
        self.assertTrue(self.path.exists())

    def test_write_failure_keeps_journal(self):
        self.journal()
        self.backend.write_error = OSError("denied")
        self.assertEqual(self.recover()["error_code"], "registry_write_failed")
        self.assertIn("system_proxy", json.loads(self.path.read_text()))

    def test_verify_mismatch_keeps_journal(self):
        self.journal()
        self.backend.verify_override = OWNED
        self.assertEqual(self.recover()["error_code"], "registry_verify_mismatch")
        self.assertIn("system_proxy", json.loads(self.path.read_text()))

    def test_missing_corrupt_and_unreadable_journal(self):
        self.assertEqual(self.recover()["state"], "not_needed")
        self.path.write_bytes(b"{bad")
        diagnostics = {}
        self.assertEqual(self.recover(diagnostics=diagnostics)["error_code"], "journal_corrupt")
        self.assertEqual(diagnostics["journal"]["size"], 4)
        self.path.read_error = PermissionError(13, "denied")
        try:
            self.assertEqual(self.recover()["error_code"], "journal_unreadable")
        finally:
            self.path.read_error = None
        self.assertEqual(self.path.read_bytes(), b"{bad")

    def test_success_then_second_run_does_not_repeat_write(self):
        self.journal()
        self.assertEqual(self.recover()["state"], "restored")
        self.assertEqual(self.recover()["state"], "not_needed")
        self.assertEqual(len(self.backend.writes), 1)

    def test_timeout_is_bounded_and_keeps_journal(self):
        self.journal()
        def slow_read():
            time.sleep(0.08)
            return OWNED
        self.backend.read = slow_read
        start = time.monotonic()
        result = self.recover(deadline_seconds=0.01)
        self.assertLess(time.monotonic() - start, 0.06)
        self.assertEqual(result, {"state": "unknown", "error_code": "deadline_exceeded", "remaining_count": None})
        time.sleep(0.09)
        self.assertEqual(self.backend.writes, [])
        self.assertIn("system_proxy", json.loads(self.path.read_text()))

    def test_timeout_during_write_does_not_clear_journal(self):
        self.journal()
        entered = threading.Event()
        release = threading.Event()
        def slow_write(settings):
            entered.set()
            self.assertTrue(release.wait(1))
            self.backend.current = settings
        self.backend.write = slow_write
        result = self.recover(deadline_seconds=0.02)
        self.assertTrue(entered.is_set())
        self.assertEqual(result["error_code"], "deadline_exceeded")
        with self.assertRaises(RecoveryStillRunning):
            result.gate.require_idle()
        # A cooperative startup cannot take over while the old write is in flight.
        release.set()
        self.assertTrue(result.gate.done.wait(1))
        result.gate.require_idle()
        self.backend.current = ProxySettings(True, "new:1080", "")
        self.assertEqual(self.backend.current.server, "new:1080")
        self.assertIn("system_proxy", json.loads(self.path.read_text()))

    def test_combined_gate_stays_closed_while_proxy_worker_runs(self):
        self.journal()
        entered = threading.Event()
        release = threading.Event()

        def slow_write(settings):
            entered.set()
            self.assertTrue(release.wait(1))
            self.backend.current = settings

        self.backend.write = slow_write
        runner = FakeFirewallRunner([(GUID_A, "ProxyManager-KillSwitch-Block")])
        result = recover_network_after_unclean_exit(
            self.store, scope=SimpleNamespace(may_take_over_host_network=True),
            deadline_seconds=0.03, backend=self.backend,
            command_runner=runner, admin_available=True,
        )
        try:
            self.assertTrue(entered.is_set())
            self.assertEqual(result["proxy"]["error_code"], "deadline_exceeded")
            with self.assertRaises(RecoveryStillRunning):
                result.gate.require_idle()
        finally:
            release.set()
        self.assertTrue(result.gate.gates[0].done.wait(1))
        result.gate.require_idle()

    def test_timeout_during_final_read_does_not_replace(self):
        self.journal()
        entered = threading.Event()
        release = threading.Event()
        reads = 0
        def block_final_read():
            nonlocal reads
            reads += 1
            if reads == 3:
                entered.set()
                self.assertTrue(release.wait(1))
        self.path.read_hook = block_final_read
        result = self.recover(deadline_seconds=0.02)
        self.assertTrue(entered.is_set())
        self.assertEqual(result["error_code"], "deadline_exceeded")
        release.set()
        self.assertTrue(result.gate.done.wait(1))
        self.assertIn("system_proxy", json.loads(self.path.read_text()))

    def test_blocked_replace_returns_commit_pending_until_finished(self):
        self.journal()
        entered = threading.Event()
        release = threading.Event()
        outcome = []
        def block_replace():
            entered.set()
            self.assertTrue(release.wait(1))
        self.replace_hook = block_replace
        caller = threading.Thread(target=lambda: outcome.append(self.recover(deadline_seconds=0.03)))
        caller.start()
        try:
            self.assertTrue(entered.wait(1))
            caller.join(0.2)
            self.assertFalse(caller.is_alive(), "recovery exceeded its deadline while replacing")
            result = outcome[0]
            self.assertEqual(result, {"state": "unknown", "error_code": "commit_pending", "remaining_count": None})
            with self.assertRaises(RecoveryStillRunning):
                result.gate.require_idle()
            self.assertIn("system_proxy", json.loads(self.path.read_text()))
        finally:
            release.set()
            caller.join(1)
        self.assertFalse(caller.is_alive())
        self.assertTrue(result.gate.done.wait(1))
        result.gate.require_idle()
        self.assertEqual(self.backend.current, ORIGINAL)
        self.assertEqual(json.loads(self.path.read_text()), {"other": {"keep": True}})

    def test_blocked_cleanup_returns_commit_pending_until_finished(self):
        self.journal()
        entered = threading.Event()
        release = threading.Event()
        outcome = []
        def block_cleanup(_name):
            entered.set()
            self.assertTrue(release.wait(1))
        with patch("runtime_network_recovery.os.unlink", side_effect=block_cleanup):
            caller = threading.Thread(target=lambda: outcome.append(self.recover(deadline_seconds=0.03)))
            caller.start()
            try:
                self.assertTrue(entered.wait(1))
                caller.join(0.2)
                self.assertFalse(caller.is_alive(), "recovery exceeded its deadline during cleanup")
                result = outcome[0]
                self.assertEqual(result, {"state": "unknown", "error_code": "commit_pending", "remaining_count": None})
                with self.assertRaises(RecoveryStillRunning):
                    result.gate.require_idle()
                self.assertEqual(json.loads(self.path.read_text()), {"other": {"keep": True}})
            finally:
                release.set()
                caller.join(1)
        self.assertFalse(caller.is_alive())
        self.assertTrue(result.gate.done.wait(1))
        result.gate.require_idle()
        self.assertEqual(self.backend.current, ORIGINAL)
        self.assertEqual(json.loads(self.path.read_text()), {"other": {"keep": True}})

    def test_temp_write_and_replace_failures_keep_journal(self):
        self.journal()
        with patch("runtime_network_recovery.os.fsync", side_effect=OSError("sync failed")):
            self.assertEqual(self.recover()["error_code"], "journal_clear_failed")
        self.assertIn("system_proxy", json.loads(self.path.read_text()))
        self.backend.current = OWNED
        with patch("runtime_network_recovery.os.replace", side_effect=OSError("replace failed")):
            self.assertEqual(self.recover()["error_code"], "journal_clear_failed")
        self.assertIn("system_proxy", json.loads(self.path.read_text()))

    def test_temp_cleanup_failure_keeps_journal_and_is_reported(self):
        self.journal()
        reads = 0
        def change_before_replace():
            nonlocal reads
            reads += 1
            if reads == 3:
                self.journal(extra=1)
        self.path.read_hook = change_before_replace
        with patch("runtime_network_recovery.os.unlink", side_effect=OSError("cleanup failed")):
            self.assertEqual(self.recover()["error_code"], "journal_clear_failed")
        self.assertIn("system_proxy", json.loads(self.path.read_text()))

    def test_cleanup_error_after_replace_does_not_hide_success(self):
        self.journal()
        with patch("runtime_network_recovery.os.unlink", side_effect=OSError("cleanup failed")):
            self.assertEqual(self.recover()["state"], "restored")
        self.assertNotIn("system_proxy", json.loads(self.path.read_text()))

    def test_isolated_scope_does_not_touch_journal_or_registry(self):
        self.path.read_error = AssertionError("journal touched")
        try:
            result = recover_startup_system_proxy(self.store, may_take_over_host_network=False, backend=self.backend)
        finally:
            self.path.read_error = None
        self.assertEqual(result, {"state": "not_needed", "error_code": "scope_isolated", "remaining_count": 0})
        self.assert_protocol_result(result)
        self.assertEqual(self.backend.reads, 0)

    def test_verify_read_failure_keeps_journal(self):
        self.journal()
        def read():
            self.backend.reads += 1
            if self.backend.reads == 2:
                raise OSError("verify denied")
            return OWNED
        self.backend.read = read
        self.assertEqual(self.recover(), {"state": "unknown", "error_code": "registry_verify_failed", "remaining_count": None})
        self.assertIn("system_proxy", json.loads(self.path.read_text()))

    def test_clear_failure_is_reported_and_keeps_journal(self):
        self.journal()
        with patch("runtime_network_recovery.tempfile.mkstemp", side_effect=OSError("disk denied")):
            self.assertEqual(self.recover()["error_code"], "journal_clear_failed")
        self.assertIn("system_proxy", json.loads(self.path.read_text()))

    def test_journal_change_prevents_clear(self):
        self.journal()
        def change_on_verify():
            self.backend.reads += 1
            if self.backend.reads == 2:
                self.journal(new_field=1)
                return ORIGINAL
            return OWNED
        self.backend.read = change_on_verify
        self.assertEqual(self.recover()["error_code"], "journal_changed")
        self.assertIn("system_proxy", json.loads(self.path.read_text()))


class FakeFirewallRunner:
    def __init__(self, rules=()):
        self.rules = {name: display for name, display in rules}
        self.calls = []
        self.inspect_error = False
        self.verify_error = False
        self.delete_fail = set()
        self.timeout_on_delete = False
        self.clock = None
        self.elapsed_per_call = 0.0
        self.before_delete = None

    def __call__(self, command, *, timeout, env=None):
        self.calls.append((command, timeout))
        if self.clock is not None:
            self.clock[0] += self.elapsed_per_call
        script = command[-1]
        if "Remove-NetFirewallRule" not in script:
            if self.inspect_error or (self.verify_error and len(self.calls) > 1):
                return subprocess.CompletedProcess(command, 1, "", "error")
            rows = [{"name": name, "display_name": display}
                    for name, display in self.rules.items()
                    if display.startswith("ProxyManager-KillSwitch-")]
            return subprocess.CompletedProcess(command, 0, json.dumps(rows), "")
        if self.timeout_on_delete:
            raise subprocess.TimeoutExpired(command, timeout)
        self.assert_safe_delete_script(script)
        assert env is not None
        if self.before_delete is not None:
            self.before_delete(self.rules)
        identity = json.loads(env["TESSERACT_RECOVERY_RULE_IDENTITY"])
        name, display = identity["rule_id"], identity["display_name"]
        if self.rules.get(name) != display or name in self.delete_fail:
            return subprocess.CompletedProcess(command, 1, "", "error")
        del self.rules[name]
        return subprocess.CompletedProcess(command, 0, "", "")

    @staticmethod
    def assert_safe_delete_script(script):
        assert script == _RECOVERY_DELETE_SCRIPT
        assert "-Name '" not in script
        assert "[StringComparison]::Ordinal" in script
        assert "-ceq" not in script
        assert "-InputObject $selected[0]" in script


class FirewallRecoveryTests(unittest.TestCase):
    BLOCK = "ProxyManager-KillSwitch-Block"
    DNS = "ProxyManager-KillSwitch-Allow-DNS"

    def recover(self, runner, **kwargs):
        return recover_startup_firewall(
            may_take_over_host_network=True, command_runner=runner,
            admin_available=True, **kwargs)

    def test_disabled_switch_still_clears_old_rules(self):
        runner = FakeFirewallRunner([(GUID_A, self.BLOCK)])
        diagnostics = {}
        self.assertEqual(self.recover(runner, diagnostics=diagnostics)["state"], "restored")
        self.assertEqual(diagnostics["deletions"], [{"name": GUID_A, "ok": True}])
        self.assertEqual(runner.rules, {})

    def test_no_admin_checks_without_deleting(self):
        runner = FakeFirewallRunner([(GUID_A, self.BLOCK)])
        result = recover_startup_firewall(
            may_take_over_host_network=True, command_runner=runner,
            admin_available=False)
        self.assertEqual(result, {"state": "permission_required", "error_code": "admin_required",
                                  "remaining_count": 1})
        self.assertEqual(len(runner.calls), 1)

    def test_no_admin_inspection_failure_is_unknown(self):
        runner = FakeFirewallRunner()
        runner.inspect_error = True
        result = recover_startup_firewall(
            may_take_over_host_network=True, command_runner=runner,
            admin_available=False)
        self.assertEqual(result["state"], "unknown")
        self.assertIsNone(result["remaining_count"])

    def test_partial_delete_and_verification_failure(self):
        runner = FakeFirewallRunner([(GUID_A, self.BLOCK), (GUID_B, self.DNS)])
        runner.delete_fail.add(GUID_B)
        diagnostics = {}
        result = self.recover(runner, diagnostics=diagnostics)
        self.assertEqual(result, {"state": "failed", "error_code": "rules_remaining",
                                  "remaining_count": 1})
        self.assertEqual([item["ok"] for item in diagnostics["deletions"]], [True, False])
        runner = FakeFirewallRunner([(GUID_A, self.BLOCK)])
        runner.verify_error = True
        self.assertEqual(self.recover(runner)["state"], "unknown")

    def test_timeout_has_no_later_deletes(self):
        runner = FakeFirewallRunner([(GUID_A, self.BLOCK), (GUID_B, self.DNS)])
        runner.timeout_on_delete = True
        result = self.recover(runner)
        self.assertEqual(result["error_code"], "deadline_exceeded")
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(len(runner.rules), 2)
        result.gate.require_idle()

    def test_other_application_rule_is_reported_and_preserved(self):
        runner = FakeFirewallRunner([(GUID_A, self.BLOCK),
                                     ("other", "ProxyManager-KillSwitch-Foreign")])
        diagnostics = {}
        self.assertEqual(self.recover(runner, diagnostics=diagnostics)["state"], "failed")
        self.assertEqual(runner.rules, {"other": "ProxyManager-KillSwitch-Foreign"})
        self.assertEqual(diagnostics["unconfirmed_rules"][0]["name"], "other")

    def test_special_ids_are_rejected_before_delete(self):
        invalid_ids = ("x’; Write-Output SENTINEL } catch { }; #", "’", "‘", "＇", "❜",
                       ";", "line\nbreak", "`", "$()", "*", "?", "[ab]", "a\u00adb")
        for rule_id in invalid_ids:
            with self.subTest(rule_id=rule_id):
                runner = FakeFirewallRunner([(rule_id, self.BLOCK),
                                             ("foreign-id", "ProxyManager-KillSwitch-Foreign")])
                diagnostics = {}
                result = self.recover(runner, diagnostics=diagnostics)
                self.assertEqual(result["state"], "not_owned")
                self.assertEqual(result["remaining_count"], 2)
                self.assertEqual(len(runner.calls), 1)
                self.assertEqual(runner.rules[rule_id], self.BLOCK)
                self.assertIn(rule_id, [row["name"] for row in diagnostics["unconfirmed_rules"]])

    def test_accepted_identity_is_data_only_and_script_is_fixed(self):
        runner = FakeFirewallRunner([(GUID_A, self.BLOCK)])
        self.assertEqual(self.recover(runner)["state"], "restored")
        command, _ = runner.calls[1]
        self.assertEqual(command[-1], _RECOVERY_DELETE_SCRIPT)
        self.assertNotIn(GUID_A, str(command))
        self.assertNotIn(self.BLOCK, str(command))

    def test_delete_helper_rejects_invalid_id_without_runner_call(self):
        invalid_ids = ("x’; Write-Output SENTINEL } catch { }; #", "’", "‘", "＇", "❜",
                       ";", "line\nbreak", "`", "$()", "*", "?", "[ab]", "a\u00adb",
                       GUID_A[1:-1], GUID_A + "x", GUID_A.replace("a", "g"),
                       "{" + "١" + GUID_A[2:])
        def forbidden(*_args, **_kwargs):
            raise AssertionError("runner called")
        for rule_id in invalid_ids:
            with self.subTest(rule_id=rule_id):
                with self.assertRaisesRegex(ValueError, "firewall_rule_identity_invalid"):
                    recovery_delete_owned_firewall_rule(
                        rule_id, self.BLOCK, forbidden, timeout=1.0)
        with self.assertRaisesRegex(ValueError, "firewall_rule_identity_invalid"):
            recovery_delete_owned_firewall_rule(
                GUID_A, self.BLOCK.replace("Block", "Bl\u00adock"), forbidden, timeout=1.0)
        with self.assertRaisesRegex(ValueError, "firewall_rule_identity_invalid"):
            recovery_delete_owned_firewall_rule(
                GUID_A, self.BLOCK.lower(), forbidden, timeout=1.0)

    def test_invalid_id_remains_after_valid_rule_deletion(self):
        runner = FakeFirewallRunner([(GUID_A, self.BLOCK), ("a\u00adb", self.DNS)])
        result = self.recover(runner)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["remaining_count"], 1)
        self.assertEqual(runner.rules, {"a\u00adb": self.DNS})
        self.assertEqual(len(runner.calls), 3)
        self.assertTrue(all("a\u00adb" not in str(command) for command, _ in runner.calls))

    def test_unicode_display_name_difference_is_residual(self):
        changed = self.BLOCK.replace("Block", "Bl\u00adock")
        runner = FakeFirewallRunner([(GUID_A, changed)])
        diagnostics = {}
        result = self.recover(runner, diagnostics=diagnostics)
        self.assertEqual(result["state"], "not_owned")
        self.assertEqual(result["remaining_count"], 1)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(diagnostics["unconfirmed_rules"][0]["display_name"], changed)

    def test_changed_display_name_is_not_deleted(self):
        runner = FakeFirewallRunner([(GUID_A, self.BLOCK)])
        runner.before_delete = lambda rules: rules.update({GUID_A: "ProxyManager-KillSwitch-Foreign"})
        diagnostics = {}
        result = self.recover(runner, diagnostics=diagnostics)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(diagnostics["deletions"][0]["ok"], False)
        self.assertEqual(runner.rules[GUID_A], "ProxyManager-KillSwitch-Foreign")

    def test_shared_deadline_budget_decreases_and_stops_calls(self):
        clock = [100.0]
        runner = FakeFirewallRunner([(GUID_A, self.BLOCK), (GUID_B, self.DNS)])
        runner.clock = clock
        runner.elapsed_per_call = 0.2
        with patch("runtime_network_recovery.time.monotonic", side_effect=lambda: clock[0]):
            result = self.recover(runner, deadline_seconds=1.0)
        self.assertEqual(result["state"], "restored")
        self.assertEqual([round(timeout, 5) for _, timeout in runner.calls], [1.0, 0.8, 0.6, 0.4])
        runner = FakeFirewallRunner([(GUID_A, self.BLOCK), (GUID_B, self.DNS)])
        runner.clock = clock = [100.0]
        runner.elapsed_per_call = 0.4
        with patch("runtime_network_recovery.time.monotonic", side_effect=lambda: clock[0]):
            result = self.recover(runner, deadline_seconds=1.0)
        self.assertEqual(result["error_code"], "deadline_exceeded")
        self.assertEqual(len(runner.calls), 3)
        self.assertEqual(runner.rules, {})

    def test_isolated_scope_never_calls_runner(self):
        def forbidden(*_args, **_kwargs):
            raise AssertionError("runner called")
        result = recover_startup_firewall(
            may_take_over_host_network=False, command_runner=forbidden)
        self.assertEqual(result, {"state": "not_needed", "error_code": "scope_isolated",
                                  "remaining_count": 0})

    def test_combined_result_validates_and_gates_both_domains(self):
        # The combined recovery also runs the proxy RT-1 phase: model a fresh
        # process, as RecoveryTests.setUp does, so earlier runtime tests in the
        # same pytest process cannot leave "runtime constructed" behind.
        for name, value in (("_PROXY_RUNTIME_CONSTRUCTED", False),
                            ("_PROXY_RT1_PHASE", None),
                            ("_PROXY_TRANSACTIONS_IN_FLIGHT", 0),
                            ("_PROXY_WRITES_REVOKED", threading.Event())):
            patcher = patch("system_proxy." + name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        runner = FakeFirewallRunner([(GUID_A, self.BLOCK)])
        store = SimpleNamespace(path=FakeJournalPath())
        result = recover_network_after_unclean_exit(
            store, scope=SimpleNamespace(may_take_over_host_network=True),
            command_runner=runner, admin_available=True)
        hello = {"t": "hello", "epoch": "e", "produced_at_ms": 1, "sent_at_ms": 1,
                 "proto": 1, "app_version": "test", "runtime_pid": 1, "lang": "en_US",
                 "caps": {"commands": [], "topics": [], "features": []},
                 "scope": {"kind": "default", "id": "default"},
                 "network_recovery": result}
        self.assertEqual(validate_hello(hello), [])
        result.gate.require_idle()
        self.assertEqual(result["firewall"]["state"], "restored")

    def test_combined_isolated_scope_touches_neither_domain(self):
        def forbidden(*_args, **_kwargs):
            raise AssertionError("command called")
        path = FakeJournalPath()
        path.read_error = AssertionError("journal touched")
        result = recover_network_after_unclean_exit(
            SimpleNamespace(path=path),
            scope=SimpleNamespace(may_take_over_host_network=False),
            command_runner=forbidden)
        self.assertEqual(result["proxy"]["error_code"], "scope_isolated")
        self.assertEqual(result["firewall"]["error_code"], "scope_isolated")
        result.gate.require_idle()

    def test_default_runner_kills_and_waits_before_timeout_returns(self):
        events = []

        class FakeProcess:
            def __init__(self, *_args, **_kwargs):
                self.returncode = None

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                events.append("wait")
                self.returncode = -9

            def communicate(self, _input=None, timeout=None):
                events.append("communicate")
                if timeout is not None:
                    raise subprocess.TimeoutExpired("fake", timeout)
                return "", ""

            def kill(self):
                events.append("kill")

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                events.append("wait")
                self.returncode = -9
                return self.returncode

        with patch("killswitch.subprocess.Popen", FakeProcess):
            with self.assertRaises(subprocess.TimeoutExpired):
                recovery_command_runner(["fake"], timeout=0.01)
        self.assertEqual(events[:3], ["communicate", "kill", "communicate"])
        self.assertIn("wait", events)


if __name__ == "__main__":
    unittest.main()
