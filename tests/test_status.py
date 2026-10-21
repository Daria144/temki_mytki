"""Команда /status: повний стан її грошей одним копіювальним текстом.

Цей дамп читатиме стороння AI, яка про неї не знає нічого. Тому перевіряємо не
«чи є цифри», а чи текст самодостатній: чи названі гроші, які НЕ вільні, чи є
поточні ставки для порівняння і чи є правила, без яких порада буде загальною.

Окремо стережемо дві речі, заради яких команду й робили: дамп не ходить до
порадника (має працювати при вичерпаній квоті Gemini) і не ламає копіювальний
блок при нарізці на повідомлення.

Мережі немає: біржі, ключі й ринок мокаємо.

Запуск: ./.venv/bin/python -m pytest tests/test_status.py -q
"""
import asyncio
import datetime
import os
import sys
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import budget, handlers, service, status
from app.config import config
from app.earn import Offer, classify_risk
from app.formatting import QUOTE_CLOSE, QUOTE_OPEN, journal_list_text
from app.portfolio import EARN, SPOT, Holding, Portfolio
from app.status import dump_text, money_snapshot, status_messages
from app.storage import KIND_FIXED, KIND_FLEX, STATUS_ACTIVE, Storage

TODAY = datetime.date(2026, 9, 5)
# Межа Telegram на одне повідомлення. Дамп великий, тому це не теорія.
TG_LIMIT = 4096


def _store(tmp_path, name="status.db") -> Storage:
    st = Storage(str(tmp_path / name))
    st.init()
    return st


def _add_order(st: Storage, coin: str, price: float, notional: float,
               status_: str, created_at: int, filled_at=None, idx=[0]):
    idx[0] += 1
    with st._conn() as c:
        c.execute(
            "INSERT INTO orders(rec_id, order_index, coin, price, qty, notional, status, "
            "created_at, filled_at) VALUES(0, ?, ?, ?, ?, ?, ?, ?, ?)",
            (idx[0], coin, price, notional / price, notional, status_, created_at, filled_at),
        )


def _offer(exchange, coin, apr, product="Simple Earn"):
    return Offer(exchange, coin, apr, "flex", classify_risk(coin),
                 product=product, section=f"Заробити → {product}")


class _FakeMarket:
    def __init__(self, fail=False):
        self.fail = fail

    async def fetch_coin(self, coin):
        if self.fail:
            raise RuntimeError("джерела цін недоступні")
        price = {"BTC": 110_000.0, "ETH": 4_000.0}[coin.upper()]
        return {"coin": coin.upper(), "price": price, "change_24h": -0.012}

    async def fetch_fng(self):
        return 34


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


def _rich_storage(tmp_path, name="status.db") -> Storage:
    """База, схожа на її справжню: бюджет, докупки, заявка, три вклади в журналі."""
    st = _store(tmp_path, name)
    month = budget.current_month()
    start = int(datetime.datetime(TODAY.year, TODAY.month, 1).timestamp()) + 3600
    st.set_budget(month, 300.0)
    _add_order(st, "BTC", 100_000.0, 120.0, "filled", start, filled_at=start + 100)
    _add_order(st, "BTC", 95_000.0, 60.0, "open", int(time.time()) - 12 * 86400)
    st.earn_deposit_add({"coin": "USDT", "amount": 200.0, "exchange": "Bybit",
                         "product": "Savings", "apr": 0.0677, "kind": KIND_FLEX},
                        STATUS_ACTIVE)
    st.earn_deposit_add({"coin": "USDT", "amount": 500.0, "exchange": "Bybit",
                         "product": "On-Chain Earn", "apr": 0.0475, "kind": KIND_FLEX},
                        STATUS_ACTIVE)
    st.earn_deposit_add({"coin": "BTC", "amount": 0.01, "exchange": "OKX",
                         "product": "Simple Earn", "apr": 0.005, "kind": KIND_FIXED,
                         "term_days": 45,
                         "unlock_date": (TODAY + datetime.timedelta(days=45)).isoformat()},
                        STATUS_ACTIVE)
    return st


