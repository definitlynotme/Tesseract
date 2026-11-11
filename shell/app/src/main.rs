#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
mod transport;
mod scheduler;
mod acceptance;
mod window;
mod single_instance;
mod native_text;
mod tray;
mod shell_log;
mod lifecycle;
mod endsession;
mod crash;
mod dialog;
mod notify;
mod notice;
mod presence;
mod host;
mod hostwin;
mod update;
mod webview;
mod hgate;
#[cfg(test)] mod assets;
use bridge_host::{Event, PrivatePipe};
use serde_json::{json, Value};
use std::{collections::BTreeMap, env, ffi::OsString, fs, io, path::PathBuf,
    sync::{atomic::{AtomicBool, Ordering}, mpsc::{self, SyncSender}, Arc, Mutex}, time::{Duration, Instant, SystemTime, UNIX_EPOCH}};
use tauri::{ipc::Channel, Manager, State};
use std::os::windows::process::CommandExt;
use transport::{Delivery, Hub, TOPICS};
use scheduler::SendQueue;
use lifecycle::{Action, Exited, Lifecycle, Sidecar};
pub use lifecycle::QuitReason;

/// Only internal fixed codes and numeric exit status; never raw frames/errors (shell_log.rs).
pub fn log_code(data: &std::path::Path, code: &str) { shell_log::code(data, code); }
fn now() -> u64 { SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_millis() as u64 }
fn stamp(mut v:Value,epoch:&str) -> Value {
    v["epoch"]=json!(epoch); v["produced_at_ms"]=json!(now()); v["sent_at_ms"]=json!(now()); v
}
enum Control { Frame { epoch:String, frame:Value } }
fn current_frame(control:Control, epoch:&str) -> Option<Value> {
    match control { Control::Frame{epoch:registered,frame} if registered==epoch=>Some(frame),_=>None }
}
struct Bridge { hub:Arc<Mutex<Hub>>, tx:SyncSender<Control>, control_tx:SyncSender<Control>, shared:Shared }

/// The sidecar process handle of the live session (SYNCHRONIZE | TERMINATE | QUERY), so the
/// user's "force quit" / "force restart" can terminate exactly that process.
#[derive(Default)]
pub struct ChildSlot(Mutex<Option<isize>>);
impl ChildSlot {
    fn lock(&self) -> std::sync::MutexGuard<'_, Option<isize>> { self.0.lock().unwrap_or_else(|e| e.into_inner()) }
    fn set(&self, handle: Option<isize>) { *self.lock() = handle; }
    fn take(&self) -> Option<isize> { self.lock().take() }
    pub fn terminate(&self, code: u32) -> bool {
        use windows::Win32::{Foundation::HANDLE, System::Threading::TerminateProcess};
        self.lock().is_some_and(|h| unsafe { TerminateProcess(HANDLE(h as _), code) }.is_ok())
    }
}
/// T-P3-06 save grants: the live sidecar's handle also carries PROCESS_DUP_HANDLE, so an approved
/// target file is duplicated into exactly that process (PROTOCOL §13 item 2).
impl host::SidecarPort for ChildSlot {
    fn pid(&self) -> Option<u32> {
        self.lock().map(|h| unsafe { windows::Win32::System::Threading::GetProcessId(windows::Win32::Foundation::HANDLE(h as _)) }).filter(|p| *p != 0)
    }
    fn duplicate(&self, raw: isize, pid: u32) -> io::Result<u64> {
        use windows::Win32::{Foundation::{DuplicateHandle, DUPLICATE_SAME_ACCESS, HANDLE}, System::Threading::{GetCurrentProcess, GetProcessId}};
        let guard = self.lock();
        let Some(process) = *guard else { return Err(io::Error::other("sidecar_gone")) };
        if unsafe { GetProcessId(HANDLE(process as _)) } != pid { return Err(io::Error::other("sidecar_changed")); }
        let mut target = HANDLE::default();
        unsafe { DuplicateHandle(GetCurrentProcess(), HANDLE(raw as _), HANDLE(process as _), &mut target, 0, false, DUPLICATE_SAME_ACCESS) }.map_err(io::Error::other)?;
        Ok(target.0 as usize as u64)
    }
    fn close_remote(&self, value: u64, pid: u32) {
        use windows::Win32::{Foundation::{DuplicateHandle, DUPLICATE_CLOSE_SOURCE, HANDLE}, System::Threading::GetProcessId};
        let guard = self.lock();
        let Some(process) = *guard else { return };
        // Only in the very process the value was duplicated into (the slot holds its handle, so
        // the pid cannot have been reused while it is still the slot's process).
        if unsafe { GetProcessId(HANDLE(process as _)) } != pid { return; }
        unsafe { let _ = DuplicateHandle(HANDLE(process as _), HANDLE(value as usize as _), HANDLE::default(), std::ptr::null_mut(), 0, false, DUPLICATE_CLOSE_SOURCE); }
    }
}
/// T-P3-06 `--autostart` (D8): the `ui.start_in_tray_on_autostart` value read once after hello.
#[derive(Default)]
pub struct Autostart { decided: Mutex<Option<bool>>, ready: std::sync::Condvar }
impl Autostart {
    /// The first answer wins (a relaunched sidecar never changes how this start opened).
    pub fn decide(&self, tray: bool) {
        let mut decided = self.decided.lock().unwrap_or_else(|e| e.into_inner());
        if decided.is_none() { *decided = Some(tray); self.ready.notify_all(); }
    }
    pub fn decided(&self) -> bool { self.decided.lock().unwrap_or_else(|e| e.into_inner()).is_some() }
    pub fn wait(&self, timeout: Duration) -> Option<bool> {
        let guard = self.decided.lock().unwrap_or_else(|e| e.into_inner());
        *self.ready.wait_timeout_while(guard, timeout, |d| d.is_none()).unwrap_or_else(|e| e.into_inner()).0
    }
}
/// `config.snapshot` reply → `values.ui.start_in_tray_on_autostart`; anything else is the default false.
pub fn autostart_tray(reply: &Value) -> bool {
    reply["t"] == "res" && reply["ok"] == true && reply["data"]["values"]["ui"]["start_in_tray_on_autostart"] == true
}
/// Hidden autostart only when the setting says so AND the tray icon is there to restore from.
pub fn autostart_hidden(tray_setting: Option<bool>, tray_available: bool) -> bool { tray_setting == Some(true) && tray_available }
/// What the bridge worker shares with the UI thread, Tauri commands, dialogs and the window hook.
#[derive(Clone)]
pub struct Shared {
    pub lifecycle: Arc<Lifecycle>,
    pub session_end: Arc<endsession::Tracker>,
    pub child: Arc<ChildSlot>,
    /// Page asked for "强制重启" while the unresponsive banner is up.
    pub force_restart: Arc<AtomicBool>,
    pub unresponsive: Arc<AtomicBool>,
    /// The worker thread has ended (no sidecar will be launched again).
    pub done: Arc<AtomicBool>,
    /// T-P3-05 notices (routing, receipts), the page's routed page and the window probe.
    pub desk: Arc<notify::Desk>,
    /// T-P3-06 page host commands and save grants.
    pub host: Arc<host::Host>,
    /// T-P3-06 update hand-off.
    pub update: Arc<update::Update>,
    /// T-P3-06 `--autostart` decision.
    pub autostart: Arc<Autostart>,
    /// 10-30 notice card window (notice.rs); the desk's `Cards` in production (`run`).
    pub notice: Arc<notice::Host>,
}
impl Shared {
    #[cfg(test)]
    fn new(lifecycle: Arc<Lifecycle>, child: Arc<ChildSlot>, desk: Arc<notify::Desk>) -> Self {
        Self::with(lifecycle, child, desk, host::Host::new(Box::new(hostwin::Win32Desktop)), update::Update::new(Box::new(update::Win32Platform)))
    }
    /// The notice host starts unattached (every card falls back); `run` puts in the one the desk shows cards on.
    fn with(lifecycle: Arc<Lifecycle>, child: Arc<ChildSlot>, desk: Arc<notify::Desk>, host: Arc<host::Host>, update: Arc<update::Update>) -> Self {
        Self { lifecycle, session_end: Arc::new(endsession::Tracker::default()), child,
            force_restart: Arc::new(AtomicBool::new(false)), unresponsive: Arc::new(AtomicBool::new(false)), done: Arc::new(AtomicBool::new(false)), desk,
            host, update, autostart: Arc::new(Autostart::default()), notice: notice::Host::new() }
    }
}
/// 10-30: each window calls only its own invokes (Tauri has no app ACL manifest here, so the
/// capability files cannot say it): the main page the `HOST_INVOKES`, the notice card window the
/// `notice::INVOKES`. A page that ends up in the wrong window (Tauri serves index.html for a
/// missing asset) can neither connect the hub nor confirm notices.
fn caller(webview: &tauri::Webview, label: &str) -> Result<(), String> {
    if webview.label() == label { Ok(()) } else { Err("not_allowed".into()) }
}
/// The exit code the quit state machine decided (review M1). Tauri's `app.exit(code)` only
/// carries `code` into `RunEvent::ExitRequested`; the loop then stops with tao's
/// `ControlFlow::Exit` (= `ExitWithCode(0)`), so `run_return` always returns 0. The shell takes
/// its process exit code from here instead.
#[derive(Default)]
pub struct ExitSlot(Mutex<Option<i32>>);
impl ExitSlot {
    fn lock(&self) -> std::sync::MutexGuard<'_, Option<i32>> { self.0.lock().unwrap_or_else(|e| e.into_inner()) }
    /// The first decision wins: the machine emits one `Exit`, nothing later may override it.
    pub fn decide(&self, code: i32) { let mut slot=self.lock(); if slot.is_none() { *slot=Some(code); } }
    /// The process exit code: the decided one, else whatever the event loop returned.
    pub fn resolve(&self, returned: i32) -> i32 { self.lock().unwrap_or(returned) }
}
/// The one shell quit entry: tray 「退出」, closing without a tray, dev and acceptance hooks.
/// T-P3-04 runs the quit state machine (lifecycle.rs) behind it; callers are unchanged.
#[derive(Clone)]
pub struct QuitPort { lifecycle:Arc<Lifecycle> }
impl QuitPort {
    /// A quit is in progress (asking the page, prompting, or sent): close must not hide to tray.
    pub fn requested(&self)->bool { self.lifecycle.machine().phase!=lifecycle::Phase::Running }
    pub fn request_quit(&self, _app:&tauri::AppHandle, reason:QuitReason) { self.lifecycle.request(reason); }
}

/// T-P3-06 install step on its own thread (residual wait, hash, CreateProcess), then the outcome.
fn run_install(lc:&Arc<Lifecycle>, update:&Arc<update::Update>) {
    let (lc,update)=(lc.clone(),update.clone());
    std::thread::spawn(move || { let launched=update.install().is_ok(); lc.install_finished(launched); });
}
/// The production effects of the quit state machine.
struct ShellEffects {
    data:PathBuf, hub:Arc<Mutex<Hub>>, app:Arc<Mutex<Option<tauri::AppHandle>>>, pending_exit:Arc<Mutex<Option<i32>>>, exit_code:Arc<ExitSlot>,
    quitting:Arc<single_instance::Quitting>, feed:Arc<tray::Feed>, child:Arc<ChildSlot>, dismiss:Arc<AtomicBool>,
    update:Arc<update::Update>,
}
impl lifecycle::Effects for ShellEffects {
    fn page_connected(&self)->bool { self.hub.lock().unwrap_or_else(|e|e.into_inner()).page_connected() }
    fn perform(&self, lc:&Arc<Lifecycle>, action:Action) {
        match action {
            Action::Log(code)=>log_code(&self.data,&code),
            Action::MarkQuitting=>if let Err(e)=self.quitting.mark_quitting() { log_code(&self.data,&format!("quit_mark_failed_{e}")); },
            Action::ClearQuitting=>if let Err(e)=self.quitting.clear() { log_code(&self.data,&format!("quit_clear_failed_{e}")); },
            Action::AskPage(token)=>self.hub.lock().unwrap_or_else(|e|e.into_inner()).host(json!({"t":"host:confirmQuit","token":token})),
            Action::PromptUnsaved(count)=>{
                let (lc,locale)=(lc.clone(),self.feed.status().locale);
                std::thread::spawn(move || { let proceed=dialog::unsaved(locale,count); lc.unsaved_answer(proceed); });
            },
            Action::PromptStall=>{
                self.dismiss.store(false,Ordering::Release);
                let (lc,locale,data,dismiss)=(lc.clone(),self.feed.status().locale,self.data.clone(),self.dismiss.clone());
                std::thread::spawn(move || { let force=dialog::stall(locale,&data,dismiss); lc.stall_answer(force); });
            },
            Action::DismissStall=>self.dismiss.store(true,Ordering::Release),
            Action::Terminate=>log_code(&self.data,if self.child.terminate(lifecycle::FORCED_EXIT as u32) {"sidecar_force_killed"} else {"sidecar_force_kill_failed"}),
            Action::Install=>run_install(lc,&self.update),
            Action::RestoreWindow=>{
                let restored=self.app.lock().unwrap_or_else(|e|e.into_inner()).as_ref().and_then(|app|app.get_webview_window("main"))
                    .is_some_and(|w| { let shown=w.show().is_ok(); let _=w.unminimize(); let _=w.set_focus(); shown });
                log_code(&self.data,if restored {"update_window_restored"} else {"update_window_restore_failed"});
            },
            Action::Exit(code)=>{
                // Recorded before app.exit: run() exits the process with this code (M1).
                self.exit_code.decide(code);
                let (app,pending)=(self.app.clone(),self.pending_exit.clone());
                // Give the WebView IPC callback queue a bounded chance to deliver bye first.
                std::thread::spawn(move || {
                    std::thread::sleep(Duration::from_millis(250));
                    let guard=app.lock().unwrap_or_else(|e|e.into_inner());
                    match guard.as_ref() { Some(app)=>app.exit(code), None=>*pending.lock().unwrap_or_else(|e|e.into_inner())=Some(code) }
                });
            },
        }
    }
}

