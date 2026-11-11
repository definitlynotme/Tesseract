"""Pure projection and file-transaction helpers for a Tier3B file provider.

The runtime formatter still owns the canonical Mihomo configuration.  This
module only projects an already-built internal YAML document into two views:
the main config and a local ``proxy-provider`` file.  Controller I/O is kept
behind callbacks so this module has no dependency on the desktop runtime or
the Mihomo API client.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import yaml


class Tier3CoreProviderError(ValueError):
    """Raised when a runtime config cannot be projected without ambiguity."""


@dataclass(frozen=True, slots=True)
class Tier3CoreProviderProjection:
    main_yaml: str
    provider_yaml: str
    provider_name: str
    provider_path: str
    proxy_names: tuple[str, ...]
    provider_member_names: tuple[str, ...]
    provider_digest: str
    member_digest: str


@dataclass(frozen=True, slots=True)
class ProviderReadbackValidation:
    valid: bool
    reason: str
    provider_name: str
    expected_names: tuple[str, ...]
    observed_names: tuple[str, ...]
    expected_member_digest: str
    observed_member_digest: str
    expected_version: str | None
    observed_version: str | None
    verification_basis: str


@dataclass(frozen=True, slots=True)
class ProviderFileTransactionResult:
    committed: bool
    changed: bool
    rolled_back: bool
    validation: ProviderReadbackValidation | None
    error: str | None
    activation_error: str | None
    rollback_error: str | None


def _yaml_mapping(runtime_yaml: str | bytes | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(runtime_yaml, Mapping):
        payload: Any = copy.deepcopy(dict(runtime_yaml))
    else:
        text = runtime_yaml.decode("utf-8") if isinstance(runtime_yaml, bytes) else runtime_yaml
        try:
            payload = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise Tier3CoreProviderError(f"invalid runtime YAML: {exc}") from exc
    if not isinstance(payload, dict):
        raise Tier3CoreProviderError("runtime YAML must contain a top-level mapping")
    return payload


def _normalized_names(names: Sequence[str], *, label: str) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_name in names:
        name = str(raw_name or "").strip()
        if not name:
            raise Tier3CoreProviderError(f"{label} contains an empty name")
        if name in seen:
            raise Tier3CoreProviderError(f"{label} contains duplicate name {name!r}")
        seen.add(name)
        normalized.append(name)
    if not normalized:
        raise Tier3CoreProviderError(f"{label} must not be empty")
    return tuple(normalized)


def _member_digest(names: Sequence[str]) -> str:
    encoded = json.dumps(
        list(names),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _semantic_yaml_digest(payload: Mapping[str, Any]) -> str:
    canonical = yaml.safe_dump(
        dict(payload),
        allow_unicode=True,
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def project_tier3b_file_provider(
    runtime_yaml: str | bytes | Mapping[str, Any],
    tier3b_proxy_names: Sequence[str],
    *,
    provider_name: str = "TIER3B-RUNTIME",
    provider_path: str = "./tier3b-runtime.yaml",
    group_name: str = "TIER3-AUTO",
    provider_health_check: bool = False,
) -> Tier3CoreProviderProjection:
    """Move selected Tier3B proxies into one ordered local file provider.

    The provider's proxy order follows the existing ``TIER3-AUTO.proxies``
    order, not the caller's set/order.  Mihomo prepends a group's direct
    ``proxies`` provider before every provider named by ``use``.  Consequently,
    direct members can only remain when they form the original order prefix;
    ``REJECT`` is emitted as the final file-provider member so it cannot become
    the first fallback candidate.
    """

    provider_name = str(provider_name or "").strip()
    provider_path = str(provider_path or "").strip()
    group_name = str(group_name or "").strip()
    if not provider_name:
        raise Tier3CoreProviderError("provider_name must not be empty")
    if not provider_path:
        raise Tier3CoreProviderError("provider_path must not be empty")
    if not group_name:
        raise Tier3CoreProviderError("group_name must not be empty")

    # An empty durable Tier3B pool is a valid fail-closed inventory.  The file
    # provider must still contain its terminal REJECT member so Mihomo cannot
    # resurrect a removed first member or force a full config reload merely to
    # represent zero usable automatic leaves.
    requested_names = (
        _normalized_names(tier3b_proxy_names, label="tier3b_proxy_names")
        if tier3b_proxy_names
        else ()
    )
    requested_set = set(requested_names)
    payload = _yaml_mapping(runtime_yaml)

    raw_proxies = payload.get("proxies")
    if not isinstance(raw_proxies, list):
        raise Tier3CoreProviderError("runtime YAML proxies must be a list")

    proxy_by_name: dict[str, dict[str, Any]] = {}
    for index, raw_proxy in enumerate(raw_proxies):
        if not isinstance(raw_proxy, dict):
            raise Tier3CoreProviderError(f"proxies[{index}] must be a mapping")
        name = str(raw_proxy.get("name") or "").strip()
        if not name:
            raise Tier3CoreProviderError(f"proxies[{index}] has no name")
        if name in proxy_by_name:
            raise Tier3CoreProviderError(f"duplicate top-level proxy name {name!r}")
        proxy_by_name[name] = raw_proxy

    missing_proxy_defs = [name for name in requested_names if name not in proxy_by_name]
    if missing_proxy_defs:
        raise Tier3CoreProviderError(
            "Tier3B proxy definitions are missing: " + ", ".join(missing_proxy_defs)
        )

    raw_groups = payload.get("proxy-groups")
    if not isinstance(raw_groups, list):
        raise Tier3CoreProviderError("runtime YAML proxy-groups must be a list")
    matching_groups = [
        group
        for group in raw_groups
        if isinstance(group, dict) and str(group.get("name") or "").strip() == group_name
    ]
    if len(matching_groups) != 1:
        raise Tier3CoreProviderError(
            f"runtime YAML must contain exactly one {group_name!r} proxy group"
        )
    target_group = matching_groups[0]
    existing_use = target_group.get("use")
    if existing_use not in (None, [], [provider_name]):
        raise Tier3CoreProviderError(
            f"{group_name}.use already points at a different provider set"
        )
    raw_members = target_group.get("proxies")
    if not isinstance(raw_members, list):
        raise Tier3CoreProviderError(f"{group_name}.proxies must be a list")

    group_members: list[str] = []
    seen_members: set[str] = set()
    for index, raw_member in enumerate(raw_members):
        member = str(raw_member or "").strip()
        if not member:
            raise Tier3CoreProviderError(f"{group_name}.proxies[{index}] is empty")
        if member in seen_members:
            raise Tier3CoreProviderError(
                f"{group_name}.proxies contains duplicate member {member!r}"
            )
        seen_members.add(member)
        group_members.append(member)

    missing_group_members = [name for name in requested_names if name not in seen_members]
    if missing_group_members:
        raise Tier3CoreProviderError(
            f"{group_name} does not route requested Tier3B proxies: "
            + ", ".join(missing_group_members)
        )

    ordinary_members = [name for name in group_members if name != "REJECT"]
    seen_provider_member = False
    direct_prefix: list[str] = []
    for name in ordinary_members:
        if name in requested_set:
            seen_provider_member = True
        elif seen_provider_member:
            raise Tier3CoreProviderError(
                f"{group_name} interleaves retained member {name!r} after Tier3B members; "
                "Mihomo cannot preserve that order across proxies and use"
            )
        else:
            direct_prefix.append(name)
    if "REJECT" in group_members and group_members[-1] != "REJECT":
        raise Tier3CoreProviderError(
            f"{group_name} must keep REJECT last before conversion"
        )

    ordered_names = tuple(name for name in ordinary_members if name in requested_set)
    provider_member_names = (*ordered_names, "REJECT")
    provider_payload = {
        "proxies": [
            *[copy.deepcopy(proxy_by_name[name]) for name in ordered_names],
            {"name": "REJECT", "type": "reject"},
        ]
    }

    payload["proxies"] = [
        proxy for proxy in raw_proxies if str(proxy.get("name") or "").strip() not in requested_set
    ]
    if direct_prefix:
        target_group["proxies"] = direct_prefix
    else:
        target_group.pop("proxies", None)
    target_group["use"] = [provider_name]
    target_group["empty-fallback"] = "REJECT"
    # Keep the provider-backed Core graph invariant across the complete
    # durable-pool range, including zero leaves.  The canonical formatter
    # normally omits TIER3-AUTO from PROXY-FALLBACK and flips both groups to
    # lazy when the pool is empty.  Leaving those inventory-derived fields in
    # the main document would turn every 0 <-> N provider update into a full
    # graph change, defeating the zero-reload contract.
    target_group["lazy"] = False

    parent_groups = [
        group
        for group in raw_groups
        if isinstance(group, dict)
        and str(group.get("name") or "").strip() == "PROXY-FALLBACK"
    ]
    if len(parent_groups) != 1:
        raise Tier3CoreProviderError(
            "runtime YAML must contain exactly one 'PROXY-FALLBACK' proxy group"
        )
    parent_group = parent_groups[0]
    raw_parent_members = parent_group.get("proxies")
    if not isinstance(raw_parent_members, list):
        raise Tier3CoreProviderError("PROXY-FALLBACK.proxies must be a list")
    parent_members: list[str] = []
    for index, raw_member in enumerate(raw_parent_members):
        member = str(raw_member or "").strip()
        if not member:
            raise Tier3CoreProviderError(
                f"PROXY-FALLBACK.proxies[{index}] is empty"
            )
        if member not in parent_members and member != "REJECT":
            parent_members.append(member)
    if group_name not in parent_members:
        parent_members.append(group_name)
    parent_group["proxies"] = parent_members
    parent_group["lazy"] = False

    raw_providers = payload.get("proxy-providers")
    if raw_providers is None:
        proxy_providers: dict[str, Any] = {}
    elif isinstance(raw_providers, dict):
        proxy_providers = raw_providers
    else:
        raise Tier3CoreProviderError("runtime YAML proxy-providers must be a mapping")

    existing_provider = proxy_providers.get(provider_name)
    if existing_provider is None:
        provider_entry: dict[str, Any] = {
            "type": "file",
            "path": provider_path,
        }
    elif not isinstance(existing_provider, dict):
        raise Tier3CoreProviderError(
            f"existing proxy-provider {provider_name!r} must be a mapping"
        )
    else:
        existing_type = str(existing_provider.get("type") or "").strip().lower()
        existing_path = str(existing_provider.get("path") or "").strip()
        if existing_type not in {"", "file"} or existing_path not in {"", provider_path}:
            raise Tier3CoreProviderError(
                f"existing proxy-provider {provider_name!r} conflicts with the local file projection"
            )
        provider_entry = copy.deepcopy(existing_provider)
        provider_entry["type"] = "file"
        provider_entry["path"] = provider_path
    group_url = str(target_group.get("url") or "").strip()
    if provider_health_check and group_url:
        # Mihomo forwards only url, expected-status, filter and interval from
        # a group to its use-providers, never lazy: without its own block the
        # provider is lazy and skips its ticks when no traffic touched it.
        # The block repeats TIER3-AUTO's own contract exactly (same url, so
        # Mihomo registers one check per member, not two), from this one
        # group so the two can never drift apart.  Part of the main graph:
        # it is invariant across inventories and loads with the next full load.
        provider_entry["health-check"] = {
            "enable": True,
            "url": group_url,
            "interval": int(target_group.get("interval") or 300),
            "timeout": int(target_group.get("timeout") or 5000),
            "lazy": False,
            "expected-status": target_group.get("expected-status", 204),
        }
    proxy_providers[provider_name] = provider_entry
    payload["proxy-providers"] = proxy_providers

    main_yaml = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
    provider_yaml = yaml.safe_dump(provider_payload, allow_unicode=True, sort_keys=False)
    return Tier3CoreProviderProjection(
        main_yaml=main_yaml,
        provider_yaml=provider_yaml,
        provider_name=provider_name,
        provider_path=provider_path,
        proxy_names=ordered_names,
        provider_member_names=provider_member_names,
        provider_digest=_semantic_yaml_digest(provider_payload),
        member_digest=_member_digest(provider_member_names),
    )


def reorder_tier3b_projection(
    projection: Tier3CoreProviderProjection,
    ordered_names: Sequence[str],
) -> Tier3CoreProviderProjection:
    """Return ``projection`` with its provider members in ``ordered_names`` order.

    Only the provider file changes: the main document never lists provider
    members, so it is identical for every order.  ``ordered_names`` must be a
    permutation of the projection's members; the terminal REJECT stays last.
    Used to put the live TIER3-AUTO pick first, so the rebuild that any byte
    change triggers (every member alive-by-default, an unfixed fallback then
    serving its first member) keeps serving that pick.
    """

    names = _normalized_names(ordered_names, label="ordered_names") if ordered_names else ()
    if sorted(names) != sorted(projection.proxy_names):
        raise Tier3CoreProviderError("ordered_names must be a permutation of the projected members")
    if tuple(names) == tuple(projection.proxy_names):
        return projection
    provider_payload = _yaml_mapping(projection.provider_yaml)
    raw_proxies = provider_payload.get("proxies")
    if not isinstance(raw_proxies, list):
        raise Tier3CoreProviderError("provider YAML proxies must be a list")
    by_name = {
        str(item.get("name") or "").strip(): item
        for item in raw_proxies
        if isinstance(item, dict)
    }
    terminal = [
        item
        for item in raw_proxies
        if isinstance(item, dict) and str(item.get("name") or "").strip() not in set(names)
    ]
    reordered = {"proxies": [*(by_name[name] for name in names), *terminal]}
    provider_member_names = (*names, *(str(item.get("name") or "").strip() for item in terminal))
    return Tier3CoreProviderProjection(
        main_yaml=projection.main_yaml,
        provider_yaml=yaml.safe_dump(reordered, allow_unicode=True, sort_keys=False),
        provider_name=projection.provider_name,
        provider_path=projection.provider_path,
        proxy_names=tuple(names),
        provider_member_names=provider_member_names,
        provider_digest=_semantic_yaml_digest(reordered),
        member_digest=_member_digest(provider_member_names),
    )


def extend_tier3b_projection(
    projection: Tier3CoreProviderProjection,
    extra_proxies: Sequence[Mapping[str, Any]],
) -> Tier3CoreProviderProjection:
    """Return ``projection`` with ``extra_proxies`` appended before REJECT.

    Used to keep a member the Manager no longer exports (a kicked pick that
    still serves) in the provider until traffic has moved off it; its
    definition comes from the Core-loaded contract, not from a new render.
    """

    additions = [copy.deepcopy(dict(item)) for item in extra_proxies if isinstance(item, Mapping)]
    names = [str(item.get("name") or "").strip() for item in additions]
    if not additions:
        return projection
    if any(not name or name in set(projection.provider_member_names) for name in names):
        raise Tier3CoreProviderError("extra provider members must have new, non-empty names")
    provider_payload = _yaml_mapping(projection.provider_yaml)
    raw_proxies = provider_payload.get("proxies")
    if not isinstance(raw_proxies, list):
        raise Tier3CoreProviderError("provider YAML proxies must be a list")
    members = [item for item in raw_proxies if str(item.get("name") or "").strip() in set(projection.proxy_names)]
    terminal = [item for item in raw_proxies if str(item.get("name") or "").strip() not in set(projection.proxy_names)]
    extended = {"proxies": [*members, *additions, *terminal]}
    proxy_names = (*projection.proxy_names, *names)
    provider_member_names = (*proxy_names, *(str(item.get("name") or "").strip() for item in terminal))
    return Tier3CoreProviderProjection(
        main_yaml=projection.main_yaml,
        provider_yaml=yaml.safe_dump(extended, allow_unicode=True, sort_keys=False),
        provider_name=projection.provider_name,
        provider_path=projection.provider_path,
        proxy_names=tuple(proxy_names),
        provider_member_names=tuple(provider_member_names),
        provider_digest=_semantic_yaml_digest(extended),
        member_digest=_member_digest(provider_member_names),
    )


def atomic_replace_if_changed(path: str | os.PathLike[str], content: str | bytes) -> bool:
    """Atomically replace ``path`` and return whether its bytes changed."""

    target = Path(path)
    payload = content.encode("utf-8") if isinstance(content, str) else bytes(content)
    try:
        if target.read_bytes() == payload:
            return False
    except FileNotFoundError:
        pass

    target.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return True


def _provider_payload(
    readback: Mapping[str, Any],
    provider_name: str,
) -> Mapping[str, Any] | None:
    providers = readback.get("providers")
    if isinstance(providers, Mapping):
        candidate = providers.get(provider_name)
        return candidate if isinstance(candidate, Mapping) else None
    return readback


def _readback_version(payload: Mapping[str, Any]) -> str | None:
    containers: list[Mapping[str, Any]] = [payload]
    for key in ("metadata", "_meta"):
        nested = payload.get(key)
        if isinstance(nested, Mapping):
            containers.append(nested)
    for container in containers:
        for key in ("version", "digest", "content_digest", "contentDigest"):
            value = str(container.get(key) or "").strip()
            if value:
                return value
    return None


def validate_proxy_provider_readback(
    readback: Mapping[str, Any],
    *,
    provider_name: str,
    expected_proxy_names: Sequence[str],
    expected_version: str | None = None,
) -> ProviderReadbackValidation:
    """Validate controller order and, when exposed, the provider version.

    Mihomo's provider representation normally exposes ordered proxy objects but
    no raw-file digest.  Exact ordered membership is therefore the equivalent
    digest fallback when a controller/provider version is unavailable.
    """

    expected_names = _normalized_names(expected_proxy_names, label="expected_proxy_names")
    expected_member_digest = _member_digest(expected_names)
    normalized_expected_version = str(expected_version or "").strip() or None

    if not isinstance(readback, Mapping):
        return ProviderReadbackValidation(
            valid=False,
            reason="provider readback is not a mapping",
            provider_name=provider_name,
            expected_names=expected_names,
            observed_names=(),
            expected_member_digest=expected_member_digest,
            observed_member_digest=_member_digest(()),
            expected_version=normalized_expected_version,
            observed_version=None,
            verification_basis="",
        )

    payload = _provider_payload(readback, provider_name)
    if payload is None:
        return ProviderReadbackValidation(
            valid=False,
            reason=f"provider {provider_name!r} is absent from controller readback",
            provider_name=provider_name,
            expected_names=expected_names,
            observed_names=(),
            expected_member_digest=expected_member_digest,
            observed_member_digest=_member_digest(()),
            expected_version=normalized_expected_version,
            observed_version=None,
            verification_basis="",
        )

    observed_provider_name = str(payload.get("name") or "").strip()
    if observed_provider_name and observed_provider_name != provider_name:
        reason = (
            f"controller returned provider {observed_provider_name!r}, expected {provider_name!r}"
        )
        return ProviderReadbackValidation(
            valid=False,
            reason=reason,
            provider_name=provider_name,
            expected_names=expected_names,
            observed_names=(),
            expected_member_digest=expected_member_digest,
            observed_member_digest=_member_digest(()),
            expected_version=normalized_expected_version,
            observed_version=_readback_version(payload),
            verification_basis="",
        )

    raw_proxies = payload.get("proxies")
    observed_names_list: list[str] = []
    malformed = not isinstance(raw_proxies, list)
    if isinstance(raw_proxies, list):
        for raw_proxy in raw_proxies:
            if isinstance(raw_proxy, Mapping):
                name = str(raw_proxy.get("name") or "").strip()
            else:
                name = str(raw_proxy or "").strip()
            if not name:
                malformed = True
                break
            observed_names_list.append(name)
    observed_names = tuple(observed_names_list)
    observed_member_digest = _member_digest(observed_names)
    observed_version = _readback_version(payload)

    if malformed:
        valid = False
        reason = "provider readback has malformed proxies"
        basis = ""
    elif observed_names != expected_names:
        valid = False
        reason = "provider member order/content does not match the projected order"
        basis = "member_digest"
    elif (
        normalized_expected_version is not None
        and observed_version is not None
        and observed_version != normalized_expected_version
    ):
        valid = False
        reason = "provider version/digest does not match the projected version"
        basis = "version"
    else:
        valid = True
        reason = ""
        basis = "version" if observed_version is not None and normalized_expected_version else "member_digest"

    return ProviderReadbackValidation(
        valid=valid,
        reason=reason,
        provider_name=provider_name,
        expected_names=expected_names,
        observed_names=observed_names,
        expected_member_digest=expected_member_digest,
        observed_member_digest=observed_member_digest,
        expected_version=normalized_expected_version,
        observed_version=observed_version,
        verification_basis=basis,
    )


@dataclass(frozen=True, slots=True)
class VerifiedProviderWriteResult:
    """Outcome of ``write_provider_file_verified``.

    Exactly one of ``applied``, ``rejected`` and ``unknown`` is true.
    """

    applied: bool
    rejected: bool
    unknown: bool
    changed: bool
    write_started_wall: float
    validation: ProviderReadbackValidation | None
    error: str | None
    activation_error: str | None


def _replace_with_retry(target: Path, content: str | bytes, *, sleep: Callable[[float], None]) -> bool:
    # Windows refuses os.replace while Mihomo's watcher holds the file open
    # for its 100 ms re-read; a short retry is enough (the main config uses
    # the same pattern).
    for attempt in range(4):
        try:
            return atomic_replace_if_changed(target, content)
        except PermissionError:
            if attempt == 3:
                raise
            sleep(0.05 * (attempt + 1))
    return False


def write_provider_file_verified(
    path: str | os.PathLike[str],
    content: str | bytes,
    *,
    provider_name: str,
    expected_proxy_names: Sequence[str],
    previous_proxy_names: Sequence[str],
    activate: Callable[[], None],
    readback: Callable[[], Mapping[str, Any]],
    is_rejection: Callable[[BaseException], bool],
    settle_seconds: float = 0.3,
    put_attempts: int = 3,
    read_attempts: int = 3,
    read_interval: float = 0.5,
    sleep: Callable[[float], None] | None = None,
) -> VerifiedProviderWriteResult:
    """Write a provider file whose write is the commit, then prove the load.

    Mihomo's file watcher applies the file 0.1-0.27 s after it is written,
    with or without a controller PUT; the PUT only makes the load immediate.

    * A PUT the controller refuses as unparsable (``is_rejection``) leaves
      the previous members loaded.  Only then are the previous bytes
      restored; Mihomo treats them as a no-op (same hash as its last good
      load), so nothing is rebuilt.  Result ``rejected``.
    * A PUT that failed in transport is retried (identical bytes are a no-op
      in Mihomo) and the readback decides, after the watcher had time to
      act.  The file is never restored after a transport failure: the watcher
      may already have loaded it, and a restore would be a second rebuild.
    * Readback failures are retried; ``unknown`` means no read proved either
      membership.
    """

    pause = sleep if sleep is not None else time.sleep
    target = Path(path)
    expected = tuple(str(name) for name in expected_proxy_names)
    previous = tuple(str(name) for name in previous_proxy_names)
    try:
        previous_content = target.read_bytes() if target.exists() else None
    except Exception as exc:
        return VerifiedProviderWriteResult(
            False, False, True, False, time.time(), None,
            f"could not preserve previous provider file: {exc}", None,
        )
    write_started_wall = time.time()
    try:
        changed = _replace_with_retry(target, content, sleep=pause)
    except Exception as exc:
        return VerifiedProviderWriteResult(
            False, False, True, False, write_started_wall, None,
            f"provider file write failed: {exc}", None,
        )

    activation_error: str | None = None
    rejected_by_controller = False
    put_succeeded = False
    for attempt in range(max(1, int(put_attempts))):
        try:
            activate()
            put_succeeded = True
            break
        except Exception as exc:
            activation_error = str(exc)
            if is_rejection(exc):
                rejected_by_controller = True
                break
            if attempt + 1 < put_attempts:
                pause(0.2)
    if not put_succeeded and not rejected_by_controller:
        # Give the file watcher its window before trusting a readback.
        remaining = write_started_wall + float(settle_seconds) - time.time()
        if remaining > 0:
            pause(remaining)

    validation: ProviderReadbackValidation | None = None
    read_error: str | None = None
    for attempt in range(max(1, int(read_attempts))):
        try:
            validation = validate_proxy_provider_readback(
                readback(),
                provider_name=provider_name,
                expected_proxy_names=expected,
            )
            read_error = None
        except Exception as exc:
            validation = None
            read_error = str(exc)
        if validation is not None and (
            validation.valid
            or rejected_by_controller
            or tuple(validation.observed_names) != previous
            or put_succeeded
        ):
            break
        if attempt + 1 < read_attempts:
            pause(float(read_interval))

    if validation is not None and validation.valid:
        return VerifiedProviderWriteResult(
            True, False, False, changed, write_started_wall, validation, None, activation_error,
        )
    if (
        rejected_by_controller
        and validation is not None
        and tuple(validation.observed_names) == previous
    ):
        restore_error: str | None = None
        if changed:
            try:
                if previous_content is None:
                    target.unlink(missing_ok=True)
                else:
                    _replace_with_retry(target, previous_content, sleep=pause)
            except Exception as exc:
                restore_error = f"file restore failed: {exc}"
        return VerifiedProviderWriteResult(
            False, True, False, changed, write_started_wall, validation,
            "; ".join(
                part for part in (
                    f"controller rejected the provider file: {activation_error}",
                    restore_error,
                ) if part
            ),
            activation_error,
        )
    return VerifiedProviderWriteResult(
        False, False, True, changed, write_started_wall, validation,
        (
            f"provider readback failed: {read_error}"
            if validation is None
            else f"provider readback did not prove the write: {validation.reason}"
        ),
        activation_error,
    )


def replace_provider_file_transactionally(
    path: str | os.PathLike[str],
    content: str | bytes,
    *,
    provider_name: str,
    expected_proxy_names: Sequence[str],
    activate: Callable[[], None],
    readback: Callable[[], Mapping[str, Any]],
    expected_version: str | None = None,
    rollback_activate: Callable[[], None] | None = None,
) -> ProviderFileTransactionResult:
    """Replace, activate and verify a provider file, restoring it on failure.

    ``activate`` normally performs the controller provider-update request and
    ``readback`` fetches the provider afterward.  Neither callback is retried.
    If either callback or validation fails, the exact previous bytes (or prior
    absence) are restored before the optional rollback activation callback.
    """

    target = Path(path)
    previous_exists = target.exists()
    try:
        previous_content = target.read_bytes() if previous_exists else None
    except Exception as exc:
        return ProviderFileTransactionResult(
            committed=False,
            changed=False,
            rolled_back=False,
            validation=None,
            error=f"could not preserve previous provider file: {exc}",
            activation_error=None,
            rollback_error=None,
        )

    changed = False
    validation: ProviderReadbackValidation | None = None
    activation_error: str | None = None
    try:
        changed = atomic_replace_if_changed(target, content)
        try:
            activate()
        except Exception as exc:
            # Provider update may have committed before its HTTP response was
            # interrupted.  Read back exactly once before deciding to restore.
            activation_error = str(exc)
        validation = validate_proxy_provider_readback(
            readback(),
            provider_name=provider_name,
            expected_proxy_names=expected_proxy_names,
            expected_version=expected_version,
        )
        if not validation.valid:
            raise Tier3CoreProviderError(validation.reason)
    except Exception as exc:
        rolled_back = False
        rollback_errors: list[str] = []
        if changed:
            try:
                if previous_content is None:
                    target.unlink(missing_ok=True)
                else:
                    atomic_replace_if_changed(target, previous_content)
                rolled_back = True
            except Exception as rollback_exc:
                rollback_errors.append(f"file restore failed: {rollback_exc}")
            if rollback_activate is not None:
                try:
                    rollback_activate()
                except Exception as rollback_exc:
                    rollback_errors.append(f"rollback activation failed: {rollback_exc}")
        return ProviderFileTransactionResult(
            committed=False,
            changed=changed,
            rolled_back=rolled_back,
            validation=validation,
            error=(
                f"activation failed: {activation_error}; verification failed: {exc}"
                if activation_error
                else str(exc)
            ),
            activation_error=activation_error,
            rollback_error="; ".join(rollback_errors) or None,
        )

    return ProviderFileTransactionResult(
        committed=True,
        changed=changed,
        rolled_back=False,
        validation=validation,
        error=None,
        activation_error=activation_error,
        rollback_error=None,
    )
