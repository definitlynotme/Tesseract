from __future__ import annotations

import os
import socket
import subprocess
import sys

import pytest

from runtime_listener_preflight import (
    RuntimeBoundEndpoint,
    RuntimeListenerPreflightState,
    RuntimeListenerSpec,
    preflight_runtime_listeners,
    windows_bound_listener_snapshot,
)


class _FakeSocket:
    def __init__(self, failures: list[OSError | None]) -> None:
        self._failures = failures

    def setsockopt(self, *_args: object) -> None:
        return None

    def bind(self, _target: tuple[object, ...]) -> None:
        failure = self._failures.pop(0) if self._failures else None
        if failure is not None:
            raise failure

    def listen(self, _backlog: int) -> None:
        return None

    def close(self) -> None:
        return None


def _factory(failures: list[OSError | None]):
    def create(_family: int, _socket_type: int) -> _FakeSocket:
        return _FakeSocket(failures)

    return create


def _permission_error() -> PermissionError:
    return PermissionError(13, "socket access denied", None, 10013)


def test_background_process_presence_is_not_a_preflight_input() -> None:
    background = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = int(reservation.getsockname()[1])
        result = preflight_runtime_listeners(
            (RuntimeListenerSpec("mixed", "tcp", "127.0.0.1", port),)
        )
        assert background.poll() is None
        assert result.state is RuntimeListenerPreflightState.AVAILABLE
        assert result.conflicts == ()
    finally:
        background.terminate()
        background.wait(timeout=5)


def test_access_error_without_active_endpoint_is_indeterminate_not_occupied() -> None:
    failures: list[OSError | None] = [_permission_error(), _permission_error()]
    result = preflight_runtime_listeners(
        (RuntimeListenerSpec("mixed", "tcp", "127.0.0.1", 17890),),
        socket_factory=_factory(failures),
        endpoint_snapshot_provider=lambda: frozenset(),
    )
    assert result.state is RuntimeListenerPreflightState.INDETERMINATE
    assert result.conflicts == ()
    assert result.endpoint_snapshot_available
    assert result.failures[0].windows_error == 10013


def test_address_unavailable_without_active_endpoint_still_defers_to_core() -> None:
    unavailable = OSError(99, "address not available")
    unavailable.winerror = 10049
    failures: list[OSError | None] = [unavailable, unavailable]
    result = preflight_runtime_listeners(
        (RuntimeListenerSpec("mixed", "tcp", "192.0.2.10", 17898),),
        socket_factory=_factory(failures),
        endpoint_snapshot_provider=lambda: frozenset(),
    )
    assert result.state is RuntimeListenerPreflightState.INDETERMINATE
    assert result.conflicts == ()
    assert result.failures[0].windows_error == 10049


def test_access_error_with_active_wildcard_endpoint_is_occupied() -> None:
    failures: list[OSError | None] = [_permission_error(), _permission_error()]
    result = preflight_runtime_listeners(
        (RuntimeListenerSpec("dns", "udp", "127.0.0.1", 11053),),
        socket_factory=_factory(failures),
        endpoint_snapshot_provider=lambda: frozenset(
            {
                RuntimeBoundEndpoint(
                    transport="udp",
                    family=socket.AF_INET,
                    host="0.0.0.0",
                    port=11053,
                )
            }
        ),
    )
    assert result.state is RuntimeListenerPreflightState.OCCUPIED
    assert result.conflicts == (
        RuntimeListenerSpec("dns", "udp", "127.0.0.1", 11053),
    )


def test_transient_bind_failure_is_rechecked_once_and_allowed() -> None:
    failures: list[OSError | None] = [_permission_error(), None]
    snapshots: list[bool] = []
    result = preflight_runtime_listeners(
        (RuntimeListenerSpec("mixed", "tcp", "127.0.0.1", 17891),),
        socket_factory=_factory(failures),
        endpoint_snapshot_provider=lambda: snapshots.append(True) or frozenset(),
    )
    assert result.state is RuntimeListenerPreflightState.AVAILABLE
    assert result.attempts == 2
    assert len(result.transient_failures) == 1
    assert snapshots == []


