"""T-P2-19: save grants, diagnostic bundle and filtered log export."""

import io
import json
import logging
import os
import random
import re
import string
import threading
import time
import zipfile
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest
import yaml

import bridge_exports as exports
from bridge_exports import (
    BundlePaths, ExecutorJobIdentity, ExecutorJobOutcomes, LogSnapshot, LogViewModel,
    RegistryKnownSecrets, SaveGrantTable, SecretPass, SessionExportSources, TerminalJob,
    UnavailableSaveTargets,
)
from bridge_jobs import JobEventRecorder, JobExecutor, JobFailure
from bridge_log_view import LogView
from bridge_redact import MAX_TEXT, KnownSecrets
from diagnostic_bundle import DiagnosticAttachment, DiagnosticBundleInputs, write_diagnostic_bundle


EPOCH, OWNER = "epoch-p2-19", "page-1"
NOW_MS = 1_790_000_000_000
SECRETS = {
    "S3cretPassw0rd!": "password",
    "tok_ABCDEF123456": "token",
    "https://sub.example.com/link?key=xyz": "subscription_url",
    "NodeUuidLikeValue-77": "uuid",
    "only-in-shell-9f8e7d": "secret",
    "café-secret ü/+": "secret",
    "registered-after-ingest-42": "password",
}


def forms(value):
    return {value, quote(value, safe=""), json.dumps(value)[1:-1],
            json.dumps(value, ensure_ascii=False)[1:-1]}


class Clock:
    def __init__(self):
        self.mono, self.wall = 1000.0, NOW_MS

    def monotonic(self):
        return self.mono

    def wall_ms(self):
        return self.wall

    def advance(self, seconds):
        self.mono += seconds
        self.wall += int(seconds * 1000)


class FileTarget:
    def __init__(self, owner, handle_id, path):
        self.owner, self.handle_id, self.path = owner, handle_id, path
        self.file = open(path, "wb")
        self.written = 0

    def write(self, data):
        if self.owner.fail_after is not None and self.written + len(data) > self.owner.fail_after:
            raise OSError("disk full")
        self.file.write(data)
        self.written += len(data)

    def commit(self):
        self.file.flush()
        os.fsync(self.file.fileno())
        self.file.close()
        self.owner.committed.append(self.handle_id)
        return self.path.stat().st_size

    def discard(self):
        self.file.close()
        self.path.unlink(missing_ok=True)
        self.owner.discarded.append(self.handle_id)


class DirTargets:
    """Test stand-in for the shell's duplicated handles (P3-06)."""

    def __init__(self, root):
        self.root = Path(root)
        self.handles, self.consumed = {}, set()
        self.released, self.discarded, self.committed, self.opened = [], [], [], []
        self.fail_after = None
        self.fail_open = False

    def add(self, handle_id, name):
        path = self.root / name
        path.write_bytes(b"")  # The shell created/approved the target.
        self.handles[handle_id] = path
        return path

    def verify(self, handle_id, *, purpose, format):
        return handle_id in self.handles and handle_id not in self.consumed

    def open(self, handle_id):
        if self.fail_open or handle_id not in self.handles or handle_id in self.consumed:
            raise OSError("handle unusable")
        self.consumed.add(handle_id)
        self.opened.append(handle_id)
        return FileTarget(self, handle_id, self.handles[handle_id])

    def release(self, handle_id):
        self.released.append(handle_id)


class FakeTimers:
    """Captures expiry timers instead of starting threads."""

    def __init__(self):
        self.timers = []

    def __call__(self, delay, callback):
        timer = SimpleNamespace(delay=delay, callback=callback, started=False, cancelled=False)
        timer.start = lambda: setattr(timer, "started", True)
        timer.cancel = lambda: setattr(timer, "cancelled", True)

        def fire():
            timer.cancelled = True  # A fired timer is spent.
            callback()

        timer.fire = fire
        self.timers.append(timer)
        return timer

    def live(self):
        return [timer for timer in self.timers if timer.started and not timer.cancelled]


class Identity:
    def __init__(self, value=(EPOCH, OWNER)):
        self.value = value

    def job_identity(self, op):
        return self.value


def ExecutorIdentity(executor):
    """T-P2-31a: the real adapter over the public ``JobExecutor.job_identity``."""
    return ExecutorJobIdentity(lambda: executor)


class Sources:
    def __init__(self, tmp_path, values=None, published=None):
        self.tmp = Path(tmp_path)
        self.values = dict(SECRETS if values is None else values)
        self.published = published if published is not None else {}
        self.paths = BundlePaths({}, {}, None, None, None, None)

    def known_secret_values(self):
        return dict(self.values)

    def bundle_paths(self):
        return self.paths

    def app_facts(self):
        return {"version": "9.9.9", "language": "zh_CN"}

    def published_facts(self):
        return dict(self.published)


def log_row(raw, **kwargs):
    base = dict(ts_ms=NOW_MS, rx_ms=NOW_MS, key="", source="MIHOMO", level="info",
                category="other", title="", detail="", raw=raw)
    base.update(kwargs)
    return base


def make_view(count=0, **bounds):
    view = LogView(**bounds)
    for index in range(count):
        view.append(log_row(f"line {index + 1}"))
    return view


def service(tmp_path, *, view=None, model=None, sources=None, identity=None, clock=None,
            timers=None):
    clock = clock or Clock()
    targets = DirTargets(tmp_path)
    view = view if view is not None else make_view()
    svc = exports.ExportService(
        targets=targets, identity=identity or Identity(),
        sources=sources or Sources(tmp_path),
        log_model=model or LogViewModel(lambda: view, wall_ms=clock.wall_ms),
        monotonic=clock.monotonic, wall_ms=clock.wall_ms,
        timer_factory=timers if timers is not None else FakeTimers(), draft_root=tmp_path)
    return svc, targets, clock


def grant(svc, targets, ref, purpose, fmt, handle_id, *, ttl_ms=60_000):
    targets.add(handle_id, f"{ref}.{fmt}")
    args = dict(ref=ref, purpose=purpose, format=fmt, handle_id=handle_id,
                expires_at_ms=svc._wall_ms() + ttl_ms)
    return svc.save_grant(args, SimpleNamespace(op=f"request-{handle_id}"))


CTX = SimpleNamespace(op="job-1")


def code_of(excinfo):
    return excinfo.value.code


# ------------------------------------------------------------ save grants
def test_grant_registers_and_is_consumed_exactly_once(tmp_path):
    svc, targets, _ = service(tmp_path, view=make_view(3))
    assert grant(svc, targets, "ref-a", "filtered", "json", 1) == {"registered": True}
    result = svc.export_filtered({"save_ref": "ref-a", "format": "json", "seqs": [1, 2]}, CTX)
    assert result == {"ref": "ref-a", "count": 2, "missing_seqs": []}
    with pytest.raises(JobFailure) as again:
        svc.export_filtered({"save_ref": "ref-a", "format": "json", "seqs": [1]}, CTX)
    assert code_of(again) == "not_allowed"
    # The consumed ref can never be registered again in this epoch.
    targets.add(9, "again.json")
    with pytest.raises(JobFailure) as reuse:
        svc.save_grant(dict(ref="ref-a", purpose="filtered", format="json", handle_id=9,
                            expires_at_ms=NOW_MS + 1000), CTX)
    assert code_of(reuse) == "conflict" and 9 in targets.released


