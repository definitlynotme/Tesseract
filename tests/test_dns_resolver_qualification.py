from __future__ import annotations

import datetime as dt
import ipaddress
import json
import socket
import ssl
import struct
import threading
import time
from pathlib import Path

import pytest

import dns_resolver_qualification as qual
from dns_resolver_memory import ResolverMemory
from dns_resolver_policy import (
    CANDIDATE_ANSWER_MISMATCH,
    CANDIDATE_CONNECT_FAILED,
    CANDIDATE_NOT_PROBED,
    CANDIDATE_QUALIFIED,
    CANDIDATE_TIMEOUT,
    CANDIDATE_TLS_FAILED,
    LINK_ANSWERS_INTERNAL_ONLY,
    LINK_ANSWERS_PUBLIC,
    LINK_CAPTIVE_SUSPECTED,
    LINK_OUTER_FAKE_IP,
    LINK_SILENT,
    TRANSPORT_DOH,
    TRANSPORT_DOT,
    EncryptedCandidate,
    EncryptedCandidateResult,
    encrypted_candidates,
)
from dns_resolver_qualification import (
    DiscoveredAdapter,
    LinkDiscovery,
    QualificationHint,
    discover_link_resolvers,
    probe_encrypted_candidate,
    qualify_resolvers,
)
from runtime_source_dns import PlainAnswer, PlainProbeResult, _parse_dns_answers, probe_plain_resolvers


# ----------------------------------------------------------------- helpers

def dns_reply(query: bytes, addresses: list[str], *, rcode: int = 0) -> bytes:
    """Build a wire reply that echoes the question and answers with A/AAAA records."""

    qdcount = struct.unpack_from("!H", query, 4)[0]
    offset = 12
    for _ in range(qdcount):
        while query[offset] != 0:
            offset += 1 + query[offset]
        offset += 5
    question = query[12:offset]
    flags = 0x8180 | (rcode & 0xF)
    header = query[:2] + struct.pack("!HHHHH", flags, qdcount, len(addresses), 0, 0)
    answers = b""
    for address in addresses:
        parsed = ipaddress.ip_address(address)
        if parsed.version == 4:
            answers += b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 60, 4) + parsed.packed
        else:
            answers += b"\xc0\x0c" + struct.pack("!HHIH", 28, 1, 60, 16) + parsed.packed
    return header + question + answers


def question_name(query: bytes) -> str:
    labels = []
    offset = 12
    while query[offset] != 0:
        length = query[offset]
        labels.append(query[offset + 1: offset + 1 + length].decode("ascii"))
        offset += 1 + length
    return ".".join(labels)


class UdpResponder:
    def __init__(self, answers, *, silent: bool = False):
        self.answers = answers
        self.silent = silent
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.05)
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                payload, address = self.sock.recvfrom(1232)
            except socket.timeout:
                continue
            except OSError:
                return
            if self.silent:
                continue
            answer = self.answers(question_name(payload))
            if answer is None:
                continue
            addresses, rcode = answer
            try:
                self.sock.sendto(dns_reply(payload, addresses, rcode=rcode), address)
            except OSError:
                return

    def close(self):
        self._stop.set()
        self.thread.join(1.0)
        self.sock.close()


def make_cert(*, with_ip_san: bool):
    cryptography = pytest.importorskip("cryptography")
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "resolver.test")])
    now = dt.datetime.now(dt.timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
    )
    san = [x509.DNSName("resolver.test")]
    if with_ip_san:
        san.append(x509.IPAddress(ipaddress.ip_address("127.0.0.1")))
    builder = builder.add_extension(x509.SubjectAlternativeName(san), critical=False)
    cert = builder.sign(key, hashes.SHA256())
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return cert_pem, key_pem


