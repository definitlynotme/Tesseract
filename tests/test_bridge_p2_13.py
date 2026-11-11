"""T-P2-13: source batch save (one write, one reload) and retained keys.

Temporary config directories and a stand-in runtime object only; the real
``DesktopRuntime.apply_source_batch`` method runs against it. No live profile,
no network, no registry.
"""
from __future__ import annotations

import logging
import re
import threading
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

import bridge_sources
from bridge_protocol import _schema
from bridge_redact import KnownSecrets
from bridge_runtime_access import opaque_id, source_identity
from bridge_sources import RuntimeSourcePort, SourceSectionSaver, backend_rows, source_rows
from bridge_state import PublicationState
from config_store import ConfigStore
from desktop_runtime import DesktopRuntime
from import_service import ImportService, SourceBatchError, sources_document_digest


REPO_URL = "https://github.com/old/repo"
SUB_URL = "https://example.test/sub"
SUB_URL_2 = "https://example.test/second"


def _document() -> dict:
    return {
        "migrations": {"sources_v2": True},
        "fetch_profiles": {"mirror": [{"kind": "github_api"}]},
        "github_delivery": {"hedge_seconds": 0.2},
        "github_discovery": {"enabled": False, "limit": 5},
        "github_repos": [{
            "url": REPO_URL, "fetch_profile": "mirror",
            "github_delivery": {"hedge_seconds": 0.5}, "extension": {"x": 1},
        }],
        "telegram_channels": ["@fixture_channel"],
        "subscription_urls": [SUB_URL, {"value": SUB_URL_2, "enabled": False, "note": "keep"}],
        "local_files": [],
    }


class FakeRuntime:
    """Only what DesktopRuntime.apply_source_batch and the port touch."""

    apply_source_batch = DesktopRuntime.apply_source_batch
    _reload_after = DesktopRuntime._reload_after

    def __init__(self, root: Path, *, document: dict | None = None) -> None:
        self.sources_path = root / "sources.yaml"
        self.config_path = root / "config.yaml"
        self.sources_path.write_text(
            yaml.safe_dump(document or _document(), allow_unicode=True, sort_keys=False), encoding="utf-8")
        self.config_path.write_text(
            "client:\n  egress_recovery:\n    enabled: true\n    node_judgement:\n      mode: shadow\n"
            "ui:\n  theme: light\n", encoding="utf-8")
        self.config_store = ConfigStore(local_config_path=root / "config.local.yaml",
                                        sources_path=self.sources_path, backup_dir=root / "backups")
        self.import_service = ImportService(self.config_store)
        self._config_change_lock = threading.RLock()
        self.logger = logging.getLogger("test_bridge_p2_13")
        self.reloads: list[str | None] = []
        self.reload_error: Exception | None = None
        self.writes = 0
        original = self.config_store.write_sources

        def counted(payload):
            self.writes += 1
            return original(payload)
        self.config_store.write_sources = counted

    def read_sources_config(self):
        return self.config_store.read_sources()

    def reload_config(self, *, target_tier=None):
        self.reloads.append(target_tier)
        if self.reload_error is not None:
            raise self.reload_error
        return {}


@pytest.fixture
def runtime(tmp_path):
    return FakeRuntime(tmp_path)


def _ids(runtime):
    return {(row["kind"], i): row["id"] for i, row in enumerate(source_rows(runtime.read_sources_config()))}


def _id(kind, entry):
    return source_identity(kind, entry)


def _saver(runtime, **kwargs):
    return SourceSectionSaver(RuntimeSourcePort(runtime), **kwargs)


def _items(runtime):
    return [{"id": row["id"]} for row in source_rows(runtime.read_sources_config())]


def _assert_error_dtos(outcome):
    _schema_errors = []
    _schema(outcome.section_result(), "SectionResult", "SectionResult", _schema_errors)
    for error in outcome.errors:
        _schema(error, "Error", "Error", _schema_errors)
    assert _schema_errors == []


# -- one write, one reload ---------------------------------------------------

