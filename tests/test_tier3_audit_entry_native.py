from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import time

import pytest

from scripts.tier3_audit_socket_policy import physical_process_receipt_failures
from scripts.tier3_audit_job_inventory import WindowsAuditJobInventory
from scripts.tier3_audit_job_launch import AuditJobLaunchRegistry, system_console_host_path
from scripts.tier3_audit_process_census import audit_process_census
from tier3_windows_job import WindowsJob


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows Job-owned helper entry')
@pytest.mark.parametrize('workers', [4, 16])
def test_native_helpers_install_same_manifest_before_reject_only_prewarm(tmp_path, workers):
    """Local process proof only; this is not a physical-route performance audit."""
    root = Path.cwd()
    config, data = tmp_path / 'config', tmp_path / 'data'
    config.mkdir()
    data.mkdir()
    temporary = tmp_path / 'temp'
    temporary.mkdir()
    manifest = tmp_path / 'audit.json'
    manifest.write_text(json.dumps(dict(
        schema=1, audit_id='b' * 32, interface='loopback-test-only', interface_index=1,
        dns_controller_port=19110, dns_listener_port=19111,
        config_dir=str(config), data_dir=str(data),
    )))
    env = dict(os.environ, TESSERACT_ISOLATED_AUDIT_MANIFEST=str(manifest),
               TESSERACT_CONFIG_DIR=str(config), TESSERACT_DATA_DIR=str(data),
               TEMP=str(temporary), TMP=str(temporary))
    # Resolver-role lifecycle only: no upstream DNS, network listener or remote
    # traffic. Packet-path proof must come from the independent real audit.
    resolver_config = tmp_path / 'resolver.yaml'
    resolver_config.write_text('dns:\n  enable: false\nrules:\n  - MATCH,REJECT\nlog-level: error\n')
    log_path = tmp_path / 'application.log'
    with WindowsJob() as job, WindowsAuditJobInventory(job, observe_images=True) as witness, AuditJobLaunchRegistry() as launches, log_path.open('wb') as log, (tmp_path / 'resolver.log').open('wb') as resolver_log:
        resolver = launches.launch(job, 'resolver',
            [str(root / 'bin/mihomo.exe'), '-d', str(tmp_path), '-f', str(resolver_config)],
            config_path=resolver_config, stdout=resolver_log, cwd=root, env=env)
        process = launches.launch(job, 'application',
            [str(root / '.venv/Scripts/pythonw.exe'), 'scripts/tier3_helper_launch_diagnostic.py',
             '--bulkhead', '4', '--workers', str(workers), '--native', '--query-version',
             '--query-platform-version'],
            config_path=manifest, cwd=root, env=env, stdout=log)
        code = process.wait(timeout=35)
        assert resolver.poll() is None
        resolver.terminate()
        resolver.wait(timeout=5)
        deadline = time.monotonic() + 2
        while job.snapshot().active_processes and time.monotonic() < deadline:
            time.sleep(.01)
        kernel = witness.snapshot()
        launch_receipts = launches.snapshot()
    output = log_path.read_text(encoding='utf-8')
    assert code == 0, output
    report = json.loads(output)
    assert report['active'] == workers and report['stop'] == 'STOPPED'
    receipts = {value['process_id']: value for path in data.glob('physical-config-control-*.json')
                if (value := json.loads(path.read_text()))}
    parents = [pid for pid, receipt in receipts.items()
               if receipt.get('process_inventory', {}).get('attempts')]
    assert len(parents) == 1 and len(receipts) == workers + 1
    parent = receipts[parents[0]]
    commands = parent['command_process_inventory']['attempts']
    mihomo_digest = hashlib.sha256((root / 'bin/mihomo.exe').read_bytes()).hexdigest()
    cmd_digest = hashlib.sha256(system_console_host_path().with_name('cmd.exe').read_bytes()).hexdigest()
    assert sum(command['executable_sha256'] == mihomo_digest for command in commands) == 1
    assert any(command['executable_sha256'] == cmd_digest for command in commands)
    assert all(command['exit_confirmed'] and command['exitcode'] == 0
               and type(command['pid']) is int for command in commands)
    assert not physical_process_receipt_failures(receipts, parent_pid=parents[0], require_core=False,
                                                interface='loopback-test-only')
    for attempt in parent['process_inventory']['attempts']:
        receipt = receipts[attempt['pid']]
        assert receipt['completed'] and receipt['received_prewarm'] == 1
        assert receipt['manifest_sha256'] == parent['manifest_sha256']
        native = receipt['mihomo_processes']
        assert len(native) == 1 and type(native[0]['exitcode']) is int
        assert native[0]['pid'] in attempt['job_process_ids']
    assert all(receipt['audit_id'] == 'b' * 32 for receipt in receipts.values())
    census = audit_process_census(kernel, receipts, launch_receipts,
                                   console_host_path=system_console_host_path())
    (tmp_path / 'kernel-census.json').write_text(json.dumps(dict(kernel=kernel, census=census)))
    assert not census['failures'], (census, kernel)
    # Replay notification loss without changing the independently read process
    # handles. The original kernel record above stays intact for diagnosis.
    lost_exits = dict(kernel, events=[item for item in kernel['events']
                                     if item['message'] not in (7, 8)])
    assert not audit_process_census(lost_exits, receipts, launch_receipts,
                                    console_host_path=system_console_host_path())['failures']
