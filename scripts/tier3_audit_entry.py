"""Opt-in isolation for the same frozen executable used in acceptance tests.

No candidate, validation or scheduling policy is replaced here. A receipt is
configuration evidence, not proof that a packet bypassed an external TUN.
"""
from __future__ import annotations

import atexit
import functools
import hashlib
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import urlencode

from atomic_file import replace_with_retry
from scripts.tier3_audit_import_hooks import AuditImportHooks
from scripts.tier3_audit_activity import AuditActivityRecorder
from scripts.tier3_audit_process_registry import AuditNativeProcessRegistry, AuditProcessRegistry
from scripts.tier3_audit_socket_policy import (
    OwnedLoopbackEndpoints, assert_physical_mihomo_config,
    owned_dns_getaddrinfo, physical_socket_address, pin_mihomo_audit_dns,
)


ENVIRONMENT_KEY = 'TESSERACT_ISOLATED_AUDIT_MANIFEST'
_guard = None


def read_manifest(path, environment):
    path = Path(path).resolve(strict=True)
    raw = path.read_bytes()
    value = json.loads(raw)
    required = {'schema', 'audit_id', 'interface', 'interface_index',
                'dns_controller_port', 'dns_listener_port', 'config_dir', 'data_dir'}
    if (not isinstance(value, dict) or set(value) != required
            or type(value['schema']) is not int or value['schema'] != 1):
        raise ValueError('invalid isolation manifest schema')
    audit_id = value['audit_id']
    if (not isinstance(audit_id, str) or len(audit_id) != 32
            or any(c not in '0123456789abcdef' for c in audit_id)):
        raise ValueError('invalid isolation audit id')
    if not isinstance(value['interface'], str) or not value['interface'].strip():
        raise ValueError('isolation requires a physical interface')
    index = value['interface_index']
    if type(index) is not int or not 1 <= index <= 0xffffffff:
        raise ValueError('invalid isolation interface index')
    ports = [value['dns_controller_port'], value['dns_listener_port']]
    if (any(type(port) is not int or not 1 <= port <= 65535 for port in ports)
            or ports[0] == ports[1]):
        raise ValueError('isolation requires distinct owned DNS ports')
    for key, env, name in (('config_dir', 'TESSERACT_CONFIG_DIR', 'config'),
                           ('data_dir', 'TESSERACT_DATA_DIR', 'data')):
        folder = Path(value[key]).resolve(strict=True)
        supplied = environment.get(env)
        if (not folder.is_dir() or folder != path.parent / name or not supplied
                or Path(supplied).resolve(strict=True) != folder):
            raise ValueError('isolation requires matching dedicated config/data directories')
        value[key] = str(folder)
    value['manifest_sha256'] = hashlib.sha256(raw).hexdigest()
    return value


