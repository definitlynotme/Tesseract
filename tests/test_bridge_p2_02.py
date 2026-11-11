"""P2-02 contract/executor tests: substitutes only, no runtime or I/O probes."""

from copy import deepcopy
import json
from pathlib import Path
import random
import runpy
import threading
import time

import pytest

import bridge_commands as commands
import bridge_protocol as protocol
from bridge_jobs import (
    Ack, CancellationReport, JobEventRecorder, JobExecutor, JobFailure,
    RequestReceipt, StopIntent,
)


FIXTURES = Path(__file__).resolve().parents[1] / "bridge_schema/fixtures/v1/valid"
JOBS = [name for name, spec in commands.COMMAND_SPECS.items() if spec.reply == "job"]
REQUESTS = [name for name in commands.PYTHON_COMMANDS if commands.COMMAND_SPECS[name].reply != "job"]


def fixture(name, kind="result"):
    frame = json.loads((FIXTURES / f"{kind}_{name}.json").read_bytes())
    if kind == "command":
        return frame["args"]
    return frame["data"]["result"] if frame["t"] == "evt" else frame["data"]


def register(executor, name, execute=None, **kwargs):
    if name == "service.restart":
        kwargs.setdefault("before", lambda *_: fixture(name)["core_generation"] - 1)
    if name in {"service.stop", "app.quit"}:
        kwargs.setdefault("stop_intent", lambda *_: StopIntent(True))
    if commands.COMMAND_SPECS[name].cancellation in {"session", "diagnostic"}:
        kwargs.setdefault("cancel", lambda *_: CancellationReport(True, True))
    executor.register(name, execute or (lambda *_: fixture(name)), **kwargs)


def submit(executor, name, owner="window-a", *, send=True, args=None):
    receipt = executor.submit(name, fixture(name, "command") if args is None else args,
                              req_id="request-a", owner=owner)
    if send and receipt.accepted and isinstance(receipt, Ack):
        if receipt.intent_receipt:
            assert receipt.wait_stop_intent(1).accepted
        assert executor.acceptance_sent(receipt.op, epoch=executor.epoch)
    return receipt


@pytest.fixture
def executor():
    instance = JobExecutor(JobEventRecorder(8192), epoch="p2-test", require_acceptance=True,
                           normal_workers=2, normal_queue=16)
    yield instance
    instance.shutdown(time.monotonic() + 1)


def drain(executor):
    events = executor.recorder.prepare_for_send(100)
    assert all(not protocol.validate_frame(event) for event in events)
    executor.recorder.ack_sent(events)
    return events


def test_inventory_is_derived_and_shell_excluded(executor):
    assert len(commands.COMMAND_SPECS) == 34
    assert set(commands.COMMAND_SPECS) == set(protocol.V1_COMMAND_SPECS)
    for name, spec in commands.COMMAND_SPECS.items():
        assert spec.contract is protocol.V1_COMMAND_SPECS[name]
        assert spec.args_schema is protocol.V1_COMMAND_SPECS[name][4]
        assert spec.result_schema is protocol.V1_COMMAND_SPECS[name][5]
        if spec.domain == "shell":
            with pytest.raises(ValueError):
                executor.register(name, lambda *_: {})
            assert executor.submit(name, {}).skipped_reason == "not_allowed"
        else:
            register(executor, name)


def test_intent_confirmation_inventory():
    assert commands.INTENT_CONFIRMING_COMMANDS == {"service.stop", "app.quit", "cancel"}
    assert "cancel" not in commands.COMMAND_SPECS  # Independent control frame.
    assert {name for name, spec in commands.COMMAND_SPECS.items()
            if spec.intent_confirming} == {"service.stop", "app.quit"}
    assert all(not commands.COMMAND_SPECS[name].intent_confirming
               for name in JOBS if name not in {"service.stop", "app.quit"})


def test_import_detects_contract_addition(monkeypatch):
    monkeypatch.setitem(protocol.V1_COMMAND_SPECS, "extra.test", protocol.V1_COMMAND_SPECS["service.start"])
    with pytest.raises(ValueError, match="inventory differ"):
        runpy.run_path(str(Path(commands.__file__)))


def test_import_detects_missing_metadata():
    source = Path(commands.__file__).read_bytes().decode("utf-8")
    source = source.replace('COMMAND_SPECS = build_specs()',
                            '_METADATA.pop("nodes.delete")\nCOMMAND_SPECS = build_specs()')
    # Executing under the existing module name keeps dataclass introspection
    # available; this private namespace cannot replace the imported inventory.
    with pytest.raises(ValueError, match="inventory differ"):
        exec(compile(source, commands.__file__, "exec"), {"__name__": commands.__name__})


def test_self_check_missing_job_adapter_and_shell_domain():
    from dataclasses import replace
    table = dict(commands.COMMAND_SPECS)
    table["nodes.delete"] = replace(table["nodes.delete"], adapter=None)
    with pytest.raises(ValueError, match="adapter"):
        commands.validate_specs(table)
    table.pop("nodes.delete")
    with pytest.raises(ValueError, match="inventory"):
        commands.validate_specs(table)


