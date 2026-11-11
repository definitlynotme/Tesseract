"""P1 IPC 合同的跨语言样例与标准库编解码契约。"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

import bridge_protocol as protocol


FIXTURES = Path(__file__).resolve().parents[1] / "bridge_schema" / "fixtures"
V1_VALID = FIXTURES / "v1" / "valid"
V1_INVALID = FIXTURES / "v1" / "invalid"
VALID = FIXTURES / "valid"
INVALID = FIXTURES / "invalid"
FRAME_TYPES = {
    "hello", "hello_ack", "req", "ack", "res", "evt", "batch", "cancel",
    "error", "ping", "pong", "fatal", "bye", "subscribe", "subscribed",
    "snapshot_req", "snapshot_res", "owner_revoke",
}
P1_TOPICS = {
    "service.state", "route.published", "tiers.summary", "traffic.tick",
    "traffic.history", "logs.batch", "core.info", "jobs",
}
SNAPSHOT_TOPICS = P1_TOPICS - {"logs.batch", "jobs"}
REQUIRED_INVALID_FIXTURES = {
    "base_rev_mismatch.json",
    "invalid_utf8.json",
    "missing_base_rev.json",
    "missing_epoch.json",
    "missing_field.json",
    "oversize.json",
    "sensitive_field.json",
    "wrong_readiness_enum.json",
    "invalid_subscribe_topic.json",
    "invalid_snapshot_req_topic.json",
    "invalid_snapshot_res_topic.json",
    "invalid_jobs_snapshot.json",
    "invalid_jobs_patch.json",
    "invalid_log_drop_total.json",
    "invalid_gap_reverse.json",
}
EXPECTED_INVALID_FRAME_ERRORS = {
    "missing_field.json": "missing:args",
    "missing_epoch.json": "missing:epoch",
    "wrong_readiness_enum.json": "enum:service.state.readiness.state",
    "sensitive_field.json": "sensitive:data.password",
    "missing_base_rev.json": "missing:base_rev",
    "invalid_subscribe_topic.json": "type:topics",
    "invalid_snapshot_req_topic.json": "enum:topic",
    "invalid_snapshot_res_topic.json": "invariant:snapshot",
    "invalid_jobs_snapshot.json": "not_allowed:topic.kind",
    "invalid_jobs_patch.json": "not_allowed:topic.kind",
    "invalid_log_drop_total.json": "invariant:logs.batch.dropped_total",
    "invalid_gap_reverse.json": "invariant:data.gap",
}


def _json(path: Path) -> dict:
    return json.loads(path.read_bytes().decode("utf-8"))


def _valid_paths() -> list[Path]:
    return sorted(VALID.glob("*.json"))


def _invalid_paths() -> list[Path]:
    return sorted(INVALID.glob("*.json"))


@pytest.mark.parametrize("path", sorted(V1_VALID.glob("*.json")), ids=lambda path: path.name)
def test_v1_fixtures_validate_and_round_trip(path: Path) -> None:
    frame = _json(path)
    assert protocol.validate_frame(frame) == []
    assert protocol.decode_line(protocol.encode_frame(frame)) == frame


def test_v1_inventory_covers_full_contract_and_replies() -> None:
    assert {path.stem.removeprefix("command_") for path in V1_VALID.glob("command_*.json")} == protocol.V1_COMMANDS
    assert {path.stem.removeprefix("query_") for path in V1_VALID.glob("query_*.json")} == protocol.V1_QUERIES
    assert {path.stem.removeprefix("topic_") for path in V1_VALID.glob("topic_*.json")} | P1_TOPICS == protocol.V1_TOPICS
    # 提醒 redesign 10-30: alerts.onboarding.* (4), alerts.catalog / alerts.deliveries
    # and the alerts.delivery / alerts.onboarding topics were retired.
    assert (len(protocol.V1_COMMANDS), len(protocol.V1_QUERIES), len(protocol.V1_TOPICS)) == (34, 8, 29)
    assert len(protocol.PAGE_COMMANDS) == 27
    assert len(protocol.SHELL_COMMANDS) == 7
    for name, spec in protocol.V1_COMMAND_SPECS.items():
        request = _json(V1_VALID / f"command_{name}.json")
        result = _json(V1_VALID / f"result_{name}.json")
        assert protocol.validate_frame(result) == []
        if spec[2] == "job":
            reply = _json(V1_VALID / f"ack_{name}.json")
        else:
            reply = result
        assert protocol.validate_response(request, reply) == []
    for name in protocol.V1_QUERIES:
        assert protocol.validate_response(_json(V1_VALID / f"query_{name}.json"),
                                          _json(V1_VALID / f"response_{name}.json")) == []


@pytest.mark.parametrize("path", sorted(V1_INVALID.glob("*.json")), ids=lambda path: path.name)
def test_v1_invalid_fixture_has_specific_failure(path: Path) -> None:
    case = _json(path)
    kind = case["validator"]
    if kind == "patch":
        assert protocol.apply_topic_patch(case["baseline"], case["frame"]) == (None, True)
        return
    if kind == "pages":
        errors = protocol.validate_snapshot_pages(case["pages"])
    elif kind == "response":
        errors = protocol.validate_response(case["request"], case["frame"])
    elif kind == "page":
        errors = protocol.validate_page_request(case["frame"])
    else:
        errors = protocol.validate_frame(case["frame"])
    assert case["expected"] in errors


@pytest.mark.parametrize("name", sorted(protocol.SHELL_COMMANDS))
def test_page_cannot_forge_shell_origin(name: str) -> None:
    request = _json(V1_VALID / f"command_{name}.json")
    assert "not_allowed:page" in protocol.validate_page_request(request)
    request["from"] = "page"
    assert "not_allowed:cmd" in protocol.validate_frame(request)


@pytest.mark.parametrize("name", sorted(protocol.SHELL_TOPICS))
def test_page_cannot_subscribe_shell_topics(name: str) -> None:
    request = _json(VALID / "frame_subscribe.json")
    request["topics"] = [name]
    assert protocol.validate_frame(request) == []
    assert "not_allowed:page.topics" in protocol.validate_page_request(request)


@pytest.mark.parametrize("name,spec", list(protocol.V1_COMMAND_SPECS.items()))
def test_v1_required_parameters_cannot_be_omitted(name: str, spec: tuple) -> None:
    request = _json(V1_VALID / f"command_{name}.json")
    for field in spec[4]:
        if field.endswith("?"):
            continue
        changed = deepcopy(request)
        del changed["args"][field]
        assert f"missing:args.{field}" in protocol.validate_frame(changed)
    if name not in protocol.P1_COMMANDS:
        for field in ("owner", "req_id"):
            changed = deepcopy(request)
            del changed[field]
            assert f"missing:{field}" in protocol.validate_frame(changed)


@pytest.mark.parametrize("command,path", [
    ("settings.apply", ("changes", 0, "secrets", 0, "value")),
    ("settings.validate", ("value",)), ("nodes.update", ("secrets", 0, "value")),
    ("import.preview", ("text",)), ("import.apply", ("manual", "password")),
])
def test_private_inputs_are_precise_and_do_not_exempt_extensions(command: str, path: tuple) -> None:
    request = _json(V1_VALID / f"command_{command}.json")
    if command == "settings.apply":
        request["args"]["changes"][0]["secrets"] = [{"field": "password", "action": "set", "value": "fixture"}]
    if command == "nodes.update":
        request["args"]["secrets"] = [{"field": "password", "action": "set", "value": "fixture"}]
    if command == "import.apply":
        request["args"].pop("plan_id")
        request["args"].update(mode="manual", manual={})
    target = request["args"]
    for slot in path[:-1]:
        target = target[slot]
    target[path[-1]] = "https://fixture.example/?token=not-real"
    assert protocol.validate_frame(request) == []
    request["extension"] = {"token": "not-real"}
    assert "sensitive:extension.token" in protocol.validate_frame(request)


def test_private_responses_never_become_jobs_and_require_full_correlation() -> None:
    request = _json(V1_VALID / "command_nodes.reveal.json")
    response = _json(V1_VALID / "private_nodes.reveal.json")
    assert protocol.validate_response(request, response) == []
    for field in ("epoch", "id", "req_id", "owner", "cmd"):
        changed = deepcopy(response)
        changed[field] = 2 if field == "id" else "other"
        assert f"invariant:reply.{field}" in protocol.validate_response(request, changed)
    job = _json(V1_VALID / "result_nodes.update.json")
    job["data"].update(name="nodes.reveal", result=response["data"])
    assert "enum:jobs.name" in protocol.validate_frame(job)
    assert any(error.startswith("sensitive:") for error in protocol.validate_frame(job))


def test_keyed_pages_and_patch_oracles_enforce_identity_and_complete_baseline() -> None:
    pages = [_json(V1_VALID / f"page_nodes.list_{index}.json") for index in (0, 1)]
    assert protocol.validate_snapshot_pages(pages) == []
    assert protocol.validate_snapshot_pages(pages[:1])
    assert protocol.validate_snapshot_pages(list(reversed(pages)))
    baseline = _json(V1_VALID / "topic_nodes.list.json")
    patch = _json(V1_VALID / "patch_nodes.list.json")
    merged, needed = protocol.apply_topic_patch(baseline, patch)
    assert not needed and merged["rev"] == 2
    assert baseline["rev"] == 1
    assert protocol.apply_topic_patch(pages[0], patch) == (None, True)
    for field in ("epoch", "key"):
        changed = deepcopy(patch)
        changed[field] = "different"
        assert protocol.apply_topic_patch(baseline, changed) == (None, True)


@pytest.mark.parametrize("command,field,value", [
    ("geo.update", "triggered", False), ("geo.update", "completed", False),
    ("app.update.install", "verified", False), ("app.update.install", "handoff_pending", False),
    ("nodes.diagnose", "cleanup_pending", True), ("settings.apply", "sections", []),
    ("mode.set", "applied", False), ("subscriptions.refresh", "stored", False),
])
def test_done_does_not_mask_partial_or_failed_business_results(command, field, value) -> None:
    frame = _json(V1_VALID / f"result_{command}.json")
    frame["data"]["result"][field] = value
    assert protocol.validate_frame(frame)
    frame["data"]["state"] = "failed"
    frame["data"]["error"] = {"code": "io", "message_key": "errors.operation.failed", "params": {}}
    assert protocol.validate_frame(frame) == []


def test_saved_pending_is_distinct_from_saved_not_applied_failure() -> None:
    pending = _json(V1_VALID / "settings_saved_pending.json")
    assert protocol.validate_frame(pending) == []
    pending["data"]["result"]["pending"] = None
    assert "invariant:jobs.result.pending" in protocol.validate_frame(pending)
    assert protocol.validate_frame(_json(V1_VALID / "mode_saved_not_applied.json")) == []


@pytest.mark.parametrize("query", ["logs.tail", "logs.query"])
def test_query_log_free_text_is_diagnostic_but_structured_credentials_rejected(query) -> None:
    frame = _json(V1_VALID / f"response_{query}.json")
    frame["data"]["records"][0]["raw"] = "password=fixture-only"
    protocol.privacy_diagnostics.clear()
    assert protocol.validate_frame(frame) == []
    assert list(protocol.privacy_diagnostics)
    frame["data"]["records"][0]["token"] = "fixture-only"
    assert "sensitive:data.records[0].token" in protocol.validate_frame(frame)


@pytest.mark.parametrize("key", ["deadline", "monotonic", "deadline_ms", "monotonic_ms", "monotonic_deadline_ms"])
def test_internal_absolute_clocks_remain_forbidden_in_private_and_public_objects(key) -> None:
    for command,slot in (("import.apply", "manual"), ("nodes.update", "updates")):
        frame = _json(V1_VALID / f"command_{command}.json")
        if command == "import.apply":  # a private object slot (alerts.test.draft until 10-30)
            frame["args"].pop("plan_id", None)
            frame["args"].update(mode="manual", manual={})
        frame["args"][slot][key] = 123
        assert f"sensitive:args.{slot}.{key}" in protocol.validate_frame(frame)
        del frame["args"][slot][key]
        frame["args"][slot]["remaining_ms"] = 123
        assert protocol.validate_frame(frame) == []


def test_protocol_document_is_utf8_and_records_full_inventory() -> None:
    raw = (FIXTURES.parent / "PROTOCOL.md").read_bytes()
    assert raw.count(b"\r\n") == 0
    doc = raw.decode("utf-8")
    assert "??" not in doc
    assert "34 条命令、8 条查询、29 个主题" in doc
    assert "页面运行时命令（23）" in doc and "外壳专用命令（7）" in doc
    for name in protocol.V1_COMMANDS | protocol.V1_QUERIES | protocol.V1_TOPICS:
        assert f"`{name}`" in doc


def test_startup_failure_examples_have_correlated_structured_reasons() -> None:
    for path in V1_VALID.glob("startup_*.json"):
        frame = _json(path)
        state = frame["data"]
        assert state["runtime"]["failure"]["code"] == state["runtime"]["failure_code"]
        assert state["runtime"]["failure"]["message_key"].startswith("errors.start.")
        assert state["readiness"]["state"] not in {"ready", "serving"}
        assert state["core"]["running"] is False
        if state["runtime"]["failure_code"] == "superseded":
            assert state["runtime"]["startup_phase"] == "stopped"
        bad = deepcopy(frame)
        bad["data"]["runtime"]["startup_phase"] = "failed"
        bad["data"]["readiness"]["state"] = "ready"
        assert "invariant:service.state.failed_readiness" in protocol.validate_frame(bad)


@pytest.mark.parametrize("name,field", [
    ("result_settings.validate", "valid"), ("result_host.saveFile", "ref"),
])
def test_direct_response_semantics_are_not_just_object_shapes(name, field) -> None:
    frame = _json(V1_VALID / f"{name}.json")
    if field == "valid":
        frame["data"][field] = False
    elif field == "state":
        frame["data"][field]["state"] = "pending"
    else:
        frame["data"][field] = "unissued-ref"
    assert f"invariant:data.{field}" in protocol.validate_frame(frame)


@pytest.mark.parametrize("name,location", [
    ("command_shell.saveGrant", "args"), ("result_import.preview", "data"),
    ("topic_update.prepareExit", "data"),
])
def test_new_grants_and_handoffs_cannot_already_be_expired(name, location) -> None:
    frame = _json(V1_VALID / f"{name}.json")
    frame[location]["expires_at_ms"] = frame["produced_at_ms"]
    assert "invariant:expires_at_ms" in protocol.validate_frame(frame)


def test_identity_update_and_gateway_success_require_observed_facts() -> None:
    exit_frame = _json(V1_VALID / "result_exit.verify.json")
    exit_frame["data"]["result"]["binding_id"] = None
    assert "invariant:jobs.result.fresh" in protocol.validate_frame(exit_frame)
    update = _json(V1_VALID / "result_app.update.check.json")
    update["data"]["result"]["version"] = None
    assert "invariant:jobs.result.version" in protocol.validate_frame(update)
    gateway = _json(V1_VALID / "result_gateway.select.json")
    gateway["data"]["result"]["selection"] = None
    assert "invariant:jobs.result.selection" in protocol.validate_frame(gateway)
    request = _json(V1_VALID / "command_settings.apply.json")
    result = _json(V1_VALID / "result_settings.apply.json")
    assert [row["section"] for row in result["data"]["result"]["sections"]] == [row["section"] for row in request["args"]["changes"]]


def test_job_error_uses_typed_closed_error_contract() -> None:
    frame = _json(V1_VALID / "mode_saved_not_applied.json")
    frame["data"]["error"]["reason_code"] = 123
    frame["data"]["error"]["unexpected"] = {"safe": True}
    errors = protocol.validate_frame(frame)
    assert "type:jobs.error.reason_code" in errors
    assert "unknown:jobs.error" in errors


def test_fixture_inventory_covers_every_p1_frame_and_topic() -> None:
    frames = {_json(path)["t"] for path in VALID.glob("frame_*.json")}
    topics = {_json(path)["topic"] for path in VALID.glob("topic_*.json")}
    assert frames == FRAME_TYPES
    assert topics == P1_TOPICS
    batch = _json(VALID / "frame_batch.json")
    assert batch["tick_seq"] > 0
    assert len({item["topic"] for item in batch["items"]}) > 1


def test_required_invalid_fixture_inventory_is_fixed() -> None:
    assert REQUIRED_INVALID_FIXTURES <= {path.name for path in _invalid_paths()}
    frame_cases = {
        path.name for path in _invalid_paths()
        if _json(path).get("case") not in {"transport", "patch_mismatch"}
    }
    assert frame_cases == set(EXPECTED_INVALID_FRAME_ERRORS)


@pytest.mark.parametrize("path", _valid_paths(), ids=lambda path: path.name)
def test_valid_fixture_passes_frame_validation(path: Path) -> None:
    frame = _json(path)
    assert protocol.validate_frame(frame) == []
    if path.name.startswith("topic_") and frame["kind"] in {"snapshot", "edge"}:
        assert protocol.validate_topic_payload(frame["topic"], frame["data"]) == []


@pytest.mark.parametrize("path", _valid_paths(), ids=lambda path: path.name)
def test_valid_fixture_round_trips_as_one_ndjson_line(path: Path) -> None:
    frame = _json(path)
    encoded = protocol.encode_frame(frame)
    assert encoded.endswith(b"\n")
    assert encoded.count(b"\n") == 1
    assert protocol.decode_line(encoded) == frame


@pytest.mark.parametrize("path", _invalid_paths(), ids=lambda path: path.name)
def test_invalid_fixture_is_rejected_with_expected_reason(path: Path) -> None:
    case = _json(path)
    if case.get("case") == "transport":
        if "raw_hex" in case:
            wire = bytes.fromhex(case["raw_hex"])
        else:
            wire = case["repeat"].encode("utf-8") * case["count"]
        with pytest.raises(ValueError, match=case["error"]):
            protocol.decode_line(wire)
        return
    if case.get("case") == "patch_mismatch":
        merged, needs_snapshot = protocol.apply_patch(case["baseline"], case["patch"])
        assert merged is None
        assert needs_snapshot is True
        return

    errors = protocol.validate_frame(case)
    assert errors, f"{path.name} was accepted"
    assert EXPECTED_INVALID_FRAME_ERRORS[path.name] in errors
    with pytest.raises(ValueError, match="invalid_frame"):
        protocol.encode_frame(case)


def test_patch_merges_by_id_without_changing_source() -> None:
    baseline = {
        "rev": 8,
        "items": [{"id": "node-a", "usable": True}, {"id": "node-b", "usable": True}],
    }
    patch = {
        "base_rev": 8,
        "rev": 9,
        "changed": [{"id": "node-a", "usable": False}, {"id": "node-c", "usable": True}],
        "removed": ["node-b"],
    }
    merged, needs_snapshot = protocol.apply_patch(baseline, patch)
    assert needs_snapshot is False
    assert merged is not None
    assert merged["rev"] == 9
    assert {row["id"]: row["usable"] for row in merged["items"]} == {
        "node-a": False, "node-c": True,
    }
    assert baseline["items"][0]["usable"] is True
    assert len(baseline["items"]) == 2


def test_decode_rejects_non_object_json_and_malformed_json() -> None:
    for wire in (b"[]\n", b"{broken}\n"):
        with pytest.raises(ValueError):
            protocol.decode_line(wire)


def test_encode_rejects_frame_over_hard_limit() -> None:
    frame = _json(VALID / "frame_res.json")
    frame["data"] = {"message": "x" * 4_194_304}
    with pytest.raises(ValueError):
        protocol.encode_frame(frame)


@pytest.mark.parametrize(
    "fixture,field,bad_value",
    [
        ("frame_req.json", ("cmd",), []),
        ("frame_req.json", ("from",), {}),
        ("topic_service_state.json", ("data", "readiness", "state"), []),
        ("topic_route_published.json", ("data", "managed_tier"), {}),
        ("topic_tiers_summary.json", ("data", "carrier"), []),
        ("topic_traffic_history.json", ("data", "source"), {}),
    ],
)
def test_bad_json_types_return_errors_without_raising(
    fixture: str, field: tuple[str, ...], bad_value: object,
) -> None:
    frame = _json(VALID / fixture)
    target = frame
    for key in field[:-1]:
        target = target[key]
    target[field[-1]] = bad_value
    errors = protocol.validate_frame(frame)
    assert isinstance(errors, list) and errors
    if frame["t"] == "evt":
        topic_errors = protocol.validate_topic_payload(frame["topic"], frame["data"])
        assert isinstance(topic_errors, list) and topic_errors


JSON_TYPE_REPLACEMENTS = ([], {}, None, True, 1.5, "")
SINGLE_ARGUMENT_VALIDATORS = tuple(
    getattr(protocol, name)
    for name in sorted(dir(protocol))
    if name.startswith("validate_")
    and name not in {"validate_control_reply", "validate_topic_payload"}
)


@pytest.mark.parametrize("validator", SINGLE_ARGUMENT_VALIDATORS, ids=lambda fn: fn.__name__)
@pytest.mark.parametrize("value", JSON_TYPE_REPLACEMENTS, ids=["list", "dict", "null", "bool", "float", "empty_string"])
def test_every_public_validator_returns_an_error_list_for_json_root_types(
    validator, value: object,
) -> None:
    errors = validator(deepcopy(value))
    assert isinstance(errors, list)
    assert all(isinstance(error, str) for error in errors)


@pytest.mark.parametrize("value", JSON_TYPE_REPLACEMENTS, ids=["list", "dict", "null", "bool", "float", "empty_string"])
def test_multi_argument_validators_return_error_lists_for_json_root_types(value: object) -> None:
    topic_errors = protocol.validate_topic_payload(deepcopy(value), deepcopy(value))
    payload_errors = protocol.validate_topic_payload("core.info", deepcopy(value))
    reply_errors = protocol.validate_control_reply(deepcopy(value), deepcopy(value))
    for errors in (topic_errors, payload_errors, reply_errors):
        assert isinstance(errors, list)
        assert all(isinstance(error, str) for error in errors)


def _json_field_paths(value: object, parent: tuple[str | int, ...] = ()):
    if isinstance(value, dict):
        for key, child in value.items():
            path = (*parent, key)
            yield path
            yield from _json_field_paths(child, path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            path = (*parent, index)
            yield path
            yield from _json_field_paths(child, path)


@pytest.mark.parametrize("path", _valid_paths() + sorted(V1_VALID.glob("*.json")), ids=lambda path: path.name)
def test_every_valid_fixture_field_accepts_json_type_fuzz_without_exceptions(path: Path) -> None:
    fixture = _json(path)
    direct_validator = getattr(protocol, f"validate_{fixture['t']}")
    for field_path in _json_field_paths(fixture):
        for replacement in JSON_TYPE_REPLACEMENTS:
            frame = deepcopy(fixture)
            parent = frame
            for part in field_path[:-1]:
                parent = parent[part]
            parent[field_path[-1]] = deepcopy(replacement)
            try:
                for validator in (protocol.validate_frame, direct_validator):
                    errors = validator(frame)
                    assert isinstance(errors, list)
                    assert all(isinstance(error, str) for error in errors)
                if fixture["t"] == "evt":
                    topic_errors = protocol.validate_topic_payload(
                        frame.get("topic"), frame.get("data")
                    )
                    assert isinstance(topic_errors, list)
                    assert all(isinstance(error, str) for error in topic_errors)
                try:
                    encoded = protocol.encode_frame(frame)
                except ValueError:
                    pass
                else:
                    assert protocol.decode_line(encoded) == frame
            except Exception as exc:
                pytest.fail(f"{path.name} {field_path!r} -> {replacement!r}: {exc!r}")


@pytest.mark.parametrize("field,bad_value", [("id", 0), ("id", -1), ("id", "17"), ("req_id", 17)])
def test_request_ids_have_distinct_wire_types(field: str, bad_value: object) -> None:
    frame = _json(VALID / "frame_req.json")
    frame[field] = bad_value
    assert any(error.endswith(f":{field}") for error in protocol.validate_frame(frame))


def test_hello_ack_acceptance_requires_protocol_v1() -> None:
    frame = _json(VALID / "frame_hello_ack.json")
    frame["proto"] = 2
    assert any("proto" in error for error in protocol.validate_frame(frame))


@pytest.mark.parametrize("field,bad_value", [("level", "trace"), ("source", "USER"), ("category", "secret")])
def test_log_record_enums_reject_unknown_values(field: str, bad_value: str) -> None:
    frame = _json(VALID / "topic_logs_batch.json")
    frame["data"]["records"][0][field] = bad_value
    assert any(error.endswith(f".{field}") for error in protocol.validate_frame(frame))
    assert any(error.endswith(f".{field}") for error in protocol.validate_topic_payload("logs.batch", frame["data"]))


def _unpublished_route_frame() -> dict:
    frame = _json(VALID / "topic_route_published.json")
    frame["data"].update(
        owner=None,
        selected_chain=[],
        leaf=None,
        managed_tier=None,
        valid=False,
        invalid_reason="no_published_route",
    )
    return frame


def test_route_with_no_published_snapshot_clears_cached_route() -> None:
    frame = _unpublished_route_frame()
    assert protocol.validate_frame(frame) == []
    assert protocol.validate_topic_payload("route.published", frame["data"]) == []


@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("valid", True),
        ("selected_chain", ["TIER1"]),
        ("leaf", "node-a"),
        ("managed_tier", "vanguard"),
        ("invalid_reason", "stale"),
    ],
)
def test_null_route_owner_requires_explicit_empty_state(field: str, bad_value: object) -> None:
    frame = _unpublished_route_frame()
    frame["data"][field] = bad_value
    assert "invariant:route.published.owner" in protocol.validate_frame(frame)
    assert "invariant:route.published.owner" in protocol.validate_topic_payload(
        "route.published", frame["data"]
    )


@pytest.mark.parametrize(
    "name",
    ["sensitive_log_json.json", "sensitive_log_pem.json", "sensitive_error_detail.json"],
)
def test_sensitive_free_text_records_diagnostic_before_encoding(name: str) -> None:
    frame = _json(VALID / name)
    _assert_text_diagnostic(frame)


def _assert_text_diagnostic(frame):
    protocol.privacy_diagnostics.clear()
    assert protocol.validate_frame(frame) == []
    assert protocol.privacy_diagnostics
    assert all(item == {"code": "free_text_sensitive", "surface": item["surface"]}
               and item["surface"] in {"logs", "error"}
               for item in protocol.privacy_diagnostics)
    assert protocol.decode_line(protocol.encode_frame(frame)) == frame


def test_escaped_json_credential_text_records_diagnostic() -> None:
    frame = _json(VALID / "topic_logs_batch.json")
    frame["data"]["records"][0]["raw"] = r'{\"password\":\"CANARY_PASSWORD\",\"token\":\"CANARY_TOKEN\"}'
    _assert_text_diagnostic(frame)


CREDENTIAL_KEYS = (
    "password", "passwd", "pwd", "secret", "token", "apikey", "key",
    "authorization", "cookie", "session", "credential", "credentials",
    "access_token", "refresh_token", "client_secret", "api_key", "private_key",
)
FREE_TEXT_LOCATIONS = ("raw", "detail", "title", "error.detail", "error.params")


def _free_text_frame(location: str, value: str) -> tuple[dict, str]:
    if location in {"raw", "detail", "title"}:
        frame = _json(VALID / "topic_logs_batch.json")
        frame["data"]["records"][0][location] = value
        return frame, f"sensitive:data.records[0].{location}"
    frame = _json(VALID / "frame_error.json")
    if location == "error.detail":
        frame["error"]["detail"] = value
        return frame, "sensitive:error.detail"
    frame["error"]["params"]["text"] = value
    return frame, "sensitive:error.params.text"


@pytest.mark.parametrize("location", FREE_TEXT_LOCATIONS)
@pytest.mark.parametrize("form", ["json", "escaped_json", "key_value", "query"])
@pytest.mark.parametrize("key", CREDENTIAL_KEYS)
def test_credential_keys_are_diagnostic_in_all_free_text_surfaces(
    location: str, form: str, key: str,
) -> None:
    quoted = json.dumps({key: "CANARY"})
    text = {
        "json": quoted,
        "escaped_json": quoted.replace('"', r'\"'),
        "key_value": f"{key}=CANARY",
        "query": f"/auth/callback?{key}=CANARY",
    }[form]
    frame, expected_error = _free_text_frame(location, text)
    _assert_text_diagnostic(frame)
    assert protocol.decode_line(protocol.encode_frame(frame)) == frame


@pytest.mark.parametrize("location", FREE_TEXT_LOCATIONS)
@pytest.mark.parametrize("form", ["json", "escaped_json", "key_value", "query"])
@pytest.mark.parametrize(
    "key",
    ["token_count", "session_id", "session_count", "secret_count", "api_key_count"],
)
def test_non_credential_field_names_are_not_rejected_as_secrets(
    location: str, form: str, key: str,
) -> None:
    quoted = json.dumps({key: 7})
    text = {
        "json": quoted,
        "escaped_json": quoted.replace('"', r'\"'),
        "key_value": f"{key}=7",
        "query": f"/metrics?{key}=7",
    }[form]
    frame, _ = _free_text_frame(location, text)
    assert protocol.validate_frame(frame) == []
    assert protocol.decode_line(protocol.encode_frame(frame)) == frame


@pytest.mark.parametrize("location", FREE_TEXT_LOCATIONS)
@pytest.mark.parametrize("text", [r'{"access\u005ftoken":"CANARY"}',
                                   r'{\"CLIENT\u005fSECRET\"\u003a\"CANARY\"}'])
def test_json_escaped_credential_text_records_diagnostic(location, text):
    frame, _ = _free_text_frame(location, text)
    _assert_text_diagnostic(frame)


@pytest.mark.parametrize("location", FREE_TEXT_LOCATIONS)
@pytest.mark.parametrize(
    "text",
    [
        r"\u0074oken=CANARY",
        r"\u0074oken\u003dCANARY",
        r"\\u0074oken\\u003dCANARY",
    ],
)
def test_escaped_assignment_is_fully_decoded_before_key_scanning(
    location: str, text: str,
) -> None:
    frame, expected_error = _free_text_frame(location, text)
    _assert_text_diagnostic(frame)
    assert protocol.decode_line(protocol.encode_frame(frame)) == frame


@pytest.mark.parametrize("location", FREE_TEXT_LOCATIONS)
@pytest.mark.parametrize(
    "text",
    [
        "访问https://example.test/path完成",
        "令牌Bearer abc.def完成",
        # PEM markers are split so the source never holds one (public-release audit).
        "材料-----BEGIN " + "PRIVATE KEY-----如下",
    ],
)
def test_sensitive_value_patterns_use_ascii_alphanumeric_boundaries(
    location: str, text: str,
) -> None:
    frame, expected_error = _free_text_frame(location, text)
    _assert_text_diagnostic(frame)
    assert protocol.decode_line(protocol.encode_frame(frame)) == frame


@pytest.mark.parametrize("location", FREE_TEXT_LOCATIONS)
@pytest.mark.parametrize(
    "encoded_key",
    ["to%6ben", "%74%6f%6b%65%6e", "api_%6bey"],
)
def test_query_key_is_percent_decoded_before_sensitive_key_scanning(
    location: str, encoded_key: str,
) -> None:
    frame, expected_error = _free_text_frame(
        location, f"/callback?{encoded_key}=CANARY",
    )
    _assert_text_diagnostic(frame)
    assert protocol.decode_line(protocol.encode_frame(frame)) == frame


@pytest.mark.parametrize(
    "key",
    [
        "metrics.token", "metrics_token", "metrics-token", "metrics token",
        "METRICS/TOKEN", "metrics@token", "api_key", "message_key",
        "token_", "metrics.token-", "metrics[token]", r"token\u005f",
        'token"', "token\\",
    ],
)
def test_structured_and_text_keys_share_sensitive_suffix_rule(key: str) -> None:
    structured = _json(VALID / "frame_error.json")
    structured["error"]["params"][key] = "CANARY"
    assert f"sensitive:error.params.{key}" in protocol.validate_error(structured)
    assert f"sensitive:error.params.{key}" in protocol.validate_frame(structured)

    text, expected_error = _free_text_frame("raw", json.dumps({key: "CANARY"}))
    _assert_text_diagnostic(text)
    with pytest.raises(ValueError, match="invalid_frame:.*sensitive:"):
        protocol.encode_frame(structured)
    assert protocol.decode_line(protocol.encode_frame(text)) == text


@pytest.mark.parametrize(
    "key",
    [
        "token_count", "TOKEN_COUNT", "session_id", "SESSION-ID",
        "token:count", "token=count", "session:id", "session=id",
        "token:count\n", 'session:id"',
    ],
)
def test_structured_and_text_keys_share_measurement_or_identifier_exception(
    key: str,
) -> None:
    structured = _json(VALID / "frame_error.json")
    structured["error"]["params"][key] = 7
    text, _ = _free_text_frame("raw", json.dumps({key: 7}))
    for frame in (structured, text):
        assert protocol.validate_frame(frame) == []
        assert protocol.decode_line(protocol.encode_frame(frame)) == frame


@pytest.mark.parametrize(
    "key",
    [
        "token_", "metrics.token-", "metrics[token]", "token}", "token,",
        "token;", "token&", "token?", "token{", r"token\u007d",
    ],
)
@pytest.mark.parametrize("form", ["key_value", "query"])
def test_bare_text_key_with_trailing_separator_uses_last_nonempty_segment(
    key: str, form: str,
) -> None:
    value = {
        "key_value": f"{key}=CANARY",
        "query": f"/callback?{key}=CANARY",
    }[form]
    frame, expected_error = _free_text_frame("raw", value)
    _assert_text_diagnostic(frame)
    assert protocol.decode_line(protocol.encode_frame(frame)) == frame


@pytest.mark.parametrize("key", ['token"', 'metrics"token"'])
def test_json_escaped_quoted_key_value_rejects_sensitive_suffix(key: str) -> None:
    frame, expected_error = _free_text_frame(
        "raw", f"{json.dumps(key)}=CANARY",
    )
    _assert_text_diagnostic(frame)
    assert protocol.decode_line(protocol.encode_frame(frame)) == frame


@pytest.mark.parametrize(
    ("key", "form"),
    [
        ("token:count", "quoted_key_value"),
        ("session:id", "quoted_key_value"),
        ("token=count", "quoted_key_value"),
        ("session=id", "quoted_key_value"),
        ("token:count\n", "quoted_key_value"),
        ('session:id"', "quoted_key_value"),
        ("token:count", "query"),
        ("session:id", "query"),
    ],
)
def test_text_assignment_preserves_safe_final_segment(key: str, form: str) -> None:
    text = {
        "quoted_key_value": f"{json.dumps(key)}=7",
        "query": f"/callback?{key}=7",
    }[form]
    frame, _ = _free_text_frame("raw", text)
    assert protocol.validate_frame(frame) == []
    assert protocol.decode_line(protocol.encode_frame(frame)) == frame


@pytest.mark.parametrize("location", FREE_TEXT_LOCATIONS)
@pytest.mark.parametrize(
    "text",
    [
        "token: abc=", "secret: xyz=", "password: value=",
        "cookie: sid=", "session: id=", "Authorization: Basic abc=",
    ],
)
def test_colon_credential_assignment_is_not_masked_by_later_equals(
    location: str, text: str,
) -> None:
    frame, expected_error = _free_text_frame(location, text)
    _assert_text_diagnostic(frame)
    assert protocol.decode_line(protocol.encode_frame(frame)) == frame


def test_excessively_nested_json_escapes_are_rejected_conservatively() -> None:
    nested_separator = r"\u005c" + "u005c" * 40 + "u005f"
    key = f"access{nested_separator}token"

    structured = _json(VALID / "frame_error.json")
    structured["error"]["params"][key] = "CANARY"
    assert f"sensitive:error.params.{key}" in protocol.validate_frame(structured)

    text, expected_error = _free_text_frame("raw", f'{{"{key}":"CANARY"}}')
    _assert_text_diagnostic(text)
    assert protocol.decode_line(protocol.encode_frame(text)) == text


def test_final_allowed_escape_layer_is_scanned_before_acceptance() -> None:
    structured_separator = r"\u005c" + "u005c" * 30 + "u005f"
    structured_key = f"access{structured_separator}token"
    structured = _json(VALID / "frame_error.json")
    structured["error"]["params"][structured_key] = "CANARY"
    assert f"sensitive:error.params.{structured_key}" in protocol.validate_frame(
        structured
    )

    text_separator = r"\u005c" + "u005c" * 31 + "u005f"
    text_key = f"access{text_separator}token"
    text, expected_error = _free_text_frame(
        "raw", f'{{"{text_key}":"CANARY"}}',
    )
    _assert_text_diagnostic(text)
    with pytest.raises(ValueError, match="invalid_frame:.*sensitive:"):
        protocol.encode_frame(structured)
    _assert_text_diagnostic(text)


def test_fixed_schema_key_exception_is_scoped_to_its_exact_path() -> None:
    frame = _json(VALID / "topic_logs_batch.json")
    record = frame["data"]["records"][0]
    frame["data"]["shadow"] = record
    assert "sensitive:logs.batch.shadow.key" in protocol.validate_frame(frame)
    assert "sensitive:logs.batch.shadow.key" in protocol.validate_logs_batch(
        frame["data"]
    )
    with pytest.raises(ValueError, match="invalid_frame:.*sensitive:"):
        protocol.encode_frame(frame)


@pytest.mark.parametrize("location", FREE_TEXT_LOCATIONS)
@pytest.mark.parametrize(
    "text",
    [
        "-----BEGIN " + "PRIVATE KEY-----\nCANARY",
        "Authorization: Bearer AbC.def-123_~",
        "Bearer abc=",
        "Authorization: Bearer abc==",
        "https://example.test/path=x",
        "ftp://user=pass@example.test/file",
        "https://example.test/#frag=x",
        json.dumps({"https://example.test/path": 1}),
        json.dumps({"Authorization: Bearer abc=": 1}),
        json.dumps({"-----BEGIN " + "PRIVATE KEY-----": 1}),
    ],
)
def test_pem_and_authorization_bearer_are_diagnostic_in_free_text(
    location: str, text: str,
) -> None:
    frame, expected_error = _free_text_frame(location, text)
    _assert_text_diagnostic(frame)
    assert protocol.decode_line(protocol.encode_frame(frame)) == frame


@pytest.mark.parametrize("key", CREDENTIAL_KEYS)
def test_structured_error_params_reject_each_credential_key(key: str) -> None:
    frame = _json(VALID / "frame_error.json")
    frame["error"]["params"][key] = "CANARY"
    expected_error = f"sensitive:error.params.{key}"
    assert expected_error in protocol.validate_error(frame)
    assert expected_error in protocol.validate_frame(frame)
    with pytest.raises(ValueError, match="invalid_frame:.*sensitive:"):
        protocol.encode_frame(frame)


def test_traffic_session_shape_is_not_exempt_inside_error_params() -> None:
    frame = _json(VALID / "frame_error.json")
    frame["error"]["params"]["session"] = {"up_bytes": 1, "down_bytes": 2}
    assert "sensitive:error.params.session" in protocol.validate_error(frame)
    assert "sensitive:error.params.session" in protocol.validate_frame(frame)
    with pytest.raises(ValueError, match="invalid_frame:.*sensitive:"):
        protocol.encode_frame(frame)


@pytest.mark.parametrize("proto", [True, 1.0, "1", 2])
def test_hello_version_requires_exact_integer(proto: object) -> None:
    frame = _json(VALID / "frame_hello.json")
    frame["proto"] = proto
    assert "enum:proto" in protocol.validate_frame(frame)
    with pytest.raises(ValueError, match="invalid_frame"):
        protocol.encode_frame(frame)


@pytest.mark.parametrize("topics", [[], ["core.info", "core.info"], ["unknown.topic"], "core.info"])
def test_subscribe_requires_nonempty_unique_p1_topics(topics: object) -> None:
    frame = _json(VALID / "frame_subscribe.json")
    frame["topics"] = topics
    assert protocol.validate_frame(frame)


def test_subscription_reply_uses_the_same_topic_restrictions() -> None:
    frame = _json(VALID / "frame_subscribed.json")
    frame["topics"] = ["core.info", "core.info"]
    assert protocol.validate_frame(frame)


def test_subscription_reply_correlates_request_without_auto_subscription() -> None:
    request = _json(VALID / "frame_subscribe.json")
    reply = _json(VALID / "frame_subscribed.json")
    assert protocol.validate_frame(request) == []
    assert protocol.validate_frame(reply) == []
    assert protocol.validate_control_reply(request, reply) == []


def test_subscription_can_return_correlated_error() -> None:
    request = _json(VALID / "frame_subscribe.json")
    reply = _json(VALID / "frame_error.json")
    reply["id"] = request["id"]
    assert protocol.validate_control_reply(request, reply) == []


@pytest.mark.parametrize("field,bad_value", [("id", 22), ("topics", ["core.info"]), ("epoch", "other-sidecar")])
def test_subscription_reply_rejects_cross_frame_mismatch(field: str, bad_value: object) -> None:
    request = _json(VALID / "frame_subscribe.json")
    reply = _json(VALID / "frame_subscribed.json")
    reply[field] = bad_value
    assert protocol.validate_frame(reply) == []
    assert protocol.validate_control_reply(request, reply)


@pytest.mark.parametrize("topic", sorted(SNAPSHOT_TOPICS))
def test_snapshot_request_accepts_each_p1_state_topic(topic: str) -> None:
    frame = _json(VALID / "frame_snapshot_req.json")
    frame["topic"] = topic
    assert protocol.validate_frame(frame) == []


@pytest.mark.parametrize("topic", ["jobs", "logs.batch", "unknown.topic"])
def test_snapshot_request_rejects_edge_or_unknown_topic(topic: str) -> None:
    frame = _json(VALID / "frame_snapshot_req.json")
    frame["topic"] = topic
    assert protocol.validate_frame(frame)


def test_snapshot_request_has_no_p1_key() -> None:
    frame = _json(VALID / "frame_snapshot_req.json")
    frame["key"] = "anything"
    assert protocol.validate_frame(frame)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda frame: frame["snapshot"].update(topic="route.published"),
        lambda frame: frame["snapshot"].update(kind="edge"),
        lambda frame: frame["snapshot"].update(epoch="old-sidecar"),
        lambda frame: frame.update(error={"code": "snapshot_required", "message_key": "errors.bridge.snapshot_required", "params": {}}),
        lambda frame: frame.pop("snapshot"),
    ],
)
def test_snapshot_response_requires_one_matching_full_snapshot(mutation) -> None:
    frame = _json(VALID / "frame_snapshot_res.json")
    mutation(frame)
    assert protocol.validate_frame(frame)


def test_replacement_snapshot_reestablishes_receiver_revision() -> None:
    initial = _json(VALID / "topic_core_info.json")
    initial["rev"] = 3
    initial["data"]["generation"] = 7
    initial["data"]["running"] = False
    cache = {(initial["epoch"], initial["topic"]): deepcopy(initial)}
    cache_key = (initial["epoch"], initial["topic"])

    # apply_patch is the generic reference baseline check; P1 does not send patch frames.
    baseline = {"rev": cache[cache_key]["rev"], "items": [{"id": "core-1", "running": False}]}
    stale_patch = {"base_rev": 2, "rev": 4, "changed": [{"id": "core-1", "running": True}], "removed": []}
    assert protocol.apply_patch(baseline, stale_patch) == (None, True)
    waiting_for_snapshot = True
    assert cache[cache_key]["data"]["generation"] == 7

    request = _json(VALID / "frame_snapshot_req.json")
    response = _json(VALID / "frame_snapshot_res.json")
    assert request["topic"] == cache_key[1]
    assert protocol.validate_frame(request) == []
    assert protocol.validate_frame(response) == []
    assert protocol.validate_control_reply(request, response) == []
    replacement = protocol.decode_line(protocol.encode_frame(response))["snapshot"]
    assert replacement["kind"] == "snapshot"
    assert replacement["rev"] > cache[cache_key]["rev"]
    assert replacement["data"] == _json(VALID / "topic_core_info.json")["data"]

    # The correlated response replaces the old cache, then normal full DTO updates resume.
    cache[cache_key] = deepcopy(replacement)
    waiting_for_snapshot = False
    assert cache[cache_key]["rev"] == 4
    assert cache[cache_key]["data"]["generation"] == 8

    def receive_full_event(event: dict) -> None:
        assert not waiting_for_snapshot
        assert protocol.validate_frame(event) == []
        assert event["epoch"] == cache_key[0]
        assert event["topic"] == cache_key[1]
        assert event["kind"] in {"snapshot", "edge"}
        assert event["rev"] > cache[cache_key]["rev"]
        cache[cache_key] = deepcopy(event)

    next_snapshot = deepcopy(replacement)
    next_snapshot["rev"] = 5
    next_snapshot["data"]["generation"] = 9
    receive_full_event(next_snapshot)
    assert cache[cache_key]["data"]["generation"] == 9

    next_edge = deepcopy(next_snapshot)
    next_edge.update(rev=6, kind="edge", seq=41)
    next_edge["data"]["running"] = False
    next_edge["data"]["cleanup_pending"] = True
    receive_full_event(next_edge)
    assert cache[cache_key]["rev"] == 6
    assert cache[cache_key]["kind"] == "edge"
    assert cache[cache_key]["data"]["running"] is False
    assert initial["data"]["generation"] == 7


@pytest.mark.parametrize("field,bad_value", [("id", 23), ("topic", "route.published"), ("epoch", "other-sidecar")])
def test_snapshot_response_rejects_cross_frame_mismatch(field: str, bad_value: object) -> None:
    request = _json(VALID / "frame_snapshot_req.json")
    response = _json(VALID / "frame_snapshot_res.json")
    if field == "topic":
        request[field] = bad_value
    else:
        response[field] = bad_value
        if field == "epoch":
            response["snapshot"]["epoch"] = bad_value
    assert protocol.validate_frame(request) == []
    assert protocol.validate_frame(response) == []
    assert protocol.validate_control_reply(request, response)


@pytest.mark.parametrize("topic", sorted(P1_TOPICS))
def test_p1_topic_without_items_baseline_rejects_wire_patch(topic: str) -> None:
    frame = _json(VALID / f"topic_{topic.replace('.', '_')}.json")
    frame.pop("seq", None)
    frame.update(kind="patch", rev=2, base_rev=1, data={"changed": [{"id": "x"}], "removed": []})
    assert protocol.validate_frame(frame)
    with pytest.raises(ValueError, match="invalid_frame"):
        protocol.encode_frame(frame)


@pytest.mark.parametrize("kind", ["snapshot", "patch", "gap"])
def test_jobs_topic_requires_edge_kind(kind: str) -> None:
    frame = _json(VALID / "topic_jobs.json")
    frame["kind"] = kind
    frame.pop("seq")
    assert protocol.validate_frame(frame)


def test_jobs_gap_frame_is_forbidden_while_logs_gap_is_valid() -> None:
    log_gap = _json(VALID / "topic_gap.json")
    log_gap["topic"] = "logs.batch"
    assert protocol.validate_frame(log_gap) == []
    assert protocol.decode_line(protocol.encode_frame(log_gap)) == log_gap

    job_gap = deepcopy(log_gap)
    job_gap["topic"] = "jobs"
    assert "not_allowed:topic.kind" in protocol.validate_frame(job_gap)
    with pytest.raises(ValueError, match="invalid_frame"):
        protocol.encode_frame(job_gap)


def test_jobs_sequence_skip_is_protocol_error_and_ends_unfinished_waiters() -> None:
    first = _json(VALID / "topic_jobs.json")
    skipped = deepcopy(first)
    skipped.update(rev=2, seq=first["seq"] + 2)
    skipped["data"]["op"] = "job-44"
    assert protocol.validate_frame(first) == []
    assert protocol.validate_frame(skipped) == []

    # Minimal shell receiver model: a jobs edge cannot explain a skipped seq.
    receiver = {
        "next_seq": first["seq"] + 1,
        "unfinished_waiters": {"job-43": None},
        "diagnostics": [],
        "rebuild_requested": False,
    }
    if skipped["seq"] != receiver["next_seq"]:
        receiver["diagnostics"].append("sequence_gap")
        receiver["rebuild_requested"] = True
        receiver["unfinished_waiters"] = {
            op: "runtime_gone" for op in receiver["unfinished_waiters"]
        }
    assert receiver["diagnostics"] == ["sequence_gap"]
    assert receiver["rebuild_requested"] is True
    assert receiver["unfinished_waiters"] == {"job-43": "runtime_gone"}


def test_jobs_edge_requires_sequence() -> None:
    frame = _json(VALID / "topic_jobs.json")
    frame.pop("seq")
    assert "missing:seq" in protocol.validate_frame(frame)


@pytest.mark.parametrize("topic", ["traffic.tick", "traffic.history"])
@pytest.mark.parametrize("kind", ["edge", "gap"])
def test_traffic_topics_only_allow_snapshot(topic: str, kind: str) -> None:
    frame = _json(VALID / f"topic_{topic.replace('.', '_')}.json")
    frame["kind"] = kind
    frame["seq"] = 2
    assert protocol.validate_frame(frame)


def test_logs_batch_disallows_snapshot() -> None:
    frame = _json(VALID / "topic_logs_batch.json")
    frame["kind"] = "snapshot"
    frame.pop("seq")
    assert protocol.validate_frame(frame)


def test_p1_event_disallows_key() -> None:
    frame = _json(VALID / "topic_core_info.json")
    frame["key"] = "core-1"
    assert protocol.validate_frame(frame)


@pytest.mark.parametrize(
    "patch",
    [
        {"base_rev": 8, "rev": 9, "changed": [{"id": "a"}, {"id": "a"}], "removed": []},
        {"base_rev": 8, "rev": 9, "changed": [], "removed": ["a", "a"]},
        {"base_rev": 8, "rev": 9, "changed": [{"id": "a"}], "removed": ["a"]},
    ],
)
def test_patch_reference_rejects_duplicate_or_conflicting_ids(patch: dict) -> None:
    baseline = {"rev": 8, "items": [{"id": "a"}]}
    assert protocol.apply_patch(baseline, patch) == (None, True)
    frame = _json(VALID / "topic_core_info.json")
    frame.update(kind="patch", rev=patch["rev"], base_rev=patch["base_rev"])
    frame["data"] = {"changed": patch["changed"], "removed": patch["removed"]}
    assert "not_allowed:topic.kind" in protocol.validate_evt(frame)
    assert "invariant:data" in protocol.validate_evt(frame)


def test_gap_fixture_round_trips_and_describes_missing_interval() -> None:
    frame = _json(VALID / "topic_gap.json")
    assert frame["data"]["dropped"] == frame["data"]["to_seq"] - frame["data"]["from_seq"] + 1
    assert protocol.validate_frame(frame) == []
    assert protocol.decode_line(protocol.encode_frame(frame)) == frame


@pytest.mark.parametrize(
    "from_seq,to_seq,dropped",
    [(10, 1, 10), (10, 12, 0), (10, 12, 2)],
)
def test_gap_interval_and_count_must_agree(from_seq: int, to_seq: int, dropped: int) -> None:
    frame = _json(VALID / "topic_gap.json")
    frame["data"].update(from_seq=from_seq, to_seq=to_seq, dropped=dropped)
    frame["seq"] = to_seq + 1
    errors = protocol.validate_frame(frame)
    expected_error = "invariant:data.gap" if dropped else "type:data.dropped"
    assert expected_error in errors


def test_gap_marker_follows_omitted_sequence() -> None:
    frame = _json(VALID / "topic_gap.json")
    frame["seq"] = frame["data"]["to_seq"]
    assert "invariant:data.gap" in protocol.validate_frame(frame)


def test_log_drop_total_cannot_be_less_than_batch_drop_count() -> None:
    frame = _json(VALID / "topic_logs_batch.json")
    frame["data"].update(dropped=10, dropped_total=9, gap=True)
    assert protocol.validate_frame(frame)


def test_log_drop_count_requires_gap_flag() -> None:
    frame = _json(VALID / "topic_logs_batch.json")
    frame["data"].update(dropped=1, dropped_total=1, gap=False)
    assert "invariant:logs.batch.gap" in protocol.validate_frame(frame)


@pytest.mark.parametrize(
    "command,result",
    [
        ("service.start", {"startup_phase": "collecting", "failure_code": None}),
        ("service.start", {"startup_phase": "core_standby", "failure_code": None}),
        ("service.start", {"startup_phase": "running", "failure_code": None}),
        ("service.stop", {"startup_phase": "stopped", "shutdown_exit_code": 0, "cleanup_pending": False}),
        ("service.stop", {"startup_phase": "stopped", "shutdown_exit_code": None, "cleanup_pending": False}),
        ("service.restart", {"applied": True, "core_running": True, "core_cleanup_pending": False, "core_generation": 8}),
        ("app.quit", {"startup_phase": "stopped", "shutdown_exit_code": 0, "cleanup_pending": False}),
    ],
)
def test_each_p1_command_has_a_truthful_done_result(command: str, result: dict) -> None:
    frame = _json(VALID / "topic_jobs.json")
    frame["data"].update(name=command, result=result)
    assert protocol.validate_frame(frame) == []
    assert protocol.decode_line(protocol.encode_frame(frame)) == frame


@pytest.mark.parametrize(
    "command,result",
    [
        ("service.start", {}),
        ("service.start", {"startup_phase": "failed", "failure_code": "timeout"}),
        ("service.stop", {}),
        ("service.stop", {"startup_phase": "stopping", "shutdown_exit_code": None, "cleanup_pending": True}),
        ("service.stop", {"startup_phase": "stopped", "shutdown_exit_code": 0, "cleanup_pending": True}),
        ("service.stop", {"startup_phase": "stopped", "shutdown_exit_code": 1, "cleanup_pending": False}),
        ("service.restart", {}),
        ("service.restart", {"applied": False, "core_running": True, "core_cleanup_pending": False, "core_generation": 8}),
        ("service.restart", {"applied": True, "core_running": False, "core_cleanup_pending": False, "core_generation": 8}),
        ("service.restart", {"applied": True, "core_running": True, "core_cleanup_pending": False}),
        ("service.restart", {"applied": True, "core_running": True, "core_cleanup_pending": False, "core_generation": True}),
        ("app.quit", {}),
        ("app.quit", {"startup_phase": "stopping", "shutdown_exit_code": None, "cleanup_pending": True}),
    ],
)
def test_done_rejects_empty_or_incomplete_job_results(command: str, result: dict) -> None:
    frame = _json(VALID / "topic_jobs.json")
    frame["data"].update(name=command, result=result)
    assert protocol.validate_frame(frame)
    with pytest.raises(ValueError, match="invalid_frame"):
        protocol.encode_frame(frame)


@pytest.mark.parametrize(
    "command,result,field,bad_value,expected_error",
    [
        ("service.start", {"startup_phase": "running", "failure_code": None}, "failure_code", 7, "type:jobs.result.failure_code"),
        ("service.stop", {"startup_phase": "stopped", "shutdown_exit_code": 0, "cleanup_pending": False}, "cleanup_pending", "false", "type:jobs.result.cleanup_pending"),
        ("service.stop", {"startup_phase": "stopped", "shutdown_exit_code": 0, "cleanup_pending": False}, "shutdown_exit_code", True, "type:jobs.result.shutdown_exit_code"),
        ("service.restart", {"applied": True, "core_running": True, "core_cleanup_pending": False, "core_generation": 8}, "applied", "true", "type:jobs.result.applied"),
        ("service.restart", {"applied": True, "core_running": True, "core_cleanup_pending": False, "core_generation": 8}, "core_running", 1, "type:jobs.result.core_running"),
        ("app.quit", {"startup_phase": "stopped", "shutdown_exit_code": 0, "cleanup_pending": False}, "startup_phase", ["stopped"], "enum:jobs.result.startup_phase"),
    ],
)
def test_job_result_fields_reject_bad_types(
    command: str, result: dict, field: str, bad_value: object, expected_error: str,
) -> None:
    frame = _json(VALID / "topic_jobs.json")
    frame["data"].update(name=command, result={**result, field: bad_value})
    assert expected_error in protocol.validate_frame(frame)


@pytest.mark.parametrize("state", ["queued", "running", "cancelled", "superseded"])
@pytest.mark.parametrize("field,value", [("result", {"startup_phase": "running"}), ("error", {"code": "timeout", "message_key": "errors.bridge.timeout", "params": {}})])
def test_job_without_done_or_failed_state_cannot_carry_result_or_error(state: str, field: str, value: object) -> None:
    frame = _json(VALID / "topic_jobs.json")
    frame["data"].update(state=state, result=None, error=None)
    frame["data"][field] = deepcopy(value)
    assert protocol.validate_frame(frame)


def test_failed_job_keeps_non_success_result_and_error() -> None:
    frame = _json(VALID / "topic_jobs.json")
    frame["data"].update(
        state="failed",
        result={"startup_phase": "failed", "failure_code": "timeout"},
        error={"code": "timeout", "message_key": "errors.bridge.timeout", "params": {}},
    )
    assert protocol.validate_frame(frame) == []
    frame["data"].pop("error")
    assert protocol.validate_frame(frame)


@pytest.mark.parametrize(
    "name,partial_fact",
    [
        ("job_stop_failed.json", ("cleanup_pending", True)),
        ("job_restart_failed.json", ("applied", False)),
    ],
)
def test_failed_job_fixtures_preserve_partial_result_and_error(
    name: str, partial_fact: tuple[str, object],
) -> None:
    frame = _json(VALID / name)
    assert frame["data"]["state"] == "failed"
    assert frame["data"]["result"][partial_fact[0]] == partial_fact[1]
    assert frame["data"]["error"]["code"]
    assert protocol.validate_frame(frame) == []
    assert protocol.decode_line(protocol.encode_frame(frame)) == frame


def test_log_gap_extensions_do_not_bypass_structured_sensitive_scan():
    frame = _json(VALID / "topic_logs_batch.json")
    frame.update(kind="gap", seq=3)
    frame["data"] = {"from_seq": 1, "to_seq": 2, "dropped": 2,
                     "records": [{"password": "CANARY", "key": ""}]}
    assert "sensitive:data.records[0].password" in protocol.validate_frame(frame)
    with pytest.raises(ValueError):
        protocol.encode_frame(frame)


def test_nested_error_params_diagnostics_are_bounded_and_content_free():
    frame = _json(VALID / "frame_error.json")
    frame["error"]["params"] = {"nested": [{"text": "password=PrivateCanary"}]}
    for _ in range(100):
        _assert_text_diagnostic(frame)
    for _ in range(100):
        protocol.encode_frame(frame)
    assert len(protocol.privacy_diagnostics) == 64
    assert "PrivateCanary" not in repr(protocol.privacy_diagnostics)
    frame["error"]["params"]["nested"].append({"token": "PrivateCanary"})
    with pytest.raises(ValueError, match="sensitive"):
        protocol.encode_frame(frame)
