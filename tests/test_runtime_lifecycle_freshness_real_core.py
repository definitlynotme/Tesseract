"""Service-state freshness after READY on the bundled Mihomo (11-01).

In the user's 11-01 08:03 run the new UI froze about one second after READY:
lifecycle_status() reported freshness.stale until the stop, so the real
BridgeTiers sampler invalidated bastion.pool (and network.mode and
service.collect) on every tick. A re-read of the same Core-loaded route had
stamped the published snapshot with a newer loaded_at than the READY
publication, and the early profile never re-publishes readiness.

Here a same-route re-read and an online-growth provider commit (selection
unchanged) must leave the sample fresh, with bastion.pool still publishing the
Manager's count. Each step waits longer than one 15.6 ms Windows monotonic
tick; a read inside the same tick compares equal and would hide the latch.

Without that time clause, the readback's ownership-change epoch bump is what
keeps an older Core's READY off a new Core (T-P1-10); it is driven here too.
"""

from __future__ import annotations

import time
from contextlib import ExitStack

import pytest

from bridge_runtime_access import RuntimeAccess
from bridge_tiers import SampledTopics
from desktop_runtime import ProviderSyncDisposition
from runtime_egress_recovery import RuntimeEgressReadiness, RuntimeEgressReadinessState
from tier3_pool_view import (
    Tier3AutoPoolCountSnapshot,
    Tier3StartupGateProgress,
    Tier3UserPoolHealthSnapshot,
)
from tests.test_runtime_online_growth_real_core import _ServingCore, _skip_without_core

TICK_GAP = 0.05


class _RecordingState:
    """Bridge publication state as BridgeTiers drives it, recorded."""

    def __init__(self):
        self.events = []

    def record_state(self, topic, dto, **_kwargs):
        self.events.append(('state', topic, dict(dto)))

    def record_edge(self, topic, dto):
        self.events.append(('edge', topic, dict(dto)))
        return 0

    def invalidate_state(self, topic, *, notify=False):
        self.events.append(('invalidate', topic, None))

    def snapshot(self, topic):
        raise ValueError('not subscribed')


def _serving(core, pool):
    """Give the harness runtime what lifecycle_status and bastion.pool read."""

    runtime = core.runtime
    pid = runtime.core_runner.status()['pid']
    runtime.core_runner.lightweight_fact = lambda: {
        'running': True, 'pid': pid, 'process_generation': 1, 'cleanup_pending': False}
    manager = runtime.manager
    manager.config = runtime.config
    manager.background_actions = {}
    manager.tier3_auto_pool_count_snapshot = lambda: Tier3AutoPoolCountSnapshot(
        revision=pool['healthy'], healthy=pool['healthy'], total=pool['healthy'])
    manager.tier3_user_pool_health_snapshot = lambda: Tier3UserPoolHealthSnapshot(
        revision=0, healthy=0, total=0)
    manager.tier3_startup_gate_progress = lambda: Tier3StartupGateProgress(
        healthy=pool['healthy'], required=3)
    runtime.running = True
    runtime._desired_running = True
    return runtime


def _publish_ready(runtime):
    """Install READY at the current epochs, as the activation publication does."""

    with runtime._runtime_route_snapshot_lock:
        route_epoch = int(runtime._runtime_route_epoch)
        network_epoch = int(runtime._runtime_network_context_epoch)
    readiness = RuntimeEgressReadiness(
        state=RuntimeEgressReadinessState.READY, validation_active=False,
        lifecycle_revision=int(runtime._lifecycle_revision), route_epoch=route_epoch,
        network_context_epoch=network_epoch, updated_at_monotonic=time.monotonic())
    with runtime._tier3_egress_activation_lock:
        runtime._runtime_egress_readiness = readiness
        runtime._runtime_egress_readiness_publication_revision = int(
            runtime._runtime_egress_readiness_publication_revision or 0) + 1


