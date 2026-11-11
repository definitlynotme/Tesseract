"""Bounded observation of the real pipeline; never supplies a PASS or decision."""
from __future__ import annotations

import functools
import json
import os
import threading
import time
from pathlib import Path

from atomic_file import replace_with_retry
from scripts.tier3_audit_liveness import auto_admission_completion, stage_gaps


class AuditActivityRecorder:
    FIELDS = (
        'candidate_fresh_backlog', 'candidate_reserved_handoff_count',
        'candidate_tcp_handoff_lease_count', 'tcp_pending_depth',
        'validation_ingress_depth', 'fast_ready', 'fast_active',
        'deep_ready', 'deep_parked', 'deep_active', 'commit_pending',
    )

    def __init__(self, directory, *, clock=time.monotonic, limit=50000):
        self.directory = Path(directory)
        self.clock, self.origin, self.limit = clock, clock(), limit
        self.lock = threading.Lock()
        self.events, self.work, self.errors = [], [], []
        self.starts, self.completions = set(), set()
        self.stop_at = None

    def _error(self, value):
        if value not in self.errors:
            self.errors.append(value)

    def _event(self, key, kind, seen):
        with self.lock:
            if key in seen:
                return
            if len(self.events) >= self.limit:
                self._error('activity_event_limit')
                return
            seen.add(key)
            self.events.append((self.clock() - self.origin, kind))

    def record_starts(self, owner, tickets):
        for ticket in tickets:
            key = (id(owner), ticket.scheduler_generation, ticket.ticket_id)
            self._event(key, 'tcp_start', self.starts)

    def record_completion(self, owner, result):
        if auto_admission_completion(result):
            self._event((id(owner), result.lease_id, result.sequence),
                        'validation_completion', self.completions)

    def record_work(self, work, target):
        if work is None:
            return
        with self.lock:
            if len(self.work) >= self.limit:
                self._error('activity_work_limit')
                return
            item = {name: getattr(work, name) for name in self.FIELDS}
            item.update(at=self.clock() - self.origin, healthy=work.healthy_count,
                        target=target, guardrail=work.hard_resource_guardrail)
            self.work.append(item)

    def record_stop(self):
        with self.lock:
            if self.stop_at is None:
                self.stop_at = self.clock() - self.origin

    def install_scheduler(self, module, patch):
        owner = module.Tier3CandidateScheduler
        original = owner.commit_handoffs

        @functools.wraps(original)
        def commit(scheduler, *args, **kwargs):
            result = original(scheduler, *args, **kwargs)
            self.record_starts(scheduler, result)
            return result
        patch(owner, 'commit_handoffs', commit)

    def install_controller(self, module, patch):
        owner = module.Tier3PIController
        original = owner.expansion_envelope

        @functools.wraps(original)
        def envelope(controller, *args, **kwargs):
            result = original(controller, *args, **kwargs)
            self.record_work(kwargs.get('work_snapshot'), kwargs.get('target_alive'))
            return result
        patch(owner, 'expansion_envelope', envelope)

    def install_supervisor(self, module, patch):
        owner = module.Tier3ValidatorSupervisor
        original = owner.__init__

        @functools.wraps(original)
        def initialize(supervisor, *args, **kwargs):
            sink = kwargs['attempt_sink']

            def record(result):
                self.record_completion(supervisor, result)
                return sink(result)

            kwargs['attempt_sink'] = record
            original(supervisor, *args, **kwargs)
        patch(owner, '__init__', initialize)

    def install_manager(self, module, patch):
        owner = module.ProxyTierManager
        original = owner.request_stop

        @functools.wraps(original)
        def stop(manager, *args, **kwargs):
            self.record_stop()
            return original(manager, *args, **kwargs)
        patch(owner, 'request_stop', stop)

    def snapshot(self):
        with self.lock:
            payload = dict(
                liveness_schema=2, tcp_start_count=len(self.starts),
                completion_count=len(self.completions), work_snapshot_count=len(self.work),
                stop_boundary_observed=self.stop_at is not None, stop_at_seconds=self.stop_at,
                events=list(self.events), work=[dict(item) for item in self.work],
                recording_errors=list(self.errors),
                scope='before_target_internal_activity_not_admission_yield',
            )
        if payload['stop_at_seconds'] is not None and payload['work']:
            payload['stages'] = stage_gaps(payload['work'], payload['events'], payload['stop_at_seconds'])
        return payload

    def save(self):
        payload = self.snapshot()
        # Helpers install the same hook but must not overwrite the main
        # application's activity evidence with an empty local snapshot.
        if not payload['work']:
            return
        destination = self.directory / 'activity-control.json'
        pending = destination.with_suffix(f'.{os.getpid()}.pending')
        pending.write_text(json.dumps(payload, sort_keys=True), encoding='utf-8')
        replace_with_retry(pending, destination)
