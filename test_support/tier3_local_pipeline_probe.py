"""Local-only source/proxy endpoints; real collection, validation and admission.

No public-network performance or TUN-bypass conclusion can follow from this
fixture. Only source content and its upstream HTTP proxy transport are owned.
"""
import argparse
import base64
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import ipaddress
import json
import logging
import os
from pathlib import Path
import socket
import socketserver
import ssl
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from runtime_source_transport import RuntimeSourceListenerCredentials, RuntimeSourceListenerSpec, RuntimeSourceRouteKind
from tests.test_manager import ManagerTests
from windows_subprocess import hidden_subprocess_kwargs
from tier3_validation_domain import DEFAULT_PROCESS_CONFIRM_URLS, DEFAULT_PROCESS_PROBE_URLS
from tier3_candidate_scheduler import Tier3CandidateScheduler
from tier3_validator_supervisor import Tier3ValidatorSupervisor


def progress_summary(trace, elapsed_end):
    ready = [item["elapsed"] for item in trace
             if item["event"] == "helper_ready" and item["elapsed"] <= elapsed_end]
    if not ready:
        return {"ready": False, "max_gap_seconds": None, "events": 0}
    first_ready = min(ready)
    progress = sorted(item["elapsed"] for item in trace
                      if first_ready <= item["elapsed"] <= elapsed_end
                      and (item["event"] == "started"
                           or (item["event"] == "validation_completion" and item["transport"])))
    timeline = [first_ready, *progress, elapsed_end]
    return {"ready": True, "first_ready_seconds": first_ready,
            "max_gap_seconds": max(later - earlier for earlier, later in zip(timeline, timeline[1:])),
            "events": len(progress)}


class ProxyHandler(socketserver.BaseRequestHandler):
    def headers(self):
        body = b""
        while b"\r\n\r\n" not in body and len(body) < 32768:
            part = self.request.recv(4096)
            if not part:
                break
            body += part
        return body

    def handle(self):
        self.request.settimeout(5)
        try:
            request = self.headers()
            if not request:
                return  # Real TCP prefilter connects without an HTTP request.
            if self.server.profile == "mixed-tail":
                authorization = next((line.split(b":", 1)[1].strip() for line in request.split(b"\r\n")
                    if line.lower().startswith(b"proxy-authorization:")), b"")
                if not authorization.startswith(b"Basic "):
                    raise ValueError("fixture proxy requires candidate-specific authentication")
                identity = base64.b64decode(authorization[6:]).decode("ascii")
                index = int(identity.rsplit("-", 1)[1])
                if index >= 8 and index % 16 != 0:
                    self.server.rejected_requests += 1
                    self.request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                    return
            if request.startswith(b"CONNECT "):
                self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                if self.request.recv(1, socket.MSG_PEEK) == b"\x16":
                    self.request = self.server.tls_context.wrap_socket(self.request, server_side=True)
                    self.server.tls_requests += 1
                request = self.headers()
            if request:
                self.server.probe_requests += 1
                self.request.sendall(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        except (OSError, TimeoutError):
            return


class ProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 128
    probe_requests = 0
    rejected_requests = 0
    tls_requests = 0
    profile = "all-responsive"


class SourceHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = self.server.payload
        self.server.fetches += 1
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def listener_port():
    with socket.socket() as candidate:
        candidate.bind(("127.0.0.1", 0))
        return candidate.getsockname()[1]


def local_tls_context(directory):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Tier3 local-only fixture")])
    now = datetime.now(timezone.utc)
    # Probe and admission-gate (confirm) hosts both answer through the local TLS fixture.
    hosts = sorted({urlsplit(url).hostname
                    for url in (*DEFAULT_PROCESS_PROBE_URLS, *DEFAULT_PROCESS_CONFIRM_URLS)})
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([
            *(x509.DNSName(host) for host in hosts),
            x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        ]), critical=False).sign(key, hashes.SHA256()))
    cert_path, key_path = directory / "fixture-ca.pem", directory / "fixture-key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    # Scoped to this diagnostic and its fresh Python helpers, never the host trust store.
    os.environ["SSL_CERT_FILE"] = str(cert_path)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert_path), str(key_path))
    return context


