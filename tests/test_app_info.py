from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app_info
from app_update_signing import decode_public_key, public_key_id


class AppInfoTests(unittest.TestCase):
    def test_source_release_contract_is_stable(self) -> None:
        self.assertEqual(app_info.APP_RELEASE_BASE_VERSION, "0.1.0")
        self.assertIsNone(app_info.APP_RELEASE_CANDIDATE_NUMBER)
        self.assertEqual(app_info.DEFAULT_APP_VERSION, "0.1.0")
        self.assertEqual(
            app_info.DEFAULT_RELEASE_VERSION_CONTRACT,
            {
                "version": "0.1.0",
                "base_version": "0.1.0",
                "is_prerelease": False,
                "rc_number": None,
                "windows_version_numeric": "0.1.0.65535",
                "windows_version_tuple": [0, 1, 0, 65535],
            },
        )

    def test_stable_windows_version_orders_after_every_permitted_rc(self) -> None:
        stable = app_info.release_version_contract("0.1.0")
        last_rc = app_info.release_version_contract("0.1.0-rc.65534")

        self.assertEqual(stable["windows_version_numeric"], "0.1.0.65535")
        self.assertEqual(last_rc["windows_version_numeric"], "0.1.0.65534")

    def test_release_version_contract_rejects_ambiguous_or_unrepresentable_versions(
        self,
    ) -> None:
        for version in (
            "v0.1.0",
            "0.1",
            "00.1.0",
            "0.1.0-beta.1",
            "0.1.0-rc.65535",
            "65536.0.0",
        ):
            with self.subTest(version=version):
                with self.assertRaises(ValueError):
                    app_info.release_version_contract(version)

    def test_bootstrap_update_keyring_embeds_the_release_public_key(self) -> None:
        self.assertTrue(app_info.APP_UPDATE_ENABLED)
        self.assertEqual(
            app_info.APP_UPDATE_SIGNING_PUBLIC_KEYS,
            (app_info.APP_UPDATE_SIGNING_PUBLIC_KEY,),
        )
        self.assertEqual(
            public_key_id(
                decode_public_key(app_info.APP_UPDATE_SIGNING_PUBLIC_KEY)
            ),
            "sha256:c7b50d82e80cf8ebb59ff9c9a84f743404ec04b4ab2626e5622cae7bf14ecb65",
        )

    def test_load_app_version_uses_default_when_build_version_file_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("app_info._app_version_resource_root", return_value=Path(temp_dir)):
                self.assertEqual(app_info._load_app_version(), app_info.DEFAULT_APP_VERSION)

    def test_load_app_version_uses_build_version_file_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "app_version.txt").write_text("0.2.0\n", encoding="ascii")
            with patch("app_info._app_version_resource_root", return_value=Path(temp_dir)):
                self.assertEqual(app_info._load_app_version(), "0.2.0")


if __name__ == "__main__":
    unittest.main()
