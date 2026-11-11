"""T-P2-12 B-26(3): listener field errors and save-failure semantics.

Pure facts only: no sockets, no runtime start, no real config or network.
"""
from __future__ import annotations

import json
import logging
import time

import pytest

import bridge_protocol as protocol
from bridge_messages import (
    LISTENER_KIND_FIELDS, classify_start_failure, listener_error,
    listener_section_result, startup_diagnostic,
)
from bridge_runtime_access import RuntimeAccess, immutable_record, service_state_data
from desktop_runtime import (
    RUNTIME_LISTENER_CONFLICT_MESSAGE, RUNTIME_LISTENER_SETUP_MESSAGE, DesktopRuntime,
    RuntimeListenerPreflightError, RuntimeListenerSetupError,
)
from runtime_listener_preflight import (
    RuntimeListenerPreflightResult, RuntimeListenerPreflightState,
    RuntimeListenerProbeFailure, RuntimeListenerSpec,
)


def _schema_errors(value, name):
    errors = []
    protocol._schema(value, name, name, errors)
    return errors


def _setup_error(*specs, failed=True):
    return RuntimeListenerSetupError(RuntimeListenerPreflightResult(
        state=RuntimeListenerPreflightState.INDETERMINATE, specs=tuple(specs),
        failures=tuple(RuntimeListenerProbeFailure(spec, "bind") for spec in specs) if failed else ()))


def _spec_runtime(network_mode):
    # Same isolated shape as test_runtime_startup_stability_regressions: no
    # constructor, no sockets; _runtime_listener_preflight_specs reads config only.
    runtime = object.__new__(DesktopRuntime)
    runtime.config = {"client": {
        "network_mode": network_mode, "mihomo_mixed_port": 17890,
        "system_proxy": {"enabled": True, "host": "127.0.0.1", "port": 17890},
        "mihomo_controller": {"enabled": True, "host": "127.0.0.1", "port": 19090},
        "dns": {"enable": True, "listen": "127.0.0.1:11053"},
    }}
    runtime.network_mode = network_mode
    runtime.core_runner = None
    runtime.logger = logging.getLogger(f"p2-12-{time.monotonic_ns()}")
    runtime.logger.handlers = [logging.NullHandler()]
    runtime.logger.propagate = False
    return runtime


def test_registered_kinds_are_exactly_the_real_preflight_kinds():
    kinds = set()
    for mode in ("system_proxy", "tun", "mixed"):
        kinds |= {spec.kind for spec in _spec_runtime(mode)._runtime_listener_preflight_specs()}
    assert kinds == {"mixed", "controller", "dns"} == set(LISTENER_KIND_FIELDS)


def test_registered_fields_are_real_page_form_fields():
    from bridge_validation import FIELDS
    for field in filter(None, LISTENER_KIND_FIELDS.values()):
        section, name = field.split(".", 1)
        assert name in FIELDS[section]
    # Controller port is read-only on the page; DNS listen is not page-editable.
    assert "controller_port" not in FIELDS["network"]
    assert not {"listen", "dns_listen"} & FIELDS["dns"]


@pytest.mark.parametrize("kind,transport,field", [
    ("mixed", "tcp", "network.mihomo_mixed_port"),
    ("controller", "tcp", None),
    ("dns", "tcp", None),
    ("dns", "udp", None),
])
def test_each_kind_maps_to_its_field(kind, transport, field):
    exc = RuntimeListenerPreflightError(((kind, transport, "127.0.0.1", 12345),))
    entry = {"kind": kind, "transport": transport, "host": "127.0.0.1", "port": 12345}
    assert exc.diagnostic["params"] == {"conflicts": [entry]}
    error = listener_error(exc, written=False)
    expected = dict(code="runtime_listener_conflict", message_key="errors.start.listener_conflict",
                    params={"conflicts": [entry]}, reason_code="runtime_listener_conflict", hold=True)
    if field is not None:
        expected["field"] = field
    assert error == expected
    assert _schema_errors(error, "Error") == []