#[tauri::command(rename_all = "snake_case")]
fn rt(webview:tauri::Webview, state:State<Bridge>, req_id:String, cmd:String, args:Value, owner:Option<String>) -> Result<Value,String> {
    caller(&webview,"main")?;
    if !state.shared.lifecycle.accepting() { return Err("runtime_unavailable".into()); }
    // T-P3-06: host.* runs in the shell only (never forwarded); the res follows on the Channel.
    if host::is_host(&cmd) {
        let id=next_id(&state.hub);
        state.shared.host.submit(host::Request{id,req_id,owner:owner.unwrap_or_default(),cmd,args})?;
        return Ok(json!({"queued":true,"id":id}));
    }
    let mut hub=state.hub.lock().map_err(|_| "internal")?;
    let frame=hub.request(&req_id,&cmd,args,owner)?;
    let id=frame["id"].as_u64().unwrap();
    // T-P2-31b: every control-domain page command (PROTOCOL §10) takes the control lane.
    let tx=if transport::control_command(&cmd) {&state.control_tx}else{&state.tx};
    if tx.try_send(Control::Frame{epoch:hub.epoch.clone(),frame}).is_err() {
        hub.reject_queued(id,&req_id); return Err("job_capacity".into());
    }
    Ok(json!({"queued":true,"id":id}))
}
#[tauri::command]
fn rt_connect(webview:tauri::Webview, state:State<Bridge>, channel:Channel<Delivery>) -> Result<(),String> {
    caller(&webview,"main")?;
    state.hub.lock().map_err(|_| "internal")?.connect(channel); Ok(())
}
/// T-P4-C3: the page asks for one topic's complete snapshot again (`Hub::resync`). The cached
/// snapshot goes out on the Channel at once; a replacement the shell must request goes out with
/// the worker's next `take_requests`. Answers `{source: cache|pending|request}` or an error code.
#[tauri::command]
fn rt_resync(webview:tauri::Webview, state:State<Bridge>, topic:String, key:Option<String>) -> Result<Value,String> {
    caller(&webview,"main")?;
    if !state.shared.lifecycle.accepting() { return Err("runtime_unavailable".into()); }
    state.hub.lock().map_err(|_| "internal")?.resync(&topic,key.as_deref(),Instant::now()).map_err(String::from)
}
/// Page answer to `host:confirmQuit{token}`: how many unsaved changes it holds.
#[tauri::command(rename_all = "snake_case")]
fn host_confirm_quit(webview:tauri::Webview, state:State<Bridge>, token:u64, unsaved:u32) -> Result<(),String> {
    caller(&webview,"main")?;
    state.shared.lifecycle.page_reply(token,unsaved).map_err(String::from)
}
/// 「强制重启」 on the `sidecar_unresponsive` banner: TerminateProcess, then the crash path.
#[tauri::command]
fn host_force_restart(webview:tauri::Webview, state:State<Bridge>) -> Result<(),String> {
    caller(&webview,"main")?;
    if !state.shared.unresponsive.load(Ordering::Acquire) { return Err("not_unresponsive".into()); }
    state.shared.force_restart.store(true,Ordering::Release); Ok(())
}
/// Page answer to `host:notify{delivery_id}`: the notice is on screen (PROTOCOL §13 in-app
/// success). Accepted only for a notice this shell handed to the same page connection within
/// 3 s; the shell, not the page, then sends `alerts.desktopDelivered`. 10-30: `shown: false` when
/// the page confirms a notice it did not show because the overview already shows the incident
/// (receipted the same, no sound).
#[tauri::command(rename_all = "snake_case")]
fn host_notify_rendered(webview:tauri::Webview, state:State<Bridge>, delivery_id:String, shown:Option<bool>) -> Result<(),String> {
    caller(&webview,"main")?;
    let page=state.hub.lock().map_err(|_| "internal")?.page_generation();
    state.shared.desk.rendered_as(&delivery_id,page,shown.unwrap_or(true)).map_err(String::from)
}
/// The page's routed page (`ui.presence.page` whitelist), for this page connection only (a
/// reloaded page reports "none" until it names its page again). Visible/focused are never the page's.
#[tauri::command]
fn host_set_page(webview:tauri::Webview, state:State<Bridge>, page:String) -> Result<(),String> {
    caller(&webview,"main")?;
    let generation=state.hub.lock().map_err(|_| "internal")?.page_generation();
    state.shared.desk.page.set(&page,generation).map(|_|()).map_err(String::from)
}
/// The page declares that this connection renders `host:notify` and confirms it with
/// `host_notify_rendered`. Until then (and after every reload until it declares again) foreground
/// notices use the card window instead of being dropped by a page that cannot show them.
#[tauri::command]
fn host_notify_ready(webview:tauri::Webview, state:State<Bridge>) -> Result<(),String> {
    caller(&webview,"main")?;
    let generation=state.hub.lock().map_err(|_| "internal")?.page_generation();
    if generation==0 { return Err("page_not_connected".into()); }
    state.shared.desk.notify_ready(generation); Ok(())
}
/// 11-05 in-page title bar: the page's window buttons (the window has no system caption).
/// `close` is the system close: `CloseRequested` below decides tray or quit, as for Alt+F4.
#[tauri::command]
fn host_window(webview:tauri::Webview, action:String) -> Result<(),String> {
    caller(&webview,"main")?;
    let window=webview.window();
    match window::Caption::parse(&action).ok_or("invalid_args")? {
        window::Caption::Minimize=>window.minimize(),
        window::Caption::ToggleMaximize=>if window.is_maximized().unwrap_or(false) { window.unmaximize() } else { window.maximize() },
        window::Caption::Close=>window.close(),
    }.map_err(|_| "internal".into())
}
#[cfg(debug_assertions)]
#[tauri::command]
fn dev_quit(webview:tauri::Webview, state:State<Bridge>) { if caller(&webview,"main").is_ok() { state.shared.lifecycle.request(QuitReason::Dev); } }
/// T-P3-07 §2.2.1 step 6: the page has updated its DOM from real state; the shell uncloaks the
/// main window (unless the 1.5 s timeout already did). Later calls (a reloaded page) change nothing.
#[tauri::command]
fn host_ready(webview:tauri::Webview, supervisor:State<Arc<webview::Supervisor>>) -> Result<(),String> {
    caller(&webview,"main")?;
    if supervisor.gate.ready() { log_code(supervisor.data(),"host_ready"); }
    Ok(())
}
/// 10-30 notice card window (notice.rs): the page listens on `channel`; the waiting cards go out.
#[tauri::command]
fn notice_ready(webview:tauri::Webview, state:State<Bridge>, channel:Channel<Value>) -> Result<(),String> {
    caller(&webview,notice::LABEL)?;
    state.shared.notice.ready(channel).map_err(String::from)
}
/// The card is in the page; with the window on screen that is `delivered/windows`.
#[tauri::command(rename_all = "snake_case")]
fn notice_rendered(webview:tauri::Webview, state:State<Bridge>, delivery_id:String) -> Result<(),String> {
    caller(&webview,notice::LABEL)?;
    state.shared.notice.rendered(&delivery_id).map_err(String::from)
}
/// The card body or its 「查看」: restore the main window and go to the card's target.
#[tauri::command(rename_all = "snake_case")]
fn notice_open(webview:tauri::Webview, state:State<Bridge>, delivery_id:String) -> Result<(),String> {
    caller(&webview,notice::LABEL)?;
    state.shared.notice.open(&delivery_id).map_err(String::from)
}
/// The user closed a card (×).
#[tauri::command(rename_all = "snake_case")]
fn notice_dismiss(webview:tauri::Webview, state:State<Bridge>, delivery_id:String) -> Result<(),String> {
    caller(&webview,notice::LABEL)?;
    state.shared.notice.dismiss(&delivery_id).map_err(String::from)
}
/// The card stack's height in CSS px (0: empty, the window hides).
#[tauri::command]
fn notice_resize(webview:tauri::Webview, state:State<Bridge>, height:f64) -> Result<(),String> {
    caller(&webview,notice::LABEL)?;
    state.shared.notice.resize(height).map_err(String::from)
}
/// What a main window is built from; kept for a T-P3-07 recreation after a browser-process loss.
#[derive(Clone)]
struct WindowSpec { scope:String, data:PathBuf, dev_page:bool, browser_args:String }
impl WindowSpec {
    /// §7.9 / §2.2.6: the first creation at once, then up to 3 retries with backoff (the user data
    /// folder may still be held by a browser process that is going away). Ok((window, attempt)).
    /// `phase` "recreate" runs off the UI thread (recreation thread); "startup" runs in `setup`.
    fn create(&self, app:&tauri::AppHandle, show:bool, supervisor:&Arc<webview::Supervisor>, phase:&str) -> tauri::Result<(tauri::WebviewWindow,u32)> {
        // Debug-only smoke hooks for recreation attempts (scripts/p3_shell_smoke.ps1):
        //   "1"           every attempt fails before a window is built (scenario "lost");
        //   "after_build" every attempt builds the real window, then fails, so the destroy and
        //                 label wait below run before each retry (scenario "lostbuilt", B-S2).
        let fail=if cfg!(debug_assertions) && phase=="recreate" { env::var("TESSERACT_SHELL_DEV_RECREATE_FAIL").ok() } else { None };
        webview::with_retries(|attempt| {
            let created=match fail.as_deref() {
                Some("1")=>Err(tauri::Error::AssetNotFound("dev_recreate_fail".into())),
                Some("after_build")=>window::create(app,&self.scope,&self.data,self.dev_page,self.browser_args.clone(),show,supervisor.clone()).and_then(|_built| {
                    shell_log::fields(&self.data,"dev_recreate_fail_after_build",&[("attempt",&attempt.to_string())]);
                    Err(tauri::Error::AssetNotFound("dev_recreate_fail_after_build".into()))
                }),
                _=>window::create(app,&self.scope,&self.data,self.dev_page,self.browser_args.clone(),show,supervisor.clone()),
            };
            if created.is_err() {
                shell_log::fields(&self.data,"webview_create_failed",&[("phase",phase),("attempt",&attempt.to_string())]);
                // A window that was built before its WebView setup failed would block the label.
                if let Some(left)=app.get_webview_window("main") {
                    let _=left.destroy();
                    // B-S2: Tauri frees the label only when its event loop handles Destroyed. Off the
                    // UI thread (recreation) wait for that before the retry; in `setup` (startup) the
                    // loop is not running yet, so waiting would only stall it — a startup failure
                    // after build() therefore cannot be retried under the same label (report).
                    if phase=="recreate" && !wait_main_label_gone(app,Duration::from_secs(5)) { log_code(&self.data,"webview_label_still_registered"); }
                }
            }
            created
        },std::thread::sleep).map_err(|(error,_)|error)
    }
}
/// Off the UI thread: until Tauri has unregistered the destroyed main window's label (or `limit`).
fn wait_main_label_gone(app:&tauri::AppHandle, limit:Duration) -> bool {
    let deadline=Instant::now()+limit;
    while app.get_webview_window("main").is_some() {
        if Instant::now()>=deadline { return false; }
        std::thread::sleep(Duration::from_millis(20));
    }
    true
}
/// The per-window hooks of the main window (presence probe, host owner window).
fn attach_window(app:&tauri::AppHandle, probe:&presence::Win32Probe, hwnd:windows::Win32::Foundation::HWND) {
    probe.set(hwnd.0 as isize);
    app.state::<Bridge>().shared.host.set_window(hwnd.0 as isize);
}
/// T-P3-07 §2.2.6: the WebView2 browser process is gone. Destroy the dead main window and build a
/// new one (at once, then 3 retries with backoff); the new page reconnects and rebuilds from the
/// shell cache. A window that was in the tray comes back in the tray. If every attempt fails: the
/// native box with 「退出」, then the normal quit (network restore first, T-P3-04).
/// Fix round 1: `mode` Limited (A-S2) skips the rebuild and goes to the exhausted path at once;
/// while the shell is leaving (A-S1) nothing happens at all (false = not started).
fn recreate_main_window(app:tauri::AppHandle, supervisor:Arc<webview::Supervisor>, spec:WindowSpec, probe:Arc<presence::Win32Probe>,
    lifecycle:Arc<Lifecycle>, feed:Arc<tray::Feed>, session_end:Arc<endsession::Tracker>, mode:webview::Recreate) -> bool {
    let leaving=|| webview::skip_recreate(lifecycle.accepting(),lifecycle.machine().reason()==Some(QuitReason::Update),session_end.active());
    if leaving() { log_code(&spec.data,"webview_recreate_skipped_quitting"); return false; }
    std::thread::spawn(move || {
        let data=spec.data.clone();
        let leaving=|| webview::skip_recreate(lifecycle.accepting(),lifecycle.machine().reason()==Some(QuitReason::Update),session_end.active());
        let old=app.get_webview_window("main");
        let show=old.as_ref().is_some_and(|w| w.is_visible().unwrap_or(false));
        shell_log::fields(&data,"webview_recreate_start",&[("show",if show {"true"} else {"false"}),
            ("mode",if mode==webview::Recreate::Limited {"limited"} else {"attempt"})]);
        // B-S5: from here until the new window is attached there is no main window to report on:
        // presence says nothing (as before the first window existed) and host dialogs get no owner.
        probe.set(0);
        app.state::<Bridge>().shared.host.set_window(0);
        if let Some(old)=old { if old.destroy().is_err() { log_code(&data,"webview_destroy_failed"); } }
        if !wait_main_label_gone(&app,Duration::from_secs(5)) { log_code(&data,"webview_label_still_registered"); }
        let created=match mode {
            webview::Recreate::Attempt=>spec.create(&app,show,&supervisor,"recreate"),
            webview::Recreate::Limited=>Err(tauri::Error::AssetNotFound("webview_recreate_limited".into())),
        };
        match created {
            Ok((window,attempt))=>{
                match window.hwnd() {
                    Ok(hwnd)=>{
                        let (raw,hwnd)=(hwnd.0 as isize,windows::Win32::Foundation::HWND(hwnd.0));
                        // SetWindowSubclass must run on the window's own (UI) thread.
                        if app.run_on_main_thread(move || endsession::reattach(windows::Win32::Foundation::HWND(raw as _))).is_err() { log_code(&data,"session_end_hook_failed"); }
                        attach_window(&app,&probe,hwnd);
                    },
                    Err(_)=>log_code(&data,"webview_recreate_hwnd_failed"),
                }
                supervisor.recovered();
                shell_log::fields(&data,"webview_recreated",&[("attempt",&attempt.to_string())]);
            },
            Err(_)=>{
                // Still "recovering": with no window left, Tauri must keep the loop until the quit ends it.
                log_code(&data,"webview_recreate_exhausted");
                // A-S1: a quit or session end began meanwhile; it ends the shell, no box on top.
                if leaving() { log_code(&data,"webview_recreate_skipped_quitting"); return; }
                // A-S8: the session-end hook left with the destroyed window. The tray's hidden
                // top-level window still hears WM_QUERYENDSESSION while the box waits for the user.
                match tray::installed().map(|t| t.window()) {
                    Some(raw)=>if app.run_on_main_thread(move || endsession::reattach(windows::Win32::Foundation::HWND(raw as _))).is_ok() {
                        log_code(&data,"session_end_hook_on_tray");
                    } else { log_code(&data,"session_end_hook_failed"); },
                    None=>log_code(&data,"session_end_hook_no_window"),
                }
                dialog::webview_lost(feed.status().locale);
                lifecycle.request(QuitReason::WebViewLost);
            },
        }
    });
    true
}

