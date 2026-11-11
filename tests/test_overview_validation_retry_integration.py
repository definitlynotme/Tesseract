"""Real native fallback during a manual proof finishes in the same UI request."""
from __future__ import annotations

import ssl
import time
import urllib.request

import desktop_runtime as runtime_module
import overview_ip_check as identity_module
from desktop_runtime import _RuntimeAbortHandle
from runtime_route_audit import inspect_runtime_route
from tests.test_runtime_sudden_failure import _Origin, _start_and_verify, local_runtime


def test_one_overview_request_reproves_real_native_fallback(local_runtime, monkeypatch):
    runtime, proxies = local_runtime
    monkeypatch.setattr(runtime, "_maybe_watch_runtime_path", lambda: None)
    client = _start_and_verify(runtime, proxies)
    before = client.proxies()
    original_get = _Origin.do_GET

    def identity_get(self):
        if self.path != "/ip":
            return original_get(self)
        body = b"8.8.8.8"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    monkeypatch.setattr(_Origin, "do_GET", identity_get)
    monkeypatch.setattr(ssl, "_create_default_https_context", ssl.create_default_context)
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda _host: False)
    providers = tuple(
        (f"retry-identity-{index}", 4, url.replace("/generate_204", "/ip"))
        for index, url in enumerate(runtime._default_probe_urls())
    )
    monkeypatch.setattr(identity_module, "EXIT_IDENTITY_PROVIDERS", providers)
    monkeypatch.setattr(runtime_module, "EXIT_IDENTITY_PROVIDERS", providers)
    monkeypatch.setattr(runtime_module, "OVERVIEW_IDENTITY_PROVIDERS", providers)
    original_contracts = runtime._manual_current_exit_quality_contracts
    contract_leaves = []

    def contracts_then_native_switch(session):
        contract_leaves.append(inspect_runtime_route(client.proxies())["leaf_proxy"])
        results = original_contracts(session)
        assert all(result.is_https_204_success(session.budget) for result in results)
        if len(contract_leaves) == 1:
            proxies[0].refused.set()
            for _ in range(2):
                try:
                    runtime._abortable_https_204_via_http_proxy(
                        proxy_host="127.0.0.1",
                        proxy_port=runtime._client_mixed_port(runtime._client_config()),
                        probe_url=runtime._default_probe_urls()[0],
                        timeout_seconds=.5,
                        abort_owner=_RuntimeAbortHandle(),
                    )
                except Exception:
                    pass
            # This is Core's own ordered fallback, without a selector write
            # or an application-owned manual switch/selector intent change.
            switch_deadline = time.monotonic() + 1
            while time.monotonic() < switch_deadline:
                if inspect_runtime_route(client.proxies())["leaf_proxy"] == "local-1":
                    break
                time.sleep(.01)
            assert inspect_runtime_route(client.proxies())["leaf_proxy"] == "local-1"
        return results

    monkeypatch.setattr(runtime, "_manual_current_exit_quality_contracts", contracts_then_native_switch)
    selector_intent = runtime._runtime_selector_intent_revision
    started = time.monotonic()
    result = runtime.check_current_exit_identity(
        proxy_url=f"http://127.0.0.1:{runtime._client_mixed_port(runtime._client_config())}",
        request_id="real-native-retry", timeout_seconds=8,
    )
    elapsed = time.monotonic() - started
    assert result["state"] == "complete", result
    assert result["identity"]["ipv4"]["ip"] == "8.8.8.8"
    assert result["route"]["confirmed"] is True
    assert contract_leaves == ["local-0", "local-1"]
    assert result["diagnostics"]["overview_validation_attempts"] == 2
    assert runtime._runtime_selector_intent_revision == selector_intent
    assert runtime.config["tiers"]["tier2"]["check_interval"] == 3600
    assert client.proxies()["proxies"]["PROXY-FALLBACK"]["all"] == before["proxies"]["PROXY-FALLBACK"]["all"]
    assert elapsed < 8
    print(f"One overview request reproved native fallback in {elapsed:.3f}s")
