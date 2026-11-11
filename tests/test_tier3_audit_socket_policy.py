from __future__ import annotations

import socket
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts.tier3_audit_socket_policy import OwnedLoopbackEndpoints, physical_socket_address
from scripts.tier3_audit_socket_policy import assert_physical_mihomo_config
from scripts.tier3_audit_socket_policy import physical_process_receipt_failures
from scripts.tier3_audit_socket_policy import owned_dns_getaddrinfo
from scripts.tier3_audit_socket_policy import verified_audit_child_pid
from scripts.tier3_audit_socket_policy import pin_mihomo_audit_dns


def test_physical_dns_removes_inherited_host_and_policy_routes():
    config = {"interface-name": "physical", "dns": {
        "enable": True, "nameserver": ["system"], "default-nameserver": ["198.18.0.2"],
        "proxy-server-nameserver": ["dhcp://external"],
        "direct-nameserver": ["198.18.0.2"],
        "nameserver-policy": {"+.example": "system"},
        "proxy-server-nameserver-policy": {"+.example": "system"},
        "fallback": ["198.18.0.2"], "fallback-filter": {"geoip": True},
        "respect-rules": True, "direct-nameserver-follow-policy": True,
    }}
    pin_mihomo_audit_dns(config, 19100)
    assert_physical_mihomo_config(config, "physical", owned_dns_port=19100)
    assert config["dns"]["nameserver"] == ["127.0.0.1:19100"]


@pytest.mark.parametrize("dns", [None, {}, {"enable": False}])
def test_disabled_or_missing_dns_cannot_fall_back_to_host_resolution(dns):
    config = {"interface-name": "physical"}
    if dns is not None:
        config["dns"] = dns
    with pytest.raises(ValueError, match="enabled owned DNS"):
        assert_physical_mihomo_config(config, "physical", owned_dns_port=19100)
    pin_mihomo_audit_dns(config, 19100)
    assert config["dns"]["enable"] is True
    assert_physical_mihomo_config(config, "physical", owned_dns_port=19100)


@pytest.mark.parametrize("family,answers,queries,expected_family", [
    (socket.AF_INET, {"A": ["192.0.2.8"]}, ["A"], socket.AF_INET),
    (socket.AF_INET6, {"AAAA": ["2001:db8::8"]}, ["AAAA"], socket.AF_INET6),
    (socket.AF_UNSPEC, {"A": [], "AAAA": ["2001:db8::8"]}, ["A", "AAAA"], socket.AF_INET6),
    (socket.AF_UNSPEC, {"A": ["192.0.2.8"], "AAAA": []}, ["A", "AAAA"], socket.AF_INET),
])
def test_owned_resolution_honors_address_family_without_host_dns(family, answers, queries, expected_family):
    from scripts.tier3_audit_socket_policy import owned_dns_getaddrinfo

    query = Mock(side_effect=lambda name, record: answers[record])
    numeric_calls = []

    def numeric_only(host, port, resolved_family, sock_type, proto, flags):
        import ipaddress
        ipaddress.ip_address(host)
        numeric_calls.append(host)
        return socket.getaddrinfo(host, port, resolved_family, sock_type, proto, flags)

    result = owned_dns_getaddrinfo("audit.invalid", 443, family, socket.SOCK_STREAM,
        query=query, numeric_getaddrinfo=numeric_only)
    assert all(row[0] == expected_family for row in result)
    assert [call.args[1] for call in query.call_args_list] == queries
    assert numeric_calls


def test_failed_owned_resolution_never_retries_through_host_dns():
    from scripts.tier3_audit_socket_policy import owned_dns_getaddrinfo

    host_dns = Mock(side_effect=AssertionError("must not call host resolver"))
    for query in (Mock(return_value=[]), Mock(side_effect=TimeoutError("owned DNS timeout"))):
        with pytest.raises((socket.gaierror, TimeoutError)):
            owned_dns_getaddrinfo("audit.invalid", 443, query=query, numeric_getaddrinfo=host_dns)
    host_dns.assert_not_called()


