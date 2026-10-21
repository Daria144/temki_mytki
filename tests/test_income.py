"""Облік ЗАРОБІТКУ з earn-позицій: скільки принесли гроші і яка ставка НАСПРАВДІ.

Тут стережемо дві речі, на яких увесь облік і тримається.

Перша — арифметика полів біржі. Наростаюче поле («заробила за весь час») дає
приріст РІЗНИЦЕЮ між знімками, періодичне («за добу») — СУМОЮ. Переплутати їх
означає показати заробіток за всі роки як заробіток за тиждень, і саме тому
однакові цифри тут прогоняються двома способами.

Друга — чесність, коли даних мало. З одного знімка приросту немає взагалі, а з
двох за кілька годин ставка стрибає в рази: на OKX її долари дають у позику за
живою погодинною ставкою. Тому облік мусить ВІДМОВЛЯТИСЬ рахувати ставку, а не
вигадувати її, і в тексті мусить стояти «дані збираються», а не «$0».

Мережі немає: біржі й ринок мокаємо.

Запуск: ./.venv/bin/python -m pytest tests/test_income.py -q
"""
import asyncio
import datetime
import os
import sys
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import alerts, income
from app.earn import Offer, classify_risk
from app.formatting import income_block, journal_list_text
from app.portfolio import EARN, SPOT, Holding, Portfolio
from app.status import dump_text, money_snapshot
from app.storage import KIND_FLEX, STATUS_ACTIVE, Storage

START = datetime.date(2026, 9, 1)
PRICES = {"BTC": 110_000.0, "ETH": 4_000.0}


def day(n: int) -> str:
    return (START + datetime.timedelta(days=n)).isoformat()


def _at(n: int) -> datetime.date:
    return START + datetime.timedelta(days=n)


def _row(n, earned=0.0, amount=500.0, coin="USDT", exchange="OKX",
         product="Simple Earn", apr=0.035, cumulative=True, measured=True,
         price=1.0):
    """Один денний знімок так, як його кладе в базу earn_income_check."""
    return {"day": day(n), "exchange": exchange, "coin": coin, "product": product,
            "amount": amount, "apr": apr, "earned": earned,
            "cumulative": cumulative, "measured": measured, "price": price}


def _store(tmp_path, name="income.db") -> Storage:
    st = Storage(str(tmp_path / name))
    st.init()
    return st


# --- моки бірж і ринку ---

class _FakeMarket:
    async def fetch_coin(self, coin):
        return {"coin": coin.upper(), "price": PRICES[coin.upper()], "change_24h": 0.0}

    async def fetch_fng(self):
        return 40


class _FakeEarn:
    def __init__(self, offers=()):
        self._offers = list(offers)

    async def offers(self):
        return self._offers


class _FakePortfolio:
    """Ключі бірж: або віддають залишки, або падають — обидва випадки важливі."""

    def __init__(self, holdings=(), failed=(), boom=False):
        self.holdings = list(holdings)
        self.failed = list(failed)
        self.boom = boom

    async def fetch(self, prices=None):
        if self.boom:
            raise RuntimeError("Bybit: код 10003 (ключ невірний)")
        return Portfolio(list(self.holdings), list(self.failed), time.time())


def _services(storage, portfolio=None, offers=()):
    return types.SimpleNamespace(
        storage=storage, market=_FakeMarket(), earn=_FakeEarn(offers),
        portfolio=portfolio if portfolio is not None else _FakePortfolio(),
        etf=None, coin_facts=None)


# =====================================================================
# 1. Наростаюче поле проти періодичного
# =====================================================================

def test_cumulative_field_is_read_as_a_difference():
    """OKX earnings — заробіток за весь час, тому за проміжок беремо приріст."""
    rows = [_row(0, earned=10.0), _row(1, earned=11.0), _row(2, earned=13.0)]
    p = income.period(rows, 7, today=_at(2))
    # Від 10 до 13 — це 3, а не 34 (не сума знімків).
    assert round(p.earned, 6) == 3.0


