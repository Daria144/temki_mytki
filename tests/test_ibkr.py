"""Локальні перевірки щомісячної купівлі ETF на IBKR — без мережі й без бота.

Запуск: ./.venv/bin/python -m pytest tests/test_ibkr.py  (або просто як скрипт).
"""
import asyncio
import datetime
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import ibkr
from app.storage import Storage


def _tmp_storage() -> Storage:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    st = Storage(path)
    st.init()
    return st


class _FakePrices:
    """Двійник EtfPrices: віддає задану ціну або падає — без жодного запиту."""

    def __init__(self, prices=None, boom=False):
        self.prices = prices or {}
        self.boom = boom

    async def price_of(self, fund):
        if self.boom:
            raise RuntimeError("тест: джерело ціни впало")
        return self.prices.get(fund.ticker)


# --- календар фондів ---

def test_calendar_only_august_is_cema():
    for month in range(1, 13):
        fund = ibkr.fund_for_month(datetime.date(2027, month, 15))
        expected = "CEMA" if month == 8 else "WEBN"
        assert fund.ticker == expected, (month, fund.ticker)
    # Одинадцять місяців WEBN і один CEMA — звідси й очікувані ~10,5% у CEMA.
    picks = [ibkr.fund_for_month(datetime.date(2027, m, 1)).ticker for m in range(1, 13)]
    assert picks.count("WEBN") == 11 and picks.count("CEMA") == 1

    # Лютий — тепер звичайний місяць WEBN, а не CEMA.
    assert ibkr.fund_for_month(datetime.date(2027, 2, 15)).ticker == "WEBN"
    assert ibkr.fund_for_month_key("2027-02").ticker == "WEBN"
    assert not ibkr.is_annual_cema_month(datetime.date(2027, 2, 1))

    # Серпень — той єдиний місяць CEMA на рік.
    assert ibkr.is_annual_cema_month(datetime.date(2027, 8, 1))
    assert ibkr.CEMA_MONTHS == (8,)

    # Той самий календар працює і від ключа місяця (кнопки знають лише 'YYYY-MM').
    assert ibkr.fund_for_month_key("2027-08").ticker == "CEMA"
    assert ibkr.fund_for_month_key("2026-09").ticker == "WEBN"
    assert ibkr.fund_for_month_key("дурня") is None
    print("календар OK:", picks)


def test_funds_carry_correct_isins():
    # ISIN — єдине, за чим шукаємо фонд, тому перевіряємо його як константу.
    assert ibkr.FUNDS["WEBN"].isin == "IE0003XJA0J9"
    assert ibkr.FUNDS["CEMA"].isin == "IE00B5L8K969"
    assert ibkr.FUNDS["WEBN"].target_pct == 85.0
    assert ibkr.FUNDS["CEMA"].target_pct == 15.0
    # На Xetra потрібний нам фонд EM Asia торгується під CEBL, а не CEMA.
    assert ibkr.FUNDS["CEMA"].xetra == "CEBL" and ibkr.FUNDS["CEMA"].yahoo == "CEBL.DE"
    assert sum(f.target_pct for f in ibkr.FUNDS.values()) == 100.0
    print("ISIN OK:", {t: f.isin for t, f in ibkr.FUNDS.items()})


# --- перший робочий день ---

def test_first_business_day_for_each_weekday():
    # 1-ше число — субота: беремо понеділок 3-го (серпень 2026: 1.08 — субота).
    assert ibkr.first_business_day(2026, 8) == datetime.date(2026, 8, 3)
    # 1-ше число — неділя: понеділок 2-го (листопад 2026: 1.11 — неділя).
    assert ibkr.first_business_day(2026, 11) == datetime.date(2026, 11, 2)
    # 1-ше число — робочий день: беремо саме його.
    assert ibkr.first_business_day(2026, 9) == datetime.date(2026, 9, 1)   # вівторок
    assert ibkr.first_business_day(2026, 10) == datetime.date(2026, 10, 1)  # четвер
    assert ibkr.first_business_day(2026, 12) == datetime.date(2026, 12, 1)  # вівторок
    assert ibkr.first_business_day(2027, 1) == datetime.date(2027, 1, 1)    # пʼятниця
    # Ще пара субот/неділь у інших роках — щоб не залежати від одного року.
    assert ibkr.first_business_day(2027, 5) == datetime.date(2027, 5, 3)   # 1.05 — субота
    assert ibkr.first_business_day(2028, 10) == datetime.date(2028, 10, 2)  # 1.10 — неділя

    # Результат ніколи не пізніше 3-го числа і завжди пн–пт.
    for year in (2026, 2027, 2028, 2029):
        for month in range(1, 13):
            day = ibkr.first_business_day(year, month)
            assert day.month == month and day.year == year
            assert day.weekday() < 5, day
            assert day.day <= 3, day
    assert ibkr.is_first_business_day(datetime.date(2026, 8, 3))
    assert not ibkr.is_first_business_day(datetime.date(2026, 8, 1))
    print("перший робочий день OK")


# --- скільки паїв виходить ---