@pytest.mark.parametrize("key,value", [
    ("default-nameserver", ["198.18.0.2"]), ("nameserver", ["system"]),
    ("proxy-server-nameserver", ["dhcp://external"]),
    ("nameserver-policy", {"+.example": "system"}),
    ("proxy-server-nameserver-policy", {"+.example": "system"}),
    ("direct-nameserver", ["198.18.0.2"]), ("fallback", ["198.18.0.2"]),
    ("respect-rules", True), ("direct-nameserver-follow-policy", True),
])
def test_received_audit_config_cannot_restore_external_dns(key, value):
    config = {"interface-name": "physical", "dns": {"enable": True}}
    pin_mihomo_audit_dns(config, 19100)
    config["dns"][key] = value
    with pytest.raises(ValueError, match="inherit"):
        assert_physical_mihomo_config(config, "physical", owned_dns_port=19100)


@pytest.mark.parametrize("port", [0, -1, 65536, True, "19100"])
def test_audit_dns_binding_cannot_use_an_invalid_port(port):
    with pytest.raises(ValueError, match="owned listener"):
        pin_mihomo_audit_dns({"dns": {"enable": True}}, port)


def test_venv_redirector_is_not_confused_with_the_guarded_interpreter():
    marker = dict(process_id=200, parent_process_id=100)
    assert verified_audit_child_pid(marker, launcher_pid=100, job_process_ids={100, 200}) == 200
    assert verified_audit_child_pid(marker, launcher_pid=200, job_process_ids={200}) == 200
    assert verified_audit_child_pid(marker, launcher_pid=100, job_process_ids={100}) is None
    assert verified_audit_child_pid(marker, launcher_pid=99, job_process_ids={99, 200}) is None


@pytest.mark.parametrize("pid", [None, 0, True, "200"])
def test_invalid_interpreter_marker_cannot_bind_receipts(pid):
    assert verified_audit_child_pid(dict(process_id=pid, parent_process_id=100),
        launcher_pid=100, job_process_ids={100, 200}) is None


def process_receipts():
    from tests.test_tier3_audit_final_receipts import receipts
    values = receipts()
    values[1]['process_inventory']['attempts'].append(dict(
        attempt_id='helper-2-1:2', pid=3, launch_finished=True, job_process_ids=[3, 30],
        start_sent=True, exit_confirmed=True, exitcode=0,
    ))
    values[3] = dict(values[2], process_id=3,
                     mihomo_processes=[dict(pid=30, helper_pid=3, exitcode=0)])
    return values


def test_all_spawned_processes_require_their_own_final_isolation_receipt():
    receipts = process_receipts()
    assert not physical_process_receipt_failures(receipts, parent_pid=1, interface="physical")
    receipts.pop(3)
    assert physical_process_receipt_failures(receipts, parent_pid=1, interface="physical")


def test_guard_receipt_without_matching_launch_inventory_invalidates_audit():
    receipts = process_receipts()
    receipts[1]['process_inventory']['attempts'] = []
    receipts.pop(3)
    assert physical_process_receipt_failures(
        receipts, parent_pid=1, interface='physical', require_helpers=False,
    ) == ['isolation_unregistered_process:2']


@pytest.mark.parametrize("field,value", [("interface", "tun"), ("process_id", 4),
    ("socket_guard_installed", False), ("received_prewarm", 0), ("received_prewarm", True)])
def test_parent_guard_does_not_prove_child_transport_isolation(field, value):
    receipts = process_receipts()
    receipts[2][field] = value
    assert physical_process_receipt_failures(receipts, parent_pid=1, interface="physical")


@pytest.mark.parametrize("inventory", [None, [], [0], [1], [True], ["2"]])
def test_missing_or_invalid_helper_inventory_cannot_prove_full_bypass(inventory):
    receipts = process_receipts()
    receipts[1].pop('process_inventory')
    receipts[1]["spawned_helper_pids"] = inventory
    assert physical_process_receipt_failures(receipts, parent_pid=1, interface="physical")


