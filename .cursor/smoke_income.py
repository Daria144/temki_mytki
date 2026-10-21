"""Смоук обліку заробітку на ЖИВИХ ключах: рівно те, що вона побачить у боті.

Нічого нікому не шле й нічого не змінює в робочій базі: вона копіюється в
тимчасовий файл, і денний знімок пишеться саме в копію.

Показує три речі:
  1. сирі цифри від бірж (скільки заробила за весь час — поле самої біржі);
  2. текст кнопки «📒 мій журнал вкладів» після знімка;
  3. earn-блок дампа /status.

Запуск на сервері: /home/ubuntu/invest-bot/.venv/bin/python .cursor/smoke_income.py
"""
import asyncio
import datetime
import os
import shutil
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import income
from app.alerts import earn_income_check
from app.config import config
from app.earn import Earn
from app.formatting import journal_list_text, to_html
from app.ibkr import EtfPrices
from app.market import Market
from app.portfolio import PortfolioReader
from app.status import money_snapshot, sections
from app.storage import Storage


def _storage_copy() -> Storage:
    """Копія робочої бази: смоук не має права нічого в ній зачепити."""
    path = os.path.join(tempfile.mkdtemp(), "income_smoke.db")
    if os.path.exists(config.db_path):
        shutil.copy(config.db_path, path)
    st = Storage(path)
    st.init()
    return st


def head(title):
    print(f"\n\n{'=' * 70}\n### {title}\n{'=' * 70}")


async def main():
    storage = _storage_copy()
    services = types.SimpleNamespace(
        storage=storage, market=Market(), earn=Earn(),
        portfolio=PortfolioReader() if config.has_exchange_keys else None,
        etf=EtfPrices(), coin_facts=None,
    )
    print(f"ключі бірж: {'є' if config.has_exchange_keys else 'НЕМАЄ (заробітку не видно)'}")
    was = storage.earn_income()
    print(f"знімків у базі до смоуку: {len(was)}"
          + (f" (з {was[0]['day']} до {was[-1]['day']})" if was else ""))

    head("СИРІ ЦИФРИ БІРЖ (заробіток за весь час у самій позиції)")
    if services.portfolio is not None:
        pf = await services.portfolio.fetch()
        for h in pf.in_earn():
            src = "None (біржа не дала — рахуватиме бот)" if h.earned is None else \
                f"{h.earned:.10f} ({'за весь час' if h.earned_cumulative else 'за період'})"
            apr = f"{h.current_apr * 100:.2f}%" if h.current_apr is not None else "—"
            print(f"  {h.exchange:6} {h.coin:6} {h.product or '':16} "
                  f"amount={h.amount:.10f} ставка в позиції={apr} earned={src}")
        if pf.failed:
            print(f"  не відповіли: {', '.join(pf.failed)}")

    head("ЗНІМОК: пишемо рядок у КОПІЮ бази")
    today = datetime.date.today()
    await earn_income_check(None, services, today)
    rows = storage.earn_income()
    print(f"рядків у earn_income: {len(rows)}")
    for r in rows:
        print(f"  {r['day']} {r['exchange']:6} {r['coin']:6} {r['product'] or '':16} "
              f"amount={r['amount']:.8f} apr={r['apr']} earned={r['earned']:.10f} "
              f"cumulative={r['cumulative']} measured={r['measured']} price={r['price']}")
    if storage.earn_income_skips():
        print(f"доби без знімка: {storage.earn_income_skips()}")

    snap = await money_snapshot(services, today)
    prices = snap.get("prices") or {}

    head("КНОПКА «📒 мій журнал вкладів» — те, що вона побачить")
    print(to_html(journal_list_text(
        storage.earn_deposits(), prices,
        income.summary(storage.earn_income(), prices), storage.earn_income_skips())))

    head("EARN-БЛОК /status — те, що піде в чат зі стороннім AI")
    for title, lines in sections(snap):
        if "ЗАРОБИЛИ" in title:
            print(title)
            print("\n".join(lines))

    await services.market.close()
    await services.etf.close()


asyncio.run(main())
