import unittest

from manager_alert_policy import (
    kill_switch_activated_event,
    kill_switch_deactivated_event,
    kill_switch_manual_clear_event,
    kill_switch_manual_override_event,
    kill_switch_unavailable_event,
    mode_transition_events,
    tier1_node_down_event,
    tier1_protocol_fallback_event,
    tier2_tier3_flap_event,
    tier3_intervention_event,
    tier3_top_nodes_listing,
)
from models import AlertType, Node


BANNER = "BANNER"


def _event_tuples(**kwargs):
    return [
        (event.alert_type, event.title, event.message)
        for event in mode_transition_events(**kwargs)
    ]


class ManagerAlertPolicyTests(unittest.TestCase):
    def test_tier3_top_nodes_listing_preserves_existing_format(self) -> None:
        nodes = [
            Node(name="one", address="one.example.com", port=443),
            Node(name="two", address="two.example.com", port=8443),
        ]

        self.assertEqual(
            tier3_top_nodes_listing(nodes),
            "one@one.example.com:443, two@two.example.com:8443",
        )
        self.assertEqual(tier3_top_nodes_listing([]), "No healthy fallback nodes")

    def test_tier1_healthy_alerts_only_after_prior_mode(self) -> None:
        self.assertEqual(
            _event_tuples(
                new_mode="tier1_healthy",
                previous_mode=None,
                metrics={},
                tier3_listing="",
                tier3_warning_banner=BANNER,
            ),
            [],
        )
        self.assertEqual(
            _event_tuples(
                new_mode="tier1_healthy",
                previous_mode="tier2_active",
                metrics={},
                tier3_listing="",
                tier3_warning_banner=BANNER,
            ),
            [
                (
                    AlertType.RECOVERY,
                    "Nodes restored",
                    "Nodes are healthy again.",
                )
            ],
        )

    def test_tier2_active_alerts_recovery_or_degradation(self) -> None:
        self.assertEqual(
            _event_tuples(
                new_mode="tier2_active",
                previous_mode="tier3_active",
                metrics={"tier2_alive": 4},
                tier3_listing="",
                tier3_warning_banner=BANNER,
            ),
            [
                (
                    AlertType.RECOVERY,
                    "Nodes restored",
                    "Available: 4.",
                )
            ],
        )
        self.assertEqual(
            _event_tuples(
                new_mode="tier2_active",
                previous_mode="tier1_degraded",
                metrics={"tier2_alive": 3},
                tier3_listing="",
                tier3_warning_banner=BANNER,
            ),
            [
                (
                    AlertType.CRITICAL,
                    "Nodes unavailable",
                    "Fallback available: 3.",
                ),
                (
                    AlertType.BACKUP_REMINDER,
                    "Degradation-triggered backup",
                    "Primary infrastructure is failing. Push critical backups now.",
                ),
            ],
        )

    def test_degraded_tier3_and_offline_messages_preserve_contract(self) -> None:
        self.assertEqual(
            _event_tuples(
                new_mode="tier1_degraded",
                previous_mode="tier1_healthy",
                metrics={"tier1_alive": 1, "tier1_total": 2},
                tier3_listing="",
                tier3_warning_banner=BANNER,
            ),
            [
                (
                    AlertType.WARNING,
                    "Nodes degraded",
                    "Available: 1/2.",
                )
            ],
        )
        self.assertEqual(
            _event_tuples(
                new_mode="tier3_active",
                previous_mode="tier2_active",
                metrics={},
                tier3_listing="node@example.com:443",
                tier3_warning_banner="BANNER\nBLOCK",
            ),
            [
                (
                    AlertType.EMERGENCY,
                    "Fallback active",
                    "Available fallback nodes: node@example.com:443. BANNER BLOCK",
                )
            ],
        )
        self.assertEqual(
            _event_tuples(
                new_mode="offline",
                previous_mode="tier3_active",
                metrics={},
                tier3_listing="",
                tier3_warning_banner=BANNER,
            ),
            [
                (
                    AlertType.EMERGENCY,
                    "No nodes available",
                    "No available nodes. BANNER",
                )
            ],
        )

    def test_mode_transition_events_set_event_groups(self) -> None:
        self.assertEqual(
            [
                event.event_group
                for event in mode_transition_events(
                    new_mode="tier2_active",
                    previous_mode="tier1_degraded",
                    metrics={"tier2_alive": 3},
                    tier3_listing="",
                    tier3_warning_banner=BANNER,
                )
            ],
            ["route_health", "backup"],
        )
        self.assertEqual(
            mode_transition_events(
                new_mode="tier3_active",
                previous_mode="tier2_active",
                metrics={},
                tier3_listing="node@example.com:443",
                tier3_warning_banner=BANNER,
            )[0].event_group,
            "emergency_fallback",
        )

    def test_direct_alert_events_set_event_groups(self) -> None:
        self.assertEqual(tier1_node_down_event(["node-a"]).event_group, "vanguard_nodes")
        self.assertEqual(
            tier1_protocol_fallback_event(
                [{"node": "node-a", "failed_protocols": ["hysteria2"], "fallback_protocol": "vless"}]
            ).event_group,
            "vanguard_nodes",
        )
        intervention = tier3_intervention_event()
        self.assertEqual(intervention.event_group, "emergency_fallback")
        self.assertNotIn("3a", intervention.message.lower())
        self.assertEqual(intervention.message, "No usable nodes in configured sources.")
        self.assertEqual(tier2_tier3_flap_event("Bastion Layer").event_group, "route_health")
        self.assertEqual(kill_switch_unavailable_event("Run as admin.", source="test").event_group, "protection")
        self.assertEqual(kill_switch_activated_event("blocked").event_group, "protection")
        self.assertEqual(kill_switch_deactivated_event().event_group, "protection")
        self.assertEqual(kill_switch_manual_override_event().event_group, "protection")
        self.assertEqual(kill_switch_manual_clear_event().event_group, "protection")


if __name__ == "__main__":
    unittest.main()