class TlsResponder:
    """Loopback DoT/DoH responder using a freshly minted certificate."""

    def __init__(self, tmp_path: Path, *, with_ip_san: bool, mode: str, answers=None, hang: bool = False):
        cert_pem, key_pem = make_cert(with_ip_san=with_ip_san)
        self.cert_pem = cert_pem
        cert_file = tmp_path / f"{mode}-{with_ip_san}.pem"
        cert_file.write_bytes(cert_pem + key_pem)
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(str(cert_file))
        self.mode = mode
        self.answers = answers or (lambda name: (["9.9.9.9"], 0))
        self.hang = hang
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.sock.settimeout(0.05)
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        conn.settimeout(2.0)
        try:
            secure = self.context.wrap_socket(conn, server_side=True)
        except (ssl.SSLError, OSError):
            conn.close()
            return
        try:
            if self.hang:
                self._stop.wait(3.0)
                return
            if self.mode == "dot":
                header = secure.recv(2)
                length = int.from_bytes(header, "big")
                query = secure.recv(length)
                addresses, rcode = self.answers(question_name(query))
                reply = dns_reply(query, addresses, rcode=rcode)
                secure.sendall(len(reply).to_bytes(2, "big") + reply)
            else:
                request = b""
                while b"\r\n\r\n" not in request:
                    chunk = secure.recv(4096)
                    if not chunk:
                        return
                    request += chunk
                first_line = request.split(b"\r\n", 1)[0].decode("ascii", "replace")
                path = first_line.split(" ")[1]
                import base64
                encoded = path.split("dns=", 1)[1].split("&")[0]
                padding = "=" * (-len(encoded) % 4)
                query = base64.urlsafe_b64decode(encoded + padding)
                addresses, rcode = self.answers(question_name(query))
                reply = dns_reply(query, addresses, rcode=rcode)
                secure.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/dns-message\r\n"
                    + f"Content-Length: {len(reply)}\r\nConnection: close\r\n\r\n".encode("ascii")
                    + reply
                )
        except (ssl.SSLError, OSError, IndexError, ValueError):
            pass
        finally:
            try:
                secure.close()
            except OSError:
                pass

    def client_context(self) -> ssl.SSLContext:
        context = ssl.create_default_context(cadata=self.cert_pem.decode("ascii"))
        return context

    def close(self):
        self._stop.set()
        self.thread.join(1.0)
        self.sock.close()


def loopback_candidate(port: int, transport: str, *, control_answers=frozenset({"9.9.9.9"})) -> EncryptedCandidate:
    return EncryptedCandidate(
        provider_id="test",
        display_name="Test",
        address="127.0.0.1",
        transport=transport,
        url=f"https://127.0.0.1/dns-query" if transport == TRANSPORT_DOH else "tls://127.0.0.1",
        control_name="dns.quad9.net",
        control_answers=control_answers,
        family=4,
        port=port,
    )


# ------------------------------------------------------------- wire parser

def test_parse_dns_answers_handles_compression_rcode_and_truncation():
    from runtime_source_dns import _dns_query_packet

    query = _dns_query_packet("one.one.one.one")
    reply = dns_reply(query, ["1.1.1.1", "2606:4700:4700::1111"])
    parsed = _parse_dns_answers(reply)
    assert parsed == PlainAnswer(0, ("1.1.1.1", "2606:4700:4700::1111"))
    nxdomain = _parse_dns_answers(dns_reply(query, [], rcode=3))
    assert nxdomain == PlainAnswer(3, ())
    truncated = _parse_dns_answers(reply[:-3])
    assert truncated is not None and truncated.addresses == ("1.1.1.1",)
    assert _parse_dns_answers(query) is None  # a question is not an answer
    assert _parse_dns_answers(b"\x00" * 5) is None


# ------------------------------------------------------------ plain probes