/// Worker clocks; tests shorten them.
#[derive(Clone,Copy)]
struct Timing { pong_timeout:Duration, restart_delay:Duration }
impl Default for Timing { fn default()->Self { Self{pong_timeout:crash::PONG_TIMEOUT,restart_delay:crash::RESTART_DELAY} } }
struct Options { config:PathBuf, data:PathBuf, exe:PathBuf, child_args:Vec<String>, dev_page:bool, browser_args:String, verify_port:Option<u16>, timing:Timing,
    /// T-P3-06: started by Windows autostart (`--autostart`, D8).
    autostart:bool,
    /// T-P3-06: directories whose processes must be gone before the installer starts.
    install_roots:Vec<PathBuf>,
    /// T-P6-06: the scope this shell runs in (`resolve_scope`); hello must report exactly this.
    scope_kind:&'static str, scope_id:String }
/// T-P6-06: the distribution build (`--features distribution`, only scripts/build_shell_preview.py
/// and later the release script). It admits the default scope and always runs the frozen sidecar
/// next to the shell. Development builds (shell/target, every test, e2e and p3 smoke) do not.
const DISTRIBUTION:bool=cfg!(feature="distribution");
/// The release version contract (`app_version.txt`), printed by a distribution build's `--version`.
const APP_VERSION:&str=include_str!("../../../app_version.txt");
/// T-P6-06: the scope decision (kind, id, directories). See `resolve_scope`.
#[derive(Clone,Debug,PartialEq)]
struct Scope { kind:&'static str, id:String, config:PathBuf, data:PathBuf }
/// `fs::canonicalize` without the verbatim prefix (`\\?\C:\…` → `C:\…`, `\\?\UNC\h\s` → `\\h\s`).
fn plain_path(path:PathBuf) -> PathBuf {
    let text=path.to_string_lossy();
    if let Some(rest)=text.strip_prefix(r"\\?\UNC\") { PathBuf::from(format!(r"\\{rest}")) }
    else if let Some(rest)=text.strip_prefix(r"\\?\") { PathBuf::from(rest) } else { path }
}
/// Python's `Path.resolve()` (strict=False) for the default directories: the deepest existing
/// ancestor resolved, the missing rest appended. Reads the file system, never writes it.
fn resolve_lenient(path:&std::path::Path) -> PathBuf {
    if let Ok(found)=fs::canonicalize(path) { return plain_path(found); }
    match (path.parent(),path.file_name()) {
        (Some(parent),Some(name)) if !parent.as_os_str().is_empty() => resolve_lenient(parent).join(name),
        _ => path.to_path_buf(),
    }
}
/// `app_paths.default_config_dir` / `default_data_dir` on Windows: `%APPDATA%\Tesseract` and
/// `%LOCALAPPDATA%\Tesseract`; an unset or empty variable falls back to `Path.home()`'s
/// `AppData\Roaming` / `AppData\Local` (`USERPROFILE`, else `HOMEDRIVE` + `HOMEPATH`). A base that is
/// not absolute (Python would resolve it against the working directory) is refused instead.
fn default_directories(env:&dyn Fn(&str)->Option<OsString>) -> io::Result<(PathBuf,PathBuf)> {
    let invalid=|s| io::Error::other(s);
    let home=|| -> io::Result<PathBuf> {
        if let Some(profile)=env("USERPROFILE") { return Ok(PathBuf::from(profile)); }
        let path=env("HOMEPATH").ok_or_else(||invalid("user_profile_missing"))?;
        let mut drive=env("HOMEDRIVE").unwrap_or_default(); drive.push(path); Ok(PathBuf::from(drive))
    };
    let base=|var:&str,fallback:&str| -> io::Result<PathBuf> {
        let root=match env(var).filter(|v|!v.is_empty()) { Some(v)=>PathBuf::from(v), None=>home()?.join("AppData").join(fallback) };
        if !root.is_absolute() { return Err(invalid("default_directory_invalid")); }
        Ok(resolve_lenient(&root.join("Tesseract")))
    };
    Ok((base("APPDATA","Roaming")?,base("LOCALAPPDATA","Local")?))
}
/// T-P6-06 scope decision. `env` is the process environment (tests inject one); `distribution`
/// is `DISTRIBUTION`. Reads the file system (canonicalize) but creates nothing: `run` creates the
/// default directories after `--print-scope` has been handled.
/// - `TESSERACT_ISOLATED_AUDIT_MANIFEST` set: refused, as before (`explicit_custom_scope_required`);
/// - `TESSERACT_INSTANCE_SCOPE` set: the custom path, unchanged (`live_scope_forbidden` included);
/// - only `TESSERACT_CONFIG_DIR` / `TESSERACT_DATA_DIR` set: refused (`scope_label_missing`), never
///   the legacy names with non-default directories;
/// - none of the three: the default scope in a distribution build, refused in a development build.
fn resolve_scope(env:&dyn Fn(&str)->Option<OsString>, distribution:bool) -> io::Result<Scope> {
    let invalid=|s| io::Error::other(s);
    if env("TESSERACT_ISOLATED_AUDIT_MANIFEST").is_some() { return Err(invalid("explicit_custom_scope_required")); }
    if let Some(label)=env("TESSERACT_INSTANCE_SCOPE") {
        if label.to_str().unwrap_or_default().trim().is_empty() { return Err(invalid("explicit_custom_scope_required")); }
        let directory=|name| -> io::Result<PathBuf> {
            let raw=PathBuf::from(env(name).ok_or_else(||invalid("scope_directory_missing"))?);
            if !raw.is_absolute() { return Err(invalid("absolute_scope_directory_required")); }
            let resolved=fs::canonicalize(raw)?;
            if !resolved.is_dir() { return Err(invalid("scope_directory_invalid")); }
            // Refuse live production roots even when wrapped in an explicit label.
            for var in ["APPDATA","LOCALAPPDATA"] {
                if let Some(root)=env(var) {
                    let protected=PathBuf::from(root).join("Tesseract");
                    let protected=fs::canonicalize(&protected).unwrap_or(protected);
                    if resolved.to_string_lossy().to_lowercase().starts_with(&protected.to_string_lossy().to_lowercase()) { return Err(invalid("live_scope_forbidden")); }
                }
            }
            Ok(resolved)
        };
        let config=directory("TESSERACT_CONFIG_DIR")?; let data=directory("TESSERACT_DATA_DIR")?;
        let id=bridge_host::scope::custom_id(&config.to_string_lossy(),&data.to_string_lossy())?;
        return Ok(Scope{kind:"custom",id,config,data});
    }
    if env("TESSERACT_CONFIG_DIR").is_some() || env("TESSERACT_DATA_DIR").is_some() { return Err(invalid("scope_label_missing")); }
    if !distribution { return Err(invalid("explicit_custom_scope_required")); }
    let (config,data)=default_directories(env)?;
    Ok(Scope{kind:"default",id:"default".into(),config,data})
}
/// T-P6-06: where the sidecar comes from.
#[derive(Clone,Debug,PartialEq)]
enum SidecarSource {
    /// A frozen `tesseract-runtime.exe` (`--sidecar PATH`, or the one next to a distribution shell).
    Frozen(PathBuf),
    /// Development only: the repository `.venv` runs `runtime_main.pyw` from source.
    Source,
    /// Development (debug) only: `--dev-sidecar-script` (scripts/p3_notify_smoke.ps1).
    Script(PathBuf),
}
/// T-P6-06 sidecar choice. A distribution build never runs the repository sources: in the default
/// scope it always runs `tesseract-runtime.exe` next to `shell_exe` and refuses `--sidecar`
/// (the live profile is never handed to another program); in a custom scope `--sidecar` may name
/// one, else the one next to the shell. Development builds keep `--sidecar`, the source sidecar
/// and the debug-only `--dev-sidecar-script`.
fn sidecar_source(distribution:bool, debug:bool, kind:&str, args:&[String], shell_exe:&std::path::Path) -> Result<SidecarSource,&'static str> {
    let value=|key:&str| args.windows(2).find(|v|v[0]==key).map(|v|PathBuf::from(&v[1]));
    let script=value("--dev-sidecar-script");
    if script.is_some() && (!debug || distribution) { return Err("dev_sidecar_script_debug_only"); }
    if distribution {
        if kind=="default" && args.iter().any(|a|a=="--sidecar") { return Err("sidecar_override_forbidden"); }
        return Ok(SidecarSource::Frozen(value("--sidecar").unwrap_or_else(||shell_exe.with_file_name("tesseract-runtime.exe"))));
    }
    Ok(match (value("--sidecar"),script) { (Some(exe),_)=>SidecarSource::Frozen(exe), (None,Some(script))=>SidecarSource::Script(script), (None,None)=>SidecarSource::Source })
}
/// T-P6-06 hello check of the scope: kind and id exactly as this shell resolved them.
fn hello_scope_matches(hello:&Value, kind:&str, id:&str) -> bool { hello["scope"]["kind"]==kind && hello["scope"]["id"]==id }
/// T-P6-06 `--print-scope`: the decision as one JSON line (exit 0), or with the refusal code
/// (exit 2). Takes no mutex or event, creates no directory, writes no shell.log, starts nothing.
fn scope_report(env:&dyn Fn(&str)->Option<OsString>, distribution:bool, args:&[String], shell_exe:&std::path::Path) -> (Value,i32) {
    let build=if distribution {"distribution"} else {"development"};
    let scope=match resolve_scope(env,distribution) {
        Ok(scope)=>scope,
        Err(error)=>return (json!({"build":build,"kind":null,"error":error.to_string()}),2),
    };
    let names=match bridge_host::scope::names(scope.kind,&scope.id) { Ok(names)=>names, Err(error)=>return (json!({"build":build,"kind":null,"error":error.to_string()}),2) };
    let webview2=single_instance::elevated().map(|admin|single_instance::profile(&scope.data,&scope.id,admin).to_string_lossy().into_owned()).ok();
    let mut report=json!({"build":build,"kind":scope.kind,"id":scope.id,"config":scope.config.to_string_lossy(),"data":scope.data.to_string_lossy(),
        "names":names,"webview2":webview2,"sidecar":null,"sidecar_error":null,"error":null});
    match sidecar_source(distribution,cfg!(debug_assertions),scope.kind,args,shell_exe) {
        Ok(SidecarSource::Frozen(path))=>report["sidecar"]=json!({"mode":"frozen","path":path.to_string_lossy(),"exists":path.is_file()}),
        Ok(SidecarSource::Source)=>report["sidecar"]=json!({"mode":"source"}),
        Ok(SidecarSource::Script(path))=>report["sidecar"]=json!({"mode":"script","path":path.to_string_lossy()}),
        Err(code)=>{ report["sidecar_error"]=json!(code); return (report,2); },
    }
    (report,0)
}
/// The source checkout (source-mode sidecar, `bin` with Mihomo). A distribution build only runs
/// the frozen sidecar next to itself, so it does not embed the build machine's checkout path.
#[cfg(not(feature="distribution"))]
fn source_repo()->PathBuf { PathBuf::from(env!("CARGO_MANIFEST_DIR")).parent().unwrap().parent().unwrap().to_owned() }
#[cfg(feature="distribution")]
fn source_repo()->PathBuf { PathBuf::new() }
fn options(scope:&Scope) -> io::Result<Options> {
    let invalid=|s| io::Error::other(s);
    let (config,data)=(scope.config.clone(),scope.data.clone());
    let args:Vec<String>=env::args().skip(1).collect();
    let repo=source_repo();
    let shell_exe=env::current_exe()?;
    // T-P3-05: debug-only scripted stand-in sidecar for scripts/p3_notify_smoke.ps1.
    // T-P6-06: a distribution build runs only the frozen sidecar (`sidecar_source`).
    let source=sidecar_source(DISTRIBUTION,cfg!(debug_assertions),scope.kind,&args,&shell_exe).map_err(invalid)?;
    // T-P3-06 residual-process roots (update::install_roots): release builds add the shell's own
    // directory (the install directory) to the `--sidecar` directory, else to the source-mode repo
    // `bin` where Mihomo lives; debug builds may point it at a test directory instead. A root that
    // does not resolve leaves none, and the install then fails closed (residual_roots_unknown).
    let frozen=match &source { SidecarSource::Frozen(exe)=>Some(exe.clone()), _=>None };
    let debug_root=if cfg!(debug_assertions) { env::var_os("TESSERACT_SHELL_DEV_INSTALL_ROOT").map(PathBuf::from) } else { None };
    let source_bin=(!DISTRIBUTION).then(||repo.join("bin"));
    let roots=update::install_roots(&shell_exe,frozen.as_deref(),source_bin.as_deref(),!cfg!(debug_assertions),debug_root);
    let script=match &source { SidecarSource::Script(script)=>Some(script.clone()), _=>None };
    let (exe,child_args)=if let Some(exe)=frozen { (exe,vec![]) }
    else {
        let python=repo.join(".venv/Scripts/python.exe");
        // Venv's Windows redirector starts another PID; launch its base interpreter
        // directly with venv modules on PYTHONPATH to retain bridge PID ownership.
        let out=std::process::Command::new(&python).creation_flags(0x08000000)
            .args(["-B","-c","import sys; print(sys._base_executable)"]).output()?;
        if !out.status.success() { return Err(invalid("venv_interpreter_lookup_failed")); }
        let exe=PathBuf::from(String::from_utf8(out.stdout).map_err(|_|invalid("invalid_interpreter_path"))?.trim());
        let mut paths=vec![repo.clone(),repo.join(".venv/Lib/site-packages")];
        // Do not inherit an arbitrary PYTHONPATH into the owned sidecar (the debug stand-in too).
        env::set_var("PYTHONPATH",env::join_paths(paths.drain(..)).map_err(|_|invalid("pythonpath_invalid"))?);
        env::set_var("PYTHONDONTWRITEBYTECODE","1");
        if let Some(script)=script {
            if !script.is_absolute() || !script.is_file() { return Err(invalid("dev_sidecar_script_invalid")); }
            let id=bridge_host::scope::custom_id(&config.to_string_lossy(),&data.to_string_lossy())?;
            return finish_options(scope,exe,vec!["-B".into(),script.to_string_lossy().into_owned(),"--scope".into(),id],&args,roots);
        }
        // GUI-subsystem release has no console streams. Give Python harmless
        // NUL streams before runtime imports, just as a frozen windowed launcher does.
        let bootstrap="import os,sys,runpy; sys.stdout=sys.stdout or open(os.devnull,'w'); sys.stderr=sys.stderr or open(os.devnull,'w'); sys.stdin=sys.stdin or open(os.devnull,'r'); sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0],run_name='__main__')";
        (exe,vec!["-B".into(),"-c".into(),bootstrap.into(),repo.join("runtime_main.pyw").to_string_lossy().into_owned()])
    };
    finish_options(scope,exe,child_args,&args,roots)
}
fn finish_options(scope:&Scope,exe:PathBuf,child_args:Vec<String>,args:&[String],install_roots:Vec<PathBuf>) -> io::Result<Options> {
    let invalid=|s| io::Error::other(s);
    let (config,data)=(scope.config.clone(),scope.data.clone());
    let mut child_args=child_args;
    child_args.extend(["--legacy-gate".into(),"inherited".into()]);
    let exe=fs::canonicalize(exe)?;
    let browser_args=browser_arguments(cfg!(debug_assertions),env::var("TESSERACT_SHELL_CDP_PORT").ok())?;
    // The WebView2 environment overrides are dropped at the very start of run() (T-P3-07,
    // webview::scrub_process_environment): release removes all of them, debug keeps the four of §2.2.1.
    let verify_port=if args.iter().any(|a|a=="--verify-webview") {
        if cfg!(debug_assertions) {return Err(invalid("release_acceptance_required"));}
        Some(env::var("TESSERACT_SHELL_VERIFY_PORT").map_err(|_|invalid("verify_port_missing"))?
            .parse::<u16>().ok().filter(|p|*p>=1024).ok_or_else(||invalid("verify_port_invalid"))?)
    } else {None};
    Ok(Options{config,data,exe,child_args,verify_port,dev_page:cfg!(debug_assertions)&&args.iter().any(|a|a=="--dev-rt"),browser_args,timing:Timing::default(),
        autostart:args.iter().any(|a|a=="--autostart"),install_roots,scope_kind:scope.kind,scope_id:scope.id.clone()})
}
fn browser_arguments(debug:bool,port:Option<String>) -> io::Result<String> {
    let mut args=window::DEFAULT_ARGS.to_string();
    if debug {
        if let Some(port)=port {
            let n:u16=port.parse().map_err(|_|io::Error::other("invalid_cdp_port"))?;
            if n<1024 { return Err(io::Error::other("invalid_cdp_port")); }
            args.push_str(&format!(" --remote-debugging-port={n} --remote-debugging-address=127.0.0.1"));
        }
    }
    Ok(args)
}
/// One sidecar frame into the shell cache. The tray only sees facts the hub's cache accepted
/// (`Hub::take_facts`, T-P4-C3 fix round 1), so the tooltip and icon never show a frame the page
/// would reject or an older rev the cache refused. Shell-only topic events (notify.rs) are
/// taken out first and returned once the rest of the frame was accepted; the page never sees them.
fn accept_frame(hub:&Mutex<Hub>, feed:&tray::Feed, mut frame:Value) -> Result<Vec<Value>,&'static str> {
    let (shell,tray_items);
    {
        let mut hub=hub.lock().unwrap();
        // Batch atomicity: a shell item of another epoch rejects the whole frame, as the hub does.
        notify::check_batch(&frame,&hub.epoch)?;
        shell=notify::take_shell_items(&mut frame);
        let accepted=if frame.is_null() { Ok(()) /* one shell-topic evt: notify::Cursor checks it */ }
            else if !shell.is_empty() && frame["items"].as_array().is_some_and(|items|items.is_empty()) { hub.shell_only_batch(&frame) }
            else { hub.receive(frame) };
        tray_items=hub.take_facts();
        accepted?;
    }
    feed.apply(tray_items);
    Ok(shell)
}
/// One shell-topic event of the current session (already split off by `accept_frame`). Returns the
/// data of a valid `update.prepareExit` edge; the worker owns the hand-off (update.rs).
fn shell_item(item:&Value, epoch:&str, cursor:&mut notify::Cursor, desk:&notify::Desk, hub:&Mutex<Hub>, feed:&tray::Feed, data:&std::path::Path) -> Option<Value> {
    let rejected=|code:&str| { shell_log::fields(data,"shell_topic_rejected",&[("topic",item["topic"].as_str().unwrap_or("-")),("code",code)]); None };
    let (topic,skip)=match cursor.validate(item,epoch) { Ok(checked)=>checked, Err(code)=>return rejected(code) };
    if let Some(skip)=skip {
        // Accepted (no rebuild for a shell topic), but an edge was lost without a gap.
        shell_log::fields(data,"shell_topic_seq_skip",&[("topic",topic),("expected",&skip.expected.to_string()),("got",&skip.got.to_string())]);
    }
    if item["kind"]=="gap" {
        // Lost alerts.desktop edges are never receipted and nothing redelivers them: a runtime
        // alert expires unreceipted in the sidecar; an update notice is offered again at the next start.
        let dropped=item["data"]["dropped"].as_u64().map_or("-".into(),|d|d.to_string());
        shell_log::fields(data,"shell_topic_gap",&[("topic",topic),("dropped",&dropped)]);
        return None;
    }
    if topic==notify::PREPARE_EXIT { return Some(item["data"].clone()); }
    if topic==notify::LANGUAGE {
        let Some(frame)=item["data"]["language"].as_str().and_then(notify::language_frame) else { return rejected("invalid_language") };
        // Native text: tray menu/tooltip and every later dialog read the feed's locale.
        feed.apply(vec![item.clone()]);
        // 10-30: the notice cards on screen follow too.
        if let Some(locale)=item["data"]["language"].as_str().and_then(native_text::supported) { desk.language(locale); }
        let lang=frame["language"].as_str().unwrap_or("-").to_owned();
        // The first snapshot repeats hello.lang: the page is told only about a change.
        if hub.lock().unwrap().host_sticky("language",frame) { shell_log::fields(data,"language_applied",&[("lang",&lang),("source","topic")]); }
    } else {
        match notify::Notice::parse(&item["data"]) {
            Ok(notice)=>{ desk.deliver(&notice,hub,Instant::now()); },
            Err(code)=>{ rejected(code); },
        }
    }
    None
}
/// T-P3-06 shell requests awaiting the sidecar's reply (never shown to the page).
enum Pending { Host(mpsc::Sender<Result<Value,&'static str>>), ExitResponse(String), Decline, InstallResult, Config }
/// `config.snapshot` from the shell (PROTOCOL §11: the shell may send page queries itself).
fn config_query(id:u64) -> Value {
    json!({"t":"req","id":id,"cmd":"config.snapshot","args":{},"from":"shell","req_id":format!("shell-config-{id}"),"owner":"shell"})
}
/// After an update quit's sidecar exited (`Exited::Installing`): wait for the install step. True:
/// it failed and the machine left `Installing` without exiting, so the worker relaunches the
/// sidecar in the stopped state. The step may already be over when this is called (an immediate
/// failure such as bye_missing on its own thread): only `finished()` decides, never whether
/// `Installing` was still observed here (review M1).
fn install_relaunch(lifecycle:&Arc<Lifecycle>) -> bool {
    while lifecycle.installing() { lifecycle.tick(); std::thread::sleep(Duration::from_millis(20)); }
    !lifecycle.finished()
}
/// shell.log fields of the sidecar's reply to a shell report (receipt or presence).
fn report_reply_fields(frame:&Value) -> Vec<(&'static str,String)> {
    if frame["t"]=="res" && frame["ok"]==true {
        let flag=|k:&str| frame["data"][k].as_bool().map_or("missing".into(),|b|b.to_string());
        vec![("ok","true".into()),("recorded",flag("recorded")),("duplicate",flag("duplicate"))]
    } else {
        vec![("ok","false".into()),("code",frame["error"]["code"].as_str().unwrap_or("missing").into())]
    }
}
/// Shell report requests (receipts, presence) remembered for their replies per session.
const REPORT_CAPACITY: usize = 256;
/// The `next_id` the hub reserved for shell-originated requests (subscribe is 1).
fn next_id(hub:&Mutex<Hub>) -> u64 { let mut h=hub.lock().unwrap(); let id=h.next_id; h.next_id+=1; id }
/// shell.log fields of `bye`: the exit code and the PROTOCOL recovery enums, nothing else.
fn bye_fields(frame:&Value) -> Vec<(&'static str,String)> {
    let recovery=&frame["network_recovery"];
    let state=|d:&str| recovery[d]["state"].as_str().unwrap_or("missing").to_owned();
    let error=|d:&str| recovery[d]["error_code"].as_str().unwrap_or("-").to_owned();
    vec![("exit_code",frame["exit_code"].as_i64().map_or("missing".into(),|c|c.to_string())),
        ("proxy",state("proxy")),("proxy_error",error("proxy")),("firewall",state("firewall")),("firewall_error",error("firewall"))]
}
fn log_fields(data:&std::path::Path, code:&str, fields:&[(&'static str,String)]) {
    let borrowed:Vec<(&str,&str)>=fields.iter().map(|(k,v)|(*k,v.as_str())).collect();
    shell_log::fields(data,code,&borrowed);
}
/// Waits up to `ms` for the launched process, then returns its exit code and closes the handle.
fn reap(handle:Option<isize>, ms:u32) -> (bool,Option<u32>) {
    use windows::Win32::{Foundation::{CloseHandle,HANDLE,WAIT_OBJECT_0}, System::Threading::{GetExitCodeProcess,WaitForSingleObject}};
    let Some(raw)=handle else { return (false,None) };
    unsafe {
        let process=HANDLE(raw as _);
        let stopped=WaitForSingleObject(process,ms)==WAIT_OBJECT_0;
        let mut code=0u32;
        let code=(stopped && GetExitCodeProcess(process,&mut code).is_ok()).then_some(code);
        let _=CloseHandle(process);
        (stopped,code)
    }
}
/// §2.2.6 unexpected exit: banner, then a stopped-state relaunch (≤3 per 5 min). True = relaunch.
/// 10-30: while the window is not in front the shell also shows its own `service_stopped` card.
fn crash_restart(code:Option<u32>, lifecycle:&Arc<Lifecycle>, policy:&mut crash::RestartPolicy, recovering:&mut Option<(Option<u32>,usize)>,
    hub:&Mutex<Hub>, desk:&notify::Desk, data:&std::path::Path, timing:Timing) -> bool {
    lifecycle.set_sidecar(Sidecar::Down);
    let exit=code.map_or("none".into(),|c|c.to_string());
    desk.service_stopped(Instant::now());
    match policy.on_crash(Instant::now()) {
        crash::Restart::Allowed{attempt}=>{
            shell_log::fields(data,"sidecar_crashed",&[("exit_code",&exit),("restart",&attempt.to_string())]);
            hub.lock().unwrap().banner(crash::banner::crashed("restarting",code,attempt));
            *recovering=Some((code,attempt));
            // A quit during the pause finds no sidecar and exits 0; then nothing is relaunched.
            let until=Instant::now()+timing.restart_delay;
            while Instant::now()<until && !lifecycle.finished() { lifecycle.tick(); std::thread::sleep(Duration::from_millis(20)); }
            !lifecycle.finished()
        },
        crash::Restart::Exhausted=>{
            shell_log::fields(data,"sidecar_crashed",&[("exit_code",&exit),("restart","exhausted")]);
            hub.lock().unwrap().banner(crash::banner::crashed("unrecoverable",code,crash::RESTART_LIMIT));
            false
        },
    }
}
/// The first sidecar's start (11-05 window-first launch): `Ok` at its hello, `Err(code)` when it
/// never said hello. `run` no longer waits for it before the window: [`watch_startup`] does.
type StartupRx=mpsc::Receiver<Result<(),String>>;
fn start_worker(options:&Options, feed:Arc<tray::Feed>, hub:Arc<Mutex<Hub>>, shared:Shared) -> io::Result<(Bridge,mpsc::Receiver<()>,StartupRx)> {
    // T-P6-06: the resolved scope (kind + id), never a hard-coded "custom".
    let (scope_kind,scope)=(options.scope_kind,options.scope_id.clone());
    let exe=options.exe.clone(); let child_args=options.child_args.clone(); let data=options.data.clone(); let timing=options.timing;
    let h=hub.clone();
    // T-P3-06: the host commands publish their res to the page hub and duplicate into ChildSlot's
    // process; the hand-off knows the data directory (installer root) and the residual roots.
    {
        let (publish_hub,locale_feed)=(hub.clone(),feed.clone());
        shared.host.attach(options.config.clone(),options.data.clone(),Box::new(move |frame| publish_hub.lock().unwrap_or_else(|e|e.into_inner()).host(frame)),
            shared.child.clone(),Box::new(move || locale_feed.status().locale));
        shared.update.configure(options.data.clone(),options.install_roots.clone(),Some(options.data.clone()));
    }
    let autostart_query=options.autostart;
    let (tx,rx)=mpsc::sync_channel(64); let (ready_tx,ready_rx)=mpsc::channel::<Result<(),String>>(); let (done_tx,done_rx)=mpsc::channel();
    let (control_tx,control_rx)=mpsc::sync_channel(16);
    let worker=shared.clone();
    std::thread::spawn(move || {
        let Shared{lifecycle,session_end,child,force_restart,unresponsive,done,desk,host:host_cmds,update,autostart,notice:_}=worker;
        let mut ready_tx=Some(ready_tx);
        let mut rebuilds=0; let mut policy=crash::RestartPolicy::default();
        // After a crash: (exit code, restarts) so the next hello turns the banner into "restarted".
        let mut recovering:Option<(Option<u32>,usize)>=None;
        loop {
        if !lifecycle.start_launch() { break; }
        let mut ready=false; let mut child_exit=None; let mut bye=false;
        let result=(|| -> io::Result<()> {
            let mut host=PrivatePipe::create()?.launch(&exe,&child_args)?;
            // Hold a handle to this exact launched process across pipe teardown.
            let process=unsafe { windows::Win32::System::Threading::OpenProcess(
                windows::Win32::System::Threading::PROCESS_SYNCHRONIZE|windows::Win32::System::Threading::PROCESS_TERMINATE
                |windows::Win32::System::Threading::PROCESS_QUERY_LIMITED_INFORMATION|windows::Win32::System::Threading::PROCESS_DUP_HANDLE,false,host.child_pid())
                .map_err(|_|io::Error::other("child_wait_handle_failed"))? };
            child.set(Some(process.0 as isize));
            let hello=host.hello().clone();
            if hello["t"]!="hello" || hello["proto"]!=1 || hello["runtime_pid"]!=host.child_pid()
                || !hello_scope_matches(&hello,scope_kind,&scope)
                || !hello["caps"]["topics"].as_array().is_some_and(|v|TOPICS.iter().all(|t|v.iter().any(|x|x==t))) {
                return Err(io::Error::other("hello_contract_mismatch"));
            }
            let epoch=hello["epoch"].as_str().filter(|e|!e.is_empty()).ok_or_else(||io::Error::other("epoch_missing"))?.to_owned();
            // A new epoch: the hub publishes rt:reset, the page drops the old cache (§2.2.6).
            h.lock().unwrap().reset(hello.clone());
            feed.hello(&hello);
            // T-P3-05: hello.lang sets the native text (feed) and the page language; ui.language later.
            desk.session();
            if let Some(frame)=hello["lang"].as_str().and_then(notify::language_frame) {
                let lang=frame["language"].as_str().unwrap_or("-").to_owned();
                if h.lock().unwrap().host_sticky("language",frame) { shell_log::fields(&data,"language_applied",&[("lang",&lang),("source","hello")]); }
            }
            let topics=notify::subscription(&hello);
            let (can_receipt,can_presence)=(notify::supports(&hello,"alerts.desktopDelivered"),notify::supports(&hello,"ui.presence"));
            // T-P3-06: save grants bind to this epoch and pid; an unanswered hand-off of a dead epoch is dropped.
            host_cmds.session(&epoch,host.child_pid(),notify::supports(&hello,host::SAVE_GRANT));
            update.session();
            let (can_exit_response,can_install_result)=(notify::supports(&hello,update::EXIT_RESPONSE),notify::supports(&hello,update::INSTALL_RESULT));
            let mut calls:BTreeMap<u64,Pending>=BTreeMap::new();
            let mut cursor=notify::Cursor::default(); let mut reporter=presence::Reporter::default(); let mut sample_at=Instant::now();
            let mut presence_log=presence::LogGate::default();
            // Shell report requests (receipt / presence) awaiting the sidecar's reply; bounded so a
            // sidecar that never answers cannot grow it for the whole session (oldest ids go first).
            let mut report_ids:BTreeMap<u64,&'static str>=BTreeMap::new(); let mut unanswered=0u64;
            let mut queue=SendQueue::new([
                json!({"t":"hello_ack","proto":1,"accepted":true}),
                json!({"t":"subscribe","id":1,"topics":topics})]);
            // A failed install is reported to the relaunched sidecar (new epoch), once.
            if let Some(args)=update.take_failure() {
                let reason=args["reason_code"].as_str().unwrap_or("-").to_owned();
                if can_install_result {
                    let id=next_id(&h);
                    if queue.shell(update::request_frame(id,update::INSTALL_RESULT,args)) { calls.insert(id,Pending::InstallResult); }
                    shell_log::fields(&data,"update_install_result_sent",&[("reason",&reason)]);
                } else { shell_log::fields(&data,"update_install_result_unsupported",&[("reason",&reason)]); }
            }
            // `--autostart`: read ui.start_in_tray_on_autostart once (missing → false).
            if autostart_query && !autostart.decided() {
                if notify::supports(&hello,"config.snapshot") {
                    let id=next_id(&h);
                    if queue.shell(config_query(id)) { calls.insert(id,Pending::Config); } else { autostart.decide(false); }
                } else { log_code(&data,"autostart_setting_unsupported"); autostart.decide(false); }
            }
            ready=true; lifecycle.set_sidecar(Sidecar::Live);
            shell_log::fields(&data,"sidecar_ready",&[("pid",&host.child_pid().to_string())]);
            if let Some(tx)=ready_tx.take() { let _=tx.send(Ok(())); }
            if let Some((code,restarts))=recovering.take() { h.lock().unwrap().banner(crash::banner::crashed("restarted",code,restarts)); }
            let mut quit=false; let mut ping=Instant::now(); let mut n=0;
            let mut watchdog=crash::Watchdog::new(Instant::now(),endsession::resume_count(),timing.pong_timeout);
            let mut shell_ids:Vec<u64>=Vec::new(); let mut drain_until=None;
            loop {
                let now=Instant::now();
                lifecycle.tick();
                if !quit {
                    if let Some(reason)=lifecycle.take_send() {
                        quit=true; let id=next_id(&h); shell_ids.push(id);
                        // Preserve mandatory handshake frames; discard unsent page work.
                        queue.quit(json!({"t":"req","id":id,"cmd":"app.quit","args":{"reason":reason.wire()},"from":"shell"}));
                        shell_log::fields(&data,"quit_sent",&[("reason",reason.wire())]);
                    }
                }
                // Also after app.quit: until bye, a session end still goes out (T-P3-05 item 5).
                if let Some((reason,budget))=session_end.take_request() {
                    // Straight from the window hook to the front of the control queue.
                    quit=true; let id=next_id(&h); shell_ids.push(id);
                    queue.urgent(endsession::request_frame(id,reason,budget)); session_end.sent(id);
                    shell_log::fields(&data,"session_end_sent",&[("reason",reason),("budget_ms",&budget.to_string())]);
                }
                // T-P3-05 receipts (each delivery id once) and presence changes. 10-30: a due hold
                // (cards kept while the user is busy) is looked at first, so its results go out too.
                desk.tick(&h,now);
                for receipt in desk.receipts(now) {
                    let (outcome,surface)=receipt.outcome.wire();
                    let fields=[("delivery",receipt.delivery_id.as_str()),("outcome",outcome),("surface",surface)];
                    if quit { shell_log::fields(&data,"notify_receipt_skipped_quitting",&fields); continue; }
                    if !can_receipt { shell_log::fields(&data,"notify_receipt_unsupported",&fields); continue; }
                    let id=next_id(&h);
                    if queue.shell(notify::receipt_frame(id,&receipt)) { report_ids.insert(id,"notify_receipt_reply"); shell_log::fields(&data,"notify_receipt_sent",&fields); }
                    else { shell_log::fields(&data,"notify_receipt_overflow",&fields); }
                }
                if can_presence && !quit && now>=sample_at {
                    sample_at=now+presence::SAMPLE;
                    let generation=h.lock().unwrap().page_generation();
                    if let Some(args)=reporter.sample(desk.probe.window(),desk.page.get(generation)) {
                        let id=next_id(&h);
                        // A line per visible/page change; focus-only changes are counted into it.
                        if let Some(focus_only)=presence_log.note(&args) {
                            let mut fields=presence::fields(&args); fields.push(("focus_only",focus_only.to_string()));
                            log_fields(&data,"presence_sent",&fields);
                        }
                        if let Some(replaced)=queue.presence(presence::request_frame(id,args)) { report_ids.remove(&replaced); }
                        report_ids.insert(id,"presence_reply");
                    }
                }
                // T-P3-06 shell calls: save grants (this epoch only) and the hand-off answer.
                for call in host_cmds.outbox.drain() {
                    if quit || call.epoch!=epoch { let _=call.reply.send(Err("runtime_gone")); continue; }
                    let id=next_id(&h);
                    if queue.shell(host::call_frame(id,&call)) { calls.insert(id,Pending::Host(call.reply)); } else { let _=call.reply.send(Err("job_capacity")); }
                }
                if can_exit_response && !quit {
                    // An exitResponse the sidecar never answers must not hold the hand-off slot (review S1/S5).
                    update.expire_response(now,crate::now());
                    if let Some(args)=update.take_response(crate::now(),lifecycle.running()) {
                        let id=next_id(&h); let handoff_id=args["handoff_id"].as_str().unwrap_or_default().to_owned();
                        if queue.shell(update::request_frame(id,update::EXIT_RESPONSE,args)) { calls.insert(id,Pending::ExitResponse(handoff_id)); }
                        else { update.on_response_reply(&handoff_id,false); log_code(&data,"update_exit_response_overflow"); }
                    }
                }
                while report_ids.len()>REPORT_CAPACITY {
                    let Some((&oldest,&code))=report_ids.iter().next() else { break };
                    report_ids.remove(&oldest); unanswered+=1;
                    if unanswered==1 || unanswered%64==0 { shell_log::fields(&data,"shell_report_unanswered",&[("kind",code),("total",&unanswered.to_string())]); }
                }
                // T-P2-31b: list-slot replacement snapshots and next pages the hub asked for (the
                // shell is the v1 subscriber, PROTOCOL §11); one the lane cannot take is retried.
                if !quit {
                    // T-P4-C3: a new epoch's seed that ran into its grace ends with rt:replayed.
                    let seed={ let mut hub=h.lock().unwrap(); hub.seed_tick(now); hub.take_seed_outcome() };
                    if let Some(fields)=seed { log_fields(&data,"rt_seed_done",&fields); }
                    // T-P3-H2: the rt:stale notices this produced go to shell.log too.
                    let (requests,stale,pool)={ let mut hub=h.lock().unwrap(); let requests=hub.take_requests(); (requests,hub.take_stale_log(),hub.take_pool_log()) };
                    for fields in stale { log_fields(&data,"rt_stale",&fields); }
                    // Bastion trace (CLAUDE_CHANGES §8r, diagnostic): bastion.pool frames, replies, resyncs
                    for fields in pool { log_fields(&data,"rt_pool",&fields); }
                    for frame in requests {
                        if !queue.shell(frame.clone()) { h.lock().unwrap().request_unsent(&frame); }
                    }
                }
                if !quit && queue.pump(&rx,&control_rx,&epoch) { lifecycle.request(QuitReason::HostGone); }
                if ping.elapsed()>Duration::from_secs(2) && !quit {
                    n+=1; queue.ping(json!({"t":"ping","n":n})); ping=Instant::now();
                }
                // §2.2.6: 15 s without pong only raises the banner; never app.quit, never a kill.
                if !quit && watchdog.tick(now,endsession::resume_count())==Some(crash::Watch::Unresponsive) {
                    log_code(&data,"pong_timeout"); unresponsive.store(true,Ordering::Release);
                    h.lock().unwrap().banner(crash::banner::unresponsive(true));
                }
                if force_restart.swap(false,Ordering::AcqRel) {
                    if watchdog.unresponsive && !quit && child_exit.is_none() {
                        log_code(&data,if child.terminate(crash::FORCE_RESTART_CODE) {"sidecar_force_restart"} else {"sidecar_force_restart_failed"});
                    } else { log_code(&data,"sidecar_force_restart_ignored"); }
                }
                if child_exit.is_none() && host.poll_write()? {
                    if let Some(frame)=queue.next() {
                        let id=frame["id"].as_u64();
                        let mut bytes=serde_json::to_vec(&stamp(frame,&epoch))?; bytes.push(b'\n');
                        // T-P2-31b fix round 1 (M2): Hub::request refuses oversized page frames; this
                        // backstop drops any frame the pipe would refuse instead of failing the session.
                        if bytes.len()>bridge_host::MAX_FRAME {
                            log_code(&data,"frame_too_large");
                            // Only page requests are pending in the hub; the shell's own frames are small.
                            if let Some(id)=id { h.lock().unwrap().reject_oversized(id); }
                        } else if !host.send(&bytes)? { return Err(io::Error::other("write_busy")); }
                    }
                }
                for event in host.poll_event()? {
                    match event {
                        Event::Frame(line)=>{
                            let frame:Value=match serde_json::from_str(&line) { Ok(f)=>f,Err(_)=>{log_code(&data,"invalid_json");continue;} };
                            if quit && lifecycle::is_progress(&frame) { lifecycle.progress(); }
                            // Replies to the shell's own app.quit / app.sessionEnd never reach the page.
                            if matches!(frame["t"].as_str(),Some("ack"|"res"|"error")) && frame["id"].as_u64().is_some_and(|id|shell_ids.contains(&id)) {
                                if frame["epoch"]!=json!(epoch) { log_code(&data,"epoch_mismatch"); continue; }
                                if session_end.id()==frame["id"].as_u64() {
                                    let outcome=endsession::Outcome{sent:true,reply:Some(frame.clone()),bye:false,timed_out:false};
                                    log_fields(&data,"session_end_reply",&endsession::result_fields(&outcome));
                                    session_end.reply(frame);
                                } else if frame["t"]!="ack" || frame["accepted"]!=true { log_code(&data,"quit_not_accepted"); }
                                continue;
                            }
                            // T-P2-31b: replies to the shell's own snapshot_req (list slots) stay in the hub.
                            // T-P4-C3 fix round 1: a state replacement the hub cached is a tray fact too.
                            if frame["t"]=="snapshot_res" {
                                let outcome={ let mut hub=h.lock().unwrap(); if hub.owns_snapshot_request(&frame) { Some((hub.snapshot_res(&frame),hub.take_facts())) } else { None } };
                                if let Some((outcome,facts))=outcome { feed.apply(facts); if let Err(code)=outcome { log_code(&data,code); } continue; }
                            }
                            // T-P3-06 replies (save grants, hand-off, install result, autostart setting) stay in the shell.
                            if matches!(frame["t"].as_str(),Some("res"|"error")) {
                                if let Some(pending)=frame["id"].as_u64().and_then(|id|calls.remove(&id)) {
                                    if frame["epoch"]!=json!(epoch) { log_code(&data,"epoch_mismatch"); continue; }
                                    let ok=frame["t"]=="res" && frame["ok"]==true;
                                    match pending {
                                        Pending::Host(reply)=>{ let _=reply.send(Ok(frame)); },
                                        Pending::ExitResponse(handoff_id)=>{
                                            if update.on_response_reply(&handoff_id,ok && frame["data"]["recorded"]==true) {
                                                // Ruling P2-27: the shell sends app.quit{reason:update} itself.
                                                if lifecycle.request_if_running(QuitReason::Update) { log_code(&data,"update_quit_requested"); } else { update.quit_refused(); }
                                            }
                                        },
                                        Pending::Decline=>log_fields(&data,"update_decline_reply",&report_reply_fields(&frame)),
                                        Pending::InstallResult=>log_fields(&data,"update_install_result_reply",&report_reply_fields(&frame)),
                                        Pending::Config=>{
                                            let tray=autostart_tray(&frame);
                                            shell_log::fields(&data,"autostart_setting",&[("ok",if ok {"true"} else {"false"}),("tray",if tray {"true"} else {"false"})]);
                                            autostart.decide(tray);
                                        },
                                    }
                                    continue;
                                }
                            }
                            // Replies to the shell's receipts and presence reports stay in the shell too.
                            if matches!(frame["t"].as_str(),Some("res"|"error")) {
                                if let Some(code)=frame["id"].as_u64().and_then(|id|report_ids.remove(&id)) {
                                    if frame["epoch"]!=json!(epoch) { log_code(&data,"epoch_mismatch"); continue; }
                                    let fields=report_reply_fields(&frame);
                                    // A recorded presence is routine; only its failures are logged.
                                    if code!="presence_reply" || fields[0].1!="true" { log_fields(&data,code,&fields); }
                                    continue;
                                }
                            }
                            let is_bye=frame["t"]=="bye";
                            let bye_log=is_bye.then(||bye_fields(&frame));
                            if frame["t"]=="subscribed" && (frame["id"]!=1 || frame["topics"]!=json!(topics)) { log_code(&data,"subscription_mismatch"); continue; }
                            let is_pong=frame["t"]=="pong";
                            // T-P3-06: bye facts for the hand-off; export results make their refs revealable.
                            let bye_frame=is_bye.then(||frame.clone());
                            let has_jobs=frame["topic"]=="jobs" || frame["items"].as_array().is_some_and(|items|items.iter().any(|i|i["topic"]=="jobs"));
                            let jobs_frame=has_jobs.then(||frame.clone());
                            let shell_items=match accept_frame(&h,&feed,frame) {
                                Ok(items)=>items,
                                Err(code)=>{
                                    log_code(&data,code);
                                    if transport::rebuild_required(code) { return Err(io::Error::other(code)); }
                                    continue;
                                },
                            };
                            if let Some(jobs)=&jobs_frame { host_cmds.observe(jobs); }
                            if let Some(bye)=&bye_frame { update.on_bye(bye); }
                            for item in &shell_items {
                                let Some(handoff)=shell_item(item,&epoch,&mut cursor,&desk,&h,&feed,&data) else { continue };
                                if !can_exit_response || quit { log_code(&data,"update_handoff_ignored"); continue; }
                                if let Some(args)=update.on_prepare_exit(&handoff,crate::now(),lifecycle.running()) {
                                    let id=next_id(&h);
                                    if queue.shell(update::request_frame(id,update::EXIT_RESPONSE,args)) { calls.insert(id,Pending::Decline); }
                                }
                            }
                            if is_pong && watchdog.pong(now)==Some(crash::Watch::Recovered) {
                                log_code(&data,"pong_recovered"); unresponsive.store(false,Ordering::Release);
                                h.lock().unwrap().banner(crash::banner::unresponsive(false));
                            }
                            if let Some(fields)=bye_log {
                                bye=true; log_fields(&data,"bye",&fields);
                                lifecycle.bye(); session_end.bye();
                            }
                        },
                        Event::ChildExited(code)=>{child_exit=Some(code);drain_until=Some(Instant::now()+Duration::from_secs(1));},
                        Event::Disconnected=>{log_code(&data,"pipe_disconnected");},
                        _=>log_code(&data,"invalid_frame"),
                    }
                }
                if child_exit.is_some() && (bye || drain_until.is_some_and(|until|Instant::now()>=until)) { break; }
                std::thread::sleep(Duration::from_millis(10));
            }
            Ok(())
        })();
        // A WM_ENDSESSION waiting on this session (ours, or one that found a quit already
        // running) must not sit out its whole budget once the session is gone.
        if session_end.active() || lifecycle.quitting() { session_end.ended(); }
        if unresponsive.swap(false,Ordering::AcqRel) { h.lock().unwrap().banner(crash::banner::unresponsive(false)); }
        host_cmds.session_gone();
        let handle=child.take();
        match (result,child_exit) {
            (Ok(()),Some(code))=>{
                let _=reap(handle,0);
                log_code(&data,&format!("sidecar_exit_{code}"));
                if !bye { log_code(&data,"bye_missing"); }
                h.lock().unwrap().gone(); feed.gone();
                update.sidecar_gone(true,Some(code));
                // In a quit or session end: leave with the sidecar's exit code. A user-forced quit
                // already exited the shell; its terminated sidecar is not a crash to restart.
                // An update quit installs instead (T-P3-06); a failed install relaunches below.
                let exited=lifecycle.sidecar_exited(code);
                if exited==Exited::Installing && install_relaunch(&lifecycle) { log_code(&data,"update_relaunch_stopped"); }
                else if exited!=Exited::Unhandled || lifecycle.finished() { break; }
                else {
                    match crash::classify(code) {
                        crash::ExitKind::Normal|crash::ExitKind::CleanupFailed=>{ lifecycle.follow_sidecar_exit(code); break; },
                        crash::ExitKind::Crash=>{},
                    }
                    if !crash_restart(Some(code),&lifecycle,&mut policy,&mut recovering,&h,&desk,&data,timing) { break; }
                }
            },
            (result,_)=>{
                let error=result.err().unwrap_or_else(||io::Error::other("session_ended_without_exit"));
                let code=match error.raw_os_error() { Some(os)=>format!("os_{os}"), None=>shell_log::token(&error.to_string()) };
                if let Some(os)=error.raw_os_error() {log_code(&data,&format!("bridge_os_error_{os}"));}
                else if ["identity_mismatch","hello_timeout","hello_contract_mismatch","epoch_missing","child_wait_handle_failed"].contains(&error.to_string().as_str()) {log_code(&data,&error.to_string());}
                if !ready {
                    // The sidecar never said hello (it exited, timed out or broke the contract).
                    let _=reap(handle,5000);
                    if let Some(tx)=ready_tx.take() {
                        log_code(&data,"sidecar_start_failed"); lifecycle.set_sidecar(Sidecar::Down);
                        let _=tx.send(Err(code)); break;
                    }
                    if lifecycle.finished() { break; }
                    log_code(&data,"sidecar_restart_failed");
                    if !crash_restart(None,&lifecycle,&mut policy,&mut recovering,&h,&desk,&data,timing) { break; }
                } else {
                    log_code(&data,"bridge_worker_failed"); h.lock().unwrap().gone(); feed.gone();
                    let (stopped,exit)=reap(handle,20000);
                    if !stopped { log_code(&data,"runtime_cleanup_pending"); }
                    update.sidecar_gone(stopped,exit);
                    let exited=lifecycle.sidecar_exited(exit.unwrap_or(1));
                    if exited==Exited::Installing && install_relaunch(&lifecycle) {
                        // A live sidecar still holds its runtime lease: no relaunch (as below).
                        if !stopped { h.lock().unwrap().banner(crash::banner::crashed("unrecoverable",None,0)); break; }
                        log_code(&data,"update_relaunch_stopped");
                        while rx.try_recv().is_ok() {}
                        while control_rx.try_recv().is_ok() {}
                        continue;
                    }
                    if exited!=Exited::Unhandled || lifecycle.finished() { break; }
                    if transport::rebuild_required(&error.to_string()) && stopped && rebuilds<3 {
                        // gone() already closed admission; old requests must never become a new-epoch start.
                        while rx.try_recv().is_ok() {}
                        while control_rx.try_recv().is_ok() {}
                        rebuilds+=1; log_code(&data,"protocol_session_rebuild"); continue;
                    }
                    // A live sidecar still holds its runtime lease: a relaunch would only wait on it.
                    if !stopped { h.lock().unwrap().banner(crash::banner::crashed("unrecoverable",None,0)); break; }
                    if !crash_restart(exit,&lifecycle,&mut policy,&mut recovering,&h,&desk,&data,timing) { break; }
                }
            },
        }
        // Stopped-state relaunch: no request of the old epoch may reach the new sidecar.
        while rx.try_recv().is_ok() {}
        while control_rx.try_recv().is_ok() {}
        }
        // No sidecar will run again: a pending quit leaves now, a later one exits 0 at once.
        lifecycle.set_sidecar(Sidecar::Down);
        done.store(true,Ordering::SeqCst);
        session_end.ended();
        let _=done_tx.send(());
    });
    // 11-05 window-first launch: the window opens while the sidecar starts (no wait here).
    Ok((Bridge{hub,tx,control_tx,shared},done_rx,ready_rx))
}
/// The longest the first sidecar may take to say hello (§2.2.6), as `run` used to wait for it.
const STARTUP_LIMIT:Duration=Duration::from_secs(15);
/// What ended a start that never reached hello, or None for a hello / a quit that came first.
/// The waiter's answer: the worker's failure code, its 15 s silence, or a worker that left
/// without a word (only a quit makes it leave before hello; anything else is a failed start).
fn startup_failure(answer:Result<Result<(),String>,mpsc::RecvTimeoutError>, running:bool) -> Option<String> {
    let error=match answer {
        Ok(Ok(()))=>return None,
        Ok(Err(code))=>format!("sidecar_start_failed_{code}"),
        Err(_)=>"sidecar_start_timeout".to_owned(),
    };
    // A quit the user asked for while it was starting ends the shell by itself, with no box.
    running.then_some(error)
}
/// 11-05 window-first launch: the window (and the page's launch screen) is up while the first
/// sidecar starts. A start that never reaches hello ends the shell as before (§2.2.6): exit code
/// 1 is decided first, the normal quit path closes the window and the tray (no sidecar is
/// running, so it exits at once), then `run` gives up the instance lease and shows the
/// pre-hello box on its own thread, as it did before the window existed.
fn watch_startup(app:tauri::AppHandle, ready:StartupRx, lifecycle:Arc<Lifecycle>, exit_code:Arc<ExitSlot>, failure:Arc<Mutex<Option<String>>>, data:PathBuf) {
    std::thread::spawn(move || {
        let answer=ready.recv_timeout(STARTUP_LIMIT);
        let failed=answer.as_ref().is_ok_and(|a|a.is_err()) || answer.is_err();
        let Some(error)=startup_failure(answer,lifecycle.running()) else {
            if failed { log_code(&data,"sidecar_start_failed_while_quitting"); }
            return;
        };
        // The launch screen goes at once; the box follows once the shell has wound down.
        if app.get_webview_window("main").is_some_and(|w| w.hide().is_ok()) { log_code(&data,"startup_failed_window_hidden"); }
        *failure.lock().unwrap_or_else(|e|e.into_inner())=Some(error);
        exit_code.decide(1);
        lifecycle.request(QuitReason::HostGone);
    });
}
fn run() -> Result<i32,Box<dyn std::error::Error>> {
    let cli_args:Vec<String>=env::args().skip(1).collect();
    // T-P6-06 `--print-scope` (both builds): the decision only; no lock, directory, log or process.
    if cli_args.iter().any(|a|a=="--print-scope") {
        let (report,code)=scope_report(&|name|env::var_os(name),DISTRIBUTION,&cli_args,&env::current_exe()?);
        println!("{report}");
        return Ok(code);
    }
    if let Some(code)=cli_mode(&cli_args) {
        // T-P6-06: a distribution build names itself as the product (Tesseract.exe, app_version.txt).
        if code==0 { if DISTRIBUTION {println!("Tesseract {}",APP_VERSION.trim());} else {println!("Tesseract Shell {}",env!("CARGO_PKG_VERSION"));} }
        else if DISTRIBUTION {println!("Usage: Tesseract.exe [--autostart] [--sidecar PATH] [--print-scope]");}
        else {println!("Usage: tesseract-shell [--autostart] [--dev-rt] [--sidecar PATH]");}
        return Ok(0);
    }
    // §2.2.1 step 1 (T-P3-07), before any thread or WebView2 call: release builds drop the
    // WebView2 environment overrides from this process (debug keeps the four of §2.2.1), and the
    // process carries the AUMID `Tesseract.ProxyApp`.
    let scrubbed=webview::scrub_process_environment(!cfg!(debug_assertions));
    let aumid=webview::set_app_user_model_id();
    let scope=resolve_scope(&|name|env::var_os(name),DISTRIBUTION)?;
    // T-P6-06: a distribution build refuses a sidecar override (default scope) before it creates a
    // directory, writes shell.log or takes an instance name (`options` checks the same again).
    if DISTRIBUTION { sidecar_source(DISTRIBUTION,cfg!(debug_assertions),scope.kind,&cli_args,&env::current_exe()?).map_err(io::Error::other)?; }
    // T-P6-06: the default directories are created as app_paths does (`mkdir(parents=True)`).
    if scope.kind=="default" { fs::create_dir_all(&scope.config)?; fs::create_dir_all(&scope.data)?; }
    let data=scope.data.clone();
    for name in &scrubbed { shell_log::fields(&data,"webview_env_scrubbed",&[("name",name)]); }
    shell_log::fields(&data,"aumid_set",&[("ok",if aumid {"true"} else {"false"})]);
    let panic_data=data.clone();
    // T-P6-06: the default scope carries the legacy names (plan §2.3: the Qt app and the shell
    // exclude each other); a custom scope its hashed names.
    let names=bridge_host::scope::names(scope.kind,&scope.id)?;
    let api=single_instance::Win32{data:data.clone()};
    let instance=match single_instance::acquire(api.clone(),&names,false) {
        Ok(single_instance::Outcome::Exit(code))=>return Ok(code),
        Ok(single_instance::Outcome::Start(lease))=>lease,
        Err(failure)=>{
            log_code(&data,&failure.code());
            native_text::show(match failure {
                single_instance::Failure::QuittingTimeout=>"quitting_timeout",
                single_instance::Failure::RuntimeTimeout=>"runtime_timeout",_=>"instance_unknown",
            });
            return Err(io::Error::other("single_instance_failed").into());
        }
    };
    let quitting=Arc::new(single_instance::Quitting::new(api,&names).map_err(|code| {
        log_code(&data,&format!("instance_win32_error_{code}"));native_text::show("instance_unknown");
        io::Error::from_raw_os_error(code as i32)
    })?);
    // §2.2.1 step 3: no WebView2 runtime → native box (Windows UI language, §7.10) with the
    // offline-installer link (through host.openUrl's whitelist), then exit. Debug builds can
    // simulate a missing runtime (TESSERACT_SHELL_DEV_WEBVIEW2_MISSING=1) for the smoke.
    let simulated=cfg!(debug_assertions) && env::var("TESSERACT_SHELL_DEV_WEBVIEW2_MISSING").as_deref()==Ok("1");
    match if simulated {Err(webview::MISSING_HRESULT)} else {webview::runtime_version()} {
        Ok(version)=>shell_log::fields(&data,"webview_runtime",&[("version",&version)]),
        Err(hresult)=>{
            shell_log::fields(&data,"webview_runtime_missing",&[("hresult",&format!("0x{:08x}",hresult as u32)),("simulated",if simulated {"true"} else {"false"})]);
            // The box waits for the user: free the scope's instance lease first (no sidecar runs).
            drop(instance);
            let open=dialog::webview2_missing(native_text::system_locale(),webview::RUNTIME_HELP_URL);
            if open { log_code(&data,if webview::open_runtime_help() {"webview_runtime_help_opened"} else {"webview_runtime_help_failed"}); }
            log_code(&data,"shell_exit_1");
            return Err(io::Error::other("webview2_runtime_missing").into());
        },
    }
    // §2.2.4: hear WM_QUERYENDSESSION before the sidecar and other applications.
    endsession::raise_shutdown_order(&data);
    let options=options(&scope)?;
    std::panic::set_hook(Box::new(move |info| {
        let location=info.location().map(|l|format!("panic_{}_{}",std::path::Path::new(l.file()).file_name().unwrap_or_default().to_string_lossy(),l.line())).unwrap_or_else(||"panic".into());
        log_code(&panic_data,&location);
    }));
    // §7.10: tray text uses the Windows UI language until hello.lang arrives.
    let feed=tray::Feed::new(native_text::system_locale());
    let hub=Arc::new(Mutex::new(Hub::default()));
    let app:Arc<Mutex<Option<tauri::AppHandle>>>=Arc::new(Mutex::new(None)); let pending_exit=Arc::new(Mutex::new(None));
    let exit_code=Arc::new(ExitSlot::default());
    let child=Arc::new(ChildSlot::default());
    let update=update::Update::new(Box::new(update::Win32Platform));
    let lifecycle=Lifecycle::new(Box::new(ShellEffects{data:data.clone(),hub:hub.clone(),app:app.clone(),pending_exit:pending_exit.clone(),
        quitting:quitting.clone(),feed:feed.clone(),child:child.clone(),dismiss:Arc::new(AtomicBool::new(false)),exit_code:exit_code.clone(),update:update.clone()}));
    let probe=Arc::new(presence::Win32Probe::default());
    // 10-30: notices outside the window go to the card window (attached in setup; until then, and
    // whenever it cannot show a card, the tray balloon), held while Windows says the user is busy.
    let notice_host=notice::Host::new();
    let desk=Arc::new(notify::Desk::new(Box::new(probe.clone()),notify::Desk::tray_balloon(),Some(data.clone()))
        .with_cards(notice_host.clone()).with_busy(Box::new(notice::user_busy)).with_sound(Arc::new(notice::play_sound)));
    let mut shared=Shared::with(lifecycle.clone(),child,desk,host::Host::new(Box::new(hostwin::Win32Desktop)),update);
    shared.notice=notice_host;
    // 11-05 window-first launch: the sidecar starts while the window opens (`watch_startup`).
    let (bridge,done,startup)=start_worker(&options,feed.clone(),hub,shared.clone())?;
    let startup_failed:Arc<Mutex<Option<String>>>=Arc::new(Mutex::new(None));
    let (watch_lifecycle,watch_exit,watch_failed)=(lifecycle.clone(),exit_code.clone(),startup_failed.clone());
    let worker_done=shared.done.clone();
    let quit_port=QuitPort{lifecycle:lifecycle.clone()};
    let close_port=quit_port.clone(); let verify_port=quit_port.clone(); let placement_data=data.clone(); let close_data=data.clone();
    let session_end=shared.session_end.clone(); let end_lifecycle=lifecycle.clone(); let end_feed=feed.clone();
    let exit_session_end=shared.session_end.clone();
    // T-P3-07: cloak/reveal gate, WebView2 failure handling and the exit wait.
    // Debug-only TESSERACT_SHELL_DEV_EXIT_WAIT_IGNORE_EXITED=1: the exit wait runs into its cap, so
    // the shell smoke can prove it accepts the `timeout` branch (fix round 1, M1).
    let ignore_exited=cfg!(debug_assertions) && env::var("TESSERACT_SHELL_DEV_EXIT_WAIT_IGNORE_EXITED").as_deref()==Ok("1");
    let supervisor=webview::Supervisor::with_exit_hook(data.clone(),webview::ready_timeout(cfg!(debug_assertions),env::var("TESSERACT_SHELL_DEV_READY_TIMEOUT_MS").ok()),ignore_exited);
    let setup_supervisor=supervisor.clone(); let recreate_lifecycle=lifecycle.clone();
    let builder=tauri::Builder::default().manage(bridge).manage(single_instance::ScopeNames(names)).manage(supervisor.clone());
    #[cfg(debug_assertions)] let builder=builder.invoke_handler(tauri::generate_handler![rt,rt_connect,rt_resync,host_confirm_quit,host_force_restart,host_notify_rendered,host_notify_ready,host_set_page,host_ready,host_window,notice_ready,notice_rendered,notice_open,notice_dismiss,notice_resize,dev_quit]);
    #[cfg(not(debug_assertions))] let builder=builder.invoke_handler(tauri::generate_handler![rt,rt_connect,rt_resync,host_confirm_quit,host_force_restart,host_notify_rendered,host_notify_ready,host_set_page,host_ready,host_window,notice_ready,notice_rendered,notice_open,notice_dismiss,notice_resize]);
    let setup_quitting=quitting.clone();
    let result=builder.setup(move |application| {
        {
            let mut slot=app.lock().unwrap(); *slot=Some(application.handle().clone());
            // An exit decided before the event loop existed (e.g. a quit with no sidecar).
            if let Some(code)=pending_exit.lock().unwrap().take() { application.handle().exit(code); }
        }
        application.state::<Bridge>().hub.lock().unwrap().app=Some(application.handle().clone());
        // T-P3-06 `--autostart`: start hidden until the tray setting is known (shown below otherwise).
        // T-P3-07: cloaked until host_ready; a creation failure is retried with backoff (§7.9).
        let spec=WindowSpec{scope:options.scope_id.clone(),data:options.data.clone(),dev_page:options.dev_page,browser_args:options.browser_args.clone()};
        let (window,attempt)=spec.create(application.handle(),!options.autostart,&setup_supervisor,"startup")?;
        if attempt>1 { shell_log::fields(&options.data,"webview_created_after_retry",&[("attempt",&attempt.to_string())]); }
        // Tauri has no session-end callback: subclass the top-level main window (§2.2.4).
        let hwnd=windows::Win32::Foundation::HWND(window.hwnd()?.0);
        endsession::install(hwnd,endsession::Context::new(end_lifecycle,session_end,options.data.clone(),
            Box::new(move || end_feed.status().locale)));
        // T-P3-05: presence and notice routing read this window; the page hears host:visibility
        // (and a clicked balloon's host:navigate once the window is visible again).
        attach_window(application.handle(),&probe,hwnd);
        let bridge=application.state::<Bridge>();
        presence::spawn_visibility(bridge.shared.desk.clone(),bridge.hub.clone(),options.data.clone());
        // T-P3-07 §2.2.6: a lost browser process rebuilds the window with the same hooks.
        {
            let (app,weak,probe,lifecycle,feed)=(application.handle().clone(),Arc::downgrade(&setup_supervisor),probe.clone(),recreate_lifecycle.clone(),feed.clone());
            let session_end=application.state::<Bridge>().shared.session_end.clone();
            setup_supervisor.set_recreate(Arc::new(move |mode| {
                weak.upgrade().is_some_and(|supervisor| recreate_main_window(app.clone(),supervisor,spec.clone(),probe.clone(),lifecycle.clone(),feed.clone(),session_end.clone(),mode))
            }));
        }
        if let Some(port)=options.verify_port {
            let handle=application.handle().clone();
            acceptance::start(window,options.data.clone(),Box::new(move || verify_port.request_quit(&handle,QuitReason::Acceptance)),port);
        }
        log_code(&options.data,"shell_window_ready");
        // 11-05: the window is up; the first sidecar may still be starting (its page shows the launch).
        watch_startup(application.handle().clone(),startup,watch_lifecycle,watch_exit,watch_failed,options.data.clone());
        // 10-30 notice cards: the card window shares the main window's WebView2 profile and
        // browser arguments (one browser process); a click restores the main window the way the
        // RestoreListener does and goes to the card's target once the window is visible.
        {
            let bridge=application.state::<Bridge>();
            let (restore_app,restore_data,navigate_desk,locale_feed)=(application.handle().clone(),options.data.clone(),bridge.shared.desk.clone(),feed.clone());
            bridge.shared.notice.attach(notice::Env{app:application.handle().clone(),
                profile:single_instance::profile(&options.data,&options.scope_id,single_instance::elevated()?),browser_args:options.browser_args.clone(),
                reduced:window::reduced_motion(env::var_os(window::REDUCED_MOTION_ENV).as_deref()),devtools:cfg!(debug_assertions),data:options.data.clone()},
                notice::Hooks{
                    restore:Box::new(move || {
                        let restored=restore_app.get_webview_window("main").is_some_and(|w| w.show().and_then(|_|w.unminimize()).and_then(|_|w.set_focus()).is_ok());
                        if !restored { log_code(&restore_data,"notice_restore_failed"); }
                    }),
                    navigate:Box::new(move |target| navigate_desk.navigate.request(target,Instant::now())),
                    locale:Box::new(move || locale_feed.status().locale),
                });
        }
        install_tray(application,feed,quit_port,&options.data);
        if options.autostart { autostart_window(application.handle().clone(),application.state::<Bridge>().shared.autostart.clone(),options.data.clone()); }
        #[cfg(debug_assertions)]
        if env::var("TESSERACT_SHELL_DEV_MARK_QUITTING").as_deref()==Ok("1") {
            setup_quitting.mark_quitting().map_err(|e|io::Error::from_raw_os_error(e as i32))?;
            log_code(&options.data,"dev_mark_quitting");
        }
        #[cfg(not(debug_assertions))] let _=&setup_quitting;
        Ok(())
    }).on_window_event(move |window,event| {
        // 10-30: the notice card window is the shell's own (notice.rs): it never closes to the tray
        // or quits, and its moves are not the main window's placement.
        if window.label()==notice::LABEL {
            if let tauri::WindowEvent::CloseRequested{api,..}=event { api.prevent_close(); }
            return;
        }
        match event {
            tauri::WindowEvent::Moved(_)|tauri::WindowEvent::Resized(_) => {
                if let Some(view)=window.app_handle().get_webview_window("main") { window::persist(&view,&placement_data); }
                // 11-05 in-page title bar: the maximize button's glyph (kept for replay, sent on change only).
                if let (tauri::WindowEvent::Resized(_),Some(frame))=(event,window::caption_state(window)) {
                    window.app_handle().state::<Bridge>().hub.lock().unwrap_or_else(|e|e.into_inner()).host_sticky("window",frame);
                }
            },
            tauri::WindowEvent::CloseRequested{api,..}=>{
                let (requested,finished)=(close_port.requested(),worker_done.load(Ordering::Acquire));
                match tray::close_decision(requested,finished,|| tray::installed().is_some_and(|t|t.ensure_available())) {
                    // No sidecar will run again: the quit path exits 0 at once (§2.2.3 step 6).
                    tray::CloseAction::ExitNow=>{ api.prevent_close(); close_port.request_quit(window.app_handle(),QuitReason::WindowWithoutTray); },
                    tray::CloseAction::AwaitQuit=>api.prevent_close(),
                    tray::CloseAction::HideToTray=>{
                        api.prevent_close();
                        log_code(&close_data,if window.hide().is_ok() {"window_closed_to_tray"} else {"window_hide_failed"});
                    },
                    tray::CloseAction::RequestQuit=>{ api.prevent_close(); close_port.request_quit(window.app_handle(),QuitReason::WindowWithoutTray); },
                }
            },
            _=>(),
        }
    }).build(tauri::tauri_build_context!());
    match result {
        Ok(application)=>{
            let listener=single_instance::RestoreListener::start(&instance,application.handle().clone()).map_err(|code| {
                log_code(&data,&format!("instance_win32_error_{code}"));native_text::show("instance_unknown");
                lifecycle.request(QuitReason::HostGone); let _=done.recv_timeout(Duration::from_secs(20));
                io::Error::from_raw_os_error(code as i32)
            })?;
            // run_return always yields 0 after app.exit(code); the decided code wins (M1).
            // T-P3-07: while the main window is being recreated there is briefly no window at all;
            // that is not the user leaving. Exits the quit state machine asks for carry a code.
            let exit_guard=supervisor.clone();
            let code=exit_code.resolve(application.run_return(move |_,event| {
                if let tauri::RunEvent::ExitRequested{code:None,api,..}=&event {
                    if exit_guard.recovering() { api.prevent_exit(); log_code(exit_guard.data(),"exit_prevented_recreating"); }
                }
            }));
            if let Some(tray)=tray::installed() { tray.shutdown(); }
            // §7.9: the WebView2 browser releases the user data folder only after it exits; wait for
            // BrowserProcessExited (bounded) so an immediate restart does not fail to create.
            // Not at logoff/shutdown: nothing restarts, and Windows ends the process anyway. Not
            // after an update hand-off either: the installer is already running and may have to
            // replace this exe (fix round 1, A-S4 / B-S4).
            let closed=window::close_controller();
            // The card window's WebView (same browser process) must go too, or the browser never exits.
            if notice::close_controller() { log_code(&data,"notice_controller_closed"); }
            let update_quit=lifecycle.machine().reason()==Some(QuitReason::Update);
            let (waited,ms)=match webview::exit_wait(exit_session_end.active(),update_quit) {
                Ok(limit)=>webview::wait_browser_exit(&supervisor,limit),
                Err(skip)=>(skip.wire(),0),
            };
            if !closed { log_code(&data,"webview_controller_not_closed"); }
            shell_log::fields(&data,"webview_browser_exit_wait",&[("result",waited),("ms",&ms.to_string())]);
            drop(listener);
            drop(instance);
            // §2.2.6: the first sidecar exited (or never answered) before hello; the window and
            // the tray are gone. The dialog waits for the user indefinitely, so the scope's
            // instance lease went first (above) and a second launch is not blocked behind it.
            // Show the redacted tail of its own stderr log (display only, never in shell.log).
            if let Some(error_text)=startup_failed.lock().unwrap_or_else(|e|e.into_inner()).take() {
                let kind=crash::prehello_kind(&error_text);
                shell_log::fields(&data,"sidecar_prehello_dialog",&[("kind",if kind==crash::PreHello::Exited {"exited"} else {"handshake"})]);
                dialog::sidecar_before_hello(native_text::system_locale(),&data,kind,crash::stderr_tail(&data,crash::TAIL_LINES));
                log_code(&data,"sidecar_prehello_dialog_closed");
            }
            log_code(&data,&format!("shell_exit_{code}"));
            return Ok(code);
        },
        Err(e)=>{lifecycle.request(QuitReason::HostGone);let _=done.recv_timeout(Duration::from_secs(20));return Err(e.into());},
    }
}
/// T-P3-06 `--autostart` (D8): stay in the tray only when `ui.start_in_tray_on_autostart` is true
/// and the tray icon is up; otherwise (false, missing, no answer in 5 s, no tray) show the window.
/// 11-05: this starts with the window, before the sidecar's hello, so the 5 s answer budget
/// sits on top of the hello budget (`STARTUP_LIMIT`), as it did when the window came after hello.
fn autostart_window(app:tauri::AppHandle, autostart:Arc<Autostart>, data:PathBuf) {
    std::thread::spawn(move || {
        let setting=autostart.wait(STARTUP_LIMIT+Duration::from_secs(5));
        let deadline=Instant::now()+Duration::from_secs(3);
        let tray=|| tray::installed().is_some_and(|t|t.available());
        while setting==Some(true) && !tray() && Instant::now()<deadline { std::thread::sleep(Duration::from_millis(50)); }
        let hidden=autostart_hidden(setting,tray());
        let setting=match setting { Some(true)=>"true", Some(false)=>"false", None=>"unknown" };
        if hidden { shell_log::fields(&data,"autostart_start",&[("setting",setting),("window","tray")]); return; }
        let shown=app.get_webview_window("main").is_some_and(|w| { let shown=w.show().is_ok(); if shown { window::persist(&w,&data); } shown });
        shell_log::fields(&data,"autostart_start",&[("setting",setting),("window",if shown {"shown"} else {"show_failed"})]);
    });
}
/// Tray left click and 「显示 Tesseract」 signal the scope's restore event, so they run the
/// exact P3-02 RestoreListener path (show, unminimize, set focus) on the UI thread.
/// Any failure leaves the tray unavailable, and closing the window then quits.
fn install_tray(application:&tauri::App,feed:Arc<tray::Feed>,quit_port:QuitPort,data:&std::path::Path) {
    use single_instance::Api;
    let api=single_instance::Win32{data:data.to_path_buf()};
    let names=application.state::<single_instance::ScopeNames>().0.clone();
    let restore=match api.open_event(names["restore_event"].as_str().unwrap_or_default()) {
        Ok(Some(handle))=>handle,
        Ok(None)=>{log_code(data,"tray_install_failed_2");return;},
        Err(code)=>{log_code(data,&format!("tray_install_failed_{code}"));return;},
    };
    let restore_api=api.clone(); let app=application.handle().clone();
    let desk=application.state::<Bridge>().shared.desk.clone();
    let hooks=tray::Hooks{
        // The event handle lives as long as the process-wide tray; process exit closes it.
        restore:Box::new(move || { if restore_api.set(restore).is_err() { restore_api.log("tray_restore_signal_failed"); } }),
        quit:Box::new(move || quit_port.request_quit(&app,QuitReason::Tray)),
        // A clicked alert balloon: restore (above) only signals the restore event, so the page is
        // routed by the visibility monitor once the window is visible again (presence.rs).
        navigate:Box::new(move |target| desk.navigate.request(target,Instant::now())),
    };
    match tray::install(feed,data.to_path_buf(),hooks) {
        Ok(tray)=>tray.schedule_show(),
        Err(code)=>{ let _=api.close(restore); log_code(data,&format!("tray_install_failed_{code}")); },
    }
}
fn cli_mode(args:&[String])->Option<i32> {
    if args.iter().any(|a|a=="--version" || a=="-V") {Some(0)}
    else if args.iter().any(|a|a=="--help" || a=="-h") {Some(1)} else {None}
}
fn main() {
    let code=run().unwrap_or_else(|_| {
        eprintln!("tesseract-shell: initialization_failed (see scoped logs/shell.log)");1
    });
    // run_return lets the acquiring thread release ownership before exiting.
    std::process::exit(code);
}
#[cfg(test)] mod tests {
    use super::*;
    /// T-P4-C3: host:caps.invokes is exactly what both invoke tables register (minus the debug
    /// `dev_quit` hook and the notice card window's own invokes), so `T.rt.hostHas(name)` is never
    /// true for an invoke the shell lacks. 10-30: the notice window's are exactly `notice::INVOKES`.
    #[test] fn host_caps_invokes_match_both_invoke_tables() {
        let source=include_str!("main.rs");
        let table=|cfg:&str, notice:bool| -> Vec<String> {
            let marker=format!("#[cfg({cfg})] let builder=builder.invoke_handler(tauri::generate_handler![");
            let body=&source[source.find(&marker).unwrap()+marker.len()..]; let body=&body[..body.find(']').unwrap()];
            let mut names:Vec<String>=body.split(',').map(|s|s.trim().to_owned())
                .filter(|s|!s.is_empty()&&s!="dev_quit"&&s.starts_with("notice_")==notice).collect(); names.sort(); names
        };
        let mut caps:Vec<String>=transport::HOST_INVOKES.iter().map(|s|s.to_string()).collect(); caps.sort();
        let mut notices:Vec<String>=notice::INVOKES.iter().map(|s|s.to_string()).collect(); notices.sort();
        for cfg in ["debug_assertions","not(debug_assertions)"] {
            assert_eq!(table(cfg,false),caps,"{cfg}");
            assert_eq!(table(cfg,true),notices,"{cfg}");
        }
        // Each window calls only its own: every registered command checks the caller's label.
        let code=&source[..source.find("#[cfg(test)] mod tests").unwrap()];
        for name in transport::HOST_INVOKES.iter().chain(notice::INVOKES.iter()) {
            let at=code.find(&format!("fn {name}(")).unwrap_or_else(||panic!("fn {name}"));
            let body=&code[at..at+code[at..].find("\n}").unwrap()];
            let label=if name.starts_with("notice_") {"caller(&webview,notice::LABEL)?"} else {"caller(&webview,\"main\")?"};
            assert!(body.contains("webview:tauri::Webview") && body.contains(label),"{name} must check its caller: {body}");
        }
    }
    #[test] fn cdp_requires_debug_and_explicit_valid_port() {
        assert_eq!(env!("P3_ASSET_DEBUG"),if cfg!(debug_assertions) {"true"} else {"false"});
        assert!(!browser_arguments(false,Some("9222".into())).unwrap().contains("remote-debugging"));
        assert!(!browser_arguments(true,None).unwrap().contains("remote-debugging"));
        assert!(browser_arguments(true,Some("9222".into())).unwrap().contains("127.0.0.1"));
        assert!(browser_arguments(true,Some("1 --bad".into())).is_err());
    }
    #[test] fn generated_assets_embed_ui_instead_of_a_windows_url() {
        let context:tauri::Context<tauri::Wry>=tauri::tauri_build_context!();
        assert!(context.assets().get(&"index.html".into()).is_some());
        assert!(context.assets().get(&"build-manifest.json".into()).is_some());
        assert_eq!(context.assets().get(&"dev-rt.html".into()).is_some(),cfg!(debug_assertions));
        assert!(!matches!(context.config().build.frontend_dist,Some(tauri::utils::config::FrontendDist::Url(_))));
    }
    #[test] fn exit_code_is_the_decided_one_not_the_event_loop_zero() {
        let slot=ExitSlot::default();
        assert_eq!(slot.resolve(0),0,"no decision: the loop's own code");
        assert_eq!(slot.resolve(5),5);
        slot.decide(3);
        assert_eq!(slot.resolve(0),3,"app.exit(3) makes run_return return 0; the shell still exits 3");
        slot.decide(1);
        assert_eq!(slot.resolve(0),3,"the first decision wins");
        let slot=ExitSlot::default(); slot.decide(lifecycle::FORCED_EXIT); assert_eq!(slot.resolve(0),1);
        let slot=ExitSlot::default(); slot.decide(0xFFFF_FFFFu32 as i32); assert_eq!(slot.resolve(0) as u32,0xFFFF_FFFF,"a u32 sidecar code round-trips");
    }
    #[test] fn queued_requests_never_cross_epoch_on_session_rebuild() {
        assert!(current_frame(Control::Frame{epoch:"old".into(),frame:json!({"cmd":"service.start","id":2})},"new").is_none());
        assert!(current_frame(Control::Frame{epoch:"new".into(),frame:json!({"cmd":"service.stop","id":2})},"new").is_some());
    }
    /// Records every quit-machine action; tests read `Exit` codes instead of exiting.
    #[derive(Default)] struct Recorder(Mutex<Vec<Action>>);
    impl lifecycle::Effects for Arc<Recorder> {
        fn page_connected(&self)->bool { false }
        fn perform(&self,_:&Arc<Lifecycle>,action:Action) { self.0.lock().unwrap().push(action); }
    }
    impl Recorder {
        fn exits(&self)->Vec<i32> { self.0.lock().unwrap().iter().filter_map(|a| if let Action::Exit(c)=a {Some(*c)} else {None}).collect() }
        /// The quit machine's own log codes (production writes them to shell.log via ShellEffects).
        fn logs(&self)->String { self.0.lock().unwrap().iter().filter_map(|a| if let Action::Log(c)=a {Some(c.clone())} else {None}).collect::<Vec<_>>().join("\n") }
    }
    struct Cleanup(PathBuf);
    impl Drop for Cleanup {fn drop(&mut self){let _=fs::remove_dir_all(&self.0);}}
    /// A scoped worker on a scripted fixture sidecar ("替身进程"), with a capture of the page Channel.
    struct Rig { _cleanup:Cleanup, data:PathBuf, state:PathBuf, recorder:Arc<Recorder>, shared:Shared, feed:Arc<tray::Feed>,
        hub:Arc<Mutex<Hub>>, captured:Arc<Mutex<Vec<Value>>>, started:io::Result<(Bridge,mpsc::Receiver<()>)> }
    fn repo()->PathBuf { PathBuf::from(env!("CARGO_MANIFEST_DIR")).parent().unwrap().parent().unwrap().to_owned() }
    fn base_python()->PathBuf {
        let out=std::process::Command::new(repo().join(".venv/Scripts/python.exe"))
            .creation_flags(0x08000000).args(["-B","-c","import sys; print(sys._base_executable)"]).output().unwrap();
        assert!(out.status.success());
        PathBuf::from(String::from_utf8(out.stdout).unwrap().trim())
    }
    fn rig(script:&str, extra:&[&str], timing:Timing)->Rig {
        rig_with(script,extra,timing,|_|Arc::new(notify::tests::desk(None,None).0))
    }
    fn rig_with(script:&str, extra:&[&str], timing:Timing, desk:impl FnOnce(&std::path::Path)->Arc<notify::Desk>)->Rig {
        rig_shared(script,extra,timing,|data,recorder| Shared::new(Lifecycle::new(Box::new(recorder)),Arc::new(ChildSlot::default()),desk(data)))
    }
    /// The rig with a caller-built `Shared` (T-P3-06: stand-in host desktop or install platform).
    fn rig_shared(script:&str, extra:&[&str], timing:Timing, make:impl FnOnce(&std::path::Path,Arc<Recorder>)->Shared)->Rig {
        rig_full(script,extra,timing,|_,_|{},make)
    }
    /// `tweak` adjusts the options (e.g. `--autostart`) and may prepare the fixture state directory.
    fn rig_full(script:&str, extra:&[&str], timing:Timing, tweak:impl FnOnce(&mut Options,&std::path::Path),
        make:impl FnOnce(&std::path::Path,Arc<Recorder>)->Shared)->Rig {
        static NEXT:std::sync::atomic::AtomicU64=std::sync::atomic::AtomicU64::new(0);
        let root=env::temp_dir().join(format!("tesseract-worker-{}-{}-{}",std::process::id(),now(),NEXT.fetch_add(1,Ordering::Relaxed)));
        let config=root.join("config"); let data=root.join("data"); let state=root.join("state");
        fs::create_dir_all(&config).unwrap();fs::create_dir_all(&data).unwrap();
        let scope=bridge_host::scope::custom_id(&config.to_string_lossy(),&data.to_string_lossy()).unwrap();
        let mut child_args=vec!["-B".into(),repo().join(script).to_string_lossy().into_owned(),"--scope".into(),scope.clone()];
        child_args.extend(extra.iter().map(|s|s.to_string()));
        if script.ends_with("lifecycle_fixture.py") || script.ends_with("notify_fixture.py") || script.ends_with("host_fixture.py") { child_args.extend(["--state".into(),state.to_string_lossy().into_owned()]); }
        let mut options=Options{config,data:data.clone(),exe:base_python(),child_args,dev_page:false,browser_args:String::new(),verify_port:None,timing,autostart:false,install_roots:vec![],
            scope_kind:"custom",scope_id:scope.clone()};
        tweak(&mut options,&state);
        let feed=tray::Feed::new("en_US");
        let recorder=Arc::new(Recorder::default());
        let shared=make(&data,recorder.clone());
        let hub=Arc::new(Mutex::new(Hub::default()));
        let captured=Arc::new(Mutex::new(Vec::<Value>::new())); let output=captured.clone();
        hub.lock().unwrap().connect(Channel::new(move |body| {
            if let tauri::ipc::InvokeResponseBody::Json(s)=body {output.lock().unwrap().push(serde_json::from_str::<Value>(&s).unwrap()["frame"].clone());} Ok(())
        }));
        // 11-05: `run` no longer waits for hello; the rig does, with the same answer `watch_startup` reads.
        let started=start_worker(&options,feed.clone(),hub.clone(),shared.clone()).and_then(|(bridge,done,ready)|
            match startup_failure(ready.recv_timeout(STARTUP_LIMIT),true) { None=>Ok((bridge,done)), Some(error)=>Err(io::Error::other(error)) });
        Rig{_cleanup:Cleanup(root),data,state,recorder,shared,feed,hub,captured,started}
    }
    fn wait(label:&str, seconds:u64, mut ok:impl FnMut()->bool) {
        let deadline=Instant::now()+Duration::from_secs(seconds);
        while !ok() { assert!(Instant::now()<deadline,"timeout: {label}"); std::thread::sleep(Duration::from_millis(20)); }
    }
    impl Rig {
        fn log(&self)->String { fs::read_to_string(self.data.join("logs/shell.log")).unwrap_or_default() }
        fn frames(&self)->Vec<Value> { self.captured.lock().unwrap().clone() }
        fn banners(&self,kind:&str)->Vec<Value> { self.frames().into_iter().filter(|f|f["t"]=="host:banner" && f["kind"]==kind).collect() }
        fn received(&self,launch:u32)->Vec<Value> {
            fs::read_to_string(self.state.join(format!("frames-{launch}.jsonl"))).unwrap_or_default().lines().map(|l|serde_json::from_str(l).unwrap()).collect()
        }
        fn launches(&self)->usize { fs::read_dir(&self.state).map(|d|d.filter(|e|e.as_ref().unwrap().file_name().to_string_lossy().starts_with("launch-")).count()).unwrap_or(0) }
        fn done(&self) { self.started.as_ref().unwrap().1.recv_timeout(Duration::from_secs(20)).unwrap(); }
    }
    const FAST:Timing=Timing{pong_timeout:Duration::from_secs(15),restart_delay:Duration::from_millis(50)};

