"""FastAPI app: control page, status API, settings API, health check."""
from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from tfnsw_trip_planner import TripPlannerClient

from .config import EnvConfig, SettingsStore
from .feeds import DAILY_REQUEST_QUOTA, KNOWN_FEEDS
from .recorder import Recorder
from .uploader import Uploader, make_s3_client
from .writer import recover_open_files

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


@dataclass
class Components:
    env: EnvConfig
    settings: SettingsStore
    recorder: Any
    uploader: Any


def build_components(env: EnvConfig) -> Components:
    env.data_dir.mkdir(parents=True, exist_ok=True)
    recovered = recover_open_files(env.data_dir)
    if recovered:
        log.info("Recovered %d unfinished files from a previous run", len(recovered))
    settings = SettingsStore(env.data_dir / "settings.json")
    s3 = env.s3
    uploader = Uploader(
        make_s3_client(s3) if s3 else None,
        s3.bucket if s3 else None,
        s3.prefix if s3 else "",
        env.data_dir,
    )
    # 8 s timeout (not the library's 30 s) so an in-flight request finishes within the recorder's 10 s shutdown join.
    recorder = Recorder(
        TripPlannerClient(api_key=env.tfnsw_api_key, timeout=8), settings, env.data_dir, uploader.enqueue
    )
    return Components(env=env, settings=settings, recorder=recorder, uploader=uploader)


def create_app(env: EnvConfig | None = None, components: Components | None = None) -> FastAPI:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    env = env or EnvConfig.from_env()
    comps = components or build_components(env)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        comps.uploader.start()
        comps.recorder.start()
        yield
        log.info("Shutting down: closing open files and uploading")
        comps.recorder.stop()
        comps.uploader.stop(timeout=20)

    app = FastAPI(title="TfNSW vehicle position recorder", lifespan=lifespan, docs_url=None, redoc_url=None)
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

    @app.get("/api/status", dependencies=[Depends(require_auth)])
    def status() -> dict:
        settings = comps.settings.get()
        feed_status = comps.recorder.status()
        feeds = []
        for feed in KNOWN_FEEDS:
            st = feed_status.get(feed)
            feeds.append({"feed": feed, "enabled": feed in settings.feeds, **(asdict(st) if st else {})})
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
