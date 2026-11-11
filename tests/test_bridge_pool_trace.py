"""Bastion trace (CLAUDE_CHANGES §8r, diagnostic, temporary).

The sampler logs to bridge.log what it did with bastion.pool each time that changes (state, edge,
unchanged, withheld and why), the publisher logs every bastion.pool frame it handed to the
transport, and the getter says why a withheld sample was withheld. Numbers and fixed codes only;
change-only and capped. Isolated doubles: no app, no Core, no network.
"""
from __future__ import annotations

import logging

import pytest

import bridge_publish
from bridge_publish import Publisher
from bridge_runtime_access import RuntimeAccess
from bridge_state import PublicationState
from tests.test_bridge_h2 import EPOCH, Wire, ticks
from tests.test_bridge_p2_04 import runtime  # noqa: F401  isolated __new__ fixture, never starts an app
from tests.test_bridge_p2_08 import dto, observe, projection


class Lines(logging.Handler):
    def __init__(self):
        super().__init__(logging.INFO)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


@pytest.fixture
def trace():
    """The trace logger at INFO with a list handler of its own (the bridge's parent logger may
    have propagate=False once bridge_main.init_logging ran in this process)."""
    handler = Lines()
    loggers = [logging.getLogger("tesseract.bridge.trace")]
    saved = [(log, log.level) for log in loggers]
    for log in loggers:
        log.setLevel(logging.INFO)
        log.addHandler(handler)
    yield handler.lines
    for log, level in saved:
        log.setLevel(level)
        log.removeHandler(handler)


def samples(lines):
    return [line for line in lines if line.startswith("bastion.pool sample")]


def field(line, name):
    return dict(part.split("=", 1) for part in line.split()[2:])[name]


def test_sampler_traces_each_change_of_what_it_did_with_the_pool(trace):
    access, state, topics, _ = projection()
    alive = access.records["bastion.pool"]["runtime_alive"]
    topics.sample(observe)
    topics.sample(observe)
    topics.sample(observe)
    access.records["bastion.pool"]["runtime_alive"] = alive + 1
    topics.sample(observe)
    got = [(field(line, "outcome"), field(line, "alive")) for line in samples(trace)]
    # the first sample is a state, then unchanged (once, not every tick), then the new count
    assert got == [("state", str(alive)), ("unchanged", str(alive)), ("edge", str(alive + 1))]
    assert all(field(line, "reason") == "-" for line in samples(trace))


def test_sampler_traces_why_a_sample_was_withheld(trace):
    access, state, topics, _ = projection()
    topics.sample(observe)
    plain = access.bastion_pool
    # the getter's own verdict (desktop_runtime.bridge_bastion_pool: stale + stale_reason)
    access.bastion_pool = lambda: {**plain(), "stale": True, "stale_reason": "manager_config"}
    topics.sample(observe)
    topics.sample(observe)
    # the lifecycle moved under the sample (bridge_tiers.sample: freshness stale)
    access.bastion_pool = plain
    access.lifecycle["freshness"]["stale"] = True
    topics.sample(observe)
    access.lifecycle["freshness"]["stale"] = False
    topics.sample(observe)
    got = [(field(line, "outcome"), field(line, "reason")) for line in samples(trace)]
    assert got == [("state", "-"), ("withheld", "manager_config"), ("withheld", "lifecycle_stale"), ("state", "-")]
    # a withheld sample is a gap, its recovery a full state: what the shell and the page expect
    assert topics.status["bastion.pool"] == "current"


def test_sampler_trace_is_capped(trace):
    access, state, topics, _ = projection()
    topics.POOL_TRACE_MAX = 3
    for n in range(10):
        access.records["bastion.pool"]["runtime_alive"] = n
        topics.sample(observe)
    assert len(samples(trace)) == 3


def test_publisher_traces_each_pool_frame_it_sent(trace):
    state, wire = PublicationState(EPOCH), Wire()
    publisher = Publisher(state, wire)
    publisher.subscription_frames({"t": "subscribe", "epoch": EPOCH, "produced_at_ms": 1,
                                   "sent_at_ms": 1, "id": 1, "topics": ["bastion.pool"]})
    data = dto("bastion.pool")
    state.record_state("bastion.pool", data)
    ticks(publisher, 4)
    state.record_edge("bastion.pool", {**data, "runtime_alive": data["runtime_alive"] + 1})
    ticks(publisher, 4)
    sent = [line for line in trace if line.startswith("bastion.pool tx")]
    assert [(field(line, "kind"), field(line, "alive")) for line in sent][-2:] == [
        ("snapshot", str(data["runtime_alive"])), ("edge", str(data["runtime_alive"] + 1))]
    # the logged wire rev is the one the shell saw (transport.rs: per topic increasing)
    assert [field(line, "rev") for line in sent][-2:] == [str(wire.revs["bastion.pool"] - 1), str(wire.revs["bastion.pool"])]


def test_publisher_trace_is_capped(trace, monkeypatch):
    monkeypatch.setattr(bridge_publish, "POOL_TRACE_MAX", 2)
    state, wire = PublicationState(EPOCH), Wire()
    publisher = Publisher(state, wire)
    publisher.subscription_frames({"t": "subscribe", "epoch": EPOCH, "produced_at_ms": 1,
                                   "sent_at_ms": 1, "id": 1, "topics": ["bastion.pool"]})
    data = dto("bastion.pool")
    state.record_state("bastion.pool", data)
    ticks(publisher, 4)
    for n in range(5):
        state.record_edge("bastion.pool", {**data, "runtime_alive": n})
        ticks(publisher, 2)
    assert len([line for line in trace if line.startswith("bastion.pool tx")]) == 2


def test_getter_says_why_the_pool_is_withheld(runtime):  # noqa: F811
    assert RuntimeAccess(runtime).bastion_pool()["stale_reason"] is None
    owner = runtime._bridge_read_owner()
    moved = (owner[0], owner[1], owner[2], {"tiers": {}}, owner[4], owner[5])
    runtime._bridge_read_owner = lambda: moved
    result = RuntimeAccess(runtime).bastion_pool()
    assert result["stale"] and result["stale_reason"] == "config"
    del runtime._bridge_read_owner
    runtime.manager = None
    result = RuntimeAccess(runtime).bastion_pool()
    assert result["stale"] and result["stale_reason"] == "no_manager"
