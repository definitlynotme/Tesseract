"""P2-06 owned substitutes only: no runtime start, network or real config."""

from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

import bridge_protocol as protocol
from bridge_nodes import NodeProjection, StaleNodeSample, recheck_progress
from bridge_runtime_access import RuntimeAccess, immutable_record, opaque_id
from tests.test_bridge_p2_04 import runtime
from tests.test_runtime_lifecycle_status import minimal_runtime


def facts(*, generation=7, checked=None, origin=None, enabled=True, running=True,
          core_generation=8, refreshing=False):
    source = opaque_id("tier2", "https://example.invalid/private-sub")
    node = {"id": opaque_id(generation, "tier2", ("vless", "example.invalid", 443)),
            "name": "原文 🚀 mixed 日本語", "tier": "sentinel", "protocol": "vless",
            "status": "alive", "latency_ms": 31.75, "pool_kind": "user",
            "source_id": source, "source_ids": (source,), "checked_at_ms": checked}
    if origin:
        node["observation_origin"] = origin
    sub = {"id": opaque_id("tier2", 0, "https://example.invalid/private-sub"),
           "name": "订阅 🛰️", "tier": "sentinel", "enabled": enabled,
           "source_id": source, "checked_at_ms": checked,
           "current_generation_evidence": checked is not None, "membership_version": 1,
           "refresh_state": {"refreshing": refreshing, "job_generation": 0},
           "check_receipt": {"runtime_generation": generation,
               "core_generation": core_generation, "core_pid": 42,
               "membership_version": 1, "member_ids": [node["id"]],
               "refresh_job_generation": 0, "checked_at_ms": checked}}
    lifecycle = {"intent": {"runtime_generation": generation, "lifecycle_revision": 1,
                            "stop_requested": False},
                 "core": {"running": running, "core_generation": core_generation, "pid": 42},
                 "freshness": {"stale": False}}
    return ({"generation": generation, "stale": False, "items": [node]},
            {"generation": generation, "stale": False, "items": [sub]}, lifecycle)


def first(result):
    return result["nodes"]["items"][0], result["subscriptions"]["items"][0]


def assert_schema(result):
    assert protocol.validate_topic_payload("nodes.list", result["nodes"]) == []
    assert protocol.validate_topic_payload("subscriptions.list", result["subscriptions"]) == []
    for topic in ("nodes.list", "subscriptions.list"):
        frame = {"t": "evt", "proto": 1, "epoch": "test-epoch", "kind": "snapshot",
                 "topic": topic, "key": "sentinel", "rev": 1,
                 "produced_at_ms": 1, "sent_at_ms": 1,
                 "data": result["nodes" if topic == "nodes.list" else "subscriptions"]}
        assert protocol.validate_frame(frame) == []


def test_refresh_visibility_and_recheck_completion():
    projector = NodeProjection()
    result = projector.project(*facts())
    node, sub = first(result)
    assert (node["status"], node["latency_ms"], sub["visibility"], sub["healthy"]) == (
        "pending", None, "pending", 0)
    assert result["recheck_progress"] == recheck_progress(0, 1)
    result = projector.project(*facts(checked=1000))
    node, sub = first(result)
    assert (node["status"], node["latency_ms"], sub["visibility"], sub["healthy"]) == (
        "healthy", 31, "current", 1)
    assert result["recheck_progress"] == recheck_progress(1, 1)
    assert_schema(result)
    result = projector.project(*facts(checked=1000, refreshing=True))
    assert first(result)[1]["visibility"] == "previous"
    assert first(result)[1]["refreshing"] is True
    assert first(projector.project(*facts(checked=2000)))[1]["visibility"] == "current"


def test_existing_current_generation_receipt_is_valid_on_first_sample():
    result = NodeProjection().project(*facts(checked=1000))
    assert first(result)[1]["visibility"] == "current"


def test_generation_resets_same_gate_and_previous_health():
    projector = NodeProjection()
    gate = projector.gate
    old = projector.project(*facts(checked=1000))
    result = projector.project(*facts(generation=8))
    node, sub = first(result)
    assert projector.gate is gate
    assert node["id"] != first(old)[0]["id"]
    assert sub["id"] == first(old)[1]["id"]
    assert (node["status"], sub["visibility"], sub["healthy"]) == ("pending", "pending", 0)
    assert first(projector.project(*facts(generation=8, checked=2000)))[1]["visibility"] == "current"
    with pytest.raises(StaleNodeSample):
        projector.project(*facts(generation=7, checked=3000))
    assert projector.gate.core_stamp[0] == 8


