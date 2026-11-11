// Flow "list_stale" (Claude supplement 10-13, 8th-wave ruling; 5th-wave ruling 31b-5; PROTOCOL
// section 13 rt:stale; T-P4-H2 item 1: the rt client forwards the shell's rt:stale as
// rt:slot-stale, shellc/banners.js shows the list-stale banner).
//  1. The stand-in says snapshot_required for one keyed list slot (nodes.list / sentinel, an
//     error without id) and refuses the next 3 replacement requests the shell sends: the shell
//     announces rt:stale resync once, then exhausted after the third failure (shell.log rt_stale),
//     the page gets rt:slot-stale {topic, key, reason} for both, and the "may be out of date"
//     banner is up with the exhausted wording (shell.stale.body with exhausted=yes: "automatic
//     retries stopped").
//  2. A same-epoch reconnect (the page's own channel-stuck path, as flows/reconnect.mjs): the
//     shell replays the slot's rt:stale exhausted exactly once, after the replay's snapshots and
//     before its rt:replayed (PROTOCOL section 13; asserted, since the page keeps its stale marks
//     over a same-epoch reconnect and "not doubled" alone would hold without the replay); the
//     banner is in the document at every mutation of #banners, once, with its wording; the shell
//     sends no new replacement request. Recorded only (fix round 1, S1): whether the banner is
//     the same element afterwards. shellc/banners.js slotStale() re-puts the banner on every
//     rt:slot-stale and core.js banner() replaces an element of the same id (new handle, 200 ms
//     opacity animation), so the banner is re-mounted in place: a finding for banners.js
//     (T-P4-06/H2), a hard assertion once that is fixed.
//  3. 「重新加载」 on the banner: the page invokes rt_resync{topic, key} (the shell clears the
//     count and asks again; the stand-in now answers with the snapshot); the complete snapshot
//     reaches the page and the banner goes. The page asked, so the shell announces no new resync.
//     Evidence that rt_resync went out: T.rt.diag() (kind resync, topic, by page, code ok; taken
//     by its clock time, the ring is capped) and the stand-in's 4th snapshot_req for the key (after
//     exhausted only rt_resync makes the shell ask again: transport.rs resync()). The page's
//     window.__TAURI__.core.invoke cannot be wrapped in the real shell, so no invoke spy.
import { bannerInfo, check, sleep, strayReconnect, waitFor } from '../driver.mjs';

export const real = false;
export const skipReal = 'needs the stand-in sidecar (scripted snapshot_required answers to the shell\'s replacement requests)';
export const launches = [{ args: [], env: {}, options: { phase: 'running' } }];

const TOPIC = 'nodes.list', KEY = 'sentinel';
const ID = 'list-stale';

