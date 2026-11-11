"""T-P2-15: catalogs (import.specs, alerts.catalog) and import.preview/apply.

Everything runs against a temporary ConfigStore and a fake runtime: no live
profile, no Mihomo, no network, no registry.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

import pytest

import bridge_catalog as catalog
import bridge_commands as commands
import bridge_import as bimport
import bridge_protocol as protocol
from bridge_jobs import JobEventRecorder, JobExecutor, JobFailure
from config_store import ConfigStore
from import_service import ImportService


SECRET_PASSWORD = "Zq9-fixture-secret-Pw"
SECRET_UUID = "5d2f1c3e-7a41-4b8e-9c60-0f1e2d3c4b5a"
VLESS_A = (f"vless://{SECRET_UUID}@a.example.test:443?encryption=none&security=reality"
           "&type=tcp&pbk=pubkeyA&sid=abcd&sni=www.example.test#Alpha")
TROJAN_B = f"trojan://{SECRET_PASSWORD}@b.example.test:443?security=tls&sni=b.example.test#Bravo"
BROKEN = "vless://not-a-valid-node"
SUB_URL = "https://sub.example.test/api/v1/client/subscribe?token=Zq9-sub-token-77"


# --- fakes --------------------------------------------------------------------

class Context:
    def __init__(self, op="op-1"):
        self.op = op


class FakeRuntime:
    """Mirrors desktop_runtime's public import entry points over a temp store."""

    def __init__(self, tmp_path: Path, *, core_running=False, refresh=None):
        local = tmp_path / "config.local.yaml"
        sources = tmp_path / "sources.yaml"
        local.write_text("tiers:\n  tier1:\n    nodes: []\n  tier2:\n    nodes: []\n    subscription_urls: []\n"
                         "  tier3:\n    enabled: false\n    nodes: []\n    subscription_urls: []\n"
                         "  exit:\n    nodes: []\n    subscription_urls: []\n", encoding="utf-8")
        sources.write_text("github_repos: []\ntelegram_channels: []\nsubscription_urls: []\nlocal_files: []\n",  # sources-marker: intentional
                           encoding="utf-8")
        self.store = ConfigStore(local_config_path=local, sources_path=sources, backup_dir=tmp_path / "backups")
        self.import_service = ImportService(self.store)
        self.core_running = core_running
        self.refresh = refresh or (lambda url: {"total": 3, "healthy": 2, "stored": True})
        self.calls: list[tuple[str, dict]] = []
        self.reloads: list[str] = []
        self.rev = 7
        self.core_reloads: list[dict] = []
        self.reload_receipt = {"reloaded": True, "running": True}
        self.log_hook = None        # (name, payload) -> None, runs inside the entry
        self.fail_before_write = None
        self.fail_after_write = None

    def bridge_config_revision(self):
        return self.rev

    def core_fact(self):
        return {"running": self.core_running, "pid": None, "process_generation": 0,
                "cleanup_pending": False, "core_generation": 1}

    def read_local_config(self):
        return self.store.read_local()

    def reload_core_config(self, *, mode="strict", reason=None, user_commit_id=""):
        self.core_reloads.append({"mode": mode, "reason": reason, "user_commit_id": user_commit_id})
        if isinstance(self.reload_receipt, Exception):
            raise self.reload_receipt
        return dict(self.reload_receipt)

    def _reload_after(self, name, payload, action, tier):
        # Same order as DesktopRuntime._reload_after: write, then reload.
        self.calls.append((name, dict(payload)))
        if self.log_hook is not None:
            self.log_hook(name, payload)
        if self.fail_before_write is not None:
            raise self.fail_before_write
        result = action()
        if self.fail_after_write is not None:
            raise self.fail_after_write
        self.reloads.append(tier)
        return result

    def import_tier1_nodes(self, payload):
        return self._reload_after("tier1_nodes", payload, lambda: self.import_service.import_tier1(payload), "tier1")

    def import_tier1_multiprotocol(self, payload):
        return self._reload_after("tier1_multi", payload,
                                  lambda: self.import_service.import_tier1_multiprotocol(payload), "tier1")

    def import_tier2_node(self, payload):
        return self._reload_after("tier2_node", payload, lambda: self.import_service.import_tier2_node(payload), "tier2")

    def import_tier3_node(self, payload):
        return self._reload_after("tier3_node", payload, lambda: self.import_service.import_tier3_node(payload), "tier3")

    def import_exit_nodes(self, payload):
        return self._reload_after("exit_nodes", payload, lambda: self.import_service.import_exit_nodes(payload), "exit")

    def _subscription(self, name, tier, payload, action):
        result = self._reload_after(name, payload, action, tier)
        try:
            refresh = self.refresh(payload["url"])
        except Exception as exc:  # same shape as _import_subscription_with_initial_refresh
            return {**result, "refresh_error": str(exc)}
        if not refresh.get("stored"):
            return {**result, "refresh": refresh, "refresh_error": str(refresh.get("error") or "failed")}
        return {**result, "refresh": refresh}

    def import_tier2_subscription(self, payload):
        return self._subscription("tier2_sub", "tier2", payload,
                                  lambda: self.import_service.import_tier2_subscription(payload))

    def import_tier3_subscription(self, payload):
        return self._subscription("tier3_sub", "tier3", payload,
                                  lambda: self.import_service.import_tier3_subscription(payload))

    def import_exit_subscription(self, payload):
        return self._subscription("exit_sub", "exit", payload,
                                  lambda: self.import_service.import_exit_subscription(payload))


class RecordingSecrets:
    def __init__(self, runtime):
        self.runtime = runtime
        self.events = []

    def register_draft(self, values):
        self.events.append(("register", dict(values), len(self.runtime.calls)))

    def restore(self):
        self.events.append(("restore", None, len(self.runtime.calls)))


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def make(tmp_path, *, owner="window-1", clock=None, **runtime_options):
    runtime = FakeRuntime(tmp_path, **runtime_options)
    secrets = RecordingSecrets(runtime)
    plans = bimport.PlanStore(monotonic=clock or Clock())
    handler = bimport.ImportCommands(bimport.RuntimeImportAdapter(lambda: runtime),
                                     owner_of=lambda context: owner, secrets_registry=secrets, plans=plans)
    return runtime, handler, secrets


def local_nodes(runtime, tier):
    return runtime.store.read_local()["tiers"][tier].get("nodes", [])


def failed(exc_info):
    exc = exc_info.value
    assert isinstance(exc, JobFailure)
    return exc.code, exc.partial_result


