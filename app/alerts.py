"""Планувальник: алерти про хороші моменти, відстеження спрацювань і чистка застряглих ордерів."""
import datetime
import logging
import time
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import config
from app.engine import analyze
from app.formatting import fmt_price, fmt_usd, format_recommendation
from app.keyboards import build_orders_kb, flatten_rec
from app.service import gather

log = logging.getLogger(__name__)

DAY = 86400


async def _send(bot, text: str):
    if config.owner_id:
        try:
            await bot.send_message(config.owner_id, text)
        except Exception:  # noqa: BLE001
            log.exception("send_message failed")


async def _send_rec(bot, services, prefix: str, rec):
    """Шле алерт з рекомендацією, де кожен ордер має власні кнопки підтвердження."""
    text = prefix + "\n\n" + format_recommendation(rec)
    keyboard = None
    if rec.has_orders():
        rec_id = services.storage.save_recommendation(rec)
        keyboard = build_orders_kb(rec_id, flatten_rec(rec), {})
    if config.owner_id:
        try:
            await bot.send_message(config.owner_id, text, reply_markup=keyboard)
        except Exception:  # noqa: BLE001
            log.exception("send_rec failed")


def _min_low_since(d: dict, created_at: int) -> Optional[float]:
    cutoff_ms = created_at * 1000
    closes = [c for t, c in zip(d.get("ts", []), d.get("closes", [])) if t >= cutoff_ms]
    return min(closes) if closes else None


async def market_check(bot, services):
    if not services.storage.alerts_enabled():
        return
    try:
        data, fng = await gather(services)
    except Exception:  # noqa: BLE001
        log.exception("market_check: gather failed")
        return

    rec = analyze(data, fng, services.storage.get_base(config.base_amount))

    # Хороший момент
    if rec.moment == "buy" and not services.storage.alert_recent("good_moment", 2 * DAY):
        await _send_rec(
            bot, services,
            "🔔 Гарний час докупити — ціни зараз привабливі. Ось що можна поставити:",
            rec,
        )
        services.storage.mark_alert("good_moment")

    # Капітуляція — різке падіння за добу
    big = [c for c in data if data[c]["change_24h"] is not None
           and data[c]["change_24h"] <= -config.capitulation_drop]
    if big and not services.storage.alert_recent("capitulation", 1 * DAY):
        await _send_rec(
            bot, services,
            f"🔴 Ринок різко впав ({', '.join(big)}). Зазвичай це вдалий момент докупити дешевше:",
            rec,
        )
        services.storage.mark_alert("capitulation")

    # Зона глибокої вартості (історично низькі ціни)
    deep = [cp.coin for cp in rec.coins if cp.mayer is not None and cp.mayer <= 1.0]
    if deep and not services.storage.alert_recent("deep_value", 3 * DAY):
        await _send_rec(
            bot, services,
            f"🟢 Ціна {', '.join(deep)} зараз дуже низька — давно такого не було. "
            f"Чудовий час, щоб докупити:",
            rec,
        )
        services.storage.mark_alert("deep_value")

    # Жадібність — натяк зафіксувати прибуток
    if fng >= config.greed_fng and not services.storage.alert_recent("greed", 5 * DAY):
        await _send(
            bot,
            "🟡 Ринок зараз перегрітий, усі женуться за прибутком. "
            "Якщо ти в плюсі — можеш продати трохи й забрати частину прибутку.",
        )
        services.storage.mark_alert("greed")


async def daily_check(bot, services):
    try:
        data, _ = await gather(services)
    except Exception:  # noqa: BLE001
        log.exception("daily_check: gather failed")
        return

    now = int(time.time())

    # Відстеження спрацювань (за способом А — по ринковій ціні)
    filled = []
    for o in services.storage.open_orders():
        d = data.get(o["coin"])
        if not d:
            continue
        low = _min_low_since(d, o["created_at"])
        if low is not None and low <= o["price"]:
            services.storage.mark_filled(o["id"], now)
            filled.append(o)
    if filled and services.storage.alerts_enabled():
        out = ["✅ Здається, твої ордери спрацювали — ти докупив по цих цінах:", ""]
        for o in filled:
            out.append(f"• {o['coin']} по ${fmt_price(o['price'])} ({fmt_usd(o['notional'])})")
        await _send(bot, "\n".join(out))

    # Застряглі ордери — пропозиція скасувати
    prices = {coin: data[coin]["price"] for coin in data}
    stale = []
    frozen = 0.0
    for o in services.storage.open_orders():
        price = prices.get(o["coin"])
        if price and now - o["created_at"] > config.stale_days * DAY and price > o["price"] * 1.10:
            stale.append(o)
            frozen += o["notional"]
    if stale and services.storage.alerts_enabled() and not services.storage.alert_recent("stale", 3 * DAY):
        out = [
            "🧹 Ці ордери висять давно, а ціна вже пішла вгору — навряд вони спрацюють.",
            "Краще скасуй їх, щоб гроші не лежали заблоковані:",
            "",
        ]
        for o in stale:
            out.append(f"• {o['coin']} по ${fmt_price(o['price'])} ({fmt_usd(o['notional'])})")
        out.append("")
        out.append(f"Заблоковано ~{fmt_usd(frozen)}. Постав нові через /invest.")
        await _send(bot, "\n".join(out))
        services.storage.mark_alert("stale")

    # Тайм-аут спрацювання — добрати по ринку
    stale_ids = {o["id"] for o in stale}
    hanging = [
        o for o in services.storage.open_orders()
        if o["id"] not in stale_ids and now - o["created_at"] > config.stale_days * DAY
    ]
    if hanging and services.storage.alerts_enabled() and not services.storage.alert_recent("market_fill", 7 * DAY):
        total_notional = sum(o["notional"] for o in hanging)
        out = [
            f"⏳ Твої ордери висять {config.stale_days}+ днів і ще не спрацювали.",
            "Щоб не переривати накопичення — розглянь докупку частини по ринку:",
            "",
        ]
        for o in hanging:
            out.append(f"• {o['coin']} по ${fmt_price(o['price'])} ({fmt_usd(o['notional'])})")
        out.append("")
        out.append(f"Загалом ~{fmt_usd(total_notional)}. Або зачекай далі — вирішуєш ти.")
        await _send(bot, "\n".join(out))
        services.storage.mark_alert("market_fill")

    # Місячне нагадування DCA
    today = datetime.date.today()
    if today.day >= 25 and services.storage.alerts_enabled() and not services.storage.has_buy_this_month():
        key = f"monthly_{today.year}_{today.month}"
        if not services.storage.alert_recent(key, 40 * DAY):
            await _send(
                bot,
                "📅 Цей місяць ти ще нічого не докуповував. Для довгої гри краще брати "
                "потроху регулярно — глянь /invest.",
            )
            services.storage.mark_alert(key)


def setup_scheduler(bot, services) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(market_check, "interval", minutes=60, args=[bot, services])
    scheduler.add_job(daily_check, "cron", hour=12, minute=0, args=[bot, services])
    return scheduler