def test_combined_batch_writes_once_and_reloads_once(runtime):
    repo_id = _id("github_repos", {"url": REPO_URL})
    tg_id = _id("telegram_channels", "@fixture_channel")
    sub_id = _id("subscription_urls", SUB_URL)
    sub2_id = _id("subscription_urls", {"value": SUB_URL_2})
    change = {"section": "sources", "values": {"items": [
        {"id": repo_id}, {"id": tg_id, "enabled": False}, {"id": sub2_id, "enabled": True},
        {"id": "new-row", "kind": "subscription_urls", "enabled": True},
    ]}, "secrets": [
        {"field": "value", "target_id": repo_id, "action": "set", "value": "next/repo"},
        {"field": "value", "target_id": "new-row", "action": "set", "value": "https://example.test/new"},
    ]}
    outcome = _saver(runtime).save(change)
    assert outcome.saved and outcome.applied and outcome.effect == "reload", outcome.errors
    assert runtime.writes == 1
    assert runtime.reloads == ["tier3"]
    stored = runtime.read_sources_config()
    assert stored["github_repos"][0]["url"] == "https://github.com/next/repo"
    assert stored["telegram_channels"] == [{"value": "@fixture_channel", "enabled": False}]
    assert stored["subscription_urls"] == [{"value": SUB_URL_2, "enabled": True, "note": "keep"},
                                           "https://example.test/new"]
    assert sub_id not in {row["id"] for row in outcome.items}
    assert outcome.id_changes["new-row"] == _id("subscription_urls", "https://example.test/new")
    _assert_error_dtos(outcome)


def test_unchanged_batch_neither_writes_nor_reloads(runtime):
    outcome = _saver(runtime).save({"section": "sources", "values": {"items": _items(runtime)}})
    assert outcome.saved and outcome.applied and outcome.effect == "none"
    assert runtime.writes == 0 and runtime.reloads == []


def test_runtime_batch_entry_counts_one_reload_for_many_ops(runtime):
    ops = [
        {"op": "upsert", "section": "subscription_urls", "value": "https://example.test/a"},
        {"op": "upsert", "section": "subscription_urls", "value": "https://example.test/b", "enabled": False},
        {"op": "delete", "section": "subscription_urls", "value": SUB_URL},
        {"op": "set_enabled", "section": "telegram_channels", "value": "@fixture_channel", "enabled": False},
        {"op": "order", "section": "subscription_urls",
         "values": ["https://example.test/b", SUB_URL_2, "https://example.test/a"]},
    ]
    receipt = runtime.apply_source_batch(ops)
    assert receipt["written"] and receipt["reloaded"] and receipt["reload_error"] is None
    assert runtime.writes == 1 and runtime.reloads == ["tier3"]
    assert [op["action"] for op in receipt["operations"]] == ["added", "added", "deleted", "updated", "updated"]
    stored = runtime.read_sources_config()
    assert [bridge_sources._entry_value(e) for e in stored["subscription_urls"]] == [
        "https://example.test/b", SUB_URL_2, "https://example.test/a"]


def test_single_source_entry_points_keep_their_own_reload(runtime):
    # Qt path unchanged: each single upsert still reloads by itself.
    DesktopRuntime.upsert_tier3_source(runtime, {"section": "subscription_urls", "value": "https://example.test/q"})
    DesktopRuntime.delete_tier3_source(runtime, {"section": "subscription_urls", "value": "https://example.test/q"})
    assert runtime.reloads == ["tier3", "tier3"]


# -- hold on any failure -----------------------------------------------------

def test_any_invalid_operation_holds_the_whole_batch(runtime):
    before = runtime.sources_path.read_bytes()
    ops = [
        {"op": "upsert", "section": "subscription_urls", "value": "https://example.test/ok"},
        {"op": "upsert", "section": "subscription_urls", "value": SUB_URL},  # duplicate
        {"op": "delete", "section": "github_repos", "value": "https://github.com/missing/repo"},
        {"op": "set_enabled", "section": "subscription_urls", "value": SUB_URL, "enabled": "no"},
        {"op": "rename"},
        {"op": "upsert", "section": "subscription_urls", "value": "ftp://example.test/x"},
    ]
    with pytest.raises(SourceBatchError) as caught:
        runtime.apply_source_batch(ops)
    assert [(e["index"], e["reason"]) for e in caught.value.errors] == [
        (1, "duplicate"), (2, "not_found"), (3, "invalid_op"), (4, "invalid_op"), (5, "invalid_value")]
    assert "example.test" not in repr(caught.value.errors)
    assert runtime.sources_path.read_bytes() == before
    assert runtime.writes == 0 and runtime.reloads == []


