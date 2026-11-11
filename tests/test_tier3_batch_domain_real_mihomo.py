"""The batch backend end to end: real supervisor, real batch helper, real Mihomo.

Eight local candidates run through ``Tier3ValidationDomain`` with
``slots_per_process=4`` (one helper process, one Mihomo, four listeners).  Each
live candidate is an identity sink that answers 204 only to its own
credential, so a PASS can only have been carried by that candidate's own
listener; dead candidates are bound-but-never-listening ports.  Nothing here
touches the public network: DNS points at a local resolver that answers
nothing and every destination is under ``.invalid``.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

import tier3_validation_config
from test_support.tier3_batch_attribution_fixtures import BatchFixture, batch_dns_block
from tier3_batch_validator_supervisor import Tier3BatchValidatorSupervisor
from tier3_validation_domain import Tier3ValidationDomain
from tier3_validation_protocol import AdmissionCommitted, Tier3TerminalKind

ROOT = Path(__file__).resolve().parents[1]
MIHOMO = ROOT / "bin" / "mihomo.exe"

pytestmark = pytest.mark.skipif(not MIHOMO.exists(), reason="bundled mihomo binary required")


def _wait_until(predicate, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition did not become true")


def test_batch_backend_attributes_every_pass_to_its_own_candidate(monkeypatch, tmp_path) -> None:
    with BatchFixture() as fixture:
        dns_port = fixture.start_nxdomain_dns()
        monkeypatch.setattr(
            tier3_validation_config,
            "helper_dns_block",
            lambda ipv6=False: batch_dns_block(dns_port),
        )
        candidates = fixture.build_candidates(8, dead=frozenset({6, 7}))
        live = [candidate for candidate in candidates if candidate.sink is not None]
        dead = [candidate for candidate in candidates if candidate.sink is None]
        # One shared probe URL whose marker names every candidate, so the sink's
        # credential check alone decides attribution.
        probe_url = (
            "http://" + "-".join(candidate.identity for candidate in candidates)
            + ".probe.invalid/generate_204"
        )
        lock = threading.Lock()
        commits: list[str] = []
        terminals: dict[str, Tier3TerminalKind] = {}
        finished = threading.Event()

        def commit_node(node, _report, intent):
            with lock:
                commits.append(node.name)
            return AdmissionCommitted(
                refresh_epoch=intent.refresh_epoch,
                candidate_digest=intent.candidate_digest,
                lease_id=intent.lease_id,
                sequence=intent.sequence,
                pool_revision=len(commits),
                net_new_pool_growth=True,
            )

        def observe(node, terminal):
            with lock:
                terminals[node.name] = terminal.kind
                if len(terminals) >= len(candidates):
                    finished.set()

        domain = Tier3ValidationDomain(
            refresh_epoch="batch-real-mihomo",
            binary_path=str(MIHOMO),
            probe_urls=[probe_url],
            expected_statuses={204},
            probe_timeout_seconds=3.0,
            slow_threshold_ms=3000,
            commit_node=commit_node,
            terminal_observer=observe,
            min_workers=4,
            max_workers=4,
            temp_parent=str(tmp_path),
            slots_per_process=4,
        )
        try:
            assert isinstance(domain.supervisor, Tier3BatchValidatorSupervisor)
            assert domain.start() is True
            _wait_until(lambda: domain.snapshot().supervisor.active_workers == 4, timeout=40.0)
            helper_pids = {int(item["helper_pid"]) for item in domain.supervisor.snapshot().workers}
            assert len(helper_pids) == 1, "four identities must share one helper process"
            for candidate in candidates:
                assert domain.submit(candidate.node, {"configured_source_count": 1})
            assert finished.wait(60.0), {
                "terminals": terminals,
                "commits": commits,
                "metrics": domain.supervisor.batch_metrics(),
                "actor": domain.snapshot().actor,
            }
            for candidate in live:
                assert terminals[candidate.identity] is Tier3TerminalKind.PASS, terminals
                records = candidate.records
                assert records, f"{candidate.identity} was never probed"
                assert all(record.credential_identity == candidate.identity for record in records), records
                assert all(record.verdict == 204 for record in records), records
            for candidate in dead:
                assert terminals[candidate.identity] is not Tier3TerminalKind.PASS, terminals
            assert sorted(commits) == sorted(candidate.identity for candidate in live)
            assert fixture.dns_queries == [], "no destination may be resolved locally"
            metrics = domain.supervisor.batch_metrics()
            assert metrics["groups"] == 1, metrics
            assert metrics["dispatches"] >= len(candidates)
            assert metrics["group_retirements"] == 0, metrics
        finally:
            assert domain.stop(timeout_seconds=6.0) == "STOPPED"


def _poison_node(index: int):
    """Renderable by the formatter but rejected by Mihomo (non-hex REALITY sid)."""

    from parser import parse_node_uri

    node = parse_node_uri(
        "vless://11111111-1111-4111-8111-11111111111"
        f"{index}@127.0.0.1:443?encryption=none&security=reality"
        "&sni=example.com&fp=chrome"
        "&pbk=aGVsbG93b3JsZGhlbGxvd29ybGRoZWxsb3dvcmxkMDA"
        f"&sid=ZZZ{index}&type=tcp#poison-{index}",
        source="batch-attribution", tier="tier3",
    )
    assert node is not None
    node.extra.update({"tier3_source_root_id": "batch-root", "tier3_source_unit_family": "vless"})
    return node


def _run_domain(monkeypatch, tmp_path, nodes, *, slots, workers, probe_url, dns_port, timeout=90.0):
    monkeypatch.setattr(
        tier3_validation_config, "helper_dns_block", lambda ipv6=False: batch_dns_block(dns_port),
    )
    lock = threading.Lock()
    commits: list[str] = []
    terminals: dict[str, Tier3TerminalKind] = {}
    finished = threading.Event()

    def commit_node(node, _report, intent):
        with lock:
            commits.append(node.name)
        return AdmissionCommitted(
            refresh_epoch=intent.refresh_epoch, candidate_digest=intent.candidate_digest,
            lease_id=intent.lease_id, sequence=intent.sequence,
            pool_revision=len(commits), net_new_pool_growth=True,
        )

    def observe(node, terminal):
        with lock:
            terminals[node.name] = terminal.kind
            if len(terminals) >= len(nodes):
                finished.set()

    domain = Tier3ValidationDomain(
        refresh_epoch=f"batch-real-{slots}x{workers}",
        binary_path=str(MIHOMO), probe_urls=[probe_url], expected_statuses={204},
        probe_timeout_seconds=3.0, slow_threshold_ms=3000,
        commit_node=commit_node, terminal_observer=observe,
        min_workers=workers, max_workers=workers, temp_parent=str(tmp_path),
        slots_per_process=slots,
    )
    try:
        assert domain.start() is True
        _wait_until(lambda: domain.snapshot().supervisor.active_workers == workers, timeout=40.0)
        for node in nodes:
            assert domain.submit(node, {"configured_source_count": 1})
        assert finished.wait(timeout), {"terminals": terminals, "metrics": domain.supervisor.batch_metrics()}
        return terminals, commits, domain.supervisor.batch_metrics()
    finally:
        assert domain.stop(timeout_seconds=6.0) == "STOPPED"


def test_a_poison_pill_in_a_coalesced_reload_fails_alone(monkeypatch, tmp_path) -> None:
    """Concurrent dispatch to one shared Mihomo: the Mihomo-rejected candidate
    gets a facility terminal, its three live siblings pass through their own
    listeners.  Attribution never consults Mihomo's error text: the slot's own
    provider update answers non-2xx and nothing else is touched (the name dates
    from the whole-config hot-install era, when a rejected coalesced reload was
    retried with only this slot as the new entry)."""

    with BatchFixture() as fixture:
        dns_port = fixture.start_nxdomain_dns()
        live = fixture.build_candidates(3)
        poison = _poison_node(0)
        probe_url = "http://" + "-".join(c.identity for c in live) + ".probe.invalid/generate_204"
        nodes = [live[0].node, poison, live[1].node, live[2].node]
        terminals, commits, metrics = _run_domain(
            monkeypatch, tmp_path, nodes, slots=4, workers=4, probe_url=probe_url, dns_port=dns_port,
        )
        assert terminals[poison.name] is Tier3TerminalKind.FACILITY_FAIL, terminals
        for candidate in live:
            assert terminals[candidate.identity] is Tier3TerminalKind.PASS, terminals
            assert candidate.records and all(
                record.credential_identity == candidate.identity and record.verdict == 204
                for record in candidate.records
            ), candidate.records
        assert sorted(commits) == sorted(c.identity for c in live)
        assert metrics["group_retirements"] == 0 and metrics["slot_quarantines"] == 0, metrics
        assert fixture.dns_queries == []


def test_sixteen_concurrent_leases_on_two_shared_mihomos_pass_without_spurious_rejections(
    monkeypatch, tmp_path,
) -> None:
    """K=8: sixteen leases hit two Mihomos at once.  Under the lock-scoped,
    coalescing install no reload can corrupt another's file, so every live
    candidate passes and nothing is charged a facility failure."""

    with BatchFixture() as fixture:
        dns_port = fixture.start_nxdomain_dns()
        live = fixture.build_candidates(16)
        probe_url = "http://" + "-".join(c.identity for c in live) + ".probe.invalid/generate_204"
        terminals, commits, metrics = _run_domain(
            monkeypatch, tmp_path, [c.node for c in live], slots=8, workers=16,
            probe_url=probe_url, dns_port=dns_port, timeout=120.0,
        )
        assert all(kind is Tier3TerminalKind.PASS for kind in terminals.values()), terminals
        assert len(commits) == 16
        for candidate in live:
            assert candidate.records and all(
                record.credential_identity == candidate.identity for record in candidate.records
            ), candidate.records
        assert metrics["groups"] == 2
        assert metrics["group_retirements"] == 0 and metrics["slot_quarantines"] == 0, metrics
        assert fixture.dns_queries == []
