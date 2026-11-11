"""A provider PUT's HTTP 503 on the bundled Mihomo 1.19.27 (review F16, 11-02).

Mihomo answers 503 for any provider.Update() error.  A read error (another
process holding the provider file without FILE_SHARE_READ) is retried like a
transport failure: it commits once the file is readable, and a lock that
outlasts every PUT leaves the write unknown (parked, never restored, never
remembered).  Only a parse error parks the write and restores the file, and
that verdict holds for the Core that gave it.  Drives the real DesktopRuntime
verified write through the ``_Lab`` harness (127.0.0.1 fixtures only).
"""

from __future__ import annotations

import ctypes
import sys
import threading
import time
from contextlib import ExitStack

import pytest
import yaml

from desktop_runtime import ProviderSyncDisposition
from mihomo_api import MihomoAPIError
from tests.test_tier3_verified_provider_writes_real_core import _lab
from tier3_core_provider import Tier3CoreProviderProjection

windows_lock = pytest.mark.skipif(sys.platform != "win32", reason="a Windows share-mode lock")


def _hold_without_read_sharing(path):
    """Open ``path`` with share mode 0: Mihomo's os.ReadFile then fails."""

    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel32.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = create(str(path), 0x80000000, 0, None, 3, 0, None)  # GENERIC_READ, OPEN_EXISTING
    if handle in (None, wintypes.HANDLE(-1).value):
        raise OSError(ctypes.get_last_error(), "could not hold the provider file")
    released = threading.Event()

    def release():
        if not released.is_set():
            released.set()
            kernel32.CloseHandle(handle)

    return release, released


def _unparsable(lab, members):
    projection = lab.projection(members)
    document = yaml.safe_load(projection.provider_yaml)
    document['proxies'][-1]['type'] = 'not-a-protocol'
    return Tier3CoreProviderProjection(**{
        **{field: getattr(projection, field) for field in projection.__dataclass_fields__},
        'provider_yaml': yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
    })


def _recording_put(lab, stack, hold_seconds, *, inline=False):
    """Wrap the PUT: the first one runs under a share-mode-0 lock held for
    ``hold_seconds`` (``inline``: the PUT returns only after the release);
    every PUT records its error and the write's busy flag."""

    real_put = lab.client.update_proxy_provider
    puts: list[BaseException | None] = []
    busy: list[bool] = []
    held: dict[str, threading.Event] = {}

    def put(name, *, timeout=None):
        busy.append(bool(lab.runtime._tier3_provider_write_busy))
        release = None
        if not held:
            release, held['released'] = _hold_without_read_sharing(lab.provider_path)
            stack.callback(release)
            if not inline:
                threading.Timer(hold_seconds, release).start()
                release = None
        try:
            result = real_put(name, timeout=timeout)
        except Exception as exc:
            puts.append(exc)
            raise
        finally:
            if release is not None:
                time.sleep(hold_seconds)
                release()
        puts.append(None)
        return result

    lab.client.update_proxy_provider = put
    return puts, busy, held


@windows_lock
def test_a_read_error_during_the_put_is_retried_and_commits(tmp_path):
    with ExitStack() as stack:
        lab = _lab(stack, tmp_path, names=['n1', 'n2', 'n3'], loaded=['n1', 'n2'])
        # Past the file watcher's 100 ms re-read, which fails too; released
        # before the first PUT's error returns, so the retry can read it.
        puts, busy, _held = _recording_put(lab, stack, 0.5, inline=True)
        real_verify = lab.runtime._verify_tier3_provider_after_write
        verify_busy: list[bool] = []

        def verify(result):
            verify_busy.append(bool(lab.runtime._tier3_provider_write_busy))
            try:
                return real_verify(result)
            finally:
                verify_busy.append(bool(lab.runtime._tier3_provider_write_busy))

        lab.runtime._verify_tier3_provider_after_write = verify
        outcome = lab.sync(['n1', 'n2', 'n3'])

        assert outcome.disposition is ProviderSyncDisposition.COMMITTED, (outcome, lab.lines[-20:])
        first = puts[0]
        assert type(first) is MihomoAPIError, first  # not a rejection
        assert first.controller_message.startswith('open ') and 'tier3b-runtime.yaml' in first.controller_message
        assert len(puts) >= 2 and puts[-1] is None  # the transport retry
        # Judgements stay out of the whole write, its member tests included (F7).
        assert all(busy) and verify_busy == [True, True], (busy, verify_busy)
        assert lab.runtime._tier3_provider_write_busy is False
        assert lab.members() == ['n1', 'n2', 'n3'] and lab.group()['now'] == 'n1'
        assert lab.runtime._tier3_rejected_provider_digests == {}


