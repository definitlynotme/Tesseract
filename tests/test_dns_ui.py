from __future__ import annotations

import unittest

from dns_ui import (
    DNS_MODE_LABELS,
    DNS_MODE_ORDER,
    dns_entries_for_ui,
    dns_entries_from_value,
    dns_mode_for_ui,
    dns_resolvers_from_rows,
    dns_rows_for_ui,
    validate_plain_dns_ips,
)


class DnsUiTests(unittest.TestCase):
    def test_splits_string_and_list_values(self) -> None:
        self.assertEqual(
            dns_entries_from_value("https://dns.example/dns-query, 1.1.1.1\n8.8.8.8"),
            ["https://dns.example/dns-query", "1.1.1.1", "8.8.8.8"],
        )
        self.assertEqual(dns_entries_from_value(["", "9.9.9.9", None]), ["9.9.9.9"])

    def test_dns_entries_for_ui_accepts_hyphenated_config_keys(self) -> None:
        self.assertEqual(
            dns_entries_for_ui({"default-nameserver": ["223.5.5.5"]}, "default_nameserver"),
            ["223.5.5.5"],
        )

    def test_dns_entries_for_ui_never_invents_resolvers(self) -> None:
        # An empty field stays empty: the mode selects the policy, and opening
        # Settings must not pin any resolver into the user's config.
        self.assertEqual(dns_entries_for_ui({}, "default_nameserver"), [])
        self.assertEqual(dns_rows_for_ui({}), [])

    def test_dns_mode_for_ui_reads_explicit_mode_or_derives_it(self) -> None:
        self.assertEqual(dns_mode_for_ui({}), "auto")
        self.assertEqual(dns_mode_for_ui("bad"), "auto")
        self.assertEqual(dns_mode_for_ui({"mode": "System"}), "system")
        self.assertEqual(dns_mode_for_ui({"mode": "encrypted-only"}), "encrypted_only")
        self.assertEqual(dns_mode_for_ui({"encrypted_only": True}), "encrypted_only")
        self.assertEqual(dns_mode_for_ui({"nameserver": ["tls://dns.example"]}), "custom")
        self.assertEqual(
            dns_mode_for_ui({
                "nameserver": ["https://doh.pub/dns-query", "https://dns.alidns.com/dns-query"],
                "default_nameserver": ["223.5.5.5", "119.29.29.29", "114.114.114.114"],
            }),
            "auto",
        )
        self.assertEqual(tuple(DNS_MODE_LABELS), DNS_MODE_ORDER)

    def test_dns_rows_for_ui_preserves_field_identity(self) -> None:
        self.assertEqual(
            dns_rows_for_ui({"proxy_server_nameserver": ["9.9.9.9"]})[-1],
            {"type": "proxy_server_nameserver", "value": "9.9.9.9"},
        )

    def test_dns_resolvers_from_rows_preserves_fields_and_deduplicates(self) -> None:
        self.assertEqual(
            dns_resolvers_from_rows(
                [
                    {"type": "nameserver", "value": "https://dns.example/dns-query"},
                    {"type": "nameserver", "value": "https://dns.example/dns-query"},
                    {"type": "default_nameserver", "value": " 223.5.5.5 "},
                    {"type": "unknown", "value": "ignored"},
                    {"type": "proxy_server_nameserver", "value": ""},
                    "bad",
                ]
            ),
            {
                "nameserver": ["https://dns.example/dns-query"],
                "default_nameserver": ["223.5.5.5"],
                "proxy_server_nameserver": [],
            },
        )

    def test_validate_plain_dns_ips_accepts_ip_literals_and_system(self) -> None:
        self.assertTrue(validate_plain_dns_ips(["223.5.5.5", "2001:4860:4860::8888"]))
        self.assertTrue(validate_plain_dns_ips(["system"]))
        self.assertFalse(validate_plain_dns_ips(["https://dns.example/dns-query"]))
        self.assertFalse(validate_plain_dns_ips([]))


if __name__ == "__main__":
    unittest.main()
