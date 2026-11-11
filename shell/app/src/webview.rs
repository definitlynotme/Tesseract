//! T-P3-07 WebView2 lifecycle (plan §2.2.1 steps 1/3/6, §2.2.6, §7.3, §7.9, §7.10).
//!
//! - Release builds drop the WebView2 environment overrides from their own process before any
//!   WebView2 environment exists; the process carries the AUMID `Tesseract.ProxyApp`.
//! - `GetAvailableCoreWebView2BrowserVersionString` decides whether a runtime is installed.
//! - The main window is DWM-cloaked from creation until the page reports `host_ready` (at most
//!   1.5 s; a timeout reveals it anyway and is logged).
//! - `ProcessFailed` is classified: renderer exited / unresponsive → reload (the page rebuilds from
//!   the shell cache through `rt_connect`), browser exited → recreate the window with backoff,
//!   anything else → log only.
//! - At exit the shell waits (bounded) for the environment's `BrowserProcessExited`, so the user
//!   data folder is free for the next start.
use std::{cell::RefCell, collections::VecDeque, path::{Path, PathBuf}, sync::{atomic::{AtomicBool, AtomicU64, Ordering}, Arc, Condvar, Mutex},
    time::{Duration, Instant}};
use webview2_com::Microsoft::Web::WebView2::Win32::ICoreWebView2Environment;

pub const AUMID: &str = "Tesseract.ProxyApp";
/// §2.2.1 step 1: removed by release builds before any WebView2 environment is created. Debug
/// (test) builds keep them so a developer can point WebView2 at another runtime or profile.
pub const RELEASE_SCRUB: [&str; 4] = ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "WEBVIEW2_USER_DATA_FOLDER",
    "WEBVIEW2_BROWSER_EXECUTABLE_FOLDER", "WEBVIEW2_RELEASE_CHANNEL_PREFERENCE"];
/// T-P3-01: the script-debugger attach switches are removed in every build (unchanged).
pub const ALWAYS_SCRUB: [&str; 2] = ["WEBVIEW2_PIPE_FOR_SCRIPT_DEBUGGER", "WEBVIEW2_WAIT_FOR_SCRIPT_DEBUGGER"];
/// HRESULT_FROM_WIN32(ERROR_FILE_NOT_FOUND): what the loader reports when no runtime is installed.
pub const MISSING_HRESULT: i32 = 0x8007_0002u32 as i32;
/// Offline-installer link of the missing-runtime dialog. It goes through `host.openUrl`'s whitelist
/// (PROTOCOL §13 item 5), which has no Microsoft host, so it is the project's release page (the
/// repository of `app_info.APP_REPOSITORY_URL`); see the T-P3-07 report, "需要 Claude 裁定".
pub const RUNTIME_HELP_URL: &str = "https://github.com/definitlynotme/Tessract/releases/latest";
/// §2.2.1 step 6: the longest the window stays cloaked waiting for `host_ready`.
pub const READY_TIMEOUT: Duration = Duration::from_millis(1500);
/// §7.9: the bounded wait for `BrowserProcessExited` at shell exit. The work order suggests about
/// 3 s; on this machine the browser process took from 0.2 s to over 20 s after the controller was
/// closed (about half of the samples above 3 s, some above 12 s; T-P3-07 report §8 ruling 3), so
/// 3 s would time out most of the time. The window and tray are already gone and the scope's
/// quitting mark makes a second launch wait. A wait that reaches the cap is logged as `timeout`.
pub const EXIT_WAIT: Duration = Duration::from_secs(12);
/// Why the exit does not wait for the browser process at all.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum ExitWaitSkip {
    /// Logoff/shutdown: no restart follows, Windows ends the process after WM_ENDSESSION anyway.
    SessionEnd,
    /// T-P3-06 update hand-off: the installer already runs and may need to replace the shell's
    /// files; the shell leaves at once (fix round 1, A-S4 / B-S4).
    Update,
}
impl ExitWaitSkip {
    pub fn wire(self) -> &'static str { match self { Self::SessionEnd => "skipped_session_end", Self::Update => "skipped_update" } }
}
/// The exit wait for this quit: none at logoff/shutdown or after an update hand-off,
/// [`EXIT_WAIT`] otherwise.
pub fn exit_wait(session_end: bool, update: bool) -> Result<Duration, ExitWaitSkip> {
    if session_end { Err(ExitWaitSkip::SessionEnd) } else if update { Err(ExitWaitSkip::Update) } else { Ok(EXIT_WAIT) }
}
/// Browser-process loss: the first creation right away, then up to 3 retries after these delays
/// (the user data folder may still be held by the dying browser's children).
pub const RETRY_DELAYS: [Duration; 3] = [Duration::from_millis(500), Duration::from_millis(1000), Duration::from_millis(2000)];
/// Renderer reloads allowed per window: a page that kills its renderer on every load must not
/// spin the shell in a reload loop.
pub const RELOAD_LIMIT: usize = 3;
pub const RELOAD_WINDOW: Duration = Duration::from_secs(60);
/// Browser-process recreations allowed within [`RECREATE_WINDOW`] (fix round 1, A-S2): a browser
/// that dies again right after every rebuild must end in the native box, not in an endless loop.
pub const RECREATE_LIMIT: usize = 3;
pub const RECREATE_WINDOW: Duration = Duration::from_secs(60);
/// How a browser-process loss is answered.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Recreate {
    /// Destroy the dead window and build a new one (with backoff).
    Attempt,
    /// [`RECREATE_LIMIT`] recreations already happened within [`RECREATE_WINDOW`]: destroy the
    /// dead window and go straight to the exhausted path (native box, then quit).
    Limited,
}
/// Fix round 1 (A-S1): no recreation (and no native box) while the shell is leaving — a quit
/// already sent (`!accepting`) or Windows ending the session. An update quit is the exception:
/// a failed install returns to Running and needs a window again.
pub fn skip_recreate(accepting: bool, update_quit: bool, session_end: bool) -> bool {
    session_end || (!accepting && !update_quit)
}

