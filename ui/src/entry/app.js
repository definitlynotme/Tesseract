// app target entry (P4 architecture §1.1.1). Fixed side-effect imports only, in the
// prototype's script order. Written once by T-P4-01; later tickets edit the boot/*
// hook files, never this list.
import '../core/core.js';
import '../core/icons.js';
import '../boot/i18n.js';
import '../boot/data-app.js';
import '../boot/rt-app.js';
import '../boot/shellc.js';
import '../pages/overview.js';
import '../pages/tiers.js';
import '../pages/logs.js';
import '../pages/settings.js';
import '../core/app.js';