@pytest.mark.parametrize("family,address,level,index", [
    (socket.AF_INET, ("192.0.2.1", 443), socket.IPPROTO_IP, socket.htonl(15)),
    (socket.AF_INET6, ("2001:db8::1", 443, 0, 0), socket.IPPROTO_IPV6, 15),
])
def test_physical_binding_applies_before_every_sync_or_async_connect(family, address, level, index):
    sock = SimpleNamespace(family=family, type=socket.SOCK_STREAM, setsockopt=Mock(),
                           getsockopt=Mock(return_value=15))
    resolver = Mock(side_effect=AssertionError("numeric address must not use host DNS"))
    assert physical_socket_address(sock, address, interface_index=15, resolver=resolver) == address
    sock.setsockopt.assert_called_once_with(level, 31, index)
    sock.getsockopt.assert_called_once_with(level, 31)


@pytest.mark.parametrize("family,address", [
    (socket.AF_INET, ("192.0.2.1", 443)),
    (socket.AF_INET6, ("2001:db8::1", 443, 0, 0)),
])
@pytest.mark.parametrize("readback", [0, 16, None, True, "15"])
def test_interface_write_success_without_exact_readback_fails_closed(family, address, readback):
    sock = SimpleNamespace(family=family, type=socket.SOCK_STREAM, setsockopt=Mock(),
                           getsockopt=Mock(return_value=readback))
    with pytest.raises(PermissionError, match="interface readback"):
        physical_socket_address(sock, address, interface_index=15, resolver=Mock())


def test_interface_readback_error_never_returns_a_dispatchable_address():
    sock = SimpleNamespace(family=socket.AF_INET, type=socket.SOCK_STREAM,
                           setsockopt=Mock(), getsockopt=Mock(side_effect=OSError("unavailable")))
    with pytest.raises(OSError, match="unavailable"):
        physical_socket_address(sock, ("192.0.2.1", 443), interface_index=15, resolver=Mock())


def test_ipv4_mapped_transport_cannot_use_the_default_ipv4_interface():
    sock = SimpleNamespace(family=socket.AF_INET6, type=socket.SOCK_STREAM, setsockopt=Mock(),
                           getsockopt=Mock(return_value=15))
    physical_socket_address(sock, ("::ffff:192.0.2.1", 443, 0, 0),
                            interface_index=15, resolver=Mock())
    assert [call.args for call in sock.setsockopt.call_args_list] == [
        (socket.IPPROTO_IPV6, 31, 15),
        (socket.IPPROTO_IP, 31, socket.htonl(15)),
    ]
    assert [call.args for call in sock.getsockopt.call_args_list] == [
        (socket.IPPROTO_IPV6, 31), (socket.IPPROTO_IP, 31),
    ]


def test_dual_stack_ipv6_success_cannot_hide_ipv4_binding_failure():
    sock = SimpleNamespace(family=socket.AF_INET6, type=socket.SOCK_STREAM,
                           setsockopt=Mock(), getsockopt=Mock(side_effect=[15, 0]))
    with pytest.raises(PermissionError, match="interface readback"):
        physical_socket_address(sock, ("::ffff:192.0.2.1", 443, 0, 0),
                                interface_index=15, resolver=Mock())


@pytest.mark.skipif(sys.platform != "win32", reason="Windows per-family socket options")
def test_dual_stack_native_socket_reads_back_both_outgoing_interfaces_without_sending():
    with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        # Loopback's interface index tests the OS API without any connection.
        physical_socket_address(sock, ("::ffff:192.0.2.1", 9, 0, 0),
                                interface_index=1, resolver=Mock())
        assert sock.getsockopt(socket.IPPROTO_IP, 31) == 1
        assert sock.getsockopt(socket.IPPROTO_IPV6, 31) == 1


@pytest.mark.parametrize("address", [("127.0.0.1", 12345), ("::1", 12345, 0, 0)])
def test_owned_loopback_listener_remains_loopback(address):
    family = socket.AF_INET if len(address) == 2 else socket.AF_INET6
    sock = SimpleNamespace(family=family, type=socket.SOCK_STREAM, setsockopt=Mock())
    assert physical_socket_address(sock, address, interface_index=15, resolver=Mock()) == address
    sock.setsockopt.assert_not_called()


