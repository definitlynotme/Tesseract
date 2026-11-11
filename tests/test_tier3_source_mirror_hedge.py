"""Explicit delivery configuration and non-authoritative mirror failures."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
import time
from typing import Any

import pytest

import tier3_sources
import collector
from config_validation import validate_sources_config
from tier3_collection_settings import tier3_collection_settings
from tier3_sources import (
    GitHubRepositorySource,
    RemoteTextSource,
    build_source,
    github_delivery_paths,
    github_raw_mirror_urls,
    normalize_github_delivery,
)


class _HTTPStatusError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"http {status}")
        self.status = status


class _FakeContent:
    def __init__(self, body: bytes, *, body_error: BaseException | None = None) -> None:
        self._body = body
        self._body_error = body_error
        self.read = False

    async def iter_chunked(self, _size: int):
        self.read = True
        if self._body_error is not None:
            raise self._body_error
        yield self._body


class _FakeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        content_type: str = "text/plain",
        body_error: BaseException | None = None,
    ) -> None:
        self._body = body
        self.status = status
        self.headers: dict[str, str] = {"Content-Type": content_type}
        self.charset = "utf-8"
        self.content = _FakeContent(body, body_error=body_error)
        self.released = False

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise _HTTPStatusError(self.status)

    def release(self) -> None:
        self.released = True


class _Answer:
    """A scripted answer: response after ``delay``, optionally HTML or a failing body."""

    def __init__(self, body: bytes, *, delay: float = 0.0, content_type: str = "text/plain",
                 body_error: BaseException | None = None) -> None:
        self.body = body
        self.delay = delay
        self.content_type = content_type
        self.body_error = body_error


def test_cancellation_during_loser_cleanup_releases_selected_response(monkeypatch):
    async def run():
        source = RemoteTextSource('https://origin.example/sub')
        winner = _FakeResponse(b'valid')
        loser_cleaning = asyncio.Event()

        async def open_url(url, *_args, **_kwargs):
            if url == source.value:
                return winner
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                loser_cleaning.set()
                await asyncio.Event().wait()

        monkeypatch.setattr(source, '_open_url', open_url)
        paths = [tier3_sources.GitHubDeliveryPath('canonical', source.value),
                 tier3_sources.GitHubDeliveryPath('backup', 'https://backup.example/sub')]
        task = asyncio.create_task(source._first_road_response(
            paths, None, outcomes={}, failed=set(), headers={}, timeout_seconds=1,
            retries=0, proxy_url=None,
        ))
        await asyncio.wait_for(loser_cleaning.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert winner.released

    asyncio.run(run())


class _FakeSession:
    """Per-URL scripted responses: bytes, an exception, a hang, or (delay, bytes)."""

    def __init__(self, script: dict[str, Any]) -> None:
        self.script = script
        self.requests: list[tuple[str, str | None, dict[str, str]]] = []
        self.responses: dict[str, _FakeRequestContext] = {}
        self.request_options: list[dict[str, Any]] = []

    def get(self, url: str, *, headers=None, timeout=None, proxy=None, **kwargs):
        self.requests.append((url, proxy, dict(headers or {})))
        self.request_options.append(kwargs)
        behaviour = self.script.get(url, ConnectionError("unscripted road"))
        context = _FakeRequestContext(behaviour)
        self.responses.setdefault(url, context)
        return context

    def urls(self) -> list[str]:
        return [url for url, _proxy, _headers in self.requests]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _FakeRequestContext:
    def __init__(self, behaviour: Any) -> None:
        self.behaviour = behaviour
        self.response: _FakeResponse | None = None

    async def _open(self) -> _FakeResponse:
        behaviour = self.behaviour
        if isinstance(behaviour, float):
            await asyncio.sleep(behaviour)
            raise ConnectionError("road stalled")
        if isinstance(behaviour, BaseException):
            raise behaviour
        if isinstance(behaviour, _Answer):
            await asyncio.sleep(behaviour.delay)
            self.response = _FakeResponse(
                behaviour.body,
                content_type=behaviour.content_type,
                body_error=behaviour.body_error,
            )
            return self.response
        if isinstance(behaviour, tuple):
            delay, body = behaviour
            await asyncio.sleep(delay)
            if isinstance(body, BaseException):
                raise body
            self.response = _FakeResponse(body)
            return self.response
        self.response = _FakeResponse(behaviour)
        return self.response

    def __await__(self):
        return self._open().__await__()

    async def __aenter__(self):
        return await self._open()

    async def __aexit__(self, *_exc):
        return False


RAW = "https://raw.githubusercontent.com/owner/repo/main/Config/vless.txt"
API = "https://api.github.com/repos/owner/repo/contents/Config/vless.txt?ref=main"
PROXY_A = f"https://gh-proxy.com/{RAW}"
PROXY_B = f"https://ghfast.top/{RAW}"
MIRROR_A = "https://testingcf.jsdelivr.net/gh/owner/repo@main/Config/vless.txt"
MIRROR_B = "https://cdn.jsdelivr.net/gh/owner/repo@main/Config/vless.txt"
PROXY = "http://user:pass@127.0.0.1:1"


def _source(url: str = RAW, *, hedge: float = 5.0, **delivery: Any) -> RemoteTextSource:
    return RemoteTextSource(url, github_delivery={
        "api": True,
        "proxies": ["https://gh-proxy.com/", "https://ghfast.top/"],
        "mirrors": ["testingcf.jsdelivr.net", "cdn.jsdelivr.net"],
        "hedge_seconds": hedge, **delivery,
    })


def _paths(url: str):
    return github_delivery_paths(
        url, proxy_prefixes=["https://gh-proxy.com/", "https://ghfast.top/"],
        mirror_hosts=["testingcf.jsdelivr.net", "cdn.jsdelivr.net"], use_api=True,
    )


def _fetch(source: RemoteTextSource, session: _FakeSession, *, retries: int = 0) -> str | None:
    return asyncio.run(
        source._fetch(
            session,
            timeout_seconds=5,
            retries=retries,
            user_agent="test",
            proxy_url=PROXY,
            max_bytes=1024 * 1024,
        )
    )


def test_every_road_is_derived_from_the_url_form_not_from_a_named_source() -> None:
    paths = _paths(RAW)
    assert [(p.label, p.url, p.start_delay_seconds, p.authoritative) for p in paths] == [
        ("canonical", RAW, 0.0, True),
        ("github_api", API, 0.0, False),
        ("proxy:gh-proxy.com", PROXY_A, 0.0, False),
        ("proxy:ghfast.top", PROXY_B, 0.75, False),
        ("mirror:testingcf.jsdelivr.net", MIRROR_A, 0.75, False),
        ("mirror:cdn.jsdelivr.net", MIRROR_B, 0.75, False),
    ]
    assert dict(paths[1].headers) == {"Accept": "application/vnd.github.raw"}

    repository_form = _paths(
        "https://github.com/someone/lists/raw/dev/sub/nodes.txt"
    )
    assert repository_form[0].url == "https://github.com/someone/lists/raw/dev/sub/nodes.txt"
    assert repository_form[1].url == (
        "https://api.github.com/repos/someone/lists/contents/sub/nodes.txt?ref=dev"
    )
    assert repository_form[2].url == (
        "https://gh-proxy.com/https://raw.githubusercontent.com/someone/lists/dev/sub/nodes.txt"
    )
    assert _paths(
        "https://raw.githubusercontent.com/o/r/refs/heads/main/sub.txt"
    )[4].url == "https://testingcf.jsdelivr.net/gh/o/r@main/sub.txt"
    for other in (
        "https://example.com/owner/repo/main/sub.txt",
        "https://raw.githubusercontent.com/owner/repo",
        "https://github.com/owner/repo",
        "https://t.me/s/channel",
    ):
        assert github_delivery_paths(other) == []
        assert github_raw_mirror_urls(other) == []


def test_a_blackholed_canonical_host_is_beaten_by_a_fresh_road_at_once() -> None:
    source = _source()
    session = _FakeSession({RAW: 5.0, API: b"vless://fresh"})

    started = time.monotonic()
    assert _fetch(source, session) == "vless://fresh"
    assert time.monotonic() - started < 1.0
    assert source._last_delivery_path == "github_api"
    assert set(session.urls()) == {RAW, API, PROXY_A}
    assert {proxy for _url, proxy, _headers in session.requests} == {PROXY}
    api_headers = next(h for url, _p, h in session.requests if url == API)
    assert api_headers["Accept"] == "application/vnd.github.raw"


def test_hedged_roads_start_at_once_when_every_started_road_failed() -> None:
    source = _source(hedge=5.0)
    session = _FakeSession({
        RAW: ConnectionError("dial timeout"),
        API: _HTTPStatusError(403),
        PROXY_A: ConnectionError("proxy down"),
        PROXY_B: b"vless://second-proxy",
    })

    started = time.monotonic()
    assert _fetch(source, session) == "vless://second-proxy"
    assert time.monotonic() - started < 1.0
    assert source._last_delivery_path == "proxy:ghfast.top"


def test_stale_mirrors_join_after_the_head_start_while_fresh_roads_hang() -> None:
    source = _source(hedge=0.05)
    session = _FakeSession({RAW: 5.0, API: 5.0, PROXY_A: 5.0, PROXY_B: 5.0, MIRROR_A: b"vless://cached"})

    started = time.monotonic()
    assert _fetch(source, session) == "vless://cached"
    assert time.monotonic() - started < 1.0
    assert source._last_delivery_path == "mirror:testingcf.jsdelivr.net"


def test_github_saying_the_file_is_missing_ends_the_race() -> None:
    source = _source(hedge=5.0)
    session = _FakeSession({RAW: _HTTPStatusError(404), API: 5.0, PROXY_A: 5.0, MIRROR_A: b"vless://stale"})

    started = time.monotonic()
    with pytest.raises(_HTTPStatusError) as raised:
        _fetch(source, session)
    assert raised.value.status == 404
    assert time.monotonic() - started < 1.0
    assert MIRROR_A not in session.urls()


def test_an_exhausted_api_quota_is_not_a_verdict_on_the_file() -> None:
    source = _source(hedge=5.0)
    session = _FakeSession({RAW: 5.0, API: _HTTPStatusError(403), PROXY_A: (0.05, b"vless://proxy")})

    assert _fetch(source, session) == "vless://proxy"


def test_one_proxy_missing_the_file_is_not_a_verdict() -> None:
    source = _source(hedge=0.0)
    session = _FakeSession({
        RAW: 5.0,
        API: 5.0,
        PROXY_A: _HTTPStatusError(404),
        PROXY_B: 5.0,
        MIRROR_A: (0.05, b"vless://mirror"),
    })

    assert _fetch(source, session) == "vless://mirror"


def test_two_mirror_404s_cannot_cancel_a_successful_official_request() -> None:

    source = _source(hedge=0.0)
    session = _FakeSession({
        RAW: (0.05, b"vless://official"),
        API: _HTTPStatusError(403),
        PROXY_A: _HTTPStatusError(404),
        PROXY_B: (0.02, _HTTPStatusError(404)),
        MIRROR_A: 5.0,
        MIRROR_B: 5.0,
    })

    started = time.monotonic()
    assert _fetch(source, session) == "vless://official"
    assert time.monotonic() - started < 1.0


def test_all_roads_failing_raises_the_canonical_error() -> None:
    source = _source(hedge=0.0)
    canonical_error = ConnectionError("canonical down")
    session = _FakeSession({RAW: canonical_error})

    with pytest.raises(ConnectionError) as raised:
        _fetch(source, session)
    assert raised.value is canonical_error
    assert set(session.urls()) == {RAW, API, PROXY_A, PROXY_B, MIRROR_A, MIRROR_B}


def test_canonical_winning_cancels_the_other_roads() -> None:
    source = _source(hedge=0.02)
    session = _FakeSession({RAW: (0.08, b"vless://canonical"), API: 5.0, PROXY_A: 5.0, PROXY_B: 5.0, MIRROR_A: 5.0})

    started = time.monotonic()
    assert _fetch(source, session) == "vless://canonical"
    assert time.monotonic() - started < 1.0
    assert source._last_delivery_path == "canonical"


def test_only_the_canonical_road_keeps_the_retry_policy() -> None:
    source = _source(hedge=5.0)
    attempts: list[str] = []

    class CountingSession(_FakeSession):
        def get(self, url: str, **kwargs):
            attempts.append(url)
            return super().get(url, **kwargs)

    session = CountingSession({RAW: ConnectionError("down"), API: ConnectionError("down"), PROXY_A: 5.0})
    source.github_delivery = normalize_github_delivery({"api": True, "hedge_seconds": 5.0, "proxies": [], "mirrors": []})

    with pytest.raises(ConnectionError):
        _fetch(source, session, retries=1)
    assert attempts.count(RAW) == 2
    assert attempts.count(API) == 1


def test_mirror_fallback_false_keeps_the_single_canonical_request() -> None:
    source = build_source({"type": "subscription_url", "value": RAW, "mirror_fallback": False})
    assert isinstance(source, RemoteTextSource)
    assert source.delivery_paths() == []
    assert source.mirror_urls() == []
    session = _FakeSession({RAW: ConnectionError("dial timeout"), API: b"vless://api"})

    with pytest.raises(ConnectionError):
        _fetch(source, session)
    assert session.urls() == [RAW]


def test_non_github_sources_keep_the_single_request_path(monkeypatch) -> None:
    calls: list[str] = []
    source = RemoteTextSource("https://mirror.example/sub.txt")

    async def fake_race(*_args, **_kwargs):  # pragma: no cover - must not run
        calls.append("race")
        return "unexpected"

    monkeypatch.setattr(source, "_fetch_delivery_race", fake_race)
    session = _FakeSession({"https://mirror.example/sub.txt": b"ss://plain"})

    assert _fetch(source, session) == "ss://plain"
    assert calls == []


def test_configured_roads_are_bound_to_the_source_and_inherited_by_its_units() -> None:
    settings = tier3_collection_settings(
        {},
        {
            "github_delivery": {
                "proxies": ["https://relay.example/"],
                "mirrors": [],
                "api": False,
                "hedge_seconds": 0.3,
            }
        },
        default_github_candidate_paths=["Config/vless.txt"],
    )
    source = build_source(
        {"type": "github_repo", "value": "https://github.com/owner/repo",
         "github_delivery": settings["github_delivery"]},
        candidate_paths=["Config/vless.txt"],
        github_delivery=settings["github_delivery"],
    )
    assert isinstance(source, GitHubRepositorySource)
    unit = source.discover()[0]
    assert [p.label for p in unit.delivery_paths()] == ["canonical", "proxy:relay.example"]
    assert unit.mirror_urls() == [f"https://relay.example/{RAW}"]

    disabled = GitHubRepositorySource(
        "https://github.com/owner/repo",
        candidate_paths=["Config/vless.txt"],
        mirror_fallback=False,
    )
    assert disabled.discover()[0].mirror_urls() == []
    assert normalize_github_delivery(None)["proxies"] == ()
    assert RemoteTextSource(RAW).mirror_urls() == []


def test_repository_units_report_which_road_delivered_them() -> None:
    source = GitHubRepositorySource(
        "https://github.com/owner/repo",
        candidate_paths=["Config/vless.txt"],
        github_delivery={"api": True, "hedge_seconds": 5.0},
    )
    session = _FakeSession({RAW: 5.0, API: b"vless://fresh"})

    async def collect():
        return [
            unit
            async for unit in source.fetch_units(
                session,
                timeout_seconds=5,
                retries=0,
                user_agent="test",
                proxy_url=PROXY,
                unit_budget=1,
                unit_deadline_seconds=3.0,
            )
        ]

    units = asyncio.run(collect())
    assert [(u.ok, u.delivery_path) for u in units] == [(True, "github_api")]


def test_sources_validation_rejects_malformed_delivery_settings() -> None:
    validate_sources_config({"github_delivery": {"proxies": ["https://relay.example/"], "mirrors": []}})
    for bad in (
        {"github_delivery": ["https://relay.example/"]},
        {"github_delivery": {"proxies": "https://relay.example/"}},
        {"github_delivery": {"proxies": ["relay.example"]}},
        {"github_delivery": {"hedge_seconds": "soon"}},
    ):
        with pytest.raises(ValueError):
            validate_sources_config(bad)


def test_the_first_road_to_answer_downloads_alone() -> None:
    """Racing whole downloads splits the link; only the first answer is read."""

    source = _source(hedge=0.0)
    session = _FakeSession({
        RAW: 5.0,
        API: _Answer(b"vless://api", delay=0.01),
        PROXY_A: _Answer(b"vless://proxy", delay=0.05),
        PROXY_B: _Answer(b"vless://proxy-b", delay=0.05),
        MIRROR_A: _Answer(b"vless://stale", delay=0.05),
    })

    assert _fetch(source, session) == "vless://api"
    api = session.responses[API].response
    assert api is not None and api.content.read and api.released
    for url in (PROXY_A, PROXY_B, MIRROR_A):
        loser = session.responses[url].response
        assert loser is None or (not loser.content.read and loser.released)


def test_an_html_page_from_a_proxy_is_not_an_answer() -> None:
    source = _source(hedge=5.0)
    session = _FakeSession({
        RAW: 5.0,
        API: _HTTPStatusError(403),
        PROXY_A: _Answer(b"<html>rate limited</html>", content_type="text/html; charset=utf-8"),
        PROXY_B: _Answer(b"vless://second", delay=0.02),
    })

    assert _fetch(source, session) == "vless://second"
    assert source._last_road_outcomes["proxy:gh-proxy.com"] == "ValueError"


def test_a_winner_whose_body_fails_hands_over_to_the_remaining_roads() -> None:
    source = _source(hedge=0.0)
    session = _FakeSession({
        RAW: 5.0,
        API: _Answer(b"", delay=0.01, body_error=ConnectionResetError("reset mid-body")),
        PROXY_A: _Answer(b"vless://proxy", delay=0.05),
    })

    assert _fetch(source, session) == "vless://proxy"
    assert source._last_road_outcomes["github_api"] == "body:ConnectionResetError"
    assert source._last_delivery_path == "proxy:gh-proxy.com"


def test_invalid_body_cannot_beat_official_content_and_every_payload_parses_once(monkeypatch):
    valid = b"vless://12345678-1234-1234-1234-123456789012@127.0.0.1:443?security=tls#fixture"
    session = _FakeSession({RAW: (0.04, valid), PROXY_A: b"upstream temporarily unavailable"})
    monkeypatch.setattr(collector.aiohttp, "ClientSession", lambda **_kwargs: session)
    parse_calls = []
    real_parse = collector._parse_content_blob_bounded

    def parse(content, **kwargs):
        parse_calls.append(content)
        return real_parse(content, **kwargs)

    monkeypatch.setattr(collector, "_parse_content_blob_bounded", parse)
    observed = []
    nodes, report = asyncio.run(collector.collect_nodes_from_sources_with_report(
        [{"type": "subscription_url", "value": RAW,
          "github_delivery": {"proxies": ["https://gh-proxy.com/"], "api": False}}], "tier3",
        logging.getLogger("delivery-test"),
        settings={"github_delivery": {"proxies": ["https://gh-proxy.com/"], "api": False},
                  "source_unit_deadline_seconds": 1.0},
        source_observer=lambda nodes, _report: observed.extend(nodes),
    ))
    assert len(nodes) == len(observed) == 1
    assert len(parse_calls) == 2
    assert len(set(parse_calls)) == 2
    assert report.fetched_sources == 1
    assert "canonical" in str(report.sources[0])


def test_parse_time_does_not_consume_unit_transport_deadline():
    source = _source(hedge=0.0, api=False, mirrors=[], proxies=["https://gh-proxy.com/"])
    session = _FakeSession({RAW: (0.01, b"valid"), PROXY_A: b"invalid"})
    parsed = []

    async def validate(unit):
        parsed.append(unit.content)
        await asyncio.sleep(0.08)
        return unit.content == "valid"

    async def run():
        return [unit async for unit in source.fetch_units(
            session, timeout_seconds=1, retries=0, user_agent="test", proxy_url=PROXY,
            unit_deadline_seconds=0.05, delivery_slack_seconds=0.0,
            unit_content_validator=validate,
        )]
    units = asyncio.run(run())
    assert units[0].ok and units[0].content == "valid"
    assert parsed == ["invalid", "valid"]
    assert units[0].transport_elapsed_ms < 50


def test_actual_roads_share_one_broker_until_body_release():
    broker = collector._Tier3SourceUnitTransportBroker(2)
    active = peak = 0

    @asynccontextmanager
    async def acquire(root, route):
        nonlocal active, peak
        async with broker.lease(root, route):
            active += 1
            peak = max(peak, active)
            try:
                yield
            finally:
                active -= 1

    async def run():
        async def root(index):
            url = RAW.replace("owner", f"root-{index}")
            source = _source(url, hedge=0.0)
            session = _FakeSession({url: (0.01, b"valid")})
            return [unit async for unit in source.fetch_units(
                session, timeout_seconds=1, retries=0, user_agent="test", proxy_url=PROXY,
                unit_transport_acquire=acquire,
            )]
        return await asyncio.gather(*(root(index) for index in range(5)))
    results = asyncio.run(run())
    assert all(result[0].ok for result in results)
    assert peak == 2 and active == broker._active == 0


def test_disabled_yaml_endpoints_do_not_start_and_order_is_user_owned():
    source = _source(api=False, mirrors=[], proxies=[
        {"url": "https://disabled.example/", "enabled": False, "delay_seconds": 0},
        {"url": "https://second.example/", "enabled": True, "delay_seconds": 0.2},
        {"url": "https://first.example/", "enabled": True, "delay_seconds": 0.1},
    ])
    assert [(path.label, path.start_delay_seconds) for path in source.delivery_paths()] == [
        ("canonical", 0), ("proxy:second.example", 0.2), ("proxy:first.example", 0.1),
    ]


@pytest.mark.parametrize('headers', [
    {'Authorization': 'Bearer fixture-secret'}, {'Cookie': 'session=fixture-secret'},
    {'X-Custom-Key': 'fixture-secret'},
])
def test_authenticated_fetch_never_rewrites_or_follows_redirects(headers):
    source = _source()
    source.headers = headers
    session = _FakeSession({RAW: b'valid'})
    assert _fetch(source, session) == 'valid'
    assert session.urls() == [RAW]
    assert headers.items() <= session.requests[0][2].items()
    assert session.request_options == [{'allow_redirects': False}]


def test_cancel_releases_all_mirror_transport_leases():
    broker = collector._Tier3SourceUnitTransportBroker(2)
    session = _FakeSession({path.url: 10.0 for path in _source().delivery_paths()})
    async def run():
        async def fetch():
            return [unit async for unit in _source().fetch_units(
                session, timeout_seconds=10, retries=0, user_agent='test', proxy_url=PROXY,
                unit_transport_acquire=broker.lease,
            )]
        task = asyncio.create_task(fetch())
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert broker._active == 0
        assert not any(not future.done() for queue in broker._waiters.values() for future in queue)
    asyncio.run(run())
