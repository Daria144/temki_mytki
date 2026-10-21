"""Повна картина earn-пропозицій: ставка, ліміт першої сходинки, блокування.

Потрібно, щоб спроєктувати тижневий огляд на справжніх умовах, а не на
самих лише відсотках: висока ставка на копійчаному ліміті нічого не варта.
Нічого не змінює, лише читає.
"""
import asyncio

from app.earn import Earn, WATCHED, STABLES, CORE


def limit_of(o) -> str:
    """Скільки грошей влазить у першу (найкращу) сходинку."""
    try:
        cap = o.steps()[0].capacity()
    except Exception:  # noqa: BLE001
        return "?"
    if cap is None or cap == float("inf"):
        return "без ліміту"
    return f"{cap:,.0f} {o.coin}"


def lock_of(o) -> str:
    d = getattr(o, "redeem_days", 0) or 0
    if d <= 0:
        return "вільно"
    if d < 1:
        return f"{d * 24:.0f} год"
    return f"{d:.0f} дн"


async def main():
    offers = await Earn().offers()
    print(f"усього пропозицій: {len(offers)}\n")

    interesting = [o for o in offers if o.coin in WATCHED]
    interesting.sort(key=lambda o: -o.steps()[0].apr)

    print(f"{'монета':7} {'ставка':>8}  {'біржа':6} {'продукт':18} "
          f"{'ліміт 1-ї сходинки':22} {'вивід':8} сходинок")
    print("-" * 95)
    for o in interesting:
        star = ""
        if o.coin in STABLES:
            star = " ← долари"
        elif o.coin in CORE:
            star = " ← твоє"
        print(f"{o.coin:7} {o.steps()[0].apr * 100:7.3f}%  {o.exchange:6} "
              f"{(o.product or '')[:18]:18} {limit_of(o):22} {lock_of(o):8} "
              f"{len(o.steps())}{star}")

    print("\n--- де ставка падає після першої сходинки ---")
    for o in interesting:
        steps = o.steps()
        if len(steps) > 1:
            desc = " → ".join(f"{s.apr * 100:.2f}%" for s in steps[:4])
            print(f"{o.coin:7} {o.exchange:6} {desc}")


asyncio.run(main())
