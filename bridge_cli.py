"""Developer-only frame client; transport is supplied by the caller."""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
from types import SimpleNamespace
from typing import Protocol

import bridge_protocol as protocol


class Transport(Protocol):
    def send(self, frame: dict) -> None: ...
    def receive(self) -> dict | None: ...


def parse_runtime_args(parser: argparse.ArgumentParser, argv: list[str]) -> argparse.Namespace:
    """Add the shell-owned gate option to the existing sidecar parser."""
    parser.add_argument("--legacy-gate", choices=("create", "inherited"), default="create")
    return parser.parse_args(argv)


class StdioTransport:
    """NDJSON frames on stdout and stdin (for local pipe pairs)."""

    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer
        self.disconnected = False
        self._incoming = queue.Queue(maxsize=64)
        self._reader = None

    def send(self, frame: dict) -> None:
        self.writer.write(protocol.encode_frame(frame))
        self.writer.flush()
        return "complete"

    def _read(self):
        try:
            while not self.disconnected:
                line = self.reader.readline(protocol.HARD_FRAME_LIMIT + 1)
                if not line:
                    break
                self._incoming.put(protocol.decode_line(line))
        finally:
            self.disconnected = True

    def receive(self, timeout=None) -> dict | None:
        if self._reader is None:
            self._reader = threading.Thread(target=self._read, daemon=True)
            self._reader.start()
        try:
            return self._incoming.get(timeout=timeout if timeout is not None else 5)
        except queue.Empty:
            return None

    def poll_write(self):
        return "complete"

    def close(self):
        self.disconnected = True


def handshake(transport: Transport, *, output=sys.stdout) -> str:
    hello = transport.receive()
    errors = protocol.validate_frame(hello)
    if errors or not isinstance(hello, dict) or hello.get("t") != "hello" or hello.get("proto") != protocol.PROTOCOL_VERSION:
        raise ValueError("invalid hello: " + ",".join(errors))
    print(json.dumps(hello, ensure_ascii=False), file=output)
    stamp = int(time.time() * 1000)
    ack = {"t": "hello_ack", "epoch": hello["epoch"], "proto": protocol.PROTOCOL_VERSION,
           "accepted": True, "produced_at_ms": stamp, "sent_at_ms": stamp}
    transport.send(ack)
    return hello["epoch"]


class LocalTransport:
    """A duplex in-memory endpoint; closing wakes its peer's read loop."""

    def __init__(self, incoming: queue.Queue, outgoing: queue.Queue):
        self.incoming = incoming
        self.outgoing = outgoing

    def send(self, frame: dict) -> None:
        self.outgoing.put(frame)

    def receive(self) -> dict | None:
        try:
            return self.incoming.get(timeout=5)
        except queue.Empty as exc:
            raise TimeoutError("local sidecar did not send a frame") from exc

    def close(self) -> None:
        self.outgoing.put(None)


def local_pair() -> tuple[LocalTransport, LocalTransport]:
    left, right = queue.Queue(), queue.Queue()
    return LocalTransport(left, right), LocalTransport(right, left)


def run_local(parsed: argparse.Namespace, output) -> int:
    """Run the real sidecar entry with isolated in-memory startup steps."""
    import bridge_instance_scope as instance_scope
    import bridge_main

    client, server = local_pair()
    scope = instance_scope.InstanceScope(
        "custom", "a" * 24, instance_scope.names_for_scope("custom", "a" * 24),
        False,
    )
    sidecar_result = []
    steps = bridge_main.Steps(
        scope=lambda: scope,
        acquire=lambda _scope: SimpleNamespace(status="acquired", lease=SimpleNamespace(close=lambda: None)),
        logging=lambda _scope: None,
        recovery=lambda _scope: bridge_main.unknown_recovery(),
        transport=lambda _args: server,
    )
    worker = threading.Thread(target=lambda: sidecar_result.append(bridge_main.main([], steps=steps)),
                              daemon=True)
    worker.start()
    try:
        epoch = handshake(client, output=output)
        kwargs = _request_kwargs(parsed)
        exchange(client, epoch, parsed.kind, parsed.id, output=output, **kwargs)
    finally:
        client.close()
        worker.join(timeout=5)
    if worker.is_alive() or sidecar_result != [0]:
        raise RuntimeError("local sidecar failed to exit")
    return 0


def _request_kwargs(parsed: argparse.Namespace) -> dict:
    return ({"topics": parsed.topics} if parsed.kind == "subscribe" else
            {"cmd": parsed.cmd, "args": json.loads(parsed.args)} if parsed.kind == "req" else
            {"op": parsed.op})


def exchange(transport: Transport, epoch: str, kind: str, request_id: int,
             *, topics: list[str] | None = None, cmd: str | None = None,
             args: dict | None = None, op: str | None = None,
             output=sys.stdout) -> dict | None:
    stamp = int(time.time() * 1000)
    frame = {"t": kind, "epoch": epoch, "produced_at_ms": stamp,
             "sent_at_ms": stamp, "id": request_id}
    if kind == "subscribe":
        frame["topics"] = topics
    elif kind == "req":
        frame.update({"cmd": cmd, "args": args or {}, "from": "shell"})
    elif kind == "cancel":
        frame["op"] = op
    else:
        raise ValueError("unsupported CLI frame")
    errors = protocol.validate_frame(frame)
    if errors:
        raise ValueError("invalid request: " + ",".join(errors))
    transport.send(frame)
    reply = transport.receive()
    if reply is not None:
        errors = protocol.validate_frame(reply)
        if errors or reply.get("epoch") != epoch:
            raise ValueError("invalid reply: " + ",".join(errors))
        if reply.get("id") != request_id:
            raise ValueError("reply id mismatch")
        if kind == "subscribe":
            errors = protocol.validate_control_reply(frame, reply)
            if errors:
                raise ValueError("invalid subscription reply: " + ",".join(errors))
        print(json.dumps(reply, ensure_ascii=False), file=output)
    return reply


def main(argv: list[str] | None = None, *, transport: Transport | None = None,
         output=sys.stdout) -> int:
    parser = argparse.ArgumentParser(prog="bridge-cli")
    parser.add_argument("--local", action="store_true", help="run an in-memory sidecar")
    parser.add_argument("--id", type=int, default=1)
    sub = parser.add_subparsers(dest="kind", required=True)
    sub.add_parser("subscribe").add_argument("topics", nargs="+")
    req = sub.add_parser("req")
    req.add_argument("cmd")
    req.add_argument("--args", default="{}")
    sub.add_parser("cancel").add_argument("op")
    parsed = parser.parse_args(argv)
    if parsed.local:
        if transport is not None:
            parser.error("--local cannot be combined with an injected transport")
        return run_local(parsed, output)
    if transport is None:
        transport = StdioTransport(sys.stdin.buffer, sys.stdout.buffer)
        output = sys.stderr
    epoch = handshake(transport, output=output)
    exchange(transport, epoch, parsed.kind, parsed.id, output=output,
             **_request_kwargs(parsed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
