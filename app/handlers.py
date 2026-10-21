"""Команди Telegram-бота."""
import datetime
import json
import logging
import time
from dataclasses import dataclass

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
)

from app import budget, ibkr, income, learning
from app.config import config
from app.earn import ALT_TOPUP_MAX, WATCHED, alt_position, best_instant_rates
from app.formatting import (
    NO_ADVISOR_NOTE,
    deposit_line,
    deposits_word,
    fmt_eur,
    fmt_price,
    fmt_units,
    fmt_usd,
    format_earn,
    format_recommendation,
    has_quote,
    journal_confirm_text,
    journal_list_text,
    split_message,
    to_html,
)
from app.keyboards import (
    build_active_orders_kb,
    build_earn_kb,
    build_fill_confirm_kb,
    build_ibkr_kb,
    build_journal_confirm_kb,
    build_journal_due_kb,
    build_orders_kb,
    flatten_rec,
)
from app.service import (
    actual_allocation,
    gather,
    parse_deposits_text,
    promo_advice,
    recommend,
    target_allocation,
)
from app.status import money_snapshot, status_messages
from app.storage import STATUS_ACTIVE, flat_orders

log = logging.getLogger(__name__)
router = Router()


@dataclass
class Services:
    storage: object
    market: object
    earn: object = None
    portfolio: object = None   # читання балансів бірж; None, якщо ключі не задані
    etf: object = None         # ціни ETF на Xetra (євро); None = працюємо без цін
    coin_facts: object = None  # довідка про незнайомі монети; None = блок просто мовчить


def _allowed(user_id: int) -> bool:
    return config.owner_id == 0 or user_id == config.owner_id


HELP = (
    "Привіт! Я підказую, коли і якими лімітними ордерами докуповувати BTC/ETH "
    "(стратегія — довгострокове накопичення).\n\n"
    "/invest [сума] — порада купити зараз (без суми бере порцію з місячного бюджету)\n"
    "/budget <сума> — місячний бюджет (бот плавно вкладе за місяць; /budget +50 — долити)\n"
    "/orders — мої активні ордери\n"
    "/portfolio — мій портфель, прибуток і висновки по докупках\n"
    "/status — повний стан моїх грошей одним текстом: скопіювати й вставити в чат "
    "з AI, щоб порада була про МОЇ цифри, а не загальна\n"
    "/earn [сума] — куди покласти вільні гроші під відсоток\n"
    "     ➕ під /earn є кнопка «записати вклад»: я запамʼятаю строк і ставку, "
    "під яку ти заходила, і нагадаю в день розморозки\n"
    "/allocation — бажана пропорція BTC/ETH (напр. /allocation 60 40)\n"
    "/ibkr — щомісячна купівля ETF на IBKR (окремо від крипти, у євро)\n"
    "/alerts on|off — сповіщення"
)


@router.message(Command("start"))
async def cmd_start(message: Message, services: Services):
    if not _allowed(message.from_user.id):
        await message.answer(
            f"Цей бот приватний. Твій Telegram ID: {message.from_user.id}\n"
            "Впиши його у OWNER_ID в .env, щоб користуватись."
        )
        return
    await message.answer(HELP + f"\n\nТвій Telegram ID: {message.from_user.id}")


@router.message(Command("invest"))
async def cmd_invest(message: Message, command: CommandObject, services: Services):
    if not _allowed(message.from_user.id):
        return
    amount: float | None = None
    if command.args:
        try:
            amount = float(command.args.replace(",", ".").strip())
        except ValueError:
            await message.answer("Сума не зрозуміла. Приклад: /invest 200")
            return
        if amount <= 0:
            await message.answer("Сума має бути більшою за 0. Приклад: /invest 200")
            return
    else:
        # Без суми: якщо є місячний бюджет — бот сам візьме розумну порцію з нього.
        st = budget.status(services.storage)
        if not st["active"]:
            await message.answer(
                "Вкажи суму. Приклад: /invest 200\n"
                "Або задай місячний бюджет (/budget 300) — тоді я сам пропонуватиму, "
                "скільки і коли ставити."
            )
            return
        if st["remaining"] <= 0:
            await message.answer(
                f"Місячний бюджет уже вичерпано (всього {fmt_usd(st['total'])}). "
                "Можеш докупити вручну: /invest <сума>."
            )
            return
    await message.answer("Аналізую ринок…")
    try:
        rec, rec_id = await recommend(services, amount)
    except Exception as e:  # noqa: BLE001
        log.exception("invest failed")
        await message.answer(f"Не вдалось отримати дані ринку: {e}")
        return

    text = format_recommendation(rec)

    st = budget.status(services.storage)
    if st["active"]:
        note = f"\n\n💼 Місячний бюджет: залишок {fmt_usd(st['remaining'])} з {fmt_usd(st['total'])}."
        if amount is None:
            note += f"\nЦього разу розглядаю порцію {fmt_usd(rec.amount)} (темп на місяць)."
        elif amount > st["remaining"] and st["remaining"] > 0:
            note += f"\n⚠️ Ти просиш більше, ніж лишилось у бюджеті ({fmt_usd(st['remaining'])})."
        text += note

    keyboard = None
    if rec.has_orders():
        keyboard = build_orders_kb(rec_id, flatten_rec(rec), {})
    extra = {"parse_mode": "HTML"} if has_quote(text) else {}
    await message.answer(to_html(text) if extra else text, reply_markup=keyboard, **extra)


@router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery, services: Services):
    await callback.answer()


@router.callback_query(F.data.startswith("o:"))
async def cb_order(callback: CallbackQuery, services: Services):
    if not _allowed(callback.from_user.id):
        await callback.answer()
        return
    try:
        _, rec_id_s, idx_s, decision = callback.data.split(":")
        rec_id = int(rec_id_s)
        idx = int(idx_s)
    except ValueError:
        await callback.answer()
        return

    status = "open" if decision == "y" else "skipped"
    order = services.storage.set_order_decision(rec_id, idx, status)

    payload = services.storage.get_recommendation(rec_id)
    flat = flat_orders(payload) if payload else []
    decisions = services.storage.get_decisions(rec_id)
    try:
        await callback.message.edit_reply_markup(
            reply_markup=build_orders_kb(rec_id, flat, decisions)
        )
    except Exception:  # noqa: BLE001
        pass

    if order and status == "open":
        await callback.answer(f"Записав: {order['coin']} ${fmt_price(order['price'])}")
    else:
        await callback.answer("Пропущено")


