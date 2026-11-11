//! T-P3-04 logoff/shutdown (plan §2.2.4, PROTOCOL §13 "注销快路径").
//!
//! WM_QUERYENDSESSION: `bye` already received, an update quit, or our session end already sent
//! → allow, send nothing. A sent `app.quit` still waiting for `bye` does not count (T-P3-05,
//! decision P3-04 item 2): the session end is sent as well. Otherwise ShutdownBlockReasonCreate("正在恢复网络设置"), then hand `app.sessionEnd
//! {reason, budget_ms}` straight to the bridge worker's control queue (no WebView, never behind
//! `app.quit`) and allow at once. WM_ENDSESSION(TRUE) waits within the budget for the reply or
//! `bye`, logs the outcome, then ShutdownBlockReasonDestroy. Windows is never refused.
//! WM_POWERBROADCAST/PBT_APMRESUMEAUTOMATIC bumps the counter the pong watchdog resets on.
use crate::lifecycle::Lifecycle;
use serde_json::Value;
use std::{path::PathBuf, sync::{atomic::{AtomicBool, AtomicU64, Ordering}, Arc, Condvar, Mutex, OnceLock}, time::{Duration, Instant}};
use windows::Win32::Foundation::{HWND, LPARAM, LRESULT, WPARAM};
use windows::Win32::UI::Shell::{DefSubclassProc, RemoveWindowSubclass, SetWindowSubclass};

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
const PBT_APMRESUMEAUTOMATIC: usize = 0x0012;
pub const ENDSESSION_LOGOFF: u32 = 0x8000_0000;
const SUBCLASS_ID: usize = 0x5033_0004;

#[link(name = "kernel32")] extern "system" { fn SetProcessShutdownParameters(level: u32, flags: u32) -> i32; }
#[link(name = "user32")] extern "system" {
    fn ShutdownBlockReasonCreate(hwnd: isize, reason: *const u16) -> i32;
    fn ShutdownBlockReasonDestroy(hwnd: isize) -> i32;
}

static RESUMES: AtomicU64 = AtomicU64::new(0);
/// Process-wide count of PBT_APMRESUMEAUTOMATIC broadcasts seen by the main window.
pub fn resume_count() -> u64 { RESUMES.load(Ordering::Acquire) }

/// `ENDSESSION_LOGOFF` set → logoff, otherwise shutdown (including Restart Manager CLOSEAPP).
pub fn reason(lparam: isize) -> &'static str { if (lparam as u32) & ENDSESSION_LOGOFF != 0 { "logoff" } else { "shutdown" } }

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
    ctx.log("session_end_query", &[("reason", reason), ("closeapp", &((lparam as u32) & 0x1 != 0).to_string())]);
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
    if !ending {
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

#[cfg(test)] mod tests {
    use super::*;
    use crate::lifecycle::{Action, Effects, Sidecar, QuitReason};
    use serde_json::json;
    #[test] fn reason_follows_the_logoff_flag() {
        assert_eq!(reason(0x8000_0000u32 as i32 as isize), "logoff");
        assert_eq!(reason(0), "shutdown");
        assert_eq!(reason(0x1), "shutdown", "ENDSESSION_CLOSEAPP alone is not a logoff");
        assert_eq!(reason(0x8000_0001u32 as i32 as isize), "logoff");
        assert!(BUDGET_MS < 5000 && SHUTDOWN_LEVEL <= 0x3FF);
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
}
