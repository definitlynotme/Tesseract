from __future__ import annotations

import threading
import unittest

from subscription_operations import (
    SubscriptionOperationCoordinator,
    canonical_subscription_url,
    subscription_operation_key,
)


class SubscriptionOperationCoordinatorTests(unittest.TestCase):
    def test_equivalent_urls_share_one_operation_key(self) -> None:
        first = " HTTPS://User:Secret@Example.COM:443/path?q=1#client-fragment "
        second = "https://User:Secret@example.com/path?q=1"

        self.assertEqual(
            subscription_operation_key(" Tier2 ", first),
            subscription_operation_key("tier2", second),
        )
        self.assertEqual(
            canonical_subscription_url(first),
            "https://User:Secret@example.com/path?q=1",
        )
        self.assertEqual(
            canonical_subscription_url("HTTP://Example.COM:80/feed#fragment"),
            "http://example.com/feed",
        )

        coordinator = SubscriptionOperationCoordinator()
        lease = coordinator.begin_system("tier2", first)
        try:
            self.assertNotIn("Secret", repr(lease))
            self.assertTrue(coordinator.is_system_active("tier2", second))
        finally:
            coordinator.finish(lease)

    def test_system_preempts_matching_manual_without_waiting(self) -> None:
        coordinator = SubscriptionOperationCoordinator()
        cancel_event = threading.Event()
        manual = coordinator.try_begin_manual(
            "tier2",
            "https://example.com/sub#manual",
            cancel_event=cancel_event,
        )
        self.assertIsNotNone(manual)
        self.assertIsNone(
            coordinator.try_begin_manual(
                "tier2",
                "HTTPS://EXAMPLE.COM:443/sub",
            )
        )

        system = coordinator.begin_system(
            "tier2",
            "HTTPS://EXAMPLE.COM:443/sub",
        )
        try:
            self.assertTrue(cancel_event.is_set())
            self.assertEqual(
                coordinator.manual_active_count("tier2", "https://example.com/sub"),
                0,
            )
            self.assertIsNone(
                coordinator.try_begin_manual("tier2", "https://example.com/sub")
            )
        finally:
            coordinator.finish(system)
            if manual is not None:
                coordinator.finish(manual)

        self.assertIsNotNone(
            coordinator.try_begin_manual("tier2", "https://example.com/sub")
        )

    def test_system_batch_only_preempts_matching_keys(self) -> None:
        coordinator = SubscriptionOperationCoordinator()
        matching_cancel = threading.Event()
        other_url_cancel = threading.Event()
        other_tier_cancel = threading.Event()
        matching = coordinator.try_begin_manual(
            "exit",
            "https://example.com/a",
            cancel_event=matching_cancel,
        )
        other_url = coordinator.try_begin_manual(
            "exit",
            "https://example.com/b",
            cancel_event=other_url_cancel,
        )
        other_tier = coordinator.try_begin_manual(
            "tier2",
            "https://example.com/a",
            cancel_event=other_tier_cancel,
        )

        leases = coordinator.begin_system_batch(
            "exit",
            ["https://example.com/a", "https://example.com/a#duplicate"],
        )
        try:
            self.assertEqual(len(leases), 1)
            self.assertTrue(matching_cancel.is_set())
            self.assertFalse(other_url_cancel.is_set())
            self.assertFalse(other_tier_cancel.is_set())
        finally:
            for lease in leases:
                coordinator.finish(lease)
            for lease in (matching, other_url, other_tier):
                if lease is not None:
                    coordinator.finish(lease)


if __name__ == "__main__":
    unittest.main()
