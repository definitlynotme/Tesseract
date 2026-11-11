"""Can one shared Mihomo hot-install a candidate while a sibling probe is in flight?

The round model never reloads while a lease runs, at the price of a staging
wait.  This experiment measures the alternative with the real binary and local
identity sinks: a probe through listener A is in flight (its sink answers after
a delay) when the config is reinstalled with A unchanged and B's port rebound
to a new candidate C.  The in-flight probe must still finish with A's own
credential, C must be reachable on the rebound port with C's credential, and a
port dropped from the config must refuse.  Reload latency is reported.
"""
from __future__ import annotations

import http.client
import threading
import time
from pathlib import Path

import pytest

from test_support.tier3_batch_attribution_fixtures import (
    BatchFixture,
    batch_dns_block,
    proxy_get,
    start_batch_mihomo,
)
from tier3_validation_config import build_batch_proxy_validation_config

ROOT = Path(__file__).resolve().parents[1]
MIHOMO = ROOT / "bin" / "mihomo.exe"

pytestmark = pytest.mark.skipif(not MIHOMO.exists(), reason="bundled mihomo binary required")


def _reload(controller_port: int, path: Path, timeout: float = 5.0) -> float:
    started = time.monotonic()
    connection = http.client.HTTPConnection("127.0.0.1", controller_port, timeout=timeout)
    try:
        body = '{"path": "%s", "payload": ""}' % str(path).replace("\\", "\\\\")
        connection.request("PUT", "/configs?force=true", body=body, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        response.read()
        assert response.status in (200, 204), response.status
    finally:
        connection.close()
    return (time.monotonic() - started) * 1000.0


def _wait_port(port: int, timeout: float = 5.0) -> bool:
    import socket

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.02)
    return False


def test_hot_install_keeps_in_flight_probe_and_rebinds_only_the_changed_port(tmp_path) -> None:
    with BatchFixture() as fixture:
        dns_port = fixture.start_nxdomain_dns()
        mixed_port = fixture.reserve()
        controller_port = fixture.reserve()
        # A answers slowly so its probe is provably in flight during the reload.
        a, b, c, d = fixture.build_candidates(4, slow={0: 2.5})
        # Generation 1: A on port pa, B on port pb.  Generation 2: A unchanged,
        # C takes over pb, D gets a fresh port pd; B is gone.
        pa, pb, pd = a.listener_port, b.listener_port, d.listener_port
        gen1, names1 = build_batch_proxy_validation_config(
            [(a.node, pa), (b.node, pb)], mixed_port, controller_port=controller_port,
        )
        gen1["dns"] = batch_dns_block(dns_port)
        assert all(names1)
        process = start_batch_mihomo(gen1, tmp_path / "work", [pa, pb])
        try:
            assert proxy_get(pb, b.probe_url)[0] == 204

            in_flight: dict[str, object] = {}

            def slow_probe() -> None:
                started = time.monotonic()
                try:
                    in_flight["status"] = proxy_get(pa, a.probe_url, timeout=8.0)[0]
                except Exception as exc:  # noqa: BLE001
                    in_flight["status"] = f"error:{exc}"
                in_flight["elapsed"] = time.monotonic() - started

            thread = threading.Thread(target=slow_probe, daemon=True)
            thread.start()
            time.sleep(0.5)
            assert thread.is_alive(), "the A probe must still be in flight"

            gen2, names2 = build_batch_proxy_validation_config(
                [(a.node, pa), (c.node, pb), (d.node, pd)], mixed_port, controller_port=controller_port,
            )
            gen2["dns"] = batch_dns_block(dns_port)
            assert all(names2)
            path2 = tmp_path / "work" / "gen2.yaml"
            import yaml

            path2.write_text(yaml.safe_dump(gen2, allow_unicode=True, sort_keys=False), encoding="utf-8")
            reload_ms = _reload(controller_port, path2)
            assert _wait_port(pd), "the new listener must come up"

            thread.join(10.0)
            assert not thread.is_alive()
            assert in_flight["status"] == 204, in_flight
            assert in_flight["elapsed"] >= 2.0, in_flight
            assert [record.credential_identity for record in a.records] == [a.identity]

            # The rebound port now carries C, and only C.
            status_c, _ = proxy_get(pb, c.probe_url)
            assert status_c == 204
            assert [record.credential_identity for record in c.records] == [c.identity]
            assert b.records == [record for record in b.records if record.credential_identity == b.identity]
            assert len(b.records) == 1, "B must not see the probe meant for C"
            assert proxy_get(pd, d.probe_url)[0] == 204

            # A second in-flight reload while A probes again, then A is dropped.
            thread = threading.Thread(target=slow_probe, daemon=True)
            thread.start()
            time.sleep(0.3)
            gen3, _ = build_batch_proxy_validation_config(
                [(c.node, pb), (d.node, pd)], mixed_port, controller_port=controller_port,
            )
            gen3["dns"] = batch_dns_block(dns_port)
            path3 = tmp_path / "work" / "gen3.yaml"
            path3.write_text(yaml.safe_dump(gen3, allow_unicode=True, sort_keys=False), encoding="utf-8")
            reload3_ms = _reload(controller_port, path3)
            thread.join(10.0)
            dropped_probe = in_flight["status"]
            assert not _wait_port(pa, timeout=1.0), "a dropped listener must close"
            print(
                f"hot-install reload_ms={reload_ms:.0f} reload3_ms={reload3_ms:.0f} "
                f"probe_through_dropped_listener={dropped_probe!r} dns_queries={len(fixture.dns_queries)}"
            )
            assert fixture.dns_queries == []
        finally:
            process.kill()
            process.wait(timeout=5)
