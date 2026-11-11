from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, NoReturn
from urllib.parse import urlparse

from config_item_identity import imported_node_uri, source_entry_value
from config_utils import ensure_list_section, ensure_mapping_section
from config_store import ConfigStore
from config_validation import validate_url
from import_node_rules import (
    existing_tier1_duplicate_keys,
    import_warnings_for_tier,
    multiprotocol_import_warnings,
    node_duplicate_key,
    node_to_tier1_config,
    summarize_import_node,
    summarize_tier1_import_entry,
    tier1_import_entries_from_nodes,
    tier1_protocol_key_from_node,
    unique_import_name,
)
from models import Node
from parser import ContentAnalysisReport, analyze_content_blob
from tier1_protocols import expand_tier1_entry, parse_fallback_order_text, parse_tier1_multiprotocol_block
# Source batch (T-P2-13) additions; existing imports above are unchanged.
import copy
import re
from config_utils import config_entry_enabled
from config_validation import validate_sources_config


TIER3B_SOURCE_SECTIONS = frozenset(
    {"github_repos", "telegram_channels", "subscription_urls"}
)
# Every list section of the sources document a batch may edit. local_files is
# not a Tier3B add/upsert target for the Qt editor, but the bridge projects it
# as ConfigValues.sources, so the batch accepts it with a path-only check.
SOURCE_BATCH_SECTIONS = (
    "github_repos", "telegram_channels", "subscription_urls", "local_files",
)
SOURCE_BATCH_MAX_OPS = 4000
_SOURCE_BATCH_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class SourceBatchError(ValueError):
    """A source batch was held: nothing was written.

    ``errors`` holds one safe record per failed operation:
    ``{"index": int | None, "op": str | None, "reason": str}``. Reasons are a
    closed set (``invalid_op``, ``invalid_value``, ``invalid_entry``,
    ``not_found``, ``duplicate``, ``conflict``, ``document_invalid``); no
    source value or exception text is ever included, because source values
    are secrets. ``document_invalid`` (index ``None``) means the resulting
    whole document fails ``validate_sources_config`` -- usually legacy
    content elsewhere in the file -- so ``write_sources`` would refuse it.
    """

    def __init__(self, errors: list[dict[str, Any]]) -> None:
        super().__init__("source batch rejected")
        self.errors = errors

    @property
    def conflict(self) -> bool:
        return any(error.get("reason") == "conflict" for error in self.errors)


class _SourceBatchOpError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def sources_document_digest(payload: Any) -> str:
    """Content digest used as the batch compare-and-swap token.

    Mapping keys are normalized to tagged strings first, so YAML documents
    with mixed key types (``{1: ..., "b": ...}``) still sort and hash.
    """

    encoded = json.dumps(
        _source_digest_keys(payload),
        sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _source_digest_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            (key if isinstance(key, str) else f"\x00{type(key).__name__}:{key!r}"):
                _source_digest_keys(child)
            for key, child in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_source_digest_keys(child) for child in value]
    return value


def _validate_source_batch_value(section: Any, value: Any) -> tuple[str, str]:
    if section == "local_files":
        normalized = str(value or "").strip() if isinstance(value, str) else ""
        if not normalized or _SOURCE_BATCH_CONTROL.search(normalized):
            raise _SourceBatchOpError("invalid_value")
        return "local_files", normalized
    if section not in TIER3B_SOURCE_SECTIONS or not isinstance(value, str):
        raise _SourceBatchOpError("invalid_op" if section not in TIER3B_SOURCE_SECTIONS else "invalid_value")
    try:
        return validate_tier3b_source(section, value)
    except ValueError as exc:
        raise _SourceBatchOpError("invalid_value") from exc


def _source_entry_with_enabled(entry: Any, enabled: bool) -> Any:
    """Toggle without rewriting untouched shape: a bare string that stays
    enabled stays a string, and an unchanged flag adds no key."""

    if config_entry_enabled(entry) == enabled:
        return entry
    if isinstance(entry, dict):
        updated = dict(entry)
        updated["enabled"] = enabled
        return updated
    return {"value": entry, "enabled": False} if not enabled else entry


