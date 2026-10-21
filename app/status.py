"""Повний стан її грошей одним копіювальним текстом — для чату зі стороннім AI.

У неї є платний Клод у телефоні, і міркувати вона хоче саме там. Бот дає те,
чого жоден зовнішній чат не знає: справжні цифри з ключів бірж, журналу вкладів
і своєї бази. Тому тут НЕМАЄ жодного запиту до порадника — /status має
працювати завжди, навіть коли безплатна квота Gemini вичерпана.

Головне про формат: цей текст читатиме AI, яка про неї не знає нічого. Тому в
дампі не самі цифри, а й що вони означають, і її правила. Без правил відповідь
буде загальною порадою з інтернету, і вся затія марна.
"""
from __future__ import annotations

import datetime
import logging
import time
from typing import Optional

from app import budget, ibkr, income, journal
from app.earn import (
    ALT_BUDGET_SHARE,
    ALT_MAX_RANK,
    ALT_MIN_AGE_YEARS,
    ALT_MIN_GAIN_YEAR,
    ALT_TOPUP_MAX,
    LOCK_MAX_DAYS,
    LOCK_MIN_GAIN_YEAR,
    STABLES,
    WATCHED,
    best_instant_rates,
)
from app.formatting import (
    QUOTE_CLOSE,
    QUOTE_OPEN,
    days_word,
    fmt_coin_amount,
    fmt_eur,
    fmt_eur_price,
    fmt_money,
    fmt_price,
    fmt_usd,
    income_block,
)
from app.portfolio import EARN, SPOT, upgrades
from app.service import allocation_context, gather, portfolio_pnl

log = logging.getLogger(__name__)

# Монети, ставку по яких показуємо завжди, навіть якщо їх зараз немає:
# саме в них лежать або будуть лежати основні гроші.
ALWAYS_RATES = ("USDT", "BTC", "ETH")
# Скільки монет максимум у блоці ставок. Далі йде хвіст із копійчаними
# залишками, а порівнювати пропозицію треба з тим, де реально лежать гроші.
MAX_RATE_LINES = 10
# Нижче цієї суми залишок — пил від старих переказів (типу 0.000000006 BTC).
# У дампі він лише з'їдає місце й збиває з пантелику того, хто це читатиме.
DUST_USD = 1.0
# З якого розходження ставка в позиції і публічна ставка того самого продукту —
# це вже дві різні цифри, а не округлення. OKX у Simple Earn віддає по позиції
# 1.00% і по USDT, і по ETH, тоді як публічно платить 3.50% і 1.00%: там ринок
# позик, і число в позиції більше схоже на виставлену мінімальну ставку.
# Мовчати про таку різницю не можна — вона змінює всю арифметику переїзду.
RATE_MISMATCH_PP = 0.0025
# Різниця, дрібніша за це, у блоці «краще місце» — шум: пара доларів на рік не
# варта ні переказу між біржами, ні місця в дампі, який вона вставляє руками.
BETTER_MIN_YEAR = 3.0
# Скільки рядків максимум у тому блоці. Далі йде хвіст на копійки, а дамп і так
# ріжеться на частини.
MAX_BETTER_LINES = 8


async def _safe(name: str, coro, failed: list, default=None):
    """Жодне джерело не має валити весь дамп: краще неповна картина, ніж жодної."""
    try:
        return await coro
    except Exception as e:  # noqa: BLE001
        log.warning("Стан: джерело %s недоступне (%s)", name, e)
        failed.append(name)
        return default


