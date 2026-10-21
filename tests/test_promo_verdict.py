"""Вердикт по біржових акціях: читання сторінки, цифри бонусу, протерміноване.

Фікстури — скорочені справжні сторінки Bybit від 5 вересня 2026. Мережу не
чіпаємо: порадника мокаємо, сторінки беремо з рядків нижче.

Запуск: ./.venv/bin/python -m pytest tests/test_promo_verdict.py
"""
import asyncio
import datetime
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import service
from app.config import config
from app.earn import Earn, Promo, TRADING_BONUS_WORTH_USD, promo_expired, strip_html
from app.formatting import NO_ADVISOR_NOTE, promo_lines

TODAY = datetime.date(2026, 9, 5)

GRID_TITLE = "Grid Trading: Earn on Every Wave"
GRID_URL = ("https://announcements.bybit.com/en-US/article/"
            "grid-trading-earn-on-every-wave--artbb6086cb8dd6/")
GRID_HTML = """
<html><head><style>.hdr{color:red}</style><script>var tracking=1;</script></head>
<body><h1>Grid Trading: Earn on Every Wave</h1>
<p>Campaign Period: August 28 &ndash; September 14, 2026</p>
<p>Promotion 1: First Grid Bot Reward &mdash; $5 Bot Loss Cover Voucher (First 5,000 Users)</p>
<p>Create your first Spot or Futures Grid Bot with an investment of at least $200
and keep it running for 3 days.</p>
<p>Promotion 2: Lucky Draw &mdash; complete the daily task of $3,000 grid trading
volume for a chance to win up to $1,000.</p>
</body></html>
"""

BTC_TITLE = "🐂 Bull Incoming? HODL BTC & Enjoy 10% APR with Bybit Earn!"
BTC_URL = ("https://announcements.bybit.com/en-US/article/"
           "bull-incoming-hodl-btc-enjoy-10-apr-with-bybit-earn--art5c62060158b0/")
BTC_HTML = """
<html><head><script>window.__DATA__={"a":1};</script></head>
<body><h1>Bull Incoming? HODL BTC &amp; Enjoy 10% APR with Bybit Earn!</h1>
<p>Event Period: Aug 21, 2026, 12 PM UTC &ndash; Aug 28, 2026, 12 PM UTC</p>
<p>Enjoy a guaranteed 10% APR with the 3-Day BTC Fixed Savings Plan.</p>
<p>Stake between 0.001 BTC (minimum) and 1 BTC (maximum).</p>
</body></html>
"""

EXPLORE_TITLE = "Explore 5 new assets for Margin Trading, Easy Earn, and Institutional Loans"

# Її реальні гроші й поточні ставки на день зняття даних.
AMOUNTS = {"BTC": 500.0, "USDT": 1200.0}
RATES = {"BTC": 0.005, "USDT": 0.0677, "ETH": 0.02054}


def _promo(title, url=""):
    return Promo("Bybit", title, url, 0)


def _fake_ai(answers):
    """Мок порадника: {заголовок: поля відповіді}. Мережі тут немає взагалі."""
    async def explain(titles, context=None, bodies=None):
        return {t: dict(answers[t]) for t in titles if t in answers}
    return explain


def _advise(monkeypatch, promos, answers, bodies=None):
    monkeypatch.setattr(service, "explain_promos", _fake_ai(answers))
    return asyncio.run(service.promo_advice(promos, AMOUNTS, 1200.0, RATES, bodies or {}))


# --- А. читання сторінки оголошення ---

def test_strip_html_pulls_the_numbers_out_of_the_grid_page():
    """Розмір бонусу й умова живуть у тексті сторінки, а не в заголовку."""
    text = strip_html(GRID_HTML)
    assert "$5 Bot Loss Cover Voucher" in text
    assert "at least $200" in text
    assert "August 28 – September 14, 2026" in text
    # Скрипти й стилі не мають потрапити в текст для порадника.
    assert "tracking" not in text and "color:red" not in text and "<" not in text