@router.callback_query(F.data.startswith("done:"))
async def cb_done(callback: CallbackQuery, services: Services):
    if not _allowed(callback.from_user.id):
        await callback.answer()
        return
    try:
        rec_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await callback.answer()
        return

    payload = services.storage.get_recommendation(rec_id)
    flat = flat_orders(payload) if payload else []
    decisions = services.storage.get_decisions(rec_id)

    confirmed = [(i, o) for i, o in enumerate(flat) if decisions.get(i) == "open"]
    skipped   = [(i, o) for i, o in enumerate(flat) if decisions.get(i) == "skipped"]
    pending   = [(i, o) for i, o in enumerate(flat) if decisions.get(i) not in ("open", "skipped")]

    total = sum(o["notional"] for _, o in confirmed)

    out = ["📋 *Звіт по ордерах*", ""]
    if confirmed:
        out.append("*Поставив на Bybit:*")
        for i, o in confirmed:
            out.append(f"  ✅ #{i+1} {o['coin']} по ${fmt_price(o['price'])} ({fmt_usd(o['notional'])})")
        out.append(f"  Разом: {fmt_usd(total)}")
    if skipped:
        out.append("")
        out.append("*Пропущено:*")
        for i, o in skipped:
            out.append(f"  ✖️ #{i+1} {o['coin']} по ${fmt_price(o['price'])}")
    if pending:
        out.append("")
        out.append("*Не вирішено:*")
        for i, o in pending:
            out.append(f"  ⬜ #{i+1} {o['coin']} по ${fmt_price(o['price'])}")

    if not confirmed and not skipped:
        out.append("Жодного рішення не прийнято.")

    await callback.message.answer("\n".join(out), parse_mode="Markdown")
    await callback.answer()


@router.message(Command("orders"))
async def cmd_orders(message: Message, services: Services):
    if not _allowed(message.from_user.id):
        return
    rows = services.storage.open_orders()
    if not rows:
        await message.answer("Немає активних ордерів у журналі.")
        return
    total = 0.0
    by_coin: dict = {}
    out = ["Твої активні ордери:", ""]
    for r in rows:
        when = datetime.datetime.fromtimestamp(r["created_at"]).strftime("%d.%m.%Y %H:%M")
        out.append(
            f"• {r['coin']} по ${fmt_price(r['price'])} ({fmt_usd(r['notional'])}) — {when}"
        )
        total += r["notional"]
        by_coin[r["coin"]] = by_coin.get(r["coin"], 0.0) + r["notional"]
    out.append("")
    parts = ", ".join(f"{c} {fmt_usd(v)}" for c, v in by_coin.items())
    out.append(f"💰 Разом: {fmt_usd(total)} ({parts})")
    out.append("")
    out.append("✅ — позначити виконаним, ❌ — скасувати:")
    await message.answer("\n".join(out), reply_markup=build_active_orders_kb(rows))


@router.callback_query(F.data.startswith("cancel:"))
async def cb_cancel(callback: CallbackQuery, services: Services):
    """Перший крок: питаємо підтвердження, щоб не скасувати випадково."""
    if not _allowed(callback.from_user.id):
        await callback.answer()
        return
    try:
        order_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await callback.answer()
        return

    order = services.storage.get_order(order_id)
    if not order or order["status"] != "open":
        await callback.answer("Цей ордер уже не активний")
        rows = services.storage.open_orders()
        try:
            await callback.message.edit_reply_markup(
                reply_markup=build_active_orders_kb(rows) if rows else None
            )
        except Exception:  # noqa: BLE001
            pass
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=f"✅ Так, скасувати {order['coin']} ${fmt_price(order['price'])}",
            callback_data=f"cancelyes:{order_id}",
        ),
    ], [
        InlineKeyboardButton(text="↩️ Ні, залишити", callback_data="cancelno"),
    ]])
    try:
        await callback.message.edit_reply_markup(reply_markup=kb)
    except Exception:  # noqa: BLE001
        pass
    await callback.answer()


@router.callback_query(F.data == "cancelno")
async def cb_cancel_no(callback: CallbackQuery, services: Services):
    if not _allowed(callback.from_user.id):
        await callback.answer()
        return
    rows = services.storage.open_orders()
    try:
        await callback.message.edit_reply_markup(
            reply_markup=build_active_orders_kb(rows) if rows else None
        )
    except Exception:  # noqa: BLE001
        pass
    await callback.answer("Залишила")


@router.callback_query(F.data.startswith("cancelyes:"))
async def cb_cancel_yes(callback: CallbackQuery, services: Services):
    """Другий крок: підтверджене скасування."""
    if not _allowed(callback.from_user.id):
        await callback.answer()
        return
    try:
        order_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await callback.answer()
        return

    order = services.storage.get_order(order_id)
    if not order or order["status"] != "open":
        await callback.answer("Цей ордер уже не активний")
        rows = services.storage.open_orders()
        try:
            await callback.message.edit_reply_markup(
                reply_markup=build_active_orders_kb(rows) if rows else None
            )
        except Exception:  # noqa: BLE001
            pass
        return

    # Якщо ціна вже втекла вгору (runaway) — це 'впущена можливість' (урок), інакше звичайне скасування.
    if order.get("runaway"):
        services.storage.mark_missed(order_id)
        note = "Скасовано (ціна втекла — врахую як урок)"
    else:
        services.storage.mark_cancelled(order_id)
        note = f"Скасовано: {order['coin']} ${fmt_price(order['price'])}"
    rows = services.storage.open_orders()
    kb = build_active_orders_kb(rows) if rows else None
    try:
        await callback.message.edit_reply_markup(reply_markup=kb)
    except Exception:  # noqa: BLE001
        pass
    await callback.answer(note)


