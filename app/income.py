"""Облік ЗАРОБІТКУ з earn-позицій: скільки вона вже заробила і за якою ставкою.

Ключі відповідають на питання «скільки грошей лежить у earn» (app/portfolio.py).
Цей модуль — на інше: скільки ці гроші вже принесли і яку ставку їй НАСПРАВДІ
платять. Обіцяна ставка й фактична — дві різні цифри, і різниця між ними тут
головна: саме в ній видно, що продукт платить не те, що показує.

Звідки беруться цифри (це принципово, бо точне й приблизне не змішуємо):
  • OKX Simple Earn віддає поле earnings — заробіток ЗА ВЕСЬ ЧАС у цій позиції.
    Перевірено двома незалежними способами: amt мінус earnings дає рівно ту
    суму, яку вона колись поклала (500 USDT, 0.2 ETH), а сума погодинних
    нарахувань із lending-history сходиться з earnings до останнього знака.
  • Bybit віддає totalPnl (зараховане) і claimableYield (нараховане, ще не
    зараховане) — теж цифри від біржі.
  • Якщо біржа заробітку не дала, бот рахує його сам: сума × ставка × час.
    Тоді цифра ПРИБЛИЗНА і в текстах завжди підписана як порахована ботом.

Як рахується заробіток за проміжок. Наростаюче поле («за весь час») дає
приріст РІЗНИЦЕЮ між знімками; періодичне («за добу») — СУМОЮ. Плутати їх не
можна: різниця періодичних полів — це не заробіток, а шум.

«За весь час» видно вже з ПЕРШОГО знімка, бо наростаюче поле біржі накопичило
все, що було до початку обліку. Тому історія не втрачена й перший знімок не
показує «заробила $0».

Знімки лежать у таблиці earn_income (app/storage.py), кладе їх щоденне
завдання earn_income_check (app/alerts.py).
"""
from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass
from typing import Optional

from app.earn import STABLES

log = logging.getLogger(__name__)

YEAR_DAYS = 365.0

# Скільки діб мусить накритись знімками, щоб узагалі рахувати заробіток за
# проміжок і фактичну ставку. З одного знімка приросту немає взагалі, а з двох
# за кілька годин ставка стрибає в рази: на OKX її долари дають у позику, і
# погодинна ставка там — живий ринок, а не обіцянка.
MIN_SPAN_DAYS = 2

# Проміжки, які вона просила бачити. None — «за весь час».
WINDOWS = {"all": None, "d30": 30, "d7": 7}

# З якої різниці фактична й обіцяна ставки — це вже дві різні цифри, а не
# округлення. Той самий поріг, що й у /status для ставки в позиції.
RATE_GAP_PP = 0.0025


def _as_day(raw) -> Optional[datetime.date]:
    try:
        return datetime.date.fromisoformat(str(raw)[:10])
    except (TypeError, ValueError):
        return None


def _price(coin: str, prices: Optional[dict] = None) -> Optional[float]:
    """Ціна монети в доларах. Стейбли — це просто долари."""
    coin = str(coin or "").upper()
    if coin in STABLES:
        return 1.0
    value = (prices or {}).get(coin)
    return float(value) if value else None


def promised_rates(offers: Optional[list] = None) -> dict:
    """{(біржа, монета): найкраща ПУБЛІЧНА ставка} — те, що біржа обіцяє.

    Потрібно там, де ставки в самій позиції немає (Bybit її не віддає): без
    обіцяної ставки нема з чим порівнювати фактичну.
    """
    out: dict = {}
    for o in offers or []:
        apr = getattr(o, "apr", None)
        if apr is None:
            continue
        key = (getattr(o, "exchange", ""), getattr(o, "coin", ""))
        if key not in out or float(apr) > out[key]:
            out[key] = float(apr)
    return out


# --- денний знімок ---

def position_key(row: dict) -> tuple:
    return (str(row.get("exchange") or ""),
            str(row.get("coin") or "").upper(),
            str(row.get("product") or ""))


def _estimated_earned(row: dict, prev: Optional[dict],
                      day: datetime.date) -> float:
    """Скільки позиція мала принести з попереднього знімка: сума × ставка × час.

    Свідомо занижуємо: беремо МЕНШУ з двох сум. Якщо вона докинула грошей між
    знімками, нараховувати весь проміжок на нову суму означало б завищити
    заробіток, а завищена цифра тут гірша за занижену.

    Нуль без попереднього знімка — це не «не заробила», а «проміжок нульовий»:
    рахувати приріст ще нема від чого.
    """
    apr = row.get("apr")
    if not apr or not prev:
        return 0.0
    before = _as_day(prev.get("day"))
    if before is None or before >= day:
        return 0.0
    amount = min(float(row.get("amount") or 0.0), float(prev.get("amount") or 0.0))
    return amount * float(apr) * (day - before).days / YEAR_DAYS