def test_expired_grant_is_rejected_and_its_handle_released(tmp_path):
    svc, targets, clock = service(tmp_path, view=make_view(1))
    grant(svc, targets, "ref-exp", "bundle", "zip", 2, ttl_ms=10_000)
    clock.advance(10.001)
    with pytest.raises(JobFailure) as failure:
        svc.export_bundle({"save_ref": "ref-exp"}, CTX)
    assert code_of(failure) == "not_allowed"
    assert targets.released == [2] and targets.opened == []


def test_grant_lifetime_is_capped_at_five_minutes(tmp_path):
    svc, targets, clock = service(tmp_path, view=make_view(1))
    grant(svc, targets, "ref-long", "filtered", "text", 3, ttl_ms=24 * 3600 * 1000)
    clock.advance(300.001)
    assert svc.grants.sweep() == 1 and targets.released == [3]


def test_purpose_mismatch_is_rejected_and_grant_keeps_its_rightful_use(tmp_path):
    svc, targets, _ = service(tmp_path, view=make_view(2))
    grant(svc, targets, "ref-bundle", "bundle", "zip", 4)
    with pytest.raises(JobFailure) as failure:
        svc.export_filtered({"save_ref": "ref-bundle", "format": "json", "seqs": [1]}, CTX)
    assert code_of(failure) == "not_allowed" and targets.opened == []
    assert len(svc.grants) == 1  # Still usable by logs.exportBundle.


def test_format_mismatch_is_rejected(tmp_path):
    svc, targets, _ = service(tmp_path, view=make_view(2))
    grant(svc, targets, "ref-json", "filtered", "json", 5)
    with pytest.raises(JobFailure) as failure:
        svc.export_filtered({"save_ref": "ref-json", "format": "csv", "seqs": [1]}, CTX)
    assert code_of(failure) == "not_allowed" and targets.opened == []
    result = svc.export_filtered({"save_ref": "ref-json", "format": "json", "seqs": [1]}, CTX)
    assert result["count"] == 1


def test_other_owner_or_epoch_cannot_consume(tmp_path):
    svc, targets, _ = service(tmp_path, view=make_view(1))
    grant(svc, targets, "ref-own", "filtered", "json", 6)
    for other in ((EPOCH, "page-2"), ("old-epoch", OWNER), None):
        svc.identity = Identity(other)
        with pytest.raises(JobFailure) as failure:
            svc.export_filtered({"save_ref": "ref-own", "format": "json", "seqs": [1]}, CTX)
        assert code_of(failure) == "not_allowed"
    svc.identity = Identity()
    assert svc.export_filtered({"save_ref": "ref-own", "format": "json", "seqs": [1]}, CTX)["count"] == 1


def test_registration_rejections_release_only_handles_they_own(tmp_path):
    svc, targets, _ = service(tmp_path)
    grant(svc, targets, "ref-1", "filtered", "csv", 10)
    base = dict(purpose="filtered", format="csv", expires_at_ms=NOW_MS + 5000)
    targets.add(11, "dup.csv")
    with pytest.raises(JobFailure) as dup_ref:
        svc.save_grant(dict(base, ref="ref-1", handle_id=11), CTX)
    assert code_of(dup_ref) == "conflict" and targets.released == [11]
    with pytest.raises(JobFailure) as dup_handle:
        svc.save_grant(dict(base, ref="ref-2", handle_id=10), CTX)
    assert code_of(dup_handle) == "conflict" and 10 not in targets.released
    with pytest.raises(JobFailure) as unknown_handle:
        svc.save_grant(dict(base, ref="ref-3", handle_id=77), CTX)
    assert code_of(unknown_handle) == "not_allowed" and 77 in targets.released
    targets.add(12, "late.csv")
    with pytest.raises(JobFailure) as expired:
        svc.save_grant(dict(base, ref="ref-4", handle_id=12, expires_at_ms=NOW_MS), CTX)
    assert code_of(expired) == "validation" and 12 in targets.released
    targets.add(13, "mismatch.zip")
    with pytest.raises(JobFailure) as wrong_format:
        svc.save_grant(dict(base, ref="ref-5", handle_id=13, format="zip"), CTX)
    assert code_of(wrong_format) == "validation" and 13 in targets.released
    svc.identity = Identity(None)
    targets.add(14, "anon.csv")
    with pytest.raises(JobFailure) as anonymous:
        svc.save_grant(dict(base, ref="ref-6", handle_id=14), CTX)
    assert code_of(anonymous) == "not_allowed" and 14 in targets.released
    assert len(svc.grants) == 1


def test_grant_capacity_per_owner(tmp_path):
    svc, targets, _ = service(tmp_path)
    for index in range(exports.GRANT_OWNER_CAPACITY):
        grant(svc, targets, f"cap-{index}", "filtered", "json", 100 + index)
    with pytest.raises(JobFailure) as failure:
        grant(svc, targets, "cap-over", "filtered", "json", 200)
    assert code_of(failure) == "job_capacity" and 200 in targets.released


def test_owner_revoke_and_close_release_unconsumed_grants(tmp_path):
    svc, targets, _ = service(tmp_path)
    grant(svc, targets, "rv-1", "filtered", "json", 20)
    svc.identity = Identity((EPOCH, "page-9"))
    grant(svc, targets, "rv-2", "bundle", "zip", 21)
    svc.revoke_owner(OWNER)
    assert targets.released == [20] and len(svc.grants) == 1
    svc.close()
    assert targets.released == [20, 21] and len(svc.grants) == 0


class SlowVerifyTargets(DirTargets):
    def __init__(self, root):
        super().__init__(root)
        self.entered, self.proceed = threading.Event(), threading.Event()

    def verify(self, handle_id, *, purpose, format):
        if handle_id == 999:
            self.entered.set()
            assert self.proceed.wait(5)
        return super().verify(handle_id, purpose=purpose, format=format)


def test_handle_verification_runs_outside_the_table_lock(tmp_path):
    clock = Clock()
    targets = SlowVerifyTargets(tmp_path)
    table = SaveGrantTable(targets, monotonic=clock.monotonic, wall_ms=clock.wall_ms,
                           timer_factory=None)
    base = dict(purpose="filtered", format="json", expires_at_ms=NOW_MS + 60_000)
    targets.add(1, "ready.json")
    assert table.register(dict(base, ref="ready", handle_id=1), (EPOCH, OWNER))
    targets.add(999, "slow.json")
    outcome = {}
    slow = threading.Thread(target=lambda: outcome.setdefault(
        "slow", table.register(dict(base, ref="slow", handle_id=999), (EPOCH, OWNER))))
    slow.start()
    assert targets.entered.wait(5)
    # While the slow check runs, consume/sweep/revoke proceed, and the
    # reservation already blocks a duplicate ref or handle.
    assert table.consume("ready", "filtered", "json", (EPOCH, OWNER)).handle_id == 1
    assert table.sweep() == 0
    for duplicate in (dict(base, ref="slow", handle_id=5), dict(base, ref="other", handle_id=999)):
        targets.add(duplicate["handle_id"], f"dup-{duplicate['handle_id']}.json")
        with pytest.raises(JobFailure) as conflict:
            table.register(duplicate, (EPOCH, OWNER))
        assert code_of(conflict) == "conflict"
    assert 999 not in targets.released  # The reservation owns that handle.
    targets.proceed.set()
    slow.join(5)
    assert outcome["slow"] is True and len(table) == 1


