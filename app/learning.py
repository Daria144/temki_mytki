"""Памʼять і навчання на історії докупок.

Зберігаємо кожну виконану докупку та оцінюємо її через 30 і 90 днів:
- докупка ВДАЛА, якщо ОБИДВА: (а) ціна після покупки зросла на горизонті оцінки;
  (б) купили дешевше за середню ціну монети за цей період.
Невиконані ордери, де ціна втекла вгору (runaway) — це урок 'впущена можливість'
(ставили лімітку занадто низько).

Підсумок використовується і для звіту користувачу (/history), і як підказка AI-трейдеру.
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional

DAY = 86400
HORIZONS = (30, 90)


def _avg(values: List[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def _window(d: Dict, start_ms: int, end_ms: int):
    """Ціни (closes) монети у вікні [start_ms, end_ms] і ціна найближче до end_ms."""
    ts = d.get("ts", []) or []
    closes = d.get("closes", []) or []
    win = [c for t, c in zip(ts, closes) if start_ms <= t <= end_ms]
    price_at_end = None
    best = None
    for t, c in zip(ts, closes):
        if t <= end_ms:
            if best is None or t > best:
                best = t
                price_at_end = c
    return win, price_at_end


def evaluate_orders(storage, data_by_coin: Dict[str, dict]) -> int:
    """Рахує win/loss для виконаних ордерів, у яких горизонт уже минув. Повертає к-сть оцінених."""
    now = int(time.time())
    done = 0
    for horizon in HORIZONS:
        before = now - horizon * DAY  # горизонт минув, якщо filled_at <= now - horizon
        for o in storage.orders_to_evaluate(horizon, before):
            d = data_by_coin.get(o["coin"]) or data_by_coin.get(o["coin"].upper())
            if not d:
                continue
            entry = o.get("fill_price") or o.get("price")
            if not entry:
                continue
            start_ms = int(o["filled_at"]) * 1000
            end_ms = start_ms + horizon * DAY * 1000
            win_prices, price_at_end = _window(d, start_ms, end_ms)
            avg = _avg(win_prices)
            if avg is None or price_at_end is None:
                continue  # недостатньо історії — оцінимо пізніше
            grew = price_at_end > entry          # (а) ціна зросла після покупки
            cheaper = entry < avg                # (б) купили дешевше за середню
            win = 1 if (grew and cheaper) else 0
            entry_vs_avg = round((entry / avg - 1.0) * 100, 2)  # <0 = нижче середньої (добре)
            storage.set_eval(o["id"], horizon, win, entry_vs_avg, now)
            done += 1
    return done


def summary(storage) -> dict:
    """Зведення навчання для звіту й AI."""
    rows = storage.learning_rows()
    filled = [r for r in rows if r["status"] == "filled"]
    missed = [r for r in rows if r["status"] == "missed" or (r.get("runaway") and r["status"] != "filled")]
    missed_count = len({r["id"] for r in missed})

    def _stats(horizon: int):
        col, dev = f"eval{horizon}", f"entry_vs_avg{horizon}"
        ev = [r for r in filled if r.get(col) is not None]
        wins = sum(1 for r in ev if r[col] == 1)
        devs = [r[dev] for r in ev if r.get(dev) is not None]
        return {
            "evaluated": len(ev),
            "wins": wins,
            "winrate": (wins / len(ev)) if ev else None,
            "avg_entry_vs_avg": _avg(devs),  # <0 = входили дешевше за середню
        }

    s30 = _stats(30)
    s90 = _stats(90)

    filled_count = len(filled)
    total_attempts = filled_count + missed_count
    missed_ratio = (missed_count / total_attempts) if total_attempts else 0.0

    lessons = _lessons(filled_count, missed_count, missed_ratio, s30)

    return {
        "filled_count": filled_count,
        "missed_count": missed_count,
        "missed_ratio": missed_ratio,
        "h30": s30,
        "h90": s90,
        "lessons": lessons,
        "has_data": filled_count > 0 or missed_count > 0,
    }


def _lessons(filled_count: int, missed_count: int, missed_ratio: float, s30: dict) -> List[str]:
    """Короткі висновки простою мовою (без крипто-жаргону)."""
    out: List[str] = []
    if filled_count == 0 and missed_count == 0:
        return ["Поки що мало історії — статистика накопичується."]

    if missed_count >= 2 and missed_ratio >= 0.3:
        out.append(
            "Часто ставимо ціну надто низько — ринок встигає піти вгору, і докупка не відбувається. "
            "Варто інколи брати трохи ближче до поточної ціни."
        )

    wr = s30["winrate"]
    dev = s30["avg_entry_vs_avg"]
    if wr is not None and s30["evaluated"] >= 3:
        if wr < 0.5:
            out.append("Частина докупок була не дуже вдалою — інколи беремо ще не на самому дні.")
        elif wr >= 0.7:
            out.append("Загалом докупки вдалі — здебільшого вгадуємо з моментом.")

    if dev is not None and s30["evaluated"] >= 3:
        if dev > 1:
            out.append("Буває, докуповуємо трохи дорожче за середню ціну — варто більше терпіння.")
        elif dev < -1:
            out.append("У середньому входимо дешевше за середню ціну — це добре.")

    if not out:
        out.append("Поки що все рівно — статистика ще накопичується.")
    return out


def depth_factor(storage) -> float:
    """Множник глибини сходинок драбинки на основі історії (замкнений цикл навчання).

    <1 — ставимо БЛИЖЧЕ до ціни (бо часто 'впущені можливості': лімітки не спрацьовують);
    >1 — ставимо ГЛИБШЕ (бо входимо дорожче за середню — треба більше терпіння);
    1.0 — без змін. Обмежено діапазоном 0.6..1.4, щоб не кидало в крайнощі.
    """
    s = summary(storage)
    if not s["has_data"]:
        return 1.0
    factor = 1.0
    # Часто не встигаємо купити → тягнемо ордери ближче до ринку.
    if s["missed_count"] >= 2 and s["missed_ratio"] >= 0.3:
        factor *= 0.75
    # Входимо дорожче за середню → ставимо глибше (терплячіше).
    dev = s["h30"].get("avg_entry_vs_avg")
    if dev is not None and s["h30"]["evaluated"] >= 3:
        if dev > 1:
            factor *= 1.2
        elif dev < -1:
            factor *= 0.9  # і так входимо дешево — можна трохи ближче, не жадувати глибини
    return round(max(0.6, min(1.4, factor)), 2)


def ai_hint(storage) -> Optional[str]:
    """Компактний рядок з уроками для промпта AI. None, якщо історії ще нема."""
    s = summary(storage)
    if not s["has_data"]:
        return None
    parts = []
    h30 = s["h30"]
    if h30["evaluated"]:
        wr = int(round(h30["winrate"] * 100))
        parts.append(f"вдалих докупок {h30['wins']}/{h30['evaluated']} (на горизонті 30 днів, {wr}%)")
    if s["missed_count"]:
        parts.append(f"впущених можливостей (ставили ліміт надто низько): {s['missed_count']}")
    if h30["avg_entry_vs_avg"] is not None:
        dev = h30["avg_entry_vs_avg"]
        if dev < 0:
            parts.append(f"в середньому входимо на {abs(dev):.1f}% дешевше за середню ціну")
        else:
            parts.append(f"в середньому входимо на {dev:.1f}% дорожче за середню ціну")
    if not parts:
        return None
    return "Уроки з минулих докупок: " + "; ".join(parts) + "."
