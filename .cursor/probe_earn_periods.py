"""Як саме нараховується заробіток: структура періодів і що вміє Bybit.

Продовження probe_earn_income.py. Тут два питання:
  1) OKX lending-history — це погодинні нарахування? Чи сума їх сходиться з
     полем earnings, яке ми збираємось вважати «за весь час»?
  2) чи є в Bybit хоч якесь джерело історії нагород, крім totalPnl у позиції.

Нічого не змінює, лише читає. Запускати на сервері:
    PYTHONPATH=. .venv/bin/python .cursor/probe_earn_periods.py
"""
import asyncio
import json
import time

from app.config import config
from app.portfolio import PortfolioReader

# Bybit-кандидати: у документації v5 історії нагород по Earn немає, але
# перевірити дешево, а ціна помилки — рахувати приблизно там, де є точне.
BYBIT_TRY = (
    ("earn reward-history", "/v5/earn/reward-history", {"category": "FlexibleSaving"}),
    ("earn interest-history", "/v5/earn/interest-history", {"category": "FlexibleSaving"}),
    ("earn history", "/v5/earn/history", {"category": "FlexibleSaving"}),
    ("earn position (без категорії)", "/v5/earn/position", {}),
    ("earn product FlexibleSaving", "/v5/earn/product", {"category": "FlexibleSaving"}),
    ("transaction-log SPOT", "/v5/account/transaction-log",
     {"accountType": "SPOT", "limit": "10"}),
    ("transaction-log UNIFIED (усе)", "/v5/account/transaction-log",
     {"accountType": "UNIFIED", "limit": "20"}),
    ("asset internal-transfer", "/v5/asset/transfer/query-inter-transfer-list",
     {"limit": "10"}),
)


async def okx_periods(reader: PortfolioReader) -> None:
    for ccy in ("USDT", "ETH", "BTC"):
        print(f"\n=== OKX lending-history {ccy} (limit 100) ===")
        try:
            rows = await reader._okx("/api/v5/finance/savings/lending-history",
                                     {"ccy": ccy, "limit": "100"})
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {e}")
            continue
        print(f"  рядків: {len(rows)}")
        if not rows:
            continue
        stamps = sorted({int(r.get("ts") or 0) for r in rows})
        total = sum(float(r.get("earnings") or 0) for r in rows)
        print(f"  період: {time.strftime('%d.%m %H:%M', time.localtime(stamps[0] / 1000))}"
              f" … {time.strftime('%d.%m %H:%M', time.localtime(stamps[-1] / 1000))}")
        print(f"  різних мометів часу: {len(stamps)}, сума earnings за них: {total:.8f}")
        # Крок між нарахуваннями: година, доба, чи щось своє.
        if len(stamps) > 1:
            gaps = [(b - a) / 60000.0 for a, b in zip(stamps, stamps[1:])]
            print(f"  крок між нарахуваннями, хв: мін {min(gaps):.0f}, "
                  f"макс {max(gaps):.0f}, типовий {sorted(gaps)[len(gaps) // 2]:.0f}")
        for r in rows[:4]:
            ts = int(r.get("ts") or 0) / 1000
            print(f"   {time.strftime('%d.%m %H:%M', time.localtime(ts))} "
                  f"amt={r.get('amt')} earnings={r.get('earnings')} rate={r.get('rate')}")


async def bybit_try(reader: PortfolioReader) -> None:
    for name, path, params in BYBIT_TRY:
        print(f"\n=== Bybit {name}  [{path} {params}] ===")
        try:
            res = await reader._bybit(path, params)
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {e}")
            continue
        rows = res.get("list") if isinstance(res, dict) else None
        if rows is None:
            print("  " + json.dumps(res, ensure_ascii=False, sort_keys=True)[:600])
            continue
        print(f"  рядків: {len(rows)}")
        for row in rows[:4]:
            print("  " + json.dumps(row, ensure_ascii=False, sort_keys=True)[:600])


async def main() -> None:
    print(f"знято: {time.strftime('%Y-%m-%d %H:%M:%S')} (unix {int(time.time())})")
    reader = PortfolioReader()
    if config.has_okx:
        await okx_periods(reader)
    if config.has_bybit:
        await bybit_try(reader)


asyncio.run(main())
