//! P3-03 notification-area icon: Shell_NotifyIconW (tooltip, menu, NIF_INFO balloon),
//! close-to-tray policy and the tooltip model fed from the shell's accepted topic frames.
//!
//! Threads: the bridge worker only updates `Feed` and posts REFRESH; every Shell_NotifyIconW
//! call except `show_balloon` runs on the UI thread that owns the hidden tray window.
//! `show_balloon` may be called from any thread and is serialized by the icon lock.
//! T-P3-05 alert balloons are queued by `post_alert_balloon` (any thread) and submitted by the
//! UI thread on ALERT_BALLOON, so a hung Explorer never blocks the bridge worker.
//! T-P4-C3 (D20): the icon itself follows the run state the page's brand dot shows (mapping_core
//! section 1.2 `run`, from accepted `service.state` facts) and carries an amber badge while the
//! kill switch is disabled / blocked / error (`killswitch.state`, same classification as the
//! page). Offline, stale and a new epoch read `unknown`. The tooltip text is unchanged.
use crate::native_text;
use serde_json::Value;
use std::{collections::VecDeque, path::PathBuf, sync::{atomic::{AtomicIsize, AtomicU8, Ordering}, Arc, Mutex, MutexGuard, OnceLock},
    time::{Duration, Instant}};
use windows::core::{s, w, PCSTR, PCWSTR};
use windows::Win32::Foundation::{HWND, LPARAM, LRESULT, WPARAM};
use windows::Win32::System::LibraryLoader::{GetModuleHandleW, GetProcAddress};
use windows::Win32::UI::Shell::*;
use windows::Win32::UI::WindowsAndMessaging::*;

/// `NOTIFYICONDATAW.szTip` holds 128 UTF-16 units including the terminator.
pub const TIP_UNITS: usize = 127;
const INFO_TITLE_UNITS: usize = 63;
const INFO_TEXT_UNITS: usize = 255;
/// `STARTUP_TRAY_SHOW_DELAY_MS` in the Qt shell.
pub const SHOW_DELAY_MS: u32 = 250;
/// PROTOCOL §3: running/starting `service.state` older than 3 s is stale.
pub const SERVICE_SILENCE: Duration = Duration::from_secs(3);
pub const CLASS: PCWSTR = w!("TesseractShellTray");
pub const ICON_ID: u32 = 1;
pub const CALLBACK: u32 = WM_APP + 1;
/// Debug-build-only smoke entry: SendMessage returns 1 when the balloon was submitted.
#[cfg_attr(not(debug_assertions), allow(dead_code))]
pub const DEV_BALLOON: u32 = WM_APP + 2;
const REFRESH: u32 = WM_APP + 3;
/// T-P3-05: submit the queued alert balloons (posted by `post_alert_balloon`).
const ALERT_BALLOON: u32 = WM_APP + 4;
/// Alert balloons waiting for the UI thread; more means that thread is stuck (no surface).
const ALERT_QUEUE: usize = 64;
pub const CMD_SHOW: usize = 1;
pub const CMD_QUIT: usize = 2;
const TIMER_SHOW: usize = 1;
const TIMER_STALE: usize = 2;
const TIMER_READD: usize = 3;
const READD_ATTEMPTS: u8 = 5;
const NIN_KEYSELECT: u32 = NIN_SELECT | 1;
const APP_ICON_RESOURCE: usize = 32512; // tauri-build embeds the window icon with this id.

pub fn fit(text: &str, max_units: usize) -> String {
    if text.encode_utf16().count() <= max_units { return text.to_owned(); }
    let mut out = String::new(); let mut units = 0;
    for ch in text.chars() {
        // Reserve one unit for the ellipsis; never split a surrogate pair.
        if units + ch.len_utf16() + 1 > max_units { break; }
        units += ch.len_utf16(); out.push(ch);
    }
    out.push('…'); out
}

// ---------------------------------------------------------------- tooltip model

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Tier { Vanguard, Sentinel, Bastion, Gateway }
impl Tier {
    fn parse(id: &str) -> Option<Self> {
        Some(match id { "vanguard"=>Self::Vanguard, "sentinel"=>Self::Sentinel, "bastion"=>Self::Bastion, "gateway"=>Self::Gateway, _=>return None })
    }
    /// The codename alone, as the page names a tier (DESIGN §6.11, F-03: no role word).
    fn label(self) -> &'static str {
        match self { Self::Vanguard=>"Vanguard", Self::Sentinel=>"Sentinel", Self::Bastion=>"Bastion", Self::Gateway=>"Gateway" }
    }
}
#[derive(Clone, Copy, Debug)]
struct Service { core_running: bool, stale: bool, live: bool, run: Run, at: Instant }

/// D20 run state of the icon: the page's brand-dot `run` (mapping_core 1.2; `stopping` shows
/// as stopped, as the page's grey dot does). Order = the generated icon files' order.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Run { Unknown, Stopped, Starting, Running, Failed }
impl Run {
    #[cfg_attr(not(test), allow(dead_code))]
    pub const ALL: [Run; 5] = [Run::Unknown, Run::Stopped, Run::Starting, Run::Running, Run::Failed];
    /// `runtime.startup_phase` -> run (`ui/src/rt/project.js` runOf).
    pub fn of(phase: Option<&str>) -> Self {
        match phase {
            Some("collecting" | "core_standby") => Self::Starting, Some("running") => Self::Running,
            Some("stopping" | "stopped") => Self::Stopped, Some("failed") => Self::Failed, _ => Self::Unknown,
        }
    }
    pub fn name(self) -> &'static str {
        match self { Self::Unknown=>"unknown", Self::Stopped=>"stopped", Self::Starting=>"starting", Self::Running=>"running", Self::Failed=>"failed" }
    }
}
/// The accepted `killswitch.state` fact of the current epoch (PROTOCOL section 11 DTO).
#[derive(Clone, Copy, Debug, PartialEq)]
struct Killswitch { enabled: bool, admin: bool, error: bool, triggered: bool }
impl Killswitch {
    /// The page's classification (`project.js` killswitchOf): disabled / error / inactive /
    /// triggered / blocked / armed. D20 marks disabled, blocked and error amber.
    fn amber(self, run: Run) -> bool {
        if !self.enabled || self.error { return true; }
        run == Run::Running && !self.triggered && !self.admin
    }
}
/// What the tray icon shows: one of the ten generated icons.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Glyph { pub run: Run, pub amber: bool }
impl Glyph {
    pub const UNKNOWN: Glyph = Glyph { run: Run::Unknown, amber: false };
    pub fn index(self) -> usize { self.run as usize * 2 + self.amber as usize }
    /// shell.log and file name: `running`, `running-ks`.
    pub fn name(self) -> String { format!("{}{}", self.run.name(), if self.amber { "-ks" } else { "" }) }
}

