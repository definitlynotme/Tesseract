// Hook: i18n runtime (owner: T-P4-03; see boot/README.md, P4 architecture §3.5–§3.7).
// Installs T.t / T.tx / T.ti / T.i18n (bound to T.store, T.emit and T.fmt), registers the
// language files that ran before it (the source catalog is preloaded by index.html), then
// boots: before hello, preload the navigator.language guess. The page language itself comes
// from hello.lang / host:language via T.i18n.set(lang).
import { install } from '../i18n/t.js';

install(window).boot();
