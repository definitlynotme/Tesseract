import unittest

from tier1_rows import (
    best_latency,
    best_tier1_variant_row,
    latency_value,
    latest_text,
    tier1_aggregate_row,
    tier1_display_rows,
    tier1_variants_from_status_payload,
)


class Tier1RowsTests(unittest.TestCase):
    def test_variants_from_status_payload_formats_protocol_entries(self) -> None:
        variants = tier1_variants_from_status_payload(
            [
                {
                    "name": "edge-a",
                    "protocol_label": "VLESS",
                    "protocol_key": "vless",
                    "status": "alive",
                    "latency_ms": 88,
                    "address": "1.1.1.1",
                    "port": 443,
                    "identity": "node-a",
                    "has_uri": True,
                    "fallback_order": ["vless", "hy2"],
                    "connection": {
                        "uuid": "runtime-uuid",
                        "network": "grpc",
                    },
                },
                "bad",
            ]
        )

        self.assertEqual(len(variants), 1)
        self.assertEqual(variants[0]["endpoint"], "1.1.1.1:443")
        self.assertEqual(variants[0]["fallback_order"], ["vless", "hy2"])
        self.assertEqual(
            variants[0]["connection"],
            {"uuid": "runtime-uuid", "network": "grpc"},
        )

    def test_best_variant_uses_status_rank_before_latency(self) -> None:
        rows = [
            ["tier1", "slow-fast", "Hysteria2", "a:1", "slow", "10ms"],
            ["tier1", "alive-slow", "VLESS", "a:2", "alive", "200ms"],
        ]

        self.assertEqual(best_tier1_variant_row(rows)[1], "alive-slow")
        self.assertEqual(best_latency(rows), "10ms")
        self.assertEqual(latency_value("bad"), 999999.0)

    def test_aggregate_row_preserves_variant_metadata_and_risk(self) -> None:
        rows = [
            [
                "tier1",
                "node-vless",
                "VLESS",
                "1.1.1.1:443",
                "alive",
                "120ms",
                "2026-05-31T10:00:00",
                "runtime",
                "yes",
                "Normal",
                {"physical_node": "node-a", "protocol_key": "vless", "identity": "id-a", "has_uri": True, "fallback_order": ["vless", "hy2"], "connection": {"uuid": "runtime-uuid"}},
            ],
            [
                "tier1",
                "node-hy2",
                "Hysteria2",
                "1.1.1.1:8443",
                "slow",
                "80ms",
                "2026-05-31T11:00:00",
                "runtime",
                "--",
                "TLS mismatch",
                {"physical_node": "node-a", "protocol_key": "hy2", "identity": "id-a", "has_uri": False},
            ],
        ]

        row = tier1_aggregate_row("node-a", rows, translate=lambda text: f"tr:{text}")

        self.assertEqual(row[:10], ["tier1", "node-a", "VLESS -> Hysteria2", "1.1.1.1:443", "alive", "80ms", "2026-05-31T11:00:00", "tr:配置", "tr:是", "TLS mismatch"])
        self.assertTrue(row[10]["aggregate"])
        self.assertEqual(row[10]["fallback_order"], ["vless", "hy2"])
        self.assertEqual([variant["protocol_key"] for variant in row[10]["variants"]], ["vless", "hy2"])
        self.assertEqual(
            row[10]["variants"][0]["connection"],
            {"uuid": "runtime-uuid"},
        )

    def test_display_rows_groups_by_physical_node_but_preserves_aggregate_rows(self) -> None:
        aggregate = ["tier1", "existing", "VLESS", "a:1", "alive", "1ms", "", "", "", "", {"aggregate": True}]
        aggregate_rows = [aggregate]
        self.assertIs(tier1_display_rows(aggregate_rows), aggregate_rows)

        rows = [
            ["tier1", "a-vless", "VLESS", "a:1", "alive", "20ms", "", "", "", "Normal", {"physical_node": "node-a"}],
            ["tier1", "a-hy2", "Hysteria2", "a:2", "slow", "10ms", "", "", "", "Normal", {"physical_node": "node-a"}],
        ]
        grouped = tier1_display_rows(rows)

        self.assertEqual(len(grouped), 1)
        self.assertEqual(grouped[0][1], "node-a")
        self.assertEqual(grouped[0][2], "VLESS -> Hysteria2")

    def test_latest_text_returns_latest_non_empty_item(self) -> None:
        self.assertEqual(latest_text(["", "2026-05-31T10:00:00", "2026-05-31T11:00:00"]), "2026-05-31T11:00:00")
        self.assertEqual(latest_text(["", None]), "--")


if __name__ == "__main__":
    unittest.main()