@pytest.mark.parametrize("name", ["nodes.diagnose"])
def test_registration_requires_real_cancel_callback(executor, name):
    with pytest.raises(ValueError, match="cancellation"):
        executor.register(name, lambda *_: {})


@pytest.mark.parametrize("name", ["service.stop", "app.quit", "service.restart"])
def test_registration_requires_control_or_before(executor, name):
    with pytest.raises(ValueError):
        executor.register(name, lambda *_: {})


@pytest.mark.parametrize("name", JOBS)
@pytest.mark.parametrize("variant", ["valid", "missing", "saved_unapplied"])
def test_every_job_result_adapter(executor, name, variant):
    raw = fixture(name)
    if variant == "missing":
        raw.pop(next(iter(raw)))
    elif variant == "saved_unapplied":
        if name == "settings.apply":
            raw["sections"][0]["applied"] = False
        else:
            raw.update(saved=True, applied=False)
    register(executor, name, lambda *_: raw)
    ack = submit(executor, name)
    assert ack.accepted
    result = executor.wait(ack.op, 1)
    assert result["state"] == ("done" if variant == "valid" else "failed")
    if variant == "missing":
        assert result["error"]["code"] == "internal"
    drain(executor)


def test_settings_deferred_and_partial_facts(executor):
    raw = fixture("settings.apply")
    raw["sections"][0].update(applied=False, effect="deferred")
    raw["pending"] = dict(intent_id="apply-1", config_rev=1, generation=1,
                          state="pending", effect="restart", error=None)
    register(executor, "settings.apply", lambda *_: raw)
    ack = submit(executor, "settings.apply")
    assert executor.wait(ack.op, 1)["state"] == "done"
    raw["sections"][0]["error"] = dict(code="io", message_key="errors.io", params={})
    ack = submit(executor, "settings.apply")
    failed = executor.wait(ack.op, 1)
    assert failed["state"] == "failed" and failed["result"]["sections"][0]["saved"]
    drain(executor)


@pytest.mark.parametrize("name", JOBS)
def test_thrown_business_codes_preserve_only_declared_codes(executor, name):
    raw = fixture(name)
    codes = iter(["io" if "io" in commands.COMMAND_SPECS[name].errors else "conflict", "not_a_public_code", "admin_required"])

    def execute(*_):
        raise JobFailure(next(codes), raw)

    register(executor, name, execute)
    for expected in ["io" if "io" in commands.COMMAND_SPECS[name].errors else "conflict", "internal",
                     "admin_required" if "admin_required" in commands.COMMAND_SPECS[name].errors else "internal"]:
        ack = submit(executor, name)
        failed = executor.wait(ack.op, 1)
        assert failed["state"] == "failed" and failed["error"]["code"] == expected
        assert failed["result"] == raw
    drain(executor)


@pytest.mark.parametrize("name", REQUESTS)
def test_once_only_response_modes(executor, name):
    register(executor, name)
    request = submit(executor, name)
    assert isinstance(request, RequestReceipt) and request.accepted
    response = request.wait(1)
    assert response.ok and response.name == name and response.owner == "window-a"
    assert response.outlet == commands.COMMAND_SPECS[name].reply
    assert request.wait(0) is None
    assert executor.get_job(request.op) is None
    assert not executor._terminals and not executor.recorder.pending


@pytest.mark.parametrize("args,expected", [({}, "missing_field"), ({"mode": 3}, "invalid_type"),
                                         ({"mode": "bad"}, "invalid_enum"), ([], "invalid_type"),
                                         ({"mode": "tun", "extra": 1}, "invalid_type")])
def test_schema_parameter_errors(executor, args, expected):
    register(executor, "mode.set")
    ack = submit(executor, "mode.set", args=args)
    assert not ack.accepted and ack.skipped_reason == expected
    assert not executor.recorder.pending


def test_unknown_unregistered_wildcard_and_conditional_args(executor):
    for name in ["unknown", "nodes.*", "nodes.delete", "host.saveFile"]:
        assert executor.submit(name, {}).skipped_reason == "not_allowed"
    register(executor, "import.apply")
    rejected = submit(executor, "import.apply", args={"tier": "gateway", "mode": "paste"})
    assert rejected.skipped_reason == "missing_field"
    register(executor, "settings.apply")
    rejected = submit(executor, "settings.apply", args={"base_rev": True, "changes": []})
    assert rejected.skipped_reason == "missing_field"
    assert executor.submit("settings.apply", {"base_rev": True, "changes": fixture("settings.apply", "command")["changes"]},
                           "req", "owner").skipped_reason == "invalid_type"


