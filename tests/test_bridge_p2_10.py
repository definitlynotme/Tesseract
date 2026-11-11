"""T-P2-10: unified settings save orchestration (settings.apply / validate).

Stand-in runtime only: real DesktopRuntime methods for the local write,
source batch, latency recheck split and config revision run against a
temporary config directory (real ConfigStore, real load_config merge).
Core, registry, alerts and the system proxy are recording fakes; no live
profile, no network, no HKCU write, no firewall.
"""
from __future__ import annotations

from copy import deepcopy
import json
import logging
from pathlib import Path
import re
import shutil
import threading
import time
from types import SimpleNamespace

import pytest
import yaml

import bridge_commands as commands
import bridge_protocol as protocol
import bridge_settings
from bridge_jobs import JobEventRecorder, JobExecutor, JobFailure
from bridge_runtime_access import config_query_owners
from bridge_settings import (RuntimeSettingsPort, SettingsService, TRAY_KEY,
                             backend_sections, write_section)
from bridge_sources import RuntimeSourcePort, SourceSectionSaver, source_rows
from bridge_state import PublicationState
from config_store import ConfigStore
from config_validation import validate_local_config
from desktop_runtime import DesktopRuntime
from desktop_runtime_preferences import apply_ui_preferences_to_payload, normalized_ui_preferences
from import_service import ImportService
from main import load_config
from runtime_reload_gate import RuntimeReloadReason


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "bridge_schema/fixtures/v1/valid"
GRAPH = RuntimeReloadReason.USER_GRAPH_CONFIG.value
DNS = RuntimeReloadReason.DNS_LIFECYCLE.value
NODE_JUDGEMENT = {"enabled": True, "post_dial_judge": "shadow", "observer_cpu_budget_pct": 2}
BOT_TOKEN = "123456789:AAfixtureTokenValue_abcdefghijklmnop"
SUB_URL = "https://example.test/fixture-sub"
REPO_URL = "https://github.com/fixture/repo"


class CoreRunner:
    def __init__(self):
        self.running = True

    def is_running(self):
        return self.running


class Manager:
    def __init__(self, events):
        self.events = events
        self.lock = threading.RLock()

    def check_exit(self, force_remote_refresh=False):
        self.events.append("check_exit")
        return []

    def check_tier1(self):
        self.events.append("check_tier1")
        return []

    def check_tier2(self, force_remote_refresh=False):
        self.events.append("check_tier2")
        return []

    def evaluate_state(self):
        self.events.append("evaluate_state")


class Alerts:
    def __init__(self, events):
        self.events = events

    def set_language(self, language):
        self.events.append(("set_language", language))


