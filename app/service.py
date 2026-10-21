"""Високорівневі операції, спільні для команд бота і планувальника."""
import datetime
import logging
import re
from typing import Optional, Tuple

from app import budget, learning
from app.advisor import decide, explain_coins, explain_promos, parse_deposits
from app.config import config
from app.earn import (
    LIMIT_WARN,
    TRADING_BONUS_WORTH_USD,
    TRADING_RISK_WARN,
    fallback_explain,
    is_trading_promo,
)
from app.journal import clean_ai_deposit, parse_text
from app.engine import Recommendation, analyze, build_from_ai, coin_snapshot
from app.formatting import (
    UNCHECKED_COIN_NOTE,
    UNKNOWN_COIN_NOTE,
    days_word,
    fmt_money,
    fmt_usd,
)

log = logging.getLogger(__name__)


async def gather(services) -> Tuple[dict, int]:
    data = {}
    for coin in config.symbols:
        data[coin] = await services.market.fetch_coin(coin)
    fng = await services.market.fetch_fng()
    return data, fng


def _price_of(data: dict, coin: str):
    d = data.get(coin) or data.get(coin.upper())
    return d.get("price") if d else None


def target_allocation(storage) -> dict:
    """Цільова пропорція з налаштувань (дефолт із config.base_allocation, за порядком symbols)."""
    default = {c: a for c, a in zip(config.symbols, config.base_allocation)}
    return storage.get_allocation(default)


def actual_allocation(storage, data: dict) -> dict:
    """Фактична пропорція портфеля у % за поточною вартістю filled-позицій (qty * ціна)."""
    pos = storage.filled_positions()
    values = {}
    for coin, p in pos.items():
        price = _price_of(data, coin)
        qty = p.get("qty") or 0.0
        if price and qty:
            values[coin] = price * qty
    total = sum(values.values())
    if total <= 0:
        return {}
    return {coin: round(v / total * 100.0, 1) for coin, v in values.items()}


def allocation_context(storage, data: dict) -> dict:
    """Контекст для AI: цільова + фактична пропорція портфеля."""
    return {
        "target": target_allocation(storage),
        "actual": actual_allocation(storage, data),
    }


def portfolio_pnl(storage, data: dict) -> dict:
    """Реальний прибуток за filled-позиціями і поточними цінами.

    Повертає {invested, value, pnl, pnl_pct, by_coin:{COIN:{pnl_pct,...}}}.
    pnl_pct=None, якщо ще нема куплених монет / цін.
    """
    pos = storage.filled_positions()
    invested_tot = value_tot = 0.0
    by_coin = {}
    for coin, p in pos.items():
        price = _price_of(data, coin)
        invested = p.get("invested") or 0.0
        qty = p.get("qty") or 0.0
        if not (price and qty and invested):
            continue
        value = price * qty
        invested_tot += invested
        value_tot += value
        by_coin[coin] = {
            "invested": round(invested, 2),
            "value": round(value, 2),
            "pnl": round(value - invested, 2),
            "pnl_pct": round((value / invested - 1.0) * 100.0, 1),
        }
    pnl_pct = round((value_tot / invested_tot - 1.0) * 100.0, 1) if invested_tot > 0 else None
    return {
        "invested": round(invested_tot, 2),
        "value": round(value_tot, 2),
        "pnl": round(value_tot - invested_tot, 2),
        "pnl_pct": pnl_pct,
        "by_coin": by_coin,
    }


async def analyze_market(data: dict, fng: int, amount: float,
                         position: Optional[dict] = None,
                         budget_ctx: Optional[dict] = None,
                         learning_hint: Optional[str] = None,
                         depth_factor: float = 1.0,
                         allocation: Optional[dict] = None) -> Recommendation:
    """Рішення приймає AI-трейдер; якщо він недоступний — детермінована формула."""
    snapshot = coin_snapshot(data, fng)
    decision = await decide(snapshot, fng, amount, position, budget_ctx, learning_hint, allocation)
    open_by_coin = (position or {}).get("open_by_coin") if position else None
    if decision:
        try:
            return build_from_ai(data, fng, amount, decision, open_by_coin, depth_factor)
        except Exception:  # noqa: BLE001
            log.exception("build_from_ai failed — fallback на формулу")
    # Формулі віддаємо ті самі відкриті ордери: правила безпеки мають бути однакові
    # в обох режимах, інакше запасна гілка ставить заявки поверх уже виставлених.
    return analyze(data, fng, amount, open_by_coin)


