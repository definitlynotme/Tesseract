"""T-P2-H2: backend small fixes from 决定_第五波裁定 (wave 5 rulings).

In-process tests only. The one test that builds a real ``DesktopRuntime``
does so through ``bridge_main.create_runtime`` on a hardened, temporary
custom-scope sandbox (tests/test_bridge_p2_33.prepare_sandbox): nothing is
started, no Core, no network, no HKCU or firewall write.
"""
from __future__ import annotations

from copy import deepcopy
import json
import logging
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest
import yaml

import bridge_main
import bridge_protocol as protocol
import bridge_wiring
from bridge_jobs import JobEventRecorder, JobExecutor, JobFailure
from bridge_publish import Publisher
from bridge_state import PublicationState


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "bridge_schema/fixtures/v1/valid"
EPOCH = "epoch-h2"
CUSTOM = SimpleNamespace(kind="custom", may_take_over_host_network=False)
DEFAULT = SimpleNamespace(kind="default", may_take_over_host_network=True)


def make_executor():
    return JobExecutor(JobEventRecorder(4096), epoch=EPOCH, require_acceptance=True)


def run_job(executor, name, args):
    ack = executor.submit(name, args, req_id="r-1", owner="window-1")
    assert ack.accepted, ack
    assert executor.acceptance_sent(ack.op, epoch=EPOCH)
    job = executor.wait(ack.op, 10)
    assert job is not None and job["state"] in {"done", "failed"}, job
    return job


def job_frame(executor):
    """Validation errors of every queued job edge as a wire frame."""
    frames = executor.recorder.prepare_for_send(int(time.time() * 1000))
    try:
        assert frames
        return [protocol.validate_frame(frame) for frame in frames]
    finally:
        executor.recorder.abort_send()


# ===================================================== 1. 33-#17 scope forcing

def forced(config):
    return (config["client"]["network_mode"], config["client"]["system_proxy"]["enabled"],
            config["killswitch"]["enabled"])


def tampered_config():
    return {"client": {"network_mode": "tun", "system_proxy": {"enabled": True}},
            "killswitch": {"enabled": True}}


