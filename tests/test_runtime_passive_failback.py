from __future__ import annotations

import logging
import threading
import time
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from desktop_runtime import DesktopRuntime
from runtime_egress_recovery import CoreLoadedRouteSnapshot, RuntimeNodeRef, RuntimeRouteBinding
from runtime_fallback_paths import preferred_fallback_path


@pytest.fixture
def runtime():
    runtime = object.__new__(DesktopRuntime)
    runtime.stop_event = threading.Event()
    runtime.logger = logging.getLogger('test-passive-failback')
    runtime._runtime_egress_recovery_profile = 'early_single_coordinator'
    runtime._runtime_failure_monitor_lock = threading.RLock()
    runtime._runtime_route_snapshot_lock = threading.Lock()
    runtime._runtime_selector_intent_revision = 1
    runtime._runtime_recheck_retry_attempts = {}
    runtime._runtime_pending_recovered_payloads = {}
    runtime._runtime_incident_recovery_state = {'incident': 'ready'}
    runtime._runtime_failure_recovery_threads = set()
    runtime._runtime_route_epoch = 1
    runtime._runtime_worker_token = lambda: (1,)
    runtime._runtime_worker_token_is_current = lambda token: token == (1,) and not runtime.stop_event.is_set()
    runtime._runtime_recovery_now = time.monotonic
    runtime._mark_runtime_route_attribution_barrier_locked = lambda: None
    runtime._runtime_candidate_health_snapshot = lambda: None
    runtime._bounded_core_transition = lambda *_args: nullcontext()
    runtime._runtime_egress_coordinator = SimpleNamespace(
        core_mutation_transaction=lambda *_args, **_kwargs: nullcontext(object()),
        set_current_snapshot=lambda *_args: None,
    )
    a = RuntimeNodeRef('tier1', 'tier1_variant', 'identity-a', 'a')
    b = RuntimeNodeRef('tier3', 'tier3b', 'identity-b', 'b')
    c = RuntimeNodeRef('tier2', 'tier2', 'identity-c', 'c')
    snapshot = CoreLoadedRouteSnapshot(process_pid=1, process_generation=1, core_generation=1, semantic_digest='loaded', route_epoch=1, proxy_nodes=(a, b, c))
    state = {'leaf': 'b', 'fixed': 'b'}
    entries = {
        'FINAL-EXIT': {'type': 'Selector', 'now': 'PROXY-FALLBACK', 'all': ['PROXY-FALLBACK', 'REJECT']},
        'PROXY-FALLBACK': {'type': 'Fallback', 'now': 'b', 'all': ['a', 'c', 'b'], 'fixed': 'b'},
        'a': {'type': 'HTTP'}, 'b': {'type': 'HTTP'}, 'c': {'type': 'HTTP'},
    }
    payload = {'proxies': {'proxies': entries}}
    def refresh(**_kwargs):
        entries['PROXY-FALLBACK'].update(now=state['leaf'], fixed=state['fixed'])
        return snapshot
    runtime._refresh_runtime_route_snapshot = refresh
    runtime._runtime_route_snapshot_state = lambda: (snapshot, payload)
    def bound(_snapshot, _payload, outbound, **_kwargs):
        ref = {'a': a, 'b': b, 'c': c}[state['leaf']]
        return RuntimeRouteBinding(process_pid=1, process_generation=1, core_generation=1, semantic_digest='loaded', route_epoch=1, observed_outbound=outbound, route_chain=(outbound, 'PROXY-FALLBACK', ref.proxy_name), node=ref)
    runtime._runtime_route_binding_for_outbound = bound
    original = RuntimeRouteBinding(process_pid=1, process_generation=1, core_generation=1, semantic_digest='loaded', route_epoch=1, observed_outbound='FINAL-EXIT', route_chain=('FINAL-EXIT', 'PROXY-FALLBACK', 'c'), node=c)
    runtime._runtime_incident_bindings = {'incident': original}
    lease = {'incident_id': 'incident', 'owner_token': 'owner', 'from': 'c', 'to': 'b', 'target_member': 'b', 'mode': 'fixed', 'group': 'PROXY-FALLBACK', 'selector_intent_revision': 1, 'route_epoch': 1, 'verified_at_monotonic': time.monotonic()}
    runtime._runtime_route_mutation_lease = lease
    calls = []
    def unfix(group, **_kwargs):
        calls.append('DELETE')
        state.update(fixed='', leaf='a')
        return {}
    def read(group, **_kwargs):
        calls.append('GET')
        return {'fixed': state['fixed']}
    runtime._require_api_client = lambda: SimpleNamespace(unfix_proxy=unfix, proxy=read)
    runtime._prewarm_runtime_switch_target = lambda *_args, **_kwargs: calls.append('prewarm')
    runtime._verify_runtime_final_exit = lambda **_kwargs: calls.append('204') or {'status': 204}
    runtime._queue_runtime_route_snapshot_refresh = lambda: calls.append('refresh')
    runtime._clear_runtime_audit_export_identity = lambda _identity: calls.append('clear')
    runtime._set_effective_egress_state = lambda ok, _reason: calls.append(('egress', ok))
    runtime._try_arm_runtime_recovery = lambda: calls.append('arm') or True
    runtime._publish_verified_runtime_source_proof = lambda **_kwargs: calls.append('source-proof') or True
    runtime.test = SimpleNamespace(a=a, b=b, c=c, state=state, calls=calls, snapshot=snapshot, payload=payload, lease=lease)
    yield runtime
    runtime.stop_event.set()
    for thread in tuple(runtime._runtime_failure_recovery_threads):
        thread.join(1)


