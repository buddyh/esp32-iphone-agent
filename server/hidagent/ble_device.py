"""Mac BLE transport for the current S3 firmware, without its video channel."""

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .device import Device, DeviceError

log = logging.getLogger(__name__)
SERVICE = "6e400001-a9e8-4b1b-9c39-6d6f8a3c0000"
CMD = "6e400001-a9e8-4b1b-9c39-6d6f8a3c0001"
REPLY = "6e400001-a9e8-4b1b-9c39-6d6f8a3c0002"


async def _bounded(awaitable: Awaitable[Any], timeout: float) -> Any:
    """Like asyncio.wait_for, but never loses a cancellation of the caller.

    On Python 3.10, wait_for returns the result instead of raising CancelledError when the caller is cancelled
    in the same loop iteration that the awaited operation finishes, so a cancelled drag would carry on.
    """
    inner = asyncio.ensure_future(awaitable)
    try:
        done, _ = await asyncio.wait({inner}, timeout=timeout)
    except asyncio.CancelledError:
        inner.cancel()
        raise
    if not done:
        inner.cancel()
        await asyncio.wait({inner})  # unlike awaiting it, this raises only if the caller is cancelled
        raise asyncio.TimeoutError
    return inner.result()


class BoardSelectionError(DeviceError):
    """Discovery found multiple boards and needs an explicit device address."""


