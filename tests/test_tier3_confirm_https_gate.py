"""Mainstream HTTPS confirm gate of Tier3 validation.

A node is admitted only if, after its scheduled portfolio endpoint PASSED, it
also loads every configured confirm URL (default Google ``generate_204``) over
verified HTTPS.  Pinned here:

* runtime: pass / completed failure / timeout of the confirm step, measured
  through a real loopback proxy (single-slot helper and a batch slot);
* supervisor: a portfolio fact plus trailing confirm facts is a valid result,
  and START carries the setting to both backends;
* actor: ledgers keep the portfolio endpoint, a confirm verdict finalises the
  transaction at once, and the lease hard cap covers the confirm budget;
* protocol: the validation contract is bumped so cached PASS records re-probe;
* Tier3B rechecks (legacy tester path): a recheck PASS is held to the same
  gate through one real Mihomo, so a failing node is not kept or "recovered".
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import queue
import socket
import socketserver
import threading
import time
import types
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest

import manager as manager_module
from models import Node, NodeStatus
from runtime_health_check import health_check_verdict
from tester import confirm_tier3_nodes_mainstream_https
from tests.test_runtime_health_check_commit import (
    confirm as confirm_incident,
    node as runtime_node,
    tier3_manager,
)
from tier3_batch_validator_runtime import BatchValidatorHelperRuntime, _BatchProbeSlot
from tier3_batch_validator_supervisor import (
    Tier3BatchValidatorSupervisor,
    _BatchWorkerGeneration,
    _HelperGroup,
)
from tier3_endpoint_controller import Tier3EndpointController
from tier3_expansion_actor import Tier3ExpansionActor
from tier3_validation_domain import (
    DEFAULT_PROCESS_CONFIRM_URLS,
    Tier3ValidationDomain,
    node_from_terminal,
    process_validator_settings,
)
from tier3_validation_protocol import (
    CONFIRM_HTTPS_CENSORED_REASON,
    CONFIRM_HTTPS_FAILED_REASON,
    CONFIRM_HTTPS_STAGE,
    TIER3_VALIDATION_CONTRACT_HASH,
    VALIDATION_CONTRACT_MATERIAL,
    CandidateEnvelope,
    EndpointAttemptKind,
    EndpointAttemptResult,
    ProbeAttemptFact,
    Tier3TerminalKind,
    Tier3WorkerPhase,
    ValidateEndpointCmd,
    ValidationStage,
    ValidationTerminal,
    canonical_json,
    confirm_https_budget_seconds,
    dataclass_payload,
)
from tier3_validator_runtime import ValidatorHelperRuntime
from tier3_validator_supervisor import Tier3ValidatorSupervisor

PRIMARY = "http://primary.invalid/primary"
CONFIRM_PASS = "http://confirm.invalid/pass"
CONFIRM_403 = "http://confirm.invalid/forbidden"
CONFIRM_200_BODY = "http://confirm.invalid/page"
CONFIRM_SLOW = "http://confirm.invalid/slow"
CONFIRM_TLS_EOF = "https://tls-eof.invalid/generate_204"
# 503 on the first request only, then an empty 204: one transient hiccup.
CONFIRM_FLAKY_503 = "http://confirm.invalid/flaky-503"
# A broken response (then a dropped connection) on the first request only.
# (A silent drop is not used: aiohttp itself re-sends an idempotent GET once.)
CONFIRM_FLAKY_RESET = "http://confirm.invalid/flaky-reset"
CONFIRM_ALWAYS_503 = "http://confirm.invalid/always-503"
# A broken response after a delay: a slow transport failure.
CONFIRM_SLOW_RESET = "http://confirm.invalid/slow-reset"


# ------------------------------------------------------------ loopback proxy


class _GateProxyHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        self.request.settimeout(10.0)
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.request.recv(4096)
            if not chunk:
                return
            data += chunk
        method, target, _version = (
            data.split(b"\r\n", 1)[0].decode("latin-1").split(" ", 2)
        )
        server: _GateProxy = self.server  # type: ignore[assignment]
        with server.lock:
            server.requests.append((method, target))
        if method == "CONNECT":
            # The tunnel opens, then the far end drops it during the TLS
            # handshake: the "SSL EOF on every real HTTPS site" node.
            self.request.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            return
        path = urlsplit(target).path
        with server.lock:
            server.hits[path] = server.hits.get(path, 0) + 1
            hit = server.hits[path]
        if path == "/slow":
            time.sleep(server.slow_seconds)
        if path == "/slow-reset":
            time.sleep(0.4)
            self.request.sendall(b"garbage\r\n\r\n")
            return
        if path == "/flaky-reset" and hit == 1:
            self.request.sendall(b"garbage\r\n\r\n")
            return
        if path == "/flaky-503":
            path = "/unavailable" if hit == 1 else "/pass"
        status, body = {
            "/primary": (204, b""),
            "/pass": (204, b""),
            "/forbidden": (403, b"blocked"),
            "/page": (200, b"<html>captive</html>"),
            "/slow": (204, b""),
            "/flaky-reset": (204, b""),
            "/unavailable": (503, b""),
            "/always-503": (503, b""),
        }.get(path, (404, b"missing"))
        self.request.sendall(
            f"HTTP/1.1 {status} X\r\nContent-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n".encode("ascii")
            + body
        )


class _GateProxy(socketserver.ThreadingTCPServer):
    """Stands in for a helper's Mihomo mixed port with one candidate behind it."""

    daemon_threads = True
    block_on_close = False

    def __init__(self, slow_seconds: float = 1.5) -> None:
        super().__init__(("127.0.0.1", 0), _GateProxyHandler)
        self.lock = threading.Lock()
        self.requests: list[tuple[str, str]] = []
        self.hits: dict[str, int] = {}
        self.slow_seconds = float(slow_seconds)
        self.port = int(self.server_address[1])
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()

    def targets(self) -> list[str]:
        with self.lock:
            return [target for _method, target in self.requests]

    def close(self) -> None:
        self.shutdown()
        self.server_close()