def test_shares_for_amount():
    # WEBN на реальній ціні: €200 / €12.82 = 15 цілих паїв.
    r = ibkr.shares_for(200.0, 12.82)
    assert r["units"] == 15 and r["short"] is None
    assert abs(r["spent"] - 192.30) < 1e-6
    assert abs(r["leftover"] - 7.70) < 1e-6

    # Пай дорожчий за внесок — цілого паю не виходить, і видно, скільки не хапає.
    r = ibkr.shares_for(200.0, 259.05)
    assert r["units"] == 0 and abs(r["short"] - 59.05) < 1e-6
    assert r["spent"] == 0.0 and abs(r["leftover"] - 200.0) < 1e-6

    # Рівно вкладається — без залишку.
    r = ibkr.shares_for(200.0, 100.0)
    assert r["units"] == 2 and r["leftover"] == 0.0 and r["short"] is None

    # Ціни немає — жодних вигадок.
    for bad in (None, 0.0, -5.0):
        r = ibkr.shares_for(200.0, bad)
        assert r == {"units": None, "spent": None, "leftover": None, "short": None}
    print("паї OK")


def test_one_share_for_annual_cema_purchase():
    """У серпні сума — це рівно ціна одного паю, а не €200."""
    r = ibkr.one_share_for(259.05)
    assert r["units"] == 1
    assert r["amount"] == 259.05          # сума місяця = ціна паю
    assert r["spent"] == 259.05
    assert abs(r["extra"] - 59.05) < 1e-6  # стільки треба докласти поверх €200
    assert r["leftover"] == 0.0 and r["short"] is None

    # Ціна плаває — докладання рахується від неї, нічого не зафіксовано константою.
    assert abs(ibkr.one_share_for(300.0)["extra"] - 100.0) < 1e-6
    assert abs(ibkr.one_share_for(230.40)["extra"] - 30.40) < 1e-6
    assert ibkr.one_share_for(300.0)["amount"] == 300.0

    # Якщо пай колись подешевіє нижче внеску — докладати нічого, буде решта.
    cheap = ibkr.one_share_for(180.0)
    assert cheap["units"] == 1 and cheap["extra"] == 0.0
    assert cheap["amount"] == 180.0 and abs(cheap["leftover"] - 20.0) < 1e-6

    # Рівно €200 — теж без докладання й без решти.
    exact = ibkr.one_share_for(200.0)
    assert exact["extra"] == 0.0 and exact["leftover"] == 0.0

    # Інший базовий внесок враховується.
    assert abs(ibkr.one_share_for(259.05, base=250.0)["extra"] - 9.05) < 1e-6

    # Ціни немає — суму не вигадуємо, лишається звичайний внесок як орієнтир.
    for bad in (None, 0.0, -1.0):
        r = ibkr.one_share_for(bad)
        assert r["units"] is None and r["extra"] is None
        assert r["amount"] == ibkr.DEFAULT_AMOUNT
    print("один пай на рік OK")


def test_planned_amount_per_month():
    # Звичайний місяць — завжди €200, ціна ні на що не впливає.
    assert ibkr.planned_amount(ibkr.WEBN, 12.82) == 200.0
    assert ibkr.planned_amount(ibkr.WEBN, None) == 200.0
    # Місяць CEMA — рівно ціна паю.
    assert ibkr.planned_amount(ibkr.CEMA, 259.05) == 259.05
    assert ibkr.planned_amount(ibkr.CEMA, 300.0) == 300.0
    # Без ціни падаємо назад на звичайний внесок.
    assert ibkr.planned_amount(ibkr.CEMA, None) == 200.0
    print("сума місяця OK")


def test_expected_cema_share_from_new_calendar():
    """11 внесків WEBN по €200 + один пай CEMA на рік -> близько 10,5%."""
    # 259.05 / (11*200 + 259.05) = 10.5%
    assert ibkr.expected_cema_pct(259.05) == 10.5
    # На нинішніх цінах частка помітно нижча за цільові 15% — це й прийнято свідомо.
    assert ibkr.expected_cema_pct(259.05) < 15.0
    assert ibkr.expected_cema_pct(300.0) == 12.0

    # Частка рахується від живої ціни, а не з константи.
    assert ibkr.expected_cema_pct(259.05) != ibkr.expected_cema_pct(280.0)
    # Інший базовий внесок теж враховується.
    assert ibkr.expected_cema_pct(259.05, base=100.0) > ibkr.expected_cema_pct(259.05)

    # Два паї на рік (тобто два місяці CEMA замість WEBN) дали б понад 20% —
    # саме тому й купуємо один: рівно 15% між цими двома варіантами не вміщається.
    two_shares = 259.05 * 2 / (10 * 200 + 259.05 * 2) * 100
    assert two_shares > 20.0
    assert ibkr.expected_cema_pct(259.05) < 15.0 < two_shares

    # Без ціни числа не вигадуємо.
    for bad in (None, 0.0, -5.0):
        assert ibkr.expected_cema_pct(bad) is None
    print("очікувана частка CEMA OK:", ibkr.expected_cema_pct(259.05), "%",
          "| два паї дали б", round(two_shares, 1), "%")


