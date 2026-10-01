"""Local live-viewer server (Starlette + uvicorn).

* ``GET /``                 the static viewer page
* ``GET /events``           Server-Sent Events: history first, then live events
* ``POST /control/<action>`` pause | resume | step | stop (live) or play/pause/speed/restart (replay)
* ``GET /files/<path>``     saved screenshots (jpg/png only) from the run directory

Safety: binds to loopback unless a host is given explicitly; every data/control
request needs the per-run random token; the Host header must be a loopback name
(blocks DNS rebinding) and cross-origin POSTs are rejected.
"""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import secrets
import socket
from pathlib import Path
from typing import Any, Protocol

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from ..control import RunControl
from ..events import Event, EventBus

VIEWER_HTML = Path(__file__).parent / "static" / "viewer.html"
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}
KEEPALIVE_S = 15
HISTORY_LIMIT = 50_000


def is_loopback(host: str) -> bool:
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class ControlHandler(Protocol):
    async def handle(self, action: str, payload: dict[str, Any]) -> dict[str, Any]: ...


class LiveControlHandler:
    """Maps viewer buttons onto the RunControl gate the agent loop waits on."""

    def __init__(self, control: RunControl) -> None:
        self.control = control

    async def handle(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        fn = {"pause": self.control.pause, "resume": self.control.resume,
              "step": self.control.step, "stop": self.control.stop}.get(action)
        if fn is None:
            raise KeyError(action)
        fn()
        return {"ok": True, "state": self.control.state}


class _Server(uvicorn.Server):
    """uvicorn without signal handling, so Ctrl+C still reaches the test run."""

    def install_signal_handlers(self) -> None:  # older uvicorn
        return None

    @contextlib.contextmanager
    def capture_signals(self):  # newer uvicorn
        yield


class LiveServer:
    def __init__(self, bus: EventBus, handler: ControlHandler, run_dir: Path, host: str = "127.0.0.1",
                 port: int = 0, mode: str = "live", run_id: str = "") -> None:
        self.bus, self.handler, self.run_dir = bus, handler, run_dir.resolve()
        self.host, self.port, self.mode, self.run_id = host, port, mode, run_id
        self.token = secrets.token_urlsafe(16)
        self.history: list[str] = []          # JSON of every non-frame event (late joiners)
        self.last_frame: str | None = None
        self.client_connected = asyncio.Event()
        self._clients: list = []              # active viewer subscriptions
        self._server: _Server | None = None
        self._task: asyncio.Task | None = None
        self._sock: socket.socket | None = None
        bus.tap(self._record)

    # --- lifecycle
    @property
    def url(self) -> str:
        shown = "127.0.0.1" if self.host in ("0.0.0.0", "::") else self.host
        shown = f"[{shown}]" if ":" in shown else shown
        return f"http://{shown}:{self.port}/?token={self.token}"

    async def start(self) -> str:
        family = socket.AF_INET6 if ":" in self.host else socket.AF_INET
        self._sock = socket.socket(family, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, self.port))
        self.port = self._sock.getsockname()[1]
        config = uvicorn.Config(self._app(), log_level="warning", lifespan="off",
                                timeout_graceful_shutdown=2)
        self._server = _Server(config)
        self._task = asyncio.create_task(self._server.serve(sockets=[self._sock]))
        for _ in range(100):
            if self._server.started or self._task.done():
                break
            await asyncio.sleep(0.05)
        if self._task.done():
            self._task.result()
        return self.url

    async def stop(self, drain_s: float = 3.0) -> None:
        """Let connected viewers receive the last events, then shut down."""
        deadline = asyncio.get_running_loop().time() + drain_s
        while self._clients and asyncio.get_running_loop().time() < deadline \
                and any(s.pending for s in self._clients):
            await asyncio.sleep(0.1)
        for sub in list(self._clients):
            sub.close()                    # ends each SSE stream cleanly (no forced cancellation)
        if self._server:
            self._server.should_exit = True
        if self._task:
            await asyncio.gather(self._task, return_exceptions=True)

    # --- history
    def _record(self, ev: Event) -> None:
        if ev.type == "frame":
            self.last_frame = ev.model_dump_json()
            return
        if ev.type == "reset":
            self.history.clear()
            self.last_frame = None
        self.history.append(ev.model_dump_json())
        if len(self.history) > HISTORY_LIMIT:
            del self.history[: len(self.history) - HISTORY_LIMIT]

    def reset(self) -> None:
        """Replay restart: forget history (a Reset event is emitted by the caller)."""
        self.history.clear()
        self.last_frame = None

    # --- request guards
    def _check(self, request: Request, need_token: bool = True) -> Response | None:
        raw = request.headers.get("host", "")
        hostname = raw[1:raw.index("]")] if raw.startswith("[") else raw.rsplit(":", 1)[0]
        if is_loopback(self.host) and hostname not in {"127.0.0.1", "localhost", "::1"}:
            return Response("bad host", status_code=403)   # DNS-rebinding guard
        if need_token:
            token = request.query_params.get("token") or request.headers.get("x-live-token", "")
            if not secrets.compare_digest(token, self.token):
                return Response("forbidden", status_code=403)
        if request.method == "POST":
            origin = request.headers.get("origin")
            if origin and origin.split("://", 1)[-1] != request.headers.get("host"):
                return Response("cross-origin request refused", status_code=403)
        return None

    # --- routes
    def _app(self) -> Starlette:
        server = self

        async def index(request: Request) -> Response:
            if (bad := server._check(request, need_token=False)):
                return bad
            return HTMLResponse(VIEWER_HTML.read_text(),
                                headers={"Cache-Control": "no-store",
                                         "Content-Security-Policy": "default-src 'self' 'unsafe-inline' data: blob:"})

        async def events(request: Request) -> Response:
            if (bad := server._check(request)):
                return bad
            hist, frame = list(server.history), server.last_frame
            sub = server.bus.subscribe("viewer", wants_frames=True)   # no await since snapshot
            server._clients.append(sub)
            server.client_connected.set()

            async def gen():
                try:
                    hello = {"type": "hello", "mode": server.mode, "run_id": server.run_id}
                    yield f"data: {json.dumps(hello)}\n\n"
                    for line in hist:
                        yield f"data: {line}\n\n"
                    if frame:
                        yield f"data: {frame}\n\n"
                    while not (server._server and server._server.should_exit):
                        try:
                            ev = await asyncio.wait_for(sub.get(), KEEPALIVE_S)
                        except asyncio.TimeoutError:
                            yield ": keepalive\n\n"
                            continue
                        if ev is None:
                            break
                        yield f"data: {ev.model_dump_json()}\n\n"
                finally:
                    server._clients.remove(sub)
                    server.bus.unsubscribe(sub)

            return StreamingResponse(gen(), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

        async def control(request: Request) -> Response:
            if (bad := server._check(request)):
                return bad
            try:
                payload = await request.json()
            except Exception:  # noqa: BLE001
                payload = {}
            try:
                result = await server.handler.handle(request.path_params["action"], payload or {})
            except KeyError:
                return JSONResponse({"ok": False, "error": "unknown action"}, status_code=404)
            return JSONResponse(result)

        async def files(request: Request) -> Response:
            if (bad := server._check(request)):
                return bad
            target = (server.run_dir / request.path_params["path"]).resolve()
            if (server.run_dir not in target.parents or target.suffix.lower() not in IMAGE_SUFFIXES
                    or not target.is_file()):
                return Response("not found", status_code=404)
            return FileResponse(target)

        return Starlette(routes=[
            Route("/", index),
            Route("/events", events),
            Route("/control/{action}", control, methods=["POST"]),
            Route("/files/{path:path}", files),
        ])