/// The variables a build removes: release = the four of §2.2.1 plus the debugger pair; debug =
/// only the debugger pair.
pub fn scrub_names(release: bool) -> Vec<&'static str> {
    let mut names = ALWAYS_SCRUB.to_vec();
    if release { names.extend(RELEASE_SCRUB); }
    names
}
/// Removes `scrub_names(release)` through `remove` (true = the variable was set) and returns the
/// names that were set, in order.
pub fn scrub(release: bool, mut remove: impl FnMut(&str) -> bool) -> Vec<&'static str> {
    scrub_names(release).into_iter().filter(|name| remove(name)).collect()
}
/// [`scrub`] on this process's environment. Call before any thread or WebView2 call exists.
pub fn scrub_process_environment(release: bool) -> Vec<&'static str> {
    scrub(release, |name| { let set = std::env::var_os(name).is_some(); std::env::remove_var(name); set })
}
pub fn set_app_user_model_id() -> bool {
    unsafe { windows::Win32::UI::Shell::SetCurrentProcessExplicitAppUserModelID(&windows::core::HSTRING::from(AUMID)) }.is_ok()
}
/// §2.2.1 step 3: the installed runtime's version, or the loader's HRESULT when there is none.
/// It honours `WEBVIEW2_BROWSER_EXECUTABLE_FOLDER` / `WEBVIEW2_RELEASE_CHANNEL_PREFERENCE`, so
/// it runs after [`scrub_process_environment`], like the environment creation itself.
pub fn runtime_version() -> Result<String, i32> {
    use webview2_com::Microsoft::Web::WebView2::Win32::GetAvailableCoreWebView2BrowserVersionString;
    use windows::core::{PCWSTR, PWSTR};
    let mut raw = PWSTR::null();
    let result = unsafe { GetAvailableCoreWebView2BrowserVersionString(PCWSTR::null(), &mut raw) };
    let version = if raw.is_null() { String::new() } else {
        let text = unsafe { raw.to_string() }.unwrap_or_default();
        unsafe { windows::Win32::System::Com::CoTaskMemFree(Some(raw.0 as *const _)); }
        text
    };
    runtime_outcome(result.map_err(|e| e.code().0), version)
}
/// Older loaders return S_OK with a null version when nothing is installed.
pub fn runtime_outcome(result: Result<(), i32>, version: String) -> Result<String, i32> {
    match result {
        Err(code) => Err(code),
        Ok(()) if version.trim().is_empty() => Err(MISSING_HRESULT),
        Ok(()) => Ok(version),
    }
}
/// The help link, only through `host.openUrl`'s own whitelist check (never a second copy of it).
pub fn open_runtime_help() -> bool {
    let Ok((url, _host)) = crate::host::url_args(&serde_json::json!({"url": RUNTIME_HELP_URL})) else { return false };
    let url = windows::core::HSTRING::from(url.as_str());
    let result = unsafe { windows::Win32::UI::Shell::ShellExecuteW(None, windows::core::w!("open"), &url, None, None,
        windows::Win32::UI::WindowsAndMessaging::SW_SHOWNORMAL) };
    result.0 as isize > 32
}