def assert_job_result(name, raw, *, done):
    """The executor adapter accepts the facts, and done/failed is as claimed."""
    success, result, _code = commands.adapt_result(name, raw)
    assert result is not None, "result must satisfy the closed schema"
    assert success is done
    frame = dict(t="evt", epoch="t", produced_at_ms=0, sent_at_ms=0, topic="jobs", rev=1, kind="edge",
                 seq=1, data=dict(op="op", req_id="r", owner="o", name=name, state="done" if done else "failed",
                                  started_at_ms=0, result=result,
                                  **({} if done else {"error": {"code": "conflict", "message_key": "errors.conflict",
                                                                "params": {}}})))
    assert protocol.validate_frame(frame) == []


def assert_preview_data(data):
    frame = dict(t="res", epoch="t", produced_at_ms=0, sent_at_ms=0, id=1, req_id="r", owner="o",
                 cmd="import.preview", ok=True, data=data)
    assert protocol.validate_frame(frame) == []


# --- import.specs ---------------------------------------------------------------

IMPORT_SNAPSHOT = {
    'vless_reality': 'tier:choice! name:string! server:string! port:integer! uuid:secret! network:choice! encryption:string! security:choice! flow:string* public_key:string short_id:string sni:string fingerprint:string* alpn:string* host:string path:string grpc_service_name:string',
    'vmess': 'tier:choice! name:string! server:string! port:integer! uuid:secret! alter_id:integer* scy:string* network:choice! security:choice! sni:string host:string path:string fingerprint:string* alpn:string* grpc_service_name:string',
    'trojan': 'tier:choice! name:string! server:string! port:integer! password:secret! network:choice! sni:string host:string path:string fingerprint:string* alpn:string* skip_cert_verify:boolean*',
    'ss': 'tier:choice! name:string! server:string! port:integer! password:secret method:choice!',
    'anytls': 'tier:choice! name:string! server:string! port:integer! password:secret! sni:string alpn:string* fingerprint:string* skip_cert_verify:boolean*',
    'naive': 'tier:choice! name:string! server:string! port:integer! username:secret! password:secret! security:choice! sni:string',
    'socks5': 'tier:choice! name:string! server:string! port:integer! username:secret password:secret',
    'hysteria2': 'tier:choice! name:string! server:string! port:integer! password:secret! sni:string obfs:string* obfs_password:secret* alpn:string* skip_cert_verify:boolean*',
    'tuic': 'tier:choice! name:string! server:string! port:integer! uuid:secret! password:secret! sni:string alpn:string* udp_relay_mode:choice* congestion_control:choice* skip_cert_verify:boolean*',
    'wireguard': 'tier:choice! name:string! server:string! port:integer! private_key:secret! public_key:string! local_address:string! allowed_ips:string* reserved:string* mtu:integer*',
    'argo_xhttp': 'tier:choice! name:string! server:string! port:integer! uuid:secret! security:choice! sni:string host:string! path:string! fingerprint:string*',
}

ALERT_SNAPSHOT = {
    'desktop': '',
    'sound': 'frequency:integer duration_ms:integer',
    'telegram': 'bot_token:secret! chat_id:string! parse_mode:string* message_thread_id:integer* silent:boolean* protect_content:boolean* disable_link_preview:boolean*',
    'whatsapp': 'access_token:secret! to:string! phone_number_id:string mode:choice template_name:string template_language:string* template_body_params:textarea* graph_api_version:string* api_url:secret* preview_url:boolean*',
    'slack': 'url:secret! message_template:textarea headers:secret* payload:textarea*',
    'discord': 'url:secret! message_template:textarea headers:secret* payload:textarea*',
    'teams': 'url:secret! message_template:textarea headers:secret* payload:textarea*',
    'google_chat': 'url:secret! message_template:textarea headers:secret* payload:textarea*',
    'ntfy': 'server:secret! topic:secret! token:secret priority:integer* tags:textarea* title_template:string* message_template:textarea*',
    'pushover': 'app_token:secret! user_key:secret! device:string* priority:integer* sound:string* title_template:string* message_template:textarea*',
    'gotify': 'server:secret! app_token:secret! priority:integer* title_template:string* message_template:textarea*',
    'email_smtp': 'host:string! port:integer username:secret password:secret from_addr:string! to_addrs:textarea! starttls:boolean* ssl:boolean* subject_template:string* body_template:textarea*',
    'email_sendgrid': 'api_key:secret! from_addr:string! to_addrs:textarea! api_url:secret* subject_template:string* body_template:textarea*',
    'sms_twilio': 'account_sid:secret! auth_token:secret! to_numbers:textarea! from_number:string messaging_service_sid:secret api_url:secret* body_template:textarea*',
    'generic_webhook': 'url:secret! body_format:choice headers:secret* payload:textarea* body_template:textarea* message_template:textarea*',
}

# Every implicit (sent-when-empty, never saved) default the alert catalog publishes.
_COST_TYPES = ["CRITICAL", "EMERGENCY", "KILL_SWITCH_ON"]
ALERT_IMPLICIT_DEFAULTS = {
    "telegram": {"parse_mode": "HTML", "disable_link_preview": "true"},
    "whatsapp": {"alert_types": _COST_TYPES, "mode": "text"},
    "pushover": {"alert_types": _COST_TYPES},
    "email_sendgrid": {"alert_types": _COST_TYPES},
    "sms_twilio": {"alert_types": _COST_TYPES},
    "generic_webhook": {"body_format": "json"},
}


def compact(fields):
    return " ".join(f"{f['id']}:{f['type']}{'!' if f['required'] else ''}{'*' if f['conditions'].get('advanced') else ''}"
                    for f in fields)