def test_strip_html_pulls_dates_and_limits_out_of_the_btc_page():
    text = strip_html(BTC_HTML)
    assert "Aug 28, 2026" in text
    assert "0.001 BTC (minimum) and 1 BTC (maximum)" in text
    assert "3-Day BTC Fixed Savings Plan" in text
    assert "window.__DATA__" not in text


def test_body_is_cached_and_missing_page_degrades_softly():
    """Сторінка не відкрилась — працюємо далі по заголовку, а не падаємо."""
    earn = Earn()
    calls = []

    async def ok(url):
        calls.append(url)
        return strip_html(GRID_HTML)

    earn._fetch_body = ok
    assert "$5 Bot Loss Cover Voucher" in asyncio.run(earn.promo_body(GRID_URL))
    asyncio.run(earn.promo_body(GRID_URL))
    assert len(calls) == 1          # друга спроба взялася з кешу

    async def boom(url):
        raise RuntimeError("503")

    broken = Earn()
    broken._fetch_body = boom
    assert asyncio.run(broken.promo_body(BTC_URL)) == ""
    assert asyncio.run(broken.promo_bodies([_promo(BTC_TITLE, BTC_URL)])) == {
        "Bybit:" + BTC_TITLE: ""
    }


# --- B/D. цифри бонусу і вердикт ---

GRID_AI = {
    GRID_TITLE: {
        "title_uk": "Заробляй на сітковій торгівлі",
        "what": "Дають ваучер за створення торгового бота.",
        "kind": "trading",
        "verdict": "maybe",          # порадник помʼякшив — код має виправити
        "why": "Невеликий бонус за спробу сіткової торгівлі.",
        "bonus_usd": 5,
        "bonus_what": "ваучер на покриття збитку, і то лише першим 5000 охочих",
        "requirements": "створити бота на $200 і торгувати 3 дні",
        "period_end": "2026-09-14",
        "coin": "",
        "locked_days": 3,
    }
}


def test_grid_promo_names_the_bonus_and_says_no(monkeypatch):
    """Відмовки замало: вона хоче бачити суму й умову, щоб вирішити самій."""
    p = _promo(GRID_TITLE, GRID_URL)
    ex = _advise(monkeypatch, [p], GRID_AI)[GRID_TITLE]
    assert ex["verdict"] == "skip"

    text = "\n".join(promo_lines([p], {GRID_TITLE: ex}, TODAY))
    assert "$5" in text and "$200" in text
    assert "ваучер на покриття збитку" in text
    assert "Не варто:" in text


def test_requirements_read_as_one_sentence(monkeypatch):
    """«За це треба Зареєструватися…» виглядає як помилка бота, а не як речення."""
    ai = {GRID_TITLE: dict(GRID_AI[GRID_TITLE],
                           requirements="Зареєструватися і створити бота на $200")}
    p = _promo(GRID_TITLE, GRID_URL)
    ex = _advise(monkeypatch, [p], ai)[GRID_TITLE]
    text = "\n".join(promo_lines([p], {GRID_TITLE: ex}, TODAY))
    assert "За це треба зареєструватися і створити бота на $200." in text


def test_trading_bonus_above_her_threshold_becomes_maybe(monkeypatch):
    """Її слова: «бонус 50-100 доларів на торгівлю — можу подумати»."""
    title = "Trade & win: $75 bonus for new futures users"
    ai = {title: {
        "title_uk": "Бонус $75 новачкам у торгівлі",
        "what": "Дають бонус за перші угоди.",
        "kind": "trading",
        "verdict": "skip",
        "why": "Це бонус за торгівлю.",
        "bonus_usd": 75,
        "bonus_what": "бонус на рахунок",
        "requirements": "зробити перші угоди",
    }}
    p = _promo(title)
    ex = _advise(monkeypatch, [p], ai)[title]
    assert 75 >= TRADING_BONUS_WORTH_USD
    assert ex["verdict"] == "maybe"
    text = "\n".join(promo_lines([p], {title: ex}, TODAY))
    assert "$75" in text
    assert "втратити більше" in text


