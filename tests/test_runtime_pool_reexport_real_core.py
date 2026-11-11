"""Fix 1 (``exhausted_pool_reexport``) through the product path on a real Core.

``DesktopRuntime._runtime_pool_reexport_worker`` ->
``_sync_core_config_after_pool_update(allow_live_rebase=True)`` ->
``_update_running_tier3_core_provider`` (reload-gate lease, transactional
provider file write, ``PUT /providers/proxies/TIER3B-RUNTIME``, readback,
TIER3-AUTO membership check, contract commit) against the bundled Mihomo.

Only the inputs that are not on the path under test are stubbed: the online
phase / activation flags, the Tier3 manager (pool marker and healthy count)
and the renderer that would project the manager's healthy pool (it returns a
real ``project_tier3b_file_provider`` projection with one more member).
Everything is local: 127.0.0.1 HTTP-proxy nodes and a 127.0.0.1 ticker.
"""

from __future__ import annotations

import socket
import socketserver
import subprocess
import threading
import time
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

from desktop_runtime import DesktopRuntime
from mihomo_api import MihomoAPIClient
from runtime_egress_recovery import RuntimeEgressReadiness, RuntimeEgressReadinessState
from runtime_post_dial import DEFAULT_POST_DIAL_CONSTANTS
from tier3_core_provider import project_tier3b_file_provider
from tests.test_runtime_sudden_failure import (
    _Origin,
    _Proxy,
    _reserve_listener,
    _serve,
)

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / 'bin' / 'mihomo.exe'


