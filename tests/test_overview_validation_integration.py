"""Exercise the public overview request through real Core and HTTPS sockets."""
from __future__ import annotations

import json
import ssl
import threading
import time
import urllib.request
import pytest

import desktop_runtime as runtime_module
import overview_ip_check as identity_module
from system_proxy import ProxySettings, SystemProxyManager
from tests.test_runtime_sudden_failure import _Origin, _start_and_verify, local_runtime


def _configure_local_identity(runtime, monkeypatch, providers):
    monkeypatch.setattr(ssl, '_create_default_https_context', ssl.create_default_context)
    monkeypatch.setattr(urllib.request, 'proxy_bypass', lambda _host: False)
    monkeypatch.setattr(identity_module, 'EXIT_IDENTITY_PROVIDERS', providers)
    monkeypatch.setattr(runtime_module, 'EXIT_IDENTITY_PROVIDERS', providers)
    monkeypatch.setattr(runtime_module, 'OVERVIEW_IDENTITY_PROVIDERS', providers)

    # A background cache must never complete this button request for us.
    def unexpected_background(**_kwargs):
        raise AssertionError('overview must return its own terminal identity')
    monkeypatch.setattr(runtime, '_queue_final_exit_identity_diagnostic', unexpected_background)


@pytest.mark.parametrize('capture_enabled', [False, True])
@pytest.mark.parametrize('degraded', [False, True])
def test_overview_returns_identity_after_real_https_validation(local_runtime, monkeypatch, capture_enabled, degraded):
    runtime, proxies = local_runtime
    if capture_enabled:
        class MemoryProxyBackend:
            settings = ProxySettings(enabled=False, server='', override='')
            def read(self):
                return self.settings
            def write(self, settings):
                self.settings = settings

        runtime.config['client']['system_proxy']['enabled'] = True
        runtime.system_proxy = SystemProxyManager(
            {'enabled': True, 'host': '127.0.0.1', 'port': runtime._client_mixed_port(runtime._client_config())},
            backend=MemoryProxyBackend(), platform_name='windows',
        )
    client = _start_and_verify(runtime, proxies)
    if capture_enabled:
        assert runtime.system_proxy.is_target_active()
        assert runtime._current_core_runtime_phase() == 'formal_capture'
        # Exercise the next revalidation after host capture, which used to
        # reject the contradictory formal_no_capture + active state.
        runtime._queue_tier3_egress_activation()
        assert runtime._wait_for_tier3_egress_activation(timeout=8)
        assert runtime.manager.effective_egress_ok is True
    before = client.proxies()
    original_get = _Origin.do_GET

    def identity_get(self):
        if self.path != '/ip':
            return original_get(self)
        body = b'8.8.8.8'
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    monkeypatch.setattr(_Origin, 'do_GET', identity_get)
    providers = tuple(
        (f'local-identity-{index}', 4, url.replace('/generate_204', '/ip'))
        for index, url in enumerate(runtime._default_probe_urls())
    )
    _configure_local_identity(runtime, monkeypatch, providers)

    if degraded:
        # Reproduce a runtime failure after this Core already completed startup.
        # The overview must reprove the real path instead of re-entering a
        # startup-only gate, including when system proxy capture is still on.
        runtime._set_effective_egress_readiness_state(False, 'prior runtime route failure')
        assert not runtime._runtime_initial_activation_complete()
        assert runtime._runtime_initial_activation_generation == runtime._core_generation

    started = time.monotonic()
    result = runtime.check_current_exit_identity(
        proxy_url=f'http://127.0.0.1:{runtime._client_mixed_port(runtime._client_config())}',
        request_id='real-overview', timeout_seconds=8,
    )
    elapsed = time.monotonic() - started
    assert result['state'] == 'complete', result
    assert result['request_id'] == 'real-overview'
    assert result['identity']['ipv4']['ip'] == '8.8.8.8'
    assert result['identity']['ipv4']['state'] == 'confirmed'
    assert result['diagnostics']['neutral_connectivity']['success_count'] == 2
    assert result['diagnostics']['identity_pending'] is False
    assert result['route']['confirmed'] is True
    assert elapsed < 8
    assert client.proxies()['proxies']['PROXY-FALLBACK']['all'] == before['proxies']['PROXY-FALLBACK']['all']
    assert runtime.config['tiers']['tier2']['check_interval'] == 3600
    if capture_enabled:
        for index in range(2):
            refreshed = runtime.check_current_exit_identity(
                proxy_url=f'http://127.0.0.1:{runtime.system_proxy.port}',
                request_id=f'real-overview-refresh-{index}', timeout_seconds=8,
            )
            assert refreshed['state'] == 'complete', refreshed
            assert refreshed['identity']['ipv4']['ip'] == '8.8.8.8'
            assert runtime.manager.effective_egress_ok is True
    print(f'complete overview through real Core and HTTPS in {elapsed:.3f}s')


