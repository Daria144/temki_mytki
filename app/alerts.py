"""Планувальник: алерти про хороші моменти, відстеження спрацювань і чистка застряглих ордерів."""
import datetime
import glob
import logging
import os
import shutil
import time
from typing import Optional

from aiogram.types import FSInputFile, LinkPreviewOptions
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app import advisor, budget, ibkr, income, journal, learning
from app.config import config
from app.earn import (
    ALT_MIN_GAIN_YEAR,
    ALT_TOPUP_MAX,
    RISK_HIGH,
    RISK_VOLATILE,
    STABLES,
    WATCHED,
    alt_coin_allowed,
    alt_offers,
    alt_position,
    best_instant_rates,
    classify_risk,
    promo_expired,
    rate_limit_note,
)
from app.formatting import (
    alt_coin_lines,
    coin_trust_lines,
    fmt_price,
    fmt_usd,
    format_recommendation,
    has_quote,
    journal_due_text,
    promo_lines,
    rate_drop_lines,
    reconcile_lines,
    to_html,
    weekly_review_text,
)
from app.handlers import _history_text, ibkr_plan
from app.portfolio import SPOT, upgrades
from app.keyboards import (
    build_alt_topup_kb,
    build_earn_mute_kb,
    build_fill_confirm_kb,
    build_ibkr_kb,
    build_journal_due_kb,
    build_orders_kb,
    flatten_rec,
)
from app.service import (
    allocation_context,
    analyze_market,
    coin_reliability,
    gather,
    portfolio_pnl,
    promo_advice,
)

log = logging.getLogger(__name__)

# Лічильник поспіль-невдалих завантажень ринку (для алерту про збій даних).
_fetch_failures = 0
BACKUP_DIR = "backups"
BACKUP_KEEP = 14

DAY = 86400

# Тихі години: авто-сповіщення приходять лише у проміжку [WORK_START, WORK_END) за місцевим часом.
WORK_START = 8
WORK_END = 22

# Скільки годин поспіль порадник має мовчати, щоб це вже не було «тимчасово зайнято».
# У серпні він відмовляв два тижні поспіль, а власниця про це не знала.
AI_SILENT_HOURS = 4
# Одне попередження на пів доби: щогодинне нагадування про ту саму біду — це спам.
AI_ALERT_COOLDOWN = 12 * 3600


def _quiet_now() -> bool:
    """True, якщо зараз поза робочими годинами — авто-сповіщення слід притримати."""
    h = datetime.datetime.now().hour
    return not (WORK_START <= h < WORK_END)


async def _send(bot, text: str, reply_markup=None) -> bool:
    """Шле повідомлення власниці. True — дійшло.

    Прев'ю посилань вимкнене: у сповіщеннях про акції є посилання на оголошення,
    і Telegram підтягував би велику картку, від якої компактний формат розсипається.
    Відповідь потрібна викликачу: позначати знахідки переглянутими можна лише
    після того, як вони реально пішли.
    """
    if not config.owner_id:
        return False
    # HTML вмикаємо лише там, де в тексті є копіювальний блок: у решті випадків
    # зайвий parse_mode нічого не дає, а от зламатись на чужому «&» може.
    extra = {"parse_mode": "HTML"} if has_quote(text) else {}
    try:
        await bot.send_message(
            config.owner_id, to_html(text) if extra else text, reply_markup=reply_markup,
            link_preview_options=LinkPreviewOptions(is_disabled=True), **extra,
        )
        return True
    except Exception:  # noqa: BLE001
        log.exception("send_message failed")
        return False


def _good_moment_prefix(moment: str) -> Optional[str]:
    """Текст проактивного алерту для buy / accumulate; інакше None."""
    if moment == "buy":
        return (
            "🔔 Гарний час докупити — ціни зараз привабливі, сигнал сильніший. "
            "Ось що можна поставити:"
        )
    if moment == "accumulate":
        return (
            "🔔 Помірна просадка — можна поступово докупити, не поспішаючи. "
            "Ось ордери, які мають сенс зараз:"
        )
    return None


def _good_moment_eligible(rec) -> bool:
    return rec.moment in ("buy", "accumulate") and rec.has_orders()


async def _send_rec(bot, services, prefix: str, rec):
    """Шле алерт з рекомендацією, де кожен ордер має власні кнопки підтвердження."""
    text = prefix + "\n\n" + format_recommendation(rec)
    keyboard = None
    if rec.has_orders():
        rec_id = services.storage.save_recommendation(rec)
        keyboard = build_orders_kb(rec_id, flatten_rec(rec), {})
    if config.owner_id:
        extra = {"parse_mode": "HTML"} if has_quote(text) else {}
        try:
            await bot.send_message(config.owner_id, to_html(text) if extra else text,
                                   reply_markup=keyboard, **extra)
        except Exception:  # noqa: BLE001
            log.exception("send_rec failed")


def _min_low_since(d: dict, created_at: int) -> Optional[float]:
    """Найнижча ціна від моменту created_at — по денних мінімумах, якщо вони є.

    Лімітка спрацьовує всередині дня, тож порівнювати треба саме з мінімумом дня:
    по цінах закриття бот не бачив би навіть уже зроблених покупок.
    Свічку ДНЯ СТВОРЕННЯ теж беремо (вона «накриває» момент створення) — інакше
    заявка, яку ринок зачепив того ж дня, лишалась би невиконаною назавжди.
    Мінімумів немає (жодна біржа не відповіла) — відкочуємось на закриття.
    """
    cutoff_ms = created_at * 1000
    lows = d.get("lows")
    low_ts = d.get("low_ts") or d.get("ts") or []
    if lows and len(lows) == len(low_ts):
        vals = [v for t, v in zip(low_ts, lows)
                if v is not None and t + DAY * 1000 > cutoff_ms]
    else:
        # Закриття — груба заміна, тому тут лишаємось строгими до дати.
        vals = [c for t, c in zip(d.get("ts") or [], d.get("closes") or []) if t >= cutoff_ms]
    return min(vals) if vals else None


async def _greed_or_take_profit(bot, services, data, fng) -> None:
    """Сигнал перегрітого ринку. Якщо ти суттєво в плюсі — мʼяко підкажемо зафіксувати частину.

    Один тип алерту 'greed' (з дедупом), щоб не дублювати повідомлення.
    """
    if fng < config.greed_fng or services.storage.alert_recent("greed", 5 * DAY):
        return

    pnl = portfolio_pnl(services.storage, data)
    pnl_pct = pnl.get("pnl_pct")
    thr = config.take_profit_pct

    if pnl_pct is not None and pnl_pct >= thr:
        # Знайдемо найприбутковішу монету, щоб згадати її конкретно (простою мовою).
        best_coin = None
        best_pct = None
        for coin, info in (pnl.get("by_coin") or {}).items():
            p = info.get("pnl_pct")
            if p is not None and (best_pct is None or p > best_pct):
                best_pct, best_coin = p, coin
        extra = ""
        if best_coin and best_pct is not None and best_pct >= thr:
            extra = f" Найбільше виросла {best_coin} (+{best_pct:.0f}%)."
        await _send(
            bot,
            f"🟢 Ти зараз суттєво в плюсі (+{pnl_pct:.0f}%), а ринок перегрітий — усі женуться "
            f"за прибутком.{extra} Це непоганий момент зафіксувати частину прибутку: можеш продати "
            "приблизно 10–20% і відкласти. Це не порада продавати все — просто прибрати трохи зі столу. "
            "Вирішуєш ти.",
        )
    else:
        await _send(
            bot,
            "🟡 Ринок зараз перегрітий, усі женуться за прибутком. "
            "Якщо ти в плюсі — можеш продати трохи й забрати частину прибутку.",
        )
    services.storage.mark_alert("greed")


