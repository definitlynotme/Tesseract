//! The notice card window (提醒卡片; plan `.build/提醒模块重设计_20261030.md` §4, §10.5).
//!
//! A second WebView window, label `notice`, loading the embedded `notice.html` (ui/src/notice):
//! borderless, transparent, top-most, off the taskbar and never activated. It carries
//! WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW, is shown with SW_SHOWNOACTIVATE, every SetWindowPos uses
//! SWP_NOACTIVATE, a click answers WM_MOUSEACTIVATE with MA_NOACTIVATE, and should Windows still
//! activate it, the foreground goes straight back to the window that had it (`notice_focus_returned`
//! in shell.log). It is created when the first card comes, sized to the cards the page reports
//! (360 CSS px wide plus shadow room, [`WIDTH_CSS`]; the height the page measures), placed at the
//! bottom-right of the work area of the monitor that holds the main window (the primary monitor
//! while the main window is hidden), at that monitor's DPI, and destroyed 60 s after its last card
//! left. It shares the main window's WebView2 user data folder and browser arguments, so it runs
//! in the same browser process.
//!
//! Page contract (a Channel plus five invokes, accepted from the `notice` window only, main.rs):
//!   page → shell  notice_ready{channel}        the page listens; the waiting cards go out
//!                 notice_rendered{delivery_id}  the card is in the page (counts once the window shows)
//!                 notice_resize{height}        the stack's height in CSS px (0: empty → hidden)
//!                 notice_open{delivery_id}     body or 「查看」: restore the main window, go to the card's target
//!                 notice_dismiss{delivery_id}  the user closed a card (×)
//!   shell → page  {t:"notice:show", language, card}  (notify.rs `Notice::card` / `shell_card`)
//!                 {t:"notice:close", incident}       that incident's card goes, silently
//!                 {t:"notice:clear"}                 a new sidecar epoch: every card but the shell's goes
//!                 {t:"notice:language", language}    ui.language changed: the cards on screen follow
//! Fallback (the card is `Unavailable`, notify.rs submits the tray balloon instead): no
//! `notice.html` among the embedded assets (an asset copy older than the page; Tauri would serve
//! index.html for it, so the exact key is checked), a window that cannot be built, a page that
//! does not call notice_ready within [`LOAD_WAIT`] or does not render a card within [`RENDER_WAIT`].
use crate::notify::{CardDone, CardResult, Cards, TARGETS};
use serde_json::{json, Value};
use std::{cell::RefCell, collections::VecDeque, path::PathBuf, sync::{atomic::{AtomicBool, AtomicIsize, Ordering}, Arc, Condvar, Mutex, MutexGuard, Once, OnceLock, Weak},
    time::{Duration, Instant}};
use tauri::ipc::Channel;
use webview2_com::Microsoft::Web::WebView2::Win32::*;
use windows::core::{w, Interface};
use windows::Win32::Foundation::{HWND, LPARAM, LRESULT, POINT, WPARAM};
use windows::Win32::Graphics::Gdi::{GetMonitorInfoW, MonitorFromPoint, MonitorFromWindow, MONITORINFO, MONITOR_DEFAULTTOPRIMARY};
use windows::Win32::Media::Audio::{PlaySoundW, SND_ALIAS, SND_ASYNC, SND_NODEFAULT};
use windows::Win32::UI::HiDpi::{GetDpiForMonitor, GetDpiForWindow, MDT_EFFECTIVE_DPI};
use windows::Win32::UI::Shell::{DefSubclassProc, RemoveWindowSubclass, SHQueryUserNotificationState, SetWindowSubclass,
    QUNS_BUSY, QUNS_PRESENTATION_MODE, QUNS_QUIET_TIME, QUNS_RUNNING_D3D_FULL_SCREEN};
use windows::Win32::UI::WindowsAndMessaging::*;

pub const LABEL: &str = "notice";
pub const PAGE: &str = "notice.html";
/// The notice window's invokes (main.rs registers them next to the page's `HOST_INVOKES`; its
/// unit test keeps the two in step).
#[cfg_attr(not(test), allow(dead_code))]
pub const INVOKES: [&str; 5] = ["notice_ready", "notice_rendered", "notice_open", "notice_dismiss", "notice_resize"];
/// The card is 360 CSS px wide; the page pads the stack with 12 px towards the screen edge and 24
/// px of shadow room on the other side (`.nstack` in ui/src/notice/notice.css).
pub const WIDTH_CSS: f64 = 396.0;
/// A window that is being built or loaded and has not called `notice_ready` by then is given up.
pub const LOAD_WAIT: Duration = Duration::from_secs(8);
/// A card handed to the page and not rendered by then falls back (and the page is rebuilt).
pub const RENDER_WAIT: Duration = Duration::from_secs(4);
/// The window goes this long after its last card left.
pub const IDLE_DESTROY: Duration = Duration::from_secs(60);
/// After a window that could not be built or loaded, cards use the balloon this long.
pub const BROKEN_FOR: Duration = Duration::from_secs(300);
/// Cards waiting for the page (more means the page never comes: the oldest falls back).
const QUEUE_CAPACITY: usize = 32;
/// Delivery ids whose target `notice_open` can still find.
const TARGET_CAPACITY: usize = 64;

// ---------------------------------------------------------------- the board (no Tauri, no Win32)