def test_receipt_gates_execute_and_snapshot(executor):
    executed, before = threading.Event(), threading.Event()
    register(executor, "service.restart", lambda *_: (executed.set(), fixture("service.restart"))[-1],
             before=lambda *_: (before.set(), 0)[-1])
    ack = submit(executor, "service.restart", send=False)
    assert not executed.wait(.03) and not before.is_set()
    assert not executor.acceptance_sent(ack.op, epoch="old")
    assert executor.acceptance_sent(ack.op, epoch=executor.epoch)
    assert executor.wait(ack.op, 1)["state"] == "done"
    assert executed.is_set() and before.is_set()


def test_receipt_timeout_including_queued_job():
    release = threading.Event()
    entered = threading.Event()
    executed = threading.Event()
    executor = JobExecutor(JobEventRecorder(), require_acceptance=True, acceptance_timeout=.04)
    register(executor, "mode.set", lambda *_: (entered.set(), release.wait(), fixture("mode.set"))[-1])
    register(executor, "nodes.delete", lambda *_: (executed.set(), fixture("nodes.delete"))[-1])
    try:
        running = submit(executor, "mode.set")
        assert entered.wait(1)
        queued = submit(executor, "nodes.delete", send=False)
        assert executor.wait(queued.op, 1)["state"] == "cancelled"
        assert not executor.acceptance_sent(queued.op) and not executed.is_set()
        release.set()
        assert executor.wait(running.op, 1)["state"] == "done"
    finally:
        release.set()
        executor.shutdown(time.monotonic() + 1)
    drain(executor)


def test_three_domains_saturated_stop_remains_available():
    release = threading.Event()
    normal_entered, change_entered = threading.Event(), threading.Event()
    executor = JobExecutor(JobEventRecorder(), require_acceptance=True, normal_workers=1, normal_queue=1)
    register(executor, "geo.update", lambda *_: (normal_entered.set(), release.wait(), fixture("geo.update"))[-1])
    register(executor, "mode.set", lambda *_: (change_entered.set(), release.wait(), fixture("mode.set"))[-1])
    register(executor, "service.stop")
    try:
        normal = submit(executor, "geo.update")
        assert normal_entered.wait(1)
        queued_normal = submit(executor, "geo.update")
        assert submit(executor, "geo.update").skipped_reason == "job_capacity"
        change = submit(executor, "mode.set")
        assert change_entered.wait(1)
        changes = [submit(executor, "mode.set", owner=f"owner-{i}") for i in range(57)]
        assert all(ack.accepted for ack in changes)
        assert executor._active == 60
        assert submit(executor, "mode.set", "overflow").skipped_reason == "job_capacity"
        stop = submit(executor, "service.stop")
        assert stop.wait_stop_intent(1).accepted
        assert executor.wait(stop.op, 1)["state"] == "done"
        release.set()
        assert executor.wait(normal.op, 1)["state"] == "done"
        assert executor.wait(queued_normal.op, 1)["state"] == "done"
        assert executor.wait(change.op, 1)["state"] == "done"
        assert all(executor.wait(ack.op, 1)["state"] == "cancelled" for ack in changes)
    finally:
        release.set()
        executor.shutdown(time.monotonic() + 1)
    drain(executor)


def test_two_stop_and_two_quit_reservations():
    release, entered = threading.Event(), threading.Event()
    release_intent = threading.Event()
    executor = JobExecutor(JobEventRecorder(), require_acceptance=True)
    register(executor, "service.start", lambda *_: (entered.set(), release.wait(), fixture("service.start"))[-1])
    register(executor, "service.stop", stop_intent=lambda *_: (release_intent.wait(), StopIntent(True))[-1])
    register(executor, "app.quit")
    try:
        changes = [submit(executor, "service.start", f"owner-{i}") for i in range(60)]
        assert all(ack.accepted for ack in changes) and entered.wait(1)
        # Hold the control worker at its real intent decision so all 64 places
        # are simultaneously observable, independent of completion timing.
        stops = [submit(executor, "service.stop", send=False) for _ in range(2)]
        quits = [submit(executor, "app.quit", send=False) for _ in range(2)]
        assert all(ack.accepted for ack in stops + quits) and executor._active == 64
        release_intent.set()
        for ack in stops + quits:
            assert ack.wait_stop_intent(1).accepted
            assert executor.acceptance_sent(ack.op)
        assert all(executor.wait(ack.op, 1)["state"] == "done" for ack in stops + quits)
        release.set()
    finally:
        release.set()
        release_intent.set()
        executor.shutdown(time.monotonic() + 1)


def test_queued_cancel_does_not_cancel_started_saved_transaction(executor):
    entered, release = threading.Event(), threading.Event()
    raw = fixture("nodes.delete")
    raw.update(applied=False, apply_error=dict(code="io", message_key="errors.io", params={}))
    register(executor, "nodes.delete", lambda *_: (entered.set(), release.wait(), raw)[-1])
    try:
        running = submit(executor, "nodes.delete")
        assert entered.wait(1)
        queued = submit(executor, "nodes.delete")
        assert executor.cancel(queued.op, "window-a").accepted
        assert executor.wait(queued.op, 1)["state"] == "cancelled"
        assert executor.cancel(running.op, "window-a").skipped_reason == "not_cancellable"
        executor.revoke_owner("window-a")
        release.set()
        result = executor.wait(running.op, 1)
        assert result["state"] == "failed" and result["result"] == raw
    finally:
        release.set()
    drain(executor)


