"""P2-06 named node/subscription projections; no Qt rows cross the wire.

Create one NodeProjection per runtime, shared by all pages. Call sample() on
the normal node-owner worker on fact changes, never on a publication tick.
The access getters own copies made under runtime/manager locks; projection
only works on those owned records, outside those locks. Refresh jobs publish
facts through their owner, rather than editing these returned lists.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import math
import re
import threading
from typing import Any

from bridge_runtime_access import display_filter
from bridge_vocab import PROTOCOL_TIERS
from node_status_view import _runtime_failure_display_category, subscription_group_key
from subscription_display_view import (
    SubscriptionDisplayGate,
    project_subscription_rows_for_display,
    subscription_core_stamp,
)


_UUID = re.compile(r"\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b")
_HEALTHY = frozenset({"alive", "slow", "running", "active", "healthy"})
_FAILED = frozenset({"dead", "suspicious", "error", "failed"})
_OPAQUE_ID = re.compile(r"[0-9a-f]{24}\Z")
_PROTOCOL_KEY = re.compile(r"[a-z][a-z0-9_]{0,31}")


class StaleNodeSample(ValueError):
    """Discard this sample and wait for the next owner fact change."""


class IncompleteNodeSample(ValueError):
    """Owner facts are missing; do not publish a complete list DTO."""


def _owned(value: Any) -> Any:
    """Thaw access-layer immutable records without retaining runtime objects."""
    if isinstance(value, Mapping):
        return {key: _owned(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_owned(child) for child in value]
    return value


def _uint(value: Any) -> int | None:
    if type(value) is int:
        return value if value >= 0 else None
    if type(value) is not float or not math.isfinite(value) or value < 0:
        return None
    return int(value)


def _record_id(value: Any) -> str:
    # P2-04 IDs are already opaque. Applying text redaction to their hex digits
    # could change an ID when a short credential happens to match a substring.
    if not isinstance(value, str) or not _OPAQUE_ID.fullmatch(value):
        raise ValueError("invalid opaque record id")
    return value


def _status(value: Any) -> str:
    if value in _HEALTHY:
        return "healthy"
    if value in _FAILED:
        return "failed"
    return value if value in {"pending", "disabled"} else "unknown"


def _source_keys(row: Mapping[str, Any]) -> set[tuple[str, str]]:
    values = list(row.get("source_ids") or ())
    if row.get("source_id"):
        values.append(row["source_id"])
    return {(row["tier"], value) for value in values if isinstance(value, str) and value}


def _gate_inputs(subscriptions: list[dict[str, Any]], evidence: Mapping,
                 *, baseline: bool = False):
    """Adapt *source receipts*, never translated node rows, to the shared gate.

    One synthetic row per source gives shared nodes URL-exclusive evidence.
    Only receipts matching inventory version, exact members, Core and refresh
    job may enter the gate. Legacy P2-04 source timestamps are insufficient.
    Labels/connections are empty; source values are opaque IDs, never URLs.
    """
    configs, rows, keys = [], [], {}
    for sub in subscriptions:
        if not sub["enabled"] or sub["tier"] not in {"sentinel", "gateway"}:
            continue
        source = (sub["tier"], sub["source_id"])
        if source in keys:
            continue
        item = {"tier": PROTOCOL_TIERS[sub["tier"]], "kind": "subscription",
                "enabled": True, "url": sub["source_id"]}
        keys[source] = subscription_group_key(item)
        configs.append(item)
        checked = evidence.get(source)
        live = checked is not None
        observed = ""
        if live and not baseline:
            try:
                observed = datetime.fromtimestamp(checked / 1000, timezone.utc).isoformat()
            except (ValueError, OverflowError, OSError):
                live = False
        rows.append([item["tier"], "", "", "", "unknown", "", "", "", "", "",
                     {"identity": sub["source_id"], "source": sub["source_id"],
                      "observation_origin": "live" if live and not baseline else "persisted_cache",
                      "observed_at": observed}])
    return configs, rows, keys


class NodeProjection:
    """Single node-owner gate; fresh detached DTOs on every accepted sample."""

    def __init__(self, access: Any = None, *, refresh_owner: Any = None) -> None:
        self.access = access
        self.refresh_owner = refresh_owner
        self.gate = SubscriptionDisplayGate()
        self._lock = threading.RLock()
        self._generation: int | None = None
        self._previous: dict[str, tuple[frozenset[str], int]] = {}
        self._members: dict[str, tuple[Any, ...]] = {}
        self._refresh_jobs: dict[str, int] = {}
        self._close_pending = False
        self._closed_stamp: tuple[Any, ...] | None = None

    def close(self) -> None:
        """Lifecycle owner closes health before Stop/Restart is scheduled."""
        with self._lock:
            if self.gate.initialized:
                self._closed_stamp = self.gate.core_stamp
            else:
                self._close_pending = True
            self.gate.close()

    def sample(self) -> dict[str, Any]:
        """Sample copied access facts; reject changes across the read boundary.

        No controller calls, gateway_status(), config reads or route refresh.
        Subscription receipts and lifecycle stamp bracket the node read so a
        newly finished source check cannot unlock an older node batch.
        """
        if self.access is None:
            raise ValueError("node access required")
        with self._lock:
            before = self.access.lifecycle_status()
            subscriptions = self.access.subscription_records()
            refresh = self.refresh_owner() if self.refresh_owner is not None else None
            nodes = self.access.node_records()
            times = _refresh_times(self.access)
            after_refresh = self.refresh_owner() if self.refresh_owner is not None else None
            after_subscriptions = self.access.subscription_records()
            after = self.access.lifecycle_status()
            if (subscriptions != after_subscriptions or refresh != after_refresh
                    or _lifecycle_key(before) != _lifecycle_key(after)):
                raise StaleNodeSample("node owner changed")
            projected = self.project(nodes, subscriptions, after, refresh_records=refresh,
                                     refresh_times=times)
            _note_pending(self.access, projected)
            return projected

    def project(self, node_records: Mapping[str, Any],
                subscription_records: Mapping[str, Any],
                lifecycle: Mapping[str, Any], *,
                refresh_records: Mapping[str, Any] | None = None,
                refresh_times: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Pure DTO work plus serialized gate update from already owned facts.

        Refresh owner supplies generation/stale/items with id, refreshing and
        job_generation (monotonic within a runtime, including idle/completed).
        Direct owned substitutes may carry the same facts as refresh_state.
        Incomplete refresh facts leave subscriptions=None with a reason code;
        nodes still publish with subscription health pending, without gate mutation.
        Optional subscription membership_version/check_receipt are required
        for current health; absent legacy evidence leaves nodes pending.
        """
        with self._lock:
            generation = node_records["generation"]
            if (type(generation) is not int or generation < 0
                    or subscription_records["generation"] != generation
                    or lifecycle["intent"]["runtime_generation"] != generation
                    or node_records.get("stale") or subscription_records.get("stale")
                    or lifecycle.get("freshness", {}).get("stale")
                    or self._generation is not None and generation < self._generation):
                raise StaleNodeSample("stale node facts")
            nodes = _owned(node_records["items"])
            subscriptions = _owned(subscription_records["items"])
            for record in (*nodes, *subscriptions):
                _record_id(record["id"])
            try:
                refresh = self._refresh_facts(subscriptions, generation, refresh_records)
            except IncompleteNodeSample:
                refresh = None
            # Names are preserved unless they contain sensitive material. P2-04
            # already removes known loaded/config secrets; injected raw snapshots
            # additionally supply their own secret facts to the same filter.
            clean = display_filter({"nodes": nodes, "subscriptions": subscriptions})
            safe_text = lambda value: _UUID.sub("[REDACTED]", clean(value))
            status = {"runtime_generation": generation,
                      "core_generation": lifecycle["core"]["core_generation"],
                      "core": dict(lifecycle["core"])}
            incoming_stamp = subscription_core_stamp(status)
            if (self._generation == generation and self.gate.initialized
                    and (incoming_stamp in self.gate.retired_core_stamps
                         and incoming_stamp != self.gate.core_stamp
                         or incoming_stamp[1] < self.gate.core_stamp[1])):
                raise StaleNodeSample("retired core facts")
            unlocked = set()
            if refresh is not None:
                # First observation of a runtime generation may already contain a
                # proven volatile receipt. Seed an empty baseline, then apply it.
                # A Core restart within the same runtime keeps the ordinary gate's
                # baseline rule and requires an advancing receipt.
                new_generation = self._generation != generation
                if new_generation:
                    self.gate.__init__()
                    self._generation = generation
                    self._previous.clear()
                    self._members.clear()
                    self._refresh_jobs.clear()
                evidence = self._matching_receipts(nodes, subscriptions, incoming_stamp, refresh)
                configs, receipts, keys = _gate_inputs(subscriptions, evidence)
                if new_generation:
                    initial_configs, initial_rows, _ = _gate_inputs(subscriptions, evidence, baseline=True)
                    project_subscription_rows_for_display(initial_rows, initial_configs, status, self.gate)
                if self._close_pending or lifecycle["intent"].get("stop_requested") is True:
                    self._closed_stamp = incoming_stamp
                    self._close_pending = False
                if self._closed_stamp is not None:
                    closed = self._closed_stamp
                    successor = (incoming_stamp[0] > closed[0] or
                                 incoming_stamp[0] == closed[0] and incoming_stamp[1] > closed[1])
                    if not successor:
                        self.gate.close()
                        self.gate.blocked_core_stamp = incoming_stamp
                project_subscription_rows_for_display(receipts, configs, status, self.gate)
                refreshing_sources = {(sub["tier"], sub["source_id"]) for sub in subscriptions
                                      if refresh[sub["id"]]["refreshing"]}
                unlocked = {key for key, group in keys.items()
                            if group in self.gate.unlocked_groups and key not in refreshing_sources}

            by_source: dict[tuple[str, str], list[dict[str, Any]]] = {}
            for sub in subscriptions:
                by_source.setdefault((sub["tier"], sub["source_id"]), []).append(sub)
            sub_order = {sub["id"]: index for index, sub in enumerate(subscriptions)}
            inventory: dict[tuple[str, str], list[dict[str, Any]]] = {}
            node_dtos, completed, total = [], 0, 0
            for raw in nodes:
                sources = _source_keys(raw)
                memberships = sources & by_source.keys()
                automatic = raw["tier"] == "bastion" and raw.get("pool_kind") == "automatic"
                kind = "automatic" if automatic else "subscription" if memberships else "manual"
                enabled = raw.get("enabled", True) is True and raw.get("status") != "disabled"
                if kind == "subscription":
                    enabled = enabled and any(sub["enabled"] for key in memberships for sub in by_source[key])
                state = _status(raw.get("status"))
                reason = None
                if state == "failed":
                    reason = _runtime_failure_display_category(
                        raw.get("reason_code") or raw.get("suspicious_reason") or "node_unavailable")[0]
                pending = kind == "subscription" and (
                    not memberships & unlocked
                    or raw.get("observation_origin") in {"persisted_cache", "offline_refresh"})
                if not enabled:
                    state, reason = "disabled", "disabled"
                elif pending:
                    state, reason = "pending", "subscription_recheck_pending"
                latency = _uint(raw.get("latency_ms")) if state in {"healthy", "failed"} else None
                dto = {"id": raw["id"], "name": safe_text(raw["name"]) or raw["id"],
                       "tier": raw["tier"], "kind": kind, "enabled": enabled,
                       "status": state, "reason_code": reason,
                       "protocol": safe_text(raw["protocol"]) or "unknown", "latency_ms": latency,
                       "subscription_id": _subscription_id(kind, memberships, by_source, sub_order),
                       # T-P4-C2 #4 (K-5): the runtime latency policy's own
                       # verdict (tester.py: NodeStatus.SLOW); healthy only.
                       "slow": state == "healthy" and raw.get("status") == "slow",
                       # T-P4-C2 #3 (K-2): the runtime's last check of this node.
                       "last_checked_at_ms": _uint(raw.get("checked_at_ms"))}
                node_dtos.append(dto)
                if kind == "subscription" and enabled:
                    total += 1
                    completed += int(state in {"healthy", "failed"})
                if not automatic:
                    for key in memberships:
                        inventory.setdefault(key, []).append(dto)

            node_dtos = _merge_protocol_variants(node_dtos, nodes, safe_text)
            pending_counts = {tier: 0 for tier in ("vanguard", "sentinel", "bastion", "gateway")}
            for dto in node_dtos:
                if dto["status"] == "pending" and dto["tier"] in pending_counts:
                    pending_counts[dto["tier"]] += 1
            if refresh is None:
                return {"nodes": {"items": node_dtos}, "subscriptions": None,
                        "subscriptions_unavailable_reason": "refresh_facts_incomplete",
                        "recheck_progress": recheck_progress(completed, total),
                        "generation": generation, "pending": pending_counts}

            subscription_dtos = []
            current_ids = set()
            for sub in subscriptions:
                current_ids.add(sub["id"])
                source = (sub["tier"], sub["source_id"])
                children = inventory.get(source, ())
                membership = frozenset(child["id"] for child in children)
                previous = self._previous.get(sub["id"])
                if not sub["enabled"] or previous is not None and previous[0] != membership:
                    self._previous.pop(sub["id"], None)
                refreshing = refresh[sub["id"]]["refreshing"]
                current = sub["enabled"] and source in unlocked and not refreshing
                if current and any(child["status"] == "pending" for child in children):
                    current = False
                if current:
                    visibility = "current"
                    healthy = sum(child["status"] == "healthy" for child in children)
                    self._previous[sub["id"]] = membership, healthy
                elif sub["enabled"] and sub["id"] in self._previous:
                    visibility, healthy = "previous", self._previous[sub["id"]][1]
                else:
                    visibility = "pending" if sub["enabled"] and (children or refreshing) else "unknown"
                    healthy = 0
                name = safe_text(sub["name"]) or sub["id"]
                row = {"id": sub["id"], "name": name,
                       "tier": sub["tier"], "enabled": sub["enabled"], "display": name,
                       "total": len(children), "healthy": healthy,
                       "visibility": visibility, "refreshing": refreshing}
                # T-P4-C2 #6 (K-6): progress only while members wait for the
                # recheck (the same evidence as the node rows' pending status).
                waiting = sum(child["status"] == "pending" for child in children)
                if sub["enabled"] and children and waiting:
                    row["recheck"] = {"done": len(children) - waiting, "total": len(children)}
                # Only tiers the runtime times (Sentinel, Gateway) carry the
                # field; a missing key is "unknown" (omitted), a None value is
                # "never refreshed" (null).
                if refresh_times is not None and sub["tier"] in refresh_times:
                    row["last_refresh_at_ms"] = _uint(refresh_times[sub["tier"]])
                subscription_dtos.append(row)
            self._previous = {key: value for key, value in self._previous.items() if key in current_ids}
            return {"nodes": {"items": node_dtos}, "subscriptions": {"items": subscription_dtos},
                    "recheck_progress": recheck_progress(completed, total),
                    "generation": generation, "pending": pending_counts}

    def _refresh_facts(self, subscriptions, generation, records):
        if records is None:
            rows = []
            for sub in subscriptions:
                state = sub.get("refresh_state")
                if not isinstance(state, Mapping):
                    raise IncompleteNodeSample("refresh owner state/job generation required")
                rows.append({"id": sub["id"], "refreshing": state.get("refreshing"),
                             "job_generation": state.get("job_generation")})
        else:
            if records.get("generation") != generation or records.get("stale"):
                raise StaleNodeSample("stale refresh owner facts")
            rows = _owned(records.get("items", ()))
        refresh = {}
        for row in rows:
            identifier = _record_id(row["id"])
            job = row.get("job_generation")
            if (identifier in refresh or type(row.get("refreshing")) is not bool
                    or type(job) is not int or job < 0):
                raise IncompleteNodeSample("refresh owner state/job generation required")
            if self._generation == generation and job < self._refresh_jobs.get(identifier, 0):
                raise StaleNodeSample("retired refresh job facts")
            refresh[identifier] = row
        if set(refresh) != {sub["id"] for sub in subscriptions}:
            raise IncompleteNodeSample("refresh owner must cover every subscription")
        return refresh

    def _matching_receipts(self, nodes, subscriptions, stamp, refresh):
        members = {}
        for node in nodes:
            if node["tier"] == "bastion" and node.get("pool_kind") == "automatic":
                continue
            for source in _source_keys(node):
                members.setdefault(source, set()).add(node["id"])
        evidence, current, unproven = {}, {}, set()
        for sub in subscriptions:
            source = (sub["tier"], sub["source_id"])
            membership = frozenset(members.get(source, ()))
            version = sub.get("membership_version")
            identity = (membership, version, sub["enabled"])
            old = self._members.get(sub["id"])
            receipt = sub.get("check_receipt") or {}
            checked = _uint(receipt.get("checked_at_ms"))
            high = old[3] if old is not None else None
            changed = old is not None and old[:3] != identity
            required_after = high if changed else old[4] if old is not None else None
            receipt_members = receipt.get("member_ids")
            matches = (sub.get("current_generation_evidence") is True
                and type(version) is int and version >= 0
                and type(receipt.get("membership_version")) is int
                and receipt["membership_version"] == version
                and isinstance(receipt_members, (list, tuple))
                and all(isinstance(value, str) for value in receipt_members)
                and frozenset(receipt_members) == membership
                and receipt.get("runtime_generation") == stamp[0]
                and receipt.get("core_generation") == stamp[1]
                and str(receipt.get("core_pid") or "") == stamp[2]
                and receipt.get("refresh_job_generation") == refresh[sub["id"]]["job_generation"]
                and checked is not None and (high is None or checked >= high)
                and (required_after is None or checked > required_after)
                and sub["enabled"] and not refresh[sub["id"]]["refreshing"])
            if matches:
                evidence[source] = checked
            elif sub["enabled"]:
                unproven.add(source)
            # Preserve a watermark even for unmatched late receipts, so member
            # removal/re-addition cannot reuse a prior receipt (ABA).
            current[sub["id"]] = (*identity, max(value for value in (high, checked) if value is not None)
                               if high is not None or checked is not None else None, required_after)
        self._members = current
        self._refresh_jobs = {identifier: row["job_generation"] for identifier, row in refresh.items()}
        # Duplicate configured URLs share one gate group. All enabled entries
        # must agree on proven owner facts; one job cannot unlock another job.
        return {source: checked for source, checked in evidence.items() if source not in unproven}


