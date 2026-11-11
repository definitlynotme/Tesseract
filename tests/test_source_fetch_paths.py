from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

import pytest
import yaml

import collector
from config_validation import validate_sources_config
from runtime_source_transport import RuntimeSourceFetchEndpoint, RuntimeSourceRouteKind, RuntimeSourceListenerCredentials
from tier3_sources import build_source, source_entries_from_sources_config, configured_delivery_paths, infer_source_type, SourceDispatchDeferred
from tests.test_tier3_source_mirror_hedge import _FakeSession, RAW, API, _fetch, _HTTPStatusError


def test_profile_is_opt_in_and_empty_source_override_is_authoritative():
    config = {'fetch_profiles': {'public': [{'kind': 'github_api'}]},
              'subscription_urls': [RAW, {'url': RAW + '.other', 'fetch_profile': 'public'},
                                    {'url': RAW + '.empty', 'fetch_profile': 'public', 'fetch_paths': []}]}
    entries = source_entries_from_sources_config(config)
    assert [len(build_source(entry).mirror_urls()) for entry in entries] == [0, 1, 0]
    assert all(entry['value'] for entry in entries)
    with pytest.raises(ValueError, match='undefined'):
        source_entries_from_sources_config({'subscription_urls': [{'url': RAW, 'fetch_profile': 'deleted'}]})


def test_non_github_source_has_explicit_generic_backup_not_a_guessed_rewrite():
    url, backup = 'https://owned.example/sub', 'https://replica.example/sub'
    source = build_source({'value': url, 'fetch_paths': [
        {'kind': 'github_api'}, {'kind': 'github_proxy', 'url': 'https://ignored.example/'},
        {'kind': 'url', 'url': backup, 'delay_seconds': 0},
    ]})
    assert [path.url for path in source.delivery_paths()] == [url, backup]
    session = _FakeSession({url: ConnectionError('unavailable'), backup: b'valid'})
    assert _fetch(source, session) == 'valid'
    assert set(session.urls()) == {url, backup}


@pytest.mark.parametrize('form', ['raw/main', 'blob/main', 'raw/refs/heads/main', 'blob/refs/tags/main'])
def test_github_file_forms_remain_file_sources(form):
    url = f'https://github.com/owner/project/{form}/sub.txt'
    paths = configured_delivery_paths(url, [{'kind': 'github_api'}])
    assert infer_source_type(url) == 'subscription_url'
    assert paths[1].url == 'https://api.github.com/repos/owner/project/contents/sub.txt?ref=main'


def test_same_host_alternatives_have_distinct_failure_identity():
    paths = configured_delivery_paths(RAW, [
        {'kind': 'github_proxy', 'url': 'https://replica.example/one/'},
        {'kind': 'github_proxy', 'url': 'https://replica.example/two/'},
    ])
    assert len({path.label for path in paths}) == len(paths) == 3


def test_derived_api_missing_cannot_override_the_configured_original():
    source = build_source({'value': RAW, 'fetch_paths': [{'kind': 'github_api'}]})
    session = _FakeSession({RAW: (0.03, b'valid original'), API: _HTTPStatusError(404)})
    assert _fetch(source, session) == 'valid original'


@pytest.mark.parametrize('repository', [False, True])
def test_transport_budget_wait_without_start_is_deferred(repository):
    source = build_source({'type': 'github_repo' if repository else 'subscription_url',
                           'value': 'https://github.com/owner/repo' if repository else RAW,
                           'candidate_paths': ['Config/vless.txt']})
    session = _FakeSession({RAW: b'not expected'})

    @asynccontextmanager
    async def no_transport_slot(_root, _route):
        await asyncio.Event().wait()
        yield

    async def run():
        with pytest.raises(SourceDispatchDeferred):
            return [unit async for unit in source.fetch_units(
                session, timeout_seconds=1, retries=0, user_agent='fixture', proxy_url=None,
                unit_deadline_seconds=0.03, delivery_slack_seconds=0,
                unit_transport_acquire=no_transport_slot,
            )]

    asyncio.run(run())
    assert session.requests == []


def test_collector_disables_implicit_response_cookie_sharing(monkeypatch):
    session = _FakeSession({RAW: b'not a subscription'})
    options = {}

    def client(**kwargs):
        options.update(kwargs)
        return session

    monkeypatch.setattr(collector.aiohttp, 'ClientSession', client)
    asyncio.run(collector.collect_nodes_from_sources_with_report(
        [{'type': 'subscription_url', 'value': RAW}], 'tier3', logging.getLogger('cookie-test'),
        settings={'request_timeout': 1, 'retries': 0},
    ))
    assert isinstance(options.get('cookie_jar'), collector.aiohttp.DummyCookieJar)


def test_repository_explicit_empty_units_do_not_restore_defaults():
    source = build_source({'type': 'github_repo', 'value': 'https://github.com/a/b',
                           'candidate_paths': []}, candidate_paths=['default.txt'])
    assert source.discover() == []


