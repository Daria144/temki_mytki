"""Один денний знімок заробітку в ЖИВУ базу — руками, не чекаючи 03:30.

Потрібен рівно раз, після викатки: наростаюче поле бірж уже містить усе, що
позиції заробили за весь час, тому з першого ж рядка вона побачить справжню
суму, а не «заробила $0».

Повторний запуск того самого дня безпечний: знімок перезаписується, а не
дублюється (PRIMARY KEY по добі й позиції).

Запуск на сервері: /home/ubuntu/invest-bot/.venv/bin/python .cursor/run_income_snapshot.py
"""
import asyncio
import datetime
import logging
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.alerts import earn_income_check
from app.config import config
from app.earn import Earn
from app.market import Market
from app.portfolio import PortfolioReader
from app.storage import Storage

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


async def main():
    storage = Storage(config.db_path)
    storage.init()
    services = types.SimpleNamespace(
        storage=storage, market=Market(), earn=Earn(),
        portfolio=PortfolioReader() if config.has_exchange_keys else None,
        etf=None, coin_facts=None,
    )
    before = len(storage.earn_income())
    await earn_income_check(None, services, datetime.date.today())
    rows = storage.earn_income()
    print(f"рядків у earn_income: було {before}, стало {len(rows)}")
    for r in rows[-10:]:
        print(f"  {r['day']} {r['exchange']:6} {r['coin']:6} {r['product'] or '':16} "
              f"amount={r['amount']:.8f} earned={r['earned']:.10f} "
              f"cumulative={r['cumulative']} measured={r['measured']}")
    if storage.earn_income_skips():
        print(f"доби без знімка: {storage.earn_income_skips()}")
    await services.market.close()


asyncio.run(main())
