"""Online Tier3B growth across the cohort freeze on the bundled Mihomo (11-01).

``DesktopRuntime._sync_core_config_after_pool_update`` exactly as the pool-sync
worker calls it (no ``allow_live_rebase``) while a formal Core serves:
``_tier3_online_growth_state`` admits ordinary growth below the alive target
and ``_update_running_tier3_core_provider_leased`` commits only an append-only
projection whose first member is already TIER3-AUTO's unfixed pick.  The PUT
then natively re-selects that member, so an open stream keeps flowing and no
blind pick has to settle; while a later member serves, growth waits without
touching the Core.

Stubbed as in ``test_runtime_pool_reexport_real_core``: the online phase and
activation flags, the Tier3 manager (pool marker, background target) and the
renderer of that pool (a real ``project_tier3b_file_provider`` projection).
Everything is local: 127.0.0.1 HTTP-proxy nodes, origin and ticker.
"""

from __future__ import annotations

import subprocess
import threading
import time
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from desktop_runtime import DesktopRuntime, ProviderSyncDisposition
from mihomo_api import MihomoAPIClient
from runtime_egress_recovery import RuntimeEgressReadiness, RuntimeEgressReadinessState
from tier3_core_provider import project_tier3b_file_provider
from tests.test_runtime_pool_reexport_real_core import (
    _connections_to,
    _document,
    _open_tunnel,
    _proxy_map,
    _Reader,
    _Ticker,
)
from tests.test_runtime_sudden_failure import _Origin, _Proxy, _reserve_listener, _serve

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / 'bin' / 'mihomo.exe'
LOADED = ['n1', 'n2', 'n3']
GROWN = ['n1', 'n2', 'n3', 'n4', 'n5']
MARKER = ('pool', 4)


class _ServingCore:
    """A formal Core whose TIER3-AUTO serves LOADED, bound to a DesktopRuntime."""

    def __init__(self, stack, tmp_path, *, refused=(), verified=True):
        self.origin = _serve(stack, ThreadingHTTPServer(('127.0.0.1', 0), _Origin))
        self.ticker = _serve(stack, _Ticker())
        self.nodes = {name: _serve(stack, _Proxy()) for name in GROWN}
        for name in refused:
            self.nodes[name].refused.set()
        self.mixed_port, mixed_reservation = _reserve_listener(stack, udp=True)
        controller_port, controller_reservation = _reserve_listener(stack)
        secret = 'online-growth-test-secret'
        self.url = f'http://127.0.0.1:{self.origin.server_port}/generate_204'
        shape = dict(mixed_port=self.mixed_port, controller_port=controller_port,
                     secret=secret, url=self.url)
        self.old = project_tier3b_file_provider(_document(self.nodes, members=LOADED, **shape), LOADED)
        self.new = project_tier3b_file_provider(_document(self.nodes, members=GROWN, **shape), GROWN)
        assert self.old.main_yaml == self.new.main_yaml

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
        }
        if not verified:
            # The pre-11-02 provider lane (8e first-member growth, settle,
            # rollback), kept behind tiers.tier3.verified_provider_writes.
            config['tiers'] = {'tier3': {'verified_provider_writes': False}}
        runtime = DesktopRuntime(config, project_root=tmp_path)
        runtime._tier3_core_provider_name = 'TIER3B-RUNTIME'
        runtime._tier3_core_provider_filename = 'tier3b-runtime.yaml'
        config_path = runtime._runtime_config_path_for_client()
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(self.old.main_yaml, encoding='utf-8')
        self.provider_path = runtime._tier3_core_provider_file_path()
        self.provider_path.parent.mkdir(parents=True, exist_ok=True)
        self.provider_path.write_text(self.old.provider_yaml, encoding='utf-8')

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
                for name in LOADED:
                    client.proxy(name)
                break
            except Exception:
                if process.poll() is not None or time.monotonic() > deadline:
                    pytest.fail('bundled Mihomo did not start:\n' + '\n'.join(self.lines[-40:]))
                time.sleep(.05)
        client.select_proxy('FINAL-EXIT', 'PROXY-FALLBACK', timeout=2)
        for name in LOADED:
            try:
                client.proxy_delay(name, url=self.url, timeout_ms=2000, expected_status=204,
                                   verify_expected_status=True)
            except Exception:
                assert name in refused, name

        # Runtime bound to that real Core (same shape as the fix-1 real-Core test).
        runtime.api_client = client
        runtime.core_runner = SimpleNamespace(
            is_running=lambda: True,
            status=lambda: {'pid': process.pid, 'process_generation': 1},
        )
        runtime._core_generation = 1
        runtime._core_runtime_phase = 'formal_capture'
        runtime._runtime_initial_activation_generation = 1
        runtime._runtime_proxy_name_map = lambda: _proxy_map(self.old)
        loaded = runtime._runtime_export_contract_for_payload(
            self.old.main_yaml, provider_payload=self.old.provider_yaml)
        loaded['runtime_config_path'] = str(config_path)
        runtime._runtime_loaded_export_contract = {**loaded, 'revision': 1}
        runtime._runtime_loaded_export_revision = 1
        runtime._tier3_core_provider_last_projection = self.old
        runtime._runtime_proxy_name_map = lambda: _proxy_map(self.new)
        assert runtime._refresh_runtime_route_snapshot(timeout_seconds=2.0) is not None
        runtime.manager = SimpleNamespace(
            lock=threading.RLock(), tier3_pool_revision=2, _tier3_enabled=lambda: True,
            effective_egress_ok=True, runtime_generation=int(runtime._runtime_generation),
            _tier3_background_target_alive_nodes=lambda: 25)
        runtime._runtime_egress_readiness = RuntimeEgressReadiness(
            state=RuntimeEgressReadinessState.READY, validation_active=False)
        self.runtime = runtime
        self.client = client

    def members(self):
        return list(self.client.proxy('TIER3-AUTO')['all'])

    def sync(self, *, marker=lambda: MARKER):
        """One pool-sync attempt as the worker makes it, on the grown pool."""

        with ExitStack() as patches:
            patches.enter_context(patch.object(
                self.runtime, '_render_mihomo_runtime_payload_base_with_provider',
                return_value=(self.new.main_yaml, self.new, _proxy_map(self.new))))
            patches.enter_context(patch.object(
                self.runtime, '_tier3_core_sync_marker', side_effect=marker))
            return self.runtime._sync_core_config_after_pool_update()