class BLEDevice(Device):
    """Use BLE for HID while UxPlay captures the iPhone directly on its normal LAN.

    ``start`` launches a reconnecting supervisor; it does not require a board to be
    present. ``last_error`` explains readiness failures to the HTTP status endpoint.
    Client/discovery injection keeps host tests independent of the optional Bleak package.
    """

    def __init__(self, address: str | None = None, *, scan_timeout: float = 8,
                 connect_timeout: float = 12, reconnect_delay: float = 2,
                 write_timeout: float = 5, client_factory: Callable[..., Any] | None = None,
                 discover: Callable[..., Awaitable[dict]] | None = None) -> None:
        super().__init__()
        if min(scan_timeout, connect_timeout, reconnect_delay, write_timeout) <= 0:
            raise ValueError("BLE timeouts and reconnect delay must be positive")
        self.address = (address.strip() or None) if address else None
        self.scan_timeout, self.connect_timeout = scan_timeout, connect_timeout
        self.reconnect_delay, self.write_timeout = reconnect_delay, write_timeout
        self.last_error: str | None = None
        self._client_factory, self._discover = client_factory, discover
        self._client: Any = None
        self._supervisor: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lost = asyncio.Event()
        self._closing = False
        self._next_id = 1_000_000
        self._pending: dict[str, tuple[Any, asyncio.Future[str]]] = {}
        self._disposals: dict[int, asyncio.Task] = {}

    @property
    def connected(self) -> bool:
        """True only after command-reply notifications are subscribed."""
        return self._client is not None and self._client.is_connected

    async def start(self) -> None:
        """Start BLE discovery/reconnection, importing Bleak only when selected."""
        if self._supervisor and not self._supervisor.done():
            return
        if self._client_factory is None or (self.address is None and self._discover is None):
            try:
                from bleak import BleakClient, BleakScanner
            except ImportError as exc:
                raise DeviceError("BLE transport requires the optional server[ble] dependencies") from exc
            self._client_factory = self._client_factory or BleakClient
            self._discover = self._discover or BleakScanner.discover
        self._loop = asyncio.get_running_loop()
        self._closing = False
        self._supervisor = asyncio.create_task(self._run(), name="iphone-agent-ble")

    async def close(self) -> None:
        """Release the pointer, fail pending commands and stop reconnecting."""
        self._closing = True
        if self._supervisor:
            self._supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._supervisor
            self._supervisor = None
        if self._disposals:
            await asyncio.gather(*self._disposals.values(), return_exceptions=True)
            self._disposals.clear()

    async def _target(self) -> Any:
        if self.address:
            return self.address
        assert self._discover is not None
        found = await asyncio.wait_for(
            self._discover(timeout=self.scan_timeout, return_adv=True, service_uuids=[SERVICE]),
            self.scan_timeout + 1,
        )
        boards = {}
        for device, advertisement in found.values():
            services = [value.lower() for value in (advertisement.service_uuids or [])]
            name = advertisement.local_name or device.name or ""
            if SERVICE in services or name.startswith("ESP32 Agent "):
                boards[device.address] = device
        if len(boards) > 1:
            raise BoardSelectionError("Multiple ESP32 Agent boards found; set HIDAGENT_BLE_ADDRESS explicitly")
        if not boards:
            raise DeviceError("No ESP32 Agent board found; check its power and Mac Bluetooth permission")
        return next(iter(boards.values()))

    async def _run(self) -> None:
        failures = 0
        while not self._closing:
            client = None
            try:
                target = await self._target()
                assert self._client_factory is not None
                client = self._client_factory(target, disconnected_callback=self._disconnected,
                                              timeout=self.connect_timeout)
                await asyncio.wait_for(client.connect(), self.connect_timeout)
                await asyncio.wait_for(client.start_notify(
                    REPLY, lambda _, data, owner=client: self._notification(owner, bytes(data))),
                    self.connect_timeout)
                if not client.is_connected:
                    raise DeviceError("BLE board disconnected during setup")
                self._lost = asyncio.Event()
                self._client = client
                self.links += 1
                self._last = self._beat = time.time()
                self.last_error = None
                failures = 0
                log.info("BLE board ready")
                await self._lost.wait()
            except asyncio.CancelledError:
                raise
            except BoardSelectionError as exc:
                self.last_error = str(exc)
                log.warning("%s", self.last_error)
                return  # never select an arbitrary phone's board
            except DeviceError as exc:
                self.last_error = str(exc)
            except Exception as exc:
                self.last_error = (f"BLE connection failed ({type(exc).__name__}); "
                                   "check the board, Bluetooth permission and configured address")
            finally:
                if client is not None:
                    await self._discard_link(client, release=True)
            if not self._closing:
                delay = min(30, self.reconnect_delay * (2 ** min(failures, 4)))
                failures += 1
                await asyncio.sleep(delay)

    def _dispatch(self, callback: Callable, *args: Any) -> None:
        if self._loop and not self._loop.is_closed():
            with contextlib.suppress(RuntimeError):
                self._loop.call_soon_threadsafe(callback, *args)

    def _notification(self, client: Any, data: bytes) -> None:
        self._dispatch(self._reply, client, data)

    def _reply(self, client: Any, data: bytes) -> None:
        request_id, _, reply = data.decode(errors="replace").strip().partition(" ")
        pending = self._pending.get(request_id)
        if pending and pending[0] is client and not pending[1].done():
            pending[1].set_result(reply)

    def _disconnected(self, client: Any) -> None:
        self._dispatch(self._link_lost, client)

    def _link_lost(self, client: Any) -> None:
        if self._client is client:
            self._client = None
            self.last_error = "BLE board disconnected; reconnecting"
            self._lost.set()
        for owner, future in self._pending.values():
            if owner is client and not future.done():
                future.set_exception(DeviceError("BLE board disconnected"))

    async def _discard_link(self, client: Any, *, release: bool) -> None:
        self._link_lost(client)
        key = id(client)
        cleanup = self._disposals.get(key)
        if cleanup is None:
            cleanup = asyncio.create_task(self._disconnect_client(client, release=release))
            self._disposals[key] = cleanup
        try:
            # The send path and supervisor can notice the same failure together.
            # Both wait for one cleanup; neither can cancel its release midway.
            await asyncio.shield(cleanup)
        finally:
            if cleanup.done() and self._disposals.get(key) is cleanup:
                self._disposals.pop(key)

    async def _disconnect_client(self, client: Any, *, release: bool) -> None:
        if release and client.is_connected:
            # A cancelled drag may have left the mouse down. Use a fresh id so its
            # acknowledgement cannot complete any timed-out/cancelled request.
            self._next_id += 1
            with contextlib.suppress(Exception):
                await asyncio.wait_for(client.write_gatt_char(
                    CMD, f"{self._next_id} release".encode(), response=True), min(self.write_timeout, .5))
        with contextlib.suppress(Exception):
            await asyncio.wait_for(client.disconnect(), min(self.connect_timeout, 2))

    async def send(self, command: str) -> None:
        """Send commands in order, splitting long ASCII typing under one lock.

        Neither commands nor replies are logged. Cancelled/failed typing is never
        resumed automatically, because some characters may already have arrived.
        """
        if not command or "\n" in command or "\r" in command:
            raise DeviceError("BLE command must be a nonempty single line")
        async with self._lock:
            if not self.connected:
                raise DeviceError("board not connected")
            client = self._client
            if command.startswith("type "):
                text = command[5:]
                if not text.isascii() or any(ord(char) < 32 or ord(char) > 126 for char in text):
                    raise DeviceError("BLE typing supports printable ASCII text on one line")
                while text:
                    if text.startswith(" "):
                        # Both firmware parsers trim commands/arguments. A space
                        # at a chunk edge must travel as a key, or it disappears.
                        await self._send_one(client, "key space")
                        text = text[1:]
                        continue
                    # Firmware CMD_MAX is 512 including the final NUL; request id
                    # and command prefix count toward that limit too.
                    limit = 511 - len(str(self._next_id + 1)) - len(" type ")
                    chunk = text[:limit].rstrip(" ")
                    text = text[len(chunk):]
                    await self._send_one(client, "type " + chunk)
            else:
                await self._send_one(client, command)

    async def _send_one(self, client: Any, command: str) -> None:
        if self._client is not client or not client.is_connected:
            raise DeviceError("board disconnected; incomplete typing will not be resumed")
        self._next_id += 1
        request_id = str(self._next_id)
        payload = f"{request_id} {command}".encode()
        if len(payload) >= 512:
            raise DeviceError("BLE command exceeds the board's 511-byte limit")
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = (client, future)
        try:
            await _bounded(client.write_gatt_char(CMD, payload, response=True), self.write_timeout)
            reply = await _bounded(future, self._timeout(command))
        except asyncio.CancelledError:
            self._pending.pop(request_id, None)
            future.cancel()
            await asyncio.shield(self._discard_link(client, release=True))
            raise
        except Exception as exc:
            self._pending.pop(request_id, None)
            future.cancel()
            await self._discard_link(client, release=True)
            if isinstance(exc, DeviceError):
                raise
            raise DeviceError(f"BLE command failed ({type(exc).__name__}); reconnecting") from exc
        finally:
            self._pending.pop(request_id, None)
            # Disconnect can fail this future while a GATT write is still pending.
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()
        self._beat = time.time()
        if command != "ping":
            self._last = self._beat
        if reply.partition(" ")[0] != "ok":
            if reply.startswith("err phone usb asleep"):
                raise DeviceError("phone USB is asleep; unlock the iPhone and check its S3 connection")
            raise DeviceError("BLE board rejected the command")
