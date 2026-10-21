"""Журнал earn-вкладів: те, чого біржа не віддає через API.

Ключі показують, СКІЛЬКИ грошей лежить, але не показують, НА ЯКИХ УМОВАХ:
що вклад фіксований до 20 вересня, що зайшла вона під 6.77%, що вивід іде
45 днів. Це знає лише вона — і саме це тут і записується.

Головне джерело правди ЗАВЖДИ лишається за ключами (app/portfolio.py). Журнал
нічого не замінює: він лише додає строки й вхідні ставки й чесно каже, коли
розходиться з тим, що реально лежить на біржі.

Область — тільки earn-вклади на OKX і Bybit. Ні P2P, ні інші місця сюди не йдуть.
"""
from __future__ import annotations

import datetime
import re
from typing import Optional

from app.earn import MAJORS, PRODUCTS, STABLES, CORE
from app.portfolio import EARN
from app.storage import KIND_FIXED, KIND_FLEX

# Біржі, які взагалі можуть бути у журналі.
EXCHANGES = {"okx": "OKX", "bybit": "Bybit"}

# Назви продуктів так, як вона їх пише, -> як вони підписані в застосунку.
# Порядок важливий: «on-chain earn» має впізнатись раніше за просто «earn».
PRODUCT_ALIASES = (
    ("on-chain earn", "On-Chain Earn"),
    ("on chain earn", "On-Chain Earn"),
    ("onchain earn", "On-Chain Earn"),
    ("on-chain", "On-Chain Earn"),
    ("on chain", "On-Chain Earn"),
    ("onchain", "On-Chain Earn"),
    ("simple earn", "Simple Earn"),
    ("simple", "Simple Earn"),
    ("savings", "Savings"),
    ("saving", "Savings"),
    ("флекс", "Savings"),
    ("flexible", "Savings"),
    ("eth staking", "ETH Staking"),
    ("staking", "Staking"),
    ("стейкінг", "Staking"),
    ("earn", "Earn"),
)

# Монети, які точно впізнаємо як монети, а не як частину назви продукту.
KNOWN_COINS = STABLES | CORE | MAJORS

# Слова, які стоять після числа, але монетою не є («на 30 днів», «під 6 відсотків»).
# Долари теж сюди: «100 usd» — це сума, а не окрема монета журналу.
NOT_A_COIN = {
    "days", "day", "usd", "год", "years",
    "доби", "добу", "доба", "доларів", "долар", "долари",
}
# Саме префіксами, бо відмінків забагато: «днів», «дня», «місяців», «тижнем».
# Строго окремим списком від точних збігів — інакше «usdt» відпадав як «usd».
NOT_A_COIN_PREFIX = ("дн", "день", "відсот", "процент", "місяц", "тижн", "рок", "рік")

# Скільки монети має «зійтись» із біржею, щоб не чіплятись до пилу округлення.
RECONCILE_MIN_USD = 20.0
# І скільки відсотків розбіжності вже варті окремої розмови.
RECONCILE_MIN_SHARE = 0.05

# Наскільки має впасти ставка, щоб це вже була новина, а не денний шум бірж.
RATE_DROP_PP = 0.01

_NUM = r"\d+(?:[.,]\d+)?"
_AMOUNT_RE = re.compile(rf"({_NUM})\s*([a-zа-яіїєґ]{{2,6}})\b", re.IGNORECASE)
_PCT_RE = re.compile(rf"({_NUM})\s*%")
_TERM_RE = re.compile(rf"на\s+(\d+)\s*(?:дн|день|дні|днів|дoб|доб|day|days)", re.IGNORECASE)
_TERM_TAIL_RE = re.compile(r"(\d+)\s*(?:дн|день|дні|днів|day|days)", re.IGNORECASE)
_DATE_RE = re.compile(r"до\s+(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?", re.IGNORECASE)
_ISO_RE = re.compile(r"до\s+(\d{4})-(\d{2})-(\d{2})", re.IGNORECASE)
_FLEX_WORDS = ("гнучк", "вільн", "будь-коли", "flexible", "флекс")
_FIXED_WORDS = ("фіксов", "замкн", "заблок", "fixed", "locked")


def _f(raw) -> Optional[float]:
    try:
        return float(str(raw).replace(",", "."))
    except (TypeError, ValueError):
        return None