def test_supersede_cancel_callback_and_missing_callback(executor):
    entered, release = threading.Event(), threading.Event()
    register(executor, "service.start", lambda *_: (entered.set(), release.wait(), fixture("service.start"))[-1],
             cancel=lambda *_: True)
    register(executor, "service.restart")
    try:
        ack = submit(executor, "service.start")
        assert entered.wait(1)
        cancelled = executor.cancel(ack.op, "window-a")
        assert cancelled.accepted and cancelled.cancel_receipt.reported.wait(1)
        release.set()
        assert executor.wait(ack.op, 1)["state"] == "superseded"
        queued = submit(executor, "service.restart", send=False)
        assert executor.cancel(queued.op, "window-a").accepted
        assert executor.wait(queued.op, 1)["state"] == "cancelled"
        assert not executor.cancel(ack.op, "window-a").accepted
    finally:
        release.set()


@pytest.mark.parametrize("stage", ["queued", "running"])
def test_none_cancellation_is_always_refused(executor, stage):
    entered, release = threading.Event(), threading.Event()
    register(executor, "service.stop", lambda *_: (entered.set(), release.wait(), fixture("service.stop"))[-1])
    try:
        ack = submit(executor, "service.stop", send=stage == "running")
        if stage == "running":
            assert entered.wait(1)
        assert executor.cancel(ack.op, "window-a").skipped_reason == "not_cancellable"
        assert executor.acceptance_sent(ack.op)
        release.set()
        assert executor.wait(ack.op, 1)["state"] == "done"
    finally:
        release.set()


def test_dialog_only_exists_in_shell(executor):
    assert commands.COMMAND_SPECS["host.saveFile"].cancellation == "dialog"
    with pytest.raises(ValueError):
        register(executor, "host.saveFile")
    assert executor.submit("host.saveFile", fixture("host.saveFile", "command")).skipped_reason == "not_allowed"
    assert not executor.cancel("dialog-op", "window-a").accepted


def test_owner_cancel_discards_late_probe_results(executor):
    entered, release, ended = threading.Event(), threading.Event(), threading.Event()
    register(executor, "exit.verify", lambda *_: (entered.set(), release.wait(), ended.set(), fixture("exit.verify"))[-1])
    try:
        ack = submit(executor, "exit.verify")
        assert entered.wait(1)
        assert executor.cancel(ack.op, "different-owner").skipped_reason == "not_allowed"
        assert executor.cancel(ack.op, "window-a").accepted
        assert not ended.is_set()  # Underlying probe is still executing.
        assert executor.wait(ack.op, 1)["state"] == "cancelled"
        release.set()
        assert ended.wait(1)
        assert executor.get_job(ack.op)["result"] is None
        assert [event["data"]["state"] for event in drain(executor)] == ["queued", "running", "cancelled"]
    finally:
        release.set()


@pytest.mark.parametrize("callback_result", [False, True, "event", "complete", "requested_only"])
def test_diagnostic_cancellation_waits_for_real_cleanup(executor, callback_result):
    entered, release, cleanup = threading.Event(), threading.Event(), threading.Event()
    report = CancellationReport(True, False) if callback_result == "requested_only" else CancellationReport(True, cleanup) if callback_result == "event" else (
        CancellationReport(True, True) if callback_result == "complete" else callback_result)
    register(executor, "nodes.diagnose", lambda *_: (entered.set(), release.wait(), fixture("nodes.diagnose"))[-1],
             cancel=lambda *_: report)
    try:
        ack = submit(executor, "nodes.diagnose")
        assert entered.wait(1)
        assert executor.cancel(ack.op, "stranger").skipped_reason == "not_allowed"
        cancelled = executor.cancel(ack.op, "window-a")
        assert cancelled.accepted and cancelled.cancel_receipt.reported.wait(1)
        if callback_result in {"event", "requested_only"}:
            assert cancelled.cancel_receipt.requested is True
            assert not cancelled.cancel_receipt.cleanup_complete.is_set()
            release.set()
            assert executor.wait(ack.op, .03) is None
            if callback_result == "requested_only":
                cancelled.cancel_receipt.cleanup_complete.set()
            else:
                cleanup.set()
            assert executor.wait(ack.op, 1)["state"] == "cancelled"
            assert cancelled.cancel_receipt.cleanup_complete.is_set()
        else:
            release.set()
            state = executor.wait(ack.op, 1)["state"]
            assert state == ("cancelled" if callback_result == "complete" else "done")
    finally:
        release.set()
        cleanup.set()
        if callback_result == "requested_only":
            cancelled.cancel_receipt.cleanup_complete.set()
    drain(executor)


