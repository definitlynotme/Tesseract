//! A window of our own for the in-app updater's hand-over while it waits for elevation (0.1.6).
//!
//! The hand-over runs the Inno package with no window of ours, so its elevation had no owner, and
//! Windows shows a consent request from a process without a foreground window only as a flashing
//! taskbar button. Field report 11-22: Tesseract vanished for the update, no prompt came up, the
//! user opened 0.1.4 again after 50 s and its retry failed on the installer file the waiting Setup
//! still held (更新操作失败。检查网络… on a working network). This window comes to the foreground
//! (the app that started this process had it), owns the elevation so the prompt opens on the secure
//! desktop, and says what to do if it still only flashes. It closes once the elevation is answered:
//! Inno's own /SILENT progress window shows the install from there.
use std::{sync::mpsc, thread::JoinHandle};
use windows::{
    core::{w, PCWSTR},
    Win32::{
        Foundation::{COLORREF, HWND, LPARAM, LRESULT, RECT, WPARAM},
        Graphics::Gdi::{
            BeginPaint, CreateFontIndirectW, DeleteObject, DrawTextW, EndPaint, FillRect, GetSysColor, GetSysColorBrush,
            SelectObject, SetBkMode, SetTextColor, COLOR_WINDOW, COLOR_WINDOWTEXT, DT_LEFT, DT_NOPREFIX, DT_WORDBREAK, FW_SEMIBOLD,
            HFONT, HGDIOBJ, PAINTSTRUCT, TRANSPARENT,
        },
        System::LibraryLoader::GetModuleHandleW,
        UI::WindowsAndMessaging::{
            CreateWindowExW, DefWindowProcW, DestroyWindow, DispatchMessageW, GetClientRect, GetMessageW, LoadCursorW, PostMessageW,
            PostQuitMessage, RegisterClassW, SetForegroundWindow, ShowWindow, SystemParametersInfoW, TranslateMessage, IDC_ARROW, MSG,
            NONCLIENTMETRICSW, SPI_GETNONCLIENTMETRICS, SPI_GETWORKAREA, SW_SHOWNORMAL, SYSTEM_PARAMETERS_INFO_UPDATE_FLAGS, WM_APP,
            WM_CLOSE, WM_DESTROY, WM_PAINT, WNDCLASSW, WS_CAPTION, WS_EX_DLGMODALFRAME, WS_OVERLAPPED,
        },
    },
};

/// Destroys the window from its own thread (DestroyWindow only works there).
const WM_HANDOVER_DONE: u32 = WM_APP + 0x31;

/// (heading, what to do) in the app's nine languages, else English (sys::ui_language).
pub fn text(lang: &str) -> (&'static str, &'static str) {
    match lang {
        "zh_CN" => ("正在更新 Tesseract…", "请在「用户帐户控制」中选择「是」。如果没有看到它，点任务栏上闪烁的按钮。"),
        "ja_JP" => ("Tesseract を更新しています…", "ユーザー アカウント制御で［はい］を選んでください。表示されない場合は、タスク バーで点滅しているボタンをクリックしてください。"),
        "ko_KR" => ("Tesseract 업데이트 중…", "사용자 계정 컨트롤에서 [예]를 선택하세요. 보이지 않으면 작업 표시줄에서 깜박이는 단추를 클릭하세요."),
        "de_DE" => ("Tesseract wird aktualisiert…", "Wählen Sie in der Benutzerkontensteuerung „Ja“. Wenn sie nicht zu sehen ist, klicken Sie auf die blinkende Schaltfläche in der Taskleiste."),
        "es_ES" => ("Actualizando Tesseract…", "Elige Sí en Control de cuentas de usuario. Si no lo ves, haz clic en el botón que parpadea en la barra de tareas."),
        "fr_FR" => ("Mise à jour de Tesseract…", "Choisissez Oui dans le Contrôle de compte d’utilisateur. S’il n’apparaît pas, cliquez sur le bouton qui clignote dans la barre des tâches."),
        "pt_BR" => ("Atualizando o Tesseract…", "Escolha Sim no Controle de Conta de Usuário. Se ele não aparecer, clique no botão que pisca na barra de tarefas."),
        "ru_RU" => ("Обновление Tesseract…", "Выберите «Да» в окне контроля учётных записей. Если его не видно, нажмите мигающую кнопку на панели задач."),
        _ => ("Updating Tesseract…", "Choose Yes in User Account Control. If you don't see it, click the flashing button on the taskbar."),
    }
}

