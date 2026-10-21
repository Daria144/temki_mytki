"""Друга хвиля правок earn: нічого не губиться, гроші під заявками не чіпаємо,
ліміт сповіщень, тижнева база ставок, нагадування про дедлайн, «не цікаво».

Мережі немає: біржі й порадника мокаємо.

Запуск: ./.venv/bin/python -m pytest tests/test_earn_wave2.py
"""
import asyncio
import datetime
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import alerts, service
from app.alerts import (
    MAX_MESSAGE,
    MAX_PER_BLOCK,
    _compose,
    _earn_amounts,
    _minus_reserved,
    _new_products,
    _promo_news,
    _rate_news,
    earn_check,
    promo_deadline_check,
)
from app.config import config
from app.earn import (
    MAJORS,
    RISK_HIGH,
    RISK_MAJOR,
    Offer,
    Promo,
    Tier,
    classify_risk,
    rate_limit_note,
)
from app.storage import EARN_MAX_PER_DAY, Storage

TODAY = datetime.date(2026, 9, 5)


def _offer(exchange, coin, apr, tiers=None, kind="flex", product="Simple Earn"):
    return Offer(exchange, coin, apr, kind, classify_risk(coin), None, 0.0,
                 tiers, product, f"Заробити → {product}")


def _store(tmp_path, name="w2.db") -> Storage:
    st = Storage(str(tmp_path / name))
    st.init()
    return st


class _Bot:
    def __init__(self, fail: int = 0):
        self.sent = []
        self.fail = fail          # скільки перших надсилань «зламати»

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        if self.fail > 0:
            self.fail -= 1
            raise RuntimeError("Telegram недоступний")
        self.sent.append({"text": text, "kb": reply_markup, "kw": kwargs})


# --- A5: великі монети більше не в одному кошику з токеном тижневого віку ---

def test_major_coins_are_no_longer_the_riskiest():
    assert classify_risk("SOL") == RISK_MAJOR
    assert classify_risk("XRP") == RISK_MAJOR
    assert classify_risk("BTC") == "mid" and classify_risk("USDT") == "low"
    # Свіжий токен, якого немає у списку великих, лишається найризикованішим.
    assert "ZORA" not in MAJORS and classify_risk("ZORA") == RISK_HIGH


def test_risky_block_no_longer_advertises_major_coins():
    """У блок «незнайомі монети» великі альти більше не потрапляють."""
    from app.earn import risky_coins
    sol = _offer("OKX", "SOL", 0.09)
    junk = _offer("OKX", "ZORA", 3.9)
    assert [o.coin for o in risky_coins([sol, junk])] == ["ZORA"]


# --- A2: гроші під відкритими заявками не чіпаємо ---

def test_reserved_money_is_not_offered_to_earn():
    held = {"USDT": 1000.0, "BTC": 500.0}
    out, free = _minus_reserved(held, 1000.0, 400.0)
    assert free == 600.0
    assert out["USDT"] == 600.0        # той самий гаманець, показаний двічі
    assert out["BTC"] == 500.0         # монет це не стосується


def test_earn_amounts_subtracts_open_orders(tmp_path):
    """Долари, що чекають на просадку BTC, у earn не пропонуються."""
    st = _store(tmp_path)
    st.set_earn_amount(1000.0)

    class _Rec:
        pass

    # Кладемо відкриту заявку на $300 напряму в журнал ордерів.
    with st._conn() as c:
        c.execute(
            "INSERT INTO orders(rec_id, order_index, coin, price, qty, notional, status, created_at)"
            " VALUES(1, 0, 'BTC', 100000, 0.003, 300, 'open', 0)"
        )
    assert st.reserved_for_buys() == 300.0

    svc = types.SimpleNamespace(storage=st, portfolio=None, market=None)
    amounts, free = asyncio.run(_earn_amounts(svc))
    assert free == 700.0
    assert amounts.get("USDT") == 700.0


# --- A4: одна сума на всі стейбли більше не множиться ---

def test_free_money_is_counted_once_across_stables(tmp_path):
    st = _store(tmp_path, "w2b.db")
    st.set_earn_amount(500.0)
    svc = types.SimpleNamespace(storage=st, portfolio=None, market=None)
    amounts, free = asyncio.run(_earn_amounts(svc))
    stable_total = sum(v for c, v in amounts.items() if c in ("USDT", "USDC", "DAI", "FDUSD"))
    assert stable_total == 500.0 == free      # а не 500 × кількість тикерів


