"""Шар даних: відкриті джерела (CoinGecko + alternative.me Fear&Greed).

Жодних акаунтів, ключів чи доступу до біржі — лише публічні дані.

Для стійкості до збоїв CoinGecko (429 / мережа) ціни тягнемо ланцюжком
джерел: CoinGecko -> Bybit -> Binance -> кеш.
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import time
from typing import Optional

import httpx

log = logging.getLogger(__name__)

CG = "https://api.coingecko.com/api/v3"
BYBIT = "https://api.bybit.com/v5/market"
BINANCE = "https://api.binance.com/api/v3"
FNG_URL = "https://api.alternative.me/fng/?limit=1"

# Кеш — лише СТРАХОВКА на випадок 429/збою. Свіжі дані тягнемо завжди.
# Якщо запит впав, віддаємо останній успішний результат, якщо він не старіший за це.
FALLBACK_MAX_AGE = 1800  # 30 хвилин

# Тижневі свічки (для 200-тижневої середньої) змінюються повільно — кешуємо надовго.
WEEKLY_TTL = 12 * 3600

# Денні мінімуми: сьогоднішня свічка ще жива, тому кеш коротший за тижневий.
LOWS_TTL = 3600  # 12 годин

# Назва монети -> id у CoinGecko
IDS = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "BNB": "binancecoin"}

# Назва монети -> тикер на біржах (Bybit/Binance)
SYMBOLS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT", "BNB": "BNBUSDT"}


class Market:
    def __init__(self, vs_currency: str = "usd") -> None:
        self.vs = vs_currency
        self._cache: dict = {}
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        return None

    def _get_fallback(self, key: str):
        """Повертає (data, age_sec) останнього успішного результату, якщо не задавнений."""
        entry = self._cache.get(key)
        if entry:
            age = time.time() - entry["ts"]
            if age < FALLBACK_MAX_AGE:
                return entry["data"], age
        return None, None

    def _set_cache(self, key: str, data):
        self._cache[key] = {"ts": time.time(), "data": data}

    async def _get(self, url: str, params: dict, timeout: int = 20) -> httpx.Response:
        async with httpx.AsyncClient(
            timeout=timeout, headers={"accept": "application/json"}
        ) as cl:
            r = await cl.get(url, params=params)
            r.raise_for_status()
            return r

    async def _fetch_coingecko(self, coin: str) -> dict:
        """CoinGecko /simple/price + /market_chart (365 днів)."""
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
        return {
            "coin": coin.upper(),
            "price": price,
            "change_24h": (change / 100.0) if change is not None else None,
            "closes": closes,
            "ts": ts,
            # CoinGecko market_chart дає лише ціни, без денних мінімумів.
            # Їх доллємо окремо з біржі у fetch_coin — без них не видно спрацювань.
            "lows": None,
            "low_ts": None,
        }

    async def _daily_bybit(self, coin: str):
        """Денні свічки Bybit: (початки днів у мс, closes, lows), oldest->newest."""
        symbol = SYMBOLS.get(coin.upper())
        if symbol is None:
            raise ValueError(f"Bybit: невідомий символ для {coin}")
        kl = await self._get(
            f"{BYBIT}/kline",
            params={"category": "spot", "symbol": symbol, "interval": "D", "limit": 365},
        )
        rows = kl.json()["result"]["list"]
        if not rows:
            raise ValueError("Bybit: порожній kline")
        # Bybit віддає найновіші першими — реверсимо у oldest->newest.
        rows = list(reversed(rows))
        ts = [int(r[0]) for r in rows]          # startTime у мс
        closes = [float(r[4]) for r in rows]    # close — індекс 4
        lows = [float(r[3]) for r in rows]      # low — індекс 3 (денний мінімум)
        return ts, closes, lows

    async def _daily_binance(self, coin: str):
        """Денні свічки Binance: (початки днів у мс, closes, lows), oldest->newest."""
        symbol = SYMBOLS.get(coin.upper())
        if symbol is None:
            raise ValueError(f"Binance: невідомий символ для {coin}")
        kl = await self._get(
            f"{BINANCE}/klines",
            params={"symbol": symbol, "interval": "1d", "limit": 365},
        )
        rows = kl.json()
        if not rows:
            raise ValueError("Binance: порожній klines")
        # openTime — індекс 0 (мс), low — індекс 3, close — індекс 4.
        return [int(r[0]) for r in rows], [float(r[4]) for r in rows], [float(r[3]) for r in rows]

    async def _fetch_bybit(self, coin: str) -> dict:
        """Bybit v5 spot: денні свічки + тикер."""
        symbol = SYMBOLS[coin.upper()]
        ts, closes, lows = await self._daily_bybit(coin)

        tk = await self._get(
            f"{BYBIT}/tickers",
            params={"category": "spot", "symbol": symbol},
        )
        tkj = tk.json()["result"]["list"][0]
        price = float(tkj["lastPrice"])
        # price24hPcnt вже фракція (напр. "0.05" = +5%)
        change = tkj.get("price24hPcnt")
        return {
            "coin": coin.upper(),
            "price": price,
            "change_24h": float(change) if change is not None else None,
            "closes": closes,
            "ts": ts,
            "lows": lows,
            "low_ts": ts,
        }

    async def _fetch_binance(self, coin: str) -> dict:
        """Binance spot: денні klines + 24h тикер."""
        symbol = SYMBOLS[coin.upper()]
        ts, closes, lows = await self._daily_binance(coin)

        tk = await self._get(
            f"{BINANCE}/ticker/24hr",
            params={"symbol": symbol},
        )
        tkj = tk.json()
        price = float(tkj["lastPrice"])
        # priceChangePercent — у відсотках, ділимо на 100 для фракції.
        change = tkj.get("priceChangePercent")
        return {
            "coin": coin.upper(),
            "price": price,
            "change_24h": (float(change) / 100.0) if change is not None else None,
            "closes": closes,
            "ts": ts,
            "lows": lows,
            "low_ts": ts,
        }

    async def fetch_daily_lows(self, coin: str):
        """Денні мінімуми (початки днів у мс, самі мінімуми). Bybit -> Binance -> (None, None).

        Потрібні, щоб бачити спрацювання лімітних заявок: ціна торкається заявки
        ВСЕРЕДИНІ дня, і закриття дня цього просто не показує. Основне джерело цін
        (CoinGecko) мінімумів не дає, тож доливаємо їх сюди з біржі.
        """
        key = f"lows_{coin.upper()}"
        async with self._lock:
            entry = self._cache.get(key)
            if entry and time.time() - entry["ts"] < LOWS_TTL:
                return entry["data"]
        for name, fn in (("Bybit", self._daily_bybit), ("Binance", self._daily_binance)):
            try:
                ts, _closes, lows = await fn(coin)
            except Exception as e:  # noqa: BLE001
                log.warning("%s: денні мінімуми з %s недоступні (%s)", coin, name, e)
                continue
            async with self._lock:
                self._set_cache(key, (ts, lows))
            log.info("%s: денні мінімуми з %s (%d свічок)", coin, name, len(lows))
            return ts, lows
        # Запасний варіант — не падати: перевірка спрацювань відкотиться на closes.
        log.warning("%s: денних мінімумів немає — перевірка спрацювань піде по закриттях", coin)
        return None, None

    async def _fetch_weekly_bybit(self, coin: str) -> list:
        symbol = SYMBOLS.get(coin.upper())
        if symbol is None:
            raise ValueError(f"Bybit: невідомий символ для {coin}")
        kl = await self._get(
            f"{BYBIT}/kline",
            params={"category": "spot", "symbol": symbol, "interval": "W", "limit": 1000},
        )
        rows = kl.json()["result"]["list"]
        if not rows:
            raise ValueError("Bybit: порожній weekly kline")
        rows = list(reversed(rows))  # newest-first -> oldest-first
        return [float(r[4]) for r in rows]

    async def _fetch_weekly_binance(self, coin: str) -> list:
        symbol = SYMBOLS.get(coin.upper())
        if symbol is None:
            raise ValueError(f"Binance: невідомий символ для {coin}")
        kl = await self._get(
            f"{BINANCE}/klines",
            params={"symbol": symbol, "interval": "1w", "limit": 1000},
        )
        rows = kl.json()
        if not rows:
            raise ValueError("Binance: порожній weekly klines")
        return [float(r[4]) for r in rows]

    async def fetch_weekly_closes(self, coin: str) -> list:
        """Тижневі closes (oldest->newest) для 200-тижневої середньої. Bybit -> Binance -> [].

        Опціональні дані: якщо джерела недоступні — повертаємо [] (не валимо основний потік).
        """
        key = f"weekly_{coin.upper()}"
        async with self._lock:
            entry = self._cache.get(key)
            if entry and time.time() - entry["ts"] < WEEKLY_TTL:
                return entry["data"]
        for name, fn in (("Bybit", self._fetch_weekly_bybit), ("Binance", self._fetch_weekly_binance)):
            try:
                closes = await fn(coin)
            except Exception as e:  # noqa: BLE001
                log.warning("%s: тижневі з %s недоступні (%s)", coin, name, e)
                continue
            async with self._lock:
                self._set_cache(key, closes)
            log.info("%s: тижневі з %s (%d свічок)", coin, name, len(closes))
            return closes
        return []

    async def fetch_coin(self, coin: str) -> dict:
        """Ланцюжок джерел: CoinGecko -> Bybit -> Binance -> кеш. Додає weekly_closes (200-тиж.)."""
        key = f"coin_{coin.upper()}"
        sources = (
            ("CoinGecko", self._fetch_coingecko),
            ("Bybit", self._fetch_bybit),
            ("Binance", self._fetch_binance),
        )
        last_err: Exception | None = None
        for name, fn in sources:
            try:
                result = await fn(coin)
            except Exception as e:
                last_err = e
                log.warning("%s: джерело %s недоступне (%s)", coin, name, e)
                continue
            # CoinGecko мінімумів не віддає — доливаємо їх з біржі, інакше бот
            # не побачить заявок, які ринок зачепив усередині дня.
            if not result.get("lows"):
                result["low_ts"], result["lows"] = await self.fetch_daily_lows(coin)
            # Тижневі свічки — опціонально, для 200-тижневої середньої (довгий цикл).
            result["weekly_closes"] = await self.fetch_weekly_closes(coin)
            log.info("%s: дані з джерела %s (price=%s, closes=%d, weekly=%d)",
                     coin, name, result["price"], len(result["closes"]),
                     len(result["weekly_closes"]))
            async with self._lock:
                self._set_cache(key, result)
            return result

        # Усі живі джерела впали — пробуємо віддати останній успішний результат.
        async with self._lock:
            fallback, age = self._get_fallback(key)
        if fallback is not None:
            log.warning("%s: усі джерела недоступні (%s) — беру з кешу (%.0f хв тому)",
                        coin, last_err, age / 60)
            return fallback
        log.error("%s: усі джерела недоступні і кешу немає: %s", coin, last_err)
        raise last_err if last_err is not None else RuntimeError(f"{coin}: немає даних")

    async def fetch_fng(self) -> int:
        key = "fng"
        try:
            r = await self._get(FNG_URL, params={}, timeout=10)
            value = int(r.json()["data"][0]["value"])
            async with self._lock:
                self._set_cache(key, value)
            return value
        except Exception as e:
            async with self._lock:
                fallback, age = self._get_fallback(key)
            if fallback is not None:
                log.warning("Fear&Greed недоступний (%s) — беру з кешу", e)
                return fallback
            log.warning("Fear&Greed недоступний: %s — fallback 50", e)
            return 50  # нейтральний настрій


# --- довідка про незнайому монету (окремо від цін BTC/ETH) ---

# Профіль монети змінюється місяцями, а CoinGecko вже ловив нас на 429 —
# тому доба кешу і власний клієнт, щоб ці запити не заважали цінам.
COIN_FACTS_TTL = 24 * 3600
COIN_LIST_TTL = 7 * 24 * 3600

# Скільки монет беремо з рейтингу за капіталізацією. Чотири сторінки по 250 —
# це топ-1000: усе, про що взагалі має сенс говорити, туди вміщується.
CG_MARKETS_PAGES = 4
CG_MARKETS_PER_PAGE = 250
# Пауза між сторінками рейтингу: безплатний CoinGecko інакше віддає 429.
CG_PAGE_PAUSE = 1.5
# Якщо тикер носить кілька монет і навіть найбільша з них дрібніша за це —
# сказати «не змогла надійно визначити» чесніше, ніж показати цифри навмання.
AMBIGUOUS_MIN_CAP = 50_000_000


def _years_since(raw) -> Optional[float]:
    """Скільки років минуло від дати (ISO або ISO з часом). None — дати немає."""
    if not raw:
        return None
    try:
        born = datetime.date.fromisoformat(str(raw)[:10])
    except (ValueError, TypeError):
        return None
    return round((datetime.date.today() - born).days / 365.25, 1)


class CoinFacts:
    """Вік, місце за розміром і денний оборот монети — щоб оцінити не ставку, а саму монету.

    Свідомо окремий клас із власним кешем і замком: fetch_coin() ходить у ті самі
    домени щохвилини, і змішувати з ним рідкісні довідкові запити означало б
    ділити з ними ліміт 429.
    """

    def __init__(self) -> None:
        self._cache: dict = {}
        self._lock = asyncio.Lock()

    async def _cached(self, key: str, ttl: int, producer):
        async with self._lock:
            entry = self._cache.get(key)
            if entry and time.time() - entry["ts"] < ttl:
                return entry["data"]
        data = await producer()
        async with self._lock:
            self._cache[key] = {"ts": time.time(), "data": data}
        return data

    async def _get(self, url: str, params: dict):
        async with httpx.AsyncClient(timeout=20, headers={"accept": "application/json"}) as cl:
            r = await cl.get(url, params=params)
            r.raise_for_status()
            return r.json()

    async def _markets_page(self, page: int) -> list:
        return await self._get(f"{CG}/coins/markets", {
            "vs_currency": "usd", "order": "market_cap_desc",
            "per_page": CG_MARKETS_PER_PAGE, "page": page, "sparkline": "false",
        }) or []

    async def _symbol_map(self) -> dict:
        """Тикер -> НАЙБІЛЬША за капіталізацією монета з таким тикером.

        Тикери на CoinGecko повторюються: під одним символом висять десятки
        мертвих підробок. Раніше бралась перша-ліпша, і в аналіз потрапляла
        монета з 8783-м місцем і оборотом $1.82 — тобто чесні цифри про зовсім
        іншу монету. Тому йдемо по списку, відсортованому за капіталізацією,
        і на кожен тикер лишаємо найбільшу.

        Сторінки кешуються поштучно й з паузою: безплатний CoinGecko охоче
        віддає 429, а через одну збиту сторінку губити весь рейтинг не варто.
        """
        by_symbol: dict = {}
        complete = True
        for page in range(1, CG_MARKETS_PAGES + 1):
            try:
                rows = await self._cached(
                    f"cg_markets_{page}", COIN_LIST_TTL,
                    lambda p=page: self._markets_page(p),
                )
            except Exception as e:  # noqa: BLE001
                log.warning("CoinGecko: сторінка %s рейтингу недоступна (%s)", page, e)
                complete = False
                continue
            if not rows:
                break        # рейтинг просто закінчився — це не збій
            for row in rows:
                sym = str(row.get("symbol") or "").upper()
                cid = row.get("id")
                if not sym or not cid:
                    continue
                cap = float(row.get("market_cap") or 0.0)
                cur = by_symbol.get(sym)
                if cur is None:
                    by_symbol[sym] = {"id": cid, "cap": cap,
                                      "rank": row.get("market_cap_rank"), "matches": 1}
                    continue
                cur["matches"] += 1
                if cap > cur["cap"]:
                    cur.update(id=cid, cap=cap, rank=row.get("market_cap_rank"))
            # Пауза лише перед наступним справжнім запитом: сторінка неповна
            # означає, що рейтинг закінчився і ходити більше нікуди.
            if page < CG_MARKETS_PAGES and len(rows) >= CG_MARKETS_PER_PAGE:
                await asyncio.sleep(CG_PAGE_PAUSE)
        return {"by_symbol": by_symbol, "complete": complete}

    async def _symbol_counts(self) -> dict:
        """Скільки монет узагалі носять цей тикер — щоб чесно казати «не впевнена»."""
        rows = await self._get(f"{CG}/coins/list", {})
        out: dict = {}
        for row in rows or []:
            sym = str(row.get("symbol") or "").upper()
            if sym:
                out[sym] = out.get(sym, 0) + 1
        return out

    async def _resolve(self, coin: str) -> tuple:
        """Тикер -> (id, довідка про збіги). id=None, якщо надійно визначити не вдалось."""
        sym = coin.upper()
        if sym in IDS:
            return IDS[sym], {"matches": 1, "picked_by_cap": False}

        smap = await self._cached("markets", COIN_LIST_TTL, self._symbol_map)
        info = smap["by_symbol"].get(sym)
        if info:
            # Кілька однойменних монет, і навіть найбільша з них дрібна — це вже
            # вгадування. Показати чесний аналіз НЕ ТІЄЇ монети гірше, ніж нічого.
            if info["matches"] > 1 and info["cap"] < AMBIGUOUS_MIN_CAP:
                return None, {"matches": info["matches"], "picked_by_cap": True}
            return info["id"], {"matches": info["matches"],
                                "picked_by_cap": info["matches"] > 1}

        if not smap["complete"]:
            # Рейтинг завантажився не повністю. Шукати монету за списком тикерів
            # означало б повернутись до того самого вгадування, з якого й почався баг.
            return None, {"matches": 0, "picked_by_cap": False, "unsure": True}
        counts = await self._cached("symbol_counts", COIN_LIST_TTL, self._symbol_counts)
        return None, {"matches": counts.get(sym, 0), "picked_by_cap": False}

    async def _fetch_profile(self, coin: str) -> Optional[dict]:
        cid, found = await self._resolve(coin)
        if not cid:
            # Тикер існує, але який саме з однойменних токенів мала на увазі біржа —
            # невідомо. Віддаємо явну ознаку, а не цифри навмання.
            if found["matches"] or found.get("unsure"):
                return {"coin": coin.upper(), "name": coin.upper(),
                        "ambiguous": True, "matches": found["matches"]}
            return None
        j = await self._get(f"{CG}/coins/{cid}", {
            "localization": "false", "tickers": "false", "market_data": "true",
            "community_data": "false", "developer_data": "false", "sparkline": "false",
        })
        md = j.get("market_data") or {}
        age_years = _years_since(j.get("genesis_date"))
        # У більшості монет genesis_date порожній. Тоді нижню межу віку дають
        # дати максимуму й мінімуму ціни: раніше за них монета точно торгувалась.
        age_min = None
        if age_years is None:
            dates = [(md.get(k) or {}).get("usd") for k in ("ath_date", "atl_date")]
            known = [_years_since(d) for d in dates if d]
            age_min = max([y for y in known if y is not None], default=None)
        return {
            "coin": coin.upper(),
            "name": j.get("name") or coin.upper(),
            "rank": j.get("market_cap_rank"),
            "volume_usd": (md.get("total_volume") or {}).get("usd"),
            "market_cap_usd": (md.get("market_cap") or {}).get("usd"),
            "age_years": age_years,
            "age_years_at_least": age_min,
            "ambiguous": False,
            "matches": found["matches"],
            # Тикер повторювався, і монету обрано за найбільшою капіталізацією —
            # це варто памʼятати, коли монета все одно виглядає дрібною.
            "picked_by_cap": found["picked_by_cap"],
        }

    async def profile(self, coin: str) -> Optional[dict]:
        """Довідка про монету; None, якщо CoinGecko недоступний або монети немає."""
        try:
            return await self._cached(f"facts:{coin.upper()}", COIN_FACTS_TTL,
                                      lambda: self._fetch_profile(coin))
        except Exception as e:  # noqa: BLE001
            log.warning("Довідка про %s недоступна: %s", coin, e)
            return None

    async def profiles(self, coins) -> dict:
        """Довідки по кількох монетах паралельно; недоступні просто пропускаємо."""
        coins = [c.upper() for c in dict.fromkeys(coins)]
        if not coins:
            return {}
        results = await asyncio.gather(*(self.profile(c) for c in coins),
                                       return_exceptions=True)
        return {c: r for c, r in zip(coins, results)
                if isinstance(r, dict) and r}
