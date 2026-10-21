"""Перевірки виправлень, знайдених розбором серпня 2026.

Кожен тест прив'язаний до конкретної помилки процесу, а не до вдалого/невдалого
результату: облік заявок, глибина драбинки, виявлення спрацювань, видимість збою
порадника та однакові правила безпеки в обох режимах.

Запуск: ./.venv/bin/python -m pytest tests/test_august_fixes.py -q
"""
import asyncio
import datetime
import os
import sys
import tempfile
import time
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import advisor, alerts, budget, engine, formatting
from app.alerts import _good_moment_eligible, _good_moment_prefix, _min_low_since
from app.config import config
from app.engine import MAX_DEPTH_PCT, CoinPlan, Order, Recommendation
from app.storage import Storage

DAY_MS = 86_400_000


def _tmp_storage() -> Storage:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    st = Storage(path)
    st.init()
    return st


def _add_order(st: Storage, coin: str, price: float, notional: float,
               status: str, created_at: int, filled_at=None, idx=[0]):
    idx[0] += 1
    with st._conn() as c:
        c.execute(
            "INSERT INTO orders(rec_id, order_index, coin, price, qty, notional, status, "
            "created_at, filled_at) VALUES(0, ?, ?, ?, ?, ?, ?, ?, ?)",
            (idx[0], coin, price, notional / price, notional, status, created_at, filled_at),
        )


def _this_month_ts() -> int:
    today = datetime.date.today()
    return int(datetime.datetime(today.year, today.month, 1).timestamp()) + 3600


def _falling_market(coin: str, last: float):
    """Довге падіння: дешева зона, щоб формула дала moment=buy і збудувала драбинку."""
    closes = [last * 2 - (last / 250) * i for i in range(250)]
    closes[-1] = last
    return {
        "coin": coin,
        "price": last,
        "change_24h": -0.02,
        "closes": closes,
        "ts": [i * DAY_MS for i in range(250)],
        "lows": None,
        "weekly_closes": [],
    }


# --- 1. Заявки більше не рахуються як витрачені гроші ---

def test_reserved_is_not_counted_as_deployed():
    st = _tmp_storage()
    start = _this_month_ts()
    month = budget.current_month()
    _add_order(st, "BTC", 60000, 113.74, "filled", start, filled_at=start + 100)
    _add_order(st, "BTC", 55000, 150.0, "open", start)
    _add_order(st, "ETH", 1800, 93.0, "open", start)

    assert st.deployed_in_month(month) == 113.74
    assert st.reserved_in_month(month) == 243.0


def test_budget_pace_follows_real_purchases():
    """Серпневий випадок: $455 бюджету, $113.74 куплено, $243 висить у заявках."""
    st = _tmp_storage()
    start = _this_month_ts()
    st.set_budget(budget.current_month(), 455.0)
    _add_order(st, "BTC", 60000, 113.74, "filled", start, filled_at=start + 100)
    _add_order(st, "BTC", 55000, 243.0, "open", start)

    stt = budget.status(st)
    assert stt["deployed"] == 113.74
    assert stt["reserved"] == 243.0
    # Раніше залишок був би $98 — бот вважав заявки витратою і сам себе притискав.
    assert stt["remaining"] == round(455.0 - 113.74, 2)
    assert stt["behind"] == round(max(stt["target"] - 113.74, 0.0), 2)


def test_rollover_carries_money_stuck_in_orders():
    """Заявки, що висіли на кінець місяця, грошей не витратили — вони мають перенестись."""
    st = _tmp_storage()
    prev = "2026-08"
    aug = int(datetime.datetime(2026, 8, 10).timestamp())
    st.set_budget(prev, 455.0)
    _add_order(st, "BTC", 60000, 113.74, "filled", aug, filled_at=aug + 100)
    _add_order(st, "BTC", 55000, 243.0, "cancelled", aug)

    leftover = budget.rollover_if_needed(st)
    assert leftover == round(455.0 - 113.74, 2)