def test_probe_plain_resolvers_reports_latency_and_answers_per_name(monkeypatch):
    responder = UdpResponder(lambda name: (["1.1.1.1"] if name == "one.one.one.one" else ["8.8.8.8"], 0))
    monkeypatch.setattr(qual, "LINK_PROBE_BUDGET_SECONDS", 0.5)
    try:
        results = probe_plain_resolvers(["127.0.0.1"], ("one.one.one.one", "dns.google"), port=responder.port)
    finally:
        responder.close()
    result = results["127.0.0.1"]
    assert result.replied and result.latency_ms is not None and result.latency_ms < 500
    assert result.answers["one.one.one.one"].addresses == ("1.1.1.1",)
    assert result.answers["dns.google"].addresses == ("8.8.8.8",)


def test_probe_plain_resolvers_silence_is_bounded():
    responder = UdpResponder(lambda name: None, silent=True)
    started = time.monotonic()
    try:
        results = probe_plain_resolvers(["127.0.0.1"], ("one.one.one.one",), port=responder.port, budget_seconds=0.3)
    finally:
        responder.close()
    assert time.monotonic() - started < 1.0
    assert results["127.0.0.1"].replied is False


@pytest.mark.parametrize("answers,expected", [
    ({"one.one.one.one": PlainAnswer(0, ("1.1.1.1",)), "dns.google": PlainAnswer(0, ("8.8.8.8",))}, LINK_ANSWERS_PUBLIC),
    ({"one.one.one.one": PlainAnswer(3, ()), "dns.google": PlainAnswer(2, ())}, LINK_ANSWERS_INTERNAL_ONLY),
    ({"one.one.one.one": PlainAnswer(0, ("10.1.1.1",)), "dns.google": PlainAnswer(0, ("10.1.1.1",))}, LINK_CAPTIVE_SUSPECTED),
    ({"one.one.one.one": PlainAnswer(0, ("203.0.113.9",)), "dns.google": PlainAnswer(0, ("203.0.113.9",))}, LINK_CAPTIVE_SUSPECTED),
    ({"one.one.one.one": PlainAnswer(0, ("192.168.0.1",))}, LINK_CAPTIVE_SUSPECTED),
])
def test_link_result_classification(answers, expected):
    result = PlainProbeResult("127.0.0.1", 5, answers)
    assert qual._classify_link_result(result) == expected


def test_link_result_without_reply_is_silent():
    assert qual._classify_link_result(PlainProbeResult("127.0.0.1", None, {})) == LINK_SILENT


# -------------------------------------------------------- encrypted probes

def test_dot_probe_qualifies_against_ip_san_certificate(tmp_path):
    server = TlsResponder(tmp_path, with_ip_san=True, mode="dot")
    try:
        result = probe_encrypted_candidate(
            loopback_candidate(server.port, TRANSPORT_DOT),
            timeout_seconds=1.5,
            ssl_context_factory=server.client_context,
        )
    finally:
        server.close()
    assert result.verdict == CANDIDATE_QUALIFIED
    assert result.latency_ms is not None


def test_doh_probe_qualifies_and_mismatch_is_reported(tmp_path):
    server = TlsResponder(tmp_path, with_ip_san=True, mode="doh")
    try:
        good = probe_encrypted_candidate(
            loopback_candidate(server.port, TRANSPORT_DOH),
            timeout_seconds=1.5,
            ssl_context_factory=server.client_context,
        )
        mismatch = probe_encrypted_candidate(
            loopback_candidate(server.port, TRANSPORT_DOH, control_answers=frozenset({"1.1.1.1"})),
            timeout_seconds=1.5,
            ssl_context_factory=server.client_context,
        )
    finally:
        server.close()
    assert good.verdict == CANDIDATE_QUALIFIED
    assert mismatch.verdict == CANDIDATE_ANSWER_MISMATCH


def test_certificate_without_ip_san_is_tls_failed(tmp_path):
    server = TlsResponder(tmp_path, with_ip_san=False, mode="dot")
    try:
        result = probe_encrypted_candidate(
            loopback_candidate(server.port, TRANSPORT_DOT),
            timeout_seconds=1.5,
            ssl_context_factory=server.client_context,
        )
    finally:
        server.close()
    assert result.verdict == CANDIDATE_TLS_FAILED