def test_core_restart_requires_new_receipt_not_cached_current_flag():
    projector = NodeProjection()
    projector.project(*facts(checked=1000))
    for _ in range(2):
        result = projector.project(*facts(checked=1000, core_generation=9))
        assert first(result)[0]["status"] == "pending"
        assert first(result)[1]["visibility"] == "previous"
    assert first(projector.project(*facts(checked=2000, core_generation=9)))[1]["visibility"] == "current"
    with pytest.raises(StaleNodeSample):
        projector.project(*facts(checked=3000, core_generation=8))


def test_close_blocks_late_same_core_then_new_core_baseline():
    projector = NodeProjection()
    projector.project(*facts(checked=1000))
    projector.close()
    assert first(projector.project(*facts(checked=2000)))[0]["status"] == "pending"
    assert first(projector.project(*facts(checked=2000, core_generation=9)))[0]["status"] == "pending"
    assert first(projector.project(*facts(checked=3000, core_generation=9)))[0]["status"] == "healthy"


@pytest.mark.parametrize("origin", ["persisted_cache", "offline_refresh"])
def test_offline_cache_never_unlocks_current_generation(origin):
    projector = NodeProjection()
    result = projector.project(*facts(checked=1000, origin=origin))
    assert first(result)[0]["status"] == "pending"
    assert first(result)[1]["healthy"] == 0
    assert first(result)[1]["visibility"] == "pending"
    result = projector.project(*facts())
    assert first(result)[1]["visibility"] == "pending"


def test_cache_timestamp_without_source_receipt_is_not_live_evidence():
    nodes, subscriptions, lifecycle = facts(checked=1000)
    subscriptions["items"][0]["current_generation_evidence"] = False
    result = NodeProjection().project(nodes, subscriptions, lifecycle)
    assert first(result)[0]["status"] == "pending"
    assert first(result)[1]["healthy"] == 0


def test_disabled_subscription_and_reenable_requires_current_receipt():
    projector = NodeProjection()
    projector.project(*facts(checked=1000))
    result = projector.project(*facts(checked=1000, enabled=False))
    node, sub = first(result)
    assert (node["status"], node["enabled"], sub["visibility"], sub["healthy"]) == (
        "disabled", False, "unknown", 0)
    assert result["recheck_progress"]["total"] == 0
    assert first(projector.project(*facts(enabled=True)))[0]["status"] == "pending"


def test_stopped_health_is_previous_and_not_current():
    projector = NodeProjection()
    projector.project(*facts(checked=1000))
    result = projector.project(*facts(checked=1000, running=False))
    assert (first(result)[0]["status"], first(result)[1]["visibility"]) == ("pending", "previous")
    assert first(result)[1]["healthy"] == 1
    result = NodeProjection().project(*facts(checked=1000, running=False))
    assert first(result)[1]["healthy"] == 0


def test_automatic_pool_continuity_and_name_preservation():
    projector = NodeProjection()
    nodes, subs, life = facts()
    raw = nodes["items"][0]
    raw.update(tier="bastion", pool_kind="automatic", name="🚀 mixed 原文 日本語 " + "很长Name" * 2000)
    raw["id"] = opaque_id(7, "tier3", ("vless", "host", 443))
    before = projector.project(nodes, subs, life)
    raw["status"] = "slow"
    after = projector.project(nodes, subs, life)
    dto = after["nodes"]["items"][0]
    assert dto["id"] == before["nodes"]["items"][0]["id"] == raw["id"]
    assert dto["name"] == raw["name"]
    assert dto["kind"] == "automatic"
    assert dto["status"] == "healthy"
    assert protocol.validate_topic_payload("nodes.list", after["nodes"]) == []


