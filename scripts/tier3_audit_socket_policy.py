from __future__ import annotations

"""Physical-interface binding for isolated audits, never host route mutation."""

import ipaddress
import socket
from collections.abc import Container, Mapping
from typing import Any, Callable
import weakref


_DNS_UPSTREAM_LISTS = ("default-nameserver", "nameserver", "proxy-server-nameserver")
_DNS_ROUTE_OVERRIDES = (
    "nameserver-policy", "proxy-server-nameserver-policy", "direct-nameserver",
    "fallback", "fallback-filter",
)


def pin_mihomo_audit_dns(config: dict[str, Any], owned_dns_port: int) -> None:
    """Replace audit-copy upstreams only, including policies and fallbacks."""
    if type(owned_dns_port) is not int or not 1 <= owned_dns_port <= 65535:
        raise ValueError("audit DNS requires an owned listener port")
    dns = config.setdefault("dns", {})
    if dns is None:
        dns = config["dns"] = {}
    dns["enable"] = True
    for key in _DNS_UPSTREAM_LISTS:
        dns[key] = [f"127.0.0.1:{owned_dns_port}"]
    for key in _DNS_ROUTE_OVERRIDES:
        dns.pop(key, None)
    dns["respect-rules"] = False
    dns["direct-nameserver-follow-policy"] = False


def owned_dns_getaddrinfo(
    host, port, family=socket.AF_UNSPEC, sock_type=0, proto=0, flags=0,
    *, query: Callable[[str, str], list[str]], numeric_getaddrinfo: Callable,
) -> list:
    """Use the owned resolver for names; the system API only formats literals."""
    name = host.decode("ascii") if isinstance(host, bytes) else host
    try:
        ipaddress.ip_address(name)
        numeric = True
    except (ValueError, TypeError):
        numeric = False
    if name is None:
        return numeric_getaddrinfo(host, port, family, sock_type, proto, flags)
    if numeric:
        return numeric_getaddrinfo(host, port, family, sock_type, proto, flags | socket.AI_NUMERICHOST)
    if flags & socket.AI_NUMERICHOST:
        raise socket.gaierror(socket.EAI_NONAME, "numeric audit address required")
    if family not in (socket.AF_UNSPEC, socket.AF_INET, socket.AF_INET6):
        raise socket.gaierror(socket.EAI_FAMILY, "unsupported audit address family")
    families = ((socket.AF_INET, "A"), (socket.AF_INET6, "AAAA"))
    localhost = isinstance(name, str) and name.rstrip(".").lower() == "localhost"
    result = []
    query_error = None
    for address_family, record_type in families:
        if family not in (socket.AF_UNSPEC, address_family):
            continue
        if localhost:
            addresses = ["127.0.0.1" if address_family == socket.AF_INET else "::1"]
        else:
            try:
                addresses = query(str(name), record_type)
            except OSError as exc:
                query_error = exc
                continue
        for address in addresses:
            try:
                parsed = ipaddress.ip_address(address)
            except (ValueError, TypeError) as exc:
                raise socket.gaierror(socket.EAI_FAIL, "non-numeric owned DNS answer") from exc
            if (parsed.version == 4) != (address_family == socket.AF_INET):
                continue
            result.extend(numeric_getaddrinfo(
                address, port, address_family, sock_type, proto, flags | socket.AI_NUMERICHOST,
            ))
    if result:
        return result
    if query_error is not None:
        raise query_error
    raise socket.gaierror(socket.EAI_NONAME, "owned resolver returned no address")


def verified_audit_child_pid(
    marker: Mapping[str, Any], *, launcher_pid: int, job_process_ids: Container[int],
) -> int | None:
    """Windows venv's launcher PID can differ from the executing interpreter."""
    pid = marker.get("process_id")
    parent = marker.get("parent_process_id")
    if (type(pid) is not int or pid <= 0 or pid not in job_process_ids
            or (pid != launcher_pid and parent != launcher_pid)):
        return None
    return pid


