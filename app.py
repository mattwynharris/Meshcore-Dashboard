import asyncio
import json
import logging
import os
import signal
import tempfile
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Request, UploadFile
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


# --- Reorder & Ping APIs ---

@app.post("/api/reorder")
async def reorder_repeaters(request: Request):
    """Reorder repeaters in settings and in the live data store."""
    body = await request.json()
    pubkeys = body.get("pubkeys", [])
    if not pubkeys:
        return {"ok": False, "error": "No pubkeys provided"}

    settings = cfg.get_settings()
    existing = {r["pubkey"]: r for r in settings.get("repeaters", [])}
    settings["repeaters"] = [existing[pk] for pk in pubkeys if pk in existing]
    # Preserve any not in the list (shouldn't happen, but be safe)
    for pk, r in existing.items():
        if pk not in pubkeys:
            settings["repeaters"].append(r)
    cfg.save_settings(settings)
    store.reorder(pubkeys)
    return {"ok": True}


@app.post("/api/ping/{pubkey}")
async def ping_repeater(pubkey: str):
    """Ping a repeater and return round-trip latency."""
    return await poller.ping_repeater(pubkey)


# --- Update API ---

# Allowed file paths inside the zip (only update dashboard source files)
_ALLOWED_UPDATE_PATHS = {
    "app.py", "config.py", "data_store.py", "meshcore_poller.py",
}
_ALLOWED_UPDATE_PREFIXES = ("templates/", "static/")
# These are top-level source directories — never strip them during normalisation
_KNOWN_TOP_DIRS = {"templates", "static"}


def _is_allowed_path(name: str) -> bool:
    if name in _ALLOWED_UPDATE_PATHS:
        return True
    return any(name.startswith(p) for p in _ALLOWED_UPDATE_PREFIXES)


@app.post("/api/update")
async def apply_update(file: UploadFile = File(...)):
    """Accept a zip file, validate its contents, and extract to /app/."""
    if not file.filename.endswith(".zip"):
        return {"ok": False, "error": "File must be a .zip archive"}

    data = await file.read()
    if len(data) > 20 * 1024 * 1024:
        return {"ok": False, "error": "Upload too large (max 20 MB)"}

    with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tmp:
        tmp.write(data)
        tmp_path = tmp.name

    try:
        with zipfile.ZipFile(tmp_path) as zf:
            names = zf.namelist()
            # Strip leading top-level directory if zip was created with one
            # e.g. "meshcore-dashboard/app.py" -> "app.py"
            # But do NOT strip known source dirs like "templates/" or "static/"
            def _normalise(name: str) -> str:
                parts = name.split("/", 1)
                if (len(parts) == 2 and "." not in parts[0]
                        and parts[0] not in _KNOWN_TOP_DIRS and parts[1]):
                    return parts[1]
                return name

            normalised = [_normalise(n) for n in names]
            bad = [n for n in normalised if n and not n.endswith("/") and not _is_allowed_path(n)]
            if bad:
                return {"ok": False, "error": f"Zip contains unexpected paths: {bad[:5]}"}

            for zip_name, norm_name in zip(names, normalised):
                if not norm_name or norm_name.endswith("/"):
                    continue
                dest = BASE_DIR / norm_name
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(zf.read(zip_name))

        logger.info(f"Update applied: {len([n for n in normalised if n and not n.endswith('/')])} files")
        return {"ok": True, "files": [n for n in normalised if n and not n.endswith("/")]}
    except zipfile.BadZipFile:
        return {"ok": False, "error": "Invalid zip file"}
    finally:
        os.unlink(tmp_path)


@app.post("/api/restart")
async def restart_app():
    """Send SIGTERM to self — Docker will restart the container."""
    logger.info("Restart requested via /api/restart")

    async def _delayed_kill():
        await asyncio.sleep(0.5)
        os.kill(os.getpid(), signal.SIGTERM)

    asyncio.create_task(_delayed_kill())
    return {"ok": True}