/// The reveal timeout: 1.5 s; debug builds may stretch it (`TESSERACT_SHELL_DEV_READY_TIMEOUT_MS`,
/// 100–60000) so a smoke can observe the cloaked window before it sends `host_ready`.
pub fn ready_timeout(debug: bool, raw: Option<String>) -> Duration {
    if !debug { return READY_TIMEOUT; }
    raw.and_then(|v| v.trim().parse::<u64>().ok()).filter(|ms| (100..=60_000).contains(ms)).map(Duration::from_millis).unwrap_or(READY_TIMEOUT)
}
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Reveal { Ready, Timeout, Superseded }
/// One reveal per window generation: `arm` starts a generation (a new main window), `ready` (the
/// page's `host_ready`) releases the current one, `wait` returns once per generation.
#[derive(Default)]
pub struct Gate { state: Mutex<(u64, bool)>, changed: Condvar }
impl Gate {
    fn lock(&self) -> std::sync::MutexGuard<'_, (u64, bool)> { self.state.lock().unwrap_or_else(|e| e.into_inner()) }
    pub fn arm(&self) -> u64 { let mut s = self.lock(); s.0 += 1; s.1 = false; s.0 }
    /// True when this call released the current generation (later calls, e.g. after a renderer
    /// reload, change nothing).
    pub fn ready(&self) -> bool {
        let mut s = self.lock();
        if s.1 { return false; }
        s.1 = true; self.changed.notify_all(); true
    }
    pub fn wait(&self, generation: u64, timeout: Duration) -> Reveal {
        let guard = self.lock();
        let (s, _) = self.changed.wait_timeout_while(guard, timeout, |s| s.0 == generation && !s.1).unwrap_or_else(|e| e.into_inner());
        if s.0 != generation { Reveal::Superseded } else if s.1 { Reveal::Ready } else { Reveal::Timeout }
    }
}
/// DWMWA_CLOAK: the window stays "visible" to Win32 (WebView2 keeps its visible state), DWM just
/// does not show it.
pub fn set_cloak(hwnd: isize, cloak: bool) -> bool {
    use windows::Win32::Graphics::Dwm::{DwmSetWindowAttribute, DWMWA_CLOAK};
    let value = windows::core::BOOL::from(cloak);
    unsafe { DwmSetWindowAttribute(windows::Win32::Foundation::HWND(hwnd as _), DWMWA_CLOAK, &value as *const _ as *const _,
        std::mem::size_of::<windows::core::BOOL>() as u32) }.is_ok()
}
/// DWMWA_CLOAKED as DWM reports it (0 when the query fails): DWM_CLOAKED_APP (1), _SHELL (2),
/// _INHERITED (4).
pub fn cloaked_flags(hwnd: isize) -> u32 {
    use windows::Win32::Graphics::Dwm::{DwmGetWindowAttribute, DWMWA_CLOAKED};
    let mut value = 0u32;
    let ok = unsafe { DwmGetWindowAttribute(windows::Win32::Foundation::HWND(hwnd as _), DWMWA_CLOAKED, &mut value as *mut _ as *mut _, 4) }.is_ok();
    if ok { value } else { 0 }
}
/// Cloaked by this process (DWM_CLOAKED_APP), i.e. the main window still waiting for `host_ready`.
/// A window the shell cloaks (another virtual desktop) does not count: T-P3-05 semantics.
pub fn app_cloaked(hwnd: isize) -> bool { cloaked_flags(hwnd) & 1 != 0 }
/// Waits for the generation's `host_ready` (or the timeout), then uncloaks `hwnd` on the UI thread.
pub fn start_reveal(app: tauri::AppHandle, supervisor: Arc<Supervisor>, hwnd: isize, generation: u64) {
    std::thread::spawn(move || {
        let started = Instant::now();
        let outcome = supervisor.gate.wait(generation, supervisor.ready_timeout);
        let waited = started.elapsed().as_millis().to_string();
        if outcome == Reveal::Superseded { supervisor.log("window_reveal_superseded", &[("waited_ms", &waited)]); return; }
        let (tx, rx) = std::sync::mpsc::channel();
        let shown = app.run_on_main_thread(move || { let _ = tx.send(set_cloak(hwnd, false)); }).is_ok()
            && rx.recv_timeout(Duration::from_secs(5)).unwrap_or(false);
        let ok = if shown { "true" } else { "false" };
        match outcome {
            Reveal::Ready => supervisor.log("window_revealed", &[("reason", "ready"), ("waited_ms", &waited), ("ok", ok)]),
            _ => supervisor.log("window_reveal_timeout", &[("waited_ms", &waited), ("ok", ok)]),
        }
    });
}

/// `COREWEBVIEW2_PROCESS_FAILED_KIND`, as far as the shell treats it differently.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Failure { BrowserExited, RenderExited, RenderUnresponsive, FrameRenderExited, GpuExited, OtherExited }
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Remedy { Reload, Recreate, LogOnly }
pub fn classify(kind: i32) -> Failure {
    match kind { 0 => Failure::BrowserExited, 1 => Failure::RenderExited, 2 => Failure::RenderUnresponsive,
        3 => Failure::FrameRenderExited, 6 => Failure::GpuExited, _ => Failure::OtherExited }
}
impl Failure {
    pub fn wire(self) -> &'static str {
        match self { Self::BrowserExited => "browser_exited", Self::RenderExited => "render_exited", Self::RenderUnresponsive => "render_unresponsive",
            Self::FrameRenderExited => "frame_render_exited", Self::GpuExited => "gpu_exited", Self::OtherExited => "other_exited" }
    }
    /// §2.2.6: the main renderer → reload; the browser → recreate the WebView. A sub-frame
    /// renderer, the GPU and the utility processes are restarted by WebView2 itself.
    pub fn remedy(self) -> Remedy {
        match self { Self::RenderExited | Self::RenderUnresponsive => Remedy::Reload, Self::BrowserExited => Remedy::Recreate, _ => Remedy::LogOnly }
    }
}
/// At most `limit` events within a sliding `span`: renderer reloads ([`RELOAD_LIMIT`] per
/// [`RELOAD_WINDOW`]) and browser recreations ([`RECREATE_LIMIT`] per [`RECREATE_WINDOW`]).
pub struct Budget { limit: usize, span: Duration, times: VecDeque<Instant> }
impl Budget {
    pub fn new(limit: usize, span: Duration) -> Self { Self { limit, span, times: VecDeque::new() } }
    pub fn reloads() -> Self { Self::new(RELOAD_LIMIT, RELOAD_WINDOW) }
    pub fn recreations() -> Self { Self::new(RECREATE_LIMIT, RECREATE_WINDOW) }
    pub fn allow(&mut self, now: Instant) -> bool {
        while self.times.front().is_some_and(|t| now.saturating_duration_since(*t) >= self.span) { self.times.pop_front(); }
        if self.times.len() >= self.limit { return false; }
        self.times.push_back(now); true
    }
}
/// Runs `attempt(n)` (n from 1) until it succeeds, sleeping [`RETRY_DELAYS`] before each retry:
/// `Ok((value, n))`, or `Err((last error, n))` once the retries are used up.
pub fn with_retries<T, E>(mut attempt: impl FnMut(u32) -> Result<T, E>, mut sleep: impl FnMut(Duration)) -> Result<(T, u32), (E, u32)> {
    let mut n = 1u32;
    loop {
        match attempt(n) {
            Ok(value) => return Ok((value, n)),
            Err(error) => match RETRY_DELAYS.get(n as usize - 1) {
                Some(delay) => { sleep(*delay); n += 1; },
                None => return Err((error, n)),
            },
        }
    }
}

