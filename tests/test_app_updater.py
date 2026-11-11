from __future__ import annotations

import base64
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app_update_signing import create_signature_document
from app_updater import (
    AppUpdateError,
    AppUpdateInfo,
    DownloadedUpdate,
    ReleaseAsset,
    check_latest_update,
    checksum_from_sums_text,
    compare_versions,
    download_update,
    extract_app_release_notes,
    launch_update_installer,
    update_info_from_release,
)


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.offset = 0

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            chunk = self.body[self.offset :]
            self.offset = len(self.body)
            return chunk
        chunk = self.body[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk


class FakeOpener:
    def __init__(self, responses: dict[str, bytes]) -> None:
        self.responses = responses
        self.urls: list[str] = []

    def __call__(self, request, timeout: float = 0) -> FakeResponse:  # type: ignore[no-untyped-def]
        url = request.full_url
        self.urls.append(url)
        return FakeResponse(self.responses[url])


class AppUpdaterTests(unittest.TestCase):
    @staticmethod
    def _sign_manifest(manifest: bytes) -> tuple[str, bytes]:
        private_key = Ed25519PrivateKey.generate()
        public_key = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return base64.b64encode(public_key).decode("ascii"), create_signature_document(manifest, private_key)

    def test_compare_versions_uses_numeric_order(self) -> None:
        self.assertGreater(compare_versions("0.10.0", "0.9.0"), 0)
        self.assertEqual(compare_versions("v1.2.0", "1.2"), 0)
        self.assertLess(compare_versions("1.2.0", "1.2.1"), 0)
        self.assertGreater(compare_versions("1.2.3", "1.2.3-rc.1"), 0)
        self.assertLess(compare_versions("1.2.3-rc.1", "1.2.3"), 0)
        self.assertGreater(compare_versions("1.2.3-rc.10", "1.2.3-rc.2"), 0)

    def test_update_info_selects_installable_newer_release(self) -> None:
        info = update_info_from_release(
            {
                "tag_name": "v0.2.0",
                "html_url": "https://example.test/releases/v0.2.0",
                "assets": [
                    {
                        "name": "Tesseract-Setup-0.2.0-win-x64.exe",
                        "browser_download_url": "https://example.test/setup.exe",
                        "size": 123,
                    },
                    {
                        "name": "SHA256SUMS.txt",
                        "browser_download_url": "https://example.test/SHA256SUMS.txt",
                    },
                    {
                        "name": "SHA256SUMS.txt.sig",
                        "browser_download_url": "https://example.test/SHA256SUMS.txt.sig",
                    },
                ],
            },
            current_version="0.1.0",
        )

        self.assertTrue(info.update_available)
        self.assertTrue(info.installable)
        self.assertEqual(info.latest_version, "0.2.0")
        self.assertEqual(info.installer.name if info.installer else "", "Tesseract-Setup-0.2.0-win-x64.exe")

    def test_update_info_marks_current_version_as_up_to_date(self) -> None:
        info = update_info_from_release({"tag_name": "v0.1.0", "assets": []}, current_version="0.1.0")

        self.assertFalse(info.update_available)
        self.assertFalse(info.installable)

    def test_update_info_exposes_only_marked_app_release_notes(self) -> None:
        body = """GitHub release introduction.

<!-- tesseract-app-notes:start -->
## What's Changed

- Faster startup
- Fixed DNS routing
<!-- tesseract-app-notes:end -->

## Installation and signature verification

Windows may show an Unknown Publisher warning. Verify the Ed25519 signature.
"""

        info = update_info_from_release(
            {"tag_name": "v0.2.0", "body": body, "assets": []},
            current_version="0.1.0",
        )

        self.assertEqual(
            info.release_notes,
            "## What's Changed\n\n- Faster startup\n- Fixed DNS routing",
        )
        self.assertNotIn("Unknown Publisher", info.release_notes)
        self.assertNotIn("Ed25519", info.release_notes)

    def test_release_notes_without_markers_keep_legacy_body(self) -> None:
        legacy_body = " Legacy release notes.\n\nOlder release details.\n"

        self.assertEqual(extract_app_release_notes(legacy_body), legacy_body)

    def test_malformed_release_note_markers_fail_closed(self) -> None:
        start = "<!-- tesseract-app-notes:start -->"
        end = "<!-- tesseract-app-notes:end -->"
        safety = "Unknown Publisher and Ed25519 verification details"
        malformed_bodies = (
            f"{start}\nChanges\n{safety}",
            f"Changes\n{end}\n{safety}",
            f"{end}\nChanges\n{start}\n{safety}",
            f"{start}\nChanges\n{start}\n{end}\n{safety}",
            f"{start}\nChanges\n{end}\n{end}\n{safety}",
        )

        for body in malformed_bodies:
            with self.subTest(body=body):
                self.assertEqual(extract_app_release_notes(body), "")

    def test_check_latest_update_reads_github_release_payload(self) -> None:
        opener = FakeOpener(
            {
                "https://api.example/latest": (
                    b'{"tag_name":"v0.2.0","assets":[{"name":"Tesseract-Setup-0.2.0-win-x64.exe",'
                    b'"browser_download_url":"https://example.test/setup.exe"}]}'
                )
            }
        )

        info = check_latest_update(current_version="0.1.0", api_url="https://api.example/latest", opener=opener)

        self.assertTrue(info.update_available)
        self.assertEqual(opener.urls, ["https://api.example/latest"])

    def test_checksum_from_sums_text_matches_asset_name(self) -> None:
        digest = "a" * 64

        self.assertEqual(
            checksum_from_sums_text(f"{digest}  Tesseract-Setup-0.2.0-win-x64.exe\n", "Tesseract-Setup-0.2.0-win-x64.exe"),
            digest,
        )

    def test_download_update_verifies_installer_hash(self) -> None:
        installer_body = b"installer"
        digest = hashlib.sha256(installer_body).hexdigest()
        checksum_body = f"{digest}  Tesseract-Setup-0.2.0-win-x64.exe\n".encode("utf-8")
        public_key, signature_body = self._sign_manifest(checksum_body)
        update = AppUpdateInfo(
            current_version="0.1.0",
            latest_version="0.2.0",
            release_url="https://example.test/release",
            release_notes="",
            installer=ReleaseAsset("Tesseract-Setup-0.2.0-win-x64.exe", "https://example.test/setup.exe"),
            checksum=ReleaseAsset("SHA256SUMS.txt", "https://example.test/SHA256SUMS.txt"),
            update_available=True,
            checksum_signature=ReleaseAsset(
                "SHA256SUMS.txt.sig", "https://example.test/SHA256SUMS.txt.sig"
            ),
        )
        opener = FakeOpener(
            {
                "https://example.test/setup.exe": installer_body,
                "https://example.test/SHA256SUMS.txt": checksum_body,
                "https://example.test/SHA256SUMS.txt.sig": signature_body,
            }
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            downloaded = download_update(update, Path(temp_dir), opener=opener, public_key=public_key)

            self.assertEqual(downloaded.sha256, digest)
            self.assertEqual(downloaded.installer_path.read_bytes(), installer_body)
            self.assertTrue(downloaded.manifest_verified)
            self.assertTrue(downloaded.manifest_key_id.startswith("sha256:"))
            self.assertEqual(
                opener.urls,
                [
                    "https://example.test/SHA256SUMS.txt.sig",
                    "https://example.test/SHA256SUMS.txt",
                    "https://example.test/setup.exe",
                ],
            )

    def test_download_update_does_not_fetch_installer_before_manifest_verifies(self) -> None:
        installer_body = b"installer"
        digest = hashlib.sha256(installer_body).hexdigest()
        checksum_body = f"{digest}  Tesseract-Setup-0.2.0-win-x64.exe\n".encode("utf-8")
        public_key, signature_body = self._sign_manifest(checksum_body)
        tampered_checksum = checksum_body.replace(digest.encode("ascii"), b"0" * 64)
        update = AppUpdateInfo(
            current_version="0.1.0",
            latest_version="0.2.0",
            release_url="https://example.test/release",
            release_notes="",
            installer=ReleaseAsset("Tesseract-Setup-0.2.0-win-x64.exe", "https://example.test/setup.exe"),
            checksum=ReleaseAsset("SHA256SUMS.txt", "https://example.test/SHA256SUMS.txt"),
            update_available=True,
            checksum_signature=ReleaseAsset(
                "SHA256SUMS.txt.sig", "https://example.test/SHA256SUMS.txt.sig"
            ),
        )
        opener = FakeOpener(
            {
                "https://example.test/SHA256SUMS.txt.sig": signature_body,
                "https://example.test/SHA256SUMS.txt": tampered_checksum,
            }
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(AppUpdateError, "signature verification failed"):
                download_update(update, Path(temp_dir), opener=opener, public_key=public_key)

        self.assertEqual(
            opener.urls,
            ["https://example.test/SHA256SUMS.txt.sig", "https://example.test/SHA256SUMS.txt"],
        )

    def test_update_info_rejects_prerelease_draft_and_unstable_tag(self) -> None:
        for release in (
            {"tag_name": "v0.2.0", "prerelease": True},
            {"tag_name": "v0.2.0", "draft": True},
            {"tag_name": "v0.2.0-rc.1"},
            {"tag_name": "nightly"},
        ):
            with self.subTest(release=release):
                with self.assertRaises(AppUpdateError):
                    update_info_from_release(release, current_version="0.1.0")

    def test_update_info_requires_all_three_assets_to_be_installable(self) -> None:
        info = update_info_from_release(
            {
                "tag_name": "v0.2.0",
                "assets": [
                    {
                        "name": "Tesseract-Setup-0.2.0-win-x64.exe",
                        "browser_download_url": "https://example.test/setup.exe",
                    },
                    {
                        "name": "SHA256SUMS.txt",
                        "browser_download_url": "https://example.test/SHA256SUMS.txt",
                    },
                ],
            },
            current_version="0.1.0",
        )

        self.assertTrue(info.update_available)
        self.assertFalse(info.installable)

    def test_download_update_refuses_missing_checksum_asset(self) -> None:
        update = AppUpdateInfo(
            current_version="0.1.0",
            latest_version="0.2.0",
            release_url="https://example.test/release",
            release_notes="",
            installer=ReleaseAsset("Tesseract-Setup-0.2.0-win-x64.exe", "https://example.test/setup.exe"),
            checksum=None,
            update_available=True,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(AppUpdateError):
                download_update(update, Path(temp_dir), opener=FakeOpener({}))

    def test_download_update_refuses_unconfigured_public_key_before_network(self) -> None:
        update = AppUpdateInfo(
            current_version="0.1.0",
            latest_version="0.2.0",
            release_url="https://example.test/release",
            release_notes="",
            installer=ReleaseAsset("Tesseract-Setup-0.2.0-win-x64.exe", "https://example.test/setup.exe"),
            checksum=ReleaseAsset("SHA256SUMS.txt", "https://example.test/SHA256SUMS.txt"),
            update_available=True,
            checksum_signature=ReleaseAsset(
                "SHA256SUMS.txt.sig", "https://example.test/SHA256SUMS.txt.sig"
            ),
        )
        opener = FakeOpener({})

        with patch(
            "app_updater.app_info.APP_UPDATE_SIGNING_PUBLIC_KEY", "", create=True
        ), patch(
            "app_updater.app_info.APP_UPDATE_SIGNING_PUBLIC_KEYS", (), create=True
        ):
            with self.assertRaisesRegex(AppUpdateError, "No trusted release-signing public key"):
                download_update(update, Path("unused"), opener=opener)

        self.assertEqual(opener.urls, [])

    def test_launch_update_installer_uses_inno_silent_arguments(self) -> None:
        calls: list[dict[str, object]] = []

        def fake_popen(args, **kwargs):  # type: ignore[no-untyped-def]
            calls.append({"args": args, **kwargs})
            return object()

        downloaded = DownloadedUpdate(
            update=AppUpdateInfo(
                current_version="0.1.0",
                latest_version="0.2.0",
                release_url="",
                release_notes="",
                installer=None,
                checksum=None,
                update_available=True,
            ),
            installer_path=Path("C:/Temp/Tesseract-Setup-0.2.0-win-x64.exe"),
            sha256="a" * 64,
            manifest_key_id="sha256:" + "b" * 64,
            manifest_verified=True,
        )
        with patch("app_updater.sys.platform", "win32"), patch(
            "app_updater.sha256_file", return_value="a" * 64
        ):
            args = launch_update_installer(downloaded, popen=fake_popen)

        self.assertIn("/SILENT", args)
        self.assertIn("/CLOSEAPPLICATIONS", args)
        self.assertEqual(calls[0]["cwd"], "C:\\Temp")
        if sys.platform == "win32":
            self.assertIn("creationflags", calls[0])
            self.assertIn("startupinfo", calls[0])

    def test_launch_update_installer_refuses_unverified_manifest(self) -> None:
        downloaded = DownloadedUpdate(
            update=AppUpdateInfo(
                current_version="0.1.0",
                latest_version="0.2.0",
                release_url="",
                release_notes="",
                installer=None,
                checksum=None,
                update_available=True,
            ),
            installer_path=Path("C:/Temp/Tesseract-Setup-0.2.0-win-x64.exe"),
            sha256="a" * 64,
        )

        with patch("app_updater.sys.platform", "win32"):
            with self.assertRaisesRegex(AppUpdateError, "verified release manifest"):
                launch_update_installer(downloaded)

    def test_launch_update_installer_refuses_file_changed_after_download(self) -> None:
        downloaded = DownloadedUpdate(
            update=AppUpdateInfo(
                current_version="0.1.0",
                latest_version="0.2.0",
                release_url="",
                release_notes="",
                installer=None,
                checksum=None,
                update_available=True,
            ),
            installer_path=Path("C:/Temp/Tesseract-Setup-0.2.0-win-x64.exe"),
            sha256="a" * 64,
            manifest_key_id="sha256:" + "b" * 64,
            manifest_verified=True,
        )

        with patch("app_updater.sys.platform", "win32"), patch(
            "app_updater.sha256_file", return_value="c" * 64
        ):
            with self.assertRaisesRegex(AppUpdateError, "changed before launch"):
                launch_update_installer(downloaded)


if __name__ == "__main__":
    unittest.main()