/// Last accepted `service.state` / `tiers.summary` / `killswitch.state` / `ui.language` facts of the current epoch.
pub struct Status { pub locale: &'static str, ready: bool, service: Option<Service>, tiers_seen: bool, carrier: Option<Tier>, ks: Option<Killswitch> }
impl Status {
    pub fn new(locale: &'static str) -> Self { Self { locale, ready: false, service: None, tiers_seen: false, carrier: None, ks: None } }
    /// New epoch: drop every cached fact; `hello.lang` replaces the pre-hello system language.
    pub fn hello(&mut self, hello: &Value) {
        if let Some(locale) = hello["lang"].as_str().and_then(native_text::supported) { self.locale = locale; }
        self.ready = true; self.service = None; self.tiers_seen = false; self.carrier = None; self.ks = None;
    }
    pub fn gone(&mut self) { self.ready = false; self.service = None; self.tiers_seen = false; self.carrier = None; self.ks = None; }
    /// Apply one already-accepted `evt`. Returns whether anything the tray renders changed.
    pub fn observe(&mut self, evt: &Value, now: Instant) -> bool {
        if evt["t"] != "evt" { return false; }
        // A kill-switch gap leaves no fact to show (the page marks the topic stale).
        if evt["kind"] == "gap" { return evt["topic"] == "killswitch.state" && self.ks.take().is_some(); }
        let data = &evt["data"];
        match evt["topic"].as_str() {
            Some("killswitch.state") => {
                let ks = match (data["enabled"].as_bool(), data["admin"].as_bool()) {
                    (Some(enabled), Some(admin)) => Some(Killswitch { enabled, admin, error: !data["error"].is_null(),
                        triggered: data["triggered"] == true || data["state"] == "triggered" }),
                    _ => None,
                };
                let changed = ks != self.ks; self.ks = ks; changed
            },
            Some("service.state") => {
                let phase = data["runtime"]["startup_phase"].as_str();
                self.service = match (data["core"]["running"].as_bool(), data["freshness"]["stale"].as_bool()) {
                    (Some(core_running), Some(stale)) => Some(Service { core_running, stale, at: now, run: Run::of(phase),
                        live: data["runtime"]["runtime_running"].as_bool() == Some(true)
                            || matches!(phase, Some("collecting" | "core_standby" | "running")) }),
                    _ => None,
                };
                true
            },
            Some("tiers.summary") => { self.tiers_seen = true; self.carrier = data["carrier"].as_str().and_then(Tier::parse); true },
            Some("ui.language") => match data["language"].as_str().and_then(native_text::supported) {
                Some(locale) if locale != self.locale => { self.locale = locale; true },
                _ => false,
            },
            _ => false,
        }
    }
    fn fresh(&self, now: Instant) -> Option<Service> {
        self.service.filter(|s| self.ready && !s.stale && !(s.live && now.saturating_duration_since(s.at) > SERVICE_SILENCE))
    }
    /// Qt rule: a missing or stale value shows the tier as offline and the core as stopped.
    pub fn tooltip(&self, now: Instant) -> String {
        let fresh = self.fresh(now);
        let tier = match (fresh, self.tiers_seen, self.carrier) {
            (Some(_), true, Some(tier)) => tier.label().to_owned(),
            _ => native_text::text("tray_offline", self.locale).to_owned(),
        };
        let core = if fresh.is_some_and(|s| s.core_running) { "tray_core_running" } else { "tray_core_stopped" };
        fit(&format!("Tesseract\n{}\n{}", native_text::format("tray_tier_line", self.locale, &tier),
            native_text::text(core, self.locale)), TIP_UNITS)
    }
    /// When the tooltip turns stale by silence; a one-shot timer, not polling.
    pub fn deadline(&self, now: Instant) -> Option<Instant> {
        self.fresh(now).filter(|s| s.live).map(|s| s.at + SERVICE_SILENCE)
    }
    /// D20: the icon for `now`. A missing, stale (explicit or by silence) or offline service
    /// reads unknown, without the badge (no current fact to badge).
    pub fn glyph(&self, now: Instant) -> Glyph {
        let Some(service) = self.fresh(now) else { return Glyph::UNKNOWN };
        if service.run == Run::Unknown { return Glyph::UNKNOWN; }
        Glyph { run: service.run, amber: self.ks.is_some_and(|ks| ks.amber(service.run)) }
    }
    pub fn menu(&self) -> [&'static str; 2] {
        [native_text::text("tray_show", self.locale), native_text::text("tray_quit", self.locale)]
    }
}

/// Worker-side entry: the bridge worker feeds the facts the transport hub's cache accepted
/// (`Hub::take_facts`: stream frames and `rt_resync` replacement snapshots, never a refused older
/// rev) and the shell-topic `ui.language` edges.
pub struct Feed { status: Mutex<Status>, hwnd: AtomicIsize }
impl Feed {
    pub fn new(locale: &'static str) -> Arc<Self> { Arc::new(Self { status: Mutex::new(Status::new(locale)), hwnd: AtomicIsize::new(0) }) }
    pub fn status(&self) -> MutexGuard<'_, Status> { self.status.lock().unwrap_or_else(|e| e.into_inner()) }
    pub fn apply(&self, items: Vec<Value>) {
        if items.is_empty() { return; }
        let now = Instant::now();
        let changed = { let mut s = self.status(); items.iter().fold(false, |c, item| s.observe(item, now) | c) };
        if changed { self.poke(); }
    }
    pub fn hello(&self, hello: &Value) { self.status().hello(hello); self.poke(); }
    pub fn gone(&self) { self.status().gone(); self.poke(); }
    fn poke(&self) {
        let hwnd = self.hwnd.load(Ordering::Acquire);
        if hwnd != 0 { unsafe { let _ = PostMessageW(Some(HWND(hwnd as _)), REFRESH, WPARAM(0), LPARAM(0)); } }
    }
}

// ---------------------------------------------------------------- close policy

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum CloseAction {
    /// Hide only: no quit, no balloon.
    HideToTray,
    /// No tray: closing is the quit path (`request_quit`).
    RequestQuit,
    /// A quit is already in flight: do not intercept into the tray; the worker exits the app.
    AwaitQuit,
    /// The bridge worker is already finished; exit immediately (P3-01 behaviour).
    ExitNow,
}
pub fn close_action(quit_requested: bool, worker_done: bool, tray_available: bool) -> CloseAction {
    if worker_done { CloseAction::ExitNow }
    else if quit_requested { CloseAction::AwaitQuit }
    else if tray_available { CloseAction::HideToTray }
    else { CloseAction::RequestQuit }
}
/// The window close wiring: only an ordinary close may create the tray early (Qt
/// `_ensure_tray_icon`); a close during a quit or after the worker finished never calls `ensure`.
pub fn close_decision(quit_requested: bool, worker_done: bool, ensure: impl FnOnce() -> bool) -> CloseAction {
    let tray_available = !quit_requested && !worker_done && ensure();
    close_action(quit_requested, worker_done, tray_available)
}

// ---------------------------------------------------------------- icon state machine

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Severity { Info, Warning, Error }
impl Severity {
    /// PROTOCOL `alerts.desktop.severity` is info|warn|error|critical. Same buckets as Qt
    /// `_alert_notice_severity`: critical uses the error icon; success and unknown use info.
    pub fn parse(value: &str) -> Self {
        match value { "error" | "critical"=>Self::Error, "warn" | "warning"=>Self::Warning, _=>Self::Info }
    }
}
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Balloon { Submitted, NoIcon, Empty, Rejected }
impl Balloon {
    /// PROTOCOL §13: success means the shell submitted the balloon to Windows.
    pub fn submitted(self) -> bool { self == Self::Submitted }
    pub fn code(self) -> &'static str {
        match self { Self::Submitted=>"balloon_submitted", Self::NoIcon=>"balloon_no_icon", Self::Empty=>"balloon_empty", Self::Rejected=>"balloon_rejected" }
    }
}
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Input { Restore, Menu, BalloonClick, BalloonGone }
/// Callback LOWORD(lParam) → user intent. v4 reports single left click as NIN_SELECT
/// (Qt `Trigger`); legacy callbacks only report raw mouse messages. Balloon notifications
/// (Shell 5.0+) arrive the same way in both modes.
pub fn classify(event: u32, v4: bool) -> Option<Input> {
    match (event, v4) {
        (NIN_BALLOONUSERCLICK, _) => Some(Input::BalloonClick),
        (NIN_BALLOONTIMEOUT | NIN_BALLOONHIDE, _) => Some(Input::BalloonGone),
        (NIN_SELECT | NIN_KEYSELECT, true) => Some(Input::Restore),
        (WM_CONTEXTMENU, true) => Some(Input::Menu),
        (WM_LBUTTONUP, false) => Some(Input::Restore),
        (WM_RBUTTONUP | WM_CONTEXTMENU, false) => Some(Input::Menu),
        _ => None,
    }
}

pub trait Notify {
    fn add(&mut self, tip: &str, glyph: Glyph) -> bool;
    fn modify(&mut self, tip: &str, glyph: Glyph) -> bool;
    fn balloon(&mut self, title: &str, text: &str, severity: Severity) -> bool;
    fn delete(&mut self) -> bool;
    fn v4(&self) -> bool;
}
pub struct Icon<N: Notify> { pub notify: N, wanted: bool, added: bool, tip: String, glyph: Glyph }
impl<N: Notify> Icon<N> {
    pub fn new(notify: N) -> Self { Self { notify, wanted: false, added: false, tip: String::new(), glyph: Glyph::UNKNOWN } }
    pub fn available(&self) -> bool { self.added }
    pub fn wanted(&self) -> bool { self.wanted }
    pub fn glyph(&self) -> Glyph { self.glyph }
    pub fn show(&mut self, tip: &str, glyph: Glyph) -> bool {
        self.wanted = true; self.tip = tip.into(); self.glyph = glyph;
        if !self.added { self.added = self.notify.add(tip, glyph); }
        self.added
    }
    /// Explorer restarted: every icon is gone and must be added again.
    pub fn taskbar_created(&mut self) -> bool {
        if !self.wanted { return false; }
        self.added = false; let tip = self.tip.clone();
        self.added = self.notify.add(&tip, self.glyph); self.added
    }
    /// Tooltip and (D20) icon in one NIM_MODIFY. Returns true only when this call lost the icon
    /// (modify and the immediate re-add both failed); the caller then schedules the bounded
    /// re-add retry.
    pub fn set(&mut self, tip: &str, glyph: Glyph) -> bool {
        if tip == self.tip && glyph == self.glyph { return false; }
        self.tip = tip.into(); self.glyph = glyph;
        self.redraw()
    }
    /// Sends the current tooltip and icon again (T-P4-C3 fix round 1: the icon handles were
    /// reloaded for a new DPI). Same loss rule as [`Icon::set`].
    pub fn redraw(&mut self) -> bool {
        if !self.added { return false; }
        let tip = self.tip.clone();
        if self.notify.modify(&tip, self.glyph) { return false; }
        // NIM_MODIFY also returns FALSE when a busy Explorer times out while the icon still
        // exists. Re-add at once (`add` replaces a surviving same-id icon) instead of leaving
        // the tray unavailable until the next close or TaskbarCreated.
        self.added = self.notify.add(&tip, self.glyph);
        !self.added
    }
    pub fn balloon(&mut self, title: &str, text: &str, severity: Severity) -> Balloon {
        let title = fit(title.trim(), INFO_TITLE_UNITS);
        let text = text.trim();
        // NIF_INFO with an empty szInfo removes a balloon instead of showing one (Qt: `message or title`).
        let text = fit(if text.is_empty() { &title } else { text }, INFO_TEXT_UNITS);
        if text.is_empty() { return Balloon::Empty; }
        if !self.added { return Balloon::NoIcon; }
        if self.notify.balloon(&title, &text, severity) { Balloon::Submitted } else { Balloon::Rejected }
    }
    pub fn remove(&mut self) {
        self.wanted = false;
        if self.added { self.notify.delete(); self.added = false; }
    }
}

// ---------------------------------------------------------------- Win32

fn copy(dst: &mut [u16], text: &str) {
    let units: Vec<u16> = fit(text, dst.len() - 1).encode_utf16().collect();
    dst[..units.len()].copy_from_slice(&units); dst[units.len()] = 0;
}
/// `icons`: one HICON per [`Glyph::index`] at the current small-icon size.
pub struct Win32 { hwnd: isize, icons: IconSet, v4: bool }
impl Win32 {
    fn data(&self) -> NOTIFYICONDATAW {
        NOTIFYICONDATAW { cbSize: std::mem::size_of::<NOTIFYICONDATAW>() as u32, hWnd: HWND(self.hwnd as _), uID: ICON_ID, ..Default::default() }
    }
}
impl Notify for Win32 {
    fn add(&mut self, tip: &str, glyph: Glyph) -> bool {
        let mut d = self.data();
        d.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_SHOWTIP; d.uCallbackMessage = CALLBACK; d.hIcon = self.icons.handle(glyph);
        copy(&mut d.szTip, tip);
        unsafe {
            let mut ok = Shell_NotifyIconW(NIM_ADD, &d).as_bool();
            // A same-id icon that survived (simulated TaskbarCreated) is replaced, never duplicated.
            if !ok { let _ = Shell_NotifyIconW(NIM_DELETE, &self.data()); ok = Shell_NotifyIconW(NIM_ADD, &d).as_bool(); }
            if ok { d.Anonymous.uVersion = NOTIFYICON_VERSION_4; self.v4 = Shell_NotifyIconW(NIM_SETVERSION, &d).as_bool(); }
            ok
        }
    }
    fn modify(&mut self, tip: &str, glyph: Glyph) -> bool {
        let mut d = self.data(); d.uFlags = NIF_ICON | NIF_TIP | NIF_SHOWTIP; d.hIcon = self.icons.handle(glyph); copy(&mut d.szTip, tip);
        unsafe { Shell_NotifyIconW(NIM_MODIFY, &d).as_bool() }
    }
    fn balloon(&mut self, title: &str, text: &str, severity: Severity) -> bool {
        let mut d = self.data(); d.uFlags = NIF_INFO;
        copy(&mut d.szInfoTitle, title); copy(&mut d.szInfo, text);
        d.dwInfoFlags = match severity { Severity::Info=>NIIF_INFO, Severity::Warning=>NIIF_WARNING, Severity::Error=>NIIF_ERROR };
        unsafe { Shell_NotifyIconW(NIM_MODIFY, &d).as_bool() }
    }
    fn delete(&mut self) -> bool { unsafe { Shell_NotifyIconW(NIM_DELETE, &self.data()).as_bool() } }
    fn v4(&self) -> bool { self.v4 }
}

pub struct Hooks {
    /// The P3-02 restore path (show, unminimize, set focus).
    pub restore: Box<dyn Fn() + Send + Sync>,
    /// `request_quit(QuitReason::Tray)`; P3-04 replaces the body behind it.
    pub quit: Box<dyn Fn() + Send + Sync>,
    /// T-P3-05: a clicked alert balloon, after `restore`: `host:navigate{page: target}`.
    pub navigate: Box<dyn Fn(&'static str) + Send + Sync>,
}
/// Called once with the result of an alert balloon: `None` when it never reached Windows (no
/// tray, the tray window is gone or its queue is full).
pub type BalloonDone = Box<dyn FnOnce(Option<Balloon>) + Send>;
struct AlertBalloon { title: String, text: String, severity: Severity, target: &'static str, done: BalloonDone }
/// What a click on the balloon on screen navigates to (T-P3-H, generations). Every submitted
/// balloon, alert or plain, takes the next generation together with its target (`None` for a
/// plain one) under the icon lock, so the slot always describes the latest submission - also
/// when a plain `show_balloon` from another thread interleaves with the UI thread's alert
/// balloons. Windows' NIN_BALLOONUSERCLICK/TIMEOUT/HIDE carry no balloon identity: the newest
/// generation is the one on screen, a click consumes it once, and timeout/hide never clear it
/// (the hide of a replaced balloon can arrive after the new one was submitted).
#[derive(Debug, Default)]
pub struct ClickTarget { generation: u64, target: Option<&'static str> }
impl ClickTarget {
    /// A balloon was submitted to Windows; returns its generation.
    pub fn submitted(&mut self, target: Option<&'static str>) -> u64 { self.generation += 1; self.target = target; self.generation }
    /// The user clicked: the latest balloon's generation and target, at most once per balloon.
    pub fn click(&mut self) -> (u64, Option<&'static str>) { (self.generation, self.target.take()) }
}
pub struct Tray { feed: Arc<Feed>, icon: Mutex<Icon<Win32>>, hooks: Hooks, data: PathBuf, hwnd: isize, taskbar_created: u32, readd: AtomicU8,
    /// The click target of the latest submitted balloon, by generation ([`ClickTarget`]).
    balloon_target: Mutex<ClickTarget>,
    alerts: Mutex<VecDeque<AlertBalloon>> }
static TRAY: OnceLock<Tray> = OnceLock::new();
pub fn installed() -> Option<&'static Tray> { TRAY.get() }
/// Balloon API for the notification ticket (alerts.desktop / P2-22). Returns true only when the
/// balloon was submitted to Windows (PROTOCOL §13); no tray, empty text or a refusal is false.
#[allow(dead_code)]
pub fn show_balloon(title: &str, text: &str, severity: Severity) -> bool {
    installed().is_some_and(|tray| tray.show_balloon(title, text, severity))
}

/// The app icon at `size` px, and whether the caller owns it (a LoadImageW copy must be freed
/// with DestroyIcon; the shared system icon must not be).
fn load_icon(size: i32) -> (HICON, bool) {
    unsafe {
        let module = GetModuleHandleW(None).ok();
        match LoadImageW(module.map(|m| m.into()), PCWSTR(APP_ICON_RESOURCE as *const u16), IMAGE_ICON, size, size, LR_DEFAULTCOLOR) {
            Ok(handle) => (HICON(handle.0), true),
            Err(_) => (LoadIconW(None, IDI_APPLICATION).unwrap_or_default(), false),
        }
    }
}
/// The small-icon size for the tray window's current DPI (T-P4-C3 fix round 1):
/// `GetSystemMetricsForDpi(SM_CXSMICON, GetDpiForWindow)` when user32 has them (Windows 10 1607
/// and later; resolved at run time because Cargo.toml has no HiDpi feature), else
/// `GetSystemMetrics`, which keeps the DPI the session started with.
fn small_icon_size(hwnd: HWND) -> i32 {
    type ForWindow = unsafe extern "system" fn(HWND) -> u32;
    type ForDpi = unsafe extern "system" fn(i32, u32) -> i32;
    unsafe {
        let user32 = GetModuleHandleW(w!("user32.dll")).ok();
        let lookup = |name: PCSTR| user32.and_then(|module| GetProcAddress(module, name));
        if let (Some(for_window), Some(for_dpi)) = (lookup(s!("GetDpiForWindow")), lookup(s!("GetSystemMetricsForDpi"))) {
            let (for_window, for_dpi) = (std::mem::transmute::<_, ForWindow>(for_window), std::mem::transmute::<_, ForDpi>(for_dpi));
            let dpi = for_window(hwnd);
            let size = if dpi > 0 { for_dpi(SM_CXSMICON.0, dpi) } else { 0 };
            if size > 0 { return size; }
        }
        GetSystemMetrics(SM_CXSMICON).max(1)
    }
}
/// The ten D20 status icons at one size (the app icon where a state icon failed to load), and
/// the handles this set owns. Reloaded when the small-icon size changes (`Tray::rescale`).
pub struct IconSet { size: i32, handles: [isize; 10], owned: Vec<isize> }
impl IconSet {
    fn load(size: i32, data: &std::path::Path) -> Self {
        let (fallback, fallback_owned) = load_icon(size);
        let mut set = IconSet { size, handles: [fallback.0 as isize; 10], owned: Vec::new() };
        if fallback_owned { set.owned.push(fallback.0 as isize); }
        for (slot, (name, ico)) in set.handles.iter_mut().zip(STATUS_ICONS) {
            let loaded = ico_entry(ico, size.max(1) as u32)
                .and_then(|bits| unsafe { CreateIconFromResourceEx(bits, true, 0x0003_0000, size, size, LR_DEFAULTCOLOR) }.ok());
            match loaded {
                Some(icon) => { *slot = icon.0 as isize; set.owned.push(icon.0 as isize); },
                None => crate::shell_log::fields(data, "tray_status_icon_failed", &[("icon", name), ("size", &size.to_string())]),
            }
        }
        set
    }
    fn handle(&self, glyph: Glyph) -> HICON { HICON(self.handles[glyph.index()] as _) }
    /// Frees the owned handles; call only once Shell_NotifyIconW holds a newer set (the shell
    /// keeps its own copy of an icon it was given).
    fn free(self) {
        for handle in self.owned { unsafe { let _ = DestroyIcon(HICON(handle as _)); } }
    }
}

/// D20 status icons generated by `.build/claude_brand_assets.py --tray shell/app/icons/tray`
/// (PNG-entry ICO files), in [`Glyph::index`] order.
pub const STATUS_ICONS: [(&str, &[u8]); 10] = [
    ("unknown", include_bytes!("../icons/tray/tray-unknown.ico")), ("unknown-ks", include_bytes!("../icons/tray/tray-unknown-ks.ico")),
    ("stopped", include_bytes!("../icons/tray/tray-stopped.ico")), ("stopped-ks", include_bytes!("../icons/tray/tray-stopped-ks.ico")),
    ("starting", include_bytes!("../icons/tray/tray-starting.ico")), ("starting-ks", include_bytes!("../icons/tray/tray-starting-ks.ico")),
    ("running", include_bytes!("../icons/tray/tray-running.ico")), ("running-ks", include_bytes!("../icons/tray/tray-running-ks.ico")),
    ("failed", include_bytes!("../icons/tray/tray-failed.ico")), ("failed-ks", include_bytes!("../icons/tray/tray-failed-ks.ico")),
];
/// The ICO entry for a `size` px icon: the exact size, else the smallest larger one, else the
/// largest. Returns the entry's image bytes (PNG or DIB), which CreateIconFromResourceEx takes.
pub fn ico_entry(ico: &[u8], size: u32) -> Option<&[u8]> {
    let u16le = |at: usize| ico.get(at..at + 2).map(|b| u16::from_le_bytes([b[0], b[1]]));
    let u32le = |at: usize| ico.get(at..at + 4).map(|b| u32::from_le_bytes([b[0], b[1], b[2], b[3]]));
    if u16le(0)? != 0 || u16le(2)? != 1 { return None; }
    let dim = |v: u8| if v == 0 { 256 } else { v as u32 };
    // (size, bytes, offset) of every directory entry.
    let entries = (0..u16le(4)? as usize).map(|i| 6 + 16 * i)
        .map(|at| Some((dim(*ico.get(at)?), u32le(at + 8)? as usize, u32le(at + 12)? as usize)))
        .collect::<Option<Vec<_>>>()?;
    let (_, len, offset) = *entries.iter().filter(|e| e.0 >= size).min_by_key(|e| e.0).or_else(|| entries.iter().max_by_key(|e| e.0))?;
    if len == 0 { return None; }
    ico.get(offset..offset.checked_add(len)?)
}

/// Creates the hidden tray owner window on the calling (UI) thread. The icon itself is
/// added later by `schedule_show` or on demand by `ensure_available`.
pub fn install(feed: Arc<Feed>, data: PathBuf, hooks: Hooks) -> Result<&'static Tray, u32> {
    if TRAY.get().is_some() { return Err(183); }
    unsafe {
        let module = GetModuleHandleW(None).map_err(|e| e.code().0 as u32)?;
        let class = WNDCLASSEXW { cbSize: std::mem::size_of::<WNDCLASSEXW>() as u32, lpfnWndProc: Some(window_proc),
            hInstance: module.into(), lpszClassName: CLASS, ..Default::default() };
        if RegisterClassExW(&class) == 0 { return Err(windows::Win32::Foundation::GetLastError().0); }
        // A hidden top-level (not message-only) window: only top-level windows receive the
        // TaskbarCreated broadcast. WS_EX_TOOLWINDOW keeps it off the taskbar and Alt+Tab.
        let hwnd = CreateWindowExW(WS_EX_TOOLWINDOW, CLASS, w!("Tesseract tray"), WS_POPUP, 0, 0, 0, 0,
            None, None, Some(module.into()), None).map_err(|e| e.code().0 as u32)?;
        let taskbar_created = RegisterWindowMessageW(w!("TaskbarCreated"));
        // An elevated shell still hears Explorer (medium integrity) for these two messages only.
        for message in [taskbar_created, CALLBACK] { let _ = ChangeWindowMessageFilterEx(hwnd, message, MSGFLT_ALLOW, None); }
        let icons = IconSet::load(small_icon_size(hwnd), &data);
        let tray = Tray { feed: feed.clone(), icon: Mutex::new(Icon::new(Win32 { hwnd: hwnd.0 as isize, icons, v4: false })),
            hooks, data, hwnd: hwnd.0 as isize, taskbar_created, readd: AtomicU8::new(0), balloon_target: Mutex::new(ClickTarget::default()), alerts: Mutex::new(VecDeque::new()) };
        if TRAY.set(tray).is_err() { let _ = DestroyWindow(hwnd); return Err(183); }
        feed.hwnd.store(hwnd.0 as isize, Ordering::Release);
        Ok(TRAY.get().unwrap())
    }
}

impl Tray {
    fn icon(&self) -> MutexGuard<'_, Icon<Win32>> { self.icon.lock().unwrap_or_else(|e| e.into_inner()) }
    fn hwnd(&self) -> HWND { HWND(self.hwnd as _) }
    /// The hidden top-level owner window (UI thread). T-P3-07: it carries the session-end hook
    /// once no main window can be rebuilt (it still receives WM_QUERYENDSESSION).
    pub fn window(&self) -> isize { self.hwnd }
    fn log(&self, code: &str) { crate::log_code(&self.data, code); }
    /// Call after the window's first stable show; the icon appears 250 ms later.
    pub fn schedule_show(&self) { unsafe { SetTimer(Some(self.hwnd()), TIMER_SHOW, SHOW_DELAY_MS, None); } }
    fn show_now(&self) -> bool {
        unsafe { let _ = KillTimer(Some(self.hwnd()), TIMER_SHOW); }
        let (tip, glyph) = { let now = Instant::now(); let s = self.feed.status(); (s.tooltip(now), s.glyph(now)) };
        let mut icon = self.icon();
        if icon.available() { return true; }
        let added = icon.show(&tip, glyph); drop(icon);
        self.log(if added { "tray_icon_added" } else { "tray_icon_add_failed" });
        // A running service.state cached before install never poked this window: arm the
        // silence deadline (and catch up the tooltip) now instead of waiting for a new frame.
        if added { self.refresh(); }
        added
    }
    pub fn available(&self) -> bool { self.icon().available() }
    /// UI thread, on window close: a close before the delayed show adds the icon now (Qt `_ensure_tray_icon`).
    pub fn ensure_available(&self) -> bool { self.available() || self.show_now() }
    pub fn show_balloon(&self, title: &str, text: &str, severity: Severity) -> bool {
        let mut icon = self.icon();
        let result = icon.balloon(title, text, severity);
        // Under the icon lock like the alert balloons: a plain balloon has no click target, and
        // an alert submitted around it can never be paired with the wrong generation.
        if result.submitted() { self.target().submitted(None); }
        drop(icon);
        self.log(result.code()); result.submitted()
    }
    /// T-P3-05 `alerts.desktop` balloon from any thread: queued for the UI thread, which submits
    /// it and calls `done` with the result. Only enqueues and posts; never Shell_NotifyIconW here.
    pub fn post_alert_balloon(&self, title: &str, text: &str, severity: Severity, target: &'static str, done: BalloonDone) {
        let mut queue = self.alerts.lock().unwrap_or_else(|e| e.into_inner());
        if queue.len() >= ALERT_QUEUE { drop(queue); self.log("tray_alert_queue_full"); return done(None); }
        queue.push_back(AlertBalloon { title: title.to_owned(), text: text.to_owned(), severity, target, done });
        // Posted under the queue lock: on failure the last entry is this one (the UI thread
        // cannot drain in between).
        if unsafe { PostMessageW(Some(self.hwnd()), ALERT_BALLOON, WPARAM(0), LPARAM(0)) }.is_err() {
            let mine = queue.pop_back(); drop(queue);
            self.log("tray_alert_post_failed");
            if let Some(alert) = mine { (alert.done)(None); }
        }
    }
    /// UI thread (ALERT_BALLOON): submit every queued alert balloon, then report each result.
    fn submit_alerts(&self) {
        let queued: Vec<AlertBalloon> = self.alerts.lock().unwrap_or_else(|e| e.into_inner()).drain(..).collect();
        for alert in queued {
            let result = self.show_alert_balloon(&alert.title, &alert.text, alert.severity, alert.target);
            (alert.done)(Some(result));
        }
    }
    /// The submission itself: the full result for the receipt, and the target a click on this
    /// balloon navigates to (UI thread, serialized by the icon lock).
    fn show_alert_balloon(&self, title: &str, text: &str, severity: Severity, target: &'static str) -> Balloon {
        let mut icon = self.icon();
        let result = icon.balloon(title, text, severity);
        // Set under the icon lock, so a concurrent balloon cannot pair with this target.
        if result.submitted() { self.target().submitted(Some(target)); }
        drop(icon);
        self.log(result.code()); result
    }
    fn target(&self) -> MutexGuard<'_, ClickTarget> { self.balloon_target.lock().unwrap_or_else(|e| e.into_inner()) }
    /// UI thread: the user clicked the balloon. Restore the window, then route the page.
    fn balloon_clicked(&self) {
        let (generation, target) = self.target().click();
        crate::shell_log::fields(&self.data, "tray_balloon_clicked", &[("gen", &generation.to_string()), ("target", target.unwrap_or("none"))]);
        (self.hooks.restore)();
        if let Some(target) = target { (self.hooks.navigate)(target); }
    }
    fn refresh(&self) {
        let now = Instant::now();
        let (tip, glyph, deadline) = { let s = self.feed.status(); (s.tooltip(now), s.glyph(now), s.deadline(now)) };
        let (lost, before) = { let mut icon = self.icon(); let before = icon.glyph(); (icon.set(&tip, glyph), before) };
        // D20 evidence (scripts/p3_shell_smoke.ps1): one shell.log line per icon change.
        if glyph != before { crate::shell_log::fields(&self.data, "tray_icon_state", &[("icon", &glyph.name())]); }
        unsafe {
            if lost {
                // Same bounded retry as an Explorer restart; `readd` re-adds with the latest tip.
                self.log("tray_icon_modify_failed"); self.readd.store(0, Ordering::Release);
                SetTimer(Some(self.hwnd()), TIMER_READD, 1000, None);
            }
            match deadline {
                Some(at) => { SetTimer(Some(self.hwnd()), TIMER_STALE, (at.saturating_duration_since(now).as_millis() as u32).saturating_add(50), None); },
                None => { let _ = KillTimer(Some(self.hwnd()), TIMER_STALE); },
            }
        }
    }
    /// T-P4-C3 fix round 1: the status icons follow the tray window's DPI. On WM_DPICHANGED,
    /// WM_SETTINGCHANGE, WM_DISPLAYCHANGE and TaskbarCreated the set is reloaded when the
    /// small-icon size changed, the icon is sent again with the new handle (`redraw`; not on
    /// TaskbarCreated, whose re-add follows) and the old set is freed.
    fn rescale(&self, redraw: bool) {
        let size = small_icon_size(self.hwnd());
        if self.icon().notify.icons.size == size { return; }
        let fresh = IconSet::load(size, &self.data);
        let (old, lost) = {
            let mut icon = self.icon();
            let old = std::mem::replace(&mut icon.notify.icons, fresh);
            (old, redraw && icon.redraw())
        };
        old.free();
        crate::shell_log::fields(&self.data, "tray_icon_rescaled", &[("size", &size.to_string())]);
        if lost {
            self.log("tray_icon_modify_failed"); self.readd.store(0, Ordering::Release);
            unsafe { SetTimer(Some(self.hwnd()), TIMER_READD, 1000, None); }
        }
    }
    fn readd(&self) {
        // Before the delayed first show (or after shutdown) there is nothing to restore.
        if !self.icon().wanted() { return; }
        let ok = self.icon().taskbar_created();
        if ok { self.readd.store(0, Ordering::Release); self.refresh(); self.log("tray_icon_readded"); return; }
        let attempt = self.readd.fetch_add(1, Ordering::AcqRel) + 1;
        self.log("tray_icon_readd_failed");
        // Explorer may still be starting: bounded retries, then the tray stays unavailable.
        if attempt < READD_ATTEMPTS { unsafe { SetTimer(Some(self.hwnd()), TIMER_READD, 1000, None); } }
    }
    fn menu(&self, x: i32, y: i32) {
        let [show, quit] = self.feed.status().menu();
        let wide = |s: &str| s.encode_utf16().chain(Some(0)).collect::<Vec<u16>>();
        let (show, quit) = (wide(show), wide(quit));
        unsafe {
            let Ok(menu) = CreatePopupMenu() else { self.log("tray_menu_failed"); return; };
            let _ = AppendMenuW(menu, MF_STRING, CMD_SHOW, PCWSTR(show.as_ptr()));
            let _ = AppendMenuW(menu, MF_SEPARATOR, 0, PCWSTR::null());
            let _ = AppendMenuW(menu, MF_STRING, CMD_QUIT, PCWSTR(quit.as_ptr()));
            let _ = SetMenuDefaultItem(menu, CMD_SHOW as u32, 0);
            // KB135788: foreground first so the menu closes on outside clicks; WM_NULL after.
            let _ = SetForegroundWindow(self.hwnd());
            let align = if GetSystemMetrics(SM_MENUDROPALIGNMENT) != 0 { TPM_RIGHTALIGN } else { TPM_LEFTALIGN };
            // T-P3-H fix 1: evidence for an item chosen by a real mouse release (TPM_RIGHTBUTTON
            // takes either button): where the cursor is and which buttons are down as it opens.
            let mut cursor = windows::Win32::Foundation::POINT::default(); let _ = GetCursorPos(&mut cursor);
            let opened = Instant::now();
            crate::shell_log::fields(&self.data, "tray_menu_shown", &[("x", &x.to_string()), ("y", &y.to_string()),
                ("cursor_x", &cursor.x.to_string()), ("cursor_y", &cursor.y.to_string()),
                ("lbutton", if button_down(VK_LBUTTON) { "down" } else { "up" }), ("rbutton", if button_down(VK_RBUTTON) { "down" } else { "up" })]);
            // Selection arrives as WM_COMMAND, the same path the smoke test drives.
            let _ = TrackPopupMenuEx(menu, (TPM_RIGHTBUTTON | TPM_BOTTOMALIGN | align).0, x, y, self.hwnd(), None);
            crate::shell_log::fields(&self.data, "tray_menu_closed", &[("ms", &opened.elapsed().as_millis().to_string())]);
            let _ = PostMessageW(Some(self.hwnd()), WM_NULL, WPARAM(0), LPARAM(0));
            let _ = DestroyMenu(menu);
        }
    }
    fn command(&self, id: usize) {
        match id {
            CMD_SHOW => { self.log("tray_restore_requested"); (self.hooks.restore)(); },
            CMD_QUIT => (self.hooks.quit)(),
            _ => (),
        }
    }
    /// UI thread, after the event loop returned: remove the icon so no ghost stays behind.
    pub fn shutdown(&self) {
        self.icon().remove();
        self.feed.hwnd.store(0, Ordering::Release);
        unsafe { let _ = DestroyWindow(self.hwnd()); }
    }
}

// GetAsyncKeyState without the Win32_UI_Input_KeyboardAndMouse feature (Cargo.toml is not
// touched by T-P3-H); user32 is linked already.
const VK_LBUTTON: i32 = 0x01;
const VK_RBUTTON: i32 = 0x02;
#[link(name = "user32")]
extern "system" { fn GetAsyncKeyState(key: i32) -> i16; }
fn button_down(key: i32) -> bool { unsafe { GetAsyncKeyState(key) < 0 } }

unsafe extern "system" fn window_proc(hwnd: HWND, message: u32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
    let Some(tray) = TRAY.get().filter(|t| t.hwnd == hwnd.0 as isize) else { return DefWindowProcW(hwnd, message, wparam, lparam) };
    match message {
        CALLBACK => {
            let v4 = tray.icon().notify.v4();
            let event = if v4 { (lparam.0 as u32) & 0xffff } else { lparam.0 as u32 };
            match classify(event, v4) {
                Some(Input::Restore) => tray.command(CMD_SHOW),
                Some(Input::BalloonClick) => tray.balloon_clicked(),
                // Not cleared here (see `ClickTarget`); the next submission replaces it.
                Some(Input::BalloonGone) => (),
                Some(Input::Menu) => {
                    let (x, y) = if v4 { ((wparam.0 & 0xffff) as u16 as i16 as i32, ((wparam.0 >> 16) & 0xffff) as u16 as i16 as i32) }
                        else { let mut p = Default::default(); let _ = GetCursorPos(&mut p); (p.x, p.y) };
                    tray.menu(x, y);
                },
                None => (),
            }
            LRESULT(0)
        },
        WM_COMMAND => { tray.command(wparam.0 & 0xffff); LRESULT(0) },
        WM_TIMER => {
            let _ = KillTimer(Some(hwnd), wparam.0);
            match wparam.0 { TIMER_SHOW => { tray.show_now(); }, TIMER_STALE => tray.refresh(), TIMER_READD => tray.readd(), _ => () }
            LRESULT(0)
        },
        REFRESH => { tray.refresh(); LRESULT(0) },
        ALERT_BALLOON => { tray.submit_alerts(); LRESULT(0) },
        #[cfg(debug_assertions)]
        DEV_BALLOON => LRESULT(tray.show_balloon("Tesseract", "P3-03 tray balloon smoke", Severity::Info) as isize),
        // A scaling change: reload the status icons at the new size (fix round 1), then default.
        WM_DPICHANGED | WM_SETTINGCHANGE | WM_DISPLAYCHANGE => { tray.rescale(true); DefWindowProcW(hwnd, message, wparam, lparam) },
        m if m == tray.taskbar_created && m != 0 => { tray.rescale(false); tray.readd.store(0, Ordering::Release); tray.readd(); LRESULT(0) },
        _ => DefWindowProcW(hwnd, message, wparam, lparam),
    }
}

#[cfg(test)] mod tests {
    use super::*;
    use serde_json::json;
    /// The tray facts of a raw frame, picked as the hub picks them (`transport::FACT_TOPICS`, one
    /// list). T-P4-08b (decision wave 5 C3-10): this was `Feed::extract`, a production method kept
    /// only for tests since production takes the facts from `Hub::take_facts`; now it lives here.
    fn facts(frame: &Value) -> Vec<Value> {
        let wanted = |v: &Value| v["t"] == "evt" && v["topic"].as_str().is_some_and(|t| crate::transport::FACT_TOPICS.contains(&t));
        match frame["t"].as_str() {
            Some("evt") if wanted(frame) => vec![frame.clone()],
            Some("batch") => frame["items"].as_array().map(|items| items.iter().filter(|v| wanted(v)).cloned().collect()).unwrap_or_default(),
            _ => Vec::new(),
        }
    }
    fn service(core: bool, stale: bool, phase: &str) -> Value {
        json!({"t":"evt","topic":"service.state","kind":"snapshot","rev":1,"data":{
            "runtime":{"runtime_running":phase=="running","startup_phase":phase},"core":{"running":core},"freshness":{"stale":stale}}})
    }
    fn tiers(carrier: Value) -> Value { json!({"t":"evt","topic":"tiers.summary","kind":"snapshot","rev":1,"data":{"carrier":carrier}}) }
    fn ready(locale: &'static str) -> Status { let mut s = Status::new("en_US"); s.hello(&json!({"lang":locale})); s }

    #[test] fn tooltip_lines_follow_topics_and_qt_offline_rule() {
        let now = Instant::now(); let mut s = ready("zh_CN");
        assert_eq!(s.tooltip(now), "Tesseract\n当前层级：离线\n代理核心 已停止");
        assert!(s.observe(&service(true,false,"running"),now) && s.observe(&tiers(json!("vanguard")),now));
        assert_eq!(s.tooltip(now), "Tesseract\n当前层级：Vanguard\n代理核心 运行中");
        s.observe(&tiers(json!("bastion")),now); assert_eq!(s.tooltip(now), "Tesseract\n当前层级：Bastion\n代理核心 运行中");
        s.observe(&tiers(json!("gateway")),now); assert!(s.tooltip(now).contains("当前层级：Gateway\n"));
        s.observe(&tiers(json!(null)),now); assert_eq!(s.tooltip(now), "Tesseract\n当前层级：离线\n代理核心 运行中");
        s.observe(&tiers(json!("sentinel")),now);
        // Explicit stale, silence while running, a new epoch and a gone sidecar all read offline.
        s.observe(&service(true,true,"running"),now); assert_eq!(s.tooltip(now), "Tesseract\n当前层级：离线\n代理核心 已停止");
        s.observe(&service(true,false,"running"),now); assert!(s.tooltip(now).contains("当前层级：Sentinel\n"));
        assert_eq!(s.deadline(now), Some(now + SERVICE_SILENCE));
        assert!(s.tooltip(now + Duration::from_millis(3100)).contains("离线"));
        s.observe(&service(false,false,"stopped"),now);
        assert_eq!(s.deadline(now), None, "stopped state is change-driven, never silence-stale");
        assert!(s.tooltip(now + Duration::from_secs(600)).ends_with("代理核心 已停止"));
        s.observe(&service(true,false,"running"),now);
        s.gone(); assert_eq!(s.tooltip(now), "Tesseract\n当前层级：离线\n代理核心 已停止");
        s.hello(&json!({"lang":"en_US"})); assert_eq!(s.tooltip(now), "Tesseract\nCurrent tier: offline\nProxy core stopped");
        // Malformed DTOs never pretend to be a value; gaps and unrelated topics change nothing.
        s.observe(&tiers(json!("vanguard")),now);
        s.observe(&json!({"t":"evt","topic":"service.state","kind":"edge","data":{"core":{}}}),now);
        assert_eq!(s.tooltip(now), "Tesseract\nCurrent tier: offline\nProxy core stopped");
        assert!(!s.observe(&json!({"t":"evt","topic":"service.state","kind":"gap","data":{}}),now));
        assert!(!s.observe(&json!({"t":"evt","topic":"route.published","kind":"snapshot","data":{}}),now));
    }
    #[test] fn tooltip_is_truncated_to_sz_tip_with_ellipsis() {
        assert_eq!(fit("abc",127),"abc");
        let long="x".repeat(200); let cut=fit(&long,TIP_UNITS);
        assert_eq!(cut.encode_utf16().count(),TIP_UNITS); assert!(cut.ends_with('…'));
        // Surrogate pairs are never split.
        let emoji="😀".repeat(100); let cut=fit(&emoji,TIP_UNITS);
        assert!(cut.encode_utf16().count()<=TIP_UNITS && cut.ends_with('…') && cut.chars().filter(|c|*c=='😀').count()==63);
        for locale in native_text::LOCALES {
            let now=Instant::now(); let mut s=ready(locale);
            s.observe(&service(true,false,"running"),now); s.observe(&tiers(json!("gateway")),now);
            let tip=s.tooltip(now); assert!(tip.encode_utf16().count()<=TIP_UNITS && tip.lines().count()==3,"{locale}");
            let mut buffer=[1u16;128]; copy(&mut buffer,&"y".repeat(500));
            assert_eq!(buffer[127],0); assert_eq!(buffer[126],'…' as u16);
        }
    }
    #[test] fn menu_language_system_then_hello_then_ui_language() {
        let mut s=Status::new("de_DE"); assert_eq!(s.menu(),["Tesseract anzeigen","Beenden"]);
        s.hello(&json!({"lang":"zh_CN"})); assert_eq!(s.menu(),["显示 Tesseract","退出"]);
        let lang=|l:&str|json!({"t":"evt","topic":"ui.language","kind":"edge","seq":1,"data":{"language":l,"config_rev":3}});
        assert!(s.observe(&lang("ja_JP"),Instant::now())); assert_eq!(s.menu(),["Tesseract を表示","終了"]);
        assert!(!s.observe(&lang("xx_XX"),Instant::now())); assert_eq!(s.locale,"ja_JP");
        s.hello(&json!({"lang":"bogus"})); assert_eq!(s.locale,"ja_JP","invalid hello.lang keeps the current language");
        for locale in native_text::LOCALES {
            s.observe(&lang(locale),Instant::now());
            let [show,quit]=s.menu(); assert!(show.contains("Tesseract") && !quit.is_empty(),"{locale}");
        }
    }
    /// T-P4-08b fix round 1 (S7): this covers the Feed's rendering of tray facts only. Which frames
    /// are tray facts is production's `Hub::take_facts` (transport tests `take_facts`); `facts` is
    /// this module's stand-in for it, so its own picking is not asserted here.
    #[test] fn feed_renders_the_tray_facts_of_a_batch() {
        let batch=json!({"t":"batch","items":[service(true,false,"running"),json!({"t":"evt","topic":"traffic.tick"}),tiers(json!("vanguard"))]});
        let feed=Feed::new("en_US"); feed.hello(&json!({"lang":"zh_CN"})); feed.apply(facts(&batch));
        assert!(feed.status().tooltip(Instant::now()).contains("当前层级：Vanguard\n"));
    }
    #[test] fn contract_fixtures_render_running_tooltip() {
        // The PROTOCOL's own valid fixtures, not hand-made DTOs, drive the tooltip.
        let service:Value=serde_json::from_str(include_str!("../../../bridge_schema/fixtures/valid/topic_service_state.json")).unwrap();
        let tiers:Value=serde_json::from_str(include_str!("../../../bridge_schema/fixtures/valid/topic_tiers_summary.json")).unwrap();
        let feed=Feed::new("en_US"); feed.hello(&json!({"lang":"zh_CN"}));
        feed.apply(facts(&service)); feed.apply(facts(&tiers));
        assert_eq!(feed.status().tooltip(Instant::now()),"Tesseract\n当前层级：Vanguard\n代理核心 运行中");
        let batch=json!({"t":"batch","tick_seq":1,"items":[service,tiers]});
        let feed=Feed::new("en_US"); feed.hello(&json!({"lang":"en_US"})); feed.apply(facts(&batch));
        assert_eq!(feed.status().tooltip(Instant::now()),"Tesseract\nCurrent tier: Vanguard\nProxy core running");
    }
    #[test] fn close_behaviour_three_branches() {
        assert_eq!(close_action(false,false,true),CloseAction::HideToTray);
        assert_eq!(close_action(false,false,false),CloseAction::RequestQuit);
        assert_eq!(close_action(true,false,true),CloseAction::AwaitQuit);
        assert_eq!(close_action(true,false,false),CloseAction::AwaitQuit);
        assert_eq!(close_action(false,true,true),CloseAction::ExitNow);
        // The main.rs wiring: `ensure` runs only for an ordinary close; a failed ensure quits.
        let calls=std::cell::Cell::new(0);
        let ensure=|ok:bool| { let calls=&calls; move || { calls.set(calls.get()+1); ok } };
        assert_eq!(close_decision(true,false,ensure(true)),CloseAction::AwaitQuit);
        assert_eq!(close_decision(false,true,ensure(true)),CloseAction::ExitNow);
        assert_eq!(close_decision(true,true,ensure(true)),CloseAction::ExitNow);
        assert_eq!(calls.get(),0,"requested or finished never creates the tray");
        assert_eq!(close_decision(false,false,ensure(false)),CloseAction::RequestQuit);
        assert_eq!(close_decision(false,false,ensure(true)),CloseAction::HideToTray);
        assert_eq!(calls.get(),2);
    }
    #[test] fn left_click_and_menu_classification() {
        assert_eq!(classify(NIN_SELECT,true),Some(Input::Restore));
        assert_eq!(classify(NIN_KEYSELECT,true),Some(Input::Restore));
        assert_eq!(classify(WM_CONTEXTMENU,true),Some(Input::Menu));
        assert_eq!(classify(WM_LBUTTONUP,true),None,"v4 also reports raw mouse-up; restore only once");
        assert_eq!(classify(WM_LBUTTONDBLCLK,true),None);
        assert_eq!(classify(WM_LBUTTONUP,false),Some(Input::Restore));
        assert_eq!(classify(WM_RBUTTONUP,false),Some(Input::Menu));
        assert_eq!(classify(WM_MOUSEMOVE,false),None);
        for v4 in [true,false] {
            assert_eq!(classify(NIN_BALLOONUSERCLICK,v4),Some(Input::BalloonClick));
            assert_eq!(classify(NIN_BALLOONTIMEOUT,v4),Some(Input::BalloonGone));
            assert_eq!(classify(NIN_BALLOONHIDE,v4),Some(Input::BalloonGone));
            assert_eq!(classify(NIN_BALLOONSHOW,v4),None);
        }
    }
    #[derive(Default)] struct Fake { adds: Vec<String>, modifies: Vec<String>, glyphs: Vec<Glyph>, balloons: Vec<(String,String,Severity)>, deletes: usize, fail_add: bool, fail_modify: bool, fail_balloon: bool }
    impl Notify for Fake {
        fn add(&mut self, tip:&str, glyph:Glyph)->bool { self.adds.push(tip.into()); self.glyphs.push(glyph); !self.fail_add }
        fn modify(&mut self, tip:&str, glyph:Glyph)->bool { self.modifies.push(tip.into()); self.glyphs.push(glyph); !self.fail_modify }
        fn balloon(&mut self, t:&str, x:&str, s:Severity)->bool { self.balloons.push((t.into(),x.into(),s)); !self.fail_balloon }
        fn delete(&mut self)->bool { self.deletes+=1; true }
        fn v4(&self)->bool { true }
    }
    #[test] fn taskbar_created_readds_with_latest_tooltip() {
        let mut icon=Icon::new(Fake::default());
        assert!(!icon.taskbar_created(),"never shown: Explorer restart must not create the icon");
        assert!(icon.notify.adds.is_empty() && !icon.wanted());
        assert!(icon.show("a",Glyph::UNKNOWN)); assert!(icon.wanted()); icon.set("b",Glyph::UNKNOWN); icon.set("b",Glyph::UNKNOWN);
        assert_eq!(icon.notify.modifies,vec!["b"]);
        assert!(icon.taskbar_created()); assert_eq!(icon.notify.adds,vec!["a","b"]);
        icon.notify.fail_add=true; assert!(!icon.taskbar_created()); assert!(!icon.available());
        icon.notify.fail_add=false; assert!(icon.taskbar_created()); assert!(icon.available());
        // A failed NIM_MODIFY (busy Explorer) re-adds at once with the new tip; the tray stays available.
        icon.notify.fail_modify=true; assert!(!icon.set("c",Glyph::UNKNOWN)); assert!(icon.available());
        assert_eq!(icon.notify.adds.last().unwrap(),"c");
        // Modify and the re-add both fail: the icon is lost, reported once for the bounded retry.
        icon.notify.fail_add=true; assert!(icon.set("d",Glyph::UNKNOWN)); assert!(!icon.available(),"vanished icon is reported unavailable");
        assert!(!icon.set("e",Glyph::UNKNOWN),"an unavailable icon is not modified again");
        assert_eq!(icon.notify.modifies.last().unwrap(),"d");
        icon.notify.fail_modify=false; icon.notify.fail_add=false;
        assert!(icon.taskbar_created()); assert_eq!(icon.notify.adds.last().unwrap(),"e");
        icon.remove(); assert_eq!(icon.notify.deletes,1); assert!(!icon.wanted() && !icon.taskbar_created());
    }
    #[test] fn balloon_result_mapping() {
        let mut icon=Icon::new(Fake::default());
        assert_eq!(icon.balloon("T","x",Severity::Info),Balloon::NoIcon);
        icon.show("tip",Glyph::UNKNOWN);
        assert_eq!(icon.balloon("T","x",Severity::Warning),Balloon::Submitted);
        assert_eq!(icon.balloon("Title","  ",Severity::Error),Balloon::Submitted);
        assert_eq!(icon.notify.balloons[1],("Title".into(),"Title".into(),Severity::Error));
        assert_eq!(icon.balloon(" ","",Severity::Info),Balloon::Empty);
        icon.notify.fail_balloon=true; assert_eq!(icon.balloon("T","x",Severity::Info),Balloon::Rejected);
        assert!(Balloon::Submitted.submitted());
        for b in [Balloon::NoIcon,Balloon::Empty,Balloon::Rejected] { assert!(!b.submitted()); }
        icon.notify.fail_balloon=false; icon.balloon(&"t".repeat(100),&"m".repeat(400),Severity::Info);
        let (t,m,_)=icon.notify.balloons.last().unwrap();
        assert_eq!((t.encode_utf16().count(),m.encode_utf16().count()),(63,255));
        assert_eq!(Severity::parse("error"),Severity::Error); assert_eq!(Severity::parse("warning"),Severity::Warning);
        assert_eq!(Severity::parse("success"),Severity::Info); assert_eq!(Severity::parse("??"),Severity::Info);
        // Every PROTOCOL alerts.desktop.severity value (info|warn|error|critical).
        assert_eq!(Severity::parse("info"),Severity::Info);
        assert_eq!(Severity::parse("warn"),Severity::Warning);
        assert_eq!(Severity::parse("error"),Severity::Error);
        assert_eq!(Severity::parse("critical"),Severity::Error);
    }
    #[test] fn balloon_click_targets_follow_generations() {
        let mut slot=ClickTarget::default();
        assert_eq!(slot.click(),(0,None),"no balloon yet");
        assert_eq!(slot.submitted(Some("alerts")),1);
        assert_eq!(slot.submitted(Some("update")),2,"a newer alert balloon replaces the older one");
        assert_eq!(slot.click(),(2,Some("update")),"the click belongs to the balloon on screen");
        assert_eq!(slot.click(),(2,None),"one navigation per balloon");
        // A plain balloon (no target) submitted after an alert: its click only restores.
        slot.submitted(Some("alerts")); assert_eq!(slot.submitted(None),4);
        assert_eq!(slot.click(),(4,None));
        // An alert after the plain one gets its own generation and target again.
        assert_eq!(slot.submitted(Some("overview")),5);
        assert_eq!(slot.click(),(5,Some("overview")));
    }
    #[test] fn balloon_submissions_pair_targets_under_the_icon_lock() {
        // T-P3-H: show_balloon (any thread) and show_alert_balloon (UI thread) both record the
        // click target while still holding the icon lock, so the slot and the balloon on screen
        // are always the same submission.
        let source=include_str!("tray.rs");
        for (name,record) in [("pub fn show_balloon(&self","self.target().submitted(None)"),("fn show_alert_balloon(&self","self.target().submitted(Some(target))")] {
            let body=&source[source.find(name).unwrap()..]; let body=&body[..body.find("\n    }").unwrap()];
            let (lock,submit,unlock)=(body.find("let mut icon = self.icon();").unwrap(),body.find(record).unwrap(),body.find("drop(icon);").unwrap());
            assert!(lock<submit && submit<unlock,"{name}");
        }
    }
    // ---- T-P4-C3 (D20): status icon ----
    fn phase(p: &str) -> Value {
        json!({"t":"evt","topic":"service.state","kind":"snapshot","rev":1,"data":{
            "runtime":{"runtime_running":p=="running","startup_phase":p},"core":{"running":p=="running"},"freshness":{"stale":false}}})
    }
    fn ks(enabled: bool, active: bool, admin: bool, error: Value) -> Value {
        json!({"t":"evt","topic":"killswitch.state","kind":"snapshot","rev":1,"data":{"enabled":enabled,"active":active,"admin":admin,"error":error}})
    }
    #[test] fn every_run_state_has_its_own_icon_and_the_page_rule() {
        let now=Instant::now(); let mut s=ready("en_US");
        assert_eq!(s.glyph(now),Glyph::UNKNOWN,"no service.state yet");
        let mut seen=std::collections::BTreeSet::new();
        for (p,run) in [("collecting",Run::Starting),("core_standby",Run::Starting),("running",Run::Running),("stopping",Run::Stopped),
            ("stopped",Run::Stopped),("failed",Run::Failed),("bogus",Run::Unknown)] {
            s.observe(&phase(p),now);
            assert_eq!(s.glyph(now),Glyph{run,amber:false},"{p}");
            seen.insert(s.glyph(now).index());
        }
        assert_eq!(seen.len(),5,"five run states, five icons");
        // All ten glyphs index the ten generated files, in order.
        for (i,run) in Run::ALL.iter().enumerate() { for amber in [false,true] {
            let g=Glyph{run:*run,amber};
            assert_eq!(g.index(),i*2+amber as usize); assert_eq!(STATUS_ICONS[g.index()].0,g.name());
        } }
        // Offline, stale and a new epoch read unknown (and lose the badge).
        s.observe(&phase("running"),now); s.observe(&ks(false,false,true,Value::Null),now);
        assert_eq!(s.glyph(now),Glyph{run:Run::Running,amber:true});
        assert_eq!(s.glyph(now+Duration::from_millis(3100)),Glyph::UNKNOWN,"silent while running");
        s.observe(&service(true,true,"running"),now); assert_eq!(s.glyph(now),Glyph::UNKNOWN,"explicit stale");
        s.observe(&phase("running"),now); s.hello(&json!({"lang":"en_US"})); assert_eq!(s.glyph(now),Glyph::UNKNOWN,"new epoch");
        s.observe(&phase("stopped"),now); assert_eq!(s.glyph(now),Glyph{run:Run::Stopped,amber:false},"the kill-switch fact of the old epoch is gone");
        s.gone(); assert_eq!(s.glyph(now),Glyph::UNKNOWN,"offline");
    }
    #[test] fn kill_switch_disabled_blocked_or_error_adds_the_amber_badge() {
        let now=Instant::now();
        // (enabled, active, admin, error, run) -> amber, as project.js killswitchOf: disabled / error /
        // blocked are amber; armed, inactive and triggered are not.
        for (enabled,active,admin,error,p,amber) in [
            (false,false,true,Value::Null,"running",true),    // disabled
            (false,false,true,Value::Null,"stopped",true),    // disabled while stopped
            (true,false,true,json!({"code":"x"}),"running",true), // error
            (true,false,true,json!({"code":"x"}),"stopped",true), // error while stopped
            (true,false,false,Value::Null,"running",true),    // blocked (no admin)
            (true,false,false,Value::Null,"stopped",false),   // inactive (not running)
            (true,true,true,Value::Null,"running",false),     // armed
            (true,false,true,Value::Null,"running",false),    // inactive
            (true,false,true,Value::Null,"collecting",false), // inactive while starting
        ] {
            let mut s=ready("en_US"); s.observe(&phase(p),now);
            assert!(s.observe(&ks(enabled,active,admin,error.clone()),now));
            assert_eq!(s.glyph(now).amber,amber,"{enabled} {active} {admin} {error} {p}");
        }
        let mut s=ready("en_US"); s.observe(&phase("running"),now);
        let mut triggered=ks(true,true,false,Value::Null); triggered["data"]["triggered"]=json!(true);
        s.observe(&triggered,now); assert!(!s.glyph(now).amber,"triggered is its own state (red), not amber");
        s.observe(&ks(false,false,true,Value::Null),now); assert!(s.glyph(now).amber);
        assert!(!s.observe(&ks(false,false,true,Value::Null),now),"same fact: nothing to redraw");
        assert!(s.observe(&json!({"t":"evt","topic":"killswitch.state","kind":"gap","seq":2,"data":{}}),now),"a gap drops the fact");
        assert!(!s.glyph(now).amber);
        s.observe(&json!({"t":"evt","topic":"killswitch.state","kind":"edge","seq":3,"data":{"enabled":"yes"}}),now);
        assert!(!s.glyph(now).amber,"a malformed DTO is no fact");
        // The feed takes killswitch.state from batches too.
        let batch=json!({"t":"batch","items":[phase("running"),ks(false,false,true,Value::Null)]});
        assert_eq!(facts(&batch).len(),2);
    }
    #[test] fn icon_changes_with_the_glyph_alone_and_readds_with_it() {
        let mut icon=Icon::new(Fake::default());
        let running=Glyph{run:Run::Running,amber:false}; let badged=Glyph{run:Run::Running,amber:true};
        icon.show("tip",Glyph::UNKNOWN);
        assert!(!icon.set("tip",running)); assert_eq!(icon.notify.modifies,["tip"],"same tooltip, new icon: modified");
        assert!(!icon.set("tip",running)); assert_eq!(icon.notify.modifies.len(),1,"unchanged: no call");
        icon.set("tip",badged); assert_eq!(icon.notify.glyphs.last(),Some(&badged));
        assert!(icon.taskbar_created()); assert_eq!(icon.notify.glyphs.last(),Some(&badged),"Explorer restart re-adds the current icon");
        assert_eq!(icon.glyph(),badged);
    }
    /// Fix round 1: a DPI change reloads the icon set and sends the same tip/glyph again; the
    /// usual loss rule applies, and an icon never added is left alone.
    #[test] fn redraw_resends_the_current_icon_with_the_loss_rule() {
        let mut icon=Icon::new(Fake::default());
        assert!(!icon.redraw()); assert!(icon.notify.modifies.is_empty(),"never shown: nothing to redraw");
        let running=Glyph{run:Run::Running,amber:true};
        icon.show("a",running); assert!(!icon.redraw());
        assert_eq!((icon.notify.modifies.clone(),icon.notify.glyphs.last().copied()),(vec!["a".to_owned()],Some(running)));
        icon.notify.fail_modify=true; assert!(!icon.redraw(),"a failed modify re-adds at once");
        assert_eq!(icon.notify.adds,vec!["a","a"]);
        icon.notify.fail_add=true; assert!(icon.redraw()); assert!(!icon.available());
    }
    #[test] fn status_icon_sets_load_at_every_scale_and_own_their_handles() {
        assert!(small_icon_size(HWND::default())>0,"no window: the session's small-icon size");
        let data=std::env::temp_dir().join(format!("tesseract-tray-icons-{}",std::process::id()));
        for size in [16,20,24,32,40] {
            let set=IconSet::load(size,&data);
            assert_eq!(set.size,size);
            let distinct:std::collections::BTreeSet<isize>=set.handles.iter().copied().collect();
            assert_eq!(distinct.len(),10,"{size}: ten loaded icons, no fallback");
            assert!(set.handles.iter().all(|h|set.owned.contains(h)),"{size}: every status icon is freed with the set");
            set.free();
        }
        assert!(!data.exists(),"no tray_status_icon_failed line was written");
    }
    #[test] fn generated_status_icons_have_every_tray_size() {
        for (name,ico) in STATUS_ICONS {
            for size in [16u32,20,24,32,40,48,64] {
                let bits=ico_entry(ico,size).unwrap_or_else(|| panic!("{name} {size}"));
                assert_eq!(&bits[1..4],b"PNG","{name}: PNG entries");
                let width=u32::from_be_bytes([bits[16],bits[17],bits[18],bits[19]]);
                assert_eq!(width,size,"{name}: exact entry for {size}");
            }
            let w=|b:&[u8]| u32::from_be_bytes([b[16],b[17],b[18],b[19]]);
            assert_eq!(w(ico_entry(ico,18).unwrap()),20,"the smallest larger entry");
            assert_eq!(w(ico_entry(ico,96).unwrap()),64,"else the largest");
        }
        assert!(ico_entry(b"not an icon",16).is_none());
        assert!(ico_entry(&[0,0,1,0,1,0, 16,16,0,0,1,0,32,0, 0xff,0xff,0xff,0x7f, 22,0,0,0],16).is_none(),"bounds are checked");
        let distinct:std::collections::BTreeSet<&[u8]>=STATUS_ICONS.iter().map(|(_,ico)|*ico).collect();
        assert_eq!(distinct.len(),10);
    }
    #[test] fn dev_balloon_entry_is_debug_only() {
        // The DEV_BALLOON arm is compiled only under debug_assertions; release falls to DefWindowProc.
        let source=include_str!("tray.rs");
        let arm=source.find("DEV_BALLOON => LRESULT").unwrap();
        assert!(source[..arm].trim_end().ends_with("#[cfg(debug_assertions)]"));
    }
}