def test_owner_revoked_during_verification_gets_no_grant(tmp_path):
    clock = Clock()
    targets = SlowVerifyTargets(tmp_path)
    table = SaveGrantTable(targets, monotonic=clock.monotonic, wall_ms=clock.wall_ms,
                           timer_factory=None)
    targets.add(999, "slow.json")
    outcome = {}

    def register():
        try:
            table.register(dict(ref="late", purpose="filtered", format="json", handle_id=999,
                                expires_at_ms=NOW_MS + 60_000), (EPOCH, OWNER))
        except JobFailure as failure:
            outcome["code"] = failure.code

    thread = threading.Thread(target=register)
    thread.start()
    assert targets.entered.wait(5)
    table.revoke_owner(OWNER)
    targets.proceed.set()
    thread.join(5)
    assert outcome["code"] == "not_allowed" and targets.released == [999] and len(table) == 0


def test_expiry_timer_releases_handles_without_further_traffic(tmp_path):
    timers = FakeTimers()
    svc, targets, clock = service(tmp_path, timers=timers)
    grant(svc, targets, "t-late", "filtered", "json", 90, ttl_ms=60_000)
    grant(svc, targets, "t-soon", "filtered", "json", 91, ttl_ms=10_000)
    live = timers.live()
    assert len(live) == 1 and 9.9 < live[0].delay < 10.2  # Re-armed for the earliest.
    clock.advance(10.0)
    live[0].fire()
    assert targets.released == [91] and len(svc.grants) == 1
    (rearmed,) = timers.live()
    assert 49.9 < rearmed.delay < 50.2
    clock.advance(50.0)
    rearmed.fire()
    assert targets.released == [91, 90] and len(svc.grants) == 0 and timers.live() == []
    svc.close()