async def recommend(services, amount: Optional[float] = None) -> Tuple[Recommendation, int]:
    data, fng = await gather(services)
    bstatus = budget.status(services.storage)

    if amount:
        # Явно задана сума — поважаємо вибір користувача (ручний режим).
        amt = amount
    elif bstatus["active"]:
        # Суму не задано, але є місячний бюджет — беремо розумну порцію (темп + просадка).
        amt = budget.suggested_tranche(bstatus, budget.is_dip(data, fng))
    else:
        amt = services.storage.get_base(config.base_amount)

    position = services.storage.position_summary()
    learning_hint = learning.ai_hint(services.storage)
    depth = learning.depth_factor(services.storage)
    allocation = allocation_context(services.storage, data)
    rec = await analyze_market(
        data, fng, amt, position,
        bstatus if bstatus["active"] else None,
        learning_hint, depth, allocation,
    )
    rec_id = services.storage.save_recommendation(rec)
    return rec, rec_id


# --- акції бірж: один шлях і для /earn, і для сповіщень ---

VERDICTS = ("worth", "maybe", "skip")

# «up to 10%» — це стеля для реклами, а не ставка на її суму.
_HEDGE_RE = re.compile(r"\bup to\b|\bдо\s+\d", re.IGNORECASE)
# Будь-який відсоток у заголовку акції — теж приманка на перший шматок грошей.
_PCT_RE = re.compile(r"\d+(?:[.,]\d+)?\s*%")


def _num(value) -> float:
    """Число з відповіді порадника; 0.0 на null, порожнє чи сміття."""
    try:
        return float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _plain_rates(rates: Optional[dict]) -> dict:
    """{монета: (ставка, пропозиція)} або {монета: ставка} -> {монета: ставка}."""
    out = {}
    for coin, value in (rates or {}).items():
        apr = value[0] if isinstance(value, (tuple, list)) else value
        if apr:
            out[coin] = float(apr)
    return out


def _trading_skip_why(bonus: float) -> str:
    """Чому торгова акція не варта — обовʼязково з сумою, щоб вирішувала вона, а не бот.

    Умови тут не переказуємо: рядком вище вони вже названі, і повтор лише
    роздуває повідомлення.
    """
    if bonus <= 0:
        return ("Розмір бонусу в оголошенні не вказаний, а торгувати заради невідомого "
                "не варто.")
    return f"${bonus:g} не покриють ризик, а гроші будуть зайняті."


# Довше за це акція вже схожа на звичайний вклад, і річна цифра чесна сама по собі.
SHORT_PROMO_DAYS = 90


def promo_days(ex: dict, today: Optional[datetime.date] = None) -> Optional[int]:
    """Скільки днів акція реально триває: строк блокування або час до дедлайну."""
    locked = int(_num(ex.get("locked_days")))
    if locked > 0:
        return locked
    try:
        end = datetime.date.fromisoformat(str(ex.get("period_end"))[:10])
    except (TypeError, ValueError):
        return None
    days = (end - (today or datetime.date.today())).days
    return days if days > 0 else None


def _gain_line(ex: dict, amounts: dict, rates: dict,
               today: Optional[datetime.date] = None) -> str:
    """Вигода і в грошах, і у відсотках — на ЇЇ сумі, а не на абстрактній.

    Рахує код, а не порадник: модель регулярно помиляється в арифметиці, а саме
    ця цифра й вирішує, чи піде вона щось робити.

    На короткій акції ЗАВЖДИ дві цифри: скільки це насправді за її кілька днів
    і скільки та сама ставка дала б за рік. Одна річна цифра на триденній акції —
    це обман: «10% річних» звучить як $30, а на руки виходить 25 центів.
    """
    coin = str(ex.get("coin") or "").upper()
    apr_pct = _num(ex.get("apr_pct"))
    if not coin or apr_pct <= 0:
        return ""
    days = promo_days(ex, today)
    short = bool(days and days <= SHORT_PROMO_DAYS)

    held = _num((amounts or {}).get(coin))
    now_apr = _num((rates or {}).get(coin))
    if held <= 0 or now_apr <= 0:
        # Її грошей у цій монеті немає — назвати суму нема від чого, але
        # співвідношення «за період проти за рік» усе одно чесно показуємо.
        if not short:
            return ""
        return (f"Ставка {apr_pct:g}% — це «на рік». Акція триває {days} {days_word(days)}, "
                f"тож за весь її строк вийде близько {apr_pct * days / 365.0:.2f}% від суми.")

    if apr_pct / 100.0 <= now_apr:
        return ""    # акція не краща за те, що вона вже має — хвалитись нема чим
    year_here = held * apr_pct / 100.0
    head = f"Твій {coin} зараз під {now_apr * 100:.2f}% → тут {apr_pct:g}%"
    if short:
        return (f"{head}. За {days} {days_word(days)}, поки триває акція, це "
                f"~{fmt_money(year_here * days / 365.0)} на твої {fmt_usd(held)}; "
                f"у перерахунку на рік — ~{fmt_money(year_here)}/рік, "
                "але стільки вона не триває.")
    return (f"{head}: ~{fmt_money(year_here)}/рік замість "
            f"{fmt_money(held * now_apr)} на твої {fmt_usd(held)}.")