def test_sync_hostname_resolution_uses_only_the_owned_resolver():
    sock = SimpleNamespace(family=socket.AF_INET, type=socket.SOCK_STREAM, setsockopt=Mock(),
                           getsockopt=Mock(return_value=15))
    resolver = Mock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.8", 443))])
    assert physical_socket_address(sock, ("audit.example", 443), interface_index=15, resolver=resolver) == ("192.0.2.8", 443)
    resolver.assert_called_once_with("audit.example", 443, socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt.assert_called_once_with(socket.IPPROTO_IP, 31, socket.htonl(15))


@pytest.mark.parametrize("allowed", [False, True])
def test_external_loopback_proxy_cannot_escape_physical_binding(allowed):
    sock = SimpleNamespace(family=socket.AF_INET, type=socket.SOCK_STREAM, setsockopt=Mock())
    ports = {19101} if allowed else {19102}
    kwargs = dict(interface_index=15, resolver=Mock(), allowed_loopback_ports=ports)
    if allowed:
        assert physical_socket_address(sock, ("127.0.0.1", 19101), **kwargs) == ("127.0.0.1", 19101)
    else:
        with pytest.raises(PermissionError, match="non-owned"):
            physical_socket_address(sock, ("127.0.0.1", 19101), **kwargs)
    sock.setsockopt.assert_not_called()


def test_owned_dns_answer_cannot_redirect_to_an_external_loopback_proxy():
    sock = SimpleNamespace(family=socket.AF_INET, type=socket.SOCK_STREAM, setsockopt=Mock())
    resolver = Mock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 19102))])
    with pytest.raises(PermissionError, match="non-owned"):
        physical_socket_address(sock, ("audit.example", 19102), interface_index=15,
                                resolver=resolver, allowed_loopback_ports={19101})


@pytest.mark.parametrize("address", [
    ("::ffff:127.0.0.1", 19102, 0, 0),
    ("::ffff:127.1.2.3", 19102, 0, 0),
])
def test_ipv4_mapped_loopback_cannot_reach_external_proxy(address):
    sock = SimpleNamespace(family=socket.AF_INET6, type=socket.SOCK_STREAM, setsockopt=Mock())
    with pytest.raises(PermissionError, match="non-owned"):
        physical_socket_address(sock, address, interface_index=15,
            resolver=Mock(), allowed_loopback_ports={19101})
    sock.setsockopt.assert_not_called()
    assert physical_socket_address(sock, address, interface_index=15,
        resolver=Mock(), allowed_loopback_ports={19102}) == address


@pytest.mark.parametrize("address", [("0.0.0.0", 19101), ("::", 19101, 0, 0),
                                     ("::ffff:0.0.0.0", 19101, 0, 0)])
def test_unspecified_destination_never_uses_a_local_or_default_route(address):
    sock = SimpleNamespace(family=socket.AF_INET if len(address) == 2 else socket.AF_INET6,
        type=socket.SOCK_STREAM, setsockopt=Mock())
    with pytest.raises(PermissionError, match="unspecified"):
        physical_socket_address(sock, address, interface_index=15,
            resolver=Mock(), allowed_loopback_ports={19101})
    sock.setsockopt.assert_not_called()


def test_owned_wakeup_listener_lease_expires_on_close():
    ports = OwnedLoopbackEndpoints()
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        ports.track_listener(listener)
        assert port in ports
    assert port not in ports


def test_received_prewarm_config_authorizes_helper_ports_in_child():
    import json
    from tier3_validation_config import build_validator_prewarm_config

    config = build_validator_prewarm_config(19101, controller_port=19102)
    parent = OwnedLoopbackEndpoints()
    parent.register_mihomo_config(config)
    child = OwnedLoopbackEndpoints()
    assert 19102 not in child
    child.register_mihomo_config(json.loads(json.dumps(config)))
    sock = SimpleNamespace(family=socket.AF_INET, type=socket.SOCK_STREAM, setsockopt=Mock())
    for port in (19101, 19102):
        assert physical_socket_address(sock, ("127.0.0.1", port), interface_index=15,
            resolver=Mock(), allowed_loopback_ports=child) == ("127.0.0.1", port)
    with pytest.raises(PermissionError, match="non-owned"):
        physical_socket_address(sock, ("127.0.0.1", 19103), interface_index=15,
            resolver=Mock(), allowed_loopback_ports=child)