async def _advisor_health_alert(bot, services) -> None:
    """Попереджає, якщо порадник мовчить не годину-дві, а вже давно.

    Та сама логіка, що й для збою даних із бірж: мовчазна деградація гірша за
    видиму поломку — бот просто тихо переходить на формулу, і цього не видно.
    """
    silent = advisor.health()["silent_for"]
    if silent < AI_SILENT_HOURS * 3600:
        return
    if services.storage.alert_recent("ai_fail", AI_ALERT_COOLDOWN):
        return
    await _send(
        bot,
        f"⚠️ Мій порадник не відповідає вже {silent / 3600:.0f} год поспіль. "
        "Поки що я вирішую сама, за загальним правилом — це працює, але без пояснень "
        "і трохи простіше. Найчастіше причина — вичерпаний ліміт ключа Gemini.",
    )
    services.storage.mark_alert("ai_fail")


async def market_check(bot, services):
    if not services.storage.alerts_enabled():
        return
    if _quiet_now():
        # Поза робочими годинами — нічого не шлемо й не палимо кулдаун/квоту.
        return
    global _fetch_failures
    try:
        data, fng = await gather(services)
        _fetch_failures = 0
    except Exception:  # noqa: BLE001
        log.exception("market_check: gather failed")
        _fetch_failures += 1
        # Кілька збоїв поспіль — щось не так з джерелами даних, попередимо (раз на 6 год).
        if _fetch_failures >= 3 and not services.storage.alert_recent("data_fail", 6 * 3600):
            await _send(
                bot,
                f"⚠️ Не вдається отримати дані ринку вже {_fetch_failures} раз(и) поспіль. "
                "Перевір логи сервера, якщо це триватиме.",
            )
            services.storage.mark_alert("data_fail")
        return

    position = services.storage.position_summary()
    bstatus = budget.status(services.storage)
    if bstatus["active"]:
        # Працюємо в межах місячного бюджету: за раз пропонуємо лише ПОРЦІЮ (тижневий
        # темп, ×2 на просадці), а не весь залишок — щоб розтягнути бюджет на місяць.
        can_buy = bstatus["remaining"] > 0
        amount = budget.suggested_tranche(bstatus, budget.is_dip(data, fng))
        bctx = bstatus
    else:
        can_buy = True
        amount = services.storage.get_base(config.base_amount)
        bctx = None

    if not can_buy:
        # Бюджет на місяць вичерпано — докупки не пропонуємо (лишаємо тільки сигнал жадібності).
        await _greed_or_take_profit(bot, services, data, fng)
        return

    learning_hint = learning.ai_hint(services.storage)
    depth = learning.depth_factor(services.storage)
    allocation = allocation_context(services.storage, data)
    rec = await analyze_market(data, fng, amount, position, bctx, learning_hint, depth, allocation)
    await _advisor_health_alert(bot, services)

    # Хороший момент (buy або accumulate з ордерами — один кулдаун good_moment)
    gm_prefix = _good_moment_prefix(rec.moment)
    if (
        gm_prefix
        and _good_moment_eligible(rec)
        and not services.storage.alert_recent("good_moment", 2 * DAY)
    ):
        await _send_rec(bot, services, gm_prefix, rec)
        services.storage.mark_alert("good_moment")

    # Капітуляція — різке падіння за добу
    big = [c for c in data if data[c]["change_24h"] is not None
           and data[c]["change_24h"] <= -config.capitulation_drop]
    if big and rec.has_orders() and not services.storage.alert_recent("capitulation", 1 * DAY):
        await _send_rec(
            bot, services,
            f"🔴 Ринок різко впав ({', '.join(big)}). Зазвичай це вдалий момент докупити дешевше:",
            rec,
        )
        services.storage.mark_alert("capitulation")

    # Зона глибокої вартості (історично низькі ціни)
    deep = [cp.coin for cp in rec.coins if cp.mayer is not None and cp.mayer <= 1.0]
    if deep and rec.has_orders() and not services.storage.alert_recent("deep_value", 3 * DAY):
        await _send_rec(
            bot, services,
            f"🟢 Ціна {', '.join(deep)} зараз дуже низька — давно такого не було. "
            f"Чудовий час, щоб докупити:",
            rec,
        )
        services.storage.mark_alert("deep_value")

    # Жадібність / take-profit — мʼякий натяк зафіксувати частину прибутку (з урахуванням реального P&L)
    await _greed_or_take_profit(bot, services, data, fng)


async def daily_check(bot, services):
    try:
        data, _ = await gather(services)
    except Exception:  # noqa: BLE001
        log.exception("daily_check: gather failed")
        return

    now = int(time.time())
    redetect_cooldown = 14 * DAY  # після «не спрацював» не питаємо знову певний час

    # Відстеження спрацювань (за способом А — по ринковій ціні).
    # Не вважаємо виконаним мовчки — позначаємо pending_confirm і просимо підтвердити.
    detected = []
    for o in services.storage.open_orders():
        d = data.get(o["coin"])
        if not d:
            continue
        declined = o.get("confirm_declined_at")
        if declined and now - declined < redetect_cooldown:
            continue
        low = _min_low_since(d, o["created_at"])
        if low is not None and low <= o["price"]:
            services.storage.mark_pending_confirm(o["id"], now)
            detected.append(services.storage.get_order(o["id"]))
    if detected and services.storage.alerts_enabled():
        out = [
            "✅ Здається, ці ордери спрацювали (ціна досягала твоєї).",
            "Підтверди, будь ласка, щоб я вела точний облік:",
            "",
        ]
        for o in detected:
            out.append(f"• {o['coin']} по ${fmt_price(o['price'])} ({fmt_usd(o['notional'])})")
        kb = build_fill_confirm_kb(services.storage.pending_confirm_orders())
        if config.owner_id:
            try:
                await bot.send_message(config.owner_id, "\n".join(out), reply_markup=kb)
            except Exception:  # noqa: BLE001
                log.exception("send fill-confirm failed")

    # Застряглі ордери — пропозиція скасувати (і позначка 'впущена можливість')
    prices = {coin: data[coin]["price"] for coin in data}
    stale = []
    frozen = 0.0
    for o in services.storage.open_orders():
        price = prices.get(o["coin"])
        if price and now - o["created_at"] > config.stale_days * DAY and price > o["price"] * 1.10:
            services.storage.mark_runaway(o["id"])  # ціна втекла вгору — урок на майбутнє
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


