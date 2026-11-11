from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable

from runtime_tier_state import (
    should_stop_tier3_core_probe,
    tier3_core_probe_plan,
)


class Tier3CoreProbeIndeterminate(RuntimeError):
    """A candidate probe did not start and therefore has no health verdict."""


Tier3CoreProbeCandidate = Callable[
    [str, Any, int],
    tuple[bool, float, Any, str, str, list[str]],
]


@dataclass(frozen=True)
class Tier3CoreProbeRun:
    successful_results: list[tuple[float, Any, str, str]]
    failures: list[str]
    tested_proxy_names: set[str]
    concurrency: int
    candidate_count: int
    indeterminate_proxy_names: set[str]


def run_tier3_core_probe(
    pairs: list[tuple[str, Any]],
    *,
    timeout_ms: int,
    configured_concurrency: Any,
    prune_to_successful: bool,
    target_verified: int,
    probe_candidate: Tier3CoreProbeCandidate,
) -> Tier3CoreProbeRun:
    candidates = list(pairs)
    probe_plan = tier3_core_probe_plan(
        candidates,
        configured_concurrency=configured_concurrency,
    )
    successful: list[tuple[float, Any, str, str]] = []
    failures: list[str] = []
    tested_proxy_names: set[str] = set()
    indeterminate_proxy_names: set[str] = set()
    concurrency = probe_plan.concurrency
    for batch in probe_plan.batches:
        with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="tier3-core-probe") as executor:
            future_map = {
                executor.submit(
                    probe_candidate,
                    proxy_name,
                    node,
                    timeout_ms,
                ): proxy_name
                for proxy_name, node in batch
            }
            for future in as_completed(future_map):
                proxy_name = future_map[future]
                try:
                    ok, delay, node, tested_name, probe_url, probe_failures = future.result()
                except Tier3CoreProbeIndeterminate:
                    # Queue/socket budget saturation is not evidence about the
                    # candidate.  In particular it must not enter the tested
                    # set, because callers use that set to apply health strikes.
                    indeterminate_proxy_names.add(proxy_name)
                    continue
                except Exception as exc:
                    tested_proxy_names.add(proxy_name)
                    failures.append(f"{proxy_name}: {exc}")
                    continue
                tested_proxy_names.add(proxy_name)
                failures.extend(probe_failures)
                if ok:
                    successful.append((delay, node, tested_name, probe_url))
        if should_stop_tier3_core_probe(
            prune_to_successful=prune_to_successful,
            successful_count=len(successful),
            target_verified=target_verified,
        ):
            break
    return Tier3CoreProbeRun(
        successful_results=successful,
        failures=failures,
        tested_proxy_names=tested_proxy_names,
        concurrency=concurrency,
        candidate_count=len(candidates),
        indeterminate_proxy_names=indeterminate_proxy_names,
    )
