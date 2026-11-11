from types import SimpleNamespace

import pytest
import yaml

import app_paths
import dns_resolver_policy as dns
from tier3_defaults import tier3_process_worker_ceiling
from tier3_expansion_actor import Tier3ExpansionActor
from tier3_sources import RemoteTextSource, build_source
from tier3_collection_settings import tier3_collection_settings
from tier3_validator_runtime import ValidatorHelperRuntime
from parser import parse_content_blob
from core_runner import MihomoCoreRunner


@pytest.mark.parametrize("cpus", [1, 8, 16, 24, 64, 256])
def test_helper_ceiling_does_not_change_with_cpu_count(cpus):
    assert tier3_process_worker_ceiling(cpus) == 16


@pytest.mark.parametrize("origin", ["cold_unknown", "fresh_replay", "historical_reprobe"])
def test_replay_keeps_the_same_three_second_fast_contract(origin):
    actor = SimpleNamespace(_fast_soft_entitlement_seconds=3.0)
    envelope = SimpleNamespace(dispatch_evidence_origin=origin)
    assert Tier3ExpansionActor._fast_probe_budget_seconds(actor, envelope) == 3.0


def test_helper_does_not_silently_add_system_resolver():
    before = dns.active_dns_block()
    block = {key: ["https://1.1.1.1/dns-query"] for key in (
        "nameserver", "default-nameserver", "proxy-server-nameserver"
    )}
    try:
        dns.publish_active_dns_block(block)
        helper = dns.helper_dns_block()
        for key, values in block.items():
            assert helper[key] == values
    finally:
        dns.publish_active_dns_block(before)


@pytest.mark.parametrize("local_empty", [False, True])
def test_region_default_migration_respects_explicit_empty(tmp_path, local_empty):
    base = tmp_path / "config.yaml"
    local = tmp_path / "config.local.yaml"
    base.write_text(yaml.safe_dump({"routing": {"rules": {"direct":
        list(app_paths.REGION_DIRECT_DEFAULT_RULES) if local_empty else []}}}), encoding="utf-8")
    local.write_text(yaml.safe_dump({"routing": {"rules": {"direct": []}}} if local_empty else {}), encoding="utf-8")
    app_paths._migrate_routing_region_direct_defaults(base, local)
    payload = yaml.safe_load((local if local_empty else base).read_text(encoding="utf-8"))
    assert payload["routing"]["rules"]["direct"] == []


def test_missing_delivery_config_and_source_empty_override_are_authoritative():
    global_delivery = {"api": True, "proxies": ["https://relay.example/"], "mirrors": []}
    settings = tier3_collection_settings({"github_delivery": global_delivery}, {}, default_github_candidate_paths=[])
    assert settings["github_delivery"] == {}
    source = build_source({"value": "https://raw.githubusercontent.com/a/b/main/sub", "github_delivery": {
        "api": False, "proxies": [], "mirrors": [],
    }}, github_delivery=global_delivery)
    assert source.mirror_urls() == []


@pytest.mark.parametrize("private", [
    {"headers": {"Authorization": "Bearer synthetic-secret"}},
    {"headers": {"Cookie": "synthetic-cookie"}},
    {"headers": {"X-Api-Key": "synthetic-key"}},
    {"query": "?token=synthetic-signature"},
])
def test_authenticated_source_cannot_be_rewritten(private):
    source = RemoteTextSource(
        "https://raw.githubusercontent.com/a/b/main/sub" + private.get("query", ""),
        headers=private.get("headers", {}),
        github_delivery={"api": True, "proxies": ["https://relay.example/"]},
    )
    assert source.delivery_paths() == []