def _holdings():
    return [
        Holding("Bybit", "USDT", 700.0, 700.0, EARN, 0.0475, "Savings"),
        Holding("OKX", "BTC", 0.01, 1100.0, EARN, 0.005, "Simple Earn"),
        Holding("Bybit", "USDT", 165.0, 165.0, SPOT),
        Holding("OKX", "USDT", 120.0, 120.0, SPOT),
    ]


def _services(storage, market=None, earn=None, portfolio=None):
    return types.SimpleNamespace(
        storage=storage,
        market=market or _FakeMarket(),
        earn=earn or _FakeEarn([_offer("Bybit", "USDT", 0.0677, "Savings"),
                                _offer("OKX", "USDT", 0.041, "Simple Earn"),
                                _offer("OKX", "BTC", 0.005, "Simple Earn"),
                                _offer("OKX", "ETH", 0.021, "ETH Staking"),
                                _offer("Bybit", "DOGE", 0.12, "Savings")]),
        portfolio=portfolio if portfolio is not None else _FakePortfolio(_holdings()),
        etf=None,
        coin_facts=None,
    )


def _dump(tmp_path, name="status.db", **kw) -> str:
    snap = asyncio.run(money_snapshot(_services(_rich_storage(tmp_path, name), **kw), TODAY))
    return dump_text(snap)


# =====================================================================
# 1. Дамп читає чужа AI — він мусить бути самодостатнім
# =====================================================================

def test_dump_opens_with_a_ready_request_and_the_time_it_was_taken(tmp_path):
    """Одна вставка має і дані, і завдання: інакше прийде порада з інтернету."""
    text = _dump(tmp_path, "frame.db")
    head = text.split("ХТО Я")[0]
    assert "Ось мій поточний фінансовий стан і мої правила" in head
    assert "не вигадуй умов" in head
    assert "СТАН ЗНЯТО:" in head
    # Попередження про рух цін — щоб дамп не видавали за вічну правду.
    assert "приблизними" in head
    # Рамка стоїть саме ПЕРШОЮ: те, що нижче, читається вже як дані.
    assert text.index("Ось мій поточний") < text.index("РИНОК ЗАРАЗ")


def test_dump_explains_who_she_is(tmp_path):
    text = _dump(tmp_path, "who.db")
    assert "не торгую" in text
    assert "Угорщині" in text
    assert "OKX і Bybit" in text


def test_market_block_gives_prices_and_explains_the_fear_index(tmp_path):
    text = _dump(tmp_path, "market.db")
    assert "BTC: $110 000" in text and "ETH: $4 000" in text
    # Число індексу без пояснення чужій AI нічого не каже.
    assert "34 зі 100" in text and "паніка" in text


# =====================================================================
# 2. Гроші: що вільне, а що лише виглядає вільним
# =====================================================================

def test_money_reserved_for_buy_orders_is_named_as_not_free(tmp_path):
    """Головна пастка: $60 під заявкою виглядають як вільні долари."""
    text = _dump(tmp_path, "free.db")
    assert "• OKX: $120" in text and "• Bybit: $165" in text
    assert "Разом вільних доларів: $285" in text
    assert "$60 зарезервовано" in text
    assert "ЦЕ НЕ ВІЛЬНІ ГРОШІ" in text
    assert "Реально можна кудись покласти прямо зараз: $225" in text


def test_budget_shows_what_is_actually_left_to_use(tmp_path):
    text = _dump(tmp_path, "budget.db")
    assert "Місячний бюджет на вересень 2026" in text and "$300" in text
    assert "реально куплено $120" in text
    assert "Незадіяний залишок бюджету" in text
    # $300 - $120 куплено - $60 у заявці = $120 вільного залишку.
    assert "$120. Саме це — гроші, якими я можу розпоряджатись" in text


def test_open_orders_show_price_amount_and_how_long_they_hang(tmp_path):
    text = _dump(tmp_path, "orders.db")
    assert "BTC по $95 000 на $60 " in text
    assert "стоїть 12 днів" in text


def test_bought_crypto_is_shown_with_profit_and_target_split(tmp_path):
    text = _dump(tmp_path, "crypto.db")
    assert "вкладено $120" in text
    assert "Бажана пропорція портфеля: BTC 60%, ETH 40%" in text


