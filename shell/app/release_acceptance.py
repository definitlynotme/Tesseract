"""Exercise the standard release EXE via a fixed native probe and real Windows input.

Usage: python -B shell/app/release_acceptance.py .build/新界面迁移_工单/evidence/T-P3-01/release-run
"""
import ctypes as C
from ctypes import wintypes as W
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import yaml
from ole_file_drop import drag_file

repo = Path(__file__).resolve().parents[2]
evidence = Path(sys.argv[1]).resolve(); evidence.mkdir(parents=True, exist_ok=True)
exe = repo / 'shell/target/release/tesseract-shell.exe'
for name in ('config', 'data', 'temp'): (evidence / name).mkdir(exist_ok=True)
config = yaml.safe_load((repo / 'config.example.yaml').read_bytes())
config['tiers']['tier3']['enabled'] = False
for tier in ('tier1', 'tier2'):
    config['tiers'][tier]['nodes'] = []; config['tiers'][tier]['subscription_urls'] = []
config['client']['system_proxy']['enabled'] = False; config['alerts']['enabled'] = False
if isinstance(config.get('killswitch'), dict): config['killswitch']['enabled'] = False
(evidence / 'config/config.yaml').write_bytes(yaml.safe_dump(config, allow_unicode=True).encode())
(evidence / 'config/sources.yaml').write_bytes(b'migrations:\n  tier3_source_portfolio_defaults_v1: true\n  tier3_source_portfolio_defaults_v2: true\n  tier3_source_default_delivery_v1: true\ngithub_repos: []\ntelegram_channels: []\nsubscription_urls: []\nlocal_files: []\n')
hits = []
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        hits.append({'path': self.path, 'user_agent': self.headers.get('User-Agent')})
        self.send_response(200); self.send_header('Content-Type', 'text/html'); self.end_headers()
        self.wfile.write(b'<title>P3 external browser verification</title><p>Local isolated P3 acceptance.</p>')
    def log_message(self, *args): pass
server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
sock = socket.socket(); sock.bind(('127.0.0.1', 0)); cdp = sock.getsockname()[1]; sock.close()
env = dict(os.environ, TESSERACT_CONFIG_DIR=str(evidence / 'config'), TESSERACT_DATA_DIR=str(evidence / 'data'),
           TESSERACT_INSTANCE_SCOPE='p3-release-acceptance', TEMP=str(evidence / 'temp'), TMP=str(evidence / 'temp'),
           TESSERACT_SHELL_CDP_PORT=str(cdp), TESSERACT_SHELL_VERIFY_PORT=str(server.server_port),
           WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=f'--remote-debugging-port={cdp}', PYTHONDONTWRITEBYTECODE='1')
env.pop('TESSERACT_ISOLATED_AUDIT_MANIFEST', None)
u = C.WinDLL('user32', use_last_error=True)
u.EnumWindows.argtypes = [C.c_void_p, W.LPARAM]
u.GetWindowThreadProcessId.argtypes = [W.HWND, C.POINTER(W.DWORD)]
u.IsWindowVisible.argtypes = [W.HWND]
u.GetWindowTextW.argtypes=[W.HWND,W.LPWSTR,C.c_int]
u.SetForegroundWindow.argtypes = [W.HWND]
u.BringWindowToTop.argtypes = [W.HWND]
u.AttachThreadInput.argtypes = [W.DWORD,W.DWORD,W.BOOL]
C.WinDLL('kernel32').GetCurrentThreadId.restype = W.DWORD
u.GetForegroundWindow.restype = W.HWND
u.SetWindowPos.argtypes = [W.HWND, W.HWND, C.c_int, C.c_int, C.c_int, C.c_int, W.UINT]
u.GetWindowRect.argtypes = [W.HWND, C.POINTER(W.RECT)]
u.PostMessageW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
u.SetThreadDpiAwarenessContext.argtypes = [C.c_void_p]; u.SetThreadDpiAwarenessContext.restype = C.c_void_p
u.SetThreadDpiAwarenessContext(C.c_void_p(-4))

def window(pid):
    found = []
    @C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
    def callback(hwnd, unused):
        process = W.DWORD(); u.GetWindowThreadProcessId(hwnd, C.byref(process))
        title=C.create_unicode_buffer(256); u.GetWindowTextW(hwnd,title,len(title))
        if process.value == pid and u.IsWindowVisible(hwnd) and title.value == 'Tesseract Shell (P3)': found.append(hwnd)
        return True
    u.EnumWindows(callback, 0)
    return found[0] if found else None

class KEY(C.Structure):
    _fields_ = [('vk', W.WORD), ('scan', W.WORD), ('flags', W.DWORD), ('time', W.DWORD), ('extra', C.c_size_t)]
class MOUSE(C.Structure):
    _fields_ = [('x', W.LONG), ('y', W.LONG), ('data', W.DWORD), ('flags', W.DWORD), ('time', W.DWORD), ('extra', C.c_size_t)]
class PAYLOAD(C.Union):
    _fields_ = [('key', KEY), ('mouse', MOUSE)]