/// Where the window is.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Stage { Closed, Creating(Instant), Loading(Instant), Ready, Broken(Instant) }
/// What the host does once the board lock is released.
pub enum Act { Create, Show(Value), Post(Value), Destroy, Done(CardDone, CardResult), Log(&'static str) }
impl std::fmt::Debug for Act {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Create => f.write_str("Create"), Self::Show(card) => write!(f, "Show({})", card["delivery_id"]),
            Self::Post(message) => write!(f, "Post({})", message["t"]), Self::Destroy => f.write_str("Destroy"),
            Self::Done(_, result) => write!(f, "Done({result:?})"), Self::Log(code) => write!(f, "Log({code})"),
        }
    }
}
fn card_id(card: &Value) -> String { card["delivery_id"].as_str().unwrap_or_default().to_owned() }
/// The card window's state machine: the cards waiting for the page, the ones handed to it and not
/// yet rendered, the targets `notice_open` needs, and the idle clock. Every method returns the
/// acts to run after the lock is released (callbacks never run under it).
#[derive(Default)]
pub struct Board { stage: Option<Stage>, waiting: VecDeque<(Value, CardDone)>, sent: Vec<(String, CardDone, Instant)>,
    targets: VecDeque<(String, &'static str)>, empty_since: Option<Instant> }
impl Board {
    pub fn stage(&self) -> Stage { self.stage.unwrap_or(Stage::Closed) }
    fn remember(&mut self, card: &Value) {
        let id = card_id(card);
        let target = card["target"].as_str().and_then(|t| TARGETS.iter().copied().find(|x| *x == t)).unwrap_or("overview");
        self.targets.retain(|(known, _)| *known != id);
        self.targets.push_back((id, target));
        while self.targets.len() > TARGET_CAPACITY { self.targets.pop_front(); }
    }
    /// The target of a card this window was given.
    pub fn target(&self, id: &str) -> Option<&'static str> { self.targets.iter().rev().find(|(known, _)| known == id).map(|(_, t)| *t) }
    pub fn show(&mut self, card: Value, done: CardDone, now: Instant) -> Vec<Act> {
        if let Stage::Broken(until) = self.stage() {
            if now < until { return vec![Act::Done(done, CardResult::Unavailable)]; }
            self.stage = Some(Stage::Closed);
        }
        self.remember(&card);
        if self.stage() == Stage::Ready {
            self.sent.push((card_id(&card), done, now));
            self.empty_since = None;
            return vec![Act::Show(card)];
        }
        let mut acts = Vec::new();
        // An older waiting card of the same incident never reaches the page: the new one replaces it.
        if let Some(at) = self.waiting.iter().position(|(c, _)| c["incident"] == card["incident"]) {
            if let Some((_, old)) = self.waiting.remove(at) { acts.push(Act::Done(old, CardResult::Dropped)); }
        }
        if self.waiting.len() >= QUEUE_CAPACITY {
            if let Some((_, old)) = self.waiting.pop_front() { acts.push(Act::Done(old, CardResult::Unavailable)); }
        }
        self.waiting.push_back((card, done));
        if self.stage() == Stage::Closed { self.stage = Some(Stage::Creating(now)); acts.push(Act::Create); }
        acts
    }
    /// An incident's card goes silently (a waiting one never shows: `Dropped`).
    pub fn close(&mut self, incident: &str) -> Vec<Act> {
        let mut acts = Vec::new();
        while let Some(at) = self.waiting.iter().position(|(c, _)| c["incident"] == incident) {
            if let Some((_, done)) = self.waiting.remove(at) { acts.push(Act::Done(done, CardResult::Dropped)); }
        }
        if self.stage() == Stage::Ready { acts.push(Act::Post(json!({"t":"notice:close","incident":incident}))); }
        acts
    }
    /// A new sidecar epoch: its cards go; the shell's own (`shell: true`) stay.
    pub fn reset(&mut self) -> Vec<Act> {
        let (keep, gone): (VecDeque<_>, VecDeque<_>) = self.waiting.drain(..).partition(|(c, _)| c["shell"] == true);
        self.waiting = keep;
        let mut acts: Vec<Act> = gone.into_iter().map(|(_, done)| Act::Done(done, CardResult::Dropped)).collect();
        if self.stage() == Stage::Ready { acts.push(Act::Post(json!({"t":"notice:clear"}))); }
        acts
    }
    /// The window exists; its page now has [`LOAD_WAIT`] to call `notice_ready`.
    pub fn created(&mut self, now: Instant) { if matches!(self.stage(), Stage::Creating(_)) { self.stage = Some(Stage::Loading(now)); } }
    /// The window could not be made or is given up: every card it holds falls back. `broken`
    /// keeps the window off for [`BROKEN_FOR`].
    pub fn failed(&mut self, now: Instant, broken: bool) -> Vec<Act> {
        self.stage = Some(if broken { Stage::Broken(now + BROKEN_FOR) } else { Stage::Closed });
        self.empty_since = None;
        let mut acts: Vec<Act> = self.waiting.drain(..).map(|(_, done)| Act::Done(done, CardResult::Unavailable)).collect();
        acts.extend(self.sent.drain(..).map(|(_, done, _)| Act::Done(done, CardResult::Unavailable)));
        acts
    }
    /// `notice_ready`: the page listens. Cards handed to an earlier page load (a reload) are lost
    /// with it and fall back; the waiting ones go out now.
    pub fn ready(&mut self, now: Instant) -> Vec<Act> {
        self.stage = Some(Stage::Ready);
        let mut acts: Vec<Act> = self.sent.drain(..).map(|(_, done, _)| Act::Done(done, CardResult::Unavailable)).collect();
        for (card, done) in self.waiting.drain(..) {
            self.sent.push((card_id(&card), done, now));
            acts.push(Act::Show(card));
        }
        self.empty_since = if self.sent.is_empty() { Some(now) } else { None };
        acts
    }
    /// `notice_rendered`: on screen when the window shows, else it falls back.
    pub fn rendered(&mut self, id: &str, visible: bool) -> Option<Act> {
        let at = self.sent.iter().position(|(known, _, _)| known == id)?;
        let (_, done, _) = self.sent.remove(at);
        Some(Act::Done(done, if visible { CardResult::Shown } else { CardResult::Unavailable }))
    }
    /// `notice_resize`: true when the window should show (cards on it). An empty stack starts the
    /// idle clock (not while a card is still on its way to the page).
    pub fn resized(&mut self, height: f64, now: Instant) -> bool {
        if height > 0.0 { self.empty_since = None; return true; }
        if self.stage() == Stage::Ready && self.sent.is_empty() { self.empty_since.get_or_insert(now); }
        false
    }
    /// The clocks: a window that does not load, a card that is not rendered, an idle window.
    pub fn tick(&mut self, now: Instant) -> Vec<Act> {
        let late = |since: Instant, wait: Duration| now.saturating_duration_since(since) >= wait;
        match self.stage() {
            Stage::Creating(since) | Stage::Loading(since) if late(since, LOAD_WAIT) => {
                let mut acts = vec![Act::Log("notice_load_timeout")];
                acts.extend(self.failed(now, true));
                acts.push(Act::Destroy);
                return acts;
            },
            _ => (),
        }
        if self.sent.iter().any(|(_, _, since)| late(*since, RENDER_WAIT)) {
            // A page that does not render is not trusted with the next card: rebuilt on demand.
            let mut acts = vec![Act::Log("notice_render_timeout")];
            acts.extend(self.failed(now, false));
            acts.push(Act::Destroy);
            return acts;
        }
        if self.stage() == Stage::Ready && self.empty_since.is_some_and(|since| late(since, IDLE_DESTROY)) {
            self.stage = Some(Stage::Closed);
            self.empty_since = None;
            return vec![Act::Destroy];
        }
        Vec::new()
    }
    /// When [`Board::tick`] has something to do next, if ever.
    pub fn next_deadline(&self) -> Option<Instant> {
        let mut next: Option<Instant> = None;
        let mut take = |at: Instant| next = Some(next.map_or(at, |n| n.min(at)));
        if let Stage::Creating(since) | Stage::Loading(since) = self.stage() { take(since + LOAD_WAIT); }
        for (_, _, since) in &self.sent { take(*since + RENDER_WAIT); }
        if self.stage() == Stage::Ready { if let Some(since) = self.empty_since { take(since + IDLE_DESTROY); } }
        next
    }
}

/// The card window's position and size in physical pixels: the bottom-right corner of the work
/// area, [`WIDTH_CSS`] wide and `height_css` tall at `dpi`, never larger than the work area.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Rect { pub left: i32, pub top: i32, pub right: i32, pub bottom: i32 }
pub fn placement(work: Rect, dpi: u32, height_css: f64) -> (i32, i32, i32, i32) {
    let scale = if dpi == 0 { 1.0 } else { f64::from(dpi) / 96.0 };
    let width = ((WIDTH_CSS * scale).round() as i32).clamp(1, (work.right - work.left).max(1));
    let height = ((height_css * scale).ceil() as i32).clamp(1, (work.bottom - work.top).max(1));
    (work.right - width, work.bottom - height, width, height)
}
/// `SHQueryUserNotificationState`: the states that hold cards (QUNS_BUSY, QUNS_RUNNING_D3D_FULL_SCREEN,
/// QUNS_PRESENTATION_MODE, QUNS_QUIET_TIME).
pub fn busy_state(state: i32) -> bool { [QUNS_BUSY.0, QUNS_RUNNING_D3D_FULL_SCREEN.0, QUNS_PRESENTATION_MODE.0, QUNS_QUIET_TIME.0].contains(&state) }
/// Whether Windows says the user is busy (full screen, presentation mode, quiet time). A failed
/// query is "not busy": a card is better than a lost one.
pub fn user_busy() -> bool { unsafe { SHQueryUserNotificationState() }.is_ok_and(|state| busy_state(state.0)) }
/// The Windows notification sound once (system volume and sound scheme; nothing when the scheme
/// has none).
pub fn play_sound() { unsafe { let _ = PlaySoundW(w!("Notification.Default"), None, SND_ALIAS | SND_ASYNC | SND_NODEFAULT); } }

// ---------------------------------------------------------------- the host (Tauri + Win32)

/// What the window is built from (main.rs `setup`): the main window's WebView2 profile and
/// browser arguments (one browser process), its reduced-motion flag, the shell.log directory.
pub struct Env { pub app: tauri::AppHandle, pub profile: PathBuf, pub browser_args: String, pub reduced: bool, pub devtools: bool, pub data: PathBuf }
pub struct Hooks {
    /// Shows, unminimizes and focuses the main window (main thread; the RestoreListener path).
    pub restore: Box<dyn Fn() + Send + Sync>,
    /// `host:navigate{page}` once the restored window is visible (presence.rs `Navigate`).
    pub navigate: Box<dyn Fn(&'static str) + Send + Sync>,
    /// The language the cards are written in (hello.lang, then ui.language).
    pub locale: Box<dyn Fn() -> &'static str + Send + Sync>,
}
/// The [`Cards`] of production. Before `attach` (no Tauri app yet) every card falls back.
pub struct Host {
    me: Weak<Host>,
    board: Mutex<Board>,
    /// Wakes the clock thread when a deadline may have moved (paired with `board`).
    wake: Condvar,
    clock: Once,
    channel: Mutex<Option<Channel<Value>>>,
    env: OnceLock<Env>,
    hooks: OnceLock<Hooks>,
    hwnd: AtomicIsize,
    missing_logged: AtomicBool,
}
impl Host {
    pub fn new() -> Arc<Self> {
        Arc::new_cyclic(|me| Self { me: me.clone(), board: Mutex::new(Board::default()), wake: Condvar::new(), clock: Once::new(),
            channel: Mutex::new(None), env: OnceLock::new(), hooks: OnceLock::new(), hwnd: AtomicIsize::new(0), missing_logged: AtomicBool::new(false) })
    }
    pub fn attach(&self, env: Env, hooks: Hooks) {
        let _ = LOG.set(env.data.clone());
        let _ = self.env.set(env);
        let _ = self.hooks.set(hooks);
    }
    fn board(&self) -> MutexGuard<'_, Board> { self.board.lock().unwrap_or_else(|e| e.into_inner()) }
    fn log(&self, code: &str, fields: &[(&str, &str)]) { if let Some(env) = self.env.get() { crate::shell_log::fields(&env.data, code, fields); } }
    fn run(&self, acts: Vec<Act>) {
        for act in acts {
            match act {
                Act::Create => self.create(),
                Act::Show(card) => {
                    let language = self.hooks.get().map_or("zh_CN", |h| (h.locale)());
                    self.post(json!({"t":"notice:show","language":language,"card":card}));
                },
                Act::Post(message) => self.post(message),
                Act::Destroy => self.destroy(),
                Act::Done(done, result) => done(result),
                Act::Log(code) => self.log(code, &[]),
            }
        }
        self.start_clock();
        self.wake.notify_all();
    }
    fn post(&self, message: Value) {
        let channel = self.channel.lock().unwrap_or_else(|e| e.into_inner()).clone();
        if let Some(channel) = channel { if channel.send(message).is_err() { self.log("notice_channel_failed", &[]); } }
    }
    /// Builds the window on the UI thread (synchronously there); the page then has [`LOAD_WAIT`].
    fn create(&self) {
        let Some(env) = self.env.get() else { let acts = self.board().failed(Instant::now(), false); return self.run(acts) };
        let me = self.me.clone();
        let scheduled = env.app.run_on_main_thread(move || {
            let Some(host) = me.upgrade() else { return };
            let Some(env) = host.env.get() else { return };
            match build(env) {
                Ok(hwnd) => {
                    host.hwnd.store(hwnd, Ordering::Release);
                    host.board().created(Instant::now());
                    host.log("notice_window_created", &[]);
                    host.wake.notify_all();
                },
                Err(reason) => {
                    // A missing page is checked again for the next card (cheap) and logged once.
                    let missing = reason == "page_missing";
                    if !missing || !host.missing_logged.swap(true, Ordering::AcqRel) { host.log("notice_window_unavailable", &[("reason", reason)]); }
                    let acts = host.board().failed(Instant::now(), !missing && reason != "label_busy");
                    host.run(acts);
                },
            }
        });
        if scheduled.is_err() { let acts = self.board().failed(Instant::now(), true); self.run(acts); }
    }
    fn destroy(&self) {
        *self.channel.lock().unwrap_or_else(|e| e.into_inner()) = None;
        self.hwnd.store(0, Ordering::Release);
        let Some(env) = self.env.get() else { return };
        let app = env.app.clone();
        let _ = env.app.run_on_main_thread(move || {
            close_controller();
            if let Some(window) = tauri::Manager::get_webview_window(&app, LABEL) { let _ = window.destroy(); }
        });
        self.log("notice_window_destroyed", &[]);
    }
    /// One clock thread for the board's deadlines; it sleeps until the next one (or a wake).
    fn start_clock(&self) {
        let me = self.me.clone();
        self.clock.call_once(move || {
            let _ = std::thread::Builder::new().name("notice-clock".into()).spawn(move || loop {
                let Some(host) = me.upgrade() else { return };
                let acts = {
                    let mut board = host.board();
                    loop {
                        let now = Instant::now();
                        let acts = board.tick(now);
                        if !acts.is_empty() { break acts; }
                        let wait = board.next_deadline().map_or(Duration::from_secs(3600), |at| at.saturating_duration_since(now)).max(Duration::from_millis(20));
                        board = host.wake.wait_timeout(board, wait).unwrap_or_else(|e| e.into_inner()).0;
                    }
                };
                host.run(acts);
            });
        });
    }
    fn window(&self) -> Option<HWND> {
        let raw = self.hwnd.load(Ordering::Acquire);
        (raw != 0).then_some(HWND(raw as _))
    }
    // ---- the notice window's invokes (UI thread; main.rs checks the caller's label) ----
    pub fn ready(&self, channel: Channel<Value>) -> Result<(), &'static str> {
        if self.env.get().is_none() { return Err("not_attached"); }
        *self.channel.lock().unwrap_or_else(|e| e.into_inner()) = Some(channel);
        let acts = self.board().ready(Instant::now());
        self.log("notice_page_ready", &[]);
        self.run(acts);
        Ok(())
    }
    pub fn rendered(&self, id: &str) -> Result<(), &'static str> {
        let visible = self.window().is_some_and(|hwnd| unsafe { IsWindowVisible(hwnd) }.as_bool());
        let act = self.board().rendered(id, visible).ok_or("unknown_delivery")?;
        self.run(vec![act]);
        Ok(())
    }
    pub fn resize(&self, height: f64) -> Result<(), &'static str> {
        if !height.is_finite() || !(0.0..=4096.0).contains(&height) { return Err("invalid_height"); }
        let show = self.board().resized(height, Instant::now());
        self.wake.notify_all();
        let hwnd = self.window().ok_or("no_window")?;
        if !show { unsafe { hide(hwnd); } return Ok(()); }
        let main = self.env.get().and_then(|env| tauri::Manager::get_webview_window(&env.app, "main")).and_then(|w| w.hwnd().ok()).map(|h| HWND(h.0));
        if unsafe { place(hwnd, main, height) } { Ok(()) } else { Err("placement_failed") }
    }
    pub fn open(&self, id: &str) -> Result<(), &'static str> {
        let target = self.board().target(id).ok_or("unknown_delivery")?;
        if let Some(hooks) = self.hooks.get() { (hooks.restore)(); (hooks.navigate)(target); }
        self.log("notice_opened", &[("target", target)]);
        Ok(())
    }
    pub fn dismiss(&self, id: &str) -> Result<(), &'static str> {
        self.board().target(id).ok_or("unknown_delivery")?;
        self.log("notice_dismissed", &[]);
        Ok(())
    }
}
impl Cards for Host {
    fn show(&self, card: Value, done: CardDone) {
        if self.env.get().is_none() { return done(CardResult::Unavailable); }
        let acts = self.board().show(card, done, Instant::now());
        self.run(acts);
    }
    fn close(&self, incident: &str) { let acts = self.board().close(incident); self.run(acts); }
    fn reset(&self) { let acts = self.board().reset(); self.run(acts); }
    fn language(&self, locale: &'static str) {
        let ready = self.board().stage() == Stage::Ready;
        if ready { self.post(json!({"t":"notice:language","language":locale})); }
    }
}