def _validate_source_batch_entry(payload: dict[str, Any], section: str, entry: Any) -> None:
    """Validate one written entry in context of its own fetch profile only, so
    the failing operation can be named per item. The whole resulting document
    is validated once more before the write (``write_sources`` validates it
    too), so legacy invalid content elsewhere in the file still holds the
    batch, as ``document_invalid``."""

    if not isinstance(entry, (str, dict)):
        raise _SourceBatchOpError("invalid_entry")
    profiles = payload.get("fetch_profiles", {})
    profiles = profiles if isinstance(profiles, dict) else {}
    profile = entry.get("fetch_profile") if isinstance(entry, dict) else None
    document: dict[str, Any] = {section: [entry], "fetch_profiles": {}}
    if isinstance(profile, str) and profile in profiles:
        document["fetch_profiles"] = {profile: profiles[profile]}
    try:
        validate_sources_config(document)
    except (ValueError, TypeError, AttributeError) as exc:
        raise _SourceBatchOpError("invalid_entry") from exc


class ImportDiagnosticError(ValueError):
    """Carry a stable import diagnostic across the background-task boundary."""

    def __init__(self, diagnostic: dict[str, Any]) -> None:
        payload = dict(diagnostic)
        payload["code"] = str(payload.get("code") or "import_failed")
        payload["params"] = dict(payload.get("params") or {})
        payload["message"] = str(payload.get("message") or "import failed")
        self.diagnostic = payload
        super().__init__(payload["message"])


def _import_diagnostic(
    code: str,
    message: str,
    **params: Any,
) -> dict[str, Any]:
    return {
        "code": str(code),
        "params": dict(params),
        "message": str(message),
        "source_kind": "validation",
        "blocking": True,
    }


def _known_import_error_diagnostic(exc: ValueError) -> dict[str, Any] | None:
    message = str(exc)
    lowered = message.lower()
    patterns = (
        (("subscription url is required",), "subscription_url_required"),
        (("url must be http or https",), "invalid_subscription_url"),
        (("subscription already exists",), "duplicate_subscription"),
        (("node already exists", "duplicate node already exists"), "duplicate_node"),
        (("node uri is required",), "invalid_node_uri"),
        (("no supported protocol sections",), "no_supported_nodes"),
        (("multiprotocol block is missing a server/address",), "missing_address"),
    )
    for phrases, code in patterns:
        if any(phrase in lowered for phrase in phrases):
            return _import_diagnostic(code, message)
    return None


def _raise_known_import_error(exc: ValueError) -> NoReturn:
    diagnostic = _known_import_error_diagnostic(exc)
    if diagnostic is None:
        raise exc
    raise ImportDiagnosticError(diagnostic) from exc


def validate_tier3b_source(section: Any, value: Any) -> tuple[str, str]:
    """Return a normalized Tier3B source identity or raise a user-facing error."""

    normalized_section = str(section or "").strip()
    if normalized_section not in TIER3B_SOURCE_SECTIONS:
        raise ValueError(
            "tier3 source section must be github_repos, telegram_channels, or subscription_urls"
        )
    normalized_value = str(value or "").strip()
    if not normalized_value:
        raise ValueError("tier3 source value is required")
    if normalized_section == "telegram_channels":
        telegram_value = normalized_value.removeprefix("@").strip()
        if normalized_value.startswith("@"):
            if not telegram_value or any(character.isspace() for character in telegram_value):
                raise ValueError("telegram channel must be an @name or t.me URL")
        elif "://" not in normalized_value and "/" not in normalized_value:
            if any(character.isspace() for character in normalized_value):
                raise ValueError("telegram channel must be an @name or t.me URL")
        else:
            validate_url(normalized_value)
            if urlparse(normalized_value).netloc.lower() not in {
                "t.me",
                "www.t.me",
                "telegram.me",
                "www.telegram.me",
            }:
                raise ValueError("telegram channel URL must use t.me or telegram.me")
    else:
        validate_url(normalized_value)
    return normalized_section, normalized_value


