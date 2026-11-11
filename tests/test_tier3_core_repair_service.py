import unittest

from tier3_core_repair_service import (
    Tier3CoreProbeIndeterminate,
    run_tier3_core_probe,
)


class Tier3CoreRepairServiceTests(unittest.TestCase):
    def test_probe_run_stops_after_target_batch_when_pruning(self) -> None:
        calls: list[tuple[str, object, int]] = []

        def probe(proxy_name: str, node: object, timeout_ms: int):
            calls.append((proxy_name, node, timeout_ms))
            if proxy_name == "one":
                return False, 0.0, node, proxy_name, "", ["one failed"]
            return True, 25.0, node, proxy_name, f"https://probe/{proxy_name}", []

        run = run_tier3_core_probe(
            [("one", "node-one"), ("two", "node-two"), ("three", "node-three")],
            timeout_ms=123,
            configured_concurrency=2,
            prune_to_successful=True,
            target_verified=1,
            probe_candidate=probe,
        )

        self.assertCountEqual([name for name, _node, _timeout in calls], ["one", "two"])
        self.assertTrue(all(timeout == 123 for _name, _node, timeout in calls))
        self.assertEqual(len(run.successful_results), 1)
        self.assertEqual(run.successful_results[0][2], "two")
        self.assertEqual(run.failures, ["one failed"])
        self.assertEqual(run.tested_proxy_names, {"one", "two"})
        self.assertEqual(run.concurrency, 2)
        self.assertEqual(run.candidate_count, 3)

    def test_probe_run_continues_without_pruning_and_captures_failures(self) -> None:
        def probe(proxy_name: str, node: object, timeout_ms: int):
            if proxy_name == "bad":
                raise RuntimeError("broken")
            if proxy_name == "soft-fail":
                return False, 0.0, node, proxy_name, "", ["soft failed"]
            return True, 10.0, node, proxy_name, "https://probe/good", []

        run = run_tier3_core_probe(
            [("bad", object()), ("soft-fail", object()), ("good", object())],
            timeout_ms=500,
            configured_concurrency=1,
            prune_to_successful=False,
            target_verified=1,
            probe_candidate=probe,
        )

        self.assertEqual(len(run.successful_results), 1)
        self.assertEqual(run.successful_results[0][2], "good")
        self.assertEqual(run.failures, ["bad: broken", "soft failed"])
        self.assertEqual(run.tested_proxy_names, {"bad", "soft-fail", "good"})

    def test_probe_without_socket_capacity_is_not_a_tested_failure(self) -> None:
        def probe(proxy_name: str, node: object, timeout_ms: int):
            del node, timeout_ms
            if proxy_name == "queued":
                raise Tier3CoreProbeIndeterminate("probe budget unavailable")
            return True, 12.0, object(), proxy_name, "https://probe/good", []

        run = run_tier3_core_probe(
            [("queued", object()), ("good", object())],
            timeout_ms=500,
            configured_concurrency=1,
            prune_to_successful=False,
            target_verified=1,
            probe_candidate=probe,
        )

        self.assertEqual(run.indeterminate_proxy_names, {"queued"})
        self.assertEqual(run.tested_proxy_names, {"good"})
        self.assertEqual(run.failures, [])
        self.assertEqual([item[2] for item in run.successful_results], ["good"])


if __name__ == "__main__":
    unittest.main()