async def evaluation_check(bot, services):
    """Раз на день оцінюємо виконані докупки, у яких минув горизонт 30/90 днів."""
    try:
        data, _ = await gather(services)
    except Exception:  # noqa: BLE001
        log.exception("evaluation_check: gather failed")
        return
    try:
        n = learning.evaluate_orders(services.storage, data)
        if n:
            log.info("evaluation_check: оцінено %d докупок", n)
    except Exception:  # noqa: BLE001
        log.exception("evaluation_check: evaluate_orders failed")


async def monthly_budget_check(bot, services):
    """1-ше число — питаємо суму на місяць; наприкінці місяця мʼяко нагадуємо про залишок."""
    # Перенос залишку, якщо активний бюджет лишився з минулого місяця.
    carried = budget.rollover_if_needed(services.storage)
    if not services.storage.alerts_enabled():
        return

    today = datetime.date.today()
    st = budget.status(services.storage)

    # 1-ше число місяця: підсумок навчання за період + просимо задати бюджет.
    if today.day == 1:
        report_key = f"budget_report_{st['month']}"
        if not services.storage.alert_recent(report_key, 20 * DAY):
            s = learning.summary(services.storage)
            if s["has_data"]:
                await _send(bot, "🗓 Підсумок за період:\n\n" + _history_text(s))
            services.storage.mark_alert(report_key)

        key = f"budget_ask_{st['month']}"
        if not services.storage.alert_recent(key, 20 * DAY):
            carry = services.storage.get_budget().get("carry", 0.0)
            lines = ["📅 Новий місяць! Скільки відкладаємо на BTC/ETH цього місяця?"]
            if carry > 0:
                lines.append(f"Залишок з минулого {fmt_usd(carry)} перенесено — додам зверху.")
            lines.append("Напиши, напр.: /budget 300")
            await _send(bot, "\n".join(lines))
            services.storage.mark_alert(key)
        return

    # Кінець місяця, бюджет ще не вичерпано і відстаємо — мʼяко нагадаємо.
    if st["active"] and st["remaining"] > 0 and today.day >= 22 and st["behind"] > 0:
        key = f"budget_behind_{st['month']}"
        if not services.storage.alert_recent(key, 7 * DAY):
            await _send(
                bot,
                f"⏳ До кінця місяця {st['days_left']} дн., а з бюджету ще лишилось "
                f"{fmt_usd(st['remaining'])}. Доберемо на гарних цінах — стежу за ринком. "
                "Можеш і вручну: /invest <сума>.",
            )
            services.storage.mark_alert(key)


# Повторні нагадування про купівлю ETF: лише в ці дні місяця і лише якщо статус
# досі «нагадано». Більше — це вже досаждання, а не допомога.
IBKR_REPEAT_DAYS = (5, 12, 20)


async def ibkr_check(bot, services, today: Optional[datetime.date] = None):
    """Нагадування про щомісячну купівлю ETF на IBKR: основне + кілька повторних.

    Дату можна передати явно — це потрібно тестам, планувальник її не передає.
    """
    if not services.storage.alerts_enabled() or _quiet_now():
        return
    today = today or datetime.date.today()
    month = ibkr.month_key(today)
    record = services.storage.ibkr_get_month(month)

    if record is None:
        # Умова «сьогодні вже не раніше першого робочого дня», а не «саме той день»:
        # інакше нагадування зникне назовсім, якщо того дня бот лежав.
        if today < ibkr.first_business_day(today.year, today.month):
            return
        plan = await ibkr_plan(services, today)
        services.storage.ibkr_ensure_month(
            month, plan["fund"].ticker, plan["amount"], plan["price"]
        )
        await _send(
            bot,
            ibkr.reminder_text(plan),
            reply_markup=build_ibkr_kb(month, ibkr.STATUS_REMINDED),
        )
        services.storage.mark_alert(f"ibkr_remind_{month}")
        return

    # Уже зроблено або свідомо пропущено — цього місяця більше не турбуємо.
    if record.get("status") != ibkr.STATUS_REMINDED or today.day not in IBKR_REPEAT_DAYS:
        return
    key = f"ibkr_repeat_{month}_{today.day}"
    if services.storage.alert_recent(key, 20 * DAY):
        return
    plan = await ibkr_plan(services, today)
    await _send(
        bot,
        ibkr.reminder_text(plan, repeat=True),
        reply_markup=build_ibkr_kb(month, record.get("status")),
    )
    services.storage.mark_alert(key)


async def ibkr_glide_check(bot, services, today: Optional[datetime.date] = None):
    """Заздалегідь (за місяць-два) попереджає про зміну структури портфеля."""
    if not services.storage.alerts_enabled() or _quiet_now():
        return
    warn = ibkr.stage_warning(
        ibkr.strategy_start(services.storage), today or datetime.date.today()
    )
    if warn is None:
        return
    key = f"ibkr_glide_{warn['stage'].year}"
    if services.storage.alert_recent(key, 25 * DAY):
        return
    await _send(bot, ibkr.stage_warning_text(warn))
    services.storage.mark_alert(key)


# Стрибком вважаємо ріст удвічі АБО на 2 пункти: дрібні коливання ставок —
# щоденна норма бірж, і сповіщати про них означає привчити ігнорувати бота.
RATE_JUMP_PP = 0.02
RATE_JUMP_MULT = 2.0
# Менша вигода не варта того, щоб відривати людину від справ.
MIN_GAIN_YEAR = 20.0
# З якої ставки монета цікава, навіть якщо її зараз немає в портфелі.
NOTABLE_APR = 0.08
# Скільки знахідок показуємо за раз у кожному блоці. Було 2-3 — половина
# зникала назавжди, бо позначалась переглянутою, так і не потрапивши в текст.
MAX_PER_BLOCK = 6
# Стейбли для гаманця — одні й ті самі гроші, тож у резервному шляху без ключів
# тримаємо їх під одним тикером, а не розмножуємо по всіх.
STABLE_PROXY = "USDT"


# Тижневий тригер: за 7 днів навіть повільне зростання має дати хоча б пункт
# і третину зверху. Пороги мʼякші за денні (2 пункти / удвічі) саме тому, що
# сходи 4% → 4.5% → 5% → 6% денною міркою невидимі, а за тиждень це вже інша ставка.
WEEK_RATE_PP = 0.01
WEEK_RATE_MULT = 1.3
WEEK_RATE_DAYS = 5


def _jumped(now: float, was: float) -> bool:
    # Допуск потрібен, бо 0.06 - 0.04 у дробових дає 0.0199999 і рівний поріг губиться.
    return now - was >= RATE_JUMP_PP - 1e-9 or now >= was * RATE_JUMP_MULT - 1e-9


def _grew_over_week(now: float, was: float) -> bool:
    """Повільне, але помітне зростання за тиждень — другий тригер поруч зі стрибком."""
    return now - was >= WEEK_RATE_PP - 1e-9 and now >= was * WEEK_RATE_MULT - 1e-9