# --- журнал і статуси ---

def test_journal_statuses():
    st = _tmp_storage()
    assert st.ibkr_get_month("2026-09") is None
    assert st.ibkr_history() == [] and st.ibkr_totals() == {}

    rec = st.ibkr_ensure_month("2026-09", "WEBN", 200.0, price=12.82)
    assert rec["status"] == "reminded" and rec["fund"] == "WEBN"
    assert rec["amount"] == 200.0 and rec["price"] == 12.82
    assert rec["units"] is None and rec["done_at"] is None

    # Повторний виклик нічого не переписує — нагадування може прийти двічі.
    again = st.ibkr_ensure_month("2026-09", "WEBN", 999.0)
    assert again["amount"] == 200.0 and again["status"] == "reminded"

    # Зроблено без кількості паїв — кількість необовʼязкова.
    done = st.ibkr_mark_done("2026-09")
    assert done["status"] == "done" and done["units"] is None and done["done_at"]

    # Паї можна вписати вже після «Зроблено», статус не міняється.
    upd = st.ibkr_set_units("2026-09", 15)
    assert upd["units"] == 15.0 and upd["status"] == "done"

    # Пропущений місяць.
    st.ibkr_ensure_month("2026-10", "WEBN", 200.0)
    sk = st.ibkr_mark_skipped("2026-10")
    assert sk["status"] == "skipped"

    # Історія — найновіші зверху, з обмеженням.
    st.ibkr_ensure_month("2026-11", "WEBN", 200.0)
    hist = st.ibkr_history()
    assert [r["month"] for r in hist] == ["2026-11", "2026-10", "2026-09"]
    assert [r["month"] for r in st.ibkr_history(limit=2)] == ["2026-11", "2026-10"]

    # У підсумки йдуть ЛИШЕ зроблені внески.
    tot = st.ibkr_totals()
    assert set(tot) == {"WEBN"} and tot["WEBN"]["months"] == 1
    assert tot["WEBN"]["invested"] == 200.0 and tot["WEBN"]["units"] == 15.0

    # reset() прибирає й журнал ETF.
    st.reset()
    assert st.ibkr_get_month("2026-09") is None and st.ibkr_totals() == {}
    print("журнал OK")


def test_journal_marks_are_noop_for_unknown_month():
    st = _tmp_storage()
    assert st.ibkr_mark_done("2030-01") is None
    assert st.ibkr_mark_skipped("2030-01") is None
    assert st.ibkr_set_units("2030-01", 5) is None
    print("невідомий місяць OK")


# --- фактичні частки проти цільових ---

def test_actual_shares_vs_target():
    st = _tmp_storage()

    # Без жодного внеску лишається тільки стартова купівля WEBN на €400.
    tot = ibkr.totals(st)
    assert tot["total"] == 400.0 and tot["months"] == 0
    assert tot["by_fund"]["WEBN"]["actual_pct"] == 100.0
    assert tot["by_fund"]["WEBN"]["units"] == 29.0
    assert tot["by_fund"]["CEMA"]["invested"] == 0.0

    # Рік внесків строго за новим календарем: 11 місяців WEBN по €200
    # плюс серпень — рівно один пай CEMA за €259.05.
    cema_price = 259.05
    for month in range(1, 13):
        key = f"2027-{month:02d}"
        fund = ibkr.fund_for_month(datetime.date(2027, month, 1))
        st.ibkr_ensure_month(key, fund.ticker,
                             ibkr.planned_amount(fund, cema_price))
        st.ibkr_mark_done(key, units=1 if fund.ticker == "CEMA" else 15)

    # Без стартової купівлі видно чистий ефект нового календаря: CEMA ~10,5%.
    pure = ibkr.totals(st, include_start=False)
    assert pure["months"] == 12
    assert abs(pure["total"] - (11 * 200 + cema_price)) < 0.01
    assert pure["by_fund"]["CEMA"]["invested"] == cema_price
    assert pure["by_fund"]["CEMA"]["units"] == 1.0          # один пай на рік
    assert pure["by_fund"]["WEBN"]["invested"] == 2200.0    # 11 місяців по €200
    assert pure["by_fund"]["CEMA"]["actual_pct"] == 10.5
    assert pure["by_fund"]["WEBN"]["actual_pct"] == 89.5
    assert pure["by_fund"]["CEMA"]["target_pct"] == 15.0
    # Підсумок збігається з окремим розрахунком очікуваної частки.
    assert pure["by_fund"]["CEMA"]["actual_pct"] == ibkr.expected_cema_pct(cema_price)

    # Зі стартовою купівлею WEBN на €400 частка CEMA ще менша.
    full = ibkr.totals(st)
    assert abs(full["total"] - (11 * 200 + cema_price + 400)) < 0.01
    assert full["by_fund"]["CEMA"]["actual_pct"] == 9.1
    assert full["by_fund"]["WEBN"]["actual_pct"] == 90.9
    assert full["by_fund"]["WEBN"]["invested"] == 2600.0   # 2200 внесків + 400 старт

    # Пропущений місяць у підсумки не потрапляє.
    before = ibkr.totals(st)["total"]
    st.ibkr_ensure_month("2028-01", "WEBN", 200.0)
    st.ibkr_mark_skipped("2028-01")
    assert ibkr.totals(st)["total"] == before
    print("частки OK: CEMA", pure["by_fund"]["CEMA"]["actual_pct"], "%")


