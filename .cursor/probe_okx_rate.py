"""Чи справжня ставка в дампі: сирі поля OKX Simple Earn проти публічних ставок.

Підозра проста: у /status і USDT, і ETH показані «ставка зараз ~1.00%», а це
надто схоже на заглушку. OKX у балансі Simple Earn віддає поле `rate` — і
питання в тому, це фактична дохідність чи МІНІМАЛЬНА ставка, яку виставила сама
власниця (у OKX Simple Earn це ринок позик: ти кажеш, дешевше за скільки не
даси, а платять за ринковою).

Нічого не змінює — лише читає.
"""
import asyncio
import json

from app.earn import Earn
from app.portfolio import PortfolioReader


async def main():
    r = PortfolioReader()

    print("=== СИРА відповідь OKX /finance/savings/balance ===")
    rows = await r._okx("/api/v5/finance/savings/balance")
    print(json.dumps(rows, indent=2, ensure_ascii=False))

    print("\n=== СИРА відповідь OKX /finance/staking-defi/orders-active ===")
    try:
        print(json.dumps(await r._okx("/api/v5/finance/staking-defi/orders-active"),
                         indent=2, ensure_ascii=False))
    except Exception as e:  # noqa: BLE001
        print("недоступно:", e)

    print("\n=== СИРА відповідь OKX lending-rate-summary (публічна) ===")
    e = Earn()
    j = await e._get("https://www.okx.com/api/v5/finance/savings/lending-rate-summary")
    summary = {x["ccy"]: x for x in (j.get("data") or [])}
    for coin in ("USDT", "ETH", "BTC", "USDC"):
        print(coin, json.dumps(summary.get(coin), ensure_ascii=False))

    print("\n=== Історія фактичної ставки (lending-rate-history, публічна) ===")
    for coin in ("USDT", "ETH", "BTC"):
        try:
            h = await e._get(
                "https://www.okx.com/api/v5/finance/savings/lending-rate-history",
                {"ccy": coin, "limit": "3"},
            )
            print(coin, json.dumps((h.get("data") or [])[:3], ensure_ascii=False))
        except Exception as err:  # noqa: BLE001
            print(coin, "недоступно:", err)

    print("\n=== Bybit: сирі позиції earn ===")
    for cat in ("FlexibleSaving", "OnChain"):
        try:
            res = await r._bybit("/v5/earn/position", {"category": cat})
            print(cat, json.dumps(res.get("list") or [], indent=2, ensure_ascii=False))
        except Exception as err:  # noqa: BLE001
            print(cat, "недоступно:", err)


asyncio.run(main())
