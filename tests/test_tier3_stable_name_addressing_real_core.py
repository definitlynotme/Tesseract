"""Tier3 names that address the Core are the names the Core loaded (11-02).

Real bundled Mihomo 1.19.27 through the verified-writes harness (``_Lab``),
local 127.0.0.1 fixtures only.  Unlike the provider-write suites, nothing on
the naming path is stubbed: the real renderer (write_mihomo_runtime_config ->
subscription._render_clash_config -> build_full_clash_config), the real
``_tier3_proxy_name_pairs``, the real ``_reload_pruned_primary_tier3_export``
(the strict-reload stage of a Tier3-primary profile: repair plan, export
override, re-render, hot reload) and the real
``_refresh_tier3_runtime_qualification`` run against the Core that loaded the
render.  Only what would leave 127.0.0.1 is narrowed before the Core loads a
render: its DNS, geodata, listeners and rules are dropped and its
health-check URLs point at the local origin (proxy names, groups and the
provider file stay exactly as rendered), and node probe URLs are the local
origin.
"""

from __future__ import annotations

import logging
from contextlib import ExitStack

import pytest
import yaml

from clash_formatter import build_clash_proxy_list
from models import Node, NodeStatus
from tests.test_desktop_runtime_recovery import FakeTier3Manager
from tests.test_tier3_verified_provider_writes_real_core import PROVIDER, _lab


def _local_only(source, target, url):
    """Keep the rendered names and groups; drop what would leave 127.0.0.1."""

    document = yaml.safe_load(source.read_text(encoding='utf-8'))
    for key in ('dns', 'geox-url', 'geodata-mode', 'listeners', 'tun', 'rule-providers'):
        document.pop(key, None)
    document['rules'] = ['MATCH,GENERAL']
    document['bind-address'] = '127.0.0.1'
    for group in document.get('proxy-groups', []):
        if 'url' in group:
            group['url'] = url
    for provider in (document.get('proxy-providers') or {}).values():
        if isinstance(provider.get('health-check'), dict):
            provider['health-check']['url'] = url
    target.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding='utf-8')


class _ControllerRunner:
    """hot_reload = PUT /configs of the main config the runtime has just written.

    The harness Core runs without SAFE_PATHS, so the (narrowed) copy goes to
    its home directory, next to the provider file the runtime wrote.
    """

    def __init__(self, lab, runtime):
        self.lab, self.runtime, self.reloads = lab, runtime, 0
        self.loaded_path = lab.provider_path.parent / 'loaded-runtime.yaml'

    def hot_reload(self, **_kwargs):
        _local_only(self.runtime._runtime_config_path_for_client(), self.loaded_path, self.lab.url)
        self.lab.client.reload_config(str(self.loaded_path))
        self.reloads += 1
        return {'reloaded': True}


class _Tier3PrimaryManager(FakeTier3Manager):
    """A Tier3-only pool, with what the real renderer reads from a Manager."""

    def __init__(self, runtime, nodes):
        super().__init__()
        self.min_alive_nodes = 3
        self.config = runtime.config
        self.client_config = runtime.config['client']
        self.testing_config = runtime.config.get('testing', {})
        self.logger = logging.getLogger('tests.tier3-stable-name-addressing')
        self.runtime_nodes['tier3'] = list(nodes)
        self.runtime_nodes['exit'] = []
        self.tier3_pool_revision = 2

    def active_exit_proxy_name(self):
        return ''


def _provider_members(lab):
    provider = yaml.safe_load(lab.provider_path.read_text(encoding='utf-8'))
    return [str(proxy['name']) for proxy in provider['proxies'] if proxy['name'] != 'REJECT']


