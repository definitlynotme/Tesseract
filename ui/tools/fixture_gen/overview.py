"""Generate the overview_*.json scenario fixtures of T-P4-05a (ui/test/fixtures/scenarios).

usage: .venv/Scripts/python.exe -B ui/tools/fixture_gen/overview.py [ui [name ...]]
(no argument: the ui directory this script sits in)
Every scenario starts from one running base (ASCII data only) and changes the frames its state
needs. Output: indent=2 JSON + LF, like the other scenario files.

Kept in evidence/T-P4-05a since fix round 1 (review 05a: it lived only in a session scratchpad);
moved to ui/tools/fixture_gen/ by T-P4-12 (usage and state: ui/README.md).
Runtime shapes to keep (review 05a M1): gateway.current.effective is non-null only while the
route's managed tier is gateway (desktop_runtime bridge_gateway_status), so a running fixture
with a fallback carrier has effective=None; Gateway as the exit is overview_gateway_carrier.
"""
import copy
import json
import sys
from pathlib import Path

UI = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[2]
SC = UI / 'test' / 'fixtures' / 'scenarios'
T0 = 1790000000000
# the render fixture clock starts at 2026-09-29 04:00 UTC: exit results are two minutes old then
OBS = 1790654400000 - 120000
EP = 'rt-fixture-e1'

_hist = json.loads((SC / 'overview_i18n_running.json').read_text(encoding='utf-8'))
HISTORY = next(s['deliver']['data'] for s in _hist['steps'] if s.get('deliver', {}).get('topic') == 'traffic.history')
_cfg = json.loads((SC / 'settings_autostart_error.json').read_text(encoding='utf-8'))
CONFIG = next(s['reply'][0]['data'] for s in _cfg['steps'] if s.get('on_invoke', {}).get('cmd') == 'config.snapshot')

COMMANDS = ['app.info', 'app.quit', 'config.snapshot', 'exit.verify', 'mode.set', 'service.restart', 'service.start', 'service.stop']
TOPICS = ['bastion.pool', 'config.changed', 'core.info', 'exit.identity', 'gateway.current', 'jobs', 'killswitch.state', 'network.mode',
          'nodes.list', 'protocols.mix', 'route.published', 'service.collect', 'service.state', 'tier.transition', 'tiers.summary',
          'traffic.history', 'traffic.tick']


def env(epoch=EP, **kw):
    return {'epoch': epoch, 'produced_at_ms': T0, 'sent_at_ms': T0 + 1, **kw}


def evt(topic, data, rev=1, key=None, kind='snapshot', seq=None, epoch=EP):
    f = {'t': 'evt', **env(epoch), 'topic': topic, 'rev': rev, 'kind': kind, 'data': data}
    if key is not None:
        f['key'] = key
    if seq is not None:
        f['seq'] = seq
    return f


def hello(epoch=EP):
    return {'t': 'hello', **env(epoch), 'proto': 1, 'app_version': '1.0.0', 'runtime_pid': 4381, 'lang': 'zh_CN',
            'caps': {'commands': COMMANDS, 'topics': TOPICS, 'features': []}, 'scope': {'kind': 'custom', 'id': 'rt-fixture'},
            'network_recovery': {'proxy': {'state': 'restored', 'error_code': None, 'remaining_count': 0},
                                 'firewall': {'state': 'not_needed', 'error_code': None, 'remaining_count': 0}}}


def err(code, key, params=None, reason=None):
    e = {'code': code, 'message_key': key, 'params': params or {}}
    if reason:
        e['reason_code'] = reason
    return e


def svc(phase='running', *, stop=False, core=None, cleanup=False, ready='ready', validating=False, failure=None, uptime=120):
    core = (phase in ('running', 'core_standby', 'stopping')) if core is None else core
    return {
        'intent': {'desired_running': phase not in ('stopped',), 'stop_requested': stop, 'lifecycle_revision': 12, 'runtime_generation': 4},
        'runtime': {'runtime_running': phase == 'running', 'startup_phase': phase, 'shutdown_cleanup_pending': False,
                    'failure_code': failure['code'] if failure else None, 'failure': failure},
        'core': {'running': core, 'pid': 7654 if core else None, 'process_generation': 4, 'core_generation': 8, 'cleanup_pending': cleanup},
        'readiness': {'state': ready, 'validation_active': validating, 'reason': '', 'remaining_ms': None, 'publication_revision': 36,
                      'lifecycle_revision': 12, 'route_epoch': 7, 'network_context_epoch': 2},
        'freshness': {'sampled_at_ms': T0, 'snapshot_revision': 51, 'stale': False, 'uptime_s': uptime if phase == 'running' else None},
    }