def _guard(promo, ex: dict, amounts: dict, rates: dict) -> dict:
    """Межі, які порадник не має права перетнути.

    Ключ Gemini ненадійний і модель час від часу верзе зайве, а ціна помилки тут —
    обіцянка «10% на весь твій BTC». Тому найдорожчі помилки ловить код, а не промпт.
    """
    ex = dict(ex)
    verdict = ex.get("verdict") if ex.get("verdict") in VERDICTS else "maybe"
    bonus = _num(ex.get("bonus_usd"))

    if is_trading_promo(promo.title):
        ex["kind"] = "trading"
        if bonus >= TRADING_BONUS_WORTH_USD:
            # Її власне мірило: за такі гроші вона хоче хоча б подумати сама.
            verdict = "maybe"
            if "втрат" not in (ex.get("warn") or "").lower():
                ex["warn"] = ((ex.get("warn") or "").strip() + " " + TRADING_RISK_WARN).strip()
        else:
            # Порадник інколи бачить у турнірі «легкий бонус» — за $5 це не так.
            if verdict != "skip" or not ex.get("why"):
                ex["why"] = _trading_skip_why(bonus)
            verdict = "skip"
    else:
        ex["gain_line"] = _gain_line(ex, amounts, rates)
        known_limit = _num(ex.get("max_amount")) > 0
        hedged = bool(_HEDGE_RE.search(promo.title))
        # Ліміт названий у тексті — він і буде показаний, зайве «перевір ліміт» лише шумить.
        if not known_limit and (hedged or _PCT_RE.search(promo.title)):
            if hedged and verdict == "worth":
                verdict = "maybe"
            if "ліміт" not in (ex.get("warn") or "").lower():
                ex["warn"] = ((ex.get("warn") or "").strip() + " " + LIMIT_WARN).strip()

    ex["verdict"] = verdict
    if verdict == "skip":
        ex["action"] = ""
    return ex


async def promo_advice(promos: list, amounts: Optional[dict] = None,
                       free_usd: float = 0.0, rates: Optional[dict] = None,
                       bodies: Optional[dict] = None) -> dict:
    """Акції -> {заголовок: пояснення з вердиктом}. Правило знизу, AI зверху, межі поверх усього.

    amounts/free_usd/rates — контекст її грошей: без нього вердикт був би про
    когось абстрактного, а «10% річних» звучало б однаково для BTC під 0.5%
    і для USDT під 6.8%. bodies — тексти сторінок оголошень за ключем акції:
    розмір бонусу, ліміти й дати є тільки там.
    """
    if not promos:
        return {}
    explains = fallback_explain(promos)
    amounts = amounts or {}
    plain_rates = _plain_rates(rates)
    context = {
        "amounts": amounts,
        "free_usd": free_usd,
        "rates": plain_rates,
        "today": datetime.date.today().isoformat(),
    }
    by_title = {p.title: (bodies or {}).get(p.key, "") for p in promos}
    try:
        explains.update(await explain_promos([p.title for p in promos], context, by_title))
    except Exception:  # noqa: BLE001
        log.warning("Пояснення акцій недоступні — лишаємось на правилі", exc_info=True)
    return {p.title: _guard(p, explains.get(p.title) or {}, amounts, plain_rates)
            for p in promos}


# --- розбір вкладу, записаного вільним текстом ---