def test_whitelist_and_secrets_in_names_and_other_fields():
    nodes, subs, life = facts(checked=1000)
    uuid = "12345678-1234-1234-1234-123456789abc"
    password = "PrivatePasswordOnly"
    uri = "vless://" + uuid + "@example.invalid:443"
    raw = nodes["items"][0]
    raw.update(password=password, uuid=uuid, uri=uri,
               name=f"name {password} {uri} {uuid}", status="suspicious",
               suspicious_reason=f"TLS certificate {password} {uri}",
               connection={"password": password, "uri": uri}, arbitrary="secret junk")
    subs["items"][0]["name"] = "name https://example.invalid/private-sub"
    result = NodeProjection().project(nodes, subs, life)
    dto = first(result)[0]
    # T-P4-C1 #3 / T-P4-C2 #3-#4: optional v1 additions (PROTOCOL §17).
    assert set(dto) == {"id", "name", "tier", "kind", "enabled", "status", "reason_code", "protocol", "latency_ms",
                        "subscription_id", "slow", "last_checked_at_ms"}
    assert dto["reason_code"] == "tls_failed"
    wire = json.dumps(result, ensure_ascii=False)
    assert all(secret not in wire for secret in (password, uuid, uri, "https://example.invalid/private-sub", "secret junk"))
    assert_schema(result)


@pytest.mark.parametrize("raw_status,expected", [("alive", "healthy"), ("slow", "healthy"),
    ("dead", "failed"), ("suspicious", "failed"), ("checking", "unknown"),
    ("pending", "pending"), ("disabled", "disabled")])
def test_status_codes(raw_status, expected):
    nodes, subs, life = facts()
    raw = nodes["items"][0]
    raw.update(status=raw_status, source_id=None, source_ids=())
    result = NodeProjection().project(nodes, subs, life)
    assert first(result)[0]["status"] == expected
    assert_schema(result)


@pytest.mark.parametrize("latency,expected", [(None, None), (-1, None), (float("nan"), None),
    (float("inf"), None), (True, None), ("31ms", None), (0, 0), (31.75, 31)])
def test_latency_is_numeric_uint(latency, expected):
    nodes, subs, life = facts(checked=1000)
    nodes["items"][0]["latency_ms"] = latency
    result = NodeProjection().project(nodes, subs, life)
    assert first(result)[0]["latency_ms"] == expected
    assert_schema(result)


def test_shared_node_does_not_unlock_unchecked_source():
    nodes, subs, life = facts(checked=1000)
    second = deepcopy(subs["items"][0])
    second.update(id=opaque_id("second"), source_id="second-source", current_generation_evidence=False)
    subs["items"].append(second)
    nodes["items"][0]["source_ids"] += ("second-source",)
    result = NodeProjection().project(nodes, subs, life)
    assert result["subscriptions"]["items"][0]["visibility"] == "current"
    assert result["subscriptions"]["items"][1]["visibility"] == "pending"
    assert result["subscriptions"]["items"][1]["healthy"] == 0
    assert first(result)[0]["status"] == "healthy"


def test_empty_checked_source_is_current_with_zero_nodes():
    nodes, subs, life = facts(checked=1000)
    nodes["items"] = []
    subs["items"][0]["check_receipt"]["member_ids"] = []
    result = NodeProjection().project(nodes, subs, life)
    sub = result["subscriptions"]["items"][0]
    assert (sub["visibility"], sub["total"], sub["healthy"]) == ("current", 0, 0)


def test_p204_real_getters_require_refresh_owner_and_bound_receipts(runtime):
    access = RuntimeAccess(runtime)
    life = facts()[2]
    nodes, subs = access.node_records(), access.subscription_records()
    projector = NodeProjection(access)
    assert_unavailable_nodes(projector.project(nodes, subs, life))
    assert not projector.gate.initialized
    def owner(refreshing, job):
        return immutable_record({"generation": 7, "stale": False, "items": [
            {"id": sub["id"], "refreshing": refreshing, "job_generation": job}
            for sub in subs["items"]]})
    before = projector.project(nodes, subs, life, refresh_records=owner(False, 0))
    assert before["subscriptions"]["items"][0]["name"] == before["subscriptions"]["items"][0]["id"]
    assert protocol.validate_topic_payload("nodes.list", before["nodes"]) == []
    assert protocol.validate_topic_payload("subscriptions.list", before["subscriptions"]) == []
    runtime.manager.evidence["exit"]["https://example.invalid/private-sub"] = {
        "runtime_generation": 7, "checked_at": "2026-10-01T10:05:00+00:00"}
    during = projector.project(nodes, subs, life, refresh_records=owner(True, 1))
    assert during["subscriptions"]["items"][0]["refreshing"] is True
    after = projector.project(access.node_records(), access.subscription_records(), life,
                              refresh_records=owner(False, 1))
    assert after["subscriptions"]["items"][0]["visibility"] == "pending"
    assert after["subscriptions"]["items"][0]["refreshing"] is False
    assert after["nodes"]["items"][0]["kind"] == "subscription"
    assert after["nodes"]["items"][0]["status"] == "pending"
    assert protocol.validate_topic_payload("nodes.list", after["nodes"]) == []
    assert protocol.validate_topic_payload("subscriptions.list", after["subscriptions"]) == []


