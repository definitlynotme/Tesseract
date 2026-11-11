import hashlib
import unittest

from models import Node, NodeStatus
from node_status_view import (
    configured_tier1_protocol_variants,
    configured_node_rows_for_tier,
    config_rows_for_tier,
    config_item_for_node_row,
    fallback_order_text,
    filtered_node_rows,
    gateway_effective_exit_row,
    gateway_layer_scope_rows,
    gateway_proxy_name_for_row,
    is_subscription_group_back_row,
    is_subscription_group_row,
    is_subscription_node_row,
    is_tier3_auto_pool_node,
    layer_scope_rows,
    layer_scope_view_state,
    layer_node_card_latency_semantics,
    layer_node_presentation_key,
    layer_node_card_protocol_parts,
    layer_node_card_protocol_summary,
    layer_node_card_state,
    layer_node_empty_action_text,
    layer_node_empty_description,
    layer_node_empty_state,
    layer_node_empty_text,
    layer_node_rows_signature,
    layer_node_status_state,
    layer_node_item_values,
    layer_node_tree_item_state,
    node_action_button_state,
    node_connection_payload,
    node_connection_snapshot,
    node_detail_values,
    node_detail_field_visible,
    node_edit_has_changes,
    node_edit_update_payload,
    node_edit_view_state,
    node_identity,
    node_protocol_detail_lines,
    node_protocol_tree_state,
    node_row_alive_count,
    node_row_display_key,
    node_row_key,
    node_row_meta,
    node_row_source_values,
    node_rows_by_display_tier,
    node_status_accent,
    node_status_snapshot,
    node_tier_group_title,
    node_tree_item_values,
    node_tree_item_state,
    parse_fallback_order_text,
    protocols_update_payload,
    rendered_connection_detail,
    row_matches_subscription_config,
    route_tier_row_groups,
    runtime_node_row_from_status,
    runtime_node_rows_from_snapshot,
    rows_for_tier,
    runtime_row_for_config_item,
    subscription_group_detail_rows,
    subscription_group_row,
    subscription_group_rows_for_tier,
)


