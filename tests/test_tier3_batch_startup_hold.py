"""The Tier3 cold-start helper hold counts processes, not identities.

On a fresh-source cold start (no cached, replayed or local candidate supply)
the manager holds the helper target until the first remote source transport
starts.  The reason for the hold is OS process creation competing with the
bootstrap Core and UI, so it is four helper *processes*:

* single-slot backend (``validation_slots_per_process`` 1, the default): four
  identities, exactly as before;
* batch backend (K identities per process): four groups, i.e. ``4 * K``
  identities.  Holding four identities there would start one group and ramp
  like a single process until sources deliver.

The hold is a cap.  The bind-time floor stays four identities (the domain's
``min_workers``), so when pressure sizing asks for one helper process a batch
session starts one group, held or not, as before.

Each test runs one real ``refresh_tier3`` session with deterministic helpers.
The fake source collector holds the first transport until the test has seen
the cohort it expects, then starts it (or ends without one).  Recorded on both
sides of that moment:

* every process target the manager hands the validation domain
  (``set_envelope`` desired workers and ``set_shell_prewarm_workers``), in
  identities, and the envelope controller's initial count;
* every helper process the supervisor asks to spawn (worker id on the
  single-slot backend, group index on the batch backend), and every helper it
  activates (the ``start`` command that brings up the helper's Mihomo core).
"""
from __future__ import annotations

import dataclasses
import logging
import threading
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

import pytest

import manager as manager_module
import tester as tester_module
import tier3_validator_supervisor as supervisor_module
from alerts import AlertManager
from manager import ProxyTierManager
from models import Node, NodeStatus
from test_support.tier3_batch_deterministic_helper import (
    batch_deterministic_helper_bootstrap,
)
from test_support.tier3_deterministic_helper import deterministic_helper_bootstrap
from tier3_sources import CollectionReport
from tier3_validation_domain import Tier3ValidationDomain

# full_validation_process_max: keeps each session at six helper processes so
# the envelope after the transport (six) is distinguishable from the hold
# (four) without spawning the audited sixteen.
_PROCESS_MAX = 6
_SOURCE_URL = "https://opaque.example/startup-hold"


def _minimal_manager(temp_path: Path, slots_per_process: int) -> ProxyTierManager:
    nodes_file = temp_path / "nodes.json"
    nodes_file.write_text(
        '{"generated_at":"","metadata":{"tier":"tier3","source_count":0},"nodes":[]}',
        encoding="utf-8",
    )
    tier2_cache_file = temp_path / "tier2_cache.json"
    tier2_cache_file.write_text(
        '{"generated_at":"","metadata":{"tier":"tier2","source_urls":[]},"nodes":[]}',
        encoding="utf-8",
    )
    sources_file = temp_path / "sources.yaml"
    sources_file.write_text(
        f"subscription_urls:\n  - {_SOURCE_URL}\n"
        "github_repos: []\ntelegram_channels: []\nlocal_files: []\n",
        encoding="utf-8",
    )
    exit_cache_file = temp_path / "exit_cache.json"
    config = {
        "paths": {
            "sources_file": str(sources_file),
            "tier3_nodes_file": str(nodes_file),
            "tier2_cache_file": str(tier2_cache_file),
            "exit_cache_file": str(exit_cache_file),
            "tier_observation_cache_file": str(
                temp_path / "tier_observation_cache.json"
            ),
        },
        "tiers": {
            "tier1": {"nodes": [], "check_interval": 30},
            "tier2": {
                "subscription_urls": [],
                "cache_file": str(tier2_cache_file),
                "check_interval": 60,
            },
            "tier3": {
                "enabled": True,
                "check_interval": 120,
                "auto_collect_interval": 3600,
                "max_pool_size": 100,
                "bootstrap_enabled": False,
                "subscription_urls": [_SOURCE_URL],
                "validation_concurrency_min": 4,
                "validation_concurrency_max": 4,
                "full_validation_process_max": _PROCESS_MAX,
                "validation_slots_per_process": slots_per_process,
            },
            "exit": {"cache_file": str(exit_cache_file), "check_interval": 60},
        },
        "testing": {
            "tcp_timeout": 1,
            "concurrency": 5,
            "slow_threshold_ms": 1000,
            "validation_mode": "tcp_only",
        },
    }
    silent = {"enabled": False}
    return ProxyTierManager(
        config=config,
        logger=logging.getLogger("test-tier3-batch-startup-hold"),
        alerts=AlertManager(
            {"desktop": silent, "telegram": silent, "sound": silent},
            logging.getLogger("test-tier3-batch-startup-hold-alerts"),
        ),
    )