def test_transport_start_observer_runs_at_request_boundary():
    events = []

    class Session(_FakeSession):
        def get(self, *args, **kwargs):
            events.append('request')
            return super().get(*args, **kwargs)

    source = build_source({'type': 'github_repo', 'value': 'https://github.com/owner/repo',
                           'candidate_paths': ['Config/vless.txt']})
    session = Session({RAW: b'valid body'})

    async def run():
        return [unit async for unit in source.fetch_units(
            session, timeout_seconds=1, retries=0, user_agent='fixture', proxy_url=None,
            unit_transport_started_observer=lambda *_args: events.append('transport'),
        )]

    units = asyncio.run(run())
    assert units[0].ok and units[0].transport_started
    assert events == ['transport', 'request']


def test_repository_generic_backup_requires_explicit_unit_binding():
    path = {'kind': 'url', 'url': 'https://replica.example/sub.txt'}
    config = {'github_repos': [{'url': 'https://github.com/owner/project', 'fetch_paths': [path]}]}
    with pytest.raises(ValueError, match='unit'):
        validate_sources_config(config)
    path['unit'] = 'sub.txt'
    validate_sources_config(config)


def test_example_profiles_are_explicit_and_survive_one_road_failing():
    """Tier3 is the last-resort guarantee, so the shipped roads must be durable.

    The owner's policy (2026-09-20): for Tier3 expansion reachability outranks
    privacy, but nothing may be hardcoded, special-cased or bound to a single
    candidate.  So every road still comes from editable config bound to a
    source, and the template must not depend on one host: github_api alone is
    rate limited to 60 requests/hour per IP and then answers 403, which on a
    censored network leaves no road at all.  This test previously required
    every non-api road to be disabled; it now requires the template to keep
    working when any single enabled road fails.
    """

    from pathlib import Path
    config = yaml.safe_load(Path('sources.example.yaml').read_text(encoding='utf-8'))
    validate_sources_config(config)
    entries = source_entries_from_sources_config(config)
    # 2 repos, 1 Telegram channel, 2 original lists, 5 lists added in
    # tier3_source_portfolio_defaults_v2.
    assert len(entries) == 10
    for paths in config['fetch_profiles'].values():
        enabled = [path for path in paths if path['enabled']]
        assert len(enabled) >= 2, 'one road is a single point of failure'
        assert any(path['kind'] == 'github_api' for path in enabled), (
            'the authoritative road must stay enabled and be tried first'
        )
        for path in enabled:
            if path['kind'] == 'github_api':
                assert path['delay_seconds'] == 0.0
            else:
                # A third party is only reached after GitHub itself is late.
                assert path['delay_seconds'] > 0.0
                assert path['url'].startswith('https://')
    # Roads are config, never a Python fallback: a source that declares none
    # keeps its original address only.
    for entry in entries:
        bare = dict(entry)
        bare['fetch_paths'] = []
        source = build_source(bare)
        for candidate in source.discover() or [source]:
            assert not any('proxy:' in p.label or 'mirror:' in p.label
                           for p in getattr(candidate, 'delivery_paths', lambda: [])())


def test_generation_is_rechecked_after_transport_broker_wait(monkeypatch):
    session = _FakeSession({RAW: b'not expected'})
    monkeypatch.setattr(collector.aiohttp, 'ClientSession', lambda **_kw: session)
    generation = [1]
    real_lease = collector._Tier3SourceUnitTransportBroker.lease

    @asynccontextmanager
    async def held_lease(broker, root, route):
        async with real_lease(broker, root, route):
            generation[0] = 2
            yield

    monkeypatch.setattr(collector._Tier3SourceUnitTransportBroker, 'lease', held_lease)
    endpoint = RuntimeSourceFetchEndpoint(
        RuntimeSourceRouteKind.DIRECT, 'source-direct', '127.0.0.1', 19997,
        1, 1, True, 0, 0, RuntimeSourceListenerCredentials('fixture', 'secret'),
    )
    checked = []

    def validate(phase, authority, lease):
        checked.append((phase, authority))
        return lease.config_generation == generation[0]

    nodes, report = asyncio.run(collector.collect_nodes_from_sources_with_report(
        [{'type': 'subscription_url', 'value': RAW}], 'tier3', logging.getLogger('lease-test'),
        settings={'request_timeout': 1, 'retries': 0, 'source_supply_phase': 'core',
                  'runtime_source_fetch_endpoints_by_authority': {f'subscription_url:{RAW}': [endpoint]},
                  'runtime_source_fetch_endpoint_lease_validator': validate},
    ))
    assert not nodes and not session.requests
    assert len(checked) >= 2 and all(row == ('core', f'subscription_url:{RAW}') for row in checked)
    payload = report.to_dict()
    assert payload['source_fetch_pending'] == payload['source_retry_due_count'] == 1
    assert payload['source_fetch_failed'] == payload['runtime_proxy_retry_count'] == 0