def test_only_configured_core_listener_ports_are_published():
    ports = OwnedLoopbackEndpoints()
    ports.register_mihomo_config({"mixed-port": 19101,
        "external-controller": "127.0.0.1:19102",
        "listeners": [{"type": "http", "port": 19103}],
        "proxies": [{"server": "127.0.0.1", "port": 19104}]})
    assert all(port in ports for port in (19101, 19102, 19103))
    assert 19104 not in ports


@pytest.mark.parametrize("change", [
    {"interface-name": "external-tun"},
    {"tun": {"enable": True}},
    {"proxies": [{"interface-name": "external-tun"}]},
    {"proxies": [{"interface-name": ""}]},
])
def test_audit_rejects_capture_and_per_node_interface_escape(change):
    config = {"interface-name": "physical", "tun": {"enable": False}}
    config.update(change)
    with pytest.raises(ValueError):
        assert_physical_mihomo_config(config, "physical")


def test_owned_core_and_received_prewarm_use_same_physical_contract():
    assert_physical_mihomo_config({"interface-name": "physical", "proxies": [{}]}, "physical")
    assert_physical_mihomo_config({"interface-name": "physical", "rules": ["MATCH,REJECT"]}, "physical")


@pytest.mark.parametrize("server", ["127.0.0.1", "::1", "::ffff:127.0.0.1",
                                    "0.0.0.0", "::", "::ffff:0.0.0.0"])
def test_core_proxy_config_cannot_delegate_to_an_external_local_proxy(server):
    config = {"interface-name": "physical", "proxies": [{"server": server, "port": 19102}]}
    with pytest.raises(ValueError, match="unowned local"):
        assert_physical_mihomo_config(config, "physical", owned_loopback_ports={19101})


def test_real_asyncio_self_pipe_is_allowed_without_allowing_other_loopback(monkeypatch):
    import asyncio
    ports = OwnedLoopbackEndpoints()
    original_listen = socket.socket.listen
    original_connect = socket.socket.connect

    def listen(sock, *args):
        result = original_listen(sock, *args)
        ports.track_listener(sock)
        return result

    def connect(sock, address):
        address = physical_socket_address(sock, address, interface_index=15,
            resolver=socket.getaddrinfo, allowed_loopback_ports=ports)
        return original_connect(sock, address)

    monkeypatch.setattr(socket.socket, 'listen', listen)
    monkeypatch.setattr(socket.socket, 'connect', connect)
    async def work():
        await asyncio.to_thread(lambda: None)
        return 42
    assert asyncio.run(work()) == 42
    with socket.socket() as unrelated:
        with pytest.raises(PermissionError, match='non-owned'):
            unrelated.connect(('127.0.0.1', 9))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows IOCP UDP boundary")
def test_real_iocp_udp_is_guarded_before_native_send(monkeypatch):
    import asyncio
    from asyncio.windows_events import IocpProactor

    ports = OwnedLoopbackEndpoints()
    original = IocpProactor.sendto
    native_sends = []

    def sendto(proactor, sock, buf, flags=0, addr=None):
        target = physical_socket_address(sock, addr if addr is not None else sock.getpeername(),
            interface_index=15, resolver=socket.getaddrinfo, allowed_loopback_ports=ports)
        native_sends.append(target)
        return original(proactor, sock, buf, flags, addr=target if addr is not None else None)

    monkeypatch.setattr(IocpProactor, "sendto", sendto)
    async def work():
        loop = asyncio.get_running_loop()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver, \
                socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            receiver.bind(("127.0.0.1", 0))
            receiver.setblocking(False)
            sender.setblocking(False)
            address = receiver.getsockname()
            ports.add(address[1])
            await loop.sock_sendto(sender, b"owned", address)
            payload, _ = await asyncio.wait_for(loop.sock_recvfrom(receiver, 32), 1)
            assert payload == b"owned"
            with pytest.raises(PermissionError, match="non-owned"):
                await loop.sock_sendto(sender, b"blocked", ("127.0.0.1", 9))
            assert native_sends == [address]
    asyncio.run(work())