def assert_physical_mihomo_config(
    config: Mapping[str, Any], interface: str,
    *, owned_loopback_ports: Container[int] | None = None,
    owned_dns_port: int | None = None,
) -> None:
    if not interface or config.get("interface-name") != interface:
        raise ValueError("audit Core config must bind the physical interface")
    if (config.get("tun") or {}).get("enable"):
        raise ValueError("audit Core config must not enable TUN capture")
    dns = config.get("dns") or {}
    if owned_dns_port is not None:
        if dns.get("enable") is not True:
            raise ValueError("audit config requires enabled owned DNS")
        if type(owned_dns_port) is not int or not 1 <= owned_dns_port <= 65535:
            raise ValueError("audit DNS requires an owned listener port")
        expected = [f"127.0.0.1:{owned_dns_port}"]
        if (any(dns.get(key) != expected for key in _DNS_UPSTREAM_LISTS)
                or any(dns.get(key) for key in _DNS_ROUTE_OVERRIDES)
                or dns.get("respect-rules") or dns.get("direct-nameserver-follow-policy")):
            raise ValueError("audit DNS must not inherit host or policy upstreams")
    for proxy in config.get("proxies", ()):
        if proxy.get("interface-name", interface) != interface:
            raise ValueError("audit proxy must not override the physical interface")
        try:
            address = ipaddress.ip_address(str(proxy.get("server", "")))
        except ValueError:
            continue
        mapped = address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) else None
        address = mapped or address
        if address.is_unspecified or (
            address.is_loopback and owned_loopback_ports is not None
            and int(proxy.get("port", 0)) not in owned_loopback_ports
        ):
            raise ValueError("audit proxy must not use an unowned local destination")


def physical_process_receipt_failures(
    receipts: Mapping[int, Mapping[str, Any]], *, parent_pid: int, interface: str,
    require_helpers: bool = True, require_core: bool = True,
) -> list[str]:
    """Require final isolation receipts from every spawned validation process."""
    parent = receipts.get(parent_pid, {})
    if isinstance(parent.get("process_inventory"), Mapping):
        from scripts.tier3_audit_process_registry import (
            native_process_inventory_failures, process_inventory_failures,
        )
        inventory = parent['process_inventory']
        failures = process_inventory_failures(
            inventory, receipts, interface=interface,
            require_helpers=require_helpers,
        )
        attempts = inventory.get('attempts', [])
        registered = {parent_pid}
        if isinstance(attempts, list):
            registered.update(item.get('pid') for item in attempts
                              if isinstance(item, Mapping) and type(item.get('pid')) is int)
        failures.extend(f'isolation_unregistered_process:{pid}'
                        for pid in sorted(set(receipts) - registered, key=str))
        if (parent.get('process_id') != parent_pid
                or parent.get('interface') != interface
                or parent.get('socket_guard_installed') is not True):
            failures.append(f'isolation_process_unproven:{parent_pid}')
        if type(parent.get('rendered')) is not int or parent['rendered'] <= 0:
            failures.append(f'isolation_config_unproven:{parent_pid}')
        manifest = parent.get('manifest_sha256')
        audit_id = parent.get('audit_id')
        if not all(isinstance(value, str) and len(value) == length
                   and all(character in '0123456789abcdef' for character in value)
                   for value, length in ((manifest, 64), (audit_id, 32))):
            failures.append('isolation_manifest_unproven')
        for pid in registered:
            receipt = receipts.get(pid, {})
            if receipt.get('completed') is not True:
                failures.append(f'isolation_final_receipt_unproven:{pid}')
            if receipt.get('manifest_sha256') != manifest or receipt.get('audit_id') != audit_id:
                failures.append(f'isolation_manifest_mismatch:{pid}')
        native_pids = set()
        for attempt in attempts if isinstance(attempts, list) else ():
            if not isinstance(attempt, Mapping) or type(attempt.get('pid')) is not int:
                continue
            pid = attempt['pid']
            children = receipts.get(pid, {}).get('mihomo_processes')
            if not isinstance(children, list) or (attempt.get('start_sent') and not children):
                failures.append(f'isolation_native_inventory_unproven:{pid}')
                continue
            for child in children:
                if (not isinstance(child, Mapping) or type(child.get('pid')) is not int
                        or child['pid'] <= 0 or child['pid'] == pid
                        or child.get('helper_pid') != pid
                        or child['pid'] not in attempt.get('job_process_ids', ())
                        or type(child.get('exitcode')) is not int
                        or child['pid'] in native_pids):
                    failures.append(f'isolation_native_exit_unproven:{pid}')
                if isinstance(child, Mapping) and type(child.get('pid')) is int:
                    native_pids.add(child['pid'])
        for pid in registered:
            native = receipts.get(pid, {}).get('native_process_inventory')
            failures.extend(native_process_inventory_failures(
                native, other_pids=registered | native_pids,
            ))
            core_attempts = native.get('attempts', []) if isinstance(native, Mapping) else []
            if isinstance(core_attempts, list):
                if require_core and pid == parent_pid and not any(
                    isinstance(item, Mapping) and type(item.get('pid')) is int
                    for item in core_attempts
                ):
                    failures.append('isolation_core_launch_unproven')
                native_pids.update(item['pid'] for item in core_attempts
                                   if isinstance(item, Mapping) and type(item.get('pid')) is int)
        return failures
    # A PID list cannot account for failed starts, native children or exits.
    return ['isolation_process_inventory_unproven']