@pytest.mark.parametrize("when", ["running", "completed"])
def test_private_revocation_and_once_only_owner_delivery(executor, when):
    entered, release = threading.Event(), threading.Event()
    name = "nodes.reveal"
    register(executor, name, lambda *_: (entered.set(), release.wait(), fixture(name))[-1])
    try:
        request = submit(executor, name)
        assert entered.wait(1)
        if when == "completed":
            release.set()
            assert request.finished.wait(1)
        assert executor.take_response(request.op, "wrong-owner") is None
        executor.revoke_owner("window-a")
        assert request.wait(0) is None
        release.set()
        assert executor.get_job(request.op) is None
        assert not executor._requests and not executor._jobs and not executor.recorder.pending
        assert not submit(executor, name).accepted
    finally:
        release.set()


def test_owner_quota_includes_jobs_and_private_responses(executor):
    release, entered = threading.Event(), threading.Event()
    register(executor, "mode.set", lambda *_: (entered.set(), release.wait(), fixture("mode.set"))[-1])
    register(executor, "nodes.reveal")
    register(executor, "service.stop")
    try:
        jobs = [submit(executor, "mode.set") for _ in range(executor.owner_limit - 1)]
        assert all(ack.accepted for ack in jobs) and entered.wait(1)
        private = submit(executor, "nodes.reveal")
        assert private.accepted and private.finished.wait(1)
        assert submit(executor, "mode.set").skipped_reason == "job_capacity"
        assert submit(executor, "nodes.reveal").skipped_reason == "job_capacity"
        stop = submit(executor, "service.stop")
        assert executor.wait(stop.op, 1)["state"] == "done"
        assert private.wait(0).ok
        release.set()
    finally:
        release.set()


@pytest.mark.parametrize("action", ["begin_shutdown", "prepare_quit", "epoch"])
def test_shutdown_waiters_and_persisted_facts(executor, action):
    release = threading.Event()
    normal, transaction = threading.Event(), threading.Event()
    probe = threading.Event()
    register(executor, "mode.set", lambda *_: (transaction.set(), release.wait(), fixture("mode.set"))[-1])
    register(executor, "nodes.reveal", lambda *_: (normal.set(), release.wait(), fixture("nodes.reveal"))[-1])
    register(executor, "exit.verify", lambda *_: (probe.set(), release.wait(), fixture("exit.verify"))[-1])
    next_executor = None
    try:
        saved = submit(executor, "mode.set")
        private = submit(executor, "nodes.reveal")
        owner = submit(executor, "exit.verify")
        assert transaction.wait(1) and normal.wait(1) and probe.wait(1)
        unreceived = submit(executor, "mode.set", "other", send=False)
        if action == "epoch":
            next_executor = executor.advance_epoch("next-epoch", JobEventRecorder())
            assert next_executor.epoch == "next-epoch" and next_executor._seq == 0
        else:
            getattr(executor, action)()
        assert executor.wait(unreceived.op, 1)["state"] == "cancelled"
        assert executor.wait(owner.op, 1)["error"]["code"] == "runtime_gone"
        assert private.wait(1).error["code"] == "runtime_gone"
        assert executor.wait(saved.op, .03) is None
        release.set()
        assert executor.wait(saved.op, 1)["result"]["saved"] is True
        assert all(event["epoch"] == executor.epoch for event in drain(executor))
    finally:
        release.set()
        if next_executor:
            next_executor.shutdown(time.monotonic() + 1)


def test_terminal_retention_and_reservation():
    now = [0.0]
    executor = JobExecutor(JobEventRecorder(), require_acceptance=True,
                           terminal_capacity=261, monotonic=lambda: now[0])
    register(executor, "mode.set")
    try:
        operations = []
        for _ in range(257):
            ack = submit(executor, "mode.set")
            assert ack.accepted and executor.wait(ack.op, 1)["state"] == "done"
            operations.append(ack.op)
            drain(executor)
        assert submit(executor, "mode.set").skipped_reason == "job_capacity"
        assert executor.get_job(operations[0]) is not None
        now[0] = 601
        ack = submit(executor, "mode.set")
        assert executor.wait(ack.op, 1)["state"] == "done"
        assert executor.get_job(operations[0]) is None
        assert executor.get_job(operations[1]) is None
        assert executor.get_job(operations[2]) is not None
    finally:
        executor.begin_shutdown()


def test_light_concurrent_submit_cancel_seq_and_state_transitions(executor):
    register(executor, "nodes.delete")
    register(executor, "exit.verify")
    accepted = []
    lock = threading.Lock()
    barrier = threading.Barrier(4)

    def produce(index):
        rng = random.Random(index)
        barrier.wait()
        for attempt in range(40):
            name = rng.choice(["nodes.delete", "exit.verify"])
            owner = f"owner-{index}-{attempt}"
            ack = submit(executor, name, owner, send=False)
            if ack.accepted:
                with lock:
                    accepted.append(ack)
                if rng.randrange(2):
                    executor.cancel(ack.op, owner)
                else:
                    executor.acceptance_sent(ack.op)

    workers = [threading.Thread(target=produce, args=(i,)) for i in range(4)]
    began = time.monotonic()
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(2)
        assert not worker.is_alive()
    for ack in accepted:
        assert executor.wait(ack.op, 1)["state"] in {"done", "cancelled"}
    assert time.monotonic() - began < 3
    events = drain(executor)
    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
    states = {}
    for event in events:
        states.setdefault(event["data"]["op"], []).append(event["data"]["state"])
    assert len(states) == len(accepted)
    assert all(path in (["queued", "cancelled"], ["queued", "running", "done"],
                        ["queued", "running", "cancelled"]) for path in states.values())


