"""Equal-slot comparison: 16 processes x 1 slot versus 4 processes x 4 slots.

codex's question, and the only one this answers: at the SAME number of
validation slots, does sharing one Mihomo across slots validate faster than a
process per candidate?  Total slots are equal by construction - the probe
refuses to run otherwise - so this does not measure "more concurrency", and it
must not be quoted as if it did.

Both arms are driven by the same parent loop and the same command factory, so
budgets and dispatch are identical by construction.  The only difference is the
helper geometry:

  control : the production ValidatorHelperRuntime, one candidate per process,
            config hot-reloaded per candidate (tier3_validator_runtime.py:292)
  batch   : the prototype BatchValidatorHelperRuntime, several slots per
            process, config fixed for a generation and never hot-updated

This is a local instrument. Candidates are whatever list you pass it; the result
says nothing about public-network reachability, and nothing here is production
code. Artifacts are written under .build only.
"""
from __future__ import annotations

import argparse
import json
import multiprocessing
import socket
import statistics
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from models import Node  # noqa: E402
from tier3_validation_config import build_validator_prewarm_config  # noqa: E402
from tier3_validation_protocol import (  # noqa: E402
    PROTOCOL_SCHEMA_VERSION,
    ValidationStage,
    message_bytes,
    parse_message,
)
from tier3_validator_helper import BOOTSTRAP_READY_MESSAGE, helper_process_bootstrap  # noqa: E402
from test_support.tier3_batch_helper_bootstrap import batch_helper_bootstrap  # noqa: E402
from tier3_validation_domain import tier3_candidate_config_digest  # noqa: E402

MIHOMO = ROOT / "bin" / "mihomo.exe"


def reserve_port(excluded: set[int]) -> int:
    for _attempt in range(256):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
        finally:
            sock.close()
        if port > 0 and port not in excluded:
            excluded.add(port)
            return port
    raise RuntimeError("unable to reserve a distinct port")


class Helper:
    """One spawned helper process, whichever runtime it carries."""

    def __init__(self, worker_id: int, slots: int, batch: bool, ports: set[int],
                 temp_parent: Path, probe_urls: list[str], budget: float) -> None:
        self.worker_id = worker_id
        self.slots = slots
        self.batch = batch
        self.mixed_port = reserve_port(ports)
        self.controller_port = reserve_port(ports)
        self.listener_ports = [reserve_port(ports) for _ in range(slots)] if batch else []
        context = multiprocessing.get_context("spawn")
        self.parent, child = context.Pipe(duplex=True)
        target = batch_helper_bootstrap if batch else helper_process_bootstrap
        self.process = context.Process(
            target=target, args=(child,), name=f"slot-parity-{worker_id}", daemon=False,
        )
        self.process.start()
        child.close()
        self.started_at = time.monotonic()
        self.ready_at: float | None = None
        self.inflight: dict[str, float] = {}
        assert self.parent.recv_bytes() == BOOTSTRAP_READY_MESSAGE
        payload: dict[str, Any] = {
            "worker_id": worker_id,
            "worker_generation": 1,
            "binary_path": str(MIHOMO),
            "probe_urls": list(probe_urls),
            "expected_statuses": [200, 204],
            "probe_timeout_seconds": budget,
            "slow_threshold_ms": 3000,
            "temp_parent": str(temp_parent),
            "ipv6": False,
            "mixed_port": self.mixed_port,
            "controller_port": self.controller_port,
            "prewarm_config": build_validator_prewarm_config(
                self.mixed_port, self.controller_port, ipv6=False,
            ),
        }
        if batch:
            payload["slot_count"] = slots
        self.parent.send_bytes(json.dumps(
            {"schema": PROTOCOL_SCHEMA_VERSION, "kind": "start", "payload": payload}
        ).encode("utf-8"))

    def send(self, kind: str, payload: dict[str, Any]) -> None:
        self.parent.send_bytes(message_bytes(kind, payload))

    def close(self) -> None:
        try:
            self.send("stop", {"worker_id": self.worker_id, "worker_generation": 1})
        except Exception:  # noqa: BLE001
            pass
        self.process.join(timeout=8)
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(timeout=5)


def command(node: Node, sequence: int, worker_id: int, budget: float,
            endpoint: str) -> dict[str, Any]:
    """One command factory for both arms, so budgets cannot drift apart."""

    digest = tier3_candidate_config_digest(node)
    return {
        "refresh_epoch": "slot-parity",
        "candidate_digest": digest,
        "config_digest": digest,
        "lease_id": f"lease-{worker_id}-{sequence}",
        "sequence": sequence,
        "node_json": json.dumps(node.to_dict(), ensure_ascii=False),
        "source_root": "slot-parity",
        "source_family": "probe",
        "transaction_id": f"txn-{worker_id}-{sequence}",
        "slice_id": 1,
        "stage": ValidationStage.FAST_PROBE.value,
        "worker_id": worker_id,
        "worker_generation": 1,
        "probe_budget_seconds": budget,
        "facility_budget_seconds": 10.0,
        "endpoint": endpoint,
        "candidate_hard_cap_seconds": budget + 2.75,
    }


