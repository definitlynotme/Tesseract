from __future__ import annotations

import unittest

from tier3_source_supply import Tier3SourceSupplySupervisor


class MutableClock:
    def __init__(self) -> None:
        self.value = 10.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class Tier3SourceSupplySupervisorTests(unittest.TestCase):
    def test_retry_lease_is_reclaimable_after_backoff_and_route_wake(self) -> None:
        clock = MutableClock()
        supervisor = Tier3SourceSupplySupervisor(
            runtime_generation=7,
            refresh_epoch=3,
            root_keys=("a", "b"),
            clock=clock,
        )

        leases = supervisor.claim_due(("a", "b"), limit=2)
        self.assertEqual([lease.root_key for lease in leases], ["a", "b"])
        self.assertTrue(
            supervisor.finish(leases[0], ok=False, retryable=True)
        )
        self.assertTrue(
            supervisor.finish(leases[1], ok=True, retryable=False)
        )
        self.assertEqual(supervisor.claim_due(("a", "b"), limit=2), ())

        supervisor.wake("config_generation_changed")
        reclaimed = supervisor.claim_due(("a", "b"), limit=2)
        self.assertEqual([lease.root_key for lease in reclaimed], ["a"])

    def test_dispatch_deferred_does_not_consume_backoff_attempt(self) -> None:
        clock = MutableClock()
        supervisor = Tier3SourceSupplySupervisor(
            runtime_generation=9,
            refresh_epoch=4,
            root_keys=("root",),
            clock=clock,
        )
        first = supervisor.claim_due(("root",), limit=1)[0]
        self.assertEqual(first.attempt, 1)
        self.assertTrue(
            supervisor.finish(
                first,
                ok=False,
                retryable=True,
                deferred=True,
            )
        )
        clock.advance(0.25)
        second = supervisor.claim_due(("root",), limit=1)[0]
        self.assertEqual(second.attempt, 1)
        self.assertEqual(supervisor.snapshot().deferred_count, 1)

    def test_terminal_and_success_roots_leave_no_actionable_work(self) -> None:
        supervisor = Tier3SourceSupplySupervisor(
            runtime_generation=1,
            refresh_epoch=1,
            root_keys=("missing", "ok"),
        )
        missing, ok = supervisor.claim_due(("missing", "ok"), limit=2)
        self.assertTrue(
            supervisor.finish(missing, ok=False, retryable=False)
        )
        self.assertTrue(supervisor.finish(ok, ok=True, retryable=False))
        self.assertFalse(supervisor.actionable())
        snapshot = supervisor.snapshot()
        self.assertEqual(snapshot.terminal_roots, 1)
        self.assertEqual(snapshot.succeeded_roots, 1)

    def test_wait_consumes_a_wake_that_arrived_before_wait(self) -> None:
        supervisor = Tier3SourceSupplySupervisor(
            runtime_generation=2,
            refresh_epoch=5,
            root_keys=("root",),
        )

        supervisor.wake("net_new_admission")

        self.assertTrue(supervisor.wait(0.0))
        self.assertFalse(supervisor.wait(0.0))

    def test_stale_pending_report_does_not_reopen_completed_root(self) -> None:
        supervisor = Tier3SourceSupplySupervisor(
            runtime_generation=4,
            refresh_epoch=9,
            root_keys=("root",),
        )
        lease = supervisor.claim_due(("root",), limit=1)[0]
        self.assertTrue(supervisor.finish(lease, ok=True, retryable=False))

        supervisor.observe_root(
            "root",
            ok=False,
            pending=True,
            retryable=True,
        )

        self.assertFalse(supervisor.actionable())
        self.assertEqual(supervisor.snapshot().succeeded_roots, 1)

    def test_successful_root_can_be_conditionally_refreshed_after_minimum_age(self) -> None:
        clock = MutableClock()
        supervisor = Tier3SourceSupplySupervisor(
            runtime_generation=4,
            refresh_epoch=10,
            root_keys=("fresh", "retry"),
            clock=clock,
        )
        fresh, retry = supervisor.claim_due(("fresh", "retry"), limit=2)
        self.assertTrue(supervisor.finish(fresh, ok=True, retryable=False))
        self.assertTrue(supervisor.finish(retry, ok=False, retryable=True))

        clock.advance(29.9)
        self.assertEqual(supervisor.rearm_succeeded(), ())
        self.assertEqual(supervisor.due_root_keys(), ("retry",))

        clock.advance(0.1)
        self.assertEqual(supervisor.rearm_succeeded(), ("fresh",))
        self.assertEqual(
            supervisor.due_root_keys(),
            ("fresh", "retry"),
        )
        lease = supervisor.claim_due(("fresh",), limit=1)[0]
        self.assertEqual(lease.root_key, "fresh")
        self.assertTrue(lease.refresh_existing)
        self.assertTrue(
            supervisor.finish(lease, ok=False, retryable=True)
        )
        self.assertEqual(supervisor.snapshot().succeeded_roots, 1)


if __name__ == "__main__":
    unittest.main()