# --- 2. Драбинка не тікає нижче 8% від нинішньої ціни ---

def test_ladder_never_goes_deeper_than_cap():
    price = 63000.0
    orders = [
        Order("BTC", price * 0.97, 1, 40),      # -3%
        Order("BTC", price * 0.90, 1, 40),      # -10%, задалеко
        Order("BTC", price * 0.87, 1, 40),      # -13%, серпнева історія
    ]
    kept, note = engine.guard_orders("BTC", price, orders)
    assert [round(o.price) for o in kept] == [round(price * 0.97)]
    assert note == ""
    assert MAX_DEPTH_PCT == 8.0


def test_old_orders_cannot_push_new_one_into_the_basement():
    """Найдовша серпнева хиба: старі заявки тягли нову все глибше від ринку."""
    price = 63000.0
    # Уже висить лімітка на 56 878 — «глибше за неї» вимагало б ~ -11% від ринку.
    open_by_coin = {"BTC": {"count": 1, "notional": 25.0, "min_price": 56877.92,
                            "max_price": 58816.94}}
    orders = [Order("BTC", 54944.4, 1, 41.52)]
    kept, note = engine.guard_orders("BTC", price, orders, open_by_coin)
    assert kept == []
    # Заявку не вигадуємо силоміць, але й не мовчимо.
    assert "BTC" in note and "нинішньої ціни" in note


def test_blocked_note_reaches_the_message():
    price = 63000.0
    open_by_coin = {"BTC": {"count": 1, "notional": 25.0, "min_price": 56877.92,
                            "max_price": 58816.94}}
    data = {"BTC": _falling_market("BTC", price)}
    rec = engine.analyze(data, fng=20, amount=300.0, open_by_coin=open_by_coin)
    text = formatting.format_recommendation(rec)
    assert "нинішньої ціни" in text


def test_recommendation_order_price_and_notional_are_tap_to_copy():
    """Ціну й суму ордера можна тапнути й скопіювати — без «$» і без пробілів у числі.

    Інакше їх треба було передруковувати вручну в поле лімітного ордера на біржі.
    """
    cp = CoinPlan(coin="BTC", price=67123.456, score=1.0, zone="buy", mayer=0.9,
                 orders=[Order(coin="BTC", price=67123.456, qty=0.0044, notional=295.0)])
    rec = Recommendation(moment="buy", headline="Гарний момент", amount=300.0, fng=20,
                         coins=[cp])
    text = formatting.format_recommendation(rec)

    # Мітки копіювання присутні й охоплюють саме числа, без «$» і без пробілів.
    assert formatting.has_quote(text)
    html_text = formatting.to_html(text)
    assert "<code>67123</code>" in html_text
    assert "<code>295</code>" in html_text
    # Поза <code> лишається звичний текст із «$» для читабельності.
    assert "по $<code>" in html_text and "(<code>295</code>$)" in html_text


# --- 3. Спрацювання шукаємо по денних мінімумах, включно з днем створення ---

def _day_data(lows, closes, first_day_ts=0):
    n = len(lows)
    ts = [first_day_ts + i * DAY_MS for i in range(n)]
    return {"coin": "BTC", "price": closes[-1], "change_24h": 0.0,
            "closes": closes, "ts": ts, "lows": lows, "low_ts": ts}


def test_intraday_low_detects_fill_that_closes_would_miss():
    # Ціна закриття весь час 63 000, але вдень провалювалась до 62 894.
    d = _day_data([62894.0, 63500.0], [63000.0, 63400.0])
    assert _min_low_since(d, 0) == 62894.0
    # По закриттях цю саму заявку не видно — саме так серпень і губив покупки.
    only_closes = {"coin": "BTC", "price": 63400.0, "change_24h": 0.0,
                   "closes": [63000.0, 63400.0], "ts": [0, DAY_MS], "lows": None}
    assert _min_low_since(only_closes, 0) == 63000.0


