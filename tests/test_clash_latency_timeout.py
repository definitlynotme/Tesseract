from __future__ import annotations

import logging
import unittest

import yaml

from clash_formatter import build_full_clash_config
from models import Node
from subscription import render_subscription_route


def _node(name: str, tier: str) -> Node:
    extra = (
        {
            "physical_node": name,
            "protocol_key": "socks5",
            "protocol_label": "SOCKS5",
        }
        if tier == "tier1"
        else {}
    )
    return Node(
        name=name,
        protocol="socks5",
        address=f"{name.lower()}.example.com",
        port=1080,
        tier=tier,
        extra=extra,
    )


class ClashLatencyTimeoutTests(unittest.TestCase):
    def test_primary_healthchecks_use_configured_limit_only(self) -> None:
        parsed = yaml.safe_load(
            build_full_clash_config(
                {
                    "tier1": [_node("Tier1", "tier1")],
                    "tier2": [_node("Tier2", "tier2")],
                    "tier3": [_node("Tier3", "tier3")],
                    "exit": [],
                },
                primary_gateway_max_latency_ms=1234,
            )
        )
        groups = {group["name"]: group for group in parsed["proxy-groups"]}

        self.assertEqual(groups["TIER1-Tier1-AUTO"]["timeout"], 1234)
        self.assertEqual(groups["TIER1-AUTO"]["timeout"], 1234)
        self.assertEqual(groups["TIER2-AUTO"]["timeout"], 1234)
        self.assertEqual(groups["TIER3-AUTO"]["timeout"], 5000)
        self.assertEqual(groups["PROXY-FALLBACK"]["timeout"], 5000)

    def test_primary_healthchecks_default_to_800_for_invalid_value(self) -> None:
        parsed = yaml.safe_load(
            build_full_clash_config(
                {"tier1": [_node("Tier1", "tier1")], "tier2": [], "tier3": [], "exit": []},
                primary_gateway_max_latency_ms=99,
            )
        )
        groups = {group["name"]: group for group in parsed["proxy-groups"]}

        self.assertEqual(groups["TIER1-Tier1-AUTO"]["timeout"], 800)
        self.assertEqual(groups["TIER1-AUTO"]["timeout"], 800)

    def test_subscription_forwards_testing_latency_limit(self) -> None:
        class FakeManager:
            logger = logging.getLogger("clash-latency-timeout")
            testing_config = {"primary_gateway_max_latency_ms": 1350}
            client_config = {"mihomo_controller": {"enabled": False}, "system_proxy": {"port": 7890}}
            config = {"subscription_server": {}, "routing": {}, "geo_update": {}}

            def get_nodes_for_subscription(self, tier_id: str, limit: int | None = None) -> list[Node]:
                del limit
                return [_node("Tier1", "tier1")] if tier_id == "tier1" else []

        _content_type, payload = render_subscription_route(FakeManager(), "/clash/config")
        groups = {group["name"]: group for group in yaml.safe_load(payload)["proxy-groups"]}

        self.assertEqual(groups["TIER1-AUTO"]["timeout"], 1350)


if __name__ == "__main__":
    unittest.main()
