import json
from pathlib import Path

import pytest

import bridge_protocol as protocol
from bridge_redact import KnownSecrets, redact_record, redact_text

CORPUS = json.loads((Path(__file__).parent / "fixtures/redaction_corpus/synthetic.json").read_bytes())
REGRESSIONS = json.loads((Path(__file__).parent / "fixtures/redaction_corpus/astra_regressions.json").read_bytes())
CORPUS += json.loads((Path(__file__).parent / "fixtures/redaction_corpus/round4_decision.json").read_bytes())
CORPUS += REGRESSIONS + json.loads((Path(__file__).parent / "fixtures/redaction_corpus/round3_credentials.json").read_bytes())


@pytest.mark.parametrize("sample", CORPUS, ids=lambda s: s["kind"])
def test_independent_corpus(sample):
    assert redact_text(sample["input"], known_secrets=KnownSecrets(sample.get("known_secret_values", {}))) == sample["expected"]


@pytest.mark.parametrize("sample", CORPUS, ids=lambda s: s["kind"])
def test_producer_output_passes_protocol_scan(sample):
    text = redact_text(sample["input"], known_secrets=KnownSecrets(sample.get("known_secret_values", {})))
    record = {"seq": 1, "ts_ms": 1, "rx_ms": 1, "source": "TESSERACT", "level": "info",
              "category": "other", "key": "", "title": text, "detail": text, "raw": text}
    frame = {"t": "evt", "epoch": "synthetic", "produced_at_ms": 1, "sent_at_ms": 1,
             "topic": "logs.batch", "rev": 1, "seq": 1, "kind": "edge",
             "data": {"from_seq": 1, "records": [record], "dropped": 0,
                      "dropped_total": 0, "gap": False}}
    protocol.privacy_diagnostics.clear()
    assert protocol.validate_frame(frame) == []
    assert not protocol.privacy_diagnostics  # A producer omission still fails corpus acceptance.


def test_corpus_count_and_structured_params():
    assert len(CORPUS) >= 119
    safe = redact_record({"title": "password=synthetic", "key": "dial_failed",
                          "params": {"token": "synthetic", "nested": {"text": "https://example.invalid/x"}},
                          "path": r"C:\Users\Alice\config.yaml"})
    assert safe == {"title": "[REDACTED]", "key": "dial_failed",
                    "params": {"nested": {"text": "[REDACTED]"}}, "path": "[USER_PATH]"}


def test_unserializable_is_rejected_and_unicode_cleaned():
    with pytest.raises(TypeError):
        redact_record({"message": object()})
    with pytest.raises(ValueError):
        redact_record({"message": "safe", "params": {"x": float("nan")}})
    assert redact_record({"message": "bad \udfff"}) == {"message": "bad \ufffd"}


@pytest.mark.parametrize("sample", REGRESSIONS, ids=lambda s: s["kind"])
def test_sensitive_paths_and_json_keys_leave_no_identifiers(sample):
    output = redact_text(sample["input"], known_secrets=KnownSecrets(sample.get("known_secret_values", {})))
    for identifier in sample["absent"]:
        assert identifier not in output


def test_structured_sensitive_keys_removed_recursively():
    record = {"message": "safe", "metadata": {
        "https://example.invalid/sub?token=synthetic": "private-value",
        "nested": [{r"C:\Users\alice\config.yaml": "private-value", "safe": "kept"},
                   {"socks5://synthetic:password@192.0.2.1:1080": "private-value"}]}}
    assert redact_record(record) == {"message": "safe", "metadata": {"nested": [{"safe": "kept"}, {}]}}


@pytest.mark.parametrize("name", ["password", "metrics[token]", "token_", "metrics.token-",
                                   "metrics token", "subscription.url", "api_key", "alert_sendkey"])
@pytest.mark.parametrize("form", ["{name}=SYNTHETIC", '"{name}":"SYNTHETIC"',
                                  'msg="{name}=SYNTHETIC"', '"{name}":'])
def test_assignment_variants_do_not_leak_or_hit_scanner(name, form):
    text = redact_text(form.format(name=name))
    assert "SYNTHETIC" not in text
    row = {"seq": 1, "ts_ms": 1, "rx_ms": 1, "source": "TESSERACT", "level": "info",
           "category": "other", "key": "", "title": text, "detail": text, "raw": text}
    assert protocol.validate_logs_batch({"from_seq": 1, "records": [row], "dropped": 0,
                                         "dropped_total": 0, "gap": False}) == []


def test_plain_mihomo_line_skips_json_and_multilayer_decoding(monkeypatch):
    import bridge_redact
    def forbidden(*args, **kwargs):
        raise AssertionError("plain line entered slow decoding")
    monkeypatch.setattr(bridge_redact, "_decode", forbidden)
    monkeypatch.setattr(bridge_redact.json, "loads", forbidden)
    line = "[TCP] 192.0.2.1:53000 --> example.invalid:443 match Match using Vanguard"
    assert redact_text(line) == line