@pytest.fixture
def gate_proxy():
    proxy = _GateProxy()
    try:
        yield proxy
    finally:
        proxy.close()


def _free_port(excluded: set[int]) -> int:
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
        if port not in excluded:
            return port


class _Connection:
    def send_bytes(self, _payload: bytes) -> None:
        return None


def _command(
    *,
    endpoint: str = PRIMARY,
    hard_cap: float = 30.0,
    stage: ValidationStage = ValidationStage.FAST_PROBE,
) -> ValidateEndpointCmd:
    return ValidateEndpointCmd(
        refresh_epoch="confirm-gate",
        candidate_digest="a" * 64,
        config_digest="b" * 64,
        lease_id="lease-confirm",
        sequence=1,
        node_json=canonical_json(
            Node(
                name="confirm-candidate",
                protocol="http",
                address="127.0.0.1",
                port=9,
                tier="tier3",
            ).to_dict()
        ),
        source_root="root",
        source_family="http",
        stage=stage,
        endpoint=endpoint,
        probe_budget_seconds=1.0,
        candidate_hard_cap_seconds=hard_cap,
    )


def _run_helper(
    proxy: _GateProxy,
    confirm_urls: list[str] | None,
    command: ValidateEndpointCmd,
    *,
    probe_timeout: float = 1.0,
) -> EndpointAttemptResult:
    payload: dict[str, Any] = {
        "worker_id": 1,
        "worker_generation": 1,
        "binary_path": "fixture",
        "probe_urls": [PRIMARY],
        "expected_statuses": [204, 200],
        "probe_timeout_seconds": probe_timeout,
        "slow_threshold_ms": 3000,
        "mixed_port": proxy.port,
        "controller_port": _free_port({proxy.port}),
    }
    if confirm_urls is not None:
        payload["confirm_urls"] = confirm_urls
    runtime = ValidatorHelperRuntime(_Connection(), payload)

    async def _reload(_self, _node, _cmd):
        return True, "", 1

    runtime._reload_candidate = types.MethodType(_reload, runtime)

    async def exercise() -> EndpointAttemptResult:
        try:
            return await runtime.validate_endpoint(command)
        finally:
            await runtime.close()

    return asyncio.run(exercise())


# ------------------------------------------------------------------ runtime


def test_confirm_pass_keeps_portfolio_verdict_and_latency(gate_proxy) -> None:
    result = _run_helper(gate_proxy, [CONFIRM_PASS], _command())

    assert result.kind is EndpointAttemptKind.PASS
    assert result.probe_url == PRIMARY
    primary, confirm = result.attempt_facts
    assert primary.endpoint == PRIMARY and primary.outcome == "pass"
    assert primary.stage == ValidationStage.FAST_PROBE.value
    assert confirm.endpoint == CONFIRM_PASS
    assert confirm.stage == CONFIRM_HTTPS_STAGE
    assert confirm.outcome == "pass" and confirm.status_code == 204
    assert confirm.attempt_id != primary.attempt_id
    # Admission latency is the portfolio probe's, not the confirm's.
    assert result.latency_ms == pytest.approx(float(primary.elapsed_ms))
    assert result.clean_confirmed is True
    assert gate_proxy.targets() == [PRIMARY, CONFIRM_PASS]


def test_confirm_tls_eof_is_a_candidate_failure(gate_proxy) -> None:
    result = _run_helper(gate_proxy, [CONFIRM_TLS_EOF], _command())

    assert result.kind is EndpointAttemptKind.CANDIDATE_FAIL
    assert result.reason.startswith(f"{CONFIRM_HTTPS_FAILED_REASON}:")
    assert result.latency_ms is None and result.probe_url == ""
    # The EOF is sampled twice: one transport failure is not yet a verdict.
    primary, first, repeat = result.attempt_facts
    assert primary.outcome == "pass"
    assert first.stage == repeat.stage == CONFIRM_HTTPS_STAGE
    assert first.outcome == "transient"
    assert repeat.completed is True
    assert repeat.outcome == "candidate_fail"
    assert repeat.failure_domain == "candidate"
    assert len({primary.attempt_id, first.attempt_id, repeat.attempt_id}) == 3
    assert gate_proxy.requests.count(("CONNECT", "tls-eof.invalid:443")) == 2


@pytest.mark.parametrize("url", [CONFIRM_FLAKY_503, CONFIRM_FLAKY_RESET])
def test_confirm_single_transport_hiccup_is_not_a_verdict(gate_proxy, url) -> None:
    # Review finding: one 5xx or one dropped connection used to become
    # CANDIDATE_FAIL -> a 6 h negative-cache cooldown for a good node.
    result = _run_helper(gate_proxy, [url], _command())

    assert result.kind is EndpointAttemptKind.PASS
    assert result.probe_url == PRIMARY
    primary, first, repeat = result.attempt_facts
    assert primary.outcome == "pass"
    assert first.stage == CONFIRM_HTTPS_STAGE and first.outcome == "transient"
    assert repeat.stage == CONFIRM_HTTPS_STAGE and repeat.outcome == "pass"
    assert gate_proxy.targets() == [PRIMARY, url, url]


def test_confirm_repeated_5xx_is_a_candidate_failure(gate_proxy) -> None:
    result = _run_helper(gate_proxy, [CONFIRM_ALWAYS_503], _command())

    assert result.kind is EndpointAttemptKind.CANDIDATE_FAIL
    assert result.reason == f"{CONFIRM_HTTPS_FAILED_REASON}:unexpected_status:503"
    assert [fact.outcome for fact in result.attempt_facts] == [
        "pass", "transient", "candidate_fail",
    ]
    assert gate_proxy.targets() == [PRIMARY, CONFIRM_ALWAYS_503, CONFIRM_ALWAYS_503]


def test_confirm_explicit_answer_is_not_resampled(gate_proxy) -> None:
    result = _run_helper(gate_proxy, [CONFIRM_403], _command())

    assert result.kind is EndpointAttemptKind.CANDIDATE_FAIL
    assert gate_proxy.targets() == [PRIMARY, CONFIRM_403]