def test_multiple_conflicts_keep_order_and_focus_first_editable_field():
    conflicts = (("dns", "udp", "127.0.0.1", 1053), ("dns", "tcp", "127.0.0.1", 1053),
                 ("controller", "tcp", "::1", 9090), ("mixed", "tcp", "127.0.0.1", 7890),
                 ("mixed", "tcp", "127.0.0.1", 7890))
    exc = RuntimeListenerPreflightError(conflicts)
    entries = exc.diagnostic["params"]["conflicts"]
    assert [(e["kind"], e["transport"], e["port"]) for e in entries] == [
        ("dns", "udp", 1053), ("dns", "tcp", 1053), ("controller", "tcp", 9090), ("mixed", "tcp", 7890)]
    assert entries[2]["host"] == "::1"
    error = listener_error(exc, written=True)
    assert error["params"] == {"conflicts": entries}
    assert error["field"] == "network.mihomo_mixed_port"
    assert "hold" not in error
    # Without an editable kind no field is invented.
    only_fixed = listener_error(RuntimeListenerPreflightError(conflicts[:3]), written=True)
    assert "field" not in only_fixed and len(only_fixed["params"]["conflicts"]) == 3


def test_entry_count_is_bounded():
    exc = RuntimeListenerPreflightError(tuple(("mixed", "tcp", "127.0.0.1", 2000 + i) for i in range(40)))
    assert len(exc.diagnostic["params"]["conflicts"]) == 16
    assert len(listener_error(exc, written=False)["params"]["conflicts"]) == 16


@pytest.mark.parametrize("kind", ["source_private", "unknown", "subscription", "http"])
def test_unknown_kind_is_reported_without_a_guessed_field(kind):
    exc = RuntimeListenerPreflightError(((kind, "tcp", "127.0.0.1", 25500),))
    error = listener_error(exc, written=False)
    assert error["params"] == {"conflicts": [
        {"kind": kind, "transport": "tcp", "host": "127.0.0.1", "port": 25500}]}
    assert "field" not in error
    assert error["code"] == "runtime_listener_conflict"


@pytest.mark.parametrize("bad", [
    ("C:/private", "tcp", "127.0.0.1", 1), ("mixed", "sctp", "127.0.0.1", 7890),
    ("mixed", "tcp", "127.0.0.1", True), ("mixed", "tcp", "127.0.0.1", 0),
    ("mixed", "tcp", "127.0.0.1", 70000), ("Mixed", "tcp", "127.0.0.1", 7890), ("mixed",),
])
def test_runtime_whitelist_drops_malformed_entries(bad):
    exc = RuntimeListenerPreflightError((bad,))
    assert exc.diagnostic["params"] == {}
    assert str(exc) == RUNTIME_LISTENER_CONFLICT_MESSAGE


def test_non_ip_host_is_dropped_and_no_free_text_leaks():
    exc = RuntimeListenerPreflightError((("mixed", "tcp", "private-host.example", 7890),
                                         ("controller", "tcp", "[fe80::1%12]", 9090),
                                         ("dns", "udp", "LOCALHOST", 1053)))
    entries = exc.diagnostic["params"]["conflicts"]
    assert entries == [
        {"kind": "mixed", "transport": "tcp", "port": 7890},
        {"kind": "controller", "transport": "tcp", "host": "fe80::1", "port": 9090},
        {"kind": "dns", "transport": "udp", "host": "localhost", "port": 1053},
    ]
    wire = json.dumps(listener_error(exc, written=False))
    assert "private-host" not in wire and "%12" not in wire
    # Qt keeps reading str(exc): unchanged text, no port numbers.
    assert str(exc) == RUNTIME_LISTENER_CONFLICT_MESSAGE and "7890" not in str(exc)


@pytest.mark.parametrize("kind,field", [
    ("mixed", "network.mihomo_mixed_port"), ("controller", None), ("dns", None)])
def test_listener_setup_failure_is_not_a_conflict(kind, field):
    spec = RuntimeListenerSpec(kind, "tcp", "127.0.0.1", 12345)
    exc = _setup_error(spec)
    assert exc.diagnostic["code"] == "runtime_listener_unavailable"
    assert "conflicts" not in exc.diagnostic["params"]
    assert exc.diagnostic["params"] == {"unavailable": [
        {"kind": kind, "transport": "tcp", "host": "127.0.0.1", "port": 12345}]}
    assert str(exc) == RUNTIME_LISTENER_SETUP_MESSAGE
    error = listener_error(exc, written=False)
    assert error["code"] == "runtime_listener_unavailable"
    assert error["message_key"] == "errors.start.listener_unavailable"
    assert error["params"] == {}
    assert "conflict" not in json.dumps(error)
    assert error.get("field") == field
    assert error["hold"] is True
    assert _schema_errors(error, "Error") == []


