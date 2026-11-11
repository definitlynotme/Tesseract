from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import queue
import tempfile
import time
from collections import OrderedDict, deque
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

from clash_formatter import node_to_clash_proxy
from collector import (
    collect_nodes_from_sources_with_report,
    load_yaml,
    source_entries_from_config,
)
from models import Node, NodeStatus, utc_now_iso
from tester import _mihomo_direct_validate, scan_tier3_tcp_candidates
from tier3_collection_settings import tier3_collection_settings
from tier3_sources import (
    DEFAULT_GITHUB_CANDIDATE_PATHS,
    canonical_source_identity,
    source_entries_from_tier3_config,
)
from tier3_validation_domain import process_validator_settings, tier3_candidate_config_digest
from tier3_validation_protocol import (
    EndpointAttemptResult,
    ValidateEndpointCmd,
    ValidationStage,
    TIER3_VALIDATION_CONTRACT_HASH,
    canonical_json,
)
from tier3_validator_supervisor import Tier3ValidatorSupervisor


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


def _tier3_config(config: dict[str, Any]) -> dict[str, Any]:
    tiers = config.get("tiers")
    tiers = dict(tiers) if isinstance(tiers, dict) else {}
    value = tiers.get("tier3")
    return dict(value) if isinstance(value, dict) else {}


def _source_entries(
    config: dict[str, Any],
    sources_payload: dict[str, Any],
) -> list[dict[str, Any]]:
    tier3 = _tier3_config(config)
    entries = [
        *source_entries_from_tier3_config(tier3),
    ]
    if bool(tier3.get("bootstrap_enabled", True)):
        entries.extend(source_entries_from_config(sources_payload))
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or not bool(entry.get("enabled", True)):
            continue
        identity = canonical_source_identity(
            str(entry.get("type") or ""),
            str(entry.get("value") or ""),
        )
        if not identity or identity in seen:
            continue
        seen.add(identity)
        deduped.append(dict(entry))
    return deduped


def _root_family(node: Node) -> tuple[str, str]:
    return (
        str(
            node.extra.get("tier3_source_root_id")
            or node.extra.get("tier3_source_root_key")
            or node.source
            or "unknown"
        ),
        str(
            node.extra.get("tier3_source_unit_family")
            or node.extra.get("tier3_protocol_family")
            or node.protocol
            or "unknown"
        ),
    )


def _root_family_drr(nodes: Iterable[Node]) -> list[Node]:
    roots: OrderedDict[str, OrderedDict[str, deque[Node]]] = OrderedDict()
    for node in nodes:
        root, family = _root_family(node)
        roots.setdefault(root, OrderedDict()).setdefault(family, deque()).append(node)
    ordered: list[Node] = []
    while roots:
        for root in tuple(roots):
            families = roots[root]
            for family in tuple(families):
                bucket = families[family]
                if bucket:
                    ordered.append(bucket.popleft())
                if not bucket:
                    families.pop(family, None)
                break
            if not families:
                roots.pop(root, None)
    return ordered


def _classification_from_oracle(node: Node) -> str:
    if node.status in {NodeStatus.ALIVE, NodeStatus.SLOW}:
        return "pass"
    reason = str(node.suspicious_reason or "").lower()
    if "unsupported" in reason or "unexpected probe status=4" in reason:
        return "candidate_fail"
    if any(
        marker in reason
        for marker in (
            "timeout",
            "timed out",
            "http error 5",
            "tls",
            "eof",
            "connection reset",
            "remote end closed",
        )
    ):
        return "censored"
    if any(marker in reason for marker in ("binary not found", "requires mihomo")):
        return "facility_fail"
    return "candidate_fail"


def _classification_from_helper(kind: str) -> str:
    normalized = str(kind or "").strip().lower()
    if normalized in {"right_censored", "transient"}:
        return "censored"
    return normalized


def _candidate_digest(node: Node) -> str:
    # A parity run must not reuse a helper config across distinct rendered
    # options (for example ALPN or obfuscation stored in Node.extra).
    return tier3_candidate_config_digest(node)


def _wait_for_helpers(supervisor: Tier3ValidatorSupervisor, count: int) -> None:
    # The supervisor starts at most four helper shells and four Mihomo pairs at
    # a time. A full 16-worker parity ladder therefore has a different bounded
    # setup cost from the one-worker packaged audit; first-candidate latency is
    # covered separately and must not depend on all workers becoming ready.
    startup_bound = max(15.0, min(30.0, float(max(1, count)) * 2.0))
    deadline = time.monotonic() + startup_bound
    while time.monotonic() < deadline:
        snapshot = supervisor.snapshot()
        if snapshot.idle_workers >= count:
            return
        if snapshot.hard_stall:
            raise RuntimeError(snapshot.last_failure_reason or "helper hard stall")
        time.sleep(0.05)
    raise TimeoutError("parity helper did not become ready")


