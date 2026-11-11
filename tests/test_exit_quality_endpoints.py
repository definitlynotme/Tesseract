from __future__ import annotations

import unittest
import urllib.error
import urllib.request

from exit_quality_endpoints import (
    EXIT_QUALITY_SYSTEM_HOSTS,
    EXIT_QUALITY_SYSTEM_URLS,
    SameHostHttpsRedirectHandler,
    same_host_https_redirect_url,
)


EXPECTED_SYSTEM_HOSTS = {
    "api.ipify.org",
    "api6.ipify.org",
    "4.ident.me",
    "6.ident.me",
    "checkip.amazonaws.com",
    "cloudflare-dns.com",
    "onionoo.torproject.org",
    "proxycheck.io",
    "ip-ranges.amazonaws.com",
    "www.gstatic.com",
    "www.microsoft.com",
    "download.microsoft.com",
    "docs.oracle.com",
    "www.spamhaus.org",
    "feodotracker.abuse.ch",
}


class ExitQualityEndpointTests(unittest.TestCase):
    def test_system_hosts_are_the_complete_exact_application_owned_set(self) -> None:
        self.assertEqual(len(EXIT_QUALITY_SYSTEM_HOSTS), 15)
        self.assertEqual(set(EXIT_QUALITY_SYSTEM_HOSTS), EXPECTED_SYSTEM_HOSTS)
        self.assertEqual(len(EXIT_QUALITY_SYSTEM_HOSTS), len(set(EXIT_QUALITY_SYSTEM_HOSTS)))
        self.assertTrue(all(url.startswith("https://") for url in EXIT_QUALITY_SYSTEM_URLS))

    def test_same_host_https_redirect_accepts_relative_and_absolute_targets(self) -> None:
        self.assertEqual(
            same_host_https_redirect_url(
                "https://provider.test/path/one",
                "../two",
            ),
            "https://provider.test/two",
        )
        self.assertEqual(
            same_host_https_redirect_url(
                "https://provider.test/path",
                "https://provider.test/next",
            ),
            "https://provider.test/next",
        )

    def test_same_host_https_redirect_rejects_cross_host_and_downgrade(self) -> None:
        for target in (
            "https://redirected.test/next",
            "http://provider.test/next",
            "//redirected.test/next",
        ):
            with self.subTest(target=target), self.assertRaisesRegex(
                ValueError,
                "cross-host or non-HTTPS",
            ):
                same_host_https_redirect_url("https://provider.test/start", target)

    def test_urllib_handler_rejects_before_creating_the_next_request(self) -> None:
        handler = SameHostHttpsRedirectHandler()
        source = urllib.request.Request("https://provider.test/start")

        with self.assertRaisesRegex(
            urllib.error.HTTPError,
            "cross-host or non-HTTPS",
        ):
            handler.redirect_request(
                source,
                None,
                302,
                "Found",
                {},
                "https://redirected.test/next",
            )

        redirected = handler.redirect_request(
            source,
            None,
            302,
            "Found",
            {},
            "/next",
        )
        self.assertIsNotNone(redirected)
        assert redirected is not None
        self.assertEqual(redirected.full_url, "https://provider.test/next")


if __name__ == "__main__":
    unittest.main()