# --- глайд-пат ---

def test_glide_path_stages():
    start = datetime.date(2026, 9, 1)

    # Роки 0–8 — тільки акції.
    for years in range(0, 10):
        stage = ibkr.current_stage(years)
        assert stage.shares == {"WEBN": 85.0, "CEMA": 15.0}, years
        assert "AGGH" not in stage.shares

    # Рік 10 — зʼявляються облігації.
    for years in (10, 11, 12):
        assert ibkr.current_stage(years).shares == {"WEBN": 75.0, "CEMA": 10.0, "AGGH": 15.0}
    # Рік 13 і далі — облігацій більше.
    for years in (13, 20, 40):
        assert ibkr.current_stage(years).shares == {"WEBN": 60.0, "CEMA": 10.0, "AGGH": 30.0}

    # Кожен етап — це рівно 100%.
    for stage in ibkr.GLIDE_PATH:
        assert abs(sum(stage.shares.values()) - 100.0) < 1e-9, stage

    # Останній етап — «цільова дата» без року: сам він не настає.
    assert ibkr.GLIDE_PATH[-1].year is None
    assert ibkr.GLIDE_PATH[-1].shares == {"WEBN": 55.0, "CEMA": 5.0, "AGGH": 40.0}

    # Наступний етап рахується від поточного року.
    assert ibkr.next_stage(0).year == 10
    assert ibkr.next_stage(9).year == 10
    assert ibkr.next_stage(10).year == 13
    assert ibkr.next_stage(13) is None

    # Повні роки: до дня старту рік ще не зарахований.
    assert ibkr.years_elapsed(start, datetime.date(2026, 9, 1)) == 0
    assert ibkr.years_elapsed(start, datetime.date(2027, 8, 31)) == 0
    assert ibkr.years_elapsed(start, datetime.date(2027, 9, 1)) == 1
    assert ibkr.years_elapsed(start, datetime.date(2036, 9, 1)) == 10
    assert ibkr.years_elapsed(start, datetime.date(2020, 1, 1)) == 0  # без відʼємних
    print("глайд-пат OK")


def test_glide_path_warning_window():
    start = datetime.date(2026, 9, 1)

    # Далеко до етапу — не турбуємо.
    assert ibkr.stage_warning(start, datetime.date(2027, 1, 1)) is None
    assert ibkr.stage_warning(start, datetime.date(2036, 5, 1)) is None

    # За два і за один місяць до року 10 — попереджаємо.
    for today, left in ((datetime.date(2036, 7, 10), 2), (datetime.date(2036, 8, 10), 1)):
        warn = ibkr.stage_warning(start, today)
        assert warn is not None and warn["months_left"] == left
        assert warn["stage"].year == 10
        assert warn["current"].shares == {"WEBN": 85.0, "CEMA": 15.0}
        assert warn["when"] == datetime.date(2036, 9, 1)
        assert "AGGH" in ibkr.stage_warning_text(warn)
        assert "продавати не треба" in ibkr.stage_warning_text(warn)

    # Місяць настання — уже не «заздалегідь».
    assert ibkr.stage_warning(start, datetime.date(2036, 9, 1)) is None
    # Так само перед роком 13.
    warn13 = ibkr.stage_warning(start, datetime.date(2039, 8, 1))
    assert warn13 is not None and warn13["stage"].year == 13
    # Після року 13 етапів із роком більше немає.
    assert ibkr.stage_warning(start, datetime.date(2040, 1, 1)) is None
    print("попередження про глайд-пат OK")


def test_strategy_start_is_remembered():
    st = _tmp_storage()
    first = ibkr.strategy_start(st)
    assert first == datetime.date.today()
    # Другий виклик бере збережене, а не «сьогодні» знову.
    st.set_setting("ibkr_start_date", "2026-09-01")
    assert ibkr.strategy_start(st) == datetime.date(2026, 9, 1)
    # Зіпсоване значення не валить бота — перезаписуємо на сьогодні.
    st.set_setting("ibkr_start_date", "не дата")
    assert ibkr.strategy_start(st) == datetime.date.today()
    assert st.get_setting("ibkr_start_date") == datetime.date.today().isoformat()
    print("старт стратегії OK")


# --- тексти ---