def test_closed_port_is_connect_failed_and_hanging_server_is_timeout(tmp_path):
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    closed_port = probe.getsockname()[1]
    probe.close()
    started = time.monotonic()
    result = probe_encrypted_candidate(loopback_candidate(closed_port, TRANSPORT_DOT), timeout_seconds=0.5)
    # Windows retries a refused loopback SYN for about a second, so a short
    # budget may surface the refusal as a timeout; both are "not qualified".
    assert result.verdict in {CANDIDATE_CONNECT_FAILED, CANDIDATE_TIMEOUT}
    assert time.monotonic() - started < 2.0

    server = TlsResponder(tmp_path, with_ip_san=True, mode="dot", hang=True)
    started = time.monotonic()
    try:
        result = probe_encrypted_candidate(
            loopback_candidate(server.port, TRANSPORT_DOT),
            timeout_seconds=0.4,
            ssl_context_factory=server.client_context,
        )
    finally:
        server.close()
    assert result.verdict == CANDIDATE_TIMEOUT
    assert time.monotonic() - started < 2.0


# ---------------------------------------------------------------- discovery

def adapters_fixture():
    return (
        DiscoveredAdapter(38, "{tun}", "Mihomo", True, False, ("198.18.0.2",), "", ("198.18.0.1",), 53),
        DiscoveredAdapter(1, "{lo}", "Loopback Pseudo-Interface 1", True, False, ("fec0:0:0:ffff::1",), "", ("127.0.0.1",), 24),
        DiscoveredAdapter(8, "{vbox}", "Ethernet 2", True, False, ("fec0:0:0:ffff::1", "fec0:0:0:ffff::2"), "", ("192.168.56.1",), 6),
        DiscoveredAdapter(15, "{wlan}", "WLAN", True, True, ("222.198.178.168", "222.198.178.68"), "campus.example.edu", ("172.21.6.66", "fe80::1"), 71),
        DiscoveredAdapter(20, "{vpn}", "Corp VPN", True, False, ("10.0.0.53", "127.0.0.1", "0.0.0.0"), "corp.example", ("10.8.0.2",), 131),
        DiscoveredAdapter(21, "{down}", "Down", False, True, ("203.0.113.53",), "down.example", ("203.0.113.2",), 6),
    )


def test_discovery_filters_tunnel_bogus_and_local_resolvers_and_keeps_intranet_adapters():
    discovery = discover_link_resolvers(adapter_source=adapters_fixture, excluded_addresses=("172.21.6.66",))
    assert discovery.error == ""
    assert discovery.resolvers == ("222.198.178.168", "222.198.178.68", "10.0.0.53")
    assert discovery.resolver_adapters["10.0.0.53"] == (20, False)
    assert discovery.suffixes == ("campus.example.edu", "corp.example")
    assert discovery.ipv4_available is True and discovery.ipv6_available is False
    assert len(discovery.fingerprint) == 32


def test_discovery_fingerprint_is_stable_and_changes_with_resolvers():
    first = discover_link_resolvers(adapter_source=adapters_fixture).fingerprint
    again = discover_link_resolvers(adapter_source=lambda: tuple(reversed(adapters_fixture()))).fingerprint
    assert first == again
    changed = list(adapters_fixture())
    changed[3] = DiscoveredAdapter(15, "{wlan}", "WLAN", True, True, ("1.1.1.1",), "campus.example.edu", ("172.21.6.66",), 71)
    assert discover_link_resolvers(adapter_source=lambda: tuple(changed)).fingerprint != first


def test_discovery_failure_is_advisory():
    def broken():
        raise OSError(5, "boom")

    discovery = discover_link_resolvers(adapter_source=broken)
    assert discovery.resolvers == () and discovery.fingerprint == "unknown" and "OSError" in discovery.error