def unauthenticated_status(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=.1) as connection:
            connection.sendall(b"GET http://127.0.0.1/ HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
            return int(connection.recv(1024).split(b" ")[1])
    except (OSError, ValueError, IndexError):
        return None


def session(directory, source_url, spec, label, profile):
    owner = ManagerTests()._minimal_manager(directory, tier3_enabled=True,
        tier3_overrides={"bootstrap_enabled": True, "min_alive_nodes": 3,
                         "max_pool_size": 25, "pi_controller": {"target_alive": 25}})
    Path(owner.sources_file).write_text(yaml.safe_dump({"subscription_urls": [source_url],
                                                      "github_repos": [], "local_files": []}), encoding="utf-8")
    owner.testing_config.update(validation_mode="full", request_timeout=5, tcp_timeout=5, concurrency=16,
        mihomo_binary_path=str(ROOT / "bin" / "mihomo.exe"),
        mihomo_validation_temp_dir=str(directory),
        mihomo_helper={"binary_path": str(ROOT / "bin" / "mihomo.exe"),
                       "probe_urls": [source_url.rsplit("/", 1)[0] + "/generate_204"],
                       "expected_statuses": [204], "probe_timeout": 5})
    owner.validation_mode = "full"
    owner.bind_runtime_generation(1 if label == "cold" else 2)
    assert not owner.runtime_nodes["tier3"], "controlled session must begin with an empty pool"
    endpoint = replace(spec, runtime_generation=owner.runtime_generation).endpoint(
        ready=True, priority=0, stagger_ms=0)
    owner.runtime_source_fetch_endpoint_provider = lambda *_args: (endpoint,)
    owner.runtime_source_fetch_proxy_provider = lambda *_args: endpoint.proxy_url
    started = time.monotonic()
    target = threading.Event()
    errors, milestones = [], []

    def committed(_nodes, report):
        milestones.append((time.monotonic() - started, report["alive_count"]))
        if report["alive_count"] >= 25:
            target.set()

    owner.on_tier3_pool_committed = committed

    def refresh():
        try:
            owner.refresh_tier3(suppress_intervention_alert=True, startup_mode=True)
        except BaseException:
            errors.append(traceback.format_exc())

    worker = threading.Thread(target=refresh, name="controlled-manager-refresh")
    trace = []
    original_reserve = Tier3CandidateScheduler.reserve_handoffs
    original_commit = Tier3CandidateScheduler.commit_handoffs
    original_progress = Tier3ValidatorSupervisor._handle_startup_progress
    original_ready = Tier3ValidatorSupervisor._handle_ready
    original_attempt = Tier3ValidatorSupervisor._emit_attempt

    def observed_progress(supervisor, helper, payload):
        trace.append({'event': 'helper_progress', 'elapsed': time.monotonic() - started,
                      'worker': helper.worker_id, 'phase': payload.get('phase'),
                      'reported_elapsed': payload.get('monotonic_ns', 0) / 1e9 - started})
        return original_progress(supervisor, helper, payload)

    def observed_ready(supervisor, helper, payload):
        trace.append({'event': 'helper_ready', 'elapsed': time.monotonic() - started,
                      'worker': helper.worker_id})
        return original_ready(supervisor, helper, payload)

    def observed_attempt(supervisor, result):
        trace.append({"event": "validation_completion", "elapsed": time.monotonic() - started,
                      "stage": result.stage.value, "kind": result.kind.value,
                      "transport": result.portfolio_started_monotonic_ns > 0
                          and result.kind.value not in {"facility_fail", "cancelled"}})
        return original_attempt(supervisor, result)

    def observed_reserve(scheduler, *args, **kwargs):
        result = original_reserve(scheduler, *args, **kwargs)
        for reservation in result:
            trace.append({"event": "reserved", "elapsed": time.monotonic() - started,
                "ticket": reservation.ticket.ticket_id,
                "fixture_index": int(reservation.candidate.node.password.rsplit("-", 1)[1]),
                "visibility": reservation.ticket.visibility_class,
                "lane": reservation.ticket.selection_lane})
        return result

    def observed_commit(scheduler, *args, **kwargs):
        result = original_commit(scheduler, *args, **kwargs)
        for ticket in result:
            trace.append({"event": "started", "elapsed": time.monotonic() - started,
                "ticket": ticket.ticket_id, "visibility": ticket.visibility_class,
                "lane": ticket.selection_lane})
        return result

    with patch.object(Tier3CandidateScheduler, "reserve_handoffs", observed_reserve), \
         patch.object(Tier3CandidateScheduler, "commit_handoffs", observed_commit), \
         patch.object(Tier3ValidatorSupervisor, '_handle_startup_progress', observed_progress), \
         patch.object(Tier3ValidatorSupervisor, '_handle_ready', observed_ready), \
         patch.object(Tier3ValidatorSupervisor, '_emit_attempt', observed_attempt):
        worker.start()
        deadline = started + 61
        while worker.is_alive() and not target.is_set() and time.monotonic() < deadline:
            target.wait(.05)
        reached = target.is_set()
        work_ended = time.monotonic() - started
        stop_started = time.monotonic()
        owner.request_stop()
        worker.join(6)
        stop_status = owner.close(timeout_seconds=3)
    (directory / f"trace-{label}.json").write_text(json.dumps(trace, indent=2), encoding="utf-8")
    report = dict(owner.tier3_last_collection_report or {})
    persisted = json.loads((directory / "nodes.json").read_text())
    cache = json.loads((directory / "tier_observation_cache.json").read_text())
    accepted_pass_count = sum(bool(record.get("last_mixed_port_pass_at")) for record in cache["nodes"])
    progress = progress_summary(trace, work_ended)
    summary = {"scope": "local_controlled_not_network_acceptance", "case": label, "profile": profile,
        "reached_target": reached, "stop_status": stop_status,
        "worker_alive": worker.is_alive(), "stop_seconds": round(time.monotonic() - stop_started, 3),
        "persisted_nodes": len(persisted.get("nodes", [])), "errors": errors,
        "accepted_pass_cache_records": accepted_pass_count,
        "progress": progress,
        "milestones": milestones,
        "report": {key: value for key, value in report.items() if key in {
            "source_stream_time_to_pool_revision_3_ms", "source_stream_time_to_pool_target_ms",
            "source_stream_alive_at_20s", "source_stream_full_validation_passed_count",
            "source_stream_admission_commit_committed_count", "source_stream_admission_commit_skipped_count",
            "source_stream_tcp_start_acked_count", "source_stream_tcp_start_deferred_count",
            "source_stream_tcp_scanned_count", "source_stream_tcp_responsive_count",
            "source_stream_tcp_entitlement_exhausted_count", "source_stream_full_validation_deferred_count",
            "source_stream_primary_full_validation_passed_count", "source_stream_full_validation_failed_count",
            "source_stream_shutdown_confirmed", "candidate_scheduler_active_ticket_count",
            "candidate_scheduler_tcp_handoff_lease_count", "candidate_scheduler_reserved_handoff_count",
            "source_stream_admission_commit_pending_count", "source_stream_process_active_helper_count",
            "source_stream_observation_batch_pending_count", "source_stream_observation_batch_inflight_count",
            "source_stream_observation_batch_worker_count",
            "source_supply_worker_alive"}}}
    gates = {
        "target": reached and len(persisted.get("nodes", [])) >= 25,
        "n3": 0 < (report.get("source_stream_time_to_pool_revision_3_ms") or 999999) <= 10000,
        "n20": (report.get("source_stream_alive_at_20s") or 0) >= 15,
        "n25": 0 < (report.get("source_stream_time_to_pool_target_ms") or 999999) <= (50000 if label == "warm" else 61000),
        "accepted_pass_cache_preserved": accepted_pass_count >= len(persisted.get("nodes", [])),
        "no_internal_progress_gap": progress["ready"] and progress["events"] > 0
            and progress["max_gap_seconds"] <= 5.75,
        "pass_conservation": (report.get("source_stream_full_validation_passed_count")
            == (report.get("source_stream_admission_commit_committed_count", -1)
                + report.get("source_stream_admission_commit_skipped_count", -1))),
        "closed": not errors and not worker.is_alive() and stop_status == "STOPPED"
            and report.get("source_stream_shutdown_confirmed") is True
            and report.get("source_supply_worker_alive") is False
            and all(report.get(key) == 0 for key in (
                "candidate_scheduler_active_ticket_count", "candidate_scheduler_tcp_handoff_lease_count",
                "candidate_scheduler_reserved_handoff_count", "source_stream_admission_commit_pending_count",
                "source_stream_process_active_helper_count", "source_stream_observation_batch_pending_count",
                "source_stream_observation_batch_inflight_count", "source_stream_observation_batch_worker_count")),
    }
    summary["controlled_gates"] = gates
    (directory / f"result-{label}.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary), flush=True)
    return all(gates.values())


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--candidates", type=int, default=40)
    parser.add_argument("--profile", choices=("all-responsive", "mixed-tail"), default="all-responsive")
    args = parser.parse_args(argv)
    if not 25 <= args.candidates <= 20000:
        parser.error("candidates must be between 25 and 20000")
    directory = args.directory.resolve()
    directory.relative_to(ROOT / ".build")
    directory.mkdir(exist_ok=False)
    logging.basicConfig(filename=str(directory / "manager.log"), level=logging.INFO)
    proxy = ProxyServer(("127.0.0.1", 0), ProxyHandler)
    proxy.profile = args.profile
    proxy.tls_context = local_tls_context(directory)
    refused = socket.socket()
    refused.bind(("127.0.0.1", 0))
    refused_port = refused.getsockname()[1]  # Held open but never listening; no unrelated endpoint is contacted.
    source = ThreadingHTTPServer(("127.0.0.1", 0), SourceHandler)
    source.fetches = 0
    source.payload = "\n".join(
        f"http://local-fixture:fixture-{index}@127.0.0.1:"
        f"{refused_port if args.profile == 'mixed-tail' and index >= 8 and index % 4 else proxy.server_address[1]}#local-{index}"
        for index in range(args.candidates)
    ).encode()
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (proxy, source)]
    for thread in threads:
        thread.start()
    spec = RuntimeSourceListenerSpec(RuntimeSourceRouteKind.DIRECT, listener_port(), "DIRECT",
                                    0, 1, RuntimeSourceListenerCredentials.generate())
    config = {"allow-lan": False, "mode": "rule", "log-level": "warning",
              "ipv6": False, "dns": {"enable": False}, "rules": ["MATCH,REJECT"],
              "listeners": [spec.to_mihomo_listener()]}
    config_file = directory / "private-core.yaml"
    config_file.write_text(yaml.safe_dump(config), encoding="utf-8")
    with (directory / "private-core.log").open("w") as log:
        core = subprocess.Popen([str(ROOT / "bin" / "mihomo.exe"), "-d", str(directory), "-f", str(config_file)],
                                stdout=log, stderr=subprocess.STDOUT, **hidden_subprocess_kwargs())
        try:
            deadline = time.monotonic() + 5
            while unauthenticated_status(spec.port) != 407:
                if core.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("private listener did not establish authenticated boundary")
                time.sleep(.025)
            source_url = f"http://127.0.0.1:{source.server_address[1]}/subscription.yaml"
            cold = session(directory, source_url, spec, "cold", args.profile)
            warm = session(directory, source_url, spec, "warm", args.profile)
            print(json.dumps({"source_fetches": source.fetches, "real_proxy_requests": proxy.probe_requests,
                              "real_proxy_rejections": proxy.rejected_requests,
                              "local_tls_requests": proxy.tls_requests}), flush=True)
            return 0 if cold and warm else 1
        finally:
            core.terminate()
            try:
                core.wait(timeout=3)
            except subprocess.TimeoutExpired:
                core.kill()
                core.wait(timeout=3)
            for server in (proxy, source):
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(1)
            refused.close()


if __name__ == "__main__":
    raise SystemExit(main())
