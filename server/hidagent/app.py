"""Web API + control page. Run with `python -m hidagent`."""

import asyncio
import os
import unicodedata
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.types import ASGIApp, Receive, Scope, Send

from .agent import Agent
from .debug import Trace
from .device import Device, DeviceError
from .llm import LLM
from .phone import Phone
from .screen import Screen

DATA_DIR = Path(os.environ.get("HIDAGENT_DATA", Path.home() / ".hidagent"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DEBUG = os.environ.get("HIDAGENT_DEBUG") == "1"

TRANSPORT = os.environ.get("HIDAGENT_TRANSPORT", "tcp").strip().lower()
ALLOWED_HOSTS = frozenset(
    value.strip().lower().strip("[]")
    for value in os.environ.get("HIDAGENT_ALLOWED_HOSTS", "localhost,127.0.0.1,::1").split(",") if value.strip()
)


def create_device(transport: str):
    """Choose the board command transport; BLE never opens a TCP board listener."""
    if transport == "tcp":
        return Device()
    if transport == "ble":
        from .ble_device import BLEDevice
        return BLEDevice(address=os.environ.get("HIDAGENT_BLE_ADDRESS") or None)
    raise RuntimeError("HIDAGENT_TRANSPORT must be tcp or ble")


device = create_device(TRANSPORT)
# MIRROR_STREAM=127.0.0.1:8891 follows a standalone receiver (python -m hidagent.receiver) instead of running
# UxPlay here, so restarting the server keeps the phone's Screen Mirroring session
screen = Screen(os.environ.get("AIRPLAY_NAME", "iPhone Agent"), stream=os.environ.get("MIRROR_STREAM"),
                pidfile=DATA_DIR / "uxplay.pid")
phone = Phone(device, DATA_DIR / "calibration.json", screen)
agent = Agent(phone, screen, LLM.from_env("ACTOR"), LLM.from_env("PLANNER"),
              Trace(DATA_DIR / "traces" if DEBUG else None))


@asynccontextmanager
async def lifespan(_):
    """Start capture and one board transport, then await every owned resource on exit."""
    async with AsyncExitStack() as cleanup:
        cleanup.push_async_callback(screen.aclose)
        await _start_screen()
        if TRANSPORT == "ble":
            cleanup.push_async_callback(device.close)
            await device.start()
        else:
            server = await device.serve(os.environ.get("DEVICE_HOST", "0.0.0.0"),
                                        int(os.environ.get("DEVICE_PORT", 7300)))
            cleanup.push_async_callback(_close_tcp, server)
        awake = asyncio.create_task(device.keep_awake(screen=screen))
        cleanup.push_async_callback(_cancel_task, awake)
        cleanup.push_async_callback(agent_stop)
        yield


async def _start_screen() -> None:
    """Own startup even if lifespan is cancelled during a blocking dependency check."""
    startup = asyncio.create_task(asyncio.to_thread(screen.start))
    try:
        await asyncio.shield(startup)
    except asyncio.CancelledError:
        screen.request_stop()
        try:
            await asyncio.shield(startup)
        except Exception:
            pass
        raise


async def _cancel_task(task: asyncio.Task) -> None:
    task.cancel()
    done, pending = await asyncio.wait({task}, timeout=2)
    if pending:
        raise RuntimeError("background task did not stop within 2 seconds")
    await asyncio.gather(*done, return_exceptions=True)


async def _close_tcp(server) -> None:
    server.close()
    await server.wait_closed()
    writer = getattr(device, "_writer", None)
    if writer is not None:
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), 2)
        except (OSError, asyncio.TimeoutError):
            pass


app = FastAPI(lifespan=lifespan)


def trusted_request(request: Request) -> JSONResponse | None:
    """Keep the local control API inaccessible through browser CSRF or DNS rebinding."""
    try:
        hosts = request.headers.getlist("host")
        if len(hosts) != 1:
            raise ValueError
        host = urlsplit("//" + hosts[0])
        if host.hostname not in ALLOWED_HOSTS or host.username is not None or host.path:
            raise ValueError
        port = host.port or (443 if request.url.scheme == "https" else 80)
    except ValueError:
        return JSONResponse({"detail": "untrusted Host header"}, status_code=403)
    origins = request.headers.getlist("origin")
    if origins:
        try:
            origin = urlsplit(origins[0])
            expected = (request.url.scheme, host.hostname, port)
            actual = (origin.scheme, origin.hostname, origin.port or (443 if origin.scheme == "https" else 80))
            if (len(origins) != 1 or actual != expected or origin.path or origin.query
                    or origin.fragment or origin.username):
                raise ValueError
        except ValueError:
            return JSONResponse({"detail": "only same-origin browser requests are accepted"}, status_code=403)
    return None