def test_periodic_field_is_read_as_a_sum():
    """Заробіток за добу треба складати: різниця таких полів — це шум, не гроші."""
    rows = [_row(0, earned=1.0, cumulative=False),
            _row(1, earned=1.0, cumulative=False),
            _row(2, earned=2.0, cumulative=False)]
    p = income.period(rows, 7, today=_at(2))
    # Базовий знімок у проміжок не входить, далі 1 + 2.
    assert round(p.earned, 6) == 3.0


def test_all_time_is_visible_from_the_very_first_snapshot():
    """Наростаюче поле накопичило все ДО обліку — історія не втрачена."""
    p = income.period([_row(0, earned=42.5)], None)
    assert round(p.earned, 2) == 42.5
    assert p.enough and p.measured
    # Ставку з цієї цифри не рахуємо: вона старша за наші знімки.
    assert p.apr is None and not p.rate_ok


def test_one_snapshot_of_a_periodic_field_is_not_zero_earnings():
    """Бот рахував сам і має лише один знімок — тут нема ЩЕ нічого, а не нуль."""
    p = income.period([_row(0, earned=0.0, cumulative=False, measured=False)], None)
    assert not p.enough      # у текст піде «дані збираються», а не «$0»


def test_exchange_number_wins_over_the_bots_own_guess():
    """Ряд змішаний: біржа замовкла, бот дорахував, біржа заговорила знову.

    Найгірша можлива помилка тут — узяти наростаючу цифру біржі за приріст і
    додати її до своїх догадок. Тоді за тиждень вийшов би заробіток за роки.
    """
    rows = [_row(0, earned=10.0),
            _row(1, earned=0.04, cumulative=False, measured=False),
            _row(2, earned=10.10)]
    p = income.period(rows, 7, today=_at(2))
    # Правда біржі — приріст 0.10; догадка бота на 0.04 лежить усередині неї,
    # і двічі її не додаємо.
    assert round(p.earned, 6) == 0.10
    assert not p.measured    # частину проміжку рахував бот — цифра приблизна


def test_all_time_does_not_double_count_a_mixed_series():
    """За основу — ОСТАННЯ цифра біржі, і лише догадки після неї додаються сумою."""
    rows = [_row(0, earned=10.0),
            _row(1, earned=12.0),
            _row(2, earned=0.05, cumulative=False, measured=False)]
    p = income.period(rows, None)
    assert round(p.earned, 6) == 12.05
    assert not p.measured


def test_a_reclosed_position_does_not_eat_the_earnings():
    """Заробіток «за весь час» просів — позицію заклали заново, відлік з нуля."""
    rows = [_row(0, earned=9.0), _row(1, earned=12.0), _row(2, earned=0.3)]
    p = income.period(rows, 7, today=_at(2))
    # 12 − 9 = 3 до перезакладу, плюс 0.3 після нього.
    assert round(p.earned, 6) == 3.3
    assert p.resets == 1


# =====================================================================
# 2. Даних мало — рахувати відмовляємось
# =====================================================================

def test_a_single_snapshot_gives_no_rate_at_all():
    p = income.period([_row(0, earned=10.0)], 7, today=START)
    assert p.earned == 0.0 and p.days == 0
    assert not p.enough and p.apr is None


def test_one_day_of_snapshots_is_still_not_enough_for_a_rate():
    """Дві доби — мінімум: погодинна ставка позик стрибає в рази."""
    rows = [_row(0, earned=10.0), _row(1, earned=10.014)]
    p = income.period(rows, 7, today=_at(1))
    assert p.days == 1 < income.MIN_SPAN_DAYS
    assert p.earned > 0          # заробіток видно
    assert p.apr is None         # а ставку — ні, і вигадувати її не будемо
    assert not p.enough


def test_the_text_says_data_is_being_collected_instead_of_zero():
    """Найважливіше правило показу: «поки не знаю» ≠ «нуль»."""
    data = income.summary([_row(0, earned=4.11)], PRICES, START)
    text = "\n".join(income_block(data))
    assert "за весь час: $4.11 (від біржі)" in text
    assert text.count("дані збираються, покажу через кілька днів") >= 2
    assert "$0" not in text
    # І чому саме збираються — теж сказано, без загадок.
    assert f"хоча б {income.MIN_SPAN_DAYS} доби знімків підряд" in text


