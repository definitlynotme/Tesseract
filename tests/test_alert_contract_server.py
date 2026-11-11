from __future__ import annotations

import json
import logging
import socketserver
import threading
import urllib.parse
import unittest
from collections import defaultdict, deque
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from alerts import AlertManager


class _ContractHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _ContractRequestHandler)
        self.requests: list[dict[str, object]] = []
        self.responses: dict[
            str,
            deque[tuple[int, str, bytes]],
        ] = defaultdict(deque)
        self._lock = threading.Lock()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def enqueue(
        self,
        path: str,
        *,
        status: int = 200,
        content_type: str = "application/json",
        body: bytes = b"{}",
    ) -> None:
        self.responses[path].append((status, content_type, body))

    def capture(self, request: dict[str, object]) -> tuple[int, str, bytes]:
        path = str(request["path"])
        with self._lock:
            self.requests.append(request)
            if self.responses[path]:
                return self.responses[path].popleft()
        return 200, "application/json", b"{}"


class _ContractRequestHandler(BaseHTTPRequestHandler):
    server: _ContractHTTPServer

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        parsed = urllib.parse.urlsplit(self.path)
        status, content_type, response = self.server.capture(
            {
                "path": parsed.path,
                "query": urllib.parse.parse_qs(parsed.query),
                "headers": dict(self.headers.items()),
                "body": body,
            }
        )
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class _SMTPContractServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _SMTPContractHandler)
        self.messages: list[bytes] = []
        self._lock = threading.Lock()


class _SMTPContractHandler(socketserver.StreamRequestHandler):
    server: _SMTPContractServer

    def handle(self) -> None:
        self.wfile.write(b"220 localhost Tesseract contract SMTP\r\n")
        in_data = False
        data = bytearray()
        while True:
            line = self.rfile.readline()
            if not line:
                return
            if in_data:
                if line == b".\r\n":
                    with self.server._lock:
                        self.server.messages.append(bytes(data))
                    data.clear()
                    in_data = False
                    self.wfile.write(b"250 queued\r\n")
                else:
                    data.extend(line[1:] if line.startswith(b"..") else line)
                continue

            command = line.decode("ascii", errors="replace").strip()
            verb = command.partition(" ")[0].upper()
            if verb in {"EHLO", "HELO"}:
                self.wfile.write(b"250-localhost\r\n250 SIZE 1048576\r\n")
            elif verb in {"MAIL", "RCPT", "RSET", "NOOP"}:
                self.wfile.write(b"250 ok\r\n")
            elif verb == "DATA":
                in_data = True
                self.wfile.write(b"354 end with <CRLF>.<CRLF>\r\n")
            elif verb == "QUIT":
                self.wfile.write(b"221 bye\r\n")
                return
            else:
                self.wfile.write(b"502 unsupported\r\n")


def _manager_for(channel_id: str, channel_type: str, config: dict[str, object]) -> AlertManager:
    effective = {"enabled": True, "retries": 0, **config}
    return AlertManager(
        {
            "channels": [
                {
                    "id": channel_id,
                    "type": channel_type,
                    "enabled": True,
                    "config": effective,
                }
            ]
        },
        logging.getLogger(f"test-alert-contract-{channel_type}"),
    )


class AlertHTTPContractServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = _ContractHTTPServer()
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            name="alert-contract-http",
            daemon=True,
        )
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _request(self, index: int = -1) -> dict[str, object]:
        return self.server.requests[index]

    def test_webhook_protocol_payloads_reach_real_local_server(self) -> None:
        cases = (
            ("slack", "/slack", lambda body: self.assertIn("text", body)),
            (
                "discord",
                "/discord",
                lambda body: self.assertEqual(
                    body["allowed_mentions"],
                    {"parse": []},
                ),
            ),
            (
                "teams",
                "/teams",
                lambda body: self.assertEqual(
                    body["attachments"][0]["contentType"],
                    "application/vnd.microsoft.card.adaptive",
                ),
            ),
            (
                "google_chat",
                "/google-chat",
                lambda body: self.assertIn("text", body),
            ),
            (
                "generic_webhook",
                "/generic",
                lambda body: self.assertEqual(body["title"], "Contract title"),
            ),
        )
        for channel_type, path, assert_body in cases:
            with self.subTest(channel_type=channel_type):
                before = len(self.server.requests)
                config: dict[str, object] = {
                    "url": f"{self.server.base_url}{path}",
                }
                if channel_type == "generic_webhook":
                    config["payload"] = {
                        "title": "{title}",
                        "message": "{message}",
                    }
                manager = _manager_for(
                    channel_type,
                    channel_type,
                    config,
                )
                ok, error = manager.test_channel(
                    channel_type,
                    "Contract title",
                    "Contract body",
                )

                self.assertTrue(ok, error)
                self.assertEqual(len(self.server.requests), before + 1)
                request = self._request()
                self.assertEqual(request["path"], path)
                self.assertEqual(
                    request["headers"]["Content-Type"],
                    "application/json",
                )
                assert_body(json.loads(request["body"].decode("utf-8")))
                if channel_type == "discord":
                    self.assertEqual(request["query"], {"wait": ["true"]})

    def test_teams_embedded_429_is_retried_before_success(self) -> None:
        self.server.enqueue(
            "/teams-retry",
            content_type="text/plain",
            body=b"Microsoft Teams endpoint returned HTTP error 429",
        )
        self.server.enqueue(
            "/teams-retry",
            content_type="text/plain",
            body=b"1",
        )
        manager = _manager_for(
            "teams-retry",
            "teams",
            {
                "url": f"{self.server.base_url}/teams-retry",
                "retries": 1,
            },
        )

        with patch("time.sleep", return_value=None):
            ok, error = manager.test_channel(
                "teams-retry",
                "Contract title",
                "Contract body",
            )

        self.assertTrue(ok, error)
        self.assertEqual(
            [item["path"] for item in self.server.requests],
            ["/teams-retry", "/teams-retry"],
        )

    def test_configurable_provider_contracts_reach_real_local_server(self) -> None:
        cases = (
            (
                "whatsapp",
                "/whatsapp/messages",
                {
                    "api_url": f"{self.server.base_url}/whatsapp/messages",
                    "access_token": "local-access-token",
                    "to": "+15550000001",
                },
                lambda request: self.assertEqual(
                    json.loads(request["body"].decode("utf-8"))[
                        "messaging_product"
                    ],
                    "whatsapp",
                ),
            ),
            (
                "ntfy",
                "/alerts_topic",
                {
                    "server": self.server.base_url,
                    "topic": "alerts_topic",
                    "token": "local-ntfy-token",
                    "title_template": "合同提醒",
                    "tags": ["warning", "proxy"],
                },
                lambda request: self.assertEqual(
                    request["headers"]["Authorization"],
                    "Bearer local-ntfy-token",
                ),
            ),
            (
                "gotify",
                "/gotify/message",
                {
                    "server": f"{self.server.base_url}/gotify",
                    "app_token": "local-gotify-token",
                    "priority": 7,
                },
                lambda request: self.assertEqual(
                    request["headers"]["X-Gotify-Key"],
                    "local-gotify-token",
                ),
            ),
            (
                "email_sendgrid",
                "/sendgrid/v3/mail/send",
                {
                    "api_url": f"{self.server.base_url}/sendgrid/v3/mail/send",
                    "api_key": "local-sendgrid-key",
                    "from_addr": "sender@example.test",
                    "to_addrs": ["receiver@example.test"],
                },
                lambda request: self.assertEqual(
                    request["headers"]["Authorization"],
                    "Bearer local-sendgrid-key",
                ),
            ),
            (
                "sms_twilio",
                "/twilio/Messages.json",
                {
                    "api_url": f"{self.server.base_url}/twilio/Messages.json",
                    "account_sid": "ACLOCAL",
                    "auth_token": "local-twilio-token",
                    "from_number": "+15550000001",
                    "to_numbers": ["+15550000002"],
                },
                lambda request: self.assertEqual(
                    urllib.parse.parse_qs(
                        request["body"].decode("utf-8")
                    )["To"],
                    ["+15550000002"],
                ),
            ),
        )
        for channel_type, path, config, assert_request in cases:
            with self.subTest(channel_type=channel_type):
                before = len(self.server.requests)
                manager = _manager_for(
                    channel_type,
                    channel_type,
                    config,
                )
                ok, error = manager.test_channel(
                    channel_type,
                    "Contract title",
                    "Contract body",
                )

                self.assertTrue(ok, error)
                self.assertEqual(len(self.server.requests), before + 1)
                request = self._request()
                self.assertEqual(request["path"], path)
                assert_request(request)
                if channel_type == "ntfy":
                    self.assertTrue(
                        request["headers"]["Title"].lower().startswith(
                            "=?utf-8?"
                        )
                    )


class AlertSMTPContractServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = _SMTPContractServer()
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            name="alert-contract-smtp",
            daemon=True,
        )
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_smtp_message_reaches_real_local_server(self) -> None:
        manager = _manager_for(
            "email_smtp",
            "email_smtp",
            {
                "host": "127.0.0.1",
                "port": self.server.server_address[1],
                "from_addr": "sender@example.test",
                "to_addrs": ["receiver@example.test"],
                "starttls": False,
            },
        )

        ok, error = manager.test_channel(
            "email_smtp",
            "Contract title",
            "Contract body",
        )

        self.assertTrue(ok, error)
        self.assertEqual(len(self.server.messages), 1)
        message = BytesParser(policy=policy.default).parsebytes(
            self.server.messages[0]
        )
        self.assertEqual(message["From"], "sender@example.test")
        self.assertEqual(message["To"], "receiver@example.test")
        self.assertIn("Contract title", str(message["Subject"]))
        self.assertIn("Contract body", message.get_body().get_content())
