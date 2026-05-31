"""Команди Telegram-бота."""
import logging
import time
from dataclasses import dataclass

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from app.config import config
from app.engine import analyze, fng_label
from app.formatting import fmt_price, fmt_usd, format_recommendation
from app.keyboards import build_orders_kb, flatten_rec
from app.service import gather, recommend
from app.storage import flat_orders

log = logging.getLogger(__name__)
router = Router()


@dataclass
class Services:
    storage: object
    market: object


def _allowed(user_id: int) -> bool:
    return config.owner_id == 0 or user_id == config.owner_id


HELP = (
    "Привіт! Я підказую, коли і якими лімітними ордерами докуповувати BTC/ETH "
    "(стратегія — довгострокове накопичення).\n\n"
    "/invest [сума] — рекомендація (без суми = база)\n"
    "/status — поточний стан ринку\n"
    "/orders — мої активні ордери\n"
    "/cleanup — застряглі ордери (розморозити кошти)\n"
    "/base <сума> — змінити базову суму\n"
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
    amount = None
    if command.args:
        try:
            amount = float(command.args.replace(",", ".").strip())
        except ValueError:
            await message.answer("Сума не зрозуміла. Приклад: /invest 200")
            return
    await message.answer("Аналізую ринок…")
    try:
        rec, rec_id = await recommend(services, amount)
    except Exception as e:  # noqa: BLE001
        log.exception("invest failed")
        await message.answer(f"Не вдалось отримати дані ринку: {e}")
        return

    text = format_recommendation(rec)
    keyboard = None
    if rec.has_orders():
        keyboard = build_orders_kb(rec_id, flatten_rec(rec), {})
    await message.answer(text, reply_markup=keyboard)


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


@router.message(Command("status"))
async def cmd_status(message: Message, services: Services):
    if not _allowed(message.from_user.id):
        return
    try:
        data, fng = await gather(services)
    except Exception as e:  # noqa: BLE001
        await message.answer(f"Не вдалось отримати дані: {e}")
        return
    rec = analyze(data, fng, services.storage.get_base(config.base_amount))
    out = []
    for cp in rec.coins:
        out.append(f"{cp.coin}: ${fmt_price(cp.price)} — {cp.zone}")
    out.append(f"Настрій ринку: {fng_label(fng)} ({fng})")
    out.append("")
    out.append(rec.headline)
    await message.answer("\n".join(out))


@router.message(Command("orders"))
async def cmd_orders(message: Message, services: Services):
    if not _allowed(message.from_user.id):
        return
    rows = services.storage.open_orders()
    if not rows:
        await message.answer("Немає активних ордерів у журналі.")
        return
    out = ["Твої активні ордери:", ""]
    for r in rows:
        out.append(
            f"• {r['coin']} {r['qty']:.6f} по ${fmt_price(r['price'])} ({fmt_usd(r['notional'])})"
        )
    await message.answer("\n".join(out))


@router.message(Command("cleanup"))
async def cmd_cleanup(message: Message, services: Services):
    if not _allowed(message.from_user.id):
        return
    try:
        data, _ = await gather(services)
    except Exception as e:  # noqa: BLE001
        await message.answer(f"Не вдалось отримати дані: {e}")
        return
    prices = {coin: data[coin]["price"] for coin in data}
    now = time.time()
    stale = []
    frozen = 0.0
    for r in services.storage.open_orders():
        price = prices.get(r["coin"])
        age = now - r["created_at"]
        if price and age > config.stale_days * 86400 and price > r["price"] * 1.10:
            stale.append(r)
            frozen += r["notional"]
    if not stale:
        await message.answer("Немає застряглих ордерів — усе ок.")
        return
    out = [
        "🧹 Ці ордери висять давно, ціна втекла вгору. Можеш скасувати, щоб розморозити кошти:",
        "",
    ]
    for r in stale:
        out.append(f"• {r['coin']} по ${fmt_price(r['price'])} ({fmt_usd(r['notional'])})")
    out.append("")
    out.append(f"Заморожено ~{fmt_usd(frozen)}. Скасуй на Bybit і постав нові через /invest.")
    await message.answer("\n".join(out))


@router.message(Command("base"))
async def cmd_base(message: Message, command: CommandObject, services: Services):
    if not _allowed(message.from_user.id):
        return
    if not command.args:
        cur = services.storage.get_base(config.base_amount)
        await message.answer(f"Базова сума: {fmt_usd(cur)}. Змінити: /base 150")
        return
    try:
        value = float(command.args.replace(",", ".").strip())
    except ValueError:
        await message.answer("Приклад: /base 150")
        return
    services.storage.set_base(value)
    await message.answer(f"Готово. Базова сума тепер {fmt_usd(value)}.")


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
