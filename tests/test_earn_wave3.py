"""Третя хвиля earn: журнал вкладів, чесне визначення монети й решта дрібних правок.

Журнал закриває дірку, якої не закривають ключі: біржа віддає, СКІЛЬКИ грошей
лежить, але не віддає, під який відсоток і до якої дати. Тут перевіряється, що
записане не губиться, не осідає в базі без підтвердження й не бреше, коли
розходиться з реальним залишком.

Мережі немає: біржі, порадника й баланси мокаємо.

Запуск: ./.venv/bin/python -m pytest tests/test_earn_wave3.py
"""
import asyncio
import datetime
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import alerts, budget, handlers, journal, service
from app.config import config
from app.earn import (
    ALT_TOPUP_MAX,
    MAX_PLACES,
    Offer,
    Promo,
    Tier,
    alt_coin_allowed,
    alt_offers,
    alt_position,
    classify_risk,
    is_trading_promo,
    locked_options,
    optimize,
)
from app.formatting import (
    QUOTE_CLOSE,
    QUOTE_OPEN,
    coin_trust_lines,
    journal_confirm_text,
    journal_due_text,
    promo_quote,
    rate_drop_lines,
    reconcile_lines,
    to_html,
    weekly_review_text,
)
from app.journal import parse_line, parse_text
from app.portfolio import EARN, SPOT, Holding, Portfolio
from app.storage import KIND_FIXED, KIND_FLEX, STATUS_ACTIVE, STATUS_PENDING, Storage

TODAY = datetime.date(2026, 9, 5)


def _store(tmp_path, name="w3.db") -> Storage:
    st = Storage(str(tmp_path / name))
    st.init()
    return st


def _offer(exchange, coin, apr, tiers=None, kind="flex", product="Simple Earn",
           redeem_days=0.0, min_amount=None):
    return Offer(exchange, coin, apr, kind, classify_risk(coin), min_amount, redeem_days,
                 tiers, product, f"Заробити → {product}")


class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        self.sent.append({"text": text, "kb": reply_markup, "kw": kwargs})

    @property
    def text(self):
        return "\n".join(m["text"] for m in self.sent)


class _Msg:
    """Мінімальне повідомлення: нам потрібні лише текст, автор і відповіді."""

    def __init__(self, text):
        self.text = text
        self.from_user = types.SimpleNamespace(id=1)
        self.replies = []

    async def answer(self, text, reply_markup=None, **kwargs):
        self.replies.append({"text": text, "kb": reply_markup})
        return self.replies[-1]


class _Cb:
    def __init__(self, data):
        self.data = data
        self.from_user = types.SimpleNamespace(id=1)
        self.message = _Msg("")
        self.answers = []

    async def answer(self, text=""):
        self.answers.append(text)


class _FakeEarn:
    def __init__(self, offers=(), promos=()):
        self._offers = list(offers)
        self._promos = list(promos)

    async def offers(self):
        return self._offers

    async def promos(self):
        return self._promos

    async def promo_bodies(self, promos, limit=3):
        return {}


def _svc(storage, earn=None, portfolio=None):
    return types.SimpleNamespace(storage=storage, earn=earn or _FakeEarn(),
                                 portfolio=portfolio, market=None, coin_facts=None)


# =====================================================================
# A2. Розбір вільного тексту — саме ті рядки, які вона й пише
# =====================================================================

def test_parses_the_plain_lines_she_actually_writes():
    d = parse_line("200 usdt bybit savings 6.77", TODAY)
    assert d["amount"] == 200 and d["coin"] == "USDT"
    assert d["exchange"] == "Bybit" and d["product"] == "Savings"
    assert round(d["apr"], 4) == 0.0677
    assert d["kind"] == KIND_FLEX and d["unlock_date"] is None

    d = parse_line("500 usdt bybit on-chain 4.75", TODAY)
    assert d["product"] == "On-Chain Earn" and round(d["apr"], 4) == 0.0475

    d = parse_line("0.01 btc okx simple earn 0.5 на 45 днів", TODAY)
    assert d["amount"] == 0.01 and d["coin"] == "BTC" and d["exchange"] == "OKX"
    assert d["product"] == "Simple Earn" and round(d["apr"], 4) == 0.005
    # Строк не має сплутатись зі ставкою: 45 — це дні, 0.5 — відсоток.
    assert d["term_days"] == 45 and d["kind"] == KIND_FIXED
    assert d["unlock_date"] == (TODAY + datetime.timedelta(days=45)).isoformat()

    d = parse_line("поклала 200 usdt bybit savings 6.77% на 30 днів", TODAY)
    assert d["amount"] == 200 and round(d["apr"], 4) == 0.0677 and d["term_days"] == 30


def test_parses_several_deposits_at_once():
    text = ("200 usdt bybit savings 6.77\n"
            "500 usdt bybit on-chain 4.75\n"
            "0.01 btc okx simple earn 0.5 на 45 днів")
    deps = parse_text(text, TODAY)
    assert [d["coin"] for d in deps] == ["USDT", "USDT", "BTC"]
    assert [d["product"] for d in deps] == ["Savings", "On-Chain Earn", "Simple Earn"]


