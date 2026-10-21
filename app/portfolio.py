"""Реальні залишки на OKX і Bybit: де гроші лежать НАСПРАВДІ.

Тут — єдине місце в боті, де використовуються ключі бірж. Ключі read-only
(без торгівлі й виводу) і привʼязані до IP сервера, тому найгірше, що може
статися при витоку, — хтось побачить залишки.

Навіщо: earn.py знає, де гроші МОГЛИ Б лежати, а цей модуль — де вони лежать
зараз. Різниця між цими двома картинками і є та підказка, за якою є що робити
(див. upgrades()).

Без ключів модуль не падає: біржа без ключів просто пропускається, портфель
виходить порожній, решта бота працює як раніше.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlencode

import httpx

from .config import config
from .earn import PRODUCTS, STABLES, Offer, classify_risk, optimize

log = logging.getLogger(__name__)

OKX_BASE = "https://www.okx.com"
BYBIT_BASE = "https://api.bybit.com"

# OKX без User-Agent віддає 403 (та сама історія, що в earn.py).
HEADERS = {"User-Agent": "Mozilla/5.0", "accept": "application/json"}

TIMEOUT = 20

# Bybit: скільки мілісекунд запит вважається дійсним. 5000 — значення з докам.
BYBIT_RECV_WINDOW = "5000"

# Залишки міняються рідко (докупки раз на тиждень), а квоти на приватні
# endpoint-и жорсткі — тому кеш на 5 хвилин.
PORTFOLIO_TTL = 300
# Публічні ціни для монет, по яких біржа не дала оцінки в доларах.
PRICES_TTL = 300

SPOT, EARN = "spot", "earn"


def _body(r: httpx.Response) -> dict:
    """Тіло відповіді як dict; не-JSON (сторінка помилки, 403) — порожній dict."""
    try:
        j = r.json()
    except Exception:  # noqa: BLE001
        return {}
    return j if isinstance(j, dict) else {}


def _http_hint(exchange: str, path: str, r: httpx.Response) -> RuntimeError:
    """Bybit на невідомий ключ віддає 401 з порожнім тілом — причину пояснюємо самі.

    Це найперше, у що впирається перший запуск на сервері, тому голе
    «401 Unauthorized» у логах тут особливо марне.
    """
    return RuntimeError(
        f"{exchange} {path}: HTTP {r.status_code} без пояснення від біржі. Найімовірніше "
        "ключ невірний, IP сервера не доданий у whitelist, або ключу бракує прав на читання."
    )


def _num(raw) -> Optional[float]:
    """Біржі віддають числа рядками і часто порожніми — парсимо мʼяко."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


# --- підпис запитів ---

def okx_timestamp(ts: Optional[float] = None) -> str:
    """ISO8601 UTC з мілісекундами, напр. 2020-12-08T09:08:57.715Z.

    OKX відхиляє будь-який інший формат і будь-який часовий пояс, крім UTC.
    """
    dt = datetime.fromtimestamp(time.time() if ts is None else ts, tz=timezone.utc)
    return f"{dt.strftime('%Y-%m-%dT%H:%M:%S')}.{dt.microsecond // 1000:03d}Z"


def okx_request_path(path: str, params: Optional[dict] = None) -> str:
    """Шлях для підпису — РАЗОМ із query-рядком, інакше OKX каже Invalid signature."""
    query = urlencode(params or {})
    return f"{path}?{query}" if query else path


def okx_prehash(timestamp: str, method: str, request_path: str, body: str = "") -> str:
    return f"{timestamp}{method.upper()}{request_path}{body}"


