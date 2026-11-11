from __future__ import annotations

import io
import json
import time
from types import SimpleNamespace
from urllib.error import HTTPError
from unittest.mock import Mock

import pytest

from desktop_runtime import DesktopRuntime
from mihomo_api import MihomoAPIClient, MihomoAPIError, MihomoDelayTestFailed
from tests.test_mihomo_api import FakeHTTPConnection, FakeHTTPClientResponse, FakeOpener, FakeResponse


@pytest.mark.parametrize('closable', [False, True])
@pytest.mark.parametrize('status,body,negative', [
    (504, b'{"message":"Timeout"}', True),
    (503, b'{}', True),
    (503, b'{"message":"An error occurred in the delay test"}', True),
    (503, b'<html>Service Unavailable</html>', False),
    (504, b'{"message":"controller busy"}', False),
    (401, b'{"message":"Unauthorized"}', False),
])
def test_only_native_delay_verdicts_are_node_failures(monkeypatch, closable, status, body, negative):
    error = HTTPError('http://127.0.0.1/proxies/a/delay', status, 'failure', {}, io.BytesIO(body))
    client = MihomoAPIClient(base_url='http://127.0.0.1', opener=FakeOpener([error]))
    connection = FakeHTTPConnection('127.0.0.1', response=FakeHTTPClientResponse(body, status=status))
    monkeypatch.setattr('mihomo_api.http.client.HTTPConnection', lambda *args, **kwargs: connection)
    with pytest.raises(MihomoAPIError) as caught:
        client.proxy_delay('a', url='https://probe.invalid', connection_abort_sink=(lambda closer: None) if closable else None)
    assert isinstance(caught.value, MihomoDelayTestFailed) is negative
    assert DesktopRuntime._proxy_delay_failure_is_transient(caught.value) is not negative
    assert connection.closed if closable else error.fp.closed


@pytest.mark.parametrize('alive', [True, False, None])
def test_positive_delay_does_not_prove_expected_http_status(alive):
    url = 'https://probe.invalid/generate_204'
    state = {'extra': {url: {'alive': alive}}} if alive is not None else {}
    opener = FakeOpener([FakeResponse(b'{"delay":8}'), FakeResponse(json.dumps(state).encode())])
    client = MihomoAPIClient(base_url='http://127.0.0.1', opener=opener)
    kwargs = dict(url=url, expected_status=204, verify_expected_status=True, request_timeout=.8)
    if alive is True:
        assert client.proxy_delay('a', **kwargs) == {'delay': 8}
    else:
        with pytest.raises(MihomoAPIError) as caught:
            client.proxy_delay('a', **kwargs)
        assert isinstance(caught.value, MihomoDelayTestFailed) is (alive is False)
    assert 0 < opener.timeouts[1] <= opener.timeouts[0] + 1e-9
    assert opener.timeouts[0] == .8


@pytest.mark.parametrize('path,method', [('/group/a/delay', 'GET'), ('/proxies/a', 'GET'), ('/proxies/a/delay', 'PUT')])
def test_other_controller_endpoints_remain_attribution_neutral(path, method):
    error = HTTPError('http://127.0.0.1', 504, 'failure', {}, io.BytesIO(b'{"message":"Timeout"}'))
    client = MihomoAPIClient(base_url='http://127.0.0.1', opener=FakeOpener([error]))
    with pytest.raises(MihomoAPIError) as caught:
        client._request(method, path)
    assert type(caught.value) is MihomoAPIError


def test_idle_watch_hook_never_opens_a_probe():
    runtime = object.__new__(DesktopRuntime)
    runtime._refresh_loaded_runtime_authority = Mock()
    runtime._probe_mihomo_mixed_port_once = Mock()
    for _ in range(20):
        runtime._maybe_watch_runtime_path()
    runtime._refresh_loaded_runtime_authority.assert_not_called()
    runtime._probe_mihomo_mixed_port_once.assert_not_called()


def test_delay_core_client_and_broker_deadlines_leave_cleanup_time():
    runtime = object.__new__(DesktopRuntime)
    calls = []
    client = SimpleNamespace(proxy_delay=lambda name, **kwargs: calls.append(kwargs) or {'delay': 12})
    runtime._require_api_client = lambda: client
    runtime._register_runtime_internal_probe = Mock()
    context = SimpleNamespace(execution_deadline_monotonic=time.monotonic() + .75, register_abort_callback=lambda cb: True)
    result = runtime._probe_proxy_delay_with_urls_leased('a', ['https://probe.invalid'], timeout_ms=1500, request_timeout_seconds=2, execution_context=context)
    assert result['delay'] == 12
    assert calls[0]['timeout_ms'] / 1000 + .14 < calls[0]['request_timeout'] < .66
