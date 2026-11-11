import json
import logging
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from geo_updater import GeoDownloadPolicy, GeoUpdater


class GeoUpdaterTests(unittest.TestCase):
    def _build(self, tmp: Path, config: dict | None = None) -> GeoUpdater:
        return GeoUpdater(
            config=config or {"enabled": True, "interval_hours": 24},
            work_dir=tmp / "mihomo",
            metadata_path=tmp / "geo-metadata.json",
            egress_proxy={"enabled": True, "server": "127.0.0.1", "port": 19094},
            logger=logging.getLogger("test-geo"),
        )

    def test_status_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            updater = self._build(Path(temp_dir))
            status = updater.status()
            self.assertTrue(status["enabled"])
            self.assertEqual(status["interval_hours"], 24)
            self.assertIn("geoip", status["urls"])
            self.assertIn("geosite", status["urls"])

    def test_empty_optional_mmdb_url_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            updater = self._build(
                Path(temp_dir),
                {"enabled": True, "geox_url": {"geoip": "https://mirror/geoip.dat", "geosite": "https://mirror/geosite.dat", "mmdb": ""}},
            )
            status = updater.status()

            self.assertNotIn("mmdb", status["urls"])

    def test_include_mmdb_uses_default_mmdb_url(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            updater = self._build(Path(temp_dir), {"enabled": True, "include_mmdb": True})
            status = updater.status()

            self.assertIn("mmdb", status["urls"])

    def test_disabled_is_never_due(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            updater = self._build(Path(temp_dir), {"enabled": False})
            self.assertFalse(updater.is_due())

    def test_interval_minimum_is_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            updater = self._build(Path(temp_dir), {"enabled": True, "interval_hours": 0.01})
            # GeoUpdater clamps to >= 1 hour.
            self.assertGreaterEqual(updater.status()["interval_hours"], 1.0)

    def test_invalid_numeric_config_falls_back_without_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            updater = self._build(
                Path(temp_dir),
                {"enabled": True, "interval_hours": "bad", "request_timeout": "bad"},
            )

            self.assertEqual(updater.status()["interval_hours"], 24.0)
            self.assertEqual(updater.config["request_timeout"], 30.0)

    def test_invalid_egress_proxy_port_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            updater = GeoUpdater(
                config={"enabled": True},
                work_dir=Path(temp_dir) / "mihomo",
                metadata_path=Path(temp_dir) / "geo-metadata.json",
                egress_proxy={"enabled": True, "server": "127.0.0.1", "port": "bad"},
                logger=logging.getLogger("test-geo"),
            )

            with self.assertRaisesRegex(RuntimeError, "valid routed proxy"):
                updater._opener()

    def test_missing_egress_proxy_never_uses_host_default_network(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            updater = GeoUpdater(
                config={"enabled": True},
                work_dir=Path(temp_dir) / "mihomo",
                metadata_path=Path(temp_dir) / "geo-metadata.json",
                logger=logging.getLogger("test-geo-no-route"),
            )

            with self.assertRaisesRegex(RuntimeError, "host-default direct access is disabled"):
                updater._opener()

    def test_update_downloads_and_records_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = Path(temp_dir)
            payload = b"fake-geoip-binary-payload"

            class _FakeResponse:
                def __init__(self, data: bytes) -> None:
                    self._data = data
                    self._read = False
                    self.headers = {"Content-Length": str(len(data))}

                def __enter__(self) -> "_FakeResponse":
                    return self

                def __exit__(self, *args) -> None:
                    return None

                def read(self, size: int = -1) -> bytes:
                    if self._read:
                        return b""
                    self._read = True
                    return self._data

            config = {
                "enabled": True,
                "interval_hours": 24,
                "geox_url": {
                    "geoip": "https://example.com/geoip.dat",
                    "geosite": "https://example.com/geosite.dat",
                },
            }
            updater = self._build(tmp, config)

            def _fake_open(self, request, timeout=None):  # noqa: D401 - test double
                return _FakeResponse(payload)

            with patch("urllib.request.OpenerDirector.open", _fake_open):
                status = updater.update_now(force=True)

            self.assertIn("geoip", status["last_update"])
            self.assertEqual(status["bytes"]["geoip"], len(payload))
            self.assertTrue((tmp / "mihomo" / "geoip.dat").exists())
            self.assertEqual((tmp / "mihomo" / "geoip.dat").read_bytes(), payload)
            saved = json.loads((tmp / "geo-metadata.json").read_text(encoding="utf-8"))
            self.assertIn("last_update", saved)
            self.assertIn("geoip", saved["last_update"])

    def test_update_records_error_and_does_not_raise(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = Path(temp_dir)
            updater = self._build(tmp, {"enabled": True, "geox_url": {"geoip": "https://bad.example/geoip.dat"}})

            def _boom(self, request, timeout=None):
                raise RuntimeError("network down")

            with patch("urllib.request.OpenerDirector.open", _boom):
                status = updater.update_now(force=True)

            self.assertIn("geoip", status["errors"])
            self.assertNotIn("geoip", status.get("last_update", {}))

    def test_reconfigure_applies_new_urls(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            updater = self._build(Path(temp_dir))
            updater.reconfigure({"enabled": True, "geox_url": {"geoip": "https://mirror/geoip.dat"}})
            self.assertEqual(updater.status()["urls"]["geoip"], "https://mirror/geoip.dat")

    def test_is_due_uses_persisted_wall_clock_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = Path(temp_dir)
            metadata = {
                "last_update": {},
                "bytes": {},
                "errors": {},
                "last_success_monotonic": 999999999.0,
                "last_success_wall": 1000.0,
            }
            (tmp / "geo-metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
            updater = self._build(tmp, {"enabled": True, "interval_hours": 1})

            with patch("time.time", return_value=1000.0 + 3601.0):
                self.assertTrue(updater.is_due(now_monotonic=1.0))

    def test_non_https_url_is_rejected_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            updater = self._build(Path(temp_dir), {"enabled": True, "geox_url": {"geoip": "http://example.com/geoip.dat"}})

            status = updater.update_now(force=True)

            self.assertIn("geoip", status["errors"])
            self.assertIn("non-HTTPS", status["errors"]["geoip"])

    def test_string_false_does_not_allow_insecure_http(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            updater = self._build(
                Path(temp_dir),
                {
                    "enabled": "true",
                    "allow_insecure_http": "false",
                    "geox_url": {"geoip": "http://example.com/geoip.dat"},
                },
            )

            status = updater.update_now(force=True)

            self.assertIn("geoip", status["errors"])
            self.assertIn("non-HTTPS", status["errors"]["geoip"])

    def test_incomplete_sized_response_is_not_kept(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = Path(temp_dir)
            updater = self._build(tmp, {"enabled": True, "geox_url": {"geoip": "https://example.com/geoip.dat"}})
            # The proxy closed the connection after 40 of the announced 100 bytes.
            script = _Script([_Stream(40, 100_000.0, announced=100)])

            with patch("urllib.request.OpenerDirector.open", script.open):
                status = updater.update_now(force=True, assets=["geoip"])

            self.assertIn("incomplete", status["errors"]["geoip"])
            self.assertFalse((tmp / "mihomo" / "geoip.dat").exists())
            self.assertEqual(list((tmp / "mihomo").glob(".*.tmp")), [])

    def test_cancel_ends_a_plain_update_without_a_failure_event(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = Path(temp_dir)
            updater = self._build(tmp, EXAMPLE_CONFIG)
            script = _Script([_Stream(64 * KIB, 64 * KIB)])
            started = time.monotonic()

            with self.assertLogs("test-geo", level="INFO") as logs:
                with patch("urllib.request.OpenerDirector.open", script.open):
                    status = updater.update_now(
                        force=True, cancel=lambda: time.monotonic() - started > 0.2,
                    )

            self.assertIn("GeoDownloadCancelled", status["errors"]["geoip"])
            # The second asset was never started, nothing was left behind,
            # and a stop is not reported as a failed update.
            self.assertEqual(script.urls(), ["https://example.com/geoip.dat"])
            self.assertEqual(list((tmp / "mihomo").glob("*")), [])
            self.assertFalse([record for record in logs.records if record.levelno >= logging.WARNING])

    def test_assets_limit_the_update(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = Path(temp_dir)
            updater = self._build(tmp, EXAMPLE_CONFIG)
            script = _Script([_Stream(8 * KIB, 4096 * KIB)])

            with patch("urllib.request.OpenerDirector.open", script.open):
                status = updater.update_now(force=True, assets=["geosite"])

            self.assertEqual(script.urls(), ["https://example.com/geosite.dat"])
            self.assertEqual(sorted(status["last_update"]), ["geosite"])


KIB = 1024
EXAMPLE_CONFIG = {"enabled": True, "geox_url": {
    "geoip": "https://example.com/geoip.dat",
    "geosite": "https://example.com/geosite.dat",
}}


class _Stream:
    """A response whose body arrives at ``rate`` bytes per second (real time)."""

    def __init__(self, size: int, rate: float, *, announced: int | None = None,
                 step: float = 0.01) -> None:
        self.headers = {"Content-Length": str(size if announced is None else announced)}
        self._left = size
        self._rate = rate
        self._step = step
        self.closed = False

    def __enter__(self) -> "_Stream":
        return self

    def __exit__(self, *args) -> None:
        self.closed = True

    def read1(self, amount: int = -1) -> bytes:
        if self._left <= 0:
            return b""
        time.sleep(self._step)
        size = min(self._left, max(1, int(self._rate * self._step)))
        self._left -= size
        return b"\x01" * size

    read = read1


class _Dead:
    """A proxy that never answers: the connection fails after ``after`` seconds."""

    def __init__(self, after: float = 0.0) -> None:
        self.after = after


class _Script:
    """Stands in for OpenerDirector.open: one scripted answer per connection;
    the last answer repeats."""

    def __init__(self, answers: list) -> None:
        self._answers = list(answers)
        self._lock = threading.Lock()
        self.opened: list[tuple[str, float | None, float]] = []  # (url, timeout, at)

    def urls(self) -> list[str]:
        return [url for url, _timeout, _at in self.opened]

    @property
    def open(self):
        script = self

        def _open(self, request, timeout=None):  # noqa: D401 - test double
            with script._lock:
                script.opened.append((request.full_url, timeout, time.monotonic()))
                answer = script._answers.pop(0) if len(script._answers) > 1 else script._answers[0]
            if isinstance(answer, _Dead):
                time.sleep(answer.after)
                raise urllib.error.URLError("EOF occurred in violation of protocol")
            return answer

        return _open


def _fast_policy(**changes) -> GeoDownloadPolicy:
    """The shipped rules on a short clock, so a race takes a second."""
    values = dict(comfort_rate=1024 * KIB, window=0.3, stagger=0.15, operation_timeout=3.0,
                  retry_pause=0.01, poll=0.02)
    values.update(changes)
    return GeoDownloadPolicy(**values)


def _settled(directory: Path, seconds: float = 2.0) -> list:
    """Temporary files left once the closed connections have noticed."""
    deadline = time.monotonic() + seconds
    while True:
        left = list(directory.glob(".*.tmp"))
        if not left or time.monotonic() >= deadline:
            return left
        time.sleep(0.02)


class GeoDownloadPolicyTests(unittest.TestCase):
    def _build(self, tmp: Path, urls: dict | None = None) -> GeoUpdater:
        return GeoUpdater(
            config={"enabled": True, "geox_url": urls or {"geoip": "https://example.com/geoip.dat"}},
            work_dir=tmp / "mihomo",
            metadata_path=tmp / "geo-metadata.json",
            egress_proxy={"enabled": True, "server": "127.0.0.1", "port": 19094},
            logger=logging.getLogger("test-geo-policy"),
        )

    def test_a_slow_proxy_is_passed_by_a_second_connection(self) -> None:
        # 11-06: the first pool member carried the download slowly for 92 s
        # and nothing else was tried.
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = Path(temp_dir)
            updater = self._build(tmp)
            size = 400 * KIB
            slow = _Stream(size, 20 * KIB)  # 20 s on its own
            script = _Script([slow, _Stream(size, 4096 * KIB)])
            started = time.monotonic()

            with patch("urllib.request.OpenerDirector.open", script.open):
                status = updater.update_now(force=True, policy=_fast_policy(), assets=["geoip"])

            self.assertEqual(status["errors"], {})
            self.assertEqual((tmp / "mihomo" / "geoip.dat").read_bytes(), b"\x01" * size)
            self.assertLess(time.monotonic() - started, 5.0)
            self.assertEqual(len(script.opened), 2)
            self.assertEqual({timeout for _url, timeout, _at in script.opened}, {3.0})
            # The second connection waited for the first one's rate to be known.
            self.assertGreaterEqual(script.opened[1][2] - script.opened[0][2], 0.3)
            self.assertEqual(_settled(tmp / "mihomo"), [])
            self.assertTrue(slow.closed)

    def test_the_only_working_connection_is_never_given_up(self) -> None:
        # 11-10, real network: one member carried 216 KiB/s, every other
        # connection ended without an answer.
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = Path(temp_dir)
            updater = self._build(tmp)
            size = 300 * KIB
            script = _Script([_Stream(size, 200 * KIB), _Dead(0.05)])

            with patch("urllib.request.OpenerDirector.open", script.open):
                status = updater.update_now(force=True, policy=_fast_policy(), assets=["geoip"])

            self.assertEqual(status["errors"], {})
            self.assertEqual((tmp / "mihomo" / "geoip.dat").stat().st_size, size)
            # Others were tried beside it, and their failures changed nothing.
            self.assertGreater(len(script.opened), 2)

    def test_a_slow_link_stops_trying_other_proxies(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = Path(temp_dir)
            updater = self._build(tmp)
            size = 600 * KIB
            # Every proxy carries the same: the line is the limit.
            script = _Script([_Stream(size, 300 * KIB)] + [_Stream(size, 300 * KIB) for _ in range(8)])

            with patch("urllib.request.OpenerDirector.open", script.open):
                status = updater.update_now(force=True, policy=_fast_policy(), assets=["geoip"])

            self.assertEqual(status["errors"], {})
            self.assertEqual((tmp / "mihomo" / "geoip.dat").stat().st_size, size)
            # The lead plus three rivals that were no faster, then no more.
            self.assertEqual(len(script.opened), 1 + GeoDownloadPolicy().max_plain_rivals)
            self.assertEqual(_settled(tmp / "mihomo"), [])

    def test_dead_proxies_are_not_waited_for_one_by_one(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = Path(temp_dir)
            updater = self._build(tmp)
            size = 64 * KIB
            script = _Script([_Dead(1.0), _Dead(1.0), _Stream(size, 4096 * KIB)])
            started = time.monotonic()

            with patch("urllib.request.OpenerDirector.open", script.open):
                status = updater.update_now(force=True, policy=_fast_policy(), assets=["geoip"])

            self.assertEqual(status["errors"], {})
            self.assertEqual((tmp / "mihomo" / "geoip.dat").stat().st_size, size)
            self.assertEqual(len(script.opened), 3)
            # The second connection started while the first was still silent.
            self.assertLess(script.opened[1][2] - script.opened[0][2], 0.6)
            self.assertLess(time.monotonic() - started, 2.4)

    def test_connections_that_never_answer_end_the_asset(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = Path(temp_dir)
            updater = self._build(tmp)
            script = _Script([_Dead()])

            with patch("urllib.request.OpenerDirector.open", script.open):
                status = updater.update_now(
                    force=True, policy=_fast_policy(max_attempts=5), assets=["geoip"],
                )

            self.assertIn("URLError", status["errors"]["geoip"])
            self.assertEqual(len(script.opened), 5)
            self.assertFalse((tmp / "mihomo" / "geoip.dat").exists())

    def test_what_no_route_can_fix_is_not_tried(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = Path(temp_dir)
            updater = self._build(tmp, {"geoip": "http://example.com/geoip.dat"})
            script = _Script([_Stream(10, 1000.0)])

            with patch("urllib.request.OpenerDirector.open", script.open):
                status = updater.update_now(force=True, policy=_fast_policy(), assets=["geoip"])

            self.assertIn("non-HTTPS", status["errors"]["geoip"])
            self.assertEqual(script.opened, [])

    def test_cancel_ends_the_race_at_once_and_is_not_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = Path(temp_dir)
            updater = self._build(tmp, {
                "geoip": "https://example.com/geoip.dat",
                "geosite": "https://example.com/geosite.dat",
            })
            script = _Script([_Stream(4096 * KIB, 100 * KIB)])
            started = time.monotonic()

            with self.assertLogs("test-geo-policy", level="INFO") as logs:
                with patch("urllib.request.OpenerDirector.open", script.open):
                    status = updater.update_now(
                        force=True, policy=_fast_policy(),
                        cancel=lambda: time.monotonic() - started > 0.5,
                    )

            self.assertLess(time.monotonic() - started, 1.5)
            self.assertIn("GeoDownloadCancelled", status["errors"]["geoip"])
            self.assertNotIn("geosite", status["errors"])
            self.assertEqual(set(script.urls()), {"https://example.com/geoip.dat"})
            self.assertFalse((tmp / "mihomo" / "geoip.dat").exists())
            self.assertEqual(_settled(tmp / "mihomo"), [])
            self.assertFalse([record for record in logs.records if record.levelno >= logging.WARNING])


if __name__ == "__main__":
    unittest.main()