async def money_snapshot(services, today: Optional[datetime.date] = None) -> dict:
    """Усе, що бот знає про її гроші, зібране в один словник.

    Кожне джерело падає окремо: біржа може не відповісти, а бюджет і журнал
    лежать у своїй базі й доступні завжди.
    """
    today = today or datetime.date.today()
    storage = services.storage
    failed: list = []

    market = await _safe("ціни ринку", gather(services), failed, ({}, None))
    data, fng = market if market else ({}, None)
    data = data or {}
    prices = {c: (d or {}).get("price") for c, d in data.items() if (d or {}).get("price")}

    portfolio = None
    if getattr(services, "portfolio", None) is not None:
        portfolio = await _safe("ключі бірж", services.portfolio.fetch(prices), failed)
    offers = await _safe("ставки бірж", services.earn.offers(), failed, []) or []

    deposits = storage.earn_deposits()
    bstatus = budget.status(storage)
    bstatus["unused"] = budget.unused_free(storage)

    etf = getattr(services, "etf", None)
    fund = ibkr.fund_for_month(today)
    price = await ibkr.safe_price(etf, fund)
    plan = ibkr.month_plan(today, price)
    start = ibkr.strategy_start(storage)

    return {
        "now": datetime.datetime.now(),
        "today": today,
        "market": data,
        "fng": fng,
        "portfolio": portfolio,
        "reserved": storage.reserved_for_buys(),
        "budget": bstatus,
        "pnl": portfolio_pnl(storage, data),
        "orders": storage.open_orders(),
        "allocation": allocation_context(storage, data),
        "deposits": deposits,
        # Скільки earn-вклади вже принесли. Рахується з денних знімків у базі:
        # біржа віддає лише «зараз», а історію пам'ятає тільки бот.
        "income": income.summary(storage.earn_income(), prices, today),
        "income_skips": storage.earn_income_skips(),
        "reconcile": journal.reconcile(portfolio, deposits),
        "rates": best_instant_rates(offers, WATCHED),
        # Поріг 0: у дампі показуємо ВСЮ різницю, зокрема ту, що нижча за $20 і
        # тому не доходить до неї сповіщенням. Рішення її, а не бота.
        "upgrades": upgrades(portfolio, offers, 0.0) if portfolio is not None else [],
        # Публічна ставка по КОЖНОМУ продукту — щоб було з чим звірити ту, яку
        # біржа віддає в самій позиції.
        "product_rates": {(o.exchange, o.coin, o.product): o.apr for o in offers},
        "prices": prices,
        "ibkr": {
            "plan": plan,
            "record": storage.ibkr_get_month(plan["month"]),
            "totals": ibkr.totals(storage),
            "years": ibkr.years_elapsed(start, today),
        },
        "failed": failed,
    }


# --- секції дампа ---

def _frame_lines(snap: dict) -> list:
    """Рамка-запит на самому початку: щоб усе працювало з однієї вставки."""
    return [
        "Ось мій поточний фінансовий стан і мої правила. Далі я вставлю опис акції "
        "або питання — відповідай з урахуванням саме цих даних і правил, конкретними "
        "сумами, і не вигадуй умов, яких я не дала.",
        "Якщо в описі акції чогось немає (максимум суми, чи є блокування, чи ставка "
        "фіксована) — скажи, ЧОГО саме не хватає і що перевірити в застосунку. "
        "Краще «в тексті не сказано», ніж вигадана впевненість.",
        "Завжди дай дві цифри: скільки це грошей за фактичний строк пропозиції І "
        "скільки в перерахунку на рік. Одна річна цифра на триденній акції — це обман.",
        "Ніколи не пропонуй суму, якої в мене немає: дивись на «вільні гроші» нижче.",
        "",
        f"СТАН ЗНЯТО: {snap['now']:%d.%m.%Y о %H:%M}. Ціни й ставки рухаються — якщо "
        "відтоді минуло більше кількох годин, вважай цифри приблизними й скажи мені про це.",
    ]


def _who_lines() -> list:
    return [
        "Пасивна інвесторка, не торгую і не збираюсь: складаю гроші надовго.",
        "Крипта — довгострокове накопичення BTC та ETH лімітними заявками на просадках.",
        "Окремо, у євро — щомісячні внески в накопичувальні ETF на IBKR.",
        "Живу в Угорщині. Крипта тільки на OKX і Bybit, до обох підключені ключі "
        "лише на читання (мій бот бачить залишки, але нічого не купує сам).",
        "У крипті я не розбираюсь — пояснюй простими словами, без жаргону.",
    ]


def _market_lines(snap: dict) -> list:
    out = []
    for coin, d in (snap.get("market") or {}).items():
        price = (d or {}).get("price")
        if not price:
            continue
        change = (d or {}).get("change_24h")
        tail = f" (за добу {change * 100:+.1f}%)" if change is not None else ""
        out.append(f"{coin}: ${fmt_price(price)}{tail}")
    fng = snap.get("fng")
    if fng is not None:
        out.append(
            f"Індекс страху й жадібності ринку: {fng} зі 100 "
            "(нижче 30 — на ринку паніка й усе дешево, вище 70 — жадібність і дорого)."
        )
    if not out:
        out.append("Ціни зараз отримати не вдалось — про ринок нічого сказати не можу.")
    return out


