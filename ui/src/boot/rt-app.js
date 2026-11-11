// Hook: app target runtime (owner: T-P4-02a, then T-P4-07; see boot/README.md).
// No simulator in the app target (criticism C11). T.rt talks to the shell over TauriTransport.
// T-P4-07: the transitional thin simulator facade and the rt-to-prototype event alias are gone
// with their files (rt/legacy_sim.js, rt/alias.js); the pages listen to rt:* and call T.rt only.
import { installRt } from '../rt/client.js';
import { parseFeatureDefine } from '../rt/features.js';
import { TauriTransport } from '../rt/transport-tauri.js';

const T = window.T;
installRt(T, { enabledFeatures: parseFeatureDefine(__H_FEATURES__) });
let transport = null;
try { transport = new TauriTransport({ clock: T.clock }); } catch (e) { transport = null; } // opened outside the shell: stays idle
if (transport) T.rt.connect(transport);