def test_import_specs_full_protocol_snapshot():
    from import_protocol_view import PROTOCOL_SPECS
    from shadowsocks_capabilities import SHADOWSOCKS_CIPHER_CHOICES
    data = catalog.CatalogQueries().import_specs({}, Context())
    assert catalog.validate_query_data("import.specs", data) == []
    assert [spec["id"] for spec in data["protocols"]] == [section.key for section in PROTOCOL_SPECS]
    assert {spec["id"]: compact(spec["fields"]) for spec in data["protocols"]} == IMPORT_SNAPSHOT
    by_id = {spec["id"]: {field["id"]: field for field in spec["fields"]} for spec in data["protocols"]}
    for spec in data["protocols"]:
        assert spec["label_id"] == f"catalog.{spec['id']}"
        for field in spec["fields"]:
            assert field["label_id"] == f"catalog.{spec['id']}.{field['id']}"
    # SS cipher list, conditional fields, notices and implicit (never saved) defaults.
    assert by_id["ss"]["method"]["choices"] == list(SHADOWSOCKS_CIPHER_CHOICES)
    assert by_id["ss"]["method"]["conditions"]["implicit_default"] == "aes-256-gcm"
    assert by_id["ss"]["method"]["conditions"]["notices"] == [
        {"code": "ss_none_unencrypted", "visible_when": [{"field": "method", "values": ["none"]}]}]
    assert by_id["ss"]["password"]["conditions"]["required_when"] == [{"field": "method", "excluded_values": ["none"]}]
    assert by_id["vless_reality"]["host"]["conditions"]["visible_when"] == [{"field": "network", "values": ["ws", "h2"]}]
    assert by_id["vless_reality"]["public_key"]["conditions"]["required_when"] == [{"field": "security", "values": ["reality"]}]
    assert by_id["trojan"]["port"]["conditions"]["min"] == 1 and by_id["trojan"]["port"]["conditions"]["max"] == 65535
    assert by_id["argo_xhttp"]["tier"]["choices"] == ["vanguard"]
    assert by_id["argo_xhttp"]["tier"]["conditions"]["chain_allowed"] is False
    assert by_id["trojan"]["tier"]["choices"] == ["vanguard", "sentinel", "bastion", "gateway"]
    # No placeholder or template default is offered as a value anywhere.
    text = json.dumps(data)
    for placeholder in ("PASSWORD", "example.com", "11111111-1111", "PRIVATE_KEY", "My-Node", "\"value\""):
        assert placeholder not in text
    assert "://" not in text


def test_catalog_cache_is_static_per_version_and_returns_copies():
    builds = []
    version = ["1.0"]

    def build():
        builds.append(1)
        return {"protocols": []}

    cache = catalog.CatalogCache(version=lambda: version[0])
    first = cache.get("import.specs", build)
    first["protocols"].append("mutated")
    assert cache.get("import.specs", build) == {"protocols": []} and len(builds) == 1
    version[0] = "1.1"
    cache.get("import.specs", build)
    assert len(builds) == 2


def test_catalog_register_uses_query_registration():
    registered = {}

    class Session:
        def register_query(self, name, handler):
            registered[name] = handler

    catalog.register(Session())
    assert set(registered) == {"import.specs"}
    assert registered["import.specs"]({}, Context())["protocols"]


# --- import.preview -------------------------------------------------------------

def test_preview_returns_bound_plan_counts_and_rejections(tmp_path):
    runtime, handler, _ = make(tmp_path)
    before = time.time_ns() // 1_000_000
    data = handler.preview({"tier": "vanguard", "text": f"{VLESS_A}\n{BROKEN}"}, Context())
    assert_preview_data(data)
    assert data["accepted"] == 1 and len(data["rejected"]) == 1
    assert data["rejected"][0]["code"] == "validation"
    assert re.fullmatch(r"errors\.import\.[a-z0-9_]+", data["rejected"][0]["message_key"])
    assert "position" not in json.dumps(data) and "fingerprint" not in json.dumps(data)
    assert before + 599_000 <= data["expires_at_ms"] <= time.time_ns() // 1_000_000 + 600_000
    plan = handler.plans._plans[data["plan_id"]]
    assert (plan.owner, plan.tier, plan.layer, plan.config_rev, plan.importable) == (
        "window-1", "vanguard", "tier1", 7, False)
    assert runtime.calls == []  # Preview never writes.


def test_preview_text_is_bounded(tmp_path):
    _, handler, _ = make(tmp_path)
    with pytest.raises(JobFailure) as info:
        handler.preview({"tier": "vanguard", "text": "a" * (bimport.MAX_PREVIEW_TEXT_BYTES + 1)}, Context())
    assert info.value.code == "validation"
    assert len(handler.plans) == 0


def test_preview_rejected_list_is_capped(tmp_path):
    runtime, handler, _ = make(tmp_path)
    runtime.import_service.preview = lambda payload: {
        "node_count": 0, "can_import": False, "plan_id": "x",
        "blocking_errors": [{"code": "invalid_node", "scheme": "vless", "message": "secret text"}] * 300}
    data = handler.preview({"tier": "vanguard", "text": "x"}, Context())
    assert_preview_data(data)
    assert len(data["rejected"]) == bimport.MAX_REJECTED + 1
    assert data["rejected"][-1]["params"] == {"omitted_count": 100}
    assert data["rejected"][0]["params"] == {"scheme": "vless"}
    assert "secret text" not in json.dumps(data)


# --- plan invalidation ----------------------------------------------------------

def paste(handler, plan_id, tier="vanguard"):
    return handler.apply({"tier": tier, "mode": "paste", "plan_id": plan_id}, Context("op-apply"))


def test_plan_invalidation_owner_tier_rev_expiry(tmp_path):
    clock = Clock()
    runtime, handler, _ = make(tmp_path, clock=clock)
    text = f"{VLESS_A}\n{TROJAN_B}"

    # Owner: another owner cannot use or burn the plan.
    plan_id = handler.preview({"tier": "vanguard", "text": text}, Context())["plan_id"]
    handler.owner_of = lambda context: "window-2"
    with pytest.raises(JobFailure) as info:
        paste(handler, plan_id)
    code, result = failed(info)
    assert code == "conflict" and result["apply_error"]["reason_code"] == "plan_owner_mismatch"
    assert result["saved"] is False and runtime.calls == []
    assert_job_result("import.apply", result, done=False)
    handler.owner_of = lambda context: "window-1"
    assert plan_id in handler.plans._plans

    # Tier.
    with pytest.raises(JobFailure) as info:
        paste(handler, plan_id, tier="gateway")
    assert failed(info)[1]["apply_error"]["reason_code"] == "plan_tier_mismatch"
    # Consumed by its owner: a second use is a missing plan.
    with pytest.raises(JobFailure) as info:
        paste(handler, plan_id)
    assert failed(info)[1]["apply_error"]["reason_code"] == "plan_missing"

    # Config rev.
    plan_id = handler.preview({"tier": "vanguard", "text": text}, Context())["plan_id"]
    runtime.rev = 8
    with pytest.raises(JobFailure) as info:
        paste(handler, plan_id)
    assert failed(info)[1]["apply_error"]["reason_code"] == "plan_config_changed"

    # Expiry (10 minutes on the monotonic clock).
    plan_id = handler.preview({"tier": "vanguard", "text": text}, Context())["plan_id"]
    clock.now += bimport.PLAN_TTL_SECONDS
    with pytest.raises(JobFailure) as info:
        paste(handler, plan_id)
    assert failed(info)[1]["apply_error"]["reason_code"] == "plan_expired"
    assert runtime.calls == [] and local_nodes(runtime, "tier1") == []


