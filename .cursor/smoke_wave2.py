"""Смоук другої хвилі: справжній текст сповіщення на живих даних.

Нічого нікому не шле — замість Telegram підставлений фейковий бот, який друкує
повідомлення в консоль. База тимчасова, тому робочий стан бота не чіпається.

Запуск на сервері: /home/ubuntu/invest-bot/.venv/bin/python .cursor/smoke_wave2.py
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import alerts
from app.config import config
from app.earn import Earn, WATCHED, best_instant_rates, classify_risk, RISK_HIGH
from app.handlers import Services
from app.market import CoinFacts, Market
from app.portfolio import PortfolioReader
from app.service import coin_reliability
from app.storage import Storage


class PrintBot:
    def __init__(self):
        self.n = 0

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        self.n += 1
        print(f"\n===== ПОВІДОМЛЕННЯ {self.n} ({len(text)} знаків) =====")
        print(text)
        if reply_markup:
            labels = [b.text for row in reply_markup.inline_keyboard for b in row]
            print(f"[кнопки: {labels}]")


async def main():
    config.owner_id = config.owner_id or 1
    path = os.path.join(tempfile.mkdtemp(), "smoke.db")
    storage = Storage(path)
    storage.init()
    # Щоб це не був «перший запуск», який лише запамʼятовує стан.
    storage.mark_promos_seen(["Bybit:заглушка"])
    storage.mark_products_seen(["OKX:ZZZ:flex"])
    storage.set_earn_amount(1000.0)

    services = Services(
        storage=storage, market=Market(), earn=Earn(),
        portfolio=PortfolioReader() if config.has_exchange_keys else None,
        coin_facts=CoinFacts(),
    )

    print("### 1. Справжнє earn-сповіщення")
    bot = PrintBot()
    await alerts.earn_check(bot, services)
    if bot.n == 0:
        print("(порожньо — сьогодні нема ні нових акцій, ні стрибків ставок)")

    print("\n### 2. Аналіз надійності незнайомої монети")
    offers = await services.earn.offers()
    rates = best_instant_rates(offers, WATCHED)
    unknown = [c for c in sorted({o.coin for o in offers})
               if classify_risk(c) == RISK_HIGH][:3]
    print("монети:", unknown)
    trust = await coin_reliability(services.coin_facts, unknown)
    from app.formatting import coin_trust_lines
    for line in coin_trust_lines(trust, 500.0, rates):
        print(line)

    print("\n### 3. Дедлайни, які запамʼятав бот")
    print(storage.earn_watch_promos() or "(жодної вартої акції з датою кінця)")


asyncio.run(main())