@pytest.mark.parametrize("generations", [1, 2])
def test_mutation_transactions_remain_serial_across_epochs(executor, generations):
    entered, release, next_entered = [threading.Event() for _ in range(3)]
    calls = []

    def execute(*_):
        calls.append(len(calls))
        if len(calls) == 1:
            entered.set()
            release.wait()
        else:
            next_entered.set()
        return fixture("mode.set")

    register(executor, "mode.set", execute)
    register(executor, "service.stop")
    successors = []
    try:
        old = submit(executor, "mode.set")
        assert entered.wait(1)
        current = executor
        for index in range(generations):
            current = current.advance_epoch(f"next-{index}", JobEventRecorder())
            successors.append(current)
        new = submit(current, "mode.set")
        assert not next_entered.wait(.05)
        assert current.get_job(new.op)["state"] == "queued"
        assert executor.get_job(old.op)["state"] == "running"
        # A waiting mutation must not occupy the independent control domain.
        stop = submit(current, "service.stop")
        assert current.wait(stop.op, 1)["state"] == "done"
        assert current.wait(new.op, 1)["state"] == "cancelled"
        new = submit(current, "mode.set")
        assert not next_entered.wait(.05)
        release.set()
        assert executor.wait(old.op, 1)["result"]["saved"] is True
        assert current.wait(new.op, 1)["state"] == "done"
        assert calls == [0, 1]
        for instance in [executor] + successors:
            if instance.recorder.pending:
                assert all(event["epoch"] == instance.epoch for event in drain(instance))
    finally:
        release.set()
        for instance in successors:
            instance.shutdown(time.monotonic() + 1)


@pytest.mark.parametrize("evidence", ["event", "boolean", "context"])
def test_diagnostic_cleanup_completes_while_business_return_is_blocked(executor, evidence):
    entered, release, ended, cleanup = [threading.Event() for _ in range(4)]

    def execute(*_):
        entered.set()
        release.wait()
        ended.set()
        return fixture("nodes.diagnose")

    def cancel(_args, context):
        if evidence == "context":
            context.cancel_receipt.cleanup_complete.set()
        return CancellationReport(True, cleanup if evidence == "event" else evidence == "boolean")

    register(executor, "nodes.diagnose", execute, cancel=cancel)
    try:
        ack = submit(executor, "nodes.diagnose")
        assert entered.wait(1)
        cancellation = executor.cancel(ack.op, "window-a")
        assert cancellation.cancel_receipt.reported.wait(1)
        if evidence == "event":
            assert executor.wait(ack.op, .03) is None
            cleanup.set()
        assert executor.wait(ack.op, 1)["state"] == "cancelled"
        assert cancellation.cancel_receipt.cleanup_complete.is_set()
        assert not ended.is_set()
        before = drain(executor)
        release.set()
        assert executor.shutdown(time.monotonic() + 1)
        assert ended.is_set() and executor.recorder.pending == 0
        assert [event["data"]["state"] for event in before] == ["queued", "running", "cancelled"]
    finally:
        release.set()
        cleanup.set()


def test_new_epoch_drops_completed_unconsumed_private_response(executor):
    register(executor, "nodes.reveal")
    receipt = submit(executor, "nodes.reveal")
    assert receipt.finished.wait(1)
    successor = executor.advance_epoch("replacement", JobEventRecorder())
    try:
        response = receipt.wait(0)
        assert not response.ok and response.error["code"] == "runtime_gone"
        assert response.data is None and response.epoch == executor.epoch
    finally:
        successor.shutdown(time.monotonic() + 1)


def test_revoking_completed_nonprivate_requests_frees_capacity():
    executor = JobExecutor(JobEventRecorder(), request_capacity=2)
    register(executor, "ui.presence")
    try:
        for owner in ["a", "b"]:
            request = submit(executor, "ui.presence", owner)
            assert request.finished.wait(1)
            executor.revoke_owner(owner)
            assert request.wait(0) is None
        assert not executor._requests
        assert submit(executor, "ui.presence", "c").wait(1).ok
    finally:
        executor.shutdown(time.monotonic() + 1)


def test_shutdown_does_not_claim_started_normal_transaction_finished(executor):
    entered, release = threading.Event(), threading.Event()
    register(executor, "logs.exportBundle", lambda *_: (entered.set(), release.wait(), fixture("logs.exportBundle"))[-1])
    try:
        ack = submit(executor, "logs.exportBundle")
        assert entered.wait(1)
        assert executor.shutdown(time.monotonic() + .1) is False
        assert executor.get_job(ack.op)["state"] == "running"
        release.set()
        assert executor.wait(ack.op, 1)["state"] == "done"
        assert executor.shutdown(time.monotonic() + 1)
    finally:
        release.set()


