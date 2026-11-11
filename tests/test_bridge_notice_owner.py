"""Desktop-notice owner after the 提醒 redesign (10-30): policy notices, the
user's switches, update offers, the settings preview and delivery receipts.
Replaces the P2-22 merge / delivery-store tests (.build/提醒模块重设计_20261030.md §10)."""

from __future__ import annotations

import logging

import pytest

import bridge_protocol as protocol
from alert_policy import Facts
from bridge_alerts import AlertTester
from bridge_alerts_desktop import DesktopAlertOwner, register
from bridge_jobs import JobFailure, Pending


class Port:
    def __init__(self, alerts=None, language="zh_CN"):
        self.alerts = alerts if alerts is not None else {}
        self.language = language
        self.logger = logging.getLogger("test.notice.owner")
        self.token = object()

    def effective_alerts_config(self):
        return self.alerts

    def alert_language(self):
        return self.language

    def notice_logger(self):
        return self.logger

    def config_token(self):
        return self.token

    def config_revision_known(self):
        return True


class Publication:
    def __init__(self):
        self.edges = []

    def record_state(self, topic, dto):
        self.edges.append((topic, dto))

    def record_edge(self, topic, dto):
        self.edges.append((topic, dto))
        return len(self.edges)

    def topic(self, name):
        return [dto for topic, dto in self.edges if topic == name]


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class UpdateOwner:
    current_version = "0.1.0"

    def __init__(self):
        self.candidates = []
        self.delivered = []
        self.not_delivered = []

    def take_notice_candidate(self):
        return self.candidates.pop(0) if self.candidates else None

    def record_notice_delivered(self, version, *, delivery_id):
        self.delivered.append((version, delivery_id))

    def notice_not_delivered(self, delivery_id):
        self.not_delivered.append(delivery_id)


def build(alerts=None, *, shell=True, update=None):
    port, pub, clock, queued = Port(alerts), Publication(), Clock(), []
    ids = iter(range(1, 100_000))
    owner = DesktopAlertOwner(port, pub, submit_normal=lambda work: queued.append(work) or True,
                              shell_subscribed=lambda: shell, update_owner=update, clock=clock,
                              id_factory=lambda: f"{next(ids):032x}")
    return owner, port, pub, clock, queued


def run(queued):
    while queued:
        queued.pop(0)()


def ready(owner, queued):
    owner.poll()   # schedules the config read
    run(queued)    # ... which runs on the ordinary worker


def feed(owner, clock, start, end, facts):
    for t in range(start, end):
        clock.now = float(t)
        owner.observe(facts)


def running(carrier="vanguard", **extra):
    return Facts(run="running", generation=1, ready=True, carrier=carrier, **extra)


def outage(owner, clock):
    """Ready on Vanguard, then 30 s without a carrier: one outage opens at t=30."""
    feed(owner, clock, 0, 10, running())
    feed(owner, clock, 10, 40, running(None))


def test_policy_notice_reaches_the_shell_and_validates():
    owner, _port, pub, clock, queued = build()
    ready(owner, queued)
    outage(owner, clock)
    assert owner.poll() == 1
    dto = pub.topic("alerts.desktop")[-1]
    assert protocol.validate_topic_payload("alerts.desktop", dto) == []
    assert dto["severity"] == "error" and dto["force"] is False and dto["target"] == "overview"
    assert dto["title"] == "连接已中断" and dto["event_ids"] == ["notice.outage"]
    notice = dto["notice"]
    assert (notice["kind"], notice["state"], notice["group"]) == ("outage", "open", "connection")
    assert notice["sound"] is True and notice["sticky"] is True and notice["action"] == "view"
    assert notice["title_id"] == "notice.outage.title" and "reason_id" not in notice
    # The recovery follows the issued open, in place (same incident).
    feed(owner, clock, 40, 50, running("sentinel"))
    assert owner.poll() == 1
    resolved = pub.topic("alerts.desktop")[-1]["notice"]
    assert (resolved["state"], resolved["incident"]) == ("resolved", notice["incident"])
    assert resolved["sound"] is False and "action" not in resolved


def test_switches_filter_groups_and_sound():
    owner, _port, pub, clock, queued = build({"notify": {"events": ["tier_fallback"], "sound": False}})
    ready(owner, queued)
    outage(owner, clock)                       # connection group: switched off
    feed(owner, clock, 40, 50, running("vanguard"))
    assert owner.poll() == 0 and pub.topic("alerts.desktop") == []
    feed(owner, clock, 50, 90, running("bastion"))
    assert owner.poll() == 1
    notice = pub.topic("alerts.desktop")[-1]["notice"]
    assert (notice["kind"], notice["sound"]) == ("fallback", False)


def test_master_switch_off_shows_nothing_and_follow_ups_never_leak():
    owner, _port, pub, clock, queued = build({"notify": {"enabled": False}})
    ready(owner, queued)
    outage(owner, clock)
    feed(owner, clock, 40, 50, running("vanguard"))   # its recovery
    assert owner.poll() == 0 and pub.topic("alerts.desktop") == []
    assert owner.stats["suppressed"] == 2


def test_every_notice_is_logged_even_without_a_shell(caplog):
    owner, _port, pub, clock, queued = build(shell=False)
    with caplog.at_level(logging.INFO, logger="test.notice.owner"):
        outage(owner, clock)
    assert owner.poll() == 0 and pub.topic("alerts.desktop") == []
    records = [r for r in caplog.records if getattr(r, "event_key", "") == "alert.notice.error"]
    assert len(records) == 1
    assert records[0].alert_type == "CRITICAL" and records[0].category == "alert"
    assert records[0].event_params["title"] == "连接已中断"