/// What the WebView2 event handlers, the `host_ready` command, the recreation thread and the exit
/// path share.
pub struct Supervisor {
    data: PathBuf,
    pub gate: Gate,
    ready_timeout: Duration,
    /// A browser-process recreation is in progress (or failed for good): Tauri must not end the
    /// event loop just because the main window is gone (main.rs `run_return`).
    recovering: AtomicBool,
    reloads: Mutex<Budget>,
    recreations: Mutex<Budget>,
    /// The browser-loss handler (main.rs). False: it did not start (the shell is leaving).
    recreate: Mutex<Option<Arc<dyn Fn(Recreate) -> bool + Send + Sync>>>,
    /// Environment generations: the last one handed out ([`Self::reserve_browser`]), the current
    /// main window's one (published only once its `BrowserProcessExited` hook is in place, A-S7),
    /// and the last one whose browser exited.
    issued: AtomicU64,
    browser: AtomicU64,
    exited: AtomicU64,
    /// Debug-only smoke hook (`TESSERACT_SHELL_DEV_EXIT_WAIT_IGNORE_EXITED=1`): the exit wait
    /// ignores `BrowserProcessExited`, so a smoke can drive it into its `timeout` branch.
    ignore_exited: bool,
}
impl Supervisor {
    #[cfg(test)]
    pub fn new(data: PathBuf, ready_timeout: Duration) -> Arc<Self> { Self::with_exit_hook(data, ready_timeout, false) }
    pub fn with_exit_hook(data: PathBuf, ready_timeout: Duration, ignore_exited: bool) -> Arc<Self> {
        Arc::new(Self { data, gate: Gate::default(), ready_timeout, recovering: AtomicBool::new(false), reloads: Mutex::new(Budget::reloads()),
            recreations: Mutex::new(Budget::recreations()), recreate: Mutex::new(None), issued: AtomicU64::new(0), browser: AtomicU64::new(0),
            exited: AtomicU64::new(0), ignore_exited })
    }
    pub fn data(&self) -> &Path { &self.data }
    pub fn log(&self, code: &str, fields: &[(&str, &str)]) { crate::shell_log::fields(&self.data, code, fields); }
    pub fn recovering(&self) -> bool { self.recovering.load(Ordering::Acquire) }
    pub fn recovered(&self) { self.recovering.store(false, Ordering::Release); }
    /// The browser-loss handler (main.rs): it spawns the recreation thread.
    pub fn set_recreate(&self, recreate: Arc<dyn Fn(Recreate) -> bool + Send + Sync>) { *self.recreate.lock().unwrap_or_else(|e| e.into_inner()) = Some(recreate); }
    /// `ProcessFailed` on the UI thread. `reload` reloads the failed page (true = accepted).
    pub fn process_failed(&self, failure: Failure, reload: impl FnOnce() -> bool, now: Instant) -> Remedy {
        let remedy = failure.remedy();
        let wire = match remedy { Remedy::Reload => "reload", Remedy::Recreate => "recreate", Remedy::LogOnly => "none" };
        self.log("webview_process_failed", &[("kind", failure.wire()), ("remedy", wire)]);
        match remedy {
            Remedy::Reload => {
                if self.reloads.lock().unwrap_or_else(|e| e.into_inner()).allow(now) {
                    let ok = reload();
                    self.log("webview_reload", &[("kind", failure.wire()), ("ok", if ok { "true" } else { "false" })]);
                } else { self.log("webview_reload_limited", &[("kind", failure.wire()), ("limit", &RELOAD_LIMIT.to_string())]); }
            },
            Remedy::Recreate => {
                // One recreation at a time; a second report of the same loss changes nothing.
                if self.recovering.swap(true, Ordering::AcqRel) { self.log("webview_recreate_in_progress", &[]); }
                else {
                    let recreate = self.recreate.lock().unwrap_or_else(|e| e.into_inner()).clone();
                    match recreate {
                        Some(recreate) => {
                            // A-S2: a browser that keeps dying right after each rebuild ends in the box.
                            let mode = if self.recreations.lock().unwrap_or_else(|e| e.into_inner()).allow(now) { Recreate::Attempt } else {
                                self.log("webview_recreate_limited", &[("limit", &RECREATE_LIMIT.to_string()), ("window_s", &RECREATE_WINDOW.as_secs().to_string())]);
                                Recreate::Limited
                            };
                            // A-S1: the handler declined (the shell is leaving): nothing is recovering.
                            if !recreate(mode) { self.recovering.store(false, Ordering::Release); }
                        },
                        None => { self.recovering.store(false, Ordering::Release); self.log("webview_recreate_unavailable", &[]); },
                    }
                }
            },
            Remedy::LogOnly => (),
        }
        remedy
    }
    /// A generation number for a main window's WebView2 environment, not yet current: the window
    /// registers its `BrowserProcessExited` hook with it first and then [`Self::publish_browser`]s
    /// it (A-S7), so the current generation always has a hook that can report its exit.
    pub fn reserve_browser(&self) -> u64 { self.issued.fetch_add(1, Ordering::AcqRel) + 1 }
    pub fn publish_browser(&self, generation: u64) { self.browser.fetch_max(generation, Ordering::AcqRel); }
    pub fn browser_exited(&self, generation: u64, failed: bool) {
        self.exited.fetch_max(generation, Ordering::AcqRel);
        self.log("webview_browser_process_exited", &[("generation", &generation.to_string()), ("kind", if failed { "failed" } else { "normal" })]);
    }
    fn current_exited(&self) -> Option<bool> {
        let current = self.browser.load(Ordering::Acquire);
        (current != 0).then(|| !self.ignore_exited && self.exited.load(Ordering::Acquire) >= current)
    }
}

