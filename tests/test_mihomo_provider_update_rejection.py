"""PUT /providers/proxies/{name} answers 503 for any provider.Update() error (review F16, 11-02).

Only Mihomo's parser messages mean the provider file was refused and the
loaded members kept (``MihomoProviderUpdateRejected``); a read error stays a
plain ``MihomoAPIError``, which the verified writer retries as a transport
failure.  The message is kept as ``controller_message``; the exception text
is unchanged for every lane.  Bodies as the bundled Mihomo 1.19.27 returned
them (captured 11-02 on the _Lab harness).
"""

from __future__ import annotations

import io
import json
from typing import Any
from urllib.error import HTTPError

import pytest

import mihomo_api
from mihomo_api import (
    MihomoAPIClient,
    MihomoAPIError,
    MihomoDelayTransportFailed,
    MihomoProviderUpdateRejected,
)

PATH = "/providers/proxies/TIER3B-RUNTIME"
BASE = f"PUT {PATH} failed: HTTP 503 Service Unavailable"
FILE = r"C:\Users\user\AppData\Local\Tesseract\core-home\tier3b-runtime.yaml"
LOCKED = f"open {FILE}: The process cannot access the file because it is being used by another process."
PORT = '''proxy 1 error: cannot parse 'port' as int: strconv.ParseInt: parsing "abc": invalid syntax'''


def _body(message: Any) -> bytes:
    # Mihomo's render.JSON: compact JSON and a newline.
    return json.dumps({"message": message}, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"


@pytest.mark.parametrize(
    "message",
    [
        "proxy 1 error: unsupport proxy type: not-a-protocol",
        "proxy 1 error: missing type",
        PORT,
        "proxy 1 error: '' has unset fields: server",
        "proxy 12 override error: x",
        "yaml: line 1: did not find expected ',' or '}', convert v2ray subscribe error: format invalid",
        "file must have a `proxies` field",
        "file doesn't have any proxy",
        "doesn't match any proxy, please check your filter",
        "decrypt config error: x",
    ],
)
def test_a_parser_503_is_a_rejection_that_keeps_the_message(message: str) -> None:
    error = mihomo_api._http_failure("PUT", PATH, 503, "Service Unavailable", _body(message))
    assert type(error) is MihomoProviderUpdateRejected
    assert error.controller_message == message
    assert str(error) == BASE  # the text both lanes log is unchanged


@pytest.mark.parametrize(
    "raw,kept",
    [
        (_body(LOCKED), LOCKED),
        (_body(f"open {FILE}: The system cannot find the file specified."),
         f"open {FILE}: The system cannot find the file specified."),
        # Go asks Windows for English text, falling back to the system language.
        (_body(f"open {FILE}: 另一个程序正在使用此文件，进程无法访问。"),
         f"open {FILE}: 另一个程序正在使用此文件，进程无法访问。"),
        (_body(f"read {FILE}: The device is not ready."), f"read {FILE}: The device is not ready."),
        (_body("an unknown provider failure"), "an unknown provider failure"),
        (b"{}", None),
        (b"", None),
        (b"<html>503 Service Unavailable</html>", None),
        (_body(7), None),
    ],
)
def test_any_other_503_is_a_plain_failure_the_writer_retries(raw: bytes, kept: str | None) -> None:
    error = mihomo_api._http_failure("PUT", PATH, 503, "Service Unavailable", raw)
    assert type(error) is MihomoAPIError
    assert str(error) == BASE  # a read error's local path stays out of the text
    assert getattr(error, "controller_message", None) == kept


def test_only_the_provider_put_is_classified() -> None:
    parse = _body("proxy 0 error: missing type")
    for method, path, status in (
        ("GET", PATH, 503),
        ("PUT", PATH, 500),
        ("PUT", f"{PATH}/n1/healthcheck", 503),
        ("PUT", "/providers/rules/geo", 503),
    ):
        error = mihomo_api._http_failure(method, path, status, "x", parse)
        assert type(error) is MihomoAPIError, (method, path, status)
        assert getattr(error, "controller_message", None) is None
    # The delay classification is untouched.
    delay = mihomo_api._http_failure("GET", "/proxies/n1/delay", 503, "x", b"{}")
    assert type(delay) is MihomoDelayTransportFailed
    assert getattr(delay, "controller_message", None) is None


class _Opener:
    """urlopen stand-in that answers every request with one HTTP error."""

    def __init__(self, status: int, raw: bytes) -> None:
        self.status = status
        self.raw = raw
        self.requests: list[tuple[str, str]] = []

    def __call__(self, request: Any, timeout: float | None = None) -> Any:
        self.requests.append((request.get_method(), request.full_url))
        raise HTTPError(request.full_url, self.status, "Service Unavailable", {}, io.BytesIO(self.raw))


def test_update_proxy_provider_raises_the_classified_error() -> None:
    parse = "proxy 2 error: unsupport proxy type: x"
    opener = _Opener(503, _body(parse))
    client = MihomoAPIClient(base_url="http://127.0.0.1:9", opener=opener)
    with pytest.raises(MihomoProviderUpdateRejected) as rejected:
        client.update_proxy_provider("TIER3B-RUNTIME", timeout=1.5)
    assert rejected.value.controller_message == parse
    assert opener.requests == [("PUT", "http://127.0.0.1:9/providers/proxies/TIER3B-RUNTIME")]

    client = MihomoAPIClient(base_url="http://127.0.0.1:9", opener=_Opener(503, _body(LOCKED)))
    with pytest.raises(MihomoAPIError) as failed:
        client.update_proxy_provider("TIER3B-RUNTIME", timeout=1.5)
    assert type(failed.value) is MihomoAPIError
    assert failed.value.controller_message == LOCKED
