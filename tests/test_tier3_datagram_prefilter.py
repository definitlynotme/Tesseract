from __future__ import annotations

import asyncio
import datetime
import ipaddress
import queue
import socket
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from models import Node, NodeStatus
from tester import _tcp_probe
from tier3_validation_parity_audit import _parity_helper_command, _wait_for_helpers
from tier3_validation_protocol import EndpointAttemptKind
from tier3_validator_supervisor import Tier3ValidatorSupervisor
from tier3_windows_job import WindowsJob
from tier3_refresh_types import Tier3StreamedTcpObservation
from tier3_transport_contract import tcp_reachability_is_required


@pytest.mark.parametrize("protocol,network,required", [
    ("tuic", "", False), ("hysteria2", "", False), ("wireguard", "", False),
    ("vmess", "kcp", False), ("vless", "quic", False),
    ("vmess", "ws", True), ("vless", "tcp", True), ("ss", "", True),
])
def test_upstream_contract_not_priority(protocol, network, required):
    node = Node(name="opaque", protocol=protocol, network=network,
                address="127.0.0.1", port=9, tier="tier3", status=NodeStatus.DEAD)
    assert tcp_reachability_is_required(node) is required
    observation = Tier3StreamedTcpObservation(node=node)
    assert observation.responsive is False
    assert observation.validation_required is (not required)
    node.extra["tier3_failure_stage"] = "tcp_cancelled"
    assert not Tier3StreamedTcpObservation(node=node).validation_required