def test_changed_membership_drops_previous_health_instead_of_mixing_counts():
    projector = NodeProjection()
    projector.project(*facts(checked=1000))
    nodes, subs, life = facts(refreshing=True)
    nodes["items"] = []
    result = projector.project(nodes, subs, life)
    sub = result["subscriptions"]["items"][0]
    assert (sub["visibility"], sub["total"], sub["healthy"]) == ("pending", 0, 0)


def test_short_secret_in_hex_id_does_not_change_identity():
    nodes, subs, life = facts(checked=1000)
    raw = nodes["items"][0]
    raw["password"] = raw["id"][:3]
    assert first(NodeProjection().project(nodes, subs, life))[0]["id"] == raw["id"]


def test_sensitive_record_id_is_rejected_before_gate_update():
    nodes, subs, life = facts(checked=1000)
    nodes["items"][0]["id"] = "https://example.invalid/private"
    projector = NodeProjection()
    with pytest.raises(ValueError, match="invalid opaque record id"):
        projector.project(nodes, subs, life)
    assert not projector.gate.initialized


def test_owned_immutable_inputs_and_detached_results():
    original = facts(checked=1000)
    frozen = tuple(immutable_record(item) for item in original)
    projector = NodeProjection()
    result = projector.project(*frozen)
    first(result)[0]["name"] = "changed by caller"
    first(result)[1]["healthy"] = 900
    again = projector.project(*frozen)
    assert first(again)[0]["name"] == original[0]["items"][0]["name"]
    assert first(again)[1]["healthy"] == 1
    assert frozen[0]["items"][0]["name"] == original[0]["items"][0]["name"]


@pytest.mark.parametrize("domain", [0, 1, 2])
def test_stale_samples_do_not_change_gate(domain):
    inputs = list(facts(checked=1000))
    if domain == 2:
        inputs[2]["freshness"]["stale"] = True
    else:
        inputs[domain]["stale"] = True
    projector = NodeProjection()
    with pytest.raises(StaleNodeSample):
        projector.project(*inputs)
    assert not projector.gate.initialized


def test_sample_brackets_node_read_and_rejects_refresh_race():
    nodes, subs, life = facts(checked=1000)
    calls = []
    def read(name, value):
        calls.append(name)
        return immutable_record(value)
    access = SimpleNamespace(lifecycle_status=lambda: read("life", life),
        subscription_records=lambda: read("subs", subs), node_records=lambda: read("nodes", nodes))
    projector = NodeProjection(access)
    assert first(projector.sample())[1]["visibility"] == "current"
    assert calls == ["life", "subs", "nodes", "subs", "life"]
    def racing_nodes():
        subs["items"][0]["check_receipt"]["checked_at_ms"] = 2000
        return immutable_record(nodes)
    access.node_records = racing_nodes
    stamp = projector.gate.core_stamp
    with pytest.raises(StaleNodeSample):
        projector.sample()
    assert projector.gate.core_stamp == stamp


def test_lifecycle_race_is_rejected():
    nodes, subs, life = facts(checked=1000)
    reads = 0
    def lifecycle():
        nonlocal reads
        reads += 1
        value = deepcopy(life)
        value["core"]["core_generation"] += reads - 1
        return immutable_record(value)
    access = SimpleNamespace(lifecycle_status=lifecycle,
        subscription_records=lambda: immutable_record(subs), node_records=lambda: immutable_record(nodes))
    projector = NodeProjection(access)
    with pytest.raises(StaleNodeSample):
        projector.sample()
    assert not projector.gate.initialized