def test_partial_failure_through_saver_holds_with_item_errors(runtime):
    before = runtime.sources_path.read_bytes()
    sub2_id = _id("subscription_urls", {"value": SUB_URL_2})
    items = _items(runtime) + [{"id": "dup", "kind": "subscription_urls"}]
    change = {"section": "sources", "values": {"items": items}, "secrets": [
        {"field": "value", "target_id": "dup", "action": "set", "value": SUB_URL},  # duplicates a row
        {"field": "value", "target_id": sub2_id, "action": "set", "value": "https://example.test/fine"},
    ]}
    outcome = _saver(runtime).save(change)
    assert not outcome.saved and not outcome.applied and outcome.effect == "none"
    assert outcome.error["hold"] is True and outcome.error["code"] == "validation"
    assert outcome.error["field"] == "sources.secrets[0].value"
    assert outcome.error["reason_code"] == "source_duplicate"
    assert runtime.sources_path.read_bytes() == before
    assert runtime.writes == 0 and runtime.reloads == []
    _assert_error_dtos(outcome)


def test_validation_errors_hold_before_any_runtime_call(runtime):
    calls = []

    class Port(RuntimeSourcePort):
        def apply_source_batch(self, ops, *, expected_digest):
            calls.append(ops)
            raise AssertionError("must not write")
    saver = SourceSectionSaver(Port(runtime))
    outcome = saver.save({"section": "sources", "values": {"items": [{"id": "x", "kind": "nope"}]}})
    assert not outcome.saved and outcome.errors and all(e["hold"] for e in outcome.errors)
    assert calls == []
    _assert_error_dtos(outcome)


def test_concurrent_file_change_is_a_conflict_hold(runtime):
    class Port(RuntimeSourcePort):
        def apply_source_batch(self, ops, *, expected_digest):
            doc = runtime.config_store.read_sources()
            doc["telegram_channels"].append("@someone_else")
            runtime.config_store.write_sources(doc)  # out-of-band writer
            return super().apply_source_batch(ops, expected_digest=expected_digest)
    tg_id = _id("telegram_channels", "@fixture_channel")
    outcome = SourceSectionSaver(Port(runtime)).save(
        {"section": "sources", "values": {"items": [dict(row, enabled=False) if row["id"] == tg_id else row
                                                    for row in _items(runtime)]}})
    assert not outcome.saved and outcome.error["code"] == "conflict"
    assert outcome.error["reason_code"] == "source_conflict" and outcome.error["hold"]
    assert runtime.writes == 1 and runtime.reloads == []  # only the out-of-band write
    assert runtime.read_sources_config()["telegram_channels"] == ["@fixture_channel", "@someone_else"]
    _assert_error_dtos(outcome)


def test_digest_is_content_based():
    assert sources_document_digest({"a": [1], "b": 2}) == sources_document_digest({"b": 2, "a": [1]})
    assert sources_document_digest({"a": [1]}) != sources_document_digest({"a": [2]})


# -- written but not applied -------------------------------------------------

def test_reload_failure_after_write_is_saved_not_applied(runtime):
    runtime.reload_error = RuntimeError("reload failed for https://example.test/secret-token")
    tg_id = _id("telegram_channels", "@fixture_channel")
    items = [dict(row, enabled=False) if row["id"] == tg_id else row for row in _items(runtime)]
    outcome = _saver(runtime).save({"section": "sources", "values": {"items": items}})
    assert outcome.saved and not outcome.applied and outcome.written and outcome.effect == "reload"
    assert outcome.error["code"] == "io" and outcome.error["message_key"] == "errors.settings.apply_failed"
    assert outcome.error["reason_code"] == "source_reload_failed"
    assert "secret-token" not in repr(outcome)
    assert runtime.writes == 1 and runtime.reloads == ["tier3"]
    assert runtime.read_sources_config()["telegram_channels"] == [{"value": "@fixture_channel", "enabled": False}]
    _assert_error_dtos(outcome)


def test_runtime_reports_reload_error_type_only(runtime):
    runtime.reload_error = ValueError("https://example.test/secret-token")
    receipt = runtime.apply_source_batch(
        [{"op": "set_enabled", "section": "telegram_channels", "value": "@fixture_channel", "enabled": False}])
    assert receipt["written"] is True and receipt["reloaded"] is False
    assert receipt["reload_error"] == "ValueError"
    assert "secret-token" not in repr(receipt)


def test_write_failure_is_not_saved(runtime):
    def broken(payload):
        raise OSError("disk full")
    runtime.config_store.write_sources = broken
    before = runtime.sources_path.read_bytes()
    tg_id = _id("telegram_channels", "@fixture_channel")
    items = [dict(row, enabled=False) if row["id"] == tg_id else row for row in _items(runtime)]
    outcome = _saver(runtime).save({"section": "sources", "values": {"items": items}})
    assert not outcome.saved and not outcome.applied and outcome.error["code"] == "io"
    assert outcome.error["reason_code"] == "source_write_failed"
    assert runtime.reloads == [] and runtime.sources_path.read_bytes() == before
    _assert_error_dtos(outcome)