class FakeRuntime:
    """Only the attributes the borrowed DesktopRuntime methods and the port use."""

    update_local_config = DesktopRuntime.update_local_config
    read_local_config = DesktopRuntime.read_local_config
    write_ui_preferences = DesktopRuntime.write_ui_preferences
    read_sources_config = DesktopRuntime.read_sources_config
    apply_source_batch = DesktopRuntime.apply_source_batch
    _reload_after = DesktopRuntime._reload_after
    apply_latency_policy_settings = DesktopRuntime.apply_latency_policy_settings
    recheck_latency_policy = DesktopRuntime.recheck_latency_policy
    _recheck_latency_policy_into = DesktopRuntime._recheck_latency_policy_into
    # T-P2-RT2 split the recheck into probe/reload halves (same behavior).
    _latency_recheck_response = DesktopRuntime._latency_recheck_response
    _latency_recheck_probe_into = DesktopRuntime._latency_recheck_probe_into
    _latency_recheck_reload_into = DesktopRuntime._latency_recheck_reload_into
    _testing_block = DesktopRuntime._testing_block
    _configured_ui_language = DesktopRuntime._configured_ui_language
    bridge_config_revision = DesktopRuntime.bridge_config_revision
    bridge_commit_config_revision = DesktopRuntime.bridge_commit_config_revision
    _disable_external_desktop_notifications = DesktopRuntime._disable_external_desktop_notifications
    _settings_apply_latency_recheck = DesktopRuntime._settings_apply_latency_recheck

    def __init__(self, root: Path):
        self.root = root
        self.base_path = root / "config.yaml"
        shutil.copyfile(ROOT / "config.example.yaml", self.base_path)
        self.local_path = root / "config.local.yaml"
        self.sources_path = root / "sources.yaml"
        self.sources_path.write_text(yaml.safe_dump({
            "migrations": {"sources_v2": True},
            "fetch_profiles": {"mirror": [{"kind": "github_api"}]},
            "github_delivery": {"hedge_seconds": 0.2},
            "github_repos": [{"url": REPO_URL, "fetch_profile": "mirror",
                              "github_delivery": {"hedge_seconds": 0.5}, "extension": {"x": 1}}],
            "subscription_urls": [{"value": SUB_URL, "enabled": True, "note": "keep-me"}],
            "telegram_channels": ["@fixture_channel"],
        }, sort_keys=False), encoding="utf-8")
        self.config_store = ConfigStore(local_config_path=self.local_path,
                                        sources_path=self.sources_path,
                                        backup_dir=root / "backups")
        self.config_store.write_local({
            "client": {"mihomo_controller": {"host": "127.0.0.1", "port": 19090},
                       "egress_recovery": {"enabled": True, "node_judgement": deepcopy(NODE_JUDGEMENT)}},
            "ui": {"language": "en_US", "theme": "custom-retained"},
            # The shipped default is off; these tests switch it off as their change.
            "killswitch": {"enabled": True},
        })
        self.import_service = ImportService(self.config_store)
        self._config_change_lock = threading.RLock()
        self._runtime_intent_lock = threading.RLock()
        self.logger = logging.getLogger("test_bridge_p2_10")
        self.events: list = []
        self.local_writes = 0
        self.reloads: list = []
        self.reload_error: Exception | None = None
        self.write_failure = None
        self.core_runner = CoreRunner()
        self.manager = Manager(self.events)
        self.alerts = Alerts(self.events)
        self.phase = "running"
        self.core_generation = 1
        self.core_reload_ok = True
        self.restart_ok = True
        self.autostart = {"enabled": False}
        self.intent: dict = {}
        self.lifecycle_error: Exception | None = None
        self.config = self._disable_external_desktop_notifications(
            load_config(str(self.base_path), local_path=str(self.local_path)))
        original = self.config_store.write_local

        def counted(payload):
            if self.write_failure is not None and self.write_failure(payload):
                raise OSError("disk full")
            self.local_writes += 1
            self.events.append("write_local")
            return original(payload)
        self.config_store.write_local = counted

    # reload_config: the real merge of base + local, counted, with the real
    # desktop-notification injection. Its own language sync is recorded under
    # a separate tag so a test can tell it from the orchestrator's sync.
    def reload_config(self, *, target_tier=None):
        self.reloads.append(target_tier)
        if self.reload_error is not None:
            raise self.reload_error
        self.config = self._disable_external_desktop_notifications(
            load_config(str(self.base_path), local_path=str(self.local_path)))
        self.events.append(("reload_set_language", self._configured_ui_language()))
        return {}

    def bridge_config_facts(self):
        return {"config": deepcopy(self.config), "sources": self.read_sources_config(),
                "read_errors": [], "stale": False}

    def bridge_autostart_state(self):
        return {"values": {"enabled": self.autostart["enabled"]}, "read_errors": [], "stale": False}

    def set_autostart(self, enabled):
        self.events.append(("set_autostart", enabled))
        self.autostart["enabled"] = enabled
        return {"supported": True, "enabled": enabled, "command": "x", "error": None}

    def lifecycle_status(self):
        if self.lifecycle_error is not None:
            raise self.lifecycle_error
        return {"intent": dict(self.intent), "runtime": {"startup_phase": self.phase}}

    def core_fact(self):
        return {"running": self.core_runner.running, "cleanup_pending": False,
                "core_generation": self.core_generation}

    def restart_core(self):
        self.events.append("restart_core")
        if self.restart_ok:
            self.core_generation += 1
        return {}

    def reload_core_config(self, *, mode="strict", reason=None, user_commit_id=""):
        self.events.append(("reload_core", mode, getattr(reason, "value", reason), user_commit_id))
        return {"reloaded": self.core_reload_ok}

    def refresh_dns_resolver_policy(self, reason="settings_changed"):
        self.events.append(("dns_refresh", reason))
        return {}

    def apply_system_proxy_override(self):
        self.events.append("proxy_override")
        return {"applied": False, "reason": "inactive"}

    def delete_alert_delivery_records(self, channel_id):
        self.events.append(("delete_records", channel_id))
        return 0


class Coordinator:
    """P2-11 surface: request() returns the PendingApply fact."""

    def __init__(self, generation=1):
        self.calls = []
        self.generation = generation

    def request(self, plan, config_rev, user_commit_id):
        self.calls.append((plan, config_rev, user_commit_id))
        core = getattr(plan, "core", None)
        if core is None or core.effect != "deferred":
            return None
        return {"intent_id": f"intent-{len(self.calls)}", "config_rev": config_rev,
                "generation": self.generation, "state": "pending",
                "effect": core.requested_effect, "error": None}


class Harness:
    def __init__(self, rt, *, coordinator=None, gate_error=None):
        self.rt = rt
        self.state = PublicationState("p2-10")
        self.edges: list = []
        self.registered: list = []
        self.order: list = rt.events
        self.coordinator = coordinator
        self.gate_error = gate_error
        self.network_lock = threading.RLock()

        def record(topic, dto):
            self.state.record_edge(topic, dto)  # Real topic validation.
            self.edges.append((topic, deepcopy(dto)))

        def register_secrets(values):
            self.registered.append(dict(values))
            self.order.append("register_secrets")

        def gate():
            if self.gate_error is not None:
                raise self.gate_error

        self.service = SettingsService(
            RuntimeSettingsPort(rt), sources=SourceSectionSaver(RuntimeSourcePort(rt)),
            coordinator=lambda: self.coordinator, register_secrets=register_secrets,
            refresh_secrets=lambda: self.order.append("refresh_secrets"),
            recovery_gate=gate, record_edge=record)

    def apply(self, changes, base_rev=None):
        args = {"base_rev": self.rt.bridge_config_revision() if base_rev is None else base_rev,
                "changes": changes}
        return self.service.apply(args)