def event(ref, **kwargs):
    return {'tier': ref.tier, 'pool_kind': ref.pool_kind, 'identity': ref.identity, 'proxy_name': ref.proxy_name, 'authoritative': True, 'transition': {'action': 'recovered'}, **kwargs}


def request(runtime, ref=None):
    lease = runtime.test.lease
    return {'lease': lease, 'signature': runtime._runtime_route_lease_cas_signature(lease), 'binding': runtime._runtime_incident_bindings['incident'], 'ref': ref or runtime.test.a, 'target': 'a', 'payload': event(ref or runtime.test.a), 'worker_token': (1,), 'running': True}


def wait_done(runtime):
    deadline = time.monotonic() + 2
    while runtime._runtime_failure_recovery_threads and time.monotonic() < deadline:
        time.sleep(.005)
    assert not runtime._runtime_failure_recovery_threads


def test_periodic_pass_of_higher_layer_releases_lower_layer_pin(runtime):
    runtime._on_runtime_leaf_recheck_result_early(event(runtime.test.a, incident_id=''))
    wait_done(runtime)
    assert runtime._runtime_route_mutation_lease is None
    assert runtime.test.lease['release_state'] == 'released'
    assert runtime.test.calls.index('GET') < runtime.test.calls.index('DELETE')
    assert runtime.test.calls.index('DELETE') < runtime.test.calls.index('204')
    assert runtime.test.calls.index('204') < runtime.test.calls.index('source-proof')


@pytest.mark.parametrize('readback', [None, {}, TimeoutError('controller')])
def test_unknown_readback_never_blindly_repeats_delete(runtime, readback):
    def read(*_args, **_kwargs):
        if isinstance(readback, Exception):
            raise readback
        return readback
    runtime._require_api_client = lambda: SimpleNamespace(proxy=read, unfix_proxy=lambda *_args, **_kwargs: pytest.fail('unexpected DELETE'))
    result = runtime._release_runtime_automatic_fixed_selection(runtime.test.lease, deadline_monotonic=time.monotonic()+1, readback_first=True)
    assert result['commit_state'] == 'unknown'


def test_lost_delete_response_is_reconciled_without_second_delete(runtime):
    runtime.test.state.update(fixed='', leaf='a')
    assert runtime._attempt_runtime_passive_failback(request(runtime)) == 'released'
    assert 'DELETE' not in runtime.test.calls
    assert '204' in runtime.test.calls


def test_readback_different_fixed_member_is_superseded(runtime):
    runtime.test.state['fixed'] = 'manual-member'
    assert runtime._attempt_runtime_passive_failback(request(runtime)) == 'superseded'
    assert 'DELETE' not in runtime.test.calls