def test_enforce_scope_config_forces_only_the_custom_scope():
    config = tampered_config()
    assert forced(bridge_main.enforce_scope_config(CUSTOM, config)) == ("system_proxy", False, False)
    untouched = tampered_config()
    for scope in (DEFAULT, SimpleNamespace(kind="audit", may_take_over_host_network=False), None):
        assert bridge_main.enforce_scope_config(scope, untouched) == tampered_config()
    # Non-dict blocks are replaced by the forced values, never kept.
    odd = {"client": "x", "killswitch": None}
    assert forced(bridge_main.enforce_scope_config(CUSTOM, odd)) == ("system_proxy", False, False)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Hardened custom-scope directories (T-P2-33) with APPDATA/TEMP inside."""
    from tests.test_bridge_p2_33 import prepare_sandbox
    box = prepare_sandbox(tmp_path / "box", frozen=False)
    for name, key in (("APPDATA", "appdata"), ("LOCALAPPDATA", "localappdata"),
                      ("TEMP", "temp"), ("TMP", "temp")):
        monkeypatch.setenv(name, str(box[key]))
    import desktop_runtime
    # create_runtime swaps the module's KillSwitchManager; restore it after.
    monkeypatch.setattr(desktop_runtime, "KillSwitchManager", desktop_runtime.KillSwitchManager)
    return box


def _close_logs(runtime):
    for handler in list(getattr(runtime, "logger", logging.getLogger("x")).handlers):
        handler.close()
        runtime.logger.removeHandler(handler)


def tamper_local(path: Path):
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    client = data.setdefault("client", {})
    client["network_mode"] = "tun"
    client.setdefault("system_proxy", {})["enabled"] = True
    data.setdefault("killswitch", {})["enabled"] = True
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def test_custom_scope_reload_after_disk_tampering_keeps_the_forced_values(sandbox):
    """Real create_runtime + DesktopRuntime.reload_config (the one load point,
    SidecarRuntime._disable_external_desktop_notifications)."""
    scope = SimpleNamespace(kind="custom", may_take_over_host_network=False,
                            config_dir=sandbox["config_dir"], data_dir=sandbox["data_dir"])
    runtime = bridge_main.create_runtime(scope, SimpleNamespace(require_idle=lambda: None),
                                         lambda *_: None)
    try:
        assert forced(runtime.config) == ("system_proxy", False, False)
        local = Path(sandbox["config_dir"]) / "config.local.yaml"
        tamper_local(local)
        runtime.reload_config()
        assert forced(runtime.config) == ("system_proxy", False, False)
        assert runtime.network_mode == "system_proxy"
        # Forced in memory only: the sandbox file keeps what was written.
        assert yaml.safe_load(local.read_text(encoding="utf-8"))["client"]["network_mode"] == "tun"
        # The desktop-notification override of the base class still runs.
        assert runtime.config["alerts"]["desktop"]["disabled_in_native_ui"] is True
    finally:
        _close_logs(runtime)


def _fake_runtime_base(monkeypatch):
    """A light DesktopRuntime stand-in with the real load-point override chain."""
    import desktop_runtime
    from app_paths import apply_release_path_overrides
    from main import load_config
    real = desktop_runtime.DesktopRuntime

    class Base:
        _disable_external_desktop_notifications = real._disable_external_desktop_notifications

        def __init__(self, config, *, app_paths=None, autostart_manager=None):
            self.app_paths = app_paths
            self.config = self._disable_external_desktop_notifications(config)
            self.runtime_state = SimpleNamespace(clear=lambda: None)

        def reload_config(self):
            self.config = self._disable_external_desktop_notifications(apply_release_path_overrides(
                load_config(str(self.app_paths.config_path), str(self.app_paths.local_config_path)),
                self.app_paths))
    monkeypatch.setattr(desktop_runtime, "DesktopRuntime", Base)
    monkeypatch.setattr(desktop_runtime, "KillSwitchManager", desktop_runtime.KillSwitchManager)


def test_default_scope_reload_is_unchanged(tmp_path, monkeypatch):
    _fake_runtime_base(monkeypatch)
    scope = SimpleNamespace(kind="default", may_take_over_host_network=True,
                            config_dir=tmp_path / "config", data_dir=tmp_path / "data")
    runtime = bridge_main.create_runtime(scope, SimpleNamespace(require_idle=lambda: None),
                                         lambda *_: None)
    tamper_local(tmp_path / "config" / "config.local.yaml")
    runtime.reload_config()
    assert forced(runtime.config) == ("tun", True, True)


class ModePort:
    """mode.set port double: set_network_mode persists to a real file."""

    def __init__(self, path: Path):
        self.path, self.calls = path, []
        self.path.write_text("system_proxy", encoding="utf-8")

    def core_fact(self):
        return {"running": False, "pid": None, "process_generation": 0,
                "cleanup_pending": False, "core_generation": 1}

    def persisted_network_mode(self):
        return self.path.read_text(encoding="utf-8")

    def network_mode(self):
        return self.path.read_text(encoding="utf-8")

    def core_loaded_network_mode(self):
        return None

    def set_network_mode(self, mode):
        self.calls.append(mode)
        changed = self.path.read_text(encoding="utf-8") != mode
        self.path.write_text(mode, encoding="utf-8")
        return {"mode": mode, "changed": changed, "restarted": False,
                "core_generation_before": 1, "core_generation_after": 1}


@pytest.mark.parametrize("mode", ["tun", "mixed"])
def test_mode_set_off_system_proxy_fails_closed_in_the_custom_scope(tmp_path, mode):
    from bridge_node_commands import NodeCommands, session_scope_isolated
    session = SimpleNamespace(scope=SimpleNamespace(kind="custom", may_take_over_host_network=False))
    port = ModePort(tmp_path / "mode.txt")
    before = port.path.read_bytes()
    commands = NodeCommands(port, scope_isolated=session_scope_isolated(session))
    executor = make_executor()
    try:
        executor.register("mode.set", commands.mode_set)
        job = run_job(executor, "mode.set", {"mode": mode})
        assert job["state"] == "failed"
        assert job["error"] == {"code": "not_allowed", "message_key": "errors.mode.scope_isolated",
                                "params": {}, "reason_code": "scope_isolated"}
        assert job["result"]["saved"] is False and job["result"]["changed"] is False
        assert port.calls == [] and port.path.read_bytes() == before     # disk unchanged
        assert all(errors == [] for errors in job_frame(executor))
        # system_proxy itself stays allowed in the custom scope.
        job = run_job(executor, "mode.set", {"mode": "system"})
        assert job["state"] == "done" and port.calls == ["system_proxy"]
    finally:
        executor.shutdown(time.monotonic() + 2)


def test_mode_set_in_the_default_scope_is_unchanged(tmp_path):
    from bridge_node_commands import NodeCommands, session_scope_isolated
    for scope in (SimpleNamespace(kind="default", may_take_over_host_network=True), None):
        assert session_scope_isolated(SimpleNamespace(scope=scope)) is False
    assert session_scope_isolated(SimpleNamespace()) is False
    port = ModePort(tmp_path / "mode.txt")
    executor = make_executor()
    try:
        executor.register("mode.set", NodeCommands(port).mode_set)
        job = run_job(executor, "mode.set", {"mode": "tun"})
        assert job["state"] == "done" and port.calls == ["tun"]
        assert port.path.read_text(encoding="utf-8") == "tun"
    finally:
        executor.shutdown(time.monotonic() + 2)


# ========================================================= 2. 33-#10 bootstrap

@pytest.mark.parametrize("first_run", [True, False])
def test_every_start_bootstraps_after_the_first_run_decision_and_before_loading(
        tmp_path, monkeypatch, first_run):
    import app_paths
    import main
    calls = []

    class Stop(Exception):
        pass

    def load_config(config_path, local_path):
        calls.append("load")
        raise Stop

    monkeypatch.setattr(bridge_main, "prepare_first_run",
                        lambda paths: calls.append("first_run") or first_run)
    monkeypatch.setattr(app_paths, "bootstrap_user_files", lambda paths: calls.append("bootstrap"))
    monkeypatch.setattr(main, "load_config", load_config)
    scope = SimpleNamespace(config_dir=tmp_path / "config", data_dir=tmp_path / "data")
    with pytest.raises(Stop):
        bridge_main.create_runtime(scope, SimpleNamespace(require_idle=lambda: None), lambda *_: None)
    # A first run bootstraps inside prepare_first_run (before its language write).
    assert calls == (["first_run", "load"] if first_run else ["first_run", "bootstrap", "load"])


def test_existing_config_directory_is_migrated_before_the_first_load(tmp_path, monkeypatch):
    """An existing scope whose sources.yaml lacks the template markers gets the
    same migration Qt's startup gives it, before load_config reads it."""
    import main
    config = tmp_path / "config"
    config.mkdir()
    (config / "config.yaml").write_bytes((ROOT / "config.example.yaml").read_bytes())
    (config / "sources.yaml").write_text("github_repos: []\n", encoding="utf-8")  # sources-marker: intentional
    seen = []

    class Stop(Exception):
        pass

    def load_config(config_path, local_path):
        seen.append(yaml.safe_load((config / "sources.yaml").read_text(encoding="utf-8")))
        seen.append(Path(local_path).is_file())
        raise Stop
    monkeypatch.setattr(main, "load_config", load_config)
    scope = SimpleNamespace(config_dir=config, data_dir=tmp_path / "data")
    with pytest.raises(Stop):
        bridge_main.create_runtime(scope, SimpleNamespace(require_idle=lambda: None), lambda *_: None)
    sources, local_exists = seen
    assert sources["migrations"]["tier3_source_portfolio_defaults_v1"] is True
    assert local_exists