def test_setup_failure_without_failed_probe_has_no_field():
    exc = _setup_error(RuntimeListenerSpec("mixed", "tcp", "127.0.0.1", 7890), failed=False)
    assert exc.diagnostic["params"] == {}
    error = listener_error(exc, written=True)
    assert error == dict(code="runtime_listener_unavailable",
                         message_key="errors.start.listener_unavailable",
                         params={}, reason_code="runtime_listener_unavailable")
    assert RuntimeListenerSetupError().diagnostic["params"] == {}


def _cases():
    conflict = RuntimeListenerPreflightError((("controller", "tcp", "127.0.0.1", 9090),
                                              ("mixed", "tcp", "127.0.0.1", 7890)))
    setup = _setup_error(RuntimeListenerSpec("dns", "udp", "127.0.0.1", 1053))
    bare = RuntimeListenerPreflightError((("mixed", "tcp", "127.0.0.1", 7890),))
    del bare.diagnostic
    return [
        (conflict, None, None),
        (setup, None, None),
        (bare, None, None),
        (None, startup_diagnostic(conflict), None),
        (None, startup_diagnostic(setup), None),
        (None, {"reason_code": "runtime_listener_conflict", "params": {"kind": "mixed", "port": 7890}}, None),
        (None, {"exception_type": "RuntimeListenerSetupError"}, None),
        (None, None, {"runtime": {"failure_code": "runtime_listener_conflict"}}),
        (None, None, {"runtime": {"failure_code": "runtime_listener_conflict",
                                  "failure_diagnostic": immutable_record(startup_diagnostic(conflict))}}),
    ]


@pytest.mark.parametrize("written", [False, True])
@pytest.mark.parametrize("index", range(9))
def test_classification_matches_p2_05(index, written):
    exception, diagnostic, lifecycle = _cases()[index]
    base = classify_start_failure(exception, diagnostic, lifecycle)
    error = listener_error(exception, diagnostic, lifecycle, written=written)
    assert {k: error[k] for k in ("code", "message_key", "reason_code")} == {
        k: base[k] for k in ("code", "message_key", "reason_code")}
    assert error.get("hold") is (None if written else True)
    assert _schema_errors(error, "Error") == []
    if error["code"] == "runtime_listener_unavailable":
        assert error["params"] == {}


def test_p2_05_start_failure_dto_has_no_params_field():
    exc = RuntimeListenerPreflightError((("controller", "tcp", "127.0.0.1", 9090),
                                         ("mixed", "tcp", "127.0.0.1", 7890)))
    assert classify_start_failure(exc) == dict(
        code="runtime_listener_conflict", message_key="errors.start.listener_conflict",
        reason_code="runtime_listener_conflict",
        params={"kind": "controller", "port": 9090})
    # Ruling T-P2-12 item 2: params.field is gone; a legacy producer's
    # params.field is dropped, never echoed.
    assert "field" not in classify_start_failure(diagnostic={
        "reason_code": "runtime_listener_conflict",
        "params": {"kind": "mixed", "field": "mihomo_mixed_port", "port": 7890}})["params"]
    record = startup_diagnostic(exc)
    assert classify_start_failure(diagnostic=record) == classify_start_failure(exc)


def test_recorded_diagnostic_keeps_every_conflict():
    exc = RuntimeListenerPreflightError((("controller", "tcp", "127.0.0.1", 9090),
                                         ("mixed", "tcp", "127.0.0.1", 7890)))
    direct = listener_error(exc, written=True)
    record = startup_diagnostic(exc)
    assert listener_error(diagnostic=record, written=True) == direct
    lifecycle = {"runtime": {"failure_code": "runtime_listener_conflict",
                             "failure_diagnostic": immutable_record(record)}}
    assert listener_error(lifecycle=lifecycle, written=True) == direct
    assert direct["field"] == "network.mihomo_mixed_port"


def test_legacy_record_with_only_first_kind_still_locates_field():
    error = listener_error(diagnostic={"reason_code": "runtime_listener_conflict",
                                       "params": {"kind": "mixed", "port": 7890}}, written=False)
    assert error["params"] == {"conflicts": [{"kind": "mixed", "port": 7890}]}
    assert error["field"] == "network.mihomo_mixed_port"


def test_exception_evidence_is_never_mixed_with_stale_diagnostic():
    exc = RuntimeListenerPreflightError((("controller", "tcp", "127.0.0.1", 9090),))
    stale = startup_diagnostic(RuntimeListenerPreflightError((("mixed", "tcp", "127.0.0.1", 7890),)))
    error = listener_error(exc, stale, written=False)
    assert error["params"] == {"conflicts": [
        {"kind": "controller", "transport": "tcp", "host": "127.0.0.1", "port": 9090}]}
    assert "field" not in error
    record = startup_diagnostic(exc, diagnostic=stale)
    assert [e["kind"] for e in record["listener_conflicts"]] == ["controller"]


