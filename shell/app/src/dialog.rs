//! Native dialogs of the quit/crash flows. Each blocks only its own (spawned) thread.
//! TaskDialogIndirect is resolved at run time from comctl32 v6 (the exe manifest activates it),
//! so a host without it falls back to MessageBoxW instead of failing to load.
use crate::native_text::{self, text};
use std::{path::Path, sync::{atomic::{AtomicBool, Ordering}, Arc}};
use windows::core::{BOOL, HRESULT, PCWSTR};
use windows::Win32::Foundation::{HWND, LPARAM, S_OK, WPARAM};
use windows::Win32::System::LibraryLoader::{GetProcAddress, LoadLibraryW};
use windows::Win32::UI::Controls::*;
use windows::Win32::UI::WindowsAndMessaging::*;

const ID_FIRST: i32 = 100;
const ID_SECOND: i32 = 101;
type TaskDialogIndirectFn = unsafe extern "system" fn(*const TASKDIALOGCONFIG, *mut i32, *mut i32, *mut BOOL) -> HRESULT;

fn wide(s: &str) -> Vec<u16> { s.encode_utf16().chain(Some(0)).collect() }
fn task_dialog() -> Option<TaskDialogIndirectFn> {
    unsafe {
        let module = LoadLibraryW(windows::core::w!("comctl32.dll")).ok()?;
        let proc = GetProcAddress(module, windows::core::s!("TaskDialogIndirect"))?;
        Some(std::mem::transmute::<unsafe extern "system" fn() -> isize, TaskDialogIndirectFn>(proc))
    }
}
/// Callback state. `dismiss` is polled by the stall dialog's ~200 ms timer: set → click
/// "keep waiting" and close.
struct Callback { dismiss: Option<Arc<AtomicBool>> }
/// Brings the dialog up visible, topmost and in front. These dialogs have no owner window (the
/// pre-hello one is the process's first window), and a launcher's STARTUPINFO `SW_HIDE` (a
/// hidden or scripted start) would otherwise be applied to that first ShowWindow: the first
/// call here consumes it, the second shows the dialog.
unsafe fn surface(hwnd: HWND) {
    let _ = ShowWindow(hwnd, SW_SHOWNORMAL);
    let _ = ShowWindow(hwnd, SW_SHOWNORMAL);
    let _ = SetWindowPos(hwnd, Some(HWND_TOPMOST), 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW);
    let _ = SetForegroundWindow(hwnd);
}
unsafe extern "system" fn callback(hwnd: HWND, msg: TASKDIALOG_NOTIFICATIONS, _: WPARAM, _: LPARAM, data: isize) -> HRESULT {
    let state = (data != 0).then(|| &*(data as *const Callback));
    if msg == TDN_CREATED { surface(hwnd); }
    if msg == TDN_TIMER {
        // Belt and braces: the first-show rule, or the dialog's own repositioning after
        // TDN_CREATED, must never leave a modal question hidden or behind other windows.
        let topmost = GetWindowLongPtrW(hwnd, GWL_EXSTYLE) as u32 & WS_EX_TOPMOST.0 != 0;
        if !IsWindowVisible(hwnd).as_bool() || !topmost { surface(hwnd); }
        if state.and_then(|s| s.dismiss.as_ref()).is_some_and(|d| d.load(Ordering::Acquire)) {
            let _ = SendMessageW(hwnd, TDM_CLICK_BUTTON.0 as u32, Some(WPARAM(ID_FIRST as usize)), Some(LPARAM(0)));
        }
    }
    S_OK
}
/// Two custom buttons (`first` is the safe default). Returns Some(true) for `second`,
/// Some(false) for `first`/close, None when the task dialog is unavailable.
fn two_buttons(locale: &str, icon: PCWSTR, body: &str, footer: Option<&str>, first: &str, second: &str, dismiss: Option<Arc<AtomicBool>>) -> Option<bool> {
    two_buttons_owned(None, locale, icon, body, footer, first, second, dismiss)
}
/// [`two_buttons`] with an owner window (modal to it), e.g. the main window for `host.openUrl`.
#[allow(clippy::too_many_arguments)]
fn two_buttons_owned(parent: Option<HWND>, locale: &str, icon: PCWSTR, body: &str, footer: Option<&str>, first: &str, second: &str, dismiss: Option<Arc<AtomicBool>>) -> Option<bool> {
    let show = task_dialog()?;
    let (title, body, first, second) = (wide(text("title", locale)), wide(body), wide(first), wide(second));
    let footer = footer.map(wide);
    let buttons = [TASKDIALOG_BUTTON { nButtonID: ID_FIRST, pszButtonText: PCWSTR(first.as_ptr()) },
        TASKDIALOG_BUTTON { nButtonID: ID_SECOND, pszButtonText: PCWSTR(second.as_ptr()) }];
    let state = Box::new(Callback { dismiss });
    let mut config = TASKDIALOGCONFIG { cbSize: std::mem::size_of::<TASKDIALOGCONFIG>() as u32, ..Default::default() };
    config.dwFlags = TDF_ALLOW_DIALOG_CANCELLATION | TDF_SIZE_TO_CONTENT | TDF_CALLBACK_TIMER;
    if let Some(parent) = parent { config.hwndParent = parent; }
    config.pszWindowTitle = PCWSTR(title.as_ptr());
    config.Anonymous1.pszMainIcon = icon;
    config.pszContent = PCWSTR(body.as_ptr());
    config.cButtons = 2; config.pButtons = buttons.as_ptr(); config.nDefaultButton = ID_FIRST;
    if let Some(footer) = &footer { config.pszFooter = PCWSTR(footer.as_ptr()); }
    config.pfCallback = Some(callback); config.lpCallbackData = &*state as *const Callback as isize;
    let mut pressed = 0;
    let ok = unsafe { show(&config, &mut pressed, std::ptr::null_mut(), std::ptr::null_mut()) }.is_ok();
    drop(state);
    ok.then_some(pressed == ID_SECOND)
}
fn message_box(locale: &str, body: &str, style: MESSAGEBOX_STYLE) -> MESSAGEBOX_RESULT {
    let (title, body) = (wide(text("title", locale)), wide(body));
    unsafe { MessageBoxW(None, PCWSTR(body.as_ptr()), PCWSTR(title.as_ptr()), style | MB_SETFOREGROUND | MB_TOPMOST) }
}