@pytest.mark.parametrize('verified', [True, False], ids=['stable-names', 'positional-names'])
def test_strict_reload_stage_probes_and_qualifies_the_names_the_core_loaded(tmp_path, verified):
    """F19: with the switch on the Core loads base·digest names; the repair
    plan and the qualification must probe exactly those (they probed the
    positional names, every GET /proxies/{name}/delay was a 404 and every
    strict reload of a Tier3-primary profile was rolled back)."""

    with ExitStack() as stack:
        lab = _lab(stack, tmp_path, names=['n1', 'n2', 'n3'], loaded=['n1', 'n2', 'n3'])
        runtime = lab.runtime
        del runtime._runtime_proxy_name_map  # the harness's naming stub: under test here
        runtime.config['tiers'] = {
            'tier1': {'nodes': []},
            'tier2': {'nodes': [], 'subscription_urls': []},
            'tier3': {'enabled': True, 'verified_provider_writes': verified},
        }
        runtime.config['client'].update({
            'mihomo_mixed_port': lab.mixed_port,
            'mihomo_controller': {'port': int(lab.client.base_url.rsplit(':', 1)[1]),
                                  'secret': lab.client.secret},
        })
        runtime.config['testing'] = {'probe_url': lab.url}
        # Probe only the local origin (the configured defaults append public URLs).
        runtime._probe_urls_for_node = lambda _node: [lab.url]
        nodes = [
            Node(name=name, protocol='http', uri=f'http://127.0.0.1:{lab.nodes[fixture].server_address[1]}',
                 address='127.0.0.1', port=lab.nodes[fixture].server_address[1], tier='tier3',
                 status=NodeStatus.ALIVE)
            for name, fixture in (('alpha', 'n1'), ('beta', 'n2'), ('gamma', 'n3'))
        ]
        runtime.manager = _Tier3PrimaryManager(runtime, nodes)
        runner = _ControllerRunner(lab, runtime)

        # The Core loads the real render (what the strict reload's first hot
        # reload does before _reload_pruned_primary_tier3_export runs).
        runtime.write_mihomo_runtime_config()
        runner.hot_reload()
        loaded = _provider_members(lab)
        assert sorted(lab.members()) == sorted(loaded)
        assert all(('·' in name) is verified for name in loaded), loaded

        result = runtime._reload_pruned_primary_tier3_export(runner, {'enabled': True})
        assert result.get('reloaded') is True, result
        plan = result['_tier3_core_repair_plan']
        assert plan.successful_count == 3, plan.failure_summary
        assert runner.reloads == 2
        assert sorted(lab.members()) == sorted(loaded)  # same definitions: same names

        pairs = runtime._tier3_proxy_name_pairs()
        assert sorted(name for name, _node in pairs) == sorted(loaded)
        assert sorted(node.name for _name, node in pairs) == ['alpha', 'beta', 'gamma']

        qualification = runtime._refresh_tier3_runtime_qualification()
        assert qualification.qualified_count == 3, qualification
        assert qualification.exported_count == 3


def test_stable_names_round_trip_through_every_name_endpoint(tmp_path):
    """F17: a stable name is addressable on /providers/proxies/{p}/{name} and
    /proxies/{name} (Mihomo decodes the segment twice whenever its escaping
    equals Go's default, so a literal %XX in a name used to 404)."""

    bases = ['50%41 x', 'a%2Fb', 'a%25b', '100%', 'x/y', 'a b?c#d', 'a+b&c=d', '日本 東京',
             'quote"\'<>|^`{}[]\\']
    nodes = [Node(name=base, protocol='http', address=f'{index}.example.invalid', port=8080,
                  tier='tier3', status=NodeStatus.ALIVE)
             for index, base in enumerate(bases)]
    names = [str(proxy['name']) for proxy in build_clash_proxy_list(nodes, stable_names=True)]
    assert len(names) == len(bases)
    with ExitStack() as stack:
        lab = _lab(stack, tmp_path, names=names, loaded=names)
        assert sorted(lab.members()) == sorted(names)
        for name in names:
            lab.client.provider_member_healthcheck(PROVIDER, name, url=lab.url, timeout_ms=3000,
                                                   expected_status=204)
            assert lab.client.proxy_delay(name, url=lab.url, timeout_ms=3000,
                                          expected_status=204)['delay'] > 0
        fresh = lab.verdicts()
        assert set(fresh) >= set(names) and all(fresh[name][0] for name in names), fresh
    assert not any('%' in name for name in names), names
