from __future__ import annotations

import logging
import unittest

from log_event_reports import LOG_EVENT_SPECS, build_log_event_report
from log_store import LogRecord
from tier_log_events import MODE_EVENT_KEYS, TierLogTracker, emit_tier_log_events


def metrics(
    mode: str,
    *,
    tier1: tuple[int, int] = (0, 4),
    tier2: int = 0,
    tier3: int = 0,
    egress: bool | None = None,
    reason: str = "",
    final_exit: str = "",
) -> dict[str, object]:
    return {
        "mode": mode,
        "tier1_alive": tier1[0],
        "tier1_total": tier1[1],
        "tier2_alive": tier2,
        "tier3_alive": tier3,
        "effective_egress_ok": egress,
        "effective_egress_reason": reason,
        "final_exit": final_exit,
    }


def keys(events) -> list[str]:
    return [event.event_key for event in events]


class TierLogTrackerTests(unittest.TestCase):
    def observe(self, tracker: TierLogTracker, values: dict[str, object], *, target: int = 25):
        return tracker.observe(values, tier3_enabled=True, tier3_target=target)

    def test_cold_start_offline_is_a_baseline_not_an_outage(self) -> None:
        tracker = TierLogTracker()

        self.assertEqual(self.observe(tracker, metrics("offline")), [])
        events = self.observe(tracker, metrics("tier3_active", tier3=1))

        self.assertEqual(keys(events), ["tier.bastion_active"])
        self.assertNotIn("previous", events[0].params)
        self.assertEqual(self.observe(tracker, metrics("tier3_active", tier3=1)), [])

    def test_every_carrying_tier_change_is_logged_with_its_predecessor(self) -> None:
        tracker = TierLogTracker()
        self.observe(tracker, metrics("tier1_healthy", tier1=(4, 4), tier3=6))

        sentinel = self.observe(tracker, metrics("tier2_active", tier2=3, tier3=6))
        bastion = self.observe(tracker, metrics("tier3_active", tier3=6))
        offline = self.observe(tracker, metrics("offline"))
        back = self.observe(tracker, metrics("tier1_degraded", tier1=(2, 4)))

        self.assertEqual(keys(sentinel), ["tier.sentinel_active"])
        self.assertEqual(sentinel[0].params["previous"], "Vanguard")
        self.assertEqual(keys(bastion), ["tier.bastion_active"])
        self.assertEqual(bastion[0].params["previous"], "Sentinel")
        self.assertEqual(keys(offline), ["tier.offline"])
        self.assertEqual(offline[0].params["previous"], "Bastion")
        self.assertEqual(offline[0].level, "CRITICAL")
        self.assertEqual(keys(back)[0], "tier.vanguard_degraded")
        self.assertNotIn("previous", back[0].params)

    def test_vanguard_health_change_keeps_the_carrying_tier(self) -> None:
        tracker = TierLogTracker()
        self.observe(tracker, metrics("tier1_healthy", tier1=(4, 4)))

        degraded = self.observe(tracker, metrics("tier1_degraded", tier1=(3, 4)))
        healthy = self.observe(tracker, metrics("tier1_healthy", tier1=(4, 4)))

        self.assertEqual(keys(degraded), ["tier.vanguard_degraded"])
        self.assertNotIn("previous", degraded[0].params)
        self.assertEqual(keys(healthy), ["tier.vanguard_healthy"])

    def test_bastion_pool_milestones_fire_once_per_crossing(self) -> None:
        tracker = TierLogTracker()
        self.observe(tracker, metrics("tier1_healthy", tier1=(4, 4)), target=10)

        ready = self.observe(tracker, metrics("tier1_healthy", tier1=(4, 4), tier3=1), target=10)
        quiet = self.observe(tracker, metrics("tier1_healthy", tier1=(4, 4), tier3=5), target=10)
        reached = self.observe(tracker, metrics("tier1_healthy", tier1=(4, 4), tier3=10), target=10)
        hover = [
            self.observe(tracker, metrics("tier1_healthy", tier1=(4, 4), tier3=count), target=10)
            for count in (9, 10, 8, 10)
        ]
        empty = self.observe(tracker, metrics("tier1_healthy", tier1=(4, 4), tier3=0), target=10)
        again = self.observe(tracker, metrics("tier1_healthy", tier1=(4, 4), tier3=10), target=10)

        self.assertEqual(keys(ready), ["tier3.pool_ready"])
        self.assertEqual(ready[0].params, {"alive_count": 1, "target_count": 10})
        self.assertEqual(quiet, [])
        self.assertEqual(keys(reached), ["tier3.pool_target_reached"])
        self.assertEqual([keys(item) for item in hover], [[], [], [], []])
        self.assertEqual(keys(empty), ["tier3.pool_empty"])
        self.assertEqual(empty[0].params, {"previous_count": 10})
        self.assertEqual(keys(again), ["tier3.pool_ready", "tier3.pool_target_reached"])

    def test_entering_bastion_does_not_repeat_the_pool_count(self) -> None:
        tracker = TierLogTracker()
        self.observe(tracker, metrics("offline"))

        events = self.observe(tracker, metrics("tier3_active", tier3=2))
        gone = self.observe(tracker, metrics("offline"))

        self.assertEqual(keys(events), ["tier.bastion_active"])
        self.assertEqual(keys(gone), ["tier.offline"])

    def test_disabled_bastion_logs_no_pool_events(self) -> None:
        tracker = TierLogTracker()

        events = tracker.observe(
            metrics("tier1_healthy", tier1=(4, 4), tier3=0), tier3_enabled=False, tier3_target=25
        )

        self.assertEqual(keys(events), ["tier.vanguard_healthy"])

    def test_egress_is_reported_ready_then_failed_then_restored(self) -> None:
        tracker = TierLogTracker()
        base = {"tier1": (4, 4), "final_exit": "tier1"}

        startup_failure = self.observe(tracker, metrics("tier1_healthy", egress=False, **base))
        ready = self.observe(tracker, metrics("tier1_healthy", egress=True, **base))
        unknown = self.observe(tracker, metrics("tier1_healthy", egress=None, **base))
        failed = self.observe(
            tracker,
            metrics("tier1_healthy", egress=False, reason="selected Gateway exit is unavailable", **base),
        )
        still_failed = self.observe(tracker, metrics("tier1_healthy", egress=False, **base))
        restored = self.observe(tracker, metrics("tier1_healthy", egress=True, **base))

        self.assertEqual(keys(startup_failure), ["tier.vanguard_healthy"])
        self.assertEqual(keys(ready), ["tier.egress_ready"])
        self.assertEqual(ready[0].params, {"exit_name": "Vanguard"})
        self.assertEqual(unknown, [])
        self.assertEqual(keys(failed), ["tier.egress_failed"])
        self.assertEqual(failed[0].params, {"reason": "selected Gateway exit is unavailable"})
        self.assertEqual(still_failed, [])
        self.assertEqual(keys(restored), ["tier.egress_restored"])

    def test_every_emitted_key_is_a_registered_key_event_with_matching_level(self) -> None:
        tracker = TierLogTracker()
        sequence = [
            metrics("offline"),
            metrics("tier3_active", tier3=3, egress=True, final_exit="tier3"),
            metrics("tier2_active", tier2=1, tier3=3, egress=False, reason="probe failed"),
            metrics("tier1_degraded", tier1=(1, 4), tier3=25, egress=True, final_exit="exit"),
            metrics("tier1_healthy", tier1=(4, 4), tier3=0),
            metrics("offline"),
        ]
        emitted = [event for values in sequence for event in self.observe(tracker, values)]

        self.assertTrue(set(MODE_EVENT_KEYS.values()) <= set(LOG_EVENT_SPECS))
        for event in emitted:
            with self.subTest(event=event.event_key):
                spec = LOG_EVENT_SPECS[event.event_key]
                self.assertTrue(spec.is_key_event)
                self.assertEqual(event.level, spec.level)
                record = LogRecord.from_mapping(
                    {
                        "source": "TESSERACT",
                        "level": event.level,
                        "event_key": event.event_key,
                        "message_raw": event.message,
                        "metadata": {"event_params": dict(event.params)},
                    }
                )
                report = build_log_event_report(record)
                self.assertTrue(report.complete)
                self.assertNotIn("—", report.detail)
                self.assertNotIn("{", report.headline + report.detail)

    def test_reports_name_the_tiers_the_user_sees(self) -> None:
        tracker = TierLogTracker()
        self.observe(tracker, metrics("tier1_healthy", tier1=(4, 4)))
        event = self.observe(tracker, metrics("tier3_active", tier3=7))[0]
        record = LogRecord.from_mapping(
            {
                "source": "TESSERACT",
                "level": event.level,
                "event_key": event.event_key,
                "message_raw": event.message,
                "metadata": {"event_params": dict(event.params)},
            }
        )

        report = build_log_event_report(record)

        self.assertEqual(report.headline, "流量由 Bastion 承载")
        self.assertIn("Bastion 可用 7", report.detail)
        self.assertIn("流量已从 Vanguard 切换过来", report.detail)

    def test_emit_writes_registered_events_to_the_logger(self) -> None:
        records: list[logging.LogRecord] = []

        class _Collect(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        logger = logging.getLogger("test-tier-log-events")
        logger.propagate = False
        handler = _Collect()
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            tracker = TierLogTracker()
            emit_tier_log_events(logger, self.observe(tracker, metrics("tier2_active", tier2=2)))
        finally:
            logger.removeHandler(handler)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].event_key, "tier.sentinel_active")
        self.assertTrue(records[0].is_key_event)
        self.assertEqual(records[0].levelname, "WARNING")


class AlertModeNotificationTests(unittest.TestCase):
    def test_alert_mode_notifications_stay_out_of_the_event_flow(self) -> None:
        for key in (
            "mode.recovery.vanguard",
            "mode.tier1_degraded",
            "mode.recovery.sentinel",
            "mode.tier2_active",
            "mode.tier2_active.backup_reminder",
            "mode.tier3_active",
            "mode.offline",
            "mode.tier2_tier3_flap",
        ):
            with self.subTest(key=key):
                spec = LOG_EVENT_SPECS[key]
                self.assertFalse(spec.is_key_event)
                self.assertTrue(spec.persist_raw)
        for key in ("tier1.node_down", "tier3.intervention_required", "kill_switch.activated"):
            with self.subTest(key=key):
                self.assertTrue(LOG_EVENT_SPECS[key].is_key_event)


if __name__ == "__main__":
    unittest.main()
