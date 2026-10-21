"""Смоук /status: повний дамп стану на ЖИВИХ даних (ключі бірж, ринок, її база).

Нічого нікому не шле й нічого не змінює: база копіюється в тимчасовий файл,
тому робочий стан бота лишається недоторканим.

Показує рівно те, що вона побачить у Telegram: кожну частину з підписом і
копіювальним блоком, плюс довжину кожного повідомлення (межа 4096).

Запуск на сервері: /home/ubuntu/invest-bot/.venv/bin/python .cursor/smoke_status.py
"""
import asyncio
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import config
from app.earn import Earn
from app.formatting import journal_list_text, to_html
from app.handlers import Services
from app.ibkr import EtfPrices
from app.market import Market
from app.portfolio import PortfolioReader
from app.status import dump_text, money_snapshot, status_messages
from app.storage import Storage

TG_LIMIT = 4096


def _storage_copy() -> Storage:
    """Копія робочої бази: смоук не має права нічого в ній зачепити."""
    path = os.path.join(tempfile.mkdtemp(), "status_smoke.db")
    if os.path.exists(config.db_path):
        shutil.copy(config.db_path, path)
    st = Storage(path)
    st.init()
    return st


def head(title):
    print(f"\n\n{'=' * 70}\n### {title}\n{'=' * 70}")


async def main():
    storage = _storage_copy()
    services = Services(
        storage=storage, market=Market(), earn=Earn(),
        portfolio=PortfolioReader() if config.has_exchange_keys else None,
        etf=EtfPrices(),
    )
    print(f"ключі бірж: {'є' if config.has_exchange_keys else 'НЕМАЄ (баланси не видно)'}; "
          f"порадник у /status не потрібен взагалі")

    snap = await money_snapshot(services)
    if snap["failed"]:
        print(f"джерела, які не відповіли: {', '.join(snap['failed'])}")

    head("ПОВНИЙ ДАМП (як його прочитає чужа AI)")
    text = dump_text(snap)
    print(text)
    print(f"\n[усього {len(text)} знаків]")

    parts = status_messages(snap)
    head(f"ЯК ЦЕ ПІДЕ В TELEGRAM — {len(parts)} повідомл.")
    for i, part in enumerate(parts, 1):
        html = to_html(part)
        flag = "OK" if len(html) < TG_LIMIT else "ЗАВЕЛИКЕ!"
        print(f"\n----- повідомлення {i}: {len(html)} знаків після розмітки ({flag}) -----")
        print(html)

    head("КОПІЮВАЛЬНИЙ ЖУРНАЛ (кнопка «мій журнал вкладів»)")
    prices = {c: d.get("price") for c, d in (snap.get("market") or {}).items() if d.get("price")}
    print(to_html(journal_list_text(storage.earn_deposits(), prices)))

    await services.market.close()
    await services.etf.close()


asyncio.run(main())