def test_explicit_delivery_migration_backup_idempotence_and_deletion(tmp_path):
    path = tmp_path / "sources.yaml"
    original = {"subscription_urls": [{"url": "https://source.example/sub", "headers": {"X-Key": "kept"}},
                                      'https://raw.githubusercontent.com/a/b/main/sub'],
                "github_delivery": {"proxies": ["https://relay.example/"], "mirrors": []}}
    path.write_text(yaml.safe_dump(original), encoding="utf-8")
    paths = SimpleNamespace(sources_path=path, config_backup_dir=tmp_path / "backups")
    app_paths._migrate_explicit_source_delivery(paths)
    backups = list(paths.config_backup_dir.iterdir())
    assert len(backups) == 1
    assert yaml.safe_load(backups[0].read_text(encoding="utf-8")) == original
    migrated = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert migrated["subscription_urls"][0] == original["subscription_urls"][0]
    assert 'github_delivery' not in migrated
    assert migrated['subscription_urls'][1]['fetch_paths'] == [{
        "kind": 'github_proxy', "url": "https://relay.example/", "enabled": True, "delay_seconds": 0.0,
    }]
    app_paths._migrate_explicit_source_delivery(paths)
    assert len(list(paths.config_backup_dir.iterdir())) == 1
    del migrated['subscription_urls'][1]['fetch_paths']
    path.write_text(yaml.safe_dump(migrated), encoding="utf-8")
    app_paths._migrate_explicit_source_delivery(paths)
    assert "github_delivery" not in yaml.safe_load(path.read_text(encoding="utf-8"))
    assert 'fetch_paths' not in yaml.safe_load(path.read_text(encoding='utf-8'))['subscription_urls'][1]


def test_invalid_global_delivery_is_not_silently_erased_during_migration(tmp_path):
    path = tmp_path / 'sources.yaml'
    original = yaml.safe_dump({'github_delivery': 'invalid', 'subscription_urls': [
        {'url': 'https://raw.githubusercontent.com/a/b/main/sub',
         'github_delivery': {'proxies': ['https://relay.example/']}}]})
    path.write_text(original, encoding='utf-8')
    paths = SimpleNamespace(sources_path=path, config_backup_dir=tmp_path / 'backups')
    app_paths._migrate_explicit_source_delivery(paths)
    assert path.read_text(encoding='utf-8') == original
    assert not paths.config_backup_dir.exists()


def test_spawned_runtime_keeps_parent_dns_for_candidate_reload(tmp_path):
    dns_block = {key: ["https://1.1.1.1/dns-query"] for key in (
        "nameserver", "default-nameserver", "proxy-server-nameserver",
    )}
    payload = {"worker_id": 1, "worker_generation": 1, "binary_path": "fixture",
               "mixed_port": 17891, "controller_port": 19091,
               "prewarm_config": {"dns": dns_block}}
    runtime = ValidatorHelperRuntime(None, payload, prewarmed=SimpleNamespace(work_dir=tmp_path, process=None))
    before = dns.active_dns_block()
    try:
        # A spawned interpreter has no parent module globals.
        dns.publish_active_dns_block(None)
        assert runtime._base_config()["dns"] == dns_block
        node = parse_content_blob("vless://12345678-1234-1234-1234-123456789012@127.0.0.1:443#fixture", "fixture", "tier3")[0]
        assert runtime._candidate_config(node)["dns"] == dns_block
        dns_block["nameserver"].append("system")
        assert "system" not in runtime._base_config()["dns"]["nameserver"]
    finally:
        dns.publish_active_dns_block(before)


def test_version_lookup_is_reused_by_binary_identity_and_invalidated(tmp_path):
    binary = tmp_path / "mihomo.exe"
    binary.write_bytes(b"first-fixture")
    calls = []

    def query(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout=f"fixture-{len(calls)}", stderr="")

    def runner():
        return MihomoCoreRunner(binary_path=str(binary), runtime_config_path="unused",
                                work_dir=str(tmp_path), command_runner=query)

    first, second = runner(), runner()
    assert first.get_version() == second.get_version() == "fixture-1"
    assert len(calls) == 1
    binary.write_bytes(b"changed-fixture-binary")
    assert second.get_version() == "fixture-2"
    assert first.version_binary_sha256 != second.version_binary_sha256


def test_requested_minimum_cannot_escape_process_hard_cap():
    from tier3_validator_supervisor import Tier3ValidatorSupervisor
    supervisor = Tier3ValidatorSupervisor(
        binary_path='fixture', probe_urls=['http://fixture/204'], expected_statuses={204},
        probe_timeout_seconds=5, slow_threshold_ms=3000, min_workers=24, max_workers=256,
        capacity_sink=lambda _value: None, attempt_sink=lambda _value: None,
    )
    assert supervisor.min_workers == supervisor.max_workers == 16
