"""Loopback HTTP adapter; Tailscale identity and Bearer authentication are ORed.

Trust the identity header only behind Tailscale Serve, which overwrites it. Tokens
must live in an owner-only regular file. Bodies, credentials and model output are
never logged. Only a completed successful answer becomes an actionable response.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hmac
import json
import math
import os
from pathlib import Path
import queue
import re
import select
import signal
import socket
import stat
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
import uuid

from .appserver import BridgeError, CodexBackend

MAX_BODY = 6 * 1024 * 1024
MAX_IMAGE = 4 * 1024 * 1024
MAX_PROMPT = 32 * 1024
MAX_ANSWER = 1024 * 1024
MAX_IMAGES = 12
CLEANUP_TIMEOUT = 8.0


def read_token(path: Path) -> str:
    """Read a private, current-user-owned token without following symlinks."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("token file must be a current-user-owned regular file with mode 0600")
        if info.st_size > 4096:
            raise ValueError("token file is too large")
        token = os.read(fd, 4097).decode("ascii").strip()
        if not 32 <= len(token) <= 4096 or any(not 33 <= ord(char) <= 126 for char in token):
            raise ValueError("token must contain at least 32 non-whitespace ASCII characters")
        return token
    finally:
        os.close(fd)


def parse_request(body: Any) -> dict[str, Any]:
    """Validate the stateless vision payload emitted by the iOS client."""
    def reject(message: str) -> None:
        raise BridgeError(400, message)

    allowed = {"model", "messages", "stream", "temperature", "max_tokens", "max_completion_tokens", "stream_options"}
    if not isinstance(body, dict) or body.keys() - allowed:
        reject("unsupported request fields")
    model = body.get("model")
    if not isinstance(model, str) or not model.strip() or len(model) > 256 or any(ord(c) < 32 for c in model):
        reject("model must be a nonempty model id")
    if not isinstance(body.get("stream", False), bool):
        reject("stream must be boolean")
    for key in ("max_tokens", "max_completion_tokens"):
        if key in body and (type(body[key]) is not int or not 1 <= body[key] <= 32768):
            reject(f"{key} must be between 1 and 32768")
    if "temperature" in body:
        value = body["temperature"]
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 2:
            reject("temperature must be between 0 and 2")
    options = body.get("stream_options", {})
    if not isinstance(options, dict) or options.keys() - {"include_usage"} or ("include_usage" in options and type(options["include_usage"]) is not bool):
        reject("unsupported stream options")
    messages = body.get("messages")
    if not isinstance(messages, list) or len(messages) != 2:
        reject("exactly one system message and one user message are supported")
    for message, role in zip(messages, ("system", "user")):
        if not isinstance(message, dict) or set(message) != {"role", "content"} or message["role"] != role:
            reject("only system then user roles are supported")
    system, parts = messages[0]["content"], messages[1]["content"]
    if not isinstance(system, str) or not isinstance(parts, list) or not 1 <= len(parts) <= MAX_IMAGES + 1:
        reject("system must be text and user content must be a short parts list")
    texts: list[str] = []
    images: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            reject("invalid content part")
        if part.get("type") == "text" and set(part) == {"type", "text"} and isinstance(part["text"], str):
            texts.append(part["text"])
            continue
        value = part.get("image_url")
        if part.get("type") != "image_url" or set(part) != {"type", "image_url"} or not isinstance(value, dict) or set(value) != {"url"}:
            reject("only text and inline image_url parts are supported")
        url = value["url"]
        if not isinstance(url, str) or len(images) >= MAX_IMAGES:
            reject("at most twelve inline images are supported")
        match = re.fullmatch(r"data:image/(jpeg|png|webp);base64,([A-Za-z0-9+/]*={0,2})", url)
        if not match:
            reject("images must be base64 JPEG, PNG or WebP data URLs; remote URLs are forbidden")
        if len(match[2]) > 4 * ((MAX_IMAGE + 2) // 3):
            reject("decoded image exceeds 4 MiB")
        try:
            data = base64.b64decode(match[2], validate=True)
        except (ValueError, binascii.Error):
            reject("invalid image base64")
        signatures = {"jpeg": data.startswith(b"\xff\xd8\xff"), "png": data.startswith(b"\x89PNG\r\n\x1a\n"), "webp": data.startswith(b"RIFF") and data[8:12] == b"WEBP"}
        if not data or len(data) > MAX_IMAGE or not signatures[match[1]]:
            reject("image content does not match its declared format")
        images.append(url)
    text = "\n".join(texts)
    if not texts or len(system) + len(text) > MAX_PROMPT:
        reject("request requires text and at most 32768 combined prompt characters")
    return {"model": model, "system": system, "text": text, "images": images, "stream": body.get("stream", False)}


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject ambiguous duplicate JSON fields rather than silently using the last."""
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate JSON field")
    return result


class BridgeServer(ThreadingHTTPServer):
    """A private listener with one inference slot and no waiting inference queue."""

    daemon_threads = True

    def __init__(self, port: int, backend: Any, *, tailscale_login: str | None = None, token: str | None = None) -> None:
        if not tailscale_login and not token:
            raise ValueError("configure --tailscale-login or --token-file")
        self.backend, self.tailscale_login, self.token = backend, tailscale_login, token
        self.inference_lock = threading.Lock()
        self.active_cancel: threading.Event | None = None
        self._state_lock = threading.Lock()
        self._closing = False
        self._idle = threading.Event()
        self._idle.set()
        super().__init__(("127.0.0.1", port), BridgeHandler)

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Suppress traceback/request logging; HTTP errors are already sanitized."""

    def begin_job(self) -> threading.Event:
        """Atomically reject closing/busy requests or reserve the single backend slot."""
        with self._state_lock:
            if self._closing:
                raise BridgeError(503, "bridge is shutting down")
            if not self.inference_lock.acquire(blocking=False):
                raise BridgeError(429, "another inference is running; retry shortly")
            self.active_cancel = threading.Event()
            self._idle.clear()
            return self.active_cancel

    def finish_job(self) -> None:
        """Release the slot only after backend/generator cleanup has completed."""
        with self._state_lock:
            self.active_cancel = None
            self.inference_lock.release()
            self._idle.set()

    def _cancel_jobs(self) -> None:
        with self._state_lock:
            self._closing = True
            if self.active_cancel is not None:
                self.active_cancel.set()

    def shutdown(self) -> None:
        """Stop admitting jobs and cancel active work before stopping the listener."""
        self._cancel_jobs()
        super().shutdown()

    def server_close(self) -> None:
        """Wait for bounded Codex process cleanup instead of orphaning daemon jobs."""
        self._cancel_jobs()
        super().server_close()
        if not self._idle.wait(CLEANUP_TIMEOUT):
            raise RuntimeError("backend did not finish cancellation cleanup within 8 seconds")


class BridgeHandler(BaseHTTPRequestHandler):
    """Serve the minimal iOS-compatible API without recording private input."""

    server: BridgeServer
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, format: str, *args: Any) -> None:
        """Disable access logging, including URL, credential and payload logging."""

    def _json(self, status: int, value: Any) -> None:
        encoded = json.dumps(value, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        if status == 429:
            self.send_header("Retry-After", "1")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(encoded)

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        """Keep parser/method failures in the same non-reflective JSON format."""
        self._json(code, {"error": {"message": "unsupported HTTP request"}})

    def _guard(self) -> None:
        if self.headers.get_all("Origin"):
            raise BridgeError(403, "browser origins are not accepted")
        login = self.headers.get_all("Tailscale-User-Login", [])
        auth = self.headers.get_all("Authorization", [])
        identity_ok = bool(self.server.tailscale_login and login == [self.server.tailscale_login])
        token_ok = bool(self.server.token and len(auth) == 1 and hmac.compare_digest(auth[0].encode(), ("Bearer " + self.server.token).encode()))
        if not identity_ok and not token_ok:
            raise BridgeError(401, "authentication required")

    def _length(self, required: bool = False) -> int:
        if self.headers.get_all("Transfer-Encoding"):
            raise BridgeError(400, "transfer encoding is not supported")
        lengths = self.headers.get_all("Content-Length", [])
        if not lengths and not required:
            return 0
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,10}", lengths[0]):
            raise BridgeError(400, "one valid Content-Length is required")
        length = int(lengths[0])
        if length > MAX_BODY:
            raise BridgeError(413, "request exceeds 6 MiB")
        return length

    def do_GET(self) -> None:
        """Return anonymous liveness or the authenticated model catalog."""
        try:
            if self.path not in ("/", "/health", "/v1/models"):
                raise BridgeError(404, "unknown endpoint")
            if self._length():
                raise BridgeError(400, "GET bodies are not supported")
            if self.path == "/v1/models":
                self._guard()
                self._json(200, {"object": "list", "data": self._catalog()})
            else:
                self._json(200, {"status": "ok"})
        except BridgeError as exc:
            self._json(exc.status, {"error": {"message": exc.public_message}})
        except (OSError, ValueError):
            self.close_connection = True
        except Exception:
            self._json(502, {"error": {"message": "model catalog unavailable"}})

    def _check_disconnect(self) -> None:
        if select.select([self.connection], [], [], 0)[0] and not self.connection.recv(1, socket.MSG_PEEK):
            raise BrokenPipeError("client disconnected")

    def _catalog(self) -> list[dict[str, Any]]:
        """Monitor catalog discovery so abandoned clients promptly cancel its job."""
        cancel = self.server.begin_job()
        result: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

        def discover() -> None:
            try:
                result.put((True, self.server.backend.models(cancel=cancel)))
            except Exception as exc:
                result.put((False, exc))
            finally:
                self.server.finish_job()

        worker = threading.Thread(target=discover, daemon=True)
        try:
            worker.start()
        except BaseException:
            self.server.finish_job()
            raise
        try:
            while not cancel.is_set():
                try:
                    success, value = result.get(timeout=0.25)
                except queue.Empty:
                    self._check_disconnect()
                    continue
                if not success:
                    raise value
                if not cancel.is_set():
                    return value
            raise BridgeError(503, "bridge is shutting down")
        finally:
            cancel.set()
            worker.join(timeout=1)

    def do_POST(self) -> None:
        """Validate and execute one inference without queueing concurrent work."""
        try:
            if self.path != "/v1/chat/completions":
                raise BridgeError(404, "unknown endpoint")
            self._guard()
            length = self._length(required=True)
            if self.headers.get_content_type() != "application/json" or len(self.headers.get_all("Content-Type", [])) != 1:
                raise BridgeError(415, "Content-Type must be application/json")
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise BridgeError(400, "incomplete request body")
            try:
                body = json.loads(raw, object_pairs_hook=_json_object, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            except (ValueError, UnicodeError, RecursionError):
                raise BridgeError(400, "invalid JSON") from None
            request = parse_request(body)
            cancel = self.server.begin_job()
            self._complete(request, cancel)
        except BridgeError as exc:
            self._failure(exc)
        except (OSError, TimeoutError):
            self.close_connection = True
        except Exception:
            self._failure(BridgeError(502, "inference failed"))

    def _failure(self, error: BridgeError) -> None:
        if getattr(self, "_stream_started", False):
            self._event({"error": {"message": error.public_message}})
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        else:
            self._json(error.status, {"error": {"message": error.public_message}})

    def _complete(self, request: dict[str, Any], cancel: threading.Event) -> None:
        events: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=4)

        def deliver(kind: str, value: Any) -> None:
            while not cancel.is_set():
                try:
                    events.put((kind, value), timeout=0.2)
                    return
                except queue.Full:
                    continue

        def produce() -> None:
            iterator = None
            try:
                iterator = self.server.backend.stream(**{key: request[key] for key in ("model", "system", "text", "images")}, cancel=cancel)
                for item in iterator:
                    if cancel.is_set():
                        break
                    if item is not None:
                        deliver("text", item)
                deliver("done", None)
            except BridgeError as exc:
                deliver("error", exc)
            except Exception:
                deliver("error", BridgeError(502, "inference failed"))
            finally:
                try:
                    if iterator is not None and hasattr(iterator, "close"):
                        iterator.close()
                finally:
                    self.server.finish_job()

        worker = threading.Thread(target=produce, daemon=True)
        try:
            worker.start()
        except BaseException:
            self.server.finish_job()
            raise
        stream = request["stream"]
        try:
            if stream:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                self.close_connection = True
                self._stream_started = True
            chunks: list[str] = []
            size = 0
            while True:
                if cancel.is_set():
                    raise BridgeError(503, "bridge is shutting down")
                try:
                    kind, value = events.get(timeout=1)
                except queue.Empty:
                    self._check_disconnect()
                    if stream:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                    continue
                if kind == "error":
                    if not stream:
                        raise value
                    self._event({"error": {"message": value.public_message}})
                    break
                if kind == "done":
                    answer = "".join(chunks)
                    if not answer.strip():
                        if not stream:
                            raise BridgeError(502, "model returned no answer")
                        self._event({"error": {"message": "model returned no answer"}})
                        break
                    completion = {"id": "chatcmpl-" + uuid.uuid4().hex, "created": int(time.time()), "model": request["model"], "object": "chat.completion.chunk" if stream else "chat.completion", "choices": [{"index": 0, "finish_reason": "stop", "delta" if stream else "message": {"role": "assistant", "content": answer}}]}
                    if stream:
                        self._event(completion)
                    else:
                        self._json(200, completion)
                    break
                if not isinstance(value, str) or size + len(value) > MAX_ANSWER:
                    raise BridgeError(502, "model answer exceeded output limit")
                chunks.append(value)
                size += len(value)
            if stream:
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
        finally:
            cancel.set()
            worker.join(timeout=1)

    def _event(self, value: Any) -> None:
        self.wfile.write(b"data: " + json.dumps(value).encode() + b"\n\n")
        self.wfile.flush()


def main() -> None:
    """Start the private bridge; SIGINT/SIGTERM cancel the active operation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--tailscale-login")
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--codex-binary", default="codex")
    parser.add_argument("--timeout", type=float, default=75)
    parser.add_argument("--workspace", type=Path)
    args = parser.parse_args()
    if not args.tailscale_login and not args.token_file:
        parser.error("--tailscale-login or --token-file is required")
    if not 1 <= args.port <= 65535 or not math.isfinite(args.timeout) or not 1 <= args.timeout <= 600:
        parser.error("port must be 1..65535 and timeout must be 1..600 seconds")
    if args.tailscale_login and (len(args.tailscale_login) > 256 or any(c.isspace() or ord(c) < 32 for c in args.tailscale_login)):
        parser.error("tailscale login must be an exact non-whitespace identity")
    try:
        token = read_token(args.token_file) if args.token_file else None
        backend = CodexBackend(codex_binary=args.codex_binary, timeout=args.timeout, workspace=args.workspace)
        server = BridgeServer(args.port, backend, tailscale_login=args.tailscale_login, token=token)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    def stop(signum: int, frame: Any) -> None:
        if server.active_cancel is not None:
            server.active_cancel.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print(f"Codex bridge listening on 127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
