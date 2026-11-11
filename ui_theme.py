from __future__ import annotations

from typing import Any


DEFAULT_UI_THEME = "daylight"

# Shared surface geometry. Keeping the paint overlays and the final QSS on one
# scale prevents the antialiased inner fill from cutting across a card border
# at the corners.
UI_BORDER_WIDTH = 1
UI_RADIUS_SHELL = 10
UI_RADIUS_FLUSH = UI_RADIUS_SHELL - UI_BORDER_WIDTH
UI_RADIUS_CARD = 8
UI_RADIUS_ROW = 6
WORDMARK_FONT_FAMILY = (
    '"Segoe UI Variable Display", "Segoe UI", "Microsoft YaHei UI", '
    '"Microsoft YaHei", sans-serif'
)

UI_THEME_OPTIONS = {
    "daylight": "白色",
    "midnight": "午夜",
    "absolutely": "暖色",
}
UI_THEME_ALIASES = {
    "classic": "midnight",
    "dark": "midnight",
    "night": "midnight",
    "white": "daylight",
    "light": "daylight",
    "day": "daylight",
    "daytime": "daylight",
    "clean": "daylight",
    "dawn": "daylight",
    "blue": "daylight",
    "focus": "daylight",
    "harbor": "daylight",
    "absolutly": "absolutely",
}
THEME_PALETTES = {
    "midnight": {
        "window": "#080b10",
        "window_text": "#d6e2ee",
        "base": "#090e14",
        "alternate": "#0d141d",
        "tooltip": "#111824",
        "text": "#d6e2ee",
        "button": "#111925",
        "button_text": "#d6e2ee",
        "link": "#42bced",
        "highlight": "#245c7d",
        "highlighted_text": "#ecfeff",
        "disabled": "#58697c",
    },
    "daylight": {
        "window": "#ffffff",
        "window_text": "#252a32",
        "base": "#ffffff",
        "alternate": "#f8f9fb",
        "tooltip": "#ffffff",
        "text": "#252a32",
        "button": "#ffffff",
        "button_text": "#252a32",
        "link": "#175cd3",
        "highlight": "#edf4ff",
        "highlighted_text": "#15171a",
        "disabled": "#66717f",
    },
    "absolutely": {
        "window": "#f7f1e7",
        "window_text": "#332d26",
        "base": "#fffaf2",
        "alternate": "#f2e9dd",
        "tooltip": "#fffaf2",
        "text": "#332d26",
        "button": "#efe4d5",
        "button_text": "#332d26",
        "link": "#b75d3d",
        "highlight": "#efd0bb",
        "highlighted_text": "#2a211b",
        "disabled": "#9a8f83",
    },
}
THEME_STYLES = {
    "midnight": {
        "bg": "#080b10",
        "surface": "#0d1219",
        "surface_alt": "#0d141d",
        "panel": "#0a1017",
        "border": "#1a2433",
        "divider": "#141d29",
        "border_strong": "#243247",
        "text": "#c8d8e8",
        "title": "#e7f1fb",
        "muted": "#60778f",
        "faint": "#526984",
        "brand": "#d9e8f6",
        "accent": "#42bced",
        "secondary_accent": "#9a8cff",
        "accent_soft": "#0d1722",
        "accent_border": "#2f6c98",
        "primary_bg": "#10283c",
        "primary_hover": "#143650",
        "primary_border": "#2f6c98",
        "primary_text": "#cdeaff",
        "input": "#090d13",
        "selected": "#17243a",
        "sidebar": "#0b1016",
        "nav_active": "#171d34",
        "nav_hover": "#111925",
        "health_bg": "#10221b",
        "health_border": "#214234",
        "health_text": "#4fd2a0",
        "danger_bg": "#251016",
        "danger_border": "#6a2b35",
        "danger_text": "#f1b8bf",
        "scroll": "#243247",
        "chart_bg": "#0d141d",
        "chart_grid": "#192334",
        "chart_track": "#17202c",
        "icon": "#8aa7c1",
        "nav_icon": "#6d88a5",
        "nav_icon_active": "#a8cef0",
        "primary_icon": "#cdeaff",
        "danger_icon": "#f1b8bf",
        "log_bg": "#05080c",
        "log_border": "#1a2433",
        "log_text": "#d7e7f5",
        "log_level_debug": "#60778f",
        "log_level_info": "#42bced",
        "log_level_warning": "#f2c84b",
        "log_level_error": "#f1b8bf",
        "font_family": "\"Microsoft YaHei UI\", \"Microsoft YaHei\", \"Segoe UI\", \"Arial\", sans-serif",
        "brand_font_family": "\"Microsoft YaHei UI\", \"Microsoft YaHei\", \"Segoe UI\", \"Arial\", sans-serif",
    },
    "daylight": {
        "bg": "#ffffff",
        "surface": "#ffffff",
        "surface_alt": "#f8f9fb",
        "panel": "#f4f6f8",
        "border": "#bbc3cd",
        "divider": "#dde2e8",
        "border_strong": "#8994a3",
        "text": "#252a32",
        "title": "#15171a",
        "muted": "#4f5b69",
        "faint": "#66717f",
        "brand": "#15171a",
        "accent": "#175cd3",
        "secondary_accent": "#087e6b",
        "accent_soft": "#f5f8ff",
        "accent_border": "#8fb6ee",
        "primary_bg": "#175cd3",
        "primary_hover": "#134ea8",
        "primary_border": "#175cd3",
        "primary_text": "#ffffff",
        "input": "#ffffff",
        "selected": "#edf4ff",
        "sidebar": "#ffffff",
        "nav_active": "#ffffff",
        "nav_hover": "#f8f9fb",
        "health_bg": "#f1f8f4",
        "health_border": "#92c7a6",
        "health_text": "#12704a",
        "danger_bg": "#fff2f0",
        "danger_border": "#e9a29c",
        "danger_text": "#b42318",
        "scroll": "#9aa4b2",
        "chart_bg": "#ffffff",
        "chart_grid": "#dde2e8",
        "chart_track": "#eef1f4",
        "icon": "#4f5b69",
        "nav_icon": "#596579",
        "nav_icon_active": "#175cd3",
        "primary_icon": "#ffffff",
        "danger_icon": "#b42318",
        "log_bg": "#ffffff",
        "log_border": "#bbc3cd",
        "log_text": "#252a32",
        "log_level_debug": "#4f5b69",
        "log_level_info": "#175cd3",
        "log_level_warning": "#8a5800",
        "log_level_error": "#b42318",
        "font_family": "\"Microsoft YaHei UI\", \"Microsoft YaHei\", \"Segoe UI\", \"Arial\", sans-serif",
        "brand_font_family": "\"Microsoft YaHei UI\", \"Microsoft YaHei\", \"Segoe UI\", \"Arial\", sans-serif",
    },
    "absolutely": {
        "bg": "#f7f2e8",
        "surface": "#fffaf1",
        "surface_alt": "#f4eadc",
        "panel": "#f1e4d2",
        "border": "#ddcdb9",
        "divider": "#eadfd5",
        "border_strong": "#c9b59d",
        "text": "#332b23",
        "title": "#1f1a15",
        "muted": "#74695d",
        "faint": "#918377",
        "brand": "#2d241e",
        "accent": "#d97757",
        "secondary_accent": "#8f6a5c",
        "accent_soft": "#f2d8c7",
        "accent_border": "#c9785e",
        "primary_bg": "#efe0d2",
        "primary_hover": "#e5c8b4",
        "primary_border": "#c9785e",
        "primary_text": "#4b2d22",
        "input": "#fffdf8",
        "selected": "#efd2bf",
        "sidebar": "#efe5d6",
        "nav_active": "#e8ded2",
        "nav_hover": "#f5eadf",
        "health_bg": "#efe3cf",
        "health_border": "#d5b98e",
        "health_text": "#8a5a2f",
        "danger_bg": "#f8ded8",
        "danger_border": "#d99588",
        "danger_text": "#7d332c",
        "scroll": "#cbb8a2",
        "chart_bg": "#f4eadc",
        "chart_grid": "#e4d6c5",
        "chart_track": "#eadfce",
        "icon": "#7d5f50",
        "nav_icon": "#8b7a6d",
        "nav_icon_active": "#9b4d35",
        "primary_icon": "#9b4d35",
        "danger_icon": "#7d332c",
        "log_bg": "#f2e9dd",
        "log_border": "#ddcdb9",
        "log_text": "#5a4e40",
        "log_level_debug": "#74695d",
        "log_level_info": "#9b4d35",
        "log_level_warning": "#8a5a2f",
        "log_level_error": "#7d332c",
        "font_family": "\"Microsoft YaHei UI\", \"Microsoft YaHei\", \"Segoe UI\", \"Arial\", sans-serif",
        "brand_font_family": "\"Segoe UI Variable Display\", \"Segoe UI\", \"Microsoft YaHei UI\", \"Microsoft YaHei\", \"Arial\", sans-serif",
    },
}

