"""Shared TLS client contexts are built once per factory and never reused across factories."""
from __future__ import annotations

import ssl
import urllib.request

import pytest

import client_tls
import overview_ip_check


@pytest.fixture(autouse=True)
def fresh_cache():
    client_tls.reset_shared_contexts()
    yield
    client_tls.reset_shared_contexts()


def test_contract_context_is_built_once_per_factory(monkeypatch):
    calls = []
    original = ssl.create_default_context

    def counting(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(ssl, "create_default_context", counting)
    first = client_tls.shared_contract_context()
    second = client_tls.shared_contract_context()
    assert first is second
    assert len(calls) == 1
    assert first.verify_mode == ssl.CERT_REQUIRED
    assert first.check_hostname is True


def test_context_is_rebuilt_when_the_factory_is_replaced(monkeypatch):
    first = client_tls.shared_contract_context()
    replacement = ssl.create_default_context()
    monkeypatch.setattr(ssl, "create_default_context", lambda *a, **k: replacement)
    assert client_tls.shared_contract_context() is replacement
    assert client_tls.shared_contract_context() is not first


def test_identity_context_matches_urllib_defaults(monkeypatch):
    calls = []
    original = ssl._create_default_https_context

    def counting(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(ssl, "_create_default_https_context", counting)
    context = client_tls.shared_identity_context()
    assert client_tls.shared_identity_context() is context
    assert len(calls) == 1
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    if context.post_handshake_auth is not None:
        assert context.post_handshake_auth is True


def test_identity_openers_share_one_tls_context(monkeypatch):
    calls = []
    original = ssl._create_default_https_context

    def counting(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(ssl, "_create_default_https_context", counting)
    openers = [overview_ip_check._build_opener("http://127.0.0.1:7890") for _ in range(5)]
    assert len(calls) == 1
    contexts = {
        id(handler._context)
        for opener in openers
        for handler in opener.handlers
        if isinstance(handler, urllib.request.HTTPSHandler)
    }
    assert len(contexts) == 1
