//! A message-only window that receives the installer's progress.
//!
//! The elevated Inno package is told this window's handle (`/TSPROGRESS=<hwnd>`) and posts
//! `WM_APP + 0x54` with wParam = stage, lParam = permille (Tesseract-shell.iss, [Code]).
//! A higher-integrity process may post to a lower one, so the elevation does not block it.
//! Anything else that posts here can only move the progress shown.
use std::sync::{mpsc, Mutex, OnceLock};
use windows::{
    core::w,
    Win32::{
        Foundation::{HWND, LPARAM, LRESULT, WPARAM},
        System::LibraryLoader::GetModuleHandleW,
        UI::WindowsAndMessaging::{
            CreateWindowExW, DefWindowProcW, DispatchMessageW, GetMessageW, RegisterClassW, HWND_MESSAGE, MSG, WINDOW_EX_STYLE, WINDOW_STYLE,
            WM_APP, WNDCLASSW,
        },
    },
};

pub const WM_TS_PROGRESS: u32 = WM_APP + 0x54;

type Sink = Box<dyn Fn(u32, u32) + Send>;
static SINK: OnceLock<Mutex<Sink>> = OnceLock::new();

unsafe extern "system" fn proc(hwnd: HWND, msg: u32, wp: WPARAM, lp: LPARAM) -> LRESULT {
    if msg == WM_TS_PROGRESS {
        if let Some(sink) = SINK.get() { if let Ok(f) = sink.lock() { f(wp.0 as u32, lp.0 as u32); } }
        return LRESULT(0);
    }
    DefWindowProcW(hwnd, msg, wp, lp)
}

/// Starts the window on its own thread; returns its handle (0 if it could not be created,
/// then the installer runs without progress and the bar waits at its first step).
pub fn spawn(sink: impl Fn(u32, u32) + Send + 'static) -> isize {
    let _ = SINK.set(Mutex::new(Box::new(sink)));
    let (tx, rx) = mpsc::channel();
    std::thread::spawn(move || unsafe {
        let Ok(module) = GetModuleHandleW(None) else { let _ = tx.send(0); return; };
        let class = w!("TesseractSetupProgress");
        let wc = WNDCLASSW { lpfnWndProc: Some(proc), hInstance: module.into(), lpszClassName: class, ..Default::default() };
        RegisterClassW(&wc);
        let hwnd = CreateWindowExW(WINDOW_EX_STYLE(0), class, w!(""), WINDOW_STYLE(0), 0, 0, 0, 0, Some(HWND_MESSAGE), None, Some(module.into()), None);
        let Ok(hwnd) = hwnd else { let _ = tx.send(0); return; };
        let _ = tx.send(hwnd.0 as isize);
        let mut msg = MSG::default();
        while GetMessageW(&mut msg, None, 0, 0).as_bool() { DispatchMessageW(&msg); }
    });
    rx.recv().unwrap_or(0)
}
