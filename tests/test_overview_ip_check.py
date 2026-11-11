from __future__ import annotations

import unittest
from typing import Any

import overview_ip_check
from overview_ip_check import _identity_observation_from_payload, _request_identity


class _Response:
    def __init__(self, payload: bytes, *, final_url: str) -> None:
        self.payload = payload
        self.final_url = final_url

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def geturl(self) -> str:
        return self.final_url

    def read(self, _limit: int) -> bytes:
        return self.payload


class _Opener:
    def __init__(self, response: _Response) -> None:
        self.response = response

    def open(self, _request: Any, timeout: float) -> _Response:
        self.timeout = timeout
        return self.response


class OverviewIpCheckTests(unittest.TestCase):
    def test_ipify_identity_accepts_json_and_plain_text(self) -> None:
        json_observation = _identity_observation_from_payload(
            "ipify",
            b'{"ip":"1.1.1.1"}',
            expected_family=4,
        )
        text_observation = _identity_observation_from_payload(
            "ipify",
            b"2606:4700:4700::1111\n",
            expected_family=6,
        )

        self.assertEqual(json_observation, {"ip": "1.1.1.1"})
        self.assertEqual(text_observation, {"ip": "2606:4700:4700::1111"})

    def test_ident_me_profile_keeps_only_declared_free_fields(self) -> None:
        observation = _identity_observation_from_payload(
            "ident.me",
            (
                b'{"ip":"1.1.1.1","asn":13335,"country":"United States",'
                b'"city":"Los Angeles","security":{"vpn":true},"risk":99}'
            ),
            expected_family=4,
        )

        self.assertEqual(observation["ip"], "1.1.1.1")
        self.assertEqual(
            observation["profile"],
            {
                "asn": 13335,
                "country": "United States",
                "city": "Los Angeles",
            },
        )
        self.assertNotIn("security", observation["profile"])
        self.assertNotIn("risk", observation["profile"])

    def test_identity_payload_requires_matching_public_address_family(self) -> None:
        for payload, family in (
            (b'{"ip":"10.0.0.1"}', 4),
            (b'{"ip":"1.1.1.1"}', 6),
            (b'{"query":"1.1.1.1"}', 4),
        ):
            with self.subTest(payload=payload, family=family), self.assertRaises(ValueError):
                _identity_observation_from_payload(
                    "ipify",
                    payload,
                    expected_family=family,
                )

    def test_identity_request_rejects_cross_host_https_redirect(self) -> None:
        opener = _Opener(
            _Response(
                b'{"ip":"1.1.1.1"}',
                final_url="https://redirected.test/v4",
            )
        )

        with self.assertRaisesRegex(ValueError, "redirected outside"):
            _request_identity(
                opener,
                "https://identity.test/v4",
                timeout_seconds=2.0,
            )

    def test_identity_request_rejects_https_downgrade_in_final_url(self) -> None:
        opener = _Opener(
            _Response(
                b'{"ip":"1.1.1.1"}',
                final_url="http://identity.test/v4",
            )
        )

        with self.assertRaisesRegex(ValueError, "redirected outside"):
            _request_identity(
                opener,
                "https://identity.test/v4",
                timeout_seconds=2.0,
            )

    def test_identity_request_rejects_response_over_eight_kibibytes(self) -> None:
        url = "https://identity.test/v4"
        opener = _Opener(_Response(b"x" * 8193, final_url=url))

        with self.assertRaisesRegex(ValueError, "exceeded the size limit"):
            _request_identity(opener, url, timeout_seconds=2.0)

    def test_legacy_config_and_single_source_probe_api_is_removed(self) -> None:
        for name in (
            "PUBLIC_IP_PROVIDERS",
            "IP_PROFILE_PROVIDERS",
            "DNS_PROBES",
            "provider_entries_from_config",
            "exit_check_providers_from_config",
            "detect_public_ip",
            "public_ip_from_payload",
            "public_ip_details_from_payload",
            "dns_probe_from_payload",
        ):
            with self.subTest(name=name):
                self.assertFalse(hasattr(overview_ip_check, name))


if __name__ == "__main__":
    unittest.main()