class NodeStatusViewTests(unittest.TestCase):
    def test_node_status_snapshot_preserves_manager_contract(self) -> None:
        node = Node(
            name="alpha :: VLESS",
            protocol="vless",
            uri="vless://secret@example.com:443",
            address="example.com",
            port=443,
            tier="tier1",
            uuid="secret-uuid",
            password="secret-password",
            encryption="none",
            security="reality",
            network="tcp",
            host="host.example",
            path="/x",
            sni="sni.example",
            tls=True,
            status=NodeStatus.ALIVE,
            latency_ms=51,
            last_checked_at="2026-05-30T10:00:00+00:00",
            suspicious_reason="",
            sources=["config.yaml"],
            extra={
                "physical_node": "alpha",
                "protocol_key": "vless_reality",
                "protocol_label": "VLESS Reality",
                "fallback_order": ["vless", "hysteria2"],
                "fp": "chrome",
                "alpn": ["h2"],
                "sid": "abcd",
                "config_integrity_ok": True,
                "config_integrity_errors": [],
            },
        )
        identity = node_identity(node)

        snapshot = node_status_snapshot(node)

        self.assertEqual(snapshot["id"], hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12])
        self.assertEqual(snapshot["identity"], identity)
        self.assertEqual(snapshot["name"], "alpha :: VLESS")
        self.assertEqual(snapshot["protocol_key"], "vless_reality")
        self.assertEqual(snapshot["protocol_label"], "VLESS Reality")
        self.assertEqual(snapshot["fallback_order"], ["vless", "hysteria2"])
        self.assertEqual(snapshot["status"], "alive")
        self.assertEqual(snapshot["self_check_status"], "alive")
        self.assertEqual(
            snapshot["self_check_last_checked_at"],
            "2026-05-30T10:00:00+00:00",
        )
        self.assertEqual(snapshot["health_origin"], "self_check")
        self.assertEqual(
            snapshot["health_observed_at"],
            "2026-05-30T10:00:00+00:00",
        )
        self.assertFalse(snapshot["pending_recheck"])
        self.assertTrue(snapshot["usable"])
        self.assertEqual(snapshot["latency_ms"], 51)
        self.assertEqual(snapshot["source_count"], 1)
        self.assertTrue(snapshot["has_uri"])
        self.assertTrue(snapshot["config_integrity_ok"])
        self.assertNotIn("uri", snapshot)
        self.assertNotIn("uuid", snapshot)
        self.assertNotIn("password", snapshot)
        self.assertEqual(snapshot["connection"]["uri"], "vless://secret@example.com:443")
        self.assertEqual(snapshot["connection"]["uuid"], "secret-uuid")
        self.assertEqual(snapshot["connection"]["password"], "secret-password")
        self.assertEqual(snapshot["connection"]["fingerprint"], "chrome")
        self.assertEqual(snapshot["connection"]["alpn"], ["h2"])
        self.assertEqual(snapshot["connection"]["short_id"], "abcd")

    def test_node_status_snapshot_projects_optional_manager_runtime_health(self) -> None:
        node = Node(
            name="edge-a",
            protocol="ss",
            address="198.51.100.10",
            port=443,
            tier="tier2",
            status=NodeStatus.ALIVE,
            latency_ms=19,
            last_checked_at="2026-08-23T00:00:00+00:00",
        )
        identity = node_identity(node)

        class HealthOwner:
            @staticmethod
            def effective_node_status(_tier, _node):
                return NodeStatus.SUSPICIOUS

            @staticmethod
            def effective_node_usable(_tier, _node):
                return False

            @staticmethod
            def runtime_health_snapshot():
                return {
                    "entries": [
                        {
                            "tier": "tier2",
                            "identity": identity,
                            "proxy_name": "edge-a",
                            "source": "mihomo_stdout",
                            "observed_at": "2026-08-23T00:00:04+00:00",
                        }
                    ]
                }

        snapshot = node_status_snapshot(node, health_owner=HealthOwner())

        self.assertEqual(snapshot["status"], "suspicious")
        self.assertFalse(snapshot["usable"])
        self.assertIsNone(snapshot["latency_ms"])
        self.assertEqual(snapshot["self_check_status"], "alive")
        self.assertEqual(
            snapshot["self_check_last_checked_at"],
            "2026-08-23T00:00:00+00:00",
        )
        self.assertEqual(snapshot["health_origin"], "mihomo_stdout")
        self.assertEqual(
            snapshot["health_observed_at"],
            "2026-08-23T00:00:04+00:00",
        )
        self.assertTrue(snapshot["pending_recheck"])
        self.assertIs(node.status, NodeStatus.ALIVE)

    def test_connection_snapshot_preserves_protocol_specific_aliases(self) -> None:
        node = Node(
            name="wg",
            protocol="wireguard",
            password="private",
            tier="tier1",
            extra={
                "public-key": "public",
                "allowed_ips": ["0.0.0.0/0"],
                "reserved": [1, 2, 3],
                "mtu": 1280,
                "udp_relay_mode": "native",
                "congestion_controller": "bbr",
                "skip-cert-verify": True,
            },
        )

        connection = node_connection_snapshot(node)

        self.assertEqual(connection["private_key"], "private")
        self.assertEqual(connection["public_key"], "public")
        self.assertEqual(connection["allowed_ips"], ["0.0.0.0/0"])
        self.assertEqual(connection["reserved"], [1, 2, 3])
        self.assertEqual(connection["mtu"], 1280)
        self.assertEqual(connection["udp_relay_mode"], "native")
        self.assertEqual(connection["congestion_control"], "bbr")
        self.assertTrue(connection["skip_cert_verify"])

    def test_rendered_connection_detail_matches_desktop_display_contract(self) -> None:
        detail = rendered_connection_detail(
            {
                "port": 443,
                "uuid": "secret-uuid",
                "tls": True,
                "alpn": ["h2", ""],
                "reserved": [1, 2, 3],
                "skip_cert_verify": False,
                "uri": "vless://secret@example.com:443",
            },
            port=8443,
        )

        fields = detail["fields"]
        self.assertEqual(fields["port"], "8443")
        self.assertEqual(fields["uuid"], "secret-uuid")
        self.assertEqual(fields["tls"], "yes")
        self.assertEqual(fields["alpn"], "h2")
        self.assertEqual(fields["reserved"], "1, 2, 3")
        self.assertEqual(fields["skip_cert_verify"], "no")
        self.assertEqual(detail["uri"], "vless://secret@example.com:443")
        self.assertTrue(detail["show_details"])

    def test_rendered_connection_detail_handles_empty_or_invalid_payload(self) -> None:
        detail = rendered_connection_detail("not-a-dict")

        self.assertEqual(detail["uri"], "")
        self.assertFalse(detail["show_details"])
        self.assertTrue(all(value == "" for value in detail["fields"].values()))

    def test_node_status_accent_preserves_desktop_status_colors(self) -> None:
        self.assertEqual(node_status_accent("alive"), "#4fd2a0")
        self.assertEqual(node_status_accent("running"), "#4fd2a0")
        self.assertEqual(node_status_accent("active"), "#4fd2a0")
        self.assertEqual(node_status_accent("configured"), "#d6e2ee")
        self.assertEqual(node_status_accent("unknown"), "#d6e2ee")
        self.assertEqual(node_status_accent("slow"), "#f2c84b")
        self.assertEqual(node_status_accent("dead"), "#e86b70")
        self.assertEqual(node_status_accent("unknown", "Config Error: bad port"), "#e86b70")
        self.assertEqual(node_status_accent("unknown", "异常: timeout"), "#e86b70")
        self.assertEqual(node_status_accent("unknown", "Config Error: bad port", risk_marks_error=False), "#d6e2ee")

    def test_node_protocol_tree_state_preserves_desktop_visibility_and_rows(self) -> None:
        vless = {
            "protocol": "VLESS Reality",
            "protocol_key": "vless_reality",
            "status": "alive",
            "latency": "42 ms",
            "endpoint": "edge.example:443",
            "identity": "edge|vless",
        }
        hy2 = {
            "protocol": "Hysteria2",
            "protocol_key": "hysteria2",
            "status": "slow",
            "latency": "180 ms",
            "endpoint": "edge.example:8443",
            "identity": "edge|hy2",
        }
        row = [
            "tier1",
            "Edge",
            "VLESS",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            {"variants": [vless, "invalid", hy2]},
        ]

        state = node_protocol_tree_state(row, status_label=lambda status: f"status:{status}")

        self.assertTrue(state.visible)
        self.assertEqual(
            [item.values for item in state.rows],
            [("1", "VLESS Reality", "status:alive", "42 ms"), ("2", "Hysteria2", "status:slow", "180 ms")],
        )
        self.assertEqual([item.accent for item in state.rows], ["#4fd2a0", "#f2c84b"])
        self.assertEqual(state.rows[0].payload["protocol"], "VLESS Reality")
        self.assertEqual(state.rows[0].payload["priority"], "1")
        self.assertEqual(state.rows[0].payload["protocol_key"], "vless_reality")

    def test_node_protocol_tree_state_hides_single_or_missing_variant(self) -> None:
        single = [
            "tier1",
            "Edge",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            {"variants": [{"protocol": "VLESS", "status": "alive"}]},
        ]

        self.assertFalse(node_protocol_tree_state(single).visible)
        self.assertEqual(node_protocol_tree_state(single).rows, ())
        self.assertFalse(node_protocol_tree_state(["tier2", "Gateway"]).visible)
        self.assertFalse(node_protocol_tree_state(None).visible)

    def test_node_protocol_tree_state_marks_unchecked_configured_variants_pending(self) -> None:
        row = [
            "tier1",
            "Edge",
            "multi",
            "",
            "configured",
            "",
            "",
            "",
            "",
            "",
            {
                "variants": [
                    {"protocol": "VLESS", "status": "configured", "latency": "--"},
                    {"protocol": "Hysteria2", "status": "unknown", "latency": "--"},
                ]
            },
        ]

        state = node_protocol_tree_state(
            row,
            status_label=lambda status: f"status:{status}",
            translate=lambda text: f"translated:{text}",
        )

        self.assertEqual(
            [item.status_label for item in state.rows],
            ["translated:待自动检查", "translated:待自动检查"],
        )
        self.assertEqual([item.accent for item in state.rows], ["#d6e2ee", "#d6e2ee"])

    def test_node_protocol_tree_state_normalizes_noncanonical_failure_statuses(self) -> None:
        row = [
            "tier1",
            "Edge",
            "multi",
            "",
            "dead",
            "",
            "",
            "",
            "",
            "",
            {
                "variants": [
                    {"protocol": "Trojan", "status": "dead", "latency": "--"},
                    {"protocol": "VMess", "status": "suspicious", "latency": "--"},
                    {"protocol": "VLESS", "status": "error", "latency": "--"},
                    {"protocol": "Hysteria2", "status": "failed", "latency": "--"},
                ]
            },
        ]

        state = node_protocol_tree_state(
            row,
            status_label=lambda status: f"raw:{status}",
            translate=lambda text: {"不可用": "Unavailable"}.get(text, text),
        )

        self.assertEqual(
            [item.status_label for item in state.rows],
            ["Unavailable", "Unavailable", "Unavailable", "Unavailable"],
        )
        self.assertEqual(
            [item.accent for item in state.rows],
            ["#e86b70", "#e86b70", "#e86b70", "#e86b70"],
        )

    def test_node_protocol_detail_lines_matches_desktop_detail_contract(self) -> None:
        lines = node_protocol_detail_lines(
            {
                "protocol": "Hysteria2",
                "status": "slow",
                "latency": "180 ms",
                "endpoint": "edge.example:8443",
                "identity": "edge|hy2",
            },
            translate=lambda text: text,
            status_label=lambda status: f"status:{status}",
        )

        self.assertEqual(
            lines,
            (),
        )
        self.assertEqual(node_protocol_detail_lines("not-a-protocol"), ())

    def test_node_protocol_detail_lines_omits_repeated_table_and_endpoint_values(self) -> None:
        lines = node_protocol_detail_lines(
            {
                "priority": "2",
                "protocol": "Hysteria2",
                "protocol_key": "hysteria2",
                "status": "slow",
                "latency": "180 ms",
                "endpoint": "edge.example:8443",
                "fallback_order": ["naive", "hysteria2"],
            },
            translate=lambda text: text,
            status_label=lambda status: status,
        )

        self.assertEqual(lines, ())
        self.assertNotIn("优先级: 2", lines)
        self.assertNotIn("协议: Hysteria2", lines)
        self.assertNotIn("状态: slow", lines)
        self.assertNotIn("延迟: 180 ms", lines)
        self.assertNotIn("地址: edge.example:8443", lines)
        self.assertNotIn("回退顺序: naive -> hysteria2", lines)
        self.assertNotIn("协议标识: hysteria2", lines)

    def test_config_item_for_node_row_prefers_embedded_config_item(self) -> None:
        embedded = {"tier": "tier2", "kind": "node", "key": "embedded"}
        row = ["tier2", "Name", "vless", "example.com:443", "alive", "", "", "", "", "", {"config_item": embedded}]

        self.assertIs(config_item_for_node_row(row, []), embedded)

    def test_config_item_for_node_row_matches_tier1_physical_name(self) -> None:
        item = {"tier": "tier1", "kind": "node", "key": "VPS-A", "label": "Display-A"}
        row = [
            "tier1",
            "Display-A :: VLESS",
            "VLESS",
            "example.com:443",
            "alive",
            "",
            "",
            "",
            "",
            "",
            {"physical_node": "VPS-A"},
        ]

        self.assertIs(config_item_for_node_row(row, [item]), item)

    def test_config_item_for_node_row_uses_single_strong_address_match(self) -> None:
        good = {"tier": "tier2", "kind": "node", "label": "Gateway", "address": "198.51.100.1", "port": "443"}
        wrong_address = {"tier": "tier2", "kind": "node", "label": "Gateway", "address": "198.51.100.2", "port": "443"}
        row = [
            "tier2",
            "Gateway",
            "ss",
            "198.51.100.1:443",
            "alive",
            "",
            "",
            "",
            "",
            "",
            {"address": "198.51.100.1", "port": "443"},
        ]

        self.assertIs(config_item_for_node_row(row, [wrong_address, good]), good)

    def test_node_row_key_uses_embedded_config_item_identity(self) -> None:
        row = [
            "tier2",
            "Gateway",
            "ss",
            "198.51.100.1:443",
            "alive",
            "",
            "",
            "",
            "",
            "",
            {"config_item": {"tier": "tier2", "kind": "node", "key": "ss://gateway"}},
        ]

        self.assertEqual(node_row_key(row), "tier2|node|ss://gateway")

    def test_node_row_key_falls_back_to_physical_node_then_row_fields(self) -> None:
        row = [
            "tier1",
            "Display-A :: VLESS",
            "VLESS",
            "example.com:443",
            "alive",
            "",
            "",
            "",
            "",
            "",
            {"physical_node": "VPS-A"},
        ]
        row_without_physical = ["tier2", "Gateway", "ss", "198.51.100.1:443"]

        self.assertEqual(node_row_key(row), "tier1|VPS-A|VLESS|example.com:443")
        self.assertEqual(node_row_key(row_without_physical), "tier2|Gateway|ss|198.51.100.1:443")

    def test_node_row_display_key_uses_first_four_display_fields(self) -> None:
        row = ["tier2", "Gateway", "ss", "198.51.100.1:443", "alive"]

        self.assertEqual(node_row_display_key(row), "tier2|Gateway|ss|198.51.100.1:443")

    def test_rows_for_tier_and_route_groups_preserve_known_tiers(self) -> None:
        tier1 = ["tier1", "VPS"]
        tier3 = ["tier3", "Imported"]
        unknown = ["custom", "Other"]
        empty: list[str] = []
        rows = [tier1, tier3, unknown, empty]

        self.assertEqual(rows_for_tier(rows, "tier3"), [tier3])
        groups = route_tier_row_groups(rows)
        self.assertEqual(groups["tier1"], [tier1])
        self.assertEqual(groups["tier2"], [])
        self.assertEqual(groups["tier3"], [tier3])
        self.assertEqual(groups["exit"], [])

    def test_filtered_node_rows_preserves_tier_and_status_filters(self) -> None:
        rows = [
            ["tier1", "A", "", "", "alive"],
            ["tier1", "B", "", "", "dead"],
            ["tier2", "C", "", "", "alive"],
            ["tier3", "D"],
        ]

        self.assertEqual(filtered_node_rows(rows), rows)
        self.assertEqual(filtered_node_rows(rows, tier_filter="tier1"), rows[:2])
        self.assertEqual(filtered_node_rows(rows, status_filter="alive"), [rows[0], rows[2]])
        self.assertEqual(filtered_node_rows(rows, tier_filter="tier1", status_filter="dead"), [rows[1]])

    def test_node_rows_by_display_tier_orders_known_then_custom_tiers(self) -> None:
        custom_b = ["z-custom", "Z"]
        tier3 = ["tier3", "Imported"]
        tier1 = ["tier1", "VPS"]
        custom_a = ["a-custom", "A"]

        self.assertEqual(
            node_rows_by_display_tier([custom_b, tier3, tier1, custom_a]),
            [("tier1", [tier1]), ("tier3", [tier3]), ("a-custom", [custom_a]), ("z-custom", [custom_b])],
        )

    def test_layer_node_rows_signature_includes_nested_card_metadata(self) -> None:
        row = ["tier3", "Imported", "vless", "example.com:443", "alive", "12ms", "now", "source", "yes", "normal", {"ignored": True}]
        initial = layer_node_rows_signature("tier3", [row])
        row[10]["subscription_healthy_count"] = 4
        updated = layer_node_rows_signature("tier3", [row])

        self.assertNotEqual(initial, updated)
        self.assertNotEqual(updated, layer_node_rows_signature("exit", [row]))

    def test_node_tree_item_values_preserve_route_and_group_columns(self) -> None:
        row = ["tier2", "Gateway", "ss", "198.51.100.1:443", "alive", "12ms", "now", "source", "yes", "normal"]

        self.assertEqual(
            node_tree_item_values(row, status_label=lambda value: {"alive": "Alive"}.get(str(value), str(value))),
            ["Gateway", "ss", "Alive", "12ms", "source", "normal"],
        )

    def test_node_tree_item_state_carries_values_accent_key_and_payload(self) -> None:
        row = ["tier2", "Gateway", "ss", "198.51.100.1:443", "dead", "12ms", "now", "source", "yes", "normal"]

        state = node_tree_item_state(row, status_label=lambda value: {"dead": "Dead"}.get(str(value), str(value)))

        self.assertEqual(state.values, ["Gateway", "ss", "不可用", "12ms", "source", "normal"])
        self.assertEqual(state.accent, "#e86b70")
        self.assertEqual(state.key, "tier2|Gateway|ss|198.51.100.1:443")
        self.assertIs(state.payload, row)

    def test_layer_node_item_values_treat_configured_status_as_awaiting_automatic_check(self) -> None:
        row = ["tier3", "Imported", "vless", "example.com:443", "configured", "--", "--", "User import", "yes", "Normal"]

        self.assertEqual(
            layer_node_item_values(
                row,
                status_label=lambda value: str(value),
                translate=lambda text: {"待自动检查": "Awaiting automatic check"}.get(text, text),
            ),
            ["Imported", "vless", "Awaiting automatic check", "--", "example.com:443", "User import", "--"],
        )

    def test_layer_node_tree_item_state_uses_unknown_accent_without_check_evidence(self) -> None:
        item = {"tier": "tier3", "kind": "node", "key": "node-1"}
        row = [
            "tier3",
            "Imported",
            "vless",
            "example.com:443",
            "configured",
            "--",
            "--",
            "User import",
            "yes",
            "Normal",
            {"config_item": item},
        ]

        state = layer_node_tree_item_state(
            row,
            status_label=str,
            translate=lambda text: {"待自动检查": "Awaiting automatic check"}.get(text, text),
        )

        self.assertEqual(state.values[2], "Awaiting automatic check")
        self.assertEqual(state.accent, "#d6e2ee")
        self.assertEqual(state.key, "tier3|node|node-1")
        self.assertIs(state.payload, row)

    def test_layer_node_card_state_preserves_compact_operational_fields(self) -> None:
        item = {"tier": "tier3", "kind": "subscription", "key": "https://sub.example/all"}
        row = [
            "tier3",
            "sub.example",
            "订阅",
            "https://sub.example",
            "configured",
            "--",
            "--",
            "用户导入",
            "是",
            "--",
            {"config_item": item},
        ]

        state = layer_node_card_state(
            row,
            status_label=str,
            translate=lambda text: {"待自动检查": "Awaiting automatic check"}.get(text, text),
        )

        self.assertEqual(state.title, "sub.example")
        self.assertEqual(state.protocol, "订阅")
        self.assertEqual(state.protocol_count_label, "")
        self.assertEqual(state.summary, "订阅")
        self.assertEqual(state.protocol_tooltip, "订阅")
        self.assertEqual(state.protocol_parts, ("订阅",))
        self.assertEqual(state.primary_protocol, "订阅")
        self.assertEqual(state.endpoint_label, "https://sub.example")
        self.assertEqual(state.endpoint_tooltip, "https://sub.example")
        self.assertEqual(state.origin_label, "用户导入")
        self.assertEqual(state.origin_tooltip, "用户导入")
        self.assertEqual(state.latency_label, "")
        self.assertEqual(state.latency_semantics, "none")
        self.assertEqual(state.status_label, "Awaiting automatic check")
        self.assertEqual(state.status_state, "unknown")
        self.assertEqual(state.state_tag_label, "待检查")
        self.assertEqual(state.state_tag_state, "unknown")
        self.assertFalse(state.is_current_exit)
        self.assertEqual(state.selection_label, "")
        self.assertEqual(state.key, "tier3|subscription|https://sub.example/all")
        self.assertIs(state.payload, row)

    def test_layer_node_card_state_summarizes_multiprotocol_without_full_detail(self) -> None:
        row = [
            "tier1",
            "Edge",
            "Hysteria2 -> VLESS Reality -> Naive HTTPS",
            "198.51.100.1:443",
            "alive",
            "80ms",
            "now",
            "配置",
            "是",
            "正常",
            {
                "variants": [
                    {"protocol": "Hysteria2"},
                    {"protocol": "VLESS Reality"},
                    {"protocol": "Naive HTTPS"},
                ]
            },
        ]

        state = layer_node_card_state(
            row,
            status_label=str,
            translate=lambda text: {"协议": "protocols"}.get(text, text),
        )

        self.assertEqual(state.protocol, "Hysteria2 / VLESS Reality / Naive HTTPS")
        self.assertEqual(state.protocol_parts, ("Hysteria2", "VLESS Reality", "Naive HTTPS"))
        self.assertEqual(state.primary_protocol, "Hysteria2 +2")
        self.assertEqual(state.protocol_count_label, "")
        self.assertEqual(state.summary, "Hysteria2 / VLESS Reality / Naive HTTPS")
        self.assertEqual(state.protocol_tooltip, "Hysteria2 -> VLESS Reality -> Naive HTTPS")
        self.assertEqual(state.latency_label, "80ms")
        self.assertEqual(state.latency_semantics, "live")
        self.assertEqual(state.endpoint_label, "198.51.100.1:443")
        self.assertEqual(state.origin_label, "配置")
        self.assertEqual(state.state_tag_label, "可用")
        self.assertEqual(state.state_tag_state, "ok")

    def test_layer_node_card_state_stacks_protocol_chain_from_plain_protocol_text(self) -> None:
        row = [
            "tier1",
            "Edge",
            "Hysteria2 -> VLESS Reality -> Naive HTTPS",
            "198.51.100.1:443",
            "alive",
        ]

        state = layer_node_card_state(row, status_label=str)

        self.assertEqual(state.summary, "Hysteria2 / VLESS Reality / Naive HTTPS")
        self.assertEqual(state.protocol_tooltip, "Hysteria2 -> VLESS Reality -> Naive HTTPS")
        self.assertEqual(state.protocol_parts, ("Hysteria2", "VLESS Reality", "Naive HTTPS"))

    def test_layer_node_card_state_marks_runtime_failures_for_scan(self) -> None:
        row = ["tier2", "Gateway", "ss", "198.51.100.1:443", "dead", "timeout", "now", "source", "yes", "Config Error"]

        state = layer_node_card_state(row, status_label=lambda value: f"status:{value}")

        self.assertEqual(state.status_label, "不可用")
        self.assertEqual(state.status_state, "error")
        self.assertEqual(state.state_tag_label, "不可用")
        self.assertEqual(state.state_tag_state, "error")
        self.assertEqual(state.protocol_count_label, "")
        self.assertEqual(state.summary, "ss")
        self.assertEqual(state.latency_label, "timeout")
        self.assertEqual(state.latency_semantics, "last")
        self.assertEqual(state.key, "tier2|Gateway|ss|198.51.100.1:443")

    def test_layer_node_card_state_labels_gateway_selection_without_claiming_runtime_health(self) -> None:
        row = [
            "exit",
            "Selected Gateway",
            "vless",
            "198.51.100.8:443",
            "dead",
            "timeout",
            "now",
            "source",
            "yes",
            "Config Error",
            {"gateway_effective": True},
        ]

        state = layer_node_card_state(
            row,
            status_label=str,
            translate=lambda text: {"当前": "Current"}.get(text, text),
        )

        self.assertEqual(state.selection_label, "Current")
        self.assertEqual(state.status_state, "error")
        self.assertEqual(state.status_label, "不可用")
        self.assertTrue(state.is_current_exit)
        self.assertEqual(state.state_tag_label, "Current · 故障")
        self.assertEqual(state.state_tag_state, "error")

    def test_layer_node_card_state_keeps_current_exit_in_one_truthful_status_tag(self) -> None:
        cases = (
            ("alive", "当前", "ok", "42 ms"),
            ("slow", "当前 · 慢速", "warning", "42 ms"),
            ("configured", "当前 · 待检查", "unknown", ""),
        )
        for status, expected_label, expected_state, expected_latency in cases:
            with self.subTest(status=status):
                row = [
                    "exit",
                    "Selected",
                    "vless",
                    "198.51.100.8:443",
                    status,
                    "42",
                    "",
                    "source",
                    "yes",
                    "",
                    {"gateway_effective": True},
                ]

                state = layer_node_card_state(row, status_label=str, translate=lambda text: text)

                self.assertTrue(state.is_current_exit)
                self.assertEqual(state.state_tag_label, expected_label)
                self.assertEqual(state.state_tag_state, expected_state)
                self.assertEqual(state.latency_label, expected_latency)

    def test_layer_node_card_protocol_summary_uses_raw_protocol_for_single_protocol(self) -> None:
        row = ["tier2", "Gateway", "ss", "198.51.100.1:443", "alive"]

        self.assertEqual(layer_node_card_protocol_summary(row), ("ss", "", "ss"))
        self.assertEqual(layer_node_card_protocol_parts(row), ("ss",))

    def test_layer_node_card_latency_semantics_are_strict(self) -> None:
        self.assertEqual(
            layer_node_card_latency_semantics(enabled=True, status_state="ok", latency_label="12 ms"),
            "live",
        )
        self.assertEqual(
            layer_node_card_latency_semantics(enabled=True, status_state="error", latency_label="928 ms"),
            "last",
        )
        self.assertEqual(
            layer_node_card_latency_semantics(enabled=False, status_state="draft", latency_label="928 ms"),
            "none",
        )
        self.assertEqual(
            layer_node_card_latency_semantics(enabled=True, status_state="ok", latency_label=""),
            "none",
        )
        self.assertEqual(
            layer_node_card_latency_semantics(enabled=True, status_state="unknown", latency_label="12 ms"),
            "none",
        )

    def test_layer_node_card_state_hides_latency_without_runtime_evidence(self) -> None:
        row = ["tier2", "Pending", "ss", "198.51.100.1:443", "configured", "41"]

        state = layer_node_card_state(row, status_label=str, translate=lambda text: text)

        self.assertEqual(state.primary_protocol, "ss")
        self.assertEqual(state.state_tag_label, "待检查")
        self.assertEqual(state.state_tag_state, "unknown")
        self.assertEqual(state.latency_label, "")
        self.assertEqual(state.latency_semantics, "none")

    def test_node_detail_field_visibility_hides_default_noise(self) -> None:
        translate = lambda text: {
            "配置": "Settings",
            "用户导入": "User Imported",
            "正常": "Normal",
        }.get(text, text)

        self.assertFalse(node_detail_field_visible("latency", "--", translate=translate))
        self.assertTrue(node_detail_field_visible("latency", "88ms", translate=translate))
        self.assertFalse(node_detail_field_visible("source", "Settings", translate=translate))
        self.assertFalse(node_detail_field_visible("source", "User Imported", translate=translate))
        self.assertTrue(node_detail_field_visible("source", "2", translate=translate))
        self.assertFalse(node_detail_field_visible("risk", "Normal", translate=translate))
        self.assertTrue(node_detail_field_visible("risk", "TLS mismatch", translate=translate))

    def test_layer_node_status_state_groups_operational_states(self) -> None:
        self.assertEqual(layer_node_status_state("alive"), "ok")
        self.assertEqual(layer_node_status_state("configured"), "unknown")
        self.assertEqual(layer_node_status_state("slow"), "warning")
        self.assertEqual(layer_node_status_state("dead"), "error")
        self.assertEqual(layer_node_status_state("error"), "error")
        self.assertEqual(layer_node_status_state("failed"), "error")
        self.assertEqual(layer_node_status_state("unknown"), "unknown")

    def test_layer_node_empty_state_uses_layer_specific_translatable_copy(self) -> None:
        translate = lambda text: f"t:{text}"

        expected = {
            "tier1": (
                "t:没有可用节点",
                "t:添加节点或订阅后，可在这里查看状态与配置。",
                "t:添加单节点或订阅",
            ),
            "tier2": (
                "t:没有可用节点",
                "t:添加节点或订阅后，可在这里查看状态与配置。",
                "t:添加单节点或订阅",
            ),
            "tier3": (
                "t:暂无用户自定义 Bastion 来源",
                "t:添加 Bastion 来源后，订阅与节点会显示在这里。",
                "t:添加 Bastion 来源",
            ),
            "exit": (
                "t:暂无出口节点",
                "t:导入出口节点后，可在这里选择当前 Gateway。",
                "t:导入出口节点",
            ),
        }
        for tier, copy in expected.items():
            with self.subTest(tier=tier):
                state = layer_node_empty_state(tier, translate=translate)
                self.assertEqual(
                    (state.title, state.description, state.action_text),
                    copy,
                )
                self.assertEqual(
                    layer_node_empty_text(tier, translate=translate),
                    copy[0],
                )
                self.assertEqual(
                    layer_node_empty_description(tier, translate=translate),
                    copy[1],
                )
                self.assertEqual(
                    layer_node_empty_action_text(tier, translate=translate),
                    copy[2],
                )

    def test_node_tier_group_title_counts_alive_and_slow_rows(self) -> None:
        rows = [
            ["tier1", "A", "", "", "alive"],
            ["tier1", "B", "", "", "slow"],
            ["tier1", "C", "", "", "dead"],
        ]

        self.assertEqual(node_row_alive_count(rows), 2)
        self.assertEqual(
            node_tier_group_title(
                "tier1",
                rows,
                tier_label=lambda tier: {"tier1": "Tier1"}.get(str(tier), str(tier)),
                translate=lambda text: {"可用": "available"}.get(text, text),
            ),
            "Tier1  2/3 available",
        )

    def test_node_detail_values_preserve_selected_node_fields(self) -> None:
        row = ["tier3", "Imported", "vless", "example.com:443", "configured", "--", "--", "User import", "yes", "Normal"]

        self.assertEqual(
            node_detail_values(
                row,
                tier_label=lambda tier: {"tier3": "Tier3"}.get(str(tier), str(tier)),
                status_label=lambda value: str(value),
                translate=lambda text: {"待自动检查": "Awaiting automatic check"}.get(text, text),
            ),
            {
                "tier": "Tier3",
                "name": "Imported",
                "protocol": "vless",
                "address": "example.com:443",
                "status": "Awaiting automatic check",
                "latency": "--",
                "last_checked": "Awaiting automatic check",
                "source": "User import",
                "subscription": "yes",
                "risk": "--",
            },
        )

    def test_disabled_subscription_group_detail_uses_disabled_not_draft_status(self) -> None:
        row = subscription_group_row(
            {
                "tier": "tier2",
                "kind": "subscription",
                "label": "Provider",
                "url": "https://provider.example/sub",
                "enabled": False,
            },
            [],
            translate=lambda text: text,
        )

        detail = node_detail_values(
            row,
            tier_label=str,
            status_label=str,
            translate=lambda text: text,
        )
        card = layer_node_card_state(row, status_label=str, translate=lambda text: text)

        self.assertEqual(detail["status"], "已禁用")
        self.assertEqual(card.action_label, "管理")

    def test_node_connection_payload_uses_row_meta_connection_and_port(self) -> None:
        connection = {"uuid": "secret"}
        row = ["tier2", "Gateway", "ss", "198.51.100.1:443", "alive", "", "", "", "", "", {"connection": connection, "port": "443"}]

        self.assertEqual(node_connection_payload(row), (connection, "443"))
        self.assertEqual(node_connection_payload(["tier2", "Gateway"]), ({}, None))

    def test_node_edit_view_state_preserves_tier1_multiprotocol_fields(self) -> None:
        item = {
            "tier": "tier1",
            "kind": "node",
            "editable": True,
            "label": "Edge",
            "address": "198.51.100.1",
            "port": "443",
            "vless_link": "vless://edge",
            "protocols": {"vless_reality": {}, "hysteria2": {}},
            "fallback_order": ["hysteria2", "vless_reality"],
        }
        row = ["tier1", "Edge", "VLESS", "198.51.100.1:443", "alive", "", "", "", "", "", {"identity": "edge"}]

        state = node_edit_view_state(item, row, translate=lambda text: text)

        self.assertTrue(state.editable)
        self.assertTrue(state.multiprotocol)
        self.assertEqual(state.visible_fields, frozenset({"name", "address", "port"}))
        self.assertEqual(state.editable_fields, frozenset({"name", "address", "port"}))
        self.assertEqual(
            state.original_values,
            {
                "name": "Edge",
                "address": "198.51.100.1",
                "port": "443",
                "fallback_order": "hysteria2 -> vless_reality",
                "protocols": {"vless_reality": {}, "hysteria2": {}},
            },
        )
        self.assertTrue(state.can_delete)
        self.assertEqual(state.delete_label, "删除")
        self.assertEqual(state.message, "")

    def test_node_edit_view_state_treats_legacy_tier1_links_as_multiprotocol(self) -> None:
        item = {
            "tier": "tier1",
            "kind": "node",
            "editable": True,
            "label": "Edge",
            "address": "198.51.100.1",
            "port": "443",
            "vless_link": "vless://edge",
            "hysteria2_link": "hysteria2://edge",
            "protocols": {},
            "fallback_order": ["hysteria2", "vless_reality"],
        }

        state = node_edit_view_state(item, None, translate=lambda text: text)

        self.assertTrue(state.multiprotocol)
        self.assertEqual(state.visible_fields, frozenset({"name", "address", "port"}))
        self.assertEqual(state.editable_fields, frozenset({"name", "address", "port"}))
        self.assertEqual(
            state.original_values,
            {
                "name": "Edge",
                "address": "198.51.100.1",
                "port": "443",
                "fallback_order": "hysteria2 -> vless_reality",
                "protocols": {},
            },
        )

    def test_node_edit_view_state_hides_empty_legacy_links_for_single_protocol_nodes(self) -> None:
        state = node_edit_view_state(
            {
                "tier": "tier1",
                "kind": "node",
                "editable": True,
                "label": "Edge",
                "address": "198.51.100.1",
                "port": "443",
                "vless_link": "vless://edge",
                "hysteria2_link": "",
                "naive_link": "",
                "protocols": {},
            },
            None,
            translate=lambda text: text,
        )

        self.assertFalse(state.multiprotocol)
        self.assertEqual(state.visible_fields, frozenset({"name", "address", "port", "vless_link"}))
        self.assertEqual(state.editable_fields, frozenset({"name", "address", "port", "vless_link"}))
        self.assertEqual(
            state.original_values,
            {
                "name": "Edge",
                "address": "198.51.100.1",
                "port": "443",
                "vless_link": "vless://edge",
            },
        )

        no_link_state = node_edit_view_state(
            {
                "tier": "tier1",
                "kind": "node",
                "editable": True,
                "label": "Bare",
                "address": "203.0.113.8",
                "port": "443",
                "protocols": {},
            },
            None,
            translate=lambda text: text,
        )

        self.assertEqual(no_link_state.visible_fields, frozenset({"name", "address", "port"}))
        self.assertEqual(no_link_state.editable_fields, frozenset({"name", "address", "port"}))
        self.assertNotIn("vless_link", no_link_state.original_values)
        self.assertNotIn("hysteria2_link", no_link_state.original_values)
        self.assertNotIn("naive_link", no_link_state.original_values)

    def test_node_edit_view_state_preserves_imported_node_and_subscription_actions(self) -> None:
        imported = {
            "tier": "tier3",
            "kind": "node",
            "editable": True,
            "label": "Imported",
            "uri": "vless://imported",
            "address": "203.0.113.9",
            "port": "443",
        }
        imported_state = node_edit_view_state(imported, None, translate=lambda text: text)

        self.assertTrue(imported_state.multiprotocol)
        self.assertEqual(imported_state.visible_fields, frozenset({"name", "address", "port", "uri"}))
        self.assertEqual(imported_state.editable_fields, frozenset({"name", "uri"}))
        self.assertEqual(imported_state.values["address"], "203.0.113.9")
        self.assertEqual(imported_state.values["port"], "443")
        self.assertEqual(
            imported_state.original_values,
            {
                "name": "Imported",
                "uri": "vless://imported",
                "protocols": {"": {"uri": "vless://imported"}},
            },
        )
        self.assertEqual(imported_state.delete_label, "删除")
        self.assertEqual(imported_state.message, "")

        subscription_state = node_edit_view_state(
            {"tier": "tier3", "kind": "subscription", "editable": False, "label": "Provider"},
            None,
            translate=lambda text: text,
        )

        self.assertFalse(subscription_state.editable)
        self.assertEqual(subscription_state.visible_fields, frozenset())
        self.assertEqual(subscription_state.editable_fields, frozenset())
        self.assertTrue(subscription_state.can_delete)
        self.assertEqual(subscription_state.delete_label, "删除")
        self.assertEqual(subscription_state.message, "")

        editable_subscription_state = node_edit_view_state(
            {
                "tier": "tier3",
                "kind": "subscription",
                "editable": True,
                "label": "Provider",
                "url": "https://sub.example/list?token=secret",
            },
            None,
            translate=lambda text: text,
        )

        self.assertEqual(editable_subscription_state.visible_fields, frozenset({"uri"}))
        self.assertEqual(editable_subscription_state.editable_fields, frozenset({"uri"}))
        self.assertEqual(
            editable_subscription_state.original_values,
            {"uri": "https://sub.example/list?token=secret"},
        )
        self.assertTrue(editable_subscription_state.can_toggle_enabled)
        self.assertTrue(editable_subscription_state.can_delete)
        self.assertEqual(editable_subscription_state.delete_label, "删除")
        self.assertEqual(editable_subscription_state.message, "")

    def test_node_edit_view_state_preserves_dynamic_row_read_only_message(self) -> None:
        state = node_edit_view_state(None, ["tier3", "Auto", "vless"], translate=lambda text: text)

        self.assertEqual(state.tier, "tier3")
        self.assertFalse(state.editable)
        self.assertEqual(state.visible_fields, frozenset())
        self.assertEqual(state.editable_fields, frozenset())
        self.assertFalse(state.can_delete)
        self.assertEqual(state.delete_label, "删除")
        self.assertEqual(state.message, "只读")

    def test_node_edit_view_state_empty_selection_has_no_dynamic_warning(self) -> None:
        state = node_edit_view_state(None, None, translate=lambda text: text)

        self.assertEqual(state.tier, "")
        self.assertFalse(state.editable)
        self.assertEqual(state.visible_fields, frozenset())
        self.assertEqual(state.editable_fields, frozenset())
        self.assertFalse(state.can_delete)
        self.assertEqual(state.message, "")

    def test_node_edit_has_changes_compares_trimmed_visible_values(self) -> None:
        self.assertFalse(node_edit_has_changes({"name": "Edge"}, {"name": " Edge "}))
        self.assertTrue(node_edit_has_changes({"name": "Edge"}, {"name": "Gateway"}))
        self.assertFalse(node_edit_has_changes({"hidden": "old"}, {}))
        self.assertFalse(node_edit_has_changes({"protocols": {"vless": {"uri": "vless://edge"}}}, {"protocols": {"vless": {"uri": "vless://edge"}}}))
        self.assertTrue(node_edit_has_changes({"protocols": {"vless": {}}}, {"protocols": {"hysteria2": {}}}))

    def test_node_edit_update_payload_preserves_tier_specific_save_contract(self) -> None:
        self.assertEqual(
            node_edit_update_payload(
                {"tier": "tier1"},
                {
                    "name": " Edge ",
                    "address": " 198.51.100.1 ",
                    "port": " 443 ",
                    "vless_link": " vless://edge ",
                },
            ),
            {
                "name": "Edge",
                "address": "198.51.100.1",
                "port": "443",
                "vless_link": "vless://edge",
            },
        )
        self.assertEqual(
            node_edit_update_payload(
                {"tier": "tier3"},
                {
                    "name": " Imported ",
                    "address": "203.0.113.9",
                    "port": "443",
                    "uri": " vless://imported ",
                },
            ),
            {"name": "Imported", "uri": "vless://imported"},
        )
        self.assertEqual(
            node_edit_update_payload(
                {"tier": "tier3", "kind": "subscription"},
                {"uri": " https://new.example/list "},
            ),
            {"url": "https://new.example/list"},
        )
        self.assertIsNone(node_edit_update_payload({"tier": "unknown"}, {"name": "Auto"}))

    def test_node_edit_update_payload_parses_tier1_fallback_order(self) -> None:
        self.assertEqual(
            node_edit_update_payload(
                {"tier": "tier1"},
                {"fallback_order": "hysteria2 -> naive, vless_reality"},
            ),
            {"fallback_order": ["hysteria2", "naive", "vless_reality"]},
        )

    def test_node_edit_update_payload_preserves_protocols_draft(self) -> None:
        self.assertEqual(
            node_edit_update_payload(
                {"tier": "tier1"},
                {
                    "protocols": {
                        " vless_reality ": {" uri ": "vless://edge"},
                        "hysteria2": "hy2://edge",
                        "disabled": False,
                        "": {"uri": "ignored"},
                    }
                },
            ),
            {
                "protocols": {
                    "vless_reality": {"uri": "vless://edge"},
                    "hysteria2": {"uri": "hy2://edge"},
                }
            },
        )

    def test_protocols_update_payload_drops_disabled_and_empty_keys(self) -> None:
        self.assertEqual(
            protocols_update_payload({"vless": {"uri": "u"}, "off": False, "": {"uri": "x"}, "raw": "ss://node"}),
            {"vless": {"uri": "u"}, "raw": {"uri": "ss://node"}},
        )

    def test_fallback_order_text_helpers_accept_common_user_input(self) -> None:
        self.assertEqual(fallback_order_text(["hy2", "vless"]), "hy2 -> vless")
        self.assertEqual(parse_fallback_order_text("hy2\nvless, naive"), ["hy2", "vless", "naive"])

    def test_configured_tier1_protocol_variants_follow_fallback_order(self) -> None:
        variants = configured_tier1_protocol_variants(
            {
                "tier": "tier1",
                "kind": "node",
                "label": "Edge",
                "detail": "198.51.100.1:443",
                "protocols": {
                    "vless_reality": {"pbk": "public-key"},
                    "hysteria2": {"password": "hy-secret"},
                    "naive": {"extra": {"nested": ["one", "two"]}},
                },
                "fallback_order": ["hysteria2", "naive"],
            }
        )

        self.assertEqual([item["protocol_key"] for item in variants], ["hysteria2", "naive", "vless_reality"])
        self.assertEqual([item["protocol"] for item in variants], ["Hysteria2", "Naive HTTPS", "VLESS Reality"])
        self.assertEqual(variants[0]["config"], {"password": "hy-secret"})
        self.assertEqual(
            variants[1]["config"],
            {"extra": {"nested": ["one", "two"]}},
        )

        aliased = configured_tier1_protocol_variants(
            {
                "tier": "tier1",
                "kind": "node",
                "protocols": {
                    "vless_reality": {},
                    "hysteria2": {},
                    "naive": {},
                },
                "fallback_order": ["reality", "hy2", "naiveproxy"],
            }
        )
        self.assertEqual(
            [item["protocol_key"] for item in aliased],
            ["vless_reality", "hysteria2", "naive"],
        )

    def test_configured_tier1_rows_include_protocol_variants_when_core_is_stopped(self) -> None:
        rows = configured_node_rows_for_tier(
            "tier1",
            [
                {
                    "tier": "tier1",
                    "kind": "node",
                    "label": "Edge",
                    "address": "198.51.100.1",
                    "port": "443",
                    "protocol": "hysteria2, naive, vless_reality",
                    "protocols": {"vless_reality": {}, "hysteria2": {}, "naive": {}},
                    "fallback_order": ["hysteria2", "naive", "vless_reality"],
                }
            ],
            translate=lambda text: text,
        )

        self.assertEqual(rows[0][10]["fallback_order"], ["hysteria2", "naive", "vless_reality"])
        self.assertEqual([item["protocol_key"] for item in rows[0][10]["variants"]], ["hysteria2", "naive", "vless_reality"])

    def test_node_action_button_state_only_controls_save_and_delete(self) -> None:
        item = {"kind": "node", "editable": True}

        state = node_action_button_state(
            item,
            edit_has_changes=True,
            busy_tasks=set(),
        )

        self.assertTrue(state.can_save)
        self.assertTrue(state.can_delete)

        busy_state = node_action_button_state(
            item,
            edit_has_changes=True,
            busy_tasks={"save-node-config", "delete-config-item"},
        )

        self.assertFalse(busy_state.can_save)
        self.assertFalse(busy_state.can_delete)

        read_only_state = node_action_button_state(
            {"kind": "node", "editable": False},
            edit_has_changes=False,
            busy_tasks=set(),
        )

        self.assertFalse(read_only_state.can_save)
        self.assertTrue(read_only_state.can_delete)

    def test_runtime_node_row_from_status_preserves_snapshot_row_contract(self) -> None:
        node = {
            "name": "Edge :: VLESS",
            "protocol": "vless",
            "protocol_label": "VLESS Reality",
            "address": "198.51.100.1",
            "port": 443,
            "status": "alive",
            "latency_ms": 42,
            "last_checked_at": "2026-05-30T10:00:00+00:00",
            "source_count": 2,
            "has_uri": True,
            "identity": "edge|vless",
            "physical_node": "Edge",
            "protocol_key": "vless_reality",
            "fallback_order": ["vless_reality", "hysteria2"],
            "source": "https://sub.example/list",
            "sources": ["https://sub.example/list", "config.yaml"],
            "observation_origin": "persisted_cache",
            "observed_at": "2026-05-30T10:01:00+00:00",
            "connection": {"uuid": "secret"},
            "protocols": [
                {"protocol_key": "vless_reality", "status": "alive", "latency_ms": 42},
            ],
        }

        row = runtime_node_row_from_status("tier1", node, translate=lambda text: {"是": "Yes", "否": "No", "正常": "Normal"}.get(text, text))

        self.assertEqual(row[:10], ["tier1", "Edge :: VLESS", "VLESS Reality", "198.51.100.1:443", "alive", "42", "2026-05-30T10:00:00+00:00", "2", "Yes", "Normal"])
        self.assertEqual(row[10]["identity"], "edge|vless")
        self.assertEqual(row[10]["physical_node"], "Edge")
        self.assertEqual(row[10]["protocol_key"], "vless_reality")
        self.assertEqual(row[10]["fallback_order"], ["vless_reality", "hysteria2"])
        self.assertEqual(row[10]["source"], "https://sub.example/list")
        self.assertEqual(row[10]["sources"], ["https://sub.example/list", "config.yaml"])
        self.assertEqual(row[10]["observation_origin"], "persisted_cache")
        self.assertEqual(row[10]["observed_at"], "2026-05-30T10:01:00+00:00")
        self.assertEqual(row[10]["static_risk"], "Normal")
        self.assertEqual(row[10]["connection"], {"uuid": "secret"})
        self.assertEqual(row[10]["variants"][0]["protocol_key"], "vless_reality")

    def test_runtime_node_row_keeps_effective_and_self_check_health_separate(self) -> None:
        row = runtime_node_row_from_status(
            "tier2",
            {
                "name": "Edge",
                "protocol": "ss",
                "address": "198.51.100.11",
                "port": 443,
                "status": "suspicious",
                "self_check_status": "alive",
                "last_checked_at": "2026-08-23T00:00:00+00:00",
                "self_check_last_checked_at": "2026-08-23T00:00:00+00:00",
                "health_origin": "mihomo_stdout",
                "health_observed_at": "2026-08-23T00:00:04+00:00",
                "pending_recheck": True,
                "usable": False,
            },
            translate=lambda text: text,
        )

        self.assertEqual(row[4], "suspicious")
        self.assertEqual(row[6], "2026-08-23T00:00:00+00:00")
        self.assertEqual(row[10]["self_check_status"], "alive")
        self.assertEqual(
            row[10]["self_check_last_checked_at"],
            "2026-08-23T00:00:00+00:00",
        )
        self.assertEqual(row[10]["health_origin"], "mihomo_stdout")
        self.assertEqual(
            row[10]["health_observed_at"],
            "2026-08-23T00:00:04+00:00",
        )
        self.assertTrue(row[10]["pending_recheck"])
        self.assertFalse(row[10]["usable"])

    def test_runtime_node_row_from_status_reports_integrity_risk_before_normal_fallback(self) -> None:
        row = runtime_node_row_from_status(
            "tier2",
            {
                "name": "Gateway",
                "protocol": "ss",
                "address": "198.51.100.2",
                "port": 8388,
                "config_integrity_ok": False,
                "config_integrity_errors": ["bad cipher", "missing password", "extra detail"],
            },
            translate=lambda text: {"配置异常": "Config Error", "否": "No", "正常": "Normal"}.get(text, text),
        )

        self.assertEqual(row[8], "No")
        self.assertEqual(row[9], "Config Error")
        self.assertIs(row[10]["config_integrity_ok"], False)
        self.assertEqual(row[10]["static_risk"], "Config Error")
        self.assertEqual(row[10]["config_integrity_error_count"], 3)
        self.assertNotIn("config_integrity_errors", row[10])
        self.assertEqual(row[10]["runtime_issue_kind"], "configuration_invalid")

    def test_runtime_node_row_localizes_latency_limit_reason_code(self) -> None:
        row = runtime_node_row_from_status(
            "tier1",
            {
                "name": "Slow Edge",
                "protocol": "ss",
                "address": "198.51.100.3",
                "port": 8388,
                "status": "dead",
                "latency_ms": 801,
                "suspicious_reason": "latency_limit_exceeded",
            },
            translate=lambda text: {
                "延迟超过上限": "Latency over limit",
                "正常": "Normal",
                "否": "No",
            }.get(text, text),
        )

        self.assertEqual(row[4], "dead")
        self.assertEqual(row[5], "801")
        self.assertEqual(row[9], "Latency over limit")

    def test_runtime_node_row_localizes_known_dns_resolution_failures(self) -> None:
        reasons = (
            "tcp_dns_failed",
            "[Errno 11001] getaddrinfo failed",
            "[Errno 11003] A nonrecoverable error occurred during a database lookup",
            "[WinError 11001] getaddrinfo failed",
            "[WinError 11003] A nonrecoverable error occurred during a database lookup",
            "socket.gaierror: [Errno -2] Name or service not known",
            "getaddrinfo temporarily failed",
        )

        for reason in reasons:
            with self.subTest(reason=reason):
                row = runtime_node_row_from_status(
                    "tier3",
                    {
                        "name": "Bastion Node",
                        "protocol": "vless",
                        "address": "node.invalid",
                        "port": 443,
                        "status": "dead",
                        "suspicious_reason": reason,
                    },
                    translate=lambda text: {
                        "域名解析失败": "DNS resolution failed",
                        "正常": "Normal",
                        "否": "No",
                    }.get(text, text),
                )

                self.assertEqual(row[9], "DNS resolution failed")
                self.assertEqual(
                    row[10]["runtime_issue_kind"],
                    "dns_resolution_failed",
                )

    def test_runtime_node_row_never_exposes_unknown_failure_reason_verbatim(self) -> None:
        reasons = (
            "socket error 11001",
            "name or service changed",
            "tcp_connect_failed",
        )

        for reason in reasons:
            with self.subTest(reason=reason):
                row = runtime_node_row_from_status(
                    "tier3",
                    {
                        "name": "Bastion Node",
                        "protocol": "vless",
                        "address": "198.51.100.8",
                        "port": 443,
                        "status": "dead",
                        "suspicious_reason": reason,
                    },
                    translate=lambda text: {
                        "域名解析失败": "DNS resolution failed",
                        "节点不可用": "Node unavailable",
                        "正常": "Normal",
                        "否": "No",
                    }.get(text, text),
                )

                self.assertEqual(row[9], "Node unavailable")
                self.assertNotIn(reason.strip(), row[9])
                self.assertEqual(row[10]["runtime_issue_kind"], "node_unavailable")

    def test_runtime_node_row_exposes_only_bounded_failure_categories(self) -> None:
        cases = (
            (
                "connection timed out",
                "Connection timed out",
                "connection_timeout",
            ),
            (
                "[WinError 10060] A connection attempt failed because the connected party did not properly respond",
                "Connection timed out",
                "connection_timeout",
            ),
            (
                "[Errno 111] Connection refused",
                "Connection refused",
                "connection_refused",
            ),
            (
                "[WinError 10061] No connection could be made because the target machine actively refused it",
                "Connection refused",
                "connection_refused",
            ),
            (
                "TLS handshake failed: certificate verify failed",
                "TLS connection failed",
                "tls_failed",
            ),
            (
                "ssl.SSLCertVerificationError: x509 certificate has expired",
                "TLS connection failed",
                "tls_failed",
            ),
        )
        translations = {
            "节点不可用": "Node unavailable",
            "连接超时": "Connection timed out",
            "连接被拒绝": "Connection refused",
            "TLS 连接失败": "TLS connection failed",
            "正常": "Normal",
            "否": "No",
            "不可用": "Unavailable",
        }

        for reason, expected_text, expected_kind in cases:
            with self.subTest(reason=reason):
                row = runtime_node_row_from_status(
                    "tier1",
                    {
                        "name": "Edge",
                        "protocol": "ss",
                        "address": "198.51.100.10",
                        "port": 443,
                        "status": "dead",
                        "suspicious_reason": reason,
                    },
                    translate=lambda text: translations.get(text, text),
                )

                self.assertEqual(row[4], "dead")
                self.assertEqual(row[9], expected_text)
                self.assertEqual(row[10]["runtime_issue_kind"], expected_kind)
                self.assertNotIn(reason, repr(row))

                card = layer_node_card_state(
                    row,
                    status_label=lambda status: str(status),
                    translate=lambda text: translations.get(text, text),
                )
                self.assertEqual(card.state_tag_label, "Unavailable")
                self.assertEqual(card.state_tag_state, "error")
                visible_card_text = "\n".join(
                    (
                        card.title,
                        card.summary,
                        card.protocol_tooltip,
                        card.status_label,
                        card.state_tag_label,
                        card.meta_label,
                    )
                )
                self.assertNotIn(reason, visible_card_text)
                self.assertNotIn(expected_text, visible_card_text)

                detail = node_detail_values(
                    row,
                    tier_label=lambda tier: str(tier),
                    status_label=lambda status: str(status),
                    translate=lambda text: translations.get(text, text),
                )
                self.assertEqual(detail["risk"], expected_text)
                self.assertNotIn(reason, repr(detail))

    def test_runtime_node_row_normalizes_bare_error_status_without_reason(self) -> None:
        for status in ("dead", "suspicious", "error", "failed"):
            with self.subTest(status=status):
                row = runtime_node_row_from_status(
                    "tier1",
                    {
                        "name": "Edge",
                        "protocol": "ss",
                        "address": "198.51.100.9",
                        "port": 443,
                        "status": status,
                    },
                    translate=lambda text: {
                        "节点不可用": "Node unavailable",
                        "正常": "Normal",
                        "否": "No",
                    }.get(text, text),
                )

                self.assertEqual(row[9], "Node unavailable")
                self.assertEqual(row[10]["runtime_issue_kind"], "node_unavailable")

    def test_runtime_node_rows_from_snapshot_collects_tiers_in_payload_order(self) -> None:
        rows = runtime_node_rows_from_snapshot(
            {
                "tiers": {
                    "tier2": {"nodes": [{"name": "A", "protocol": "ss", "address": "a.example", "port": 1}]},
                    "tier3": {"nodes": [{"name": "B", "protocol": "vless", "address": "b.example", "port": 2}]},
                }
            },
            translate=lambda text: {"否": "No", "正常": "Normal"}.get(text, text),
        )

        self.assertEqual([row[0] for row in rows], ["tier2", "tier3"])
        self.assertEqual([row[1] for row in rows], ["A", "B"])
        self.assertEqual([row[10]["observation_origin"] for row in rows], ["live", "live"])
        self.assertEqual([row[10]["observed_at"] for row in rows], ["", ""])

    def test_configured_node_rows_for_tier_preserves_desktop_row_contract(self) -> None:
        item = {
            "tier": "tier2",
            "kind": "node",
            "label": "Gateway",
            "protocol": "ss",
            "address": "198.51.100.1",
            "port": "443",
            "uri": "ss://gateway",
        }
        ignored = {"tier": "tier2", "kind": "subscription", "label": "Sub"}

        rows = configured_node_rows_for_tier(
            "tier2",
            [item, ignored],
            translate=lambda text: {"配置": "Config", "是": "Yes"}.get(text, text),
        )

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row[:10], ["tier2", "Gateway", "ss", "198.51.100.1:443", "configured", "--", "--", "Config", "Yes", "--"])
        self.assertEqual(row[10]["config_item"], item)
        self.assertEqual(row[10]["address"], "198.51.100.1")
        self.assertEqual(row[10]["port"], "443")

    def test_config_rows_for_tier_preserves_runtime_status_for_nodes(self) -> None:
        item = {
            "tier": "tier3",
            "kind": "node",
            "label": "Imported",
            "protocol": "vless",
            "address": "198.51.100.1",
            "port": "443",
            "uri": "vless://imported",
        }
        ignored = {"tier": "tier2", "kind": "node", "label": "Other"}
        runtime_row = [
            "tier3",
            "Imported",
            "vless",
            "198.51.100.1:443",
            "alive",
            "12ms",
            "2026-05-30T10:00:00+00:00",
            "",
            "",
            "Normal",
            {"address": "198.51.100.1", "port": "443", "identity": "runtime-id"},
        ]

        rows = config_rows_for_tier(
            "tier3",
            [ignored, item],
            [runtime_row],
            translate=lambda text: {"用户导入": "User import", "是": "Yes", "正常": "Normal"}.get(text, text),
        )

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(
            row[:10],
            [
                "tier3",
                "Imported",
                "vless",
                "198.51.100.1:443",
                "alive",
                "12ms",
                "2026-05-30T10:00:00+00:00",
                "User import",
                "Yes",
                "Normal",
            ],
        )
        self.assertEqual(row[10]["identity"], "runtime-id")
        self.assertEqual(row[10]["entry_kind"], "node")
        self.assertIs(row[10]["config_item"], item)

    def test_config_rows_for_tier_preserves_subscription_contract(self) -> None:
        item = {
            "tier": "tier3",
            "kind": "subscription",
            "label": "Provider",
            "detail": "https://sub.example/list",
        }

        rows = config_rows_for_tier(
            "tier3",
            [item],
            [],
            translate=lambda text: {"订阅": "Subscription", "用户导入": "User import", "是": "Yes"}.get(text, text),
        )

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(
            row[:10],
            [
                "tier3",
                "Provider",
                "Subscription",
                "https://sub.example/list",
                "configured",
                "--",
                "--",
                "User import",
                "--",
                "--",
            ],
        )
        self.assertEqual(row[10]["entry_kind"], "subscription")
        self.assertEqual(row[10]["protocol"], "Subscription")
        self.assertIs(row[10]["config_item"], item)

    def test_layer_scope_rows_prefers_runtime_rows_for_non_tier3_and_config_rows_for_tier3(self) -> None:
        tier2_runtime = ["tier2", "Runtime", "ss", "198.51.100.1:443", "alive", "", "", "", "", "", {"identity": "runtime"}]
        tier2_config = {
            "tier": "tier2",
            "kind": "node",
            "label": "Configured",
            "protocol": "ss",
            "address": "198.51.100.2",
            "port": "443",
        }
        tier3_config = {
            "tier": "tier3",
            "kind": "node",
            "label": "Imported",
            "protocol": "vless",
            "address": "198.51.100.3",
            "port": "443",
        }

        self.assertEqual(layer_scope_rows("tier2", [tier2_runtime], [tier2_config], translate=str), [tier2_runtime])
        tier3_rows = layer_scope_rows("tier3", [], [tier3_config], translate=str)

        self.assertEqual(len(tier3_rows), 1)
        self.assertEqual(tier3_rows[0][1], "Imported")
        self.assertEqual(tier3_rows[0][3], "198.51.100.3:443")

    def test_layer_scope_rows_groups_tier2_subscription_nodes_without_changing_manual_imports(self) -> None:
        url = "https://provider.example/sub?token=secret"
        manual = [
            "tier2",
            "Manual",
            "ss",
            "198.51.100.1:443",
            "alive",
            "30",
            "",
            "1",
            "是",
            "正常",
            {"identity": "manual", "source_type": "manual"},
        ]
        subscription_node = [
            "tier2",
            "Sub Node",
            "vless",
            "203.0.113.10:443",
            "alive",
            "21",
            "",
            "1",
            "是",
            "正常",
            {"identity": "sub", "source_type": "subscription_url", "source": url, "sources": [url]},
        ]
        subscription = {
            "tier": "tier2",
            "kind": "subscription",
            "label": "provider.example",
            "detail": "https://provider.example",
            "url": url,
            "uri": url,
            "editable": True,
        }

        rows = layer_scope_rows("tier2", [manual, subscription_node], [subscription], translate=lambda text: text)

        self.assertEqual(rows[0], manual)
        self.assertEqual(len(rows), 2)
        group = rows[1]
        self.assertTrue(is_subscription_group_row(group))
        self.assertEqual(group[1], "provider.example")
        self.assertEqual(group[10]["subscription_child_count"], 1)
        self.assertEqual(group[10]["subscription_healthy_count"], 1)
        self.assertEqual(
            group[10]["subscription_child_preview"],
            (
                {
                    "name": "Sub Node",
                    "status": "alive",
                    "latency": "21 ms",
                    "enabled": True,
                    "observation_origin": "live",
                    "observed_at": "",
                },
            ),
        )
        self.assertEqual(node_row_source_values(subscription_node), (url,))
        self.assertTrue(row_matches_subscription_config(subscription_node, subscription))

        card = layer_node_card_state(group, status_label=str, translate=lambda text: text)
        self.assertEqual(card.status_state, "subscription")
        self.assertIn("1/1", card.summary)
        self.assertEqual(card.entry_kind, "subscription_group")
        self.assertEqual(card.endpoint_label, "https://provider.example")
        self.assertEqual(card.endpoint_tooltip, "https://provider.example")
        self.assertEqual(card.origin_label, url)
        self.assertEqual(card.origin_tooltip, url)
        self.assertEqual(card.protocol_parts, ())
        self.assertEqual(card.primary_protocol, "订阅")
        self.assertEqual(card.latency_semantics, "none")
        self.assertEqual(card.state_tag_label, "可用")
        self.assertEqual(card.state_tag_state, "ok")
        self.assertFalse(card.is_current_exit)
        self.assertEqual(card.source_label, "provider.example")
        self.assertEqual(card.source_tooltip, url)
        self.assertEqual(card.count_label, "1")
        self.assertEqual(card.health_label, "本次 1/1")
        self.assertEqual(card.health_state, "ok")
        self.assertEqual(card.action_label, "详情")
        self.assertTrue(card.enabled)
        self.assertEqual(len(card.subscription_node_previews), 1)
        self.assertEqual(card.subscription_node_previews[0].name, "Sub Node")
        self.assertEqual(card.subscription_node_previews[0].status_state, "ok")
        self.assertEqual(card.subscription_node_previews[0].latency_label, "21 ms")
        self.assertEqual(card.subscription_remaining_count, 0)

        detail_rows = subscription_group_detail_rows("tier2", subscription, [manual, subscription_node], translate=lambda text: text)
        self.assertTrue(is_subscription_group_back_row(detail_rows[0]))
        self.assertEqual(len(detail_rows), 2)
        self.assertTrue(is_subscription_node_row(detail_rows[1]))
        self.assertEqual(detail_rows[1][7], "订阅导入")
        self.assertEqual(detail_rows[1][8], url)
        self.assertNotIn("config_item", detail_rows[1][10])
        self.assertEqual(detail_rows[1][10]["subscription_config_item"], subscription)
        detail_rows[1][10]["address"] = "203.0.113.10"
        detail_rows[1][10]["port"] = "443"
        same_identity_manual_config = {
            "tier": "tier2",
            "kind": "node",
            "label": "Sub Node",
            "address": "203.0.113.10",
            "port": "443",
            "editable": True,
        }
        self.assertIsNone(config_item_for_node_row(detail_rows[1], [same_identity_manual_config]))

    def test_enabled_empty_subscription_group_is_neutral_and_awaits_update(self) -> None:
        row = subscription_group_row(
            {
                "tier": "tier2",
                "kind": "subscription",
                "label": "Empty Provider",
                "url": "https://empty.example/sub",
            },
            [],
            translate=lambda text: text,
        )

        card = layer_node_card_state(row, status_label=str, translate=lambda text: text)

        self.assertTrue(card.enabled)
        self.assertEqual(card.status_label, "待更新")
        self.assertEqual(card.status_state, "unknown")
        self.assertEqual(card.state_tag_label, "待更新")
        self.assertEqual(card.state_tag_state, "unknown")
        self.assertEqual(card.count_label, "0")
        self.assertEqual(card.health_label, "未加载")
        self.assertEqual(card.health_state, "unknown")
        self.assertEqual(card.availability_ratio, -1.0)

    def test_persisted_subscription_group_hides_cached_health_evidence(self) -> None:
        def child(
            name: str,
            status: str,
            latency: str,
            observed_at: str,
        ) -> list[object]:
            return [
                "tier2",
                name,
                "vless",
                "203.0.113.10:443",
                status,
                latency,
                observed_at,
                "1",
                "是",
                "正常",
                {
                    "source_type": "subscription_url",
                    "source": "https://cached.example/sub",
                    "sources": ["https://cached.example/sub"],
                    "observation_origin": "persisted_cache",
                    "observed_at": observed_at,
                },
            ]

        rows = [
            child("Cached Alive", "alive", "20", "2026-07-30T01:00:00+00:00"),
            child("Cached Dead", "dead", "90", "2026-07-30T01:02:00+00:00"),
            child("Cached Slow", "slow", "350", "2026-07-30T01:01:00+00:00"),
        ]
        group = subscription_group_row(
            {
                "tier": "tier2",
                "kind": "subscription",
                "label": "Cached Provider",
                "url": "https://cached.example/sub",
            },
            rows,
            translate=lambda text: text,
        )

        self.assertEqual(group[10]["observation_origin"], "persisted_cache")
        self.assertEqual(group[10]["observed_at"], "")
        self.assertEqual(group[10]["subscription_healthy_count"], 0)
        self.assertEqual(group[4], "configured")
        card = layer_node_card_state(group, status_label=str, translate=lambda text: text)
        self.assertEqual(card.status_label, "待复检")
        self.assertEqual(card.status_state, "unknown")
        self.assertEqual(card.state_tag_label, "待复检")
        self.assertEqual(card.state_tag_state, "unknown")
        self.assertEqual(card.count_label, "3")
        self.assertEqual(card.health_label, "3 个节点")
        self.assertEqual(card.health_state, "unknown")
        self.assertEqual(card.availability_ratio, -1.0)
        self.assertEqual(
            tuple(preview.status_label for preview in card.subscription_node_previews),
            ("待复检", "待复检", "待复检"),
        )
        self.assertEqual(
            tuple(preview.status_state for preview in card.subscription_node_previews),
            ("unknown", "unknown", "unknown"),
        )
        self.assertEqual(
            tuple(preview.latency_label for preview in card.subscription_node_previews),
            ("", "", ""),
        )
        detail_rows = subscription_group_detail_rows(
            "tier2",
            {
                "tier": "tier2",
                "kind": "subscription",
                "label": "Cached Provider",
                "url": "https://cached.example/sub",
            },
            rows,
            translate=lambda text: text,
        )
        cached_child = layer_node_card_state(
            detail_rows[1],
            status_label=str,
            translate=lambda text: text,
        )
        self.assertEqual(cached_child.status_label, "待复检")
        self.assertEqual(cached_child.status_state, "unknown")
        self.assertEqual(cached_child.state_tag_label, "待复检")
        self.assertEqual(cached_child.state_tag_state, "unknown")
        self.assertEqual(cached_child.latency_label, "")
        self.assertEqual(cached_child.latency_semantics, "none")
        detail = node_detail_values(
            detail_rows[1],
            tier_label=str,
            status_label=str,
            translate=lambda text: text,
        )
        self.assertEqual(detail["status"], "待复检")
        self.assertEqual(detail["latency"], "")
        self.assertEqual(detail["last_checked"], "")
        self.assertNotIn("2026-07-30", "\n".join(detail.values()))

        unchecked_group = subscription_group_row(
            {
                "tier": "tier2",
                "kind": "subscription",
                "label": "Unchecked Provider",
                "url": "https://cached.example/sub",
            },
            [child("Unchecked", "configured", "--", "2026-07-30T01:03:00+00:00")],
            translate=lambda text: text,
        )
        unchecked = layer_node_card_state(
            unchecked_group,
            status_label=str,
            translate=lambda text: text,
        ).subscription_node_previews[0]
        self.assertEqual(unchecked.status_label, "待复检")
        self.assertEqual(unchecked.status_state, "unknown")
        self.assertEqual(unchecked.latency_label, "")

    def test_persisted_subscription_without_observed_time_stays_neutral(self) -> None:
        url = "https://cached.example/no-time"
        child = [
            "tier2",
            "Cached No Time",
            "vless",
            "203.0.113.12:443",
            "alive",
            "31",
            "",
            "1",
            "是",
            "正常",
            {
                "source_type": "subscription_url",
                "entry_kind": "subscription_node",
                "source": url,
                "sources": [url],
                "observation_origin": "persisted_cache",
                "observed_at": "",
            },
        ]
        item = {
            "tier": "tier2",
            "kind": "subscription",
            "label": "No Time Provider",
            "url": url,
        }

        group = subscription_group_row(
            item,
            [child],
            translate=lambda text: text,
        )
        card = layer_node_card_state(
            group,
            status_label=str,
            translate=lambda text: text,
        )

        self.assertEqual(group[4], "configured")
        self.assertEqual(group[10]["observation_origin"], "persisted_cache")
        self.assertEqual(group[10]["observed_at"], "")
        self.assertEqual(card.status_state, "unknown")
        self.assertEqual(card.state_tag_state, "unknown")
        self.assertEqual(card.status_label, "待复检")
        self.assertEqual(card.health_label, "1 个节点")
        self.assertNotIn("上次", card.summary)
        self.assertEqual(card.health_state, "unknown")
        self.assertEqual(card.availability_ratio, -1.0)
        self.assertEqual(card.subscription_node_previews[0].status_label, "待复检")
        self.assertEqual(card.subscription_node_previews[0].status_state, "unknown")
        self.assertEqual(card.subscription_node_previews[0].latency_label, "")

        child_card = layer_node_card_state(
            child,
            status_label=str,
            translate=lambda text: text,
        )
        self.assertEqual(child_card.status_label, "待复检")
        self.assertEqual(child_card.status_state, "unknown")
        self.assertEqual(child_card.latency_label, "")

    def test_persisted_subscription_never_derives_visible_health_ratio(self) -> None:
        url = "https://cached.example/mixed-time"

        def child(name: str, status: str, observed_at: str) -> list[object]:
            return [
                "tier2",
                name,
                "vless",
                "203.0.113.13:443",
                status,
                "31",
                observed_at,
                "1",
                "yes",
                "Normal",
                {
                    "source_type": "subscription_url",
                    "entry_kind": "subscription_node",
                    "source": url,
                    "sources": [url],
                    "observation_origin": "persisted_cache",
                    "observed_at": observed_at,
                },
            ]

        group = subscription_group_row(
            {
                "tier": "tier2",
                "kind": "subscription",
                "label": "Mixed Cache Provider",
                "url": url,
            },
            [
                child("Observed Alive", "alive", "2026-07-30T01:00:00+00:00"),
                child("Observed Dead", "dead", "2026-07-30T01:01:00+00:00"),
                child("Legacy Alive", "alive", ""),
            ],
            translate=lambda text: text,
        )

        self.assertEqual(group[10]["subscription_child_count"], 3)
        self.assertEqual(group[10]["subscription_healthy_count"], 0)
        card = layer_node_card_state(
            group,
            status_label=str,
            translate=lambda text: text,
        )
        self.assertEqual(card.health_label, "3 个节点")
        self.assertNotIn("1/3", card.summary)
        self.assertEqual(card.health_state, "unknown")
        self.assertEqual(card.availability_ratio, -1.0)

    def test_legacy_offline_refresh_marker_uses_neutral_persisted_cache_semantics(self) -> None:
        url = "https://offline.example/sub"
        children = [
            [
                "exit",
                "Fresh Alive",
                "ss",
                "203.0.113.20:443",
                "alive",
                "25",
                "",
                "1",
                "是",
                "正常",
                {
                    "source_type": "subscription_url",
                    "source": url,
                    "sources": [url],
                    "observation_origin": "offline_refresh",
                    "observed_at": "2026-07-30T02:00:00+00:00",
                },
            ],
            [
                "exit",
                "Fresh Dead",
                "ss",
                "203.0.113.21:443",
                "dead",
                "80",
                "",
                "1",
                "是",
                "正常",
                {
                    "source_type": "subscription_url",
                    "source": url,
                    "sources": [url],
                    "observation_origin": "offline_refresh",
                    "observed_at": "2026-07-30T02:01:00+00:00",
                },
            ],
        ]
        group = subscription_group_row(
            {
                "tier": "exit",
                "kind": "subscription",
                "label": "Offline Provider",
                "url": url,
            },
            children,
            translate=lambda text: text,
        )

        self.assertEqual(group[4], "configured")
        self.assertEqual(group[10]["observation_origin"], "persisted_cache")
        self.assertEqual(group[10]["observed_at"], "")
        self.assertEqual(group[10]["subscription_healthy_count"], 0)
        card = layer_node_card_state(group, status_label=str, translate=lambda text: text)
        self.assertEqual(card.status_label, "待复检")
        self.assertEqual(card.status_state, "unknown")
        self.assertEqual(card.state_tag_label, "待复检")
        self.assertEqual(card.state_tag_state, "unknown")
        self.assertEqual(card.health_label, "2 个节点")
        self.assertEqual(card.health_state, "unknown")
        self.assertEqual(card.availability_ratio, -1.0)
        self.assertEqual(
            tuple(preview.status_label for preview in card.subscription_node_previews),
            ("待复检", "待复检"),
        )
        self.assertEqual(
            tuple(preview.status_state for preview in card.subscription_node_previews),
            ("unknown", "unknown"),
        )
        self.assertEqual(
            tuple(preview.latency_label for preview in card.subscription_node_previews),
            ("", ""),
        )

        detail_rows = subscription_group_detail_rows(
            "exit",
            {
                "tier": "exit",
                "kind": "subscription",
                "label": "Offline Provider",
                "url": url,
            },
            children,
            translate=lambda text: text,
        )
        child_card = layer_node_card_state(
            detail_rows[1],
            status_label=str,
            translate=lambda text: text,
        )
        self.assertEqual(child_card.status_label, "待复检")
        self.assertEqual(child_card.status_state, "unknown")
        self.assertEqual(child_card.state_tag_state, "unknown")
        self.assertEqual(child_card.latency_label, "")
        self.assertEqual(child_card.latency_semantics, "none")

    def test_subscription_group_observation_uses_safest_child_origin(self) -> None:
        def observed_child(origin: str, observed_at: str) -> list[object]:
            return [
                "tier2",
                origin,
                "ss",
                "203.0.113.30:443",
                "alive",
                "30",
                observed_at,
                "1",
                "是",
                "正常",
                {
                    "observation_origin": origin,
                    "observed_at": observed_at,
                },
            ]

        group = subscription_group_row(
            {
                "tier": "tier2",
                "kind": "subscription",
                "label": "Mixed Provider",
                "url": "https://mixed.example/sub",
            },
            [
                observed_child("live", "2026-07-30T03:03:00+00:00"),
                observed_child("offline_refresh", "2026-07-30T03:02:00+00:00"),
                observed_child("persisted_cache", "2026-07-30T03:01:00+00:00"),
            ],
            translate=lambda text: text,
        )

        self.assertEqual(group[10]["observation_origin"], "persisted_cache")
        self.assertEqual(group[10]["observed_at"], "")

    def test_node_card_presentation_profiles_cover_distinct_protocol_and_entry_shapes(self) -> None:
        protocol_rows = {
            "ss": [
                "tier2",
                "Shadowsocks",
                "ss",
                "198.51.100.1:8388",
                "alive",
                "31",
                "",
                "Config",
                "",
                "",
                {"protocol_key": "ss"},
            ],
            "ssr": [
                "tier2",
                "ShadowsocksR",
                "ssr",
                "198.51.100.3:8388",
                "alive",
                "33",
                "",
                "Config",
                "",
                "",
                {"protocol_key": "ssr"},
            ],
            "socks5": [
                "tier2",
                "SOCKS",
                "SOCKS5",
                "198.51.100.2:1080",
                "alive",
                "34",
                "",
                "Config",
                "",
                "",
                {"protocol_key": "socks5"},
            ],
            "vless_reality": [
                "tier1",
                "Reality",
                "VLESS Reality",
                "reality.example:443",
                "alive",
                "42",
                "",
                "Config",
                "",
                "",
                {"protocol_key": "vless_reality"},
            ],
            "wireguard": [
                "tier1",
                "WireGuard",
                "WireGuard",
                "wg.example:51820",
                "alive",
                "48",
                "",
                "Config",
                "",
                "",
                {"protocol_key": "wireguard"},
            ],
        }

        for expected, row in protocol_rows.items():
            with self.subTest(profile=expected):
                self.assertEqual(layer_node_presentation_key(row), expected)
                self.assertEqual(
                    layer_node_card_state(
                        row,
                        status_label=str,
                        translate=lambda text: text,
                    ).presentation_key,
                    expected,
                )

        multiprotocol = [
            "tier1",
            "Edge Multi",
            "VLESS Reality / WireGuard",
            "edge.example:443",
            "alive",
            "38",
            "",
            "Config",
            "",
            "",
            {
                "variants": [
                    {"protocol_key": "vless_reality", "protocol": "VLESS Reality"},
                    {"protocol_key": "wireguard", "protocol": "WireGuard"},
                ]
            },
        ]
        multiprotocol_state = layer_node_card_state(
            multiprotocol,
            status_label=str,
            translate=lambda text: text,
        )
        self.assertEqual(
            multiprotocol_state.presentation_key,
            "tier1_multiprotocol",
        )
        self.assertEqual(multiprotocol_state.kind_tag_label, "多协议")

        subscription = subscription_group_row(
            {
                "tier": "tier2",
                "kind": "subscription",
                "label": "Provider",
                "url": "https://provider.example/sub",
                "enabled": True,
            },
            [],
            translate=lambda text: text,
        )
        self.assertEqual(
            layer_node_card_state(
                subscription,
                status_label=str,
                translate=lambda text: text,
            ).presentation_key,
            "subscription_group",
        )

    def test_layer_scope_rows_hides_unconfigured_subscription_runtime_rows(self) -> None:
        url = "https://provider.example/sub?token=secret"
        subscription_node = [
            "tier2",
            "Sub Node",
            "vless",
            "203.0.113.10:443",
            "alive",
            "21",
            "",
            "1",
            "yes",
            "Normal",
            {"identity": "sub", "source_type": "subscription_url", "source": url, "sources": [url]},
        ]

        self.assertEqual(layer_scope_rows("tier2", [subscription_node], [], translate=lambda text: text), [])

    def test_manual_config_card_remains_visible_when_subscription_child_has_same_identity(self) -> None:
        for tier in ("tier2", "exit"):
            with self.subTest(tier=tier):
                url = f"https://provider.example/{tier}/sub"
                runtime_row = [
                    tier,
                    "Shared Node",
                    "vless",
                    "203.0.113.10:443",
                    "alive",
                    "21",
                    "",
                    "1",
                    "yes",
                    "Normal",
                    {
                        "identity": f"{tier}-shared",
                        "address": "203.0.113.10",
                        "port": "443",
                        "source_type": "subscription_url",
                        "source": url,
                        "sources": [url],
                    },
                ]
                manual = {
                    "tier": tier,
                    "kind": "node",
                    "label": "Shared Node",
                    "address": "203.0.113.10",
                    "port": "443",
                    "protocol": "vless",
                    "editable": True,
                    "enabled": True,
                }
                subscription = {
                    "tier": tier,
                    "kind": "subscription",
                    "label": "Provider",
                    "url": url,
                    "uri": url,
                    "editable": True,
                    "enabled": True,
                }

                rows = layer_scope_rows(
                    tier,
                    [runtime_row],
                    [manual, subscription],
                    translate=lambda text: text,
                )

                manual_rows = [
                    row
                    for row in rows
                    if not is_subscription_group_row(row)
                    and node_row_meta(row).get("config_item") is manual
                ]
                groups = [row for row in rows if is_subscription_group_row(row)]
                self.assertEqual(len(manual_rows), 1)
                self.assertEqual(manual_rows[0][4], "alive")
                self.assertEqual(len(groups), 1)
                self.assertEqual(groups[0][10]["subscription_child_count"], 1)

                detail_rows = subscription_group_detail_rows(
                    tier,
                    subscription,
                    [runtime_row],
                    translate=lambda text: text,
                )
                child = next(row for row in detail_rows if is_subscription_node_row(row))
                self.assertIsNone(config_item_for_node_row(child, [manual]))

    def test_disabled_subscription_group_hides_stale_runtime_children_in_every_supported_tier(self) -> None:
        for tier in ("tier2", "tier3", "exit"):
            with self.subTest(tier=tier):
                url = f"https://provider.example/{tier}/sub"
                subscription = {
                    "tier": tier,
                    "kind": "subscription",
                    "label": f"{tier} Provider",
                    "url": url,
                    "key": url,
                    "editable": True,
                    "enabled": False,
                }
                stale_child = [
                    tier,
                    "Stale Child",
                    "vless",
                    "203.0.113.20:443",
                    "alive",
                    "20",
                    "",
                    "Subscription",
                    url,
                    "Normal",
                    {
                        "source_type": "subscription_url",
                        "source": url,
                        "sources": [url],
                        "tier3_origin": "tier3a" if tier == "tier3" else "",
                    },
                ]

                groups = subscription_group_rows_for_tier(
                    tier,
                    [stale_child],
                    [subscription],
                    translate=lambda text: text,
                )
                self.assertEqual(len(groups), 1)
                self.assertEqual(groups[0][4], "disabled")
                self.assertEqual(groups[0][10]["subscription_child_count"], 0)
                self.assertFalse(groups[0][10]["enabled"])
                detail = subscription_group_detail_rows(
                    tier,
                    subscription,
                    [stale_child],
                    translate=lambda text: text,
                )
                self.assertEqual(len(detail), 1)
                self.assertTrue(is_subscription_group_back_row(detail[0]))
                self.assertFalse(any(is_subscription_node_row(row) for row in detail))
                card = layer_node_card_state(groups[0], status_label=str, translate=lambda text: text)
                self.assertFalse(card.enabled)
                self.assertEqual(card.status_label, "已禁用")
                self.assertEqual(card.status_state, "draft")
                self.assertEqual(card.primary_protocol, "订阅")
                self.assertEqual(card.state_tag_label, "已禁用")
                self.assertEqual(card.state_tag_state, "draft")
                self.assertEqual(card.count_label, "未加载")
                self.assertEqual(card.health_label, "未加载")

    def test_layer_scope_rows_groups_only_tier3_3a_subscription_nodes(self) -> None:
        url = "https://tier3.example/sub?token=secret"
        subscription = {
            "tier": "tier3",
            "kind": "subscription",
            "label": "tier3.example",
            "detail": "https://tier3.example",
            "url": url,
            "uri": url,
            "editable": True,
        }
        tier3a_node = [
            "tier3",
            "3A Sub Node",
            "vless",
            "203.0.113.20:443",
            "alive",
            "36",
            "",
            "1",
            "是",
            "正常",
            {
                "identity": "tier3a-sub",
                "source_type": "subscription_url",
                "source": url,
                "sources": [url],
                "source_origin": "tier3a",
                "tier3_origin": "tier3a",
            },
        ]
        tier3b_node = [
            "tier3",
            "3B Pool Node",
            "ss",
            "203.0.113.21:443",
            "alive",
            "40",
            "",
            "1",
            "是",
            "正常",
            {
                "identity": "tier3b-auto",
                "source_type": "subscription_url",
                "source": url,
                "sources": [url],
                "source_origin": "tier3b",
                "tier3_origin": "tier3b",
                "tier3_auto_pool": True,
            },
        ]

        groups = subscription_group_rows_for_tier(
            "tier3",
            [tier3a_node, tier3b_node],
            [subscription],
            translate=lambda text: text,
        )
        detail_rows = subscription_group_detail_rows(
            "tier3",
            subscription,
            [tier3a_node, tier3b_node],
            translate=lambda text: text,
        )
        children = [row for row in detail_rows if is_subscription_node_row(row)]

        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0][10]["subscription_child_count"], 1)
        self.assertTrue(row_matches_subscription_config(tier3a_node, subscription))
        self.assertFalse(row_matches_subscription_config(tier3b_node, subscription))
        self.assertEqual([row[1] for row in children], ["3A Sub Node"])

    def test_layer_scope_rows_never_promotes_tier3b_pool_nodes_to_cards(self) -> None:
        configured = {
            "tier": "tier3",
            "kind": "node",
            "key": "3A Manual",
            "label": "3A Manual",
            "protocol": "vless",
            "address": "203.0.113.30",
            "port": "443",
            "uri": "vless://tier3a-manual",
            "enabled": True,
        }
        tier3a_node = [
            "tier3",
            "3A Manual",
            "vless",
            "203.0.113.30:443",
            "dead",
            "",
            "",
            "1",
            "是",
            "节点不可用",
            {
                "identity": "tier3a-manual",
                "tier3_origin": "tier3a",
                "source_origin": "tier3a",
            },
        ]
        tier3b_node = [
            "tier3",
            "3B Auto Candidate",
            "ss",
            "203.0.113.31:443",
            "dead",
            "",
            "",
            "1",
            "是",
            "节点不可用",
            {
                "identity": "tier3b-auto",
                "tier3_origin": "tier3b",
                "source_origin": "tier3b",
                "tier3_auto_pool": True,
            },
        ]

        rows = layer_scope_rows(
            "tier3",
            [tier3a_node, tier3b_node],
            [configured],
            translate=lambda text: text,
        )

        self.assertEqual([row[1] for row in rows], ["3A Manual"])
        self.assertEqual(rows[0][4], "dead")
        self.assertNotIn("3B Auto Candidate", repr(rows))

    def test_tier3_config_card_never_borrows_same_endpoint_tier3b_state(self) -> None:
        configured = {
            "tier": "tier3",
            "kind": "node",
            "key": "3A Manual",
            "label": "3A Manual",
            "protocol": "vless",
            "address": "203.0.113.30",
            "port": "443",
            "uri": "vless://tier3a-manual",
            "enabled": True,
        }
        tier3b_same_endpoint = [
            "tier3",
            "3B Auto Candidate",
            "ss",
            "203.0.113.30:443",
            "alive",
            "9",
            "",
            "1",
            "是",
            "正常",
            {
                "identity": "tier3b-auto",
                "address": "203.0.113.30",
                "port": "443",
                "tier3_origin": "tier3b",
                "source_origin": "tier3b",
                "tier3_auto_pool": True,
            },
        ]
        tier3a_same_endpoint = [
            "tier3",
            "3A Manual",
            "vless",
            "203.0.113.30:443",
            "dead",
            "",
            "",
            "1",
            "是",
            "节点不可用",
            {
                "identity": "tier3a-manual",
                "address": "203.0.113.30",
                "port": "443",
                "tier3_origin": "tier3a",
                "source_origin": "tier3a",
            },
        ]

        rows = layer_scope_rows(
            "tier3",
            [tier3b_same_endpoint, tier3a_same_endpoint],
            [configured],
            translate=lambda text: text,
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "3A Manual")
        self.assertEqual(rows[0][4], "dead")
        self.assertEqual(rows[0][10]["tier3_origin"], "tier3a")
        self.assertNotIn("tier3b-auto", repr(rows))

    def test_layer_scope_rows_falls_back_to_configured_nodes_when_runtime_rows_are_absent(self) -> None:
        item = {
            "tier": "exit",
            "kind": "node",
            "label": "Gateway",
            "protocol": "ss",
            "address": "198.51.100.4",
            "port": "443",
            "uri": "ss://gateway",
        }

        rows = layer_scope_rows("exit", [], [item], translate=str)

        self.assertEqual(rows[0][:7], ["exit", "Gateway", "ss", "198.51.100.4:443", "configured", "--", "--"])

    def test_gateway_layer_scope_rows_returns_all_exits_with_effective_first(self) -> None:
        osaka = ["exit", "Osaka", "ss", "198.51.100.4:443", "alive", "40", "", "", "", "", {"name": "Osaka"}]
        tokyo = ["exit", "Tokyo", "vless", "198.51.100.5:443", "alive", "25", "", "", "", "", {"name": "Tokyo"}]

        rows = gateway_layer_scope_rows("exit", [osaka, tokyo], [], selected_proxy_name="Tokyo", translate=str)

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][1], "Tokyo")
        self.assertEqual(rows[1][1], "Osaka")
        self.assertEqual(gateway_proxy_name_for_row(rows[0]), "Tokyo")
        self.assertTrue(rows[0][10]["gateway_effective"])
        self.assertEqual(rows[0][10]["gateway_candidate_count"], 2)
        self.assertFalse(rows[1][10].get("gateway_effective", False))

    def test_gateway_subscription_groups_use_unique_non_proxy_lookup_keys(self) -> None:
        first = subscription_group_row(
            {
                "tier": "exit",
                "kind": "subscription",
                "key": "https://first.example/sub",
                "url": "https://first.example/sub",
                "label": "Shared name",
            },
            [],
            translate=str,
        )
        second = subscription_group_row(
            {
                "tier": "exit",
                "kind": "subscription",
                "key": "https://second.example/sub",
                "url": "https://second.example/sub",
                "label": "Shared name",
            },
            [],
            translate=str,
        )
        actual_node = [
            "exit",
            "Shared name",
            "ss",
            "198.51.100.5:443",
            "alive",
            "25",
            "",
            "",
            "",
            "",
            {"name": "Shared name"},
        ]

        self.assertNotEqual(gateway_proxy_name_for_row(first), gateway_proxy_name_for_row(second))
        self.assertNotEqual(gateway_proxy_name_for_row(first), "Shared name")
        selected = gateway_effective_exit_row(
            [first, second, actual_node],
            selected_proxy_name="Shared name",
        )
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected[2], "ss")

    def test_gateway_effective_exit_row_requires_explicit_selected_candidate(self) -> None:
        dead = ["exit", "Dead", "ss", "198.51.100.4:443", "dead", "", "", "", "", "", {"name": "Dead"}]
        healthy = ["exit", "Healthy", "vless", "198.51.100.5:443", "alive", "", "", "", "", "", {"name": "Healthy"}]

        self.assertIsNone(gateway_effective_exit_row([dead, healthy], selected_proxy_name=""))
        self.assertIsNone(gateway_effective_exit_row([dead, healthy], selected_proxy_name="Missing"))

        row = gateway_effective_exit_row([dead, healthy], selected_proxy_name="Healthy")
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row[1], "Healthy")
        self.assertEqual(row[10]["gateway_selected_proxy"], "Healthy")

    def test_gateway_layer_scope_rows_keep_candidates_without_implicit_effective_exit(self) -> None:
        dead = ["exit", "Dead", "ss", "198.51.100.4:443", "dead", "", "", "", "", "", {"name": "Dead"}]
        healthy = ["exit", "Healthy", "vless", "198.51.100.5:443", "alive", "", "", "", "", "", {"name": "Healthy"}]

        rows = gateway_layer_scope_rows("exit", [dead, healthy], [], selected_proxy_name="", translate=str)

        self.assertEqual([row[1] for row in rows], ["Dead", "Healthy"])
        self.assertFalse(any(node_row_meta(row).get("gateway_effective") for row in rows))

    def test_layer_scope_view_state_preserves_tier3_specific_labels(self) -> None:
        state = layer_scope_view_state("tier3", translate=lambda text: f"t:{text}")

        self.assertEqual(state.nodes_title, "")
        self.assertEqual(state.nodes_minimum_height, 150)
        self.assertEqual(state.tree_headers[0], "t:名称")
        self.assertEqual(state.tree_headers[4], "t:地址 / URL")

    def test_layer_scope_view_state_uses_gateway_effective_exit_labels(self) -> None:
        state = layer_scope_view_state("exit", translate=lambda text: f"t:{text}")

        self.assertEqual(state.nodes_title, "")
        self.assertEqual(state.nodes_minimum_height, 230)
        self.assertEqual(state.tree_headers[0], "t:名称")
        self.assertEqual(
            layer_node_empty_text("exit", translate=lambda text: f"t:{text}"),
            "t:暂无出口节点",
        )

    def test_layer_scope_view_state_preserves_standard_layer_headers(self) -> None:
        state = layer_scope_view_state("tier2", translate=lambda text: f"t:{text}")

        self.assertEqual(state.nodes_title, "")
        self.assertEqual(state.nodes_minimum_height, 230)
        self.assertEqual(state.tree_headers, ["t:名称", "t:协议", "t:状态", "t:延迟", "t:地址", "t:来源", "t:风险"])

    def test_runtime_row_for_config_item_matches_address_and_port(self) -> None:
        matching = [
            "tier2",
            "Gateway",
            "ss",
            "198.51.100.1:443",
            "alive",
            "12ms",
            "",
            "",
            "",
            "",
            {"address": "198.51.100.1", "port": "443"},
        ]
        wrong_port = [
            "tier2",
            "Gateway",
            "ss",
            "198.51.100.1:8443",
            "alive",
            "30ms",
            "",
            "",
            "",
            "",
            {"address": "198.51.100.1", "port": "8443"},
        ]
        item = {"tier": "tier2", "kind": "node", "label": "Gateway", "address": "198.51.100.1", "port": "443"}

        self.assertIs(runtime_row_for_config_item(item, [wrong_port, matching]), matching)

    def test_runtime_row_for_config_item_falls_back_to_label_without_endpoint(self) -> None:
        matching = ["tier3", "Imported", "vless", "example.com:443", "alive", "", "", "", "", "", {}]
        wrong_label = ["tier3", "Other", "vless", "other.example:443", "alive", "", "", "", "", "", {}]
        item = {"tier": "tier3", "kind": "node", "label": "Imported"}

        self.assertIs(runtime_row_for_config_item(item, [wrong_label, matching]), matching)

    def test_runtime_row_for_config_item_ignores_non_nodes_and_tier_mismatch(self) -> None:
        row = ["tier2", "Gateway", "ss", "198.51.100.1:443", "alive", "", "", "", "", "", {}]

        self.assertIsNone(runtime_row_for_config_item({"tier": "tier2", "kind": "subscription", "label": "Gateway"}, [row]))
        self.assertIsNone(runtime_row_for_config_item({"tier": "tier3", "kind": "node", "label": "Gateway"}, [row]))

    def test_disabled_config_never_matches_same_endpoint_runtime_subscription(self) -> None:
        runtime = [
            "tier2",
            "Subscription child",
            "vless",
            "198.51.100.1:443",
            "alive",
            "18",
            "",
            "1",
            "yes",
            "Normal",
            {
                "address": "198.51.100.1",
                "port": "443",
                "source_type": "subscription_url",
                "source": "https://example.test/sub",
            },
        ]
        draft = {
            "tier": "tier2",
            "kind": "node",
            "key": "draft",
            "label": "Draft",
            "address": "198.51.100.1",
            "port": "443",
            "enabled": False,
        }

        self.assertIsNone(runtime_row_for_config_item(draft, [runtime]))
        self.assertIsNone(config_item_for_node_row(runtime, [draft]))

    def test_layer_scope_rows_keeps_drafts_after_active_rows(self) -> None:
        active = [
            "exit",
            "Active",
            "ss",
            "203.0.113.8:443",
            "alive",
            "12",
            "",
            "Config",
            "yes",
            "Normal",
            {"address": "203.0.113.8", "port": "443"},
        ]
        draft = {
            "tier": "exit",
            "kind": "node",
            "key": "draft",
            "label": "Draft",
            "protocol": "vless",
            "address": "203.0.113.9",
            "port": "443",
            "enabled": False,
        }

        rows = layer_scope_rows("exit", [active], [draft], translate=lambda text: text)

        self.assertEqual([row[1] for row in rows], ["Active", "Draft"])
        self.assertEqual(rows[1][4], "disabled")
        self.assertFalse(rows[1][10]["enabled"])

    def test_enabling_draft_keeps_the_same_config_backed_row_key(self) -> None:
        draft_item = {
            "tier": "tier2",
            "kind": "node",
            "key": "edge-config",
            "label": "Edge",
            "protocol": "vless",
            "address": "203.0.113.10",
            "port": "443",
            "enabled": False,
        }
        draft_row = layer_scope_rows("tier2", [], [draft_item], translate=lambda text: text)[0]
        runtime = [
            "tier2",
            "Edge",
            "vless",
            "203.0.113.10:443",
            "alive",
            "14",
            "",
            "Config",
            "yes",
            "Normal",
            {"address": "203.0.113.10", "port": "443"},
        ]
        enabled_item = {**draft_item, "enabled": True}

        active_row = layer_scope_rows("tier2", [runtime], [enabled_item], translate=lambda text: text)[0]

        self.assertEqual(node_row_key(active_row), node_row_key(draft_row))
        self.assertEqual(active_row[10]["config_item"]["key"], "edge-config")

    def test_draft_card_omits_runtime_health_latency_and_gateway_selection(self) -> None:
        draft = [
            "exit",
            "Draft",
            "vless",
            "203.0.113.9:443",
            "dead",
            "999",
            "",
            "Config",
            "yes",
            "Normal",
            {"enabled": False, "gateway_effective": True},
        ]

        state = layer_node_card_state(draft, status_label=str, translate=lambda text: text)

        self.assertFalse(state.enabled)
        self.assertEqual(state.status_label, "草稿")
        self.assertEqual(state.status_state, "draft")
        self.assertEqual(state.state_tag_label, "草稿")
        self.assertEqual(state.state_tag_state, "draft")
        self.assertFalse(state.is_current_exit)
        self.assertEqual(state.latency_label, "")
        self.assertEqual(state.latency_semantics, "none")
        self.assertEqual(state.endpoint_label, "203.0.113.9:443")
        self.assertEqual(state.origin_label, "Config")
        self.assertEqual(state.selection_label, "")
        self.assertIsNone(gateway_effective_exit_row([draft], selected_proxy_name="Draft"))

    def test_node_edit_update_payload_includes_enabled_for_nodes_and_subscription_groups(self) -> None:
        node = {"tier": "tier2", "kind": "node"}
        subscription = {"tier": "tier2", "kind": "subscription"}

        self.assertFalse(node_edit_update_payload(node, {"name": "Draft", "uri": "vless://node", "enabled": False})["enabled"])
        self.assertFalse(
            node_edit_update_payload(
                subscription,
                {"uri": "https://example.test/sub", "enabled": False},
            )["enabled"]
        )

    def test_node_enable_toggle_is_only_available_for_persisted_editable_nodes(self) -> None:
        editable_node = node_edit_view_state(
            {"tier": "tier2", "kind": "node", "editable": True, "enabled": False},
            None,
            translate=lambda text: text,
        )
        subscription = node_edit_view_state(
            {"tier": "tier2", "kind": "subscription", "editable": True},
            None,
            translate=lambda text: text,
        )
        auto_pool = node_edit_view_state(
            {"tier": "tier3", "kind": "node", "editable": True, "tier3_auto_pool": True},
            None,
            translate=lambda text: text,
        )

        self.assertTrue(editable_node.can_toggle_enabled)
        self.assertFalse(editable_node.enabled)
        self.assertTrue(subscription.can_toggle_enabled)
        self.assertFalse(auto_pool.can_toggle_enabled)

    def test_tier3_auto_pool_detection_matches_manager_contract(self) -> None:
        self.assertFalse(is_tier3_auto_pool_node(Node(tier="tier1")))
        self.assertFalse(is_tier3_auto_pool_node(Node(tier="tier3", extra={"tier3_origin": "tier3a"})))
        self.assertTrue(is_tier3_auto_pool_node(Node(tier="tier3", extra={"tier3_origin": "github"})))
        self.assertTrue(is_tier3_auto_pool_node(Node(tier="tier3", extra={})))


if __name__ == "__main__":
    unittest.main()
