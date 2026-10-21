"""Earn-пропозиції з OKX і Bybit: де можуть полежати вільні гроші.

Лише публічні endpoint-и — жодних ключів і доступу до акаунтів (як і market.py).

Головна ідея: користувачу цікаві тільки вигода і ризик, тому назовні віддаємо
не «APR», а дохід у грошах за місяць плюс зрозумілий рівень ризику.
"""
from __future__ import annotations

import asyncio
import datetime
import html
import itertools
import logging
import re
import time
from dataclasses import dataclass
from typing import Optional

import httpx

log = logging.getLogger(__name__)

OKX = "https://www.okx.com/api/v5"
BYBIT = "https://api.bybit.com/v5"

# OKX без User-Agent віддає 403.
HEADERS = {"User-Agent": "Mozilla/5.0", "accept": "application/json"}

RATES_TTL = 1800   # ставки міняються повільно — 30 хв
PROMO_TTL = 3600   # оголошення — раз на годину
# Текст оголошення після публікації вже не змінюють, тому тримаємо його довго:
# розмір бонусу й дати треба читати зі сторінки, але смикати її щоразу — марно.
PROMO_BODY_TTL = 24 * 3600
# Скільки тексту віддаємо пораднику. Далі йдуть підвал, юридичні дрібниці й
# посилання на інші акції — вони лише плутають і роздувають запит.
PROMO_BODY_CHARS = 2500

# Монети, де сума не змінюється у доларах: ризик лише в самій біржі.
STABLES = {"USDT", "USDC", "DAI", "TUSD", "FDUSD", "USDE", "PYUSD"}
# Монети, які користувач і так накопичує — відсоток тут просто бонус зверху.
CORE = {"BTC", "ETH"}
# Великі монети, які давно на ринку: ставки по них бувають цікаві, а сама монета
# навряд чи зникне за місяць — на відміну від свіжих, під які й дають 300%.
MAJORS = {
    "SOL", "XRP", "ADA", "DOT", "LINK", "LTC", "BCH", "ATOM", "AVAX",
    "DOGE", "TRX", "BNB", "XLM", "ETC", "NEAR", "UNI", "AAVE", "POL", "MATIC",
}
# За чим стежимо у сповіщеннях. Решта 400+ монет — суцільний шум щодня.
WATCHED = STABLES | CORE | MAJORS

# Скільки умовно може впасти незнайомий альткоїн за місяць — для чесного
# порівняння «заробиш проти можеш втратити». Не прогноз, а ілюстрація.
ALT_DRAWDOWN = 0.40

# Вище цієї ставки — майже завжди короткий бонус на новій монеті, а не реальна
# дохідність: тримається дні й рахується на копійчаний ліміт.
SUSPICIOUS_APR = 0.50

# Слова, за якими впізнаємо справжню акцію про дохід...
PROMO_GOOD = ("apr", "apy", "earn", "yield", "savings", "staking", "hodl", "bonus", "reward")
# ...і службові оголошення, які нам не цікаві (обслуговування, лістинги тощо).
PROMO_NOISE = (
    "suspend", "delist", "maintenance", "adjust", "upgrade", "migration",
    "halt", "resume", "rules", "notice", "will list", "announcement",
)

# Акції для інших регіонів: з Угорщини в них не зайти, тому й показувати не варто.
PROMO_REGIONS = (
    "mena", "latam", "apac", "cis", "eea only",
    "brazil", "brasil", "turkey", "türkiye", "india", "indonesia", "vietnam",
    "thailand", "philippines", "malaysia", "singapore", "nigeria", "africa",
    "argentina", "mexico", "colombia", "peru", "chile", "japan", "korea",
    "taiwan", "kazakhstan", "uzbekistan", "australia", "canada", "russia",
    "us only", "uk only",
)
# Через межі слів, інакше «cis» ловиться всередині «decision».
_REGION_RE = re.compile(r"\b(?:" + "|".join(PROMO_REGIONS) + r")\b", re.IGNORECASE)

# Рівні ризику самої монети (не пропозиції). RISK_MAJOR зʼявився тому, що без
# нього SOL, XRP і ADA потрапляли в один кошик із токеном тижневого віку, і бот
# лякав однаковим попередженням там, де ризик зовсім різний.
RISK_LOW, RISK_MID, RISK_MAJOR, RISK_HIGH = "low", "mid", "major", "high"

