"""Tier-ladder T0 comparison rules (spec_tier_ladder_v1 §7.1).

Shared by ``.build/claude-fallback/ladder/t0_compare.py`` and any pytest that
checks the key-off identity.  Pure: no product import, no I/O beyond the two
directories it is given.

The rule is byte equality of every family file the driver writes, after
exactly these documented exemptions (all others are failures):

1. Variant ``overshoot_only`` only: a log bucket may carry the stream
   ``observer:config_warning`` with exactly one line, equal to
   ``CONFIG_WARNING_LINE`` (spec §2.1, §7.1 critic M7).  It is removed before
   the comparison and counted.
2. Every variant: the ``_check_exit_quality_impl`` call site of
   ``_runtime_alternative_switch_action`` may gain the keyword
   ``allow_pf_subtree_select=True`` (spec §2.3).  It is removed before the
   comparison and counted.  No other call site may change.
3. Every variant, family ``policy`` only: a ``ladder_properties`` value
   (``ladder_on``/``overshoot_on``) that reads exactly ``False`` equals the
   golden's ``None``.  The golden was captured before the properties existed
   (the driver's ``getattr`` default was None, while its intent is "absent
   reads False"); a property reading True, or any other value, still fails.
4. Every variant, family ``policy`` only: a policy entry's ``constants``
   may carry the key ``not_reproduced_backoff_s`` (10-28 ship2: the leaf
   back-off after a NOT_REPRODUCED verdict, a non-ladder product change) with
   exactly the value in ``ADDED_CONSTANTS`` when the reference's matching
   ``constants`` lacks that key.  It is removed before the comparison and
   counted.  Any other value, any other new key, or a reference that already
   has the key (the differential base) compares as usual.  No other family
   changed: the driver never re-judges a leaf after NOT_REPRODUCED.
   10-28 ship2 fixes 1-3 add five more keys under the same rule (the fix 3
   override thresholds and the fix 1 re-export constants); fixes 1 and 2
   act only with judge act + tier_ladder_judge, never in a T0 variant, and
   the driver has no FAST re-trigger inside a NOT_REPRODUCED back-off.
5. Every variant, family ``policy`` only: a policy entry's ``fields`` may
   carry the fields in ``ADDED_POLICY_FIELDS`` (10-28 ship2: the three fix keys
   ``exhausted_pool_reexport`` / ``exhausted_hold`` /
   ``not_reproduced_override``) with exactly the value ``False`` when the
   reference's matching entry lacks the field.  Removed and counted with
   exemption 4; ``True``, or a reference that has the field, compares as
   usual.  The keys default False and no T0 variant sets them.

Absence check (spec §7.1): no log line of any variant may contain the ladder
texts in ``ABSENT_TEXTS`` (after exemption 1 for ``overshoot_only``).
"""

from __future__ import annotations

import copy
import difflib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

VARIANTS = ("absent", "false", "overshoot_only")
CONFIG_WARNING_STREAM = "observer:config_warning"
CONFIG_WARNING_LINE = (
    "INFO Post-dial observer config_warning (key=overshoot_repair reason=requires_tier_ladder_judge)"
)
ALLOWED_CALL_SITE_KEYWORD = ("_check_exit_quality_impl", "allow_pf_subtree_select", "True")
ADDED_CONSTANTS = {
    "not_reproduced_backoff_s": 420.0,
    # 10-28 ship2 fixes 1-3 (exemption 4, same rule).
    "not_reproduced_override_min_share": 0.8,
    "not_reproduced_override_min_n": 4,
    "pool_reexport_interval_s": 120.0,
    "pool_reexport_max_alive": 1,
    "pool_reexport_min_surplus": 4,
}
ADDED_POLICY_FIELDS = ("exhausted_pool_reexport", "exhausted_hold", "not_reproduced_override")
ABSENT_TEXTS = (
    "ladder=",
    "ladder_",
    "rung",
    "rverdict",
    "rearm_source",
    "Runtime ladder view",
    "demotion_deferred",
    "config_warning",
    "witness",
    "overshoot",
    "gateway_scope_off",
    "route_outside_ladder",
    "no_rung",
    "d4_txn_busy",
    "post_dial_ladder",
)
_LOG_PREFIXES = ("DEBUG ", "INFO ", "WARNING ", "ERROR ", "CRITICAL ")


@dataclass
class FamilyResult:
    family: str
    variant: str
    equal: bool
    diff: list[str] = field(default_factory=list)
    config_warnings: int = 0
    config_warning_errors: list[str] = field(default_factory=list)
    call_site_exemptions: int = 0
    absent_property_exemptions: int = 0
    added_constant_exemptions: int = 0
    absent_hits: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.equal and not self.config_warning_errors and not self.absent_hits and self.error is None


def dump(value: Any) -> bytes:
    text = json.dumps(value, sort_keys=True, indent=1, ensure_ascii=False, default=repr)
    return (text + "\n").encode("utf-8")