/// 「有 N 项更改没保存，仍然退出？」 — true means quit anyway. Default button: No.
pub fn unsaved(locale: &str, count: u32) -> bool {
    let body = native_text::fill("quit_unsaved", locale, &[("count", &count.to_string())]);
    message_box(locale, &body, MB_YESNO | MB_ICONWARNING | MB_DEFBUTTON2) == IDYES
}
/// 20 s without quit progress. true = force quit (network settings may not be restored).
/// `dismiss` closes the dialog as "keep waiting" once the quit finishes by itself.
pub fn stall(locale: &str, data: &Path, dismiss: Arc<AtomicBool>) -> bool {
    let dir = data.display().to_string();
    let body = text("quit_stall", locale).to_owned();
    let footer = native_text::fill("quit_stall_scripts", locale, &[("dir", &dir)]);
    match two_buttons(locale, TD_WARNING_ICON, &body, Some(&footer), text("quit_stall_wait", locale), text("quit_stall_force", locale), Some(dismiss.clone())) {
        Some(force) => force && !dismiss.load(Ordering::Acquire),
        None => {
            let body = format!("{body}\n\n{footer}\n\n{}", text("quit_stall_fallback", locale));
            message_box(locale, &body, MB_YESNO | MB_ICONWARNING | MB_DEFBUTTON2) == IDYES && !dismiss.load(Ordering::Acquire)
        },
    }
}
/// T-P3-06 `host.openUrl`: 「将在浏览器中打开 <host>」. Only the whitelisted host is shown, never the
/// full link (it may carry a pairing token). true = open; the default button and closing cancel.
/// `owner` (the main window, 0 when unknown) owns the box, so it is modal to the page (review S6).
pub fn confirm_url(locale: &str, host: &str, owner: isize) -> bool {
    let body = native_text::fill("open_url_confirm", locale, &[("host", host)]);
    let parent = (owner != 0).then_some(HWND(owner as _));
    match two_buttons_owned(parent, locale, TD_INFORMATION_ICON, &body, None, text("cancel", locale), text("open_url_open", locale), None) {
        Some(open) => open,
        None => {
            let (title, body) = (wide(text("title", locale)), wide(&body));
            unsafe { MessageBoxW(parent, PCWSTR(body.as_ptr()), PCWSTR(title.as_ptr()), MB_OKCANCEL | MB_ICONINFORMATION | MB_DEFBUTTON2 | MB_SETFOREGROUND | MB_TOPMOST) == IDOK }
        },
    }
}
/// One custom button (T-P3-07 「退出」). None when the task dialog is unavailable.
fn one_button(locale: &str, icon: PCWSTR, body: &str, button: &str) -> Option<()> {
    let show = task_dialog()?;
    let (title, body, button) = (wide(text("title", locale)), wide(body), wide(button));
    let buttons = [TASKDIALOG_BUTTON { nButtonID: ID_FIRST, pszButtonText: PCWSTR(button.as_ptr()) }];
    let state = Box::new(Callback { dismiss: None });
    let mut config = TASKDIALOGCONFIG { cbSize: std::mem::size_of::<TASKDIALOGCONFIG>() as u32, ..Default::default() };
    config.dwFlags = TDF_ALLOW_DIALOG_CANCELLATION | TDF_SIZE_TO_CONTENT | TDF_CALLBACK_TIMER;
    config.pszWindowTitle = PCWSTR(title.as_ptr());
    config.Anonymous1.pszMainIcon = icon;
    config.pszContent = PCWSTR(body.as_ptr());
    config.cButtons = 1; config.pButtons = buttons.as_ptr(); config.nDefaultButton = ID_FIRST;
    config.pfCallback = Some(callback); config.lpCallbackData = &*state as *const Callback as isize;
    let mut pressed = 0;
    let ok = unsafe { show(&config, &mut pressed, std::ptr::null_mut(), std::ptr::null_mut()) }.is_ok();
    drop(state);
    ok.then_some(())
}
/// T-P3-07 (§2.2.1 step 3, pre-hello language §7.10): no WebView2 runtime. 「退出」 (default) or
/// 「打开下载页」; true = open `url` (the caller opens it only through the host.openUrl whitelist).
pub fn webview2_missing(locale: &str, url: &str) -> bool {
    let body = text("webview2_missing", locale);
    let footer = native_text::fill("webview2_missing_link", locale, &[("url", url)]);
    match two_buttons(locale, TD_ERROR_ICON, body, Some(&footer), text("tray_quit", locale), text("webview2_open_download", locale), None) {
        Some(open) => open,
        None => { message_box(locale, &format!("{body}\n\n{footer}"), MB_OK | MB_ICONERROR); false },
    }
}
/// T-P3-07 (§2.2.6): the browser process is gone and the window could not be recreated. One
/// button, 「退出」; the caller then runs the normal quit (network restore first).
pub fn webview_lost(locale: &str) {
    let body = text("webview_lost", locale);
    if one_button(locale, TD_ERROR_ICON, body, text("tray_quit", locale)).is_none() { message_box(locale, body, MB_OK | MB_ICONERROR); }
}
/// Startup failed before `hello`: redacted stderr tail plus 「打开日志目录」. Display only.
/// `kind` picks the wording: the sidecar exited, or the shell stopped it (handshake timeout or
/// contract mismatch).
pub fn sidecar_before_hello(locale: &str, data: &Path, kind: crate::crash::PreHello, tail: Option<Vec<String>>) {
    let lead = match kind { crate::crash::PreHello::Exited => "sidecar_prehello", crate::crash::PreHello::Handshake => "sidecar_prehello_handshake" };
    let mut body = text(lead, locale).to_owned();
    match tail {
        Some(lines) if !lines.is_empty() => { body.push_str("\n\n"); body.push_str(text("sidecar_prehello_tail", locale)); body.push('\n'); body.push_str(&lines.join("\n")); },
        _ => { body.push_str("\n\n"); body.push_str(text("sidecar_prehello_nolog", locale)); },
    }
    let open = match two_buttons(locale, TD_ERROR_ICON, &body, None, text("close", locale), text("open_logs", locale), None) {
        Some(open) => open,
        None => message_box(locale, &format!("{body}\n\n{}", text("open_logs_question", locale)), MB_YESNO | MB_ICONERROR) == IDYES,
    };
    if open {
        let logs = data.join("logs"); let _ = std::fs::create_dir_all(&logs);
        let path = windows::core::HSTRING::from(logs.as_os_str());
        unsafe { windows::Win32::UI::Shell::ShellExecuteW(None, windows::core::w!("open"), &path, None, None, SW_SHOWNORMAL); }
    }
}