# --- A3: поріг вигоди для нових продуктів ---

def test_new_product_below_gain_threshold_is_skipped():
    """Продукт на $0.50 за рік не варто показувати — поріг той самий, що в стрибках."""
    st = _FakeStore(products={"OKX:USDT:flex"})
    svc = types.SimpleNamespace(storage=st)
    tiny = _offer("Bybit", "USDT", 0.005, [Tier(0, None, 0.005)], product="Savings")
    lines, coins, done = _new_products(svc, [tiny], {}, 100.0)   # $100 під 0.5% = $0.50
    assert lines == [] and coins == set()
    assert done == {"Bybit:USDT:flex"}          # опрацьовано, повторно не крутиться

    rich = _offer("Bybit", "USDT", 0.06, [Tier(0, None, 0.06)], product="Savings")
    lines, coins, _ = _new_products(svc, [rich], {}, 1000.0)     # $60/рік
    assert coins == {"USDT"} and len(lines) == 1


# --- B10: висока ставка показує, на яку суму вона діє ---

def test_high_rate_shows_the_amount_limit():
    tiered = _offer("Bybit", "USDT", 0.10, [Tier(0, 500, 0.10), Tier(500, None, 0.01)])
    assert rate_limit_note(tiered) == "на перші $500"
    # OKX ліміту не показує взагалі — про це чесніше сказати, ніж мовчати.
    atom = _offer("OKX", "ATOM", 0.169)
    assert "не показує" in rate_limit_note(atom)
    # На спокійній ставці нічого не дописуємо — це був би зайвий шум.
    assert rate_limit_note(_offer("OKX", "BTC", 0.005)) == ""


def test_rate_jump_line_carries_the_limit():
    o = _offer("Bybit", "USDT", 0.10, [Tier(0, 500, 0.10), Tier(500, None, 0.01)],
               product="Savings")
    lines = _rate_news({"USDT": (0.10, o)}, {"USDT": 0.03}, {"USDT": 2000.0})
    assert "на перші $500" in lines[0]


# --- B3: повільне зростання за тиждень у сповіщення не йде ---

def test_slow_weekly_growth_does_not_spam():
    """Повзуче зростання без різкого стрибка — не щоденна новина."""
    o = _offer("Bybit", "USDT", 0.06, [Tier(0, None, 0.06)], product="Savings")
    rates = {"USDT": (0.06, o)}
    yesterday = {"USDT": 0.055}
    week_ago = {"USDT": 0.04}
    assert _rate_news(rates, yesterday, {"USDT": 2000.0}) == []
    assert _rate_news(rates, yesterday, {"USDT": 2000.0}, week_prev=week_ago) == []


def test_rate_history_keeps_a_weekly_base(tmp_path):
    st = _store(tmp_path, "w2c.db")
    st.set_earn_rates({"USDT": 0.04}, today=TODAY - datetime.timedelta(days=7))
    st.set_earn_rates({"USDT": 0.055}, today=TODAY - datetime.timedelta(days=1))
    st.set_earn_rates({"USDT": 0.06}, today=TODAY)
    assert st.get_earn_rates() == {"USDT": 0.06}          # «минулого разу»
    assert st.earn_rates_days_ago(5, TODAY) == {"USDT": 0.04}   # «тиждень тому»
    # Історії ще немає — тижневого тригера просто нема, і це не помилка.
    assert _store(tmp_path, "w2d.db").earn_rates_days_ago(5, TODAY) == {}


# --- B2: денний ліміт earn-сповіщень ---

def test_daily_notification_quota(tmp_path):
    st = _store(tmp_path, "w2e.db")
    assert st.earn_quota_left(TODAY) == EARN_MAX_PER_DAY == 1
    st.mark_earn_notified(TODAY)
    assert st.earn_quota_left(TODAY) == 0
    # Новий день — лічильник із нуля.
    assert st.earn_quota_left(TODAY + datetime.timedelta(days=1)) == EARN_MAX_PER_DAY