def test_plan_store_bounds_and_owner_revoke():
    clock = Clock()
    store = bimport.PlanStore(monotonic=clock, per_owner=2, capacity=3)
    make_plan = lambda owner: store.create(owner=owner, tier="vanguard", layer="tier1", text="t",  # noqa: E731
                                           service_plan_id="s", importable=True, accepted=1, config_rev=0)
    a1, a2, a3 = make_plan("a"), make_plan("a"), make_plan("a")
    assert a1.plan_id not in store._plans and {a2.plan_id, a3.plan_id} <= set(store._plans)
    b1, b2 = make_plan("b"), make_plan("b")
    assert len(store) == 3 and a2.plan_id not in store._plans  # global capacity evicts oldest
    store.revoke_owner("b")
    assert set(store._plans) == {a3.plan_id}
    clock.now += bimport.PLAN_TTL_SECONDS + 1
    make_plan("c")
    assert a3.plan_id not in store._plans
    assert len(a1.plan_id) >= 24 and a1.plan_id != a2.plan_id


# --- paste batch semantics ------------------------------------------------------

def test_paste_accepts_whole_batch(tmp_path):
    runtime, handler, secrets = make(tmp_path)
    plan_id = handler.preview({"tier": "vanguard", "text": f"{VLESS_A}\n{TROJAN_B}"}, Context())["plan_id"]
    result = paste(handler, plan_id)
    assert_job_result("import.apply", result, done=True)
    assert result["saved"] is True and result["applied"] is True and len(result["items"]) == 2
    assert all(re.fullmatch(r"[0-9a-f]{24}", item) for item in result["items"])
    assert len(local_nodes(runtime, "tier1")) == 2
    name, payload = runtime.calls[0]
    assert name == "tier1_nodes" and payload["expected_plan_id"] and payload["source"] == "native-app-tier1-import"
    # Draft secrets registered before the runtime call, restored afterwards.
    assert secrets.events[0][0] == "register" and secrets.events[0][2] == 0
    assert SECRET_PASSWORD in secrets.events[0][1] and secrets.events[-1][0] == "restore"


def test_paste_with_any_rejected_entry_rejects_whole_batch(tmp_path):
    runtime, handler, _ = make(tmp_path)
    preview = handler.preview({"tier": "vanguard", "text": f"{VLESS_A}\n{TROJAN_B}\n{BROKEN}"}, Context())
    assert preview["accepted"] == 2 and preview["rejected"]
    with pytest.raises(JobFailure) as info:
        paste(handler, preview["plan_id"])
    code, result = failed(info)
    assert code == "validation" and result["apply_error"]["reason_code"] == "plan_rejected"
    assert result["saved"] is False and result["items"] == []
    assert runtime.calls == [] and local_nodes(runtime, "tier1") == []


def test_paste_exact_one_tier_and_service_recheck(tmp_path):
    runtime, handler, _ = make(tmp_path)
    preview = handler.preview({"tier": "sentinel", "text": f"{VLESS_A}\n{TROJAN_B}"}, Context())
    assert [error["reason_code"] for error in preview["rejected"]] == ["exactly_one_node_required"]
    # A plan whose recognition changed is refused by the import service itself.
    plan_id = handler.preview({"tier": "sentinel", "text": TROJAN_B}, Context())["plan_id"]
    plan = handler.plans._plans[plan_id]
    handler.plans._plans[plan_id] = bimport.ImportPlan(**{**plan.__dict__, "service_plan_id": "0" * 64})
    with pytest.raises(JobFailure) as info:
        paste(handler, plan_id, tier="sentinel")
    code, result = failed(info)
    assert code == "conflict" and result["apply_error"]["reason_code"] == "import_plan_changed"
    assert local_nodes(runtime, "tier2") == []


def test_paste_duplicate_is_conflict_and_unsaved(tmp_path):
    runtime, handler, _ = make(tmp_path)
    paste(handler, handler.preview({"tier": "gateway", "text": TROJAN_B}, Context())["plan_id"], tier="gateway")
    with pytest.raises(JobFailure) as info:
        paste(handler, handler.preview({"tier": "gateway", "text": TROJAN_B}, Context())["plan_id"], tier="gateway")
    code, result = failed(info)
    assert code == "conflict" and result["saved"] is False
    assert result["apply_error"]["reason_code"] == "duplicate_node"
    assert len(local_nodes(runtime, "exit")) == 1


def test_running_core_applies_by_one_strict_reload(tmp_path):
    from runtime_reload_gate import RuntimeReloadReason
    runtime, handler, _ = make(tmp_path, core_running=True)
    result = paste(handler, handler.preview({"tier": "vanguard", "text": VLESS_A}, Context())["plan_id"])
    assert_job_result("import.apply", result, done=True)
    assert result["saved"] is True and result["applied"] is True
    [reload] = runtime.core_reloads
    assert reload["mode"] == "strict" and reload["reason"] == RuntimeReloadReason.USER_GRAPH_CONFIG
    assert reload["user_commit_id"].startswith("import-apply:tier1:")
    # Unconfirmed receipt (kept existing Core) and a raising reload: saved, not applied.
    for receipt, reason in (({"reloaded": False, "kept_existing_core": True}, "reload_unconfirmed"),
                            (RuntimeError("reload boom"), "core_reload_failed")):
        runtime.reload_receipt = receipt
        text = TROJAN_B if reason == "reload_unconfirmed" else VLESS_A.replace("a.example.test", "a2.example.test")
        with pytest.raises(JobFailure) as info:
            paste(handler, handler.preview({"tier": "vanguard", "text": text}, Context())["plan_id"])
        code, result = failed(info)
        assert code == "io" and result["saved"] is True and result["applied"] is False
        assert result["apply_error"]["reason_code"] == reason
        assert "boom" not in json.dumps(result)
        assert_job_result("import.apply", result, done=False)
    # Core state unknown: no reload attempted, apply is unconfirmed.
    runtime.core_fact = lambda: (_ for _ in ()).throw(RuntimeError("gone"))
    before = len(runtime.core_reloads)
    with pytest.raises(JobFailure) as info:
        manual(handler, "gateway", "trojan", {"name": "U", "server": "u.example.test", "port": 443,
                                              "password": SECRET_PASSWORD, "network": "tcp"})
    code, result = failed(info)
    assert (code, result["saved"], result["apply_error"]["reason_code"]) == ("io", True, "apply_unconfirmed")
    assert len(runtime.core_reloads) == before


def test_stopped_core_needs_no_core_reload(tmp_path):
    runtime, handler, _ = make(tmp_path)
    result = paste(handler, handler.preview({"tier": "vanguard", "text": VLESS_A}, Context())["plan_id"])
    assert result["applied"] is True and runtime.core_reloads == []


