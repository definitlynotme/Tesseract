"""Small Python steps for scripts/publish_release.ps1.

    check-key <private-key.pem>     the key opens with the password in
                                    TESSERACT_RELEASE_SIGNING_PASSWORD and its
                                    public half is the first key in app_info
    check-public-key <public.txt>   the public half saved next to the key file
                                    is the first key in app_info (no password)
    install-public-key <public.txt> put that public key into app_info.py as the
                                    only update-signing key (before the first
                                    public release only)
    verify-live [download-dir]      read the latest GitHub release exactly as
                                    the app does, download it and verify the
                                    signed manifest with the embedded key ring
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

PASSWORD_ENV = "TESSERACT_RELEASE_SIGNING_PASSWORD"
APP_INFO = REPOSITORY_ROOT / "app_info.py"
_KEY_LINE = re.compile(rb'^APP_UPDATE_SIGNING_PUBLIC_KEY = "[^"\r\n]*"$', re.MULTILINE)


def check_key(private_key_path: Path) -> int:
    import app_info
    from app_update_signing import (
        decode_public_key,
        load_encrypted_private_key,
        public_key_base64_from_private_key,
        public_key_id,
    )

    password = os.environ.get(PASSWORD_ENV, "").encode("utf-8")
    private_key = load_encrypted_private_key(private_key_path, password)
    derived = public_key_base64_from_private_key(private_key)
    ring = tuple(app_info.APP_UPDATE_SIGNING_PUBLIC_KEYS)
    if not ring or ring[0] != app_info.APP_UPDATE_SIGNING_PUBLIC_KEY:
        print("app_info.py: the signing key must be the first key of the key ring.")
        return 2
    key_id = public_key_id(decode_public_key(derived))
    if derived != app_info.APP_UPDATE_SIGNING_PUBLIC_KEY:
        print(f"The private key ({key_id}) does not match the public key built into app_info.py.")
        return 3
    print(f"Update-signing key matches app_info.py ({key_id}).")
    return 0


def check_public_key(public_key_path: Path) -> int:
    import app_info
    from app_update_signing import decode_public_key, public_key_id

    public_key = public_key_path.read_text(encoding="ascii").strip()
    key_id = public_key_id(decode_public_key(public_key))
    ring = tuple(app_info.APP_UPDATE_SIGNING_PUBLIC_KEYS)
    if not ring or ring[0] != app_info.APP_UPDATE_SIGNING_PUBLIC_KEY:
        print("app_info.py: the signing key must be the first key of the key ring.")
        return 2
    if public_key != app_info.APP_UPDATE_SIGNING_PUBLIC_KEY:
        print(f"The saved public key ({key_id}) does not match the public key built into app_info.py.")
        return 3
    print(f"Update-signing key matches app_info.py ({key_id}).")
    return 0


def install_public_key(public_key_path: Path) -> int:
    from app_update_signing import decode_public_key, public_key_id

    public_key = public_key_path.read_text(encoding="ascii").strip()
    key_id = public_key_id(decode_public_key(public_key))
    # Patch by bytes: the repository is LF and text-mode writes would turn it CRLF.
    data = APP_INFO.read_bytes()
    replacement = f'APP_UPDATE_SIGNING_PUBLIC_KEY = "{public_key}"'.encode("ascii")
    patched, count = _KEY_LINE.subn(replacement, data)
    if count != 1:
        print("app_info.py: expected exactly one APP_UPDATE_SIGNING_PUBLIC_KEY line.")
        return 2
    APP_INFO.write_bytes(patched)
    print(f"app_info.py now trusts update-signing key {key_id}.")
    return 0


def verify_live(download_dir: Path | None) -> int:
    import app_info
    import app_updater

    # Pretend to be an old install so the latest release counts as an update.
    info = app_updater.check_latest_update(current_version="0.0.0")
    print(f"Latest release: {info.latest_version} ({info.release_url})")
    if not info.installable:
        print("The release is not installable by the app (installer, SHA256SUMS.txt or its .sig missing).")
        return 2
    if app_updater.normalize_version(info.latest_version) != app_updater.normalize_version(app_info.APP_VERSION):
        print(f"Note: this source tree is version {app_info.APP_VERSION}.")
    with tempfile.TemporaryDirectory(prefix="tesseract-update-check-") as scratch:
        target = download_dir or Path(scratch)
        downloaded = app_updater.download_update(info, target)
        if not downloaded.manifest_verified:
            print("The downloaded installer was not verified against the signed manifest.")
            return 3
        print(f"Downloaded {downloaded.installer_path.name}: SHA-256 matches the signed manifest "
              f"(key {downloaded.manifest_key_id}).")
    print("In-app update path verified against the live release.")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "check-key":
        return check_key(Path(argv[1]))
    if len(argv) >= 2 and argv[0] == "check-public-key":
        return check_public_key(Path(argv[1]))
    if len(argv) >= 2 and argv[0] == "install-public-key":
        return install_public_key(Path(argv[1]))
    if argv and argv[0] == "verify-live":
        return verify_live(Path(argv[1]) if len(argv) > 1 else None)
    print(__doc__)
    return 64


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except Exception as exc:  # report one line; the PowerShell caller stops on the exit code
        print(f"{type(exc).__name__}: {exc}")
        raise SystemExit(1)