@pytest.fixture
def rt(tmp_path):
    return FakeRuntime(tmp_path)


def adapt(result):
    return commands.adapt_result("settings.apply", result)


def assert_done(result):
    success, safe, _ = adapt(result)
    assert success and safe == result


def failure(harness, changes, **kwargs):
    with pytest.raises(JobFailure) as caught:
        harness.apply(changes, **kwargs)
    exc = caught.value
    _, safe, _ = adapt(exc.partial_result)
    assert safe == exc.partial_result  # Schema-valid failed result facts.
    return exc


def sections(result):
    return {item["section"]: item for item in result["sections"]}


def local(rt):
    return yaml.safe_load(rt.local_path.read_text(encoding="utf-8"))


def alert_ids(rt):
    return [row["id"] for row in backend_sections(rt.config, autostart=None)["alerts"]["channels"]]


def validate_event(topic, data):
    frame = json.loads((FIXTURES / "topic_config.changed.json").read_bytes())
    if topic == "config.changed":
        frame["data"] = deepcopy(data)
        assert protocol.validate_frame(frame) == []


# -- combined save, rev and config.changed --------------------------------------

def test_combined_save_writes_once_applies_and_publishes(rt):
    h = Harness(rt)
    result = h.apply([
        {"section": "network", "values": {"mihomo_mixed_port": 7999, "allow_lan": True}},
        {"section": "dns", "values": {"respect_rules": True}},
        {"section": "routing", "values": {"rules": {"direct": ["example.test"]}}},
        {"section": "latency", "values": {"primary_gateway_max_latency_ms": 900}},
        {"section": "ui", "values": {"language": "zh_CN"}},
        {"section": "autostart", "values": {"enabled": True}},
    ])
    assert_done(result)
    assert result["rev"] == 1 and rt.bridge_config_revision() == 1
    by = sections(result)
    assert all(item["saved"] and item["applied"] and item["error"] is None for item in by.values())
    assert by["network"]["effect"] == "restart" and by["routing"]["effect"] == "reload"
    assert by["latency"]["effect"] == "recheck" and by["ui"]["effect"] == "ui"
    assert by["autostart"]["effect"] == "autostart"
    # ui goes through Qt's no-reload preference write; the other local
    # sections share one merged write and its single Python reload.
    assert rt.local_writes == 2 and rt.reloads == [None]
    # One Core plan (restart wins), then the independent recheck and actions.
    assert rt.events.count("restart_core") == 1
    # The only Core reload is the latency recheck's own incremental export.
    reloads = [e for e in rt.events if isinstance(e, tuple) and e[0] == "reload_core"]
    # Correlated with the save's commit ID, yet distinct from it.
    assert len(reloads) == 1
    assert re.fullmatch(r"settings-apply:1:[0-9a-f]{12}:latency-recheck", reloads[0][3])
    recheck = rt.events.index("check_exit")
    assert rt.events.index("restart_core") < recheck
    assert rt.events.count(("set_language", "zh_CN")) == 1 and ("set_autostart", True) in rt.events
    saved = local(rt)
    assert saved["client"]["mihomo_mixed_port"] == 7999
    assert saved["client"]["system_proxy"]["port"] == 7999  # Qt coupling.
    assert saved["client"]["dns"]["respect_rules"] is True
    assert saved["routing"]["rules"]["direct"] == ["example.test"]
    assert saved["testing"]["primary_gateway_max_latency_ms"] == 900
    # config.changed after the controlled write; ui.language after the save.
    topics = [topic for topic, _ in h.edges]
    assert topics == ["config.changed", "ui.language"]
    changed = h.edges[0][1]
    assert changed == {"rev": 1, "files": ["local"], "sections": result["sections"],
                       "pending": None, "external_detection": "bridge_writes_only"}
    validate_event("config.changed", changed)
    assert h.edges[1][1] == {"language": "zh_CN", "config_rev": 1}
    # config.snapshot reads the same revision through the existing owner seam.
    revision, _ = config_query_owners(SimpleNamespace(runtime=rt))
    assert revision() == 1


def test_unchanged_section_is_saved_without_write_or_revision(rt):
    h = Harness(rt)
    result = h.apply([{"section": "ui", "values": {"language": "en_US"}}])
    assert_done(result)
    assert result["rev"] == 0 and rt.local_writes == 0 and h.edges == []
    assert sections(result)["ui"] == {"section": "ui", "saved": True, "applied": True,
                                      "effect": "none", "error": None}


def test_dns_only_change_refreshes_policy_then_reloads_incrementally(rt):
    h = Harness(rt)
    rt.core_reload_ok = False  # Incremental DNS reload refused: restart fallback.
    result = h.apply([{"section": "dns", "values": {"respect_rules": True}}])
    assert_done(result)
    core = [e for e in rt.events if e in ("restart_core",) or isinstance(e, tuple)
            and e[0] in {"dns_refresh", "reload_core"}]
    assert core == [("dns_refresh", "settings_changed"),
                    ("reload_core", "incremental_export", DNS, ""), "restart_core"]
    assert sections(result)["dns"]["effect"] == "reload"


