"""Legacy post-dial judgements and a verified provider write (review F7, 11-02).

With ``tier_ladder_judge`` off, the judge reads the verified Tier3B provider
write's busy flag as the tier ladder's ``d4_txn_busy`` check does: a trigger
is deferred while a write runs, and a judgement admitted just before the flag
went up stands down (indeterminate) instead of probing members the write is
rebuilding.  The ladder's other D4 checks stay ladder-only, so nothing changes
while the flag is down (it is raised on the verified lane alone).  Runs over
the T0 driver's fake controller; no Mihomo, socket or profile is touched.
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from runtime_node_judge import ProbeClass, StartProbe
from runtime_post_dial_judge import GATE_RECHECK_S

ROOT = Path(__file__).resolve().parents[1]
BUSY = "d4_txn_busy_provider_write"


def _load_driver() -> Any:
    name = "ladder_t0_driver_for_legacy_busy"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / "tests" / "fixtures" / "ladder_t0" / "t0_driver.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


drv = _load_driver()


@pytest.fixture
def make(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("TESSERACT_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TESSERACT_DATA_DIR", str(tmp_path / "data"))
    ctx = drv.Ctx(ROOT, {}, tmp_path, family="legacy-busy")
    made: list[Any] = []

    def build() -> Any:
        # The legacy judge: the 10-28 ladder/rearm/overshoot keys pinned off.
        harness = drv.Harness(ctx, "t3", flags={
            "tier_ladder_judge": False, "rearm_on_stale_route": False, "overshoot_repair": False})
        made.append(harness)
        return harness

    yield build
    for harness in made:
        harness.runtime._tier3_provider_write_busy = False
        harness.close()


def admit(harness: Any, leaf: str, *, register: bool = True) -> Any:
    runtime = harness.runtime
    return runtime._runtime_post_dial_admission(
        harness.trigger_obj(leaf),
        runtime._runtime_post_dial_judge(),
        now=runtime._runtime_recovery_now(),
        register=register,
    )


def observer_lines(harness: Any, ev: str) -> list[str]:
    prefix = f"Post-dial observer {ev} ("
    return [record.getMessage() for record in list(harness.logs.records)
            if record.getMessage().startswith(prefix)]


def post_dial_tickets(runtime: Any) -> set[str]:
    return {key for key in dict(getattr(runtime, "_runtime_recovery_tickets", {}) or {})
            if str(key).startswith("post-dial-")}


def test_a_legacy_admission_defers_while_a_verified_provider_write_runs(make: Any) -> None:
    harness = make()
    runtime = harness.runtime
    before = post_dial_tickets(runtime)
    runtime._tier3_provider_write_busy = True
    assert admit(harness, "n1") == (None, BUSY)
    assert admit(harness, "n1", register=False) == (None, BUSY)  # the shadow read, too
    assert post_dial_tickets(runtime) == before  # deferred before any ticket

    runtime._tier3_provider_write_busy = False
    job, reason = admit(harness, "n1")
    assert reason is None and job is not None and job.ladder is None
    runtime._runtime_post_dial_finish(job, "test_done")


def test_the_ladders_other_d4_transactions_stay_ladder_only(make: Any) -> None:
    harness = make()
    runtime = harness.runtime
    with runtime._runtime_failure_monitor_lock:
        runtime._runtime_passive_failback_requests = {"x": {"running": True}}
    try:
        assert runtime._runtime_post_dial_d4_txn_busy() == "d4_txn_busy:passive_failback"
        job, reason = admit(harness, "n1")
        assert reason is None and job is not None  # unchanged with the flag down
        runtime._runtime_post_dial_finish(job, "test_done")
    finally:
        with runtime._runtime_failure_monitor_lock:
            runtime._runtime_passive_failback_requests = {}


def test_a_trigger_during_the_write_is_gated_and_judged_after_it(make: Any) -> None:
    harness = make()
    runtime = harness.runtime
    runtime._tier3_provider_write_busy = True
    harness.trigger("n1", episode=1)
    assert harness.quiesce(10.0)
    assert not harness.judge.judging()
    assert harness.controller.delay_calls == []
    gated = observer_lines(harness, "gated")
    assert len(gated) == 1 and f"reason={BUSY}" in gated[0], gated

    runtime._tier3_provider_write_busy = False
    time.sleep(GATE_RECHECK_S + 0.1)  # past the cached gate answer
    harness.trigger("n1", episode=1, new_episode=False)  # the same episode, evaluated again
    assert harness.quiesce(20.0)
    assert [call["name"] for call in harness.controller.delay_calls][:1] == ["n1"]
    assert len(observer_lines(harness, "verdict")) == 1


def test_a_judgement_admitted_just_before_the_flag_stands_down(make: Any) -> None:
    harness = make()
    runtime = harness.runtime
    admission = runtime._runtime_post_dial_admission

    def admitted_then_write_starts(*args: Any, **kwargs: Any) -> Any:
        result = admission(*args, **kwargs)
        # The write raises its flag between this admission and judge.start:
        # its own judge check then saw no judgement yet.
        runtime._tier3_provider_write_busy = True
        return result

    runtime._runtime_post_dial_admission = admitted_then_write_starts
    # n1 is dead and n2 alive: an unguarded judgement would convict n1 and
    # commit while the write rebuilds the members.
    harness.controller.modes["n1"] = "zombie"
    harness.trigger("n1")
    assert harness.quiesce(20.0)

    assert harness.controller.delay_calls == []  # nothing probed while the write ran
    assert harness.controller.writes == []
    refused = observer_lines(harness, "probe_refused")
    assert refused and all(line.endswith(f"reason={BUSY})") for line in refused), refused
    assert "name=A_C " in refused[0]
    (verdict,) = observer_lines(harness, "verdict")
    assert "verdict=INDETERMINATE" in verdict and "reason=accused_indeterminate" in verdict, verdict
    assert observer_lines(harness, "commit") == []
    # The bookkeeping of any indeterminate judgement: the leaf back-off.
    assert harness.judge.blocked_cheap("n1", runtime._runtime_recovery_now()) == "leaf_backoff"


def test_the_legacy_probe_hook_refuses_only_while_the_flag_is_up(make: Any) -> None:
    harness = make()
    runtime = harness.runtime
    job, _reason = admit(harness, "n1")
    broker: list[Any] = []
    delay: list[Any] = []

    def run_brokered(callback: Any, **kwargs: Any) -> Any:
        broker.append(kwargs)
        return callback(None)

    def leased(name: str, urls: list[str], **kwargs: Any) -> Any:
        delay.append((name, list(urls), kwargs.get("expected_status"), kwargs.get("timeout_ms")))
        return {"delay": 1}

    runtime._run_brokered_runtime_probe = run_brokered
    runtime._probe_proxy_delay_with_urls_leased = leased
    runtime._tier3_provider_write_busy = True
    refused = runtime._runtime_post_dial_contract_probe(job, StartProbe("A_C", "n1", 5000, 5.65))
    assert refused.probe is ProbeClass.INDETERMINATE and broker == [] and delay == []
    assert observer_lines(harness, "probe_refused")[-1].endswith(f"name=A_C reason={BUSY})")

    runtime._tier3_provider_write_busy = False
    probed = runtime._runtime_post_dial_contract_probe(job, StartProbe("A_C", "n1", 5000, 5.65))
    assert probed.probe is ProbeClass.PASS
    assert delay == [("n1", [job.test_url], job.expected_status, 5000)]
    runtime._runtime_post_dial_finish(job, "test_done")
