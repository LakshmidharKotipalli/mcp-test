"""Chrome launch with a remote debugging port, plus a raw CDP client for live frames.

* ``BrowserProcess`` starts Chrome/Chromium with ``--remote-debugging-port`` on
  loopback. Playwright MCP is pointed at it with ``--cdp-endpoint``.
* ``CDPScreencaster`` opens a *second*, independent CDP connection to the same
  browser and uses ``Page.startScreencast`` to stream JPEG frames. It only reads,
  so it does not interfere with the MCP session.
* The WebSocket client is a minimal built-in implementation (RFC 6455, client side)
  so no extra dependency is needed.
"""
from __future__ import annotations

import asyncio
import base64
import glob
import json
import os
import shutil
import socket
import struct
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

from ..events import EventBus, Frame
from ..models import LiveConfig
from .frames import FrameSnapshot


class LiveUnavailable(Exception):
    """CDP live view cannot be used; callers fall back to screenshot mode."""


# --------------------------------------------------------------- websocket
class MiniWebSocket:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._r, self._w = reader, writer

    @classmethod
    async def connect(cls, url: str, timeout: float = 10) -> "MiniWebSocket":
        u = urlparse(url)
        host, port = u.hostname or "127.0.0.1", u.port or 80
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, limit=2 ** 25), timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        writer.write((f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
                      f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                      f"Sec-WebSocket-Version: 13\r\n\r\n").encode())
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout)
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            writer.close()
            raise LiveUnavailable(f"websocket upgrade refused: {head[:80]!r}")
        return cls(reader, writer)

    async def _send_frame(self, opcode: int, payload: bytes) -> None:
        n = len(payload)
        header = bytes([0x80 | opcode])
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload)) if n < 4096 else \
            (int.from_bytes(payload, "big") ^ int.from_bytes((mask * (n // 4 + 1))[:n], "big")
             ).to_bytes(n, "big")
        self._w.write(header + mask + masked)
        await self._w.drain()

    async def send(self, text: str) -> None:
        await self._send_frame(0x1, text.encode())

    async def recv(self) -> str | None:
        """Next text message, or None when the connection closed."""
        buf = b""
        while True:
            try:
                b1, b2 = await self._r.readexactly(2)
                n = b2 & 0x7F
                if n == 126:
                    n = struct.unpack(">H", await self._r.readexactly(2))[0]
                elif n == 127:
                    n = struct.unpack(">Q", await self._r.readexactly(8))[0]
                payload = await self._r.readexactly(n)  # servers do not mask
            except (asyncio.IncompleteReadError, ConnectionError):
                return None
            opcode, fin = b1 & 0x0F, bool(b1 & 0x80)
            if opcode == 0x8:
                return None
            if opcode == 0x9:
                await self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            buf += payload
            if fin:
                return buf.decode()

    async def close(self) -> None:
        try:
            await self._send_frame(0x8, b"")
        except Exception:  # noqa: BLE001
            pass
        self._w.close()


class CDPConnection:
    """Request/response + event dispatch over one CDP websocket."""

    def __init__(self, ws: MiniWebSocket) -> None:
        self._ws = ws
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._handlers: list[Callable[[str, dict, str | None], None]] = []
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._read_loop())

    def on_event(self, cb: Callable[[str, dict, str | None], None]) -> None:
        self._handlers.append(cb)

    async def _read_loop(self) -> None:
        while (raw := await self._ws.recv()) is not None:
            msg = json.loads(raw)
            if "id" in msg:
                fut = self._pending.pop(msg["id"], None)
                if fut and not fut.done():
                    if "error" in msg:
                        fut.set_exception(LiveUnavailable(str(msg["error"])))
                    else:
                        fut.set_result(msg.get("result", {}))
            elif "method" in msg:
                for cb in self._handlers:
                    cb(msg["method"], msg.get("params", {}), msg.get("sessionId"))
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(LiveUnavailable("CDP connection closed"))

    async def send(self, method: str, params: dict | None = None,
                   session_id: str | None = None, timeout: float = 10) -> dict:
        self._id += 1
        msg: dict[str, Any] = {"id": self._id, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        fut = asyncio.get_running_loop().create_future()
        self._pending[self._id] = fut
        await self._ws.send(json.dumps(msg))
        return await asyncio.wait_for(fut, timeout)

    async def close(self) -> None:
        await self._ws.close()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)


# ------------------------------------------------------------ browser launch
def find_browser(explicit: str = "") -> str | None:
    """Locate a Chrome/Chromium executable (explicit path, Playwright cache, then PATH)."""
    for cand in (explicit, os.environ.get("BROWSER_PATH", "")):
        if cand and Path(cand).is_file():
            return cand
    roots = [os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ""),
             str(Path.home() / ".cache/ms-playwright"),
             str(Path.home() / "Library/Caches/ms-playwright"),
             os.path.join(os.environ.get("LOCALAPPDATA", ""), "ms-playwright")]
    patterns = ["chromium-*/chrome-linux*/chrome",
                "chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium",
                "chromium-*/chrome-win*/chrome.exe"]
    for root in filter(None, roots):
        for pat in patterns:
            hits = sorted(glob.glob(os.path.join(root, pat)), reverse=True)
            if hits:
                return hits[0]
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
                 "chrome", "msedge"):
        if found := shutil.which(name):
            return found
    return None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class BrowserProcess:
    """Chrome with a loopback remote-debugging port and a throwaway profile."""

    def __init__(self, executable: str, headless: bool) -> None:
        self.executable, self.headless = executable, headless
        self.endpoint = ""
        self._proc: asyncio.subprocess.Process | None = None
        self._profile = ""

    async def __aenter__(self) -> "BrowserProcess":
        port = _free_port()
        self._profile = tempfile.mkdtemp(prefix="llm-web-tester-")
        args = [f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1",
                f"--user-data-dir={self._profile}", "--no-first-run",
                "--no-default-browser-check", "--window-size=1280,800"]
        if self.headless:
            args.append("--headless=new")
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            args.append("--no-sandbox")  # Chrome refuses to run as root otherwise
        args.append("about:blank")
        self._proc = await asyncio.create_subprocess_exec(
            self.executable, *args, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL)
        self.endpoint = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 20
        async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
            while time.monotonic() < deadline:
                if self._proc.returncode is not None:
                    break
                try:
                    (await client.get(f"{self.endpoint}/json/version")).raise_for_status()
                    return self
                except httpx.HTTPError:
                    await asyncio.sleep(0.2)
        await self.__aexit__()
        raise LiveUnavailable("browser did not expose a debugging endpoint")

    async def __aexit__(self, *exc: Any) -> None:
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), 5)
            except asyncio.TimeoutError:
                self._proc.kill()
                await self._proc.wait()
        shutil.rmtree(self._profile, ignore_errors=True)