def _fill_detail(order: dict) -> str:
    """Рядок для підтвердження докупки: монета, ціна і скільки USDT вкладено."""
    price = order["price"]
    notional = order["notional"]
    text = f"{order['coin']} по ${fmt_price(price)} — вклала {fmt_usd(notional)}"
    if price:
        qty = notional / price
        text += f" (≈ {qty:.6g} {order['coin']})"
    return text


@router.callback_query(F.data.startswith("mark:"))
async def cb_mark_filled(callback: CallbackQuery, services: Services):
    """Користувач сам позначив ордер виконаним → записуємо й одразу дивимось ринок."""
    if not _allowed(callback.from_user.id):
        await callback.answer()
        return
    try:
        order_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await callback.answer()
        return

    order = services.storage.get_order(order_id)
    if not order or order["status"] not in ("open", "pending_confirm"):
        await callback.answer("Цей ордер уже не активний")
        rows = services.storage.open_orders()
        try:
            await callback.message.edit_reply_markup(
                reply_markup=build_active_orders_kb(rows) if rows else None
            )
        except Exception:  # noqa: BLE001
            pass
        return

    services.storage.mark_user_filled(order_id, int(time.time()))
    rows = services.storage.open_orders()
    try:
        await callback.message.edit_reply_markup(
            reply_markup=build_active_orders_kb(rows) if rows else None
        )
    except Exception:  # noqa: BLE001
        pass
    detail = _fill_detail(order)
    await callback.answer(f"Записала: {order['coin']} {fmt_usd(order['notional'])}")

    # Одразу аналізуємо ринок; пропонуємо НОВИЙ ордер лише якщо момент реально гарний.
    try:
        rec, rec_id = await recommend(services)
    except Exception:  # noqa: BLE001
        log.exception("mark_filled recommend failed")
        await callback.message.answer(f"✅ Записала докупку: {detail}.")
        return

    if rec.moment == "buy" and rec.has_orders():
        text = f"✅ Записала докупку: {detail}.\nІ саме зараз гарний момент — можна поставити ще:\n\n"
        text += format_recommendation(rec)
        st = budget.status(services.storage)
        if st["active"]:
            text += f"\n\n💼 Порція {fmt_usd(rec.amount)}, залишок бюджету {fmt_usd(st['remaining'])}."
        keyboard = build_orders_kb(rec_id, flatten_rec(rec), {})
        extra = {"parse_mode": "HTML"} if has_quote(text) else {}
        await callback.message.answer(to_html(text) if extra else text, reply_markup=keyboard, **extra)
    else:
        await callback.message.answer(
            f"✅ Записала докупку: {detail}.\nЗараз докуповувати ще не варто — почекаємо кращої ціни, "
            "я напишу, коли зʼявиться гарний момент."
        )


@router.callback_query(F.data.startswith("fill:"))
async def cb_fill(callback: CallbackQuery, services: Services):
    """Підтвердження/відхилення авто-визначеного спрацювання ордера."""
    if not _allowed(callback.from_user.id):
        await callback.answer()
        return
    try:
        _, order_id_s, decision = callback.data.split(":")
        order_id = int(order_id_s)
    except ValueError:
        await callback.answer()
        return

    order = services.storage.get_order(order_id)
    if not order or order["status"] != "pending_confirm":
        await callback.answer("Цей ордер уже опрацьований")
    elif decision == "y":
        services.storage.confirm_fill(order_id)
        await callback.answer(f"Записала: {order['coin']} {fmt_usd(order['notional'])}")
        await callback.message.answer(f"✅ Записала докупку: {_fill_detail(order)}.")
    else:
        services.storage.decline_fill(order_id, int(time.time()))
        await callback.answer("Ок, ордер досі активний")

    pending = services.storage.pending_confirm_orders()
    kb = build_fill_confirm_kb(pending) if pending else None
    try:
        await callback.message.edit_reply_markup(reply_markup=kb)
    except Exception:  # noqa: BLE001
        pass


def _budget_text(st: dict) -> str:
    if not st["active"]:
        lines = ["📅 Місячний бюджет ще не заданий на цей місяць."]
        if st["carry"] > 0:
            lines.append(f"Перенесений залишок з минулого: {fmt_usd(st['carry'])} (додам зверху).")
        lines.append("Задати: /budget 300")
        return "\n".join(lines)
    pct = int(st["deployed"] / st["total"] * 100) if st["total"] else 0
    lines = [
        f"📅 Місячний бюджет ({st['month']}):",
        f"• Всього: {fmt_usd(st['total'])}",
        f"• Реально куплено: {fmt_usd(st['deployed'])} ({pct}%)",
    ]
    # Заявки — це ще не витрачені гроші, тому окремим рядком і не в «куплено».
    if st.get("reserved", 0) > 0:
        lines.append(
            f"• Висить у заявках: {fmt_usd(st['reserved'])} (гроші поки на місці)"
        )
    lines += [
        f"• Залишок: {fmt_usd(st['remaining'])}",
        f"• Днів до кінця місяця: {st['days_left']}",
    ]
    if st["on_track"]:
        lines.append("✅ Темп нормальний — встигаємо.")
    elif st["behind"] > 0:
        lines.append(f"⏳ Трохи відстаємо від плану (~{fmt_usd(st['behind'])}). Доберемо на гарних цінах.")
    return "\n".join(lines)