# -- additional and hidden keys ----------------------------------------------

def _dump(value) -> str:
    return yaml.safe_dump(value, allow_unicode=True, sort_keys=False)


def test_url_owner_repo_round_trip_keeps_additional_keys_and_value_key(runtime):
    repo_id = _id("github_repos", {"url": REPO_URL})
    original = deepcopy(runtime.read_sources_config()["github_repos"][0])
    config_before = runtime.config_path.read_bytes()
    first = _saver(runtime).save({"section": "sources", "values": {}, "secrets": [
        {"field": "value", "target_id": repo_id, "action": "set", "value": "next/repo"}]})
    assert first.saved and first.applied, first.errors
    entry = runtime.read_sources_config()["github_repos"][0]
    assert entry == dict(original, url="https://github.com/next/repo")
    assert list(entry) == list(original)  # value key and key order kept
    new_id = first.id_changes[repo_id]
    second = _saver(runtime).save({"section": "sources", "values": {}, "secrets": [
        {"field": "url", "target_id": new_id, "action": "set", "value": REPO_URL}]})
    assert second.saved and second.applied, second.errors
    assert runtime.read_sources_config()["github_repos"][0] == original
    assert second.id_changes[new_id] == repo_id
    assert runtime.config_path.read_bytes() == config_before  # node_judgement/ui.theme untouched
    assert runtime.writes == 2 and runtime.reloads == ["tier3", "tier3"]


def test_hidden_top_level_blocks_are_byte_identical_after_write(runtime):
    before = runtime.read_sources_config()
    text_before = runtime.sources_path.read_text(encoding="utf-8")
    tg_id = _id("telegram_channels", "@fixture_channel")
    items = [dict(row, enabled=False) if row["id"] == tg_id else row for row in _items(runtime)]
    assert _saver(runtime).save({"section": "sources", "values": {"items": items}}).saved
    after = runtime.read_sources_config()
    text_after = runtime.sources_path.read_text(encoding="utf-8")
    for key in ("migrations", "fetch_profiles", "github_delivery", "github_discovery", "github_repos"):
        assert after[key] == before[key]
        block = _dump({key: before[key]})
        assert block in text_before and block in text_after
    assert after["subscription_urls"] == before["subscription_urls"]


def test_node_judgement_and_additional_key_submissions_are_rejected(runtime):
    before = runtime.sources_path.read_bytes()
    repo_id = _id("github_repos", {"url": REPO_URL})
    for change in (
        {"section": "sources", "values": {"items": [{"id": repo_id, "node_judgement": {}}]}},
        {"section": "sources", "values": {"node_judgement": {"mode": "act"}}},
        {"section": "sources", "values": {"items": [{"id": repo_id, "fetch_profile": "other"}]}},
        {"section": "sources", "values": {"items": [{"id": repo_id, "extension": {"x": 2}}]}},
    ):
        outcome = _saver(runtime).save(change)
        assert not outcome.saved and outcome.error["hold"] is True
    assert runtime.sources_path.read_bytes() == before and runtime.writes == 0


def test_display_is_not_persisted_and_existing_display_key_is_kept(tmp_path):
    document = _document()
    document["subscription_urls"][1]["display"] = "kept label"
    runtime = FakeRuntime(tmp_path, document=document)
    sub2_id = _id("subscription_urls", {"value": SUB_URL_2})
    items = [dict(row, display="changed") for row in _items(runtime)]
    outcome = _saver(runtime).save({"section": "sources", "values": {"items": items}})
    assert outcome.saved and outcome.effect == "none" and runtime.writes == 0
    stored = runtime.read_sources_config()
    assert stored["subscription_urls"][1]["display"] == "kept label"
    assert {row["id"]: row["display"] for row in outcome.items}[sub2_id] == "subscription_urls"


# -- stable IDs ----------------------------------------------------------------

def test_ids_are_stable_across_toggle_reorder_and_unrelated_delete(runtime):
    before = {row["id"] for row in source_rows(runtime.read_sources_config())}
    tg_id = _id("telegram_channels", "@fixture_channel")
    sub_id = _id("subscription_urls", SUB_URL)
    items = list(reversed([dict(row, enabled=False) if row["id"] == tg_id else row
                           for row in _items(runtime) if row["id"] != sub_id]))
    outcome = _saver(runtime).save({"section": "sources", "values": {"items": items}})
    assert outcome.saved and outcome.applied and outcome.id_changes == {}
    after = {row["id"] for row in outcome.items}
    assert after == before - {sub_id}
    # The bare string became a mapping only to carry enabled=False: same ID.
    assert runtime.read_sources_config()["telegram_channels"] == [{"value": "@fixture_channel", "enabled": False}]
    assert tg_id in after
    assert runtime.writes == 1 and runtime.reloads == ["tier3"]


