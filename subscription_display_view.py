from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

from node_status_view import (
    config_item_enabled,
    row_matches_subscription_config,
    subscription_config_url,
    subscription_group_key,
    subscription_pending_recheck_row,
)
from tier1_rows import node_row_meta


SUBSCRIPTION_DISPLAY_TIERS = frozenset({"tier2", "exit"})


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _core_status(status: dict[str, Any] | None) -> dict[str, Any]:
    payload = status if isinstance(status, dict) else {}
    core = payload.get("core")
    return core if isinstance(core, dict) else {}


def subscription_core_stamp(
    status: dict[str, Any] | None,
) -> tuple[int | None, int | None, str]:
    """Return the UI-visible Core instance identity without changing Runtime.

    ``core_generation`` is the primary lifecycle marker.  PID is included as a
    conservative UI-only fallback for an internal Core restart that does not
    advance that counter.
    """

    payload = status if isinstance(status, dict) else {}
    core = _core_status(payload)
    return (
        _optional_int(payload.get("runtime_generation")),
        _optional_int(payload.get("core_generation")),
        str(core.get("pid") or "").strip(),
    )


def subscription_core_running(status: dict[str, Any] | None) -> bool:
    payload = status if isinstance(status, dict) else {}
    core = _core_status(payload)
    if "core_running" in payload:
        return bool(payload.get("core_running"))
    return bool(core.get("running"))


def _core_generation_key(
    stamp: tuple[int | None, int | None, str],
) -> tuple[int, int] | None:
    runtime_generation, core_generation, _pid = stamp
    if runtime_generation is None or core_generation is None:
        return None
    return runtime_generation, core_generation


