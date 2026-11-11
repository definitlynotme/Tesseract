from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from app_update_signing import (  # noqa: E402
    ReleaseSignatureError,
    decode_public_key,
    public_key_base64_from_private_key,
    public_key_id,
    write_new_file,
)


DEFAULT_PASSWORD_ENV = "TESSERACT_RELEASE_SIGNING_PASSWORD"


def _password_from_environment_or_prompt(environment_name: str, *, confirm: bool) -> bytes:
    password = os.environ.get(environment_name)
    if password is None:
        password = getpass.getpass("Release signing key password: ")
        if confirm and password != getpass.getpass("Confirm password: "):
            raise ReleaseSignatureError("The two passwords did not match.")
    if not password:
        raise ReleaseSignatureError("A non-empty private-key password is required.")
    return password.encode("utf-8")


def generate_key_files(private_key_path: Path, public_key_path: Path, password: bytes) -> str:
    if private_key_path.resolve() == public_key_path.resolve():
        raise ReleaseSignatureError("Private-key and public-key paths must be different.")
    if private_key_path.exists() or public_key_path.exists():
        raise ReleaseSignatureError("Refusing to overwrite an existing signing-key file.")
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError as exc:
        raise ReleaseSignatureError("The cryptography package is unavailable.") from exc
    if not password:
        raise ReleaseSignatureError("A non-empty private-key password is required.")
    private_key = Ed25519PrivateKey.generate()
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.BestAvailableEncryption(password),
    )
    public_base64 = public_key_base64_from_private_key(private_key)
    private_created = False
    public_created = False
    try:
        write_new_file(private_key_path, private_pem)
        private_created = True
        write_new_file(public_key_path, (public_base64 + "\n").encode("ascii"))
        public_created = True
    except Exception:
        if private_created:
            private_key_path.unlink(missing_ok=True)
        if public_created:
            public_key_path.unlink(missing_ok=True)
        raise
    return public_key_id(decode_public_key(public_base64))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate a password-protected Ed25519 key for Tesseract release manifests."
    )
    parser.add_argument("--private-key", required=True, type=Path)
    parser.add_argument("--public-key", required=True, type=Path)
    parser.add_argument("--password-env", default=DEFAULT_PASSWORD_ENV)
    args = parser.parse_args(argv)
    try:
        password = _password_from_environment_or_prompt(args.password_env, confirm=True)
        key_id = generate_key_files(args.private_key, args.public_key, password)
    except ReleaseSignatureError as exc:
        parser.error(str(exc))
    print(f"Generated encrypted private key: {args.private_key}")
    print(f"Generated public key: {args.public_key}")
    print(f"Key ID: {key_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
