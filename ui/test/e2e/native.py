"""T-P4-11 native helper for the end-to-end flows (Win32 only; called by driver.mjs).

    python -B ui/test/e2e/native.py <action> --pid <shell pid> [--button <id>]

Acts only on windows owned by the given debug shell process (a custom-scope test shell started by
scripts/p4_ui_e2e.ps1): its main window (title "Tesseract Shell (P3)"), its tray window (class
TesseractShellTray) and its native boxes (#32770). Prints one JSON object.
  state          main / tray handles, visible, iconic, DWM cloaked, foreground, tray icon
                 registered with Explorer (Shell_NotifyIconGetRect), visible native boxes with
                 their static text and buttons
  close          WM_CLOSE to the main window (closes to the tray)
  restore        tray callback NIN_SELECT (v4, the left click / 「显示」 path)
  balloon_click  tray callback NIN_BALLOONUSERCLICK (a click on the shell's last balloon)
  tray_quit      WM_COMMAND 2 to the tray window (the tray menu's 「退出」)
  click          WM_COMMAND <button> to the shell's visible native box (MessageBox ids: 6 yes, 7 no)
  foreground     asks Windows to bring the main window to the front; reports whether it is
"""
import argparse
import ctypes
from ctypes import wintypes
import json
import time

user32 = ctypes.WinDLL('user32', use_last_error=True)
shell32 = ctypes.WinDLL('shell32')
dwmapi = ctypes.WinDLL('dwmapi')
kernel32 = ctypes.WinDLL('kernel32')
EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [EnumProc, wintypes.LPARAM]
user32.EnumChildWindows.argtypes = [wintypes.HWND, EnumProc, wintypes.LPARAM]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetDlgCtrlID.argtypes = [wintypes.HWND]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.SwitchToThisWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.GetForegroundWindow.restype = wintypes.HWND
dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]

WM_CLOSE, WM_COMMAND, WM_APP = 0x10, 0x111, 0x8000
TRAY_CALLBACK, NIN_SELECT, NIN_BALLOONUSERCLICK = WM_APP + 1, 0x400, 0x405
MAIN_TITLE, TRAY_CLASS, DIALOG_CLASS = 'Tesseract Shell (P3)', 'TesseractShellTray', '#32770'


class NotifyIconIdentifier(ctypes.Structure):
    _fields_ = [('cbSize', wintypes.DWORD), ('hWnd', wintypes.HWND), ('uID', wintypes.UINT), ('guidItem', ctypes.c_byte * 16)]


shell32.Shell_NotifyIconGetRect.argtypes = [ctypes.POINTER(NotifyIconIdentifier), ctypes.POINTER(wintypes.RECT)]


def text_of(hwnd, getter=user32.GetWindowTextW):
    buf = ctypes.create_unicode_buffer(1024)
    getter(hwnd, buf, 1024)
    return buf.value


def windows(pid):
    found = []

    def visit(hwnd, _):
        owner = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid:
            found.append(hwnd)
        return True
    user32.EnumWindows(EnumProc(visit), 0)
    return found


def classify(pid):
    main = tray = None
    dialogs = []
    for hwnd in windows(pid):
        cls = text_of(hwnd, user32.GetClassNameW)
        if cls == TRAY_CLASS:
            tray = hwnd
        elif cls == DIALOG_CLASS and user32.IsWindowVisible(hwnd):
            dialogs.append(hwnd)
        elif text_of(hwnd) == MAIN_TITLE and main is None:
            main = hwnd
    return main, tray, dialogs


def dialog_facts(hwnd):
    texts, buttons = [], []

    def visit(child, _):
        cls = text_of(child, user32.GetClassNameW)
        if cls == 'Static' and text_of(child):
            texts.append(text_of(child))
        elif cls == 'Button':
            buttons.append({'id': user32.GetDlgCtrlID(child), 'text': text_of(child)})
        return True
    user32.EnumChildWindows(hwnd, EnumProc(visit), 0)
    return {'hwnd': hwnd, 'title': text_of(hwnd), 'text': '\n'.join(texts), 'buttons': buttons}


def cloaked(hwnd):
    value = ctypes.c_int(0)
    ok = dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(value), 4) == 0
    return value.value if ok else -1


def tray_icon(tray):
    if not tray:
        return False
    ident = NotifyIconIdentifier()
    ident.cbSize, ident.hWnd, ident.uID = ctypes.sizeof(NotifyIconIdentifier), tray, 1
    rect = wintypes.RECT()
    return shell32.Shell_NotifyIconGetRect(ctypes.byref(ident), ctypes.byref(rect)) == 0


def state(pid):
    main, tray, dialogs = classify(pid)
    fg = user32.GetForegroundWindow()
    return {'main': main, 'tray': tray, 'visible': bool(main and user32.IsWindowVisible(main)),
            'iconic': bool(main and user32.IsIconic(main)), 'cloaked': cloaked(main) if main else None,
            'foreground': bool(main and fg == main), 'tray_icon': tray_icon(tray),
            'dialogs': [dialog_facts(h) for h in dialogs]}


def post(hwnd, msg, wparam, lparam):
    if not hwnd:
        return False
    return bool(user32.PostMessageW(hwnd, msg, wparam, lparam))


def foreground(main):
    if not main:
        return False
    if user32.GetForegroundWindow() == main:
        return True
    current = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
    mine = kernel32.GetCurrentThreadId()
    attached = current and current != mine and user32.AttachThreadInput(mine, current, True)
    try:
        user32.BringWindowToTop(main)
        user32.SetForegroundWindow(main)
    finally:
        if attached:
            user32.AttachThreadInput(mine, current, False)
    time.sleep(.2)
    if user32.GetForegroundWindow() != main:
        user32.SwitchToThisWindow(main, True)
        time.sleep(.3)
    return user32.GetForegroundWindow() == main


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['state', 'close', 'restore', 'balloon_click', 'tray_quit', 'click', 'foreground'])
    parser.add_argument('--pid', type=int, required=True)
    parser.add_argument('--button', type=int, default=7)
    args = parser.parse_args()
    main_hwnd, tray, dialogs = classify(args.pid)
    out = {'action': args.action}
    if args.action == 'close':
        out['ok'] = post(main_hwnd, WM_CLOSE, 0, 0)
    elif args.action == 'restore':
        out['ok'] = post(tray, TRAY_CALLBACK, 0, 65536 + NIN_SELECT)
    elif args.action == 'balloon_click':
        out['ok'] = post(tray, TRAY_CALLBACK, 0, NIN_BALLOONUSERCLICK)
    elif args.action == 'tray_quit':
        out['ok'] = post(tray, WM_COMMAND, 2, 0)
    elif args.action == 'click':
        out['ok'] = bool(dialogs) and post(dialogs[0], WM_COMMAND, args.button, 0)
    elif args.action == 'foreground':
        out['ok'] = foreground(main_hwnd)
    out['state'] = state(args.pid)
    print(json.dumps(out))


if __name__ == '__main__':
    main()