def _walk_buckets(value: Any, path: str, visit: Any) -> None:
    if isinstance(value, dict):
        if any(isinstance(k, str) and (k.startswith("observer:") or k.startswith("log:")) for k in value):
            visit(value, path)
        for key, item in value.items():
            _walk_buckets(item, f"{path}/{key}", visit)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _walk_buckets(item, f"{path}[{index}]", visit)


def apply_exemptions(value: Any, family: str, variant: str, result: FamilyResult) -> Any:
    """A copy of ``value`` with the documented exemptions removed."""

    value = copy.deepcopy(value)

    def strip_warning(bucket: dict[str, Any], path: str) -> None:
        lines = bucket.get(CONFIG_WARNING_STREAM)
        if lines is None:
            return
        if variant != "overshoot_only":
            return  # stays: a diff (and an absence hit) in the other variants
        if lines == [CONFIG_WARNING_LINE]:
            result.config_warnings += 1
            del bucket[CONFIG_WARNING_STREAM]
        else:
            result.config_warning_errors.append(f"{path}: {lines!r}")

    _walk_buckets(value, family, strip_warning)
    if family == "switch" and isinstance(value, dict):
        caller, keyword, literal = ALLOWED_CALL_SITE_KEYWORD
        for site in value.get("call_sites") or []:
            if (
                isinstance(site, dict)
                and str(site.get("caller", "")).split(".")[-1] == caller
                and isinstance(site.get("keywords"), dict)
                and site["keywords"].get(keyword) == literal
            ):
                del site["keywords"][keyword]
                result.call_site_exemptions += 1
    if family == "policy" and isinstance(value, dict):
        for entry in value.values():
            props = entry.get("ladder_properties") if isinstance(entry, dict) else None
            if isinstance(props, dict):
                for key in list(props):
                    if props[key] is False:
                        props[key] = None
                        result.absent_property_exemptions += 1
    return value


def strip_added_constants(reference: Any, value: Any, family: str, result: FamilyResult) -> Any:
    """Exemption 4: a copy of ``value`` without the ``ADDED_CONSTANTS`` keys
    the reference's matching ``constants`` lacks (exact value only)."""

    if family != "policy" or not isinstance(value, dict) or not isinstance(reference, dict):
        return value
    value = copy.deepcopy(value)
    for name, entry in value.items():
        ref_entry = reference.get(name)
        fields = entry.get("fields") if isinstance(entry, dict) else None
        ref_fields = ref_entry.get("fields") if isinstance(ref_entry, dict) else None
        if isinstance(fields, dict) and isinstance(ref_fields, dict):
            for key in ADDED_POLICY_FIELDS:
                if key in fields and key not in ref_fields and fields[key] is False:
                    del fields[key]
                    result.added_constant_exemptions += 1
        constants = entry.get("constants") if isinstance(entry, dict) else None
        ref_constants = ref_entry.get("constants") if isinstance(ref_entry, dict) else None
        if not isinstance(constants, dict) or not isinstance(ref_constants, dict):
            continue
        for key, expected in ADDED_CONSTANTS.items():
            if key in constants and key not in ref_constants and constants[key] == expected:
                del constants[key]
                result.added_constant_exemptions += 1
    return value


def absent_hits(value: Any, path: str = "") -> list[str]:
    hits: list[str] = []
    if isinstance(value, str):
        if value.startswith(_LOG_PREFIXES):
            for text in ABSENT_TEXTS:
                if text in value:
                    hits.append(f"{path}: {text!r} in {value[:160]!r}")
    elif isinstance(value, dict):
        for key, item in value.items():
            hits.extend(absent_hits(item, f"{path}/{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            hits.extend(absent_hits(item, f"{path}[{index}]"))
    return hits


def compare_family(golden_file: Path, current_file: Path, family: str, variant: str,
                   *, max_diff: int = 80) -> FamilyResult:
    result = FamilyResult(family=family, variant=variant, equal=False)
    try:
        golden = json.loads(golden_file.read_bytes().decode("utf-8"))
        current = json.loads(current_file.read_bytes().decode("utf-8"))
    except Exception as exc:  # missing or unreadable file
        result.error = f"{type(exc).__name__}: {exc}"
        return result
    stripped = strip_added_constants(golden, apply_exemptions(current, family, variant, result), family, result)
    result.absent_hits = absent_hits(stripped, family)
    left, right = dump(golden), dump(stripped)
    result.equal = left == right
    if not result.equal:
        diff = difflib.unified_diff(
            left.decode("utf-8").splitlines(),
            right.decode("utf-8").splitlines(),
            fromfile=f"golden/{family}.json",
            tofile=f"{variant}/{family}.json",
            lineterm="",
            n=2,
        )
        result.diff = [line[:400] for _, line in zip(range(max_diff), diff)]
    return result


def compare_dirs(golden_dir: Path, current_dir: Path, variant: str, families: list[str]) -> list[FamilyResult]:
    return [
        compare_family(golden_dir / f"{name}.json", current_dir / f"{name}.json", name, variant)
        for name in families
    ]
