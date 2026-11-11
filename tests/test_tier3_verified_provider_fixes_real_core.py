"""Review fixes for verified Tier3B provider writes on the bundled Mihomo (11-02).

Each test drives the real DesktopRuntime code (the pool-sync worker, its
monitor-tick retries and the provider transaction) against a real Mihomo
1.19.27 with local 127.0.0.1 fixtures, through the ``_Lab`` harness of
``test_tier3_verified_provider_writes_real_core``, and pins one confirmed
review finding: write scheduling under churn (F3), unproven writes retried
(F4), the kicked-pick re-run (F5), facility holds parking once (F6), the judge
re-check (F7), owned and unowned pins (F1/F13), Phase B counting (F11) and the
append-only lanes after a verified reorder (F2).
"""

from __future__ import annotations

import threading
import time
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

import desktop_runtime as desktop_runtime_module
from desktop_runtime import ProviderSyncDisposition
from mihomo_api import MihomoAPIError
from tests.test_runtime_pool_reexport_real_core import _proxy_map
from tests.test_tier3_verified_provider_writes_real_core import PROVIDER, _lab, _Watch


class _Follow:
    """Render and pool marker that follow ``members`` (the Manager's export)."""

    def __init__(self, lab, members):
        self.lab = lab
        self.members = list(members)
        self._stack = ExitStack()

    def set(self, members):
        self.members = list(members)

    def _render(self, **_kwargs):
        projection = self.lab.projection(self.members)
        return projection.main_yaml, projection, _proxy_map(projection)

    def __enter__(self):
        runtime = self.lab.runtime
        self._stack.enter_context(patch.object(
            runtime, '_render_mihomo_runtime_payload_base_with_provider', side_effect=self._render))
        self._stack.enter_context(patch.object(
            runtime, '_tier3_core_sync_marker', side_effect=lambda: ('pool', tuple(self.members))))
        runtime._runtime_proxy_name_map = lambda *_a, **_k: _proxy_map(self.lab.projection(self.members))
        return self

    def __exit__(self, *_exc):
        self._stack.close()


