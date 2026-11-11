from __future__ import annotations

import json
import threading
import unittest
from typing import Any

from overview_ip_check import EXIT_IDENTITY_PROVIDERS, collect_exit_identity


IPIFY_V4 = "https://ipify.test/v4"
IDENT_V4 = "https://ident.test/v4"
IPIFY_V6 = "https://ipify.test/v6"
IDENT_V6 = "https://ident.test/v6"


class _Response:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self, _limit: int) -> bytes:
        return self.payload


class _UrlOpener:
    def __init__(self, payloads: dict[str, list[bytes | Exception]]) -> None:
        self.payloads = {url: list(values) for url, values in payloads.items()}
        self.calls: list[tuple[str, float]] = []
        self._lock = threading.Lock()

    def open(self, request: Any, timeout: float) -> _Response:
        url = request.full_url
        with self._lock:
            self.calls.append((url, timeout))
            values = self.payloads.get(url)
            if not values:
                raise AssertionError(f"unexpected request for {url}")
            value = values.pop(0)
        if isinstance(value, Exception):
            raise value
        return _Response(value)


def _json_payload(ip: str, **extra: Any) -> bytes:
    return json.dumps({"ip": ip, **extra}).encode("utf-8")


class ExitIdentityTests(unittest.TestCase):
    def test_default_observers_cover_ipify_and_ident_me_for_both_families(self) -> None:
        self.assertEqual(
            EXIT_IDENTITY_PROVIDERS,
            (
                ("ipify", 4, "https://api.ipify.org?format=json"),
                ("ident.me", 4, "https://4.ident.me/json"),
                ("ipify", 6, "https://api6.ipify.org?format=json"),
                ("ident.me", 6, "https://6.ident.me/json"),
            ),
        )

    def test_two_independent_sources_confirm_ipv4_without_double_counting_rounds(self) -> None:
        opener = _UrlOpener(
            {
                IPIFY_V4: [_json_payload("1.1.1.1"), _json_payload("1.1.1.1")],
                IDENT_V4: [_json_payload("1.1.1.1"), _json_payload("1.1.1.1")],
            }
        )

        result = collect_exit_identity(
            opener=opener,
            providers=(("ipify", 4, IPIFY_V4), ("ident.me", 4, IDENT_V4)),
            clock=lambda: 123.0,
        )

        ipv4 = result["identity"]["ipv4"]
        self.assertEqual(ipv4["state"], "confirmed")
        self.assertEqual(ipv4["confidence"], "medium")
        self.assertEqual(ipv4["ip"], "1.1.1.1")
        self.assertEqual(ipv4["agreement"]["agreeing_sources"], 2)
        self.assertEqual(ipv4["agreement"]["successful_sources"], 2)
        self.assertEqual(ipv4["agreement"]["stable_sources"], 2)
        self.assertEqual(result["checked_at"], 123.0)
        self.assertEqual(result["identity"]["ipv6"]["state"], "unavailable")
        self.assertNotIn("score", result)
        self.assertNotIn("score_label", result)

    def test_stable_sources_with_different_addresses_are_a_conflict(self) -> None:
        opener = _UrlOpener(
            {
                IPIFY_V4: [_json_payload("1.1.1.1"), _json_payload("1.1.1.1")],
                IDENT_V4: [_json_payload("8.8.8.8"), _json_payload("8.8.8.8")],
            }
        )

        ipv4 = collect_exit_identity(
            opener=opener,
            providers=(("ipify", 4, IPIFY_V4), ("ident.me", 4, IDENT_V4)),
        )["identity"]["ipv4"]

        self.assertEqual(ipv4["state"], "conflict")
        self.assertEqual(ipv4["confidence"], "low")
        self.assertIsNone(ipv4["ip"])
        self.assertEqual(ipv4["observed_ips"], ["1.1.1.1", "8.8.8.8"])
        self.assertEqual(ipv4["agreement"]["agreeing_sources"], 1)

    def test_address_change_between_rounds_is_rotating(self) -> None:
        opener = _UrlOpener(
            {
                IPIFY_V4: [_json_payload("1.1.1.1"), _json_payload("8.8.8.8")],
                IDENT_V4: [_json_payload("1.1.1.1"), _json_payload("1.1.1.1")],
            }
        )

        ipv4 = collect_exit_identity(
            opener=opener,
            providers=(("ipify", 4, IPIFY_V4), ("ident.me", 4, IDENT_V4)),
        )["identity"]["ipv4"]

        self.assertEqual(ipv4["state"], "rotating")
        self.assertEqual(ipv4["confidence"], "low")
        self.assertIsNone(ipv4["ip"])
        self.assertEqual(ipv4["agreement"]["rotating_sources"], 1)
        self.assertEqual(ipv4["evidence"][0]["stability"], "rotating")

    def test_one_working_source_is_unconfirmed(self) -> None:
        opener = _UrlOpener(
            {
                IPIFY_V4: [_json_payload("1.1.1.1"), _json_payload("1.1.1.1")],
                IDENT_V4: [TimeoutError("timed out"), TimeoutError("timed out")],
            }
        )

        ipv4 = collect_exit_identity(
            opener=opener,
            providers=(("ipify", 4, IPIFY_V4), ("ident.me", 4, IDENT_V4)),
        )["identity"]["ipv4"]

        self.assertEqual(ipv4["state"], "unconfirmed")
        self.assertEqual(ipv4["confidence"], "low")
        self.assertEqual(ipv4["ip"], "1.1.1.1")
        self.assertEqual(ipv4["agreement"]["successful_sources"], 1)
        self.assertEqual(
            [attempt["status"] for attempt in ipv4["evidence"][1]["attempts"]],
            ["error", "error"],
        )

    def test_two_partial_sources_do_not_claim_stable_confirmation(self) -> None:
        opener = _UrlOpener(
            {
                IPIFY_V4: [_json_payload("1.1.1.1"), TimeoutError("timed out")],
                IDENT_V4: [_json_payload("1.1.1.1"), RuntimeError("offline")],
            }
        )

        ipv4 = collect_exit_identity(
            opener=opener,
            providers=(("ipify", 4, IPIFY_V4), ("ident.me", 4, IDENT_V4)),
        )["identity"]["ipv4"]

        self.assertEqual(ipv4["state"], "unconfirmed")
        self.assertEqual(ipv4["ip"], "1.1.1.1")
        self.assertEqual(ipv4["agreement"]["agreeing_sources"], 2)
        self.assertEqual(ipv4["agreement"]["stable_agreeing_sources"], 0)

    def test_duplicate_provider_entries_cannot_manufacture_consensus(self) -> None:
        duplicate_url = "https://duplicate.test/v4"
        opener = _UrlOpener(
            {IPIFY_V4: [_json_payload("1.1.1.1"), _json_payload("1.1.1.1")]}
        )

        ipv4 = collect_exit_identity(
            opener=opener,
            providers=(
                ("ipify", 4, IPIFY_V4),
                ("ipify", 4, duplicate_url),
            ),
        )["identity"]["ipv4"]

        self.assertEqual(ipv4["state"], "unconfirmed")
        self.assertEqual(ipv4["agreement"]["attempted_sources"], 1)
        self.assertEqual(len(opener.calls), 2)

    def test_ipv4_and_ipv6_vote_independently(self) -> None:
        opener = _UrlOpener(
            {
                IPIFY_V4: [_json_payload("1.1.1.1"), _json_payload("1.1.1.1")],
                IDENT_V4: [_json_payload("1.1.1.1"), _json_payload("1.1.1.1")],
                IPIFY_V6: [
                    _json_payload("2606:4700:4700::1111"),
                    _json_payload("2606:4700:4700::1111"),
                ],
                IDENT_V6: [
                    _json_payload("2606:4700:4700::1111"),
                    _json_payload("2606:4700:4700::1111"),
                ],
            }
        )

        identity = collect_exit_identity(
            opener=opener,
            providers=(
                ("ipify", 4, IPIFY_V4),
                ("ident.me", 4, IDENT_V4),
                ("ipify", 6, IPIFY_V6),
                ("ident.me", 6, IDENT_V6),
            ),
        )["identity"]

        self.assertEqual(identity["ipv4"]["state"], "confirmed")
        self.assertEqual(identity["ipv4"]["ip"], "1.1.1.1")
        self.assertEqual(identity["ipv6"]["state"], "confirmed")
        self.assertEqual(identity["ipv6"]["ip"], "2606:4700:4700::1111")

    def test_ident_me_profile_stays_attached_to_its_observed_ip(self) -> None:
        profile = {
            "aso": "CLOUDFLARENET",
            "asn": 13335,
            "type": "hosting",
            "cc": "US",
            "country": "United States",
            "city": "Los Angeles",
            "tz": "America/Los_Angeles",
        }
        opener = _UrlOpener(
            {
                IDENT_V4: [
                    _json_payload("1.1.1.1", **profile),
                    _json_payload("1.1.1.1", **profile),
                ],
            }
        )

        ipv4 = collect_exit_identity(
            opener=opener,
            providers=(("ident.me", 4, IDENT_V4),),
        )["identity"]["ipv4"]

        evidence = ipv4["evidence"][0]
        self.assertEqual(
            evidence["profile"],
            {
                "aso": "CLOUDFLARENET",
                "asn": 13335,
                "cc": "US",
                "country": "United States",
                "city": "Los Angeles",
                # Kept since 09-23 (user decision): shown as source-reported.
                "tz": "America/Los_Angeles",
                "type": "hosting",
            },
        )
        self.assertEqual(evidence["profiles"]["1.1.1.1"], evidence["profile"])
        self.assertEqual(evidence["attempts"][0]["profile"], evidence["profile"])
        self.assertNotIn("security", evidence["profile"])
        self.assertNotIn("risk_flags", evidence)

    def test_non_public_addresses_never_enter_a_vote(self) -> None:
        invalid_addresses = (
            "10.0.0.1",
            "100.64.0.1",
            "127.0.0.1",
            "169.254.1.1",
            "224.0.0.1",
            "240.0.0.1",
            "192.0.2.1",
            "fc00::1",
            "::1",
            "fe80::1",
            "ff02::1",
            "2001:db8::1",
            "fec0::1",
            "::ffff:8.8.8.8",
        )

        for address in invalid_addresses:
            with self.subTest(address=address):
                family = 6 if ":" in address else 4
                url = f"https://invalid.test/v{family}"
                opener = _UrlOpener(
                    {url: [_json_payload(address), _json_payload(address)]}
                )

                result = collect_exit_identity(
                    opener=opener,
                    providers=(("invalid", family, url),),
                )["identity"][f"ipv{family}"]

                self.assertEqual(result["state"], "unavailable")
                self.assertEqual(result["confidence"], "none")
                self.assertEqual(result["observed_ips"], [])
                self.assertEqual(result["agreement"]["successful_sources"], 0)
                self.assertEqual(
                    [attempt["status"] for attempt in result["evidence"][0]["attempts"]],
                    ["invalid", "invalid"],
                )

    def test_network_errors_and_timeouts_are_preserved_as_source_evidence(self) -> None:
        opener = _UrlOpener(
            {
                IPIFY_V4: [RuntimeError("offline"), RuntimeError("offline")],
                IDENT_V4: [TimeoutError("timed out"), TimeoutError("timed out")],
            }
        )

        ipv4 = collect_exit_identity(
            opener=opener,
            providers=(("ipify", 4, IPIFY_V4), ("ident.me", 4, IDENT_V4)),
        )["identity"]["ipv4"]

        self.assertEqual(ipv4["state"], "unavailable")
        self.assertEqual(ipv4["confidence"], "none")
        self.assertEqual(ipv4["agreement"]["successful_sources"], 0)
        errors = [
            attempt["error"]
            for source in ipv4["evidence"]
            for attempt in source["attempts"]
        ]
        self.assertTrue(any("RuntimeError" in error for error in errors))
        self.assertTrue(any("TimeoutError" in error for error in errors))

    def test_opener_factory_receives_proxy_for_each_round(self) -> None:
        opener = _UrlOpener(
            {IPIFY_V4: [_json_payload("1.1.1.1"), _json_payload("1.1.1.1")]}
        )
        factory_calls: list[str | None] = []

        def opener_factory(proxy_url: str | None) -> _UrlOpener:
            factory_calls.append(proxy_url)
            return opener

        result = collect_exit_identity(
            proxy_url="http://127.0.0.1:7890",
            opener_factory=opener_factory,
            providers=(("ipify", 4, IPIFY_V4),),
        )

        self.assertEqual(result["proxy"], "http://127.0.0.1:7890")
        self.assertEqual(factory_calls, ["http://127.0.0.1:7890"] * 2)


if __name__ == "__main__":
    unittest.main()
