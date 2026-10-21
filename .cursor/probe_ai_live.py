"""Одна ЖИВА порада від Gemini: чи справді порадник заробив після зміни моделей.

Один запит, не більше: квота безплатного ключа не гумова. Показує, яка модель
відповіла і чи рішення справді від AI, а не від запасної формули.
"""
import asyncio

from app import advisor, service
from app.config import config
from app.market import Market


async def main():
    print("основна модель:", config.gemini_model)
    print("запасна модель:", config.gemini_fallback_model)
    advisor.reset_health()

    market = Market()
    data = {c: await market.fetch_coin(c) for c in config.symbols}
    fng = await market.fetch_fng()
    rec = await service.analyze_market(data, fng, 100.0)
    await market.close()

    print("\nджерело рішення:", rec.source)
    print("модель, що відповіла:", advisor._preferred_model or "жодна")
    print("порадник живий:", advisor.health()["ok"])
    print("\nзаголовок:", rec.headline)
    print("пояснення:", rec.reasoning)


asyncio.run(main())