# =====================================================================
# 3. Фактична ставка й розбіжність з обіцяною
# =====================================================================

def _real_case():
    """Її справжній випадок: OKX обіцяє 3.50% по USDT, а платить близько 1%."""
    return [_row(0, earned=10.0, apr=0.035),
            _row(4, earned=10.0 + 500 * 0.01 * 4 / 365, apr=0.035)]


def test_the_actual_rate_is_computed_from_her_own_earnings():
    p = income.period(_real_case(), 7, today=_at(4))
    assert p.days == 4 and p.enough and p.rate_ok
    assert round(p.apr, 4) == 0.01       # 1.00% на рік — стільки платять насправді


def test_the_gap_with_the_promised_rate_is_named_out_loud():
    data = income.summary(_real_case(), PRICES, _at(4))
    gap = income.rate_gap(data["rows"][0], "d7")
    assert round(gap["actual"], 4) == 0.01 and gap["promised"] == 0.035
    assert gap["worse"] and not gap["better"]

    text = "\n".join(income_block(data))
    assert "платять насправді 1.00% на рік" in text
    assert "а обіцяють 3.50%" in text
    assert "МЕНШЕ, ніж на вітрині" in text
    # Ставку рахував бот, і це підписано; сам заробіток при цьому від біржі.
    assert "це порахувала я з заробітку за 4 дні" in text


def test_a_rate_that_matches_the_promise_is_not_dressed_up_as_a_problem():
    rows = [_row(0, earned=1.0, apr=0.035),
            _row(4, earned=1.0 + 500 * 0.035 * 4 / 365, apr=0.035)]
    text = "\n".join(income_block(income.summary(rows, PRICES, _at(4))))
    assert "обіцяли 3.50% — сходиться" in text
    assert "МЕНШЕ" not in text


def test_without_a_promised_rate_there_is_nothing_to_compare_with():
    """Bybit ставки в позиції не віддає — тоді так і кажемо, а не вигадуємо."""
    rows = [_row(0, earned=1.0, apr=None, exchange="Bybit", product="Savings"),
            _row(4, earned=1.2, apr=None, exchange="Bybit", product="Savings")]
    text = "\n".join(income_block(income.summary(rows, PRICES, _at(4))))
    assert "обіцяної ставки біржа тут не показує" in text


def test_the_promised_rate_can_come_from_the_public_offers():
    offers = [Offer("Bybit", "USDT", 0.0677, "flex", classify_risk("USDT"),
                    product="Savings"),
              Offer("Bybit", "USDT", 0.02, "flex", classify_risk("USDT"),
                    product="Savings")]
    # Беремо найкращу публічну ставку — саме її вона й бачить на вітрині.
    assert income.promised_rates(offers)[("Bybit", "USDT")] == 0.0677


# =====================================================================
# 4. Пропуски в ряді: біржа мовчала
# =====================================================================

def test_a_missing_day_is_reported_as_a_gap():
    rows = [_row(0, earned=10.0), _row(1, earned=10.1), _row(3, earned=10.3)]
    p = income.period(rows, 7, today=_at(3))
    assert p.gaps == 1
    text = "\n".join(income_block(income.summary(rows, PRICES, _at(3))))
    assert "1 день біржа мовчала" in text


def test_an_unreachable_exchange_writes_a_skip_and_no_zero_row(tmp_path):
    """Знімка немає — у ряді мусить бути ДІРКА, а не нуль."""
    st = _store(tmp_path, "skip.db")
    services = _services(st, _FakePortfolio(boom=True))
    asyncio.run(alerts.earn_income_check(None, services, START))
    assert st.earn_income() == []
    assert st.earn_income_skips() == {day(0): ["ключі бірж"]}


