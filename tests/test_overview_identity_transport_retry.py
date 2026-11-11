from __future__ import annotations

import copy
import json
import ssl
import threading
import time
import urllib.error
from collections import Counter

import pytest

from overview_ip_check import collect_exit_identity
from tests.test_exit_identity import _Response, _UrlOpener


V4_PROVIDERS = (
    ('ipify', 4, 'https://ipify.test/v4'),
    ('ident.me', 4, 'https://ident.test/v4'),
)
PROVIDERS = V4_PROVIDERS + (
    ('ipify', 6, 'https://ipify.test/v6'),
    ('ident.me', 6, 'https://ident.test/v6'),
)


@pytest.fixture(autouse=True)
def identity_workers_finish():
    existing = set(threading.enumerate())
    yield
    workers = [thread for thread in threading.enumerate()
               if thread not in existing and thread.name.startswith('exit-identity')]
    for thread in workers:
        thread.join(timeout=1)
    assert not [thread.name for thread in workers if thread.is_alive()]


def _payload(ip='1.1.1.1'):
    return json.dumps({'ip': ip, 'cc': 'US', 'city': 'Test City',
                       'asn': 13335, 'aso': 'Test Network'}).encode('utf-8')


def _collect(opener, **kwargs):
    return collect_exit_identity(
        opener_factory=lambda _proxy: opener, providers=V4_PROVIDERS,
        rounds=1, retry_transient_errors=True, total_timeout_seconds=1,
        **kwargs,
    )


def test_ipv4_sources_retry_independently_while_ipv6_waits_for_total_deadline():
    release = threading.Event()
    calls = Counter()
    timeouts = {url: [] for _, _, url in PROVIDERS}
    calls_lock = threading.Lock()

    class Opener:
        def open(self, request, timeout):
            url = request.full_url
            with calls_lock:
                calls[url] += 1
                attempt = calls[url]
                timeouts[url].append(timeout)
            if url.endswith('/v6'):
                assert release.wait(2), 'test IPv6 provider was not released'
                raise TimeoutError('IPv6 unavailable')
            if attempt == 1:
                raise urllib.error.HTTPError(url, 503, 'Unavailable', {}, None)
            return _Response(_payload())

    started = time.monotonic()
    try:
        result = collect_exit_identity(
            opener_factory=lambda _proxy: Opener(), providers=PROVIDERS,
            rounds=1, max_workers=4, retry_transient_errors=True,
            timeout_seconds=4, total_timeout_seconds=0.35,
        )
        assert time.monotonic() - started < 0.8
        ipv4 = result['identity']['ipv4']
        assert ipv4['ip'] == '1.1.1.1'
        assert ipv4['state'] == 'confirmed'
        assert ipv4['agreement']['successful_sources'] == 2
        assert ipv4['agreement']['agreeing_sources'] == 2
        assert ipv4['location']['city'] == 'Test City'
        assert ipv4['location']['ip'] == ipv4['ip']
        assert ipv4['network']['asn'] == '13335'
        assert ipv4['network']['organisation'] == 'Test Network'
        for source in ipv4['evidence']:
            assert len(source['attempts']) == 1
            observation = source['attempts'][0]
            assert observation['round'] == 1
            assert observation['status'] == 'ok'
            assert len(observation['transport_retries']) == 1
            assert observation['transport_retries'][0]['status'] == 'error'
            assert '503' in observation['transport_retries'][0]['error']
            assert source['successful_attempts'] == 1
        assert result['identity']['ipv6']['state'] == 'unavailable'
        assert all(source['attempts'][0]['status'] == 'timeout'
                   for source in result['identity']['ipv6']['evidence'])
        snapshot = copy.deepcopy(result)
    finally:
        release.set()
    assert result == snapshot
    assert calls == Counter({url: 2 if family == 4 else 1 for _, family, url in PROVIDERS})
    assert all(0 < values[-1] <= values[0] <= 0.35 for values in timeouts.values())


@pytest.mark.parametrize('failure', [
    TimeoutError('temporarily timed out'), ConnectionResetError('connection reset'),
    ssl.SSLEOFError('TLS peer closed connection unexpectedly'),
    urllib.error.URLError(TimeoutError('temporarily timed out')),
    *[urllib.error.HTTPError('https://ipify.test/v4', code, 'Temporary', {}, None)
      for code in (408, 429, 500, 503)],
])
def test_transient_error_gets_only_one_retry_and_remains_evidence(failure):
    opener = _UrlOpener({url: [failure, failure] for _, _, url in V4_PROVIDERS})
    ipv4 = _collect(opener)['identity']['ipv4']
    assert ipv4['state'] == 'unavailable'
    assert len(opener.calls) == 4
    for source in ipv4['evidence']:
        assert len(source['attempts']) == 1
        assert source['attempts'][0]['status'] == 'error'
        assert source['attempts'][0]['transport_retries'][0]['status'] == 'error'


