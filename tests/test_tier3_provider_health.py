"""Unit tests for tier3_provider_health (single-provider parity, 11-02)."""

from __future__ import annotations

import threading
import time

from tier3_provider_health import (
    ALIVE,
    DEAD,
    UNKNOWN,
    LedgerEntry,
    Tier3HealthLedger,
    member_verdicts,
    order_tier3_members,
    overlay_provider_payload,
    verification_order,
    verify_provider_members,
)

URL = "https://www.gstatic.com/generate_204"


def _stamp(wall: float) -> str:
    from tier3_provider_health import _rfc3339

    return _rfc3339(wall)


def _member(name, *, alive=None, at=None, url=URL, global_alive=True, kind="Vless"):
    extra = {}
    if alive is not None:
        extra[url] = {"alive": alive, "history": [{"time": _stamp(at), "delay": 12 if alive else 0}]}
    return {"name": name, "type": kind, "alive": global_alive, "history": [], "extra": extra}


def _provider(*members):
    return {"name": "TIER3B-RUNTIME", "proxies": list(members) + [{"name": "REJECT", "type": "Reject", "alive": True, "extra": {}}]}


def test_verdicts_come_only_from_the_group_url():
    now = time.time()
    payload = _provider(
        _member("a", alive=True, at=now),
        _member("b", alive=False, at=now),
        _member("fresh"),  # rebuilt: global alive true, no extra
        _member("other", alive=True, at=now, url="http://cp.cloudflare.com"),
    )
    verdicts = member_verdicts(payload, URL)
    assert set(verdicts) == {"a", "b"}
    assert verdicts["a"][0] is True and verdicts["b"][0] is False


def test_verdicts_newer_than_filter_and_ledger_entries_ignored():
    now = time.time()
    payload = _provider(_member("old", alive=True, at=now - 10), _member("new", alive=False, at=now))
    assert set(member_verdicts(payload, URL, newer_than_wall=now - 1)) == {"new"}
    injected = overlay_provider_payload(_provider(_member("x")), URL, lambda _n: LedgerEntry(ALIVE, now))
    assert member_verdicts(injected, URL) == {}


def test_ledger_keeps_newest_verdict_and_dead_streak():
    ledger = Tier3HealthLedger()
    assert ledger.state("n1") == UNKNOWN
    assert ledger.observe("n1", False, 100.0)
    assert not ledger.observe("n1", True, 99.0)  # older
    assert ledger.observe("n1", False, 101.0)
    entry = ledger.entry("n1")
    assert entry.state == DEAD and entry.dead_streak == 2 and entry.dead_since_wall == 100.0
    assert ledger.dead_settled("n1", now_wall=101.0)
    assert ledger.observe("n1", True, 102.0)
    assert ledger.entry("n1") == LedgerEntry(ALIVE, 102.0)
    assert not ledger.dead_settled("n1")


def test_dead_settles_by_age_and_retain_forgets():
    ledger = Tier3HealthLedger()
    ledger.observe("n1", False, 1000.0)
    assert not ledger.dead_settled("n1", now_wall=1010.0)
    assert ledger.dead_settled("n1", now_wall=1061.0)
    ledger.observe("n2", True, 1000.0)
    assert ledger.retain(["n2"]) == 1
    assert ledger.state("n1") == UNKNOWN and ledger.state("n2") == ALIVE


def test_observe_payload_maps_names_to_identities():
    now = time.time()
    ledger = Tier3HealthLedger()
    payload = _provider(_member("A", alive=True, at=now), _member("B", alive=False, at=now), _member("C"))
    changed = ledger.observe_payload(payload, URL, {"A": "id-a", "B": "id-b", "C": "id-c"}.get)
    assert changed == 2
    assert ledger.state("id-a") == ALIVE and ledger.state("id-b") == DEAD and ledger.state("id-c") == UNKNOWN