def test_confirm_transport_failure_without_room_to_repeat_is_censored(gate_proxy) -> None:
    # 0.8 s of lease is left after the parent's delivery slack; the first
    # confirm burns ~0.4 s before its connection drops, leaving less than
    # the minimum repeat budget.  One failure alone must not negative-cache.
    result = _run_helper(
        gate_proxy, [CONFIRM_SLOW_RESET], _command(hard_cap=0.75 + 0.8), probe_timeout=1.0,
    )

    assert result.kind is EndpointAttemptKind.RIGHT_CENSORED
    assert result.reason == CONFIRM_HTTPS_CENSORED_REASON
    assert [fact.outcome for fact in result.attempt_facts] == ["pass", "transient"]
    assert gate_proxy.targets() == [PRIMARY, CONFIRM_SLOW_RESET]


@pytest.mark.parametrize(
    ("url", "status"),
    [(CONFIRM_403, 403), (CONFIRM_200_BODY, 200)],
)
def test_confirm_non_204_answer_is_a_candidate_failure(gate_proxy, url, status) -> None:
    result = _run_helper(gate_proxy, [url], _command())

    assert result.kind is EndpointAttemptKind.CANDIDATE_FAIL
    assert result.reason == f"{CONFIRM_HTTPS_FAILED_REASON}:unexpected_status:{status}"
    assert result.attempt_facts[-1].status_code == status


def test_confirm_timeout_is_right_censored_within_remaining_hard_cap(gate_proxy) -> None:
    # 0.75 s of the hard cap is the parent's delivery slack; 0.3 s is left for
    # the portfolio probe plus the confirm.
    result = _run_helper(
        gate_proxy, [CONFIRM_SLOW], _command(hard_cap=0.75 + 0.3), probe_timeout=1.0,
    )

    assert result.kind is EndpointAttemptKind.RIGHT_CENSORED
    assert result.reason == CONFIRM_HTTPS_CENSORED_REASON
    confirm = result.attempt_facts[-1]
    assert confirm.stage == CONFIRM_HTTPS_STAGE
    assert confirm.completed is False
    assert confirm.outcome == "right_censored"
    assert 0 < confirm.granted_budget_ms <= 300


def test_confirm_every_listed_url_must_pass(gate_proxy) -> None:
    result = _run_helper(gate_proxy, [CONFIRM_PASS, CONFIRM_403], _command())

    assert result.kind is EndpointAttemptKind.CANDIDATE_FAIL
    assert [fact.endpoint for fact in result.attempt_facts] == [
        PRIMARY, CONFIRM_PASS, CONFIRM_403,
    ]


def test_confirm_never_runs_when_the_portfolio_endpoint_fails(gate_proxy) -> None:
    result = _run_helper(
        gate_proxy, [CONFIRM_PASS], _command(endpoint="http://primary.invalid/forbidden"),
    )

    assert result.kind is EndpointAttemptKind.CANDIDATE_FAIL
    assert len(result.attempt_facts) == 1
    assert CONFIRM_PASS not in gate_proxy.targets()


@pytest.mark.parametrize("confirm_urls", [None, []])
def test_confirm_absent_or_empty_keeps_bare_portfolio_contract(gate_proxy, confirm_urls) -> None:
    result = _run_helper(gate_proxy, confirm_urls, _command())

    assert result.kind is EndpointAttemptKind.PASS
    assert len(result.attempt_facts) == 1
    assert gate_proxy.targets() == [PRIMARY]


def test_batch_slot_runs_the_same_confirm_gate(gate_proxy, tmp_path) -> None:
    excluded = {gate_proxy.port}
    mixed_port = _free_port(excluded)
    controller_port = _free_port(excluded | {mixed_port})
    runtime = BatchValidatorHelperRuntime(
        _Connection(),
        {
            "worker_id": 1,
            "worker_generation": 1,
            "binary_path": "unused",
            "probe_urls": [PRIMARY],
            "confirm_urls": [CONFIRM_TLS_EOF],
            "expected_statuses": [204],
            "probe_timeout_seconds": 1.0,
            "slow_threshold_ms": 3000,
            "temp_parent": str(tmp_path),
            "mixed_port": mixed_port,
            "controller_port": controller_port,
            "member_worker_ids": [1],
            "listener_ports": [gate_proxy.port],
        },
    )

    async def _install(_slot, _node, _digest, _budget):
        return True, "", 0

    runtime._install_candidate = _install  # type: ignore[method-assign]
    slot = _BatchProbeSlot(runtime, 1, gate_proxy.port)

    async def exercise() -> EndpointAttemptResult:
        try:
            return await ValidatorHelperRuntime.validate_endpoint(slot, _command())
        finally:
            await slot._invalidate_probe_session()

    try:
        assert slot.confirm_urls == (CONFIRM_TLS_EOF,)
        result = asyncio.run(exercise())
    finally:
        assert runtime._sender.close(timeout=2.0)

    assert result.kind is EndpointAttemptKind.CANDIDATE_FAIL
    assert result.reason.startswith(f"{CONFIRM_HTTPS_FAILED_REASON}:")
    assert result.attempt_facts[-1].stage == CONFIRM_HTTPS_STAGE


# ---------------------------------------------------------------- supervisor


def _fact(endpoint: str, *, stage: str, attempt: int, outcome: str = "pass") -> ProbeAttemptFact:
    return ProbeAttemptFact(
        endpoint=endpoint,
        elapsed_ms=40,
        full_budget=True,
        completed=True,
        status_code=204 if outcome == "pass" else None,
        outcome=outcome,
        attempt_id=f"lease:{attempt}",
        stage=stage,
    )


