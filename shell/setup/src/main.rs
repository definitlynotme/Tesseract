//! Tesseract Setup: one window, one action (design: shell/setup/README.md).
//!
//! The window shows; the embedded Inno Setup package does the installing, silently and
//! elevated, exactly as before (rollback, file replacement, shortcuts, the uninstall entry, the
//! WebView2 check). The window runs as the user, so the app it opens afterwards does too.
//!
//! Hand-over to the Inno package as it is, with no window of ours:
//! - any command-line switch (`/VERYSILENT`, `/DIR=…`: deployments keep working unchanged);
//! - no WebView2 runtime (older Windows 10): Inno's own wizard installs it first.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod assets;
mod handover;
mod payload;
mod progress;
mod rm;
mod sys;

use serde_json::{json, Value};
use std::path::{Path, PathBuf};
use tao::{
    dpi::{LogicalSize, PhysicalPosition},
    event::{Event, WindowEvent},
    event_loop::{ControlFlow, EventLoopBuilder, EventLoopProxy},
    platform::windows::{IconExtWindows, WindowBuilderExtWindows, WindowExtWindows},
    window::{Icon, WindowBuilder},
};
use wry::{NewWindowResponse, WebContext, WebViewBuilder, WebViewBuilderExtWindows};

const VERSION: &str = env!("SETUP_VERSION");
const INSTALLED_BYTES: &str = env!("SETUP_INSTALLED_BYTES");
const ORIGIN: &str = "http://setup.localhost/";
/// SW_SHOWNORMAL, for a Tesseract opened again.
const SW_NORMAL: i32 = 1;

enum Ev {
    Ipc(String),
    ToPage(Value),
    Show,
    /// the installer has finished: what the page shows next and the phase it leaves us in
    Outcome(Value, Phase),
    /// 打开 on the done page: whether Tesseract was started (open_app)
    Opened(bool),
}

#[derive(Clone, Copy, PartialEq)]
enum Phase { Ready, Working, Done, Failed }

fn main() {
    // Split as Inno splits it (sys::params): the switches mean here what they mean to Inno.
    let mut args = sys::params().into_iter();
    let mut payload_arg = None;
    let mut rest = Vec::new();
    while let Some(arg) = args.next() {
        if arg == "--payload" { payload_arg = args.next().map(PathBuf::from); } else { rest.push(arg); }
    }
    let Some(source) = payload::Source::locate(payload_arg) else {
        eprintln!("tesseract-setup: no installer embedded; pass --payload <Tesseract-Setup-…-win-x64.exe>");
        std::process::exit(1);
    };
    if !rest.is_empty() || !sys::webview2_installed() {
        std::process::exit(hand_over(&source, &rest));
    }
    run(source);
}

/// Runs the Inno package with our command line and returns its exit code.
fn hand_over(source: &payload::Source, args: &[String]) -> i32 {
    // 0.1.6: the in-app updater's run waits in a window of its own (handover.rs), shown at once
    // while the app closes, so its elevation prompt opens instead of flashing on the taskbar.
    let mut waiting = shows_handover_window(args, sys::elevated()).then(|| handover::show(sys::ui_language()));
    let existing = sys::existing_install().map(|e| e.dir);
    let target = rm::target_dir(args, existing.as_deref());
    // A running Tesseract would make Inno abort (rm.rs). Only a silent install closes it here: a
    // wizard has Inno's own Preparing page, and /? installs nothing. /NOCLOSEAPPLICATIONS keeps it
    // running. A shell already on its way out (the in-app updater's, or one the user quit) gets the
    // full wait, also when the package then cannot even be extracted, and is not opened again for
    // that (the updater's run opens its own: after_hand_over). One that cannot be asked (it runs
    // elevated and this window does not) gets a short one: Inno then stops with nothing changed.
    let mut closed = None;
    if rm::silent_install(args) && !rm::keep_running(args) {
        if let Some(dir) = &target {
            let quitting = sys::shell_quitting();
            let before = if quitting { None } else { rm::shell_here(dir) };
            let unasked = if quitting || rm::restarts_app(args) { rm::WAIT } else { rm::UNASKED_WAIT };
            let _ = rm::close_app(dir, rm::WAIT, unasked);
            closed = before;
        }
    }
    let switches = inno_switches(args, closed.as_ref().map(reopens_with_own_token));
    let owner = waiting.as_ref().map_or(0, handover::Window::owner);
    let code = match payload::extract(source) {
        Err(_) => 1,
        Ok(file) => {
            let mut params = switches.iter().map(|a| sys::quote(a)).collect::<Vec<_>>();
            // The in-app updater's run leaves a log behind, for when an update does not finish.
            if rm::restarts_app(args) && !rm::logs_itself(args) {
                if let Some(log) = temp_log("Tesseract-Update") { params.push(sys::quote(&format!("/LOG={}", log.display()))); }
            }
            match sys::run_and_wait_then(owner, file.path(), &params.join(" "), false, || drop(waiting.take())) {
                sys::Run::Exited(code) => code as i32,
                sys::Run::Declined => 2,
                sys::Run::Failed(_) => 1,
            }
        },
    };
    drop(waiting);
    // An install never leaves the user without Tesseract (after_hand_over).
    if let (Some(dir), Some(how)) = (&target, after_hand_over(closed, args, code)) { reopen(dir, how); }
    code
}

