from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from app_update_signing import (  # noqa: E402
    ReleaseSignatureError,
    SIGNED_MANIFEST_NAME,
    create_signature_document,
    load_encrypted_private_key,
    parse_signature_document,
    public_key_base64_from_private_key,
    public_key_id,
    decode_public_key,
    write_new_file,
)


DEFAULT_PASSWORD_ENV = "TESSERACT_RELEASE_SIGNING_PASSWORD"


def _password_from_environment_or_prompt(environment_name: str) -> bytes:
    password = os.environ.get(environment_name)
    if password is None:
        password = getpass.getpass("Release signing key password: ")
    if not password:
        raise ReleaseSignatureError("A non-empty private-key password is required.")
    return password.encode("utf-8")


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise ReleaseSignatureError(f"Could not atomically write release input: {path}") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise ReleaseSignatureError(f"Could not hash release payload: {path}") from exc
    return digest.hexdigest()


def prepare_signed_release_inputs(
    *,
    manifest_path: Path,
    metadata_path: Path,
    payload_paths: list[Path],
    key_id: str,
) -> None:
    if len(payload_paths) != 5:
        raise ReleaseSignatureError("The signed release must contain exactly five manifest payloads.")
    resolved_manifest = manifest_path.resolve()
    resolved_metadata = metadata_path.resolve()
    resolved_payloads = [path.resolve() for path in payload_paths]
    if len(set(resolved_payloads)) != len(resolved_payloads):
        raise ReleaseSignatureError("The signed release payload list contains duplicate paths.")
    if resolved_metadata not in resolved_payloads:
        raise ReleaseSignatureError("release-metadata.json must be one of the signed payloads.")
    if resolved_manifest in resolved_payloads:
        raise ReleaseSignatureError("SHA256SUMS.txt cannot include itself as a payload.")
    if any(path.name == "SHA256SUMS.txt.sig" for path in resolved_payloads):
        raise ReleaseSignatureError("SHA256SUMS.txt.sig cannot be a manifest payload.")
    if len({path.name for path in resolved_payloads}) != len(resolved_payloads):
        raise ReleaseSignatureError("Signed release payload file names must be unique.")
    for path in resolved_payloads:
        if not path.is_file():
            raise ReleaseSignatureError(f"Release payload is missing: {path}")
        if path.parent != resolved_manifest.parent:
            raise ReleaseSignatureError("Every signed payload must be beside SHA256SUMS.txt.")

    try:
        metadata = json.loads(resolved_metadata.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseSignatureError("release-metadata.json is not valid UTF-8 JSON.") from exc
    if not isinstance(metadata, dict) or metadata.get("schema_version") != 2:
        raise ReleaseSignatureError("release-metadata.json must be a schema-version-2 object.")
    if metadata.get("authenticode") != "unsigned-by-policy":
        raise ReleaseSignatureError("Manifest signing cannot change the Authenticode policy.")
    trusted_key_ids = metadata.get("trusted_update_key_ids")
    if (
        not isinstance(trusted_key_ids, list)
        or not trusted_key_ids
        or any(not isinstance(value, str) for value in trusted_key_ids)
        or len(set(trusted_key_ids)) != len(trusted_key_ids)
        or key_id not in trusted_key_ids
    ):
        raise ReleaseSignatureError(
            "release-metadata.json must name the signing key in a unique trusted_update_key_ids list."
        )
    metadata["release_signature"] = "ed25519-valid"
    metadata["key_id"] = key_id
    metadata_bytes = (
        json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    _atomic_write(resolved_metadata, metadata_bytes)

    manifest_lines = [
        f"{_sha256(path)}  {path.name}"
        for path in sorted(resolved_payloads, key=lambda candidate: candidate.name)
    ]
    manifest_bytes = ("\n".join(manifest_lines) + "\n").encode("utf-8")
    _atomic_write(resolved_manifest, manifest_bytes)


def sign_manifest(
    manifest_path: Path,
    private_key_path: Path,
    output_path: Path,
    password: bytes,
    *,
    metadata_path: Path | None = None,
    payload_paths: list[Path] | None = None,
) -> str:
    if manifest_path.name != SIGNED_MANIFEST_NAME:
        raise ReleaseSignatureError(f"The signed manifest must be named {SIGNED_MANIFEST_NAME}.")
    if output_path.exists():
        raise ReleaseSignatureError(f"Refusing to overwrite existing file: {output_path}")
    private_key = load_encrypted_private_key(private_key_path, password)
    public_key_base64 = public_key_base64_from_private_key(private_key)
    key_id = public_key_id(decode_public_key(public_key_base64))
    if (metadata_path is None) != (payload_paths is None):
        raise ReleaseSignatureError("--metadata and --payload must be supplied together.")
    if metadata_path is not None and payload_paths is not None:
        prepare_signed_release_inputs(
            manifest_path=manifest_path,
            metadata_path=metadata_path,
            payload_paths=payload_paths,
            key_id=key_id,
        )
    try:
        manifest_bytes = manifest_path.read_bytes()
    except OSError as exc:
        raise ReleaseSignatureError(f"Could not read release manifest: {manifest_path}") from exc
    signature_document = create_signature_document(manifest_bytes, private_key)
    write_new_file(output_path, signature_document)
    parsed_key_id = str(parse_signature_document(signature_document)["key_id"])
    if parsed_key_id != key_id:
        output_path.unlink(missing_ok=True)
        raise ReleaseSignatureError("The generated signature key ID is inconsistent.")
    return parsed_key_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sign SHA256SUMS.txt with an encrypted Ed25519 key.")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--private-key", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--password-env", default=DEFAULT_PASSWORD_ENV)
    parser.add_argument("--metadata", type=Path)
    parser.add_argument("--payload", action="append", type=Path)
    args = parser.parse_args(argv)
    try:
        password = _password_from_environment_or_prompt(args.password_env)
        key_id = sign_manifest(
            args.manifest,
            args.private_key,
            args.output,
            password,
            metadata_path=args.metadata,
            payload_paths=args.payload,
        )
    except ReleaseSignatureError as exc:
        parser.error(str(exc))
    print(f"Signed {args.manifest} with {key_id}")
    print(f"Signature document: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
