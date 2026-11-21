//! T-P3-04 logoff/shutdown (plan §2.2.4, PROTOCOL §13 "注销快路径").
//!
//! WM_QUERYENDSESSION: `bye` already received, an update quit, or our session end already sent
//! → allow, send nothing. A sent `app.quit` still waiting for `bye` does not count (T-P3-05,
//! decision P3-04 item 2): the session end is sent as well. Otherwise ShutdownBlockReasonCreate("正在恢复网络设置"), then hand `app.sessionEnd
//! {reason, budget_ms}` straight to the bridge worker's control queue (no WebView, never behind
//! `app.quit`) and allow at once. WM_ENDSESSION(TRUE) waits within the budget for the reply or
//! `bye`, logs the outcome, then ShutdownBlockReasonDestroy. Windows is never refused.
//! WM_POWERBROADCAST/PBT_APMRESUMEAUTOMATIC bumps the counter the pong watchdog resets on.
//!
//! An installer's close is no session end (11-20). Restart Manager asks with ENDSESSION_CLOSEAPP
//! and without ENDSESSION_LOGOFF ([`closes_app_only`]); afterwards Windows ends nothing and the
//! session goes on. Tesseract Setup closes a running Tesseract this way before every install and
//! opens it again afterwards (setup rm.rs). Its query is allowed with nothing sent; its
//! WM_ENDSESSION(TRUE) starts an ordinary quit and returns at once (`Lifecycle::installer_close`):
//! `app.quit`, the sidecar's full exit recovery, the 20 s prompt, the bounded browser-exit wait.
//! 0.1.0–0.1.3 ran it as a shutdown (`app.sessionEnd`, 4 s budget, no browser wait) and then
//! died in tao's panic, which [`guard_event_loop`] prevents from 0.1.4 on.
use crate::lifecycle::Lifecycle;
use serde_json::Value;
use std::{path::{Path, PathBuf}, sync::{atomic::{AtomicBool, AtomicU64, Ordering}, Arc, Condvar, Mutex, OnceLock}, time::{Duration, Instant}};
use windows::core::BOOL;
use windows::Win32::Foundation::{HWND, LPARAM, LRESULT, WPARAM};
use windows::Win32::System::Threading::GetCurrentThreadId;
use windows::Win32::UI::Shell::{DefSubclassProc, RemoveWindowSubclass, SetWindowSubclass};
use windows::Win32::UI::WindowsAndMessaging::{EnumThreadWindows, GetClassNameW};

/// Conservative RT-2 budget: Windows lets an application hold WM_ENDSESSION for about 5 s
/// (WaitToKillAppTimeout / HungAppTimeout default 5000 ms) before the "apps are preventing
/// shutdown" screen or a forced kill. 4000 ms leaves ~1 s for the pipe hop, the reply, the
/// shell's own log write and process exit, while RT-2 itself (revoke writers, registry
/// restore, firewall cleanup) needs well under a second when not blocked.
pub const BUDGET_MS: u32 = 4000;
/// Extra wait beyond the budget for the reply frame to cross the pipe.
const REPLY_GRACE: Duration = Duration::from_millis(500);
/// Highest application shutdown level (0x100–0x3FF): the shell hears the session end first.
pub const SHUTDOWN_LEVEL: u32 = 0x3FF;
const WM_QUERYENDSESSION: u32 = 0x0011;
const WM_ENDSESSION: u32 = 0x0016;
const WM_POWERBROADCAST: u32 = 0x0218;
const WM_NCDESTROY: u32 = 0x0082;
const WM_CLOSE: u32 = 0x0010;
const PBT_APMRESUMEAUTOMATIC: usize = 0x0012;
pub const ENDSESSION_LOGOFF: u32 = 0x8000_0000;
pub const ENDSESSION_CLOSEAPP: u32 = 0x0000_0001;
pub const ENDSESSION_CRITICAL: u32 = 0x4000_0000;
const SUBCLASS_ID: usize = 0x5033_0004;
const LOOP_GUARD_ID: usize = 0x5033_0005;
/// Window class of tao's hidden event-target window (tao 0.35, `THREAD_EVENT_TARGET_WINDOW_CLASS`).
const TAO_EVENT_TARGET_CLASS: &str = "Tao Thread Event Target";

#[link(name = "kernel32")] extern "system" { fn SetProcessShutdownParameters(level: u32, flags: u32) -> i32; }
#[link(name = "user32")] extern "system" {
    fn ShutdownBlockReasonCreate(hwnd: isize, reason: *const u16) -> i32;
    fn ShutdownBlockReasonDestroy(hwnd: isize) -> i32;
}

static RESUMES: AtomicU64 = AtomicU64::new(0);
/// Process-wide count of PBT_APMRESUMEAUTOMATIC broadcasts seen by the main window.
pub fn resume_count() -> u64 { RESUMES.load(Ordering::Acquire) }