def _subscription_id(kind: str, memberships: set[tuple[str, str]],
                     by_source: Mapping[tuple[str, str], list[dict[str, Any]]],
                     order: Mapping[str, int]) -> str | None:
    """T-P4-C1 #3 (K-4): owning Subscription.id of a subscription node.

    The membership is the same owner fact the subscription row counts
    (``total``) and its check receipt carries as ``member_ids``. A node that
    several configured subscriptions share (same URL configured twice, or one
    node from two subscriptions) belongs to the first in configuration order;
    an enabled owner wins over a disabled one. Other kinds: None.
    """
    if kind != "subscription":
        return None
    owners = [sub for key in memberships for sub in by_source.get(key, ())]
    if not owners:
        return None
    # An owner missing from the order sorts last (never first); the id makes
    # the choice deterministic whatever the set iteration order.
    return min(owners, key=lambda sub: (not sub["enabled"], order.get(sub["id"], len(order)),
                                        sub["id"]))["id"]


def _refresh_times(access: Any) -> Mapping[str, Any] | None:
    """T-P4-C2 #6: tier-level last subscription refresh, or None when unknown.

    Port: ``bridge_runtime_access.subscription_refresh_times(access)`` ->
    ``{"generation", "stale", "tiers": {sentinel|gateway: ms|None}}``
    (runtime getter ``bridge_subscription_refresh_times``). Absent, failed or
    stale -> None and the field is omitted (frozen sidecars, fakes).
    """
    from bridge_runtime_access import subscription_refresh_times
    try:
        raw = _owned(subscription_refresh_times(access))
    except Exception:
        return None
    if (not isinstance(raw, Mapping) or raw.get("stale") is not False
            or not isinstance(raw.get("tiers"), Mapping)):
        return None
    return raw["tiers"]


