import unittest

from killswitch_config_view import (
    KillswitchConfigFormState,
    KillswitchWhitelistFormState,
    apply_killswitch_config_payload,
    apply_killswitch_enabled_payload,
    apply_killswitch_whitelist_payload,
    effective_killswitch_config,
    killswitch_form_state,
    list_entries,
    parse_whitelist_text,
)


class KillswitchConfigViewTests(unittest.TestCase):
    def test_effective_killswitch_config_merges_local_over_runtime(self) -> None:
        merged = effective_killswitch_config(
            {"killswitch": {"enabled": True, "allow_lan": True, "allow_dns": False}},
            {"killswitch": {"enabled": False, "dns_whitelist": ["1.1.1.1"]}},
        )

        self.assertEqual(
            merged,
            {"enabled": False, "allow_lan": True, "allow_dns": False, "dns_whitelist": ["1.1.1.1"]},
        )

    def test_killswitch_form_state_normalizes_booleans_and_lists(self) -> None:
        state = killswitch_form_state(
            {"killswitch": {"enabled": True, "allow_lan": True}},
            {
                "killswitch": {
                    "enabled": "false",
                    "allow_dns": "true",
                    "additional_whitelist": "example.com\n  internal.local  \n",
                    "dns_whitelist": [" 1.1.1.1 ", "", None],
                }
            },
        )

        self.assertFalse(state.enabled)
        self.assertTrue(state.allow_lan)
        self.assertTrue(state.allow_dns)
        self.assertEqual(state.additional_whitelist, ["example.com", "internal.local"])
        self.assertEqual(state.dns_whitelist, ["1.1.1.1"])

    def test_killswitch_form_state_uses_safe_defaults_for_bad_sections(self) -> None:
        state = killswitch_form_state({"killswitch": "bad"}, {"killswitch": "also bad"})

        self.assertFalse(state.enabled)
        self.assertTrue(state.allow_lan)
        self.assertFalse(state.allow_dns)
        self.assertEqual(state.additional_whitelist, [])
        self.assertEqual(state.dns_whitelist, [])

    def test_apply_killswitch_config_payload_replaces_invalid_section_and_copies_lists(self) -> None:
        additional = ["example.com"]
        dns = ["1.1.1.1"]
        payload = {"killswitch": "bad"}
        state = KillswitchConfigFormState(False, True, True, additional, dns)

        result = apply_killswitch_config_payload(payload, state)
        additional.append("mutated")
        dns.append("mutated")

        self.assertIs(result, payload)
        self.assertEqual(
            payload["killswitch"],
            {
                "enabled": False,
                "allow_lan": True,
                "allow_dns": True,
                "additional_whitelist": ["example.com"],
                "dns_whitelist": ["1.1.1.1"],
            },
        )

    def test_apply_killswitch_enabled_payload_preserves_whitelist_and_unknown_keys(self) -> None:
        payload = {
            "killswitch": {
                "enabled": True,
                "allow_lan": False,
                "allow_dns": True,
                "additional_whitelist": ["203.0.113.5"],
                "dns_whitelist": ["1.1.1.1"],
                "future_option": {"mode": "strict"},
            },
            "client": {"ipv6": True},
        }

        result = apply_killswitch_enabled_payload(payload, False)

        self.assertIs(result, payload)
        self.assertEqual(
            payload,
            {
                "killswitch": {
                    "enabled": False,
                    "allow_lan": False,
                    "allow_dns": True,
                    "additional_whitelist": ["203.0.113.5"],
                    "dns_whitelist": ["1.1.1.1"],
                    "future_option": {"mode": "strict"},
                },
                "client": {"ipv6": True},
            },
        )

    def test_apply_killswitch_whitelist_payload_preserves_enabled_and_copies_lists(self) -> None:
        additional = ["203.0.113.5"]
        dns = ["1.1.1.1"]
        payload = {
            "killswitch": {
                "enabled": False,
                "allow_lan": True,
                "allow_dns": False,
                "additional_whitelist": ["old.example"],
                "dns_whitelist": ["8.8.8.8"],
                "future_option": "preserved",
            }
        }
        state = KillswitchWhitelistFormState(
            allow_lan=False,
            allow_dns=True,
            additional_whitelist=additional,
            dns_whitelist=dns,
        )

        result = apply_killswitch_whitelist_payload(payload, state)
        additional.append("mutated")
        dns.append("mutated")

        self.assertIs(result, payload)
        self.assertEqual(
            payload["killswitch"],
            {
                "enabled": False,
                "allow_lan": False,
                "allow_dns": True,
                "additional_whitelist": ["203.0.113.5"],
                "dns_whitelist": ["1.1.1.1"],
                "future_option": "preserved",
            },
        )

    def test_scoped_killswitch_updaters_compose_without_overwriting_each_other(self) -> None:
        state = KillswitchWhitelistFormState(
            allow_lan=False,
            allow_dns=True,
            additional_whitelist=["203.0.113.5"],
            dns_whitelist=["1.1.1.1"],
        )

        enabled_then_whitelist = {"killswitch": {"enabled": True}}
        apply_killswitch_enabled_payload(enabled_then_whitelist, False)
        apply_killswitch_whitelist_payload(enabled_then_whitelist, state)

        whitelist_then_enabled = {"killswitch": {"enabled": True}}
        apply_killswitch_whitelist_payload(whitelist_then_enabled, state)
        apply_killswitch_enabled_payload(whitelist_then_enabled, False)

        self.assertEqual(enabled_then_whitelist, whitelist_then_enabled)
        self.assertFalse(enabled_then_whitelist["killswitch"]["enabled"])
        self.assertEqual(
            enabled_then_whitelist["killswitch"]["additional_whitelist"],
            ["203.0.113.5"],
        )

    def test_list_entries_splits_strings_and_ignores_blank_values(self) -> None:
        self.assertEqual(list_entries(" one \n\n two "), ["one", "two"])
        self.assertEqual(list_entries([" one ", "", None, "two"]), ["one", "two"])

    def test_parse_whitelist_text_splits_common_separators(self) -> None:
        entries, invalid = parse_whitelist_text(
            " 203.0.113.5、2001:db8::/32，192.0.2.10-192.0.2.20;localsubnet；DNS"
            "\n\n 203.0.113.5 　 10.0.0.0/8 "
        )

        self.assertEqual(
            entries,
            ["203.0.113.5", "2001:db8::/32", "192.0.2.10-192.0.2.20", "localsubnet", "DNS", "10.0.0.0/8"],
        )
        self.assertEqual(invalid, [])

    def test_parse_whitelist_text_reports_invalid_additional_entries_in_order(self) -> None:
        entries, invalid = parse_whitelist_text(
            "nas.local\n203.0.113.5\nany\n192.0.2.20-192.0.2.10\nfe80::1%12\nnas.local"
        )

        self.assertEqual(entries, ["203.0.113.5"])
        self.assertEqual(invalid, ["nas.local", "any", "192.0.2.20-192.0.2.10", "fe80::1%12"])

    def test_parse_dns_whitelist_text_accepts_only_ip_addresses(self) -> None:
        entries, invalid = parse_whitelist_text(
            "1.1.1.1、8.8.8.8\n2606:4700:4700::1111\nLocalSubnet\ndns.google\n10.0.0.1-10.0.0.9",
            dns=True,
        )

        self.assertEqual(entries, ["1.1.1.1", "8.8.8.8", "2606:4700:4700::1111"])
        self.assertEqual(invalid, ["LocalSubnet", "dns.google", "10.0.0.1-10.0.0.9"])


if __name__ == "__main__":
    unittest.main()