# --- B1 + A1: нічого не губиться ---

def test_long_message_is_split_and_nothing_is_lost():
    entries = [f"• запис номер {i} " + "y" * 900 for i in range(6)]
    messages = _compose([("🆕 Нові пропозиції на біржах:", entries)])
    assert len(messages) > 1
    assert all(len(m) <= MAX_MESSAGE for m in messages)
    joined = "\n".join(messages)
    for i in range(6):
        assert f"• запис номер {i} " in joined
    assert "(продовження)" in joined


class _FakeStore:
    """Мінімальне сховище для earn_check: усе в памʼяті, нічого зайвого."""

    def __init__(self, promos=None, products=None):
        self.promos = set(promos or [])
        self.products = set(products or [])
        self.rates = {}
        self.notified = 0
        self.mutes = set()
        self.watch = {}
        self.mute_ids = {}

    def alerts_enabled(self):
        return True

    def seen_promos(self):
        return set(self.promos)

    def mark_promos_seen(self, keys):
        self.promos |= set(keys)

    def seen_products(self):
        return set(self.products)

    def mark_products_seen(self, keys):
        self.products |= set(keys)

    def get_earn_rates(self):
        return dict(self.rates)

    def set_earn_rates(self, rates, today=None):
        self.rates = dict(rates)

    def earn_rates_days_ago(self, days=5, today=None):
        return {}

    def earn_quota_left(self, today=None):
        return max(EARN_MAX_PER_DAY - self.notified, 0)

    def mark_earn_notified(self, today=None):
        self.notified += 1

    def earn_muted(self):
        return set(self.mutes)

    def earn_mute_register(self, target):
        mid = len(self.mute_ids) + 1
        self.mute_ids[mid] = target
        return mid

    def earn_watch_promos(self):
        return dict(self.watch)

    def earn_watch_promo(self, key, info):
        self.watch.setdefault(key, dict(info, reminded=False))

    def earn_promo_reminded(self, key):
        self.watch[key]["reminded"] = True

    def earn_drop_watched(self, keys):
        for k in keys:
            self.watch.pop(k, None)

    def get_earn_amount(self):
        return 1000.0

    def reserved_for_buys(self):
        return 0.0

    def filled_positions(self):
        return {}

    # --- журнал вкладів і бюджет: третя хвиля навчила earn_check зазирати й сюди ---

    def earn_deposits(self, status="active"):
        return []

    def earn_deposit_rate_alerted(self, dep_id, apr):
        pass

    def get_budget(self):
        return {"month": "", "amount": 0.0, "carry": 0.0}

    def deployed_in_month(self, month):
        return 0.0

    def reserved_in_month(self, month):
        return 0.0


class _FakeEarn:
    def __init__(self, offers, promos=()):
        self._offers = offers
        self._promos = list(promos)

    async def offers(self):
        return self._offers

    async def promos(self):
        return self._promos

    async def promo_bodies(self, promos, limit=3):
        return {p.key: f"текст про {p.title}" for p in promos[:limit]}


def _svc(store, earn):
    return types.SimpleNamespace(storage=store, earn=earn, portfolio=None,
                                 market=None, coin_facts=None)


def _fake_promo_ai(answers):
    async def explain(titles, context=None, bodies=None):
        return {t: dict(answers[t]) for t in titles if t in answers}
    return explain


def test_nothing_is_marked_seen_until_it_is_actually_sent(monkeypatch):
    """Головний баг: показували 3, а позначали переглянутим усе, і решта зникала."""
    monkeypatch.setattr(config, "owner_id", 1)
    promos = [Promo("Bybit", f"Earn {i}% APR on USDT savings", f"http://x/{i}", i)
              for i in range(9)]
    ai = {p.title: {"title_uk": f"Акція {i}", "what": "-", "kind": "deposit",
                    "verdict": "maybe", "why": "-", "is_offer": True}
          for i, p in enumerate(promos)}
    monkeypatch.setattr(service, "explain_promos", _fake_promo_ai(ai))

    store = _FakeStore(promos={"Bybit:щось давнє"})
    svc = _svc(store, _FakeEarn([], promos))
    bot = _Bot()
    asyncio.run(alerts.earn_check(bot, svc))

    text = "\n".join(m["text"] for m in bot.sent)
    shown = [i for i in range(9) if f"Акція {i}»" in text]
    assert len(shown) == MAX_PER_BLOCK          # показали стільки, скільки дозволено
    # І позначили рівно стільки ж — решта лишилась на наступний раз.
    assert len(store.promos) == 1 + MAX_PER_BLOCK
    for i in shown:
        assert promos[i].key in store.promos