def test_recheck_progress_fits_jobs_protocol_without_translated_text():
    progress = recheck_progress(1, 2)
    payload = {"op": "test", "name": "subscriptions.refresh", "state": "running",
               "started_at_ms": 1, "progress": progress}
    assert protocol.validate_jobs(payload) == []
    errors = []
    protocol._sensitive(progress, "progress", errors)
    assert errors == []
    assert progress["params"] == {"completed": 1, "total": 2}
    with pytest.raises(ValueError):
        recheck_progress(3, 2)


def test_old_receipt_cannot_unlock_added_cached_member_even_on_repetition():
    projector = NodeProjection()
    projector.project(*facts(checked=1000))
    nodes, subs, life = facts(checked=1000)
    added = deepcopy(nodes["items"][0])
    added.update(id=opaque_id("added cached node"), checked_at_ms=None)
    nodes["items"].append(added)  # Real getter has no observation_origin.
    for _ in range(2):
        result = projector.project(nodes, subs, life)
        assert [node["status"] for node in result["nodes"]["items"]] == ["pending", "pending"]
        assert (first(result)[1]["healthy"], first(result)[1]["visibility"]) == (0, "pending")
        assert result["recheck_progress"] == recheck_progress(0, 2)
    sub = subs["items"][0]
    sub["membership_version"] = 2
    sub["check_receipt"].update(membership_version=2,
        member_ids=[node["id"] for node in nodes["items"]])
    # Even a relabelled receipt with the old timestamp cannot reopen health.
    for _ in range(2):
        assert first(projector.project(nodes, subs, life))[1]["visibility"] == "pending"
    sub["check_receipt"]["checked_at_ms"] = 2000
    result = projector.project(nodes, subs, life)
    assert (first(result)[1]["healthy"], first(result)[1]["visibility"]) == (2, "current")
    assert result["recheck_progress"] == recheck_progress(2, 2)
    assert_schema(result)


def test_member_removal_and_readdition_cannot_replay_original_receipt():
    projector = NodeProjection()
    nodes, subs, life = facts(checked=1000)
    projector.project(nodes, subs, life)
    empty = deepcopy(nodes)
    empty["items"] = []
    assert projector.project(empty, subs, life)["subscriptions"]["items"][0]["healthy"] == 0
    for _ in range(2):
        assert first(projector.project(nodes, subs, life))[0]["status"] == "pending"


@pytest.mark.parametrize("field,value", [("runtime_generation", 6), ("core_generation", 7),
    ("core_pid", 99), ("membership_version", 0), ("member_ids", []),
    ("refresh_job_generation", 99)])
def test_receipt_must_match_current_owner_scope(field, value):
    inputs = facts(checked=1000)
    inputs[1]["items"][0]["check_receipt"][field] = value
    result = NodeProjection().project(*inputs)
    assert first(result)[0]["status"] == "pending"
    assert first(result)[1]["visibility"] == "pending"
    assert result["recheck_progress"]["completed"] == 0


def test_legacy_source_receipt_is_not_member_or_core_evidence():
    inputs = facts(checked=1000)
    sub = inputs[1]["items"][0]
    del sub["check_receipt"]
    del sub["membership_version"]
    result = NodeProjection().project(*inputs)
    assert first(result)[0]["status"] == "pending"
    assert first(result)[1]["healthy"] == 0


def test_close_before_first_sample_survives_initialization_and_incomplete_sample():
    projector = NodeProjection()
    gate = projector.gate
    projector.close()
    incomplete = facts(checked=1000)
    del incomplete[1]["items"][0]["refresh_state"]
    assert_unavailable_nodes(projector.project(*incomplete))
    assert not projector.gate.initialized
    for checked in (1000, 2000):
        result = projector.project(*facts(checked=checked))
        assert projector.gate is gate
        assert first(result)[0]["status"] == "pending"
        assert first(result)[1]["healthy"] == 0
    # A PID change alone is insufficient; Core generation must advance.
    changed_pid = facts(checked=3000)
    changed_pid[2]["core"]["pid"] = 43
    changed_pid[1]["items"][0]["check_receipt"]["core_pid"] = 43
    assert first(projector.project(*changed_pid))[0]["status"] == "pending"
    assert first(projector.project(*facts(checked=4000, core_generation=9)))[0]["status"] == "pending"
    assert first(projector.project(*facts(checked=5000, core_generation=9)))[0]["status"] == "healthy"