def _supervisor_result(
    command: ValidateEndpointCmd,
    facts: tuple[ProbeAttemptFact, ...],
    kind: EndpointAttemptKind = EndpointAttemptKind.PASS,
) -> dict[str, Any]:
    return dataclass_payload(
        EndpointAttemptResult(
            refresh_epoch=command.refresh_epoch,
            transaction_id=command.transaction_id,
            candidate_digest=command.candidate_digest,
            config_digest=command.config_digest,
            lease_id=command.lease_id,
            slice_id=command.slice_id,
            sequence=command.sequence,
            stage=command.stage,
            worker_id=1,
            worker_generation=1,
            node_json="",
            kind=kind,
            reason="endpoint_probe_passed",
            attempt_facts=facts,
            clean_confirmed=True,
            probe_policy_revision=command.probe_policy_revision,
            portfolio_digest=command.portfolio_digest,
        )
    )


def _plain_supervisor(confirm_urls: tuple[str, ...], attempts: list) -> Tier3ValidatorSupervisor:
    return Tier3ValidatorSupervisor(
        binary_path="fixture",
        probe_urls=[PRIMARY],
        expected_statuses={204},
        probe_timeout_seconds=1.0,
        slow_threshold_ms=3000,
        attempt_sink=attempts.append,
        capacity_sink=lambda _count: None,
        min_workers=1,
        max_workers=1,
        confirm_urls=confirm_urls,
    )


def _validating_worker(command: ValidateEndpointCmd) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        command=command,
        worker_id=1,
        generation=1,
        sealed_lease_id="",
        deadline_at=1.0,
        deadline_phase="probe",
        transport_started_at=0.0,
        cancel_deadline_at=0.0,
        phase=None,
        heartbeat_at=0.0,
    )


@pytest.mark.parametrize(
    ("facts", "accepted"),
    [
        # Portfolio fact first, one trailing confirm fact per confirm URL.
        (
            (
                _fact(PRIMARY, stage="fast_probe", attempt=1),
                _fact(CONFIRM_PASS, stage=CONFIRM_HTTPS_STAGE, attempt=2),
            ),
            True,
        ),
        # A confirm fact can never stand in for the portfolio fact.
        ((_fact(CONFIRM_PASS, stage=CONFIRM_HTTPS_STAGE, attempt=2),), False),
        (
            (
                _fact(CONFIRM_PASS, stage=CONFIRM_HTTPS_STAGE, attempt=2),
                _fact(PRIMARY, stage="fast_probe", attempt=1),
            ),
            False,
        ),
        # A transport failure repeated once: two confirm facts for one URL.
        (
            (
                _fact(PRIMARY, stage="fast_probe", attempt=1),
                _fact(
                    CONFIRM_PASS, stage=CONFIRM_HTTPS_STAGE, attempt=2, outcome="transient",
                ),
                _fact(CONFIRM_PASS, stage=CONFIRM_HTTPS_STAGE, attempt=3),
            ),
            True,
        ),
        # More confirm facts than two samples per configured confirm URL.
        (
            (
                _fact(PRIMARY, stage="fast_probe", attempt=1),
                _fact(CONFIRM_PASS, stage=CONFIRM_HTTPS_STAGE, attempt=2),
                _fact(CONFIRM_PASS, stage=CONFIRM_HTTPS_STAGE, attempt=3),
                _fact(CONFIRM_PASS, stage=CONFIRM_HTTPS_STAGE, attempt=4),
            ),
            False,
        ),
        # Two portfolio facts are still a protocol violation.
        (
            (
                _fact(PRIMARY, stage="fast_probe", attempt=1),
                _fact(PRIMARY, stage="fast_probe", attempt=2),
            ),
            False,
        ),
    ],
)
def test_supervisor_accepts_only_portfolio_fact_with_trailing_confirm_facts(
    monkeypatch, facts, accepted,
) -> None:
    attempts: list[EndpointAttemptResult] = []
    retired: list[str] = []
    supervisor = _plain_supervisor((CONFIRM_PASS,), attempts)
    monkeypatch.setattr(
        supervisor,
        "_retire_worker",
        lambda _worker, reason, **_kwargs: retired.append(reason),
    )
    command = _command()

    supervisor._handle_endpoint_result(
        _validating_worker(command), _supervisor_result(command, facts),
    )

    if accepted:
        assert retired == []
        assert len(attempts) == 1 and attempts[0].attempt_facts == facts
    else:
        assert attempts == []
        assert retired == ["helper_endpoint_result_protocol_mismatch"]


def test_single_slot_start_payload_carries_confirm_urls() -> None:
    supervisor = _plain_supervisor(("https://www.google.com/generate_204",), [])
    supervisor._desired_workers = 1
    supervisor._workers = {
        1: types.SimpleNamespace(
            worker_id=1,
            generation=1,
            phase=Tier3WorkerPhase.WAITING_START,
            bootstrap_ready=True,
            mixed_port=20001,
            controller_port=21001,
        )
    }

    with patch.object(supervisor, "_send", return_value=True) as send:
        supervisor._activate_waiting_workers()

    assert send.call_count == 1
    _worker, kind, payload = send.call_args.args
    assert kind == "start"
    assert payload["confirm_urls"] == ["https://www.google.com/generate_204"]


def test_batch_start_payload_carries_confirm_urls() -> None:
    supervisor = Tier3BatchValidatorSupervisor(
        slots_per_process=2,
        binary_path="unused-by-fixture",
        probe_urls=[PRIMARY],
        expected_statuses={204},
        probe_timeout_seconds=1.0,
        slow_threshold_ms=3000,
        attempt_sink=lambda _result: None,
        capacity_sink=lambda _count: None,
        min_workers=2,
        max_workers=2,
        confirm_urls=["https://www.google.com/generate_204"],
    )
    group = _HelperGroup(1, 1, None, None, None, threading.Lock())
    members = [
        _BatchWorkerGeneration(
            worker_id=worker_id, generation=1, process=None, connection=None, job=None,
            send_lock=group.send_lock, group=group, phase=Tier3WorkerPhase.WAITING_START,
            bootstrap_ready=True,
        )
        for worker_id in (1, 2)
    ]
    group.members = members
    group.lead = members[0]
    group.mixed_port, group.controller_port, group.listener_ports = 20001, 20002, [20003, 20004]
    supervisor._groups[1] = group
    for member in members:
        supervisor._workers[member.worker_id] = member
    supervisor._desired_workers = 2

    with patch.object(supervisor, "_send", return_value=True) as send:
        supervisor._activate_waiting_groups()

    assert send.call_count == 1
    _lead, kind, payload = send.call_args.args
    assert kind == "start"
    assert payload["confirm_urls"] == ["https://www.google.com/generate_204"]
    assert payload["member_worker_ids"] == [1, 2]


