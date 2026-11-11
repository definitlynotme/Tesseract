from __future__ import annotations

import unittest

from ui_theme import (
    DEFAULT_UI_THEME,
    UI_THEME_OPTIONS,
    THEME_STYLES,
    normalize_ui_theme,
    theme_palette_colors,
    theme_style_tokens,
    themed_accent,
)


def _relative_luminance(color: str) -> float:
    channels = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4 for channel in channels]
    return (0.2126 * linear[0]) + (0.7152 * linear[1]) + (0.0722 * linear[2])


def _contrast_ratio(first: str, second: str) -> float:
    lighter, darker = sorted((_relative_luminance(first), _relative_luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


class UiThemeTests(unittest.TestCase):
    def test_normalizes_known_aliases_and_unknown_theme(self) -> None:
        self.assertEqual(normalize_ui_theme("absolutly"), "absolutely")
        self.assertEqual(normalize_ui_theme("classic"), "midnight")
        self.assertEqual(normalize_ui_theme("light"), "daylight")
        self.assertEqual(normalize_ui_theme("dawn"), "daylight")
        self.assertEqual(normalize_ui_theme("harbor"), "daylight")
        self.assertEqual(normalize_ui_theme("unknown"), DEFAULT_UI_THEME)

    def test_theme_options_only_expose_daylight_midnight_and_absolutely(self) -> None:
        self.assertEqual(DEFAULT_UI_THEME, "daylight")
        self.assertEqual(tuple(UI_THEME_OPTIONS), ("daylight", "midnight", "absolutely"))
        self.assertEqual(UI_THEME_OPTIONS["midnight"], "午夜")
        self.assertEqual(UI_THEME_OPTIONS["daylight"], "白色")
        self.assertEqual(UI_THEME_OPTIONS["absolutely"], "暖色")
        self.assertEqual(set(THEME_STYLES), set(UI_THEME_OPTIONS))
        token_key_sets = {tuple(sorted(tokens)) for tokens in THEME_STYLES.values()}
        self.assertEqual(len(token_key_sets), 1)

    def test_theme_tokens_and_palette_fall_back_to_default(self) -> None:
        self.assertEqual(theme_style_tokens("unknown"), theme_style_tokens(DEFAULT_UI_THEME))
        self.assertEqual(theme_palette_colors("unknown"), theme_palette_colors(DEFAULT_UI_THEME))
        self.assertEqual(theme_style_tokens("absolutely")["health_text"], "#8a5a2f")

    def test_absolutely_theme_maps_cool_accents_to_local_palette(self) -> None:
        self.assertEqual(themed_accent("absolutely", "#4fd2a0"), "#8a5a2f")
        self.assertEqual(themed_accent("absolutely", "#42bced"), theme_style_tokens("absolutely")["accent"])
        self.assertEqual(themed_accent("midnight", "#42bced"), "#42bced")

    def test_daylight_theme_maps_dark_theme_accents_to_readable_colors(self) -> None:
        tokens = theme_style_tokens("daylight")

        self.assertEqual(themed_accent("daylight", "#d6e2ee"), tokens["text"])
        self.assertEqual(themed_accent("daylight", "#42bced"), tokens["accent"])
        self.assertEqual(themed_accent("daylight", "#9a8cff"), tokens["secondary_accent"])
        self.assertEqual(themed_accent("daylight", "#4fd2a0"), tokens["health_text"])
        self.assertEqual(tokens["bg"], "#ffffff")
        self.assertEqual(tokens["surface"], "#ffffff")
        self.assertEqual(tokens["accent"], "#175cd3")
        self.assertEqual(tokens["secondary_accent"], "#087e6b")
        self.assertEqual(tokens["selected"], "#edf4ff")
        self.assertEqual(tokens["sidebar"], "#ffffff")
        self.assertNotEqual(tokens["primary_bg"], "#000000")

    def test_daylight_keeps_a_white_base_with_clear_neutral_hierarchy(self) -> None:
        tokens = theme_style_tokens("daylight")
        palette = theme_palette_colors("daylight")

        for key in ("bg", "surface", "input", "sidebar"):
            with self.subTest(key=key):
                self.assertEqual(tokens[key], "#ffffff")

        self.assertEqual(palette["alternate"], tokens["surface_alt"])
        self.assertEqual(palette["button"], tokens["surface"])
        self.assertNotEqual(tokens["surface_alt"], tokens["surface"])
        self.assertNotEqual(tokens["panel"], tokens["surface"])
        self.assertNotEqual(tokens["border"], tokens["surface"])
        self.assertNotEqual(tokens["divider"], tokens["border"])
        self.assertNotEqual(tokens["border_strong"], tokens["border"])
        self.assertNotEqual(tokens["accent_border"], tokens["primary_border"])

        for key in ("title", "text", "muted", "faint", "accent", "health_text", "danger_text", "log_level_warning"):
            with self.subTest(key=key):
                self.assertGreaterEqual(_contrast_ratio(tokens[key], tokens["surface"]), 4.5)

        self.assertGreaterEqual(_contrast_ratio(tokens["primary_text"], tokens["primary_bg"]), 4.5)

    def test_daylight_protocol_chart_accents_remain_distinct(self) -> None:
        source_colors = ("#42bced", "#9a8cff", "#4fd2a0", "#f2b45e", "#e86b70", "#6fa6ff")
        mapped = [themed_accent("daylight", color) for color in source_colors]

        self.assertEqual(len(set(mapped)), len(source_colors))

    def test_selection_fix_keeps_existing_theme_colors(self) -> None:
        expected = {
            "midnight": ("#245c7d", "#ecfeff", "#17243a"),
            "daylight": ("#edf4ff", "#15171a", "#edf4ff"),
            "absolutely": ("#efd0bb", "#2a211b", "#efd2bf"),
        }

        for theme in THEME_STYLES:
            with self.subTest(theme=theme):
                tokens = theme_style_tokens(theme)
                palette = theme_palette_colors(theme)
                expected_highlight, expected_highlighted_text, expected_selected = expected[theme]

                self.assertNotIn("selected_text", tokens)
                self.assertEqual(palette["highlight"], expected_highlight)
                self.assertEqual(palette["highlighted_text"], expected_highlighted_text)
                self.assertEqual(tokens["selected"], expected_selected)

    def test_chart_background_blends_with_theme_canvas(self) -> None:
        for theme in THEME_STYLES:
            tokens = theme_style_tokens(theme)
            with self.subTest(theme=theme):
                expected = tokens["surface"] if theme == "daylight" else tokens["surface_alt"]
                self.assertEqual(tokens["chart_bg"], expected)


if __name__ == "__main__":
    unittest.main()
