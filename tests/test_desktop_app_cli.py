import faulthandler
import json
import tempfile
import sys
import unittest
import uuid
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from alert_config import disable_external_desktop_notifications
from app_paths import build_app_paths
from desktop_app_shell import (
    acquire_single_instance_lock,
    consume_single_instance_restore_request,
    create_single_instance_restore_event,
    parse_args,
    release_single_instance_lock,
    run_tier3_validation_parity_audit_cli,
    run_validator_helper_smoke,
    signal_existing_instance,
)
from dns_ui import dns_entries_for_ui, dns_rows_for_ui, validate_plain_dns_ips
from startup_planning import (
    bastion_disabled_empty_start_prompt_required,
    bastion_empty_pool_prompt_required,
    bastion_startup_plan,
    configured_start_source_count,
    format_startup_duration,
)


class DesktopAppCliTests(unittest.TestCase):
    def tearDown(self) -> None:
        release_single_instance_lock()

    @unittest.skipUnless(sys.platform == "win32", "Windows mutex behavior")
    def test_single_instance_lock_rejects_existing_mutex(self) -> None:
        import ctypes

        name = f"Global\\TesseractTestSingleInstance-{uuid.uuid4()}"
        handle = ctypes.windll.kernel32.CreateMutexW(None, False, name)
        try:
            self.assertTrue(handle)
            self.assertFalse(acquire_single_instance_lock(name))
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)

    @unittest.skipUnless(sys.platform == "win32", "Windows restore event behavior")
    def test_single_instance_restore_event_round_trips(self) -> None:
        name = f"Local\\TesseractTestRestoreWindow-{uuid.uuid4()}"

        self.assertTrue(create_single_instance_restore_event(name))
        self.assertFalse(consume_single_instance_restore_request())
        self.assertTrue(signal_existing_instance(name))
        self.assertTrue(consume_single_instance_restore_request())
        self.assertFalse(consume_single_instance_restore_request())

    def _paths(self, temp_path: Path):
        resource_root = temp_path / "resources"
        (resource_root / "bin").mkdir(parents=True)
        (resource_root / "static").mkdir()
        (resource_root / "locales").mkdir()
        (resource_root / "bin" / "mihomo.exe").write_text("fake", encoding="utf-8")
        (resource_root / "static" / "app_icon.ico").write_text("fake", encoding="utf-8")
        (resource_root / "locales" / "ja_JP.json").write_text("{}", encoding="utf-8")
        (resource_root / "locales" / "ru_RU.json").write_text("{}", encoding="utf-8")
        (resource_root / "config.example.yaml").write_text("client:\n  mode: clash\n", encoding="utf-8")
        (resource_root / "sources.example.yaml").write_text("github_repos: []\n", encoding="utf-8")
        (resource_root / "THIRD_PARTY_NOTICES.md").write_text("Test notices\n", encoding="utf-8")
        return build_app_paths(
            resource_root=resource_root,
            config_dir=temp_path / "config",
            data_dir=temp_path / "data",
        )

    def test_parse_version_and_smoke_flags(self) -> None:
        self.assertTrue(parse_args(["--version"]).version)
        self.assertTrue(parse_args(["--smoke-test"]).smoke_test)
        self.assertTrue(
            parse_args(["--node-card-native-smoke"]).node_card_native_smoke
        )
        self.assertTrue(
            parse_args(["--validator-helper-smoke"]).validator_helper_smoke
        )
        self.assertEqual(
            parse_args(["--validator-helper-smoke", "16"]).validator_helper_smoke,
            16,
        )
        self.assertEqual(parse_args(["--validator-helper-smoke"]).validator_helper_slots, 1)
        self.assertEqual(
            parse_args(
                ["--validator-helper-smoke", "8", "--validator-helper-slots", "8"]
            ).validator_helper_slots,
            8,
        )
        parity = parse_args(
            [
                "--tier3-validation-parity-audit",
                "--tier3-parity-sample-size",
                "12",
            ]
        )
        self.assertTrue(parity.tier3_validation_parity_audit)
        self.assertEqual(parity.tier3_parity_sample_size, 12)

    def test_parity_audit_failure_is_persisted_for_packaged_cli(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = self._paths(Path(temp_dir))
            with patch(
                "desktop_app_shell.load_desktop_config",
                side_effect=RuntimeError("fixture audit failure"),
            ):
                exit_code = run_tier3_validation_parity_audit_cli(
                    paths,
                    sample_size=1,
                )

            self.assertEqual(exit_code, 1)
            artifact = paths.data_dir / "tier3-validation-parity-audit.json"
            payload = json.loads(artifact.read_text(encoding="utf-8"))
            self.assertEqual(payload["verdict"], "audit_error")
            self.assertEqual(payload["error_type"], "RuntimeError")
            self.assertIn("fixture audit failure", payload["error_message"])

    def test_validator_helper_smoke_requires_ready_worker_and_clean_stop(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = self._paths(Path(temp_dir))

            class Supervisor:
                def __init__(
                    self,
                    *,
                    attempt_sink,
                    capacity_sink,
                    **_kwargs,
                ) -> None:
                    self.started = False
                    self.attempt_sink = attempt_sink
                    self.capacity_sink = capacity_sink

                def start(self) -> None:
                    self.started = True

                def snapshot(self) -> SimpleNamespace:
                    return SimpleNamespace(
                        idle_workers=1,
                        hard_stall=False,
                        last_failure_reason="",
                        workers=({"helper_pid": 10, "mihomo_pid": 11},),
                    )

                def stop(self, timeout_seconds: float) -> str:
                    self.assert_timeout = timeout_seconds
                    return "STOPPED"

            with patch(
                "tier3_validator_supervisor.Tier3ValidatorSupervisor",
                Supervisor,
            ):
                self.assertEqual(run_validator_helper_smoke(paths), 0)

            self.assertFalse(
                (paths.data_dir / "validator-helper-smoke-stacks.log").exists()
            )

    def test_validator_helper_smoke_batch_slots_require_every_identity(self) -> None:
        created: list[dict] = []

        def supervisor_class(helper_pids: list[int]):
            class Supervisor:
                def __init__(self, **kwargs) -> None:
                    created.append(kwargs)

                def start(self) -> None:
                    pass

                def snapshot(self) -> SimpleNamespace:
                    return SimpleNamespace(
                        idle_workers=len(helper_pids),
                        hard_stall=False,
                        last_failure_reason="",
                        workers=tuple(
                            {"helper_pid": pid, "mihomo_pid": pid + 1000}
                            for pid in helper_pids
                        ),
                    )

                def stop(self, timeout_seconds: float) -> str:
                    return "STOPPED"

            return Supervisor

        with tempfile.TemporaryDirectory() as temp_dir:
            paths = self._paths(Path(temp_dir))
            # Two processes with four identities each: eight ready identities.
            with patch(
                "tier3_batch_validator_supervisor.Tier3BatchValidatorSupervisor",
                supervisor_class([10, 10, 10, 10, 20, 20, 20, 20]),
            ):
                self.assertEqual(
                    run_validator_helper_smoke(paths, 2, slots_per_process=4),
                    0,
                )
            self.assertEqual(created[-1]["slots_per_process"], 4)
            self.assertEqual(created[-1]["min_workers"], 8)
            self.assertEqual(created[-1]["max_workers"], 8)

            # Eight identities that all live in one process is not two helpers.
            with patch(
                "tier3_batch_validator_supervisor.Tier3BatchValidatorSupervisor",
                supervisor_class([10] * 8),
            ):
                self.assertEqual(
                    run_validator_helper_smoke(paths, 2, slots_per_process=4),
                    1,
                )
            payload = json.loads(
                (paths.data_dir / "validator-helper-smoke.json").read_text(encoding="utf-8")
            )
            self.assertIn("expected 2 validator helper processes", payload["error"])

    def test_validator_helper_smoke_dumps_stack_only_after_real_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = self._paths(Path(temp_dir))

            class Supervisor:
                def __init__(self, **_kwargs) -> None:
                    pass

                def start(self) -> None:
                    pass

                def snapshot(self) -> SimpleNamespace:
                    return SimpleNamespace(
                        idle_workers=0,
                        hard_stall=True,
                        last_failure_reason="fixture hard stall",
                        workers=(),
                    )

                def stop(self, timeout_seconds: float) -> str:
                    self.assert_timeout = timeout_seconds
                    return "STOPPED"

            with (
                patch(
                    "tier3_validator_supervisor.Tier3ValidatorSupervisor",
                    Supervisor,
                ),
                patch(
                    "desktop_app_shell.faulthandler.dump_traceback_later",
                    side_effect=AssertionError("native watchdog must not be armed"),
                ) as delayed_dump,
                patch(
                    "desktop_app_shell.faulthandler.cancel_dump_traceback_later",
                    side_effect=AssertionError("native watchdog must not be cancelled"),
                ) as cancel_delayed_dump,
                patch(
                    "desktop_app_shell.faulthandler.dump_traceback",
                    wraps=faulthandler.dump_traceback,
                ) as synchronous_dump,
            ):
                self.assertEqual(run_validator_helper_smoke(paths), 1)

            delayed_dump.assert_not_called()
            cancel_delayed_dump.assert_not_called()
            synchronous_dump.assert_called_once()
            stack_path = paths.data_dir / "validator-helper-smoke-stacks.log"
            self.assertTrue(stack_path.is_file())
            self.assertGreater(stack_path.stat().st_size, 0)
            self.assertTrue(synchronous_dump.call_args.kwargs["all_threads"])
            diagnostic = json.loads(
                (paths.data_dir / "validator-helper-smoke.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                diagnostic["error"],
                "RuntimeError: fixture hard stall",
            )

    def test_native_ui_disables_external_desktop_notifications(self) -> None:
        config = {"alerts": {"desktop": {"enabled": True, "timeout": 8}}}

        patched = disable_external_desktop_notifications(config)

        self.assertFalse(patched["alerts"]["desktop"]["enabled"])
        self.assertTrue(patched["alerts"]["desktop"]["disabled_in_native_ui"])

    def test_dns_ui_defaults_and_ip_validation(self) -> None:
        # No resolver is invented for an empty field any more; mode decides.
        self.assertEqual(dns_entries_for_ui({}, "default_nameserver"), [])
        self.assertEqual(
            dns_entries_for_ui({"nameserver": "https://dns.example/dns-query\n1.1.1.1"}, "nameserver"),
            ["https://dns.example/dns-query", "1.1.1.1"],
        )
        self.assertTrue(validate_plain_dns_ips(["223.5.5.5", "2001:4860:4860::8888"]))
        self.assertFalse(validate_plain_dns_ips(["https://dns.example/dns-query"]))
        self.assertEqual(
            dns_rows_for_ui({"proxy_server_nameserver": ["9.9.9.9"]})[-1],
            {"type": "proxy_server_nameserver", "value": "9.9.9.9"},
        )

    def test_enabled_bastion_sources_count_as_startable_configuration(self) -> None:
        summary = {
            "tier1": {"total": 0},
            "tier2": {"total": 0, "subscription_count": 0},
            "tier3": {"enabled": True, "total": 0, "subscription_count": 0, "bootstrap_enabled": True},
            "exit": {"total": 0, "subscription_count": 0},
            "sources": {"github_repos": 0, "telegram_channels": 0, "subscription_urls": 1, "local_files": 0},
        }

        self.assertEqual(configured_start_source_count(summary), 1)

    def test_bastion_sources_do_not_count_until_enabled(self) -> None:
        summary = {
            "tier1": {"total": 0},
            "tier2": {"total": 0, "subscription_count": 0},
            "tier3": {"enabled": False, "total": 1, "subscription_count": 1, "bootstrap_enabled": True},
            "exit": {"total": 0, "subscription_count": 0},
            "sources": {"github_repos": 1, "telegram_channels": 1, "subscription_urls": 1, "local_files": 0},
        }

        self.assertEqual(configured_start_source_count(summary), 0)

    def test_bastion_string_false_does_not_count_as_enabled(self) -> None:
        summary = {
            "tier1": {"total": 0},
            "tier2": {"total": 0, "subscription_count": 0},
            "tier3": {"enabled": "false", "total": 0, "subscription_count": 0, "bootstrap_enabled": "true"},
            "exit": {"total": 0, "subscription_count": 0},
            "sources": {"github_repos": 1, "telegram_channels": 1, "subscription_urls": 1, "local_files": 1},
        }

        self.assertEqual(configured_start_source_count(summary), 0)
        self.assertFalse(bastion_empty_pool_prompt_required(summary, prompt_available=True))

    def test_disabled_bastion_prompt_surfaces_empty_start_enable_guidance(self) -> None:
        summary = {
            "tier1": {"total": 0},
            "tier2": {"total": 0, "subscription_count": 0},
            "tier3": {"enabled": False, "total": 0, "subscription_count": 0, "bootstrap_enabled": True, "bootstrap_source_count": 5},
            "exit": {"total": 0, "subscription_count": 0},
            "sources": {"github_repos": 0, "telegram_channels": 0, "subscription_urls": 0, "local_files": 0},
        }

        self.assertTrue(bastion_disabled_empty_start_prompt_required(summary))
        self.assertEqual(configured_start_source_count(summary), 0)
        self.assertFalse(bastion_empty_pool_prompt_required(summary, prompt_available=True))

    def test_bastion_bootstrap_does_not_invent_unconfigured_sources(self) -> None:
        summary = {
            "tier1": {"total": 0},
            "tier2": {"total": 0, "subscription_count": 0},
            "tier3": {"enabled": True, "total": 0, "subscription_count": 0, "bootstrap_enabled": True},
            "exit": {"total": 0, "subscription_count": 0},
            "sources": {"github_repos": 0, "telegram_channels": 0, "subscription_urls": 0, "local_files": 0},
        }

        self.assertEqual(configured_start_source_count(summary), 0)

    def test_bastion_bootstrap_count_uses_configured_source_count(self) -> None:
        summary = {
            "tier1": {"total": 0},
            "tier2": {"total": 0, "subscription_count": 0},
            "tier3": {"enabled": True, "total": 0, "subscription_count": 0, "bootstrap_enabled": True, "bootstrap_source_count": 3},
            "exit": {"total": 0, "subscription_count": 0},
            "sources": {"github_repos": 0, "telegram_channels": 0, "subscription_urls": 0, "local_files": 0},
        }

        self.assertEqual(configured_start_source_count(summary), 3)

    def test_bastion_empty_pool_prompt_is_only_ui_notice_for_empty_primary_bootstrap(self) -> None:
        summary = {
            "tier1": {"total": 0},
            "tier2": {"total": 0, "subscription_count": 0},
            "tier3": {
                "enabled": True,
                "total": 0,
                "subscription_count": 0,
                "bootstrap_enabled": True,
                "bootstrap_source_count": 5,
                "runtime": {"healthy": 0},
            },
            "sources": {"github_repos": 0, "telegram_channels": 0, "subscription_urls": 0, "local_files": 0},
        }

        self.assertTrue(bastion_empty_pool_prompt_required(summary, prompt_available=True))
        self.assertFalse(bastion_empty_pool_prompt_required(summary, prompt_available=False))

        with_primary = {**summary, "tier1": {"total": 1}}
        self.assertFalse(bastion_empty_pool_prompt_required(with_primary, prompt_available=True))

        with_tier3_import = {**summary, "tier3": {**summary["tier3"], "subscription_count": 1}}
        self.assertFalse(bastion_empty_pool_prompt_required(with_tier3_import, prompt_available=True))

        with_runtime_pool = {**summary, "tier3": {**summary["tier3"], "runtime": {"healthy": 1}}}
        self.assertFalse(bastion_empty_pool_prompt_required(with_runtime_pool, prompt_available=True))

    def test_bastion_empty_pool_prompt_ignores_auto_pool_total_when_user_nodes_are_empty(self) -> None:
        summary = {
            "tier1": {"total": 0},
            "tier2": {"total": 0, "subscription_count": 0},
            "tier3": {
                "enabled": True,
                "total": 15,
                "nodes": [],
                "subscription_count": 0,
                "bootstrap_enabled": True,
                "bootstrap_source_count": 5,
                "runtime": {"healthy": 0},
            },
            "sources": {"github_repos": 0, "telegram_channels": 0, "subscription_urls": 0, "local_files": 0},
        }

        self.assertTrue(bastion_empty_pool_prompt_required(summary, prompt_available=True))
        self.assertEqual(configured_start_source_count(summary), 5)

    def test_bastion_startup_plan_keeps_gate_and_background_target_separate(self) -> None:
        plan = bastion_startup_plan(
            {
                "tiers": {
                    "tier3": {
                        "min_alive_nodes": 17,
                        "validation_empty_retry_limit": 40,
                        "startup_validation_empty_retry_limit": None,
                        "startup_validation_concurrency": 8,
                        "pi_controller": {"target_alive": 25},
                    }
                },
                "testing": {
                    "request_timeout": 4,
                    "tcp_timeout": 1,
                    "mihomo_helper": {"probe_timeout": 2, "startup_timeout": 3, "concurrency": 4},
                },
            }
        )

        self.assertEqual(plan["target"], 17)
        self.assertEqual(plan["gate"], 17)
        self.assertEqual(plan["background_target"], 39)
        self.assertEqual(plan["candidate_budget"], 156)
        self.assertGreaterEqual(plan["estimate_seconds"], 30)

    def test_bastion_startup_plan_keeps_background_target_at_least_gate(self) -> None:
        plan = bastion_startup_plan(
            {
                "tiers": {
                    "tier3": {
                        "min_alive_nodes": 20,
                        "validation_empty_retry_limit": 4,
                        "startup_validation_empty_retry_limit": 4,
                        "pi_controller": {"target_alive": 5},
                    }
                },
                "testing": {"mihomo_helper": {"concurrency": 3}},
            }
        )

        self.assertEqual(plan["target"], 20)
        self.assertEqual(plan["gate"], 20)
        self.assertEqual(plan["background_target"], 42)
        self.assertEqual(plan["candidate_budget"], 168)

    def test_format_startup_duration(self) -> None:
        self.assertEqual(format_startup_duration(45), "45 秒")
        self.assertEqual(format_startup_duration(125), "2 分 5 秒")


if __name__ == "__main__":
    unittest.main()