def _free_lines(snap: dict) -> list:
    """Вільні долари по біржах і скільки з них насправді зайнято заявками."""
    pf = snap.get("portfolio")
    reserved = float(snap.get("reserved") or 0.0)
    out = []

    known = pf is not None and not getattr(pf, "empty", True)
    free_total = 0.0
    if not known:
        out.append("Реальних залишків зараз не видно (ключі бірж не відповіли або не "
                   "підключені), тому суму вільних доларів вважай невідомою.")
    else:
        by_exchange: dict = {}
        for h in pf.idle():
            if h.stable:
                by_exchange[h.exchange] = by_exchange.get(h.exchange, 0.0) + h.value()
        free_total = sum(by_exchange.values())
        out.append("Вільні долари (стейбли, які просто лежать і нічого не приносять):")
        shown = {e: v for e, v in by_exchange.items() if v >= DUST_USD}
        for exchange, value in sorted(shown.items(), key=lambda kv: -kv[1]):
            out.append(f"• {exchange}: {fmt_money(value)}")
        if not by_exchange:
            out.append("• на біржах вільних доларів немає — усе вже кудись покладено")
        out.append(f"Разом вільних доларів: {fmt_money(free_total)}")

    if reserved > 0:
        # Без відомої суми залишків «з них» не було б від чого відняти — і рядок
        # звучав би як загадка.
        lead = f"З них {fmt_usd(reserved)}" if known else f"{fmt_usd(reserved)} моїх грошей"
        out.append(
            f"{lead} зарезервовано під мої відкриті заявки на купівлю крипти. "
            "ЦЕ НЕ ВІЛЬНІ ГРОШІ: вони чекають на просадку і знадобляться саме тоді, "
            "коли ціна впаде. Не пропонуй класти їх кудись."
        )
        if known:
            out.append(f"Реально можна кудись покласти прямо зараз: "
                       f"{fmt_usd(max(free_total - reserved, 0.0))}")
    else:
        out.append("Під відкритими заявками на купівлю крипти зараз нічого не зарезервовано.")

    st = snap.get("budget") or {}
    if st.get("active"):
        # Місяць словами: «2026-09» у тексті для людини виглядає як помилка бота.
        line = (f"Місячний бюджет на {ibkr.month_name(snap['today'])}: всього "
                f"{fmt_usd(st['total'])}, реально куплено {fmt_usd(st['deployed'])}")
        if st.get("reserved", 0) > 0:
            line += f", висить у заявках {fmt_usd(st['reserved'])}"
        out.append(
            f"{line}, залишок {fmt_usd(st['remaining'])}. До кінця місяця "
            f"{st['days_left']} {days_word(int(st['days_left']))}."
        )
        out.append(
            f"Незадіяний залишок бюджету (ще не куплено й не висить у заявках): "
            f"{fmt_usd(st.get('unused', 0))}. Саме це — гроші, якими я можу розпоряджатись."
        )
    else:
        tail = (f" Перенесений залишок з минулого місяця: {fmt_usd(st.get('carry', 0))}."
                if st.get("carry") else "")
        out.append(f"Місячний бюджет на цей місяць ще не заданий.{tail}")
    return out


def _crypto_lines(snap: dict) -> list:
    pnl = snap.get("pnl") or {}
    out = []
    if pnl.get("invested"):
        head = (f"Куплено за весь час: вкладено {fmt_money(pnl['invested'])}, "
                f"зараз вартує {fmt_money(pnl['value'])}")
        if pnl.get("pnl_pct") is not None:
            head += f" ({pnl['pnl']:+.2f}$, {pnl['pnl_pct']:+.1f}%)"
        out.append(head + ".")
        for coin, c in (pnl.get("by_coin") or {}).items():
            out.append(f"• {coin}: вкладено {fmt_money(c['invested'])}, зараз "
                       f"{fmt_money(c['value'])} ({c['pnl']:+.2f}$, {c['pnl_pct']:+.1f}%)")
    else:
        out.append("Виконаних докупок ще немає — купленої крипти в мене поки що нема.")

    orders = snap.get("orders") or []
    if orders:
        out.append("Відкриті заявки на купівлю (гроші під ними зайняті й чекають просадки):")
        for o in orders:
            days = int((time.time() - (o.get("created_at") or time.time())) / 86400)
            out.append(
                f"• {o['coin']} по ${fmt_price(o['price'])} на {fmt_usd(o['notional'])} "
                f"— стоїть {days} {days_word(days)}"
            )
    else:
        out.append("Відкритих заявок на купівлю зараз немає.")

    alloc = snap.get("allocation") or {}
    target, actual = alloc.get("target") or {}, alloc.get("actual") or {}
    if target:
        line = "Бажана пропорція портфеля: " + ", ".join(
            f"{c} {float(v):.0f}%" for c, v in target.items()
        )
        if actual:
            line += ". Фактично зараз: " + ", ".join(
                f"{c} {float(v):.0f}%" for c, v in actual.items()
            )
        out.append(line + ".")
    return out