def test_non_listener_failure_is_not_claimed():
    assert listener_error(RuntimeError("port 7890 in use"), written=False) is None
    assert listener_error(diagnostic={"reason_code": "core_missing"}, written=True) is None
    assert listener_section_result("network", "restart", RuntimeError("x"), written=True) is None


def test_save_semantics_hold_before_write_and_kept_after_write():
    exc = RuntimeListenerPreflightError((("mixed", "tcp", "127.0.0.1", 7890),))
    held = listener_section_result("network", "restart", exc, written=False)
    assert held["saved"] is False and held["applied"] is False
    assert held["error"]["hold"] is True
    written = listener_section_result("network", "restart", exc, written=True)
    # Written but not applied: the saved file is kept, no rollback marker, no hold.
    assert written["saved"] is True and written["applied"] is False
    assert "hold" not in written["error"]
    assert written["error"]["field"] == "network.mihomo_mixed_port"
    for result in (held, written):
        assert _schema_errors(result, "SectionResult") == []
    with pytest.raises(TypeError):
        listener_error(exc)  # written must be stated explicitly


# --- Fix round 1 -----------------------------------------------------------

def _bridge_lifecycle(*conflicts):
    """The lifecycle shape the bridge really sees: service_state_data output."""
    record = startup_diagnostic(RuntimeListenerPreflightError(conflicts))
    sample = {"intent": {"desired_running": True},
              "runtime": {"startup_phase": "failed", "failure_code": "runtime_listener_conflict",
                          "failure_diagnostic": immutable_record(record)},
              "core": {}, "readiness": {}, "freshness": {}}
    return sample


def test_bridge_lifecycle_facts_keep_first_kind_port_and_field():
    sample = _bridge_lifecycle(("mixed", "tcp", "127.0.0.1", 7890), ("controller", "tcp", "127.0.0.1", 9090))
    facts = service_state_data(sample)
    assert "failure_diagnostic" not in facts["runtime"]
    for kwargs in ({"lifecycle": facts}, {"lifecycle": immutable_record(facts)},
                   {"diagnostic": facts["runtime"]["failure"]}):
        error = listener_error(**kwargs, written=True)
        assert error["code"] == "runtime_listener_conflict"
        assert {"kind": "mixed", "port": 7890} in error["params"]["conflicts"]
        assert error["field"] == "network.mihomo_mixed_port"
        assert _schema_errors(error, "Error") == []


def test_runtime_access_lifecycle_status_path_keeps_kind_port_and_field():
    sample = _bridge_lifecycle(("mixed", "tcp", "127.0.0.1", 7890),)

    class _Runtime:
        def lifecycle_status(self):
            return immutable_record(sample)

    facts = RuntimeAccess(_Runtime()).lifecycle_status()
    error = listener_error(lifecycle=facts, written=True)
    assert error["params"] == {"conflicts": [{"kind": "mixed", "port": 7890}]}
    assert error["field"] == "network.mihomo_mixed_port"
    # Only the first kind/port survives; multi-conflict needs the exception.
    controller_first = service_state_data(_bridge_lifecycle(
        ("controller", "tcp", "127.0.0.1", 9090), ("mixed", "tcp", "127.0.0.1", 7890)))
    error = listener_error(lifecycle=controller_first, written=False)
    assert error["params"] == {"conflicts": [{"kind": "controller", "port": 9090}]}
    assert "field" not in error and error["hold"] is True


def test_lifecycle_failure_code_only_has_no_params():
    error = listener_error(lifecycle={"runtime": {"failure_code": "runtime_listener_conflict"}}, written=True)
    assert error["params"] == {} and "field" not in error


def test_text_prefix_exception_does_not_hide_typed_diagnostic():
    entry = {"kind": "mixed", "transport": "tcp", "host": "127.0.0.1", "port": 7890}
    diagnostic = {"code": "runtime_listener_conflict", "params": {"conflicts": [entry]}}
    error = listener_error(RuntimeError("runtime_listener_conflict: busy"), diagnostic, written=True)
    assert error["params"] == {"conflicts": [entry]}
    assert error["field"] == "network.mihomo_mixed_port"
    # Alone, a text-prefix exception still classifies, but has no endpoints.
    alone = listener_error(RuntimeError("runtime_listener_conflict: busy"), written=True)
    assert alone["code"] == "runtime_listener_conflict" and alone["params"] == {}