# =====================================================================
# 3. Earn: ключі, журнал і розбіжність між ними
# =====================================================================

def test_real_balances_from_keys_show_where_money_actually_sits(tmp_path):
    text = _dump(tmp_path, "where.db")
    assert "Bybit · працює під відсоток · $700" in text
    assert "Savings" in text and "ставка зараз ~4.75%" in text
    assert "OKX · лежить вільно · $120" in text


def test_journal_keeps_the_entry_rate_and_the_unlock_date(tmp_path):
    """Саме цього ключі не показують — ставка входу і строк."""
    text = _dump(tmp_path, "journal.db")
    assert "зайшла під 6.77%" in text
    assert "вивід вільний" in text
    assert "замкнено до 20.10 (ще 45 днів)" in text
    assert "Разом за журналом працює" in text


def test_journal_and_exchange_disagreement_is_told_from_both_sides(tmp_path):
    """У журналі $700, а на біржі в earn лежить $700 + 0.01 BTC — розбіжність видно."""
    holdings = [
        Holding("Bybit", "USDT", 900.0, 900.0, EARN, 0.0475, "Savings"),
        Holding("OKX", "BTC", 0.01, 1100.0, EARN, 0.005, "Simple Earn"),
    ]
    text = _dump(tmp_path, "recon.db", portfolio=_FakePortfolio(holdings))
    assert "Журнал розходиться з реальними балансами" in text
    assert "правда завжди за ключами" in text
    assert "у журналі $700" in text and "на біржі лежить $900" in text
    assert "не записувала" in text


def test_rates_block_covers_her_coins_and_forbids_praising_worse(tmp_path):
    text = _dump(tmp_path, "rates.db")
    assert "• USDT — 6.77% на рік (Bybit Savings)" in text
    assert "• BTC — 0.50% на рік" in text
    # Чужа монета в блок ставок не лізе: порівнювати треба з її грошима.
    assert "DOGE" not in text
    assert "Те, що гірше за них, мені не цікаве" in text


# =====================================================================
# 4. IBKR і правила
# =====================================================================

def test_ibkr_block_names_the_fund_of_this_month_and_the_stage(tmp_path):
    text = _dump(tmp_path, "ibkr.db")
    assert "WEBN" in text and "IE0003XJA0J9" in text
    assert "серпень — CEMA" in text
    assert "Етап стратегії (glide path)" in text


def test_rules_carry_every_number_that_makes_advice_meaningful(tmp_path):
    """Без цих правил порада буде загальною — заради них дамп і робився."""
    text = _dump(tmp_path, "rules.db").split("МОЇ ПРАВИЛА")[1]
    assert "ПРІОРИТЕТ — докупівля крипти на просадці" in text
    assert "подушка на просадку" in text
    assert "максимум половину (50%)" in text and "$100" in text
    assert "$20 на рік" in text and "$10 на рік" in text
    assert "120 днів" in text
    assert "50-ки за розміром" in text and "3 років" in text
    assert "гірше за мою поточну ставку" in text
    assert "дві цифри" in text
    assert "Не вигадуй умов" in text
    assert "Угорщина" in text
    assert "тільки на OKX і Bybit" in text
    assert "ніколи не продаю" in text
    assert "Автопродовження" in text


def test_dump_admits_what_it_does_not_know(tmp_path):
    text = _dump(tmp_path, "limits.db")
    assert "Банк, готівка, P2P" in text
    assert "знімок" in text


# =====================================================================
# 5. Нарізка: копіювальний блок не можна рвати
# =====================================================================

def test_every_part_fits_telegram_and_holds_exactly_one_copy_block(tmp_path):
    snap = asyncio.run(money_snapshot(_services(_rich_storage(tmp_path, "cut.db")), TODAY))
    parts = status_messages(snap)
    assert len(parts) > 1, "дамп великий — він мусить різатись на частини"
    for part in parts:
        assert len(part) < TG_LIMIT
        assert part.count(QUOTE_OPEN) == 1 and part.count(QUOTE_CLOSE) == 1
        assert part.index(QUOTE_OPEN) < part.index(QUOTE_CLOSE)


