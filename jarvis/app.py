"""HTTP + WebSocket server: the bridge between the brain and the HUD.

Security model (the server can control your computer, so it's locked down):
  * binds to 127.0.0.1 by default;
  * Host header must be local (blocks DNS-rebinding);
  * Origin must match the server (blocks other websites from driving JARVIS);
  * every API/WebSocket call needs a per-run access token that is only
    embedded in the HUD page served by this process;
  * API keys never leave the server.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import secrets
import time
from collections import deque
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from jarvis import __version__
from jarvis.api.schemas import ClientMessageAdapter, ErrorEvent, HelloMessage, UserTextMessage
from jarvis.config import Settings, load_settings
from jarvis.core.metrics import Stopwatch
from jarvis.core.orchestrator import Orchestrator
from jarvis.core.services import Services
from jarvis.logging_setup import session_id_var, setup_logging
from jarvis.voice.speech import SpeechError
from jarvis.web.client import WebError
from jarvis.web.weather import Location

log = logging.getLogger(__name__)

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
MAX_AUDIO_BYTES = 15_000_000


class RateLimiter:
    def __init__(self, limit: int, per_s: float) -> None:
        self._limit, self._per = limit, per_s
        self._hits: deque[float] = deque()

    def allow(self) -> bool:
        now = time.monotonic()
        while self._hits and now - self._hits[0] > self._per:
            self._hits.popleft()
        if len(self._hits) >= self._limit:
            return False
        self._hits.append(now)
        return True


class TtsRequest(BaseModel):
    text: str = Field(min_length=1, max_length=1500)


def create_app(settings: Settings | None = None, services: Services | None = None) -> FastAPI:
    settings = settings or load_settings()
    setup_logging(settings.log_level, settings.data_dir / "logs", settings.secret_values())
    token = settings.access_token.get_secret_value() if settings.access_token else secrets.token_urlsafe(24)
    allowed_hosts = _LOCAL_HOSTS | {settings.host}

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
        svc = services or Services(settings)
        app.state.services = svc
        app.state.orchestrator = Orchestrator(svc)
        await svc.boot()
        try:
            yield
        finally:
            await svc.shutdown()

    app = FastAPI(title="J.A.R.V.I.S.", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.token = token

    def host_ok(host_header: str | None) -> bool:
        host = (host_header or "").rsplit(":", 1)[0] if not (host_header or "").startswith("[") else \
            (host_header or "").split("]")[0] + "]"
        return host in allowed_hosts

    def origin_ok(origin: str | None, host_header: str | None) -> bool:
        if not origin:
            return True  # non-browser clients (curl, tests) don't send Origin; the token still applies
        return origin.split("://", 1)[-1] == (host_header or "")

    def token_ok(candidate: str | None) -> bool:
        return bool(candidate) and hmac.compare_digest(candidate or "", token)

    class Guard(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
            if not host_ok(request.headers.get("host")):
                return JSONResponse({"error": "host not allowed"}, status_code=403)
            path = request.url.path
            if path.startswith("/api/") and path != "/api/health":
                if not origin_ok(request.headers.get("origin"), request.headers.get("host")):
                    return JSONResponse({"error": "origin not allowed"}, status_code=403)
                if not token_ok(request.headers.get("x-jarvis-token")):
                    return JSONResponse({"error": "missing or invalid token"}, status_code=401)
            response = await call_next(request)
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "no-referrer"
            return response

    app.add_middleware(Guard)

    def svc(request: Request) -> Services:
        return request.app.state.services  # type: ignore[no-any-return]

    # ── Pages ───────────────────────────────────────────────────────────────
    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        html = (settings.web_dir / "index.html").read_text(encoding="utf-8").replace("{{JARVIS_TOKEN}}", token)
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    app.mount("/static", StaticFiles(directory=settings.web_dir), name="static")

    # ── REST ────────────────────────────────────────────────────────────────
    @app.get("/api/health")
    async def health(request: Request) -> dict[str, Any]:
        return {"status": "ok", "version": __version__, "boot_complete": svc(request).boot_complete}

    @app.get("/api/diagnostics")
    async def diagnostics(request: Request) -> dict[str, Any]:
        return await svc(request).diagnostics()

    @app.get("/api/tools")
    async def tools(request: Request) -> list[dict[str, Any]]:
        services_ = svc(request)
        return services_.registry.describe(services_)

    @app.get("/api/tasks")
    async def tasks(request: Request) -> list[dict[str, Any]]:
        return [t.model_dump(mode="json") for t in svc(request).tasks.recent(30)]

    @app.get("/api/memory")
    async def memory(request: Request) -> list[dict[str, Any]]:
        return [m.to_dict() for m in svc(request).memory.entries(limit=500)]

    @app.delete("/api/memory/{memory_id}")
    async def delete_memory(memory_id: str, request: Request) -> dict[str, Any]:
        if not svc(request).memory.delete(memory_id):
            raise HTTPException(404, "memory not found")
        return {"deleted": memory_id}

    @app.get("/api/weather")
    async def weather(request: Request) -> dict[str, Any]:
        user = settings.user
        if user.latitude is None or user.longitude is None:
            raise HTTPException(404, "set JARVIS_LATITUDE and JARVIS_LONGITUDE to show local weather")
        try:
            return await svc(request).weather.forecast(Location(user.location_name or "Local", user.latitude,
                                                                user.longitude))
        except WebError as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.post("/api/voice/transcribe")
    async def transcribe(request: Request, audio: UploadFile = File(...),  # noqa: B008
                         session_id: str = Form(...)) -> dict[str, Any]:
        services_ = svc(request)
        orchestrator: Orchestrator = request.app.state.orchestrator
        session = services_.sessions.get(session_id)
        if not session:
            raise HTTPException(404, "unknown session — connect the WebSocket first")
        data = await audio.read()
        if not data or len(data) > MAX_AUDIO_BYTES:
            raise HTTPException(413, "audio missing or too large")
        clock = Stopwatch()
        try:
            transcript = await services_.stt.transcribe(data, audio.filename or "speech.wav",
                                                        audio.content_type or "audio/wav")
        except SpeechError as exc:
            log.warning("transcription failed: %s", exc)
            raise HTTPException(502, str(exc)) from exc
        stt_ms = clock.ms
        services_.metrics.record("stt", stt_ms)
        if transcript.text:
            session_id_var.set(session.id)
            await orchestrator.on_user_input(session, transcript.text, "voice", stt_ms=stt_ms)
        return {"text": transcript.text, "stt_ms": round(stt_ms)}

    @app.post("/api/voice/tts")
    async def tts(body: TtsRequest, request: Request) -> Response:
        services_ = svc(request)
        clock = Stopwatch()
        try:
            audio, mime = await services_.tts.synthesize(body.text)
        except SpeechError as exc:
            log.warning("text-to-speech failed: %s", exc)  # surfaces in the console and SYSTEMS → recent errors
            raise HTTPException(502, str(exc)) from exc
        services_.metrics.record("tts", clock.ms)
        return Response(audio, media_type=mime, headers={"Cache-Control": "no-store"})

    # ── WebSocket ───────────────────────────────────────────────────────────
    @app.websocket("/ws")
    async def websocket(ws: WebSocket) -> None:
        if not host_ok(ws.headers.get("host")) or not origin_ok(ws.headers.get("origin"), ws.headers.get("host")) \
                or not token_ok(ws.query_params.get("token")):
            await ws.close(code=4403)
            return
        await ws.accept()
        services_: Services = ws.app.state.services
        orchestrator: Orchestrator = ws.app.state.orchestrator
        try:
            first = ClientMessageAdapter.validate_json(await asyncio.wait_for(ws.receive_text(), timeout=15))
        except (TimeoutError, ValidationError, WebSocketDisconnect):
            await ws.close(code=4400)
            return
        if not isinstance(first, HelloMessage):
            await ws.close(code=4400)
            return
        session = services_.sessions.get_or_create(first.session_id)
        subscription = services_.bus.subscribe(session.id)
        await orchestrator.handle_client_message(session, first)
        await ws.send_text(orchestrator.snapshot(session).model_dump_json())
        log.info("HUD connected (session %s)", session.id)
        limiter = RateLimiter(limit=20, per_s=10)

        async def sender() -> None:
            while True:
                event = await subscription.queue.get()
                await ws.send_text(event.model_dump_json())

        send_task = asyncio.create_task(sender())
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    message = ClientMessageAdapter.validate_json(raw)
                except ValidationError as exc:
                    await ws.send_text(ErrorEvent(session_id=session.id, message="invalid message",
                                                  code="invalid_message").model_dump_json())
                    log.debug("invalid client message: %s", exc.errors()[:1])
                    continue
                if isinstance(message, UserTextMessage) and not limiter.allow():
                    await ws.send_text(ErrorEvent(session_id=session.id, message="Too many requests — slow down.",
                                                  code="rate_limited").model_dump_json())
                    continue
                await orchestrator.handle_client_message(session, message)
        except WebSocketDisconnect:
            pass
        finally:
            send_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await send_task
            services_.bus.unsubscribe(subscription)
            if not services_.bus.has_subscribers(session.id):
                session.mic_active = False
                session.tts_playing = False
            log.info("HUD disconnected (session %s); background tasks continue", session.id)

    return app
