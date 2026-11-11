use serde::{Deserialize, Serialize};
use std::{cell::RefCell, fs, path::{Path, PathBuf}, sync::{atomic::{AtomicBool, Ordering}, OnceLock}};
use tauri::{PhysicalPosition, WebviewUrl, WebviewWindow, WebviewWindowBuilder};
use webview2_com::Microsoft::Web::WebView2::Win32::*;
use windows::core::Interface;

pub const WIDTH: f64 = 1280.0;
pub const HEIGHT: f64 = 800.0;
pub const MIN_WIDTH: f64 = 1060.0;
pub const MIN_HEIGHT: f64 = 700.0;
/// T-P6-06: a distribution build's window is the product, `Tesseract`. Development builds keep the
/// P3 title that the e2e runs, `release_acceptance.py` and the p3 smokes look for.
pub const TITLE: &str = if cfg!(feature = "distribution") { "Tesseract" } else { "Tesseract Shell (P3)" };
pub const DEFAULT_ARGS: &str = "--disable-features=msWebOOUI,msPdfOOUI,msSmartScreenProtection";
pub const GUARDS: &str = r#"(() => {
document.addEventListener('dragover', e => { if (e.dataTransfer?.types.includes('Files')) e.preventDefault(); }, true);
document.addEventListener('drop', e => { if (e.dataTransfer?.types.includes('Files')) { e.preventDefault(); e.stopImmediatePropagation(); } }, true);
document.addEventListener('keydown', e => {
 const k = e.key.toLowerCase();
 if (k === 'f5' || ((e.ctrlKey || e.metaKey) && ['r','+','-','=','0'].includes(k))) { e.preventDefault(); e.stopImmediatePropagation(); }
}, true);
})()"#;
/// T-P4-C3 item 7 (G52, criticism D4): the shell half of reduced motion. Only this environment
/// variable decides it (no registry, no system setting: the page ORs in the CSS media query
/// itself, T-P4-06).
pub const REDUCED_MOTION_ENV: &str = "TESSERACT_REDUCED_MOTION";
/// `1`, `true`, `yes`, `on` (any case, surrounding blanks ignored) mean reduced; anything else,
/// or unset, means not reduced.
pub fn reduced_motion(value: Option<&std::ffi::OsStr>) -> bool {
    value.and_then(|v| v.to_str()).map(|v| v.trim().to_ascii_lowercase())
        .is_some_and(|v| matches!(v.as_str(), "1" | "true" | "yes" | "on"))
}
/// The initialization script of every main window: the input guards, then the frozen
/// `window.__TESSERACT_MOTION__` the page reads before its own scripts run.
pub fn init_script(reduced: bool) -> String {
    format!("{GUARDS};\nwindow.__TESSERACT_MOTION__=Object.freeze({{reduced:{reduced}}});")
}