def test_stopped_core_every_import_mode_is_done_and_applied(tmp_path):
    """Cross-ticket ruling 1: Core stopped, a successful save is done with applied=true."""
    runtime, handler, _ = make(tmp_path)
    chain = [{"protocol": "trojan", "fields": {"name": "Combo", "server": "c.example.test", "port": 443,
                                                "password": SECRET_PASSWORD, "network": "tcp"}},
             {"protocol": "hysteria2", "fields": {"port": 8443, "password": "hy2-Secret-9"}}]
    results = [
        paste(handler, handler.preview({"tier": "sentinel", "text": TROJAN_B}, Context())["plan_id"],
              tier="sentinel"),
        manual(handler, "gateway", "trojan", {"name": "M", "server": "m.example.test", "port": 443,
                                              "password": SECRET_PASSWORD, "network": "tcp"}),
        handler.apply({"tier": "vanguard", "mode": "chain", "chain": chain}, Context("op-chain")),
        subscribe(handler, [SUB_URL], tier="bastion"),
    ]
    for result in results:
        assert_job_result("import.apply", result, done=True)
        assert result["saved"] is True and result["applied"] is True and result["apply_error"] is None
    assert results[-1]["subscriptions"][0]["applied"] is True
    assert runtime.core_reloads == []


def test_save_outcome_is_read_back_after_a_runtime_failure(tmp_path):
    runtime, handler, _ = make(tmp_path)
    fields = {"name": "R", "server": "r.example.test", "port": 443, "password": SECRET_PASSWORD, "network": "tcp"}
    # Written, then the runtime reload failed: saved=true, job failed (io).
    runtime.fail_after_write = RuntimeError(f"reload failed for {SECRET_PASSWORD}")
    with pytest.raises(JobFailure) as info:
        manual(handler, "gateway", "trojan", fields)
    code, result = failed(info)
    assert (code, result["saved"], result["applied"]) == ("io", True, False)
    assert result["apply_error"]["reason_code"] == "runtime_reload_failed" and len(result["items"]) == 1
    assert len(local_nodes(runtime, "exit")) == 1 and SECRET_PASSWORD not in json.dumps(result)
    assert_job_result("import.apply", result, done=False)
    # Failed before writing: saved=false.
    runtime.fail_after_write = None
    runtime.fail_before_write = RuntimeError("disk full")
    with pytest.raises(JobFailure) as info:
        manual(handler, "gateway", "trojan", {**fields, "server": "r2.example.test"})
    code, result = failed(info)
    assert (code, result["saved"], result["apply_error"]["reason_code"]) == ("io", False, "import_failed")
    runtime.fail_before_write = ValueError("bad")
    with pytest.raises(JobFailure) as info:
        manual(handler, "gateway", "trojan", {**fields, "server": "r2.example.test"})
    assert failed(info)[1]["apply_error"]["reason_code"] == "import_rejected"
    # Read-back unavailable: the outcome is reported as unknown, never as saved.
    runtime.read_local_config = lambda: (_ for _ in ()).throw(OSError("locked"))
    runtime.fail_before_write = RuntimeError("x")
    with pytest.raises(JobFailure) as info:
        manual(handler, "gateway", "trojan", {**fields, "server": "r3.example.test"})
    code, result = failed(info)
    assert (code, result["saved"], result["apply_error"]["reason_code"]) == ("io", False, "save_outcome_unknown")
    assert len(local_nodes(runtime, "exit")) == 1


def test_secret_registry_failures_are_separated_from_the_business_result(tmp_path):
    runtime, handler, secrets = make(tmp_path)
    fields = {"name": "G", "server": "g.example.test", "port": 443, "password": SECRET_PASSWORD, "network": "tcp"}

    def broken_register(values):
        raise RuntimeError("registry down")

    secrets.register_draft = broken_register
    with pytest.raises(JobFailure) as info:
        manual(handler, "gateway", "trojan", fields)
    code, result = failed(info)
    assert code == "internal" and result["saved"] is False and runtime.calls == []
    with pytest.raises(JobFailure) as info:
        subscribe(handler, [SUB_URL], tier="gateway")
    code, result = failed(info)
    assert code == "internal" and runtime.calls == []
    assert result["saved"] is False and result["items"] == []
    assert [item["error"]["reason_code"] for item in result["subscriptions"]] == ["secret_registry_failed"]
    assert_job_result("import.apply", result, done=False)
    # A failing restore never turns a completed save into a failure.
    (tmp_path / "second").mkdir()
    runtime2, handler2, secrets2 = make(tmp_path / "second")
    secrets2.restore = lambda: (_ for _ in ()).throw(RuntimeError("restore down"))
    result = manual(handler2, "gateway", "trojan", fields)
    assert result["saved"] is True and result["applied"] is True
    assert_job_result("import.apply", result, done=True)


# --- manual / chain ---------------------------------------------------------------

def manual(handler, tier, protocol_key, fields):
    return handler.apply({"tier": tier, "mode": "manual", "manual": {"protocol": protocol_key, "fields": fields}},
                         Context("op-manual"))


def test_manual_builds_uri_in_python_without_placeholders(tmp_path):
    runtime, handler, secrets = make(tmp_path)
    result = manual(handler, "gateway", "trojan", {"name": "Bravo", "server": "b.example.test", "port": 443,
                                                   "password": SECRET_PASSWORD, "network": "tcp",
                                                   "skip_cert_verify": True})
    assert_job_result("import.apply", result, done=True)
    name, payload = runtime.calls[0]
    assert name == "exit_nodes" and payload["source"] == "native-app-exit-structured-import"
    uri = local_nodes(runtime, "exit")[0]["uri"]
    assert uri.startswith(f"trojan://{SECRET_PASSWORD}@b.example.test:443?") and "allowInsecure=1" in uri
    for placeholder in ("PASSWORD", "example.com", "/ws"):
        assert placeholder not in uri
    assert SECRET_PASSWORD in secrets.events[0][1]