def test_explicit_unlock_date_is_understood():
    d = parse_line("0.01 btc okx simple earn 0.5 до 20.10", TODAY)
    assert d["unlock_date"] == "2026-10-20" and d["kind"] == KIND_FIXED
    # Дата, яка вже минула цього року, означає наступний рік — інакше журнал
    # одразу нагадував би про «прострочений» вклад.
    assert parse_line("100 usdt bybit savings 5 до 01.02", TODAY)["unlock_date"] == "2027-02-01"


def test_garbage_does_not_become_a_deposit():
    assert parse_line("привіт", TODAY) is None
    assert parse_line("", TODAY) is None
    assert parse_text("просто якийсь текст без цифр", TODAY) == []


# =====================================================================
# A1. Дві ловилки тексту не мають красти повідомлення одна в одної
# =====================================================================

def test_journal_button_and_ibkr_button_do_not_steal_each_others_text(tmp_path, monkeypatch):
    """Головний ризик пункту A1: у боті вже є ловилка тексту для паїв IBKR."""
    monkeypatch.setattr(config, "owner_id", 1)
    monkeypatch.setattr(config, "gemini_api_key", "")     # формульний розбір
    st = _store(tmp_path, "w3fsm.db")
    svc = _svc(st)

    # 1. Натиснули «записати вклад» -> текст іде в журнал, а не в паї.
    asyncio.run(handlers.cb_journal_new(_Cb("ejnew"), svc))
    msg = _Msg("200 usdt bybit savings 6.77")
    asyncio.run(handlers.msg_plain_text(msg, svc))
    assert "Записала 1 вклад" in msg.replies[0]["text"]
    assert st.earn_deposits(STATUS_PENDING)

    # 2. Натиснули «Зроблено» під IBKR -> той самий текст уже про паї.
    st.ibkr_ensure_month("2026-09", "WEBN", 200.0)
    handlers._start_wait(st, handlers.IBKR_STATE, "2026-09")
    msg = _Msg("15")
    asyncio.run(handlers.msg_plain_text(msg, svc))
    assert "куплено 15 паїв" in msg.replies[0]["text"]
    assert st.ibkr_get_month("2026-09")["units"] == 15.0

    # 3. Кнопка журналу гасить очікування IBKR, і навпаки — чекаємо лише на щось одне.
    handlers._start_wait(st, handlers.IBKR_STATE, "2026-09")
    asyncio.run(handlers.cb_journal_new(_Cb("ejnew"), svc))
    assert st.get_setting(handlers.IBKR_STATE, "") == ""
    handlers._start_wait(st, handlers.JOURNAL_STATE)
    handlers._start_wait(st, handlers.IBKR_STATE, "2026-09")
    assert st.get_setting(handlers.JOURNAL_STATE, "") == ""


def test_plain_text_without_any_expectation_is_ignored(tmp_path, monkeypatch):
    """Бот не чекає нічого — випадкове повідомлення не має нікуди потрапити."""
    monkeypatch.setattr(config, "owner_id", 1)
    st = _store(tmp_path, "w3fsm2.db")
    msg = _Msg("200 usdt bybit savings 6.77")
    asyncio.run(handlers.msg_plain_text(msg, _svc(st)))
    assert msg.replies == [] and st.earn_deposits(None) == []


def test_journal_expectation_expires(tmp_path):
    """Повідомлення через кілька годин — це вже не відповідь на кнопку."""
    st = _store(tmp_path, "w3fsm3.db")
    st.set_setting(handlers.JOURNAL_STATE, "1")     # 1970 рік
    assert handlers._wait_started(st, handlers.JOURNAL_STATE, handlers.JOURNAL_WAIT_SEC) is False
    assert st.get_setting(handlers.JOURNAL_STATE, "") == ""


# =====================================================================
# A3. Підтвердження: помилка розбору не осідає в журналі мовчки
# =====================================================================

def test_nothing_reaches_the_journal_without_confirmation(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "owner_id", 1)
    monkeypatch.setattr(config, "gemini_api_key", "")
    st = _store(tmp_path, "w3conf.db")
    svc = _svc(st)
    asyncio.run(handlers.cb_journal_new(_Cb("ejnew"), svc))
    asyncio.run(handlers.msg_plain_text(_Msg("200 usdt bybit savings 6.77"), svc))

    assert st.earn_deposits(STATUS_ACTIVE) == []        # активним ще нічого не стало
    assert len(st.earn_deposits(STATUS_PENDING)) == 1

    asyncio.run(handlers.cb_journal_confirm(_Cb("ejc:ok"), svc))
    assert len(st.earn_deposits(STATUS_ACTIVE)) == 1