def test_retry_rebinds_only_the_failed_endpoint_after_releasing_reservations() -> None:
    first_port = 17896
    second_port = 17897
    calls: list[tuple[str, int]] = []
    attempts = {first_port: 0, second_port: 0}

    class RecordingSocket:
        def setsockopt(self, *_args: object) -> None:
            return None

        def bind(self, target: tuple[object, ...]) -> None:
            host, raw_port = target[:2]
            port = int(raw_port)
            calls.append((str(host), port))
            attempts[port] += 1
            if port == second_port and attempts[port] == 1:
                raise _permission_error()

        def listen(self, _backlog: int) -> None:
            return None

        def close(self) -> None:
            return None

    result = preflight_runtime_listeners(
        (
            RuntimeListenerSpec("mixed", "tcp", "127.0.0.1", first_port),
            RuntimeListenerSpec("controller", "tcp", "127.0.0.1", second_port),
        ),
        socket_factory=lambda *_args: RecordingSocket(),
        endpoint_snapshot_provider=lambda: pytest.fail(
            "a successful retry must not query endpoint ownership"
        ),
    )

    assert result.state is RuntimeListenerPreflightState.AVAILABLE
    assert attempts == {first_port: 1, second_port: 2}
    assert calls == [
        ("127.0.0.1", first_port),
        ("127.0.0.1", second_port),
        ("127.0.0.1", second_port),
    ]


def test_resolution_failure_never_claims_an_active_proxy() -> None:
    def fail_resolution(*_args: object, **_kwargs: object):
        raise OSError(11001, "host not found")

    result = preflight_runtime_listeners(
        (RuntimeListenerSpec("mixed", "tcp", "invalid.invalid", 17892),),
        resolver=fail_resolution,
        endpoint_snapshot_provider=lambda: pytest.fail(
            "resolution errors must not query endpoint ownership"
        ),
    )
    assert result.state is RuntimeListenerPreflightState.INDETERMINATE
    assert result.conflicts == ()
    assert result.failures[0].stage == "resolve"


def test_overlapping_listener_configuration_is_not_external_occupancy() -> None:
    result = preflight_runtime_listeners(
        (
            RuntimeListenerSpec("mixed", "tcp", "127.0.0.1", 17893),
            RuntimeListenerSpec("controller", "tcp", "0.0.0.0", 17893),
        ),
        socket_factory=lambda *_args: pytest.fail(
            "configuration conflicts must be rejected before binding"
        ),
    )
    assert result.state is RuntimeListenerPreflightState.CONFIGURATION_ERROR
    assert result.conflicts == ()
    assert result.failures[0].stage == "configuration"


def test_identical_listener_configuration_is_a_configuration_error() -> None:
    spec = RuntimeListenerSpec("mixed", "tcp", "127.0.0.1", 17894)
    result = preflight_runtime_listeners(
        (spec, spec),
        socket_factory=lambda *_args: pytest.fail(
            "duplicate configuration must be rejected before binding"
        ),
    )
    assert result.state is RuntimeListenerPreflightState.CONFIGURATION_ERROR
    assert result.conflicts == ()
    assert result.attempts == 0


def test_invalid_port_is_a_configuration_error() -> None:
    result = preflight_runtime_listeners(
        (RuntimeListenerSpec("mixed", "tcp", "127.0.0.1", 0),),
        socket_factory=lambda *_args: pytest.fail(
            "invalid configuration must be rejected before binding"
        ),
    )
    assert result.state is RuntimeListenerPreflightState.CONFIGURATION_ERROR
    assert result.conflicts == ()


def test_other_address_family_wildcard_is_not_occupancy_proof() -> None:
    failures: list[OSError | None] = [_permission_error(), _permission_error()]
    result = preflight_runtime_listeners(
        (RuntimeListenerSpec("mixed", "tcp", "127.0.0.1", 17895),),
        socket_factory=_factory(failures),
        endpoint_snapshot_provider=lambda: frozenset(
            {
                RuntimeBoundEndpoint(
                    transport="tcp",
                    family=socket.AF_INET6,
                    host="::",
                    port=17895,
                )
            }
        ),
    )
    assert result.state is RuntimeListenerPreflightState.INDETERMINATE
    assert result.conflicts == ()


@pytest.mark.skipif(os.name != "nt", reason="Windows IP Helper API only")
def test_windows_snapshot_reports_real_tcp_and_udp_bindings() -> None:
    tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        tcp.bind(("127.0.0.1", 0))
        port = int(tcp.getsockname()[1])
        tcp.listen(1)
        udp.bind(("127.0.0.1", port))

        snapshot = windows_bound_listener_snapshot()

        assert snapshot is not None
        assert RuntimeBoundEndpoint("tcp", socket.AF_INET, "127.0.0.1", port) in snapshot
        assert RuntimeBoundEndpoint("udp", socket.AF_INET, "127.0.0.1", port) in snapshot
    finally:
        tcp.close()
        udp.close()
