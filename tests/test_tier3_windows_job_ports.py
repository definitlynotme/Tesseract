"""``ports_released`` must never let a probe error escape into its caller.

The supervisor reactor thread calls it while finalizing a retired generation;
an escaping exception there kills the reactor and silently stops all
validation (observed 2026-09-22 under a socket policy that refuses loopback
connects).  A probe that cannot connect proves nothing is accepting for us,
which is what "released" means for that port.
"""
from __future__ import annotations

import socket

from tier3_windows_job import ports_released


def test_ports_released_reports_a_listening_port_as_still_bound() -> None:
    listener = socket.socket()
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = int(listener.getsockname()[1])
        assert ports_released((port,)) is False
    finally:
        listener.close()
    assert ports_released((port,)) is True


def test_ports_released_swallows_probe_errors_instead_of_raising(monkeypatch) -> None:
    def refuse(self, address):  # noqa: ANN001
        raise PermissionError("socket policy refuses loopback")

    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    assert ports_released((65000, 65001)) is True


def test_ports_released_skips_non_positive_ports() -> None:
    assert ports_released((0, -1)) is True
