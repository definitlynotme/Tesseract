"""A shared-Mihomo batch must never confuse one candidate with another.

The batch builder gives every candidate its own listener bound to its own proxy.
Connecting to those ports only proves they listen; these tests prove the part
that matters for admission: a probe egresses through its own candidate, a dead
candidate cannot borrow a live sibling's verdict, and nothing rides the unbound
mixed port. Every endpoint here is a local sink, so no assertion depends on the
public network.
"""
from __future__ import annotations

import http.client
import json
import random
import time
from pathlib import Path

import pytest
import yaml

from test_support.tier3_batch_attribution_fixtures import (
    MIHOMO,
    BatchFixture,
    batch_dns_block,
    proxy_get,
    start_batch_mihomo,
)
from tier3_validation_config import build_batch_proxy_validation_config

pytestmark = pytest.mark.skipif(not MIHOMO.exists(), reason="bundled mihomo required")


def _batch(fixture: BatchFixture, candidates, tmp_path: Path, *, name: str):
    mixed_port = fixture.reserve()
    controller_port = fixture.reserve()
    dns_port = fixture.start_nxdomain_dns()
    config, names = build_batch_proxy_validation_config(
        [(candidate.node, candidate.listener_port) for candidate in candidates],
        mixed_port,
        controller_port=controller_port,
    )
    config["dns"] = batch_dns_block(dns_port)
    process = start_batch_mihomo(
        config,
        tmp_path / name,
        [candidate.listener_port for candidate in candidates] + [controller_port],
    )
    return process, names, mixed_port, controller_port


def test_batch_listener_forwards_candidate_credentials_without_resolving_anything(tmp_path):
    """Preflight: if this fails, every other attribution assertion is unsound.

    If Mihomo resolved the destination itself it would get no records from the
    local resolver and the probe would fail; assertion 6 then says so directly.
    """

    with BatchFixture() as fixture:
        candidates = fixture.build_candidates(1)
        process, names, _mixed, _controller = _batch(
            fixture, candidates, tmp_path, name="preflight",
        )
        try:
            status, body = proxy_get(candidates[0].listener_port, candidates[0].probe_url)
        finally:
            process.kill()
        assert names == ("TEST-PROXY-0",)
        assert status == 204, f"batch probe did not reach its sink: {status}"
        assert body == b""
        assert len(candidates[0].records) == 1
        record = candidates[0].records[0]
        assert record.credential_identity == "cand00", (
            "the candidate's own credential did not reach the upstream proxy"
        )
        assert "cand00" in record.target_identity, "the destination did not survive the hop"
        assert [q for q in fixture.dns_queries if q.endswith("probe.invalid")] == [], (
            "the shared process resolved the destination instead of forwarding it"
        )


def test_each_listener_egresses_through_its_own_candidate_and_no_sibling(tmp_path):
    with BatchFixture() as fixture:
        candidates = fixture.build_candidates(8)
        process, _names, _mixed, _controller = _batch(
            fixture, candidates, tmp_path, name="own-candidate",
        )
        try:
            order = list(range(8))
            random.Random(20260920).shuffle(order)
            results = {}
            for index in order:
                status, _body = proxy_get(
                    candidates[index].listener_port, candidates[index].probe_url,
                )
                results[index] = status
            # Snapshot before the control, which deliberately adds one record.
            counts = [len(c.records) for c in candidates]
            own = [
                [
                    (r.credential_identity, candidate.identity in r.target_identity)
                    for r in candidate.records
                ]
                for candidate in candidates
            ]
            # Negative control: candidate 0's listener with candidate 3's target.
            cross_status, _body = proxy_get(
                candidates[0].listener_port, candidates[3].probe_url,
            )
        finally:
            process.kill()
        assert results == {index: 204 for index in range(8)}
        assert counts == [1] * 8, "a candidate saw traffic that was not its own probe"
        assert sum(counts) == 8, "eight probes did not produce eight egresses"
        for index, candidate in enumerate(candidates):
            assert own[index] == [(candidate.identity, True)], (
                f"candidate {index} was not the one that carried its own probe: {own[index]}"
            )
        assert cross_status == 409, (
            "a sink answered for a destination that was not its own candidate"
        )
        assert candidates[0].records[-1].verdict == 409
        assert len(candidates[3].records) == 1, "the cross probe reached the wrong sink"