def snapshot_rows(portfolio, prices: Optional[dict] = None,
                  rates: Optional[dict] = None,
                  day: Optional[datetime.date] = None,
                  previous: Optional[dict] = None) -> list[dict]:
    """Денний знімок: по рядку на кожну earn-позицію.

    previous — останній знімок по кожній позиції. Потрібен лише там, де біржа
    заробітку не дала: тоді бот рахує приріст сам і позначає його приблизним.

    Порожній список означає «нема чого писати»: біржа не відповіла або
    earn-позицій немає. Нулі в такому разі не пишемо свідомо — нуль у ряді
    потім не відрізнити від справжнього «нічого не заробила».
    """
    if portfolio is None or getattr(portfolio, "empty", True):
        return []
    day = day or datetime.date.today()
    previous = previous or {}
    out = []
    for h in portfolio.in_earn():
        promised = h.current_apr
        if promised is None:
            promised = (rates or {}).get((h.exchange, h.coin))
        row = {
            "day": day.isoformat(),
            "exchange": h.exchange,
            "coin": h.coin,
            "product": h.product or "",
            "amount": float(h.amount),
            "apr": float(promised) if promised is not None else None,
            "price": _price(h.coin, prices),
            "earned": 0.0,
            "cumulative": True,
            "measured": True,
        }
        if h.earned is not None:
            row["earned"] = float(h.earned)
            row["cumulative"] = bool(h.earned_cumulative)
        else:
            # Біржа заробітку не дала. cumulative=False саме тому: це приріст
            # за проміжок від попереднього знімка, а не сума за весь час.
            row["earned"] = _estimated_earned(row, previous.get(position_key(row)), day)
            row["cumulative"] = False
            row["measured"] = False
        out.append(row)
    return out


# --- заробіток за проміжок ---

@dataclass
class Period:
    """Заробіток за проміжок — і чесна метадата про те, звідки цифра взялась."""
    earned: float = 0.0        # у монеті
    usd: float = 0.0
    days: int = 0              # скільки діб реально накрито знімками
    amount: float = 0.0        # середня сума в позиції за проміжок, у монеті
    measured: bool = True      # True — цифра від біржі, False — рахував бот
    partial: bool = False      # знімків на весь запитаний проміжок не хватило
    gaps: int = 0              # скільки діб у ряді пропущено (біржа мовчала)
    resets: int = 0            # скільки разів позицію перезаклали
    enough: bool = False       # чи можна цю цифру взагалі показувати
    rate_ok: bool = False      # чи можна з неї рахувати фактичну ставку

    @property
    def apr(self) -> Optional[float]:
        """Ставка, яку їй ФАКТИЧНО платять, у перерахунку на рік.

        None — коли міряти ще нема з чого. Зокрема «за весь час» ставки не
        дає: наростаюче поле біржі накопичувалось ще до початку обліку, і
        поділити його на наші кілька діб знімків означало б назвати дурницю.
        """
        if not self.rate_ok or self.amount <= 0 or self.days < MIN_SPAN_DAYS:
            return None
        return self.earned / self.amount * YEAR_DAYS / self.days


def by_position(rows: Optional[list]) -> dict:
    """{(біржа, монета, продукт): знімки за датою} — окремий ряд на позицію."""
    out: dict = {}
    for r in rows or []:
        if _as_day(r.get("day")) is None:
            continue
        out.setdefault(position_key(r), []).append(r)
    for series in out.values():
        series.sort(key=lambda r: str(r.get("day")))
    return out