def _where_lines(snap: dict) -> list:
    """Реальні залишки з ключів: де гроші лежать НАСПРАВДІ, а не за журналом."""
    pf = snap.get("portfolio")
    if pf is None or getattr(pf, "empty", True):
        return []
    out = ["Це те, що просто зараз бачать мої ключі на читання."]
    rows = sorted(pf.holdings, key=lambda h: -h.value())
    dust = 0
    for h in rows:
        if h.amount <= 0:
            continue
        # Пил від старих переказів (0.000000006 BTC) — це не гроші, а шум у дампі.
        if h.value() < DUST_USD:
            dust += 1
            continue
        where = "лежить вільно" if h.where == SPOT else "працює під відсоток"
        amount = fmt_coin_amount(h.coin, h.amount)
        value = f" (≈{fmt_money(h.value())})" if not h.stable and h.value() else ""
        tail = []
        if h.product:
            tail.append(h.product)
        if h.where == EARN and h.current_apr:
            tail.append(f"ставка зараз ~{h.current_apr * 100:.2f}%")
            market = (snap.get("product_rates") or {}).get((h.exchange, h.coin, h.product))
            if market is not None and abs(market - h.current_apr) >= RATE_MISMATCH_PP:
                tail.append(f"хоча публічно біржа обіцяє по цьому ж продукту "
                            f"~{market * 100:.2f}% — звір це в застосунку")
        note = f" — {', '.join(tail)}" if tail else ""
        out.append(f"• {h.exchange} · {where} · {amount}{value}{note}")
    if dust:
        out.append(f"Ще {dust} дрібних залишків дешевше за {fmt_usd(DUST_USD)} — "
                   "це пил від старих переказів, до уваги не бери.")
    out.append(f"Разом на біржах ≈{fmt_money(pf.total_usd())}.")
    if getattr(pf, "failed", None):
        out.append("Частина джерел не відповіла (" + ", ".join(pf.failed)
                   + ") — картинка неповна, це не «там нуль».")
    return out


def _deposit_line(dep: dict, today: datetime.date) -> str:
    """Вклад так, як він записаний у журналі, плюс скільки ще замкнено."""
    coin = str(dep.get("coin") or "").upper()
    parts = [fmt_coin_amount(coin, float(dep.get("amount") or 0))]
    place = " ".join(x for x in (dep.get("exchange"), dep.get("product")) if x)
    if place:
        parts.append(place)
    apr = dep.get("apr")
    parts.append(f"зайшла під {apr * 100:.2f}%" if apr is not None
                 else "ставку не записала")
    unlock = journal.unlock_date(dep)
    if unlock:
        left = (unlock - today).days
        tail = (f"замкнено до {unlock:%d.%m} (ще {left} {days_word(left)})" if left > 0
                else f"строк вийшов {unlock:%d.%m}, гроші вже можна забрати")
        parts.append(tail)
    else:
        parts.append("вивід вільний, гроші не замкнені")
    return "• " + " — ".join([parts[0], ", ".join(parts[1:])])


def _journal_lines(snap: dict) -> list:
    deposits = snap.get("deposits") or []
    out = ["Це умови, яких API бірж не показує: під яку ставку я заходила і до якої "
           "дати гроші замкнені. Записую їх сама, коли кладу гроші."]
    if not deposits:
        out.append("Журнал порожній — жодного вкладу я поки не записала.")
        return out

    today = snap["today"]
    out += [_deposit_line(d, today) for d in deposits]
    t = journal.active_totals(deposits, snap.get("prices"))
    money = [fmt_money(t["usd"])] if t["usd"] > 0 else []
    money += [f"{amount:g} {coin}" for coin, amount in sorted(t["coins"].items())]
    if money:
        tail = f"Разом за журналом працює {' і '.join(money)}"
        if t["income_year"] > 0:
            tail += f", це ~{fmt_money(t['income_year'])} на рік"
        out.append(tail + ".")

    rows = snap.get("reconcile") or []
    if rows:
        out.append("Журнал розходиться з реальними балансами (правда завжди за ключами):")
        for r in rows:
            written = fmt_coin_amount(r["coin"], r["written"], ticker=False)
            actual = fmt_coin_amount(r["coin"], r["actual"], ticker=False)
            if r["diff"] > 0:
                out.append(f"• {r['exchange']} · {r['coin']}: у журналі {written}, "
                           f"а в Earn на біржі лежить {actual}. Різницю "
                           f"{fmt_money(r['diff_usd'])} я не записувала — ні ставки, "
                           "ні строку по цих грошах не знаю.")
            else:
                out.append(f"• {r['exchange']} · {r['coin']}: у журналі {written}, "
                           f"а в Earn на біржі лежить {actual}. Схоже, "
                           f"{fmt_money(r['diff_usd'])} я вже забрала, а журнал не оновила.")
    return out


