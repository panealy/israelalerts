#!/usr/bin/env python3
"""
Tzofar WebSocket listener + HTTP API + WebSocket broadcast server
Stores alerts in SQLite, exposes HTTP API on :8080,
and broadcasts live alerts to authenticated WebSocket clients on :8082.
"""

import asyncio
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

import websockets

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
TZOFAR_WS_URL   = "wss://ws.tzevaadom.co.il/socket?platform=ANDROID"
WS_HEADERS      = {
    "Origin":     "https://www.tzevaadom.co.il",
    "User-Agent": "okhttp/4.9.0",
}
DB_PATH         = os.environ.get("DB_PATH",     "/opt/tzofar/alerts.db")
API_TOKEN       = os.environ.get("API_TOKEN",   "CHANGE_ME")
HTTP_HOST       = "0.0.0.0"
HTTP_PORT       = int(os.environ.get("HTTP_PORT", 8080))
WS_PORT         = int(os.environ.get("WS_PORT",  8082))
RECONNECT_DELAY = 5   # seconds between WS reconnect attempts
MAX_ROWS        = 50_000  # hard cap — trim oldest when exceeded
MAX_WS_CLIENTS  = 10  # max simultaneous broadcast clients

# ── Broadcast client registry ─────────────────────────────────────────────────
# Set of authenticated WebSocket connections to broadcast to.
# Managed entirely within the asyncio event loop.
_broadcast_clients: set = set()
_broadcast_loop = None  # asyncio.AbstractEventLoop or None

async def broadcast(message: str) -> None:
    """Send a message to all authenticated broadcast clients."""
    if not _broadcast_clients:
        return
    disconnected = set()
    for ws in _broadcast_clients.copy():
        try:
            await ws.send(message)
        except Exception:
            disconnected.add(ws)
    _broadcast_clients.difference_update(disconnected)

