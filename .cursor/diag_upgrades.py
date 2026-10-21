"""Чи справді бот мав порадити переїзд: рахуємо вигоду так, як її рахує сам бот.

Питання просте: $500 USDT лежать під 1%, а найкраща ставка 6.77% — чому мовчить?
Наївно це $29/рік, але 6.77% діють лише на перші сходинки. Тут видно правду:
скільки грошей влазить у кожну сходинку і скільки виходить НАСПРАВДІ.

Нічого не змінює — лише читає публічні ставки й залишки за ключами на читання.
"""
import asyncio

from app.earn import Earn, optimize, smart_amounts
from app.portfolio import PortfolioReader, SPOT, _current_apr, _merge_spot, upgrades


def money(x: float) -> str:
    return f"${x:,.2f}"


def tiers_of(o) -> str:
    parts = []
    for t in o.steps():
        cap = t.capacity()
        cap_s = "∞" if cap == float("inf") else f"{cap:g}"
        parts.append(f"{t.apr * 100:.2f}% на {cap_s}")
    return " → ".join(parts)


def show_offer(o, amount: float, price: float) -> None:
    year = o.per_year(amount)
    eff = year / amount if amount else 0.0
    print(f"      {o.label:22} {o.apr * 100:6.2f}% баз | сходинки: {tiers_of(o)}")
    print(f"        на {amount:g} {o.coin}: {money(year * price)}/рік "
          f"(середня {eff * 100:.2f}%), вивід {o.redeem_days:g} дн, "
          f"мін {o.min_amount or 0:g}")


async def main():
    offers = await Earn().offers()
    pf = await PortfolioReader().fetch()
    print(f"пропозицій: {len(offers)}, позицій: {len(pf.holdings)}, "
          f"не відповіли: {pf.failed or 'нема'}\n")

    print("=" * 78)
    print("ЩО ЛЕЖИТЬ (після склеювання вільних залишків, як це робить upgrades)")
    print("=" * 78)
    merged = _merge_spot(pf.holdings)
    for h in sorted(merged, key=lambda x: -x.value()):
        if h.value() < 1.0:
            continue
        now = _current_apr(h, offers)
        where = "вільно" if h.where == SPOT else (h.product or "earn")
        print(f"\n{h.exchange:6} {h.coin:5} {h.amount:>14.8f} ≈{money(h.value()):>10} "
              f"| {where} | зараз {(now or 0) * 100:.2f}% "
              f"= {money((now or 0) * h.amount * (h.price or 0))}/рік")
        same = sorted([o for o in offers if o.coin == h.coin],
                      key=lambda o: -o.per_year(h.amount))
        for o in same[:4]:
            show_offer(o, h.amount, h.price or 0.0)
        # Найкраще, що дає розклад по кількох місцях.
        for places in (1, 2, 3):
            res = optimize(same, h.amount, max_places=places)
            gain = (res["income"] - (now or 0) * h.amount) * (h.price or 0)
            names = ", ".join(f"{p['offer'].label} {p['amount']:g}" for p in res["parts"])
            print(f"      optimize({places}): {money(res['income'] * (h.price or 0))}/рік "
                  f"(+{money(gain)} проти зараз) → {names}")

    print("\n" + "=" * 78)
    print("upgrades() — те, що реально бачить сповіщення _upgrade_news")
    print("=" * 78)
    for thr, label in ((20.0, "поріг $20 (як у боті)"), (0.0, "поріг $0 (усе поспіль)")):
        rows = upgrades(pf, offers, thr)
        print(f"\n--- {label}: {len(rows)} рядків ---")
        for u in rows:
            print(f"• {u['coin']:5} {money(u['usd_value']):>10} "
                  f"{u['from_exchange']}/{u['from_where']} {u['from_apr'] * 100:.2f}% "
                  f"→ {u['offer'].label} {u['to_apr'] * 100:.2f}% "
                  f"= +{money(u['gain_year_usd'])}/рік")

    print("\n" + "=" * 78)
    print("Сходинки USDT: на яких сумах ставка ламається")
    print("=" * 78)
    usdt = [o for o in offers if o.coin == "USDT"]
    amounts = smart_amounts(usdt, extra=(100, 200, 500, 1000, 2000))
    for amt in amounts:
        res = optimize(usdt, amt)
        names = ", ".join(f"{p['offer'].label} {p['amount']:g}" for p in res["parts"])
        print(f"  ${amt:>9,.0f}: {money(res['income'])}/рік "
              f"(середня {res['effective_apr'] * 100:.2f}%) → {names}")


asyncio.run(main())