def test_graph_reload_carries_reason_and_real_commit_id(rt):
    h = Harness(rt)
    result = h.apply([{"section": "routing", "values": {"rules": {"proxy": ["p.test"]}}}])
    assert_done(result)
    reload = [e for e in rt.events if isinstance(e, tuple) and e[0] == "reload_core"]
    assert len(reload) == 1
    _, mode, reason, commit = reload[0]
    assert (mode, reason) == ("strict", GRAPH)
    assert re.fullmatch(r"settings-apply:1:[0-9a-f]{12}", commit)


# -- partial failures ----------------------------------------------------------------

def test_partial_write_failure_keeps_the_other_section(rt):
    h = Harness(rt)
    rt.write_failure = lambda payload: payload.get("traffic", {}).get("history_seconds") == 7200
    exc = failure(h, [
        {"section": "network", "values": {"allow_lan": True}},
        {"section": "traffic", "values": {"history_seconds": 7200}},
    ])
    assert exc.code == "io"
    by = sections(exc.partial_result)
    assert by["network"]["saved"] and by["network"]["applied"] and by["network"]["error"] is None
    assert by["traffic"]["saved"] is False and by["traffic"]["error"]["code"] == "io"
    assert by["traffic"]["error"]["reason_code"] == "local_write_failed"
    saved = local(rt)
    assert saved["client"]["allow_lan"] is True and "traffic" not in saved
    # The committed write still advanced the revision and was published.
    assert exc.partial_result["rev"] == 1 and h.edges[0][0] == "config.changed"


def test_validation_failure_of_one_section_does_not_block_others(rt):
    h = Harness(rt)
    exc = failure(h, [
        {"section": "network", "values": {"mihomo_mixed_port": 70000}},
        {"section": "killswitch", "values": {"enabled": False}},
    ])
    assert exc.code == "validation"
    by = sections(exc.partial_result)
    assert by["network"]["error"]["field"] == "network.mihomo_mixed_port"
    assert by["network"]["error"]["hold"] is True and by["network"]["saved"] is False
    assert by["killswitch"]["saved"] and by["killswitch"]["applied"]
    assert local(rt)["killswitch"]["enabled"] is False


def test_core_reload_failure_is_saved_not_applied(rt):
    h = Harness(rt)
    rt.core_reload_ok = False
    exc = failure(h, [{"section": "routing", "values": {"rules": {"direct": ["a.test"]}}},
                      {"section": "ui", "values": {"overview_auto_exit_lookup": False}}])
    assert exc.code == "io"
    by = sections(exc.partial_result)
    assert by["routing"] == {"section": "routing", "saved": True, "applied": False,
                             "effect": "reload", "error": by["routing"]["error"]}
    assert by["routing"]["error"]["reason_code"] == "reload_not_confirmed"
    assert by["ui"]["saved"] and by["ui"]["applied"] and by["ui"]["error"] is None
    assert local(rt)["routing"]["rules"]["direct"] == ["a.test"]  # Not rolled back.


def test_python_reload_failure_after_write_reports_saved(rt):
    h = Harness(rt)
    rt.reload_error = RuntimeError("reload failed with secret material")
    exc = failure(h, [{"section": "killswitch", "values": {"enabled": False}}])
    item = sections(exc.partial_result)["killswitch"]
    assert item["saved"] is True and item["applied"] is False
    assert item["error"]["reason_code"] == "config_reload_failed"
    assert "secret material" not in json.dumps(exc.partial_result)
    assert local(rt)["killswitch"]["enabled"] is False and exc.partial_result["rev"] == 1


def test_restart_failure_is_reported(rt):
    h = Harness(rt)
    rt.restart_ok = False
    exc = failure(h, [{"section": "network", "values": {"ipv6": True}}])
    item = sections(exc.partial_result)["network"]
    assert item["saved"] and not item["applied"] and item["effect"] == "restart"
    assert item["error"]["reason_code"] == "restart_not_confirmed"
    assert local(rt)["client"]["dns"]["ipv6"] is True  # Qt keeps dns.ipv6 with ipv6.


# -- lifecycle phases ------------------------------------------------------------------

def test_save_while_starting_is_deferred_with_pending(rt):
    coordinator = Coordinator()
    h = Harness(rt, coordinator=coordinator)
    rt.phase = "collecting"
    rt.core_runner.running = False
    result = h.apply([{"section": "network", "values": {"allow_lan": True}},
                      {"section": "latency", "values": {"primary_gateway_max_latency_ms": 700}},
                      {"section": "ui", "values": {"language": "ja_JP"}}])
    assert_done(result)
    by = sections(result)
    assert by["network"]["effect"] == by["latency"]["effect"] == "deferred"
    assert not by["network"]["applied"] and not by["latency"]["applied"]
    assert by["ui"]["applied"]  # Independent of Core.
    assert result["pending"]["state"] == "pending" and result["pending"]["effect"] == "restart"
    plan, rev, commit = coordinator.calls[0]
    assert plan.core.effect == "deferred" and rev == 1 and commit.startswith("settings-apply:1:")
    assert "restart_core" not in rt.events and "check_exit" not in rt.events
    assert h.edges[0][1]["pending"] == result["pending"]
    validate_event("config.changed", h.edges[0][1])