def test_a_half_dead_exchange_keeps_what_did_answer(tmp_path):
    """OKX відповів, Bybit ні: рядок OKX пишемо, і про Bybit кажемо вголос."""
    st = _store(tmp_path, "half.db")
    holdings = [Holding("OKX", "USDT", 510.0, 510.0, EARN, 0.01, "Simple Earn",
                        earned=10.0)]
    services = _services(st, _FakePortfolio(holdings, failed=["Bybit"]))
    asyncio.run(alerts.earn_income_check(None, services, START))
    rows = st.earn_income()
    assert len(rows) == 1 and rows[0]["exchange"] == "OKX"
    assert st.earn_income_skips() == {day(0): ["Bybit"]}
    text = "\n".join(income_block(income.summary(rows, PRICES, START),
                                  st.earn_income_skips()))
    assert "Доби без знімка" in text and "Bybit" in text


def test_skips_do_not_grow_without_end(tmp_path):
    st = _store(tmp_path, "skiproll.db")
    for n in range(60):
        st.earn_income_skip(_at(n), ["Bybit"])
    assert len(st.earn_income_skips()) == 40


# =====================================================================
# 5. Монети в долари
# =====================================================================

def test_coins_are_shown_in_dollars_because_satoshis_say_nothing():
    """«0.00000048 BTC» їй ні про що не говорить — переводимо за ціною."""
    rows = [_row(0, earned=0.0004, coin="BTC", amount=0.01, apr=0.005,
                 price=110_000.0)]
    data = income.summary(rows, PRICES, START)
    assert round(data["rows"][0]["periods"]["all"].usd, 2) == 44.0
    text = "\n".join(income_block(data))
    assert "за весь час: $44.00 (від біржі)" in text
    assert "працює $1 100" in text


def test_a_coin_without_a_price_is_not_silently_counted_as_zero():
    rows = [_row(0, earned=5.0, coin="XYZ", amount=100.0, price=None)]
    data = income.summary(rows, PRICES, START)
    assert not data["rows"][0]["known_price"]
    assert data["totals"]["all"]["unpriced"] == ["XYZ"]
    text = "\n".join(income_block(data))
    assert "ціни зараз не знаю, у долари не переведу" in text
    assert "У суму не входить заробіток по XYZ" in text


def test_the_total_adds_up_the_positions_and_names_what_is_estimated():
    rows = [_row(0, earned=10.0),
            _row(0, earned=2.0, coin="ETH", amount=0.2, price=4_000.0,
                 product="ETH Staking"),
            _row(0, earned=1.0, exchange="Bybit", product="Savings", amount=200.0,
                 cumulative=False, measured=False)]
    data = income.summary(rows, PRICES, START)
    # 10 USDT + 2 ETH × $4000 + 1 USDT.
    assert round(data["totals"]["all"]["usd"], 2) == 8011.0
    assert data["totals"]["all"]["estimated"]
    text = "\n".join(income_block(data))
    assert "за весь час $8 011, частину порахувала я" in text


# =====================================================================
# 6. Денний знімок із портфеля
# =====================================================================

def test_the_snapshot_takes_the_exchange_number_as_is():
    pf = Portfolio([Holding("OKX", "USDT", 510.0, 510.0, EARN, 0.01,
                            "Simple Earn", earned=10.0),
                    Holding("Bybit", "USDT", 200.0, 200.0, SPOT)])
    rows = income.snapshot_rows(pf, PRICES, {}, START)
    assert len(rows) == 1        # вільні гроші заробітку не дають
    assert rows[0]["earned"] == 10.0 and rows[0]["cumulative"]
    assert rows[0]["measured"] and rows[0]["apr"] == 0.01


def test_without_an_exchange_number_the_bot_counts_it_itself_and_says_so():
    pf = Portfolio([Holding("Bybit", "USDT", 500.0, 500.0, EARN, None, "Savings")])
    prev = {("Bybit", "USDT", "Savings"): _row(0, exchange="Bybit",
                                               product="Savings")}
    rows = income.snapshot_rows(pf, PRICES, {("Bybit", "USDT"): 0.0365}, _at(4),
                                previous=prev)
    # 500 × 3.65% × 4/365 = 0.20; ставку взяли з публічної пропозиції.
    assert round(rows[0]["earned"], 4) == 0.2
    assert not rows[0]["measured"] and not rows[0]["cumulative"]