def test_parts_are_numbered_so_she_can_paste_them_in_order(tmp_path):
    snap = asyncio.run(money_snapshot(_services(_rich_storage(tmp_path, "num.db")), TODAY))
    parts = status_messages(snap)
    total = len(parts)
    for i, part in enumerate(parts, 1):
        assert f"частина {i} з {total}" in part
    assert "далі буде ще" in parts[0]
    assert "остання частина" in parts[-1]


def test_nothing_from_the_dump_is_lost_in_the_cut(tmp_path):
    snap = asyncio.run(money_snapshot(_services(_rich_storage(tmp_path, "keep.db")), TODAY))
    joined = "\n".join(status_messages(snap))
    for line in dump_text(snap).splitlines():
        assert line in joined, f"рядок загубився при нарізці: {line[:60]}"


def test_a_section_is_never_split_in_the_middle_of_a_line(tmp_path):
    """Ріжемо по рядках: обрізане на пів слова речення чужа AI прочитає невірно."""
    snap = asyncio.run(money_snapshot(_services(_rich_storage(tmp_path, "cut2.db")), TODAY))
    parts = status_messages(snap, limit=900)
    lines = dump_text(snap).splitlines()
    for part in parts:
        body = part.split(QUOTE_OPEN)[1].split(QUOTE_CLOSE)[0]
        for line in body.splitlines():
            if not line or line.startswith("(це ") or line.endswith("(продовження)"):
                continue
            assert line in lines


# =====================================================================
# 6. Незалежність від порадника і від збоїв бірж
# =====================================================================

def test_status_never_asks_the_advisor(tmp_path, monkeypatch):
    """Головна перевага команди: вона працює при вичерпаній квоті Gemini."""
    def _boom(*a, **kw):
        raise AssertionError("/status не має ходити до порадника")

    monkeypatch.setattr(config, "gemini_api_key", "ключ-є-але-він-не-потрібен")
    for name in ("decide", "explain_promos", "explain_coins", "parse_deposits"):
        monkeypatch.setattr(service, name, _boom)
    text = _dump(tmp_path, "noai.db")
    assert "МОЇ ПРАВИЛА" in text


def test_broken_exchange_keys_do_not_break_the_dump(tmp_path):
    text = _dump(tmp_path, "boom.db", portfolio=_FakePortfolio(boom=True))
    assert "ключі бірж не відповіли" in text
    assert "Цього разу не відповіли: ключі бірж" in text
    # Решта картинки лишається: бюджет і журнал живуть у своїй базі.
    assert "Місячний бюджет" in text and "зайшла під 6.77%" in text


def test_dead_market_does_not_break_the_dump(tmp_path):
    text = _dump(tmp_path, "nomarket.db", market=_FakeMarket(fail=True))
    assert "Ціни зараз отримати не вдалось" in text
    assert "МОЇ ПРАВИЛА" in text


def test_partial_exchange_failure_is_said_out_loud(tmp_path):
    text = _dump(tmp_path, "partial.db",
                 portfolio=_FakePortfolio(_holdings(), failed=["OKX стейкінг"]))
    assert "OKX стейкінг" in text
    assert "це не «там нуль»" in text


def test_empty_bot_still_produces_a_usable_dump(tmp_path):
    """Порожня база — не привід мовчати: правила й ставки все одно корисні."""
    snap = asyncio.run(money_snapshot(_services(_store(tmp_path, "empty.db"),
                                                portfolio=_FakePortfolio()), TODAY))
    text = dump_text(snap)
    assert "Журнал порожній" in text
    assert "Виконаних докупок ще немає" in text
    assert "бюджет на цей місяць ще не заданий" in text
    assert "МОЇ ПРАВИЛА" in text


# =====================================================================
# 7. Команда в боті
# =====================================================================

class _Msg:
    def __init__(self, text="/status", user_id=1):
        self.text = text
        self.from_user = types.SimpleNamespace(id=user_id)
        self.replies = []

    async def answer(self, text, reply_markup=None, **kwargs):
        self.replies.append({"text": text, "kb": reply_markup, "kw": kwargs})
        return self.replies[-1]