class IsolatedAuditGuard:
    def __init__(self, manifest):
        self.manifest = dict(manifest)
        self.interface = manifest['interface']
        self.interface_index = manifest['interface_index']
        self.allowed_ports = OwnedLoopbackEndpoints((manifest['dns_controller_port'],
                                                     manifest['dns_listener_port']))
        self.lock = threading.RLock()
        self.receipt = dict(process_id=os.getpid(), parent_process_id=os.getppid(),
                            audit_id=manifest['audit_id'], manifest_sha256=manifest['manifest_sha256'],
                            interface=self.interface, interface_index=self.interface_index,
                            evidence_class='process_configuration_only',
                            executable=str(Path(sys.executable).resolve()),
                            socket_guard_installed=False, rendered=0, received_prewarm=0,
                            outgoing_bindings={}, mihomo_processes=[], completed=False,
                            command_policy_violation_count=0)
        self._patches = []
        self._last_publish = 0.0
        self.registry = AuditProcessRegistry(self._inventory)
        self.receipt['process_inventory'] = self.registry.snapshot()
        self.native_registry = AuditNativeProcessRegistry(self._native_inventory)
        self.command_registry = AuditNativeProcessRegistry(self._command_inventory)
        self._command_processes = {}
        self._version_context = threading.local()
        self._command_observer_installed = False
        self._native_processes = {}
        self.receipt['native_process_inventory'] = self.native_registry.snapshot()
        self.receipt['command_process_inventory'] = self.command_registry.snapshot()
        self._process_attempts = {}
        self.activity = AuditActivityRecorder(manifest['data_dir'])
        self.hooks = AuditImportHooks({
            'asyncio.windows_events': self._install_iocp,
            'multiprocessing.process': self._install_process,
            'clash_formatter': self._install_renderers,
            'tier3_validation_config': self._install_renderers,
            'tier3_validator_prewarm': self._install_prewarm,
            'tier3_validator_supervisor': self._install_supervisor,
            'core_runner': self._install_core_runner,
            'platform': self._install_platform,
            'system_proxy': self._block_system_proxy,
            'killswitch': self._block_firewall,
            'runtime_network_recovery': self._block_recovery_firewall,
            'app_info': self._disable_update_checks,
            'alerts': self._disable_alert_delivery,
            'urllib.request': self._disable_system_proxy_discovery,
            'desktop_app_shell': self._isolate_window_identity,
            'desktop_app': self._observe_window_start,
            'manager': lambda module: self.activity.install_manager(module, self._patch),
            'tier3_candidate_scheduler': lambda module: self.activity.install_scheduler(module, self._patch),
            'tier3_refresh_controller': lambda module: self.activity.install_controller(module, self._patch),
        })

    def _patch(self, owner, name, replacement):
        self._patches.append((owner, name, getattr(owner, name), replacement))
        setattr(owner, name, replacement)

    def publish(self):
        with self.lock:
            path = Path(self.manifest['data_dir'], f'physical-config-control-{os.getpid()}.json')
            temporary = path.with_suffix('.pending')
            self.receipt['socket_guard_installed'] = self.socket_guard_installed()
            self.receipt['outbound_socket_guard_installed'] = self.receipt['socket_guard_installed']
            self.receipt['non_owned_loopback_blocked'] = self.receipt['socket_guard_installed']
            self.receipt['auxiliary_proxy_disabled'] = bool(
                self.receipt['socket_guard_installed']
                and {'app_info', 'alerts', 'urllib.request'} <= self.hooks.applied)
            temporary.write_text(json.dumps(self.receipt, sort_keys=True), encoding='utf-8')
            replace_with_retry(temporary, path)
            self._last_publish = time.monotonic()

    def _inventory(self, inventory):
        with self.lock:
            self.receipt['process_inventory'] = inventory
            self.publish()

    def _native_inventory(self, inventory):
        with self.lock:
            self.receipt['native_process_inventory'] = inventory
            self.publish()

    def _command_inventory(self, inventory):
        with self.lock:
            self.receipt['command_process_inventory'] = inventory
            self.publish()

    def _record_native_exits(self):
        with self.lock:
            processes = tuple(self._native_processes.values())
            commands = tuple(self._command_processes.values())
        for key, process in processes:
            self.native_registry.exited(key, process.poll())
        for key, process in commands:
            self.command_registry.exited(key, process.poll())

    def socket_guard_installed(self):
        required = {(id(socket), 'getaddrinfo')} | {
            (id(socket.socket), name) for name in ('connect', 'connect_ex', 'sendto', 'listen')}
        installed = {(id(owner), name) for owner, name, _old, _new in self._patches}
        return required <= installed and all(getattr(owner, name) is replacement
                                             for owner, name, _old, replacement in self._patches)

    def _query(self, name, record_type):
        connection = http.client.HTTPConnection('127.0.0.1', self.manifest['dns_controller_port'], timeout=1)
        try:
            connection.request('GET', '/dns/query?' + urlencode({'name': name, 'type': record_type}))
            response = connection.getresponse()
            if response.status != 200:
                raise socket.gaierror(socket.EAI_AGAIN, 'owned audit resolver unavailable')
            value = json.loads(response.read(1024 * 1024))
            number = 1 if record_type == 'A' else 28
            return [answer['data'] for answer in value.get('Answer', []) if answer.get('type') == number]
        finally:
            connection.close()

    def _address(self, sock, address):
        result = physical_socket_address(sock, address, interface_index=self.interface_index,
                                        resolver=socket.getaddrinfo, allowed_loopback_ports=self.allowed_ports)
        with self.lock:
            key = f'{int(sock.family)}:{int(sock.type)}'
            counters = self.receipt['outgoing_bindings']
            counters[key] = counters.get(key, 0) + 1
            if time.monotonic() - self._last_publish >= 1:
                self.publish()
        return result

    def install(self):
        numeric_getaddrinfo = socket.getaddrinfo

        def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
            return owned_dns_getaddrinfo(host, port, family, type, proto, flags,
                                        query=self._query, numeric_getaddrinfo=numeric_getaddrinfo)

        self._patch(socket, 'getaddrinfo', getaddrinfo)
        for method in ('connect', 'connect_ex'):
            original = getattr(socket.socket, method)

            def connect(sock, address, _original=original):
                return _original(sock, self._address(sock, address))

            self._patch(socket.socket, method, connect)
        original_sendto, original_listen = socket.socket.sendto, socket.socket.listen

        def sendto(sock, data, *args):
            return original_sendto(sock, data, *args[:-1], self._address(sock, args[-1]))

        def listen(sock, *args):
            result = original_listen(sock, *args)
            self.allowed_ports.track_listener(sock)
            return result

        self._patch(socket.socket, 'sendto', sendto)
        self._patch(socket.socket, 'listen', listen)
        self._install_command_observer()
        self.hooks.install()
        self.publish()
        atexit.register(self.finish)

    def finish(self):
        self._record_native_exits()
        self.activity.save()
        with self.lock:
            self.receipt['completed'] = True
            self.publish()

    def uninstall_for_test(self):
        atexit.unregister(self.finish)
        self.hooks.remove()
        for owner, name, old, replacement in reversed(self._patches):
            if getattr(owner, name) is replacement:
                setattr(owner, name, old)
        self._patches.clear()

    def _install_iocp(self, module):
        owner = module.IocpProactor
        original_connect, original_sendto = owner.connect, owner.sendto

        def connect(proactor, sock, address):
            return original_connect(proactor, sock, self._address(sock, address))

        def sendto(proactor, sock, buf, flags=0, addr=None):
            target = self._address(sock, addr if addr is not None else sock.getpeername())
            return original_sendto(proactor, sock, buf, flags, addr=target if addr is not None else None)

        self._patch(owner, 'connect', connect)
        self._patch(owner, 'sendto', sendto)

    def _install_process(self, module):
        owner = module.BaseProcess
        original_start, original_join, original_close, original_run = owner.start, owner.join, owner.close, owner.run

        def start(process):
            if not process.name.startswith('tier3-validator-helper-'):
                return original_start(process)
            key = self.registry.begin(process.name)
            self._process_attempts[id(process)] = key
            error = None
            try:
                return original_start(process)
            except BaseException as exc:
                error = type(exc).__name__
                raise
            finally:
                self.registry.launched(key, process.pid, error)

        def record_exit(process):
            key = self._process_attempts.get(id(process))
            if key is not None:
                self.registry.exited(key, process.exitcode)

        def join(process, *args, **kwargs):
            try:
                return original_join(process, *args, **kwargs)
            finally:
                record_exit(process)

        def close(process):
            record_exit(process)
            result = original_close(process)
            self._process_attempts.pop(id(process), None)
            return result

        def run(process):
            try:
                return original_run(process)
            finally:
                self.finish()

        for name, function in (('start', start), ('join', join), ('close', close), ('run', run)):
            self._patch(owner, name, function)

    def _install_supervisor(self, module):
        self.activity.install_supervisor(module, self._patch)
        original_spawn, original_send = module.Tier3WorkerStarter._spawn, module.Tier3ValidatorSupervisor._send

        def spawn(starter, *args, **kwargs):
            worker = original_spawn(starter, *args, **kwargs)
            self.registry.job_assigned(int(worker.process.pid), list(worker.job.snapshot().process_ids))
            return worker

        def send(supervisor, worker, kind, payload):
            result = original_send(supervisor, worker, kind, payload)
            if kind == 'start' and result:
                self.registry.start_sent(int(worker.process.pid))
            return result

        self._patch(module.Tier3WorkerStarter, '_spawn', spawn)
        self._patch(module.Tier3ValidatorSupervisor, '_send', send)
        for name in ('_confirm_prewarm_membership', '_confirm_ready_membership'):
            original = getattr(module.Tier3ValidatorSupervisor, name)

            def confirm(supervisor, worker, _original=original):
                result = _original(supervisor, worker)
                if result:
                    self.registry.job_assigned(int(worker.process.pid), list(worker.job.snapshot().process_ids))
                return result

            self._patch(module.Tier3ValidatorSupervisor, name, confirm)

    def _install_renderers(self, module):
        def wrap(function):
            @functools.wraps(function)
            def render(*args, **kwargs):
                value = function(*args, **kwargs)
                if value is None:
                    return None
                import yaml
                string = isinstance(value, str)
                config = yaml.safe_load(value) if string else value
                config['interface-name'] = self.interface
                pin_mihomo_audit_dns(config, self.manifest['dns_listener_port'])
                self._validate_config(config)
                with self.lock:
                    self.receipt['rendered'] += 1
                    self.publish()
                return yaml.safe_dump(config, allow_unicode=True, sort_keys=False) if string else config
            return render

        for name in ('build_full_clash_config', 'build_capture_disabled_clash_config',
                     'build_bootstrap_clash_config', 'build_validator_prewarm_config',
                     'build_single_proxy_validation_config'):
            if hasattr(module, name):
                self._patch(module, name, wrap(getattr(module, name)))

    def _validate_config(self, config):
        assert_physical_mihomo_config(config, self.interface, owned_loopback_ports=self.allowed_ports,
                                     owned_dns_port=self.manifest['dns_listener_port'])
        self.allowed_ports.register_mihomo_config(config)

    def _checked_core_file(self, runner):
        import yaml
        path = runner.runtime_config_path.resolve(strict=True)
        if not path.is_relative_to(Path(self.manifest['data_dir']).resolve(strict=True)):
            raise PermissionError('audit Core config is outside the isolated data directory')
        raw = path.read_bytes()
        config = yaml.safe_load(raw)
        if not isinstance(config, dict):
            raise ValueError('audit Core config must be a mapping')
        self._validate_config(config)
        with runner.binary_path.open('rb') as stream:
            binary_digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        return hashlib.sha256(raw).hexdigest(), binary_digest

    def _platform_command_binary(self, args, positional, kwargs):
        from scripts.tier3_audit_job_launch import system_console_host_path
        import winreg

        binary = system_console_host_path().with_name('cmd.exe').resolve(strict=True)
        executable = kwargs.get('executable') or os.environ.get('COMSPEC') or str(binary)
        if (args != 'ver' or positional or kwargs.get('shell') is not True
                or Path(executable).resolve(strict=True) != binary):
            raise PermissionError('audit platform query requires the system ver command')
        # cmd /c can run registry AutoRun entries before its explicit command.
        # Observe the real query only when it cannot execute those extra bytes.
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, r'Software\Microsoft\Command Processor') as key:
                    autorun, _kind = winreg.QueryValueEx(key, 'AutoRun')
            except FileNotFoundError:
                continue
            if autorun != '':
                raise PermissionError('audit platform query has unverified AutoRun commands')
        return binary

    def _install_command_observer(self):
        if self._command_observer_installed:
            return
        original_popen = subprocess.Popen
        guard = self

        class ObservedCommandProcess(original_popen):
            def __init__(process, args, *positional, **kwargs):
                scope = getattr(guard._version_context, 'scope', None)
                if scope is None:
                    super().__init__(args, *positional, **kwargs)
                    return
                platform_query = scope.get('kind') == 'platform_version'
                if not platform_query and (
                    scope['launched'] or not isinstance(args, (tuple, list))
                    or list(args) != scope['command'] or kwargs.get('shell', False)
                ):
                    raise PermissionError('audit version query must launch exactly one binary with -v')
                key = guard.command_registry.begin() if platform_query else scope['key']
                error = None
                try:
                    if platform_query:
                        binary = guard._platform_command_binary(args, positional, kwargs)
                        if scope['launched']:
                            raise PermissionError('audit platform query launched more than once')
                        with binary.open('rb') as stream:
                            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
                        command_digest = hashlib.sha256(json.dumps([str(binary), '/c', 'ver']).encode()).hexdigest()
                        guard.command_registry.configured(key, command_digest, digest)
                    scope['launched'] = True
                    guard.command_registry.creating(key)
                    super().__init__(args, *positional, **kwargs)
                except BaseException as exc:
                    error = type(exc).__name__
                    if platform_query:
                        with guard.lock:
                            guard.receipt['command_policy_violation_count'] += 1
                    raise
                finally:
                    pid = getattr(process, 'pid', None)
                    if pid is not None:
                        with guard.lock:
                            guard._command_processes[id(process)] = (key, process)
                    guard.command_registry.launched(key, pid, error)

        self._patch(subprocess, 'Popen', ObservedCommandProcess)
        self._command_observer_installed = True

    def _install_platform(self, module):
        self._install_command_observer()
        original = module._syscmd_ver

        @functools.wraps(original)
        def query(*args, **kwargs):
            previous = getattr(self._version_context, 'scope', None)
            self._version_context.scope = dict(kind='platform_version', launched=False)
            try:
                return original(*args, **kwargs)
            finally:
                self._version_context.scope = previous
                self._record_native_exits()

        self._patch(module, '_syscmd_ver', query)

    def _install_core_runner(self, module):
        self._install_command_observer()
        owner = module.MihomoCoreRunner
        original_init, original_reload, original_stop = owner.__init__, owner.hot_reload, owner.stop
        original_query = owner._query_version

        def query_version(runner, *args, **kwargs):
            key = self.command_registry.begin()
            scope = dict(key=key, command=[str(runner.binary_path), '-v'], launched=False)
            previous = getattr(self._version_context, 'scope', None)
            error = None
            try:
                with runner.binary_path.open('rb') as stream:
                    digest = hashlib.file_digest(stream, 'sha256').hexdigest()
                command_digest = hashlib.sha256(json.dumps(scope['command']).encode()).hexdigest()
                self.command_registry.configured(key, command_digest, digest)
                self._version_context.scope = scope
                result = original_query(runner, *args, **kwargs)
                if not scope['launched']:
                    raise RuntimeError('audit version query has no native process receipt')
                return result
            except BaseException as exc:
                error = type(exc).__name__
                raise
            finally:
                self._version_context.scope = previous
                if not scope['launched']:
                    self.command_registry.launched(key, None, error)
                self._record_native_exits()

        def initialize(runner, *args, **kwargs):
            original_init(runner, *args, **kwargs)
            original_factory = runner.process_factory

            def create(*args, **kwargs):
                key = self.native_registry.begin()
                process, error = None, None
                try:
                    config_digest, binary_digest = self._checked_core_file(runner)
                    self.native_registry.configured(key, config_digest, binary_digest)
                    self.native_registry.creating(key)
                    process = original_factory(*args, **kwargs)
                    with self.lock:
                        # Retain each handle, even if the runner is replaced or
                        # startup raises after the operating system created it.
                        self._native_processes[id(process)] = (key, process)
                    return process
                except BaseException as exc:
                    error = type(exc).__name__
                    raise
                finally:
                    self.native_registry.launched(key, getattr(process, 'pid', None), error)

            runner.process_factory = create

        def reload(runner, *args, **kwargs):
            if runner.is_running():
                with self.lock:
                    registered = self._native_processes.get(id(runner.process))
                if registered is None or registered[1] is not runner.process:
                    raise PermissionError('audit cannot reload an unregistered Core')
                config_digest, binary_digest = self._checked_core_file(runner)
                self.native_registry.configured(registered[0], config_digest, binary_digest)
            return original_reload(runner, *args, **kwargs)

        def stop(runner, *args, **kwargs):
            try:
                return original_stop(runner, *args, **kwargs)
            finally:
                self._record_native_exits()

        for name, replacement in (('__init__', initialize), ('hot_reload', reload), ('stop', stop),
                                  ('_query_version', query_version)):
            self._patch(owner, name, replacement)

    def _install_prewarm(self, module):
        original = module.ValidatorPrewarmProcess.__init__
        original_close = module.ValidatorPrewarmProcess.close

        def record_process(prewarm):
            process = getattr(prewarm, '_popen', None)
            if process is not None:
                with self.lock:
                    items = self.receipt['mihomo_processes']
                    item = next((item for item in items if item['pid'] == process.pid), None)
                    if item is None:
                        item = {'pid': process.pid, 'helper_pid': os.getpid()}
                        items.append(item)
                    item['exitcode'] = process.poll()
                    self.publish()

        def initialize(prewarm, payload, send):
            self._validate_config(payload['prewarm_config'])
            with self.lock:
                self.receipt['received_prewarm'] += 1
                self.publish()
            try:
                return original(prewarm, payload, send)
            finally:
                record_process(prewarm)

        def close(prewarm):
            try:
                return original_close(prewarm)
            finally:
                record_process(prewarm)

        self._patch(module.ValidatorPrewarmProcess, '__init__', initialize)
        self._patch(module.ValidatorPrewarmProcess, 'close', close)

    def _block_system_proxy(self, module):
        def forbidden(*args, **kwargs):
            raise PermissionError('isolated audit cannot mutate the system proxy')
        self._patch(module.WinRegProxyBackend, 'write', forbidden)

    def _block_firewall(self, module):
        def forbidden(*args, **kwargs):
            raise PermissionError('isolated audit cannot mutate the system firewall')
        self._patch(module.KillSwitchManager, '_run_command', forbidden)
        self._block_recovery_firewall(module)

    def _block_recovery_firewall(self, module):
        # RT-1 uses a standalone deletion function, including an imported alias.
        def forbidden(*args, **kwargs):
            raise PermissionError('isolated audit cannot mutate the system firewall')
        if hasattr(module, 'recovery_delete_owned_firewall_rule'):
            self._patch(module, 'recovery_delete_owned_firewall_rule', forbidden)

    def _disable_update_checks(self, module):
        self._patch(module, 'APP_UPDATE_ENABLED', False)

    def _disable_alert_delivery(self, module):
        self._patch(module.AlertManager, '_configured_alert_channels', lambda _owner: ())

    def _disable_system_proxy_discovery(self, module):
        self._patch(module, '_opener', module.build_opener(module.ProxyHandler({})))

    def _isolate_window_identity(self, module):
        for name, suffix in (
                ('acquire_single_instance_lock', 'instance'),
                ('create_single_instance_restore_event', 'restore'),
                ('signal_existing_instance', 'restore')):
            original = getattr(module, name)
            identity = f"Local\\TesseractAudit-{self.manifest['audit_id']}-{suffix}"

            def invoke(*_args, _original=original, _identity=identity, **_kwargs):
                return _original(_identity)
            self._patch(module, name, invoke)

    def _observe_window_start(self, module):
        # Support a source interpreter whose desktop module was already loaded
        # as well as the frozen loader, without bypassing the OS mutex contract.
        import desktop_app_shell
        for name in ('acquire_single_instance_lock', 'create_single_instance_restore_event',
                     'signal_existing_instance'):
            self._patch(module, name, getattr(desktop_app_shell, name))
        self._patch(module, 'APP_UPDATE_ENABLED', False)
        owner = module.ProxyDesktopWindow
        original = owner.start_startup_window_audit

        @functools.wraps(original)
        def begin(window, *args, **kwargs):
            marker = dict(window_started_monotonic=time.monotonic(),
                          process_id=os.getpid(), parent_process_id=os.getppid(),
                          audit_id=self.manifest['audit_id'],
                          manifest_sha256=self.manifest['manifest_sha256'])
            for filename in ('window-start-control.json', 'child-process-control.json'):
                destination = Path(self.manifest['data_dir'], filename)
                pending = destination.with_suffix('.pending')
                pending.write_text(json.dumps(marker), encoding='utf-8')
                replace_with_retry(pending, destination)
            return original(window, *args, **kwargs)
        self._patch(owner, 'start_startup_window_audit', begin)


def install_from_environment():
    global _guard
    path = os.environ.get(ENVIRONMENT_KEY)
    if not path:
        return None
    if _guard is None:
        _guard = IsolatedAuditGuard(read_manifest(path, os.environ))
        _guard.install()
    return _guard
