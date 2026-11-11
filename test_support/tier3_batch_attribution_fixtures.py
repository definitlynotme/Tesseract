"""Local identity sinks that prove which candidate carried a batch probe.

A shared-Mihomo batch gives every candidate its own listener bound to its own
proxy, so the risk it introduces is misattribution: a request leaving through
the wrong candidate, a dead candidate borrowing a live sibling's verdict, or a
result landing on whoever next occupies a reused port.  These fixtures make that
observable without touching the public network: each candidate is a loopback
HTTP proxy sink carrying its own credential, and a sink answers 204 only when
both the credential and the destination marker are its own, 409 otherwise, so
the probe's own status code names whether the right candidate carried it.

Like ``tier3_local_pipeline_probe``, this owns local sink identity only and
proves nothing about public-network reachability or throughput.
"""
from __future__ import annotations

import base64
import socket
import socketserver
import struct
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import yaml

from dns_resolver_policy import helper_dns_block
from models import Node
from windows_subprocess import hidden_subprocess_kwargs

ROOT = Path(__file__).resolve().parents[1]
MIHOMO = ROOT / "bin" / "mihomo.exe"


def reserve_loopback_port(excluded: frozenset[int] = frozenset()) -> int:
    """Reserve a distinct loopback port, as the validator runtime does."""

    for _attempt in range(128):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
        finally:
            sock.close()
        if port > 0 and port not in excluded:
            return port
    raise RuntimeError("unable to reserve distinct attribution ports")


@dataclass(frozen=True)
class SinkRecord:
    credential_identity: str
    target_identity: str
    verdict: int