#[derive(Debug, PartialEq)]
pub struct Policy {
    pub devtools: bool, pub browser_keys: bool, pub context_menu: bool,
    pub zoom: bool, pub drop: bool, pub status_bar: bool, pub autofill: bool,
    pub pinch: bool, pub swipe: bool,
}
pub fn policy(debug: bool) -> Policy {
    Policy { devtools: debug, browser_keys: debug, context_menu: debug,
        zoom: false, drop: false, status_bar: false, autofill: false, pinch: false, swipe: false }
}
pub fn internal(url: &tauri::Url) -> bool {
    url.scheme() == "http" && url.host_str() == Some("tauri.localhost") && url.port_or_known_default() == Some(80)
        && url.username().is_empty() && url.password().is_none()
}
pub fn open_external(url: &tauri::Url) {
    if internal(url) { return; }
    if !["https","http"].contains(&url.scheme()) || url.host_str().is_none()
        || !url.username().is_empty() || url.password().is_some() { return; }
    let url = windows::core::HSTRING::from(url.as_str());
    unsafe { windows::Win32::UI::Shell::ShellExecuteW(None, windows::core::w!("open"), &url,
        None, None, windows::Win32::UI::WindowsAndMessaging::SW_SHOWNORMAL); }
}
// P3-03: hiding to the tray sets WebView2 IsVisible=false, so the page sees
// document.visibilityState='hidden' and Chromium stops animation frames and throttles timers.
// The shell records the transition internally; ui.presence reporting is a later ticket.
thread_local! { static CONTROLLER: RefCell<Option<ICoreWebView2Controller>> = const { RefCell::new(None) }; }
static VISIBLE: AtomicBool = AtomicBool::new(false);
static VISIBILITY_LOG: OnceLock<PathBuf> = OnceLock::new();
const VISIBILITY_SUBCLASS: usize = 0x5033_0003;
#[allow(dead_code)] // read by the later ui.presence ticket
pub fn window_visible() -> bool { VISIBLE.load(Ordering::Acquire) }
fn transition(previous: bool, visible: bool) -> Option<&'static str> {
    (previous != visible).then_some(if visible { "window_shown" } else { "window_hidden" })
}
/// T-P3-07 §7.9, on the UI thread after the event loop ended: Tauri keeps its windows (and so the
/// WebView) alive as long as any AppHandle exists, so the shell closes the controller itself; the
/// browser process can then exit and raise BrowserProcessExited. True when a controller was closed.
pub fn close_controller() -> bool {
    CONTROLLER.with(|c| c.borrow_mut().take()).is_some_and(|controller| unsafe { controller.Close() }.is_ok())
}
fn apply_visibility(visible: bool) {
    CONTROLLER.with(|c| if let Some(controller) = c.borrow().as_ref() { unsafe { let _ = controller.SetIsVisible(visible); } });
    if let (Some(code), Some(dir)) = (transition(VISIBLE.swap(visible, Ordering::AcqRel), visible), VISIBILITY_LOG.get()) {
        crate::log_code(dir, code);
    }
}
unsafe extern "system" fn visibility_proc(hwnd: windows::Win32::Foundation::HWND, message: u32,
    wparam: windows::Win32::Foundation::WPARAM, lparam: windows::Win32::Foundation::LPARAM, _: usize, _: usize) -> windows::Win32::Foundation::LRESULT {
    use windows::Win32::UI::{Shell::{DefSubclassProc, RemoveWindowSubclass}, WindowsAndMessaging::{WM_NCDESTROY, WM_SHOWWINDOW}};
    // Every show/hide path (close-to-tray, tray restore, second launch) passes through here.
    if message == WM_SHOWWINDOW { apply_visibility(wparam.0 != 0); }
    if message == WM_NCDESTROY {
        // Only this window's controller: a recreated window (T-P3-07) may already own the slot.
        // The controller is hosted in wry's container child, so compare against it and its parent.
        CONTROLLER.with(|c| {
            let mut slot = c.borrow_mut();
            let mine = slot.as_ref().is_some_and(|controller| {
                let mut parent = windows::Win32::Foundation::HWND::default();
                controller.ParentWindow(&mut parent).is_err() || parent == hwnd
                    || windows::Win32::UI::WindowsAndMessaging::GetParent(parent).is_ok_and(|g| g == hwnd)
            });
            if mine { slot.take(); }
        });
        let _ = RemoveWindowSubclass(hwnd, Some(visibility_proc), VISIBILITY_SUBCLASS);
    }
    DefSubclassProc(hwnd, message, wparam, lparam)
}
/// 11-05 in-page title bar: ask DWM for the rounded corners a captioned window gets on Windows 11
/// (Windows 10 has no such attribute and refuses it; the window stays square there).
fn round_corners(hwnd: windows::Win32::Foundation::HWND) {
    use windows::Win32::Graphics::Dwm::{DwmSetWindowAttribute, DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND};
    let preference = DWMWCP_ROUND;
    let _ = unsafe { DwmSetWindowAttribute(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE,
        std::ptr::from_ref(&preference).cast(), std::mem::size_of_val(&preference) as u32) };
}
/// 11-05 in-page title bar: what the page's window buttons ask for (`host_window{action}`).
#[derive(Debug, PartialEq, Clone, Copy)]
pub enum Caption { Minimize, ToggleMaximize, Close }
impl Caption {
    pub fn parse(action: &str) -> Option<Self> {
        match action { "minimize" => Some(Self::Minimize), "toggle_maximize" => Some(Self::ToggleMaximize), "close" => Some(Self::Close), _ => None }
    }
}
/// The page-facing `host:window{maximized}` of the main window, or None while it is minimized
/// (a minimized window reports not maximized even when it will come back maximized).
pub fn caption_state(window: &tauri::Window) -> Option<serde_json::Value> {
    if window.is_minimized().unwrap_or(true) { return None; }
    window.is_maximized().ok().map(|maximized| serde_json::json!({"t":"host:window","maximized":maximized}))
}
#[derive(Debug, Serialize, Deserialize, Copy, Clone)]
struct Placement { x: i32, y: i32, width: u32, height: u32, scale: f64, maximized: bool }
fn visible(p: Placement, bounds: (i32,i32,u32,u32)) -> bool {
    let (x,y,w,h) = bounds;
    let left = i64::from(p.x).max(i64::from(x));
    let top = i64::from(p.y).max(i64::from(y));
    let right = (i64::from(p.x)+i64::from(p.width)).min(i64::from(x)+i64::from(w));
    let bottom = (i64::from(p.y)+40).min(i64::from(y)+i64::from(h));
    p.scale.is_finite() && (0.5..=8.0).contains(&p.scale) && p.width >= 100 && p.height >= 100
        && right-left >= 100 && bottom-top >= 20
}
fn restore(window: &WebviewWindow, file: &Path) {
    let Some(p) = fs::read(file).ok().filter(|b| b.len() < 4096)
        .and_then(|b| serde_json::from_slice::<Placement>(&b).ok()) else { return; };
    if let Ok(monitors) = window.available_monitors() {
        if monitors.iter().any(|m| visible(p, (m.position().x,m.position().y,m.size().width,m.size().height))) {
            let _ = window.set_position(PhysicalPosition::new(p.x,p.y));
            // Apply logical dimensions against the destination monitor's DPI.
            let _ = window.set_size(tauri::LogicalSize::new((p.width as f64/p.scale).clamp(MIN_WIDTH,3840.0),
                (p.height as f64/p.scale).clamp(MIN_HEIGHT,2160.0)));
            if p.maximized { let _ = window.maximize(); }
        }
    }
}
pub fn persist(window: &WebviewWindow, data: &Path) {
    if window.is_minimized().unwrap_or(true) || window.is_maximized().unwrap_or(false) { return; }
    if let (Ok(pos),Ok(size),Ok(scale)) = (window.outer_position(),window.inner_size(),window.scale_factor()) {
        let p = Placement { x:pos.x,y:pos.y,width:size.width,height:size.height,scale,maximized:false };
        if let Ok(bytes) = serde_json::to_vec(&p) {
            let tmp = data.join("window-placement.pending");
            if fs::write(&tmp,bytes).is_ok() { let _ = fs::rename(tmp,data.join("window-placement.json")); }
        }
    }
}
/// `show`: false for a T-P3-06 `--autostart` start, which shows the window later (or stays in the tray).
/// T-P3-07: the window is DWM-cloaked until the page's `host_ready` (or the 1.5 s timeout), its
/// `ProcessFailed` goes to the supervisor (reload / recreate), and its environment's
/// `BrowserProcessExited` is recorded for the exit wait. `app` may be used off the UI thread
/// (browser-process recreation).
/// T-P6-06: `scope` is the resolved scope id (`default`, or a custom id), so the WebView2 user data
/// folder is `<data>\WebView2\<id>` (`single_instance::profile`) for every scope.
pub fn create(app: &tauri::AppHandle, scope: &str, data: &Path, dev_page: bool, browser_args: String, show: bool,
    supervisor: std::sync::Arc<crate::webview::Supervisor>) -> tauri::Result<WebviewWindow> {
    // Armed before the page can load, so an early host_ready is never lost.
    let generation = supervisor.gate.arm();
    let p = policy(cfg!(debug_assertions));
    let page = if cfg!(debug_assertions) && dev_page { "dev-rt.html" } else { "index.html" };
    let reduced = reduced_motion(std::env::var_os(REDUCED_MOTION_ENV).as_deref());
    crate::shell_log::fields(data, "reduced_motion", &[("reduced", if reduced { "true" } else { "false" })]);
    let window = WebviewWindowBuilder::new(app,"main",WebviewUrl::App(page.into()))
        .title(TITLE).inner_size(WIDTH,HEIGHT).min_inner_size(MIN_WIDTH,MIN_HEIGHT)
        // 11-05 in-page title bar: no system caption; the page draws the window buttons and marks
        // its title band `app-region: drag` (WebView2 non-client regions, which wry turns on), so
        // dragging, double-click, Aero Snap and the system menu stay native. `shadow` keeps the
        // DWM frame (shadow, Win11 rounded corners, resize edges outside the client area).
        .decorations(false).shadow(true).visible(false).background_color(tauri::window::Color(255,255,255,255))
        .data_directory(crate::single_instance::profile(data,scope,crate::single_instance::elevated()?)).devtools(p.devtools).zoom_hotkeys_enabled(p.zoom)
        // Keep Tauri's native drop handler; it consumes files instead of navigating.
        .additional_browser_args(&browser_args).initialization_script(init_script(reduced))
        .on_web_resource_request(|request,response| {
            // Tauri falls unknown assets back to index.html; explicitly deny dev routes in release.
            if !cfg!(debug_assertions) && request.uri().path().trim_start_matches('/').starts_with("dev-rt.") {
                *response.status_mut()=tauri::http::StatusCode::NOT_FOUND;
                *response.body_mut()=std::borrow::Cow::Borrowed(b"");
            }
        })
        .on_navigation(|url| { if internal(url) { true } else { open_external(url); false } })
        .on_new_window(|url,_| { open_external(&url); tauri::webview::NewWindowResponse::Deny })
        .build()?;
    let log_dir = data.to_path_buf();
    let (tx,rx) = std::sync::mpsc::channel();
    let hooks = supervisor.clone();
    window.with_webview(move |webview| {
        let result = (|| -> windows::core::Result<()> {
            unsafe {
                let controller = webview.controller();
                CONTROLLER.with(|c| *c.borrow_mut() = Some(controller.clone()));
                // Fix round 1 (A-S3): wry creates the controller with IsVisible=true whatever the
                // window does; a window built hidden (autostart into the tray, a recreation of a
                // window that was in the tray) gets no WM_SHOWWINDOW, so set it here — the page
                // must not run rAF at full rate in the tray (V2). Showing it later sets it back.
                if !show { apply_visibility(false); }
                let core = controller.CoreWebView2()?;
                let settings = core.Settings()?;
                settings.SetAreDevToolsEnabled(p.devtools.into())?;
                let s3: ICoreWebView2Settings3 = settings.cast()?;
                s3.SetAreBrowserAcceleratorKeysEnabled(p.browser_keys)?;
                settings.SetAreDefaultContextMenusEnabled(p.context_menu.into())?;
                settings.SetIsStatusBarEnabled(p.status_bar.into())?;
                settings.SetIsZoomControlEnabled(p.zoom.into())?;
                let s4: ICoreWebView2Settings4 = settings.cast()?;
                s4.SetIsPasswordAutosaveEnabled(p.autofill)?;
                s4.SetIsGeneralAutofillEnabled(p.autofill)?;
                let s5: ICoreWebView2Settings5 = settings.cast()?;
                s5.SetIsPinchZoomEnabled(p.pinch)?;
                let s6: ICoreWebView2Settings6 = settings.cast()?;
                s6.SetIsSwipeNavigationEnabled(p.swipe)?;
                let c4: ICoreWebView2Controller4 = controller.cast()?;
                c4.SetAllowExternalDrop(p.drop)?;
                // §2.2.6: renderer exited / unresponsive → reload (the page rebuilds from the shell
                // cache through rt_connect); browser exited → recreate the window (main.rs).
                let failed = hooks.clone();
                let handler = webview2_com::ProcessFailedEventHandler::create(Box::new(move |sender: Option<ICoreWebView2>, args: Option<ICoreWebView2ProcessFailedEventArgs>| {
                    let mut kind = COREWEBVIEW2_PROCESS_FAILED_KIND(-1);
                    if let Some(args) = args { let _ = args.ProcessFailedKind(&mut kind); }
                    failed.process_failed(crate::webview::classify(kind.0), || sender.is_some_and(|w| w.Reload().is_ok()), std::time::Instant::now());
                    Ok(())
                }));
                let mut token = 0;
                core.add_ProcessFailed(&handler,&mut token)?;
                // §7.9: the exit path waits for this environment's BrowserProcessExited.
                let environment = core.cast::<ICoreWebView2_2>()?.Environment()?;
                // A-S7: reserved now, made current only after the hook below is registered.
                let browser = hooks.reserve_browser();
                let exited = hooks.clone();
                let handler = webview2_com::BrowserProcessExitedEventHandler::create(Box::new(move |_, args: Option<ICoreWebView2BrowserProcessExitedEventArgs>| {
                    let mut kind = COREWEBVIEW2_BROWSER_PROCESS_EXIT_KIND_NORMAL;
                    if let Some(args) = args { let _ = args.BrowserProcessExitKind(&mut kind); }
                    exited.browser_exited(browser, kind != COREWEBVIEW2_BROWSER_PROCESS_EXIT_KIND_NORMAL);
                    Ok(())
                }));
                let mut token = 0;
                environment.cast::<ICoreWebView2Environment5>()?.add_BrowserProcessExited(&handler,&mut token)?;
                hooks.publish_browser(browser);
                crate::webview::keep_environment(environment);
                let mut pid = 0u32;
                let _ = core.BrowserProcessId(&mut pid);
                crate::shell_log::fields(&log_dir,"webview_browser_started",&[("generation",&browser.to_string()),("pid",&pid.to_string())]);
            } Ok(())
        })();
        let _ = tx.send(result.map_err(|_| "webview_hardening_failed"));
    })?;
    rx.recv_timeout(std::time::Duration::from_secs(5)).map_err(|_| tauri::Error::AssetNotFound("webview_settings_timeout".into()))?
        .map_err(|e| tauri::Error::AssetNotFound(e.into()))?;
    let _ = VISIBILITY_LOG.set(data.to_path_buf());
    let hwnd = windows::Win32::Foundation::HWND(window.hwnd()?.0);
    // SetWindowSubclass only works on the window's own (UI) thread; a T-P3-07 recreation builds
    // the window from another thread (run_on_main_thread runs inline when already on it).
    let (raw, done) = (hwnd.0 as isize, std::sync::mpsc::channel());
    let sent = app.run_on_main_thread(move || {
        let ok = unsafe { windows::Win32::UI::Shell::SetWindowSubclass(windows::Win32::Foundation::HWND(raw as _), Some(visibility_proc), VISIBILITY_SUBCLASS, 0) }.as_bool();
        let _ = done.0.send(ok);
    }).is_ok();
    if !(sent && done.1.recv_timeout(std::time::Duration::from_secs(5)).unwrap_or(false)) {
        crate::log_code(data, "window_visibility_subclass_failed");
    }
    restore(&window,&data.join("window-placement.json"));
    round_corners(hwnd);
    // §2.2.1 step 6: cloaked, not hidden — Win32 and WebView2 see a visible window that renders,
    // DWM does not show it until host_ready (or the timeout; webview::start_reveal).
    if !crate::webview::set_cloak(hwnd.0 as isize, true) { crate::log_code(data, "window_cloak_failed"); }
    if show { window.show()?; persist(&window,data); }
    crate::webview::start_reveal(app.clone(), supervisor, hwnd.0 as isize, generation);
    Ok(window)
}

