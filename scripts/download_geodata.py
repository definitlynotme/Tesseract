"""Fetch the GeoData files that ship inside the Windows package.

The package carries ``geoip.dat`` and ``geosite.dat`` so a first start never
has to download them before Mihomo can load GEOIP/GEOSITE rules.  The runtime
GeoUpdater keeps refreshing them afterwards from the same upstream.

Upstream (MetaCubeX/meta-rules-dat) rebuilds its ``latest`` release every day
and force-pushes the branch behind it, so no URL stays pinned to one build.
Each file is therefore checked against the ``.sha256sum`` published in the
same release, checked for the v2ray ``.dat`` shape, and recorded in
``PROVENANCE.json`` (URL, SHA-256, size, fetch time) next to the files; that
record is what ships with the package and identifies the exact data.

Existing files are reused when ``PROVENANCE.json`` still matches them, unless
``--force`` is given (official builds always fetch).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = REPO_ROOT / "bin" / "geodata"
DEFAULT_BASE_URL = "https://github.com/MetaCubeX/meta-rules-dat/releases/download/latest"
SOURCE = {
    "name": "MetaCubeX/meta-rules-dat",
    "homepage": "https://github.com/MetaCubeX/meta-rules-dat",
    "license": "GPL-3.0",
    "release": "latest",
}
ASSETS = ("geoip.dat", "geosite.dat")
PROVENANCE_NAME = "PROVENANCE.json"
# Full upstream files are several MB; anything this small is a lite build, a
# truncated download or an error page.
MIN_ASSET_BYTES = 1024 * 1024
MAX_ASSET_BYTES = 200 * 1024 * 1024
# Each list entry is a length-delimited field 1 whose message starts with the
# country_code string; the default routing rules need "CN" in both files.
REQUIRED_MARKER = b"\x0a\x02CN"
USER_AGENT = "Tesseract-build"
_SHA256_RE = re.compile(r"^([0-9a-fA-F]{64})\b")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_asset(name: str, path: Path) -> None:
    """Raise ValueError unless ``path`` looks like a full upstream ``.dat``."""

    size = path.stat().st_size
    if size < MIN_ASSET_BYTES:
        raise ValueError(f"{name} is only {size} bytes; expected the full upstream file")
    if size > MAX_ASSET_BYTES:
        raise ValueError(f"{name} is {size} bytes, above the {MAX_ASSET_BYTES} byte cap")
    data = path.read_bytes()
    if not data.startswith(b"\x0a"):
        raise ValueError(f"{name} does not start like a v2ray GeoData list")
    if REQUIRED_MARKER not in data:
        raise ValueError(f"{name} has no CN entry")


def read_provenance(output_dir: Path) -> dict | None:
    try:
        payload = json.loads((output_dir / PROVENANCE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def existing_files_current(output_dir: Path) -> bool:
    """Whether every asset on disk is the one PROVENANCE.json records."""

    payload = read_provenance(output_dir)
    files = payload.get("files") if payload else None
    if not isinstance(files, dict):
        return False
    for name in ASSETS:
        record = files.get(name)
        path = output_dir / name
        if not isinstance(record, dict) or not path.is_file():
            return False
        if sha256_file(path) != str(record.get("sha256", "")).lower():
            return False
        try:
            validate_asset(name, path)
        except ValueError:
            return False
    return True


def _open(url: str, timeout: float):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    return urllib.request.urlopen(request, timeout=timeout)


def _with_retries(action, description: str, attempts: int):
    last: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return action()
        except (OSError, ValueError) as exc:
            last = exc
            print(f"{description} failed (attempt {attempt}/{attempts}): {exc}", file=sys.stderr)
            if attempt < attempts:
                time.sleep(2.0 * attempt)
    raise RuntimeError(f"{description} failed: {last}")


def fetch_expected_sha256(url: str, timeout: float) -> str:
    with _open(url, timeout) as response:
        text = response.read(4096).decode("ascii", errors="replace").strip()
    match = _SHA256_RE.match(text)
    if not match:
        raise ValueError(f"unexpected checksum file at {url}: {text[:80]!r}")
    return match.group(1).lower()


def fetch_asset(name: str, url: str, expected_sha256: str, output_dir: Path, timeout: float) -> tuple[Path, int]:
    """Download ``url`` into a temporary file in ``output_dir`` and verify it."""

    handle = tempfile.NamedTemporaryFile(
        "wb", dir=output_dir, prefix=f".{name}.", suffix=".tmp", delete=False
    )
    temp_path = Path(handle.name)
    try:
        with handle, _open(url, timeout) as response:
            total = 0
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_ASSET_BYTES:
                    raise ValueError(f"{name} exceeds the {MAX_ASSET_BYTES} byte cap")
                handle.write(chunk)
        actual = sha256_file(temp_path)
        if actual != expected_sha256:
            raise ValueError(f"{name} SHA-256 mismatch: expected {expected_sha256}, got {actual}")
        validate_asset(name, temp_path)
        return temp_path, total
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def download(output_dir: Path, *, base_url: str, timeout: float, attempts: int) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    base_url = base_url.rstrip("/")
    fetched: dict[str, tuple[Path, int, str, str]] = {}
    try:
        # Both files are fetched and verified before either replaces what is
        # on disk, so the pair always comes from one upstream build.
        for name in ASSETS:
            url = f"{base_url}/{name}"
            expected = _with_retries(
                lambda: fetch_expected_sha256(f"{url}.sha256sum", timeout),
                f"checksum for {name}",
                attempts,
            )
            temp_path, size = _with_retries(
                lambda: fetch_asset(name, url, expected, output_dir, timeout),
                f"download of {name}",
                attempts,
            )
            fetched[name] = (temp_path, size, expected, url)
            print(f"Verified {name}: {size} bytes, sha256 {expected}")
        for name, (temp_path, _size, _digest, _url) in fetched.items():
            os.replace(temp_path, output_dir / name)
    finally:
        for temp_path, *_rest in fetched.values():
            temp_path.unlink(missing_ok=True)

    provenance = {
        "source": dict(SOURCE),
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "files": {
            name: {"url": url, "sha256": digest, "bytes": size}
            for name, (_temp, size, digest, url) in fetched.items()
        },
    }
    temp = output_dir / f".{PROVENANCE_NAME}.tmp"
    temp.write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, output_dir / PROVENANCE_NAME)
    return provenance


def _under_repo(path: Path) -> Path:
    resolved = path.resolve()
    if resolved != REPO_ROOT and REPO_ROOT not in resolved.parents:
        raise SystemExit(f"Refusing to write outside repository: {resolved}")
    return resolved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--force", action="store_true", help="fetch even when the files on disk are current")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--attempts", type=int, default=3)
    args = parser.parse_args(argv)

    output_dir = _under_repo(args.output_dir)
    if not args.force and existing_files_current(output_dir):
        print(f"GeoData already present at {output_dir}")
        return 0
    try:
        download(output_dir, base_url=args.base_url, timeout=args.timeout, attempts=max(1, args.attempts))
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"GeoData download failed: {exc}", file=sys.stderr)
        return 1
    print(f"GeoData written to {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