def test_a_top_up_between_snapshots_is_not_counted_as_a_full_period():
    """Свідомо занижуємо: беремо МЕНШУ суму, бо завищений заробіток тут гірший."""
    pf = Portfolio([Holding("Bybit", "USDT", 5000.0, 5000.0, EARN, 0.0365, "Savings")])
    prev = {("Bybit", "USDT", "Savings"): _row(0, amount=500.0, exchange="Bybit",
                                               product="Savings")}
    rows = income.snapshot_rows(pf, PRICES, {}, _at(4), previous=prev)
    assert round(rows[0]["earned"], 4) == 0.2       # нараховано на 500, не на 5000


def test_a_silent_exchange_produces_no_rows_at_all():
    assert income.snapshot_rows(Portfolio([]), PRICES, {}, START) == []
    assert income.snapshot_rows(None, PRICES, {}, START) == []


# =====================================================================
# 7. База: знімки переживають перезапуск і не дублюються
# =====================================================================

def test_snapshots_survive_a_round_trip_through_the_database(tmp_path):
    st = _store(tmp_path, "trip.db")
    st.earn_income_add([_row(0, earned=10.0),
                        _row(0, earned=1.0, exchange="Bybit", product="Savings",
                             cumulative=False, measured=False)])
    rows = st.earn_income()
    assert len(rows) == 2
    okx = next(r for r in rows if r["exchange"] == "OKX")
    # Прапорці в SQLite цілі, а весь облік працює з булевими.
    assert okx["cumulative"] is True and okx["measured"] is True
    bybit = next(r for r in rows if r["exchange"] == "Bybit")
    assert bybit["cumulative"] is False and bybit["measured"] is False


def test_a_second_run_the_same_day_refreshes_the_snapshot(tmp_path):
    st = _store(tmp_path, "twice.db")
    st.earn_income_add([_row(0, earned=10.0)])
    st.earn_income_add([_row(0, earned=10.5)])
    rows = st.earn_income()
    assert len(rows) == 1 and rows[0]["earned"] == 10.5


def test_nothing_is_written_when_there_is_nothing_to_write(tmp_path):
    st = _store(tmp_path, "empty.db")
    assert st.earn_income_add([]) == 0
    assert st.earn_income() == []


def test_the_latest_snapshot_per_position_is_the_base_for_the_next_one(tmp_path):
    st = _store(tmp_path, "latest.db")
    st.earn_income_add([_row(0, earned=10.0), _row(1, earned=10.2)])
    latest = st.earn_income_latest()
    assert latest[("OKX", "USDT", "Simple Earn")]["earned"] == 10.2


def test_the_new_table_appears_on_a_live_base_without_losing_data(tmp_path):
    """Міграція на її сервері: таблиця додається, старі дані лишаються.

    Ряд знімків накопичується роками, і відновити його заднім числом уже не
    вийде, тому втрата бази тут — не дрібниця.
    """
    path = str(tmp_path / "live.db")
    old = Storage(path)
    old.init()
    old.earn_deposit_add({"coin": "USDT", "amount": 200.0, "exchange": "Bybit",
                          "product": "Savings", "apr": 0.0677, "kind": KIND_FLEX},
                         STATUS_ACTIVE)
    old.earn_income_add([_row(0, earned=10.0)])
    # Стара база без таблиці заробітку — саме таку бот і зустріне на сервері.
    with old._conn() as c:
        c.execute("DROP TABLE earn_income")

    fresh = Storage(path)
    fresh.init()
    assert fresh.earn_income() == []             # таблиця створена наново
    assert len(fresh.earn_deposits()) == 1       # журнал не постраждав
    fresh.earn_income_add([_row(0, earned=10.0)])
    assert len(fresh.earn_income()) == 1