# ================================================= 3. 33-#13 stopped Gateway

def test_gateway_status_without_core_is_the_saved_intent_and_not_stale():
    import desktop_runtime
    config = {"tiers": {"exit": {"selected": "Exit-Tokyo"}}}
    owner = (7, 3, None, config, False, False)
    fake = SimpleNamespace(
        _bridge_read_owner=lambda: owner, _bridge_owner_current=lambda value: value is owner,
        core_fact=lambda: {"running": False, "pid": None, "process_generation": 0,
                           "cleanup_pending": False, "core_generation": 2},
        bridge_secret_facts=lambda: {})
    raw = desktop_runtime.DesktopRuntime.bridge_gateway_status(fake)
    assert (raw["selected"], raw["effective"], raw["missing"], raw["applied"], raw["stale"],
            raw["generation"]) == ("Exit-Tokyo", None, False, False, False, 7)
    dto = {key: raw[key] for key in ("selected", "effective", "missing", "applied", "pending")}
    assert protocol.validate_topic_payload("gateway.current", dto) == []
    # An owner change during the read is reported stale, never current.
    moved = SimpleNamespace(**{**fake.__dict__, "_bridge_owner_current": lambda value: False})
    assert desktop_runtime.DesktopRuntime.bridge_gateway_status(moved)["stale"] is True


