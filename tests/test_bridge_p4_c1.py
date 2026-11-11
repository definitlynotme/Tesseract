"""T-P4-C1: v1 contract additions (no proto bump), first-release blockers.

Items: 1 Bastion write path, 2 log facts and live key-event titles, 3 node
subscription ownership, 4 config.snapshot projection gaps (D8 included),
5 kill switch triggered, 6 first-run language, 7 update completed,
8 re-enabling a disabled node, 9 masked source hints (U4).

Isolated: temporary config directories, recording fakes, no live profile,
no network, no HKCU or firewall writes.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re
from types import SimpleNamespace

import pytest
import yaml

import bridge_protocol as protocol
from bridge_jobs import JobFailure
from bridge_runtime_access import (config_projection, display_filter, source_hint,
                                   _config_secret_values)
from tests.test_bridge_p2_10 import FakeRuntime, Harness, sections


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "bridge_schema" / "fixtures"
V1_VALID, V1_INVALID = FIXTURES / "v1" / "valid", FIXTURES / "v1" / "invalid"
DOT = "\u2022"
BOT_TOKEN = "123456789:AAc1FixtureTokenValue_abcdefghijk"
SMTP_PASSWORD = "SmtpFixturePassword-99"


@pytest.fixture
def rt(tmp_path):
    return FakeRuntime(tmp_path)


def projection(rt):
    facts = rt.bridge_config_facts()
    clean = display_filter({"config": facts["config"], "sources": facts["sources"]})
    return config_projection(facts["config"], facts["sources"], clean=clean)


def snapshot_frame(values, fields=()):
    return {"t": "res", "epoch": "p4c1", "produced_at_ms": 1, "sent_at_ms": 2, "id": 1,
            "req_id": "q-1", "owner": "page-1", "cmd": "config.snapshot", "ok": True,
            "data": {"rev": 1, "values": values, "secret_fields_set": list(fields),
                     "read_errors": [], "pending": None}}


def write_local(rt, update):
    payload = rt.config_store.read_local()
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(payload.get(key), dict):
            payload[key].update(deepcopy(value))
        else:
            payload[key] = deepcopy(value)
    rt.config_store.write_local(payload)
    rt.reload_config()


# --------------------------------------------------------------- fixtures

def test_p4c1_fixtures_are_new_files_and_validate():
    valid = sorted(V1_VALID.glob("p4c1_*.json"))
    invalid = sorted(V1_INVALID.glob("p4c1_*.json"))
    assert len(valid) >= 10 and len(invalid) >= 10
    for path in valid:
        assert protocol.validate_frame(json.loads(path.read_bytes())) == [], path.name
    for path in invalid:
        case = json.loads(path.read_bytes())
        assert case["expected"] in protocol.validate_frame(case["frame"]), path.name


def test_fields_absent_in_old_fixtures_still_pass():
    """Rule 1 (B10): every addition is optional; the frozen v1 fixtures stand."""
    for name in ("response_config.snapshot.json", "topic_nodes.list.json",
                 "topic_bastion.sources.json", "topic_killswitch.state.json",
                 "topic_update.state.json", "topic_logs.batch.json",
                 "response_logs.tail.json", "command_settings.apply.json", "hello_v1_caps.json"):
        frame = json.loads((V1_VALID / name).read_bytes())
        assert protocol.validate_frame(frame) == [], name
    p1 = json.loads((FIXTURES / "valid" / "topic_logs_batch.json").read_bytes())
    assert protocol.validate_frame(p1) == []


def test_hello_caps_unchanged_and_feature_names_use_b_item_ids():
    hello = json.loads((V1_VALID / "hello_v1_caps.json").read_bytes())
    for good in (["B-30a"], ["B-31", "B-30a"], []):
        hello["caps"]["features"] = good
        assert protocol.validate_frame(hello) == []
    for bad in (["b-31"], ["B-"], ["B-3ab"], ["H-1"], ["B-31", "B-31"]):
        hello["caps"]["features"] = bad
        assert protocol.validate_frame(hello), bad


# ------------------------------------------------------------ 1. Bastion

def test_bastion_projection_and_change_shape(rt):
    values, _ = projection(rt)
    tier3 = rt.config["tiers"]["tier3"]
    assert values["bastion"] == {"enabled": bool(tier3.get("enabled")),
                                 "min_alive_nodes": min(tier3["max_pool_size"], tier3["min_alive_nodes"])}
    errors = []
    protocol._schema({"section": "bastion", "values": {"enabled": True}}, "ConfigChange", "c", errors)
    assert errors == []


def test_bastion_save_uses_the_qt_tier3_entry_and_python_reload_only(rt):
    h = Harness(rt)
    reloads = len(rt.reloads)
    result = h.apply([{"section": "bastion", "values": {"enabled": True, "min_alive_nodes": 12}}])
    section = sections(result)["bastion"]
    assert (section["saved"], section["applied"], section["effect"], section["error"]) == (True, True, "none", None)
    local = yaml.safe_load(rt.local_path.read_text(encoding="utf-8"))
    assert local["tiers"]["tier3"]["enabled"] is True and local["tiers"]["tier3"]["min_alive_nodes"] == 12
    assert "max_pool_size" not in local["tiers"]["tier3"]
    assert len(rt.reloads) == reloads + 1                     # update_local_config -> reload_config
    assert not [e for e in rt.events if isinstance(e, tuple) and e[0] == "reload_core"]
    assert "restart_core" not in rt.events
    assert projection(rt)[0]["bastion"] == {"enabled": True, "min_alive_nodes": 12}
    assert [topic for topic, _ in h.edges] == ["config.changed"]


def test_bastion_min_alive_upper_bound_is_the_pool_capacity(rt):
    capacity = rt.config["tiers"]["tier3"]["max_pool_size"]
    h = Harness(rt)
    before = rt.local_path.read_bytes()
    with pytest.raises(JobFailure) as caught:
        h.apply([{"section": "bastion", "values": {"min_alive_nodes": capacity + 1}}])
    error = sections(caught.value.partial_result)["bastion"]["error"]
    assert error["code"] == "validation" and error["field"] == "bastion.min_alive_nodes" and error["hold"]
    assert rt.local_path.read_bytes() == before
    for value, valid in ((capacity + 1, False), (capacity, True), (0, False), (True, False), ("7", True)):
        answer = h.service.validate({"section": "bastion", "field": "min_alive_nodes", "value": value})
        assert answer["valid"] is valid, value
    with pytest.raises(JobFailure):
        h.apply([{"section": "bastion", "values": {"max_pool_size": 500}}])   # not page-editable


# ----------------------------------------------------- 4. projection gaps

# T-P2-H2 (C1-#2): a resolver with a fragment (Mihomo's "#RULES" suffix) is
# now withheld (fail closed); the public examples carry none.
DOH = ["https://1.1.1.1/dns-query", "tls://9.9.9.9"]


def configure_rich(rt):
    write_local(rt, {
        "client": {"dns": {"mode": "custom", "nameserver": DOH,
                           "proxy_server_nameserver": ["quic://dns.example.invalid"],
                           "default_nameserver": ["system"],
                           "fallback": ["https://user:pass@doh.example.invalid/q"]},
                   "egress_recovery": {"enabled": False, "retry_source_refresh": False}},
        "killswitch": {"enabled": True, "allow_lan": False, "allow_dns": True,
                       "dns_whitelist": ["192.0.2.53"], "additional_whitelist": ["198.51.100.0/24"]},
        "alerts": {"channels": [
            {"id": "ops", "type": "telegram", "label": "Ops", "enabled": False,
             "alert_types": ["critical"],
             "config": {"enabled": False, "bot_token": BOT_TOKEN, "chat_id": "-1001234567890",
                        "parse_mode": "HTML", "silent": True}},
            {"id": "mail", "type": "email_smtp", "label": "Mail", "enabled": False,
             "alert_types": ["critical"],
             "config": {"enabled": False, "host": "smtp.example.invalid", "port": 465, "ssl": True,
                        "username": "mailer", "password": SMTP_PASSWORD,
                        "from_addr": "alerts@example.invalid", "to_addrs": ["ops@example.invalid"]}},
        ]},
        "ui": {"language": "en_US", "start_in_tray_on_autostart": True},
    })


def test_projection_fills_killswitch_egress_dns_and_alert_fields(rt):
    configure_rich(rt)
    values, fields = projection(rt)
    assert values["killswitch"] == {"enabled": True, "allow_lan": False, "allow_dns": True,
                                    "dns_whitelist": ["192.0.2.53"],
                                    "additional_whitelist": ["198.51.100.0/24"]}
    assert values["network"]["egress_recovery"] == {"enabled": False, "retry_source_refresh": False}
    # DoH/DoT/DoQ addresses are configuration; userinfo stays a secret field.
    assert values["dns"]["nameserver"] == DOH
    assert values["dns"]["proxy_server_nameserver"] == ["quic://dns.example.invalid"]
    # A list holding a secret entry is withheld whole (re-saving cannot drop it).
    assert "fallback" not in values["dns"] and "dns.fallback" in fields
    assert not any(field.startswith("dns.nameserver") for field in fields)
    # 提醒 redesign 10-30: alerts are three switches; retired channels never project.
    assert values["alerts"] == {"notify": {"enabled": True,
                                           "events": ["connection", "tier_fallback", "update"],
                                           "sound": True}}
    assert values["ui"]["language"] == "en_US"
    frame = snapshot_frame(values, fields)
    assert protocol.validate_frame(frame) == []
    text = json.dumps(frame, ensure_ascii=False)
    for secret in (BOT_TOKEN, SMTP_PASSWORD, "mailer", "user:pass", "node_judgement"):
        assert secret not in text


def test_retired_alert_channels_never_reach_the_snapshot(rt):
    configure_rich(rt)
    values, fields = projection(rt)
    assert set(values["alerts"]) == {"notify"}
    assert not [field for field in fields if field.startswith("alerts.")]
    text = json.dumps(snapshot_frame(values, fields), ensure_ascii=False)
    for retired in ("-1001234567890", "smtp.example.invalid", "ops@example.invalid", "chat_id", "bot_token"):
        assert retired not in text


def test_dns_resolver_exemption_is_exact():
    assert protocol.public_dns_resolver("https://1.1.1.1/dns-query")
    assert protocol.public_dns_resolver("tls://dns.google")
    assert not protocol.public_dns_resolver("https://u:p@doh.example.invalid/q")
    assert not protocol.public_dns_resolver("https://doh.example.invalid/q?token=abcdef")
    assert not protocol.public_dns_resolver("tls://9.9.9.9#RULES")          # T-P2-H2
    values = {"dns": {"nameserver": ["https://1.1.1.1/dns-query"]}}
    assert protocol.validate_frame(snapshot_frame(values)) == []
    elsewhere = {"network": {"mode": "https://1.1.1.1/dns-query"}}
    assert "sensitive:data.values.network.mode" in protocol.validate_frame(snapshot_frame(elsewhere))
    # The page may submit resolver addresses in a dns change, nowhere else.
    request = {"t": "req", "epoch": "p4c1", "produced_at_ms": 1, "sent_at_ms": 2, "id": 1,
               "req_id": "r-1", "owner": "page-1", "cmd": "settings.apply", "from": "page",
               "args": {"base_rev": 0, "changes": [{"section": "dns", "values": {"nameserver": DOH}}]}}
    assert protocol.validate_frame(request) == []
    request["args"]["changes"][0]["section"] = "network"
    assert protocol.validate_frame(request)
    # Not a registered known secret (logs show it), userinfo still is.
    table = _config_secret_values({"config": {"client": {"dns": {
        "nameserver": DOH + ["https://u:p@doh.example.invalid/q"]}}}})
    assert not set(DOH) & set(table) and "https://u:p@doh.example.invalid/q" in table


def configure_withheld_lists(rt):
    """A list holding one withheld entry (fix M1): a URL in the kill switch whitelist."""
    configure_rich(rt)
    payload = rt.config_store.read_local()
    payload["killswitch"]["additional_whitelist"] = ["10.0.0.0/8", "https://corp.example.invalid"]
    rt.config_store.write_local(payload)
    rt.reload_config()


def test_list_with_a_withheld_entry_is_withheld_whole(rt):
    configure_withheld_lists(rt)
    values, fields = projection(rt)
    assert "additional_whitelist" not in values["killswitch"]
    assert "killswitch.additional_whitelist" in fields
    # Field-level IDs only: never one entry's index.
    assert not [f for f in fields if re.search(r"\.additional_whitelist\.\d+$", f)]
    assert values["killswitch"]["dns_whitelist"] == ["192.0.2.53"]   # untouched list stays shown
    text = json.dumps(snapshot_frame(values, fields))
    assert "corp.example.invalid" not in text


# Editable routing.rules keys (bridge_validation._normal); raw / rule_sets
# and rule_providers are projected read-only.
ROUTING_RULE_KEYS = {"direct", "proxy", "reject", "compatibility_defaults"}
# Sections whose editable fields the rich fake configuration projects.
RESAVED = {"network", "dns", "routing", "latency", "sources", "alerts", "ui", "killswitch", "bastion"}


def editable_changes(rt, values):
    """The page-editable part of config.snapshot.values, unchanged: every
    field registered in bridge_validation.FIELDS (nested blocks: NESTED)."""
    from bridge_validation import FIELDS, NESTED
    changes = []
    for section, names in FIELDS.items():
        if section == "sources":
            changes.append({"section": section, "values": {"items": deepcopy(values["sources"])}})
            continue
        current = values.get(section) or {}
        block = {name: deepcopy(current[name]) for name in names if name in current}
        for name in set(block) & set(NESTED):
            block[name] = {key: item for key, item in block[name].items() if key in NESTED[name]}
        if "rules" in block and section == "routing":
            block["rules"] = {key: item for key, item in block["rules"].items() if key in ROUTING_RULE_KEYS}
        if section == "ui":
            # D8 is projected by desktop_runtime.bridge_config_values (T-P2-RT2).
            block["start_in_tray_on_autostart"] = rt.config["ui"]["start_in_tray_on_autostart"]
        if block:
            changes.append({"section": section, "values": block})
    return changes


@pytest.mark.parametrize("configure", [configure_rich, configure_withheld_lists])
def test_unchanged_values_resave_is_byte_identical(rt, configure):
    """Every page-editable field (bridge_validation.FIELDS / NESTED) submitted
    with its snapshot value -> no write, configuration files byte for byte.

    Not the whole values object: read-only projected fields (dns.enable,
    dns.listen, network.mode/controller/subscription, network.tun.device,
    routing.rules.raw, ui.theme, ...) are not page-editable and must not be
    submitted back (PROTOCOL §16, fix M2); the next test pins that.
    """
    configure(rt)
    values, _ = projection(rt)
    changes = editable_changes(rt, values)
    assert {change["section"] for change in changes} == RESAVED
    before = {path: path.read_bytes() for path in (rt.local_path, rt.base_path, rt.sources_path)}
    writes, rev = rt.local_writes, rt.bridge_config_revision()
    h = Harness(rt)
    result = h.apply(changes)
    assert all(row["saved"] and row["applied"] and row["error"] is None for row in result["sections"])
    assert {path: path.read_bytes() for path in before} == before
    assert rt.local_writes == writes and rt.bridge_config_revision() == rev and h.edges == []


@pytest.mark.parametrize("section, values, field", [
    ("dns", {"enable": True}, "dns.enable"),
    ("dns", {"listen": "127.0.0.1:1053"}, "dns.listen"),
    ("network", {"mode": "system"}, "network.mode"),
    ("network", {"controller": {"enabled": True}}, "network.controller"),
    ("ui", {"theme": "custom-retained"}, "ui.theme"),
])
def test_read_only_projected_fields_are_not_submittable(section, values, field):
    """PROTOCOL §16 (fix M2): the resave promise covers editable fields only."""
    from bridge_validation import validate_change
    result = validate_change({"section": section, "values": values}, {section: {}})
    assert [error["params"]["field"] for error in result.errors] == [field]


# --------------------------- 提醒 redesign 10-30: alerts.notify switches

def local_alerts(rt):
    return yaml.safe_load(rt.local_path.read_text(encoding="utf-8"))["alerts"]


def test_notify_save_writes_the_changed_leaf_and_drops_retired_channel_blocks(rt):
    configure_rich(rt)
    payload = rt.config_store.read_local()
    payload["alerts"]["ntfy"] = {"enabled": True, "server": "https://ntfy.example.invalid", "topic": "fixture-topic"}
    payload["alerts"]["desktop"] = {"enabled": False, "alert_types": ["CRITICAL"]}
    rt.config_store.write_local(payload)
    rt.reload_config()
    result = Harness(rt).apply([{"section": "alerts", "values": {"notify": {"sound": False}}}])
    assert sections(result)["alerts"]["saved"]
    assert local_alerts(rt) == {"notify": {"sound": False}}
    text = rt.local_path.read_text(encoding="utf-8")
    for retired in (BOT_TOKEN, SMTP_PASSWORD, "fixture-topic", "channels"):
        assert retired not in text
    assert projection(rt)[0]["alerts"]["notify"] == {
        "enabled": True, "events": ["connection", "tier_fallback", "update"], "sound": False}


def test_notify_validation_is_closed():
    from bridge_validation import validate_change, validate_field
    current = {"alerts": {"notify": {"enabled": True, "events": ["connection"], "sound": True}}}
    ok = validate_change({"section": "alerts", "values": {"notify": {"events": ["update", "connection"]}}}, current)
    assert ok.valid and ok.values["notify"]["events"] == ["connection", "update"]
    for bad in ({"notify": {"events": ["sms"]}}, {"notify": {"events": "connection"}},
                {"notify": {"enabled": "yes"}}, {"notify": {"channels": []}}, {"channels": []}):
        assert not validate_change({"section": "alerts", "values": bad}, current).valid, bad
    assert validate_field("alerts", "notify.sound", False) == {"valid": True, "errors": []}
    assert validate_field("alerts", "notify.events", ["x"])["valid"] is False


def test_d8_tray_flag_validate_apply_and_read_back(rt):
    from bridge_validation import validate_change, validate_field
    assert validate_field("ui", "start_in_tray_on_autostart", True) == {"valid": True, "errors": []}
    assert validate_field("ui", "start_in_tray_on_autostart", "yes")["valid"] is False
    assert validate_change({"section": "ui", "values": {"start_in_tray_on_autostart": False}},
                           {"ui": {"language": "en_US"}}).valid
    h = Harness(rt)
    assert h.service.validate({"section": "ui", "field": "start_in_tray_on_autostart",
                               "value": True})["valid"] is True
    h.apply([{"section": "ui", "values": {"start_in_tray_on_autostart": True}}])
    local = yaml.safe_load(rt.local_path.read_text(encoding="utf-8"))
    assert local["ui"]["start_in_tray_on_autostart"] is True
    # desktop_runtime.bridge_config_values projects the boolean (T-P2-RT2).
    from desktop_runtime import DesktopRuntime
    rt.bridge_secret_facts = lambda: {"config": rt.config, "sources": {}, "nodes": []}
    rt._bridge_effective_desktop_alerts = lambda config, errors: errors
    rt._bridge_read_owner = lambda: None
    rt._bridge_owner_current = lambda owner: True
    snapshot = DesktopRuntime.bridge_config_values(rt)
    assert snapshot["values"]["ui"]["start_in_tray_on_autostart"] is True


# ---------------------------------------------------------- 2. logs

def collector(language=None):
    from bridge_logs import LogCollector
    from bridge_state import PublicationState
    state = PublicationState("p4c1")
    state.subscribe(["logs.batch"])
    logs = LogCollector(state)
    if language is not None:
        logs.set_language_source(lambda: language[0])
    return logs


def key_record(key="kill_switch.activated", message="activated"):
    return {"source": "TESSERACT", "level": "CRITICAL", "event_key": key,
            "message": message, "raw_message": message}


def test_log_key_is_unified_and_new_facts_are_carried():
    logs = collector()
    logs.put_nowait({"source": "MIHOMO", "level": "info", "message": "[TCP] a --> b"})
    logs.put_nowait(key_record())
    logs.put_nowait({"source": "TESSERACT", "level": "info", "event_key": "runtime.start_requested",
                     "message": "start"})
    logs.put_nowait({"source": "TESSERACT", "level": "info", "message": "after start"})
    batch = logs.flush()
    assert protocol.validate_logs_batch(batch) == []
    raw, key, start, after = batch["records"]
    history = logs.tail({"n": 10})["records"]
    assert raw["key"] == history[0]["key"] == "log.raw"
    assert (raw["is_key_event"], key["is_key_event"]) == (False, True)
    assert "run_id" not in raw and "run_id" not in key
    assert start["run_id"] == after["run_id"] == history[3]["run_id"] == logs.feed()["run_id"]
    assert [row["is_key_event"] for row in history] == [False, True, False, False]


def test_live_key_event_title_follows_the_current_language():
    language = ["en_US"]
    logs = collector(language)
    logs.put_nowait(key_record())
    row = logs.flush()["records"][0]
    assert row["title"] == "Kill switch activated"
    assert logs.tail({"n": 5})["records"][0]["title"] == "Kill switch activated"
    language[0] = "ja_JP"
    logs.put_nowait(key_record())
    row = logs.flush()["records"][0]
    assert row["title"] == "キルスイッチ保護を有効化"
    page = logs.query({"before_seq": 10, "filter": {}, "n": 5})["records"]
    assert {r["title"] for r in page} == {"キルスイッチ保護を有効化"}   # history re-renders
    language[0] = "zh_CN"
    logs.put_nowait(key_record())
    assert logs.flush()["records"][0]["title"] == "熔断保护已启用"


def test_log_page_cursor_is_bound_to_the_language():
    """Fix S2: titles and the text filter follow the language, so a cursor
    from before a language switch needs a new snapshot (PROTOCOL §5.5)."""
    from bridge_log_view import SnapshotRequired
    language = ["en_US"]
    logs = collector(language)
    for _ in range(3):
        logs.put_nowait(key_record())
    logs.flush()
    first = logs.query({"before_seq": 100, "filter": {}, "n": 1})
    assert first["next_cursor"]
    following = {"before_seq": 100, "filter": {}, "n": 1, "cursor": first["next_cursor"]}
    assert logs.query(dict(following))["records"]               # same language: continues
    language[0] = "ja_JP"
    with pytest.raises(SnapshotRequired):
        logs.query(dict(following))
    tail = logs.tail({"n": 1, "since_seq": 0})
    language[0] = "en_US"
    with pytest.raises(SnapshotRequired):
        logs.tail({"n": 1, "since_seq": 0, "cursor": tail["next_cursor"]})


def test_register_reads_the_runtime_language_without_copying():
    import bridge_logs
    logs = collector()
    runtime = SimpleNamespace(config={"ui": {"language": "en_US"}})
    assert bridge_logs.register(SimpleNamespace(logs=logs, runtime=runtime)) is logs
    logs.put_nowait(key_record())
    assert logs.flush()["records"][0]["title"] == "Kill switch activated"
    runtime.config = {"ui": {"language": "ja_JP"}}     # reload replaces the dict
    logs.put_nowait(key_record())
    assert logs.flush()["records"][0]["title"] == "キルスイッチ保護を有効化"


def test_classified_live_row_keeps_raw_out_of_the_rendered_title():
    logs = collector(["en_US"])
    logs.put_nowait({"source": "MIHOMO", "level": "warning", "event_key": "mihomo.connection_failed",
                     "message": "dial secret.example.invalid failed", "raw_message": "x"})
    row = logs.flush()["records"][0]
    assert row["raw"] == "" and row["title"] == "Connection failed"
    assert "secret.example" not in json.dumps(row)


# ------------------------------------------------------- 3. subscription_id

def test_node_subscription_id_comes_from_the_owner_membership():
    from bridge_nodes import NodeProjection
    from tests.test_bridge_p2_06 import facts
    from bridge_runtime_access import opaque_id
    nodes, subs, life = facts(checked=1000)
    manual = deepcopy(nodes["items"][0])
    manual.update(id=opaque_id("manual"), source_id=None, source_ids=())
    nodes["items"].append(manual)
    result = NodeProjection().project(nodes, subs, life)
    by_id = {row["id"]: row for row in result["nodes"]["items"]}
    sub = subs["items"][0]
    assert by_id[nodes["items"][0]["id"]]["subscription_id"] == sub["id"]
    assert sub["check_receipt"]["member_ids"] == [nodes["items"][0]["id"]]
    assert by_id[manual["id"]]["subscription_id"] is None
    assert protocol.validate_topic_payload("nodes.list", result["nodes"]) == []


def test_shared_node_belongs_to_the_first_enabled_subscription():
    from bridge_nodes import NodeProjection
    from tests.test_bridge_p2_06 import facts
    from bridge_runtime_access import opaque_id
    nodes, subs, life = facts(checked=1000)
    first = subs["items"][0]
    first["enabled"] = False
    second = deepcopy(first)
    second.update(id=opaque_id("second"), source_id="second-source", enabled=True,
                  current_generation_evidence=False)
    subs["items"].append(second)
    nodes["items"][0]["source_ids"] += ("second-source",)
    node = NodeProjection().project(nodes, subs, life)["nodes"]["items"][0]
    assert node["subscription_id"] == second["id"]

def test_owner_missing_from_the_order_sorts_last_deterministically():
    """Review S7: an unordered owner never wins by a default position 0."""
    from bridge_nodes import _subscription_id
    by_source = {("sentinel", "a"): [{"id": "sub-z", "enabled": True}],
                 ("sentinel", "b"): [{"id": "sub-y", "enabled": True}],
                 ("sentinel", "c"): [{"id": "sub-x", "enabled": True}]}
    keys = set(by_source)
    assert _subscription_id("subscription", keys, by_source, {"sub-y": 0}) == "sub-y"
    assert _subscription_id("subscription", keys, by_source, {}) == "sub-x"       # by id
    assert _subscription_id("manual", keys, by_source, {}) is None


# ---------------------------------------------------------- 5. kill switch

def test_killswitch_triggered_is_rules_in_force():
    from bridge_killswitch import state_dto
    from killswitch import KillSwitchEvent
    armed = state_dto(KillSwitchEvent(1, "snapshot", True, False, True))
    fired = state_dto(KillSwitchEvent(2, "activate", True, True, True))
    failed = state_dto(KillSwitchEvent(3, "error", True, False, True, "io"))
    assert (armed["triggered"], fired["triggered"], failed["triggered"]) == (False, True, False)
    for data in (armed, fired, failed):
        assert data["triggered"] is data["active"]
        assert protocol.validate_topic_payload("killswitch.state", data) == []


# ------------------------------------------------------ 6. first-run language

def lang_vectors():
    document = json.loads((FIXTURES / "lang_map_vectors.json").read_bytes())
    return [(int(row["langid"], 16), row["lang"], row["expected"]) for row in document["vectors"]]


def test_shared_langid_vectors_match_the_mapping():
    from bridge_settings import language_for_langid
    from bridge_validation import LANGUAGES
    vectors = lang_vectors()
    assert {expected for _, _, expected in vectors} == LANGUAGES
    assert any(tag == "it-IT" and expected == "en_US" for _, tag, expected in vectors)
    for tag in ("zh-TW", "zh-HK"):   # U3
        assert any(t == tag and e == "zh_CN" for _, t, e in vectors)
    for langid, tag, expected in vectors:
        assert language_for_langid(langid) == expected, (hex(langid), tag)
    assert language_for_langid(-1) == language_for_langid(0x10000) == "en_US"


def test_shell_mapping_table_is_the_same():
    """native_text.rs `language`: the primary-language arms equal ours."""
    from bridge_settings import FIRST_RUN_LANGUAGES
    source = (ROOT / "shell" / "app" / "src" / "native_text.rs").read_text(encoding="utf-8")
    # The shared vectors (lang_map_vectors.json) are the contract; this is a
    # second, best-effort look at the shell source, which C3 may reformat:
    # a parse miss fails with a clear message instead of an AttributeError.
    compact = re.sub(r"\s+", "", source)
    body = re.search(r"pubfnlanguage\(lang:u16\)->&'staticstr\{matchlang&0x3ff\{(.*?)\}\}", compact, re.S)
    assert body is not None, "native_text.rs: `pub fn language(lang:u16)` match not found; compare by the shared vectors"
    arms = dict((int(code, 16), name) for code, name in re.findall(r'0x([0-9a-fA-F]+)=>"([a-z]{2}_[A-Z]{2})"', body.group(1)))
    assert arms == FIRST_RUN_LANGUAGES, "native_text.rs language arms differ from bridge_settings.FIRST_RUN_LANGUAGES"
    assert '_=>"en_US"' in body.group(1), "native_text.rs: the fallback arm is not en_US"


def app_paths(tmp_path):
    from app_paths import build_app_paths
    return build_app_paths(resource_root=ROOT, config_dir=tmp_path / "config",
                           data_dir=tmp_path / "data")


def test_first_run_writes_the_mapped_language_to_the_local_layer(tmp_path):
    from bridge_main import prepare_first_run
    from main import load_config
    paths = app_paths(tmp_path)
    assert not paths.config_path.exists()
    assert prepare_first_run(paths, language="ja_JP") is True
    base = yaml.safe_load(paths.config_path.read_text(encoding="utf-8"))
    assert base["ui"]["language"] == "en_US"                       # template untouched
    assert yaml.safe_load(paths.local_config_path.read_text(encoding="utf-8"))["ui"]["language"] == "ja_JP"
    assert load_config(str(paths.config_path), str(paths.local_config_path))["ui"]["language"] == "ja_JP"
    snapshot = {path: path.read_bytes() for path in (paths.config_path, paths.local_config_path)}
    assert prepare_first_run(paths, language="de_DE") is False      # existing config: never
    assert {path: path.read_bytes() for path in snapshot} == snapshot


def test_first_run_keeps_a_language_already_in_the_local_layer(tmp_path):
    from bridge_settings import write_first_run_language
    paths = app_paths(tmp_path)
    paths.config_dir.mkdir(parents=True)
    paths.local_config_path.write_text("ui:\n  language: ru_RU\n", encoding="utf-8")
    assert write_first_run_language(paths, "ko_KR") is False
    assert "ru_RU" in paths.local_config_path.read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        write_first_run_language(paths, "it_IT")


def test_first_run_language_uses_the_system_langid(monkeypatch):
    import bridge_settings
    monkeypatch.setattr(bridge_settings, "system_ui_langid", lambda: 0x0C04)   # zh-HK
    assert bridge_settings.first_run_language() == "zh_CN"
    monkeypatch.setattr(bridge_settings, "system_ui_langid", lambda: None)
    assert bridge_settings.first_run_language() == "en_US"
    assert bridge_settings.first_run_language(0x0416) == "pt_BR"


def test_create_runtime_calls_the_first_run_hook_before_loading(tmp_path, monkeypatch):
    """Behaviour, not source order: the hook sees the scope's paths and runs
    before the configuration is loaded (stopped right at load_config)."""
    import bridge_main
    import main
    calls = []

    class Stop(Exception):
        pass

    def load_config(config_path, local_path):
        calls.append(("load", Path(config_path)))
        raise Stop

    import app_paths
    monkeypatch.setattr(bridge_main, "prepare_first_run",
                        lambda paths: calls.append(("first_run", paths.config_path)))
    # T-P2-H2 (33-#10): an existing configuration (prepare_first_run False)
    # is bootstrapped/migrated on every start, after the first-run decision.
    monkeypatch.setattr(app_paths, "bootstrap_user_files",
                        lambda paths: calls.append(("bootstrap", paths.config_path)))
    monkeypatch.setattr(main, "load_config", load_config)
    scope = SimpleNamespace(config_dir=tmp_path / "config", data_dir=tmp_path / "data")
    gate = SimpleNamespace(require_idle=lambda: calls.append(("gate", None)))
    with pytest.raises(Stop):
        bridge_main.create_runtime(scope, gate, lambda *args: None)
    config_path = tmp_path / "config" / "config.yaml"
    assert calls == [("gate", None), ("first_run", config_path), ("bootstrap", config_path),
                     ("load", config_path)]


# ------------------------------------------------------ 7. update completed

def test_update_completed_reports_previous_and_current_once(tmp_path):
    import app_update_state
    from bridge_update import UpdateOwner
    from tests.test_app_update_state import _downloaded_update
    path = app_update_state.update_state_path(tmp_path)
    app_update_state.record_pending_update(path, _downloaded_update())
    owner = UpdateOwner(path, tmp_path / "updates", current_version="0.2.0")
    assert owner.snapshot()["data"]["previous_version"] is None
    assert owner.load_completed_update() == "0.1.0"
    data = owner.snapshot()["data"]
    assert (data["current_version"], data["previous_version"]) == ("0.2.0", "0.1.0")
    assert protocol.validate_topic_payload("update.state", data) == []
    assert owner.take_state() is not None
    again = UpdateOwner(path, tmp_path / "updates", current_version="0.2.0")
    assert again.load_completed_update() is None                    # once-only
    assert again.snapshot()["data"]["previous_version"] is None


def test_update_not_yet_installed_or_unreadable_state_has_no_previous(tmp_path):
    import app_update_state
    from bridge_update import UpdateOwner
    from tests.test_app_update_state import _downloaded_update
    path = app_update_state.update_state_path(tmp_path)
    app_update_state.record_pending_update(path, _downloaded_update())
    owner = UpdateOwner(path, tmp_path / "updates", current_version="0.1.0")
    assert owner.load_completed_update() is None
    path.write_text("{not json", encoding="utf-8")
    assert UpdateOwner(path, tmp_path / "updates", current_version="0.2.0").load_completed_update() is None


def test_update_register_loads_the_completed_record(tmp_path):
    import app_update_state
    import bridge_update
    from tests.test_app_update_state import _downloaded_update
    path = app_update_state.update_state_path(tmp_path)
    app_update_state.record_pending_update(path, _downloaded_update())
    owner = bridge_update.UpdateOwner(path, tmp_path / "updates", current_version="0.2.0")
    executor = SimpleNamespace(register=lambda *args: None)
    bridge_update.register(SimpleNamespace(executor=executor, state=None, shutdown=None), owner)
    assert owner.snapshot()["data"]["previous_version"] == "0.1.0"


# --------------------------------------------- 8. re-enable a disabled node

def test_disabled_node_reenabled_through_its_config_items_id():
    import bridge_node_commands
    import bridge_wiring
    from tests.test_bridge_p2_14 import FakePort
    port = FakePort()
    disabled = {"tier": "tier2", "kind": "node", "key": "trojan://Off@off.invalid:443#Off",
                "uri": "trojan://Off@off.invalid:443#Off", "name": "Off", "enabled": False}
    port.items.append(disabled)
    rows = bridge_wiring.config_items_projection(port.items)
    item = next(row for row in rows if row["name"] == "Off")
    assert item["enabled"] is False
    request = {"t": "req", "epoch": "p4c1", "produced_at_ms": 1, "sent_at_ms": 2, "id": 1,
               "req_id": "r-1", "owner": "page-1", "cmd": "nodes.update", "from": "page",
               "args": {"item_id": item["id"], "updates": {"enabled": True}}}
    assert protocol.validate_frame(request) == []
    commands = bridge_node_commands.NodeCommands(port)
    result = commands.nodes_update(request["args"], SimpleNamespace(op="o"))
    assert result["saved"] is True
    assert port.called("update_config_item")[0][2] == {"enabled": True}


# ----------------------------------------------------------- 9. source hint

HINT_CASES = [
    ("github_repos", {"url": "https://github.com/acme/repo"}, f"a{DOT * 3}/r{DOT * 3}"),
    ("github_repos", "acme/Repo.git", f"a{DOT * 3}/R{DOT * 3}"),
    ("github_repos", {"value": "github.com/_x/1y"}, f"{DOT}{DOT * 3}/1{DOT * 3}"),
    ("telegram_channels", "@channelname", f"@c{DOT * 3}"),
    ("telegram_channels", "https://t.me/s/News", f"@N{DOT * 3}"),
    ("telegram_channels", {"channel": "频道"}, f"@{DOT}{DOT * 3}"),
    ("subscription_urls", {"value": "https://user:pw@sub.example.invalid:8443/p?t=1"}, f"s{DOT * 3}"),
    ("subscription_urls", "https://订阅.example.invalid/x", f"{DOT}{DOT * 3}"),
    ("local_files", {"path": "C:\\Users\\user\\nodes\\list.txt"}, f"l{DOT * 3}"),
    ("local_files", "/home/user/.hidden.yaml", f"{DOT}{DOT * 3}"),
    ("local_files", {"path": "D:/data/节点.txt"}, f"{DOT}{DOT * 3}"),
]


@pytest.mark.parametrize("kind,entry,expected", HINT_CASES)
def test_source_hint_rules(kind, entry, expected):
    assert source_hint(kind, entry) == expected
    # Review fix: the frame validator pins the same mask shape.
    assert protocol._SOURCE_HINT.fullmatch(expected)
    row = {"id": "s", "kind": kind, "display": kind, "enabled": True, "hint": expected}
    errors: list = []
    protocol._schema(row, "Source", "s", errors)
    assert errors == []
    for leaked in ("ac" + DOT * 3, "acme/repo", expected + "x", "@" + expected.lstrip("@") * 2):
        errors = []
        protocol._schema(dict(row, hint=leaked), "Source", "s", errors)
        assert errors == ["invariant:s.hint"], leaked


@pytest.mark.parametrize("kind,entry", [
    ("github_repos", ""), ("github_repos", "https://gitlab.com/a/b"), ("github_repos", "onlyowner"),
    ("telegram_channels", "  "), ("subscription_urls", "not a url"), ("local_files", {"path": ""}),
    ("local_files", {"enabled": False}), ("unknown_kind", "x"),
])
def test_source_hint_is_omitted_for_empty_or_unparsable(kind, entry):
    assert source_hint(kind, entry) is None


def test_hint_never_carries_a_second_character_of_the_value():
    from bridge_redact import KnownSecrets, redact_text
    from bridge_secrets import SecretRegistry
    documents = {kind: [entry] for kind, entry, _ in HINT_CASES}
    registry = SecretRegistry(lambda: _config_secret_values({"sources": documents}))
    secrets = KnownSecrets(registry.merged(reload_base=True))
    for kind, entry, _ in HINT_CASES:
        hint = source_hint(kind, entry)
        visible = [ch for ch in hint if ch not in {DOT, "@", "/"}]
        assert len(visible) <= (2 if kind == "github_repos" else 1)
        raw = entry if isinstance(entry, str) else next(v for v in entry.values() if v)
        segments = [part for part in re.split(r"[^A-Za-z0-9]+", raw) if part]
        assert all(any(seg[0] == ch for seg in segments) for ch in visible), (kind, hint)
        assert redact_text(hint, known_secrets=secrets) == hint
        for value in registry.merged():
            assert len(value) < 2 or value[1:] not in hint


def test_hint_in_snapshot_and_bastion_sources_but_not_a_secret_field(rt):
    from bridge_sources import source_rows
    values, fields = projection(rt)
    document = rt.read_sources_config()
    rows = source_rows(document)
    assert [row.get("hint") for row in values["sources"]] == [row.get("hint") for row in rows]
    assert all(row.get("hint") for row in rows)
    assert not any("hint" in field for field in fields)
    assert protocol.validate_topic_payload("bastion.sources", {"items": rows}) == []
    # The page cannot set a hint: validated and ignored, nothing is written.
    from bridge_validation import validate_change
    from bridge_sources import backend_rows
    current = {"sources": backend_rows(document)[0]}
    submitted = [dict(row, hint="z" + DOT * 3) for row in rows]
    result = validate_change({"section": "sources", "values": {"items": submitted}}, current)
    assert result.valid and all("hint" not in row for row in result.values)
    bad = [dict(rows[0], hint="")] + rows[1:]
    assert not validate_change({"section": "sources", "values": {"items": bad}}, current).valid


# ------------------------------------------- 2. CPU budget (scheduled window)

@pytest.mark.skipif(__import__("os").environ.get("TESSERACT_BRIDGE_CPU_TEST") != "1",
                    reason="CPU measurement requires an explicitly scheduled window")
@pytest.mark.parametrize("rate", [50, 500])
@pytest.mark.parametrize("registry_size", [0, 1000])
def test_cpu_gate_every_line_a_rendered_key_event_in_en_us(rate, registry_size, monkeypatch):
    """Worst case of T-P4-C1 #2: the existing log CPU gate (test_bridge_traffic)
    with EVERY Mihomo line classified as an event whose live title/detail is
    rendered in en_US. Same budgets: <=0.02 core at 50/s, <=0.1 core at 500/s."""
    import bridge_logs
    from tests import test_bridge_traffic as gate
    real = bridge_logs.captured_record_from_text_line

    def keyed(text, source):
        captured = real(text, source=source)
        if captured is not None:
            captured["event_key"] = "mihomo.connection_failed"
        return captured

    original = bridge_logs.LogCollector.__init__

    def init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self.set_language_source(lambda: "en_US")

    monkeypatch.setattr(bridge_logs, "captured_record_from_text_line", keyed)
    monkeypatch.setattr(bridge_logs.LogCollector, "__init__", init)
    gate.test_slow_logs_per_second_and_1hz_traffic_thread_cpu(rate, registry_size, monkeypatch)


# --------------------------- 11-01: routing.rule_order (the match order of the three lists)

def test_rule_order_is_projected_complete_saved_and_compiled(rt):
    """The snapshot always carries the full order (the default when the file
    has none); the page saves a permutation as routing.rule_order without
    touching the lists, and the rules compile in that order."""
    from clash_formatter import build_custom_routing_rules
    write_local(rt, {"routing": {"rules": {"direct": ["suffix:example.cn"], "proxy": ["domain:www.example.cn"], "reject": []}}})
    rt.reload_config()
    assert projection(rt)[0]["routing"]["rule_order"] == ["direct", "proxy", "reject"]
    result = Harness(rt).apply([{"section": "routing", "values": {"rule_order": ["proxy", "reject", "direct"]}}])
    assert sections(result)["routing"]["saved"]
    local = yaml.safe_load(rt.local_path.read_text(encoding="utf-8"))
    assert local["routing"]["rule_order"] == ["proxy", "reject", "direct"]
    assert local["routing"]["rules"]["direct"] == ["suffix:example.cn"]
    rt.reload_config()
    assert projection(rt)[0]["routing"]["rule_order"] == ["proxy", "reject", "direct"]
    compiled = build_custom_routing_rules(rt.config["routing"])
    assert compiled.index("DOMAIN,www.example.cn,GENERAL") < compiled.index("DOMAIN-SUFFIX,example.cn,DIRECT")


def test_rule_order_validation_takes_only_a_full_permutation():
    from bridge_validation import validate_change, validate_field
    current = {"routing": {"rules": {}, "rule_order": ["direct", "proxy", "reject"]}}
    ok = validate_change({"section": "routing", "values": {"rule_order": ["reject", "direct", "proxy"]}}, current)
    assert ok.valid and ok.values["rule_order"] == ["reject", "direct", "proxy"]
    assert validate_field("routing", "rule_order", ["proxy", "direct", "reject"]) == {"valid": True, "errors": []}
    for bad in (["direct", "proxy"], ["direct", "direct", "reject"], ["direct", "proxy", "raw"],
                ["Direct", "proxy", "reject"], "direct", None):
        result = validate_field("routing", "rule_order", bad)
        assert result["valid"] is False and result["errors"][0]["params"]["field"] == "routing.rule_order", bad