def test_overlay_fills_untested_members_and_keeps_mihomo_verdicts():
    now = time.time()
    payload = _provider(
        _member("tested", alive=False, at=now),
        _member("carried"),
        _member("unknown"),
    )
    entries = {"carried": LedgerEntry(ALIVE, now - 30), "tested": LedgerEntry(ALIVE, now - 60)}
    patched = overlay_provider_payload(payload, URL, entries.get)
    by_name = {member["name"]: member for member in patched["proxies"]}
    assert by_name["tested"]["extra"][URL]["alive"] is False  # Mihomo wins
    assert by_name["carried"]["extra"][URL]["alive"] is True
    assert by_name["carried"]["extra"][URL]["ledger"] == ALIVE
    # The carried sample keeps the verdict's own time, never the read time.
    assert member_verdicts({"proxies": [dict(by_name["carried"], extra={URL: {**by_name["carried"]["extra"][URL], "ledger": None}})]}, URL)["carried"][1] < now - 29
    assert by_name["unknown"]["extra"][URL] == {"alive": False, "history": [], "ledger": UNKNOWN}
    assert by_name["REJECT"]["extra"] == {}
    assert payload["proxies"][1]["extra"] == {}  # input untouched


def test_order_keeps_loaded_order_when_membership_is_unchanged():
    states = {"a": DEAD, "b": ALIVE, "c": ALIVE}
    order = order_tier3_members(["c", "b", "a"], ["a", "b", "c"], pick="b", pick_alive=True,
                                state_of=states.get, dead_settled=lambda _i: True)
    assert order == ["a", "b", "c"]


def test_order_puts_the_live_pick_first_then_health_classes():
    states = {"a": DEAD, "b": ALIVE, "c": UNKNOWN, "d": ALIVE, "e": DEAD}
    order = order_tier3_members(
        ["a", "b", "c", "d", "e", "n1", "n2"], ["a", "b", "c", "d", "e"],
        pick="d", pick_alive=True, state_of=lambda i: states.get(i, UNKNOWN),
        dead_settled=lambda i: i == "e",
    )
    assert order == ["d", "b", "n1", "n2", "c", "a", "e"]


def test_order_keeps_a_kicked_pick_and_drops_other_kicked_members():
    states = {"a": ALIVE, "b": ALIVE, "c": DEAD}
    order = order_tier3_members(["b", "n1"], ["a", "b", "c"], pick="a", pick_alive=True,
                                state_of=states.get, dead_settled=lambda _i: False)
    assert order == ["a", "b", "n1"]
    # Once the kicked member no longer serves, it leaves.
    order = order_tier3_members(["b", "n1"], ["a", "b", "c"], pick="b", pick_alive=True,
                                state_of=states.get, dead_settled=lambda _i: False)
    assert order == ["b", "n1"]


def test_order_all_dead_moves_new_admissions_ahead():
    states = {"a": DEAD, "b": DEAD}
    order = order_tier3_members(["a", "b", "n1"], ["a", "b"], pick="a", pick_alive=False,
                                state_of=states.get, dead_settled=lambda _i: False)
    assert order == ["n1", "a", "b"]


def test_order_limit_drops_settled_dead_first_and_never_the_pick():
    states = {"p": ALIVE, "x": DEAD, "y": DEAD, "z": UNKNOWN}
    order = order_tier3_members(
        ["p", "x", "y", "z", "n1", "n2"], ["p", "x", "y", "z"], pick="p", pick_alive=True,
        state_of=lambda i: states.get(i, UNKNOWN), dead_settled=lambda i: i == "x", limit=4,
    )
    assert order == ["p", "n1", "n2", "z"]
    order = order_tier3_members(["p"], ["p", "x"], pick="p", pick_alive=True,
                                state_of=lambda i: states.get(i, UNKNOWN), dead_settled=lambda _i: True, limit=1)
    assert order == ["p"]


def test_verification_order_pick_successors_new_rest():
    names = ["p", "s1", "s2", "s3", "s4", "n1", "REJECT"]
    assert verification_order(names, pick="p", new=["n1"], successors=3) == ["p", "s1", "s2", "s3", "n1", "s4"]
    assert verification_order(["a", "b"], pick=None, new=[]) == ["a", "b"]


class _FakeCore:
    """Provider-scoped member tests landing on the current objects."""

    def __init__(self, members, *, slow=(), raise_for=()):
        self.members = list(members)
        self.verdicts = {}
        self.slow = set(slow)
        self.raise_for = set(raise_for)
        self.calls = []
        self.lock = threading.Lock()
        self.generation = 0

    def test(self, name):
        with self.lock:
            self.calls.append(name)
            generation = self.generation
        if name in self.raise_for:
            raise RuntimeError("controller unreachable")
        time.sleep(0.3 if name in self.slow else 0.01)
        with self.lock:
            if generation == self.generation:
                self.verdicts[name] = (not name.startswith("d"), time.time())

    def rebuild(self):
        with self.lock:
            self.generation += 1
            self.verdicts.clear()

    def read(self):
        with self.lock:
            members = [
                _member(name, alive=self.verdicts[name][0], at=self.verdicts[name][1])
                if name in self.verdicts else _member(name)
                for name in self.members
            ]
        return _provider(*members)