def test_persisted_id_survives_value_change(tmp_path):
    document = _document()
    document["subscription_urls"][1]["id"] = "fixture-persisted"
    runtime = FakeRuntime(tmp_path, document=document)
    persisted = opaque_id("source", "subscription_urls", "id", "fixture-persisted")
    assert persisted in {row["id"] for row in source_rows(runtime.read_sources_config())}
    outcome = _saver(runtime).save({"section": "sources", "values": {}, "secrets": [
        {"field": "value", "target_id": persisted, "action": "set", "value": "https://example.test/moved"}]})
    assert outcome.saved and outcome.applied and outcome.id_changes == {}
    assert persisted in {row["id"] for row in outcome.items}
    entry = runtime.read_sources_config()["subscription_urls"][1]
    assert entry == {"value": "https://example.test/moved", "enabled": False, "note": "keep", "id": "fixture-persisted"}


def test_backend_rows_collapse_duplicate_legacy_entries_and_refuse_ambiguous_edits(tmp_path):
    document = _document()
    document["subscription_urls"].append(SUB_URL)  # legacy duplicate, same ID
    document["subscription_urls"].append({"value": SUB_URL, "id": "other"})  # same value, other ID
    runtime = FakeRuntime(tmp_path, document=document)
    rows, origins = backend_rows(runtime.read_sources_config())
    assert len({row["id"] for row in rows}) == len(rows)
    other = opaque_id("source", "subscription_urls", "id", "other")
    assert origins[other].shared
    before = runtime.sources_path.read_bytes()
    items = [dict(row, enabled=False) if row["id"] == other else row for row in _items(runtime)]
    outcome = _saver(runtime).save({"section": "sources", "values": {"items": items}})
    assert not outcome.saved and outcome.error["reason_code"] == "source_ambiguous_identity"
    assert runtime.sources_path.read_bytes() == before
    _assert_error_dtos(outcome)


# -- secret rules --------------------------------------------------------------

@pytest.mark.parametrize("row", [
    {"owner": "next", "repo": "repo"}, {"url": "https://github.com/next/repo"},
    {"value": "https://github.com/next/repo"}, {"owner": "***", "repo": "repo"},
])
def test_owner_repo_and_values_cannot_be_ordinary_values(runtime, row):
    repo_id = _id("github_repos", {"url": REPO_URL})
    before = runtime.sources_path.read_bytes()
    outcome = _saver(runtime).save({"section": "sources", "values": {"items": [{"id": repo_id, **row}]}})
    assert not outcome.saved and outcome.error["code"] == "validation"
    assert runtime.sources_path.read_bytes() == before and runtime.writes == 0


@pytest.mark.parametrize("value", ["***", "已设置", "ftp://github.com/a/b"])
def test_masks_and_invalid_secret_values_hold(runtime, value):
    repo_id = _id("github_repos", {"url": REPO_URL})
    outcome = _saver(runtime).save({"section": "sources", "values": {}, "secrets": [
        {"field": "value", "target_id": repo_id, "action": "set", "value": value}]})
    assert not outcome.saved and outcome.error["params"]["field"] == "sources.secrets[0].value"
    assert runtime.writes == 0


def test_secret_clear_of_a_source_requires_delete(runtime):
    sub2_id = _id("subscription_urls", {"value": SUB_URL_2})
    outcome = _saver(runtime).save({"section": "sources", "values": {}, "secrets": [
        {"field": "value", "target_id": sub2_id, "action": "clear"}]})
    assert not outcome.saved and outcome.error["reason_code"] == "source_clear_requires_delete"
    assert outcome.error["field"] == "sources.secrets[0].value"
    assert runtime.writes == 0


