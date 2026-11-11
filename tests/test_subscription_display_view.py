from __future__ import annotations

import copy
import unittest
from typing import Any

from node_status_view import (
    node_row_meta,
    subscription_group_detail_rows,
    subscription_group_key,
)
from subscription_display_view import (
    SubscriptionDisplayGate,
    project_subscription_rows_for_display,
    subscription_core_stamp,
)


TIER2_URL_A = "https://subscriptions.example/a"
TIER2_URL_B = "https://subscriptions.example/b"
TIER3_URL = "https://subscriptions.example/tier3"


def _subscription(tier: str, url: str) -> dict[str, Any]:
    return {
        "tier": tier,
        "kind": "subscription",
        "label": f"{tier}:{url.rsplit('/', 1)[-1]}",
        "url": url,
        "enabled": True,
    }


def _status(
    *,
    running: bool,
    runtime_generation: int = 1,
    core_generation: int = 1,
    pid: int | str = 4100,
) -> dict[str, Any]:
    return {
        "runtime_generation": runtime_generation,
        "core_generation": core_generation,
        "core_running": running,
        "core": {
            "running": running,
            "pid": pid,
        },
    }


def _row(
    name: str,
    *,
    tier: str = "tier2",
    source: str = TIER2_URL_A,
    sources: list[str] | None = None,
    source_type: str = "subscription_url",
    origin: str = "live",
    observed_at: str = "2026-08-02T01:00:00+00:00",
    status: str = "alive",
    latency: str = "31",
    identity: str | None = None,
) -> list[Any]:
    return [
        tier,
        name,
        "vless",
        f"{name.lower().replace(' ', '-')}.example:443",
        status,
        latency,
        observed_at,
        "1",
        "yes",
        "Normal",
        {
            "identity": identity or f"{tier}|{name}",
            "source_type": source_type,
            "source": source,
            "sources": list(sources or [source]),
            "observation_origin": origin,
            "observed_at": observed_at,
        },
    ]


def _project(
    rows: list[list[Any]],
    config_items: list[dict[str, Any]],
    status: dict[str, Any],
    gate: SubscriptionDisplayGate,
    *,
    batch_status: dict[str, Any] | None = None,
) -> list[list[Any]]:
    return project_subscription_rows_for_display(
        rows,
        config_items,
        status,
        gate,
        row_batch_stamp=subscription_core_stamp(batch_status or status),
    )


