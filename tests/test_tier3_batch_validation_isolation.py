"""One candidate's fault must not reach its siblings in a shared Mihomo.

Today's helper owns a single probe session, and the ordinary budget-overrun path
closes it (tier3_validator_runtime.py:700 -> :558 -> :519), so a shared process
would abort every sibling's live socket and then report the damage as the
siblings' own transport evidence. The prototype gives each slot its own session,
task and listener port; these tests are what makes that claim checkable.

Everything is local: each candidate is its own identity sink, so "did a sibling
keep working" is answered by that sibling's own egress record rather than by
timing.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from parser import parse_node_uri
from tier_cache import NON_PERSISTENT_NEGATIVE_FAILURE_STAGES
from test_support.tier3_batch_attribution_fixtures import MIHOMO, BatchFixture
from test_support.tier3_slot_parity_probe import Helper, command
from tier3_validation_domain import tier3_candidate_config_digest
from tier3_validation_protocol import parse_message

pytestmark = pytest.mark.skipif(not MIHOMO.exists(), reason="bundled mihomo required")

# tier3_validation_domain.node_from_terminal:534 maps a FACILITY_FAIL terminal
# to this stage; tier_cache treats it as non-persistent.
FACILITY_STAGE = "validator_facility_failure"


def _await_kind(helper: Helper, kind: str, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if helper.parent.poll(0.05):
            received, payload = parse_message(helper.parent.recv_bytes())
            if received == kind:
                return payload
    raise AssertionError(f"helper never sent {kind} within {timeout}s")


def _collect_results(helper: Helper, expected: int, timeout: float) -> dict[str, dict]:
    results: dict[str, dict] = {}
    deadline = time.monotonic() + timeout
    while len(results) < expected and time.monotonic() < deadline:
        if helper.parent.poll(0.05):
            kind, payload = parse_message(helper.parent.recv_bytes())
            if kind == "endpoint_result":
                results[str(payload.get("candidate_digest", ""))] = payload
    return results


def _collect_results_with_seals(
    helper: Helper, expected: int, timeout: float,
) -> tuple[dict[str, dict], dict[str, int]]:
    """Collect results and count how many times each lease was sealed."""

    results: dict[str, dict] = {}
    seals: dict[str, int] = {}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if helper.parent.poll(0.05):
            kind, payload = parse_message(helper.parent.recv_bytes())
            if kind != "endpoint_result":
                continue
            digest = str(payload.get("candidate_digest", ""))
            results[digest] = payload
            seals[digest] = seals.get(digest, 0) + 1
        elif len(results) >= expected:
            # Keep draining briefly so a duplicate seal would still be seen.
            break
    return results, seals


def _load_batch(helper: Helper, candidates) -> dict:
    helper.send("load_batch_generation", {
        "nodes": [candidate.node.to_dict() for candidate in candidates],
        "candidate_digests": [
            tier3_candidate_config_digest(candidate.node) for candidate in candidates
        ],
        "listener_ports": helper.listener_ports[:len(candidates)],
        "facility_budget_seconds": 20.0,
    })
    return _await_kind(helper, "batch_generation_ready", 60.0)


def _start(fixture: BatchFixture, candidates, tmp_path: Path, probe_url: str) -> Helper:
    helper = Helper(1, len(candidates), True, set(), tmp_path, [probe_url], 3.0)
    ready = _await_kind(helper, "ready", 120.0)
    helper.mihomo_pid = int(ready.get("mihomo_pid") or 0)
    return helper


def test_one_slot_exhausting_its_budget_does_not_disturb_its_siblings(tmp_path):
    """The ordinary overrun path closes a session; it must be the slot's own."""

    with BatchFixture() as fixture:
        # Candidate 0 answers far too late for its budget; the rest answer at once.
        # Siblings answer only after slot 0 has already overrun, so the
        # overrun provably happens while they are still mid-probe.
        candidates = fixture.build_candidates(
            4, slow={0: 3.0, 1: 1.5, 2: 1.5, 3: 1.5},
        )
        helper = _start(fixture, candidates, tmp_path, candidates[0].probe_url)
        try:
            ready = _load_batch(helper, candidates)
            assert ready["loaded"] is True, ready
            digests = [tier3_candidate_config_digest(c.node) for c in candidates]
            for index, candidate in enumerate(candidates):
                budget = 0.5 if index == 0 else 3.0
                helper.send("validate_endpoint", command(
                    candidate.node, index + 1, helper.worker_id, budget, candidate.probe_url,
                ))
            results = _collect_results(helper, 4, 60.0)
        finally:
            helper.close()

        assert set(results) == set(digests), "a lease produced no result at all"
        assert results[digests[0]]["kind"] == "right_censored", (
            f"the slow candidate should be censored: {results[digests[0]]['kind']}"
        )
        for index in (1, 2, 3):
            assert results[digests[index]]["kind"] == "pass", (
                f"sibling {index} was disturbed by slot 0's overrun: "
                f"{results[digests[index]]['kind']} {results[digests[index]].get('reason')}"
            )
            assert len(candidates[index].records) == 1
            assert candidates[index].records[0].verdict == 204


