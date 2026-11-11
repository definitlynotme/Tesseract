from __future__ import annotations

import datetime
import http.client
import ipaddress
import secrets
import select
import socket
import socketserver
import ssl
import subprocess
import threading
import time
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from app_paths import apply_release_path_overrides, build_app_paths
from desktop_runtime import DesktopRuntime, _RuntimeAbortHandle
from manager import ProxyTierManager
from mihomo_api import MihomoAPIClient, MihomoDelayTransportFailed
from models import NodeStatus
from parser import parse_node_uri
from runtime_egress_recovery import (
    RuntimeEgressReadiness,
    RuntimeEgressReadinessState,
    RuntimeRecoveryStatus,
)
from runtime_post_dial import Trigger
from runtime_route_audit import inspect_runtime_route
from system_proxy import ProxySettings, SystemProxyManager
from tests.test_desktop_runtime_recovery import MemoryProxyBackend


class _Origin(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.proxy_authorization_received.append('Proxy-Authorization' in self.headers)
        # Mihomo treats a rounded 0 ms URL-test result as a failed test.
        time.sleep(.01)
        self.send_response(204)
        self.send_header('Content-Length', '0')
        self.end_headers()

    do_HEAD = do_GET

    def log_message(self, *_args):
        pass


class _Proxy(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self):
        super().__init__(('127.0.0.1', 0), _Tunnel)
        self.failed = threading.Event()
        self.refused = threading.Event()
        # Zombie (the 09-25 shape): the tunnel is accepted, then broken inside
        # it before any byte comes back.  Core sees no dial error.
        self.zombie = threading.Event()
        self.connected = 0

    def handle_error(self, *_args):
        pass


class _Tunnel(socketserver.StreamRequestHandler):
    def handle(self):
        line = self.rfile.readline().decode('ascii')
        while self.rfile.readline() not in (b'\r\n', b'\n', b''):
            pass
        if self.server.refused.is_set():
            return
        if self.server.failed.is_set():
            # Silent black hole: Core gets no terminal dial error until its
            # own timeout. Passive recovery starts only after that error.
            self.server.shutdown_event.wait(8)
            return
        if self.server.zombie.is_set():
            self.wfile.write(b'HTTP/1.1 200 Connection established\r\n\r\n')
            self.wfile.flush()
            self.connection.recv(65536)
            return
        method, target, _ = line.split()
        assert method == 'CONNECT'
        host, port = target.rsplit(':', 1)
        # The business hostname is deliberately non-local in Core's error
        # event, but this fixture never opens an external connection.
        if host == 'business.test':
            host = '127.0.0.1'
        with socket.create_connection((host, int(port)), timeout=2) as upstream:
            self.server.connected += 1
            self.wfile.write(b'HTTP/1.1 200 Connection established\r\n\r\n')
            self.wfile.flush()
            peers = (self.connection, upstream)
            while not self.server.shutdown_event.is_set():
                readable, _, _ = select.select(peers, (), (), .1)
                for peer in readable:
                    data = peer.recv(65536)
                    if not data:
                        return
                    (upstream if peer is self.connection else self.connection).sendall(data)


def _serve(stack, server):
    server.shutdown_event = threading.Event()
    server.proxy_authorization_received = []
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
    thread.start()
    def stop():
        server.shutdown_event.set()
        server.shutdown()
        server.server_close()
        thread.join(2)
    stack.callback(stop)
    return server


def _reserve_listener(stack, *, udp=False):
    # Keep fixture listeners out of the OS ephemeral-client range, and hold
    # every required transport until the native Core is about to start.
    for _ in range(100):
        reservation = ExitStack()
        port = 20000 + secrets.randbelow(20000)
        try:
            for kind in ((socket.SOCK_STREAM, socket.SOCK_DGRAM) if udp
                         else (socket.SOCK_STREAM,)):
                sock = reservation.enter_context(socket.socket(socket.AF_INET, kind))
                if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                sock.bind(('127.0.0.1', port))
        except OSError:
            reservation.close()
            continue
        stack.callback(reservation.close)
        return port, reservation
    raise RuntimeError('could not reserve local integration listener')


def test_mixed_fixture_reserves_both_transports_until_launch():
    with ExitStack() as stack:
        port, reservation = _reserve_listener(stack, udp=True)
        assert 20000 <= port < 40000
        for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
            with socket.socket(socket.AF_INET, kind) as probe:
                with pytest.raises(OSError):
                    probe.bind(('127.0.0.1', port))
        reservation.close()
        for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
            with socket.socket(socket.AF_INET, kind) as probe:
                probe.bind(('127.0.0.1', port))


def test_mixed_fixture_does_not_accept_tcp_free_but_udp_occupied(monkeypatch):
    with ExitStack() as stack:
        occupied = stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
        occupied.bind(('127.0.0.1', 0))
        occupied_port = occupied.getsockname()[1]
        random = secrets.randbelow
        draws = iter((occupied_port - 20000,))
        monkeypatch.setattr(secrets, 'randbelow', lambda limit: next(draws, random(limit)))
        port, _ = _reserve_listener(stack, udp=True)
        assert port != occupied_port
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', occupied_port))


@pytest.fixture
def local_runtime(tmp_path, monkeypatch, request):
    root = Path(__file__).resolve().parents[1]
    if not (root / 'bin' / 'mihomo.exe').is_file():
        pytest.skip('repository Mihomo is unavailable')
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now-datetime.timedelta(minutes=1))
            .not_valid_after(now+datetime.timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost'), x509.DNSName('business.test'), x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / 'cert.pem', tmp_path / 'key.pem'
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    original_context = ssl.create_default_context
    def trusted_context(*args, **kwargs):
        # These servers use only our temporary CA. Enumerating the Windows
        # root store for every fresh probe can consume several seconds and
        # turn a loopback timing check into a machine-dependent timeout.
        kwargs.setdefault('cafile', str(cert_path))
        context = original_context(*args, **kwargs)
        context.load_verify_locations(cafile=str(cert_path))
        return context
    monkeypatch.setattr(ssl, 'create_default_context', trusted_context)
    with ExitStack() as stack:
        plain = _serve(stack, ThreadingHTTPServer(('127.0.0.1', 0), _Origin))
        secure = ThreadingHTTPServer(('127.0.0.1', 0), _Origin)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(str(cert_path), str(key_path))
        secure.socket = tls.wrap_socket(secure.socket, server_side=True)
        _serve(stack, secure)
        business = ThreadingHTTPServer(('127.0.0.1', 0), _Origin)
        business.socket = tls.wrap_socket(business.socket, server_side=True)
        _serve(stack, business)
        case = getattr(request, 'param', 3)
        tiers = case['tiers'] if isinstance(case, dict) else ['tier2'] * case
        proxies = [_serve(stack, _Proxy()) for _ in tiers]
        mixed_port, mixed_reservation = _reserve_listener(stack, udp=True)
        controller_port, controller_reservation = _reserve_listener(stack)
        paths = build_app_paths(resource_root=root, config_dir=tmp_path, data_dir=tmp_path/'data')
        config = apply_release_path_overrides({
            'tiers': {'tier1': {'nodes': []}, 'tier2': {'nodes': [], 'check_interval': 3600}, 'tier3': {'enabled': False}, 'exit': {'nodes': []}},
            'client': {'network_mode': 'system_proxy', 'mihomo_mixed_port': mixed_port, 'system_proxy': {'enabled': False}, 'mihomo_controller': {'enabled': True, 'host': '127.0.0.1', 'port': controller_port}, 'dns': {'enable': False}},
            'testing': {'mihomo_helper': {'probe_urls': [f'http://127.0.0.1:{plain.server_port}/generate_204', f'https://localhost:{secure.server_port}/generate_204', f'https://127.0.0.1:{secure.server_port}/generate_204']}},
            'routing': {'rules': {'compatibility_defaults': []}},
            'geo_update': {'enabled': False},
            'killswitch': {'enabled': False},
        }, paths)
        for tier in set(tiers):
            config['tiers'][tier]['check_interval'] = 3600
            config['tiers'][tier]['nodes'] = [
                {'uri': f'http://test:test@127.0.0.1:{proxy.server_address[1]}#local-{index}'}
                for index, proxy in enumerate(proxies) if tiers[index] == tier
            ]
        runtime = DesktopRuntime(config, project_root=root, app_paths=paths)
        runtime.manager = ProxyTierManager(config=runtime.config, logger=runtime.logger, alerts=runtime.alerts)
        nodes = []
        for index, proxy in enumerate(proxies):
            node = parse_node_uri(f'http://test:test@127.0.0.1:{proxy.server_address[1]}#local-{index}')
            node.tier = tiers[index]
            node.status = NodeStatus.ALIVE
            node.latency_ms = 10
            nodes.append(node)
        for tier in set(tiers):
            runtime.manager.runtime_nodes[tier] = [node for node in nodes if node.tier == tier]
        runtime._default_probe_url = lambda: f'http://127.0.0.1:{plain.server_port}/generate_204'
        runtime._default_probe_urls = lambda: [f'https://localhost:{secure.server_port}/generate_204', f'https://127.0.0.1:{secure.server_port}/generate_204']
        runtime._integration_business_url = f'https://business.test:{business.server_port}/generate_204'
        runtime._integration_origin_proxy_auth = secure.proxy_authorization_received
        runtime._integration_errors = []
        output_sink = runtime._on_core_output_event
        def record_business_error(event):
            if 'business.test' in event.raw_line and 'error' in event.raw_line:
                runtime._integration_errors.append(event.received_at_monotonic)
            output_sink(event)
        runtime._on_core_output_event = record_business_error
        # No OS proxy or public endpoint participates in this integration.
        runtime._runtime_generation = 1
        runtime._lifecycle_revision = 1
        runtime._desired_running = True
        runtime.running = True
        runtime._attach_manager_runtime_hooks(runtime.manager)
        runner = runtime._get_core_runner()
        original_start = runner.start

        def start_reserved_core():
            mixed_reservation.close()
            controller_reservation.close()
            return original_start()

        monkeypatch.setattr(runner, 'start', start_reserved_core)
        try:
            yield runtime, proxies
        finally:
            runtime.stop(actor='test', reason='isolated sudden-failure integration complete')


def _start_and_verify(runtime, proxies):
    try:
        runtime.start_core()
    except Exception as exc:
        exc.add_note('\n'.join(runtime._get_core_runner().get_recent_logs(limit=80)))
        raise
    assert runtime._wait_for_tier3_egress_activation(timeout=18), runtime._tier3_initial_activation_trace_snapshot()
    assert runtime.manager.effective_egress_ok is True, runtime._tier3_initial_activation_trace_snapshot()
    assert runtime._probe_mihomo_mixed_port_once(timeout_seconds=2)['status'] == 204
    client = runtime._require_api_client()
    before = inspect_runtime_route(client.proxies())['leaf_proxy']
    assert 'local-0' in before
    assert proxies[0].connected > 0
    assert runtime._runtime_monitoring_is_active()
    return client


def _business_request(runtime, timeout=12):
    return runtime._abortable_https_204_via_http_proxy(
        proxy_host='127.0.0.1',
        proxy_port=runtime._client_mixed_port(runtime._client_config()),
        probe_url=runtime._integration_business_url,
        timeout_seconds=timeout,
        abort_owner=_RuntimeAbortHandle(),
    )


def _await_recovered(runtime, client, expected_leaf, *, timeout=8):
    started = time.monotonic()
    deadline = started + timeout
    while time.monotonic() < deadline:
        leaf = inspect_runtime_route(client.proxies())['leaf_proxy']
        # Core may already have switched before the application's confirmation;
        # a real new request, not a synthetic recovery outcome, proves service.
        if (leaf == expected_leaf and runtime._runtime_monitoring_is_active()
                and runtime._runtime_initial_activation_complete()):
            break
        time.sleep(.05)
    else:
        pytest.fail(str({'outcomes': runtime._runtime_recovery_outcomes, 'circuits': runtime._runtime_egress_coordinator.current_snapshots(), 'ready': runtime._runtime_initial_activation_complete(), 'armed': runtime._runtime_recovery_is_armed(), 'watch_at': runtime._runtime_path_watch_last_at, 'watch_thread': runtime._runtime_path_watch_thread, 'broker': runtime._runtime_probe_broker.snapshot(), 'monitor': runtime._runtime_failure_monitor_thread}))
    assert leaf == expected_leaf
    assert runtime._runtime_initial_activation_complete()
    assert not runtime._runtime_probe_broker.snapshot().poisoned
    assert _business_request(runtime, timeout=2)[0] == 204
    if runtime._integration_errors:
        elapsed = time.monotonic() - runtime._integration_errors[0]
        print(f'local business error recovered to {leaf} in {elapsed*1000:.0f} ms after Core error')


def test_real_business_errors_and_consecutive_failures_preserve_order(local_runtime):
    runtime, proxies = local_runtime
    client = _start_and_verify(runtime, proxies)
    for index in (0, 1):
        runtime._integration_errors.clear()
        proxies[index].refused.set()
        try:
            _business_request(runtime, timeout=2)
        except Exception:
            pass
        _await_recovered(runtime, client, f'local-{index + 1}')
        assert proxies[index + 1].connected > 0


@pytest.mark.parametrize('local_runtime', [6], indirect=True)
def test_real_core_reaches_fifth_backup_without_waiting_for_self_check(local_runtime):
    runtime, proxies = local_runtime
    client = _start_and_verify(runtime, proxies)
    for proxy in proxies[:5]:
        proxy.refused.set()
    try:
        _business_request(runtime, timeout=3)
    except Exception:
        pass
    _await_recovered(runtime, client, 'local-5')
    assert proxies[5].connected > 0
    assert runtime.config['tiers']['tier2']['check_interval'] == 3600


def test_native_dial_errors_trigger_ordered_fallback_before_periodic_checks(local_runtime):
    runtime, proxies = local_runtime
    # Exercise actual business errors with the passive application listener.
    client = _start_and_verify(runtime, proxies)
    proxies[0].refused.set()
    started = time.monotonic()
    for _ in range(2):
        try:
            _business_request(runtime, timeout=1)
        except Exception:
            pass
    while time.monotonic() - started < 3:
        leaf = inspect_runtime_route(client.proxies())['leaf_proxy']
        if leaf == 'local-1':
            break
        time.sleep(.025)
    assert leaf == 'local-1'
    assert runtime._probe_mihomo_mixed_port_once(timeout_seconds=2)['status'] == 204
    print(f'local immediate dial failure recovered in {(time.monotonic()-started)*1000:.0f} ms')


def test_failed_proxy_pool_never_bypasses_to_reachable_direct_origin(local_runtime, monkeypatch):
    runtime, proxies = local_runtime
    client = _start_and_verify(runtime, proxies)
    restored = []
    original_restore = runtime.restore_system_proxy

    def record_restore():
        restored.append(True)
        return original_restore()

    monkeypatch.setattr(runtime, 'restore_system_proxy', record_restore)
    for proxy in proxies:
        proxy.refused.set()
    try:
        # The origin is still running on loopback and would return 204 if
        # FINAL-EXIT silently fell back to DIRECT. Use new connections.
        for _ in range(3):
            with pytest.raises(Exception):
                runtime._abortable_https_204_via_http_proxy(
                    proxy_host='127.0.0.1',
                    proxy_port=runtime._client_mixed_port(runtime._client_config()),
                    probe_url=runtime._default_probe_urls()[0],
                    timeout_seconds=1,
                    abort_owner=_RuntimeAbortHandle(),
                )
            assert inspect_runtime_route(client.proxies())['leaf_proxy'] != 'DIRECT'
        assert restored == []
    finally:
        monkeypatch.setattr(runtime, 'restore_system_proxy', original_restore)


def test_export_failure_preserves_closed_proxy_with_reachable_origin(local_runtime, monkeypatch):
    runtime, proxies = local_runtime
    monkeypatch.setattr(runtime, '_maybe_watch_runtime_path', lambda: None)
    _start_and_verify(runtime, proxies)
    port = runtime._client_mixed_port(runtime._client_config())
    backend = MemoryProxyBackend(ProxySettings(enabled=False, server='', override=''))
    runtime.system_proxy = SystemProxyManager(
        {'enabled': True, 'host': '127.0.0.1', 'port': port},
        backend=backend, platform_name='windows',
    )
    runtime.system_proxy.enable()
    active = backend.read()
    result = runtime._fail_closed_after_core_export_removal_failure(
        runner=runtime.core_runner, reason='isolated export failure',
    )
    assert result['fail_closed'] is True
    assert not runtime.core_runner.is_running()
    assert backend.read() == active
    assert runtime.system_proxy.is_target_active()
    with pytest.raises(Exception):
        runtime._abortable_https_204_via_http_proxy(
            proxy_host='127.0.0.1', proxy_port=port,
            probe_url=runtime._default_probe_urls()[0], timeout_seconds=1,
            abort_owner=_RuntimeAbortHandle(),
        )


@pytest.mark.parametrize('local_runtime', [{'tiers': ['tier3']}], indirect=True)
def test_real_bootstrap_source_proof_does_not_admit_ordinary_traffic(local_runtime):
    runtime, _proxies = local_runtime
    from runtime_source_transport import RuntimeSourceRouteKind

    for config in (runtime.config, runtime.manager.config):
        config['tiers']['tier3'].update(enabled=True, min_alive_nodes=3)
    runtime._tier3_required_for_start = True
    runtime.manager.effective_egress_ok = False
    assert runtime.manager.tier3_auto_pool_count_snapshot().total == 1
    assert runtime._ensure_bootstrap_core_proxy()
    assert runtime._current_core_runtime_phase() == 'bootstrap'
    authority = runtime._refresh_loaded_runtime_authority(
        timeout_seconds=.5, require_managed_final_exit=False,
        worker_token=runtime._runtime_worker_token(),
    )
    route = inspect_runtime_route(runtime._require_api_client().proxies())
    assert authority is not None, str({
        'managed': tuple((runtime._runtime_loaded_export_contract or {}).get('proxy_map', {})),
        'chain': route.get('selected_chain'), 'leaf': route.get('leaf_proxy'),
        'snapshot': runtime._runtime_route_snapshot is not None,
    })
    assert route['leaf_proxy'] == 'REJECT'
    runtime._request_runtime_source_qualification('bootstrap')
    deadline = time.monotonic() + 3
    while not runtime._source_control_final_exit_ready(runtime.manager):
        assert time.monotonic() < deadline, runtime._runtime_source_demand_state
        time.sleep(.01)
    endpoints = runtime._runtime_source_fetch_endpoints_provider('bootstrap')
    final = next(e for e in endpoints if e.route_kind == RuntimeSourceRouteKind.FINAL_EXIT)
    assert final.ready
    assert not runtime._runtime_initial_activation_complete()
    assert runtime.manager.effective_egress_ok is False
    assert runtime._current_core_runtime_phase() == 'bootstrap'
    assert inspect_runtime_route(runtime._require_api_client().proxies())['leaf_proxy'] == 'REJECT'
    assert not runtime._runtime_monitoring_is_active()
    assert runtime.manager.tier3_auto_pool_count_snapshot().total == 1
    assert runtime._integration_origin_proxy_auth
    assert not any(runtime._integration_origin_proxy_auth)


@pytest.mark.parametrize('local_runtime', [{'tiers': ['tier3']}], indirect=True)
def test_real_first_commit_loads_bootstrap_source_pool_before_gate(local_runtime):
    runtime, _proxies = local_runtime
    manager = runtime.manager
    node = manager.runtime_nodes['tier3'].pop()
    for config in (runtime.config, manager.config):
        config['tiers']['tier3'].update(enabled=True, min_alive_nodes=3)
    runtime._tier3_required_for_start = True
    manager.effective_egress_ok = False
    assert runtime._ensure_bootstrap_core_proxy()
    assert not (runtime._runtime_loaded_export_contract or {}).get('proxy_map')
    before = runtime._core_export_sync_marker()
    pid = runtime.core_runner.status()['pid']

    manager.runtime_nodes['tier3'].append(node)
    manager.tier3_pool_revision += 1
    after = runtime._core_export_sync_marker()
    assert runtime._core_semantic_digest_from_marker(before) != (
        runtime._core_semantic_digest_from_marker(after)
    ), 'bootstrap digest omitted newly committed provider members'
    node.latency_ms += 10
    node.extra['test_diagnostic_only'] = 'changed'
    assert runtime._core_semantic_digest_from_marker(runtime._core_export_sync_marker()) == (
        runtime._core_semantic_digest_from_marker(after)
    )
    runtime._on_tier3_pool_committed([node])
    deadline = time.monotonic() + 4
    while not runtime._source_control_final_exit_ready(manager):
        assert time.monotonic() < deadline, runtime._runtime_source_demand_state
        runtime._request_runtime_source_qualification('bootstrap')
        time.sleep(.05)
    assert runtime.core_runner.status()['pid'] == pid
    assert runtime._current_core_runtime_phase() == 'bootstrap'
    assert manager.effective_egress_ok is False
    assert not runtime._runtime_initial_activation_complete()
    assert inspect_runtime_route(runtime._require_api_client().proxies())['leaf_proxy'] == 'REJECT'


def test_private_proxy_authorization_cannot_inject_headers(monkeypatch):
    def unexpected_socket(*_args, **_kwargs):
        pytest.fail('invalid authorization must be rejected before socket creation')

    monkeypatch.setattr(socket, 'create_connection', unexpected_socket)
    runtime = object.__new__(DesktopRuntime)
    with pytest.raises(ValueError, match='authorization'):
        runtime._abortable_https_204_via_http_proxy(
            proxy_host='127.0.0.1', proxy_port=1,
            proxy_authorization='Basic invalid\r\nInjected: value',
            probe_url='https://localhost/generate_204', timeout_seconds=1,
            abort_owner=_RuntimeAbortHandle(),
        )


def test_real_native_selection_qualifies_source_only_on_demand(local_runtime):
    runtime, proxies = local_runtime
    client = _start_and_verify(runtime, proxies)
    assert runtime._source_control_final_exit_ready(runtime.manager)
    old_proof = runtime._tier3_source_control_qualification
    client.proxy_delay('local-1', url=runtime._default_probe_url(), timeout_ms=1500)
    committed = client.select_proxy_with_readback(
        'TIER2-AUTO', 'local-1', mode='fixed', timeout=2,
    )
    assert committed.commit_state == 'committed'
    authority = runtime._refresh_loaded_runtime_authority(
        timeout_seconds=.5, require_managed_final_exit=True,
        worker_token=runtime._runtime_worker_token(),
    )
    assert authority is not None and authority[2].node.proxy_name == 'local-1'
    assert authority[0].snapshot_key != old_proof.snapshot_key
    assert not runtime._source_control_final_exit_ready(runtime.manager)

    runtime._request_runtime_source_qualification('core')
    worker = getattr(runtime, '_runtime_source_demand_worker', None)
    if worker is not None:
        worker.join(3)
        assert not worker.is_alive()
    assert runtime._source_control_final_exit_ready(runtime.manager)
    assert runtime._tier3_source_control_qualification.binding_key == authority[2].node.key
    assert runtime._runtime_monitoring_is_active()


@pytest.mark.parametrize('local_runtime', [4], indirect=True)
def test_real_native_selection_to_bad_leaf_cannot_disable_error_monitor(local_runtime):
    runtime, proxies = local_runtime
    client = _start_and_verify(runtime, proxies)
    client.proxy_delay('local-1', url=runtime._default_probe_url(), timeout_ms=1500)
    committed = client.select_proxy_with_readback(
        'TIER2-AUTO', 'local-1', mode='fixed', timeout=2,
    )
    assert committed.commit_state == 'committed'
    authority = runtime._refresh_loaded_runtime_authority(
        timeout_seconds=.5, require_managed_final_exit=True,
        worker_token=runtime._runtime_worker_token(),
    )
    assert authority is not None and authority[2].node.proxy_name == 'local-1'
    assert not runtime._runtime_recovery_is_armed()
    assert not runtime._source_control_final_exit_ready(runtime.manager)
    # Both the Core's native fallback and the application's forward-only
    # recovery now have the same first eligible alternative. Their race must
    # not make a healthy but differently selected route fail this monitor test.
    proxies[0].refused.set()
    proxies[1].refused.set()
    # A failed newly selected leaf must not need a successful probe of itself
    # before the one coordinator is allowed to verify a replacement.
    assert runtime._runtime_monitoring_is_active()
    try:
        _business_request(runtime, timeout=2)
    except Exception:
        pass
    _await_recovered(runtime, client, 'local-2')
    proxies[2].refused.set()
    try:
        _business_request(runtime, timeout=2)
    except Exception:
        pass
    _await_recovered(runtime, client, 'local-3')
    outcomes = tuple(runtime._runtime_recovery_outcomes.values())
    assert all(outcome.budget_ms <= 6000 for outcome in outcomes)
    # Native recovery may finish before the application creates a transaction.
    # Source proof is then requested by a real pending source batch.
    runtime._request_runtime_source_qualification('core')
    source_deadline = time.monotonic() + 3
    while time.monotonic() < source_deadline:
        if runtime._source_control_final_exit_ready(runtime.manager):
            break
        time.sleep(.25)
        runtime._request_runtime_source_qualification('core')
    assert runtime._source_control_final_exit_ready(runtime.manager), str({
        'source_state': runtime._runtime_source_demand_state,
        'leaf': inspect_runtime_route(client.proxies())['leaf_proxy'],
        'phase': runtime._current_core_runtime_phase(),
        'proof': repr(runtime._tier3_source_control_qualification),
        'broker': repr(runtime._runtime_probe_broker.snapshot()),
    })
    assert runtime._tier3_source_control_qualification.binding_key[-1] == 'local-3'


def test_idle_blackhole_does_not_trigger_background_exit_probes(local_runtime, monkeypatch):
    runtime, proxies = local_runtime
    _start_and_verify(runtime, proxies)
    calls = []
    original = runtime._probe_mihomo_mixed_port_once
    monkeypatch.setattr(runtime, '_probe_mihomo_mixed_port_once', lambda **kwargs: calls.append(kwargs) or original(**kwargs))
    proxies[0].failed.set()
    # Beyond the old two-second watch interval, without any business demand.
    time.sleep(2.3)
    assert calls == []
    assert runtime._integration_errors == []
    assert runtime._runtime_monitoring_is_active()


def test_silent_business_failure_recovers_after_core_emits_error(local_runtime):
    runtime, proxies = local_runtime
    client = _start_and_verify(runtime, proxies)
    proxies[0].failed.set()
    try:
        _business_request(runtime, timeout=12)
    except Exception:
        pass
    assert runtime._integration_errors, 'Core must emit a real error before passive recovery'
    _await_recovered(runtime, client, 'local-1')


@pytest.mark.parametrize('local_runtime', [{'tiers': ['tier1', 'tier2', 'tier2']}], indirect=True)
def test_real_manager_periodic_pass_releases_lower_tier_pin(local_runtime):
    runtime, proxies = local_runtime
    client = _start_and_verify(runtime, proxies)
    before, payload = runtime._runtime_route_snapshot_state()
    original = runtime._runtime_route_binding_for_outbound(before, payload, 'FINAL-EXIT')
    # Establish the same verified temporary fallback lease the recovery
    # coordinator publishes. Both the controller mutation and HTTPS are real.
    result = runtime._apply_runtime_automatic_selection(
        {'mode': 'fixed', 'group': 'PROXY-FALLBACK', 'target': 'TIER2-AUTO'},
        deadline_monotonic=time.monotonic() + 2,
    )
    assert result['commit_state'] == 'committed'
    snapshot = runtime._refresh_runtime_route_snapshot(timeout_seconds=.5)
    assert _business_request(runtime, timeout=2)[0] == 204
    assert client.proxy('PROXY-FALLBACK')['fixed'] == 'TIER2-AUTO'
    incident = 'local-periodic-release'
    lease = {
        'incident_id': incident, 'owner_token': 'integration',
        'group': 'PROXY-FALLBACK', 'mode': 'fixed',
        'from': original.node.proxy_name, 'to': 'local-1',
        'target_member': 'TIER2-AUTO', 'route_epoch': snapshot.route_epoch,
        'selector_intent_revision': runtime._runtime_selector_intent_revision,
        'verified_at_monotonic': time.monotonic(),
    }
    runtime._runtime_route_mutation_lease = lease
    runtime._runtime_incident_bindings[incident] = original
    runtime._runtime_incident_recovery_state[incident] = 'ready'
    manager = runtime.manager
    recovered = manager.runtime_nodes['tier1'][0].clone()
    context = manager._capture_health_check_context('tier1', recovered, origin='scheduled')
    transition = manager._commit_health_check_result(context, recovered)
    assert transition['action'] == 'recovered'
    deadline = time.monotonic() + 5
    while runtime._runtime_route_mutation_lease is not None and time.monotonic() < deadline:
        time.sleep(.025)
    assert runtime._runtime_route_mutation_lease is None, lease
    assert lease['release_state'] == 'released'
    assert not client.proxy('PROXY-FALLBACK')['fixed']
    assert inspect_runtime_route(client.proxies())['leaf_proxy'] == original.node.proxy_name
    assert _business_request(runtime, timeout=2)[0] == 204
    assert runtime._source_control_final_exit_ready(manager)


# ---------------------------------------------------------------------------
# Node judgement P2 (design v3 §12 P2 real-Core test): the production
# TIER3-AUTO shape on the bundled Mihomo, provider-backed.


@pytest.fixture
def tier3_provider_core(tmp_path):
    """TIER3-AUTO over a file provider TIER3B-RUNTIME of local HTTP-proxy nodes.

    Same group graph and contract fields as production (GENERAL -> FINAL-EXIT
    -> PROXY-FALLBACK -> TIER3-AUTO ``use: [TIER3B-RUNTIME]``, fallback,
    timeout 5000, expected-status 204, max-failed-times 2, lazy false).  The
    contract URL is a local plain-HTTP 204 origin: Mihomo URL tests verify
    TLS against the system roots.  Periodic checks are an hour apart, so
    every sample in a test is one the test (or the judge) asked for.
    """

    root = Path(__file__).resolve().parents[1]
    binary = root / 'bin' / 'mihomo.exe'
    if not binary.is_file():
        pytest.skip('repository Mihomo is unavailable')
    with ExitStack() as stack:
        origin = _serve(stack, ThreadingHTTPServer(('127.0.0.1', 0), _Origin))
        nodes = {f'n{index}': _serve(stack, _Proxy()) for index in (1, 2, 3)}
        mixed_port, mixed_reservation = _reserve_listener(stack, udp=True)
        controller_port, controller_reservation = _reserve_listener(stack)
        secret = secrets.token_hex(12)
        url = f'http://127.0.0.1:{origin.server_port}/generate_204'
        home = tmp_path / 'mihomo-home'
        home.mkdir()
        provider = {
            'proxies': [
                {'name': name, 'type': 'http', 'server': '127.0.0.1', 'port': proxy.server_address[1]}
                for name, proxy in nodes.items()
            ]
        }
        (home / 'tier3b-runtime.yaml').write_text(yaml.safe_dump(provider, sort_keys=False), encoding='utf-8')
        contract = {
            'url': url, 'interval': 3600, 'lazy': False, 'timeout': 5000,
            'expected-status': 204, 'max-failed-times': 2,
        }
        config = {
            'mixed-port': mixed_port, 'allow-lan': False, 'bind-address': '127.0.0.1',
            'mode': 'rule', 'log-level': 'info', 'ipv6': False,
            'external-controller': f'127.0.0.1:{controller_port}', 'secret': secret,
            'proxies': [],
            'proxy-providers': {'TIER3B-RUNTIME': {'type': 'file', 'path': './tier3b-runtime.yaml'}},
            'proxy-groups': [
                {'name': 'TIER3-AUTO', 'type': 'fallback', 'empty-fallback': 'REJECT',
                 'use': ['TIER3B-RUNTIME'], **contract},
                {'name': 'PROXY-FALLBACK', 'type': 'fallback', 'empty-fallback': 'REJECT',
                 'proxies': ['TIER3-AUTO'], **contract},
                {'name': 'FINAL-EXIT', 'type': 'select', 'empty-fallback': 'REJECT',
                 'proxies': ['PROXY-FALLBACK', 'REJECT']},
                {'name': 'GENERAL', 'type': 'select', 'empty-fallback': 'REJECT', 'proxies': ['FINAL-EXIT']},
                {'name': 'GLOBAL', 'type': 'select', 'empty-fallback': 'REJECT',
                 'proxies': ['GENERAL', 'FINAL-EXIT']},
            ],
            'rules': ['MATCH,GENERAL'],
        }
        (home / 'config.yaml').write_text(yaml.safe_dump(config, sort_keys=False), encoding='utf-8')
        mixed_reservation.close()
        controller_reservation.close()
        process = subprocess.Popen(
            [str(binary), '-d', str(home), '-f', str(home / 'config.yaml')],
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
        lines = []

        def pump():
            for raw in process.stdout:
                lines.append(raw.decode('utf-8', 'replace').rstrip())

        threading.Thread(target=pump, daemon=True).start()
        client = MihomoAPIClient(base_url=f'http://127.0.0.1:{controller_port}', secret=secret, timeout=2.0)
        deadline = time.monotonic() + 15
        while True:
            try:
                client.proxy('TIER3-AUTO')
                # The group can answer before the file provider has
                # registered its members: wait until every one is readable.
                for name in nodes:
                    client.proxy(name)
                break
            except Exception:
                if process.poll() is not None or time.monotonic() > deadline:
                    pytest.fail('bundled Mihomo did not start:\n' + '\n'.join(lines[-40:]))
                time.sleep(.05)
        # A select group with REJECT among its members boots on REJECT with a
        # fresh cache.db (F20); the app's startup writes FINAL-EXIT's route.
        client.select_proxy('FINAL-EXIT', 'PROXY-FALLBACK', timeout=2)
        assert inspect_runtime_route(client.proxies())['selected_chain'][:3] == [
            'FINAL-EXIT', 'PROXY-FALLBACK', 'TIER3-AUTO']
        # One passing contract sample per member, as a healthy check would leave.
        for name in nodes:
            client.proxy_delay(name, url=url, timeout_ms=2000, expected_status=204, verify_expected_status=True)
        assert client.proxy('TIER3-AUTO')['now'] == 'n1'
        yield SimpleNamespace(
            client=client, nodes=nodes, url=url, mixed_port=mixed_port,
            pid=process.pid, lines=lines,
        )


def _http_via_mixed(port, url, *, timeout=5.0):
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=timeout)
    try:
        connection.request('GET', url)
        response = connection.getresponse()
        response.read()
        return response.status
    finally:
        connection.close()


def _real_core_judge_runtime(tmp_path, core, *, durable=True):
    """A READY, armed runtime bound to the real Core (no app-owned process)."""

    config = {
        'client': {
            'runtime_state_path': str(tmp_path / 'runtime-state.json'),
            # 10-28: ladder/rearm/overshoot/lease default on; pinned to
            # the legacy off values this real-Core case was written for.
            'egress_recovery': {'node_judgement': {'post_dial_judge': 'act',
                                                   'durable_conviction': durable,
                                                   'rearm_on_stale_route': False,
                                                   'tier_ladder_judge': False,
                                                   'overshoot_repair': False},
                                'route_lease': {'enabled': False}},
        },
        'alerts': {'log_file': str(tmp_path / 'monitor.log'), 'desktop': {'enabled': False}},
    }
    runtime = DesktopRuntime(config, project_root=tmp_path)
    runtime.api_client = core.client
    runtime.core_runner = SimpleNamespace(
        is_running=lambda: True,
        status=lambda: {'pid': core.pid, 'process_generation': 1},
    )
    runtime._core_generation = 1
    runtime._runtime_loaded_export_contract = {
        'revision': 1,
        'semantic_digest': 'real-core-tier3-provider',
        'proxy_map': {
            name: {'tier': 'tier3', 'pool_kind': 'tier3b', 'identity': f'identity-{name}',
                   'proxy': {'name': name, 'type': 'http'}}
            for name in core.nodes
        },
    }
    manager = SimpleNamespace(effective_egress_ok=True, runtime_generation=int(runtime._runtime_generation),
                              observations=[], rechecks=[])

    def apply(observation):
        manager.observations.append(dict(observation))
        return {'accepted': True, 'changed': True, 'action': 'quarantined'}

    manager.apply_runtime_health_observation = apply
    manager.enqueue_runtime_leaf_recheck = lambda node, **_kwargs: manager.rechecks.append(node)
    runtime.manager = manager
    runtime._runtime_egress_readiness = RuntimeEgressReadiness(
        state=RuntimeEgressReadinessState.READY, validation_active=False,
    )
    runtime._runtime_initial_activation_generation = 1
    runtime._core_runtime_phase = 'formal_capture'
    return runtime, manager


def test_real_core_provider_read_does_not_clear_a_fixed_pick(tier3_provider_core):
    core = tier3_provider_core
    client, node = core.client, core.nodes['n1']

    def pin_dead_n1():
        node.zombie.set()
        with pytest.raises(MihomoDelayTransportFailed):
            client.proxy_delay('n1', url=core.url, timeout_ms=2000, expected_status=204,
                               verify_expected_status=True)
        client.select_proxy('TIER3-AUTO', 'n1', timeout=6)

    def revive_n1_and_read_fixed():
        node.zombie.clear()
        client.proxy_delay('n1', url=core.url, timeout_ms=2000, expected_status=204,
                           verify_expected_status=True)
        return client.proxy('TIER3-AUTO').get('fixed')

    # The judge's S3 read (§3.1, Conf-1): no group is marshalled, so a dead
    # fixed member survives it.  (Reading the group itself would clear it.)
    pin_dead_n1()
    provider = client.provider_proxies_one('TIER3B-RUNTIME')
    assert [entry['name'] for entry in provider['proxies']] == ['n1', 'n2', 'n3']
    assert revive_n1_and_read_fixed() == 'n1'
    client.unfix_proxy('TIER3-AUTO')
    # Contrast: the all-provider read clears it as a side effect, which is
    # why the judge never uses it (§5.5).
    pin_dead_n1()
    client.providers_proxies()
    assert not revive_n1_and_read_fixed()


@pytest.mark.parametrize('durable', [True, False], ids=['durable', 'move_only'])
def test_real_core_failed_early_check_moves_the_next_dial_and_native_commit_rebinds(
        tier3_provider_core, tmp_path, durable):
    core = tier3_provider_core
    client = core.client
    runtime, manager = _real_core_judge_runtime(tmp_path, core, durable=durable)
    try:
        before = runtime._refresh_runtime_route_snapshot(timeout_seconds=2.0)
        assert before is not None
        assert runtime._try_arm_runtime_recovery()
        snapshot, payload = runtime._runtime_route_snapshot_state()
        binding = runtime._runtime_route_binding_for_outbound(snapshot, payload, 'FINAL-EXIT')
        assert binding.route_chain == ('FINAL-EXIT', 'PROXY-FALLBACK', 'TIER3-AUTO', 'n1')
        core.nodes['n1'].zombie.set()
        now = time.time()
        runtime._on_runtime_post_dial_trigger(
            Trigger(kind='FAST', leaf='n1', tokens=('0' * 32, '1' * 32), t=now, n_events=2,
                    first_event_t=now - .05, episode=1, new_episode=True),
            mode='act',
            tokenizer=runtime._node_judgement_state().tokenizer,
        )
        judge = runtime._runtime_post_dial_judge()
        assert judge.wait_idle(12)

        # The accused check failed at transport level: Mihomo itself moved
        # TIER3-AUTO to the next member.  The app wrote no selection.
        group = client.proxy('TIER3-AUTO')
        assert group['now'] == 'n2' and not group.get('fixed')
        assert client.proxy('n1')['extra'][core.url]['alive'] is False
        # native_commit: a real route-snapshot refresh, the rebind (only
        # TIER3-AUTO.now changed), then (durable_conviction) the existing
        # health overlay + recheck; move-only records nothing about n1.
        after, payload = runtime._runtime_route_snapshot_state()
        assert after.route_epoch > before.route_epoch
        assert (after.process_pid, after.core_generation, after.semantic_digest) == (
            before.process_pid, before.core_generation, before.semantic_digest)
        rebound = runtime._runtime_route_binding_for_outbound(after, payload, 'FINAL-EXIT')
        assert rebound.route_chain == ('FINAL-EXIT', 'PROXY-FALLBACK', 'TIER3-AUTO', 'n2')
        if durable:
            assert [item['proxy_name'] for item in manager.observations] == ['n1']
            assert manager.observations[0]['identity'] == 'identity-n1'
            assert [node.proxy_name for node in manager.rechecks] == ['n1']
        else:
            assert manager.observations == [] and manager.rechecks == []
            assert runtime._runtime_flap_history == {}
        incidents = [name for name in runtime._runtime_recovery_tickets if name.startswith('post-dial-')]
        assert len(incidents) == 1
        outcome = runtime._runtime_recovery_outcomes[incidents[0]]
        assert outcome.status is RuntimeRecoveryStatus.RECOVERED and outcome.to_ref.proxy_name == 'n2'
        assert outcome.reason == ('post_dial_native_commit' if durable else 'post_dial_native_rebind')
        assert (incidents[0] in runtime._runtime_incident_bindings) is durable
        assert runtime._runtime_recovery_is_armed()

        # The next user dial is Mihomo's own pick: through n2, never n1.
        counts = {name: proxy.connected for name, proxy in core.nodes.items()}
        next_url = core.url.replace('/generate_204', '/next-dial')
        assert _http_via_mixed(core.mixed_port, next_url) == 204
        assert core.nodes['n2'].connected == counts['n2'] + 1
        assert core.nodes['n1'].connected == counts['n1']
        # Mihomo's own open line for that dial names n2 (URL tests print none).
        origin = core.url.split('/')[2]
        deadline = time.monotonic() + 3
        while not any('[TCP]' in line and f'--> {origin} ' in line and '[n2]"' in line for line in core.lines):
            assert time.monotonic() < deadline, core.lines[-20:]
            time.sleep(.02)
        assert not any('[TCP]' in line and f'--> {origin} ' in line and '[n1]"' in line for line in core.lines)
    finally:
        runtime._runtime_post_dial_judge().wait_idle(8)
        runtime._stop_runtime_post_dial_observer('test', join_timeout=2.0)
        runtime.stop_event.set()
