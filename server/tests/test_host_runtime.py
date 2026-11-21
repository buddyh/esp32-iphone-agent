"""Capture ownership, local API boundaries and cancellation regressions."""

import asyncio
import importlib
import os
import runpy
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hidagent.screen import Screen


class CaptureTests(unittest.TestCase):
    def test_missing_binary_and_plugin_fail_before_receiver_spawn(self):
        screen = Screen()
        with (
            patch.dict(os.environ, {"UXPLAY_BIN": "/missing/uxplay"}),
            self.assertRaisesRegex(RuntimeError, "absolute path"),
        ):
            screen.start()
        with (
            patch.object(Screen, "_executable", return_value=sys.executable),
            patch("hidagent.screen.subprocess.run", return_value=SimpleNamespace(returncode=1)),
            patch("hidagent.screen.subprocess.Popen") as spawn,
            self.assertRaisesRegex(RuntimeError, "h264parse"),
        ):
            screen.start()
        spawn.assert_not_called()

    @unittest.skipIf(os.name == "nt", "POSIX receiver: shebang script, SIGTERM and waitpid")
    def test_owned_receiver_is_killed_reaped_and_thread_joined(self):
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "receiver"
            executable.write_text(
                f"#!{sys.executable}\nimport signal,time\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\nwhile True: time.sleep(.1)\n"
            )
            executable.chmod(0o700)
            screen = Screen()
            with (
                patch.dict(os.environ, {"UXPLAY_BIN": str(executable), "UXPLAY_ARGS": "", "UXPLAY_PORT": ""}),
                patch.object(Screen, "_executable", side_effect=[str(executable), sys.executable]),
                patch("hidagent.screen.subprocess.run", return_value=SimpleNamespace(returncode=0)),
            ):
                screen.start()
            process, thread = screen._proc, screen._thread
            try:
                started = time.monotonic()
                screen.stop()
                self.assertLess(time.monotonic() - started, 3)
                self.assertIsNotNone(process.poll())
                self.assertFalse(thread.is_alive())
                self.assertIsNone(screen._proc)
                with self.assertRaises(ChildProcessError):
                    os.waitpid(process.pid, os.WNOHANG)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()

    @unittest.skipIf(os.name == "nt", "Windows cannot count receiver connections, so it uses frame age")
    def test_mac_connection_proof_preserves_still_frame_and_rejects_stale(self):
        screen = Screen()
        screen._proc = SimpleNamespace(pid=12345, poll=lambda: None)
        screen.frame = b"jpeg"
        screen.updated = time.time() - 1000
        screen._updated_monotonic = time.monotonic() - 1000
        with (
            patch("hidagent.screen.sys.platform", "darwin"),
            patch(
                "hidagent.screen.subprocess.run",
                return_value=SimpleNamespace(returncode=0, stdout="p12345\nf12\n", stderr=""),
            ) as inspect,
        ):
            self.assertTrue(screen.live)
            self.assertGreater(screen.metadata["frame_age_seconds"], 999)
            self.assertTrue(screen.metadata["connection_verified"])
            self.assertEqual(inspect.call_count, 1)
            self.assertEqual(inspect.call_args.args[0][3:5], ["-p", "12345"])
            self.assertEqual(inspect.call_args.kwargs["timeout"], 0.75)
        screen._clients_at = 0
        with (
            patch("hidagent.screen.sys.platform", "darwin"),
            patch("hidagent.screen.subprocess.run", return_value=SimpleNamespace(returncode=1, stdout="", stderr="")),
        ):
            self.assertFalse(screen.live)
        screen._clients_at = 0
        with (
            patch("hidagent.screen.sys.platform", "darwin"),
            patch("hidagent.screen.subprocess.run", side_effect=subprocess.TimeoutExpired("lsof", 0.75)),
        ):
            self.assertFalse(screen.live)
            self.assertFalse(screen.metadata["connection_verified"])
            self.assertIn("cannot verify", screen.metadata["error"])

    def test_web_listener_defaults_to_loopback(self):
        run = unittest.mock.Mock()
        with patch.dict(sys.modules, {"uvicorn": SimpleNamespace(run=run)}), patch.dict(os.environ) as environment:
            environment.pop("HOST", None)
            runpy.run_module("hidagent.__main__", run_name="__main__")
        self.assertEqual(run.call_args.kwargs["host"], "127.0.0.1")


class HostAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import fastapi  # noqa: F401
            import httpx
        except ImportError as exc:
            raise unittest.SkipTest("FastAPI/httpx runtime dependencies are not installed") from exc
        cls.httpx = httpx

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.environment = patch.dict(os.environ, {"HIDAGENT_DATA": self.temporary.name})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.module = importlib.import_module("hidagent.app")
        self.calls = []

        async def reset():
            self.calls.append("reset")

        async def home():
            self.calls.append("home")

        self.phone = SimpleNamespace(reset=reset, go_home=home, calibrated=True)
        self.agent = SimpleNamespace(running=False, _job=None, stop=lambda: self.calls.append("agent-stop"))
        for name, value in {
            "phone": self.phone,
            "agent": self.agent,
            "screen": SimpleNamespace(live=True),
            "device": SimpleNamespace(connected=True),
            "manual_lock": asyncio.Lock(),
            "stop_lock": asyncio.Lock(),
            "manual_operation": None,
            "cancelled_manual": set(),
            "ALLOWED_HOSTS": frozenset({"localhost", "127.0.0.1", "::1"}),
        }.items():
            mock = patch.object(self.module, name, value)
            mock.start()
            self.addCleanup(mock.stop)

    def test_transport_factory_and_ble_lifespan_never_open_tcp(self):
        async def scenario():
            calls = self.calls

            async def start():
                calls.append("ble-start")

            async def close():
                calls.append("ble-close")

            async def awake(**kwargs):
                try:
                    await asyncio.Event().wait()
                finally:
                    calls.append("awake-cancelled")

            async def stop():
                calls.append("screen-stopped")

            device = SimpleNamespace(start=start, close=close, keep_awake=awake, connected=False)
            screen = SimpleNamespace(start=lambda: calls.append("screen-started"), aclose=stop)
            with (
                patch.object(self.module, "TRANSPORT", "ble"),
                patch.object(self.module, "device", device),
                patch.object(self.module, "screen", screen),
            ):
                async with self.module.lifespan(None):
                    await asyncio.sleep(0)
                    self.assertEqual(calls[:2], ["screen-started", "ble-start"])
                self.assertIn("awake-cancelled", calls)
                self.assertEqual(calls[-2:], ["ble-close", "screen-stopped"])

        with patch.dict(os.environ, {"HIDAGENT_BLE_ADDRESS": "configured-board"}):
            self.assertEqual(self.module.create_device("ble").address, "configured-board")
        with self.assertRaisesRegex(RuntimeError, "tcp or ble"):
            self.module.create_device("invalid")
        asyncio.run(scenario())

    def test_cancelled_startup_cannot_spawn_after_lifespan_cleanup(self):
        screen = Screen()
        entered, release = threading.Event(), threading.Event()

        def inspect(*args, **kwargs):
            entered.set()
            release.wait(2)
            return SimpleNamespace(returncode=0)

        async def scenario():
            with (
                patch.object(self.module, "screen", screen),
                patch.object(Screen, "_executable", return_value=sys.executable),
                patch("hidagent.screen.subprocess.run", side_effect=inspect),
                patch("hidagent.screen.subprocess.Popen") as spawn,
            ):

                async def enter():
                    async with self.module.lifespan(None):
                        self.fail("cancelled startup entered lifespan")

                job = asyncio.create_task(enter())
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                job.cancel()
                await asyncio.sleep(0)
                self.assertFalse(job.done())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(job, 2)
                spawn.assert_not_called()
                self.assertIsNone(screen._thread)
                self.assertIsNone(screen._proc)

        asyncio.run(scenario())

    def test_browser_origin_host_and_missing_capture_cannot_trigger_hid(self):
        async def scenario():
            async with self.httpx.AsyncClient(
                transport=self.httpx.ASGITransport(app=self.module.app), base_url="http://localhost"
            ) as client:
                for headers in ({"Origin": "https://foreign.example"}, {"Host": "rebound.example"}):
                    self.assertEqual((await client.post("/api/home", headers=headers)).status_code, 403)
                self.assertEqual(self.calls, [])
                self.assertEqual(
                    (await client.post("/api/home", headers={"Origin": "http://localhost"})).status_code, 200
                )
                self.assertEqual((await client.post("/api/home")).status_code, 200)
                self.module.screen.live = False
                self.assertEqual((await client.post("/api/home")).status_code, 409)
                self.assertEqual((await client.post("/api/agent/stop")).status_code, 200)
                self.assertEqual(self.calls.count("home"), 2)

        asyncio.run(scenario())

    def test_manual_stop_cancels_then_releases_and_blocks_interleaving(self):
        async def scenario():
            entered, resetting, release_reset = asyncio.Event(), asyncio.Event(), asyncio.Event()

            async def home():
                entered.set()
                try:
                    await asyncio.Event().wait()
                    self.calls.append("late-action")
                finally:
                    self.calls.append("gesture-release")

            async def reset():
                resetting.set()
                await release_reset.wait()
                self.calls.append("reset")

            self.phone.go_home, self.phone.reset = home, reset
            async with self.httpx.AsyncClient(
                transport=self.httpx.ASGITransport(app=self.module.app), base_url="http://localhost"
            ) as client:
                action = asyncio.create_task(client.post("/api/home"))
                await asyncio.wait_for(entered.wait(), 1)
                stopping = asyncio.create_task(client.post("/api/agent/stop"))
                await asyncio.wait_for(resetting.wait(), 1)
                self.assertEqual((await client.post("/api/home")).status_code, 409)
                release_reset.set()
                self.assertEqual((await stopping).status_code, 200)
                self.assertEqual((await action).status_code, 409)
            self.assertEqual(self.calls, ["agent-stop", "gesture-release", "reset"])

        asyncio.run(scenario())

    def test_asgi_disconnect_cancels_action_before_any_late_completion(self):
        async def scenario():
            entered, cancelled = asyncio.Event(), asyncio.Event()

            async def home():
                entered.set()
                try:
                    await asyncio.Event().wait()
                    self.calls.append("late-action")
                finally:
                    self.calls.append("gesture-release")
                    cancelled.set()

            self.phone.go_home = home
            incoming = asyncio.Queue()
            await incoming.put({"type": "http.request", "body": b"", "more_body": False})
            scope = {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/api/home",
                "raw_path": b"/api/home",
                "query_string": b"",
                "root_path": "",
                "headers": [(b"host", b"localhost")],
                "client": ("127.0.0.1", 1),
                "server": ("localhost", 80),
            }

            async def send(message):
                pass

            job = asyncio.create_task(self.module.app(scope, incoming.get, send))
            await asyncio.wait_for(entered.wait(), 1)
            await incoming.put({"type": "http.disconnect"})
            await asyncio.wait_for(cancelled.wait(), 1)
            await asyncio.wait_for(job, 2)
            self.assertEqual(self.calls, ["gesture-release", "reset"])

        asyncio.run(scenario())

    def test_pending_cancel_cleanup_blocks_agent_start_after_request_returns(self):
        async def scenario():
            release = asyncio.Event()

            async def resistant():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    await release.wait()

            async def disconnected():
                return True

            self.agent.start = lambda goal: self.calls.append("delegate")
            with self.assertRaises(self.module.HTTPException) as stopping:
                await self.module.run_manual(resistant, SimpleNamespace(is_disconnected=disconnected))
            self.assertEqual(stopping.exception.status_code, 409)
            self.assertFalse(self.module.manual_lock.locked())
            operation = self.module.manual_operation
            self.assertIsNotNone(operation)
            self.assertFalse(operation.done())
            with self.assertRaises(self.module.HTTPException) as busy:
                await self.module.agent_start(self.module.Text(text="Open Clock"))
            self.assertEqual(busy.exception.status_code, 409)
            self.assertNotIn("delegate", self.calls)
            release.set()
            await asyncio.wait_for(operation, 1)
            await asyncio.sleep(0)
            self.assertEqual(await self.module.agent_start(self.module.Text(text="Open Clock")), {"ok": True})

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