def test_new_secret_values_are_registered_before_the_write(runtime):
    events = []

    def register(values):
        events.append(("register", dict(values), runtime.sources_path.read_bytes()))

    def refresh():
        events.append(("refresh", None, runtime.sources_path.read_bytes()))
    before = runtime.sources_path.read_bytes()
    repo_id = _id("github_repos", {"url": REPO_URL})
    outcome = _saver(runtime, register_secrets=register, refresh_secrets=refresh).save(
        {"section": "sources", "values": {}, "secrets": [
            {"field": "value", "target_id": repo_id, "action": "set", "value": "next/repo"}]})
    assert outcome.saved
    assert [event[0] for event in events] == ["register", "refresh"]
    assert events[0][2] == before  # registered while the file still held the old value
    # KnownSecrets contract: key = secret value, value = safe category label.
    assert set(events[0][1]) == {"next/repo", "https://github.com/next/repo"}
    assert all(re.fullmatch(r"[a-z][a-z0-9_]{0,31}", c) for c in events[0][1].values())
    assert set(events[0][1].values()) == {"secret"}
    assert events[1][2] != before


def test_subscription_secret_values_use_the_subscription_category(runtime):
    seen = []
    sub2_id = _id("subscription_urls", {"value": SUB_URL_2})
    outcome = _saver(runtime, register_secrets=lambda values: seen.append(dict(values))).save(
        {"section": "sources", "values": {}, "secrets": [
            {"field": "value", "target_id": sub2_id, "action": "set", "value": " https://example.test/s2 "}]})
    assert outcome.saved, outcome.errors
    assert seen == [{" https://example.test/s2 ": "subscription", "https://example.test/s2": "subscription"}]


def test_failed_secret_registration_writes_nothing(runtime):
    def register(values):
        raise RuntimeError("redactor unavailable")
    repo_id = _id("github_repos", {"url": REPO_URL})
    outcome = _saver(runtime, register_secrets=register).save({"section": "sources", "values": {}, "secrets": [
        {"field": "value", "target_id": repo_id, "action": "set", "value": "next/repo"}]})
    assert not outcome.saved and outcome.error["code"] == "internal"
    assert runtime.writes == 0 and runtime.reloads == []
    _assert_error_dtos(outcome)


def test_results_never_carry_source_values(runtime):
    repo_id = _id("github_repos", {"url": REPO_URL})
    outcome = _saver(runtime).save({"section": "sources", "values": {"items": _items(runtime) + [
        {"id": "n", "kind": "subscription_urls"}]}, "secrets": [
        {"field": "value", "target_id": repo_id, "action": "set", "value": "next/repo"},
        {"field": "value", "target_id": "n", "action": "set", "value": "https://example.test/new-secret"}]})
    assert outcome.saved
    text = repr(outcome)
    for secret in ("next/repo", "new-secret", "example.test", "fixture_channel"):
        assert secret not in text


# -- wiring --------------------------------------------------------------------

def test_register_publishes_bastion_sources_after_write(runtime):
    class Access:
        def __init__(self, rt):
            self.runtime = rt

        def known_secret_values(self):
            return {}

    class Session:
        def __init__(self, rt):
            self.runtime = rt
            self.access = Access(rt)
            self.state = PublicationState("p2-13-fixture")
            self.logs = None

    session = Session(runtime)
    saver = bridge_sources.register(session)
    assert saver.publish() is True
    tg_id = _id("telegram_channels", "@fixture_channel")
    items = [dict(row, enabled=False) if row["id"] == tg_id else row for row in _items(runtime)]
    outcome = saver.save({"section": "sources", "values": {"items": items}})
    assert outcome.saved and outcome.applied
    latest = session.state._list_latest
    (event,) = [value for slot, value in latest.items() if "bastion.sources" in repr(slot)]
    assert event["rev"] == 2
    assert {row["id"]: row["enabled"] for row in event["data"]["items"]}[tg_id] is False


def test_runtime_port_requires_the_batch_entry():
    class Bare:
        def read_sources_config(self):
            return {}
    with pytest.raises(NotImplementedError):
        RuntimeSourcePort(Bare()).apply_source_batch([], expected_digest=None)


class _Access:
    def __init__(self, rt, known=None):
        self.runtime = rt
        self.known = dict(known or {})

    def known_secret_values(self):
        return dict(self.known)


class _Logs:
    """Compiles every table with the real KnownSecrets, as the log pipeline does."""

    def __init__(self, runtime):
        self.runtime = runtime
        self.tables = []

    def refresh_known_secret_values(self, table):
        self.tables.append((KnownSecrets(table), self.runtime.sources_path.read_bytes()))


class _LoggedSession:
    def __init__(self, rt, known=None):
        self.runtime = rt
        self.access = _Access(rt, known)
        self.state = PublicationState("p2-13-logged")
        self.logs = _Logs(rt)

    def refresh_secrets(self):
        self.logs.refresh_known_secret_values(self.access.known_secret_values())