def test_cancel_removes_the_misparsed_record_completely(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "owner_id", 1)
    monkeypatch.setattr(config, "gemini_api_key", "")
    st = _store(tmp_path, "w3cancel.db")
    svc = _svc(st)
    asyncio.run(handlers.cb_journal_new(_Cb("ejnew"), svc))
    asyncio.run(handlers.msg_plain_text(_Msg("200 usdt bybit savings 6.77"), svc))

    cb = _Cb("ejc:no")
    asyncio.run(handlers.cb_journal_confirm(cb, svc))
    assert st.earn_deposits(None) == []                 # ні активних, ні відкладених
    assert "Нічого не записала" in cb.message.replies[0]["text"]


def test_confirmation_shows_what_was_understood():
    deps = parse_text("200 usdt bybit savings 6.77\n"
                      "500 usdt bybit on-chain 4.75\n"
                      "0.01 btc okx simple earn 0.5 до 20.10", TODAY)
    text = journal_confirm_text(deps)
    assert "Записала 3 вклади" in text
    assert "$200 USDT — Bybit Savings, 6.77%, вивід вільний" in text
    assert "$500 USDT — Bybit On-Chain Earn, 4.75%, вивід вільний" in text
    assert "0.01 BTC — OKX Simple Earn, 0.50%, замкнено до 20.10" in text
    # Підсумок у грошах: $700 працює і приносить ~$37 на рік.
    assert "Разом працює $700 і 0.01 BTC" in text and "~$37" in text
    assert "Скасувати" in text


def test_gaps_in_the_record_are_visible_not_hidden():
    """Чого бот не зрозумів — має бути видно, а не тихо лежати порожнім."""
    text = journal_confirm_text(parse_text("300 usdt", TODAY))
    assert "не зрозуміла" in text and "біржу" in text and "ставку" in text


# =====================================================================
# A4-A5. Нагадування В ДЕНЬ розморозки і відповідь кнопками
# =====================================================================

def test_unlock_reminder_comes_on_the_day_with_advice(tmp_path):
    st = _store(tmp_path, "w3due.db")
    dep_id = st.earn_deposit_add({
        "coin": "BTC", "amount": 0.01, "exchange": "OKX", "product": "Simple Earn",
        "apr": 0.005, "kind": KIND_FIXED, "unlock_date": TODAY.isoformat(),
    }, STATUS_ACTIVE)

    okx = _offer("OKX", "BTC", 0.005)
    bot = _Bot()
    asyncio.run(alerts.journal_check(bot, _svc(st, _FakeEarn([okx])), TODAY))

    assert len(bot.sent) == 1
    text = bot.sent[0]["text"]
    assert "05.09 закінчується твій вклад: 0.01 BTC у OKX Simple Earn" in text
    # Головне попередження пункту A4: продовжать самі, часто під гіршу ставку.
    assert "автоматично продовжуються" in text and "гіршу ставку" in text
    assert "Зараз найкраще по BTC: 0.50%" in text
    labels = [b.text for row in bot.sent[0]["kb"].inline_keyboard for b in row]
    assert labels == ["✅ забрала", "🔄 продовжила"]

    # Двічі на день про той самий вклад не пишемо.
    bot2 = _Bot()
    asyncio.run(alerts.journal_check(bot2, _svc(st, _FakeEarn([okx])), TODAY))
    assert bot2.sent == []
    assert st.earn_deposit(dep_id)["status"] == STATUS_ACTIVE   # автоматично не закрили


def test_long_redemption_is_mentioned_in_the_reminder():
    """BTC на Bybit виводиться 45 днів — почати треба сьогодні, а не потім."""
    dep = {"coin": "BTC", "amount": 0.01, "exchange": "Bybit",
           "product": "On-Chain Earn", "apr": 0.005, "unlock_date": TODAY.isoformat()}
    text = journal_due_text(dep, TODAY, _offer("OKX", "BTC", 0.005), redeem_days=45)
    assert "вивід тут іде ~45 днів" in text and "20.10" in text


def test_answer_buttons_update_the_journal(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "owner_id", 1)
    st = _store(tmp_path, "w3ans.db")
    took = st.earn_deposit_add({"coin": "USDT", "amount": 200, "exchange": "Bybit",
                                "product": "Savings", "apr": 0.0677, "kind": KIND_FIXED,
                                "term_days": 30, "unlock_date": TODAY.isoformat()},
                               STATUS_ACTIVE)
    kept = st.earn_deposit_add({"coin": "USDT", "amount": 500, "exchange": "Bybit",
                                "product": "Savings", "apr": 0.0475, "kind": KIND_FIXED,
                                "term_days": 30, "unlock_date": TODAY.isoformat()},
                               STATUS_ACTIVE)
    svc = _svc(st)

    asyncio.run(handlers.cb_journal_due(_Cb(f"ejd:{took}:took"), svc))
    assert st.earn_deposit(took)["status"] == "closed"
    assert st.earn_deposit(took)["closed_how"] == "took"

    cb = _Cb(f"ejd:{kept}:kept")
    asyncio.run(handlers.cb_journal_due(cb, svc))
    row = st.earn_deposit(kept)
    assert row["status"] == STATUS_ACTIVE               # продовжений вклад лишається
    assert row["unlock_date"] > TODAY.isoformat()       # строк поїхав далі
    assert "ставку" in cb.message.replies[0]["text"]    # і попередили про нову ставку