def _quiet(action):
    # The real SamplingWorker swallows getter failures the same way; topics
    # this harness does not model must not stop the bastion.pool projection.
    try:
        return action()
    except Exception:
        return None


def test_same_route_reads_and_growth_commits_keep_the_service_state_fresh(tmp_path):
    _skip_without_core()
    with ExitStack() as stack:
        core = _ServingCore(stack, tmp_path)
        pool = {'healthy': 3}
        runtime = _serving(core, pool)
        state = _RecordingState()
        topics = SampledTopics(state, RuntimeAccess(runtime),
                               submit_normal=lambda _work: False, config_revision=lambda: 0)

        def sample(healthy):
            pool['healthy'] = healthy
            time.sleep(TICK_GAP)
            first = len(state.events)
            topics.sample(lambda _name, action: _quiet(action))
            status = runtime.lifecycle_status()
            published = [(kind, (dto or {}).get('runtime_alive'))
                         for kind, topic, dto in state.events[first:] if topic == 'bastion.pool']
            return status, published

        _publish_ready(runtime)
        route_epoch = runtime._runtime_route_epoch
        status, published = sample(3)
        assert not status['freshness']['stale']
        assert status['runtime']['startup_phase'] == 'running'
        assert published == [('state', 3)]

        # The ordinary same-route re-read that latched the 11-01 run.
        time.sleep(TICK_GAP)
        assert runtime._refresh_runtime_route_snapshot(timeout_seconds=2.0) is not None
        assert runtime._runtime_route_epoch == route_epoch
        status, published = sample(4)
        assert not status['freshness']['stale'], status['freshness']
        assert status['runtime']['startup_phase'] == 'running'
        assert published == [('edge', 4)]

        # An online-growth commit keeps the selection and the epoch.
        outcome = core.sync()
        assert outcome.disposition is ProviderSyncDisposition.COMMITTED, (outcome, core.lines[-30:])
        assert runtime._runtime_route_epoch == route_epoch
        status, published = sample(5)
        assert not status['freshness']['stale'], status['freshness']
        assert status['runtime']['startup_phase'] == 'running'
        assert published == [('edge', 5)]


@pytest.mark.parametrize('shape', ['stop_first', 'exited'])
def test_an_older_core_ready_is_stale_after_the_new_core_readback(tmp_path, shape):
    # The health-restart paths stop the runner without revoking provider
    # authority: no epoch bump, no FAIL_CLOSED. Phase "stopped" clears the
    # snapshot (stop_first); a Core that died leaves the old one (exited). The
    # new Core's first readback must then advance the epoch on the ownership
    # change alone, the FINAL-EXIT selection being the same. The same Mihomo
    # process stands in for the new Core.
    _skip_without_core()
    with ExitStack() as stack:
        core = _ServingCore(stack, tmp_path)
        runtime = _serving(core, {'healthy': 3})
        _publish_ready(runtime)
        assert not runtime.lifecycle_status()['freshness']['stale']
        route_epoch = runtime._runtime_route_epoch
        selection = runtime._runtime_route_selection_signature
        if shape == 'stop_first':
            runtime._set_core_runtime_phase('stopped')
            assert runtime._runtime_route_snapshot is None
        assert runtime._runtime_route_epoch == route_epoch

        pid = runtime.core_runner.status()['pid']
        runtime.core_runner.status = lambda: {'pid': pid, 'process_generation': 2}
        runtime.core_runner.lightweight_fact = lambda: {
            'running': True, 'pid': pid, 'process_generation': 2, 'cleanup_pending': False}
        with runtime._core_lifecycle_lock:
            runtime._core_generation += 1
            runtime._core_runtime_phase = 'formal_no_capture'
        assert runtime.lifecycle_status()['freshness']['stale']
        time.sleep(TICK_GAP)
        assert runtime._refresh_runtime_route_snapshot(timeout_seconds=2.0) is not None
        owner = runtime._runtime_route_snapshot
        assert (owner.process_generation, owner.core_generation) == (2, runtime._core_generation)
        assert runtime._runtime_route_selection_signature == selection
        assert runtime._runtime_route_epoch == route_epoch + 1
        status = runtime.lifecycle_status()
        assert status['freshness']['stale'], status['freshness']
        assert status['runtime']['startup_phase'] != 'running'