def _note_pending(access: Any, projected: Mapping[str, Any]) -> None:
    """T-P4-C2 #7 (G33): hand the per-tier pending counts to tiers.summary.

    The counts are this projection's own ``pending`` node rows; the access
    layer merges them into the summary only for the same runtime generation.
    """
    from bridge_runtime_access import note_node_pending
    if isinstance(projected.get("pending"), Mapping):
        try:
            note_node_pending(access, projected["generation"], dict(projected["pending"]))
        except Exception:
            pass


def node_detail_extras(access: Any, node_id: Any) -> dict[str, Any]:
    """T-P4-C2 #3/#5 (K-1, K-3): ``endpoint`` and ``protocols`` of nodes.detail.

    The integration merges the result into the ``nodes.detail`` reply
    (bridge_wiring._nodes_detail). Port: ``bridge_runtime_access.node_endpoint``
    (runtime getter ``bridge_node_endpoint``): ``{"stale", "host", "port",
    "protocols": [{"key", "port", "node_id"}]}`` or None. The host is masked
    here (bridge_runtime_access.mask_host); no credential, URI or full
    address leaves this function. Unknown or stale -> {} (fields omitted).
    """
    from bridge_runtime_access import mask_host, node_endpoint
    if not isinstance(node_id, str) or not node_id:
        return {}
    try:
        raw = _owned(node_endpoint(access, node_id))
    except Exception:
        return {}
    if not isinstance(raw, Mapping) or raw.get("stale") is not False:
        return {}
    extras: dict[str, Any] = {}
    masked, port = mask_host(raw.get("host")), raw.get("port")
    if masked and type(port) is int and 0 < port <= 65535:
        extras["endpoint"] = {"host_masked": masked, "port": port}
    protocols = []
    for item in raw.get("protocols") or ():
        if not isinstance(item, Mapping):
            continue
        key, port = item.get("key"), item.get("port")
        if (not isinstance(key, str) or not _PROTOCOL_KEY.fullmatch(key)
                or type(port) is not int or not 0 < port <= 65535):
            continue
        entry = {"name": key, "port": port}
        if isinstance(item.get("node_id"), str) and _OPAQUE_ID.fullmatch(item["node_id"]):
            entry["node_id"] = item["node_id"]
        protocols.append(entry)
    if protocols:
        extras["protocols"] = protocols
    return extras


