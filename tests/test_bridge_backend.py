"""Protocol boundary tests against an executable fake app-server."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from codex_bridge.appserver import BridgeError, CodexBackend


FAKE_SERVER = r'''#!/usr/bin/env python3
import json, sys, time
def send(value):
    print(json.dumps(value), flush=True)
for line in sys.stdin:
    msg = json.loads(line)
    method, ident = msg.get("method"), msg.get("id")
    if method == "initialize":
        send({"id":ident,"result":{"userAgent":"fake-test"}})
    elif method == "account/read":
        send({"id":ident,"result":{"account":{"type":"chatgpt","email":"fixture@example.invalid","planType":"pro"},"requiresOpenaiAuth":True}})
    elif method == "model/list":
        send({"id":ident,"result":{"data":[{"id":"fixture-model","model":"fixture-model","displayName":"Fixture","isDefault":True,"inputModalities":["text","image"]}],"nextCursor":None}})
    elif method == "thread/start":
        send({"id":ident,"result":{"thread":{"id":"thread-test"}}})
    elif method == "turn/start":
        params = msg["params"]
        text = json.dumps(params)
        send({"id":ident,"result":{"turn":{"id":"turn-test","status":"inProgress","items":[]}}})
        if "TEST_STALL" in text:
            time.sleep(30)
        elif "TEST_TOOL" in text:
            send({"id":900,"method":"item/commandExecution/requestApproval","params":{"threadId":"thread-test","turnId":"turn-test","itemId":"tool-test","command":"forbidden"}})
            time.sleep(30)
        else:
            send({"method":"item/agentMessage/delta","params":{"threadId":"thread-test","turnId":"turn-test","itemId":"answer","delta":"PARTIAL_MUST_NOT_ESCAPE"}})
            if "TEST_FAIL" in text:
                turn={"id":"turn-test","status":"failed","error":{"message":"private model details"},"items":[]}
            elif "TEST_INTERRUPT" in text:
                turn={"id":"turn-test","status":"interrupted","items":[]}
            else:
                images=[i for i in params.get("input",[]) if i.get("type")=="image"]
                answer=json.dumps({"images":len(images),"action":"finish"})
                item={"id":"answer","type":"agentMessage","text":answer}
                send({"method":"item/completed","params":{"threadId":"thread-test","turnId":"turn-test","item":item}})
                turn={"id":"turn-test","status":"completed","items":[item]}
            send({"method":"turn/completed","params":{"threadId":"thread-test","turn":turn}})
'''


class BackendProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.environment = patch.dict(os.environ, {"CODEX_HOME": self.tmp.name})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.fake = Path(self.tmp.name) / "fake-codex"
        self.fake.write_text(FAKE_SERVER)
        self.fake.chmod(0o700)
        self.backend = CodexBackend(codex_binary=str(self.fake), timeout=1.5)

    def infer(self, text: str, images: list[str] | None = None) -> list[str | None]:
        return list(self.backend.stream(model="fixture-model", system="Return only an action.",
                                        text=text, images=images or [], cancel=threading.Event()))

    def test_model_discovery_uses_actual_catalog(self) -> None:
        self.assertEqual(self.backend.models()[0]["id"], "fixture-model")

    def test_completed_answer_and_image_survive_protocol(self) -> None:
        chunks = self.infer("TEST_OK", ["data:image/png;base64,aGVsbG8="])
        answer = "".join(c for c in chunks if c is not None)
        self.assertEqual(json.loads(answer), {"images": 1, "action": "finish"})
        self.assertNotIn("PARTIAL", answer)

    def test_failure_never_yields_partial_action(self) -> None:
        seen = []
        with self.assertRaises(BridgeError):
            for chunk in self.backend.stream(model="fixture-model", system="", text="TEST_FAIL",
                                             images=[], cancel=threading.Event()):
                if chunk:
                    seen.append(chunk)
        self.assertEqual(seen, [])

    def test_interrupted_turn_is_not_success(self) -> None:
        with self.assertRaises(BridgeError):
            self.infer("TEST_INTERRUPT")

    def test_deadline_reaps_stalled_child(self) -> None:
        started = time.monotonic()
        with self.assertRaises(BridgeError):
            self.infer("TEST_STALL")
        self.assertLess(time.monotonic() - started, 5)

    def test_tool_request_fails_closed(self) -> None:
        with self.assertRaises(BridgeError):
            self.infer("TEST_TOOL")

    def test_catalog_requires_explicit_image_capability(self) -> None:
        for modalities in (None, "image", ["text"], {"image": True}):
            with self.subTest(modalities=modalities):
                self.assertFalse(self.backend._usable({"model": "fixture", "inputModalities": modalities}))
        self.assertFalse(self.backend._usable({"model": "fixture"}))
        self.assertTrue(self.backend._usable({"model": "fixture", "inputModalities": ["text", "image"]}))

    def test_cancelled_catalog_does_not_spawn(self) -> None:
        cancel = threading.Event()
        cancel.set()
        with patch("codex_bridge.appserver.subprocess.Popen") as spawn:
            with self.assertRaises(BridgeError):
                self.backend.models(cancel=cancel)
            spawn.assert_not_called()

    def test_base_url_override_is_rejected_without_exposing_value(self) -> None:
        private = "https://private.example.invalid/secret"
        with patch.dict(os.environ, {"OPENAI_BASE_URL": private}):
            with self.assertRaises(BridgeError) as caught:
                self.backend.models()
        self.assertNotIn(private, str(caught.exception))

    def test_nested_openai_provider_override_is_rejected(self) -> None:
        private = "https://private.example.invalid/secret"
        config = Path(self.tmp.name) / "config.toml"
        config.write_text('[profiles.fixture.model_providers.openai]\nbase_url = "' + private + '"\n')
        with self.assertRaises(BridgeError) as caught:
            self.backend.models()
        self.assertNotIn(private, str(caught.exception))

    def test_pre_cancelled_request_does_not_generate(self) -> None:
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(BridgeError):
            list(self.backend.stream(model="fixture-model", system="", text="TEST_OK",
                                     images=[], cancel=cancel))


if __name__ == "__main__":
    unittest.main()
