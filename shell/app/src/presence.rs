//! T-P3-05 window presence: `ui.presence` (PROTOCOL §10, §13 "presence") and `host:visibility`.
//!
//! Only the shell reports `visible`/`focused`, read from the real main window; the page only
//! names its routed page through `host_set_page`, checked against the PROTOCOL page whitelist.
//! The worker sends the full value on every change and once more after each hello (reconnect).
use serde_json::{json, Value};
use std::{path::PathBuf, sync::{atomic::{AtomicIsize, Ordering}, Arc, Mutex}, time::{Duration, Instant}};
use windows::Win32::Foundation::HWND;
use windows::Win32::UI::WindowsAndMessaging::{GetForegroundWindow, IsIconic, IsWindowVisible};

/// PROTOCOL §10 `ui.presence.page`.
pub const PAGES: [&str; 7] = ["overview", "nodes", "settings", "logs", "alerts", "about", "none"];
/// How often the worker and the visibility monitor sample the window.
pub const SAMPLE: Duration = Duration::from_millis(100);
pub fn page_id(value: &str) -> Option<&'static str> { PAGES.iter().copied().find(|p| *p == value) }

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Window { pub visible: bool, pub focused: bool }
impl Window {
    /// alerts.desktop routing: the user is looking at the window (shown, not minimized, active).
    pub fn foreground(self) -> bool { self.visible && self.focused }
}
/// The window facts; `None` while the main window does not exist yet (or, T-P3-07, is still
/// cloaked waiting for `host_ready`).
pub trait Probe: Send + Sync { fn window(&self) -> Option<Window>; }

/// Qt `_overview_page_visible` semantics: shown and not minimized; focused = the foreground window.
#[derive(Default)]
pub struct Win32Probe { hwnd: AtomicIsize }
impl Win32Probe {
    pub fn set(&self, hwnd: isize) { self.hwnd.store(hwnd, Ordering::Release); }
}
impl Probe for Win32Probe {
    fn window(&self) -> Option<Window> {
        let raw = self.hwnd.load(Ordering::Acquire);
        if raw == 0 { return None; }
        // T-P3-07: while the main window is still cloaked for host_ready (at most 1.5 s per window)
        // nothing truthful can be said yet: no ui.presence, no host:visibility, notices go to the
        // tray, as before the window existed. The first value after the reveal is the real one.
        if crate::webview::app_cloaked(raw) { return None; }
        let hwnd = HWND(raw as _);
        unsafe {
            let visible = IsWindowVisible(hwnd).as_bool() && !IsIconic(hwnd).as_bool();
            Some(Window { visible, focused: visible && GetForegroundWindow() == hwnd })
        }
    }
}
impl<P: Probe + ?Sized> Probe for Arc<P> { fn window(&self) -> Option<Window> { (**self).window() } }