def test_reminder_text_has_everything_important():
    plan = ibkr.month_plan(datetime.date(2026, 9, 1), price=12.82)
    text = ibkr.reminder_text(plan)

    assert "IE0003XJA0J9" in text          # ISIN мусить бути помітно
    assert "шукай саме за цим номером" in text
    assert "Amundi Prime All Country World UCITS ETF Acc" in text
    assert "€200" in text and "€12.82" in text
    assert "15 паїв на €192.30" in text    # скільки виходить на внесок і на яку суму
    assert "€7.70" in text                 # решта лишається на рахунку
    assert "market order" in text and "Xetra" in text
    assert "після 14:00" in text           # друга половина дня по Франкфурту
    assert "Один ордер, один фонд" in text
    assert "Ніколи нічого не продаємо" in text
    assert "accumulating" in text
    # Новий календар: CEMA раз на рік у серпні, лютий — звичайний місяць WEBN.
    assert "CEMA купуємо раз на рік, у серпні" in text
    # Лютий як місяць CEMA згадуватись не має ніде («валюта» теж містить «лют»,
    # тому шукаємо саме назву місяця).
    for word in ("лютий", "лютого", "лютому"):
        assert word not in text.lower(), word

    # Повторне нагадування — та сама суть, інший початок.
    repeat = ibkr.reminder_text(plan, repeat=True)
    assert repeat.startswith("🔔 Нагадую")
    assert "IE0003XJA0J9" in repeat and "€12.82" in repeat
    print("текст нагадування OK:", len(text), "символів")


def test_reminder_text_survives_missing_price():
    """Ціни немає — нагадування все одно повне й зрозуміле, просто без паїв."""
    plan = ibkr.month_plan(datetime.date(2026, 9, 1), price=None)
    text = ibkr.reminder_text(plan)

    assert "IE0003XJA0J9" in text and "€200" in text
    assert "Ціну паю зараз не вдалось отримати" in text
    assert "market order" in text and "Ніколи нічого не продаємо" in text
    # Жодних «None» чи «?» у тексті — це те, що побачить людина.
    assert "None" not in text and "?" not in text
    print("текст без ціни OK")


def test_february_is_now_an_ordinary_webn_month():
    """Лютий більше не місяць CEMA — це звичайний внесок €200 у WEBN."""
    plan = ibkr.month_plan(datetime.date(2027, 2, 1), price=12.82)
    assert plan["fund"].ticker == "WEBN"
    assert plan["single_share"] is False
    assert plan["amount"] == 200.0 and plan["units"] == 15

    text = ibkr.reminder_text(plan)
    assert "IE0003XJA0J9" in text and "IE00B5L8K969" not in text
    assert "15 паїв" in text
    # Жодного слова про докладання чи про CEMA як фонд цього місяця.
    assert "докласти" not in text and "рівно 1 пай" not in text
    assert "Цього місяця купуємо WEBN" in text
    print("лютий — звичайний WEBN OK")


def test_august_text_instructs_to_top_up_for_exactly_one_share():
    """Серпень: рішення вже ухвалене, тому це інструкція, а не вибір варіантів."""
    plan = ibkr.month_plan(datetime.date(2027, 8, 1), price=259.05)
    assert plan["fund"].ticker == "CEMA"
    assert plan["single_share"] is True
    assert plan["units"] == 1 and plan["amount"] == 259.05
    assert abs(plan["extra"] - 59.05) < 1e-6

    text = ibkr.reminder_text(plan)
    assert "IE00B5L8K969" in text
    assert "Купуємо рівно 1 пай CEMA" in text
    assert "€259.05" in text                       # сума цього разу
    assert "€59.05" in text                        # скільки докласти
    assert "треба докласти" in text
    assert "CEMA купуємо раз на рік" in text
    # Старе формулювання-вибір мусило зникнути.
    assert "Варіанти" not in text
    assert "наступного внеску" not in text
    assert "перенес" not in text.lower()
    assert "не виходить навіть одного паю" not in text
    print("текст серпня OK")


def test_august_text_survives_missing_price():
    """Ціни немає — кажемо, що потрібен один пай, і просимо глянути суму на біржі."""
    plan = ibkr.month_plan(datetime.date(2027, 8, 1), price=None)
    assert plan["fund"].ticker == "CEMA" and plan["single_share"] is True
    assert plan["units"] is None and plan["extra"] is None
    assert plan["amount"] == 200.0        # орієнтир, поки ціни немає

    text = ibkr.reminder_text(plan)
    assert "IE00B5L8K969" in text
    assert "Купуємо рівно 1 пай CEMA" in text
    assert "Ціну паю зараз не вдалось отримати" in text
    assert "Глянь ціну на біржі" in text
    assert "market order" in text and "Ніколи нічого не продаємо" in text
    assert "None" not in text and "?" not in text
    print("текст серпня без ціни OK")