def tiers(carrier='vanguard', *, fallback=False, offline=None, v=(2, 3), s=(1, 2), b=(25, 40), g=(2, 2), on=None, extra=None):
    on = on or {}
    t = {
        'vanguard': {'enabled': on.get('vanguard', True), 'total': v[1], 'usable': v[0]},
        'sentinel': {'enabled': on.get('sentinel', True), 'total': s[1], 'usable': s[0]},
        'bastion': {'enabled': on.get('bastion', True), 'total': b[1], 'usable': b[0]},
        'gateway': {'enabled': on.get('gateway', True), 'total': g[1], 'usable': g[0]},
    }
    for k, x in (extra or {}).items():
        t[k].update(x)
    order = ['vanguard', 'sentinel', 'bastion', 'gateway']
    if carrier in order:
        order = [carrier] + [x for x in order if x != carrier]
    pool_u = t['vanguard']['usable'] * t['vanguard']['enabled'] + t['sentinel']['usable'] * t['sentinel']['enabled'] + t['gateway']['usable'] * t['gateway']['enabled']
    pool_t = t['vanguard']['total'] + t['sentinel']['total'] + t['gateway']['total']
    return {'carrier': carrier, 'offline_reason': offline, 'order': order,
            'degraded': bool(carrier and t[carrier]['usable'] < t[carrier]['total']), 'fallback': fallback,
            'tiers': t, 'pool': {'usable': pool_u, 'total': pool_t}}


def route(leaf='HK-01', tier='vanguard', valid=True, chain=None):
    if leaf is None:
        return {'publication_revision': 20, 'owner': None, 'selected_chain': [], 'leaf': None, 'managed_tier': None,
                'observed_at_ms': T0, 'valid': False, 'invalid_reason': 'no_published_route', 'fail_closed_confirmed': False}
    return {'publication_revision': 20,
            'owner': {'process_pid': 7654, 'process_generation': 4, 'core_generation': 8, 'semantic_digest': 'a' * 64, 'route_epoch': 7,
                      'network_context_epoch': 2},
            'selected_chain': chain or ['TIER1', leaf], 'leaf': leaf, 'managed_tier': tier, 'observed_at_ms': T0, 'valid': valid,
            'invalid_reason': None if valid else 'route_epoch_changed', 'fail_closed_confirmed': False}


def node(name, tier='vanguard', status='healthy', latency=42, reason=None, protocol='vless', i=1, **kw):
    return {'id': f'{i:024x}', 'name': name, 'tier': tier, 'kind': 'manual', 'enabled': True, 'status': status, 'reason_code': reason,
            'protocol': protocol, 'latency_ms': latency, **kw}


def leg(addr=None, country='JP', status=None, reason=None, **ctx):
    if addr:
        return {'status': status or 'ok', 'address': addr, 'country': country, 'reason_code': reason, 'observed_at_ms': OBS, **ctx}
    return {'status': status or 'unknown', 'address': None, 'country': None, 'reason_code': reason, 'observed_at_ms': None}


V4, V6 = '203.0.113.24', '2001:db8::24'


def ident(status='ok', v4=None, v6=None, *, reason=None, fresh=None, trigger='auto', req='auto-1', flags=None, binding='b-1'):
    good = status in ('ok', 'partial')
    if v4 is None:
        v4 = leg(V4) if status in ('ok', 'partial') else leg()
    if v6 is None:
        v6 = leg(V6) if status == 'ok' else leg(status='failed', reason='identity_unavailable') if status == 'partial' else leg()
    r = {'request_id': req, 'generation': 8, 'binding_id': binding if good else None, 'status': status, 'reason_code': reason,
         'v4': v4, 'v6': v6, 'fresh': good if fresh is None else fresh, 'observed_at_ms': OBS if good else None,
         'native_fallback_verified': False, 'native_fallback_verified_commit_rejected': False,
         'native_fallback_unverified_budget': False, 'manual_execution_binding_stale': False}
    r.update(flags or {})
    if trigger and status != 'idle':
        r['trigger'] = trigger
    return r


def idle():
    return ident('idle', trigger=None, req='idle-0')


def collect(state='complete', running=None):
    rows = []
    for t in ['bastion', 'gateway', 'sentinel', 'vanguard']:
        fc = (running or {}).get(t, state)
        rows.append({'tier': t, 'generation': 1, 'first_check': fc, 'source_count': 2 if fc == 'complete' else None,
                     'healthy': 2 if fc == 'complete' else None, 'required': 1 if fc == 'complete' else None,
                     'observed_at_ms': T0 if fc == 'complete' else None})
    return {'generation': 1, 'tiers': rows, 'observed_at_ms': T0}


def app_info(epoch=EP):
    return {'on_invoke': {'cmd': 'app.info'}, 'reply': [{'t': 'res', **env(epoch), 'id': 1, 'req_id': '$req_id', 'owner': '$owner', 'cmd': 'app.info', 'ok': True,
            'data': {'version': '1.0.0', 'language': 'zh_CN', 'caps': [], 'listeners': [
                {'kind': 'mixed', 'host': '127.0.0.1', 'port': 7890, 'available': True},
                {'kind': 'controller', 'host': '127.0.0.1', 'port': 9090, 'available': True}]}}]}