def _walk(base: dict, window: list, partial: bool) -> Period:
    """Приріст від базового знімка через усі наступні.

    Наростаюче поле дає приріст різницею, періодичне — сумою. Прапорець
    cumulative у кожного рядка свій, тому обидва випадки живуть в одному
    проході: джерело заробітку по позиції з часом може змінитись.
    """
    p = Period(partial=partial)
    if not window:
        return p            # один знімок — приросту з нього не буває

    # Остання цифра «за весь час», яку дала САМА біржа. None — такої ще не було:
    # ряд починається з того, що бот рахував сам, і прив'язатись поки нема до чого.
    anchor = float(base.get("earned") or 0.0) if base.get("cumulative") else None
    guessed = 0.0           # скільки бот дорахував після останньої цифри біржі
    start = prev_day = _as_day(base["day"])
    amounts = [float(base.get("amount") or 0.0)]
    measured = bool(base.get("measured", True))

    for row in window:
        day = _as_day(row["day"])
        value = float(row.get("earned") or 0.0)
        if row.get("cumulative"):
            if anchor is None:
                # Перша цифра біржі у проміжку. Вона накопичена ще до нього, тому
                # це лише прив'язка для наступних знімків, а не заробіток за
                # проміжок: додати її означало б показати заробіток за всі роки
                # як заробіток за тиждень.
                anchor = value
            else:
                if value < anchor:
                    # Заробіток «за весь час» просів — позицію забрали й заклали
                    # заново, відлік біржі почався з нуля. Тоді за проміжок вона
                    # заробила рівно те, що встигло набігти після перезакладу.
                    p.resets += 1
                delta = value - anchor if value >= anchor else value
                # Цифра біржі точніша за все, що бот дорахував після її минулого
                # знімка, тому свої догадки за цей час другий раз не додаємо.
                p.earned += max(delta - guessed, 0.0)
                anchor = value
            guessed = 0.0
        else:
            p.earned += value
            guessed += value
        gap = (day - prev_day).days - 1
        if gap > 0:
            p.gaps += gap       # біржа мовчала — у ряді дірка, і про це кажемо
        prev_day = day
        amounts.append(float(row.get("amount") or 0.0))
        measured = measured and bool(row.get("measured", True))

    p.days = (prev_day - start).days
    p.amount = sum(amounts) / len(amounts)
    p.measured = measured
    p.enough = p.days >= MIN_SPAN_DAYS and p.amount > 0
    p.rate_ok = p.enough
    return p


def _all_time(series: list) -> Period:
    """Заробіток за весь час.

    Наростаюче поле біржі вже містить усе, що вона заробила до початку обліку,
    тому повна картина є вже з першого знімка. Періодичне поле такого не знає —
    там лишається скласти те, що ми самі записали, і це вже «з початку обліку».

    Ряд може бути змішаний: біржа давала заробіток, потім перестала (і бот
    рахував сам), потім дала знову. Тому за основу беремо ОСТАННЮ цифру біржі,
    а все, що бот дорахував після неї, додаємо сумою — інакше цифра за весь час
    склалась би з накопиченого заробітку плюс той самий заробіток ще раз.
    """
    last = series[-1]
    p = Period(amount=float(last.get("amount") or 0.0))
    p.days = (_as_day(last["day"]) - _as_day(series[0]["day"])).days
    cut = max((i for i, r in enumerate(series) if r.get("cumulative")), default=-1)
    tail = series[cut + 1:]
    p.earned = (float(series[cut].get("earned") or 0.0) if cut >= 0 else 0.0)
    p.earned += sum(float(r.get("earned") or 0.0) for r in tail)
    p.measured = all(bool(r.get("measured", True)) for r in series[max(cut, 0):])
    if cut >= 0:
        # Цифра біржі накрила все, що було до початку обліку.
        p.enough = True
        # Ставку звідси не рахуємо: заробіток накопичувався довше, ніж наші знімки.
        p.rate_ok = False
    else:
        p.partial = True        # до першого знімка ми нічого не бачили
        # З одного знімка приросту немає взагалі, і нуль тут показувати не можна.
        p.enough = p.days >= 1
        p.rate_ok = p.days >= MIN_SPAN_DAYS and p.amount > 0
    return p


def period(series: Optional[list], days: Optional[int] = None,
           today: Optional[datetime.date] = None) -> Period:
    """Заробіток по одній позиції за проміжок. days=None — за весь час."""
    series = sorted((r for r in (series or []) if _as_day(r.get("day"))),
                    key=lambda r: str(r.get("day")))
    if not series:
        return Period()
    if days is None:
        return _all_time(series)

    start = (today or datetime.date.today()) - datetime.timedelta(days=days)
    # Базовий знімок — останній ДО початку проміжку: заробіток за проміжок це
    # приріст саме від нього. Якщо такого немає, беремо найраніший наявний і
    # чесно кажемо, що проміжок вийшов коротшим за запитаний.
    older = [r for r in series if _as_day(r["day"]) <= start]
    base = older[-1] if older else series[0]
    window = [r for r in series if _as_day(r["day"]) > _as_day(base["day"])]
    return _walk(base, window, partial=not older)