def test_status_text_shows_status_history_and_shares():
    st = _tmp_storage()
    start = datetime.date(2026, 9, 1)
    st.ibkr_ensure_month("2026-09", "WEBN", 200.0, price=12.82)
    st.ibkr_mark_done("2026-09", units=15)

    today = datetime.date(2026, 10, 1)
    plan = ibkr.month_plan(today, price=12.90)
    text = ibkr.status_text(
        plan, st.ibkr_get_month("2026-10"), st.ibkr_history(6),
        ibkr.totals(st), start, today,
    )
    assert "жовтень 2026" in text
    assert "⬜ ще не починали" in text            # на жовтень запису ще немає
    assert "2026-09 · WEBN · €200 ✅ (15 паїв)" in text
    assert "цільова 85%" in text and "цільова 15%" in text
    # Пояснення нової очікуваної частки CEMA (без живої ціни — діапазоном).
    assert "один пай CEMA раз на рік, у серпні" in text
    assert "близько 10–11%" in text
    assert "нічого виправляти не треба" in text and "не пропускати внески" in text
    assert "16,7%" not in text                    # стара цифра мусила зникнути
    assert "рік 0" in text and "Наступна зміна — рік 10" in text
    assert "01.10.2026" in text                   # перший робочий день місяця
    assert "None" not in text
    # Число внесків — по-людськи, без «внесків(и)».
    assert "1 внесок" in text and "внесків(и)" not in text

    # Той самий місяць уже зроблений — статус і паї видно.
    st.ibkr_ensure_month("2026-10", "WEBN", 200.0)
    st.ibkr_mark_done("2026-10", units=15)
    done = ibkr.status_text(
        plan, st.ibkr_get_month("2026-10"), st.ibkr_history(6),
        ibkr.totals(st), start, today,
    )
    assert "✅ зроблено" in done and "Куплено: 15 паїв" in done
    assert "€800" in done                          # 400 старт + 2×200
    assert "2 внески" in done
    # Стартові 29 паїв WEBN видно в підсумках і в частках.
    assert "29 паїв WEBN" in done and "59 паїв" in done   # 29 старт + 15 + 15
    print("текст /ibkr OK")


def test_units_are_declined_properly():
    # Найчастіша причина «74 паїв» у тексті — саме відсутність цієї функції.
    assert ibkr.units_word(1) == "1 пай"
    assert ibkr.units_word(2) == "2 паї"
    assert ibkr.units_word(4) == "4 паї"
    assert ibkr.units_word(5) == "5 паїв"
    assert ibkr.units_word(11) == "11 паїв"
    assert ibkr.units_word(14) == "14 паїв"
    assert ibkr.units_word(15) == "15 паїв"
    assert ibkr.units_word(21) == "21 пай"
    assert ibkr.units_word(29) == "29 паїв"
    assert ibkr.units_word(74) == "74 паї"
    assert ibkr.units_word(112) == "112 паїв"
    assert ibkr.units_word(15.5) == "15.5 паю"
    assert ibkr.units_word(None) == "? паїв"
    print("відмінки паїв OK")


# --- ціни: кеш, перевірки й мʼяка деградація ---

def test_price_source_validates_isin_and_currency():
    prices = ibkr.EtfPrices()
    calls = {"n": 0}

    async def fake_get(url, params, timeout=15):
        calls["n"] += 1
        raise RuntimeError("тест: мережі немає")

    prices._get = fake_get
    # Обидва джерела недоступні, кешу немає -> None, а не виняток.
    assert asyncio.run(prices.fetch(ibkr.WEBN)) is None
    assert calls["n"] == 2          # спробували і Xetra, і Yahoo

    # Свіжа ціна в кеші — джерела навіть не питаємо.
    prices._set_cache(ibkr.WEBN.isin, {"isin": ibkr.WEBN.isin, "price": 12.82,
                                       "currency": "EUR", "source": "тест", "as_of": None})
    calls["n"] = 0
    assert asyncio.run(prices.fetch(ibkr.WEBN))["price"] == 12.82
    assert calls["n"] == 0
    assert asyncio.run(prices.price_of(ibkr.WEBN)) == 12.82
    print("кеш і збої джерел OK")


def test_price_parsing_of_both_sources():
    prices = ibkr.EtfPrices()

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    # Xetra: відповідь ключована ISIN, тому чужий ISIN мусить бути відкинутий.
    async def xetra_ok(url, params, timeout=15):
        return _Resp({"isin": params["isin"], "lastPrice": 259.05,
                      "timestampLastPrice": "2026-09-01T12:06:07Z"})

    prices._get = xetra_ok
    data = asyncio.run(prices._fetch_boerse(ibkr.CEMA))
    assert data["price"] == 259.05 and data["currency"] == "EUR"
    assert data["isin"] == "IE00B5L8K969" and "Xetra" in data["source"]

    async def xetra_wrong_isin(url, params, timeout=15):
        return _Resp({"isin": "IE00OTHER0000", "lastPrice": 999.0})

    prices._get = xetra_wrong_isin
    try:
        asyncio.run(prices._fetch_boerse(ibkr.CEMA))
        raise AssertionError("чужий ISIN мусив бути відкинутий")
    except ValueError as e:
        assert "інший ISIN" in str(e)

    # Yahoo: запит за символом, тому валюта — обовʼязкова перевірка.
    # Саме так відсіюється не та версія фонду (напр. лондонська, у доларах).
    def _yahoo(currency="EUR", kind="ETF", price=12.82):
        async def fn(url, params, timeout=15):
            return _Resp({"chart": {"result": [{"meta": {
                "currency": currency, "instrumentType": kind,
                "regularMarketPrice": price, "fullExchangeName": "XETRA",
                "regularMarketTime": 1788264241,
            }}]}})
        return fn

    prices._get = _yahoo()
    data = asyncio.run(prices._fetch_yahoo(ibkr.WEBN))
    assert data["price"] == 12.82 and "WEBN.DE" in data["source"]

    for bad_currency in ("USD", "GBP", None):
        prices._get = _yahoo(currency=bad_currency)
        try:
            asyncio.run(prices._fetch_yahoo(ibkr.WEBN))
            raise AssertionError(f"валюта {bad_currency} мусила бути відкинута")
        except ValueError as e:
            assert "не EUR" in str(e)

    prices._get = _yahoo(kind="EQUITY")
    try:
        asyncio.run(prices._fetch_yahoo(ibkr.WEBN))
        raise AssertionError("не-ETF мусив бути відкинутий")
    except ValueError as e:
        assert "не ETF" in str(e)
    print("розбір джерел OK")