def _minus_reserved(held: dict, free: float, reserved: float) -> tuple[dict, float]:
    """Прибирає з вільних грошей ті, що вже чекають на просадку у виставлених заявках.

    Це той самий гаманець, показаний двічі, тому віднімаємо і з `free`, і зі
    стейблів у `held` — інакше бот порадив би замкнути в earn долари, які
    зникнуть тієї ж миті, коли спрацює лімітка на BTC.
    """
    if reserved <= 0:
        return held, free
    free = max(free - reserved, 0.0)
    left = reserved
    out = dict(held)
    for coin in sorted(out, key=lambda c: -out[c]):
        if left <= 0:
            break
        if coin in STABLES and out[coin] > 0:
            cut = min(out[coin], left)
            out[coin] -= cut
            left -= cut
    return out, free


async def _earn_amounts(services) -> tuple[dict, float]:
    """Скільки доларів у кожній монеті й скільки з них ВІЛЬНІ для earn.

    З ключами беремо реальні залишки бірж, без ключів — памʼять /earn і журнал
    виконаних ордерів. Потрібно, щоб рахувати вигоду в грошах, а не у відсотках.
    """
    reserved = 0.0
    try:
        reserved = services.storage.reserved_for_buys()
    except Exception:  # noqa: BLE001
        log.warning("earn_check: не вдалось порахувати зарезервоване", exc_info=True)

    reader = getattr(services, "portfolio", None)
    if reader is not None:
        try:
            pf = await reader.fetch()
            if not pf.empty:
                held = {coin: sum(h.value() for h in hs)
                        for coin, hs in pf.by_coin().items()}
                return _minus_reserved(held, pf.free_stables(), reserved)
        except Exception:  # noqa: BLE001
            log.warning("earn_check: баланси бірж недоступні", exc_info=True)

    out: dict = {}
    free = services.storage.get_earn_amount()
    if free > 0:
        # Одна й та сама сума під кожним тикером давала кратне завищення при
        # сумуванні. Стейбли взаємозамінні, тому рахуємо ці гроші РАЗ.
        out[STABLE_PROXY] = free
    try:
        data, _ = await gather(services)
        for coin, pos in services.storage.filled_positions().items():
            price = (data.get(coin) or {}).get("price")
            qty = pos.get("qty") or 0.0
            if price and qty:
                out[coin] = qty * price
    except Exception:  # noqa: BLE001
        log.warning("earn_check: не вдалось оцінити залишки монет", exc_info=True)
    return _minus_reserved(out, free, reserved)


def _alt_warning(coin: str) -> str:
    """Чесне попередження про альткоїн — один текст на всі блоки, щоб не різнився."""
    return (f"   ⚠️ Ставка тут плаває щодня, а сам {coin} може просісти в ціні — "
            "це не депозит у доларах.")


def _rate_news(rates: dict, prev: dict, amounts: dict, free_usd: float = 0.0,
               skip: Optional[set] = None, week_prev: Optional[dict] = None) -> list[str]:
    """Рядки про монети, де ставка підскочила І до неї є що прикласти.

    Без привʼязки до реальних грошей це перетворюється на стрічку котирувань:
    ставки позикового ринку на альткоїнах стрибають удвічі мало не щодня.
    Два тригери: різкий стрибок від минулого разу і повільне зростання за тиждень.
    """
    skip = skip or set()
    week_prev = week_prev or {}
    mine, maybe, stable_shown = [], [], False
    for coin, (apr, offer) in sorted(rates.items(), key=lambda kv: -kv[1][0]):
        was = prev.get(coin, 0.0)
        was_week = week_prev.get(coin, 0.0)
        jumped = bool(was) and _jumped(apr, was)
        # Повільне «повзло тиждень» не шлемо: позикові ставки дригаються щодня,
        # а їй потрібен лише помітний стрибок, не щоденний курс.
        if coin in skip or not jumped:
            continue
        # По стейблах достатньо однієї найкращої: для гаманця це ті самі гроші.
        if coin in STABLES:
            if stable_shown:
                continue
            stable_shown = True

        head = f"• {coin} у {offer.label}: {apr * 100:.2f}% замість {was * 100:.2f}%"
        limit = rate_limit_note(offer)
        if limit:
            head += f" ({limit})"
        held = amounts.get(coin, 0.0)
        if held > 0:
            # Підскочила ставка ПЕРШОЇ сходинки, тож і приріст рахуємо на тих грошах,
            # які в цю сходинку влазять. Інакше порівнювали б різні речі.
            fits = min(held, offer.steps()[0].capacity())
            gain = (apr - was) * fits
            if gain >= MIN_GAIN_YEAR:
                mine.append(f"{head}\n   На твоїх {fmt_usd(fits)} це ~{fmt_usd(gain)}/рік зверху.")
        elif free_usd > 0 and apr >= NOTABLE_APR and not maybe:
            # Монети немає, але є вільні долари. Річну суму тут не називаємо: ставка
            # позикового ринку живе днями, і «$975/рік» було б обіцянкою з повітря.
            fits = min(free_usd, offer.capacity())
            gain = offer.per_year(fits)
            if gain >= MIN_GAIN_YEAR:
                maybe.append(
                    f"{head}\n   На {fmt_usd(fits)} це ~{fmt_usd(gain / 12)}/міс, поки "
                    f"ставка тримається.\n{_alt_warning(coin)}"
                )
    # Спершу те, де вже лежать гроші: воно надійніше й ближче до діла.
    return (mine + maybe)[:MAX_PER_BLOCK]


def _product_key(o) -> str:
    """Ключ пропозиції: новий продукт по вже відомій монеті теж має вважатись новим."""
    return f"{o.exchange}:{o.coin}:{o.kind}"


def _new_products(services, offers: list, amounts: dict, free_usd: float,
                  skip: Optional[set] = None) -> tuple[list[str], set, set]:
    """Пропозиції, яких минулого разу не було.

    Перший запуск лише запамʼятовує стан: інакше «новим» виявиться весь ринок
    і користувачка отримає простирадло на десятки рядків.

    Повертає (рядки, монети, ключі, які можна вважати опрацьованими). Позначати
    переглянутим тут не можна: раніше позначалось УСЕ, а показувалось двоє, і
    решта зникала назавжди, так і не потрапивши в жодне повідомлення.
    """
    skip = skip or set()
    watched = [o for o in offers if o.coin in WATCHED]
    seen = services.storage.seen_products()
    if not seen:
        services.storage.mark_products_seen(_product_key(o) for o in watched)
        return [], set(), set()

    fresh = [o for o in watched if _product_key(o) not in seen and o.coin not in skip]
    lines, coins, done = [], set(), set()
    for o in sorted(fresh, key=lambda x: -x.steps()[0].apr):
        if len(lines) >= MAX_PER_BLOCK:
            break
        if o.coin in coins:
            # Та сама монета в другому продукті — це не друга новина, а той самий
            # ATOM під трохи іншим відсотком. Показуємо лише найкращий варіант.
            done.add(_product_key(o))
            continue
        apr = o.steps()[0].apr
        held = amounts.get(o.coin, 0.0)
        # Той самий поріг вигоди, що й для стрибків ставки: без нього сюди
        # потрапляв продукт на $0.50 за рік. Немає монети — рахуємо на вільні гроші.
        money = held if held > 0 else free_usd
        if o.per_year(min(money, o.capacity())) < MIN_GAIN_YEAR:
            done.add(_product_key(o))       # вигоди нема — це вже опрацьовано
            continue

        part = [f"• {o.label} · {o.coin} — {apr * 100:.2f}%"]
        limit = rate_limit_note(o)
        if limit:
            part[0] += f" ({limit})"
        if o.risk in RISK_VOLATILE:
            # Не долари: сама монета може просісти, і про це треба сказати вголос.
            if o.risk == RISK_HIGH and apr < NOTABLE_APR:
                done.add(_product_key(o))
                continue
            part.append(f"   Тримати треба саме {o.coin}, а не долари.")
            part.append(_alt_warning(o.coin))
        elif held > 0:
            part.append(f"   У тебе цієї монети вже є — можна скористатись.")
        elif o.coin in STABLES and free_usd > 0:
            part.append("   Це долари: сума не міняється, ризик лише в самій біржі.")
        else:
            part.append(f"   {o.coin} у тебе зараз немає — просто зʼявилась така можливість.")
        if o.section:
            part.append(f"   шукати: {o.section}")
        lines.append("\n".join(part))
        coins.add(o.coin)
        done.add(_product_key(o))
    return lines, coins, done


