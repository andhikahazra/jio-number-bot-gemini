import os
import time
import asyncio
import json
import threading
from typing import Optional, List, Dict, Any
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .bot_engine import BotEngine, PREFIX_PRESETS, TIER_1_PREFIXES, TIER_2_PREFIXES, TIER_3_PREFIXES

# Load env variables if present
def load_env():
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if env_file.exists():
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    if k not in os.environ:
                        os.environ[k] = v

load_env()

API_KEY = os.getenv("GRIZZLY_API_KEY", "")
SERVICE = os.getenv("GRIZZLY_SERVICE", "jio")
COUNTRY = os.getenv("GRIZZLY_COUNTRY", "22")
MAX_PRICE = float(os.getenv("GRIZZLY_MAX_PRICE", "0.3"))
PRESET = os.getenv("PREFIX_PRESET", "tier1")
TARGET_PREFIXES = os.getenv("TARGET_PREFIXES", "")
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "2"))
RETRY_DELAY = int(os.getenv("RETRY_DELAY", "4"))
CLEANUP_DELAY = int(os.getenv("CLEANUP_DELAY", "120"))
VERIFY_JIO = os.getenv("VERIFY_JIO", "true").lower() in ("true", "1", "yes")
HEADED_BROWSER = os.getenv("HEADED_BROWSER", "true").lower() in ("true", "1", "yes")

# Initialize Engine
engine = BotEngine(
    api_key=API_KEY,
    service=SERVICE,
    country=COUNTRY,
    max_price=MAX_PRICE,
    preset=PRESET,
    custom_prefixes=TARGET_PREFIXES if PRESET not in PREFIX_PRESETS else "",
    batch_size=BATCH_SIZE,
    retry_delay=RETRY_DELAY,
    cleanup_delay=CLEANUP_DELAY,
    verify_jio=VERIFY_JIO,
    headed_browser=HEADED_BROWSER,
)