def test_deferred_without_coordinator_is_failed_but_saved(rt):
    h = Harness(rt)
    rt.phase = "core_standby"
    exc = failure(h, [{"section": "network", "values": {"allow_lan": True}}])
    item = sections(exc.partial_result)["network"]
    assert exc.code == "runtime_unavailable"
    assert item["saved"] and item["effect"] == "deferred" and not item["applied"]
    assert local(rt)["client"]["allow_lan"] is True


@pytest.mark.parametrize("phase,running", [("stopping", True), ("stopped", False)])
def test_save_while_stopping_or_stopped_is_done_and_applied(rt, phase, running):
    coordinator = Coordinator()
    h = Harness(rt, coordinator=coordinator)
    rt.phase = phase
    rt.core_runner.running = running
    result = h.apply([{"section": "network", "values": {"allow_lan": True}},
                      {"section": "latency", "values": {"primary_gateway_max_latency_ms": 650}}])
    assert_done(result)
    assert all(item["applied"] and item["effect"] == "none" for item in result["sections"])
    assert result["pending"] is None
    assert "restart_core" not in rt.events and "check_exit" not in rt.events
    assert len(coordinator.calls) == 1  # A live intent is still advanced to rev 1.


def test_recovery_gate_busy_writes_nothing(rt):
    h = Harness(rt, gate_error=RuntimeError("RT-1 running"))
    before = rt.local_path.read_bytes()
    exc = failure(h, [{"section": "ui", "values": {"language": "de_DE"}}])
    assert exc.code == "lifecycle_busy" and rt.local_path.read_bytes() == before
    assert rt.bridge_config_revision() == 0 and h.edges == []


# -- base_rev conflict -------------------------------------------------------------------

def test_base_rev_conflict_writes_nothing(rt):
    h = Harness(rt)
    h.apply([{"section": "killswitch", "values": {"enabled": False}}])
    before = rt.local_path.read_bytes()
    edges = len(h.edges)
    exc = failure(h, [{"section": "ui", "values": {"language": "fr_FR"}}], base_rev=0)
    assert exc.code == "conflict"
    item = exc.partial_result["sections"][0]
    assert item["saved"] is False and item["error"]["code"] == "conflict"
    assert exc.partial_result["rev"] == 1
    assert rt.local_path.read_bytes() == before and len(h.edges) == edges
    assert ("set_language", "fr_FR") not in rt.events


# -- hidden keys, masks and secrets --------------------------------------------------------

def _block(text: str, start: str) -> str:
    lines = text.splitlines(keepends=True)
    index = next(i for i, line in enumerate(lines) if line.rstrip() == start)
    indent = len(start) - len(start.lstrip())
    end = index + 1
    while end < len(lines) and len(lines[end]) - len(lines[end].lstrip()) > indent:
        end += 1
    return "".join(lines[index:end])


def test_hidden_keys_are_byte_identical_after_saves(rt):
    h = Harness(rt)
    text = rt.local_path.read_text(encoding="utf-8")
    hidden = _block(text, "    node_judgement:")
    controller = _block(text, "  mihomo_controller:")
    sources_before = yaml.safe_load(rt.sources_path.read_text(encoding="utf-8"))
    rows = source_rows(sources_before)
    result = h.apply([
        {"section": "network", "values": {"egress_recovery": {"enabled": False}, "allow_lan": True}},
        {"section": "ui", "values": {"language": "ko_KR"}},
        {"section": "sources", "values": {"items": [
            {"id": row["id"], "enabled": row["kind"] != "subscription_urls"} for row in rows]}},
    ])
    assert_done(result)
    after = rt.local_path.read_text(encoding="utf-8")
    assert _block(after, "    node_judgement:") == hidden
    assert _block(after, "  mihomo_controller:") == controller
    saved = local(rt)
    assert saved["client"]["egress_recovery"] == {"enabled": False, "node_judgement": NODE_JUDGEMENT}
    assert saved["ui"]["theme"] == "custom-retained"
    # No default materialized from the base file into the local layer.
    assert set(saved["client"]) == {"mihomo_controller", "egress_recovery", "allow_lan"}
    document = yaml.safe_load(rt.sources_path.read_text(encoding="utf-8"))
    assert document["subscription_urls"][0] == {"value": SUB_URL, "enabled": False, "note": "keep-me"}
    assert document["github_repos"] == sources_before["github_repos"]  # Additional keys kept.
    for key in ("migrations", "fetch_profiles", "github_delivery"):
        assert document[key] == sources_before[key]
    assert h.edges[0][1]["files"] == ["local", "sources"]


def test_hidden_node_judgement_submission_is_rejected(rt):
    h = Harness(rt)
    before = rt.local_path.read_bytes()
    exc = failure(h, [{"section": "network", "values": {
        "egress_recovery": {"node_judgement": {"enabled": False}}}}])
    assert exc.code == "validation" and rt.local_path.read_bytes() == before


def _telegram_rows(rt, **telegram):
    rows = [{"id": item} for item in alert_ids(rt)]
    for row in rows:
        if row["id"] == "telegram":
            row.update(telegram)
    return rows