def config_snapshot(auto=True, epoch=EP):
    data = copy.deepcopy(CONFIG)
    data['values']['ui']['overview_auto_exit_lookup'] = auto
    return {'on_invoke': {'cmd': 'config.snapshot'}, 'reply': [{'t': 'res', **env(epoch), 'id': 2, 'req_id': '$req_id', 'owner': '$owner',
                                                               'cmd': 'config.snapshot', 'ok': True, 'data': data}]}


def job_reply(op, name, state, *, result=None, error=None, accepted=True, skipped=None, seq=1):
    ack = {'t': 'ack', **env(), 'id': 3, 'req_id': '$req_id', 'owner': '$owner', 'accepted': accepted}
    if accepted:
        ack['op'] = op
    else:
        ack['skipped_reason'] = skipped
        return [ack]
    d = {'op': op, 'req_id': '$req_id', 'owner': '$owner', 'name': name, 'state': state, 'started_at_ms': T0, 'progress': None,
         'result': result, 'error': error}
    return [ack, evt('jobs', d, rev=10 + seq, kind='edge', seq=seq)]


def tick(running=True, error=None):
    return {'t_ms': T0, 'up_bps': 2048 if running else 0, 'down_bps': 8192 if running else 0,
            'session': {'up_bytes': 512000, 'down_bytes': 1024000}, 'running': running, 'error_code': error}


class S:
    """One scenario: the base frames by topic, edited by the variant, then serialized in order."""

    def __init__(self, phase='running'):
        run = phase == 'running'
        self.f = {
            'service.state': svc(phase),
            'route.published': route() if run else route(None),
            'tiers.summary': tiers() if run else tiers(),
            'service.collect': collect(),
            'network.mode': {'mode': 'system', 'admin': True, 'switching': False, 'proxy_state': 'owned' if run else 'disabled', 'drift': False},
            'killswitch.state': {'enabled': True, 'active': False, 'admin': True, 'error': None, 'triggered': False},
            'bastion.pool': {'runtime_alive': 25, 'user_alive': 0, 'capacity': 60, 'min_alive_nodes': 10, 'target_alive': 25, 'collecting': False,
                             'observed_at_ms': T0} if run else None,
            'protocols.mix': {'source': 'loaded' if run else 'configured', 'counts': {'vless': 3, 'shadowsocks': 2, 'trojan': 1, 'hysteria2': 1},
                              'observed_at_ms': T0},
            # running with a fallback carrier: no Gateway on the route (effective only exists with
            # carrier gateway, M1); stopped: the saved choice (T-P2-H2)
            'gateway.current': ({'selected': None, 'effective': None, 'missing': False, 'applied': False, 'pending': None} if run
                                else {'selected': 'GW-Tokyo', 'effective': None, 'missing': False, 'applied': False, 'pending': None}),
            'exit.identity': {'current': ident() if run else idle(), 'previous': None},
            'traffic.tick': tick(run),
            'traffic.history': HISTORY,
            'core.info': {'running': run, 'pid': 7654 if run else None, 'version': '1.19.27', 'generation': 8, 'cleanup_pending': False},
        }
        self.nodes = {'vanguard': [node('HK-01', i=1), node('HK-02', latency=88, i=2), node('HK-03', status='failed', latency=None, reason='timeout', i=3)],
                      'sentinel': [node('SUB-JP-01', tier='sentinel', protocol='trojan', latency=120, i=4), node('SUB-JP-02', tier='sentinel', status='failed', latency=None, reason='timeout', protocol='trojan', i=5)]}
        self.auto = True
        self.after = []      # steps after the base (deliveries, advances, clicks are in the case)
        self.invokes = []

    def steps(self):
        out = [{'deliver': {'t': 'rt:reset', 'epoch': EP, 'reason': 'reconnect'}}, {'deliver': hello()}]
        for topic, data in self.f.items():
            if data is not None:
                out.append({'deliver': evt(topic, data)})
        for k, items in self.nodes.items():
            out.append({'deliver': evt('nodes.list', {'items': items}, key=k)})
        out.append({'deliver': {'t': 'host:language', 'language': 'zh_CN', 'epoch': EP}})
        out.append({'deliver': {'t': 'host:visibility', 'visible': True, 'epoch': EP}})
        out += [app_info(), config_snapshot(self.auto)] + self.invokes + self.after
        return out


SCEN = {}


def scen(name, desc):
    def deco(fn):
        SCEN[name] = (desc, fn)
        return fn
    return deco