# -------------------------------------------------------------------- actor


def _envelope(digest: str, endpoints: tuple[str, ...]) -> CandidateEnvelope:
    return CandidateEnvelope(
        refresh_epoch="epoch-1",
        candidate_digest=digest,
        config_digest=f"config-{digest}",
        node_json="{}",
        report_json="{}",
        source_root="root-a",
        source_family="vless",
        probe_urls=endpoints,
    )


def _actor_result(
    command: ValidateEndpointCmd,
    kind: EndpointAttemptKind,
    facts: tuple[ProbeAttemptFact, ...],
    reason: str,
) -> EndpointAttemptResult:
    return EndpointAttemptResult(
        refresh_epoch=command.refresh_epoch,
        transaction_id=command.transaction_id,
        candidate_digest=command.candidate_digest,
        config_digest=command.config_digest,
        lease_id=command.lease_id,
        slice_id=command.slice_id,
        sequence=command.sequence,
        stage=command.stage,
        worker_id=1,
        worker_generation=1,
        node_json=command.node_json,
        kind=kind,
        reason=reason,
        latency_ms=40.0 if kind is EndpointAttemptKind.PASS else None,
        probe_url=command.endpoint if kind is EndpointAttemptKind.PASS else "",
        attempt_facts=facts,
        probe_policy_revision=command.probe_policy_revision,
        portfolio_digest=command.portfolio_digest,
        clean_confirmed=True,
    )


def _portfolio_pass(command: ValidateEndpointCmd) -> ProbeAttemptFact:
    return ProbeAttemptFact(
        endpoint=command.endpoint,
        elapsed_ms=40,
        full_budget=True,
        completed=True,
        status_code=204,
        outcome="pass",
        attempt_id=f"{command.lease_id}:1",
        stage=command.stage.value,
    )


def _confirm_fact(command: ValidateEndpointCmd, url: str, outcome: str) -> ProbeAttemptFact:
    completed = outcome != "right_censored"
    return ProbeAttemptFact(
        endpoint=url,
        elapsed_ms=60 if completed else 5000,
        full_budget=completed,
        completed=completed,
        entitlement_exhausted=not completed,
        status_code=204 if outcome == "pass" else None,
        outcome=outcome,
        attempt_id=f"{command.lease_id}:2",
        stage=CONFIRM_HTTPS_STAGE,
    )


def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition did not become true")


def _started_actor(
    endpoints: tuple[str, ...],
    *,
    confirm_budget: float = 5.0,
) -> tuple[Tier3ExpansionActor, list[ValidateEndpointCmd], "queue.Queue[ValidationTerminal]", list]:
    dispatched: list[ValidateEndpointCmd] = []
    terminals: queue.Queue[ValidationTerminal] = queue.Queue()
    continuations: list = []
    actor = Tier3ExpansionActor(
        "epoch-1",
        dispatch_sink=dispatched.append,
        admission_sink=lambda _intent: True,
        terminal_sink=terminals,
        initial_worker_envelope=2,
        endpoint_controller=Tier3EndpointController(endpoints),
        continuation_sink=lambda *args: continuations.append(args),
        confirm_budget_seconds=confirm_budget,
    )
    actor.start()
    return actor, dispatched, terminals, continuations


def test_actor_hard_cap_includes_confirm_budget() -> None:
    endpoints = ("http://probe-a.example/204",)
    actor, dispatched, _terminals, _continuations = _started_actor(endpoints)
    try:
        assert actor.submit(_envelope("1" * 64, endpoints))
        actor.capacity_available(1)
        _wait_until(lambda: len(dispatched) == 1)
        fast = dispatched[0]
        assert fast.probe_budget_seconds == 3.0
        assert fast.candidate_hard_cap_seconds == pytest.approx(2.0 + 3.0 + 5.0 + 0.75)
        # A plain FAST censor retains the canonical entitlement as before.
        actor.endpoint_result(
            _actor_result(fast, EndpointAttemptKind.RIGHT_CENSORED, (), "fixture")
        )
        actor.capacity_available(1)
        _wait_until(lambda: len(dispatched) == 2)
        deep = dispatched[1]
        assert deep.stage is ValidationStage.CANONICAL_DEEP
        assert deep.candidate_hard_cap_seconds == pytest.approx(2.0 + 5.0 + 5.0 + 0.75)
    finally:
        assert actor.stop()


@pytest.mark.parametrize(
    ("kind", "reason", "outcome", "terminal_kind"),
    [
        (
            EndpointAttemptKind.CANDIDATE_FAIL,
            f"{CONFIRM_HTTPS_FAILED_REASON}:connect:ssl eof",
            "candidate_fail",
            Tier3TerminalKind.CANDIDATE_FAIL,
        ),
        (
            EndpointAttemptKind.RIGHT_CENSORED,
            CONFIRM_HTTPS_CENSORED_REASON,
            "right_censored",
            Tier3TerminalKind.CENSORED,
        ),
    ],
)
def test_actor_confirm_verdict_finalises_fast_without_canonical_replay(
    kind, reason, outcome, terminal_kind,
) -> None:
    endpoints = ("http://probe-a.example/204", "https://probe-b.example/204")
    actor, dispatched, terminals, continuations = _started_actor(endpoints)
    try:
        assert actor.submit(_envelope("2" * 64, endpoints))
        actor.capacity_available(1)
        _wait_until(lambda: len(dispatched) == 1)
        fast = dispatched[0]
        actor.endpoint_result(
            _actor_result(
                fast,
                kind,
                (_portfolio_pass(fast), _confirm_fact(fast, CONFIRM_TLS_EOF, outcome)),
                reason,
            )
        )
        terminal = terminals.get(timeout=2.0)
        assert terminal.kind is terminal_kind
        assert terminal.reason == reason
        assert terminal.stage is ValidationStage.FAST_PROBE
        assert [fact.stage for fact in terminal.attempt_facts] == [
            ValidationStage.FAST_PROBE.value, CONFIRM_HTTPS_STAGE,
        ]
        actor.capacity_available(2)
        time.sleep(0.1)
        assert len(dispatched) == 1, "no canonical replay after a confirm verdict"
        snapshot = actor.snapshot()
        assert snapshot.fast_continued == 0
        assert snapshot.passed == 0
        assert snapshot.conservation_ok
        assert continuations == []
    finally:
        assert actor.stop()