@pytest.mark.parametrize("fact_versions", [True, False])
def test_sampled_gateway_current_publishes_the_intent_while_core_is_stopped(fact_versions):
    """The stopped intent must not depend on bridge_sampling_fact_versions
    (it still gates the running-Core path; the real runtime has it since
    T-P2-H6, other runtimes may not)."""
    from tests.test_bridge_p2_08 import projection
    access, state, topics, work = projection()
    if not fact_versions:
        del access.runtime.bridge_sampling_fact_versions
    access.lifecycle["core"].update(running=False, pid=None, process_generation=0, cleanup_pending=False)
    access.route.update(valid=False, owner=None, managed_tier=None, leaf=None)
    access.records["gateway.current"].update(selected="Exit-Tokyo", effective=None,
                                             missing=False, applied=False)
    state.subscribe(["gateway.current"])
    topics.sample(lambda name, action: action())
    assert work, "the stopped state must schedule a gateway read"
    for item in list(work):
        item()
    snapshot = state.snapshot("gateway.current")
    assert snapshot is not None and snapshot["kind"] == "snapshot"
    assert snapshot["data"]["effective"] is None and snapshot["data"]["applied"] is False
    assert topics.status["gateway.current"] == "current"
    # Core running again without a current published route: withheld as before.
    access.lifecycle["core"].update(running=True, pid=4242, process_generation=1)
    topics.sample(lambda name, action: action())
    assert topics.status["gateway.current"] in {"stale", "unknown"}


# ============================================== 4. 33-#12/#11 snapshot vs edges

class Wire:
    """Transport double that also enforces the shell's receive rules
    (transport.rs EventCursor): per topic increasing rev, continuous edge
    seq, gap from_seq = previous + 1."""

    def __init__(self):
        self.frames, self.revs, self.seqs = [], {}, {}

    def send(self, frame):
        assert protocol.validate_frame(frame) == [], (frame, protocol.validate_frame(frame))
        items = (frame["items"] if frame["t"] == "batch" else [frame] if frame["t"] == "evt"
                 else [frame["snapshot"]] if "snapshot" in frame else [])
        for item in items:
            topic = item["topic"]
            assert item["rev"] > self.revs.get(topic, 0), ("rev", item)
            self.revs[topic] = item["rev"]
            if item["kind"] in {"edge", "gap"} and topic != "jobs":
                previous = self.seqs.get(topic, 0)
                if item["kind"] == "gap":
                    assert item["data"]["from_seq"] == previous + 1, ("gap", previous, item)
                else:
                    assert item["seq"] == previous + 1, ("seq", previous, item)
                self.seqs[topic] = item["seq"]
        self.frames.append(deepcopy(frame))
        return "complete"

    def poll_write(self):
        return "complete"

    def items(self, topic):
        out = []
        for frame in self.frames:
            items = (frame["items"] if frame["t"] == "batch" else [frame] if frame["t"] == "evt"
                     else [])
            out += [(item["kind"], item["data"]) for item in items if item["topic"] == topic]
        return out


def shutdown_dto(phase):
    return {"phase": phase, "recovery": {"proxy": "unknown", "firewall": "unknown"},
            "workers_pending": phase != "finished"}


def ticks(publisher, count=12):
    for _ in range(count):
        publisher.tick()


def test_shutdown_barrier_snapshot_is_not_followed_by_older_phases():
    """33-#12 and #11 on the shutdown topic, the T-P2-33 frame order."""
    state, wire = PublicationState(EPOCH), Wire()
    publisher = Publisher(state, wire)
    publisher.subscription_frames({"t": "subscribe", "epoch": EPOCH, "produced_at_ms": 1,
                                   "sent_at_ms": 1, "id": 1, "topics": ["shutdown"]})
    ticks(publisher, 2)
    held = publisher.snapshot_response({"t": "snapshot_req", "epoch": EPOCH, "produced_at_ms": 1,
                                        "sent_at_ms": 1, "id": 99, "topic": "shutdown"})
    assert held["error"]["code"] == "snapshot_required"      # no baseline at request time
    state.record_state("shutdown", shutdown_dto("draining"))
    for phase in ("stopping", "recovering", "waiting_workers", "finished"):
        state.record_edge("shutdown", shutdown_dto(phase))
    ticks(publisher)
    phases = [data["phase"] for _kind, data in wire.items("shutdown")]
    assert phases == ["finished"], phases          # covered edges dropped, not replayed
    reply = next(frame for frame in wire.frames if frame["t"] == "snapshot_res")
    assert reply["snapshot"]["data"]["phase"] == "finished"  # #11: answered with the baseline
    assert reply["snapshot"]["rev"] > max(frame["rev"] for frame in wire.frames
                                          if frame["t"] == "evt")
    # A later edge continues the receiver's seq at 1 (state seq 5).
    state.record_edge("shutdown", shutdown_dto("finished") | {"workers_pending": True})
    ticks(publisher, 3)
    assert wire.items("shutdown")[-1][0] == "edge" and wire.seqs["shutdown"] == 1
    assert state.pending(64) == []


