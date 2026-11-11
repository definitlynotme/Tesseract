"""The ship act-arm package end to end on the bundled Mihomo 1.19.27 (10-30).

ship2 (33 h) was wasted because ``_runtime_post_dial_exhausted_admission``
handed the STORED publication wrapper ``{"proxies": <GET /proxies>,
"proxy_map": ...}`` to ``_runtime_tier3_route_view``, which unwrapped once:
every TIER3-AUTO member read dead, the group "exhausted", and every act
trigger was gated ``exhausted_hold``.  The unit tests used a hand-made flat
payload and a stubbed route view, so nothing caught it.

Here nothing on that path is hand-made: an isolated temp profile, a real
Mihomo 1.19.27 process on 127.0.0.1 with local HTTP-proxy nodes and a local
204 origin, the production projection (``project_tier3b_file_provider``),
the real ``_refresh_runtime_route_snapshot`` publication (so the stored
payload has exactly the production shape), the real arm, the real observer
and keep-alive controller, the real judge service with real ``GET /proxies``
/ provider reads and real ``/proxies/<n>/delay`` contract probes.

Stubbed (inputs that are not on the path under test): the Tier3 manager
(``FakeManager``-like health sink), the online phase / activation flags, the
proxy-name map (the projection's own names), and the trigger itself: a real
``runtime_post_dial.Trigger`` handed to ``_on_runtime_post_dial_trigger``
(the observer's FAST rule detection is covered by its own tests).

Every key of the ship act package is on (act, rearm, route_lease,
tier_ladder_judge, overshoot_repair, exhausted_pool_reexport, exhausted_hold,
not_reproduced_override, post_dial_provisional_move,
post_dial_successor_from_history, post_dial_close_frozen_streams).
"""

from __future__ import annotations

import logging
import socketserver
import subprocess
import threading
import time
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from desktop_runtime import DesktopRuntime
from mihomo_api import MihomoAPIClient
from runtime_egress_recovery import RuntimeEgressReadiness, RuntimeEgressReadinessState
from runtime_post_dial import Trigger
from runtime_route_audit import mihomo_proxies_entries
from tier3_core_provider import project_tier3b_file_provider
from tests.test_runtime_sudden_failure import _Origin, _Proxy, _reserve_listener, _serve

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "bin" / "mihomo.exe"
MEMBERS = ("n1", "n2", "n3")

SHIP_ACT_ARM = {
    "post_dial_judge": "act",
    "durable_conviction": False,
    "rearm_on_stale_route": True,
    "tier_ladder_judge": True,
    "overshoot_repair": True,
    "exhausted_pool_reexport": True,
    "exhausted_hold": True,
    "not_reproduced_override": True,
    "post_dial_provisional_move": True,
    "post_dial_fast_successor": False,
    "post_dial_successor_from_history": True,
    "post_dial_close_frozen_streams": True,
}


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.messages.append(record.getMessage())
        except Exception:
            pass


class _Manager:
    """The Tier3 manager's health sink (not on the path under test)."""

    def __init__(self, runtime_generation: int) -> None:
        self.lock = threading.RLock()
        self.runtime_generation = runtime_generation
        self.tier3_pool_revision = 1
        self.effective_egress_ok = True
        self.observations: list[dict] = []

    def _tier3_enabled(self) -> bool:
        return True

    def apply_runtime_health_observation(self, observation: dict) -> dict:
        self.observations.append(dict(observation))
        return {"applied": True}

    def enqueue_runtime_leaf_recheck(self, node: Any, **kwargs: Any) -> None:
        pass


def _document(nodes, *, mixed_port, controller_port, secret, url):
    contract = {"url": url, "interval": 3600, "lazy": False, "timeout": 5000,
                "expected-status": 204, "max-failed-times": 1}
    return {
        "mixed-port": mixed_port, "allow-lan": False, "bind-address": "127.0.0.1",
        "mode": "rule", "log-level": "info", "ipv6": False,
        "external-controller": f"127.0.0.1:{controller_port}", "secret": secret,
        "proxies": [
            {"name": name, "type": "http", "server": "127.0.0.1", "port": nodes[name].server_address[1]}
            for name in MEMBERS
        ],
        "proxy-groups": [
            {"name": "TIER3-AUTO", "type": "fallback", "empty-fallback": "REJECT",
             "proxies": [*MEMBERS, "REJECT"], **contract},
            {"name": "PROXY-FALLBACK", "type": "fallback", "empty-fallback": "REJECT",
             "proxies": ["TIER3-AUTO"], **contract},
            {"name": "FINAL-EXIT", "type": "select", "empty-fallback": "REJECT",
             "proxies": ["PROXY-FALLBACK", "REJECT"]},
            {"name": "GENERAL", "type": "select", "empty-fallback": "REJECT", "proxies": ["FINAL-EXIT"]},
            {"name": "GLOBAL", "type": "select", "empty-fallback": "REJECT",
             "proxies": ["GENERAL", "FINAL-EXIT"]},
        ],
        "rules": ["MATCH,GENERAL"],
    }


