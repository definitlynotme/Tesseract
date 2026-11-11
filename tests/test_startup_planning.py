import unittest

from startup_planning import (
    bastion_disabled_empty_start_prompt_required,
    bastion_empty_pool_prompt_required,
    bastion_startup_plan,
    coerce_int,
    configured_start_source_count,
    format_startup_duration,
    recent_tier3_sources_available,
    tier3_recent_source_collection_status,
    tier3_config_from_runtime,
)


class StartupPlanningTests(unittest.TestCase):
    def test_configured_start_source_count_respects_tier3_enabled(self) -> None:
        disabled = {
            "tier1": {"total": 0},
            "tier2": {"total": 0, "subscription_count": 0},
            "tier3": {"enabled": "false", "total": 3, "subscription_count": 2, "bootstrap_enabled": True},
            "exit": {"total": 0, "subscription_count": 0},
            "sources": {"github_repos": 1, "telegram_channels": 1, "subscription_urls": 1, "local_files": 1},
        }
        self.assertEqual(configured_start_source_count(disabled), 0)

        enabled = {
            **disabled,
            "tier3": {"enabled": True, "total": 0, "subscription_count": 0, "bootstrap_enabled": True, "bootstrap_source_count": 3},
        }
        self.assertEqual(configured_start_source_count(enabled), 4)

    def test_configured_start_source_count_uses_enabled_node_totals(self) -> None:
        summary = {
            "tier1": {"total": 2, "enabled_total": 0},
            "tier2": {
                "total": 3,
                "enabled_total": 1,
                "subscription_count": 2,
                "enabled_subscription_count": 0,
            },
            "tier3": {
                "enabled": True,
                "total": 4,
                "enabled_total": 1,
                "subscription_count": 1,
                "enabled_subscription_count": 0,
                "bootstrap_enabled": False,
            },
            "exit": {
                "total": 2,
                "enabled_total": 0,
                "subscription_count": 1,
                "enabled_subscription_count": 0,
            },
            "sources": {},
        }

        self.assertEqual(configured_start_source_count(summary), 2)

    def test_disabled_drafts_do_not_suppress_empty_start_prompts(self) -> None:
        summary = {
            "tier1": {"total": 1, "enabled_total": 0},
            "tier2": {"total": 1, "enabled_total": 0, "subscription_count": 0},
            "tier3": {
                "enabled": False,
                "total": 1,
                "enabled_total": 0,
                "subscription_count": 0,
                "bootstrap_enabled": True,
                "bootstrap_source_count": 3,
            },
            "exit": {"total": 1, "enabled_total": 0, "subscription_count": 0},
            "sources": {},
        }

        self.assertTrue(bastion_disabled_empty_start_prompt_required(summary))

    def test_recent_tier3_source_collection_status_reports_pending_sources(self) -> None:
        summary = {
            "tier3": {
                "enabled": True,
                "collection": {
                    "source_count": 5,
                    "source_fetch_ok": 1,
                    "source_fetch_failed": 0,
                    "source_fetch_pending": 4,
                },
            }
        }

        status = tier3_recent_source_collection_status(summary)

        self.assertEqual(status, {"source_count": 5, "fetched": 1, "failed": 0, "pending": 4, "retry_due": 0})
        self.assertTrue(recent_tier3_sources_available(summary))

    def test_recent_tier3_source_collection_status_treats_retry_due_as_available(self) -> None:
        summary = {
            "tier3": {
                "enabled": True,
                "collection": {
                    "source_count": 5,
                    "source_fetch_ok": 2,
                    "source_fetch_failed": 3,
                    "source_fetch_pending": 0,
                    "source_retry_due_count": 2,
                    "source_retry_due": True,
                },
            }
        }

        status = tier3_recent_source_collection_status(summary)

        self.assertEqual(status["source_count"], 5)
        self.assertEqual(status["retry_due"], 2)
        self.assertTrue(recent_tier3_sources_available(summary))

    def test_recent_tier3_source_collection_status_prefers_configured_width(self) -> None:
        summary = {
            "tier3": {
                "enabled": True,
                "collection": {
                    "configured_source_count": 5,
                    "source_count": 2,
                    "source_fetch_ok": 2,
                    "source_fetch_failed": 0,
                    "source_fetch_pending": 0,
                },
            }
        }

        status = tier3_recent_source_collection_status(summary)

        self.assertEqual(status["source_count"], 5)
        self.assertEqual(status["pending"], 3)
        self.assertTrue(recent_tier3_sources_available(summary))

    def test_bastion_empty_pool_prompt_is_a_notice_condition_only(self) -> None:
        summary = {
            "tier1": {"total": 0},
            "tier2": {"total": 0, "subscription_count": 0},
            "tier3": {
                "enabled": True,
                "total": 0,
                "subscription_count": 0,
                "bootstrap_enabled": True,
                "bootstrap_source_count": 3,
                "runtime": {"healthy": 0},
            },
            "sources": {"github_repos": 0, "telegram_channels": 0, "subscription_urls": 0, "local_files": 0},
        }
        self.assertTrue(bastion_empty_pool_prompt_required(summary, prompt_available=True))
        self.assertFalse(bastion_empty_pool_prompt_required(summary, prompt_available=False))
        self.assertFalse(
            bastion_empty_pool_prompt_required(
                {**summary, "tier3": {**summary["tier3"], "runtime": {"healthy": 1}}},
                prompt_available=True,
            )
        )

    def test_bastion_empty_pool_prompt_does_not_treat_auto_pool_total_as_user_import(self) -> None:
        summary = {
            "tier1": {"total": 0},
            "tier2": {"total": 0, "subscription_count": 0},
            "tier3": {
                "enabled": True,
                "total": 15,
                "nodes": [],
                "subscription_count": 0,
                "bootstrap_enabled": True,
                "bootstrap_source_count": 3,
                "runtime": {"healthy": 0},
            },
            "sources": {"github_repos": 0, "telegram_channels": 0, "subscription_urls": 0, "local_files": 0},
        }

        self.assertEqual(configured_start_source_count(summary), 3)
        self.assertTrue(bastion_empty_pool_prompt_required(summary, prompt_available=True))

        with_user_node = {**summary, "tier3": {**summary["tier3"], "nodes": [{"name": "manual"}]}}
        self.assertFalse(bastion_empty_pool_prompt_required(with_user_node, prompt_available=True))

    def test_disabled_bastion_empty_start_prompt_points_to_manual_enable(self) -> None:
        summary = {
            "tier1": {"total": 0},
            "tier2": {"total": 0, "subscription_count": 0},
            "tier3": {
                "enabled": False,
                "total": 0,
                "subscription_count": 0,
                "bootstrap_enabled": True,
                "bootstrap_source_count": 3,
            },
            "exit": {"total": 0, "subscription_count": 0},
            "sources": {"github_repos": 0, "telegram_channels": 0, "subscription_urls": 0, "local_files": 0},
        }

        self.assertTrue(bastion_disabled_empty_start_prompt_required(summary))
        self.assertFalse(
            bastion_disabled_empty_start_prompt_required(
                {**summary, "tier3": {**summary["tier3"], "enabled": True}}
            )
        )
        self.assertFalse(
            bastion_empty_pool_prompt_required(
                {**summary, "tier3": {**summary["tier3"], "enabled": False}},
                prompt_available=True,
            )
        )

    def test_bastion_startup_plan_separates_gate_from_background_target(self) -> None:
        plan = bastion_startup_plan(
            {
                "tiers": {
                    "tier3": {
                        "min_alive_nodes": "20",
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
        self.assertGreaterEqual(plan["candidate_budget"], 42)
        self.assertGreaterEqual(plan["estimate_seconds"], 30)

    def test_coerce_int_and_nested_tier3_config_are_tolerant(self) -> None:
        self.assertEqual(coerce_int("inherit", 5, minimum=1), 5)
        self.assertEqual(coerce_int("0", 5, minimum=1), 1)
        self.assertEqual(coerce_int("bad", 5, minimum=1), 5)
        self.assertEqual(tier3_config_from_runtime({"tiers": {"tier3": {"enabled": True}}}), {"enabled": True})
        self.assertEqual(tier3_config_from_runtime({"tiers": []}), {})

    def test_format_startup_duration(self) -> None:
        self.assertEqual(format_startup_duration(45), "45 秒")
        self.assertEqual(format_startup_duration(125), "2 分 5 秒")
        self.assertEqual(format_startup_duration(3600), "1 小时")
        self.assertEqual(format_startup_duration(3660), "1 小时 1 分")


if __name__ == "__main__":
    unittest.main()