THEME_CHART_PALETTES = {
    "midnight": (
        "#42bced",
        "#9a8cff",
        "#4fd2a0",
        "#f2b45e",
        "#e86b70",
        "#6fa6ff",
    ),
    "daylight": (
        "#175cd3",
        "#087e6b",
        "#12704a",
        "#8a5800",
        "#b42318",
        "#6d5bd0",
    ),
    "absolutely": (
        "#d97757",
        "#8f6a5c",
        "#6f7f42",
        "#b58132",
        "#9a4e62",
        "#5f6f9b",
    ),
}

def normalize_ui_theme(value: Any) -> str:
    key = str(value or DEFAULT_UI_THEME).strip().lower()
    normalized = UI_THEME_ALIASES.get(key, key)
    return normalized if normalized in UI_THEME_OPTIONS else DEFAULT_UI_THEME


def theme_palette_colors(theme: str) -> dict[str, str]:
    return THEME_PALETTES.get(normalize_ui_theme(theme), THEME_PALETTES[DEFAULT_UI_THEME])


def theme_style_tokens(theme: Any) -> dict[str, str]:
    normalized = normalize_ui_theme(theme)
    return THEME_STYLES.get(normalized, THEME_STYLES[DEFAULT_UI_THEME])


def theme_chart_palette(theme: Any) -> tuple[str, ...]:
    normalized = normalize_ui_theme(theme)
    return THEME_CHART_PALETTES.get(
        normalized,
        THEME_CHART_PALETTES[DEFAULT_UI_THEME],
    )