def test_manual_validation_rejects_before_write(tmp_path):
    runtime, handler, _ = make(tmp_path)
    cases = [
        ({"name": "B", "server": "b.example.test", "port": 443, "network": "tcp"}, "field_required", "password"),
        ({"name": "B", "server": "b.example.test", "port": 70000, "password": "p", "network": "tcp"}, "field_invalid", "port"),
        ({"name": "B", "server": "b.example.test", "port": 443, "password": "p", "bogus": "x"}, "field_unknown", None),
    ]
    for fields, reason, field in cases:
        with pytest.raises(JobFailure) as info:
            manual(handler, "vanguard", "trojan", fields)
        code, result = failed(info)
        assert code == "validation" and result["apply_error"]["reason_code"] == reason
        if field:
            assert result["apply_error"]["field"] == field
        assert_job_result("import.apply", result, done=False)
    with pytest.raises(JobFailure) as info:
        manual(handler, "gateway", "argo_xhttp", {"name": "A"})
    assert failed(info)[1]["apply_error"]["reason_code"] == "protocol_not_allowed"
    with pytest.raises(JobFailure) as info:
        manual(handler, "vanguard", "ss", {"name": "S", "server": "s.example.test", "port": 8388, "method": "aes-128-gcm"})
    assert failed(info)[1]["apply_error"]["field"] == "password"
    assert runtime.calls == []


def test_manual_values_are_checked_against_the_catalog(tmp_path):
    runtime, handler, _ = make(tmp_path)
    tuic = {"name": "T", "server": "t.example.test", "port": 443, "uuid": SECRET_UUID, "password": "tuic-pw"}
    vless = {"name": "V", "server": "v.example.test", "port": 443, "uuid": SECRET_UUID,
             "security": "reality", "public_key": "pk"}
    wireguard = {"name": "W", "server": "w.example.test", "port": 51820, "private_key": "wg-private",
                 "public_key": "wg-public", "local_address": "10.0.0.2/32"}
    trojan = {"name": "B", "server": "b.example.test", "password": "p", "network": "tcp"}
    cases = [
        ("tuic", {**tuic, "congestion_control": "bogus-cc"}, "congestion_control"),
        ("tuic", {**tuic, "udp_relay_mode": "weird"}, "udp_relay_mode"),
        ("vless_reality", {**vless, "network": "evil&x=1"}, "network"),
        ("wireguard", {**wireguard, "mtu": "abc"}, "mtu"),
        ("wireguard", {**wireguard, "mtu": 100}, "mtu"),            # below catalog min 576
        ("trojan", {**trojan, "port": True}, "port"),                # bool is not an integer
        ("trojan", {**trojan, "port": "44 3"}, "port"),
        ("trojan", {**trojan, "port": 443, "skip_cert_verify": "yes"}, "skip_cert_verify"),
        ("trojan", {**trojan, "port": 443, "sni": 7}, "sni"),        # string fields take text only
        ("ss", {"name": "S", "server": "s.example.test", "port": 8388, "password": "p",
                "method": "rot13"}, "method"),
    ]
    for protocol_key, fields, field in cases:
        with pytest.raises(JobFailure) as info:
            manual(handler, "gateway", protocol_key, fields)
        code, result = failed(info)
        assert (code, result["apply_error"]["reason_code"], result["apply_error"]["field"]) == (
            "validation", "field_invalid", field), (protocol_key, field)
        assert_job_result("import.apply", result, done=False)
    assert runtime.calls == []
    # Text integers, true/false text and empty choices (= not filled) are accepted.
    result = manual(handler, "gateway", "tuic", {**tuic, "port": "+8443", "skip_cert_verify": "true",
                                                 "congestion_control": "", "udp_relay_mode": "quic"})
    assert_job_result("import.apply", result, done=True)
    uri = local_nodes(runtime, "exit")[0]["uri"]
    # An empty choice is "not filled": the builder applies its own fallback, as in Qt.
    assert ":8443?" in uri and "udp_relay_mode=quic" in uri and "congestion_control=bbr" in uri


def test_manual_argo_on_vanguard_uses_the_multiprotocol_entry(tmp_path):
    runtime, handler, secrets = make(tmp_path)
    result = manual(handler, "vanguard", "argo_xhttp", {
        "name": "Argo", "server": "argo.example.test", "port": 443, "uuid": SECRET_UUID,
        "security": "tls", "sni": "edge.example.test", "host": "edge.example.test", "path": "/xh"})
    assert_job_result("import.apply", result, done=True)
    [(name, payload)] = runtime.calls
    assert name == "tier1_multi" and payload["fallback_order"] == "argo_xhttp" and payload["name"] == "Argo"
    assert len(local_nodes(runtime, "tier1")) == 1
    assert SECRET_UUID in secrets.events[0][1]


def test_manual_single_protocol_tiers_use_existing_entries(tmp_path):
    runtime, handler, _ = make(tmp_path)
    fields = {"name": "S", "server": "s.example.test", "port": 8388, "password": SECRET_PASSWORD,
              "method": "aes-256-gcm"}
    manual(handler, "sentinel", "ss", fields)
    manual(handler, "bastion", "ss", {**fields, "server": "t.example.test"})
    assert [name for name, _payload in runtime.calls] == ["tier2_node", "tier3_node"]
    assert runtime.reloads == ["tier2", "tier3"]


def test_chain_builds_multiprotocol_block_for_vanguard(tmp_path):
    runtime, handler, _ = make(tmp_path)
    chain = [{"protocol": "trojan", "fields": {"name": "Combo", "server": "c.example.test", "port": 443,
                                                "password": SECRET_PASSWORD, "network": "tcp"}},
             {"protocol": "hysteria2", "fields": {"port": 8443, "password": "hy2-Secret-9"}}]
    result = handler.apply({"tier": "vanguard", "mode": "chain", "chain": chain}, Context("op-chain"))
    assert_job_result("import.apply", result, done=True)
    name, payload = runtime.calls[0]
    assert name == "tier1_multi" and payload["fallback_order"] == "trojan -> hysteria2"
    assert payload["name"] == "Combo" and "hysteria2://hy2-Secret-9@c.example.test:8443" in payload["text"]
    assert len(local_nodes(runtime, "tier1")) == 1
    with pytest.raises(JobFailure) as info:
        handler.apply({"tier": "gateway", "mode": "chain", "chain": chain}, Context())
    assert failed(info)[1]["apply_error"]["reason_code"] == "chain_tier_not_allowed"
    with pytest.raises(JobFailure) as info:
        handler.apply({"tier": "vanguard", "mode": "chain", "chain": [chain[0], chain[0]]}, Context())
    assert failed(info)[1]["apply_error"]["reason_code"] == "chain_duplicate_protocol"
    # Qt parity: a chain needs at least two protocols (one goes through mode="manual").
    for short in ([chain[0]], []):
        with pytest.raises(JobFailure) as info:
            handler.apply({"tier": "vanguard", "mode": "chain", "chain": short}, Context())
        code, result = failed(info)
        assert (code, result["apply_error"]["reason_code"], result["apply_error"]["field"]) == (
            "validation", "chain_length_invalid", "chain")
        assert_job_result("import.apply", result, done=False)
    assert len(runtime.calls) == 1


