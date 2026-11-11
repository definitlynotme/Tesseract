//! T-P3-04 quit state machine (plan §2.2.3): Running → QuitRequested → QuitSent → WaitingSidecar
//! → Exited. The machine is pure (injected clock, no Win32); `Lifecycle` serializes it behind one
//! lock and hands every resulting `Action` to an `Effects` port outside that lock.
use serde_json::Value;
use std::{sync::{Arc, Mutex, MutexGuard}, time::{Duration, Instant}};

/// Page reply window for `host:confirmQuit`.
pub const CONFIRM_WAIT: Duration = Duration::from_secs(3);
/// No progress for this long while quitting: ask the user (never kill on our own).
pub const STALL: Duration = Duration::from_secs(20);
/// Exit code of a user-forced quit (the sidecar was terminated before `bye`).
pub const FORCED_EXIT: i32 = 1;

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum QuitReason {
    Tray, WindowWithoutTray,
    /// Debug-build `dev_quit` command (dev page); it goes through the same confirmation.
    #[cfg_attr(not(debug_assertions), allow(dead_code))]
    Dev,
    /// Release `--verify-webview` acceptance probe; no page to ask.
    Acceptance,
    /// The Tauri host is going away (builder failure, channel closed).
    HostGone,
    /// T-P3-06 update hand-off: `app.quit{reason:"update"}`; after the sidecar exits the machine
    /// enters `Installing` instead of exiting (update.rs).
    Update,
    /// Windows session end (WM_QUERYENDSESSION / WM_ENDSESSION): `app.sessionEnd`, never `app.quit`.
    SessionEnd,
    /// The sidecar exited by itself with 0 or 74 outside a quit: the shell follows it out.
    SidecarExit,
    /// T-P3-07: the WebView2 browser process was lost and the window could not be recreated; the
    /// user chose 「退出」 on the native box. A user quit on the wire; there is no page to ask.
    WebViewLost,
}
impl QuitReason {
    pub fn code(self) -> &'static str {
        match self {
            Self::Tray=>"tray", Self::WindowWithoutTray=>"window_without_tray", Self::Dev=>"dev", Self::Acceptance=>"acceptance",
            Self::HostGone=>"host_gone", Self::Update=>"update", Self::SessionEnd=>"session_end", Self::SidecarExit=>"sidecar_exit",
            Self::WebViewLost=>"webview_lost",
        }
    }
    /// PROTOCOL §10 `app.quit.reason`: user | update | shell_lost.
    pub fn wire(self) -> &'static str {
        match self { Self::Update=>"update", Self::HostGone=>"shell_lost", _=>"user" }
    }
    /// Only an interactive user quit asks the page for unsaved changes.
    fn asks_page(self) -> bool { matches!(self, Self::Tray | Self::WindowWithoutTray | Self::Dev) }
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Sidecar { Starting, Live, Down }

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Phase {
    Running,
    /// QuitRequested, waiting up to 3 s for the page's unsaved-change count.
    Confirming { token: u64, since: Instant },
    /// QuitRequested, the native "N unsaved changes" dialog is open.
    Prompting { count: u32 },
    /// `app.quit` (or `app.sessionEnd`) handed to the worker; progress heartbeat armed.
    QuitSent { last_progress: Instant, stall_open: bool },
    /// `bye` received; waiting for the sidecar process to exit.
    WaitingSidecar { last_progress: Instant, stall_open: bool },
    /// T-P3-06: the sidecar of an update quit exited; the installer is being checked and started.
    /// Success exits 0; failure returns to Running (close-to-tray again) and the worker relaunches.
    Installing,
    Exited,
}
impl Phase {
    pub fn name(&self) -> &'static str {
        match self { Self::Running=>"running", Self::Confirming{..}=>"confirming", Self::Prompting{..}=>"prompting",
            Self::QuitSent{..}=>"quit_sent", Self::WaitingSidecar{..}=>"waiting_sidecar", Self::Installing=>"installing", Self::Exited=>"exited" }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub enum Action {
    Log(String),
    MarkQuitting,
    ClearQuitting,
    /// Send `host:confirmQuit{token}` over the page Channel.
    AskPage(u64),
    /// Native yes/no: "N changes are not saved, quit anyway?"; answer → `unsaved_answer`.
    PromptUnsaved(u32),
    /// Native "keep waiting / force quit" dialog; answer → `stall_answer`.
    PromptStall,
    DismissStall,
    /// TerminateProcess on the current sidecar (user-forced quit only).
    Terminate,
    /// T-P3-06: run the install step (update.rs) on its own thread; answer → `install_finished`.
    Install,
    /// T-P3-06: a failed install restores the main window (plan §2.2.5 step 5, PROTOCOL §13).
    RestoreWindow,
    Exit(i32),
}

pub struct Machine {
    pub phase: Phase,
    reason: Option<QuitReason>,
    token: u64,
    send: Option<QuitReason>,
    sidecar: Sidecar,
    bye: bool,
    /// Windows is ending the session: no 20 s prompt (nobody can answer it). Cleared again when
    /// Windows cancels the session end (WM_ENDSESSION FALSE), see `session_end_cancelled`.
    stall_suppressed: bool,
}
impl Default for Machine { fn default() -> Self { Self::new() } }
impl Machine {
    pub fn new() -> Self { Self { phase: Phase::Running, reason: None, token: 0, send: None, sidecar: Sidecar::Starting, bye: false, stall_suppressed: false } }
    pub fn reason(&self) -> Option<QuitReason> { self.reason }
    pub fn sidecar(&self) -> Sidecar { self.sidecar }
    /// Page commands are refused once the quit command is on its way.
    pub fn accepting(&self) -> bool { matches!(self.phase, Phase::Running | Phase::Confirming{..} | Phase::Prompting{..}) }
    /// `app.quit` or `app.sessionEnd` already sent (or the shell is done): session end just allows.
    pub fn quitting(&self) -> bool { !self.accepting() }
    pub fn finished(&self) -> bool { self.phase == Phase::Exited }