def test_the_unbound_mixed_port_carries_nothing_while_the_batch_is_live(tmp_path):
    with BatchFixture() as fixture:
        candidates = fixture.build_candidates(8)
        process, _names, mixed_port, _controller = _batch(
            fixture, candidates, tmp_path, name="unbound-mixed",
        )
        try:
            try:
                status, _body = proxy_get(mixed_port, candidates[0].probe_url)
            except (OSError, http.client.HTTPException):
                status = 0
            leaked = sum(len(c.records) for c in candidates)
            healthy, _body = proxy_get(
                candidates[0].listener_port, candidates[0].probe_url,
            )
        finally:
            process.kill()
        assert status not in (200, 204), "the unbound mixed port served a request"
        assert leaked == 0, "a candidate was dialed on behalf of the unbound mixed port"
        assert healthy == 204, "the batch was not healthy, so the rejection proves nothing"


def test_reusing_listener_ports_across_generations_never_answers_for_the_old_candidate(tmp_path):
    """Port reuse must not let a later generation inherit an earlier verdict."""

    with BatchFixture() as fixture:
        old = fixture.build_candidates(4)
        ports = [candidate.listener_port for candidate in old]
        mixed_port = fixture.reserve()
        controller_port = fixture.reserve()
        dns_port = fixture.start_nxdomain_dns()
        first, _names = build_batch_proxy_validation_config(
            [(candidate.node, candidate.listener_port) for candidate in old],
            mixed_port,
            controller_port=controller_port,
        )
        first["dns"] = batch_dns_block(dns_port)
        work = tmp_path / "reload"
        process = start_batch_mihomo(first, work, ports + [controller_port])
        try:
            for candidate in old:
                status, _body = proxy_get(candidate.listener_port, candidate.probe_url)
                assert status == 204
            before = [len(candidate.records) for candidate in old]

            # The next generation reuses the same listener ports.
            new = fixture.build_candidates(4, first_index=4, listener_ports=ports)
            second, _names = build_batch_proxy_validation_config(
                [(candidate.node, candidate.listener_port) for candidate in new],
                mixed_port,
                controller_port=controller_port,
            )
            second["dns"] = batch_dns_block(dns_port)
            path = work / "config-generation-2.yaml"
            path.write_text(
                yaml.safe_dump(second, allow_unicode=True, sort_keys=False), encoding="utf-8",
            )
            connection = http.client.HTTPConnection("127.0.0.1", controller_port, timeout=10)
            try:
                connection.request(
                    "PUT",
                    "/configs?force=true",
                    body=json.dumps({"path": str(path), "payload": ""}),
                    headers={"Content-Type": "application/json"},
                )
                reload_status = connection.getresponse().status
            finally:
                connection.close()
            assert reload_status < 300, f"controller refused the new generation: {reload_status}"

            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline:
                status, _body = proxy_get(new[0].listener_port, new[0].probe_url)
                if status == 204:
                    break
                time.sleep(0.1)
            for candidate in new[1:]:
                status, _body = proxy_get(candidate.listener_port, candidate.probe_url)
                assert status == 204, "a reused port did not serve its new candidate"
            stale_status, _body = proxy_get(new[0].listener_port, old[0].probe_url)
        finally:
            process.kill()
        assert [len(candidate.records) for candidate in old] == before, (
            "a request after the reload reached a previous-generation candidate"
        )
        assert all(len(candidate.records) >= 1 for candidate in new)
        for candidate in new:
            assert all(
                record.credential_identity == candidate.identity
                for record in candidate.records
            )
        assert stale_status == 409, (
            "the reused port still answered for the retired candidate's destination"
        )


def test_a_dead_candidate_cannot_borrow_a_live_siblings_verdict(tmp_path):
    dead = frozenset({1, 3, 6})
    live = [index for index in range(8) if index not in dead]
    with BatchFixture() as fixture:
        candidates = fixture.build_candidates(8, dead=dead)
        process, _names, _mixed, _controller = _batch(
            fixture, candidates, tmp_path, name="dead-sibling",
        )
        try:
            rounds = []
            for round_index in range(2):
                order = list(range(8))
                random.Random(20260920 + round_index).shuffle(order)
                results = {}
                for index in order:
                    try:
                        status, _body = proxy_get(
                            candidates[index].listener_port, candidates[index].probe_url,
                        )
                    except (OSError, http.client.HTTPException):
                        status = 0
                    results[index] = status
                rounds.append(results)
        finally:
            process.kill()
        for round_index, results in enumerate(rounds):
            assert [results[index] for index in live] == [204] * len(live), (
                f"a live candidate failed in round {round_index}: {results}"
            )
            assert all(results[index] not in (200, 204) for index in dead), (
                f"a dead candidate passed in round {round_index}: {results}"
            )
        assert [len(candidates[index].records) for index in live] == [2] * len(live), (
            "a live sink saw traffic that was not its own two probes"
        )
        for index in live:
            candidate = candidates[index]
            assert all(r.credential_identity == candidate.identity for r in candidate.records), (
                "a live sink saw another candidate's credential, i.e. a borrowed dial"
            )
            assert all(r.verdict == 204 for r in candidate.records)