def _split_lines(u: dict) -> list[str]:
    """Розклад по кількох місцях — коли він дає більше за один переїзд.

    Підвищена ставка діє лише на перші кількасот доларів, тому «двісті сюди,
    решта туди» цілком буває вигіднішим за будь-який один переїзд.
    """
    split = u.get("split")
    if not split:
        return []
    parts = " + ".join(
        f"{fmt_usd(p['usd_value'])} у {p['offer'].label} під {p['apr'] * 100:.2f}%"
        for p in split["parts"]
    )
    return [
        f"   А ще краще розкласти: {parts}.",
        f"   Разом це ~{fmt_usd(split['gain_year_usd'])}/рік зверху.",
    ]


def _upgrade_signature(u: dict) -> str:
    """Підпис конкретної пропозиції переїзду — без суми в доларах, яка постійно

    дрібно дрейфує разом із нарахованими відсотками. Якщо підпис той самий, що
    й минулого разу, це не новина — гроші просто досі лежать там само.
    """
    target = u["offer"]
    return "|".join((
        u["from_exchange"], u["from_product"] or "", f"{round(u['from_apr'], 4)}",
        target.exchange, target.product or "", f"{round(u['to_effective_apr'], 4)}",
    ))


async def _upgrade_news(services, offers: list,
                        skip: Optional[set] = None) -> tuple[list[str], set, dict]:
    """Що варто перекласти, рахуючи з реальних залишків на біржах.

    Порожньо, якщо ключів немає — тоді працюють інші блоки. Пропозицію, яку вже
    показали і яка не змінилась, повторно в текст не кладемо — лише тримаємо
    монету в other-блоках skip, щоб про неї не писали вдруге деінде.
    """
    reader = getattr(services, "portfolio", None)
    if reader is None:
        return [], set(), {}
    try:
        pf = await reader.fetch()
    except Exception:  # noqa: BLE001
        log.warning("earn_check: не вдалось прочитати баланси бірж", exc_info=True)
        return [], set(), {}
    if pf.empty:
        return [], set(), {}

    skip = skip or set()
    storage = getattr(services, "storage", None)
    seen_sigs = storage.earn_seen_upgrades() if storage else {}
    lines, coins, sigs = [], set(), {}
    for u in upgrades(pf, offers, MIN_GAIN_YEAR):
        if u["coin"] in skip:
            continue
        coins.add(u["coin"])
        sig = _upgrade_signature(u)
        sigs[u["coin"]] = sig
        if seen_sigs.get(u["coin"]) == sig:
            continue  # та сама пропозиція, що й минулого разу — вже показали
        if len(lines) >= MAX_PER_BLOCK:
            continue
        target = u["offer"]
        src = u["from_product"] or u["from_exchange"]
        where = ("просто лежить без діла" if u["from_where"] == SPOT
                 else f"лежить у «{src}» під {u['from_apr'] * 100:.2f}%")
        part = [
            f"• {u['coin']}: {fmt_usd(u['usd_value'])} на {u['from_exchange']} {where}.",
            f"   Краще у {target.label} — {u['to_effective_apr'] * 100:.2f}% на цю суму, "
            f"це ~{fmt_usd(u['gain_year_usd'])}/рік зверху.",
        ]
        if target.section:
            part.append(f"   шукати: {target.section}")
        if not u["same_exchange"]:
            part.append(f"   ⚠️ Це інша біржа — знадобиться переказ на {u['to_exchange']}.")
        part += _split_lines(u)
        lines.append("\n".join(part))
    return lines, coins, sigs


# Скільки акцій узагалі віддаємо пораднику за один раз. Біржі сиплють по 40
# оголошень на добу; читати 40 сторінок і питати про них AI двічі на день — це
# і повільно, і ризиковано по квоті ключа.
PROMO_CANDIDATES = 10


async def _promo_news(services, amounts: Optional[dict] = None, free_usd: float = 0.0,
                      rates: Optional[dict] = None, today=None) -> tuple[list[str], set, dict]:
    """Нові акції про дохід з вердиктом. Перший запуск лише запамʼятовує.

    Контекст її грошей передаємо всередину: без нього це знову був би переказ
    заголовка, а вона просила саме відповідь «чи варто вкладатись».
    Дату можна передати явно — це потрібно тестам про протерміновані акції.

    Повертає (рядки, ключі опрацьованих акцій, варті акції з дедлайном).
    Позначає переглянутим не тут, а після успішного надсилання.
    """
    try:
        promos = await services.earn.promos()
    except Exception:  # noqa: BLE001
        log.warning("earn_check: акції недоступні", exc_info=True)
        return [], set(), {}, set()

    # Словами відкидаємо лише явне сміття, решту віддаємо пораднику: рішення
    # «чи це взагалі пропозиція про дохід» він приймає по ТЕКСТУ оголошення.
    good = [p for p in promos if p.candidate]
    seen = services.storage.seen_promos()
    if not seen:
        services.storage.mark_promos_seen([p.key for p in good])
        return [], set(), {}, set()
    muted = services.storage.earn_muted()
    fresh = [p for p in good if p.key not in seen and f"promo:{p.key}" not in muted]
    if not fresh:
        return [], set(), {}, set()

    fresh = fresh[:PROMO_CANDIDATES]
    try:
        bodies = await services.earn.promo_bodies(fresh, limit=len(fresh))
    except Exception:  # noqa: BLE001
        log.warning("earn_check: тексти акцій недоступні", exc_info=True)
        bodies = {}
    explains = await promo_advice(fresh, amounts, free_usd, rates, bodies)

    # Не пропозиція або вже протермінована — опрацьовано й більше не потрібне.
    offers, done = [], set()
    for p in fresh:
        ex = explains.get(p.title) or {}
        if ex.get("is_offer") is False or promo_expired(ex, today):
            done.add(p.key)
        else:
            offers.append(p)

    shown = offers[:MAX_PER_BLOCK]
    lines = promo_lines(shown, explains, today)
    done |= {p.key for p in shown}

    # Варті акції з дедлайном запамʼятовуємо окремо — щоб нагадати перед кінцем.
    watch, coins = {}, set()
    for p in shown:
        ex = explains.get(p.title) or {}
        coin = str(ex.get("coin") or "").upper()
        if coin:
            coins.add(coin)
        if ex.get("verdict") != "skip" and ex.get("period_end"):
            watch[p.key] = {
                "exchange": p.exchange,
                "title": ex.get("title_uk") or p.title,
                "url": p.url,
                "end": str(ex["period_end"])[:10],
            }
    return lines, done, watch, coins