def _merge_protocol_variants(dtos: list[dict[str, Any]], raws: list[Mapping[str, Any]],
                             safe_text: Any) -> list[dict[str, Any]]:
    """§8u: one Vanguard row per physical node (its protocol variants are one node to the user).

    ``raws[i]`` is the record of ``dtos[i]``. Records carrying ``variant_group`` (desktop_runtime
    bridge_node_records) are merged in their group's place: the row keeps the id of the first
    variant in fallback order (nodes.detail lists every variant with its node_id; edit, delete and
    diagnose resolve that id as before), the physical name, the protocols in fallback order, and
    the state of the first usable variant (the TIER1 group fails over among them). This matches
    the runtime's tier count (manager.compact_tiers_count_status: physical groups count once).
    """
    out: list[Any] = []
    groups: dict[str, list[tuple[int, dict[str, Any], Mapping[str, Any]]]] = {}
    for dto, raw in zip(dtos, raws):
        group = raw.get("variant_group")
        if dto["tier"] != "vanguard" or dto["kind"] != "manual" or not isinstance(group, str) or not group:
            out.append(dto)
            continue
        members = groups.get(group)
        if members is None:
            members = groups[group] = []
            out.append(members)
        rank = raw.get("variant_rank")
        members.append((rank if type(rank) is int else 1 << 30, dto, raw))
    return [item if isinstance(item, dict) else _merged_row(item, safe_text) for item in out]


