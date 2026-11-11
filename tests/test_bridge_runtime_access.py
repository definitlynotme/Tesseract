from pathlib import Path
import ast
import inspect

from types import SimpleNamespace

import pytest

from bridge_runtime_access import (CAPABILITIES, RuntimeAccess, cached_process_admin,
                                   restart_result, start_result, stop_result)


def test_capability_allowlist():
    assert CAPABILITIES == {
        "start", "request_stop", "stop", "restart_core", "lifecycle_status",
        "core_fact", "connection_info", "capabilities", "published_route_status", "tiers_summary_status",
    }
    assert {name for name, value in vars(RuntimeAccess).items()
            if not name.startswith("_") and callable(value)} == CAPABILITIES | {"known_secret_values"}
    assert list(inspect.signature(RuntimeAccess.request_stop).parameters) == [
        "self", "expected_generation"]
    assert list(inspect.signature(RuntimeAccess.stop).parameters) == [
        "self", "stop_request"]


def test_published_route_access_forwards_only_public_getter():
    expected = {"publication_revision": 1}

    class Runtime:
        def published_route_status(self):
            return expected

    assert RuntimeAccess(Runtime()).published_route_status() is expected


def test_result_adapters_keep_partial_outcomes():
    state = {"runtime": {"startup_phase": "stopping",
                         "shutdown_cleanup_pending": True},
             "core": {"cleanup_pending": False}}
    assert stop_result({}, state) == {
        "startup_phase": "stopping", "shutdown_exit_code": None,
        "cleanup_pending": True, "stop_skipped": False,
        "skipped_reason": ""}
    assert stop_result({"stop_skipped": True,
                        "stop_skipped_reason": "stale_stop_intent"}, state)[
        "skipped_reason"] == "stale_stop_intent"
    before = {"core_generation": 2}
    after = {"running": True, "cleanup_pending": False, "core_generation": 3}
    assert restart_result({}, before, after)["applied"]
    assert not restart_result({"applied": False}, before, after)["applied"]
    assert not restart_result({}, before, {**after, "core_generation": 2})["applied"]
    with pytest.raises(ValueError):
        stop_result({"shutdown_exit_code": -1}, state)
    assert stop_result({"shutdown_cleanup_pending": True}, {
        "runtime": {"startup_phase": "stopped", "shutdown_cleanup_pending": False},
        "core": {"cleanup_pending": False}})["cleanup_pending"]
    assert start_result({"start_blocked_by_cleanup": True,
                         "start_blocked_reason": "shutdown_cleanup_pending"}, {
        "runtime": {"startup_phase": "stopped", "failure_code": None}}) == {
        "startup_phase": "failed", "failure_code": "internal"}


def test_stop_boundary_retains_exact_intent():
    intent = SimpleNamespace(accepted=True, generation=3, lifecycle_revision=7,
                             skipped_reason="")

    class Runtime:
        def request_stop(self, **kwargs):
            assert kwargs == {"expected_generation": 3}
            return intent

        def stop(self, *, stop_intent):
            assert stop_intent is intent
            return {"shutdown_exit_code": 0}

        def lifecycle_status(self):
            return {"intent": {}, "runtime": {"startup_phase": "stopped",
                    "shutdown_cleanup_pending": False},
                    "core": {"cleanup_pending": False}, "readiness": {},
                    "freshness": {}}

    access = RuntimeAccess(Runtime())
    request = access.request_stop(expected_generation=3)
    assert request.wire["accepted"]
    with pytest.raises(TypeError):
        request.wire["accepted"] = False
    assert access.stop(stop_request=request)["shutdown_exit_code"] == 0


def private_runtime_accesses(source):
    tree = ast.parse(source)
    aliases = {"runtime"}
    def is_runtime(node):
        return ((isinstance(node, ast.Name) and node.id in aliases) or
                (isinstance(node, ast.Attribute) and node.attr == "runtime" and
                 isinstance(node.value, ast.Name) and node.value.id == "self"))
    for _ in range(3):
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and is_runtime(node.value):
                aliases.update(target.id for target in node.targets
                               if isinstance(target, ast.Name))
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            if is_runtime(node.value):
                hits.append(node.lineno)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and
                node.func.id in {"getattr", "setattr", "delattr"} and node.args and
                is_runtime(node.args[0])):
            hits.append(node.lineno)
    return hits


def test_bridge_modules_have_no_private_runtime_access():
    root = Path(__file__).resolve().parents[1]
    for path in root.glob("bridge_*.py"):
        assert private_runtime_accesses(path.read_text(encoding="utf-8")) == [], path


def test_private_access_check_catches_aliases_and_dynamic_calls():
    assert private_runtime_accesses("alias = runtime\ncopy = alias\ncopy._hidden()") == [3]
    assert private_runtime_accesses("alias = self.runtime\ngetattr(alias, name)") == [2]
    assert private_runtime_accesses("runtime.lifecycle_status()") == []