thread_local! {
    /// The current main window's environment, held on the UI thread so its `BrowserProcessExited`
    /// can still be raised after the WebView itself is gone (§7.9).
    static ENVIRONMENT: RefCell<Option<ICoreWebView2Environment>> = const { RefCell::new(None) };
}
pub fn keep_environment(environment: ICoreWebView2Environment) { ENVIRONMENT.with(|e| *e.borrow_mut() = Some(environment)); }
/// §7.9, on the UI thread after the event loop ended: pump messages until the current
/// environment's `BrowserProcessExited` arrived or `timeout` passed. ("exited" | "timeout" | "none", ms)
pub fn wait_browser_exit(supervisor: &Supervisor, timeout: Duration) -> (&'static str, u64) {
    use windows::Win32::UI::WindowsAndMessaging::{DispatchMessageW, MsgWaitForMultipleObjects, PeekMessageW, TranslateMessage, MSG, PM_REMOVE, QS_ALLINPUT};
    let started = Instant::now();
    let result = loop {
        match supervisor.current_exited() {
            None => break "none",
            Some(true) => break "exited",
            Some(false) if started.elapsed() >= timeout => break "timeout",
            Some(false) => unsafe {
                let left = timeout.saturating_sub(started.elapsed()).min(Duration::from_millis(50));
                let _ = MsgWaitForMultipleObjects(None, false, left.as_millis() as u32, QS_ALLINPUT);
                let mut msg = MSG::default();
                while PeekMessageW(&mut msg, None, 0, 0, PM_REMOVE).as_bool() { let _ = TranslateMessage(&msg); DispatchMessageW(&msg); }
            },
        }
    };
    ENVIRONMENT.with(|e| e.borrow_mut().take());
    (result, started.elapsed().as_millis() as u64)
}

