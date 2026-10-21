"""Перевірки розрахунків для /earn на синтетичних ставках (без мережі).

Запуск: ./.venv/bin/python -m pytest tests/test_earn.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.earn import (
    Offer,
    Promo,
    Tier,
    classify_risk,
    fallback_explain,
    is_giveaway_promo,
    ladder,
    optimize,
    rate_steps,
    smart_amounts,
)
from app.formatting import format_earn


def _stable(exchange, coin, tiers, min_amount=None, redeem_days=0.0):
    base = tiers[-1].apr if tiers else 0.0
    return Offer(exchange, coin, base, "flex", classify_risk(coin),
                 min_amount, redeem_days, tiers)


def test_per_year_uses_tiers_not_flat_rate():
    """Дохід рахується по сходинках: перші $200 під 10%, решта під 2%."""
    o = _stable("Bybit", "USDT", [Tier(0, 200, 0.10), Tier(200, None, 0.02)])
    assert o.per_year(200) == 20.0
    # $500 = 200*10% + 300*2% = 20 + 6
    assert o.per_year(500) == 26.0


def test_optimize_splits_across_two_exchanges():
    """Коли на кожній біржі висока ставка лише на першу сотню — гроші треба ділити."""
    a = _stable("Bybit", "USDT", [Tier(0, 100, 0.10), Tier(100, None, 0.01)])
    b = _stable("OKX", "USDT", [Tier(0, 100, 0.08), Tier(100, None, 0.01)])
    res = optimize([a, b], 200, max_places=2)
    assert len(res["parts"]) == 2
    # 100*10% + 100*8% = 18, а не 100*10% + 100*1% = 11
    assert round(res["income"], 6) == 18.0


def test_optimize_respects_max_places():
    """Обмеження на кількість місць виконується навіть коли третя біржа вигідна."""
    offers = [
        _stable("A", "USDT", [Tier(0, 100, 0.10), Tier(100, None, 0.01)]),
        _stable("B", "USDT", [Tier(0, 100, 0.09), Tier(100, None, 0.01)]),
        _stable("C", "USDT", [Tier(0, 100, 0.08), Tier(100, None, 0.01)]),
    ]
    res = optimize(offers, 300, max_places=2)
    assert len(res["parts"]) <= 2
    places = {p["offer"].exchange for p in res["parts"]}
    assert places == {"A", "B"}


def test_optimize_skips_offer_below_minimum():
    """Пропозицію з мінімумом більшим за суму не пропонуємо взагалі."""
    rich = _stable("Bybit", "USDT", [Tier(0, None, 0.20)], min_amount=1000)
    plain = _stable("OKX", "USDT", [Tier(0, None, 0.03)])
    res = optimize([rich, plain], 100)
    assert [p["offer"].exchange for p in res["parts"]] == ["OKX"]


def test_marginal_rate_falls_as_amount_grows():
    """Головне питання користувача: з якої суми докладати вже немає сенсу."""
    o = _stable("Bybit", "USDT", [Tier(0, 200, 0.10), Tier(200, None, 0.01)])
    rows = ladder([o], [200.0, 400.0])
    assert round(rows[0]["effective_apr"], 4) == 0.10
    # Другі $200 працюють уже під 1% — саме це й треба показати.
    assert round(rows[1]["marginal_apr"], 4) == 0.01


def test_smart_amounts_hits_tier_boundaries():
    """Сходинки сум мають попадати на межі, де змінюється ставка."""
    a = _stable("Bybit", "USDT", [Tier(0, 200, 0.10), Tier(200, None, 0.01)])
    b = _stable("OKX", "USDT", [Tier(0, 300, 0.05), Tier(300, None, 0.01)])
    amounts = smart_amounts([a, b])
    assert 200.0 in amounts
    assert 500.0 in amounts  # 200 під 10% + 300 під 5%


def test_rate_steps_sorted_by_rate():
    a = _stable("Bybit", "USDT", [Tier(0, 100, 0.10)])
    b = _stable("OKX", "USDT", [Tier(0, 100, 0.20)])
    assert rate_steps([a, b])[0][2].exchange == "OKX"


def test_fallback_explain_flags_trading_promos():
    """Без AI користувач мусить бачити головне: чи треба торгувати."""
    promos = [
        Promo("Bybit", "Trade & earn up to 50 USDT", "", 0),
        Promo("OKX", "Savings: earn higher APR on USDT", "", 0),
    ]
    ex = fallback_explain(promos)
    assert ex["Trade & earn up to 50 USDT"]["kind"] == "trading"
    assert ex["Savings: earn higher APR on USDT"]["kind"] == "deposit"


def test_giveaway_promos_are_not_alert_candidates():
    """Розіграші й «новим користувачам USDT» не мають потрапляти в «Нові акції»."""
    raffle = Promo("Bybit", "Розіграш 2000 USDT для нових користувачів Bybit", "", 0)
    assert is_giveaway_promo(raffle.title)
    assert not raffle.candidate
    deposit = Promo("OKX", "Savings: earn higher APR on USDT", "", 0)
    assert deposit.candidate


def test_format_earn_shows_ladder_and_split():
    offers = [
        _stable("Bybit", "USDT", [Tier(0, 200, 0.10), Tier(200, None, 0.01)]),
        _stable("OKX", "USDT", [Tier(0, None, 0.05)]),
    ]
    text = format_earn(offers, [], free_usd=500.0)
    assert "ВІЛЬНІ ДОЛАРИ" in text
    assert "твоя сума" in text
    assert "Bybit · USDT" in text and "OKX · USDT" in text
    assert "Не фінансова порада" in text


def test_format_earn_without_amount_asks_for_it():
    offers = [_stable("OKX", "USDT", [Tier(0, None, 0.05)])]
    text = format_earn(offers, [])
    assert "/earn 500" in text


def test_format_earn_marks_unclear_withdrawal_for_staking():
    """OKX не віддає строк виводу публічно — не вигадуємо «будь-коли»."""
    staking = Offer("OKX", "ETH", 0.02, "staking", "mid", None, 0.0, None)
    holdings = {"ETH": {"qty": 0.03, "value": 100.0}}
    text = format_earn([staking], [], holdings=holdings, prices={"ETH": 3300.0})
    assert "уточни на біржі" in text


def test_format_earn_hides_unreachable_target():
    """Не радимо докласти втричі більше заради копійок."""
    cheap = Offer("OKX", "BTC", 0.005, "flex", "mid", None, 0.0, None)
    far = Offer("Bybit", "BTC", 0.008, "staking", "mid", 0.1, 0.0, None)
    holdings = {"BTC": {"qty": 0.001, "value": 120.0}}
    text = format_earn([cheap, far], [], holdings=holdings, prices={"BTC": 120000.0})
    assert "не варте того" in text


def test_optimize_zero_amount_is_safe():
    o = _stable("OKX", "USDT", [Tier(0, None, 0.05)])
    res = optimize([o], 0)
    assert res["income"] == 0.0 and res["parts"] == []


# --- сповіщення: пороги, регіони, розрахунок вигоди ---

def test_region_filter_drops_foreign_promos():
    """Акції для інших регіонів з Угорщини недоступні."""
    mena = Promo("Bybit", "[MENA Exclusive] Deposit & trade to earn 50 USDT APR", "", 0)
    ok = Promo("Bybit", "HODL BTC & Enjoy 10% APR with Bybit Earn", "", 0)
    assert mena.about_earn and not mena.region_ok and not mena.relevant
    assert ok.relevant


def test_region_filter_ignores_words_inside_other_words():
    """«cis» не має ловитись усередині «decision»."""
    assert Promo("OKX", "Earn APR: our decision on precise rates", "", 0).region_ok


def test_rate_jump_needs_two_points_or_double():
    from app.alerts import _jumped
    assert _jumped(0.06, 0.04)      # рівно 2 пункти, попри похибку дробових
    assert _jumped(0.09, 0.03)      # утричі
    assert not _jumped(0.05, 0.04)  # 1 пункт — замало
    assert not _jumped(0.035, 0.03)


def test_rate_news_skips_gain_below_threshold():
    """Менше $20/рік не варте сповіщення."""
    from app.alerts import _rate_news
    offer = _stable("Bybit", "USDT", [Tier(0, None, 0.06)])
    rates = {"USDT": (0.06, offer)}
    assert _rate_news(rates, {"USDT": 0.03}, {"USDT": 200.0}) == []       # $6/рік
    assert len(_rate_news(rates, {"USDT": 0.03}, {"USDT": 2000.0})) == 1  # $60/рік


def test_rate_news_puts_own_money_before_hypothetical():
    """Спершу те, де гроші вже лежать, і лише потім чужі монети."""
    from app.alerts import _rate_news
    mine = _stable("Bybit", "USDT", [Tier(0, None, 0.06)])
    alt = Offer("OKX", "XLM", 0.20, "flex", "high", None, 0.0, [Tier(0, None, 0.20)])
    rates = {"USDT": (0.06, mine), "XLM": (0.20, alt)}
    prev = {"USDT": 0.03, "XLM": 0.05}
    lines = _rate_news(rates, prev, {"USDT": 2000.0}, free_usd=2000.0)
    assert "USDT" in lines[0] and "XLM" in lines[1]


def test_upgrade_news_reports_idle_money():
    """З ключами бот порівнює, де гроші лежать і де могли б."""
    import asyncio
    import time
    import types
    from app.alerts import _upgrade_news
    from app.portfolio import SPOT, Holding, Portfolio

    class Reader:
        async def fetch(self, prices=None):
            return Portfolio(holdings=[Holding("OKX", "USDT", 1200.0, 1200.0, SPOT)],
                             failed=[], ts=time.time())

    offers = [_stable("Bybit", "USDT", [Tier(0, None, 0.05)])]
    svc = types.SimpleNamespace(portfolio=Reader())
    lines, coins, sigs = asyncio.run(_upgrade_news(svc, offers))
    assert len(lines) == 1 and coins == {"USDT"}
    assert "лежить без діла" in lines[0] and "переказ" in lines[0]
    assert sigs == {"USDT": _upgrade_signature_for(offers, "USDT")}


def _upgrade_signature_for(offers, coin):
    """Хелпер лише для тестів: рахує підпис так само, як і сам код."""
    from app.alerts import _upgrade_signature
    import time
    from app.portfolio import SPOT, Holding, Portfolio, upgrades
    pf = Portfolio(holdings=[Holding("OKX", coin, 1200.0, 1200.0, SPOT)], failed=[], ts=time.time())
    ups = upgrades(pf, offers, 0.0)
    return _upgrade_signature(ups[0])


def test_upgrade_news_empty_without_keys():
    """Без ключів цей блок просто порожній — решта блоків працює далі."""
    import asyncio
    import types
    from app.alerts import _upgrade_news
    assert asyncio.run(_upgrade_news(types.SimpleNamespace(portfolio=None), [])) == ([], set(), {})


def test_upgrade_news_skips_already_shown_offer():
    """Та сама пропозиція другий день підряд — мовчимо, гроші нікуди не зникли."""
    import asyncio
    import time
    import types
    from app.alerts import _upgrade_news, _upgrade_signature
    from app.portfolio import SPOT, Holding, Portfolio, upgrades

    class Reader:
        async def fetch(self, prices=None):
            return Portfolio(holdings=[Holding("OKX", "USDT", 1200.0, 1200.0, SPOT)],
                             failed=[], ts=time.time())

    class _Store:
        def __init__(self):
            self.seen = {}

        def earn_seen_upgrades(self):
            return self.seen

    offers = [_stable("Bybit", "USDT", [Tier(0, None, 0.05)])]
    pf = Portfolio(holdings=[Holding("OKX", "USDT", 1200.0, 1200.0, SPOT)], failed=[], ts=time.time())
    sig = _upgrade_signature(upgrades(pf, offers, 0.0)[0])
    st = _Store()
    st.seen["USDT"] = sig
    svc = types.SimpleNamespace(portfolio=Reader(), storage=st)
    lines, coins, sigs = asyncio.run(_upgrade_news(svc, offers))
    assert lines == []                 # уже показували — вдруге не пишемо
    assert coins == {"USDT"}           # але інші блоки про цю монету теж мовчать
    assert sigs == {"USDT": sig}


# --- назви продуктів: щоб було зрозуміло, що відкривати на біржі ---

def test_offer_label_includes_product_name():
    o = Offer("Bybit", "USDT", 0.05, "flex", "low", product="Savings")
    assert o.label == "Bybit Savings"


def test_offer_label_falls_back_to_exchange():
    """Якщо назви немає — не лишаємо порожнечу й зайвий пробіл."""
    assert Offer("OKX", "USDT", 0.03, "flex", "low").label == "OKX"


def test_format_earn_shows_product_name_and_section():
    """У розкладі суми має бути видно і назву продукту, і де його шукати."""
    o = Offer("Bybit", "USDT", 0.05, "flex", "low", None, 0.0,
              [Tier(0, None, 0.05)], "Savings", "Earn → Savings")
    text = format_earn([o], [], free_usd=1000.0)
    assert "Bybit Savings" in text
    assert "шукати: Earn → Savings" in text


def test_rate_news_line_names_the_product():
    from app.alerts import _rate_news
    o = Offer("Bybit", "USDT", 0.06, "flex", "low", None, 0.0,
              [Tier(0, None, 0.06)], "Savings", "Earn → Savings")
    lines = _rate_news({"USDT": (0.06, o)}, {"USDT": 0.03}, {"USDT": 2000.0})
    assert "Bybit Savings" in lines[0]


def test_upgrade_news_names_both_sides():
    """Видно, звідки й куди перекладати — за назвами, а не за біржами."""
    import asyncio
    import time
    import types
    from app.alerts import _upgrade_news
    from app.portfolio import EARN, Holding, Portfolio

    class Reader:
        async def fetch(self, prices=None):
            return Portfolio(
                holdings=[Holding("OKX", "USDT", 3000.0, 3000.0, EARN, 0.01, "Simple Earn")],
                failed=[], ts=time.time())

    target = Offer("Bybit", "USDT", 0.05, "flex", "low", None, 0.0,
                   [Tier(0, None, 0.05)], "Savings", "Earn → Savings")
    lines, _, _ = asyncio.run(_upgrade_news(types.SimpleNamespace(portfolio=Reader()), [target]))
    assert "Simple Earn" in lines[0] and "Bybit Savings" in lines[0]
    assert "шукати: Earn → Savings" in lines[0]


def test_every_live_offer_has_a_product_name():
    """Жодне джерело не має лишати пропозицію без назви."""
    import asyncio
    from app.earn import Earn
    offers = asyncio.run(Earn().offers())
    if not offers:                      # біржа може бути недоступна — не валимо тест
        return
    assert all(o.product for o in offers)
    assert {o.product for o in offers} <= {"Simple Earn", "ETH Staking", "Savings", "On-Chain Earn"}


# --- моніторинг ринку: нові продукти й складання блоків ---

class _Store:
    """Мінімальне сховище для перевірки памʼяті про продукти."""
    def __init__(self):
        self.products = set()

    def seen_products(self):
        return set(self.products)

    def mark_products_seen(self, keys):
        self.products |= set(keys)


def test_first_run_is_silent_and_only_remembers():
    """Перший запуск: усе виглядає новим — мовчимо, інакше буде простирадло."""
    import types
    from app.alerts import _new_products
    st = _Store()
    offers = [_stable("Bybit", "USDT", [Tier(0, None, 0.05)]),
              _stable("OKX", "BTC", [Tier(0, None, 0.01)])]
    lines, coins, done = _new_products(types.SimpleNamespace(storage=st), offers, {}, 0.0)
    assert lines == [] and coins == set() and done == set()
    assert len(st.products) == 2          # але стан запамʼятали


def test_second_run_reports_only_the_new_one():
    import types
    from app.alerts import _new_products
    st = _Store()
    svc = types.SimpleNamespace(storage=st)
    first = [_stable("Bybit", "USDT", [Tier(0, None, 0.05)])]
    _new_products(svc, first, {}, 0.0)                       # запамʼятали
    later = first + [_stable("OKX", "ETH", [Tier(0, None, 0.03)])]
    lines, coins, done = _new_products(svc, later, {"ETH": 2000.0}, 0.0)
    assert coins == {"ETH"} and len(lines) == 1
    assert "OKX" in lines[0] and "ETH" in lines[0]
    assert done == {"OKX:ETH:flex"}


def test_new_product_of_known_coin_still_counts_as_new():
    """Та сама монета, але інший продукт — це теж нова можливість."""
    import types
    from app.alerts import _new_products
    st = _Store()
    svc = types.SimpleNamespace(storage=st)
    _new_products(svc, [_stable("Bybit", "ETH", [Tier(0, None, 0.02)])], {}, 0.0)
    staking = Offer("Bybit", "ETH", 0.05, "staking", "mid", None, 0.0,
                    [Tier(0, None, 0.05)], "On-Chain Earn", "Earn → On-Chain Earn")
    lines, coins, _ = _new_products(svc, [staking], {"ETH": 1000.0}, 0.0)
    assert coins == {"ETH"} and "On-Chain Earn" in lines[0]


def test_new_altcoin_product_carries_the_warning():
    import types
    from app.alerts import _new_products
    st = _Store()
    svc = types.SimpleNamespace(storage=st)
    _new_products(svc, [_stable("Bybit", "USDT", [Tier(0, None, 0.05)])], {}, 0.0)
    alt = Offer("OKX", "XLM", 0.20, "flex", "high", None, 0.0,
                [Tier(0, None, 0.20)], "Simple Earn", "Заробити → Simple Earn")
    lines, _, _ = _new_products(svc, [alt], {}, 1000.0)
    assert "може просісти в ціні" in lines[0]


def test_new_products_ignores_unwatched_coins():
    """Дрібні монети не мають роздувати памʼять і сповіщення."""
    import types
    from app.alerts import _new_products
    st = _Store()
    svc = types.SimpleNamespace(storage=st)
    junk = Offer("OKX", "ZORA", 3.9, "flex", "high", None, 0.0, [Tier(0, None, 3.9)], "Simple Earn")
    _new_products(svc, [junk], {}, 0.0)
    assert st.products == set()


def test_blocks_are_additive_not_exclusive():
    """Чотири блоки складаються в одне повідомлення, у порядку важливості."""
    from app.alerts import _compose
    text = "\n".join(_compose([
        ("💡 Є краще місце для твоїх грошей:", ["• рядок про гроші"]),
        ("🆕 Нові пропозиції на біржах:", ["• рядок про новинку"]),
        ("📈 Ставки підросли:", ["• рядок про стрибок"]),
        ("🎁 Нові акції:", ["• рядок про акцію"]),
    ]))
    for part in ("краще місце", "Нові пропозиції", "Ставки підросли", "Нові акції"):
        assert part in text
    assert text.index("краще місце") < text.index("Нові пропозиції") < text.index("Ставки підросли")


def test_compose_splits_instead_of_dropping():
    """Задовге повідомлення розбивається на кілька — не губиться НІЧОГО.

    Раніше найменш важливий блок просто викидався, хоч його вміст уже був
    позначений переглянутим і більше ніколи б не зʼявився.
    """
    from app.alerts import _compose, MAX_MESSAGE
    big = ["x" * 1200] * 3
    messages = _compose([
        ("💡 Є краще місце для твоїх грошей:", ["• важливе"]),
        ("🆕 Нові пропозиції на біржах:", big),
        ("📈 Ставки підросли:", big),
        ("🎁 Нові акції:", big),
    ])
    assert len(messages) > 1
    assert all(len(m) <= MAX_MESSAGE for m in messages)
    joined = "\n".join(messages)
    assert "важливе" in joined and "Нові акції" in joined
    assert joined.count("x" * 1200) == 9        # усі девʼять рядків доїхали
    assert joined.rstrip().endswith("/earn")    # підпис лише в кінці останнього


def test_rate_news_skips_coins_already_covered():
    """Монету не повторюємо в двох блоках підряд."""
    from app.alerts import _rate_news
    o = _stable("Bybit", "USDT", [Tier(0, None, 0.06)])
    rates, prev, amounts = {"USDT": (0.06, o)}, {"USDT": 0.03}, {"USDT": 2000.0}
    assert len(_rate_news(rates, prev, amounts)) == 1
    assert _rate_news(rates, prev, amounts, skip={"USDT"}) == []


# --- розклад по кількох місцях у самому сповіщенні ---

def _reader(holdings):
    import time
    from app.portfolio import Portfolio

    class Reader:
        async def fetch(self, prices=None):
            return Portfolio(holdings=list(holdings), failed=[], ts=time.time())

    return Reader()


def test_upgrade_news_offers_the_split_when_one_move_is_not_enough():
    """Один переїзд дає $17.7 і мовчав би; два місця дають $21.9 — про це кажемо."""
    import asyncio
    import types
    from app.alerts import _upgrade_news
    from app.portfolio import SPOT, Holding

    offers = [
        Offer("Bybit", "USDT", 0.0176, "flex", "low", 1.5, 0.0,
              [Tier(0.0, 200.0, 0.0676), Tier(200.0, None, 0.0176)], "Savings",
              "Earn → Savings"),
        Offer("OKX", "USDT", 0.035, "flex", "low", product="Simple Earn"),
    ]
    svc = types.SimpleNamespace(portfolio=_reader([Holding("OKX", "USDT", 439.47, 439.47, SPOT)]))
    lines, coins, _ = asyncio.run(_upgrade_news(svc, offers))
    assert coins == {"USDT"} and len(lines) == 1
    text = lines[0]
    assert "А ще краще розкласти" in text
    assert "$200 у Bybit Savings під 6.76%" in text
    assert "$239 у OKX Simple Earn під 3.50%" in text
    assert "~$22/рік зверху" in text


def test_upgrade_news_shows_the_rate_that_matches_the_money():
    """Ставку називаємо ту, з якої вийшла сума: базові 1.76% суперечили б їй."""
    import asyncio
    import types
    from app.alerts import _upgrade_news
    from app.portfolio import SPOT, Holding

    tiered = Offer("Bybit", "USDT", 0.0176, "flex", "low", 1.5, 0.0,
                   [Tier(0.0, 200.0, 0.0676), Tier(200.0, None, 0.0176)], "Savings")
    svc = types.SimpleNamespace(portfolio=_reader([Holding("OKX", "USDT", 2000.0, 2000.0, SPOT)]))
    lines, _, _ = asyncio.run(_upgrade_news(svc, [tiered]))
    # 200*6.76% + 1800*1.76% = $45.2 -> 2.26% на всю суму, а не 1.76% і не 6.76%.
    assert "2.26% на цю суму" in lines[0]
    assert "1.76%" not in lines[0] and "6.76%" not in lines[0]
