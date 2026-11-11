"""Kernel-owned descendant audit. No kill-on-close, no network changes, no PID-only claims.

Residue rule (decision P3-06 item 6, T-P3-H): after the root exits, every descendant must be
gone within `residual_timeout` (10 s), except this scope's WebView2 processes, which get
`webview_grace` (20 s) in total. "This scope's WebView2" = image `msedgewebview2.exe`, a member
of the root's kernel job (its process tree), and a `--user-data-dir` under this scope's WebView2
profile root (`<TESSERACT_DATA_DIR>/WebView2[-admin]`, or `--webview-data-dir`). When the command
line cannot be read, the process-tree membership alone decides. Any other process still alive at
`residual_timeout` fails at once, without waiting for the WebView2 grace.

T-P3-H2 (C3-8): a failed audit says what was left. The result is written at once with the
residue's process tree (`residue_tree`: pid, parent, WebView2 `--type`, whether the parent is the
shell / still alive) and then, after `residue_observe` more seconds of watching the residual
handles (never killing), rewritten with each one's `exit_after_root_s` and `residue_class`:
`shell_residue` (a non-WebView2 member, another profile's WebView2 or an unidentified member is
left: the shell's own tree), `webview2_slow_exit` (only this scope's WebView2 was left and it went
away by itself within the observation: the runtime's own slow shutdown) or `webview2_stuck` (only
this scope's WebView2, still alive after the observation).
"""
import argparse
import ctypes as C
from ctypes import wintypes as W
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time

RESIDUAL_TIMEOUT = 10
WEBVIEW_GRACE = 20
WEBVIEW_IMAGE = 'msedgewebview2.exe'
# T-P3-H2: how long a failed audit keeps watching the residue before it classifies it.
RESIDUE_OBSERVE = 20

k = C.WinDLL('kernel32', use_last_error=True)
H = W.HANDLE
U = C.c_size_t

class STARTUPINFO(C.Structure):
    _fields_ = [('cb', W.DWORD), ('reserved', W.LPWSTR), ('desktop', W.LPWSTR), ('title', W.LPWSTR),
                ('x', W.DWORD), ('y', W.DWORD), ('xs', W.DWORD), ('ys', W.DWORD),
                ('xc', W.DWORD), ('yc', W.DWORD), ('fill', W.DWORD), ('flags', W.DWORD),
                ('show', W.WORD), ('reserved2', W.WORD), ('reserved_ptr', C.c_void_p),
                ('stdin', H), ('stdout', H), ('stderr', H)]

class PROCESSINFO(C.Structure):
    _fields_ = [('process', H), ('thread', H), ('pid', W.DWORD), ('tid', W.DWORD)]

class ASSOCIATE(C.Structure):
    _fields_ = [('key', C.c_void_p), ('port', H)]

class ACCOUNTING(C.Structure):
    _fields_ = [('user', C.c_longlong), ('kernel', C.c_longlong), ('period_user', C.c_longlong),
                ('period_kernel', C.c_longlong), ('faults', W.DWORD), ('total', W.DWORD),
                ('active', W.DWORD), ('terminated', W.DWORD)]

def api(name, result, args):
    fn = getattr(k, name); fn.restype = result; fn.argtypes = args
    return fn

create_job = api('CreateJobObjectW', H, [C.c_void_p, W.LPCWSTR])
create_port = api('CreateIoCompletionPort', H, [H, H, U, W.DWORD])
set_job = api('SetInformationJobObject', W.BOOL, [H, C.c_int, C.c_void_p, W.DWORD])
query_job = api('QueryInformationJobObject', W.BOOL, [H, C.c_int, C.c_void_p, W.DWORD, C.c_void_p])
assign = api('AssignProcessToJobObject', W.BOOL, [H, H])
create = api('CreateProcessW', W.BOOL, [W.LPCWSTR, W.LPWSTR, C.c_void_p, C.c_void_p, W.BOOL, W.DWORD, C.c_void_p, W.LPCWSTR, C.POINTER(STARTUPINFO), C.POINTER(PROCESSINFO)])
resume = api('ResumeThread', W.DWORD, [H])
wait = api('WaitForSingleObject', W.DWORD, [H, W.DWORD])
close = api('CloseHandle', W.BOOL, [H])
get_packet = api('GetQueuedCompletionStatus', W.BOOL, [H, C.POINTER(W.DWORD), C.POINTER(U), C.POINTER(C.c_void_p), W.DWORD])
open_process = api('OpenProcess', H, [W.DWORD, W.BOOL, W.DWORD])
get_times = api('GetProcessTimes', W.BOOL, [H, C.POINTER(W.FILETIME), C.POINTER(W.FILETIME), C.POINTER(W.FILETIME), C.POINTER(W.FILETIME)])
get_image = api('QueryFullProcessImageNameW', W.BOOL, [H, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD)])
get_code = api('GetExitCodeProcess', W.BOOL, [H, C.POINTER(W.DWORD)])
terminate_suspended = api('TerminateProcess', W.BOOL, [H, W.UINT])