# =====================================================================
# A6. Звірка з реальними балансами: правда завжди за ключами
# =====================================================================

def _portfolio(*holdings):
    return Portfolio(list(holdings), [], 0.0)


def test_money_outside_the_journal_is_reported():
    """Записала $200, а на біржі в Earn лежить $340 — про $140 бот має сказати."""
    pf = _portfolio(Holding("Bybit", "USDT", 340.0, 340.0, EARN, None, "Savings"))
    deps = [{"coin": "USDT", "amount": 200.0, "exchange": "Bybit", "product": "Savings"}]
    rows = journal.reconcile(pf, deps)
    assert len(rows) == 1 and round(rows[0]["diff"], 2) == 140.0

    text = "\n".join(reconcile_lines(rows))
    assert "у журналі $200" in text and "на біржі лежить $340" in text
    assert "$140 не враховані в журналі" in text


def test_journal_larger_than_the_exchange_is_also_reported():
    pf = _portfolio(Holding("Bybit", "USDT", 200.0, 200.0, EARN, None, "Savings"))
    deps = [{"coin": "USDT", "amount": 500.0, "exchange": "Bybit", "product": "Savings"}]
    rows = journal.reconcile(pf, deps)
    assert rows and rows[0]["diff"] < 0
    assert "схоже, ці гроші ти вже" in "\n".join(reconcile_lines(rows))

    # Нуль з будь-якого боку називаємо словами: «у журналі $0.00» читалось як збій.
    empty = journal.reconcile(_portfolio(Holding("OKX", "USDT", 500.0, 500.0, EARN)),
                              [{"coin": "USDT", "amount": 0.0, "exchange": "OKX"}])
    assert "у журналі цієї монети немає" in reconcile_lines(empty)[0]
    gone = journal.reconcile(_portfolio(Holding("OKX", "USDT", 0.0, 0.0, EARN)),
                             [{"coin": "USDT", "amount": 500.0, "exchange": "OKX"}])
    assert "на біржі зараз нічого немає" in reconcile_lines(gone)[0]


def test_small_and_matching_amounts_are_not_nitpicked():
    """Пил округлення й вільні гроші на споті — це не розбіжність журналу."""
    pf = _portfolio(
        Holding("Bybit", "USDT", 200.4, 200.4, EARN, None, "Savings"),
        Holding("Bybit", "USDT", 900.0, 900.0, SPOT),      # просто лежать, не в earn
    )
    deps = [{"coin": "USDT", "amount": 200.0, "exchange": "Bybit", "product": "Savings"}]
    assert journal.reconcile(pf, deps) == []
    assert journal.reconcile(None, deps) == []


# =====================================================================
# A7. Падіння ставки у гнучкому вкладі
# =====================================================================

def test_rate_drop_is_reported_in_percent_and_in_money():
    """Клала під 6.77%, зараз 4% — і у відсотках, і в грошах на рік."""
    dep = {"id": 1, "coin": "USDT", "amount": 500.0, "exchange": "Bybit",
           "product": "Savings", "apr": 0.0677, "kind": KIND_FLEX}
    now = _offer("Bybit", "USDT", 0.04, product="Savings")
    drops = journal.rate_drops([dep], {"USDT": (0.04, now)}, 10.0)
    assert len(drops) == 1 and round(drops[0]["lost_year"], 2) == 13.85

    # Той самий поріг вигоди, що й скрізь: $13.85 на рік нижче $20 — мовчимо,
    # бо ставка гнучкого продукту так стрибає мало не щодня.
    assert journal.rate_drops([dep], {"USDT": (0.04, now)}, 20.0) == []

    text = "\n".join(rate_drop_lines(drops, {"USDT": _offer("OKX", "USDT", 0.055)}))
    assert "клала під 6.77%, зараз 4.00%" in text
    assert "~$20.00/рік замість ~$33.85" in text and "$13.85/рік менше" in text
    assert "Краще зараз: OKX Simple Earn — 5.50%" in text

    # А той самий продукт, у якому вона вже сидить, за пораду не рахується.
    same = "\n".join(rate_drop_lines(drops, {"USDT": _offer("Bybit", "USDT", 0.055,
                                                            product="Savings")}))
    assert "Краще зараз" not in same


def test_fixed_deposit_rate_is_not_expected_to_float():
    dep = {"id": 1, "coin": "USDT", "amount": 5000.0, "apr": 0.10, "kind": KIND_FIXED}
    assert journal.rate_drops([dep], {"USDT": (0.02, None)}, 20.0) == []


