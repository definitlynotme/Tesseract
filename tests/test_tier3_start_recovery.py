import unittest

from tier3_start_recovery import (
    Tier3StartRecoveryAction,
    Tier3StartRecoveryPlan,
    tier3_start_recovery_plan,
)


class Tier3StartRecoveryTests(unittest.TestCase):
    def test_primary_ready_needs_no_bastion_recovery(self) -> None:
        self.assertEqual(
            tier3_start_recovery_plan(
                primary_ready=True,
                tier3_enabled=True,
                cached_healthy_count=0,
                target_healthy_count=3,
                primary_failure=None,
            ),
            Tier3StartRecoveryPlan(
                recovery_required=False,
                action=Tier3StartRecoveryAction.NONE,
                target_healthy_count=3,
                reason="primary layers are ready",
            ),
        )

    def test_primary_unavailable_empty_pool_is_normal_bastion_collection(self) -> None:
        no_primary = tier3_start_recovery_plan(
            primary_ready=False,
            tier3_enabled=True,
            cached_healthy_count=0,
            target_healthy_count=2,
            primary_failure="no primary layers are configured",
        )
        primary_down = tier3_start_recovery_plan(
            primary_ready=False,
            tier3_enabled=True,
            cached_healthy_count=0,
            target_healthy_count=2,
            primary_failure="Vanguard/Sentinel startup checks found no healthy nodes",
        )

        self.assertEqual(no_primary.action, Tier3StartRecoveryAction.COLLECT_POOL)
        self.assertEqual(primary_down.action, Tier3StartRecoveryAction.COLLECT_POOL)
        self.assertTrue(no_primary.recovery_required)
        self.assertTrue(primary_down.recovery_required)
        self.assertEqual(no_primary.target_healthy_count, 2)
        self.assertEqual(primary_down.target_healthy_count, 2)

    def test_cached_pool_is_revalidated_before_collection(self) -> None:
        plan = tier3_start_recovery_plan(
            primary_ready=False,
            tier3_enabled=True,
            cached_healthy_count=1,
            target_healthy_count=3,
            primary_failure="primary failed",
        )

        self.assertTrue(plan.recovery_required)
        self.assertEqual(plan.action, Tier3StartRecoveryAction.REVALIDATE_CACHED_POOL)
        self.assertEqual(plan.target_healthy_count, 3)

    def test_pending_cached_pool_is_revalidated_before_collection(self) -> None:
        plan = tier3_start_recovery_plan(
            primary_ready=False,
            tier3_enabled=True,
            cached_healthy_count=0,
            cached_revalidation_count=15,
            target_healthy_count=3,
            primary_failure="primary failed",
        )

        self.assertTrue(plan.recovery_required)
        self.assertEqual(plan.action, Tier3StartRecoveryAction.REVALIDATE_CACHED_POOL)
        self.assertEqual(plan.target_healthy_count, 3)

    def test_disabled_bastion_reports_primary_failure_as_unavailable_reason(self) -> None:
        plan = tier3_start_recovery_plan(
            primary_ready=False,
            tier3_enabled=False,
            cached_healthy_count=0,
            target_healthy_count=0,
            primary_failure="primary failed",
        )

        self.assertTrue(plan.recovery_required)
        self.assertEqual(plan.action, Tier3StartRecoveryAction.UNAVAILABLE)
        self.assertEqual(plan.target_healthy_count, 1)
        self.assertEqual(plan.reason, "primary failed; Bastion Layer is disabled")


if __name__ == "__main__":
    unittest.main()