def _wait_for(predicate: Callable[[], bool], timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


def _candidate(index: int, tag: str) -> Node:
    return Node(
        name=f"{tag}-{index}",
        protocol="ss",
        address=f"{tag}-{index}.invalid",
        port=443,
        method="aes-128-gcm",
        password="fixture",
        tier="tier3",
        source=_SOURCE_URL,
    )


class _Session:
    """One real ``refresh_tier3`` session with recorded targets and helpers.

    ``backlog`` pre-seeds candidates, so the session has immediate supply and
    is never held.  ``one_process_sizing`` makes pressure sizing ask for one
    helper process: budget pressure 0 with one Mihomo process, and a PI
    target of at most K identities.  ``deliver_candidates=False`` ends the
    collection without a transport, so the hold is never lifted.
    """

    def __init__(
        self,
        tmp_path: Path,
        slots_per_process: int,
        *,
        backlog: int = 0,
        one_process_sizing: bool = False,
        deliver_candidates: bool = True,
    ) -> None:
        self.slots_per_process = int(slots_per_process)
        self.one_process_sizing = bool(one_process_sizing)
        self.deliver_candidates = bool(deliver_candidates)
        self.owner = _minimal_manager(tmp_path, slots_per_process)
        self.owner.mark_tier3_core_running(effective_egress_ok=True)
        self.owner.set_effective_egress_state(True)
        self.owner.runtime_source_fetch_proxy_provider = (
            lambda _phase: "http://127.0.0.1:19131"
        )
        if backlog:
            added = self.owner._extend_tier3_candidate_backlog(
                [_candidate(index, "backlog") for index in range(backlog)]
            )
            assert added == backlog
        self.candidates = [_candidate(index, "hold-candidate") for index in range(12)]
        self.lock = threading.Lock()
        self.transport_started = False
        self.release_transport = threading.Event()
        # (after the first transport, desired identities, dispatch enabled)
        self.targets: list[tuple[bool, int, bool]] = []
        self.shells: list[tuple[bool, int]] = []
        self.spawns: list[tuple[bool, int, int]] = []
        # (after the first transport, helper process index)
        self.activations: list[tuple[bool, int]] = []
        self.initial_counts: list[int] = []
        self.domains: list[Tier3ValidationDomain] = []
        self.errors: list[BaseException] = []
        self.worker = threading.Thread(
            target=self._refresh, name="startup-hold-refresh", daemon=True,
        )

    # -- recorders -------------------------------------------------------

    def _refresh(self) -> None:
        try:
            self.owner.refresh_tier3(
                suppress_intervention_alert=True, startup_mode=False,
            )
        except BaseException as exc:  # pragma: no cover - reported by tests
            self.errors.append(exc)

    def started(self) -> bool:
        with self.lock:
            return bool(self.transport_started)

    def spawned(self, *, after: bool) -> set[int]:
        with self.lock:
            return {
                index for flag, index, _generation in self.spawns if flag is after
            }

    def all_activated(self) -> set[int]:
        with self.lock:
            return {index for _flag, index in self.activations}

    def seen_after(self, value: int) -> bool:
        with self.lock:
            return any(
                flag and count == value for flag, count, _enabled in self.targets
            ) and any(flag and count == value for flag, count in self.shells)

    def _patches(self) -> list[Any]:
        real_domain = manager_module.Tier3ValidationDomain
        real_set_envelope = Tier3ValidationDomain.set_envelope
        real_set_shell = Tier3ValidationDomain.set_shell_prewarm_workers
        real_submit = supervisor_module.Tier3WorkerStarter.submit
        real_send = supervisor_module.Tier3ValidatorSupervisor._send
        real_controller = manager_module.Tier3ValidatorEnvelopeController
        real_budget = ProxyTierManager._tier3_validation_budget
        real_expansion = ProxyTierManager._tier3_expansion_envelope_decision
        session = self

        def domain_factory(*args: Any, **kwargs: Any) -> Tier3ValidationDomain:
            batch = int(kwargs.get("slots_per_process", 1) or 1) > 1
            kwargs["binary_path"] = "deterministic-test-helper"
            kwargs["helper_target"] = (
                batch_deterministic_helper_bootstrap
                if batch
                else deterministic_helper_bootstrap
            )
            domain = real_domain(*args, **kwargs)
            session.domains.append(domain)
            return domain

        def record_envelope(domain, desired_workers, *args, **kwargs):
            with session.lock:
                session.targets.append(
                    (
                        bool(session.transport_started),
                        int(desired_workers),
                        bool(kwargs.get("dispatch_enabled", True)),
                    )
                )
            return real_set_envelope(domain, desired_workers, *args, **kwargs)

        def record_shell(domain, count):
            with session.lock:
                session.shells.append((bool(session.transport_started), int(count)))
            return real_set_shell(domain, count)

        def record_submit(starter, worker_id, generation):
            with session.lock:
                session.spawns.append(
                    (bool(session.transport_started), int(worker_id), int(generation))
                )
            return real_submit(starter, worker_id, generation)

        def record_send(supervisor, worker, kind, payload):
            if kind == "start":
                # The batch lead is its group's first member id.
                helper_index = (
                    int(payload["worker_id"]) - 1
                ) // session.slots_per_process + 1
                with session.lock:
                    session.activations.append(
                        (bool(session.transport_started), helper_index)
                    )
            return real_send(supervisor, worker, kind, payload)

        def record_controller(*args: Any, **kwargs: Any):
            session.initial_counts.append(int(kwargs["initial_count"]))
            return real_controller(*args, **kwargs)

        def one_process_budget(manager_self, *args: Any, **kwargs: Any):
            return dataclasses.replace(
                real_budget(manager_self, *args, **kwargs),
                tier3_throughput_pressure=0.0,
                mihomo_process_count=1,
            )

        def one_process_expansion(manager_self, *args: Any, **kwargs: Any):
            decision = real_expansion(manager_self, *args, **kwargs)
            return dataclasses.replace(
                decision,
                desired_helpers=min(
                    int(decision.desired_helpers), session.slots_per_process,
                ),
            )

        patches = [
            patch.object(manager_module, "Tier3ValidationDomain", domain_factory),
            patch.object(Tier3ValidationDomain, "set_envelope", record_envelope),
            patch.object(
                Tier3ValidationDomain, "set_shell_prewarm_workers", record_shell,
            ),
            patch.object(
                supervisor_module.Tier3WorkerStarter, "submit", record_submit,
            ),
            patch.object(
                supervisor_module.Tier3ValidatorSupervisor, "_send", record_send,
            ),
            patch.object(
                manager_module, "Tier3ValidatorEnvelopeController", record_controller,
            ),
            patch.object(
                manager_module, "collect_nodes_from_sources_with_report", self._collect,
            ),
            patch.object(tester_module, "_tcp_probe", _dead_probe),
        ]
        if self.one_process_sizing:
            patches += [
                patch.object(
                    ProxyTierManager, "_tier3_validation_budget", one_process_budget,
                ),
                patch.object(
                    ProxyTierManager,
                    "_tier3_expansion_envelope_decision",
                    one_process_expansion,
                ),
            ]
        return patches

    async def _collect(self, *_args: Any, **kwargs: Any):
        # This collector thread owns the first transport; blocking it holds
        # the session in its pre-transport state until the test releases it.
        self.release_transport.wait(60.0)
        report = CollectionReport(
            source_count=1,
            fetched_sources=1,
            node_count=len(self.candidates) if self.deliver_candidates else 0,
            sources=[{"type": "subscription_url", "value": _SOURCE_URL, "ok": True}],
        )
        if not self.deliver_candidates:
            return [], report
        observer = dict(kwargs.get("settings") or {}).get(
            "source_unit_transport_started_observer"
        )
        with self.lock:
            self.transport_started = True
        if callable(observer):
            observer(
                f"subscription_url:{_SOURCE_URL}", "default", "core", time.monotonic(),
            )
        source_observer = kwargs.get("source_observer")
        if callable(source_observer):
            source_observer(list(self.candidates), report)
        return list(self.candidates), report

    # -- driver ----------------------------------------------------------

    def run(
        self,
        drive: Callable[["_Session"], None],
        *,
        stop_after_drive: bool,
    ) -> dict[str, Any]:
        """Start the session, let ``drive`` observe it, then end it.

        With ``stop_after_drive`` the session is stopped right after
        ``drive``; otherwise it must end on its own and its final collection
        report is returned.
        """

        observed: dict[str, Any] = {}
        try:
            with ExitStack() as stack:
                for active_patch in self._patches():
                    stack.enter_context(active_patch)
                self.worker.start()
                try:
                    drive(self)
                finally:
                    self.release_transport.set()
                    if stop_after_drive:
                        self.owner.request_stop()
                    self.worker.join(30.0)
                    observed["ended_on_its_own"] = not self.worker.is_alive()
                    if self.worker.is_alive():
                        self.owner.request_stop()
                        self.worker.join(30.0)
            observed["worker_alive_after_stop"] = self.worker.is_alive()
            observed["report"] = dict(self.owner.tier3_last_collection_report or {})
            with self.lock:
                observed.update(
                    {
                        "transport_started": bool(self.transport_started),
                        "targets": list(self.targets),
                        "shells": list(self.shells),
                        "spawns": list(self.spawns),
                        "activations": list(self.activations),
                        "initial_counts": list(self.initial_counts),
                        "errors": list(self.errors),
                        "domain_slots": [
                            int(domain.slots_per_process) for domain in self.domains
                        ],
                        "supervisor_classes": [
                            type(domain.supervisor).__name__ for domain in self.domains
                        ],
                    }
                )
            return observed
        finally:
            self.release_transport.set()
            self.owner.close(timeout_seconds=3.0)


async def _dead_probe(node: Node, **_kwargs: Any) -> Node:
    updated = node.clone()
    updated.status = NodeStatus.DEAD
    updated.suspicious_reason = "tcp connection refused"
    return updated


def _held(records: list[tuple]) -> list[int]:
    return [int(record[1]) for record in records if not record[0]]


def _after(records: list[tuple]) -> list[int]:
    return [int(record[1]) for record in records if record[0]]


def _enabled(targets: list[tuple[bool, int, bool]]) -> list[int]:
    return [count for _started, count, enabled in targets if enabled]


def _spawned(spawns: list[tuple[bool, int, int]], *, after: bool) -> set[int]:
    return {index for started, index, _generation in spawns if started is after}


def _detail(observed: dict[str, Any]) -> dict[str, Any]:
    report = observed.get("report") or {}
    return {
        **{
            key: observed.get(key)
            for key in (
                "targets", "shells", "spawns", "activations", "initial_counts",
                "errors", "supervisor_classes",
            )
        },
        "report": {
            key: report.get(key)
            for key in (
                "source_stream_helper_startup_staged",
                "source_stream_helper_startup_cap",
                "source_stream_helper_startup_release_reason",
                "source_stream_source_transport_started_ms",
            )
        },
    }


_BACKENDS = pytest.mark.parametrize(
    ("slots_per_process", "backend", "hold_identities", "cap_identities"),
    [
        # Single-slot default: four identities, the historical hold.
        (1, "Tier3ValidatorSupervisor", 4, _PROCESS_MAX),
        # Batch backend, K = 8: four processes = 32 identities.
        (8, "Tier3BatchValidatorSupervisor", 32, 8 * _PROCESS_MAX),
    ],
    ids=["single_slot", "batch_k8"],
)


@_BACKENDS
def test_cold_start_hold_is_four_helper_processes_until_first_transport(
    tmp_path: Path,
    slots_per_process: int,
    backend: str,
    hold_identities: int,
    cap_identities: int,
) -> None:
    session = _Session(tmp_path, slots_per_process)

    observed_live: dict[str, Any] = {}

    def drive(current: _Session) -> None:
        # The supervisor starts after the initial envelope is bound; the
        # whole held cohort is requested in its first pass.
        observed_live["held_cohort_requested"] = _wait_for(
            lambda: len(current.spawned(after=False)) >= 4, 45.0,
        )
        # A fifth helper would be requested within this window.
        time.sleep(0.5)
        observed_live["session_alive_in_hold"] = current.worker.is_alive()
        observed_live["started_before_release"] = current.started()
        current.release_transport.set()
        observed_live["full_target_after"] = _wait_for(
            lambda: current.seen_after(cap_identities), 30.0,
        )
        observed_live["spawned_beyond_hold_after"] = _wait_for(
            lambda: any(index > 4 for index in current.spawned(after=True)), 60.0,
        )

    observed = session.run(drive, stop_after_drive=True)
    observed.update(observed_live)
    detail = _detail(observed)

    assert not observed["errors"], detail
    assert not observed["worker_alive_after_stop"], detail
    assert observed["domain_slots"] == [slots_per_process], detail
    assert observed["supervisor_classes"] == [backend], detail
    assert observed["session_alive_in_hold"], detail
    assert not observed["started_before_release"], detail

    # Before the first transport: every target is the hold, never less than
    # four processes and never more.
    held_targets = _held(observed["targets"])
    held_shells = _held(observed["shells"])
    assert held_targets and set(held_targets) == {hold_identities}, detail
    assert held_shells and set(held_shells) == {hold_identities}, detail
    assert hold_identities // slots_per_process == 4
    # The envelope controller starts from the held cohort too.
    assert observed["initial_counts"] == [hold_identities], detail
    # Physically: exactly four helper processes were requested (worker ids on
    # the single-slot backend, group indices on the batch backend).
    assert observed["held_cohort_requested"], detail
    assert _spawned(observed["spawns"], after=False) == {1, 2, 3, 4}, detail

    # After it: the full envelope, and helper processes beyond the hold.
    assert observed["full_target_after"], detail
    assert max(_after(observed["targets"])) == cap_identities, detail
    assert max(_after(observed["shells"])) == cap_identities, detail
    assert observed["spawned_beyond_hold_after"], detail
    assert _spawned(observed["spawns"], after=True) - {1, 2, 3, 4} <= set(
        range(5, _PROCESS_MAX + 1)
    ), detail


@_BACKENDS
def test_session_ending_before_any_transport_reports_the_process_hold(
    tmp_path: Path,
    slots_per_process: int,
    backend: str,
    hold_identities: int,
    cap_identities: int,
) -> None:
    # Sources end without a transport, so the hold is never lifted and the
    # final report still names it (source_stream_helper_startup_cap).
    session = _Session(tmp_path, slots_per_process, deliver_candidates=False)

    observed_live: dict[str, Any] = {}

    def drive(current: _Session) -> None:
        observed_live["held_cohort_requested"] = _wait_for(
            lambda: len(current.spawned(after=False)) >= 4, 45.0,
        )
        time.sleep(0.5)

    observed = session.run(drive, stop_after_drive=False)
    observed.update(observed_live)
    detail = _detail(observed)
    report = observed["report"]

    assert not observed["errors"], detail
    assert observed["ended_on_its_own"], detail
    assert observed["supervisor_classes"] == [backend], detail
    assert not observed["transport_started"], detail
    assert observed["held_cohort_requested"], detail
    assert set(_enabled(observed["targets"])) == {hold_identities}, detail
    assert set(_held(observed["shells"])) == {hold_identities}, detail
    assert observed["initial_counts"] == [hold_identities], detail
    assert _spawned(observed["spawns"], after=False) == {1, 2, 3, 4}, detail
    assert report.get("source_stream_helper_startup_staged") is True, detail
    assert report.get("source_stream_helper_startup_cap") == hold_identities, detail
    assert report.get("source_stream_source_transport_started_ms") is None, detail
    assert hold_identities < cap_identities


@pytest.mark.parametrize("held", [True, False], ids=["held", "immediate_supply"])
@pytest.mark.parametrize(
    ("slots_per_process", "max_target", "helpers"),
    [
        # Single-slot: the four-identity floor is four processes, as before.
        (1, 4, {1, 2, 3, 4}),
        # Batch K = 8: pressure sizing asks for one group; the floor (four
        # identities) and the hold (a cap) keep it at one group.
        (8, 8, {1}),
    ],
    ids=["single_slot", "batch_k8"],
)
def test_bind_floor_stays_four_identities_when_sizing_asks_for_one_process(
    tmp_path: Path,
    slots_per_process: int,
    max_target: int,
    helpers: set[int],
    held: bool,
) -> None:
    session = _Session(
        tmp_path,
        slots_per_process,
        backlog=0 if held else 3,
        one_process_sizing=True,
    )

    observed_live: dict[str, Any] = {}

    def drive(current: _Session) -> None:
        observed_live["first_helper_activated"] = _wait_for(
            lambda: bool(current.all_activated()), 45.0,
        )
        # Helpers beyond the sizing would be activated within this window
        # (a four-group floor activated them within 0.5 s of the first).
        _wait_for(lambda: not current.all_activated() <= helpers, 2.0)

    observed = session.run(drive, stop_after_drive=False)
    observed.update(observed_live)
    detail = _detail(observed)
    report = observed["report"]

    assert not observed["errors"], detail
    assert observed["ended_on_its_own"], detail
    assert observed["first_helper_activated"], detail
    assert observed["domain_slots"] == [slots_per_process], detail
    # The scenario is what it claims to be.
    assert report.get("source_stream_helper_startup_staged") is held, detail
    assert report.get("source_stream_helper_startup_release_reason") == (
        "source_transport_started" if held else "immediate_supply"
    ), detail
    if held:
        assert _held(observed["targets"]), detail
    # No target above what the sizing asked for, before or after the
    # transport, and no helper activated beyond it.  (Helper shells are
    # still prewarmed up to the shell target; only activation is sized.)
    assert max(_enabled(observed["targets"])) == max_target, detail
    activated = {index for _started, index in observed["activations"]}
    assert 1 in activated and activated <= helpers, detail