def test_the_same_drop_is_not_repeated():
    dep = {"id": 1, "coin": "USDT", "amount": 500.0, "apr": 0.0677, "kind": KIND_FLEX,
           "rate_alert_apr": 0.04}
    assert journal.rate_drops([dep], {"USDT": (0.04, None)}, 20.0) == []
    # А от якщо просіла ще глибше — це вже нова новина.
    assert journal.rate_drops([dep], {"USDT": (0.02, None)}, 20.0)


# =====================================================================
# B. Монету шукаємо за найбільшою капіталізацією, а не «перший збіг»
# =====================================================================

class _FakeCG:
    """CoinGecko з двома монетами під тикером CP: справжньою й мертвою підробкою."""

    def __init__(self, markets, listing=None):
        self.markets = markets
        self.listing = listing or []
        self.asked = []

    async def _get(self, url, params):
        if url.endswith("/coins/markets"):
            page = int(params.get("page", 1))
            return self.markets if page == 1 else []
        if url.endswith("/coins/list"):
            return self.listing
        self.asked.append(url)
        return {"name": url.rsplit("/", 1)[-1], "market_cap_rank": 30,
                "genesis_date": "2017-01-01",
                "market_data": {"total_volume": {"usd": 5e8},
                                "market_cap": {"usd": 2e9}}}


def _facts(markets, listing=None):
    from app.market import CoinFacts

    cf = CoinFacts()
    fake = _FakeCG(markets, listing)
    cf._get = fake._get
    return cf, fake


def test_ticker_resolves_to_the_biggest_coin_not_the_first_one():
    """Справжній баг: під тикером CP бралась перша-ліпша монета з 8783-м місцем."""
    markets = [
        {"symbol": "cp", "id": "dead-cp-token", "market_cap": 1_000.0,
         "market_cap_rank": 8783},
        {"symbol": "cp", "id": "real-cp", "market_cap": 2_000_000_000.0,
         "market_cap_rank": 30},
    ]
    cf, fake = _facts(markets)
    profile = asyncio.run(cf.profile("CP"))
    assert profile["ambiguous"] is False
    assert profile["picked_by_cap"] is True and profile["matches"] == 2
    assert fake.asked and fake.asked[0].endswith("/real-cp")     # питали про велику


def test_unresolvable_ticker_says_so_instead_of_showing_numbers():
    """Кілька дрібних однойменних монет — краще мовчання, ніж чужі цифри."""
    markets = [
        {"symbol": "cp", "id": "cp-one", "market_cap": 900_000.0, "market_cap_rank": 4000},
        {"symbol": "cp", "id": "cp-two", "market_cap": 400_000.0, "market_cap_rank": 6000},
    ]
    cf, fake = _facts(markets)
    profile = asyncio.run(cf.profile("CP"))
    assert profile["ambiguous"] is True and fake.asked == []     # монету навіть не питали


def test_ticker_missing_from_the_top_is_ambiguous_too():
    cf, _ = _facts([], listing=[{"symbol": "cp", "id": "a"}, {"symbol": "cp", "id": "b"}])
    assert asyncio.run(cf.profile("CP"))["ambiguous"] is True
    # А тикера, якого немає взагалі, просто немає — це не «невпевненість».
    assert asyncio.run(cf.profile("ZZZZ")) is None


def test_broken_ranking_does_not_fall_back_to_guessing():
    """CoinGecko віддав 429 — краще сказати «не впевнена», ніж брати перший збіг."""
    from app.market import CoinFacts

    cf = CoinFacts()

    async def _dead(url, params):
        if url.endswith("/coins/markets"):
            raise RuntimeError("429 Too Many Requests")
        return [{"symbol": "cp", "id": "dead-cp-token"}]

    cf._get = _dead
    assert asyncio.run(cf.profile("CP"))["ambiguous"] is True


def test_unclear_coin_never_reaches_the_advisor(monkeypatch):
    """Порадник охоче напише переконливе речення про цифри, яких у нас немає."""
    called = []

    async def _explain(profiles):
        called.append(sorted(profiles))
        return {}

    monkeypatch.setattr(service, "explain_coins", _explain)

    class _Facts:
        async def profiles(self, coins):
            return {"CP": {"coin": "CP", "ambiguous": True, "matches": 12},
                    "SOL": {"coin": "SOL", "rank": 6, "age_years": 5.0,
                            "volume_usd": 2e9, "ambiguous": False}}

    out = asyncio.run(service.coin_reliability(_Facts(), ["CP", "SOL"]))
    assert called == [["SOL"]]                       # про CP не питали
    assert out["CP"]["ambiguous"] is True and out["CP"]["trust"] == "unknown"
    assert "не змогла надійно визначити" in out["CP"]["summary"]


def test_a_dead_directory_is_not_blamed_on_the_coin(monkeypatch):
    """Кілька однойменних монет — це одне, а недоступний CoinGecko — інше."""
    class _Facts:
        async def profiles(self, coins):
            return {"CP": {"coin": "CP", "ambiguous": True, "matches": 0}}

    out = asyncio.run(service.coin_reliability(_Facts(), ["CP"]))
    assert "довідник ринку зараз не відповідає" in out["CP"]["summary"]