def test_nothing_is_marked_when_telegram_fails(monkeypatch):
    """Повідомлення не пішло — знахідки лишаються новими, а не згорають."""
    monkeypatch.setattr(config, "owner_id", 1)
    p = Promo("Bybit", "Earn 8% APR on USDT savings", "http://x/1", 1)
    monkeypatch.setattr(service, "explain_promos", _fake_promo_ai(
        {p.title: {"title_uk": "Акція", "what": "-", "kind": "deposit",
                   "verdict": "maybe", "why": "-", "is_offer": True}}))

    store = _FakeStore(promos={"Bybit:давнє"})
    svc = _svc(store, _FakeEarn([], [p]))
    asyncio.run(alerts.earn_check(_Bot(fail=5), svc))
    assert p.key not in store.promos
    assert store.notified == 0


def test_non_offers_are_dropped_but_remembered(monkeypatch):
    """B6: релевантність вирішує AI по тексту; сміття більше не крутиться щодня."""
    monkeypatch.setattr(config, "owner_id", 1)
    news = Promo("Bybit", "Bybit partners with a football club", "http://x/n", 2)
    real = Promo("Bybit", "New savings product launched", "http://x/r", 1)
    monkeypatch.setattr(service, "explain_promos", _fake_promo_ai({
        news.title: {"title_uk": "Партнерство", "what": "-", "kind": "other",
                     "verdict": "skip", "why": "-", "is_offer": False},
        real.title: {"title_uk": "Новий вклад", "what": "-", "kind": "deposit",
                     "verdict": "maybe", "why": "-", "is_offer": True},
    }))
    store = _FakeStore(promos={"Bybit:давнє"})
    svc = _svc(store, _FakeEarn([], [news, real]))
    bot = _Bot()
    asyncio.run(alerts.earn_check(bot, svc))

    text = "\n".join(m["text"] for m in bot.sent)
    assert "Новий вклад" in text and "Партнерство" not in text
    # Обидві опрацьовані: сміття теж запамʼятали, щоб не питати про нього щодня.
    assert news.key in store.promos and real.key in store.promos


def test_promo_without_earn_words_still_gets_through(monkeypatch):
    """Раніше акція проходила лише за словами apr/earn — тепер вирішує текст."""
    p = Promo("Bybit", "Hold your coins with us this autumn", "http://x/a", 1)
    assert not p.relevant          # старий словниковий фільтр її не пускав
    assert p.candidate             # а новий — пускає до порадника


def test_obvious_noise_never_reaches_the_advisor():
    assert not Promo("OKX", "Notice on the delisting of XYZ", "", 0).candidate
    assert not Promo("OKX", "System maintenance on Sep 10", "", 0).candidate
    assert not Promo("Bybit", "[MENA Exclusive] Deposit and earn", "", 0).candidate


# --- B7: одне нагадування про дедлайн, поза денним лімітом ---

def test_deadline_reminder_is_sent_once(monkeypatch):
    monkeypatch.setattr(config, "owner_id", 1)
    store = _FakeStore()
    store.watch = {
        "Bybit:soon": {"exchange": "Bybit", "title": "Тримай BTC під 10%",
                       "url": "http://x/1", "end": "2026-09-06", "reminded": False},
    }
    svc = _svc(store, _FakeEarn([]))
    bot = _Bot()
    asyncio.run(promo_deadline_check(bot, svc, TODAY))
    assert len(bot.sent) == 1
    assert "лишилось 1 дн." in bot.sent[0]["text"]
    assert "http://x/1" in bot.sent[0]["text"]

    # Друге нагадування про ту саму акцію не йде.
    asyncio.run(promo_deadline_check(bot, svc, TODAY))
    assert len(bot.sent) == 1
    # І денний ліміт earn-сповіщень воно не витрачає.
    assert store.notified == 0 and store.earn_quota_left() == EARN_MAX_PER_DAY