class OwnedLoopbackEndpoints:
    """Explicit Core ports plus still-listening Python-owned wakeup sockets."""

    def __init__(self, ports=()) -> None:
        self._ports = {int(port) for port in ports}
        self._listeners = weakref.WeakValueDictionary()

    def add(self, port: int) -> None:
        self._ports.add(int(port))

    def register_mihomo_config(self, config: Mapping[str, Any]) -> None:
        for key in ("port", "mixed-port", "socks-port"):
            if config.get(key):
                self.add(int(config[key]))
        if config.get("external-controller"):
            self.add(int(config["external-controller"].rsplit(":", 1)[-1]))
        for listener in config.get("listeners", ()):
            if listener.get("port"):
                self.add(int(listener["port"]))

    def track_listener(self, sock: socket.socket) -> None:
        if sock.family not in (socket.AF_INET, socket.AF_INET6):
            return
        address = sock.getsockname()
        if ipaddress.ip_address(address[0]).is_loopback:
            self._listeners[int(address[1])] = sock

    def __contains__(self, port: int) -> bool:
        if int(port) in self._ports:
            return True
        listener = self._listeners.get(int(port))
        if listener is None:
            return False
        try:
            return bool(listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN))
        except OSError:
            return False


def _bind_outgoing_interface(sock: Any, level: int, interface_index: int) -> None:
    # Windows' IPv4 setter expects network order, but both getters return the
    # index in host order. A successful setter alone is not isolation evidence.
    value = socket.htonl(interface_index) if level == socket.IPPROTO_IP else interface_index
    sock.setsockopt(level, 31, value)
    readback = sock.getsockopt(level, 31)
    if type(readback) is not int or readback != interface_index:
        raise PermissionError("physical audit interface readback did not match")


def physical_socket_address(
    sock: Any,
    address: tuple,
    *,
    interface_index: int,
    resolver: Callable[..., list],
    allowed_loopback_ports: Container[int] | None = None,
) -> tuple:
    if sock.family not in (socket.AF_INET, socket.AF_INET6):
        return address
    if interface_index <= 0:
        raise ValueError("physical audit requires a verified interface index")
    try:
        ip = ipaddress.ip_address(address[0])
    except ValueError:
        results = resolver(address[0], address[1], sock.family, sock.type)
        if not results:
            raise socket.gaierror(socket.EAI_NONAME, "physical resolver returned no address")
        address = results[0][4]
        ip = ipaddress.ip_address(address[0])
    effective_ip = ip.ipv4_mapped if isinstance(ip, ipaddress.IPv6Address) else None
    effective_ip = effective_ip or ip
    if effective_ip.is_unspecified:
        raise PermissionError("physical audit refuses an unspecified destination")
    if effective_ip.is_loopback:
        if allowed_loopback_ports is not None and int(address[1]) not in allowed_loopback_ports:
            raise PermissionError("physical audit refuses a non-owned loopback endpoint")
    else:
        if sock.family == socket.AF_INET:
            _bind_outgoing_interface(sock, socket.IPPROTO_IP, interface_index)
        else:
            _bind_outgoing_interface(sock, socket.IPPROTO_IPV6, interface_index)
            if effective_ip.version == 4:
                # Windows keeps independent IPv4/IPv6 defaults on dual sockets.
                _bind_outgoing_interface(sock, socket.IPPROTO_IP, interface_index)
    return address
