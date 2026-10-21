"""День за днем: коли саме бот помітив би кожну серпневу покупку.

Разова перевірка виправлення №3 на реальних даних. Симулює щоденний daily_check
старою логікою (ціни закриття, без дня створення) і новою (денні мінімуми, з днем
створення). База — КОПІЯ бойової, шлях передається аргументом.

Запуск: .venv/bin/python .cursor/replay_august_fills.py /tmp/botcheck/aug.db [джерело]
"""
import asyncio
import datetime
import sqlite3
import sys

sys.path.insert(0, ".")

from app.alerts import _min_low_since
from app.market import Market

AUG_START = 1785535200   # 2026-08-01
SEP_START = 1788213600   # 2026-09-01


def old_min_low(d, created_at):
    """Як було до виправлення: лише closes і строго ПІСЛЯ моменту створення."""
    cutoff_ms = created_at * 1000
    ts = d.get("ts") or []
    return min((c for t, c in zip(ts, d.get("closes") or []) if t >= cutoff_ms), default=None)


def _cut(d, upto_ts, key_ts, key_val):
    """Дані, доступні станом на день upto_ts — щоб симуляція не підглядала в майбутнє."""
    ts, vals = d.get(key_ts) or [], d.get(key_val) or []
    keep = [(t, v) for t, v in zip(ts, vals) if t <= upto_ts * 1000]
    return [t for t, _ in keep], [v for _, v in keep]


async def load(source: str):
    market = Market()
    out = {}
    for coin in ("BTC", "ETH"):
        if source == "coingecko":
            d = await market._fetch_coingecko(coin)
            d["low_ts"], d["lows"] = await market.fetch_daily_lows(coin)
        else:
            d = await market.fetch_coin(coin)
        out[coin] = d
    return out


async def main(db: str, source: str):
    data = await load(source)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, coin, price, created_at, filled_at FROM orders "
        "WHERE status='filled' AND created_at >= ? ORDER BY created_at", (AUG_START,)
    ).fetchall()

    def fmt(ts):
        return datetime.datetime.fromtimestamp(ts).strftime("%d.%m") if ts else "ніколи"

    print(f"\nджерело закриттів: {source}")
    print(f"{'id':>3} {'монета':6} {'ціна':>10} {'створено':>9} {'реально':>9} "
          f"{'стара логіка':>13} {'нова логіка':>12}")
    old_seen = new_seen = 0
    for r in rows:
        d = data[r["coin"]]
        first_old = first_new = None
        day = r["created_at"]
        while day <= SEP_START:
            ts_c, closes = _cut(d, day, "ts", "closes")
            ts_l, lows = _cut(d, day, "low_ts", "lows")
            if first_old is None:
                v = old_min_low({"ts": ts_c, "closes": closes}, r["created_at"])
                if v is not None and v <= r["price"]:
                    first_old = day
            if first_new is None:
                v = _min_low_since(
                    {"ts": ts_c, "closes": closes, "low_ts": ts_l, "lows": lows},
                    r["created_at"],
                )
                if v is not None and v <= r["price"]:
                    first_new = day
            day += 86400
        old_seen += first_old is not None
        new_seen += first_new is not None
        print(f"{r['id']:>3} {r['coin']:6} {r['price']:>10.2f} {fmt(r['created_at']):>9} "
              f"{fmt(r['filled_at']):>9} {fmt(first_old):>13} {fmt(first_new):>12}")

    print(f"\nПобачив би: стара логіка {old_seen}/{len(rows)}, нова {new_seen}/{len(rows)}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "auto"))