def test_verify_reads_verdicts_back_and_reports_untested():
    core = _FakeCore(["a", "d1", "b"], raise_for=["b"])
    started = time.time()
    outcome = verify_provider_members(
        ["a", "d1", "b"], test_member=core.test, read_provider=core.read, test_url=URL,
        started_wall=started, deadline_monotonic=time.monotonic() + 3.0,
    )
    assert outcome.alive == ("a",) and outcome.dead == ("d1",)
    assert outcome.untested == ("b",) and "b" in outcome.errors and outcome.orphaned == ()
    assert not outcome.complete


def test_verify_detects_orphaned_verdicts_after_a_rebuild():
    core = _FakeCore(["a", "s"], slow=["s"])
    started = time.time()
    timer = threading.Timer(0.1, core.rebuild)
    timer.start()
    outcome = verify_provider_members(
        ["a", "s"], test_member=core.test, read_provider=core.read, test_url=URL,
        started_wall=started, deadline_monotonic=time.monotonic() + 3.0,
    )
    timer.join()
    assert "s" in outcome.orphaned


def test_reorder_projection_moves_the_pick_first_and_keeps_main_yaml():
    import pytest
    import yaml
    from tier3_core_provider import Tier3CoreProviderError, project_tier3b_file_provider, reorder_tier3b_projection

    document = {
        "proxies": [{"name": name, "type": "http", "server": "127.0.0.1", "port": 1000 + i}
                    for i, name in enumerate(["a", "b", "c"])],
        "proxy-groups": [
            {"name": "TIER3-AUTO", "type": "fallback", "proxies": ["a", "b", "c", "REJECT"]},
            {"name": "PROXY-FALLBACK", "type": "fallback", "proxies": ["TIER3-AUTO"]},
        ],
    }
    projection = project_tier3b_file_provider(document, ["a", "b", "c"])
    moved = reorder_tier3b_projection(projection, ["c", "a", "b"])
    assert moved.main_yaml == projection.main_yaml
    assert moved.proxy_names == ("c", "a", "b")
    assert moved.provider_member_names == ("c", "a", "b", "REJECT")
    assert [item["name"] for item in yaml.safe_load(moved.provider_yaml)["proxies"]] == ["c", "a", "b", "REJECT"]
    assert moved.provider_digest != projection.provider_digest
    assert reorder_tier3b_projection(projection, ["a", "b", "c"]) is projection
    with pytest.raises(Tier3CoreProviderError):
        reorder_tier3b_projection(projection, ["a", "b"])


def _tier3_names(nodes, *, stable=True):
    import yaml
    from clash_formatter import build_full_clash_config

    document = yaml.safe_load(build_full_clash_config(
        {"tier1": [], "tier2": [], "tier3": nodes, "exit": []}, stable_tier3_names=stable))
    group = next(item for item in document["proxy-groups"] if item["name"] == "TIER3-AUTO")
    by_server = {proxy["name"]: proxy["port"] for proxy in document["proxies"]}
    return {by_server[name]: name for name in group["proxies"] if name in by_server}


def test_stable_tier3_names_do_not_move_between_same_base_members():
    from models import Node

    nodes = [Node(name="IT", protocol="trojan", address="127.0.0.1", port=port, password="p")
             for port in (1001, 1002, 1003)]
    loaded = _tier3_names(nodes)
    assert len(set(loaded.values())) == 3 and all(name.startswith("IT·") for name in loaded.values())
    # Pick-first reorder and a kick of the first member rename nobody.
    assert _tier3_names([nodes[2], nodes[0], nodes[1]]) == loaded
    assert _tier3_names(nodes[1:]) == {1002: loaded[1002], 1003: loaded[1003]}
    # A changed definition is a new name (remove plus add), not a reused one.
    changed = Node(name="IT", protocol="trojan", address="127.0.0.1", port=1002, password="other")
    assert _tier3_names([nodes[0], changed, nodes[2]])[1002] != loaded[1002]
    # Off: today's positional names.
    assert sorted(_tier3_names(nodes, stable=False).values()) == ["IT", "IT-2", "IT-3"]