@router.message(Command("budget"))
async def cmd_budget(message: Message, command: CommandObject, services: Services):
    if not _allowed(message.from_user.id):
        return
    budget.rollover_if_needed(services.storage)
    if not command.args:
        st = budget.status(services.storage)
        await message.answer(_budget_text(st))
        return

    raw = command.args.replace(",", ".").strip()
    is_topup = raw.startswith("+")
    try:
        value = float(raw.lstrip("+").strip())
    except ValueError:
        await message.answer("Приклад: /budget 300 (задати) або /budget +50 (долити)")
        return
    if value <= 0:
        await message.answer("Сума має бути більшою за 0. Приклад: /budget 300 або /budget +50")
        return

    if is_topup:
        res = budget.add_for_month(services.storage, value)
        if res["was_new"]:
            out = [f"✅ Бюджет на {res['month']} ще не був заданий — задаю {fmt_usd(res['total'])}."]
        else:
            out = [f"✅ Долила {fmt_usd(res['added'])}. Бюджет на {res['month']} тепер {fmt_usd(res['total'])}."]
    else:
        res = budget.set_for_month(services.storage, value)
        out = [f"✅ Бюджет на {res['month']}: {fmt_usd(res['total'])}."]
        if res["carry"] > 0:
            out.append(f"(Твоя сума {fmt_usd(res['entered'])} + перенесений залишок {fmt_usd(res['carry'])}.)")
    out.append("Я плавно розгортатиму його за місяць — більше на просадках, менше коли дорого.")
    await message.answer("\n".join(out))

    # Одразу дивимось ринок: чи варто вже щось поставити з цього бюджету.
    await message.answer("Дивлюсь, чи є сенс ставити вже зараз…")
    try:
        rec, rec_id = await recommend(services)
    except Exception as e:  # noqa: BLE001
        log.exception("budget recommend failed")
        await message.answer(f"Ринок гляну трохи згодом — зараз не вдалось отримати дані: {e}")
        return

    text = format_recommendation(rec)
    st = budget.status(services.storage)
    if st["active"]:
        text += f"\n\n💼 Цього разу розглядаю порцію {fmt_usd(rec.amount)} (залишок {fmt_usd(st['remaining'])})."
    keyboard = build_orders_kb(rec_id, flatten_rec(rec), {}) if rec.has_orders() else None
    extra = {"parse_mode": "HTML"} if has_quote(text) else {}
    await message.answer(to_html(text) if extra else text, reply_markup=keyboard, **extra)


def _history_text(s: dict) -> str:
    if not s["has_data"]:
        return (
            "📊 Поки що історії докупок нема. Коли ордери почнуть спрацьовувати — "
            "я питатиму підтвердження й зберігатиму результат тут."
        )
    out = ["📊 Підсумок докупок:", ""]
    out.append(f"• Виконано докупок: {s['filled_count']}")

    h30, h90 = s["h30"], s["h90"]
    if h30["evaluated"]:
        wr = int(round(h30["winrate"] * 100))
        out.append(f"• Вдалих (через 30 днів): {h30['wins']} з {h30['evaluated']} ({wr}%)")
    if h90["evaluated"]:
        wr = int(round(h90["winrate"] * 100))
        out.append(f"• Вдалих (через 90 днів): {h90['wins']} з {h90['evaluated']} ({wr}%)")

    if h30["avg_entry_vs_avg"] is not None:
        dev = h30["avg_entry_vs_avg"]
        if dev < 0:
            out.append(f"• Купували в середньому на {abs(dev):.1f}% дешевше за середню ціну 👍")
        else:
            out.append(f"• Купували в середньому на {dev:.1f}% дорожче за середню ціну")

    if s["missed_count"]:
        out.append(f"• Впущених можливостей (ціна втекла вгору): {s['missed_count']}")

    out.append("")
    out.append("Висновки:")
    for lesson in s["lessons"]:
        out.append(f"• {lesson}")
    return "\n".join(out)


def _history_brief(s: dict) -> list[str]:
    """Короткі висновки для вбудовування в /portfolio."""
    if not s["has_data"]:
        return []
    out = ["📊 Якість докупок:"]
    h30, h90 = s["h30"], s["h90"]
    if h90["evaluated"]:
        wr = int(round(h90["winrate"] * 100))
        out.append(f"• Вдалих (через 90 днів): {h90['wins']} з {h90['evaluated']} ({wr}%)")
    elif h30["evaluated"]:
        wr = int(round(h30["winrate"] * 100))
        out.append(f"• Вдалих (через 30 днів): {h30['wins']} з {h30['evaluated']} ({wr}%)")
    dev = h30["avg_entry_vs_avg"]
    if dev is not None:
        if dev < 0:
            out.append(f"• Купували в середньому на {abs(dev):.1f}% дешевше за середню 👍")
        else:
            out.append(f"• Купували в середньому на {dev:.1f}% дорожче за середню")
    if s["missed_count"]:
        out.append(f"• Впущених можливостей: {s['missed_count']}")
    for lesson in s["lessons"][:2]:
        out.append(f"• {lesson}")
    return out


@router.message(Command("portfolio"))
async def cmd_portfolio(message: Message, services: Services):
    if not _allowed(message.from_user.id):
        return
    pos = services.storage.filled_positions()
    if not pos:
        await message.answer(
            "📦 Поки що нема виконаних докупок. Коли позначиш ордер виконаним (✅ у /orders) — "
            "тут зʼявиться середня ціна входу, вкладена сума і поточний прибуток."
        )
        return
    try:
        data, _ = await gather(services)
    except Exception as e:  # noqa: BLE001
        await message.answer(f"Не вдалось отримати поточні ціни: {e}")
        return

    out = ["📦 Мій портфель (за виконаними докупками):", ""]
    tot_invested = tot_value = 0.0
    for coin, p in pos.items():
        price = (data.get(coin) or data.get(coin.upper()) or {}).get("price")
        invested = p["invested"]
        tot_invested += invested
        line = f"{coin}: вкладено {fmt_usd(invested)}"
        if p["avg_price"]:
            line += f", середня ${fmt_price(p['avg_price'])}"
        if price and p["qty"]:
            value = price * p["qty"]
            tot_value += value
            pnl = value - invested
            pct = (pnl / invested * 100) if invested else 0.0
            sign = "🟢" if pnl >= 0 else "🔴"
            line += f"\n   зараз {fmt_usd(value)} ({sign} {pnl:+.2f}$, {pct:+.1f}%)"
        out.append(line)

    if tot_value:
        pnl = tot_value - tot_invested
        pct = (pnl / tot_invested * 100) if tot_invested else 0.0
        out.append("")
        out.append(f"💰 Разом: вкладено {fmt_usd(tot_invested)}, зараз {fmt_usd(tot_value)} ({pnl:+.2f}$, {pct:+.1f}%)")

    brief = _history_brief(learning.summary(services.storage))
    if brief:
        out.append("")
        out.extend(brief)

    out.append("")
    out.append("Рахується лише за докупками, які ти позначила виконаними.")
    await message.answer("\n".join(out))


