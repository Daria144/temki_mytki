"""Смоук промо-аналізу: справжні акції -> текст, який прийде в Telegram.

Читає сторінки оголошень, кличе порадника і складає повідомлення так само,
як це робить earn_check. Ключ у вивід не друкує.

Запуск на сервері:
    cd /home/ubuntu/invest-bot && ./.venv/bin/python .cursor/promo_smoke.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.alerts import _compose
from app.config import config
from app.earn import Earn, Promo
from app.formatting import promo_lines
from app.service import promo_advice

# Три релевантні акції, які бот віддав за останні дні.
PROMOS = [
    Promo("Bybit", "🐂 Bull Incoming? HODL BTC & Enjoy 10% APR with Bybit Earn!",
          "https://announcements.bybit.com/en-US/article/"
          "bull-incoming-hodl-btc-enjoy-10-apr-with-bybit-earn--art5c62060158b0/", 0),
    Promo("Bybit", "Grid Trading: Earn on Every Wave",
          "https://announcements.bybit.com/en-US/article/"
          "grid-trading-earn-on-every-wave--artbb6086cb8dd6/", 0),
    Promo("Bybit", "Explore 5 new assets for Margin Trading, Easy Earn, and Institutional Loans",
          "", 0),
]

# Її гроші й поточні найкращі ставки без блокування (як їх бачить earn_check).
AMOUNTS = {"BTC": 500.0, "USDT": 1200.0}
FREE_USD = 1200.0
RATES = {"BTC": 0.005, "USDT": 0.0677, "ETH": 0.02054, "SOL": 0.04, "ATOM": 0.169}

FIELDS = ("verdict", "bonus_usd", "bonus_what", "apr_pct", "min_amount",
          "max_amount", "locked_days", "period_end", "by_rule")


async def main() -> None:
    print("Ключ Gemini заданий:", bool(config.gemini_api_key))
    earn = Earn()
    bodies = await earn.promo_bodies(PROMOS, limit=len(PROMOS))
    for p in PROMOS:
        print(f"  сторінка «{p.title[:45]}…»: {len(bodies.get(p.key) or '')} знаків")

    explains = await promo_advice(PROMOS, AMOUNTS, FREE_USD, RATES, bodies)
    print("\nЩо витяг порадник:")
    for title, ex in explains.items():
        print(f"  {title[:45]}… -> " +
              ", ".join(f"{f}={ex.get(f)!r}" for f in FIELDS if ex.get(f) not in (None, "")))

    print("\n--- текст сповіщення ---")
    print(_compose([("🎁 Нові акції:", promo_lines(PROMOS, explains))]))


if __name__ == "__main__":
    asyncio.run(main())