@pytest.mark.parametrize("control", ["service.stop", "app.quit"])
def test_session_end_gets_independent_emergency_execution(executor, control):
    entered, release = threading.Event(), threading.Event()
    register(executor, control, lambda *_: (entered.set(), release.wait(), fixture(control))[-1])
    register(executor, "app.sessionEnd")
    try:
        slow = submit(executor, control)
        assert entered.wait(1)
        emergency = submit(executor, "app.sessionEnd", "shell-instance")
        assert emergency.accepted and emergency.wait(.1).ok
        release.set()
        assert executor.wait(slow.op, 1)["state"] == "done"
    finally:
        release.set()


def test_rejected_stop_intent_keeps_sibling_stop_marker(executor):
    first_entered, first_release, second_entered, second_release = [threading.Event() for _ in range(4)]
    count = [0]

    def intent(*_):
        count[0] += 1
        if count[0] == 1:
            first_entered.set()
            first_release.wait()
            return StopIntent(False, "conflict")
        second_entered.set()
        second_release.wait()
        return StopIntent(True)

    register(executor, "service.stop", stop_intent=intent)
    register(executor, "service.start")
    try:
        first = submit(executor, "service.stop", send=False)
        assert first_entered.wait(1)
        second = submit(executor, "service.stop", send=False)
        first_release.set()
        assert not first.wait_stop_intent(1).accepted and second_entered.wait(1)
        assert submit(executor, "service.start").skipped_reason == "lifecycle_busy"
        second_release.set()
        assert executor.acceptance_sent(second.op)
        assert executor.wait(second.op, 1)["state"] == "done"
    finally:
        first_release.set()
        second_release.set()


@pytest.mark.parametrize("name", REQUESTS)
def test_bad_response_fails_once_without_jobs(executor, name):
    register(executor, name, lambda *_: {})
    receipt = submit(executor, name)
    response = receipt.wait(1)
    assert response.ok is False and response.error["code"] == "internal"
    assert response.data is None and receipt.wait(0) is None
    assert not executor._jobs and not executor.recorder.pending


def test_p2_minimum_terminal_capacity_prevents_retention_dead_end():
    with pytest.raises(ValueError, match="terminal capacity"):
        JobExecutor(JobEventRecorder(), require_acceptance=True, terminal_capacity=260)
    legacy = JobExecutor(JobEventRecorder(), terminal_capacity=259)
    try:
        with pytest.raises(ValueError, match="terminal capacity"):
            register(legacy, "nodes.delete")
    finally:
        legacy.shutdown(time.monotonic() + 1)


@pytest.mark.parametrize("name", ["service.stop", "app.quit"])
@pytest.mark.parametrize("loss", ["timeout", "epoch", "disconnect"])
@pytest.mark.parametrize("stage", ["acquiring_intent", "executing"])
@pytest.mark.parametrize("outcome", ["done", "failed"])
def test_intent_confirmation_ack_loss_preserves_true_terminal(name, loss, stage, outcome):
    """No ack is sent; disconnect uses the executor's pipe-close lifecycle."""
    now = [0.0]
    entered, release, executed, finish = [threading.Event() for _ in range(4)]
    effects = []
    raw = fixture(name)
    if outcome == "failed":
        raw.update(startup_phase="stopping", cleanup_pending=True)
    executor = JobExecutor(JobEventRecorder(), epoch="old-epoch", require_acceptance=True,
                           acceptance_timeout=1, monotonic=lambda: now[0])

    def intent(*_):
        entered.set()
        release.wait()
        effects.append("real_stop_intent")
        return StopIntent(True)

    def execute(*_):
        executed.set()
        finish.wait()
        effects.append("cleanup")
        return raw

    register(executor, name, execute, stop_intent=intent)
    successor = None
    try:
        receipt = submit(executor, name, send=False)
        assert entered.wait(1)
        if stage == "executing":
            release.set()
            assert receipt.wait_stop_intent(1).accepted and executed.wait(1)
        if loss == "timeout":
            now[0] = 2
        elif loss == "epoch":
            successor = executor.advance_epoch("new-epoch", JobEventRecorder())
        else:
            executor.begin_shutdown()
        assert executor.wait(receipt.op, .02) is None
        release.set()
        assert receipt.wait_stop_intent(1).accepted and executed.wait(1)
        finish.set()
        terminal = executor.wait(receipt.op, 1)
        assert terminal["state"] == outcome and terminal["result"] == raw
        if outcome == "failed":
            assert terminal["error"]["code"] == "timeout"
        assert effects == ["real_stop_intent", "cleanup"]
        events = drain(executor)
        assert [event["data"]["state"] for event in events] == ["queued", "running", outcome]
        assert [event["seq"] for event in events] == [1, 2, 3]
        assert all(event["epoch"] == "old-epoch" for event in events)
        assert executor.get_job(receipt.op) == terminal  # Retained after pipe close.
        assert not executor.acceptance_sent(receipt.op, epoch="wrong-epoch")
        # A late delivery receipt only records delivery, including after done.
        assert executor.acceptance_sent(receipt.op, epoch="old-epoch")
        assert executor.get_job(receipt.op) == terminal and not executor.recorder.pending
    finally:
        release.set()
        finish.set()
        # The injected clock deliberately does not advance during shutdown.
        executor.begin_shutdown()
        if successor:
            successor.begin_shutdown()