def test_safe_price_never_raises():
    # Джерело падає -> None, нагадування все одно піде.
    assert asyncio.run(ibkr.safe_price(_FakePrices(boom=True), ibkr.WEBN)) is None
    # Джерела взагалі немає (etf=None у Services) -> теж None.
    assert asyncio.run(ibkr.safe_price(None, ibkr.WEBN)) is None
    # Нормальний випадок.
    fake = _FakePrices({"WEBN": 12.82, "CEMA": 259.05})
    assert asyncio.run(ibkr.safe_price(fake, ibkr.CEMA)) == 259.05
    print("safe_price OK")


# --- план на місяць ---

def test_month_plan_fields():
    # Серпень — місяць CEMA: сума дорівнює ціні одного паю, а не звичайним €200.
    plan = ibkr.month_plan(datetime.date(2026, 8, 15), price=259.05)
    assert plan["month"] == "2026-08" and plan["month_name"] == "серпень 2026"
    assert plan["fund"].ticker == "CEMA"
    assert plan["single_share"] is True
    assert plan["base"] == ibkr.DEFAULT_AMOUNT == 200.0
    assert plan["amount"] == 259.05 and plan["units"] == 1
    assert plan["first_business_day"] == datetime.date(2026, 8, 3)

    # Звичайний місяць — сума саме внеску.
    plan = ibkr.month_plan(datetime.date(2026, 9, 1), price=12.82, amount=400.0)
    assert plan["single_share"] is False
    assert plan["amount"] == 400.0 and plan["units"] == 31
    print("план на місяць OK")


# --- заплановані нагадування (app/alerts.py) ---

class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        self.sent.append({"text": text, "kb": reply_markup})


class _Services:
    def __init__(self, storage, etf):
        self.storage = storage
        self.etf = etf


def _alerts_env():
    """Планувальник під тестом: без тихих годин і з фіктивним отримувачем."""
    import app.alerts as alerts

    saved = (alerts._quiet_now, alerts.config.owner_id)
    alerts._quiet_now = lambda: False
    alerts.config.owner_id = 424242
    return alerts, saved


def _restore(alerts, saved):
    alerts._quiet_now, alerts.config.owner_id = saved


def test_scheduled_reminder_is_sent_once_and_repeats_until_done():
    alerts, saved = _alerts_env()
    try:
        st = _tmp_storage()
        bot = _FakeBot()
        services = _Services(st, _FakePrices({"WEBN": 12.82}))

        # Жовтень 2026: 1-ше — четвер, тобто це і є перший робочий день.
        # До нього нагадування не шлемо взагалі (тут 1-ше вересня, інший місяць — тому
        # беремо листопад: 1.11.2026 — неділя, перший робочий день 2-ге).
        asyncio.run(alerts.ibkr_check(bot, services, datetime.date(2026, 11, 1)))
        assert bot.sent == [] and st.ibkr_get_month("2026-11") is None

        # Перший робочий день — нагадування пішло, у журналі зʼявився запис.
        asyncio.run(alerts.ibkr_check(bot, services, datetime.date(2026, 11, 2)))
        assert len(bot.sent) == 1
        assert "IE0003XJA0J9" in bot.sent[0]["text"]
        assert bot.sent[0]["kb"] is not None
        rec = st.ibkr_get_month("2026-11")
        assert rec["status"] == "reminded" and rec["fund"] == "WEBN"
        assert rec["price"] == 12.82

        # Наступного дня повторно не шлемо: повтори лише 5, 12 і 20 числа.
        asyncio.run(alerts.ibkr_check(bot, services, datetime.date(2026, 11, 3)))
        assert len(bot.sent) == 1

        # 5-те число — повторне нагадування.
        asyncio.run(alerts.ibkr_check(bot, services, datetime.date(2026, 11, 5)))
        assert len(bot.sent) == 2 and bot.sent[1]["text"].startswith("🔔 Нагадую")
        # Двічі того самого дня — ні (дедуп через alert_state).
        asyncio.run(alerts.ibkr_check(bot, services, datetime.date(2026, 11, 5)))
        assert len(bot.sent) == 2

        # 12-те — ще одне.
        asyncio.run(alerts.ibkr_check(bot, services, datetime.date(2026, 11, 12)))
        assert len(bot.sent) == 3

        # Позначила «зроблено» -> 20-го вже тихо.
        st.ibkr_mark_done("2026-11", units=15)
        asyncio.run(alerts.ibkr_check(bot, services, datetime.date(2026, 11, 20)))
        assert len(bot.sent) == 3, [m["text"][:40] for m in bot.sent]
        print("розклад нагадувань OK:", len(bot.sent), "повідомлення")
    finally:
        _restore(alerts, saved)