class TrustedRequestMiddleware:
    """Validate headers without wrapping receive or hiding http.disconnect from actions."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            rejected = trusted_request(Request(scope))
            if rejected is not None:
                await rejected(scope, receive, send)
                return
        await self.app(scope, receive, send)


app.add_middleware(TrustedRequestMiddleware)


class Static(StaticFiles):
    """Static files that browsers re-check on every load, so a new version shows up on refresh."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", Static(directory=Path(__file__).parent / "static"), name="static")


class Point(BaseModel):
    x: float = Field(ge=0, le=1, allow_inf_nan=False)
    y: float = Field(ge=0, le=1, allow_inf_nan=False)


class Tap(Point):
    precise: bool = False  # find the pointer on screen and correct a miss before tapping (about 1 s slower)


class Swipe(BaseModel):
    x1: float = Field(ge=0, le=1, allow_inf_nan=False)
    y1: float = Field(ge=0, le=1, allow_inf_nan=False)
    x2: float = Field(ge=0, le=1, allow_inf_nan=False)
    y2: float = Field(ge=0, le=1, allow_inf_nan=False)


class Scroll(Point):
    lines: int = Field(ge=-100, le=100)


class Text(BaseModel):
    text: str


class KeyboardText(Text):
    text: str = Field(min_length=1, max_length=4000)


def check_keyboard(text, ascii_only=False):
    """One line, no control characters. Non-ASCII text is fine for typing (it is pasted), never for keys."""
    if (ascii_only and not text.isascii()) or any(unicodedata.category(c) in ("Cc", "Zl", "Zp") for c in text):
        raise HTTPException(422, "keys must be printable ASCII" if ascii_only else "text must be one printable line")


manual_lock = asyncio.Lock()
stop_lock = asyncio.Lock()
manual_operation: asyncio.Task | None = None
cancelled_manual: set[asyncio.Task] = set()


def check_manual():
    pending_operation = manual_operation is not None and not manual_operation.done()
    if agent.running or manual_lock.locked() or stop_lock.locked() or pending_operation:
        raise HTTPException(409, "phone is busy; stop the agent or wait for the current action")
    if not screen.live:
        raise HTTPException(409, "no verified live screen: start Screen Mirroring before controlling the phone")


def _forget_manual(operation: asyncio.Task) -> None:
    global manual_operation
    if manual_operation is operation:
        manual_operation = None
    cancelled_manual.discard(operation)


async def _watch_disconnect(request: Request, finished: asyncio.Event) -> bool:
    """Return True once the client disconnects; return False once `finished` is set.

    Starlette's is_disconnected() runs inside an anyio cancel scope that can swallow this task's cancellation,
    so the event, not cancel(), is what reliably ends the watcher.
    """
    while not finished.is_set():
        if await request.is_disconnected():
            return True
        try:
            await asyncio.wait_for(finished.wait(), 0.2)
        except asyncio.TimeoutError:
            pass
    return False


async def _release_after(tasks: set[asyncio.Task]) -> None:
    if tasks:
        done, pending = await asyncio.wait(tasks, timeout=2)
        await asyncio.gather(*done, return_exceptions=True)
        if pending:
            raise HTTPException(409, "active action is still stopping; wait before issuing another action")
    try:
        if device.connected:
            await asyncio.wait_for(phone.reset(), 5)
    except (DeviceError, RuntimeError, asyncio.TimeoutError) as e:
        raise HTTPException(409, f"pointer release could not be confirmed: {e}") from e


async def _stop_disconnected(operation: asyncio.Task) -> None:
    async with stop_lock:
        cancelled_manual.add(operation)
        operation.cancel()
        await _release_after({operation})


async def run_manual(action: Callable[[], Awaitable[Any]], request: Request | None = None) -> Any:
    """Run one cancellable manual action under the same exclusivity guard as the agent."""
    global manual_operation
    check_manual()
    try:
        async with manual_lock:
            operation = asyncio.create_task(action())
            manual_operation = operation
            finished = asyncio.Event()
            watcher = asyncio.create_task(_watch_disconnect(request, finished)) if request is not None else None
            try:
                if watcher is not None:
                    done, _ = await asyncio.wait({operation, watcher}, return_when=asyncio.FIRST_COMPLETED)
                    if watcher in done and watcher.result():
                        await _stop_disconnected(operation)
                        raise HTTPException(409, "manual action stopped after client disconnect")
                return await operation
            except asyncio.CancelledError:
                if operation in cancelled_manual:
                    raise HTTPException(409, "manual action stopped") from None
                if not operation.done():
                    await asyncio.shield(_stop_disconnected(operation))
                raise
            finally:
                if watcher is not None:
                    finished.set()
                    watcher.cancel()
                    await asyncio.gather(watcher, return_exceptions=True)
                if operation.done():
                    _forget_manual(operation)
                else:
                    operation.add_done_callback(_forget_manual)
    except (DeviceError, RuntimeError) as e:
        raise HTTPException(409, str(e)) from e