def _merged_row(members: list[tuple[int, dict[str, Any], Mapping[str, Any]]], safe_text: Any) -> dict[str, Any]:
    members = sorted(members, key=lambda member: member[0])
    head = members[0][1]
    live = [dto for _, dto, _ in members if dto["enabled"]]
    pick = (next((dto for dto in live if dto["status"] == "healthy"), None)
            or next((dto for dto in live if dto["status"] == "pending"), None)
            or (live[0] if live else head))
    row = dict(head)
    name = safe_text(members[0][2].get("variant_name") or "")
    if name:
        row["name"] = name
    keys = [safe_text(raw.get("variant_key") or "") or dto["protocol"] for _, dto, raw in members]
    row["protocol"] = " / ".join(dict.fromkeys(key for key in keys if key)) or head["protocol"]
    row["enabled"] = bool(live)
    for key in ("status", "reason_code", "latency_ms", "slow"):
        row[key] = pick[key]
    checked = [dto["last_checked_at_ms"] for _, dto, _ in members if dto.get("last_checked_at_ms") is not None]
    row["last_checked_at_ms"] = max(checked) if checked else None
    return row


def _lifecycle_key(sample: Mapping[str, Any]) -> tuple[Any, ...]:
    return (sample["intent"]["runtime_generation"], sample["intent"].get("lifecycle_revision"),
            sample["intent"].get("stop_requested"), sample["core"]["running"],
            sample["core"]["core_generation"], sample["core"].get("pid"),
            sample.get("freshness", {}).get("stale"))


def recheck_progress(completed: int, total: int) -> dict[str, Any]:
    """For jobs.progress, not an extension of either strict list DTO schema.

    Counts describe visible subscription-node evidence, not worker execution.
    P2-08 supplies job ownership/state separately; no job is fabricated here.
    """
    if type(completed) is not int or type(total) is not int or not 0 <= completed <= total:
        raise ValueError("invalid recheck counts")
    return {"completed": completed, "total": total,
            "message_id": "nodes.recheck.complete" if completed == total else "nodes.recheck.pending",
            "params": {"completed": completed, "total": total}}