/// Whether a hand-over waits for its elevation in a window of its own (handover.rs): the in-app
/// updater's run (`/RESTARTAPPLICATIONS`, silent), started unelevated, so the elevation prompts.
/// Deployments (`/VERYSILENT` without it, or an elevated console) keep running with no window.
fn shows_handover_window(args: &[String], elevated: bool) -> bool {
    !elevated && rm::restarts_app(args) && rm::silent_install(args)
}

/// Whether the copy this window closed opens again with this window's own token: `routes` starts
/// it that way first only for the same account and elevation, outside a launcher's job. Test
/// builds always do (they keep the sandbox scope in this window's environment).
fn reopens_with_own_token(shell: &rm::Shell) -> bool {
    sys::test_key().is_some()
        || routes(shell, sys::own_user().as_deref(), sys::elevated(), sys::in_job(), sys::explorer_elevated()).first() == Some(&Route::Own)
}

/// The switches the Inno package gets. `/RESTARTAPPLICATIONS` also makes the .iss `[Run]` entry
/// start the installed Tesseract after a good install with Inno's token, which is this window's
/// (its runasoriginaluser does nothing for a window that is already elevated). That fits when this
/// window closed nothing (`own_token` None: the in-app updater's run, whose shell quit before it
/// started this window with its own token) or when this window's token is the closed copy's own.
/// A deployment from an elevated console or another account that closed the user's copy runs
/// without it (0.1.5), and that copy opens again as it ran (`after_hand_over`).
fn inno_switches(args: &[String], own_token: Option<bool>) -> Vec<String> {
    let keep = |a: &&String| own_token != Some(false) || !a.eq_ignore_ascii_case("/RESTARTAPPLICATIONS");
    args.iter().filter(keep).cloned().collect()
}

/// How Tesseract opens again after a hand-over, if at all. The copy this window closed: as it ran
/// (`Reopen::Closed`, `routes`), after an install that did not finish, and after a good one too
/// (Inno's own RestartApplications=yes) unless /NORESTARTAPPLICATIONS says it stays closed; a
/// `[Run]` relaunch already running is not started twice (`reopen`). Nothing closed and the in-app
/// updater's run (`/RESTARTAPPLICATIONS`, its switch): whenever the package ends without starting
/// it again (UAC declined, aborted, rolled back, a failed relaunch), the version installed now,
/// with this window's token, which the app itself gave it. Before 0.1.5 every
/// `/RESTARTAPPLICATIONS` run counted as the updater's, so an elevated console's deployment brought
/// the user's copy back elevated.
fn after_hand_over(closed: Option<rm::Shell>, args: &[String], code: i32) -> Option<Reopen> {
    match closed {
        Some(shell) => (code != 0 || !rm::no_restart(args)).then_some(Reopen::Closed(shell)),
        None => rm::restarts_app(args).then_some(Reopen::Own),
    }
}

/// How Tesseract is started again.
#[derive(Debug, PartialEq)]
enum Reopen {
    /// With this window's token: the in-app updater's run, which the app itself started (and
    /// whose job, if any, the app lived in too).
    Own,
    /// As the copy this window closed ran.
    Closed(rm::Shell),
}