@pytest.mark.parametrize('payload', [
    b'{"ip":"127.0.0.1"}', b'not an IP',
    ssl.SSLCertVerificationError('certificate verification failed'),
    urllib.error.HTTPError('https://ipify.test/v4', 403, 'Forbidden', {}, None),
])
def test_invalid_and_non_transient_errors_are_not_retried(payload):
    opener = _UrlOpener({url: [payload] for _, _, url in V4_PROVIDERS})
    ipv4 = _collect(opener)['identity']['ipv4']
    assert ipv4['state'] == 'unavailable'
    assert len(opener.calls) == 2
    assert all('transport_retries' not in source['attempts'][0] for source in ipv4['evidence'])


@pytest.mark.parametrize('second_ip, state', [('1.1.1.1', 'confirmed'), ('8.8.8.8', 'conflict')])
def test_successful_observations_are_not_retried_or_erased_to_resolve_conflict(second_ip, state):
    opener = _UrlOpener({V4_PROVIDERS[0][2]: [_payload()], V4_PROVIDERS[1][2]: [_payload(second_ip)]})
    ipv4 = _collect(opener)['identity']['ipv4']
    assert ipv4['state'] == state
    assert len(opener.calls) == 2
    assert all('transport_retries' not in source['attempts'][0] for source in ipv4['evidence'])
    if state == 'conflict':
        assert ipv4['ip'] is None
        assert 'location' not in ipv4
        assert 'network' not in ipv4


def test_transport_retry_does_not_hide_rotation_between_rounds():
    opener = _UrlOpener({url: [TimeoutError('transient'), _payload(), _payload('8.8.8.8')]
                         for _, _, url in V4_PROVIDERS})
    ipv4 = collect_exit_identity(
        opener=opener, providers=V4_PROVIDERS, rounds=2,
        retry_transient_errors=True, total_timeout_seconds=1,
    )['identity']['ipv4']
    assert ipv4['state'] == 'rotating'
    assert ipv4['ip'] is None
    assert len(opener.calls) == 6
    assert all(len(source['attempts']) == 2 for source in ipv4['evidence'])


def test_retry_is_not_started_with_less_than_point_two_seconds_left():
    calls = []

    class Opener:
        def open(self, request, timeout):
            calls.append(timeout)
            time.sleep(0.20)
            raise TimeoutError('temporary timeout near deadline')

    started = time.monotonic()
    result = collect_exit_identity(
        opener=Opener(), providers=V4_PROVIDERS[:1], rounds=1,
        retry_transient_errors=True, total_timeout_seconds=0.35,
    )
    assert time.monotonic() - started < 0.6
    assert len(calls) == 1
    assert 'transport_retries' not in result['identity']['ipv4']['evidence'][0]['attempts'][0]


def test_retry_requires_shared_total_deadline():
    with pytest.raises(ValueError, match='retry_transient_errors requires total_timeout_seconds'):
        collect_exit_identity(retry_transient_errors=True)


def test_http_error_response_is_closed_before_retry_starts():
    events = []

    class HttpError(urllib.error.HTTPError):
        def close(self):
            events.append('closed')
            return super().close()

    class Opener:
        def open(self, request, timeout):
            if not events:
                events.append('failed')
                raise HttpError(request.full_url, 503, 'Unavailable', {}, None)
            assert events == ['failed', 'closed']
            events.append('retried')
            return _Response(_payload())

    ipv4 = collect_exit_identity(
        opener=Opener(), providers=V4_PROVIDERS[:1], rounds=1,
        retry_transient_errors=True, total_timeout_seconds=1,
    )['identity']['ipv4']
    assert ipv4['ip'] == '1.1.1.1'
    assert events[:3] == ['failed', 'closed', 'retried']


def test_retry_timeout_preserves_first_failure_without_late_result_mutation():
    release = threading.Event()
    finished = threading.Event()
    calls = []

    class Opener:
        def open(self, request, timeout):
            calls.append(timeout)
            if len(calls) == 1:
                raise urllib.error.HTTPError(request.full_url, 503, 'Unavailable', {}, None)
            try:
                assert release.wait(2), 'test retry was not released'
                return _Response(_payload())
            finally:
                finished.set()

    try:
        started = time.monotonic()
        result = collect_exit_identity(
            opener=Opener(), providers=V4_PROVIDERS[:1], rounds=1,
            retry_transient_errors=True, timeout_seconds=4, total_timeout_seconds=0.35,
        )
        assert time.monotonic() - started < 0.8
        attempt = result['identity']['ipv4']['evidence'][0]['attempts'][0]
        assert attempt['status'] == 'timeout'
        assert attempt['transport_retries'][0]['status'] == 'error'
        assert '503' in attempt['transport_retries'][0]['error']
        assert 0 < calls[1] <= calls[0] <= 0.35
        snapshot = copy.deepcopy(result)
    finally:
        release.set()
    assert finished.wait(1)
    assert result == snapshot
    assert len(calls) == 2


def test_transport_retries_remain_disabled_by_default():
    opener = _UrlOpener({url: [TimeoutError('unavailable')] for _, _, url in V4_PROVIDERS})
    result = collect_exit_identity(opener=opener, providers=V4_PROVIDERS, rounds=1,
                                   total_timeout_seconds=1)
    assert result['identity']['ipv4']['state'] == 'unavailable'
    assert len(opener.calls) == 2
