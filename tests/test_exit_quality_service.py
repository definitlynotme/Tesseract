import json
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path

from exit_quality_service import (
    DEFAULT_DATASET_SPECS,
    DatasetSpec,
    DatasetRefreshCancelled,
    DatasetUpdater,
    ExitQualityService,
    HttpResponse,
    SourceEvidence,
    _SourceCache,
    _conclusion,
    _conclusion_confidence,
    _free_check_groups,
    _quality_score,
    _normalize_identity,
    _onionoo_lookup,
    _source_evidence,
    _team_cymru_lookup,
    _tri_state,
    parse_feodo_active_c2,
    parse_onionoo_exact_ip,
    parse_proxycheck_response,
    parse_spamhaus_asns,
    parse_spamhaus_networks,
    parse_team_cymru_doh,
)
from exit_quality_endpoints import AZURE_SERVICE_TAGS_PAGE_URL


class MutableClock:
    def __init__(self, value: float = 1_800_000_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def confirmed_identity(ip: str = "1.1.1.1") -> dict:
    return {
        "schema_version": 2,
        "checked_at": "2027-01-15T08:00:00+00:00",
        "rounds": 2,
        "identity": {
            "ipv4": {
                "family": 4,
                "state": "confirmed",
                "confidence": "medium",
                "ip": ip,
                "observed_ips": [ip],
                "agreement": {"stable_agreeing_sources": 2},
                "evidence": [{"source": "one"}, {"source": "two"}],
            },
            "ipv6": {
                "family": 6,
                "state": "unavailable",
                "confidence": "none",
                "ip": None,
                "observed_ips": [],
                "agreement": {},
                "evidence": [],
            },
        },
    }


def stable_route(*, marker: str = "route-a", confirmed: bool = True) -> dict:
    return {
        "route_marker": marker,
        "selected_chain": ["FINAL-EXIT", "node-a"],
        "leaf_proxy": "node-a",
        "path_kind": "proxy",
        "core_instance": "core-1",
        "confirmed": confirmed,
    }


def online_http_get(url: str, **_kwargs) -> HttpResponse:
    if "dns-query" in url:
        body = {
            "Status": 0,
            "Answer": [{"data": '"13335 | 1.1.1.0/24 | US | arin | 2010-01-01"'}],
        }
    elif "onionoo" in url:
        body = {"relays": []}
    elif "proxycheck" in url:
        body = {
            "status": "ok",
            "1.1.1.1": {
                "network": {"asn": "AS13335", "provider": "Example"},
                "detections": {
                    "proxy": False,
                    "vpn": False,
                    "compromised": False,
                    "scraper": False,
                    "tor": False,
                    "hosting": False,
                    "anonymous": False,
                    "risk": 0,
                    "confidence": 100,
                },
            },
        }
    else:
        raise AssertionError(f"unexpected request: {url}")
    return HttpResponse(200, json.dumps(body).encode(), {}, url)


def spamhaus_payload(*records: dict) -> bytes:
    rows = [*records, {
        "type": "metadata",
        "timestamp": 1_800_000_000,
        "records": len(records),
        "copyright": "(c) 2027 The Spamhaus Project SLU",
    }]
    return ("\n".join(json.dumps(row, separators=(",", ":")) for row in rows) + "\n").encode()


def evidence(source: str, *, positive: bool | None, status: str, data: dict | None = None) -> SourceEvidence:
    return SourceEvidence(
        source=source,
        status=status,
        observed_at="2027-01-15T08:00:00+00:00",
        expires_at="2027-01-15T09:00:00+00:00",
        positive=positive,
        data=data,
    )


class ExitQualityParserTests(unittest.TestCase):
    def test_team_cymru_multiple_origin_asns_are_preserved_as_conflict(self) -> None:
        parsed = parse_team_cymru_doh(
            {
                "Status": 0,
                "Answer": [
                    {"data": '"64500 64501 | 1.1.1.0/24 | US | arin | 2010-01-01"'}
                ],
            }
        )

        self.assertEqual(parsed["asn"], "")
        self.assertEqual(parsed["asns"], ["AS64500", "AS64501"])
        self.assertTrue(parsed["asn_conflict"])

    def test_onionoo_uses_only_exact_ipv4_exit_addresses(self) -> None:
        payload = {
            "relays": [
                {
                    "fingerprint": "relay-a",
                    "or_addresses": ["1.1.1.1:9001"],
                    "dir_address": "1.1.1.1:9030",
                    "exit_addresses": ["8.8.8.8"],
                }
            ],
            "bridges": [],
        }

        self.assertFalse(parse_onionoo_exact_ip(payload, "1.1.1.1")["tor"])
        self.assertTrue(parse_onionoo_exact_ip(payload, "8.8.8.8")["tor"])
        ipv6 = parse_onionoo_exact_ip(payload, "2606:4700:4700::1111")
        self.assertIsNone(ipv6["tor"])
        self.assertEqual(ipv6["coverage"], "ipv4_exit_addresses_only")

    def test_feodo_valid_empty_dataset_is_a_fresh_negative_candidate(self) -> None:
        self.assertEqual(parse_feodo_active_c2(b"[]"), [])
        self.assertEqual(parse_feodo_active_c2(b'{"data": []}'), [])
        with self.assertRaises(ValueError):
            parse_feodo_active_c2(b'{"message": "not a dataset"}')
        with self.assertRaises(ValueError):
            parse_feodo_active_c2(b'[{"unexpected": "schema drift"}]')

    def test_spamhaus_json_lines_parser_does_not_require_json_array(self) -> None:
        payload = spamhaus_payload(
            {"cidr": "192.0.2.0/24"},
            {"cidr": "2001:db8::/32"},
        )
        self.assertEqual(
            [item["value"] for item in parse_spamhaus_networks(payload)],
            ["192.0.2.0/24", "2001:db8::/32"],
        )
        with self.assertRaises(ValueError):
            parse_spamhaus_networks(b'{"cidr":"192.0.2.0/24"}\n{"cidr":')

    def test_proxycheck_rate_limit_is_structured_and_retryable(self) -> None:
        response = HttpResponse(
            429,
            b'{"status":"denied","message":"limit"}',
            {"retry-after": "120"},
            "https://proxycheck.io/v3/1.1.1.1",
        )
        result = parse_proxycheck_response(response, "1.1.1.1")
        self.assertEqual(result["status"], "rate_limited")
        self.assertEqual(result["error"]["retry_after_seconds"], 120)

    def test_proxycheck_proxy_denial_is_not_mislabeled_as_rate_limit(self) -> None:
        response = HttpResponse(
            429,
            b'{"status":"denied","message":"Blocked due to using a proxy server"}',
            {"retry-after": "3600"},
            "https://proxycheck.io/v3/1.1.1.1",
        )
        result = parse_proxycheck_response(response, "1.1.1.1")
        self.assertEqual(result["status"], "denied")
        self.assertEqual(result["error"]["code"], "proxy_denied")

    def test_proxycheck_missing_detection_fields_is_unknown_not_clean(self) -> None:
        response = HttpResponse(
            200,
            b'{"status":"ok","1.1.1.1":{"network":{"asn":"AS13335"},"detections":{}}}',
            {},
            "https://proxycheck.io/v3/1.1.1.1",
        )
        result = parse_proxycheck_response(response, "1.1.1.1")
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["error"]["code"], "incomplete_detection_data")
        self.assertIsNone(result["data"]["risk"])

    def test_tri_state_does_not_turn_mixed_unknown_and_negative_into_clean(self) -> None:
        self.assertIsNone(_tri_state([False, None]))
        self.assertFalse(_tri_state([False, False]))
        self.assertTrue(_tri_state([False, None, True]))

    def test_ident_profile_is_bound_to_the_confirmed_family_ip(self) -> None:
        payload = confirmed_identity()
        payload["identity"]["ipv4"]["evidence"] = [
            {
                "source": "ident.me",
                "observed_ips": ["1.1.1.1"],
                "profile": {"ip": "1.1.1.1", "asn": "AS13335 Cloudflare", "org": "Cloudflare"},
                "profiles": {
                    "1.1.1.1": {"ip": "1.1.1.1", "asn": "AS13335 Cloudflare", "org": "Cloudflare"},
                    "2.2.2.2": {"ip": "2.2.2.2", "asn": "AS64500 Wrong profile"},
                },
            },
            {
                "source": "unbound",
                "observed_ips": ["2.2.2.2"],
                "profile": {"ip": "2.2.2.2", "asn": "AS64501"},
            },
        ]
        normalized = _normalize_identity(payload)
        self.assertEqual(len(normalized["network_profiles"]), 1)
        self.assertEqual(normalized["network_profiles"][0]["ip"], "1.1.1.1")
        self.assertEqual(normalized["network"]["asn"], "AS13335 Cloudflare")

    def test_location_uses_only_ident_attempts_for_the_confirmed_ip(self) -> None:
        payload = confirmed_identity()
        payload["identity"]["ipv4"]["evidence"] = [
            {
                "source": "ident.me",
                "observed_ips": ["1.1.1.1"],
                "attempts": [
                    {
                        "round": 1,
                        "status": "ok",
                        "ip": "1.1.1.1",
                        "profile": {
                            "cc": "us",
                            "country": "United States",
                            "city": "Los Angeles",
                            "tz": "America/Los_Angeles",
                            "latitude": "34.0522",
                            "longitude": -118.2437,
                        },
                    },
                    {
                        "round": 2,
                        "status": "ok",
                        "ip": "1.1.1.1",
                        "profile": {
                            "cc": "US",
                            "country": "United States",
                            "city": "Los Angeles",
                            "tz": "America/Los_Angeles",
                            "latitude": 34.0522,
                            "longitude": -118.2437,
                        },
                    },
                    {
                        "round": 3,
                        "status": "ok",
                        "ip": "2.2.2.2",
                        "profile": {"country": "Wrong country", "city": "Wrong city"},
                    },
                ],
                "profiles": {
                    "1.1.1.1": {"country": "United States", "city": "Los Angeles"},
                    "2.2.2.2": {"country": "Wrong country", "city": "Wrong city"},
                },
            },
            {
                "source": "another-provider",
                "observed_ips": ["1.1.1.1"],
                "profile": {"country": "Also wrong", "city": "Also wrong"},
            },
        ]

        location = _normalize_identity(payload)["ipv4"]["location"]

        self.assertEqual(location["state"], "reported")
        self.assertEqual(location["ip"], "1.1.1.1")
        self.assertEqual(location["country_code"], "US")
        self.assertEqual(location["country"], "United States")
        self.assertEqual(location["city"], "Los Angeles")
        self.assertEqual(location["timezone"], "America/Los_Angeles")
        self.assertEqual(location["latitude"], 34.0522)
        self.assertEqual(location["longitude"], -118.2437)
        self.assertEqual(location["source"], "ident.me")
        self.assertTrue(location["observations_consistent"])

    def test_location_conflict_does_not_select_a_disputed_value(self) -> None:
        payload = confirmed_identity()
        payload["identity"]["ipv4"]["evidence"] = [
            {
                "source": "ident.me",
                "observed_ips": ["1.1.1.1"],
                "attempts": [
                    {
                        "round": 1,
                        "status": "ok",
                        "ip": "1.1.1.1",
                        "profile": {"cc": "US", "country": "United States", "city": "Chicago"},
                    },
                    {
                        "round": 2,
                        "status": "ok",
                        "ip": "1.1.1.1",
                        "profile": {"cc": "US", "country": "United States", "city": "New York"},
                    },
                ],
            }
        ]

        location = _normalize_identity(payload)["ipv4"]["location"]

        self.assertEqual(location["state"], "conflict")
        self.assertEqual(location["country"], "United States")
        self.assertEqual(location["city"], "")
        self.assertEqual(location["conflicting_fields"], ["city"])
        self.assertFalse(location["observations_consistent"])

    def test_location_never_cross_fills_between_ips_or_families(self) -> None:
        payload = confirmed_identity()
        ipv6 = "2606:4700:4700::1111"
        payload["identity"]["ipv4"]["evidence"] = [
            {
                "source": "ident.me",
                "observed_ips": [ipv6],
                "profiles": {ipv6: {"country": "Germany", "city": "Frankfurt"}},
            }
        ]
        payload["identity"]["ipv6"] = {
            "family": 6,
            "state": "confirmed",
            "confidence": "medium",
            "ip": ipv6,
            "observed_ips": [ipv6],
            "agreement": {"stable_agreeing_sources": 2},
            "evidence": [
                {
                    "source": "ident.me",
                    "observed_ips": [ipv6],
                    "profiles": {
                        "1.1.1.1": {"country": "United States", "city": "Los Angeles"},
                        ipv6: {"country": "Germany", "city": "Frankfurt"},
                    },
                }
            ],
        }

        normalized = _normalize_identity(payload)

        self.assertEqual(normalized["ipv4"]["location"]["state"], "unknown")
        self.assertEqual(normalized["ipv4"]["location"]["country"], "")
        self.assertEqual(normalized["ipv6"]["location"]["state"], "reported")
        self.assertEqual(normalized["ipv6"]["location"]["country"], "Germany")
        self.assertEqual(normalized["ipv6"]["location"]["ip"], ipv6)

    def test_location_is_unknown_for_an_unconfirmed_family(self) -> None:
        payload = confirmed_identity()
        payload["identity"]["ipv4"]["state"] = "unconfirmed"
        payload["identity"]["ipv4"]["evidence"] = [
            {
                "source": "ident.me",
                "observed_ips": ["1.1.1.1"],
                "profile": {"country": "United States", "city": "Los Angeles"},
            }
        ]

        location = _normalize_identity(payload)["ipv4"]["location"]

        self.assertEqual(location["state"], "unknown")
        self.assertEqual(location["ip"], "1.1.1.1")
        self.assertEqual(location["country"], "")
        self.assertEqual(location["source"], "")

    def test_normalization_keeps_selected_ip_when_observed_list_has_other_addresses(self) -> None:
        payload = confirmed_identity()
        payload["identity"]["ipv4"]["observed_ips"] = ["1.1.1.1", "2.2.2.2"]

        normalized = _normalize_identity(payload)

        self.assertEqual(normalized["ipv4"]["ip"], "1.1.1.1")
        self.assertEqual(normalized["ipv4"]["observed_ips"], ["1.1.1.1", "2.2.2.2"])

    def test_unstable_second_family_blocks_global_identity_confirmation(self) -> None:
        rotating = confirmed_identity()
        rotating["identity"]["ipv6"] = {
            "family": 6,
            "state": "rotating",
            "confidence": "low",
            "ip": None,
            "observed_ips": ["2001:4860:4860::8888", "2606:4700:4700::1111"],
            "agreement": {},
            "evidence": [],
        }
        normalized_rotating = _normalize_identity(rotating)
        self.assertTrue(normalized_rotating["conflict"])
        self.assertFalse(normalized_rotating["confirmed"])

        unconfirmed = confirmed_identity()
        unconfirmed["identity"]["ipv6"] = {
            "family": 6,
            "state": "unconfirmed",
            "confidence": "low",
            "ip": "2001:4860:4860::8888",
            "observed_ips": ["2001:4860:4860::8888"],
            "agreement": {},
            "evidence": [],
        }
        normalized_unconfirmed = _normalize_identity(unconfirmed)
        self.assertFalse(normalized_unconfirmed["conflict"])
        self.assertFalse(normalized_unconfirmed["confirmed"])