/// The window while it lives; dropping it closes it and waits for its thread.
pub struct Window {
    hwnd: isize,
    thread: Option<JoinHandle<()>>,
}

impl Window {
    /// The elevation's owner (0 when the window could not be created: then the hand-over runs as
    /// before 0.1.6, with no owner).
    pub fn owner(&self) -> isize { self.hwnd }
}

impl Drop for Window {
    fn drop(&mut self) {
        if self.hwnd != 0 {
            let _ = unsafe { PostMessageW(Some(HWND(self.hwnd as _)), WM_HANDOVER_DONE, WPARAM(0), LPARAM(0)) };
        }
        if let Some(thread) = self.thread.take() { let _ = thread.join(); }
    }
}

struct Fonts { heading: HFONT, body: HFONT }

static TEXT: std::sync::OnceLock<(Vec<u16>, Vec<u16>)> = std::sync::OnceLock::new();
static FONTS: std::sync::OnceLock<(isize, isize)> = std::sync::OnceLock::new();

unsafe extern "system" fn proc(hwnd: HWND, msg: u32, wp: WPARAM, lp: LPARAM) -> LRESULT {
    match msg {
        WM_PAINT => {
            let mut ps = PAINTSTRUCT::default();
            let hdc = BeginPaint(hwnd, &mut ps);
            let mut client = RECT::default();
            let _ = GetClientRect(hwnd, &mut client);
            FillRect(hdc, &client, GetSysColorBrush(COLOR_WINDOW));
            SetBkMode(hdc, TRANSPARENT);
            SetTextColor(hdc, COLORREF(GetSysColor(COLOR_WINDOWTEXT)));
            if let (Some((heading, body)), Some(&(heading_font, body_font))) = (TEXT.get(), FONTS.get()) {
                let pad = (client.bottom - client.top) / 6;
                let mut top = RECT { left: client.left + pad, top: client.top + pad, right: client.right - pad, bottom: client.bottom };
                let old = SelectObject(hdc, HGDIOBJ(heading_font as _));
                let height = DrawTextW(hdc, &mut heading.clone(), &mut top, DT_LEFT | DT_NOPREFIX | DT_WORDBREAK);
                SelectObject(hdc, HGDIOBJ(body_font as _));
                let mut rest = RECT { top: top.top + height + pad / 2, ..top };
                DrawTextW(hdc, &mut body.clone(), &mut rest, DT_LEFT | DT_NOPREFIX | DT_WORDBREAK);
                SelectObject(hdc, old);
            }
            let _ = EndPaint(hwnd, &ps);
            LRESULT(0)
        }
        // No close button, and Alt+F4 does not end it: it goes when the elevation is answered.
        WM_CLOSE => LRESULT(0),
        WM_HANDOVER_DONE => { let _ = DestroyWindow(hwnd); LRESULT(0) }
        WM_DESTROY => { PostQuitMessage(0); LRESULT(0) }
        _ => DefWindowProcW(hwnd, msg, wp, lp),
    }
}

fn wide(text: &str) -> Vec<u16> { text.encode_utf16().collect() }

/// The message font of this computer (its height decides the window's size, at any DPI), and a
/// larger semibold one for the heading.
unsafe fn fonts() -> Option<Fonts> {
    let mut metrics = NONCLIENTMETRICSW { cbSize: std::mem::size_of::<NONCLIENTMETRICSW>() as u32, ..Default::default() };
    SystemParametersInfoW(SPI_GETNONCLIENTMETRICS, metrics.cbSize, Some(&mut metrics as *mut _ as *mut _),
        SYSTEM_PARAMETERS_INFO_UPDATE_FLAGS(0)).ok()?;
    let body = metrics.lfMessageFont;
    let mut heading = body;
    heading.lfHeight = body.lfHeight.saturating_mul(4) / 3;
    heading.lfWeight = FW_SEMIBOLD.0 as i32;
    let (heading, body) = (CreateFontIndirectW(&heading), CreateFontIndirectW(&body));
    if heading.is_invalid() || body.is_invalid() {
        let _ = DeleteObject(HGDIOBJ(heading.0)); let _ = DeleteObject(HGDIOBJ(body.0));
        return None;
    }
    Some(Fonts { heading, body })
}