@pytest.mark.parametrize("already_sampled", [False, True])
def test_stop_intent_blocks_late_receipts_until_successor_runtime(already_sampled):
    projector = NodeProjection()
    if already_sampled:
        projector.project(*facts(checked=1000))
    for checked in (2000, 3000):
        inputs = facts(checked=checked)
        inputs[2]["intent"]["stop_requested"] = True
        result = projector.project(*inputs)
        assert first(result)[0]["status"] == "pending"
        assert first(result)[1]["visibility"] == ("previous" if already_sampled else "pending")
    assert first(projector.project(*facts(checked=4000)))[0]["status"] == "pending"
    assert first(projector.project(*facts(generation=8, checked=5000)))[0]["status"] == "healthy"


@pytest.mark.parametrize("state", [None, {}, {"refreshing": False},
    {"refreshing": None, "job_generation": 0}, {"refreshing": False, "job_generation": -1}])
def test_missing_refresh_facts_only_reject_subscriptions_without_mutating_gate(state):
    projector = NodeProjection()
    projector.project(*facts(checked=1000))
    inputs = facts(checked=2000)
    sub = inputs[1]["items"][0]
    sub["refreshing"] = False  # The old invented/default fact is insufficient.
    if state is None:
        del sub["refresh_state"]
    else:
        sub["refresh_state"] = state
    watermark = deepcopy(projector.gate.accepted_watermarks)
    assert_unavailable_nodes(projector.project(*inputs))
    assert projector.gate.accepted_watermarks == watermark
    assert first(projector.project(*facts(checked=1000)))[1]["visibility"] == "current"


def test_refresh_owner_job_generation_blocks_old_completion():
    projector = NodeProjection()
    projector.project(*facts(checked=1000))
    inputs = facts(checked=1000)
    sub = inputs[1]["items"][0]
    sub["refresh_state"] = {"refreshing": True, "job_generation": 1}
    assert first(projector.project(*inputs))[1]["refreshing"] is True
    sub["refresh_state"]["refreshing"] = False
    assert first(projector.project(*inputs))[0]["status"] == "pending"
    with pytest.raises(StaleNodeSample, match="retired refresh job"):
        projector.project(*facts(checked=2000))
    sub["check_receipt"].update(refresh_job_generation=1, checked_at_ms=2000)
    assert first(projector.project(*inputs))[1]["visibility"] == "current"


def test_sample_brackets_refresh_owner_and_rejects_job_race():
    nodes, subs, life = facts(checked=1000)
    del subs["items"][0]["refresh_state"]
    refresh = {"generation": 7, "stale": False, "items": [
        {"id": subs["items"][0]["id"], "refreshing": False, "job_generation": 0}]}
    access = SimpleNamespace(lifecycle_status=lambda: immutable_record(life),
        subscription_records=lambda: immutable_record(subs), node_records=lambda: immutable_record(nodes))
    projector = NodeProjection(access, refresh_owner=lambda: immutable_record(refresh))
    assert first(projector.sample())[1]["visibility"] == "current"
    def race():
        refresh["items"][0].update(refreshing=True, job_generation=1)
        return immutable_record(nodes)
    access.node_records = race
    watermark = deepcopy(projector.gate.accepted_watermarks)
    with pytest.raises(StaleNodeSample, match="node owner changed"):
        projector.sample()
    assert projector.gate.accepted_watermarks == watermark


@pytest.mark.parametrize("change", ["stale", "generation", "missing"])
def test_refresh_owner_stale_or_missing_subscription_rejects(change):
    nodes, subs, life = facts(checked=1000)
    refresh = {"generation": 7, "stale": False, "items": [
        {"id": subs["items"][0]["id"], "refreshing": False, "job_generation": 0}]}
    if change == "missing":
        refresh["items"] = []
    elif change == "stale":
        refresh["stale"] = True
    else:
        refresh["generation"] = 6
    projector = NodeProjection()
    if change == "missing":
        assert_unavailable_nodes(projector.project(nodes, subs, life, refresh_records=refresh))
    else:
        with pytest.raises(StaleNodeSample):
            projector.project(nodes, subs, life, refresh_records=refresh)
    assert not projector.gate.initialized