class ImportService:
    def __init__(self, config_store: ConfigStore) -> None:
        self.config_store = config_store

    def preview(self, payload: dict[str, Any]) -> dict[str, Any]:
        text = str(payload.get("text", "") or "")
        source = str(payload.get("source", "manual-import") or "manual-import")
        tier = str(payload.get("tier", "tier1") or "tier1")
        _analysis, _nodes, result = self._node_import_analysis(
            text=text,
            source=source,
            tier=tier,
            exact_one=tier in {"tier2", "tier3"},
        )
        return result

    def _node_import_analysis(
        self,
        *,
        text: str,
        source: str,
        tier: str,
        exact_one: bool,
    ) -> tuple[ContentAnalysisReport, list[Node], dict[str, Any]]:
        analysis = analyze_content_blob(text, source=source, tier=tier)
        nodes = list(analysis.accepted_nodes)
        rejected = [self._content_issue_payload(issue) for issue in analysis.rejected]
        blocking_errors = list(rejected)
        if not nodes:
            blocking_errors.append(
                self._service_issue(
                    "no_supported_nodes",
                    f"no supported nodes found for {tier} import",
                )
            )
        elif exact_one and len(nodes) != 1:
            blocking_errors.append(
                self._service_issue(
                    "exactly_one_node_required",
                    "import accepts exactly one single-protocol node",
                )
            )

        tier1_entries = tier1_import_entries_from_nodes(nodes) if tier == "tier1" else []
        physical_nodes = self._tier1_physical_summaries(tier1_entries, nodes) if tier == "tier1" else []
        result = {
            "source": source,
            "tier": tier,
            "node_count": len(nodes),
            "nodes": [summarize_import_node(node) for node in nodes],
            "warnings": import_warnings_for_tier(tier, nodes),
            "can_import": not blocking_errors,
            "candidate_count": int(analysis.candidate_count),
            "duplicate_count": int(analysis.duplicate_count),
            "rejected_count": len(rejected),
            "rejected": rejected,
            "blocking_errors": blocking_errors,
            "base64_sources": list(analysis.base64_sources),
            "ignored_count": int(analysis.ignored_count),
        }
        if tier == "tier1":
            result["physical_node_count"] = len(tier1_entries)
            result["physical_nodes"] = physical_nodes
        result["plan_id"] = self._node_import_plan_id(text=text, tier=tier, result=result)
        return analysis, nodes, result

    def _strict_node_import(
        self,
        payload: dict[str, Any],
        *,
        tier: str,
        default_source: str,
        exact_one: bool = False,
    ) -> tuple[list[Node], dict[str, Any]]:
        text = str(payload.get("text", "") or "")
        source = str(payload.get("source", default_source) or default_source)
        _analysis, nodes, preview = self._node_import_analysis(
            text=text,
            source=source,
            tier=tier,
            exact_one=exact_one,
        )
        expected_plan_id = str(payload.get("expected_plan_id", "") or "").strip()
        if expected_plan_id and not hmac.compare_digest(expected_plan_id, str(preview["plan_id"])):
            raise ImportDiagnosticError(
                _import_diagnostic(
                    "import_plan_changed",
                    "import content or recognition result changed; run preview again",
                )
            )
        if not preview["can_import"]:
            first_error = preview["blocking_errors"][0]
            raise ImportDiagnosticError(first_error)
        return nodes, preview

    def import_tier1(self, payload: dict[str, Any]) -> dict[str, Any]:
        nodes, _preview = self._strict_node_import(
            payload,
            tier="tier1",
            default_source="manual-tier1-import",
        )
        import_entries = tier1_import_entries_from_nodes(nodes)
        with self.config_store.lock:
            local_config = self.config_store.read_local()
            configured_nodes = self._tier_nodes_payload(local_config, "tier1")
            existing_names = {str(node.get("name", "")) for node in configured_nodes if isinstance(node, dict)}
            existing_keys = existing_tier1_duplicate_keys(configured_nodes)
            added = 0
            skipped = 0
            duplicates: list[str] = []
            for entry in import_entries:
                variants = expand_tier1_entry(entry)
                duplicate_variants = [node for node in variants if node_duplicate_key(node) in existing_keys]
                if duplicate_variants:
                    skipped += 1
                    duplicates.extend(node.name or node.remarks or node.address for node in duplicate_variants)
                    continue
                entry_payload = dict(entry)
                entry_payload["name"] = unique_import_name(str(entry_payload["name"]), existing_names)
                configured_nodes.append(entry_payload)
                existing_names.add(str(entry_payload["name"]))
                for variant in variants:
                    existing_keys.add(node_duplicate_key(variant))
                added += 1
            if added == 0 and duplicates:
                raise ImportDiagnosticError(
                    _import_diagnostic(
                        "duplicate_node",
                        f"duplicate node already exists: {', '.join(duplicates)}",
                        names=duplicates,
                    )
                )
            result = self.config_store.write_local(local_config)
        return {"added": added, "variant_count": len(nodes), "skipped": skipped, "duplicates": duplicates, **result}

    def preview_tier1_multiprotocol(self, payload: dict[str, Any]) -> dict[str, Any]:
        text = str(payload.get("text", "") or "")
        name = str(payload.get("name", "") or "") or None
        try:
            entry = parse_tier1_multiprotocol_block(text, name=name)
        except ValueError as exc:
            _raise_known_import_error(exc)
        fallback_order = parse_fallback_order_text(payload.get("fallback_order"))
        if fallback_order:
            entry["fallback_order"] = fallback_order
        variants = expand_tier1_entry(entry)
        return {
            "entry": summarize_tier1_import_entry(entry),
            "variant_count": len(variants),
            "variants": [summarize_import_node(node) for node in variants],
            "warnings": multiprotocol_import_warnings(entry, variants),
        }

    def import_tier1_multiprotocol(self, payload: dict[str, Any]) -> dict[str, Any]:
        text = str(payload.get("text", "") or "")
        name = str(payload.get("name", "") or "") or None
        try:
            entry = parse_tier1_multiprotocol_block(text, name=name)
        except ValueError as exc:
            _raise_known_import_error(exc)
        fallback_order = parse_fallback_order_text(payload.get("fallback_order"))
        if fallback_order:
            entry["fallback_order"] = fallback_order
        variants = expand_tier1_entry(entry)
        if not variants:
            raise ImportDiagnosticError(
                _import_diagnostic(
                    "no_supported_nodes",
                    "no supported protocol variants found",
                )
            )
        with self.config_store.lock:
            local_config = self.config_store.read_local()
            configured_nodes = self._tier_nodes_payload(local_config, "tier1")
            existing_names = {str(node.get("name", "")) for node in configured_nodes if isinstance(node, dict)}
            existing_keys = existing_tier1_duplicate_keys(configured_nodes)
            duplicate_variants = [node for node in variants if node_duplicate_key(node) in existing_keys]
            if duplicate_variants:
                names = sorted({node.name or node.remarks or node.address for node in duplicate_variants})
                raise ImportDiagnosticError(
                    _import_diagnostic(
                        "duplicate_node",
                        f"duplicate node already exists: {', '.join(names)}",
                        names=names,
                    )
                )
            entry["name"] = unique_import_name(str(entry["name"]), existing_names)
            configured_nodes.append(entry)
            result = self.config_store.write_local(local_config)
        return {"added": 1, "variant_count": len(variants), "entry": summarize_tier1_import_entry(entry), **result}

    def import_tier2_subscription(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = str(payload.get("url", "") or "")
        with self.config_store.lock:
            try:
                return self.config_store.add_tier2_subscription(url)
            except ValueError as exc:
                _raise_known_import_error(exc)

    def import_tier2_node(self, payload: dict[str, Any]) -> dict[str, Any]:
        nodes, _preview = self._strict_node_import(
            payload,
            tier="tier2",
            default_source="manual-tier2-import",
            exact_one=True,
        )
        node = nodes[0]
        node_payload = {"name": node.name or node.remarks or node.address, "uri": node.uri}
        with self.config_store.lock:
            try:
                result = self.config_store.add_tier2_node(node_payload)
            except ValueError as exc:
                _raise_known_import_error(exc)
        return {"added": 1, "node": summarize_import_node(node), **result}

    def import_tier3_subscription(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = str(payload.get("url", "") or "")
        with self.config_store.lock:
            try:
                return self.config_store.add_tier3_subscription(url)
            except ValueError as exc:
                _raise_known_import_error(exc)

    def import_tier3_node(self, payload: dict[str, Any]) -> dict[str, Any]:
        nodes, _preview = self._strict_node_import(
            payload,
            tier="tier3",
            default_source="manual-tier3a-import",
            exact_one=True,
        )
        node = nodes[0]
        node_payload = {"name": node.name or node.remarks or node.address, "uri": node.uri, "origin": "tier3a"}
        with self.config_store.lock:
            try:
                result = self.config_store.add_tier3_node(node_payload)
            except ValueError as exc:
                _raise_known_import_error(exc)
        return {"added": 1, "node": summarize_import_node(node), **result}

    def import_exit_nodes(self, payload: dict[str, Any]) -> dict[str, Any]:
        nodes, _preview = self._strict_node_import(
            payload,
            tier="exit",
            default_source="manual-exit-import",
        )
        with self.config_store.lock:
            local_config = self.config_store.read_local()
            configured_nodes = self._tier_nodes_payload(local_config, "exit")
            existing_uris = {
                imported_node_uri(item)
                for item in configured_nodes
                if imported_node_uri(item)
            }
            added = 0
            skipped = 0
            for node in nodes:
                if node.uri in existing_uris:
                    skipped += 1
                    continue
                configured_nodes.append({"name": node.name or node.remarks or node.address, "uri": node.uri})
                existing_uris.add(node.uri)
                added += 1
            if added == 0 and skipped:
                raise ImportDiagnosticError(
                    _import_diagnostic(
                        "duplicate_node",
                        "duplicate node already exists",
                    )
                )
            result = self.config_store.write_local(local_config)
        return {"added": added, "skipped": skipped, **result}

    def import_exit_subscription(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = str(payload.get("url", "") or "")
        with self.config_store.lock:
            try:
                return self.config_store.add_exit_subscription(url)
            except ValueError as exc:
                _raise_known_import_error(exc)

    def import_tier3_source(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Compatibility entry point for adding a new Tier3B source."""

        request = dict(payload)
        request.setdefault("value", request.get("url", ""))
        return self._upsert_tier3_source(request)

    def upsert_tier3_source(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Add or replace one Tier3B source without touching Tier3A configuration."""

        return self._upsert_tier3_source(payload)

    def _upsert_tier3_source(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        section, value = validate_tier3b_source(
            payload.get("section", "github_repos"),
            payload.get("value", payload.get("url", "")),
        )
        has_previous_section = "previous_section" in payload
        has_previous_value = "previous_value" in payload or "previous_url" in payload
        if has_previous_section != has_previous_value:
            raise ValueError("previous_section and previous_value must be provided together")
        previous: tuple[str, str] | None = None
        if has_previous_section:
            previous = validate_tier3b_source(
                payload.get("previous_section"),
                payload.get("previous_value", payload.get("previous_url", "")),
            )

        with self.config_store.lock:
            sources = self.config_store.read_sources()
            entries = self._source_entries_payload(sources, section)
            target_values = {source_entry_value(item).strip() for item in entries}

            if previous is None:
                if value in target_values:
                    raise ValueError("tier3 source already exists")
                entries.append(value)
                result = self.config_store.write_sources(sources)
                return {
                    "action": "added",
                    "section": section,
                    "value": value,
                    **result,
                }

            previous_section, previous_value = previous
            previous_entries = self._source_entries_payload(sources, previous_section)
            previous_indexes = [
                index
                for index, item in enumerate(previous_entries)
                if source_entry_value(item).strip() == previous_value
            ]
            if not previous_indexes:
                raise KeyError("tier3 source not found")
            if section == previous_section and value == previous_value:
                return {
                    "action": "unchanged",
                    "section": section,
                    "value": value,
                    "path": str(self.config_store.sources_path),
                    "backup": None,
                }
            if value in target_values:
                raise ValueError("tier3 source already exists")

            first_index = previous_indexes[0]
            previous_entry = previous_entries[first_index]
            replacement = self._source_entry_with_value(previous_entry, value)
            previous_entries[:] = [
                item
                for item in previous_entries
                if source_entry_value(item).strip() != previous_value
            ]
            if section == previous_section:
                previous_entries.insert(min(first_index, len(previous_entries)), replacement)
            else:
                entries.append(replacement)
            result = self.config_store.write_sources(sources)
            return {
                "action": "updated",
                "section": section,
                "value": value,
                "previous_section": previous_section,
                "previous_value": previous_value,
                **result,
            }

    def delete_tier3_source(self, payload: dict[str, Any]) -> dict[str, Any]:
        section, value = validate_tier3b_source(
            payload.get("section"),
            payload.get("value", payload.get("url", "")),
        )
        with self.config_store.lock:
            sources = self.config_store.read_sources()
            entries = self._source_entries_payload(sources, section)
            remaining = [
                item for item in entries if source_entry_value(item).strip() != value
            ]
            removed = len(entries) - len(remaining)
            if not removed:
                raise KeyError("tier3 source not found")
            sources[section] = remaining
            result = self.config_store.write_sources(sources)
            return {
                "action": "deleted",
                "section": section,
                "value": value,
                "removed": removed,
                **result,
            }

    def apply_tier3_source_batch(
        self,
        ops: list[dict[str, Any]],
        *,
        expected_digest: str | None = None,
    ) -> dict[str, Any]:
        """Apply several source edits with one read and at most one write.

        Operations run in order on an in-memory copy of the sources document:

        - ``{"op": "upsert", "section", "value", "previous_section"?,
          "previous_value"?, "enabled"?}``: the add/replace rules of
          ``upsert_tier3_source``; a replacement keeps the entry's additional
          keys and its value key (``value``/``url``/``path``).
        - ``{"op": "delete", "section", "value"}``
        - ``{"op": "set_enabled", "section", "value", "enabled": bool}``
        - ``{"op": "order", "section", "values": [str, ...]}``: the entries
          whose value is listed are rearranged in that order among the slots
          they already occupy; unlisted entries (for example valueless legacy
          rows) keep their exact slots.

        Every operation is checked before anything is written. Any failure
        raises ``SourceBatchError`` with per-operation errors and nothing is
        written; so does a resulting document that fails
        ``validate_sources_config`` (``document_invalid``). ``expected_digest``
        (``sources_document_digest`` of the document the caller planned
        against) turns a concurrent change into a ``conflict`` hold. Top-level
        keys and untouched entries are carried over as read. A replacement
        whose new value is still held by another entry is a ``duplicate``
        even when a later operation in the same batch would vacate it
        (swapping two values takes two saves). If the write raises after the
        file was already replaced (for example while restricting its
        permissions), the file is re-read under the lock and a document equal
        to the intended one counts as written. The existing single-source
        entry points are unchanged.
        """

        if not isinstance(ops, list) or len(ops) > SOURCE_BATCH_MAX_OPS:
            raise SourceBatchError([{"index": None, "op": None, "reason": "invalid_op"}])
        with self.config_store.lock:
            sources = self.config_store.read_sources()
            if expected_digest is not None and sources_document_digest(sources) != expected_digest:
                raise SourceBatchError([{"index": None, "op": None, "reason": "conflict"}])
            working = copy.deepcopy(sources)
            errors: list[dict[str, Any]] = []
            outcomes: list[dict[str, Any]] = []
            for index, op in enumerate(ops):
                name = op.get("op") if isinstance(op, dict) else None
                try:
                    outcomes.append(self._apply_source_batch_op(working, op))
                except _SourceBatchOpError as exc:
                    errors.append({
                        "index": index,
                        "op": name if name in {"upsert", "delete", "set_enabled", "order"} else None,
                        "reason": exc.reason,
                    })
            if errors:
                raise SourceBatchError(errors)
            if working == sources:
                return {
                    "action": "unchanged",
                    "written": False,
                    "operations": outcomes,
                    "path": str(self.config_store.sources_path),
                    "backup": None,
                }
            try:
                validate_sources_config(working)
            except (ValueError, TypeError, AttributeError) as exc:
                raise SourceBatchError(
                    [{"index": None, "op": None, "reason": "document_invalid"}]
                ) from exc
            try:
                result = self.config_store.write_sources(working)
            except Exception as exc:
                # The atomic replace may have happened before a later step
                # (permission tightening) raised. The lock is still held, so
                # an equal document on disk is this batch's write.
                try:
                    landed = sources_document_digest(self.config_store.read_sources())
                except Exception:
                    raise exc from None
                if landed != sources_document_digest(working):
                    raise
                result = {
                    "path": str(self.config_store.sources_path),
                    "backup": None,
                    "write_warning": type(exc).__name__,
                }
            return {"action": "batch", "written": True, "operations": outcomes, **result}

    @staticmethod
    def _source_batch_key(section: Any, value: Any) -> tuple[str, str]:
        """Identify an existing entry. Deliberately lenient: an entry stored
        before today's value rules must still be deletable/toggleable."""

        if section not in SOURCE_BATCH_SECTIONS:
            raise _SourceBatchOpError("invalid_op")
        if not isinstance(value, str) or not value.strip():
            raise _SourceBatchOpError("invalid_value")
        return section, value.strip()

    @staticmethod
    def _source_batch_entries(working: dict[str, Any], section: str) -> list[Any] | None:
        entries = working.get(section)
        if entries is None:
            return None
        if not isinstance(entries, list):
            raise _SourceBatchOpError("invalid_entry")
        return entries

    @staticmethod
    def _source_batch_matches(entries: list[Any] | None, value: str) -> list[int]:
        return [
            index
            for index, item in enumerate(entries or ())
            if source_entry_value(item).strip() == value
        ]

    def _apply_source_batch_op(self, working: dict[str, Any], op: Any) -> dict[str, Any]:
        if not isinstance(op, dict):
            raise _SourceBatchOpError("invalid_op")
        name = op.get("op")
        if name == "upsert":
            return self._source_batch_upsert(working, op)
        if name == "delete":
            section, value = self._source_batch_key(op.get("section"), op.get("value"))
            entries = self._source_batch_entries(working, section)
            remaining = [
                item for item in entries or () if source_entry_value(item).strip() != value
            ]
            if entries is None or len(remaining) == len(entries):
                raise _SourceBatchOpError("not_found")
            removed = len(entries) - len(remaining)
            entries[:] = remaining
            return {"op": "delete", "action": "deleted", "section": section, "removed": removed}
        if name == "set_enabled":
            section, value = self._source_batch_key(op.get("section"), op.get("value"))
            enabled = op.get("enabled")
            if not isinstance(enabled, bool):
                raise _SourceBatchOpError("invalid_op")
            entries = self._source_batch_entries(working, section)
            indexes = self._source_batch_matches(entries, value)
            if not indexes:
                raise _SourceBatchOpError("not_found")
            changed = 0
            for index in indexes:
                updated = _source_entry_with_enabled(entries[index], enabled)
                if updated is not entries[index]:
                    entries[index] = updated
                    changed += 1
            return {
                "op": "set_enabled",
                "action": "updated" if changed else "unchanged",
                "section": section,
            }
        if name == "order":
            section = op.get("section")
            values = op.get("values")
            if section not in SOURCE_BATCH_SECTIONS or not isinstance(values, list):
                raise _SourceBatchOpError("invalid_op")
            if any(not isinstance(item, str) or not item.strip() for item in values):
                raise _SourceBatchOpError("invalid_value")
            values = [item.strip() for item in values]
            if len(set(values)) != len(values):
                raise _SourceBatchOpError("duplicate")
            entries = self._source_batch_entries(working, section)
            if any(not self._source_batch_matches(entries, item) for item in values):
                raise _SourceBatchOpError("not_found")
            if entries is None:
                return {"op": "order", "action": "unchanged", "section": section}
            position = {item: index for index, item in enumerate(values)}
            slots = [
                index
                for index, item in enumerate(entries)
                if source_entry_value(item).strip() in position
            ]
            listed = sorted(
                (entries[index] for index in slots),
                key=lambda item: position[source_entry_value(item).strip()],
            )
            ordered = list(entries)
            for index, item in zip(slots, listed):
                ordered[index] = item
            action = "unchanged" if all(a is b for a, b in zip(ordered, entries)) else "updated"
            entries[:] = ordered
            return {"op": "order", "action": action, "section": section}
        raise _SourceBatchOpError("invalid_op")

    def _source_batch_upsert(self, working: dict[str, Any], op: dict[str, Any]) -> dict[str, Any]:
        section, value = _validate_source_batch_value(op.get("section"), op.get("value"))
        enabled = op.get("enabled")
        if enabled is not None and not isinstance(enabled, bool):
            raise _SourceBatchOpError("invalid_op")
        has_previous_section = "previous_section" in op
        if has_previous_section != ("previous_value" in op):
            raise _SourceBatchOpError("invalid_op")
        target = self._source_batch_entries(working, section)
        if not has_previous_section:
            if self._source_batch_matches(target, value):
                raise _SourceBatchOpError("duplicate")
            # Bare string when enabled: the same shape the Qt editor appends.
            entry: Any = value if enabled in (None, True) else {"value": value, "enabled": False}
            _validate_source_batch_entry(working, section, entry)
            if target is None:
                target = working[section] = []
            target.append(entry)
            return {"op": "upsert", "action": "added", "section": section}

        previous_section, previous_value = self._source_batch_key(
            op.get("previous_section"), op.get("previous_value"),
        )
        previous_entries = self._source_batch_entries(working, previous_section)
        indexes = self._source_batch_matches(previous_entries, previous_value)
        if not indexes:
            raise _SourceBatchOpError("not_found")
        if section == previous_section and value == previous_value:
            if enabled is None:
                return {"op": "upsert", "action": "unchanged", "section": section}
            updated = [
                _source_entry_with_enabled(previous_entries[index], enabled) for index in indexes
            ]
            for index, entry in zip(indexes, updated):
                previous_entries[index] = entry
            return {"op": "upsert", "action": "updated", "section": section}
        if self._source_batch_matches(target, value):
            raise _SourceBatchOpError("duplicate")
        first_index = indexes[0]
        replacement = self._source_entry_with_value(previous_entries[first_index], value)
        if enabled is not None:
            replacement = _source_entry_with_enabled(replacement, enabled)
        _validate_source_batch_entry(working, section, replacement)
        previous_entries[:] = [
            item for item in previous_entries if source_entry_value(item).strip() != previous_value
        ]
        if section == previous_section:
            previous_entries.insert(min(first_index, len(previous_entries)), replacement)
        else:
            if target is None:
                target = working[section] = []
            target.append(replacement)
        return {"op": "upsert", "action": "updated", "section": section}

    def _tier_nodes_payload(self, payload: dict[str, Any], tier: str) -> list[Any]:
        tiers = ensure_mapping_section(payload, "tiers")
        tier_payload = ensure_mapping_section(tiers, tier)
        return ensure_list_section(tier_payload, "nodes")

    def _source_entries_payload(self, payload: dict[str, Any], section: str) -> list[Any]:
        return ensure_list_section(payload, section)

    def _source_entry_with_value(self, entry: Any, value: str) -> Any:
        if not isinstance(entry, dict):
            return value
        updated = dict(entry)
        value_key = next(
            (key for key in ("value", "url", "path") if key in updated),
            "value",
        )
        for key in ("value", "url", "path"):
            if key != value_key:
                updated.pop(key, None)
        updated[value_key] = value
        return updated

    def _node_import_plan_id(self, *, text: str, tier: str, result: dict[str, Any]) -> str:
        stable_rejected = [
            {
                key: issue.get(key)
                for key in ("code", "scheme", "fingerprint", "source_kind", "position", "blocking")
                if key in issue
            }
            for issue in result.get("rejected", [])
            if isinstance(issue, dict)
        ]
        payload = {
            "version": 1,
            "tier": tier,
            "text_digest": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "candidate_count": result.get("candidate_count", 0),
            "duplicate_count": result.get("duplicate_count", 0),
            "nodes": result.get("nodes", []),
            "rejected": stable_rejected,
            "physical_nodes": result.get("physical_nodes", []),
            "blocking_codes": [
                issue.get("code", "")
                for issue in result.get("blocking_errors", [])
                if isinstance(issue, dict)
            ],
        }
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def _tier1_physical_summaries(
        self,
        entries: list[dict[str, Any]],
        nodes: list[Node],
    ) -> list[dict[str, Any]]:
        node_by_uri = {node.uri: node for node in nodes if node.uri}
        summaries: list[dict[str, Any]] = []
        for entry in entries:
            summary = summarize_tier1_import_entry(entry)
            if not summary["protocols"]:
                uri = str(entry.get("vless_link") or entry.get("imported_link") or "")
                node = node_by_uri.get(uri)
                if node is not None:
                    summary["protocols"] = [tier1_protocol_key_from_node(node)]
            summaries.append(summary)
        return summaries

    def _content_issue_payload(self, issue: Any) -> dict[str, Any]:
        if hasattr(issue, "to_dict"):
            payload = issue.to_dict()
            if isinstance(payload, dict):
                return dict(payload)
        return self._service_issue("invalid_node", str(getattr(issue, "message", "invalid node")))

    def _service_issue(self, code: str, message: str) -> dict[str, Any]:
        return {
            "code": code,
            "message": message,
            "scheme": "",
            "fingerprint": "",
            "source_kind": "validation",
            "position": None,
            "blocking": True,
        }

    def _warnings_for_tier(self, tier: str, nodes: list[Node]) -> list[dict[str, Any]]:
        return import_warnings_for_tier(tier, nodes)

    def _multiprotocol_warnings(self, entry: dict[str, Any], nodes: list[Node]) -> list[dict[str, Any]]:
        return multiprotocol_import_warnings(entry, nodes)

    def _summarize_tier1_entry(self, entry: dict[str, Any]) -> dict[str, Any]:
        return summarize_tier1_import_entry(entry)

    def _summarize_node(self, node: Node) -> dict[str, Any]:
        return summarize_import_node(node)

    def _node_to_tier1_config(self, node: Node) -> dict[str, Any]:
        return node_to_tier1_config(node)

    def _tier1_import_entries_from_nodes(self, nodes: list[Node]) -> list[dict[str, Any]]:
        return tier1_import_entries_from_nodes(nodes)

    def _unique_name(self, name: str, existing_names: set[str]) -> str:
        return unique_import_name(name, existing_names)

    def _existing_tier1_duplicate_keys(self, entries: list[Any]) -> set[tuple[str, ...]]:
        return existing_tier1_duplicate_keys(entries)

    def _node_duplicate_key(self, node: Node) -> tuple[str, ...]:
        return node_duplicate_key(node)
