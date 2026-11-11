import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from app_paths import (
    ROUTING_COMPATIBILITY_PURGE_MIGRATION,
    ROUTING_DIRECT_DEFAULTS_MIGRATION,
    ROUTING_RULE_DEFAULTS_MIGRATION,
    ROUTING_RULE_DEFAULTS_V2_MIGRATION,
    TIER3_BOOTSTRAP_SWITCH_REMOVAL_MIGRATION,
    TIER3_FAST_VALIDATION_DEFAULT_MIGRATION,
    TIER3_MIN_ALIVE_DEFAULT_MIGRATION,
    TIER3_SOURCE_DEFAULT_DELIVERY_MIGRATION,
    TIER3_SOURCE_PORTFOLIO_DEFAULTS_MIGRATION,
    TIER3_SOURCE_PORTFOLIO_DEFAULTS_V2_MIGRATION,
    apply_release_path_overrides,
    bootstrap_user_files,
    build_app_paths,
)


DEFAULT_COMPATIBILITY_RULES = [
    {"rule_type": "direct", "value": "geosite:CN"},
    {"rule_type": "direct", "value": "geoip:CN"},
    {"rule_type": "direct", "value": "suffix:github.com"},
    {"rule_type": "direct", "value": "suffix:githubusercontent.com"},
    {"rule_type": "direct", "value": "suffix:t.me"},
    {"rule_type": "direct", "value": "suffix:telegram.me"},
]

V2_COMPATIBILITY_RULES = DEFAULT_COMPATIBILITY_RULES[:2]