# Монети, ціна яких може помітно просісти: попередження про це доречне і для
# великих альтів, і для незнайомих — різниця лише в силі формулювань.
RISK_VOLATILE = (RISK_MAJOR, RISK_HIGH)

# Назви продуктів так, як вони підписані в застосунках бірж. У публічних API їх
# немає (Bybit віддає лише category й productId, OKX — саму монету зі ставкою),
# тому це єдине місце, де назви прописані руками. Якщо біржа перейменує продукт —
# правити тут.
PRODUCTS = {
    ("OKX", "flex"):      ("Simple Earn", "Заробити → Simple Earn"),
    ("OKX", "staking"):   ("ETH Staking", "Заробити → ETH Staking"),
    ("Bybit", "flex"):    ("Savings", "Earn → Savings"),
    ("Bybit", "staking"): ("On-Chain Earn", "Earn → On-Chain Earn"),
}


def classify_risk(coin: str) -> str:
    c = coin.upper()
    if c in STABLES:
        return RISK_LOW
    if c in CORE:
        return RISK_MID
    if c in MAJORS:
        return RISK_MAJOR
    return RISK_HIGH


@dataclass
class Tier:
    """Сходинка ставки: на суму від lo до hi діє свій відсоток.

    Біржі дають підвищений відсоток на перші кілька сотень доларів, тому
    середня дохідність падає, щойно сума виходить за межу сходинки.
    """
    lo: float
    hi: Optional[float]    # None — без верхньої межі
    apr: float

    def capacity(self) -> float:
        return float("inf") if self.hi is None else max(self.hi - self.lo, 0.0)


@dataclass
class Offer:
    exchange: str          # OKX / Bybit
    coin: str
    apr: float             # базова ставка (без сходинок) — для короткого показу
    kind: str              # flex — забрати будь-коли; staking — з періодом розморозки
    risk: str
    min_amount: Optional[float] = None
    redeem_days: float = 0.0
    tiers: Optional[list] = None
    product: str = ""      # як продукт зветься на біржі
    section: str = ""      # де його шукати в застосунку

    @property
    def label(self) -> str:
        """Біржа й назва продукту — щоб було зрозуміло, що саме відкривати."""
        return f"{self.exchange} {self.product}" if self.product else self.exchange

    @property
    def flexible(self) -> bool:
        return self.kind == "flex"

    @property
    def instant(self) -> bool:
        return self.redeem_days < 1

    def steps(self) -> list:
        """Сходинки ставки; якщо їх нема — одна нескінченна за базовою ставкою."""
        return self.tiers or [Tier(0.0, None, self.apr)]

    def capacity(self) -> float:
        """Скільки грошей пропозиція взагалі вміщує: сходинки бувають обмежені."""
        return sum(t.capacity() for t in self.steps())

    def per_year(self, amount: float) -> float:
        """Дохід за рік з урахуванням сходинок (а не просто amount * apr)."""
        left, total = amount, 0.0
        for t in self.steps():
            if left <= 0:
                break
            take = min(left, t.capacity())
            total += take * t.apr
            left -= take
        return total


@dataclass
class Promo:
    exchange: str
    title: str
    url: str
    ts: int

    @property
    def key(self) -> str:
        return f"{self.exchange}:{self.title}"

    @property
    def about_earn(self) -> bool:
        """Акція про відсотки/бонуси, а не службове оголошення біржі."""
        t = self.title.lower()
        if any(w in t for w in PROMO_NOISE):
            return False
        return any(w in t for w in PROMO_GOOD)

    @property
    def region_ok(self) -> bool:
        """Чи доступна акція з Угорщини — регіональні пропускаємо."""
        return not _REGION_RE.search(self.title)

    @property
    def relevant(self) -> bool:
        """Схоже на дохід за словами із заголовка — запасне правило, коли AI мовчить."""
        return self.about_earn and self.region_ok

    @property
    def candidate(self) -> bool:
        """Чи варто взагалі показувати цю акцію пораднику.

        Словами відкидаємо лише явне сміття (лістинги, техроботи, чужі регіони).
        Вирішувати, чи це справді пропозиція про дохід, має AI по ТЕКСТУ: через
        слова «Grid Trading: Earn on Every Wave» проходила лише завдяки «earn»,
        а нормальна депозитна акція без цих слів не проходила взагалі.
        """
        if is_giveaway_promo(self.title) or is_trading_promo(self.title):
            return False
        t = self.title.lower()
        return not any(w in t for w in PROMO_NOISE) and self.region_ok