# --- повний стан грошей одним копіювальним текстом (для чату зі стороннім AI) ---

@router.message(Command("status"))
async def cmd_status(message: Message, services: Services):
    """Дамп усього, що бот знає про її гроші. Свідомо БЕЗ порадника.

    Міркувати вона буде в іншому чаті, а бот дає те, чого той чат не знає, —
    справжні цифри. Тому /status не залежить від квоти Gemini й працює завжди.
    """
    if not _allowed(message.from_user.id):
        return
    await message.answer("Збираю повний стан — це кілька секунд…")
    try:
        snap = await money_snapshot(services)
    except Exception as e:  # noqa: BLE001
        log.exception("status failed")
        await message.answer(f"Не вдалось зібрати стан: {e}")
        return

    parts = status_messages(snap)
    for part in parts:
        # HTML — заради копіювального блоку; прев'ю вимикаємо, щоб Telegram не
        # чіплявся до жодного посилання всередині тексту.
        await message.answer(
            to_html(part), parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    if len(parts) > 1:
        await message.answer(
            f"Це все — {len(parts)} частини. Скопіюй їх підряд у чат з AI, а вже "
            "після них вставляй опис акції або питання."
        )


@router.message(Command("allocation"))
async def cmd_allocation(message: Message, command: CommandObject, services: Services):
    if not _allowed(message.from_user.id):
        return
    symbols = list(config.symbols)

    if not command.args:
        target = target_allocation(services.storage)
        out = ["🎯 Бажана пропорція портфеля:"]
        out.append("• " + ", ".join(f"{c} {float(target.get(c, 0)):.0f}%" for c in symbols))
        try:
            data, _ = await gather(services)
            actual = actual_allocation(services.storage, data)
        except Exception:  # noqa: BLE001
            actual = {}
        if actual:
            out.append("")
            out.append("Фактично зараз (за виконаними докупками):")
            out.append("• " + ", ".join(f"{c} {float(actual.get(c, 0)):.0f}%" for c in symbols))
        else:
            out.append("")
            out.append("Поки що нема виконаних докупок — фактичний розподіл зʼявиться згодом.")
        out.append("")
        out.append("Змінити: /allocation 60 40 (BTC 60%, ETH 40%).")
        await message.answer("\n".join(out))
        return

    raw = command.args.replace(",", " ").replace("/", " ").split()
    if len(raw) != len(symbols):
        await message.answer(
            f"Вкажи {len(symbols)} числа — частки для {', '.join(symbols)}. Приклад: /allocation 60 40"
        )
        return
    try:
        nums = [float(x) for x in raw]
    except ValueError:
        await message.answer("Не зрозумів числа. Приклад: /allocation 60 40")
        return
    if any(n < 0 for n in nums) or sum(nums) <= 0:
        await message.answer("Частки мають бути додатними. Приклад: /allocation 60 40")
        return

    total = sum(nums)
    mapping = {c: round(n / total * 100.0, 1) for c, n in zip(symbols, nums)}
    services.storage.set_allocation(mapping)
    await message.answer(
        "✅ Збережено бажану пропорцію: "
        + ", ".join(f"{c} {mapping[c]:.0f}%" for c in symbols)
        + ".\nЯ враховуватиму це й поступово вирівнюватиму портфель — більше докуповуватиму те, чого замало."
    )


async def _free_usd(services: Services, args: str | None) -> tuple[float | None, str | None]:
    """Сума вільних доларів: з команди, з памʼяті, або залишок бюджету."""
    if args:
        try:
            value = float(args.replace(",", ".").strip())
        except ValueError:
            return None, "Сума не зрозуміла. Приклад: /earn 500"
        if value <= 0:
            return None, "Сума має бути більшою за 0. Приклад: /earn 500"
        services.storage.set_earn_amount(value)
        return value, None

    # З ключами суму питати не треба — бот бачить вільні долари сам.
    if getattr(services, "portfolio", None) is not None:
        try:
            free = (await services.portfolio.fetch()).free_stables()
            if free > 0:
                return free, None
        except Exception:  # noqa: BLE001
            log.warning("earn: не вдалось прочитати залишки з бірж", exc_info=True)

    saved = services.storage.get_earn_amount()
    if saved > 0:
        return saved, None

    st = budget.status(services.storage)
    free = st["remaining"] + st["carry"] if st["active"] else st["carry"]
    return (free if free > 0 else None), None


async def earn_snapshot(services: Services, free_usd: float | None) -> tuple[str, bool]:
    """Готує текст /earn: ставки, залишки монет у доларах, акції з поясненнями."""
    offers = await services.earn.offers()
    if not offers:
        return "Біржі зараз не віддають ставки — спробую пізніше.", False
    promos = await services.earn.promos()
    # Гроші під виставленими заявками замикати не можна: заявка спрацює саме
    # тоді, коли вони будуть недоступні.
    try:
        reserved = services.storage.reserved_for_buys()
    except Exception:  # noqa: BLE001
        reserved = 0.0

    # Залишки монет бот знає сам — з виконаних ордерів; ціни беремо з ринку.
    prices, holdings = {}, {}
    try:
        data, _ = await gather(services)
        positions = services.storage.filled_positions()
        for coin, pos in positions.items():
            price = (data.get(coin) or {}).get("price")
            qty = pos.get("qty") or 0.0
            if price and qty:
                prices[coin] = price
                holdings[coin] = {"qty": qty, "value": qty * price}
    except Exception:  # noqa: BLE001
        log.warning("earn: не вдалось оцінити залишки монет", exc_info=True)

    # Словами відсіюємо лише явне сміття — чи це справді пропозиція про дохід,
    # вирішує порадник по тексту оголошення (той самий підхід, що й у сповіщеннях).
    good = [p for p in promos if p.candidate][:4]
    # Той самий шлях, що й у сповіщеннях: інакше команда й алерт казали б різне
    # про ту саму акцію. Контекст грошей — з тих же залишків, що показані вище.
    amounts = {coin: h["value"] for coin, h in holdings.items()}
    if free_usd:
        # Стейбли для гаманця — одні й ті самі гроші, тому рахуємо суму РАЗ:
        # інакше при сумуванні виходило кратне завищення.
        amounts["USDT"] = free_usd
    bodies = await services.earn.promo_bodies(good, limit=len(good))
    explains = await promo_advice(good, amounts, free_usd or 0.0,
                                  best_instant_rates(offers, WATCHED), bodies)
    return format_earn(offers, promos, holdings, free_usd, explains, prices,
                       reserved), True


@router.message(Command("earn"))
async def cmd_earn(message: Message, command: CommandObject, services: Services):
    if not _allowed(message.from_user.id):
        return
    free_usd, err = await _free_usd(services, command.args)
    if err:
        await message.answer(err)
        return

    await message.answer("Дивлюсь, що дають на OKX і Bybit…")
    try:
        text, ok = await earn_snapshot(services, free_usd)
    except Exception as e:  # noqa: BLE001
        log.exception("earn failed")
        await message.answer(f"Не вдалось отримати пропозиції: {e}")
        return
    # У тексті є посилання на оголошення — без цього Telegram підтягне велику
    # картку першого з них і зіпсує компактний вигляд. HTML потрібен для
    # копіювального блоку з питаннями до підтримки біржі, а ріжемо тому, що з
    # цими блоками відповідь уже впирається в межу довжини повідомлення.
    parts = split_message(text)
    for i, part in enumerate(parts):
        await message.answer(
            to_html(part), parse_mode="HTML",
            reply_markup=build_earn_kb() if i == len(parts) - 1 else None,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )


# --- журнал earn-вкладів: строки й вхідні ставки, яких ключі не показують ---

# Скільки чекаємо текст після кнопки «записати вклад». Півгодини вистачає, щоб
# спокійно переписати умови із застосунку біржі, і не настільки довго, щоб
# випадкове повідомлення через три години потрапило в журнал.
JOURNAL_WAIT_SEC = 1800
JOURNAL_STATE = "earn_journal_awaiting"
JOURNAL_PENDING = "earn_journal_pending"
IBKR_STATE = "ibkr_awaiting_units"

JOURNAL_ASK = (
    "✍️ Напиши, що ти поклала — вільним текстом, можна кілька рядків за раз.\n\n"
    "Наприклад:\n"
    "200 usdt bybit savings 6.77\n"
    "500 usdt bybit on-chain 4.75\n"
    "0.01 btc okx simple earn 0.5 на 45 днів\n\n"
    "Мені потрібні сума, монета, біржа, назва продукту, відсоток і строк "
    "(якщо гроші замкнені). Чого не напишеш — просто лишу порожнім."
)


def _wait_started(storage, key: str, ttl: int) -> bool:
    """Чи бот справді ЗАРАЗ чекає текст саме для цього стану.

    Стан явний і з віком: інакше ловилка звичайного тексту для одного сценарію
    мовчки перехоплювала б відповіді для іншого.
    """
    raw = str(storage.get_setting(key, "") or "")
    if not raw:
        return False
    try:
        started = int(raw.split(":")[-1])
    except (ValueError, IndexError):
        storage.set_setting(key, "")
        return False
    if time.time() - started > ttl:
        storage.set_setting(key, "")
        return False
    return True


def _start_wait(storage, key: str, payload: str = "") -> None:
    """Вмикає очікування тексту й ГАСИТЬ решту: чекати можна лише на щось одне."""
    for other in (JOURNAL_STATE, IBKR_STATE):
        if other != key:
            storage.set_setting(other, "")
    stamp = str(int(time.time()))
    storage.set_setting(key, f"{payload}:{stamp}" if payload else stamp)


@router.callback_query(F.data == "ejnew")
async def cb_journal_new(callback: CallbackQuery, services: Services):
    """«➕ записати вклад» — вмикаємо очікування саме тексту про вклад."""
    if not _allowed(callback.from_user.id):
        await callback.answer()
        return
    _start_wait(services.storage, JOURNAL_STATE)
    await callback.answer()
    await callback.message.answer(JOURNAL_ASK)


@router.callback_query(F.data == "ejlist")
async def cb_journal_list(callback: CallbackQuery, services: Services):
    if not _allowed(callback.from_user.id):
        await callback.answer()
        return
    await callback.answer()
    # HTML потрібен для копіювального блоку зі списком вкладів: вона хоче
    # забирати журнал одним натисканням, а не переписувати руками.
    storage = services.storage
    prices = await _coin_prices(services)
    text = journal_list_text(
        storage.earn_deposits(), prices,
        # Заробіток рахуємо з денних знімків у базі, а не з бірж: тут потрібна
        # історія, і жодна біржа її за нас не пам'ятає.
        income.summary(storage.earn_income(), prices),
        storage.earn_income_skips(),
    )
    await callback.message.answer(to_html(text), parse_mode="HTML")


async def _coin_prices(services: Services) -> dict:
    """Ціни BTC/ETH — щоб дохід із вкладів у монетах теж рахувався в доларах."""
    try:
        data, _ = await gather(services)
    except Exception:  # noqa: BLE001
        log.warning("журнал: ціни недоступні", exc_info=True)
        return {}
    return {c: d.get("price") for c, d in data.items() if d.get("price")}


async def _handle_deposit_text(message: Message, services: Services) -> None:
    """Текст після кнопки журналу -> розбір, показ і кнопки підтвердження."""
    storage = services.storage
    storage.set_setting(JOURNAL_STATE, "")
    deposits, by_ai = await parse_deposits_text(message.text or "")
    if not deposits:
        await message.answer(
            "Не змогла нічого розібрати 🙈 Мені потрібні хоча б сума й монета, "
            "напр.: 200 usdt bybit savings 6.77\n"
            "Спробуй ще раз кнопкою «➕ записати вклад» під /earn."
        )
        return

    ids = [storage.earn_deposit_add(d) for d in deposits]
    storage.set_setting(JOURNAL_PENDING, json.dumps(ids))
    text = journal_confirm_text(deposits, await _coin_prices(services))
    if not by_ai:
        text += "\n" + NO_ADVISOR_NOTE
    await message.answer(text, reply_markup=build_journal_confirm_kb())


@router.callback_query(F.data.startswith("ejc:"))
async def cb_journal_confirm(callback: CallbackQuery, services: Services):
    """Підтвердження або скасування розбору. Без цього в журналі нічого не лишається."""
    if not _allowed(callback.from_user.id):
        await callback.answer()
        return
    storage = services.storage
    try:
        ids = json.loads(storage.get_setting(JOURNAL_PENDING, "") or "[]")
    except (ValueError, TypeError):
        ids = []
    storage.set_setting(JOURNAL_PENDING, "")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001
        pass

    if not ids:
        await callback.answer("Цей запис уже опрацьований")
        return
    if callback.data.endswith(":ok"):
        n = storage.earn_deposits_confirm(ids)
        await callback.answer("Записала")
        await callback.message.answer(
            f"✅ Занесла в журнал {n} {deposits_word(n)}. Нагадаю в день розморозки "
            "й скажу, якщо ставка десь просяде. Подивитись усе — кнопка "
            "«📒 мій журнал вкладів» під /earn."
        )
    else:
        storage.earn_deposits_drop(ids)
        await callback.answer("Скасувала")
        await callback.message.answer(
            "✖️ Нічого не записала. Спробуй ще раз кнопкою «➕ записати вклад» — "
            "можна написати докладніше, напр.: 200 usdt bybit savings 6.77% на 30 днів"
        )


@router.callback_query(F.data.startswith("ejd:"))
async def cb_journal_due(callback: CallbackQuery, services: Services):
    """«забрала» / «продовжила» після розморозки — журнал оновлюємо з її відповіді."""
    if not _allowed(callback.from_user.id):
        await callback.answer()
        return
    try:
        _, dep_id_s, how = callback.data.split(":")
        dep_id = int(dep_id_s)
    except ValueError:
        await callback.answer()
        return

    dep = services.storage.earn_deposit(dep_id)
    if not dep or dep.get("status") != STATUS_ACTIVE:
        await callback.answer("Цей вклад уже опрацьований")
        return
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001
        pass

    if how == "took":
        services.storage.earn_deposit_close(dep_id, "took")
        await callback.answer("Записала: забрала")
        await callback.message.answer(
            f"✅ Прибрала з журналу: {deposit_line(dep).lstrip('• ')}\n"
            "Куди покласти ці гроші — подивись /earn."
        )
        return

    term = int(dep.get("term_days") or 0)
    new_unlock = (datetime.date.today() + datetime.timedelta(days=term)) if term else None
    services.storage.earn_deposit_extend(dep_id, new_unlock.isoformat() if new_unlock else None)
    tail = (f"Наступна розморозка — {new_unlock.strftime('%d.%m')}."
            if new_unlock else "Строк не знаю — нагадаю, якщо впишеш його заново.")
    await callback.answer("Записала: продовжила")
    await callback.message.answer(
        f"🔄 Добре, вклад лишається в журналі. {tail}\n"
        "⚠️ При автопродовженні біржі часто ставлять уже іншу ставку. Якщо вона "
        "змінилась — перезапиши вклад кнопкою «➕ записати вклад», і я рахуватиму "
        "правильно."
    )


@router.callback_query(F.data.startswith("alt:"))
async def cb_alt_topup(callback: CallbackQuery, services: Services):
    """Добровільне докидання своїх грошей у чужу монету — з жорсткою межею."""
    if not _allowed(callback.from_user.id):
        await callback.answer()
        return
    try:
        _, coin, raw = callback.data.split(":")
        amount = min(float(raw), ALT_TOPUP_MAX)
    except ValueError:
        await callback.answer()
        return

    free = budget.unused_free(services.storage)
    pos = alt_position(free, topup=amount)
    services.storage.set_setting(f"earn_alt_topup_{coin}", pos["amount"])
    await callback.answer("Записала")
    await callback.message.answer(
        f"➕ Добре: {fmt_usd(pos['from_topup'])} твоїх зверху до "
        f"{fmt_usd(pos['from_budget'])} з бюджету — разом {fmt_usd(pos['amount'])} у {coin}.\n"
        f"Більше ніж {fmt_usd(ALT_TOPUP_MAX)} своїх я не пропоную в принципі: це "
        "монета, якої в тебе немає, і вона може просісти в ціні.\n"
        "Розклад по біржах — /earn."
    )


# --- щомісячна купівля ETF на IBKR (євро, окремо від крипти) ---

# Скільки часу після «Зроблено» чекаємо, що вона впише кількість паїв.
UNITS_WAIT_SEC = 3600


async def ibkr_plan(services: Services, today: datetime.date | None = None) -> dict:
    """План купівлі на цей місяць. Ціна необовʼязкова: без неї план просто без паїв."""
    today = today or datetime.date.today()
    fund = ibkr.fund_for_month(today)
    price = await ibkr.safe_price(getattr(services, "etf", None), fund)
    return ibkr.month_plan(today, price)


@router.message(Command("ibkr"))
async def cmd_ibkr(message: Message, services: Services):
    if not _allowed(message.from_user.id):
        return
    today = datetime.date.today()
    start = ibkr.strategy_start(services.storage)
    plan = await ibkr_plan(services, today)
    record = services.storage.ibkr_get_month(plan["month"])
    text = ibkr.status_text(
        plan,
        record,
        services.storage.ibkr_history(6),
        ibkr.totals(services.storage),
        start,
        today,
    )
    await message.answer(
        text,
        reply_markup=build_ibkr_kb(plan["month"], (record or {}).get("status")),
    )


@router.callback_query(F.data.startswith("em:"))
async def cb_earn_mute(callback: CallbackQuery, services: Services):
    """«Не цікаво» під earn-сповіщенням: перестаємо радити саме це."""
    if not _allowed(callback.from_user.id):
        await callback.answer()
        return
    try:
        mute_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await callback.answer()
        return

    target = services.storage.earn_mute_target(mute_id)
    if not target:
        await callback.answer("Не пам'ятаю цю пораду — вона вже застара.")
        return
    services.storage.earn_mute(target)
    what = target.split(":", 1)[1] if ":" in target else target
    await callback.answer(f"Добре, про {what} більше не нагадую.")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001
        pass


@router.callback_query(F.data.startswith("ibkr:"))
async def cb_ibkr(callback: CallbackQuery, services: Services):
    """«Зроблено» / «Пропустити цього місяця» під нагадуванням про купівлю ETF."""
    if not _allowed(callback.from_user.id):
        await callback.answer()
        return
    try:
        _, action, month = callback.data.split(":")
    except ValueError:
        await callback.answer()
        return

    fund = ibkr.fund_for_month_key(month)
    if fund is None:
        await callback.answer()
        return

    # Запис міг ще не існувати: кнопки бувають і під /ibkr, а не лише під нагадуванням.
    # Сума не завжди €200 — у місяць CEMA це ціна одного паю, тому питаємо ціну.
    price = await ibkr.safe_price(getattr(services, "etf", None), fund)
    amount = ibkr.planned_amount(fund, price)
    services.storage.ibkr_ensure_month(month, fund.ticker, amount, price)

    if action == "done":
        services.storage.ibkr_mark_done(month)
        # Кількість паїв — за бажанням. Просто чекаємо число наступним повідомленням.
        # Явний стан гасить очікування журналу: чекати можна лише на щось одне.
        _start_wait(services.storage, IBKR_STATE, month)
        note = f"Записала: {month} — {fund.ticker} {fmt_eur(amount)}"
        follow = (
            f"✅ Записала купівлю за {month}: {fund.ticker}, {fmt_eur(amount)}.\n"
            "Цього місяця більше не нагадую.\n\n"
            "Якщо хочеш — напиши, скільки паїв реально купила (просто числом, напр. 15). "
            "Це не обовʼязково, можна не відповідати."
        )
    elif action == "skip":
        services.storage.ibkr_mark_skipped(month)
        services.storage.set_setting("ibkr_awaiting_units", "")
        note = f"Пропущено: {month}"
        follow = (
            f"✖️ Добре, {month} пропускаємо — нагадувати більше не буду.\n"
            "Нічого страшного: пропущений місяць не ламає стратегію, наступний внесок "
            "піде за календарем як завжди."
        )
    else:
        await callback.answer()
        return

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001
        pass
    await callback.answer(note)
    await callback.message.answer(follow)


@router.message(Command("alerts"))
async def cmd_alerts(message: Message, command: CommandObject, services: Services):
    if not _allowed(message.from_user.id):
        return
    arg = (command.args or "").strip().lower()
    if arg == "on":
        services.storage.set_alerts(True)
        await message.answer("🔔 Сповіщення увімкнено.")
    elif arg == "off":
        services.storage.set_alerts(False)
        await message.answer("🔕 Сповіщення вимкнено.")
    else:
        state = "увімкнені" if services.storage.alerts_enabled() else "вимкнені"
        await message.answer(f"Сповіщення: {state}. /alerts on|off")


# Ловилка звичайного тексту — має лишатись ОСТАННЬОЮ в роутері, щоб не перебивати команди.
@router.message(F.text & ~F.text.startswith("/"))
async def msg_plain_text(message: Message, services: Services):
    """Єдиний вхід для звичайного тексту — далі розводимо за ЯВНИМ станом.

    Ловилка тут одна свідомо: два незалежні перехоплювачі тексту в одному
    роутері неминуче почали б красти повідомлення одне в одного. Тому спершу
    дивимось, чого саме бот зараз чекає: тексту про вклад після кнопки журналу
    чи числа паїв після кнопки IBKR.
    """
    if not _allowed(message.from_user.id):
        return
    if _wait_started(services.storage, JOURNAL_STATE, JOURNAL_WAIT_SEC):
        await _handle_deposit_text(message, services)
        return
    await _handle_ibkr_units(message, services)


async def _handle_ibkr_units(message: Message, services: Services):
    """Число після «Зроблено» — це кількість куплених паїв.

    Слухаємо лише коротким вікном після натискання кнопки, щоб не реагувати на
    випадкові повідомлення. Не число — тихо забуваємо про очікування.
    """
    raw = services.storage.get_setting("ibkr_awaiting_units", "") or ""
    if ":" not in raw:
        return
    month, _, ts = raw.partition(":")
    try:
        waiting_since = int(ts)
    except ValueError:
        services.storage.set_setting("ibkr_awaiting_units", "")
        return
    if time.time() - waiting_since > UNITS_WAIT_SEC:
        services.storage.set_setting("ibkr_awaiting_units", "")
        return

    services.storage.set_setting("ibkr_awaiting_units", "")
    try:
        units = float((message.text or "").replace(",", ".").strip())
    except ValueError:
        return
    if units <= 0:
        await message.answer("Кількість паїв має бути більшою за 0. Нічого не змінюю.")
        return

    services.storage.ibkr_set_units(month, units)
    await message.answer(
        f"✅ Записала: за {month} куплено {fmt_units(units)} паїв. Дивитись усе — /ibkr"
    )