app = FastAPI(title="Grizzly MyJio Bot API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def set_loop(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    def broadcast_sync(self, message: dict):
        if self._loop and self.active_connections:
            asyncio.run_coroutine_threadsafe(self.broadcast(message), self._loop)

    async def broadcast(self, message: dict):
        dead_sockets = []
        for connection in list(self.active_connections):
            try:
                await connection.send_json(message)
            except Exception:
                dead_sockets.append(connection)
        for dead in dead_sockets:
            self.disconnect(dead)

ws_manager = ConnectionManager()

# Hook engine events into WebSocket broadcast
def on_engine_event(event_type: str, data: Any):
    ws_manager.broadcast_sync({"type": event_type, "data": data})

engine.subscribe(on_engine_event)

@app.on_event("startup")
async def startup_event():
    ws_manager.set_loop(asyncio.get_running_loop())

# Request Models
class ActionRequest(BaseModel):
    action: str  # "start", "stop", "pause", "resume", "skip"

class ConfigUpdateRequest(BaseModel):
    api_key: Optional[str] = None
    preset: Optional[str] = None
    custom_prefixes: Optional[str] = None
    max_price: Optional[float] = None
    batch_size: Optional[int] = None
    retry_delay: Optional[int] = None
    verify_jio: Optional[bool] = None
    headed_browser: Optional[bool] = None

@app.get("/api/status")
def get_status():
    return engine.get_state()

@app.post("/api/action")
def perform_action(req: ActionRequest):
    act = req.action.lower().strip()
    if act == "start":
        engine.start()
    elif act == "stop":
        engine.stop()
    elif act == "pause":
        engine.pause()
    elif act == "resume":
        engine.resume()
    elif act == "skip":
        engine.skip()
    elif act == "clear":
        with engine._state_lock:
            engine.active_number = None
            engine.active_id = None
            engine.matched_prefix = None
            engine.latest_otp = None
            engine.candidate_queue.clear()
            engine.status_message = "Ready"
            engine.history.clear()
        engine._handle_log("INFO", "🧹 Active session and dummy data cleared.")
        engine._broadcast("state_changed", engine.get_state())
        return {"status": "ok", "action": act, "engine_state": engine.get_state()}
    elif act == "test_playwright":
        def _run_test():
            dummy = "9820012345"
            engine._handle_log("INFO", f"🧪 [Demo] Testing dummy number +91 {dummy} in Playwright Chrome...")
            is_valid, reason = engine.verifier.verify_number(dummy)
            if is_valid:
                engine._handle_log("SUCCESS", f"✅ [Demo] Playwright Validated: {reason}")
            else:
                engine._handle_log("WARN", f"❌ [Demo] Playwright Rejected: {reason}")
        threading.Thread(target=_run_test, daemon=True).start()
        return {"status": "ok", "action": act, "engine_state": engine.get_state()}
    else:
        raise HTTPException(status_code=400, detail=f"Unknown action: {act}")
    return {"status": "ok", "action": act, "engine_state": engine.get_state()}

@app.get("/api/config")
def get_config():
    return {
        "api_key_configured": bool(engine.api_key),
        "preset": engine.preset,
        "custom_prefixes": engine.custom_prefixes,
        "max_price": engine.max_price,
        "batch_size": engine.batch_size,
        "retry_delay": engine.retry_delay,
        "verify_jio": engine.verify_jio,
        "headed_browser": engine.headed_browser,
        "active_prefixes": engine.get_active_prefixes(),
    }

@app.post("/api/config")
def update_config(req: ConfigUpdateRequest):
    engine.update_config(
        api_key=req.api_key,
        preset=req.preset,
        custom_prefixes=req.custom_prefixes,
        max_price=req.max_price,
        batch_size=req.batch_size,
        retry_delay=req.retry_delay,
        verify_jio=req.verify_jio,
        headed_browser=req.headed_browser,
    )
    return {"status": "ok", "config": get_config()}

@app.get("/api/history")
def get_history():
    return list(engine.history)

@app.get("/api/logs")
def get_logs():
    return list(engine.logs)

@app.get("/api/prefixes")
def get_prefixes():
    return {
        "presets": {
            "tier1": {"name": "Tier 1 (Strict / Guaranteed)", "count": len(TIER_1_PREFIXES), "prefixes": TIER_1_PREFIXES},
            "tier2": {"name": "Tier 2 (Popular / Moderate)", "count": len(PREFIX_PRESETS["tier2"]), "prefixes": PREFIX_PRESETS["tier2"]},
            "tier3": {"name": "Tier 3 (Extended / All Known)", "count": len(PREFIX_PRESETS["tier3"]), "prefixes": PREFIX_PRESETS["tier3"]},
        },
        "raw_tiers": {
            "tier1": TIER_1_PREFIXES,
            "tier2": TIER_2_PREFIXES,
            "tier3": TIER_3_PREFIXES,
        }
    }

@app.get("/api/screenshot")
def get_screenshot():
    shot_path = Path(__file__).resolve().parent.parent / "web" / "public" / "live_jio.png"
    if shot_path.exists():
        return FileResponse(
            str(shot_path),
            media_type="image/png",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"}
        )
    raise HTTPException(status_code=404, detail="No screenshot available")

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await ws_manager.connect(websocket)
    try:
        # Send initial full state and recent logs
        await websocket.send_json({"type": "state_changed", "data": engine.get_state()})
        await websocket.send_json({"type": "initial_logs", "data": list(engine.logs)})

        while True:
            data = await websocket.receive_json()
            if isinstance(data, dict):
                action = data.get("action")
                if action:
                    perform_action(ActionRequest(action=action))
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
    except Exception:
        ws_manager.disconnect(websocket)

# Check if Next.js static export exists in web/out
STATIC_DIR = Path(__file__).resolve().parent.parent / "web" / "out"
if STATIC_DIR.exists() and (STATIC_DIR / "index.html").exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static_web")
