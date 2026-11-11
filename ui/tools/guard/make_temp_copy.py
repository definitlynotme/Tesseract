"""A disposable copy of the source tree for the guard's injection checks (T-P6-05 acceptance).

    python -B ui/tools/guard/make_temp_copy.py --dest <empty dir under %TEMP%>

Copies what the guard reads (the root modules and config templates, bridge_schema, locales, static,
tests, scripts, ui without node_modules / dist, shell/app/src) and links the big read-only
directories back to this tree with directory junctions (ui/node_modules, bin). The copy has no
.venv: scripts/ui_guard.py falls back to this tree's .venv. Then, for example:

    python -B scripts/ui_guard.py --repo <copy> --only 1,2,3 --source-sidecar --out <dir>

runs the checks on the edited copy (item 3 on the copy's runtime_main.pyw, no PyInstaller build).
Delete the copy afterwards (rmdir the junctions first, or use --remove: it unlinks them before
deleting, so the linked directories of this tree are never touched).
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
ROOT_SUFFIXES = {".py", ".pyw", ".yaml", ".txt", ".json", ".spec"}
DIRS = ("bridge_schema", "locales", "static", "tests", "scripts", "ui", "shell/app/src")
SKIP = {"node_modules", "dist", "__pycache__", ".pytest_cache"}
JUNCTIONS = ("ui/node_modules", "bin")


def junction(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)


def is_junction(path: Path) -> bool:
    try:
        return path.is_junction() if hasattr(path, "is_junction") else bool(os.readlink(path))
    except OSError:
        return False


def make_copy(dest: Path) -> Path:
    dest = dest.resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    if temp_root not in dest.parents:
        raise SystemExit(f"--dest must be under {temp_root}")
    if dest.exists() and any(dest.iterdir()):
        raise SystemExit(f"--dest is not empty: {dest}")
    dest.mkdir(parents=True, exist_ok=True)
    for item in REPO.iterdir():
        if item.is_file() and item.suffix.lower() in ROOT_SUFFIXES:
            shutil.copy2(item, dest / item.name)
    for name in DIRS:
        source = REPO / name
        if source.is_dir():
            shutil.copytree(source, dest / name, ignore=lambda d, names: [n for n in names if n in SKIP])
    for name in JUNCTIONS:
        if (REPO / name).is_dir():
            junction(dest / name, REPO / name)
    return dest


def remove_copy(dest: Path) -> None:
    dest = dest.resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    if temp_root not in dest.parents:
        raise SystemExit(f"refusing to delete {dest}: not under {temp_root}")
    for name in JUNCTIONS:
        link = dest / name
        if link.exists() and is_junction(link):
            os.rmdir(link)  # removes the link only
    shutil.rmtree(dest, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="make_temp_copy.py", description=__doc__.splitlines()[0])
    parser.add_argument("--dest", type=Path, required=True)
    parser.add_argument("--remove", action="store_true", help="delete a copy made by this script")
    args = parser.parse_args(argv)
    if args.remove:
        remove_copy(args.dest)
        print(f"removed {args.dest}")
        return 0
    print(make_copy(args.dest))
    return 0


if __name__ == "__main__":
    sys.exit(main())