def test_command_sends_the_whole_dump_as_copy_blocks(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "owner_id", 1)
    msg = _Msg()
    asyncio.run(handlers.cmd_status(msg, _services(_rich_storage(tmp_path, "cmd.db"))))
    texts = [r["text"] for r in msg.replies]
    assert "Збираю повний стан" in texts[0]
    blocks = [t for t in texts if "<pre>" in t]
    assert blocks, "дамп має піти копіювальним блоком"
    assert all(r["kw"].get("parse_mode") == "HTML" for r in msg.replies if "<pre>" in r["text"])
    assert "Ось мій поточний фінансовий стан" in blocks[0]


def test_stranger_gets_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "owner_id", 1)
    msg = _Msg(user_id=999)
    asyncio.run(handlers.cmd_status(msg, _services(_rich_storage(tmp_path, "guard.db"))))
    assert msg.replies == []


# =====================================================================
# 8. Журнал теж має бути копіювальним
# =====================================================================

def test_journal_list_ends_with_a_copyable_block(tmp_path):
    st = _rich_storage(tmp_path, "jcopy.db")
    text = journal_list_text(st.earn_deposits(), {"BTC": 110_000.0})
    assert QUOTE_OPEN in text and QUOTE_CLOSE in text
    block = text.split(QUOTE_OPEN)[1].split(QUOTE_CLOSE)[0]
    assert "$200 USDT — Bybit Savings, 6.77%" in block
    assert "0.01 BTC — OKX Simple Earn" in block
    # У копію не тягнемо звертань до неї — це текст для вставки в інший чат.
    assert "допиши" not in block


def test_empty_journal_has_nothing_to_copy(tmp_path):
    text = journal_list_text([], {})
    assert QUOTE_OPEN not in text


def test_reserved_money_is_named_even_without_exchange_keys(tmp_path):
    """Без ключів сума залишків невідома — «з них» не було б від чого віднімати."""
    text = _dump(tmp_path, "nokeys.db", portfolio=_FakePortfolio())
    assert "суму вільних доларів вважай невідомою" in text
    assert "$60 моїх грошей зарезервовано" in text
    assert "Реально можна кудись покласти" not in text


def test_dust_balances_are_kept_out_of_the_dump(tmp_path):
    """0.000000006 BTC у дампі — це шум, який чужа AI сприйме як дані."""
    holdings = _holdings() + [
        Holding("OKX", "BTC", 5.99e-09, 0.0, SPOT),
        Holding("Bybit", "NIGHT", 0.76, 0.02, SPOT),
    ]
    text = _dump(tmp_path, "dust.db", portfolio=_FakePortfolio(holdings))
    assert "NIGHT" not in text and "e-09" not in text
    assert "Ще 2 дрібних залишків дешевше за $1" in text


def test_dump_warns_that_bought_crypto_is_only_what_the_bot_recorded(tmp_path):
    text = _dump(tmp_path, "onlybot.db")
    assert "лише докупки, які записав бот" in text


# =====================================================================
# 8. Гроші під гіршою ставкою: мовчати про це не можна навіть нижче порога
# =====================================================================

def _tiered_usdt():
    """Ставки, як на живих даних: 6.76% лише на перші $200, далі 1.76%."""
    from app.earn import Tier
    return Offer("Bybit", "USDT", 0.0176, "flex", "low", 1.5, 0.0,
                 [Tier(0.0, 200.0, 0.0676), Tier(200.0, None, 0.0176)],
                 "Savings", "Earn → Savings")


