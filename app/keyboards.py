"""Інлайн-клавіатури: кожен ордер має власні кнопки «поставив / пропустити»."""
from typing import Dict, List

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.formatting import fmt_price


def flatten_rec(rec) -> List[dict]:
    """Плоский список ордерів з об'єкта рекомендації (той самий порядок, що й у тексті)."""
    res = []
    for cp in rec.coins:
        for o in cp.orders:
            res.append({"coin": cp.coin, "price": o.price, "qty": o.qty, "notional": o.notional})
    return res


def build_orders_kb(rec_id: int, flat: List[dict], decisions: Dict[int, str]) -> InlineKeyboardMarkup:
    rows = []
    for i, o in enumerate(flat):
        status = decisions.get(i)
        label = f"#{i + 1} {o['coin']} ${fmt_price(o['price'])}"
        if status == "open":
            rows.append([InlineKeyboardButton(text=f"✅ {label} — поставив", callback_data="noop")])
        elif status == "skipped":
            rows.append([InlineKeyboardButton(text=f"✖️ {label} — пропущено", callback_data="noop")])
        else:
            rows.append([
                InlineKeyboardButton(text=f"✅ {label}", callback_data=f"o:{rec_id}:{i}:y"),
                InlineKeyboardButton(text="✖️", callback_data=f"o:{rec_id}:{i}:n"),
            ])
    return InlineKeyboardMarkup(inline_keyboard=rows)