def normalize_product(raw: str, exchange: str = "", kind: str = "") -> str:
    """Як вона написала -> як продукт зветься в застосунку біржі."""
    text = (raw or "").lower()
    for alias, name in PRODUCT_ALIASES:
        if alias in text:
            # «Earn» саме по собі нічого не каже — доводимо до назви розділу біржі.
            if name == "Earn" and exchange:
                return PRODUCTS.get((exchange, "flex"), ("Earn", ""))[0]
            return name
    if exchange:
        key = (exchange, "staking" if kind == KIND_FIXED else "flex")
        if key in PRODUCTS:
            return PRODUCTS[key][0]
    return (raw or "").strip()


def section_of(exchange: str, product: str) -> str:
    """Де шукати продукт у застосунку — той самий довідник, що й у /earn."""
    for (ex, _kind), (name, section) in PRODUCTS.items():
        if ex == exchange and name == product:
            return section
    return ""


def _parse_date(line: str, today: datetime.date) -> tuple:
    """Дата розморозки з тексту -> (дата, місце в рядку).

    Місце потрібне не менше за саму дату: без нього «до 20.10» потім
    зчитувалось як ставка 20.1%.
    """
    m = _ISO_RE.search(line)
    if m:
        try:
            return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3))), m.span()
        except ValueError:
            return None, None
    m = _DATE_RE.search(line)
    if not m:
        return None, None
    day, month, year = int(m.group(1)), int(m.group(2)), m.group(3)
    if year:
        year = int(year)
        year += 2000 if year < 100 else 0
    else:
        # Без року: найближче майбутнє. «до 20.01», написане в грудні, — це вже наступний рік.
        year = today.year
    try:
        date = datetime.date(year, month, day)
    except ValueError:
        return None, None
    if not m.group(3) and date < today:
        try:
            date = datetime.date(year + 1, month, day)
        except ValueError:
            return None, None
    return date, m.span()


def parse_line(line: str, today: Optional[datetime.date] = None) -> Optional[dict]:
    """Один рядок вільним текстом -> вклад. None, якщо суми з монетою там немає.

    Запасний розбір без AI: ключ Gemini регулярно недоступний, а записати вклад
    вона має змогти будь-коли. Формула бере те, що видно однозначно, а чого не
    видно — лишає порожнім, щоб це було видно в підтвердженні.
    """
    raw = (line or "").strip()
    if not raw:
        return None
    today = today or datetime.date.today()
    low = raw.lower()

    amount = coin = None
    used: list = []       # шматки тексту, які вже пішли в суму — щоб не сплутати зі ставкою
    for m in _AMOUNT_RE.finditer(low):
        word = m.group(2)
        if word in NOT_A_COIN or word.startswith(NOT_A_COIN_PREFIX):
            continue
        if word.upper() not in KNOWN_COINS and not re.fullmatch(r"[a-z]{2,6}", word):
            continue
        amount, coin = _f(m.group(1)), word.upper()
        used.append(m.span())
        break
    if amount is None or not amount or not coin:
        return None

    exchange = ""
    for key, name in EXCHANGES.items():
        if re.search(rf"\b{key}\b", low):
            exchange = name
            break

    term_days = None
    m = _TERM_RE.search(low) or _TERM_TAIL_RE.search(low)
    if m:
        term_days = int(m.group(1))
        used.append(m.span())
    unlock, date_span = _parse_date(low, today)
    if date_span:
        used.append(date_span)
    if unlock is None and term_days:
        unlock = today + datetime.timedelta(days=term_days)
    if unlock is not None and term_days is None:
        term_days = max((unlock - today).days, 0)

    apr = None
    m = _PCT_RE.search(low)
    if m:
        apr = _f(m.group(1))
    else:
        # Без знака відсотка ставка — це останнє число, яке не пішло ні в суму,
        # ні в строк. Саме так вона й пише: «200 usdt bybit savings 6.77».
        for num in re.finditer(_NUM, low):
            if any(s <= num.start() < e for s, e in used):
                continue
            value = _f(num.group(0))
            if value is None or value > 100:
                continue
            apr = value
    if apr is not None and apr > 100:
        apr = None

    if any(w in low for w in _FIXED_WORDS):
        kind = KIND_FIXED
    elif any(w in low for w in _FLEX_WORDS):
        kind = KIND_FLEX
        unlock, term_days = None, None
    else:
        kind = KIND_FIXED if unlock else KIND_FLEX

    product = normalize_product(low, exchange, kind)
    return {
        "coin": coin,
        "amount": amount,
        "exchange": exchange,
        "product": product,
        "apr": apr / 100.0 if apr is not None else None,
        "kind": kind,
        "term_days": term_days,
        "unlock_date": unlock.isoformat() if unlock else None,
        "note": raw,
    }


