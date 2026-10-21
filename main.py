import asyncio
import logging
import os
import ssl

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import BotCommand

from app.alerts import setup_scheduler
from app.config import config
from app.earn import Earn
from app.handlers import Services, router
from app.ibkr import EtfPrices
from app.market import CoinFacts, Market
from app.portfolio import PortfolioReader
from app.storage import Storage


def _build_session() -> AiohttpSession:
    session = AiohttpSession()
    # За корпоративним TLS-проксі (напр. локально) Python не довіряє підміненому
    # сертифікату Telegram. Якщо заданий власний CA-bundle — використовуємо його.
    if config.ca_bundle and os.path.exists(config.ca_bundle):
        session._connector_init["ssl"] = ssl.create_default_context(cafile=config.ca_bundle)
        logging.info("Використовую CA-bundle: %s", config.ca_bundle)
    return session


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx пише повний URL кожного запиту — це і шум, і ризик засвітити ключ у логах.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if not config.bot_token:
        raise SystemExit("Не заданий TELEGRAM_BOT_TOKEN. Скопіюй .env.example у .env і заповни.")

    bot = Bot(config.bot_token, session=_build_session())
    dp = Dispatcher()
    dp.include_router(router)

    storage = Storage(config.db_path)
    storage.init()
    market = Market()  # дані з CoinGecko у USD (публічні, без ключів)
    earn = Earn()      # ставки депозитів OKX/Bybit (теж публічні, без ключів)
    # Реальні залишки на біржах — лише читання. Без ключів бот працює як раніше,
    # просто питає суму вручну.
    portfolio = PortfolioReader() if config.has_exchange_keys else None
    if portfolio:
        logging.info("Ключі бірж знайдено: рахую з реальних залишків.")
    # Ціни ETF на Xetra у євро (Deutsche Börse за ISIN, запасне — Yahoo). Без ключів.
    etf = EtfPrices()
    # Довідка про незнайомі монети (вік, розмір, оборот). Свій кеш і свої запити,
    # щоб рідкісні довідкові виклики не з'їдали ліміт CoinGecko у цін BTC/ETH.
    coin_facts = CoinFacts()
    services = Services(storage=storage, market=market, earn=earn,
                        portfolio=portfolio, etf=etf, coin_facts=coin_facts)

    await bot.set_my_commands([
        BotCommand(command="invest",  description="Порада купити зараз (без суми — порція з бюджету)"),
        BotCommand(command="budget",  description="Місячний бюджет: /budget 300 або /budget +50"),
        BotCommand(command="orders",  description="Мої активні ордери у журналі"),
        BotCommand(command="portfolio", description="Портфель: вкладено, вартість, прибуток, висновки"),
        BotCommand(command="status",  description="Повний стан моїх грошей — скопіювати й вставити в чат з AI"),
        BotCommand(command="earn",    description="Куди покласти вільні гроші під відсоток"),
        BotCommand(command="allocation", description="Бажана пропорція BTC/ETH, напр. /allocation 60 40"),
        BotCommand(command="ibkr",     description="Щомісячна купівля ETF на IBKR (у євро)"),
        BotCommand(command="alerts",  description="Сповіщення: /alerts on або /alerts off"),
        BotCommand(command="start",   description="Довідка і мій Telegram ID"),
    ])

    scheduler = setup_scheduler(bot, services)
    scheduler.start()

    # Якщо на токені лишився активний webhook — polling (getUpdates) не працюватиме
    # і бот "мовчить". Знімаємо webhook на старті, щоб цього не траплялось.
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("Бот запущений. Очікую команди…")

    try:
        await dp.start_polling(bot, services=services)
    finally:
        scheduler.shutdown(wait=False)
        await market.close()
        await etf.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