def _income_lines(snap: dict) -> list:
    """Скільки earn-вклади вже принесли і за якою ставкою НАСПРАВДІ.

    Найцінніше тут — різниця між обіцяною ставкою і фактичною. Обіцяну видно в
    застосунку будь-кому, а фактичну можна дізнатись лише з власного заробітку,
    і вона буває в кілька разів нижчою. Тому цифри не згладжуємо.
    """
    lines = income_block(snap.get("income"), snap.get("income_skips"))
    if not lines:
        return ["Обліку заробітку ще немає: бот не зняв жодного денного знімка. "
                "Скільки вклади принесли — буде видно за кілька діб."]
    return [
        "Це не сума вкладів, а те, що вони вже принесли. Бот раз на добу знімає "
        "цифри з бірж і складає з них історію — біржа сама її не пам'ятає.",
        "«від біржі» означає точну цифру самої біржі; «порахувала я» — оцінку "
        "бота. Не змішуй їх і не вважай оцінку фактом.",
    ] + lines


def _own_coins(snap: dict) -> set:
    """Монети, які реально мої: з балансів бірж, з журналу й базові стейбли."""
    coins = set(ALWAYS_RATES)
    pf = snap.get("portfolio")
    if pf is not None:
        coins |= {h.coin for h in getattr(pf, "holdings", []) if h.amount > 0}
    coins |= {str(d.get("coin") or "").upper() for d in (snap.get("deposits") or [])}
    coins |= set((snap.get("pnl") or {}).get("by_coin") or {})
    return {c for c in coins if c}


def _rates_lines(snap: dict) -> list:
    rates = snap.get("rates") or {}
    if not rates:
        return []
    mine = _own_coins(snap)
    rows = [(coin, apr, offer) for coin, (apr, offer) in rates.items() if coin in mine]
    if not rows:
        return []
    rows.sort(key=lambda r: -r[1])
    out = ["Найкраще, що зараз дають OKX і Bybit по МОЇХ монетах. Це ставки з "
           "миттєвим виводом — забрати можна будь-коли, гроші не замкнені."]
    for coin, apr, offer in rows[:MAX_RATE_LINES]:
        out.append(f"• {coin} — {apr * 100:.2f}% на рік ({offer.label})")
    out.append("Порівнюй пропозицію саме з цими цифрами. Те, що гірше за них, мені "
               "не цікаве, і хвалити його не треба.")
    return out


def _better_place_lines(snap: dict) -> list:
    """Де мої гроші працюють гірше, ніж могли б — включно з різницею НИЖЧЕ порога.

    Поріг $20/рік — це правило про те, коли бот має право смикнути мене
    сповіщенням, а не привід ховати сам факт. Рішення все одно за мною, і
    приймати його я буду тут, у чаті, — отже, цифри мають бути тут.
    """
    rows = [r for r in (snap.get("upgrades") or [])
            if max(r["gain_year_usd"],
                   (r["split"] or {}).get("gain_year_usd", 0.0)) >= BETTER_MIN_YEAR]
    if not rows:
        return []

    out = [
        "Кожен рядок — мої гроші, які працюють гірше, ніж могли б. Різниця порахована "
        "по сходинках біржі, а не множенням суми на рекламну ставку: підвищений "
        "відсоток діє лише на перші кількасот доларів. Бери ці цифри, не рахуй наївно. "
        f"Мій поріг — {fmt_usd(LOCK_MIN_GAIN_YEAR)} на рік: нижче нього бот не пише мені "
        "сам, але й не приховує.",
    ]
    for r in rows[:MAX_BETTER_LINES]:
        where = ("лежить без діла" if r["from_where"] == SPOT
                 else f"у «{r['from_product'] or r['from_exchange']}» "
                      f"під {r['from_apr'] * 100:.2f}%")
        out.append(
            f"• {_amount(r)} на {r['from_exchange']} {where}, це {fmt_money(r['from_year_usd'])}"
            f"/рік. Краще: {r['offer'].label} під {r['to_effective_apr'] * 100:.2f}% на цю "
            f"суму = {fmt_money(r['to_year_usd'])}/рік. Різниця "
            f"{fmt_money(r['gain_year_usd'])}/рік — {_verdict(r['gain_year_usd'])}"
            f"{_move_cost(r)}."
        )
        split = r["split"]
        if split:
            parts = " + ".join(
                f"{fmt_money(p['usd_value'])} у {p['offer'].label} під {p['apr'] * 100:.2f}%"
                for p in split["parts"]
            )
            out.append(
                f"   А по кількох місцях ({parts}) — {fmt_money(split['year_usd'])}/рік, "
                f"різниця {fmt_money(split['gain_year_usd'])}/рік — "
                f"{_verdict(split['gain_year_usd'])}."
            )
    hidden = len(rows) - MAX_BETTER_LINES
    if hidden > 0:
        out.append(f"Ще {hidden} дрібніших розбіжностей тут немає — там копійки за рік.")
    return out