async def parse_deposits_text(text: str,
                              today: Optional[datetime.date] = None) -> Tuple[list, bool]:
    """Вільний текст -> вклади. Другим значенням — чи розбирав саме порадник.

    AI читає живу мову значно краще за регулярку, але ключ Gemini регулярно
    недоступний, а записати вклад вона має змогти будь-коли. Тому формула — не
    аварійний режим, а рівноцінний другий шлях; про те, який спрацював, бот
    каже чесно.
    """
    today = today or datetime.date.today()
    items = None
    try:
        items = await parse_deposits(text, today.isoformat())
    except Exception:  # noqa: BLE001
        log.warning("Розбір вкладу порадником недоступний", exc_info=True)
    if items:
        deposits = [d for d in (clean_ai_deposit(i, today) for i in items) if d]
        if deposits:
            return deposits, True
    return parse_text(text, today), False


# --- наскільки надійна сама монета (для акцій і продуктів на незнайомих монетах) ---

# Межі за місцем у рейтингу CoinGecko. Числа умовні, але потрібні саме як
# запасний варіант: коли порадник мовчить, краще груба оцінка, ніж мовчання.
TRUST_SOLID_RANK = 20
TRUST_OK_RANK = 100
TRUST_MIN_AGE_YEARS = 2.0


def _rule_trust(facts: dict) -> str:
    rank = facts.get("rank")
    age = facts.get("age_years")
    if age is None:
        age = facts.get("age_years_at_least")
    if age is not None and age < TRUST_MIN_AGE_YEARS:
        return "shaky"
    if rank and rank <= TRUST_SOLID_RANK:
        return "solid"
    if rank and rank <= TRUST_OK_RANK:
        return "ok"
    return "shaky"


def _rule_summary(facts: dict) -> str:
    """Сухий переказ цифр, коли порадник недоступний."""
    parts = []
    age, at_least = facts.get("age_years"), facts.get("age_years_at_least")
    if age:
        parts.append(f"на ринку {age:g} р.")
    elif at_least:
        parts.append(f"на ринку щонайменше {at_least:g} р.")
    rank = facts.get("rank")
    if rank:
        parts.append(f"№{rank} за розміром")
    vol = facts.get("volume_usd")
    if vol:
        parts.append(f"оборот {fmt_usd(vol)} за добу")
    return (", ".join(parts) + ".").capitalize() if parts else "Даних про монету обмаль."


async def coin_reliability(facts_reader, coins) -> dict:
    """{тікер: {summary, trust, by_rule}} — наскільки надійна сама монета.

    Для незнайомої монети це важливіше за ставку: 25% річних на $100 — це $25,
    а падіння монети на 40% — це мінус $40, і бот має казати це прямо.
    """
    coins = [c for c in dict.fromkeys(coins) if c]
    if not coins or facts_reader is None:
        return {}
    try:
        profiles = await facts_reader.profiles(coins)
    except Exception:  # noqa: BLE001
        log.warning("Довідка про монети недоступна", exc_info=True)
        return {}
    if not profiles:
        return {}

    # Монети, які надійно визначити не вдалось, пораднику не показуємо взагалі:
    # він охоче напише переконливе речення про цифри, яких у нас немає.
    unclear = {c: f for c, f in profiles.items() if f.get("ambiguous")}
    known = {c: f for c, f in profiles.items() if not f.get("ambiguous")}

    ai = {}
    if known:
        try:
            ai = await explain_coins(known)
        except Exception:  # noqa: BLE001
            log.warning("Оцінка монет порадником недоступна — лишаємось на цифрах", exc_info=True)

    out = {}
    for coin, facts in unclear.items():
        out[coin] = {
            "facts": facts,
            # Кілька однойменних монет — це одне, а недоступний довідник — інше.
            "summary": UNKNOWN_COIN_NOTE if facts.get("matches") else UNCHECKED_COIN_NOTE,
            "trust": "unknown",
            "by_rule": False,     # це не «формула замість AI», а свідома відмова
            "ambiguous": True,
        }
    for coin, facts in known.items():
        said = ai.get(coin) or {}
        out[coin] = {
            "facts": facts,
            "summary": said.get("summary") or _rule_summary(facts),
            "trust": said.get("trust") if said.get("trust") in ("solid", "ok", "shaky")
                     else _rule_trust(facts),
            "by_rule": not said,
            "ambiguous": False,
        }
    return out