def test_known_uncommitted_release_can_retry_and_commit(runtime):
    calls = []
    def unfix(*_args, **_kwargs):
        calls.append('DELETE')
        if len(calls) == 2:
            runtime.test.state.update(fixed='', leaf='a')
    runtime._require_api_client = lambda: SimpleNamespace(unfix_proxy=unfix, proxy=lambda *_args, **_kwargs: {'fixed': runtime.test.state['fixed']})
    pending = request(runtime)
    assert runtime._attempt_runtime_passive_failback(pending) == 'pending'
    assert runtime._attempt_runtime_passive_failback(pending) == 'released'
    assert calls == ['DELETE', 'DELETE']


def test_manual_intent_racing_prewarm_prevents_delete(runtime):
    runtime._prewarm_runtime_switch_target = lambda *_args, **_kwargs: setattr(runtime, '_runtime_selector_intent_revision', 2)
    assert runtime._attempt_runtime_passive_failback(request(runtime)) == 'superseded'
    assert 'DELETE' not in runtime.test.calls


@pytest.mark.parametrize('arrival', ['before_attempt', 'during_prewarm'])
def test_new_failure_confirmation_defers_old_passive_failback(runtime, arrival):
    binding = runtime._runtime_route_binding_for_outbound(
        runtime.test.snapshot, runtime.test.payload, 'FINAL-EXIT',
    )
    runtime._runtime_failure_probe_bindings = set()

    def confirming():
        runtime._runtime_failure_probe_bindings.add(binding.circuit_key)

    if arrival == 'before_attempt':
        confirming()
    else:
        runtime._prewarm_runtime_switch_target = lambda *_a, **_k: confirming()
    pending = request(runtime)
    assert runtime._attempt_runtime_passive_failback(pending) == 'recovery_pending'
    assert runtime._runtime_route_mutation_lease is runtime.test.lease
    assert runtime.test.state == {'leaf': 'b', 'fixed': 'b'}
    assert 'DELETE' not in runtime.test.calls
    assert '204' not in runtime.test.calls
    assert 'source-proof' not in runtime.test.calls
    assert ('egress', True) not in runtime.test.calls

    # A bounded later attempt can consume the same health PASS if recovery did
    # not replace this lease; it must not lose the request merely due to priority.
    runtime._runtime_failure_probe_bindings.clear()
    runtime._prewarm_runtime_switch_target = lambda *_a, **_k: None
    assert runtime._attempt_runtime_passive_failback(pending) == 'released'


@pytest.mark.parametrize('changed', [
    {'process_pid': 99}, {'process_generation': 2}, {'core_generation': 2},
    {'semantic_digest': 'other-config'}, {'network_context_epoch': 1},
])
def test_old_core_confirmation_does_not_block_current_passive_failback(runtime, changed):
    from dataclasses import replace
    binding = runtime._runtime_route_binding_for_outbound(
        runtime.test.snapshot, runtime.test.payload, 'FINAL-EXIT',
    )
    runtime._runtime_failure_probe_bindings = {replace(binding, **changed).circuit_key}
    assert runtime._attempt_runtime_passive_failback(request(runtime)) == 'released'


@pytest.mark.parametrize('arrival', ['during_delete', 'verification_pass', 'verification_fail'])
def test_late_confirmation_prevents_old_failback_publication_or_rollback(runtime, arrival):
    binding = runtime._runtime_route_binding_for_outbound(
        runtime.test.snapshot, runtime.test.payload, 'FINAL-EXIT',
    )
    runtime._runtime_failure_probe_bindings = set()

    def confirming():
        runtime._runtime_failure_probe_bindings.add(binding.circuit_key)

    if arrival == 'during_delete':
        client = runtime._require_api_client()
        original = client.unfix_proxy
        def release(*args, **kwargs):
            result = original(*args, **kwargs)
            confirming()
            return result
        client.unfix_proxy = release
        runtime._require_api_client = lambda: client
    else:
        def verify(**_kwargs):
            confirming()
            if arrival == 'verification_fail':
                raise TimeoutError('old promotion lost connectivity')
            return {'status': 204}
        runtime._verify_runtime_final_exit = verify
    runtime._restore_runtime_automatic_route = lambda **_k: pytest.fail(
        'old failback must not roll back the route under a newer confirmation',
    )
    assert runtime._attempt_runtime_passive_failback(request(runtime)) == 'recovery_pending'
    assert runtime._runtime_route_mutation_lease is runtime.test.lease
    assert runtime.test.state == {'leaf': 'a', 'fixed': ''}
    assert ('egress', True) not in runtime.test.calls
    assert 'source-proof' not in runtime.test.calls