def starting(s):
    """While the service starts: the mix is the configuration's, no Bastion baseline, no route, no traffic yet."""
    s.f['protocols.mix']['source'] = 'configured'
    s.f['bastion.pool'] = None
    s.f['route.published'] = route(None)
    s.f['traffic.tick'] = tick(False)
    s.f['network.mode']['proxy_state'] = 'disabled'
    s.f['gateway.current'] = {'selected': 'GW-Tokyo', 'effective': None, 'missing': False, 'applied': False, 'pending': None}


# ---------------------------------------------------------------- run / start / stop
@scen('overview_stopped', '服务已停止：停止态英雄区与回退说明、出口「服务未启动」、协议按配置统计、Bastion 没有基线（未知）')
def _(s):
    pass


@scen('overview_start_collecting', '启动第 1 阶段 collecting：药丸 1/3、各层收集中、出口「服务启动中」')
def _(s):
    s.f['service.state'] = svc('collecting', core=False)
    s.f['service.collect'] = collect('running')
    s.f['exit.identity'] = {'current': idle(), 'previous': None}
    starting(s)


@scen('overview_start_standby', '启动第 2 阶段 core_standby + validation_active：药丸 2/3「正在验证出口」，Vanguard 已完成首次检测')
def _(s):
    s.f['service.state'] = svc('core_standby', validating=True)
    s.f['service.collect'] = collect('running', running={'vanguard': 'complete'})
    s.f['exit.identity'] = {'current': idle(), 'previous': None}
    starting(s)


@scen('overview_ready_validating', '启动第 3 阶段：running 但 readiness.validation_active（英雄区「正在验证最终出口」，不播「已就绪」）')
def _(s):
    s.f['service.state'] = svc('running', ready='serving', validating=True)


@scen('overview_cancelling', '启动被取消：starting 且 intent.stop_requested（药丸、英雄区、层卡、操作段一律「取消中」）')
def _(s):
    s.f['service.state'] = svc('collecting', core=False, stop=True)
    s.f['service.collect'] = collect('running')
    s.f['exit.identity'] = {'current': idle(), 'previous': None}
    starting(s)


@scen('overview_running', '运行中、就绪：承载层 Vanguard、出口双栈已确认、未使用 Gateway（经由 HK-01 直接出口）、协议 Mihomo 已加载')
def _(s):
    pass


for st in ['activation_hold', 'controller_hold', 'facility_hold', 'data_plane_failed', 'fail_closed']:
    def mk(st):
        def _(s):
            s.f['service.state'] = svc('running', ready=st)
            if st == 'fail_closed':
                s.f['tiers.summary'] = tiers(None, offline='fail_closed', v=(0, 3), s=(0, 2), b=(0, 40))
                s.f['route.published'] = route('HK-01', valid=False)
        return _
    scen(f'overview_ready_{st}', f'运行中 readiness.state={st}（G06 英雄区）')(mk(st))

START_FAILS = {
    'listener_conflict': err('runtime_listener_conflict', 'errors.start.listener_conflict', {'kind': 'mixed', 'port': 7890}, 'RuntimeListenerPreflightError'),
    'insufficient_nodes': err('needs_route', 'errors.start.insufficient_validated_nodes', {'healthy': 1, 'required': 3}, 'insufficient_validated_nodes'),
    'core_timeout': err('timeout', 'errors.start.core_start_timeout', {}, 'core_start_timeout'),
    'geodata_needs_route': err('needs_route', 'errors.start.geodata_needs_route', {}, 'geodata_needs_route'),
    'admin_required': err('admin_required', 'errors.start.admin_required', {}, 'admin_required'),
}
for k, e in START_FAILS.items():
    def mk(e):
        def _(s):
            s.f['service.state'] = svc('failed', core=False, ready='fail_closed', failure=e)
            s.f['route.published'] = route(None)
            s.f['bastion.pool'] = None
            s.f['exit.identity'] = {'current': idle(), 'previous': None}
            s.f['protocols.mix']['source'] = 'configured'
            s.f['gateway.current'] = {'selected': 'GW-Tokyo', 'effective': None, 'missing': False, 'applied': False, 'pending': None}
            s.f['traffic.tick'] = tick(False)
            s.f['network.mode']['proxy_state'] = 'disabled'
        return _
    scen(f'overview_start_failed_{k}', f'启动失败（PROTOCOL §14 代表码 {e["message_key"]}）：药丸「启动失败 · 重试」，英雄区写失败原因')(mk(e))


@scen('overview_cleanup_pending', '启动失败但 Core 仍在运行且清理未完成：药丸保留「停止」，「重试」在读数区')
def _(s):
    s.f['service.state'] = svc('failed', core=True, cleanup=True, ready='fail_closed',
                               failure=err('lifecycle_busy', 'errors.start.cleanup_pending', {}, 'cleanup_pending'))
    s.f['exit.identity'] = {'current': idle(), 'previous': None}


