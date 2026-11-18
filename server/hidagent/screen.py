"""The phone's screen, received over AirPlay mirroring by UxPlay, as JPEG frames."""

import asyncio
import logging
import os
import select
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)


class Screen:
    """Runs UxPlay with a GStreamer sink that writes JPEGs into a pipe; keeps the latest one."""

    def __init__(self, name: str = "iPhone Agent", fps: int = 8, quality: int = 75):
        self.name, self.fps, self.quality = name, fps, quality
        self.frame: bytes | None = None
        self.updated = 0.0
        self._proc = None
        self._clients_at, self._clients_n = 0.0, None
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._updated_monotonic = 0.0
        self.last_error: str | None = None
        self._binary = ""
        self._extra_args: list[str] = []
        self._connection_error: str | None = None
        self._lifecycle_lock = threading.Lock()

    def start(self) -> None:
        """Validate capture dependencies and start an owned, joinable receiver."""
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("AirPlay receiver is already running")
        if self._stopping.is_set():
            raise RuntimeError("AirPlay startup was cancelled; create a new receiver before restarting")
        self._binary = self._executable("UXPLAY_BIN", "uxplay")
        inspector = self._executable("GST_INSPECT_BIN", "gst-inspect-1.0")
        for plugin in ("h264parse", "decodebin", "videoconvert", "videorate", "jpegenc", "fdsink"):
            if self._stopping.is_set():
                raise RuntimeError("AirPlay startup was cancelled")
            try:
                checked = subprocess.run([inspector, plugin], stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL, timeout=3, check=False)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(f"GStreamer inspection timed out for {plugin}") from exc
            if checked.returncode:
                raise RuntimeError(f"GStreamer element '{plugin}' is missing; repair GStreamer before starting capture")
        self._extra_args = shlex.split(os.environ.get("UXPLAY_ARGS", ""))
        if "-p" in self._extra_args:
            raise RuntimeError("set UXPLAY_PORT instead of -p in UXPLAY_ARGS so ports can be checked")
        base = os.environ.get("UXPLAY_PORT")
        if base:
            try:
                port = int(base)
            except ValueError as exc:
                raise RuntimeError("UXPLAY_PORT must be a base port between 1024 and 65533") from exc
            if not 1024 <= port <= 65533:
                raise RuntimeError("UXPLAY_PORT must be a base port between 1024 and 65533")
            for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
                for candidate in range(port, port + 3):
                    try:
                        with socket.socket(socket.AF_INET, kind) as probe:
                            probe.bind(("0.0.0.0", candidate))
                    except OSError as exc:
                        protocol = "TCP" if kind == socket.SOCK_STREAM else "UDP"
                        message = f"UxPlay {protocol} port {candidate} is unavailable; choose another UXPLAY_PORT"
                        raise RuntimeError(message) from exc
            self._extra_args += ["-p", str(port)]
        process, fd = self._spawn()
        time.sleep(0.2)
        if self._stopping.is_set() or process.poll() is not None:
            os.close(fd)
            self._reap(process)
            self._proc = None
            raise RuntimeError("UxPlay stopped during startup; check its output and receiver configuration")
        self._thread = threading.Thread(target=self._supervise, args=(process, fd), name="hidagent-airplay")
        try:
            self._thread.start()
        except BaseException:
            self._reap(process)
            os.close(fd)
            self._proc = None
            raise

    @staticmethod
    def _executable(variable: str, fallback: str) -> str:
        configured = os.environ.get(variable)
        if configured:
            target = Path(configured).expanduser()
            if not target.is_absolute() or not target.is_file() or not os.access(target, os.X_OK):
                raise RuntimeError(f"{variable} must name an executable absolute path")
            return str(target)
        found = shutil.which(fallback)
        if found is None:
            raise RuntimeError(f"{fallback} is missing; install it or configure {variable}")
        return found

    @staticmethod
    def _reap(process) -> None:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)

    def request_stop(self) -> None:
        """Synchronously prevent startup/restart from spawning another owned receiver."""
        with self._lifecycle_lock:
            self._stopping.set()

    async def stop(self) -> None:
        """Stop capture and await process/pipe cleanup without blocking the API loop."""
        self.request_stop()
        self.frame = None
        thread = self._thread
        if thread is not None:
            await asyncio.to_thread(thread.join, 3)
            if thread.is_alive():
                raise RuntimeError("AirPlay receiver did not finish shutdown within 3 seconds")
        self._thread = None

    def _spawn(self):
        read_fd, write_fd = os.pipe()
        sink = (f"videorate drop-only=true ! video/x-raw,framerate={self.fps}/1 ! "
                f"jpegenc quality={self.quality} ! fdsink fd={write_fd}")
        try:
            with self._lifecycle_lock:
                if self._stopping.is_set():
                    raise RuntimeError("AirPlay startup was cancelled")
                process = subprocess.Popen(
                    [self._binary, "-n", self.name, "-nh", "-a", "-reset", "0", *self._extra_args, "-vs", sink],
                    pass_fds=(write_fd,))  # UxPlay logs to stdout: keep them in ours
        except (OSError, RuntimeError):
            os.close(read_fd)
            raise
        finally:
            os.close(write_fd)
        self._proc = process
        self._clients_at, self._clients_n = 0.0, None
        self.last_error = None
        log.info("AirPlay receiver '%s' started (pid %d)", self.name, process.pid)
        return process, read_fd

    def _supervise(self, process, fd: int) -> None:
        """Keep UxPlay running: it exits on some disconnects, so restart it every time."""
        while True:
            try:
                self._read(fd)
            except OSError as exc:
                self.last_error = f"AirPlay capture pipe failed: {exc}"
            finally:
                self._reap(process)
                self.frame = None
                self._proc = None
            if self._stopping.wait(1):
                return
            log.warning("AirPlay receiver exited (code %s), restarting", process.returncode)
            while not self._stopping.is_set():
                try:
                    process, fd = self._spawn()
                    break
                except (OSError, RuntimeError) as exc:
                    self.last_error = f"AirPlay receiver restart failed: {exc}"
                    log.error("%s", self.last_error)
                    self._stopping.wait(1)
            else:
                return

    @property
    def live(self) -> bool:
        running = not self._stopping.is_set() and self._proc is not None and self._proc.poll() is None
        if not running or self.frame is None:
            return False
        clients = self._clients()
        # A still phone can send no new frames. Require a receiver connection, not an arbitrary frame age.
        return clients is not None and clients > 0

    @property
    def metadata(self) -> dict:
        """Report capture age without claiming that a still frame proves connectivity."""
        running = not self._stopping.is_set() and self._proc is not None and self._proc.poll() is None
        clients = self._clients() if running else 0
        age = max(0.0, time.monotonic() - self._updated_monotonic) if self.frame is not None else None
        return {"receiver_running": running, "frame_available": self.frame is not None,
                "frame_age_seconds": round(age, 3) if age is not None else None,
                "connection_verified": clients is not None, "connected_clients": clients,
                "error": self.last_error or self._connection_error}

    def _clients(self) -> int | None:
        """Count this receiver's TCP connections; unknown inspection fails closed."""
        if time.monotonic() - self._clients_at < 1:
            return self._clients_n
        self._connection_error = None
        if sys.platform == "darwin":
            try:
                result = subprocess.run(["/usr/sbin/lsof", "-nP", "-a", "-p", str(self._proc.pid),
                                         "-iTCP", "-sTCP:ESTABLISHED", "-F", "f"],
                                        capture_output=True, text=True, timeout=0.75, check=False)
                if result.returncode not in (0, 1) or (result.returncode == 1 and result.stderr.strip()):
                    raise RuntimeError("receiver connection inspection failed")
                n = sum(line.startswith("f") for line in result.stdout.splitlines())
            except (OSError, subprocess.TimeoutExpired, RuntimeError):
                n = None
                self._connection_error = "cannot verify AirPlay receiver connections"
            self._clients_at, self._clients_n = time.monotonic(), n
            return n
        try:
            fds = os.listdir(f"/proc/{self._proc.pid}/fd")
            inodes = set()
            for fd in fds:
                link = os.readlink(f"/proc/{self._proc.pid}/fd/{fd}")
                if link.startswith("socket:["):
                    inodes.add(link[8:-1])
            n = 0
            for table in ("/proc/net/tcp", "/proc/net/tcp6"):
                with open(table) as f:
                    for row in f.readlines()[1:]:
                        cols = row.split()
                        n += cols[3] == "01" and cols[9] in inodes  # 01 = ESTABLISHED
        except OSError:
            n = None
            self._connection_error = "cannot verify AirPlay receiver connections"
        self._clients_at, self._clients_n = time.monotonic(), n
        return n

    @property
    def aspect(self) -> float:
        """Frame width / height, read from the JPEG header (portrait iPhone as a fallback)."""
        f = self.frame or b""
        i = f.find(b"\xff\xc0")  # baseline start-of-frame: ... height(2) width(2)
        if i < 0 or len(f) < i + 9:
            return 9 / 19.5
        return int.from_bytes(f[i + 7:i + 9], "big") / int.from_bytes(f[i + 5:i + 7], "big")

    async def settled(self, quiet: float = 0.35, least: float = 0.25, most: float = 4) -> None:
        """Wait until the screen stops changing (AirPlay only sends frames on change)."""
        started = time.time()
        await asyncio.sleep(least)
        while time.time() - self.updated < quiet and time.time() - started < most:
            await asyncio.sleep(0.1)

    async def fresh(self, since: float, timeout: float = 0.8) -> bytes | None:
        """The first frame received after `since`, or None if none arrives in time."""
        deadline = time.time() + timeout
        while self.updated <= since:
            if time.time() > deadline:
                return None
            await asyncio.sleep(0.02)
        return self.frame

    def _read(self, fd: int) -> None:
        buf = b""
        with os.fdopen(fd, "rb", buffering=0) as pipe:
            while not self._stopping.is_set():
                if not select.select([pipe], [], [], 0.2)[0]:
                    continue
                chunk = pipe.read(1 << 16)
                if not chunk:
                    return
                buf += chunk
                if len(buf) > 8 * 1024 * 1024:
                    self.last_error = "AirPlay JPEG exceeded the 8 MiB frame limit"
                    buf = buf[-1:]
                while (start := buf.find(b"\xff\xd8")) >= 0 and (end := buf.find(b"\xff\xd9", start)) >= 0:
                    self.frame, self.updated = buf[start:end + 2], time.time()
                    self._updated_monotonic = time.monotonic()
                    buf = buf[end + 2:]