def test_manual_intent_after_final_verification_prevents_publication(runtime):
    def verify(**_kwargs):
        runtime._runtime_selector_intent_revision = 2
        return {'status': 204}
    runtime._verify_runtime_final_exit = verify
    assert runtime._attempt_runtime_passive_failback(request(runtime)) == 'superseded'
    assert runtime._runtime_route_mutation_lease is runtime.test.lease
    assert 'source-proof' not in runtime.test.calls
    assert ('egress', True) not in runtime.test.calls


def test_promotion_204_failure_restores_verified_backup(runtime):
    def verify(**_kwargs):
        if runtime.test.state['leaf'] == 'a':
            raise TimeoutError('restored node fails end to end')
        runtime.test.calls.append('rollback-204')
        return {'status': 204}
    def restore(**_kwargs):
        runtime.test.state.update(leaf='b', fixed='b')
        runtime.test.calls.append('rollback')
        return runtime.test.snapshot, runtime.test.payload, None
    runtime._verify_runtime_final_exit = verify
    runtime._restore_runtime_automatic_route = restore
    assert runtime._attempt_runtime_passive_failback(request(runtime)) == 'pending'
    assert runtime.test.state == {'leaf': 'b', 'fixed': 'b'}
    assert runtime._runtime_route_mutation_lease is runtime.test.lease
    assert runtime.test.calls.index('rollback') < runtime.test.calls.index('rollback-204')


def test_release_retry_budget_is_finite_and_new_pass_can_restart(runtime):
    delays = []
    runtime.stop_event = SimpleNamespace(is_set=lambda: False, set=lambda: None, wait=lambda delay: delays.append(delay) or False)
    attempts = []
    runtime._attempt_runtime_passive_failback = lambda req: attempts.append(req['attempt']) or 'readback_pending'
    runtime._on_runtime_leaf_recheck_result_early(event(runtime.test.a))
    wait_done(runtime)
    assert attempts == [0, 1, 2, 3]
    assert delays == [5, 15, 30]
    runtime._on_runtime_leaf_recheck_result_early(event(runtime.test.a, check_run_id='new-check'))
    wait_done(runtime)
    assert attempts == [0, 1, 2, 3] * 2


def test_duplicate_pass_cannot_restart_exhausted_retry_budget(runtime):
    runtime.stop_event = SimpleNamespace(is_set=lambda: False, set=lambda: None, wait=lambda _delay: False)
    attempts = []
    runtime._attempt_runtime_passive_failback = lambda req: attempts.append(req['attempt']) or 'pending'
    payload = event(runtime.test.a, check_run_id='same-check')
    runtime._on_runtime_leaf_recheck_result_early(payload)
    wait_done(runtime)
    runtime._on_runtime_leaf_recheck_result_early(payload)
    wait_done(runtime)
    assert attempts == [0, 1, 2, 3]


def test_neutral_health_checks_retry_bounded_but_fail_does_not(runtime):
    scheduled = []
    runtime._schedule_runtime_leaf_recheck = lambda payload, **kwargs: scheduled.append(kwargs['delay_seconds'])
    for _ in range(5):
        runtime._on_runtime_leaf_recheck_result_early(event(runtime.test.a, transition={'action': 'pending'}))
    assert scheduled == [5, 15, 30]
    runtime._on_runtime_leaf_recheck_result_early(event(runtime.test.a, transition={'action': 'transferred'}, verdict='FAIL'))
    assert scheduled == [5, 15, 30]
    runtime._on_runtime_leaf_recheck_result_early(event(runtime.test.a, transition={'action': 'pass_confirmation_pending'}))
    assert scheduled == [5, 15, 30, 2]


@pytest.mark.parametrize('incident_id', ['', 'incident'])
def test_neutral_retry_uses_health_queue_without_requiring_an_incident(runtime, incident_id):
    called = threading.Event()
    calls = []
    def enqueue(kind, ref, **kwargs):
        calls.append((kind, ref, kwargs))
        called.set()
        return {'accepted': True}
    runtime.manager = SimpleNamespace(
        enqueue_node_health_check=lambda ref, **kwargs: enqueue('health', ref, **kwargs),
        enqueue_runtime_leaf_recheck=lambda ref, **kwargs: enqueue('incident', ref, **kwargs),
    )
    runtime._schedule_runtime_leaf_recheck(event(runtime.test.a, incident_id=incident_id), delay_seconds=0)
    assert called.wait(1)
    assert calls[0][0] == ('incident' if incident_id else 'health')
    assert calls[0][1] == runtime.test.a
    assert ('incident_id' in calls[0][2]) is bool(incident_id)


