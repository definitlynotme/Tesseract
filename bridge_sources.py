"""Sources section save adapter (T-P2-13).

P2-10's ``settings.apply`` delegates the ``sources`` ConfigChange here. One
call validates the whole change against the latest sources document, turns it
into a batch of upsert/delete/enable/order operations and hands that batch to
the runtime's single public batch entry: one lock, one sources write and at
most one ``reload_config``. Nothing is written when any item is invalid
(hold, per-item errors). A write whose reload fails is reported as saved but
not applied.

Contract notes (PROTOCOL sections 12, 15, 16):

- Source rows are identified by the P2-04 stable ID
  (``bridge_runtime_access.source_identity``): a persisted ``id`` key wins,
  otherwise the kind and the stripped source value. Enabling, disabling,
  reordering and edits of other rows never change a row's ID. Changing the
  value of a row without a persisted ``id`` necessarily yields a new ID; the
  mapping is returned in ``id_changes``.
- Source values (URL, owner/repo, channel, path) are secrets: they are only
  written through ``SecretEdit`` (``bridge_validation`` rejects them, and
  owner/repo, as ordinary values). GitHub ``owner/repo`` is stored as its
  canonical URL. Replacing a value keeps the entry's additional keys
  (fetch profile/paths, delivery, extensions) and its value key.
- Top-level keys of the sources document (fetch profiles, delivery, discovery,
  migrations) and untouched entries are carried over as read. Hidden settings
  such as ``node_judgement`` live in other files and are never written here;
  a submission containing them is rejected by the validator.
- ``display`` has no backing key in the sources document (the projection
  derives it from ``kind``); a submitted display is validated and ignored,
  and an existing ``display`` key in the file is kept unchanged.
- An existing row's ``kind`` cannot change (``source_kind_immutable`` hold):
  moving an entry across sections would carry section-specific additional
  keys along. The page deletes the row and adds a new one instead.
- Swapping the values of two rows in one save is a ``source_duplicate`` hold
  (each replacement is checked against the document as it stands at that
  step); the page saves the two edits separately.
- New SecretEdit values are registered in the redaction table (value ->
  category, the same categories ``_config_secret_values`` uses) before the
  write; a registration failure writes nothing.

Runs on a mutation-domain worker; never on the pipe reader or publication
tick. Source values and exception text never enter a result or an Error.
"""
from __future__ import annotations

import logging
import threading
from copy import deepcopy
from dataclasses import dataclass, field as data_field
from typing import Any, Callable, Mapping, Protocol

from bridge_runtime_access import source_hint, source_identity
from bridge_validation import validate_change
from config_item_identity import source_entry_value
from config_utils import config_entry_enabled
from import_service import SourceBatchError, sources_document_digest


# Same order as the ConfigValues projection (bridge_runtime_access).
SOURCE_SECTIONS = ("github_repos", "telegram_channels", "subscription_urls", "local_files")
TOPIC = "bastion.sources"
# Redaction categories, aligned with bridge_runtime_access._config_secret_values
# (subscription_urls -> "subscription"; other "://" values -> "secret").
_SECRET_CATEGORY = {"subscription_urls": "subscription"}
_VALUE_KEYS = ("value", "url", "path")
_LOGGER = logging.getLogger(__name__)


class SourcePort(Protocol):
    """Runtime facts and the one write entry used by this adapter."""

    def read_sources(self) -> Mapping[str, Any]:
        """Latest sources document (backend, contains secrets)."""

    def apply_source_batch(self, ops: list[dict[str, Any]], *,
                           expected_digest: str | None) -> Mapping[str, Any]:
        """One lock, one write, at most one reload. Raises SourceBatchError
        (nothing written) or returns ``written``/``reloaded``/``reload_error``."""


class RuntimeSourcePort:
    """Adapter over DesktopRuntime's public methods only."""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    def read_sources(self) -> Mapping[str, Any]:
        return self.runtime.read_sources_config()

    def apply_source_batch(self, ops: list[dict[str, Any]], *,
                           expected_digest: str | None) -> Mapping[str, Any]:
        try:  # literal attribute: the bridge gate forbids getattr on the runtime
            batch = self.runtime.apply_source_batch
        except AttributeError:
            raise NotImplementedError("DesktopRuntime.apply_source_batch") from None
        return batch(ops, expected_digest=expected_digest)


@dataclass(frozen=True)
class _Origin:
    section: str
    value: str
    enabled: bool
    persisted_id: str | None
    shared: bool


