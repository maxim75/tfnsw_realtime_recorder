"""FastAPI app: control page, live map, status/settings/live APIs, health check."""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from tfnsw_trip_planner import TripPlannerClient

from .config import EnvConfig, SettingsStore
from .feeds import DAILY_REQUEST_QUOTA, KNOWN_FEEDS, feed_version
from .history import History
from .live import LiveHub
from .recorder import FeedStatus, Recorder
from .uploader import Uploader, make_s3_client
from .writer import recover_open_files

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

SSE_POLL_S = 1.0  # how often an open stream checks the hub for new batches
SSE_HEARTBEAT_S = 15.0  # keeps proxies (Coolify's Traefik) from closing idle streams


@dataclass
class Components:
    env: EnvConfig
    settings: SettingsStore
    recorder: Any
    uploader: Any
    live: LiveHub = field(default_factory=LiveHub)
    history: History | None = None


def build_components(env: EnvConfig) -> Components:
    env.data_dir.mkdir(parents=True, exist_ok=True)
    recovered = recover_open_files(env.data_dir)
    if recovered:
        log.info("Recovered %d unfinished files from a previous run", len(recovered))
    settings = SettingsStore(env.data_dir / "settings.json")
    s3 = env.s3
    s3_client = make_s3_client(s3) if s3 else None
    uploader = Uploader(
        s3_client,
        s3.bucket if s3 else None,
        s3.prefix if s3 else "",
        env.data_dir,
    )
    # 8 s timeout (not the library's 30 s) so an in-flight request finishes within the recorder's 10 s shutdown join.
    live = LiveHub()
    recorder = Recorder(
        TripPlannerClient(api_key=env.tfnsw_api_key, timeout=8), settings, env.data_dir, uploader.enqueue,
        on_positions=live.publish,
    )
    history = History(s3_client, s3.bucket if s3 else None, s3.prefix if s3 else "", env.data_dir)
    return Components(env=env, settings=settings, recorder=recorder, uploader=uploader, live=live, history=history)


def create_app(env: EnvConfig | None = None, components: Components | None = None) -> FastAPI:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    env = env or EnvConfig.from_env()
    comps = components or build_components(env)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        comps.uploader.start()
        try:
            comps.recorder.start()
            yield
        finally:
            comps.live.close()
            log.info("Shutting down: closing open files and uploading")
            try:
                comps.recorder.stop(timeout=10)
            finally:
                comps.uploader.stop(timeout=15)

    app = FastAPI(
        title="TfNSW vehicle position recorder",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    security = HTTPBasic()

    def require_auth(creds: HTTPBasicCredentials = Depends(security)) -> None:
        user_ok = secrets.compare_digest(creds.username.encode(), env.admin_user.encode())
        pass_ok = secrets.compare_digest(creds.password.encode(), env.admin_password.encode())
        if not (user_ok and pass_ok):
            raise HTTPException(401, "Invalid credentials", headers={"WWW-Authenticate": "Basic"})

    @app.get("/healthz", response_class=PlainTextResponse)
    def healthz() -> PlainTextResponse:
        ok = comps.recorder.alive and comps.uploader.alive
        return PlainTextResponse("ok" if ok else "unhealthy", status_code=200 if ok else 500)

    @app.get("/", dependencies=[Depends(require_auth)])
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/map", dependencies=[Depends(require_auth)])
    def live_map() -> FileResponse:
        return FileResponse(STATIC_DIR / "map.html")

    @app.get("/api/live", dependencies=[Depends(require_auth)])
    def live_snapshot() -> dict:
        return comps.live.snapshot()

    @app.get("/api/history/window", dependencies=[Depends(require_auth)])
    def history_window(start: int) -> Response:
        """Recorded reports of the 10-minute window starting at *start* (epoch s), gzip JSON."""
        if comps.history is None:
            raise HTTPException(503, "Playback is not available")
        try:
            body = comps.history.window(start)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:
            log.exception("Reading recorded window %s failed", start)
            raise HTTPException(502, "Could not read recorded data") from exc
        return Response(body, media_type="application/json",
                        headers={"Content-Encoding": "gzip", "Cache-Control": "no-store"})

    @app.get("/api/live/stream", dependencies=[Depends(require_auth)])
    async def live_stream(
        request: Request, since: int | None = None, last_event_id: str | None = Header(None)
    ) -> StreamingResponse:
        """Server-Sent Events: one event per batch of new reports, id = batch seq."""
        if last_event_id and last_event_id.isdigit():
            since = int(last_event_id)  # an EventSource reconnect resumes where it stopped
        hub = comps.live

        async def events():
            seq = hub.seq if since is None else since
            if seq > hub.seq:
                seq = 0  # id from before a server restart: resend the whole buffer
            idle = 0.0
            yield "retry: 5000\n\n"
            while True:
                for batch in hub.batches_after(seq):
                    seq = batch["seq"]
                    idle = 0.0
                    yield f"id: {seq}\ndata: {json.dumps(batch, separators=(',', ':'))}\n\n"
                if hub.closed or await request.is_disconnected():
                    return
                await asyncio.sleep(SSE_POLL_S)
                idle += SSE_POLL_S
                if idle >= SSE_HEARTBEAT_S:
                    idle = 0.0
                    yield ": ping\n\n"

        return StreamingResponse(
            events(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/status", dependencies=[Depends(require_auth)])
    def status() -> dict:
        settings = comps.settings.get()
        feed_status = comps.recorder.status()
        feeds = []
        for feed in KNOWN_FEEDS:
            st = feed_status.get(feed)
            feeds.append({
                "feed": feed,
                "enabled": feed in settings.feeds,
                "version": feed_version(feed),
                **asdict(st or FeedStatus()),
            })
        return {
            "recorder": {"alive": comps.recorder.alive, "started_at": comps.recorder.started_at},
            "settings": settings.to_dict(),
            "quota": {
                "requests_per_day": round(len(settings.feeds) * 86400 / settings.interval),
                "daily_quota": DAILY_REQUEST_QUOTA,
            },
            "feeds": feeds,
            "uploader": comps.uploader.status(),
        }

    @app.put("/api/settings", dependencies=[Depends(require_auth)])
    def update_settings(payload: Any = Body(...)) -> dict:
        try:
            return comps.settings.update(payload).to_dict()
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    return app