@scen('overview_stop_failed', '运行中点「停止」：service.stop 作业失败（io，PROTOCOL 7 停止只有 lifecycle_busy 与 io），药丸「停止失败 · 重试」，英雄区说明（G05）')
def _(s):
    s.invokes.append({'on_invoke': {'cmd': 'service.stop'}, 'reply': job_reply('op-stop-1', 'service.stop', 'failed', error=err('io', 'errors.io'))})


@scen('overview_stop_refused', '运行中点「停止」：service.stop 被拒绝（lifecycle_busy）：只在英雄区短暂提示，药丸仍是运行中，不进「停止失败」')
def _(s):
    s.invokes.append({'on_invoke': {'cmd': 'service.stop'}, 'reply': job_reply('op-stop-1', 'service.stop', 'failed', error=err('lifecycle_busy', 'errors.lifecycle_busy'))})


# ---------------------------------------------------------------- exit card
EXIT = {
    'overview_exit_ok': ('出口双栈成功（已确认，自动查询）', lambda: {'current': ident(), 'previous': None}),
    'overview_exit_running': ('手动查询进行中（querying，刷新禁用，上次结果变暗）', lambda: {'current': ident('running', leg(status='running'), leg(status='running'), reason='querying', trigger='manual', req='manual-2'), 'previous': ident(fresh=False)}),
    'overview_exit_upgrading': ('自动查询中点刷新：合并为 upgrading（将在当前查询结束后再验证一次）', lambda: {'current': ident('running', leg(status='running'), leg(status='running'), reason='upgrading', trigger='manual', req='manual-2'), 'previous': None}),
    'overview_exit_partial': ('单栈 partial：IPv6 失败写原因（identity_timeout），不显示假地址', lambda: {'current': ident('partial', leg(V4), leg(status='failed', reason='identity_timeout')), 'previous': None}),
    'overview_exit_v6_only': ('只有 IPv6：IPv4 失败（identity_unavailable），IPv6 地址与自己的国家', lambda: {'current': ident('partial', leg(status='failed', reason='identity_unavailable'), leg(V6, country='SG')), 'previous': None}),
    'overview_exit_failed': ('查询失败（identity_timeout）：显示上次结果并标「上次结果」', lambda: {'current': ident('failed', reason='identity_timeout', req='auto-2'), 'previous': ident(fresh=False)}),
    'overview_exit_failed_noprev': ('查询失败、没有上次结果（exit_unreachable）：卡内写原因', lambda: {'current': ident('failed', reason='exit_unreachable', req='auto-2'), 'previous': None}),
    'overview_exit_cancelled': ('被取消：安静地回到上次结果', lambda: {'current': ident('cancelled', reason='owner_revoked', req='manual-3', trigger='manual'), 'previous': ident(fresh=False)}),
    'overview_exit_superseded': ('被取代：安静地回到上次结果', lambda: {'current': ident('superseded', reason='route_changed', req='manual-3', trigger='manual'), 'previous': ident(fresh=False)}),
    'overview_exit_expired': ('已过期（fresh=false，identity_stale）：地址保留，「已过期 · 点刷新」', lambda: {'current': ident(reason='identity_stale', fresh=False), 'previous': None}),
    'overview_exit_changed': ('出口已变化（route_changed 失败）：显示上次结果', lambda: {'current': ident('failed', reason='route_changed', req='auto-2'), 'previous': ident(fresh=False)}),
    'overview_exit_conflict': ('出口冲突（identity_conflict）：隐藏 IP', lambda: {'current': ident('failed', reason='identity_conflict', req='auto-2'), 'previous': ident(fresh=False)}),
    'overview_exit_rotating': ('出口轮换（identity_rotating）：隐藏 IP', lambda: {'current': ident('failed', reason='identity_rotating', req='auto-2'), 'previous': None}),
    'overview_exit_warn': ('警告（system_proxy_capture_inactive）：地址保留，徽标写警告', lambda: {'current': ident(reason='system_proxy_capture_inactive'), 'previous': None}),
    'overview_exit_fallback_verified': ('Fallback 结果（native_fallback_verified）：地址保留，徽标说明 Mihomo 自行切换且已核实', lambda: {'current': ident(flags={'native_fallback_verified': True}), 'previous': None}),
    'overview_exit_route_contract_failed': ('route_contract_failed 失败', lambda: {'current': ident('failed', reason='route_contract_failed', req='auto-2'), 'previous': None}),
    'overview_exit_waiting': ('正在等待连接（waiting_connection，G33）', lambda: {'current': ident('running', leg(status='running'), leg(status='running'), reason='waiting_connection', trigger='manual', req='manual-2'), 'previous': None}),
    'overview_exit_retrying': ('正在重试（retrying，G33）', lambda: {'current': ident('running', leg(status='running'), leg(status='running'), reason='retrying', trigger='manual', req='manual-2'), 'previous': None}),
    'overview_exit_idle': ('运行中还没有结果、自动查询开启：「等待查询」，不无限转圈（S-E2）', lambda: {'current': idle(), 'previous': None}),
    'overview_exit_manual': ('手动验证的结果（trigger=manual）：「已验证」', lambda: {'current': ident(trigger='manual', req='manual-1'), 'previous': None}),
}
for name, (desc, fn) in EXIT.items():
    def mk(fn):
        def _(s):
            s.f['exit.identity'] = fn()
        return _
    scen(name, desc)(mk(fn))