/// The routed page the page reported (`host_set_page`), bound to the page connection
/// (`Hub::page_generation`) that reported it: a reloaded page is "none" until it says otherwise.
pub struct PageSlot(Mutex<(u64, &'static str)>);
impl Default for PageSlot { fn default() -> Self { Self(Mutex::new((0, "none"))) } }
impl PageSlot {
    /// The page id for the current page connection `generation`.
    pub fn get(&self, generation: u64) -> &'static str {
        let (owner, page) = *self.0.lock().unwrap_or_else(|e| e.into_inner());
        if owner == generation { page } else { "none" }
    }
    /// Only PROTOCOL page IDs are accepted; anything else leaves the value unchanged.
    pub fn set(&self, page: &str, generation: u64) -> Result<&'static str, &'static str> {
        let page = page_id(page).ok_or("invalid_page")?;
        *self.0.lock().unwrap_or_else(|e| e.into_inner()) = (generation, page);
        Ok(page)
    }
}

/// How long a clicked balloon's `host:navigate` waits for the restored window to be visible.
pub const NAVIGATE_WAIT: Duration = Duration::from_secs(3);
/// A clicked alert balloon's target. The tray's restore only signals the restore event (the
/// window is shown asynchronously), so `host:navigate` goes out once the window is visible —
/// after the `host:visibility{true}` of the same sample — or after [`NAVIGATE_WAIT`] anyway.
#[derive(Default)]
pub struct Navigate(Mutex<Option<(&'static str, Instant)>>);
impl Navigate {
    pub fn request(&self, page: &'static str, now: Instant) { *self.0.lock().unwrap_or_else(|e| e.into_inner()) = Some((page, now)); }
    /// The target to send now and whether the window was visible by then.
    pub fn due(&self, visible: bool, now: Instant) -> Option<(&'static str, bool)> {
        let mut slot = self.0.lock().unwrap_or_else(|e| e.into_inner());
        let (page, since) = (*slot)?;
        if !visible && now.saturating_duration_since(since) < NAVIGATE_WAIT { return None; }
        *slot = None;
        Some((page, visible))
    }
}

/// shell.log gate for `presence_sent`: a line when `visible` or `page` changes (or the first of a
/// session); focus-only changes are counted into the next line instead of one line each, so
/// alt-tabbing does not roll crash and quit diagnostics out of the 3 MiB shell.log.
#[derive(Default)]
pub struct LogGate { logged: Option<(bool, String)>, focus_only: u64 }
impl LogGate {
    /// Some(focus-only changes since the last line) when this value is worth a line.
    pub fn note(&mut self, args: &Value) -> Option<u64> {
        let key = (args["visible"].as_bool().unwrap_or(false), args["page"].as_str().unwrap_or("none").to_owned());
        if self.logged.as_ref() == Some(&key) { self.focus_only += 1; return None; }
        self.logged = Some(key);
        Some(std::mem::take(&mut self.focus_only))
    }
}

/// Worker side, one per sidecar session (a new hello starts a new one, so a reconnect reports
/// the full value again): dedupes the reported value.
#[derive(Default)]
pub struct Reporter { sent: Option<(Window, &'static str)> }
impl Reporter {
    /// The `ui.presence` args when the value differs from the last one reported this session.
    /// Without a main window there is nothing truthful to report.
    pub fn sample(&mut self, window: Option<Window>, page: &'static str) -> Option<Value> {
        let window = window?;
        if self.sent == Some((window, page)) { return None; }
        self.sent = Some((window, page));
        Some(json!({"visible": window.visible, "focused": window.focused, "page": page}))
    }
}
/// `ui.presence` request (shell-only command; P2 envelope like `app.sessionEnd`).
pub fn request_frame(id: u64, args: Value) -> Value {
    json!({"t":"req","id":id,"cmd":"ui.presence","args":args,"from":"shell","req_id":format!("shell-presence-{id}"),"owner":"shell"})
}
/// shell.log fields of a reported presence value.
pub fn fields(args: &Value) -> Vec<(&'static str, String)> {
    vec![("visible", args["visible"].to_string()), ("focused", args["focused"].to_string()),
        ("page", args["page"].as_str().unwrap_or("none").to_owned())]
}

/// Page-facing `host:visibility{visible}` on every visible change (hide to tray, restore,
/// minimize); the page pauses animations and timers while hidden.
#[derive(Default)]
pub struct Visibility { last: Option<bool> }
impl Visibility {
    pub fn observe(&mut self, window: Option<Window>) -> Option<Value> {
        let visible = window?.visible;
        if self.last == Some(visible) { return None; }
        self.last = Some(visible);
        Some(json!({"t":"host:visibility","visible":visible}))
    }
}
/// Samples the window for the process lifetime and publishes `host:visibility` changes. The hub
/// remembers the latest one, so a page that connects later still learns the current value.
/// A clicked balloon's `host:navigate` (Desk::navigate) goes out from here, after the window
/// became visible.
pub fn spawn_visibility(desk: Arc<crate::notify::Desk>, hub: Arc<Mutex<crate::transport::Hub>>, data: PathBuf) {
    std::thread::spawn(move || {
        let mut visibility = Visibility::default();
        loop {
            let window = desk.probe.window();
            if let Some(frame) = visibility.observe(window) {
                crate::shell_log::fields(&data, "page_visibility", &[("visible", &frame["visible"].to_string())]);
                hub.lock().unwrap_or_else(|e| e.into_inner()).host_sticky("visibility", frame);
            }
            if let Some((page, visible)) = desk.navigate.due(window.is_some_and(|w| w.visible), Instant::now()) {
                crate::shell_log::fields(&data, "tray_balloon_navigate", &[("page", page), ("visible", if visible { "true" } else { "false" })]);
                hub.lock().unwrap_or_else(|e| e.into_inner()).host(json!({"t":"host:navigate","page":page}));
            }
            std::thread::sleep(SAMPLE);
        }
    });
}

#[cfg(test)] mod tests {
    use super::*;
    const SHOWN: Window = Window { visible: true, focused: false };
    const ACTIVE: Window = Window { visible: true, focused: true };
    const HIDDEN: Window = Window { visible: false, focused: false };
    #[test] fn page_whitelist_is_the_protocol_list() {
        for page in PAGES { assert_eq!(page_id(page), Some(page)); }
        for bad in ["", "Overview", "update", "overview ", "../logs", "nodes\u{0}"] { assert_eq!(page_id(bad), None, "{bad:?}"); }
        let slot = PageSlot::default();
        assert_eq!(slot.get(1), "none");
        assert_eq!(slot.set("nodes", 1), Ok("nodes")); assert_eq!(slot.get(1), "nodes");
        assert_eq!(slot.set("update", 1), Err("invalid_page"));
        assert_eq!(slot.get(1), "nodes", "a rejected page id leaves the last valid one");
        // A reloaded page (new connection) has not named its page yet.
        assert_eq!(slot.get(2), "none");
        assert_eq!(slot.set("logs", 2), Ok("logs")); assert_eq!(slot.get(2), "logs");
        assert_eq!(slot.get(1), "none", "never the other connection's page");
    }
    #[test] fn balloon_navigate_waits_for_the_visible_window() {
        let nav = Navigate::default(); let t0 = Instant::now();
        assert_eq!(nav.due(true, t0), None, "nothing requested");
        nav.request("alerts", t0);
        assert_eq!(nav.due(false, t0 + Duration::from_millis(100)), None, "restore still in flight");
        assert_eq!(nav.due(true, t0 + Duration::from_millis(200)), Some(("alerts", true)));
        assert_eq!(nav.due(true, t0 + Duration::from_millis(300)), None, "sent once");
        // A restore that never shows the window: navigate anyway after the wait.
        nav.request("update", t0);
        assert_eq!(nav.due(false, t0 + NAVIGATE_WAIT - Duration::from_millis(1)), None);
        assert_eq!(nav.due(false, t0 + NAVIGATE_WAIT), Some(("update", false)));
    }
    #[test] fn presence_log_lines_only_on_visible_or_page_changes() {
        let mut gate = LogGate::default();
        let v = |visible: bool, focused: bool, page: &str| json!({"visible":visible,"focused":focused,"page":page});
        assert_eq!(gate.note(&v(true, true, "none")), Some(0), "the first value of a session");
        assert_eq!(gate.note(&v(true, false, "none")), None);
        assert_eq!(gate.note(&v(true, true, "none")), None);
        assert_eq!(gate.note(&v(true, true, "logs")), Some(2), "page change: a line carrying the two focus-only changes");
        assert_eq!(gate.note(&v(false, false, "logs")), Some(0));
    }
    #[test] fn presence_dedupes_and_resends_after_reconnect() {
        let mut r = Reporter::default();
        assert_eq!(r.sample(None, "overview"), None, "no main window: nothing is claimed");
        assert_eq!(r.sample(Some(ACTIVE), "overview"), Some(json!({"visible":true,"focused":true,"page":"overview"})));
        assert_eq!(r.sample(Some(ACTIVE), "overview"), None, "unchanged value is not sent again");
        assert_eq!(r.sample(Some(SHOWN), "overview").unwrap()["focused"], false);
        assert_eq!(r.sample(Some(SHOWN), "logs").unwrap()["page"], "logs");
        assert_eq!(r.sample(Some(HIDDEN), "logs").unwrap(), json!({"visible":false,"focused":false,"page":"logs"}));
        assert_eq!(r.sample(Some(HIDDEN), "logs"), None);
        let mut r = Reporter::default(); // the worker's next session
        assert_eq!(r.sample(Some(HIDDEN), "logs"), Some(json!({"visible":false,"focused":false,"page":"logs"})), "full value after a reconnect");
        let frame = request_frame(7, json!({"visible":true,"focused":false,"page":"none"}));
        assert_eq!((frame["cmd"].as_str(), frame["from"].as_str(), frame["owner"].as_str()), (Some("ui.presence"), Some("shell"), Some("shell")));
        assert_eq!(fields(&frame["args"]), vec![("visible", "true".into()), ("focused", "false".into()), ("page", "none".into())]);
    }
    #[test] fn foreground_needs_visible_and_focused() {
        assert!(ACTIVE.foreground());
        assert!(!SHOWN.foreground() && !HIDDEN.foreground());
        assert!(!Window { visible: false, focused: true }.foreground());
    }
    #[test] fn visibility_frames_only_on_change() {
        let mut v = Visibility::default();
        assert_eq!(v.observe(None), None);
        assert_eq!(v.observe(Some(SHOWN)), Some(json!({"t":"host:visibility","visible":true})));
        assert_eq!(v.observe(Some(ACTIVE)), None, "focus alone is not a visibility change");
        assert_eq!(v.observe(Some(HIDDEN)).unwrap()["visible"], false);
        assert_eq!(v.observe(Some(HIDDEN)), None);
        assert_eq!(v.observe(Some(SHOWN)).unwrap()["visible"], true);
    }
    #[test] fn unset_window_probe_reports_nothing() {
        assert_eq!(Win32Probe::default().window(), None);
    }
}