/// Opens `<dir>\Tesseract.exe` again, unless it already runs in this session (rm.rs), and never in
/// a service's session, which has no desktop.
fn reopen(dir: &Path, how: Reopen) {
    let exe = dir.join("Tesseract.exe");
    if !exe.is_file() || !sys::in_user_session() || rm::shell_running(dir) { return; }
    let shell = match how {
        Reopen::Own => { let _ = sys::open(&exe, Some(dir)); return; }
        Reopen::Closed(shell) => shell,
    };
    // Test builds keep the sandbox scope in this window's environment.
    if sys::test_key().is_some() { let _ = sys::open(&exe, Some(dir)); return; }
    let own_user = sys::own_user();
    for route in routes(&shell, own_user.as_deref(), sys::elevated(), sys::in_job(), sys::explorer_elevated()) {
        let started = match route {
            Route::Own => sys::open(&exe, Some(dir)),
            Route::Breakaway => sys::open_breakaway(&exe, dir),
            Route::Explorer => sys::open_as_desktop_user(&exe, "", dir, SW_NORMAL),
        };
        if started { return; }
    }
    // none fits: it stays closed, closed cleanly with its network restored
}

#[derive(Debug, Clone, Copy, PartialEq)]
enum Route {
    /// This window's token, from here.
    Own,
    /// This window's token, outside its job.
    Breakaway,
    /// The signed-in user's, through Explorer: unelevated (elevated where UAC is off).
    Explorer,
}

/// The ways to start the copy this window closed, in order. This window's token only when it is
/// the copy's own (same account, same elevation), and never inside a launcher's job, which would
/// wait for the app (Start-Process -Wait) or end it with the job and skip its network restore.
/// Through Explorer only when its token fits: always for a copy that ran unelevated, for an
/// elevated one only where Explorer is elevated too. A token this window cannot read belongs to a
/// process above its integrity: elevated.
fn routes(shell: &rm::Shell, own_user: Option<&[u8]>, own_elevated: bool, in_job: bool, explorer_elevated: Option<bool>) -> Vec<Route> {
    let elevated = shell.elevated.unwrap_or(true);
    let mut routes = Vec::new();
    if shell.user.is_some() && shell.user.as_deref() == own_user && elevated == own_elevated {
        routes.push(if in_job { Route::Breakaway } else { Route::Own });
    }
    if !elevated || explorer_elevated == Some(true) { routes.push(Route::Explorer); }
    routes
}

/// 打开 on the done page starts the installed Tesseract as the signed-in user: from this window
/// (it runs as the user: `here`), or, from a window started as administrator, through Explorer
/// only (`desktop`, which waits at most 10 s for it). True: started. 0.1.5: when Explorer cannot be
/// asked or does not answer, nothing is started and the page says to use the Start menu. Before,
/// this window then started an elevated copy itself, the case `routes` refuses, and Explorer could
/// still run the queued start afterwards, a second copy that cannot open the first one's objects.
fn open_app(elevated: bool, test_build: bool, desktop: impl FnOnce() -> bool, here: impl FnOnce() -> bool) -> bool {
    if elevated && !test_build { desktop() } else { here() }
}

/// A fresh log path for one run of the package, `%TEMP%\<prefix>-<unix seconds>.log`. None unless
/// %TEMP% is a folder on a local disk: Inno stops when it cannot create its /LOG file, and its
/// elevated instance does not see a network, SUBST or mapped drive of this logon.
fn temp_log(prefix: &str) -> Option<PathBuf> {
    let stamp = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0);
    let dir = std::env::temp_dir();
    (dir.is_dir() && sys::local_disk(&dir)).then(|| dir.join(format!("{prefix}-{stamp}.log")))
}

/// What the page needs to draw its first screen. Read again on 重试.
fn facts() -> Value {
    let existing = sys::existing_install();
    let scene = match &existing { None => "fresh", Some(e) if e.version == VERSION => "reinstall", Some(_) => "update" };
    json!({
        "ev": "init",
        "lang": sys::ui_language(),
        "version": VERSION,
        "scene": scene,
        "old": existing.as_ref().map(|e| e.version.clone()),
        "dir": existing.as_ref().map(|e| e.dir.clone()).unwrap_or_else(sys::default_dir),
        "desktopExists": sys::desktop_shortcut_exists(),
        "running": sys::app_running(),
    })
}