@scen('overview_exit_deferred', '启动中点刷新：deferred（就绪后查询，最多等 2 分钟）')
def _(s):
    s.f['service.state'] = svc('core_standby', validating=True)
    s.f['service.collect'] = collect('running', running={'vanguard': 'complete'})
    s.f['exit.identity'] = {'current': ident('running', leg(), leg(), reason='deferred', trigger='manual', req='manual-2'), 'previous': None}
    starting(s)


@scen('overview_auto_off', '运行中没有结果、自动出口查询关闭（G09）：「未自动查询 · 点刷新查看」')
def _(s):
    s.f['exit.identity'] = {'current': idle(), 'previous': None}
    s.auto = False


@scen('overview_c2_fields', 'T-P4-C2 字段出现：城市、时区、ASN、运营商、网络类型、单一来源、trigger=manual；各层待复检计数')
def _(s):
    s.f['exit.identity'] = {'current': ident('ok', leg(V4, city='Tokyo', timezone='Asia/Tokyo', asn='64500', org='Example Networks', type='hosting', single_source=True),
                                             leg(V6, single_source=False), trigger='manual', req='manual-1'), 'previous': None}
    s.f['tiers.summary'] = tiers(extra={'vanguard': {'pending': 1}, 'sentinel': {'pending': 2}, 'gateway': {'pending': 0}})


# ---------------------------------------------------------------- strip
@scen('overview_mode_drift', '系统代理被其他程序改写（G08 drift）：提示与「重启服务」')
def _(s):
    s.f['network.mode'] = {'mode': 'system', 'admin': True, 'switching': False, 'proxy_state': 'other', 'drift': True}


@scen('overview_mode_proxy_unknown', '系统代理状态未知：「无法读取系统代理设置」')
def _(s):
    s.f['network.mode'] = {'mode': 'system', 'admin': True, 'switching': False, 'proxy_state': 'unknown', 'drift': False}


@scen('overview_mode_admin_required', '停止时切到全局：mode.set 失败 admin_required（停止时也会，W-05）')
def _(s):
    s.invokes.append({'on_invoke': {'cmd': 'mode.set', 'args': {'mode': 'tun'}}, 'reply': job_reply('op-mode-1', 'mode.set', 'failed', error=err('admin_required', 'errors.admin_required'))})


@scen('overview_mode_cleanup_pending', '停止时切到混合：mode.set 失败 lifecycle_busy/cleanup_pending（「正在清理上一次的网络设置」）')
def _(s):
    s.invokes.append({'on_invoke': {'cmd': 'mode.set', 'args': {'mode': 'mixed'}}, 'reply': job_reply('op-mode-1', 'mode.set', 'failed', error=err('lifecycle_busy', 'errors.lifecycle_busy', {}, 'cleanup_pending'))})


@scen('overview_mode_not_applied', '运行中确认切到全局：mode.set 失败 saved=true / applied=false（「已保存，重启服务后生效」+ apply_error）')
def _(s):
    s.invokes.append({'on_invoke': {'cmd': 'mode.set', 'args': {'mode': 'tun'}}, 'reply': job_reply('op-mode-1', 'mode.set', 'failed',
                      result={'mode': 'tun', 'changed': True, 'saved': True, 'restarted': True, 'applied': False, 'apply_error': err('io', 'errors.mode.apply_failed')},
                      error=err('io', 'errors.mode.apply_failed'))})


@scen('overview_mode_next_start', '停止时切到混合：mode.set done applied=true（「下次启动生效」）')
def _(s):
    s.invokes.append({'on_invoke': {'cmd': 'mode.set', 'args': {'mode': 'mixed'}}, 'reply': job_reply('op-mode-1', 'mode.set', 'done',
                      result={'mode': 'mixed', 'changed': True, 'saved': True, 'restarted': False, 'applied': True, 'apply_error': None})})


KS = {
    'overview_ks_disabled': ('熔断保护已关闭（enabled=false）', {'enabled': False, 'active': False, 'admin': True, 'error': None, 'triggered': False}),
    'overview_ks_blocked': ('熔断保护未生效：没有管理员权限', {'enabled': True, 'active': False, 'admin': False, 'error': None, 'triggered': False}),
    'overview_ks_error': ('熔断保护出错（error≠null）', {'enabled': True, 'active': False, 'admin': True, 'error': err('io', 'errors.killswitch.command_failed'), 'triggered': False}),
    'overview_ks_triggered': ('熔断保护已触发（triggered=true）：英雄区「出站已阻断」', {'enabled': True, 'active': True, 'admin': True, 'error': None, 'triggered': True}),
}
for name, (desc, data) in KS.items():
    def mk(data):
        def _(s):
            s.f['killswitch.state'] = data
            if data['triggered']:
                s.f['tiers.summary'] = tiers(None, offline='fail_closed', v=(0, 3), s=(0, 2), b=(0, 40))
        return _
    scen(name, desc)(mk(data))