def test_creation_day_is_included():
    """Заявка, яку ринок зачепив того ж дня, теж має бути помічена."""
    created = 12 * 3600          # заявка виставлена опівдні першого дня
    d = _day_data([62894.0, 64000.0], [63500.0, 64200.0])
    assert _min_low_since(d, created) == 62894.0
    # Наступний день сам по собі мінімуму вже не дає.
    assert _min_low_since(d, created + DAY_MS // 1000) == 64000.0


def test_falls_back_to_closes_without_lows():
    d = {"coin": "BTC", "price": 100.0, "change_24h": 0.0,
         "closes": [100.0, 96.0], "ts": [0, DAY_MS], "lows": None, "low_ts": None}
    assert _min_low_since(d, 0) == 96.0
    # Криві дані (довжини не збігаються) не мають валити перевірку.
    broken = dict(d, lows=[95.0], low_ts=[0, DAY_MS])
    assert _min_low_since(broken, 0) == 96.0


# --- 4. Збій порадника видно: підпис у повідомленні + сповіщення ---

def test_formula_decision_is_signed():
    data = {"BTC": _falling_market("BTC", 63000.0)}
    rec = engine.analyze(data, fng=20, amount=300.0)
    assert rec.source == "formula"

    old_key = config.gemini_api_key
    try:
        config.gemini_api_key = "test-key"
        assert formatting.NO_ADVISOR_NOTE in formatting.format_recommendation(rec)
        rec.source = "ai"
        assert formatting.NO_ADVISOR_NOTE not in formatting.format_recommendation(rec)
        # Без ключа порадник вимкнений свідомо — вибачатись нема за що.
        rec.source = "formula"
        config.gemini_api_key = ""
        assert formatting.NO_ADVISOR_NOTE not in formatting.format_recommendation(rec)
    finally:
        config.gemini_api_key = old_key


class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        self.sent.append(text)


class _Services:
    def __init__(self, storage):
        self.storage = storage


def test_advisor_silence_is_reported_once():
    advisor.reset_health()
    st = _tmp_storage()
    bot, services = _Bot(), _Services(st)
    old_owner = config.owner_id
    try:
        config.owner_id = 1
        # Щойно відмовив — це ще нормальна життєва ситуація, не турбуємо.
        advisor._note_failure()
        asyncio.run(alerts._advisor_health_alert(bot, services))
        assert bot.sent == []

        # Мовчить довше кількох годин — тепер це вже поломка, про яку треба знати.
        advisor._failing_since = time.time() - (alerts.AI_SILENT_HOURS + 1) * 3600
        asyncio.run(alerts._advisor_health_alert(bot, services))
        assert len(bot.sent) == 1 and "порадник" in bot.sent[0]

        # І не щогодини.
        asyncio.run(alerts._advisor_health_alert(bot, services))
        assert len(bot.sent) == 1
    finally:
        config.owner_id = old_owner
        advisor.reset_health()


# --- 5. Запасна модель справді задіяна ---

def test_working_model_is_tried_first_next_time():
    advisor.reset_health()
    try:
        first = advisor._models()
        assert first[0] == config.gemini_model
        # Основна перевантажена, відповіла запасна — далі починаємо саме з неї.
        advisor._note_success("gemini-2.5-flash")
        assert advisor._models()[0] == "gemini-2.5-flash"
        assert advisor.health()["ok"] is True
        assert len(advisor._models()) == len(set(advisor._models()))
    finally:
        advisor.reset_health()


def test_timeout_is_generous_enough_for_thinking():
    assert advisor.AI_TIMEOUT >= 60


# --- 6. Однакові правила безпеки в обох режимах ---

def _open_btc(min_price: float):
    return {"BTC": {"count": 2, "notional": 51.0, "min_price": min_price,
                    "max_price": min_price * 1.03}}


def test_duplicate_protection_works_in_formula_mode():
    """9 серпня формула поставила заявки поверх уже відкритих — тепер так не можна."""
    price = 63000.0
    data = {"BTC": _falling_market("BTC", price)}
    open_by_coin = _open_btc(62000.0)
    rec = engine.analyze(data, fng=20, amount=300.0, open_by_coin=open_by_coin)
    ceiling = 62000.0 * (1 - engine.DUP_GAP_PCT / 100.0)
    for cp in rec.coins:
        for o in cp.orders:
            assert o.price < ceiling, (o.price, ceiling)


def test_duplicate_protection_works_in_ai_mode():
    price = 63000.0
    data = {"BTC": _falling_market("BTC", price)}
    decision = {
        "moment": "buy", "headline": "тест", "deploy_now_pct": 100,
        "coins": [{"coin": "BTC", "weight_pct": 100, "ladder_drops_pct": [1.0, 2.0]}],
        "reasoning": "",
    }
    rec = engine.build_from_ai(data, 20, 300.0, decision, _open_btc(62000.0))
    ceiling = 62000.0 * (1 - engine.DUP_GAP_PCT / 100.0)
    for cp in rec.coins:
        for o in cp.orders:
            assert o.price < ceiling, (o.price, ceiling)


def _rec(moment: str, with_orders: bool) -> Recommendation:
    orders = [Order("BTC", 60000.0, 0.001, 60.0)] if with_orders else []
    return Recommendation(
        moment=moment,
        headline="тест",
        amount=60.0,
        fng=25,
        coins=[CoinPlan("BTC", 63000.0, 80.0, "cheap", 1.2, orders=orders)],
    )


def test_good_moment_gate_accumulate_with_orders():
    assert _good_moment_eligible(_rec("accumulate", True))
    assert _good_moment_prefix("accumulate") is not None
    assert "Помірна просадка" in _good_moment_prefix("accumulate")
    assert _good_moment_eligible(_rec("accumulate", False)) is False
    assert _good_moment_eligible(_rec("wait", False)) is False
    assert _good_moment_eligible(_rec("buy", True))


async def _run_market_check_good_moment(rec: Recommendation, bot, services):
    data = {"BTC": _falling_market("BTC", 63000.0)}
    with patch.object(alerts, "gather", AsyncMock(return_value=(data, 25))), patch.object(
        alerts, "analyze_market", AsyncMock(return_value=rec)
    ), patch.object(alerts, "_quiet_now", return_value=False), patch.object(
        alerts, "_greed_or_take_profit", AsyncMock()
    ), patch.object(
        alerts, "_advisor_health_alert", AsyncMock()
    ):
        await alerts.market_check(bot, services)


def test_market_check_notifies_on_accumulate_with_orders():
    st = _tmp_storage()
    bot, services = _Bot(), _Services(st)
    old_owner = config.owner_id
    try:
        config.owner_id = 1
        rec = _rec("accumulate", True)
        asyncio.run(_run_market_check_good_moment(rec, bot, services))
        assert len(bot.sent) == 1
        assert "Помірна просадка" in bot.sent[0]
        assert st.alert_recent("good_moment", alerts.DAY * 2)
    finally:
        config.owner_id = old_owner


def test_market_check_skips_accumulate_without_orders():
    st = _tmp_storage()
    bot, services = _Bot(), _Services(st)
    old_owner = config.owner_id
    try:
        config.owner_id = 1
        asyncio.run(_run_market_check_good_moment(_rec("accumulate", False), bot, services))
        assert bot.sent == []
    finally:
        config.owner_id = old_owner


def test_both_modes_respect_min_order_and_budget_cap():
    price = 100.0
    orders = [Order("BTC", 99.0, 1, 40.0), Order("BTC", 98.0, 1, 40.0)]
    # Дрібниця нижче min_order не проходить у жодному режимі.
    tiny = [Order("BTC", 99.0, 1, config.min_order - 1)]
    assert engine.guard_orders("BTC", price, tiny)[0] == []
    # Сума ордерів не перевищує виділену на раунд порцію.
    kept, _ = engine.guard_orders("BTC", price, orders, budget_cap=50.0)
    assert sum(o.notional for o in kept) <= 50.0