def test_connection_info_admin_cache_and_start_boundary():
    cached_process_admin.cache_clear()
    class Runtime:
        checks = 0
        def connection_info(self):
            return {"mixed_port": 7890}
        def capabilities(self):
            self.checks += 1
            return {"is_admin": True}
        def start(self):
            return {"startup_phase": "failed", "last_error": "no_sources"}
        def lifecycle_status(self):
            return {"intent": {}, "runtime": {"startup_phase": "collecting",
                    "failure_code": None}, "core": {}, "readiness": {}, "freshness": {}}
    runtime = Runtime()
    access = RuntimeAccess(runtime)
    assert access.connection_info() == {"mixed_port": 7890}
    assert access.capabilities() == {"is_admin": True}
    assert access.start() == {"startup_phase": "failed", "failure_code": "no_sources"}
    from desktop_runtime import DesktopRuntime
    instance = DesktopRuntime.__new__(DesktopRuntime)
    calls = []
    def check_admin():
        calls.append(True)
        return True
    instance._process_is_admin = check_admin
    assert DesktopRuntime.capabilities(instance)["is_admin"]
    assert DesktopRuntime.capabilities(instance)["is_admin"]
    assert len(calls) == 1
    cached_process_admin.cache_clear()


def test_known_secret_values_owned_config_copy_and_not_wire_capability():
    from copy import deepcopy
    from alert_config import SUPPORTED_ALERT_CHANNEL_TYPES
    config = {"alerts": {"channels": [
        {"type": name, "config": {"token": "SyntheticChannel" + name,
         "headers": {"X-Custom-Auth": "SyntheticHeader" + name}}}
        for name in SUPPORTED_ALERT_CHANNEL_TYPES]},
        "tiers": {"tier1": {"nodes": [{"password": "SyntheticNodePassword", "uuid": "SyntheticUuid",
                                      "private-key": "SyntheticPrivateKey"},
                                     "trojan://SyntheticUriPassword@example.invalid:443"]},
                  "tier2": {"subscription_urls": ["https://example.invalid/private-sub",
                                                   {"url": "https://example.invalid/another-sub"}]}}}
    original = deepcopy(config)
    access = RuntimeAccess(SimpleNamespace(config=config))
    values = access.known_secret_values()
    assert config == original
    assert values["SyntheticNodePassword"] == "password"
    assert values["SyntheticUriPassword"] == "password"
    assert values["SyntheticUuid"] == "uuid"
    assert values["SyntheticPrivateKey"] == "private_key"
    assert values["https://example.invalid/private-sub"] == "subscription"
    assert values["https://example.invalid/another-sub"] == "subscription"
    for name in SUPPORTED_ALERT_CHANNEL_TYPES:
        assert values["SyntheticChannel" + name] == "token"
        assert values["SyntheticHeader" + name] == "authorization"
    values.clear()
    assert access.known_secret_values()
    config["alerts"]["telegram"] = {"bot_token": "NewSyntheticBotValue"}
    assert "NewSyntheticBotValue" in access.known_secret_values()
    assert "known_secret_values" not in CAPABILITIES


@pytest.mark.parametrize("shape", ["legacy", "channels"])
def test_channel_used_secret_forms_match_sender_whitespace_semantics(shape):
    from bridge_runtime_access import _ALERT_STRIPPED_FIELDS
    from bridge_redact import KnownSecrets, redact_text
    from alert_config import SUPPORTED_ALERT_CHANNEL_TYPES
    assert set(_ALERT_STRIPPED_FIELDS) == set(SUPPORTED_ALERT_CHANNEL_TYPES)
    configs = {}
    expected = []
    for channel, fields in _ALERT_STRIPPED_FIELDS.items():
        config = {}
        for field in fields:
            # Camel-free aliases are understood by the extractor; actual field
            # spellings are checked in the dedicated cases below.
            spelling = {"bottoken": "bot_token", "accesstoken": "access_token",
                        "webhookurl": "webhook_url", "apptoken": "app_token",
                        "userkey": "user_key", "apikey": "api_key",
                        "accountsid": "account_sid", "authtoken": "auth_token",
                        "messagingservicesid": "messaging_service_sid", "apiurl": "api_url"}.get(field, field)
            raw = "  Synthetic" + channel + field + "  "
            config[spelling] = raw
            expected.append((raw, raw.strip()))
        configs[channel] = config
    configs["email_smtp"]["password"] = "  SyntheticSmtpRaw  "
    configs["generic_webhook"]["headers"] = {"X-Private": "  SyntheticHeaderRaw  ", "X-Numeric": 12345678}
    alerts = configs if shape == "legacy" else {"channels": [
        {"type": channel, "config": config} for channel, config in configs.items()]}
    values = RuntimeAccess(SimpleNamespace(config={"alerts": alerts})).known_secret_values()
    for raw, effective in expected:
        assert raw in values and effective in values
        assert effective not in redact_text(effective, known_secrets=KnownSecrets(values))
    assert "  SyntheticSmtpRaw  " in values and "SyntheticSmtpRaw" not in values
    assert "  SyntheticHeaderRaw  " in values and "SyntheticHeaderRaw" not in values
    assert "12345678" in values
    # Node passwords retain their original whitespace semantics.
    nodes = RuntimeAccess(SimpleNamespace(config={"nodes": [{"password": "  SyntheticNodeRaw  "}]}))
    assert nodes.known_secret_values() == {"  SyntheticNodeRaw  ": "password"}


def test_strip_alias_is_registered_even_when_shorter_than_old_six_char_floor():
    from bridge_redact import KnownSecrets, redact_text
    access = RuntimeAccess(SimpleNamespace(config={"alerts": {"gotify": {"app_token": "  p1234  "}}}))
    values = access.known_secret_values()
    assert values == {"  p1234  ": "token", "p1234": "token"}
    assert redact_text("p1234", known_secrets=KnownSecrets(values)) == "[redacted:token]"
