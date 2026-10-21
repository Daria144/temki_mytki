"""Щомісячна купівля ETF на IBKR: пасивний глобальний buy & hold з мінімальною комісією.

Це окрема від крипти частина бота. Тут євро, Xetra і один ордер на місяць.

Стратегія — вхідні дані, бот її не переглядає й нічого не оптимізує:
• WEBN 85% + CEMA 15%, внесок €200/міс, переказ SEPA з Revolut на IBKR;
• один ордер на місяць і один фонд на місяць — так найдешевше по комісії;
• календар: серпень — CEMA (рівно один пай), решта одинадцяти місяців — WEBN;
• у серпні на пай CEMA докладаються власні гроші поверх €200, бо пай дорожчий;
• Xetra, EUR, market order, друга половина дня по Франкфурту;
• ніколи не продавати для вирівнювання часток — тільки спрямовувати нові внески;
• фонди накопичувальні (accumulating): податкова подія лише при продажу;
• шукати фонд за ISIN, а не за тікером.

Чому CEMA лише раз на рік: пай CEMA коштує помітно більше за місячний внесок, а
дробових паїв для європейських ETF на Xetra немає. Тому цільові 15% недосяжні в
принципі — один пай на рік дає близько 10-11%, два вже понад 20%. Власниця
свідомо обрала один пай на рік і меншу фактичну частку.
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import time
from dataclasses import dataclass
from typing import Optional

import httpx

from app.formatting import fmt_eur, fmt_eur_money, fmt_eur_price, fmt_units

log = logging.getLogger(__name__)


# --- фонди ---

@dataclass(frozen=True)
class Fund:
    ticker: str        # як фонд називає користувачка — так підписані кнопки й тексти
    name: str          # повна назва фонду
    isin: str          # єдиний надійний ідентифікатор: шукати треба саме за ним
    target_pct: float  # цільова частка в портфелі
    xetra: str         # тікер саме на Xetra (може не збігатися зі звичною назвою)
    yahoo: str         # символ у Yahoo Finance (запасне джерело ціни)


WEBN = Fund(
    ticker="WEBN",
    name="Amundi Prime All Country World UCITS ETF Acc",
    isin="IE0003XJA0J9",
    target_pct=85.0,
    xetra="WEBN",
    yahoo="WEBN.DE",
)
# Увага, це не описка: на Xetra цей фонд торгується під тікером CEBL, а CEMA —
# тікер лондонського листингу (той самий фонд, але в доларах). Під символом
# "CEMA.DE" публічні джерела віддають ЗОВСІМ ІНШИЙ фонд (iShares MSCI World Swap
# EUR Hedged). Це і є та сама пастка, через яку шукати треба за ISIN.
CEMA = Fund(
    ticker="CEMA",
    name="iShares MSCI EM Asia UCITS ETF Acc",
    isin="IE00B5L8K969",
    target_pct=15.0,
    xetra="CEBL",
    yahoo="CEBL.DE",
)

FUNDS = {f.ticker: f for f in (WEBN, CEMA)}

# Календар: серпень — CEMA, решта одинадцяти місяців — WEBN.
CEMA_MONTHS = (8,)

DEFAULT_AMOUNT = 200.0          # звичайний внесок за місяць, EUR
EXCHANGE = "Xetra"
FEE_PER_ORDER = 1.25            # мінімум за ордер на Tiered
FEE_PER_YEAR = 15.0             # орієнтовно за рік при одному ордері на місяць

# Стартова позиція, яка вже була на рахунку до першого нагадування бота.
# У журнал її не пишемо (журнал — це саме щомісячні внески), але в підсумках
# враховуємо, інакше фактичні частки будуть неправдою.
START_UNITS = {"WEBN": 29.0}
START_INVESTED = {"WEBN": 400.0}

STATUS_REMINDED = "reminded"
STATUS_DONE = "done"
STATUS_SKIPPED = "skipped"

MONTHS_UK = (
    "січень", "лютий", "березень", "квітень", "травень", "червень",
    "липень", "серпень", "вересень", "жовтень", "листопад", "грудень",
)


# --- глайд-пат ---

@dataclass(frozen=True)
class Stage:
    year: Optional[int]   # з якого року стратегії діє; None = «цільова дата»
    label: str
    shares: dict          # частки по фондах, %


# Останній етап без року: цільову дату користувачка ще не називала, тому не вигадуємо.
GLIDE_PATH = (
    Stage(0, "роки 0–8: тільки акції", {"WEBN": 85.0, "CEMA": 15.0}),
    Stage(10, "рік 10: додаються облігації", {"WEBN": 75.0, "CEMA": 10.0, "AGGH": 15.0}),
    Stage(13, "рік 13: більше облігацій", {"WEBN": 60.0, "CEMA": 10.0, "AGGH": 30.0}),
    Stage(None, "цільова дата", {"WEBN": 55.0, "CEMA": 5.0, "AGGH": 40.0}),
)


# --- календар ---

def month_key(when: datetime.date) -> str:
    """Місяць у вигляді YYYY-MM — це і ключ у журналі."""
    return f"{when.year:04d}-{when.month:02d}"


def month_name(when: datetime.date) -> str:
    return f"{MONTHS_UK[when.month - 1]} {when.year}"


def fund_for_month(when: datetime.date) -> Fund:
    """Який фонд купуємо цього місяця. Серпень — CEMA, решта одинадцяти — WEBN."""
    return CEMA if when.month in CEMA_MONTHS else WEBN


def is_annual_cema_month(when: datetime.date) -> bool:
    """Чи це той єдиний місяць на рік, коли купуємо один пай CEMA."""
    return when.month in CEMA_MONTHS


def fund_for_month_key(month: str) -> Optional[Fund]:
    """Фонд за ключем місяця 'YYYY-MM' — потрібно для кнопок, де є лише ключ."""
    try:
        year, mon = (int(x) for x in month.split("-"))
        return fund_for_month(datetime.date(year, mon, 1))
    except (ValueError, TypeError, AttributeError):
        return None


def first_business_day(year: int, month: int) -> datetime.date:
    """Перший робочий день місяця — перший день, що не субота й не неділя.

    Відоме обмеження: державні свята Німеччини не враховуються. Реально це може
    зачепити 1 січня, 1 травня, 3 жовтня і Великодній понеділок — тоді Xetra
    закрита, і нагадування прийде на день-два раніше, ніж вийде поставити ордер.
    Для нагадування це не критично (ордер усе одно ставиться вручну), але знати варто.
    """
    day = datetime.date(year, month, 1)
    while day.weekday() >= 5:   # 5 = субота, 6 = неділя
        day += datetime.timedelta(days=1)
    return day


def is_first_business_day(when: datetime.date) -> bool:
    return when == first_business_day(when.year, when.month)


# --- етап стратегії ---

def strategy_start(storage) -> datetime.date:
    """Дата початку стратегії. Перший запуск запамʼятовує «сьогодні» назавжди.

    Зберігаємо в базу, щоб етап глайд-пату не зʼїхав при перезапуску бота.
    """
    raw = storage.get_setting("ibkr_start_date", "")
    if raw:
        try:
            return datetime.date.fromisoformat(raw)
        except ValueError:
            log.warning("ibkr: зіпсована дата початку (%r) — перезаписую", raw)
    today = datetime.date.today()
    storage.set_setting("ibkr_start_date", today.isoformat())
    return today


def years_elapsed(start: datetime.date, today: datetime.date) -> int:
    """Скільки ПОВНИХ років триває стратегія."""
    years = today.year - start.year
    if (today.month, today.day) < (start.month, start.day):
        years -= 1
    return max(years, 0)


def current_stage(years: int) -> Stage:
    """Етап глайд-пату на зараз (етап «цільова дата» сам не настає — вона не задана)."""
    stage = GLIDE_PATH[0]
    for s in GLIDE_PATH:
        if s.year is not None and years >= s.year:
            stage = s
    return stage


def next_stage(years: int) -> Optional[Stage]:
    """Найближчий етап із зафіксованим роком, який ще не настав."""
    for s in GLIDE_PATH:
        if s.year is not None and s.year > years:
            return s
    return None


def stage_warning(start: datetime.date, today: datetime.date,
                  months_ahead: int = 2) -> Optional[dict]:
    """Чи пора попереджати про зміну структури портфеля (за місяць-два до етапу).

    Повертає None, якщо до наступного етапу ще далеко або етапів більше немає.
    """
    years = years_elapsed(start, today)
    nxt = next_stage(years)
    if nxt is None:
        return None
    # Дата, коли настане етап: та сама дата старту, але через nxt.year років.
    try:
        when = start.replace(year=start.year + nxt.year)
    except ValueError:                       # 29 лютого у невисокосному році
        when = start.replace(year=start.year + nxt.year, day=28)
    months_left = (when.year - today.year) * 12 + (when.month - today.month)
    if months_left <= 0 or months_left > months_ahead:
        return None
    return {
        "stage": nxt,
        "when": when,
        "months_left": months_left,
        "current": current_stage(years),
    }


# --- скільки паїв виходить на внесок ---

def shares_for(amount: float, price: Optional[float]) -> dict:
    """Скільки ЦІЛИХ паїв виходить на суму внеску.

    Дробові паї для європейських ETF на IBKR не працюють, тому округляємо вниз.
    Якщо навіть один пай не влазить у суму — кажемо, скільки не хапає (short).
    """
    if not price or price <= 0:
        return {"units": None, "spent": None, "leftover": None, "short": None}
    units = int(amount // price)
    return {
        "units": units,
        "spent": round(units * price, 2),
        "leftover": round(amount - units * price, 2),
        # Скільки не хапає до одного паю (тільки коли навіть один не влазить).
        "short": round(price - amount, 2) if units == 0 else None,
    }


def one_share_for(price: Optional[float], base: float = DEFAULT_AMOUNT) -> dict:
    """Рівно ОДИН пай — так купуємо CEMA раз на рік.

    Сума цього місяця дорівнює ціні паю, а не звичайним €200, тому окремо
    рахуємо, скільки доведеться докласти (extra) поверх звичайного внеску.
    Ціна плаває, тому нічого не фіксуємо константами.
    """
    if not price or price <= 0:
        # Ціни немає: суму назвати не можемо, але нагадування все одно піде.
        return {"units": None, "spent": None, "leftover": None, "short": None,
                "amount": base, "extra": None}
    price = round(price, 2)
    return {
        "units": 1,
        "spent": price,
        # Пай дорожчий за внесок — треба докласти; якщо колись подешевіє, лишиться решта.
        "extra": round(price - base, 2) if price > base else 0.0,
        "leftover": round(base - price, 2) if price < base else 0.0,
        "short": None,
        "amount": price,
    }


def planned_amount(fund: Fund, price: Optional[float] = None,
                   base: float = DEFAULT_AMOUNT) -> float:
    """Скільки грошей потрібно цього місяця: у місяць CEMA — рівно на один пай."""
    if fund.ticker != CEMA.ticker:
        return base
    return one_share_for(price, base)["amount"]


# --- план на місяць ---

def month_plan(today: Optional[datetime.date] = None,
               price: Optional[float] = None,
               amount: float = DEFAULT_AMOUNT) -> dict:
    """Усе про купівлю цього місяця: фонд, чому саме він, сума, ціна, паї.

    `amount` — це звичайний місячний внесок. У місяць CEMA підсумкова сума буде
    іншою: рівно ціна одного паю, бо купуємо один пай і докладаємо різницю.
    """
    today = today or datetime.date.today()
    fund = fund_for_month(today)
    annual = is_annual_cema_month(today)
    numbers = one_share_for(price, amount) if annual else shares_for(amount, price)
    return {
        "month": month_key(today),
        "month_name": month_name(today),
        "fund": fund,
        "single_share": annual,   # місяць CEMA: один пай на рік, сума не €200
        "base": amount,           # звичайний внесок
        "amount": amount,         # скільки треба цього місяця (у серпні — ціна паю)
        "extra": None,            # скільки докласти поверх звичайного внеску
        "price": price,
        "first_business_day": first_business_day(today.year, today.month),
        **numbers,
    }


# --- підсумки журналу ---

def expected_cema_pct(cema_price: Optional[float] = None,
                      base: float = DEFAULT_AMOUNT) -> Optional[float]:
    """Яку частку CEMA дає календар: 11 внесків WEBN по €200 + один пай CEMA на рік.

    Рахуємо від живої ціни паю, бо вона плаває: при ціні близько €260 виходить
    ~10,5%. Рівно цільових 15% досягти неможливо — два паї на рік дали б понад
    20%, а дробових паїв для європейських ETF на Xetra немає.
    Без ціни повертаємо None: вигадувати число не будемо.
    """
    if not cema_price or cema_price <= 0:
        return None
    webn_year = base * (12 - len(CEMA_MONTHS))
    return round(cema_price / (webn_year + cema_price) * 100.0, 1)


def totals(storage, include_start: bool = True) -> dict:
    """Скільки всього внесено, скільки в який фонд і фактичні частки проти цільових.

    Календар «один пай CEMA раз на рік, у серпні» дає CEMA близько 10-11% нових
    внесків замість цільових 15% (див. expected_cema_pct) — тому фактична частка
    й цікава. Це очікуваний результат рішення, а не помилка обліку.
    """
    rows = storage.ibkr_totals()
    by_fund = {}
    for ticker, fund in FUNDS.items():
        row = rows.get(ticker) or {}
        invested = float(row.get("invested") or 0.0)
        units = float(row.get("units") or 0.0)
        months = int(row.get("months") or 0)
        if include_start:
            invested += START_INVESTED.get(ticker, 0.0)
            units += START_UNITS.get(ticker, 0.0)
        by_fund[ticker] = {
            "name": fund.name,
            "isin": fund.isin,
            "months": months,
            "invested": round(invested, 2),
            "units": units,
            "target_pct": fund.target_pct,
            "actual_pct": None,
        }
    total = sum(f["invested"] for f in by_fund.values())
    if total > 0:
        for f in by_fund.values():
            f["actual_pct"] = round(f["invested"] / total * 100.0, 1)
    return {
        "total": round(total, 2),
        "months": sum(f["months"] for f in by_fund.values()),
        "by_fund": by_fund,
        "start_included": include_start,
    }


def status_label(status: Optional[str]) -> str:
    return {
        STATUS_DONE: "✅ зроблено",
        STATUS_SKIPPED: "✖️ пропущено цього місяця",
        STATUS_REMINDED: "⬜ ще не зроблено",
    }.get(status or "", "⬜ ще не починали")


# --- ціни: перевірене джерело + запасне + кеш ---

# Головне джерело — офіційний API Deutsche Börse: запит іде ЗА ISIN, тому взяти
# інший фонд просто неможливо (mic=XETR — це саме Xetra).
BOERSE_URL = "https://api.boerse-frankfurt.de/v1/data/quote_box/single"
# Запасне — Yahoo Finance. Тут запит уже за символом, тому додатково перевіряємо
# валюту й біржу у відповіді: інакше легко отримати іншу версію фонду.
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart"

PRICE_TTL = 900              # 15 хв: ціна ETF для наших цілей змінюється повільно
PRICE_FALLBACK_MAX_AGE = 6 * 3600   # застарілу ціну віддаємо, але не старішу за це


class EtfPrices:
    """Ціни паїв на Xetra в євро. М'яка деградація: ціна — приємний бонус, не умова.

    Якщо джерела недоступні — повертаємо None, і нагадування все одно піде,
    просто без ціни й кількості паїв.
    """

    def __init__(self) -> None:
        self._cache: dict = {}
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        return None

    def _get_cached(self, isin: str, max_age: float):
        entry = self._cache.get(isin)
        if entry and time.time() - entry["ts"] < max_age:
            return entry["data"], time.time() - entry["ts"]
        return None, None

    def _set_cache(self, isin: str, data: dict) -> None:
        self._cache[isin] = {"ts": time.time(), "data": data}

    async def _get(self, url: str, params: dict, timeout: int = 15) -> httpx.Response:
        async with httpx.AsyncClient(
            timeout=timeout, headers={"accept": "application/json"}
        ) as cl:
            r = await cl.get(url, params=params)
            r.raise_for_status()
            return r

    async def _fetch_boerse(self, fund: Fund) -> dict:
        """Deutsche Börse: запит за ISIN, тому фонд гарантовано той самий."""
        r = await self._get(BOERSE_URL, params={"isin": fund.isin, "mic": "XETR"})
        j = r.json()
        if str(j.get("isin") or "") != fund.isin:
            raise ValueError(f"Xetra віддала інший ISIN: {j.get('isin')}")
        price = j.get("lastPrice")
        if not price:
            raise ValueError("Xetra: немає ціни")
        return {
            "isin": fund.isin,
            "price": float(price),
            "currency": "EUR",
            "source": "Deutsche Börse (Xetra)",
            "as_of": j.get("timestampLastPrice"),
        }

    async def _fetch_yahoo(self, fund: Fund) -> dict:
        """Yahoo Finance: запит за символом, тому перевіряємо валюту й тип паперу."""
        r = await self._get(
            f"{YAHOO_URL}/{fund.yahoo}", params={"range": "5d", "interval": "1d"}
        )
        meta = r.json()["chart"]["result"][0]["meta"]
        if (meta.get("currency") or "").upper() != "EUR":
            raise ValueError(f"Yahoo {fund.yahoo}: валюта {meta.get('currency')}, а не EUR")
        if meta.get("instrumentType") != "ETF":
            raise ValueError(f"Yahoo {fund.yahoo}: це не ETF")
        price = meta.get("regularMarketPrice")
        if not price:
            raise ValueError(f"Yahoo {fund.yahoo}: немає ціни")
        return {
            "isin": fund.isin,
            "price": float(price),
            "currency": "EUR",
            "source": f"Yahoo Finance ({fund.yahoo}, {meta.get('fullExchangeName')})",
            "as_of": meta.get("regularMarketTime"),
        }

    async def fetch(self, fund: Fund) -> Optional[dict]:
        """Ціна одного фонду. Ніколи не падає — у найгіршому разі None."""
        async with self._lock:
            fresh, _ = self._get_cached(fund.isin, PRICE_TTL)
        if fresh is not None:
            return fresh

        for name, fn in (("Xetra", self._fetch_boerse), ("Yahoo", self._fetch_yahoo)):
            try:
                data = await fn(fund)
            except Exception as e:  # noqa: BLE001
                log.warning("%s: джерело %s недоступне (%s)", fund.ticker, name, e)
                continue
            async with self._lock:
                self._set_cache(fund.isin, data)
            log.info("%s (%s): ціна %.2f EUR з %s",
                     fund.ticker, fund.isin, data["price"], data["source"])
            return data

        async with self._lock:
            stale, age = self._get_cached(fund.isin, PRICE_FALLBACK_MAX_AGE)
        if stale is not None:
            log.warning("%s: джерела ціни недоступні — беру з кешу (%.0f хв тому)",
                        fund.ticker, age / 60)
            return stale
        log.error("%s: ціна недоступна і кешу немає", fund.ticker)
        return None

    async def price_of(self, fund: Fund) -> Optional[float]:
        data = await self.fetch(fund)
        return data["price"] if data else None


async def safe_price(prices, fund: Fund) -> Optional[float]:
    """Ціна фонду там, де падати не можна взагалі (нагадування має піти й без ціни)."""
    if prices is None:
        return None
    try:
        return await prices.price_of(fund)
    except Exception:  # noqa: BLE001
        log.warning("ibkr: ціна %s недоступна", fund.ticker, exc_info=True)
        return None


# --- тексти ---

RULES_BLOCK = (
    "Як робити (щоб не переплатити комісію):\n"
    f"• Один ордер, один фонд — комісія близько {fmt_eur_money(FEE_PER_ORDER)} "
    f"(~{fmt_eur(FEE_PER_YEAR)}/рік)\n"
    f"• Біржа {EXCHANGE}, валюта EUR, тип ордера — market order\n"
    "• Друга половина дня по Франкфурту (приблизно після 14:00)\n"
    "• Шукай фонд ЗА ISIN, а не за тікером: під тим самим тікером на різних біржах "
    "бувають інші версії фонду"
)

REMEMBER_BLOCK = (
    "Пам'ятаємо:\n"
    "• Ніколи нічого не продаємо, щоб вирівняти частки — тільки спрямовуємо нові внески\n"
    "• Фонди накопичувальні (accumulating): дивіденди реінвестуються самі, "
    "податкова подія буде тільки якщо колись продаси"
)


def units_word(q: Optional[float]) -> str:
    """«1 пай / 2 паї / 5 паїв» — щоб у тексті не вилазило «74 паїв»."""
    if q is None:
        return "? паїв"
    if q != int(q):                      # дробові паї (раптом вона впише 15,5)
        return f"{fmt_units(q)} паю"
    n = int(q)
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} пай"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} паї"
    return f"{n} паїв"


def _why_this_fund(fund: Fund) -> str:
    if fund.ticker == CEMA.ticker:
        return ("Чому він: CEMA купуємо раз на рік, і саме серпень — той місяць. "
                "Решту одинадцяти місяців — WEBN.")
    return "Чому він: CEMA купуємо раз на рік, у серпні. Решту одинадцяти місяців — WEBN."


def _annual_price_lines(plan: dict) -> list[str]:
    """Місяць CEMA: купуємо рівно один пай, доклавши різницю поверх звичайних €200.

    Рішення вже ухвалене, тому це інструкція, а не перелік варіантів.
    """
    base = plan["base"]
    if plan["price"] is None:
        return [
            "Купуємо рівно 1 пай CEMA — це раз на рік.",
            f"Ціну паю зараз не вдалось отримати, а вона помітно вища за звичайні "
            f"{fmt_eur(base)}. Глянь ціну на біржі й переведи стільки, скільки коштує "
            "один пай — цього місяця сума рахується саме за паєм, а не за внеском.",
        ]
    out = [
        "Купуємо рівно 1 пай CEMA — це раз на рік.",
        f"Ціна паю: {fmt_eur_price(plan['price'])} ({EXCHANGE})",
    ]
    extra = plan["extra"] or 0.0
    if extra > 0:
        out.append(
            f"Сума цього разу: {fmt_eur_price(plan['amount'])} — це звичайні "
            f"{fmt_eur(base)} плюс {fmt_eur_price(extra)}, які треба докласти "
            "своїх, щоб вийшов цілий пай."
        )
    else:
        out.append(
            f"Сума цього разу: {fmt_eur_price(plan['amount'])} — звичайних "
            f"{fmt_eur(base)} хапає, докладати нічого не треба, а решта "
            f"{fmt_eur_price(plan['leftover'])} просто залишиться на рахунку."
        )
    return out


def _price_lines(plan: dict) -> list[str]:
    """Ціна й кількість паїв. Без ціни блок просто менший — це нормально."""
    if plan.get("single_share"):
        return _annual_price_lines(plan)

    amount = plan["amount"]
    if plan["price"] is None:
        return [
            f"Сума: {fmt_eur(amount)}",
            "Ціну паю зараз не вдалось отримати — подивись її в IBKR, "
            "коли ставитимеш ордер.",
        ]
    out = [
        f"Сума: {fmt_eur(amount)}",
        f"Ціна паю: {fmt_eur_price(plan['price'])} ({EXCHANGE})",
    ]
    if plan["units"] and plan["units"] > 0:
        out.append(
            f"Виходить {units_word(plan['units'])} на {fmt_eur_price(plan['spent'])}, "
            f"решта {fmt_eur_price(plan['leftover'])} просто залишиться на рахунку"
        )
    else:
        # Пай дорожчий за внесок. Для CEMA це штатна ситуація й іде іншою гілкою,
        # тут лишається страховка на випадок, якщо колись подорожчає й WEBN.
        out.append(
            f"⚠️ Один пай коштує більше за внесок — на {fmt_eur(amount)} не виходить "
            f"навіть одного паю (не хапає {fmt_eur_money(plan['short'])}).\n"
            f"   Купи 1 пай за {fmt_eur_price(plan['price'])}, доклавши різницю."
        )
    return out


def reminder_text(plan: dict, repeat: bool = False) -> str:
    """Текст нагадування про щомісячну купівлю. Головне тут — ISIN."""
    fund = plan["fund"]
    if repeat:
        head = [
            f"🔔 Нагадую: купівля ETF за {plan['month_name']} ще не позначена зробленою.",
            "",
        ]
    else:
        head = [
            "🗓 Новий місяць — час на щомісячну купівлю ETF на IBKR.",
            "",
        ]
    out = head + [
        f"Цього місяця купуємо {fund.ticker} — {fund.name}",
        f"ISIN: {fund.isin}  ← шукай саме за цим номером",
        f"На Xetra цей фонд під тікером {fund.xetra}.",
        _why_this_fund(fund),
        "",
    ]
    out += _price_lines(plan)
    out += ["", RULES_BLOCK, "", REMEMBER_BLOCK, ""]
    out.append("Як зробиш — тисни «Зроблено». Якщо цього місяця пропускаєш — так і скажи.")
    return "\n".join(out)


def _history_lines(rows: list) -> list[str]:
    out = []
    for r in rows:
        mark = status_label(r.get("status")).split(" ")[0]
        line = f"• {r['month']} · {r['fund']} · {fmt_eur(r['amount'] or 0)} {mark}"
        if r.get("units"):
            line += f" ({units_word(r['units'])})"
        out.append(line)
    return out


def _plural_deposits(n: int) -> str:
    """«1 внесок / 2 внески / 5 внесків» — інакше в тексті вилазить «внесків(и)»."""
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} внесок"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} внески"
    return f"{n} внесків"


def _shares_lines(tot: dict, cema_price: Optional[float] = None) -> list[str]:
    out = ["🎯 Частки фондів (за вкладеними грошима):"]
    for ticker, f in tot["by_fund"].items():
        actual = f"{f['actual_pct']:.1f}%" if f["actual_pct"] is not None else "—"
        out.append(
            f"• {ticker}: цільова {f['target_pct']:.0f}%, фактично {actual} "
            f"({fmt_eur(f['invested'])}, {units_word(f['units'])})"
        )
    expected = expected_cema_pct(cema_price)
    expected_str = f"близько {expected:.1f}%".replace(".", ",") if expected else "близько 10–11%"
    out.append(
        f"Календар «один пай CEMA раз на рік, у серпні» дає CEMA {expected_str} "
        "нових внесків замість цільових 15%. Рівно 15% при такій ціні паю просто "
        "не виходить: менше — це один пай на рік, більше — вже два паї, а це понад "
        "20%. Дробових паїв для європейських ETF не буває, тож середини немає."
    )
    out.append(
        "Це нормально й нічого виправляти не треба: така різниця у частці впливає "
        "на портфель мізерно, набагато важливіше просто не пропускати внески. "
        "Продавати нічого не треба — вирівнюємо тільки новими внесками."
    )
    return out


def _stage_lines(start: datetime.date, today: datetime.date) -> list[str]:
    years = years_elapsed(start, today)
    stage = current_stage(years)
    parts = ", ".join(f"{k} {v:.0f}%" for k, v in stage.shares.items())
    out = [
        f"🧭 Етап стратегії: рік {years} ({stage.label})",
        f"Структура на цей етап: {parts}",
    ]
    nxt = next_stage(years)
    if nxt is not None:
        nparts = ", ".join(f"{k} {v:.0f}%" for k, v in nxt.shares.items())
        out.append(f"Наступна зміна — рік {nxt.year}: {nparts}")
    return out


def status_text(plan: dict, record: Optional[dict], history: list,
                tot: dict, start: datetime.date,
                today: Optional[datetime.date] = None) -> str:
    """Текст команди /ibkr: що купуємо, статус, історія, підсумки, етап."""
    today = today or datetime.date.today()
    fund = plan["fund"]
    out = [
        "📈 Щомісячна купівля ETF на IBKR",
        "",
        f"Цього місяця ({plan['month_name']}) купуємо {fund.ticker} — {fund.name}",
        f"ISIN: {fund.isin}  ← шукай саме за цим номером",
        f"На Xetra цей фонд під тікером {fund.xetra}.",
        _why_this_fund(fund),
        "",
    ]
    out += _price_lines(plan)
    out += [
        "",
        f"Статус за {plan['month']}: {status_label((record or {}).get('status'))}",
    ]
    if record and record.get("units"):
        out.append(f"Куплено: {units_word(record['units'])}")
    out += [
        "",
        f"Перший робочий день цього місяця: {plan['first_business_day'].strftime('%d.%m.%Y')}",
        "",
        RULES_BLOCK,
        "",
    ]
    if history:
        out += ["📜 Останні місяці:", *_history_lines(history), ""]
    start_eur = sum(START_INVESTED.values())
    out += [
        f"💰 Разом вкладено: {fmt_eur(tot['total'])} — це {_plural_deposits(tot['months'])} "
        f"плюс стартова купівля на {fmt_eur(start_eur)} "
        f"({units_word(START_UNITS['WEBN'])} WEBN)",
        "",
    ]
    # У серпні ціна в плані — це саме ціна CEMA, тож очікувану частку скажемо точно.
    cema_price = plan["price"] if fund.ticker == CEMA.ticker else None
    out += _shares_lines(tot, cema_price)
    out += ["", *_stage_lines(start, today), "", REMEMBER_BLOCK]
    return "\n".join(out)


def stage_warning_text(warn: dict) -> str:
    """Попередження, що скоро змінюється структура портфеля."""
    stage, nxt = warn["current"], warn["stage"]
    parts_now = ", ".join(f"{k} {v:.0f}%" for k, v in stage.shares.items())
    parts_new = ", ".join(f"{k} {v:.0f}%" for k, v in nxt.shares.items())
    when = warn["when"].strftime("%m.%Y")
    months = warn["months_left"]
    word = "місяць" if months == 1 else "місяці"
    return "\n".join([
        f"🧭 За {months} {word} ({when}) настає рік {nxt.year} стратегії — "
        "пора змінити структуру нових внесків.",
        "",
        f"Було: {parts_now}",
        f"Стане: {parts_new}",
        "",
        "AGGH — це фонд облігацій: він менше росте, але й менше падає. "
        "З роками його частка збільшується, щоб портфель став спокійнішим.",
        "",
        "Важливо: нічого продавати не треба. Просто нові внески підуть за новою "
        "пропорцією, а вже куплене лишається як є.",
        "",
        "Це попередження заздалегідь — зробимо, коли час настане.",
    ])