def test_excluded_adapter_names_drop_own_tunnel_even_with_public_looking_resolvers():
    def adapters():
        return (
            DiscoveredAdapter(5, "{own}", "Tesseract", True, True, ("9.9.9.9",), "", ("10.10.0.1",), 53),
            DiscoveredAdapter(6, "{wlan}", "WLAN", True, True, ("192.168.1.1",), "", ("192.168.1.20",), 71),
        )

    discovery = discover_link_resolvers(adapter_source=adapters)
    assert discovery.resolvers == ("192.168.1.1",)


# ------------------------------------------------------------ orchestration

def fake_discovery(resolvers=("222.198.178.68",), suffixes=("campus.example.edu",), **kwargs):
    def factory(**_):
        return LinkDiscovery(
            resolvers=tuple(resolvers),
            resolver_adapters={address: (15, True) for address in resolvers},
            suffixes=tuple(suffixes),
            ipv4_available=kwargs.get("ipv4", True),
            ipv6_available=kwargs.get("ipv6", False),
            fingerprint="fp-test",
        )
    return factory


def fake_plain_probe(verdict_answers):
    def probe(resolvers, hostnames, *, budget_seconds):
        return {
            resolver: PlainProbeResult(resolver, 7, dict(verdict_answers))
            for resolver in resolvers
        }
    return probe


PUBLIC_ANSWERS = {"one.one.one.one": PlainAnswer(0, ("1.1.1.1",)), "dns.google": PlainAnswer(0, ("8.8.8.8",))}


def test_qualify_resolvers_assembles_verdicts_and_returns_early_on_two_qualified():
    def encrypted_probe(candidate, *, timeout_seconds):
        if candidate.provider_id in {"quad9", "cloudflare"} and candidate.transport == TRANSPORT_DOT:
            return EncryptedCandidateResult(candidate, CANDIDATE_QUALIFIED, 40)
        if candidate.provider_id == "google":
            time.sleep(5.0)
        return EncryptedCandidateResult(candidate, CANDIDATE_TLS_FAILED)

    started = time.monotonic()
    result = qualify_resolvers(
        budget_seconds=1.0,
        discovery=fake_discovery(),
        plain_probe=fake_plain_probe(PUBLIC_ANSWERS),
        encrypted_probe=encrypted_probe,
        outer_fake_ip_probe=lambda names: False,
    )
    assert time.monotonic() - started < 1.5
    assert result.fingerprint == "fp-test"
    assert result.inconclusive is False and result.outer_fake_ip is False
    assert [item.verdict for item in result.link_resolvers] == [LINK_ANSWERS_PUBLIC]
    verdicts = {item.candidate.url: item.verdict for item in result.candidates}
    assert verdicts["tls://9.9.9.9"] == CANDIDATE_QUALIFIED
    assert verdicts["tls://1.1.1.1"] == CANDIDATE_QUALIFIED
    assert verdicts["https://8.8.8.8/dns-query"] in {CANDIDATE_TIMEOUT, CANDIDATE_NOT_PROBED}
    assert result.dns_suffixes == ("campus.example.edu",)


def test_qualify_resolvers_never_exceeds_budget_when_everything_hangs():
    def hanging_probe(candidate, *, timeout_seconds):
        time.sleep(3.0)
        return EncryptedCandidateResult(candidate, CANDIDATE_QUALIFIED, 1)

    def hanging_plain(resolvers, hostnames, *, budget_seconds):
        time.sleep(3.0)
        return {}

    started = time.monotonic()
    result = qualify_resolvers(
        budget_seconds=0.5,
        discovery=fake_discovery(),
        plain_probe=hanging_plain,
        encrypted_probe=hanging_probe,
        outer_fake_ip_probe=lambda names: None,
    )
    assert time.monotonic() - started < 1.2
    assert result.inconclusive is True
    assert all(item.verdict in {CANDIDATE_TIMEOUT, CANDIDATE_NOT_PROBED} for item in result.candidates)
    assert all(item.verdict in {LINK_SILENT, "not_probed"} for item in result.link_resolvers)