/// The page among the embedded assets, by its exact key (`AssetResolver::get` falls back to
/// index.html for a missing path, which must never load in this window).
fn page_embedded(app: &tauri::AppHandle) -> bool {
    app.asset_resolver().iter().any(|(key, _)| key.trim_start_matches('/') == PAGE)
}
/// UI thread: the window, hidden, its WebView hardened like the main window's, the extended
/// styles set and the activation guard installed.
fn build(env: &Env) -> Result<isize, &'static str> {
    use tauri::Manager;
    if !page_embedded(&env.app) { return Err("page_missing"); }
    // A window of the label still being destroyed: this card falls back, the next one builds.
    if let Some(stale) = env.app.get_webview_window(LABEL) { let _ = stale.destroy(); return Err("label_busy"); }
    let window = tauri::WebviewWindowBuilder::new(&env.app, LABEL, tauri::WebviewUrl::App(PAGE.into()))
        .title("Tesseract").inner_size(WIDTH_CSS, 160.0)
        .decorations(false).transparent(true).shadow(false).resizable(false).maximizable(false).minimizable(false).closable(false)
        .always_on_top(true).skip_taskbar(true).focused(false).focusable(false).visible(false)
        .data_directory(env.profile.clone()).additional_browser_args(&env.browser_args)
        .devtools(env.devtools).zoom_hotkeys_enabled(false)
        .initialization_script(crate::window::init_script(env.reduced))
        .on_navigation(|url| crate::window::internal(url))
        .on_new_window(|_, _| tauri::webview::NewWindowResponse::Deny)
        .build().map_err(|_| "build_failed")?;
    let hwnd = match window.hwnd() { Ok(hwnd) => HWND(hwnd.0), Err(_) => { let _ = window.destroy(); return Err("hwnd_failed"); } };
    let devtools = env.devtools;
    let _ = window.with_webview(move |webview| unsafe {
        let controller = webview.controller();
        CONTROLLER.with(|c| *c.borrow_mut() = Some(controller.clone()));
        let Ok(core) = controller.CoreWebView2() else { return };
        let Ok(settings) = core.Settings() else { return };
        let _ = settings.SetAreDevToolsEnabled(devtools.into());
        let _ = settings.SetAreDefaultContextMenusEnabled(false.into());
        let _ = settings.SetIsStatusBarEnabled(false.into());
        let _ = settings.SetIsZoomControlEnabled(false.into());
        if let Ok(s3) = settings.cast::<ICoreWebView2Settings3>() { let _ = s3.SetAreBrowserAcceleratorKeysEnabled(false); }
        if let Ok(s4) = settings.cast::<ICoreWebView2Settings4>() { let _ = s4.SetIsPasswordAutosaveEnabled(false); let _ = s4.SetIsGeneralAutofillEnabled(false); }
        if let Ok(s5) = settings.cast::<ICoreWebView2Settings5>() { let _ = s5.SetIsPinchZoomEnabled(false); }
        if let Ok(s6) = settings.cast::<ICoreWebView2Settings6>() { let _ = s6.SetIsSwipeNavigationEnabled(false); }
        if let Ok(c4) = controller.cast::<ICoreWebView2Controller4>() { let _ = c4.SetAllowExternalDrop(false); }
    });
    unsafe {
        style(hwnd);
        if !SetWindowSubclass(hwnd, Some(guard_proc), SUBCLASS, 0).as_bool() { crate::log_code(&env.data, "notice_guard_failed"); }
    }
    Ok(hwnd.0 as isize)
}