def test_snapshot_res_of_an_ordinary_state_topic_drops_covered_edges():
    """33-#12 is general: service.state, snapshot_req while edges are queued."""
    from tests.test_bridge_p2_08 import dto
    state, wire = PublicationState(EPOCH), Wire()
    publisher = Publisher(state, wire)
    base = dto("service.state")
    state.record_state("service.state", base)
    publisher.subscription_frames({"t": "subscribe", "epoch": EPOCH, "produced_at_ms": 1,
                                   "sent_at_ms": 1, "id": 1, "topics": ["service.state"]})
    ticks(publisher, 3)
    state.record_edge("service.state", base)                 # seq 1 delivered normally
    ticks(publisher, 2)
    assert wire.seqs["service.state"] == 1
    older = deepcopy(base)
    older["runtime"]["startup_phase"] = "collecting"
    newer = deepcopy(base)
    newer["runtime"]["startup_phase"] = "core_standby"
    state.record_edge("service.state", older)                # seq 2, queued
    state.record_edge("service.state", newer)                # seq 3, queued
    publisher.snapshot_response({"t": "snapshot_req", "epoch": EPOCH, "produced_at_ms": 1,
                                 "sent_at_ms": 1, "id": 5, "topic": "service.state"})
    ticks(publisher)
    reply = next(frame for frame in wire.frames if frame["t"] == "snapshot_res")
    assert reply["snapshot"]["data"]["runtime"]["startup_phase"] == "core_standby"
    after = wire.frames[wire.frames.index(reply) + 1:]
    assert not any(item["topic"] == "service.state" for frame in after
                   for item in (frame.get("items") or []))   # no older edge after it
    latest = deepcopy(base)
    latest["runtime"]["startup_phase"] = "running"
    state.record_edge("service.state", latest)               # state seq 4 -> wire seq 2
    ticks(publisher, 3)
    assert wire.seqs["service.state"] == 2
    assert wire.items("service.state")[-1][1]["runtime"]["startup_phase"] == "running"


def test_a_covered_gap_is_dropped_and_later_gaps_stay_continuous():
    from tests.test_bridge_p2_08 import dto
    state, wire = PublicationState(EPOCH, edge_capacity=2), Wire()
    publisher = Publisher(state, wire)
    base = dto("service.state")
    publisher.subscription_frames({"t": "subscribe", "epoch": EPOCH, "produced_at_ms": 1,
                                   "sent_at_ms": 1, "id": 1, "topics": ["service.state"]})
    ticks(publisher, 2)                       # barrier waits: no baseline yet
    for phase in ("collecting", "core_standby", "running", "collecting"):
        changed = deepcopy(base)
        changed["runtime"]["startup_phase"] = phase
        state.record_edge("service.state", changed)    # overflows: gap + edges
    ticks(publisher)
    assert [kind for kind, _data in wire.items("service.state")] == ["snapshot"]
    for _ in range(3):                                  # overflow again, after the drop
        state.record_edge("service.state", base)
    ticks(publisher)
    kinds = [kind for kind, _data in wire.items("service.state")]
    assert kinds[0] == "snapshot" and "gap" in kinds    # Wire asserted gap.from_seq continuity


# ================================================== 7. C1-#2 private resolvers