class AppPathsTests(unittest.TestCase):
    def _resource_root(self, temp_path: Path) -> Path:
        root = temp_path / "resources"
        (root / "bin").mkdir(parents=True)
        (root / "static").mkdir()
        (root / "bin" / "mihomo.exe").write_text("fake", encoding="utf-8")
        (root / "static" / "app_icon.ico").write_text("fake", encoding="utf-8")
        (root / "config.example.yaml").write_text("client:\n  mode: clash\n", encoding="utf-8")
        (root / "sources.example.yaml").write_text("github_repos: []\n", encoding="utf-8")
        return root

    def _write_routing_defaults_template(self, resource: Path) -> None:
        (resource / "config.example.yaml").write_text(
            yaml.safe_dump(
                {
                    "routing": {
                        "rules": {
                            "direct": [],
                            "proxy": [],
                            "reject": [],
                            "raw": [],
                            "rule_sets": [],
                        }
                    }
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )

    def _write_source_portfolio_template(self, resource: Path) -> dict:
        template_path = Path(__file__).resolve().parents[1] / "sources.example.yaml"
        payload = yaml.safe_load(template_path.read_text(encoding="utf-8"))
        (resource / "sources.example.yaml").write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        return payload

    def test_source_portfolio_defaults_migrate_once_then_return_to_user_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            resource = self._resource_root(temp_path)
            defaults = self._write_source_portfolio_template(resource)
            paths = build_app_paths(
                resource_root=resource,
                config_dir=temp_path / "config",
                data_dir=temp_path / "data",
            )
            paths.config_dir.mkdir()
            original = {
                "github_repos": [
                    {
                        "value": "https://github.com/barry-far/V2ray-Config",
                        "headers": {"X-Custom": "kept"},
                        "tags": ["user"],
                    }
                ],
                "telegram_channels": ["https://t.me/s/V2RayRootFree"],
                "subscription_urls": ["https://custom.example/sub"],
                "local_files": ["custom-nodes.txt"],
                "github_discovery": {"candidate_paths": ["custom/path.txt"]},
            }
            paths.sources_path.write_text(
                yaml.safe_dump(original, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )

            bootstrap_user_files(paths)

            migrated = yaml.safe_load(paths.sources_path.read_text(encoding="utf-8"))
            self.assertTrue(
                migrated["migrations"][TIER3_SOURCE_PORTFOLIO_DEFAULTS_MIGRATION]
            )
            self.assertEqual(migrated["github_repos"][0], original["github_repos"][0])
            self.assertEqual(migrated["telegram_channels"], original["telegram_channels"])
            self.assertEqual(migrated["subscription_urls"][0], "https://custom.example/sub")
            self.assertEqual(migrated["local_files"], ["custom-nodes.txt"])
            self.assertEqual(migrated["github_discovery"]["candidate_paths"][0], "custom/path.txt")
            self.assertTrue(
                migrated["migrations"][TIER3_SOURCE_PORTFOLIO_DEFAULTS_V2_MIGRATION]
            )
            self.assertEqual(len(migrated["github_repos"]), 2)
            self.assertEqual(len(migrated["telegram_channels"]), 1)
            self.assertEqual(
                len(migrated["subscription_urls"]),
                1 + len(defaults["subscription_urls"]),
            )
            self.assertFalse(
                any(
                    isinstance(entry, dict) and "added_in" in entry
                    for entry in migrated["subscription_urls"]
                )
            )
            self.assertEqual(
                migrated["github_discovery"]["candidate_paths"][1:],
                defaults["github_discovery"]["candidate_paths"],
            )
            backups = list(paths.config_backup_dir.glob("sources.yaml.*.bak"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(yaml.safe_load(backups[0].read_text(encoding="utf-8")), original)

            user_owned = dict(migrated)
            user_owned["github_repos"] = [migrated["github_repos"][0]]
            user_owned["subscription_urls"] = []
            user_owned["telegram_channels"] = list(reversed(migrated["telegram_channels"]))
            paths.sources_path.write_text(
                yaml.safe_dump(user_owned, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )

            bootstrap_user_files(paths)

            self.assertEqual(
                yaml.safe_load(paths.sources_path.read_text(encoding="utf-8")),
                user_owned,
            )
            self.assertEqual(
                len(list(paths.config_backup_dir.glob("sources.yaml.*.bak"))),
                1,
            )

    def test_portfolio_v2_adds_only_tagged_lists_once(self) -> None:
        """A profile that took v1 gets the v2 lists, never a default it deleted."""

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            resource = self._resource_root(temp_path)
            defaults = self._write_source_portfolio_template(resource)
            paths = build_app_paths(
                resource_root=resource,
                config_dir=temp_path / "config",
                data_dir=temp_path / "data",
            )
            paths.config_dir.mkdir()
            added = [
                entry["url"]
                for entry in defaults["subscription_urls"]
                if isinstance(entry, dict)
                and entry.get("added_in") == TIER3_SOURCE_PORTFOLIO_DEFAULTS_V2_MIGRATION
            ]
            self.assertEqual(len(added), 5)
            # Took v1 earlier, then removed Matin and kept a custom list.
            original = {
                "migrations": {
                    TIER3_SOURCE_PORTFOLIO_DEFAULTS_MIGRATION: True,
                    TIER3_SOURCE_DEFAULT_DELIVERY_MIGRATION: True,
                },
                "github_repos": [],
                "telegram_channels": [],
                "subscription_urls": ["https://custom.example/sub"],
                "local_files": [],
            }
            paths.sources_path.write_text(
                yaml.safe_dump(original, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )

            bootstrap_user_files(paths)

            migrated = yaml.safe_load(paths.sources_path.read_text(encoding="utf-8"))
            self.assertTrue(
                migrated["migrations"][TIER3_SOURCE_PORTFOLIO_DEFAULTS_V2_MIGRATION]
            )
            urls = [
                entry["url"] if isinstance(entry, dict) else entry
                for entry in migrated["subscription_urls"]
            ]
            self.assertEqual(urls, ["https://custom.example/sub", *added])
            self.assertEqual(migrated["github_repos"], [])
            self.assertEqual(migrated["telegram_channels"], [])
            for entry in migrated["subscription_urls"][1:]:
                self.assertNotIn("added_in", entry)
                self.assertNotIn("fetch_profile", entry)
                self.assertEqual(
                    entry["fetch_paths"], defaults["fetch_profiles"]["github_public"]
                )
            self.assertEqual(
                len(list(paths.config_backup_dir.glob("sources.yaml.*.bak"))), 1
            )

            # The user removes one of them; later starts leave that alone.
            migrated["subscription_urls"] = migrated["subscription_urls"][:3]
            paths.sources_path.write_text(
                yaml.safe_dump(migrated, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            bootstrap_user_files(paths)
            self.assertEqual(
                yaml.safe_load(paths.sources_path.read_text(encoding="utf-8")),
                migrated,
            )

    def test_seeded_default_sources_receive_template_roads_once(self) -> None:
        """A config seeded before roads existed keeps fetching from one blocked address."""

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            resource = self._resource_root(temp_path)
            defaults = self._write_source_portfolio_template(resource)
            paths = build_app_paths(
                resource_root=resource,
                config_dir=temp_path / "config",
                data_dir=temp_path / "data",
            )
            paths.config_dir.mkdir()
            seeded = {
                "migrations": {
                    TIER3_SOURCE_PORTFOLIO_DEFAULTS_MIGRATION: True,
                    TIER3_SOURCE_PORTFOLIO_DEFAULTS_V2_MIGRATION: True,
                },
                "github_repos": [
                    "https://github.com/barry-far/V2ray-Config",
                    {"url": "https://github.com/V2RayRoot/V2RayConfig", "fetch_paths": []},
                ],
                "telegram_channels": ["@V2RayRootFree"],
                "subscription_urls": [
                    "https://raw.githubusercontent.com/MatinGhanbari/v2ray-configs/main/subscriptions/v2ray/all_sub.txt",
                    {
                        "url": "https://github.com/Epodonios/v2ray-configs/raw/main/All_Configs_Sub.txt",
                        "headers": {"Authorization": "Bearer synthetic-secret"},
                    },
                    "https://mine.example/sub",
                ],
            }
            paths.sources_path.write_text(
                yaml.safe_dump(seeded, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )

            bootstrap_user_files(paths)

            migrated = yaml.safe_load(paths.sources_path.read_text(encoding="utf-8"))
            self.assertTrue(migrated["migrations"][TIER3_SOURCE_DEFAULT_DELIVERY_MIGRATION])
            expected = [
                road
                for road in defaults["fetch_profiles"]["github_public"]
            ]
            # The bare seeded default gains exactly the template's roads.
            self.assertEqual(migrated["github_repos"][0]["url"], seeded["github_repos"][0])
            self.assertEqual(migrated["github_repos"][0]["fetch_paths"], expected)
            # An explicit empty override stays the user's answer.
            self.assertEqual(migrated["github_repos"][1], seeded["github_repos"][1])
            # An authenticated source keeps its original address only.
            self.assertEqual(migrated["subscription_urls"][1], seeded["subscription_urls"][1])
            # A source the user added is not a seeded default.
            self.assertEqual(migrated["subscription_urls"][2], "https://mine.example/sub")
            # The template ships no road for Telegram, so nothing is invented.
            self.assertEqual(migrated["telegram_channels"], ["@V2RayRootFree"])
            self.assertEqual(
                migrated["subscription_urls"][0]["fetch_paths"], expected
            )
            backups = list(paths.config_backup_dir.glob("sources.yaml.*.bak"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(yaml.safe_load(backups[0].read_text(encoding="utf-8")), seeded)

    def test_roads_the_user_removes_are_not_restored(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            resource = self._resource_root(temp_path)
            self._write_source_portfolio_template(resource)
            paths = build_app_paths(
                resource_root=resource,
                config_dir=temp_path / "config",
                data_dir=temp_path / "data",
            )
            paths.config_dir.mkdir()
            paths.sources_path.write_text(
                yaml.safe_dump(
                    {
                        "migrations": {
                            TIER3_SOURCE_PORTFOLIO_DEFAULTS_MIGRATION: True,
                            TIER3_SOURCE_PORTFOLIO_DEFAULTS_V2_MIGRATION: True,
                        },
                        "github_repos": ["https://github.com/barry-far/V2ray-Config"],
                    },
                    allow_unicode=True,
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            bootstrap_user_files(paths)

            migrated = yaml.safe_load(paths.sources_path.read_text(encoding="utf-8"))
            self.assertIn("fetch_paths", migrated["github_repos"][0])
            del migrated["github_repos"][0]["fetch_paths"]
            paths.sources_path.write_text(
                yaml.safe_dump(migrated, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )

            bootstrap_user_files(paths)

            after = yaml.safe_load(paths.sources_path.read_text(encoding="utf-8"))
            self.assertNotIn("fetch_paths", after["github_repos"][0])
            self.assertEqual(
                len(list(paths.config_backup_dir.glob("sources.yaml.*.bak"))),
                1,
            )

    def test_fresh_install_keeps_the_template_text_and_skips_the_road_migration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            resource = self._resource_root(temp_path)
            template_path = Path(__file__).resolve().parents[1] / "sources.example.yaml"
            template_text = template_path.read_text(encoding="utf-8")
            (resource / "sources.example.yaml").write_text(template_text, encoding="utf-8")
            paths = build_app_paths(
                resource_root=resource,
                config_dir=temp_path / "config",
                data_dir=temp_path / "data",
            )

            bootstrap_user_files(paths)
            bootstrap_user_files(paths)

            self.assertEqual(
                paths.sources_path.read_text(encoding="utf-8"),
                template_text,
            )
            self.assertFalse(list(paths.config_backup_dir.glob("sources.yaml.*.bak")))

    def test_bootstrap_creates_clean_user_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            paths = build_app_paths(
                resource_root=self._resource_root(temp_path),
                config_dir=temp_path / "config",
                data_dir=temp_path / "data",
            )

            bootstrap_user_files(paths)

            self.assertEqual(paths.config_path.read_text(encoding="utf-8"), "client:\n  mode: clash\n")
            local_text = paths.local_config_path.read_text(encoding="utf-8")
            local_config = yaml.safe_load(local_text)
            self.assertIn("nodes: []", local_text)
            self.assertNotIn("Example VPS", local_text)
            tier3 = local_config["tiers"]["tier3"]
            self.assertGreater(len(local_config["client"]["mihomo_controller"]["secret"]), 20)
            self.assertFalse(tier3["enabled"])
            # The fallback-source switch is retired: missing means enabled.
            self.assertNotIn("bootstrap_enabled", tier3)
            self.assertEqual(tier3["min_alive_nodes"], 3)
            self.assertEqual(local_config["ui"]["language"], "en_US")
            self.assertEqual(local_config["ui"]["theme"], "daylight")
            self.assertNotIn("click_sound", local_config["ui"])
            self.assertTrue(local_config["migrations"][TIER3_MIN_ALIVE_DEFAULT_MIGRATION])
            # A fresh local file has nothing to migrate, so the template marks
            # both one-time migrations done.
            self.assertTrue(local_config["migrations"][TIER3_FAST_VALIDATION_DEFAULT_MIGRATION])
            self.assertTrue(local_config["migrations"][TIER3_BOOTSTRAP_SWITCH_REMOVAL_MIGRATION])
            self.assertNotIn("bootstrap_sources", tier3)
            self.assertNotIn("bootstrap_defaults_initialized", tier3)
            self.assertEqual(paths.sources_path.read_text(encoding="utf-8"), "github_repos: []\n")
            expected_data_dir = (temp_path / "data").resolve()
            self.assertEqual(paths.alert_delivery_history_path, expected_data_dir / "alert-delivery-history.json")
            self.assertEqual(paths.exit_quality_dir, expected_data_dir / "exit-quality")
            self.assertEqual(
                paths.exit_quality_results_path,
                expected_data_dir / "exit-quality" / "results-v2.json",
            )

    def test_bootstrap_migrates_existing_user_local_config_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            paths = build_app_paths(
                resource_root=self._resource_root(temp_path),
                config_dir=temp_path / "config",
                data_dir=temp_path / "data",
            )
            paths.config_dir.mkdir()
            paths.config_path.write_text("client:\n  mode: existing\n", encoding="utf-8")
            paths.local_config_path.write_text(
                """
private: true
tiers:
  tier3:
    enabled: 'false'
    nodes: []
    subscription_urls: []
    bootstrap_enabled: true
    bootstrap_sources:
      - https://example.com/custom-sub.txt
""",
                encoding="utf-8",
            )
            paths.sources_path.write_text("subscription_urls:\n  - https://example.com/sub\n", encoding="utf-8")

            bootstrap_user_files(paths)

            self.assertIn("existing", paths.config_path.read_text(encoding="utf-8"))
            local_config = yaml.safe_load(paths.local_config_path.read_text(encoding="utf-8"))
            tier3 = local_config["tiers"]["tier3"]
            first_secret = local_config["client"]["mihomo_controller"]["secret"]
            self.assertGreater(len(first_secret), 20)
            sources = yaml.safe_load(paths.sources_path.read_text(encoding="utf-8"))
            values = [
                *(sources.get("subscription_urls") or []),
                *(sources.get("github_repos") or []),
                *(sources.get("telegram_channels") or []),
            ]
            self.assertTrue(local_config["private"])
            self.assertFalse(tier3["enabled"])
            self.assertNotIn("bootstrap_sources", tier3)
            self.assertNotIn("bootstrap_defaults_initialized", tier3)
            self.assertIn("https://example.com/custom-sub.txt", values)
            self.assertEqual(len(values), len(set(values)))
            self.assertIn("https://example.com/sub", paths.sources_path.read_text(encoding="utf-8"))

            paths.local_config_path.write_text(yaml.safe_dump(local_config, sort_keys=False), encoding="utf-8")
            bootstrap_user_files(paths)

            migrated_again = yaml.safe_load(paths.local_config_path.read_text(encoding="utf-8"))
            self.assertEqual(migrated_again["client"]["mihomo_controller"]["secret"], first_secret)
            self.assertNotIn("bootstrap_sources", migrated_again["tiers"]["tier3"])

    def test_bootstrap_migrates_legacy_tier3_min_alive_default_to_current_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            paths = build_app_paths(
                resource_root=self._resource_root(temp_path),
                config_dir=temp_path / "config",
                data_dir=temp_path / "data",
            )
            paths.config_dir.mkdir()
            paths.config_path.write_text("client:\n  mode: existing\n", encoding="utf-8")
            paths.local_config_path.write_text(
                """
tiers:
  tier3:
    enabled: true
    nodes: []
    subscription_urls: []
    bootstrap_enabled: true
    min_alive_nodes: 20
""",
                encoding="utf-8",
            )

            bootstrap_user_files(paths)

            local_config = yaml.safe_load(paths.local_config_path.read_text(encoding="utf-8"))
            self.assertEqual(local_config["tiers"]["tier3"]["min_alive_nodes"], 3)
            self.assertTrue(local_config["migrations"][TIER3_MIN_ALIVE_DEFAULT_MIGRATION])

    def test_bootstrap_purges_hidden_compatibility_defaults_from_base(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            resource = self._resource_root(temp_path)
            self._write_routing_defaults_template(resource)
            paths = build_app_paths(
                resource_root=resource,
                config_dir=temp_path / "config",
                data_dir=temp_path / "data",
            )
            paths.config_dir.mkdir()
            paths.config_path.write_text(
                yaml.safe_dump(
                    {
                        "routing": {
                            "rules": {
                                "direct": ["base.example"],
                                "compatibility_defaults": DEFAULT_COMPATIBILITY_RULES,
                            }
                        }
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            bootstrap_user_files(paths)
            base = yaml.safe_load(paths.config_path.read_text(encoding="utf-8"))
            first_local = paths.local_config_path.read_text(encoding="utf-8")
            local = yaml.safe_load(first_local)

            # The hidden CN/source-host DIRECT stage is gone; the user's own
            # direct bucket is the only public DIRECT authorization left.
            self.assertNotIn("compatibility_defaults", base["routing"]["rules"])
            self.assertEqual(base["routing"]["rules"]["direct"], ["base.example"])
            self.assertTrue(local["migrations"][ROUTING_COMPATIBILITY_PURGE_MIGRATION])

            first_base = paths.config_path.read_text(encoding="utf-8")
            bootstrap_user_files(paths)
            self.assertEqual(paths.config_path.read_text(encoding="utf-8"), first_base)
            self.assertEqual(paths.local_config_path.read_text(encoding="utf-8"), first_local)

    def test_bootstrap_purges_hidden_compatibility_defaults_from_local(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            resource = self._resource_root(temp_path)
            self._write_routing_defaults_template(resource)
            paths = build_app_paths(
                resource_root=resource,
                config_dir=temp_path / "config",
                data_dir=temp_path / "data",
            )
            paths.config_dir.mkdir()
            paths.config_path.write_text(
                "routing:\n  rules:\n    direct: []\n", encoding="utf-8"
            )
            paths.local_config_path.write_text(
                yaml.safe_dump(
                    {
                        "routing": {
                            "rules": {
                                "compatibility_defaults": DEFAULT_COMPATIBILITY_RULES,
                            }
                        }
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            bootstrap_user_files(paths)
            local = yaml.safe_load(paths.local_config_path.read_text(encoding="utf-8"))
            rules = local.get("routing", {}).get("rules", {})
            self.assertNotIn("compatibility_defaults", rules)
            self.assertTrue(local["migrations"][ROUTING_COMPATIBILITY_PURGE_MIGRATION])

    def test_fresh_install_has_no_hidden_compatibility_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            resource = self._resource_root(temp_path)
            self._write_routing_defaults_template(resource)
            paths = build_app_paths(
                resource_root=resource,
                config_dir=temp_path / "config",
                data_dir=temp_path / "data",
            )
            paths.config_dir.mkdir()

            bootstrap_user_files(paths)
            base = yaml.safe_load(paths.config_path.read_text(encoding="utf-8"))
            rules = base.get("routing", {}).get("rules", {})
            self.assertNotIn("compatibility_defaults", rules)

    def test_bootstrap_purge_preserves_user_added_cn_direct_rule(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            resource = self._resource_root(temp_path)
            self._write_routing_defaults_template(resource)
            paths = build_app_paths(
                resource_root=resource,
                config_dir=temp_path / "config",
                data_dir=temp_path / "data",
            )
            paths.config_dir.mkdir()
            paths.config_path.write_text(
                yaml.safe_dump(
                    {
                        "routing": {
                            "rules": {
                                "direct": ["geosite:CN", "geoip:CN"],
                                "compatibility_defaults": DEFAULT_COMPATIBILITY_RULES,
                            }
                        }
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            bootstrap_user_files(paths)
            base = yaml.safe_load(paths.config_path.read_text(encoding="utf-8"))
            # A user who explicitly wants China-direct keeps it in the direct
            # bucket; only the hidden stage is removed.
            self.assertEqual(
                base["routing"]["rules"]["direct"], ["geosite:CN", "geoip:CN"]
            )
            self.assertNotIn("compatibility_defaults", base["routing"]["rules"])

    def test_bootstrap_preserves_user_tier3_min_alive_after_default_migration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            paths = build_app_paths(
                resource_root=self._resource_root(temp_path),
                config_dir=temp_path / "config",
                data_dir=temp_path / "data",
            )
            paths.config_dir.mkdir()
            paths.config_path.write_text("client:\n  mode: existing\n", encoding="utf-8")
            paths.local_config_path.write_text(
                f"""
migrations:
  {TIER3_MIN_ALIVE_DEFAULT_MIGRATION}: true
tiers:
  tier3:
    enabled: true
    nodes: []
    subscription_urls: []
    bootstrap_enabled: true
    min_alive_nodes: 20
""",
                encoding="utf-8",
            )

            bootstrap_user_files(paths)

            local_config = yaml.safe_load(paths.local_config_path.read_text(encoding="utf-8"))
            self.assertEqual(local_config["tiers"]["tier3"]["min_alive_nodes"], 20)
            self.assertTrue(local_config["migrations"][TIER3_MIN_ALIVE_DEFAULT_MIGRATION])

    def test_bootstrap_migrates_existing_base_config_missing_runtime_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            resource = self._resource_root(temp_path)
            (resource / "config.example.yaml").write_text(
                """
client:
  mode: clash
  mihomo_controller:
    enabled: true
    host: 127.0.0.1
    port: 9090
testing:
  xray_helper:
    probe_url: https://www.google.com/generate_204
  mihomo_helper:
    enabled: true
    probe_urls:
      - https://cp.cloudflare.com
      - https://www.gstatic.com/generate_204
    probe_url: https://cp.cloudflare.com
tiers:
  tier3:
    enabled: true
    validation_candidate_limit: 24
    validation_empty_retry_limit: 96
    validation_batch_size: 12
    startup_validation_candidate_limit: null
    startup_validation_empty_retry_limit: null
    startup_validation_batch_size: null
    startup_validation_concurrency: 8
    pi_controller:
      kp: 0.85
      ki: 0.15
      min_intensity: 0.12
    bootstrap_sources:
      - type: github_repo
        value: https://github.com/barry-far/V2ray-Config
        enabled: true
""",
                encoding="utf-8",
            )
            paths = build_app_paths(
                resource_root=resource,
                config_dir=temp_path / "config",
                data_dir=temp_path / "data",
            )
            paths.config_dir.mkdir()
            paths.config_path.write_text(
                """
client:
  mode: existing
testing:
  xray_helper:
    probe_url: https://www.google.com/generate_204
tiers:
  tier3:
    validation_candidate_limit: 12
    validation_empty_retry_limit: 36
    startup_min_alive_nodes: 1
    zero_asset_startup_min_alive_nodes: 2
    zero_asset_startup_core_min_alive_nodes: 1
    zero_asset_startup_source_budget: 6
    zero_asset_validation_empty_retry_limit: 72
    zero_asset_validation_batch_size: 6
    startup_validation_empty_retry_limit: 36
""",
                encoding="utf-8",
            )

            bootstrap_user_files(paths)

            config = yaml.safe_load(paths.config_path.read_text(encoding="utf-8"))
            self.assertEqual(config["client"]["mode"], "existing")
            self.assertTrue(config["client"]["mihomo_controller"]["enabled"])
            self.assertEqual(config["testing"]["mihomo_helper"]["probe_urls"][0], "https://cp.cloudflare.com")
            self.assertEqual(config["tiers"]["tier3"]["validation_candidate_limit"], 24)
            self.assertEqual(config["tiers"]["tier3"]["validation_empty_retry_limit"], 96)
            self.assertEqual(config["tiers"]["tier3"]["validation_batch_size"], 12)
            self.assertTrue(config["tiers"]["tier3"]["enabled"])
            self.assertNotIn("startup_min_alive_nodes", config["tiers"]["tier3"])
            self.assertNotIn("zero_asset_startup_min_alive_nodes", config["tiers"]["tier3"])
            self.assertNotIn("zero_asset_startup_core_min_alive_nodes", config["tiers"]["tier3"])
            self.assertEqual(config["tiers"]["tier3"]["startup_validation_concurrency"], 8)
            self.assertNotIn("zero_asset_startup_source_budget", config["tiers"]["tier3"])
            self.assertNotIn("zero_asset_validation_empty_retry_limit", config["tiers"]["tier3"])
            self.assertNotIn("zero_asset_validation_batch_size", config["tiers"]["tier3"])
            self.assertIsNone(config["tiers"]["tier3"]["startup_validation_empty_retry_limit"])
            self.assertEqual(config["tiers"]["tier3"]["pi_controller"]["kp"], 0.85)
            self.assertNotIn("bootstrap_sources", config["tiers"]["tier3"])
            sources = yaml.safe_load(paths.sources_path.read_text(encoding="utf-8"))
            self.assertIn("https://github.com/barry-far/V2ray-Config", sources["github_repos"])

    def test_bootstrap_syncs_tier3_throughput_defaults_without_touching_local_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            resource = self._resource_root(temp_path)
            (resource / "config.example.yaml").write_text(
                """
tiers:
  tier3:
    validation_concurrency_min: 3
    validation_concurrency_max: 16
    validation_worker_min: 1
    validation_worker_max: 16
    full_validation_process_max: 16
    source_stream_window_min: 8
    source_stream_window_max: null
    validation_budget_multiplier: 4
testing:
  mihomo_helper:
    concurrency: 3
""",
                encoding="utf-8",
            )
            paths = build_app_paths(
                resource_root=resource,
                config_dir=temp_path / "config",
                data_dir=temp_path / "data",
            )
            paths.config_dir.mkdir()
            paths.config_path.write_text(
                """
tiers:
  tier3:
    validation_concurrency_min: 3
    validation_concurrency_max: 8
    validation_worker_min: 1
    validation_worker_max: 3
    source_stream_window_min: 8
    source_stream_window_max: null
    validation_budget_multiplier: 4
testing:
  mihomo_helper:
    concurrency: 3
""",
                encoding="utf-8",
            )
            paths.local_config_path.write_text(
                """
tiers:
  tier3:
    validation_worker_max: 2
""",
                encoding="utf-8",
            )

            bootstrap_user_files(paths)

            config = yaml.safe_load(paths.config_path.read_text(encoding="utf-8"))
            local_config = yaml.safe_load(paths.local_config_path.read_text(encoding="utf-8"))
            tier3 = config["tiers"]["tier3"]
            self.assertEqual(tier3["validation_concurrency_max"], 16)
            self.assertEqual(tier3["validation_worker_max"], 16)
            self.assertEqual(tier3["full_validation_process_max"], 16)
            self.assertEqual(local_config["tiers"]["tier3"]["validation_worker_max"], 2)

    FAST_PROFILE = {
        "validation_slots_per_process": 8,
        "validation_worker_max": 8,
        "full_validation_process_max": 8,
        "validation_concurrency_max": 8,
        "tcp_breadth_per_identity": 2.0,
        "tcp_dns_capacity": "auto",
    }

    def _shipped_template_paths(self, temp_path: Path):
        import shutil

        resource = self._resource_root(temp_path)
        project_root = Path(__file__).resolve().parents[1]
        shutil.copy2(project_root / "config.example.yaml", resource / "config.example.yaml")
        paths = build_app_paths(
            resource_root=resource,
            config_dir=temp_path / "config",
            data_dir=temp_path / "data",
        )
        paths.config_dir.mkdir()
        return paths

    def test_existing_base_config_converges_to_the_shipped_fast_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = self._shipped_template_paths(Path(temp_dir))
            # A base file written before fast mode became the default.
            paths.config_path.write_text(
                """
tiers:
  tier3:
    validation_concurrency_max: 16
    validation_worker_max: 24
    full_validation_process_max: 24
    validation_slots_per_process: 1
    tcp_breadth_per_identity: 1.0
    bootstrap_enabled: false
""",
                encoding="utf-8",
            )

            bootstrap_user_files(paths)

            base = yaml.safe_load(paths.config_path.read_text(encoding="utf-8"))
            tier3 = base["tiers"]["tier3"]
            for key, value in self.FAST_PROFILE.items():
                self.assertEqual(tier3[key], value, key)
            self.assertNotIn("bootstrap_enabled", tier3)
            helper = base["testing"]["mihomo_helper"]
            self.assertEqual(helper["confirm_urls"], ["https://www.google.com/generate_204"])
            self.assertEqual(
                helper["route_health_check_url"], "https://www.google.com/generate_204"
            )

    def test_example_documents_real_site_checks_as_the_runtime_reads_them(self) -> None:
        # Base sync copies these keys, with their comments, into every existing
        # config.yaml, so the comments must describe what the code really does.
        from runtime_settings import DEFAULT_ROUTE_HEALTH_CHECK_URL, route_health_check_url
        from tier3_validation_domain import (
            DEFAULT_PROCESS_CONFIRM_URLS,
            process_validator_settings,
        )

        text = (Path(__file__).resolve().parents[1] / "config.example.yaml").read_text(
            encoding="utf-8"
        )
        example = yaml.safe_load(text)
        helper = example["testing"]["mihomo_helper"]
        self.assertEqual(helper["confirm_urls"], list(DEFAULT_PROCESS_CONFIRM_URLS))
        self.assertEqual(route_health_check_url(example), DEFAULT_ROUTE_HEALTH_CHECK_URL)
        # confirm_urls: [] really turns the admission gate off, as documented.
        self.assertEqual(
            process_validator_settings({"mihomo_helper": {"confirm_urls": []}})["confirm_urls"],
            [],
        )
        # route_health_check_url has no off switch: '' falls back to the default
        # instead of returning TIER3-AUTO to probe_url, and the comment says so.
        blank = {"testing": {"mihomo_helper": {**helper, "route_health_check_url": ""}}}
        self.assertEqual(route_health_check_url(blank), DEFAULT_ROUTE_HEALTH_CHECK_URL)
        lines = text.splitlines()
        key_index = next(
            index
            for index, line in enumerate(lines)
            if line.strip().startswith("route_health_check_url:")
        )
        comment_lines = []
        for line in reversed(lines[:key_index]):
            if not line.strip().startswith("#"):
                break
            comment_lines.append(line)
        comment = " ".join(comment_lines)
        self.assertIn("falls back", comment)
        self.assertNotIn("disables", comment)

    def _migrate_local_tier3(self, tier3: dict, migrations: dict | None = None) -> dict:
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = self._shipped_template_paths(Path(temp_dir))
            local = {"tiers": {"tier3": dict(tier3)}}
            if migrations is not None:
                local["migrations"] = dict(migrations)
            paths.local_config_path.write_text(
                yaml.safe_dump(local, sort_keys=False), encoding="utf-8"
            )

            bootstrap_user_files(paths)
            first = paths.local_config_path.read_text(encoding="utf-8")
            bootstrap_user_files(paths)
            # Idempotent: a second start rewrites nothing.
            self.assertEqual(paths.local_config_path.read_text(encoding="utf-8"), first)
            return yaml.safe_load(first)

    def test_local_fast_switch_profile_is_dropped_to_inherit_the_base(self) -> None:
        local = self._migrate_local_tier3(
            {"enabled": True, "min_alive_nodes": 3, "validation_concurrency_min": 1, **self.FAST_PROFILE}
        )

        tier3 = local["tiers"]["tier3"]
        for key in self.FAST_PROFILE:
            self.assertNotIn(key, tier3)
        # Keys the switch never wrote are the user's and stay.
        self.assertEqual(tier3["validation_concurrency_min"], 1)
        self.assertTrue(local["migrations"][TIER3_FAST_VALIDATION_DEFAULT_MIGRATION])

    def test_local_fast_switch_off_fingerprint_is_dropped(self) -> None:
        local = self._migrate_local_tier3(
            {
                "enabled": True,
                "min_alive_nodes": 3,
                "validation_slots_per_process": 1,
                "tcp_breadth_per_identity": 1.0,
            }
        )

        tier3 = local["tiers"]["tier3"]
        self.assertNotIn("validation_slots_per_process", tier3)
        self.assertNotIn("tcp_breadth_per_identity", tier3)
        self.assertTrue(local["migrations"][TIER3_FAST_VALIDATION_DEFAULT_MIGRATION])

    def test_hand_written_local_capacity_values_are_kept(self) -> None:
        cases = (
            # The documented single-slot opt-out.
            {
                "validation_slots_per_process": 1,
                "tcp_breadth_per_identity": 1.0,
                "validation_worker_max": 24,
                "full_validation_process_max": 24,
                "validation_concurrency_max": 16,
            },
            # Single slot beside a hand-written DNS pool size.
            {
                "validation_slots_per_process": 1,
                "tcp_breadth_per_identity": 1.0,
                "tcp_dns_capacity": 24,
            },
            # A partial or different batch layout.
            {"validation_slots_per_process": 8},
            {**self.FAST_PROFILE, "validation_worker_max": 12},
            {"validation_worker_max": 2},
        )
        for custom in cases:
            with self.subTest(custom=custom):
                local = self._migrate_local_tier3({"enabled": True, **custom})
                tier3 = local["tiers"]["tier3"]
                for key, value in custom.items():
                    self.assertEqual(tier3[key], value, key)

    def test_fast_validation_migration_runs_once(self) -> None:
        local = self._migrate_local_tier3(
            {
                "enabled": True,
                "validation_slots_per_process": 1,
                "tcp_breadth_per_identity": 1.0,
            },
            migrations={TIER3_FAST_VALIDATION_DEFAULT_MIGRATION: True},
        )

        tier3 = local["tiers"]["tier3"]
        self.assertEqual(tier3["validation_slots_per_process"], 1)
        self.assertEqual(tier3["tcp_breadth_per_identity"], 1.0)

    def test_retired_bootstrap_switch_is_dropped_from_local_once(self) -> None:
        local = self._migrate_local_tier3({"enabled": True, "bootstrap_enabled": False})

        self.assertNotIn("bootstrap_enabled", local["tiers"]["tier3"])
        self.assertTrue(local["migrations"][TIER3_BOOTSTRAP_SWITCH_REMOVAL_MIGRATION])

        # After the one-time cleanup a hand-written value is the user's.
        local = self._migrate_local_tier3(
            {"enabled": True, "bootstrap_enabled": False},
            migrations={TIER3_BOOTSTRAP_SWITCH_REMOVAL_MIGRATION: True},
        )
        self.assertFalse(local["tiers"]["tier3"]["bootstrap_enabled"])

    def test_release_path_overrides_use_appdata_locations_and_resource_binary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            paths = build_app_paths(
                resource_root=self._resource_root(temp_path),
                config_dir=temp_path / "config",
                data_dir=temp_path / "data",
            )
            config = {
                "paths": {"sources_file": "sources.yaml"},
                "tiers": {"tier2": {"cache_file": "data/old-cache.json"}},
                "alerts": {"log_file": "data/monitor.log"},
                "client": {
                    "mihomo_binary_path": "bin/mihomo.exe",
                    "mihomo_work_dir": "data/mihomo",
                    "mihomo_runtime_config": "data/mihomo-runtime.yaml",
                    "runtime_state_path": "data/runtime-state.json",
                },
            }

            patched = apply_release_path_overrides(config, paths)

            self.assertEqual(patched["paths"]["sources_file"], str(paths.sources_path))
            self.assertEqual(patched["paths"]["tier3_nodes_file"], str(paths.tier3_nodes_file))
            self.assertEqual(
                patched["paths"]["tier3_control_state_path"],
                str(paths.tier3_control_state_path),
            )
            self.assertEqual(patched["tiers"]["tier2"]["cache_file"], str(paths.tier2_cache_file))
            self.assertEqual(patched["alerts"]["log_file"], str(paths.log_file))
            self.assertEqual(patched["client"]["mihomo_binary_path"], str(paths.mihomo_binary_path))
            self.assertEqual(patched["client"]["mihomo_work_dir"], str(paths.mihomo_work_dir))
            self.assertEqual(patched["client"]["runtime_state_path"], str(paths.runtime_state_path))
            self.assertEqual(config["client"]["mihomo_binary_path"], "bin/mihomo.exe")


if __name__ == "__main__":
    unittest.main()


class DnsResolverDefaultsMigrationTests(unittest.TestCase):
    LEGACY_DNS = """client:
  dns:
    enable: true
    respect_rules: false
    ipv6: false
    fake_ip_filter:
    - '*.lan'
    - '*.local'
    - '+.msftconnecttest.com'
    - '+.msftncsi.com'
    - localhost.ptlogin2.qq.com
    nameserver:
    - https://doh.pub/dns-query
    - https://dns.alidns.com/dns-query
    fallback: []
    default_nameserver:
    - 223.5.5.5
    - 119.29.29.29
    - 114.114.114.114
    proxy_server_nameserver:
    - 223.5.5.5
    - 119.29.29.29
    - 114.114.114.114
"""

    def _paths(self, temp_path: Path):
        import shutil

        resource = temp_path / "resource"
        resource.mkdir()
        shutil.copy2(Path("config.example.yaml"), resource / "config.example.yaml")
        shutil.copy2(Path("sources.example.yaml"), resource / "sources.example.yaml")
        paths = build_app_paths(
            resource_root=resource,
            config_dir=temp_path / "config",
            data_dir=temp_path / "data",
        )
        paths.config_dir.mkdir()
        return paths

    def test_bootstrap_turns_legacy_lists_into_mode_auto_and_keeps_custom_lists(self) -> None:
        from app_paths import DNS_RESOLVER_DEFAULTS_MIGRATION

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            paths = self._paths(temp_path)
            paths.config_path.write_text(self.LEGACY_DNS, encoding="utf-8")
            paths.local_config_path.write_text(
                "client:\n  dns:\n    nameserver:\n    - tls://dns.example\n    default_nameserver:\n    - 192.0.2.53\n",
                encoding="utf-8",
            )

            bootstrap_user_files(paths)
            base = yaml.safe_load(paths.config_path.read_text(encoding="utf-8"))
            first_local_text = paths.local_config_path.read_text(encoding="utf-8")
            local = yaml.safe_load(first_local_text)

            base_dns = base["client"]["dns"]
            self.assertEqual(base_dns["mode"], "auto")
            for key in ("nameserver", "default_nameserver", "proxy_server_nameserver", "fallback"):
                self.assertNotIn(key, base_dns)
            self.assertNotIn("fake_ip_filter", base_dns)
            self.assertTrue(base_dns["enable"])
            local_dns = local["client"]["dns"]
            self.assertEqual(local_dns["mode"], "custom")
            self.assertEqual(local_dns["nameserver"], ["tls://dns.example"])
            self.assertEqual(local_dns["default_nameserver"], ["192.0.2.53"])
            self.assertTrue(local["migrations"][DNS_RESOLVER_DEFAULTS_MIGRATION])

            bootstrap_user_files(paths)
            self.assertEqual(paths.local_config_path.read_text(encoding="utf-8"), first_local_text)

    def test_bootstrap_reclassifies_shipped_doh_proxy_pair_filed_as_custom_by_v2(self) -> None:
        from app_paths import DNS_RESOLVER_DEFAULTS_MIGRATION

        shipped_pair = self.LEGACY_DNS.replace(
            "    proxy_server_nameserver:\n    - 223.5.5.5\n    - 119.29.29.29\n    - 114.114.114.114\n",
            "    proxy_server_nameserver:\n    - https://1.1.1.1/dns-query\n    - https://8.8.8.8/dns-query\n"
            "    mode: custom\n",
        )
        self.assertIn("mode: custom", shipped_pair)
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            paths = self._paths(temp_path)
            paths.config_path.write_text(shipped_pair, encoding="utf-8")
            paths.local_config_path.write_text(
                "migrations:\n  dns_resolver_defaults_v2: true\n", encoding="utf-8",
            )

            bootstrap_user_files(paths)
            base = yaml.safe_load(paths.config_path.read_text(encoding="utf-8"))
            base_dns = base["client"]["dns"]
            self.assertEqual(base_dns["mode"], "auto")
            for key in ("nameserver", "default_nameserver", "proxy_server_nameserver", "fallback"):
                self.assertNotIn(key, base_dns)
            local = yaml.safe_load(paths.local_config_path.read_text(encoding="utf-8"))
            self.assertTrue(local["migrations"][DNS_RESOLVER_DEFAULTS_MIGRATION])

    def test_bootstrap_keeps_a_user_chosen_system_or_encrypted_only_mode(self) -> None:
        for chosen in ("system", "encrypted_only"):
            with tempfile.TemporaryDirectory() as temp_dir:
                temp_path = Path(temp_dir)
                paths = self._paths(temp_path)
                paths.config_path.write_text(
                    self.LEGACY_DNS.replace("    enable: true\n", f"    enable: true\n    mode: {chosen}\n"),
                    encoding="utf-8",
                )
                bootstrap_user_files(paths)
                base = yaml.safe_load(paths.config_path.read_text(encoding="utf-8"))
                self.assertEqual(base["client"]["dns"]["mode"], chosen)

    def test_bootstrap_maps_encrypted_only_flag_to_mode_and_leaves_fresh_installs_alone(self) -> None:
        from app_paths import DNS_RESOLVER_DEFAULTS_MIGRATION

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            paths = self._paths(temp_path)
            paths.config_path.write_text(
                self.LEGACY_DNS.replace("    enable: true\n", "    enable: true\n    encrypted_only: true\n"),
                encoding="utf-8",
            )

            bootstrap_user_files(paths)
            base = yaml.safe_load(paths.config_path.read_text(encoding="utf-8"))
            self.assertEqual(base["client"]["dns"]["mode"], "encrypted_only")
            self.assertNotIn("encrypted_only", base["client"]["dns"])
            local = yaml.safe_load(paths.local_config_path.read_text(encoding="utf-8"))
            self.assertTrue(local["migrations"][DNS_RESOLVER_DEFAULTS_MIGRATION])

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            paths = self._paths(temp_path)
            bootstrap_user_files(paths)
            base = yaml.safe_load(paths.config_path.read_text(encoding="utf-8"))
            self.assertEqual(base["client"]["dns"]["mode"], "auto")
            self.assertNotIn("nameserver", base["client"]["dns"])