    #[test] fn worker_survives_legal_bad_legal_frames_in_same_session() {
        let rig=rig("shell/app/frame_fixture.py",&[],Timing::default());
        rig.started.as_ref().unwrap();
        wait("rev 2 delivered",5,|| rig.frames().iter().any(|f|f["rev"]==2));
        let ready=rig.hub.lock().unwrap().ready; let epoch=rig.hub.lock().unwrap().epoch.clone();
        rig.shared.lifecycle.request(QuitReason::Tray); rig.done();
        assert!(ready); assert_eq!(epoch,"fault-fixture");
        assert!(rig.frames().iter().any(|f|f["t"]=="bye"));
        let log=rig.log();
        assert!(log.contains("missing_field")); assert!(!log.contains("bridge_worker_failed"));
        assert_eq!(rig.recorder.exits(),vec![0],"bye then exit 0: the shell leaves with the sidecar's code");
        // The fixture's service.state DTOs are not tray facts, and a finished session reads offline.
        assert_eq!(rig.feed.status().tooltip(Instant::now()),"Tesseract\nCurrent tier: offline\nProxy core stopped");
    }
    #[test] fn quit_sends_app_quit_and_exits_with_the_sidecar_code_after_bye() {
        let rig=rig("shell/app/src/lifecycle_fixture.py",&["--mode","quit"],FAST);
        rig.started.as_ref().unwrap();
        wait("snapshot",10,|| rig.frames().iter().any(|f|f["topic"]=="service.state"));
        rig.shared.lifecycle.request(QuitReason::Tray);
        assert!(!rig.shared.lifecycle.accepting(),"page commands are refused once app.quit is queued");
        rig.done();
        let received=rig.received(1);
        let quit=received.iter().find(|f|f["cmd"]=="app.quit").expect("app.quit reached the sidecar");
        assert_eq!((quit["from"].as_str(),quit["args"]["reason"].as_str()),(Some("shell"),Some("user")));
        assert_eq!(rig.recorder.exits(),vec![0]);
        let frames=rig.frames();
        assert!(frames.iter().any(|f|f["t"]=="bye"),"bye still reaches the page");
        assert!(!frames.iter().any(|f|f["t"]=="ack"),"the shell's own app.quit ack stays in the shell");
        let log=rig.log();
        for code in ["quit_sent reason=user","bye exit_code=0 proxy=not_needed proxy_error=scope_isolated firewall=not_needed","sidecar_exit_0"] {
            assert!(log.contains(code),"{code} missing in\n{log}");
        }
        let machine=rig.recorder.logs();
        for code in ["quit_requested_tray","quit_confirm_skipped_no_page","quit_send_tray","quit_bye_received","quit_exit_0"] {
            assert!(machine.contains(code),"{code} missing in\n{machine}");
        }
        assert_eq!(rig.launches(),1);
    }
    #[test] fn session_end_goes_straight_to_the_pipe_and_reports() {
        let rig=rig("shell/app/src/lifecycle_fixture.py",&["--mode","session_end"],FAST);
        rig.started.as_ref().unwrap();
        wait("snapshot",10,|| rig.frames().iter().any(|f|f["topic"]=="service.state"));
        let ctx=endsession::Context::new(rig.shared.lifecycle.clone(),rig.shared.session_end.clone(),rig.data.clone(),Box::new(||"en_US"));
        assert!(endsession::start(&ctx,"logoff",Instant::now()));
        let outcome=rig.shared.session_end.wait(Instant::now()+Duration::from_millis(endsession::BUDGET_MS.into()));
        assert!(outcome.sent && !outcome.timed_out,"{outcome:?}");
        assert_eq!(outcome.reply.as_ref().unwrap()["data"]["writes_revoked"],true);
        rig.done();
        let received=rig.received(1);
        assert_eq!(received[0]["cmd"],"app.sessionEnd","the first command the sidecar sees is the session end");
        assert_eq!(received[0]["args"],json!({"reason":"logoff","budget_ms":4000}));
        assert_eq!((received[0]["owner"].as_str(),received[0]["from"].as_str()),(Some("shell"),Some("shell")));
        assert!(!received.iter().any(|f|f["cmd"]=="app.quit"),"never app.quit for a session end");
        assert!(!rig.frames().iter().any(|f|f["t"]=="res"),"the session-end reply is not shown to the page");
        assert_eq!(rig.recorder.exits(),vec![0]);
        let log=rig.log();
        for code in ["session_end_requested reason=logoff budget_ms=4000","session_end_sent reason=logoff budget_ms=4000",
            "session_end_reply sent=true reply=ok writes_revoked=true proxy=not_needed firewall=not_needed workers_pending=false"] {
            assert!(log.contains(code),"{code} missing in\n{log}");
        }
    }
    #[test] fn session_end_after_a_stuck_quit_is_still_sent() {
        // T-P3-05 item 5: app.quit is acked but no bye comes; a logoff must still send app.sessionEnd.
        let rig=rig("shell/app/src/lifecycle_fixture.py",&["--mode","quit_stuck"],FAST);
        rig.started.as_ref().unwrap();
        wait("snapshot",10,|| rig.frames().iter().any(|f|f["topic"]=="service.state"));
        rig.shared.lifecycle.request(QuitReason::Tray);
        wait("app.quit received",10,|| rig.received(1).iter().any(|f|f["cmd"]=="app.quit"));
        let ctx=endsession::Context::new(rig.shared.lifecycle.clone(),rig.shared.session_end.clone(),rig.data.clone(),Box::new(||"en_US"));
        assert!(endsession::start(&ctx,"logoff",Instant::now()),"quit sent, bye missing: the session end goes out");
        let outcome=rig.shared.session_end.wait(Instant::now()+Duration::from_millis(endsession::BUDGET_MS.into()));
        assert!(outcome.sent && !outcome.timed_out,"{outcome:?}");
        rig.done();
        let commands:Vec<String>=rig.received(1).iter().filter_map(|f|f["cmd"].as_str().map(String::from)).collect();
        assert_eq!(commands,["app.quit","app.sessionEnd"]);
        assert_eq!(rig.recorder.exits(),vec![0]);
        let log=rig.log();
        for code in ["quit_sent reason=user","session_end_requested reason=logoff","session_end_sent reason=logoff budget_ms=4000","session_end_reply sent=true reply=ok"] {
            assert!(log.contains(code),"{code} missing in\n{log}");
        }
        assert!(rig.recorder.logs().contains("session_end_during_quit_tray"));
    }
    fn push(rig:&Rig, name:&str, spec:Value) {
        let dir=rig.state.join("push"); fs::create_dir_all(&dir).unwrap();
        let tmp=dir.join(format!("{name}.tmp")); fs::write(&tmp,serde_json::to_vec(&spec).unwrap()).unwrap();
        fs::rename(&tmp,dir.join(format!("{name}.json"))).unwrap();
    }
    fn desktop(id:&str)->Value {
        json!({"topic":"alerts.desktop","kind":"edge","data":{"delivery_id":id,"event_ids":["e"],"severity":"error","title":"Core stopped","detail":"Mihomo exited","members":["e"],"target":"alerts"}})
    }
    fn receipts(rig:&Rig)->Vec<Value> { rig.received(1).into_iter().filter(|f|f["cmd"]=="alerts.desktopDelivered").map(|f|f["args"].clone()).collect() }
    fn presences(rig:&Rig)->Vec<Value> { rig.received(1).into_iter().filter(|f|f["cmd"]=="ui.presence").map(|f|f["args"].clone()).collect() }
    #[test] fn shell_topics_route_notices_report_presence_and_switch_language() {
        use presence::Window;
        let parts=Arc::new(Mutex::new(None)); let slot=parts.clone();
        let rig=rig_with("shell/app/src/notify_fixture.py",&[],FAST,move |data| {
            let (desk,probe,_tray,balloons)=notify::tests::desk_at(Some(Window{visible:true,focused:true}),None,Some(data.to_owned()));
            let desk=Arc::new(desk); *slot.lock().unwrap()=Some((desk.clone(),probe,balloons)); desk
        });
        let (desk,probe,balloons)=parts.lock().unwrap().take().unwrap();
        rig.started.as_ref().unwrap();
        wait("fixture ready",10,|| rig.state.join("ready").exists());
        // Subscription follows hello.caps; hello.lang reaches the page and the native text.
        let subscribe=rig.received(1).into_iter().find(|f|f["t"]=="subscribe").unwrap();
        assert_eq!(subscribe["topics"].as_array().unwrap().iter().rev().take(2).collect::<Vec<_>>(),[&json!("ui.language"),&json!("alerts.desktop")]);
        wait("host:language",5,|| rig.frames().iter().any(|f|f["t"]=="host:language" && f["language"]=="zh_CN"));
        // Presence: the full value at once, then only changes; the page id comes from host_set_page.
        wait("first presence",5,|| presences(&rig).contains(&json!({"visible":true,"focused":true,"page":"none"})));
        let generation=rig.hub.lock().unwrap().page_generation();
        desk.page.set("nodes",generation).unwrap();
        wait("page presence",5,|| presences(&rig).contains(&json!({"visible":true,"focused":true,"page":"nodes"})));
        // In front with a page that has not declared host_notify_ready: the balloon (no tray here).
        push(&rig,"00",desktop("desktop-w0"));
        wait("unready receipt",5,|| receipts(&rig).contains(&json!({"delivery_id":"desktop-w0","outcome":"dropped","surface":"none"})));
        assert!(!rig.frames().iter().any(|f|f["t"]=="host:notify"),"an undeclared page gets no host:notify");
        desk.notify_ready(generation);
        // In front with a ready page: in-app; the page's confirmation becomes delivered/in_app.
        push(&rig,"01",desktop("desktop-w1"));
        wait("host:notify",5,|| rig.frames().iter().any(|f|f["t"]=="host:notify" && f["delivery_id"]=="desktop-w1"));
        assert!(desk.rendered("desktop-w1",rig.hub.lock().unwrap().page_generation()).is_ok());
        wait("in-app receipt",5,|| receipts(&rig).contains(&json!({"delivery_id":"desktop-w1","outcome":"delivered","surface":"in_app"})));
        // Forced test in front: a balloon is tried; no tray in this test, so dropped/none.
        push(&rig,"02",desktop("desktop-test-w2"));
        wait("forced receipt",5,|| receipts(&rig).contains(&json!({"delivery_id":"desktop-test-w2","outcome":"dropped","surface":"none"})));
        // The page stays silent: dropped/none after 3 s.
        push(&rig,"03",desktop("desktop-w3"));
        wait("timeout receipt",8,|| receipts(&rig).contains(&json!({"delivery_id":"desktop-w3","outcome":"dropped","surface":"none"})));
        // Hidden window: presence reports it, and the notice tries the balloon.
        *probe.0.lock().unwrap()=Some(Window{visible:false,focused:false});
        wait("hidden presence",5,|| presences(&rig).contains(&json!({"visible":false,"focused":false,"page":"nodes"})));
        push(&rig,"04",desktop("desktop-w4"));
        wait("background receipt",5,|| receipts(&rig).iter().any(|r|r["delivery_id"]=="desktop-w4"));
        // A repeated id is ignored; a language change inside a batch updates tray and page.
        push(&rig,"05",desktop("desktop-w1"));
        push(&rig,"06",json!({"batch":[{"topic":"service.state","kind":"snapshot","data":{"fixture":2}},
            {"topic":"ui.language","kind":"edge","data":{"language":"ja_JP","config_rev":2}}]}));
        wait("host:language ja_JP",5,|| rig.frames().iter().any(|f|f["t"]=="host:language" && f["language"]=="ja_JP"));
        let batch=rig.frames().into_iter().find(|f|f["t"]=="batch").expect("the rest of the batch reaches the page");
        assert_eq!(batch["items"].as_array().unwrap().iter().map(|i|(i["topic"].as_str().unwrap(),i["rev"].as_u64().unwrap())).collect::<Vec<_>>(),[("service.state",2)]);
        assert_eq!(rig.feed.status().menu(),["Tesseract を表示","終了"],"tray menu follows ui.language");
        std::thread::sleep(Duration::from_millis(300));
        rig.shared.lifecycle.request(QuitReason::Tray); rig.done();
        assert_eq!(rig.recorder.exits(),vec![0]);
        let all=receipts(&rig);
        for id in ["desktop-w0","desktop-w1","desktop-test-w2","desktop-w3","desktop-w4"] {
            assert_eq!(all.iter().filter(|r|r["delivery_id"]==id).count(),1,"{id}: exactly one receipt in {all:?}");
        }
        assert_eq!(*balloons.lock().unwrap(),["desktop-w0","desktop-test-w2","desktop-w4"]);
        assert_eq!(rig.frames().iter().filter(|f|f["t"]=="host:language" && f["language"]=="zh_CN").count(),1,
            "hello.lang and the first ui.language snapshot are the same value: published once");
        let frames=rig.frames();
        let shell_topic=|f:&Value| f["topic"]=="alerts.desktop" || f["topic"]=="ui.language";
        assert!(!frames.iter().any(|f|shell_topic(f) || f["items"].as_array().is_some_and(|i|i.iter().any(shell_topic))),"shell topics never reach the page");
        assert!(!frames.iter().any(|f|f["t"]=="res"),"replies to shell reports stay in the shell");
        let log=rig.log();
        for code in ["language_applied lang=zh_CN source=hello","presence_sent visible=true focused=true page=none focus_only=0",
            "notify_route delivery=desktop-w0 route=no_surface force=false visible=true focused=true severity=error target=alerts page=unready",
            "notify_route delivery=desktop-w1 route=in_app force=false visible=true focused=true severity=error target=alerts page=ready",
            "notify_receipt_sent delivery=desktop-w1 outcome=delivered surface=in_app",
            "notify_route delivery=desktop-test-w2 route=no_surface force=true","notify_receipt_reply ok=true recorded=true duplicate=false",
            "notify_route delivery=desktop-w1 route=duplicate","language_applied lang=ja_JP source=topic"] {
            assert!(log.contains(code),"{code} missing in\n{log}");
        }
        assert!(!log.contains("language_applied lang=zh_CN source=topic"),"the unchanged snapshot is not applied twice");
    }
    #[test] fn presence_is_reported_in_full_again_after_a_crash_restart() {
        use presence::Window;
        let rig=rig_with("shell/app/src/notify_fixture.py",&["--mode","crash_once"],FAST,|data| {
            Arc::new(notify::tests::desk_at(Some(Window{visible:true,focused:false}),None,Some(data.to_owned())).0)
        });
        rig.started.as_ref().unwrap();
        let shown=json!({"visible":true,"focused":false,"page":"none"});
        let presences=|launch:u32| rig.received(launch).into_iter().filter(|f|f["cmd"]=="ui.presence").map(|f|f["args"].clone()).collect::<Vec<_>>();
        wait("relaunched",15,|| rig.hub.lock().unwrap().epoch=="notify-2" && rig.hub.lock().unwrap().ready);
        assert_eq!(presences(1),[shown.clone()],"the first sidecar heard the value once");
        // Nothing changed in the window, yet the new session gets the full value again.
        wait("presence after the restart",5,|| presences(2)==[shown.clone()]);
        rig.shared.lifecycle.request(QuitReason::Tray); rig.done();
        assert_eq!(rig.recorder.exits(),vec![0]);
        assert_eq!(rig.launches(),2);
        let log=rig.log();
        assert!(log.contains("sidecar_crashed exit_code=3 restart=1"),"{log}");
        assert_eq!(log.matches("presence_sent visible=true focused=false page=none").count(),2,"one line per session");
    }
    #[test] fn a_foreign_shell_item_rejects_the_whole_batch() {
        let hub=Mutex::new(Hub::default()); let feed=tray::Feed::new("en_US");
        hub.lock().unwrap().reset(json!({"t":"hello","epoch":"one"}));
        let item=|topic:&str,epoch:&str,rev:u64| json!({"t":"evt","epoch":epoch,"topic":topic,"rev":rev,"seq":rev,"kind":"edge","produced_at_ms":1,"sent_at_ms":1,"data":{}});
        let batch=|items:Vec<Value>| json!({"t":"batch","epoch":"one","tick_seq":1,"items":items,"produced_at_ms":1,"sent_at_ms":1});
        assert_eq!(accept_frame(&hub,&feed,batch(vec![item("core.info","one",1),item("alerts.desktop","two",1)])),Err("epoch_mismatch"));
        // Nothing of the rejected frame was taken: the same tick and core.info revision still go in.
        let shell=accept_frame(&hub,&feed,batch(vec![item("core.info","one",1),item("alerts.desktop","one",1)])).unwrap();
        assert_eq!(shell.len(),1);
    }
    #[test] fn crash_restarts_in_stopped_state_with_a_new_epoch_and_banner() {
        let rig=rig("shell/app/src/lifecycle_fixture.py",&["--mode","crash_once"],FAST);
        rig.started.as_ref().unwrap();
        wait("relaunched",15,|| rig.hub.lock().unwrap().epoch=="lifecycle-2" && rig.hub.lock().unwrap().ready);
        wait("restarted banner",5,|| rig.banners("sidecar_crashed").iter().any(|b|b["state"]=="restarted"));
        let banners=rig.banners("sidecar_crashed");
        assert_eq!((banners[0]["state"].as_str(),banners[0]["exit_code"].as_u64(),banners[0]["restarts"].as_u64()),(Some("restarting"),Some(3),Some(1)));
        // S5: `restarted` is a one-shot notice; a reconnecting page is not shown the crash again.
        assert_eq!(banners.last().unwrap()["active"],false);
        let replayed=Arc::new(Mutex::new(Vec::<Value>::new())); let out=replayed.clone();
        rig.hub.lock().unwrap().connect(Channel::new(move |body| {
            if let tauri::ipc::InvokeResponseBody::Json(s)=body {out.lock().unwrap().push(serde_json::from_str::<Value>(&s).unwrap()["frame"].clone());} Ok(())
        }));
        assert!(replayed.lock().unwrap().iter().any(|f|f["t"]=="hello"),"the reconnect replay ran");
        assert!(!replayed.lock().unwrap().iter().any(|f|f["t"]=="host:banner"),"no crash banner replayed after a successful restart");
        let frames=rig.frames();
        let reset=frames.iter().position(|f|f["t"]=="rt:reset" && f["epoch"]=="lifecycle-2").expect("new epoch triggers rt:reset");
        assert!(frames[reset..].iter().any(|f|f["t"]=="hello" && f["epoch"]=="lifecycle-2"));
        assert!(rig.recorder.exits().is_empty(),"a crash is not a quit");
        // The relaunched sidecar is in the stopped state: no command was replayed into it.
        assert!(!rig.received(2).iter().any(|f|f["t"]=="req"));
        rig.shared.lifecycle.request(QuitReason::Tray); rig.done();
        assert_eq!(rig.recorder.exits(),vec![0]);
        assert!(rig.log().contains("sidecar_crashed exit_code=3 restart=1"));
        assert_eq!(rig.launches(),2);
    }
    #[test] fn restarts_stop_after_three_and_a_later_quit_exits_zero() {
        let rig=rig("shell/app/src/lifecycle_fixture.py",&["--mode","crash_always"],FAST);
        rig.started.as_ref().unwrap();
        rig.done();
        assert_eq!(rig.launches(),4,"the first launch plus three restarts");
        let banners=rig.banners("sidecar_crashed");
        assert_eq!(banners.last().unwrap()["state"],"unrecoverable");
        assert!(rig.log().contains("sidecar_crashed exit_code=3 restart=exhausted"));
        assert!(rig.recorder.exits().is_empty());
        rig.shared.lifecycle.request(QuitReason::Tray);
        assert_eq!(rig.recorder.exits(),vec![0],"no sidecar running: exit 0 at once");
    }
    #[test] fn exit_74_follows_the_shutdown_and_never_restarts() {
        let rig=rig("shell/app/src/lifecycle_fixture.py",&["--mode","exit74"],FAST);
        rig.started.as_ref().unwrap();
        rig.done();
        assert_eq!(rig.recorder.exits(),vec![74]);
        assert_eq!(rig.launches(),1);
        assert!(rig.banners("sidecar_crashed").is_empty());
        assert!(rig.recorder.logs().contains("quit_requested_sidecar_exit"));
    }
    #[test] fn exit_before_hello_is_a_startup_failure_not_a_restart() {
        let rig=rig("shell/app/src/lifecycle_fixture.py",&["--mode","prehello"],FAST);
        let error=rig.started.as_ref().err().expect("start fails").to_string();
        assert!(error.starts_with("sidecar_start_failed_"),"{error}");
        std::thread::sleep(Duration::from_millis(300));
        assert_eq!(rig.launches(),1,"the first launch failing before hello is never relaunched");
        assert!(rig.log().contains("sidecar_start_failed"));
    }
    #[test] fn pong_timeout_only_raises_a_banner_and_force_restart_relaunches() {
        let timing=Timing{pong_timeout:Duration::from_secs(3),restart_delay:Duration::from_millis(50)};
        let rig=rig("shell/app/src/lifecycle_fixture.py",&["--mode","silent_once"],timing);
        rig.started.as_ref().unwrap();
        wait("unresponsive banner",15,|| rig.banners("sidecar_unresponsive").iter().any(|b|b["active"]==true));
        std::thread::sleep(Duration::from_millis(500));
        // Old P3-01 behaviour sent app.quit here; now nothing is sent and nothing is killed.
        assert!(!rig.received(1).iter().any(|f|f["t"]=="req"));
        assert_eq!(rig.launches(),1); assert!(rig.recorder.exits().is_empty());
        assert!(rig.shared.unresponsive.load(Ordering::Acquire));
        rig.shared.force_restart.store(true,Ordering::Release);
        wait("relaunched",15,|| rig.hub.lock().unwrap().epoch=="lifecycle-2" && rig.hub.lock().unwrap().ready);
        assert!(rig.banners("sidecar_unresponsive").last().unwrap()["active"]==false,"the hang banner clears with the old session");
        rig.shared.lifecycle.request(QuitReason::Tray); rig.done();
        assert_eq!(rig.recorder.exits(),vec![0]);
        let log=rig.log();
        assert!(log.contains("pong_timeout") && log.contains("sidecar_force_restart") && log.contains(&format!("sidecar_crashed exit_code={} restart=1",crash::FORCE_RESTART_CODE)));
    }
    #[test] fn accepted_frames_feed_the_tray_and_rejected_frames_do_not() {
        let service:Value=serde_json::from_str(include_str!("../../../bridge_schema/fixtures/valid/topic_service_state.json")).unwrap();
        let tiers:Value=serde_json::from_str(include_str!("../../../bridge_schema/fixtures/valid/topic_tiers_summary.json")).unwrap();
        let epoch=service["epoch"].as_str().unwrap().to_owned();
        let hub=Mutex::new(Hub::default()); let feed=tray::Feed::new("en_US");
        let hello=json!({"t":"hello","proto":1,"epoch":epoch,"lang":"zh_CN"});
        hub.lock().unwrap().reset(hello.clone()); feed.hello(&hello);
        accept_frame(&hub,&feed,service.clone()).unwrap(); accept_frame(&hub,&feed,tiers.clone()).unwrap();
        assert_eq!(feed.status().tooltip(Instant::now()),"Tesseract\n当前层级：Vanguard\n代理核心 运行中");
        // A frame the hub rejects (foreign epoch, replayed revision) never reaches the tooltip.
        let mut foreign=tiers.clone(); foreign["epoch"]=json!("other"); foreign["rev"]=json!(2); foreign["data"]["carrier"]=json!("bastion");
        assert_eq!(accept_frame(&hub,&feed,foreign),Err("epoch_mismatch"));
        let mut replay=service.clone(); replay["data"]["core"]["running"]=json!(false);
        assert!(accept_frame(&hub,&feed,replay).is_err());
        assert_eq!(feed.status().tooltip(Instant::now()),"Tesseract\n当前层级：Vanguard\n代理核心 运行中");
    }
    /// T-P3-06: records like `Recorder` and really runs the install step (stand-in platform).
    /// `sync`: the step runs inside `perform`, so it is over before `sidecar_exited` returns —
    /// the worst case of the production thread (review M1).
    struct InstallEffects { recorder:Arc<Recorder>, update:Arc<update::Update>, sync:bool }
    impl lifecycle::Effects for InstallEffects {
        fn page_connected(&self)->bool { false }
        fn perform(&self, lc:&Arc<Lifecycle>, action:Action) {
            let install=action==Action::Install;
            self.recorder.0.lock().unwrap().push(action);
            if install && self.sync { let launched=self.update.install().is_ok(); lc.install_finished(launched); }
            else if install { run_install(lc,&self.update); }
        }
    }
    const HOST_FIXTURE:&str="shell/app/src/host_fixture.py";
    fn rig_update(fake:Arc<update::tests::Fake>)->Rig { rig_update_with(fake,false,|_|{}) }
    /// `prepare` may drop markers into the fixture state directory (e.g. `no_bye`).
    fn rig_update_with(fake:Arc<update::tests::Fake>, sync:bool, prepare:impl FnOnce(&std::path::Path))->Rig {
        rig_full(HOST_FIXTURE,&[],FAST,move |options,state| {
            // The residual roots must be known (an empty set fails the install closed).
            options.install_roots=vec![options.data.clone()];
            fs::create_dir_all(state).unwrap(); prepare(state);
        },move |_,recorder| {
            let update=update::Update::new(Box::new(fake));
            let lifecycle=Lifecycle::new(Box::new(InstallEffects{recorder,update:update.clone(),sync}));
            Shared::with(lifecycle,Arc::new(ChildSlot::default()),Arc::new(notify::tests::desk(None,None).0),host::Host::new(Box::new(hostwin::Win32Desktop)),update)
        })
    }
    fn sent(rig:&Rig, launch:u32, cmd:&str)->Vec<Value> { rig.received(launch).into_iter().filter(|f|f["cmd"]==cmd).collect() }
    fn handoff(rig:&Rig, sha:Option<&str>) {
        wait("fixture ready",10,|| rig.state.join("ready-1").exists());
        let (_,mut dto)=update::tests::installer(&rig.data,b"installer bytes");
        if let Some(sha)=sha { dto["sha256"]=json!(sha); }
        push(rig,"00",json!({"topic":"update.prepareExit","kind":"edge","data":dto}));
    }
    #[test] fn update_handoff_quits_for_the_update_then_starts_the_installer() {
        let fake=Arc::new(update::tests::Fake::new());
        let rig=rig_update(fake.clone());
        rig.started.as_ref().unwrap();
        let subscribe=|| rig.received(1).into_iter().find(|f|f["t"]=="subscribe");
        wait("subscribe",10,|| subscribe().is_some());
        assert!(subscribe().unwrap()["topics"].as_array().unwrap().contains(&json!("update.prepareExit")),"declared: subscribed");
        handoff(&rig,None);
        rig.done();
        let response=sent(&rig,1,"update.exitResponse");
        assert_eq!(response[0]["args"],json!({"handoff_id":"update-handoff-1","accepted":true}));
        assert_eq!((response[0]["from"].as_str(),response[0]["owner"].as_str()),(Some("shell"),Some("shell")));
        let commands:Vec<String>=rig.received(1).iter().filter_map(|f|f["cmd"].as_str().map(String::from)).collect();
        assert_eq!(commands,["update.exitResponse","app.quit"],"the shell sends app.quit itself after the recorded acceptance");
        assert_eq!(sent(&rig,1,"app.quit")[0]["args"],json!({"reason":"update"}));
        assert_eq!(fake.launches.lock().unwrap().len(),1,"the installer started once");
        assert_eq!(rig.recorder.exits(),vec![0]);
        assert_eq!(rig.launches(),1,"no relaunch after a launched installer");
        assert!(!rig.frames().iter().any(|f|f["topic"]=="update.prepareExit" || f["t"]=="res"),"the hand-off never reaches the page");
        let log=rig.log();
        for code in ["update_handoff_received handoff=update-handoff-1 version=9.9.9","update_exit_response handoff=update-handoff-1 accepted=true",
            "update_quit_requested","quit_sent reason=update","update_installer_launched handoff=update-handoff-1 version=9.9.9"] {
            assert!(log.contains(code),"{code} missing in\n{log}");
        }
        assert!(rig.recorder.logs().contains("update_install_begin"));
    }
    #[test] fn failed_install_relaunches_stopped_and_reports_install_result() {
        let fake=Arc::new(update::tests::Fake{fail_launch:true,..update::tests::Fake::new()});
        let rig=rig_update(fake.clone());
        rig.started.as_ref().unwrap();
        handoff(&rig,None);
        wait("relaunched",20,|| rig.hub.lock().unwrap().epoch=="host-2" && rig.hub.lock().unwrap().ready);
        wait("installResult",10,|| !sent(&rig,2,"update.installResult").is_empty());
        let result=&sent(&rig,2,"update.installResult")[0];
        assert_eq!(result["args"],json!({"receipt_ref":"receipt-1","launched":false,"reason_code":"launch_failed"}));
        assert!(!rig.received(2).iter().any(|f|f["cmd"]=="service.start"),"the relaunched sidecar stays stopped");
        assert!(rig.recorder.exits().is_empty(),"a failed install does not exit");
        let actions=rig.recorder.0.lock().unwrap().clone();
        assert!(actions.contains(&Action::ClearQuitting) && actions.contains(&Action::RestoreWindow));
        assert!(rig.shared.lifecycle.accepting(),"back to Running: close hides to the tray again");
        rig.shared.lifecycle.request(QuitReason::Tray); rig.done();
        assert_eq!(rig.recorder.exits(),vec![0]);
        assert_eq!(rig.launches(),2);
        let log=rig.log();
        for code in ["update_install_failed handoff=update-handoff-1 version=9.9.9 reason=launch_failed","update_relaunch_stopped",
            "update_install_result_sent reason=launch_failed","update_install_result_reply ok=true"] {
            assert!(log.contains(code),"{code} missing in\n{log}");
        }
    }
    /// Review M1: an install step that fails at once (no bye: bye_missing) is over before the
    /// worker looks; the worker must still relaunch the sidecar stopped and report installResult.
    #[test] fn immediate_install_failure_still_relaunches_and_reports_install_result() {
        let fake=Arc::new(update::tests::Fake::new());
        let rig=rig_update_with(fake.clone(),true,|state| fs::write(state.join("no_bye"),b"1").unwrap());
        rig.started.as_ref().unwrap();
        handoff(&rig,None);
        wait("relaunched",20,|| rig.hub.lock().unwrap().epoch=="host-2" && rig.hub.lock().unwrap().ready);
        wait("installResult",10,|| !sent(&rig,2,"update.installResult").is_empty());
        assert_eq!(sent(&rig,2,"update.installResult")[0]["args"],json!({"receipt_ref":"receipt-1","launched":false,"reason_code":"bye_missing"}));
        assert!(fake.launches.lock().unwrap().is_empty(),"nothing launched without bye");
        assert!(!rig.received(2).iter().any(|f|f["cmd"]=="service.start"),"the relaunched sidecar stays stopped");
        assert!(rig.recorder.exits().is_empty(),"a failed install does not exit");
        assert!(rig.shared.lifecycle.accepting(),"back to Running");
        rig.shared.lifecycle.request(QuitReason::Tray); rig.done();
        assert_eq!(rig.recorder.exits(),vec![0]);
        assert_eq!(rig.launches(),2);
        let log=rig.log();
        for code in ["update_install_failed handoff=update-handoff-1 version=9.9.9 reason=bye_missing","update_relaunch_stopped",
            "update_install_result_sent reason=bye_missing"] {
            assert!(log.contains(code),"{code} missing in\n{log}");
        }
    }
    #[test] fn hash_mismatch_declines_and_keeps_running() {
        let fake=Arc::new(update::tests::Fake::new());
        let rig=rig_update(fake.clone());
        rig.started.as_ref().unwrap();
        handoff(&rig,Some(&"0".repeat(64)));
        wait("declined",10,|| !sent(&rig,1,"update.exitResponse").is_empty());
        assert_eq!(sent(&rig,1,"update.exitResponse")[0]["args"],json!({"handoff_id":"update-handoff-1","accepted":false,"reason_code":"hash_mismatch"}));
        std::thread::sleep(Duration::from_millis(300));
        assert!(sent(&rig,1,"app.quit").is_empty() && rig.shared.lifecycle.accepting());
        rig.shared.lifecycle.request(QuitReason::Tray); rig.done();
        assert!(fake.launches.lock().unwrap().is_empty());
        assert_eq!(rig.recorder.exits(),vec![0]);
    }
    #[test] fn save_grant_duplicates_a_real_handle_into_the_sidecar_and_reveal_follows_the_export() {
        let desktop=Arc::new(host::tests::FakeDesktop::default());
        desktop.real.store(true,Ordering::SeqCst);
        let made=desktop.clone();
        let rig=rig_shared(HOST_FIXTURE,&[],FAST,move |_,recorder| {
            Shared::with(Lifecycle::new(Box::new(recorder)),Arc::new(ChildSlot::default()),Arc::new(notify::tests::desk(None,None).0),
                host::Host::new(Box::new(made)),update::Update::new(Box::new(update::Win32Platform)))
        });
        rig.started.as_ref().unwrap();
        wait("fixture ready",10,|| rig.state.join("ready-1").exists());
        let target=rig.data.join("export").join("diag.zip"); fs::create_dir_all(target.parent().unwrap()).unwrap();
        *desktop.answer.lock().unwrap()=Some(target.clone());
        let request=|id:u64,req:&str,cmd:&str,args:Value| host::Request{id,req_id:req.into(),owner:"page-1".into(),cmd:cmd.into(),args};
        let reply=|req:&str| rig.frames().into_iter().find(|f|f["t"]=="res" && f["req_id"]==req);
        rig.shared.host.submit(request(900,"save-1","host.saveFile",json!({"purpose":"bundle","format":"zip"}))).unwrap();
        wait("saveFile res",10,|| reply("save-1").is_some());
        let res=reply("save-1").unwrap();
        assert_eq!((res["ok"].as_bool(),res["data"]["cancelled"].as_bool(),res["epoch"].as_str()),(Some(true),Some(false),Some("host-1")),"{res}");
        let reference=res["data"]["ref"].as_str().unwrap().to_owned();
        assert!(!res.to_string().contains("diag.zip"),"no path reaches the page");
        let grant:Value=serde_json::from_str(fs::read_to_string(rig.state.join("grants.jsonl")).unwrap().lines().next().unwrap()).unwrap();
        assert_eq!(grant["file_type"],1,"a disk-file handle valid inside the sidecar");
        assert_eq!(grant["written"],20);
        assert_eq!(PathBuf::from(grant["path"].as_str().unwrap()).canonicalize().unwrap(),target.canonicalize().unwrap());
        assert_eq!((grant["args"]["ref"].as_str(),grant["args"]["purpose"].as_str(),grant["args"]["format"].as_str()),(Some(reference.as_str()),Some("bundle"),Some("zip")));
        assert_eq!(fs::read(&target).unwrap(),b"host-fixture-export\n","written through the duplicated handle");
        let request_frame=sent(&rig,1,"shell.saveGrant").remove(0);
        assert_eq!((request_frame["from"].as_str(),request_frame["owner"].as_str(),request_frame["args"]["for_owner"].as_str()),(Some("shell"),Some("shell"),Some("page-1")));
        // Reveal: refused until the export job reported the ref, then explorer /select (stand-in).
        rig.shared.host.submit(request(901,"reveal-1","host.reveal",json!({"ref":reference}))).unwrap();
        wait("reveal refused",10,|| reply("reveal-1").is_some());
        assert_eq!(reply("reveal-1").unwrap()["error"]["code"],"not_allowed");
        push(&rig,"01",json!({"topic":"jobs","kind":"edge","data":{"op":"export-op","name":"logs.exportBundle","state":"done","owner":"page-1","started_at_ms":1,
            "result":{"ref":reference,"size":20,"files":[]}}}));
        wait("export edge",10,|| rig.frames().iter().any(|f|f["topic"]=="jobs"));
        rig.shared.host.submit(request(902,"reveal-2","host.reveal",json!({"ref":reference}))).unwrap();
        wait("reveal",10,|| reply("reveal-2").is_some());
        assert_eq!(reply("reveal-2").unwrap()["data"],json!({"revealed":true}));
        assert!(desktop.calls.lock().unwrap().iter().any(|c|c.starts_with("reveal ") && c.ends_with("diag.zip")));
        rig.shared.lifecycle.request(QuitReason::Tray); rig.done();
        assert_eq!(rig.recorder.exits(),vec![0]);
    }
    #[test] fn autostart_reads_the_tray_setting_once() {
        assert!(autostart_tray(&json!({"t":"res","ok":true,"data":{"values":{"ui":{"start_in_tray_on_autostart":true}}}})));
        for reply in [json!({"t":"res","ok":true,"data":{"values":{"ui":{}}}}),json!({"t":"res","ok":true,"data":{"values":{}}}),
            json!({"t":"res","ok":false,"error":{"code":"internal"}}),json!({"t":"res","ok":true,"data":{"values":{"ui":{"start_in_tray_on_autostart":"true"}}}})] {
            assert!(!autostart_tray(&reply),"{reply}");
        }
        assert!(autostart_hidden(Some(true),true));
        assert!(!autostart_hidden(Some(true),false),"no tray: the window must show");
        assert!(!autostart_hidden(Some(false),true) && !autostart_hidden(None,true));
        let slot=Autostart::default();
        assert_eq!(slot.wait(Duration::from_millis(10)),None);
        slot.decide(true); slot.decide(false);
        assert_eq!(slot.wait(Duration::from_millis(10)),Some(true),"the first answer wins");
    }
    #[test] fn autostart_queries_config_snapshot_and_follows_the_setting() {
        for tray in [true,false] {
            let rig=rig_autostart(tray);
            rig.started.as_ref().unwrap();
            assert_eq!(rig.shared.autostart.wait(Duration::from_secs(10)),Some(tray));
            let query=sent(&rig,1,"config.snapshot").remove(0);
            assert_eq!((query["from"].as_str(),query["owner"].as_str(),&query["args"]),(Some("shell"),Some("shell"),&json!({})));
            assert!(!rig.frames().iter().any(|f|f["t"]=="res"),"the config reply stays in the shell");
            assert!(rig.log().contains(&format!("autostart_setting ok=true tray={tray}")));
            rig.shared.lifecycle.request(QuitReason::Tray); rig.done();
        }
    }
    fn rig_autostart(tray:bool)->Rig {
        rig_full(HOST_FIXTURE,&[],FAST,move |options,state| {
            options.autostart=true;
            if tray { fs::create_dir_all(state).unwrap(); fs::write(state.join("tray"),b"1").unwrap(); }
        },|_,recorder| Shared::new(Lifecycle::new(Box::new(recorder)),Arc::new(ChildSlot::default()),Arc::new(notify::tests::desk(None,None).0)))
    }

    /// T-P6-06: the scope decision, the sidecar choice, the hello check and `--print-scope` on an
    /// injected environment (never the process's own; nothing here touches %APPDATA%\Tesseract).
    mod scope_decision {
        use super::super::*;
        use std::collections::HashMap;
        struct Root(PathBuf);
        impl Drop for Root { fn drop(&mut self) { let _=fs::remove_dir_all(&self.0); } }
        fn root(tag:&str)->Root {
            static NEXT:std::sync::atomic::AtomicU64=std::sync::atomic::AtomicU64::new(0);
            let path=env::temp_dir().join(format!("tesseract-scope-{tag}-{}-{}-{}",std::process::id(),now(),NEXT.fetch_add(1,Ordering::Relaxed)));
            fs::create_dir_all(&path).unwrap();
            Root(plain_path(fs::canonicalize(&path).unwrap()))
        }
        fn fake(pairs:&[(&str,&std::path::Path)])->impl Fn(&str)->Option<OsString> {
            let map:HashMap<String,OsString>=pairs.iter().map(|(k,v)|(k.to_string(),v.as_os_str().to_owned())).collect();
            move |name| map.get(name).cloned()
        }
        fn code(result:io::Result<Scope>)->String { result.unwrap_err().to_string() }
        fn legacy()->Value { bridge_host::scope::names("default","default").unwrap() }
        #[test] fn default_scope_only_in_a_distribution_build() {
            let r=root("default");
            let (roaming,local)=(r.0.join("Roaming"),r.0.join("Local"));
            let env=fake(&[("APPDATA",&roaming),("LOCALAPPDATA",&local),("USERPROFILE",&r.0)]);
            let scope=resolve_scope(&env,true).unwrap();
            assert_eq!(scope,Scope{kind:"default",id:"default".into(),config:roaming.join("Tesseract"),data:local.join("Tesseract")});
            assert!(!roaming.exists() && !local.exists(),"the decision creates no directory");
            assert_eq!(bridge_host::scope::names(scope.kind,&scope.id).unwrap()["shell_mutex"],"Local\\TesseractShell");
            assert_eq!(legacy()["legacy_mutex"],"Local\\TesseractProxyManagerSingleInstance");
            // §9.8: the WebView2 user data folder lives in the data directory, under the scope id.
            assert_eq!(single_instance::profile(&scope.data,&scope.id,false),local.join("Tesseract").join("WebView2").join("default"));
            assert_eq!(code(resolve_scope(&env,false)),"explicit_custom_scope_required","a development build never takes the default scope");
            // Existing directories resolve to the same spelling.
            fs::create_dir_all(roaming.join("Tesseract")).unwrap(); fs::create_dir_all(local.join("Tesseract")).unwrap();
            assert_eq!(resolve_scope(&env,true).unwrap(),scope);
        }
        #[test] fn default_directories_fall_back_like_app_paths() {
            let r=root("fallback");
            let home=r.0.join("home");
            for empty in [false,true] {
                let blank=std::path::Path::new("");
                let env=if empty { fake(&[("APPDATA",blank),("LOCALAPPDATA",blank),("USERPROFILE",&home)]) } else { fake(&[("USERPROFILE",&home)]) };
                let scope=resolve_scope(&env,true).unwrap();
                assert_eq!((scope.config,scope.data),(home.join("AppData\\Roaming\\Tesseract"),home.join("AppData\\Local\\Tesseract")),"empty={empty}");
            }
            // No USERPROFILE: HOMEDRIVE + HOMEPATH (Python's ntpath.expanduser).
            let text=home.to_string_lossy().into_owned();
            let (drive,path)=text.split_at(2);
            let env=fake(&[("HOMEDRIVE",std::path::Path::new(drive)),("HOMEPATH",std::path::Path::new(path))]);
            assert_eq!(resolve_scope(&env,true).unwrap().data,home.join("AppData\\Local\\Tesseract"));
            // One variable set, the other falling back.
            let env=fake(&[("APPDATA",&r.0.join("R")),("USERPROFILE",&home)]);
            let scope=resolve_scope(&env,true).unwrap();
            assert_eq!((scope.config,scope.data),(r.0.join("R\\Tesseract"),home.join("AppData\\Local\\Tesseract")));
            assert_eq!(code(resolve_scope(&fake(&[]),true)),"user_profile_missing");
            assert_eq!(code(resolve_scope(&fake(&[("APPDATA",std::path::Path::new("relative")),("USERPROFILE",&home)]),true)),"default_directory_invalid");
            assert!(!home.exists(),"no directory created");
        }
        #[test] fn custom_scope_is_unchanged_in_both_builds() {
            let r=root("custom");
            let (config,data)=(r.0.join("config"),r.0.join("data"));
            fs::create_dir_all(&config).unwrap(); fs::create_dir_all(&data).unwrap();
            for distribution in [false,true] {
                let env=fake(&[("TESSERACT_INSTANCE_SCOPE",std::path::Path::new("p6-test")),("TESSERACT_CONFIG_DIR",&config),("TESSERACT_DATA_DIR",&data),
                    ("APPDATA",&r.0.join("Roaming")),("LOCALAPPDATA",&r.0.join("Local"))]);
                let scope=resolve_scope(&env,distribution).unwrap();
                assert_eq!(scope.kind,"custom");
                assert_eq!(scope.id,bridge_host::scope::custom_id(&config.to_string_lossy(),&data.to_string_lossy()).unwrap());
                assert_eq!(fs::canonicalize(&scope.config).unwrap(),fs::canonicalize(&config).unwrap());
                // Blank label, missing or relative directory: refused as before.
                let blank=fake(&[("TESSERACT_INSTANCE_SCOPE",std::path::Path::new("  ")),("TESSERACT_CONFIG_DIR",&config),("TESSERACT_DATA_DIR",&data)]);
                assert_eq!(code(resolve_scope(&blank,distribution)),"explicit_custom_scope_required");
                let missing=fake(&[("TESSERACT_INSTANCE_SCOPE",std::path::Path::new("x")),("TESSERACT_CONFIG_DIR",&config)]);
                assert_eq!(code(resolve_scope(&missing,distribution)),"scope_directory_missing");
                let relative=fake(&[("TESSERACT_INSTANCE_SCOPE",std::path::Path::new("x")),("TESSERACT_CONFIG_DIR",std::path::Path::new("config")),("TESSERACT_DATA_DIR",&data)]);
                assert_eq!(code(resolve_scope(&relative,distribution)),"absolute_scope_directory_required");
            }
        }
        #[test] fn live_directories_stay_forbidden_under_a_label() {
            let r=root("live");
            let roaming=r.0.join("Roaming");
            let (config,data)=(roaming.join("Tesseract").join("c"),r.0.join("data"));
            fs::create_dir_all(&config).unwrap(); fs::create_dir_all(&data).unwrap();
            for distribution in [false,true] {
                let env=fake(&[("TESSERACT_INSTANCE_SCOPE",std::path::Path::new("x")),("TESSERACT_CONFIG_DIR",&config),("TESSERACT_DATA_DIR",&data),("APPDATA",&roaming)]);
                assert_eq!(code(resolve_scope(&env,distribution)),"live_scope_forbidden");
                let env=fake(&[("TESSERACT_INSTANCE_SCOPE",std::path::Path::new("x")),("TESSERACT_CONFIG_DIR",&data),("TESSERACT_DATA_DIR",&roaming.join("Tesseract")),("LOCALAPPDATA",&roaming)]);
                assert_eq!(code(resolve_scope(&env,distribution)),"live_scope_forbidden");
            }
        }
        #[test] fn directories_without_a_label_and_audit_are_refused_in_both_builds() {
            let r=root("label");
            let (config,data)=(r.0.join("config"),r.0.join("data"));
            fs::create_dir_all(&config).unwrap(); fs::create_dir_all(&data).unwrap();
            for distribution in [false,true] {
                for env in [fake(&[("TESSERACT_CONFIG_DIR",&config)]),fake(&[("TESSERACT_DATA_DIR",&data)]),fake(&[("TESSERACT_CONFIG_DIR",&config),("TESSERACT_DATA_DIR",&data)])] {
                    assert_eq!(code(resolve_scope(&env,distribution)),"scope_label_missing");
                }
                let manifest=r.0.join("manifest.json");
                for env in [fake(&[("TESSERACT_ISOLATED_AUDIT_MANIFEST",&manifest)]),
                    fake(&[("TESSERACT_ISOLATED_AUDIT_MANIFEST",&manifest),("TESSERACT_INSTANCE_SCOPE",std::path::Path::new("x")),("TESSERACT_CONFIG_DIR",&config),("TESSERACT_DATA_DIR",&data)])] {
                    assert_eq!(code(resolve_scope(&env,distribution)),"explicit_custom_scope_required");
                }
            }
        }
        #[test] fn sidecar_choice_per_build_and_scope() {
            let shell=std::path::Path::new("C:\\Apps\\Tesseract\\Tesseract.exe");
            let next=SidecarSource::Frozen(PathBuf::from("C:\\Apps\\Tesseract\\tesseract-runtime.exe"));
            let args=|list:&[&str]| list.iter().map(|s|s.to_string()).collect::<Vec<_>>();
            let other=args(&["--sidecar","D:\\other.exe"]);
            for debug in [false,true] {
                // Distribution: the default scope always runs the one next to the shell and refuses --sidecar.
                assert_eq!(sidecar_source(true,debug,"default",&[],shell),Ok(next.clone()));
                assert_eq!(sidecar_source(true,debug,"default",&other,shell),Err("sidecar_override_forbidden"));
                assert_eq!(sidecar_source(true,debug,"default",&args(&["--autostart","--sidecar"]),shell),Err("sidecar_override_forbidden"));
                assert_eq!(sidecar_source(true,debug,"custom",&[],shell),Ok(next.clone()));
                assert_eq!(sidecar_source(true,debug,"custom",&other,shell),Ok(SidecarSource::Frozen(PathBuf::from("D:\\other.exe"))));
                assert_eq!(sidecar_source(true,debug,"custom",&args(&["--dev-sidecar-script","C:\\s.py"]),shell),Err("dev_sidecar_script_debug_only"));
                // Development: --sidecar, else the repository source sidecar.
                assert_eq!(sidecar_source(false,debug,"custom",&[],shell),Ok(SidecarSource::Source));
                assert_eq!(sidecar_source(false,debug,"custom",&other,shell),Ok(SidecarSource::Frozen(PathBuf::from("D:\\other.exe"))));
            }
            let script=args(&["--dev-sidecar-script","C:\\s.py"]);
            assert_eq!(sidecar_source(false,true,"custom",&script,shell),Ok(SidecarSource::Script(PathBuf::from("C:\\s.py"))));
            assert_eq!(sidecar_source(false,false,"custom",&script,shell),Err("dev_sidecar_script_debug_only"));
        }
        /// 11-05 window-first launch: what ends a start before hello, and that `run` builds the
        /// window without waiting for hello (the waiter starts in `setup`, after the window).
        #[test] fn startup_failure_reads_the_first_answer_and_run_opens_the_window_first() {
            assert_eq!(startup_failure(Ok(Ok(())),true),None);
            assert_eq!(startup_failure(Ok(Ok(())),false),None);
            assert_eq!(startup_failure(Ok(Err("os_2".into())),true).as_deref(),Some("sidecar_start_failed_os_2"));
            assert_eq!(startup_failure(Err(mpsc::RecvTimeoutError::Timeout),true).as_deref(),Some("sidecar_start_timeout"));
            assert_eq!(startup_failure(Err(mpsc::RecvTimeoutError::Disconnected),true).as_deref(),Some("sidecar_start_timeout"));
            // A quit that came first ends the shell by itself: no pre-hello box on top of it.
            assert_eq!(startup_failure(Ok(Err("os_2".into())),false),None);
            assert_eq!(startup_failure(Err(mpsc::RecvTimeoutError::Timeout),false),None);
            // The box's two texts, as before: an exited sidecar, or one that never shook hands.
            assert!(crash::prehello_kind("sidecar_start_failed_os_2")==crash::PreHello::Exited);
            assert!(crash::prehello_kind("sidecar_start_failed_hello_timeout")==crash::PreHello::Handshake);
            assert!(crash::prehello_kind("sidecar_start_timeout")==crash::PreHello::Handshake);
            let source=include_str!("main.rs");
            let run=&source[source.find("fn run()").unwrap()..source.find("fn autostart_window(").unwrap()];
            assert!(!run.contains("recv_timeout(STARTUP_LIMIT)") && !run.contains("ready_rx"), "run must not wait for hello");
            let (window,watch)=(run.find("spec.create(application.handle()").unwrap(),run.find("watch_startup(application.handle()").unwrap());
            assert!(window<watch, "the waiter starts after the window exists");
            let (dialog,exit)=(run.find("dialog::sidecar_before_hello").unwrap(),run.rfind("shell_exit_{code}").unwrap());
            assert!(run.find("drop(instance);").unwrap()<dialog && dialog<exit, "lease first, then the box, then shell_exit_1");
        }
        #[test] fn hello_scope_must_match_the_resolved_kind_and_id() {
            let hello=|kind:&str,id:&str| json!({"t":"hello","scope":{"kind":kind,"id":id}});
            assert!(hello_scope_matches(&hello("default","default"),"default","default"));
            assert!(hello_scope_matches(&hello("custom","0123456789abcdef01234567"),"custom","0123456789abcdef01234567"));
            assert!(!hello_scope_matches(&hello("custom","default"),"default","default"));
            assert!(!hello_scope_matches(&hello("default","default"),"custom","0123456789abcdef01234567"));
            assert!(!hello_scope_matches(&hello("custom","0123456789abcdef01234567"),"custom","0123456789abcdef01234568"));
            assert!(!hello_scope_matches(&json!({"t":"hello"}),"default","default"));
            // The worker checks hello through this function, with the scope options carry.
            let source=include_str!("main.rs");
            let worker=&source[source.find("fn start_worker(").unwrap()..source.find("fn run()").unwrap()];
            assert!(worker.contains("!hello_scope_matches(&hello,scope_kind,&scope)") && !worker.contains("[\"kind\"]!=\"custom\""));
        }
        #[test] fn print_scope_reports_without_side_effects() {
            let r=root("print");
            let (roaming,local)=(r.0.join("Roaming"),r.0.join("Local"));
            let shell=r.0.join("app").join("Tesseract.exe");
            let env=fake(&[("APPDATA",&roaming),("LOCALAPPDATA",&local),("USERPROFILE",&r.0)]);
            let (report,code)=scope_report(&env,true,&[],&shell);
            assert_eq!(code,0,"{report}");
            assert_eq!((report["build"].as_str(),report["kind"].as_str(),report["id"].as_str()),(Some("distribution"),Some("default"),Some("default")));
            assert_eq!(report["config"],json!(roaming.join("Tesseract").to_string_lossy()));
            assert_eq!(report["data"],json!(local.join("Tesseract").to_string_lossy()));
            assert_eq!(report["names"],legacy());
            assert_eq!(report["sidecar"]["mode"],"frozen");
            assert_eq!(report["sidecar"]["path"],json!(shell.with_file_name("tesseract-runtime.exe").to_string_lossy()));
            assert!(report["webview2"].as_str().unwrap().starts_with(&*local.join("Tesseract").join("WebView2").to_string_lossy()));
            let (report,code)=scope_report(&env,true,&["--sidecar".into(),"x.exe".into()],&shell);
            assert_eq!((code,report["sidecar_error"].as_str()),(2,Some("sidecar_override_forbidden")));
            let (report,code)=scope_report(&env,false,&[],&shell);
            assert_eq!((code,report["build"].as_str(),report["error"].as_str()),(2,Some("development"),Some("explicit_custom_scope_required")));
            assert!(!roaming.exists() && !local.exists() && !shell.parent().unwrap().exists(),"--print-scope creates nothing");
        }
        /// A distribution build refuses `--sidecar` in the default scope before it touches the live
        /// profile: right after the decision, before the directories, shell.log and the instance names.
        #[test] fn sidecar_override_is_refused_before_any_side_effect() {
            let source=include_str!("main.rs");
            let run=&source[source.find("fn run()").unwrap()..source.find("fn cli_mode(").unwrap()];
            let at=|needle:&str| run.find(needle).unwrap_or_else(||panic!("run() lacks {needle}"));
            let early=at("if DISTRIBUTION { sidecar_source(DISTRIBUTION,cfg!(debug_assertions),scope.kind,&cli_args,&env::current_exe()?)");
            assert!(at("let scope=resolve_scope(")<early);
            for later in ["fs::create_dir_all(&scope.config)","shell_log::fields(","single_instance::acquire(","let options=options(&scope)?"] {
                assert!(early<at(later),"{later} comes before the sidecar check");
            }
        }
        /// The build flavour is the Cargo feature, and the same fake environment lands where it should.
        #[test] fn this_build_flavour() {
            assert_eq!(DISTRIBUTION,cfg!(feature="distribution"));
            let r=root("flavour");
            let env=fake(&[("APPDATA",&r.0.join("Roaming")),("LOCALAPPDATA",&r.0.join("Local"))]);
            match resolve_scope(&env,DISTRIBUTION) {
                Ok(scope)=>{ assert!(DISTRIBUTION); assert_eq!(scope.kind,"default"); },
                Err(error)=>{ assert!(!DISTRIBUTION); assert_eq!(error.to_string(),"explicit_custom_scope_required"); },
            }
            assert_eq!(APP_VERSION.trim(),fs::read_to_string(super::repo().join("app_version.txt")).unwrap().trim());
        }
    }
}