# --- subscriptions / first refresh ------------------------------------------------

def subscribe(handler, urls, tier="sentinel"):
    return handler.apply({"tier": tier, "mode": "subscriptions", "urls": urls}, Context("op-sub"))


def test_subscriptions_success_returns_refresh_results(tmp_path):
    runtime, handler, secrets = make(tmp_path)
    result = subscribe(handler, [SUB_URL])
    assert_job_result("import.apply", result, done=True)
    assert result["subscriptions"] == [{"total": 3, "healthy": 2, "stored": True, "applied": True,
                                        "retained_previous": False, "apply_error": None, "error": None}]
    from bridge_runtime_access import opaque_id
    assert result["items"] == [opaque_id("tier2", SUB_URL)]
    assert SUB_URL in secrets.events[0][1]


def test_first_refresh_failure_is_saved_but_job_failed_with_items(tmp_path):
    def refresh(url):
        if "bad" in url:
            raise RuntimeError(f"fetch failed for {url}")
        return {"total": 4, "healthy": 4, "stored": True}

    runtime, handler, _ = make(tmp_path, refresh=refresh)
    bad = "https://bad.example.test/sub?token=Zq9-bad-token"
    with pytest.raises(JobFailure) as info:
        subscribe(handler, [SUB_URL, bad], tier="gateway")
    code, result = failed(info)
    assert code == "io" and result["saved"] is True and result["applied"] is False
    assert [item["stored"] for item in result["subscriptions"]] == [True, False]
    assert result["subscriptions"][1]["error"]["reason_code"] == "initial_refresh_failed"
    assert len(result["items"]) == 2  # Both subscriptions were saved.
    assert len(runtime.store.read_local()["tiers"]["exit"]["subscription_urls"]) == 2
    assert_job_result("import.apply", result, done=False)
    assert "bad.example.test" not in json.dumps(result) and "fetch failed" not in json.dumps(result)


@pytest.mark.parametrize("core", ["running", "stopped", "unknown"])
def test_subscription_registry_failure_mid_batch_keeps_alignment_and_applies_stored(tmp_path, core):
    runtime, handler, secrets = make(tmp_path, core_running=core == "running")
    if core == "unknown":
        runtime.core_fact = lambda: (_ for _ in ()).throw(RuntimeError("gone"))
    registered = []

    def register_once(values):
        registered.append(values)
        if len(registered) > 1:
            raise RuntimeError("registry down")

    secrets.register_draft = register_once
    urls = [SUB_URL, "https://two.example.test/sub?token=Zq9-two", "https://three.example.test/sub?token=Zq9-3"]
    with pytest.raises(JobFailure) as info:
        subscribe(handler, urls, tier="gateway")
    code, result = failed(info)
    assert code == "internal"
    # subscriptions[i] answers urls[i]: stored, failed registration, not attempted.
    assert len(result["subscriptions"]) == len(urls)
    first, second, third = result["subscriptions"]
    assert [item["stored"] for item in result["subscriptions"]] == [True, False, False]
    assert (second["error"]["code"], second["error"]["reason_code"]) == ("internal", "secret_registry_failed")
    assert (third["error"]["code"], third["error"]["reason_code"]) == ("internal", "batch_aborted")
    assert [name for name, _payload in runtime.calls] == ["exit_sub"]  # Nothing written after the failure.
    assert result["saved"] is True and len(result["items"]) == 1 and result["applied"] is False
    # The stored subscription still went through the Core apply step.
    if core == "running":
        assert len(runtime.core_reloads) == 1
        assert first["applied"] is True and first["apply_error"] is None
    elif core == "stopped":
        assert runtime.core_reloads == [] and first["applied"] is True and first["apply_error"] is None
    else:
        assert runtime.core_reloads == [] and first["applied"] is False
        assert first["apply_error"]["reason_code"] == "apply_unconfirmed"
    assert_job_result("import.apply", result, done=False)
    assert "Zq9" not in json.dumps(result)


def test_subscription_refresh_without_store_and_duplicate(tmp_path):
    runtime, handler, _ = make(tmp_path, refresh=lambda url: {"total": 0, "healthy": 0, "stored": False,
                                                                "error": "empty"})
    with pytest.raises(JobFailure) as info:
        subscribe(handler, [SUB_URL])
    code, result = failed(info)
    assert code == "io" and result["saved"] is True
    with pytest.raises(JobFailure) as info:
        subscribe(handler, [SUB_URL])
    code, result = failed(info)
    assert result["saved"] is False and result["subscriptions"][0]["error"]["code"] in {"conflict", "validation"}
    for urls, tier in (([SUB_URL], "vanguard"), ([SUB_URL, SUB_URL], "sentinel"), ([" "], "sentinel")):
        with pytest.raises(JobFailure) as info:
            subscribe(handler, urls, tier=tier)
        assert failed(info)[0] == "validation"


# --- secrets ---------------------------------------------------------------------

