// Deterministic render fixture (T-P4-01; P4 architecture §1.4, critique E1).
// Load with Playwright `page.addInitScript({path})` (or `context.addInitScript`) so it
// runs before any page script. It freezes the page's notion of time and chance:
//
//   Date / Date.now / new Date()  fixed start (default 2026-09-29T04:00:00Z) + virtual ms
//   performance.now()             virtual ms since start (starts at 0)
//   Math.random()                 seeded PRNG (mulberry32, default seed 20260929)
//   setTimeout / setInterval / requestAnimationFrame / requestIdleCallback (+ clear*)
//                                 virtual timers: nothing fires until the test advances
//   matchMedia('(prefers-reduced-motion: …)')  answers "reduce"
//
// The test drives time explicitly:  await page.evaluate(ms => window.__fixtureClock.advance(ms), 3000)
// advance() fires due timers in (time, creation order) and yields one macrotask after each
// callback so promise continuations run as they would in a browser. CSS media is not
// changed from here: the runner also calls page.emulateMedia({reducedMotion: 'reduce'})
// (or creates the context with reducedMotion: 'reduce'), which the stylesheet honours.
//
// Optional configuration, set by an earlier init script:
//   window.__FIXTURE_CLOCK_CONFIG__ = {start: <epoch ms>, seed: <uint32>}
(function () {
  'use strict';
  if (window.__fixtureClock) return;
  var cfg = window.__FIXTURE_CLOCK_CONFIG__ || {};
  var START = typeof cfg.start === 'number' ? cfg.start : Date.UTC(2026, 8, 29, 4, 0, 0);
  var seed = (typeof cfg.seed === 'number' ? cfg.seed : 20260929) >>> 0;
  var now = 0;
  var errors = [];

  // ---- Math.random: mulberry32 ----------------------------------------------------
  Math.random = function random() {
    seed = (seed + 0x6D2B79F5) >>> 0;
    var t = seed;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };

  // ---- Date -------------------------------------------------------------------------
  var RealDate = Date;
  function FakeDate() {
    var a = Array.prototype.slice.call(arguments);
    if (!new.target) return new RealDate(START + now).toString();
    return a.length ? new (Function.prototype.bind.apply(RealDate, [null].concat(a)))() : new RealDate(START + now);
  }
  FakeDate.prototype = RealDate.prototype;
  FakeDate.now = function () { return START + now; };
  FakeDate.parse = RealDate.parse;
  FakeDate.UTC = RealDate.UTC;
  window.Date = FakeDate;

  // ---- performance.now --------------------------------------------------------------
  try {
    Object.defineProperty(performance, 'now', { configurable: true, writable: true, value: function () { return now; } });
  } catch (e) { errors.push('performance.now: ' + e); }

  // ---- virtual timers ---------------------------------------------------------------
  var timers = new Map();
  var nextId = 1, seq = 0;
  function add(fn, delay, args, interval) {
    var id = nextId++;
    var d = Math.max(0, Number(delay) || 0);
    timers.set(id, { at: now + d, fn: typeof fn === 'function' ? fn : function () {}, args: args, every: interval ? Math.max(1, d) : 0, seq: seq++ });
    return id;
  }
  function clear(id) { timers.delete(id); }
  window.setTimeout = function (fn, delay) { return add(fn, delay, Array.prototype.slice.call(arguments, 2), false); };
  window.setInterval = function (fn, delay) { return add(fn, delay, Array.prototype.slice.call(arguments, 2), true); };
  window.clearTimeout = clear;
  window.clearInterval = clear;
  window.requestAnimationFrame = function (fn) { return add(function () { fn(now); }, 16, [], false); };
  window.cancelAnimationFrame = clear;
  window.requestIdleCallback = function (fn, opts) {
    var wait = opts && opts.timeout ? Math.min(1, opts.timeout) : 1;
    return add(function () { fn({ didTimeout: false, timeRemaining: function () { return 50; } }); }, wait, [], false);
  };
  window.cancelIdleCallback = clear;

  // One macrotask yield (MessageChannel: no timer clamping, not virtualised).
  var channel = new MessageChannel(), waiting = [];
  channel.port1.onmessage = function () { var r = waiting.shift(); if (r) r(); };
  function yieldTask() { return new Promise(function (r) { waiting.push(r); channel.port2.postMessage(0); }); }

  function nextDue(end) {
    var best = null, bestId = 0;
    timers.forEach(function (t, id) {
      if (t.at <= end && (!best || t.at < best.at || (t.at === best.at && t.seq < best.seq))) { best = t; bestId = id; }
    });
    return best ? { id: bestId, t: best } : null;
  }

  async function advance(ms) {
    var end = now + Math.max(0, Number(ms) || 0);
    await yieldTask();
    for (var guard = 0; guard < 200000; guard++) {
      var n = nextDue(end);
      if (!n) break;
      now = Math.max(now, n.t.at);
      if (n.t.every) { n.t.at += n.t.every; n.t.seq = seq++; } else timers.delete(n.id);
      try { n.t.fn.apply(window, n.t.args); } catch (e) { errors.push(String(e && e.stack || e)); }
      await yieldTask();
    }
    now = end;
    await yieldTask();
    return now;
  }

  // ---- prefers-reduced-motion ---------------------------------------------------------
  var realMatchMedia = window.matchMedia ? window.matchMedia.bind(window) : null;
  function fixedList(query, matches) {
    return {
      media: query, matches: matches, onchange: null,
      addListener: function () {}, removeListener: function () {},
      addEventListener: function () {}, removeEventListener: function () {},
      dispatchEvent: function () { return false; },
    };
  }
  window.matchMedia = function (query) {
    var q = String(query);
    if (/prefers-reduced-motion\s*:\s*reduce/.test(q)) return fixedList(q, true);
    if (/prefers-reduced-motion\s*:\s*no-preference/.test(q)) return fixedList(q, false);
    return realMatchMedia ? realMatchMedia(q) : fixedList(q, false);
  };

  window.__fixtureClock = {
    start: START,
    now: function () { return now; },
    advance: advance,
    pending: function () { return timers.size; },
    errors: errors,
  };
})();