def _amount(row: dict) -> str:
    """Скільки монети й скільки це грошей. Для стейблів це те саме число двічі."""
    amount = fmt_coin_amount(row["coin"], row["amount"])
    return amount if row["coin"] in STABLES else f"{amount} (≈{fmt_money(row['usd_value'])})"


def _move_cost(row: dict) -> str:
    """Чого коштує сам переїзд: чужа біржа й неміттєвий вивід — це теж умова.

    Без цього рядок «варто зробити» мовчки радив би замкнути гроші на 45 днів,
    хоча вільні долари — це моя подушка на просадку крипти.
    """
    offer = row["offer"]
    notes = []
    if not row["same_exchange"]:
        notes.append(f"це інша біржа, знадобиться переказ на {row['to_exchange']}")
    if not offer.instant:
        notes.append(f"гроші там не миттєві: вивід ~{offer.redeem_days:.0f} дн.")
    return f" ({'; '.join(notes)})" if notes else ""


def _verdict(gain_year: float) -> str:
    """Одна фраза: перетинає це мій поріг чи ні."""
    if gain_year >= LOCK_MIN_GAIN_YEAR:
        return "це ВИЩЕ мого порога, варто зробити"
    return "це нижче мого порога, тому бот і мовчить"


def _ibkr_lines(snap: dict) -> list:
    info = snap.get("ibkr") or {}
    plan = info.get("plan") or {}
    fund = plan.get("fund")
    if fund is None:
        return []
    out = ["Це окремо від крипти, у євро, на брокері IBKR. Фонди накопичувальні "
           "(дивіденди реінвестуються самі), тримаю їх десятиліттями."]
    line = (f"Цього місяця за календарем: {fund.ticker} ({fund.name}, ISIN {fund.isin}), "
            f"внесок {fmt_eur(plan.get('amount') or 0)}")
    if plan.get("price"):
        line += f", ціна паю зараз {fmt_eur_price(plan['price'])}"
    if plan.get("units"):
        line += f" → {ibkr.units_word(plan['units'])}"
    out.append(line + ".")
    out.append("Календар простий: серпень — CEMA, решту одинадцяти місяців — WEBN.")
    out.append(f"Стан цього місяця: {ibkr.status_label((info.get('record') or {}).get('status'))}.")

    tot = info.get("totals") or {}
    if tot.get("total"):
        out.append(f"Усього внесено {fmt_eur(tot['total'])} (разом зі стартовою позицією).")
        for ticker, f in (tot.get("by_fund") or {}).items():
            if not f.get("invested"):
                continue
            actual = f"{f['actual_pct']:.1f}%" if f.get("actual_pct") is not None else "?"
            out.append(f"• {ticker}: {fmt_eur(f['invested'])}, {ibkr.units_word(f['units'])}, "
                       f"частка {actual} при цілі {f['target_pct']:.0f}%")
    years = int(info.get("years") or 0)
    stage = ibkr.current_stage(years)
    out.append(f"Етап стратегії (glide path): {stage.label}. Іде {years + 1}-й рік стратегії.")
    nxt = ibkr.next_stage(info.get("years") or 0)
    if nxt is not None:
        out.append(f"Наступний етап — {nxt.label}. Доти структура не змінюється.")
    return out