def _skip_without_core():
    if not BINARY.is_file():
        pytest.skip('repository Mihomo is unavailable')


def test_growth_appends_while_the_first_member_serves_and_the_stream_keeps_flowing(tmp_path):
    _skip_without_core()
    with ExitStack() as stack:
        core = _ServingCore(stack, tmp_path)
        assert core.members() == [*LOADED, 'REJECT']
        assert core.client.proxy('TIER3-AUTO')['now'] == 'n1'
        assert core.runtime._tier3_online_provider_cohort_frozen() is True
        assert core.runtime._tier3_online_growth_state() == (True, 'eligible')

        sock = _open_tunnel(core.mixed_port, core.ticker.server_address[1])
        stack.callback(sock.close)
        reader = _Reader(sock)
        reader.start()
        time.sleep(.5)
        conns_before = _connections_to(core.client, core.ticker.server_address[1])
        assert len(conns_before) == 1, conns_before
        assert conns_before[0]['chains'][0] == 'n1', conns_before[0]['chains']
        bytes_before = reader.total
        assert bytes_before > 0

        outcome = core.sync()

        assert outcome.disposition is ProviderSyncDisposition.COMMITTED, (outcome, core.lines[-30:])
        after_group = core.client.proxy('TIER3-AUTO')
        assert list(after_group['all']) == [*GROWN, 'REJECT']
        assert after_group['now'] == 'n1'
        assert core.provider_path.read_text(encoding='utf-8') == core.new.provider_yaml
        assert core.runtime._last_tier3_core_sync_marker == MARKER

        time.sleep(1.0)
        conns_after = _connections_to(core.client, core.ticker.server_address[1])
        assert [item['id'] for item in conns_after] == [conns_before[0]['id']], conns_after
        assert reader.error is None and not reader.eof
        assert reader.total > bytes_before
        assert core.ticker.resets == 0
        print(f'\nGROWTH members_after={list(after_group["all"])} now={after_group["now"]} '
              f'chain={conns_after[0]["chains"]} bytes_before={bytes_before} '
              f'bytes_after={reader.total}')


def test_growth_waits_without_touching_the_core_while_a_later_member_serves(tmp_path):
    # Legacy lane only; the verified lane commits this case
    # (tests/test_tier3_verified_provider_writes_real_core.py).
    _skip_without_core()
    with ExitStack() as stack:
        core = _ServingCore(stack, tmp_path, refused=('n1',), verified=False)
        assert core.client.proxy('TIER3-AUTO')['now'] == 'n2'
        assert core.runtime._tier3_online_growth_state() == (True, 'eligible')
        provider_before = core.provider_path.read_bytes()

        outcome = core.sync()

        assert outcome.disposition is ProviderSyncDisposition.PARKED, (outcome, core.lines[-30:])
        assert outcome.reason == 'online growth waits until TIER3-AUTO selects its first member'
        assert core.provider_path.read_bytes() == provider_before
        assert core.members() == [*LOADED, 'REJECT']
        assert core.client.proxy('TIER3-AUTO')['now'] == 'n2'
        assert core.runtime._tier3_online_growth_waits == 1
        assert not core.runtime._tier3_online_growth_retry_due()


def test_an_admission_landing_mid_transaction_does_not_roll_growth_back(tmp_path):
    _skip_without_core()
    with ExitStack() as stack:
        core = _ServingCore(stack, tmp_path)
        landed = threading.Event()
        put = core.client.update_proxy_provider

        def put_then_admit(*args, **kwargs):
            result = put(*args, **kwargs)
            landed.set()  # the Manager appends one more admission meanwhile
            return result

        core.client.update_proxy_provider = put_then_admit

        outcome = core.sync(marker=lambda: (*MARKER, 'n6') if landed.is_set() else MARKER)

        assert landed.is_set()
        assert outcome.disposition is ProviderSyncDisposition.COMMITTED, (outcome, core.lines[-30:])
        assert core.members() == [*GROWN, 'REJECT']
        assert core.client.proxy('TIER3-AUTO')['now'] == 'n1'
        # The loaded projection is recorded, not the later pool, so the next
        # sync still sees (and appends) the admission that landed meanwhile.
        assert core.runtime._last_tier3_core_sync_marker == MARKER
