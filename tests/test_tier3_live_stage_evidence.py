from collections import deque
import inspect
import threading
from types import SimpleNamespace
from unittest.mock import Mock

from manager import ProxyTierManager, _tier3_process_trace_fields
from tier3_validation_domain import Tier3ValidationDomain
from tier3_validation_protocol import EndpointAttemptKind, EndpointAttemptResult, ValidationStage


def test_live_trace_copies_bounded_owner_rings_without_changing_authority():
    snapshot = SimpleNamespace(**{
        f"{kind}_samples": tuple({"sequence": i, "attempts": [{"elapsed_ms": i}]}
                                for i in range(80))
        for kind in ("dispatch", "attempt", "terminal")
    })
    fields = _tier3_process_trace_fields(snapshot)
    assert set(fields) == {f"source_stream_process_{kind}_samples"
                           for kind in ("dispatch", "attempt", "terminal")}
    for kind in ("dispatch", "attempt", "terminal"):
        rows = fields[f"source_stream_process_{kind}_samples"]
        assert len(rows) == 64
        assert [row["sequence"] for row in rows] == list(range(16, 80))
        rows[0]["attempts"][0]["elapsed_ms"] = -1
        assert getattr(snapshot, f"{kind}_samples")[16]["attempts"][0]["elapsed_ms"] == 16
    assert all(not value for value in _tier3_process_trace_fields(None).values())
    source = inspect.getsource(ProxyTierManager.refresh_tier3)
    hot = source.split("def source_stream_hot_progress_fields(", 1)[1].split(
        "def publish_source_stream_queue_progress(", 1)[0]
    assert "**_tier3_process_trace_fields(process_snapshot)" in hot


def test_attempt_timing_uses_owner_receive_time_and_preserves_result(monkeypatch):
    domain = object.__new__(Tier3ValidationDomain)
    domain.actor = SimpleNamespace(endpoint_result=Mock())
    domain._result_metrics_lock = threading.Lock()
    domain._diagnostics_lock = threading.Lock()
    domain._actor_result_max_ms = 0
    domain._attempt_samples = deque(maxlen=64)
    domain._publish_observer_event = Mock()
    monkeypatch.setattr("tier3_validation_domain.time.monotonic_ns", lambda: 999)
    result = EndpointAttemptResult(
        refresh_epoch="session", transaction_id="transaction", candidate_digest="a" * 64,
        config_digest="b" * 64, lease_id="lease", slice_id=2, sequence=3,
        stage=ValidationStage.FAST_PROBE, worker_id=1, worker_generation=1,
        node_json='{"password":"must-not-appear"}', kind=EndpointAttemptKind.PASS,
        dispatch_monotonic_ns=100, portfolio_started_monotonic_ns=200,
        yield_created_monotonic_ns=300,
    )
    domain._attempt_received(result)
    sample = domain._attempt_samples[0]
    assert sample["slice_id"] == 2
    assert sample["dispatch_monotonic_ns"] == 100
    assert sample["transport_started_monotonic_ns"] == 200
    assert sample["yield_created_monotonic_ns"] == 300
    assert sample["received_monotonic_ns"] == 999
    assert "must-not-appear" not in str(sample)
    domain.actor.endpoint_result.assert_called_once_with(result)
    domain._publish_observer_event.assert_called_once_with("attempt", result)