def test_actor_deep_confirm_censor_ends_the_ledger() -> None:
    endpoints = (
        "http://probe-a.example/204",
        "http://probe-b.example/204",
        "https://probe-c.example/204",
    )
    actor, dispatched, terminals, _continuations = _started_actor(endpoints)
    try:
        assert actor.submit(_envelope("3" * 64, endpoints))
        actor.capacity_available(1)
        _wait_until(lambda: len(dispatched) == 1)
        fast = dispatched[0]
        actor.endpoint_result(
            _actor_result(fast, EndpointAttemptKind.RIGHT_CENSORED, (), "fixture")
        )
        actor.capacity_available(1)
        _wait_until(lambda: len(dispatched) == 2)
        deep = dispatched[1]
        assert deep.stage is ValidationStage.CANONICAL_DEEP
        actor.endpoint_result(
            _actor_result(
                deep,
                EndpointAttemptKind.RIGHT_CENSORED,
                (
                    _portfolio_pass(deep),
                    _confirm_fact(deep, CONFIRM_TLS_EOF, "right_censored"),
                ),
                CONFIRM_HTTPS_CENSORED_REASON,
            )
        )
        terminal = terminals.get(timeout=2.0)
        assert terminal.kind is Tier3TerminalKind.CENSORED
        assert terminal.reason == CONFIRM_HTTPS_CENSORED_REASON
        actor.capacity_available(2)
        time.sleep(0.1)
        assert len(dispatched) == 2
    finally:
        assert actor.stop()


def test_actor_trailing_confirm_fact_does_not_touch_endpoint_ledgers() -> None:
    # The confirm URL deliberately coincides with a portfolio endpoint: the
    # stage, not the URL, tells the two facts apart.
    endpoints = ("http://probe-a.example/204", "http://probe-b.example/204")
    controller = Tier3EndpointController(endpoints)
    dispatched: list[ValidateEndpointCmd] = []
    terminals: queue.Queue[ValidationTerminal] = queue.Queue()
    actor = Tier3ExpansionActor(
        "epoch-1",
        dispatch_sink=dispatched.append,
        admission_sink=lambda _intent: True,
        terminal_sink=terminals,
        initial_worker_envelope=2,
        endpoint_controller=controller,
        confirm_budget_seconds=5.0,
    )
    actor.start()
    try:
        assert actor.submit(_envelope("4" * 64, endpoints))
        actor.capacity_available(1)
        _wait_until(lambda: len(dispatched) == 1)
        fast = dispatched[0]
        other = next(endpoint for endpoint in endpoints if endpoint != fast.endpoint)
        actor.endpoint_result(
            _actor_result(
                fast,
                EndpointAttemptKind.PASS,
                (_portfolio_pass(fast), _confirm_fact(fast, other, "pass")),
                "endpoint_probe_passed",
            )
        )
        terminal = terminals.get(timeout=2.0)
        assert terminal.kind is Tier3TerminalKind.PASS
        assert terminal.probe_url == fast.endpoint
        state = actor._states[fast.candidate_digest]
        assert set(state.endpoint_outcomes) == {fast.endpoint}
        stats = controller.snapshot()
        assert stats[controller._diagnostic_label(fast.endpoint)]["successes"] == 1
        assert stats[controller._diagnostic_label(other)]["completed"] == 0
        assert stats["_policy"]["paired_evidence_count"] == 0
    finally:
        assert actor.stop()


# ------------------------------------------------------ settings and domain


def test_process_validator_settings_default_and_disable_confirm_urls() -> None:
    assert DEFAULT_PROCESS_CONFIRM_URLS == ("https://www.google.com/generate_204",)
    assert process_validator_settings({})["confirm_urls"] == [
        "https://www.google.com/generate_204"
    ]
    assert process_validator_settings(
        {"mihomo_helper": {"confirm_urls": []}}
    )["confirm_urls"] == []
    assert process_validator_settings(
        {"mihomo_helper": {"confirm_urls": ""}}
    )["confirm_urls"] == []
    assert process_validator_settings(
        {
            "mihomo_helper": {
                "confirm_urls": [
                    "https://github.com",
                    "not a url",
                    "https://github.com",
                    "https://www.google.com/generate_204",
                ]
            }
        }
    )["confirm_urls"] == ["https://github.com", "https://www.google.com/generate_204"]


@pytest.mark.parametrize("slots", [1, 4])
def test_domain_carries_confirm_urls_to_supervisor_and_actor(slots: int) -> None:
    kwargs: dict[str, Any] = dict(
        refresh_epoch=f"confirm-domain-{slots}",
        binary_path="fixture",
        probe_urls=[PRIMARY],
        expected_statuses={204},
        probe_timeout_seconds=8.0,
        slow_threshold_ms=3000,
        commit_node=None,
        terminal_observer=None,
        slots_per_process=slots,
    )
    bare = Tier3ValidationDomain(**kwargs)
    assert bare.supervisor.confirm_urls == ()
    assert bare.actor._confirm_budget_seconds == 0.0

    gated = Tier3ValidationDomain(
        confirm_urls=["https://www.google.com/generate_204"], **kwargs,
    )
    assert gated.supervisor.confirm_urls == ("https://www.google.com/generate_204",)
    # Per-URL budget is min(5 s, probe timeout).
    assert gated.actor._confirm_budget_seconds == pytest.approx(5.0)
    assert confirm_https_budget_seconds(["a", "b"], 1.5) == pytest.approx(3.0)
    assert confirm_https_budget_seconds([], 5.0) == 0.0