def parse_text(text: str, today: Optional[datetime.date] = None) -> list[dict]:
    """Кілька рядків за раз — саме так вона й записує вклади пачкою."""
    out = []
    for line in (text or "").splitlines():
        dep = parse_line(line, today)
        if dep:
            out.append(dep)
    return out


def clean_ai_deposit(item: dict, today: Optional[datetime.date] = None) -> Optional[dict]:
    """Відповідь порадника -> той самий вклад, що й у формули.

    AI розбирає вільний текст краще за регулярку, але цифрам з нього довіряти
    наосліп не можна: біржу й монету звіряємо зі своїми списками, а строк
    перетворюємо на дату самі.
    """
    today = today or datetime.date.today()
    coin = str(item.get("coin") or "").upper().strip()
    amount = _f(item.get("amount"))
    if not coin or not amount or amount <= 0:
        return None

    exchange = EXCHANGES.get(str(item.get("exchange") or "").lower().strip(), "")
    apr = _f(item.get("apr_pct"))
    apr = apr / 100.0 if apr is not None and 0 < apr <= 100 else None

    term_days = None
    raw_term = _f(item.get("term_days"))
    if raw_term and raw_term > 0:
        term_days = int(raw_term)

    unlock = None
    raw_date = str(item.get("unlock_date") or "")[:10]
    if raw_date:
        try:
            unlock = datetime.date.fromisoformat(raw_date)
        except ValueError:
            unlock = None
    if unlock is None and term_days:
        unlock = today + datetime.timedelta(days=term_days)
    if unlock is not None and term_days is None:
        term_days = max((unlock - today).days, 0)

    kind = KIND_FIXED if str(item.get("kind") or "").lower().startswith("fix") else ""
    if not kind:
        kind = KIND_FIXED if unlock else KIND_FLEX
    if kind == KIND_FLEX:
        unlock, term_days = None, None

    return {
        "coin": coin,
        "amount": amount,
        "exchange": exchange,
        "product": normalize_product(str(item.get("product") or ""), exchange, kind),
        "apr": apr,
        "kind": kind,
        "term_days": term_days,
        "unlock_date": unlock.isoformat() if unlock else None,
        "note": str(item.get("note") or "").strip(),
    }


def missing_fields(dep: dict) -> list[str]:
    """Чого в записі бракує — простими словами, щоб вона побачила це в підтвердженні."""
    gaps = []
    if not dep.get("exchange"):
        gaps.append("біржу")
    if dep.get("apr") is None:
        gaps.append("ставку")
    if not dep.get("product"):
        gaps.append("назву продукту")
    return gaps


def is_stable(coin: str) -> bool:
    return str(coin or "").upper() in STABLES


def year_income_usd(dep: dict, prices: Optional[dict] = None) -> float:
    """Скільки вклад приносить за рік у доларах. 0, якщо ціни монети не знаємо."""
    apr = dep.get("apr")
    amount = float(dep.get("amount") or 0.0)
    if not apr or amount <= 0:
        return 0.0
    coin = str(dep.get("coin") or "").upper()
    price = 1.0 if is_stable(coin) else (prices or {}).get(coin)
    return amount * apr * price if price else 0.0


def unlock_date(dep: dict) -> Optional[datetime.date]:
    raw = dep.get("unlock_date")
    if not raw:
        return None
    try:
        return datetime.date.fromisoformat(str(raw)[:10])
    except (TypeError, ValueError):
        return None


def due_deposits(deposits: list, today: Optional[datetime.date] = None) -> list[dict]:
    """Вклади, у яких СЬОГОДНІ (або вже раніше) розморозка, а нагадування ще не було.

    Саме в день розморозки, а не за три дні: за три дні вона про це забуде, а
    сьогодні можна піти й забрати.
    """
    today = today or datetime.date.today()
    out = []
    for dep in deposits:
        date = unlock_date(dep)
        if date is None or date > today:
            continue
        if str(dep.get("reminded_on") or "")[:10] == today.isoformat():
            continue
        out.append(dep)
    return out