export async function run(s) {
  const c = s.checks;
  await s.reloadObserved();
  await s.until(() => window.__e2e.events.some(e => e.e === 'rt:replayed'), 'replayed', 20000);
  const reqs = () => s.requests().filter(r => r.t === 'snapshot_req' && r.topic === TOPIC && r.key === KEY).length;
  c.snapshot_req_before = reqs();
  check(!(await bannerInfo(s, ID)).up, 'a list-stale banner before the scripted failure');

  // 1. three refused replacements: exhausted
  let mark = await s.eval(() => window.__e2e.events.length);
  await s.control({ op: 'stale', topic: TOPIC, key: KEY, fails: 3 });
  await s.logWait(new RegExp(`^rt_stale topic=${TOPIC.replace('.', '\\.')} key=${KEY} reason=exhausted$`), 'shell.log rt_stale exhausted', 15000);
  c.shell_log = s.logFind(/^rt_stale /).map(l => l.text);
  check(c.shell_log.length === 2 && /reason=resync$/.test(c.shell_log[0]) && /reason=exhausted$/.test(c.shell_log[1]),
    `shell.log rt_stale lines: ${JSON.stringify(c.shell_log)}`);
  await s.until(m => window.__e2e.events.slice(m).some(e => e.e === 'rt:slot-stale' && e.reason === 'exhausted'), 'rt:slot-stale exhausted on the page', 10000, mark);
  c.slot_stale = (await s.eval(m => window.__e2e.events.slice(m).filter(e => e.e === 'rt:slot-stale'), mark)).map(e => ({ topic: e.topic, key: e.key, reason: e.reason }));
  check(c.slot_stale.length === 2 && c.slot_stale.every(e => e.topic === TOPIC && e.key === KEY) && c.slot_stale[0].reason === 'resync' && c.slot_stale[1].reason === 'exhausted',
    `rt:slot-stale on the page: ${JSON.stringify(c.slot_stale)}`);
  c.snapshot_req_refused = reqs() - c.snapshot_req_before;
  check(c.snapshot_req_refused === 3, `the shell sent ${c.snapshot_req_refused} replacement requests (3 expected, then it stops)`);
  await s.until(i => window.T.notices.banners.has(i), 'the list-stale banner', 10000, ID);
  const words = await s.eval(() => ({
    title: window.T.t('shell.stale.title'), yes: window.T.t('shell.stale.body', { count: 1, exhausted: 'yes' }), no: window.T.t('shell.stale.body', { count: 1, exhausted: 'no' }),
    reload: window.T.t('shell.stale.reload'),
  }));
  c.banner = await bannerInfo(s, ID);
  c.words = { yes: words.yes, no: words.no };
  check(c.banner.title === words.title, `banner title ${JSON.stringify(c.banner.title)}`);
  check(c.banner.message === words.yes && words.yes !== words.no, `the banner is not the exhausted wording: ${JSON.stringify(c.banner.message)} (expected ${JSON.stringify(words.yes)})`);
  check(c.banner.elements === 1, `${c.banner.elements} list-stale banners in the document`);
  await s.shot('stale-exhausted');

  // 2. a same-epoch reconnect: the banner stays, once, with its wording; nothing is fetched again
  await s.eval(i => {
    // (removed: list-stale elements taken out of #banners, a replacement in one task included)
    const w = window.__e2eStaleWatch = { min: 9, max: 0, samples: 0, removed: 0, added: 0 };
    window.__e2eStaleHandle = window.T.notices.banners.get(i) || null;
    const isIt = n => n && n.nodeType === 1 && n.matches && n.matches(`.banner[data-id="${i}"]`);
    const sample = recs => {
      for (const r of recs || []) { w.removed += Array.from(r.removedNodes).filter(isIt).length; w.added += Array.from(r.addedNodes).filter(isIt).length; }
      const n = document.querySelectorAll(`#banners .banner[data-id="${i}"]`).length; w.samples++; w.min = Math.min(w.min, n); w.max = Math.max(w.max, n);
    };
    sample();
    w.mo = new MutationObserver(sample);
    w.mo.observe(document.getElementById('banners') || document.body, { childList: true, subtree: true });
  }, ID);
  const before = c.banner;
  mark = await strayReconnect(s);
  await sleep(1500);
  c.reconnect = await s.eval(([m, i]) => {
    const w = window.__e2eStaleWatch; w.mo.disconnect();
    const ev = window.__e2e.events.slice(m.events);
    const reset = ev.findIndex(e => e.e === 'rt:reset');
    const replayed = ev.findIndex((e, n) => n > reset && e.e === 'rt:replayed');
    const stale = ev.map((e, n) => (e.e === 'rt:slot-stale' ? n : -1)).filter(n => n >= 0);
    // (a same-epoch replay's snapshots carry revs the page has: rt/topics.js drops them as dup
    // without a trace, so the page sees no snapshot event. What it does see in order: hello
    // (rt:caps) before the snapshots, the replay's tail host:language / host:visibility after the
    // rt:stale, then rt:replayed: PROTOCOL section 13 rt_connect replay order)
    const inReplay = n => n > reset && (replayed < 0 || n < replayed);
    const hello = ev.findIndex((e, n) => inReplay(n) && e.e === 'rt:caps');
    const tail = ev.map((e, n) => (inReplay(n) && (e.e === 'host:language' || e.e === 'host:visibility') ? n : -1)).filter(n => n >= 0);
    const h = window.__e2eStaleHandle, now = window.T.notices.banners.get(i);
    return { reset: reset >= 0 ? ev[reset] : null, replayed_stale: stale.map(n => ev[n].reason),
      replayed_stale_full: stale.map(n => ({ topic: ev[n].topic, key: ev[n].key, reason: ev[n].reason })),
      order: { reset, hello, stale, tail, replayed }, replay_events: ev.slice(Math.max(0, reset), replayed < 0 ? ev.length : replayed + 1).map(e => e.e),
      watch: { min: w.min, max: w.max, samples: w.samples, removed: w.removed, added: w.added }, banners: window.T.notices.banners.has(i),
      same_banner: !!(h && now === h && h.el && h.el.isConnected) };
  }, [mark, ID]);
  c.reconnect.banner = await bannerInfo(s, ID);
  check(c.reconnect.reset && c.reconnect.reset.reason === 'reconnect' && c.reconnect.reset.generation_changed === false, `rt:reset ${JSON.stringify(c.reconnect.reset)}`);
  // the replay carries the still-stale slot's rt:stale once, with its current reason, after the
  // snapshots and before rt:replayed (PROTOCOL section 13 rt:stale / rt:replayed): on the page,
  // after hello and before the replay's host:language / host:visibility tail
  const o = c.reconnect.order;
  check(JSON.stringify(c.reconnect.replayed_stale_full) === JSON.stringify([{ topic: TOPIC, key: KEY, reason: 'exhausted' }]),
    `the reconnect's replay did not carry rt:stale exhausted exactly once: ${JSON.stringify(c.reconnect.replayed_stale_full)}`);
  check(o.reset >= 0 && o.replayed > o.reset && o.stale[0] > o.reset && o.stale[0] < o.replayed,
    `rt:slot-stale is not inside the replay (rt:reset ${o.reset}, rt:slot-stale ${o.stale}, rt:replayed ${o.replayed})`);
  check(o.hello > o.reset && o.stale[0] > o.hello, `rt:slot-stale is not after the replay's hello (rt:caps ${o.hello}, rt:slot-stale ${o.stale[0]})`);
  check(o.tail.length > 0 && o.tail.every(n => n > o.stale[0]),
    `rt:slot-stale is not before the replay's host:language / host:visibility tail (tail ${JSON.stringify(o.tail)}, rt:slot-stale ${o.stale[0]}): ${JSON.stringify(c.reconnect.replay_events)}`);
  // (S1, recorded only: a re-mount in place keeps the count at 1 but shows as removed/added)
  c.reconnect.remounted = !c.reconnect.same_banner || c.reconnect.watch.removed > 0;
  check(c.reconnect.banner.up && c.reconnect.banner.connected, 'the list-stale banner was withdrawn by the same-epoch reconnect');
  check(c.reconnect.watch.min >= 1, 'the list-stale banner left the document during the reconnect');
  check(c.reconnect.watch.max <= 1 && c.reconnect.banner.elements === 1, `the list-stale banner is doubled (max ${c.reconnect.watch.max}, now ${c.reconnect.banner.elements})`);
  check(c.reconnect.banner.message === before.message, `the banner's wording changed: ${JSON.stringify(c.reconnect.banner.message)}`);
  c.snapshot_req_after_reconnect = reqs() - c.snapshot_req_before;
  check(c.snapshot_req_after_reconnect === 3, `the reconnect made the shell ask again (${c.snapshot_req_after_reconnect} requests)`);
  await s.shot('stale-after-reconnect');

  // 3. 「重新加载」: rt_resync, the snapshot, the banner goes
  mark = await s.eval(() => window.__e2e.events.length);
  // (T.rt.diag() is a ring of DIAG_MAX entries: its length stops growing once full, so the new
  // entries are taken by their clock time, rt/clock.js performance.now(), not by index)
  const diag0 = await s.eval(() => performance.now());
  const btn = await s.eval(([i, label]) => {
    const b = window.T.notices.banners.get(i);
    const el = b && Array.from(b.el.querySelectorAll('.banner-actions button')).find(x => x.textContent.trim() === label);
    if (!el) return null;
    el.setAttribute('data-e2e-reload', '1');
    return el.textContent.trim();
  }, [ID, words.reload]);
  check(btn, `no 「${words.reload}」 button on the list-stale banner`);
  await s.page.click('[data-e2e-reload="1"]');
  await waitFor(async () => !(await bannerInfo(s, ID)).up, 'the list-stale banner goes after the reload', 15000);
  await sleep(500);
  c.after_reload = await s.eval(([m, d0]) => ({
    diag: window.T.rt.diag().filter(x => x.kind === 'resync' && typeof x.at === 'number' && x.at >= d0),
    slot_stale: window.__e2e.events.slice(m).filter(e => e.e === 'rt:slot-stale').map(e => e.reason),
    fresh: window.__e2e.events.slice(m).filter(e => e.e === 'shellc:list-stale').map(e => ({ topic: e.topic, key: e.key, stale: e.stale })),
  }), [mark, diag0]);
  c.after_reload.banner = await bannerInfo(s, ID);
  c.snapshot_req_after_reload = reqs() - c.snapshot_req_before;
  check(c.after_reload.diag.some(x => x.topic === TOPIC && x.by === 'page'), `「${words.reload}」 did not invoke rt_resync{${TOPIC}, ${KEY}}: ${JSON.stringify(c.after_reload)}`);
  check(c.after_reload.diag.some(x => x.topic === TOPIC && x.by === 'page' && x.code === 'ok'), `rt_resync outcome: ${JSON.stringify(c.after_reload.diag)}`);
  check(c.snapshot_req_after_reload === 4, `the shell sent ${c.snapshot_req_after_reload - 3} replacement request(s) after the reload (1 expected)`);
  check(!c.after_reload.banner.up && c.after_reload.banner.elements === 0, 'the list-stale banner is still in the document');
  check(c.after_reload.fresh.some(e => e.topic === TOPIC && e.key === KEY && e.stale === false), `the slot did not turn fresh: ${JSON.stringify(c.after_reload.fresh)}`);
  check(!c.after_reload.slot_stale.includes('resync'), `the page-initiated refetch was announced: ${JSON.stringify(c.after_reload.slot_stale)}`);
  c.shell_log_end = s.logFind(/^rt_stale /).map(l => l.text);
  await s.shot('stale-reloaded');
}