# =====================================================================
# 8. Показ у боті
# =====================================================================

def test_the_accounting_works_on_an_empty_journal():
    """Журнал вона може не вести взагалі — заробіток мусить бути видно з ключів."""
    data = income.summary([_row(0, earned=10.0), _row(4, earned=10.06)], PRICES,
                          _at(4))
    text = journal_list_text([], PRICES, data, {})
    assert "Журнал вкладів порожній" in text
    assert "Скільки твої вклади вже заробили" in text
    assert "за весь час: $10.06 (від біржі)" in text
    assert "платять насправді" in text


def test_the_journal_shows_what_a_deposit_really_pays(tmp_path):
    """У журналі ставка, під яку вона заходила; поруч — та, яку платять."""
    st = _store(tmp_path, "jrn.db")
    st.earn_deposit_add({"coin": "USDT", "amount": 500.0, "exchange": "OKX",
                         "product": "Simple Earn", "apr": 0.035, "kind": KIND_FLEX},
                        STATUS_ACTIVE)
    data = income.summary(_real_case(), PRICES, _at(4))
    text = journal_list_text(st.earn_deposits(), PRICES, data, {})
    assert "фактично ці гроші приносять 1.00% на рік, а заходила ти під 3.50%" in text
    # Копіювальний блок лишається в самому кінці — вона його натискає.
    assert text.rstrip().endswith("\x02")


def test_a_deposit_the_accounting_knows_nothing_about_gets_no_invented_line(tmp_path):
    st = _store(tmp_path, "jrn2.db")
    st.earn_deposit_add({"coin": "DOGE", "amount": 100.0, "exchange": "Bybit",
                         "product": "Savings", "apr": 0.12, "kind": KIND_FLEX},
                        STATUS_ACTIVE)
    text = journal_list_text(st.earn_deposits(), PRICES,
                             income.summary(_real_case(), PRICES, _at(4)), {})
    assert "фактично ці гроші приносять" not in text


def test_without_a_single_snapshot_the_block_is_simply_absent():
    """Нема знімків — нема й блоку: порожня таблиця не має вигадувати рядків."""
    assert income_block(income.summary([], PRICES, START)) == []
    assert income_block(None) == []
    assert "вже заробили" not in journal_list_text([], PRICES, None, {})


def test_the_history_length_is_stated_so_the_numbers_can_be_judged():
    data = income.summary([_row(0, earned=10.0), _row(2, earned=10.1)], PRICES,
                          _at(2))
    text = "\n".join(income_block(data))
    assert f"Знімків заробітку в мене 2 (з {START:%d.%m}" in text


def test_status_shows_the_earnings_and_the_real_rate(tmp_path):
    """Дамп /status читатиме чужа AI — заробіток мусить бути в ньому теж."""
    st = _store(tmp_path, "st.db")
    st.earn_income_add(_real_case())
    holdings = [Holding("OKX", "USDT", 510.5, 510.5, EARN, 0.01, "Simple Earn",
                        earned=10.05)]
    snap = asyncio.run(money_snapshot(_services(st, _FakePortfolio(holdings)), _at(4)))
    text = dump_text(snap)
    assert "СКІЛЬКИ МОЇ ВКЛАДИ ВЖЕ ЗАРОБИЛИ (і яка ставка НАСПРАВДІ)" in text
    assert "платять насправді 1.00% на рік" in text and "а обіцяють 3.50%" in text
    # Звідки цифра — підписано, і в межах дампа теж сказано, що це не запит до біржі.
    assert "«від біржі» означає точну цифру самої біржі" in text
    assert "це історія з денних знімків бота" in text


def test_status_says_plainly_that_the_accounting_has_not_started_yet(tmp_path):
    """Знімків ще немає — так і пишемо, без порожньої таблиці й без нулів."""
    snap = asyncio.run(money_snapshot(_services(_store(tmp_path, "st0.db")), START))
    text = dump_text(snap)
    assert "бот не зняв жодного денного знімка" in text
    assert "Знімків заробітку в мене" not in text