/// `ENDSESSION_LOGOFF` set → logoff, otherwise shutdown (including a forced CLOSEAPP; an
/// installer's close sends no `app.sessionEnd` at all, see [`closes_app_only`]).
pub fn reason(lparam: isize) -> &'static str { if (lparam as u32) & ENDSESSION_LOGOFF != 0 { "logoff" } else { "shutdown" } }
/// Restart Manager closing the app for an installer: ENDSESSION_CLOSEAPP, without
/// ENDSESSION_LOGOFF, and not forced. After WM_ENDSESSION the session goes on and Windows ends
/// nothing (Tesseract Setup does this, rm.rs), so the shell quits as it does from the tray. A
/// forced close (ENDSESSION_CRITICAL) is ended by Windows after its timeout and keeps the 4 s
/// session-end path, as a logoff that also closes apps does.
pub fn closes_app_only(lparam: isize) -> bool {
    let f = lparam as u32;
    f & ENDSESSION_CLOSEAPP != 0 && f & (ENDSESSION_LOGOFF | ENDSESSION_CRITICAL) == 0
}

pub fn raise_shutdown_order(data: &std::path::Path) {
    if unsafe { SetProcessShutdownParameters(SHUTDOWN_LEVEL, 0) } == 0 { crate::log_code(data, "shutdown_order_failed"); }
}

