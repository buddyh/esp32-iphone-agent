"""Transport tests use fake BLE clients; they never discover or control real devices."""

import asyncio
import unittest
from types import SimpleNamespace

from hidagent.ble_device import CMD, REPLY, SERVICE, BLEDevice
from hidagent.device import DeviceError


class FakeClient:
    def __init__(self, target, *, disconnected_callback, timeout):
        self.target, self.callback = target, disconnected_callback
        self.is_connected = False
        self.notifications = {}
        self.writes = []
        self.acknowledge = True
        self.block_write = False
        self.wrote = asyncio.Event()
        self.typed_text = ""

    async def connect(self):
        self.is_connected = True

    async def start_notify(self, characteristic, callback):
        self.notifications[characteristic] = callback

    def reply(self, request_id, result="ok"):
        self.notifications[REPLY](REPLY, bytearray(f"{request_id} {result}".encode()))

    async def write_gatt_char(self, characteristic, payload, *, response):
        assert characteristic == CMD and response is True
        # Model ble_link.cpp's v.trim() and hid_bridge.ino's rest.trim(),
        # rather than treating raw payload text as the keyboard's output.
        request_id, _, command = payload.decode().strip().partition(" ")
        self.writes.append((request_id, command))
        name, _, argument = command.partition(" ")
        argument = argument.strip()
        if name == "type":
            self.typed_text += argument
        elif name == "key" and argument == "space":
            self.typed_text += " "
        self.wrote.set()
        if self.block_write and command != "release":
            await asyncio.Event().wait()
        if self.acknowledge or command == "release":
            self.reply(request_id, "err unknown command" if command == "bad" else "ok")

    async def disconnect(self):
        self.is_connected = False
        self.callback(self)


class BLEDeviceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clients = []
        self.devices = []

    async def asyncTearDown(self):
        for device in self.devices:
            await device.close()

    def make_device(self, address="specific-board", **kwargs):
        def factory(*args, **options):
            client = FakeClient(*args, **options)
            self.clients.append(client)
            return client

        device = BLEDevice(address, client_factory=factory, reconnect_delay=.01,
                           connect_timeout=.1, write_timeout=.02, **kwargs)
        self.devices.append(device)
        return device

    async def ready(self, device, links=1):
        async def wait():
            while not device.connected or device.links < links:
                await asyncio.sleep(.001)
        await asyncio.wait_for(wait(), .5)
        return self.clients[-1]

    async def test_exact_address_round_trip_and_no_video_subscription(self):
        device = self.make_device()
        await device.start()
        client = await self.ready(device)
        await device.send("ping")
        self.assertEqual(client.target, "specific-board")
        self.assertEqual(set(client.notifications), {REPLY})
        self.assertEqual(client.writes[0][1], "ping")
        self.assertEqual(device.links, 1)
        self.assertFalse(device._pending)
        with self.assertRaisesRegex(DeviceError, "rejected"):
            await device.send("bad")

    async def test_ambiguous_discovery_never_connects(self):
        async def discover(**kwargs):
            self.assertEqual(kwargs["service_uuids"], [SERVICE])
            return {str(i): (SimpleNamespace(address=str(i), name="ESP32 Agent " + str(i)),
                             SimpleNamespace(service_uuids=[SERVICE], local_name=None)) for i in range(2)}

        device = self.make_device(None, discover=discover)
        await device.start()
        await asyncio.wait_for(device._supervisor, .5)
        self.assertFalse(device.connected)
        self.assertIn("HIDAGENT_BLE_ADDRESS", device.last_error)
        self.assertEqual(self.clients, [])

    async def test_unique_discovery_ignores_unrelated_devices(self):
        board = SimpleNamespace(address="chosen", name="ESP32 Agent ABCD")

        async def discover(**kwargs):
            return {"board": (board, SimpleNamespace(service_uuids=[SERVICE], local_name=None)),
                    "other": (SimpleNamespace(address="other", name="headphones"),
                              SimpleNamespace(service_uuids=[], local_name=None))}

        device = self.make_device(None, discover=discover)
        await device.start()
        client = await self.ready(device)
        self.assertIs(client.target, board)

    async def test_wrong_reply_id_does_not_complete_command(self):
        device = self.make_device()
        await device.start()
        client = await self.ready(device)
        client.acknowledge = False
        command = asyncio.create_task(device.send("ping"))
        await client.wrote.wait()
        client.reply("unrelated")
        await asyncio.sleep(0)
        self.assertFalse(command.done())
        client.reply(client.writes[0][0])
        await command

    async def test_timeout_invalidates_old_reply_before_reconnecting(self):
        device = self.make_device()
        device._timeout = lambda _: .01
        await device.start()
        old = await self.ready(device)
        old.acknowledge = False
        with self.assertRaisesRegex(DeviceError, "TimeoutError"):
            await device.send("press")
        request_id = old.writes[0][0]
        self.assertFalse(device._pending)
        self.assertTrue(any(command == "release" for _, command in old.writes))
        new = await self.ready(device, links=2)
        new.acknowledge = False
        command = asyncio.create_task(device.send("click"))
        await new.wrote.wait()
        old.reply(request_id)
        await asyncio.sleep(.001)
        self.assertFalse(command.done())
        new.reply(new.writes[0][0])
        await command

    async def test_cancelled_drag_releases_and_drops_uncertain_connection(self):
        device = self.make_device()
        await device.start()
        old = await self.ready(device)
        old.acknowledge = False
        command = asyncio.create_task(device.send("press"))
        await old.wrote.wait()
        command.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await command
        self.assertFalse(old.is_connected)
        self.assertFalse(device._pending)
        self.assertTrue(any(value == "release" for _, value in old.writes))
        await self.ready(device, links=2)
        await device.send("ping")

    async def test_disconnect_fails_pending_and_old_disconnect_cannot_drop_new_link(self):
        device = self.make_device()
        await device.start()
        old = await self.ready(device)
        old.acknowledge = False
        pending = asyncio.create_task(device.send("click"))
        await old.wrote.wait()
        await old.disconnect()
        with self.assertRaisesRegex(DeviceError, "disconnected"):
            await pending
        new = await self.ready(device, links=2)
        old.callback(old)
        await asyncio.sleep(.001)
        self.assertTrue(device.connected)
        self.assertTrue(new.is_connected)
        await device.send("ping")

    async def test_hung_gatt_write_is_bounded(self):
        device = self.make_device()
        await device.start()
        old = await self.ready(device)
        old.block_write = True
        with self.assertRaisesRegex(DeviceError, "TimeoutError"):
            await asyncio.wait_for(device.send("type private text"), .2)
        self.assertFalse(old.is_connected)
        self.assertFalse(device._pending)

    async def test_close_stops_reconnection_and_fails_unconnected_send(self):
        device = self.make_device()
        await device.start()
        client = await self.ready(device)
        await device.close()
        self.assertFalse(device.connected)
        self.assertFalse(client.is_connected)
        count = len(self.clients)
        await asyncio.sleep(.03)
        self.assertEqual(len(self.clients), count)
        with self.assertRaisesRegex(DeviceError, "not connected"):
            await device.send("click")

    async def test_sleeping_usb_error_is_actionable_without_exposing_command(self):
        device = self.make_device()
        await device.start()
        client = await self.ready(device)
        client.acknowledge = False
        command = asyncio.create_task(device.send("type private text"))
        await client.wrote.wait()
        client.reply(client.writes[0][0], "err phone usb asleep (unlock the phone)")
        with self.assertRaisesRegex(DeviceError, "unlock the iPhone") as raised:
            await command
        self.assertNotIn("private text", str(raised.exception))

    async def test_long_typing_preserves_text_and_blocks_interleaving_actions(self):
        device = self.make_device()
        await device.start()
        client = await self.ready(device)
        text = "A paragraph with spaces and punctuation. " * 100
        typing = asyncio.create_task(device.send("type " + text))
        await client.wrote.wait()
        click = asyncio.create_task(device.send("click"))
        await asyncio.gather(typing, click)
        commands = [command for _, command in client.writes]
        self.assertEqual(commands[-1], "click")
        self.assertTrue(all(command.startswith("type ") or command == "key space" for command in commands[:-1]))
        self.assertEqual(client.typed_text, text)
        self.assertTrue(all(len(f"{request_id} {command}".encode()) < 512
                            for request_id, command in client.writes))

    async def test_partial_typing_cancel_never_resumes_and_cleanup_releases_once(self):
        device = self.make_device()
        await device.start()
        old = await self.ready(device)
        second_chunk = asyncio.Event()
        original_write = old.write_gatt_char
        typed_chunks = 0

        async def write(characteristic, payload, *, response):
            nonlocal typed_chunks
            if payload.decode().partition(" ")[2].startswith("type "):
                typed_chunks += 1
                old.acknowledge = typed_chunks == 1
            await original_write(characteristic, payload, response=response)
            if typed_chunks == 2:
                second_chunk.set()

        old.write_gatt_char = write
        command = asyncio.create_task(device.send("type " + "private paragraph " * 200))
        await asyncio.wait_for(second_chunk.wait(), .5)
        command.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await command
        self.assertEqual(typed_chunks, 2)
        self.assertEqual(sum(value == "release" for _, value in old.writes), 1)
        new = await self.ready(device, links=2)
        self.assertFalse(any(value.startswith("type ") for _, value in new.writes))
        self.assertFalse(device._pending)

    async def test_non_typing_oversize_rejected_without_disconnecting(self):
        device = self.make_device()
        await device.start()
        client = await self.ready(device)
        with self.assertRaisesRegex(DeviceError, "511-byte"):
            await device.send("key " + "a" * 512)
        self.assertTrue(device.connected)
        self.assertEqual(client.writes, [])

    async def test_firmware_trimming_preserves_4000_character_whitespace_cases(self):
        cases = {
            "spaces at packet boundaries": (("x" * 497 + " ") * 9)[:3997] + "   ",
            "leading and trailing spaces": "   " + ("words with spaces " * 250)[:3994] + "   ",
            "only spaces": " " * 4000,
        }
        for name, text in cases.items():
            with self.subTest(name=name):
                self.assertEqual(len(text), 4000)
                device = self.make_device()
                await device.start()
                client = await self.ready(device)
                await device.send("type " + text)
                self.assertEqual(client.typed_text, text)
                self.assertTrue(all(len(f"{request_id} {command}".encode()) < 512
                                    for request_id, command in client.writes))
                await device.close()


if __name__ == "__main__":
    unittest.main()