class _ProviderController:
    """Mihomo's provider-load rules for write_provider_file_verified."""

    def __init__(self, path, loaded, *, put=("ok",), reads=None):
        self.path = path
        self.loaded = list(loaded)
        self.put_plan = list(put)
        self.reads = list(reads) if reads is not None else None
        self.puts = 0

    def activate(self):
        outcome = self.put_plan[min(self.puts, len(self.put_plan) - 1)]
        self.puts += 1
        if outcome == "reject":
            raise RuntimeError("PUT /providers/proxies/TIER3B-RUNTIME failed: HTTP 503 Service Unavailable")
        if outcome == "transport":
            raise RuntimeError("PUT /providers/proxies/TIER3B-RUNTIME failed: timed out")
        self.load()

    def load(self):
        import yaml

        self.loaded = [item["name"] for item in yaml.safe_load(self.path.read_text("utf-8"))["proxies"]]

    def readback(self):
        if self.reads is not None and self.reads:
            step = self.reads.pop(0)
            if step == "error":
                raise RuntimeError("GET failed")
            if step == "watcher":
                self.load()
        return {"name": "TIER3B-RUNTIME", "proxies": [{"name": name} for name in self.loaded]}


def _verified_write(tmp_path, controller, new_names):
    import yaml
    from tier3_core_provider import write_provider_file_verified

    content = yaml.safe_dump({"proxies": [{"name": name, "type": "http"} for name in new_names]})
    return write_provider_file_verified(
        controller.path, content, provider_name="TIER3B-RUNTIME", expected_proxy_names=new_names,
        previous_proxy_names=["a", "REJECT"], activate=controller.activate, readback=controller.readback,
        is_rejection=lambda exc: "HTTP 503" in str(exc), sleep=lambda _s: None,
    )


def _provider_file(tmp_path, names):
    import yaml

    path = tmp_path / "tier3b-runtime.yaml"
    path.write_text(yaml.safe_dump({"proxies": [{"name": name, "type": "http"} for name in names]}), "utf-8")
    return path


def test_verified_write_applied_rejected_and_transport_outcomes(tmp_path):
    path = _provider_file(tmp_path, ["a", "REJECT"])
    previous = path.read_bytes()

    ok = _verified_write(tmp_path, _ProviderController(path, ["a", "REJECT"]), ["a", "b", "REJECT"])
    assert ok.applied and not ok.rejected and not ok.unknown and ok.changed

    path.write_bytes(previous)
    rejected_controller = _ProviderController(path, ["a", "REJECT"], put=("reject",))
    rejected = _verified_write(tmp_path, rejected_controller, ["a", "bad", "REJECT"])
    assert rejected.rejected and path.read_bytes() == previous  # restore is a Mihomo no-op
    assert rejected_controller.puts == 1

    # Transport failures: the file watcher loads the file anyway; never restore.
    transport_controller = _ProviderController(path, ["a", "REJECT"], put=("transport",), reads=["watcher"])
    applied_by_watcher = _verified_write(tmp_path, transport_controller, ["a", "c", "REJECT"])
    assert applied_by_watcher.applied and transport_controller.puts == 3
    assert b"c" in path.read_bytes()

    path.write_bytes(previous)
    silent = _verified_write(tmp_path, _ProviderController(path, ["a", "REJECT"], put=("transport",),
                                                          reads=["error", "error", "error"]),
                             ["a", "d", "REJECT"])
    assert silent.unknown and b"d" in path.read_bytes()


def test_provider_health_check_block_repeats_the_group_contract():
    import yaml
    from tier3_core_provider import project_tier3b_file_provider

    document = {
        "proxies": [{"name": "a", "type": "http", "server": "127.0.0.1", "port": 1}],
        "proxy-groups": [
            {"name": "TIER3-AUTO", "type": "fallback", "proxies": ["a", "REJECT"],
             "url": "https://www.google.com/generate_204", "interval": 120, "timeout": 5000,
             "expected-status": 204},
            {"name": "PROXY-FALLBACK", "type": "fallback", "proxies": ["TIER3-AUTO"]},
        ],
    }
    plain = yaml.safe_load(project_tier3b_file_provider(document, ["a"]).main_yaml)
    assert "health-check" not in plain["proxy-providers"]["TIER3B-RUNTIME"]
    checked = project_tier3b_file_provider(document, ["a"], provider_health_check=True)
    block = yaml.safe_load(checked.main_yaml)["proxy-providers"]["TIER3B-RUNTIME"]["health-check"]
    assert block == {"enable": True, "url": "https://www.google.com/generate_204", "interval": 120,
                     "timeout": 5000, "lazy": False, "expected-status": 204}
    # Invariant across inventories: the block never names members.
    other = project_tier3b_file_provider(
        {**document, "proxies": [*document["proxies"], {"name": "b", "type": "http", "server": "127.0.0.1", "port": 2}],
         "proxy-groups": [{**document["proxy-groups"][0], "proxies": ["a", "b", "REJECT"]}, document["proxy-groups"][1]]},
        ["a", "b"], provider_health_check=True)
    assert other.main_yaml == checked.main_yaml


