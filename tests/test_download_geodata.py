from __future__ import annotations

import hashlib
import http.server
import importlib.util
import json
import threading
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "download_geodata_under_test", REPO_ROOT / "scripts" / "download_geodata.py"
)
download_geodata = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(download_geodata)


def _asset(tag: bytes) -> bytes:
    # The shape validate_asset expects: a field-1 list entry with a CN code.
    head = b"\x0a\x08\x0a\x02CN" + tag
    return head + b"\x00" * (download_geodata.MIN_ASSET_BYTES + 16 - len(head))


@pytest.fixture
def server():
    files: dict[str, bytes] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            body = files.get(self.path.lstrip("/"))
            if body is None:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield files, f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def _publish(files: dict[str, bytes], name: str, body: bytes, *, digest: str | None = None) -> None:
    files[name] = body
    digest = digest or hashlib.sha256(body).hexdigest()
    files[f"{name}.sha256sum"] = f"{digest}  {name}\n".encode("ascii")


def _download(output_dir: Path, base_url: str) -> dict:
    return download_geodata.download(output_dir, base_url=base_url, timeout=10, attempts=1)


def test_download_verifies_files_and_records_provenance(server, tmp_path: Path) -> None:
    files, base_url = server
    geoip, geosite = _asset(b"ip"), _asset(b"site")
    _publish(files, "geoip.dat", geoip)
    _publish(files, "geosite.dat", geosite)

    provenance = _download(tmp_path, base_url)

    assert (tmp_path / "geoip.dat").read_bytes() == geoip
    assert (tmp_path / "geosite.dat").read_bytes() == geosite
    recorded = json.loads((tmp_path / "PROVENANCE.json").read_text(encoding="utf-8"))
    assert recorded == provenance
    assert recorded["source"]["name"] == "MetaCubeX/meta-rules-dat"
    assert recorded["files"]["geoip.dat"] == {
        "url": f"{base_url}/geoip.dat",
        "sha256": hashlib.sha256(geoip).hexdigest(),
        "bytes": len(geoip),
    }
    assert sorted(path.name for path in tmp_path.iterdir()) == ["PROVENANCE.json", "geoip.dat", "geosite.dat"]
    assert download_geodata.existing_files_current(tmp_path)

    (tmp_path / "geosite.dat").write_bytes(_asset(b"edited"))
    assert not download_geodata.existing_files_current(tmp_path)


def test_checksum_mismatch_keeps_the_previous_pair(server, tmp_path: Path) -> None:
    files, base_url = server
    _publish(files, "geoip.dat", _asset(b"ip-1"))
    _publish(files, "geosite.dat", _asset(b"site-1"))
    _download(tmp_path, base_url)
    before = {name: (tmp_path / name).read_bytes() for name in ("geoip.dat", "geosite.dat", "PROVENANCE.json")}

    # A mirror whose cache serves a newer geoip but a stale geosite checksum.
    _publish(files, "geoip.dat", _asset(b"ip-2"))
    _publish(files, "geosite.dat", _asset(b"site-2"), digest="0" * 64)

    with pytest.raises(RuntimeError, match="geosite.dat SHA-256 mismatch"):
        _download(tmp_path, base_url)

    assert {name: (tmp_path / name).read_bytes() for name in before} == before
    assert not list(tmp_path.glob(".*.tmp"))


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"\x0a\x02CN" + b"\x00" * 1024, "expected the full upstream file"),
        (b"<html>" + b"\x00" * (2 * 1024 * 1024), "does not start like a v2ray GeoData list"),
        (b"\x0a\x02US" + b"\x00" * (2 * 1024 * 1024), "has no CN entry"),
    ],
    ids=["lite-or-truncated", "error-page", "no-cn"],
)
def test_rejects_files_that_are_not_full_geodata(server, tmp_path: Path, body: bytes, message: str) -> None:
    files, base_url = server
    _publish(files, "geoip.dat", body)
    _publish(files, "geosite.dat", _asset(b"site"))

    with pytest.raises(RuntimeError, match=message):
        _download(tmp_path, base_url)

    assert not (tmp_path / "geoip.dat").exists()
    assert not (tmp_path / "geosite.dat").exists()


def test_main_refuses_to_write_outside_the_repository(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="outside repository"):
        download_geodata.main(["--output-dir", str(tmp_path)])
