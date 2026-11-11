"""Local transport evidence, not real-network cold/warm acceptance."""
import json
import os
from pathlib import Path
from statistics import median
import subprocess
import sys
import tempfile

import pytest

from windows_subprocess import hidden_subprocess_kwargs


@pytest.mark.skipif(sys.platform != "win32", reason="bundled Windows helper integration")
# Local evidence only. Every gate below is a speed threshold (time to 3/20/25
# nodes, progress gap, warm vs cold, a clean stop inside the probe's budget),
# so on a shared CI runner it measures the runner, not the code. The stop gate
# also depends on Windows process-kill latency: when the pool fills while
# helpers are still starting, confirming that every helper has exited can
# outlast the stop budget.
@pytest.mark.skipif(os.environ.get("GITHUB_ACTIONS") == "true",
                    reason="local timing probe; not a CI release gate")
@pytest.mark.parametrize("profile,count", [("all-responsive", 40), ("mixed-tail", 4096)])
def test_real_manager_cold_and_warm_keep_pass_authority_and_bounded_tail(profile, count):
    root = Path(__file__).resolve().parents[1]
    if not (root / "bin" / "mihomo.exe").is_file():
        pytest.skip("bundled Mihomo unavailable")
    scratch = root / ".build" / "controlled-pipeline-tests"
    scratch.mkdir(exist_ok=True)
    # Keep failed-run evidence. Each pair gets a fresh parent and fresh helpers;
    # native startup jitter is not a cache/scheduling throughput measurement.
    area = Path(tempfile.mkdtemp(prefix=f"{profile}-", dir=scratch))
    cold_times, warm_times = [], []
    rounds = 3 if profile == "all-responsive" else 1
    for index in range(rounds):
        pair = area / f"pair-{index + 1}"
        pair.mkdir()
        environment = os.environ.copy()
        for variable, suffix in (("TESSERACT_CONFIG_DIR", "config"), ("TESSERACT_DATA_DIR", "data"),
                                 ("TEMP", "temp"), ("TMP", "temp")):
            path = pair / suffix
            path.mkdir(exist_ok=True)
            environment[variable] = str(path)
        output = pair / "sessions"
        with (pair / "probe.log").open("w", encoding="utf-8") as log:
            result = subprocess.run(
                [sys.executable, "-m", "test_support.tier3_local_pipeline_probe",
                 "--directory", str(output), "--candidates", str(count), "--profile", profile],
                cwd=root, env=environment, stdout=log, stderr=subprocess.STDOUT,
                **hidden_subprocess_kwargs(),
            )
        assert result.returncode == 0, f"controlled probe failed; evidence: {pair}"
        cold = json.loads((output / "result-cold.json").read_text())
        warm = json.loads((output / "result-warm.json").read_text())
        assert all(cold["controlled_gates"].values()), (pair, cold["controlled_gates"])
        assert all(warm["controlled_gates"].values()), (pair, warm["controlled_gates"])
        cold_times.append(cold["report"]["source_stream_time_to_pool_target_ms"])
        warm_times.append(warm["report"]["source_stream_time_to_pool_target_ms"])
        assert cold["accepted_pass_cache_records"] >= cold["persisted_nodes"] >= 25
        assert warm["accepted_pass_cache_records"] >= warm["persisted_nodes"] >= 25
        assert cold["scope"] == warm["scope"] == "local_controlled_not_network_acceptance"
    comparison = {"cold_ms": cold_times, "warm_ms": warm_times,
                  "relative_limit": 1.1, "scope": "local_controlled_not_network_acceptance"}
    (area / "paired-result.json").write_text(json.dumps(comparison, indent=2))
    assert median(warm_times) <= median(cold_times) * 1.1, (area, comparison)
