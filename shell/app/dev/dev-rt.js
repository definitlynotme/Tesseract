'use strict';
const {invoke, Channel} = window.__TAURI__.core;
window.devFrames = [];
window.devDeliveries = [];
let channel;
let request = 0;
window.devSubscribe = async () => {
  channel = new Channel();
  channel.onmessage = delivery => {
    window.__p3Record?.(JSON.stringify(delivery));
    window.devDeliveries.push(delivery);
    window.devFrames.push(delivery.frame);
    const text = document.getElementById('frames');
    text.textContent = window.devFrames.slice(-100).map(f => JSON.stringify(f)).join('\n');
  };
  await invoke('rt_connect', {channel});
};
window.devSend = cmd => invoke('rt', {req_id: `dev-${++request}`, cmd, args: {}, owner: 'dev-rt'});
document.getElementById('subscribe').onclick = () => window.devSubscribe();
document.getElementById('start').onclick = () => window.devSend('service.start');
document.getElementById('stop').onclick = () => window.devSend('service.stop');
document.getElementById('quit').onclick = () => invoke('dev_quit');