# ----------------------------------------------------------------- screencast
class CDPScreencaster:
    """Streams JPEG frames from every page of the browser onto the event bus."""

    def __init__(self, endpoint: str, bus: EventBus, cfg: LiveConfig) -> None:
        self.endpoint, self.bus, self.cfg = endpoint, bus, cfg
        self._conn: CDPConnection | None = None
        self._targets: dict[str, dict] = {}       # targetId -> {url,title,session}
        self._sessions: dict[str, str] = {}       # cdp session id -> targetId
        self._last_publish = 0.0
        self._last_session: str | None = None     # session that produced the newest frame
        self._viewport: list[float] = []
        self._enabled: set[str] = set()
        self._tasks: set[asyncio.Task] = set()
        self.latest: FrameSnapshot | None = None

    async def __aenter__(self) -> "CDPScreencaster":
        try:
            async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
                info = (await client.get(f"{self.endpoint}/json/version")).json()
            ws = await MiniWebSocket.connect(info["webSocketDebuggerUrl"])
        except (httpx.HTTPError, KeyError, OSError, asyncio.TimeoutError, LiveUnavailable) as exc:
            raise LiveUnavailable(f"cannot attach to browser: {exc}") from exc
        self._conn = CDPConnection(ws)
        self._conn.on_event(self._on_event)
        self._conn.start()
        await self._conn.send("Target.setDiscoverTargets", {"discover": True})
        return self

    async def __aexit__(self, *exc: Any) -> None:
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._conn:
            await self._conn.close()

    # --- FrameSource
    async def current(self) -> FrameSnapshot | None:
        await asyncio.sleep(0.2)   # let the page settle so the frame reflects the action
        return self.latest

    # --- Highlighter
    async def locate(self, role: str, name: str) -> dict | None:
        """Bounding box of the element with this role and accessible name (best effort)."""
        sid = self._last_session
        if not (self._conn and sid):
            return None
        try:
            if sid not in self._enabled:
                await self._conn.send("DOM.enable", session_id=sid)
                await self._conn.send("Accessibility.enable", session_id=sid)
                self._enabled.add(sid)
            root = (await self._conn.send("DOM.getDocument", {"depth": 0},
                                          session_id=sid, timeout=2))["root"]["nodeId"]
            params: dict[str, Any] = {"nodeId": root, "role": role}
            if name:
                params["accessibleName"] = name
            nodes = (await self._conn.send("Accessibility.queryAXTree", params,
                                           session_id=sid, timeout=2)).get("nodes", [])
            for node in nodes:
                backend = node.get("backendDOMNodeId")
                if backend is None:
                    continue
                quad = (await self._conn.send("DOM.getBoxModel", {"backendNodeId": backend},
                                              session_id=sid, timeout=2))["model"]["border"]
                xs, ys = quad[0::2], quad[1::2]
                return {"rect": [min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)],
                        "viewport": self._viewport}
        except Exception:  # noqa: BLE001 - highlighting is best effort
            return None
        return None

    # --- internals
    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _on_event(self, method: str, params: dict, sid: str | None) -> None:
        if method in ("Target.targetCreated", "Target.targetInfoChanged"):
            info = params["targetInfo"]
            if info.get("type") != "page":
                return
            tid = info["targetId"]
            known = self._targets.setdefault(tid, {"session": None})
            known.update(url=info.get("url", ""), title=info.get("title", ""))
            if known["session"] is None and not known.get("attaching"):
                known["attaching"] = True
                self._spawn(self._attach(tid))
        elif method == "Target.targetDestroyed":
            self._targets.pop(params.get("targetId", ""), None)
        elif method == "Page.screencastFrame" and sid:
            self._on_frame(params, sid)

    async def _attach(self, tid: str) -> None:
        assert self._conn
        try:
            sid = (await self._conn.send("Target.attachToTarget",
                                         {"targetId": tid, "flatten": True}))["sessionId"]
            self._sessions[sid] = tid
            self._targets[tid]["session"] = sid
            await self._conn.send("Page.enable", session_id=sid)
            await self._conn.send("Page.startScreencast", {
                "format": "jpeg", "quality": self.cfg.quality,
                "maxWidth": self.cfg.max_width, "maxHeight": self.cfg.max_height,
                "everyNthFrame": 1}, session_id=sid)
        except Exception as exc:  # noqa: BLE001
            self._targets.get(tid, {}).pop("attaching", None)
            print(f"[live] could not attach to page: {exc}", file=sys.stderr)

    def _on_frame(self, params: dict, sid: str) -> None:
        assert self._conn
        # Chrome stops sending frames until each one is acknowledged.
        self._spawn(self._conn.send("Page.screencastFrameAck",
                                    {"sessionId": params["sessionId"]}, session_id=sid))
        raw = base64.b64decode(params["data"])
        meta = params.get("metadata", {})
        info = self._targets.get(self._sessions.get(sid, ""), {})
        self._last_session = sid
        self._viewport = [meta.get("deviceWidth", 0), meta.get("deviceHeight", 0)]
        from ..util import image_size
        w, h = image_size(raw)
        self.latest = FrameSnapshot(raw, "image/jpeg", w, h, info.get("url", ""),
                                    info.get("title", ""))
        now = time.monotonic()
        if now - self._last_publish < 1.0 / max(self.cfg.fps, 0.1):
            return                                  # throttle to the configured fps
        if len(raw) > self.cfg.max_frame_kb * 1024:
            return                                  # cap frame size
        self._last_publish = now
        self.bus.emit(Frame(data=params["data"], width=w, height=h, url=info.get("url", ""),
                            title=info.get("title", ""), viewport=self._viewport))