def test_confirm_failure_terminal_is_a_deterministic_candidate_failure() -> None:
    node = Node(name="eof", protocol="trojan", address="198.51.100.1", port=443, tier="tier3")
    confirm = ProbeAttemptFact(
        endpoint=CONFIRM_TLS_EOF,
        elapsed_ms=80,
        full_budget=True,
        completed=True,
        outcome="candidate_fail",
        failure_domain="candidate",
        detail="connect:ssl eof",
        stage=CONFIRM_HTTPS_STAGE,
    )
    terminal = ValidationTerminal(
        refresh_epoch="epoch",
        candidate_digest="c" * 64,
        config_digest="d" * 64,
        lease_id="lease",
        sequence=1,
        worker_id=1,
        worker_generation=1,
        kind=Tier3TerminalKind.CANDIDATE_FAIL,
        node_json=canonical_json(node.to_dict()),
        reason=f"{CONFIRM_HTTPS_FAILED_REASON}:connect:ssl eof",
        attempt_facts=(confirm,),
    )

    tested = node_from_terminal(terminal)

    # The negative cache keys its (non-short) cooldown on this stage.
    assert tested.extra["tier3_failure_stage"] == "full_validation_candidate_failed"
    assert CONFIRM_HTTPS_FAILED_REASON in tested.extra["tier3_failure_reason"]
    assert tested.extra["validation_probe_failures_by_url"] == {
        CONFIRM_TLS_EOF: "connect:ssl eof"
    }


def test_the_confirm_gate_keeps_the_validation_contract() -> None:
    # Replays are admitted only through a live validation that now runs the
    # confirm, so the gate needs no contract bump; a bump would only demote
    # the warm-start cache to slow re-probing (coordinator decision 09-25).
    assert b"mainstream-https-confirm" not in VALIDATION_CONTRACT_MATERIAL
    assert VALIDATION_CONTRACT_MATERIAL.endswith(
        b"|readonly-network-context-no-policy-input"
    )
    assert TIER3_VALIDATION_CONTRACT_HASH == hashlib.sha256(
        VALIDATION_CONTRACT_MATERIAL
    ).hexdigest()[:24]


# ------------------------------------------------ Tier3B recheck (legacy path)
#
# Review finding: Tier3B periodic and incident rechecks run the legacy tester,
# which proves only the neutral probe URLs.  A node that answers
# cp.cloudflare.com but SSL-EOFs on every real site was kept ALIVE, or an
# incident on it "recovered", so the pool looked full and supply stopped.


def _mihomo_pass(value: Node) -> Node:
    passed = value.clone()
    passed.extra["validated_by"] = "mihomo-batch"
    return passed


def _run_tier3b_incident_recheck(monkeypatch, verdict: str):
    value = runtime_node("tier3", tier3_origin="tier3b")
    instance = tier3_manager(value)
    confirm_incident(instance, value)
    calls: list[tuple[list[str], list[str]]] = []
    monkeypatch.setattr(
        manager_module, "test_nodes", lambda values, **_kwargs: [_mihomo_pass(value)],
    )

    def fake_confirm(nodes, _config, urls, _logger, **_kwargs):
        calls.append(([node.name for node in nodes], list(urls)))
        return [(verdict, "https://www.google.com/generate_204: HTTP 503")] * len(nodes)

    monkeypatch.setattr(manager_module, "confirm_tier3_nodes_mainstream_https", fake_confirm)
    context = instance._capture_health_check_context(
        "tier3", value, origin="incident_initial",
    )
    transition = instance._run_unbound_tier3_health_check(context, value)
    return instance, transition, calls


def test_tier3b_incident_recheck_does_not_recover_a_node_failing_the_confirm(
    monkeypatch,
) -> None:
    instance, transition, calls = _run_tier3b_incident_recheck(monkeypatch, "fail")

    assert calls == [(["leaf"], list(DEFAULT_PROCESS_CONFIRM_URLS))]
    assert transition["action"] == "transferred"
    current = instance.runtime_nodes["tier3"][0]
    assert current.status is NodeStatus.SUSPICIOUS
    assert current.suspicious_reason.startswith(
        f"mihomo_validation_failed: {CONFIRM_HTTPS_FAILED_REASON}:"
    )
    # The same deterministic stage the admission gate records.
    assert current.extra["tier3_failure_stage"] == "full_validation_candidate_failed"
    assert not instance.effective_node_usable("tier3", current)


@pytest.mark.parametrize(
    ("verdict", "action", "usable"),
    [
        ("pass", "recovered", True),
        # A timeout is not a verdict: the incident stays open, no recovery.
        ("censored", "pending", False),
        # The confirm could not run: the probe verdict stands (fail-open), so
        # a validator facility fault can never empty a working pool.
        ("facility", "recovered", True),
    ],
)
def test_tier3b_incident_recheck_other_confirm_verdicts(
    monkeypatch, verdict, action, usable,
) -> None:
    instance, transition, _calls = _run_tier3b_incident_recheck(monkeypatch, verdict)

    assert transition["action"] == action
    current = instance.runtime_nodes["tier3"][0]
    assert instance.effective_node_usable("tier3", current) is usable


def _slice_manager(*, testing_config: dict[str, Any], gateway: list[Node]) -> Any:
    stub = types.SimpleNamespace(
        logger=logging.getLogger(__name__),
        _testing_config_for_node_checks=lambda: dict(testing_config),
        _gateway_nodes_for_testing=lambda _tier: list(gateway),
        _run_async=lambda value, **_kwargs: value,
    )
    stub._confirm_tier3_recheck_results = types.MethodType(
        manager_module.ProxyTierManager._confirm_tier3_recheck_results, stub,
    )
    return stub


