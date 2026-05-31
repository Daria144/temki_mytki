"""Шар даних: відкриті джерела (CoinGecko + alternative.me Fear&Greed).

Жодних акаунтів, ключів чи доступу до біржі — лише публічні дані.
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

RETRY_ATTEMPTS = 3
RETRY_DELAY = 5  # секунд між спробами

log = logging.getLogger(__name__)

CG = "https://api.coingecko.com/api/v3"
FNG_URL = "https://api.alternative.me/fng/?limit=1"

CACHE_TTL = 600  # 10 хвилин

# Назва монети -> id у CoinGecko
IDS = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "BNB": "binancecoin"}


class Market:
    def __init__(self, vs_currency: str = "usd") -> None:
        self.vs = vs_currency
        self._cache: dict = {}
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        return None

    def _get_cache(self, key: str):
        entry = self._cache.get(key)
        if entry and time.time() - entry["ts"] < CACHE_TTL:
            return entry["data"]
        return None

    def _set_cache(self, key: str, data):
        self._cache[key] = {"ts": time.time(), "data": data}

    async def _get(self, url: str, params: dict, timeout: int = 20) -> httpx.Response:
        last_exc = None
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                async with httpx.AsyncClient(
                    timeout=timeout, headers={"accept": "application/json"}
                ) as cl:
                    r = await cl.get(url, params=params)
                    r.raise_for_status()
                    return r
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429:
                    wait = RETRY_DELAY * attempt
                    log.warning("CoinGecko 429 — чекаю %ds (спроба %d/%d)", wait, attempt, RETRY_ATTEMPTS)
                    await asyncio.sleep(wait)
                    last_exc = e
                else:
                    raise
            except Exception as e:
                log.warning("HTTP error attempt %d/%d: %s", attempt, RETRY_ATTEMPTS, e)
                if attempt < RETRY_ATTEMPTS:
                    await asyncio.sleep(RETRY_DELAY)
                last_exc = e
        raise last_exc  # type: ignore[misc]

    async def fetch_coin(self, coin: str) -> dict:
        key = f"coin_{coin.upper()}"
        async with self._lock:
            cached = self._get_cache(key)
            if cached:
                log.debug("cache hit: %s", key)
                return cached

        cid = IDS.get(coin.upper(), coin.lower())
        sp = await self._get(
            f"{CG}/simple/price",
            params={"ids": cid, "vs_currencies": self.vs, "include_24hr_change": "true"},
        )
        spj = sp.json()[cid]
        price = float(spj[self.vs])
        change = spj.get(f"{self.vs}_24h_change")

        mc = await self._get(
            f"{CG}/coins/{cid}/market_chart",
            params={"vs_currency": self.vs, "days": "365"},
        )
        prices = mc.json()["prices"]

        ts = [int(p[0]) for p in prices]
        closes = [float(p[1]) for p in prices]
        result = {
            "coin": coin.upper(),
            "price": price,
            "change_24h": (change / 100.0) if change is not None else None,
            "closes": closes,
            "ts": ts,
        }
        async with self._lock:
            self._set_cache(key, result)
        return result

    async def fetch_fng(self) -> int:
        key = "fng"
        async with self._lock:
            cached = self._get_cache(key)
            if cached is not None:
                return cached

        try:
            r = await self._get(FNG_URL, params={}, timeout=10)
            value = int(r.json()["data"][0]["value"])
            async with self._lock:
                self._set_cache(key, value)
            return value
        except Exception as e:  # настрій ринку не критичний — fallback нейтральний
            log.warning("Fear&Greed недоступний: %s", e)
            return 50