@pytest.mark.parametrize("failure", ["interface", "resolution", "binding"])
def test_binding_failure_never_falls_back_to_default_route(failure):
    sock = SimpleNamespace(family=socket.AF_INET, type=socket.SOCK_STREAM, setsockopt=Mock())
    resolver = Mock(return_value=[])
    if failure == "binding":
        sock.setsockopt.side_effect = OSError("interface no longer available")
    with pytest.raises((OSError, ValueError)):
        physical_socket_address(sock, ("audit.example" if failure == "resolution" else "192.0.2.1", 443),
                                interface_index=0 if failure == "interface" else 15, resolver=resolver)


def test_owned_dns_unspecified_family_keeps_both_families_without_system_lookup():
    query = Mock(side_effect=lambda name, kind: ['192.0.2.8'] if kind == 'A' else ['2001:db8::8'])
    numeric = Mock(side_effect=lambda host, port, family, kind, proto, flags:
                   [(family, kind, proto, '', (host, port))])
    values = owned_dns_getaddrinfo('audit.example', 443, socket.AF_UNSPEC, socket.SOCK_STREAM,
                                  query=query, numeric_getaddrinfo=numeric)
    assert {value[0] for value in values} == {socket.AF_INET, socket.AF_INET6}
    assert [call.args[1] for call in query.call_args_list] == ['A', 'AAAA']
    assert all(call.args[-1] & socket.AI_NUMERICHOST for call in numeric.call_args_list)


@pytest.mark.parametrize('family', [socket.AF_UNSPEC, socket.AF_INET, socket.AF_INET6])
def test_localhost_is_numeric_loopback_without_host_or_owned_dns(family):
    query = Mock(side_effect=AssertionError('localhost must not make DNS requests'))
    hosts = []
    def numeric(host, port, address_family, kind, proto, flags):
        assert host in {'127.0.0.1', '::1'}
        assert flags & socket.AI_NUMERICHOST
        hosts.append(host)
        return [(address_family, kind, proto, '', (host, port))]
    result = owned_dns_getaddrinfo('localhost', 1234, family, socket.SOCK_STREAM,
                                  query=query, numeric_getaddrinfo=numeric)
    assert result
    assert len(hosts) == (2 if family == socket.AF_UNSPEC else 1)
    query.assert_not_called()


@pytest.mark.parametrize('failed_kind', ['A', 'AAAA'])
def test_owned_dns_family_failure_does_not_discard_other_family(failed_kind):
    def query(name, kind):
        if kind == failed_kind:
            raise socket.gaierror(socket.EAI_AGAIN, 'owned DNS timeout')
        return ['192.0.2.8'] if kind == 'A' else ['2001:db8::8']
    numeric = Mock(side_effect=lambda host, port, family, kind, proto, flags:
                   [(family, kind, proto, '', (host, port))])
    result = owned_dns_getaddrinfo('audit.example', 443, socket.AF_UNSPEC, socket.SOCK_STREAM,
                                  query=query, numeric_getaddrinfo=numeric)
    assert len(result) == 1
    assert result[0][0] == (socket.AF_INET6 if failed_kind == 'A' else socket.AF_INET)
    assert numeric.call_args.args[-1] & socket.AI_NUMERICHOST


def test_localhost_with_numeric_only_flag_is_not_silently_resolved():
    resolver = Mock(side_effect=AssertionError('must reject without resolution'))
    with pytest.raises(socket.gaierror):
        owned_dns_getaddrinfo('localhost', 443, flags=socket.AI_NUMERICHOST,
                             query=resolver, numeric_getaddrinfo=resolver)
    resolver.assert_not_called()
