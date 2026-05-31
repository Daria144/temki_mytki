"""Зберігання стану в SQLite: налаштування, журнал рекомендацій/ордерів, стан алертів."""
import datetime
import json
import sqlite3
import time
from dataclasses import asdict
from typing import List, Optional

from app.engine import Recommendation


def flat_orders(payload: dict) -> List[dict]:
    """Розгортає рекомендацію у плоский список ордерів (індекс = порядок у списку)."""
    res = []
    for cp in payload.get("coins", []):
        for o in cp.get("orders", []):
            res.append({
                "coin": cp["coin"],
                "price": o["price"],
                "qty": o["qty"],
                "notional": o["notional"],
            })
    return res


class Storage:
    def __init__(self, path: str):
        self.path = path

    def _conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def init(self):
        with self._conn() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings(
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                CREATE TABLE IF NOT EXISTS recommendations(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at INTEGER,
                    payload TEXT
                );
                CREATE TABLE IF NOT EXISTS orders(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rec_id INTEGER,
                    order_index INTEGER,
                    coin TEXT,
                    price REAL,
                    qty REAL,
                    notional REAL,
                    status TEXT,
                    created_at INTEGER,
                    filled_at INTEGER,
                    UNIQUE(rec_id, order_index)
                );
                CREATE TABLE IF NOT EXISTS alert_state(
                    type TEXT PRIMARY KEY,
                    last_sent INTEGER
                );
                """
            )

    # --- settings ---
    def get_setting(self, key: str, default=None):
        with self._conn() as c:
            row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return row["value"] if row else default

    def set_setting(self, key: str, value):
        with self._conn() as c:
            c.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )

    def get_base(self, default: float) -> float:
        v = self.get_setting("base_amount")
        return float(v) if v else default

    def set_base(self, value: float):
        self.set_setting("base_amount", value)

    def alerts_enabled(self) -> bool:
        return self.get_setting("alerts", "on") == "on"

    def set_alerts(self, on: bool):
        self.set_setting("alerts", "on" if on else "off")

    # --- recommendations / orders ---
    def save_recommendation(self, rec: Recommendation) -> int:
        payload = json.dumps(asdict(rec))
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO recommendations(created_at, payload) VALUES(?, ?)",
                (int(time.time()), payload),
            )
            return cur.lastrowid

    def get_recommendation(self, rec_id: int) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute(
                "SELECT payload FROM recommendations WHERE id=?", (rec_id,)
            ).fetchone()
            return json.loads(row["payload"]) if row else None

    def set_order_decision(self, rec_id: int, order_index: int, status: str) -> Optional[dict]:
        """Зберігає рішення по ОДНОМУ ордеру: 'open' (поставив) або 'skipped' (пропустив)."""
        payload = self.get_recommendation(rec_id)
        if not payload:
            return None
        flat = flat_orders(payload)
        if order_index < 0 or order_index >= len(flat):
            return None
        o = flat[order_index]
        now = int(time.time())
        with self._conn() as c:
            c.execute(
                "INSERT INTO orders(rec_id, order_index, coin, price, qty, notional, status, created_at, filled_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, NULL) "
                "ON CONFLICT(rec_id, order_index) DO UPDATE SET "
                "status=excluded.status, created_at=excluded.created_at, filled_at=NULL",
                (rec_id, order_index, o["coin"], o["price"], o["qty"], o["notional"], status, now),
            )
        return o

    def get_decisions(self, rec_id: int) -> dict:
        with self._conn() as c:
            rows = c.execute(
                "SELECT order_index, status FROM orders WHERE rec_id=?", (rec_id,)
            ).fetchall()
            return {r["order_index"]: r["status"] for r in rows}

    def open_orders(self) -> List[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM orders WHERE status='open' ORDER BY created_at").fetchall()
            return [dict(r) for r in rows]

    def mark_filled(self, order_id: int, ts: int):
        with self._conn() as c:
            c.execute(
                "UPDATE orders SET status='likely_filled', filled_at=? WHERE id=?",
                (ts, order_id),
            )

    def mark_cancelled(self, order_id: int):
        with self._conn() as c:
            c.execute("UPDATE orders SET status='cancelled' WHERE id=?", (order_id,))

    def has_buy_this_month(self) -> bool:
        today = datetime.date.today()
        start = int(datetime.datetime(today.year, today.month, 1).timestamp())
        with self._conn() as c:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM orders WHERE filled_at IS NOT NULL AND filled_at >= ?",
                (start,),
            ).fetchone()
            return row["n"] > 0

    # --- alert dedupe ---
    def alert_recent(self, alert_type: str, cooldown_sec: int) -> bool:
        with self._conn() as c:
            row = c.execute(
                "SELECT last_sent FROM alert_state WHERE type=?", (alert_type,)
            ).fetchone()
            if not row:
                return False
            return (time.time() - row["last_sent"]) < cooldown_sec

    def mark_alert(self, alert_type: str):
        with self._conn() as c:
            c.execute(
                "INSERT INTO alert_state(type, last_sent) VALUES(?, ?) "
                "ON CONFLICT(type) DO UPDATE SET last_sent=excluded.last_sent",
                (alert_type, int(time.time())),
            )