@pytest.mark.parametrize("value", [
    "https://dns.nextdns.io/45f2a1", "https://45f2a1.dns.nextdns.io", "tls://45f2a1.dns.nextdns.io",
    "quic://45f2a1.dns.nextdns.io", "https://dns.nextdns.io/45f2a1/My-Laptop",
    "https://d.adguard-dns.com/dns-query/d1a2b3c4", "tls://d1a2b3c4.d.adguard-dns.com",
    "quic://d1a2b3c4.d.adguard-dns.com", "https://abcdef.dns.nextdns.io",
    "https://dns.example.invalid/private-path", "https://doh.example.invalid/dns-query?token=abc",
    "tls://9.9.9.9#RULES", "https://u:p" + "@dns.google/dns-query","https://dns.google/dns-query/",
    # fix round 1: an ID in any label left of the registrable domain, and any
    # name below a private-resolver zone (all-letter IDs included).
    "tls://abcdefgh.d.adguard-dns.com", "tls://laptop.x7ab23xy.d.adguard-dns.com",
    "tls://my-phone.abcd1234.d.adguard-dns.com", "https://qwertyuiop.dns.controld.com",
    "tls://xkqzpwrtab.dns.controld.com", "https://myprofile.dns.nextdns.io",
    "https://abc12345.doh.example.com/dns-query",
])
def test_private_or_doubtful_resolvers_are_secrets(value):
    from bridge_runtime_access import _config_secret_values
    assert not protocol.public_dns_resolver(value)
    table = _config_secret_values({"config": {"client": {"dns": {"nameserver": [value]}}}})
    assert value in table


@pytest.mark.parametrize("value", [
    "https://dns.google/dns-query", "tls://dns.google", "https://cloudflare-dns.com/dns-query",
    "https://1.1.1.1/dns-query", "1.1.1.1", "tls://dns.alidns.com", "https://dns.alidns.com/resolve",
    "https://dns.quad9.net/dns-query", "8.8.8.8:53", "[2606:4700:4700::1111]:853",
    "2606:4700:4700::1111", "quic://dns.adguard-dns.com", "system", "dhcp://en0",
    # fix round 1: the zones' own service names and public sub-names stay public.
    "tls://family.adguard-dns.com", "tls://unfiltered.adguard-dns.com",
    "https://d.adguard-dns.com/dns-query", "https://dns.nextdns.io", "tls://dns.controld.com",
    "https://security.cloudflare-dns.com/dns-query", "https://doh.opendns.com/dns-query",
])
def test_public_resolvers_stay_plain_configuration(value):
    from bridge_runtime_access import _config_secret_values
    assert protocol.public_dns_resolver(value)
    table = _config_secret_values({"config": {"client": {"dns": {"nameserver": [value]}}}})
    assert value not in table


# ===================================================== 6. C1-#1 logs wiring

def test_real_wiring_attaches_the_log_language_and_reason_sources():
    from bridge_logs import LogCollector
    state = PublicationState(EPOCH)
    logs = LogCollector(state, logger=logging.getLogger("test-h2-logs"))
    registered = []
    runtime = SimpleNamespace(config={"ui": {"language": "ja_JP"}}, api_client=None)
    session = SimpleNamespace(
        logs=logs, runtime=runtime,
        access=SimpleNamespace(core_fact=lambda: {"running": False}),
        register_query=lambda name, handler: registered.append(name),
        register_command=lambda name, handler: registered.append(name))
    wiring = bridge_wiring.Wiring(session)
    wiring._step("logs", wiring._logs)
    assert wiring.registered == ["logs"] and wiring.failed == []
    assert registered == ["logs.tail", "logs.query", "logs.setMihomoLevel"]
    assert logs._language_source() == "ja_JP"
    runtime.config["ui"]["language"] = "de_DE"           # follows the live config
    assert logs._language_source() == "de_DE"
    assert logs._feed_reason("reconnecting") == "core_not_running"
    session.access.core_fact = lambda: {"running": True}
    assert logs._feed_reason("stdout") == "controller_disabled"
    assert "logs" in bridge_wiring.REGISTRATION_ORDER


# ====================================================== 8. 31b-#18 geo.update

def _geo_wiring(enabled):
    config = {"geo_update": {} if enabled is None else {"enabled": enabled}}
    runtime = SimpleNamespace(config=config, trigger_geo_update=lambda force: (_ for _ in ()).throw(
        AssertionError("a disabled update must not be triggered")))
    return bridge_wiring.Wiring(SimpleNamespace(runtime=runtime))


def test_geo_update_reports_disabled_and_never_triggers():
    executor = make_executor()
    try:
        executor.register("geo.update", _geo_wiring(False)._geo_update)
        job = run_job(executor, "geo.update", {"force": True})
        assert job["state"] == "failed"
        assert job["error"] == {"code": "conflict", "message_key": "errors.geo.disabled",
                                "params": {}, "reason_code": "disabled"}
        assert job["result"]["triggered"] is False and job["result"]["completed"] is False
        assert all(errors == [] for errors in job_frame(executor))
    finally:
        executor.shutdown(time.monotonic() + 2)
    assert _geo_wiring(None)._geo_enabled() is True       # the updater's default
    assert _geo_wiring("false")._geo_enabled() is False