# ── Database ──────────────────────────────────────────────────────────────────
def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            received_at TEXT    NOT NULL,
            type        TEXT,
            time        INTEGER,
            threat      INTEGER,
            is_drill    INTEGER,
            cities      TEXT,
            raw         TEXT    NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_received_at ON alerts (received_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_threat ON alerts (threat)")
    conn.commit()

    # Migration: fix rows where threat/cities were stored incorrectly
    bad_rows = conn.execute(
        "SELECT id, raw FROM alerts WHERE type = 'ALERT' AND threat IS NULL"
    ).fetchall()
    if bad_rows:
        log.info("Migrating %d ALERT rows with missing threat/cities columns...", len(bad_rows))
        for row_id, raw_json in bad_rows:
            try:
                payload = json.loads(raw_json)
                data    = payload.get("data") or {}
                conn.execute(
                    "UPDATE alerts SET threat=?, cities=?, time=?, is_drill=? WHERE id=?",
                    (
                        data.get("threat"),
                        json.dumps(data.get("cities"), ensure_ascii=False) if data.get("cities") else None,
                        data.get("time"),
                        int(bool(data.get("isDrill", False))),
                        row_id,
                    )
                )
            except Exception as e:
                log.warning("Migration failed for row %d: %s", row_id, e)
        conn.commit()
        log.info("Migration complete.")

    return conn

_db_lock = threading.Lock()
_db = None

def get_db() -> sqlite3.Connection:
    global _db
    if _db is None:
        _db = init_db()
    return _db

def store_alert(payload: dict, received_at: str) -> None:
    db = get_db()
    data = payload.get("data") or {}
    msg_type = payload.get("type")

    if msg_type == "ALERT":
        threat   = data.get("threat")
        cities   = data.get("cities")
        time     = data.get("time")
        is_drill = int(bool(data.get("isDrill", False)))
    else:
        threat   = None
        cities   = None
        time     = data.get("time")
        is_drill = 0

    with _db_lock:
        db.execute("""
            INSERT INTO alerts (received_at, type, time, threat, is_drill, cities, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            received_at,
            msg_type,
            time,
            threat,
            is_drill,
            json.dumps(cities, ensure_ascii=False) if cities is not None else None,
            json.dumps(payload, ensure_ascii=False),
        ))
        db.commit()
        db.execute("""
            DELETE FROM alerts WHERE id IN (
                SELECT id FROM alerts ORDER BY id ASC
                LIMIT MAX(0, (SELECT COUNT(*) FROM alerts) - ?)
            )
        """, (MAX_ROWS,))
        db.commit()

# ── Tzofar WebSocket listener ─────────────────────────────────────────────────
connection_status = {
    "connected":    False,
    "last_message": None,
    "connect_time": None,
    "reconnects":   0,
}

async def listen_forever() -> None:
    global _broadcast_loop
    _broadcast_loop = asyncio.get_running_loop()

    while True:
        try:
            log.info("Connecting to Tzofar WebSocket...")
            async with websockets.connect(
                TZOFAR_WS_URL,
                additional_headers=WS_HEADERS,
                ping_interval=25,
                ping_timeout=15,
            ) as ws:
                connection_status["connected"]    = True
                connection_status["connect_time"] = _now()
                log.info("Connected.")

                async for raw in ws:
                    received_at = _now()
                    connection_status["last_message"] = received_at

                    # Handle binary frames from Tzofar (ping/keepalive)
                    if isinstance(raw, bytes):
                        log.debug("Binary frame ignored (%d bytes)", len(raw))
                        continue

                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError:
                        log.warning("Non-JSON frame: %s", raw[:200])
                        continue

                    log.info("Message: %s", json.dumps(payload)[:200])
                    store_alert(payload, received_at)
                    # Broadcast to live WebSocket clients
                    msg = json.dumps({**payload, "received_at": received_at}, ensure_ascii=False)
                    await broadcast(msg)

        except Exception as exc:
            connection_status["connected"] = False
            connection_status["reconnects"] += 1
            log.warning("WS error (%s), reconnecting in %ds...", exc, RECONNECT_DELAY)
            await asyncio.sleep(RECONNECT_DELAY)

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

# ── WebSocket broadcast server ────────────────────────────────────────────────
async def handle_broadcast_client(ws) -> None:
    """
    Authenticate then stream live Tzofar messages to a client.

    Auth handshake:
      Client sends:  {"token": "YOUR_API_TOKEN"}
      Server sends:  {"status": "ok"}  -- then live messages follow
                 or: {"status": "error", "message": "..."}  -- then disconnects
    """
    remote = ws.remote_address
    log.info("WS broadcast: new connection from %s", remote)

    if len(_broadcast_clients) >= MAX_WS_CLIENTS:
        await ws.send(json.dumps({"status": "error", "message": "Too many clients"}))
        await ws.close()
        log.warning("WS broadcast: rejected %s (max clients)", remote)
        return

    # Wait for auth message (5 second timeout)
    try:
        auth_raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
        auth = json.loads(auth_raw)
        if auth.get("token") != API_TOKEN:
            await ws.send(json.dumps({"status": "error", "message": "Unauthorized"}))
            await ws.close()
            log.warning("WS broadcast: rejected %s (bad token)", remote)
            return
    except asyncio.TimeoutError:
        await ws.send(json.dumps({"status": "error", "message": "Auth timeout"}))
        await ws.close()
        return
    except Exception as e:
        await ws.send(json.dumps({"status": "error", "message": str(e)}))
        await ws.close()
        return

    # Auth OK — add to broadcast set
    await ws.send(json.dumps({"status": "ok", "message": "Authenticated. Listening for live alerts."}))
    _broadcast_clients.add(ws)
    log.info("WS broadcast: %s authenticated (%d clients)", remote, len(_broadcast_clients))

    try:
        # Keep connection alive until client disconnects
        await ws.wait_closed()
    finally:
        _broadcast_clients.discard(ws)
        log.info("WS broadcast: %s disconnected (%d clients)", remote, len(_broadcast_clients))

async def run_broadcast_server() -> None:
    async with websockets.serve(handle_broadcast_client, "0.0.0.0", WS_PORT):
        log.info("WS broadcast server listening on 0.0.0.0:%d", WS_PORT)
        await asyncio.Future()  # run forever

async def main_async() -> None:
    await asyncio.gather(
        listen_forever(),
        run_broadcast_server(),
    )

# ── HTTP API ──────────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _auth(self) -> bool:
        token = self.headers.get("X-API-Token", "")
        return token == API_TOKEN

    def _json(self, data: object, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type",   "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._auth():
            self._json({"error": "Unauthorized"}, 401)
            return

        parsed = urlparse(self.path)
        qs     = parse_qs(parsed.query)
        path   = parsed.path

        if path == "/status":
            self._json({
                **connection_status,
                "ws_broadcast_clients": len(_broadcast_clients),
            })

        elif path == "/alerts":
            limit  = min(int(qs.get("limit",  [100])[0]), 1000)
            offset = int(qs.get("offset", [0])[0])
            threat = qs.get("threat")
            since  = qs.get("since")

            where  = "WHERE 1=1"
            params: list = []

            if threat:
                where += " AND threat = ?"
                params.append(int(threat[0]))
            if since:
                where += " AND received_at >= ?"
                params.append(since[0])

            with _db_lock:
                db = get_db()
                total = db.execute(
                    f"SELECT COUNT(*) FROM alerts {where}", params
                ).fetchone()[0]
                rows = db.execute(
                    f"SELECT raw, received_at FROM alerts {where} ORDER BY id DESC LIMIT ? OFFSET ?",
                    params + [limit, offset]
                ).fetchall()

            alerts = []
            for raw_json, received_at in rows:
                try:
                    obj = json.loads(raw_json)
                except Exception:
                    obj = {"parse_error": raw_json}
                obj["received_at"] = received_at
                alerts.append(obj)

            self._json({
                "total":  total,
                "count":  len(alerts),
                "offset": offset,
                "limit":  limit,
                "alerts": alerts,
            })

        elif path == "/alerts/latest":
            with _db_lock:
                row = get_db().execute(
                    "SELECT raw, received_at FROM alerts ORDER BY id DESC LIMIT 1"
                ).fetchone()
            if row:
                obj = json.loads(row[0])
                obj["received_at"] = row[1]
                self._json(obj)
            else:
                self._json(None)

        else:
            self._json(
                {"error": "Unknown path. Try /status /alerts /alerts/latest"}, 404
            )

# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("Starting Tzofar listener  db=%s  port=%d  ws_port=%d",
             DB_PATH, HTTP_PORT, WS_PORT)

    # Run HTTP server in its own thread
    get_db()  # init DB + migration on startup
    server = HTTPServer((HTTP_HOST, HTTP_PORT), Handler)
    http_thread = threading.Thread(target=server.serve_forever, daemon=True)
    http_thread.start()
    log.info("HTTP API listening on %s:%d", HTTP_HOST, HTTP_PORT)

    # Run Tzofar listener + WS broadcast server together in asyncio
    asyncio.run(main_async())