@scen('overview_bastion_off', 'Bastion 未启用：层卡「未启用」、节点池不画 Bastion')
def _(s):
    s.f['tiers.summary'] = tiers(on={'bastion': False}, b=(0, 0))


@scen('overview_bastion_collecting', 'Bastion 正在补充（collecting）且低于最少节点数')
def _(s):
    s.f['bastion.pool'] = {'runtime_alive': 4, 'user_alive': 0, 'capacity': 60, 'min_alive_nodes': 10, 'target_alive': 25, 'collecting': True, 'observed_at_ms': T0}
    s.f['tiers.summary'] = tiers('bastion', fallback=True, v=(0, 3), s=(0, 2), b=(4, 40))
    s.f['route.published'] = route('PUB-01', tier='bastion')
    s.nodes['bastion'] = [node('PUB-01', tier='bastion', protocol='shadowsocks', latency=310, i=9)]


@scen('overview_bastion_low', 'Bastion 低于最少节点数（未在补充）：琥珀')
def _(s):
    s.f['bastion.pool'] = {'runtime_alive': 4, 'user_alive': 0, 'capacity': 60, 'min_alive_nodes': 10, 'target_alive': 25, 'collecting': False, 'observed_at_ms': T0}


@scen('overview_proto_other', '协议超过 6 种：尾部合并为「其他」（G33）')
def _(s):
    s.f['protocols.mix']['counts'] = {'vless': 9, 'shadowsocks': 7, 'trojan': 5, 'hysteria2': 4, 'vmess': 3, 'tuic': 2, 'socks5': 1, 'http': 1}


@scen('overview_proto_empty', '还没有节点：协议分布为空（L6）')
def _(s):
    s.f['protocols.mix']['counts'] = {}


@scen('overview_gateway_missing', 'Gateway 当前出口已缺失（missing=true）：EXIT-SELECT 只剩 REJECT，路由叶子是 REJECT，没有承载层（运行时形状，评审 05a M1）')
def _(s):
    s.f['gateway.current'] = {'selected': 'GW-Tokyo', 'effective': None, 'missing': True, 'applied': False, 'pending': None}
    s.f['tiers.summary'] = tiers(None, offline='unmanaged_route')
    s.f['route.published'] = route('REJECT', tier=None, chain=['FINAL-EXIT', 'EXIT-SELECT', 'REJECT'])


def gateway_carrier(s):
    """Gateway as the exit, the runtime's shape (review 05a M1): FINAL-EXIT -> EXIT-SELECT -> the Gateway
    node; managed_tier = carrier = gateway, order gateway first, fallback false, effective = leaf."""
    s.f['tiers.summary'] = tiers('gateway')
    s.f['route.published'] = route('GW-Tokyo', tier='gateway', chain=['FINAL-EXIT', 'EXIT-SELECT', 'GW-Tokyo'])
    s.f['gateway.current'] = {'selected': 'GW-Tokyo', 'effective': 'GW-Tokyo', 'missing': False, 'applied': True, 'pending': None}
    s.nodes['gateway'] = [node('GW-Tokyo', tier='gateway', protocol='vless', latency=64, i=7), node('GW-Osaka', tier='gateway', protocol='trojan', latency=71, i=8)]


@scen('overview_gateway_carrier', 'Gateway 固定出口生效（运行时形状）：carrier=gateway、order 以 gateway 开头、fallback=false、effective=leaf；英雄区 Gateway + 「经过的回退层暂时无法确认」，前卡「承载未确认」，路由行「经由未确认的承载层 → GW-Tokyo」')
def _(s):
    gateway_carrier(s)


@scen('overview_gateway_off', '未使用 Gateway（selected=null）：路由行「直接出口」')
def _(s):
    s.f['gateway.current'] = {'selected': None, 'effective': None, 'missing': False, 'applied': False, 'pending': None}


@scen('overview_tier_fallback', '已回退：Sentinel 承载（order 承载层在首位），Vanguard 全部不可用（timeout 占多数）')
def _(s):
    s.f['tiers.summary'] = tiers('sentinel', fallback=True, v=(0, 3))
    s.f['route.published'] = route('SUB-JP-01', tier='sentinel')
    s.nodes['vanguard'] = [node('HK-01', status='failed', latency=None, reason='timeout', i=1), node('HK-02', status='failed', latency=None, reason='timeout', i=2),
                           node('HK-03', status='failed', latency=None, reason='tls_error', i=3)]