def test_cancelling_one_lease_leaves_its_siblings_probing(tmp_path):
    with BatchFixture() as fixture:
        candidates = fixture.build_candidates(4, slow={0: 3.0, 1: 1.0, 2: 1.0, 3: 1.0})
        helper = _start(fixture, candidates, tmp_path, candidates[0].probe_url)
        try:
            assert _load_batch(helper, candidates)["loaded"] is True
            digests = [tier3_candidate_config_digest(c.node) for c in candidates]
            for index, candidate in enumerate(candidates):
                helper.send("validate_endpoint", command(
                    candidate.node, index + 1, helper.worker_id, 4.0, candidate.probe_url,
                ))
            time.sleep(0.3)  # let every slot reach its probe
            helper.send("cancel", {
                "refresh_epoch": "slot-parity",
                "lease_id": f"lease-{helper.worker_id}-1",
                "worker_id": helper.worker_id,
                "worker_generation": 1,
                "sequence": 1,
                "reason": "isolation_test",
            })
            clean = _await_kind(helper, "clean_confirmed", 30.0)
            results = _collect_results(helper, 3, 60.0)
        finally:
            helper.close()

        assert clean["slot_index"] == 0, clean
        assert clean["poisoned"] is False, "a routine cancel quarantined the slot"
        for index in (1, 2, 3):
            payload = results.get(digests[index])
            assert payload is not None, f"sibling {index} lost its result to a cancel"
            assert payload["kind"] == "pass", (
                f"sibling {index} was damaged by the cancel: {payload['kind']}"
            )
        assert digests[0] not in results, "a cancelled lease still produced a verdict"


def test_a_generation_that_failed_to_load_refuses_to_probe(tmp_path):
    """A failed load must not be probed through the previous generation's ports.

    The listener ports are reused across generations, so if the new config never
    installs, the old one is still bound to them. Probing then would credit the
    OLD candidate's answer to the NEW candidate: a false pass. The gate must
    answer facility failure instead.
    """

    with BatchFixture() as fixture:
        first = fixture.build_candidates(2)
        # Production gives every candidate the same probe URL, so a misrouted
        # probe reaching a live previous-generation node answers 204 and the new
        # candidate is credited with it. Model exactly that.
        shared_probe_url = first[0].probe_url
        helper = _start(fixture, first, tmp_path, shared_probe_url)
        try:
            assert _load_batch(helper, first)["loaded"] is True
            before = [len(candidate.records) for candidate in first]

            # A second generation on the SAME ports, renderable by the formatter
            # but rejected by Mihomo, so the previous config keeps serving them.
            poison = [
                parse_node_uri(
                    "vless://11111111-1111-4111-8111-11111111111"
                    f"{index}@127.0.0.1:443?encryption=none&security=reality"
                    "&sni=example.com&fp=chrome"
                    "&pbk=aGVsbG93b3JsZGhlbGxvd29ybGRoZWxsb3dvcmxkMDA"
                    f"&sid=ZZZ{index}&type=tcp#poison-{index}",
                    source="isolation", tier="tier3",
                )
                for index in range(2)
            ]
            assert all(node is not None for node in poison)
            helper.send("load_batch_generation", {
                "nodes": [node.to_dict() for node in poison],
                "candidate_digests": [
                    tier3_candidate_config_digest(node) for node in poison
                ],
                "listener_ports": helper.listener_ports[:2],
                "facility_budget_seconds": 20.0,
            })
            ready = _await_kind(helper, "batch_generation_ready", 60.0)
            assert ready["loaded"] is False, ready
            assert ready["unrenderable_indexes"] == [], (
                "the poison candidates must be renderable, or the test proves nothing"
            )

            digests = [tier3_candidate_config_digest(node) for node in poison]
            for index, node in enumerate(poison):
                helper.send("validate_endpoint", command(
                    node, 90 + index, helper.worker_id, 3.0, shared_probe_url,
                ))
            results = _collect_results(helper, 2, 30.0)
        finally:
            helper.close()

        assert set(results) == set(digests)
        for index, digest in enumerate(digests):
            assert results[digest]["kind"] != "pass", (
                f"candidate {index} was credited with a previous generation's answer"
            )
        assert [len(candidate.records) for candidate in first] == before, (
            "a new generation's probe reached a previous generation's candidate"
        )


