"""Loopback HTTP integration coverage using a deterministic in-memory backend."""

from __future__ import annotations

import base64
import contextlib
import http.client
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from typing import Any, Iterator

from codex_bridge.appserver import BridgeError
from codex_bridge.server import BridgeServer, MAX_BODY, MAX_IMAGE, read_token

LOGIN = "tester@example.com"
TOKEN = "a-private-local-bridge-token-of-sufficient-length"
IMAGE = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRz8AAAAASUVORK5CYII="


def payload(*, stream: bool = False) -> dict[str, Any]:
    """Build the same system/user vision shape used by LLMClient.swift."""
    return {"model": "test-model", "messages": [
        {"role": "system", "content": "Return a phone action."},
        {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": IMAGE}},
            {"type": "text", "text": "Look at this screenshot."},
        ]},
    ], "stream": stream, "temperature": 0, "max_tokens": 600}


class FakeBackend:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.block = False
        self.fail = False
        self.cancelled = threading.Event()
        self.catalog_block = False
        self.catalog_calls = 0
        self.oversize = False

    def models(self, *, cancel: threading.Event | None = None) -> list[dict[str, Any]]:
        self.catalog_calls += 1
        if self.catalog_block:
            self.started.set()
            while not self.release.wait(0.02):
                if cancel is not None and cancel.is_set():
                    self.cancelled.set()
                    return []
        return [{"id": "test-model", "object": "model", "owned_by": "test", "is_default": True}]

    def stream(self, **kwargs: Any) -> Iterator[str | None]:
        self.calls.append(kwargs)
        self.started.set()
        try:
            while self.block and not self.release.wait(0.02):
                if kwargs["cancel"].is_set():
                    self.cancelled.set()
                    return
                yield None
            yield '{"action": "tap", "x": 1}'
            if self.oversize:
                yield "x" * (1024 * 1024 + 1)
            if self.fail:
                raise BridgeError(503, "model unavailable")
        finally:
            if kwargs["cancel"].is_set():
                self.cancelled.set()