def test_pass_before_recovery_lease_is_consumed_after_commit(runtime):
    runtime._runtime_route_mutation_lease = None
    runtime._runtime_incident_recovery_state['incident'] = 'recovering'
    runtime._on_runtime_leaf_recheck_result_early(event(runtime.test.a))
    assert runtime._runtime_pending_recovered_payloads
    runtime._runtime_route_mutation_lease = runtime.test.lease
    runtime._runtime_incident_recovery_state['incident'] = 'ready'
    runtime._resume_runtime_passive_failback()
    wait_done(runtime)
    assert runtime._runtime_route_mutation_lease is None


def test_missing_snapshot_retries_only_controller_and_cleans_pending_payload(runtime):
    delays = []
    reads = []
    runtime.stop_event = SimpleNamespace(is_set=lambda: False, set=lambda: None, wait=lambda delay: delays.append(delay) or False)
    runtime._runtime_route_snapshot_state = lambda: (None, None)
    runtime._refresh_runtime_route_snapshot = lambda **kwargs: reads.append(kwargs['timeout_seconds'])
    payload = event(runtime.test.a, check_run_id='controller-unavailable')
    runtime._on_runtime_leaf_recheck_result_early(payload)
    wait_done(runtime)
    assert delays == [5, 15, 30]
    assert reads == [.35] * 3
    assert 'prewarm' not in runtime.test.calls and 'DELETE' not in runtime.test.calls and '204' not in runtime.test.calls
    pending = runtime._runtime_passive_failback_readbacks['incident']
    assert pending['running'] is False and 'payloads' not in pending
    runtime._on_runtime_leaf_recheck_result_early(payload)
    wait_done(runtime)
    assert len(reads) == 3


def test_missing_snapshot_resume_uses_same_pass_after_controller_recovers(runtime):
    missing = {'value': True}
    runtime.stop_event = SimpleNamespace(is_set=lambda: False, set=lambda: None, wait=lambda _delay: False)
    original_state = runtime._runtime_route_snapshot_state
    runtime._runtime_route_snapshot_state = lambda: (None, None) if missing['value'] else original_state()
    original_refresh = runtime._refresh_runtime_route_snapshot
    def refresh(**kwargs):
        missing['value'] = False
        return original_refresh(**kwargs)
    runtime._refresh_runtime_route_snapshot = refresh
    runtime._on_runtime_leaf_recheck_result_early(event(runtime.test.a, check_run_id='recover-controller'))
    wait_done(runtime)
    assert runtime._runtime_route_mutation_lease is None
    assert runtime.test.calls.count('DELETE') == 1


def test_lower_priority_pass_during_missing_snapshot_does_not_erase_recovery(runtime):
    entered, release = threading.Event(), threading.Event()
    missing = {'value': True}
    original_state = runtime._runtime_route_snapshot_state
    runtime._runtime_route_snapshot_state = lambda: (None, None) if missing['value'] else original_state()
    def wait(_delay):
        entered.set()
        assert release.wait(1)
        return False
    runtime.stop_event = SimpleNamespace(is_set=lambda: False, set=lambda: release.set(), wait=wait)
    original_refresh = runtime._refresh_runtime_route_snapshot
    def refresh(**kwargs):
        missing['value'] = False
        return original_refresh(**kwargs)
    runtime._refresh_runtime_route_snapshot = refresh
    runtime._on_runtime_leaf_recheck_result_early(event(runtime.test.a, check_run_id='upper-pass'))
    assert entered.wait(1)
    runtime._on_runtime_leaf_recheck_result_early(event(runtime.test.b, check_run_id='lower-pass'))
    release.set()
    wait_done(runtime)
    assert runtime._runtime_route_mutation_lease is None
    assert runtime.test.calls.count('DELETE') == 1


