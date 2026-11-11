from __future__ import annotations

import argparse
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from app_update_signing import (  # noqa: E402
    ReleaseSignatureError,
    SIGNED_MANIFEST_NAME,
    verify_signature_document,
)


def verify_manifest(manifest_path: Path, signature_path: Path, public_key_path: Path) -> str:
    if manifest_path.name != SIGNED_MANIFEST_NAME:
        raise ReleaseSignatureError(f"The signed manifest must be named {SIGNED_MANIFEST_NAME}.")
    try:
        manifest_bytes = manifest_path.read_bytes()
        signature_document = signature_path.read_bytes()
        public_key_base64 = public_key_path.read_bytes().strip()
    except OSError as exc:
        raise ReleaseSignatureError("A release verification input could not be read.") from exc
    return verify_signature_document(manifest_bytes, signature_document, public_key_base64)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify a signed Tesseract SHA256SUMS.txt manifest.")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--signature", required=True, type=Path)
    parser.add_argument("--public-key", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        key_id = verify_manifest(args.manifest, args.signature, args.public_key)
    except ReleaseSignatureError as exc:
        parser.error(str(exc))
    print(f"Verified {args.manifest} with {key_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

