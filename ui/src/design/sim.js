/* ==========================================================================
   sim.js — T.sim : the simulated runtime of the design target (every behaviour
   here is fake). T-P4-02b: the simulator no longer writes the store. It keeps its
   own facts, a frame generator (design/frames.js) turns them into v1 frames, and
   FakeTransport (rt/transport-fake.js, sim mode) delivers them to T.rt, which
   projects them onto the store keys and rt:* events exactly as in the app target
   (P4 architecture §2.6 last item, §2.7). Commands and queries the pages send with
   T.rt.call / T.rt.query reach the simulator's actions through the same transport.
   --------------------------------------------------------------------------
   State machine  stopped → starting (steps 收集节点 → 验证出口 → 就绪)
                  → running → stopping → stopped.  stop() cancels a start.
   T.sim (the prototype API, kept for the pages until their 05x tickets)
                  start() · stop() · toggle() · restart() · verifyExit() · setMode(mode)
                    go through T.rt.call — the same path a page takes after 05x
                  bastion.enable(on) · setMin(n) · refill() · fillTo() · recount() ·
                    usable(n) change the simulator's facts directly (settings the
                    prototype pages still make locally) and publish the result
                  scenario('fallback' | 'recover') · setState('running' | 'stopped')
                    demo only (__DEMO__)
                  phases · history (the projected traffic.history) · logs (sim:log
                    records, fed from the logs.batch frames) · state · init()
                    (idempotent: the boot starts the simulator before core/app.js
                    calls init(); T-P4-06 removes that call)
   Commands       service.start / stop / restart, mode.set, exit.verify,
                  gateway.select, settings.apply (a SaveResult only, the config is
                  not changed), logs.setMihomoLevel, app.update.check, geo.update,
                  subscriptions.refresh, cancel (exit.verify only); anything else
                  answers not_allowed (an honest degradation of the design target).
                  Every accepted job reaches a terminal. A job that repeats one under
                  way is merged into it as the sidecar does (T-P4-H): a second stop
                  shares the stop intent, a start while starting / running and a
                  restart while restarting end with the one under way; a start while a
                  stop is pending is refused lifecycle_busy (the executor's rule).
                  Refused acks carry skipped_reason + errors.bridge.<code>. Repeated
                  subscription refreshes queue (the serial mutation domain: queued,
                  then running; cancellable while queued), Geo downloads run one after
                  another (the updater's action lock), update checks share the one in
                  flight (UpdateOwner.check). geo / subscription / update jobs run on
                  their own timers; mode.set, gateway.select and a stored refresh move
                  the config rev and send config.changed (PROTOCOL §15)
   Queries        config.snapshot, app.info, alerts.catalog, import.specs, logs.tail,
                  logs.query, nodes.detail, autostart.state, geo.status,
                  alerts.deliveries
   ========================================================================== */
import CATALOG from './catalog.json' with { type: 'json' };
import {
  CORE_VERSION, LISTENERS, SECRET_FIELDS_SET, BASTION_SOURCES, configValues, seedAlerts, seedLogRecords,
} from './demo-data.js';
import {
  EPOCH, PAGE_COMMANDS, PAGE_QUERIES, PAGE_TOPICS, TIER_IDS, bastionPool, chosenGateway, configItems, coreInfo, createPublisher,
  gatewayCurrent, identity, killswitchState, logsFeed, networkMode, nodeRow, nodesList, pageRecordToLog, protocolsMix,
  routePublished, serviceCollect, serviceState, simRecordToLog, startupPhase, subscriptionsList, tiersSummary, usableNode,
} from './frames.js';

const PH = [
  { key: 'collect', label: '收集节点', hint: '正在收集节点', dur: 1350 },
  { key: 'verify', label: '验证出口', hint: '正在验证出口', dur: 1250 },
  { key: 'ready', label: '就绪', hint: '即将就绪', dur: 560 },
];
const COLLECT_AT = { vanguard: 380, sentinel: 760, bastion: 1020 };
// the runtime's shape (PROTOCOL 5.3, T-P4-C1): all four tiers, gateway included, the carrier moved to
// the front (desktop_runtime tiers_summary_status); the overview deals the three fallback tiers from
// it (T-P4-05a, ruling 5th wave 02b-4)
const ORDER = ['vanguard', 'sentinel', 'bastion', 'gateway'];
const FALLBACK_ORDER = ['sentinel', 'vanguard', 'bastion', 'gateway'];
const LOG_KEEP = 240;
const COMMIT_ROUNDS = 8;
// message_key: errors.<code> as the sidecar's executor (bridge_jobs._error), or a key that the
// error catalog (i18n/locales/zh_CN/errors.json) has; the design target makes up no key of its own
const err = (code, key, extra) => ({ code, message_key: key || `errors.${code}`, params: {}, ...(extra || {}) });

