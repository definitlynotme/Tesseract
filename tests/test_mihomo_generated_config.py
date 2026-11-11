import socket
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

import yaml

from clash_formatter import build_bootstrap_clash_config, build_full_clash_config, dump_clash_yaml
from dns_resolver_policy import (
    BOOTSTRAP_LADDER_AUTO,
    CANDIDATE_QUALIFIED,
    LINK_ANSWERS_INTERNAL_ONLY,
    LINK_ANSWERS_PUBLIC,
    RUNG_INTRANET_ONLY,
    RUNG_SYSTEM,
    EncryptedCandidateResult,
    LinkResolverResult,
    ResolverQualification,
    apply_dns_block_to_payload,
    choose_rung,
    encrypted_candidates,
    render_dns_block,
)
from mihomo_api import MihomoAPIClient, MihomoAPIError
from parser import parse_node_uri
from windows_subprocess import hidden_subprocess_kwargs


class MihomoGeneratedConfigTests(unittest.TestCase):
    @staticmethod
    def _reserve_loopback_port(used: set[int]) -> int:
        while True:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                sock.bind(("127.0.0.1", 0))
                port = int(sock.getsockname()[1])
            finally:
                sock.close()
            if port not in used:
                used.add(port)
                return port

    def _assert_mihomo_accepts(self, config_yaml: str) -> None:
        binary = Path("bin/mihomo.exe")
        if not binary.exists():
            self.skipTest("bin/mihomo.exe is not available")
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            config_path = temp_path / "mihomo-runtime.yaml"
            config_path.write_text(config_yaml, encoding="utf-8")
            result = subprocess.run(
                [str(binary.resolve()), "-t", "-f", str(config_path), "-d", str(temp_path)],
                cwd=str(Path.cwd()),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                **hidden_subprocess_kwargs(),
            )

        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)

    def test_generated_config_passes_mihomo_parser_when_binary_exists(self) -> None:
        node = parse_node_uri(
            "vless://11111111-1111-1111-1111-111111111111@example.com:443"
            "?encryption=none&security=tls&type=tcp&sni=example.com#ParserSmoke",
            source="test",
            tier="tier1",
        )
        assert node is not None
        config_yaml = build_full_clash_config(
            {"tier1": [node], "tier2": [], "tier3": [], "exit": []},
            external_controller=None,
            routing_config={
                "rules": {
                    "direct": ["example.cn"],
                    "reject": ["geosite:category-ads-all"],
                    "ai_fixed_ip": ["openai.com"],
                    "raw": ["DOMAIN,legacy-ai.example,AI-FIXED-IP"],
                    "rule_sets": ["RULE-SET,legacy-ai,AI-FIXED-IP"],
                    "compatibility_defaults": [
                        {"rule_type": "ai_fixed_ip", "value": "chatgpt.com"},
                    ],
                }
            },
        )
        self.assertNotIn("AI-FIXED-IP", config_yaml)
        # Parser validation must be deterministic and must not silently turn
        # into a network/download test.  Keep the production formatter output,
        # but remove only rules whose parser initialization requires external
        # GeoSite/GeoIP databases.  Their rendering is covered separately by
        # the formatter unit tests.
        config_yaml = "\n".join(
            line
            for line in config_yaml.splitlines()
            if not line.startswith("- GEOSITE,")
            and not line.startswith("- GEOIP,")
        ) + "\n"
        self._assert_mihomo_accepts(config_yaml)

    def test_resolver_rung_blocks_pass_packaged_mihomo_parser(self) -> None:
        """Every rung the resolver ladder can render must load in the real Core."""

        formal_text = build_full_clash_config(
            {"tier1": [], "tier2": [], "tier3": [], "exit": []},
            external_controller=None,
            dns_config={"enable": True},
        )
        formal = yaml.safe_load(formal_text)
        by_url = {candidate.url: candidate for candidate in encrypted_candidates(include_ipv6=True)}
        links = (
            LinkResolverResult("222.198.178.68", LINK_ANSWERS_PUBLIC, 5),
            LinkResolverResult("2001:db8::53", LINK_ANSWERS_PUBLIC, 9),
        )

        def qualification(**overrides) -> ResolverQualification:
            base = dict(
                fingerprint="fp", wall_time_ms=1, link_resolvers=links,
                dns_suffixes=("campus.example.edu",), candidates=(),
                ipv4_available=True, ipv6_available=False, ipv6_only=False,
                outer_fake_ip=False, captive_suspected=False, inconclusive=False,
            )
            base.update(overrides)
            return ResolverQualification(**base)

        cases = {
            "encrypted_mixed_transports": qualification(candidates=(
                EncryptedCandidateResult(by_url["tls://9.9.9.9"], CANDIDATE_QUALIFIED, 20),
                EncryptedCandidateResult(by_url["https://1.1.1.1/dns-query"], CANDIDATE_QUALIFIED, 30),
                EncryptedCandidateResult(by_url["https://223.5.5.5/dns-query"], CANDIDATE_QUALIFIED, 40),
            )),
            "encrypted_full_pool_ipv4": qualification(inconclusive=True, link_resolvers=()),
            "encrypted_full_pool_ipv6": qualification(
                inconclusive=True, link_resolvers=(), ipv4_available=False,
                ipv6_available=True, ipv6_only=True,
            ),
            "encrypted_ipv6_literal": qualification(candidates=(
                EncryptedCandidateResult(by_url["tls://[2620:fe::fe]"], CANDIDATE_QUALIFIED, 20),
                EncryptedCandidateResult(by_url["https://[2606:4700:4700::1111]/dns-query"], CANDIDATE_QUALIFIED, 25),
            )),
        }
        for label, evidence in cases.items():
            with self.subTest(rung=label):
                decision = choose_rung(evidence, ladder=BOOTSTRAP_LADDER_AUTO)
                block = render_dns_block(formal["dns"], decision, evidence)
                self._assert_mihomo_accepts(dump_clash_yaml(apply_dns_block_to_payload(formal, block)))

        system_evidence = qualification(candidates=())
        for rung in (RUNG_SYSTEM, RUNG_INTRANET_ONLY):
            with self.subTest(rung=rung):
                decision = choose_rung(
                    system_evidence, ladder=BOOTSTRAP_LADDER_AUTO,
                    position=BOOTSTRAP_LADDER_AUTO.index(rung),
                )
                self.assertEqual(decision.rung, rung)
                block = render_dns_block(formal["dns"], decision, system_evidence)
                self._assert_mihomo_accepts(dump_clash_yaml(apply_dns_block_to_payload(formal, block)))

        with self.subTest(rung="system_bare"):
            bare = qualification(candidates=(), link_resolvers=(), outer_fake_ip=True)
            decision = choose_rung(bare, ladder=BOOTSTRAP_LADDER_AUTO, position=1)
            block = render_dns_block(formal["dns"], decision, bare)
            self.assertEqual(block["nameserver"], ["system"])
            self._assert_mihomo_accepts(dump_clash_yaml(apply_dns_block_to_payload(formal, block)))

        with self.subTest(rung="intranet_only_internal_link"):
            internal = qualification(
                candidates=(),
                link_resolvers=(LinkResolverResult("10.0.0.53", LINK_ANSWERS_INTERNAL_ONLY, 4),),
            )
            decision = choose_rung(internal, ladder=BOOTSTRAP_LADDER_AUTO)
            self.assertEqual(decision.rung, RUNG_INTRANET_ONLY)
            block = render_dns_block(formal["dns"], decision, internal)
            self._assert_mihomo_accepts(dump_clash_yaml(apply_dns_block_to_payload(formal, block)))

    def test_no_node_fail_closed_config_passes_packaged_mihomo_parser(self) -> None:
        config_yaml = build_full_clash_config(
            {"tier1": [], "tier2": [], "tier3": [], "exit": []},
            external_controller=None,
            dns_config={
                "enable": True,
                "nameserver": ["https://1.1.1.1/dns-query"],
                "default_nameserver": ["1.1.1.1"],
                "proxy_server_nameserver": ["1.1.1.1"],
            },
        )

        self._assert_mihomo_accepts(config_yaml)

    def test_isolated_bootstrap_config_passes_packaged_mihomo_parser(self) -> None:
        formal_config = build_full_clash_config(
            {"tier1": [], "tier2": [], "tier3": [], "exit": []},
            external_controller="127.0.0.1:19090",
            network_mode="mixed",
            routing_config={
                "rules": {
                    "compatibility_defaults": [
                        {"rule_type": "direct", "value": "suffix:github.com"},
                        {
                            "rule_type": "direct",
                            "value": "suffix:githubusercontent.com",
                        },
                        {"rule_type": "direct", "value": "suffix:t.me"},
                        {"rule_type": "direct", "value": "suffix:telegram.me"},
                    ]
                }
            },
        )
        bootstrap_config = build_bootstrap_clash_config(
            formal_config,
            egress_proxy={
                "enabled": True,
                "scheme": "socks5",
                "server": "127.0.0.1",
                "port": 18080,
                "use_for": {"collection": True},
            },
        )

        self.assertNotIn("tun:", bootstrap_config)
        self.assertIn("BOOTSTRAP-EGRESS-PROXY", bootstrap_config)
        self._assert_mihomo_accepts(bootstrap_config)

    def test_real_core_never_selects_direct_compatible_during_fallback_reload(
        self,
    ) -> None:
        binary = Path("bin/mihomo.exe").resolve()
        if not binary.exists():
            self.skipTest("bin/mihomo.exe is not available")

        used_ports: set[int] = set()
        mixed_port = self._reserve_loopback_port(used_ports)
        controller_port = self._reserve_loopback_port(used_ports)
        local_probe_port = self._reserve_loopback_port(used_ports)
        unsafe = {"DIRECT", "COMPATIBLE"}

        def runtime_config(candidate: str) -> dict[str, object]:
            return {
                "mixed-port": mixed_port,
                "external-controller": f"127.0.0.1:{controller_port}",
                "allow-lan": False,
                "mode": "rule",
                "log-level": "silent",
                "ipv6": False,
                "proxies": [],
                "proxy-groups": [
                    {
                        "name": "PROXY-FALLBACK",
                        "type": "fallback",
                        "proxies": [candidate],
                        "url": (
                            f"http://127.0.0.1:{local_probe_port}/generate_204"
                        ),
                        "interval": 30,
                        "lazy": False,
                        "empty-fallback": "REJECT",
                    },
                    {
                        "name": "FINAL-EXIT",
                        "type": "select",
                        "proxies": ["PROXY-FALLBACK"],
                    },
                ],
                "rules": ["MATCH,FINAL-EXIT"],
            }

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            initial_path = temp_path / "initial.yaml"
            reload_path = temp_path / "reload.yaml"
            initial_path.write_text(
                yaml.safe_dump(runtime_config("REJECT"), sort_keys=False),
                encoding="utf-8",
            )
            reload_path.write_text(
                yaml.safe_dump(runtime_config("REJECT-DROP"), sort_keys=False),
                encoding="utf-8",
            )
            log_path = temp_path / "mihomo-test.log"
            process: subprocess.Popen | None = None
            reload_errors: list[BaseException] = []
            observed_now: list[str] = []
            observed_candidates: list[tuple[str, ...]] = []
            controller_errors = 0

            with log_path.open("w+", encoding="utf-8") as log_file:
                try:
                    process = subprocess.Popen(
                        [
                            str(binary),
                            "-f",
                            str(initial_path),
                            "-d",
                            str(temp_path),
                        ],
                        cwd=str(Path.cwd()),
                        stdin=subprocess.DEVNULL,
                        stdout=log_file,
                        stderr=subprocess.STDOUT,
                        **hidden_subprocess_kwargs(),
                    )
                    local_opener = build_opener(ProxyHandler({}))
                    client = MihomoAPIClient(
                        base_url=f"http://127.0.0.1:{controller_port}",
                        timeout=0.25,
                        opener=local_opener.open,
                    )

                    ready_deadline = time.monotonic() + 8.0
                    last_ready_error = "controller not ready"
                    while time.monotonic() < ready_deadline:
                        if process.poll() is not None:
                            break
                        try:
                            client.version()
                            break
                        except MihomoAPIError as exc:
                            last_ready_error = str(exc)
                            time.sleep(0.01)
                    else:
                        self.fail(last_ready_error)
                    if process.poll() is not None:
                        log_file.flush()
                        log_file.seek(0)
                        self.fail(
                            "Mihomo exited before controller readiness: "
                            + log_file.read()
                        )

                    def sample_once() -> None:
                        nonlocal controller_errors
                        try:
                            payload = client.proxies()
                        except MihomoAPIError:
                            controller_errors += 1
                            return
                        proxies = (
                            payload.get("proxies")
                            if isinstance(payload, dict)
                            else None
                        )
                        group = (
                            proxies.get("PROXY-FALLBACK")
                            if isinstance(proxies, dict)
                            else None
                        )
                        if not isinstance(group, dict):
                            return
                        observed_now.append(str(group.get("now") or ""))
                        candidates = group.get("all")
                        if isinstance(candidates, list):
                            observed_candidates.append(
                                tuple(str(item) for item in candidates)
                            )

                    startup_deadline = time.monotonic() + 0.75
                    while time.monotonic() < startup_deadline:
                        sample_once()
                        time.sleep(0.002)

                    def reload_config() -> None:
                        try:
                            client.reload_config(str(reload_path), force=True)
                        except BaseException as exc:  # surfaced in the test thread
                            reload_errors.append(exc)

                    reload_thread = threading.Thread(
                        target=reload_config,
                        name="mihomo-real-reload-test",
                    )
                    reload_thread.start()
                    post_reload_deadline: float | None = None
                    while True:
                        sample_once()
                        if not reload_thread.is_alive():
                            if post_reload_deadline is None:
                                post_reload_deadline = time.monotonic() + 0.75
                            elif time.monotonic() >= post_reload_deadline:
                                break
                        time.sleep(0.002)
                    reload_thread.join(timeout=2.0)

                    self.assertFalse(reload_thread.is_alive())
                    self.assertEqual(reload_errors, [])
                    self.assertGreater(len(observed_now), 20)
                    self.assertTrue(
                        all(value.upper() not in unsafe for value in observed_now),
                        observed_now,
                    )
                    self.assertIn(("REJECT",), observed_candidates)
                    self.assertIn(("REJECT-DROP",), observed_candidates)
                    self.assertLess(controller_errors, 10)
                finally:
                    if process is not None and process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=5.0)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5.0)


if __name__ == "__main__":
    unittest.main()