def okx_sign(secret: str, timestamp: str, method: str, request_path: str, body: str = "") -> str:
    msg = okx_prehash(timestamp, method, request_path, body).encode()
    digest = hmac.new(secret.encode(), msg, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def bybit_prehash(timestamp: str, api_key: str, query: str,
                  recv_window: str = BYBIT_RECV_WINDOW) -> str:
    """Для GET підписується timestamp + api_key + recv_window + queryString."""
    return f"{timestamp}{api_key}{recv_window}{query}"


def bybit_sign(secret: str, timestamp: str, api_key: str, query: str,
               recv_window: str = BYBIT_RECV_WINDOW) -> str:
    msg = bybit_prehash(timestamp, api_key, query, recv_window).encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def _bybit_earned(row: dict) -> Optional[float]:
    """Скільки earn-позиція Bybit заробила за весь час.

    Дві частини, бо Bybit тримає їх окремо: totalPnl — уже зараховане,
    claimableYield — нараховане, але ще не зараховане. Сума саме тому й
    потрібна: у момент зарахування нагорода переїжджає з другого поля в
    перше, і кожне з них окремо в цей момент просідає.

    None (а не 0) — коли обох полів немає: «біржа не сказала» і «не заробила»
    мусять лишатись різними відповідями.
    """
    paid = _num(row.get("totalPnl"))
    pending = _num(row.get("claimableYield"))
    if paid is None and pending is None:
        return None
    return (paid or 0.0) + (pending or 0.0)


# --- дані ---

@dataclass
class Holding:
    """Скільки однієї монети лежить в одному місці однієї біржі."""
    exchange: str                       # OKX / Bybit
    coin: str
    amount: float
    usd_value: Optional[float] = None   # None — біржа не дала оцінки
    where: str = SPOT                   # spot — просто лежить; earn — вже під відсоток
    current_apr: Optional[float] = None # лише для earn, і не всі біржі його віддають
    product: Optional[str] = None       # назва продукту, як її знає біржа
    # Накопичений заробіток так, як його віддає САМА біржа (у монеті).
    # None — біржа такого поля не дала, і рахувати доведеться самим (app/income.py).
    earned: Optional[float] = None
    # True — earned наростає за весь час позиції (OKX earnings, Bybit totalPnl).
    # False — це заробіток за один період і його треба сумувати, а не відніматись.
    earned_cumulative: bool = True

    @property
    def idle(self) -> bool:
        return self.where == SPOT

    @property
    def stable(self) -> bool:
        return self.coin in STABLES

    @property
    def price(self) -> Optional[float]:
        """Ціна монети в USD. Для стейблів вважаємо 1 — похибка тут неістотна."""
        if self.usd_value is not None and self.amount > 0:
            return self.usd_value / self.amount
        return 1.0 if self.stable else None

    def value(self) -> float:
        """Оцінка в доларах; невідому ціну вважаємо нулем, щоб не завищити суму."""
        if self.usd_value is not None:
            return self.usd_value
        return self.amount if self.stable else 0.0


@dataclass
class Portfolio:
    holdings: list = field(default_factory=list)
    # Джерела, які не відповіли: картинка неповна, і про це варто сказати вголос.
    failed: list = field(default_factory=list)
    ts: float = 0.0

    def by_coin(self) -> dict:
        out: dict = {}
        for h in self.holdings:
            out.setdefault(h.coin, []).append(h)
        return out

    def idle(self) -> list:
        """Гроші, які просто лежать і не приносять нічого."""
        return [h for h in self.holdings if h.idle and h.amount > 0]

    def in_earn(self) -> list:
        return [h for h in self.holdings if h.where == EARN and h.amount > 0]

    def total_usd(self) -> float:
        return sum(h.value() for h in self.holdings)

    def free_stables(self) -> float:
        """Вільні стейбли в доларах — саме ці гроші можна кудись покласти сьогодні."""
        return sum(h.value() for h in self.idle() if h.stable)

    def amount_of(self, coin: str, where: Optional[str] = None) -> float:
        return sum(
            h.amount for h in self.holdings
            if h.coin == coin.upper() and (where is None or h.where == where)
        )

    @property
    def empty(self) -> bool:
        return not self.holdings


class PortfolioReader:
    """Клієнт на читання до обох бірж. Нічого не купує і не переказує."""

    def __init__(self, cfg=None) -> None:
        self.cfg = cfg if cfg is not None else config
        self._cache: dict = {}
        self._lock = asyncio.Lock()

    # --- транспорт ---

    async def _cached(self, key: str, ttl: int, producer):
        async with self._lock:
            entry = self._cache.get(key)
            if entry and time.time() - entry["ts"] < ttl:
                return entry["data"]
        data = await producer()
        async with self._lock:
            self._cache[key] = {"ts": time.time(), "data": data}
        return data

    async def _okx(self, path: str, params: Optional[dict] = None) -> list:
        request_path = okx_request_path(path, params)
        ts = okx_timestamp()
        headers = dict(HEADERS)
        headers.update({
            "OK-ACCESS-KEY": self.cfg.okx_api_key,
            "OK-ACCESS-SIGN": okx_sign(self.cfg.okx_api_secret, ts, "GET", request_path),
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self.cfg.okx_passphrase,
            "Content-Type": "application/json",
        })
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=headers) as cl:
            r = await cl.get(f"{OKX_BASE}{request_path}")
            j = _body(r)
        # Помилки автентифікації OKX віддає з HTTP 401, але справжня причина
        # (напр. «IP не в whitelist») лежить у тілі — тому читаємо тіло першим.
        code = str(j.get("code") or "0")
        if code != "0":
            raise RuntimeError(f"OKX {path}: код {code} ({j.get('msg')})")
        if r.status_code >= 400:
            raise _http_hint("OKX", path, r)
        return j.get("data") or []

    async def _bybit(self, path: str, params: Optional[dict] = None) -> dict:
        query = urlencode(params or {})
        ts = str(int(time.time() * 1000))
        headers = dict(HEADERS)
        headers.update({
            "X-BAPI-API-KEY": self.cfg.bybit_api_key,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": BYBIT_RECV_WINDOW,
            "X-BAPI-SIGN": bybit_sign(self.cfg.bybit_api_secret, ts,
                                      self.cfg.bybit_api_key, query),
        })
        # URL збираємо руками: підписаний query-рядок мусить піти в запит
        # символ-у-символ, тому httpx не має його перебудовувати.
        url = f"{BYBIT_BASE}{path}?{query}" if query else f"{BYBIT_BASE}{path}"
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=headers) as cl:
            r = await cl.get(url)
            j = _body(r)
        code = _num(j.get("retCode")) or 0.0
        if code != 0.0:
            raise RuntimeError(f"Bybit {path}: код {j.get('retCode')} ({j.get('retMsg')})")
        if r.status_code >= 400:
            raise _http_hint("Bybit", path, r)
        return j.get("result") or {}

    # --- джерела OKX ---

    async def _okx_trading(self) -> list:
        """Торговий акаунт. eqUsd — оцінка всього залишку монети, її й масштабуємо."""
        out = []
        for acc in await self._okx("/api/v5/account/balance"):
            for row in acc.get("details") or []:
                coin = (row.get("ccy") or "").upper()
                total = _num(row.get("eq"))
                # У earn можна віддати тільки вільні гроші, не заморожені в ордерах.
                amount = _num(row.get("availBal"))
                if amount is None:
                    amount = _num(row.get("cashBal")) or total
                if not coin or not amount or amount <= 0:
                    continue
                eq_usd = _num(row.get("eqUsd"))
                usd = eq_usd * amount / total if eq_usd and total else None
                out.append(Holding("OKX", coin, amount, usd, SPOT))
        return out

    async def _okx_funding(self) -> list:
        """Фандинговий акаунт — оцінки в доларах тут немає взагалі."""
        out = []
        for row in await self._okx("/api/v5/asset/balances"):
            coin = (row.get("ccy") or "").upper()
            amount = _num(row.get("availBal"))
            if amount is None:
                amount = _num(row.get("bal"))
            if coin and amount and amount > 0:
                out.append(Holding("OKX", coin, amount, None, SPOT))
        return out

    async def _okx_savings(self) -> list:
        """Simple Earn (гнучкий): rate — частка на рік, не відсотки.

        earnings — заробіток ЗА ВЕСЬ ЧАС у цій позиції, і він уже сидить
        усередині amt: amt мінус earnings дає рівно ту суму, яку вона колись
        поклала. Це точна цифра від біржі, звідси й облік заробітку.
        """
        out = []
        for row in await self._okx("/api/v5/finance/savings/balance"):
            coin = (row.get("ccy") or "").upper()
            amount = _num(row.get("amt"))
            if coin and amount and amount > 0:
                out.append(Holding("OKX", coin, amount, None, EARN,
                                   _num(row.get("rate")), "Simple Earn",
                                   earned=_num(row.get("earnings"))))
        return out

    @staticmethod
    def _staking_earned(row: dict, coin: str) -> Optional[float]:
        """Заробіток по стейкінг-замовленню: OKX кладе його в earningData.

        Свідомо мʼяко: живими даними не перевірено (стейкінг-позицій зараз
        немає), тому відсутнє поле — це None «біржа не сказала», а не нуль
        «не заробила».
        """
        total = None
        for item in row.get("earningData") or []:
            if (item.get("ccy") or "").upper() not in (coin, ""):
                continue
            value = _num(item.get("earnings"))
            if value is not None:
                total = (total or 0.0) + value
        return total

    async def _okx_staking(self) -> list:
        """Стейкінг і on-chain: сума лежить у investData, ставка в apy."""
        out = []
        for row in await self._okx("/api/v5/finance/staking-defi/orders-active"):
            apy = _num(row.get("apy"))
            # projectDisplayName — це підпис продукту в застосунку (напр. «Eigenlayer»),
            # він зрозуміліший за технічний protocol.
            product = (row.get("projectDisplayName") or row.get("protocol")
                       or row.get("protocolType") or "Staking")
            for inv in row.get("investData") or []:
                coin = (inv.get("ccy") or row.get("ccy") or "").upper()
                amount = _num(inv.get("amt"))
                if coin and amount and amount > 0:
                    out.append(Holding("OKX", coin, amount, None, EARN, apy, product,
                                       earned=self._staking_earned(row, coin)))
        return out

    # --- джерела Bybit ---

    async def _bybit_unified(self) -> list:
        res = await self._bybit("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
        out = []
        for acc in res.get("list") or []:
            for row in acc.get("coin") or []:
                coin = (row.get("coin") or "").upper()
                total = _num(row.get("walletBalance"))
                if not coin or not total or total <= 0:
                    continue
                # locked — заморожене відкритим спотовим ордером, це не вільні гроші.
                amount = max(total - (_num(row.get("locked")) or 0.0), 0.0)
                if amount <= 0:
                    continue
                usd_total = _num(row.get("usdValue"))
                usd = usd_total * amount / total if usd_total else None
                out.append(Holding("Bybit", coin, amount, usd, SPOT))
        return out

    async def _bybit_funding(self) -> list:
        res = await self._bybit(
            "/v5/asset/transfer/query-account-coins-balance", {"accountType": "FUND"}
        )
        out = []
        for row in res.get("balance") or []:
            coin = (row.get("coin") or "").upper()
            amount = _num(row.get("transferBalance"))
            if amount is None:
                amount = _num(row.get("walletBalance"))
            if coin and amount and amount > 0:
                out.append(Holding("Bybit", coin, amount, None, SPOT))
        return out

    async def _bybit_earn(self, category: str) -> list:
        res = await self._bybit("/v5/earn/position", {"category": category})
        # Bybit повертає технічну категорію, а не підпис продукту — перекладаємо
        # на ту саму назву, яку бачимо в застосунку.
        kind = "flex" if category == "FlexibleSaving" else "staking"
        product = PRODUCTS.get(("Bybit", kind), (category, ""))[0]
        out = []
        for row in res.get("list") or []:
            coin = (row.get("coin") or "").upper()
            amount = _num(row.get("amount"))
            if not coin or not amount or amount <= 0:
                continue  # закриті позиції Bybit теж повертає, з нульовою сумою
            # Ставки в позиції немає — її потім підставляємо з публічних
            # пропозицій тієї ж біржі (див. _current_apr).
            out.append(Holding("Bybit", coin, amount, None, EARN, None, product,
                               earned=_bybit_earned(row)))
        return out

    # --- ціни для монет без оцінки ---

    async def _public_prices(self) -> dict:
        """Ціни зі спотових тикерів Bybit — публічний запит, без ключів."""
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS) as cl:
            r = await cl.get(f"{BYBIT_BASE}/v5/market/tickers", params={"category": "spot"})
            r.raise_for_status()
            j = r.json()
        out = {}
        for row in (j.get("result") or {}).get("list") or []:
            symbol = (row.get("symbol") or "").upper()
            price = _num(row.get("lastPrice"))
            if symbol.endswith("USDT") and price:
                out[symbol[:-4]] = price
        return out

    async def _fill_usd(self, holdings: list, prices: Optional[dict] = None) -> None:
        """Домальовує usd_value там, де біржа його не дала."""
        known = dict(prices or {})
        # Одна біржа могла оцінити ту саму монету — цим і користуємось,
        # щоб не ходити в мережу без потреби.
        for h in holdings:
            if h.coin not in known and h.usd_value is not None and h.amount > 0:
                known[h.coin] = h.usd_value / h.amount

        missing = {h.coin for h in holdings
                   if h.usd_value is None and not h.stable and h.coin not in known}
        if missing:
            try:
                known.update(await self._cached("prices", PRICES_TTL, self._public_prices))
            except Exception as e:  # noqa: BLE001
                log.warning("Портфель: публічні ціни недоступні (%s) — частина сум без оцінки", e)

        for h in holdings:
            if h.usd_value is not None:
                continue
            price = 1.0 if h.stable else known.get(h.coin)
            if price:
                h.usd_value = h.amount * price

    # --- збірка ---

    def _sources(self) -> tuple:
        srcs = []
        if self.cfg.has_okx:
            srcs += [
                ("OKX торговий", self._okx_trading()),
                ("OKX фандинг", self._okx_funding()),
                ("OKX Simple Earn", self._okx_savings()),
                ("OKX стейкінг", self._okx_staking()),
            ]
        if self.cfg.has_bybit:
            srcs += [
                ("Bybit unified", self._bybit_unified()),
                ("Bybit фандинг", self._bybit_funding()),
                ("Bybit flexible", self._bybit_earn("FlexibleSaving")),
                ("Bybit on-chain", self._bybit_earn("OnChain")),
            ]
        return tuple(srcs)

    async def _fetch(self, prices: Optional[dict] = None) -> Portfolio:
        sources = self._sources()
        if not sources:
            log.info("Портфель: ключів бірж немає — читати нічого")
            return Portfolio(ts=time.time())

        results = await asyncio.gather(*(c for _, c in sources), return_exceptions=True)
        holdings: list = []
        failed: list = []
        for (name, _), res in zip(sources, results):
            if isinstance(res, Exception):
                log.warning("Портфель: джерело %s недоступне (%s)", name, res)
                failed.append(name)
                continue
            holdings.extend(res)

        await self._fill_usd(holdings, prices)
        return Portfolio(holdings, failed, time.time())

    async def fetch(self, prices: Optional[dict] = None) -> Portfolio:
        """Усі залишки з обох бірж (кеш 5 хв). prices — готові ціни, якщо вже є."""
        return await self._cached("portfolio", PORTFOLIO_TTL, lambda: self._fetch(prices))