def _wait_worker_idle(runtime, timeout=25.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with runtime._tier3_pool_sync_lock:
            idle = not runtime._tier3_pool_sync_pending and not runtime._tier3_pool_sync_worker_running
        if idle:
            return True
        time.sleep(0.05)
    return False


def _tick(runtime):
    """One monitor-loop tick of the frozen-cohort branch."""

    if not runtime._retry_parked_tier3_online_growth():
        runtime._retry_kicked_tier3_pick()


def test_churn_inside_the_write_spacing_lands_writes_instead_of_superseding_them(tmp_path):
    """F3: the spacing wait comes before the pool marker is taken, and an
    attempt that wrote nothing does not space the next one."""

    names = [f'n{i}' for i in range(1, 13)]
    with ExitStack() as stack:
        lab = _lab(stack, tmp_path, names=names, loaded=names[:2])
        runtime = lab.runtime
        assert runtime._tier3_online_provider_cohort_frozen()
        changes: list[tuple[float, tuple[str, ...]]] = []
        stop = threading.Event()

        def poll():
            last = None
            while not stop.is_set():
                try:
                    members = tuple(lab.members())
                except Exception:
                    members = last
                if members != last:
                    changes.append((time.monotonic(), members))
                    last = members
                stop.wait(0.05)

        poller = threading.Thread(target=poll, daemon=True)
        with _Follow(lab, names[:2]) as follow:
            poller.start()
            for count in range(3, 13):  # one admission a second, 10 s
                follow.set(names[:count])
                runtime._queue_tier3_pool_sync_after_commit()
                last_admission = time.monotonic()
                time.sleep(1.0)
            churn_end = time.monotonic()
            deadline = churn_end + 15
            while set(lab.members()) != set(names) and time.monotonic() < deadline:
                time.sleep(0.05)
            settled = time.monotonic()
            assert _wait_worker_idle(runtime)
        stop.set()
        poller.join(5)

        assert set(lab.members()) == set(names), (lab.members(), lab.lines[-20:])
        during = [stamp for stamp, _members in changes[1:] if stamp < churn_end]
        # One write lands every spacing while admissions keep coming; before
        # the fix every attempt after the first was superseded until quiet.
        assert len(during) >= 2, changes
        assert settled - last_admission < 7.5, (settled - last_admission, changes)
        print(f'\nCHURN commits_during={len(during)} lag={settled - last_admission:.2f}s')


def test_an_unproven_write_parks_and_is_retried_on_the_verified_backoff(tmp_path):
    """F4: a verified UNKNOWN (membership readback lost after the write) is
    parked and retried by the monitor tick; the retry commits the contract
    and tests the members."""

    with ExitStack() as stack:
        lab = _lab(stack, tmp_path, names=['n1', 'n2', 'n3'], loaded=['n1', 'n2'])
        runtime = lab.runtime
        real = runtime.mihomo_proxies
        state = {'generation': runtime._tier3_provider_write_generation, 'failed': 0}

        def readback_lost_once(*args, **kwargs):
            if runtime._tier3_provider_write_generation != state['generation'] and not state['failed']:
                state['failed'] = 1
                raise MihomoAPIError('GET /proxies failed: timed out')
            return real(*args, **kwargs)

        runtime.mihomo_proxies = readback_lost_once
        with _Follow(lab, ['n1', 'n2', 'n3']):
            runtime._queue_tier3_pool_sync_after_commit()
            assert _wait_worker_idle(runtime)
            assert state['failed'] == 1
            with runtime._tier3_pool_sync_lock:
                assert runtime._tier3_pool_sync_parked_key is not None
            # The file watcher loaded the write; the contract did not move.
            assert lab.members() == ['n1', 'n2', 'n3']
            assert 'n3' not in runtime._runtime_loaded_export_contract['proxy_map']
            parked_at = time.monotonic()
            retried_at = None
            deadline = parked_at + 15
            while time.monotonic() < deadline:
                if runtime._retry_parked_tier3_online_growth() and retried_at is None:
                    retried_at = time.monotonic()
                if 'n3' in runtime._runtime_loaded_export_contract['proxy_map']:
                    break
                time.sleep(0.1)
            assert _wait_worker_idle(runtime)

        assert retried_at is not None and retried_at - parked_at >= 1.5  # the verified backoff
        assert 'n3' in runtime._runtime_loaded_export_contract['proxy_map'], lab.lines[-20:]
        assert lab.members() == ['n1', 'n2', 'n3'] and lab.group()['now'] == 'n1'
        fresh = lab.verdicts(newer_than=time.time() - (time.monotonic() - retried_at) - 0.5)
        assert fresh.get('n3', (False,))[0] is True, fresh


def test_a_kicked_pick_moves_after_its_grace_without_another_pool_change(tmp_path, monkeypatch):
    """F5: the grace re-run is timed by the monitor tick, not by a pool change."""

    monkeypatch.setattr(desktop_runtime_module, '_TIER3_VERIFIED_KICKED_PICK_GRACE_SECONDS', 2.0)
    with ExitStack() as stack:
        lab = _lab(stack, tmp_path, names=['n1', 'n2', 'n3'], loaded=['n1', 'n2', 'n3'])
        runtime = lab.runtime
        with _Follow(lab, ['n2', 'n3']):  # the Manager kicks the serving n1
            runtime._queue_tier3_pool_sync_after_commit()
            assert _wait_worker_idle(runtime)
            assert lab.members() == ['n1', 'n2', 'n3'] and lab.group()['now'] == 'n1'
            assert 'n1' in runtime._tier3_kicked_pick_since
            kicked_at = time.monotonic()
            with _Watch(lab) as watch:
                deadline = kicked_at + 15
                while 'n1' in lab.members() and time.monotonic() < deadline:
                    _tick(runtime)
                    time.sleep(0.25)
                assert _wait_worker_idle(runtime)
            moved_after = time.monotonic() - kicked_at

        assert lab.members() == ['n2', 'n3'], lab.lines[-20:]
        assert lab.group()['now'] == 'n2'
        assert runtime._tier3_kicked_pick_since == {}
        assert 1.5 <= moved_after < 10, moved_after
        assert watch.failed_dials() == 0 and set(watch.distinct_picks()) <= {'n1', 'n2'}


def test_a_kicked_pick_leaves_as_soon_as_traffic_moved_off_it(tmp_path):
    """F5: once Mihomo itself moved off the kicked member, it is removed
    before the grace ends, so it cannot recapture traffic later."""

    with ExitStack() as stack:
        lab = _lab(stack, tmp_path, names=['n1', 'n2', 'n3'], loaded=['n1', 'n2', 'n3'])
        runtime = lab.runtime
        with _Follow(lab, ['n2', 'n3']):
            runtime._queue_tier3_pool_sync_after_commit()
            assert _wait_worker_idle(runtime)
            assert lab.members()[0] == 'n1' and 'n1' in runtime._tier3_kicked_pick_since
            assert runtime._retry_kicked_tier3_pick() is False  # inside the grace, still serving
            lab.nodes['n1'].refused.set()
            lab.client.health_check_provider(PROVIDER, timeout=8)
            assert lab.group()['now'] == 'n2'
            assert runtime._refresh_runtime_route_snapshot(timeout_seconds=2.0) is not None
            started = time.monotonic()
            deadline = started + 10
            while 'n1' in lab.members() and time.monotonic() < deadline:
                _tick(runtime)
                time.sleep(0.25)
            assert _wait_worker_idle(runtime)

        assert lab.members() == ['n2', 'n3'], lab.lines[-20:]
        assert time.monotonic() - started < 10  # far inside the 60 s grace


def test_an_owned_pin_keeps_its_member_past_the_grace(tmp_path, monkeypatch):
    """F1: a member pinned by an owner in this process is never removed."""

    monkeypatch.setattr(desktop_runtime_module, '_TIER3_VERIFIED_KICKED_PICK_GRACE_SECONDS', 0.0)
    with ExitStack() as stack:
        lab = _lab(stack, tmp_path, names=['n1', 'n2', 'n3'], loaded=['n1', 'n2', 'n3'])
        runtime = lab.runtime
        lab.client.select_proxy('TIER3-AUTO', 'n2', timeout=2)
        runtime._route_lease_counters['_user_groups'] = {'TIER3-AUTO'}  # a user pick in this run
        assert runtime._refresh_runtime_route_snapshot(timeout_seconds=2.0) is not None

        outcome = lab.sync(['n1', 'n3'])  # the Manager stops exporting the pinned n2

        assert outcome.disposition in {ProviderSyncDisposition.COMMITTED, ProviderSyncDisposition.NOOP}, outcome
        group = lab.group()
        assert (group['fixed'], group['now']) == ('n2', 'n2'), (group, lab.lines[-20:])
        # Still a member (a pin skips the members before it, wherever it is).
        assert set(lab.members()) == {'n1', 'n2', 'n3'}
        assert runtime._retry_kicked_tier3_pick() is False  # no timed move while owned


def test_an_unowned_pin_on_a_member_the_write_removes_is_cleared_before_the_write(tmp_path, monkeypatch):
    """F13: no write leaves a pin naming a non-member (served blind)."""

    monkeypatch.setattr(desktop_runtime_module, '_TIER3_VERIFIED_KICKED_PICK_GRACE_SECONDS', 0.0)
    with ExitStack() as stack:
        lab = _lab(stack, tmp_path, names=['n1', 'n2', 'n3'], loaded=['n1', 'n2', 'n3'])
        runtime = lab.runtime
        lab.client.select_proxy('TIER3-AUTO', 'n1', timeout=2)  # nobody in this process owns it
        assert runtime._refresh_runtime_route_snapshot(timeout_seconds=2.0) is not None
        lab.nodes['n2'].refused.set()  # died after its last (alive) verdict: ranked first
        clears = []
        real_unfix = lab.client.unfix_proxy

        def recording_unfix(group, *args, **kwargs):
            clears.append((group, runtime._tier3_provider_write_generation, lab.group()['fixed']))
            return real_unfix(group, *args, **kwargs)

        lab.client.unfix_proxy = recording_unfix
        generation = runtime._tier3_provider_write_generation

        outcome = lab.sync(['n2', 'n3'])  # the Manager kicks the pinned n1

        assert outcome.disposition is ProviderSyncDisposition.COMMITTED, (outcome, lab.lines[-20:])
        assert clears and clears[0] == ('TIER3-AUTO', generation, 'n1'), clears
        group = lab.group()
        assert (group['fixed'], group['now']) == ('', 'n3'), (group, lab.lines[-20:])
        assert 'n1' not in lab.members()


def test_phase_b_counts_and_records_only_projected_members(tmp_path):
    """F11: the terminal REJECT is neither counted nor folded into the ledger."""

    with ExitStack() as stack:
        lab = _lab(stack, tmp_path, names=['d1', 'd2', 'a3', 'a4'], loaded=['d1', 'd2'],
                   refused=['d1', 'd2'])
        runtime = lab.runtime
        summaries = []
        original = runtime._verify_tier3_provider_after_write

        def capture(result):
            summaries.append(original(result))
            return summaries[-1]

        runtime._verify_tier3_provider_after_write = capture
        outcome = lab.sync(['d1', 'd2', 'a3', 'a4'])  # all dead: a pick-moving write

        assert outcome.disposition is ProviderSyncDisposition.COMMITTED, (outcome, lab.lines[-20:])
        summary = summaries[-1]
        assert summary['members'] == 4 and summary['tested'] <= 4, summary
        assert summary['dead'] <= 2, summary
        assert 'REJECT' not in runtime._tier3_health_ledger.snapshot()


def test_the_append_only_lanes_grow_after_a_verified_reorder(tmp_path):
    """F2: the legacy append-only lanes judge growth against the Core-loaded
    order, so a verified pick-first reorder does not wedge them."""

    with ExitStack() as stack:
        lab = _lab(stack, tmp_path, names=['n1', 'n2', 'n3', 'n4', 'n5'],
                   loaded=['n1', 'n2', 'n3'], refused=['n1'])
        runtime = lab.runtime
        outcome = lab.sync(['n1', 'n2', 'n3', 'n4'])
        assert outcome.disposition is ProviderSyncDisposition.COMMITTED, outcome
        assert lab.members() == ['n2', 'n3', 'n4', 'n1']  # not the Manager's order

        # A facility hold sends provider writes to the append-only lane.
        runtime._runtime_network_context_hold_active = True
        runtime._runtime_network_context_hold_reason = 'test network context hold'
        assert runtime._tier3_verified_online_write_state() == (False, 'facility_hold')
        with _Follow(lab, ['n1', 'n2', 'n3', 'n4', 'n5']):
            result = runtime._update_running_tier3_core_provider(('pool', ('n1', 'n2', 'n3', 'n4', 'n5')))

        assert result.get('updated'), (result, lab.lines[-20:])
        assert lab.members() == ['n2', 'n3', 'n4', 'n1', 'n5']
        assert lab.group()['now'] == 'n2'


def test_a_facility_hold_parks_the_verified_lane_once_and_resumes_after_it(tmp_path):
    """F6: no attempt per tick inside a hold; the parked key runs after it."""

    with ExitStack() as stack:
        lab = _lab(stack, tmp_path, names=['n1', 'n2', 'n3'], loaded=['n1', 'n2'])
        runtime = lab.runtime
        attempts = []
        original = runtime._update_running_tier3_core_provider

        def counting(*args, **kwargs):
            attempts.append(time.monotonic())
            return original(*args, **kwargs)

        runtime._update_running_tier3_core_provider = counting
        runtime._runtime_network_context_hold_active = True
        runtime._runtime_network_context_hold_reason = 'test network context hold'
        with _Follow(lab, ['n1', 'n2', 'n3']):
            runtime._queue_tier3_pool_sync_after_commit()
            for _ in range(15):
                _tick(runtime)
                time.sleep(0.2)
            assert _wait_worker_idle(runtime)
            assert attempts == [] and lab.members() == ['n1', 'n2']
            with runtime._tier3_pool_sync_lock:
                assert runtime._tier3_pool_sync_parked_key is not None

            runtime._runtime_network_context_hold_active = False
            deadline = time.monotonic() + 15
            while lab.members() != ['n1', 'n2', 'n3'] and time.monotonic() < deadline:
                _tick(runtime)
                time.sleep(0.2)
            assert _wait_worker_idle(runtime)

        assert lab.members() == ['n1', 'n2', 'n3'], lab.lines[-20:]
        assert len(attempts) == 1


def test_a_judgement_admitted_inside_the_spacing_parks_the_write(tmp_path):
    """F7: judgements stay admissible during the spacing; one admitted there
    owns the route, and the write parks without touching the provider."""

    with ExitStack() as stack:
        lab = _lab(stack, tmp_path, names=['n1', 'n2', 'n3'], loaded=['n1', 'n2'])
        runtime = lab.runtime
        state = {'judging': False}
        judge = SimpleNamespace(judging=lambda: state['judging'], ladder_in_flight=lambda: False)
        runtime._node_judgement_state = lambda: SimpleNamespace(judge=judge)
        runtime._tier3_provider_write_finished_at = time.monotonic()  # a full spacing is due
        busy_during_spacing = []
        threading.Timer(0.8, lambda: busy_during_spacing.append(runtime._runtime_post_dial_d4_txn_busy())).start()
        threading.Timer(1.2, lambda: state.update(judging=True)).start()
        generation = runtime._tier3_provider_write_generation

        outcome = lab.sync(['n1', 'n2', 'n3'])

        assert outcome.disposition is ProviderSyncDisposition.PARKED, outcome
        assert 'judging' in str(outcome.reason), outcome
        assert busy_during_spacing == [None]
        assert runtime._tier3_provider_write_generation == generation
        assert runtime._tier3_provider_write_busy is False
        assert lab.members() == ['n1', 'n2']


def test_a_judgement_admitted_before_the_core_lock_defers_the_write(tmp_path):
    """F7: the write re-checks the judge under the Core lock (a judgement
    that registered after the busy flag went up but before the lock)."""

    with ExitStack() as stack:
        lab = _lab(stack, tmp_path, names=['n1', 'n2', 'n3'], loaded=['n1', 'n2'])
        runtime = lab.runtime
        # Judging only as seen from inside the Core transition lock.
        judge = SimpleNamespace(judging=lambda: runtime._core_transition_lock._is_owned(),
                                ladder_in_flight=lambda: False)
        runtime._node_judgement_state = lambda: SimpleNamespace(judge=judge)
        generation = runtime._tier3_provider_write_generation

        outcome = lab.sync(['n1', 'n2', 'n3'])

        assert outcome.disposition is ProviderSyncDisposition.PARKED, outcome
        assert 'judgement' in str(outcome.reason), outcome
        assert runtime._tier3_provider_write_generation == generation
        assert lab.members() == ['n1', 'n2']


def test_a_verified_write_never_runs_without_its_busy_flag(tmp_path):
    """F7 (write side): the verified lane opening between the caller's lane
    choice and the leased write (an initial activation completing while the
    write waited for its locks) parks the write instead of running it and its
    member tests unseen by the judges; the retry takes the lane with its flag."""

    with ExitStack() as stack:
        lab = _lab(stack, tmp_path, names=['n1', 'n2', 'n3'], loaded=['n1', 'n2'])
        runtime = lab.runtime
        real_open = runtime._tier3_verified_lane_open
        real_leased = runtime._update_running_tier3_core_provider_leased
        lane = {'open': False}

        def leased(*args, **kwargs):
            lane['open'] = True  # opened after the caller chose its lane
            return real_leased(*args, **kwargs)

        runtime._tier3_verified_lane_open = lambda: lane['open'] and real_open()
        runtime._update_running_tier3_core_provider_leased = leased
        generation = runtime._tier3_provider_write_generation

        first = lab.sync(['n1', 'n2', 'n3'])

        assert first.disposition is ProviderSyncDisposition.PARKED, (first, lab.lines[-20:])
        assert 'busy flag' in str(first.reason), first
        assert runtime._tier3_provider_write_generation == generation  # nothing written
        assert runtime._tier3_provider_write_busy is False
        assert lab.members() == ['n1', 'n2']

        busy_at_put = []
        real_put = lab.client.update_proxy_provider

        def put(name, *, timeout=None):
            busy_at_put.append(runtime._tier3_provider_write_busy)
            return real_put(name, timeout=timeout)

        lab.client.update_proxy_provider = put
        runtime._tier3_provider_write_finished_at = 0.0
        second = lab.sync(['n1', 'n2', 'n3'])

        assert second.disposition is ProviderSyncDisposition.COMMITTED, (second, lab.lines[-20:])
        assert busy_at_put and all(busy_at_put), busy_at_put
        assert runtime._tier3_provider_write_busy is False
        assert lab.members() == ['n1', 'n2', 'n3']


def test_a_failing_pool_marker_read_never_leaves_the_busy_flag_up(tmp_path):
    """F7 (write side): the busy flag is lowered whatever raises after it went
    up; a flag left up would gate every judgement for the process lifetime."""

    with ExitStack() as stack:
        lab = _lab(stack, tmp_path, names=['n1', 'n2', 'n3'], loaded=['n1', 'n2'])
        runtime = lab.runtime
        projection = lab.projection(['n1', 'n2', 'n3'])
        marker = ('pool', tuple(projection.proxy_names))

        def marker_read():
            if runtime._tier3_provider_write_busy:
                raise RuntimeError('pool marker read failed')
            return marker

        with patch.object(runtime, '_render_mihomo_runtime_payload_base_with_provider',
                          return_value=(projection.main_yaml, projection, _proxy_map(projection))), \
                patch.object(runtime, '_tier3_core_sync_marker', side_effect=marker_read):
            runtime._runtime_proxy_name_map = lambda *_a, **_k: _proxy_map(projection)
            try:
                runtime._sync_core_config_after_pool_update()
            except RuntimeError as exc:
                assert 'pool marker read failed' in str(exc)
            else:
                raise AssertionError('the marker read was expected to raise under the flag')

        assert runtime._tier3_provider_write_busy is False
        assert lab.members() == ['n1', 'n2']


def test_a_judgement_running_through_the_spacing_never_sees_the_busy_flag(tmp_path):
    """F7 (review JL-1): a write that finds a judgement admitted during its
    spacing parks without raising the flag, so the judgement's own checks
    never stand it down for a write that was going to park anyway."""

    with ExitStack() as stack:
        lab = _lab(stack, tmp_path, names=['n1', 'n2', 'n3'], loaded=['n1', 'n2'])
        runtime = lab.runtime
        state = {'judging': False}
        judge = SimpleNamespace(judging=lambda: state['judging'], ladder_in_flight=lambda: False)
        runtime._node_judgement_state = lambda: SimpleNamespace(judge=judge)
        runtime._tier3_provider_write_finished_at = time.monotonic()  # a full spacing is due
        threading.Timer(1.0, lambda: state.update(judging=True)).start()
        flag_while_judging = []
        real_state = runtime._tier3_verified_online_write_state

        def write_state():
            if state['judging']:
                flag_while_judging.append(runtime._tier3_provider_write_busy)
            return real_state()

        runtime._tier3_verified_online_write_state = write_state
        generation = runtime._tier3_provider_write_generation

        outcome = lab.sync(['n1', 'n2', 'n3'])

        assert outcome.disposition is ProviderSyncDisposition.PARKED, outcome
        assert 'judging' in str(outcome.reason), outcome
        assert flag_while_judging == [False], flag_while_judging
        assert runtime._tier3_provider_write_busy is False
        assert runtime._tier3_provider_write_generation == generation
        assert lab.members() == ['n1', 'n2']


def test_a_member_kicked_again_under_the_same_name_gets_its_own_timed_move(tmp_path, monkeypatch):
    """F5 (review TXN-1): the re-run signature carries the kick stamps, so a
    second kick episode of the same stable name (same definition) is re-run
    after its grace instead of being taken for the first one."""

    monkeypatch.setattr(desktop_runtime_module, '_TIER3_VERIFIED_KICKED_PICK_GRACE_SECONDS', 1.5)
    with ExitStack() as stack:
        lab = _lab(stack, tmp_path, names=['n1', 'n2', 'n3', 'n4'], loaded=['n1', 'n2', 'n3'])
        runtime = lab.runtime

        def tick_until(done, seconds):
            deadline = time.monotonic() + seconds
            last_refresh = 0.0
            while not done() and time.monotonic() < deadline:
                _tick(runtime)
                if time.monotonic() - last_refresh >= 1.0:
                    runtime._refresh_runtime_route_snapshot(timeout_seconds=2.0)
                    last_refresh = time.monotonic()
                time.sleep(0.25)
            assert _wait_worker_idle(runtime)

        with _Follow(lab, ['n2', 'n3']) as follow:
            # Episode 1: the Manager kicks the serving n1; the timer moves it.
            runtime._queue_tier3_pool_sync_after_commit()
            assert _wait_worker_idle(runtime)
            assert lab.members()[0] == 'n1' and 'n1' in runtime._tier3_kicked_pick_since
            tick_until(lambda: 'n1' not in lab.members(), 10)
            assert 'n1' not in lab.members(), lab.lines[-20:]

            # n1 comes back (same definition, same name) and serves again on
            # its own merit: the members ahead die, an admission puts it first.
            follow.set(['n2', 'n3', 'n1'])
            runtime._tier3_provider_write_finished_at = 0.0
            runtime._queue_tier3_pool_sync_after_commit()
            assert _wait_worker_idle(runtime)
            lab.nodes['n2'].refused.set()
            lab.nodes['n3'].refused.set()
            lab.client.health_check_provider(PROVIDER, timeout=8)
            assert lab.group()['now'] == 'n1' and not lab.group().get('fixed'), lab.group()
            assert runtime._refresh_runtime_route_snapshot(timeout_seconds=2.0) is not None
            follow.set(['n2', 'n3', 'n1', 'n4'])
            runtime._tier3_provider_write_finished_at = 0.0
            runtime._queue_tier3_pool_sync_after_commit()
            assert _wait_worker_idle(runtime)
            assert lab.members()[0] == 'n1', lab.members()
            lab.nodes['n2'].refused.clear()
            lab.nodes['n3'].refused.clear()
            lab.client.health_check_provider(PROVIDER, timeout=8)
            assert lab.group()['now'] == 'n1', lab.group()
            assert runtime._refresh_runtime_route_snapshot(timeout_seconds=2.0) is not None

            # Episode 2: the Manager kicks the serving n1 again.
            follow.set(['n2', 'n3', 'n4'])
            runtime._tier3_provider_write_finished_at = 0.0
            runtime._queue_tier3_pool_sync_after_commit()
            assert _wait_worker_idle(runtime)
            assert 'n1' in lab.members() and 'n1' in runtime._tier3_kicked_pick_since
            tick_until(lambda: 'n1' not in lab.members(), 12)

        assert 'n1' not in lab.members(), (lab.members(), lab.lines[-20:])
        assert lab.group()['now'] != 'n1'
        assert runtime._tier3_kicked_pick_since == {}


def _kicked_pick_move(stack, tmp_path, *, carry):
    """Decision D: one pick-moving verified write (the K2 move) after an anchored READY."""

    from runtime_egress_recovery import RuntimeEgressReadinessState
    from tests.test_runtime_lifecycle_freshness_real_core import _CallRecorder

    lab = _lab(stack, tmp_path, names=['n1', 'n2', 'n3'], loaded=['n1', 'n2', 'n3'])
    runtime = lab.runtime
    pid = runtime.core_runner.status()['pid']
    runtime.core_runner.lightweight_fact = lambda: {
        'running': True, 'pid': pid, 'process_generation': 1, 'cleanup_pending': False}
    runtime.running = True
    runtime._desired_running = True
    ready = runtime._publish_runtime_egress_readiness(
        RuntimeEgressReadinessState.READY, validation_active=False)
    assert ready.route_owner
    carry_calls: list[str] = []
    real_carry = runtime._maybe_carry_runtime_ready

    def carry_hook(source):
        if not carry:
            return False
        client = runtime.api_client
        runtime.api_client = _CallRecorder(client, carry_calls)
        try:
            return real_carry(source)
        finally:
            runtime.api_client = client

    runtime._maybe_carry_runtime_ready = carry_hook
    summaries = []
    real_verify = runtime._verify_tier3_provider_after_write

    def verify(result):
        summary = real_verify(result)
        summaries.append(summary)
        return summary

    runtime._verify_tier3_provider_after_write = verify
    with _Follow(lab, ['n2', 'n3']):  # the Manager kicks the serving n1; no grace
        runtime._queue_tier3_pool_sync_after_commit()
        assert _wait_worker_idle(runtime)
    status = runtime.lifecycle_status()
    pick = lab.group()['now']
    return {
        'stale': status['freshness']['stale'],
        'phase': status['runtime']['startup_phase'],
        'members': lab.members(),
        'now': pick,
        'phase_b': {key: summaries[-1].get(key) for key in ('members', 'tested', 'pick_skipped', 'outcome')}
        if summaries else None,
        'carry_calls': carry_calls,
        'counts': dict(runtime._runtime_ready_carry_counts),
    }


def test_a_pick_moving_write_carries_ready_and_leaves_phase_b_as_it_was(tmp_path, monkeypatch):
    """Decision D: the K2 move (kicked pick after its grace) carries READY at the
    provider commit, with no request of its own, and the write's member checks
    (Phase B) come out the same with the carry on and off: the carry does not
    perturb the self-check cycle (the user's condition)."""

    monkeypatch.setattr(desktop_runtime_module, '_TIER3_VERIFIED_KICKED_PICK_GRACE_SECONDS', 0.0)
    with ExitStack() as stack:
        on = _kicked_pick_move(stack, tmp_path / 'on', carry=True)
    with ExitStack() as stack:
        off = _kicked_pick_move(stack, tmp_path / 'off', carry=False)

    assert on['members'] == off['members'] == ['n2', 'n3']
    assert on['now'] == off['now'] == 'n2'
    assert (on['stale'], on['phase']) == (False, 'running'), on['counts']
    assert on['carry_calls'] == []
    assert on['counts'].get('carried') == 1
    assert off['stale'] is True and off['phase'] != 'running'
    assert on['phase_b'] == off['phase_b'] and on['phase_b'] is not None
