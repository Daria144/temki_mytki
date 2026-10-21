"""Розвідник: що зараз живе по earn — акції, ставки, реальні залишки.

Потрібен, щоб проєктувати AI-пораду на справжніх даних, а не на вигаданих.
Нічого не змінює, лише читає.
"""
import asyncio
import json

from app.config import config
from app.earn import Earn, best_instant_rates, WATCHED


async def main():
    earn = Earn()

    offers = await earn.offers()
    promos = await earn.promos()

    print("=== АКЦІЇ (усі, з ознакою релевантності) ===")
    for p in promos:
        print(json.dumps({
            "exchange": p.exchange,
            "title": p.title,
            "key": p.key,
            "relevant": bool(p.relevant),
            "region_ok": bool(getattr(p, "region_ok", True)),
            "about_earn": bool(getattr(p, "about_earn", False)),
            "url": getattr(p, "url", "") or "",
        }, ensure_ascii=False))

    print("\n=== СТАВКИ ПО МОНЕТАХ (найкраще без блокування) ===")
    rates = best_instant_rates(offers, WATCHED)
    for coin, (apr, off) in sorted(rates.items(), key=lambda kv: -kv[1][0]):
        print(f"{coin:6} {apr * 100:7.3f}%  {off.exchange:6} {off.label}")

    print("\n=== РЕАЛЬНІ ЗАЛИШКИ ===")
    if config.has_exchange_keys:
        from app.portfolio import PortfolioReader
        try:
            pf = await PortfolioReader().fetch()
            print("порожньо:", pf.empty)
            for h in getattr(pf, "holdings", []):
                print(f"{h.coin:6} {h.usd_value:10.2f} USD  {h.exchange:6} "
                      f"where={getattr(h, 'where', '?')} apr={getattr(h, 'apr', 0) * 100:.2f}%")
            print("вільні стейбли:", pf.free_stables())
        except Exception as e:  # noqa: BLE001
            print("не вдалось прочитати баланси:", type(e).__name__, e)
    else:
        print("ключів бірж немає")


asyncio.run(main())
