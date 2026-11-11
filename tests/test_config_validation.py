import unittest

from config_validation import validate_local_config, validate_sources_config, validate_tier1_node, validate_url


class ConfigValidationTests(unittest.TestCase):
    def test_validate_tier1_node_preserves_port_bounds(self) -> None:
        validate_tier1_node({"name": "node", "address": "203.0.113.1", "port": 443})
        # Reachability belongs to the self-check, not config validation. Keep
        # an unreachable configured node editable instead of rejecting the
        # whole user configuration.
        validate_tier1_node({"name": "1", "address": "1", "port": 1})

        with self.assertRaisesRegex(ValueError, "tier1 node port must be between 1 and 65535"):
            validate_tier1_node({"name": "node", "address": "203.0.113.1", "port": 70000})

    def test_validate_sources_accepts_legacy_source_entry_shapes(self) -> None:
        validate_sources_config(
            {
                "github_repos": [{"url": "https://example.com/free.txt"}],
                "subscription_urls": [{"value": "https://example.com/sub"}],
                "telegram_channels": [{"value": "@channel"}],
                "local_files": [{"path": "C:/subs/free.txt"}],
            }
        )

    def test_validate_sources_rejects_invalid_url_sections(self) -> None:
        with self.assertRaisesRegex(ValueError, "URL must be http or https"):
            validate_sources_config({"github_repos": [{"path": "C:/subs/free.txt"}]})

    def test_validate_local_rejects_legacy_tier3_bootstrap_sources(self) -> None:
        with self.assertRaisesRegex(ValueError, "tiers.tier3.bootstrap_sources has moved to sources.yaml"):
            validate_local_config({"tiers": {"tier3": {"bootstrap_sources": "bad"}}})

    def test_validate_local_accepts_missing_or_valid_latency_policy(self) -> None:
        validate_local_config({})
        validate_local_config({"testing": {}})
        validate_local_config({"testing": {"primary_gateway_max_latency_ms": 100}})
        validate_local_config({"testing": {"primary_gateway_max_latency_ms": 5000}})

    def test_validate_local_requires_testing_mapping_and_strict_latency_integer(self) -> None:
        with self.assertRaisesRegex(ValueError, "testing must be an object"):
            validate_local_config({"testing": None})
        for value in (99, 5001, "800", 800.0, True):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "must be an integer between 100 and 5000"):
                    validate_local_config(
                        {"testing": {"primary_gateway_max_latency_ms": value}}
                    )

    def test_validate_url_keeps_http_https_only(self) -> None:
        validate_url("https://example.com/sub")

        with self.assertRaisesRegex(ValueError, "URL must be http or https"):
            validate_url("ftp://example.com/sub")


if __name__ == "__main__":
    unittest.main()


class DnsLocalValidationTests(unittest.TestCase):
    def test_dns_mode_must_be_known(self) -> None:
        from config_validation import validate_dns_local

        validate_dns_local(None)
        validate_dns_local({"mode": "auto"})
        validate_dns_local({"mode": "encrypted-only"})
        with self.assertRaisesRegex(ValueError, "client.dns.mode"):
            validate_dns_local({"mode": "fastest"})
        with self.assertRaisesRegex(ValueError, "client.dns must be an object"):
            validate_dns_local("bad")

    def test_custom_mode_requires_literal_bootstrap_resolvers(self) -> None:
        from config_validation import validate_dns_local, validate_local_config

        validate_dns_local({"mode": "custom", "nameserver": ["tls://dns.example"], "default_nameserver": ["system"]})
        validate_dns_local({"nameserver": ["tls://dns.example"], "default_nameserver": ["192.0.2.53"]})
        with self.assertRaisesRegex(ValueError, "default_nameserver"):
            validate_dns_local({"mode": "custom", "default_nameserver": ["dns.example"]})
        with self.assertRaisesRegex(ValueError, "must be a list"):
            validate_dns_local({"mode": "custom", "nameserver": {"bad": True}})
        # Non-custom modes ignore the lists, so stale entries never block startup.
        validate_dns_local({"mode": "auto", "default_nameserver": ["dns.example"]})
        validate_local_config({"client": {"dns": {"mode": "system"}}})