def test_unclear_coin_shows_no_made_up_numbers():
    trust = {"CP": {"summary": "не змогла надійно визначити цю монету.",
                    "trust": "unknown", "ambiguous": True}}
    line = coin_trust_lines(trust, 500.0, {"CP": 0.25})[0]
    assert line.startswith("❔ CP —")
    assert "$" not in line and "%" not in line       # жодної цифри навмання


# =====================================================================
# C2. Ключові слова торгових акцій — по межах слів
# =====================================================================

def test_trading_words_match_whole_words_only():
    """Підрядком «bot» ловився всередині «both», і депозитна акція ставала торговою."""
    assert not is_trading_promo("Hold both BTC and ETH and earn 10% APR")
    assert not is_trading_promo("Robotics token listing")
    assert not is_trading_promo("Deposit USDT and get a bonus")
    assert is_trading_promo("Trading bonus: get $50")
    assert is_trading_promo("Grid bots competition")      # множина теж рахується
    assert is_trading_promo("Boost your futures volume")


# =====================================================================
# C3. Розкладаємо суму щонайбільше по трьох місцях
# =====================================================================

def test_money_is_spread_over_three_places():
    assert MAX_PLACES == 3
    offers = [
        _offer("Bybit", "USDT", 0.10, [Tier(0, 300, 0.10)], product="Savings"),
        _offer("OKX", "USDT", 0.08, [Tier(0, 300, 0.08)]),
        _offer("Bybit", "USDC", 0.06, [Tier(0, 300, 0.06)], product="Savings"),
        _offer("OKX", "USDC", 0.01, [Tier(0, None, 0.01)]),
    ]
    res = optimize(offers, 900.0)
    assert len(res["parts"]) == 3
    # На двох місцях третя сотня впала б у 1% — три місця дають на $50 більше.
    assert res["income"] > optimize(offers, 900.0, max_places=2)["income"]


# =====================================================================
# C5. Блокування коштів: тільки заради помітної вигоди й не з подушки
# =====================================================================

def test_locking_is_advised_only_when_it_is_worth_real_money():
    free = _offer("Bybit", "USDT", 0.05, product="Savings")
    locked = _offer("Bybit", "USDT", 0.09, kind="staking", product="On-Chain Earn",
                    redeem_days=30)
    res = locked_options([free, locked], 1000.0)
    assert res and round(res["gain_year"], 2) == 40.0    # +$40/рік — варте розмови

    # На $200 та сама різниця — це $8 на рік: замикати гроші заради цього не варто.
    assert locked_options([free, locked], 200.0) is None


def test_locked_block_never_touches_money_reserved_for_dips(tmp_path):
    """Ці долари чекають на просадку BTC — замкнути їх означало б проґавити її."""
    from app.formatting import _lock_block

    offers = [
        _offer("Bybit", "USDT", 0.05, product="Savings"),
        _offer("Bybit", "USDT", 0.09, kind="staking", product="On-Chain Earn",
               redeem_days=30),
    ]
    # $1000 вільних, але $800 зарезервовано -> рахуємо лише $200, і вигоди немає.
    assert _lock_block(offers, 1000.0, 800.0) == []
    text = "\n".join(_lock_block(offers, 1500.0, 500.0))
    assert "$1 000" in text                       # рахували з $1000, а не з $1500
    assert "чекають на просадку" in text


# =====================================================================
# C6-C7. Альткоїн: гроші беремо з незадіяного залишку місячного бюджету
# =====================================================================

def _budget_store(tmp_path, name, total, filled=0.0, open_orders=0.0):
    st = _store(tmp_path, name)
    st.set_budget(budget.current_month(), total)
    if filled:
        with st._conn() as c:
            c.execute("INSERT INTO orders(rec_id, order_index, coin, price, qty, notional,"
                      " status, created_at, filled_at) VALUES(1, 0, 'BTC', 1, 1, ?,"
                      " 'filled', ?, ?)",
                      (filled, int(datetime.datetime.now().timestamp()),
                       int(datetime.datetime.now().timestamp())))
    if open_orders:
        with st._conn() as c:
            c.execute("INSERT INTO orders(rec_id, order_index, coin, price, qty, notional,"
                      " status, created_at) VALUES(2, 0, 'BTC', 1, 1, ?, 'open', ?)",
                      (open_orders, int(datetime.datetime.now().timestamp())))
    return st


def test_alt_money_comes_from_the_unused_budget_remainder(tmp_path):
    """$500 бюджету, $200 уже куплено, $100 висить у заявці -> вільних $200."""
    st = _budget_store(tmp_path, "w3b1.db", 500.0, filled=200.0, open_orders=100.0)
    assert budget.unused_free(st) == 200.0

    pos = alt_position(200.0)
    # Половину лишаємо недоторканою: це та сама подушка на просадку крипти.
    assert pos["from_budget"] == 100.0 and pos["amount"] == 100.0
    assert pos["budget_left"] == 100.0 and pos["from_topup"] == 0.0
    # Більше, ніж треба самій акції, не беремо.
    assert alt_position(200.0, needed=40.0)["amount"] == 40.0


