"""Import-only causal comparison. No network or throughput acceptance claim."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]


def measure(strategy: str) -> dict:
    import platform

    if sys.platform != "win32":
        raise RuntimeError("this diagnostic compares the Windows OS-name path")
    calls = []
    original_system = platform.system
    original_wmi = platform._wmi_query

    def query(*args):
        started = time.monotonic()
        error = ""
        try:
            return original_wmi(*args)
        except BaseException as exc:
            error = type(exc).__name__
            raise
        finally:
            calls.append({"table": args[0], "seconds": time.monotonic() - started,
                          "error": error})

    platform._wmi_query = query
    if strategy == "system-name":
        platform.system = lambda: "Windows"
    sys.path.insert(0, str(ROOT))
    started = time.monotonic()
    try:
        import tier3_validator_runtime  # noqa: F401
        import aiohttp.helpers

        return {"strategy": strategy, "seconds": time.monotonic() - started,
                "wmi": calls, "python": sys.version.split()[0],
                "aiohttp": aiohttp.__version__,
                "is_windows": aiohttp.helpers.IS_WINDOWS,
                "is_macos": aiohttp.helpers.IS_MACOS}
    finally:
        platform.system = original_system
        platform._wmi_query = original_wmi


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy", choices=("stdlib", "system-name"))
    parser.add_argument("--rounds", type=int, default=3)
    args = parser.parse_args()
    if args.strategy:
        print(json.dumps(measure(args.strategy)), flush=True)
        return 0
    env = dict(os.environ)
    executable = sys.executable
    if sys.platform == "win32" and hasattr(sys, "_base_executable"):
        env["__PYVENV_LAUNCHER__"] = executable
        executable = sys._base_executable
    for pair in range(args.rounds):
        strategies = ("stdlib", "system-name") if pair % 2 == 0 else ("system-name", "stdlib")
        for strategy in strategies:
            result = subprocess.run(
                [executable, str(Path(__file__).resolve()), "--strategy", strategy],
                cwd=ROOT, env=env, capture_output=True, text=True, timeout=25,
            )
            if result.returncode:
                print(json.dumps({"pair": pair, "strategy": strategy,
                                  "returncode": result.returncode, "stderr": result.stderr}), flush=True)
                return 1
            sample = json.loads(result.stdout)
            sample["pair"] = pair
            sample["stderr"] = result.stderr
            print(json.dumps(sample), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