def run_arm(name: str, nodes: list[Node], processes: int, slots: int, *,
            batch: bool, budget: float, probe_urls: list[str],
            temp_parent: Path) -> dict[str, Any]:
    ports: set[int] = set()
    # Start the clock BEFORE spawning: process creation is part of what an arm
    # costs, and excluding it flatters whichever arm spawns more processes.
    started = time.monotonic()
    helpers = [
        Helper(index + 1, slots, batch, ports, temp_parent, probe_urls, budget)
        for index in range(processes)
    ]
    spawned_at = time.monotonic()
    results: dict[str, str] = {}
    facility_ms: list[int] = []
    first_result_at: float | None = None
    try:
        # Wait for every helper to report ready.
        pending = set(range(len(helpers)))
        deadline = time.monotonic() + 120.0
        while pending and time.monotonic() < deadline:
            for index in list(pending):
                helper = helpers[index]
                while helper.parent.poll(0):
                    kind, _payload = parse_message(helper.parent.recv_bytes())
                    if kind == "ready":
                        helper.ready_at = time.monotonic()
                        pending.discard(index)
            time.sleep(0.02)
        if pending:
            raise RuntimeError(f"{name}: helpers never became ready: {sorted(pending)}")
        helpers_ready_seconds = max(h.ready_at - started for h in helpers if h.ready_at)

        # Deal candidates round-robin so both arms see the same work split.
        queues: list[list[Node]] = [[] for _ in helpers]
        for index, node in enumerate(nodes):
            queues[index % len(helpers)].append(node)

        if batch:
            for helper, queue in zip(helpers, queues):
                for offset in range(0, len(queue), helper.slots):
                    pass  # generations are loaded lazily below
        dispatch_started = time.monotonic()
        cursors = [0] * len(helpers)
        generation_loaded = [False] * len(helpers)
        sequence = 0
        outstanding = 0
        total = len(nodes)
        completed = 0
        while completed < total and time.monotonic() - dispatch_started < 600.0:
            for index, helper in enumerate(helpers):
                queue = queues[index]
                if batch:
                    busy = len(helper.inflight)
                    if busy == 0 and cursors[index] < len(queue) and not generation_loaded[index]:
                        chunk = queue[cursors[index]:cursors[index] + helper.slots]
                        helper.send("load_batch_generation", {
                            "nodes": [node.to_dict() for node in chunk],
                            "candidate_digests": [
                                tier3_candidate_config_digest(node) for node in chunk
                            ],
                            "listener_ports": helper.listener_ports[:len(chunk)],
                            "facility_budget_seconds": 15.0,
                        })
                        generation_loaded[index] = "pending"
                elif len(helper.inflight) == 0 and cursors[index] < len(queue):
                    node = queue[cursors[index]]
                    cursors[index] += 1
                    sequence += 1
                    payload = command(node, sequence, helper.worker_id, budget, probe_urls[0])
                    helper.inflight[payload["candidate_digest"]] = time.monotonic()
                    helper.send("validate_endpoint", payload)
                    outstanding += 1
                while helper.parent.poll(0):
                    kind, payload = parse_message(helper.parent.recv_bytes())
                    if kind == "batch_generation_ready":
                        generation_loaded[index] = True
                        chunk = queues[index][cursors[index]:cursors[index] + helper.slots]
                        cursors[index] += len(chunk)
                        for node in chunk:
                            sequence += 1
                            cmd = command(node, sequence, helper.worker_id, budget, probe_urls[0])
                            helper.inflight[cmd["candidate_digest"]] = time.monotonic()
                            helper.send("validate_endpoint", cmd)
                            outstanding += 1
                    elif kind == "endpoint_result":
                        digest = str(payload.get("candidate_digest", ""))
                        helper.inflight.pop(digest, None)
                        results[digest] = str(payload.get("kind", ""))
                        facility_ms.append(int(payload.get("facility_elapsed_ms", 0) or 0))
                        completed += 1
                        if first_result_at is None:
                            first_result_at = time.monotonic()
                        if batch and not helper.inflight:
                            generation_loaded[index] = False
            time.sleep(0.005)
        wall = time.monotonic() - dispatch_started
    finally:
        for helper in helpers:
            helper.close()
    return {
        "arm": name,
        "processes": processes,
        "slots_per_process": slots,
        "total_slots": processes * slots,
        "candidates": len(nodes),
        "completed": completed,
        "helpers_spawn_seconds": round(spawned_at - started, 3),
        "helpers_ready_seconds": round(helpers_ready_seconds, 3),
        "helpers_ready_includes_spawn": True,
        "dispatch_wall_seconds": round(wall, 3),
        "completions_per_second": round(completed / wall, 3) if wall > 0 else 0.0,
        "time_to_first_result_seconds": (
            round(first_result_at - dispatch_started, 3) if first_result_at else None
        ),
        "facility_elapsed_ms_median": (
            int(statistics.median(facility_ms)) if facility_ms else None
        ),
        "facility_elapsed_ms_total": sum(facility_ms),
        "verdicts": results,
        "scope": "local_equal_slot_not_network_acceptance",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--candidates", type=Path, required=True,
                        help="JSON list of node dicts")
    parser.add_argument("--control-processes", type=int, default=16)
    parser.add_argument("--control-slots", type=int, default=1)
    parser.add_argument("--batch-processes", type=int, default=4)
    parser.add_argument("--batch-slots", type=int, default=4)
    parser.add_argument("--budget", type=float, default=3.0)
    parser.add_argument("--probe-url", action="append", default=[])
    parser.add_argument("--allow-unequal-slots", action="store_true",
                        help="the SEPARATE concurrency question; never the equal-slot one")
    parser.add_argument("--batch-first", action="store_true",
                        help="run the batch arm first; alternate across repeats so "
                             "arm order cannot be confounded with drift")
    args = parser.parse_args()

    control_total = args.control_processes * args.control_slots
    batch_total = args.batch_processes * args.batch_slots
    if control_total != batch_total and not args.allow_unequal_slots:
        raise SystemExit(
            f"equal-slot condition violated: control has {control_total} slots, "
            f"batch has {batch_total}. This probe only answers the equal-slot question."
        )
    directory = args.directory.resolve()
    directory.relative_to(ROOT / ".build")
    directory.mkdir(parents=True, exist_ok=True)
    temp_parent = directory / "temp"
    temp_parent.mkdir(exist_ok=True)

    payload = json.loads(args.candidates.read_text(encoding="utf-8"))
    nodes = [Node.from_dict(item) for item in payload]
    probe_urls = args.probe_url or ["http://cp.cloudflare.com/generate_204"]

    report = {}
    arms = [
        ("control", args.control_processes, args.control_slots, False),
        ("batch", args.batch_processes, args.batch_slots, True),
    ]
    if args.batch_first:
        arms.reverse()
    for name, processes, slots, batch in arms:
        report[name] = run_arm(
            name, nodes, processes, slots, batch=batch, budget=args.budget,
            probe_urls=probe_urls, temp_parent=temp_parent,
        )
        (directory / f"result-{name}.json").write_text(
            json.dumps(report[name], indent=2), encoding="utf-8",
        )
        print(json.dumps({k: v for k, v in report[name].items() if k != "verdicts"}), flush=True)

    control_verdicts = report["control"]["verdicts"]
    batch_verdicts = report["batch"]["verdicts"]
    shared = set(control_verdicts) & set(batch_verdicts)
    disagreements = {
        digest: (control_verdicts[digest], batch_verdicts[digest])
        for digest in shared
        if control_verdicts[digest] != batch_verdicts[digest]
    }
    # A single disagreement count hides the only asymmetry that matters: a
    # candidate the control admitted and the batch did not.
    control_pass = {d for d in shared if control_verdicts[d] == "pass"}
    batch_pass = {d for d in shared if batch_verdicts[d] == "pass"}
    transitions: dict[str, int] = {}
    for digest, (before, after) in disagreements.items():
        key = f"{before}->{after}"
        transitions[key] = transitions.get(key, 0) + 1
    summary = {
        "question": (
            "equal_slot_shared_process" if control_total == batch_total
            else "concurrency_scaling_NOT_equal_slot"
        ),
        "control_slots": control_total,
        "batch_slots": batch_total,
        "arm_order": [name for name, _p, _s, _b in arms],
        "verdict_agreement": {
            "compared": len(shared),
            "disagreements": len(disagreements),
            "control_pass": len(control_pass),
            "batch_pass": len(batch_pass),
            "pass_lost_by_batch": sorted(control_pass - batch_pass),
            "pass_gained_by_batch": sorted(batch_pass - control_pass),
            "kind_transitions_control_to_batch": dict(
                sorted(transitions.items(), key=lambda item: -item[1])
            ),
            "examples": dict(list(disagreements.items())[:8]),
        },
        "scope": "local_equal_slot_not_network_acceptance",
    }
    (directory / "parity-summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8",
    )
    print(json.dumps(summary), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
