"""Service close must wait until in-flight Codex cleanup finishes."""
from __future__ import annotations

import http.client
import json
import threading
import unittest

from codex_bridge.server import BridgeServer


class CleanupBackend:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.cleaning = threading.Event()
        self.release = threading.Event()
        self.cleaned = threading.Event()

    def work(self, cancel: threading.Event | None) -> None:
        self.entered.set()
        if cancel is None:
            self.release.wait(2)
        else:
            cancel.wait(2)
        self.cleaning.set()
        self.release.wait(2)
        self.cleaned.set()

    def models(self, *, cancel: threading.Event | None = None) -> list[dict]:
        self.work(cancel)
        return []

    def stream(self, *, cancel: threading.Event, **kwargs):
        self.work(cancel)
        yield '{"action":"finish"}'


class ShutdownTests(unittest.TestCase):
    def exercise(self, method: str) -> None:
        backend = CleanupBackend()
        server = BridgeServer(0, backend, tailscale_login="fixture")
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        finished = threading.Event()

        def client() -> None:
            conn = http.client.HTTPConnection(*server.server_address, timeout=4)
            try:
                if method == "GET":
                    conn.request(method, "/v1/models", headers={"Tailscale-User-Login": "fixture"})
                else:
                    body = {"model":"fixture-model","messages":[{"role":"system","content":""},
                        {"role":"user","content":[{"type":"text","text":"test"}]}],"stream":True}
                    conn.request(method, "/v1/chat/completions", json.dumps(body),
                                 {"Tailscale-User-Login":"fixture","Content-Type":"application/json"})
                conn.getresponse().read()
            except (OSError, http.client.HTTPException):
                pass
            finally:
                conn.close()

        requesting = threading.Thread(target=client, daemon=True)
        requesting.start()
        try:
            self.assertTrue(backend.entered.wait(2))
            server.shutdown()
            def close() -> None:
                server.server_close()
                finished.set()
            closing = threading.Thread(target=close, daemon=True)
            closing.start()
            self.assertTrue(backend.cleaning.wait(1), "shutdown did not cancel active backend")
            self.assertFalse(finished.wait(0.05), "service returned before backend cleanup")
            backend.release.set()
            self.assertTrue(finished.wait(2))
            self.assertTrue(backend.cleaned.is_set())
            closing.join(2)
        finally:
            backend.release.set()
            server.shutdown()
            server.server_close()
            serving.join(2)
            requesting.join(3)

    def test_inference_cleanup_is_joined_before_service_close(self) -> None:
        self.exercise("POST")

    def test_catalog_cleanup_is_joined_before_service_close(self) -> None:
        self.exercise("GET")


if __name__ == "__main__":
    unittest.main()
