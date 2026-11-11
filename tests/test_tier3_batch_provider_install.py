"""Per-slot provider install on the real binary: the batch backend's facility contract.

Layout under test is the one ``BatchValidatorHelperRuntime`` installs once per
group: listener W -> select group W -> file provider W.  Installing a candidate
is a provider update, never a config reload.  These pin, with local identity
sinks and a DNS that answers nothing:

* a probe in flight on listener A is untouched by an install on B;
* after the install, B's listener routes to the new candidate with only that
  candidate's credential, and the old candidate sees nothing;
* a candidate Mihomo rejects answers non-2xx and the previous proxy stays live
  (poison-pill attribution by status, no error-text parsing, no reload);
* sixteen concurrent installs all succeed and every listener routes to its own
  candidate;
* the REJECT placeholder gives an idle slot no egress.
"""
from __future__ import annotations

import http.client
import threading
import time
from pathlib import Path

import pytest
import yaml

from test_support.tier3_batch_attribution_fixtures import (
    BatchFixture,
    batch_dns_block,
    proxy_get,
    start_batch_mihomo,
)
from tier3_validation_config import (
    batch_provider_name,
    batch_provider_placeholder,
    build_batch_provider_validation_config,
    render_batch_provider_proxy,
)

ROOT = Path(__file__).resolve().parents[1]
MIHOMO = ROOT / "bin" / "mihomo.exe"

pytestmark = pytest.mark.skipif(not MIHOMO.exists(), reason="bundled mihomo binary required")


def _controller(port: int, method: str, path: str, timeout: float = 5.0) -> tuple[int, bytes, float]:
    started = time.monotonic()
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request(method, path, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        return int(response.status), response.read()[:300], (time.monotonic() - started) * 1000.0
    finally:
        connection.close()


def _write_provider(home: Path, key: int, proxy: dict) -> None:
    (home / f"provider-{key}.yaml").write_text(
        yaml.safe_dump({"proxies": [proxy]}, allow_unicode=True, sort_keys=False), encoding="utf-8",
    )


def _start(fixture: BatchFixture, home: Path, slots, dns_port: int):
    home.mkdir(parents=True, exist_ok=True)
    for key, _candidate in slots:
        _write_provider(home, key, batch_provider_placeholder(key))
    config = build_batch_provider_validation_config(
        [(key, candidate.listener_port) for key, candidate in slots],
        fixture.reserve(),
        provider_dir=str(home),
        controller_port=fixture.reserve(),
    )
    config["dns"] = batch_dns_block(dns_port)
    controller_port = int(config["external-controller"].rsplit(":", 1)[1])
    process = start_batch_mihomo(config, home, [candidate.listener_port for _key, candidate in slots])
    return process, controller_port


def _install(home: Path, controller_port: int, key: int, candidate, *, poison: bool = False) -> tuple[int, bytes, float]:
    proxy = render_batch_provider_proxy(candidate.node, key)
    assert proxy is not None
    if poison:
        proxy = dict(proxy, type="vless", uuid="not-a-uuid", **{"reality-opts": {"public-key": "x", "short-id": "zz"}})
    _write_provider(home, key, proxy)
    return _controller(controller_port, "PUT", f"/providers/proxies/{batch_provider_name(key)}")


def test_provider_install_leaves_sibling_probe_untouched_and_rejects_poison_alone(tmp_path) -> None:
    with BatchFixture() as fixture:
        dns_port = fixture.start_nxdomain_dns()
        a, b, c, d = fixture.build_candidates(4, slow={0: 2.5})
        home = tmp_path / "mihomo"
        process, controller_port = _start(fixture, home, [(1, a), (2, b)], dns_port)
        try:
            # Placeholder: no egress before any install.
            status, _ = proxy_get(a.listener_port, a.probe_url)
            assert status != 204 and a.records == []

            assert _install(home, controller_port, 1, a)[0] == 204
            assert _install(home, controller_port, 2, b)[0] == 204
            assert proxy_get(b.listener_port, b.probe_url)[0] == 204
            assert [record.credential_identity for record in b.records] == [b.identity]

            in_flight: dict[str, object] = {}

            def slow_probe() -> None:
                started = time.monotonic()
                try:
                    in_flight["status"] = proxy_get(a.listener_port, a.probe_url, timeout=8.0)[0]
                except Exception as exc:  # noqa: BLE001
                    in_flight["status"] = f"error:{exc}"
                in_flight["elapsed"] = time.monotonic() - started

            thread = threading.Thread(target=slow_probe, daemon=True)
            thread.start()
            time.sleep(0.5)
            assert thread.is_alive(), "the A probe must still be in flight"

            status, _body, install_ms = _install(home, controller_port, 2, c)
            assert status == 204
            assert proxy_get(b.listener_port, c.probe_url)[0] == 204
            assert [record.credential_identity for record in c.records] == [c.identity]
            assert len(b.records) == 1, "B must not see the probe meant for C"

            thread.join(10.0)
            assert not thread.is_alive()
            assert in_flight["status"] == 204, in_flight
            assert in_flight["elapsed"] >= 2.0, in_flight
            assert [record.credential_identity for record in a.records] == [a.identity]

            # Poison pill: rejected with a status, C stays live, nothing reloaded.
            status, body, _ms = _install(home, controller_port, 2, d, poison=True)
            assert status >= 400, (status, body)
            assert proxy_get(b.listener_port, c.probe_url)[0] == 204
            assert len(c.records) == 2
            assert d.records == []
            assert fixture.dns_queries == []
            print(f"provider install_ms={install_ms:.0f} poison_status={status}")
        finally:
            process.kill()
            process.wait(timeout=5)


def test_sixteen_concurrent_provider_installs_route_every_listener_to_its_own_candidate(tmp_path) -> None:
    with BatchFixture() as fixture:
        dns_port = fixture.start_nxdomain_dns()
        candidates = fixture.build_candidates(16)
        slots = list(enumerate(candidates, start=1))
        home = tmp_path / "mihomo"
        process, controller_port = _start(fixture, home, slots, dns_port)
        try:
            results: dict[int, tuple[int, bytes, float]] = {}

            def install(key: int, candidate) -> None:
                results[key] = _install(home, controller_port, key, candidate)

            started = time.monotonic()
            threads = [threading.Thread(target=install, args=(key, candidate)) for key, candidate in slots]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10.0)
            wall_ms = (time.monotonic() - started) * 1000.0
            assert sorted(results) == [key for key, _candidate in slots]
            assert {status for status, _body, _ms in results.values()} == {204}, results
            for key, candidate in slots:
                assert proxy_get(candidate.listener_port, candidate.probe_url)[0] == 204
                assert [record.credential_identity for record in candidate.records] == [candidate.identity]
            assert fixture.dns_queries == []
            slowest = max(ms for _status, _body, ms in results.values())
            print(f"16 concurrent provider installs: wall_ms={wall_ms:.0f} slowest_ms={slowest:.0f}")
            assert wall_ms < 5000
        finally:
            process.kill()
            process.wait(timeout=5)