def test_no_free_budget_means_no_amount_is_offered(tmp_path):
    """Весь бюджет розібраний — суми, якої немає, бот не пропонує."""
    st = _budget_store(tmp_path, "w3b2.db", 300.0, filled=100.0, open_orders=200.0)
    assert budget.unused_free(st) == 0.0
    assert budget.unused_free(_store(tmp_path, "w3b3.db")) == 0.0   # бюджету взагалі нема

    pos = alt_position(0.0)
    assert pos["amount"] == 0.0
    offer = _offer("OKX", "ATOM", 0.169)
    text = "\n".join(alerts.alt_coin_lines("ATOM", offer, pos, {"rank": 40, "age_years": 6},
                                           freeing_soon=200.0))
    assert "вільних грошей у бюджеті цього місяця не лишилось" in text
    assert "Орієнтовно звільниться $200" in text
    # Дві цифри лишаються обовʼязковими навіть тут — на межі добровільного внеску.
    assert "~$16.90 за рік" in text and "мінус $40" in text
    assert "не більше $100" in text


def test_voluntary_topup_is_capped_at_one_hundred():
    """Докидати вона не планує; якщо колись і докине — не більше $100."""
    assert ALT_TOPUP_MAX == 100.0
    pos = alt_position(0.0, topup=500.0)
    assert pos["from_topup"] == 100.0 and pos["amount"] == 100.0
    # І поверх бюджету межа теж діє саме на СВОЇ гроші.
    pos = alt_position(400.0, topup=1000.0)
    assert pos["from_budget"] == 200.0 and pos["from_topup"] == 100.0
    assert pos["amount"] == 300.0


def test_alt_advice_names_both_numbers_and_the_source_of_money():
    offer = _offer("OKX", "ATOM", 0.169)
    pos = alt_position(200.0)
    text = "\n".join(alerts.alt_coin_lines("ATOM", offer, pos,
                                           {"rank": 40, "age_years": 6.0}))
    assert "Беру з вільних грошей місяця" in text and "лишилось $200" in text
    assert "Заробиш ~$16.90 за рік" in text          # скільки заробить...
    assert "втратиш ~$40" in text                    # ...і скільки може втратити
    assert "подушка" in text and "без блокування" in text
    assert "$100 лишаю недоторканими" in text


def test_only_big_and_old_coins_are_advised():
    assert alt_coin_allowed({"rank": 40, "age_years": 6.0})
    assert not alt_coin_allowed({"rank": 120, "age_years": 6.0})    # не топ-50
    assert not alt_coin_allowed({"rank": 12, "age_years": 1.5})     # молодша за 3 роки
    assert not alt_coin_allowed({"rank": 12, "ambiguous": True})    # монету не впізнали
    assert not alt_coin_allowed(None)
    # Вік «щонайменше» теж рахується: genesis_date у CoinGecko майже завжди порожній.
    assert alt_coin_allowed({"rank": 12, "age_years": None, "age_years_at_least": 4.0})


def test_alt_money_goes_only_where_it_is_not_locked():
    """Ці гроші — подушка: продукт із періодом розморозки тут не годиться."""
    flex = _offer("OKX", "ATOM", 0.12)
    locked = _offer("Bybit", "ATOM", 0.30, kind="staking", redeem_days=30,
                    product="On-Chain Earn")
    assert [o.exchange for o in alt_offers([flex, locked], "ATOM")] == ["OKX"]


def test_alt_block_is_silent_without_a_suitable_coin(tmp_path):
    st = _budget_store(tmp_path, "w3b4.db", 500.0)
    trust = {"ZORA": {"facts": {"rank": 380, "age_years": 0.8}}}
    lines, coin = asyncio.run(alerts._alt_news(_svc(st), [_offer("OKX", "ZORA", 3.0)], trust))
    assert lines == [] and coin == ""


# =====================================================================
# C8. Копіювальний блок із питаннями до підтримки біржі
# =====================================================================

def test_promo_quote_is_english_copyable_and_has_no_amounts():
    p = Promo("Bybit", "HODL BTC & Enjoy 10% APR", "http://x/1", 1)
    quote = promo_quote(p, {"coin": "BTC", "apr_pct": 10, "locked_days": 3,
                            "period_end": "2026-09-20"})
    assert quote.startswith(QUOTE_OPEN) and quote.endswith(QUOTE_CLOSE)
    assert "Advertised rate: 10% APR" in quote and "Stated lock-up: 3 days" in quote
    assert "Questions to support:" in quote and "auto-renew" in quote
    # Жодних її сум: блок пишеться, щоб просто скопіювати й вставити в підтримку.
    assert "$" not in quote and "500" not in quote