class IdentitySink(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 128

    def __init__(self, address: tuple[str, int], handler: type, identity: str) -> None:
        super().__init__(address, handler)
        self.identity = identity
        self.records: list[SinkRecord] = []
        self.delay_seconds = 0.0


class IdentityHandler(socketserver.BaseRequestHandler):
    """Answer 204 only for this sink's own candidate, 409 for anyone else's."""

    def _headers(self) -> bytes:
        data = b""
        while b"\r\n\r\n" not in data and len(data) < 32768:
            try:
                chunk = self.request.recv(4096)
            except OSError:
                return b""
            if not chunk:
                break
            data += chunk
        return data

    @staticmethod
    def _credential(request: bytes) -> str:
        for line in request.split(b"\r\n"):
            if not line.lower().startswith(b"proxy-authorization:"):
                continue
            value = line.split(b":", 1)[1].strip()
            if not value.startswith(b"Basic "):
                return ""
            try:
                decoded = base64.b64decode(value[6:]).decode("ascii")
            except Exception:  # noqa: BLE001
                return ""
            return decoded.split(":", 1)[1] if ":" in decoded else decoded
        return ""

    @staticmethod
    def _target(request: bytes) -> str:
        """The destination, whichever of the three request shapes carries it.

        Mihomo forwards a plain HTTP request to an upstream proxy in origin
        form, so the hostname arrives in ``Host`` rather than in the request
        line; CONNECT carries it in the line instead.  Joining both makes the
        marker visible in every shape.
        """

        first = request.split(b"\r\n", 1)[0].decode("latin-1")
        parts = first.split(" ")
        line_target = parts[1] if len(parts) > 1 else ""
        host = ""
        for line in request.split(b"\r\n"):
            if line.lower().startswith(b"host:"):
                host = line.split(b":", 1)[1].strip().decode("latin-1")
                break
        return f"{host}{line_target}" if host else line_target

    def handle(self) -> None:  # noqa: D102
        self.request.settimeout(3.0)
        request = self._headers()
        if not request:
            return
        credential = self._credential(request)
        target = self._target(request)
        if request.startswith(b"CONNECT "):
            # Never refuse at CONNECT: refusing would hide the status channel.
            self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            tunneled = self._headers()
            if tunneled:
                target = self._target(tunneled) or target
                credential = credential or self._credential(tunneled)
        server: IdentitySink = self.server  # type: ignore[assignment]
        if server.delay_seconds > 0.0:
            time.sleep(server.delay_seconds)
        mine = server.identity in credential and server.identity in target
        verdict = 204 if mine else 409
        server.records.append(
            SinkRecord(credential_identity=credential, target_identity=target, verdict=verdict)
        )
        reason = b"No Content" if verdict == 204 else b"Conflict"
        self.request.sendall(
            b"HTTP/1.1 %d %s\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            % (verdict, reason)
        )


@dataclass
class BatchCandidate:
    index: int
    identity: str
    node: Node
    listener_port: int
    probe_url: str
    sink: IdentitySink | None = None
    _holder: socket.socket | None = field(default=None, repr=False)

    @property
    def records(self) -> list[SinkRecord]:
        return self.sink.records if self.sink is not None else []


class BatchFixture:
    """Owns every socket and thread the batch fixtures create."""

    def __init__(self) -> None:
        self.candidates: list[BatchCandidate] = []
        self._servers: list[socketserver.BaseServer] = []
        self._threads: list[threading.Thread] = []
        self._holders: list[socket.socket] = []
        self._reserved: set[int] = set()
        self.dns_queries: list[str] = []

    def reserve(self) -> int:
        port = reserve_loopback_port(frozenset(self._reserved))
        self._reserved.add(port)
        return port

    def _serve(self, server: socketserver.BaseServer) -> None:
        self._servers.append(server)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self._threads.append(thread)

    def build_candidates(
        self,
        count: int,
        *,
        dead: frozenset[int] = frozenset(),
        slow: dict[int, float] | None = None,
        first_index: int = 0,
        listener_ports: Sequence[int] | None = None,
    ) -> list[BatchCandidate]:
        slow = slow or {}
        built: list[BatchCandidate] = []
        for offset in range(count):
            index = first_index + offset
            identity = f"cand{index:02d}"
            listener_port = (
                int(listener_ports[offset]) if listener_ports is not None else self.reserve()
            )
            probe_url = f"http://{identity}.probe.invalid/generate_204"
            if offset in dead or index in dead:
                # Bound but never listening, so the dial is refused at once and
                # no unrelated endpoint is ever contacted.
                holder = socket.socket()
                holder.bind(("127.0.0.1", 0))
                upstream_port = int(holder.getsockname()[1])
                self._holders.append(holder)
                sink = None
            else:
                upstream_port = self.reserve()
                sink = IdentitySink(("127.0.0.1", upstream_port), IdentityHandler, identity)
                sink.delay_seconds = float(slow.get(offset, slow.get(index, 0.0)))
                self._serve(sink)
            node = Node(
                name=identity,
                protocol="http",
                uri=f"http://127.0.0.1:{upstream_port}",
                address="127.0.0.1",
                port=upstream_port,
                source="batch-attribution",
                tier="tier3",
                password=identity,
                extra={
                    "username": "batch",
                    "tier3_source_root_id": "batch-root",
                    "tier3_source_unit_family": "http",
                },
            )
            built.append(
                BatchCandidate(
                    index=index,
                    identity=identity,
                    node=node,
                    listener_port=listener_port,
                    probe_url=probe_url,
                    sink=sink,
                )
            )
        self.candidates.extend(built)
        return built

    def start_nxdomain_dns(self) -> int:
        """A local TCP resolver that answers every query with no records."""

        queries = self.dns_queries

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:  # noqa: D102
                self.request.settimeout(2.0)
                header = self.request.recv(2)
                if len(header) < 2:
                    return
                length = struct.unpack("!H", header)[0]
                payload = b""
                while len(payload) < length:
                    chunk = self.request.recv(length - len(payload))
                    if not chunk:
                        return
                    payload += chunk
                labels, cursor = [], 12
                while cursor < len(payload):
                    size = payload[cursor]
                    if size == 0:
                        break
                    labels.append(payload[cursor + 1 : cursor + 1 + size].decode("latin-1"))
                    cursor += size + 1
                queries.append(".".join(labels))
                answer = payload[:2] + b"\x81\x83" + payload[4:6] + b"\x00\x00\x00\x00\x00\x00"
                answer += payload[12:]
                self.request.sendall(struct.pack("!H", len(answer)) + answer)

        port = self.reserve()
        server = socketserver.ThreadingTCPServer(("127.0.0.1", port), Handler)
        server.daemon_threads = True
        self._serve(server)
        return port

    def close(self) -> None:
        for server in self._servers:
            try:
                server.shutdown()
                server.server_close()
            except Exception:  # noqa: BLE001
                pass
        for holder in self._holders:
            try:
                holder.close()
            except Exception:  # noqa: BLE001
                pass
        self._servers.clear()
        self._holders.clear()

    def __enter__(self) -> "BatchFixture":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def batch_dns_block(dns_port: int) -> dict[str, Any]:
    """The helper DNS contract, pointed at a local resolver so nothing leaks."""

    block = dict(helper_dns_block(ipv6=False))
    for key in ("nameserver", "default-nameserver", "proxy-server-nameserver"):
        block[key] = [f"tcp://127.0.0.1:{int(dns_port)}"]
    block.pop("nameserver-policy", None)
    return block


def start_batch_mihomo(
    config: dict[str, Any],
    work_dir: Path,
    ports: Sequence[int],
    *,
    timeout_seconds: float = 25.0,
) -> subprocess.Popen:
    """Start one Mihomo for a batch and wait until every listener serves."""

    work_dir.mkdir(parents=True, exist_ok=True)
    config_path = work_dir / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8",
    )
    process = subprocess.Popen(
        [str(MIHOMO), "-d", str(work_dir), "-f", str(config_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        **hidden_subprocess_kwargs(),
    )
    deadline = time.monotonic() + timeout_seconds
    pending = list(ports)
    while pending and time.monotonic() < deadline:
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout else ""
            raise AssertionError(f"mihomo exited before serving the batch: {output[:1200]}")
        remaining = []
        for port in pending:
            try:
                with socket.create_connection(("127.0.0.1", int(port)), timeout=0.2):
                    continue
            except OSError:
                remaining.append(port)
        pending = remaining
        if pending:
            time.sleep(0.05)
    if pending:
        try:
            process.kill()
        finally:
            pass
        raise AssertionError(f"listeners never came up: {pending}")
    return process


def proxy_get(port: int, url: str, *, timeout: float = 4.0) -> tuple[int, bytes]:
    """Absolute-form GET through a loopback proxy listener."""

    import http.client

    connection = http.client.HTTPConnection("127.0.0.1", int(port), timeout=timeout)
    try:
        connection.request("GET", url, headers={"Host": url.split("/")[2]})
        response = connection.getresponse()
        return int(response.status), response.read()
    finally:
        connection.close()