def test_register_with_real_known_secrets_saves_and_redacts_before_the_write(runtime):
    session = _LoggedSession(runtime, known={REPO_URL: "secret"})
    saver = bridge_sources.register(session)
    before = runtime.sources_path.read_bytes()
    repo_id = _id("github_repos", {"url": REPO_URL})
    outcome = saver.save({"section": "sources", "values": {}, "secrets": [
        {"field": "value", "target_id": repo_id, "action": "set", "value": "next/repo"}]})
    assert outcome.saved and outcome.applied and outcome.error is None, outcome.errors
    assert runtime.writes == 1 and runtime.reloads == ["tier3"]
    (registered, file_then), (refreshed, file_after) = session.logs.tables
    assert file_then == before and file_after != before  # registered before the write
    text = "moved to next/repo i.e. https://github.com/next/repo from " + REPO_URL
    redacted = registered.replace(text)
    assert "next/repo" not in redacted and "github.com/next" not in redacted
    assert "[redacted:secret]" in redacted
    assert REPO_URL not in redacted  # the existing table is extended, not replaced
    assert refreshed is not registered


def test_register_with_real_known_secrets_adds_a_subscription(runtime):
    session = _LoggedSession(runtime)
    outcome = bridge_sources.register(session).save({"section": "sources", "values": {"items": _items(runtime) + [
        {"id": "n", "kind": "subscription_urls"}]}, "secrets": [
        {"field": "value", "target_id": "n", "action": "set", "value": "https://example.test/new-secret"}]})
    assert outcome.saved and outcome.applied, outcome.errors
    registered = session.logs.tables[0][0]
    assert registered.replace("GET https://example.test/new-secret") == "GET [redacted:subscription]"


# -- repair round 1 (SHOULD follow-ups) ----------------------------------------

def _toggle_channel(runtime):
    tg_id = _id("telegram_channels", "@fixture_channel")
    return [dict(row, enabled=False) if row["id"] == tg_id else row for row in _items(runtime)]


def test_existing_row_kind_change_is_held(runtime):
    before = runtime.sources_path.read_bytes()
    sub_id = _id("subscription_urls", SUB_URL)
    items = [dict(row, kind="github_repos") if row["id"] == sub_id else row for row in _items(runtime)]
    outcome = _saver(runtime).save({"section": "sources", "values": {"items": items}})
    assert not outcome.saved and outcome.error["reason_code"] == "source_kind_immutable"
    index = [row["id"] for row in items].index(sub_id)
    assert outcome.error["field"] == f"sources.items[{index}].kind" and outcome.error["hold"]
    assert runtime.sources_path.read_bytes() == before and runtime.writes == 0
    _assert_error_dtos(outcome)


def test_invalid_document_elsewhere_is_a_validation_hold(tmp_path):
    document = _document()
    document["github_delivery"] = {"hedge_seconds": "legacy-text"}  # invalid, untouched block
    runtime = FakeRuntime(tmp_path, document=document)
    before = runtime.sources_path.read_bytes()
    outcome = _saver(runtime).save({"section": "sources", "values": {"items": _toggle_channel(runtime)}})
    assert not outcome.saved and outcome.error["code"] == "validation" and outcome.error["hold"]
    assert outcome.error["reason_code"] == "source_document_invalid"
    assert runtime.sources_path.read_bytes() == before and runtime.writes == 0 and runtime.reloads == []
    _assert_error_dtos(outcome)


def test_digest_accepts_mixed_key_types(tmp_path):
    mixed = {"fetch_profiles": {1: [], "b": []}, "x": [{2: "a", "c": None}]}
    assert sources_document_digest(mixed) == sources_document_digest(deepcopy(mixed))
    assert sources_document_digest(mixed) != sources_document_digest(
        {"fetch_profiles": {"1": [], "b": []}, "x": [{2: "a", "c": None}]})
    document = _document()
    document["extra_block"] = {1: "one", "b": "two"}
    runtime = FakeRuntime(tmp_path, document=document)
    outcome = _saver(runtime).save({"section": "sources", "values": {"items": _toggle_channel(runtime)}})
    assert outcome.saved and outcome.applied, outcome.errors
    assert runtime.read_sources_config()["extra_block"] == {1: "one", "b": "two"}