def test_quote_becomes_a_copy_block_and_the_rest_is_escaped():
    """У заголовках бірж трапляється «&» — без екранування повідомлення б не пішло."""
    text = "Bybit: «HODL BTC & Enjoy»\n" + QUOTE_OPEN + "Promo: a & b <x>" + QUOTE_CLOSE
    out = to_html(text)
    assert out.startswith("Bybit: «HODL BTC &amp; Enjoy»")
    assert out.endswith("<pre>Promo: a &amp; b &lt;x&gt;</pre>")
    assert "<pre>" in out and out.count("<pre>") == 1


def test_long_earn_answer_is_split_without_breaking_a_quote():
    """Блоки для підтримки додали до /earn майже 2000 знаків — межа стала близькою."""
    from app.formatting import split_message

    quote = QUOTE_OPEN + "Promo: x\n\nQuestions to support:\n1. a\n2. b" + QUOTE_CLOSE
    text = "\n\n".join([f"• рядок {i} " + "y" * 400 + "\n" + quote for i in range(6)])
    parts = split_message(text, limit=1200)
    assert len(parts) > 1
    # Найважливіше: жоден копіювальний блок не розірваний навпіл.
    for part in parts:
        assert part.count(QUOTE_OPEN) == part.count(QUOTE_CLOSE)
        assert to_html(part).count("<pre>") == to_html(part).count("</pre>")
    # І нічого не загубилось по дорозі.
    joined = "\n\n".join(parts)
    for i in range(6):
        assert f"• рядок {i} " in joined
    # Короткий текст не ріжемо взагалі.
    assert split_message("коротко", limit=1200) == ["коротко"]


def test_every_promo_carries_its_own_quote():
    from app.formatting import promo_lines

    promos = [Promo("Bybit", f"Earn {i}% APR on USDT", f"http://x/{i}", i) for i in range(2)]
    ex = {p.title: {"title_uk": f"Акція {i}", "what": "-", "kind": "deposit",
                    "verdict": "maybe", "why": "-", "coin": "USDT", "apr_pct": 8}
          for i, p in enumerate(promos)}
    entries = promo_lines(promos, ex)
    assert len(entries) == 2
    assert all(QUOTE_OPEN in e and "Questions to support:" in e for e in entries)


# =====================================================================
# C4. Тижневий огляд у неділю — і лише коли є про що казати
# =====================================================================

def test_weekly_review_only_on_sunday_and_only_with_news(tmp_path):
    st = _store(tmp_path, "w3week.db")
    st.earn_deposit_add({"coin": "USDT", "amount": 500, "exchange": "Bybit",
                         "product": "Savings", "apr": 0.0677, "kind": KIND_FLEX},
                        STATUS_ACTIVE)
    st.set_earn_amount(500.0)
    offers = [_offer("Bybit", "USDT", 0.04, product="Savings")]
    svc = _svc(st, _FakeEarn(offers))

    monday = datetime.date(2026, 9, 7)
    bot = _Bot()
    asyncio.run(alerts.weekly_earn_review(bot, svc, monday))
    assert bot.sent == []                       # не неділя — мовчимо

    sunday = datetime.date(2026, 9, 6)
    st.set_earn_rates({"USDT": 0.0677}, today=sunday - datetime.timedelta(days=8))
    bot = _Bot()
    asyncio.run(alerts.weekly_earn_review(bot, svc, sunday))
    assert len(bot.sent) == 1
    text = bot.sent[0]["text"]
    assert "Тижневий огляд вкладів" in text
    assert "$500 USDT — Bybit Savings, 6.77%" in text
    assert "6.77% → 4.00%" in text and "впала" in text
    # Тільки earn: ні докупок крипти, ні внесків на IBKR сюди не мішаємо.
    assert "докуп" not in text and "ордер" not in text and "IBKR" not in text
    assert "крипта й ETF рахуються окремо" in text

    # Другий раз того ж тижня — нема про що казати, отже мовчимо.
    bot = _Bot()
    asyncio.run(alerts.weekly_earn_review(bot, svc, sunday))
    assert bot.sent == []


def test_weekly_review_is_silent_when_nothing_changed(tmp_path):
    st = _store(tmp_path, "w3week2.db")
    svc = _svc(st, _FakeEarn([_offer("Bybit", "USDT", 0.05, product="Savings")]))
    bot = _Bot()
    asyncio.run(alerts.weekly_earn_review(bot, svc, datetime.date(2026, 9, 6)))
    assert bot.sent == []


def test_weekly_review_names_exact_sums():
    changes = [{"coin": "USDT", "was": 0.0677, "now": 0.04, "money_year": -13.85}]
    best = {"USDT": _offer("OKX", "USDT", 0.055)}
    deps = [{"coin": "USDT", "amount": 500.0, "exchange": "Bybit",
             "product": "Savings", "apr": 0.0677}]
    text = weekly_review_text(deps, changes, best)
    assert "Разом ~$33.85/рік" in text
    assert "на твоїх грошах це $13.85/рік" in text
    assert "USDT — 5.50% (OKX Simple Earn)" in text