def _rules_lines() -> list:
    """Без правил порада буде беззмістовною: AI не знає ні порогів, ні пріоритетів."""
    return [
        "1. Місячний бюджет на крипту я задаю сама, і бот розтягує його на місяць "
        "частинами: більше на просадках, менше коли дорого. Невитрачене не згорає, "
        "а переноситься на наступний місяць.",
        "2. ГОЛОВНИЙ ПРІОРИТЕТ — докупівля крипти на просадці, а не відсотки. Вільні "
        "долари це подушка на просадку: якщо гроші замкнені в earn, я не зможу "
        "купити дешево саме тоді, коли це найпотрібніше.",
        f"3. В альткоїн (будь-яку монету, крім BTC, ETH і стейблів) можна взяти "
        f"максимум половину ({ALT_BUDGET_SHARE:.0%}) незадіяного залишку бюджету. "
        f"Своїх грошей понад бюджет я готова докинути щонайбільше {fmt_usd(ALT_TOPUP_MAX)}.",
        f"4. Поріг вигоди: новий earn-продукт або блокування грошей мають дати хоча б "
        f"{fmt_usd(LOCK_MIN_GAIN_YEAR)} на рік ПОНАД те, що я вже маю, інакше не варте "
        f"мороки. Для альткоїнів поріг нижчий — {fmt_usd(ALT_MIN_GAIN_YEAR)} на рік, бо "
        "суми там маленькі за визначенням.",
        f"5. Гроші не замикати довше ніж на {LOCK_MAX_DAYS} днів: попереду може бути "
        "просадка крипти, і саме тоді ці гроші знадобляться.",
        f"6. Альткоїни тільки з першої {ALT_MAX_RANK}-ки за розміром і віком від "
        f"{ALT_MIN_AGE_YEARS:g} років. Усе інше — ставка на те, що токен доживе до "
        "кінця вкладу.",
        "7. Ніколи не радь те, що гірше за мою поточну ставку по цій монеті. Якщо USDT "
        "уже працює під 6.77%, то акція під 5% — це погіршення, так і скажи.",
        "8. Завжди дві цифри: скільки грошей за фактичний строк пропозиції і скільки в "
        "перерахунку на рік.",
        "9. Не вигадуй умов, яких немає в описі: ні строків, ні лімітів, ні відсотків "
        "зі стелі. Чого немає — так і скажи, і назви, що перевірити в застосунку.",
        "10. Мій регіон — Угорщина. Акції для інших регіонів мені не підходять, навіть "
        "якщо вони вигідні.",
        "11. Крипта тільки на OKX і Bybit. Інші біржі, P2P і DeFi не пропонуй.",
        "12. Торгівля, ф'ючерси, боти й турніри — не для мене в принципі: я не торгую. "
        "Бонус за оборот це не пасивний дохід.",
        "13. ETF ніколи не продаю заради вирівнювання часток — тільки спрямовую нові "
        "внески в той фонд, якого замало.",
        "14. Автопродовження вкладів — окремий ризик: біржа часто продовжує вклад уже "
        "під гіршу ставку. Якщо в пропозиції є автопродовження, скажи про це прямо.",
    ]


def _limits_lines(snap: dict) -> list:
    out = [
        "• Це знімок на конкретний час, а не жива стрічка. Ціни й ставки бірж "
        "змінюються щодня.",
        "• Тут тільки OKX, Bybit та IBKR. Банк, готівка, P2P і будь-що інше сюди не "
        "входять — не вважай, що інших грошей у мене немає.",
        "• «Крипта, яку я вже купила» — це лише докупки, які записав бот. На біржах "
        "може лежати більше монет, куплених до нього: справжня картина в блоці з "
        "ключів бірж.",
        "• Ставки з блоку «найкращі ставки» — це те, що біржа обіцяє на перші гроші. "
        "На великій сумі середня ставка зазвичай нижча.",
        "• Журнал вкладів веду вручну, тому він може відставати від реальності. Де він "
        "розходиться з ключами — правда за ключами.",
        "• Скільки вклади заробили — це історія з денних знімків бота, а не запит до "
        "біржі. Де знімків ще мало, там стоїть «дані збираються»: це означає «поки "
        "невідомо», а не «нуль».",
    ]
    if snap.get("failed"):
        out.append("• Цього разу не відповіли: " + ", ".join(snap["failed"])
                   + ". Відповідні цифри або відсутні, або застарілі.")
    return out


