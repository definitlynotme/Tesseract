// Node's built-in WebSocket; no npm packages or external browser required.
import assert from 'node:assert/strict';
import {writeFile} from 'node:fs/promises';
const [port, output] = process.argv.slice(2);
const deadline = Date.now() + 45000;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
let target;
while (Date.now() < deadline) {
  try {
    const pages = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
    target = pages.find(p => p.type === 'page' && p.url.includes('dev-rt.html'));
    if (target) break;
  } catch {}
  await sleep(100);
}
assert(target, 'dev-rt CDP target missing');
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((resolve,reject) => { ws.onopen=resolve; ws.onerror=reject; });
let next = 0;
const pending = new Map();
const deliveries = [];
ws.onmessage = event => {
  const message = JSON.parse(event.data);
  if (message.id && pending.has(message.id)) {
    const {resolve,reject} = pending.get(message.id); pending.delete(message.id);
    message.error ? reject(new Error(JSON.stringify(message.error))) : resolve(message.result);
  }
  if (message.method === 'Runtime.bindingCalled' && message.params.name === '__p3Record') {
    deliveries.push(JSON.parse(message.params.payload));
  }
};
function call(method,params={}) {
  const id = ++next;
  return new Promise((resolve,reject) => {
    pending.set(id,{resolve,reject}); ws.send(JSON.stringify({id,method,params}));
    setTimeout(() => {if(pending.delete(id)) reject(new Error(`CDP timeout: ${method}`));},8000).unref();
  });
}
async function evaluate(expression) {
  const r = await call('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});
  if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails));
  return r.result.value;
}
function frames() { return deliveries.flatMap(d => d.frame.t === 'batch' ? d.frame.items : [d.frame]); }
async function until(predicate,label) {
  while(Date.now()<deadline) { if(predicate()) return; await sleep(20); }
  throw new Error(`timeout: ${label}`);
}
await call('Runtime.enable');
await call('Runtime.addBinding',{name:'__p3Record'});
async function pageReady(expression,label) {
  while(Date.now()<deadline) {
    try { if(await evaluate(expression)) return; } catch {}
    await sleep(50);
  }
  throw new Error(`page initialization timeout: ${label}`);
}
// Prove the default UI document loads with its runtime (P4 transition: T.rt or the prototype's T.sim).
await call('Page.navigate',{url:'http://tauri.localhost/'});
await pageReady("location.pathname === '/' && !!(window.T?.rt || window.T?.sim) && document.querySelectorAll('.nav button').length === 4",'prototype');
const prototype=await evaluate("({sim:!!window.T.sim,rt:!!window.T.rt,title:document.title,nav:document.querySelectorAll('.nav button').length,width:innerWidth,height:innerHeight,dpr:devicePixelRatio})");
assert(Math.abs(prototype.width-1280)<=2 && Math.abs(prototype.height-800)<=2,'default logical window size');
await call('Page.navigate',{url:'http://tauri.localhost/dev-rt.html'});
await pageReady("location.pathname === '/dev-rt.html' && typeof window.devSubscribe === 'function'",'dev-rt');
// Wait for the deferred page script and then use exactly the debug page's API.
while(!await evaluate("typeof window.devSubscribe === 'function'")) {
  assert(Date.now()<deadline,'debug page script did not initialize'); await sleep(50);
}
await evaluate('window.devSubscribe()');
await until(() => frames().some(f=>f.topic==='service.state'), 'service.state snapshot');
const hello=frames().find(f=>f.t==='hello');
assert.equal(hello.proto,1); assert.equal(hello.scope.kind,'custom');
const epoch=hello.epoch;
const snapshots=['service.state','route.published','tiers.summary','traffic.tick','traffic.history','core.info'];
await until(() => snapshots.every(topic=>frames().some(f=>f.topic===topic)), 'all P1 snapshots');
assert.equal(await evaluate("window.__TAURI__.core.invoke('rt',{req_id:'forbidden',cmd:'app.quit',args:{}}).then(()=>false,e=>e==='not_allowed')"),true);
assert.equal(await evaluate("window.__TAURI__.core.invoke('rt',{req_id:'cancel',cmd:'cancel',args:{op:'foreign'}}).then(()=>false,e=>e==='not_allowed')"),true);
const start = await evaluate("window.devSend('service.start')"); assert.equal(start.queued,true);
const terminal=['done','failed','cancelled','superseded'];
await until(()=>frames().some(f=>f.topic==='jobs' && f.data.name==='service.start' && terminal.includes(f.data.state)),'start terminal');
const startJob=frames().find(f=>f.topic==='jobs'&&f.data.name==='service.start'&&terminal.includes(f.data.state));
const replayIndex=deliveries.length;
await evaluate('window.devSubscribe()');
await until(()=>deliveries.slice(replayIndex).some(d=>d.frame.topic==='jobs'&&d.frame.data.op===startJob.data.op),'replayed job');
await evaluate("window.devSend('service.stop')");
await until(()=>frames().some(f=>f.topic==='jobs'&&f.data.name==='service.stop'&&terminal.includes(f.data.state)),'stop terminal');
const stopJob=frames().find(f=>f.topic==='jobs'&&f.data.name==='service.stop'&&terminal.includes(f.data.state));
assert.equal(stopJob.data.state,'done'); assert.equal(stopJob.data.result.cleanup_pending,false);
await evaluate("window.__TAURI__.core.invoke('dev_quit')");
await until(()=>frames().some(f=>f.t==='bye'),'bye on Channel');
const bye=frames().find(f=>f.t==='bye'); assert.equal(bye.exit_code,0);
for(const domain of ['proxy','firewall']) assert(['not_needed','not_owned','restored'].includes(bye.network_recovery[domain].state));
assert(deliveries.every((d,i)=>!i || d.order===deliveries[i-1].order+1),'Channel order must be contiguous');
assert(frames().filter(f=>!f.t.startsWith('rt:')).every(f=>f.epoch===epoch),'epoch mismatch');
await writeFile(output,JSON.stringify({passed:true,runtime_pid:hello.runtime_pid,epoch,deliveries:deliveries.length,
  snapshots,start_state:startJob.data.state,start_result:startJob.data.result,start_error:startJob.data.error,
  stop_state:stopJob.data.state,bye,channel_order:true,cache_replay:true,whitelist:true,prototype},null,2));
ws.close();
console.log(`P3 WebView Channel smoke passed: ${deliveries.length} deliveries, start=${startJob.data.state}, stop=done, bye=0`);
