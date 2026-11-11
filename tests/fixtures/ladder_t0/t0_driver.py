"""Tier-ladder Track 0: the key-off reference driver (spec_tier_ladder_v1 §7.1, §9).

Run in its own process against one source tree::

    python tests/fixtures/ladder_t0/t0_driver.py --root <tree> --variant <name> --out <dir>

It imports the product modules of ``--root`` and drives today's post-dial
judge wiring over a deterministic fixture matrix, then writes one JSON file
per family into ``--out`` (LF, sorted keys).  Every path the tier-ladder
wiring will touch is exercised with ``tier_ladder_judge`` off:

- ``policy``        NodeJudgementPolicy over the base configs
- ``route``         published snapshots, route context, observer build lines,
                    ``Runtime route view`` lines, observer-core replay
                    (Tier3 trigger, Tier1/Tier2 bursts ignored)
- ``admission``     route gate + admission matrix (shadow, act, register),
                    gated records, not_armed / stale-arm refresh (rearm off
                    and on), mismatch refresh, gateway on path, d4 busy,
                    status sample, contract/route unavailable
- ``probe``         the contract probe hook (controller + broker kwargs)
- ``reads``         route read / leaf read controller calls and results
- ``commit``        ``_runtime_post_dial_native_commit`` outcomes
- ``closes``        ``_runtime_post_dial_close`` and close_enabled
- ``e2e``           whole judgements on the real judge threads (records,
                    verdicts, commits, watch closes, tickets)
- ``switch``        ``_runtime_alternative_switch_action`` matrix plus the
                    argument text of its 8 call sites
- ``tickets``       ticket registration windows
- ``lease_busy``    ``_route_lease_busy`` reasons

Variants (merged into every ``node_judgement`` block the driver writes):
``absent`` (no ladder key), ``false`` (both keys false), ``overshoot_only``
(``overshoot_repair: true`` alone).  Key off, all three must produce the
same output, except that ``overshoot_only`` may carry exactly one
``config_warning`` line per observer build (``t0_compare.py`` checks that).

Nothing here talks to a real Mihomo, opens a socket, or touches a user
profile: the runtime gets a temp project root and fake controllers.
"""

from __future__ import annotations

import argparse
import ast
import copy
import dataclasses
import json
import logging
import os
import re
import sys
import tempfile
import threading
import time
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

VARIANTS = {
    "absent": {},
    "false": {"tier_ladder_judge": False, "overshoot_repair": False},
    "overshoot_only": {"overshoot_repair": True},
}
# Fields the ladder adds to NodeJudgementPolicy (spec §2.1): reported apart,
# so the legacy field dict stays comparable before and after Track A.
LADDER_POLICY_FIELDS = ("tier_ladder_judge", "overshoot_repair")
LADDER_POLICY_PROPERTIES = ("ladder_on", "overshoot_on", "ladder_policy_key")

URL = "http://t0-contract.test/generate_204"
URL3 = "http://t0-tier3.test/generate_204"
PID = 6150
PROCESS_GENERATION = 4
CORE_GENERATION = 9
FIXED_KEY = bytes(range(32))
JUDGE_THREADS = (
    "runtime-post-dial-judge",
    "runtime-post-dial-probe",
    "runtime-post-dial-close",
    "runtime-route-snapshot-refresh",
)
FORBIDDEN = frozenset(
    {
        "select_proxy",
        "select_proxy_with_readback",
        "unfix_proxy",
        "fix_proxy",
        "reload_config",
        "patch_config",
        "update_proxy_provider",
        "close_all_connections",
        "group_delay",
        "providers_proxies",
        "provider_healthcheck",
    }
)

T1A = "nodeA :: VLESS"
T1A2 = "nodeA :: TROJAN"
T1B = "nodeB :: VLESS"
T1B2 = "nodeB :: TROJAN"


# ---------------------------------------------------------------------------
# Bootstrapping (process-wide, before any product import)


def _bootstrap(root: Path, scratch: Path) -> None:
    os.environ["TESSERACT_CONFIG_DIR"] = str(scratch / "config")
    os.environ["TESSERACT_DATA_DIR"] = str(scratch / "data")
    os.chdir(root)
    sys.path.insert(0, str(root))
    import diagnostic_privacy
    import runtime_egress_recovery

    # Deterministic keyed tokens (process keys are random per process).
    runtime_egress_recovery._PROCESS_DESTINATION_HMAC_KEY = FIXED_KEY
    diagnostic_privacy._PROCESS_HMAC_KEY = FIXED_KEY
    import desktop_runtime

    # Deterministic incident / instance / observer ids: ``desktop_runtime``
    # draws them from ``os.urandom``; the driver reseeds per harness.
    desktop_runtime.os = _DeterministicOs(os)


