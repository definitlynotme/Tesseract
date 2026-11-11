"""P2-09: pure effect vectors, validation and protocol integration shapes."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

import bridge_protocol as protocol
from bridge_validation import validate_change, validate_field
from settings_effects import plan, SECTIONS


VECTORS = json.loads((Path(__file__).resolve().parents[1] / "bridge_schema" /
                      "fixtures" / "settings_effects_vectors.json").read_text(encoding="utf-8"))


def schema(name, value):
    errors = []
    protocol._schema(value, name, name, errors)
    assert errors == []


@pytest.mark.parametrize("vector", VECTORS, ids=lambda v: v["name"])
def test_shared_effect_vectors(vector):
    before, after = deepcopy(vector["before"]), deepcopy(vector["after"])
    actual = plan(before, after, running=vector["running"], phase=vector["phase"])
    expected = vector["expected"]
    for field in ("effect", "reason", "mode", "fallback_effect"):
        assert getattr(actual.core, field) == expected[field]
    assert [a.action for a in actual.actions] == expected["actions"]
    for result in actual.section_results():
        schema("SectionResult", result)
    assert before == vector["before"] and after == vector["after"]
    assert actual == plan(before, after, running=vector["running"], phase=vector["phase"])


def test_each_section_has_shared_change_vectors():
    for section in SECTIONS:
        assert sum(v["before"].get(section) != v["after"].get(section) for v in VECTORS) >= 2


def test_core_plan_intent_and_independent_action_states():
    before = {"latency": {"primary_gateway_max_latency_ms": 800}, "ui": {"language": "en_US"}}
    after = {"latency": {"primary_gateway_max_latency_ms": 500}, "ui": {"language": "zh_CN"}}
    for phase, running, state in [("stopped", False, "suppressed"), ("collecting", False, "deferred"), ("stopping", True, "suppressed"), ("running", True, "ready")]:
        result = plan(before, after, running=running, phase=phase)
        assert result.actions[0].state == state
        assert result.actions[1].state == "ready"
        if phase == "collecting":
            assert result.core.requested_effect == "recheck"
            schema("PendingApply", {"intent_id": "intent-a", "config_rev": 2,
                                    "generation": 1, "state": "pending",
                                    "effect": result.core.requested_effect, "error": None})
        assert result.core.user_commit_id is None


@pytest.mark.parametrize("section,values,current,expected", [
    ("network", {"mihomo_mixed_port": "7891"}, {"controller_port": 19090}, {"mihomo_mixed_port": 7891, "controller_port": 19090}),
    ("dns", {"respect_rules": True}, {"mode": "auto"}, {"respect_rules": True, "mode": "auto"}),
    ("routing", {"rules": {"direct": " a.test\na.test\nb.test "}}, {"rules": {"custom_extension": [1]}}, {"rules": {"direct": ["a.test", "b.test"], "custom_extension": [1]}}),
    ("latency", {"primary_gateway_max_latency_ms": "500"}, {}, {"primary_gateway_max_latency_ms": 500}),
    ("ui", {"language": "zh_CN"}, {"theme": "legacy-theme"}, {"language": "zh_CN", "theme": "legacy-theme"}),
    ("autostart", {"enabled": True}, {}, {"enabled": True}),
    ("traffic", {"history_seconds": "3600"}, {"extension": 9}, {"history_seconds": 3600, "extension": 9}),
    ("killswitch", {"additional_whitelist": ["127.0.0.1"]}, {"enabled": True}, {"enabled": True, "additional_whitelist": ["127.0.0.1"]}),
])
def test_normalization_preservation_and_purity(section, values, current, expected):
    change = {"section": section, "values": values}
    snapshot = {section: current}
    copy_change, copy_current = deepcopy(change), deepcopy(snapshot)
    result = validate_change(change, snapshot)
    assert result.valid, result.errors
    assert result.values == expected
    assert change == copy_change and snapshot == copy_current
    schema("ConfigChange", change)


def test_hidden_node_judgement_preserved_and_uneditable():
    hidden = {"enabled": True, "post_dial_judge": "shadow", "observer_cpu_budget_pct": 2}
    current = {"network": {"egress_recovery": {"node_judgement": hidden, "enabled": True},
                           "mihomo_controller": {"port": 19909, "secret": "fixture-only"}},
               "ui": {"theme": "custom-retained"}}
    result = validate_change({"section": "network", "values": {"egress_recovery": {"enabled": False}}}, current)
    assert result.valid
    assert result.values["egress_recovery"]["node_judgement"] == hidden
    assert result.values["mihomo_controller"] == current["network"]["mihomo_controller"]
    assert "network.egress_recovery.node_judgement.post_dial_judge" in result.retained_fields
    for section, values in [("ui", {"theme": "dark"}), ("network", {"controller_port": 9090}),
                            ("network", {"egress_recovery": {"node_judgement": {"enabled": False}}})]:
        bad = validate_change({"section": section, "values": values}, current)
        assert not bad.valid and bad.values is None
        assert all("field" in e["params"] for e in bad.errors)


def _alert_current():
    return {"alerts": {"notify": {"enabled": True, "events": ["connection"], "sound": True}}}


@pytest.mark.parametrize("edit", [
    {"field": "bot_token", "target_id": "telegram-a", "action": "set", "value": "******"},
    {"field": "bot_token", "target_id": "missing", "action": "clear"},
    {"field": "config.bot_token", "target_id": "telegram-a", "action": "clear"},
    {"field": "bot_token", "target_id": "telegram-a", "action": "keep", "value": "bad"},
    {"field": "bot_token", "target_id": "telegram-a", "action": "set"},
])
def test_invalid_secret_edits_are_not_writable(edit):
    result = validate_change({"section": "alerts", "values": {}, "secrets": [edit]}, _alert_current())
    assert not result.valid and result.values is None and result.secret_edits == []
    for error in result.errors:
        schema("Error", error)
        assert error["params"]["field"].startswith("alerts.secrets[0]")


def _source_current():
    return {"sources": [{"id": "repo-a", "kind": "github_repos", "enabled": True,
        "value": "https://github.com/old/repo", "fetch_profile": "retained-profile",
        "fetch_paths": [{"kind": "github_api", "unit": "subscription"}],
        "github_delivery": {"hedge_seconds": 0.2}, "extension": {"x": 1}}]}


def test_source_url_owner_repo_conversion_preserves_additional_keys():
    current = _source_current()
    first = validate_change({"section": "sources", "values": {}, "secrets": [
        {"field": "value", "target_id": "repo-a", "action": "set", "value": "next/repo"}]}, current)
    assert first.valid, first.errors
    assert first.values[0]["value"] == "https://github.com/next/repo"
    second = validate_change({"section": "sources", "values": {}, "secrets": [
        {"field": "url", "target_id": "repo-a", "action": "set", "value": "old/repo"}]}, {"sources": first.values})
    assert second.valid, second.errors
    assert second.values == current["sources"]
    assert second.secret_edits[0].path == (0, "value")
    for key in ("fetch_profile", "fetch_paths", "github_delivery", "extension"):
        assert first.values[0][key] == current["sources"][0][key]


def test_source_mask_and_reordering_keep_identity_bound_extras():
    current = _source_current()
    current["sources"].append({"id": "sub-b", "kind": "subscription_urls", "value": "https://example.test/sub", "extension": 42})
    result = validate_change({"section": "sources", "values": {"items": [
        {"id": "sub-b", "value": "已设置"}, {"id": "repo-a", "url": "***"}]}}, current)
    assert not result.valid and result.values is None
    assert result.errors[0]["params"]["field"] == "sources.items[0].value"
    # Reordering without credential submissions still retains all private extras.
    result = validate_change({"section": "sources", "values": {"items": [
        {"id": "sub-b"}, {"id": "repo-a"}]}}, current)
    assert result.valid, result.errors
    assert result.values == list(reversed(current["sources"]))


@pytest.mark.parametrize("section,field,value", [
    ("network", "mihomo_mixed_port", True), ("network", "subscription_port", 0),
    ("network", "egress_recovery.node_judgement", {}), ("ui", "theme", "dark"),
    ("dns", "mode", "unknown"), ("dns", "default_nameserver", ["dns.example.test"]),
    ("latency", "primary_gateway_max_latency_ms", 99), ("latency", "primary_gateway_max_latency_ms", True),
    ("routing", "rules", {"unknown": []}), ("traffic", "history_seconds", -1),
    ("killswitch", "dns_whitelist", ["example.test"]), ("autostart", "enabled", "true"),
    ("alerts", "notify.events", ["sms"]), ("sources", "items.owner", "a/b"),
])
def test_single_field_errors_always_locate_a_field_and_match_schema(section, field, value):
    result = validate_field(section, field, value)
    assert result["valid"] is False
    assert result["errors"][0]["params"]["field"] == f"{section}.{field}"
    schema("Error", result["errors"][0])
    frame = {"t": "res", "epoch": "test", "produced_at_ms": 1, "sent_at_ms": 1,
             "id": 1, "req_id": "req-a", "owner": "window-a", "cmd": "settings.validate", "ok": True, "data": result}
    assert protocol.validate_frame(frame) == []


def test_multiple_field_errors_and_required_custom_dns_fields():
    result = validate_change({"section": "network", "values": {"mihomo_mixed_port": 0, "subscription_port": False}}, {})
    assert {e["params"]["field"] for e in result.errors} == {"network.mihomo_mixed_port", "network.subscription_port"}
    dns = validate_change({"section": "dns", "values": {"mode": "custom"}}, {"dns": {"nameserver": [], "proxy_server_nameserver": []}})
    assert {e["params"]["field"] for e in dns.errors} == {"dns.nameserver", "dns.proxy_server_nameserver"}


def test_new_secret_source_and_protocol_private_input_contract():
    change = {"section": "sources", "values": {"items": [{"id": "new", "kind": "subscription_urls", "display": "fixture", "enabled": True}]},
              "secrets": [{"field": "url", "target_id": "new", "action": "set", "value": "https://example.test/sub"}]}
    result = validate_change(change, {})
    assert result.valid, result.errors
    assert result.retained_fields == []
    schema("ConfigValues", {"sources": [{"id": "new", "kind": "subscription_urls", "display": "fixture", "enabled": True}]})
    frame = {"t": "req", "epoch": "test", "produced_at_ms": 1, "sent_at_ms": 1, "id": 1,
             "req_id": "req-a", "owner": "window-a", "cmd": "settings.apply", "from": "page",
             "args": {"base_rev": 1, "changes": [change]}}
    assert protocol.validate_frame(frame) == []


def test_save_result_templates_pass_contract_without_claiming_success():
    p = plan({}, {"ui": {"language": "zh_CN"}}, running=False, phase="stopped")
    schema("SaveResult", {"rev": 2, "sections": p.section_results(), "pending": None})
    assert all(not r["saved"] and not r["applied"] for r in p.section_results())


@pytest.mark.parametrize("section,values,expected_field", [
    ("alerts", {"notify": {"events": ["sms"]}}, "alerts.notify.events"),
    ("alerts", {"channels": []}, "alerts.channels"),
    ("sources", {"items": [{"id": "a", "kind": "subscription_urls"}]}, "sources.items[0].value"),
    ("sources", {"items": [{"id": "a", "kind": "subscription_urls"}, {"id": "a", "kind": "subscription_urls"}]}, "sources.items[1].id"),
    ("ui", {"node_judgement": {"enabled": True}}, "ui.node_judgement"),
])
def test_row_and_public_validator_errors_have_specific_fields(section, values, expected_field):
    result = validate_change({"section": section, "values": values}, {})
    assert not result.valid and result.values is None
    assert result.errors[0]["params"]["field"] == expected_field
    schema("Error", result.errors[0])


def test_duplicate_secret_or_plain_secret_never_becomes_write_material():
    edit = {"field": "bot_token", "target_id": "telegram-a", "action": "set", "value": "fixture-new"}
    for change in [
        {"section": "alerts", "values": {}, "secrets": [edit, edit]},
        {"section": "alerts", "values": {"channels": [{"id": "telegram-a", "config": {"bot_token": "plain-value"}}]}},
    ]:
        result = validate_change(change, _alert_current())
        assert not result.valid and result.values is None and result.secret_edits == []


def test_source_secret_http_validation_and_retained_external_delivery_url():
    for value in ("ftp://github.com/example/repo", "invalid", "已设置"):
        result = validate_change({"section": "sources", "values": {}, "secrets": [
            {"field": "url", "target_id": "repo-a", "action": "set", "value": value}]}, _source_current())
        assert not result.valid
        assert result.errors[0]["params"]["field"] == "sources.secrets[0].value"
    current = {"sources": [{"id": "repo-a", "kind": "github_repos", "url": "https://example.test/feed", "extension": 3}]}
    result = validate_change({"section": "sources", "values": {"items": [{"id": "repo-a", "enabled": False}]}}, current)
    assert result.valid, result.errors
    assert result.values[0]["url"] == current["sources"][0]["url"]
    assert "value" not in result.values[0]


def test_all_public_field_successes_pass_response_schema():
    for section, field, value in [
        ("network", "tun.enabled", True), ("network", "system_proxy.bypass", ["localhost", "10.*"]),
        ("dns", "default_nameserver", ["1.1.1.1"]), ("sources", "items.github_repos.value", "example/repo"),
        ("alerts", "notify.enabled", False), ("alerts", "notify.events", ["connection", "update"]),
        ("alerts", "notify.sound", True),
        ("sources", "items.enabled", True), ("sources", "items.kind", "github_repos"),
        ("routing", "rules.direct", ["example.test"]),
    ]:
        result = validate_field(section, field, value)
        assert result == {"valid": True, "errors": []}


def test_source_additional_keys_are_not_editable_and_masks_with_secret_set_fail():
    change = {"section": "sources", "values": {"items": [{"id": "repo-a", "fetch_profile": "replacement"}]}}
    assert not validate_change(change, _source_current()).valid
    change = {"section": "sources", "values": {}, "secrets": [{"target_id": "repo-a", "field": "url", "action": "set", "value": "***"}]}
    assert not validate_change(change, _source_current()).valid


def test_effect_module_has_no_qt_bridge_or_runtime_side_effect_imports():
    import ast
    import settings_effects
    tree = ast.parse(Path(settings_effects.__file__).read_text(encoding="utf-8"))
    names = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    assert set(names) <= {"__future__", "dataclasses", "typing", "runtime_reload_gate"}


def test_new_wire_files_use_lf_only():
    root = Path(__file__).resolve().parents[1]
    for name in ("settings_effects.py", "bridge_validation.py", "tests/test_bridge_p2_09.py", "bridge_schema/fixtures/settings_effects_vectors.json"):
        assert b"\r\n" not in (root / name).read_bytes()


@pytest.mark.parametrize("kind,good,bad", [
    ("github_repos", "example/repo", "ftp://github.com/example/repo"),
    ("subscription_urls", "https://example.test/sub", "not-a-url"),
    ("telegram_channels", "example_channel", ""),
    ("local_files", r"C:\fixtures\nodes.txt", 123),
])
@pytest.mark.parametrize("name", ["value", "url"])
def test_source_value_field_matches_whole_section_for_each_kind(kind, good, bad, name):
    for value, valid in [(good, True), (bad, False), ("***", False)]:
        field = f"items.{kind}.{name}"
        response = validate_field("sources", field, value)
        change = {"section": "sources", "values": {"items": [{"id": "source-a", "kind": kind}]},
                  "secrets": [{"field": name, "target_id": "source-a", "action": "set", "value": value}]}
        result = validate_change(change, {})
        assert response["valid"] == result.valid == valid
        for error in response["errors"] + result.errors:
            schema("Error", error)
        if valid:
            expected = "https://github.com/example/repo" if kind == "github_repos" else good
            assert result.values[0]["value"] == expected
        else:
            assert response["errors"][0]["params"]["field"] == f"sources.{field}"


@pytest.mark.parametrize("field", ["items.value", "items.url", "items.unknown.value",
    "items.local_files.owner", "items.telegram_channels.repo"])
def test_ambiguous_or_wrong_kind_source_field_is_rejected(field):
    assert not validate_field("sources", field, "example/repo")["valid"]


@pytest.mark.parametrize("kind", ["github_repos", "subscription_urls", "telegram_channels", "local_files"])
def test_source_secrets_preserve_and_explicitly_clear_disabled_rows(kind):
    value = "https://github.com/example/repo" if kind == "github_repos" else "https://example.test/sub" if kind == "subscription_urls" else "fixture-channel-or-path"
    current = {"sources": [{"id": "fixture-source", "kind": kind, "value": value, "enabled": False}]}
    retained = validate_change({"section": "sources", "values": {"items": [{"id": "fixture-source"}]}}, current)
    assert retained.valid and retained.values == current["sources"]
    edit = {"field": "value", "target_id": "fixture-source", "action": "clear"}
    result = validate_change({"section": "sources", "values": {}, "secrets": [edit]}, current)
    assert result.valid, result.errors
    assert result.values[0]["value"] == ""
    assert validate_field("sources", f"items.{kind}.value", edit)["valid"]



def test_disabled_cleared_source_is_retained_but_cannot_be_enabled():
    current = {"sources": [{"id": "fixture-source", "kind": "subscription_urls", "value": "", "enabled": False}]}
    result = validate_change({"section": "sources", "values": {"items": [{"id": "fixture-source", "display": "fixture"}]}}, current)
    assert result.valid, result.errors
    assert result.values[0]["value"] == ""
    invalid = validate_change({"section": "sources", "values": {"items": [{"id": "fixture-source", "enabled": True}]}}, current)
    assert not invalid.valid and invalid.values is None


@pytest.mark.parametrize("submitted", [
    {"owner": "replacement", "repo": "repo"}, {"owner": "replacement"},
    {"repo": "replacement"}, {"owner": "***", "repo": "repo"},
])
def test_source_owner_repo_cannot_bypass_secret_edit(submitted):
    current = _source_current()
    original = deepcopy(current)
    result = validate_change({"section": "sources", "values": {"items": [
        {"id": "repo-a", **submitted}]}}, current)
    assert not result.valid and result.values is None and not result.secret_edits
    key = next(iter(submitted))
    assert result.errors[0]["params"]["field"] == f"sources.items[0].{key}"
    for key, value in submitted.items():
        for field in (f"items.{key}", f"items.github_repos.{key}"):
            assert not validate_field("sources", field, value)["valid"]
    assert current == original
    retained = validate_change({"section": "sources", "values": {"items": [
        {"id": "repo-a", "display": "changed"}]}}, current)
    assert retained.valid, retained.errors
    assert retained.values[0]["value"] == original["sources"][0]["value"]
    assert "sources.items[0].value" in retained.retained_fields
    edit = {"field": "value", "target_id": "repo-a", "action": "set", "value": "replacement/repo"}
    explicit = validate_change({"section": "sources", "values": {}, "secrets": [edit]}, current)
    assert explicit.valid and validate_field("sources", "items.github_repos.value", edit)["valid"]
    assert explicit.values[0]["value"] == "https://github.com/replacement/repo"
    assert explicit.secret_edits[0].path == (0, "value")
    for key in ("fetch_profile", "fetch_paths", "github_delivery", "extension"):
        assert explicit.values[0][key] == original["sources"][0][key]