def test_deadline_reminder_is_quiet_while_it_is_early_and_drops_expired(monkeypatch):
    monkeypatch.setattr(config, "owner_id", 1)
    store = _FakeStore()
    store.watch = {
        "Bybit:later": {"exchange": "Bybit", "title": "Ще нескоро",
                        "url": "", "end": "2026-09-30", "reminded": False},
        "Bybit:over": {"exchange": "Bybit", "title": "Уже минула",
                       "url": "", "end": "2026-08-28", "reminded": False},
    }
    bot = _Bot()
    asyncio.run(promo_deadline_check(bot, _svc(store, _FakeEarn([])), TODAY))
    assert bot.sent == []
    assert "Bybit:over" not in store.watch      # протерміновану прибрали з памʼяті
    assert "Bybit:later" in store.watch


def test_deadline_reminder_survives_a_telegram_failure(monkeypatch):
    """Збій Telegram не має спалити ЄДИНЕ нагадування."""
    monkeypatch.setattr(config, "owner_id", 1)
    store = _FakeStore()
    store.watch = {"Bybit:soon": {"exchange": "Bybit", "title": "Акція",
                                  "url": "", "end": "2026-09-06", "reminded": False}}
    svc = _svc(store, _FakeEarn([]))
    asyncio.run(promo_deadline_check(_Bot(fail=1), svc, TODAY))
    assert store.watch["Bybit:soon"]["reminded"] is False

    bot = _Bot()
    asyncio.run(promo_deadline_check(bot, svc, TODAY))
    assert len(bot.sent) == 1


# --- B4: «не цікаво» ---

def test_mute_button_uses_a_short_id(tmp_path):
    """У callback_data влазить 64 байти, а ключ акції — це 80+ знаків."""
    from app.keyboards import build_earn_mute_kb
    st = _store(tmp_path, "w2f.db")
    long_key = "promo:Bybit:🐂 Bull Incoming? HODL BTC & Enjoy 10% APR with Bybit Earn!"
    assert len(long_key.encode()) > 64
    mute_id = st.earn_mute_register(long_key)
    assert len(f"em:{mute_id}".encode()) <= 64
    assert st.earn_mute_target(mute_id) == long_key

    kb = build_earn_mute_kb(st, ["ATOM", "TRX"])
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert all(len(d.encode()) <= 64 for d in datas)
    assert all(d.startswith("em:") for d in datas)


def test_muted_coin_stops_being_advised(monkeypatch, tmp_path):
    st = _store(tmp_path, "w2g.db")
    st.earn_mute("coin:ATOM")
    assert "coin:ATOM" in st.earn_muted()

    monkeypatch.setattr(config, "owner_id", 1)
    store = _FakeStore(products={"OKX:USDT:flex"})
    store.mutes = {"coin:ATOM"}
    atom = _offer("OKX", "ATOM", 0.169)
    svc = _svc(store, _FakeEarn([atom]))
    bot = _Bot()
    asyncio.run(alerts.earn_check(bot, svc))
    assert "ATOM" not in "\n".join(m["text"] for m in bot.sent)


def test_mute_expires_and_does_not_last_forever(tmp_path):
    """Глушимо на кілька місяців, а не назавжди: ринок міняється."""
    st = _store(tmp_path, "w2h.db")
    st.earn_mute("coin:SOL", days=0)
    assert st.earn_muted() == set()


# --- B5: надійність незнайомої монети ---

class _FakeFacts:
    def __init__(self, data):
        self.data = data
        self.calls = 0

    async def profiles(self, coins):
        self.calls += 1
        return {c: self.data[c] for c in coins if c in self.data}


def test_unknown_coin_gets_a_reliability_note(monkeypatch):
    """Для незнайомої монети ризик самої монети важливіший за ставку."""
    monkeypatch.setattr(config, "gemini_api_key", "")
    facts = _FakeFacts({"ZORA": {"coin": "ZORA", "name": "Zora", "rank": 380,
                                 "volume_usd": 4_000_000, "age_years": 0.8}})
    trust = asyncio.run(service.coin_reliability(facts, ["ZORA"]))
    assert trust["ZORA"]["trust"] == "shaky"
    assert trust["ZORA"]["by_rule"] is True

    from app.formatting import coin_trust_lines
    text = "\n".join(coin_trust_lines(trust, 500.0, {"ZORA": 0.25}))
    assert "🔴 ZORA" in text
    assert "~$125 за рік" in text         # скільки заробить...
    assert "мінус $200" in text           # ...і скільки може втратити