class SubscriptionDisplayViewTests(unittest.TestCase):
    def assertPending(self, row: list[Any]) -> None:
        self.assertEqual(row[4], "configured")
        self.assertEqual(row[5], "")
        self.assertEqual(row[6], "")
        meta = node_row_meta(row)
        self.assertEqual(meta.get("observation_origin"), "persisted_cache")
        self.assertEqual(meta.get("observed_at"), "")
        self.assertIs(meta.get("display_pending_recheck"), True)

    def assertLive(
        self,
        row: list[Any],
        *,
        status: str = "alive",
        latency: str = "31",
    ) -> None:
        self.assertEqual(row[4], status)
        self.assertEqual(row[5], latency)
        meta = node_row_meta(row)
        self.assertEqual(meta.get("observation_origin"), "live")
        self.assertNotIn("display_pending_recheck", meta)

    def test_core_off_projection_is_membership_only_and_keeps_inputs_unchanged(
        self,
    ) -> None:
        tier2_subscription = _subscription("tier2", TIER2_URL_A)
        tier3_subscription = _subscription("tier3", TIER3_URL)
        rows = [
            _row(
                "Cached Tier2",
                origin="persisted_cache",
                status="slow",
                latency="480",
            ),
            _row(
                "Manual Tier2",
                source_type="manual",
                status="alive",
                latency="22",
            ),
            _row(
                "Tier3 Subscription",
                tier="tier3",
                source=TIER3_URL,
                sources=[TIER3_URL],
                status="alive",
                latency="44",
            ),
        ]
        rows[0][9] = "previous self-check failure"
        rows[0][10]["static_risk"] = "Config Error: missing UUID"
        config_items = [tier2_subscription, tier3_subscription]
        status = _status(
            running=False,
            runtime_generation=0,
            core_generation=0,
            pid="",
        )
        rows_before = copy.deepcopy(rows)
        config_before = copy.deepcopy(config_items)
        status_before = copy.deepcopy(status)

        projected = _project(
            rows,
            config_items,
            status,
            SubscriptionDisplayGate(),
        )

        self.assertPending(projected[0])
        self.assertEqual(projected[0][9], "Config Error: missing UUID")
        self.assertNotIn("self-check failure", str(projected[0]))
        self.assertEqual(projected[1], rows_before[1])
        self.assertEqual(projected[2], rows_before[2])
        self.assertIsNot(projected[0], rows[0])
        self.assertIsNot(projected[1], rows[1])
        self.assertIsNot(projected[2], rows[2])
        self.assertEqual(rows, rows_before)
        self.assertEqual(config_items, config_before)
        self.assertEqual(status, status_before)

    def test_first_core_start_unlocks_only_after_watermark_advances(
        self,
    ) -> None:
        item = _subscription("tier2", TIER2_URL_A)
        gate = SubscriptionDisplayGate()
        stopped = _status(
            running=False,
            runtime_generation=1,
            core_generation=0,
            pid="",
        )
        running = _status(
            running=True,
            runtime_generation=1,
            core_generation=1,
            pid=4100,
        )
        baseline = [_row("A", observed_at="2026-08-02T01:00:00+00:00")]
        baseline_before = copy.deepcopy(baseline)

        cached = _row(
            "A",
            origin="persisted_cache",
            observed_at="2026-08-02T01:00:00+00:00",
        )
        self.assertPending(_project([cached], [item], stopped, gate)[0])
        first = _project(baseline, [item], running, gate)
        unchanged = _project(baseline, [item], running, gate)
        advanced_rows = [
            _row("A", observed_at="2026-08-02T01:00:01+00:00")
        ]
        advanced_before = copy.deepcopy(advanced_rows)
        advanced = _project(advanced_rows, [item], running, gate)

        self.assertPending(first[0])
        self.assertPending(unchanged[0])
        self.assertLive(advanced[0])
        self.assertIn(subscription_group_key(item), gate.unlocked_groups)
        self.assertEqual(baseline, baseline_before)
        self.assertEqual(advanced_rows, advanced_before)

    def test_stop_and_core_restart_require_a_current_batch_and_new_watermark(
        self,
    ) -> None:
        item = _subscription("tier2", TIER2_URL_A)
        gate = SubscriptionDisplayGate()
        first_core = _status(
            running=True,
            runtime_generation=3,
            core_generation=7,
            pid=4701,
        )
        t0_rows = [_row("A", observed_at="2026-08-02T02:00:00+00:00")]
        t1_rows = [_row("A", observed_at="2026-08-02T02:00:01+00:00")]
        self.assertPending(_project(t0_rows, [item], first_core, gate)[0])
        self.assertLive(_project(t1_rows, [item], first_core, gate)[0])

        stopped = _status(
            running=False,
            runtime_generation=3,
            core_generation=7,
            pid="",
        )
        self.assertPending(_project(t1_rows, [item], stopped, gate)[0])
        self.assertEqual(gate.unlocked_groups, set())

        restarted = _status(
            running=True,
            runtime_generation=3,
            core_generation=8,
            pid=4801,
        )
        t2_rows = [_row("A", observed_at="2026-08-02T02:00:02+00:00")]
        stale_batch = _project(
            t2_rows,
            [item],
            restarted,
            gate,
            batch_status=first_core,
        )
        fresh_batch = _project(t2_rows, [item], restarted, gate)
        t3_rows = [_row("A", observed_at="2026-08-02T02:00:03+00:00")]
        advanced_batch = _project(t3_rows, [item], restarted, gate)

        self.assertPending(stale_batch[0])
        self.assertPending(fresh_batch[0])
        self.assertLive(advanced_batch[0])

        old_core_late = _project(
            [_row("A", observed_at="2026-08-02T02:00:04+00:00")],
            [item],
            first_core,
            gate,
        )
        self.assertPending(old_core_late[0])
        self.assertEqual(gate.core_stamp, subscription_core_stamp(restarted))
        self.assertLive(_project(t3_rows, [item], restarted, gate)[0])

    def test_pid_change_relocks_health_even_when_generations_are_unchanged(
        self,
    ) -> None:
        item = _subscription("exit", TIER2_URL_A)
        gate = SubscriptionDisplayGate()
        first_process = _status(
            running=True,
            runtime_generation=5,
            core_generation=9,
            pid=5901,
        )
        t0_rows = [
            _row(
                "Exit A",
                tier="exit",
                observed_at="2026-08-02T03:00:00+00:00",
            )
        ]
        t1_rows = [
            _row(
                "Exit A",
                tier="exit",
                observed_at="2026-08-02T03:00:01+00:00",
            )
        ]
        self.assertPending(_project(t0_rows, [item], first_process, gate)[0])
        self.assertLive(_project(t1_rows, [item], first_process, gate)[0])

        replacement_process = _status(
            running=True,
            runtime_generation=5,
            core_generation=9,
            pid=5902,
        )
        same_watermark = _project(
            t1_rows,
            [item],
            replacement_process,
            gate,
        )
        t2_rows = [
            _row(
                "Exit A",
                tier="exit",
                observed_at="2026-08-02T03:00:02+00:00",
            )
        ]
        advanced = _project(t2_rows, [item], replacement_process, gate)

        self.assertPending(same_watermark[0])
        self.assertLive(advanced[0])

    def test_explicit_close_rejects_late_same_core_batches_until_replacement(
        self,
    ) -> None:
        item = _subscription("tier2", TIER2_URL_A)
        gate = SubscriptionDisplayGate()
        first_core = _status(
            running=True,
            runtime_generation=8,
            core_generation=12,
            pid=8120,
        )
        t0_rows = [_row("A", observed_at="2026-08-02T05:00:00+00:00")]
        t1_rows = [_row("A", observed_at="2026-08-02T05:00:01+00:00")]
        self.assertPending(_project(t0_rows, [item], first_core, gate)[0])
        self.assertLive(_project(t1_rows, [item], first_core, gate)[0])

        gate.close()
        late_t2_rows = [
            _row("A", observed_at="2026-08-02T05:00:02+00:00")
        ]
        late_t3_rows = [
            _row("A", observed_at="2026-08-02T05:00:03+00:00")
        ]
        self.assertPending(_project(late_t2_rows, [item], first_core, gate)[0])
        self.assertPending(_project(late_t3_rows, [item], first_core, gate)[0])
        self.assertEqual(gate.unlocked_groups, set())
        self.assertEqual(gate.blocked_core_stamp, subscription_core_stamp(first_core))

        replacement = _status(
            running=True,
            runtime_generation=8,
            core_generation=13,
            pid=8130,
        )
        t4_rows = [_row("A", observed_at="2026-08-02T05:00:04+00:00")]
        # The first payload that reports a new Core only establishes the
        # transition and its current watermark baseline.  Repeating that
        # batch is still not proof from the new Core.
        self.assertPending(_project(t4_rows, [item], replacement, gate)[0])
        self.assertPending(_project(t4_rows, [item], replacement, gate)[0])
        t5_rows = [_row("A", observed_at="2026-08-02T05:00:05+00:00")]
        self.assertLive(_project(t5_rows, [item], replacement, gate)[0])
        self.assertIsNone(gate.blocked_core_stamp)

    def test_shared_node_evidence_cannot_unlock_the_other_subscription(
        self,
    ) -> None:
        item_a = _subscription("tier2", TIER2_URL_A)
        item_b = _subscription("tier2", TIER2_URL_B)
        items = [item_a, item_b]
        gate = SubscriptionDisplayGate()
        running = _status(
            running=True,
            runtime_generation=6,
            core_generation=10,
            pid=6100,
        )

        def rows(a_time: str, b_time: str, shared_time: str) -> list[list[Any]]:
            return [
                _row(
                    "A only",
                    source=TIER2_URL_A,
                    sources=[TIER2_URL_A],
                    observed_at=a_time,
                    identity="a-only",
                ),
                _row(
                    "B only",
                    source=TIER2_URL_B,
                    sources=[TIER2_URL_B],
                    observed_at=b_time,
                    identity="b-only",
                ),
                _row(
                    "Shared",
                    source=TIER2_URL_A,
                    sources=[TIER2_URL_A, TIER2_URL_B],
                    observed_at=shared_time,
                    identity="shared",
                ),
            ]

        t0 = "2026-08-02T04:00:00+00:00"
        baseline = rows(t0, t0, t0)
        self.assertTrue(
            all(
                row[4] == "configured"
                for row in _project(baseline, items, running, gate)
            )
        )

        shared_only = rows(t0, t0, "2026-08-02T04:00:01+00:00")
        shared_only_before = copy.deepcopy(shared_only)
        shared_projection = _project(shared_only, items, running, gate)
        self.assertTrue(
            all(row[4] == "configured" for row in shared_projection)
        )
        self.assertEqual(gate.unlocked_groups, set())

        a_advanced = rows(
            "2026-08-02T04:00:02+00:00",
            t0,
            "2026-08-02T04:00:01+00:00",
        )
        a_advanced_before = copy.deepcopy(a_advanced)
        projected = _project(a_advanced, items, running, gate)
        key_a = subscription_group_key(item_a)
        key_b = subscription_group_key(item_b)

        self.assertEqual(gate.unlocked_groups, {key_a})
        self.assertLive(projected[0])
        self.assertPending(projected[1])
        self.assertLive(projected[2])
        shared_meta = node_row_meta(projected[2])
        self.assertEqual(
            shared_meta.get("subscription_display_group_keys"),
            (key_a, key_b),
        )
        self.assertEqual(
            shared_meta.get("subscription_display_unlocked_group_keys"),
            (key_a,),
        )

        detail_a = subscription_group_detail_rows(
            "tier2",
            item_a,
            projected,
            translate=lambda text: text,
        )[1:]
        detail_b = subscription_group_detail_rows(
            "tier2",
            item_b,
            projected,
            translate=lambda text: text,
        )[1:]
        self.assertEqual([row[4] for row in detail_a], ["alive", "alive"])
        self.assertEqual(
            [row[4] for row in detail_b],
            ["configured", "configured"],
        )
        self.assertEqual(shared_only, shared_only_before)
        self.assertEqual(a_advanced, a_advanced_before)


if __name__ == "__main__":
    unittest.main()