    fn exit(&mut self, code: i32, out: &mut Vec<Action>) {
        self.phase = Phase::Exited; self.send = None;
        out.push(Action::DismissStall); out.push(Action::Log(format!("quit_exit_{code}"))); out.push(Action::Exit(code));
    }
    /// QuitRequested → QuitSent. No live or starting sidecar: plan step 6, exit 0 at once.
    fn send_quit(&mut self, now: Instant, out: &mut Vec<Action>) {
        let reason = self.reason.unwrap_or(QuitReason::Tray);
        if self.sidecar == Sidecar::Down { out.push(Action::Log("quit_without_sidecar".into())); self.exit(0, out); return; }
        self.send = Some(reason);
        self.phase = Phase::QuitSent { last_progress: now, stall_open: false };
        out.push(Action::Log(format!("quit_send_{}", reason.code())));
    }
    pub fn request(&mut self, reason: QuitReason, now: Instant, page_connected: bool) -> Vec<Action> {
        let mut out = Vec::new();
        if self.phase != Phase::Running { return out; } // duplicate requests while quitting are ignored
        self.reason = Some(reason);
        out.push(Action::Log(format!("quit_requested_{}", reason.code())));
        out.push(Action::MarkQuitting);
        if self.sidecar == Sidecar::Down { self.send_quit(now, &mut out); }
        else if reason.asks_page() && page_connected {
            self.token += 1;
            self.phase = Phase::Confirming { token: self.token, since: now };
            out.push(Action::Log("quit_confirm_asked".into()));
            out.push(Action::AskPage(self.token));
        } else {
            out.push(Action::Log(if reason.asks_page() { "quit_confirm_skipped_no_page" } else { "quit_confirm_not_needed" }.into()));
            self.send_quit(now, &mut out);
        }
        out
    }
    /// `host_confirm_quit{token, unsaved}` from the page. A stale token is refused.
    pub fn page_reply(&mut self, token: u64, unsaved: u32, now: Instant) -> Result<Vec<Action>, &'static str> {
        let Phase::Confirming { token: expected, .. } = self.phase else { return Err("not_confirming") };
        if token != expected { return Err("stale_token"); }
        let mut out = vec![Action::Log(format!("quit_confirm_reply_{}", unsaved.min(999)))];
        if unsaved == 0 { self.send_quit(now, &mut out); }
        else { self.phase = Phase::Prompting { count: unsaved }; out.push(Action::PromptUnsaved(unsaved)); }
        Ok(out)
    }
    pub fn unsaved_answer(&mut self, proceed: bool, now: Instant) -> Vec<Action> {
        let mut out = Vec::new();
        if !matches!(self.phase, Phase::Prompting{..}) { return out; }
        if proceed { out.push(Action::Log("quit_unsaved_confirmed".into())); self.send_quit(now, &mut out); }
        else {
            // Back to Running; the second-launch "quitting" marker is cleared again.
            self.phase = Phase::Running; self.reason = None;
            out.push(Action::Log("quit_cancelled".into())); out.push(Action::ClearQuitting);
        }
        out
    }
    /// Driven by the bridge worker (~10 ms): confirmation timeout and the 20 s stall prompt.
    pub fn tick(&mut self, now: Instant) -> Vec<Action> {
        let mut out = Vec::new();
        match self.phase {
            Phase::Confirming { since, .. } if now.saturating_duration_since(since) >= CONFIRM_WAIT => {
                out.push(Action::Log("quit_confirm_timeout".into())); self.send_quit(now, &mut out);
            },
            Phase::QuitSent { last_progress, stall_open: false } | Phase::WaitingSidecar { last_progress, stall_open: false }
                if now.saturating_duration_since(last_progress) >= STALL && !self.stall_suppressed => {
                self.set_stall(true);
                out.push(Action::Log("quit_stall_prompt".into())); out.push(Action::PromptStall);
            },
            _ => (),
        }
        out
    }
    fn set_stall(&mut self, open: bool) {
        match &mut self.phase { Phase::QuitSent { stall_open, .. } | Phase::WaitingSidecar { stall_open, .. } => *stall_open = open, _ => () }
    }
    /// Any sidecar frame that is quit progress (see [`is_progress`]) restarts the 20 s clock.
    pub fn progress(&mut self, now: Instant) {
        match &mut self.phase { Phase::QuitSent { last_progress, .. } | Phase::WaitingSidecar { last_progress, .. } => *last_progress = now, _ => () }
    }
    pub fn stall_answer(&mut self, force: bool, now: Instant) -> Vec<Action> {
        let mut out = Vec::new();
        let open = matches!(self.phase, Phase::QuitSent { stall_open: true, .. } | Phase::WaitingSidecar { stall_open: true, .. });
        if !open { return out; }
        if force {
            out.push(Action::Log("quit_forced_by_user".into())); out.push(Action::Terminate);
            self.exit(FORCED_EXIT, &mut out);
        } else {
            self.set_stall(false); self.progress(now);
            out.push(Action::Log("quit_stall_continue".into()));
        }
        out
    }
    /// The worker takes the pending `app.quit` once a session is live.
    pub fn take_send(&mut self) -> Option<QuitReason> { self.send.take() }
    pub fn bye(&mut self, now: Instant) -> Vec<Action> {
        self.bye = true;
        if let Phase::QuitSent { stall_open, .. } = self.phase {
            self.phase = Phase::WaitingSidecar { last_progress: now, stall_open };
            return vec![Action::Log("quit_bye_received".into())];
        }
        Vec::new()
    }
    pub fn set_sidecar(&mut self, state: Sidecar) -> Vec<Action> {
        self.sidecar = state;
        let mut out = Vec::new();
        // A bye belongs to one sidecar process: a new session, or none at all, has not said it
        // (a sidecar that sent bye and then crashed must not let a later logoff skip sessionEnd).
        if matches!(state, Sidecar::Live | Sidecar::Down) { self.bye = false; }
        // A quit that was waiting for a (re)launch that never came: nothing left to stop.
        if state == Sidecar::Down && matches!(self.phase, Phase::QuitSent{..} | Phase::WaitingSidecar{..}) {
            out.push(Action::Log("quit_without_sidecar".into())); self.exit(0, &mut out);
        }
        // Still asking the page: with no sidecar there is nothing a page answer could protect.
        if state == Sidecar::Down && matches!(self.phase, Phase::Confirming{..}) { self.send_quit(Instant::now(), &mut out); }
        out
    }
    /// Worker, before every (re)launch: false once the shell has exited (no orphan sidecar).
    pub fn start_launch(&mut self) -> bool {
        if self.phase == Phase::Exited { return false; }
        self.sidecar = Sidecar::Starting; self.bye = false; true
    }
    /// The sidecar process of a quit (or session end) exited: leave with its exit code.
    /// Returns None when no quit is in flight, so the caller classifies the exit (crash.rs).
    pub fn sidecar_exited(&mut self, code: u32) -> Option<Vec<Action>> {
        if !matches!(self.phase, Phase::QuitSent{..} | Phase::WaitingSidecar{..}) { return None; }
        let mut out = Vec::new();
        if self.reason == Some(QuitReason::Update) {
            // The install step checks bye, the exit code and the recovery itself (update.rs).
            self.phase = Phase::Installing; self.send = None;
            out.push(Action::DismissStall); out.push(Action::Log("update_install_begin".into())); out.push(Action::Install);
            return Some(out);
        }
        // Without `bye` the cleanup truth is unknown: never report success with 0.
        let code = if self.bye { code as i32 } else if code == 0 { 1 } else { code as i32 };
        if !self.bye { out.push(Action::Log("quit_exit_without_bye".into())); }
        self.exit(code, &mut out);
        Some(out)
    }
    /// T-P3-06: the install step's outcome. Launched: exit 0. Failed: back to Running — closing
    /// hides to the tray again, the quitting marker is cleared, the window is restored and the
    /// worker relaunches the sidecar in the stopped state.
    pub fn install_finished(&mut self, launched: bool) -> Vec<Action> {
        let mut out = Vec::new();
        if self.phase != Phase::Installing { return out; }
        if launched { self.exit(0, &mut out); return out; }
        self.phase = Phase::Running; self.reason = None; self.bye = false; self.stall_suppressed = false;
        out.push(Action::Log("update_install_failed_resume".into())); out.push(Action::ClearQuitting); out.push(Action::RestoreWindow);
        out
    }
    pub fn installing(&self) -> bool { self.phase == Phase::Installing }
    /// The sidecar left on its own with 0 or 74 (plan §2.2.6): follow it out, no restart.
    pub fn follow_sidecar_exit(&mut self, code: u32) -> Vec<Action> {
        let mut out = Vec::new();
        if self.phase == Phase::Exited { return out; }
        self.reason = Some(QuitReason::SidecarExit);
        out.push(Action::Log(format!("quit_requested_{}", QuitReason::SidecarExit.code())));
        out.push(Action::MarkQuitting);
        self.exit(code as i32, &mut out);
        out
    }
    /// Whether a Windows session end still needs `app.sessionEnd`. It is allowed without a
    /// command only once `bye` arrived (the sidecar finished its cleanup), during an update quit
    /// (the installer hand-off owns the exit), for a session end already sent, or when the shell
    /// is done. A sent `app.quit` without `bye` is NOT enough: that quit may sit behind a long
    /// lock, while `app.sessionEnd` runs RT-2 on its own fast path (decision P3-04 item 2).
    pub fn session_end_needed(&self) -> bool {
        let owned = self.quitting() && matches!(self.reason, Some(QuitReason::Update | QuitReason::SessionEnd));
        !(self.finished() || self.bye || owned)
    }
    /// WM_QUERYENDSESSION / WM_ENDSESSION. True: the caller must send `app.sessionEnd` now.
    /// False: nothing more to send (see [`Self::session_end_needed`]) or nothing to stop; allow.
    pub fn begin_session_end(&mut self, now: Instant) -> (bool, Vec<Action>) {
        let mut out = Vec::new();
        if !self.session_end_needed() { out.push(Action::Log("session_end_already_quitting".into())); return (false, out); }
        if self.quitting() {
            // app.quit is out but no bye yet: the session end goes out as well (they coexist).
            out.push(Action::Log(format!("session_end_during_quit_{}", self.reason.unwrap_or(QuitReason::Tray).code())));
        } else {
            out.push(Action::Log(format!("quit_requested_{}", QuitReason::SessionEnd.code())));
            out.push(Action::MarkQuitting);
        }
        self.reason = Some(QuitReason::SessionEnd);
        if self.sidecar == Sidecar::Down {
            out.push(Action::Log("session_end_no_sidecar".into())); self.exit(0, &mut out);
            return (false, out);
        }
        // An app.quit the worker has not taken yet is superseded by app.sessionEnd.
        self.send = None;
        // Nobody can answer a stall dialog while Windows ends the session.
        if matches!(self.phase, Phase::QuitSent { stall_open: true, .. } | Phase::WaitingSidecar { stall_open: true, .. }) { out.push(Action::DismissStall); }
        // Any open unsaved-change prompt is obsolete; its late answer is ignored.
        self.phase = Phase::QuitSent { last_progress: now, stall_open: false };
        self.stall_suppressed = true;
        (true, out)
    }
    /// WM_ENDSESSION(FALSE): Windows cancelled the session end, but `app.sessionEnd` already put
    /// the sidecar into its final cleanup. The shell keeps waiting for `bye` like any quit, so the
    /// 20 s prompt is armed again (clock restarted) instead of a silent wait forever.
    pub fn session_end_cancelled(&mut self, now: Instant) -> Vec<Action> {
        if !self.stall_suppressed || self.reason != Some(QuitReason::SessionEnd) || self.finished() { return Vec::new(); }
        self.stall_suppressed = false;
        self.progress(now);
        vec![Action::Log("session_end_cancelled_stall_armed".into())]
    }
}