def themed_accent(theme: str, accent: str) -> str:
    normalized = normalize_ui_theme(theme)
    if normalized == "daylight":
        tokens = theme_style_tokens(theme)
        return {
            "#42bced": tokens["accent"],
            "#9a8cff": tokens["secondary_accent"],
            "#4fd2a0": tokens["health_text"],
            "#f2c84b": tokens["log_level_warning"],
            "#e86b70": tokens["danger_text"],
            "#f2b45e": tokens["log_level_warning"],
            "#6fa6ff": "#6d5bd0",
            "#7ea6c7": tokens["muted"],
            "#6d88a5": tokens["nav_icon"],
            "#8aa7c1": tokens["icon"],
            "#d6e2ee": tokens["text"],
        }.get(accent.lower(), accent)
    if normalized != "absolutely":
        return accent
    tokens = theme_style_tokens(theme)
    return {
        "#42bced": tokens["accent"],
        "#9a8cff": tokens["secondary_accent"],
        "#4fd2a0": tokens["health_text"],
        "#f2c84b": "#b58132",
        "#e86b70": tokens["danger_text"],
        "#6fa6ff": tokens["accent"],
        "#7ea6c7": tokens["accent"],
        "#6d88a5": tokens["nav_icon"],
        "#8aa7c1": tokens["icon"],
        "#d6e2ee": tokens["text"],
    }.get(accent.lower(), accent)


