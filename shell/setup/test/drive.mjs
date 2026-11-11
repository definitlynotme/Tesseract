// Drives a running Tesseract Setup window over CDP (WebView2 started with
// WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=<port>) and records what the
// page shows, for the sandbox end-to-end check. Node's built-in WebSocket; no packages.
//   node drive.mjs <port> <outdir> <plan>
// plan: install | install-desktop | look (screenshot the first screen only) | fail
import {writeFile, mkdir} from 'node:fs/promises';
const [port, outdir, plan = 'install'] = process.argv.slice(2);
await mkdir(outdir, {recursive: true});
const sleep = ms => new Promise(r => setTimeout(r, ms));
let target;
for (const deadline = Date.now() + 30000; Date.now() < deadline; await sleep(150)) {
  try {
    const pages = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
    target = pages.find(p => p.type === 'page' && p.url.startsWith('http://setup.localhost/'));
    if (target) break;
  } catch {}
}
if (!target) { console.error('no setup page'); process.exit(2); }
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((ok, no) => { ws.onopen = ok; ws.onerror = no; });
let next = 0; const pending = new Map(); const logs = [];
ws.onmessage = e => {
  const m = JSON.parse(e.data);
  if (m.id && pending.has(m.id)) { const {ok, no} = pending.get(m.id); pending.delete(m.id); m.error ? no(new Error(JSON.stringify(m.error))) : ok(m.result); }
  if (m.method === 'Runtime.consoleAPICalled') logs.push(m.params.args.map(a => a.value ?? a.description).join(' '));
  if (m.method === 'Runtime.exceptionThrown') logs.push('EXCEPTION ' + JSON.stringify(m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text));
};
const call = (method, params = {}) => new Promise((ok, no) => {
  const id = ++next; pending.set(id, {ok, no}); ws.send(JSON.stringify({id, method, params}));
  setTimeout(() => { if (pending.delete(id)) no(new Error('timeout ' + method)); }, 10000);
});
const ev = async x => (await call('Runtime.evaluate', {expression: x, awaitPromise: true, returnByValue: true})).result.value;
const shot = async name => { const r = await call('Page.captureScreenshot', {format: 'png'}); await writeFile(`${outdir}/${name}.png`, Buffer.from(r.data, 'base64')); };
await call('Runtime.enable'); await call('Page.enable');
const snap = () => ev(`(() => { const a = document.querySelector('.act'); return {
  body: document.getElementById('body').innerText.replace(/\\n+/g, ' | '),
  ver: document.getElementById('ver').innerText.replace(/\\n+/g, ' '),
  act: a ? a.dataset.mode + ':' + (a.querySelector('.act-under').lastElementChild?.innerText || '') + ':' + a.style.getPropertyValue('--p') : '',
  foot: document.getElementById('footR').innerText.replace(/\\n+/g, ' / '),
  dot: document.getElementById('dot').getAttribute('class'), close: document.getElementById('wcClose').hidden,
  lang: document.documentElement.lang, w: innerWidth, h: innerHeight }; })()`);
const timeline = [];
const record = async label => { const s = await snap(); s.t = Date.now(); s.label = label; const last = timeline[timeline.length - 1];
  if (!last || last.body !== s.body || last.act.split(':').slice(0, 2).join() !== s.act.split(':').slice(0, 2).join() || label) timeline.push(s); return s; };

await sleep(1600);                               // the opening draw
await record('first'); await shot('1-ready');
if (plan === 'look') { await writeFile(`${outdir}/timeline.json`, JSON.stringify({timeline, logs}, null, 1)); process.exit(0); }
if (plan === 'install-desktop') await ev(`document.getElementById('desk')?.click(), 1`);
await ev(`document.querySelector('.act').click(), 1`);
let midShot = false, final;
for (const deadline = Date.now() + 120000; Date.now() < deadline; await sleep(80)) {
  const s = await record();
  const p = parseFloat(s.act.split(':')[2] || '0');
  if (!midShot && s.act.startsWith('track') && p > .3) { midShot = true; await shot('2-working'); }
  if (/^button:/.test(s.act) && !s.body.includes('…')) { final = s; break; }
}
await sleep(1200);
await record('end'); await shot('3-end');
const out = {timeline, logs};
await writeFile(`${outdir}/timeline.json`, JSON.stringify(out, null, 1));
console.log(JSON.stringify(timeline.map(s => `${s.label || ''} ${s.body} || ${s.act} || ${s.foot} || ${s.dot} || close-hidden=${s.close}`), null, 1));
console.log('logs', logs);
// leave through the page's own 完成 / close, as a user would
if (plan !== 'fail') await ev(`[...document.querySelectorAll('#footR .btn')].find(b => !b.classList.contains('act'))?.click(), 1`);
ws.close();
