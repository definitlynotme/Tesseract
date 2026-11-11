import tempfile
import unittest
from pathlib import Path

from main import build_subscription_server, load_config, tier1_nodes_for_killswitch


class MainRuntimeTests(unittest.TestCase):
    def test_tier1_nodes_for_killswitch_uses_only_list_nodes(self) -> None:
        self.assertEqual(tier1_nodes_for_killswitch({"tiers": "invalid"}), [])
        self.assertEqual(tier1_nodes_for_killswitch({"tiers": {"tier1": {"nodes": "invalid"}}}), [])

        nodes = [
            {"name": "primary"},
            {"name": "draft", "enabled": "false"},
            "legacy-node",
        ]
        self.assertEqual(
            tier1_nodes_for_killswitch({"tiers": {"tier1": {"nodes": nodes}}}),
            [nodes[0], nodes[2]],
        )

    def test_build_subscription_server_uses_shared_runtime_config(self) -> None:
        server = build_subscription_server(
            object(),  # type: ignore[arg-type]
            {
                "subscription_server": {
                    "host": "127.0.0.2",
                    "port": "bad",
                    "token": "secret",
                    "cors_allowed_origins": [" https://app.example ", ""],
                    "tier3_limit": "9",
                }
            },
        )

        self.assertEqual(server.host, "127.0.0.2")
        self.assertEqual(server.port, 25500)
        self.assertEqual(server.token, "secret")
        self.assertEqual(server.cors_allowed_origins, ["https://app.example"])
        self.assertEqual(server.limits, {"tier3_limit": 9})

    def test_load_config_requires_object_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.yaml"
            local_path = Path(temp_dir) / "config.local.yaml"
            config_path.write_text("[]\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "YAML root must be an object"):
                load_config(str(config_path), local_path=str(local_path))

            config_path.write_text("client: {}\n", encoding="utf-8")
            local_path.write_text("[]\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "YAML root must be an object"):
                load_config(str(config_path), local_path=str(local_path))


if __name__ == "__main__":
    unittest.main()
