"""Високорівневі операції, спільні для команд бота і планувальника."""
from typing import Optional, Tuple

from app.config import config
from app.engine import Recommendation, analyze


async def gather(services) -> Tuple[dict, int]:
    data = {}
    for coin in config.symbols:
        data[coin] = await services.market.fetch_coin(coin)
    fng = await services.market.fetch_fng()
    return data, fng


async def recommend(services, amount: Optional[float] = None) -> Tuple[Recommendation, int]:
    base = services.storage.get_base(config.base_amount)
    amt = amount if amount else base
    data, fng = await gather(services)
    rec = analyze(data, fng, amt)
    rec_id = services.storage.save_recommendation(rec)
    return rec, rec_id