# Слова, за якими видно, що бонус дають за торгівлю, а не за внесені гроші.
PROMO_TRADING = (
    "trade", "trading", "volume", "futures", "perpetual", "grid", "bot",
    "tournament", "competition", "squeeze", "leaderboard", "copy",
)
# Розіграші, лотереї, «виграй USDT» — не пасивний дохід, у сповіщення не потрібні.
PROMO_GIVEAWAY = (
    "giveaway", "lottery", "raffle", "sweepstake", "prize pool", "lucky draw",
    "lucky wheel", "spin the", " treasure hunt", "grand prize", "chance to win",
    "win up to", "share and win", "participate and win", "розіграш",
)
# Обовʼязково по МЕЖАХ СЛІВ: підрядком «bot» ловився всередині «both» і «robot»,
# і звичайна депозитна акція раптом ставала «бонусом за торгівлю». Хвіст «s?» —
# щоб «trades», «bots» і «tournaments» теж рахувались.
_TRADING_RE = re.compile(r"\b(?:" + "|".join(PROMO_TRADING) + r")s?\b", re.IGNORECASE)


# Вимога перевірити ліміт — один текст на всі гілки. «До 10% річних» на біржах
# майже завжди означає копійчаний перший шматок суми, і без цього рядка вердикт
# перетворився б на обіцянку 10% на весь її BTC.
LIMIT_WARN = "Обіцяний відсоток майже завжди діє лише на невелику суму — перевір ліміт на біржі."

# Її власне мірило: спершу сказала «50-100 доларів на торгівлю — можу подумати»,
# потім поправила поріг до $40. Нижче цієї суми торгову акцію показуємо як
# «не варто» — але суму все одно називаємо, бо рішення її, а не бота.
TRADING_BONUS_WORTH_USD = 40.0

TRADING_RISK_WARN = "На торгівлі можна втратити більше, ніж цей бонус."

# Скрипти й стилі виносимо разом із вмістом, інакше в текст потрапляє JS-код.
_SCRIPT_RE = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")


def strip_html(raw: str) -> str:
    """HTML сторінки оголошення -> суцільний текст для порадника."""
    text = _SCRIPT_RE.sub(" ", raw or "")
    text = _TAG_RE.sub(" ", text)
    return _SPACE_RE.sub(" ", html.unescape(text)).strip()[:PROMO_BODY_CHARS]


def promo_expired(ex: Optional[dict], today: Optional[datetime.date] = None) -> bool:
    """Чи акція вже закінчилась за датою з оголошення.

    Порадити акцію, яка добігла кінця тиждень тому, — гірше за мовчання: людина
    піде на біржу й нічого там не знайде. Дату беремо лише з тексту сторінки.
    """
    end = (ex or {}).get("period_end")
    if not end:
        return False
    try:
        return datetime.date.fromisoformat(str(end)[:10]) < (today or datetime.date.today())
    except (TypeError, ValueError):
        return False


def is_trading_promo(title: str) -> bool:
    """Чи це бонус за торгівлю: саме такі акції їй не підходять у принципі."""
    return bool(_TRADING_RE.search(title or ""))


def is_giveaway_promo(title: str) -> bool:
    """Розіграш / лотерея / «виграй USDT» — не те, заради чого вона тримає гроші в earn."""
    t = (title or "").lower()
    if any(w in t for w in PROMO_GIVEAWAY):
        return True
    # «Новим користувачам 2000 USDT» без deposit/savings — зазвичай реєстраційний бонус.
    if ("new user" in t or "new users" in t) and not any(
        w in t for w in ("deposit", "savings", "apr", "apy", "staking", "hodl", "hold")
    ):
        return True
    return False