/// Quit progress: every sidecar frame except pure samples. `pong` only proves the read loop
/// lives, and `service.state` (re-sampled each second while active), `traffic.*` and
/// `logs.batch` keep flowing while a stop is stuck on the lifecycle lock, so they would hide
/// exactly the stall the 20 s prompt exists for. The same holds for `alerts.desktop` (T-P3-05):
/// alerts keep arriving while a stop is stuck. Jobs edges, the shell-only `shutdown`
/// topic, replies, change-only topics, `bye` and `fatal` all count.
pub fn is_progress(frame: &Value) -> bool {
    fn sample(topic: Option<&str>) -> bool { matches!(topic, Some("service.state" | "traffic.tick" | "traffic.history" | "logs.batch" | "alerts.desktop")) }
    match frame["t"].as_str() {
        Some("pong") | None => false,
        Some("evt") => !sample(frame["topic"].as_str()),
        Some("batch") => frame["items"].as_array().is_some_and(|items| items.iter().any(|item| !sample(item["topic"].as_str()))),
        Some(_) => true,
    }
}

/// What `Lifecycle::sidecar_exited` decided for an exited sidecar.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Exited {
    /// No quit in flight: the caller classifies the exit (crash.rs).
    Unhandled,
    /// A quit or session end: the shell leaves with the sidecar.
    Leaving,
    /// An update quit entered `Installing` (T-P3-06): the install outcome decides exit or relaunch.
    Installing,
}