@scen('overview_tier_unconfigured', 'Vanguard 已启用但没有节点（total=0）：「未配置」；承载层 Sentinel')
def _(s):
    s.f['tiers.summary'] = tiers('sentinel', fallback=True, v=(0, 0))
    s.f['route.published'] = route('SUB-JP-01', tier='sentinel')
    s.nodes['vanguard'] = []


@scen('overview_traffic_error', '流量采样出错（traffic.tick.error_code）：「流量数据暂不可用」')
def _(s):
    s.f['traffic.tick'] = tick(True, 'monitor_unavailable')


@scen('overview_history_single', '流量历史只有一个点：图表空状态，不出现 NaN（决定_第五波裁定 10-5）')
def _(s):
    h = copy.deepcopy(HISTORY)
    h['h24'] = h['h24'][-1:]
    h['live'] = h['live'][-1:]
    s.f['traffic.history'] = h


@scen('overview_b_items', 'B 项控件：承载层当前节点 Node.slow=true（T-P4-C2 第 4 项）。B-01 未开时不显示「延迟升高」，--h-features all 构建里显示')
def _(s):
    s.nodes['vanguard'][0]['slow'] = True


# ---------------------------------------------------------------- stale / reset
@scen('overview_stale', '运行中 4 秒没有新的 service.state / traffic.tick：各区「数据未更新」，保留上次值')
def _(s):
    s.after.append({'advance_ms': 4000})


@scen('overview_stale_regions', '运行中、就绪的底稿；渲染用例再依次送 route.published、nodes.list(vanguard)、bastion.pool 各一个 gap（基线撤下、等补发）：节点行、路由行与 Bastion 卡保留上次值并标「数据未更新」（评审 05a S6）')
def _(s):
    # the gaps are the case's deliver actions: a gap in the steps asks for a resync, which in the
    # fake (no rt_resync) reconnects at once and drops the rest of the steps
    pass


@scen('overview_stale_gateway', 'Gateway 固定出口生效时 gateway.current 收到 gap：Gateway 行与路由行保留上次值并标「数据未更新」（评审 05a S6）')
def _(s):
    gateway_carrier(s)
    s.after.append({'deliver': evt('gateway.current', {'from_seq': 1, 'to_seq': 1, 'dropped': 1}, rev=2, kind='gap', seq=2)})


@scen('overview_reset_generation', '运行中途 rt:reset（runtime_gone）+ 新 epoch 的 hello：全页回到「正在重新连接运行时」，不显示旧值')
def _(s):
    s.after += [{'advance_ms': 500}, {'deliver': {'t': 'rt:reset', 'epoch': 'rt-fixture-e2', 'reason': 'runtime_gone'}},
                {'deliver': hello('rt-fixture-e2')}]
    # the page asks its two queries again in the new epoch
    s.invokes += [app_info('rt-fixture-e2'), config_snapshot(True, 'rt-fixture-e2')]


@scen('overview_reconnect_same_epoch', '同 epoch rt:reset{reconnect}：重放 hello 与同样的快照，数据与品牌点不闪、不回骨架（批评 B9）')
def _(s):
    s.replay = True


@scen('overview_reconnect_newrev', '同 epoch rt:reset{reconnect}：重放 hello 与内容相同但 rev 更新的快照（客户端不去重、逐个重装），数据与品牌点仍然不闪（评审 05a S9）')
def _(s):
    s.replay = 'newrev'


def build(name):
    desc, fn = SCEN[name]
    stop_base = name in ('overview_stopped', 'overview_mode_admin_required', 'overview_mode_cleanup_pending', 'overview_mode_next_start') or name.startswith('overview_start_failed_')
    s = S('stopped' if stop_base else 'running')
    if stop_base:
        s.f['bastion.pool'] = None
    fn(s)
    doc = {'name': name, 'desc': 'T-P4-05a: ' + desc + '。ASCII data only.', 'lang': 'zh_CN', 'steps': s.steps()}
    if getattr(s, 'replay', False):
        # the shell's replay on the next rt_connect: the same epoch, the cached snapshots
        doc['replay'] = [copy.deepcopy(st['deliver']) for st in doc['steps'] if 'deliver' in st and st['deliver'].get('t') in ('rt:reset', 'hello', 'evt')]
        if s.replay == 'newrev':
            # the same content under a newer rev: the topic cache installs every frame again
            for f in doc['replay']:
                if f.get('t') == 'evt':
                    f['rev'] += 1
    return doc


def main():
    names = sys.argv[2:] or list(SCEN)
    for n in names:
        p = SC / f'{n}.json'
        p.write_bytes((json.dumps(build(n), ensure_ascii=False, indent=2) + '\n').encode('utf-8'))
    print(len(names), 'scenario(s)')


if __name__ == '__main__':
    main()
