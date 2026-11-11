import unittest
from dataclasses import FrozenInstanceError

from import_protocol_view import (
    PROTOCOL_DISPLAY_LABELS,
    PROTOCOL_SPECS,
    PROTOCOL_TEMPLATE_SPECS,
    FieldCondition,
    FieldPlacement,
    ProtocolFieldSpec,
    ProtocolGroupSpec,
    ProtocolOrderDraft,
    ProtocolTemplateSpec,
    SINGLE_PROTOCOL_BASE_FIELDS,
    SingleProtocolDraft,
    protocol_display_label,
    protocol_field_is_required,
    protocol_field_is_visible,
    protocol_legacy_override_field_map,
    protocol_required_field_keys,
    protocol_template_spec,
    protocol_visible_field_keys,
    protocol_visible_groups,
)
from structured_import_validation import structured_required_fields


class ImportProtocolViewTests(unittest.TestCase):
    @staticmethod
    def _field(protocol: str, key: str) -> ProtocolFieldSpec:
        section = next(spec for spec in PROTOCOL_SPECS if spec.key == protocol)
        return next(field for field in section.fields if field.key == key)

    def test_protocol_field_spec_keeps_three_argument_text_compatibility(self) -> None:
        field = ProtocolFieldSpec("key", "Label", "placeholder")

        self.assertEqual(field.input_type, "text")
        self.assertEqual(field.choices, ())
        self.assertIsNone(field.minimum)
        self.assertIsNone(field.maximum)
        self.assertFalse(field.advanced)

    def test_protocol_order_draft_adds_selects_moves_and_removes(self) -> None:
        draft = ProtocolOrderDraft(["vless", "hysteria2", "tuic"])

        self.assertTrue(draft.add("vless"))
        self.assertTrue(draft.add("hysteria2"))
        self.assertEqual(draft.ordered_keys, ("vless", "hysteria2"))
        self.assertEqual(draft.active_key, "hysteria2")
        self.assertEqual(draft.available_keys, ("tuic",))

        self.assertTrue(draft.move_up())
        self.assertEqual(draft.ordered_keys, ("hysteria2", "vless"))
        self.assertTrue(draft.select("vless"))
        self.assertTrue(draft.remove("vless"))
        self.assertEqual(draft.active_key, "hysteria2")
        self.assertEqual(draft.available_keys, ("vless", "tuic"))

    def test_single_protocol_drafts_keep_independent_selections(self) -> None:
        supported = ["vless", "hysteria2", "vless"]
        single = SingleProtocolDraft(supported, "vless")
        other = SingleProtocolDraft(supported)

        self.assertEqual(single.supported_keys, ("vless", "hysteria2"))
        self.assertEqual(single.selected_key, "vless")
        self.assertEqual(other.selected_key, "")

        self.assertTrue(other.select("hysteria2"))
        self.assertEqual(single.selected_key, "vless")
        self.assertEqual(other.selected_key, "hysteria2")

    def test_single_and_multi_protocol_drafts_do_not_share_state(self) -> None:
        supported = ["vless", "hysteria2"]
        single = SingleProtocolDraft(supported, "vless")
        multi = ProtocolOrderDraft(supported, ["hysteria2"])

        self.assertTrue(single.select("hysteria2"))
        self.assertTrue(multi.add("vless"))
        self.assertEqual(single.selected_key, "hysteria2")
        self.assertEqual(multi.ordered_keys, ("hysteria2", "vless"))

        single.clear()
        self.assertEqual(single.selected_key, "")
        self.assertEqual(multi.ordered_keys, ("hysteria2", "vless"))

    def test_single_protocol_draft_rejects_invalid_selection_and_clears(self) -> None:
        draft = SingleProtocolDraft(["vless", "hysteria2"], "unknown")

        self.assertEqual(draft.selected_key, "")
        self.assertFalse(draft.select("unknown"))
        self.assertEqual(draft.selected_key, "")
        self.assertTrue(draft.select("vless"))
        self.assertTrue(draft.select("vless"))

        draft.clear()
        self.assertEqual(draft.selected_key, "")
        self.assertEqual(draft.supported_keys, ("vless", "hysteria2"))

    def test_protocol_order_draft_readd_keeps_external_session_fields(self) -> None:
        draft = ProtocolOrderDraft(["vless", "hysteria2"], ["vless", "hysteria2"])
        field_drafts = {"vless": {"uuid": "kept-after-remove"}}

        self.assertTrue(draft.remove("vless"))
        self.assertTrue(draft.add("vless"))

        self.assertEqual(draft.ordered_keys, ("hysteria2", "vless"))
        self.assertEqual(field_drafts["vless"]["uuid"], "kept-after-remove")

    def test_protocol_order_draft_filters_invalid_and_duplicate_initial_entries(self) -> None:
        draft = ProtocolOrderDraft(
            ["vless", "hysteria2", "tuic"],
            ["unknown", "tuic", "tuic", "vless"],
            active_key="unknown",
        )

        self.assertEqual(draft.ordered_keys, ("tuic", "vless"))
        self.assertEqual(draft.active_key, "tuic")
        self.assertFalse(draft.add("unknown"))

    def test_protocol_order_draft_allows_empty_single_and_multiple_orders(self) -> None:
        draft = ProtocolOrderDraft(["vless", "hysteria2"])
        self.assertEqual(draft.ordered_keys, ())
        self.assertEqual(draft.active_key, "")

        self.assertTrue(draft.add("vless"))
        self.assertEqual(draft.ordered_keys, ("vless",))
        self.assertTrue(draft.add("hysteria2"))
        self.assertEqual(draft.ordered_keys, ("vless", "hysteria2"))

        draft.clear()
        self.assertEqual(draft.ordered_keys, ())
        self.assertEqual(draft.active_key, "")

    def test_protocol_specs_describe_semantic_native_controls(self) -> None:
        for protocol in (spec.key for spec in PROTOCOL_SPECS):
            with self.subTest(protocol=protocol, field="port"):
                port = self._field(protocol, "port")
                self.assertEqual(port.input_type, "integer")
                self.assertEqual((port.minimum, port.maximum), (1, 65535))

        for protocol, choices in {
            "vless_reality": ("tcp", "ws", "grpc", "h2"),
            "vmess": ("ws", "grpc", "h2", "tcp"),
            "trojan": ("tcp", "ws"),
            "argo_xhttp": ("xhttp",),
        }.items():
            with self.subTest(protocol=protocol, field="network"):
                network = self._field(protocol, "network")
                self.assertEqual(network.input_type, "enum")
                self.assertEqual(network.choices, choices)

        for protocol in ("vless_reality", "vmess", "naive", "argo_xhttp"):
            with self.subTest(protocol=protocol, field="security"):
                security = self._field(protocol, "security")
                self.assertEqual(security.input_type, "enum")
                self.assertTrue(security.choices)

        for protocol in ("trojan", "anytls", "hysteria2", "tuic"):
            with self.subTest(protocol=protocol, field="skip_cert_verify"):
                skip_verify = self._field(protocol, "skip_cert_verify")
                self.assertEqual(skip_verify.input_type, "boolean")
                self.assertTrue(skip_verify.advanced)

        self.assertEqual(self._field("vmess", "alter_id").input_type, "integer")
        self.assertEqual(self._field("vmess", "alter_id").minimum, 0)
        self.assertEqual(self._field("wireguard", "mtu").input_type, "integer")
        self.assertEqual(
            (self._field("wireguard", "mtu").minimum, self._field("wireguard", "mtu").maximum),
            (576, 65535),
        )

    def test_password_and_private_key_fields_request_password_controls(self) -> None:
        for protocol in ("trojan", "ss", "anytls", "naive", "socks5", "hysteria2", "tuic"):
            with self.subTest(protocol=protocol):
                self.assertEqual(self._field(protocol, "password").input_type, "password")
        self.assertEqual(self._field("hysteria2", "obfs_password").input_type, "password")
        self.assertEqual(self._field("wireguard", "private_key").input_type, "password")

    def test_protocol_order_preserves_existing_import_ui_order(self) -> None:
        self.assertEqual(
            [spec.key for spec in PROTOCOL_SPECS],
            [
                "vless_reality",
                "vmess",
                "trojan",
                "ss",
                "anytls",
                "naive",
                "socks5",
                "hysteria2",
                "tuic",
                "wireguard",
                "argo_xhttp",
            ],
        )

    def test_every_protocol_has_one_immutable_custom_template(self) -> None:
        self.assertEqual(
            [template.protocol_key for template in PROTOCOL_TEMPLATE_SPECS],
            [spec.key for spec in PROTOCOL_SPECS],
        )
        for template in PROTOCOL_TEMPLATE_SPECS:
            with self.subTest(protocol=template.protocol_key):
                self.assertIs(protocol_template_spec(template.protocol_key), template)
                self.assertGreaterEqual(len(template.groups), 1)
                field_keys = [
                    placement.field_key
                    for group in template.groups
                    for placement in group.fields
                ]
                self.assertEqual(len(field_keys), len(set(field_keys)))
                for group in template.groups:
                    for placement in group.fields:
                        self.assertGreaterEqual(placement.row, 0)
                        self.assertGreaterEqual(placement.column, 0)
                        self.assertGreaterEqual(placement.column_span, 1)
                        self.assertLessEqual(
                            placement.column + placement.column_span,
                            template.columns,
                        )

        template = ProtocolTemplateSpec(
            "example",
            (ProtocolGroupSpec("main", "Main", (FieldPlacement("port", 0),)),),
        )
        with self.assertRaises(FrozenInstanceError):
            template.columns = 3  # type: ignore[misc]

    def test_template_fields_match_protocol_fields_except_fixed_argo_network(self) -> None:
        for spec in PROTOCOL_SPECS:
            template = protocol_template_spec(spec.key)
            self.assertIsNotNone(template)
            template_keys = {
                placement.field_key
                for group in template.groups
                for placement in group.fields
            }
            expected = {field.key for field in spec.fields}
            if spec.key == "argo_xhttp":
                expected.remove("network")
            with self.subTest(protocol=spec.key):
                self.assertEqual(template_keys, expected)

    def test_conditions_share_visibility_and_required_state(self) -> None:
        values = {
            ("vless_reality", "network"): "tcp",
            ("vless_reality", "security"): "reality",
        }

        def field_value(protocol: str, key: str) -> str:
            return values.get((protocol, key), "")

        self.assertFalse(protocol_field_is_visible("vless_reality", "host", field_value))
        self.assertFalse(protocol_field_is_required("vless_reality", "host", field_value))
        self.assertTrue(protocol_field_is_required("vless_reality", "public_key", field_value))

        values[("vless_reality", "network")] = "grpc"
        self.assertTrue(protocol_field_is_visible("vless_reality", "grpc_service_name", field_value))
        self.assertTrue(protocol_field_is_required("vless_reality", "grpc_service_name", field_value))
        self.assertFalse(protocol_field_is_visible("vless_reality", "path", field_value))

        values[("vless_reality", "security")] = "none"
        self.assertFalse(protocol_field_is_visible("vless_reality", "public_key", field_value))
        self.assertNotIn("reality", {group.key for group in protocol_visible_groups("vless_reality", field_value)})
        self.assertEqual(
            set(protocol_required_field_keys("vless_reality", field_value)),
            {"port", "uuid", "network", "encryption", "security", "grpc_service_name"},
        )

    def test_presence_condition_hides_inactive_obfs_password_without_losing_spec(self) -> None:
        values: dict[tuple[str, str], str] = {}
        reader = lambda protocol, key: values.get((protocol, key), "")

        self.assertFalse(protocol_field_is_visible("hysteria2", "obfs_password", reader))
        values[("hysteria2", "obfs")] = "salamander"
        self.assertTrue(protocol_field_is_visible("hysteria2", "obfs_password", reader))
        self.assertTrue(protocol_field_is_required("hysteria2", "obfs_password", reader))
        self.assertTrue(FieldCondition("obfs", present=True).matches("salamander"))
        self.assertFalse(FieldCondition("obfs", present=True).matches(""))

    def test_visible_field_helper_uses_protocol_defaults(self) -> None:
        visible = set(protocol_visible_field_keys("vmess"))
        self.assertIn("host", visible)
        self.assertIn("path", visible)
        self.assertIn("sni", visible)
        self.assertNotIn("grpc_service_name", visible)
        self.assertNotIn("network", protocol_visible_field_keys("argo_xhttp"))
        self.assertEqual(protocol_visible_field_keys("unknown"), ())

    def test_visible_template_variants_never_overlap_grid_cells(self) -> None:
        variants = {
            "vless_reality": [
                {"network": network, "security": security}
                for network in ("tcp", "ws", "grpc", "h2")
                for security in ("reality", "tls", "none")
            ],
            "vmess": [
                {"network": network, "security": security}
                for network in ("ws", "grpc", "h2", "tcp")
                for security in ("tls", "none")
            ],
            "trojan": [{"network": "tcp"}, {"network": "ws"}],
            "naive": [{"security": "tls"}, {"security": "none"}],
            "hysteria2": [{"obfs": ""}, {"obfs": "salamander"}],
            "argo_xhttp": [{"security": "tls"}, {"security": "none"}],
        }
        for template in PROTOCOL_TEMPLATE_SPECS:
            for values in variants.get(template.protocol_key, [{}]):
                reader = lambda _protocol, key, values=values: values.get(key, "")
                for group in template.groups:
                    occupied: set[tuple[int, int]] = set()
                    for placement in group.fields:
                        if not protocol_field_is_visible(
                            template.protocol_key,
                            placement.field_key,
                            reader,
                        ):
                            continue
                        cells = {
                            (placement.row, column)
                            for column in range(
                                placement.column,
                                placement.column + placement.column_span,
                            )
                        }
                        with self.subTest(
                            protocol=template.protocol_key,
                            group=group.key,
                            values=values,
                            field=placement.field_key,
                        ):
                            self.assertFalse(occupied & cells)
                        occupied.update(cells)

    def test_single_protocol_base_fields_preserve_node_identity_fields(self) -> None:
        self.assertEqual([field.key for field in SINGLE_PROTOCOL_BASE_FIELDS], ["name", "server"])
        self.assertEqual(SINGLE_PROTOCOL_BASE_FIELDS[0].placeholder, "My-Node")
        self.assertEqual(SINGLE_PROTOCOL_BASE_FIELDS[1].placeholder, "example.com")

    def test_protocol_display_label_preserves_existing_validation_labels(self) -> None:
        self.assertEqual(protocol_display_label("vless_reality"), "VLESS Reality")
        self.assertEqual(protocol_display_label("argo_xhttp"), "Argo XHTTP")
        self.assertEqual(protocol_display_label("unknown"), "unknown")
        self.assertEqual(PROTOCOL_DISPLAY_LABELS["wireguard"], "WireGuard")

    def test_protocol_forms_expose_every_required_validation_field(self) -> None:
        base_keys = {field.key for field in SINGLE_PROTOCOL_BASE_FIELDS}
        for spec in PROTOCOL_SPECS:
            with self.subTest(protocol=spec.key):
                visible_keys = base_keys | {field.key for field in spec.fields}
                self.assertTrue(structured_required_fields([spec.key]) <= visible_keys)

    def test_legacy_override_map_keeps_uuid_secret_protocols(self) -> None:
        fields = {"port", "uuid", "password", "path"}
        for protocol in ("vless_reality", "vmess", "tuic", "argo_xhttp"):
            with self.subTest(protocol=protocol):
                self.assertEqual(
                    protocol_legacy_override_field_map(protocol, fields),
                    {"port": "port", "secret": "uuid", "path": "path"},
                )

    def test_legacy_override_map_keeps_password_secret_protocols(self) -> None:
        self.assertEqual(
            protocol_legacy_override_field_map("trojan", {"port", "password", "path"}),
            {"port": "port", "secret": "password", "path": "path"},
        )
        self.assertEqual(
            protocol_legacy_override_field_map("ss", {"port", "password", "method"}),
            {"port": "port", "secret": "password"},
        )

    def test_legacy_override_map_keeps_wireguard_special_path(self) -> None:
        self.assertEqual(
            protocol_legacy_override_field_map(
                "wireguard",
                {"port", "private_key", "public_key", "local_address"},
            ),
            {"port": "port", "secret": "private_key", "path": "public_key"},
        )


if __name__ == "__main__":
    unittest.main()