pub trait Effects: Send + Sync {
    fn page_connected(&self) -> bool;
    fn perform(&self, lifecycle: &Arc<Lifecycle>, action: Action);
}
pub struct Lifecycle { machine: Mutex<Machine>, effects: Box<dyn Effects> }
impl Lifecycle {
    pub fn new(effects: Box<dyn Effects>) -> Arc<Self> { Arc::new(Self { machine: Mutex::new(Machine::new()), effects }) }
    pub fn machine(&self) -> MutexGuard<'_, Machine> { self.machine.lock().unwrap_or_else(|e| e.into_inner()) }
    /// Effects run after the machine lock is released: dialogs and exits never hold it.
    pub fn run(self: &Arc<Self>, actions: Vec<Action>) { for action in actions { self.effects.perform(self, action); } }
    pub fn request(self: &Arc<Self>, reason: QuitReason) {
        let page = self.effects.page_connected();
        let actions = self.machine().request(reason, Instant::now(), page);
        self.run(actions);
    }
    pub fn page_reply(self: &Arc<Self>, token: u64, unsaved: u32) -> Result<(), &'static str> {
        let actions = self.machine().page_reply(token, unsaved, Instant::now())?;
        self.run(actions); Ok(())
    }
    pub fn unsaved_answer(self: &Arc<Self>, proceed: bool) { let a = self.machine().unsaved_answer(proceed, Instant::now()); self.run(a); }
    pub fn stall_answer(self: &Arc<Self>, force: bool) { let a = self.machine().stall_answer(force, Instant::now()); self.run(a); }
    pub fn tick(self: &Arc<Self>) { let a = self.machine().tick(Instant::now()); self.run(a); }
    pub fn progress(&self) { self.machine().progress(Instant::now()); }
    pub fn bye(self: &Arc<Self>) { let a = self.machine().bye(Instant::now()); self.run(a); }
    pub fn take_send(&self) -> Option<QuitReason> { self.machine().take_send() }
    pub fn set_sidecar(self: &Arc<Self>, state: Sidecar) { let a = self.machine().set_sidecar(state); self.run(a); }
    pub fn start_launch(&self) -> bool { self.machine().start_launch() }
    /// The machine's own verdict, taken before any effect runs: an install step started here may
    /// already have finished (and returned to Running) by the time the caller looks again.
    pub fn sidecar_exited(self: &Arc<Self>, code: u32) -> Exited {
        let actions = self.machine().sidecar_exited(code);
        match actions {
            Some(a) => { let installing = a.contains(&Action::Install); self.run(a); if installing { Exited::Installing } else { Exited::Leaving } },
            None => Exited::Unhandled,
        }
    }
    pub fn follow_sidecar_exit(self: &Arc<Self>, code: u32) { let a = self.machine().follow_sidecar_exit(code); self.run(a); }
    /// T-P3-06: `app.quit{reason}` only while nothing else is quitting. False: another quit (or a
    /// confirmation) is already running and this request was not taken.
    pub fn request_if_running(self: &Arc<Self>, reason: QuitReason) -> bool {
        let page = self.effects.page_connected();
        let actions = { let mut machine = self.machine(); if machine.phase != Phase::Running { return false; } machine.request(reason, Instant::now(), page) };
        self.run(actions); true
    }
    pub fn install_finished(self: &Arc<Self>, launched: bool) { let a = self.machine().install_finished(launched); self.run(a); }
    pub fn installing(&self) -> bool { self.machine().installing() }
    pub fn running(&self) -> bool { self.machine().phase == Phase::Running }
    pub fn session_end_cancelled(self: &Arc<Self>) { let a = self.machine().session_end_cancelled(Instant::now()); self.run(a); }
    pub fn begin_session_end(self: &Arc<Self>) -> bool {
        let (send, actions) = self.machine().begin_session_end(Instant::now());
        self.run(actions); send
    }
    pub fn session_end_needed(&self) -> bool { self.machine().session_end_needed() }
    pub fn accepting(&self) -> bool { self.machine().accepting() }
    pub fn quitting(&self) -> bool { self.machine().quitting() }
    pub fn finished(&self) -> bool { self.machine().finished() }
}