def _observed_watermark(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = f"{text[:-1]}+00:00" if text.endswith("Z") else text
    try:
        return datetime.fromisoformat(normalized).timestamp()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _row_identity(row: list[Any], index: int) -> str:
    meta = node_row_meta(row)
    identity = str(meta.get("identity") or "").strip()
    if identity:
        return identity
    parts = [
        str(row[column] if len(row) > column else "").strip()
        for column in (0, 1, 2, 3)
    ]
    rendered = "|".join(parts)
    return rendered if rendered.strip("|") else f"row:{index}"


def _normalized_origin(row: list[Any]) -> str:
    origin = str(node_row_meta(row).get("observation_origin") or "").strip().lower()
    return "persisted_cache" if origin == "offline_refresh" else origin


def _subscription_items(
    config_items: Iterable[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    return [
        item
        for item in config_items or ()
        if isinstance(item, dict)
        and str(item.get("tier") or "").strip().lower()
        in SUBSCRIPTION_DISPLAY_TIERS
        and str(item.get("kind") or "").strip().lower() == "subscription"
        and config_item_enabled(item)
        and subscription_config_url(item)
    ]


@dataclass
class SubscriptionDisplayGate:
    """Window-local ownership of formal subscription health visibility."""

    initialized: bool = False
    core_running: bool = False
    core_stamp: tuple[int | None, int | None, str] = (None, None, "")
    blocked_core_stamp: tuple[int | None, int | None, str] | None = None
    retired_core_stamps: set[tuple[int | None, int | None, str]] = field(
        default_factory=set
    )
    baseline_by_group: dict[str, dict[str, float | None]] = field(
        default_factory=dict
    )
    last_watermarks_by_group: dict[str, dict[str, float | None]] = field(
        default_factory=dict
    )
    unlocked_groups: set[str] = field(default_factory=set)
    accepted_watermarks: dict[str, float] = field(default_factory=dict)

    def close(self) -> None:
        """Hide formal health immediately while retaining raw membership."""

        if self.core_running:
            self.baseline_by_group = copy.deepcopy(
                self.last_watermarks_by_group
            )
        if self.initialized:
            # A lifecycle action closes visibility synchronously, before the
            # worker can stop or replace Core.  Keep the currently observed
            # instance locked so a late refresh from that same Core cannot
            # reopen health while Stop/Restart is still in flight.
            self.blocked_core_stamp = self.core_stamp
        self.core_running = False
        self.unlocked_groups.clear()
        self.accepted_watermarks.clear()


def _row_batch_matches_core(
    current_stamp: tuple[int | None, int | None, str],
    batch_stamp: tuple[int | None, int | None, str] | None,
) -> bool:
    runtime_generation, core_generation, _pid = current_stamp
    if runtime_generation is None or core_generation is None:
        return False
    if batch_stamp is None:
        return True
    return batch_stamp == current_stamp


def project_subscription_rows_for_display(
    rows: Iterable[list[Any]] | None,
    config_items: Iterable[dict[str, Any]] | None,
    status: dict[str, Any] | None,
    gate: SubscriptionDisplayGate,
    *,
    row_batch_stamp: tuple[int | None, int | None, str] | None = None,
) -> list[list[Any]]:
    """Return UI-only rows with stale Tier2/Exit health evidence removed.

    The input rows and their metadata are never mutated.  Tier3 and ordinary
    manually configured nodes pass through unchanged apart from the defensive
    deep copy used for the display cache.
    """

    raw_rows = [row for row in rows or () if isinstance(row, list)]
    items = _subscription_items(config_items)
    group_rows: dict[str, list[int]] = {}
    row_groups: dict[int, list[str]] = {}
    watermarks: dict[str, dict[str, float | None]] = {}

    for item in items:
        key = subscription_group_key(item)
        matches = [
            index
            for index, row in enumerate(raw_rows)
            if row_matches_subscription_config(row, item)
        ]
        group_rows[key] = matches
        group_watermarks: dict[str, float | None] = {}
        for index in matches:
            row = raw_rows[index]
            row_groups.setdefault(index, []).append(key)
            group_watermarks[_row_identity(row, index)] = _observed_watermark(
                node_row_meta(row).get("observed_at")
            )
        watermarks[key] = group_watermarks

    running = subscription_core_running(status)
    stamp = subscription_core_stamp(status)
    current_generation = _core_generation_key(gate.core_stamp)
    incoming_generation = _core_generation_key(stamp)
    stale_instance = bool(
        gate.initialized
        and (
            (
                incoming_generation is not None
                and current_generation is not None
                and incoming_generation < current_generation
            )
            or (
                running
                and stamp in gate.retired_core_stamps
                and stamp != gate.core_stamp
            )
        )
    )
    if (
        not stale_instance
        and running
        and gate.blocked_core_stamp is not None
        and stamp != gate.blocked_core_stamp
    ):
        gate.retired_core_stamps.add(gate.blocked_core_stamp)
        gate.blocked_core_stamp = None
    blocked_same_instance = bool(
        not stale_instance
        and running
        and gate.blocked_core_stamp is not None
        and stamp == gate.blocked_core_stamp
    )
    current_groups = set(group_rows)
    first_observation = not gate.initialized
    instance_changed = bool(
        not stale_instance
        and gate.initialized
        and running
        and not blocked_same_instance
        and (
            not gate.core_running
            or stamp != gate.core_stamp
        )
    )
    stopped_now = bool(gate.initialized and gate.core_running and not running)

    if not stale_instance:
        if first_observation:
            gate.initialized = True
            gate.baseline_by_group = copy.deepcopy(watermarks)
            gate.unlocked_groups.clear()
            gate.accepted_watermarks.clear()
        elif instance_changed:
            gate.retired_core_stamps.add(gate.core_stamp)
            # Rows do not carry a Core generation.  Treat the first batch for
            # a new stamp as its baseline so an old Core observation captured
            # across the status/rows boundary cannot unlock on repetition.
            gate.baseline_by_group = copy.deepcopy(watermarks)
            gate.unlocked_groups.clear()
            gate.accepted_watermarks.clear()
        elif stopped_now:
            gate.retired_core_stamps.add(gate.core_stamp)
            gate.baseline_by_group = copy.deepcopy(
                gate.last_watermarks_by_group
            )
            gate.unlocked_groups.clear()
            gate.accepted_watermarks.clear()

        gate.core_running = running
        gate.core_stamp = stamp
        gate.unlocked_groups.intersection_update(current_groups)
        gate.accepted_watermarks = {
            key: value
            for key, value in gate.accepted_watermarks.items()
            if key in current_groups
        }
        gate.baseline_by_group = {
            key: value
            for key, value in gate.baseline_by_group.items()
            if key in current_groups
        }

    batch_matches = bool(
        running
        and not stale_instance
        and not blocked_same_instance
        and _row_batch_matches_core(stamp, row_batch_stamp)
    )
    if not stale_instance and not running:
        # Cache refreshes while stopped can update inventory and timestamps.
        # Freeze the newest stopped-state watermarks for the next Core instance.
        gate.baseline_by_group = copy.deepcopy(watermarks)
    elif batch_matches and not first_observation and not instance_changed:
        for key, indexes in group_rows.items():
            if not indexes:
                gate.unlocked_groups.discard(key)
                gate.accepted_watermarks.pop(key, None)
                continue
            all_live = all(
                _normalized_origin(raw_rows[index]) == "live"
                for index in indexes
            )
            group_watermarks = watermarks.get(key, {})
            timestamps = tuple(group_watermarks.values())
            complete_watermark = bool(timestamps) and all(
                value is not None for value in timestamps
            )
            current_high = max(
                (value for value in timestamps if value is not None),
                default=None,
            )
            if key in gate.unlocked_groups:
                accepted = gate.accepted_watermarks.get(key)
                if (
                    not all_live
                    or not complete_watermark
                    or current_high is None
                    or (accepted is not None and current_high < accepted)
                ):
                    gate.unlocked_groups.discard(key)
                    gate.accepted_watermarks.pop(key, None)
                else:
                    gate.accepted_watermarks[key] = current_high
                continue

            # A shared row carries only identity-level evidence in the current
            # Runtime contract.  Require at least one URL-exclusive row to
            # prove which subscription completed the formal check.
            exclusive_indexes = [
                index for index in indexes if len(row_groups.get(index, ())) == 1
            ]
            baseline = gate.baseline_by_group.get(key, {})
            advanced = any(
                watermarks[key].get(_row_identity(raw_rows[index], index))
                is not None
                and (
                    baseline.get(_row_identity(raw_rows[index], index)) is None
                    or watermarks[key][_row_identity(raw_rows[index], index)]
                    > baseline[_row_identity(raw_rows[index], index)]
                )
                for index in exclusive_indexes
            )
            if all_live and complete_watermark and advanced and current_high is not None:
                gate.unlocked_groups.add(key)
                gate.accepted_watermarks[key] = current_high

    projected: list[list[Any]] = []
    for index, row in enumerate(raw_rows):
        memberships = tuple(row_groups.get(index, ()))
        if not memberships:
            projected.append(copy.deepcopy(row))
            continue
        unlocked = tuple(
            key for key in memberships if key in gate.unlocked_groups
        )
        visible = copy.deepcopy(row)
        while len(visible) < 11:
            visible.append("")
        meta = dict(node_row_meta(visible))
        meta["subscription_display_group_keys"] = memberships
        meta["subscription_display_unlocked_group_keys"] = unlocked
        visible[10] = meta
        if not unlocked or not batch_matches:
            visible = subscription_pending_recheck_row(visible)
        projected.append(visible)

    if not stale_instance:
        gate.last_watermarks_by_group = copy.deepcopy(watermarks)
    return projected
