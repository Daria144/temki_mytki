"""Движок рішень: оцінка "дешево/дорого", розподіл BTC/ETH і "драбинка" ордерів.

Уся складність ховається тут. Назовні віддаємо просте рішення + готові ордери.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from app import indicators as ind
from app.config import config

# Новий ордер по монеті має бути щонайменше на стільки % ГЛИБШЕ за найнижчий уже відкритий.
DUP_GAP_PCT = 2.0

# Наскільки глибше за НИНІШНЮ ціну взагалі має сенс ставити лімітку.
# Правило "лише глибше за старі заявки" саме по собі не тримається за ринок:
# поки старі висять, кожна наступна йде ще нижче, і драбинка тікає в підвал
# (у серпні по BTC доїхало до −13% від ринку — така заявка не мала шансів).
# Це стеля глибини, а не поріг стратегії: заявка далі неї просто не ставиться.
MAX_DEPTH_PCT = 8.0

# Що сказати, коли всі рівні по монеті не пройшли перевірку глибини/дублювання.
TOO_DEEP_NOTE = (
    "по {coin} зараз нову заявку не ставлю: щоб не повторювати вже виставлені, "
    "вона мала б бути надто далеко від нинішньої ціни — там її б ніхто не виконав"
)


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
    blocked_note: str = ""     # чому по цій монеті нічого не ставимо (людською мовою)


@dataclass
class Recommendation:
    moment: str  # buy | ok | pricey | wait  (AI: buy | accumulate | cautious | wait)
    headline: str
    amount: float
    fng: int
    coins: List[CoinPlan] = field(default_factory=list)
    note: str = ""
    reasoning: str = ""        # пояснення трейдера (AI) людською мовою
    source: str = "formula"    # formula | ai

    def has_orders(self) -> bool:
        return any(cp.orders for cp in self.coins)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


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


def guard_orders(coin: str, price: float, orders: List[Order],
                 open_by_coin: Optional[Dict] = None,
                 budget_cap: Optional[float] = None):
    """Єдиний набір перевірок безпеки для будь-якої драбинки — і від AI, і від формули.

    Раніше ці правила діяли лише в AI-гілці, тож у формульному режимі бот міг
    поставити заявку впритул до вже відкритої (9 серпня саме так і сталось).
    Тепер обидва шляхи проходять тут, і правила однакові.

    Повертає (ордери, що лишились; пояснення для користувачки — або порожній рядок).
    """
    if not orders:
        return [], ""

    # 1. Стеля глибини: заявка далі MAX_DEPTH_PCT від нинішньої ціни просто не спрацює.
    floor = price * (1 - MAX_DEPTH_PCT / 100.0)
    kept = [o for o in orders if o.price >= floor]

    # 2. Не дублюємо вже виставлене: нові рівні лише помітно глибше за найнижчу відкриту лімітку.
    by_coin = open_by_coin or {}
    existing = by_coin.get(coin) or by_coin.get(coin.upper())
    if existing and existing.get("min_price"):
        ceiling = float(existing["min_price"]) * (1 - DUP_GAP_PCT / 100.0)
        kept = [o for o in kept if o.price < ceiling]

    # Якщо саме ці два правила лишили нас ні з чим — заявку не вигадуємо, а пояснюємо.
    note = TOO_DEEP_NOTE.format(coin=coin) if not kept else ""

    # 3. Дрібні ордери не варті комісій і мороки.
    kept = [o for o in kept if o.notional >= config.min_order]

    # 4. Межа бюджету на цей раунд. Допуск у 5 центів — на округлення сходинок.
    if budget_cap is not None:
        allowed, spent = [], 0.0
        for o in kept:
            if spent + o.notional > budget_cap + 0.05:
                continue
            allowed.append(o)
            spent += o.notional
        kept = allowed

    return kept, note


def _ladder(coin: str, price: float, amount: float, atr_pct: Optional[float],
            moment: str, rungs: int) -> List[Order]:
    if amount < config.min_order:
        return []
    u = max(0.015, min(0.04, atr_pct if atr_pct else 0.03))
    if moment == "pricey":
        drops = [0.4 * u]
    else:
        drops = [1.0 * u, 2.2 * u, 3.6 * u][:rungs]

    n = len(drops)
    while n > 1 and amount / n < config.min_order:
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


def analyze(data: Dict[str, Dict], fng: int, amount: float,
            open_by_coin: Optional[Dict] = None) -> Recommendation:
    """Запасна формула, коли порадник мовчить.

    open_by_coin потрібен, щоб формула жила за тими самими правилами безпеки,
    що й AI: без нього вона ставила заявки поверх уже виставлених.
    """
    raw = {}
    for coin, d in data.items():
        score, mayer = _coin_score(d, fng)
        raw[coin] = {"d": d, "score": score, "mayer": mayer}

    avg = sum(c["score"] for c in raw.values()) / len(raw)

    if avg >= config.good_moment_score:
        moment, mult, head = "buy", 1.0, "✅ Зараз добрий момент вкласти."
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
        orders, blocked = guard_orders(coin, d["price"], orders, open_by_coin, coin_amount)
        coins.append(CoinPlan(
            coin=coin,
            price=d["price"],
            score=round(c["score"], 1),
            zone=zone_label(c["score"]),
            mayer=round(c["mayer"], 2) if c["mayer"] else None,
            orders=orders,
            blocked_note=blocked,
        ))

    note = "(Нічого ставити не треба. Чекаємо кращих цін.)" if moment == "wait" else ""
    return Recommendation(moment=moment, headline=head, amount=amount, fng=fng, coins=coins, note=note)


def coin_snapshot(data: Dict[str, Dict], fng: int) -> List[Dict]:
    """Готує 'торговий екран' для AI: індикатори по кожній монеті (читабельні числа)."""
    out: List[Dict] = []
    for coin, d in data.items():
        closes = d["closes"]
        price = d["price"]
        sma200 = ind.sma(closes, 200)
        rsi_val = ind.rsi(closes, 14)
        vol = ind.volatility(closes, 14)
        mayer = (price / sma200) if sma200 else None
        lo = min(closes) if closes else None
        hi = max(closes) if closes else None
        annual_pos = ((price - lo) / (hi - lo)) if (lo is not None and hi is not None and hi > lo) else None

        # Просадка від піку останніх 30 днів (0 = на піку, від'ємне = нижче піку)
        recent = closes[-30:] if closes else []
        pullback_30d = ((price / max(recent)) - 1.0) * 100.0 if recent and max(recent) > 0 else None

        # Тренд: зміна ціни за 30 і 90 днів у % (напрямок руху).
        # closes[-(n+1)] — ціна приблизно n днів тому. Стійко до короткої історії.
        def _trend(days: int) -> Optional[float]:
            if not closes or len(closes) <= days:
                return None
            past = closes[-(days + 1)]
            return ((price / past) - 1.0) * 100.0 if past else None

        trend_30d = _trend(30)
        trend_90d = _trend(90)

        # Відхилення від 200-тижневої середньої (довгий цикл); weekly_closes може не бути
        weekly = d.get("weekly_closes") or []
        sma200w = ind.sma(weekly, 200)
        dist_200w = ((price / sma200w) - 1.0) * 100.0 if sma200w else None

        out.append({
            "coin": coin.upper(),
            "price": round(price, 2),
            "change_24h_pct": round(d["change_24h"] * 100, 2) if d.get("change_24h") is not None else None,
            "sma200": round(sma200, 2) if sma200 else None,
            "mayer": round(mayer, 3) if mayer else None,
            "rsi14": round(rsi_val, 1) if rsi_val is not None else None,
            "annual_low": round(lo, 2) if lo is not None else None,
            "annual_high": round(hi, 2) if hi is not None else None,
            "annual_pos_pct": round(annual_pos * 100, 1) if annual_pos is not None else None,
            "pullback_30d_pct": round(pullback_30d, 2) if pullback_30d is not None else None,
            "trend_30d_pct": round(trend_30d, 2) if trend_30d is not None else None,
            "trend_90d_pct": round(trend_90d, 2) if trend_90d is not None else None,
            "dist_from_sma200w_pct": round(dist_200w, 1) if dist_200w is not None else None,
            "daily_volatility_pct": round(vol * 100, 2) if vol is not None else None,
        })
    return out


def _ladder_from_drops(coin: str, price: float, amount: float, drops_pct: List[float]) -> List[Order]:
    """Будує ордери з відсотків просадки, які запропонував AI. Ціни рахує бот (не AI)."""
    if amount < config.min_order or not drops_pct:
        return []
    # Безпека: кожна просадка в межах 0.2%..15%, не більше 5 сходинок
    drops = sorted({_clamp(x, 0.2, 15.0) for x in drops_pct})[:5]
    n = len(drops)
    while n > 1 and amount / n < config.min_order:
        n -= 1
    drops = drops[:n]
    per = amount / n
    orders: List[Order] = []
    for drop in drops:
        p = price * (1 - drop / 100.0)
        p = round(p, 2) if p >= 1 else round(p, 6)
        qty = per / p
        orders.append(Order(coin=coin, price=p, qty=qty, notional=round(per, 2)))
    return orders


def build_from_ai(data: Dict[str, Dict], fng: int, amount: float, decision: Dict,
                  open_by_coin: Optional[Dict] = None, depth_factor: float = 1.0) -> Recommendation:
    """Перетворює рішення AI-трейдера (відсотки) у реальну рекомендацію з ордерами.

    AI задає СТРАТЕГІЮ (скільки вкласти, розподіл, глибину сходинок), бот рахує точні ціни.
    open_by_coin: вже відкриті ордери по монетах — нові рівні ставимо ЛИШЕ глибше (запобіжник).
    depth_factor: множник глибини сходинок з історії навчання (<1 ближче, >1 глибше).
    """
    open_by_coin = open_by_coin or {}
    try:
        depth_factor = float(depth_factor) or 1.0
    except (TypeError, ValueError):
        depth_factor = 1.0
    moment = str(decision.get("moment", "wait"))
    headline = str(decision.get("headline", "")).strip() or "Рекомендація на основі аналізу ринку."
    deploy = _clamp(float(decision.get("deploy_now_pct", 0) or 0), 0.0, 100.0) / 100.0
    total_now = amount * deploy

    coins_dec = decision.get("coins", []) or []
    weight_sum = sum(max(float(c.get("weight_pct", 0) or 0), 0.0) for c in coins_dec) or 1.0

    # Швидкий доступ до рішення по монеті
    by_coin = {str(c.get("coin", "")).upper(): c for c in coins_dec}

    # Розумна концентрація: якщо деплою замало на осмислений ордер у КОЖНІЙ монеті,
    # не розмазуємо по обох — кладемо все в монету з найбільшою вагою.
    focus_coin: Optional[str] = None
    active_coins = [c for c in coins_dec if max(float(c.get("weight_pct", 0) or 0), 0.0) > 0]
    if total_now < len(active_coins) * config.min_order and active_coins:
        focus_coin = max(
            active_coins,
            key=lambda c: float(c.get("weight_pct", 0) or 0),
        )
        focus_coin = str(focus_coin.get("coin", "")).upper()

    coins: List[CoinPlan] = []
    for coin, d in data.items():
        coin = coin.upper()
        price = d["price"]
        score, mayer = _coin_score(d, fng)
        cdec = by_coin.get(coin, {})
        if focus_coin:
            weight = 1.0 if coin == focus_coin else 0.0
        else:
            weight = max(float(cdec.get("weight_pct", 0) or 0), 0.0) / weight_sum
        coin_amount = total_now * weight
        drops = [float(x) for x in (cdec.get("ladder_drops_pct") or []) if isinstance(x, (int, float))]
        # При концентрації монета може не мати власних сходинок від AI — даємо одну розумну.
        if focus_coin and coin == focus_coin and not drops:
            vol = ind.volatility(d.get("closes") or [], 14)
            drops = [round(max(0.5, min(3.0, (vol or 0.02) * 100)), 2)]
        # Авто-корекція глибини з історії навчання.
        if depth_factor != 1.0 and drops:
            drops = [round(x * depth_factor, 2) for x in drops]
        orders = _ladder_from_drops(coin, price, coin_amount, drops) if moment != "wait" else []
        orders, blocked = guard_orders(coin, price, orders, open_by_coin, coin_amount)
        coins.append(CoinPlan(
            coin=coin,
            price=price,
            score=round(score, 1),
            zone=zone_label(score),
            mayer=round(mayer, 2) if mayer else None,
            orders=orders,
            blocked_note=blocked,
        ))

    return Recommendation(
        moment=moment,
        headline=headline,
        amount=amount,
        fng=fng,
        coins=coins,
        note="",
        reasoning=str(decision.get("reasoning", "")).strip(),
        source="ai",
    )