/// A picked folder becomes `<folder>\Tesseract`, unless it already is one.
fn install_dir_for(picked: &str) -> String {
    let picked = picked.trim_end_matches('\\');
    let last = Path::new(picked).file_name().map(|n| n.to_string_lossy().to_lowercase());
    if last.as_deref() == Some("tesseract") { picked.to_owned() } else { format!(r"{picked}\Tesseract") }
}

fn valid_dir(dir: &str) -> bool {
    let b = dir.as_bytes();
    b.len() >= 3 && b[0].is_ascii_alphabetic() && b[1] == b':' && b[2] == b'\\' && !dir.contains(['"', '<', '>', '|', '?', '*'])
        && !dir[2..].contains(':')
}

fn send(proxy: &EventLoopProxy<Ev>, v: Value) { let _ = proxy.send_event(Ev::ToPage(v)); }

/// The worker: extract, elevate, wait; reports steps and the outcome through the proxy.
fn install(proxy: EventLoopProxy<Ev>, source: &'static payload::Source, owner: isize, progress_hwnd: isize, dir: Option<String>, desktop: bool) {
    sys::com_init();
    send(&proxy, json!({"ev": "step", "step": "prepare"}));
    let file = match payload::extract(source) {
        Ok(f) => f,
        Err(e) => {
            let _ = proxy.send_event(Ev::Outcome(json!({"ev": "failed", "kind": "extract", "detail": e.to_string()}), Phase::Failed));
            return;
        }
    };
    let log = temp_log("Tesseract-Setup");
    let mut params = vec![
        "/VERYSILENT".to_owned(), "/SUPPRESSMSGBOXES".into(), "/NORESTART".into(), "/NOCANCEL".into(), "/SP-".into(),
        "/CLOSEAPPLICATIONS".into(), "/NORESTARTAPPLICATIONS".into(),
        format!("/TSPROGRESS={progress_hwnd}"),
        format!("/MERGETASKS={}desktopicon", if desktop { "" } else { "!" }),
    ];
    if let Some(log) = &log { params.push(format!("/LOG={}", log.display())); }
    // Close a running Tesseract politely first: Inno's own pass cannot (rm.rs) and would roll back.
    // This session's Tesseract, as it ran, opens again afterwards, whatever the outcome: the user
    // had it running. One the user is already quitting gets the full wait and stays closed.
    let target = dir.clone().or_else(|| sys::existing_install().map(|e| e.dir)).map(PathBuf::from);
    let mut closed = None;
    if let Some(target) = target.as_ref().filter(|t| rm::in_use(t)) {
        let quitting = sys::shell_quitting();
        let before = if quitting { None } else { rm::shell_here(target) };
        send(&proxy, json!({"ev": "step", "step": "close"}));
        match rm::close_app(target, rm::WAIT, if quitting { rm::WAIT } else { rm::UNASKED_WAIT }) {
            rm::Closed::StillRunning(left) => {
                // Either the shell itself is still here ("running": quit it from the tray), or it
                // went (its network settings already restored) while another program keeps one of
                // the folder's modules loaded ("busy", named): what was closed opens again.
                let page = if rm::shell_running(target) {
                    json!({"ev": "failed", "kind": "running"})
                } else {
                    if let Some(shell) = before { reopen(target, Reopen::Closed(shell)); }
                    json!({"ev": "failed", "kind": "busy", "apps": rm::names(&left)})
                };
                let _ = proxy.send_event(Ev::Outcome(page, Phase::Failed));
                return;
            }
            rm::Closed::Gone | rm::Closed::NothingRunning | rm::Closed::Unknown => closed = before,
        }
    }
    if let Some(dir) = dir { params.push(format!("/DIR={dir}")); }
    let params = params.iter().map(|a| sys::quote(a)).collect::<Vec<_>>().join(" ");
    send(&proxy, json!({"ev": "step", "step": "elevate"}));
    let outcome = sys::run_and_wait(owner, file.path(), &params, !sys::no_elevate());
    drop(file);
    if let (Some(shell), Some(dir)) = (closed, target.as_ref()) { reopen(dir, Reopen::Closed(shell)); }
    let log_shown = log.filter(|l| l.exists()).map(|l| l.display().to_string());
    let (page, phase) = match outcome {
        sys::Run::Exited(0) => {
            let dir = sys::existing_install().map(|e| e.dir);
            (json!({"ev": "done", "dir": dir, "desktop": sys::desktop_shortcut_exists()}), Phase::Done)
        }
        // Inno's exit codes: 1–3, 7, 8 end before anything is changed; 4 and 5 roll back;
        // 6 was killed mid-way (no rollback)
        sys::Run::Exited(code) => {
            let kind = match code { 1 | 2 | 3 | 7 | 8 => "unchanged", 4 | 5 => "rolledback", 6 => "interrupted", _ => "unknown" };
            (json!({"ev": "failed", "kind": kind, "code": code, "log": log_shown}), Phase::Failed)
        }
        sys::Run::Declined => (json!({"ev": "declined"}), Phase::Ready),
        sys::Run::Failed(detail) => (json!({"ev": "failed", "kind": "launch", "detail": detail, "log": log_shown}), Phase::Failed),
    };
    let _ = proxy.send_event(Ev::Outcome(page, phase));
}