# Decision D (11-04): after READY the early profile never re-publishes
# readiness, so the first change of the serving node used to leave the sample
# stale until stop.  _maybe_carry_runtime_ready carries the same READY to the
# new epoch on the self-check's own verdict, and must never test a node: the
# user's condition, checked here against the real Mihomo record.


class _CallRecorder:
    """The runtime's controller client, recording every call made through it."""

    def __init__(self, inner, calls):
        self._inner, self._calls = inner, calls

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr

        def call(*args, **kwargs):
            self._calls.append(name)
            return attr(*args, **kwargs)
        return call


def _anchored_ready(runtime):
    ready = runtime._publish_runtime_egress_readiness(
        RuntimeEgressReadinessState.READY, validation_active=False, reason='verified')
    assert ready.route_owner, 'the publisher stamps the Core owner'
    status = runtime.lifecycle_status()
    assert not status['freshness']['stale'] and status['runtime']['startup_phase'] == 'running'
    return ready


def _self_check(core, name):
    """What the group's own health check writes for one member."""

    try:
        core.client.proxy_delay(name, url=core.url, timeout_ms=2000, expected_status=204,
                                verify_expected_status=True)
    except Exception:
        pass


def _read(core, name, seconds=8.0):
    """One controller entry, retried: a loaded box can time a single 2 s read out."""

    deadline = time.monotonic() + seconds
    while True:
        try:
            return core.client.proxy(name)
        except Exception:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.1)