def test_risk_is_named_even_without_a_known_rate():
    """Ставку по незнайомій монеті знаємо не завжди, а ризик падіння — завжди."""
    from app.formatting import coin_trust_lines
    trust = {"CP": {"summary": "Дрібна монета.", "trust": "shaky"}}
    line = coin_trust_lines(trust, 500.0)[0]
    assert "мінус $200" in line and "Якщо вкласти сюди $500" in line


def test_same_coin_is_not_advertised_twice():
    """ATOM на двох біржах — це та сама новина, а не дві."""
    st = _FakeStore(products={"OKX:USDT:flex"})
    svc = types.SimpleNamespace(storage=st)
    a = _offer("Bybit", "ATOM", 0.17, product="On-Chain Earn")
    b = _offer("OKX", "ATOM", 0.169, product="Simple Earn")
    lines, coins, done = _new_products(svc, [a, b], {}, 1000.0)
    assert len(lines) == 1 and "17.00%" in lines[0]
    assert coins == {"ATOM"}
    assert "OKX:ATOM:flex" in done          # другий варіант теж опрацьовано


def test_summary_does_not_repeat_the_ticker():
    """Порадник починає речення з тікера, а він уже є в рядку — прибираємо."""
    from app.formatting import coin_trust_lines
    trust = {"CP": {"summary": "CP — це дуже дрібна монета.", "trust": "shaky"}}
    assert coin_trust_lines(trust)[0] == "🔴 CP — це дуже дрібна монета."


def test_age_falls_back_to_the_price_history():
    """genesis_date у CoinGecko майже завжди порожній — беремо нижню межу віку."""
    from app.market import _years_since
    assert _years_since(None) is None
    assert _years_since("2020-01-01T00:00:00.000Z") > 6
    facts = {"coin": "X", "rank": 80, "volume_usd": 1e7, "age_years": None,
             "age_years_at_least": 4.0}
    assert "щонайменше 4 р." in service._rule_summary(facts)
    assert service._rule_trust(facts) == "ok"
    # А молода монета лишається сумнівною, навіть якщо місце пристойне.
    assert service._rule_trust(dict(facts, age_years_at_least=0.5)) == "shaky"


def test_main_money_cannot_be_muted_by_accident(tmp_path):
    """Заглушити USDT або BTC означало б осліпнути там, де найважливіше бачити."""
    from app.keyboards import build_earn_mute_kb
    st = _store(tmp_path, "w2i.db")
    kb = build_earn_mute_kb(st, ["USDT", "BTC", "ATOM"])
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert labels == ["🔕 ATOM — не цікаво"]
    assert build_earn_mute_kb(st, ["USDT", "ETH"]) is None


def test_promos_are_separated_by_exactly_one_blank_line(monkeypatch):
    """Порожні рядки додає складальник повідомлення — раніше вони подвоювались."""
    monkeypatch.setattr(config, "owner_id", 1)
    from app.formatting import promo_lines
    promos = [Promo("Bybit", f"Earn {i}", f"http://x/{i}", i) for i in range(2)]
    ex = {p.title: {"title_uk": f"Акція {i}", "what": "-", "kind": "deposit",
                    "verdict": "maybe", "why": "-"} for i, p in enumerate(promos)}
    entries = promo_lines(promos, ex)
    assert len(entries) == 2                    # один елемент = одна акція
    text = "\n".join(_compose([("🎁 Нові акції:", entries)]))
    assert "\n\n\n" not in text


def test_reliability_survives_a_dead_coingecko():
    """Довідки немає — блок просто мовчить, решта сповіщення працює."""
    class _Dead:
        async def profiles(self, coins):
            raise RuntimeError("429")

    assert asyncio.run(service.coin_reliability(_Dead(), ["ZORA"])) == {}
    assert asyncio.run(service.coin_reliability(None, ["ZORA"])) == {}
