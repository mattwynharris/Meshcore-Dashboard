import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import config as cfg
from data_store import DataStore
from meshcore_poller import MeshcorePoller

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("app")

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
store = DataStore()
poller = MeshcorePoller(store)

# Attach SQLite log handler to capture poller activity
_log_handler = store.get_log_handler()
logging.getLogger().addHandler(_log_handler)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(poller.start())
    logger.info("MeshCore poller started")

    async def prune_logs_periodically():
        while True:
            await asyncio.sleep(3600)
            retention = cfg.get_log_retention_hours()
            store.prune_activity_logs(retention)

    prune_task = asyncio.create_task(prune_logs_periodically())

    yield

    prune_task.cancel()
    await poller.stop()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    try:
        await prune_task
    except asyncio.CancelledError:
        pass
    logger.info("MeshCore poller stopped")


app = FastAPI(title="MeshCore Repeater Dashboard", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# --- Dashboard ---

@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = BASE_DIR / "templates" / "dashboard.html"
    return html_path.read_text()


@app.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request):
    return templates.TemplateResponse("logs.html", {"request": request})


# --- Repeater Data API ---

@app.get("/api/repeaters")
async def get_repeaters():
    return store.get_all()


@app.get("/api/history/{pubkey}")
async def get_history(pubkey: str, hours: int = 24):
    return store.get_history(pubkey, hours)


@app.get("/api/logs")
async def get_logs(hours: int = 24, level: str = None, search: str = None, limit: int = 500):
    """Return recent activity logs, optionally filtered by level and/or message text."""
    return store.get_activity_logs(hours=hours, level=level, search=search, limit=limit)


@app.get("/api/stream")
async def event_stream():
    async def generate():
        while True:
            data = json.dumps(store.get_all())
            yield f"event: update\ndata: {data}\n\n"
            await asyncio.sleep(5)

    return StreamingResponse(generate(), media_type="text/event-stream")


# --- Settings API ---

@app.get("/api/settings")
async def get_settings():
    """Return current settings for the settings page."""
    return cfg.get_settings()


@app.post("/api/settings")
async def save_settings(request: Request):
    """Save settings from the web UI and trigger poller reconnect."""
    body = await request.json()

    # Validate required fields
    if "companion_host" not in body or not body["companion_host"]:
        return {"ok": False, "error": "Companion host IP is required"}

    if "companion_port" not in body:
        body["companion_port"] = 5000

    # Ensure port is an integer
    try:
        body["companion_port"] = int(body["companion_port"])
    except (ValueError, TypeError):
        return {"ok": False, "error": "Port must be a number"}

    # Validate repeaters list
    repeaters = body.get("repeaters", [])
    for r in repeaters:
        if not r.get("name") or not r.get("pubkey"):
            return {"ok": False, "error": "Each repeater needs a name and public key"}

    # Ensure timing values are integers with sensible minimums
    body.setdefault("poll_interval_seconds", 120)
    body.setdefault("stagger_delay_seconds", 15)
    body.setdefault("stale_threshold_seconds", 900)
    try:
        body["poll_interval_seconds"] = max(30, int(body["poll_interval_seconds"]))
        body["stagger_delay_seconds"] = max(5, int(body["stagger_delay_seconds"]))
        body["stale_threshold_seconds"] = max(60, int(body["stale_threshold_seconds"]))
    except (ValueError, TypeError):
        return {"ok": False, "error": "Timing values must be numbers"}

    # Log retention
    body.setdefault("log_retention_hours", 24)
    try:
        body["log_retention_hours"] = max(1, int(body["log_retention_hours"]))
    except (ValueError, TypeError):
        body["log_retention_hours"] = 24

    # Save to settings.json
    cfg.save_settings(body)
    logger.info(f"Settings saved: {body['companion_host']}:{body['companion_port']}, "
                f"{len(repeaters)} repeaters")

    # Sync the data store with the new repeater list
    store.sync_repeaters(repeaters)

    # Tell the poller to reconnect with new settings
    poller.request_reconnect()

    return {"ok": True}