class DatasetUpdaterTests(unittest.TestCase):
    def test_cancelled_refresh_never_starts_network_or_writes_cache(self) -> None:
        cancel_event = threading.Event()
        cancel_event.set()

        def forbidden(*_args, **_kwargs):
            raise AssertionError("cancelled refresh performed network I/O")

        with tempfile.TemporaryDirectory() as temp_dir:
            updater = DatasetUpdater(
                temp_dir,
                http_get=forbidden,
                clock=MutableClock(),
            )
            spec = DatasetSpec(
                "cancelled",
                "https://example.test/list.json",
                60,
                parse_feodo_active_c2,
                allow_empty=True,
            )

            with self.assertRaises(DatasetRefreshCancelled):
                updater.refresh(spec, cancel_event=cancel_event)

            self.assertFalse((Path(temp_dir) / "cancelled.json").exists())
            self.assertFalse((Path(temp_dir) / "metadata.json").exists())

    def test_service_cancellation_caps_request_and_skips_remaining_sources(self) -> None:
        cancel_event = threading.Event()
        calls: list[tuple[str, float]] = []

        def cancelling_http(
            url: str,
            *,
            timeout: float,
            **_kwargs,
        ) -> HttpResponse:
            calls.append((url, timeout))
            cancel_event.set()
            return HttpResponse(200, b"[]", {}, url)

        with tempfile.TemporaryDirectory() as temp_dir:
            specs = (
                DatasetSpec(
                    "first",
                    "https://example.test/first.json",
                    60,
                    parse_feodo_active_c2,
                    allow_empty=True,
                ),
                DatasetSpec(
                    "second",
                    "https://example.test/second.json",
                    60,
                    parse_feodo_active_c2,
                    allow_empty=True,
                ),
            )
            service = ExitQualityService(
                temp_dir,
                route_snapshot=stable_route,
                http_get=cancelling_http,
                dataset_specs=specs,
                timeout_seconds=8.0,
            )

            snapshots = service.refresh_static_sources(
                cancel_event=cancel_event
            )

            self.assertEqual(snapshots, {})
            self.assertEqual(
                [url for url, _timeout in calls],
                [specs[0].url],
            )
            self.assertLessEqual(calls[0][1], 0.5)
            self.assertFalse(
                (Path(temp_dir) / "datasets" / "first.json").exists()
            )

    def test_empty_allowed_snapshot_is_cached_and_carries_attribution(self) -> None:
        calls: list[str] = []

        def http_get(url: str, **_kwargs) -> HttpResponse:
            calls.append(url)
            return HttpResponse(
                200,
                b"[]",
                {"last-modified": "Thu, 15 Jan 2027 08:00:00 GMT"},
                url,
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            clock = MutableClock()
            spec = DatasetSpec(
                "empty",
                "https://example.test/list.json",
                900,
                parse_feodo_active_c2,
                allow_empty=True,
                attribution="Example Rights Holder",
                terms_url="https://example.test/terms",
            )
            updater = DatasetUpdater(temp_dir, http_get=http_get, clock=clock)
            refreshed = updater.refresh(spec)
            cached = updater.load_cached(spec)

            self.assertEqual(refreshed.status, "fresh")
            self.assertEqual(refreshed.entries, ())
            self.assertEqual(cached.status, "fresh")
            self.assertEqual(calls, [spec.url])
            self.assertEqual(cached.metadata["copyright_date"], "2027")
            self.assertEqual(cached.metadata["attribution"], "© 2027 Example Rights Holder")
            self.assertEqual(cached.metadata["terms_url"], spec.terms_url)

    def test_cache_only_load_never_downloads_missing_dataset(self) -> None:
        def forbidden(*_args, **_kwargs):
            raise AssertionError("cache-only load performed network I/O")

        with tempfile.TemporaryDirectory() as temp_dir:
            updater = DatasetUpdater(temp_dir, http_get=forbidden, clock=MutableClock())
            spec = DatasetSpec("missing", "https://example.test/list.json", 60, lambda payload: [])
            snapshot = updater.load_cached(spec)
            self.assertEqual(snapshot.status, "error")
            self.assertEqual(snapshot.error["code"], "dataset_cache_missing")

    def test_conditional_refresh_and_last_good_fallback(self) -> None:
        responses = [
            HttpResponse(
                200,
                spamhaus_payload({"cidr": "192.0.2.0/24"}),
                {"etag": '"v1"', "last-modified": "Thu, 15 Jan 2027 08:00:00 GMT"},
                "https://example.test/drop.json",
            ),
            HttpResponse(304, b"", {}, "https://example.test/drop.json"),
            HttpResponse(503, b"", {}, "https://example.test/drop.json"),
        ]
        request_headers: list[dict] = []

        def http_get(_url: str, *, headers=None, **_kwargs) -> HttpResponse:
            request_headers.append(dict(headers or {}))
            return responses.pop(0)

        with tempfile.TemporaryDirectory() as temp_dir:
            clock = MutableClock()
            spec = DatasetSpec(
                "drop",
                "https://example.test/drop.json",
                60,
                parse_spamhaus_networks,
            )
            updater = DatasetUpdater(temp_dir, http_get=http_get, clock=clock)
            first = updater.refresh(spec)
            clock.value += 61
            conditional = updater.refresh(spec)
            clock.value += 61
            fallback = updater.refresh(spec)

            self.assertEqual(first.status, "fresh")
            self.assertEqual(conditional.status, "fresh")
            self.assertTrue(conditional.cache_hit)
            self.assertEqual(request_headers[1]["If-None-Match"], '"v1"')
            self.assertIn("If-Modified-Since", request_headers[1])
            self.assertEqual(fallback.status, "stale")
            self.assertTrue(fallback.cache_hit)
            self.assertEqual(fallback.entries[0]["value"], "192.0.2.0/24")

    def test_truncated_json_lines_cannot_replace_last_good_dataset(self) -> None:
        responses = [
            HttpResponse(
                200,
                spamhaus_payload(
                    {"cidr": "192.0.2.0/24"},
                    {"cidr": "198.51.100.0/24"},
                ),
                {},
                "https://example.test/drop.json",
            ),
            HttpResponse(
                200,
                b'{"cidr":"203.0.113.0/24"}\n{"cidr":',
                {},
                "https://example.test/drop.json",
            ),
        ]

        def http_get(_url: str, **_kwargs) -> HttpResponse:
            return responses.pop(0)

        with tempfile.TemporaryDirectory() as temp_dir:
            clock = MutableClock()
            spec = DatasetSpec(
                "drop",
                "https://example.test/drop.json",
                60,
                parse_spamhaus_networks,
            )
            updater = DatasetUpdater(temp_dir, http_get=http_get, clock=clock)
            first = updater.refresh(spec)
            clock.value += 61
            fallback = updater.refresh(spec)
            cached = updater.load_cached(spec)

            self.assertEqual(first.status, "fresh")
            self.assertEqual(fallback.status, "stale")
            self.assertTrue(fallback.cache_hit)
            self.assertEqual(
                [item["value"] for item in fallback.entries],
                ["192.0.2.0/24", "198.51.100.0/24"],
            )
            self.assertEqual(
                [item["value"] for item in cached.entries],
                ["192.0.2.0/24", "198.51.100.0/24"],
            )

    def test_refresh_rejects_http_without_requesting_it(self) -> None:
        def forbidden(*_args, **_kwargs):
            raise AssertionError("non-HTTPS URL was requested")

        with tempfile.TemporaryDirectory() as temp_dir:
            updater = DatasetUpdater(temp_dir, http_get=forbidden, clock=MutableClock())
            spec = DatasetSpec("unsafe", "http://example.test/list", 60, parse_spamhaus_networks)
            snapshot = updater.refresh(spec)
            self.assertEqual(snapshot.status, "error")
            self.assertIn("non-HTTPS", snapshot.error["message"])

    def test_refresh_rejects_https_request_redirected_to_http(self) -> None:
        def downgraded(url: str, **_kwargs) -> HttpResponse:
            return HttpResponse(
                200,
                spamhaus_payload({"cidr": "192.0.2.0/24"}),
                {},
                url.replace("https://", "http://", 1),
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            updater = DatasetUpdater(temp_dir, http_get=downgraded, clock=MutableClock())
            spec = DatasetSpec(
                "downgraded",
                "https://example.test/list",
                60,
                parse_spamhaus_networks,
            )
            snapshot = updater.refresh(spec)

            self.assertEqual(snapshot.status, "error")
            self.assertIn("redirected to non-HTTPS", snapshot.error["message"])
            self.assertFalse((Path(temp_dir) / "downgraded.json").exists())

    def test_refresh_rejects_cross_host_https_final_url(self) -> None:
        def cross_host(_url: str, **_kwargs) -> HttpResponse:
            return HttpResponse(
                200,
                spamhaus_payload({"cidr": "192.0.2.0/24"}),
                {},
                "https://redirected.test/list",
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            updater = DatasetUpdater(temp_dir, http_get=cross_host, clock=MutableClock())
            spec = DatasetSpec(
                "cross-host",
                "https://example.test/list",
                60,
                parse_spamhaus_networks,
            )
            snapshot = updater.refresh(spec)

            self.assertEqual(snapshot.status, "error")
            self.assertIn("redirected to another host", snapshot.error["message"])
            self.assertFalse((Path(temp_dir) / "cross-host.json").exists())

    def test_azure_directory_and_download_are_two_explicit_same_host_requests(self) -> None:
        azure_spec = next(spec for spec in DEFAULT_DATASET_SPECS if spec.name == "cloud_azure")
        download_url = (
            "https://download.microsoft.com/download/1/2/3/"
            "ServiceTags_Public_20270115.json"
        )
        calls: list[str] = []

        def azure_http_get(url: str, **_kwargs) -> HttpResponse:
            calls.append(url)
            if url == AZURE_SERVICE_TAGS_PAGE_URL:
                return HttpResponse(
                    200,
                    f'<a href="{download_url}">download</a>'.encode(),
                    {},
                    AZURE_SERVICE_TAGS_PAGE_URL,
                )
            if url == download_url:
                body = json.dumps(
                    {
                        "values": [
                            {
                                "name": "AzureCloud",
                                "properties": {
                                    "addressPrefixes": ["20.0.0.0/8"],
                                    "region": "global",
                                    "systemService": "Azure",
                                },
                            }
                        ]
                    }
                ).encode()
                return HttpResponse(200, body, {}, download_url)
            raise AssertionError(f"unexpected request: {url}")

        with tempfile.TemporaryDirectory() as temp_dir:
            updater = DatasetUpdater(temp_dir, http_get=azure_http_get, clock=MutableClock())
            snapshot = updater.refresh(azure_spec)

            self.assertEqual(snapshot.status, "fresh")
            self.assertEqual(calls, [AZURE_SERVICE_TAGS_PAGE_URL, download_url])
            self.assertEqual(snapshot.metadata["url"], download_url)
            self.assertEqual(snapshot.entries[0]["source"], "cloud_azure")

    def test_azure_directory_rejects_cross_host_final_url(self) -> None:
        azure_spec = next(spec for spec in DEFAULT_DATASET_SPECS if spec.name == "cloud_azure")
        download_url = (
            "https://download.microsoft.com/download/1/2/3/"
            "ServiceTags_Public_20270115.json"
        )
        calls: list[str] = []

        def redirected_directory(url: str, **_kwargs) -> HttpResponse:
            calls.append(url)
            return HttpResponse(
                200,
                f'<a href="{download_url}">download</a>'.encode(),
                {},
                "https://redirected.test/azure-directory",
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            updater = DatasetUpdater(
                temp_dir,
                http_get=redirected_directory,
                clock=MutableClock(),
            )
            snapshot = updater.refresh(azure_spec)

            self.assertEqual(snapshot.status, "error")
            self.assertEqual(calls, [AZURE_SERVICE_TAGS_PAGE_URL])
            self.assertIn("Azure directory redirected to another host", snapshot.error["message"])
            self.assertFalse((Path(temp_dir) / "cloud_azure.json").exists())

    def test_azure_download_rejects_cross_host_final_url(self) -> None:
        azure_spec = next(spec for spec in DEFAULT_DATASET_SPECS if spec.name == "cloud_azure")
        download_url = (
            "https://download.microsoft.com/download/1/2/3/"
            "ServiceTags_Public_20270115.json"
        )
        calls: list[str] = []

        def redirected_download(url: str, **_kwargs) -> HttpResponse:
            calls.append(url)
            if url == AZURE_SERVICE_TAGS_PAGE_URL:
                return HttpResponse(
                    200,
                    f'<a href="{download_url}">download</a>'.encode(),
                    {},
                    AZURE_SERVICE_TAGS_PAGE_URL,
                )
            if url == download_url:
                return HttpResponse(
                    200,
                    json.dumps(
                        {
                            "values": [
                                {
                                    "name": "AzureCloud",
                                    "properties": {
                                        "addressPrefixes": ["20.0.0.0/8"],
                                    },
                                }
                            ]
                        }
                    ).encode(),
                    {},
                    "https://redirected.test/service-tags.json",
                )
            raise AssertionError(f"unexpected request: {url}")

        with tempfile.TemporaryDirectory() as temp_dir:
            updater = DatasetUpdater(
                temp_dir,
                http_get=redirected_download,
                clock=MutableClock(),
            )
            snapshot = updater.refresh(azure_spec)

            self.assertEqual(snapshot.status, "error")
            self.assertEqual(calls, [AZURE_SERVICE_TAGS_PAGE_URL, download_url])
            self.assertIn("dataset redirected to another host", snapshot.error["message"])
            self.assertFalse((Path(temp_dir) / "cloud_azure.json").exists())


class ConclusionRuleTests(unittest.TestCase):
    def _reputation(self, **overrides) -> dict:
        result = {
            "risk": None,
            "confidence": None,
            "signals": {
                "threat": False,
                "tor": False,
                "proxy": False,
                "vpn": False,
                "hosting": False,
                "anonymous": False,
                "compromised": False,
                "scraper": False,
                "cloud": False,
            },
        }
        result.update(overrides)
        return result

    def _score_groups(self, **overrides: str) -> dict[str, dict]:
        groups = {
            key: {
                "state": "not_detected",
                "applicable": True,
                "complete": True,
                "checked_sources": [key],
            }
            for key in ("identity", "network", "anonymizer", "cloud", "threat")
        }
        for key, state in overrides.items():
            groups[key] = {
                "state": state,
                "applicable": True,
                "complete": state in {"not_detected", "detected"},
                "checked_sources": [key] if state != "unknown" else [],
            }
        return groups

    def test_quality_score_uses_free_group_evidence_and_caps_clean_results(self) -> None:
        score = _quality_score(
            groups=self._score_groups(),
            conclusion="no_known_issue",
            route_confirmed=True,
            route_changed=False,
            identity_confirmed=True,
            identity_conflict=False,
        )

        self.assertEqual(score["quality_score"], 96)
        self.assertEqual(score["quality_grade"], "excellent")
        self.assertEqual(score["score_confidence"], 100)
        self.assertEqual(score["score_algorithm"], "tesseract-free-evidence-v3")

    def test_quality_score_penalizes_missing_evidence_instead_of_assuming_clean(self) -> None:
        score = _quality_score(
            groups=self._score_groups(threat="unknown"),
            conclusion="insufficient_evidence",
            route_confirmed=True,
            route_changed=False,
            identity_confirmed=True,
            identity_conflict=False,
        )

        self.assertEqual(score["quality_score"], 72)
        self.assertEqual(score["quality_grade"], "attention")
        self.assertEqual(score["score_confidence"], 60)

    def test_quality_score_renormalizes_sources_that_do_not_apply_to_ipv6(self) -> None:
        groups = self._score_groups()
        groups["anonymizer"] = {
            "state": "not_applicable",
            "applicable": False,
            "complete": False,
            "checked_sources": [],
            "expected_sources": [],
        }

        score = _quality_score(
            groups=groups,
            conclusion="no_known_issue",
            route_confirmed=True,
            route_changed=False,
            identity_confirmed=True,
            identity_conflict=False,
        )

        self.assertEqual(score["quality_score"], 96)
        self.assertEqual(score["score_confidence"], 100)

    def test_quality_score_applies_severity_caps_to_exact_matches(self) -> None:
        threat = _quality_score(
            groups=self._score_groups(threat="detected"),
            conclusion="high_risk",
            route_confirmed=True,
            route_changed=False,
            identity_confirmed=True,
            identity_conflict=False,
        )
        anonymizer = _quality_score(
            groups=self._score_groups(anonymizer="detected"),
            conclusion="anonymizer_detected",
            route_confirmed=True,
            route_changed=False,
            identity_confirmed=True,
            identity_conflict=False,
        )
        cloud = _quality_score(
            groups=self._score_groups(cloud="detected"),
            conclusion="hosting_detected",
            route_confirmed=True,
            route_changed=False,
            identity_confirmed=True,
            identity_conflict=False,
        )

        self.assertEqual(threat["quality_score"], 20)
        self.assertEqual(threat["quality_grade"], "high_risk")
        self.assertEqual(anonymizer["quality_score"], 45)
        self.assertEqual(anonymizer["quality_grade"], "poor")
        self.assertEqual(cloud["quality_score"], 82)
        self.assertEqual(cloud["quality_grade"], "good")

    def test_quality_score_is_withheld_when_route_or_identity_is_unconfirmed(self) -> None:
        score = _quality_score(
            groups=self._score_groups(),
            conclusion="no_known_issue",
            route_confirmed=False,
            route_changed=False,
            identity_confirmed=True,
            identity_conflict=False,
        )

        self.assertIsNone(score["quality_score"])
        self.assertEqual(score["score_confidence"], 0)
        self.assertEqual(score["quality_grade"], "unavailable")

    def test_quality_score_is_withheld_below_minimum_evidence_coverage(self) -> None:
        score = _quality_score(
            groups=self._score_groups(
                anonymizer="unknown",
                cloud="unknown",
                threat="unknown",
            ),
            conclusion="insufficient_evidence",
            route_confirmed=True,
            route_changed=False,
            identity_confirmed=True,
            identity_conflict=False,
        )

        self.assertIsNone(score["quality_score"])
        self.assertEqual(score["score_confidence"], 25)

    def test_positive_precedence_and_official_cloud_match(self) -> None:
        reputation = self._reputation()
        reputation["signals"] = {**reputation["signals"], "threat": True}
        self.assertEqual(
            _conclusion(identity_confirmed=True, identity_conflict=False, evidence=[], reputation=reputation),
            "high_risk",
        )
        reputation = self._reputation()
        reputation["signals"] = {**reputation["signals"], "cloud": True}
        self.assertEqual(
            _conclusion(identity_confirmed=True, identity_conflict=False, evidence=[], reputation=reputation),
            "hosting_detected",
        )

    def test_no_known_issue_requires_all_fresh_negative_evidence(self) -> None:
        items = [
            evidence("onionoo", positive=False, status="negative"),
            evidence("spamhaus_drop", positive=False, status="negative"),
            evidence("spamhaus_dropv6", positive=False, status="negative"),
            evidence("spamhaus_asndrop", positive=False, status="negative"),
            evidence("feodo", positive=False, status="negative"),
        ]
        self.assertEqual(
            _conclusion(
                identity_confirmed=True,
                identity_conflict=False,
                evidence=items,
                reputation=self._reputation(risk=0, confidence=100),
                groups=self._score_groups(),
            ),
            "no_known_issue",
        )
        items[-1] = SourceEvidence(**{**items[-1].to_dict(), "stale": True})
        self.assertEqual(
            _conclusion(
                identity_confirmed=True,
                identity_conflict=False,
                evidence=items,
                reputation=self._reputation(risk=0, confidence=100),
                groups=self._score_groups(threat="unknown"),
            ),
            "insufficient_evidence",
        )

    def test_confidence_is_bound_to_the_record_that_reported_the_signal(self) -> None:
        low_confidence_proxy = evidence(
            "proxycheck",
            positive=True,
            status="positive",
            data={"risk": 20, "confidence": 20, "signals": {"proxy": True, "vpn": False}},
        )
        high_confidence_clean = evidence(
            "proxycheck",
            positive=False,
            status="negative",
            data={"risk": 0, "confidence": 100, "signals": {"proxy": False, "vpn": False}},
        )
        reputation = self._reputation(risk=20, confidence=100)
        reputation["signals"] = {**reputation["signals"], "proxy": True}
        self.assertEqual(
            _conclusion(
                identity_confirmed=True,
                identity_conflict=False,
                evidence=[low_confidence_proxy, high_confidence_clean],
                reputation=reputation,
            ),
            "insufficient_evidence",
        )
        self.assertIsNone(
            _conclusion_confidence(
                "insufficient_evidence",
                [low_confidence_proxy, high_confidence_clean],
            )
        )

    def test_conclusion_confidence_respects_exact_and_non_hit_boundaries(self) -> None:
        low_confidence_high_risk = evidence(
            "proxycheck",
            positive=True,
            status="positive",
            data={"risk": 90, "confidence": 10, "signals": {}},
        )
        exact_threat = evidence("feodo", positive=True, status="positive")
        clean_v4 = evidence(
            "proxycheck",
            positive=False,
            status="negative",
            data={"risk": 0, "confidence": 99, "signals": {}},
        )
        clean_v6 = evidence(
            "proxycheck",
            positive=False,
            status="negative",
            data={"risk": 0, "confidence": 40, "signals": {}},
        )

        self.assertIsNone(_conclusion_confidence("high_risk", [low_confidence_high_risk]))
        self.assertEqual(_conclusion_confidence("high_risk", [exact_threat]), 100)
        self.assertEqual(_conclusion_confidence("no_known_issue", [clean_v4, clean_v6]), 50)

    def test_proxycheck_confidence_never_strengthens_a_clean_conclusion(self) -> None:
        clean_low = evidence(
            "proxycheck",
            positive=False,
            status="negative",
            data={"risk": 0, "confidence": 1, "signals": {}},
        )
        clean_high = evidence(
            "proxycheck",
            positive=False,
            status="negative",
            data={"risk": 0, "confidence": 100, "signals": {}},
        )

        self.assertEqual(_conclusion_confidence("no_known_issue", [clean_low]), 50)
        self.assertEqual(_conclusion_confidence("no_known_issue", [clean_high]), 50)

    def test_proxycheck_tor_requires_confidence_but_onionoo_exact_does_not(self) -> None:
        onionoo_negative = evidence("onionoo", positive=False, status="negative")
        low_confidence_tor = evidence(
            "proxycheck",
            positive=True,
            status="positive",
            data={"risk": 20, "confidence": 10, "signals": {"tor": True}},
        )
        reputation = self._reputation(risk=20, confidence=10)
        reputation["signals"] = {**reputation["signals"], "tor": True}
        self.assertEqual(
            _conclusion(
                identity_confirmed=True,
                identity_conflict=False,
                evidence=[onionoo_negative, low_confidence_tor],
                reputation=reputation,
            ),
            "insufficient_evidence",
        )

        high_confidence_tor = SourceEvidence(
            **{
                **low_confidence_tor.to_dict(),
                "data": {"risk": 20, "confidence": 90, "signals": {"tor": True}},
            }
        )
        self.assertEqual(
            _conclusion(
                identity_confirmed=True,
                identity_conflict=False,
                evidence=[onionoo_negative, high_confidence_tor],
                reputation=reputation,
            ),
            "insufficient_evidence",
        )

        onionoo_positive = evidence("onionoo", positive=True, status="positive")
        clean_reputation = self._reputation()
        clean_reputation["signals"] = {**clean_reputation["signals"], "tor": True}
        self.assertEqual(
            _conclusion(
                identity_confirmed=True,
                identity_conflict=False,
                evidence=[onionoo_positive],
                reputation=clean_reputation,
            ),
            "anonymizer_detected",
        )

    def test_stale_threat_positive_is_attention_not_high_risk(self) -> None:
        stale_threat = SourceEvidence(
            **{
                **evidence("feodo", positive=True, status="positive").to_dict(),
                "stale": True,
            }
        )
        reputation = self._reputation()
        self.assertEqual(
            _conclusion(
                identity_confirmed=True,
                identity_conflict=False,
                evidence=[stale_threat],
                reputation=reputation,
            ),
            "needs_attention",
        )

    def test_stale_proxycheck_signal_is_attention_not_current_anonymizer(self) -> None:
        stale_proxy = SourceEvidence(
            source="proxycheck",
            status="rate_limited",
            observed_at="2027-01-14T08:00:00+00:00",
            expires_at="2027-01-15T08:00:00+00:00",
            positive=True,
            data={
                "risk": 50,
                "confidence": 90,
                "signals": {"proxy": True, "vpn": False},
            },
            error={"code": "rate_limited"},
            cache_hit=True,
            stale=True,
        )
        reputation = self._reputation()
        self.assertEqual(
            _conclusion(
                identity_confirmed=True,
                identity_conflict=False,
                evidence=[stale_proxy],
                reputation=reputation,
            ),
            "insufficient_evidence",
        )


class ExitQualityServiceTests(unittest.TestCase):
    def test_internal_request_observer_wraps_service_and_dataset_http_requests(self) -> None:
        events: list[tuple[str, str, float]] = []
        dataset_url = "https://example.test/feodo.json"

        def observer(url: str, timeout_seconds: float) -> None:
            events.append(("observer", url, timeout_seconds))

        def http_get(url: str, *, timeout: float, **kwargs) -> HttpResponse:
            del kwargs
            events.append(("request", url, timeout))
            if url == dataset_url:
                return HttpResponse(200, b"[]", {}, url)
            return online_http_get(url)

        with tempfile.TemporaryDirectory() as temp_dir:
            service = ExitQualityService(
                temp_dir,
                route_snapshot=stable_route,
                identity_collector=confirmed_identity,
                http_get=http_get,
                internal_request_observer=observer,
                dataset_specs=(
                    DatasetSpec(
                        "feodo",
                        dataset_url,
                        60,
                        parse_feodo_active_c2,
                        allow_empty=True,
                    ),
                ),
                timeout_seconds=2.75,
            )
            self.assertIs(service.dataset_updater.http_get, service.http_get)
            service.dataset_updater.http_get(dataset_url, timeout=2.75)
            _team_cymru_lookup(
                "1.1.1.1",
                http_get=service.http_get,
                timeout=2.75,
                proxy_url=None,
            )
            _onionoo_lookup(
                "1.1.1.1",
                http_get=service.http_get,
                timeout=2.75,
                proxy_url=None,
            )

        self.assertTrue(any(url == dataset_url for _kind, url, _timeout in events))
        self.assertTrue(any("dns-query" in url for _kind, url, _timeout in events))
        self.assertTrue(any("onionoo" in url for _kind, url, _timeout in events))
        self.assertEqual(len(events) % 2, 0)
        for index in range(0, len(events), 2):
            observed = events[index]
            requested = events[index + 1]
            self.assertEqual(observed[0], "observer")
            self.assertEqual(requested[0], "request")
            self.assertEqual(observed[1:], requested[1:])
            self.assertEqual(observed[2], 2.75)

    def test_internal_request_observer_failure_does_not_affect_http_request(self) -> None:
        requests: list[str] = []

        def failing_observer(_url: str, _timeout_seconds: float) -> None:
            raise RuntimeError("observer unavailable")

        def http_get(url: str, **_kwargs) -> HttpResponse:
            requests.append(url)
            return HttpResponse(200, b"{}", {}, url)

        with tempfile.TemporaryDirectory() as temp_dir:
            service = ExitQualityService(
                temp_dir,
                route_snapshot=stable_route,
                identity_collector=confirmed_identity,
                http_get=http_get,
                internal_request_observer=failing_observer,
                dataset_specs=(),
            )

            response = service.http_get(
                "https://example.test/internal",
                timeout=0.75,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(requests, ["https://example.test/internal"])

    def test_free_check_groups_are_fixed_and_count_only_complete_categories(self) -> None:
        identity = _normalize_identity(confirmed_identity())
        network = {
            "by_family": {
                "ipv4": {
                    "sources": [
                        {"source": "identity:ident.me", "ip": "1.1.1.1", "asn": "AS13335"}
                    ]
                }
            },
            "asn_sources": [{"source": "team_cymru", "ip": "1.1.1.1", "asn": "AS13335"}],
            "source_conflict": False,
            "asn_conflict": False,
        }
        items = [
            evidence("identity", positive=None, status="confirmed"),
            evidence(
                "team_cymru",
                positive=None,
                status="confirmed",
                data={"target_ip": "1.1.1.1"},
            ),
            evidence(
                "onionoo",
                positive=False,
                status="negative",
                data={"target_ip": "1.1.1.1"},
            ),
            evidence(
                "proxycheck",
                positive=False,
                status="negative",
                data={
                    "target_ip": "1.1.1.1",
                    "risk": 0,
                    "confidence": 100,
                    "signals": {},
                },
            ),
            *[
                evidence(
                    source,
                    positive=False,
                    status="negative",
                    data={"evaluated_ips": ["1.1.1.1"]},
                )
                for source in (
                    "cloud_aws",
                    "cloud_gcp",
                    "cloud_azure",
                    "cloud_oci",
                    "spamhaus_drop",
                    "spamhaus_asndrop",
                    "feodo",
                )
            ],
        ]

        groups = _free_check_groups(
            identity=identity,
            network=network,
            evidence=items,
            started_at=1_800_000_000.0,
        )

        self.assertEqual(
            list(groups),
            ["identity", "network", "anonymizer", "cloud", "threat"],
        )
        self.assertTrue(all(group["complete"] for group in groups.values()))
        self.assertEqual(sum(bool(group["complete"]) for group in groups.values()), 5)
        self.assertTrue(all(group["state"] == "not_detected" for group in groups.values()))
        self.assertEqual(
            groups["threat"]["expected_sources"],
            ["spamhaus_drop", "feodo", "spamhaus_asndrop"],
        )
        self.assertNotIn("spamhaus_dropv6", groups["threat"]["expected_sources"])

    def test_exact_onionoo_positive_ignores_retired_provider_rows(self) -> None:
        identity = _normalize_identity(confirmed_identity())
        rate_limited = SourceEvidence(
            source="proxycheck",
            status="rate_limited",
            observed_at="2027-01-15T08:30:00+00:00",
            expires_at="2027-01-15T08:35:00+00:00",
            data={"target_ip": "1.1.1.1"},
            error={"code": "rate_limited", "retry_after_seconds": 300},
        )
        groups = _free_check_groups(
            identity=identity,
            network={"by_family": {}, "asn_sources": []},
            evidence=[
                evidence("identity", positive=None, status="confirmed"),
                evidence(
                    "onionoo",
                    positive=True,
                    status="positive",
                    data={"target_ip": "1.1.1.1"},
                ),
                rate_limited,
            ],
            started_at=1_800_000_000.0,
        )

        group = groups["anonymizer"]
        self.assertEqual(group["state"], "detected")
        self.assertTrue(group["complete"])
        self.assertEqual(group["matched_sources"], ["onionoo"])
        self.assertNotIn("rate_limited", group["warning_codes"])
        self.assertNotIn("incomplete_coverage", group["warning_codes"])
        self.assertEqual(group["observed_at"], "2027-01-15T08:00:00+00:00")
        self.assertNotIn("retry_at", group)
        self.assertEqual(
            [item["source"] for item in group["source_results"]],
            ["onionoo"],
        )

    def test_retired_provider_scores_never_enter_anonymizer_group(self) -> None:
        identity = _normalize_identity(confirmed_identity())
        base = [
            evidence("identity", positive=None, status="confirmed"),
            evidence(
                "onionoo",
                positive=False,
                status="negative",
                data={"target_ip": "1.1.1.1"},
            ),
        ]

        def proxy(confidence: int) -> SourceEvidence:
            return evidence(
                "proxycheck",
                positive=True,
                status="positive",
                data={
                    "target_ip": "1.1.1.1",
                    "risk": 20,
                    "confidence": confidence,
                    "signals": {"proxy": True},
                },
            )

        low = _free_check_groups(
            identity=identity,
            network={"by_family": {}, "asn_sources": []},
            evidence=[*base, proxy(40)],
            started_at=1_800_000_000.0,
        )["anonymizer"]
        high = _free_check_groups(
            identity=identity,
            network={"by_family": {}, "asn_sources": []},
            evidence=[*base, proxy(90)],
            started_at=1_800_000_000.0,
        )["anonymizer"]

        self.assertTrue(low["complete"])
        self.assertEqual(low["state"], "not_detected")
        self.assertEqual(low["matched_sources"], [])
        self.assertEqual(low["warning_codes"], [])
        self.assertEqual(high["state"], "not_detected")
        self.assertEqual(high["matched_sources"], [])

        high_risk = evidence(
            "proxycheck",
            positive=True,
            status="positive",
            data={
                "target_ip": "1.1.1.1",
                "risk": 90,
                "confidence": 5,
                "signals": {},
            },
        )
        risk_group = _free_check_groups(
            identity=identity,
            network={"by_family": {}, "asn_sources": []},
            evidence=[*base, high_risk],
            started_at=1_800_000_000.0,
        )["anonymizer"]
        self.assertEqual(risk_group["state"], "not_detected")
        self.assertEqual(risk_group["matched_sources"], [])

    def test_denied_group_exposes_earliest_structured_retry_time_only(self) -> None:
        identity = _normalize_identity(confirmed_identity())

        def denied(source: str, retry_after: object) -> SourceEvidence:
            return SourceEvidence(
                source=source,
                status="denied",
                observed_at="2027-01-15T08:00:00+00:00",
                expires_at="2027-01-15T08:15:00+00:00",
                data={"target_ip": "1.1.1.1"},
                error={"code": "proxy_denied", "retry_after_seconds": retry_after},
            )

        group = _free_check_groups(
            identity=identity,
            network={"by_family": {}, "asn_sources": []},
            evidence=[
                evidence("identity", positive=None, status="confirmed"),
                denied("onionoo", 1200),
                denied("proxycheck", 900),
            ],
            started_at=1_800_000_000.0,
        )["anonymizer"]

        self.assertEqual(group["state"], "denied")
        self.assertEqual(group["retry_at"], "2027-01-15T08:20:00+00:00")
        retries = {
            item["source"]: item["retry_at"]
            for item in group["source_results"]
        }
        self.assertEqual(retries["onionoo"], "2027-01-15T08:20:00+00:00")
        self.assertNotIn("proxycheck", retries)

        invalid = _free_check_groups(
            identity=identity,
            network={"by_family": {}, "asn_sources": []},
            evidence=[
                evidence("identity", positive=None, status="confirmed"),
                denied("onionoo", 0),
                denied("proxycheck", "900"),
            ],
            started_at=1_800_000_000.0,
        )["anonymizer"]
        self.assertNotIn("retry_at", invalid)
        self.assertTrue(
            all("retry_at" not in item for item in invalid["source_results"])
        )

    def test_ipv6_only_groups_exclude_ipv4_only_sources(self) -> None:
        identity_payload = confirmed_identity()
        identity_payload["identity"]["ipv4"] = {
            "family": 4,
            "state": "unavailable",
            "ip": None,
        }
        identity_payload["identity"]["ipv6"] = {
            "family": 6,
            "state": "confirmed",
            "ip": "2606:4700:4700::1111",
            "observed_ips": ["2606:4700:4700::1111"],
            "agreement": {"stable_agreeing_sources": 2},
            "evidence": [{"source": "ipify"}, {"source": "ident.me"}],
        }
        identity = _normalize_identity(identity_payload)
        groups = _free_check_groups(
            identity=identity,
            network={
                "by_family": {},
                "asn_sources": [
                    {
                        "source": "team_cymru",
                        "ip": "2606:4700:4700::1111",
                        "asn": "AS13335",
                    }
                ],
            },
            evidence=[evidence("identity", positive=None, status="confirmed")],
            started_at=1_800_000_000.0,
        )

        self.assertEqual(groups["anonymizer"]["expected_sources"], [])
        self.assertFalse(groups["anonymizer"]["applicable"])
        self.assertEqual(groups["anonymizer"]["state"], "not_applicable")
        self.assertEqual(
            groups["threat"]["expected_sources"],
            ["spamhaus_dropv6", "spamhaus_asndrop"],
        )
        self.assertNotIn("onionoo", groups["anonymizer"]["expected_sources"])
        self.assertNotIn("feodo", groups["threat"]["expected_sources"])

    def test_dual_stack_group_keeps_family_divergence_and_ignores_inapplicable_onionoo(self) -> None:
        identity_payload = confirmed_identity()
        identity_payload["identity"]["ipv6"] = {
            "family": 6,
            "state": "confirmed",
            "ip": "2606:4700:4700::1111",
            "observed_ips": ["2606:4700:4700::1111"],
            "agreement": {"stable_agreeing_sources": 2},
            "evidence": [{"source": "ipify"}, {"source": "ident.me"}],
        }
        identity = _normalize_identity(identity_payload)
        v4 = "1.1.1.1"
        v6 = "2606:4700:4700::1111"
        groups = _free_check_groups(
            identity=identity,
            network={"by_family": {}, "asn_sources": []},
            evidence=[
                evidence("identity", positive=None, status="confirmed"),
                evidence(
                    "onionoo",
                    positive=False,
                    status="negative",
                    data={"target_ip": v4},
                ),
                SourceEvidence(
                    source="onionoo",
                    status="unknown",
                    observed_at="2027-01-15T08:00:00+00:00",
                    expires_at="2027-01-15T09:00:00+00:00",
                    data={"target_ip": v6},
                    error={"code": "ipv6_exit_evidence_unavailable"},
                ),
                evidence(
                    "proxycheck",
                    positive=False,
                    status="negative",
                    data={
                        "target_ip": v4,
                        "risk": 0,
                        "confidence": 100,
                        "signals": {"proxy": False},
                    },
                ),
                evidence(
                    "proxycheck",
                    positive=True,
                    status="positive",
                    data={
                        "target_ip": v6,
                        "risk": 20,
                        "confidence": 90,
                        "signals": {"proxy": True},
                    },
                ),
            ],
            started_at=1_800_000_000.0,
        )

        group = groups["anonymizer"]
        self.assertTrue(group["complete"])
        self.assertEqual(group["state"], "not_detected")
        self.assertEqual(group["matched_sources"], [])
        self.assertNotIn("family_results_differ", group["warning_codes"])
        onionoo = next(
            item for item in group["source_results"] if item["source"] == "onionoo"
        )
        self.assertEqual(onionoo["target_ips"], [v4])
        self.assertNotIn("ipv6_exit_evidence_unavailable", onionoo["warning_codes"])

    def test_dual_stack_location_and_network_are_kept_by_family(self) -> None:
        identity = confirmed_identity()
        identity["identity"]["ipv4"]["evidence"] = [
            {
                "source": "ident.me",
                "observed_ips": ["1.1.1.1"],
                "attempts": [
                    {
                        "round": round_number,
                        "status": "ok",
                        "ip": "1.1.1.1",
                        "profile": {
                            "asn": "AS13335",
                            "aso": "Cloudflare",
                            "type": "business",
                            "cc": "US",
                            "country": "United States",
                            "city": "Los Angeles",
                        },
                    }
                    for round_number in (1, 2)
                ],
            }
        ]
        identity["identity"]["ipv6"] = {
            "family": 6,
            "state": "confirmed",
            "confidence": "medium",
            "ip": "2606:4700:4700::1111",
            "observed_ips": ["2606:4700:4700::1111"],
            "agreement": {"stable_agreeing_sources": 2},
            "evidence": [
                {
                    "source": "ident.me",
                    "observed_ips": ["2606:4700:4700::1111"],
                    "attempts": [
                        {
                            "round": round_number,
                            "status": "ok",
                            "ip": "2606:4700:4700::1111",
                            "profile": {
                                "asn": "AS13336",
                                "aso": "Example IPv6 Org",
                                "type": "hosting",
                                "cc": "DE",
                                "country": "Germany",
                                "city": "Frankfurt",
                            },
                        }
                        for round_number in (1, 2)
                    ],
                }
            ],
        }

        def dual_stack_http(url: str, **_kwargs) -> HttpResponse:
            if "dns-query" in url:
                ipv6 = "origin6" in urllib.parse.unquote(url)
                record = (
                    '"13336 | 2606:4700:4700::/48 | DE | ripencc | 2010-01-01"'
                    if ipv6
                    else '"13335 | 1.1.1.0/24 | US | arin | 2010-01-01"'
                )
                return HttpResponse(
                    200,
                    json.dumps({"Status": 0, "Answer": [{"data": record}]}).encode(),
                    {},
                    url,
                )
            if "onionoo" in url:
                return HttpResponse(200, b'{"relays":[]}', {}, url)
            if "proxycheck" in url:
                target = urllib.parse.unquote(urllib.parse.urlparse(url).path.rsplit("/", 1)[-1])
                ipv6 = ":" in target
                network = {
                    "asn": "AS13336" if ipv6 else "AS13335",
                    "range": "2606:4700:4700::/48" if ipv6 else "1.1.1.0/24",
                    "provider": "Example IPv6" if ipv6 else "Cloudflare",
                    "organisation": "Example IPv6 Org" if ipv6 else "Cloudflare",
                    "type": "hosting" if ipv6 else "business",
                }
                return HttpResponse(
                    200,
                    json.dumps(
                        {
                            "status": "ok",
                            target: {
                                "network": network,
                                "detections": {
                                    "proxy": False,
                                    "vpn": False,
                                    "compromised": False,
                                    "scraper": False,
                                    "tor": False,
                                    "hosting": ipv6,
                                    "anonymous": False,
                                    "risk": 0,
                                    "confidence": 100,
                                },
                            },
                        }
                    ).encode(),
                    {},
                    url,
                )
            raise AssertionError(f"unexpected request: {url}")

        with tempfile.TemporaryDirectory() as temp_dir:
            service = ExitQualityService(
                temp_dir,
                route_snapshot=stable_route,
                identity_collector=lambda: identity,
                http_get=dual_stack_http,
                dataset_specs=(),
            )
            result = service.check()

        self.assertEqual(result.identity["ipv4"]["location"]["country"], "United States")
        self.assertEqual(result.identity["ipv4"]["location"]["city"], "Los Angeles")
        self.assertEqual(result.identity["ipv6"]["location"]["country"], "Germany")
        self.assertEqual(result.identity["ipv6"]["location"]["city"], "Frankfurt")
        self.assertEqual(result.network["by_family"]["ipv4"]["asn"], "AS13335")
        self.assertEqual(result.network["by_family"]["ipv4"]["prefix"], "1.1.1.0/24")
        self.assertEqual(result.network["by_family"]["ipv4"]["registered_country"], "US")
        self.assertEqual(result.network["by_family"]["ipv4"]["organisation"], "Cloudflare")
        self.assertEqual(result.network["by_family"]["ipv6"]["asn"], "AS13336")
        self.assertEqual(
            result.network["by_family"]["ipv6"]["prefix"],
            "2606:4700:4700::/48",
        )
        self.assertEqual(result.network["by_family"]["ipv6"]["registered_country"], "DE")
        self.assertEqual(result.network["by_family"]["ipv6"]["organisation"], "Example IPv6 Org")
        self.assertEqual(result.network["country"], "United States")
        self.assertEqual(result.network["registered_country"], "US")
        self.assertTrue(result.coverage["groups"]["identity"]["complete"])
        self.assertTrue(result.coverage["groups"]["network"]["complete"])
        self.assertEqual(
            result.coverage["groups"]["network"]["checked_sources"],
            ["ident.me", "team_cymru"],
        )
        self.assertTrue(result.coverage["groups"]["anonymizer"]["complete"])

    def test_unconfirmed_exact_threat_remains_high_risk_but_low_confidence_partial(self) -> None:
        identity = confirmed_identity()
        identity["identity"]["ipv4"]["state"] = "unconfirmed"
        identity["identity"]["ipv4"]["confidence"] = "low"

        def threat_http(url: str, **kwargs) -> HttpResponse:
            if "feodo.json" in url:
                return HttpResponse(
                    200,
                    json.dumps(
                        [
                            {
                                "status": "online",
                                "ip_address": "1.1.1.1",
                                "port": 443,
                            }
                        ]
                    ).encode(),
                    {},
                    url,
                )
            return online_http_get(url, **kwargs)

        with tempfile.TemporaryDirectory() as temp_dir:
            service = ExitQualityService(
                temp_dir,
                route_snapshot=stable_route,
                identity_collector=lambda: identity,
                http_get=threat_http,
                dataset_specs=(
                    DatasetSpec(
                        "feodo",
                        "https://example.test/feodo.json",
                        60,
                        parse_feodo_active_c2,
                        allow_empty=True,
                    ),
                ),
            )
            service.refresh_static_sources()
            result = service.check()

        self.assertEqual(result.reputation["conclusion"], "high_risk")
        self.assertEqual(result.reputation["conclusion_confidence"], 25)
        self.assertEqual(result.state, "partial")
        feodo = next(item for item in result.evidence if item["source"] == "feodo")
        self.assertEqual(feodo["data"]["target_ips"], ["1.1.1.1"])

    def test_team_cymru_moas_blocks_asn_drop_exact_match(self) -> None:
        def moas_http(url: str, **_kwargs) -> HttpResponse:
            if "drop.json" in url:
                return HttpResponse(200, spamhaus_payload({"asn": 64500}), {}, url)
            if "dns-query" in url:
                body = {
                    "Status": 0,
                    "Answer": [
                        {"data": '"64500 64501 | 1.1.1.0/24 | US | arin | 2010-01-01"'}
                    ],
                }
                return HttpResponse(200, json.dumps(body).encode(), {}, url)
            if "onionoo" in url:
                return HttpResponse(200, b'{"relays":[],"bridges":[]}', {}, url)
            if "proxycheck" in url:
                body = {
                    "status": "ok",
                    "1.1.1.1": {
                        "network": {},
                        "detections": {
                            "proxy": False,
                            "vpn": False,
                            "compromised": False,
                            "scraper": False,
                            "tor": False,
                            "hosting": False,
                            "anonymous": False,
                            "risk": 0,
                            "confidence": 100,
                        },
                    },
                }
                return HttpResponse(200, json.dumps(body).encode(), {}, url)
            raise AssertionError(f"unexpected request: {url}")

        with tempfile.TemporaryDirectory() as temp_dir:
            service = ExitQualityService(
                temp_dir,
                route_snapshot=stable_route,
                identity_collector=confirmed_identity,
                http_get=moas_http,
                dataset_specs=(
                    DatasetSpec(
                        "spamhaus_asndrop",
                        "https://example.test/drop.json",
                        60,
                        parse_spamhaus_asns,
                    ),
                ),
            )
            service.refresh_static_sources()
            result = service.check()

        asn_drop = next(item for item in result.evidence if item["source"] == "spamhaus_asndrop")
        self.assertTrue(result.network["asn_conflict"])
        self.assertEqual(asn_drop["status"], "conflict")
        self.assertEqual(asn_drop["data"]["match_count"], 0)
        self.assertEqual(result.reputation["conclusion"], "needs_attention")

    def test_dual_stack_check_does_not_query_retired_provider(self) -> None:
        identity = confirmed_identity()
        identity["identity"]["ipv6"] = {
            "family": 6,
            "state": "confirmed",
            "confidence": "medium",
            "ip": "2606:4700:4700::1111",
            "observed_ips": ["2606:4700:4700::1111"],
            "agreement": {"stable_agreeing_sources": 2},
            "evidence": [{"source": "one"}, {"source": "two"}],
        }

        def mixed_http(url: str, **_kwargs) -> HttpResponse:
            if "dns-query" in url:
                body = {
                    "Status": 0,
                    "Answer": [{"data": '"13335 | 1.1.1.0/24 | US | arin | 2010-01-01"'}],
                }
            elif "onionoo" in url:
                body = {"relays": [], "bridges": []}
            elif "proxycheck" in url:
                address = "2606:4700:4700::1111" if "%3A" in url.upper() else "1.1.1.1"
                detected = address != "1.1.1.1"
                body = {
                    "status": "ok",
                    address: {
                        "network": {"asn": "AS13335", "provider": "Example"},
                        "detections": {
                            "proxy": detected,
                            "vpn": False,
                            "compromised": False,
                            "scraper": False,
                            "tor": False,
                            "hosting": False,
                            "anonymous": detected,
                            "risk": 20 if detected else 0,
                            "confidence": 10 if detected else 99,
                        },
                    },
                }
            else:
                raise AssertionError(f"unexpected request: {url}")
            return HttpResponse(200, json.dumps(body).encode(), {}, url)

        with tempfile.TemporaryDirectory() as temp_dir:
            result = ExitQualityService(
                temp_dir,
                route_snapshot=stable_route,
                identity_collector=lambda: identity,
                http_get=mixed_http,
                dataset_specs=(),
            ).check()

        self.assertEqual(result.reputation["conclusion"], "insufficient_evidence")
        self.assertIsNone(result.reputation["conclusion_confidence"])
        self.assertIsNone(result.reputation["confidence"])
        self.assertNotIn("source_confidence_max", result.reputation)
        self.assertFalse(
            any(item["source"] == "proxycheck" for item in result.evidence)
        )

    def test_dual_stack_online_failures_keep_their_target_ip(self) -> None:
        identity = confirmed_identity()
        identity["identity"]["ipv6"] = {
            "family": 6,
            "state": "confirmed",
            "confidence": "medium",
            "ip": "2606:4700:4700::1111",
            "observed_ips": ["2606:4700:4700::1111"],
            "agreement": {"stable_agreeing_sources": 2},
            "evidence": [{"source": "one"}, {"source": "two"}],
        }

        def failing_http(url: str, **_kwargs) -> HttpResponse:
            if "proxycheck" in url:
                return HttpResponse(
                    429,
                    b'{"status":"denied","message":"limit"}',
                    {"retry-after": "300"},
                    url,
                )
            return HttpResponse(503, b"", {}, url)

        with tempfile.TemporaryDirectory() as temp_dir:
            service = ExitQualityService(
                temp_dir,
                route_snapshot=stable_route,
                identity_collector=lambda: identity,
                http_get=failing_http,
                dataset_specs=(
                    DatasetSpec(
                        "feodo",
                        "https://example.test/feodo.json",
                        60,
                        parse_feodo_active_c2,
                        allow_empty=True,
                    ),
                ),
            )
            result = service.check()

        team_rows = [item for item in result.evidence if item["source"] == "team_cymru"]
        self.assertEqual(
            {item["data"]["target_ip"] for item in team_rows},
            {"1.1.1.1", "2606:4700:4700::1111"},
        )
        onionoo_rows = [item for item in result.evidence if item["source"] == "onionoo"]
        self.assertEqual(
            {item["data"]["target_ip"] for item in onionoo_rows},
            {"1.1.1.1"},
        )
        self.assertFalse(
            any(item["source"] == "proxycheck" for item in result.evidence)
        )
        feodo = next(item for item in result.evidence if item["source"] == "feodo")
        self.assertEqual(feodo["data"]["target_ips"], [])
        self.assertEqual(
            feodo["data"]["evaluated_ips"],
            ["1.1.1.1", "2606:4700:4700::1111"],
        )

    def test_route_change_invalidates_and_does_not_persist_result(self) -> None:
        snapshots = iter((stable_route(marker="a"), stable_route(marker="b")))
        with tempfile.TemporaryDirectory() as temp_dir:
            service = ExitQualityService(
                temp_dir,
                route_snapshot=lambda: next(snapshots),
                identity_collector=confirmed_identity,
                http_get=online_http_get,
                dataset_specs=(),
            )
            result = service.check(request_id="request-1")
            self.assertEqual(result.state, "route_changed")
            self.assertEqual(result.reputation["conclusion"], "insufficient_evidence")
            self.assertIsNone(result.reputation["conclusion_confidence"])
            self.assertEqual(service.recent_results(), [])

    def test_core_restart_on_same_logical_route_is_not_route_change(self) -> None:
        before = stable_route(marker="same-route")
        after = {**before, "core_instance": "core-2"}
        snapshots = iter((before, after))
        with tempfile.TemporaryDirectory() as temp_dir:
            service = ExitQualityService(
                temp_dir,
                route_snapshot=lambda: next(snapshots),
                identity_collector=confirmed_identity,
                http_get=online_http_get,
                dataset_specs=(),
            )
            result = service.check(request_id="request-core-restart")

        self.assertNotEqual(
            result.route["execution_binding_marker"],
            "",
        )
        self.assertNotEqual(result.state, "route_changed")
        self.assertNotEqual(result.freshness.get("reason"), "route_changed")

    def test_logical_leaf_change_still_invalidates_result(self) -> None:
        before = stable_route(marker="same-marker")
        after = {
            **before,
            "selected_chain": ["FINAL-EXIT", "node-b"],
            "leaf_proxy": "node-b",
            "core_instance": "core-2",
        }
        snapshots = iter((before, after))
        with tempfile.TemporaryDirectory() as temp_dir:
            service = ExitQualityService(
                temp_dir,
                route_snapshot=lambda: next(snapshots),
                identity_collector=confirmed_identity,
                http_get=online_http_get,
                dataset_specs=(),
            )
            result = service.check(request_id="request-leaf-change")

        self.assertEqual(result.state, "route_changed")
        self.assertEqual(result.freshness.get("reason"), "route_changed")

    def test_result_preserves_family_evidence_and_dns_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            clock = MutableClock()
            service = ExitQualityService(
                temp_dir,
                route_snapshot=stable_route,
                identity_collector=confirmed_identity,
                http_get=online_http_get,
                dataset_specs=(
                    DatasetSpec(
                        "missing_static",
                        "https://example.test/missing.json",
                        60,
                        parse_spamhaus_networks,
                    ),
                ),
                clock=clock,
            )
            result = service.check(request_id="request-2")

            self.assertEqual(result.identity["ipv4"]["state"], "confirmed")
            self.assertEqual(result.identity["ipv4"]["agreement"]["stable_agreeing_sources"], 2)
            self.assertEqual(len(result.identity["ipv4"]["evidence"]), 2)
            self.assertEqual(result.freshness["state"], "fresh")
            self.assertEqual(result.freshness["status"], "partial")
            self.assertEqual(list(result.coverage["groups"]), [
                "identity",
                "network",
                "anonymizer",
                "cloud",
                "threat",
            ])
            self.assertEqual(result.coverage["expected_groups"], 5)
            self.assertEqual(
                result.coverage["checked_groups"],
                sum(
                    bool(group["complete"])
                    for group in result.coverage["groups"].values()
                ),
            )
            self.assertIn("missing_static", result.diagnostics["source_errors"])
            self.assertEqual(result.diagnostics["dns_reachability"]["state"], "reachable")
            self.assertEqual(result.diagnostics["dns_leak_test"], "not_run")
            signal_states = {
                "detected",
                "not_detected",
                "unknown",
                "rate_limited",
                "conflict",
                "stale",
            }
            self.assertTrue(set(result.reputation["signals"].values()).issubset(signal_states))
            self.assertTrue(all(item["state"] in signal_states for item in result.evidence))

            self.assertEqual(service.latest_result()["freshness"]["state"], "fresh")
            clock.value += 10 * 60
            self.assertEqual(service.latest_result()["freshness"]["state"], "stale")
            clock.value += 25 * 60
            self.assertEqual(service.latest_result()["freshness"]["state"], "expired")

    def test_latest_result_keeps_old_v2_records_without_new_network_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            results_path = Path(temp_dir) / "results-v2.json"
            legacy_record = {
                "schema_version": 2,
                "request_id": "legacy-v2",
                "checked_at": "2027-01-15T08:00:00+00:00",
                "freshness": {"state": "fresh"},
                "network": {"asn": "AS13335", "country": "US"},
                "identity": {
                    "ipv4": {"state": "confirmed", "ip": "1.1.1.1"},
                    "ipv6": {"state": "unavailable", "ip": None},
                },
            }
            results_path.write_text(
                json.dumps({"schema_version": 2, "records": [legacy_record]}),
                encoding="utf-8",
            )
            service = ExitQualityService(
                temp_dir,
                results_path=results_path,
                route_snapshot=stable_route,
                identity_collector=confirmed_identity,
                http_get=online_http_get,
                dataset_specs=(),
                clock=MutableClock(),
            )

            latest = service.latest_result()

        self.assertIsNotNone(latest)
        self.assertEqual(latest["request_id"], "legacy-v2")
        self.assertEqual(latest["network"], {"asn": "AS13335", "country": "US"})
        self.assertNotIn("location", latest["identity"]["ipv4"])
        self.assertNotIn("coverage", latest)

    def test_asn_disagreement_for_same_ip_is_attention(self) -> None:
        identity = confirmed_identity()
        identity["identity"]["ipv4"]["evidence"] = [
            {
                "source": "ident.me",
                "observed_ips": ["1.1.1.1"],
                "profile": {"ip": "1.1.1.1", "asn": "AS13335"},
            }
        ]

        def conflicting_http(url: str, **kwargs) -> HttpResponse:
            if "dns-query" in url:
                body = {
                    "Status": 0,
                    "Answer": [{"data": '"64500 | 1.1.1.0/24 | US | arin | 2010-01-01"'}],
                }
                return HttpResponse(200, json.dumps(body).encode(), {}, url)
            return online_http_get(url, **kwargs)

        with tempfile.TemporaryDirectory() as temp_dir:
            service = ExitQualityService(
                temp_dir,
                route_snapshot=stable_route,
                identity_collector=lambda: identity,
                http_get=conflicting_http,
                dataset_specs=(),
            )
            result = service.check()
            self.assertTrue(result.network["asn_conflict"])
            self.assertTrue(result.network["by_family"]["ipv4"]["source_conflict"])
            self.assertIn("asn", result.network["by_family"]["ipv4"]["conflicts"])
            self.assertEqual(result.network["by_family"]["ipv4"]["asn"], "")
            self.assertEqual(
                {item["asn"] for item in result.network["asn_sources"]},
                {"AS13335", "AS64500"},
            )
            self.assertEqual(result.reputation["conclusion"], "needs_attention")

    def test_disputed_asn_cannot_trigger_asn_drop_high_risk(self) -> None:
        identity = confirmed_identity()
        identity["identity"]["ipv4"]["evidence"] = [
            {
                "source": "ident.me",
                "observed_ips": ["1.1.1.1"],
                "profile": {"ip": "1.1.1.1", "asn": "AS13335"},
            }
        ]

        def conflicting_http(url: str, **kwargs) -> HttpResponse:
            if "drop.json" in url:
                return HttpResponse(200, spamhaus_payload({"asn": 64500}), {}, url)
            if "dns-query" in url:
                body = {
                    "Status": 0,
                    "Answer": [{"data": '"64500 | 1.1.1.0/24 | US | arin | 2010-01-01"'}],
                }
                return HttpResponse(200, json.dumps(body).encode(), {}, url)
            return online_http_get(url, **kwargs)

        with tempfile.TemporaryDirectory() as temp_dir:
            spec = DatasetSpec(
                "spamhaus_asndrop",
                "https://example.test/drop.json",
                60,
                parse_spamhaus_asns,
            )
            service = ExitQualityService(
                temp_dir,
                route_snapshot=stable_route,
                identity_collector=lambda: identity,
                http_get=conflicting_http,
                dataset_specs=(spec,),
            )
            service.refresh_static_sources()
            result = service.check()

            asn_drop = next(item for item in result.evidence if item["source"] == "spamhaus_asndrop")
            self.assertTrue(result.network["asn_conflict"])
            self.assertEqual(asn_drop["status"], "conflict")
            self.assertEqual(asn_drop["state"], "conflict")
            self.assertEqual(asn_drop["data"]["match_count"], 0)
            self.assertEqual(result.reputation["conclusion"], "needs_attention")

    def test_asn_drop_is_unknown_when_no_source_can_identify_the_asn(self) -> None:
        def no_asn_http(url: str, **_kwargs) -> HttpResponse:
            if "drop.json" in url:
                return HttpResponse(200, spamhaus_payload({"asn": 64500}), {}, url)
            if "dns-query" in url:
                return HttpResponse(503, b"", {}, url)
            if "onionoo" in url:
                return HttpResponse(200, b'{"relays":[]}', {}, url)
            if "proxycheck" in url:
                body = {
                    "status": "ok",
                    "1.1.1.1": {
                        "network": {},
                        "detections": {
                            "proxy": False,
                            "vpn": False,
                            "compromised": False,
                            "scraper": False,
                            "tor": False,
                            "hosting": False,
                            "anonymous": False,
                            "risk": 0,
                            "confidence": 100,
                        },
                    },
                }
                return HttpResponse(200, json.dumps(body).encode(), {}, url)
            raise AssertionError(f"unexpected request: {url}")

        with tempfile.TemporaryDirectory() as temp_dir:
            spec = DatasetSpec(
                "spamhaus_asndrop",
                "https://example.test/drop.json",
                60,
                parse_spamhaus_asns,
            )
            service = ExitQualityService(
                temp_dir,
                route_snapshot=stable_route,
                identity_collector=confirmed_identity,
                http_get=no_asn_http,
                dataset_specs=(spec,),
            )
            service.refresh_static_sources()
            result = service.check()

            asn_drop = next(item for item in result.evidence if item["source"] == "spamhaus_asndrop")
            self.assertEqual(asn_drop["status"], "unknown")
            self.assertEqual(asn_drop["error"]["code"], "asn_unavailable")
            self.assertEqual(result.reputation["conclusion"], "insufficient_evidence")

    def test_retired_provider_cache_cannot_supply_asn_or_trigger_asn_drop(self) -> None:
        def limited_http(url: str, **_kwargs) -> HttpResponse:
            if "drop.json" in url:
                return HttpResponse(200, spamhaus_payload({"asn": 64500}), {}, url)
            if "dns-query" in url:
                return HttpResponse(503, b"", {}, url)
            if "onionoo" in url:
                return HttpResponse(200, b'{"relays":[]}', {}, url)
            if "proxycheck" in url:
                return HttpResponse(
                    429,
                    b'{"status":"denied","message":"limit"}',
                    {"retry-after": "300"},
                    url,
                )
            raise AssertionError(f"unexpected request: {url}")

        with tempfile.TemporaryDirectory() as temp_dir:
            clock = MutableClock()
            spec = DatasetSpec(
                "spamhaus_asndrop",
                "https://example.test/drop.json",
                60,
                parse_spamhaus_asns,
            )
            service = ExitQualityService(
                temp_dir,
                route_snapshot=stable_route,
                identity_collector=confirmed_identity,
                http_get=limited_http,
                dataset_specs=(spec,),
                clock=clock,
            )
            service.refresh_static_sources()
            service.source_cache.put(
                "proxycheck",
                "1.1.1.1",
                {
                    "status": "positive",
                    "last_good_status": "positive",
                    "observed_at": "2027-01-14T08:00:00+00:00",
                    "expires_at": "2027-01-15T08:00:00+00:00",
                    "positive": True,
                    "data": {
                        "asn": "AS64500",
                        "risk": 50,
                        "confidence": 90,
                        "signals": {"proxy": True},
                    },
                },
                expires_at=clock() - 1,
            )

            result = service.check()

            asn_drop = next(item for item in result.evidence if item["source"] == "spamhaus_asndrop")
            self.assertFalse(
                any(item["source"] == "proxycheck" for item in result.evidence)
            )
            self.assertFalse(
                any(
                    row.get("source") == "proxycheck"
                    for row in result.network["asn_sources"]
                )
            )
            self.assertNotEqual(asn_drop["status"], "positive")
            self.assertNotEqual(result.reputation["conclusion"], "high_risk")

    def test_onionoo_revalidates_expired_cache_with_http_validators(self) -> None:
        onion_headers: list[dict] = []
        onion_urls: list[str] = []

        def conditional_http(url: str, *, headers=None, **kwargs) -> HttpResponse:
            if "onionoo" not in url:
                return online_http_get(url, headers=headers, **kwargs)
            onion_urls.append(url)
            onion_headers.append(dict(headers or {}))
            if len(onion_headers) == 1:
                return HttpResponse(
                    200,
                    b'{"relays":[]}',
                    {"etag": '"tor-v1"', "last-modified": "Thu, 15 Jan 2027 08:00:00 GMT"},
                    url,
                )
            return HttpResponse(304, b"", {}, url)

        with tempfile.TemporaryDirectory() as temp_dir:
            clock = MutableClock()
            service = ExitQualityService(
                temp_dir,
                route_snapshot=stable_route,
                identity_collector=confirmed_identity,
                http_get=conditional_http,
                dataset_specs=(),
                clock=clock,
            )
            service.check()
            clock.value += 60 * 60 + 1
            result = service.check()

            self.assertEqual(onion_headers[1]["If-None-Match"], '"tor-v1"')
            self.assertIn("If-Modified-Since", onion_headers[1])
            self.assertTrue(all("search=1.1.1.1" in url for url in onion_urls))
            self.assertTrue(all("running=" not in url and "flag=" not in url for url in onion_urls))
            onion = next(item for item in result.evidence if item["source"] == "onionoo")
            self.assertEqual(onion["status"], "negative")
            self.assertTrue(onion["cache_hit"])

    def test_unconfirmed_route_forces_partial_insufficient_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = ExitQualityService(
                temp_dir,
                route_snapshot=lambda: stable_route(confirmed=False),
                identity_collector=confirmed_identity,
                http_get=online_http_get,
                dataset_specs=(),
            )
            result = service.check()
            self.assertEqual(result.state, "partial")
            self.assertEqual(result.reputation["conclusion"], "insufficient_evidence")
            self.assertTrue(result.diagnostics["route_unconfirmed"])

    def test_corrupt_and_expired_per_ip_cache_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "source-cache.json"
            cache_path.write_text("not-json", encoding="utf-8")
            clock = MutableClock()
            cache = _SourceCache(cache_path, clock=clock)
            self.assertIsNone(cache.get("source", "1.1.1.1"))
            cache.put("source", "1.1.1.1", {"status": "negative"}, expires_at=clock() + 10)
            self.assertEqual(cache.get("source", "1.1.1.1")["status"], "negative")
            clock.value += 11
            self.assertIsNone(cache.get("source", "1.1.1.1"))

    def test_online_refresh_failure_retains_stale_last_good_positive(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            clock = MutableClock()
            cache = _SourceCache(Path(temp_dir) / "source-cache.json", clock=clock)
            cache.put(
                "proxycheck",
                "1.1.1.1",
                {
                    "status": "positive",
                    "observed_at": "2027-01-15T08:00:00+00:00",
                    "expires_at": "2027-01-15T09:00:00+00:00",
                    "positive": True,
                    "data": {"risk": 80},
                },
                expires_at=clock() + 10,
            )
            clock.value += 11

            item = _source_evidence(
                cache,
                "proxycheck",
                "1.1.1.1",
                ttl_seconds=60,
                lookup=lambda: {
                    "status": "rate_limited",
                    "error": {"code": "rate_limited", "retry_after_seconds": 300},
                },
                now=clock(),
            )

            self.assertEqual(item.status, "rate_limited")
            self.assertTrue(item.positive)
            self.assertTrue(item.stale)
            self.assertTrue(item.cache_hit)
            self.assertEqual(item.data["risk"], 80)
            self.assertEqual(item.error["code"], "rate_limited")

            cached_item = _source_evidence(
                cache,
                "proxycheck",
                "1.1.1.1",
                ttl_seconds=60,
                lookup=lambda: self.fail("rate-limit backoff should use cached stale evidence"),
                now=clock(),
            )
            self.assertEqual(cached_item.status, "rate_limited")
            self.assertTrue(cached_item.positive)
            self.assertTrue(cached_item.stale)


if __name__ == "__main__":
    unittest.main()