#[cfg(test)] mod tests {
    use super::*;
    use std::collections::BTreeSet;
    /// Fix round 1 (M2): the process environment is shared by every test thread, and the WebView2
    /// loader behind `runtime_version()` reads `WEBVIEW2_BROWSER_EXECUTABLE_FOLDER` /
    /// `WEBVIEW2_RELEASE_CHANNEL_PREFERENCE`. Every test that sets those variables or asks the real
    /// loader holds this lock, so libtest's parallel threads never interleave the two.
    static ENV_LOCK: Mutex<()> = Mutex::new(());
    fn env_lock() -> std::sync::MutexGuard<'static, ()> { ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner()) }
    /// Removes the §2.2.1 variables again even when an assertion fails while they are set.
    struct Unset;
    impl Drop for Unset { fn drop(&mut self) { for name in RELEASE_SCRUB { std::env::remove_var(name); } } }
    #[test] fn release_scrubs_the_four_overrides_and_debug_keeps_them() {
        let env: BTreeSet<&str> = RELEASE_SCRUB.iter().chain(ALWAYS_SCRUB.iter()).copied().chain(["WEBVIEW2_OTHER"]).collect();
        let run = |release: bool| {
            let mut left = env.clone();
            let removed = scrub(release, |name| left.remove(name));
            (removed, left)
        };
        let (removed, left) = run(true);
        assert_eq!(removed, ["WEBVIEW2_PIPE_FOR_SCRIPT_DEBUGGER", "WEBVIEW2_WAIT_FOR_SCRIPT_DEBUGGER", "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS",
            "WEBVIEW2_USER_DATA_FOLDER", "WEBVIEW2_BROWSER_EXECUTABLE_FOLDER", "WEBVIEW2_RELEASE_CHANNEL_PREFERENCE"]);
        assert_eq!(left.into_iter().collect::<Vec<_>>(), ["WEBVIEW2_OTHER"], "release keeps nothing of §2.2.1 step 1");
        let (removed, left) = run(false);
        assert_eq!(removed, ALWAYS_SCRUB, "debug only drops the T-P3-01 debugger pair");
        for name in RELEASE_SCRUB { assert!(left.contains(name), "debug keeps {name}"); }
        // Unset variables are not reported as removed.
        assert!(scrub(true, |_| false).is_empty());
    }
    #[test] fn process_environment_scrub_follows_the_build() {
        // The WebView2 loader reads two of these names: serialized with the real loader call.
        let _env = env_lock(); let _unset = Unset;
        for name in RELEASE_SCRUB { std::env::set_var(name, "x"); }
        assert!(scrub_process_environment(false).is_empty());
        for name in RELEASE_SCRUB { assert!(std::env::var_os(name).is_some(), "debug keeps {name}"); }
        assert_eq!(scrub_process_environment(true), RELEASE_SCRUB);
        for name in RELEASE_SCRUB.iter().chain(ALWAYS_SCRUB.iter()) { assert!(std::env::var_os(name).is_none(), "release removed {name}"); }
    }
    #[test] fn runtime_outcome_and_help_link_pass_the_open_url_whitelist() {
        assert_eq!(runtime_outcome(Ok(()), "131.0.2903.70".into()), Ok("131.0.2903.70".into()));
        assert_eq!(runtime_outcome(Ok(()), " ".into()), Err(MISSING_HRESULT));
        assert_eq!(runtime_outcome(Err(-1), "1".into()), Err(-1));
        let (url, host) = crate::host::url_args(&serde_json::json!({"url": RUNTIME_HELP_URL})).expect("host.openUrl whitelist");
        assert_eq!((url.as_str(), host), (RUNTIME_HELP_URL, "github.com"));
        // The repository is the one app_info.py publishes (no drift between the two).
        let app_info = include_str!("../../../app_info.py");
        let repo = app_info.lines().find_map(|l| l.strip_prefix("APP_REPOSITORY_URL = \"")).and_then(|l| l.strip_suffix('"')).expect("APP_REPOSITORY_URL");
        assert!(RUNTIME_HELP_URL.starts_with(&format!("{repo}/")), "{repo}");
        // This machine has a runtime (the shell itself needs one); the real loader call answers.
        // Under ENV_LOCK: never while another test has pointed the loader elsewhere (M2).
        let version = { let _env = env_lock(); runtime_version() };
        assert!(version.as_ref().is_ok_and(|v| v.split('.').count() == 4), "{version:?}");
    }
    #[test] fn ready_or_timeout_reveals_once_per_generation() {
        assert_eq!(ready_timeout(false, Some("20000".into())), READY_TIMEOUT, "release ignores the override");
        assert_eq!(ready_timeout(true, Some("20000".into())), Duration::from_secs(20));
        for bad in ["99", "60001", "x", ""] { assert_eq!(ready_timeout(true, Some(bad.into())), READY_TIMEOUT, "{bad}"); }
        assert_eq!(ready_timeout(true, None), Duration::from_millis(1500));
        let gate = Arc::new(Gate::default());
        // No host_ready: the wait ends at the timeout, not before.
        let generation = gate.arm();
        let started = Instant::now();
        assert_eq!(gate.wait(generation, Duration::from_millis(120)), Reveal::Timeout);
        assert!(started.elapsed() >= Duration::from_millis(120));
        // host_ready while waiting releases it at once; a second host_ready changes nothing.
        let generation = gate.arm();
        let waiter = { let gate = gate.clone(); std::thread::spawn(move || { let s = Instant::now(); (gate.wait(generation, Duration::from_secs(10)), s.elapsed()) }) };
        std::thread::sleep(Duration::from_millis(50));
        assert!(gate.ready()); assert!(!gate.ready());
        let (outcome, waited) = waiter.join().unwrap();
        assert_eq!(outcome, Reveal::Ready); assert!(waited < Duration::from_secs(5));
        // host_ready that came before the wait still counts.
        let generation = gate.arm(); assert!(gate.ready());
        assert_eq!(gate.wait(generation, Duration::from_millis(10)), Reveal::Ready);
        // A replaced window's waiter is told so (it must not reveal or log a timeout).
        let old = gate.arm();
        let waiter = { let gate = gate.clone(); std::thread::spawn(move || gate.wait(old, Duration::from_secs(10))) };
        std::thread::sleep(Duration::from_millis(30));
        let new = gate.arm(); assert_ne!(old, new);
        gate.ready();
        assert_eq!(waiter.join().unwrap(), Reveal::Superseded);
    }
    #[test] fn app_cloak_is_seen_by_dwm_and_hides_the_window_from_presence() {
        use crate::presence::Probe;
        use windows::Win32::UI::WindowsAndMessaging::{CreateWindowExW, DestroyWindow, WINDOW_EX_STYLE, WS_OVERLAPPEDWINDOW};
        let hwnd = unsafe { CreateWindowExW(WINDOW_EX_STYLE(0), windows::core::w!("STATIC"), windows::core::w!("p3-07-cloak"), WS_OVERLAPPEDWINDOW,
            0, 0, 120, 80, None, None, None, None) }.expect("test window");
        let raw = hwnd.0 as isize;
        let probe = crate::presence::Win32Probe::default(); probe.set(raw);
        assert_eq!((cloaked_flags(raw), app_cloaked(raw)), (0, false));
        assert!(probe.window().is_some(), "an uncloaked window has presence facts");
        assert!(set_cloak(raw, true));
        assert!(app_cloaked(raw), "DWMWA_CLOAKED reports DWM_CLOAKED_APP: {}", cloaked_flags(raw));
        assert_eq!(probe.window(), None, "no presence while cloaked for host_ready");
        assert!(set_cloak(raw, false));
        assert!(!app_cloaked(raw)); assert!(probe.window().is_some());
        unsafe { let _ = DestroyWindow(hwnd); }
    }
    #[test] fn retries_back_off_and_stop_after_three() {
        let mut slept = Vec::new();
        let r: Result<((), u32), (&str, u32)> = with_retries(|_| Err("busy"), |d| slept.push(d));
        assert_eq!(r, Err(("busy", 4)), "first attempt plus three retries");
        assert_eq!(slept, RETRY_DELAYS.to_vec());
        assert_eq!(slept, [Duration::from_millis(500), Duration::from_secs(1), Duration::from_secs(2)]);
        let mut slept = Vec::new();
        let r = with_retries(|n| if n < 3 { Err(n) } else { Ok("up") }, |d| slept.push(d));
        assert_eq!(r, Ok(("up", 3))); assert_eq!(slept.len(), 2);
        let r: Result<(u8, u32), ((), u32)> = with_retries(|_| Ok(7), |_| panic!("no sleep on success"));
        assert_eq!(r, Ok((7, 1)));
    }
    #[test] fn failures_are_classified_into_reload_recreate_or_log() {
        let cases = [(0, Failure::BrowserExited, Remedy::Recreate), (1, Failure::RenderExited, Remedy::Reload), (2, Failure::RenderUnresponsive, Remedy::Reload),
            (3, Failure::FrameRenderExited, Remedy::LogOnly), (4, Failure::OtherExited, Remedy::LogOnly), (5, Failure::OtherExited, Remedy::LogOnly),
            (6, Failure::GpuExited, Remedy::LogOnly), (7, Failure::OtherExited, Remedy::LogOnly), (9, Failure::OtherExited, Remedy::LogOnly), (42, Failure::OtherExited, Remedy::LogOnly)];
        for (kind, failure, remedy) in cases { assert_eq!((classify(kind), classify(kind).remedy()), (failure, remedy), "kind {kind}"); }
        let dir = std::env::temp_dir().join(format!("p3-07-failure-{}", std::process::id()));
        let sup = Supervisor::new(dir.clone(), READY_TIMEOUT);
        let t0 = Instant::now();
        // Reloads: three per minute, then refused until the window slides.
        let mut reloads = 0;
        for i in 0..4 { assert_eq!(sup.process_failed(Failure::RenderExited, || { reloads += 1; true }, t0 + Duration::from_secs(i)), Remedy::Reload); }
        assert_eq!(reloads, 3);
        sup.process_failed(Failure::RenderUnresponsive, || { reloads += 1; true }, t0 + RELOAD_WINDOW);
        assert_eq!(reloads, 4, "the first reload left the window");
        // Log-only kinds never reload or recreate.
        assert_eq!(sup.process_failed(Failure::GpuExited, || panic!("no reload"), t0), Remedy::LogOnly);
        // Browser loss: one recreation at a time; without a handler the flag is released.
        assert_eq!(sup.process_failed(Failure::BrowserExited, || panic!("no reload"), t0), Remedy::Recreate);
        assert!(!sup.recovering());
        let calls = Arc::new(Mutex::new(Vec::new()));
        let accept = Arc::new(AtomicBool::new(true));
        { let (calls, accept) = (calls.clone(), accept.clone());
          sup.set_recreate(Arc::new(move |mode| { calls.lock().unwrap().push(mode); accept.load(Ordering::Acquire) })); }
        let modes = || calls.lock().unwrap().clone();
        sup.process_failed(Failure::BrowserExited, || false, t0);
        sup.process_failed(Failure::BrowserExited, || false, t0);
        assert_eq!(modes(), [Recreate::Attempt]); assert!(sup.recovering());
        sup.recovered(); sup.process_failed(Failure::BrowserExited, || false, t0 + Duration::from_secs(1));
        assert_eq!(modes(), [Recreate::Attempt; 2]);
        // A-S2: a third recreation within the minute is still an attempt, the fourth is limited
        // (straight to the native box); once the first left the window, attempts are back.
        sup.recovered(); sup.process_failed(Failure::BrowserExited, || false, t0 + Duration::from_secs(2));
        sup.recovered(); sup.process_failed(Failure::BrowserExited, || false, t0 + Duration::from_secs(3));
        assert_eq!(modes(), [Recreate::Attempt, Recreate::Attempt, Recreate::Attempt, Recreate::Limited]);
        assert!(sup.recovering(), "a limited loss still runs the exhausted path (box, then quit)");
        sup.recovered(); sup.process_failed(Failure::BrowserExited, || false, t0 + RECREATE_WINDOW);
        assert_eq!(modes().last(), Some(&Recreate::Attempt));
        // A-S1: the handler declines while the shell is leaving; nothing stays "recovering", so a
        // later windowless exit request is not held back.
        sup.recovered(); accept.store(false, Ordering::Release);
        sup.process_failed(Failure::BrowserExited, || false, t0 + RECREATE_WINDOW * 3);
        assert!(!sup.recovering());
        let log = std::fs::read_to_string(dir.join("logs/shell.log")).unwrap();
        for line in ["webview_process_failed kind=render_exited remedy=reload", "webview_reload kind=render_exited ok=true",
            "webview_reload_limited kind=render_exited limit=3", "webview_process_failed kind=gpu_exited remedy=none",
            "webview_recreate_unavailable", "webview_recreate_in_progress", "webview_recreate_limited limit=3 window_s=60"] { assert!(log.contains(line), "{line}\n{log}"); }
        let _ = std::fs::remove_dir_all(dir);
    }
    #[test] fn recreation_is_skipped_only_while_leaving() {
        // (accepting, update quit, session end) → skip
        assert!(!skip_recreate(true, false, false), "running (or only asking the page): rebuild");
        assert!(skip_recreate(false, false, false), "app.quit sent: leaving, no rebuild, no box");
        assert!(!skip_recreate(false, true, false), "update quit: a failed install returns to Running");
        assert!(skip_recreate(true, false, true) && skip_recreate(false, true, true), "session end always skips");
        let mut budget = Budget::recreations(); let t0 = Instant::now();
        assert!((0..3).all(|i| budget.allow(t0 + Duration::from_secs(i))));
        assert!(!budget.allow(t0 + Duration::from_secs(59)));
        assert!(budget.allow(t0 + RECREATE_WINDOW));
    }
    #[test] fn browser_exit_generations() {
        let dir = std::env::temp_dir().join(format!("p3-07-exit-{}", std::process::id()));
        let sup = Supervisor::new(dir.clone(), READY_TIMEOUT);
        assert_eq!(wait_browser_exit(&sup, Duration::from_secs(5)).0, "none", "no WebView was ever created");
        // A-S7: a reserved generation is not current until its hook is in place (published).
        let first = sup.reserve_browser();
        assert_eq!(wait_browser_exit(&sup, Duration::from_secs(5)).0, "none", "reserved, never published");
        sup.publish_browser(first);
        // A creation that failed after reserving (no hook, never published) changes nothing ...
        let failed = sup.reserve_browser();
        let second = sup.reserve_browser();
        assert!(first < failed && failed < second);
        sup.publish_browser(second);
        sup.browser_exited(first, true);
        let (result, ms) = wait_browser_exit(&sup, Duration::from_millis(80));
        assert_eq!(result, "timeout", "the crashed first browser is not the current one"); assert!(ms >= 80);
        // ... and an older generation published late never moves the current one back.
        sup.publish_browser(first);
        sup.browser_exited(second, false);
        assert_eq!(wait_browser_exit(&sup, Duration::from_secs(5)).0, "exited");
        // The debug smoke hook: BrowserProcessExited ignored, the wait runs into its cap.
        let hooked = Supervisor::with_exit_hook(dir.clone(), READY_TIMEOUT, true);
        let only = hooked.reserve_browser(); hooked.publish_browser(only); hooked.browser_exited(only, false);
        assert_eq!(wait_browser_exit(&hooked, Duration::from_millis(60)).0, "timeout");
        // Logoff/shutdown and the update hand-off skip the wait; every other quit is bounded by EXIT_WAIT.
        assert_eq!(exit_wait(true, false), Err(ExitWaitSkip::SessionEnd));
        assert_eq!(exit_wait(true, true), Err(ExitWaitSkip::SessionEnd));
        assert_eq!(exit_wait(false, true), Err(ExitWaitSkip::Update));
        assert_eq!(exit_wait(false, false), Ok(EXIT_WAIT));
        assert_eq!((ExitWaitSkip::SessionEnd.wire(), ExitWaitSkip::Update.wire()), ("skipped_session_end", "skipped_update"));
        assert!(EXIT_WAIT >= Duration::from_secs(3) && EXIT_WAIT <= Duration::from_secs(15));
        let _ = std::fs::remove_dir_all(dir);
    }
}