def fallback_explain(promos: list[Promo]) -> dict:
    """Просте пояснення акцій із вердиктом, коли AI недоступний.

    Заголовки бірж англійською і в жаргоні, тому без пояснення вони для
    користувача безкорисні. Тут головне — сказати, чи треба торгувати і чи
    взагалі варто витрачати на це час. Правило не бачить її грошей, тому
    «worth» не роздає взагалі: максимум «можна глянути».
    """
    out = {}
    for p in promos:
        t = p.title.lower()
        if is_trading_promo(t):
            out[p.title] = {
                "kind": "trading",
                "what": "Бонус дають за торгівлю — треба зробити оборот, а не просто покласти гроші.",
                "verdict": "skip",
                "why": "Тут платять за торгівлю, а ти вкладаєш гроші надовго й не торгуєш.",
                "action": "",
                "warn": "Це не пасивний дохід: на торгівлі можна втратити більше, ніж бонус.",
            }
        elif any(w in t for w in ("deposit", "savings", "apr", "apy", "staking", "hold", "hodl")):
            out[p.title] = {
                "kind": "deposit",
                "what": "Схоже на підвищений відсоток за те, що гроші просто лежать.",
                "verdict": "maybe",
                "why": "Схоже на відсоток за гроші, що просто лежать, — але умов із заголовка не видно.",
                "action": f"Відкрий {p.exchange} → Earn і перевір максимальну суму й строк.",
                "warn": LIMIT_WARN,
            }
        else:
            out[p.title] = {
                "kind": "other",
                "what": "Умови не зрозумілі із заголовка — треба глянути на біржі.",
                "verdict": "maybe",
                "why": "Із заголовка не видно ні суми, ні відсотка — сказати щось певне не можу.",
                "action": f"Глянь умови на {p.exchange}, якщо буде час.",
                "warn": "",
            }
        # Позначка «вирішила формула»: інакше сухе правило не відрізнити від поради.
        out[p.title]["by_rule"] = True
        # Без AI релевантність визначаємо по словах — так само, як робили раніше.
        out[p.title]["is_offer"] = p.relevant
    return out