async def _prices(services) -> dict:
    """Ціни монет — щоб дохід із вкладів у BTC/ETH теж рахувався в доларах."""
    try:
        data, _ = await gather(services)
    except Exception:  # noqa: BLE001
        log.warning("earn_check: ціни монет недоступні", exc_info=True)
        return {}
    return {c: d.get("price") for c, d in data.items() if d.get("price")}


async def _portfolio_or_none(services):
    reader = getattr(services, "portfolio", None)
    if reader is None:
        return None
    try:
        return await reader.fetch()
    except Exception:  # noqa: BLE001
        log.warning("earn_check: баланси бірж недоступні", exc_info=True)
        return None


async def _journal_gap_news(services, portfolio) -> list[str]:
    """Журнал проти реальних залишків: правда завжди за ключами.

    Журнал не сперечається з біржею — він лише показує, що частина грошей
    лежить невідомо під який відсоток, і про це варто знати.
    """
    deposits = services.storage.earn_deposits()
    rows = journal.reconcile(portfolio, deposits)
    if not rows:
        return []
    lines = reconcile_lines(rows[:MAX_PER_BLOCK])
    lines.append("Головне — те, що показують ключі бірж. Журнал лише додає до "
                 "цієї картини строки й ставки, під які ти заходила.")
    return lines


def _journal_rate_news(services, rates: dict, prices: dict) -> list[str]:
    """Ставка по вже зробленому вкладу просіла — і це видно лише завдяки журналу."""
    deposits = services.storage.earn_deposits()
    drops = journal.rate_drops(deposits, rates, MIN_GAIN_YEAR, prices)
    if not drops:
        return []
    better = {c: o for c, (_apr, o) in rates.items()}
    lines = rate_drop_lines(drops[:MAX_PER_BLOCK], better)
    for d in drops[:MAX_PER_BLOCK]:
        services.storage.earn_deposit_rate_alerted(d["deposit"]["id"], d["now_apr"])
    return lines


async def _alt_news(services, offers: list, reliability: dict,
                    skip: Optional[set] = None) -> tuple[list[str], str]:
    """Дрібна позиція в чужій монеті — лише з незадіяного залишку місячного бюджету.

    Це не нові гроші: джерело — те, що в бюджеті ще не куплено й не висить у
    заявках. Той самий залишок є подушкою на просадку крипти, тому беремо лише
    частину і лише туди, звідки можна забрати того ж дня.
    """
    skip = skip or set()
    free = budget.unused_free(services.storage)
    try:
        freeing = services.storage.reserved_for_buys()
    except Exception:  # noqa: BLE001
        freeing = 0.0

    for coin, info in reliability.items():
        if coin in skip or not alt_coin_allowed(info.get("facts")):
            continue
        variants = alt_offers(offers, coin)
        if not variants:
            continue
        offer = variants[0]
        # Скільки грошей узагалі влазить у верхню сходинку — більше туди класти
        # немає сенсу, ставка далі падає.
        cap = offer.steps()[0].capacity()
        needed = None if cap == float("inf") else cap
        pos = alt_position(free, needed)
        # Поріг вигоди для чужої монети свій: суми тут маленькі за визначенням.
        check = pos["amount"] if pos["amount"] > 0 else ALT_TOPUP_MAX
        if offer.per_year(check) < ALT_MIN_GAIN_YEAR:
            continue
        return alt_coin_lines(coin, offer, pos, info.get("facts"), freeing), coin
    return [], ""


async def journal_check(bot, services, today: Optional[datetime.date] = None):
    """Нагадування САМЕ В ДЕНЬ розморозки — і одразу з порадою, куди перекласти.

    Свідомо поза денним лімітом earn-сповіщень і окремим повідомленням на вклад:
    під кожним мають бути свої кнопки «забрала» / «продовжила», а відкласти це
    на завтра означає, що біржа встигне продовжити вклад сама.
    """
    if not services.storage.alerts_enabled() or _quiet_now():
        return
    today = today or datetime.date.today()
    deposits = services.storage.earn_deposits()
    due = journal.due_deposits(deposits, today)
    if not due:
        return

    try:
        offers = await services.earn.offers()
    except Exception:  # noqa: BLE001
        log.warning("journal_check: пропозиції недоступні", exc_info=True)
        offers = []
    rates = best_instant_rates(offers)

    for dep in due:
        coin = str(dep.get("coin") or "").upper()
        best = (rates.get(coin) or (None, None))[1]
        # Довгий вивід згадуємо окремо: на Bybit по BTC це буває 45 днів, і
        # починати треба сьогодні, а не коли гроші знадобляться.
        same = [o for o in offers if o.coin == coin and o.exchange == dep.get("exchange")]
        redeem = max((o.redeem_days for o in same), default=0.0)
        text = journal_due_text(dep, today, best, redeem)
        if await _send(bot, text, reply_markup=build_journal_due_kb(dep["id"])):
            services.storage.earn_deposit_reminded(dep["id"], today)


# Наскільки має зрушити ставка, щоб згадувати її в тижневому огляді.
WEEK_REVIEW_PP = 0.005


def _week_changes(rates: dict, week_prev: dict, amounts: dict) -> list[dict]:
    """Що змінилось за тиждень — з точними сумами на її грошах."""
    out = []
    for coin, (apr, _offer) in sorted(rates.items(), key=lambda kv: -kv[1][0]):
        was = week_prev.get(coin)
        if not was or abs(apr - was) < WEEK_REVIEW_PP:
            continue
        held = float(amounts.get(coin) or 0.0)
        out.append({
            "coin": coin,
            "was": was,
            "now": apr,
            "money_year": (apr - was) * held if held > 0 else 0.0,
        })
    return out


