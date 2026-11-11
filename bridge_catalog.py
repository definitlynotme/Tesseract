"""Static form catalogs for the v1 bridge (M-11): import.specs.

Only stable IDs leave this module: protocol/channel/field IDs, message IDs
(``catalog.<protocol>.<field>``), types, required flags and declarative
conditions. No translated labels, no placeholders and no channel secrets.
Defaults are never presented as saved values; where the backend substitutes a
value for an empty field it is published as ``conditions.implicit_default``
(evaluation hint only, the page must not prefill or persist it).

Secret marking has a single source: ``bridge_vocab.secret_projection_path``,
plus the existing protocol editor's masked input type for node forms.
URI construction stays in Python (``bridge_import`` + ``structured_node_uri``).
"""

from __future__ import annotations

import copy
import threading
from typing import Any, Callable, Iterable, Mapping

import bridge_protocol as protocol


IMPORT_SPECS_QUERY = "import.specs"

# Existing editor input types -> FieldSpec.type (PROTOCOL §16 closed enum).
_INPUT_TYPES = {"text": "string", "integer": "integer", "enum": "choice",
                "password": "secret", "boolean": "boolean"}
_PROTOCOL_TIERS = ("vanguard", "sentinel", "bastion", "gateway")
_RUNTIME_LAYERS = {"vanguard": "tier1", "sentinel": "tier2", "bastion": "tier3", "gateway": "exit"}


def _app_version() -> str:
    try:
        from app_info import APP_VERSION
    except Exception:  # pragma: no cover - packaging fallback
        return "unknown"
    return str(APP_VERSION)


def _condition(condition: Any) -> dict[str, Any]:
    """FieldCondition -> closed JSON; ANDed inside one list, like the editor."""
    result: dict[str, Any] = {"field": str(condition.field_key)}
    if condition.values:
        result["values"] = [str(value) for value in condition.values]
    if condition.excluded_values:
        result["excluded_values"] = [str(value) for value in condition.excluded_values]
    if condition.present is not None:
        result["present"] = bool(condition.present)
    return result


def _field_spec(field_id: str, label_id: str, kind: str, *, required: bool,
                choices: Iterable[str] = (), conditions: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {"id": field_id, "label_id": label_id, "type": kind, "required": bool(required),
            "choices": [str(choice) for choice in choices], "conditions": dict(conditions or {})}


def _node_field_is_secret(field_id: str, input_type: str) -> bool:
    from bridge_vocab import secret_projection_path
    # The vocabulary is the registered source; a masked editor input is kept
    # as a secret too (e.g. obfs_password is not yet registered, see report).
    return input_type == "password" or secret_projection_path(None, field_id)


def _protocol_spec(section: Any) -> dict[str, Any]:
    from import_mode_view import manual_protocol_options
    from import_protocol_view import (PROTOCOL_FIELD_DEFAULTS, SINGLE_PROTOCOL_BASE_FIELDS,
                                      protocol_template_spec)
    key = section.key
    template = protocol_template_spec(key)
    placements: dict[str, tuple[Any, Any]] = {}
    if template is not None:
        for group in template.groups:
            for placement in group.fields:
                placements[placement.field_key] = (placement, group)
    defaults = PROTOCOL_FIELD_DEFAULTS.get(key, {})
    tiers = [tier for tier in _PROTOCOL_TIERS
             if any(value == key for _label, value in manual_protocol_options(_RUNTIME_LAYERS[tier], "single"))]
    chain = any(value == key for _label, value in manual_protocol_options("tier1", "multi"))
    # Protocol-level scope: the closed ProtocolSpec has no metadata slot, so
    # the real "tier" input carries the allowed tiers and chain eligibility.
    fields = [_field_spec("tier", f"catalog.{key}.tier", "choice", required=True, choices=tiers,
                          conditions={"scope": "protocol", "advanced": False, "chain_allowed": chain})]
    for base in SINGLE_PROTOCOL_BASE_FIELDS:
        fields.append(_field_spec(base.key, f"catalog.{key}.{base.key}",
                                  "secret" if _node_field_is_secret(base.key, base.input_type) else "string",
                                  required=True, conditions={"group": "base", "advanced": False}))
    for field in section.fields:
        located = placements.get(field.key)
        if located is None:
            continue  # Unplaced fields are never visible, so never sent to URI construction.
        placement, group = located
        kind = "secret" if _node_field_is_secret(field.key, field.input_type) else _INPUT_TYPES.get(field.input_type, "string")
        conditions: dict[str, Any] = {"group": group.key, "advanced": bool(field.advanced or group.advanced)}
        if placement.visible_when:
            conditions["visible_when"] = [_condition(item) for item in placement.visible_when]
        if placement.required_when:
            conditions["required_when"] = [_condition(item) for item in placement.required_when]
        if field.minimum is not None:
            conditions["min"] = int(field.minimum)
        if field.maximum is not None:
            conditions["max"] = int(field.maximum)
        if field.key in defaults:
            conditions["implicit_default"] = str(defaults[field.key])
        if template is not None:
            notices = [{"code": notice.code, "visible_when": [_condition(item) for item in notice.visible_when]}
                       for notice in template.notices
                       if any(item.field_key == field.key for item in notice.visible_when)]
            if notices:
                conditions["notices"] = notices
        fields.append(_field_spec(field.key, f"catalog.{key}.{field.key}", kind,
                                  required=bool(placement.required), choices=field.choices,
                                  conditions=conditions))
    return {"id": key, "label_id": f"catalog.{key}", "fields": fields}


def build_import_specs() -> dict[str, Any]:
    """Every protocol template from import_protocol_view, in editor order."""
    from import_protocol_view import PROTOCOL_SPECS
    return {"protocols": [_protocol_spec(section) for section in PROTOCOL_SPECS]}


# --- cache ----------------------------------------------------------------

class CatalogCache:
    """Application-version scoped static cache; each caller gets a private copy."""

    def __init__(self, version: Callable[[], str] = _app_version) -> None:
        self._version = version
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[str, dict[str, Any]]] = {}

    def get(self, name: str, build: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        version = self._version()
        with self._lock:
            entry = self._entries.get(name)
        if entry is None or entry[0] != version:
            value = build()
            with self._lock:
                self._entries[name] = (version, value)
            entry = (version, value)
        return copy.deepcopy(entry[1])


class CatalogQueries:
    def __init__(self, *, cache: CatalogCache | None = None) -> None:
        self.cache = cache if cache is not None else CatalogCache()

    def import_specs(self, args: Mapping[str, Any] | None = None, context: Any = None) -> dict[str, Any]:
        return self.cache.get(IMPORT_SPECS_QUERY, build_import_specs)


def validate_query_data(name: str, data: Any) -> list[str]:
    """Same closed-schema and structural privacy checks as a real res frame."""
    frame = dict(t="res", epoch="catalog", produced_at_ms=0, sent_at_ms=0, id=1,
                 req_id="catalog", owner="catalog", cmd=name, ok=True, data=data)
    return protocol.validate_frame(frame)


def register(session: Any) -> CatalogQueries:
    """Integration hook: register the catalog query on a bridge session.

    The alert channel catalog (``alerts.catalog``) was retired with the
    提醒 redesign (10-30): notices are local only and have no channel forms.
    """
    queries = CatalogQueries()
    session.register_query(IMPORT_SPECS_QUERY, queries.import_specs)
    return queries