def test_tier3b_recheck_slice_confirms_only_fresh_mihomo_passes(monkeypatch) -> None:
    fresh = _mihomo_pass(Node(name="fresh", protocol="ss", address="a.example", port=1, tier="tier3"))
    tcp_only = Node(
        name="tcp-only", protocol="ss", address="b.example", port=1, tier="tier3",
        status=NodeStatus.ALIVE,
    )
    failed = Node(
        name="failed", protocol="ss", address="c.example", port=1, tier="tier3",
        status=NodeStatus.SUSPICIOUS, extra={"validated_by": "mihomo-batch"},
    )
    fresh.status = NodeStatus.SLOW
    confirmed: list[str] = []
    observed: list[str] = []
    monkeypatch.setattr(
        manager_module,
        "test_nodes",
        lambda values, **kwargs: (
            observed.append(f"observer={kwargs['result_observer'] is not None}")
            or [fresh, tcp_only, failed]
        ),
    )

    def fake_confirm(nodes, _config, urls, _logger, **kwargs):
        confirmed.extend(node.name for node in nodes)
        assert kwargs["per_url_timeout_seconds"] == pytest.approx(5.0)
        return [("censored", "timeout")] * len(nodes)

    monkeypatch.setattr(manager_module, "confirm_tier3_nodes_mainstream_https", fake_confirm)
    stub = _slice_manager(testing_config={}, gateway=[])

    results = manager_module.ProxyTierManager._test_tier3_candidate_slice(
        stub, [fresh], result_observer=lambda value: observed.append(value.name),
    )

    assert confirmed == ["fresh"]
    by_name = {value.name: value for value in results}
    assert by_name["fresh"].status is NodeStatus.SUSPICIOUS
    assert by_name["fresh"].extra["tier3_failure_stage"] == "validation_deferred"
    assert health_check_verdict(by_name["fresh"]) == "pending"
    assert by_name["tcp-only"].status is NodeStatus.ALIVE
    # The observer sees final (post-confirm) results only.
    assert observed == ["observer=False", "fresh", "tcp-only", "failed"]


@pytest.mark.parametrize(
    ("testing_config", "gateway", "full_validation"),
    [
        ({"mihomo_helper": {"confirm_urls": []}}, [], None),
        ({}, [Node(name="gw", protocol="ss", address="g.example", port=1, tier="exit")], None),
        ({}, [], False),
    ],
    ids=["disabled", "gateway-chain", "tcp-only-check"],
)
def test_tier3b_recheck_slice_skips_confirm(
    monkeypatch, testing_config, gateway, full_validation,
) -> None:
    passed = _mihomo_pass(
        Node(
            name="p", protocol="ss", address="a.example", port=1, tier="tier3",
            status=NodeStatus.ALIVE,
        )
    )
    monkeypatch.setattr(manager_module, "test_nodes", lambda values, **_kwargs: [passed])
    monkeypatch.setattr(
        manager_module,
        "confirm_tier3_nodes_mainstream_https",
        lambda *args, **kwargs: pytest.fail("confirm must not run"),
    )
    stub = _slice_manager(testing_config=testing_config, gateway=gateway)

    results = manager_module.ProxyTierManager._test_tier3_candidate_slice(
        stub, [passed], full_validation_enabled=full_validation,
    )

    assert [value.status for value in results] == [NodeStatus.ALIVE]


_MIHOMO = Path(__file__).resolve().parents[1] / "bin" / "mihomo.exe"


class _NodeProxyHandler(socketserver.BaseRequestHandler):
    """One candidate node as Mihomo's HTTP outbound sees it (CONNECT)."""

    def handle(self) -> None:
        self.request.settimeout(10.0)
        server: _NodeProxy = self.server  # type: ignore[assignment]
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.request.recv(4096)
            if not chunk:
                return
            head += chunk
        if not head.startswith(b"CONNECT "):
            return
        with server.lock:
            server.connects += 1
        self.request.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        if server.mode == "eof":
            return  # relays the check, then drops the real request
        inner = b""
        while b"\r\n\r\n" not in inner:
            chunk = self.request.recv(4096)
            if not chunk:
                return
            inner += chunk
        if server.mode == "hang":
            time.sleep(3.0)
            return
        status = 204 if server.mode == "good" else 403
        self.request.sendall(
            f"HTTP/1.1 {status} X\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode()
        )


class _NodeProxy(socketserver.ThreadingTCPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, mode: str) -> None:
        super().__init__(("127.0.0.1", 0), _NodeProxyHandler)
        self.mode = mode
        self.lock = threading.Lock()
        self.connects = 0
        threading.Thread(target=self.serve_forever, daemon=True).start()


@pytest.mark.skipif(not _MIHOMO.exists(), reason="bundled mihomo binary required")
@pytest.mark.skipif(os.environ.get("GITHUB_ACTIONS") == "true",
                    reason="speed gate against real helper processes or a real Core; "
                    "the release runner is slower than these budgets (runs locally)")
def test_confirm_tier3_nodes_with_one_real_mihomo() -> None:
    servers = {mode: _NodeProxy(mode) for mode in ("good", "eof", "hang", "forbidden")}
    try:
        nodes = [
            Node(
                name=mode, protocol="http", address="127.0.0.1",
                port=int(server.server_address[1]), tier="tier3",
            )
            for mode, server in servers.items()
        ]
        nodes.append(Node(name="odd", protocol="bogus", address="127.0.0.1", port=1, tier="tier3"))

        verdicts = confirm_tier3_nodes_mainstream_https(
            nodes,
            {"mihomo_binary_path": str(_MIHOMO)},
            ["http://confirm.invalid/generate_204"],
            logging.getLogger(__name__),
            per_url_timeout_seconds=1.0,
        )

        assert [verdict for verdict, _detail in verdicts] == [
            "pass", "fail", "censored", "fail", "facility",
        ]
        # A failed test is repeated once before it is a verdict; an explicit
        # wrong status is not.
        assert servers["eof"].connects == 2
        assert servers["forbidden"].connects == 1
        assert servers["good"].connects == 1
    finally:
        for server in servers.values():
            server.shutdown()
            server.server_close()