def _proxy_map(projection):
    provider = yaml.safe_load(projection.provider_yaml)
    return {
        str(proxy["name"]): {
            "proxy_name": str(proxy["name"]), "tier": "tier3", "pool_kind": "tier3b",
            "identity": f"identity-{proxy['name']}", "node": None, "proxy": dict(proxy),
        }
        for proxy in provider["proxies"] if str(proxy.get("name") or "") != "REJECT"
    }


class RealCore:
    """One isolated real-Core runtime (temp profile, local targets only)."""

    def __init__(self, stack: ExitStack, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TESSERACT_CONFIG_DIR", str(tmp_path / "config-dir"))
        monkeypatch.setenv("TESSERACT_DATA_DIR", str(tmp_path / "data-dir"))
        self.stack = stack
        self.origin = _serve(stack, ThreadingHTTPServer(("127.0.0.1", 0), _Origin))
        self.nodes = {name: _serve(stack, _Proxy()) for name in MEMBERS}
        mixed_port, mixed_reservation = _reserve_listener(stack, udp=True)
        controller_port, controller_reservation = _reserve_listener(stack)
        secret = "ship-act-arm-secret"
        self.url = f"http://127.0.0.1:{self.origin.server_port}/generate_204"
        projection = project_tier3b_file_provider(
            _document(self.nodes, mixed_port=mixed_port, controller_port=controller_port,
                      secret=secret, url=self.url),
            list(MEMBERS))
        self.projection = projection
        config = {
            "client": {
                "mihomo_runtime_config": str(tmp_path / "config" / "mihomo-runtime.yaml"),
                "mihomo_work_dir": str(tmp_path / "core-home"),
                "runtime_state_path": str(tmp_path / "runtime-state.json"),
                "egress_recovery": {
                    "node_judgement": dict(SHIP_ACT_ARM),
                    "route_lease": {"enabled": True},
                },
            },
            "alerts": {"log_file": str(tmp_path / "monitor.log"), "desktop": {"enabled": False}},
        }
        runtime = DesktopRuntime(config, project_root=tmp_path)
        for handler in list(runtime.logger.handlers):
            handler.close()
            runtime.logger.removeHandler(handler)
        logger = logging.getLogger(f"ship-act-arm-real-core.{id(runtime)}")
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        self.log = _Records()
        logger.handlers[:] = [self.log]
        runtime.logger = logger
        self.runtime = runtime
        self.records: list[dict] = []
        real_log = runtime._log_runtime_post_dial_record

        def spy(level: str, record: dict) -> None:
            self.records.append(dict(record))
            real_log(level, record)

        runtime._log_runtime_post_dial_record = spy
        runtime._tier3_core_provider_name = "TIER3B-RUNTIME"
        runtime._tier3_core_provider_filename = "tier3b-runtime.yaml"
        config_path = runtime._runtime_config_path_for_client()
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(projection.main_yaml, encoding="utf-8")
        provider_path = runtime._tier3_core_provider_file_path()
        provider_path.parent.mkdir(parents=True, exist_ok=True)
        provider_path.write_text(projection.provider_yaml, encoding="utf-8")
        mixed_reservation.close()
        controller_reservation.close()
        self.process = subprocess.Popen(
            [str(BINARY), "-d", str(provider_path.parent), "-f", str(config_path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        stack.callback(self._stop_core)
        self.core_lines: list[str] = []

        def pump() -> None:
            # What MihomoCoreRunner's stdout reader does: every Core line
            # goes to the runtime's output callback (the post-dial tap feeds
            # the observer from it).
            from core_runner import CoreOutputEvent

            for raw in self.process.stdout:
                line = raw.decode("utf-8", "replace").rstrip()
                self.core_lines.append(line)
                try:
                    self.runtime._on_core_output_event(CoreOutputEvent(
                        raw_line=line, pid=self.process.pid, process_generation=1,
                        received_at_monotonic=time.monotonic()))
                except Exception:
                    pass

        threading.Thread(target=pump, daemon=True).start()
        self.client = MihomoAPIClient(base_url=f"http://127.0.0.1:{controller_port}", secret=secret, timeout=2.0)
        deadline = time.monotonic() + 15
        while True:
            try:
                self.client.proxy("TIER3-AUTO")
                break
            except Exception:
                if self.process.poll() is not None or time.monotonic() > deadline:
                    pytest.fail("bundled Mihomo did not start:\n" + "\n".join(self.core_lines[-40:]))
                time.sleep(.05)
        self.client.select_proxy("FINAL-EXIT", "PROXY-FALLBACK", timeout=2)
        runtime.api_client = self.client
        runtime.core_runner = SimpleNamespace(
            is_running=lambda: True,
            status=lambda: {"pid": self.process.pid, "process_generation": 1},
        )
        runtime._core_generation = 1
        runtime._core_runtime_phase = "formal_capture"
        runtime._runtime_initial_activation_generation = 1
        runtime._runtime_proxy_name_map = lambda: _proxy_map(projection)
        loaded = runtime._runtime_export_contract_for_payload(
            projection.main_yaml, provider_payload=projection.provider_yaml)
        loaded["runtime_config_path"] = str(config_path)
        runtime._runtime_loaded_export_contract = {**loaded, "revision": 1}
        runtime._runtime_loaded_export_revision = 1
        runtime._tier3_core_provider_last_projection = projection
        self.manager = _Manager(int(runtime._runtime_generation))
        runtime.manager = self.manager
        runtime._runtime_egress_readiness = RuntimeEgressReadiness(
            state=RuntimeEgressReadinessState.READY, validation_active=False)
        stack.callback(self._stop_runtime)

    def _stop_core(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(5)

    def _stop_runtime(self) -> None:
        runtime = self.runtime
        runtime.stop_event.set()
        observer = runtime.__dict__.get("_runtime_post_dial_observer")
        if observer is not None:
            try:
                observer.stop("test")
                observer.join(3.0)
            except Exception:
                pass
        state = runtime.__dict__.get("_node_judgement_runtime_state")
        judge = getattr(state, "judge", None)
        if judge is not None:
            judge.wait_idle(10.0)
        event = runtime.__dict__.get("_runtime_overshoot_event")
        if event is not None:
            event.set()

    # -- helpers
    def health_check_members(self, alive_expected: set[str]) -> None:
        """Real native samples at the group contract (what the group's own
        health check writes into /proxies)."""

        for name in MEMBERS:
            try:
                self.client.proxy_delay(name, url=self.url, timeout_ms=1500, expected_status=204,
                                        verify_expected_status=True)
            except Exception:
                pass
        entries = mihomo_proxies_entries(self.client.proxies())
        from runtime_node_judge import member_alive

        alive = {name for name in MEMBERS if member_alive(entries.get(name), self.url)}
        assert alive == alive_expected, (alive, entries.get("n1"))

    def publish(self) -> Any:
        snapshot = self.runtime._refresh_runtime_route_snapshot(timeout_seconds=3.0)
        assert snapshot is not None
        return snapshot

    def arm(self) -> None:
        assert self.runtime._try_arm_runtime_recovery() is True

    def trigger(self, leaf: str, *, episode: int = 1, n_failed: int = 5) -> None:
        now = time.time()
        trigger = Trigger(kind="FAST", leaf=leaf, tokens=("tok-a", "tok-b"), t=now, n_events=2,
                          first_event_t=now - 0.05, episode=episode, new_episode=True,
                          share_failed=n_failed, share_answered=0)
        runtime = self.runtime
        mode = runtime._runtime_post_dial_mode()[0]
        assert mode == "act"
        runtime._on_runtime_post_dial_trigger(trigger, mode=mode,
                                              tokenizer=runtime._node_judgement_state().tokenizer)

    def wait_judge(self, timeout: float = 30.0) -> None:
        judge = self.runtime._runtime_post_dial_judge()
        assert judge.wait_idle(timeout)

    def evs(self, ev: str) -> list[dict]:
        return [r for r in list(self.records) if r.get("ev") == ev]


@pytest.fixture
def core(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    if not BINARY.is_file():
        pytest.skip("repository Mihomo is unavailable")
    with ExitStack() as stack:
        yield RealCore(stack, tmp_path, monkeypatch)


def test_stored_publication_has_the_wrapper_shape_and_one_accessor_reads_it(core: RealCore) -> None:
    core.health_check_members({"n1", "n2", "n3"})
    snapshot = core.publish()
    stored = core.runtime._runtime_route_snapshot_payload
    snap2, refs_payload = core.runtime._runtime_post_dial_route_refs()
    assert snap2 is snapshot and refs_payload is stored
    # The production shape: a wrapper around the whole controller response.
    assert set(stored) == {"proxies", "proxy_map"}
    assert set(stored["proxies"]) == {"proxies"}
    entries = mihomo_proxies_entries(stored)
    assert entries == mihomo_proxies_entries(stored["proxies"])
    assert {"n1", "n2", "n3", "TIER3-AUTO", "FINAL-EXIT"} <= set(entries)
    assert core.runtime._runtime_proxy_entries(stored) is entries or core.runtime._runtime_proxy_entries(stored) == entries
    # A bare entries map (the old hand-made fixture shape) is not accepted.
    assert mihomo_proxies_entries(entries) == {}
    # Both shapes give the same TIER3-AUTO view.
    view_stored = core.runtime._runtime_tier3_route_view(snapshot, stored)
    view_controller = core.runtime._runtime_tier3_route_view(snapshot, stored["proxies"])
    assert view_stored == view_controller
    assert view_stored["alive"] == 3 and view_stored["loaded"] == 3 and view_stored["exhausted"] is False


def test_healthy_group_is_not_held_and_a_judgement_runs_a_c(core: RealCore) -> None:
    """The ship2 regression: on the old code this trigger was gated
    ``exhausted_hold`` and no A_C ran."""

    core.health_check_members({"n1", "n2", "n3"})
    core.publish()
    core.arm()
    judge = core.runtime._runtime_post_dial_judge()
    assert core.runtime._runtime_post_dial_exhausted_admission(judge, "n1") is False
    assert judge.gates.hold is None
    core.nodes["n1"].zombie.set()
    core.trigger("n1")
    core.wait_judge()
    gated = [r for r in core.evs("gated")]
    assert all(r.get("reason") != "exhausted_hold" for r in gated), gated
    assert not core.evs("hold_entered"), core.evs("hold_entered")
    probes = [r for r in core.records if r.get("ev") in {"probe_result", "rung_probe_result"}]
    assert any(r.get("name") == "A_C" for r in probes), core.records
    verdicts = [r for r in core.records if r.get("ev") in {"verdict", "rung_verdict"}]
    assert verdicts, core.records
    print("\nHEALTHY records:", [(r.get("ev"), r.get("name") or r.get("verdict") or r.get("reason") or r.get("outcome"))
                                for r in core.records])


def test_truly_exhausted_group_enters_the_hold(core: RealCore) -> None:
    core.publish()
    core.arm()
    for name in MEMBERS:
        core.nodes[name].refused.set()
    core.health_check_members(set())
    core.publish()
    snapshot, stored = core.runtime._runtime_post_dial_route_refs()
    view = core.runtime._runtime_tier3_route_view(snapshot, stored)
    assert view["exhausted"] is True and view["alive"] == 0
    core.trigger("n1")
    core.wait_judge()
    held = core.evs("hold_entered")
    assert held and held[0].get("cause") == "group_exhausted", core.records
    assert core.runtime._runtime_post_dial_judge().gates.hold is not None
    assert not [r for r in core.records if r.get("ev") in {"probe_result", "rung_probe_result"}]


def test_provisional_move_fires_on_a_slow_accused(core: RealCore) -> None:
    """n1 black-holes (A_C runs into its 5 s contract timeout): at
    provisional_move_s the rung-1 group is pinned to the successor through the
    route-lease fixed write, on the real Core."""

    core.health_check_members({"n1", "n2", "n3"})
    core.publish()
    core.arm()
    core.nodes["n1"].failed.set()
    core.trigger("n1")
    core.wait_judge(40.0)
    moves = core.evs("provisional_move")
    print("\nPROVISIONAL records:", [(r.get("ev"), {k: v for k, v in r.items() if k not in {"t"}})
                                     for r in core.records if r.get("ev") not in {"arm_state"}])
    assert moves, core.records
    assert any(str(m.get("outcome") or m.get("reason") or "") == "moved" or m.get("moved") in {True, 1, "1"}
               for m in moves), moves


class _FreezableTicker(socketserver.ThreadingTCPServer):
    """A stream at ~1.3 MB/s that stops sending (keeps the socket open) once
    ``frozen`` is set: a frozen stream, as rule 6 defines it."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self) -> None:
        class Handler(socketserver.BaseRequestHandler):
            def handle(inner) -> None:
                server = inner.server
                try:
                    while not server.shutdown_event.is_set():
                        if server.frozen.is_set():
                            time.sleep(.05)
                            continue
                        inner.request.sendall(b"x" * 65536)
                        time.sleep(.05)
                except OSError:
                    server.resets += 1

        super().__init__(("127.0.0.1", 0), Handler)
        self.frozen = threading.Event()
        self.resets = 0

    def handle_error(self, *_args: Any) -> None:
        pass


def _open_tunnel(mixed_port: int, target_port: int):
    import socket

    sock = socket.create_connection(("127.0.0.1", mixed_port), timeout=5)
    sock.sendall(f"CONNECT 127.0.0.1:{target_port} HTTP/1.1\r\nHost: 127.0.0.1:{target_port}\r\n\r\n".encode("ascii"))
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = sock.recv(1)
        assert chunk, "tunnel closed during CONNECT"
        head += chunk
    assert b" 200" in head.split(b"\r\n", 1)[0], head
    sock.settimeout(None)
    return sock


def test_frozen_stream_on_the_convicted_leaf_is_closed(core: RealCore) -> None:
    core.health_check_members({"n1", "n2", "n3"})
    core.publish()
    core.arm()
    ticker = _serve(core.stack, _FreezableTicker())
    mixed_port = int(yaml.safe_load(core.projection.main_yaml)["mixed-port"])
    sock = _open_tunnel(mixed_port, ticker.server_address[1])
    core.stack.callback(sock.close)
    state = {"total": 0, "eof": False, "error": None}

    def read() -> None:
        try:
            while True:
                data = sock.recv(65536)
                if not data:
                    state["eof"] = True
                    return
                state["total"] += len(data)
        except OSError as exc:
            state["error"] = exc

    threading.Thread(target=read, daemon=True).start()
    time.sleep(2.5)  # several observer polls see the stream at full rate
    assert state["total"] > 0
    conns = [c for c in (core.client.connections().get("connections") or [])
             if str((c.get("metadata") or {}).get("destinationPort")) == str(ticker.server_address[1])]
    assert len(conns) == 1 and conns[0]["chains"][0] == "n1", conns
    ticker.frozen.set()
    core.nodes["n1"].zombie.set()
    core.trigger("n1")
    core.wait_judge()
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline and not (state["eof"] or state["error"]):
        time.sleep(.1)
    closes = core.evs("close")
    print("\nFROZEN records:", [
        {k: v for k, v in r.items()
         if k not in {"t", "v", "mono_ns", "instance", "seq", "loss_total", "_arm_enveloped"}}
        for r in core.records if r.get("ev") not in {"arm_state", "arm_diagnostics_state"}
    ], "reader:", state)
    assert any(int(r.get("frozen") or 0) >= 1 and int(r.get("count") or 0) >= 1 for r in closes), core.records
    assert state["eof"] or state["error"] is not None
    left = [c for c in (core.client.connections().get("connections") or [])
            if str((c.get("metadata") or {}).get("destinationPort")) == str(ticker.server_address[1])]
    assert left == [], left


def test_successor_history_hook_reads_the_real_provider_payload(core: RealCore) -> None:
    """post_dial_successor_from_history: the hook's provider parser on the
    real ``GET /providers/proxies/TIER3B-RUNTIME`` after real samples."""

    core.health_check_members({"n1", "n2", "n3"})
    core.publish()
    job = SimpleNamespace(context=SimpleNamespace(provider_name="TIER3B-RUNTIME"), test_url=core.url)
    assert core.runtime._runtime_post_dial_successor_history(job, "n2") is True
    core.nodes["n3"].refused.set()
    core.health_check_members({"n1", "n2"})
    assert core.runtime._runtime_post_dial_successor_history(job, "n3") is False
    other = SimpleNamespace(context=SimpleNamespace(provider_name="TIER3B-RUNTIME"),
                            test_url="http://127.0.0.1:9/other_contract")
    assert core.runtime._runtime_post_dial_successor_history(other, "n2") is False


def test_pool_reexport_hook_sees_the_real_alive_count_at_publication(core: RealCore) -> None:
    """exhausted_pool_reexport at publication: a healthy group starts no
    re-export; one alive member (<= POOL_REEXPORT_MAX_ALIVE) does."""

    core.health_check_members({"n1", "n2", "n3"})
    core.publish()
    time.sleep(.3)
    assert not core.evs("pool_reexport"), core.evs("pool_reexport")
    core.nodes["n2"].refused.set()
    core.nodes["n3"].refused.set()
    core.health_check_members({"n1"})
    core.publish()
    thread = core.runtime.__dict__.get("_runtime_pool_reexport_thread")
    assert thread is not None
    thread.join(10.0)
    records = core.evs("pool_reexport")
    assert len(records) == 1, records
    assert (records[0]["alive"], records[0]["loaded"], records[0]["exhausted"]) == (1, 3, False)