@pytest.mark.parametrize('change', ['manual', 'worker'])
def test_missing_snapshot_retry_cannot_cross_new_intent_or_core_generation(runtime, change):
    runtime._runtime_route_snapshot_state = lambda: (None, None)
    reads = []
    runtime._refresh_runtime_route_snapshot = lambda **_kwargs: reads.append(True)
    def wait(_delay):
        if change == 'manual':
            runtime._runtime_selector_intent_revision = 2
        else:
            runtime._runtime_worker_token_is_current = lambda _token: False
        return False
    runtime.stop_event = SimpleNamespace(is_set=lambda: False, set=lambda: None, wait=wait)
    runtime._on_runtime_leaf_recheck_result_early(event(runtime.test.a, check_run_id='superseded'))
    wait_done(runtime)
    assert reads == []
    assert runtime.test.lease['release_state'] == 'superseded'


def test_lower_priority_and_other_gateway_pass_do_not_release(runtime):
    assert runtime._runtime_passive_failback_target(runtime.test.snapshot, runtime.test.payload, runtime.test.lease, runtime.test.b) is None
    assert runtime._runtime_passive_failback_target(runtime.test.snapshot, runtime.test.payload, runtime.test.lease, RuntimeNodeRef('exit', 'gateway', 'other', 'gateway')) is None


def nested_entries(runtime):
    entries = runtime.test.payload['proxies']['proxies']
    entries['PROXY-FALLBACK']['all'] = ['TIER1-SELECT', 'b']
    entries['TIER1-SELECT'] = {'type': 'Selector', 'now': 'TIER1-AUTO', 'all': ['TIER1-AUTO', 'manual']}
    entries['TIER1-AUTO'] = {'type': 'Fallback', 'now': 'physical', 'all': ['physical', 'c']}
    entries['physical'] = {'type': 'Selector', 'now': 'protocols', 'all': ['protocols', 'c']}
    entries['protocols'] = {'type': 'Fallback', 'now': 'c', 'all': ['c', 'a']}
    entries['manual'] = {'type': 'HTTP'}
    return entries


def test_nested_unselected_recovered_leaf_has_legal_inner_first_path(runtime):
    entries = nested_entries(runtime)
    assert preferred_fallback_path(entries, 'PROXY-FALLBACK', 'b', 'a') == ('PROXY-FALLBACK', 'TIER1-SELECT', 'TIER1-AUTO', 'physical', 'protocols', 'a')


@pytest.mark.parametrize('choice', ['selector', 'fixed', 'cycle'])
def test_nested_recovery_cannot_override_inner_user_choice_or_cycle(runtime, choice):
    entries = nested_entries(runtime)
    if choice == 'selector':
        entries['TIER1-SELECT']['now'] = 'manual'
    elif choice == 'fixed':
        entries['protocols']['fixed'] = 'c'
    else:
        entries['physical'].update(now='TIER1-AUTO', all=['TIER1-AUTO'])
    assert preferred_fallback_path(entries, 'PROXY-FALLBACK', 'b', 'a') is None


def test_nested_pass_refreshes_native_health_inside_out_before_outer_release(runtime):
    entries = nested_entries(runtime)
    def prewarm(_snapshot, action, **_kwargs):
        runtime.test.calls.append(('health', action['group'], action['target']))
        if action['group'] == 'protocols':
            entries['protocols']['now'] = 'a'
        return {'a': 5}
    runtime._prewarm_runtime_switch_target = prewarm
    runtime.test_group_delay = lambda *_args, **_kwargs: pytest.fail('unrelated group-wide probe')
    assert runtime._attempt_runtime_passive_failback(request(runtime)) == 'released'
    assert [('health', 'protocols', 'a'), ('health', 'TIER1-AUTO', 'physical'), ('health', 'PROXY-FALLBACK', 'TIER1-SELECT')] == [call for call in runtime.test.calls if isinstance(call, tuple) and call[0] == 'health']
    assert runtime.test.calls.index(('health', 'TIER1-AUTO', 'physical')) < runtime.test.calls.index('DELETE')


def test_nested_user_choice_changed_during_health_refresh_prevents_release(runtime):
    entries = nested_entries(runtime)
    def prewarm(*_args, **_kwargs):
        entries['TIER1-SELECT']['now'] = 'manual'
        return {'a': 5}
    runtime._prewarm_runtime_switch_target = prewarm
    assert runtime._attempt_runtime_passive_failback(request(runtime)) == 'pending'
    assert 'DELETE' not in runtime.test.calls
    assert runtime.test.state['fixed'] == 'b'