fn run(source: payload::Source) {
    let source: &'static payload::Source = Box::leak(Box::new(source));
    let event_loop = EventLoopBuilder::<Ev>::with_user_event().build();
    let proxy = event_loop.create_proxy();

    let window = WindowBuilder::new()
        .with_title("Tesseract Setup")
        .with_inner_size(LogicalSize::new(560.0, 380.0))
        .with_resizable(false)
        .with_maximizable(false)
        .with_decorations(false)
        .with_undecorated_shadow(true)
        .with_visible(false)
        .with_window_icon(Icon::from_resource(1, None).ok())
        .build(&event_loop)
        .expect("window");
    if let Some(m) = window.current_monitor().or_else(|| window.primary_monitor()) {
        let (mp, ms, ws) = (m.position(), m.size(), window.outer_size());
        window.set_outer_position(PhysicalPosition::new(
            mp.x + (ms.width as i32 - ws.width as i32) / 2,
            mp.y + (ms.height as i32 - ws.height as i32) * 9 / 20,
        ));
    }
    let hwnd = window.hwnd();
    sys::round_corners(hwnd);

    let progress_hwnd = {
        let p = proxy.clone();
        progress::spawn(move |stage, permille| send(&p, json!({"ev": "progress", "stage": stage, "permille": permille.min(1000)})))
    };

    let mut context = WebContext::new(Some(std::env::temp_dir().join("Tesseract-Setup-WebView2")));
    let ipc = proxy.clone();
    let webview = WebViewBuilder::new_with_web_context(&mut context)
        .with_custom_protocol("setup".into(), |_id, request| assets::serve(request.uri().path()))
        .with_url(format!("{ORIGIN}index.html"))
        .with_ipc_handler(move |request| { let _ = ipc.send_event(Ev::Ipc(request.into_body())); })
        .with_navigation_handler(|url| url.starts_with(ORIGIN))
        .with_new_window_req_handler(|_, _| NewWindowResponse::Deny)
        .with_background_color((255, 255, 255, 255))
        .with_devtools(cfg!(debug_assertions))
        .with_hotkeys_zoom(false)
        .with_browser_accelerator_keys(false)
        .with_default_context_menus(false)
        .build(&window)
        .expect("webview");

    // the page shows the window once its fonts are in (no blank or re-flowing first frame);
    // a page that never says so still gets a window
    { let p = proxy.clone(); std::thread::spawn(move || { std::thread::sleep(std::time::Duration::from_millis(2500)); let _ = p.send_event(Ev::Show); }); }

    let needed: u64 = INSTALLED_BYTES.parse().unwrap_or(0);
    let mut phase = Phase::Ready;
    let mut shown = false;
    let mut app_dir: Option<String> = None;
    let mut log: Option<String> = None;
    // 打开 is being carried out (open_app, on a thread of its own): a second click waits for it
    let mut opening = false;

    event_loop.run(move |event, _, flow| {
        *flow = ControlFlow::Wait;
        let to_page = |v: &Value| { let _ = webview.evaluate_script(&format!("window.__host&&window.__host({v})")); };
        match event {
            Event::WindowEvent { event: WindowEvent::CloseRequested, .. } => {
                if phase != Phase::Working { *flow = ControlFlow::Exit; }
            }
            Event::UserEvent(Ev::Show) => {
                if !shown { shown = true; window.set_visible(true); window.set_focus(); }
            }
            Event::UserEvent(Ev::ToPage(v)) => to_page(&v),
            Event::UserEvent(Ev::Outcome(v, next)) => {
                phase = next;
                if next == Phase::Done { app_dir = v.get("dir").and_then(Value::as_str).map(str::to_owned); }
                log = v.get("log").and_then(Value::as_str).map(str::to_owned);
                to_page(&v);
            }
            Event::UserEvent(Ev::Opened(started)) => {
                opening = false;
                if started { *flow = ControlFlow::Exit; } else { to_page(&json!({"ev": "openManual"})); }
            }
            Event::UserEvent(Ev::Ipc(text)) => {
                let Ok(msg) = serde_json::from_str::<Value>(&text) else { return };
                match msg.get("cmd").and_then(Value::as_str).unwrap_or("") {
                    "hello" | "refresh" => to_page(&facts()),
                    "shown" => { if !shown { shown = true; window.set_visible(true); window.set_focus(); } }
                    "drag" => { let _ = window.drag_window(); }
                    "min" => window.set_minimized(true),
                    "close" => { if phase != Phase::Working { *flow = ControlFlow::Exit; } }
                    "pick" if phase != Phase::Working => {
                        let start = msg.get("dir").and_then(Value::as_str).unwrap_or("").to_owned();
                        let p = proxy.clone();
                        std::thread::spawn(move || {
                            sys::com_init();
                            if let Ok(Some(picked)) = sys::pick_folder(hwnd, &start) { send(&p, json!({"ev": "dir", "dir": install_dir_for(&picked)})); }
                        });
                    }
                    "install" if phase == Phase::Ready || phase == Phase::Failed => {
                        let scene_fresh = sys::existing_install().is_none();
                        let dir = msg.get("dir").and_then(Value::as_str).map(|d| d.trim().trim_end_matches('\\').to_owned());
                        // an existing install stays where it is (Inno's UsePreviousAppDir)
                        let dir = if scene_fresh { dir.filter(|d| valid_dir(d)) } else { None };
                        if scene_fresh && dir.is_none() { to_page(&json!({"ev": "failed", "kind": "dir"})); phase = Phase::Failed; return; }
                        let check = dir.clone().unwrap_or_else(sys::default_dir);
                        if needed > 0 {
                            if let Some((free, drive)) = sys::free_bytes(&check) {
                                if free < needed + needed / 20 {
                                    to_page(&json!({"ev": "failed", "kind": "disk", "need": needed, "free": free, "drive": drive}));
                                    phase = Phase::Failed;
                                    return;
                                }
                            }
                        }
                        phase = Phase::Working;
                        let desktop = msg.get("desktop").and_then(Value::as_bool).unwrap_or(false);
                        let p = proxy.clone();
                        std::thread::spawn(move || install(p, source, hwnd, progress_hwnd, dir, desktop));
                    }
                    "open" if phase == Phase::Done && !opening => {
                        let Some(dir) = app_dir.clone().map(PathBuf::from) else { *flow = ControlFlow::Exit; return };
                        // as the signed-in user, also from a window started as administrator; off
                        // this thread, which must not stop drawing while Explorer takes its time
                        opening = true;
                        let (p, elevated, test_build) = (proxy.clone(), sys::elevated(), sys::test_key().is_some());
                        std::thread::spawn(move || {
                            sys::com_init();
                            let exe = dir.join("Tesseract.exe");
                            let started = open_app(elevated, test_build, || sys::open_as_desktop_user(&exe, "", &dir, SW_NORMAL), || sys::open(&exe, Some(&dir)));
                            let _ = p.send_event(Ev::Opened(started));
                        });
                    }
                    "log" => { if let Some(l) = &log { sys::open(Path::new(l), None); } }
                    _ => {}
                }
            }
            _ => {}
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn only_the_unelevated_in_app_update_waits_in_a_window() {
        let args = |line: &str| line.split(' ').map(str::to_owned).collect::<Vec<_>>();
        let in_app = args("/SILENT /SUPPRESSMSGBOXES /CLOSEAPPLICATIONS /RESTARTAPPLICATIONS /NORESTART");
        assert!(shows_handover_window(&in_app, false));
        assert!(!shows_handover_window(&in_app, true), "already elevated: no prompt to wait for");
        assert!(!shows_handover_window(&args("/VERYSILENT /DIR=C:\\Apps\\Tesseract"), false), "a deployment");
        assert!(!shows_handover_window(&args("/RESTARTAPPLICATIONS"), false), "Inno's own wizard");
        assert!(!shows_handover_window(&args("/SILENT /RESTARTAPPLICATIONS /?"), false));
    }
    #[test]
    fn the_copy_this_window_closed_starts_with_its_own_kind_of_token() {
        let user = |n: u8| Some(vec![1, 5, 0, 0, 0, 0, 0, 5, 21, n]);
        let shell = |elevated: Option<bool>, user: Option<Vec<u8>>| rm::Shell { pid: 4, elevated, user };
        let me = user(1);
        let none: Vec<Route> = Vec::new();
        // the page, started from the desktop: its own token, Explorer as a fallback
        assert_eq!(routes(&shell(Some(false), me.clone()), me.as_deref(), false, false, Some(false)), [Route::Own, Route::Explorer]);
        // an elevated console or Run as administrator, the user's unelevated copy: through Explorer
        assert_eq!(routes(&shell(Some(false), me.clone()), me.as_deref(), true, false, Some(false)), [Route::Explorer]);
        // an elevated console, an elevated (TUN, kill switch) copy: this token; inside
        // Start-Process -Wait's job only by breaking away, never into the job
        assert_eq!(routes(&shell(Some(true), me.clone()), me.as_deref(), true, false, Some(false)), [Route::Own]);
        assert_eq!(routes(&shell(Some(true), me.clone()), me.as_deref(), true, true, Some(false)), [Route::Breakaway]);
        // a launcher's job around an unelevated window: outside it, by breaking away or through Explorer
        assert_eq!(routes(&shell(Some(false), me.clone()), me.as_deref(), false, true, Some(false)), [Route::Breakaway, Route::Explorer]);
        // SYSTEM or another account in the user's session: never its own token
        assert_eq!(routes(&shell(Some(true), me.clone()), user(18).as_deref(), true, false, Some(false)), none);
        assert_eq!(routes(&shell(Some(false), me.clone()), user(18).as_deref(), true, false, Some(false)), [Route::Explorer]);
        // a copy this window cannot read runs above it: elevated, so not through an unelevated Explorer
        assert_eq!(routes(&shell(None, None), me.as_deref(), false, false, Some(false)), none);
        // UAC off: everything is elevated, Explorer too
        assert_eq!(routes(&shell(Some(true), me.clone()), me.as_deref(), true, false, Some(true)), [Route::Own, Route::Explorer]);
    }

    #[test]
    fn only_the_updaters_own_run_reopens_with_this_windows_token() {
        // 0.1.5: /RESTARTAPPLICATIONS is the in-app updater's switch, but also Inno's documented one.
        // A deployment from an elevated console (or another account) that closed the user's running
        // copy brought it back with this window's token: elevated, or as that account.
        let args = |a: &[&str]| a.iter().map(|s| s.to_string()).collect::<Vec<_>>();
        let updater = args(&["/SILENT", "/SUPPRESSMSGBOXES", "/CLOSEAPPLICATIONS", "/RESTARTAPPLICATIONS", "/NORESTART"]);
        let copy = rm::Shell { pid: 4, elevated: Some(false), user: Some(vec![1, 5, 0, 0, 0, 0, 0, 5, 21, 1]) };
        // The updater's run: its shell had quit, nothing was closed here. The package keeps the switch
        // (its [Run] relaunch), and the installed version opens with this window's token otherwise.
        assert_eq!(inno_switches(&updater, None), updater);
        for code in [0, 2, 5] { assert_eq!(after_hand_over(None, &updater, code), Some(Reopen::Own), "exit {code}"); }
        // The same switches from a context whose token is not the closed copy's own: Inno runs
        // without /RESTARTAPPLICATIONS (no [Run] relaunch as this window), and the copy opens again
        // as it ran (routes), after a good install too.
        assert_eq!(inno_switches(&updater, Some(false)), args(&["/SILENT", "/SUPPRESSMSGBOXES", "/CLOSEAPPLICATIONS", "/NORESTART"]));
        assert_eq!(inno_switches(&args(&["/silent", "/RestartApplications", r"/DIR=D:\T"]), Some(false)), args(&["/silent", r"/DIR=D:\T"]), "any case");
        for code in [0, 5] { assert_eq!(after_hand_over(Some(copy.clone()), &updater, code), Some(Reopen::Closed(copy.clone())), "exit {code}"); }
        // This window's token is the copy's own (the same account and elevation): the [Run]
        // relaunch may start it, and reopen() then finds it running.
        assert_eq!(inno_switches(&updater, Some(true)), updater);
        assert_eq!(after_hand_over(Some(copy.clone()), &updater, 0), Some(Reopen::Closed(copy.clone())));
        // A deployment without the switch, as before: what was closed opens again unless
        // /NORESTARTAPPLICATIONS after a good install; nothing closed, nothing opened.
        let deploy = args(&["/VERYSILENT", "/NORESTARTAPPLICATIONS"]);
        assert_eq!(inno_switches(&deploy, Some(false)), deploy);
        assert_eq!(after_hand_over(Some(copy.clone()), &deploy, 0), None);
        assert_eq!(after_hand_over(Some(copy.clone()), &deploy, 5), Some(Reopen::Closed(copy.clone())));
        assert_eq!(after_hand_over(Some(copy.clone()), &args(&["/VERYSILENT"]), 0), Some(Reopen::Closed(copy.clone())));
        assert_eq!(after_hand_over(None, &deploy, 5), None);
    }

    #[test]
    fn the_done_pages_open_never_starts_an_elevated_copy_itself() {
        // 0.1.5: from a window started as administrator, 打开 goes through Explorer only. When
        // Explorer cannot be asked or does not answer, nothing starts from here (an elevated copy).
        use std::cell::Cell;
        let (desktop, here) = (Cell::new(0), Cell::new(0));
        let open = |elevated, test_build, explorer_starts: bool, starts_here: bool| open_app(elevated, test_build,
            || { desktop.set(desktop.get() + 1); explorer_starts }, || { here.set(here.get() + 1); starts_here });
        assert!(open(false, false, true, true), "a window of the user's: from here");
        assert_eq!((desktop.get(), here.get()), (0, 1));
        assert!(open(true, false, true, true), "an elevated window: through Explorer");
        assert_eq!((desktop.get(), here.get()), (1, 1));
        assert!(!open(true, false, false, true), "Explorer did not start it: nothing started, the page says so");
        assert_eq!((desktop.get(), here.get()), (2, 1), "never from this elevated window");
        assert!(open(true, true, false, true), "test builds keep the sandbox scope in this window: from here");
        assert_eq!((desktop.get(), here.get()), (2, 2));
        assert!(!open(false, false, true, false), "a start that failed here is said too");
    }

    #[test]
    fn picked_folder_gets_the_app_folder() {
        assert_eq!(install_dir_for(r"D:\Apps"), r"D:\Apps\Tesseract");
        assert_eq!(install_dir_for(r"D:\Apps\"), r"D:\Apps\Tesseract");
        assert_eq!(install_dir_for(r"D:\Apps\tesseract"), r"D:\Apps\tesseract");
        assert_eq!(install_dir_for(r"D:\"), r"D:\Tesseract");
    }
    #[test]
    fn dirs() {
        assert!(valid_dir(r"C:\Program Files\Tesseract"));
        assert!(valid_dir(r"D:\应用\Tesseract"));
        assert!(!valid_dir(r"\\server\share\Tesseract"));
        assert!(!valid_dir(r"Tesseract"));
        assert!(!valid_dir(r#"C:\a"b"#));
        assert!(!valid_dir(r"C:\a:b"));
    }
}