def test_secrets_never_leave_results_errors_jobs_or_logs(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    runtime = FakeRuntime(tmp_path, refresh=lambda url: (_ for _ in ()).throw(RuntimeError(url)))
    executor = JobExecutor(JobEventRecorder(8192), epoch="p2-15", require_acceptance=True,
                           normal_workers=2, normal_queue=16)
    revoke_hooks = []

    class Session:
        pass

    session = Session()
    session.executor = executor
    session.register_owner_revoke_hook = revoke_hooks.append
    secrets = RecordingSecrets(runtime)
    handler = bimport.register(session, port=bimport.RuntimeImportAdapter(lambda: runtime),
                               secrets_registry=secrets)
    assert {"import.preview", "import.apply"} <= set(executor._specs) and revoke_hooks
    try:
        receipt = executor.submit("import.preview", {"tier": "vanguard", "text": f"{VLESS_A}\n{TROJAN_B}"},
                                  req_id="req-1", owner="window-1")
        response = receipt.wait(5)
        assert response.ok and response.data["accepted"] == 2
        plan_id = response.data["plan_id"]
        assert handler.plans._plans[plan_id].owner == "window-1"  # default executor owner resolver
        outcomes = []
        for args in ({"tier": "vanguard", "mode": "paste", "plan_id": plan_id},
                     {"tier": "gateway", "mode": "manual", "manual": {"protocol": "trojan", "fields": {
                         "name": "M", "server": "m.example.test", "port": 443, "password": SECRET_PASSWORD,
                         "network": "tcp"}}},
                     {"tier": "gateway", "mode": "subscriptions", "urls": [SUB_URL]},
                     {"tier": "vanguard", "mode": "manual", "manual": {"protocol": "trojan", "fields": {
                         "name": "X", "server": "x.example.test", "password": SECRET_PASSWORD}}}):
            ack = executor.submit("import.apply", args, req_id="req-2", owner="window-1")
            assert ack.accepted and executor.acceptance_sent(ack.op, epoch=executor.epoch)
            outcomes.append(executor.wait(ack.op, 10))
        assert [outcome["state"] for outcome in outcomes] == ["done", "done", "failed", "failed"]
        assert outcomes[2]["result"]["saved"] is True and outcomes[2]["error"]["code"] == "io"
        events = executor.recorder.prepare_for_send(100)
        assert events and all(protocol.validate_frame(event) == [] for event in events)
        executor.recorder.ack_sent(events)
        # Owner revoke drops plans of that owner.
        receipt = executor.submit("import.preview", {"tier": "vanguard", "text": VLESS_A},
                                  req_id="req-3", owner="window-1")
        assert receipt.wait(5).ok and len(handler.plans) == 1
        revoke_hooks[0]("window-1")
        assert len(handler.plans) == 0
        published = json.dumps([events, outcomes, response.data])
    finally:
        executor.shutdown(time.monotonic() + 2)
    logged = "\n".join(record.getMessage() for record in caplog.records)
    for secret in (SECRET_PASSWORD, SECRET_UUID, "Zq9-sub-token-77", "sub.example.test", "a.example.test"):
        assert secret not in published
        assert secret not in logged
    # Every runtime entry ran inside a registered-draft window: the i-th
    # registration precedes the i-th runtime call, and each is restored after it.
    assert [(event[0], event[2]) for event in secrets.events] == [
        ("register", 0), ("restore", 1), ("register", 1), ("restore", 2), ("register", 2), ("restore", 3)]
    assert len(runtime.calls) == 3


def test_runtime_logs_during_import_are_redacted_by_the_real_registry(tmp_path):
    """Real SessionSecretRegistry + LogCollector (KnownSecrets) path, not a fake."""
    from bridge_logs import LogCollector
    from bridge_state import PublicationState
    state = PublicationState("p2-15-redaction")
    state.subscribe(["logs.batch"])
    logs = LogCollector(state, logger=logging.Logger("p2-15-redaction"))
    runtime = FakeRuntime(tmp_path, refresh=lambda url: (_ for _ in ()).throw(RuntimeError(url)))
    cursor = [0]

    def log_hook(name, payload):
        # Failure reasons in the runtime quote the raw input (URI, URL, password).
        cursor[0] += 1
        text = " | ".join(str(value) for value in payload.values())
        assert logs.ingest_source({"source": "TESSERACT", "level": "warning",
                                   "message_raw": f"import {name} rejected: {text} pw={SECRET_PASSWORD}"},
                                  source="runtime", generation=1, cursor=cursor[0])

    runtime.log_hook = log_hook

    class Access:
        def known_secret_values(self):
            # Saved-config secrets, as RuntimeAccess derives them. Before the
            # first save this is empty: only the draft registration can redact.
            from bridge_runtime_access import _config_secret_values
            return _config_secret_values(runtime.read_local_config())

    class Session:
        pass

    session = Session()
    session.access, session.logs = Access(), logs
    session.refresh_secrets = lambda: logs.refresh_known_secret_values(session.access.known_secret_values())
    handler = bimport.ImportCommands(bimport.RuntimeImportAdapter(lambda: runtime), owner_of=lambda c: "w",
                                     secrets_registry=bimport.SessionSecretRegistry(session))
    paste(handler, handler.preview({"tier": "vanguard", "text": TROJAN_B}, Context())["plan_id"])
    manual(handler, "gateway", "trojan", {"name": "M", "server": "m.example.test", "port": 443,
                                          "password": SECRET_PASSWORD, "network": "tcp"})
    with pytest.raises(JobFailure):
        subscribe(handler, [SUB_URL], tier="gateway")
    records = logs.tail({"n": 100, "since_seq": 0})["records"]
    assert len(records) == 3
    text = json.dumps(records)
    for secret in (SECRET_PASSWORD, SUB_URL, "Zq9-sub-token-77"):
        assert secret not in text
    # Control: without the draft registration the same log line leaks.
    logs.refresh_known_secret_values({})
    cursor[0] += 1
    logs.ingest_source({"source": "TESSERACT", "level": "warning", "message_raw": f"pw={SECRET_PASSWORD}"},
                       source="runtime", generation=1, cursor=cursor[0])
    assert SECRET_PASSWORD in json.dumps(logs.tail({"n": 100, "since_seq": 0})["records"])


def test_preview_from_a_revoked_owner_leaves_no_plan(tmp_path):
    import threading
    _, handler, _ = make(tmp_path)
    context = Context()
    context.owner_revoked = threading.Event()
    real_create = handler.plans.create

    def create_then_revoke(**kwargs):
        plan = real_create(**kwargs)
        context.owner_revoked.set()  # Revoke hooks ran while the plan was being created.
        return plan

    handler.plans.create = create_then_revoke
    with pytest.raises(JobFailure) as info:
        handler.preview({"tier": "vanguard", "text": VLESS_A}, context)
    assert info.value.code == "not_allowed" and len(handler.plans) == 0
    with pytest.raises(JobFailure):
        handler.preview({"tier": "vanguard", "text": VLESS_A}, context)
    assert len(handler.plans) == 0


def test_plan_store_global_byte_bound():
    store = bimport.PlanStore(monotonic=Clock(), max_bytes=10)
    plans = [store.create(owner=f"o{i}", tier="vanguard", layer="tier1", text="x" * 4, service_plan_id="s",
                          importable=True, accepted=1, config_rev=0) for i in range(3)]
    assert set(store._plans) == {plans[1].plan_id, plans[2].plan_id}  # 4+4 <= 10 < 12
    assert bimport.MAX_PLAN_BYTES <= 8 * 1024 * 1024


def test_owner_resolver_prefers_public_executor_api():
    class Executor:
        def owner_of(self, op):
            return {"op-9": "window-9"}.get(op)

    assert bimport.executor_owner_resolver(Executor())(Context("op-9")) == "window-9"


def test_missing_owner_is_not_allowed(tmp_path):
    runtime, handler, _ = make(tmp_path, owner=None)
    for call, args in ((handler.preview, {"tier": "vanguard", "text": VLESS_A}),
                       (handler.apply, {"tier": "vanguard", "mode": "paste", "plan_id": "p"})):
        with pytest.raises(JobFailure) as info:
            call(args, Context())
        assert info.value.code == "not_allowed"