# --- головне порівняння: де гроші лежать проти де могли б ---

def _merge_spot(holdings: list) -> list:
    """Вільні гроші однієї монети на одній біржі — це одна сума.

    Торговий і фандинговий акаунти для нас те саме: між ними переказ
    безкоштовний і мгновенний, тому поріг вигоди має рахуватись від суми.
    """
    merged: dict = {}
    prices: dict = {}
    out: list = []
    for h in holdings:
        if not h.idle:
            out.append(h)
            continue
        key = (h.exchange, h.coin)
        if key not in merged:
            merged[key] = Holding(h.exchange, h.coin, 0.0, None, SPOT)
            out.append(merged[key])
        merged[key].amount += h.amount
        if key not in prices and h.price is not None:
            prices[key] = h.price
    # Ціну беремо, а не суму доларів: частина залишків може бути без оцінки,
    # і тоді складання дало б занижену ціну монети.
    for key, acc in merged.items():
        if key in prices:
            acc.usd_value = acc.amount * prices[key]
    return out


def _current_apr(h: Holding, offers: list) -> Optional[float]:
    """Під який відсоток гроші працюють ЗАРАЗ."""
    if h.idle:
        return 0.0
    if h.current_apr is not None:
        return h.current_apr
    # Bybit не віддає ставку в позиції. Найближча правда — публічна ставка
    # тієї ж біржі по цій монеті: саме її користувач і бачить у застосунку.
    same = [o.apr for o in offers if o.exchange == h.exchange and o.coin == h.coin]
    return max(same) if same else None