def test_real_udp_only_proxy_passes_helper_but_not_tcp(tmp_path: Path) -> None:
    """Real positive/negative control: a TCP refusal is not QUIC evidence."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    binary = Path(__file__).resolve().parents[1] / "bin" / "mihomo.exe"
    if not binary.exists():
        pytest.skip("bundled Mihomo required")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        quic_port = sock.getsockname()[1]

    class Endpoint(BaseHTTPRequestHandler):
        response_delay_seconds = 0.0

        def log_message(self, *_args):
            pass

        def do_GET(self):
            time.sleep(self.response_delay_seconds)
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([
            x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        ]), critical=False).sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    identity = str(uuid.uuid4())
    config_path = tmp_path / "server.yaml"
    config_path.write_text(yaml.safe_dump({
        "tuic-server": {
            "enable": True, "listen": f"127.0.0.1:{quic_port}",
            "users": {identity: "local-test-password"}, "alpn": ["h3"],
            "certificate": str(cert_path), "private-key": str(key_path),
            "congestion-controller": "bbr", "max-idle-time": 15000,
        },
        "rules": ["MATCH,DIRECT"], "log-level": "warning",
    }), encoding="utf-8")
    node = Node(name="datagram-control", protocol="tuic", tier="tier3",
                address="127.0.0.1", port=quic_port, uuid=identity,
                password="local-test-password", sni="localhost",
                extra={"skip-cert-verify": True, "alpn": ["h3"]})
    endpoint = f"http://127.0.0.1:{server.server_port}/generate_204"
    results = queue.Queue()
    supervisor = Tier3ValidatorSupervisor(
        binary_path=str(binary), probe_urls=[endpoint], expected_statuses={204},
        probe_timeout_seconds=5.0, slow_threshold_ms=3000,
        capacity_sink=lambda _count: None, attempt_sink=results.put_nowait,
        min_workers=1, max_workers=1, temp_parent=str(tmp_path),
    )
    with WindowsJob() as job, (tmp_path / "server.log").open("wb") as log:
        process = subprocess.Popen(
            [str(binary), "-d", str(tmp_path), "-f", str(config_path)],
            stdout=log, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        job.assign_pid(process.pid)
        supervisor.start()
        try:
            _wait_for_helpers(supervisor, 1)
            assert asyncio.run(_tcp_probe(node, 1.25, 3000)).status is NodeStatus.DEAD
            for sequence, wrong in enumerate((False, True, False), start=1):
                candidate = node.clone()
                if wrong:
                    candidate.password = "incorrect-password"
                supervisor.submit(_parity_helper_command(candidate, endpoint, sequence))
                result = results.get(timeout=12)
                assert (result.kind is EndpointAttemptKind.PASS) is (not wrong), result
            assert supervisor.stop(timeout_seconds=5) == "STOPPED"
            Endpoint.response_delay_seconds = 1.0
            _assert_real_manager_admission(tmp_path, binary, node, endpoint)
        finally:
            assert supervisor.stop(timeout_seconds=5) == "STOPPED"
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def _assert_real_manager_admission(tmp_path, binary, node, endpoint):
    import manager as manager_module
    from tests.test_manager import ManagerTests
    from tier3_sources import CollectionReport

    manager_path = tmp_path / "manager"
    manager_path.mkdir()
    manager = ManagerTests()._minimal_manager(
        manager_path, tier3_enabled=True,
        tier3_overrides={"bootstrap_enabled": False,
                        "subscription_urls": ["https://opaque.invalid/source"]},
    )
    manager.testing_config.update(mihomo_helper={
        "binary_path": str(binary), "probe_urls": [endpoint],
        # The admission gate stays on, pointed at the local endpoint: the
        # default Google confirm would make this offline control depend on
        # the public network.
        "confirm_urls": [endpoint],
        "expected_statuses": [204], "probe_timeout": 5.0,
    }, mihomo_validation_temp_dir=str(tmp_path))
    manager.mark_tier3_core_running(effective_egress_ok=True)
    manager.set_effective_egress_state(True)
    manager.runtime_source_fetch_proxy_provider = lambda _phase: "http://127.0.0.1:19101"
    candidate = node.clone()
    candidate.source = "https://opaque.invalid/source"
    errors = []
    work_samples = []
    original_envelope = manager._tier3_expansion_envelope_decision

    def observe_envelope(*args, **kwargs):
        work = kwargs.get("work_snapshot")
        if work is not None:
            work_samples.append(work)
        return original_envelope(*args, **kwargs)

    async def collect(*_args, **kwargs):
        report = CollectionReport(source_count=1, fetched_sources=1, node_count=1,
            sources=[{"type": "subscription_url", "value": candidate.source, "ok": True}])
        kwargs["source_observer"]([candidate], report)
        return [candidate], report

    def refresh():
        try:
            manager.refresh_tier3(suppress_intervention_alert=True, startup_mode=False)
        except Exception as exc:
            errors.append(exc)

    with (
        patch.object(manager_module, "collect_nodes_from_sources_with_report", collect),
        patch.object(manager, "_tier3_expansion_envelope_decision", observe_envelope),
    ):
        worker = threading.Thread(target=refresh)
        worker.start()
        try:
            deadline = time.monotonic() + 12
            while not manager.runtime_nodes["tier3"] and not errors and time.monotonic() < deadline:
                time.sleep(.025)
            assert not errors
            assert len(manager.runtime_nodes["tier3"]) == 1, manager.tier3_last_collection_report
            assert manager.runtime_nodes["tier3"][0].status in {NodeStatus.ALIVE, NodeStatus.SLOW}
        finally:
            manager.request_stop()
            worker.join(timeout=6)
            manager.close(timeout_seconds=3)
        assert not worker.is_alive()
    report = manager.tier3_last_collection_report
    assert report["candidate_scheduler_active_ticket_count"] == 0
    assert report["source_stream_tcp_responsive_count"] == 0
    assert report["source_stream_admission_commit_committed_count"] == 1
    validating_after_tcp = [work for work in work_samples
                            if work.fast_active > 0
                            and work.candidate_tcp_handoff_lease_count == 0]
    assert validating_after_tcp, "real helper validation must overlap a control revision"
    assert all(work.tcp_pending_depth == 0 for work in validating_after_tcp)
