"""Verified Tier3B provider writes on the bundled Mihomo 1.19.27 (11-02).

The single TIER3B-RUNTIME provider is the main path.  These tests drive the
real ``DesktopRuntime._sync_core_config_after_pool_update`` (the pool-sync
worker's call) against a real Mihomo with local 127.0.0.1 fixtures (alive,
refused and black-hole HTTP-proxy nodes).  Only the Manager side is stubbed:
the renderer returns a real ``project_tier3b_file_provider`` projection of the
requested pool and the pool marker follows it.

What a write must guarantee (single-provider parity with the shard plan):
the live pick never moves because of a write, every member is tested
afterwards, failures never roll the Core back by a timer, and dead or kicked
members leave without a blind window.
"""

from __future__ import annotations

import os
import socket
import subprocess
import threading
import time
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

import desktop_runtime as desktop_runtime_module
from desktop_runtime import DesktopRuntime, ProviderSyncDisposition
from mihomo_api import MihomoAPIClient, MihomoAPIError, MihomoDelayTestFailed
from runtime_egress_recovery import RuntimeEgressReadiness, RuntimeEgressReadinessState
from tier3_core_provider import Tier3CoreProviderProjection, project_tier3b_file_provider
from tests.test_runtime_online_growth_real_core import BINARY, GROWN, _ServingCore, _skip_without_core
from tests.test_runtime_pool_reexport_real_core import (
    _connections_to,
    _document,
    _open_tunnel,
    _proxy_map,
    _Reader,
    _Ticker,
)
from tests.test_runtime_sudden_failure import _Origin, _Proxy, _reserve_listener, _serve
from tier3_provider_health import member_verdicts, verify_provider_members

PROVIDER = 'TIER3B-RUNTIME'


def _member_test(client, url):
    def run(name):
        try:
            client.provider_member_healthcheck(PROVIDER, name, url=url, timeout_ms=5000,
                                               expected_status=204)
        except MihomoDelayTestFailed:
            pass  # a negative verdict; it is read back from extra[url]
    return run


def test_member_healthcheck_tests_a_rebuild_whose_own_check_was_skipped(tmp_path):
    _skip_without_core()
    with ExitStack() as stack:
        core = _ServingCore(stack, tmp_path)
        client = core.client
        core.nodes['n1'].refused.set()
        # A provider check that ends now opens Mihomo's 1 s singledo window:
        # the rebuild's own post-write check then reuses it and tests nothing.
        client.health_check_provider(PROVIDER, timeout=8)
        core.provider_path.write_text(core.new.provider_yaml, encoding='utf-8')
        client.update_proxy_provider(PROVIDER, timeout=2)
        before = client.proxy_provider(PROVIDER, timeout=2)
        assert member_verdicts(before, core.url) == {}, 'the rebuild was tested by Mihomo itself'
        assert client.proxy('TIER3-AUTO')['now'] == 'n1'  # the revived dead first member

        started = time.time()
        outcome = verify_provider_members(
            GROWN, test_member=_member_test(client, core.url),
            read_provider=lambda: client.proxy_provider(PROVIDER, timeout=2),
            test_url=core.url, started_wall=started,
            deadline_monotonic=time.monotonic() + 7.0)

        assert outcome.complete, outcome
        assert outcome.dead == ('n1',)
        assert set(outcome.alive) == set(GROWN) - {'n1'}
        assert client.proxy('TIER3-AUTO')['now'] == 'n2'
        print(f'\nVERIFY elapsed_ms={outcome.elapsed_ms:.0f} alive={outcome.alive} dead={outcome.dead}')