def test_unnamed_bonus_is_admitted_not_invented(monkeypatch):
    """Немає суми в тексті — так і кажемо, а не вигадуємо цифру."""
    title = "Trading festival: rewards for active traders"
    ai = {title: {
        "title_uk": "Фестиваль торгівлі",
        "what": "Нагороди активним трейдерам.",
        "kind": "trading",
        "verdict": "skip",
        "why": "",
        "bonus_usd": None,
        "bonus_what": "",
        "requirements": "",
    }}
    p = _promo(title)
    ex = _advise(monkeypatch, [p], ai)[title]
    text = "\n".join(promo_lines([p], {title: ex}, TODAY))
    assert "Розмір бонусу в оголошенні не вказаний" in text
    assert "$" not in text.split("\n", 1)[1]      # жодної вигаданої суми


def test_new_assets_promo_is_never_worth(monkeypatch):
    """«Explore 5 new assets… Margin Trading…» — це не про пасивні гроші."""
    ai = {EXPLORE_TITLE: {
        "title_uk": "5 нових монет у маржинальній торгівлі",
        "what": "Додали нові монети для торгівлі й Easy Earn.",
        "kind": "other",
        "verdict": "worth",
        "why": "Нові можливості.",
    }}
    ex = _advise(monkeypatch, [_promo(EXPLORE_TITLE)], ai)[EXPLORE_TITLE]
    assert ex["verdict"] != "worth"


# --- C. протерміновані акції ---

BTC_AI_FIELDS = {
    "title_uk": "Тримай BTC під 10% річних",
    "what": "Кладеш BTC на 3 дні під підвищений відсоток.",
    "kind": "deposit",
    "verdict": "worth",
    "why": "Ставка помітно вища за твою поточну.",
    "action": "Bybit → Earn → BTC",
    "coin": "BTC",
    "apr_pct": 10,
    "min_amount": 0.001,
    "max_amount": 1,
    "locked_days": 3,
    "bonus_usd": None,
    "bonus_what": "підвищена ставка 10% річних",
}


def test_expired_promo_is_dropped(monkeypatch):
    """Справжній баг: BTC-акція закінчилась 28 серпня, а бот радив би її 5 вересня."""
    ex = dict(BTC_AI_FIELDS, period_end="2026-08-28")
    p = _promo(BTC_TITLE, BTC_URL)
    advice = _advise(monkeypatch, [p], {BTC_TITLE: ex})
    assert promo_expired(advice[BTC_TITLE], TODAY)
    assert promo_lines([p], advice, TODAY) == []


def test_expired_promo_never_reaches_the_notification(monkeypatch):
    """Наскрізь: у сповіщення протермінована акція не потрапляє взагалі."""
    p = _promo(BTC_TITLE, BTC_URL)
    services = _services([p])
    monkeypatch.setattr(service, "explain_promos",
                        _fake_ai({BTC_TITLE: dict(BTC_AI_FIELDS, period_end="2026-08-28")}))
    from app.alerts import _promo_news
    lines, done, watch, _ = asyncio.run(_promo_news(services, AMOUNTS, 1200.0, RATES, TODAY))
    assert lines == [] and watch == {}
    assert p.key in done        # опрацьовано — більше не крутитиметься


def test_active_btc_promo_shows_money_percent_and_limits(monkeypatch):
    """Та сама акція з живою датою: гроші, відсотки, ліміти й блокування."""
    p = _promo(BTC_TITLE, BTC_URL)
    services = _services([p])
    monkeypatch.setattr(service, "explain_promos",
                        _fake_ai({BTC_TITLE: dict(BTC_AI_FIELDS, period_end="2026-09-20")}))
    from app.alerts import _promo_news
    lines, done, watch, _ = asyncio.run(_promo_news(services, AMOUNTS, 1200.0, RATES, TODAY))
    text = "\n".join(lines)
    assert watch[p.key]["end"] == "2026-09-20"      # запамʼятали для нагадування

    assert "✅" in text and "Тримай BTC під 10% річних" in text
    assert "0.50%" in text and "10%" in text            # і у відсотках...
    # ...і в грошах — ДВІ цифри: за фактичні 3 дні акції та в перерахунку на рік.
    # Сама лише річна цифра на триденній акції обіцяла б $50 замість 41 цента.
    assert "$0.41" in text and "$50.00/рік" in text
    assert "За 3 дні" in text and "але стільки вона не триває" in text
    assert "від 0.001 до 1 BTC" in text
    assert "блокуються на 3 дні" in text
    assert "Діє до 20.09" in text
    # Ліміт названий у тексті — розмите «перевір ліміт» тут уже зайве.
    assert "перевір ліміт" not in text