class INPUT(C.Structure):
    _fields_ = [('type', W.DWORD), ('payload', PAYLOAD)]
u.SendInput.argtypes = [W.UINT, C.POINTER(INPUT), C.c_int]
def key(vk, up=False):
    event = INPUT(); event.type = 1; event.payload.key = KEY(vk, 0, 2 if up else 0, 0, 0)
    assert u.SendInput(1, C.byref(event), C.sizeof(event)) == 1
def chord(*keys):
    for vk in keys: key(vk)
    for vk in reversed(keys): key(vk, True)
    time.sleep(.2)

def load(path):
    try: return json.loads(path.read_bytes())
    except (FileNotFoundError, json.JSONDecodeError): return None

def launch(round_number):
    data = evidence / 'data'
    for name in ('acceptance-result.json', 'acceptance-stage.json', 'acceptance-input.done'):
        path = data / name
        if path.exists(): path.unlink()
    ready = evidence / f'root-{round_number}.json'
    output = evidence / f'process-inventory-{round_number}.json'
    log = (evidence / f'process-watch-{round_number}.log').open('wb')
    watcher = subprocess.Popen([sys.executable, '-B', str(repo / 'shell/app/process_watch.py'), '--output', str(output),
                                '--ready', str(ready), '--', str(exe), '--dev-rt', '--verify-webview'], env=env,
                               stdout=log, stderr=subprocess.STDOUT, creationflags=0x08000000)
    deadline = time.monotonic() + 70; hwnd = None; did_input = False; cdp_checks = 0; initial_position = None; physical_drop = None
    try:
        while time.monotonic() < deadline and watcher.poll() is None:
            root = load(ready)
            if root: hwnd = window(root['pid'])
            if hwnd and load(data / 'acceptance-stage.json') and not did_input:
                rect=W.RECT(); assert u.GetWindowRect(hwnd,C.byref(rect)); initial_position={'x':rect.left,'y':rect.top}
                current_thread=C.WinDLL('kernel32').GetCurrentThreadId()
                foreground_thread=u.GetWindowThreadProcessId(u.GetForegroundWindow(),None)
                attached=foreground_thread != current_thread and u.AttachThreadInput(current_thread,foreground_thread,True)
                try:
                    u.BringWindowToTop(hwnd); u.SetForegroundWindow(hwnd)
                finally:
                    if attached: u.AttachThreadInput(current_thread,foreground_thread,False)
                time.sleep(.2)
                assert u.GetForegroundWindow() == hwnd, 'input target not foreground' 
                rect = W.RECT(); assert u.GetWindowRect(hwnd, C.byref(rect))
                # Focus the real WebView client, then send real refresh/zoom accelerators.
                u.SetCursorPos(rect.left + 450, rect.top + 250)
                u.mouse_event(2, 0, 0, 0, 0); u.mouse_event(4, 0, 0, 0, 0)
                for keys in ((0x74,), (0x11, 0x52), (0x11, 0x10, 0x52), (0x11, 0xBB), (0x11, 0xBD), (0x11, 0x30)):
                    chord(*keys)
                fixture=data / 'file-drop-fixture.html'; fixture.write_bytes(b'<title>P3 drag fixture</title>')
                physical_drop=drag_file(hwnd, fixture)
                if round_number == 1: assert u.SetWindowPos(hwnd, None, 140, 160, 0, 0, 0x0015)
                (data / 'acceptance-input.done').touch(); did_input = True
            with socket.socket() as probe:
                probe.settimeout(.1)
                assert probe.connect_ex(('127.0.0.1', cdp)) != 0, 'release enabled CDP despite environment overrides'
                cdp_checks += 1
            time.sleep(.1)
        assert watcher.wait(timeout=5) == 0, f'release process audit failed: {output}'
        result = load(data / 'acceptance-result.json'); assert result and result['passed'], result
        assert did_input and cdp_checks > 2
        result['initial_position'] = initial_position; result['cdp_checks'] = cdp_checks; result['physical_file_drop'] = physical_drop
        result['exe_sha256'] = hashlib.sha256(exe.read_bytes()).hexdigest()
        (evidence / f'release-summary-{round_number}.json').write_bytes(json.dumps(result, indent=2).encode())
        return result
    finally:
        if watcher.poll() is None and hwnd: u.PostMessageW(hwnd, 0x10, 0, 0)
        log.close()

try:
    first = launch(1); second = launch(2)
    assert second['initial_position'] == {'x': 140, 'y': 160}, second['initial_position']
    assert any(hit['path'] == '/p3-external' for hit in hits) and any(hit['path'] == '/p3-navigation' for hit in hits), hits
    summary = {'passed': True, 'restart_position_restored': True, 'external_browser_hits': hits,
               'physical_keyboard': True, 'monitor_count': u.GetSystemMetrics(80),
               'cross_monitor_dpi': 'unavailable: only one physical monitor; no host display settings changed',
               'exe_sha256': first['exe_sha256']}
    (evidence / 'release-summary.json').write_bytes(json.dumps(summary, indent=2).encode())
    print(json.dumps(summary, indent=2))
finally: server.shutdown()