def test_duplicate_source_does_not_borrow_another_refresh_jobs_receipt():
    nodes, subs, life = facts(checked=1000)
    duplicate = deepcopy(subs["items"][0])
    duplicate["id"] = opaque_id("duplicate source config entry")
    duplicate["refresh_state"]["job_generation"] = 1
    subs["items"].append(duplicate)
    result = NodeProjection().project(nodes, subs, life)
    assert first(result)[0]["status"] == "pending"
    assert [sub["visibility"] for sub in result["subscriptions"]["items"]] == ["pending", "pending"]


def test_null_refresh_state_is_incomplete():
    inputs = facts(checked=1000)
    inputs[1]["items"][0]["refresh_state"] = None
    assert_unavailable_nodes(NodeProjection().project(*inputs))


def assert_unavailable_nodes(result):
    assert result["subscriptions"] is None
    assert result["subscriptions_unavailable_reason"] == "refresh_facts_incomplete"
    assert protocol.validate_topic_payload("nodes.list", result["nodes"]) == []
    for node in result["nodes"]["items"]:
        if node["kind"] == "subscription" and node["enabled"]:
            assert (node["status"], node["reason_code"], node["latency_ms"]) == (
                "pending", "subscription_recheck_pending", None)
    assert result["recheck_progress"]["completed"] == 0


def test_real_lifecycle_shape_projects_and_samples():
    access = RuntimeAccess(minimal_runtime())
    lifecycle = access.lifecycle_status()
    assert "generation" not in lifecycle["intent"]
    nodes, subs, _ = facts(generation=2, core_generation=7, checked=1000)
    projected = NodeProjection().project(nodes, subs, lifecycle)
    sampled = NodeProjection(SimpleNamespace(
        lifecycle_status=access.lifecycle_status,
        node_records=lambda: immutable_record(nodes),
        subscription_records=lambda: immutable_record(subs))).sample()
    assert projected == sampled
    assert first(projected)[0]["status"] == "healthy"
    assert first(projected)[1]["visibility"] == "current"
    assert_schema(projected)


@pytest.mark.parametrize("method", ["project", "sample"])
@pytest.mark.parametrize("already_sampled", [False, True])
def test_missing_refresh_still_publishes_manual_automatic_and_pending_nodes(method, already_sampled):
    nodes, subs, life = facts(checked=1000)
    manual = deepcopy(nodes["items"][0])
    manual.update(id=opaque_id("manual"), source_id=None, source_ids=())
    automatic = deepcopy(manual)
    automatic.update(id=opaque_id("automatic"), tier="bastion", pool_kind="automatic")
    nodes["items"].extend([manual, automatic])
    access = SimpleNamespace(lifecycle_status=lambda: immutable_record(life),
        node_records=lambda: immutable_record(nodes), subscription_records=lambda: immutable_record(subs))
    projector = NodeProjection(access)
    if already_sampled:
        assert first(projector.sample())[0]["status"] == "healthy"
    before = deepcopy((projector._generation, projector._previous, projector._members,
                       projector._refresh_jobs, projector.gate.__dict__))
    del subs["items"][0]["refresh_state"]
    result = projector.sample() if method == "sample" else projector.project(nodes, subs, life)
    assert_unavailable_nodes(result)
    assert [node["status"] for node in result["nodes"]["items"]] == ["pending", "healthy", "healthy"]
    assert [node["kind"] for node in result["nodes"]["items"]] == ["subscription", "manual", "automatic"]
    assert before == (projector._generation, projector._previous, projector._members,
                      projector._refresh_jobs, projector.gate.__dict__)
    subs["items"][0]["refresh_state"] = {"refreshing": False, "job_generation": 0}
    restored = projector.sample()
    assert first(restored)[1]["visibility"] == "current"
    for topic, key in (("nodes.list", "nodes"), ("subscriptions.list", "subscriptions")):
        assert protocol.validate_topic_payload(topic, restored[key]) == []
        for tier in ("sentinel", "bastion"):
            frame = {"t": "evt", "proto": 1, "epoch": "test-epoch", "kind": "snapshot",
                     "topic": topic, "key": tier, "rev": 1, "produced_at_ms": 1, "sent_at_ms": 1,
                     "data": {"items": [row for row in restored[key]["items"] if row["tier"] == tier]}}
            assert protocol.validate_frame(frame) == []