export function createDesignSim(T, { features = [] } = {}) {
  const D = T.data, S = T.store;
  const KB = 1024, MB = 1024 * 1024;
  const now = () => Date.now();
  const rnd = T.util.rng(9152026);
  const pub = createPublisher({ epoch: EPOCH, now });

  /* ---- the simulator's facts (frames.js reads them; nothing here touches the store) ---- */
  const F = {
    run: 'stopped', step: -1, stopRequested: false, gen: 42, lifeRev: 1, coreGen: 7, coreRunning: false, pid: 0, snapRev: 0,
    uptime: 0, carrier: null, order: ORDER.slice(), collect: {}, collectFacts: {}, mode: 'system', switching: false,
    admin: true, ksEnabled: true, latCeiling: 800, restarting: false,
    bp: { enabled: false, ok: 0, capacity: 60, min: 3, phase: 'off', rotAt: 0, timer: 0 },
    exit: null, traffic: { down: 0, up: 0, rateDown: 0, rateUp: 0, at: 0 }, pubRev: 1, routeEpoch: 1, logLevel: 'info',
    geo: { running: false, updated_at_ms: null, errors: [] },
    update: { state: 'idle', version: null, update_available: false, installable: false, error: null },
    configRev: 1, refreshing: new Set(), alerts: { items: [], deliveries: [], lastSeq: 0 },
  };
  let inited = false, push = null, diag = null, BOOT = 0, history = null;
  let timers = [], scenTimers = [], exitTimer = 0, tickT = 0, tickN = 0, tgtD = 1.5 * MB;
  const workTimers = new Set();   // jobs outside the lifecycle (geo / subscription / update): no lifecycle step clears them
  let exitCheck = null;           // {how, request_id, waiters: [job]} while an exit check runs
  let modePend = null, restartJob = null, startJob = null, stopJob = null;
  // jobs merged into the start / stop / restart under way: they end with it (T-P4-H)
  const joined = { start: [], stop: [], restart: [] };
  let refreshQ = [], refreshBusy = null;  // subscriptions.refresh: one at a time, the rest queued
  let geoQ = [], geoBusy = null;          // geo.update: downloads one after another
  let updateCheck = null;                 // {waiters: [job]} while an update check runs
  let shellId = 0, opSeq = 0, idSeq = 0, logN = 0, logSeq = 0;
  const jobs = new Map();         // op -> job of this epoch not yet terminal
  const legacy = new Map();       // Log seq -> the prototype-shaped record (sim:log, T.sim.logs)
  const logs = [];                // T.sim.logs (prototype records; pages push theirs too)

  const later = (fn, ms, list = timers) => { const id = setTimeout(fn, ms); list.push(id); return id; };
  const clearTimers = () => { timers.forEach(clearTimeout); timers = []; };
  const clearScenario = () => { scenTimers.forEach(clearTimeout); scenTimers = []; };
  // A command job's completion (PROTOCOL §6: a terminal is never swallowed). start / stop /
  // finishStart / setState clear the lifecycle timers only, so these always reach their end.
  const work = (fn, ms) => { const id = setTimeout(() => { workTimers.delete(id); fn(); }, ms); workTimers.add(id); return id; };
  const fillTo = () => Math.min(F.bp.capacity, Math.max(45, F.bp.min + 4));
  const summary = () => tiersSummary(F, D);
  const usable = n => usableNode(n, F.latCeiling);
  const idleExit = () => ({ current: identity({ request_id: 'idle-0', generation: 0, status: 'idle' }), previous: null });
  // the exit's source report on the IPv4 leg (IdentityLeg v1 additions, T-P4-C2 item 1): the demo
  // exit's city, time zone, ASN (digits), operator and network type
  const EXIT_GEO = { v4: { city: D.exit.v4.city, timezone: D.exit.v4.tz, asn: String(D.exit.v4.asn).replace(/^AS/, ''), org: D.exit.v4.org, type: 'hosting' }, v6: null };
  // an exit.verify job result carries no trigger (PROTOCOL 16 IdentityResult.trigger)
  const jobResult = r => { const { trigger, ...rest } = r; return rest; };

  /* ---- publishing: facts -> frames -> FakeTransport -> T.rt ---- */
  let flushing = false, again = null;
  let tierSeen = null;            // the last published managed_tier and its core generation
  function sync({ tick = false, lists = true } = {}) {
    const at = now();
    pub.state('network.mode', networkMode(F));
    pub.state('killswitch.state', killswitchState(F));
    pub.state('bastion.pool', bastionPool(F, fillTo, at));
    pub.state('tiers.summary', summary());
    if (lists) {
      for (const tier of TIER_IDS) {
        pub.list('nodes.list', tier, nodesList(D, tier, F.latCeiling));
        pub.list('subscriptions.list', tier, subscriptionsList(F, D, tier));
      }
      pub.list('config.items', null, configItems(D));
      pub.list('bastion.sources', null, BASTION_SOURCES);
    }
    pub.state('service.collect', serviceCollect(F, at));
    const route = routePublished(F, D, at);
    pub.state('route.published', route);
    // tier.transition as the sidecar derives it (bridge_tiers.transition): an edge whenever the
    // published route's managed_tier changes within one core generation, none across an invalid
    // route or a new core. So choosing or cancelling a Gateway is an edge (vanguard <-> gateway,
    // sentinel -> gateway, gateway -> sentinel ...), and a fallback under a chosen Gateway is none:
    // the runtime cannot see it (T-P4-H2 item 3).
    const mt = route.valid ? route.managed_tier : null;
    if (mt && tierSeen && tierSeen.gen === F.coreGen && tierSeen.tier !== mt) transition(tierSeen.tier, mt);
    tierSeen = mt ? { tier: mt, gen: F.coreGen } : null;
    pub.state('core.info', coreInfo(F, CORE_VERSION));
    pub.state('gateway.current', gatewayCurrent(F, D));
    pub.state('protocols.mix', protocolsMix(F, D, at));
    pub.state('exit.identity', F.exit);
    pub.state('traffic.tick', { t_ms: F.traffic.at, up_bps: F.traffic.rateUp, down_bps: F.traffic.rateDown,
      session: { up_bytes: Math.round(F.traffic.up), down_bytes: Math.round(F.traffic.down) }, running: F.run === 'running', error_code: null });
    F.snapRev += 1;
    const ss = serviceState(F, at);
    const key = [ss.runtime.startup_phase, ss.intent.stop_requested, ss.readiness.validation_active].join('/');
    pub.state('service.state', ss, { edge: key !== sync.key, force: tick });
    sync.key = key;
    pub.state('logs.feed', logsFeed(F, at));
    pub.state('update.state', F.update);
    pub.state('geo.status', { running: F.geo.running, updated_at_ms: F.geo.updated_at_ms, errors: F.geo.errors });
    pub.state('alerts.history', { items: F.alerts.items, last_seq: F.alerts.lastSeq });
    pub.state('alerts.delivery', { items: F.alerts.deliveries });
    pub.state('alerts.onboarding', { sessions: [] });
  }
  sync.key = null;

  // Settle the facts into frames and deliver them now. A delivery runs the pages' listeners,
  // which may call back in (a page writing a setting): that is folded into the same loop.
  function commit(opts = {}) {
    if (!inited) return;
    if (flushing) { again = { tick: !!((again && again.tick) || opts.tick), lists: (again ? again.lists : false) || opts.lists !== false }; return; }
    flushing = true;
    try {
      let o = opts;
      for (let i = 0; o && i < COMMIT_ROUNDS; i++) {
        again = null;
        sync(o);
        const out = pub.take();
        if (push) for (const f of out) push(f);
        o = again;
      }
      // Still asked for another round: the listeners keep writing back. The facts are kept and
      // the next commit publishes them; the skipped round is recorded (T.rt.diag()).
      if (o) { again = null; if (diag) diag({ kind: 'sim_commit_overflow', rounds: COMMIT_ROUNDS }); }
    } finally { flushing = false; }
  }

  /* ---- logs: a prototype record per event; frames.js makes the v1 row ---- */
  function log(level, category, title, detail, source) {
    const ts = now();
    const rec = { id: 'L' + (++logN), ts, level, source: source || 'Tesseract', category, title, detail: detail || '',
      raw: `${T.fmt.time(ts)} ${level.toUpperCase().padEnd(5)} [${category}] ${title}${detail ? ' · ' + detail : ''}` };
    // the run a live line belongs to: the runtime generation, as logs.feed.run_id says it (frames.js)
    const row = simRecordToLog(rec, ++logSeq, F, `run-${F.gen}`);
    legacy.set(row.seq, rec);
    pub.log([row]);
    return rec;
  }
  // Transitional (until T-P4-05c): the logs page still takes sim:log records and T.sim.logs.
  // They follow the delivered logs.batch frames, so they ride the same projection path; the
  // replayed tail (instant) fills T.sim.logs without an event, like the prototype's boot seed.
  function onLogs(e) {
    const recs = e && e.data && Array.isArray(e.data.records) ? e.data.records : [];
    for (const r of recs) {
      const s = legacy.get(r.seq);
      if (!s) continue;
      legacy.delete(r.seq);
      logs.push(s); if (logs.length > LOG_KEEP) logs.shift();
      if (!e.instant) T.emit('sim:log', s);
    }
  }
  const ROUTINE = [
    () => ['info', 'health', '健康检查通过', `Vanguard · 香港 IEPL 03 · ${curLat('vanguard')} ms`, 'Tesseract'],
    () => ['info', 'health', '健康检查通过', `Gateway · 东京 Gateway 01 · ${curLat('gateway')} ms`, 'Tesseract'],
    () => ['info', 'traffic', '流量采样正常', `↓ ${T.fmt.rate(F.traffic.rateDown)} · ↑ ${T.fmt.rate(F.traffic.rateUp)}`, 'Mihomo'],
    () => ['info', 'health', '复检通过', 'Sentinel · 首尔 01 · 49 ms', 'Tesseract'],
    () => ['info', 'dns', 'DNS 解析正常', '加密 DNS · 平均 24 ms', 'Mihomo'],
    () => ['warn', 'health', '节点暂不可用', 'Sentinel · 法兰克福 04 · 连接超时，已暂停使用', 'Tesseract'],
  ];
  function curLat(tier) { const n = (D.nodes[tier] || []).find(x => x.current) || D.nodes[tier][0]; return n && n.lat != null ? Math.round(n.lat) : '—'; }

  /* ---- jobs (PROTOCOL §6): queued → running → done / failed / cancelled / superseded ---- */
  function newJob(name, p) {
    const j = { op: `op-${++opSeq}`, req_id: p && p.req_id, owner: p && p.owner, name, started: null };
    jobs.set(j.op, j);
    return j;
  }
  function jobEdge(j, state, extra = {}, opt = {}) {
    if (!j || !jobs.has(j.op)) return;
    if (state === 'running') j.started = now();
    j.state = state;
    const dto = { op: j.op, name: j.name, state, started_at_ms: j.started, progress: null, result: null, error: null, ...extra };
    if (j.req_id) dto.req_id = j.req_id;
    if (j.owner) dto.owner = j.owner;
    pub.job(dto, opt);
    if (['done', 'failed', 'cancelled', 'superseded'].includes(state)) jobs.delete(j.op);
  }
  const begin = (j, opt) => { jobEdge(j, 'queued', {}, opt); jobEdge(j, 'running', {}, opt); };
  const done = (j, result, opt) => jobEdge(j, 'done', { result }, opt);
  const failed = (j, error, result = null, opt) => jobEdge(j, 'failed', { error, result }, opt);
  // A job merged into the start / stop / restart under way (PROTOCOL §6, the sidecar's own
  // semantics): accepted, running, and it ends with the job it joined (the same terminal and
  // result), so every accepted job still reaches a terminal.
  function join(kind, j) { begin(j); joined[kind].push(j); }
  function endJoined(kind, state, extra = {}) {
    const list = joined[kind];
    joined[kind] = [];
    for (const j of list) jobEdge(j, state, extra);
  }
  // PROTOCOL §15: a bridge write that may change the saved configuration (mode.set,
  // gateway.select, subscriptions.refresh stored) moves the rev and sends config.changed,
  // before its job ends; one section per write, as bridge_wiring._config_written does.
  function configWritten(family, files, r) {
    F.configRev += 1;
    const applied = r.applied === true;
    const error = r.apply_error || r.error || null;
    const effect = r.restarted === true ? 'restart' : applied ? 'reload' : 'none';
    pub.event('config.changed', { rev: F.configRev, files, sections: [{ section: family, saved: true, applied, effect, error }],
      pending: null, external_detection: 'bridge_writes_only' });
  }
  const modeDone = (j, r) => { if (r.changed) configWritten('network', ['local'], r); done(j, r); };

  /* ---- tier / pool facts ---- */
  function restoreNodes() {
    Object.values(D.nodes).flat().forEach(n => { if (n.id === 's-fra04') { n.state = 'down'; n.lat = null; } else { n.state = 'ok'; n.lat = n.base; } });
  }

  /* ---- Bastion's automatic pool (3B): fills while the service runs (prototype rules) ---- */
  const BP = F.bp;
  function bpFill(goal, step0, then) {
    clearTimeout(BP.timer);
    BP.phase = 'filling';
    const step = () => {
      BP.ok = Math.min(goal, BP.ok + (step0 || 2 + Math.floor(rnd() * 3)));
      if (BP.ok < goal) BP.timer = setTimeout(step, 360);
      else { BP.phase = 'ready'; BP.rotAt = now(); if (then) then(); }
      commit({ lists: false });
    };
    BP.timer = setTimeout(step, 420);
  }
  function bpSync(instant) {
    clearTimeout(BP.timer);
    if (!BP.enabled) { BP.phase = 'off'; BP.ok = 0; }
    else if (F.run !== 'running') { BP.phase = 'idle'; BP.ok = 0; }
    else if (instant) { BP.ok = fillTo(); BP.phase = 'ready'; BP.rotAt = now() - 12 * 60000; }
    else bpFill(fillTo());
  }
  const bastion = {
    get state() { return S.get('bastion'); },
    fillTo,
    enable(on) {
      on = !!on;
      D.tier('bastion').enabled = on;
      if (BP.enabled === on) return;
      BP.enabled = on; BP.ok = 0;
      bpSync(false);
      commit();
    },
    setMin(v) {
      v = Math.round(+v);
      if (!(v >= 1 && v <= BP.capacity) || v === BP.min) return false;
      BP.min = v;
      // raised above what the pool holds: it refills at once, as its hint promises
      if (BP.enabled && F.run === 'running' && BP.phase === 'ready' && BP.ok < BP.min) bpFill(fillTo());
      commit({ lists: false });
      return true;
    },
    refill() {
      if (F.run !== 'running' || !BP.enabled) return Promise.reject(new Error('服务未启动'));
      return new Promise(res => bpFill(Math.min(BP.capacity, BP.ok + 3), 1, () => res(true)));
    },
  };
  function bpTick() {
    if (!BP.enabled || BP.phase !== 'ready' || tickN % 7) return;
    const f = fillTo(), next = T.util.clamp(BP.ok + (rnd() < 0.5 ? -1 : 1), Math.max(BP.min, f - 3), Math.min(BP.capacity, f + 3));
    if (next !== BP.ok) BP.ok = next;
  }

  function logKs() {
    const k = killswitchState(F);
    // armed = enabled, admin, no error, not triggered (PROTOCOL §11; `active` means triggered)
    if (F.run === 'running' && k.enabled && k.admin && !k.error && !k.triggered) log('info', 'killswitch', '熔断保护已布防', '所有层级都不可用时阻断出站');
    else if (k.enabled && !k.admin) log('warn', 'killswitch', '熔断保护没有生效', '需要管理员权限才能写入 Windows 防火墙规则');
  }

  /* ---- state machine ---- */
  function ticking(on) { if (on && !tickT) tickT = setInterval(tick, 1000); else if (!on && tickT) { clearInterval(tickT); tickT = 0; } }
  function setRun(run, instant) {
    F.run = run;
    ticking(run === 'running');
    bpSync(!!instant);
  }
  // the service stops under a running check: the manual checks waiting on it end not_running
  // (the prototype resolved them false)
  function endExitCheck() {
    clearTimeout(exitTimer);
    const c = exitCheck; exitCheck = null;
    if (!c) return;
    for (const j of c.waiters) failed(j, err('not_running'));
  }

  function start(j) {
    if (F.run === 'running' || F.run === 'starting') return false;
    clearTimers(); clearScenario(); endExitCheck();
    startJob = j || null;
    if (startJob) begin(startJob);
    F.gen += 1; F.lifeRev += 1; F.step = 0; F.stopRequested = false; F.carrier = null; F.order = ORDER.slice();
    F.collect = { vanguard: 'running', sentinel: 'running', bastion: 'running', gateway: 'running' }; F.collectFacts = {};
    F.uptime = 0; F.exit = idleExit();
    restoreNodes();
    F.traffic = { down: 0, up: 0, rateDown: 0, rateUp: 0, at: now() };
    setRun('starting');
    log('info', 'runtime', '正在启动服务', '收集节点 1/3');
    commit();
    Object.keys(COLLECT_AT).forEach(k => later(() => {
      const t = summary().tiers[k];
      F.collect[k] = 'complete'; F.collectFacts[k] = { source_count: k === 'sentinel' ? 1 : 2, healthy: t.usable, required: 1 };
      if (k !== 'bastion') log('info', 'source', '节点来源刷新完成', `${D.tier(k).name} · ${t.usable}/${t.total} 可用`);
      commit();
    }, COLLECT_AT[k]));
    later(() => {
      F.step = 1; F.coreRunning = true; F.coreGen += 1; F.pid = 7300 + F.coreGen;
      log('info', 'exit', '正在验证最终出口', '东京 Gateway 01');
      commit();
    }, PH[0].dur);
    later(() => {
      const t = summary().tiers.gateway;
      F.collect.gateway = 'complete'; F.collectFacts.gateway = { source_count: 1, healthy: t.usable, required: 1 };
      F.step = 2;
      log('info', 'exit', '代理出口已可用', '东京 Gateway 01 · 3/3');
      commit();
    }, PH[0].dur + PH[1].dur);
    later(finishStart, PH[0].dur + PH[1].dur + PH[2].dur);
    return true;
  }
  function finishStart() {
    clearTimers();
    F.step = -1; F.carrier = 'vanguard'; F.pubRev += 1; F.routeEpoch += 1;
    for (const k of TIER_IDS) F.collect[k] = 'complete';
    setRun('running');
    const started = { startup_phase: 'running', failure_code: null };
    if (startJob) { done(startJob, started); startJob = null; }
    endJoined('start', 'done', { result: started });
    log('info', 'runtime', '代理核心已启动', '控制端口 127.0.0.1:25500', 'Mihomo');
    const md = F.mode;
    log('info', 'proxy', md === 'system' ? '系统代理已接管' : `${D.modes[md].label}已接管`, md === 'system' ? D.listen : md === 'tun' ? '本机全部流量' : `本机全部流量 · ${D.listen}`);
    logKs();
    commit();
    exitTimer = setTimeout(() => verify('auto'), 900);
  }
  function stop(j) {
    if (F.run === 'stopped' || F.run === 'stopping') return false;
    // a stop (or a cancel) is already under way: it keeps its job and its timer; a second job
    // that got this far shares that stop and ends with it (the runtime's one stop intent)
    if (stopJob || F.stopRequested) {
      if (j) { if (stopJob) join('stop', j); else { stopJob = j; begin(j); } commit({ lists: false }); }
      return false;
    }
    const cancelling = F.run === 'starting';
    clearTimers(); clearScenario(); endExitCheck();
    stopJob = j || null;
    if (stopJob) begin(stopJob);
    // a mode switch in flight is a setting the user confirmed: it is kept (it applies on the next
    // start) and its job ends done; a restart or a start in flight is superseded by the stop
    const mp = modePend; modePend = null;
    if (restartJob) { jobEdge(restartJob, 'superseded'); restartJob = null; F.restarting = false; }
    endJoined('restart', 'superseded');
    if (startJob) { jobEdge(startJob, 'superseded'); startJob = null; }
    endJoined('start', 'superseded');
    if (mp) {
      F.mode = mp.mode; F.switching = false;
      log('info', 'proxy', '网络模式已切换', `${D.modes[mp.mode].label} · 下次启动时生效`);
      modeDone(mp.job, { mode: mp.mode, changed: true, saved: true, restarted: false, applied: true, apply_error: null });
    }
    // a start is cancelled from where it is (starting + stop_requested: 正在取消); a running
    // service goes through stopping
    if (cancelling) F.stopRequested = true;
    else { F.run = 'stopping'; ticking(false); }
    F.traffic = { ...F.traffic, rateDown: 0, rateUp: 0, at: now() };
    commit();
    const t = { ...F.traffic };
    later(() => {
      F.step = -1; F.stopRequested = false; F.carrier = null; F.order = ORDER.slice(); F.exit = idleExit();
      F.collect = {}; F.collectFacts = {}; F.coreRunning = false; F.pid = 0; F.switching = false;
      restoreNodes();
      setRun('stopped');
      log('info', 'runtime', cancelling ? '已取消启动' : '服务已停止', cancelling ? '' : `本次会话 ↓ ${T.fmt.bytes(t.down)} · ↑ ${T.fmt.bytes(t.up)}`);
      const stopped = { startup_phase: 'stopped', shutdown_exit_code: 0, cleanup_pending: false };
      if (stopJob) { done(stopJob, stopped); stopJob = null; }
      endJoined('stop', 'done', { result: stopped });
      commit();
    }, cancelling ? 380 : 540);
    return true;
  }
  function restart(j) {
    restartJob = j;
    begin(restartJob);
    F.restarting = true;
    log('info', 'runtime', '正在重启代理核心', '流量会短暂中断', 'Mihomo');
    commit();
    later(() => {
      F.restarting = false; F.coreGen += 1; F.pid = 7300 + F.coreGen; F.pubRev += 1; F.routeEpoch += 1;
      log('info', 'runtime', '代理核心已重启', '', 'Mihomo');
      const j = restartJob; restartJob = null;
      const restarted = { applied: true, core_running: true, core_cleanup_pending: false, core_generation: F.coreGen };
      done(j, restarted);
      endJoined('restart', 'done', { result: restarted });
      commit();
      verify('auto');
    }, 1400);
  }

  /* ---- exit verification: a manual check joins the active one (prototype) ---- */
  function verify(how, job) {
    if (F.run !== 'running') { if (job) { failed(job, err('not_running')); commit(); } return; }
    if (exitCheck) { if (job) exitCheck.waiters.push(job); return; }
    const cur = F.exit && F.exit.current;
    const prev = cur && (cur.status === 'ok' || cur.status === 'partial') ? cur : F.exit && F.exit.previous;
    const request_id = `${how}-${++idSeq}`;
    exitCheck = { how, request_id, waiters: job ? [job] : [] };
    F.exit = { current: identity({ request_id, generation: F.coreGen, status: 'running', trigger: how === 'manual' ? 'manual' : 'auto' }), previous: prev || null };
    commit();
    exitTimer = setTimeout(() => {
      const c = exitCheck; exitCheck = null;
      if (!c || F.run !== 'running') return;
      const at = now();
      const result = identity({ request_id: c.request_id, generation: F.coreGen, status: 'ok', at, v4: D.exit.v4.ip, v6: D.exit.v6.ip, binding: `binding-${F.coreGen}-${idSeq}`, trigger: c.how === 'manual' ? 'manual' : 'auto', geo: EXIT_GEO });
      // the job ends before the identity changes: the projection learns the manual request first
      for (const j of c.waiters) done(j, jobResult(result), { before: true });
      F.exit = { current: result, previous: prev || null };
      log('info', 'exit', c.how === 'manual' ? '出口已验证' : '出口已确认', `${D.exit.v4.ip} · ${D.exit.v4.country} · ${D.exit.v4.city}`);
      commit();
    }, how === 'manual' ? 1300 + Math.round(rnd() * 600) : 1100);
  }
  function freshExit() {
    const at = now();
    const cur = F.exit && F.exit.current;
    const prev = cur && cur.status === 'ok' ? cur : F.exit && F.exit.previous;
    F.exit = { current: identity({ request_id: `auto-${++idSeq}`, generation: F.coreGen, status: 'ok', at, v4: D.exit.v4.ip, v6: D.exit.v6.ip, binding: `binding-${F.coreGen}-${idSeq}`, trigger: 'auto', geo: EXIT_GEO }), previous: prev || null };
  }

  function addAlert(severity, event_id, title, detail) {
    const at = now();
    F.alerts.lastSeq += 1;
    F.alerts.items = [...F.alerts.items, { id: `alert-${F.alerts.lastSeq}`, seq: F.alerts.lastSeq, severity, event_id, title, detail, ts_ms: at }].slice(-50);
    F.alerts.deliveries = F.alerts.deliveries.map(d => (d.state === 'healthy' ? { ...d, last_attempt_ms: at, last_success_ms: at } : d));
  }

  /* ---- scenario: Vanguard slows → falls back to Sentinel → recovers (demo only) ---- */
  function transition(from, to) { pub.event('tier.transition', { from, to, observed_at_ms: now(), authority: 'observed' }); }
  let scenario = () => false;
  if (__DEMO__) {
    // The scenarios act on PROXY-FALLBACK's own level (F.carrier). Under a chosen Gateway the
    // runtime reports the Gateway and none of this is visible (T-P4-H2 item 3): no tier.transition,
    // no fallback, as in the app; cancel the Gateway (gateway.select '') to see the tier again.
    // Under a chosen Gateway the tier lines and alerts ("traffic is carried by Sentinel",
    // "fell back to Sentinel", "Vanguard recovered") are not written either: the traffic goes
    // through the Gateway, and the logs / alerts pages must not contradict the overview
    // (T-P4-H2 fix round 1). The node-level health line (Vanguard latency rose) stays.
    const underGw = () => !!chosenGateway(D);
    scenario = function (name) {
      const s = tiersSummary(F, D, { underGateway: true });
      if (name === 'fallback') {
        if (F.run !== 'running' || s.fallback || s.degraded) return false;
        const v = D.nodes.vanguard[0];
        v.state = 'slow'; v.lat = 820;
        log('warn', 'health', 'Vanguard 延迟升高', '香港 IEPL 03 · 820 ms，超过上限 800 ms');
        commit();
        later(() => {
          if (F.run !== 'running') return;
          D.nodes.vanguard.forEach(n => { n.state = 'down'; n.lastLat = n.lat || n.base; n.lat = null; });
          F.carrier = 'sentinel'; F.order = FALLBACK_ORDER.slice(); F.pubRev += 1;
          if (!underGw()) {
            log('warn', 'tier', '流量由 Sentinel 承载', 'Vanguard 已全部不可用，流量切换到 Sentinel · 首尔 01。');
            addAlert('warn', 'WARNING', '已回退到 Sentinel', 'Vanguard 已全部不可用，流量切换到 Sentinel · 首尔 01。');
          }
          freshExit();
          commit();
        }, 1400, scenTimers);
        return true;
      }
      if (name === 'recover') {
        if (F.run !== 'running' || (!s.fallback && !s.degraded)) return false;
        clearScenario();
        // recovering while only slow (before the fallback happened) is not a switch back
        const wasFallback = s.fallback;
        D.nodes.vanguard.forEach(n => { n.state = 'ok'; n.lat = n.base; });
        F.carrier = 'vanguard'; F.order = ORDER.slice(); F.pubRev += 1;
        if (underGw()) { /* the Gateway carried throughout: no tier line, no alert */ } else if (wasFallback) {
          log('info', 'tier', 'Vanguard 已恢复', '流量已切回 香港 IEPL 03');
          addAlert('info', 'RECOVERY', 'Vanguard 已恢复', '流量已切回 香港 IEPL 03');
        } else log('info', 'health', 'Vanguard 延迟已恢复', '香港 IEPL 03 · 延迟回到上限以内，流量一直由它承载');
        freshExit();
        commit();
        return true;
      }
      return false;
    };
  }

  function setMode(mode, job) {
    if (mode === F.mode) { begin(job); modeDone(job, { mode, changed: false, saved: true, restarted: false, applied: true, apply_error: null }); commit(); return; }
    if (D.modes[mode].admin && !F.admin) { begin(job); failed(job, err('admin_required')); commit(); return; }   // 全局 / 混合 need the TUN adapter
    if (F.run !== 'running') {
      begin(job, { before: true });
      F.mode = mode;
      modeDone(job, { mode, changed: true, saved: true, restarted: false, applied: true, apply_error: null });
      commit();
      return;
    }
    begin(job, { before: true });
    F.switching = true;
    log('info', 'proxy', '正在切换网络模式', `${D.modes[F.mode].label} → ${D.modes[mode].label}`);
    commit();
    modePend = { mode, job };
    later(() => {
      modePend = null;
      F.mode = mode; F.switching = false;
      log('info', 'proxy', '网络模式已切换', D.modes[mode].label);
      modeDone(job, { mode, changed: true, saved: true, restarted: true, applied: true, apply_error: null });
      commit();
    }, 1500);
  }

  /* ---- 1 s tick ---- */
  function tick() {
    if (F.run !== 'running') return;
    tickN++;
    F.uptime += 1;
    const t = { ...F.traffic };
    if (rnd() < 0.12) tgtD = MB * (1.05 + rnd() * 0.95);
    let rd = t.rateDown || 1.2 * MB;
    rd = Math.max(24 * KB, rd + (tgtD - rd) * 0.28 + (rnd() - 0.5) * 0.12 * MB);
    if (F.restarting) rd *= 0.15;
    const ru = Math.max(3 * KB, rd * (0.064 + rnd() * 0.012));
    t.rateDown = rd; t.rateUp = ru; t.down += rd; t.up += ru; t.at = now();
    F.traffic = t;
    Object.values(D.nodes).flat().forEach(n => {
      if (n.state !== 'ok' || n.base == null) return;
      n.lat = Math.round(Math.max(n.base * 0.82, Math.min(n.base * 1.35, n.lat + (rnd() - 0.5) * 6 + (n.base - n.lat) * 0.3)));
    });
    bpTick();
    if (tickN % 5 === 0) { const r = ROUTINE[(tickN / 5) % ROUTINE.length](); log(r[0], r[1], r[2], r[3], r[4]); }
    commit({ tick: true, lists: tickN % 5 === 0 });
  }

  /* ---- instant state for demos / screenshots ---- */
  function setStateNow(s) {
    if (s !== 'running' && s !== 'stopped') return;
    clearTimers(); clearScenario(); endExitCheck();
    restoreNodes();
    const sess = D.session;
    F.step = -1; F.stopRequested = false; F.switching = false; F.order = ORDER.slice();
    if (restartJob) { jobEdge(restartJob, 'superseded'); restartJob = null; F.restarting = false; }
    endJoined('restart', 'superseded');
    if (startJob) { jobEdge(startJob, 'superseded'); startJob = null; }
    endJoined('start', 'superseded');
    if (stopJob || joined.stop.length) {
      const stopped = { startup_phase: 'stopped', shutdown_exit_code: 0, cleanup_pending: false };
      if (s === 'stopped') { if (stopJob) done(stopJob, stopped); endJoined('stop', 'done', { result: stopped }); }
      else { if (stopJob) failed(stopJob, err('conflict')); endJoined('stop', 'failed', { error: err('conflict'), result: null }); }
      stopJob = null;
    }
    if (modePend) {
      const mp = modePend; modePend = null; F.mode = mp.mode;
      modeDone(mp.job,{ mode: mp.mode, changed: true, saved: true, restarted: false, applied: true, apply_error: null });
    }
    if (s === 'running') {
      F.carrier = 'vanguard'; F.coreRunning = true; F.pid = 7300 + F.coreGen; F.uptime = sess.uptime;
      for (const k of TIER_IDS) { const t = summary().tiers[k]; F.collect[k] = 'complete'; F.collectFacts[k] = { source_count: 1, healthy: t.usable, required: 1 }; }
      const at = now() - 3 * 60000;
      F.exit = { current: identity({ request_id: `manual-${++idSeq}`, generation: F.coreGen, status: 'ok', at, v4: D.exit.v4.ip, v6: D.exit.v6.ip, binding: `binding-${F.coreGen}-${idSeq}`, trigger: 'auto', geo: EXIT_GEO }), previous: null };
      F.traffic = { down: sess.down, up: sess.up, rateDown: sess.rateDown, rateUp: sess.rateUp, at: now() };
    } else {
      F.carrier = null; F.coreRunning = false; F.pid = 0; F.collect = {}; F.collectFacts = {}; F.exit = idleExit();
      F.traffic = { down: sess.down, up: sess.up, rateDown: 0, rateUp: 0, at: now() };
    }
    setRun(s, true);
  }

  /* ---- boot (idempotent) ---- */
  function init() {
    if (inited) return;
    inited = true;
    // the boot state goes out with the first replay (hello first), not as live frames
    const attached = push;
    push = null;
    try { boot(); } finally { push = attached; }
  }
  function boot() {
    BOOT = now();
    Object.values(D.nodes).flat().forEach(n => { n.base = n.lat != null ? n.lat : n.lastLat; });
    const DF = D.defaults;
    BP.enabled = !!D.tier('bastion').enabled; BP.capacity = DF.bastionPool.size; BP.min = DF.bastionPool.min;
    F.admin = !!DF.admin; F.ksEnabled = DF.killswitch !== false;
    // settings the prototype pages still write into the store themselves (配置's demo link,
    // switches and the latency slider): the simulator adopts them as its facts and publishes
    // the consequences (network.mode, killswitch.state, tiers.summary)
    S.sub('admin', v => { if (v !== undefined && !!v !== F.admin) { F.admin = !!v; commit({ lists: false }); } });
    S.sub('ksEnabled', v => { if (v !== undefined && (v !== false) !== F.ksEnabled) { F.ksEnabled = v !== false; commit({ lists: false }); } });
    S.sub('latCeiling', v => { const n = Math.round(+v); if (n > 0 && n !== F.latCeiling) { F.latCeiling = n; commit(); } });
    T.on('rt:logs', onLogs);
    history = D.seedHistory(BOOT);
    pub.state('traffic.history', { sampled_at_ms: BOOT, source: 'temporary_snapshot',
      h24: history.h24.map(b => ({ t_ms: Math.round(b.t), down_bps: b.d, up_bps: b.u, sample_count: Math.round(b.n) })),
      live: history.live.map(p => ({ t_ms: Math.round(p.t), down_bps: p.d, up_bps: p.u })) });
    const al = seedAlerts(BOOT, D.session.uptime);
    F.alerts = { items: al.items, deliveries: al.deliveries, lastSeq: al.items.length };
    F.geo.updated_at_ms = BOOT - 26 * 3600 * 1000;
    setStateNow('running');
    // the log history: the logs page's seed (two runs) and the simulator's own boot lines, in
    // time order; only the latter are sim:log records (the page seeds the former itself)
    const ks = killswitchState(F);
    const boot = [['info', 'runtime', '代理核心已启动', '控制端口 127.0.0.1:25500', 'Mihomo', 8053],
      ['info', 'source', '节点来源刷新完成', 'Vanguard · 2/2 可用', 'Tesseract', 8052],
      ['info', 'source', '节点来源刷新完成', 'Sentinel · 4/5 可用', 'Tesseract', 8051],
      ['info', 'exit', '代理出口已可用', '东京 Gateway 01 · 3/3', 'Tesseract', 8050],
      F.run === 'running' && ks.enabled && ks.admin && !ks.error && !ks.triggered ? ['info', 'killswitch', '熔断保护已布防', '所有层级都不可用时阻断出站', 'Tesseract', 8049]
        : ks.enabled && !ks.admin ? ['warn', 'killswitch', '熔断保护没有生效', '需要管理员权限才能写入 Windows 防火墙规则', 'Tesseract', 8049] : null,
      ['info', 'exit', '出口已验证', '203.0.113.24 · 日本 · 东京', 'Tesseract', 180]].filter(Boolean).map(([lv, cat, title, detail, src, ago]) => ({
      sim: { id: 'L' + (++logN), ts: BOOT - ago * 1000, level: lv, source: src, category: cat, title, detail,
        raw: `${T.fmt.time(BOOT - ago * 1000)} ${lv.toUpperCase().padEnd(5)} [${cat}] ${title} · ${detail}` } }));
    const page = seedLogRecords(T, D, BOOT).map(r => ({ page: r }));
    const all = [...page, ...boot].sort((a, b) => (a.page || a.sim).ts - (b.page || b.sim).ts);
    // run_id: each seed run starts at its start record (its generation); the lines before the
    // first start have none (PROTOCOL §5.5)
    let runId = null;
    pub.log(all.map(x => {
      const seq = ++logSeq;
      if (x.page && x.page.runStart && Number.isInteger(x.page.gen)) runId = `run-${x.page.gen}`;
      if (x.sim) { legacy.set(seq, x.sim); return simRecordToLog(x.sim, seq, F, runId); }
      return pageRecordToLog(x.page, seq, runId);
    }));
    commit();
  }

  /* ---- commands and queries arriving through FakeTransport (sim mode) ---- */
  // `id` echoes the shell's request id (PROTOCOL §2): FakeTransport hands the id it returned
  // from invoke() in the payload; a payload without one (a direct backend.invoke) gets our own
  const idOf = p => (Number.isInteger(p.id) && p.id > 0 ? p.id : ++shellId);
  const ACK = (p, extra) => ({ ...pub.env('ack'), id: idOf(p), ...(p.req_id ? { req_id: p.req_id } : {}), ...(p.owner ? { owner: p.owner } : {}), ...extra });
  const RES = (p, ok, body) => ({ ...pub.env('res'), id: idOf(p), req_id: p.req_id, owner: p.owner, cmd: p.cmd, ok, ...(ok ? { data: body } : { error: body }) });
  const ERR = (p, error) => ({ ...pub.env('error'), id: idOf(p), ...(p.req_id ? { req_id: p.req_id } : {}), ...(p.owner ? { owner: p.owner } : {}), error });
  // a refused ack as bridge_main.pump writes it: skipped_reason plus an Error whose message_key
  // is errors.bridge.<code> (every code used here has one in the zh_CN catalog)
  const refuse = (p, code) => ACK(p, { accepted: false, skipped_reason: code, error: { code, message_key: `errors.bridge.${code}`, params: {} } });
  const refuseErr = (p, error) => ACK(p, { accepted: false, error });

  // geo.update downloads, one after another. A job that finds the service stopped when its
  // turn ends fails needs_route; the previous files stay (§15: no exit -> needs_route).
  function nextGeo() {
    if (geoBusy || !geoQ.length) return;
    const j = geoQ.shift();
    geoBusy = j;
    work(() => {
      geoBusy = null;
      const more = geoQ.length > 0;
      if (F.run !== 'running') {
        F.geo = { ...F.geo, running: more };
        failed(j, err('needs_route'));
      } else {
        F.geo = { running: more, updated_at_ms: now(), errors: [] };
        // this download completed: its status reads running:false (§15 done invariant)
        done(j, { triggered: true, completed: true, status: { ...F.geo, running: false } });
      }
      commit({ lists: false });
      nextGeo();
    }, 1200);
  }
  // subscriptions.refresh, one at a time (the serial mutation domain); the next queued job
  // starts running when the one before it ended. An unknown item fails as the sidecar's does
  // (conflict, reason_code unknown_item, with the idle RefreshResult).
  function nextRefresh() {
    if (refreshBusy) return;
    let j = refreshQ.shift();
    while (j && !jobs.has(j.op)) j = refreshQ.shift();
    if (!j) return;
    jobEdge(j, 'running');
    const sub = (D.subscriptions || []).find(s => s.id === j.item);
    if (!sub) {
      const idle = { total: 0, healthy: 0, stored: false, applied: false, retained_previous: true, apply_error: null, error: null };
      const e = err('conflict', 'errors.subscriptions.unknown_item', { reason_code: 'unknown_item' });
      failed(j, e, { ...idle, error: e });
      commit({ lists: false });
      nextRefresh();
      return;
    }
    refreshBusy = j;
    F.refreshing.add(sub.id);
    commit();
    work(() => {
      refreshBusy = null;
      F.refreshing.delete(sub.id);
      const row = subscriptionsList(F, D, sub.tier).find(r => r.id === sub.id);
      const r = { total: row.total, healthy: row.healthy, stored: true, applied: true, retained_previous: false, apply_error: null, error: null };
      configWritten('subscriptions', [], r);
      done(j, r);
      commit();
      nextRefresh();
    }, 1500);
  }

  // A job command: the ack (with its op) goes out first, then the action runs and its job
  // frames follow in order (PROTOCOL §6: the ack precedes every side effect).
  function accept(p, name, action) {
    const j = newJob(name, p);
    if (push) push(ACK(p, { accepted: true, op: j.op }));
    action(j);
    return j.op;
  }

  const JOB_COMMANDS = {
    'service.start'(p) {
      // a stop is pending (stopping, or a start being cancelled): the executor refuses a
      // lifecycle change (bridge_jobs: service.stop in the control queue -> lifecycle_busy)
      if (F.run === 'stopping' || F.stopRequested || stopJob) return push(refuse(p, 'lifecycle_busy'));
      // already starting: merged into the start under way; running: DesktopRuntime.start
      // returns its status, the job is done at once
      if (F.run === 'starting') return accept(p, 'service.start', j => { if (startJob) join('start', j); else { startJob = j; begin(j); } commit({ lists: false }); });
      if (F.run === 'running') return accept(p, 'service.start', j => { begin(j); done(j, { startup_phase: 'running', failure_code: null }); commit({ lists: false }); });
      accept(p, 'service.start', j => start(j));
    },
    'service.stop'(p) {
      const g = p.args && p.args.expected_generation;
      if (Number.isInteger(g) && g !== F.gen) return push(refuseErr(p, err('conflict')));
      // a cancel (starting + stop_requested) or a stop under way: the second stop shares the one
      // stop intent (DesktopRuntime.request_stop) and ends with the first
      if (F.run === 'stopping' || F.stopRequested || stopJob) {
        return accept(p, 'service.stop', j => { if (stopJob) join('stop', j); else { stopJob = j; begin(j); } commit({ lists: false }); });
      }
      // already stopped: the stop is accepted and has nothing to do
      if (F.run === 'stopped') return accept(p, 'service.stop', j => { begin(j); done(j, { startup_phase: 'stopped', shutdown_exit_code: 0, cleanup_pending: false }); commit({ lists: false }); });
      accept(p, 'service.stop', j => stop(j));
    },
    'service.restart'(p) {
      if (F.run !== 'running') return push(refuse(p, 'not_running'));
      // a restart under way: merged into it
      if (restartJob) return accept(p, 'service.restart', j => { join('restart', j); commit({ lists: false }); });
      accept(p, 'service.restart', j => restart(j));
    },
    'mode.set'(p) {
      const mode = p.args && p.args.mode;
      if (!D.modes[mode]) return push(refuseErr(p, err('invalid_enum', null, { field: 'mode' })));
      if (modePend) return push(refuseErr(p, err('conflict')));
      accept(p, 'mode.set', j => setMode(mode, j));
    },
    'exit.verify'(p) {
      accept(p, 'exit.verify', j => { begin(j); commit({ lists: false }); verify('manual', j); });
    },
    'gateway.select'(p) {
      accept(p, 'gateway.select', j => {
        begin(j);
        const name = p.args && p.args.proxy_name;
        // proxy_name '' turns Gateway off (PROTOCOL section 15, T-P4-C2): FINAL-EXIT back to PROXY-FALLBACK
        const off = name === '';
        const n = off ? null : (D.nodes.gateway || []).find(x => x.name === name && !x.draft);
        if (!off && !n) { failed(j, err('validation', null, { field: 'proxy_name' })); commit(); return; }
        D.nodes.gateway.forEach(x => { x.current = x === n; });
        F.pubRev += 1;
        const r = { selected: name, applied: true, apply_error: null, selection: off ? null : name, pending: null };
        configWritten('gateway', ['local'], r);
        done(j, r);
        commit();
      });
    },
    'settings.apply'(p) {
      accept(p, 'settings.apply', j => {
        begin(j);
        const changes = (p.args && Array.isArray(p.args.changes)) ? p.args.changes : [];
        if (!changes.length) { failed(j, err('validation')); commit(); return; }
        // only the ten ConfigValues sections (§16) exist; a page sending another one is a page bug
        const known = new Set(Object.keys(configValues(F, S.get('lang') || 'zh_CN')));
        if (changes.some(c => !c || !known.has(c.section))) { failed(j, err('validation', null, { field: 'section', hold: true })); commit(); return; }
        if (p.args.base_rev !== F.configRev) { failed(j, err('conflict', null, { hold: true })); commit(); return; }
        // the design target only answers: the rev moves and config.changed says so; the values
        // config.snapshot returns stay the simulator's own (T-P4-02b requirement 2)
        F.configRev += 1;
        const sections = changes.map(c => ({ section: String(c.section), saved: true, applied: true, effect: 'none', error: null }));
        pub.event('config.changed', { rev: F.configRev, files: ['local'], sections, pending: null, external_detection: 'bridge_writes_only' });
        done(j, { rev: F.configRev, sections, pending: null });
        commit({ lists: false });
      });
    },
    'app.update.check'(p) {
      accept(p, 'app.update.check', j => {
        begin(j);
        // a check in flight: this job shares it and ends with its result (UpdateOwner.check)
        if (updateCheck) { updateCheck.waiters.push(j); commit({ lists: false }); return; }
        updateCheck = { waiters: [j] };
        F.update = { ...F.update, state: 'checking' };
        commit({ lists: false });
        work(() => {
          const c = updateCheck; updateCheck = null;
          F.update = { state: 'idle', version: null, update_available: false, installable: false, error: null };
          for (const w of c.waiters) done(w, { ...F.update });
          commit({ lists: false });
        }, 900);
      });
    },
    'geo.update'(p) {
      accept(p, 'geo.update', j => {
        begin(j);
        if (F.run !== 'running') { failed(j, err('needs_route')); commit({ lists: false }); return; }
        // a download in progress: this one runs after it (the updater's action lock), as its
        // own forced update; the job is running meanwhile (a normal-domain worker waits)
        geoQ.push(j);
        F.geo = { ...F.geo, running: true };
        commit({ lists: false });
        nextGeo();
      });
    },
    'subscriptions.refresh'(p) {
      accept(p, 'subscriptions.refresh', j => {
        // the serial mutation domain: queued until the refresh before it ended
        jobEdge(j, 'queued');
        j.item = String((p.args && p.args.item_id) || '').replace(/^cfg-/, '');
        refreshQ.push(j);
        commit({ lists: false });
        nextRefresh();
      });
    },
  };

  const RES_COMMANDS = {
    'logs.setMihomoLevel'(p) {
      const level = p.args && p.args.level;
      if (level !== 'info' && level !== 'debug') return RES(p, false, err('invalid_enum', null, { field: 'level' }));
      if (F.run !== 'running') return RES(p, false, err('not_running'));
      F.logLevel = level;
      commit({ lists: false });
      return RES(p, true, { level, applied: true });
    },
  };

  // LogPage rows: a raw line's key is "log.raw" in the history and in logs.batch alike (T-P4-C1
  // item 2; frames.js pageRecordToLog). The mapping below stays for a row of an older shape.
  function logPage(rows, n) {
    const list = rows.slice(-n).map(r => (r.key ? r : { ...r, key: 'log.raw' }));
    const ring = pub.logs();
    return { records: list, watermark: ring.length ? ring[ring.length - 1].seq : 0, next_cursor: null, gap: false, dropped: 0 };
  }
  const QUERIES = {
    'config.snapshot': () => ({ rev: F.configRev, values: configValues(F, S.get('lang') || 'zh_CN'), secret_fields_set: SECRET_FIELDS_SET.slice(), read_errors: [], pending: null }),
    'app.info': () => ({ version: D.version, language: S.get('lang') || 'zh_CN', caps: CAPS.commands.slice(),
      listeners: LISTENERS.map(l => ({ ...l, available: F.run === 'running' ? true : null })) }),
    'alerts.catalog': () => ({ ...JSON.parse(JSON.stringify(CATALOG.alerts)), acl: { plaintext_storage: true, access_state: 'no_known_failures', unconfirmed_count: 0 } }),
    'import.specs': () => JSON.parse(JSON.stringify(CATALOG.import)),
    'logs.tail': a => {
      const since = Number.isInteger(a.since_seq) ? a.since_seq : 0;
      const ring = pub.logs();
      const page = logPage(ring.filter(r => r.seq > since), Math.max(1, Math.min(4000, a.n | 0 || 4000)));
      page.gap = !!(since && ring.length && ring[0].seq > since + 1);
      return page;
    },
    'logs.query': a => {
      const f = a.filter || {};
      const text = typeof f.text === 'string' ? f.text.toLowerCase() : '';
      const rows = pub.logs().filter(r => r.seq < (a.before_seq || Infinity)
        && (!f.levels || f.levels.includes(r.level)) && (!f.sources || f.sources.includes(r.source))
        && (!f.categories || f.categories.includes(r.category))
        && (!text || `${r.title}\n${r.detail}\n${r.raw}`.toLowerCase().includes(text)));
      return logPage(rows, Math.max(1, Math.min(4000, a.n | 0 || 4000)));
    },
    'nodes.detail': a => {
      for (const tier of TIER_IDS) {
        const n = (D.nodes[tier] || []).find(x => x.id === a.node_id && !x.draft);
        if (!n) continue;
        const item = configItems(D).find(c => c.id === `cfg-${n.id}`) || null;
        return { node: nodeRow(n, tier, F.latCeiling), config_item: item, last_diagnostic: null, check_interval_s: 60 };
      }
      return { __error: err('validation', null, { field: 'node_id' }) };
    },
    'autostart.state': () => ({ supported: true, enabled: false, target: 'none', mismatch: false, error: null }),
    'geo.status': () => ({ running: F.geo.running, updated_at_ms: F.geo.updated_at_ms, errors: F.geo.errors.slice() }),
    'alerts.deliveries': a => {
      const limit = Math.max(1, Math.min(200, a.limit | 0 || 200));
      const items = F.alerts.deliveries.filter(d => !a.channel_id || d.channel_id === a.channel_id).slice(0, limit);
      return { items: JSON.parse(JSON.stringify(items)), next_cursor: null };
    },
  };

  function cancel(p) {
    const op = p.args && p.args.op;
    const j = typeof op === 'string' ? jobs.get(op) : null;
    // the executor's answers (bridge_jobs.cancel): an unknown or ended op is not_running; a job
    // still queued (not started) is cancelled whatever its command; exit.verify revokes its
    // ownership; anything else running is not_cancellable
    if (!j) return push(refuse(p, 'not_running'));
    if (j.state === 'queued') {
      push(ACK(p, { accepted: true, op: j.op }));
      refreshQ = refreshQ.filter(x => x !== j);
      jobEdge(j, 'cancelled');
      commit({ lists: false });
      return;
    }
    if (j.name !== 'exit.verify') return push(refuse(p, 'not_cancellable'));
    // exit.verify cancels its ownership only (PROTOCOL §10 取消列 owner): the check goes on
    push(ACK(p, { accepted: true }));
    if (exitCheck) exitCheck.waiters = exitCheck.waiters.filter(w => w !== j);
    jobEdge(j, 'cancelled');
    commit({ lists: false });
  }

  // FakeTransport's sim port: invoke(payload) answers like the sidecar behind the shell.
  function invoke(p) {
    const cmd = p && p.cmd;
    const args = (p && p.args) || {};
    const q = { ...p, args };
    if (cmd === 'cancel') { cancel(q); return; }
    if (JOB_COMMANDS[cmd]) { JOB_COMMANDS[cmd](q); return; }
    if (RES_COMMANDS[cmd]) { push(RES_COMMANDS[cmd](q)); return; }
    if (QUERIES[cmd]) {
      const data = QUERIES[cmd](args);
      push(data && data.__error ? RES(q, false, data.__error) : RES(q, true, data));
      return;
    }
    push(ERR(q, err('not_allowed', 'errors.not_allowed')));
  }

  // caps.features: the build's ENABLED_H_FEATURES (a design build with --h-features all opens
  // every B control, architecture §5); the default design build declares none, like the sidecar.
  const CAPS = { commands: [...PAGE_COMMANDS, ...PAGE_QUERIES], topics: [...PAGE_TOPICS], features: features.slice() };
  function hello() {
    return { ...pub.env('hello'), proto: 1, app_version: D.version, runtime_pid: 4242, lang: 'zh_CN', caps: JSON.parse(JSON.stringify(CAPS)),
      scope: { kind: 'custom', id: 'design' },
      network_recovery: { proxy: { state: 'not_needed', error_code: null, remaining_count: 0 }, firewall: { state: 'not_needed', error_code: null, remaining_count: 0 } } };
  }

  const backend = {
    epoch: EPOCH,
    attach(fn, onDiag) { push = fn; diag = typeof onDiag === 'function' ? onDiag : null; },
    detach() { push = null; diag = null; },
    replay() { init(); return pub.replay(hello()); },
    invoke(p) { init(); invoke(p); },
    init,
    setFeatures(list) { CAPS.features = Array.isArray(list) ? list.filter(x => typeof x === 'string') : []; },
  };

  /* ---- T.sim: the prototype API; commands go through T.rt like a page after its 05x ---- */
  const rtCall = (cmd, args) => (T.rt ? T.rt.call(cmd, args || {}) : Promise.reject(new Error('runtime_unavailable')));
  const quiet = (pr, ok = () => true) => Promise.resolve(pr).then(ok, () => false);
  const stopArgs = () => {
    const s = T.rt && T.rt.topic('service.state').get();
    const g = s && s.intent && s.intent.runtime_generation;
    return Number.isInteger(g) ? { expected_generation: g } : {};
  };
  const api = {
    phases: PH,
    get history() { return S.get('history'); },
    logs,
    bastion,
    get state() { return S.get('run'); },
    recount() { commit(); },
    usable,
    init,
    start() { quiet(rtCall('service.start')); },
    stop() { quiet(rtCall('service.stop', stopArgs())); },
    toggle() { const r = S.get('run'); if (r === 'running' || r === 'starting') api.stop(); else if (r === 'stopped') api.start(); },
    restart() { return quiet(rtCall('service.restart'), r => !!(r && r.applied)); },
    verifyExit() { return quiet(rtCall('exit.verify', { trigger: 'manual' }), r => !!r && (r.status === 'ok' || r.status === 'partial')); },
    setMode(mode) {
      if (!D.modes[mode] || mode === S.get('mode')) return Promise.resolve(false);
      return quiet(rtCall('mode.set', { mode }), r => !!(r && r.applied && r.changed));
    },
    setState() {},
    scenario() { return false; },
  };
  if (__DEMO__) {
    api.setState = s => { setStateNow(s); commit(); };
    api.scenario = name => scenario(name);
  }
  return { api, backend, facts: F, phase: () => startupPhase(F) };
}

/* The design boot (boot/rt-design.js) asks for the backend; T.sim is installed on import (not
   started: the fixture mode replaces it with the thin T.sim and never starts it). */
let installed = null;
export function designBackend(T, opts = {}) {
  if (!installed || installed.T !== T) installed = { T, sim: createDesignSim(T, opts) };
  if (opts.features) installed.sim.backend.setFeatures(opts.features);
  T.sim = installed.sim.api;
  return installed.sim.backend;
}
if (typeof window !== 'undefined' && window.T && window.T.data && window.T.store) {
  installed = { T: window.T, sim: createDesignSim(window.T) };
  window.T.sim = installed.sim.api;
}
