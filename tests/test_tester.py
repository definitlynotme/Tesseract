import asyncio
import logging
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from models import Node, NodeStatus
from tester import (
    _MihomoProbeEndpointHealth,
    _MihomoRealProbeAttemptResult,
    _MihomoRealProbeOutcome,
    Tier3TcpProbeEntitlement,
    _build_mihomo_prewarm_config,
    _collect_tcp_probe_entitlements,
    _gateway_chain_validate,
    _mihomo_canonical_probe_urls,
    _mihomo_primary_fast_probe_urls,
    _mihomo_probe_urls_for_initial_cohort,
    _mihomo_direct_validate,
    _mihomo_probe_urls,
    _mihomo_validation_probe_portfolio,
    _run_mihomo_real_probe_attempts,
    _run_tcp_probe_entitlement,
    _stop_process,
    _wait_for_tcp_port,
    apply_success_latency_policy,
    scan_tier3_tcp_candidates,
    test_nodes as run_test_nodes,
    validate_tier3_responsive_nodes_with_mihomo_batch,
)


class TesterTests(unittest.TestCase):
    def test_mihomo_prewarm_config_rejects_unmatched_traffic(self) -> None:
        config = _build_mihomo_prewarm_config(ipv6=True)

        self.assertEqual(config["rules"], ["MATCH,REJECT"])
        self.assertEqual(config["proxies"], [])
        self.assertEqual(config["proxy-groups"], [])
        self.assertTrue(config["ipv6"])

    def test_primary_gateway_latency_policy_boundaries_and_recovery(self) -> None:
        testing_config = {
            "primary_gateway_max_latency_ms": 800,
            "slow_threshold_ms": 2000,
        }

        for tier in ("tier1", "tier2", "exit"):
            with self.subTest(tier=tier, latency_ms=400):
                result = apply_success_latency_policy(
                    Node(tier=tier),
                    400,
                    testing_config,
                    "tcp",
                )
                self.assertEqual(result.status, NodeStatus.ALIVE)
            with self.subTest(tier=tier, latency_ms=400.01):
                result = apply_success_latency_policy(
                    Node(tier=tier),
                    400.01,
                    testing_config,
                    "tcp",
                )
                self.assertEqual(result.status, NodeStatus.SLOW)
            with self.subTest(tier=tier, latency_ms=800):
                result = apply_success_latency_policy(
                    Node(tier=tier),
                    800,
                    testing_config,
                    "tcp",
                )
                self.assertEqual(result.status, NodeStatus.SLOW)
            with self.subTest(tier=tier, latency_ms=800.01):
                result = apply_success_latency_policy(
                    Node(tier=tier),
                    800.01,
                    testing_config,
                    "tcp",
                )
                self.assertEqual(result.status, NodeStatus.DEAD)
                self.assertEqual(result.latency_ms, 800.01)
                self.assertEqual(result.suspicious_reason, "latency_limit_exceeded")
                self.assertEqual(result.extra["latency_policy_state"], "over_limit")
                self.assertEqual(result.extra["latency_policy_limit_ms"], 800)
                self.assertEqual(result.extra["latency_policy_observed_ms"], 800.01)
                self.assertEqual(result.extra["latency_policy_scope"], "tcp")

        stale = Node(
            tier="tier1",
            status=NodeStatus.DEAD,
            latency_ms=900,
            suspicious_reason="latency_limit_exceeded",
            extra={
                "latency_policy_state": "over_limit",
                "latency_policy_limit_ms": 800,
                "latency_policy_observed_ms": 900,
                "latency_policy_scope": "tcp",
                "keep": "value",
            },
        )
        recovered = apply_success_latency_policy(stale, 399, testing_config, "tcp")

        self.assertEqual(recovered.status, NodeStatus.ALIVE)
        self.assertIsNone(recovered.suspicious_reason)
        self.assertEqual(recovered.extra, {"keep": "value"})
        self.assertEqual(stale.status, NodeStatus.DEAD)

    def test_tier3_and_unknown_tiers_retain_legacy_slow_threshold(self) -> None:
        testing_config = {
            "primary_gateway_max_latency_ms": 800,
            "slow_threshold_ms": 2000,
        }

        for tier in ("tier3", "", "future-tier"):
            with self.subTest(tier=tier, latency_ms=801):
                result = apply_success_latency_policy(
                    Node(tier=tier),
                    801,
                    testing_config,
                    "tcp",
                )
                self.assertEqual(result.status, NodeStatus.ALIVE)
                self.assertIsNone(result.suspicious_reason)
                self.assertNotIn("latency_policy_state", result.extra)
            with self.subTest(tier=tier, latency_ms=2000.01):
                result = apply_success_latency_policy(
                    Node(tier=tier),
                    2000.01,
                    testing_config,
                    "tcp",
                )
                self.assertEqual(result.status, NodeStatus.SLOW)

    def test_test_nodes_applies_hard_limit_to_successful_tcp_probe(self) -> None:
        class FakeWriter:
            def close(self) -> None:
                pass

            async def wait_closed(self) -> None:
                pass

        async def fake_open_connection(*_args, **_kwargs):
            return object(), FakeWriter()

        node = Node(
            name="primary",
            protocol="ss",
            address="primary.example",
            port=8388,
            tier="tier1",
        )
        with (
            patch("tester.asyncio.open_connection", fake_open_connection),
            patch("tester.time.perf_counter", side_effect=[0.0, 0.801]),
        ):
            result = asyncio.run(
                run_test_nodes(
                    [node],
                    testing_config={"primary_gateway_max_latency_ms": 800},
                    logger=logging.getLogger("test-primary-hard-latency"),
                )
            )

        self.assertEqual(result[0].status, NodeStatus.DEAD)
        self.assertEqual(result[0].latency_ms, 801)
        self.assertEqual(result[0].suspicious_reason, "latency_limit_exceeded")

    def test_xray_full_validation_applies_request_latency_without_startup(self) -> None:
        async def fake_tcp_probe(node, *_args, **_kwargs):
            updated = node.clone()
            updated.status = NodeStatus.ALIVE
            updated.latency_ms = 10
            return updated

        class FakeResponse:
            status = 204

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def getcode(self):
                return 204

            def read(self, _size=-1):
                return b""

        class FakeOpener:
            def open(self, _request, timeout=None):
                return FakeResponse()

        class FakeTempDirectory:
            def __init__(self, *_args, **_kwargs) -> None:
                pass

            def __enter__(self):
                return "."

            def __exit__(self, *_args):
                return False

        node = Node(
            name="primary-full",
            protocol="ss",
            address="primary-full.example",
            port=8388,
            tier="tier1",
        )
        with (
            patch("tester._tcp_probe", fake_tcp_probe),
            patch("tester._build_xray_config", return_value={"inbounds": []}),
            patch("tester.tempfile.TemporaryDirectory", FakeTempDirectory),
            patch("tester.Path.write_text"),
            patch("tester.subprocess.Popen", return_value=object()),
            patch("tester.time.sleep"),
            patch("tester._stop_process"),
            patch("tester.urllib.request.build_opener", return_value=FakeOpener()),
            patch("tester.time.perf_counter", side_effect=[20.0, 20.9]),
        ):
            result = asyncio.run(
                run_test_nodes(
                    [node],
                    testing_config={
                        "primary_gateway_max_latency_ms": 800,
                        "xray_helper": {
                            "enabled": True,
                            "binary_path": "fake-xray.exe",
                            "startup_delay": 30,
                        },
                    },
                    logger=logging.getLogger("test-xray-request-timing"),
                    full_validation_enabled=True,
                )
            )

        self.assertEqual(result[0].status, NodeStatus.DEAD)
        self.assertEqual(result[0].latency_ms, 900)
        self.assertEqual(result[0].suspicious_reason, "latency_limit_exceeded")
        self.assertEqual(result[0].extra["latency_policy_scope"], "xray_direct")

    def test_mihomo_direct_latency_excludes_startup_and_failed_probe_urls(self) -> None:
        class FakeResponse:
            status = 204

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def getcode(self):
                return 204

            def read(self, _size=-1):
                return b""

        class FakeOpener:
            def __init__(self) -> None:
                self.calls = 0

            def open(self, _request, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    raise TimeoutError("first endpoint failed")
                return FakeResponse()

        class FakeTempDir:
            name = "."

            def cleanup(self) -> None:
                pass

        node = Node(tier="tier1", protocol="ss", address="node.example", port=8388)
        with (
            patch("tester.Path.exists", return_value=True),
            patch("tester.Path.write_text"),
            patch("tester._temporary_validation_dir", return_value=FakeTempDir()),
            patch("tester._build_mihomo_direct_config", return_value={"mixed-port": 1}),
            patch("tester.subprocess.Popen", return_value=object()),
            patch("tester._wait_for_tcp_port"),
            patch("tester._stop_process"),
            patch("tester.urllib.request.build_opener", return_value=FakeOpener()),
            patch(
                "tester.time.perf_counter",
                side_effect=[0.0, 10.0, 20.0, 30.0, 30.3],
            ),
        ):
            result = _mihomo_direct_validate(
                node,
                {
                    "mihomo_binary_path": "fake-mihomo.exe",
                    "primary_gateway_max_latency_ms": 800,
                    "mihomo_helper": {"port_attempts": 1},
                },
                2000,
                logging.getLogger("test-direct-request-timing"),
                _probe_urls_override=["https://failed.example", "https://ok.example"],
            )

        self.assertEqual(result.status, NodeStatus.ALIVE)
        self.assertEqual(result.latency_ms, 300)
        self.assertEqual(result.extra["validation_helper_startup_elapsed_ms"], 10000)
        self.assertEqual(result.extra["validation_total_elapsed_ms"], 30300)

    def test_gateway_chain_latency_excludes_helper_startup(self) -> None:
        class FakeResponse:
            status = 204

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def getcode(self):
                return 204

            def read(self, _size=-1):
                return b""

        class FakeOpener:
            def open(self, _request, timeout=None):
                return FakeResponse()

        class FakeTempDir:
            name = "."

            def cleanup(self) -> None:
                pass

        node = Node(tier="tier2", protocol="ss", address="entry.example", port=8388)
        gateway = Node(
            tier="exit",
            protocol="ss",
            address="gateway.example",
            port=8388,
            status=NodeStatus.ALIVE,
        )
        with (
            patch("tester.Path.exists", return_value=True),
            patch("tester.Path.write_text"),
            patch("tester._temporary_validation_dir", return_value=FakeTempDir()),
            patch("tester._build_mihomo_chain_config", return_value={"mixed-port": 1}),
            patch("tester.subprocess.Popen", return_value=object()),
            patch("tester._wait_for_tcp_port"),
            patch("tester._stop_process"),
            patch("tester.urllib.request.build_opener", return_value=FakeOpener()),
            patch(
                "tester.time.perf_counter",
                side_effect=[0.0, 10.0, 20.0, 20.9],
            ),
        ):
            result = _gateway_chain_validate(
                node,
                [gateway],
                {
                    "mihomo_binary_path": "fake-mihomo.exe",
                    "primary_gateway_max_latency_ms": 800,
                    "mihomo_helper": {
                        "port_attempts": 1,
                        "probe_urls": ["https://ok.example"],
                    },
                },
                2000,
                logging.getLogger("test-gateway-request-timing"),
            )

        self.assertEqual(result.status, NodeStatus.DEAD)
        self.assertEqual(result.latency_ms, 900)
        self.assertEqual(result.suspicious_reason, "latency_limit_exceeded")
        self.assertEqual(result.extra["latency_policy_scope"], "gateway_chain")
        self.assertEqual(result.extra["validation_helper_startup_elapsed_ms"], 10000)
        self.assertEqual(result.extra["validation_total_elapsed_ms"], 20900)

    def test_validation_portfolio_always_preserves_two_http_and_one_https(self) -> None:
        portfolio = _mihomo_validation_probe_portfolio(
            [
                "https://probe-a.example/generate_204",
                "https://probe-b.example/generate_204",
            ],
            limit=3,
        )
        self.assertEqual(
            portfolio,
            [
                "http://connectivitycheck.gstatic.com/generate_204",
                "http://www.gstatic.com/generate_204",
                "https://probe-a.example/generate_204",
            ],
        )

    def test_primary_stable_hash_and_canonical_url_set_do_not_repeat_endpoint(self) -> None:
        portfolio = _mihomo_validation_probe_portfolio(
            ["https://probe.example/generate_204"],
            limit=3,
        )
        primary_choices = {
            _mihomo_primary_fast_probe_urls(portfolio, selection_key=f"identity-{index}")[0]
            for index in range(16)
        }
        self.assertEqual(
            primary_choices,
            {
                "http://connectivitycheck.gstatic.com/generate_204",
                "http://www.gstatic.com/generate_204",
            },
        )
        attempted = "http://connectivitycheck.gstatic.com/generate_204"
        self.assertEqual(
            _mihomo_canonical_probe_urls(portfolio, attempted_urls={attempted}),
            [
                "http://www.gstatic.com/generate_204",
                "https://probe.example/generate_204",
            ],
        )
        self.assertEqual(
            _mihomo_canonical_probe_urls(
                portfolio,
                attempted_urls={attempted},
                preferred_urls=["https://probe.example/generate_204"],
            ),
            [
                "https://probe.example/generate_204",
                "http://www.gstatic.com/generate_204",
            ],
        )

    def test_real_probe_outcome_records_per_endpoint_exposure(self) -> None:
        class FakeResponse:
            status = 204

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def getcode(self):
                return 204

            def read(self, _size=-1):
                return b""

        class FakeOpener:
            def open(self, _request, timeout=None):
                time.sleep(0.01)
                return FakeResponse()

        url = "http://probe.example/generate_204"
        outcome = _run_mihomo_real_probe_attempts(
            FakeOpener(),
            [url],
            {204},
            probe_timeout_seconds=1.0,
            deadline_at=time.monotonic() + 1.0,
            initial_parallelism=1,
        )

        self.assertIsNotNone(outcome.result)
        self.assertGreater(outcome.exposure_ms_by_url[url], 0.0)

    def test_real_probe_deadline_marks_started_probe_censored_not_exhausted(self) -> None:
        class BlockingOpener:
            def open(self, _request, timeout=None):
                time.sleep(0.2)
                raise TimeoutError("late transport result")

        url = "http://probe.example/generate_204"
        outcome = _run_mihomo_real_probe_attempts(
            BlockingOpener(),
            [url],
            {204},
            probe_timeout_seconds=1.0,
            deadline_at=time.monotonic() + 0.05,
            initial_parallelism=1,
        )

        self.assertTrue(outcome.deadline_hit)
        self.assertEqual(outcome.started_urls, [url])
        self.assertEqual(outcome.completed_urls, [])
        self.assertEqual(outcome.exhausted_urls, [])
        self.assertEqual(outcome.censored_urls, [url])
        self.assertEqual(outcome.next_probe_index, 0)

    def test_endpoint_health_does_not_treat_premature_censor_as_transient(self) -> None:
        url = "http://probe.example/generate_204"
        node = Node(
            name="censored-endpoint",
            protocol="vless",
            address="censored.example.com",
            port=443,
            source="root-a",
            tier="tier3",
        )
        health = _MihomoProbeEndpointHealth(minimum_attempts=1)
        health.record(
            node,
            _MihomoRealProbeOutcome(
                result=None,
                failures=[],
                failures_by_url={},
                attempted_urls=[url],
                elapsed_ms=50,
                deadline_hit=True,
                probe_sequence_starved=True,
                late_completed_ignored=1,
                next_probe_index=0,
                total_probe_count=1,
                exposure_ms_by_url={url: 50.0},
                completed_urls=[],
                exhausted_urls=[],
                censored_urls=[url],
            ),
        )

        snapshot = health.snapshot()[url]
        self.assertEqual(snapshot["started"], 1)
        self.assertEqual(snapshot["attempted"], 0)
        self.assertEqual(snapshot["transient"], 0)
        self.assertEqual(snapshot["censored"], 1)
        self.assertFalse(snapshot["cooldown"])

    def test_probe_entitlement_exhaustion_is_a_verdict_not_a_deferral(self) -> None:
        """A node that burns its whole probe slot without answering is judged.

        Reporting that as ``validation_deferred`` made it permanently
        unjudgeable: transient, never counted as a failure, and refused by the
        negative cache, so the same unresponsive candidates were re-probed
        every wave and crowded out candidates that had never been tried.
        """

        import tier_cache
        from tester import _mihomo_session_real_probe_validate_node

        url = "http://probe.example/generate_204"
        node = Node(
            name="slot-exhausted",
            protocol="vless",
            address="slot.example.com",
            port=443,
            tier="tier3",
        )
        outcome = _MihomoRealProbeOutcome(
            result=None,
            failures=[],
            failures_by_url={},
            attempted_urls=[url],
            elapsed_ms=3000,
            deadline_hit=True,
            probe_sequence_starved=False,
            late_completed_ignored=0,
            next_probe_index=0,
            total_probe_count=1,
        )

        def run(*_args, **_kwargs):
            return outcome

        cacheable_prefixes = (
            "mihomo_validation_failed:",
            "mihomo_batch_validation_failed:",
            "all_gateways_unavailable:",
        )

        # Slot entitlement expired with no wave deadline in play: node-attributable.
        with patch("tester._run_mihomo_real_probe_attempts", run):
            exhausted = _mihomo_session_real_probe_validate_node(
                None,
                node,
                "TEST-PROXY",
                mixed_port=1080,
                probe_urls=[url],
                expected_statuses={204},
                probe_timeout_seconds=5.0,
                slow_threshold_ms=2000,
                deadline_at=None,
                probe_slot_deadline_seconds=3.0,
            )
        reason = str(exhausted.suspicious_reason or "")
        stage = str(exhausted.extra.get("tier3_failure_stage") or "")
        self.assertTrue(exhausted.extra.get("tier3_probe_entitlement_exhausted"))
        self.assertFalse(reason.startswith("validation_deferred:"), reason)
        self.assertTrue(reason.startswith(cacheable_prefixes), reason)
        # Short cooldown, so this is a retryable verdict and not a permanent ban.
        self.assertIn(stage, tier_cache.NON_PERSISTENT_NEGATIVE_FAILURE_STAGES)
        lowered = reason.lower()
        self.assertNotIn("http error 503", lowered)
        self.assertNotIn("http error 504", lowered)

        # An external wave deadline still cuts the probe short for scheduling
        # reasons, so the node was never judged and must stay deferred.
        with patch("tester._run_mihomo_real_probe_attempts", run):
            deferred = _mihomo_session_real_probe_validate_node(
                None,
                node,
                "TEST-PROXY",
                mixed_port=1080,
                probe_urls=[url],
                expected_statuses={204},
                probe_timeout_seconds=5.0,
                slow_threshold_ms=2000,
                deadline_at=time.monotonic() + 30.0,
                probe_slot_deadline_seconds=3.0,
            )
        deferred_reason = str(deferred.suspicious_reason or "")
        self.assertTrue(deferred_reason.startswith("validation_deferred:"), deferred_reason)
        self.assertFalse(deferred.extra.get("tier3_probe_entitlement_exhausted"))

    def test_endpoint_health_cooldown_is_session_local_and_half_open(self) -> None:
        url = "http://probe.example/generate_204"
        health = _MihomoProbeEndpointHealth(
            minimum_attempts=8,
            transient_threshold=0.8,
            cooldown_seconds=10.0,
        )
        for index in range(8):
            node = Node(
                name=f"endpoint-{index}",
                protocol="vless" if index % 2 == 0 else "ss",
                address=f"endpoint-{index}.example.com",
                port=443,
                source=f"root-{index % 2}",
                tier="tier3",
                extra={"tier3_source_root_id": f"root-{index % 2}"},
            )
            health.record(
                node,
                _MihomoRealProbeOutcome(
                    result=None,
                    failures=[f"{url}: timed out"],
                    failures_by_url={url: "timed out"},
                    attempted_urls=[url],
                    elapsed_ms=1000,
                    deadline_hit=False,
                    probe_sequence_starved=False,
                    late_completed_ignored=0,
                    next_probe_index=1,
                    total_probe_count=1,
                ),
            )
        snapshot = health.snapshot()[url]
        self.assertTrue(snapshot["cooldown"])
        self.assertEqual(snapshot["root_count"], 2)
        self.assertEqual(snapshot["protocol_count"], 2)
        self.assertEqual(health.available_urls([url]), [])

        half_open = health.available_urls([url], now=time.monotonic() + 11.0)
        self.assertEqual(half_open, [url])
        self.assertTrue(health.snapshot()[url]["half_open"])
        success_node = Node(
            name="endpoint-success",
            protocol="vless",
            address="endpoint-success.example.com",
            port=443,
            source="root-0",
            tier="tier3",
        )
        health.record(
            success_node,
            _MihomoRealProbeOutcome(
                result=_MihomoRealProbeAttemptResult(
                    probe_url=url,
                    success=True,
                    status_code=204,
                    body_ok=True,
                    latency_ms=20.0,
                    failure="",
                ),
                failures=[],
                failures_by_url={},
                attempted_urls=[url],
                elapsed_ms=20,
                deadline_hit=False,
                probe_sequence_starved=False,
                late_completed_ignored=0,
                next_probe_index=1,
                total_probe_count=1,
            ),
        )
        recovered = health.snapshot()[url]
        self.assertFalse(recovered["cooldown"])
        self.assertFalse(recovered["half_open"])

    def test_primary_fast_probe_prefers_one_plain_http_probe(self) -> None:
        self.assertEqual(
            _mihomo_primary_fast_probe_urls(
                [
                    "https://cp.cloudflare.com",
                    "http://connectivitycheck.gstatic.com/generate_204",
                    "https://www.gstatic.com/generate_204",
                ]
            ),
            ["http://connectivitycheck.gstatic.com/generate_204"],
        )

    def test_canonical_probe_cursor_resumes_without_replaying_completed_probe(self) -> None:
        attempted: list[str] = []

        class FakeResponse:
            status = 204

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def getcode(self):
                return 204

            def read(self, _size=-1):
                return b""

        class FakeOpener:
            def open(self, request, timeout=None):
                attempted.append(request.full_url)
                return FakeResponse()

        outcome = _run_mihomo_real_probe_attempts(
            FakeOpener(),
            ["http://probe-1.test", "http://probe-2.test"],
            {204},
            probe_timeout_seconds=1.0,
            deadline_at=time.monotonic() + 1.0,
            initial_parallelism=1,
            probe_start_index=1,
        )

        self.assertIsNotNone(outcome.result)
        self.assertEqual(attempted, ["http://probe-2.test"])
        self.assertEqual(outcome.next_probe_index, 2)
        self.assertEqual(outcome.total_probe_count, 2)

    def test_canonical_probe_deadline_retries_the_incomplete_probe_cursor(self) -> None:
        class FakeOpener:
            def open(self, request, timeout=None):
                if request.full_url.endswith("probe-1"):
                    raise OSError("first failed")
                time.sleep(0.2)
                raise TimeoutError("late")

        outcome = _run_mihomo_real_probe_attempts(
            FakeOpener(),
            ["http://probe-1", "http://probe-2", "http://probe-3"],
            {204},
            probe_timeout_seconds=1.0,
            deadline_at=time.monotonic() + 0.05,
            initial_parallelism=1,
        )

        self.assertTrue(outcome.deadline_hit)
        self.assertEqual(outcome.next_probe_index, 1)
        self.assertEqual(outcome.total_probe_count, 3)
        self.assertTrue(outcome.probe_sequence_starved)



    def test_stop_process_uses_windows_process_tree_kill_when_terminate_times_out(self) -> None:
        class StubbornProcess:
            pid = 12345

            def __init__(self) -> None:
                self.terminated = False
                self.killed = False

            def poll(self):
                return None

            def terminate(self) -> None:
                self.terminated = True

            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired("mihomo", timeout)

            def kill(self) -> None:
                self.killed = True

        process = StubbornProcess()
        with patch("tester.sys.platform", "win32"), patch("tester.subprocess.run") as taskkill:
            _stop_process(process)  # type: ignore[arg-type]

        self.assertTrue(process.terminated)
        self.assertTrue(process.killed)
        taskkill.assert_called_once()
        self.assertEqual(taskkill.call_args.args[0], ["taskkill", "/PID", "12345", "/T", "/F"])

    def test_stop_process_does_not_taskkill_when_process_exits_cleanly(self) -> None:
        class GracefulProcess:
            pid = 12346

            def __init__(self) -> None:
                self.running = True

            def poll(self):
                return None if self.running else 0

            def terminate(self) -> None:
                self.running = False

            def wait(self, timeout=None):
                return 0

            def kill(self) -> None:
                raise AssertionError("kill should not be called")

        with patch("tester.sys.platform", "win32"), patch("tester.subprocess.run") as taskkill:
            _stop_process(GracefulProcess())  # type: ignore[arg-type]

        taskkill.assert_not_called()

    def test_wait_for_tcp_port_cancel_interrupts_startup_poll(self) -> None:
        cancel_event = threading.Event()
        timer = threading.Timer(0.03, cancel_event.set)
        timer.start()
        started = time.monotonic()
        try:
            with (
                patch(
                    "tester.socket.create_connection",
                    side_effect=OSError("not ready"),
                ),
                self.assertRaisesRegex(RuntimeError, "validation_cancelled"),
            ):
                _wait_for_tcp_port(
                    "127.0.0.1",
                    65530,
                    5.0,
                    cancel_event=cancel_event,
                )
        finally:
            timer.cancel()

        self.assertLess(time.monotonic() - started, 0.25)

    def test_mihomo_probe_urls_keep_cloudflare_fallback_after_configured_url(self) -> None:
        urls = _mihomo_probe_urls({"mihomo_helper": {"probe_url": "https://www.gstatic.com/generate_204"}})

        self.assertEqual(urls[0], "https://www.gstatic.com/generate_204")
        self.assertIn("https://cp.cloudflare.com", urls)

    def test_mihomo_probe_urls_do_not_inherit_xray_google_default(self) -> None:
        urls = _mihomo_probe_urls({"xray_helper": {"probe_url": "https://www.google.com/generate_204"}})

        self.assertEqual(
            urls,
            [
                "https://cp.cloudflare.com",
                "https://www.gstatic.com/generate_204",
                "http://connectivitycheck.gstatic.com/generate_204",
                "http://www.gstatic.com/generate_204",
            ],
        )

    def test_mihomo_probe_initial_cohort_prefers_two_http_fallbacks(self) -> None:
        urls = _mihomo_probe_urls({})

        cohort = _mihomo_probe_urls_for_initial_cohort(urls, 3)[:3]

        self.assertEqual(cohort[0], "https://cp.cloudflare.com")
        self.assertIn("http://connectivitycheck.gstatic.com/generate_204", cohort)
        self.assertIn("http://www.gstatic.com/generate_204", cohort)
        self.assertEqual(sum(1 for url in cohort if url.startswith("http://")), 2)

    def test_mihomo_direct_validation_marks_missing_binary_suspicious(self) -> None:
        node = Node(
            name="free-vless",
            protocol="vless",
            uri="vless://11111111-1111-1111-1111-111111111111@example.com:443#free",
            address="example.com",
            port=443,
            uuid="11111111-1111-1111-1111-111111111111",
            tier="tier3",
            status=NodeStatus.ALIVE,
        )

        result = _mihomo_direct_validate(
            node,
            {"mihomo_binary_path": str(Path("missing-mihomo.exe"))},
            1000,
            logging.getLogger("test-mihomo-direct"),
        )

        self.assertEqual(result.status, NodeStatus.SUSPICIOUS)
        self.assertIn("mihomo binary not found", str(result.suspicious_reason))

    def test_tier3_full_validation_uses_mihomo_even_when_xray_disabled(self) -> None:
        node = Node(
            name="free-vless",
            protocol="vless",
            address="example.com",
            port=443,
            uuid="11111111-1111-1111-1111-111111111111",
            tier="tier3",
        )

        async def fake_tcp_probe(node, *_args, **_kwargs):
            updated = node.clone()
            updated.status = NodeStatus.ALIVE
            updated.latency_ms = 10
            return updated

        def fake_mihomo_validate(node, *_args, **_kwargs):
            updated = node.clone()
            updated.status = NodeStatus.SUSPICIOUS
            updated.suspicious_reason = "mihomo-called"
            return updated

        with patch("tester._tcp_probe", fake_tcp_probe), patch("tester._mihomo_direct_validate", fake_mihomo_validate):
            result = asyncio.run(
                run_test_nodes(
                    [node],
                    testing_config={
                        "xray_helper": {"enabled": False},
                        "mihomo_helper": {"enabled": True, "batch_enabled": False},
                    },
                    logger=logging.getLogger("test-tier3-mihomo-validation"),
                    full_validation_enabled=True,
                )
            )

        self.assertEqual(result[0].status, NodeStatus.SUSPICIOUS)
        self.assertEqual(result[0].suspicious_reason, "mihomo-called")

    def test_tier3_full_validation_batches_mihomo_proxy_delay(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            binary_path = Path(temp_dir) / "mihomo.exe"
            binary_path.write_text("fake", encoding="utf-8")
            nodes = [
                Node(
                    name=f"free-{index}",
                    protocol="ss",
                    address=f"free-{index}.example.com",
                    port=8388 + index,
                    method="aes-128-gcm",
                    password="secret",
                    tier="tier3",
                )
                for index in range(3)
            ]
            observed: list[str] = []
            popen_calls: list[list[str]] = []

            async def fake_tcp_probe(node, *_args, **_kwargs):
                updated = node.clone()
                updated.status = NodeStatus.ALIVE
                updated.latency_ms = 10
                return updated

            class FakeProcess:
                pid = 12345

                def __init__(self) -> None:
                    self.running = True

                def poll(self):
                    return None if self.running else 0

                def terminate(self) -> None:
                    self.running = False

                def wait(self, timeout=None):
                    self.running = False
                    return 0

                def kill(self) -> None:
                    self.running = False

            def fake_popen(command, *_args, **_kwargs):
                popen_calls.append(command)
                return FakeProcess()

            class FakeMihomoClient:
                def __init__(self, **_kwargs) -> None:
                    pass

                def proxy_delay(self, name, *, url, timeout_ms=5000):
                    return {"delay": int(str(name).rsplit("-", 1)[-1]) * 10}

            with (
                patch("tester._tcp_probe", fake_tcp_probe),
                patch("tester.subprocess.Popen", fake_popen),
                patch("tester._wait_for_tcp_port"),
                patch("tester.MihomoAPIClient", FakeMihomoClient),
            ):
                result = asyncio.run(
                    run_test_nodes(
                        nodes,
                        testing_config={
                            "mihomo_binary_path": str(binary_path),
                            "mihomo_helper": {"enabled": True, "concurrency": 3, "probe_urls": ["https://cp.cloudflare.com"]},
                        },
                        logger=logging.getLogger("test-tier3-batch-validation"),
                        full_validation_enabled=True,
                        result_observer=lambda node: observed.append(node.name),
                    )
                )

            self.assertEqual(len(popen_calls), 1)
            self.assertEqual({node.name for node in result}, {node.name for node in nodes})
            self.assertTrue(all(node.status == NodeStatus.ALIVE for node in result))
            self.assertTrue(all(node.extra.get("validated_by") == "mihomo-batch" for node in result))
            self.assertEqual(set(observed), {node.name for node in nodes})

















    # Historical daemon-thread stress oracle; the production equivalent is the
    # process-domain 10,000-terminal release soak.

    # Historical in-process stress oracle. Production validation now uses the
    # Job-bound helper process domain covered by test_tier3_validator_process_domain.






    def test_tier3_mihomo_batch_fallback_defers_nodes_after_wave_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            binary_path = Path(temp_dir) / "mihomo.exe"
            binary_path.write_text("fake", encoding="utf-8")
            node = Node(
                name="fallback-deadline",
                protocol="ss",
                address="fallback-deadline.example.com",
                port=8443,
                method="aes-128-gcm",
                password="secret",
                tier="tier3",
                status=NodeStatus.ALIVE,
            )
            release_proxy_delay = threading.Event()
            observed: list[Node] = []

            class FakeProcess:
                pid = 12356

                def poll(self):
                    return None

                def terminate(self) -> None:
                    pass

                def wait(self, timeout=None):
                    return 0

                def kill(self) -> None:
                    pass

            class FakeMihomoClient:
                def __init__(self, **_kwargs) -> None:
                    pass

                def proxy_delay(self, name, *, url, timeout_ms=5000):
                    release_proxy_delay.wait(1.0)
                    return {"delay": 20}

            with (
                patch("tester.subprocess.Popen", lambda *_args, **_kwargs: FakeProcess()),
                patch("tester._wait_for_tcp_port"),
                patch("tester.MihomoAPIClient", FakeMihomoClient),
            ):
                try:
                    started = time.monotonic()
                    result = validate_tier3_responsive_nodes_with_mihomo_batch(
                        [node],
                        {
                            "mihomo_binary_path": str(binary_path),
                            "tier3_validation_deadline_seconds": 0.05,
                            "tier3_primary_probe_only": True,
                            "mihomo_helper": {
                                "enabled": True,
                                "batch_enabled": True,
                                "probe_urls": ["https://cp.cloudflare.com"],
                            },
                        },
                        logging.getLogger("test-tier3-batch-deadline"),
                        concurrency=1,
                        result_observer=observed.append,
                    )
                    elapsed = time.monotonic() - started
                finally:
                    release_proxy_delay.set()

            self.assertLess(elapsed, 0.55)
            self.assertEqual(len(result), 1)
            self.assertEqual(len(observed), 1)
            self.assertEqual(result[0].extra.get("tier3_failure_stage"), "validation_deferred")
            self.assertTrue(result[0].extra.get("tier3_full_validation_deadline_hit"))

    def test_responsive_tier3_batch_validation_uses_injected_session(self) -> None:
        node = Node(
            name="injected-session",
            protocol="ss",
            address="injected.example.com",
            port=8388,
            method="aes-128-gcm",
            password="secret",
            tier="tier3",
            status=NodeStatus.UNKNOWN,
        )
        observed: list[Node] = []

        class FakeSession:
            def __init__(self) -> None:
                self.calls = 0

            def validate(self, nodes, *, result_observer=None, max_workers=None, cancel_event=None):
                self.calls += 1
                updated = nodes[0].clone()
                updated.status = NodeStatus.ALIVE
                updated.latency_ms = 12
                if callable(result_observer):
                    result_observer(updated)
                return [updated]

        session = FakeSession()

        def fail_batch(*_args, **_kwargs):
            raise AssertionError("injected session should bypass one-shot batch validation")

        with patch("tester._mihomo_batch_validate", fail_batch):
            result = validate_tier3_responsive_nodes_with_mihomo_batch(
                [node],
                {
                    "tier3_responsive_candidates": True,
                    "mihomo_validation_session": session,
                    "mihomo_helper": {"enabled": True, "batch_enabled": True},
                },
                logging.getLogger("test-tier3-injected-session"),
                concurrency=1,
                result_observer=observed.append,
            )

        self.assertEqual(session.calls, 1)
        self.assertEqual(result[0].status, NodeStatus.ALIVE)
        self.assertEqual(observed[0].name, "injected-session")

    def test_responsive_tier3_batch_validation_can_cancel_running_proxy_delay(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            binary_path = Path(temp_dir) / "mihomo.exe"
            binary_path.write_text("fake", encoding="utf-8")
            nodes = [
                Node(
                    name=f"cancel-{index}",
                    protocol="ss",
                    address=f"cancel-{index}.example.com",
                    port=8388 + index,
                    method="aes-128-gcm",
                    password="secret",
                    tier="tier3",
                    status=NodeStatus.ALIVE,
                )
                for index in range(3)
            ]
            cancel_event = threading.Event()
            proxy_delay_started = threading.Event()
            release_proxy_delay = threading.Event()
            observed: list[Node] = []
            result: list[Node] = []
            errors: list[BaseException] = []

            class FakeProcess:
                pid = 12347

                def poll(self):
                    return None

                def terminate(self) -> None:
                    pass

                def wait(self, timeout=None):
                    return 0

                def kill(self) -> None:
                    pass

            class FakeMihomoClient:
                def __init__(self, **_kwargs) -> None:
                    pass

                def proxy_delay(self, name, *, url, timeout_ms=5000):
                    proxy_delay_started.set()
                    release_proxy_delay.wait(5)
                    return {"delay": 20}

            def run_validation() -> None:
                try:
                    result.extend(
                        validate_tier3_responsive_nodes_with_mihomo_batch(
                            nodes,
                            {
                                "mihomo_binary_path": str(binary_path),
                                "validation_cancel_event": cancel_event,
                                "tier3_responsive_candidates": True,
                                "mihomo_helper": {
                                    "enabled": True,
                                    "batch_enabled": True,
                                    "concurrency": 3,
                                    "probe_timeout": 5,
                                },
                            },
                            logging.getLogger("test-tier3-cancellable-batch"),
                            concurrency=3,
                            result_observer=observed.append,
                        )
                    )
                except BaseException as exc:  # pragma: no cover - surfaced below
                    errors.append(exc)

            with (
                patch("tester.subprocess.Popen", return_value=FakeProcess()),
                patch("tester._wait_for_tcp_port"),
                patch("tester.MihomoAPIClient", FakeMihomoClient),
            ):
                thread = threading.Thread(target=run_validation)
                thread.start()
                try:
                    self.assertTrue(proxy_delay_started.wait(2))
                    cancel_event.set()
                    thread.join(2)
                    self.assertFalse(thread.is_alive())
                finally:
                    release_proxy_delay.set()
                    thread.join(2)

            if errors:
                raise errors[0]
            self.assertEqual(len(result), len(nodes))
            self.assertEqual(observed, [])
            self.assertTrue(
                all(str(node.suspicious_reason or "").startswith("validation_cancelled") for node in result)
            )

    def test_responsive_tier3_unsupported_proxy_skips_per_node_fallback_when_marked_outside(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            binary_path = Path(temp_dir) / "mihomo.exe"
            binary_path.write_text("fake", encoding="utf-8")
            node = Node(
                name="unsupported-outside",
                protocol="ss",
                address="unsupported.example.com",
                port=8388,
                tier="tier3",
                status=NodeStatus.ALIVE,
            )
            observed: list[Node] = []

            def fail_direct_fallback(*_args, **_kwargs):
                raise AssertionError("outside unsupported proxy should not start per-node fallback")

            with patch("tester._mihomo_direct_validate_many", fail_direct_fallback):
                result = validate_tier3_responsive_nodes_with_mihomo_batch(
                    [node],
                    {
                        "mihomo_binary_path": str(binary_path),
                        "tier3_responsive_candidates": True,
                        "tier3_no_unsupported_fallback": True,
                        "mihomo_helper": {"enabled": True, "batch_enabled": True, "concurrency": 1},
                    },
                    logging.getLogger("test-tier3-unsupported-outside"),
                    concurrency=1,
                    result_observer=observed.append,
                )

            self.assertEqual(len(result), 1)
            self.assertEqual(len(observed), 1)
            self.assertEqual(result[0].status, NodeStatus.SUSPICIOUS)
            self.assertEqual(result[0].suspicious_reason, "mihomo_validation_failed: unsupported proxy")

    def test_tier3_batch_validation_falls_back_to_direct_with_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            binary_path = Path(temp_dir) / "mihomo.exe"
            binary_path.write_text("fake", encoding="utf-8")
            nodes = [
                Node(
                    name=f"fallback-{index}",
                    protocol="ss",
                    address=f"fallback-{index}.example.com",
                    port=8388 + index,
                    method="aes-128-gcm",
                    password="secret",
                    tier="tier3",
                )
                for index in range(2)
            ]
            popen_calls: list[list[str]] = []
            direct_calls: list[str] = []

            async def fake_tcp_probe(node, *_args, **_kwargs):
                updated = node.clone()
                updated.status = NodeStatus.ALIVE
                updated.latency_ms = 10
                return updated

            class FakeProcess:
                pid = 12346

                def poll(self):
                    return None

                def terminate(self) -> None:
                    pass

                def wait(self, timeout=None):
                    return 0

                def kill(self) -> None:
                    pass

            def fake_popen(command, *_args, **_kwargs):
                popen_calls.append(command)
                return FakeProcess()

            def fake_direct_validate(node, *_args, **_kwargs):
                direct_calls.append(node.name)
                updated = node.clone()
                updated.status = NodeStatus.ALIVE
                updated.latency_ms = 20
                updated.extra["validated_by"] = "mihomo-direct-fallback"
                return updated

            logger = logging.getLogger("test-tier3-batch-fallback")
            with (
                patch("tester._tcp_probe", fake_tcp_probe),
                patch("tester.subprocess.Popen", fake_popen),
                patch("tester._wait_for_tcp_port", side_effect=TimeoutError("controller timeout")),
                patch("tester._mihomo_direct_validate", fake_direct_validate),
                self.assertLogs(logger, level="WARNING") as logs,
            ):
                result = asyncio.run(
                    run_test_nodes(
                        nodes,
                        testing_config={
                            "mihomo_binary_path": str(binary_path),
                            "mihomo_helper": {"enabled": True, "concurrency": 2, "port_attempts": 1},
                        },
                        logger=logger,
                        full_validation_enabled=True,
                    )
                )

            self.assertEqual(len(popen_calls), 1)
            self.assertEqual(set(direct_calls), {node.name for node in nodes})
            self.assertTrue(all(node.extra.get("validated_by") == "mihomo-direct-fallback" for node in result))
            self.assertIn("falling back to per-node validation", "\n".join(logs.output))

    def test_result_observer_receives_nodes_as_each_probe_finishes(self) -> None:
        nodes = [
            Node(name="slow", protocol="ss", address="slow.example", port=8388, tier="tier3"),
            Node(name="fast", protocol="ss", address="fast.example", port=8389, tier="tier3"),
        ]
        observed: list[str] = []

        async def fake_probe(node, *_args, **_kwargs):
            if node.name == "slow":
                await asyncio.sleep(0.05)
            updated = node.clone()
            updated.status = NodeStatus.ALIVE
            updated.latency_ms = 10
            return updated

        with patch("tester._probe_with_optional_validation", fake_probe):
            result = asyncio.run(
                run_test_nodes(
                    nodes,
                    testing_config={"concurrency": 2},
                    logger=logging.getLogger("test-result-observer-streams"),
                    result_observer=lambda node: observed.append(node.name),
                )
            )

        self.assertEqual(observed, ["fast", "slow"])
        self.assertEqual({node.name for node in result}, {"fast", "slow"})

    def test_tcp_wave_deadline_defers_slow_tier3_candidates(self) -> None:
        nodes = [
            Node(name=f"slow-tcp-{index}", protocol="ss", address=f"slow-{index}.example", port=8388, tier="tier3")
            for index in range(2)
        ]
        observed: list[Node] = []

        async def fake_tcp_probe(node, *_args, **_kwargs):
            await asyncio.sleep(5)
            updated = node.clone()
            updated.status = NodeStatus.ALIVE
            updated.latency_ms = 10
            return updated

        started = time.perf_counter()
        with patch("tester._tcp_probe", fake_tcp_probe):
            result = asyncio.run(
                run_test_nodes(
                    nodes,
                    testing_config={
                        "concurrency": 2,
                        "tcp_wave_deadline_seconds": 0.05,
                    },
                    logger=logging.getLogger("test-tcp-wave-deadline"),
                    result_observer=observed.append,
                )
            )
        elapsed = time.perf_counter() - started

        self.assertLess(elapsed, 1.0)
        self.assertEqual(len(result), len(nodes))
        self.assertEqual(len(observed), len(nodes))
        self.assertTrue(all(node.extra.get("tier3_failure_stage") == "tcp_deferred" for node in result))
        self.assertTrue(all(str(node.suspicious_reason or "").startswith("tcp_deferred:") for node in result))

    def test_tcp_candidate_scan_cancel_closes_pending_sockets_without_observer_updates(self) -> None:
        nodes = [
            Node(
                name=f"cancel-scan-{index}",
                protocol="ss",
                address=f"cancel-scan-{index}.example",
                port=8388 + index,
                tier="tier3",
            )
            for index in range(4)
        ]
        cancel_event = threading.Event()
        socket_started: list[str] = []
        socket_cancelled: list[str] = []
        observed: list[str] = []

        async def pending_open_connection(host, _port, **_kwargs):
            socket_started.append(str(host))
            if len(socket_started) == 1:
                asyncio.get_running_loop().call_later(0.02, cancel_event.set)
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                socket_cancelled.append(str(host))
                raise
            raise AssertionError("pending TCP connection unexpectedly completed")

        with patch("tester.asyncio.open_connection", pending_open_connection):
            outcome = asyncio.run(
                scan_tier3_tcp_candidates(
                    nodes,
                    testing_config={
                        "concurrency": 2,
                        "tcp_timeout": 10,
                        "tcp_wave_cleanup_slack_seconds": 0.25,
                    },
                    logger=logging.getLogger("test-scan-cancel-pending-sockets"),
                    concurrency=2,
                    result_observer=lambda node: observed.append(node.name),
                    cancel_event=cancel_event,
                )
            )

        self.assertTrue(cancel_event.is_set())
        self.assertGreater(len(socket_started), 0)
        self.assertCountEqual(socket_cancelled, socket_started)
        self.assertEqual(observed, [])
        self.assertTrue(outcome.metadata["cancelled"])
        self.assertEqual(len(outcome.nodes), len(nodes))
        self.assertTrue(
            all("tcp_wave_cancelled" in str(node.suspicious_reason) for node in outcome.nodes)
        )

    def test_test_nodes_cancel_closes_pending_sockets_without_observer_updates(self) -> None:
        nodes = [
            Node(
                name=f"cancel-test-nodes-{index}",
                protocol="ss",
                address=f"cancel-test-nodes-{index}.example",
                port=8388 + index,
                tier="tier3",
            )
            for index in range(4)
        ]
        cancel_event = threading.Event()
        socket_started: list[str] = []
        socket_cancelled: list[str] = []
        observed: list[str] = []

        async def pending_open_connection(host, _port, **_kwargs):
            socket_started.append(str(host))
            if len(socket_started) == 1:
                asyncio.get_running_loop().call_later(0.02, cancel_event.set)
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                socket_cancelled.append(str(host))
                raise
            raise AssertionError("pending TCP connection unexpectedly completed")

        with patch("tester.asyncio.open_connection", pending_open_connection):
            result = asyncio.run(
                run_test_nodes(
                    nodes,
                    testing_config={
                        "concurrency": 2,
                        "tcp_timeout": 10,
                        "tcp_wave_cleanup_slack_seconds": 0.25,
                    },
                    logger=logging.getLogger("test-nodes-cancel-pending-sockets"),
                    result_observer=lambda node: observed.append(node.name),
                    cancel_event=cancel_event,
                )
            )

        self.assertTrue(cancel_event.is_set())
        self.assertGreater(len(socket_started), 0)
        self.assertCountEqual(socket_cancelled, socket_started)
        self.assertEqual(observed, [])
        self.assertEqual(len(result), len(nodes))
        self.assertTrue(
            all("tcp_wave_cancelled" in str(node.suspicious_reason) for node in result)
        )

    def test_scan_tier3_tcp_candidates_hard_bounds_deadline_cleanup(self) -> None:
        nodes = [
            Node(name=f"slow-scan-{index}", protocol="ss", address=f"slow-scan-{index}.example", port=8388, tier="tier3")
            for index in range(3)
        ]
        observed: list[Node] = []

        async def fake_tcp_probe(node, *_args, **_kwargs):
            await asyncio.sleep(5)
            updated = node.clone()
            updated.status = NodeStatus.ALIVE
            updated.latency_ms = 10
            return updated

        started = time.perf_counter()
        with patch("tester._tcp_probe", fake_tcp_probe):
            outcome = asyncio.run(
                scan_tier3_tcp_candidates(
                    nodes,
                    testing_config={
                        "concurrency": 3,
                        "tcp_wave_deadline_seconds": 0.05,
                        "tcp_wave_cleanup_slack_seconds": 5.0,
                    },
                    logger=logging.getLogger("test-scan-tcp-deadline"),
                    concurrency=3,
                    result_observer=observed.append,
                )
            )
            result = outcome.nodes
        elapsed = time.perf_counter() - started

        self.assertLess(elapsed, 0.75)
        self.assertEqual(len(result), len(nodes))
        self.assertEqual(len(observed), len(nodes))
        self.assertTrue(all(node.extra.get("tier3_failure_stage") == "tcp_deferred" for node in result))
        self.assertTrue(all(str(node.suspicious_reason or "").startswith("tcp_deferred:") for node in result))
        self.assertTrue(outcome.metadata["deadline_hit"])
        self.assertEqual(outcome.metadata["deferred_by_deadline"], len(nodes))
        self.assertLessEqual(outcome.metadata["cleanup_slack_ms"], 500)
        self.assertLessEqual(outcome.metadata["tcp_scan_elapsed_ms"], 550)

    def test_scan_tier3_tcp_candidates_does_not_wait_for_slow_close(self) -> None:
        node = Node(name="close-hangs", protocol="ss", address="close-hangs.example", port=8388, tier="tier3")
        observed: list[Node] = []

        class FakeWriter:
            def close(self) -> None:
                pass

            async def wait_closed(self) -> None:
                await asyncio.sleep(5)

        async def fake_open_connection(*_args, **_kwargs):
            return object(), FakeWriter()

        started = time.perf_counter()
        with patch("asyncio.open_connection", fake_open_connection):
            outcome = asyncio.run(
                scan_tier3_tcp_candidates(
                    [node],
                    testing_config={
                        "concurrency": 1,
                        "tcp_timeout": 10,
                        "tcp_wave_deadline_seconds": 0.2,
                        "tcp_wave_cleanup_slack_seconds": 0.25,
                    },
                    logger=logging.getLogger("test-scan-close-does-not-block"),
                    concurrency=1,
                    result_observer=observed.append,
                )
            )
        elapsed = time.perf_counter() - started

        self.assertLess(elapsed, 0.75)
        self.assertEqual(len(outcome.nodes), 1)
        self.assertEqual(outcome.nodes[0].status, NodeStatus.ALIVE)
        self.assertEqual(len(observed), 1)
        self.assertFalse(outcome.metadata["deadline_hit"])
        self.assertLess(outcome.metadata["tcp_scan_elapsed_ms"], 200)

    def test_scan_tier3_tcp_candidates_hard_censors_noncooperative_probe(self) -> None:
        node = Node(name="late-dead", protocol="ss", address="late-dead.example", port=8388, tier="tier3")

        async def fake_tcp_probe(node_arg, *_args, **_kwargs):
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                updated = node_arg.clone()
                updated.status = NodeStatus.DEAD
                updated.suspicious_reason = "late tcp dead"
                return updated
            raise AssertionError("probe should have been cancelled by deadline")

        with patch("tester._tcp_probe", fake_tcp_probe):
            outcome = asyncio.run(
                scan_tier3_tcp_candidates(
                    [node],
                    testing_config={
                        "concurrency": 1,
                        "tcp_wave_deadline_seconds": 0.05,
                        "tcp_wave_cleanup_slack_seconds": 0.25,
                    },
                    logger=logging.getLogger("test-scan-late-result-ignored"),
                    concurrency=1,
                )
            )

        self.assertEqual(len(outcome.nodes), 1)
        self.assertEqual(outcome.nodes[0].extra.get("tier3_failure_stage"), "tcp_deferred")
        self.assertTrue(str(outcome.nodes[0].suspicious_reason or "").startswith("tcp_deferred:"))
        self.assertTrue(outcome.metadata["deadline_hit"])
        self.assertEqual(outcome.metadata["deferred_by_deadline"], 1)
        self.assertEqual(outcome.metadata["hard_censored_count"], 1)

    def test_tcp_entitlement_accepts_full_budget_dead_in_delivery_grace(self) -> None:
        node = Node(
            name="full-budget-dead",
            protocol="ss",
            address="full-budget-dead.example",
            port=8388,
            tier="tier3",
        )

        async def fake_tcp_probe(node_arg, *_args, **_kwargs):
            complete_after = time.monotonic() + 0.08
            while time.monotonic() < complete_after:
                await asyncio.sleep(0.005)
            updated = node_arg.clone()
            updated.status = NodeStatus.DEAD
            updated.suspicious_reason = "connect timeout"
            return updated

        with patch("tester._tcp_probe", fake_tcp_probe):
            outcome = asyncio.run(
                scan_tier3_tcp_candidates(
                    [node],
                    testing_config={
                        "tcp_timeout": 0.05,
                        "tcp_probe_entitlement_seconds": 0.05,
                        "tcp_wave_cleanup_slack_seconds": 0.1,
                    },
                    logger=logging.getLogger("test-tcp-delivery-grace"),
                    concurrency=1,
                )
            )

        self.assertEqual(len(outcome.nodes), 1)
        self.assertEqual(outcome.nodes[0].status, NodeStatus.DEAD)
        self.assertEqual(outcome.metadata["completed_count"], 1)
        self.assertEqual(outcome.metadata["completed_before_deadline"], 0)
        self.assertEqual(outcome.metadata["hard_censored_count"], 0)

    def test_tcp_entitlements_start_four_transports_concurrently(self) -> None:
        nodes = [
            Node(
                name=f"parallel-{index}",
                protocol="ss",
                address=f"parallel-{index}.example",
                port=8388 + index,
                tier="tier3",
            )
            for index in range(4)
        ]
        started_at: list[float] = []
        acknowledged_at: list[float] = []
        all_started = asyncio.Event()

        async def fake_tcp_probe(node, *_args, **_kwargs):
            started_at.append(time.perf_counter())
            if len(started_at) == len(nodes):
                all_started.set()
            await asyncio.wait_for(all_started.wait(), timeout=0.1)
            updated = node.clone()
            updated.status = NodeStatus.ALIVE
            return updated

        async def run_scan():
            with patch("tester._tcp_probe", fake_tcp_probe):
                return await scan_tier3_tcp_candidates(
                    nodes,
                    testing_config={
                        "tcp_timeout": 0.2,
                        "tcp_probe_entitlement_seconds": 0.2,
                    },
                    logger=logging.getLogger("test-tcp-entitlement-parallel"),
                    concurrency=4,
                    transport_start_observer=(
                        lambda _node, started: acknowledged_at.append(started)
                        or True
                    ),
                )

        outcome = asyncio.run(run_scan())

        self.assertEqual(len(started_at), 4)
        self.assertEqual(len(acknowledged_at), 4)
        self.assertLess(max(started_at) - min(started_at), 0.05)
        self.assertEqual(outcome.metadata["transport_started_count"], 4)
        self.assertEqual(
            outcome.metadata["transport_start_acknowledged_count"],
            4,
        )
        self.assertEqual(outcome.metadata["deferred_by_deadline"], 0)

    def test_tcp_entitlement_accepts_completion_collected_after_deadline(self) -> None:
        node = Node(
            name="completed-before-deadline",
            protocol="ss",
            address="completed-before-deadline.example",
            port=8388,
            tier="tier3",
        )

        async def run_scan():
            entitlement = Tier3TcpProbeEntitlement(node=node)

            async def probe(_timeout_seconds: float) -> Node:
                await asyncio.sleep(0.005)
                updated = node.clone()
                updated.status = NodeStatus.ALIVE
                return updated

            task = asyncio.create_task(
                _run_tcp_probe_entitlement(
                    entitlement,
                    semaphore=asyncio.Semaphore(1),
                    entitlement_seconds=0.05,
                    cleanup_slack_seconds=0.25,
                    cancel_event=None,
                    probe_factory=probe,
                )
            )
            while not task.done():
                await asyncio.sleep(0.001)
            await asyncio.sleep(
                max(0.0, entitlement.deadline_at - time.monotonic()) + 0.01
            )
            return await _collect_tcp_probe_entitlements(
                {task: entitlement},
                entitlement_seconds=0.05,
                dispatch_start_guard_seconds=0.25,
                cleanup_slack_seconds=0.25,
            )

        outcome = asyncio.run(run_scan())

        self.assertEqual(len(outcome.nodes), 1)
        self.assertEqual(outcome.nodes[0].status, NodeStatus.ALIVE)
        self.assertEqual(outcome.metadata["completed_before_deadline"], 1)
        self.assertEqual(outcome.metadata["deferred_by_deadline"], 0)

    def test_tcp_entitlement_freezes_current_envelope_at_transport_start(self) -> None:
        node = Node(
            name="dynamic-entitlement",
            protocol="ss",
            address="dynamic-entitlement.example",
            port=8388,
            tier="tier3",
        )
        observed_timeouts: list[float] = []

        async def run_probe() -> tuple[Node, Tier3TcpProbeEntitlement]:
            entitlement = Tier3TcpProbeEntitlement(node=node)

            async def probe(timeout_seconds: float) -> Node:
                observed_timeouts.append(timeout_seconds)
                updated = node.clone()
                updated.status = NodeStatus.DEAD
                return updated

            result = await _run_tcp_probe_entitlement(
                entitlement,
                semaphore=asyncio.Semaphore(1),
                entitlement_seconds=3.74,
                cleanup_slack_seconds=0.25,
                cancel_event=None,
                probe_factory=probe,
                transport_entitlement_provider=lambda _node, _started_at: (
                    42,
                    1.25,
                ),
            )
            return result, entitlement

        result, entitlement = asyncio.run(run_probe())

        self.assertEqual(result.status, NodeStatus.DEAD)
        self.assertEqual(observed_timeouts, [1.25])
        self.assertEqual(entitlement.envelope_revision, 42)
        self.assertEqual(entitlement.entitlement_seconds, 1.25)
        self.assertAlmostEqual(
            entitlement.transport_deadline - entitlement.transport_started_at,
            1.25,
            places=3,
        )

    def test_tcp_start_rejection_has_no_transport_or_entitlement_side_effect(self) -> None:
        node = Node(
            name="start-rejected",
            protocol="ss",
            address="start-rejected.example",
            port=8388,
            tier="tier3",
        )
        probe_called = False

        async def fake_tcp_probe(*_args, **_kwargs):
            nonlocal probe_called
            probe_called = True
            raise AssertionError("rejected handoff must not start transport")

        with patch("tester._tcp_probe", fake_tcp_probe):
            outcome = asyncio.run(
                scan_tier3_tcp_candidates(
                    [node],
                    testing_config={
                        "tcp_timeout": 0.2,
                        "tcp_probe_entitlement_seconds": 0.2,
                    },
                    logger=logging.getLogger("test-tcp-start-rejected"),
                    concurrency=1,
                    transport_start_observer=lambda _node, _started: False,
                )
            )

        self.assertFalse(probe_called)
        self.assertEqual(outcome.metadata["transport_started_count"], 0)
        self.assertEqual(
            outcome.metadata["transport_start_acknowledged_count"],
            0,
        )
        self.assertEqual(outcome.metadata["transport_start_cancelled_count"], 1)
        self.assertEqual(outcome.metadata["deferred_before_transport_start"], 0)
        self.assertIn(
            "tcp_handoff_start_rejected",
            str(outcome.nodes[0].suspicious_reason),
        )

    def test_tcp_entitlement_defers_only_item_not_started_within_guard(self) -> None:
        nodes = [
            Node(
                name=f"guard-{index}",
                protocol="ss",
                address=f"guard-{index}.example",
                port=8388 + index,
                tier="tier3",
            )
            for index in range(2)
        ]

        async def fake_tcp_probe(node, *_args, **_kwargs):
            await asyncio.sleep(0.06)
            updated = node.clone()
            updated.status = NodeStatus.ALIVE
            return updated

        with patch("tester._tcp_probe", fake_tcp_probe):
            outcome = asyncio.run(
                scan_tier3_tcp_candidates(
                    nodes,
                    testing_config={
                        "tcp_timeout": 0.2,
                        "tcp_probe_entitlement_seconds": 0.2,
                        "tcp_dispatch_start_guard_seconds": 0.03,
                    },
                    logger=logging.getLogger("test-tcp-entitlement-start-guard"),
                    concurrency=1,
                )
            )

        self.assertEqual(len(outcome.nodes), 2)
        self.assertEqual(outcome.metadata["transport_started_count"], 1)
        self.assertEqual(outcome.metadata["deferred_before_transport_start"], 1)
        self.assertEqual(outcome.metadata["transport_start_cancelled_count"], 0)
        self.assertEqual(
            sum(node.status is NodeStatus.ALIVE for node in outcome.nodes),
            1,
        )

    def test_string_false_mihomo_helper_does_not_run_tier3_validation(self) -> None:
        node = Node(
            name="free-vless",
            protocol="vless",
            address="example.com",
            port=443,
            uuid="11111111-1111-1111-1111-111111111111",
            tier="tier3",
        )

        async def fake_tcp_probe(node, *_args, **_kwargs):
            updated = node.clone()
            updated.status = NodeStatus.ALIVE
            updated.latency_ms = 10
            return updated

        def fail_mihomo_validate(*_args, **_kwargs):
            raise AssertionError("mihomo validation should be disabled")

        with patch("tester._tcp_probe", fake_tcp_probe), patch("tester._mihomo_direct_validate", fail_mihomo_validate):
            result = asyncio.run(
                run_test_nodes(
                    [node],
                    testing_config={"mihomo_helper": {"enabled": "false"}},
                    logger=logging.getLogger("test-string-false-mihomo-disabled"),
                    full_validation_enabled=True,
                )
            )

        self.assertEqual(result[0].status, NodeStatus.ALIVE)

    def test_string_false_full_validation_flag_does_not_run_validation(self) -> None:
        node = Node(
            name="free-vless",
            protocol="vless",
            address="example.com",
            port=443,
            uuid="11111111-1111-1111-1111-111111111111",
            tier="tier3",
        )

        async def fake_tcp_probe(node, *_args, **_kwargs):
            updated = node.clone()
            updated.status = NodeStatus.ALIVE
            updated.latency_ms = 10
            return updated

        def fail_mihomo_validate(*_args, **_kwargs):
            raise AssertionError("full validation should be disabled")

        with patch("tester._tcp_probe", fake_tcp_probe), patch("tester._mihomo_direct_validate", fail_mihomo_validate):
            result = asyncio.run(
                run_test_nodes(
                    [node],
                    testing_config={"mihomo_helper": {"enabled": True}},
                    logger=logging.getLogger("test-string-false-full-validation-disabled"),
                    full_validation_enabled="false",
                )
            )

        self.assertEqual(result[0].status, NodeStatus.ALIVE)

    def test_invalid_testing_numbers_use_defaults_without_blocking_node_tests(self) -> None:
        node = Node(name="free-vless", protocol="vless", address="example.com", port=443, tier="tier3")
        captured_thresholds: list[int] = []
        captured_timeouts: list[float] = []

        async def fake_tcp_probe(node, *_args, **kwargs):
            captured_thresholds.append(kwargs["slow_threshold_ms"])
            captured_timeouts.append(kwargs["timeout_seconds"])
            updated = node.clone()
            updated.status = NodeStatus.ALIVE
            updated.latency_ms = 10
            return updated

        with patch("tester._tcp_probe", fake_tcp_probe):
            result = asyncio.run(
                run_test_nodes(
                    [node],
                    testing_config={
                        "concurrency": "bad",
                        "slow_threshold_ms": "bad",
                        "tcp_timeout": "bad",
                        "xray_helper": False,
                    },
                    logger=logging.getLogger("test-invalid-testing-numbers"),
                )
            )

        self.assertEqual(result[0].status, NodeStatus.ALIVE)
        self.assertEqual(captured_thresholds, [2000])
        self.assertEqual(captured_timeouts, [5.0])

    def test_tier3_mihomo_validation_uses_helper_concurrency(self) -> None:
        nodes = [
            Node(
                name=f"free-{index}",
                protocol="vless",
                address=f"free-{index}.example.com",
                port=443,
                uuid=f"11111111-1111-1111-1111-11111111111{index}",
                tier="tier3",
            )
            for index in range(5)
        ]
        active = 0
        max_active = 0

        async def fake_tcp_probe(node, *_args, **_kwargs):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            updated = node.clone()
            updated.status = NodeStatus.ALIVE
            active -= 1
            return updated

        def fake_mihomo_validate(node, *_args, **_kwargs):
            return node

        with patch("tester._tcp_probe", fake_tcp_probe), patch("tester._mihomo_direct_validate", fake_mihomo_validate):
            result = asyncio.run(
                run_test_nodes(
                    nodes,
                    testing_config={
                        "concurrency": 50,
                        "xray_helper": {"enabled": False},
                        "mihomo_helper": {"enabled": True, "batch_enabled": False, "concurrency": 2},
                    },
                    logger=logging.getLogger("test-tier3-mihomo-concurrency"),
                    full_validation_enabled=True,
                )
            )

        self.assertEqual(len(result), 5)
        self.assertGreater(max_active, 1)
        self.assertLessEqual(max_active, 2)

    def test_responsive_tier3_batch_helper_uses_single_mihomo_batch(self) -> None:
        nodes = [
            Node(
                name=f"responsive-{index}",
                protocol="ss",
                uri=f"ss://responsive-{index}",
                address=f"responsive-{index}.example.com",
                port=8388,
                tier="tier3",
                status=NodeStatus.ALIVE,
            )
            for index in range(4)
        ]
        observed: list[str] = []
        batch_calls = 0

        def fake_batch_validate(nodes_arg, _testing_config, _slow_threshold_ms, _logger, **kwargs):
            nonlocal batch_calls
            batch_calls += 1
            for node in nodes_arg:
                updated = node.clone()
                updated.status = NodeStatus.ALIVE
                updated.latency_ms = 20
                kwargs["result_observer"](updated)
            return list(nodes_arg)

        with patch("tester._mihomo_batch_validate", fake_batch_validate):
            result = validate_tier3_responsive_nodes_with_mihomo_batch(
                nodes,
                {"mihomo_helper": {"enabled": True, "batch_enabled": True, "concurrency": 8}},
                logging.getLogger("test-responsive-tier3-helper"),
                concurrency=8,
                result_observer=lambda node: observed.append(node.name),
            )

        self.assertEqual(len(result), 4)
        self.assertEqual(observed, [node.name for node in nodes])
        self.assertEqual(batch_calls, 1)

    def test_responsive_tier3_marker_skips_tcp_prefilter_before_mihomo_batch(self) -> None:
        nodes = [
            Node(
                name=f"responsive-marker-{index}",
                protocol="ss",
                uri=f"ss://responsive-marker-{index}",
                address=f"responsive-marker-{index}.example.com",
                port=8388,
                tier="tier3",
                status=NodeStatus.ALIVE,
            )
            for index in range(3)
        ]
        batch_calls = 0

        async def fail_tcp_probe(*_args, **_kwargs):
            raise AssertionError("responsive Tier3 candidates should not repeat TCP prefilter")

        def fake_batch_validate(nodes_arg, _testing_config, _slow_threshold_ms, _logger, **kwargs):
            nonlocal batch_calls
            batch_calls += 1
            result = []
            for node in nodes_arg:
                updated = node.clone()
                updated.status = NodeStatus.ALIVE
                updated.latency_ms = 25
                result.append(updated)
                kwargs["result_observer"](updated)
            return result

        observed: list[str] = []
        with patch("tester._tcp_probe", fail_tcp_probe), patch("tester._mihomo_batch_validate", fake_batch_validate):
            result = asyncio.run(
                run_test_nodes(
                    nodes,
                    testing_config={
                        "tier3_responsive_candidates": True,
                        "mihomo_helper": {"enabled": True, "batch_enabled": True, "concurrency": 8},
                    },
                    logger=logging.getLogger("test-responsive-tier3-marker"),
                    full_validation_enabled=True,
                    result_observer=lambda node: observed.append(node.name),
                )
            )

        self.assertEqual(len(result), 3)
        self.assertEqual(observed, [node.name for node in nodes])
        self.assertEqual(batch_calls, 1)

    def test_responsive_tier3_batch_validation_ignores_gateway_context(self) -> None:
        nodes = [
            Node(
                name=f"responsive-gateway-{index}",
                protocol="ss",
                uri=f"ss://responsive-gateway-{index}",
                address=f"responsive-gateway-{index}.example.com",
                port=8388,
                tier="tier3",
                status=NodeStatus.ALIVE,
            )
            for index in range(2)
        ]
        gateway = Node(
            name="gateway",
            protocol="ss",
            uri="ss://gateway",
            address="gateway.example.com",
            port=8388,
            tier="exit",
            status=NodeStatus.ALIVE,
        )
        batch_calls = 0
        gateway_calls = 0

        def fake_batch_validate(nodes_arg, _testing_config, _slow_threshold_ms, _logger, **kwargs):
            nonlocal batch_calls
            batch_calls += 1
            result = []
            for node in nodes_arg:
                updated = node.clone()
                updated.status = NodeStatus.ALIVE
                updated.latency_ms = 25
                result.append(updated)
                kwargs["result_observer"](updated)
            return result

        def fake_gateway_validate(*_args, **_kwargs):
            nonlocal gateway_calls
            gateway_calls += 1
            raise AssertionError("responsive Tier3 candidates should use batch full validation")

        observed: list[str] = []
        with (
            patch("tester._mihomo_batch_validate", fake_batch_validate),
            patch("tester._gateway_chain_validate", fake_gateway_validate),
        ):
            result = asyncio.run(
                run_test_nodes(
                    nodes,
                    testing_config={
                        "tier3_responsive_candidates": True,
                        "mihomo_helper": {"enabled": True, "batch_enabled": True, "concurrency": 8},
                    },
                    logger=logging.getLogger("test-responsive-tier3-gateway-context"),
                    full_validation_enabled=True,
                    gateway_nodes=[gateway],
                    result_observer=lambda node: observed.append(node.name),
                )
            )

        self.assertEqual(len(result), 2)
        self.assertEqual(observed, [node.name for node in nodes])
        self.assertEqual(batch_calls, 1)
        self.assertEqual(gateway_calls, 0)

    def test_gateway_chain_failure_marks_node_suspicious(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            binary_path = Path(temp_dir) / "mihomo.exe"
            binary_path.write_text("fake", encoding="utf-8")
            node = Node(
                name="entry",
                protocol="vless",
                uri="vless://11111111-1111-1111-1111-111111111111@example.com:443#entry",
                address="example.com",
                port=443,
                uuid="11111111-1111-1111-1111-111111111111",
                tier="tier2",
            )
            unsupported_gateway = Node(
                name="bad-gateway",
                protocol="unsupported",
                uri="",
                address="gateway.example",
                port=443,
                tier="exit",
            )

            result = _gateway_chain_validate(
                node,
                [unsupported_gateway],
                {"mihomo_binary_path": str(binary_path), "mihomo_helper": {"port_attempts": 1}},
                1000,
                logging.getLogger("test-gateway-chain"),
            )

            self.assertEqual(result.status, NodeStatus.SUSPICIOUS)
            self.assertIn("all_gateways_unavailable", str(result.suspicious_reason))


if __name__ == "__main__":
    unittest.main()