def _parse_pct(raw) -> Optional[float]:
    """'1.66%' або '0.035' -> частка на рік."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        if s.endswith("%"):
            return float(s[:-1]) / 100.0
        return float(s)
    except ValueError:
        return None


class Earn:
    def __init__(self) -> None:
        self._cache: dict = {}
        self._lock = asyncio.Lock()

    async def _get(self, url: str, params: Optional[dict] = None, timeout: int = 20):
        async with httpx.AsyncClient(timeout=timeout, headers=HEADERS) as cl:
            r = await cl.get(url, params=params or {})
            r.raise_for_status()
            return r.json()

    async def _cached(self, key: str, ttl: int, producer):
        async with self._lock:
            entry = self._cache.get(key)
            if entry and time.time() - entry["ts"] < ttl:
                return entry["data"]
        data = await producer()
        async with self._lock:
            self._cache[key] = {"ts": time.time(), "data": data}
        return data

    # --- джерела ---

    async def _okx_flexible(self) -> list[Offer]:
        j = await self._get(f"{OKX}/finance/savings/lending-rate-summary")
        out = []
        for row in j.get("data") or []:
            coin = (row.get("ccy") or "").upper()
            apr = _parse_pct(row.get("estRate") or row.get("avgRate"))
            if coin and apr is not None:
                name, section = PRODUCTS[("OKX", "flex")]
                out.append(Offer("OKX", coin, apr, "flex", classify_risk(coin),
                                 product=name, section=section))
        return out

    async def _okx_eth_staking(self) -> list[Offer]:
        j = await self._get(f"{OKX}/finance/staking-defi/eth/apy-history", {"days": "1"})
        rows = j.get("data") or []
        if not rows:
            return []
        apr = _parse_pct(rows[0].get("rate"))
        if apr is None:
            return []
        name, section = PRODUCTS[("OKX", "staking")]
        return [Offer("OKX", "ETH", apr, "staking", RISK_MID,
                      product=name, section=section)]

    async def _bybit_products(self, category: str) -> list[Offer]:
        j = await self._get(f"{BYBIT}/earn/product", {"category": category})
        kind = "flex" if category == "FlexibleSaving" else "staking"
        best: dict[str, Offer] = {}
        for row in (j.get("result") or {}).get("list") or []:
            if row.get("status") != "Available":
                continue
            coin = (row.get("coin") or "").upper()
            apr = _parse_pct(row.get("estimateApr"))
            if not coin or apr is None:
                continue
            try:
                min_amt = float(row.get("minStakeAmount") or 0) or None
            except ValueError:
                min_amt = None

            tiers = []
            for t in row.get("tierAprDetails") or []:
                t_apr = _parse_pct(t.get("estimateApr"))
                if t_apr is None:
                    continue
                try:
                    lo = float(t.get("min") or 0)
                    raw_hi = float(t.get("max") or -1)
                except ValueError:
                    continue
                tiers.append(Tier(lo, None if raw_hi < 0 else raw_hi, t_apr))
            tiers.sort(key=lambda x: x.lo)

            name, section = PRODUCTS[("Bybit", kind)]
            offer = Offer(
                "Bybit", coin, apr, kind, classify_risk(coin), min_amt,
                redeem_days=float(row.get("redeemProcessingMinute") or 0) / 1440.0,
                tiers=tiers or None, product=name, section=section,
            )
            # На одну монету буває кілька продуктів — лишаємо найвигідніший
            # (порівнюємо по ставці на перших грошах, бо саме вона зазвичай вирішує).
            if coin not in best or offer.steps()[0].apr > best[coin].steps()[0].apr:
                best[coin] = offer
        return list(best.values())

    async def _fetch_offers(self) -> list[Offer]:
        sources = (
            ("OKX flexible", self._okx_flexible()),
            ("OKX ETH-стейкінг", self._okx_eth_staking()),
            ("Bybit flexible", self._bybit_products("FlexibleSaving")),
            ("Bybit on-chain", self._bybit_products("OnChain")),
        )
        results = await asyncio.gather(*(c for _, c in sources), return_exceptions=True)
        offers: list[Offer] = []
        for (name, _), res in zip(sources, results):
            if isinstance(res, Exception):
                log.warning("Earn: джерело %s недоступне (%s)", name, res)
                continue
            offers.extend(res)
        return offers

    async def offers(self) -> list[Offer]:
        """Усі доступні пропозиції з обох бірж (кеш 30 хв)."""
        return await self._cached("offers", RATES_TTL, self._fetch_offers)

    # --- акції ---

    async def _bybit_promos(self) -> list[Promo]:
        j = await self._get(
            f"{BYBIT}/announcements/index",
            {"locale": "en-US", "limit": "20"},
        )
        out = []
        for row in (j.get("result") or {}).get("list") or []:
            title = (row.get("title") or "").strip()
            if not title:
                continue
            ts = int(row.get("dateTimestamp") or row.get("publishTime") or 0)
            out.append(Promo("Bybit", title, row.get("url") or "", ts))
        return out

    async def _okx_promos(self) -> list[Promo]:
        j = await self._get(f"{OKX}/support/announcements")
        out = []
        for block in j.get("data") or []:
            for row in block.get("details") or []:
                title = (row.get("title") or "").strip()
                if not title:
                    continue
                ts = int(row.get("pTime") or 0)
                out.append(Promo("OKX", title, row.get("url") or "", ts))
        return out

    async def _fetch_promos(self) -> list[Promo]:
        results = await asyncio.gather(
            self._bybit_promos(), self._okx_promos(), return_exceptions=True
        )
        promos: list[Promo] = []
        for res in results:
            if isinstance(res, Exception):
                log.warning("Earn: оголошення недоступні (%s)", res)
                continue
            promos.extend(res)
        # Спершу те, що стосується відсотків, далі — свіжіше.
        promos.sort(key=lambda p: (p.relevant, p.ts), reverse=True)
        return promos

    async def promos(self) -> list[Promo]:
        """Акції та бонуси з обох бірж (кеш 1 год)."""
        return await self._cached("promos", PROMO_TTL, self._fetch_promos)

    async def _fetch_body(self, url: str) -> str:
        async with httpx.AsyncClient(timeout=20, headers=HEADERS, follow_redirects=True) as cl:
            r = await cl.get(url)
            r.raise_for_status()
            return strip_html(r.text)

    async def promo_body(self, url: str) -> str:
        """Текст сторінки оголошення; порожньо, якщо не відкрилась.

        Саме тут живуть розмір бонусу, ліміти суми й дати — у заголовку їх немає.
        Але сторінка не критична: не відкрилась — працюємо як раніше, по заголовку.
        """
        if not url:
            return ""
        try:
            return await self._cached(f"body:{url}", PROMO_BODY_TTL,
                                      lambda: self._fetch_body(url))
        except Exception as e:  # noqa: BLE001
            log.warning("Earn: сторінка акції недоступна (%s): %s", url, e)
            return ""

    async def promo_bodies(self, promos: list[Promo], limit: int = 3) -> dict:
        """{ключ акції: текст сторінки} — паралельно, щоб не гальмувати перевірку."""
        sel = list(promos)[:limit]
        if not sel:
            return {}
        results = await asyncio.gather(*(self.promo_body(p.url) for p in sel),
                                       return_exceptions=True)
        return {p.key: (r if isinstance(r, str) else "")
                for p, r in zip(sel, results)}


def _best_of(offers: list[Offer], key) -> list[Offer]:
    """По одній найвигіднішій пропозиції на кожне значення ключа."""
    best: dict = {}
    for o in offers:
        k = key(o)
        if k not in best or o.apr > best[k].apr:
            best[k] = o
    return sorted(best.values(), key=lambda o: o.apr, reverse=True)


def core_per_coin(offers: list[Offer], coin: str) -> list[Offer]:
    """Пропозиції по BTC/ETH: найкраща на кожній біржі, від вигіднішої."""
    sel = [o for o in offers if o.risk == RISK_MID and o.coin == coin]
    return _best_of(sel, lambda o: (o.exchange, o.kind))


def risky_coins(offers: list[Offer], limit: int = 3) -> list[Offer]:
    """Найгучніші ставки — по одній на монету, щоб не дублювати біржі."""
    sel = [o for o in offers if o.risk == RISK_HIGH]    # великі альти сюди вже не падають
    return _best_of(sel, lambda o: o.coin)[:limit]


# З якої ставки мовчання про ліміт суми стає небезпечним. ATOM 16.9% і TRX 15.8%
# у звичайному Simple Earn виглядають як нормальна дохідність, і поки не видно,
# на яку суму вони діють, це саме та цифра, у яку легко повірити.
RATE_LIMIT_HINT_APR = 0.10


def rate_limit_note(offer: Offer) -> str:
    """Скільки грошей влазить у ставку першої сходинки — коротким хвостиком до відсотка.

    Висока ставка на копійчаному ліміті так сама себе й видає. Де біржа ліміту
    не показує (OKX), чесніше сказати про це, ніж мовчати.
    """
    cap = offer.steps()[0].capacity()
    if cap != float("inf"):
        return f"на перші ${cap:,.0f}".replace(",", " ")
    if offer.steps()[0].apr >= RATE_LIMIT_HINT_APR:
        return "ліміт суми біржа не показує — перевір у застосунку"
    return ""


def best_instant_rates(offers: list[Offer], coins: Optional[set] = None) -> dict:
    """Найкраща ставка на ПЕРШИХ грошах по кожній монеті, тільки з миттєвим виводом.

    Саме цю ставку показує /earn, тому й сповіщення мають рахувати так само —
    інакше бот писав би одні цифри, а команда показувала інші.
    """
    out: dict = {}
    for o in offers:
        if not o.instant or (coins is not None and o.coin not in coins):
            continue
        apr = o.steps()[0].apr
        if o.coin not in out or apr > out[o.coin][0]:
            out[o.coin] = (apr, o)
    return out


def rate_steps(offers: list[Offer]) -> list[tuple]:
    """Усі сходинки набору як (ставка, місткість, пропозиція) — від дорожчої."""
    slots = [(t.apr, t.capacity(), o) for o in offers for t in o.steps()]
    slots.sort(key=lambda s: s[0], reverse=True)
    return slots


def _fill(offers: list[Offer], amount: float) -> dict:
    """Жадібно заповнює найдорожчі сходинки заданого набору пропозицій."""
    slots = rate_steps(offers)

    left = amount
    used: dict = {}
    for apr, cap, offer in slots:
        if left <= 0:
            break
        take = min(left, cap)
        if take <= 0:
            continue
        key = (offer.exchange, offer.coin, offer.kind)
        if key not in used:
            if offer.min_amount and take < offer.min_amount:
                continue  # не дотягуємо до мінімуму — цю пропозицію не відкриваємо
            used[key] = {"offer": offer, "amount": 0.0, "income": 0.0}
        used[key]["amount"] += take
        used[key]["income"] += take * apr
        left -= take

    parts = sorted(used.values(), key=lambda p: -p["offer"].steps()[0].apr)
    return {"parts": parts, "income": sum(p["income"] for p in parts), "left": left}


# По скількох місцях максимум розкладаємо суму. Двох було замало: висока ставка
# на біржах діє лише на перші кількасот доларів, і на двох місцях залишок сповзав
# у найнижчу сходинку. Чотири вже морока, три — розумна межа.
MAX_PLACES = 3


def optimize(offers: list[Offer], amount: float, max_places: int = MAX_PLACES) -> dict:
    """Найвигідніший розклад суми, але не більш ніж по max_places місцях.

    Розкидати гроші по 4 біржах заради кількох доларів — не варте мороки,
    тому перебираємо всі комбінації з не більш ніж max_places пропозицій.
    """
    if amount <= 0:
        return {"parts": [], "income": 0.0, "effective_apr": 0.0}

    usable = [o for o in offers if not (o.min_amount and o.min_amount > amount)]
    if not usable:
        return {"parts": [], "income": 0.0, "effective_apr": 0.0}

    best = {"parts": [], "income": -1.0}
    for size in range(1, max(1, max_places) + 1):
        for combo in itertools.combinations(usable, size):
            res = _fill(list(combo), amount)
            if res["income"] > best["income"]:
                best = res
    return {
        "parts": best["parts"],
        "income": best["income"],
        "effective_apr": best["income"] / amount if amount else 0.0,
    }


# --- блокування коштів: коли замкнути гроші справді варте того ---

# Скільки має дати блокування ПОНАД вільний варіант, щоб його взагалі згадувати.
# Заради $3 на рік зачиняти гроші на місяць — погана угода в будь-якому разі.
LOCK_MIN_GAIN_YEAR = 20.0
# Довше за це гроші замикати не радимо: попереду може бути просадка крипти,
# і саме тоді ці гроші й знадобляться.
LOCK_MAX_DAYS = 120


def locked_options(offers: list[Offer], amount: float, coin: str = "USDT",
                   min_gain_year: float = LOCK_MIN_GAIN_YEAR) -> Optional[dict]:
    """Чи варто замкнути гроші: найкраще з блокуванням проти найкращого вільного.

    Блокування дозволене, включно з доларами, але лише коли виграш проти
    вільного варіанта справді помітний у грошах — інакше це просто втрата
    свободи задарма.
    """
    if amount <= 0:
        return None
    same = [o for o in offers if o.coin == coin.upper()
            and not (o.min_amount and o.min_amount > amount)]
    free = [o for o in same if o.instant]
    locked = [o for o in same if not o.instant and o.redeem_days <= LOCK_MAX_DAYS]
    if not free or not locked:
        return None

    best_free = max(free, key=lambda o: o.per_year(amount))
    best_lock = max(locked, key=lambda o: o.per_year(amount))
    gain = best_lock.per_year(amount) - best_free.per_year(amount)
    if gain < min_gain_year:
        return None
    return {
        "coin": coin.upper(),
        "amount": amount,
        "free": best_free,
        "locked": best_lock,
        "free_year": best_free.per_year(amount),
        "locked_year": best_lock.per_year(amount),
        "gain_year": gain,
        "days": best_lock.redeem_days,
    }


# --- незнайомі альткоїни: коли їх узагалі можна згадувати ---

# Монета має бути з першої півсотні за розміром і прожити на ринку хоча б три роки.
# Усе інше — це ставка на те, що токен узагалі доживе до кінця вкладу.
ALT_MAX_RANK = 50
ALT_MIN_AGE_YEARS = 3.0
# Поріг вигоди для чужої монети нижчий за звичайний ($20): суми тут маленькі
# за визначенням, і на $60 навіть добра ставка не дасть $20 на рік.
ALT_MIN_GAIN_YEAR = 10.0
# Скільки вона готова докинути СВОЇХ грошей понад бюджет, якщо сама захоче.
# Це додаткова опція, а не основний шлях: докидати вона не планує.
ALT_TOPUP_MAX = 100.0
# Яку частку вільного залишку бюджету взагалі можна відправити в альткоїн.
# Решта — подушка на просадку крипти, і саме докупівля на просадці має пріоритет.
ALT_BUDGET_SHARE = 0.5


def alt_coin_allowed(facts: Optional[dict]) -> bool:
    """Чи взагалі можна радити монету, якої в неї немає: топ-50 і вік від 3 років."""
    if not facts or facts.get("ambiguous"):
        return False
    rank = facts.get("rank")
    if not rank or rank > ALT_MAX_RANK:
        return False
    age = facts.get("age_years")
    if age is None:
        age = facts.get("age_years_at_least")
    return age is not None and age >= ALT_MIN_AGE_YEARS


def alt_position(free_budget: float, needed: Optional[float] = None,
                 topup: float = 0.0) -> dict:
    """Скільки грошей можна покласти в чужий альткоїн.

    Джерело — НЕ нові гроші, а незадіяний залишок місячного бюджету: те, що ще
    не куплено й не висить у заявках. Але забирати його весь не можна — це та
    сама подушка, з якої піде докупівля, коли крипта просяде.
    """
    free = max(float(free_budget or 0.0), 0.0)
    from_budget = round(free * ALT_BUDGET_SHARE, 2)
    extra = min(max(float(topup or 0.0), 0.0), ALT_TOPUP_MAX)
    total = from_budget + extra
    if needed and needed > 0:
        total = min(total, float(needed))
        from_budget = min(from_budget, total)
        extra = max(total - from_budget, 0.0)
    return {
        "amount": round(total, 2),
        "from_budget": round(from_budget, 2),
        "from_topup": round(extra, 2),
        "budget_free": round(free, 2),
        "budget_left": round(free - from_budget, 2),
    }


def alt_offers(offers: list[Offer], coin: str) -> list[Offer]:
    """Варіанти під альткоїн, де гроші НЕ застрягають.

    Свідомо лише миттєвий вивід: ці гроші — подушка на просадку крипти, і
    замикати їх у продукт із періодом розморозки не можна.
    """
    sel = [o for o in offers if o.coin == coin.upper() and o.instant]
    return sorted(sel, key=lambda o: -o.steps()[0].apr)


def smart_amounts(offers: list[Offer], extra: tuple = (), limit: int = 6) -> list[float]:
    """Суми, на яких змінюється ставка (межі сходинок), плюс кілька круглих.

    Саме на цих сумах видно, де докладати ще має сенс, а де вже ні.
    """
    bounds: set[float] = set()
    cum = 0.0
    slots = sorted(
        (t.apr, t.capacity()) for o in offers for t in o.steps()
    )[::-1]
    for _, cap in slots:
        if cap == float("inf"):
            break
        cum += cap
        if cum > 0:
            bounds.add(round(cum, 8))
    bounds.update(x for x in extra if x > 0)
    return sorted(bounds)[:limit]


def ladder(offers: list[Offer], amounts: list[float], max_places: int = MAX_PLACES) -> list[dict]:
    """Скільки дає кожна сума — щоб побачити, де докладати вже немає сенсу.

    marginal_apr — під який відсоток працюють гроші, ДОдані понад попередню сходинку.
    """
    rows = []
    prev_amount = prev_income = 0.0
    for amt in amounts:
        res = optimize(offers, amt, max_places=max_places)
        step = amt - prev_amount
        rows.append({
            "amount": amt,
            "income": res["income"],
            "effective_apr": res["effective_apr"],
            "marginal_apr": (res["income"] - prev_income) / step if step > 0 else 0.0,
            "parts": res["parts"],
        })
        prev_amount, prev_income = amt, res["income"]
    return rows