def test_losing_the_shared_mihomo_answers_every_live_lease(tmp_path):
    """A shared facility fault must not silently swallow the leases it carried."""

    with BatchFixture() as fixture:
        candidates = fixture.build_candidates(4, slow={0: 4.0, 1: 4.0, 2: 4.0, 3: 4.0})
        helper = _start(fixture, candidates, tmp_path, candidates[0].probe_url)
        mihomo_pid = 0
        try:
            ready_payload = None
            assert _load_batch(helper, candidates)["loaded"] is True
            digests = [tier3_candidate_config_digest(c.node) for c in candidates]
            for index, candidate in enumerate(candidates):
                helper.send("validate_endpoint", command(
                    candidate.node, index + 1, helper.worker_id, 5.0, candidate.probe_url,
                ))
            # Wait until every slot has provably started its probe (each sink
            # records the request on arrival) but none can have answered yet:
            # the sinks hold their 204 for 4s, so a pass here would be a bug.
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline and sum(
                len(candidate.records) for candidate in candidates
            ) < 4:
                time.sleep(0.02)
            assert sum(len(candidate.records) for candidate in candidates) == 4, (
                "not every slot reached its sink before the kill"
            )
            import subprocess

            from windows_subprocess import hidden_subprocess_kwargs

            mihomo_pid = int(helper.mihomo_pid)
            subprocess.run(
                ["taskkill.exe", "/PID", str(mihomo_pid), "/F"],
                capture_output=True, **hidden_subprocess_kwargs(),
            )
            results, seals = _collect_results_with_seals(helper, 4, 60.0)
        finally:
            helper.close()

        assert mihomo_pid > 0
        assert set(results) == set(digests), (
            "losing the shared Mihomo lost leases silently: "
            f"{len(results)} of 4 answered"
        )
        assert len(seals) == 4 and all(count == 1 for count in seals.values()), (
            f"a lease was sealed more than once: {seals}"
        )
        for index, digest in enumerate(digests):
            kind = results[digest]["kind"]
            # A shared Mihomo kill mid-probe surfaces either as a connection
            # fault (facility_fail) or, if the reset loses the race with the
            # probe budget, as right_censored/transient.  Both are
            # non-persistent and never charge the candidate; only a pass or a
            # candidate_fail would wrongly blame the node for the facility loss.
            assert kind in {"facility_fail", "right_censored", "transient"}, (
                f"candidate {index} was charged for a shared facility fault: "
                f"{kind} {results[digest].get('reason')}"
            )
        # A shared facility fault must not become durable negative knowledge
        # about the candidates it happened to be carrying.
        assert FACILITY_STAGE in NON_PERSISTENT_NEGATIVE_FAILURE_STAGES, (
            "the stage a facility failure maps to would write a negative record"
        )


def test_an_unrenderable_candidate_fails_alone_inside_a_live_batch(tmp_path):
    with BatchFixture() as fixture:
        candidates = fixture.build_candidates(4)
        candidates[2].node.protocol = "not-a-protocol"
        helper = _start(fixture, candidates, tmp_path, candidates[0].probe_url)
        try:
            ready = _load_batch(helper, candidates)
            digests = [tier3_candidate_config_digest(c.node) for c in candidates]
            for index, candidate in enumerate(candidates):
                helper.send("validate_endpoint", command(
                    candidate.node, index + 1, helper.worker_id, 3.0, candidate.probe_url,
                ))
            results = _collect_results(helper, 4, 60.0)
        finally:
            helper.close()

        assert ready["unrenderable_indexes"] == [2], ready
        assert results[digests[2]]["kind"] == "candidate_fail"
        assert results[digests[2]]["reason"] == "unsupported_proxy"
        for index in (0, 1, 3):
            assert results[digests[index]]["kind"] == "pass", (
                f"sibling {index} was charged for candidate 2's render failure"
            )