@pytest.mark.parametrize('provider_behavior', ['slow', 'first_503', 'always_503'])
def test_overview_handles_identity_service_latency_and_transient_errors(
    local_runtime, monkeypatch, provider_behavior,
):
    runtime, proxies = local_runtime
    client = _start_and_verify(runtime, proxies)
    before = client.proxies()['proxies']['PROXY-FALLBACK']
    prior_recovery_outcomes = dict(runtime._runtime_recovery_outcomes)
    original_get = _Origin.do_GET
    calls = {'/ipify': 0, '/ident-me': 0}
    calls_lock = threading.Lock()

    def identity_get(self):
        if self.path not in calls:
            return original_get(self)
        with calls_lock:
            calls[self.path] += 1
            attempt = calls[self.path]
        if provider_behavior == 'slow':
            # Both independent identity requests exceed the old 2.2 s cap,
            # while the original eight-second button deadline is sufficient.
            time.sleep(2.6 if self.path == '/ipify' else 2.7)
        failed = provider_behavior == 'always_503' or (
            provider_behavior == 'first_503' and attempt == 1
        )
        payload = {'ip': '8.8.8.8'}
        if self.path == '/ident-me':
            payload.update(cc='US', country='United States', city='Test City',
                           asn=15169, aso='Test Network')
        body = b'' if failed else json.dumps(payload).encode('utf-8')
        self.send_response(503 if failed else 200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    monkeypatch.setattr(_Origin, 'do_GET', identity_get)
    urls = runtime._default_probe_urls()
    _configure_local_identity(runtime, monkeypatch, (
        ('ipify', 4, urls[0].replace('/generate_204', '/ipify')),
        ('ident.me', 4, urls[1].replace('/generate_204', '/ident-me')),
    ))

    started = time.monotonic()
    result = runtime.check_current_exit_identity(
        proxy_url=f'http://127.0.0.1:{runtime._client_mixed_port(runtime._client_config())}',
        request_id=f'real-identity-{provider_behavior}', timeout_seconds=8,
    )
    elapsed = time.monotonic() - started

    assert elapsed < 8, (elapsed, result)
    assert result['request_id'] == f'real-identity-{provider_behavior}'
    assert result['route']['confirmed'] is True, result
    assert result['diagnostics']['neutral_connectivity']['success_count'] == 2
    assert result['diagnostics']['identity_pending'] is False
    expected_calls = 1 if provider_behavior == 'slow' else 2
    assert calls == {'/ipify': expected_calls, '/ident-me': expected_calls}, result
    if provider_behavior == 'always_503':
        assert result['state'] == 'unavailable', result
        assert result['error'] == 'identity_unavailable'
        assert not result['identity']['ipv4']['ip']
    else:
        assert result['state'] == 'complete', result
        identity = result['identity']['ipv4']
        assert identity['state'] == 'confirmed'
        assert identity['ip'] == '8.8.8.8'
        assert identity['location']['ip'] == identity['ip']
        assert identity['location']['city'] == 'Test City'
        assert identity['network']['ip'] == identity['ip']
        assert identity['network']['asn'] == '15169'
        assert identity['network']['organisation'] == 'Test Network'
        if provider_behavior == 'slow':
            assert elapsed >= 2.7
    after = client.proxies()['proxies']['PROXY-FALLBACK']
    assert after['all'] == before['all']
    assert after['now'] == before['now']
    assert runtime._runtime_recovery_outcomes == prior_recovery_outcomes
    assert runtime.config['tiers']['tier2']['check_interval'] == 3600
    print(f'{provider_behavior}: {result["state"]} through real Core and HTTPS in {elapsed:.3f}s')


def test_overview_does_not_wait_for_silent_ipv6_after_real_ipv4_result(local_runtime, monkeypatch):
    runtime, proxies = local_runtime
    client = _start_and_verify(runtime, proxies)
    before = client.proxies()['proxies']['PROXY-FALLBACK']
    original_get = _Origin.do_GET
    release_ipv6 = threading.Event()

    def identity_get(self):
        if self.path not in {'/ip', '/ipv6'}:
            return original_get(self)
        if self.path == '/ipv6':
            release_ipv6.wait(10)
        body = json.dumps({
            'ip': '2606:4700:4700::1111' if self.path == '/ipv6' else '8.8.8.8',
            'asn': 15169, 'aso': 'Test Network', 'city': 'Test City',
        }).encode()
        try:
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
            pass

    monkeypatch.setattr(_Origin, 'do_GET', identity_get)
    urls = runtime._default_probe_urls()
    _configure_local_identity(runtime, monkeypatch, (
        ('ipify', 4, urls[0].replace('/generate_204', '/ip')),
        ('ident.me', 4, urls[1].replace('/generate_204', '/ip')),
        ('ipify', 6, urls[0].replace('/generate_204', '/ipv6')),
        ('ident.me', 6, urls[1].replace('/generate_204', '/ipv6')),
    ))
    try:
        started = time.monotonic()
        result = runtime.check_current_exit_identity(
            proxy_url=f'http://127.0.0.1:{runtime._client_mixed_port(runtime._client_config())}',
            request_id='real-overview-silent-v6', timeout_seconds=8,
        )
        elapsed = time.monotonic() - started
        assert result['state'] == 'complete', result
        assert elapsed < 2.0, (elapsed, result)
        assert result['identity']['ipv4']['state'] == 'confirmed'
        assert result['identity']['ipv4']['location']['city'] == 'Test City'
        assert result['identity']['ipv6']['state'] == 'unavailable'
        assert result['diagnostics']['identity_collection']['completion_reason'] == 'answered'
        assert result['diagnostics']['neutral_connectivity']['success_count'] == 2
        assert result['route']['confirmed'] is True
        assert client.proxies()['proxies']['PROXY-FALLBACK']['all'] == before['all']
        print(f'real overview with silent IPv6 returned verified IPv4 in {elapsed:.3f}s')
    finally:
        release_ipv6.set()
