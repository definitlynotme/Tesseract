"""Process-wide verified TLS client contexts for exit checks.

Building a default context loads the Windows trust store: ~30 ms idle and
several hundred milliseconds under startup load, which the overview exit
contracts and identity lookups used to pay inside their own timeouts. A
client ``SSLContext`` is safe to share between threads for handshakes as long
as it is never mutated after publication.

Each cache slot holds the factory it was built with (a strong reference,
compared with ``is``). Replacing the factory - as tests do with temporary CA
factories or mocks - builds a fresh context instead of reusing one built by a
different factory.
"""
from __future__ import annotations

import http.client
import ssl
import threading
from typing import Any

_LOCK = threading.Lock()
_CONTRACT_SLOT: list[Any] = [None, None]
_IDENTITY_SLOT: list[Any] = [None, None]


def _cached(slot: list[Any], factory: Any, build: Any) -> Any:
    with _LOCK:
        if slot[0] is factory and slot[1] is not None:
            return slot[1]
        context = build()
        slot[0], slot[1] = factory, context
        return context


def shared_contract_context() -> ssl.SSLContext:
    """Return ``ssl.create_default_context()``, built once per factory."""

    factory = ssl.create_default_context
    return _cached(_CONTRACT_SLOT, factory, factory)


def shared_identity_context() -> ssl.SSLContext:
    """Return urllib's default HTTPS context, built once per factory.

    It matches ``urllib.request.HTTPSHandler()``'s own default: ALPN
    ``http/1.1`` and post-handshake auth where supported.
    """

    factory = ssl._create_default_https_context
    return _cached(
        _IDENTITY_SLOT,
        factory,
        lambda: http.client._create_https_context(http.client.HTTPSConnection._http_vsn),
    )


def reset_shared_contexts() -> None:
    """Drop cached contexts (trust-store refresh or tests)."""

    with _LOCK:
        _CONTRACT_SLOT[0] = _CONTRACT_SLOT[1] = None
        _IDENTITY_SLOT[0] = _IDENTITY_SLOT[1] = None