def test_hedged_promo_without_known_limit_demands_checking(monkeypatch):
    """«up to 10%» і жодного ліміту в тексті — не вище «можна глянути» + вимога перевірити."""
    title = "Earn up to 10% APR on BTC with Bybit Earn"
    ai = {title: dict(BTC_AI_FIELDS, max_amount=None, min_amount=None,
                      period_end="2026-09-20", verdict="worth")}
    p = _promo(title)
    ex = _advise(monkeypatch, [p], ai)[title]
    assert ex["verdict"] == "maybe"
    text = "\n".join(promo_lines([p], {title: ex}, TODAY))
    assert "перевір ліміт" in text
    assert "🤔" in text


# --- деградація без порадника ---

class _Store:
    def __init__(self, seen):
        self.promos = set(seen)
        self.mutes = set()

    def seen_promos(self):
        return set(self.promos)

    def mark_promos_seen(self, keys):
        self.promos |= set(keys)

    def earn_muted(self):
        return set(self.mutes)


class _Earn:
    def __init__(self, promos):
        self._promos = promos

    async def promos(self):
        return self._promos

    async def promo_bodies(self, promos, limit=3):
        return {p.key: strip_html(BTC_HTML if "HODL" in p.title else GRID_HTML)
                for p in promos[:limit]}


def _services(promos):
    # Памʼять уже не порожня, інакше перший запуск лише запамʼятовує й мовчить.
    return types.SimpleNamespace(earn=_Earn(promos), storage=_Store({"Bybit:старе"}))


def test_rule_path_works_without_ai(monkeypatch):
    """Ключа немає — вердикт усе одно є, просто обережніший."""
    monkeypatch.setattr(config, "gemini_api_key", "")
    p = _promo(GRID_TITLE, GRID_URL)
    advice = asyncio.run(service.promo_advice([p], AMOUNTS, 1200.0, RATES))
    assert advice[GRID_TITLE]["verdict"] == "skip"
    assert advice[GRID_TITLE]["by_rule"] is True
    text = "\n".join(promo_lines([p], advice, TODAY))
    assert "❌" in text
    # Без ключа порадник вимкнено свідомо — вибачатись нема за що.
    assert NO_ADVISOR_NOTE not in text


def test_broken_ai_falls_back_and_signs_the_decision(monkeypatch):
    """AI кинув виняток: працюємо на правилі й чесно це підписуємо."""
    async def boom(titles, context=None, bodies=None):
        raise RuntimeError("429 quota")

    monkeypatch.setattr(config, "gemini_api_key", "test-key")
    monkeypatch.setattr(service, "explain_promos", boom)
    p = _promo(BTC_TITLE, BTC_URL)
    advice = asyncio.run(service.promo_advice([p], AMOUNTS, 1200.0, RATES))
    assert advice[BTC_TITLE]["verdict"] in ("maybe", "skip")
    text = "\n".join(promo_lines([p], advice, TODAY))
    assert NO_ADVISOR_NOTE in text
    assert "перевір ліміт" in text     # ліміту не знаємо — вимагаємо перевірити


def test_verdict_survives_message_assembly(monkeypatch):
    """Вердикт має дожити до Telegram, а не загубитись у складанні блоків."""
    from app.alerts import _compose
    p = _promo(GRID_TITLE, GRID_URL)
    ex = _advise(monkeypatch, [p], GRID_AI)
    text = "\n".join(_compose([("🎁 Нові акції:", promo_lines([p], ex, TODAY))]))
    assert "🎁 Нові акції:" in text
    assert "❌ Bybit: «Заробляй на сітковій торгівлі»" in text
    assert "$5" in text and "$200" in text