def test_update_notice_follows_its_switch_and_is_remembered_only_when_shown():
    update = UpdateOwner()
    update.candidates.append({"version": "0.2.0", "delivery_id": "update-1"})
    owner, port, pub, clock, queued = build({"notify": {"events": ["connection"]}}, update=update)
    ready(owner, queued)
    assert owner.poll() == 0 and update.candidates   # the offer stays with the update owner
    port.alerts = {"notify": {"events": ["update"]}}
    port.token = object()
    clock.now = 5.0
    ready(owner, queued)
    assert owner.poll() == 1
    dto = pub.topic("alerts.desktop")[-1]
    assert dto["version"] == "0.2.0" and dto["target"] == "update" and dto["delivery_id"] == "update-1"
    assert dto["notice"]["params"] == {"latest": "0.2.0", "current": "0.1.0"}
    assert protocol.validate_topic_payload("alerts.desktop", dto) == []
    answer = owner.desktop_delivered_handler(
        {"delivery_id": "update-1", "outcome": "delivered", "surface": "windows"}, None)
    assert isinstance(answer, Pending)
    run(queued)
    assert update.delivered == [("0.2.0", "update-1")]


def test_duplicate_update_receipt_during_the_write_waits_for_its_outcome():
    update = UpdateOwner()
    update.candidates.append({"version": "0.2.0", "delivery_id": "update-1"})
    owner, _port, _pub, _clock, queued = build(update=update)
    ready(owner, queued)
    owner.poll()
    args = {"delivery_id": "update-1", "outcome": "delivered", "surface": "windows"}
    first = owner.desktop_delivered_handler(dict(args), None)
    duplicate = owner.desktop_delivered_handler(dict(args), None)
    answers = {}
    for name, pending in (("first", first), ("duplicate", duplicate)):
        pending._bind(lambda outcome, name=name: answers.__setitem__(name, outcome) or True)
    assert answers == {}
    run(queued)
    assert answers["first"] == ("result", {"recorded": True, "duplicate": False})
    assert answers["duplicate"] == ("result", {"recorded": True, "duplicate": True})
    assert owner.desktop_delivered_handler(dict(args), None) == {"recorded": True, "duplicate": True}


def test_policy_notice_receipts_are_idempotent_and_never_persist():
    owner, _port, pub, clock, queued = build()
    ready(owner, queued)
    outage(owner, clock)
    owner.poll()
    delivery_id = pub.topic("alerts.desktop")[-1]["delivery_id"]
    args = {"delivery_id": delivery_id, "outcome": "delivered", "surface": "in_app"}
    assert owner.desktop_delivered(dict(args)) == {"recorded": False, "duplicate": False}
    assert owner.desktop_delivered(dict(args)) == {"recorded": False, "duplicate": True}
    with pytest.raises(JobFailure):
        owner.desktop_delivered({"delivery_id": "desktop-never", "outcome": "delivered", "surface": "windows"})


def test_preview_is_forced_bypasses_the_switches_and_waits_for_the_receipt():
    owner, _port, pub, _clock, queued = build({"notify": {"enabled": False}})
    ready(owner, queued)
    tester = AlertTester(desktop=owner, deferred=True)
    pending = tester.execute({}, None)
    assert isinstance(pending, Pending)
    dto = pub.topic("alerts.desktop")[-1]
    assert dto["force"] is True and dto["delivery_id"].startswith("desktop-test-")
    assert dto["notice"]["kind"] == "preview" and dto["notice"]["group"] == "preview"
    assert protocol.validate_topic_payload("alerts.desktop", dto) == []
    answers = []
    pending._bind(lambda outcome: answers.append(outcome) or True)
    with pytest.raises(JobFailure):
        owner.force_desktop_test()   # one preview at a time
    owner.desktop_delivered({"delivery_id": dto["delivery_id"], "outcome": "delivered", "surface": "windows"})
    assert answers == [("result", {"ok": True, "error": None, "delivery_id": dto["delivery_id"]})]


def test_preview_without_a_shell_is_dropped():
    owner, _port, pub, _clock, queued = build(shell=False)
    delivery_id = owner.force_desktop_test()
    assert pub.topic("alerts.desktop") == []
    assert owner.wait_desktop_receipt(delivery_id, 0) == {"outcome": "dropped", "surface": "none",
                                                          "recorded": False}


def test_close_expires_pending_receipts():
    owner, _port, pub, clock, queued = build()
    ready(owner, queued)
    outage(owner, clock)
    owner.poll()
    delivery_id = pub.topic("alerts.desktop")[-1]["delivery_id"]
    seen = []
    assert owner.watch_desktop_receipt(delivery_id, seen.append)
    owner.close()
    assert seen == [None] and owner.poll() == 0 and owner.observe(running(None)) == 0
    assert owner.desktop_delivered({"delivery_id": delivery_id, "outcome": "delivered",
                                    "surface": "windows"}) == {"recorded": False, "duplicate": False}


def test_register_wires_only_the_receipt_command():
    registered = []

    class Session:
        runtime = object()
        state = Publication()
        subscribed_topics = {"alerts.desktop"}
        executor = type("Executor", (), {"submit_normal": staticmethod(lambda work: True)})()

        def register_command(self, name, handler):
            registered.append(name)

        def register_query(self, name, handler):  # pragma: no cover - must not be called
            raise AssertionError(name)

    owner = register(Session(), port=Port())
    assert registered == ["alerts.desktopDelivered"] and isinstance(owner, DesktopAlertOwner)