def _parity_helper_command(
    node: Node,
    endpoint: str,
    sequence: int,
) -> ValidateEndpointCmd:
    digest = _candidate_digest(node)
    return ValidateEndpointCmd(
        refresh_epoch="tier3-parity-audit",
        candidate_digest=digest,
        config_digest=digest,
        lease_id=f"parity:{sequence}:{digest[:16]}",
        sequence=sequence,
        node_json=canonical_json(node.to_dict()),
        source_root=_root_family(node)[0],
        source_family=_root_family(node)[1],
        transaction_id=f"parity:{digest[:24]}",
        slice_id=1,
        stage=ValidationStage.CANONICAL_DEEP,
        endpoint=endpoint,
        probe_budget_seconds=5.0,
        candidate_hard_cap_seconds=7.75,
        portfolio_digest=hashlib.sha256(endpoint.encode("utf-8")).hexdigest()[:24],
        endpoint_selection_reason="parity_exact_endpoint",
        endpoint_canonical_position=0,
        submitted_monotonic_ns=time.monotonic_ns(),
    )


def _run_helper_attempt(
    supervisor: Tier3ValidatorSupervisor,
    results: queue.Queue[EndpointAttemptResult],
    node: Node,
    endpoint: str,
    sequence: int,
) -> EndpointAttemptResult:
    command = _parity_helper_command(node, endpoint, sequence)
    supervisor.submit(command)
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            result = results.get(timeout=0.1)
        except queue.Empty:
            continue
        if result.lease_id == command.lease_id:
            return result
    raise TimeoutError("parity helper attempt did not return")


def _build_parity_supervisor(
    *,
    mihomo_binary_path: Path,
    endpoints: tuple[str, ...],
    validator: dict[str, Any],
    testing: dict[str, Any],
    results: queue.Queue[EndpointAttemptResult],
) -> Tier3ValidatorSupervisor:
    """Build the audit supervisor through the current production contract."""

    return Tier3ValidatorSupervisor(
        binary_path=str(mihomo_binary_path),
        probe_urls=list(endpoints),
        expected_statuses=set(validator["expected_statuses"]),
        probe_timeout_seconds=float(validator["probe_timeout_seconds"]),
        slow_threshold_ms=int(testing.get("slow_threshold_ms") or 3000),
        capacity_sink=lambda _count: None,
        attempt_sink=results.put_nowait,
        min_workers=1,
        max_workers=1,
        temp_parent=tempfile.gettempdir(),
        ipv6=bool(validator["ipv6"]),
    )


