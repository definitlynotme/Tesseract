from __future__ import annotations

import json
import re
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import yaml

import diagnostic_bundle
from diagnostic_bundle import (
    BundleRedactor,
    DiagnosticBundleInputs,
    default_bundle_name,
    state_file_candidates,
    write_diagnostic_bundle,
)


MARKER = re.compile(r"\[redacted:[0-9a-f]{12}\]")

PRIVATE_SUBSCRIPTION = "https://sub.example-airport.net/api/v1/client/subscribe?token=Zx81kQ"
PUBLIC_LIST = "https://raw.githubusercontent.com/someone/free-nodes/main/v2ray.txt"
NTFY_TOPIC = "alice-phone-7f3k"
NTFY_TOKEN = "tk_live_31d9aa"
BOT_TOKEN = "123456789:" + "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawx"  # split: no whole token literal
CHAT_ID = "88442211"
CONTROLLER_SECRET = "c0ntroller-s3cret"
NODE_ADDRESS = "203.0.113.45"
NODE_UUID = "2f1c7d0e-5b8a-4c3d-9e21-7a6b5c4d3e2f"
SOCKS_URI = "socks5://bob:hunter22@203.0.113.46:1080"
EGRESS_PASSWORD = "egress-pa55"

CONFIG_LOCAL = {
    "alerts": {
        "channels": [
            {
                "id": "ntfy-main",
                "type": "ntfy",
                "enabled": True,
                "config": {"server": "https://ntfy.sh", "topic": NTFY_TOPIC, "token": NTFY_TOKEN},
            }
        ],
        "telegram": {"enabled": False, "bot_token": BOT_TOKEN, "chat_id": CHAT_ID},
    },
    "client": {"mihomo_controller": {"secret": CONTROLLER_SECRET}, "network_mode": "system_proxy"},
    "egress_proxy": {"enabled": False, "server": "127.0.0.1", "port": 7897, "password": EGRESS_PASSWORD},
    "subscription_server": {"enabled": True, "port": 8080, "token": "sub-server-tok"},
    "tiers": {
        "tier1": {
            "nodes": [
                {
                    "name": "home-vps",
                    "address": NODE_ADDRESS,
                    "port": 443,
                    "protocols": {
                        "vmess": {"uuid": NODE_UUID, "port": "443"},
                        "socks5": {"uri": SOCKS_URI},
                    },
                }
            ]
        }
    },
    "ui": {"language": "zh_CN", "theme": "daylight"},
}
SOURCES = {
    "subscription_urls": [
        {"url": PRIVATE_SUBSCRIPTION, "fetch_paths": [{"kind": "direct", "enabled": True}]},
        {"url": PUBLIC_LIST, "fetch_paths": [{"kind": "gh-proxy", "url": "https://gh-proxy.com/"}]},
    ],
    "telegram_channels": ["freenodes"],
}
MONITOR_LOG = "\n".join(
    [
        "2026-09-20 04:30:01,100 [INFO] desktop_runtime: Desktop runtime start requested (generation=4 start_core=True)",
        f"2026-09-20 04:30:02,100 [WARNING] collector: Source failed subscription_url:{PRIVATE_SUBSCRIPTION} (timeout)",
        f"2026-09-20 04:30:03,100 [WARNING] collector: Source failed subscription_url:{PUBLIC_LIST} (timeout)",
        f"2026-09-20 04:30:04,100 [ERROR] manager: Tier1 node home-vps ({NODE_ADDRESS}:443) down",
        f"2026-09-20 04:30:05,100 [INFO] alerts: ntfy delivered to https://ntfy.sh/{NTFY_TOPIC}",
        "2026-09-20 04:30:06,100 [INFO] mihomo: [TCP] dial DIRECT 127.0.0.1:60023 --> raw.githubusercontent.com:443 "
        "error: dial tcp 185.199.108.133:443: i/o timeout",
        "2026-09-20 04:30:07,100 [INFO] mihomo: Hot-reloaded config via http://127.0.0.1:9090/configs?force=true",
        r"2026-09-20 04:30:08,100 [ERROR] core_runner: cannot open C:\Users\alice\AppData\Local\Tesseract\mihomo.exe",
        f"2026-09-20 04:30:09,100 [WARNING] parser: rejected {SOCKS_URI} for node {NODE_UUID}",
    ]
) + "\n"
EVENTS = [
    {
        "event_key": "update.sources_failed",
        "level": "WARNING",
        "message_raw": f"Source refresh failed for {PRIVATE_SUBSCRIPTION}",
        "metadata": {"event_params": {"identity": f"vmess|{NODE_ADDRESS}:443|{NODE_UUID}", "count": 3}},
    },
    {"event_key": "mihomo.connection_timeout", "level": "INFO", "message_raw": "[TCP] dial DIRECT ..."},
]


class BundleFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.config_dir = root / "config"
        self.data_dir = root / "data"
        self.config_dir.mkdir()
        self.data_dir.mkdir()
        (self.config_dir / "config.local.yaml").write_text(
            yaml.safe_dump(CONFIG_LOCAL, allow_unicode=True), encoding="utf-8"
        )
        (self.config_dir / "sources.yaml").write_text(yaml.safe_dump(SOURCES), encoding="utf-8")
        # Bytes, so Windows text mode does not turn "\n" into "\r\n".
        (self.data_dir / "monitor.log").write_bytes(MONITOR_LOG.encode("utf-8"))
        (self.data_dir / "events.jsonl").write_bytes(
            ("\n".join(json.dumps(event) for event in EVENTS) + "\nnot json at all\n").encode("utf-8")
        )
        (self.data_dir / "dns-resolver-memory.json").write_text(
            json.dumps({"networks": {"a": {"link_resolvers": ["tls://223.5.5.5:853"]}}}),
            encoding="utf-8",
        )
        self.target = root / "out" / "tesseract-diagnostics-20260920-043000.zip"
        self.target.parent.mkdir()

    def inputs(self, **overrides) -> DiagnosticBundleInputs:
        values = {
            "log_file": self.data_dir / "monitor.log",
            "events_file": self.data_dir / "events.jsonl",
            "config_files": {
                "config.yaml": self.config_dir / "config.yaml",
                "config.local.yaml": self.config_dir / "config.local.yaml",
                "sources.yaml": self.config_dir / "sources.yaml",
            },
            "state_files": state_file_candidates(self.data_dir),
            "runtime_status": {"startup_phase": "failed", "last_error": f"egress via {NODE_ADDRESS} failed"},
            "key_events_text": f"2026-09-20 04:30:04.100 [ERROR] Vanguard node down — {NODE_ADDRESS}",
            "app": {"version": "0.1.0", "language": "zh_CN"},
            "home": r"C:\Users\alice",
        }
        values.update(overrides)
        return DiagnosticBundleInputs(**values)


def read_bundle(path: Path) -> dict[str, str]:
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name).decode("utf-8") for name in archive.namelist()}


class DiagnosticBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.fixture = BundleFixture(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def build(self, **overrides) -> tuple[diagnostic_bundle.DiagnosticBundleResult, dict[str, str]]:
        result = write_diagnostic_bundle(self.fixture.target, self.fixture.inputs(**overrides))
        return result, read_bundle(result.path)

    def test_settings_secrets_never_reach_any_file(self) -> None:
        result, files = self.build()
        blob = "\n".join(files.values())
        for secret in (
            PRIVATE_SUBSCRIPTION,
            "Zx81kQ",
            NTFY_TOPIC,
            NTFY_TOKEN,
            BOT_TOKEN,
            CHAT_ID,
            CONTROLLER_SECRET,
            NODE_ADDRESS,
            NODE_UUID,
            "hunter22",
            EGRESS_PASSWORD,
            "sub-server-tok",
            "alice",
        ):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, blob)
        self.assertGreater(result.markers, 0)

    def test_one_secret_keeps_one_marker_across_files(self) -> None:
        _result, files = self.build()
        folder = self.fixture.target.stem
        sources = yaml.safe_load(files[f"{folder}/config/sources.yaml"])
        marker = sources["subscription_urls"][0]["url"]
        self.assertRegex(marker, MARKER)
        log = files[f"{folder}/logs/monitor.log"]
        self.assertIn(f"subscription_url:{marker} (timeout)", log)
        events = files[f"{folder}/logs/events.jsonl"]
        self.assertIn(marker, events)
        local = yaml.safe_load(files[f"{folder}/config/config.local.yaml"])
        address_marker = local["tiers"]["tier1"]["nodes"][0]["address"]
        self.assertIn(f"home-vps ({address_marker}:443) down", log)
        self.assertIn(address_marker, files[f"{folder}/runtime-status.json"])
        self.assertIn(address_marker, files[f"{folder}/key-events.txt"])
        # Share links and UUIDs are hidden by the general rules, which give
        # the same marker as the settings do.
        protocols = local["tiers"]["tier1"]["nodes"][0]["protocols"]
        self.assertIn(
            f"rejected {protocols['socks5']['uri']} for node {protocols['vmess']['uuid']}",
            log,
        )

    def test_what_a_connectivity_problem_is_made_of_stays_readable(self) -> None:
        _result, files = self.build()
        folder = self.fixture.target.stem
        log = files[f"{folder}/logs/monitor.log"]
        self.assertIn(
            "[TCP] dial DIRECT 127.0.0.1:60023 --> raw.githubusercontent.com:443 "
            "error: dial tcp 185.199.108.133:443: i/o timeout",
            log,
        )
        # A public node list names the failing source and holds no secret.
        self.assertIn(f"subscription_url:{PUBLIC_LIST} (timeout)", log)
        self.assertIn(PUBLIC_LIST, files[f"{folder}/config/sources.yaml"])
        self.assertIn("https://gh-proxy.com/", files[f"{folder}/config/sources.yaml"])
        self.assertIn("http://127.0.0.1:9090/configs?", log)
        self.assertIn(r"%USERPROFILE%\AppData\Local\Tesseract\mihomo.exe", log)
        self.assertIn("tls://223.5.5.5:853", files[f"{folder}/state/dns-resolver-memory.json"])
        local = yaml.safe_load(files[f"{folder}/config/config.local.yaml"])
        # Loopback endpoints, ports, flags and the language are not private.
        self.assertEqual(local["egress_proxy"]["server"], "127.0.0.1")
        self.assertEqual(local["egress_proxy"]["port"], 7897)
        self.assertEqual(local["subscription_server"]["port"], 8080)
        self.assertIs(local["subscription_server"]["enabled"], True)
        self.assertRegex(local["subscription_server"]["token"], MARKER)
        self.assertEqual(local["ui"]["language"], "zh_CN")

    def test_structured_logs_stay_valid_json_and_hash_identities(self) -> None:
        _result, files = self.build()
        lines = files[f"{self.fixture.target.stem}/logs/events.jsonl"].splitlines()
        self.assertEqual(len(lines), 3)
        first = json.loads(lines[0])
        params = first["metadata"]["event_params"]
        self.assertNotIn("identity", params)
        self.assertTrue(params["identity_hash"].startswith("hmac:"))
        self.assertEqual(params["count"], 3)
        self.assertEqual(json.loads(lines[1])["event_key"], "mihomo.connection_timeout")
        # A line that is not JSON is still scrubbed as text, not dropped.
        self.assertEqual(lines[2], "not json at all")

    def test_manifest_lists_every_file_and_what_was_missing_or_too_large(self) -> None:
        big = self.fixture.data_dir / "tier3-source-state.json"
        big.write_text(json.dumps({"sources": ["x" * 500]}), encoding="utf-8")
        with mock.patch.object(diagnostic_bundle, "STATE_FILE_MAX_BYTES", 200):
            result, files = self.build()
        folder = self.fixture.target.stem
        manifest = json.loads(files[f"{folder}/manifest.json"])
        self.assertEqual(manifest["schema_version"], diagnostic_bundle.BUNDLE_SCHEMA_VERSION)
        listed = [entry["name"] for entry in manifest["files"]]
        self.assertEqual(sorted(listed), sorted(name for name in result.files if name != "manifest.json"))
        self.assertIn("config/config.yaml", manifest["missing"])
        self.assertIn("state/runtime-lifecycle.json", manifest["missing"])
        self.assertEqual(manifest["skipped_too_large"], ["state/tier3-source-state.json"])
        self.assertEqual(manifest["app"]["version"], "0.1.0")
        self.assertEqual(manifest["redaction"]["markers"], result.markers)
        self.assertTrue(manifest["redaction"]["policy"])

    def test_log_tail_starts_on_a_whole_line_and_says_it_was_cut(self) -> None:
        log = self.fixture.data_dir / "monitor.log"
        log.write_bytes("".join(f"line {index:03d} padding padding\n" for index in range(200)).encode("utf-8"))
        size = log.stat().st_size
        with mock.patch.object(diagnostic_bundle, "LOG_TAIL_BYTES", 300):
            _result, files = self.build()
        folder = self.fixture.target.stem
        text = files[f"{folder}/logs/monitor.log"]
        self.assertTrue(text.startswith("line "))
        self.assertTrue(text.endswith("line 199 padding padding\n"))
        self.assertLessEqual(len(text.encode("utf-8")), 300)
        entry = next(
            item
            for item in json.loads(files[f"{folder}/manifest.json"])["files"]
            if item["name"] == "logs/monitor.log"
        )
        self.assertEqual((entry["source_bytes"], entry["truncated"]), (size, True))
        # The budget went to the current file; the rotation is not read.
        self.assertNotIn(f"{folder}/logs/monitor.log.1", files)

    def test_rotated_log_fills_what_the_current_file_left(self) -> None:
        (self.fixture.data_dir / "monitor.log.1").write_bytes(
            "".join(f"older {index:03d}\n" for index in range(100)).encode("utf-8")
        )
        current = (self.fixture.data_dir / "monitor.log").stat().st_size
        with mock.patch.object(diagnostic_bundle, "LOG_TAIL_BYTES", current + 50):
            _result, files = self.build()
        folder = self.fixture.target.stem
        older = files[f"{folder}/logs/monitor.log.1"]
        self.assertTrue(older.startswith("older "))
        self.assertTrue(older.endswith("older 099\n"))
        self.assertLessEqual(len(older.encode("utf-8")), 50)

    def test_bundle_is_one_folder_and_is_written_atomically(self) -> None:
        result, files = self.build()
        folder = self.fixture.target.stem
        self.assertTrue(all(name.startswith(f"{folder}/") for name in files))
        self.assertEqual(result.size, self.fixture.target.stat().st_size)
        self.assertFalse(self.fixture.target.with_name(self.fixture.target.name + ".partial").exists())

        self.fixture.target.unlink()
        with mock.patch.object(zipfile.ZipFile, "writestr", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                write_diagnostic_bundle(self.fixture.target, self.fixture.inputs())
        self.assertFalse(self.fixture.target.exists())
        self.assertFalse(self.fixture.target.with_name(self.fixture.target.name + ".partial").exists())

    def test_unreadable_settings_are_scrubbed_as_text(self) -> None:
        broken = self.fixture.config_dir / "config.yaml"
        broken.write_text(f"subscription: {PRIVATE_SUBSCRIPTION}\n  bad: [indent\n", encoding="utf-8")
        _result, files = self.build()
        text = files[f"{self.fixture.target.stem}/config/config.yaml"]
        self.assertNotIn("Zx81kQ", text)
        self.assertIn("bad: [indent", text)

    def test_default_name_is_a_timestamped_zip(self) -> None:
        from datetime import datetime

        self.assertEqual(
            default_bundle_name(datetime(2026, 9, 20, 4, 30, 5)),
            "tesseract-diagnostics-20260920-043005.zip",
        )


class BundleRedactorTextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.redactor = BundleRedactor(home=r"C:\Users\alice")

    def assertRedactedTo(self, text: str, expected_shape: str) -> None:
        self.assertEqual(MARKER.sub("<R>", self.redactor.text(text)), expected_shape)

    def test_web_addresses_keep_scheme_and_host_only(self) -> None:
        self.assertRedactedTo(
            "https://sub.example.net/api/v1/client?token=abc",
            "https://sub.example.net/<R>?<R>",
        )
        self.assertRedactedTo("http://user:pw@proxy.example.net:8080/", "http://<R>@proxy.example.net:8080/")
        self.assertRedactedTo("see (https://example.net/x/y).", "see (https://example.net/<R>).")
        self.assertRedactedTo("https://dns.alidns.com/dns-query", "https://dns.alidns.com/dns-query")
        self.assertRedactedTo("tls://223.5.5.5:853", "tls://223.5.5.5:853")
        self.assertRedactedTo(PUBLIC_LIST, PUBLIC_LIST)
        self.assertRedactedTo("https://raw.githubusercontent.com/a/b/c.txt?token=x", "https://raw.githubusercontent.com/a/b/c.txt?<R>")

    def test_telegram_channel_pages_are_public_but_invites_are_not(self) -> None:
        self.assertRedactedTo("https://t.me/s/freenodes", "https://t.me/s/freenodes")
        self.assertRedactedTo("https://t.me/+AbCdEfGhIjK", "https://t.me/<R>")
        self.assertRedactedTo("https://t.me/joinchat/AbCdEfGh", "https://t.me/<R>")

    def test_share_links_and_proxy_urls_are_hidden_whole(self) -> None:
        self.assertRedactedTo("vmess://eyJ2IjoiMiIsImFkZCI6IjE5Mi4wLjIuNCJ9", "<R>")
        self.assertRedactedTo("node ss://YWVzLTI1Ni1nY206cGFzcw@198.51.100.4:443#hk ok", "node <R> ok")
        self.assertRedactedTo(SOCKS_URI, "<R>")
        self.assertRedactedTo("trojan:pass@host", "<R>")

    def test_uuids_mailboxes_and_bot_tokens_are_hidden(self) -> None:
        self.assertRedactedTo(f"id={NODE_UUID}", "id=<R>")
        self.assertRedactedTo("mail alice@example.com now", "mail <R> now")
        self.assertRedactedTo(f"bot {BOT_TOKEN}", "bot <R>")

    def test_node_labels_are_not_mailboxes(self) -> None:
        for label in ("俄罗斯@tgfree.example.com:443", "Unknown@node.example.com:8443"):
            with self.subTest(label=label):
                self.assertRedactedTo(label, label)

    def test_home_directory_is_replaced_with_either_separator(self) -> None:
        self.assertRedactedTo(r"C:\Users\alice\AppData\x", r"%USERPROFILE%\AppData\x")
        self.assertRedactedTo("c:/users/ALICE/Desktop", "%USERPROFILE%/Desktop")
        # Another user whose name starts with the current one; split for the release audit.
        other_home = "C:\\Users\\" + "alicebob\\x"
        self.assertRedactedTo(other_home, other_home)

    def test_sensitive_keys_mark_values_not_whole_sections(self) -> None:
        settings = {
            "subscription_server": {"enabled": True, "port": 8080, "token": "server-token-1"},
            "stats": {"pass": 12, "fast_pass": "3", "red_fail_then_other_pass": 4},
        }
        self.redactor.learn(settings)
        scrubbed = self.redactor.scrub(settings)
        self.assertEqual(scrubbed["subscription_server"]["port"], 8080)
        self.assertRegex(scrubbed["subscription_server"]["token"], MARKER)
        self.assertEqual(scrubbed["stats"], settings["stats"])
        self.assertEqual(self.redactor.text("listening on 8080"), "listening on 8080")
        self.assertRedactedTo("auth server-token-1 rejected", "auth <R> rejected")

    def test_learnt_literals_match_whole_words_only(self) -> None:
        self.redactor.learn({"alerts": {"telegram": {"chat_id": 4455}}, "egress_proxy": {"password": "abcd"}})
        self.assertRedactedTo("chat 4455 and 44556", "chat <R> and 44556")
        self.assertRedactedTo("abcd abcde", "<R> abcde")
        # Too short to be told apart from ordinary text: kept out of logs.
        self.redactor.learn({"egress_proxy": {"password": "abc"}})
        self.assertEqual(self.redactor.text("abc"), "abc")
        self.assertRegex(self.redactor.scrub({"password": "abc"})["password"], MARKER)


if __name__ == "__main__":
    unittest.main()
