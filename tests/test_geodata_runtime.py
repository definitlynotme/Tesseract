import tempfile
import unittest
from pathlib import Path

from geodata_runtime import (
    CoreStartupTimeoutPlan,
    core_startup_timeout_plan,
    missing_geodata_assets,
    runtime_config_text_uses_geodata,
    runtime_config_uses_geodata,
)


class GeoDataRuntimeTests(unittest.TestCase):
    def test_runtime_config_text_detects_geosite_and_geoip_rules(self) -> None:
        self.assertTrue(runtime_config_text_uses_geodata("rules:\n- GEOSITE,category-ads-all,REJECT"))
        self.assertTrue(runtime_config_text_uses_geodata("rules:\n- geoip,us,general,no-resolve"))
        self.assertFalse(runtime_config_text_uses_geodata("rules:\n- DOMAIN-SUFFIX,example.com,DIRECT"))

    def test_runtime_config_file_detection_treats_missing_file_as_not_required(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "mihomo-runtime.yaml"

            self.assertFalse(runtime_config_uses_geodata(path))

            path.write_text("rules:\n- GEOIP,US,GENERAL,no-resolve\n", encoding="utf-8")
            self.assertTrue(runtime_config_uses_geodata(path))

    def test_missing_geodata_assets_accepts_packaged_and_downloaded_names(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            work_dir = Path(temp_dir)

            self.assertEqual(missing_geodata_assets(work_dir), ["geoip", "geosite"])

            (work_dir / "GeoIP.dat").write_bytes(b"geoip")
            self.assertEqual(missing_geodata_assets(work_dir), ["geosite"])

            (work_dir / "geosite.dat").write_bytes(b"geosite")
            self.assertEqual(missing_geodata_assets(work_dir), [])

    def test_missing_geodata_assets_accepts_injected_asset_filenames(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            work_dir = Path(temp_dir)
            asset_filenames = {"geoip": "custom-geoip.dat", "geosite": "custom-geosite.dat"}

            (work_dir / "custom-geoip.dat").write_bytes(b"geoip")
            self.assertEqual(missing_geodata_assets(work_dir, asset_filenames=asset_filenames), ["geosite"])

            (work_dir / "custom-geosite.dat").write_bytes(b"geosite")
            self.assertEqual(missing_geodata_assets(work_dir, asset_filenames=asset_filenames), [])

    def test_core_startup_timeout_keeps_base_without_geodata_or_missing_assets(self) -> None:
        self.assertEqual(
            core_startup_timeout_plan(
                {"startup_timeout": 5},
                uses_geodata=False,
                missing_assets=["geoip"],
                geo_config={"request_timeout": 10},
            ),
            CoreStartupTimeoutPlan(seconds=5.0, base_seconds=5.0, extended=False),
        )
        self.assertEqual(
            core_startup_timeout_plan(
                {"startup_timeout": 5},
                uses_geodata=True,
                missing_assets=[],
                geo_config={"request_timeout": 10},
            ),
            CoreStartupTimeoutPlan(seconds=5.0, base_seconds=5.0, extended=False),
        )

    def test_core_startup_timeout_extends_for_missing_geodata_and_caps_result(self) -> None:
        self.assertEqual(
            core_startup_timeout_plan(
                {"startup_timeout": 5},
                uses_geodata=True,
                missing_assets=["geoip", "geosite"],
                geo_config={"request_timeout": 10},
            ),
            CoreStartupTimeoutPlan(seconds=35.0, base_seconds=5.0, extended=True),
        )
        self.assertEqual(
            core_startup_timeout_plan(
                {"startup_timeout": 170},
                uses_geodata=True,
                missing_assets=["geoip", "geosite"],
                geo_config={"request_timeout": 20},
            ),
            CoreStartupTimeoutPlan(seconds=180.0, base_seconds=170.0, extended=True),
        )

    def test_core_startup_timeout_preserves_fallback_and_minimum(self) -> None:
        self.assertEqual(
            core_startup_timeout_plan(
                {"startup_timeout": "bad"},
                uses_geodata=False,
                missing_assets=[],
                geo_config={},
            ),
            CoreStartupTimeoutPlan(seconds=30.0, base_seconds=30.0, extended=False),
        )
        self.assertEqual(
            core_startup_timeout_plan(
                {"startup_timeout": 0},
                uses_geodata=False,
                missing_assets=[],
                geo_config={},
            ),
            CoreStartupTimeoutPlan(seconds=0.1, base_seconds=0.0, extended=False),
        )


if __name__ == "__main__":
    unittest.main()