def test_real_timer_closes_an_expired_handle(tmp_path):
    svc, targets, _ = service(tmp_path, timers=threading.Timer,
                              clock=SimpleNamespace(monotonic=time.monotonic,
                                                    wall_ms=lambda: time.time_ns() // 1_000_000))
    grant(svc, targets, "real", "bundle", "zip", 92, ttl_ms=150)
    deadline = time.monotonic() + 5
    while 92 not in targets.released and time.monotonic() < deadline:
        time.sleep(0.02)
    assert targets.released == [92] and len(svc.grants) == 0
    svc.close()


def test_default_adapters_fail_closed(tmp_path):
    identity = ExecutorJobIdentity(lambda: object())
    assert identity.job_identity("job-1") is None
    table = SaveGrantTable(UnavailableSaveTargets())
    with pytest.raises(JobFailure) as failure:
        table.register(dict(ref="r", purpose="bundle", format="zip", handle_id=1,
                            expires_at_ms=time.time_ns() // 1_000_000 + 60_000), (EPOCH, OWNER))
    assert code_of(failure) == "not_allowed"
    assert LogViewModel(lambda: None).export_snapshot() is None


# ------------------------------------------------------- filtered export
def test_filtered_export_formats_and_csv_formula_guard(tmp_path):
    view = make_view()
    view.append(log_row("=HYPERLINK(\"x\")", level="warn", category="routing"))
    view.append(log_row("plain info"))
    view.append(log_row("boundary", key="source_boundary", level="debug"))
    svc, targets, _ = service(tmp_path, view=view)
    outputs = {}
    for index, fmt in enumerate(("json", "text", "csv")):
        grant(svc, targets, f"fmt-{fmt}", "filtered", fmt, 30 + index)
        result = svc.export_filtered(
            {"save_ref": f"fmt-{fmt}", "format": fmt, "filter": {"levels": ["warn"]}}, CTX)
        assert result == {"ref": f"fmt-{fmt}", "count": 2, "missing_seqs": []}
        outputs[fmt] = targets.handles[30 + index].read_bytes()
    document = json.loads(outputs["json"])
    assert [r["seq"] for r in document["records"]] == [1, 3]  # Boundary never hidden.
    assert document["selection"] == "filter" and document["watermark"] == 3
    assert set(document["records"][0]) == set(exports._LOG_FIELDS)
    text = outputs["text"].decode()
    assert "#1 [MIHOMO] [warn] [routing]" in text and "plain info" not in text
    rows = outputs["csv"].decode("utf-8-sig").splitlines()
    assert rows[0].split(",")[:3] == ["seq", "time", "source"]
    assert "'=HYPERLINK" in rows[1]


def test_seqs_evicted_before_export_are_reported_missing(tmp_path):
    view = make_view(8, max_items=5)  # Retains 4..8.
    svc, targets, _ = service(tmp_path, view=view)
    grant(svc, targets, "ev", "filtered", "json", 40)
    result = svc.export_filtered({"save_ref": "ev", "format": "json", "seqs": [99, 6, 1, 2, 7, 6]}, CTX)
    assert result == {"ref": "ev", "count": 2, "missing_seqs": [1, 2, 99]}
    document = json.loads(targets.handles[40].read_bytes())
    assert [r["seq"] for r in document["records"]] == [6, 7]
    assert document["missing_seqs"] == [1, 2, 99]


def test_ring_eviction_during_export_loses_nothing(tmp_path):
    view = make_view(6, max_items=6)
    inner = LogViewModel(lambda: view)

    class EvictingModel:
        def export_snapshot(self):
            snapshot = inner.export_snapshot()
            for index in range(20):  # The ring keeps turning while we export.
                view.append(log_row(f"late {index}"))
            return snapshot

    svc, targets, _ = service(tmp_path, model=EvictingModel())
    grant(svc, targets, "turn", "filtered", "json", 41)
    result = svc.export_filtered({"save_ref": "turn", "format": "json", "filter": {}}, CTX)
    assert view.min_seq > 6  # Every exported record has since been evicted.
    document = json.loads(targets.handles[41].read_bytes())
    assert [r["seq"] for r in document["records"]] == [1, 2, 3, 4, 5, 6]
    assert result["count"] == 6 and result["missing_seqs"] == []


def test_concurrent_ring_eviction_never_tears_an_export(tmp_path):
    view = make_view(200, max_items=200)
    stop = threading.Event()

    def writer():
        index = 0
        while not stop.is_set():
            index += 1
            view.append(log_row(f"concurrent {index}"))

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    try:
        svc, targets, _ = service(tmp_path, view=view)
        for attempt in range(20):
            grant(svc, targets, f"race-{attempt}", "filtered", "json", 300 + attempt)
            result = svc.export_filtered(
                {"save_ref": f"race-{attempt}", "format": "json", "filter": {}}, CTX)
            document = json.loads(targets.handles[300 + attempt].read_bytes())
            seqs = [record["seq"] for record in document["records"]]
            # One atomic window: contiguous, ends at the watermark, no holes.
            assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))
            assert seqs[-1] == document["watermark"] and result["missing_seqs"] == []
            evicted = document.get("evicted_before_export")
            assert evicted is None or evicted["to_seq"] == seqs[0] - 1
    finally:
        stop.set()
        thread.join(5)


def test_two_workers_racing_for_one_grant_consume_it_once(tmp_path):
    svc, targets, _ = service(tmp_path, view=make_view(3))
    grant(svc, targets, "race", "filtered", "json", 310)
    barrier, outcomes = threading.Barrier(4), []

    def worker():
        barrier.wait()
        try:
            outcomes.append(svc.export_filtered({"save_ref": "race", "format": "json", "seqs": [1]}, CTX))
        except JobFailure as failure:
            outcomes.append(failure.code)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert sorted(map(str, outcomes)).count("not_allowed") == 3
    assert targets.opened == [310]


def test_filter_mode_states_the_evicted_range_in_every_format(tmp_path):
    view = make_view(10, max_items=4)  # Retains 7..10; 1..6 evicted.
    svc, targets, _ = service(tmp_path, view=view)
    files = {}
    for index, fmt in enumerate(("json", "text", "csv")):
        grant(svc, targets, f"scope-{fmt}", "filtered", fmt, 42 + index)
        result = svc.export_filtered({"save_ref": f"scope-{fmt}", "format": fmt, "filter": {}}, CTX)
        # Evicted before the snapshot: stated as a range, not listed seq by seq.
        assert result == {"ref": f"scope-{fmt}", "count": 4, "missing_seqs": []}
        files[fmt] = targets.handles[42 + index].read_bytes().decode("utf-8-sig")
    document = json.loads(files["json"])
    assert document["evicted_before_export"] == {"from_seq": 1, "to_seq": 6, "count": 6}
    # T-P2-31b: LogView.clear_seq is public; a never-cleared view reports 0.
    assert document["view_clear_seq"] == 0 and document["missing_seqs"] == []
    note = "# evicted before export, not checked against the filter: 1-6 (6 records)"
    assert note in files["text"].splitlines()
    rows = files["csv"].splitlines()
    assert rows[0] == note and rows[1].split(",")[:3] == ["seq", "time", "source"]


def test_filter_mode_holes_beyond_the_array_cap_require_a_snapshot(tmp_path, monkeypatch):
    rows = tuple(make_view(12).snapshot())
    holed = (rows[0], rows[-1])  # 2..11 absent inside the retained window.

    class HoledModel:
        def export_snapshot(self):
            return LogSnapshot(holed, 1, 12, None, NOW_MS)

    svc, targets, _ = service(tmp_path, model=HoledModel())
    grant(svc, targets, "holes", "filtered", "json", 46)
    result = svc.export_filtered({"save_ref": "holes", "format": "json", "filter": {}}, CTX)
    assert result["missing_seqs"] == list(range(2, 12))
    monkeypatch.setattr(exports, "MISSING_SEQS_MAX", 9)
    grant(svc, targets, "holes-2", "filtered", "text", 47)
    with pytest.raises(JobFailure) as failure:
        svc.export_filtered({"save_ref": "holes-2", "format": "text", "filter": {}}, CTX)
    assert code_of(failure) == "snapshot_required"
    assert 47 in targets.released and 47 not in targets.opened


def test_long_session_filter_export_succeeds_with_the_default_bounds(tmp_path):
    view = make_view(20_001)  # Default LogView keeps 10,000; 10,001 evicted.
    svc, targets, _ = service(tmp_path, view=view)
    grant(svc, targets, "long", "filtered", "json", 48)
    result = svc.export_filtered(
        {"save_ref": "long", "format": "json", "filter": {"text": "line 2000"}}, CTX)
    assert result["missing_seqs"] == []
    document = json.loads(targets.handles[48].read_bytes())
    assert document["evicted_before_export"] == {"from_seq": 1, "to_seq": 10_001, "count": 10_001}
    assert [r["seq"] for r in document["records"]] == [20_000, 20_001]
    assert result["count"] == 2


def test_filter_mode_respects_a_known_clear_watermark(tmp_path):
    view = make_view(10, max_items=4)
    rows = tuple(view.snapshot())

    class ClearedModel:
        def export_snapshot(self):
            return LogSnapshot(rows, 7, 10, 8, NOW_MS)

    svc, targets, _ = service(tmp_path, model=ClearedModel())
    grant(svc, targets, "clr", "filtered", "json", 44)
    result = svc.export_filtered({"save_ref": "clr", "format": "json", "filter": {}}, CTX)
    assert result == {"ref": "clr", "count": 2, "missing_seqs": []}
    document = json.loads(targets.handles[44].read_bytes())
    # Records 7..8 were cleared from the view, not evicted: no evicted range.
    assert document["view_clear_seq"] == 8 and "evicted_before_export" not in document


def test_invalid_selection_keeps_the_grant(tmp_path, monkeypatch):
    svc, targets, _ = service(tmp_path, view=make_view(2))
    grant(svc, targets, "keep", "filtered", "json", 45)
    with pytest.raises(JobFailure) as bad_filter:
        svc.export_filtered({"save_ref": "keep", "format": "json", "filter": {"text": "x" * 20_000}}, CTX)
    monkeypatch.setattr(exports, "SELECTED_SEQS_MAX", 2)
    with pytest.raises(JobFailure) as too_many:
        svc.export_filtered({"save_ref": "keep", "format": "json", "seqs": [1, 2, 3]}, CTX)
    assert code_of(bad_filter) == code_of(too_many) == "validation"
    assert len(svc.grants) == 1


def test_write_failure_is_structured_and_removes_the_partial_file(tmp_path, monkeypatch):
    monkeypatch.setattr(exports, "WRITE_CHUNK", 16)
    svc, targets, _ = service(tmp_path, view=make_view(5))
    grant(svc, targets, "wf", "filtered", "json", 50)
    path = targets.handles[50]
    targets.fail_after = 40
    with pytest.raises(JobFailure) as failure:
        svc.export_filtered({"save_ref": "wf", "format": "json", "filter": {}}, CTX)
    assert code_of(failure) == "io"
    assert targets.discarded == [50] and not path.exists()
    with pytest.raises(JobFailure) as retry:  # One consumption; no default path.
        svc.export_filtered({"save_ref": "wf", "format": "json", "filter": {}}, CTX)
    assert code_of(retry) == "not_allowed"


def test_open_failure_releases_the_handle(tmp_path):
    svc, targets, _ = service(tmp_path, view=make_view(1))
    grant(svc, targets, "of", "filtered", "csv", 51)
    targets.fail_open = True
    with pytest.raises(JobFailure) as failure:
        svc.export_filtered({"save_ref": "of", "format": "csv", "seqs": [1]}, CTX)
    assert code_of(failure) == "io" and targets.released == [51]


def test_filtered_export_applies_the_current_registry(tmp_path):
    view = make_view()
    view.append(log_row("leaked registered-after-ingest-42 and caf%C3%A9-secret%20%C3%BC%2F%2B"))
    svc, targets, _ = service(tmp_path, view=view)
    for index, fmt in enumerate(("json", "text", "csv")):
        grant(svc, targets, f"reg-{fmt}", "filtered", fmt, 60 + index)
        svc.export_filtered({"save_ref": f"reg-{fmt}", "format": fmt, "seqs": [1]}, CTX)
        text = targets.handles[60 + index].read_bytes().decode("utf-8-sig")
        for value in SECRETS:
            assert not any(form in text for form in forms(value))
        assert "[redacted:password]" in text


def test_registry_covers_upper_case_unicode_escapes_and_reserved_url_forms():
    value = "café-secret ü/+"
    secrets = SecretPass({value: "secret", "https://sub.example.com/link?key=xyz": "subscription_url"})
    upper = json.dumps(value)[1:-1].replace("\\u00e9", "\\u00E9").replace("\\u00fc", "\\u00FC")
    reserved = quote(value, safe=":/?#[]@!$&'()*+,;=")
    url_reserved = quote("https://sub.example.com/link?key=xyz", safe=":/?#[]@!$&'()*+,;=")
    for form in (upper, reserved, url_reserved):
        assert secrets.leaks(f"x {form} y")
        assert form not in secrets.text(f"x {form} y")


def test_structural_integers_survive_a_low_entropy_registry_value(tmp_path):
    view = make_view(3)
    sources = Sources(tmp_path, values={"2": "secret", "S3cretPassw0rd!": "password"})
    svc, targets, _ = service(tmp_path, view=view, sources=sources)
    grant(svc, targets, "low", "filtered", "json", 64)
    svc.export_filtered({"save_ref": "low", "format": "json", "seqs": [1, 2, 3, 9]}, CTX)
    document = json.loads(targets.handles[64].read_bytes())
    assert [record["seq"] for record in document["records"]] == [1, 2, 3]
    assert document["missing_seqs"] == [9] and document["count"] == 3
    # Content still loses the registered spelling.
    assert document["records"][1]["raw"] == "line [redacted:secret]"


def flat_reference(values, text):
    pattern = re.compile("|".join(re.escape(v) for v in sorted(values, key=lambda v: (-len(v), v))))
    return pattern.sub(lambda m: "<" + m[0] + ">", text)


def test_prefix_factored_windowed_scan_equals_a_flat_whole_text_scan(monkeypatch):
    rng = random.Random(19)
    values = ["ab", "abc", "abcdef", "bcd", "cde", "zz", "a", "q" * 40, "ababab"]
    values += ["".join(rng.choice("abcdz") for _ in range(rng.randint(2, 9))) for _ in range(40)]
    text = "".join(rng.choice("abcdzq ") for _ in range(20_000))
    expected = flat_reference(values, text)
    for chunk in (1, 3, 7, 64, 16 * 1024):
        monkeypatch.setattr(exports, "SCAN_CHUNK", chunk)
        secrets = SecretPass({v: "secret" for v in values})
        secrets._replacements = {form: "<" + form + ">" for form in secrets._replacements}
        assert secrets.text(text) == expected, chunk
        assert secrets.leaks(text) and not secrets.leaks("y" * 100)
    known = RegistryKnownSecrets({v: "secret" for v in values})
    assert known.replace(text) == KnownSecrets({v: "secret" for v in values}).replace(text)


def test_large_registry_scan_keeps_other_threads_running():
    rng = random.Random(7)
    alphabet = string.ascii_letters + string.digits
    values = {"".join(rng.choice(alphabet) for _ in range(28)): "secret" for _ in range(1500)}
    values.update({f"https://sub{i}.example.com/s?token={i:08d}x": "subscription_url"
                   for i in range(300)})
    chosen = list(values)
    lines, size = [], 0
    while size < 3_000_000:
        middle = rng.choice(chosen) if rng.random() < 0.02 else "".join(rng.choice(alphabet) for _ in range(12))
        lines.append(f"2026-10-05 12:00:00 INFO dial node {middle} rtt=12ms")
        size += len(lines[-1]) + 1
    text = "\n".join(lines)
    stall, stop = [0.0], threading.Event()

    def ticker():
        last = time.perf_counter()
        while not stop.is_set():
            time.sleep(0.005)
            now = time.perf_counter()
            stall[0], last = max(stall[0], now - last), now

    thread = threading.Thread(target=ticker)
    thread.start()
    try:
        secrets = SecretPass(values)
        cleaned = secrets.text(text)
        leaked = secrets.leaks(cleaned)
    finally:
        stop.set()
        thread.join(5)
    assert not leaked and secrets.replacements > 0
    assert not any(value in cleaned for value in chosen[:200])
    assert stall[0] < 0.5, stall[0]


# ---------------------------------------------------------------- bundle
def bundle_fixture(tmp_path):
    home = tmp_path / "home" / "alice"
    data = tmp_path / "data"
    logs = data / "logs"
    logs.mkdir(parents=True)
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({
        "client": {"password": "S3cretPassw0rd!"},
        "alerts": {"channels": [{"type": "telegram", "token": "tok_ABCDEF123456"}]},
        "subscription_urls": ["https://sub.example.com/link?key=xyz"],
    }), encoding="utf-8")
    state = data / "runtime-state.json"
    state.write_text(json.dumps({"last_identity_note": "only-in-shell-9f8e7d",
                                 "port": 7890}), encoding="utf-8")
    monitor = data / "monitor.log"
    monitor.write_text("2026-10-05 node NodeUuidLikeValue-77 failed\n", encoding="utf-8")
    events = data / "events.jsonl"
    events.write_text(json.dumps({"message": "uses tok_ABCDEF123456"}) + "\n", encoding="utf-8")
    long_line = "x" * (MAX_TEXT + 10) + " only-in-shell-9f8e7d"
    (logs / "shell.log").write_text("\n".join([
        "2026-10-05T01:00:00Z INFO shell started",
        "2026-10-05T01:00:01Z WARN sidecar said only-in-shell-9f8e7d",
        "pipe url caf%C3%A9-secret%20%C3%BC%2F%2B",
        'frame {"v":"caf\\u00e9-secret \\u00fc/+"}',
        f"opened {home}\\AppData\\Tesseract",
    ]) + "\n", encoding="utf-8")
    (logs / "shell.log.1").write_text("rotated shell line\n", encoding="utf-8")
    (logs / "sidecar-stderr.log").write_text("\n".join([
        "Traceback (most recent call last):",
        "  config token=tok_ABCDEF123456",
        long_line,
        "faulthandler: thread 0x1",
    ]) + "\n", encoding="utf-8")
    sources = Sources(tmp_path, published={
        "service.state": {"produced_at_ms": NOW_MS - 5, "rev": 3,
                          "data": {"runtime": {"note": "tok_ABCDEF123456", "phase": "running"}}},
        "route.published": None,
    })
    sources.paths = BundlePaths({"config.yaml": config}, {"runtime-state.json": state},
                                monitor, events, logs, str(home))
    view = make_view()
    view.append(log_row("seed before registry: registered-after-ingest-42"))
    view.append(log_row("ordinary routing line", category="routing"))
    return sources, view


def zip_texts(payload):
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        return {info.filename.partition("/")[2]: archive.read(info).decode("utf-8")
                for info in archive.infolist()}


def test_bundle_contents_snapshot_times_and_no_registered_secret(tmp_path):
    sources, view = bundle_fixture(tmp_path)
    svc, targets, _ = service(tmp_path, view=view, sources=sources)
    grant(svc, targets, "bundle-1", "bundle", "zip", 70)
    result = svc.export_bundle({"save_ref": "bundle-1"}, CTX)
    payload = targets.handles[70].read_bytes()
    assert result["ref"] == "bundle-1" and result["size"] == len(payload)
    texts = zip_texts(payload)
    assert set(result["files"]) == set(texts)
    for name in ("logs/shell.log", "logs/shell.log.1", "logs/sidecar-stderr.log",
                 "logs/bridge-log.jsonl", "runtime-status.json", "config/config.yaml",
                 "state/runtime-state.json", "logs/monitor.log", "logs/events.jsonl"):
        assert name in texts, name
    for name, text in texts.items():
        for value in SECRETS:
            for form in forms(value):
                assert form not in text, (name, form)
    assert "shell started" in texts["logs/shell.log"]
    assert "rotated shell line" in texts["logs/shell.log.1"]
    assert "alice" not in texts["logs/shell.log"]  # Producer redaction removes user paths.
    assert "faulthandler: thread" in texts["logs/sidecar-stderr.log"]
    assert "ordinary routing line" in texts["logs/bridge-log.jsonl"]
    manifest = json.loads(texts["manifest.json"])
    assert "qt" not in manifest["app"] and "theme" not in manifest["app"]
    assert manifest["app"]["version"] == "9.9.9"
    times = manifest["snapshot_times"]
    assert times["published:service.state"] == NOW_MS - 5
    assert {"log_model", "shell.log", "sidecar-stderr.log", "bundle"} <= set(times)
    status = json.loads(texts["runtime-status.json"])
    assert status["unavailable"] == ["route.published"]
    assert status["published"]["service.state"]["data"]["runtime"]["phase"] == "running"
    assert manifest["registry_pass"]["replacements"] > 0
    listed = {entry["name"]: entry["bytes"] for entry in manifest["files"]}
    for name, size in listed.items():
        assert size == len(texts[name].encode("utf-8")), name


def test_bundle_registry_hit_after_the_pass_fails_closed(tmp_path, monkeypatch):
    sources, view = bundle_fixture(tmp_path)
    svc, targets, _ = service(tmp_path, view=view, sources=sources)
    grant(svc, targets, "bundle-2", "bundle", "zip", 71)
    monkeypatch.setattr(SecretPass, "leaks", lambda self, text: True)
    with pytest.raises(JobFailure) as failure:
        svc.export_bundle({"save_ref": "bundle-2"}, CTX)
    assert code_of(failure) == "internal"
    assert targets.opened == [] and targets.released == [71]
    assert targets.handles[71].read_bytes() == b""


def test_redact_lines_never_passes_an_unfiltered_line():
    known = exports.KnownSecrets({"tok_ABCDEF123456": "token"})
    text = exports.redact_lines("ok line\r\nauth token=tok_ABCDEF123456\n" + "y" * (MAX_TEXT + 1), known)
    lines = text.split("\n")
    assert lines[0] == "ok line" and "tok_ABCDEF123456" not in text
    assert lines[2] == exports.REDACTED


PEM = ("-----BEGIN " "PRIVATE KEY-----\n"  # split: no whole PEM marker in source
       "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7\n"
       "q2Zk3mXlD9hY0sV4Jt8wPnR1oA6bC5eF7gH9iK0lM2nO4pQ6rS8\n"
       "-----END PRIVATE KEY-----")


def test_multi_line_registered_value_never_reaches_the_bundle(tmp_path):
    sources, view = bundle_fixture(tmp_path)
    sources.values[PEM] = "private_key"
    logs = sources.paths.extra_log_dir
    # CRLF on disk in one log, LF in the other.
    (logs / "shell.log").write_bytes(("before\r\n" + PEM.replace("\n", "\r\n") + "\r\nafter\r\n").encode())
    (logs / "sidecar-stderr.log").write_bytes(("key:\n" + PEM + "\n").encode())
    svc, targets, _ = service(tmp_path, view=view, sources=sources)
    grant(svc, targets, "pem", "bundle", "zip", 74)
    svc.export_bundle({"save_ref": "pem"}, CTX)
    texts = zip_texts(targets.handles[74].read_bytes())
    for name, text in texts.items():
        for body_line in PEM.splitlines()[1:-1]:
            assert body_line not in text, name
    assert "[redacted:private_key]" in texts["logs/shell.log"]
    assert "after" in texts["logs/shell.log"]


def test_bundle_records_log_model_retention(tmp_path):
    sources, _ = bundle_fixture(tmp_path)
    view = make_view(12, max_items=5)
    svc, targets, _ = service(tmp_path, view=view, sources=sources)
    grant(svc, targets, "ret", "bundle", "zip", 75)
    svc.export_bundle({"save_ref": "ret"}, CTX)
    status = json.loads(zip_texts(targets.handles[75].read_bytes())["runtime-status.json"])
    assert status["log_model"] == {"min_seq": 8, "latest_seq": 12, "retained": 5,
                                   "evicted_through_seq": 7, "view_clear_seq": 0,
                                   "snapshot_at_ms": NOW_MS}


def test_internal_failure_leaves_only_a_class_name_locally(tmp_path, monkeypatch, caplog):
    sources, view = bundle_fixture(tmp_path)
    svc, targets, _ = service(tmp_path, view=view, sources=sources)
    grant(svc, targets, "int", "bundle", "zip", 76)
    monkeypatch.setattr(SecretPass, "leaks", lambda self, text: True)
    # Own handler on the target logger: independent of propagation/level state
    # left behind by other tests in the same process.
    records = []
    handler = logging.Handler(logging.WARNING)
    handler.emit = records.append
    logger = logging.getLogger("tesseract.bridge.exports")
    monkeypatch.setattr(logger, "disabled", False)
    monkeypatch.setattr(logger, "level", logging.WARNING)
    logger.addHandler(handler)
    try:
        with pytest.raises(JobFailure) as failure:
            svc.export_bundle({"save_ref": "int"}, CTX)
    finally:
        logger.removeHandler(handler)
    assert code_of(failure) == "internal"
    messages = [record.getMessage() for record in records]
    assert messages == ["export build failed: code=internal cause=SecretLeak"]


def test_stale_drafts_are_swept_and_fresh_ones_kept(tmp_path):
    old, fresh = tmp_path / "tesseract-export-old", tmp_path / "tesseract-export-new"
    for directory in (old, fresh):
        directory.mkdir()
        (directory / "draft.zip").write_bytes(b"draft")
    (tmp_path / "tesseract-export-file").write_bytes(b"not a dir")
    (tmp_path / "unrelated").mkdir()
    stale = time.time() - exports.DRAFT_STALE_SECONDS - 5
    os.utime(old, (stale, stale))
    assert exports.sweep_stale_drafts(tmp_path) == 1
    assert not old.exists() and fresh.exists()
    assert (tmp_path / "tesseract-export-file").exists() and (tmp_path / "unrelated").exists()
    assert exports.sweep_stale_drafts(tmp_path / "missing") == 0


def test_bundle_draft_lives_only_under_the_draft_root_and_is_removed(tmp_path):
    sources, view = bundle_fixture(tmp_path)
    drafts = tmp_path / "drafts"
    drafts.mkdir()
    svc, targets, _ = service(tmp_path, view=view, sources=sources)
    svc.draft_root = drafts
    grant(svc, targets, "draft", "bundle", "zip", 77)
    svc.export_bundle({"save_ref": "draft"}, CTX)
    assert list(drafts.iterdir()) == []


class FakeState:
    def __init__(self, events):
        self.events = events

    def snapshot(self, topic):
        if topic not in self.events:
            raise ValueError("topic is not subscribed")
        return self.events[topic]


def test_session_export_sources_use_public_surfaces_and_report_gaps(tmp_path):
    data = tmp_path / "data"
    paths = SimpleNamespace(log_file=str(tmp_path / "monitor.log"), config_path=str(tmp_path / "c.yaml"),
                            local_config_path=str(tmp_path / "l.yaml"),
                            sources_path=str(tmp_path / "s.yaml"), data_dir=str(data))

    class Access:
        def known_secret_values(self):
            return {"tok_ABCDEF123456": "token"}

        def app_info(self):
            raise RuntimeError("runtime gone")

    session = SimpleNamespace(
        access=Access(), state=FakeState({"service.state": {"produced_at_ms": 5, "rev": 1, "data": {}}}),
        runtime=SimpleNamespace(app_paths=paths, alerts_config={"log_file": str(tmp_path / "alt.log")}))
    sources = SessionExportSources(session)
    assert sources.known_secret_values() == {"tok_ABCDEF123456": "token"}
    assert sources.app_facts() == {}
    facts = sources.published_facts()
    assert facts["service.state"]["rev"] == 1
    # Unsubscribed topics are unavailable until a latest-value getter exists.
    assert facts["route.published"] is None and set(facts) == set(exports.STATE_TOPICS)
    bundle = sources.bundle_paths()
    assert bundle.log_file == tmp_path / "alt.log"
    assert bundle.extra_log_dir == data / "logs"
    assert set(bundle.config_files) == {"config.yaml", "config.local.yaml", "sources.yaml"}
    session.runtime.alerts_config = None
    assert sources.bundle_paths().log_file == tmp_path / "monitor.log"


# ------------------------------------------- old bundle callers unchanged
def test_bundle_without_new_fields_keeps_the_old_manifest(tmp_path):
    result = write_diagnostic_bundle(tmp_path / "old.zip", DiagnosticBundleInputs(app={"version": "1"}))
    with zipfile.ZipFile(result.path) as archive:
        manifest = json.loads(archive.read("old/manifest.json"))
    assert "snapshot_times" not in manifest and "skipped_duplicate" not in manifest
    assert result.files == ("manifest.json",)


def test_attachment_names_are_plain_and_duplicates_are_listed(tmp_path):
    for bad in ("../x.log", "a/b.log", "", ".hidden"):
        with pytest.raises(ValueError):
            DiagnosticAttachment(bad, "text")
    monitor = tmp_path / "shell.log"
    monitor.write_bytes(b"from file\n")
    inputs = DiagnosticBundleInputs(log_file=monitor, extra_logs=(
        DiagnosticAttachment("shell.log", "duplicate"), DiagnosticAttachment("x.jsonl", '{"a":1}\n', jsonl=True)),
        snapshot_times={"x": 5, "bad": -1, "flag": True})
    result = write_diagnostic_bundle(tmp_path / "new.zip", inputs)
    with zipfile.ZipFile(result.path) as archive:
        manifest = json.loads(archive.read("new/manifest.json"))
        assert archive.read("new/logs/shell.log").decode() == "from file\n"
    assert manifest["skipped_duplicate"] == ["logs/shell.log"]
    assert manifest["snapshot_times"] == {"x": 5}
    assert "logs/x.jsonl" in result.files


# ------------------------------------------------- executor integration
class Session:
    def __init__(self, executor):
        self.executor = executor
        self.hooks = []

    def register_owner_revoke_hook(self, hook):
        self.hooks.append(hook)


# T-P2-31a: JobExecutor offers add_terminal_listener itself (listeners run
# outside the executor lock); the former test stand-in is gone.
ListeningExecutor = JobExecutor


@pytest.fixture
def wired(tmp_path):
    executor = ListeningExecutor(JobEventRecorder(), epoch=EPOCH, require_acceptance=True)
    session = Session(executor)
    targets = DirTargets(tmp_path)
    sources, view = bundle_fixture(tmp_path)
    gate = {"event": None, "model": None}
    inner = LogViewModel(lambda: view)

    class GatedModel:
        def export_snapshot(self):
            if gate["event"] is not None:
                gate["entered"].set()
                gate["event"].wait(5)
            if gate["model"] is not None:
                return gate["model"].export_snapshot()
            return inner.export_snapshot()

    drafts = tmp_path / "drafts"
    drafts.mkdir()
    stale = drafts / "tesseract-export-crashed"
    stale.mkdir()
    old = time.time() - exports.DRAFT_STALE_SECONDS - 5
    os.utime(stale, (old, old))
    svc = exports.register(session, targets=targets, identity=ExecutorIdentity(executor),
                           sources=sources, log_model=GatedModel(), draft_root=drafts)
    yield SimpleNamespace(executor=executor, session=session, targets=targets, svc=svc,
                          gate=gate, view=view, drafts=drafts, stale=stale)
    svc.close()
    executor.shutdown(time.monotonic() + 2)


def submit_grant(w, ref, purpose, fmt, handle_id, owner=OWNER):
    w.targets.add(handle_id, f"{ref}.{fmt}")
    receipt = w.executor.submit("shell.saveGrant", dict(
        ref=ref, purpose=purpose, format=fmt, handle_id=handle_id,
        expires_at_ms=time.time_ns() // 1_000_000 + 60_000), req_id=f"g-{handle_id}", owner=owner)
    response = receipt.wait(5)
    return response


def run_job(w, name, args, *, owner=OWNER, send_ack=True):
    ack = w.executor.submit(name, args, req_id="r-" + name, owner=owner)
    assert ack.accepted, ack
    if send_ack:
        assert w.executor.acceptance_sent(ack.op, epoch=EPOCH)
    return ack


def test_registered_commands_run_through_the_executor(wired):
    response = submit_grant(wired, "exec-bundle", "bundle", "zip", 80)
    assert response.ok and response.data == {"registered": True}
    assert wired.session.hooks == [wired.svc.revoke_owner]
    ack = run_job(wired, "logs.exportBundle", {"save_ref": "exec-bundle"})
    job = wired.executor.wait(ack.op, 20)
    assert job["state"] == "done", job
    assert job["result"]["ref"] == "exec-bundle" and "manifest.json" in job["result"]["files"]
    assert job["result"]["size"] == wired.targets.handles[80].stat().st_size
    assert submit_grant(wired, "exec-filt", "filtered", "csv", 81).ok
    ack = run_job(wired, "logs.exportFiltered", {"save_ref": "exec-filt", "format": "csv", "seqs": [1, 2, 50]})
    job = wired.executor.wait(ack.op, 10)
    assert job["state"] == "done" and job["result"] == {"ref": "exec-filt", "count": 2, "missing_seqs": [50]}


def test_executor_rejects_mismatch_and_reports_structured_failures(wired):
    assert submit_grant(wired, "exec-json", "filtered", "json", 82).ok
    ack = run_job(wired, "logs.exportFiltered", {"save_ref": "exec-json", "format": "text", "filter": {}})
    job = wired.executor.wait(ack.op, 10)
    assert job["state"] == "failed" and job["error"]["code"] == "not_allowed"
    wired.targets.fail_after = 0
    ack = run_job(wired, "logs.exportFiltered", {"save_ref": "exec-json", "format": "json", "filter": {}})
    job = wired.executor.wait(ack.op, 10)
    assert job["state"] == "failed" and job["error"] == {
        "code": "io", "message_key": "errors.io", "params": {}}
    assert not wired.targets.handles[82].exists()
    # The same owner cannot register a ref twice, even with a new handle.
    duplicate = submit_grant(wired, "exec-dup", "filtered", "json", 83)
    assert duplicate.ok
    again = wired.executor.submit("shell.saveGrant", dict(
        ref="exec-dup", purpose="filtered", format="json", handle_id=84,
        expires_at_ms=time.time_ns() // 1_000_000 + 60_000), req_id="g-dup", owner=OWNER).wait(5)
    assert not again.ok and again.error["code"] == "conflict"
    # Another owner cannot claim a handle that a live grant already holds,
    # and that handle is not closed by the rejected attempt.
    foreign = wired.executor.submit("shell.saveGrant", dict(
        ref="exec-foreign", purpose="filtered", format="json", handle_id=83,
        expires_at_ms=time.time_ns() // 1_000_000 + 60_000), req_id="g-foreign", owner="page-2").wait(5)
    assert not foreign.ok and foreign.error["code"] == "conflict"
    assert 83 not in wired.targets.released


def test_queued_cancel_frees_the_grant_and_the_ref_is_spent(wired):
    assert submit_grant(wired, "exec-cancel", "filtered", "json", 85).ok
    ack = run_job(wired, "logs.exportFiltered", {"save_ref": "exec-cancel", "format": "json", "filter": {}},
                  send_ack=False)
    cancel = wired.executor.cancel(ack.op, owner=OWNER)
    assert cancel.accepted
    job = wired.executor.wait(ack.op, 5)
    assert job["state"] == "cancelled" and wired.targets.opened == []
    deadline = time.monotonic() + 5
    while 85 not in wired.targets.released and time.monotonic() < deadline:
        time.sleep(0.01)
    assert wired.targets.released == [85] and len(wired.svc.grants) == 0
    assert wired.targets.handles[85].read_bytes() == b""
    # Submitting again with the same ref cannot write anything.
    ack = run_job(wired, "logs.exportFiltered", {"save_ref": "exec-cancel", "format": "json", "filter": {}})
    job = wired.executor.wait(ack.op, 5)
    assert job["state"] == "failed" and job["error"]["code"] == "not_allowed"
    assert wired.targets.opened == []


def test_terminal_listener_adapter_is_a_no_op_without_the_executor_api(tmp_path):
    seen = []
    assert ExecutorJobOutcomes(lambda: object()).subscribe(seen.append) is False

    class Exec:
        def add_terminal_listener(self, listener):
            listener({"op": "job-1", "name": "logs.exportBundle", "args": {"save_ref": "r"},
                      "epoch": EPOCH, "owner": OWNER, "state": "cancelled"})

    assert ExecutorJobOutcomes(lambda: Exec()).subscribe(seen.append) is True
    assert seen == [TerminalJob("logs.exportBundle", {"save_ref": "r"}, EPOCH, OWNER, "cancelled")]
    # Only a cancelled export of the same epoch/owner frees a grant.
    svc, targets, _ = service(tmp_path)
    grant(svc, targets, "kept", "bundle", "zip", 95)
    for event in (TerminalJob("logs.exportBundle", {"save_ref": "kept"}, EPOCH, OWNER, "failed"),
                  TerminalJob("logs.exportBundle", {"save_ref": "kept"}, EPOCH, "page-2", "cancelled"),
                  TerminalJob("nodes.delete", {"save_ref": "kept"}, EPOCH, OWNER, "cancelled")):
        svc.on_job_terminal(event)
    assert len(svc.grants) == 1 and targets.released == []
    svc.on_job_terminal(TerminalJob("logs.exportBundle", {"save_ref": "kept"}, EPOCH, OWNER, "cancelled"))
    assert len(svc.grants) == 0 and targets.released == [95]


def test_started_export_is_not_cancellable_and_finishes_the_write(wired):
    assert submit_grant(wired, "exec-run", "filtered", "text", 86).ok
    wired.gate.update(event=threading.Event(), entered=threading.Event())
    ack = run_job(wired, "logs.exportFiltered", {"save_ref": "exec-run", "format": "text", "filter": {}})
    assert wired.gate["entered"].wait(5)
    refused = wired.executor.cancel(ack.op, owner=OWNER)
    assert not refused.accepted and refused.skipped_reason == "not_cancellable"
    wired.executor.revoke_owner(OWNER)  # A started disk transaction still finishes.
    wired.gate["event"].set()
    job = wired.executor.wait(ack.op, 10)
    assert job["state"] == "done" and job["result"]["count"] == 2
    assert "ordinary routing line" in wired.targets.handles[86].read_text("utf-8")


def test_snapshot_required_reaches_the_job_terminal(wired, monkeypatch):
    rows = tuple(make_view(4100).snapshot())
    holed = (rows[0], rows[-1])  # 4098 holes: more than one wire array holds.

    class HoledModel:
        def export_snapshot(self):
            return LogSnapshot(holed, 1, 4100, None, NOW_MS)

    wired.gate["model"] = HoledModel()
    assert submit_grant(wired, "exec-snap", "filtered", "json", 87).ok
    ack = run_job(wired, "logs.exportFiltered", {"save_ref": "exec-snap", "format": "json", "filter": {}})
    job = wired.executor.wait(ack.op, 10)
    assert job["state"] == "failed" and job["error"]["code"] == "snapshot_required"
    assert 87 in wired.targets.released and 87 not in wired.targets.opened


def test_missing_seqs_at_the_array_cap_pass_the_protocol_adapter(wired):
    for index in range(4100):
        wired.view.append(log_row(f"filler {index}"))
    wired.view.max_items = 50
    wired.view.append(log_row("evict"))  # Retains only the newest 50.
    wanted = list(range(1, 4001))  # All evicted: exactly 4000 missing.
    assert submit_grant(wired, "exec-cap", "filtered", "json", 88).ok
    ack = run_job(wired, "logs.exportFiltered", {"save_ref": "exec-cap", "format": "json", "seqs": wanted})
    job = wired.executor.wait(ack.op, 10)
    assert job["state"] == "done", job
    assert job["result"] == {"ref": "exec-cap", "count": 0, "missing_seqs": wanted}
    # A longer selection never reaches the handler: the wire array cap applies.
    over = wired.executor.submit("logs.exportFiltered", {"save_ref": "exec-cap", "format": "json",
                                                         "seqs": list(range(1, 4002))},
                                 req_id="r-over", owner=OWNER)
    assert not over.accepted
    # Long sessions: the filter scope still succeeds with an evicted range.
    assert submit_grant(wired, "exec-long", "filtered", "json", 89).ok
    ack = run_job(wired, "logs.exportFiltered", {"save_ref": "exec-long", "format": "json", "filter": {}})
    job = wired.executor.wait(ack.op, 10)
    assert job["state"] == "done" and job["result"]["missing_seqs"] == []
    document = json.loads(wired.targets.handles[89].read_bytes())
    assert document["evicted_before_export"]["to_seq"] == wired.view.min_seq - 1


def test_register_sweeps_stale_drafts_on_a_normal_worker(wired):
    deadline = time.monotonic() + 5
    while wired.stale.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not wired.stale.exists() and wired.drafts.exists()
