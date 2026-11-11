"""W17: the new UI bridge keeps hand-written tier-ladder keys (spec v1 §7.1).

No bridge code changes: saving the egress_recovery section must retain
``tier_ladder_judge`` / ``overshoot_repair`` inside the hidden
``node_judgement`` block untouched, and the UI snapshot never exposes them.
"""

from __future__ import annotations

import json
from copy import deepcopy

from bridge_runtime_access import config_projection
from bridge_validation import validate_change
from runtime_node_judge import NodeJudgementPolicy

LADDER = {"post_dial_judge": "act", "tier_ladder_judge": True, "overshoot_repair": True}


def test_saving_egress_recovery_retains_the_ladder_keys():
    current = {"network": {"egress_recovery": {"enabled": True, "node_judgement": deepcopy(LADDER)}}}
    result = validate_change(
        {"section": "network", "values": {"egress_recovery": {"enabled": False}}},
        current,
    )
    assert result.valid, result.errors
    assert result.values["egress_recovery"]["node_judgement"] == LADDER
    assert current["network"]["egress_recovery"]["node_judgement"] == LADDER
    assert "network.egress_recovery.node_judgement.tier_ladder_judge" in result.retained_fields
    assert "network.egress_recovery.node_judgement.overshoot_repair" in result.retained_fields
    # The retained block still reads as configured.
    policy = NodeJudgementPolicy.from_config({"client": {"egress_recovery": result.values["egress_recovery"]}})
    assert policy.ladder_policy_key == (True, True, True)


def test_the_ladder_keys_cannot_be_submitted_through_the_bridge():
    current = {"network": {"egress_recovery": {"enabled": True, "node_judgement": deepcopy(LADDER)}}}
    for values in (
        {"egress_recovery": {"node_judgement": {"tier_ladder_judge": False}}},
        {"egress_recovery": {"node_judgement": {"overshoot_repair": False}}},
    ):
        bad = validate_change({"section": "network", "values": values}, current)
        assert not bad.valid and bad.values is None
    assert current["network"]["egress_recovery"]["node_judgement"] == LADDER


def test_the_ui_snapshot_does_not_expose_the_ladder_keys():
    config = {"client": {"egress_recovery": {"enabled": True, "retry_source_refresh": True,
                                             "node_judgement": deepcopy(LADDER)}}}
    before = deepcopy(config)
    projected, _fields_set = config_projection(config, {}, clean=lambda value: value)
    text = json.dumps(projected)
    assert "tier_ladder_judge" not in text and "overshoot_repair" not in text
    assert "node_judgement" not in text
    assert set(projected["network"]["egress_recovery"]) <= {"enabled", "retry_source_refresh"}
    assert config == before
