import asyncio
import logging
import os
import ssl

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import BotCommand

from app.alerts import setup_scheduler
from app.config import config
from app.handlers import Services, router
from app.market import Market
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
    if not config.bot_token:
        raise SystemExit("Не заданий TELEGRAM_BOT_TOKEN. Скопіюй .env.example у .env і заповни.")

    bot = Bot(config.bot_token, session=_build_session())
    dp = Dispatcher()
    dp.include_router(router)

    storage = Storage(config.db_path)
    storage.init()
    market = Market()  # дані з CoinGecko у USD (публічні, без ключів)
    services = Services(storage=storage, market=market)

    await bot.set_my_commands([
        BotCommand(command="invest",  description="Рекомендація — коли і якими ордерами купити"),
        BotCommand(command="status",  description="Поточні ціни BTC/ETH і стан ринку"),
        BotCommand(command="orders",  description="Мої активні ордери у журналі"),
        BotCommand(command="cleanup", description="Застряглі ордери — що варто скасувати"),
        BotCommand(command="base",    description="Змінити базову суму (зараз /base 150)"),
        BotCommand(command="alerts",  description="Сповіщення: /alerts on або /alerts off"),
        BotCommand(command="reset",   description="Очистити всі дані (тільки для тестування)"),
        BotCommand(command="start",   description="Довідка і мій Telegram ID"),
    ])

    scheduler = setup_scheduler(bot, services)
    scheduler.start()
    logging.info("Бот запущений. Очікую команди…")

    try:
        await dp.start_polling(bot, services=services)
    finally:
        scheduler.shutdown(wait=False)
        await market.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
