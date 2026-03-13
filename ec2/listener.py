#!/usr/bin/env python3
"""
Tzofar WebSocket listener + HTTP API
Stores alerts in SQLite, schema matches Tzofar WS payload exactly.
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
RECONNECT_DELAY = 5   # seconds between WS reconnect attempts
MAX_ROWS        = 50_000  # hard cap — trim oldest when exceeded

# ── Database ──────────────────────────────────────────────────────────────────
def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            received_at TEXT    NOT NULL,          -- ISO-8601 UTC, added by us
            type        TEXT,                      -- Tzofar: "ALERT", "UPDATE", etc.
            time        INTEGER,                   -- Tzofar: unix timestamp of alert
            threat      INTEGER,                   -- Tzofar: threat category (0=rockets...)
            is_drill    INTEGER,                   -- Tzofar: boolean as 0/1
            cities      TEXT,                      -- Tzofar: JSON array of city strings
            raw         TEXT    NOT NULL           -- full original JSON payload
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_received_at ON alerts (received_at)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_threat ON alerts (threat)
    """)
    conn.commit()
    return conn

# Thread-safe DB connection (created once, shared via lock)
_db_lock = threading.Lock()
_db: sqlite3.Connection | None = None

def get_db() -> sqlite3.Connection:
    global _db
    if _db is None:
        _db = init_db()
    return _db

def store_alert(payload: dict, received_at: str) -> None:
    """
    Persist one Tzofar WS message.
    We store every top-level field we know about, plus the full raw JSON.
    """
    db = get_db()
    cities = payload.get("cities") or payload.get("data")
    with _db_lock:
        db.execute("""
            INSERT INTO alerts (received_at, type, time, threat, is_drill, cities, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            received_at,
            payload.get("type"),
            payload.get("time"),
            payload.get("threat"),
            int(bool(payload.get("isDrill", payload.get("is_drill", False)))),
            json.dumps(cities, ensure_ascii=False) if cities is not None else None,
            json.dumps(payload, ensure_ascii=False),
        ))
        db.commit()
        # Trim to MAX_ROWS
        db.execute("""
            DELETE FROM alerts WHERE id IN (
                SELECT id FROM alerts ORDER BY id ASC
                LIMIT MAX(0, (SELECT COUNT(*) FROM alerts) - ?)
            )
        """, (MAX_ROWS,))
        db.commit()

# ── WebSocket listener ────────────────────────────────────────────────────────
connection_status = {
    "connected":    False,
    "last_message": None,
    "connect_time": None,
    "reconnects":   0,
}

async def listen_forever() -> None:
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

                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError:
                        log.warning("Non-JSON frame: %s", raw[:200])
                        payload = {"raw_text": raw}

                    log.info("Message: %s", json.dumps(payload)[:200])
                    store_alert(payload, received_at)

        except Exception as exc:
            connection_status["connected"] = False
            connection_status["reconnects"] += 1
            log.warning("WS error (%s), reconnecting in %ds...", exc, RECONNECT_DELAY)
            await asyncio.sleep(RECONNECT_DELAY)

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

def start_ws_thread() -> None:
    def run():
        asyncio.run(listen_forever())
    t = threading.Thread(target=run, daemon=True)
    t.start()

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
            self._json(connection_status)

        elif path == "/alerts":
            limit  = min(int(qs.get("limit",  [100])[0]), 1000)
            offset = int(qs.get("offset", [0])[0])
            threat = qs.get("threat")
            since  = qs.get("since")

            sql    = "SELECT raw, received_at FROM alerts WHERE 1=1"
            params: list = []

            if threat:
                sql += " AND threat = ?"
                params.append(int(threat[0]))
            if since:
                sql += " AND received_at >= ?"
                params.append(since[0])

            sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
            params += [limit, offset]

            with _db_lock:
                rows = get_db().execute(sql, params).fetchall()

            alerts = []
            for raw_json, received_at in rows:
                try:
                    obj = json.loads(raw_json)
                except Exception:
                    obj = {"parse_error": raw_json}
                obj["received_at"] = received_at
                alerts.append(obj)

            self._json({"count": len(alerts), "alerts": alerts})

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
    log.info("Starting Tzofar listener  db=%s  port=%d", DB_PATH, HTTP_PORT)
    start_ws_thread()
    server = HTTPServer((HTTP_HOST, HTTP_PORT), Handler)
    log.info("HTTP API listening on %s:%d", HTTP_HOST, HTTP_PORT)
    server.serve_forever()