class _DeterministicOs:
    """``os`` for ``desktop_runtime`` with a seeded ``urandom`` (driver only)."""

    def __init__(self, real: Any) -> None:
        self._real = real
        self._lock = threading.Lock()
        self._seed = b"t0"
        self._counter = 0

    def reseed(self, seed: str) -> None:
        with self._lock:
            self._seed = seed.encode("utf-8")
            self._counter = 0

    def urandom(self, size: int) -> bytes:
        import hashlib

        with self._lock:
            self._counter += 1
            block = hashlib.sha256(self._seed + b"\x00" + str(self._counter).encode()).digest()
        return (block * (size // len(block) + 1))[:size]

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def _reseed(seed: str) -> None:
    import desktop_runtime

    target = getattr(desktop_runtime, "os", None)
    if isinstance(target, _DeterministicOs):
        target.reseed(seed)


# ---------------------------------------------------------------------------
# Normalisation

_INCIDENT = re.compile(r"post-dial-[0-9a-f]{16}")
_FLOAT_FIELD = re.compile(
    r"\b(t|ms|queue_wait_ms|elapsed_ms|elapsed_s|age_s|wall|snapshot_wall|t_demote|"
    r"first_event_t|t_open|loaded_at|remaining_s|cost_ms|lag_ms|duration_ms|"
    r"wait_ms|issued_at|at|mono_ns|since_ms|seq)=(-?[0-9]+(?:\.[0-9]+)?(?:e-?[0-9]+)?)"
)


# Per-probe start offsets in the verdict record (``t_ms=__A_C_:_0.0_...``).
_TIMING_MAP = re.compile(r"(?<![A-Za-z0-9_])(t_ms)=(\S+?)(?=[ )]|$)")


class Norm:
    """Per-scenario stable renaming of volatile values."""

    def __init__(self) -> None:
        self.incidents: dict[str, str] = {}

    def incident(self, value: str) -> str:
        return self.incidents.setdefault(value, f"post-dial-<I{len(self.incidents) + 1}>")

    def text(self, value: str) -> str:
        value = _INCIDENT.sub(lambda m: self.incident(m.group(0)), str(value))
        value = _TIMING_MAP.sub(lambda m: f"{m.group(1)}=<{m.group(1)}>", value)
        return _FLOAT_FIELD.sub(lambda m: f"{m.group(1)}=<{m.group(1)}>", value)

    def value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, float):
            return round(value, 3)
        if isinstance(value, dict):
            return {str(k): self.value(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.value(v) for v in value]
        if isinstance(value, (set, frozenset)):
            return sorted(self.value(v) for v in value)
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return {
                f.name: self.value(getattr(value, f.name))
                for f in dataclasses.fields(value)
            }
        if hasattr(value, "value") and type(value).__module__ != "builtins":
            return str(getattr(value, "value"))
        return value


class ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []
        self._lock_ = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        with self._lock_:
            self.records.append(record)

    def take(self) -> list[logging.LogRecord]:
        with self._lock_:
            out, self.records = self.records, []
        return out


def _stream_key(message: str) -> str:
    match = re.match(r"Post-dial observer (\w+) ", message)
    if match:
        return "observer:" + match.group(1)
    head = message.split(" (", 1)[0]
    return "log:" + head[:60]


def grouped_lines(records: list[logging.LogRecord], norm: Norm) -> dict[str, list[str]]:
    """Messages grouped by stream (observer ev, or message head), in order.

    Order is kept inside one stream; streams written by different threads
    (the async F01 diagnostics drain, refresh workers, the judge threads)
    are not interleaved, so the capture does not depend on scheduling.
    """

    out: dict[str, list[str]] = {}
    for record in records:
        message = record.getMessage()
        extra = getattr(record, "runtime_recovery_event", None)
        line = f"{record.levelname} {norm.text(message)}"
        if isinstance(extra, dict):
            line += " | " + json.dumps(norm.value(_recovery_event(extra)), sort_keys=True)
        out.setdefault(_stream_key(message), []).append(line)
    return out


_EVENT_DROP = {"elapsed_ms", "elapsed_s", "monotonic", "observed_at_monotonic", "deadline_monotonic",
               "started_at", "ended_at", "duration_ms", "wall", "at", "budget_ms"}


def _recovery_event(event: Any) -> Any:
    """The structured recovery event without its timing fields (recursive)."""

    if isinstance(event, dict):
        return {k: _recovery_event(v) for k, v in event.items() if k not in _EVENT_DROP}
    if isinstance(event, (list, tuple)):
        return [_recovery_event(v) for v in event]
    return event


def _wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(0.005)
    return bool(predicate())


# ---------------------------------------------------------------------------
# A fake Mihomo controller with fallback/select semantics over a topology


def _request_window(value: Any) -> Any:
    if value is None:
        return None
    value = float(value)
    if 4.5 <= value <= 5.65 + 1e-6:
        return "4.5..5.65"
    return round(value, 3)


class FakeCore:
    """``/proxies`` semantics: select = its selection, fallback = its pin if
    alive else the first alive member (else member 0).  A failed delay test
    marks the leaf dead before replying (the native move).  Reading a
    fallback group drops a dead pin (Conf-1).  Every write except an
    exact-id connection DELETE is recorded and refused."""

    def __init__(self, topo: dict[str, Any]) -> None:
        self.lock = threading.RLock()
        self.groups: dict[str, dict[str, Any]] = copy.deepcopy(topo["groups"])
        self.leaves: dict[str, dict[str, Any]] = {
            name: {"alive": bool(alive), "history": []} for name, alive in topo["leaves"].items()
        }
        self.provider_members: tuple[str, ...] = tuple(topo.get("provider_members", ()))
        self.modes: dict[str, str] = {}
        self.calls: list[list[Any]] = []
        self.delay_calls: list[dict[str, Any]] = []
        self.writes: list[str] = []
        self.closed: list[str] = []
        self.connections_list: list[dict[str, Any]] = []
        self.fail_proxies = 0

    # -- semantics
    def sample(self, name: str, alive: bool) -> None:
        with self.lock:
            leaf = self.leaves[name]
            leaf["alive"] = bool(alive)
            leaf["history"].append({"time": "2026-10-27T00:00:00.0000000Z", "delay": 42 if alive else 0})

    def alive(self, name: str, depth: int = 0) -> bool:
        if name in self.leaves:
            return bool(self.leaves[name]["alive"])
        group = self.groups.get(name)
        if group is None or depth > 12:
            return False
        return self.alive(self.now(name, depth + 1), depth + 1)

    def now(self, name: str, depth: int = 0) -> str:
        group = self.groups[name]
        members = list(group["all"])
        if group["type"] == "Selector":
            return str(group.get("selected") or members[0])
        fixed = str(group.get("fixed") or "")
        if fixed and self.alive(fixed, depth + 1):
            return fixed
        return next((m for m in members if m != "REJECT" and self.alive(m, depth + 1)), members[0])

    def _leaf_entry(self, name: str) -> dict[str, Any]:
        leaf = self.leaves[name]
        history = [dict(item) for item in leaf["history"]]
        extra = {url: {"alive": leaf["alive"], "history": history} for url in (URL, URL3)}
        return {"name": name, "type": "Http", "alive": leaf["alive"], "history": history, "extra": extra}

    def _group_entry(self, name: str, *, read: bool) -> dict[str, Any]:
        group = self.groups[name]
        if read and group["type"] == "Fallback" and group.get("fixed") and not self.alive(group["fixed"]):
            group["fixed"] = ""
        entry = {"name": name, "type": group["type"], "all": list(group["all"]), "now": self.now(name)}
        if group["type"] == "Fallback":
            entry["fixed"] = str(group.get("fixed") or "")
            entry["testUrl"] = str(group.get("url", URL))
            entry["expectedStatus"] = str(group.get("expected", "204"))
        return entry

    # -- reads
    def proxies(self, *, timeout: float | None = None) -> dict[str, Any]:
        with self.lock:
            self.calls.append(["proxies"])
            if self.fail_proxies > 0:
                self.fail_proxies -= 1
                raise RuntimeError("controller unavailable")
            entries: dict[str, Any] = {name: self._group_entry(name, read=True) for name in self.groups}
            entries["REJECT"] = {"name": "REJECT", "type": "Reject"}
            entries["DIRECT"] = {"name": "DIRECT", "type": "Direct"}
            entries.update({name: self._leaf_entry(name) for name in self.leaves})
            return {"proxies": entries}

    def proxy(self, name: str, *, timeout: float | None = None) -> dict[str, Any]:
        from mihomo_api import MihomoAPIError

        with self.lock:
            self.calls.append(["proxy", name])
            if name in self.groups:
                return self._group_entry(name, read=True)
            if name in self.leaves:
                return self._leaf_entry(name)
        raise MihomoAPIError("controller HTTP 404")

    def provider_proxies_one(self, name: str, *, timeout: float | None = None) -> dict[str, Any]:
        with self.lock:
            self.calls.append(["provider", name])
            return {
                "name": name,
                "testUrl": "",
                "expectedStatus": "*",
                "proxies": [self._leaf_entry(leaf) for leaf in self.provider_members],
            }

    def proxy_delay(
        self,
        name: str,
        *,
        url: str,
        timeout_ms: int = 5000,
        expected_status: Any = None,
        request_timeout: float | None = None,
        connection_abort_sink: Any = None,
        verify_expected_status: bool = False,
    ) -> dict[str, Any]:
        from mihomo_api import MihomoDelayStatusMismatch, MihomoDelayTimeout, MihomoDelayTransportFailed

        if connection_abort_sink is not None:
            connection_abort_sink(lambda: None)
        call = {
            "name": name,
            "url": url,
            "timeout_ms": timeout_ms,
            "expected_status": expected_status,
            # The request window is capped by the ticket's remaining time, so a
            # probe that starts late gets a slightly shorter one (timing, not
            # policy): exact values are kept only outside the 4.5..5.65 band.
            "request_timeout": _request_window(request_timeout),
            "verify": bool(verify_expected_status),
            "thread": re.sub(r"\d+", "#", threading.current_thread().name),
        }
        with self.lock:
            self.delay_calls.append(call)
            self.calls.append(["delay", name])
            mode = self.modes.get(name, "ok")
        if mode == "zombie":
            self.sample(name, False)
            raise MihomoDelayTransportFailed("controller HTTP 503 from delay test")
        if mode == "timeout":
            self.sample(name, False)
            raise MihomoDelayTimeout("controller HTTP 504 Timeout")
        if mode == "answer":
            self.sample(name, False)
            raise MihomoDelayStatusMismatch("proxy URL test did not satisfy its expected status")
        if mode == "error":
            raise RuntimeError("controller broke")
        self.sample(name, True)
        return {"delay": 42}

    def connections(self, *, timeout: float | None = None) -> dict[str, Any]:
        with self.lock:
            self.calls.append(["connections"])
            return {"connections": copy.deepcopy(self.connections_list)}

    def close_connection(self, connection_id: str, *, timeout: float | None = None) -> dict[str, Any]:
        with self.lock:
            self.calls.append(["close", str(connection_id)])
            self.closed.append(str(connection_id))
            self.connections_list = [c for c in self.connections_list if c.get("id") != str(connection_id)]
        return {}

    def __getattr__(self, name: str) -> Any:
        if name in FORBIDDEN:
            def forbidden(*_args: Any, **_kwargs: Any) -> Any:
                self.writes.append(name)
                raise AssertionError(f"post-dial T0 driver: forbidden write {name}")

            return forbidden
        raise AttributeError(name)


# ---------------------------------------------------------------------------
# Topologies (the §4.2 shapes, rendered as clash_formatter would)


def _fb(members: list[str], *, url: str = URL, fixed: str = "") -> dict[str, Any]:
    return {"type": "Fallback", "all": list(members), "url": url, "expected": "204", "fixed": fixed}


def _sel(members: list[str], selected: str | None = None) -> dict[str, Any]:
    return {"type": "Selector", "all": list(members), "selected": selected or members[0]}


def _base_groups(pf: list[str], *, final: str = "PROXY-FALLBACK") -> dict[str, Any]:
    return {
        "PROXY-FALLBACK": _fb(pf),
        "FINAL-EXIT": _sel([final, "REJECT"], final),
        "GENERAL": _sel(["FINAL-EXIT"]),
        "GLOBAL": _sel(["GENERAL", "FINAL-EXIT"]),
    }


def topology(name: str) -> dict[str, Any]:
    tier3 = ["n1", "n2", "n3"]
    leaves: dict[str, bool] = {n: True for n in tier3}
    groups: dict[str, Any] = {"TIER3-AUTO": _fb(tier3, url=URL3)}
    tier1_empty = {
        "TIER1-AUTO": _fb(["REJECT"]),
        "TIER1-SELECT": _sel(["TIER1-AUTO", "REJECT"]),
    }
    if name in {"t3", "gw"}:
        groups.update(tier1_empty)
        groups["TIER2-AUTO"] = _fb(["REJECT"])
        groups.update(_base_groups(["TIER3-AUTO"], final="EXIT-SELECT" if name == "gw" else "PROXY-FALLBACK"))
        if name == "gw":
            groups["EXIT-SELECT"] = _sel(["gw1"])
            leaves["gw1"] = True
    elif name == "t3_full":
        # The gate-profile shape: dead placeholder Tier1/Tier2 nodes.
        groups["TIER1-AUTO"] = _fb(["p1"])
        groups["TIER1-SELECT"] = _sel(["TIER1-AUTO", "p1"])
        groups["TIER2-AUTO"] = _fb(["q1"])
        groups.update(_base_groups(["TIER1-SELECT", "TIER2-AUTO", "TIER3-AUTO"]))
        leaves.update({"p1": False, "q1": False})
    elif name in {"t2", "t2_pinned"}:
        groups.update(tier1_empty)
        # t2_pinned: a settled D4 fixed pin (a lease-v1 lease) on t2b.
        groups["TIER2-AUTO"] = _fb(["t2a", "t2b", "t2c"], fixed="t2b" if name == "t2_pinned" else "")
        groups.update(_base_groups(["TIER2-AUTO", "TIER3-AUTO"]))
        leaves.update({"t2a": True, "t2b": True, "t2c": True})
    elif name in {"t1", "t1_user", "t1_flat"}:
        if name == "t1_flat":
            groups["TIER1-AUTO"] = _fb([T1A, T1B])
            groups["TIER1-SELECT"] = _sel(["TIER1-AUTO", T1A, T1B])
        else:
            groups["TIER1-nodeA-AUTO"] = _fb([T1A, T1A2])
            groups["TIER1-nodeA-SELECT"] = _sel(["TIER1-nodeA-AUTO", T1A, T1A2],
                                                T1A2 if name == "t1_user" else None)
            groups["TIER1-nodeB-AUTO"] = _fb([T1B, T1B2])
            groups["TIER1-nodeB-SELECT"] = _sel(["TIER1-nodeB-AUTO", T1B, T1B2])
            groups["TIER1-AUTO"] = _fb(["TIER1-nodeA-SELECT", "TIER1-nodeB-SELECT"])
            groups["TIER1-SELECT"] = _sel(
                ["TIER1-AUTO", "TIER1-nodeA-SELECT", "TIER1-nodeB-SELECT"],
                "TIER1-nodeA-SELECT" if name == "t1_user" else None,
            )
            leaves.update({T1A2: True, T1B2: True})
        leaves.update({T1A: True, T1B: True})
        groups["TIER2-AUTO"] = _fb(["t2a", "t2b"])
        leaves.update({"t2a": True, "t2b": True})
        groups.update(_base_groups(["TIER1-SELECT", "TIER2-AUTO", "TIER3-AUTO"]))
    else:
        raise ValueError(name)
    proxy_map = {}
    for leaf in leaves:
        if leaf in tier3:
            tier, pool = "tier3", "tier3b"
        elif leaf.startswith("node") or leaf == "p1":
            tier, pool = "tier1", "tier1_variant"
        elif leaf == "gw1":
            tier, pool = "exit", "gateway"
        else:
            tier, pool = "tier2", "tier2"
        proxy_map[leaf] = {
            "tier": tier,
            "pool_kind": pool,
            "identity": f"identity-{leaf}",
            "proxy": {"name": leaf, "type": "http"},
        }
    return {"groups": groups, "leaves": leaves, "provider_members": tier3, "proxy_map": proxy_map}


TOPOLOGIES = ("t3", "t3_full", "t2", "t2_pinned", "t1", "t1_user", "t1_flat", "gw")


# ---------------------------------------------------------------------------
# The DesktopRuntime harness


class FakeManager:
    effective_egress_ok = True

    def __init__(self, runtime_generation: int) -> None:
        self.runtime_generation = runtime_generation
        self.accept = True
        self.observations: list[dict[str, Any]] = []
        self.rechecks: list[str] = []

    def apply_runtime_health_observation(self, observation: dict[str, Any]) -> dict[str, Any]:
        self.observations.append(dict(observation))
        return {"accepted": self.accept, "changed": self.accept, "action": "quarantined" if self.accept else "stale"}

    def enqueue_runtime_leaf_recheck(self, node: Any, **kwargs: Any) -> None:
        self.rechecks.append(str(getattr(node, "proxy_name", node)))


class KeepAlive:
    def __init__(self, controller: FakeCore) -> None:
        self.controller = controller

    def connections(self, *, timeout_seconds: float) -> dict[str, Any]:
        return self.controller.connections()

    def close(self) -> None:
        pass


class FakeRunner:
    """The observer runner's demotion-watch surface (deterministic)."""

    def __init__(self, stuck: Any = None) -> None:
        self.stuck = stuck
        self.watches: list[str] = []
        self.ended: list[str] = []
        self.marked: list[list[str]] = []
        self.last_watch: Any = None

    def start_demotion_watch(self, leaf: str, *, t_demote_wall: float) -> Any:
        from runtime_post_dial import DemotionWatch

        self.watches.append(leaf)
        self.last_watch = DemotionWatch(leaf=leaf, t_demote_wall=t_demote_wall, until_perf=time.perf_counter() + 60.0)
        return self.last_watch

    def end_demotion_watch(self, leaf: str) -> None:
        self.ended.append(leaf)

    def stuck_now(self, leaf: str, *, t_demote_wall: float, max_age_s: float) -> Any:
        return self.stuck

    def mark_closed(self, ids: Any) -> None:
        self.marked.append(sorted(str(i) for i in ids))

    def is_alive(self) -> bool:
        return True

    stop_reason = "running"

    def stop(self, reason: str = "") -> None:
        self.stop_reason = str(reason)

    def join(self, timeout: float | None = None) -> None:
        return None


def stuck(*ids: str, aged: int | None = None) -> Any:
    from runtime_post_dial import StuckConnections

    count = len(ids) if aged is None else aged
    return StuckConnections(
        ids=tuple(ids),
        aged=count,
        waiting=len(ids) - count,
        kept_progressing=1,
        kept_not_stuck=1,
        kept_after_demotion=0,
        kept_other=0,
        snapshot_wall=time.time(),
    )


class Harness:
    def __init__(self, ctx: "Ctx", topo_name: str, *, flags: dict[str, Any] | None = None,
                 arm: bool = True, controller: FakeCore | None = None) -> None:
        from desktop_runtime import DesktopRuntime
        from runtime_egress_recovery import RuntimeEgressReadiness, RuntimeEgressReadinessState
        from runtime_post_dial import PostDialTokenizer

        self.ctx = ctx
        ctx.seq += 1
        _reseed(f"{ctx.family}:{ctx.seq}:{topo_name}")
        self.temp = tempfile.TemporaryDirectory(dir=ctx.scratch)
        temp_path = Path(self.temp.name)
        self.topo = topology(topo_name)
        self.flags = {"post_dial_judge": "act", **(flags or {}), **ctx.variant}
        config = {
            "client": {
                "runtime_state_path": str(temp_path / "runtime-state.json"),
                "egress_recovery": {"node_judgement": dict(self.flags)},
            },
            "alerts": {"log_file": str(temp_path / "monitor.log"), "desktop": {"enabled": False}},
        }
        runtime = DesktopRuntime(config, project_root=temp_path)
        for handler in list(runtime.logger.handlers):
            handler.close()
            runtime.logger.removeHandler(handler)
        logger = logging.getLogger(f"t0.driver.{id(runtime)}")
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        self.logs = ListHandler()
        logger.handlers[:] = [self.logs]
        runtime.logger = logger
        state = runtime._node_judgement_state()
        state.tokenizer = PostDialTokenizer(FIXED_KEY)
        self.runtime = runtime
        self.controller = controller or FakeCore(self.topo)
        runtime.api_client = self.controller
        runtime.core_runner = SimpleNamespace(
            is_running=lambda: True,
            status=lambda: {"pid": PID, "process_generation": PROCESS_GENERATION},
        )
        runtime._core_generation = CORE_GENERATION
        runtime._runtime_loaded_export_contract = {
            "revision": 1,
            "semantic_digest": "ladder-t0-digest",
            "proxy_map": copy.deepcopy(self.topo["proxy_map"]),
        }
        self.manager = FakeManager(int(runtime._runtime_generation))
        runtime.manager = self.manager
        runtime._runtime_egress_readiness = RuntimeEgressReadiness(
            state=RuntimeEgressReadinessState.READY,
            validation_active=False,
        )
        runtime._runtime_initial_activation_generation = CORE_GENERATION
        runtime._core_runtime_phase = "formal_capture"
        self.keepalive = KeepAlive(self.controller)
        runtime._runtime_post_dial_controller = lambda: self.keepalive
        self.refreshed = runtime._refresh_runtime_route_snapshot(timeout_seconds=1.0) is not None
        self.armed_at_start = bool(runtime._try_arm_runtime_recovery()) if arm else False
        # The arm starts the real observer thread (shadow/act); its start
        # line is emitted on that thread: wait for it, so it always lands in
        # the first bucket.
        if runtime.__dict__.get("_runtime_post_dial_observer") is not None:
            _wait_for(lambda: any(
                "Post-dial observer observer_started (" in record.getMessage()
                for record in list(self.logs.records)
            ), 3.0)

    @property
    def judge(self) -> Any:
        return self.runtime._runtime_post_dial_judge()

    def trigger_obj(self, leaf: str, *, episode: int = 1, new_episode: bool = True, kind: str = "FAST") -> Any:
        from runtime_post_dial import Trigger

        now = time.time()
        return Trigger(
            kind=kind,
            leaf=leaf,
            tokens=("tok-a", "tok-b"),
            t=now,
            n_events=2,
            first_event_t=now - 0.05,
            episode=episode,
            new_episode=new_episode,
        )

    def trigger(self, leaf: str, **kwargs: Any) -> None:
        trigger = self.trigger_obj(leaf, **kwargs)
        mode = self.runtime._runtime_post_dial_mode()[0]
        self.runtime._on_runtime_post_dial_trigger(
            trigger,
            mode=mode,
            tokenizer=self.runtime._node_judgement_state().tokenizer,
        )

    def quiesce(self, timeout: float = 20.0) -> bool:
        runtime = self.runtime
        state = runtime.__dict__.get("_node_judgement_runtime_state")
        judge = getattr(state, "judge", None)
        ok = True
        if judge is not None:
            ok = judge.wait_idle(timeout) and ok

        def idle() -> bool:
            if getattr(runtime, "_runtime_route_snapshot_refresh_running", False):
                return False
            if any(t.name in JUDGE_THREADS and t.is_alive() for t in threading.enumerate()):
                return False
            diag = runtime.__dict__.get("_node_judgement_arm_diagnostics_state")
            if isinstance(diag, dict) and diag.get("queue"):
                return False
            return True

        ok = _wait_for(idle, timeout) and ok
        time.sleep(0.05)
        ok = _wait_for(idle, timeout) and ok
        return ok

    def binding_view(self) -> Any:
        snapshot, payload = self.runtime._runtime_post_dial_route_refs()
        if snapshot is None or payload is None:
            return None
        binding = self.runtime._runtime_route_binding_for_outbound(snapshot, payload, "FINAL-EXIT")
        return None if binding is None else {"chain": list(binding.route_chain), "leaf": binding.node.proxy_name,
                                             "pool_kind": binding.node.pool_kind}

    def calls(self) -> dict[str, list[Any]]:
        """Controller calls since the last take, per kind, in order.

        Grouped by kind: the probe threads, the judge worker and the close
        thread call the controller concurrently, so only the order within
        one kind is deterministic.
        """

        with self.controller.lock:
            out, self.controller.calls = self.controller.calls, []
        grouped: dict[str, list[Any]] = {}
        for call in out:
            grouped.setdefault(str(call[0]), []).append(list(call[1:]))
        return grouped

    def lines(self, norm: Norm) -> dict[str, list[str]]:
        # The F01 diagnostics drain emits asynchronously: let it finish so a
        # line never lands in the next phase's bucket.
        self.quiesce(10.0)
        return grouped_lines(self.logs.take(), norm)

    def close(self) -> None:
        try:
            self.quiesce(10.0)
            self.runtime._stop_runtime_post_dial_observer("t0", join_timeout=2.0)
            self.runtime.stop_event.set()
        finally:
            for handler in list(self.runtime.logger.handlers):
                handler.close()
            try:
                self.temp.cleanup()
            except Exception:
                pass


class Ctx:
    def __init__(self, root: Path, variant: dict[str, Any], scratch: Path, family: str = "") -> None:
        self.root = root
        self.family = family
        self.seq = 0
        self.variant = dict(variant)
        self.scratch = scratch
        self.made: list[Harness] = []

    def harness(self, topo: str, **kwargs: Any) -> Harness:
        harness = Harness(self, topo, **kwargs)
        self.made.append(harness)
        return harness

    def close_all(self) -> None:
        for harness in self.made:
            harness.close()
        self.made.clear()


def _ticket_view(ticket: Any, norm: Norm) -> Any:
    if ticket is None:
        return None
    return {
        "incident": norm.text(ticket.incident_id),
        "window_s": round(float(ticket.deadline_monotonic) - float(ticket.observed_at_monotonic), 3),
        "route_epoch": int(ticket.route_epoch),
        "facility_epoch": int(ticket.facility_epoch),
        "selector_intent_revision": int(ticket.selector_intent_revision),
        "from": ticket.from_ref.proxy_name,
    }


def _job_view(job: Any, norm: Norm, now: float) -> Any:
    if job is None:
        return None
    context = job.context
    return {
        "leaf": job.leaf,
        "kind": job.kind,
        "tokens": list(job.tokens),
        "episode": job.episode,
        "test_url": job.test_url,
        "expected_status": job.expected_status,
        # Whole seconds: the admission itself takes a few ms after ``now``.
        "deadline_minus_now": round(job.ticket_deadline - now),
        "durable": bool(job.durable),
        "runner": type(job.runner).__name__,
        "ladder": getattr(job, "ladder", None) is not None,
        "provider_name": context.provider_name,
        "binding_chain": list(context.binding.route_chain),
        "binding_leaf": context.binding.node.proxy_name,
        "failure_error_type": context.failure.error_type,
        "ticket": _ticket_view(context.ticket, norm),
        "mutation_intent": int(context.mutation_binding.selector_intent_revision),
        "mutation_facility": int(context.mutation_binding.facility_epoch),
    }


def _outcomes(runtime: Any, norm: Norm) -> list[Any]:
    outcomes = getattr(runtime, "_runtime_recovery_outcomes", {}) or {}
    out = []
    for incident, outcome in outcomes.items():
        if not str(incident).startswith("post-dial-"):
            continue
        out.append({
            "incident": norm.text(incident),
            "status": str(getattr(outcome.status, "value", outcome.status)),
            "reason": str(getattr(outcome, "reason", "")),
            "to": getattr(getattr(outcome, "to_ref", None), "proxy_name", None),
        })
    return sorted(out, key=lambda item: item["incident"])


def _tickets(runtime: Any, norm: Norm) -> list[Any]:
    tickets = getattr(runtime, "_runtime_recovery_tickets", {}) or {}
    return sorted(
        (_ticket_view(t, norm) for i, t in tickets.items() if str(i).startswith("post-dial-")),
        key=lambda item: item["incident"],
    )


def _spy_outcomes(runtime: Any) -> list[Any]:
    calls: list[Any] = []
    complete = runtime._complete_runtime_recovery_outcome

    def spy(incident_id: str, status: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append({
            "incident": incident_id,
            "status": str(getattr(status, "value", status)),
            "reason_code": str(kwargs.get("reason_code") or ""),
            "to": getattr(kwargs.get("to_ref"), "proxy_name", None),
            "facts": kwargs.get("facts"),
            "has_deadline": kwargs.get("deadline_monotonic") is not None,
        })
        return complete(incident_id, status, *args, **kwargs)

    runtime._complete_runtime_recovery_outcome = spy
    return calls


# ---------------------------------------------------------------------------
# Families


def family_policy(ctx: Ctx) -> dict[str, Any]:
    from runtime_node_judge import NodeJudgementPolicy

    bases = {
        "default": {},
        "shadow": {"post_dial_judge": "shadow"},
        "act": {"post_dial_judge": "act"},
        "act_rearm_durable": {"post_dial_judge": "act", "rearm_on_stale_route": True, "durable_conviction": True},
        "disabled": {"enabled": False},
        "off": {"post_dial_judge": "off"},
    }
    out: dict[str, Any] = {}
    norm = Norm()
    for name, block in bases.items():
        merged = {**block, **ctx.variant}
        policy = NodeJudgementPolicy.from_config({"client": {"egress_recovery": {"node_judgement": merged}}})
        fields = {
            f.name: norm.value(getattr(policy, f.name))
            for f in dataclasses.fields(policy)
            if f.name not in LADDER_POLICY_FIELDS
        }
        out[name] = {
            "fields": fields,
            "observer_enabled": policy.observer_enabled,
            "acts": policy.acts,
            "act_rule_ids": sorted(policy.act_rule_ids),
            "constants": norm.value(policy.constants()),
            # Key off these must read False (absent fields read False too).
            "ladder_fields": {k: bool(getattr(policy, k, False)) for k in LADDER_POLICY_FIELDS},
            "ladder_properties": {k: norm.value(getattr(policy, k, None)) for k in LADDER_POLICY_PROPERTIES
                                  if k != "ladder_policy_key"},
            "ladder_policy_key": list(getattr(policy, "ladder_policy_key", (False, policy.acts, False))),
        }
    out["disabled_equals_disabled"] = bool(
        NodeJudgementPolicy.from_config(
            {"client": {"egress_recovery": {"node_judgement": {"enabled": False, **ctx.variant}}}}
        ) == NodeJudgementPolicy.disabled()
    )
    # The ladder-off policy equals the policy of the same block without any
    # ladder key (dataclass equality).
    out["equals_keyless"] = {
        name: bool(
            NodeJudgementPolicy.from_config({"client": {"egress_recovery": {"node_judgement": {**b, **ctx.variant}}}})
            == NodeJudgementPolicy.from_config({"client": {"egress_recovery": {"node_judgement": dict(b)}}})
        )
        for name, b in bases.items()
    }
    return out


def _observer_replay(context: Any, policy: Any, leaf: str, chain_tail: tuple[str, ...]) -> dict[str, Any]:
    from runtime_post_dial import OriginRules, PostDialObserverCore, PostDialTokenizer

    replay = _load_replay(ROOT)
    tokenizer = PostDialTokenizer(FIXED_KEY)
    core = PostDialObserverCore(
        tokenizer=tokenizer,
        constants=policy.constants(),
        act_rules=policy.act_rule_ids,
        gateway_groups=tuple(context.gateway_groups) if context is not None else (),
    )
    if context is not None:
        core.set_route_context(tier3_members=context.tier3_members, gateway_groups=context.gateway_groups)
    t0 = 1_790_000_000.0
    mihomo = replay.FakeMihomo()
    feed = []
    sport = 40000
    # One healthy answered connection, then a fast zombie burst (FAST).
    plan = [(t0, 2.0, 5000, 0.3, "www.python.org")]
    plan += [(t0 + 1.0 - 0.006 + i * 0.02, life, 0, None, f"z{i % 2}.go.dev")
             for i, life in enumerate((0.006, 0.012, 0.030, 0.045, 0.060, 0.008))]
    for t, life, down, down_at, host in plan:
        sport += 1
        mihomo.add(replay.FakeConn(
            conn_id=f"id-{sport}", t_open=t, sport=sport, host=host, port=443, leaf=leaf,
            t_close=t + life,
            up=replay.step(t + 0.001, 517),
            down=replay.step(t + down_at, down) if down_at is not None else (lambda _t: 0),
            chains_tail=chain_tail,
        ))
        feed.append((t + 0.001, replay.open_line(t, sport=sport, host=host, port=443, leaf=leaf)))
    rules = OriginRules.from_system_rules(("DOMAIN,sys.test,FINAL-EXIT",))
    result = replay.drive(core, feed, mihomo, t_start=t0, t_end=t0 + 6.0, rules=rules)
    norm = Norm()
    return {
        "triggers": [
            {"kind": tr.kind, "leaf": tr.leaf, "episode": tr.episode, "new_episode": tr.new_episode,
             "n_events": tr.n_events, "tokens": len(tr.tokens), "t_rel": round(tr.t - t0, 3)}
            for tr in result.triggers
        ],
        "shadow": [str(getattr(item, "rule", item)) for item in result.shadow],
        "polls": len(result.polls),
        "records": [json.dumps(norm.value(record), sort_keys=True) for record in result.records],
    }


_REPLAY: dict[str, Any] = {}


def _load_replay(root: Path) -> Any:
    if "module" not in _REPLAY:
        path = root / "tests" / "fixtures" / "post_dial" / "replay.py"
        spec = importlib.util.spec_from_file_location("ladder_t0_post_dial_replay", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _REPLAY["module"] = module
    return _REPLAY["module"]


ROOT = Path(".")


def _chain_tail(binding: Any) -> tuple[str, ...]:
    chain = list(binding["chain"]) if binding else []
    # Mihomo's chains: innermost group first, the leaf excluded.
    return tuple(reversed(chain[:-1])) + ("GENERAL",)


def family_route(ctx: Ctx) -> dict[str, Any]:
    from desktop_runtime import DesktopRuntime, _RuntimeMonitoringLease

    out: dict[str, Any] = {}
    for topo in TOPOLOGIES:
        norm = Norm()
        harness = ctx.harness(topo)
        runtime = harness.runtime
        snapshot, payload = runtime._runtime_post_dial_route_refs()
        binding = harness.binding_view()
        static_context = DesktopRuntime._runtime_post_dial_route_context(snapshot)
        lease = _RuntimeMonitoringLease(
            runtime_generation=int(runtime._runtime_generation),
            core_generation=CORE_GENERATION,
            process_pid=PID,
            process_generation=PROCESS_GENERATION,
            lifecycle_revision=int(getattr(runtime, "_lifecycle_revision", 0) or 0),
            admitted_at_monotonic=time.monotonic(),
        )
        start_lines = harness.lines(norm)
        builds = []
        for _ in range(2):
            runner = runtime._build_runtime_post_dial_observer(lease)
            context = runner._route_context_for(snapshot)
            builds.append({
                "context": None if context is None else {
                    "tier3_members": sorted(context.tier3_members),
                    "gateway_groups": sorted(context.gateway_groups),
                },
                "lines": harness.lines(norm),
            })
        policy = runtime._node_judgement_policy()
        groups = {
            state.name: {"type": state.type_name, "members": list(state.members), "now": state.now,
                         "fixed": state.fixed, "test_url": state.test_url, "expected": state.expected_status}
            for state in snapshot.group_states
        }
        leaf = binding["leaf"] if binding else "n1"
        replays = {"active_leaf": _observer_replay(context, policy, leaf, _chain_tail(binding))}
        if leaf != "n1":
            replays["tier3_n1"] = _observer_replay(
                context, policy, "n1", ("TIER3-AUTO", "PROXY-FALLBACK", "FINAL-EXIT", "GENERAL"))
        # A republish after Mihomo moved (the route-view line on change).
        if leaf in harness.controller.leaves:
            harness.controller.sample(leaf, False)
        runtime._refresh_runtime_route_snapshot(timeout_seconds=1.0)
        out[topo] = {
            "refreshed": harness.refreshed,
            "armed_at_start": harness.armed_at_start,
            "binding": binding,
            "binding_after_move": harness.binding_view(),
            "group_graph": [[g, list(m)] for g, m in snapshot.group_graph],
            "groups": groups,
            "static_context": None if static_context is None else {
                "tier3_members": sorted(static_context.tier3_members),
                "gateway_groups": sorted(static_context.gateway_groups),
            },
            "start_lines": start_lines,
            "builds": builds,
            "republish_lines": harness.lines(norm),
            "replay": replays,
            "calls": harness.calls(),
        }
        harness.close()
    return out


def _admission(harness: Harness, leaf: str, *, register: bool, norm: Norm) -> dict[str, Any]:
    runtime = harness.runtime
    judge = harness.judge
    trigger = harness.trigger_obj(leaf)
    now = runtime._runtime_recovery_now()
    outcome: dict[str, Any] = {}
    with runtime._node_judgement_arm_capture():
        job, reason = runtime._runtime_post_dial_admission(
            trigger, judge, now=now, register=register, outcome=outcome)
    view = {"reason": reason, "job": _job_view(job, norm, now), "outcome": dict(outcome)}
    if job is not None:
        runtime._runtime_post_dial_finish(job, "t0_released")
    return view


def _route_gate(harness: Harness, leaf: str) -> Any:
    runtime = harness.runtime
    snapshot, payload = runtime._runtime_post_dial_route_refs()
    reason, binding, group = runtime._runtime_post_dial_route_gate(leaf, snapshot, payload)
    return {
        "reason": reason,
        "binding": None if binding is None else [list(binding.route_chain), binding.node.proxy_name],
        "group": None if group is None else [group.name, group.now, group.test_url, group.expected_status],
    }


def _stale_arm(harness: Harness) -> None:
    runtime = harness.runtime
    with runtime._runtime_route_snapshot_lock:
        runtime._runtime_route_epoch += 1
    runtime._refresh_runtime_route_snapshot(timeout_seconds=1.0)


def family_admission(ctx: Ctx) -> dict[str, Any]:
    out: dict[str, Any] = {}

    # Route gate and admission over every topology and leaf kind.
    for topo in TOPOLOGIES:
        norm = Norm()
        harness = ctx.harness(topo)
        binding = harness.binding_view()
        leaves = sorted({*(["n1", "n2"]), *([binding["leaf"]] if binding else [])})
        rows = {}
        for leaf in leaves:
            rows[leaf] = {
                "gate": _route_gate(harness, leaf),
                "shadow_read": _admission(harness, leaf, register=False, norm=norm),
                "register": _admission(harness, leaf, register=True, norm=norm),
            }
            # A queued refresh runs on its own worker: let it finish, so the
            # next leaf's disposition never depends on its timing.
            harness.quiesce()
        rows["_register_calls"] = harness.calls()
        rows["_register_tickets"] = _tickets(harness.runtime, norm)
        rows["_register_outcomes"] = _outcomes(harness.runtime, norm)
        harness.close()
        # The entry point, on a fresh runtime (every refresh key is new, so no
        # disposition depends on elapsed time): act triggers for the active
        # leaf and the stale Tier3 leaves (gated records, refreshes).
        norm = Norm()
        harness = ctx.harness(topo)
        harness.lines(norm)
        harness.calls()
        for leaf in leaves:
            if leaf == "n1" and topo in {"t3", "t3_full"}:
                continue  # would start a judgement; covered by e2e
            harness.trigger(leaf, episode=3)
            harness.quiesce()
        rows["_trigger_lines"] = harness.lines(norm)
        rows["_calls"] = harness.calls()
        rows["_tickets"] = _tickets(harness.runtime, norm)
        rows["_outcomes"] = _outcomes(harness.runtime, norm)
        rows["_refresh_counts"] = dict(harness.runtime._node_judgement_state().refresh_counts)
        out[topo] = rows
        harness.close()

    # Shadow entry point: would_judge for a Tier3 and a Tier1 leaf.
    for topo in ("t3", "t1"):
        norm = Norm()
        harness = ctx.harness(topo, flags={"post_dial_judge": "shadow"})
        leaf = harness.binding_view()["leaf"]
        harness.trigger(leaf, episode=1)
        harness.trigger("n2", episode=1)
        harness.quiesce()
        out[f"shadow_{topo}"] = {"lines": harness.lines(norm), "calls": harness.calls(),
                                 "tickets": _tickets(harness.runtime, norm)}
        harness.close()

    # not_armed: the arm is from an older route epoch.
    for name, flags in (("rearm_off", {}), ("rearm_on", {"rearm_on_stale_route": True})):
        for topo in ("t3", "t1", "gw"):
            norm = Norm()
            harness = ctx.harness(topo, flags=flags)
            _stale_arm(harness)
            leaf = harness.binding_view()["leaf"]
            row = {
                "armed": bool(harness.runtime._runtime_recovery_is_armed()),
                "admission": _admission(harness, leaf, register=False, norm=norm),
            }
            harness.controller.modes[leaf] = "zombie"
            harness.trigger(leaf, episode=1)
            harness.quiesce()
            row["armed_after"] = bool(harness.runtime._runtime_recovery_is_armed())
            row["lines"] = harness.lines(norm)
            row["calls"] = harness.calls()
            row["refresh_counts"] = dict(harness.runtime._node_judgement_state().refresh_counts)
            row["tickets"] = _tickets(harness.runtime, norm)
            out[f"not_armed_{name}_{topo}"] = row
            harness.close()

    # Other gates on the Tier3 path.
    def gate_case(name: str, setup: Callable[[Harness], None], leaf: str = "n1", topo: str = "t3") -> None:
        norm = Norm()
        harness = ctx.harness(topo)
        setup(harness)
        out[name] = {
            "gate": _route_gate(harness, leaf),
            "shadow_read": _admission(harness, leaf, register=False, norm=norm),
            "register": _admission(harness, leaf, register=True, norm=norm),
            "register_lines": harness.lines(norm),
            "register_calls": harness.calls(),
        }
        harness.close()
        norm = Norm()
        harness = ctx.harness(topo)
        setup(harness)
        harness.lines(norm)
        harness.calls()
        harness.trigger(leaf, episode=7)
        harness.quiesce()
        out[name]["lines"] = harness.lines(norm)
        out[name]["calls"] = harness.calls()
        harness.close()

    def d4_busy(h: Harness) -> None:
        with h.runtime._runtime_failure_monitor_lock:
            h.runtime._runtime_failure_probe_bindings = {("snap", "n1", "FINAL-EXIT", "chain"): object()}

    def status_failure(h: Harness) -> None:
        h.controller.sample("n1", False)
        h.controller.sample("n1", True)
        h.judge._status_failures["n1"] = time.time() + 5.0

    def contract_missing(h: Harness) -> None:
        h.controller.groups["TIER3-AUTO"]["url"] = ""
        h.runtime._refresh_runtime_route_snapshot(timeout_seconds=1.0)

    def no_snapshot(h: Harness) -> None:
        with h.runtime._runtime_route_snapshot_lock:
            h.runtime._runtime_route_snapshot = None
            h.runtime._runtime_route_snapshot_payload = None

    def hold(h: Harness) -> None:
        from runtime_node_judge import Verdict

        with h.judge._lock:
            h.judge.gates.set_hold(Verdict.FACILITY, h.runtime._runtime_recovery_now())

    gate_case("gate_d4_busy", d4_busy)
    gate_case("gate_status_failure", status_failure)
    gate_case("gate_contract_unavailable", contract_missing)
    gate_case("gate_route_unavailable", no_snapshot)
    gate_case("gate_facility_hold", hold)
    gate_case("gate_non_member", lambda h: None, leaf="zz")
    gate_case("gate_t3_full_dead_placeholders", lambda h: None, topo="t3_full")
    return out


def _probe_job(harness: Harness, norm: Norm) -> Any:
    runtime = harness.runtime
    trigger = harness.trigger_obj("n1")
    now = runtime._runtime_recovery_now()
    job, reason = runtime._runtime_post_dial_admission(trigger, harness.judge, now=now, register=True)
    return job, reason


def family_probe(ctx: Ctx) -> dict[str, Any]:
    from runtime_node_judge import StartProbe

    out: dict[str, Any] = {}
    for mode in ("ok", "zombie", "answer", "timeout", "error"):
        norm = Norm()
        harness = ctx.harness("t3")
        runtime = harness.runtime
        job, reason = _probe_job(harness, norm)
        brokered: list[Any] = []
        leased: list[Any] = []
        real_broker = runtime._run_brokered_runtime_probe
        real_leased = runtime._probe_proxy_delay_with_urls_leased

        def broker(run: Any, **kwargs: Any) -> Any:
            issued = time.monotonic()
            brokered.append({
                "priority": str(getattr(kwargs.get("priority"), "name", kwargs.get("priority"))),
                "queue_timeout_seconds": kwargs.get("queue_timeout_seconds"),
                "socket_start_timeout_seconds": kwargs.get("socket_start_timeout_seconds"),
                # Quarter seconds: the hook reads its clock just before this spy.
                "deadline_minus_issue": round((float(kwargs["execution_deadline_monotonic"]) - issued) * 4) / 4
                if kwargs.get("execution_deadline_monotonic") is not None else None,
                "keys": sorted(kwargs),
            })
            return real_broker(run, **kwargs)

        def leased_spy(leaf: str, urls: Any, **kwargs: Any) -> Any:
            leased.append({
                "leaf": leaf,
                "urls": list(urls),
                "timeout_ms": kwargs.get("timeout_ms"),
                "request_timeout_seconds": round(float(kwargs.get("request_timeout_seconds")), 3),
                "expected_status": kwargs.get("expected_status"),
                "has_ticket": kwargs.get("recovery_ticket") is not None,
                "keys": sorted(kwargs),
            })
            return real_leased(leaf, urls, **kwargs)

        runtime._run_brokered_runtime_probe = broker
        runtime._probe_proxy_delay_with_urls_leased = leased_spy
        harness.controller.modes["n1"] = mode
        harness.calls()
        action = StartProbe(name="A_C", leaf="n1", timeout_ms=5000, request_timeout_s=5.65)
        result = runtime._runtime_post_dial_contract_probe(job, action)
        succ = StartProbe(name="S_C0", leaf="n2", timeout_ms=5000, request_timeout_s=5.65)
        result2 = runtime._runtime_post_dial_contract_probe(job, succ)
        out[mode] = {
            "admission": reason,
            "result": str(result.probe.value),
            "successor_result": str(result2.probe.value),
            "queue_wait_known": result.queue_wait_ms is not None,
            "delay_calls": list(harness.controller.delay_calls),
            "brokered": brokered,
            "leased": leased,
            "calls": harness.calls(),
            "lines": harness.lines(norm),
        }
        if job is not None:
            runtime._runtime_post_dial_finish(job, "t0")
        harness.close()
    return out


def _route_read_view(route: Any) -> Any:
    if route is None:
        return None
    return {
        "order": list(route.order),
        "judge_order": list(route.judge_order),
        "alive": {k: bool(v) for k, v in sorted(dict(route.alive).items())},
        "unknown": sorted(getattr(route, "unknown", ()) or ()),
        "now": getattr(route, "now", None),
        "fixed": getattr(route, "fixed", None),
        "demoted_n1": bool(route.demoted("n1")),
        "all_loaded_dead": route.all_loaded_dead(),
    }


def family_reads(ctx: Ctx) -> dict[str, Any]:
    out: dict[str, Any] = {}
    cases = {
        "healthy": lambda h: None,
        "n1_dead": lambda h: h.controller.sample("n1", False),
        "all_dead": lambda h: [h.controller.sample(n, False) for n in ("n1", "n2", "n3")],
        "pinned_n2": lambda h: h.controller.groups["TIER3-AUTO"].update(fixed="n2"),
    }
    for name, setup in cases.items():
        norm = Norm()
        harness = ctx.harness("t3")
        job, reason = _probe_job(harness, norm)
        setup(harness)
        harness.calls()
        runtime = harness.runtime
        route = runtime._runtime_post_dial_route_read(job)
        route_calls = harness.calls()
        leaf_entries = {}
        for leaf in ("n1", "n2", "zz"):
            try:
                entry = runtime._runtime_post_dial_leaf_read(job, leaf)
                leaf_entries[leaf] = None if entry is None else {"alive": entry.get("alive"), "name": entry.get("name")}
            except Exception as exc:
                leaf_entries[leaf] = "raised:" + type(exc).__name__
        out[name] = {
            "admission": reason,
            "route": _route_read_view(route),
            "route_calls": route_calls,
            "leaf": leaf_entries,
            "leaf_calls": harness.calls(),
        }
        if job is not None:
            runtime._runtime_post_dial_finish(job, "t0")
        harness.close()
    return out


def _done(successor: str | None, *, verdict: str = "CONVICT_CONTRACT", commit_allowed: bool = True) -> Any:
    from runtime_node_judge import Done, Verdict

    return Done(
        verdict=Verdict(verdict),
        reason="t0",
        leaf="n1",
        successor=successor,
        successor_index=0 if successor else None,
        results=(("A_C", "transport_fail"), ("S_C0", "pass")),
        commit_allowed=commit_allowed,
        tokens=("tok-a",),
        kind="FAST",
    )


def family_commit(ctx: Ctx) -> dict[str, Any]:
    out: dict[str, Any] = {}

    def case(name: str, *, flags: dict[str, Any] | None = None, before: Callable[[Harness, Any], None] | None = None,
             successor: str = "n2", move: bool = True) -> None:
        norm = Norm()
        harness = ctx.harness("t3", flags=flags)
        runtime = harness.runtime
        job, reason = _probe_job(harness, norm)
        spy = _spy_outcomes(runtime)
        if move:
            harness.controller.sample("n1", False)
        if before is not None:
            before(harness, job)
        harness.calls()
        accepted = runtime._runtime_post_dial_native_commit(job, _done(successor))
        harness.quiesce()
        out[name] = {
            "admission": reason,
            "result": list(accepted),
            "completions": norm.value(spy),
            "outcomes": _outcomes(runtime, norm),
            "tickets": _tickets(runtime, norm),
            "binding": harness.binding_view(),
            "armed": bool(runtime._runtime_recovery_is_armed()),
            "observations": [o.get("proxy_name") for o in harness.manager.observations],
            "rechecks": list(harness.manager.rechecks),
            "calls": harness.calls(),
            "writes": list(harness.controller.writes),
            "lines": harness.lines(norm),
        }
        harness.close()

    def to_shadow(h: Harness, job: Any) -> None:
        h.runtime.config["client"]["egress_recovery"]["node_judgement"]["post_dial_judge"] = "shadow"

    def expire(h: Harness, job: Any) -> None:
        job.context.ticket = dataclasses.replace(job.context.ticket,
                                                 deadline_monotonic=h.runtime._runtime_recovery_now() - 1.0)

    def user_pin(h: Harness, job: Any) -> None:
        h.runtime._runtime_selector_intent_revision = int(
            getattr(h.runtime, "_runtime_selector_intent_revision", 0) or 0) + 1

    def core_restart(h: Harness, job: Any) -> None:
        h.runtime._core_generation = CORE_GENERATION + 1

    def refresh_fails(h: Harness, job: Any) -> None:
        h.controller.fail_proxies = 5

    def manager_rejects(h: Harness, job: Any) -> None:
        h.manager.accept = False

    case("move_only_accepted")
    case("durable_accepted", flags={"durable_conviction": True})
    case("durable_rejected", flags={"durable_conviction": True}, before=manager_rejects)
    case("native_pick_mismatch", successor="n3")
    case("accused_alive", move=False)
    case("act_mode_off", before=to_shadow)
    case("commit_late", before=expire)
    case("intent_changed", before=user_pin)
    case("core_generation_changed", before=core_restart)
    case("refresh_unavailable", before=refresh_fails)
    return out


def _connection(conn_id: str, leaf: str, chain_tail: tuple[str, ...]) -> dict[str, Any]:
    return {
        "id": conn_id,
        "chains": [leaf, *chain_tail],
        "metadata": {"network": "tcp", "type": "HTTP", "host": "c.test", "destinationPort": "443",
                     "sourceIP": "127.0.0.1", "sourcePort": "40001"},
        "upload": 100,
        "download": 0,
        "start": "2026-10-27T00:00:00Z",
        "rule": "Match",
        "rulePayload": "",
    }


def family_closes(ctx: Ctx) -> dict[str, Any]:
    out: dict[str, Any] = {}
    tail = ("TIER3-AUTO", "PROXY-FALLBACK", "FINAL-EXIT", "GENERAL")
    for name, ids, leaf in (
        ("exact_ids", ("c1", "c2"), "n1"),
        ("unknown_id", ("c1", "cx"), "n1"),
        ("other_leaf", ("c3",), "n1"),
        ("wrong_job_leaf", ("c1",), "n2"),
        ("empty", (), "n1"),
    ):
        norm = Norm()
        harness = ctx.harness("t3")
        harness.controller.connections_list = [
            _connection("c1", "n1", tail), _connection("c2", "n1", tail), _connection("c3", "n2", tail)]
        job, reason = _probe_job(harness, norm)
        harness.calls()
        closed = harness.runtime._runtime_post_dial_close(job, leaf, ids)
        out[name] = {
            "admission": reason,
            "closed": list(closed),
            "calls": harness.calls(),
            "close_enabled": bool(harness.runtime._runtime_post_dial_close_enabled()),
            "lines": harness.lines(norm),
        }
        harness.runtime._runtime_post_dial_finish(job, "t0")
        harness.close()
    for name, flags in (("close_flag_off", {"close_stuck_connections": False}),
                        ("shadow", {"post_dial_judge": "shadow"})):
        harness = ctx.harness("t3", flags=flags)
        out[name] = {"close_enabled": bool(harness.runtime._runtime_post_dial_close_enabled())}
        harness.close()
    return out


def family_e2e(ctx: Ctx) -> dict[str, Any]:
    out: dict[str, Any] = {}
    tail = ("TIER3-AUTO", "PROXY-FALLBACK", "FINAL-EXIT", "GENERAL")

    def scenario(name: str, modes: dict[str, str], *, flags: dict[str, Any] | None = None,
                 stuck_ids: tuple[str, ...] = ("c1", "c2"), initial_stuck: bool = False,
                 watch_steps: int = 2, topo: str = "t3", leaf: str = "n1",
                 second_trigger: bool = True) -> None:
        norm = Norm()
        harness = ctx.harness(topo, flags=flags)
        runtime = harness.runtime
        harness.controller.connections_list = [
            _connection("c1", "n1", tail), _connection("c2", "n1", tail), _connection("c3", "n2", tail)]
        runner = FakeRunner(stuck(*stuck_ids) if initial_stuck else None)
        runtime._runtime_post_dial_observer = runner
        spy = _spy_outcomes(runtime)
        harness.controller.modes.update(modes)
        harness.calls()
        harness.trigger(leaf, episode=1)
        harness.quiesce(30.0)
        judge = harness.judge
        steps = []
        for _ in range(watch_steps):
            if runner.last_watch is None:
                break
            judge.on_watch_step(runner, runner.last_watch, stuck(*stuck_ids))
            harness.quiesce(10.0)
            steps.append(list(harness.controller.closed))
        # A second trigger in the same episode after the judgement: gates.
        # (Not where it would re-queue a refresh on the same snapshot key:
        # bounded_retry vs queued depends on the elapsed second.)
        if second_trigger:
            harness.trigger(leaf, episode=1, new_episode=False)
            harness.quiesce()
        out[name] = {
            "delay_calls": [{k: v for k, v in c.items()} for c in harness.controller.delay_calls],
            "closed": list(harness.controller.closed),
            "watch_steps": steps,
            "runner": {"watches": runner.watches, "ended": runner.ended, "marked": runner.marked},
            "completions": norm.value(spy),
            "outcomes": _outcomes(runtime, norm),
            "tickets": _tickets(runtime, norm),
            "binding": harness.binding_view(),
            "armed": bool(runtime._runtime_recovery_is_armed()),
            "hold": None if judge.gates.hold is None else str(judge.gates.hold.value),
            "metrics": dict(sorted(judge.metrics.items())),
            "observations": [o.get("proxy_name") for o in harness.manager.observations],
            "writes": list(harness.controller.writes),
            "calls": harness.calls(),
            "lines": harness.lines(norm),
        }
        harness.close()

    scenario("pf1_zombie_move_only", {"n1": "zombie"})
    scenario("pf1_zombie_initial_stuck", {"n1": "zombie"}, initial_stuck=True)
    scenario("pf1_zombie_durable", {"n1": "zombie"}, flags={"durable_conviction": True})
    scenario("pf6_not_reproduced", {})
    scenario("pf7b_exhausted", {"n1": "zombie", "n2": "zombie", "n3": "zombie"})
    scenario("accused_timeout_successor_pass", {"n1": "timeout"})
    scenario("successor_timeout_keep_all", {"n1": "zombie", "n2": "timeout"})
    scenario("status_answer", {"n1": "answer"})
    scenario("close_flag_off", {"n1": "zombie"}, flags={"close_stuck_connections": False})
    scenario("tier1_trigger_ignored", {T1A: "zombie"}, topo="t1", leaf=T1A, watch_steps=0,
             second_trigger=False)
    scenario("tier2_trigger_ignored", {"t2a": "zombie"}, topo="t2", leaf="t2a", watch_steps=0,
             second_trigger=False)
    scenario("gateway_mismatch_refresh", {"n1": "zombie"}, topo="gw", watch_steps=0,
             second_trigger=False)
    return out


def _failure(snapshot: Any, payload: Any, runtime: Any, outbound: str = "FINAL-EXIT") -> Any:
    from runtime_egress_recovery import BoundRuntimeFailure

    binding = runtime._runtime_route_binding_for_outbound(snapshot, payload, outbound)
    if binding is None:
        return None
    return BoundRuntimeFailure(
        binding=binding,
        protocol="TCP",
        observed_outbound=binding.observed_outbound,
        destination_token="t0-destination",
        error_type="t0",
        observed_at_monotonic=1000.0,
    )


def _switch_call_sites(root: Path) -> list[Any]:
    source = (root / "desktop_runtime.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    sites: list[Any] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

        def visit_Call(self, node: ast.Call) -> None:
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "_runtime_alternative_switch_action":
                sites.append({
                    "caller": ".".join(self.stack),
                    "args": [ast.unparse(arg) for arg in node.args],
                    "keywords": {str(kw.arg): ast.unparse(kw.value) for kw in node.keywords},
                })
            self.generic_visit(node)

    Visitor().visit(tree)
    return sorted(sites, key=lambda item: (item["caller"], item["args"]))


def family_switch(ctx: Ctx) -> dict[str, Any]:
    import inspect

    from desktop_runtime import DesktopRuntime
    from runtime_egress_recovery import RuntimeNodeRef

    out: dict[str, Any] = {"call_sites": _switch_call_sites(ctx.root)}
    params = inspect.signature(DesktopRuntime._runtime_alternative_switch_action).parameters
    has_allow = "allow_pf_subtree_select" in params
    alternatives = ["n1", "n2", "n3", T1A, T1A2, T1B, T1B2, "t2a", "t2b", "t2c", "gw1", "zz"]
    for topo in TOPOLOGIES:
        harness = ctx.harness(topo)
        runtime = harness.runtime
        snapshot, payload = runtime._runtime_post_dial_route_refs()
        failure = _failure(snapshot, payload, runtime)
        rows = {}
        allow_equal = True
        for alt in alternatives:
            info = harness.topo["proxy_map"].get(alt, {"tier": "tier3", "pool_kind": "tier3b"})
            ref = RuntimeNodeRef(tier=info["tier"], pool_kind=info["pool_kind"], identity=f"identity-{alt}",
                                 proxy_name=alt)
            if failure is None:
                rows[alt] = "no_failure"
                continue
            result = runtime._runtime_alternative_switch_action(failure, ref, payload)
            rows[alt] = result
            if has_allow:
                allow_equal = allow_equal and (
                    runtime._runtime_alternative_switch_action(failure, ref, payload, allow_pf_subtree_select=True)
                    == result
                )
        # A binding through a pinned select (user-pointed) and with a fixed pin.
        out[topo] = {"binding": harness.binding_view(), "actions": rows, "allow_true_matches_default": allow_equal}
        harness.close()
    return out


def family_tickets(ctx: Ctx) -> dict[str, Any]:
    out: dict[str, Any] = {}
    norm = Norm()
    harness = ctx.harness("t3")
    runtime = harness.runtime
    snapshot, payload = runtime._runtime_post_dial_route_refs()
    failure = _failure(snapshot, payload, runtime)
    failure = dataclasses.replace(failure, observed_at_monotonic=runtime._runtime_recovery_now())
    rows = {}
    t_a = runtime._register_runtime_recovery_ticket(failure, snapshot, "post-dial-" + "a" * 16)
    rows["register_default"] = _ticket_view(t_a, norm)
    rows["register_short_deadline"] = _ticket_view(runtime._register_runtime_recovery_ticket(
        failure, snapshot, "post-dial-" + "b" * 16, deadline_monotonic=failure.observed_at_monotonic + 2.0), norm)
    rows["register_long_deadline"] = _ticket_view(runtime._register_runtime_recovery_ticket(
        failure, snapshot, "post-dial-" + "c" * 16, deadline_monotonic=failure.observed_at_monotonic + 30.0), norm)
    again = runtime._register_runtime_recovery_ticket(failure, snapshot, "post-dial-" + "a" * 16)
    rows["register_again_same"] = again is t_a
    rebound = runtime._register_runtime_recovery_ticket(failure, snapshot, "post-dial-" + "a" * 16,
                                                        force_rebind=True, deadline_monotonic=t_a.deadline_monotonic)
    rows["force_rebind"] = {"same": rebound is t_a, "generation_delta": rebound.generation - t_a.generation,
                            "view": _ticket_view(rebound, norm)}
    rows["ensure_existing"] = runtime._ensure_runtime_recovery_ticket(
        failure, snapshot, "post-dial-" + "a" * 16) is rebound
    rows["ensure_new"] = _ticket_view(runtime._ensure_runtime_recovery_ticket(
        failure, snapshot, "post-dial-" + "d" * 16), norm)
    rows["ensure_empty"] = runtime._ensure_runtime_recovery_ticket(failure, snapshot, "")
    out["t3"] = rows
    out["lines"] = harness.lines(norm)
    harness.close()
    return out


def family_lease_busy(ctx: Ctx) -> dict[str, Any]:
    out: dict[str, Any] = {}

    def view(busy: Any) -> Any:
        return {"hard": busy.hard, "soft": busy.soft, "repr": repr(busy)}

    def case(name: str, setup: Callable[[Harness], Any], *, own_release: bool = False) -> None:
        harness = ctx.harness("t2")
        lease = setup(harness)
        out[name] = view(harness.runtime._route_lease_busy(lease, own_release=own_release))
        harness.close()

    lease_fixed = {"group": "TIER2-AUTO", "mode": "fixed", "to": "t2b", "target_member": "t2b",
                   "release_state": ""}

    case("none", lambda h: None)
    case("settled_fixed", lambda h: dict(lease_fixed))
    case("settled_selector", lambda h: {"group": "TIER1-SELECT", "mode": "selector", "to": "TIER1-AUTO"})

    def recovery(h: Harness) -> Any:
        h.runtime._runtime_failure_recovery_bindings = {("k",): object()}
        return dict(lease_fixed)

    def releasing(h: Harness) -> Any:
        return {**lease_fixed, "release_state": "releasing"}

    def passive(h: Harness) -> Any:
        lease = dict(lease_fixed)
        h.runtime._runtime_passive_failback_requests = {"x": {"lease": lease, "running": True}}
        return lease

    def core_down(h: Harness) -> Any:
        h.runtime.core_runner = SimpleNamespace(is_running=lambda: False, status=lambda: {})
        return dict(lease_fixed)

    def not_ready(h: Harness) -> Any:
        h.runtime._runtime_initial_activation_generation = -1
        return dict(lease_fixed)

    def probe_binding(h: Harness) -> Any:
        snapshot = h.runtime._runtime_route_snapshot
        key = tuple(snapshot.snapshot_key)
        h.runtime._runtime_failure_probe_bindings = {
            (key[0], key[1], key[2], key[3], "x", int(snapshot.network_context_epoch)): object()}
        return dict(lease_fixed)

    case("recovery_worker", recovery)
    case("releasing", releasing)
    case("releasing_own", releasing, own_release=True)
    case("passive_running", passive)
    case("core_down", core_down)
    case("not_ready", not_ready)
    case("probe_binding", probe_binding)
    return out


FAMILIES: dict[str, Callable[[Ctx], dict[str, Any]]] = {
    "policy": family_policy,
    "route": family_route,
    "admission": family_admission,
    "probe": family_probe,
    "reads": family_reads,
    "commit": family_commit,
    "closes": family_closes,
    "e2e": family_e2e,
    "switch": family_switch,
    "tickets": family_tickets,
    "lease_busy": family_lease_busy,
}


def dump(value: Any) -> bytes:
    text = json.dumps(value, sort_keys=True, indent=1, ensure_ascii=False, default=repr)
    return (text + "\n").encode("utf-8")


def main(argv: list[str] | None = None) -> int:
    global ROOT
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--root", required=True)
    parser.add_argument("--variant", choices=sorted(VARIANTS), required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--families", default=",".join(FAMILIES))
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    ROOT = root
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ladder-t0-") as scratch_name:
        scratch = Path(scratch_name)
        _bootstrap(root, scratch)
        logging.getLogger().setLevel(logging.WARNING)
        failures = 0
        for name in [item for item in args.families.split(",") if item]:
            ctx = Ctx(root, VARIANTS[args.variant], scratch, name)
            started = time.monotonic()
            try:
                value = FAMILIES[name](ctx)
            except Exception as exc:  # recorded, so a broken path is a diff
                import traceback

                value = {"_error": type(exc).__name__, "_trace": traceback.format_exc().splitlines()[-6:]}
                failures += 1
            finally:
                ctx.close_all()
            (out_dir / f"{name}.json").write_bytes(dump(value))
            print(f"{name}: {time.monotonic() - started:.1f}s", file=sys.stderr, flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