#[derive(Default)]
struct State { pending: Option<(&'static str, u32)>, started: Option<Instant>, sent: Option<u64>, reply: Option<Value>, bye: bool, ended: bool }
#[derive(Debug, PartialEq)]
pub struct Outcome { pub sent: bool, pub reply: Option<Value>, pub bye: bool, pub timed_out: bool }
/// Hand-off between the window procedure (UI thread) and the bridge worker.
#[derive(Default)]
pub struct Tracker { state: Mutex<State>, changed: Condvar }
impl Tracker {
    fn lock(&self) -> std::sync::MutexGuard<'_, State> { self.state.lock().unwrap_or_else(|e| e.into_inner()) }
    pub fn begin(&self, reason: &'static str, budget_ms: u32, now: Instant) {
        let mut s = self.lock(); s.pending = Some((reason, budget_ms)); s.started = Some(now);
    }
    pub fn active(&self) -> bool { self.lock().started.is_some() }
    pub fn started(&self) -> Option<Instant> { self.lock().started }
    /// Worker: the request to put at the front of the control queue, at most once.
    pub fn take_request(&self) -> Option<(&'static str, u32)> { self.lock().pending.take() }
    pub fn sent(&self, id: u64) { self.lock().sent = Some(id); }
    pub fn id(&self) -> Option<u64> { self.lock().sent }
    /// Worker: the `res`/`error` answering our `app.sessionEnd` id.
    pub fn reply(&self, frame: Value) { self.lock().reply = Some(frame); self.changed.notify_all(); }
    pub fn bye(&self) { self.lock().bye = true; self.changed.notify_all(); }
    /// Worker: the session is over (process exited or pipe failed); nothing more will come.
    pub fn ended(&self) { self.lock().ended = true; self.changed.notify_all(); }
    /// Waits until `bye`, the end of the session, a final reply, or `deadline`. A reply with
    /// `workers_pending:true` is not final: the sidecar still finishes its workers before `bye`,
    /// so the wait continues for `bye` while the budget lasts (Windows may end the process on
    /// return). `timed_out`: the deadline came first (`reply`/`bye` show how far it got).
    pub fn wait(&self, deadline: Instant) -> Outcome {
        let done = |s: &State| s.bye || s.ended || s.reply.as_ref().is_some_and(|r| !workers_pending(r));
        let mut s = self.lock();
        loop {
            if done(&s) { break; }
            let now = Instant::now();
            if now >= deadline { break; }
            s = self.changed.wait_timeout(s, deadline - now).map(|(g, _)| g).unwrap_or_else(|e| e.into_inner().0);
        }
        Outcome { sent: s.sent.is_some(), reply: s.reply.clone(), bye: s.bye, timed_out: !done(&s) }
    }
}

/// A successful sessionEnd reply that says workers are still finishing (PROTOCOL §10 `workers_pending`).
fn workers_pending(reply: &Value) -> bool { reply["t"] == "res" && reply["ok"] == true && reply["data"]["workers_pending"] == true }
/// `app.sessionEnd` request frame (shell-only command; P2 envelope with req_id/owner).
pub fn request_frame(id: u64, reason: &str, budget_ms: u32) -> Value {
    serde_json::json!({"t":"req","id":id,"cmd":"app.sessionEnd","args":{"reason":reason,"budget_ms":budget_ms},
        "from":"shell","req_id":format!("shell-session-end-{id}"),"owner":"shell"})
}
fn state(value: &Value) -> &str { value.as_str().or_else(|| value["state"].as_str()).unwrap_or("missing") }
/// shell.log fields for the outcome: only booleans and the PROTOCOL recovery enums.
pub fn result_fields(outcome: &Outcome) -> Vec<(&'static str, String)> {
    let mut fields = vec![("sent", outcome.sent.to_string())];
    match &outcome.reply {
        Some(frame) if frame["t"] == "res" && frame["ok"] == true => {
            let data = &frame["data"];
            fields.push(("reply", "ok".into()));
            fields.push(("writes_revoked", data["writes_revoked"].as_bool().map_or("missing".into(), |b| b.to_string())));
            fields.push(("proxy", state(&data["recovery"]["proxy"]).into()));
            fields.push(("firewall", state(&data["recovery"]["firewall"]).into()));
            fields.push(("workers_pending", data["workers_pending"].as_bool().map_or("missing".into(), |b| b.to_string())));
        },
        Some(frame) => {
            fields.push(("reply", "error".into()));
            fields.push(("code", frame["error"]["code"].as_str().unwrap_or("missing").into()));
        },
        None => fields.push(("reply", "none".into())),
    }
    fields.push(("bye", outcome.bye.to_string()));
    fields.push(("timeout", outcome.timed_out.to_string()));
    fields
}

pub struct Context { pub lifecycle: Arc<Lifecycle>, pub tracker: Arc<Tracker>, pub data: PathBuf, pub locale: Box<dyn Fn() -> &'static str + Send + Sync>, blocked: AtomicBool }
static CONTEXT: OnceLock<Context> = OnceLock::new();
impl Context {
    pub fn new(lifecycle: Arc<Lifecycle>, tracker: Arc<Tracker>, data: PathBuf, locale: Box<dyn Fn() -> &'static str + Send + Sync>) -> Self {
        Self { lifecycle, tracker, data, locale, blocked: AtomicBool::new(false) }
    }
    fn log(&self, code: &str, fields: &[(&str, &str)]) { crate::shell_log::fields(&self.data, code, fields); }
}
/// Decision half of WM_QUERYENDSESSION, separated from Win32 for tests. Returns true when a
/// session end was started (block reason wanted, request handed to the worker).
pub fn start(ctx: &Context, reason: &'static str, now: Instant) -> bool {
    if ctx.tracker.active() { return false; } // a second query of the same session end
    if !ctx.lifecycle.begin_session_end() { return false; }
    ctx.tracker.begin(reason, BUDGET_MS, now);
    ctx.log("session_end_requested", &[("reason", reason), ("budget_ms", &BUDGET_MS.to_string())]);
    true
}
fn query(ctx: &Context, hwnd: HWND, lparam: isize) {
    let reason = reason(lparam);
    ctx.log("session_end_query", &[("reason", reason), ("closeapp", &((lparam as u32) & ENDSESSION_CLOSEAPP != 0).to_string()),
        ("flags", &format!("{:#x}", lparam as u32))]);
    // An installer's close: allowed, nothing sent, no block reason; its WM_ENDSESSION quits.
    if closes_app_only(lparam) { ctx.log("installer_close_query", &[]); return; }
    if ctx.tracker.active() { return; } // a second query of our own session end
    if !ctx.lifecycle.session_end_needed() {
        // bye already received, or an update quit: allow without another command.
        ctx.log("session_end_already_quitting", &[]);
        return;
    }
    // Block reason first, then the request: the worker writes it within one loop turn.
    let text: Vec<u16> = crate::native_text::text("session_end_reason", (ctx.locale)()).encode_utf16().chain(Some(0)).collect();
    let blocked = unsafe { ShutdownBlockReasonCreate(hwnd.0 as isize, text.as_ptr()) } != 0;
    ctx.blocked.store(blocked, Ordering::Release);
    ctx.log(if blocked { "session_end_block_reason_created" } else { "session_end_block_reason_failed" }, &[]);
    if !start(ctx, reason, Instant::now()) && blocked {
        unsafe { ShutdownBlockReasonDestroy(hwnd.0 as isize); }
        ctx.blocked.store(false, Ordering::Release);
    }
}
fn end(ctx: &Context, hwnd: HWND, ending: bool, lparam: isize) {
    if closes_app_only(lparam) {
        // An installer's close: an ordinary quit, and Windows is let go at once (it ends nothing
        // and waits for the process). Cancelled: nothing was started, the app keeps running.
        if ending { installer_close(ctx, "main"); } else { ctx.log("installer_close_cancelled_by_windows", &[]); }
    } else if !ending {
        ctx.log("session_end_cancelled_by_windows", &[]);
        // Our sessionEnd already went out: wait for `bye` like a normal quit, 20 s prompt armed.
        if ctx.tracker.active() { ctx.lifecycle.session_end_cancelled(); }
    } else if !ctx.tracker.active() && ctx.lifecycle.finished() {
        // Nothing to wait for: the shell is already leaving (no sidecar, or it has exited).
        ctx.log("session_end_result_shell_exited", &[]);
    } else {
        // WM_ENDSESSION without a prior query (forced end): start the fast path now.
        if !ctx.tracker.active() { query(ctx, hwnd, lparam); }
        let deadline = ctx.tracker.started().map(|at| at + Duration::from_millis(BUDGET_MS.into()) + REPLY_GRACE)
            .unwrap_or_else(|| Instant::now() + Duration::from_millis(BUDGET_MS.into()));
        let outcome = ctx.tracker.wait(deadline);
        let fields = result_fields(&outcome);
        let borrowed: Vec<(&str, &str)> = fields.iter().map(|(k, v)| (*k, v.as_str())).collect();
        ctx.log("session_end_result", &borrowed);
    }
    if ctx.blocked.swap(false, Ordering::AcqRel) {
        unsafe { ShutdownBlockReasonDestroy(hwnd.0 as isize); }
        ctx.log("session_end_block_reason_destroyed", &[]);
    }
}
unsafe extern "system" fn subclass(hwnd: HWND, message: u32, wparam: WPARAM, lparam: LPARAM, _: usize, _: usize) -> LRESULT {
    match message {
        WM_QUERYENDSESSION => { if let Some(ctx) = CONTEXT.get() { query(ctx, hwnd, lparam.0); } return LRESULT(1); },
        WM_ENDSESSION => { if let Some(ctx) = CONTEXT.get() { end(ctx, hwnd, wparam.0 != 0, lparam.0); } return LRESULT(0); },
        WM_POWERBROADCAST if wparam.0 == PBT_APMRESUMEAUTOMATIC => {
            RESUMES.fetch_add(1, Ordering::AcqRel);
            if let Some(ctx) = CONTEXT.get() { ctx.log("power_resume", &[]); }
        },
        WM_NCDESTROY => { let _ = RemoveWindowSubclass(hwnd, Some(subclass), SUBCLASS_ID); },
        _ => (),
    }
    DefSubclassProc(hwnd, message, wparam, lparam)
}
/// Subclasses the main (top-level) window; Tauri has no session-end callback.
pub fn install(hwnd: HWND, context: Context) {
    let data = context.data.clone();
    if CONTEXT.set(context).is_err() { crate::log_code(&data, "session_end_hook_duplicate"); return; }
    if !unsafe { SetWindowSubclass(hwnd, Some(subclass), SUBCLASS_ID, 0) }.as_bool() { crate::log_code(&data, "session_end_hook_failed"); }
}
/// T-P3-07: a main window recreated after a WebView2 browser-process loss gets the same hook
/// (the context installed by [`install`] is kept; the old window's subclass left with it).
pub fn reattach(hwnd: HWND) {
    let Some(ctx) = CONTEXT.get() else { return };
    if !unsafe { SetWindowSubclass(hwnd, Some(subclass), SUBCLASS_ID, 0) }.as_bool() { crate::log_code(&ctx.data, "session_end_hook_failed"); }
}
/// An installer's WM_ENDSESSION(TRUE), heard on the main window's hook or on tao's event-target
/// window ([`guard_event_loop`]), whichever gets it first: an ordinary quit (the second finds the
/// shell already quitting).
fn installer_close(ctx: &Context, window: &str) {
    ctx.log("installer_close", &[("window", window)]);
    ctx.lifecycle.installer_close();
}
/// What [`loop_guard`] keeps from tao: an installer's WM_ENDSESSION (TRUE or FALSE), and every
/// WM_CLOSE (tao never closes its window by message; the default handling would destroy it).
fn keeps_from_tao(message: u32, lparam: isize) -> bool { message == WM_CLOSE || (message == WM_ENDSESSION && closes_app_only(lparam)) }
/// Messages the guard kept from tao (tests: they really went through it).
static KEPT: AtomicU64 = AtomicU64::new(0);
unsafe extern "system" fn loop_guard(hwnd: HWND, message: u32, wparam: WPARAM, lparam: LPARAM, _: usize, _: usize) -> LRESULT {
    if keeps_from_tao(message, lparam.0) {
        KEPT.fetch_add(1, Ordering::AcqRel);
        if message == WM_ENDSESSION && wparam.0 != 0 { if let Some(ctx) = CONTEXT.get() { installer_close(ctx, "event_loop"); } }
        return LRESULT(0);
    }
    if message == WM_NCDESTROY { let _ = RemoveWindowSubclass(hwnd, Some(loop_guard), LOOP_GUARD_ID); }
    DefSubclassProc(hwnd, message, wparam, lparam)
}
/// Tao's hidden event-target window is a top-level window, so it hears every session-end
/// message, and tao ends its event loop at WM_ENDSESSION(TRUE) (`loop_destroyed`) because Windows
/// ends the process next. After an installer's close Windows does not: tao's next loop step
/// panicked ("cannot move state from Destroyed", `panic_runner.rs_371` in shell.log). Restart
/// Manager then also sends WM_CLOSE to every top-level window (seen 11-20), and the default
/// handling destroys this one: Tauri's `exit()` then finds no event loop to post to and calls
/// `process::exit` itself. Either way the shell left without its exit path (tray icon removal,
/// browser-exit wait, `shell_exit_*`). This subclasses that window: an installer's WM_ENDSESSION
/// stops here and quits as the main window's does, and a WM_CLOSE stops here too. Every other
/// message goes on to tao, a logoff's and a shutdown's included. Call on the UI thread (tao's),
/// once the event loop exists; returns how many windows it guards (1, or 0 when tao's window was
/// not found: logged).
pub fn guard_event_loop(data: &Path) -> usize {
    let found = thread_windows_of_class(TAO_EVENT_TARGET_CLASS);
    let guarded = guard_windows(&found);
    crate::shell_log::fields(data, "installer_close_guard", &[("found", &found.len().to_string()), ("guarded", &guarded.to_string())]);
    guarded
}
fn guard_windows(windows: &[HWND]) -> usize {
    windows.iter().filter(|hwnd| unsafe { SetWindowSubclass(**hwnd, Some(loop_guard), LOOP_GUARD_ID, 0) }.as_bool()).count()
}
/// This thread's top-level windows of window class `class`.
fn thread_windows_of_class(class: &str) -> Vec<HWND> {
    struct Search { class: Vec<u16>, found: Vec<HWND> }
    unsafe extern "system" fn visit(hwnd: HWND, lparam: LPARAM) -> BOOL {
        let search = &mut *(lparam.0 as *mut Search);
        let mut name = [0u16; 64];
        let len = GetClassNameW(hwnd, &mut name);
        if len > 0 && name[..len as usize] == search.class[..] { search.found.push(hwnd); }
        BOOL(1)
    }
    let mut search = Search { class: class.encode_utf16().collect(), found: Vec::new() };
    unsafe { let _ = EnumThreadWindows(GetCurrentThreadId(), Some(visit), LPARAM(&mut search as *mut Search as isize)); }
    search.found
}

#[cfg(test)] mod tests {
    use super::*;
    use crate::lifecycle::{Action, Effects, Sidecar, QuitReason};
    use serde_json::json;
    #[test] fn reason_follows_the_logoff_flag() {
        assert_eq!(reason(0x8000_0000u32 as i32 as isize), "logoff");
        assert_eq!(reason(0), "shutdown");
        assert_eq!(reason(0x1), "shutdown", "ENDSESSION_CLOSEAPP alone is not a logoff");
        assert_eq!(reason(0x8000_0001u32 as i32 as isize), "logoff");
        const { assert!(BUDGET_MS < 5000 && SHUTDOWN_LEVEL <= 0x3FF) };
    }
    #[test] fn request_frame_is_a_shell_only_p2_envelope() {
        let f = request_frame(9, "logoff", BUDGET_MS);
        assert_eq!((f["cmd"].as_str(), f["from"].as_str(), f["owner"].as_str()), (Some("app.sessionEnd"), Some("shell"), Some("shell")));
        assert_eq!(f["args"], json!({"reason":"logoff","budget_ms":4000}));
        assert_eq!(f["req_id"], "shell-session-end-9");
    }
    #[test] fn tracker_wait_returns_on_reply_bye_end_or_deadline() {
        let t = Arc::new(Tracker::default()); let now = Instant::now();
        t.begin("shutdown", BUDGET_MS, now);
        assert_eq!(t.take_request(), Some(("shutdown", BUDGET_MS))); assert_eq!(t.take_request(), None);
        t.sent(4);
        let waiter = { let t = t.clone(); std::thread::spawn(move || t.wait(Instant::now() + Duration::from_secs(5))) };
        std::thread::sleep(Duration::from_millis(50));
        t.reply(json!({"t":"res","id":4,"ok":true,"data":{"writes_revoked":true,"recovery":{"proxy":"restored","firewall":{"state":"not_needed"}},"workers_pending":false}}));
        let outcome = waiter.join().unwrap();
        assert!(outcome.sent && !outcome.timed_out && outcome.reply.is_some());
        let fields = result_fields(&outcome);
        for pair in [("writes_revoked","true"),("proxy","restored"),("firewall","not_needed"),("workers_pending","false"),("timeout","false")] {
            assert!(fields.iter().any(|(k,v)| *k == pair.0 && v == pair.1), "{pair:?} in {fields:?}");
        }
        let t = Tracker::default(); t.begin("logoff", 10, Instant::now());
        let started = Instant::now(); let outcome = t.wait(Instant::now() + Duration::from_millis(80));
        assert!(outcome.timed_out && !outcome.sent && started.elapsed() >= Duration::from_millis(80));
        let t = Tracker::default(); t.bye(); assert!(!t.wait(Instant::now() + Duration::from_secs(5)).timed_out);
        let t = Tracker::default(); t.ended(); assert!(!t.wait(Instant::now() + Duration::from_secs(5)).timed_out);
        // workers_pending:true is not final: keep waiting for bye within the budget.
        let t = Arc::new(Tracker::default()); t.begin("logoff", BUDGET_MS, Instant::now()); t.sent(5);
        t.reply(json!({"t":"res","id":5,"ok":true,"data":{"writes_revoked":true,"workers_pending":true}}));
        let waiter = { let t = t.clone(); std::thread::spawn(move || { let at = Instant::now(); (t.wait(Instant::now() + Duration::from_secs(5)), at.elapsed()) }) };
        std::thread::sleep(Duration::from_millis(150));
        assert!(!waiter.is_finished(), "a pending-workers reply does not end the wait");
        t.bye();
        let (outcome, waited) = waiter.join().unwrap();
        assert!(outcome.bye && !outcome.timed_out && waited >= Duration::from_millis(100), "{outcome:?} {waited:?}");
        // ...but only within the budget; then it reports how far it got.
        let t = Tracker::default(); t.sent(6);
        t.reply(json!({"t":"res","id":6,"ok":true,"data":{"workers_pending":true}}));
        let outcome = t.wait(Instant::now() + Duration::from_millis(60));
        assert!(outcome.timed_out && outcome.reply.is_some() && !outcome.bye);
        // An error reply is final.
        let t = Tracker::default(); t.reply(json!({"t":"error","id":7,"error":{"code":"conflict"}}));
        assert!(!t.wait(Instant::now() + Duration::from_secs(5)).timed_out);
        let fields = result_fields(&Outcome { sent: true, reply: Some(json!({"t":"error","error":{"code":"conflict"}})), bye: false, timed_out: false });
        assert!(fields.contains(&("code", "conflict".into())));
    }
    struct Recorder(Mutex<Vec<Action>>, bool);
    impl Effects for Arc<Recorder> {
        fn page_connected(&self) -> bool { self.1 }
        fn perform(&self, _: &Arc<Lifecycle>, action: Action) { self.0.lock().unwrap().push(action); }
    }
    fn context(page: bool) -> (Context, Arc<Recorder>) {
        let recorder = Arc::new(Recorder(Mutex::new(Vec::new()), page));
        let lifecycle = Lifecycle::new(Box::new(recorder.clone()));
        lifecycle.set_sidecar(Sidecar::Live);
        let data = std::env::temp_dir().join(format!("tesseract-endsession-{}", std::process::id()));
        (Context::new(lifecycle, Arc::new(Tracker::default()), data, Box::new(|| "zh_CN")), recorder)
    }
    #[test] fn not_quitting_sends_session_end_at_once_without_the_page() {
        // A connected page must not be asked: the request goes to the worker queue directly.
        let (ctx, recorder) = context(true);
        assert!(start(&ctx, "logoff", Instant::now()));
        assert_eq!(ctx.tracker.take_request(), Some(("logoff", BUDGET_MS)));
        let actions = recorder.0.lock().unwrap().clone();
        assert!(actions.contains(&Action::MarkQuitting));
        assert!(!actions.iter().any(|a| matches!(a, Action::AskPage(_) | Action::PromptUnsaved(_))), "{actions:?}");
        assert!(ctx.lifecycle.take_send().is_none(), "no app.quit is queued for a session end");
        assert!(!start(&ctx, "logoff", Instant::now()), "a repeated query sends nothing more");
        let _ = std::fs::remove_dir_all(&ctx.data);
    }
    #[test] fn update_quit_or_bye_allows_without_a_command() {
        let (ctx, _) = context(false);
        ctx.lifecycle.request(QuitReason::Update);
        assert!(ctx.lifecycle.quitting() && !ctx.lifecycle.session_end_needed());
        assert!(!start(&ctx, "shutdown", Instant::now()));
        assert_eq!(ctx.tracker.take_request(), None);
        assert!(!ctx.tracker.active());
        let (ctx, _) = context(false);
        ctx.lifecycle.request(QuitReason::Tray); ctx.lifecycle.take_send(); ctx.lifecycle.bye();
        assert!(!start(&ctx, "logoff", Instant::now()), "bye received: allow");
        assert_eq!(ctx.tracker.take_request(), None);
        let _ = std::fs::remove_dir_all(&ctx.data);
    }
    #[test] fn quit_sent_without_bye_still_sends_session_end() {
        let (ctx, recorder) = context(false);
        ctx.lifecycle.request(QuitReason::Tray);
        assert_eq!(ctx.lifecycle.take_send(), Some(QuitReason::Tray), "app.quit is on its way");
        assert!(ctx.lifecycle.quitting());
        assert!(start(&ctx, "logoff", Instant::now()), "app.quit without bye must not just allow");
        assert_eq!(ctx.tracker.take_request(), Some(("logoff", BUDGET_MS)), "the worker sends app.sessionEnd");
        assert!(recorder.0.lock().unwrap().contains(&Action::Log("session_end_during_quit_tray".into())));
        assert!(!start(&ctx, "logoff", Instant::now()), "once per session end");
        let _ = std::fs::remove_dir_all(&ctx.data);
    }
    const LOGOFF: isize = 0x8000_0000u32 as i32 as isize;
    #[test] fn only_a_polite_restart_manager_close_is_an_installer_close() {
        assert!(closes_app_only(0x1));
        assert!(!closes_app_only(0), "a shutdown");
        assert!(!closes_app_only(LOGOFF), "a logoff");
        assert!(!closes_app_only(LOGOFF | 0x1), "a logoff that also closes apps ends the session");
        assert!(!closes_app_only(0x4000_0000), "a critical shutdown");
        assert!(!closes_app_only(0x4000_0001), "a forced close: Windows ends the process after its timeout");
        // What the guard keeps from tao: an installer's WM_ENDSESSION, and WM_CLOSE.
        assert!(keeps_from_tao(WM_ENDSESSION, 0x1) && keeps_from_tao(WM_CLOSE, 0));
        for lparam in [0, LOGOFF, LOGOFF | 0x1, 0x4000_0001] { assert!(!keeps_from_tao(WM_ENDSESSION, lparam), "{lparam:#x}"); }
        assert!(!keeps_from_tao(WM_QUERYENDSESSION, 0x1) && !keeps_from_tao(WM_NCDESTROY, 0x1));
    }
    #[test] fn an_installer_close_quits_like_the_tray_and_lets_windows_go_at_once() {
        // A connected page is not asked either. The query only allows: nothing starts, no block reason.
        let (ctx, recorder) = context(true);
        query(&ctx, HWND::default(), 0x1);
        assert!(ctx.lifecycle.running() && !ctx.tracker.active() && !ctx.blocked.load(Ordering::Acquire));
        // WM_ENDSESSION(TRUE): the ordinary quit starts and Windows is let go at once, although no
        // bye has come (a session end would wait up to its 4 s budget here).
        let at = Instant::now();
        end(&ctx, HWND::default(), true, 0x1);
        assert!(at.elapsed() < Duration::from_millis(500), "{:?}", at.elapsed());
        assert_eq!(ctx.lifecycle.take_send(), Some(QuitReason::InstallerClose), "app.quit{{reason:user}} for the worker");
        assert_eq!(ctx.tracker.take_request(), None, "no app.sessionEnd");
        // The tracker stays idle, so the exit waits for the browser like a tray quit's.
        assert!(!ctx.tracker.active());
        assert_eq!(crate::webview::exit_wait(ctx.tracker.active(), false), Ok(crate::webview::EXIT_WAIT));
        let actions = recorder.0.lock().unwrap().clone();
        assert!(!actions.iter().any(|a| matches!(a, Action::AskPage(_) | Action::PromptUnsaved(_))), "{actions:?}");
        for code in ["quit_requested_installer_close", "quit_send_installer_close"] { assert!(actions.contains(&Action::Log(code.into())), "{code} in {actions:?}"); }
        // The 20 s prompt is armed from the send on, as for a tray quit.
        assert_eq!(ctx.lifecycle.machine().tick(Instant::now() + crate::lifecycle::STALL).last(), Some(&Action::PromptStall));
        // tao's event-target window hears the same WM_ENDSESSION: nothing more goes out.
        installer_close(&ctx, "event_loop");
        assert!(ctx.lifecycle.take_send().is_none());
        assert!(recorder.0.lock().unwrap().contains(&Action::Log("installer_close_already_quitting".into())));
        let _ = std::fs::remove_dir_all(&ctx.data);
    }
    #[test] fn a_cancelled_installer_close_leaves_the_app_running() {
        let (ctx, recorder) = context(false);
        query(&ctx, HWND::default(), 0x1);
        end(&ctx, HWND::default(), false, 0x1);
        assert!(ctx.lifecycle.running() && ctx.lifecycle.take_send().is_none() && ctx.tracker.take_request().is_none());
        assert!(!recorder.0.lock().unwrap().contains(&Action::MarkQuitting));
        let _ = std::fs::remove_dir_all(&ctx.data);
    }
    #[test] fn logoffs_shutdowns_and_forced_closes_keep_the_session_end() {
        // Unchanged since 0.1.0: app.sessionEnd with the 4 s budget, never app.quit, no 20 s prompt.
        for (lparam, why) in [(LOGOFF, "logoff"), (LOGOFF | 0x1, "logoff"), (0, "shutdown"), (0x4000_0000, "shutdown"), (0x4000_0001, "shutdown")] {
            let (ctx, recorder) = context(false);
            query(&ctx, HWND::default(), lparam);
            assert_eq!(ctx.tracker.take_request(), Some((why, BUDGET_MS)), "{lparam:#x}");
            ctx.tracker.ended(); // the session is gone: no budget wait in this test
            end(&ctx, HWND::default(), true, lparam);
            assert!(ctx.lifecycle.take_send().is_none(), "{lparam:#x}: no app.quit");
            assert!(ctx.lifecycle.machine().tick(Instant::now() + Duration::from_secs(60)).is_empty(), "{lparam:#x}: no stall prompt");
            let actions = recorder.0.lock().unwrap().clone();
            assert!(actions.contains(&Action::Log("quit_requested_session_end".into())), "{lparam:#x}");
            assert!(!actions.iter().any(|a| matches!(a, Action::Log(c) if c.starts_with("installer_close"))), "{lparam:#x}: {actions:?}");
            let _ = std::fs::remove_dir_all(&ctx.data);
        }
    }
    #[test] fn a_logoff_after_an_installer_close_still_sends_the_session_end() {
        let (ctx, recorder) = context(false);
        end(&ctx, HWND::default(), true, 0x1);
        assert_eq!(ctx.lifecycle.take_send(), Some(QuitReason::InstallerClose), "the worker sent app.quit; no bye yet");
        query(&ctx, HWND::default(), LOGOFF);
        assert_eq!(ctx.tracker.take_request(), Some(("logoff", BUDGET_MS)));
        assert!(recorder.0.lock().unwrap().contains(&Action::Log("session_end_during_quit_installer_close".into())));
        let _ = std::fs::remove_dir_all(&ctx.data);
    }
    /// What Restart Manager sends each top-level window for a polite close (RmShutdown without
    /// RmForceShutdown, probed 11-20): the query, then WM_ENDSESSION(TRUE), then WM_CLOSE.
    const INSTALLER_CLOSE: [(u32, usize, isize); 2] = [(WM_ENDSESSION, 1, 0x1), (WM_CLOSE, 0, 0)];
    /// Tao's own event loop (the version tauri runs, Cargo.lock) gets `messages` on its
    /// event-target window while it waits, as Restart Manager's arrive from another process.
    /// With `then_exit`, another thread then asks the loop to exit through its proxy after 100 ms,
    /// as Tauri's `exit()` does (a proxy that cannot post makes Tauri call `process::exit`).
    /// Returns the loop events seen ("exit_requested", "resumed": a 2 s fallback ran out,
    /// "loop_destroyed"), how many messages the guard kept, and whether the proxy could post. A
    /// loop tao ended by itself leaves through WM_QUIT, before its next step would panic as in
    /// 0.1.0–0.1.3 (`panic_runner.rs_371`).
    fn tao_through(messages: &[(u32, usize, isize)], guard: bool, then_exit: bool) -> (Vec<&'static str>, u64, Option<bool>) {
        use tao::{event::{Event, StartCause}, event_loop::{ControlFlow, DeviceEventFilter, EventLoopBuilder},
            platform::{run_return::EventLoopExtRunReturn, windows::EventLoopBuilderExtWindows}};
        use windows::Win32::UI::WindowsAndMessaging::{PostMessageW, PostQuitMessage, PostThreadMessageW, WM_QUIT};
        let messages = messages.to_vec();
        std::thread::spawn(move || {
            let mut event_loop = EventLoopBuilder::new().with_any_thread(true).with_dpi_aware(false).build();
            event_loop.set_device_event_filter(DeviceEventFilter::Always); // no raw input in between
            let targets = thread_windows_of_class(TAO_EVENT_TARGET_CLASS);
            assert_eq!(targets.len(), 1, "tao's event-target window, found by its class name (tao updated?)");
            if guard { assert_eq!(guard_windows(&targets), 1); }
            let (target, kept, proxy, thread) = (targets[0], KEPT.load(Ordering::Acquire), event_loop.create_proxy(), unsafe { GetCurrentThreadId() });
            let mut exit: Option<std::thread::JoinHandle<bool>> = None;
            let mut seen = Vec::new();
            event_loop.run_return(|event, _, flow| match event {
                Event::NewEvents(StartCause::Init) => {
                    *flow = ControlFlow::WaitUntil(Instant::now() + Duration::from_secs(2));
                    for (message, wparam, lparam) in &messages { unsafe { PostMessageW(Some(target), *message, WPARAM(*wparam), LPARAM(*lparam)) }.unwrap(); }
                    if then_exit {
                        let proxy = proxy.clone();
                        exit = Some(std::thread::spawn(move || {
                            std::thread::sleep(Duration::from_millis(100));
                            let posted = proxy.send_event(()).is_ok();
                            if !posted { unsafe { let _ = PostThreadMessageW(thread, WM_QUIT, WPARAM(0), LPARAM(0)); } }
                            posted
                        }));
                    }
                },
                Event::UserEvent(()) => { seen.push("exit_requested"); *flow = ControlFlow::Exit; },
                Event::NewEvents(StartCause::ResumeTimeReached { .. }) => { seen.push("resumed"); *flow = ControlFlow::Exit; },
                Event::LoopDestroyed => {
                    seen.push("loop_destroyed");
                    if seen.len() == 1 { unsafe { PostQuitMessage(0) } }
                },
                _ => (),
            });
            (seen, KEPT.load(Ordering::Acquire) - kept, exit.map(|e| e.join().unwrap()))
        }).join().expect("tao's event loop panicked")
    }
    #[test] fn the_guard_keeps_taos_event_loop_through_an_installer_close() {
        // Unguarded, tao ends its loop at an installer's WM_ENDSESSION although the process lives on.
        assert_eq!(tao_through(&INSTALLER_CLOSE[..1], false, false), (vec!["loop_destroyed"], 0, None), "tao no longer needs the guard?");
        // Unguarded, the WM_CLOSE that follows destroys tao's window: Tauri's exit could not post.
        assert_eq!(tao_through(&INSTALLER_CLOSE[1..], false, true).2, Some(false), "tao keeps its window by itself now?");
        // Guarded, the loop and its window outlive the whole close; the exit request gets through.
        assert_eq!(tao_through(&INSTALLER_CLOSE, true, true), (vec!["exit_requested", "loop_destroyed"], 2, Some(true)));
        // A logoff still reaches tao (Windows ends the process right after, as before).
        assert_eq!(tao_through(&[(WM_ENDSESSION, 1, LOGOFF)], true, false), (vec!["loop_destroyed"], 0, None));
    }
}
