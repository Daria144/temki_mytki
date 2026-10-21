"""Сирі дані про ЗАРОБІТОК з earn-позицій: що саме віддають OKX і Bybit.

Навіщо окремий скрипт: перед тим як щось рахувати, треба знати, чи біржа
взагалі віддає накопичений заробіток — і чи це «за весь час», чи «за останній
розрахунковий період». Від цього залежить уся арифметика (різниця знімків
проти суми), тому здогадки тут не годяться.

Нічого не змінює, лише читає. Запускати на сервері (ключі привʼязані до IP):
    .venv/bin/python .cursor/probe_earn_income.py
Другий запуск через кілька годин показує, як поле поводиться в часі.
"""
import asyncio
import json
import sys
import time

from app.config import config
from app.portfolio import PortfolioReader

# Приватні endpoint-и, які МОГЛИ Б містити заробіток. Частину біржа віддасть
# помилкою — це теж результат: значить, такого джерела в неї для нас немає.
OKX_PATHS = (
    ("savings/balance", "/api/v5/finance/savings/balance", None),
    ("staking-defi orders-active", "/api/v5/finance/staking-defi/orders-active", None),
    ("savings lending-history", "/api/v5/finance/savings/lending-history", {"limit": "5"}),
    ("finance-bills (тип 43 = earn)", "/api/v5/asset/bills", {"type": "43", "limit": "5"}),
    ("staking-defi orders-history", "/api/v5/finance/staking-defi/orders-history",
     {"limit": "5"}),
)

BYBIT_PATHS = (
    ("earn/position FlexibleSaving", "/v5/earn/position", {"category": "FlexibleSaving"}),
    ("earn/position OnChain", "/v5/earn/position", {"category": "OnChain"}),
    ("earn/order FlexibleSaving", "/v5/earn/order", {"category": "FlexibleSaving"}),
    ("transaction-log INTEREST", "/v5/account/transaction-log",
     {"accountType": "UNIFIED", "type": "INTEREST", "limit": "10"}),
    ("transaction-log SETTLEMENT", "/v5/account/transaction-log",
     {"accountType": "UNIFIED", "type": "SETTLEMENT", "limit": "10"}),
)


def head(title: str) -> None:
    print(f"\n=== {title} ===")


async def dump_okx(reader: PortfolioReader) -> dict:
    out = {}
    for name, path, params in OKX_PATHS:
        head(f"OKX {name}  [{path}]")
        try:
            rows = await reader._okx(path, params)
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {e}")
            continue
        print(f"  рядків: {len(rows)}")
        for row in rows[:6]:
            print("  " + json.dumps(row, ensure_ascii=False, sort_keys=True))
        out[name] = rows
    return out


async def dump_bybit(reader: PortfolioReader) -> dict:
    out = {}
    for name, path, params in BYBIT_PATHS:
        head(f"Bybit {name}  [{path} {params}]")
        try:
            res = await reader._bybit(path, params)
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {e}")
            continue
        rows = res.get("list") if isinstance(res, dict) else None
        if rows is None:
            print("  " + json.dumps(res, ensure_ascii=False, sort_keys=True)[:900])
            out[name] = res
            continue
        print(f"  рядків: {len(rows)}")
        for row in rows[:6]:
            print("  " + json.dumps(row, ensure_ascii=False, sort_keys=True))
        out[name] = rows
    return out


async def main() -> None:
    print(f"знято: {time.strftime('%Y-%m-%d %H:%M:%S')} (unix {int(time.time())})")
    print(f"ключі: OKX={config.has_okx} Bybit={config.has_bybit}")
    reader = PortfolioReader()
    raw = {"ts": int(time.time())}
    if config.has_okx:
        raw["okx"] = await dump_okx(reader)
    if config.has_bybit:
        raw["bybit"] = await dump_bybit(reader)

    # Головна перевірка «наростаюче чи періодичне» — структурна: якщо
    # amt − earnings дає рівно ту суму, яку вона колись поклала (500, 0.2),
    # то earnings накопичується всередині amt і є заробітком за весь час.
    head("OKX savings: перевірка «amt мінус earnings = початкова сума»")
    for row in (raw.get("okx") or {}).get("savings/balance") or []:
        amt = float(row.get("amt") or 0)
        earn = float(row.get("earnings") or 0)
        pend = float(row.get("pendingAmt") or 0)
        loan = float(row.get("loanAmt") or 0)
        rate = float(row.get("rate") or 0)
        per_day = amt * rate / 365.0
        print(f"  {row.get('ccy'):6} amt={amt:.8f} earnings={earn:.8f} "
              f"→ початково {amt - earn:.8f}")
        print(f"         loanAmt+pendingAmt={loan + pend:.8f} (проти amt={amt:.8f})")
        if per_day > 0:
            print(f"         за ставкою {rate * 100:.2f}% це заробіток за "
                  f"{earn / per_day:.2f} доби, у черзі ще {pend / per_day:.2f} доби")

    # Машинний зліпок — щоб другий запуск було з чим порівняти рядок у рядок.
    path = sys.argv[1] if len(sys.argv) > 1 else ""
    if path:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=1, default=str)
        print(f"\nзліпок збережено: {path}")


asyncio.run(main())