# --- уся картина разом ---

def summary(rows: Optional[list], prices: Optional[dict] = None,
            today: Optional[datetime.date] = None) -> dict:
    """Заробіток по кожній позиції й разом, у доларах.

    Долари, а не монети, свідомо: «0.00000048 BTC» їй ні про що не говорить.
    Монету без відомої ціни в підсумок не додаємо — краще недосказати, ніж
    підсунути нуль замість справжньої суми.
    """
    today = today or datetime.date.today()
    out_rows = []
    for (exchange, coin, product), series in by_position(rows).items():
        last = series[-1]
        price = last.get("price") or _price(coin, prices)
        amount = float(last.get("amount") or 0.0)
        periods = {name: period(series, days, today) for name, days in WINDOWS.items()}
        for p in periods.values():
            p.usd = p.earned * price if price else 0.0
        out_rows.append({
            "exchange": exchange,
            "coin": coin,
            "product": product,
            "amount": amount,
            "amount_usd": amount * price if price else 0.0,
            "promised_apr": last.get("apr"),
            "price": price,
            "known_price": bool(price),
            "periods": periods,
            "day": str(last.get("day") or "")[:10],
        })
    out_rows.sort(key=lambda r: -r["amount_usd"])

    days = sorted({str(r.get("day"))[:10] for r in rows or [] if _as_day(r.get("day"))})
    totals = {}
    for name in WINDOWS:
        parts = [r["periods"][name] for r in out_rows]
        totals[name] = {
            "usd": sum(p.usd for p in parts),
            "estimated": any(not p.measured for p in parts),
            "enough": any(p.enough for p in parts),
            # Хоч одна позиція ще збирає дані — значить, сума неповна, і
            # видавати її за весь заробіток не можна.
            "incomplete": any(not p.enough for p in parts),
            "partial": any(p.partial for p in parts),
            "gaps": sum(p.gaps for p in parts),
            "resets": sum(p.resets for p in parts),
            "days": max((p.days for p in parts), default=0),
            "unpriced": [r["coin"] for r in out_rows if not r["known_price"]],
        }
    return {
        "rows": out_rows,
        "totals": totals,
        "days": len(days),
        "first_day": days[0] if days else "",
        "last_day": days[-1] if days else "",
    }


def rate_gap(row: dict, window: str = "d7") -> Optional[dict]:
    """Фактична ставка проти обіцяної по одній позиції.

    Найцінніша цифра всього обліку: обіцяну ставку видно в застосунку, а
    фактичну — лише з власного заробітку. None, коли фактичну ще нема з чого
    порахувати: вигадувати її замість «даних мало» не будемо.
    """
    p = (row.get("periods") or {}).get(window)
    if p is None:
        return None
    actual = p.apr
    if actual is None:
        return None
    promised = row.get("promised_apr")
    diff = None if promised is None else actual - float(promised)
    return {
        "actual": actual,
        "promised": float(promised) if promised is not None else None,
        "diff": diff,
        "worse": diff is not None and diff <= -RATE_GAP_PP,
        "better": diff is not None and diff >= RATE_GAP_PP,
        "days": p.days,
        "measured": p.measured,
    }


def best_window(row: dict) -> str:
    """Проміжок, з якого варто показувати фактичну ставку.

    Тиждень точніший (свіжіші дані), місяць — стійкіший, коли тижня ще немає.
    """
    for name in ("d7", "d30"):
        p = (row.get("periods") or {}).get(name)
        if p is not None and p.apr is not None:
            return name
    return "d7"


def for_deposit(dep: dict, rows: Optional[list]) -> Optional[dict]:
    """Рядок обліку, що відповідає записаному в журналі вкладу.

    Звʼязок по біржі й монеті: назву продукту вона пише як їй зручно, а
    однаково журнал і біржа називають лише ці два поля. Журнал для обліку не
    потрібен — облік працює й на порожньому журналі, самими ключами; це лише
    підказка «скільки цей вклад уже приніс».
    """
    exchange = str(dep.get("exchange") or "")
    coin = str(dep.get("coin") or "").upper()
    if not exchange or not coin:
        return None
    same = [r for r in (rows or [])
            if r.get("exchange") == exchange and r.get("coin") == coin]
    if not same:
        return None
    return max(same, key=lambda r: r.get("amount_usd") or 0.0)
