"""Движок рішень: оцінка "дешево/дорого", розподіл BTC/ETH і "драбинка" ордерів.

Уся складність ховається тут. Назовні віддаємо просте рішення + готові ордери.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from app import indicators as ind
from app.config import config


@dataclass
class Order:
    coin: str
    price: float
    qty: float
    notional: float


@dataclass
class CoinPlan:
    coin: str
    price: float
    score: float
    zone: str
    mayer: Optional[float]
    orders: List[Order] = field(default_factory=list)


@dataclass
class Recommendation:
    moment: str  # buy | ok | pricey | wait
    headline: str
    amount: float
    fng: int
    coins: List[CoinPlan] = field(default_factory=list)
    note: str = ""

    def has_orders(self) -> bool:
        return any(cp.orders for cp in self.coins)


def _lin(x: Optional[float], lo: float, hi: float) -> Optional[float]:
    """Лінійна шкала: 100 при x<=lo (дешево), 0 при x>=hi (дорого)."""
    if x is None:
        return None
    if x <= lo:
        return 100.0
    if x >= hi:
        return 0.0
    return (hi - x) / (hi - lo) * 100.0


def _weighted(parts) -> float:
    num = 0.0
    den = 0.0
    for value, weight in parts:
        if value is None:
            continue
        num += value * weight
        den += weight
    return num / den if den else 50.0


def zone_label(score: float) -> str:
    if score >= 60:
        return "дешево"
    if score >= 45:
        return "норм"
    if score >= 33:
        return "дорогувато"
    return "дорого"


def fng_label(fng: int) -> str:
    if fng < 25:
        return "страх"
    if fng < 45:
        return "обережно"
    if fng < 55:
        return "нейтрально"
    if fng < 75:
        return "оптимізм"
    return "жадібність"


def _coin_score(d: Dict, fng: int):
    closes = d["closes"]
    price = d["price"]
    sma200 = ind.sma(closes, 200)
    rsi_val = ind.rsi(closes, 14)

    mayer = price / sma200 if sma200 else None
    mayer_score = _lin(mayer, 0.8, 2.4)
    rsi_score = _lin(rsi_val, 30, 70)
    fng_score = 100.0 - fng

    # Положення ціни в річному діапазоні: 0 = річне дно (добре), 1 = річний пік (дорого)
    annual_score: Optional[float] = None
    if closes:
        lo, hi = min(closes), max(closes)
        if hi > lo:
            annual_score = (1.0 - (price - lo) / (hi - lo)) * 100.0

    score = _weighted([
        (mayer_score, 0.35),
        (annual_score, 0.20),
        (rsi_score, 0.20),
        (fng_score, 0.25),
    ])
    return score, mayer


def _ladder(coin: str, price: float, amount: float, atr_pct: Optional[float],
            moment: str, rungs: int) -> List[Order]:
    if amount < config.min_notional:
        return []
    u = max(0.015, min(0.04, atr_pct if atr_pct else 0.03))
    if moment == "pricey":
        drops = [0.4 * u]
    else:
        drops = [1.0 * u, 2.2 * u, 3.6 * u][:rungs]

    n = len(drops)
    while n > 1 and amount / n < config.min_notional:
        n -= 1
    drops = drops[:n]
    per = amount / n

    orders: List[Order] = []
    for drop in drops:
        p = price * (1 - drop)
        p = round(p, 2) if p >= 1 else round(p, 6)
        qty = per / p
        orders.append(Order(coin=coin, price=p, qty=qty, notional=round(per, 2)))
    return orders


def analyze(data: Dict[str, Dict], fng: int, amount: float) -> Recommendation:
    raw = {}
    for coin, d in data.items():
        score, mayer = _coin_score(d, fng)
        raw[coin] = {"d": d, "score": score, "mayer": mayer}

    avg = sum(c["score"] for c in raw.values()) / len(raw)

    if avg >= config.good_moment_score:
        moment, mult, head = "buy", 1.2, "✅ Зараз добрий момент вкласти."
    elif avg >= 45:
        moment, mult, head = "ok", 1.0, "🟢 Можна потроху докуповувати."
    elif avg >= 33:
        moment, mult, head = "pricey", 0.4, "🟡 Дорогувато — лише невелика докупка."
    else:
        moment, mult, head = "wait", 0.0, "⏳ Зараз дорого — раджу почекати. Я напишу, коли стане вигідно."

    total = amount * mult
    weight_sum = sum(max(c["score"], 1.0) for c in raw.values())

    coins: List[CoinPlan] = []
    for coin, c in raw.items():
        d = c["d"]
        weight = max(c["score"], 1.0) / weight_sum
        coin_amount = total * weight
        vol = ind.volatility(d["closes"], 14)
        orders = _ladder(coin, d["price"], coin_amount, vol, moment, config.rungs) if moment != "wait" else []
        coins.append(CoinPlan(
            coin=coin,
            price=d["price"],
            score=round(c["score"], 1),
            zone=zone_label(c["score"]),
            mayer=round(c["mayer"], 2) if c["mayer"] else None,
            orders=orders,
        ))

    note = "(Нічого ставити не треба. Чекаємо кращих цін.)" if moment == "wait" else ""
    return Recommendation(moment=moment, headline=head, amount=amount, fng=fng, coins=coins, note=note)
