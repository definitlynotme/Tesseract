import tempfile
import unittest
from pathlib import Path

from geodata_runtime import (
    CoreStartupTimeoutPlan,
    bundled_geodata_dir,
    core_startup_timeout_plan,
    missing_geodata_assets,
    seed_bundled_geodata,
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

    def test_seed_bundled_geodata_fills_every_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bundle = bundled_geodata_dir(root / "resources")
            bundle.mkdir(parents=True)
            (bundle / "geoip.dat").write_bytes(b"bundled-geoip")
            (bundle / "geosite.dat").write_bytes(b"bundled-geosite")
            work_dir = root / "data" / "mihomo"

            self.assertEqual(seed_bundled_geodata(work_dir, bundle), ["geoip", "geosite"])

            self.assertEqual((work_dir / "geoip.dat").read_bytes(), b"bundled-geoip")
            self.assertEqual((work_dir / "geosite.dat").read_bytes(), b"bundled-geosite")
            self.assertEqual(missing_geodata_assets(work_dir), [])
            self.assertEqual(sorted(path.name for path in work_dir.iterdir()), ["geoip.dat", "geosite.dat"])
            # Nothing left to fill on the next call.
            self.assertEqual(seed_bundled_geodata(work_dir, bundle), [])

    def test_seed_bundled_geodata_never_replaces_existing_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bundle = root / "bundle"
            bundle.mkdir()
            (bundle / "geoip.dat").write_bytes(b"bundled-geoip")
            (bundle / "geosite.dat").write_bytes(b"bundled-geosite")
            work_dir = root / "work"
            work_dir.mkdir()
            (work_dir / "GeoIP.dat").write_bytes(b"refreshed-geoip")

            self.assertEqual(seed_bundled_geodata(work_dir, bundle), ["geosite"])

            self.assertEqual((work_dir / "GeoIP.dat").read_bytes(), b"refreshed-geoip")
            self.assertEqual((work_dir / "geosite.dat").read_bytes(), b"bundled-geosite")

    def test_seed_bundled_geodata_uses_injected_target_names(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bundle = root / "bundle"
            bundle.mkdir()
            (bundle / "geoip.dat").write_bytes(b"bundled-geoip")
            (bundle / "geosite.dat").write_bytes(b"bundled-geosite")
            work_dir = root / "work"
            names = {"geoip": "custom-geoip.dat", "geosite": "custom-geosite.dat"}

            self.assertEqual(
                seed_bundled_geodata(work_dir, bundle, asset_filenames=names),
                ["geoip", "geosite"],
            )
            self.assertEqual((work_dir / "custom-geoip.dat").read_bytes(), b"bundled-geoip")
            self.assertEqual(missing_geodata_assets(work_dir, asset_filenames=names), [])

    def test_seed_bundled_geodata_without_bundle_leaves_files_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bundle = root / "bundle"
            bundle.mkdir()
            (bundle / "geosite.dat").write_bytes(b"bundled-geosite")
            work_dir = root / "work"

            self.assertEqual(seed_bundled_geodata(work_dir, root / "absent"), [])
            self.assertFalse(work_dir.exists())
            self.assertEqual(seed_bundled_geodata(work_dir, bundle), ["geosite"])
            self.assertEqual(missing_geodata_assets(work_dir), ["geoip"])

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
