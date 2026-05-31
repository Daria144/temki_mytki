"""Шар даних: відкриті джерела (CoinGecko + alternative.me Fear&Greed).

Жодних акаунтів, ключів чи доступу до біржі — лише публічні дані.
"""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)

CG = "https://api.coingecko.com/api/v3"
FNG_URL = "https://api.alternative.me/fng/?limit=1"

# Назва монети -> id у CoinGecko
IDS = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "BNB": "binancecoin"}


class Market:
    def __init__(self, vs_currency: str = "usd") -> None:
        self.vs = vs_currency

    async def close(self) -> None:
        return None

    async def fetch_coin(self, coin: str) -> dict:
        cid = IDS.get(coin.upper(), coin.lower())
        async with httpx.AsyncClient(timeout=20, headers={"accept": "application/json"}) as cl:
            sp = await cl.get(
                f"{CG}/simple/price",
                params={
                    "ids": cid,
                    "vs_currencies": self.vs,
                    "include_24hr_change": "true",
                },
            )
            sp.raise_for_status()
            spj = sp.json()[cid]
            price = float(spj[self.vs])
            change = spj.get(f"{self.vs}_24h_change")

            mc = await cl.get(
                f"{CG}/coins/{cid}/market_chart",
                params={"vs_currency": self.vs, "days": "365"},
            )
            mc.raise_for_status()
            prices = mc.json()["prices"]

        ts = [int(p[0]) for p in prices]
        closes = [float(p[1]) for p in prices]
        return {
            "coin": coin.upper(),
            "price": price,
            "change_24h": (change / 100.0) if change is not None else None,
            "closes": closes,
            "ts": ts,
        }

    async def fetch_fng(self) -> int:
        try:
            async with httpx.AsyncClient(timeout=10) as cl:
                r = await cl.get(FNG_URL)
                r.raise_for_status()
                return int(r.json()["data"][0]["value"])
        except Exception as e:  # настрій ринку не критичний — fallback нейтральний
            log.warning("Fear&Greed недоступний: %s", e)
            return 50
