import unittest

from import_mode_view import (
    ImportWorkspaceMode,
    import_editor_mode_state,
    import_kind_from_selection,
    import_method_for_kind,
    import_method_options,
    import_paste_copy,
    import_workspace_modes,
    manual_protocol_options,
    multiprotocol_import_payload,
    node_import_apply_payload,
    node_import_preview_payload,
    node_import_tier_for_mode,
    structured_multiprotocol_block,
    structured_shared_copy,
    structured_shared_field_labels,
    subscription_import_preview_payload,
    subscription_tier_for_mode,
    subscription_urls_from_inputs,
)


class ImportModeViewTests(unittest.TestCase):
    def test_import_method_options_separate_input_method_from_protocol(self) -> None:
        self.assertEqual(
            import_method_options("tier1"),
            [("粘贴", "paste"), ("手动", "manual")],
        )
        for layer in ("tier2", "tier3", "exit"):
            self.assertEqual(import_method_options(layer)[-1], ("URL", "subscription"))

    def test_manual_protocol_options_filter_layer_and_editor_kind(self) -> None:
        tier1 = manual_protocol_options("tier1")
        tier2 = manual_protocol_options("tier2")

        self.assertEqual(tier1[0], ("VLESS Reality", "vless_reality"))
        self.assertEqual(tier1, manual_protocol_options("tier1", "single"))
        self.assertIn(("Argo XHTTP", "argo_xhttp"), tier1)
        self.assertNotIn(("Argo XHTTP", "argo_xhttp"), tier2)
        self.assertNotIn(("多协议节点", "multiprotocol"), tier1)
        self.assertNotIn(("多协议节点", "multiprotocol"), tier2)
        self.assertNotIn(("粘贴节点内容（自动识别）", "nodes"), tier1)

        tier1_multi = manual_protocol_options("tier1", "multi")
        self.assertNotEqual(tier1_multi, tier1)
        self.assertNotIn(("Argo XHTTP", "argo_xhttp"), tier1_multi)
        self.assertEqual(
            tier1_multi,
            [option for option in tier1 if option[1] != "argo_xhttp"],
        )
        for layer in ("tier2", "tier3", "exit"):
            with self.subTest(layer=layer):
                self.assertEqual(manual_protocol_options(layer, "multi"), [])

    def test_manual_protocol_options_reject_invalid_layer_or_kind(self) -> None:
        self.assertEqual(manual_protocol_options("unknown"), [])
        self.assertEqual(manual_protocol_options("gateway"), [])
        self.assertEqual(manual_protocol_options("tier1", "unknown"), [])

    def test_import_selection_maps_back_to_existing_backend_kinds(self) -> None:
        self.assertEqual(import_kind_from_selection("paste", "vmess"), "nodes")
        self.assertEqual(import_kind_from_selection("manual", "vmess"), "vmess")
        self.assertEqual(import_kind_from_selection("manual", "multiprotocol"), "multiprotocol")
        self.assertEqual(import_kind_from_selection("manual", ""), "manual")
        self.assertEqual(import_kind_from_selection("subscription", "trojan"), "subscription")
        self.assertEqual(import_method_for_kind("nodes"), "paste")
        self.assertEqual(import_method_for_kind("subscription"), "subscription")
        self.assertEqual(import_method_for_kind("wireguard"), "manual")

    def test_paste_copy_keeps_only_the_input_label(self) -> None:
        for layer in ("tier1", "tier2", "tier3", "exit"):
            with self.subTest(layer=layer):
                copy = import_paste_copy(layer)
                self.assertEqual(copy.placeholder, "粘贴")
                self.assertEqual(copy.hint, "")
                self.assertEqual(copy.help_text, "")

    def test_import_workspace_modes_describe_visible_task_pages(self) -> None:
        paste = ImportWorkspaceMode("paste", "粘贴", "paste", "nodes")
        manual = ImportWorkspaceMode("manual", "手动", "manual", "")

        self.assertEqual(import_workspace_modes("unknown"), (paste, manual))
        self.assertEqual(import_workspace_modes("tier1"), (paste, manual))
        self.assertEqual(
            import_workspace_modes("tier2"),
            (
                paste,
                manual,
                ImportWorkspaceMode("subscription", "订阅", "subscription", "subscription"),
            ),
        )
        self.assertEqual(
            import_workspace_modes("tier3"),
            (
                paste,
                manual,
                ImportWorkspaceMode("subscription", "订阅", "subscription", "subscription"),
            ),
        )
        self.assertEqual(import_workspace_modes("exit"), import_workspace_modes("tier2"))

    def test_manual_workspace_kind_is_resolved_by_the_current_protocol(self) -> None:
        manual = next(mode for mode in import_workspace_modes("tier1") if mode.key == "manual")

        self.assertEqual(manual.method, "manual")
        self.assertEqual(manual.kind, "")
        self.assertEqual(import_kind_from_selection(manual.method, "wireguard"), "wireguard")

    def test_structured_shared_copy_avoids_layer_explanations(self) -> None:
        self.assertEqual(structured_shared_copy("tier1", multiprotocol=True)[0], "")
        self.assertEqual(structured_shared_copy("tier1", multiprotocol=True)[1], "")
        for layer in ("tier1", "tier2", "tier3", "exit"):
            with self.subTest(layer=layer):
                self.assertEqual(structured_shared_copy(layer, multiprotocol=False), ("", ""))

    def test_structured_shared_field_labels_switch_single_and_multi_labels(self) -> None:
        self.assertEqual(structured_shared_field_labels(multiprotocol=True)["name"], "名称")
        self.assertEqual(structured_shared_field_labels(multiprotocol=True)["port"], "端口")
        self.assertEqual(structured_shared_field_labels(multiprotocol=False)["name"], "名称")
        self.assertEqual(structured_shared_field_labels(multiprotocol=False)["port"], "端口")

    def test_import_editor_mode_state_preserves_placeholder_and_apply_text(self) -> None:
        multi = import_editor_mode_state("tier1_multiprotocol")
        tier3 = import_editor_mode_state("tier3_subscription")
        tier2_manual = import_editor_mode_state("tier2_vless_reality")
        fallback = import_editor_mode_state("unknown")

        self.assertTrue(multi.multiprotocol)
        self.assertEqual(multi.apply_text, "导入")
        self.assertEqual(tier3.apply_text, "添加")
        self.assertEqual(tier3.placeholder, "URL")
        self.assertEqual(tier2_manual.apply_text, "导入")
        self.assertEqual(fallback.apply_text, "导入")

    def test_subscription_urls_from_inputs_preserves_panel_dedupe_and_text_lines(self) -> None:
        self.assertEqual(
            subscription_urls_from_inputs(
                panel_visible=True,
                pending_url=" https://two.example/sub ",
                text="ignored",
            ),
            ["https://two.example/sub"],
        )
        self.assertEqual(
            subscription_urls_from_inputs(
                panel_visible=False,
                pending_url="ignored",
                text=" https://one.example/sub \n\nhttps://one.example/sub\n",
            ),
            ["https://one.example/sub", "https://one.example/sub"],
        )

    def test_subscription_import_payload_helpers_preserve_mode_contract(self) -> None:
        self.assertEqual(subscription_tier_for_mode("tier2_subscription"), "tier2")
        self.assertEqual(subscription_tier_for_mode("tier3_subscription"), "tier3")
        self.assertEqual(subscription_tier_for_mode("exit_subscription"), "exit")
        self.assertIsNone(subscription_tier_for_mode("tier1_nodes"))
        self.assertEqual(
            subscription_import_preview_payload("tier3", ["https://example.com/sub"]),
            {
                "tier": "tier3",
                "subscription_count": 1,
                "subscriptions": ["https://example.com/sub"],
            },
        )

    def test_node_import_payload_helpers_preserve_preview_and_apply_sources(self) -> None:
        self.assertEqual(node_import_tier_for_mode("tier2_nodes"), "tier2")
        self.assertEqual(node_import_tier_for_mode("tier3_nodes"), "tier3")
        self.assertEqual(node_import_tier_for_mode("exit_nodes"), "exit")
        self.assertEqual(node_import_tier_for_mode("unknown"), "tier1")
        self.assertEqual(
            node_import_preview_payload("tier3_nodes", "vless://node"),
            {"text": "vless://node", "tier": "tier3", "source": "native-app-tier3-import"},
        )
        self.assertEqual(
            node_import_apply_payload("tier3_nodes", "vless://node"),
            {"text": "vless://node", "source": "native-app-tier3a-import"},
        )
        self.assertEqual(
            node_import_apply_payload("tier3_nodes", "vless://node", "plan-123"),
            {
                "text": "vless://node",
                "source": "native-app-tier3a-import",
                "expected_plan_id": "plan-123",
            },
        )
        self.assertEqual(
            node_import_apply_payload("unknown", "vless://node"),
            {"text": "vless://node", "source": "native-app-tier1-import"},
        )

    def test_multiprotocol_import_payload_preserves_fields(self) -> None:
        self.assertEqual(
            multiprotocol_import_payload("VPS", "fallback:\n  vless", "vless"),
            {"name": "VPS", "text": "fallback:\n  vless", "fallback_order": "vless"},
        )

    def test_structured_multiprotocol_block_preserves_uri_protocol_sections(self) -> None:
        self.assertEqual(
            structured_multiprotocol_block(
                ["hysteria2", "vless_reality"],
                fallback_order=["vless_reality", "hysteria2"],
                uri_factory=lambda protocol: f"{protocol}://node",
                field_value=lambda protocol, key, default: default,
            ),
            "\n".join(
                [
                    "fallback:",
                    "  vless_reality -> hysteria2",
                    "",
                    "hysteria2:",
                    "  uri=hysteria2://node",
                    "",
                    "vless_reality:",
                    "  uri=vless_reality://node",
                ]
            ),
        )

    def test_structured_multiprotocol_block_preserves_argo_xhttp_section(self) -> None:
        values = {
            ("argo_xhttp", "server"): "127.0.0.1",
            ("argo_xhttp", "port"): "8443",
            ("argo_xhttp", "uuid"): "22222222-2222-2222-2222-222222222222",
            ("argo_xhttp", "host"): "argo.example.com",
            ("argo_xhttp", "path"): "/argo",
            ("argo_xhttp", "security"): "tls",
            ("argo_xhttp", "fingerprint"): "chrome",
        }

        self.assertEqual(
            structured_multiprotocol_block(
                ["argo_xhttp"],
                fallback_order=[],
                uri_factory=lambda protocol: f"{protocol}://node",
                field_value=lambda protocol, key, default: values.get((protocol, key), default),
            ),
            "\n".join(
                [
                    "fallback:",
                    "  argo_xhttp",
                    "",
                    "argo_xhttp:",
                    "  listen=127.0.0.1:8443",
                    "  uuid=22222222-2222-2222-2222-222222222222",
                    "  security=tls",
                    "  host=argo.example.com",
                    "  path=/argo",
                    "  mode=packet-up",
                    "  sni=argo.example.com",
                    "  fp=chrome",
                ]
            ),
        )

    def test_argo_xhttp_block_serializes_explicit_sni_only_for_tls(self) -> None:
        def render(security: str) -> str:
            values = {
                ("argo_xhttp", "security"): security,
                ("argo_xhttp", "host"): "host.example",
                ("argo_xhttp", "sni"): "sni.example",
                ("argo_xhttp", "fingerprint"): "chrome",
            }
            return structured_multiprotocol_block(
                ["argo_xhttp"],
                fallback_order=[],
                uri_factory=lambda protocol: f"{protocol}://node",
                field_value=lambda protocol, key, default: values.get((protocol, key), default),
            )

        tls = render("tls")
        plain = render("none")
        self.assertIn("  sni=sni.example", tls)
        self.assertIn("  fp=chrome", tls)
        self.assertNotIn("  sni=", plain)
        self.assertNotIn("  fp=", plain)


if __name__ == "__main__":
    unittest.main()