@contextlib.contextmanager
def serving(backend: FakeBackend, **kwargs: Any) -> Iterator[BridgeServer]:
    server = BridgeServer(0, backend, **kwargs)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        backend.release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class HTTPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = FakeBackend()
        self.context = serving(self.backend, tailscale_login=LOGIN, token=TOKEN)
        self.server = self.context.__enter__()
        self.addCleanup(self.context.__exit__, None, None, None)

    def request(self, method: str, path: str, body: Any = None, *, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
        client = http.client.HTTPConnection(*self.server.server_address, timeout=10)
        data = json.dumps(body).encode() if body is not None else None
        request_headers = {"Content-Type": "application/json", **(headers or {})}
        try:
            client.request(method, path, data, request_headers)
            response = client.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            client.close()

    def post(self, body: Any, **kwargs: Any) -> tuple[int, dict[str, str], bytes]:
        kwargs.setdefault("headers", {"Tailscale-User-Login": LOGIN})
        return self.request("POST", "/v1/chat/completions", body, **kwargs)

    def test_loopback_and_auth_required(self) -> None:
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        with self.assertRaises(ValueError):
            BridgeServer(0, self.backend)
        status, _, body = self.request("GET", "/health")
        self.assertEqual((status, json.loads(body)), (200, {"status": "ok"}))
        self.assertEqual(self.request("GET", "/v1/models")[0], 401)
        self.assertEqual(self.post(payload(), headers={})[0], 401)
        self.assertEqual(self.backend.calls, [])

    def test_identity_and_bearer_are_alternative_auth_modes(self) -> None:
        for headers in ({"Tailscale-User-Login": LOGIN}, {"Authorization": "Bearer " + TOKEN}):
            with self.subTest(headers=list(headers)):
                status, _, body = self.request("GET", "/v1/models", headers=headers)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body)["data"][0]["id"], "test-model")
        for headers in ({"Tailscale-User-Login": LOGIN + ".evil"}, {"Authorization": "Bearer wrong"}, {"Authorization": "Bearer \xe9"}):
            self.assertEqual(self.request("GET", "/v1/models", headers=headers)[0], 401)

    def test_vision_nonstream_and_stream_shapes(self) -> None:
        status, _, body = self.post(payload())
        self.assertEqual(status, 200)
        answer = json.loads(body)
        self.assertEqual(answer["choices"][0]["message"]["content"], '{"action": "tap", "x": 1}')
        self.assertEqual(self.backend.calls[0]["images"], [IMAGE])
        status, headers, body = self.post(payload(stream=True))
        self.assertEqual((status, headers["Content-Type"]), (200, "text/event-stream"))
        lines = [line[6:] for line in body.decode().splitlines() if line.startswith("data: ")]
        self.assertEqual(lines[-1], "[DONE]")
        self.assertEqual(json.loads(lines[0])["choices"][0]["delta"]["content"], '{"action": "tap", "x": 1}')

    def test_twelve_demo_frames_accepted_thirteen_rejected(self) -> None:
        body = payload()
        image, text = body["messages"][1]["content"]
        body["messages"][1]["content"] = [image] * 12 + [text]
        self.assertEqual(self.post(body)[0], 200)
        self.assertEqual(len(self.backend.calls[-1]["images"]), 12)
        body["messages"][1]["content"] = [image] * 13 + [text]
        self.assertEqual(self.post(body)[0], 400)
        self.assertEqual(len(self.backend.calls), 1)

    def test_origins_queries_and_unknown_paths_are_rejected(self) -> None:
        auth = {"Tailscale-User-Login": LOGIN, "Origin": "http://untrusted.example"}
        self.assertEqual(self.post(payload(), headers=auth)[0], 403)
        self.assertEqual(self.request("GET", "/v1/models", headers=auth)[0], 403)
        for path in ("/v1/models?x=1", "/v1/chat/completions?x=1", "/missing"):
            self.assertEqual(self.request("GET", path)[0], 404)
        self.assertEqual(self.request("POST", "/missing", payload())[0], 404)
        self.assertEqual(self.backend.calls, [])

    def test_rejects_bad_body_framing_and_media_type(self) -> None:
        auth = {"Tailscale-User-Login": LOGIN}
        for headers, expected in (
            ({**auth, "Content-Length": str(MAX_BODY + 1)}, 413),
            ({**auth, "Content-Length": "-1"}, 400),
            ({**auth, "Transfer-Encoding": "chunked"}, 400),
            ({**auth, "Content-Type": "text/plain"}, 415),
        ):
            with self.subTest(headers=headers):
                self.assertEqual(self.post(payload(), headers=headers)[0], expected)
        self.assertEqual(self.backend.calls, [])

    def test_duplicate_length_and_invalid_json(self) -> None:
        for extra_headers, body in (([("Content-Length", "2"), ("Content-Length", "2")], b"{}"), ([("Content-Length", "1")], b"{"), ([("Content-Length", "21")], b'{"model":1,"model":2}')):
            client = http.client.HTTPConnection(*self.server.server_address, timeout=3)
            self.addCleanup(client.close)
            client.putrequest("POST", "/v1/chat/completions")
            client.putheader("Tailscale-User-Login", LOGIN)
            client.putheader("Content-Type", "application/json")
            for key, value in extra_headers:
                client.putheader(key, value)
            client.endheaders(body)
            response = client.getresponse()
            self.assertEqual(response.status, 400)
            response.read()

    def test_payload_rejections_never_reach_backend(self) -> None:
        cases: list[dict[str, Any]] = []
        body = payload(); body["messages"].append({"role": "assistant", "content": "old turn"}); cases.append(body)
        body = payload(); body["messages"][0]["role"] = "developer"; cases.append(body)
        body = payload(); body["tools"] = []; cases.append(body)
        body = payload(); body["messages"][1]["content"][0]["image_url"]["url"] = "http://169.254.169.254/latest/meta-data"; cases.append(body)
        body = payload(); body["messages"][1]["content"][0]["image_url"]["url"] = "data:image/png;base64,!!!!"; cases.append(body)
        body = payload(); body["messages"][1]["content"][0]["image_url"]["url"] = "data:image/png;base64,YWJj"; cases.append(body)
        body = payload(); body["messages"][1]["content"] *= 7; cases.append(body)
        body = payload(); body["messages"][0]["content"] = "x" * 32769; cases.append(body)
        body = payload(); body["max_tokens"] = True; cases.append(body)
        body = payload(); body["temperature"] = 3; cases.append(body)
        body = payload(); body["stream"] = "true"; cases.append(body)
        body = payload(); body["messages"][1]["content"][0]["image_url"]["url"] = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * (MAX_IMAGE - 7)).decode(); cases.append(body)
        for index, body in enumerate(cases):
            with self.subTest(index=index):
                self.assertEqual(self.post(body)[0], 400)
        self.assertEqual(self.backend.calls, [])

    def test_busy_request_returns_429_without_queueing(self) -> None:
        self.backend.block = True
        result: list[Any] = []
        first = threading.Thread(target=lambda: result.append(self.post(payload())), daemon=True)
        first.start()
        self.assertTrue(self.backend.started.wait(2))
        status, headers, _ = self.post(payload())
        self.assertEqual((status, headers["Retry-After"]), (429, "1"))
        self.assertEqual(self.request("GET", "/v1/models", headers={"Tailscale-User-Login": LOGIN})[0], 429)
        self.assertEqual(len(self.backend.calls), 1)
        self.backend.release.set()
        first.join(timeout=2)
        self.assertEqual(result[0][0], 200)

    def test_backend_error_never_returns_partial_actions(self) -> None:
        self.backend.fail = True
        status, _, body = self.post(payload())
        self.assertEqual(status, 503)
        self.assertNotIn(b"tap", body)
        status, _, body = self.post(payload(stream=True))
        self.assertEqual(status, 200)
        self.assertIn(b"model unavailable", body)
        self.assertIn(b"[DONE]", body)
        self.assertNotIn(b"tap", body)

    def test_catalog_requests_share_the_inference_slot(self) -> None:
        self.backend.catalog_block = True
        result: list[Any] = []
        auth = {"Tailscale-User-Login": LOGIN}
        first = threading.Thread(target=lambda: result.append(self.request("GET", "/v1/models", headers=auth)), daemon=True)
        first.start()
        self.assertTrue(self.backend.started.wait(2))
        self.assertEqual(self.request("GET", "/v1/models", headers=auth)[0], 429)
        self.assertEqual(self.post(payload())[0], 429)
        self.assertEqual(self.backend.catalog_calls, 1)
        self.backend.release.set()
        first.join(timeout=2)
        self.assertEqual(result[0][0], 200)

    def test_post_header_failure_stays_a_single_sse_response(self) -> None:
        self.backend.oversize = True
        status, _, body = self.post(payload(stream=True))
        self.assertEqual(status, 200)
        self.assertIn(b"output limit", body)
        self.assertNotIn(b"HTTP/1.1", body)
        self.assertNotIn(b"tap", body)
        self.assertTrue(body.endswith(b"data: [DONE]\n\n"))

    def test_client_disconnect_cancels_backend(self) -> None:
        self.backend.block = True
        client = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        client.request("POST", "/v1/chat/completions", json.dumps(payload(stream=True)), {"Content-Type": "application/json", "Tailscale-User-Login": LOGIN})
        response = client.getresponse()
        self.assertEqual(response.status, 200)
        self.assertTrue(self.backend.started.wait(2))
        assert response.fp is not None
        client.sock.shutdown(socket.SHUT_RDWR) if client.sock is not None else response.fp.raw._sock.shutdown(socket.SHUT_RDWR)
        client.close()
        response.close()
        self.assertTrue(self.backend.cancelled.wait(4))

    def test_nonstream_disconnect_also_cancels_backend(self) -> None:
        self.backend.block = True
        client = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        client.request("POST", "/v1/chat/completions", json.dumps(payload()), {"Content-Type": "application/json", "Tailscale-User-Login": LOGIN})
        self.assertTrue(self.backend.started.wait(2))
        assert client.sock is not None
        client.sock.shutdown(socket.SHUT_RDWR)
        client.close()
        self.assertTrue(self.backend.cancelled.wait(4))

    def test_catalog_disconnect_cancels_backend_and_frees_slot(self) -> None:
        self.backend.catalog_block = True
        client = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        client.request("GET", "/v1/models", headers={"Tailscale-User-Login": LOGIN})
        self.assertTrue(self.backend.started.wait(2))
        assert client.sock is not None
        client.sock.shutdown(socket.SHUT_RDWR)
        client.close()
        self.assertTrue(self.backend.cancelled.wait(2))
        self.assertTrue(self.server._idle.wait(2))
        self.assertEqual(self.post(payload())[0], 200)

    def test_closing_rejects_new_backend_jobs(self) -> None:
        self.server._cancel_jobs()
        self.assertEqual(self.post(payload())[0], 503)
        self.assertEqual(self.request("GET", "/v1/models", headers={"Tailscale-User-Login": LOGIN})[0], 503)
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(self.backend.catalog_calls, 0)


class TokenTests(unittest.TestCase):
    def test_private_token_file_and_rejections(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            token_file = Path(temporary) / "token"
            token_file.write_text(TOKEN + "\n")
            token_file.chmod(0o600)
            self.assertEqual(read_token(token_file), TOKEN)
            token_file.chmod(0o644)
            with self.assertRaises(ValueError):
                read_token(token_file)
            token_file.chmod(0o600)
            alias = Path(temporary) / "alias"
            alias.symlink_to(token_file)
            with self.assertRaises(OSError):
                read_token(alias)
            token_file.write_text("short")
            with self.assertRaises(ValueError):
                read_token(token_file)


if __name__ == "__main__":
    unittest.main()