async def act(action, request: Request | None = None):
    await run_manual(action, request)
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/api/status")
def status():
    return {"device": device.connected, "screen": screen.live, "calibrated": phone.calibrated,
            "transport": TRANSPORT, "device_error": getattr(device, "last_error", None),
            "capture": getattr(screen, "metadata", {}),
            "debug": DEBUG, "trace": str(agent.trace.dir or ""), "aspect": round(screen.aspect, 4),
            "models": {"planner": agent.planner.model, "actor": agent.actor.model},
            "agent": {"running": agent.running, "task": agent.task, "steps": agent.steps}}


@app.get("/api/frame.jpg")
def frame():
    if not screen.live:
        raise HTTPException(404, "no live screen: start screen mirroring on the phone")
    metadata = getattr(screen, "metadata", {})
    return Response(screen.frame, media_type="image/jpeg", headers={"Cache-Control": "no-store",
                    "X-Frame-Age-Seconds": str(metadata.get("frame_age_seconds", "unknown")),
                    "X-Frame-Connection-Verified": str(metadata.get("connection_verified", False)).lower()})


@app.get("/api/steps/{index}.jpg")
def step_shot(index: int):
    if index not in agent.shots:
        raise HTTPException(404)
    return Response(agent.shots[index], media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})


@app.post("/api/tap")
async def tap(p: Tap, request: Request):
    return await act(lambda: phone.tap(p.x, p.y, aim=True) if p.precise else phone.tap(p.x, p.y), request)


@app.post("/api/swipe")
async def swipe(s: Swipe, request: Request):
    return await act(lambda: phone.swipe(s.x1, s.y1, s.x2, s.y2), request)


@app.post("/api/scroll")
async def scroll(s: Scroll, request: Request):
    return await act(lambda: phone.scroll(s.x, s.y, s.lines), request)


@app.post("/api/type")
async def type_text(t: KeyboardText, request: Request):
    check_keyboard(t.text)
    return await act(lambda: phone.type(t.text), request)


@app.post("/api/key")
async def key(t: KeyboardText, request: Request):
    check_keyboard(t.text, ascii_only=True)
    return await act(lambda: phone.key(t.text), request)


@app.post("/api/home")
async def home(request: Request):
    return await act(phone.go_home, request)


@app.post("/api/back")
async def back(request: Request):
    return await act(phone.go_back, request)


@app.post("/api/raw")
async def raw(t: Text, request: Request):
    """Debug only: send one protocol line to the board as-is."""
    if not DEBUG:
        raise HTTPException(404)

    async def send():
        phone.forget_position()  # the server will not know where the pointer is, even if the board errs
        await device.send(t.text)
    return await act(send, request)


@app.post("/api/calibrate/start")
async def calibrate_start(request: Request):
    """Calibrate the pointer. {"auto": true}: done by itself; otherwise the user clicks the pointer next."""
    automatic = await run_manual(phone.start_calibration, request)
    return {"ok": True, "auto": automatic, "span": phone.span}


@app.post("/api/calibrate/finish")
async def calibrate_finish(p: Point):
    check_manual()
    if not 0 < p.x < 1 or not 0 < p.y < 1:
        raise HTTPException(422, "calibration pointer must be inside the screen")
    phone.finish_calibration(p.x, p.y)
    return {"ok": True, "span": phone.span}


@app.post("/api/agent")
async def agent_start(t: Text):
    if manual_lock.locked() or stop_lock.locked() or (manual_operation is not None and not manual_operation.done()):
        raise HTTPException(409, "phone is busy; wait for the current action")
    try:
        agent.start(t.text)
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e
    return {"ok": True}


@app.post("/api/agent/stop")
async def agent_stop():
    """Cancel agent/manual work, await gesture release, and reset the pointer."""
    async with stop_lock:
        agent.stop()
        tasks = set()
        if manual_operation is not None and not manual_operation.done():
            cancelled_manual.add(manual_operation)
            manual_operation.cancel()
            tasks.add(manual_operation)
        job = getattr(agent, "_job", None)
        if job is not None and not job.done():
            tasks.add(job)
        await _release_after(tasks)
    return {"ok": True}


@app.post("/api/agent/clear")
def agent_clear():
    """Forget the last run (steps, screenshots, plan) so the page starts clean."""
    try:
        agent.clear()
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e
    return {"ok": True}
