"""FastAPI app: control page, live map, status/settings/live APIs, health check."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from .cities import City, load_cities
from .config import ConfigError, EnvConfig, SettingsStore
from .gtfsrt import GtfsRtClient
from .history import History
from .live import LiveHub
from .recorder import FeedStatus, Recorder
from .routes import RouteInfo, RouteRefresher
from .storage import city_dir, migrate_legacy_layout
from .uploader import Uploader, make_s3_client
from .writer import recover_open_files

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

SSE_POLL_S = 1.0  # how often an open stream checks the hub for new batches
SSE_HEARTBEAT_S = 15.0  # keeps proxies (Coolify's Traefik) from closing idle streams
RECORDER_STOP_S = 10.0  # each city's recorder closes its files within this (fetch timeout 8 s)

_API_VERSION = re.compile(r"/(v\d+)/")


@dataclass
class CityComponents:
    city: City
    recorder: Recorder | None  # None when the city is disabled (e.g. missing API key)
    live: LiveHub
    history: History
    routes: RouteInfo


@dataclass
class Components:
    env: EnvConfig
    settings: SettingsStore
    uploader: Any
    cities: dict[str, CityComponents]  # in config order; the first enabled one is the default
    refreshers: list[RouteRefresher] = field(default_factory=list)


def build_components(
    env: EnvConfig,
    *,
    cities: dict[str, City] | None = None,
    client_factory: Callable[[], Any] = GtfsRtClient,
    s3_client_factory: Callable[[Any], Any] = make_s3_client,
    route_session=None,
) -> Components:
    """Wire up every configured city. The factories and *route_session* exist for tests."""
    env.data_dir.mkdir(parents=True, exist_ok=True)
    moved = migrate_legacy_layout(env.data_dir)  # before recovery and the uploader's scan
    if moved:
        log.info("Moved legacy folders %s to %s", ", ".join(moved), city_dir(env.data_dir, "sydney"))
    cities = load_cities() if cities is None else cities
    for city in cities.values():
        recovered = recover_open_files(city_dir(env.data_dir, city.id))
        if recovered:
            log.info("%s: recovered %d unfinished files from a previous run", city.id, len(recovered))
    settings = SettingsStore(env.data_dir / "settings.json", cities)
    s3 = env.s3
    s3_client = s3_client_factory(s3) if s3 else None
    bucket = s3.bucket if s3 else None
    uploader = Uploader(s3_client, bucket, {c.id: c.s3_prefix for c in cities.values()}, env.data_dir)
    comps = Components(env=env, settings=settings, uploader=uploader, cities={})
    for city in cities.values():
        cdir = city_dir(env.data_dir, city.id)
        routes = RouteInfo(city, cdir, session=route_session)
        live = LiveHub(city, routes)
        history = History(s3_client, bucket, city.s3_prefix, cdir, city=city, routes=routes)
        recorder = None
        if city.disabled_reason:
            log.error("%s is disabled: %s", city.name, city.disabled_reason)
        else:
            # Own client per city: a requests.Session must not be shared between threads.
            recorder = Recorder(
                client_factory(), city, settings, env.data_dir, uploader.enqueue,
                routes=routes, on_positions=live.publish,
            )
        if city.static_gtfs:
            comps.refreshers.append(RouteRefresher(routes, city.id))
        comps.cities[city.id] = CityComponents(city=city, recorder=recorder, live=live, history=history, routes=routes)
    return comps


def _stop_recorders(recorders: list) -> None:
    """Stop every recorder at once (each may be finishing a fetch); re-raise the first failure."""
    if not recorders:
        return
    with ThreadPoolExecutor(max_workers=len(recorders)) as pool:
        futures = [pool.submit(r.stop, timeout=RECORDER_STOP_S) for r in recorders]
    for future in futures:
        future.result()


def _api_version(url: str) -> str | None:
    match = _API_VERSION.search(url)
    return match.group(1) if match else None


def create_app(env: EnvConfig | None = None, components: Components | None = None) -> FastAPI:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    env = env or EnvConfig.from_env()
    comps = components or build_components(env)
    if not comps.cities:
        raise ConfigError("No cities configured: cities.toml has no city sections")
    # ?city= picks a city; without it the first enabled one (the first at all if none is enabled).
    default_id = next((cid for cid, cc in comps.cities.items() if not cc.city.disabled_reason), next(iter(comps.cities)))
    recorders = [cc.recorder for cc in comps.cities.values() if cc.recorder is not None]

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        comps.uploader.start()
        try:
            for refresher in comps.refreshers:
                refresher.start()
            for recorder in recorders:
                recorder.start()
            yield
        finally:
            for cc in comps.cities.values():
                cc.live.close()
            log.info("Shutting down: closing open files and uploading")
            try:
                _stop_recorders(recorders)
            finally:
                for refresher in comps.refreshers:
                    refresher.stop()
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

    def city_of(city: str | None = Query(None)) -> CityComponents:
        cc = comps.cities.get(default_id if city is None else city)
        if cc is None:
            raise HTTPException(404, "Unknown city")
        return cc

    @app.get("/healthz", response_class=PlainTextResponse)
    def healthz() -> PlainTextResponse:
        # A disabled city has no recorder and does not count.
        ok = all(r.alive for r in recorders) and comps.uploader.alive
        return PlainTextResponse("ok" if ok else "unhealthy", status_code=200 if ok else 500)

    @app.get("/", dependencies=[Depends(require_auth)])
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/map", dependencies=[Depends(require_auth)])
    def live_map() -> FileResponse:
        return FileResponse(STATIC_DIR / "map.html")

    @app.get("/api/cities", dependencies=[Depends(require_auth)])
    def cities() -> list[dict]:
        out = []
        for cc in comps.cities.values():
            city = cc.city
            modes = {f.mode for f in city.feeds} | cc.routes.modes()
            out.append({
                "id": city.id,
                "name": city.name,
                "timezone": city.timezone.key,
                "center": list(city.center),
                "zoom": city.zoom,
                "attribution": city.attribution,
                "modes": sorted(modes),
                "enabled": not city.disabled_reason,
                "disabled_reason": city.disabled_reason,
                "daily_quota": city.daily_quota,
                "max_requests_per_second": city.max_requests_per_second,
            })
        return out

    @app.get("/api/live", dependencies=[Depends(require_auth)])
    def live_snapshot(cc: CityComponents = Depends(city_of)) -> dict:
        return cc.live.snapshot()

    @app.get("/api/history/window", dependencies=[Depends(require_auth)])
    def history_window(start: int, cc: CityComponents = Depends(city_of)) -> Response:
        """Recorded reports of the 10-minute window starting at *start* (epoch s), gzip JSON."""
        if cc.history is None:
            raise HTTPException(503, "Playback is not available")
        try:
            body = cc.history.window(start)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:
            log.exception("Reading recorded window %s failed", start)
            raise HTTPException(502, "Could not read recorded data") from exc
        return Response(body, media_type="application/json",
                        headers={"Content-Encoding": "gzip", "Cache-Control": "no-store"})

    @app.get("/api/live/stream", dependencies=[Depends(require_auth)])
    async def live_stream(
        request: Request, since: int | None = None, last_event_id: str | None = Header(None),
        cc: CityComponents = Depends(city_of),
    ) -> StreamingResponse:
        """Server-Sent Events: one event per batch of new reports, id = batch seq."""
        if last_event_id and last_event_id.isdigit():
            since = int(last_event_id)  # an EventSource reconnect resumes where it stopped
        hub = cc.live

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
    def status(cc: CityComponents = Depends(city_of)) -> dict:
        city, recorder = cc.city, cc.recorder
        settings = comps.settings.get(city.id)
        feed_status = recorder.status() if recorder else {}
        feeds = []
        for feed in city.feeds:
            st = feed_status.get(feed.id)
            feeds.append({
                "feed": feed.id,
                "enabled": feed.id in settings.feeds,
                "version": _api_version(feed.url),
                **asdict(st or FeedStatus()),
            })
        return {
            "city": city.id,
            "recorder": {
                "alive": bool(recorder and recorder.alive),
                "started_at": recorder.started_at if recorder else None,
                "disabled_reason": city.disabled_reason,
            },
            "settings": settings.to_dict(),
            "quota": {
                "requests_per_day": round(len(settings.feeds) * 86400 / settings.interval),
                "daily_quota": city.daily_quota,
            },
            "feeds": feeds,
            "uploader": comps.uploader.status(),
        }

    @app.put("/api/settings", dependencies=[Depends(require_auth)])
    def update_settings(payload: Any = Body(...), cc: CityComponents = Depends(city_of)) -> dict:
        try:
            return comps.settings.update(cc.city.id, payload).to_dict()
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    return app