def test_zh_cn_catalog_has_the_new_message_keys():
    catalog = json.loads((ROOT / "ui/src/i18n/locales/zh_CN/errors.json").read_text(encoding="utf-8"))
    for key in ("errors.geo.disabled", "errors.mode.scope_isolated"):
        assert catalog[key]["m"] and catalog[key]["_desc"], key


# ============================================ C2 wiring: geo items, nodes.detail

def test_geo_status_carries_items_on_the_known_branch():
    from bridge_queries import ReadQueries
    lifecycle = json.loads((ROOT / "bridge_schema/fixtures/valid/topic_service_state.json")
                           .read_bytes())["data"]
    updater = object()
    runtime = SimpleNamespace(config={}, manager=None, geo_updater=updater,
                              geo_update_status=lambda: {"running": False,
                                  "last_update": {"geoip": "2026-10-01T00:00:00+00:00",
                                                  "geosite": "2026-10-01T00:00:00+00:00"},
                                  "errors": {"mmdb": "boom https://x/y"}})
    access = SimpleNamespace(runtime=runtime, lifecycle_status=lambda: deepcopy(lifecycle))
    data = ReadQueries(access).geo_status({}, None)
    assert [row["kind"] for row in data["items"]] == ["geoip", "geosite", "mmdb"]
    assert data["items"][2]["updated_at_ms"] is None
    assert protocol.validate_topic_payload("geo.status", data) == []
    assert "boom" not in json.dumps(data)


def test_nodes_detail_merges_endpoint_extras_and_uses_the_manager_interval():
    fixture = json.loads((FIXTURES / "p4c2_response_nodes.detail_endpoint.json").read_bytes())
    node = fixture["data"]["node"]
    endpoint = {"stale": False, "host": "gw-tyo01.example.org", "port": 443,
                "protocols": [{"key": "vless_reality", "port": 443, "node_id": node["id"]}]}
    runtime = SimpleNamespace(config={"tiers": {"tier1": {}}},
                              bridge_node_endpoint=lambda node_id: dict(endpoint))
    wiring = bridge_wiring.Wiring(SimpleNamespace(runtime=runtime, access=SimpleNamespace(runtime=runtime)))
    wiring.owners["nodes_latest"] = {"nodes": {"items": [deepcopy(node)]}}
    data = wiring._nodes_detail({"node_id": node["id"]}, None)
    assert data["endpoint"] == {"host_masked": "g•••.org", "port": 443}
    assert data["protocols"] == [{"name": "vless_reality", "port": 443, "node_id": node["id"]}]
    assert data["check_interval_s"] == 60            # Manager default, not the old 30
    frame = {**fixture, "data": data}
    assert protocol.validate_frame(frame) == []
    for value, expected in ((45, 45), ("90", 90), (0, 1), ("inherit", 60), (None, 60)):
        runtime.config = {"tiers": {"tier1": {"check_interval": value}}}
        assert wiring._check_interval("tier1") == expected, value
    runtime.bridge_node_endpoint = lambda node_id: dict(endpoint, stale=True)
    assert "endpoint" not in wiring._nodes_detail({"node_id": node["id"]}, None)


# =================================================== 9. C1-#6 static log gate

def test_apply_source_batch_reload_failure_is_a_raw_diagnostic(tmp_path):
    from tests.test_bridge_p2_13 import FakeRuntime
    runtime = FakeRuntime(tmp_path)
    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record)
    handler = Capture()
    runtime.logger.addHandler(handler)
    runtime.logger.setLevel(logging.DEBUG)
    try:
        runtime.reload_error = RuntimeError("https://example.test/secret-token")
        receipt = runtime.apply_source_batch(
            [{"op": "set_enabled", "section": "telegram_channels", "value": "@fixture_channel",
              "enabled": False}])
    finally:
        runtime.logger.removeHandler(handler)
    assert receipt["written"] is True and receipt["reloaded"] is False
    assert receipt["reload_error"] == "RuntimeError"
    warnings = [record for record in records if record.levelno == logging.WARNING]
    assert len(warnings) == 1 and "secret-token" not in warnings[0].getMessage()
    assert warnings[0].event_match_kind == "raw_only" and warnings[0].is_key_event is False


# ============================================================ fix round 1