def check(ok):
    if not ok: raise C.WinError(C.get_last_error())

def identity(pid, handle):
    birth, end, kernel, user = (W.FILETIME() for _ in range(4))
    check(get_times(handle, C.byref(birth), C.byref(end), C.byref(kernel), C.byref(user)))
    image = C.create_unicode_buffer(32768); length = W.DWORD(len(image))
    check(get_image(handle, 0, image, C.byref(length)))
    basic = (C.c_void_p * 6)()
    query_basic = C.WinDLL('ntdll').NtQueryInformationProcess
    query_basic.argtypes = [H, C.c_int, C.c_void_p, W.ULONG, C.c_void_p]
    query_basic.restype = C.c_long
    status = query_basic(handle, 0, C.byref(basic), C.sizeof(basic), None)
    return {'parent_pid': int(basic[5] or 0) if status >= 0 else None, 'pid': pid, 'created_filetime': (birth.dwHighDateTime << 32) | birth.dwLowDateTime,
            'image': image.value, 'identity_available': True, 'membership': 'kernel-job'}

def command_line(handle):
    """The process command line (ProcessCommandLineInformation, needs only query-limited access);
    None when it cannot be read (the process-tree rule then decides alone)."""
    query = C.WinDLL('ntdll').NtQueryInformationProcess
    query.argtypes = [H, C.c_int, C.c_void_p, W.ULONG, C.POINTER(W.ULONG)]
    query.restype = C.c_long
    size = W.ULONG(0)
    query(handle, 60, None, 0, C.byref(size))
    if not size.value or size.value > 1 << 20: return None
    buffer = (C.c_byte * size.value)()
    if query(handle, 60, buffer, size.value, C.byref(size)) < 0: return None
    length = C.c_ushort.from_buffer(buffer, 0).value
    address = C.c_void_p.from_buffer(buffer, C.sizeof(C.c_void_p)).value
    return C.wstring_at(address, length // 2) if address and length else ''

_USER_DATA_DIR = re.compile(r'--user-data-dir=(?:"([^"]*)"|(\S+))')

def user_data_dir(cmdline):
    match = _USER_DATA_DIR.search(cmdline or '')
    return (match.group(1) or match.group(2)) if match else None

_PROCESS_TYPE = re.compile(r'--type=(\S+)')

def webview_type(cmdline):
    """A WebView2 process's role (T-P3-H2): its `--type=` (renderer, gpu-process, utility,
    crashpad-handler, ...), `browser` without one, None when the command line is unreadable."""
    if cmdline is None: return None
    match = _PROCESS_TYPE.search(cmdline)
    return match.group(1).strip('"') if match else 'browser'

def residue_class(residual, unidentified, exited):
    """T-P3-H2 (C3-8): `shell_residue` when anything but this scope's WebView2 was left (the
    shell's own tree; `unidentified` counts the members without identity when the audit failed,
    and no identified residue at all is never the runtime's slow exit), else `webview2_slow_exit` when every residual WebView2 went away by itself
    during the observation, else `webview2_stuck`."""
    if unidentified or not residual or any(not record.get('webview_scope') for record in residual): return 'shell_residue'
    return 'webview2_slow_exit' if all(exited) else 'webview2_stuck'

def residue_tree(residual, records, root, alive_pids):
    """The residue's process tree: each residual member with its parent (the shell root, another
    job member, or outside the job) and whether that parent is still alive."""
    by_pid = {}
    for record in records:
        by_pid.setdefault(record.get('pid'), record)
    tree = []
    for record in residual:
        parent = record.get('parent_pid')
        member = by_pid.get(parent)
        tree.append({'pid': record['pid'], 'parent_pid': parent,
                     'image': pathlib.PureWindowsPath(record.get('image') or '').name,
                     'webview_type': record.get('webview_type'), 'webview_scope': record.get('webview_scope'),
                     'parent_is_root': parent == root['pid'],
                     'parent_image': pathlib.PureWindowsPath(member.get('image') or '').name if member else None,
                     'parent_in_job': member is not None, 'parent_alive': parent in alive_pids})
    return tree

def norm(path):
    # WebView2 passes the extended form (--user-data-dir=\\?\C:\...\EBWebView): drop the prefix first.
    if path.startswith('\\\\?\\UNC\\'): path = '\\\\' + path[8:]
    elif path.startswith('\\\\?\\'): path = path[4:]
    return os.path.normcase(os.path.normpath(os.path.abspath(path))).rstrip('\\/') + os.sep

def webview_roots(data_dir):
    """This scope's WebView2 profile roots (single_instance::profile): <data>/WebView2[-admin]."""
    if not data_dir: return []
    return [norm(os.path.join(data_dir, name)) for name in ('WebView2', 'WebView2-admin')]

def webview_scope(image, cmdline, roots):
    """Why a job member counts as this scope's WebView2 (and so gets the grace), or None.
    Called only for kernel-job members: the process tree is already established."""
    if pathlib.PureWindowsPath(image or '').name.lower() != WEBVIEW_IMAGE: return None
    # Unreadable command line, no --user-data-dir, or no known profile root: the job membership
    # (a descendant of the audited shell) is the evidence. A folder outside the roots is not ours.
    folder = user_data_dir(cmdline)
    if folder is None or not roots: return 'process-tree'
    return 'user-data-dir' if any(norm(folder).startswith(root) for root in roots) else None

def residue_verdict(alive, since_root_exit, residual_timeout, webview_grace):
    """'wait' or 'fail' for the descendants still alive `since_root_exit` seconds after the root
    exited: non-WebView2 processes fail at `residual_timeout`, this scope's WebView2 at the grace."""
    if since_root_exit <= residual_timeout: return 'wait'
    if any(not record.get('webview_scope') for record in alive): return 'fail'
    return 'wait' if since_root_exit <= max(webview_grace, residual_timeout) else 'fail'

UNIDENTIFIED_CONFIRM = 0.1

def unidentified_members(active_before, active_after, identified_alive):
    """Live job members without a held identity (they can never get the grace). The job is
    queried before and after the per-handle waits: a member that exits in between is counted
    by neither the `after` query nor the waits, so min() drops it (T-P3-H fix 1, S1)."""
    return max(0, min(active_before, active_after) - identified_alive)

def membership_complete(records, total, active, zero_observed):
    """Every job member has a record and the counters agree: either members are still active,
    or the job reported zero active processes (T-P3-H fix 1)."""
    return len(records) == total and (active > 0 or zero_observed)

def write(path, value):
    path = pathlib.Path(path); temp = path.with_suffix('.pending')
    temp.write_bytes(json.dumps(value, indent=2).encode()); temp.replace(path)

def audit(command, output, ready, residual_timeout=RESIDUAL_TIMEOUT, webview_grace=WEBVIEW_GRACE, webview_data_dir=None,
          residue_observe=RESIDUE_OBSERVE):
    roots = webview_roots(webview_data_dir if webview_data_dir is not None else os.environ.get('TESSERACT_DATA_DIR'))
    job = create_job(None, None); check(job)
    port = create_port(H(-1), None, 0, 1); check(port)
    assoc = ASSOCIATE(1, port); check(set_job(job, 7, C.byref(assoc), C.sizeof(assoc)))
    # Deliberately do not set JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE.
    si = STARTUPINFO(); si.cb = C.sizeof(si); si.flags = 1; si.show = 0
    pi = PROCESSINFO(); handles = {}; records = []; root_exited_at = None; zero_observed = False; resumed = False
    unidentified_since = None

    def take(message, pid):
        nonlocal zero_observed
        if message == 6:  # JOB_OBJECT_MSG_NEW_PROCESS
            handle = open_process(0x101000, False, pid)  # query limited + synchronize
            if handle:
                try: record = identity(pid, handle)
                except OSError:
                    close(handle); handle = None
            if handle:
                # Only the classification and the profile folder are kept, not the command line.
                try: cmdline = command_line(handle)
                except OSError: cmdline = None
                record['command_line_read'] = cmdline is not None
                record['webview_scope'] = webview_scope(record['image'], cmdline, roots)
                if pathlib.PureWindowsPath(record['image']).name.lower() == WEBVIEW_IMAGE:
                    record['user_data_dir'] = user_data_dir(cmdline); record['webview_type'] = webview_type(cmdline)
            if not handle:
                record = {'pid': pid, 'identity_available': False, 'membership': 'kernel-job',
                          'reason': 'exited_before_identity_capture'}
            record['observed_monotonic'] = time.monotonic()
            records.append(record)
            if handle: handles[(pid, record['created_filetime'])] = handle
        elif message == 4:
            zero_observed = True
        elif message in (7, 8):  # normal/abnormal exit
            for record in reversed(records):
                if record['pid'] == pid and 'exit_observed' not in record:
                    record['exit_observed'] = True; break

    try:
        check(create(command[0], C.create_unicode_buffer(subprocess.list2cmdline(command)), None, None,
                     False, 0x08000004, None, None, C.byref(si), C.byref(pi)))
        # Assign before any application code can spawn a child; no sampling gap.
        check(assign(job, pi.process)); check(resume(pi.thread) != 0xffffffff); resumed = True
        root = identity(pi.pid, pi.process)
        write(ready, root)
        deadline = time.monotonic() + 90
        # Once the root exited, the residue rule below always ends the loop (at most the grace).
        while root_exited_at is not None or time.monotonic() < deadline:
            message = W.DWORD(); key = U(); value = C.c_void_p()
            if get_packet(port, C.byref(message), C.byref(key), C.byref(value), 100):
                take(message.value, int(value.value or 0))
            if root_exited_at is not None:
                # Before judging residue, drain what is already queued (one packet per round above):
                # a member whose NEW_PROCESS is still queued would otherwise count as unidentified.
                for _ in range(1024):
                    if not get_packet(port, C.byref(message), C.byref(key), C.byref(value), 0): break
                    take(message.value, int(value.value or 0))
            accounting = ACCOUNTING()
            check(query_job(job, 1, C.byref(accounting), C.sizeof(accounting), None))
            if wait(pi.process, 0) == 0 and root_exited_at is None: root_exited_at = time.monotonic()
            rule = {'residual_timeout_s': residual_timeout, 'webview_grace_s': webview_grace,
                    'webview_profile_roots': roots}
            if accounting.active == 0 and zero_observed and len(records) == accounting.total:
                code = W.DWORD(); check(get_code(pi.process, C.byref(code)))
                late = root_exited_at is not None and time.monotonic() - root_exited_at > residual_timeout
                result = {'passed': code.value == 0, 'root': root, 'root_exit_code': code.value,
                          'active_processes': 0, 'total_processes': accounting.total,
                          'records': records, 'kernel_membership_complete': True,
                          # True: only this scope's WebView2 outlived residual_timeout (within the grace).
                          'webview_grace_used': late, **rule}
                if root_exited_at is not None: result['last_exit_after_root_s'] = round(time.monotonic() - root_exited_at, 3)
                write(output, result); return result
            if root_exited_at is not None:
                since = time.monotonic() - root_exited_at
                alive = [record for record in records if record.get('identity_available') and
                         wait(handles[(record['pid'], record['created_filetime'])], 0) == 258]
                after = ACCOUNTING()
                check(query_job(job, 1, C.byref(after), C.sizeof(after), None))
                # A live member without a held identity cannot be classified: never a WebView2.
                unknown = unidentified_members(accounting.active, after.active, len(alive))
                verdict = residue_verdict(alive + [{}] * unknown, since, residual_timeout, webview_grace)
                if verdict == 'fail' and residue_verdict(alive, since, residual_timeout, webview_grace) == 'wait':
                    # Only unidentified members fail it: confirm on a later round (>= 100 ms), so a
                    # member caught mid-exit or mid-birth is not reported as residue (T-P3-H fix 1, S1).
                    now = time.monotonic()
                    if unidentified_since is None: unidentified_since = now
                    if now - unidentified_since < UNIDENTIFIED_CONFIRM: verdict = 'wait'
                else:
                    unidentified_since = None
                if verdict == 'fail':
                    code = W.DWORD(); check(get_code(pi.process, C.byref(code)))
                    alive_pids = {record['pid'] for record in alive}
                    result = {'passed': False, 'root': root, 'root_exit_code': code.value, 'active_processes': after.active,
                              'records': records, 'residual': alive, 'unidentified_residual': unknown,
                              'residual_after_root_s': round(since, 3),
                              'kernel_membership_complete': membership_complete(records, after.total, after.active, zero_observed),
                              'residue_tree': residue_tree(alive, records, root, alive_pids),
                              'residue_class': 'observing', 'residue_observe_s': residue_observe, **rule}
                    write(output, result)
                    # T-P3-H2 (C3-8): keep watching (never killing) to tell the shell's residue from
                    # the WebView2 runtime's own slow exit, then rewrite the result.
                    pending = {record['pid']: record for record in alive}
                    end = time.monotonic() + residue_observe
                    while pending and time.monotonic() < end:
                        for pid, record in list(pending.items()):
                            if wait(handles[(pid, record['created_filetime'])], 0) != 258:
                                record['exit_after_root_s'] = round(time.monotonic() - root_exited_at, 3); del pending[pid]
                        if pending: time.sleep(.1)
                    final = ACCOUNTING()
                    check(query_job(job, 1, C.byref(final), C.sizeof(final), None))
                    # Classify by what was left when the audit failed (`unknown`), not by what is still
                    # there after the observation: an unidentified member that triggered the failure
                    # and then left is still the shell's residue (T-P3-H2 fix 1).
                    result['unidentified_after_observe'] = unidentified_members(after.active, final.active, len(pending)) if unknown else 0
                    result['residue_class'] = residue_class(alive, unknown, [record.get('exit_after_root_s') is not None for record in alive])
                    result['residue_tree'] = residue_tree(alive, records, root, set(pending))
                    for node, record in zip(result['residue_tree'], alive): node['exit_after_root_s'] = record.get('exit_after_root_s')
                    result['active_processes_after_observe'] = final.active
                    write(output, result); return result
        raise TimeoutError('owned process audit exceeded deadline')
    finally:
        # If assignment fails, this still-suspended process has never run app code.
        if pi.process and not resumed: terminate_suspended(pi.process, 1)
        for handle in handles.values(): close(handle)
        for handle in (pi.thread, pi.process, port, job):
            if handle: close(handle)

def counterexample(directory):
    directory = pathlib.Path(directory).resolve(); directory.mkdir(parents=True, exist_ok=True)
    release = directory / 'fixture-release'
    if release.exists(): release.unlink()
    leaf = 'import pathlib,time; p=pathlib.Path(__import__("sys").argv[1]); end=time.monotonic()+15\nwhile not p.exists() and time.monotonic()<end: time.sleep(.05)'
    middle = 'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",sys.argv[1],sys.argv[2]],creationflags=0x08000000); time.sleep(.4)'
    root = 'import subprocess,sys,time; p=subprocess.Popen([sys.executable,"-c",sys.argv[1],sys.argv[2],sys.argv[3]],creationflags=0x08000000); p.wait()'
    try:
        result = audit([sys.executable, '-c', root, middle, leaf, str(release)], directory / 'inventory-counterexample.json', directory / 'fixture-root.json', 1,
                       residue_observe=2)
        assert not result['passed'] and result['active_processes'] >= 1
        assert result['residue_class'] == 'shell_residue', 'an orphaned non-WebView2 process is the shell tree (T-P3-H2)'
        assert any(r['image'].lower().endswith('python.exe') and r['identity_available'] for r in result['residual'])
        assert result['records'][0].get('exit_observed')
        write(directory / 'inventory-regression.json', {'passed': True, 'intermediate_exited': True, 'orphan_detected': True, 'held_creation_identity': True, 'expected_audit_result': False})
        print('PASS: root and intermediate exited; live grandchild detected with creation time, image, held handle and kernel membership')
    finally: release.touch()

def grace_selftest(directory):
    """T-P3-H: the WebView2 grace with real processes. A copy of the base interpreter named
    msedgewebview2.exe stands in for WebView2 (no browser is started); short limits (1 s / 3 s)
    keep the run quick. Checks the pure rules first, then four audited trees."""
    directory = pathlib.Path(directory).resolve(); directory.mkdir(parents=True, exist_ok=True)
    data = pathlib.Path(tempfile.mkdtemp(prefix='p3h-grace-'))
    roots = webview_roots(str(data))
    ours, other = str(data / 'WebView2' / 'scope' / 'EBWebView'), str(data.parent / (data.name + '-other') / 'EBWebView')
    # Pure rules.
    assert user_data_dir('x.exe --type=renderer --user-data-dir="C:\\a b\\EBWebView" --lang=en') == 'C:\\a b\\EBWebView'
    assert user_data_dir('x.exe --user-data-dir=C:\\a\\EBWebView --x') == 'C:\\a\\EBWebView'
    assert user_data_dir('x.exe --type=gpu-process') is None
    assert webview_scope('C:\\p\\msedgewebview2.exe', '--user-data-dir="' + ours + '"', roots) == 'user-data-dir'
    assert webview_scope('C:\\p\\MSEdgeWebView2.EXE', '--user-data-dir="' + ours + '"', roots) == 'user-data-dir'
    # The real WebView2 command line uses the extended-length form.
    assert webview_scope('C:\\p\\msedgewebview2.exe', '--type=renderer --user-data-dir=\\\\?\\' + ours + ' --x', roots) == 'user-data-dir'
    assert norm('\\\\?\\UNC\\srv\\share\\d') == norm('\\\\srv\\share\\d')
    assert webview_scope('C:\\p\\msedgewebview2.exe', '--user-data-dir="' + other + '"', roots) is None, 'another profile'
    assert webview_scope('C:\\p\\msedgewebview2.exe', '--user-data-dir="' + str(data / 'WebView2x') + '"', roots) is None, 'prefix only'
    assert webview_scope('C:\\p\\msedgewebview2.exe', None, roots) == 'process-tree'
    assert webview_scope('C:\\p\\msedgewebview2.exe', '--user-data-dir="' + other + '"', []) == 'process-tree'
    assert webview_scope('C:\\p\\python.exe', '--user-data-dir="' + ours + '"', roots) is None
    w, o = {'webview_scope': 'user-data-dir'}, {'webview_scope': None}
    assert residue_verdict([w, o], 10, 10, 20) == 'wait'
    assert residue_verdict([o], 10.1, 10, 20) == 'fail', 'other processes: no grace'
    assert residue_verdict([w, o], 10.1, 10, 20) == 'fail', 'a WebView2 does not shelter another process'
    assert residue_verdict([w], 19.9, 10, 20) == 'wait'
    assert residue_verdict([w], 20.1, 10, 20) == 'fail'
    # Fix 1 (S1): a member that exits between the job query and the waits is not unidentified.
    assert unidentified_members(3, 2, 2) == 0, 'exited mid-sample'
    assert unidentified_members(2, 3, 2) == 0, 'born after the first query'
    assert unidentified_members(3, 3, 2) == 1, 'a live member without identity'
    assert unidentified_members(1, 1, 2) == 0
    # T-P3-H2 (C3-8): roles and the residue classification.
    assert webview_type('x.exe --type=renderer --user-data-dir=C:\\a') == 'renderer'
    assert webview_type('x.exe --type=crashpad-handler "--database=C:\\d"') == 'crashpad-handler'
    assert webview_type('x.exe --embedded-browser-webview=1 --user-data-dir=C:\\a') == 'browser'
    assert webview_type(None) is None
    assert residue_class([w], 0, [True]) == 'webview2_slow_exit'
    assert residue_class([w, w], 0, [True, False]) == 'webview2_stuck'
    assert residue_class([w, o], 0, [True, True]) == 'shell_residue', 'another process is the shell tree'
    assert residue_class([w], 1, [True]) == 'shell_residue', 'an unidentified member'
    assert residue_class([], 1, []) == 'shell_residue' and residue_class([], 0, []) == 'shell_residue', 'nothing identified'
    tree = residue_tree([{'pid': 3, 'parent_pid': 2, 'image': 'C:\\p\\msedgewebview2.exe', 'webview_type': 'crashpad-handler', 'webview_scope': 'user-data-dir'}],
                        [{'pid': 1, 'image': 'C:\\s.exe'}, {'pid': 2, 'image': 'C:\\p\\msedgewebview2.exe'}], {'pid': 1}, set())
    assert tree == [{'pid': 3, 'parent_pid': 2, 'image': 'msedgewebview2.exe', 'webview_type': 'crashpad-handler', 'webview_scope': 'user-data-dir',
                     'parent_is_root': False, 'parent_image': 'msedgewebview2.exe', 'parent_in_job': True, 'parent_alive': False}], tree
    assert membership_complete([w, o], 2, 1, False) and membership_complete([w, o], 2, 0, True)
    assert not membership_complete([w, o], 2, 0, False), 'zero never reported'
    assert not membership_complete([w], 2, 1, False), 'a member without a record'
    # Real processes.
    fake_dir = data / 'fake'; fake_dir.mkdir()
    for name in ('python312.dll', 'python3.dll', 'vcruntime140.dll', 'vcruntime140_1.dll'):
        source = pathlib.Path(sys.base_prefix) / name
        if source.exists(): shutil.copy2(source, fake_dir / name)
    fake = fake_dir / WEBVIEW_IMAGE
    # pythonw: a GUI-subsystem image like the real WebView2 (no conhost.exe child of its own).
    shutil.copy2(pathlib.Path(sys.base_prefix) / 'pythonw.exe', fake)
    # T-P3-H2: a child sleeps until an absolute wall-clock deadline the root fixes at spawn time, so
    # its exit lands `seconds` after the root's exit however long the interpreter took to start on a
    # loaded machine (seven children starting at once lagged by up to ~1 s and failed the 3 s grace).
    sleeper = 'import sys,time; time.sleep(max(0.0,float(sys.argv[1])-time.time()))'
    # The root starts the children and exits at once, so a child's lifetime ~ time after the root exit.
    root = ('import json,os,subprocess,sys,time\n'
            'env=dict(os.environ,PYTHONHOME=sys.argv[2])\n'
            'for exe,args in json.loads(sys.argv[1]): subprocess.Popen([exe,"-B","-c",' + json.dumps(sleeper) + ',str(time.time()+float(args[0]))]+args[1:],env=env,creationflags=0x08000000)\n')
    def tree(name, children, residual_timeout=1, webview_grace=3, residue_observe=5):
        spec = json.dumps(children)
        started = time.monotonic()
        result = audit([sys.executable, '-B', '-c', root, spec, sys.base_prefix], directory / ('grace-' + name + '.json'),
                       directory / ('grace-' + name + '-root.json'), residual_timeout, webview_grace, str(data), residue_observe)
        result['_elapsed'] = time.monotonic() - started
        return result
    view = lambda seconds, folder: [str(fake), [str(seconds), '--user-data-dir=' + folder]]
    plain = lambda seconds: [sys.executable, [str(seconds)]]
    outcome = {}
    try:
        r = tree('webview-late-ok', [view(2, '\\\\?\\' + ours)])  # the extended form WebView2 itself uses
        assert r['passed'] and r['active_processes'] == 0 and r['webview_grace_used'], r
        assert any(x.get('webview_scope') == 'user-data-dir' for x in r['records']), 'classified by --user-data-dir'
        outcome['webview_late_ok'] = r['last_exit_after_root_s']
        # Fix 1 (S1): several scoped WebView2 trees leaving one after another inside the grace.
        r = tree('webview-staggered-ok', [view(round(1.2 + .2 * i, 2), ours) for i in range(7)])
        assert r['passed'] and r['active_processes'] == 0 and r['webview_grace_used'], r
        assert sum(x.get('webview_scope') == 'user-data-dir' for x in r['records']) == 7, r
        outcome['webview_staggered_ok'] = r['last_exit_after_root_s']
        # T-P3-H2: a WebView2 still there after the observation (9 s sleeper, fails at 3 s, watched 2 s more).
        r = tree('webview-stuck-class', [view(9, ours)], residue_observe=2)
        assert not r['passed'] and r['residue_class'] == 'webview2_stuck', r
        # Its parent is the base interpreter the venv launcher (the root) started, gone by now.
        node = r['residue_tree'][0]
        assert (node['parent_in_job'], node['parent_alive'], node['webview_type'], node['exit_after_root_s']) == (True, False, 'browser', None), r
        outcome['webview_stuck_class'] = r['residue_class']
        r = tree('webview-stuck-fails', [view(6, ours)])
        assert not r['passed'] and 3 <= r['residual_after_root_s'] < 5, r
        assert [x['webview_scope'] for x in r['residual']] == ['user-data-dir'], r
        # It leaves by itself at ~6 s, inside the 5 s observation: the runtime's slow exit.
        assert r['residue_class'] == 'webview2_slow_exit' and 5 <= r['residue_tree'][0]['exit_after_root_s'] < 8, r
        outcome['webview_stuck_fails_at_s'] = r['residual_after_root_s']
        outcome['webview_late_exit_class'] = [r['residue_class'], r['residue_tree'][0]['exit_after_root_s']]
        r = tree('foreign-profile', [view(2.5, other)])
        assert not r['passed'] and r['residual_after_root_s'] < 2, r
        assert [x['webview_scope'] for x in r['residual']] == [None], r
        assert r['residue_class'] == 'shell_residue', r
        outcome['foreign_profile_fails_at_s'] = r['residual_after_root_s']
        r = tree('other-process', [view(2.5, ours), plain(2.5)])
        assert not r['passed'] and r['residual_after_root_s'] < 2, r
        assert r['residue_class'] == 'shell_residue', r
        outcome['other_process_fails_at_s'] = r['residual_after_root_s']
        # T-P3-H2 fix 1: a member without identity triggers the failure and leaves during the
        # observation, while this scope's WebView2 leaves later, also inside it. The class follows
        # what was left at the failure (`shell_residue`), not the empty job after the observation.
        blind = fake_dir / 'unidentified.exe'; shutil.copy2(pathlib.Path(sys.base_prefix) / 'pythonw.exe', blind)
        global identity
        real_identity = identity
        def blind_identity(pid, handle):
            record = real_identity(pid, handle)
            if pathlib.PureWindowsPath(record['image']).name.lower() == blind.name: raise OSError('identity withheld (selftest)')
            return record
        identity = blind_identity
        try: r = tree('unidentified-leaves', [view(3.5, ours), [str(blind), ['2.5']]])
        finally: identity = real_identity
        assert not r['passed'] and r['unidentified_residual'] == 1 and [x['webview_scope'] for x in r['residual']] == ['user-data-dir'], r
        assert r['unidentified_after_observe'] == 0 and r['residue_tree'][0]['exit_after_root_s'] is not None, r
        assert r['residue_class'] == 'shell_residue', r
        outcome['unidentified_leaves_class'] = [r['residue_class'], r['residual_after_root_s'], r['residue_tree'][0]['exit_after_root_s']]
        write(directory / 'grace-selftest.json', {'passed': True, 'residual_timeout_s': 1, 'webview_grace_s': 3, **outcome})
        print('PASS: scoped WebView2 gets the grace; a stuck one, another profile and other processes do not', json.dumps(outcome))
    finally:
        time.sleep(4)  # the stuck sleepers end on their own (never killed)
        shutil.rmtree(data, ignore_errors=True)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--counterexample')
    parser.add_argument('--grace-selftest')
    parser.add_argument('--output'); parser.add_argument('--ready')
    parser.add_argument('--webview-data-dir', help='data directory whose WebView2[-admin] folders are this scope (default: TESSERACT_DATA_DIR)')
    parser.add_argument('--residue-observe', type=float, default=RESIDUE_OBSERVE,
                        help='seconds a failed audit keeps watching the residue before classifying it (T-P3-H2)')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.counterexample: counterexample(args.counterexample)
    elif args.grace_selftest: grace_selftest(args.grace_selftest)
    else:
        result = audit(args.command[1:] if args.command[0] == '--' else args.command, args.output, args.ready,
                       webview_data_dir=args.webview_data_dir, residue_observe=args.residue_observe)
        sys.exit(0 if result['passed'] else 1)
