// design target entry (P4 architecture §1.1.1). Same order as the app entry, with the
// design data/runtime hooks, plus boot/design.js last. Written once by T-P4-01; later
// tickets edit the boot/* hook files, never this list.
import '../core/core.js';
import '../core/icons.js';
import '../boot/i18n.js';
import '../boot/data-design.js';
import '../boot/rt-design.js';
import '../boot/shellc.js';
import '../pages/overview.js';
import '../pages/tiers.js';
import '../pages/logs.js';
import '../pages/settings.js';
import '../core/app.js';
import '../boot/design.js';