async def weekly_earn_review(bot, services, today: Optional[datetime.date] = None):
    """Недільний огляд ТІЛЬКИ по вкладах — і лише коли справді є про що казати.

    Ні крипти, ні ETF тут немає свідомо: змішаний звіт читається як стрічка
    новин, а цей має відповідати на одне питання — що зараз із її вкладами.
    """
    if not services.storage.alerts_enabled() or _quiet_now():
        return
    today = today or datetime.date.today()
    if today.weekday() != 6:        # неділя
        return
    if services.storage.alert_recent("earn_weekly", 5 * DAY):
        return

    try:
        offers = await services.earn.offers()
    except Exception:  # noqa: BLE001
        log.warning("weekly_earn_review: пропозиції недоступні", exc_info=True)
        return

    active = services.storage.earn_deposits()
    amounts, _free = await _earn_amounts(services)
    rates = best_instant_rates(offers, WATCHED)
    changes = _week_changes(rates, services.storage.earn_rates_days_ago(7, today), amounts)

    # «Є про що казати» — це або зміна ставок, або зміна самих вкладів.
    snapshot = {str(d["id"]): round(float(d.get("amount") or 0), 8) for d in active}
    same_positions = snapshot == services.storage.earn_week_snapshot()
    if not changes and same_positions:
        return

    coins = sorted({str(d.get("coin") or "").upper() for d in active} | {c for c in amounts if amounts[c] > 0})
    best = {c: rates[c][1] for c in coins if c in rates}
    prices = await _prices(services)
    text = weekly_review_text(active, changes, best, prices)
    if await _send(bot, text):
        services.storage.set_earn_week_snapshot(snapshot)
        services.storage.mark_alert("earn_weekly")


async def earn_check(bot, services):
    """Стежить за пропозиціями і сам пише, коли зʼявляється щось справді нове.

    Тригер має бути від бота, а не від користувача — інакше вигідне проходить мимо.
    Але й шуміти не можна: звідси поріг вигоди, список монет і одне сповіщення на день.
    """
    if not services.storage.alerts_enabled() or _quiet_now():
        return
    if getattr(services, "earn", None) is None or services.storage.earn_quota_left() <= 0:
        return
    try:
        offers = await services.earn.offers()
    except Exception:  # noqa: BLE001
        log.exception("earn_check: не вдалось отримати пропозиції")
        return

    rates = best_instant_rates(offers, WATCHED)
    prev = services.storage.get_earn_rates()
    amounts, free = await _earn_amounts(services)

    # Про що просили не нагадувати — не нагадуємо (кнопка «не цікаво»).
    muted_coins = {t.split(":", 1)[1] for t in services.storage.earn_muted()
                   if t.startswith("coin:")}

    # Блоки додаються один до одного, але монета згадується лише раз: спершу там,
    # де про неї сказано найконкретніше.
    mine, seen_coins, upgrade_sigs = await _upgrade_news(services, offers, skip=muted_coins)
    seen_coins |= muted_coins
    fresh, new_coins, done_products = _new_products(services, offers, amounts, free,
                                                    skip=seen_coins)
    seen_coins |= new_coins
    jumps = _rate_news(rates, prev, amounts, free, seen_coins)
    if rates:
        services.storage.set_earn_rates({c: apr for c, (apr, _) in rates.items()})
    promos, done_promos, watch, promo_coins = await _promo_news(services, amounts,
                                                                free, rates)

    # Незнайомі монети, які щойно згадали: для них ризик самої монети важливіший
    # за ставку, тому окремий блок із віком, розміром і оборотом.
    unknown = [c for c in (new_coins | promo_coins)
               if classify_risk(c) == RISK_HIGH and not amounts.get(c)]
    trust = await coin_reliability(getattr(services, "coin_facts", None), unknown[:2])
    trust_lines = coin_trust_lines(trust, free, rates)

    # Журнал: ставка по вже зробленому вкладу і розбіжність із біржею. Ні того,
    # ні іншого не видно через ключі — саме заради цього журнал і існує.
    prices = await _prices(services)
    drops = _journal_rate_news(services, rates, prices)
    alt_lines, alt_coin = await _alt_news(services, offers, trust, skip=seen_coins)

    blocks = [
        ("💡 Є краще місце для твоїх грошей:", mine),
        ("📉 Ставка по твоєму вкладу впала:", drops),
        ("🆕 Нові пропозиції на біржах:", fresh),
        ("📈 Ставки підросли:", jumps),
        ("🧐 Що це за монети:", trust_lines),
        ("🎁 Нові акції:", promos),
    ]
    messages = _compose(blocks)
    if not messages and not alt_lines:
        return

    # Кнопки «не цікаво» — під останнім повідомленням, щоб не дублювати їх у кожному.
    keyboard = build_earn_mute_kb(services.storage, sorted(seen_coins))
    sent = False
    for i, text in enumerate(messages):
        ok = await _send(bot, text, reply_markup=keyboard if i == len(messages) - 1 else None)
        sent = sent or ok

    # Порада по чужій монеті йде окремим повідомленням: під нею свої кнопки, і
    # рішення «докидати чи ні» не має губитись усередині стрічки новин.
    if alt_lines:
        ok = await _send(bot, "🪙 Монета, якої в тебе немає:\n\n" + "\n\n".join(alt_lines),
                         reply_markup=build_alt_topup_kb(alt_coin, [50, int(ALT_TOPUP_MAX)]))
        sent = sent or ok

    # Позначаємо переглянутим ЛИШЕ те, що реально пішло. Раніше позначалось усе,
    # і знахідки, які не влізли, зникали назавжди.
    if not sent:
        return
    if done_products:
        services.storage.mark_products_seen(done_products)
    if done_promos:
        services.storage.mark_promos_seen(done_promos)
    if upgrade_sigs:
        services.storage.mark_upgrades_seen(upgrade_sigs)
    for key, info in watch.items():
        services.storage.earn_watch_promo(key, info)
    services.storage.mark_earn_notified()


# За скільки днів до кінця акції нагадуємо. Один день — ризик не встигнути,
# три — ще рано й забудеться.
PROMO_DEADLINE_DAYS = 2


async def promo_deadline_check(bot, services, today: Optional[datetime.date] = None):
    """Одне нагадування про варту акцію, яка ось-ось закінчиться.

    Свідомо НЕ рахується в денний ліміт earn-сповіщень: це єдина новина, яку не
    можна відкласти на завтра, бо завтра акції вже не буде.
    """
    if not services.storage.alerts_enabled() or _quiet_now():
        return
    today = today or datetime.date.today()
    watch = services.storage.earn_watch_promos()
    if not watch:
        return

    muted = services.storage.earn_muted()
    finished, due = [], []
    for key, info in watch.items():
        left = _days_left(info, today)
        if left is None or left < 0:
            finished.append(key)            # уже закінчилась — прибираємо з памʼяті
        elif (not info.get("reminded") and left <= PROMO_DEADLINE_DAYS
                and f"promo:{key}" not in muted):
            due.append((key, info, left))

    if due:
        lines = []
        for _, info, left in due:
            tail = "сьогодні останній день" if left == 0 else f"лишилось {left} дн."
            part = [f"• {info.get('exchange', '')}: «{info.get('title', '')}» — {tail}"]
            if info.get("url"):
                part.append(f"   🔗 {info['url']}")
            lines.append("\n".join(part))
        text = "⏳ Акція ось-ось закінчиться — якщо збиралась, то зараз\n\n" + "\n\n".join(lines)
        if await _send(bot, text):
            # Позначаємо лише після успішного надсилання: інакше єдине
            # нагадування згоріло б через тимчасовий збій Telegram.
            for key, _, _ in due:
                services.storage.earn_promo_reminded(key)
    if finished:
        services.storage.earn_drop_watched(finished)


