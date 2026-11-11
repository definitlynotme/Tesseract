from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

OVERVIEW_AUTO_EXIT_LOOKUP_KEY = "overview_auto_exit_lookup"
UI_PREFERENCE_KEYS = ("language", "theme", OVERVIEW_AUTO_EXIT_LOOKUP_KEY)
PreferenceNormalizer = Callable[[Any], str]


def overview_auto_exit_lookup_enabled(config: Any) -> bool:
    """Whether the overview may look up the exit IP without a click (default on)."""
    ui = config.get("ui") if isinstance(config, dict) else None
    value = ui.get(OVERVIEW_AUTO_EXIT_LOOKUP_KEY, True) if isinstance(ui, dict) else True
    if isinstance(value, str):
        return value.strip().casefold() not in {"0", "false", "no", "off"}
    return bool(value)


@dataclass(frozen=True)
class UiPreferenceSavePlan:
    pending_preferences: dict[str, Any]
    apply_kwargs: dict[str, Any]
    restore_kwargs: dict[str, Any]
    write_payload: dict[str, Any]
    rebuild: bool


@dataclass(frozen=True)
class UiPreferenceFormState:
    language: str
    theme: str
    overview_auto_exit_lookup: bool = True


def apply_ui_preferences_to_payload(payload: dict[str, Any], preferences: dict[str, Any]) -> dict[str, Any]:
    ui = payload.setdefault("ui", {})
    if not isinstance(ui, dict):
        ui = {}
        payload["ui"] = ui

    for key in UI_PREFERENCE_KEYS:
        if key in preferences:
            ui[key] = preferences[key]

    ui.pop("click_sound", None)

    return ui


def apply_runtime_ui_preferences(config: dict[str, Any], ui: dict[str, Any]) -> dict[str, Any]:
    runtime_ui = config.setdefault("ui", {})
    if not isinstance(runtime_ui, dict):
        runtime_ui = {}
        config["ui"] = runtime_ui
    runtime_ui.update(ui)
    return runtime_ui


def _ui_section(payload: dict[str, Any]) -> dict[str, Any]:
    ui = payload.get("ui") if isinstance(payload, dict) else {}
    return dict(ui) if isinstance(ui, dict) else {}


def ui_preferences_write_payload(preferences: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "language": preferences["language"],
        "theme": preferences["theme"],
    }
    if OVERVIEW_AUTO_EXIT_LOOKUP_KEY in preferences:
        payload[OVERVIEW_AUTO_EXIT_LOOKUP_KEY] = bool(preferences[OVERVIEW_AUTO_EXIT_LOOKUP_KEY])
    return payload


def ui_preferences_apply_kwargs(
    preferences: dict[str, Any],
    *,
    rebuild: bool,
    refresh: bool = False,
) -> dict[str, Any]:
    return {
        "language": str(preferences["language"]),
        "theme": str(preferences["theme"]),
        "rebuild": bool(rebuild),
        "refresh": bool(refresh),
    }


def pending_ui_preferences_restore_kwargs(
    pending_preferences: dict[str, Any] | None,
    *,
    default_language: str,
    default_theme: str,
    refresh: bool = False,
) -> dict[str, Any] | None:
    if not isinstance(pending_preferences, dict):
        return None
    old = pending_preferences.get("old")
    if not isinstance(old, dict):
        return None
    return {
        "language": str(old.get("language", default_language)),
        "theme": str(old.get("theme", default_theme)),
        "rebuild": bool(pending_preferences.get("rebuild", False)),
        "refresh": bool(refresh),
    }


def normalized_ui_preferences(
    config: dict[str, Any],
    *,
    normalize_language: PreferenceNormalizer,
    normalize_theme: PreferenceNormalizer,
    default_language: str,
    default_theme: str,
) -> dict[str, Any]:
    ui = config.get("ui", {}) if isinstance(config.get("ui"), dict) else {}
    return {
        "language": normalize_language(ui.get("language", default_language)),
        "theme": normalize_theme(ui.get("theme", default_theme)),
    }


def pending_ui_preferences_payload(old_preferences: dict[str, Any], new_preferences: dict[str, Any]) -> dict[str, Any]:
    return {
        "old": dict(old_preferences),
        "new": dict(new_preferences),
        "rebuild": False,
    }


def ui_preferences_save_plan(
    old_preferences: dict[str, Any],
    new_preferences: dict[str, Any],
    *,
    refresh: bool = False,
) -> UiPreferenceSavePlan:
    pending_preferences = pending_ui_preferences_payload(old_preferences, new_preferences)
    rebuild = bool(pending_preferences["rebuild"])
    return UiPreferenceSavePlan(
        pending_preferences=pending_preferences,
        apply_kwargs=ui_preferences_apply_kwargs(new_preferences, rebuild=rebuild, refresh=refresh),
        restore_kwargs=ui_preferences_apply_kwargs(old_preferences, rebuild=rebuild, refresh=refresh),
        write_payload=ui_preferences_write_payload(new_preferences),
        rebuild=rebuild,
    )


def effective_ui_preferences_config(
    runtime_config: dict[str, Any],
    local_config: dict[str, Any],
    pending_preferences: dict[str, Any] | None = None,
) -> dict[str, Any]:
    merged = dict(runtime_config if isinstance(runtime_config, dict) else {})
    local_ui = local_config.get("ui") if isinstance(local_config, dict) else None
    if isinstance(local_ui, dict):
        merged["ui"] = dict(local_ui)

    pending_new = pending_preferences.get("new") if isinstance(pending_preferences, dict) else None
    if isinstance(pending_new, dict):
        ui = _ui_section(merged)
        if "language" in pending_new:
            ui["language"] = pending_new.get("language")
        if "theme" in pending_new:
            ui["theme"] = pending_new.get("theme")
        if OVERVIEW_AUTO_EXIT_LOOKUP_KEY in pending_new:
            ui[OVERVIEW_AUTO_EXIT_LOOKUP_KEY] = pending_new.get(OVERVIEW_AUTO_EXIT_LOOKUP_KEY)
        ui.pop("click_sound", None)
        merged["ui"] = ui

    return merged


def ui_preferences_form_state(
    runtime_config: dict[str, Any],
    local_config: dict[str, Any],
    pending_preferences: dict[str, Any] | None = None,
    *,
    normalize_language: PreferenceNormalizer,
    normalize_theme: PreferenceNormalizer,
    default_language: str,
    default_theme: str,
) -> UiPreferenceFormState:
    effective = effective_ui_preferences_config(runtime_config, local_config, pending_preferences)
    preferences = normalized_ui_preferences(
        effective,
        normalize_language=normalize_language,
        normalize_theme=normalize_theme,
        default_language=default_language,
        default_theme=default_theme,
    )
    return UiPreferenceFormState(
        language=str(preferences["language"]),
        theme=str(preferences["theme"]),
        overview_auto_exit_lookup=overview_auto_exit_lookup_enabled(effective),
    )