def test_notice_switches_can_be_turned_off_and_on_again(rt):
    """提醒 redesign 10-30: alerts.notify is a plain local section."""
    h = Harness(rt)
    first = h.apply([{"section": "alerts", "values": {"notify": {"enabled": False}}}])
    assert_done(first)
    assert first["rev"] == 1 and local(rt)["alerts"]["notify"] == {"enabled": False}
    second = h.apply([{"section": "alerts", "values": {"notify": {"enabled": True, "events": ["update"]}}}])
    assert_done(second)
    assert second["rev"] == 2 and rt.bridge_config_revision() == 2
    assert local(rt)["alerts"]["notify"] == {"enabled": True, "events": ["update"]}
    assert [topic for topic, _ in h.edges].count("config.changed") == 2


def test_fault_after_the_committed_write_keeps_save_result_and_publishes(rt):
    h = Harness(rt)
    rt.lifecycle_error = RuntimeError("lifecycle read failed")
    exc = failure(h, [{"section": "killswitch", "values": {"enabled": False}}])
    assert exc.code == "internal"
    item = sections(exc.partial_result)["killswitch"]
    assert item["saved"] is True and item["applied"] is False
    assert item["error"]["reason_code"] == "apply_exception"
    assert exc.partial_result["rev"] == 1 and rt.bridge_config_revision() == 1
    assert local(rt)["killswitch"]["enabled"] is False
    assert [topic for topic, _ in h.edges] == ["config.changed"]
    assert h.edges[0][1]["rev"] == 1 and h.edges[0][1]["sections"] == exc.partial_result["sections"]
    validate_event("config.changed", h.edges[0][1])


def test_stale_config_owner_rejects_the_batch(rt):
    h = Harness(rt)
    facts = rt.bridge_config_facts
    rt.bridge_config_facts = lambda: {**facts(), "stale": True}
    before = rt.local_path.read_bytes()
    exc = failure(h, [{"section": "killswitch", "values": {"enabled": False}}])
    assert exc.code == "conflict"
    assert exc.partial_result["sections"][0]["error"]["reason_code"] == "config_owner_changed"
    assert rt.local_path.read_bytes() == before and rt.bridge_config_revision() == 0
    assert h.edges == []


def test_core_stopped_during_the_save_is_applied_not_failed(rt):
    h = Harness(rt)
    rt.restart_ok = False
    rt.intent = {"desired_running": False, "stop_requested": True}  # Stop overtook it.
    result = h.apply([{"section": "network", "values": {"allow_lan": True}}])
    assert_done(result)
    item = sections(result)["network"]
    assert item == {"section": "network", "saved": True, "applied": True,
                    "effect": "none", "error": None}


def test_restart_failure_without_a_stop_still_fails(rt):
    h = Harness(rt)
    rt.restart_ok = False
    rt.intent = {"desired_running": True, "stop_requested": False}
    exc = failure(h, [{"section": "network", "values": {"allow_lan": True}}])
    assert sections(exc.partial_result)["network"]["error"]["reason_code"] == "restart_not_confirmed"


def test_settings_apply_latency_recheck_uses_the_real_entry(rt):
    """P2-11 deferred path -> DesktopRuntime.recheck_latency_policy."""
    assert rt._settings_apply_latency_recheck() is None
    assert rt.events[:4] == ["check_exit", "check_tier1", "check_tier2", "evaluate_state"]

    def broken():
        raise RuntimeError("tier1 probe failed")
    rt.manager.check_tier1 = broken
    error = rt._settings_apply_latency_recheck()
    assert error["reason_code"] == "latency_recheck_failed"


# -- sources --------------------------------------------------------------------------------

def test_sources_section_writes_and_reloads_once(rt):
    h = Harness(rt)
    rows = source_rows(rt.read_sources_config())
    writes = []
    original = rt.config_store.write_sources
    rt.config_store.write_sources = lambda payload: writes.append(1) or original(payload)
    result = h.apply([{"section": "sources", "values": {"items": [
        {"id": row["id"], "enabled": False} for row in rows]}}])
    assert_done(result)
    assert len(writes) == 1 and rt.reloads == ["tier3"] and rt.local_writes == 0
    assert sections(result)["sources"] == {"section": "sources", "saved": True, "applied": True,
                                           "effect": "reload", "error": None}
    assert h.edges[0][1]["files"] == ["sources"] and result["rev"] == 1


# -- language -------------------------------------------------------------------------------

def test_language_switch_success_syncs_and_publishes(rt):
    h = Harness(rt)
    before = local(rt)
    result = h.apply([{"section": "ui", "values": {"language": "ru_RU"}}])
    assert_done(result)
    # Qt parity: the no-reload preference write, no Python reload_config.
    assert rt.reloads == [] and rt.local_writes == 1
    assert rt.events.count(("set_language", "ru_RU")) == 1
    assert rt._configured_ui_language() == "ru_RU"
    after = local(rt)
    assert after["ui"] == {**before["ui"], "language": "ru_RU"}  # theme kept.
    assert {k: v for k, v in after.items() if k != "ui"} == {
        k: v for k, v in before.items() if k != "ui"}
    assert ("ui.language", {"language": "ru_RU", "config_rev": 1}) in h.edges


class SyncCountingPort(RuntimeSettingsPort):
    def __init__(self, runtime):
        super().__init__(runtime)
        self.synced = []

    def sync_language(self, language):
        self.synced.append(language)
        super().sync_language(language)