def test_failure_after_atomic_replace_counts_as_written(runtime, monkeypatch):
    import config_store

    def refuse(path):
        if Path(path) == runtime.sources_path:
            raise PermissionError("acl")
    monkeypatch.setattr(config_store, "restrict_to_current_user", refuse)
    outcome = _saver(runtime).save({"section": "sources", "values": {"items": _toggle_channel(runtime)}})
    assert outcome.saved and outcome.applied and outcome.written, outcome.errors
    assert runtime.reloads == ["tier3"]
    assert runtime.read_sources_config()["telegram_channels"] == [{"value": "@fixture_channel", "enabled": False}]


def test_failure_before_replace_is_still_not_saved(runtime, monkeypatch):
    import config_store
    before = runtime.sources_path.read_bytes()

    def refuse(path):
        raise PermissionError("acl")  # parent directory first: before the replace
    monkeypatch.setattr(config_store, "restrict_to_current_user", refuse)
    outcome = _saver(runtime).save({"section": "sources", "values": {"items": _toggle_channel(runtime)}})
    assert not outcome.saved and outcome.error["reason_code"] == "source_write_failed"
    assert runtime.sources_path.read_bytes() == before and runtime.reloads == []


def test_valueless_disabled_legacy_row_does_not_hold_and_keeps_its_slot(tmp_path):
    document = _document()
    legacy = {"enabled": False, "note": "legacy"}
    document["telegram_channels"] = [legacy, "@fixture_channel", "@second_channel"]
    runtime = FakeRuntime(tmp_path, document=document)
    first = _id("telegram_channels", "@fixture_channel")
    second = _id("telegram_channels", "@second_channel")
    items = _items(runtime)
    order = [row["id"] for row in items]
    a, b = order.index(first), order.index(second)
    items[a], items[b] = items[b], items[a]  # swap the two real channels
    outcome = _saver(runtime).save({"section": "sources", "values": {"items": items}})
    assert outcome.saved and outcome.applied, outcome.errors
    assert runtime.read_sources_config()["telegram_channels"] == [legacy, "@second_channel", "@fixture_channel"]


def test_valueless_subscription_row_is_a_document_hold(tmp_path):
    # validate_sources_config rejects an empty subscription URL anywhere, so
    # write_sources could never store this file: a hold, not an io failure.
    document = _document()
    document["subscription_urls"].append({"enabled": False, "note": "legacy"})
    runtime = FakeRuntime(tmp_path, document=document)
    outcome = _saver(runtime).save({"section": "sources", "values": {"items": _toggle_channel(runtime)}})
    assert not outcome.saved and outcome.error["reason_code"] == "source_document_invalid"
    assert runtime.writes == 0


def test_order_keeps_unlisted_entries_in_their_slots(tmp_path):
    document = _document()
    document["telegram_channels"] = ["@a_chan", {"enabled": False}, "@b_chan", "@a_chan"]
    runtime = FakeRuntime(tmp_path, document=document)
    result = runtime.import_service.apply_tier3_source_batch(
        [{"op": "order", "section": "telegram_channels", "values": ["@b_chan", "@a_chan"]}])
    assert result["written"]
    assert runtime.read_sources_config()["telegram_channels"] == [
        "@b_chan", {"enabled": False}, "@a_chan", "@a_chan"]


def test_swapping_two_values_in_one_save_is_a_duplicate_hold(runtime):
    sub_id = _id("subscription_urls", SUB_URL)
    sub2_id = _id("subscription_urls", {"value": SUB_URL_2})
    before = runtime.sources_path.read_bytes()
    outcome = _saver(runtime).save({"section": "sources", "values": {}, "secrets": [
        {"field": "value", "target_id": sub_id, "action": "set", "value": SUB_URL_2},
        {"field": "value", "target_id": sub2_id, "action": "set", "value": SUB_URL}]})
    assert not outcome.saved and outcome.error["reason_code"] == "source_duplicate"
    assert runtime.sources_path.read_bytes() == before and runtime.writes == 0


def test_unexpected_batch_fault_is_internal_and_logs_the_type_only(runtime, caplog):
    class Port(RuntimeSourcePort):
        def apply_source_batch(self, ops, *, expected_digest):
            raise KeyError("https://example.test/secret-token")
    with caplog.at_level(logging.WARNING, logger="bridge_sources"):
        outcome = SourceSectionSaver(Port(runtime)).save(
            {"section": "sources", "values": {"items": _toggle_channel(runtime)}})
    assert not outcome.saved and outcome.error["code"] == "internal"
    assert outcome.error["reason_code"] == "source_batch_internal_error"
    assert "KeyError" in caplog.text and "secret-token" not in caplog.text
    assert "secret-token" not in repr(outcome)
    _assert_error_dtos(outcome)