@windows_lock
def test_a_read_error_outlasting_every_put_is_unknown_and_never_restored(tmp_path):
    with ExitStack() as stack:
        lab = _lab(stack, tmp_path, names=['n1', 'n2', 'n3'], loaded=['n1', 'n2'])
        runtime = lab.runtime
        puts, busy, held = _recording_put(lab, stack, 3.0)  # outlasts 3 PUTs and the readbacks

        first = lab.sync(['n1', 'n2', 'n3'])

        assert first.disposition is ProviderSyncDisposition.PARKED, first
        assert not str(first.reason).startswith('controller rejected'), first
        assert len(puts) == 3 and all(type(exc) is MihomoAPIError for exc in puts), puts
        assert all(busy), busy
        assert runtime._tier3_rejected_provider_digests == {}
        assert lab.members() == ['n1', 'n2'] and lab.group()['now'] == 'n1'
        assert held['released'].wait(10)
        assert 'n3' in lab.provider_path.read_text(encoding='utf-8')  # never restored

        runtime._tier3_provider_write_finished_at = 0.0
        second = lab.sync(['n1', 'n2', 'n3'])
        assert second.disposition is ProviderSyncDisposition.COMMITTED, (second, lab.lines[-20:])
        assert lab.members() == ['n1', 'n2', 'n3'] and lab.group()['now'] == 'n1'


def test_a_parse_rejection_parks_and_holds_for_that_core_only(tmp_path):
    with ExitStack() as stack:
        lab = _lab(stack, tmp_path, names=['n1', 'n2', 'n3'], loaded=['n1', 'n2'])
        runtime = lab.runtime
        bad = _unparsable(lab, ['n1', 'n2', 'n3'])
        before = lab.provider_path.read_bytes()

        first = lab.sync(projection=bad)
        assert first.disposition is ProviderSyncDisposition.PARKED, first
        assert first.reason == ('controller rejected the provider file: PUT /providers/proxies/'
                                'TIER3B-RUNTIME failed: HTTP 503 Service Unavailable'), first
        assert lab.provider_path.read_bytes() == before
        assert lab.members() == ['n1', 'n2'] and lab.group()['now'] == 'n1'

        runtime._tier3_provider_write_finished_at = 0.0
        again = lab.sync(projection=bad)
        assert again.disposition is ProviderSyncDisposition.PARKED, again
        assert again.reason == 'Mihomo already rejected this exact Tier3B provider projection'

        # A later Core generation parses the same bytes again (and refuses them).
        pid = runtime.core_runner.status()['pid']
        runtime._core_generation = 2
        runtime._runtime_initial_activation_generation = 2
        runtime._tier3_provider_write_finished_at = 0.0
        retried = lab.sync(projection=bad)
        assert retried.disposition is ProviderSyncDisposition.PARKED, retried
        assert str(retried.reason).startswith('controller rejected the provider file'), retried
        assert list(runtime._tier3_rejected_provider_digests.values()) == [(pid, 1, 2)]
        assert lab.provider_path.read_bytes() == before and lab.members() == ['n1', 'n2']