# Наскільки розклад по кількох місцях має випереджати один переїзд, щоб про нього
# взагалі згадувати. Ганяти гроші між двома біржами заради центів — не варте того.
SPLIT_MIN_EXTRA = 1.0


def _split_plan(offers: list, amount: float, price: float,
                now_year: float, single_gain: float) -> Optional[dict]:
    """Чи дасть розклад по кількох місцях відчутно більше за один переїзд.

    Підвищена ставка на біржах діє лише на перші кількасот доларів, тому «двісті
    сюди, решта туди» іноді перетинає поріг там, де один переїзд його не дотягує.
    """
    res = optimize(offers, amount)
    parts = res["parts"]
    gain = (res["income"] - now_year) * price
    if len(parts) < 2 or gain < single_gain + SPLIT_MIN_EXTRA:
        return None
    return {
        "parts": [{
            "offer": p["offer"],
            "amount": p["amount"],
            "usd_value": p["amount"] * price,
            "apr": p["income"] / p["amount"] if p["amount"] else 0.0,
            "year_usd": p["income"] * price,
        } for p in parts],
        "year_usd": res["income"] * price,
        "gain_year_usd": gain,
    }


def upgrades(portfolio: Portfolio, offers: list, min_gain_year: float = 20.0) -> list:
    """Що варто переставити: поточне місце проти найвигіднішого, вигода в $/рік.

    Поріг у грошах, а не у відсотках: різниця 3% на $50 — це $1.5 на рік,
    заради чого не варто навіть відкривати застосунок.

    Крім одного переїзду рахуємо ще й розклад по кількох місцях (`split`):
    через сходинки він буває помітно вигіднішим, і поріг перетинає саме він.
    """
    rows = []
    for h in _merge_spot(portfolio.holdings):
        price = h.price
        if h.amount <= 0 or price is None:
            continue  # без ціни вигоду в доларах не порахувати
        now_apr = _current_apr(h, offers)
        if now_apr is None:
            continue
        now_year = h.amount * now_apr

        same = [o for o in offers if o.coin == h.coin]
        best: Optional[tuple] = None
        for o in same:
            if o.min_amount and o.min_amount > h.amount:
                continue
            # Уже стоїмо на цій біржі й не гірше — переставляти нічого.
            if not h.idle and o.exchange == h.exchange and o.apr <= now_apr:
                continue
            gain = (o.per_year(h.amount) - now_year) * price
            if best is None or gain > best[0]:
                best = (gain, o)

        if best is None:
            continue
        split = _split_plan(same, h.amount, price, now_year, best[0])
        if max(best[0], split["gain_year_usd"] if split else 0.0) < min_gain_year:
            continue
        gain, offer = best
        to_year = offer.per_year(h.amount) * price
        rows.append({
            "coin": h.coin,
            "amount": h.amount,
            "usd_value": h.amount * price,
            "from_exchange": h.exchange,
            "from_where": h.where,
            "from_product": h.product,
            "from_apr": now_apr,
            "from_year_usd": now_year * price,
            "to_exchange": offer.exchange,
            "to_kind": offer.kind,
            "to_apr": offer.apr,
            # Ставка на СХОДИНКАХ саме цієї суми. Базова 1.76% при першій сходинці
            # 6.76% розходилась із сумою в доларах, і виходило дві різні правди.
            "to_effective_apr": offer.per_year(h.amount) / h.amount,
            "to_year_usd": to_year,
            "gain_year_usd": gain,
            "same_exchange": offer.exchange == h.exchange,
            "risk": offer.risk or classify_risk(h.coin),
            "offer": offer,
            "split": split,
        })
    rows.sort(key=lambda r: -max(r["gain_year_usd"],
                                 (r["split"] or {}).get("gain_year_usd", 0.0)))
    return rows