def journal_by_place(deposits: list) -> dict:
    """{(біржа, монета): сума за журналом} — з чим порівнюємо реальні залишки."""
    out: dict = {}
    for dep in deposits:
        key = (str(dep.get("exchange") or ""), str(dep.get("coin") or "").upper())
        out[key] = out.get(key, 0.0) + float(dep.get("amount") or 0.0)
    return out


def reconcile(portfolio, deposits: list) -> list[dict]:
    """Журнал проти реальних залишків у earn на біржах.

    Джерело правди — ЗАВЖДИ ключі. Тому розбіжність описуємо з боку біржі:
    «на біржі лежить більше, ніж записано, і про ці гроші я нічого не знаю».
    """
    if portfolio is None or getattr(portfolio, "empty", True):
        return []

    real: dict = {}
    for h in portfolio.in_earn():
        key = (h.exchange, h.coin)
        cur = real.setdefault(key, {"amount": 0.0, "usd": 0.0})
        cur["amount"] += h.amount
        cur["usd"] += h.value()

    book = journal_by_place(deposits)
    rows = []
    for key in sorted(set(real) | set(book)):
        exchange, coin = key
        if not exchange:
            continue        # вклад без біржі порівнювати нема з чим
        actual = real.get(key, {"amount": 0.0, "usd": 0.0})
        written = book.get(key, 0.0)
        diff = actual["amount"] - written
        if abs(diff) <= 0:
            continue
        price = (actual["usd"] / actual["amount"]) if actual["amount"] else (1.0 if coin in STABLES else 0.0)
        diff_usd = abs(diff) * price
        base = max(actual["amount"], written)
        if diff_usd < RECONCILE_MIN_USD or (base and abs(diff) / base < RECONCILE_MIN_SHARE):
            continue
        rows.append({
            "exchange": exchange,
            "coin": coin,
            "written": written,
            "actual": actual["amount"],
            "diff": diff,
            "diff_usd": diff_usd,
            "actual_usd": actual["usd"],
            "price": price,
        })
    rows.sort(key=lambda r: -r["diff_usd"])
    return rows


def rate_drops(deposits: list, rates: dict, min_gain_year: float,
               prices: Optional[dict] = None) -> list[dict]:
    """Гнучкі вклади, де ставка помітно просіла проти тієї, під яку вона заходила.

    У гнучких продуктах ставка плаває щодня, тому поріг — не у відсотках, а в
    грошах: той самий поріг вигоди, що й скрізь, щоб не смикати через копійки.
    """
    out = []
    for dep in deposits:
        entry = dep.get("apr")
        coin = str(dep.get("coin") or "").upper()
        if not entry or dep.get("kind") == KIND_FIXED:
            continue        # у фіксованому ставка й не мала мінятись
        value = rates.get(coin)
        now, offer = (value if isinstance(value, tuple) else (value, None))
        if not now or now >= entry - 1e-9:
            continue
        # Про одне й те саме падіння двічі не пишемо: ставка коливається щодня.
        told = dep.get("rate_alert_apr")
        if told is not None and now >= float(told) - RATE_DROP_PP:
            continue
        amount = float(dep.get("amount") or 0.0)
        price = 1.0 if coin in STABLES else (prices or {}).get(coin) or 0.0
        lost = (entry - now) * amount * price
        if lost < min_gain_year:
            continue
        out.append({
            "deposit": dep,
            "coin": coin,
            "entry_apr": entry,
            "now_apr": now,
            "was_year": entry * amount * price,
            "now_year": now * amount * price,
            "lost_year": lost,
            "offer": offer,
        })
    out.sort(key=lambda r: -r["lost_year"])
    return out


def active_totals(deposits: list, prices: Optional[dict] = None) -> dict:
    """Підсумок активних вкладів: скільки доларів, які монети, скільки на рік."""
    usd = income = 0.0
    coins: dict = {}
    for dep in deposits:
        coin = str(dep.get("coin") or "").upper()
        amount = float(dep.get("amount") or 0.0)
        if is_stable(coin):
            usd += amount
        else:
            coins[coin] = coins.get(coin, 0.0) + amount
        income += year_income_usd(dep, prices)
    return {"usd": usd, "coins": coins, "income_year": income, "count": len(deposits)}