thread_local! { static CONTROLLER: RefCell<Option<ICoreWebView2Controller>> = const { RefCell::new(None) }; }
/// UI thread: closes the card window's WebView controller (before the window is destroyed, and at
/// exit next to `window::close_controller`, so the browser process can end). True when one was open.
pub fn close_controller() -> bool {
    CONTROLLER.with(|c| c.borrow_mut().take()).is_some_and(|controller| unsafe { controller.Close() }.is_ok())
}

const SUBCLASS: usize = 0x4E54_0001;
const GUARD_TIMER: usize = 0x4E54;
const RETURN_FOCUS: u32 = WM_APP + 0x4E;
/// The last foreground window that was not the card window (sampled when a card shows and every
/// second while one is on screen).
static PREVIOUS: AtomicIsize = AtomicIsize::new(0);
static LOG: OnceLock<PathBuf> = OnceLock::new();

/// The extended styles tao does not set: never activated, never on the taskbar, top-most. Applied
/// again before every show, in case tao rewrote the styles.
unsafe fn style(hwnd: HWND) {
    let current = GetWindowLongPtrW(hwnd, GWL_EXSTYLE);
    let want = (WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TOPMOST).0 as isize;
    let next = (current | want) & !(WS_EX_APPWINDOW.0 as isize);
    if next != current {
        SetWindowLongPtrW(hwnd, GWL_EXSTYLE, next);
        let _ = SetWindowPos(hwnd, Some(HWND_TOPMOST), 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_FRAMECHANGED);
    }
}
unsafe fn remember_foreground(hwnd: HWND) {
    let foreground = GetForegroundWindow();
    if !foreground.is_invalid() && foreground != hwnd { PREVIOUS.store(foreground.0 as isize, Ordering::Release); }
}
/// UI thread: bottom-right of the work area of the main window's monitor (the primary monitor
/// while the main window is hidden or minimized), at that monitor's DPI; shown without activation.
unsafe fn place(hwnd: HWND, main: Option<HWND>, height: f64) -> bool {
    let shown = main.filter(|m| IsWindowVisible(*m).as_bool() && !IsIconic(*m).as_bool());
    let monitor = match shown { Some(m) => MonitorFromWindow(m, MONITOR_DEFAULTTOPRIMARY), None => MonitorFromPoint(POINT { x: 0, y: 0 }, MONITOR_DEFAULTTOPRIMARY) };
    let mut info = MONITORINFO { cbSize: std::mem::size_of::<MONITORINFO>() as u32, ..Default::default() };
    if !GetMonitorInfoW(monitor, &mut info).as_bool() { return false; }
    let (mut dpi, mut dpi_y) = (96u32, 96u32);
    if GetDpiForMonitor(monitor, MDT_EFFECTIVE_DPI, &mut dpi, &mut dpi_y).is_err() || dpi == 0 { dpi = 96; }
    let work = info.rcWork;
    let (x, y, width, height) = placement(Rect { left: work.left, top: work.top, right: work.right, bottom: work.bottom }, dpi, height);
    remember_foreground(hwnd);
    style(hwnd);
    // Onto a monitor of another DPI the first move brings WM_DPICHANGED (tao resizes to the
    // suggested rectangle); the second sets the exact rectangle at the new DPI.
    if GetDpiForWindow(hwnd) != dpi { let _ = SetWindowPos(hwnd, Some(HWND_TOPMOST), x, y, width, height, SWP_NOACTIVATE); }
    let _ = SetWindowPos(hwnd, Some(HWND_TOPMOST), x, y, width, height, SWP_NOACTIVATE);
    if !IsWindowVisible(hwnd).as_bool() {
        let _ = ShowWindow(hwnd, SW_SHOWNOACTIVATE);
        SetTimer(Some(hwnd), GUARD_TIMER, 1000, None);
    }
    true
}
unsafe fn hide(hwnd: HWND) {
    let _ = KillTimer(Some(hwnd), GUARD_TIMER);
    let _ = ShowWindow(hwnd, SW_HIDE);
}
/// The activation guard on the card window: a click never activates it, and if Windows activates
/// it anyway, the foreground goes back to the window that had it.
unsafe extern "system" fn guard_proc(hwnd: HWND, message: u32, wparam: WPARAM, lparam: LPARAM, _: usize, _: usize) -> LRESULT {
    match message {
        WM_MOUSEACTIVATE => return LRESULT(MA_NOACTIVATE as isize),
        WM_TIMER if wparam.0 == GUARD_TIMER => { remember_foreground(hwnd); return LRESULT(0); },
        WM_ACTIVATE if (wparam.0 & 0xffff) as u32 != WA_INACTIVE => { let _ = PostMessageW(Some(hwnd), RETURN_FOCUS, WPARAM(0), LPARAM(0)); },
        RETURN_FOCUS => {
            let previous = HWND(PREVIOUS.load(Ordering::Acquire) as _);
            if !previous.is_invalid() && previous != hwnd && GetForegroundWindow() == hwnd && IsWindow(Some(previous)).as_bool() {
                let returned = SetForegroundWindow(previous).as_bool();
                if let Some(data) = LOG.get() { crate::shell_log::fields(data, "notice_focus_returned", &[("ok", if returned { "true" } else { "false" })]); }
            }
            return LRESULT(0);
        },
        WM_NCDESTROY => {
            let _ = KillTimer(Some(hwnd), GUARD_TIMER);
            let _ = RemoveWindowSubclass(hwnd, Some(guard_proc), SUBCLASS);
        },
        _ => (),
    }
    DefSubclassProc(hwnd, message, wparam, lparam)
}