@pytest.mark.parametrize("name", ["service.restart", "mode.set", "geo.update"])
@pytest.mark.parametrize("loss", ["timeout", "epoch", "disconnect"])
def test_absolute_barrier_ack_loss_has_no_business_callbacks(name, loss):
    now = [0.0]
    effects = []
    executor = JobExecutor(JobEventRecorder(), require_acceptance=True,
                           acceptance_timeout=1, monotonic=lambda: now[0])
    kwargs = {"before": lambda *_: (effects.append("snapshot"), 0)[-1]} if name == "service.restart" else {}
    register(executor, name, lambda *_: (effects.append("execute"), fixture(name))[-1], **kwargs)
    successor = None
    try:
        receipt = submit(executor, name, send=False)
        assert executor.wait(receipt.op, .02) is None and effects == []
        if loss == "timeout":
            now[0] = 2
        elif loss == "epoch":
            successor = executor.advance_epoch("new-epoch", JobEventRecorder())
        else:
            executor.begin_shutdown()
        assert executor.wait(receipt.op, 1)["state"] == "cancelled"
        assert not executor.acceptance_sent(receipt.op) and effects == []
        assert [event["data"]["state"] for event in drain(executor)] == ["queued", "cancelled"]
    finally:
        executor.begin_shutdown()
        if successor:
            successor.begin_shutdown()


@pytest.mark.parametrize("loss", ["timeout", "epoch"])
def test_absolute_barrier_loss_while_waiting_cross_epoch_mutation_lock(loss):
    now = [0.0]
    entered, release = threading.Event(), threading.Event()
    effects = []
    executor = JobExecutor(JobEventRecorder(), require_acceptance=True,
                           acceptance_timeout=1, monotonic=lambda: now[0])

    def execute(*_):
        effects.append("execute")
        entered.set()
        release.wait()
        return fixture("mode.set")

    register(executor, "mode.set", execute)
    successors = []
    try:
        old = submit(executor, "mode.set")
        assert entered.wait(1)
        current = executor.advance_epoch("second", JobEventRecorder())
        successors.append(current)
        queued = submit(current, "mode.set", send=False)
        assert current._mutation_lock is executor._mutation_lock
        assert current.wait(queued.op, .02) is None and effects == ["execute"]
        if loss == "timeout":
            now[0] = 2
        else:
            current = current.advance_epoch("third", JobEventRecorder())
            successors.append(current)
        assert successors[0].wait(queued.op, 1)["state"] == "cancelled"
        assert not successors[0].acceptance_sent(queued.op)
        release.set()
        assert executor.wait(old.op, 1)["result"]["saved"] is True
        # Only this newly acknowledged mutation may execute after the old one.
        new = submit(current, "mode.set")
        assert current.wait(new.op, 1)["state"] == "done"
        assert effects == ["execute", "execute"]
        for instance in [executor] + successors:
            events = drain(instance)
            assert all(event["epoch"] == instance.epoch for event in events)
            assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
    finally:
        release.set()
        for instance in [executor] + successors:
            instance.begin_shutdown()


@pytest.mark.parametrize("mode", ["queued", "supersede", "owner", "diagnostic"])
def test_cancel_intent_needs_no_ack_and_target_keeps_own_semantics(executor, mode):
    name = {"queued": "nodes.delete", "supersede": "service.start",
            "owner": "exit.verify", "diagnostic": "nodes.diagnose"}[mode]
    entered, release = threading.Event(), threading.Event()
    kwargs = {"cancel": lambda *_: True} if mode == "supersede" else {}
    register(executor, name, lambda *_: (entered.set(), release.wait(), fixture(name))[-1], **kwargs)
    try:
        receipt = submit(executor, name, send=mode != "queued")
        if mode != "queued":
            assert entered.wait(1)
        assert executor.cancel(receipt.op, "other-owner").skipped_reason == "not_allowed"
        cancellation = executor.cancel(receipt.op, "window-a")
        assert cancellation.accepted  # Intentionally never acknowledge this cancel.
        if cancellation.cancel_receipt:
            assert cancellation.cancel_receipt.reported.wait(1)
        release.set()
        terminal = executor.wait(receipt.op, 1)
        assert terminal["state"] == ("superseded" if mode == "supersede" else "cancelled")
        if mode == "queued":
            assert not entered.is_set()
        assert terminal["result"] is None
        drain(executor)
    finally:
        release.set()