#[cfg(test)] pub mod tests {
    use super::*;
    use serde_json::json;
    fn clock() -> impl Fn(u64) -> Instant { let base = Instant::now(); move |ms| base + Duration::from_millis(ms) }
    fn live() -> Machine { let mut m = Machine::new(); m.set_sidecar(Sidecar::Live); m }
    fn has(actions: &[Action], wanted: &Action) -> bool { actions.contains(wanted) }

    #[test] fn confirm_zero_unsaved_sends_quit_and_bye_exit_uses_sidecar_code() {
        let t = clock(); let mut m = live();
        let a = m.request(QuitReason::Tray, t(0), true);
        assert_eq!(a[..2], [Action::Log("quit_requested_tray".into()), Action::MarkQuitting]);
        assert!(has(&a, &Action::AskPage(1)) && matches!(m.phase, Phase::Confirming { token: 1, .. }));
        assert!(m.accepting(), "page commands still flow while only asking");
        assert!(m.request(QuitReason::Tray, t(5), true).is_empty(), "duplicate tray quit is ignored");
        assert_eq!(m.page_reply(7, 0, t(10)), Err("stale_token"));
        m.page_reply(1, 0, t(10)).unwrap();
        assert_eq!(m.take_send(), Some(QuitReason::Tray)); assert_eq!(m.take_send(), None);
        assert!(m.quitting() && !m.accepting());
        assert_eq!(m.bye(t(20)), vec![Action::Log("quit_bye_received".into())]);
        assert!(matches!(m.phase, Phase::WaitingSidecar{..}));
        let a = m.sidecar_exited(74).unwrap();
        assert_eq!(a.last(), Some(&Action::Exit(74)), "the shell leaves with the sidecar's exit code");
        assert!(m.finished());
    }
    #[test] fn unsaved_changes_prompt_confirm_or_cancel() {
        let t = clock(); let mut m = live();
        m.request(QuitReason::Tray, t(0), true);
        assert_eq!(m.page_reply(1, 3, t(100)).unwrap().last(), Some(&Action::PromptUnsaved(3)));
        assert!(m.tick(t(10_000)).is_empty(), "no timeout while the user decides");
        let a = m.unsaved_answer(false, t(200));
        assert!(has(&a, &Action::ClearQuitting) && m.phase == Phase::Running && m.take_send().is_none());
        // A new request asks again with a fresh token; confirming sends app.quit.
        m.request(QuitReason::Tray, t(300), true);
        assert_eq!(m.page_reply(1, 2, t(310)), Err("stale_token"));
        m.page_reply(2, 2, t(310)).unwrap();
        m.unsaved_answer(true, t(320));
        assert_eq!(m.take_send(), Some(QuitReason::Tray));
        assert!(m.unsaved_answer(false, t(330)).is_empty(), "a late answer changes nothing");
    }
    #[test] fn silent_or_absent_page_continues() {
        let t = clock(); let mut m = live();
        m.request(QuitReason::Tray, t(0), true);
        assert!(m.tick(t(2_999)).is_empty());
        let a = m.tick(t(3_000));
        assert!(has(&a, &Action::Log("quit_confirm_timeout".into())) && m.take_send() == Some(QuitReason::Tray));
        assert_eq!(m.page_reply(1, 5, t(3_100)), Err("not_confirming"), "a late reply cannot reopen the prompt");
        // No page channel at all: skip the question.
        let mut m = live();
        let a = m.request(QuitReason::WindowWithoutTray, t(0), false);
        assert!(has(&a, &Action::Log("quit_confirm_skipped_no_page".into())) && !a.iter().any(|x| matches!(x, Action::AskPage(_))));
        assert_eq!(m.take_send(), Some(QuitReason::WindowWithoutTray));
        // Non-interactive reasons never ask.
        let mut m = live(); m.request(QuitReason::Acceptance, t(0), true); assert_eq!(m.take_send(), Some(QuitReason::Acceptance));
        assert_eq!((QuitReason::Tray.wire(), QuitReason::Update.wire(), QuitReason::HostGone.wire()), ("user", "update", "shell_lost"));
    }
    #[test] fn twenty_seconds_without_progress_offers_wait_or_force() {
        let t = clock(); let mut m = live();
        m.request(QuitReason::Tray, t(0), false); m.take_send();
        assert!(m.tick(t(19_999)).is_empty());
        m.progress(t(15_000));
        assert!(m.tick(t(34_999)).is_empty(), "progress restarts the 20 s clock");
        let a = m.tick(t(35_000)); assert_eq!(a.last(), Some(&Action::PromptStall));
        assert!(m.tick(t(60_000)).is_empty(), "one dialog at a time");
        // Keep waiting: the clock restarts from the answer; the shell never kills by itself.
        let a = m.stall_answer(false, t(61_000));
        assert_eq!(a, vec![Action::Log("quit_stall_continue".into())]);
        assert!(m.tick(t(80_999)).is_empty());
        assert_eq!(m.tick(t(81_000)).last(), Some(&Action::PromptStall));
        // Force: terminate the sidecar, then leave with FORCED_EXIT.
        let a = m.stall_answer(true, t(82_000));
        let terminate = a.iter().position(|x| *x == Action::Terminate).unwrap();
        let exit = a.iter().position(|x| *x == Action::Exit(FORCED_EXIT)).unwrap();
        assert!(terminate < exit && m.finished());
        assert!(m.stall_answer(true, t(83_000)).is_empty());
    }
    #[test] fn stall_prompt_also_covers_bye_without_process_exit_and_bye_dismisses() {
        let t = clock(); let mut m = live();
        m.request(QuitReason::Tray, t(0), false); m.take_send();
        assert_eq!(m.tick(t(20_000)).last(), Some(&Action::PromptStall));
        m.bye(t(21_000));
        assert!(matches!(m.phase, Phase::WaitingSidecar { stall_open: true, .. }));
        let a = m.sidecar_exited(0).unwrap();
        assert!(has(&a, &Action::DismissStall) && a.last() == Some(&Action::Exit(0)));
        // Exit without bye never reports 0.
        let mut m = live(); m.request(QuitReason::Tray, t(0), false); m.take_send();
        let a = m.sidecar_exited(0).unwrap(); assert_eq!(a.last(), Some(&Action::Exit(1)));
        let mut m = live(); m.request(QuitReason::Tray, t(0), false); m.take_send();
        assert_eq!(m.sidecar_exited(3).unwrap().last(), Some(&Action::Exit(3)));
    }
    #[test] fn no_sidecar_exits_zero_at_once() {
        let t = clock(); let mut m = live(); m.set_sidecar(Sidecar::Down);
        let a = m.request(QuitReason::Tray, t(0), true);
        assert!(!a.iter().any(|x| matches!(x, Action::AskPage(_))));
        assert_eq!(a.last(), Some(&Action::Exit(0))); assert!(m.finished());
        // A quit waiting for a relaunch that failed also leaves with 0.
        let mut m = Machine::new();
        m.request(QuitReason::Tray, t(0), false);
        assert_eq!(m.take_send(), Some(QuitReason::Tray));
        assert_eq!(m.set_sidecar(Sidecar::Down).last(), Some(&Action::Exit(0)));
        // Outside a quit the exit is not ours to classify.
        let mut m = live(); assert!(m.sidecar_exited(9).is_none());
        assert_eq!(m.follow_sidecar_exit(74).last(), Some(&Action::Exit(74)));
        assert!(!m.start_launch(), "an exited shell never launches another sidecar");
        // Crash while the page is being asked: nothing to protect, leave with 0.
        let mut m = live(); m.request(QuitReason::Tray, t(0), true);
        assert_eq!(m.set_sidecar(Sidecar::Down).last(), Some(&Action::Exit(0)));
        let mut m = live(); m.set_sidecar(Sidecar::Down); assert!(m.start_launch()); assert_eq!(m.sidecar(), Sidecar::Starting);
    }
    #[test] fn session_end_allows_while_quitting_and_starts_otherwise() {
        let t = clock(); let mut m = live();
        let (send, a) = m.begin_session_end(t(0));
        assert!(send && has(&a, &Action::MarkQuitting) && m.quitting());
        assert!(m.take_send().is_none(), "session end never queues app.quit");
        assert!(m.tick(t(60_000)).is_empty(), "no stall dialog during logoff");
        let (again, _) = m.begin_session_end(t(10)); assert!(!again);
        // An update quit owns the exit: allow, send nothing.
        let mut m = live(); m.request(QuitReason::Update, t(0), false); assert_eq!(m.take_send(), Some(QuitReason::Update));
        let (send, a) = m.begin_session_end(t(1));
        assert!(!send && a == vec![Action::Log("session_end_already_quitting".into())]);
        // bye already received: the cleanup is done, allow.
        let mut m = live(); m.request(QuitReason::Tray, t(0), false); m.take_send(); m.bye(t(1));
        assert!(!m.session_end_needed() && !m.begin_session_end(t(2)).0);
        // Unsaved prompt open: the session end still goes out; the late answer is ignored.
        let mut m = live(); m.request(QuitReason::Tray, t(0), true); m.page_reply(1, 4, t(1)).unwrap();
        assert!(m.begin_session_end(t(2)).0);
        assert!(m.unsaved_answer(false, t(3)).is_empty() && m.take_send().is_none());
        // No sidecar: nothing to send, the shell just leaves.
        let mut m = live(); m.set_sidecar(Sidecar::Down);
        let (send, a) = m.begin_session_end(t(0)); assert!(!send && a.last() == Some(&Action::Exit(0)));
    }
    #[test] fn quit_sent_without_bye_still_sends_session_end() {
        // Decision P3-04 item 2: app.quit is out (stuck behind a long lock, say), no bye yet.
        let t = clock(); let mut m = live();
        m.request(QuitReason::Tray, t(0), false);
        assert_eq!(m.take_send(), Some(QuitReason::Tray), "app.quit handed to the worker");
        assert!(m.quitting() && m.session_end_needed());
        let (send, a) = m.begin_session_end(t(5));
        assert!(send, "the session end must not just be allowed");
        assert_eq!(a, vec![Action::Log("session_end_during_quit_tray".into())]);
        assert_eq!(m.reason(), Some(QuitReason::SessionEnd));
        assert!(m.tick(t(60_000)).is_empty(), "no stall dialog while Windows ends the session");
        assert!(!m.begin_session_end(t(6)).0, "only once");
        // The quit was requested but the worker had not taken it: sessionEnd supersedes it.
        let mut m = live(); m.request(QuitReason::Tray, t(0), false);
        assert!(m.begin_session_end(t(1)).0);
        assert_eq!(m.take_send(), None, "no app.quit after the session end");
        // An open stall dialog is dismissed; a later answer changes nothing.
        let mut m = live(); m.request(QuitReason::HostGone, t(0), false); m.take_send();
        assert_eq!(m.tick(t(20_000)).last(), Some(&Action::PromptStall));
        let (send, a) = m.begin_session_end(t(20_001));
        assert!(send && a.contains(&Action::DismissStall));
        assert!(m.stall_answer(true, t(20_002)).is_empty());
        // The sidecar exits after its bye as usual.
        m.bye(t(20_100)); assert_eq!(m.sidecar_exited(0).unwrap().last(), Some(&Action::Exit(0)));
    }
    #[test] fn a_bye_does_not_outlive_its_sidecar() {
        // The sidecar said bye, then exited with a crash code and is relaunched: a logoff before
        // the next hello still needs app.sessionEnd (the bye belonged to the dead process).
        let t = clock(); let mut m = live();
        m.bye(t(0));
        assert!(!m.session_end_needed(), "bye from the live sidecar: its cleanup is done");
        m.set_sidecar(Sidecar::Down);
        assert!(m.start_launch());
        assert!(m.session_end_needed(), "after bye -> Down -> Starting a logoff needs sessionEnd");
        let (send, a) = m.begin_session_end(t(10));
        assert!(send && has(&a, &Action::MarkQuitting));
        // bye, then the start of a relaunch alone (no Down in between) also forgets it.
        let mut m = live(); m.bye(t(0)); assert!(m.start_launch()); assert!(m.session_end_needed());
        // In the restart pause (Down) a logoff makes the shell leave instead of relaunching.
        let mut m = live(); m.bye(t(0)); m.set_sidecar(Sidecar::Down);
        let (send, a) = m.begin_session_end(t(1));
        assert!(!send && a.last() == Some(&Action::Exit(0)) && m.finished());
        assert!(!m.start_launch(), "no relaunch after the session end");
    }
    #[test] fn cancelled_session_end_rearms_the_stall_prompt() {
        let t = clock(); let mut m = live();
        assert!(m.begin_session_end(t(0)).0);
        assert!(m.tick(t(30_000)).is_empty(), "silent while Windows ends the session");
        assert_eq!(m.session_end_cancelled(t(40_000)), vec![Action::Log("session_end_cancelled_stall_armed".into())]);
        assert!(m.tick(t(59_999)).is_empty(), "the 20 s clock restarts at the cancellation");
        assert_eq!(m.tick(t(60_000)).last(), Some(&Action::PromptStall));
        assert!(m.session_end_cancelled(t(61_000)).is_empty(), "only once");
        assert_eq!(m.stall_answer(true, t(62_000)).last(), Some(&Action::Exit(FORCED_EXIT)));
        // Not our session end (tray quit already running, or finished): nothing changes.
        let mut m = live(); m.request(QuitReason::Tray, t(0), false);
        assert!(m.session_end_cancelled(t(1)).is_empty());
    }
    #[test] fn update_quit_installs_instead_of_exiting_and_failure_resumes() {
        let t = clock(); let mut m = live();
        m.request(QuitReason::Update, t(0), true);
        assert!(!matches!(m.phase, Phase::Confirming{..}), "an update quit never asks the page");
        assert_eq!(m.take_send(), Some(QuitReason::Update));
        m.bye(t(10));
        let a = m.sidecar_exited(0).unwrap();
        assert_eq!(a.last(), Some(&Action::Install));
        assert!(!a.iter().any(|x| matches!(x, Action::Exit(_))), "no exit before the installer started");
        assert!(m.installing() && m.quitting() && !m.accepting());
        assert!(m.tick(t(60_000)).is_empty(), "no stall prompt while installing");
        assert!(!m.session_end_needed(), "the hand-off owns the exit; the sidecar is gone");
        assert!(m.set_sidecar(Sidecar::Down).is_empty(), "Down while installing does not exit");
        // Launched: exit 0.
        assert_eq!(m.install_finished(true).last(), Some(&Action::Exit(0)));
        assert!(m.finished());
        // Failed: back to Running, marker cleared, window restored; a relaunch is allowed.
        let mut m = live(); m.request(QuitReason::Update, t(0), false); m.take_send(); m.bye(t(1));
        m.sidecar_exited(0).unwrap();
        let a = m.install_finished(false);
        assert_eq!(a, vec![Action::Log("update_install_failed_resume".into()), Action::ClearQuitting, Action::RestoreWindow]);
        assert!(m.phase == Phase::Running && m.reason().is_none() && m.accepting());
        assert!(m.start_launch());
        assert!(m.install_finished(true).is_empty(), "only while installing");
        // The same exit outside an update quit still exits with the sidecar's code.
        let mut m = live(); m.request(QuitReason::Tray, t(0), false); m.take_send(); m.bye(t(1));
        assert_eq!(m.sidecar_exited(0).unwrap().last(), Some(&Action::Exit(0)));
    }
    #[test] fn request_if_running_refuses_while_another_quit_runs() {
        #[derive(Default)] struct Quiet;
        impl Effects for Quiet { fn page_connected(&self) -> bool { true } fn perform(&self, _: &Arc<Lifecycle>, _: Action) {} }
        let lc = Lifecycle::new(Box::new(Quiet)); lc.set_sidecar(Sidecar::Live);
        lc.request(QuitReason::Tray);
        assert!(matches!(lc.machine().phase, Phase::Confirming{..}));
        assert!(!lc.request_if_running(QuitReason::Update), "a user quit asking the page is another quit");
        let lc = Lifecycle::new(Box::new(Quiet)); lc.set_sidecar(Sidecar::Live);
        assert!(lc.request_if_running(QuitReason::Update));
        assert_eq!(lc.take_send(), Some(QuitReason::Update));
    }
    #[test] fn progress_excludes_samples_and_pong() {
        for frame in [json!({"t":"pong","n":1}), json!({"t":"evt","topic":"service.state"}), json!({"t":"evt","topic":"traffic.tick"}),
            json!({"t":"evt","topic":"logs.batch"}), json!({"t":"batch","items":[{"t":"evt","topic":"traffic.tick"},{"t":"evt","topic":"service.state"}]}),
            json!({"t":"evt","topic":"alerts.desktop"}), json!({"t":"batch","items":[{"t":"evt","topic":"alerts.desktop"},{"t":"evt","topic":"logs.batch"}]})] {
            assert!(!is_progress(&frame), "{frame}");
        }
        for frame in [json!({"t":"evt","topic":"jobs"}), json!({"t":"evt","topic":"shutdown"}), json!({"t":"ack"}), json!({"t":"res"}),
            json!({"t":"bye"}), json!({"t":"fatal"}), json!({"t":"evt","topic":"core.info"}),
            json!({"t":"batch","items":[{"t":"evt","topic":"traffic.tick"},{"t":"evt","topic":"jobs"}]})] {
            assert!(is_progress(&frame), "{frame}");
        }
    }
    /// Lifecycle wiring: actions reach the port in order and outside the machine lock.
    #[test] fn lifecycle_runs_effects_outside_the_lock() {
        struct Port(Mutex<Vec<Action>>, std::sync::atomic::AtomicBool);
        struct Shared(Arc<Port>);
        impl Effects for Shared {
            fn page_connected(&self) -> bool { false }
            fn perform(&self, lifecycle: &Arc<Lifecycle>, action: Action) {
                // Re-entering the machine from an effect must not deadlock.
                self.0.1.store(lifecycle.machine.try_lock().is_ok(), std::sync::atomic::Ordering::SeqCst);
                self.0.0.lock().unwrap().push(action);
            }
        }
        let port = Arc::new(Port(Mutex::new(Vec::new()), Default::default()));
        let lc = Lifecycle::new(Box::new(Shared(port.clone())));
        lc.set_sidecar(Sidecar::Live); lc.request(QuitReason::Tray);
        assert!(port.1.load(std::sync::atomic::Ordering::SeqCst));
        assert_eq!(lc.take_send(), Some(QuitReason::Tray));
        assert!(port.0.lock().unwrap().contains(&Action::MarkQuitting));
    }
}
