"""Інлайн-клавіатури: кожен ордер має власні кнопки «поставив / пропустити»."""
from typing import Dict, List

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.earn import CORE, STABLES
from app.formatting import fmt_price, fmt_usd


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
        label = f"#{i + 1} {o['coin']} ${fmt_price(o['price'])} — {fmt_usd(o['notional'])}"
        if status == "open":
            rows.append([InlineKeyboardButton(text=f"✅ {label} — поставив", callback_data="noop")])
        elif status == "skipped":
            rows.append([InlineKeyboardButton(text=f"✖️ {label} — пропущено", callback_data="noop")])
        else:
            # Ордер — велика кнопка на весь рядок; «пропустити» — маленька на піврядка.
            rows.append([InlineKeyboardButton(text=f"✅ {label}", callback_data=f"o:{rec_id}:{i}:y")])
            rows.append([
                InlineKeyboardButton(text="✖️ пропустити", callback_data=f"o:{rec_id}:{i}:n"),
                InlineKeyboardButton(text="⠀", callback_data="noop"),
            ])
    rows.append([InlineKeyboardButton(text="📋 Готово — надіслати звіт", callback_data=f"done:{rec_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_active_orders_kb(orders: List[dict]) -> InlineKeyboardMarkup:
    """Список активних ордерів — у кожного кнопки «виконався» і «скасувати»."""
    rows = []
    for o in orders:
        title = f"{o['coin']} ${fmt_price(o['price'])}"
        rows.append([
            InlineKeyboardButton(text=f"✅ {title} виконався", callback_data=f"mark:{o['id']}"),
            InlineKeyboardButton(text="❌", callback_data=f"cancel:{o['id']}"),
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_ibkr_kb(month: str, status: str | None) -> InlineKeyboardMarkup | None:
    """Кнопки під нагадуванням про купівлю ETF: «Зроблено» і «Пропустити цього місяця».

    Коли місяць уже закритий (зроблено/пропущено) — кнопок немає, лишається текст.
    """
    if status in ("done", "skipped"):
        return None
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Зроблено", callback_data=f"ibkr:done:{month}")],
        [InlineKeyboardButton(text="✖️ Пропустити цього місяця",
                              callback_data=f"ibkr:skip:{month}")],
    ])


# Скільки кнопок «не цікаво» показуємо під сповіщенням. Більше — це вже екран
# самих кнопок замість поради.
EARN_MUTE_BUTTONS = 4


def build_earn_mute_kb(storage, coins: List[str]) -> InlineKeyboardMarkup | None:
    """Кнопки «не цікаво» під earn-сповіщенням — по одній на згадану монету.

    У callback_data влазить лише 64 байти, а ціль може бути довгою (ключ акції —
    це повна назва на 80+ знаків), тому в кнопку йде короткий числовий id,
    а сама ціль лежить у сховищі.
    """
    rows = []
    # Стейбли й BTC/ETH не глушимо: це її основні гроші, і випадково вимкнути
    # поради по них означало б осліпнути там, де найважливіше бачити.
    for coin in [c for c in coins if c not in STABLES and c not in CORE][:EARN_MUTE_BUTTONS]:
        mute_id = storage.earn_mute_register(f"coin:{coin}")
        rows.append([InlineKeyboardButton(text=f"🔕 {coin} — не цікаво",
                                          callback_data=f"em:{mute_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def build_earn_kb() -> InlineKeyboardMarkup:
    """Кнопки під /earn. Запис вкладу — саме кнопкою, а не командою: команду
    треба памʼятати, а кнопка сама показує, що так узагалі можна."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ записати вклад", callback_data="ejnew")],
        [InlineKeyboardButton(text="📒 мій журнал вкладів", callback_data="ejlist")],
    ])


def build_journal_confirm_kb() -> InlineKeyboardMarkup:
    """Підтвердження розбору: у журнал нічого не потрапляє без цієї кнопки."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Так, все правильно", callback_data="ejc:ok")],
        [InlineKeyboardButton(text="✖️ Скасувати", callback_data="ejc:no")],
    ])


def build_journal_due_kb(dep_id: int) -> InlineKeyboardMarkup:
    """Строк вийшов: питаємо, що вона зробила. Самі запис не закриваємо."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ забрала", callback_data=f"ejd:{dep_id}:took")],
        [InlineKeyboardButton(text="🔄 продовжила", callback_data=f"ejd:{dep_id}:kept")],
    ])


def build_alt_topup_kb(coin: str, amounts: List[int]) -> InlineKeyboardMarkup:
    """Добровільне докидання своїх грошей у чужу монету — саме як опція.

    Основний шлях — залишок місячного бюджету; ці кнопки просто нагадують, що
    можна й самій, і мають жорстку межу (див. ALT_TOPUP_MAX).
    """
    rows = [[InlineKeyboardButton(text=f"➕ докинути ${a:g}",
                                  callback_data=f"alt:{coin}:{a:g}")]
            for a in amounts]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_fill_confirm_kb(orders: List[dict]) -> InlineKeyboardMarkup:
    """Підтвердження спрацювання: для кожного ордера «✅ виконався / ✖️ ні»."""
    rows = []
    for o in orders:
        label = f"{o['coin']} ${fmt_price(o['price'])}"
        rows.append([
            InlineKeyboardButton(text=f"✅ {label} — виконався", callback_data=f"fill:{o['id']}:y"),
            InlineKeyboardButton(text="✖️ ні", callback_data=f"fill:{o['id']}:n"),
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)