def test_language_with_d8_falls_back_to_reload_and_orchestrator_syncs_once(rt):
    h = Harness(rt)
    port = SyncCountingPort(rt)
    h.service.port = port
    result = h.apply([{"section": "ui", "values": {"language": "de_DE", TRAY_KEY: True}}])
    assert_done(result)
    # No no-reload local entry yet: the D8 key takes the reload write.
    assert rt.reloads == [None] and rt.local_writes == 1
    assert port.synced == ["de_DE"]  # The orchestrator's own sync, exactly once.
    assert rt.events.count(("set_language", "de_DE")) == 1
    assert ("reload_set_language", "de_DE") in rt.events
    assert local(rt)["ui"][TRAY_KEY] is True


def test_preferences_use_no_reload_entry_when_the_runtime_has_it(rt):
    calls = []

    def update_local_preferences(updater):
        # Shape of the requested DesktopRuntime entry: locked read-update-
        # write, runtime ui/traffic updated, language synced, no reload.
        with rt._config_change_lock:
            calls.append(1)
            payload = updater(rt.config_store.read_local())
            result = rt.config_store.write_local(payload)
            for key in ("ui", "traffic"):
                if isinstance(payload.get(key), dict):
                    rt.config.setdefault(key, {}).update(payload[key])
            rt.alerts.set_language(rt._configured_ui_language())
            return result

    rt.update_local_preferences = update_local_preferences
    h = Harness(rt)
    result = h.apply([{"section": "ui", "values": {TRAY_KEY: True}},
                      {"section": "traffic", "values": {"history_seconds": 7200}}])
    assert_done(result)
    assert calls == [1] and rt.reloads == [] and rt.local_writes == 1
    saved = local(rt)
    assert saved["ui"][TRAY_KEY] is True and saved["traffic"] == {"history_seconds": 7200}
    assert saved["ui"]["theme"] == "custom-retained"
    assert all(item["saved"] and item["applied"] for item in result["sections"])


def test_language_switch_failure_keeps_the_old_language(rt):
    h = Harness(rt)
    rt.write_failure = lambda payload: True
    exc = failure(h, [{"section": "ui", "values": {"language": "es_ES"}}])
    item = sections(exc.partial_result)["ui"]
    assert item["saved"] is False and item["error"]["code"] == "io"
    assert item["error"]["message_key"] == "errors.settings.save_failed"
    assert local(rt)["ui"]["language"] == "en_US" and rt._configured_ui_language() == "en_US"
    assert ("set_language", "es_ES") not in rt.events and h.edges == []


def test_invalid_language_is_a_field_error(rt):
    h = Harness(rt)
    exc = failure(h, [{"section": "ui", "values": {"language": "xx_XX"}}])
    assert sections(exc.partial_result)["ui"]["error"]["field"] == "ui.language"


# -- D8: ui.start_in_tray_on_autostart -------------------------------------------------------

def test_d8_default_false_in_shipped_config_and_qt_compatible(rt):
    base = load_config(str(ROOT / "config.example.yaml"), local_path=None)
    assert base["ui"][TRAY_KEY] is False
    assert rt.config["ui"][TRAY_KEY] is False
    # Qt ignores the key when reading preferences and keeps it when writing.
    prefs = normalized_ui_preferences(rt.config, normalize_language=str, normalize_theme=str,
                                      default_language="en_US", default_theme="daylight")
    assert set(prefs) == {"language", "theme"}
    payload = {"ui": {TRAY_KEY: True, "language": "en_US"}}
    apply_ui_preferences_to_payload(payload, {"language": "de_DE", "theme": "daylight"})
    assert payload["ui"][TRAY_KEY] is True
    validate_local_config(payload)
    with pytest.raises(ValueError):
        validate_local_config({"ui": {TRAY_KEY: "yes"}})


def test_d8_read_write_through_settings_apply(rt):
    h = Harness(rt)
    result = h.apply([{"section": "ui", "values": {TRAY_KEY: True}}])
    assert_done(result)
    assert local(rt)["ui"][TRAY_KEY] is True and rt.config["ui"][TRAY_KEY] is True
    assert sections(result)["ui"]["effect"] == "ui"
    # Qt's own preference writer keeps the new key.
    rt.write_ui_preferences({"language": "pt_BR", "theme": "daylight"})
    assert local(rt)["ui"][TRAY_KEY] is True and local(rt)["ui"]["theme"] == "daylight"
    exc = failure(h, [{"section": "ui", "values": {TRAY_KEY: "true"}}])
    assert sections(exc.partial_result)["ui"]["error"]["field"] == f"ui.{TRAY_KEY}"


# -- settings.validate -----------------------------------------------------------------------