def test_outer_fake_ip_marks_link_resolvers_but_not_encrypted_candidates():
    result = qualify_resolvers(
        budget_seconds=1.0,
        discovery=fake_discovery(),
        plain_probe=fake_plain_probe({"one.one.one.one": PlainAnswer(0, ("198.18.0.7",))}),
        encrypted_probe=lambda candidate, *, timeout_seconds: EncryptedCandidateResult(candidate, CANDIDATE_QUALIFIED, 30),
        outer_fake_ip_probe=lambda names: True,
    )
    assert result.outer_fake_ip is True
    assert [item.verdict for item in result.link_resolvers] == [LINK_OUTER_FAKE_IP]
    assert all(item.verdict == CANDIDATE_QUALIFIED for item in result.candidates)


def test_ipv6_only_link_probes_ipv6_candidates():
    seen = []

    def encrypted_probe(candidate, *, timeout_seconds):
        seen.append(candidate.family)
        return EncryptedCandidateResult(candidate, CANDIDATE_TLS_FAILED)

    result = qualify_resolvers(
        budget_seconds=0.5,
        discovery=fake_discovery(resolvers=(), ipv4=False, ipv6=True),
        plain_probe=fake_plain_probe({}),
        encrypted_probe=encrypted_probe,
        outer_fake_ip_probe=lambda names: None,
    )
    assert result.ipv6_only is True and set(seen) == {6}


def test_memory_hint_only_reorders_probes():
    order = []

    def encrypted_probe(candidate, *, timeout_seconds):
        order.append(candidate.url)
        return EncryptedCandidateResult(candidate, CANDIDATE_TLS_FAILED)

    qualify_resolvers(
        budget_seconds=0.5,
        hint=QualificationHint(encrypted_urls=("tls://9.9.9.9",)),
        discovery=fake_discovery(resolvers=()),
        plain_probe=fake_plain_probe({}),
        encrypted_probe=encrypted_probe,
        outer_fake_ip_probe=lambda names: None,
    )
    assert order[0] == "tls://9.9.9.9"


# ------------------------------------------------------------------ memory

def test_memory_records_positive_evidence_only_and_expires_hints(tmp_path):
    clock = {"now": 1_000_000.0}
    memory = ResolverMemory(tmp_path / "dns-resolver-memory.json", clock=lambda: clock["now"], max_entries=2)
    assert memory.hint("fp") is None
    assert memory.record("fp", rung="encrypted") is False  # nothing qualified
    assert memory.record("unknown", rung="encrypted", encrypted_urls=("tls://9.9.9.9",)) is False
    assert memory.record("fp", rung="encrypted", encrypted_urls=("tls://9.9.9.9",)) is True
    hint = memory.hint("fp")
    assert hint is not None and hint.encrypted_urls == ("tls://9.9.9.9",) and hint.rung == "encrypted"
    clock["now"] += memory.ordering_ttl_seconds + 1
    assert memory.hint("fp") is None
    memory.record("fp2", rung="system", link_resolvers=("10.0.0.53",))
    memory.record("fp3", rung="system", link_resolvers=("10.0.0.54",))
    assert set(memory.load()) == {"fp2", "fp3"}


def test_memory_tolerates_corrupt_file(tmp_path):
    path = tmp_path / "dns-resolver-memory.json"
    path.write_text("{not json", encoding="utf-8")
    memory = ResolverMemory(path)
    assert memory.load() == {} and memory.hint("fp") is None
    assert memory.record("fp", rung="encrypted", encrypted_urls=("tls://9.9.9.9",)) is True
    assert json.loads(path.read_text(encoding="utf-8"))["networks"]["fp"]["encrypted_urls"] == ["tls://9.9.9.9"]