@pytest.mark.parametrize("record", [
    {"message": "safe", "metadata": [0] * 2049},
    {"message": "safe", "metadata": {str(i): "x" * 100 for i in range(800)}},
    {"message": "x" * 16385},
])
def test_record_work_is_bounded_before_any_serialization(record, monkeypatch):
    import bridge_redact
    def forbidden(*args, **kwargs):
        raise AssertionError("unbounded input reached serializer")
    monkeypatch.setattr(bridge_redact.json, "dumps", forbidden)
    with pytest.raises(bridge_redact.RedactionBudgetExceeded):
        redact_record(record)


def test_fast_preflight_preserves_json_array_projection_and_nested_secret_names():
    assert redact_text(" [ 1, true, null, [2] ] ") == "[1,true,null,[2]]"
    assert redact_text('{"nested":{"event_key":"private-value","message":"safe"}}') == (
        '{"nested":{"message":"safe"}}')


def test_registry_exact_case_length_overlap_and_owned_copy():
    values = {"abcdef": "token", "abcdefgh": "password", "a.b*()": "secret", "short": "secret"}
    registry = KnownSecrets(values)
    values.clear()
    assert redact_text("abcdefgh abcdef a.b*() ABCDEF short", known_secrets=registry) == (
        "[redacted:password] [redacted:token] [redacted:secret] ABCDEF [redacted:secret]")
    assert redact_text("runtime", known_secrets=KnownSecrets({"runtime": "secret"})) == "[redacted:secret]"
    assert redact_text("Opaque%53yntheticValue", known_secrets=KnownSecrets({"OpaqueSyntheticValue": "token"})) == "[redacted:token]"
    assert redact_record({"metadata": {"abcdef": "removed", "safe": "abcdef"}}, known_secrets=registry) == {
        "metadata": {"safe": "[redacted:token]"}}


def test_registry_1000_values_single_record_latency():
    import statistics
    import time
    values = {f"OpaqueSynthetic{i:04d}Value": "token" for i in range(1000)}
    registry = KnownSecrets(values)
    rows = ["ordinary host example.invalid:443", "OpaqueSynthetic0999Value", "OpaqueSynthetic0000Value"]
    timings = []
    for i in range(1200):
        row = {"ts_ms": 1, "rx_ms": 1, "source": "MIHOMO", "level": "info",
               "category": "other", "key": "", "title": rows[i % 3], "detail": "", "raw": rows[i % 3]}
        started = time.perf_counter_ns()
        safe = redact_record(row, known_secrets=registry)
        timings.append(time.perf_counter_ns() - started)
        if i % 3:
            assert safe["raw"] == "[redacted:token]"
    median = statistics.median(timings) / 1000
    p95 = sorted(timings)[int((len(timings) - 1) * .95)] / 1000
    print(f"registry_values=1000 record_median_us={median:.3f} record_p95_us={p95:.3f}")
    assert median < 1000 and p95 < 5000


def test_normalization_is_single_pass_and_residuals_are_explicit():
    import bridge_redact
    assert bridge_redact._decode(r"%2553\u005cu0053") == r"%53\u0053"
    registry = KnownSecrets({"SyntheticSecretValue": "secret"})
    assert redact_text(r"\x53yntheticSecretValue", known_secrets=registry) == r"\x53yntheticSecretValue"
    assert redact_text("U3ludGhldGljU2VjcmV0VmFsdWU=", known_secrets=registry) == "U3ludGhldGljU2VjcmV0VmFsdWU="
    assert redact_text('{"clientSecret":"OpaqueUnknownValue"}') == '{"clientSecret":"OpaqueUnknownValue"}'


def test_jwt_header_requires_alg_and_payload_has_no_eyj_requirement():
    import base64
    header = base64.urlsafe_b64encode(b'{ "alg": "none" }').decode().rstrip("=")
    assert redact_text(header + ".e30.") == "[REDACTED]"
    assert redact_text("e30.e30.") == "e30.e30."
    assert redact_text("version abc.def.ghi") == "version abc.def.ghi"


def test_short_registered_values_are_covered_in_numeric_json_and_text():
    registry = KnownSecrets({"12345": "password", "p1234": "token", "": "secret"})
    assert redact_text('value p1234', known_secrets=registry) == 'value [redacted:token]'
    assert redact_text('{"value":12345}', known_secrets=registry) == '{"value":"[redacted:password]"}'
    assert redact_text('ordinary', known_secrets=KnownSecrets({"": "secret"})) == 'ordinary'