def _wait_now(core, expected, seconds=8.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if _read(core, 'TIER3-AUTO').get('now') == expected:
            return True
        time.sleep(0.05)
    return False


def _history(core, name):
    entry = _read(core, name)
    return len(((entry.get('extra') or {}).get(core.url) or {}).get('history') or [])


def test_a_native_failover_is_carried_without_testing_any_node(tmp_path):
    _skip_without_core()
    with ExitStack() as stack:
        core = _ServingCore(stack, tmp_path)
        runtime = _serving(core, {'healthy': 3})
        state = _RecordingState()
        topics = SampledTopics(state, RuntimeAccess(runtime),
                               submit_normal=lambda _work: False, config_revision=lambda: 0)
        ready = _anchored_ready(runtime)
        epoch = runtime._runtime_route_epoch

        core.nodes['n1'].refused.set()
        _self_check(core, 'n1')
        assert _wait_now(core, 'n2')
        time.sleep(TICK_GAP)
        assert runtime._refresh_runtime_route_snapshot(timeout_seconds=2.0) is not None
        assert runtime._runtime_route_epoch == epoch + 1
        assert runtime.lifecycle_status()['freshness']['stale']

        history = {name: _history(core, name) for name in ('n1', 'n2', 'n3')}
        ledger = runtime._tier3_health_ledger.revision
        calls: list[str] = []
        real = runtime.api_client
        runtime.api_client = _CallRecorder(real, calls)
        try:
            assert runtime._maybe_carry_runtime_ready('test') is True
        finally:
            runtime.api_client = real

        assert calls == [], 'the carry made a controller request'
        status = runtime.lifecycle_status()
        assert not status['freshness']['stale'] and status['runtime']['startup_phase'] == 'running'
        assert runtime._runtime_egress_readiness.route_owner == ready.route_owner
        # The self-check's own record is exactly as Mihomo left it.
        assert {name: _history(core, name) for name in ('n1', 'n2', 'n3')} == history
        assert runtime._tier3_health_ledger.revision == ledger
        time.sleep(TICK_GAP)
        first = len(state.events)
        topics.sample(lambda _name, action: _quiet(action))
        assert ('invalidate', 'bastion.pool', None) not in state.events[first:]
        assert any(topic == 'bastion.pool' for _kind, topic, _dto in state.events[first:])


def test_the_route_refresh_worker_carries_a_native_failover_by_itself(tmp_path):
    _skip_without_core()
    with ExitStack() as stack:
        core = _ServingCore(stack, tmp_path)
        runtime = _serving(core, {'healthy': 3})
        _anchored_ready(runtime)
        core.nodes['n1'].refused.set()
        _self_check(core, 'n1')
        assert _wait_now(core, 'n2')
        epoch = runtime._runtime_route_epoch
        runtime._queue_runtime_route_snapshot_refresh()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            status = runtime.lifecycle_status()
            if runtime._runtime_route_epoch > epoch and not status['freshness']['stale']:
                break
            if runtime._runtime_route_epoch == epoch and not runtime._runtime_route_snapshot_refresh_running:
                # The worker's 0.75 s readback can time out on a loaded box:
                # queue it again (a production trigger would as well).
                runtime._queue_runtime_route_snapshot_refresh()
            time.sleep(0.05)
        assert runtime._runtime_route_epoch == epoch + 1
        status = runtime.lifecycle_status()
        assert not status['freshness']['stale'], runtime._runtime_ready_carry_counts
        assert status['runtime']['startup_phase'] == 'running'
        assert runtime._runtime_ready_carry_counts.get('carried') == 1


@pytest.mark.parametrize('change', ['process_generation', 'core_generation'])
def test_a_move_onto_a_new_core_owner_is_never_carried(tmp_path, change):
    """T-P1-10 with a move: an older Core's READY never counts on a new owner."""

    _skip_without_core()
    with ExitStack() as stack:
        core = _ServingCore(stack, tmp_path)
        runtime = _serving(core, {'healthy': 3})
        _anchored_ready(runtime)
        core.nodes['n1'].refused.set()
        _self_check(core, 'n1')
        pid = runtime.core_runner.status()['pid']
        generation = 2 if change == 'process_generation' else 1
        runtime.core_runner.status = lambda: {'pid': pid, 'process_generation': generation}
        runtime.core_runner.lightweight_fact = lambda: {
            'running': True, 'pid': pid, 'process_generation': generation, 'cleanup_pending': False}
        if change == 'core_generation':
            with runtime._core_lifecycle_lock:
                runtime._core_generation = 2
            runtime._runtime_initial_activation_generation = 2
        for _ in range(2):
            time.sleep(TICK_GAP)
            assert runtime._refresh_runtime_route_snapshot(timeout_seconds=2.0) is not None
            assert runtime._maybe_carry_runtime_ready('test') is False
            status = runtime.lifecycle_status()
            assert status['freshness']['stale'], status['freshness']
            assert status['runtime']['startup_phase'] != 'running'
        assert 'owner_changed' in runtime._runtime_ready_carry_counts


def test_an_exhausted_pool_after_an_app_invalidation_is_never_carried(tmp_path):
    _skip_without_core()
    with ExitStack() as stack:
        core = _ServingCore(stack, tmp_path)
        runtime = _serving(core, {'healthy': 0})
        _anchored_ready(runtime)
        for name in ('n1', 'n2', 'n3'):
            core.nodes[name].refused.set()
            _self_check(core, name)
        with runtime._runtime_route_snapshot_lock:
            runtime._runtime_route_snapshot = None
            runtime._runtime_route_snapshot_payload = None
            runtime._runtime_route_selection_signature = ()
        time.sleep(TICK_GAP)
        assert runtime._refresh_runtime_route_snapshot(timeout_seconds=2.0) is not None
        calls: list[str] = []
        real = runtime.api_client
        runtime.api_client = _CallRecorder(real, calls)
        try:
            assert runtime._maybe_carry_runtime_ready('test') is False
        finally:
            runtime.api_client = real
        assert calls == []
        assert runtime.lifecycle_status()['freshness']['stale']
        assert {'evidence:not_alive', 'evidence:tier3_view'} & set(runtime._runtime_ready_carry_counts)