def test_mode_set_is_refused_through_register_with_a_real_custom_instance_scope(tmp_path):
    """33-#17 end to end on the real pieces: bridge_instance_scope.resolve_scope
    builds the custom scope, bridge_node_commands.register(session) reads it."""
    import bridge_node_commands
    from bridge_instance_scope import resolve_scope
    config, data = tmp_path / "config", tmp_path / "data"
    config.mkdir()
    data.mkdir()
    scope = resolve_scope(environ={
        "TESSERACT_INSTANCE_SCOPE": "h2-fix", "TESSERACT_CONFIG_DIR": str(config),
        "TESSERACT_DATA_DIR": str(data), "APPDATA": str(tmp_path / "appdata"),
        "LOCALAPPDATA": str(tmp_path / "localappdata")})
    assert scope.kind == "custom" and scope.may_take_over_host_network is False
    port = ModePort(tmp_path / "mode.txt")
    before = port.path.read_bytes()
    executor = make_executor()
    session = SimpleNamespace(scope=scope, executor=executor, network_lock=threading.Lock(),
                              runtime=SimpleNamespace())
    try:
        bridge_node_commands.register(session, port=port)
        job = run_job(executor, "mode.set", {"mode": "tun"})
        assert job["state"] == "failed" and job["error"]["reason_code"] == "scope_isolated"
        assert job["error"]["code"] == "not_allowed"
        assert port.calls == [] and port.path.read_bytes() == before
    finally:
        executor.shutdown(time.monotonic() + 2)


def test_connect_creates_the_log_collector_before_the_wiring():
    """C1-#1: Wiring._logs reads session.logs, so BridgeSession.connect must
    build the LogCollector before it builds the Wiring."""
    import inspect
    source = inspect.getsource(bridge_main.BridgeSession.connect)
    collector = source.index("self.logs = LogCollector(")
    wiring = source.index("Wiring(self")
    assert collector < wiring


# Sandboxes of harnesses that start the real sidecar (scripts, shell/app
# smokes, bridge tests): every literal sources.yaml they write must carry the
# template migration markers, or the per-start bootstrap (33-#10) merges the
# default public source portfolio into them. A deliberate exception carries
# the comment ``sources-marker: intentional``.
SOURCES_MARKER = "tier3_source_portfolio_defaults_v1"
SOURCES_MARKER_CONSTANTS = ("SOURCE_MARKERS", "EMPTY_SOURCES")
# The six shell-lane sandboxes listed here were fixed by Claude on 10-12; nothing
# is pending any more. A new offender fails at once.
SOURCES_MARKER_PENDING: dict[str, int] = {}
_WRITE_CALLS = ("write_bytes(", "write_text(", "WriteAllText(", "WriteAllBytes(", "Set-Content", "Out-File")


def sources_marker_offenders(root: Path = ROOT) -> dict[str, list[int]]:
    files = [*root.glob("tests/test_bridge_*.py"), *root.glob("scripts/*.py"),
             *root.glob("scripts/*.ps1"), *root.glob("shell/app/*.py"), *root.glob("shell/app/src/*.py")]
    offenders: dict[str, list[int]] = {}
    for path in files:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for number, line in enumerate(lines, 1):
            if not any(call in line for call in _WRITE_CALLS):
                continue
            if "sources.yaml" not in line and "github_repos:" not in line:
                continue
            if (SOURCES_MARKER in line or any(name in line for name in SOURCES_MARKER_CONSTANTS)
                    or "sources-marker: intentional" in line):
                continue
            offenders.setdefault(path.relative_to(root).as_posix(), []).append(number)
    return offenders


def test_sidecar_sandboxes_write_sources_with_the_migration_markers():
    offenders = sources_marker_offenders()
    unexpected = {path: lines for path, lines in offenders.items()
                  if len(lines) > SOURCES_MARKER_PENDING.get(path, 0)}
    assert unexpected == {}, unexpected


def test_sources_marker_scan_is_not_vacuous(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "smoke.ps1").write_text(
        "(config/'sources.yaml').write_bytes(b'github_repos: []\\n')\n"  # sources-marker: intentional
        "(config/'sources.yaml').write_bytes(b'migrations:\\n  " + SOURCES_MARKER + ": true\\n')\n",  # sources-marker: intentional
        encoding="utf-8")
    assert sources_marker_offenders(tmp_path) == {"scripts/smoke.ps1": [1]}
    # The shared constant the pending sites should reuse carries the marker.
    from tests.test_bridge_integration import SOURCE_MARKERS
    assert SOURCES_MARKER.encode() in SOURCE_MARKERS
