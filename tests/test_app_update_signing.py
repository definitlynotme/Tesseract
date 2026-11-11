from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app_update_signing import (
    ReleaseSignatureError,
    create_signature_document,
    decode_public_key,
    parse_signature_document,
    public_key_id,
    verify_signature_document,
    verify_signature_document_with_keyring,
)
from scripts.sign_release_manifest import sign_manifest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class AppUpdateSigningTests(unittest.TestCase):
    @staticmethod
    def _key_material() -> tuple[Ed25519PrivateKey, str]:
        private_key = Ed25519PrivateKey.generate()
        raw_public_key = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return private_key, base64.b64encode(raw_public_key).decode("ascii")

    def test_signature_document_round_trip_uses_strict_contract(self) -> None:
        private_key, public_key = self._key_material()
        manifest = b"a" * 64 + b"  Tesseract-Setup-0.2.0-win-x64.exe\n"

        document = create_signature_document(manifest, private_key)
        payload = parse_signature_document(document)
        key_id = verify_signature_document(manifest, document, public_key)

        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["algorithm"], "Ed25519")
        self.assertEqual(payload["signed_file"], "SHA256SUMS.txt")
        self.assertEqual(key_id, public_key_id(decode_public_key(public_key)))
        self.assertEqual(len(base64.b64decode(payload["signature"], validate=True)), 64)

    def test_transition_keyring_selects_the_key_named_by_the_signature(self) -> None:
        old_private, old_public = self._key_material()
        _, new_public = self._key_material()
        manifest = b"transition release\n"
        document = create_signature_document(manifest, old_private)

        key_id = verify_signature_document_with_keyring(
            manifest,
            document,
            (old_public, new_public),
        )

        self.assertEqual(key_id, public_key_id(decode_public_key(old_public)))
        with self.assertRaisesRegex(ReleaseSignatureError, "trusted key ring"):
            verify_signature_document_with_keyring(manifest, document, (new_public,))
        with self.assertRaisesRegex(ReleaseSignatureError, "duplicate"):
            verify_signature_document_with_keyring(
                manifest,
                document,
                (old_public, old_public),
            )

    def test_verification_rejects_tamper_wrong_key_and_malformed_document(self) -> None:
        private_key, public_key = self._key_material()
        _, wrong_public_key = self._key_material()
        manifest = b"manifest\n"
        document = create_signature_document(manifest, private_key)
        payload = json.loads(document)

        cases = [
            (manifest + b"x", document, public_key),
            (manifest, document, wrong_public_key),
            (manifest, b"{}", public_key),
            (
                manifest,
                json.dumps({**payload, "algorithm": "ed25519"}).encode("utf-8"),
                public_key,
            ),
            (
                manifest,
                document.rstrip()[:-1] + b',"extra":true}',
                public_key,
            ),
        ]
        for signed_bytes, signature_document, trusted_key in cases:
            with self.subTest(signature_document=signature_document):
                with self.assertRaises(ReleaseSignatureError):
                    verify_signature_document(signed_bytes, signature_document, trusted_key)

    def test_parser_rejects_duplicate_fields_and_noncanonical_signature(self) -> None:
        private_key, public_key = self._key_material()
        manifest = b"manifest\n"
        payload = json.loads(create_signature_document(manifest, private_key))
        duplicate = (
            '{"schema_version":1,"schema_version":1,"algorithm":"Ed25519",'
            f'"key_id":"{payload["key_id"]}","signed_file":"SHA256SUMS.txt",'
            f'"signature":"{payload["signature"]}"}}'
        ).encode("utf-8")
        malformed = json.dumps({**payload, "signature": payload["signature"].rstrip("=")}).encode("utf-8")

        with self.assertRaises(ReleaseSignatureError):
            verify_signature_document(manifest, duplicate, public_key)
        with self.assertRaises(ReleaseSignatureError):
            verify_signature_document(manifest, malformed, public_key)

    def test_cli_generates_encrypted_key_signs_and_verifies_manifest(self) -> None:
        password = "test-only-password"
        environment = dict(os.environ)
        environment["TESSERACT_RELEASE_SIGNING_PASSWORD"] = password
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            private_key = root / "release-private.pem"
            public_key = root / "release-public.txt"
            manifest = root / "SHA256SUMS.txt"
            signature = root / "SHA256SUMS.txt.sig"
            manifest.write_bytes(b"a" * 64 + b"  artifact.exe\n")

            generate = subprocess.run(
                [
                    sys.executable,
                    str(REPOSITORY_ROOT / "scripts" / "generate_release_signing_key.py"),
                    "--private-key",
                    str(private_key),
                    "--public-key",
                    str(public_key),
                ],
                cwd=REPOSITORY_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(generate.returncode, 0, generate.stderr)
            self.assertIn(b"ENCRYPTED PRIVATE KEY", private_key.read_bytes())
            with self.assertRaises(TypeError):
                serialization.load_pem_private_key(private_key.read_bytes(), password=None)

            sign = subprocess.run(
                [
                    sys.executable,
                    str(REPOSITORY_ROOT / "scripts" / "sign_release_manifest.py"),
                    "--manifest",
                    str(manifest),
                    "--private-key",
                    str(private_key),
                    "--output",
                    str(signature),
                ],
                cwd=REPOSITORY_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(sign.returncode, 0, sign.stderr)

            verify = subprocess.run(
                [
                    sys.executable,
                    str(REPOSITORY_ROOT / "scripts" / "verify_release_manifest.py"),
                    "--manifest",
                    str(manifest),
                    "--signature",
                    str(signature),
                    "--public-key",
                    str(public_key),
                ],
                cwd=REPOSITORY_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(verify.returncode, 0, verify.stderr)
            self.assertIn("Verified", verify.stdout)

    def test_protected_signing_updates_metadata_then_rebuilds_exact_manifest(self) -> None:
        password = b"test-only-password"
        private_key, public_key = self._key_material()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            private_key_path = root / "release-private.pem"
            private_key_path.write_bytes(
                private_key.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.PKCS8,
                    encryption_algorithm=serialization.BestAvailableEncryption(password),
                )
            )
            metadata = root / "release-metadata.json"
            metadata.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "version": "0.2.0",
                        "authenticode": "unsigned-by-policy",
                        "release_signature": "pending",
                        "key_id": None,
                        "trusted_update_key_ids": [
                            public_key_id(decode_public_key(public_key))
                        ],
                    }
                ),
                encoding="utf-8",
            )
            payloads = [
                root / "Tesseract-Setup-0.2.0-win-x64.exe",
                root / "Tesseract-0.2.0-win-x64-portable.zip",
                root / "sbom.cdx.json",
                metadata,
                root / "Tesseract-0.2.0-third-party-sources.zip",
            ]
            for index, payload in enumerate(payloads):
                if payload != metadata:
                    payload.write_bytes(f"payload-{index}".encode("ascii"))
            manifest = root / "SHA256SUMS.txt"
            manifest.write_text("stale\n", encoding="utf-8")
            signature = root / "SHA256SUMS.txt.sig"

            key_id = sign_manifest(
                manifest,
                private_key_path,
                signature,
                password,
                metadata_path=metadata,
                payload_paths=payloads,
            )

            signed_metadata = json.loads(metadata.read_text(encoding="utf-8"))
            self.assertEqual(signed_metadata["release_signature"], "ed25519-valid")
            self.assertEqual(signed_metadata["key_id"], key_id)
            manifest_lines = manifest.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(manifest_lines), 5)
            self.assertEqual(
                sorted(line.split("  ", 1)[1] for line in manifest_lines),
                sorted(path.name for path in payloads),
            )
            self.assertEqual(
                verify_signature_document(manifest.read_bytes(), signature.read_bytes(), public_key),
                key_id,
            )


if __name__ == "__main__":
    unittest.main()