class _Ticker(socketserver.ThreadingTCPServer):
    """Long-lived stream: one 64-byte chunk every 50 ms until the peer closes."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self):
        super().__init__(('127.0.0.1', 0), _TickerHandler)
        self.resets = 0

    def handle_error(self, *_args):
        pass


class _TickerHandler(socketserver.BaseRequestHandler):
    def handle(self):
        stop = self.server.shutdown_event
        try:
            while not stop.is_set():
                self.request.sendall(b'x' * 64)
                time.sleep(.05)
        except OSError:
            self.server.resets += 1


def _document(nodes, *, members, mixed_port, controller_port, secret, url):
    contract = {'url': url, 'interval': 3600, 'lazy': False, 'timeout': 5000,
                'expected-status': 204, 'max-failed-times': 2}
    return {
        'mixed-port': mixed_port, 'allow-lan': False, 'bind-address': '127.0.0.1',
        'mode': 'rule', 'log-level': 'info', 'ipv6': False,
        'external-controller': f'127.0.0.1:{controller_port}', 'secret': secret,
        'proxies': [
            {'name': name, 'type': 'http', 'server': '127.0.0.1',
             'port': nodes[name].server_address[1]}
            for name in members
        ],
        'proxy-groups': [
            {'name': 'TIER3-AUTO', 'type': 'fallback', 'empty-fallback': 'REJECT',
             'proxies': [*members, 'REJECT'], **contract},
            {'name': 'PROXY-FALLBACK', 'type': 'fallback', 'empty-fallback': 'REJECT',
             'proxies': ['TIER3-AUTO'], **contract},
            {'name': 'FINAL-EXIT', 'type': 'select', 'empty-fallback': 'REJECT',
             'proxies': ['PROXY-FALLBACK', 'REJECT']},
            {'name': 'GENERAL', 'type': 'select', 'empty-fallback': 'REJECT',
             'proxies': ['FINAL-EXIT']},
            {'name': 'GLOBAL', 'type': 'select', 'empty-fallback': 'REJECT',
             'proxies': ['GENERAL', 'FINAL-EXIT']},
        ],
        'rules': ['MATCH,GENERAL'],
    }


def _proxy_map(projection):
    provider = yaml.safe_load(projection.provider_yaml)
    return {
        str(proxy['name']): {
            'proxy_name': str(proxy['name']), 'tier': 'tier3', 'pool_kind': 'tier3b',
            'identity': f"identity-{proxy['name']}", 'node': None, 'proxy': dict(proxy),
        }
        for proxy in provider['proxies'] if str(proxy.get('name') or '') != 'REJECT'
    }


def _open_tunnel(mixed_port, target_port):
    sock = socket.create_connection(('127.0.0.1', mixed_port), timeout=5)
    sock.sendall(f'CONNECT 127.0.0.1:{target_port} HTTP/1.1\r\n'
                 f'Host: 127.0.0.1:{target_port}\r\n\r\n'.encode('ascii'))
    head = b''
    while b'\r\n\r\n' not in head:
        chunk = sock.recv(1)
        assert chunk, 'tunnel closed during CONNECT'
        head += chunk
    assert b' 200' in head.split(b'\r\n', 1)[0], head
    return sock


class _Reader(threading.Thread):
    def __init__(self, sock):
        super().__init__(daemon=True)
        self.sock, self.total, self.error, self.eof = sock, 0, None, False

    def run(self):
        try:
            while True:
                data = self.sock.recv(65536)
                if not data:
                    self.eof = True
                    return
                self.total += len(data)
        except OSError as exc:
            self.error = exc


def _connections_to(client, target_port):
    payload = client._request('GET', '/connections') or {}
    return [
        item for item in (payload.get('connections') or [])
        if str((item.get('metadata') or {}).get('destinationPort')) == str(target_port)
    ]


def test_reexport_worker_grows_tier3_auto_on_real_core_and_keeps_open_connection(tmp_path):
    if not BINARY.is_file():
        pytest.skip('repository Mihomo is unavailable')
    with ExitStack() as stack:
        origin = _serve(stack, ThreadingHTTPServer(('127.0.0.1', 0), _Origin))
        ticker = _serve(stack, _Ticker())
        nodes = {f'n{index}': _serve(stack, _Proxy()) for index in (1, 2, 3, 4)}
        mixed_port, mixed_reservation = _reserve_listener(stack, udp=True)
        controller_port, controller_reservation = _reserve_listener(stack)
        secret = 'reexport-test-secret'
        url = f'http://127.0.0.1:{origin.server_port}/generate_204'
        shape = dict(mixed_port=mixed_port, controller_port=controller_port, secret=secret, url=url)
        old = project_tier3b_file_provider(
            _document(nodes, members=['n1', 'n2', 'n3'], **shape), ['n1', 'n2', 'n3'])
        new = project_tier3b_file_provider(
            _document(nodes, members=['n1', 'n2', 'n3', 'n4'], **shape), ['n1', 'n2', 'n3', 'n4'])
        assert old.main_yaml == new.main_yaml

        config = {
            'client': {
                'mihomo_runtime_config': str(tmp_path / 'config' / 'mihomo-runtime.yaml'),
                'mihomo_work_dir': str(tmp_path / 'core-home'),
                'runtime_state_path': str(tmp_path / 'runtime-state.json'),
                'egress_recovery': {'node_judgement': {
                    'post_dial_judge': 'act', 'exhausted_pool_reexport': True,
                    'tier_ladder_judge': False, 'overshoot_repair': False,
                    'rearm_on_stale_route': False}},
            },
            'alerts': {'log_file': str(tmp_path / 'monitor.log'), 'desktop': {'enabled': False}},
            # Fix 1's own provider transaction (the pre-11-02 lane).  On the
            # verified lane fix 1 only queues the pool-sync worker's write.
            'tiers': {'tier3': {'verified_provider_writes': False}},
        }
        runtime = DesktopRuntime(config, project_root=tmp_path)
        runtime._tier3_core_provider_name = 'TIER3B-RUNTIME'
        runtime._tier3_core_provider_filename = 'tier3b-runtime.yaml'
        config_path = runtime._runtime_config_path_for_client()
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(old.main_yaml, encoding='utf-8')
        provider_path = runtime._tier3_core_provider_file_path()
        provider_path.parent.mkdir(parents=True, exist_ok=True)
        provider_path.write_text(old.provider_yaml, encoding='utf-8')
        assert provider_path.parent == (tmp_path / 'core-home')

        mixed_reservation.close()
        controller_reservation.close()
        process = subprocess.Popen(
            [str(BINARY), '-d', str(provider_path.parent), '-f', str(config_path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )

        def stop():
            process.terminate()
            try:
                process.wait(5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(5)

        stack.callback(stop)
        lines: list[str] = []
        threading.Thread(
            target=lambda: [lines.append(raw.decode('utf-8', 'replace').rstrip())
                            for raw in process.stdout], daemon=True).start()
        client = MihomoAPIClient(base_url=f'http://127.0.0.1:{controller_port}',
                                 secret=secret, timeout=2.0)
        deadline = time.monotonic() + 15
        while True:
            try:
                client.proxy('TIER3-AUTO')
                for name in ('n1', 'n2', 'n3'):
                    client.proxy(name)
                break
            except Exception:
                if process.poll() is not None or time.monotonic() > deadline:
                    pytest.fail('bundled Mihomo did not start:\n' + '\n'.join(lines[-40:]))
                time.sleep(.05)
        client.select_proxy('FINAL-EXIT', 'PROXY-FALLBACK', timeout=2)
        for name in ('n1', 'n2', 'n3'):
            client.proxy_delay(name, url=url, timeout_ms=2000, expected_status=204,
                               verify_expected_status=True)
        before_group = client.proxy('TIER3-AUTO')
        members_before = list(before_group['all'])
        assert members_before == ['n1', 'n2', 'n3', 'REJECT']
        assert before_group['now'] == 'n1'

        # Runtime bound to that real Core (same shape as the P2 real-Core runtime).
        runtime.api_client = client
        runtime.core_runner = SimpleNamespace(
            is_running=lambda: True,
            status=lambda: {'pid': process.pid, 'process_generation': 1},
        )
        runtime._core_generation = 1
        runtime._core_runtime_phase = 'formal_capture'
        runtime._runtime_initial_activation_generation = 1
        runtime._runtime_proxy_name_map = lambda: _proxy_map(old)
        loaded = runtime._runtime_export_contract_for_payload(
            old.main_yaml, provider_payload=old.provider_yaml)
        loaded['runtime_config_path'] = str(config_path)
        runtime._runtime_loaded_export_contract = {**loaded, 'revision': 1}
        runtime._runtime_loaded_export_revision = 1
        runtime._tier3_core_provider_last_projection = old
        runtime._runtime_proxy_name_map = lambda: _proxy_map(new)
        assert runtime._refresh_runtime_route_snapshot(timeout_seconds=2.0) is not None
        manager = SimpleNamespace(lock=threading.RLock(), tier3_pool_revision=2,
                                  _tier3_enabled=lambda: True, effective_egress_ok=True,
                                  runtime_generation=int(runtime._runtime_generation))
        runtime.manager = manager
        runtime._runtime_egress_readiness = RuntimeEgressReadiness(
            state=RuntimeEgressReadinessState.READY, validation_active=False)

        # Long-lived connection through TIER3-AUTO's current member n1.
        sock = _open_tunnel(mixed_port, ticker.server_address[1])
        stack.callback(sock.close)
        reader = _Reader(sock)
        reader.start()
        time.sleep(.5)
        conns_before = _connections_to(client, ticker.server_address[1])
        assert len(conns_before) == 1, conns_before
        conn_id = conns_before[0]['id']
        assert conns_before[0]['chains'][0] == 'n1', conns_before[0]['chains']
        bytes_before = reader.total
        assert bytes_before > 0

        records: list[dict] = []
        sync_events: list[str] = []
        view = {'alive': 0, 'loaded': 3, 'exhausted': True}
        with ExitStack() as patches:
            # Inputs off the path under test: the healthy pool source and
            # its marker, and the render of that pool into a projection.
            patches.enter_context(patch.object(
                runtime, '_render_mihomo_runtime_payload_base_with_provider',
                return_value=(new.main_yaml, new, _proxy_map(new))))
            patches.enter_context(patch.object(
                runtime, '_tier3_core_sync_marker', return_value=('pool', 4)))
            patches.enter_context(patch.object(
                runtime, '_tier3_core_gate_check', return_value=(True, 4, 1, 'ready')))
            patches.enter_context(patch.object(
                runtime, '_log_runtime_post_dial_record',
                side_effect=lambda _level, record: records.append(dict(record))))
            real_record = runtime._record_tier3_core_sync_event
            patches.enter_context(patch.object(
                runtime, '_record_tier3_core_sync_event',
                side_effect=lambda event: (sync_events.append(event), real_record(event))))
            runtime._runtime_pool_reexport_worker(view, DEFAULT_POST_DIAL_CONSTANTS)

        assert records and records[-1]['outcome'] == 'committed', (records, lines[-30:])
        assert sync_events == ['semantic_change']
        after_group = client.proxy('TIER3-AUTO')
        members_after = list(after_group['all'])
        assert members_after == ['n1', 'n2', 'n3', 'n4', 'REJECT']
        assert provider_path.read_text(encoding='utf-8') == new.provider_yaml
        assert after_group['now'] == 'n1'

        time.sleep(1.0)
        conns_after = _connections_to(client, ticker.server_address[1])
        bytes_after = reader.total
        assert [item['id'] for item in conns_after] == [conn_id], conns_after
        assert reader.error is None and not reader.eof
        assert bytes_after > bytes_before
        assert ticker.resets == 0
        print(f'\nREEXPORT members_before={members_before} members_after={members_after} '
              f'conn_id={conn_id} chain={conns_after[0]["chains"]} '
              f'bytes_before={bytes_before} bytes_after={bytes_after} record={records[-1]}')
        runtime.stop_event.set()