def test_difference_below_the_threshold_is_still_named_in_money(tmp_path):
    """Бот не пише їй про різницю менше $20/рік — але в дампі ця цифра мусить бути.

    Інакше картина неповна: рішення її, і приймає вона його саме в цьому чаті.
    """
    text = _dump(tmp_path, "below.db")
    assert "ЧИ НЕ ЛЕЖАТЬ МОЇ ГРОШІ ПІД ГІРШОЮ СТАВКОЮ" in text
    block = text.split("ЧИ НЕ ЛЕЖАТЬ МОЇ ГРОШІ ПІД ГІРШОЮ СТАВКОЮ")[1]
    assert "нижче нього бот не пише мені сам, але й не приховує" in block
    # $700 під 4.75% проти доступних 6.77% — це $14.14 на рік, тобто нижче порога.
    assert "Різниця $14.14/рік — це нижче мого порога, тому бот і мовчить" in block


def test_a_difference_above_the_threshold_is_marked_as_worth_doing(tmp_path):
    """Те, що поріг перетинає, має читатись інакше, ніж те, що ні."""
    holdings = [Holding("OKX", "USDT", 5000.0, 5000.0, SPOT)]
    text = _dump(tmp_path, "above.db", portfolio=_FakePortfolio(holdings))
    assert "це ВИЩЕ мого порога, варто зробити" in text
    assert "знадобиться переказ на Bybit" in text


def test_a_move_that_locks_the_money_says_so(tmp_path):
    """«Варто зробити» не має мовчки означати «замкни гроші на 45 днів»."""
    locked = Offer("Bybit", "BTC", 0.012, "staking", "mid", None, 45.0,
                   None, "On-Chain Earn", "Earn → On-Chain Earn")
    holdings = [Holding("Bybit", "BTC", 0.5, 55_000.0, SPOT)]
    text = _dump(tmp_path, "locked.db", earn=_FakeEarn([locked]),
                 portfolio=_FakePortfolio(holdings))
    assert "гроші там не миттєві: вивід ~45 дн." in text
    # Своя ж біржа — про переказ вигадувати нема чого.
    assert "знадобиться переказ" not in text


def test_the_split_across_places_is_offered_when_it_gives_more(tmp_path):
    """Один переїзд $17.7, два місця $21.9 — це вже інша відповідь, ніж «нічого»."""
    holdings = [Holding("OKX", "USDT", 439.47, 439.47, SPOT)]
    earn = _FakeEarn([_tiered_usdt(), _offer("OKX", "USDT", 0.035, "Simple Earn")])
    text = _dump(tmp_path, "split.db", earn=earn, portfolio=_FakePortfolio(holdings))
    assert "А по кількох місцях" in text
    assert "$200 у Bybit Savings під 6.76%" in text
    assert "різниця $21.90/рік — це ВИЩЕ мого порога" in text


def test_tiny_differences_do_not_clutter_the_dump(tmp_path):
    """Центи за рік — це не новина, а сміття в дампі, який читатиме чужа AI."""
    holdings = [Holding("Bybit", "USDT", 5.0, 5.0, SPOT)]
    text = _dump(tmp_path, "cents.db", portfolio=_FakePortfolio(holdings))
    assert "ЧИ НЕ ЛЕЖАТЬ МОЇ ГРОШІ ПІД ГІРШОЮ СТАВКОЮ" not in text


def test_position_rate_is_checked_against_the_public_one(tmp_path):
    """OKX у Simple Earn віддає по позиції 1.00%, а публічно платить 3.50%.

    Це той самий ринок позик: у позиції видно радше виставлену мінімальну ставку.
    Показати лише одну з цих цифр означало б збрехати про всю арифметику переїзду.
    """
    holdings = [Holding("OKX", "USDT", 500.0, 500.0, EARN, 0.01, "Simple Earn")]
    text = _dump(tmp_path, "mismatch.db", portfolio=_FakePortfolio(holdings))
    assert "ставка зараз ~1.00%" in text
    assert "публічно біржа обіцяє по цьому ж продукту ~4.10%" in text
    assert "звір це в застосунку" in text


def test_matching_rates_add_no_extra_words(tmp_path):
    """Коли обидві цифри збігаються — зайвого рядка бути не має."""
    holdings = [Holding("OKX", "BTC", 0.01, 1100.0, EARN, 0.005, "Simple Earn")]
    text = _dump(tmp_path, "match.db", portfolio=_FakePortfolio(holdings))
    assert "ставка зараз ~0.50%" in text
    assert "публічно біржа обіцяє" not in text
