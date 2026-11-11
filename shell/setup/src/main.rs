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
mod payload;
mod progress;
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

enum Ev {
    Ipc(String),
    ToPage(Value),
    Show,
    /// the installer has finished: what the page shows next and the phase it leaves us in
    Outcome(Value, Phase),
}

#[derive(Clone, Copy, PartialEq)]
enum Phase { Ready, Working, Done, Failed }

fn main() {
    let mut args = std::env::args_os().skip(1);
    let mut payload_arg = None;
    let mut rest = Vec::new();
    while let Some(arg) = args.next() {
        if arg == "--payload" { payload_arg = args.next().map(PathBuf::from); } else { rest.push(arg.to_string_lossy().into_owned()); }
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
    let Ok(file) = payload::extract(source) else { return 1 };
    let params = args.iter().map(|a| sys::quote(a)).collect::<Vec<_>>().join(" ");
    match sys::run_and_wait(0, file.path(), &params, false) {
        sys::Run::Exited(code) => code as i32,
        sys::Run::Declined => 2,
        sys::Run::Failed(_) => 1,
    }
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
    let stamp = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0);
    let log = std::env::temp_dir().join(format!("Tesseract-Setup-{stamp}.log"));
    let mut params = vec![
        "/VERYSILENT".to_owned(), "/SUPPRESSMSGBOXES".into(), "/NORESTART".into(), "/NOCANCEL".into(), "/SP-".into(),
        "/CLOSEAPPLICATIONS".into(), "/NORESTARTAPPLICATIONS".into(),
        format!("/LOG={}", log.display()),
        format!("/TSPROGRESS={progress_hwnd}"),
        format!("/MERGETASKS={}desktopicon", if desktop { "" } else { "!" }),
    ];
    if let Some(dir) = dir { params.push(format!("/DIR={dir}")); }
    let params = params.iter().map(|a| sys::quote(a)).collect::<Vec<_>>().join(" ");
    send(&proxy, json!({"ev": "step", "step": "elevate"}));
    let outcome = sys::run_and_wait(owner, file.path(), &params, !sys::no_elevate());
    drop(file);
    let log_shown = log.exists().then(|| log.display().to_string());
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
                    "open" if phase == Phase::Done => {
                        if let Some(dir) = &app_dir {
                            let dir = PathBuf::from(dir);
                            sys::open(&dir.join("Tesseract.exe"), Some(&dir));
                        }
                        *flow = ControlFlow::Exit;
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
