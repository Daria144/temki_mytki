"""Що НАСПРАВДІ повертає шлях сповіщення _upgrade_news на живих даних.

Питання не в тому, як рахує формула на папері, а в тому, що бачить сам бот:
скільки грошей він вважає вільними (заявки на просадку — це вже не вільні),
які рядки складає й чому мовчить. Нічого не змінює.
"""
import asyncio

from app.alerts import MIN_GAIN_YEAR, _earn_amounts, _upgrade_news
from app.earn import Earn, WATCHED, best_instant_rates, optimize, rate_limit_note
from app.portfolio import PortfolioReader
from app.storage import Storage
from app.market import Market
from app.config import config


class Services:
    """Мінімальний набір, який очікують alerts.* — без Telegram і планувальника."""

    def __init__(self):
        self.storage = Storage(config.db_path)
        self.earn = Earn()
        self.portfolio = PortfolioReader()
        self.market = Market()
        self.coin_facts = None


async def main():
    s = Services()
    offers = await s.earn.offers()

    print("=== гроші, зарезервовані під заявки на просадку ===")
    reserved = s.storage.reserved_for_buys()
    print(f"reserved_for_buys = ${reserved:,.2f}")
    for o in s.storage.open_orders():
        print(f"  • {o['coin']} по ${o['price']:,.2f} на ${o['notional']:,.2f}")

    print("\n=== _earn_amounts (що бот вважає своїм і вільним) ===")
    amounts, free = await _earn_amounts(s)
    for coin, usd in sorted(amounts.items(), key=lambda kv: -kv[1]):
        if usd >= 1:
            print(f"  {coin:6} ${usd:,.2f}")
    print(f"  вільних доларів: ${free:,.2f}")

    print(f"\n=== _upgrade_news (поріг ${MIN_GAIN_YEAR:.0f}/рік) ===")
    lines, coins, _ = await _upgrade_news(s, offers)
    print(f"рядків: {len(lines)}, монети: {coins or 'нема'}")
    for line in lines:
        print(line)

    print("\n=== best_instant_rates — те, що показує /status і /earn ===")
    for coin, (apr, o) in sorted(best_instant_rates(offers, WATCHED).items(),
                                 key=lambda kv: -kv[1][0])[:12]:
        note = rate_limit_note(o)
        print(f"  {coin:6} {apr * 100:6.2f}%  {o.label:22} {note}")

    print("\n=== чи дасть розклад по кількох місцях більше за один переїзд ===")
    for coin, usd in sorted(amounts.items(), key=lambda kv: -kv[1]):
        same = [o for o in offers if o.coin == coin]
        if not same or usd < 20:
            continue
        # Рахуємо в монеті, а не в доларах: сходинки бірж задані саме в монеті.
        pf = await s.portfolio.fetch()
        qty = pf.amount_of(coin)
        if qty <= 0:
            continue
        price = usd / qty
        one = optimize(same, qty, max_places=1)
        three = optimize(same, qty, max_places=3)
        names = ", ".join(f"{p['offer'].label} {p['amount']:g}" for p in three["parts"])
        print(f"  {coin:6} {qty:g} (≈${usd:,.0f}): одне місце ${one['income'] * price:,.2f}/рік, "
              f"три місця ${three['income'] * price:,.2f}/рік → {names}")


asyncio.run(main())