@dataclass
class SourceSaveOutcome:
    """Section outcome. ``section_result()`` is the SectionResult DTO; the
    rest is backend detail for P2-10 (never forwarded as-is)."""

    saved: bool
    applied: bool
    effect: str  # none | reload
    error: dict[str, Any] | None = None
    errors: list[dict[str, Any]] = data_field(default_factory=list)
    written: bool = False
    id_changes: dict[str, str] = data_field(default_factory=dict)
    items: list[dict[str, Any]] | None = None

    def section_result(self) -> dict[str, Any]:
        return {"section": "sources", "saved": self.saved, "applied": self.applied,
                "effect": self.effect, "error": deepcopy(self.error)}


def _entry_value(entry: Any) -> str:
    value = source_entry_value(entry)
    return value.strip() if isinstance(value, str) else ""


def source_rows(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Public Source rows, identical to the ConfigValues projection; duplicate
    legacy entries (same identity) appear once so the list topic stays valid."""
    rows, seen = [], set()
    for kind in SOURCE_SECTIONS:
        entries = document.get(kind)
        for entry in entries if isinstance(entries, list) else ():
            identity = source_identity(kind, entry)
            if identity in seen:
                continue
            seen.add(identity)
            rows.append({"id": identity, "kind": kind, "display": kind,
                         "enabled": config_entry_enabled(entry)})
            hint = source_hint(kind, entry)  # T-P4-C1 #9 (U4), same as config.snapshot
            if hint is not None:
                rows[-1]["hint"] = hint
    return rows


def backend_rows(document: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, _Origin]]:
    """Validator input (``current["sources"]``): each entry with its public ID
    and kind, additional keys included. Private: contains source values."""
    rows: list[dict[str, Any]] = []
    staged: list[tuple[str, str, str, bool, str | None]] = []
    owners: dict[tuple[str, str], set[str]] = {}
    seen: set[str] = set()
    for kind in SOURCE_SECTIONS:
        entries = document.get(kind)
        for entry in entries if isinstance(entries, list) else ():
            identity = source_identity(kind, entry)
            value = _entry_value(entry)
            owners.setdefault((kind, value), set()).add(identity)
            if identity in seen:
                continue
            seen.add(identity)
            row = deepcopy(entry) if isinstance(entry, dict) else {
                "value": entry if isinstance(entry, str) else ""}
            if not any(key in row for key in _VALUE_KEYS):
                # Valueless legacy mapping (e.g. {enabled: false, note: ..}):
                # an explicit empty value lets P2-09 treat it as a retained
                # clear instead of holding every unrelated save. Validator
                # input only; rows are never written back.
                row["value"] = ""
            row["id"], row["kind"] = identity, kind
            rows.append(row)
            persisted = entry.get("id") if isinstance(entry, dict) and entry.get("id") else None
            staged.append((identity, kind, value, config_entry_enabled(entry), persisted))
    origins = {identity: _Origin(kind, value, enabled, persisted, len(owners[(kind, value)]) > 1)
               for identity, kind, value, enabled, persisted in staged}
    return rows, origins


def _error(code: str, field: str, reason: str) -> dict[str, Any]:
    if code == "validation":
        return {"code": "validation", "message_key": "errors.settings.invalid_field",
                "params": {"field": field}, "field": field, "hold": True, "reason_code": reason}
    if code == "conflict":
        return {"code": "conflict", "message_key": "errors.settings.conflict",
                "params": {"section": "sources"}, "hold": True, "reason_code": reason}
    return {"code": "internal" if code == "internal" else "io",
            "message_key": "errors.settings.apply_failed" if reason == "source_reload_failed"
            else "errors.settings.save_failed", "params": {"section": "sources"}, "reason_code": reason}


def _held(errors: list[dict[str, Any]]) -> SourceSaveOutcome:
    return SourceSaveOutcome(False, False, "none", errors[0], errors)


@dataclass
class _Plan:
    ops: list[dict[str, Any]]
    fields: list[str]
    errors: list[dict[str, Any]]
    id_changes: dict[str, str]
    secrets: dict[str, str]


def _row_value(row: Mapping[str, Any]) -> str:
    value = row.get("value") or row.get("url") or row.get("path") or ""
    return value.strip() if isinstance(value, str) else ""


def plan_batch(change: Mapping[str, Any], result: Any, origins: Mapping[str, _Origin]) -> _Plan:
    """Translate a validated ConfigChange into batch operations.

    Order: deletes, replacements, adds, enable toggles, then (only when the
    page submitted ``items``) one order operation per section.
    """
    rows = result.values
    secret_field: dict[int, str] = {}
    cleared: set[int] = set()
    secrets: dict[str, str] = {}
    for position, placement in enumerate(result.secret_edits):
        index = placement.path[0]
        secret_field[index] = f"sources.secrets[{position}].value"
        if placement.edit.get("action") == "clear":
            cleared.add(index)
        elif placement.edit.get("action") == "set":
            # KnownSecrets contract: key = secret value, value = category.
            row = rows[index]
            category = _SECRET_CATEGORY.get(row["kind"], "secret")
            raw = placement.edit.get("value")
            for value in (raw, raw.strip() if isinstance(raw, str) else None, _row_value(row)):
                if isinstance(value, str) and value:
                    secrets[value] = category
    deletes, replaces, adds, toggles = [], [], [], []
    errors: list[dict[str, Any]] = []
    id_changes: dict[str, str] = {}
    present = {row["id"] for row in rows}
    kept: list[tuple[int, str, str]] = []
    for index, row in enumerate(rows):
        identity, section, value = row["id"], row["kind"], _row_value(row)
        enabled = config_entry_enabled(row)
        value_field = secret_field.get(index, f"sources.items[{index}].value")
        origin = origins.get(identity)
        if index in cleared and (origin is None or origin.value):
            # The value is the source's identity: an emptied source cannot be
            # stored or loaded. Delete the row instead.
            errors.append(_error("validation", value_field, "source_clear_requires_delete"))
            continue
        if origin is None:
            adds.append(({"op": "upsert", "section": section, "value": value, "enabled": enabled}, value_field))
            id_changes[identity] = source_identity(section, value)
            kept.append((index, section, value))
            continue
        if section != origin.section:
            errors.append(_error("validation", f"sources.items[{index}].kind", "source_kind_immutable"))
            continue
        moved = value != origin.value
        if origin.shared and (moved or enabled != origin.enabled):
            errors.append(_error("validation", f"sources.items[{index}].id", "source_ambiguous_identity"))
            continue
        if moved:
            op = {"op": "upsert", "section": section, "value": value,
                  "previous_section": origin.section, "previous_value": origin.value}
            if enabled != origin.enabled:
                op["enabled"] = enabled
            replaces.append((op, value_field))
            new_id = source_identity(section, {"id": origin.persisted_id} if origin.persisted_id else value)
            if new_id != identity:
                id_changes[identity] = new_id
        elif enabled != origin.enabled:
            toggles.append(({"op": "set_enabled", "section": section, "value": value, "enabled": enabled},
                            f"sources.items[{index}].enabled"))
        kept.append((index, section, value))
    by_key: dict[tuple[str, str], list[str]] = {}
    for identity, origin in origins.items():
        by_key.setdefault((origin.section, origin.value), []).append(identity)
    removed_keys = [key for key, identities in by_key.items()
                    if any(identity not in present for identity in identities)]
    for section, value in removed_keys:
        if any(identity in present for identity in by_key[(section, value)]):
            errors.append(_error("validation", "sources.items", "source_ambiguous_identity"))
            continue
        deletes.append(({"op": "delete", "section": section, "value": value}, "sources.items"))
    ordered = deletes + replaces + adds + toggles
    if "items" in change.get("values", {}):
        for section in SOURCE_SECTIONS:
            values = list(dict.fromkeys(value for _, kind, value in kept if kind == section and value))
            if values:
                ordered.append(({"op": "order", "section": section, "values": values}, "sources.items"))
    return _Plan([op for op, _ in ordered], [f for _, f in ordered], errors, id_changes, secrets)


class SourceSectionSaver:
    """``save(change)`` for the sources ConfigChange; see module docstring."""

    def __init__(self, port: SourcePort, *,
                 register_secrets: Callable[[dict[str, str]], None] | None = None,
                 refresh_secrets: Callable[[], None] | None = None,
                 publish: Callable[[list[dict[str, Any]]], Any] | None = None) -> None:
        self.port = port
        self._register_secrets = register_secrets
        self._refresh_secrets = refresh_secrets
        self._publish = publish

    def current_rows(self) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, _Origin]]:
        document = deepcopy(dict(self.port.read_sources() or {}))
        rows, origins = backend_rows(document)
        return document, rows, origins

    def save(self, change: Mapping[str, Any]) -> SourceSaveOutcome:
        if not isinstance(change, Mapping) or change.get("section") != "sources":
            return _held([_error("validation", "change.section", "source_section_mismatch")])
        document, rows, origins = self.current_rows()
        result = validate_change(change, {"sources": rows})
        if not result.valid:
            return _held([{**error, "hold": True} for error in result.errors])
        plan = plan_batch(change, result, origins)
        if plan.errors:
            return _held(plan.errors)
        if not plan.ops:
            return SourceSaveOutcome(True, True, "none", items=source_rows(document))
        if plan.secrets and self._register_secrets is not None:
            # PROTOCOL section 12: register before anything that may log. If
            # registration fails nothing is written.
            try:
                self._register_secrets({value: category for value, category in plan.secrets.items() if value})
            except Exception:
                return _held([_error("internal", "sources", "source_secret_registration_failed")])
        try:
            receipt = self.port.apply_source_batch(
                plan.ops, expected_digest=sources_document_digest(document))
        except SourceBatchError as exc:
            return _held(self._batch_errors(exc, plan.fields))
        except NotImplementedError:
            raise
        except OSError:
            error = _error("io", "sources", "source_write_failed")
            return SourceSaveOutcome(False, False, "none", error, [error])
        except Exception as exc:
            # Not a file-system failure: an unexpected fault before the write.
            # Local diagnostics carry the exception type only (no values).
            _LOGGER.warning("Source batch failed unexpectedly: %s", type(exc).__name__)
            error = _error("internal", "sources", "source_batch_internal_error")
            return SourceSaveOutcome(False, False, "none", error, [error])
        finally:
            if plan.secrets and self._refresh_secrets is not None:
                try:
                    self._refresh_secrets()
                except Exception:
                    pass
        written = bool(receipt.get("written"))
        items = self._published_items()
        if not written:
            return SourceSaveOutcome(True, True, "none", items=items)
        if receipt.get("reloaded"):
            return SourceSaveOutcome(True, True, "reload", written=True,
                                     id_changes=dict(plan.id_changes), items=items)
        error = _error("io", "sources", "source_reload_failed")
        return SourceSaveOutcome(True, False, "reload", error, [error], written=True,
                                 id_changes=dict(plan.id_changes), items=items)

    @staticmethod
    def _batch_errors(exc: SourceBatchError, fields: list[str]) -> list[dict[str, Any]]:
        errors = []
        for record in exc.errors:
            reason = record.get("reason")
            index = record.get("index")
            field = fields[index] if isinstance(index, int) and 0 <= index < len(fields) else "sources.items"
            if reason in {"conflict", "not_found"}:
                errors.append(_error("conflict", field, f"source_{reason}"))
            elif reason == "document_invalid":
                errors.append(_error("validation", "sources.items", "source_document_invalid"))
            else:
                errors.append(_error("validation", field, f"source_{reason}"))
        return errors or [_error("validation", "sources.items", "source_invalid_op")]

    def _published_items(self) -> list[dict[str, Any]] | None:
        """Re-read the committed document once; publish bastion.sources."""
        try:
            items = source_rows(dict(self.port.read_sources() or {}))
        except Exception:
            return None
        self.publish(items)
        return items

    def publish(self, items: list[dict[str, Any]] | None = None) -> bool:
        """Record the bastion.sources list (seed or after a write). Returns
        whether a publication hook accepted a changed list."""
        if self._publish is None:
            return False
        if items is None:
            items = source_rows(dict(self.port.read_sources() or {}))
        try:
            return bool(self._publish(items))
        except Exception:
            return False


def _session_secret_hooks(session: Any):
    """Batch secrets as a lease of the session's layered registry (T-P2-31a):
    registration never replaces other modules' layers; the refresh releases
    this saver's leases and re-reads the saved config. A session without a
    registry keeps the P1-16 table-replacement path."""
    from bridge_secrets import session_registry
    leases: list[Any] = []
    lock = threading.Lock()

    def register_secrets(values: dict[str, str]) -> None:
        registry = session_registry(session)
        if registry is not None:
            handle = registry.lease("sources", values)   # raises: nothing written
            if handle is not None:
                with lock:
                    leases.append(handle)
            return
        logs = getattr(session, "logs", None)
        if logs is None:
            return
        table = dict(session.access.known_secret_values())
        table.update(values)
        logs.refresh_known_secret_values(table)

    def refresh_secrets() -> None:
        registry = session_registry(session)
        with lock:
            held = list(leases)
            leases.clear()
        if registry is not None:
            for handle in held:
                registry.release(handle, refresh="none")
        if getattr(session, "logs", None) is not None:
            session.refresh_secrets()
        elif registry is not None and held:
            registry.refresh()

    return register_secrets, refresh_secrets


def register(session: Any, *, port: SourcePort | None = None) -> SourceSectionSaver:
    """Build the sources saver for P2-10. No command or query of its own:
    ``settings.apply`` (P2-10) calls ``saver.save(change)`` for the sources
    ConfigChange on its mutation worker. ``bastion.sources`` is recorded via
    ``session.state.record_list`` after each write; integration seeds it by
    calling ``saver.publish()`` once on a normal worker."""
    runtime = getattr(session, "runtime", None)
    if runtime is None:
        runtime = session.access.runtime
    register_secrets, refresh_secrets = _session_secret_hooks(session)
    state = getattr(session, "state", None)
    record_list = getattr(state, "record_list", None)
    publish = (lambda items: record_list(TOPIC, items)) if callable(record_list) else None
    return SourceSectionSaver(port or RuntimeSourcePort(runtime), register_secrets=register_secrets,
                              refresh_secrets=refresh_secrets, publish=publish)