def test_scheduled_reminder_is_quiet_after_skip_and_respects_switch():
    alerts, saved = _alerts_env()
    try:
        st = _tmp_storage()
        bot = _FakeBot()
        services = _Services(st, _FakePrices({"WEBN": 12.82}))

        asyncio.run(alerts.ibkr_check(bot, services, datetime.date(2026, 11, 2)))
        assert len(bot.sent) == 1
        st.ibkr_mark_skipped("2026-11")
        for day in (5, 12, 20):
            asyncio.run(alerts.ibkr_check(bot, services, datetime.date(2026, 11, day)))
        assert len(bot.sent) == 1

        # Сповіщення вимкнені — нічого не шлемо навіть у перший робочий день.
        st.set_alerts(False)
        asyncio.run(alerts.ibkr_check(bot, services, datetime.date(2026, 12, 1)))
        assert len(bot.sent) == 1 and st.ibkr_get_month("2026-12") is None
        print("пропуск і вимикач OK")
    finally:
        _restore(alerts, saved)


def test_scheduled_reminder_catches_up_if_bot_was_down():
    """Бот лежав у перший робочий день — нагадування не має пропасти назовсім."""
    alerts, saved = _alerts_env()
    try:
        st = _tmp_storage()
        bot = _FakeBot()
        services = _Services(st, _FakePrices({"WEBN": 12.82}))
        asyncio.run(alerts.ibkr_check(bot, services, datetime.date(2026, 11, 9)))
        assert len(bot.sent) == 1 and st.ibkr_get_month("2026-11") is not None
        print("догін пропущеного дня OK")
    finally:
        _restore(alerts, saved)


def test_scheduled_august_reminder_records_share_price_as_amount():
    """У серпні в журнал має піти сума за один пай, а не звичайні €200."""
    alerts, saved = _alerts_env()
    try:
        st = _tmp_storage()
        bot = _FakeBot()
        services = _Services(st, _FakePrices({"CEMA": 259.05}))

        # 1.08.2027 — неділя, тому перший робочий день — 2-ге.
        asyncio.run(alerts.ibkr_check(bot, services, datetime.date(2027, 8, 2)))
        assert len(bot.sent) == 1
        text = bot.sent[0]["text"]
        assert "IE00B5L8K969" in text and "Купуємо рівно 1 пай CEMA" in text
        assert "€59.05" in text                 # скільки докласти

        rec = st.ibkr_get_month("2027-08")
        assert rec["fund"] == "CEMA" and rec["price"] == 259.05
        assert rec["amount"] == 259.05, rec["amount"]

        # Після «зроблено» частка рахується з реальної суми, а не з €200.
        st.ibkr_mark_done("2027-08", units=1)
        tot = ibkr.totals(st, include_start=False)
        assert tot["by_fund"]["CEMA"]["invested"] == 259.05
        print("серпень у журналі OK:", rec["amount"])
    finally:
        _restore(alerts, saved)


def test_scheduled_reminder_goes_out_without_price():
    """Ціна недоступна — нагадування все одно надсилається, просто без паїв."""
    alerts, saved = _alerts_env()
    try:
        st = _tmp_storage()
        bot = _FakeBot()
        services = _Services(st, _FakePrices(boom=True))
        asyncio.run(alerts.ibkr_check(bot, services, datetime.date(2026, 11, 2)))
        assert len(bot.sent) == 1
        text = bot.sent[0]["text"]
        assert "IE0003XJA0J9" in text and "€200" in text
        assert "Ціну паю зараз не вдалось отримати" in text
        assert st.ibkr_get_month("2026-11")["price"] is None
        print("нагадування без ціни OK")
    finally:
        _restore(alerts, saved)


def test_scheduled_glide_warning_is_sent_once_per_stage():
    alerts, saved = _alerts_env()
    try:
        st = _tmp_storage()
        bot = _FakeBot()
        services = _Services(st, _FakePrices())
        st.set_setting("ibkr_start_date", "2026-09-01")

        # Далеко до етапу — тихо.
        asyncio.run(alerts.ibkr_glide_check(bot, services, datetime.date(2030, 1, 1)))
        assert bot.sent == []

        # За два місяці до року 10 — попередження, але лише одне.
        asyncio.run(alerts.ibkr_glide_check(bot, services, datetime.date(2036, 7, 10)))
        assert len(bot.sent) == 1 and "рік 10" in bot.sent[0]["text"]
        asyncio.run(alerts.ibkr_glide_check(bot, services, datetime.date(2036, 7, 11)))
        assert len(bot.sent) == 1
        print("попередження про етап OK")
    finally:
        _restore(alerts, saved)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("\nALL_IBKR_CHECKS_OK")