#[cfg(test)] mod tests {
    use super::*;
    use std::sync::Arc;
    fn card(id: &str, incident: &str) -> Value { json!({"delivery_id":id,"incident":incident,"target":"alerts","shell":false}) }
    /// A `done` that records its result.
    fn recorder() -> (CardDone, Arc<Mutex<Vec<CardResult>>>) {
        let out = Arc::new(Mutex::new(Vec::new())); let o = out.clone();
        (Box::new(move |r| o.lock().unwrap().push(r)), out)
    }
    fn names(acts: &[Act]) -> Vec<String> { acts.iter().map(|a| format!("{a:?}")).collect() }
    fn run(acts: Vec<Act>) -> Vec<String> {
        let shown = names(&acts);
        for act in acts { if let Act::Done(done, result) = act { done(result); } }
        shown
    }
    #[test] fn the_first_card_builds_the_window_and_waits_for_the_page() {
        let mut b = Board::default(); let t0 = Instant::now();
        let (done, out) = recorder();
        assert_eq!(names(&b.show(card("d1", "a"), done, t0)), ["Create"]);
        assert_eq!(b.stage(), Stage::Creating(t0));
        let (done2, out2) = recorder();
        assert_eq!(run(b.show(card("d2", "b"), done2, t0)), Vec::<String>::new(), "one window: no second Create");
        b.created(t0);
        assert_eq!(b.stage(), Stage::Loading(t0));
        assert_eq!(b.next_deadline(), Some(t0 + LOAD_WAIT));
        assert_eq!(names(&b.ready(t0)), ["Show(\"d1\")", "Show(\"d2\")"]);
        assert_eq!(b.next_deadline(), Some(t0 + RENDER_WAIT));
        // Rendered with the window on screen: shown. A repeat or a stranger is unknown.
        run(b.rendered("d1", true).into_iter().collect());
        assert!(b.rendered("d1", true).is_none() && b.rendered("zz", true).is_none());
        assert_eq!(*out.lock().unwrap(), [CardResult::Shown]);
        // Rendered while the window is hidden (the resize never showed it): it falls back.
        run(b.rendered("d2", false).into_iter().collect());
        assert_eq!(*out2.lock().unwrap(), [CardResult::Unavailable]);
        assert_eq!(b.target("d1"), Some("alerts"));
        // Ready: later cards go out at once.
        let (done3, _) = recorder();
        assert_eq!(names(&b.show(card("d3", "c"), done3, t0)), ["Show(\"d3\")"]);
    }
    #[test] fn a_waiting_card_of_the_same_incident_is_replaced_and_close_drops_it() {
        let mut b = Board::default(); let t0 = Instant::now();
        let (d1, o1) = recorder(); let (d2, o2) = recorder(); let (d3, o3) = recorder();
        run(b.show(card("d1", "outage"), d1, t0));
        run(b.show(card("d2", "outage"), d2, t0));
        assert_eq!(*o1.lock().unwrap(), [CardResult::Dropped], "the older frame never reaches the page");
        run(b.show(card("d3", "fallback"), d3, t0));
        assert_eq!(names(&b.close("fallback")), ["Done(Dropped)"], "not ready yet: no message, the waiting card is dropped");
        let _ = o3;
        b.created(t0);
        assert_eq!(names(&b.ready(t0)), ["Show(\"d2\")"]);
        assert_eq!(names(&b.close("outage")), ["Post(\"notice:close\")"]);
        assert!(o2.lock().unwrap().is_empty(), "d2 is with the page: closing it says nothing about it");
    }
    #[test] fn reset_keeps_the_shell_cards() {
        let mut b = Board::default(); let t0 = Instant::now();
        let (d1, o1) = recorder(); let (d2, o2) = recorder();
        run(b.show(card("d1", "outage"), d1, t0));
        let mut shell = card("shell-service_stopped-1", "service_stopped"); shell["shell"] = json!(true);
        run(b.show(shell, d2, t0));
        assert_eq!(run(b.reset()), ["Done(Dropped)"]);
        assert_eq!(*o1.lock().unwrap(), [CardResult::Dropped]);
        b.created(t0);
        assert_eq!(names(&b.ready(t0)), ["Show(\"shell-service_stopped-1\")"]);
        assert_eq!(names(&b.reset()), ["Post(\"notice:clear\")"]);
        assert!(o2.lock().unwrap().is_empty());
    }
    #[test] fn a_window_that_does_not_load_falls_back_and_stays_off_for_a_while() {
        let mut b = Board::default(); let t0 = Instant::now();
        let (d1, o1) = recorder();
        run(b.show(card("d1", "a"), d1, t0));
        assert!(b.tick(t0 + LOAD_WAIT - Duration::from_millis(1)).is_empty());
        assert_eq!(run(b.tick(t0 + LOAD_WAIT)), ["Log(notice_load_timeout)", "Done(Unavailable)", "Destroy"]);
        assert_eq!(*o1.lock().unwrap(), [CardResult::Unavailable]);
        let t1 = t0 + LOAD_WAIT;
        let (d2, o2) = recorder();
        assert_eq!(run(b.show(card("d2", "b"), d2, t1)), ["Done(Unavailable)"], "broken: straight to the balloon");
        assert_eq!(*o2.lock().unwrap(), [CardResult::Unavailable]);
        let (d3, _) = recorder();
        assert_eq!(names(&b.show(card("d3", "c"), d3, t1 + BROKEN_FOR)), ["Create"], "tried again after the pause");
        // A build failure (failed, not broken: the page was missing) leaves no pause.
        let mut b = Board::default();
        let (d4, o4) = recorder();
        run(b.show(card("d4", "a"), d4, t0));
        run(b.failed(t0, false));
        assert_eq!(*o4.lock().unwrap(), [CardResult::Unavailable]);
        let (d5, _) = recorder();
        assert_eq!(names(&b.show(card("d5", "a"), d5, t0)), ["Create"]);
    }
    #[test] fn a_card_the_page_does_not_render_falls_back_and_the_window_is_rebuilt() {
        let mut b = Board::default(); let t0 = Instant::now();
        let (d1, o1) = recorder();
        run(b.show(card("d1", "a"), d1, t0)); b.created(t0); run(b.ready(t0));
        assert_eq!(run(b.tick(t0 + RENDER_WAIT)), ["Log(notice_render_timeout)", "Done(Unavailable)", "Destroy"]);
        assert_eq!(*o1.lock().unwrap(), [CardResult::Unavailable]);
        assert_eq!(b.stage(), Stage::Closed);
        let (d2, _) = recorder();
        assert_eq!(names(&b.show(card("d2", "b"), d2, t0 + RENDER_WAIT)), ["Create"]);
        // A page reload (notice_ready again) loses what the old page had not rendered.
        let mut b = Board::default();
        let (d3, o3) = recorder();
        run(b.show(card("d3", "a"), d3, t0)); b.created(t0); run(b.ready(t0));
        assert_eq!(run(b.ready(t0)), ["Done(Unavailable)"]);
        assert_eq!(*o3.lock().unwrap(), [CardResult::Unavailable]);
    }
    #[test] fn the_window_goes_sixty_seconds_after_its_last_card() {
        let mut b = Board::default(); let t0 = Instant::now();
        let (d1, _) = recorder();
        run(b.show(card("d1", "a"), d1, t0)); b.created(t0); run(b.ready(t0));
        assert!(!b.resized(0.0, t0), "the page's first empty report while d1 is on its way: no idle clock");
        assert_eq!(b.next_deadline(), Some(t0 + RENDER_WAIT));
        run(b.rendered("d1", true).into_iter().collect());
        assert!(b.resized(132.0, t0));
        assert_eq!(b.next_deadline(), None);
        let t1 = t0 + Duration::from_secs(5);
        assert!(!b.resized(0.0, t1));
        assert_eq!(b.next_deadline(), Some(t1 + IDLE_DESTROY));
        // A new card in between stops the clock.
        let (d2, _) = recorder();
        run(b.show(card("d2", "b"), d2, t1 + Duration::from_secs(10)));
        run(b.rendered("d2", true).into_iter().collect());
        assert!(b.resized(140.0, t1 + Duration::from_secs(10)));
        assert!(b.tick(t1 + IDLE_DESTROY).is_empty());
        let t2 = t1 + Duration::from_secs(20);
        b.resized(0.0, t2);
        assert!(b.tick(t2 + IDLE_DESTROY - Duration::from_millis(1)).is_empty());
        assert_eq!(names(&b.tick(t2 + IDLE_DESTROY)), ["Destroy"]);
        assert_eq!(b.stage(), Stage::Closed);
        let (d3, _) = recorder();
        assert_eq!(names(&b.show(card("d3", "c"), d3, t2 + IDLE_DESTROY)), ["Create"], "rebuilt on demand");
    }
    #[test] fn targets_are_bounded_and_unknown_ones_default_to_the_overview() {
        let mut b = Board::default(); let t0 = Instant::now();
        for i in 0..TARGET_CAPACITY + 3 { let (d, _) = recorder(); run(b.show(card(&format!("d{i}"), &format!("i{i}")), d, t0)); }
        assert_eq!(b.target("d0"), None);
        assert_eq!(b.target(&format!("d{}", TARGET_CAPACITY + 2)), Some("alerts"));
        let (d, _) = recorder();
        run(b.show(json!({"delivery_id":"x","incident":"x","target":"elsewhere"}), d, t0));
        assert_eq!(b.target("x"), Some("overview"));
    }
    #[test] fn placement_is_the_bottom_right_corner_at_the_monitor_dpi() {
        let work = Rect { left: 0, top: 0, right: 2560, bottom: 1392 };
        assert_eq!(placement(work, 96, 140.0), (2560 - 396, 1392 - 140, 396, 140));
        // The user's 150 %: 396 × 1.5 = 594 wide; 140.2 CSS px rounds up to 211 device pixels.
        assert_eq!(placement(work, 144, 140.2), (2560 - 594, 1392 - 211, 594, 211));
        // A second monitor to the left (negative coordinates) and a taller stack than the work area.
        let left = Rect { left: -1920, top: 0, right: 0, bottom: 1040 };
        assert_eq!(placement(left, 96, 5000.0), (-396, 0, 396, 1040));
        assert_eq!(placement(work, 0, 100.0), (2560 - 396, 1392 - 100, 396, 100), "an unknown DPI reads as 100 %");
        // The CSS that pads the stack agrees with WIDTH_CSS.
        let css = include_str!("../../../ui/src/notice/notice.css");
        assert!(css.contains("--nc-width: 396px"), "notice.css --nc-width");
    }
    #[test] fn the_notice_capability_is_its_own_and_grants_nothing() {
        let cap: Value = serde_json::from_str(include_str!("../capabilities/notice.json")).unwrap();
        assert_eq!((cap["identifier"].as_str(), &cap["windows"], &cap["permissions"]), (Some("notice"), &json!([LABEL]), &json!([])));
        assert!(cap.get("remote").is_none() && cap.get("webviews").is_none());
        let config: Value = serde_json::from_str(include_str!("../tauri.conf.json")).unwrap();
        assert_eq!(config["app"]["security"]["capabilities"], json!(["main", "notice"]));
        let main: Value = serde_json::from_str(include_str!("../capabilities/main.json")).unwrap();
        assert_eq!(main["windows"], json!(["main"]), "the main capability never covers the card window");
    }
    #[test] fn busy_states_are_the_four_the_plan_names() {
        for state in [QUNS_BUSY, QUNS_RUNNING_D3D_FULL_SCREEN, QUNS_PRESENTATION_MODE, QUNS_QUIET_TIME] { assert!(busy_state(state.0), "{state:?}"); }
        use windows::Win32::UI::Shell::{QUNS_ACCEPTS_NOTIFICATIONS, QUNS_APP, QUNS_NOT_PRESENT};
        for state in [QUNS_ACCEPTS_NOTIFICATIONS, QUNS_APP, QUNS_NOT_PRESENT] { assert!(!busy_state(state.0), "{state:?}"); }
        assert_eq!(INVOKES.len(), 5);
    }
}