def _runtime_stub(**overrides):
    import threading
    from types import SimpleNamespace
    from desktop_runtime import DesktopRuntime

    runtime = DesktopRuntime.__new__(DesktopRuntime)
    runtime.config = {}
    runtime.stop_event = threading.Event()
    runtime._core_lifecycle_lock = threading.Lock()
    runtime._core_generation = 3
    runtime._runtime_initial_activation_generation = 3
    runtime._core_runtime_phase = "formal_capture"
    runtime._tier3_initial_activation_transaction_active = lambda: False
    runtime._runtime_recovery_transaction_active = lambda: False
    runtime._node_judgement_state = lambda: SimpleNamespace(judge=None)
    runtime._current_core_runtime_phase = lambda: runtime._core_runtime_phase
    for key, value in overrides.items():
        setattr(runtime, key, value)
    return runtime


def test_verified_lane_opens_only_after_this_core_reached_ready_and_follows_the_switch():
    runtime = _runtime_stub()
    assert runtime._tier3_verified_lane_open()
    assert runtime._tier3_verified_online_write_state() == (True, "eligible")
    runtime._runtime_initial_activation_generation = 2  # an older Core's activation
    assert not runtime._tier3_verified_lane_open()
    runtime._runtime_initial_activation_generation = 3
    runtime.config = {"tiers": {"tier3": {"verified_provider_writes": False}}}
    assert not runtime._tier3_verified_lane_open()
    runtime.config = {}
    runtime._runtime_recovery_transaction_active = lambda: True
    assert runtime._tier3_verified_online_write_state() == (False, "recovery_active")


def test_judge_admission_sees_a_verified_provider_write_in_flight():
    import threading

    runtime = _runtime_stub(_runtime_failure_monitor_lock=threading.Lock())
    runtime._route_lease_fail_closed = lambda _lease: False
    assert runtime._runtime_post_dial_d4_txn_busy() is None
    runtime._tier3_provider_write_busy = True
    assert runtime._runtime_post_dial_d4_txn_busy() == "d4_txn_busy:provider_write"


def test_pin_owner_is_a_user_pick_a_mutation_lease_or_a_live_route_lease():
    from types import SimpleNamespace

    runtime = _runtime_stub()
    runtime._route_lease_counters = {}
    runtime._route_lease_clocks = {}
    runtime._runtime_route_mutation_lease = None
    assert runtime._tier3_pin_owner("TIER3-AUTO") == ""  # restored from cache.db
    runtime._route_lease_counters = {"_user_groups": {"TIER3-AUTO"}}
    assert runtime._tier3_pin_owner("TIER3-AUTO") == "user"
    runtime._route_lease_counters = {}
    runtime._runtime_route_mutation_lease = {"group": "TIER3-AUTO", "mode": "fixed"}
    assert runtime._tier3_pin_owner("TIER3-AUTO") == "mutation_lease"
    runtime._runtime_route_mutation_lease = None
    runtime._route_lease_clocks = {"TIER3-AUTO": SimpleNamespace(live=True)}
    assert runtime._tier3_pin_owner("TIER3-AUTO") == "route_lease"
    runtime._route_lease_clocks = {"TIER3-AUTO": SimpleNamespace(live=False)}
    assert runtime._tier3_pin_owner("TIER3-AUTO") == ""


def test_verify_stops_early_when_the_caller_is_satisfied():
    core = _FakeCore(["p", "x"], slow=["x"])
    outcome = verify_provider_members(
        ["p", "x"], test_member=core.test, read_provider=core.read, test_url=URL,
        started_wall=time.time(), deadline_monotonic=time.monotonic() + 3.0,
        stop_when=lambda verdicts: "p" in verdicts, read_every=0.02,
    )
    assert "p" in outcome.verdicts and "x" in outcome.untested
    assert outcome.elapsed_ms < 290