def run_tier3_validation_parity_audit(
    *,
    config: dict[str, Any],
    sources_path: Path,
    mihomo_binary_path: Path,
    artifact_path: Path,
    sample_size: int = 24,
) -> tuple[int, dict[str, Any]]:
    """Run a no-admission helper/oracle audit over normal source and TCP input."""

    logger = logging.getLogger("tier3-validation-parity-audit")
    logger.setLevel(logging.ERROR)
    sample_size = max(1, min(64, int(sample_size)))
    sources_payload = load_yaml(sources_path) if sources_path.exists() else {}
    sources_payload = (
        dict(sources_payload) if isinstance(sources_payload, dict) else {}
    )
    entries = _source_entries(config, sources_payload)
    if not entries:
        payload = {
            "generated_at": utc_now_iso(),
            "validation_contract_hash": TIER3_VALIDATION_CONTRACT_HASH,
            "verdict": "no_configured_sources",
            "sample_size": 0,
        }
        _atomic_write_json(artifact_path, payload)
        return 1, payload

    collection = config.get("collection")
    collection = dict(collection) if isinstance(collection, dict) else {}
    settings = tier3_collection_settings(
        collection,
        sources_payload,
        default_github_candidate_paths=DEFAULT_GITHUB_CANDIDATE_PATHS,
    )
    settings["source_first_result_deadline_seconds"] = 8.0
    settings["source_cancel_slack_seconds"] = 0.25
    nodes, collection_report = asyncio.run(
        collect_nodes_from_sources_with_report(
            source_entries=entries,
            tier="tier3",
            logger=logger,
            settings=settings,
        )
    )
    supported = [
        node
        for node in nodes
        if node_to_clash_proxy(node, logger=logger, seen_names=set()) is not None
    ]
    ordered = _root_family_drr(supported)

    testing = config.get("testing")
    testing = dict(testing) if isinstance(testing, dict) else {}
    helper = testing.get("mihomo_helper")
    helper = dict(helper) if isinstance(helper, dict) else {}
    helper["binary_path"] = str(mihomo_binary_path)
    testing["mihomo_helper"] = helper
    testing["mihomo_binary_path"] = str(mihomo_binary_path)
    responsive: list[Node] = []
    cursor = 0
    while cursor < len(ordered) and len(responsive) < sample_size:
        wave = ordered[cursor : cursor + 128]
        cursor += len(wave)
        outcome = asyncio.run(
            scan_tier3_tcp_candidates(
                wave,
                testing,
                logger,
                concurrency=min(64, max(1, len(wave))),
            )
        )
        responsive.extend(
            node
            for node in outcome.nodes
            if node.status in {NodeStatus.ALIVE, NodeStatus.SLOW}
        )
    sample = _root_family_drr(responsive)[:sample_size]
    if not sample:
        payload = {
            "generated_at": utc_now_iso(),
            "validation_contract_hash": TIER3_VALIDATION_CONTRACT_HASH,
            "verdict": "no_tcp_responsive_candidates",
            "sample_size": 0,
            "collected_count": len(nodes),
            "supported_count": len(supported),
        }
        _atomic_write_json(artifact_path, payload)
        return 1, payload

    audit_testing = dict(testing)
    audit_helper = audit_testing.get("mihomo_helper")
    audit_helper = dict(audit_helper) if isinstance(audit_helper, dict) else {}
    audit_helper["binary_path"] = str(mihomo_binary_path)
    audit_helper["probe_timeout"] = 5.0
    audit_testing["mihomo_helper"] = audit_helper
    audit_testing["mihomo_binary_path"] = str(mihomo_binary_path)
    audit_testing["request_timeout"] = 5.0
    validator = process_validator_settings(audit_testing)
    endpoints = tuple(str(item) for item in validator["probe_urls"] if str(item))
    results: queue.Queue[EndpointAttemptResult] = queue.Queue()
    supervisor = _build_parity_supervisor(
        mihomo_binary_path=mihomo_binary_path,
        endpoints=endpoints,
        validator=validator,
        testing=testing,
        results=results,
    )
    records: list[dict[str, Any]] = []
    supervisor.start()
    try:
        _wait_for_helpers(supervisor, 1)
        for index, node in enumerate(sample, start=1):
            endpoint = endpoints[(index - 1) % len(endpoints)]

            def helper_attempt() -> tuple[str, int]:
                helper_started = time.monotonic()
                result = _run_helper_attempt(
                    supervisor,
                    results,
                    node,
                    endpoint,
                    index,
                )
                return result.kind.value, int(
                    (time.monotonic() - helper_started) * 1000
                )

            def oracle_attempt() -> tuple[str, int]:
                started = time.monotonic()
                oracle = _mihomo_direct_validate(
                    node,
                    audit_testing,
                    int(audit_testing.get("slow_threshold_ms") or 3000),
                    logger,
                    _probe_urls_override=[endpoint],
                )
                return _classification_from_oracle(oracle), int(
                    (time.monotonic() - started) * 1000
                )

            if index % 2:
                helper_kind, helper_ms = helper_attempt()
                oracle_kind, oracle_ms = oracle_attempt()
            else:
                oracle_kind, oracle_ms = oracle_attempt()
                helper_kind, helper_ms = helper_attempt()
            helper_raw_kind = helper_kind
            helper_kind = _classification_from_helper(helper_kind)
            root, family = _root_family(node)
            records.append(
                {
                    "candidate_hash": _candidate_digest(node)[:16],
                    "root_hash": hashlib.sha256(root.encode("utf-8")).hexdigest()[:16],
                    "family": family,
                    "protocol": node.protocol,
                    "endpoint_hash": hashlib.sha256(endpoint.encode("utf-8")).hexdigest()[:16],
                    "endpoint_scheme": urlsplit(endpoint).scheme,
                    "helper": helper_kind,
                    "helper_raw": helper_raw_kind,
                    "oracle": oracle_kind,
                    "helper_ms": helper_ms,
                    "oracle_ms": oracle_ms,
                    "equivalent": helper_kind == oracle_kind,
                }
            )
    finally:
        stop_status = supervisor.stop(timeout_seconds=4.0)

    helper_pass = sum(item["helper"] == "pass" for item in records)
    oracle_pass = sum(item["oracle"] == "pass" for item in records)
    exact_matches = sum(bool(item["equivalent"]) for item in records)
    if oracle_pass >= 3 and helper_pass == 0:
        verdict = "helper_oracle_mismatch"
        exit_code = 2
    elif oracle_pass == 0 and helper_pass == 0:
        verdict = "candidate_real_probe_failed"
        exit_code = 0
    elif exact_matches == len(records):
        verdict = "equivalent"
        exit_code = 0
    else:
        verdict = "mixed_classification_drift"
        exit_code = 0
    collection_summary = collection_report.to_dict()
    payload = {
        "generated_at": utc_now_iso(),
        "validation_contract_hash": TIER3_VALIDATION_CONTRACT_HASH,
        "verdict": verdict,
        "sample_size": len(records),
        "configured_source_count": len(entries),
        "collected_count": len(nodes),
        "supported_count": len(supported),
        "tcp_responsive_count": len(responsive),
        "helper_pass_count": helper_pass,
        "oracle_pass_count": oracle_pass,
        "exact_match_count": exact_matches,
        "stop_status": stop_status,
        "collection": {
            "fetched_sources": int(getattr(collection_report, "fetched_sources", 0)),
            "failed_sources": int(getattr(collection_report, "failed_sources", 0)),
            "pending_sources": int(
                collection_summary.get("source_fetch_pending", 0) or 0
            ),
        },
        "records": records,
    }
    _atomic_write_json(artifact_path, payload)
    if stop_status != "STOPPED":
        return 1, payload
    return exit_code, payload