#[cfg(test)] mod tests {
    use super::*;
    #[test] fn dimensions_and_release_restrictions() {
        assert_eq!((WIDTH,HEIGHT,MIN_WIDTH,MIN_HEIGHT),(1280.0,800.0,1060.0,700.0));
        let p=policy(false);
        assert!(!p.devtools && !p.browser_keys && !p.context_menu && !p.zoom && !p.drop && !p.status_bar && !p.autofill && !p.pinch && !p.swipe);
        assert!(policy(true).devtools);
        assert!(GUARDS.contains("f5") && GUARDS.contains("Files"));
    }
    #[test] fn init_script_carries_motion_and_only_the_env_variable_decides() {
        use std::ffi::OsStr;
        for (value, expected) in [(None, false), (Some("1"), true), (Some("true"), true), (Some(" TRUE "), true), (Some("yes"), true),
            (Some("on"), true), (Some("0"), false), (Some("false"), false), (Some(""), false), (Some("reduce"), false)] {
            assert_eq!(reduced_motion(value.map(OsStr::new)), expected, "{value:?}");
        }
        for reduced in [true, false] {
            let script = init_script(reduced);
            assert!(script.starts_with(GUARDS), "the input guards stay first");
            assert!(script.ends_with(&format!("window.__TESSERACT_MOTION__=Object.freeze({{reduced:{reduced}}});")), "{script}");
            assert_eq!(script.matches("__TESSERACT_MOTION__").count(), 1);
        }
        // The window builder reads exactly this variable and passes the script with it; nothing
        // else in this file reads the environment or the system animation setting.
        let source = include_str!("window.rs");
        let code = &source[..source.find("#[cfg(test)] mod tests").unwrap()];
        assert_eq!(code.matches("env::var").count(), 1);
        assert!(code.contains("std::env::var_os(REDUCED_MOTION_ENV)") && code.contains(".initialization_script(init_script(reduced))"));
        assert!(!code.contains("SPI_GETCLIENTAREAANIMATION") && !code.contains("prefers-reduced-motion"));
        assert_eq!(REDUCED_MOTION_ENV, "TESSERACT_REDUCED_MOTION");
    }
    /// T-P6-06: the title follows the build flavour, and the builder uses the constant.
    #[test] fn title_follows_the_build_flavour() {
        #[cfg(feature = "distribution")] assert_eq!(TITLE, "Tesseract");
        #[cfg(not(feature = "distribution"))] assert_eq!(TITLE, "Tesseract Shell (P3)");
        let source = include_str!("window.rs");
        let code = &source[..source.find("#[cfg(test)] mod tests").unwrap()];
        assert!(code.contains(".title(TITLE)") && code.matches(".title(").count() == 1);
        // The WebView2 folder is the resolved scope id's, never a hash recomputed as "custom" here.
        assert!(code.contains("profile(data,scope,") && !code.contains("custom_id"));
    }
    /// 11-05 in-page title bar: no system caption (the page draws it), the DWM shadow and frame stay,
    /// and the page's window buttons name exactly three actions.
    #[test] fn captionless_window_and_its_three_actions() {
        let source = include_str!("window.rs");
        let code = &source[..source.find("#[cfg(test)] mod tests").unwrap()];
        assert!(code.contains(".decorations(false).shadow(true)") && !code.contains(".decorations(true)"));
        assert!(code.contains("round_corners(hwnd);"));
        assert_eq!(Caption::parse("minimize"), Some(Caption::Minimize));
        assert_eq!(Caption::parse("toggle_maximize"), Some(Caption::ToggleMaximize));
        assert_eq!(Caption::parse("close"), Some(Caption::Close));
        for bad in ["", "maximize", "Close", "close ", "quit", "hide"] { assert_eq!(Caption::parse(bad), None, "{bad:?}"); }
    }
    #[test] fn navigation_origin_is_exact() {
        for s in ["http://tauri.localhost/index.html","http://tauri.localhost/dev-rt.html"] { assert!(internal(&s.parse().unwrap())); }
        for s in ["https://example.org","file:///C:/test","http://tauri.localhost.evil/","http://tauri.localhost:42","http://user@tauri.localhost"] { assert!(!internal(&s.parse().unwrap())); }
    }
    #[test] fn visibility_transitions_are_recorded_once() {
        assert_eq!(transition(false,true),Some("window_shown"));
        assert_eq!(transition(true,false),Some("window_hidden"));
        assert_eq!(transition(true,true),None); assert_eq!(transition(false,false),None);
    }
    #[test] fn monitor_removal_and_negative_coordinates() {
        let p=Placement{x:-1280,y:0,width:1280,height:800,scale:1.0,maximized:false};
        assert!(visible(p,(-1280,0,1280,1024)));
        assert!(!visible(p,(0,0,1920,1080)));
        assert!(!visible(Placement{scale:f64::NAN,..p},(-1280,0,1280,1024)));
    }
    #[test] fn csp_and_capability_configuration() {
        let config: serde_json::Value=serde_json::from_str(include_str!("../tauri.conf.json")).unwrap();
        let csp=config["app"]["security"]["csp"].as_str().unwrap();
        assert!(csp.contains("default-src 'self'") && !csp.contains("unsafe-eval"));
        // T-P4-08b (P4 architecture §9): no inline styles or scripts; the page styles itself through
        // stylesheets and the CSSOM only
        assert!(!csp.contains("unsafe-inline") && csp.contains("style-src 'self';") && csp.contains("script-src 'self';"),"{csp}");
        let cap: serde_json::Value=serde_json::from_str(include_str!("../capabilities/main.json")).unwrap();
        assert!(cap.get("remote").is_none());
        assert_eq!(cap["windows"],serde_json::json!(["main"]));
    }
}
