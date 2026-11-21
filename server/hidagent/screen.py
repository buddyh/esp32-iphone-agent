"""The phone's screen, received over AirPlay mirroring by UxPlay, as JPEG frames."""

import asyncio
import logging
import os
import select
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

MAX_FRAME = 8 * 1024 * 1024
STILL_FOR = 90  # where connections cannot be counted: AirPlay sends no frames while the screen is still


class Screen:
    """The latest JPEG frame of the phone's screen, from UxPlay.

    By default the server runs UxPlay itself and reads frames from a pipe (a local socket on Windows). With
    `stream` ("host:port") it follows a standalone receiver instead (`python -m hidagent.receiver`), so restarting
    the server does not end the phone's Screen Mirroring session. `pidfile` is where UxPlay's pid is recorded, so a
    server following a receiver can restart that receiver's UxPlay.
    """

    def __init__(self, name: str = "iPhone Agent", fps: int = 8, quality: int = 75,
                 stream: str | None = None, pidfile: Path | None = None):
        if stream and not stream.rpartition(":")[2].isdigit():
            raise ValueError(f"MIRROR_STREAM must be host:port, not {stream!r}")
        self.name, self.fps, self.quality = name, fps, quality
        self.stream, self.pidfile = stream, pidfile
        self.frame: bytes | None = None
        self.updated = 0.0
        self._proc = None
        self._sock = None
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
        """Validate capture dependencies and start an owned, joinable receiver (or follow a standalone one)."""
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("AirPlay receiver is already running")
        if self._stopping.is_set():
            raise RuntimeError("AirPlay startup was cancelled; create a new receiver before restarting")
        if self.stream:
            self._thread = threading.Thread(target=self._follow, name="hidagent-airplay-follow", daemon=True)
            self._thread.start()
            return
        self._preflight()
        if os.name == "nt":
            self._thread = threading.Thread(target=self._supervise_socket, name="hidagent-airplay")
            self._thread.start()
            return
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

    def _preflight(self) -> None:
        """Find UxPlay and the GStreamer elements its pipeline needs, and check the configured ports."""
        self._binary = self._executable("UXPLAY_BIN", "uxplay", alias="UXPLAY")
        self._check_plugins()
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

    def _check_plugins(self) -> None:
        """Fail fast on a missing GStreamer element; skip the check where gst-inspect-1.0 is not installed."""
        if not os.environ.get("GST_INSPECT_BIN") and shutil.which("gst-inspect-1.0") is None:
            # it ships separately (e.g. gstreamer1.0-tools), so its absence says nothing about the plugins
            log.warning("gst-inspect-1.0 not found: not checking GStreamer elements (set GST_INSPECT_BIN to check)")
            return
        inspector = self._executable("GST_INSPECT_BIN", "gst-inspect-1.0")
        sink = "tcpclientsink" if os.name == "nt" else "fdsink"
        for plugin in ("h264parse", "decodebin", "videoconvert", "videorate", "jpegenc", sink):
            if self._stopping.is_set():
                raise RuntimeError("AirPlay startup was cancelled")
            try:
                checked = subprocess.run([inspector, plugin], stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL, timeout=3, check=False)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(f"GStreamer inspection timed out for {plugin}") from exc
            if checked.returncode:
                raise RuntimeError(f"GStreamer element '{plugin}' is missing; repair GStreamer before starting capture")

    @staticmethod
    def _executable(variable: str, fallback: str, alias: str | None = None) -> str:
        configured = os.environ.get(variable)
        if not configured and alias and os.environ.get(alias):  # the older name may be a bare command on PATH
            configured = os.environ[alias]
            if not Path(configured).expanduser().is_absolute():
                found = shutil.which(configured)
                if found is None:
                    raise RuntimeError(f"{configured} is missing; install it or configure {variable}")
                return found
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

    def stop(self, timeout: float = 3) -> None:
        """Stop capture, end UxPlay or the stream connection, and join the reader thread."""
        self.request_stop()
        process = self._proc
        if process is not None and process.poll() is None:
            process.terminate()  # the supervisor reaps it (and kills it if it ignores this)
        sock = self._sock
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)  # wakes the follower's recv(); close() alone does not on Linux
            except OSError:
                pass
            sock.close()  # and on Windows only close() does
        self.frame = None
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
            if thread.is_alive():
                raise RuntimeError(f"AirPlay receiver did not finish shutdown within {timeout:g} seconds")
        self._thread = None

    async def aclose(self) -> None:
        """`stop()` without blocking the API loop."""
        await asyncio.to_thread(self.stop)

    def restart(self) -> None:
        """Restart UxPlay (its supervisor starts a new one); the phone must choose it in Screen Mirroring again."""
        if self._stopping.is_set():
            return
        log.warning("AirPlay receiver: no new frames, restarting it")
        self.frame, self.updated = None, 0.0  # nothing since the restart: the next action must not restart it again
        if self.stream:
            if self._sock is None:  # the receiver is gone, so its pid may belong to an unrelated process by now
                log.warning("no AirPlay receiver is streaming at %s: start `python -m hidagent.receiver`", self.stream)
                return
            try:
                os.kill(int(Path(self.pidfile).read_text()), signal.SIGTERM)
            except (OSError, TypeError, ValueError) as e:
                log.warning("could not restart the standalone receiver: %r", e)
        elif self._proc and self._proc.poll() is None:
            self._proc.terminate()

    def _follow(self) -> None:
        """Read frames from the standalone receiver's TCP stream; reconnect whenever it goes away."""
        host, port = self.stream.rsplit(":", 1)
        waiting = False
        while not self._stopping.is_set():
            try:
                with socket.create_connection((host, int(port)), timeout=2) as sock:
                    sock.settimeout(None)
                    self._sock, waiting = sock, False
                    if self._stopping.is_set():  # stop() ran before this socket was visible to it
                        return
                    log.info("following the AirPlay receiver at %s", self.stream)
                    self._consume(lambda: sock.recv(1 << 16))
            except OSError:
                if not waiting:
                    log.info("waiting for the AirPlay receiver at %s (python -m hidagent.receiver)", self.stream)
                    waiting = True
            finally:
                self._sock, self.frame = None, None
            self._stopping.wait(0.5)

    def _launch(self, sink: str, **popen):
        """Start UxPlay with a pipeline ending in `sink`, unless shutdown has begun."""
        sink = (f"videorate drop-only=true ! video/x-raw,framerate={self.fps}/1 ! "
                f"jpegenc quality={self.quality} ! {sink}")
        binary = self._binary or os.environ.get("UXPLAY_BIN") or os.environ.get("UXPLAY", "uxplay")
        with self._lifecycle_lock:
            if self._stopping.is_set():
                raise RuntimeError("AirPlay startup was cancelled")
            process = subprocess.Popen([binary, "-n", self.name, "-nh", "-a", "-reset", "0",
                                        *self._extra_args, "-vs", sink],
                                       **popen)  # UxPlay logs to stdout: keep them in ours
        self._proc = process
        self._clients_at, self._clients_n = 0.0, None
        self.last_error = None
        if self.pidfile:
            Path(self.pidfile).write_text(str(process.pid))
        log.info("AirPlay receiver '%s' started (pid %d)", self.name, process.pid)
        return process

    def _spawn(self):
        read_fd, write_fd = os.pipe()
        try:
            process = self._launch(f"fdsink fd={write_fd}", pass_fds=(write_fd,))
        except (OSError, RuntimeError):
            os.close(read_fd)
            raise
        finally:
            os.close(write_fd)
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

    def _supervise_socket(self) -> None:
        """Windows: the same restart loop, with frames arriving over a local socket."""
        while not self._stopping.is_set():
            try:
                self._run_with_socket()
            except (OSError, RuntimeError) as exc:
                self.last_error = f"AirPlay receiver failed: {exc}"
                log.error("%s", self.last_error)
            finally:
                if self._proc is not None:
                    self._reap(self._proc)
                self.frame = None
                self._proc = None
            if self._stopping.wait(1):
                return
            log.warning("AirPlay receiver exited, restarting")

    def _run_with_socket(self) -> None:
        """Windows cannot hand UxPlay a pipe, so its pipeline connects back to a local socket instead."""
        with socket.create_server(("127.0.0.1", 0)) as srv:
            srv.settimeout(0.5)
            process = self._launch(f"tcpclientsink host=127.0.0.1 port={srv.getsockname()[1]}")
            while not self._stopping.is_set() and process.poll() is None:  # UxPlay reconnects when it rebuilds
                try:
                    conn, _ = srv.accept()
                except TimeoutError:
                    continue
                with conn:
                    conn.settimeout(None)
                    try:
                        self._consume(lambda c=conn: c.recv(1 << 16))
                    except OSError:
                        pass  # UxPlay went away mid-frame

    def _running(self, proc=None) -> bool:
        if self._stopping.is_set():
            return False
        if self.stream:
            return self._sock is not None
        return proc is not None and proc.poll() is None

    def _countable(self) -> bool:
        """Whether this receiver's connections can be counted: our own UxPlay, on Linux or macOS."""
        return not self.stream and os.name != "nt"

    @property
    def live(self) -> bool:
        proc = self._proc  # read once: the supervisor clears it when UxPlay exits
        if not self._running(proc) or self.frame is None:
            return False
        if not self._countable():  # a followed receiver's process is not ours; Windows has no /proc or lsof
            return time.time() - self.updated < STILL_FOR
        clients = self._clients(proc)
        # A still phone can send no new frames. Require a receiver connection, not an arbitrary frame age.
        return clients is not None and clients > 0

    @property
    def metadata(self) -> dict:
        """Report capture age without claiming that a still frame proves connectivity."""
        proc = self._proc
        running = self._running(proc)
        clients = (self._clients(proc) if running else 0) if self._countable() else None
        age = max(0.0, time.monotonic() - self._updated_monotonic) if self.frame is not None else None
        return {"receiver_running": running, "frame_available": self.frame is not None,
                "frame_age_seconds": round(age, 3) if age is not None else None,
                "connection_verified": clients is not None, "connected_clients": clients,
                "error": self.last_error or self._connection_error}

    def _clients(self, proc) -> int | None:
        """Count `proc`'s TCP connections (a mirroring phone keeps some open); unknown inspection fails closed."""
        if time.monotonic() - self._clients_at < 1:
            return self._clients_n
        self._connection_error = None
        if sys.platform == "darwin":
            try:
                result = subprocess.run(["/usr/sbin/lsof", "-nP", "-a", "-p", str(proc.pid),
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
            fds = os.listdir(f"/proc/{proc.pid}/fd")
            inodes = set()
            for fd in fds:
                link = os.readlink(f"/proc/{proc.pid}/fd/{fd}")
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
        with os.fdopen(fd, "rb", buffering=0) as pipe:
            def read() -> bytes:
                while not self._stopping.is_set():
                    if select.select([pipe], [], [], 0.2)[0]:
                        return pipe.read(1 << 16)
                return b""
            self._consume(read)

    def _consume(self, read) -> None:
        """Split a byte stream into JPEG frames (start/end markers) until `read` returns nothing."""
        buf = b""
        while chunk := read():
            buf += chunk
            if len(buf) > MAX_FRAME:
                self.last_error = "AirPlay JPEG exceeded the 8 MiB frame limit"
                buf = buf[-1:]
            while (start := buf.find(b"\xff\xd8")) >= 0 and (end := buf.find(b"\xff\xd9", start)) >= 0:
                self.frame, self.updated = buf[start:end + 2], time.time()
                self._updated_monotonic = time.monotonic()
                buf = buf[end + 2:]