@pytest.mark.parametrize("written", [None, 0, 1, "no", "yes"])
def test_written_must_be_a_real_bool(written):
    exc = RuntimeListenerPreflightError((("mixed", "tcp", "127.0.0.1", 7890),))
    with pytest.raises(TypeError):
        listener_error(exc, written=written)
    with pytest.raises(TypeError):
        listener_section_result("network", "restart", exc, written=written)


def test_wire_host_is_narrowed_to_loopback_or_unspecified():
    exc = RuntimeListenerPreflightError((
        ("mixed", "tcp", "192.168.1.23", 7890), ("controller", "tcp", "2001:db8::1234", 9090),
        ("dns", "udp", "0.0.0.0", 1053), ("dns", "tcp", "::", 1053),
        ("mixed", "tcp", "::1", 7891), ("mixed", "tcp", "localhost", 7892)))
    # The local runtime diagnostic keeps the full whitelisted IP.
    assert [e.get("host") for e in exc.diagnostic["params"]["conflicts"]] == [
        "192.168.1.23", "2001:db8::1234", "0.0.0.0", "::", "::1", "localhost"]
    error = listener_error(exc, written=False)
    assert [e.get("host") for e in error["params"]["conflicts"]] == [
        None, None, "0.0.0.0", "::", "::1", "localhost"]
    assert error["params"]["conflicts"][0] == {"kind": "mixed", "transport": "tcp", "port": 7890}
    wire = json.dumps(error)
    assert "192.168" not in wire and "2001:db8" not in wire
    assert error["field"] == "network.mihomo_mixed_port"


def test_bridge_drops_entries_with_invalid_transport_like_runtime():
    exc = RuntimeListenerPreflightError((("mixed", "sctp", "127.0.0.1", 7890),
                                         ("controller", "tcp", "127.0.0.1", 9090)))
    del exc.diagnostic  # bridge filters the typed tuples on its own
    error = listener_error(exc, written=True)
    assert error["params"] == {"conflicts": [
        {"kind": "controller", "transport": "tcp", "host": "127.0.0.1", "port": 9090}]}
    assert "field" not in error


@pytest.mark.parametrize("kind,field", [
    ("mixed", "network.mihomo_mixed_port"), ("controller", None), ("dns", None)])
def test_core_bind_fault_without_failed_probe_locates_its_kind(kind, field):
    specs = (RuntimeListenerSpec("mixed", "tcp", "127.0.0.1", 7890),
             RuntimeListenerSpec("controller", "tcp", "127.0.0.1", 9090),
             RuntimeListenerSpec("dns", "tcp", "127.0.0.1", 1053),
             RuntimeListenerSpec("dns", "udp", "127.0.0.1", 1053))
    result = RuntimeListenerPreflightResult(state=RuntimeListenerPreflightState.AVAILABLE, specs=specs)
    exc = RuntimeListenerSetupError(result, fault_kind=kind)
    unavailable = exc.diagnostic["params"]["unavailable"]
    assert {e["kind"] for e in unavailable} == {kind}
    assert "conflicts" not in exc.diagnostic["params"]
    assert str(exc) == RUNTIME_LISTENER_SETUP_MESSAGE
    error = listener_error(exc, written=True)
    assert error["code"] == "runtime_listener_unavailable" and error["params"] == {}
    assert error.get("field") == field
    # Recorded through startup_diagnostic it still locates the field.
    assert listener_error(diagnostic=startup_diagnostic(exc), written=True) == error
    # A failed probe still wins over the Core fault kind.
    failed = RuntimeListenerSetupError(RuntimeListenerPreflightResult(
        state=RuntimeListenerPreflightState.INDETERMINATE, specs=specs,
        failures=(RuntimeListenerProbeFailure(specs[1], "bind"),)), fault_kind=kind)
    assert [e["kind"] for e in failed.diagnostic["params"]["unavailable"]] == ["controller"]


def test_core_bind_fault_kind_without_matching_spec_stays_empty():
    result = RuntimeListenerPreflightResult(
        state=RuntimeListenerPreflightState.AVAILABLE,
        specs=(RuntimeListenerSpec("mixed", "tcp", "127.0.0.1", 7890),))
    assert RuntimeListenerSetupError(result, fault_kind="dns").diagnostic["params"] == {}
    assert RuntimeListenerSetupError(None, fault_kind="mixed").diagnostic["params"] == {}
