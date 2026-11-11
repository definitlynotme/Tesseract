from __future__ import annotations

import unittest

from desktop_runtime_preferences import (
    apply_runtime_ui_preferences,
    apply_ui_preferences_to_payload,
    effective_ui_preferences_config,
    normalized_ui_preferences,
    pending_ui_preferences_payload,
    pending_ui_preferences_restore_kwargs,
    ui_preferences_apply_kwargs,
    ui_preferences_form_state,
    ui_preferences_save_plan,
    ui_preferences_write_payload,
)


class DesktopRuntimePreferencesTest(unittest.TestCase):
    def test_apply_ui_preferences_preserves_unrelated_ui_keys_and_prunes_click_sound(self) -> None:
        payload = {"ui": {"language": "en_US", "density": "compact", "click_sound": {"enabled": True}}}

        ui = apply_ui_preferences_to_payload(
            payload,
            {
                "language": "ja_JP",
                "theme": "absolutely",
                "unknown": "ignored",
            },
        )

        self.assertIs(ui, payload["ui"])
        self.assertEqual(ui["language"], "ja_JP")
        self.assertEqual(ui["theme"], "absolutely")
        self.assertEqual(ui["density"], "compact")
        self.assertNotIn("click_sound", ui)
        self.assertNotIn("unknown", ui)

    def test_apply_ui_preferences_replaces_invalid_ui_section(self) -> None:
        payload = {"ui": "invalid"}

        ui = apply_ui_preferences_to_payload(payload, {"theme": "daylight"})

        self.assertEqual(ui, {"theme": "daylight"})
        self.assertIs(ui, payload["ui"])

    def test_apply_runtime_ui_preferences_replaces_invalid_runtime_ui(self) -> None:
        config = {"ui": "invalid"}

        runtime_ui = apply_runtime_ui_preferences(config, {"language": "pt_BR"})

        self.assertEqual(runtime_ui, {"language": "pt_BR"})
        self.assertIs(runtime_ui, config["ui"])

    def test_apply_runtime_ui_preferences_preserves_runtime_ui_keys(self) -> None:
        config = {"ui": {"language": "en_US", "density": "compact"}}

        runtime_ui = apply_runtime_ui_preferences(config, {"theme": "midnight"})

        self.assertEqual(runtime_ui, {"language": "en_US", "density": "compact", "theme": "midnight"})

    def test_ui_preferences_write_payload_maps_preferences(self) -> None:
        payload = ui_preferences_write_payload({"language": "ja_JP", "theme": "absolutely"})

        self.assertEqual(payload, {"language": "ja_JP", "theme": "absolutely"})

    def test_ui_preferences_apply_kwargs_maps_preferences(self) -> None:
        kwargs = ui_preferences_apply_kwargs(
            {"language": "ja_JP", "theme": "absolutely"},
            rebuild=True,
            refresh=False,
        )

        self.assertEqual(
            kwargs,
            {
                "language": "ja_JP",
                "theme": "absolutely",
                "rebuild": True,
                "refresh": False,
            },
        )

    def test_pending_ui_preferences_restore_kwargs_uses_old_preferences_and_defaults(self) -> None:
        self.assertIsNone(
            pending_ui_preferences_restore_kwargs(
                None,
                default_language="en_US",
                default_theme="midnight",
            )
        )

        kwargs = pending_ui_preferences_restore_kwargs(
            {
                "old": {
                    "language": "en_US",
                    "theme": "midnight",
                },
                "rebuild": True,
            },
            default_language="ja_JP",
            default_theme="absolutely",
        )

        self.assertEqual(
            kwargs,
            {
                "language": "en_US",
                "theme": "midnight",
                "rebuild": True,
                "refresh": False,
            },
        )

    def test_normalized_ui_preferences_uses_supplied_normalizers(self) -> None:
        preferences = normalized_ui_preferences(
            {"ui": {"language": "ja", "theme": "absolutly", "click_sound": {"enabled": "false"}}},
            normalize_language=lambda value: {"ja": "ja_JP"}.get(value, "en_US"),
            normalize_theme=lambda value: {"absolutly": "absolutely"}.get(value, "midnight"),
            default_language="en_US",
            default_theme="midnight",
        )

        self.assertEqual(preferences, {"language": "ja_JP", "theme": "absolutely"})

    def test_normalized_ui_preferences_tolerates_invalid_ui_sections(self) -> None:
        preferences = normalized_ui_preferences(
            {"ui": "invalid"},
            normalize_language=str,
            normalize_theme=str,
            default_language="en_US",
            default_theme="midnight",
        )

        self.assertEqual(preferences, {"language": "en_US", "theme": "midnight"})

    def test_pending_ui_preferences_payload_never_requires_rebuild_for_ui_preferences(self) -> None:
        same_preferences = pending_ui_preferences_payload(
            {"language": "en_US", "theme": "midnight"},
            {"language": "en_US", "theme": "midnight"},
        )
        changed_language = pending_ui_preferences_payload(
            {"language": "en_US", "theme": "midnight"},
            {"language": "ja_JP", "theme": "midnight"},
        )
        changed_theme = pending_ui_preferences_payload(
            {"language": "en_US", "theme": "midnight"},
            {"language": "en_US", "theme": "absolutely"},
        )

        self.assertFalse(same_preferences["rebuild"])
        self.assertFalse(changed_language["rebuild"])
        self.assertFalse(changed_theme["rebuild"])
        self.assertIsNot(changed_language["old"], changed_language["new"])

    def test_ui_preferences_save_plan_groups_pending_apply_restore_and_write_payloads(self) -> None:
        old_preferences = {"language": "en_US", "theme": "midnight"}
        new_preferences = {"language": "ja_JP", "theme": "absolutely"}

        plan = ui_preferences_save_plan(old_preferences, new_preferences)

        self.assertFalse(plan.rebuild)
        self.assertEqual(plan.pending_preferences["old"], old_preferences)
        self.assertEqual(plan.pending_preferences["new"], new_preferences)
        self.assertEqual(plan.apply_kwargs["language"], "ja_JP")
        self.assertEqual(plan.apply_kwargs["rebuild"], False)
        self.assertEqual(plan.restore_kwargs["language"], "en_US")
        self.assertEqual(plan.restore_kwargs["rebuild"], False)
        self.assertEqual(plan.write_payload, {"language": "ja_JP", "theme": "absolutely"})

    def test_effective_ui_preferences_config_prefers_local_ui(self) -> None:
        merged = effective_ui_preferences_config(
            {"ui": {"language": "en_US", "theme": "midnight", "density": "compact"}},
            {"ui": {"language": "pt_BR", "theme": "daylight"}},
        )

        self.assertEqual(merged["ui"], {"language": "pt_BR", "theme": "daylight"})

    def test_effective_ui_preferences_config_applies_pending_over_stale_local(self) -> None:
        merged = effective_ui_preferences_config(
            {"ui": {"language": "en_US", "theme": "midnight"}},
            {"ui": {"language": "en_US", "theme": "midnight", "click_sound": {"enabled": True}}},
            {"new": {"language": "ja_JP", "theme": "absolutely"}},
        )

        self.assertEqual(merged["ui"], {"language": "ja_JP", "theme": "absolutely"})

    def test_effective_ui_preferences_config_tolerates_bad_sections(self) -> None:
        merged = effective_ui_preferences_config({"ui": "bad"}, {"ui": "also bad"}, {"new": {}})

        self.assertEqual(merged["ui"], {})

    def test_ui_preferences_form_state_merges_pending_and_normalizes_fields(self) -> None:
        state = ui_preferences_form_state(
            {"ui": {"language": "en_US", "theme": "midnight"}},
            {"ui": {"language": "en_US", "theme": "daylight", "click_sound": {"enabled": True}}},
            {"new": {"language": "ja", "theme": "absolutly"}},
            normalize_language=lambda value: {"ja": "ja_JP"}.get(value, str(value)),
            normalize_theme=lambda value: {"absolutly": "absolutely"}.get(value, str(value)),
            default_language="en_US",
            default_theme="midnight",
        )

        self.assertEqual(state.language, "ja_JP")
        self.assertEqual(state.theme, "absolutely")


if __name__ == "__main__":
    unittest.main()