def _days_left(info: dict, today: datetime.date) -> Optional[int]:
    try:
        return (datetime.date.fromisoformat(str(info.get("end"))[:10]) - today).days
    except (TypeError, ValueError):
        return None


# Telegram ріже на 4096, але з телефона нечитабельним стає значно раніше.
MAX_MESSAGE = 2800


FOOTER = "Розклад суми по біржах — /earn"


def _compose(blocks: list) -> list[str]:
    """Складає блоки в ОДНЕ АБО КІЛЬКА повідомлень підряд.

    Раніше задовге повідомлення вирішувалось викиданням найменш важливого блоку —
    і знахідка просто зникала, ще й позначена переглянутою. Тепер нічого не
    губиться: що не влізло, їде наступним повідомленням.

    Порядок у blocks і є порядком важливості: її гроші, нові пропозиції,
    стрибки ставок, надійність монет, акції.
    """
    messages: list[str] = []
    current: list[str] = []

    def flush():
        if current:
            messages.append("\n".join(current).rstrip())
            current.clear()

    for title, lines in blocks:
        if not lines:
            continue
        header = title
        for entry in lines:
            chunk = ([header, ""] if header else []) + [entry, ""]
            # +1 на перенос рядка і запас на підпис у кінці останнього повідомлення.
            if current and len("\n".join(current + chunk)) + len(FOOTER) + 2 > MAX_MESSAGE:
                flush()
                header = f"{title} (продовження)"
                chunk = [header, "", entry, ""]
            current += chunk
            header = ""     # заголовок блоку друкуємо лише перед першим рядком
    flush()
    if not messages:
        return []
    messages[-1] = messages[-1] + "\n\n" + FOOTER
    return messages


async def earn_income_check(bot, services, today: Optional[datetime.date] = None):
    """Щоденний знімок заробітку з earn-позицій. Тихо, без повідомлень у чат.

    Це не новина, а рядок у базі: з ряду таких знімків потім і виходить
    «скільки я заробила за тиждень» і фактична ставка. Показують це журнал
    вкладів і /status, а не сповіщення.

    Стійкість тут головна. Якщо біржа не відповіла — НЕ пишемо нуль і не
    ламаємо ряд: пропускаємо добу з поміткою. Нуль у ряді неможливо потім
    відрізнити від справжнього «нічого не заробила», а на різниці знімків
    така помилка перетворюється на видуманий заробіток.
    """
    reader = getattr(services, "portfolio", None)
    if reader is None:
        return                  # без ключів рахувати нічого
    today = today or datetime.date.today()
    storage = services.storage

    prices = await _prices(services)
    try:
        portfolio = await reader.fetch(prices)
    except Exception:  # noqa: BLE001
        log.warning("earn_income_check: баланси бірж недоступні — знімок пропущено",
                    exc_info=True)
        storage.earn_income_skip(today, ["ключі бірж"])
        return

    # Обіцяна ставка потрібна там, де біржа не віддає її в самій позиції
    # (Bybit): без неї фактичну ставку буде ні з чим порівняти.
    try:
        offers = await services.earn.offers()
    except Exception:  # noqa: BLE001
        log.warning("earn_income_check: публічні ставки недоступні", exc_info=True)
        offers = []

    rows = income.snapshot_rows(portfolio, prices, income.promised_rates(offers),
                                today, storage.earn_income_latest())
    if rows:
        storage.earn_income_add(rows)
    failed = list(getattr(portfolio, "failed", []) or [])
    if failed:
        storage.earn_income_skip(today, failed)
    log.info("earn_income_check: знімок на %s — позицій %d%s", today, len(rows),
             f"; не відповіли: {', '.join(failed)}" if failed else "")


async def daily_backup(bot, services):
    """Щоденний локальний бекап бази (ротація) + раз на тиждень — у Telegram (офсайт)."""
    src = config.db_path
    if not os.path.exists(src):
        return
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        today = datetime.date.today().isoformat()
        dst = os.path.join(BACKUP_DIR, f"invest_bot_{today}.db")
        shutil.copy2(src, dst)
        # Ротація: лишаємо останні BACKUP_KEEP копій.
        backups = sorted(glob.glob(os.path.join(BACKUP_DIR, "invest_bot_*.db")))
        for old in backups[:-BACKUP_KEEP]:
            try:
                os.remove(old)
            except OSError:
                pass
    except Exception:  # noqa: BLE001
        log.exception("daily_backup: локальний бекап не вдався")
        return

    # Понеділок — надсилаємо копію в Telegram (офсайт-бекап на випадок втрати сервера).
    if datetime.date.today().weekday() == 0 and config.owner_id:
        try:
            await bot.send_document(
                config.owner_id,
                FSInputFile(dst),
                caption="🗄 Тижневий бекап бази (історія, бюджет, навчання). Збережи про запас.",
            )
        except Exception:  # noqa: BLE001
            log.exception("daily_backup: надсилання в Telegram не вдалось")


def setup_scheduler(bot, services) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(market_check, "interval", minutes=60, args=[bot, services])
    scheduler.add_job(daily_check, "cron", hour=12, minute=0, args=[bot, services])
    scheduler.add_job(evaluation_check, "cron", hour=11, minute=0, args=[bot, services])
    scheduler.add_job(monthly_budget_check, "cron", hour=10, minute=30, args=[bot, services])
    # Знімок заробітку — раз на добу і ПЕРЕД бекапом, щоб свіжий рядок одразу
    # потрапляв у копію бази: цей ряд накопичується роками й відновити його
    # заднім числом уже не вийде.
    scheduler.add_job(earn_income_check, "cron", hour=3, minute=30, args=[bot, services])
    scheduler.add_job(daily_backup, "cron", hour=4, minute=0, args=[bot, services])
    # ETF на IBKR: щодня перевіряємо, чи пора нагадати про купівлю цього місяця.
    scheduler.add_job(ibkr_check, "cron", hour=10, minute=15, args=[bot, services])
    scheduler.add_job(ibkr_glide_check, "cron", hour=10, minute=45, args=[bot, services])
    # Earn-сповіщення — раз на день і лише коли щось змінилось (див. earn_check).
    scheduler.add_job(earn_check, "cron", hour=10, minute=15, args=[bot, services])
    # Дедлайни акцій — окремо й раніше за earn_check: це нагадування не рахується
    # в денний ліміт, бо відкласти його на завтра означає його втратити.
    scheduler.add_job(promo_deadline_check, "cron", hour=9, minute=5, args=[bot, services])
    # Розморозка вкладів — теж поза лімітом і теж зранку: за день біржа встигає
    # продовжити вклад сама, зазвичай уже під гіршу ставку.
    scheduler.add_job(journal_check, "cron", hour=9, minute=30, args=[bot, services])
    # Недільний огляд вкладів — після ранкових перевірок, щоб ставки були свіжі.
    scheduler.add_job(weekly_earn_review, "cron", day_of_week="sun", hour=11,
                      minute=30, args=[bot, services])
    return scheduler
