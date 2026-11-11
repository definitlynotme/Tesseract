"""Generate ui/src/rt/spec.gen.json from bridge_protocol (T-P4-02a; P4 architecture section 2.4).

    python -B ui/tools/gen_rt_spec.py            write ui/src/rt/spec.gen.json
    python -B ui/tools/gen_rt_spec.py --check    exit 1 when the file differs from what
                                                 bridge_protocol produces (npm run check)
    python -B ui/tools/gen_rt_spec.py --stdout   print the generated JSON

The table carries only names and dispatch metadata: for each command its domain, source,
reply mode and cancel column; for each query the fixed query metadata; for each topic its
legal kinds, whether it is keyed and whether it may be patched. Args, DTO fields and enums
are deliberately left out (criticism B11), so backend tickets that only add optional fields
never turn the front-end gate red. Output is deterministic: sorted keys, LF, final newline.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

UI = Path(__file__).resolve().parent.parent
REPO = UI.parent
TARGET = UI / "src" / "rt" / "spec.gen.json"

sys.path.insert(0, str(REPO))
import bridge_protocol as bp  # noqa: E402  (stdlib-only module at the repo root)


def build_spec() -> dict:
    commands = {}
    for name, spec in bp.V1_COMMAND_SPECS.items():
        domain, source, reply, cancel = spec[0], spec[1], spec[2], spec[3]
        commands[name] = {"domain": domain, "source": source, "reply": reply, "cancel": cancel}
    # Queries (PROTOCOL section 11): normal domain, page source, one res, owner-only cancel.
    queries = {name: {"domain": "normal", "source": "page", "reply": "res", "cancel": "owner"}
               for name in bp.V1_QUERY_SPECS}
    topics = {}
    for name in sorted(bp.V1_TOPICS):
        kinds = sorted(bp.TOPIC_KINDS[name])
        topics[name] = {
            "kinds": kinds,
            "keyed": name in bp.KEYED_TOPICS,
            "patchable": "patch" in kinds,
            "shell_only": name in bp.SHELL_TOPICS,
        }
    return {
        "generated_by": "ui/tools/gen_rt_spec.py",
        "proto": bp.PROTOCOL_VERSION,
        "commands": commands,
        "queries": queries,
        "page_commands": sorted(bp.PAGE_COMMANDS),
        "topics": topics,
    }


def render(spec: dict) -> bytes:
    return (json.dumps(spec, indent=1, sort_keys=True, ensure_ascii=True) + "\n").encode("utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="compare instead of writing")
    group.add_argument("--stdout", action="store_true", help="print instead of writing")
    args = parser.parse_args(argv)
    data = render(build_spec())
    if args.stdout:
        sys.stdout.write(data.decode("utf-8"))
        return 0
    if args.check:
        current = TARGET.read_bytes() if TARGET.exists() else b""
        if current != data:
            print(f"[gen_rt_spec] {TARGET.relative_to(REPO).as_posix()} is out of date: "
                  "run .venv/Scripts/python.exe -B ui/tools/gen_rt_spec.py", file=sys.stderr)
            return 1
        print("[gen_rt_spec] spec.gen.json is in sync with bridge_protocol")
        return 0
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_bytes(data)
    print(f"[gen_rt_spec] wrote {TARGET.relative_to(REPO).as_posix()} ({len(data)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