/// Shows the window on its own thread and brings it to the foreground. Never fails the update:
/// without a window the hand-over goes on with no owner, as before.
pub fn show(lang: &str) -> Window {
    let (heading, body) = text(lang);
    let _ = TEXT.set((wide(heading), wide(body)));
    let (tx, rx) = mpsc::channel();
    let thread = std::thread::spawn(move || unsafe {
        let Ok(module) = GetModuleHandleW(None) else { let _ = tx.send(0); return; };
        let Some(fonts) = fonts() else { let _ = tx.send(0); return; };
        let _ = FONTS.set((fonts.heading.0 as isize, fonts.body.0 as isize));
        let class = w!("TesseractSetupHandover");
        let wc = WNDCLASSW {
            lpfnWndProc: Some(proc),
            hInstance: module.into(),
            lpszClassName: class,
            hCursor: LoadCursorW(None, IDC_ARROW).unwrap_or_default(),
            ..Default::default()
        };
        RegisterClassW(&wc);
        // Sized by the message font: about 34 by 12 lines of it (the caption included; German needs
        // three body lines), centred on the work area.
        let line = {
            let mut metrics = NONCLIENTMETRICSW { cbSize: std::mem::size_of::<NONCLIENTMETRICSW>() as u32, ..Default::default() };
            let _ = SystemParametersInfoW(SPI_GETNONCLIENTMETRICS, metrics.cbSize, Some(&mut metrics as *mut _ as *mut _),
                SYSTEM_PARAMETERS_INFO_UPDATE_FLAGS(0));
            metrics.lfMessageFont.lfHeight.unsigned_abs().max(12) as i32
        };
        let (width, height) = (line * 34, line * 12);
        let mut area = RECT::default();
        let _ = SystemParametersInfoW(SPI_GETWORKAREA, 0, Some(&mut area as *mut _ as *mut _), SYSTEM_PARAMETERS_INFO_UPDATE_FLAGS(0));
        let x = area.left + ((area.right - area.left) - width).max(0) / 2;
        let y = area.top + ((area.bottom - area.top) - height).max(0) / 2;
        let title = wide("Tesseract").into_iter().chain(Some(0)).collect::<Vec<u16>>();
        let hwnd = CreateWindowExW(WS_EX_DLGMODALFRAME, class, PCWSTR(title.as_ptr()), WS_OVERLAPPED | WS_CAPTION,
            x, y, width, height, None, None, Some(module.into()), None);
        let Ok(hwnd) = hwnd else {
            let _ = DeleteObject(HGDIOBJ(fonts.heading.0)); let _ = DeleteObject(HGDIOBJ(fonts.body.0));
            let _ = tx.send(0);
            return;
        };
        let _ = ShowWindow(hwnd, SW_SHOWNORMAL);
        // This process was started by the app the user just clicked 立即更新 in, so it may take
        // the foreground; the elevation prompt then opens at once instead of flashing.
        let _ = SetForegroundWindow(hwnd);
        let _ = tx.send(hwnd.0 as isize);
        let mut msg = MSG::default();
        while GetMessageW(&mut msg, None, 0, 0).as_bool() {
            let _ = TranslateMessage(&msg);
            DispatchMessageW(&msg);
        }
        let _ = DeleteObject(HGDIOBJ(fonts.heading.0));
        let _ = DeleteObject(HGDIOBJ(fonts.body.0));
    });
    let hwnd = rx.recv().unwrap_or(0);
    Window { hwnd, thread: Some(thread) }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_language_has_its_own_text_and_others_get_english() {
        let langs = ["zh_CN", "ja_JP", "ko_KR", "de_DE", "es_ES", "fr_FR", "pt_BR", "ru_RU", "en_US"];
        let texts: Vec<_> = langs.iter().map(|l| text(l)).collect();
        for (lang, (heading, body)) in langs.iter().zip(&texts) {
            assert!(heading.contains("Tesseract") && !body.is_empty(), "{lang}");
        }
        for (i, a) in texts.iter().enumerate() {
            for b in &texts[i + 1..] { assert_ne!(a, b); }
        }
        assert_eq!(text("it_IT"), text("en_US"));
    }

    /// Opens the real window on this desktop (`cargo test -- --ignored`), for HANDOVER_SHOW_MS
    /// (default 2000) in HANDOVER_LANG (default zh_CN).
    #[test]
    #[ignore = "shows a window on the desktop"]
    fn the_window_opens_owns_and_closes() {
        let lang = std::env::var("HANDOVER_LANG").unwrap_or_else(|_| "zh_CN".into());
        let window = show(&lang);
        assert_ne!(window.owner(), 0);
        let ms = std::env::var("HANDOVER_SHOW_MS").ok().and_then(|v| v.parse().ok()).unwrap_or(2000);
        std::thread::sleep(std::time::Duration::from_millis(ms));
        drop(window);
    }
}
