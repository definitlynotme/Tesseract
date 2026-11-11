"""Read-only sampling projections; expensive gateway reads use normal workers."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import logging
from threading import Lock
from time import monotonic

import bridge_protocol as protocol

_LOG = logging.getLogger("tesseract.bridge.tiers")
# Bastion trace (CLAUDE_CHANGES §8r, diagnostic, temporary): its own logger, child of
# tesseract.bridge (bridge.log), so it never mixes with this module's own records
_TRACE = logging.getLogger("tesseract.bridge.trace")


def owned(value):
    if isinstance(value, Mapping):
        return {key: owned(child) for key, child in value.items()}
    if isinstance(value, (tuple, list)):
        return [owned(child) for child in value]
    return value


def lifecycle_owner(sample):
    return (sample["intent"]["runtime_generation"],
            sample["intent"]["lifecycle_revision"],
            sample["core"]["core_generation"], sample["core"].get("pid"),
            sample["core"].get("process_generation"))


class SampledTopics:
    """One shared owner per epoch, independent of subscriber count.

    Unrepresentable unknown counts are withheld, never filled with zero. The
    frozen DTOs have no stale field: a withheld observation produces a gap,
    and recovery supplies a full snapshot. Observed times
    from polling getters are retained until the semantic projection changes.
    """

    def __init__(self, state, access, *, submit_normal, config_revision=lambda: 0,
                 clock=monotonic, retry_period=5.0):
        self.state, self.access = state, access
        self.submit_normal, self.config_revision = submit_normal, config_revision
        self.last = {}
        self.status = {}
        self._invalidated = set()
        self._transition = None
        self._configured_key = None
        self._configured = None
        self._lock = Lock()
        self._gateway_requested = None
        self._gateway_completed = None
        self._gateway_inflight = False
        self._gateway_valid = False
        self._gateway_attempt = None
        self._gateway_retry_at = 0.0
        self._gateway_recheck_at = 0.0
        # T-P2-H8: count of selected_node_id getter failures (field omitted).
        self.selected_node_id_errors = 0
        self.clock, self.retry_period = clock, max(1.0, retry_period)
        # Bastion trace (CLAUDE_CHANGES §8r, diagnostic): what the last projection did
        # (state / edge / unchanged / invalid / withheld) and the last traced line
        self._last_emit = None
        self._pool_trace = None
        self._pool_trace_lines = 0

    def invalidate(self, topic):
        self._invalidated.add(topic)
        if topic == "tier.transition":
            self._transition = None
        else:
            self.state.invalidate_state(topic, notify=True)
        if topic == "gateway.current":
            self._gateway_completed = None
        self.status[topic] = "stale" if topic in self.last else "unknown"

    def _emit(self, topic, dto, owner):
        if protocol.validate_topic_payload(topic, dto):
            self.invalidate(topic)
            self.status[topic] = "unknown"
            self._last_emit = "invalid"
            return False
        # Getter observation time is read time, not a new source fact.
        semantic = {key: value for key, value in dto.items() if key != "observed_at_ms"}
        key = (owner, semantic)
        previous = self.last.get(topic)
        if previous is None or topic in self._invalidated:
            self.state.record_state(topic, dto)
            self._last_emit = "state"
        elif previous != key:
            self.state.record_edge(topic, dto)
            self._last_emit = "edge"
        else:
            self._last_emit = "unchanged"
            return False
        self.last[topic] = deepcopy(key)
        self.status[topic] = "current"
        self._invalidated.discard(topic)
        return True

    def project(self, topic, raw, owner):
        raw = owned(raw)
        if (raw is None or raw.get("stale") or
                raw.get("generation", owner[0]) != owner[0]):
            if topic != "service.collect" or raw is None:
                self.invalidate(topic)
            if topic != "service.collect" or raw is None:
                self._last_emit = "withheld"
                return False
            # This DTO explicitly supports unknown, including nullable counts.
            raw = {**raw, "generation": owner[0], "tiers": [
                {"tier": tier, "generation": owner[0], "first_check": "unknown",
                 "source_count": None, "healthy": None, "required": None,
                 "observed_at_ms": None}
                for tier in ("vanguard", "sentinel", "bastion", "gateway")]}
        # Optional members ("name?") are projected under their wire name.
        fields = (field.rstrip("?") for field in protocol.V1_TOPIC_SPECS[topic][2])
        dto = {field: raw[field] for field in fields if field in raw}
        result = self._emit(topic, dto, owner)
        if topic == "service.collect" and (raw.get("stale") or
                any(row["first_check"] == "unknown" for row in raw["tiers"])):
            # Unknown is an actual published DTO, not a stale zero-count sample.
            self.status[topic] = "unknown"
        return result

    def transition(self, lifecycle, route):
        owner = lifecycle_owner(lifecycle)
        tier = route.get("managed_tier") if route else None
        route_owner = route.get("owner") or {} if route else {}
        valid = (route and route.get("valid") and not lifecycle["freshness"]["stale"]
                 and tier in {"vanguard", "sentinel", "bastion", "gateway"}
                 and route_owner.get("core_generation") == owner[2]
                 and route_owner.get("process_pid") == owner[3]
                 and route_owner.get("process_generation") == owner[4]
                 and type(route.get("observed_at_ms")) is int)
        if not valid:
            self._transition = None
            self.status["tier.transition"] = "unknown"
            return
        before = self._transition
        self._transition = (owner, tier)
        self.status["tier.transition"] = "current"
        if before is not None and before[0] == owner and before[1] != tier:
            self.state.record_edge("tier.transition", {
                "from": before[1], "to": tier, "observed_at_ms": route["observed_at_ms"],
                "authority": "observed"})

    def sample_protocols(self, lifecycle, config_key):
        owner = lifecycle_owner(lifecycle)
        if lifecycle["core"]["running"]:
            raw = self.access.protocol_mix(source="loaded")
        else:
            if config_key != self._configured_key:
                self._configured = owned(self.access.protocol_mix(source="configured"))
                if not self._configured.get("stale"):
                    self._configured_key = config_key
            raw = self._configured
        after = self.access.lifecycle_status()
        if after["freshness"]["stale"] or lifecycle_owner(after) != owner:
            raw = {**raw, "stale": True}
        return self.project("protocols.mix", raw, owner)

    def _gateway_token(self):
        from bridge_runtime_access import sampling_config_identity
        lifecycle = self.access.lifecycle_status()
        route = self.access.published_route_status()
        return (self._gateway_route_token(route), self.config_revision(),
                sampling_config_identity(self.access), lifecycle_owner(lifecycle))

    def _gateway_route_token(self, route):
        """Route publication token plus the loaded export contract (render
        receipt) revision (T-P2-H8): a reload that installs a new receipt
        without republishing the route (no-capture standby) still re-reads
        gateway.current, so a published ``selected_node_id`` never outlives
        the receipt it was reversed through until the 30 s recheck."""
        from bridge_runtime_access import loaded_export_revision
        try:
            receipt = loaded_export_revision(self.access)
        except Exception as exc:
            _LOG.debug("gateway.current: loaded export revision unavailable (%s)",
                       type(exc).__name__)
            receipt = None
        return (*self._route_token(route), receipt)

    @staticmethod
    def _route_token(route):
        return (route["publication_revision"], tuple(sorted((route.get("owner") or {}).items())))

    @staticmethod
    def _route_current(lifecycle, route):
        owner = lifecycle_owner(lifecycle)
        route_owner = route.get("owner") or {} if route else {}
        return bool(route and route.get("valid") and not lifecycle["freshness"]["stale"]
                    and type(owner[3]) is int and type(owner[4]) is int
                    and route_owner.get("core_generation") == owner[2]
                    and route_owner.get("process_pid") == owner[3]
                    and route_owner.get("process_generation") == owner[4])

    @staticmethod
    def _core_stopped(lifecycle):
        """T-P2-H2 (33-#13): no Core process and no pending cleanup. Gateway
        then publishes the saved selection intent (effective null, applied
        false), not stale; there is no published route to compare with."""
        core = lifecycle["core"]
        return (not core["running"] and not core.get("cleanup_pending")
                and not lifecycle["freshness"]["stale"])

    def _gateway_basis(self, lifecycle, route, versions_known):
        """Stopped Core: the saved intent needs no route and no owner fact
        versions, but it is published only while ``id(config)`` and the
        config revision stay what the read saw: both are in the gateway
        token, so a change withdraws the baseline (gap) at the next sample
        and a read that straddles one is dropped (T-P2-H6, 决定_第六波裁定
        H2-4). Running Core: a current published route AND the owners' fact
        versions (DesktopRuntime.bridge_sampling_fact_versions, T-P2-H6)."""
        return self._core_stopped(lifecycle) or (
            versions_known and self._route_current(lifecycle, route))

    def _gateway_current(self):
        from bridge_runtime_access import sampling_fact_versions
        lifecycle = self.access.lifecycle_status()
        return self._gateway_basis(lifecycle, self.access.published_route_status(),
                                   sampling_fact_versions(self.access) is not None)

    def schedule_gateway(self, token, *, valid=True):
        # snapshot() checks subscription without invoking any runtime getter.
        try:
            self.state.snapshot("gateway.current")
        except ValueError:
            return
        with self._lock:
            self._gateway_requested = token
            if self._gateway_completed is not None and token != self._gateway_completed:
                self.invalidate("gateway.current")
            restored = valid and not self._gateway_valid
            self._gateway_valid = valid
            if not valid:
                self.invalidate("gateway.current")
                return
            # Periodic redaction rechecks share the fact-change worker and lock.
            if self._gateway_inflight:
                return
            if token == self._gateway_completed and self.clock() < self._gateway_recheck_at:
                return
            if (not restored and token == self._gateway_attempt
                    and self.clock() < self._gateway_retry_at):
                return
            self._gateway_inflight = True
        if not self.submit_normal(self._read_gateway):
            with self._lock:
                self._gateway_inflight = False

    def _selected_node_id(self, raw):
        """T-P2-H8: optional ``selected_node_id`` for this gateway read.

        Read between the same two gateway tokens as ``raw`` (route
        publication revision, config revision and identity with the saved
        selection's fact version, lifecycle owner), so both describe one
        published route and one saved selection; a change in between drops
        the whole read. Only a current running read with a saved selection
        that the loaded EXIT-SELECT holds asks the runtime; any doubt or
        getter failure omits the field (never a guess, never a name).
        """
        from bridge_runtime_access import gateway_selected_node_id
        if (raw is None or raw.get("stale") or not raw.get("selected")
                or raw.get("missing") is not False):
            return None
        try:
            return gateway_selected_node_id(self.access, generation=raw.get("generation"))
        except Exception as exc:
            # Omitting stays the wire answer; leave a data-free trace (type only).
            self.selected_node_id_errors += 1
            _LOG.debug("gateway.current: selected_node_id omitted, getter raised %s",
                       type(exc).__name__)
            return None

    def _read_gateway(self):
        try:
            with self._lock:
                token = self._gateway_requested
            # All runtime reads here execute in the normal executor domain.
            if self._gateway_token() != token or not self._gateway_current():
                return
            raw = owned(self.access.gateway_status())
            selected_node_id = self._selected_node_id(raw)
            after = self._gateway_token()
            valid = self._gateway_current()
            with self._lock:
                if (not valid or not self._gateway_valid or after != token
                        or token != self._gateway_requested):
                    self.invalidate("gateway.current")
                    return
                raw.pop("selected_node_id", None)
                if selected_node_id is not None:
                    raw["selected_node_id"] = selected_node_id
                self.project("gateway.current", raw, token[-1])
                if self.status.get("gateway.current") == "current":
                    self._gateway_completed = token
        except Exception:
            self.invalidate("gateway.current")
            raise
        finally:
            with self._lock:
                self._gateway_attempt = token
                finished_at = self.clock()
                self._gateway_retry_at = finished_at + self.retry_period
                self._gateway_recheck_at = finished_at + 30.0
                self._gateway_inflight = False

    POOL_TRACE_MAX = 400

    def _trace_pool(self, sampled, moved, owner):
        """Bastion trace (CLAUDE_CHANGES §8r, diagnostic, temporary): one bridge.log line each
        time what the sampler did with bastion.pool changes (outcome, why it was withheld, the
        count), so a run shows whether a count ever left the sidecar. Numbers and fixed codes
        only; change-only and capped per process."""
        raw = sampled if isinstance(sampled, Mapping) else {}
        outcome = self._last_emit or "none"
        reason = "-"
        if outcome == "withheld":
            reason = (raw.get("stale_reason") if raw.get("stale") else None) or moved or (
                "generation" if raw.get("generation", owner[0]) != owner[0] else "no_sample")
        line = (outcome, reason, raw.get("runtime_alive"), raw.get("target_alive"), owner[1], owner[2])
        if line == self._pool_trace or self._pool_trace_lines >= self.POOL_TRACE_MAX:
            return
        self._pool_trace = line
        self._pool_trace_lines += 1
        _TRACE.info("bastion.pool sample outcome=%s reason=%s alive=%s target=%s lifecycle_revision=%s core_generation=%s",
                    *line)

    def sample(self, observe):
        from bridge_runtime_access import sampling_config_identity
        try:
            lifecycle = owned(self.access.lifecycle_status())
            owner = lifecycle_owner(lifecycle)
            route = owned(self.access.published_route_status())
            config_key = (self.config_revision(), sampling_config_identity(self.access), owner)
        except Exception:
            for topic in ("service.collect", "protocols.mix", "network.mode", "bastion.pool",
                          "gateway.current", "tier.transition"):
                self.invalidate(topic)
            raise

        def guarded(topic, action):
            def work():
                try:
                    return action()
                except Exception:
                    self.invalidate(topic)
                    raise
            return observe(topic, work)

        for topic, getter in (("service.collect", self.access.collect_status),
                              ("network.mode", self.access.network_mode),
                              ("bastion.pool", self.access.bastion_pool)):
            def project(topic=topic, getter=getter):
                raw = getter()
                sampled, moved = raw, None
                after = self.access.lifecycle_status()
                if after["freshness"]["stale"] or lifecycle_owner(after) != owner:
                    moved = "lifecycle_stale" if after["freshness"]["stale"] else "owner_changed"
                    raw = {**raw, "stale": True}
                self._last_emit = None
                self.project(topic, raw, owner)
                if topic == "bastion.pool":
                    self._trace_pool(sampled, moved, owner)
            guarded(topic, project)
        guarded("protocols.mix", lambda: self.sample_protocols(lifecycle, config_key))
        after = self.access.lifecycle_status()
        if after["freshness"]["stale"] or lifecycle_owner(after) != owner:
            route = None
        guarded("tier.transition", lambda: self.transition(lifecycle, route))
        if route is None:
            self.schedule_gateway((None, *config_key[:2], owner), valid=False)
            return
        token = (self._gateway_route_token(route), *config_key[:2], owner)
        observe("gateway.schedule", lambda: self.schedule_gateway(token,
            valid=self._gateway_basis(lifecycle, route, config_key[1][-1] is not None)))