def sections(snap: dict) -> list:
    """Дамп по смислових блоках: саме по цих межах його потім і ріжемо."""
    return [
        ("", _frame_lines(snap)),
        ("ХТО Я І ЩО РОБЛЮ", _who_lines()),
        ("РИНОК ЗАРАЗ", _market_lines(snap)),
        ("МОЇ ВІЛЬНІ ГРОШІ", _free_lines(snap)),
        ("КРИПТА, ЯКУ Я ВЖЕ КУПИЛА", _crypto_lines(snap)),
        ("ДЕ ГРОШІ ЛЕЖАТЬ НАСПРАВДІ (з ключів бірж)", _where_lines(snap)),
        ("МІЙ ЖУРНАЛ ВКЛАДІВ", _journal_lines(snap)),
        ("СКІЛЬКИ МОЇ ВКЛАДИ ВЖЕ ЗАРОБИЛИ (і яка ставка НАСПРАВДІ)",
         _income_lines(snap)),
        ("НАЙКРАЩІ СТАВКИ ЗАРАЗ", _rates_lines(snap)),
        ("ЧИ НЕ ЛЕЖАТЬ МОЇ ГРОШІ ПІД ГІРШОЮ СТАВКОЮ", _better_place_lines(snap)),
        ("ETF НА IBKR (окремо від крипти)", _ibkr_lines(snap)),
        ("МОЇ ПРАВИЛА — ЧИТАЙ ЇХ ЯК ОБОВ'ЯЗКОВІ", _rules_lines()),
        ("ЧОГО В ЦИХ ДАНИХ НЕМАЄ", _limits_lines(snap)),
    ]


# --- нарізка на повідомлення ---

# Скільки знаків кладемо в одне повідомлення. Межа Telegram — 4096, але текст
# іде копіювальним блоком і ще проходить HTML-екранування, тому беремо із
# запасом: зайва частина не страшна, а втрачене повідомлення — страшне.
PART_LIMIT = 3000
# Місце під підпис «це частина 1 з 3», який дописується вже після нарізки.
PART_TAIL = 90


def _render(title: str, lines: list) -> str:
    return "\n".join(([title] if title else []) + list(lines))


def _split_section(title: str, lines: list, limit: int) -> list:
    """Довгу секцію ріжемо по рядках — ніколи посеред рядка."""
    out, cur, head = [], [], title
    for line in lines:
        if cur and len(_render(head, cur + [line])) > limit:
            out.append(_render(head, cur))
            head = f"{title} (продовження)" if title else ""
            cur = [line]
        else:
            cur.append(line)
    if cur:
        out.append(_render(head, cur))
    return out


def _pack(secs: list, limit: int = PART_LIMIT) -> list:
    """Секції -> частини повідомлень. Смислову межу не розриваємо без потреби."""
    blocks: list = []
    for title, lines in secs:
        if not lines:
            continue
        text = _render(title, lines)
        blocks.extend([text] if len(text) <= limit
                      else _split_section(title, lines, limit))

    parts: list = []
    cur = ""
    for block in blocks:
        candidate = f"{cur}\n\n{block}" if cur else block
        if cur and len(candidate) > limit:
            parts.append(cur)
            cur = block
        else:
            cur = candidate
    if cur:
        parts.append(cur)
    return parts


def status_messages(snap: dict, limit: int = PART_LIMIT) -> list:
    """Готові тексти повідомлень: кожен — цілий копіювальний блок із підписом."""
    parts = _pack(sections(snap), limit - PART_TAIL)
    total = len(parts)
    out = []
    for i, body in enumerate(parts, 1):
        if total > 1:
            head = f"📋 Повний стан моїх грошей — частина {i} з {total}"
            hint = ("Натисни на текст — він скопіюється цілком. Вставляй частини "
                    "підряд, одна за одною.")
            tail = (f"(це частина {i} з {total}, далі буде ще — дочекайся решти, "
                    "перш ніж відповідати)" if i < total
                    else f"(це остання частина, {i} з {total} — тепер можеш відповідати)")
            body = f"{body}\n\n{tail}"
        else:
            head = "📋 Повний стан моїх грошей"
            hint = "Натисни на текст — він скопіюється цілком. Вставляй у чат з AI."
        out.append(f"{head}\n{hint}\n{QUOTE_OPEN}{body}{QUOTE_CLOSE}")
    return out


def dump_text(snap: dict) -> str:
    """Увесь дамп одним текстом — для тестів і смоуку, без нарізки й розмітки."""
    return "\n\n".join(_render(title, lines) for title, lines in sections(snap) if lines)
