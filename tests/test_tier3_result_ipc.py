from __future__ import annotations

from dataclasses import replace
import multiprocessing
import os
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import tier3_validation_protocol as protocol
from tier3_validator_supervisor import Tier3ValidatorSupervisor


def command_and_result(sequence: int = 1):
    command = protocol.ValidateEndpointCmd(
        refresh_epoch="ipc-session", candidate_digest="a" * 64, config_digest="b" * 64,
        lease_id=f"lease-{sequence}", sequence=sequence, worker_id=1, worker_generation=2,
        transaction_id="transaction", slice_id=1, stage=protocol.ValidationStage.FAST_PROBE,
        node_json=protocol.canonical_json({"password": "private-fixture", "metadata": "x" * 12000}),
        report_json='{"configured_source_count":5}', source_root="opaque-root", source_family="opaque",
        endpoint="https://probe.invalid/generate_204", probe_budget_seconds=3.0,
    )
    result = protocol.EndpointAttemptResult(
        refresh_epoch=command.refresh_epoch, candidate_digest=command.candidate_digest,
        config_digest=command.config_digest, lease_id=command.lease_id, sequence=sequence,
        transaction_id=command.transaction_id, slice_id=1, stage=command.stage,
        worker_id=1, worker_generation=2, node_json=command.node_json, report_json=command.report_json,
        kind=protocol.EndpointAttemptKind.PASS, clean_confirmed=True,
        attempt_facts=(protocol.ProbeAttemptFact(
            endpoint=command.endpoint, elapsed_ms=30, full_budget=False, completed=True,
            status_code=204, outcome="pass",
        ),),
    )
    return command, result


def owner(command):
    attempts, capacity = [], []
    supervisor = Tier3ValidatorSupervisor(
        binary_path="fixture", probe_urls=[command.endpoint], expected_statuses={204},
        probe_timeout_seconds=3, slow_threshold_ms=3000,
        capacity_sink=capacity.append, attempt_sink=attempts.append,
    )
    worker = SimpleNamespace(
        worker_id=1, generation=2, command=command, sealed_lease_id="",
        phase=protocol.Tier3WorkerPhase.VALIDATING, transport_started_at=1,
    )
    supervisor._retire_worker = Mock()
    return supervisor, worker, attempts, capacity


def test_result_wire_omits_parent_owned_payload_but_preserves_all_evidence():
    _, result = command_and_result()
    original = protocol.dataclass_payload(result)
    wire = protocol.endpoint_result_wire_payload(result)
    assert wire == {**original, "node_json": "", "report_json": "{}"}
    raw = protocol.message_bytes("endpoint_result", wire)
    assert b"private-fixture" not in raw
    assert len(raw) < 4096
    assert protocol.dataclass_payload(result) == original


@pytest.mark.parametrize("kind", list(protocol.EndpointAttemptKind))
def test_supervisor_rehydrates_only_the_matching_lease(kind):
    command, result = command_and_result()
    result = replace(result, kind=kind)
    supervisor, worker, attempts, capacity = owner(command)
    supervisor._handle_endpoint_result(worker, protocol.endpoint_result_wire_payload(result))
    assert attempts == [result]
    assert capacity == [1]
    assert worker.command is None
    supervisor._retire_worker.assert_not_called()


@pytest.mark.parametrize("field,value", [
    ("refresh_epoch", "old"), ("candidate_digest", "other"), ("config_digest", "other"),
    ("lease_id", "old"), ("transaction_id", "old"), ("slice_id", 2), ("sequence", 2),
    ("worker_id", 2), ("worker_generation", 3), ("probe_policy_revision", 99),
    ("portfolio_digest", "other"), ("stage", "canonical_deep"),
    ("purpose", "user_pool_health"),
])
def test_stale_compact_result_has_no_feedback_or_capacity_authority(field, value):
    command, result = command_and_result()
    supervisor, worker, attempts, capacity = owner(command)
    payload = {**protocol.endpoint_result_wire_payload(result), field: value}
    supervisor._handle_endpoint_result(worker, payload)
    assert attempts == capacity == []
    assert worker.command is command
    supervisor._retire_worker.assert_called_once()


def test_matching_result_cannot_replace_parent_node_or_report():
    command, result = command_and_result()
    supervisor, worker, attempts, _ = owner(command)
    payload = {**protocol.dataclass_payload(result), "node_json": '{"password":"different"}',
               "report_json": '{"configured_source_count":999}'}
    supervisor._handle_endpoint_result(worker, payload)
    assert attempts == [result]


def test_one_thousand_duplicate_and_cancelled_results_do_not_reacquire_lease():
    for sequence in range(1000):
        command, result = command_and_result(sequence)
        supervisor, worker, attempts, capacity = owner(command)
        payload = protocol.endpoint_result_wire_payload(result)
        if sequence % 4 == 0:
            worker.command = None
        elif sequence % 4 == 1:
            worker.sealed_lease_id = command.lease_id
        expected = int(sequence % 4 >= 2)
        for _ in range(2):
            supervisor._handle_endpoint_result(worker, payload)
        assert len(attempts) == len(capacity) == expected
        assert not expected or worker.command is None


@pytest.mark.skipif(os.name != "nt", reason="native Windows message Pipe capacity")
def test_compact_result_does_not_require_reader_to_drain_repeated_node_configuration():
    _, result = command_and_result()
    full = protocol.message_bytes("endpoint_result", protocol.dataclass_payload(result))
    compact = protocol.message_bytes("endpoint_result", protocol.endpoint_result_wire_payload(result))
    for raw, fits in ((full, False), (compact, True)):
        reader, writer = multiprocessing.Pipe()
        entered, sent = threading.Event(), threading.Event()
        def send():
            entered.set()
            writer.send_bytes(raw)
            sent.set()
        thread = threading.Thread(target=send)
        thread.start()
        try:
            assert entered.wait(1)
            if fits:
                assert sent.wait(1)
            else:
                assert not sent.wait(0.1)
            assert reader.recv_bytes() == raw
            assert sent.wait(1)
        finally:
            reader.close()
            writer.close()
            thread.join(2)
        assert not thread.is_alive()