def test_settings_validate_has_no_side_effects(rt):
    h = Harness(rt)
    before = rt.local_path.read_bytes()
    assert h.service.validate({"section": "network", "field": "mihomo_mixed_port",
                               "value": 7891}) == {"valid": True, "errors": []}
    invalid = h.service.validate({"section": "network", "field": "mihomo_mixed_port", "value": 0})
    assert invalid["valid"] is False and invalid["errors"][0]["code"] == "validation"
    assert h.service.validate({"section": "ui", "field": TRAY_KEY, "value": False})["valid"]
    assert not h.service.validate({"section": "ui", "field": TRAY_KEY, "value": 1})["valid"]
    secret = h.service.validate({"section": "alerts", "field": "channels.telegram.bot_token",
                                 "value": "••••••"})
    assert secret["valid"] is False
    for response in (invalid, secret):
        errors = []
        protocol._schema(response, protocol.V1_COMMAND_SPECS["settings.validate"][5],
                         "result", errors)
        assert errors == []
    assert rt.local_path.read_bytes() == before and rt.events == [] and h.edges == []


# -- write merge helper ------------------------------------------------------------------------

def test_write_section_only_touches_changed_leaves():
    payload = {"client": {"tun": {"enabled": False, "custom": 1}}, "other": {"x": 1}}
    old = {"tun": {"enabled": False, "stack": "system", "custom": 1}, "allow_lan": False}
    new = {"tun": {"enabled": True, "stack": "system", "custom": 1}, "allow_lan": False}
    write_section(payload, "network", old, new)
    assert payload == {"client": {"tun": {"enabled": True, "custom": 1}}, "other": {"x": 1}}


# -- runtime split and revision ----------------------------------------------------------------

def test_recheck_latency_policy_writes_nothing_and_reloads_graph(rt):
    result = rt.recheck_latency_policy()
    assert result["errors"] == [] and result["saved"] is False and rt.local_writes == 0
    assert rt.events[:4] == ["check_exit", "check_tier1", "check_tier2", "evaluate_state"]
    reload = rt.events[4]
    assert reload[:3] == ("reload_core", "incremental_export", GRAPH)
    assert reload[3].startswith(f"latency-policy:{result['maximum_ms']}:")
    rt.events.clear()
    rt.core_runner.running = False
    assert rt.recheck_latency_policy()["recheck_attempted"] is False and rt.events == []


def test_apply_latency_policy_settings_behavior_is_unchanged(rt):
    result = rt.apply_latency_policy_settings(800)
    assert result["saved"] is True and result["maximum_ms"] == 800
    assert result["recheck_order"] == ["exit", "tier1", "tier2"] and result["errors"] == []
    assert rt.events[-1][3].startswith("latency-policy:800:")
    rt.events.clear()
    rt.core_runner.running = False
    stopped = rt.apply_latency_policy_settings(700)
    assert stopped["recheck_attempted"] is False
    assert [e for e in rt.events if e != "write_local" and not isinstance(e, tuple)] == []


def test_bridge_config_revision_getter_counts_commits(rt):
    assert rt.bridge_config_revision() == 0
    assert rt.bridge_commit_config_revision() == 1
    assert rt.bridge_commit_config_revision() == 2 and rt.bridge_config_revision() == 2


# -- executor wiring -----------------------------------------------------------------------------

@pytest.fixture
def executor():
    instance = JobExecutor(JobEventRecorder(8192), epoch="p2-10", require_acceptance=True)
    yield instance
    instance.shutdown(time.monotonic() + 1)


def run(executor, name, args):
    ack = executor.submit(name, args, req_id="request-a", owner="window-a")
    assert ack.accepted, ack
    assert executor.acceptance_sent(ack.op, epoch=executor.epoch)
    job = executor.wait(ack.op, 5)
    assert job is not None
    events = executor.recorder.prepare_for_send(1)
    assert events and all(not protocol.validate_frame(event) for event in events)
    executor.recorder.ack_sent(events)
    return job


def test_register_wires_commands_and_terminal_states(rt, executor):
    gate_calls = []
    session = SimpleNamespace(
        runtime=rt, executor=executor, network_lock=threading.RLock(),
        report=SimpleNamespace(gate=SimpleNamespace(require_idle=lambda: gate_calls.append(1))),
        state=PublicationState("p2-10-session"), logs=None)
    service = bridge_settings.register(session, port=RuntimeSettingsPort(rt))
    assert session.settings is service
    done = run(executor, "settings.apply", {"base_rev": 0, "changes": [
        {"section": "killswitch", "values": {"enabled": False}}]})
    assert done["state"] == "done" and done["result"]["rev"] == 1
    failed = run(executor, "settings.apply", {"base_rev": 0, "changes": [
        {"section": "killswitch", "values": {"enabled": True}}]})
    assert failed["state"] == "failed" and failed["error"]["code"] == "conflict"
    assert failed["result"]["sections"][0]["error"]["reason_code"] == "base_rev_mismatch"
    assert gate_calls  # Recovery gate checked at the mutation entry.
    receipt = executor.submit("settings.validate", {"section": "ui", "field": "language",
                                                    "value": "zh_CN"}, req_id="v", owner="window-a")
    assert receipt.accepted
    response = receipt.wait(5)
    assert response is not None
    assert commands.COMMAND_SPECS["settings.apply"].cancellation == "queued"
    assert commands.COMMAND_SPECS["settings.apply"].domain == "mutation"


def test_bridge_settings_has_no_private_runtime_access():
    from tests.test_bridge_runtime_access import private_runtime_accesses
    source = (ROOT / "bridge_settings.py").read_text(encoding="utf-8")
    assert private_runtime_accesses(source) == []