class _Lab:
    """A formal Core serving ``loaded`` (out of ``names`` fixtures), bound to a DesktopRuntime."""

    def __init__(self, stack, tmp_path, *, names, loaded, refused=(), blackhole=(), extra_config=None):
        self.origin = _serve(stack, ThreadingHTTPServer(('127.0.0.1', 0), _Origin))
        self.ticker = _serve(stack, _Ticker())
        self.nodes = {name: _serve(stack, _Proxy()) for name in names}
        for name in refused:
            self.nodes[name].refused.set()
        for name in blackhole:
            self.nodes[name].failed.set()
        self.mixed_port, mixed_reservation = _reserve_listener(stack, udp=True)
        controller_port, controller_reservation = _reserve_listener(stack)
        secret = 'verified-writes-test-secret'
        self.url = f'http://127.0.0.1:{self.origin.server_port}/generate_204'
        self.shape = dict(mixed_port=self.mixed_port, controller_port=controller_port,
                          secret=secret, url=self.url)
        initial = self.projection(loaded)

        config = {
            'client': {
                'mihomo_runtime_config': str(tmp_path / 'config' / 'mihomo-runtime.yaml'),
                'mihomo_work_dir': str(tmp_path / 'core-home'),
                'runtime_state_path': str(tmp_path / 'runtime-state.json'),
                'egress_recovery': {'node_judgement': {
                    'post_dial_judge': 'act', 'exhausted_pool_reexport': False,
                    'tier_ladder_judge': False, 'overshoot_repair': False,
                    'rearm_on_stale_route': False}},
            },
            'alerts': {'log_file': str(tmp_path / 'monitor.log'), 'desktop': {'enabled': False}},
            **(extra_config or {}),
        }
        runtime = DesktopRuntime(config, project_root=tmp_path)
        runtime._tier3_core_provider_name = PROVIDER
        runtime._tier3_core_provider_filename = 'tier3b-runtime.yaml'
        config_path = runtime._runtime_config_path_for_client()
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(initial.main_yaml, encoding='utf-8')
        self.provider_path = runtime._tier3_core_provider_file_path()
        self.provider_path.parent.mkdir(parents=True, exist_ok=True)
        self.provider_path.write_text(initial.provider_yaml, encoding='utf-8')

        mixed_reservation.close()
        controller_reservation.close()
        process = subprocess.Popen(
            [str(BINARY), '-d', str(self.provider_path.parent), '-f', str(config_path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )

        def stop():
            runtime.stop_event.set()
            process.terminate()
            try:
                process.wait(5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(5)

        stack.callback(stop)
        self.lines: list[str] = []
        threading.Thread(
            target=lambda: [self.lines.append(raw.decode('utf-8', 'replace').rstrip())
                            for raw in process.stdout], daemon=True).start()
        client = MihomoAPIClient(base_url=f'http://127.0.0.1:{controller_port}',
                                 secret=secret, timeout=2.0)
        deadline = time.monotonic() + 15
        while True:
            try:
                client.proxy('TIER3-AUTO')
                break
            except Exception:
                if process.poll() is not None or time.monotonic() > deadline:
                    pytest.fail('bundled Mihomo did not start:\n' + '\n'.join(self.lines[-40:]))
                time.sleep(.05)
        client.select_proxy('FINAL-EXIT', 'PROXY-FALLBACK', timeout=2)
        self.client = client
        # Explicit verdicts for the loaded members, then 1.5 s so the first
        # write never lands inside a startup-check singledo window by accident.
        verify_provider_members(
            list(loaded), test_member=_member_test(client, self.url),
            read_provider=lambda: client.proxy_provider(PROVIDER, timeout=2),
            test_url=self.url, started_wall=time.time() - 3600,
            deadline_monotonic=time.monotonic() + 7.0)
        time.sleep(1.5)

        runtime.api_client = client
        runtime.core_runner = SimpleNamespace(
            is_running=lambda: True,
            status=lambda: {'pid': process.pid, 'process_generation': 1},
        )
        runtime._core_generation = 1
        runtime._core_runtime_phase = 'formal_capture'
        runtime._runtime_initial_activation_generation = 1
        contract = runtime._runtime_export_contract_for_payload(
            initial.main_yaml, provider_payload=initial.provider_yaml,
            rendered_proxy_map=_proxy_map(initial))
        contract['runtime_config_path'] = str(config_path)
        runtime._runtime_loaded_export_contract = {**contract, 'revision': 1}
        runtime._runtime_loaded_export_revision = 1
        runtime._tier3_core_provider_last_projection = initial
        runtime._runtime_proxy_name_map = lambda: _proxy_map(initial)
        assert runtime._refresh_runtime_route_snapshot(timeout_seconds=2.0) is not None
        runtime.manager = SimpleNamespace(
            lock=threading.RLock(), tier3_pool_revision=2, _tier3_enabled=lambda: True,
            effective_egress_ok=True, runtime_generation=int(runtime._runtime_generation),
            _tier3_background_target_alive_nodes=lambda: 25)
        runtime._runtime_egress_readiness = RuntimeEgressReadiness(
            state=RuntimeEgressReadinessState.READY, validation_active=False)
        self.runtime = runtime

    def projection(self, members) -> Tier3CoreProviderProjection:
        members = list(members)
        return project_tier3b_file_provider(_document(self.nodes, members=members, **self.shape), members)

    def sync(self, members=None, *, projection=None):
        projection = projection or self.projection(members)
        marker = ('pool', tuple(projection.proxy_names))
        with ExitStack() as patches:
            patches.enter_context(patch.object(
                self.runtime, '_render_mihomo_runtime_payload_base_with_provider',
                return_value=(projection.main_yaml, projection, _proxy_map(projection))))
            patches.enter_context(patch.object(
                self.runtime, '_tier3_core_sync_marker', return_value=marker))
            self.runtime._runtime_proxy_name_map = lambda: _proxy_map(projection)
            return self.runtime._sync_core_config_after_pool_update()

    def group(self):
        return self.client.proxy('TIER3-AUTO')

    def members(self):
        return [name for name in self.group()['all'] if name != 'REJECT']

    def verdicts(self, *, newer_than=None):
        return member_verdicts(self.client.proxy_provider(PROVIDER, timeout=2), self.url,
                               newer_than_wall=newer_than)


class _Watch:
    """Poll TIER3-AUTO's pick and dial through the mixed port while a sync runs."""

    def __init__(self, lab, *, period=0.02, dial_period=0.05):
        self.lab, self.period, self.dial_period = lab, period, dial_period
        self.picks: list[tuple[float, str, str]] = []
        self.dials: list[tuple[float, bool]] = []
        self.stop = threading.Event()
        self.threads = [threading.Thread(target=self._poll, daemon=True),
                        threading.Thread(target=self._dial, daemon=True)]

    def _poll(self):
        while not self.stop.is_set():
            try:
                group = self.lab.client.proxy('TIER3-AUTO', timeout=1)
                self.picks.append((time.monotonic(), str(group.get('now') or ''), str(group.get('fixed') or '')))
            except Exception:
                pass
            self.stop.wait(self.period)

    def _dial(self):
        port = self.lab.ticker.server_address[1]
        while not self.stop.is_set():
            ok = False
            try:
                with socket.create_connection(('127.0.0.1', self.lab.mixed_port), timeout=6) as sock:
                    sock.sendall(f'CONNECT 127.0.0.1:{port} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n'.encode())
                    head = sock.recv(64)
                    ok = b' 200' in head.split(b'\r\n', 1)[0]
            except OSError:
                ok = False
            self.dials.append((time.monotonic(), ok))
            self.stop.wait(self.dial_period)

    def __enter__(self):
        for thread in self.threads:
            thread.start()
        time.sleep(0.3)
        return self

    def __exit__(self, *_exc):
        time.sleep(0.3)
        self.stop.set()
        for thread in self.threads:
            thread.join(8)

    def distinct_picks(self):
        return sorted({now for _t, now, _fixed in self.picks})

    def failed_dials(self):
        return sum(1 for _t, ok in self.dials if not ok)


def _lab(stack, tmp_path, **kwargs):
    _skip_without_core()
    return _Lab(stack, tmp_path, **kwargs)


@pytest.mark.skipif(os.environ.get("GITHUB_ACTIONS") == "true",
                    reason="speed gate against real helper processes or a real Core; "
                    "the release runner is slower than these budgets (runs locally)")
def test_growth_while_a_later_member_serves_keeps_the_pick_and_tests_every_member(tmp_path):
    """AC1/AC2/AC3/AC10/AC11: the 8e first-member wait is gone."""

    with ExitStack() as stack:
        lab = _lab(stack, tmp_path, names=['n1', 'n2', 'n3', 'n4', 'n5'],
                   loaded=['n1', 'n2', 'n3'], refused=['n1'])
        assert lab.group()['now'] == 'n2'
        runtime = lab.runtime
        epoch = runtime._runtime_route_epoch
        digest = runtime._runtime_route_snapshot.semantic_digest
        sock = _open_tunnel(lab.mixed_port, lab.ticker.server_address[1])
        stack.callback(sock.close)
        reader = _Reader(sock)
        reader.start()
        time.sleep(0.4)
        tunnel = _connections_to(lab.client, lab.ticker.server_address[1])
        assert tunnel and tunnel[0]['chains'][0] == 'n2'
        write_start = time.time()

        with _Watch(lab) as watch:
            outcome = lab.sync(['n1', 'n2', 'n3', 'n4', 'n5'])

        assert outcome.disposition is ProviderSyncDisposition.COMMITTED, (outcome, lab.lines[-20:])
        assert lab.members() == ['n2', 'n3', 'n4', 'n5', 'n1']
        assert watch.distinct_picks() == ['n2'], watch.picks[:10]
        assert watch.failed_dials() == 0 and len(watch.dials) > 10
        after = _connections_to(lab.client, lab.ticker.server_address[1])
        assert [item['id'] for item in after] == [tunnel[0]['id']] and reader.error is None
        fresh = lab.verdicts(newer_than=write_start)
        # Every member was tested on its rebuilt object, except possibly the
        # pick itself (not retested while its verdict is fresh).
        assert set(fresh) >= {'n1', 'n3', 'n4', 'n5'}, fresh
        assert fresh['n1'][0] is False and all(fresh[name][0] for name in ('n3', 'n4', 'n5'))
        assert runtime._runtime_route_epoch == epoch
        assert runtime._runtime_route_snapshot.semantic_digest == digest
        view = runtime._published_tier3_route_view()
        assert (view['now'], view['alive'], view['loaded']) == ('n2', 4, 5), view
        assert runtime._last_failed_tier3_core_sync_marker is None


def test_a_rejected_provider_file_parks_and_keeps_the_loaded_core(tmp_path):
    """AC5: a real parse rejection restores the file and rebuilds nothing."""

    with ExitStack() as stack:
        lab = _lab(stack, tmp_path, names=['n1', 'n2', 'n3'], loaded=['n1', 'n2'])
        bad = lab.projection(['n1', 'n2', 'n3'])
        document = yaml.safe_load(bad.provider_yaml)
        document['proxies'][2]['type'] = 'not-a-protocol'
        bad = Tier3CoreProviderProjection(**{
            **{field: getattr(bad, field) for field in bad.__dataclass_fields__},
            'provider_yaml': yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
        })
        before = lab.provider_path.read_bytes()
        history_before = {name: len(lab.client.proxy_provider(PROVIDER)['proxies'][i]['extra'][lab.url]['history'])
                          for i, name in enumerate(['n1', 'n2'])}

        outcome = lab.sync(projection=bad)

        assert outcome.disposition is ProviderSyncDisposition.PARKED, outcome
        assert lab.provider_path.read_bytes() == before
        assert lab.members() == ['n1', 'n2'] and lab.group()['now'] == 'n1'
        # Nothing was rebuilt: the members kept their test history.
        history_after = {name: len(lab.client.proxy_provider(PROVIDER)['proxies'][i]['extra'][lab.url]['history'])
                         for i, name in enumerate(['n1', 'n2'])}
        assert history_after == history_before
        assert lab.runtime._last_failed_tier3_core_sync_marker is None


def test_a_put_transport_failure_still_commits_what_the_file_watcher_loaded(tmp_path):
    """AC5: the file write is the commit; a lost PUT answer is not undone."""

    with ExitStack() as stack:
        lab = _lab(stack, tmp_path, names=['n1', 'n2', 'n3'], loaded=['n1', 'n2'])

        def lost(*_args, **_kwargs):
            raise MihomoAPIError('PUT /providers/proxies/TIER3B-RUNTIME failed: timed out')

        lab.client.update_proxy_provider = lost
        with _Watch(lab) as watch:
            outcome = lab.sync(['n1', 'n2', 'n3'])

        assert outcome.disposition is ProviderSyncDisposition.COMMITTED, (outcome, lab.lines[-20:])
        assert lab.members() == ['n1', 'n2', 'n3']
        assert watch.distinct_picks() == ['n1'] and watch.failed_dials() == 0


def test_eleven_dead_members_ahead_need_no_settle_timer(tmp_path):
    """AC6: the pick stays first; nothing waits for Mihomo's 10-member batches."""

    dead = [f'd{i}' for i in range(1, 12)]
    with ExitStack() as stack:
        lab = _lab(stack, tmp_path, names=[*dead, 'a12', 'a13'], loaded=[*dead, 'a12'], refused=dead)
        assert lab.group()['now'] == 'a12'
        with _Watch(lab) as watch:
            started = time.monotonic()
            outcome = lab.sync([*dead, 'a12', 'a13'])
            elapsed = time.monotonic() - started

        assert outcome.disposition is ProviderSyncDisposition.COMMITTED, (outcome, lab.lines[-20:])
        assert lab.members()[:2] == ['a12', 'a13']
        assert watch.distinct_picks() == ['a12'] and watch.failed_dials() == 0
        fresh = lab.verdicts()
        assert all(fresh[name][0] is False for name in dead)
        print(f'\nELEVEN_DEAD elapsed={elapsed:.2f}s')


def test_all_dead_moves_the_pick_to_a_verified_new_member(tmp_path):
    """AC7: no rollback to the dead first member."""

    with ExitStack() as stack:
        lab = _lab(stack, tmp_path, names=['d1', 'd2', 'a3', 'a4'], loaded=['d1', 'd2'],
                   refused=['d1', 'd2'])
        assert lab.group()['now'] == 'd1'  # all dead: Mihomo serves proxies[0]

        outcome = lab.sync(['d1', 'd2', 'a3', 'a4'])

        assert outcome.disposition is ProviderSyncDisposition.COMMITTED, (outcome, lab.lines[-20:])
        assert lab.members() == ['a3', 'a4', 'd1', 'd2']
        assert lab.group()['now'] == 'a3'
        assert lab.runtime._last_failed_tier3_core_sync_marker is None


def test_a_kicked_member_leaves_and_a_kicked_pick_leaves_after_its_grace(tmp_path, monkeypatch):
    """AC9: removals reach the Core online; the serving member moves to a verified one."""

    with ExitStack() as stack:
        lab = _lab(stack, tmp_path, names=['n1', 'n2', 'n3'], loaded=['n1', 'n2', 'n3'])
        with _Watch(lab) as watch:
            outcome = lab.sync(['n1', 'n2'])
        assert outcome.disposition is ProviderSyncDisposition.COMMITTED, (outcome, lab.lines[-20:])
        assert lab.members() == ['n1', 'n2'] and watch.distinct_picks() == ['n1']

        # The Manager kicks the serving member: it keeps its place within the grace.
        outcome = lab.sync(['n2'])
        assert outcome.disposition is ProviderSyncDisposition.NOOP, outcome
        assert lab.members() == ['n1', 'n2'] and lab.group()['now'] == 'n1'

        monkeypatch.setattr(desktop_runtime_module, '_TIER3_VERIFIED_KICKED_PICK_GRACE_SECONDS', 0.0)
        lab.runtime._tier3_provider_write_finished_at = 0.0
        outcome = lab.sync(['n2'])
        assert outcome.disposition is ProviderSyncDisposition.COMMITTED, (outcome, lab.lines[-20:])
        assert lab.members() == ['n2'] and lab.group()['now'] == 'n2'


def test_a_restored_pin_nobody_owns_is_released_without_moving_traffic(tmp_path):
    """AC8: a cache.db pin that no owner in this process holds is released
    (and so cleared from cache.db) once its member is first."""

    with ExitStack() as stack:
        lab = _lab(stack, tmp_path, names=['n1', 'n2', 'n3', 'n4'], loaded=['n1', 'n2', 'n3'])
        lab.client.select_proxy('TIER3-AUTO', 'n2', timeout=2)  # as cache.db restores it
        assert (lab.group()['fixed'], lab.group()['now']) == ('n2', 'n2')
        with _Watch(lab) as watch:
            outcome = lab.sync(['n1', 'n2', 'n3', 'n4'])
        assert outcome.disposition is ProviderSyncDisposition.COMMITTED, (outcome, lab.lines[-20:])
        group = lab.group()
        assert (group['fixed'], group['now']) == ('', 'n2')
        assert lab.members()[0] == 'n2'
        assert watch.distinct_picks() == ['n2'] and watch.failed_dials() == 0


def test_a_stale_pin_is_cleared_before_the_write(tmp_path):
    """AC8: a pin on a removed member never re-attaches to a re-admitted one."""

    with ExitStack() as stack:
        lab = _lab(stack, tmp_path, names=['n1', 'n2', 'n3', 'n4'], loaded=['n1', 'n2', 'n3'])
        lab.client.select_proxy('TIER3-AUTO', 'n3', timeout=2)
        assert lab.group()['fixed'] == 'n3'
        # A reload lane drops n3 behind the provider transaction's back.
        reload_lane = lab.projection(['n1', 'n2'])
        lab.provider_path.write_text(reload_lane.provider_yaml, encoding='utf-8')
        lab.client.update_proxy_provider(PROVIDER, timeout=2)
        assert lab.group()['fixed'] == 'n3' and 'n3' not in lab.members()

        outcome = lab.sync(['n1', 'n2', 'n3', 'n4'])

        assert outcome.disposition is ProviderSyncDisposition.COMMITTED, (outcome, lab.lines[-20:])
        group = lab.group()
        assert group['fixed'] == '' and group['now'] == 'n1'
        assert set(lab.members()) == {'n1', 'n2', 'n3', 'n4'}
