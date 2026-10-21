"""Локальні перевірки шару читання залишків (app/portfolio.py) — без мережі й без ключів.

Запуск: ./.venv/bin/python -m pytest tests/test_portfolio.py  (або просто як скрипт).
"""
import asyncio
import base64
import hashlib
import hmac
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import portfolio as pf
from app.earn import Offer, Tier
from app.portfolio import EARN, SPOT, Holding, Portfolio, PortfolioReader, upgrades


class _Cfg:
    """Мінімальний двійник config із такими самими властивостями."""

    def __init__(self, okx=False, bybit=False):
        self.okx_api_key = "okx-key" if okx else ""
        self.okx_api_secret = "okx-secret" if okx else ""
        self.okx_passphrase = "okx-pass" if okx else ""
        self.bybit_api_key = "bybit-key" if bybit else ""
        self.bybit_api_secret = "bybit-secret" if bybit else ""

    @property
    def has_okx(self):
        return bool(self.okx_api_key and self.okx_api_secret and self.okx_passphrase)

    @property
    def has_bybit(self):
        return bool(self.bybit_api_key and self.bybit_api_secret)


# --- фікстури відповідей (зліплені з прикладів у документації бірж) ---

OKX_TRADING = {
    "code": "0",
    "msg": "",
    "data": [{
        "totalEq": "10600",
        "details": [
            {"ccy": "USDT", "eq": "1000", "eqUsd": "1000.5", "availBal": "800",
             "cashBal": "1000", "frozenBal": "200"},
            {"ccy": "BTC", "eq": "0.1", "eqUsd": "6000", "availBal": "0.1",
             "cashBal": "0.1", "frozenBal": "0"},
            {"ccy": "SHIB", "eq": "0", "eqUsd": "0", "availBal": "0", "cashBal": "0"},
        ],
    }],
}

OKX_FUNDING = {
    "code": "0", "msg": "",
    "data": [{"availBal": "37.11827078", "bal": "37.11827078", "ccy": "ETH", "frozenBal": "0"}],
}

OKX_SAVINGS = {
    "code": "0", "msg": "",
    "data": [{"earnings": "0.001", "redemptAmt": "", "rate": "0.0100000000000000",
              "ccy": "USDT", "amt": "11.0010737453457821", "loanAmt": "11.001", "pendingAmt": "0"}],
}

OKX_STAKING = {
    "code": "0", "msg": "",
    "data": [{
        "ordId": "2413499", "ccy": "DOT", "productId": "101", "state": "1",
        "protocol": "Polkadot", "protocolType": "defi", "term": "0", "apy": "0.1014",
        "investData": [{"ccy": "DOT", "amt": "2"}],
        "earningData": [{"ccy": "DOT", "earningType": "0", "earnings": "0.106"}],
        "purchasedTime": "1729839328000", "fastRedemptionData": [],
    }],
}

BYBIT_UNIFIED = {
    "retCode": 0, "retMsg": "OK",
    "result": {"list": [{
        "accountType": "UNIFIED", "totalEquity": "3.31216591",
        "coin": [
            {"coin": "BTC", "equity": "0.5", "usdValue": "30000", "walletBalance": "0.5",
             "locked": "0", "unrealisedPnl": "0", "spotBorrow": "0"},
            {"coin": "USDT", "equity": "500", "usdValue": "500", "walletBalance": "500",
             "locked": "100", "unrealisedPnl": "0", "spotBorrow": "0"},
        ],
    }]},
    "retExtInfo": {}, "time": 1690872862481,
}

BYBIT_FUNDING = {
    "retCode": 0, "retMsg": "success",
    "result": {"memberId": "XXXX", "accountType": "FUND",
               "balance": [{"coin": "USDC", "transferBalance": "250", "walletBalance": "250",
                            "bonus": ""}]},
    "retExtInfo": {}, "time": 1675866354913,
}

BYBIT_EARN_FLEX = {
    "retCode": 0, "retMsg": "",
    "result": {"list": [
        {"coin": "USDT", "productId": "8", "amount": "1200", "totalPnl": "0.02",
         "claimableYield": "0", "availableAmount": "1200", "freezeDetails": []},
        # Bybit віддає і повністю викуплені позиції — вони не мають потрапити в портфель.
        {"coin": "ETH", "productId": "9", "amount": "0", "claimableYield": "0"},
    ]},
    "retExtInfo": {}, "time": 1739944577575,
}

BYBIT_EARN_ONCHAIN = {"retCode": 0, "retMsg": "", "result": {"list": []},
                      "retExtInfo": {}, "time": 1739944577575}


def _reader(okx=False, bybit=False, okx_map=None, bybit_map=None, fail=()):
    """Читач із підміненими транспортними методами — жодного справжнього запиту."""
    r = PortfolioReader(_Cfg(okx=okx, bybit=bybit))
    okx_map = okx_map or {}
    bybit_map = bybit_map or {}

    async def fake_okx(path, params=None):
        if path in fail:
            raise RuntimeError("тест: джерело впало")
        j = okx_map.get(path, {"code": "0", "data": []})
        code = str(j.get("code") or "0")
        if code != "0":
            raise RuntimeError(f"OKX {path}: код {code}")
        return j.get("data") or []

    async def fake_bybit(path, params=None):
        if path in fail:
            raise RuntimeError("тест: джерело впало")
        key = (path, (params or {}).get("category") or (params or {}).get("accountType"))
        j = bybit_map.get(key) or bybit_map.get(path) or {"retCode": 0, "result": {}}
        if int(j.get("retCode") or 0) != 0:
            raise RuntimeError(f"Bybit {path}: код {j.get('retCode')}")
        return j.get("result") or {}

    async def no_prices():
        raise RuntimeError("тест: публічні ціни недоступні")

    r._okx = fake_okx
    r._bybit = fake_bybit
    r._public_prices = no_prices
    return r


def _full_reader(**kw):
    return _reader(
        okx=True, bybit=True,
        okx_map={
            "/api/v5/account/balance": OKX_TRADING,
            "/api/v5/asset/balances": OKX_FUNDING,
            "/api/v5/finance/savings/balance": OKX_SAVINGS,
            "/api/v5/finance/staking-defi/orders-active": OKX_STAKING,
        },
        bybit_map={
            ("/v5/account/wallet-balance", "UNIFIED"): BYBIT_UNIFIED,
            ("/v5/asset/transfer/query-account-coins-balance", "FUND"): BYBIT_FUNDING,
            ("/v5/earn/position", "FlexibleSaving"): BYBIT_EARN_FLEX,
            ("/v5/earn/position", "OnChain"): BYBIT_EARN_ONCHAIN,
        },
        **kw,
    )


# --- підпис ---

def test_okx_signing_matches_docs():
    # Формат таймстемпа з документації OKX: 2020-12-08T09:08:57.715Z
    ts = pf.okx_timestamp(1607418537.7159)
    assert ts == "2020-12-08T09:08:57.715Z", ts

    # Query-рядок мусить бути частиною requestPath, інакше підпис невалідний.
    assert pf.okx_request_path("/api/v5/account/balance", {"ccy": "BTC"}) == \
        "/api/v5/account/balance?ccy=BTC"
    assert pf.okx_request_path("/api/v5/asset/balances") == "/api/v5/asset/balances"
    assert pf.okx_request_path("/api/v5/asset/balances", {}) == "/api/v5/asset/balances"

    # Приклад prehash-рядка з документації.
    path = pf.okx_request_path("/api/v5/account/balance", {"ccy": "BTC"})
    assert pf.okx_prehash(ts, "get", path) == \
        "2020-12-08T09:08:57.715ZGET/api/v5/account/balance?ccy=BTC"

    # Підпис = base64(HMAC-SHA256) і він детермінований.
    secret = "22582BD0CFF14C41EDBF1AB98506286D"
    sign = pf.okx_sign(secret, ts, "GET", path)
    expected = base64.b64encode(hmac.new(
        secret.encode(), pf.okx_prehash(ts, "GET", path).encode(), hashlib.sha256
    ).digest()).decode()
    assert sign == expected
    assert sign == pf.okx_sign(secret, ts, "GET", path)
    # Інший секрет — інший підпис.
    assert sign != pf.okx_sign(secret + "x", ts, "GET", path)
    print("OKX signing OK:", ts, sign[:12] + "...")


def test_bybit_signing_matches_docs():
    # Рядок для підпису з прикладу в документації Bybit.
    plain = pf.bybit_prehash(
        "1658384314791", "XXXXXXXXXX",
        "category=option&symbol=BTC-29JUL22-25000-C", "5000",
    )
    assert plain == "1658384314791XXXXXXXXXX5000category=option&symbol=BTC-29JUL22-25000-C"

    secret = "abc123"
    sign = pf.bybit_sign(secret, "1658384314791", "XXXXXXXXXX",
                         "category=option&symbol=BTC-29JUL22-25000-C")
    expected = hmac.new(secret.encode(), plain.encode(), hashlib.sha256).hexdigest()
    assert sign == expected
    assert sign == sign.lower() and len(sign) == 64  # hex у нижньому регістрі
    # recv_window за замовчуванням — 5000.
    assert pf.BYBIT_RECV_WINDOW == "5000"
    print("Bybit signing OK:", sign[:12] + "...")


# --- парсинг відповідей ---

def test_parsing_builds_holdings():
    p = asyncio.run(_full_reader().fetch())
    assert not p.failed, p.failed
    by = p.by_coin()

    # OKX торговий: беремо availBal (800), а eqUsd масштабуємо з eq (1000).
    okx_usdt = [h for h in by["USDT"] if h.exchange == "OKX" and h.where == SPOT]
    assert len(okx_usdt) == 1
    assert okx_usdt[0].amount == 800.0
    assert abs(okx_usdt[0].usd_value - 800.4) < 1e-6

    # Нульові залишки не потрапляють у портфель.
    assert "SHIB" not in by

    # OKX фандинг: ETH без оцінки від біржі, але ціну взяли з Bybit (BTC/ETH різні —
    # тут ціни немає ніде, тому лишається None).
    okx_eth = [h for h in by["ETH"] if h.exchange == "OKX"]
    assert len(okx_eth) == 1 and abs(okx_eth[0].amount - 37.11827078) < 1e-9
    assert okx_eth[0].usd_value is None  # публічні ціни в тесті недоступні

    # OKX Simple Earn: ставка як частка на рік.
    okx_earn = [h for h in by["USDT"] if h.exchange == "OKX" and h.where == EARN]
    assert len(okx_earn) == 1 and okx_earn[0].current_apr == 0.01
    assert okx_earn[0].product == "Simple Earn"

    # OKX стейкінг: сума з investData, ставка з apy, назва з protocol.
    dot = by["DOT"][0]
    assert dot.amount == 2.0 and dot.current_apr == 0.1014 and dot.product == "Polkadot"

    # Bybit unified: locked віднімається, usdValue масштабується.
    bb_usdt = [h for h in by["USDT"] if h.exchange == "Bybit" and h.where == SPOT]
    assert len(bb_usdt) == 1 and bb_usdt[0].amount == 400.0
    assert abs(bb_usdt[0].usd_value - 400.0) < 1e-6

    bb_btc = [h for h in by["BTC"] if h.exchange == "Bybit"][0]
    assert bb_btc.amount == 0.5 and bb_btc.usd_value == 30000.0 and bb_btc.price == 60000.0

    # Bybit earn: нульова (викуплена) позиція відкидається, ставки біржа не дає.
    bb_earn = [h for h in by["USDT"] if h.exchange == "Bybit" and h.where == EARN]
    assert len(bb_earn) == 1 and bb_earn[0].amount == 1200.0
    assert bb_earn[0].current_apr is None and bb_earn[0].product == "Savings"
    assert all(h.coin != "ETH" or h.exchange != "Bybit" for h in p.in_earn())

    # Стейбли без оцінки від біржі вважаємо 1:1 (USDC із фандингу Bybit).
    usdc = by["USDC"][0]
    assert usdc.amount == 250.0 and usdc.usd_value == 250.0
    print("parsing OK: holdings =", len(p.holdings), "total_usd =", round(p.total_usd(), 2))


def test_portfolio_helpers():
    p = asyncio.run(_full_reader().fetch())
    assert len(p.idle()) == 6      # OKX: USDT, BTC, ETH; Bybit: BTC, USDT, USDC
    assert len(p.in_earn()) == 3   # OKX Simple Earn + DOT, Bybit FlexibleSaving
    # free_stables: OKX 800.4 + Bybit 400 + USDC 250 (гроші в earn не рахуються)
    assert abs(p.free_stables() - 1450.4) < 1e-6
    assert abs(p.amount_of("usdt", SPOT) - 1200.0) < 1e-6
    assert abs(p.amount_of("USDT") - 2411.0010737453) < 1e-6
    # ETH і DOT без ціни не завищують підсумок (враховуються як 0).
    expected = 800.4 + 6000 + 30000 + 400 + 250 + 11.0010737453 + 1200
    assert abs(p.total_usd() - expected) < 1e-6, p.total_usd()
    assert not p.empty
    print("helpers OK: idle =", len(p.idle()), "earn =", len(p.in_earn()))


def test_no_keys_is_quiet():
    p = asyncio.run(_reader().fetch())
    assert p.empty and p.holdings == [] and p.failed == []
    assert p.total_usd() == 0.0 and p.free_stables() == 0.0
    assert upgrades(p, [Offer("Bybit", "USDT", 0.10, "flex", "low")]) == []

    # Лише один налаштований ключ — читаємо тільки цю біржу.
    r = _reader(okx=True, okx_map={"/api/v5/account/balance": OKX_TRADING})
    p2 = asyncio.run(r.fetch())
    assert {h.exchange for h in p2.holdings} == {"OKX"}
    print("no-keys OK")


def test_broken_source_is_skipped():
    r = _full_reader(fail=("/api/v5/account/balance", "/v5/earn/position"))
    p = asyncio.run(r.fetch())
    # Три джерела впали (OKX баланс + два виклики /v5/earn/position), решта дійшла.
    assert len(p.failed) == 3, p.failed
    assert not any(h.exchange == "OKX" and h.where == SPOT and h.coin == "USDT"
                   for h in p.holdings)
    assert any(h.exchange == "Bybit" and h.coin == "USDT" for h in p.holdings)
    assert p.total_usd() > 0

    # Помилка бізнес-коду (напр. IP не в whitelist) теж лише пропускає джерело.
    r2 = _reader(okx=True, okx_map={
        "/api/v5/account/balance": {"code": "50110", "msg": "IP not in whitelist", "data": []},
        "/api/v5/asset/balances": OKX_FUNDING,
    })
    p2 = asyncio.run(r2.fetch())
    assert p2.failed and [h.coin for h in p2.holdings] == ["ETH"]
    print("broken-source OK:", p.failed)


def test_cache_hits_once():
    r = _full_reader()
    calls = {"n": 0}
    orig = r._okx

    async def counting(path, params=None):
        calls["n"] += 1
        return await orig(path, params)

    r._okx = counting

    async def twice():
        await r.fetch()
        await r.fetch()

    asyncio.run(twice())
    assert calls["n"] == 4, calls  # 4 джерела OKX, другий fetch — з кешу
    print("cache OK:", calls["n"])


# --- upgrades() ---

def test_upgrades_idle_money_gets_offer():
    p = Portfolio([Holding("OKX", "USDT", 1000.0, 1000.0, SPOT)])
    offers = [
        Offer("OKX", "USDT", 0.03, "flex", "low"),
        Offer("Bybit", "USDT", 0.08, "flex", "low"),
    ]
    rows = upgrades(p, offers)
    assert len(rows) == 1
    r = rows[0]
    assert r["from_apr"] == 0.0 and r["from_where"] == SPOT
    assert r["to_exchange"] == "Bybit" and r["to_apr"] == 0.08
    assert abs(r["gain_year_usd"] - 80.0) < 1e-6
    assert r["same_exchange"] is False and r["risk"] == "low"
    print("upgrades idle OK:", round(r["gain_year_usd"], 2))


def test_upgrades_threshold_20_usd():
    # $100 під 5% = $5 на рік — нижче порогу, підказки бути не має.
    small = Portfolio([Holding("OKX", "USDT", 100.0, 100.0, SPOT)])
    offers = [Offer("Bybit", "USDT", 0.05, "flex", "low")]
    assert upgrades(small, offers) == []
    # Той самий випадок з нижчим порогом — вигода є.
    rows = upgrades(small, offers, min_gain_year=1.0)
    assert len(rows) == 1 and abs(rows[0]["gain_year_usd"] - 5.0) < 1e-6
    # Рівно на порозі — беремо.
    exact = Portfolio([Holding("OKX", "USDT", 400.0, 400.0, SPOT)])
    assert len(upgrades(exact, offers)) == 1
    print("upgrades threshold OK")


def test_upgrades_uses_tiered_rates():
    p = Portfolio([Holding("Bybit", "USDT", 1000.0, 1000.0, SPOT)])
    tiered = Offer("Bybit", "USDT", 0.10, "flex", "low",
                   tiers=[Tier(0.0, 500.0, 0.10), Tier(500.0, None, 0.02)])
    flat = Offer("OKX", "USDT", 0.07, "flex", "low")
    rows = upgrades(p, [tiered, flat])
    # На $1000 сходинки дають 500*10% + 500*2% = $60, а рівні 7% — $70.
    # Наївне amount*apr вибрало б Bybit (10% > 7%) і збрехало б на $40.
    assert len(rows) == 1
    assert rows[0]["to_exchange"] == "OKX"
    assert abs(rows[0]["gain_year_usd"] - 70.0) < 1e-6
    # На $500 сходинка ще не закінчилась — виграє Bybit ($50 проти $35).
    small = Portfolio([Holding("Bybit", "USDT", 500.0, 500.0, SPOT)])
    rows2 = upgrades(small, [tiered, flat])
    assert rows2[0]["to_exchange"] == "Bybit"
    assert abs(rows2[0]["gain_year_usd"] - 50.0) < 1e-6
    print("upgrades tiers OK:", round(rows[0]["gain_year_usd"], 2))


def test_upgrades_from_earn_position():
    p = Portfolio([Holding("OKX", "USDT", 2000.0, 2000.0, EARN, 0.01, "Simple Earn")])
    offers = [
        Offer("OKX", "USDT", 0.01, "flex", "low"),      # там, де вже стоїмо
        Offer("Bybit", "USDT", 0.06, "flex", "low"),
    ]
    rows = upgrades(p, offers)
    assert len(rows) == 1
    # Вигода рахується як РІЗНИЦЯ, а не повний дохід нової пропозиції.
    assert abs(rows[0]["gain_year_usd"] - 100.0) < 1e-6
    assert abs(rows[0]["from_year_usd"] - 20.0) < 1e-6
    assert abs(rows[0]["to_year_usd"] - 120.0) < 1e-6

    # Якщо ніде не краще — порожньо.
    assert upgrades(p, [Offer("Bybit", "USDT", 0.011, "flex", "low")]) == []


def test_upgrades_apr_from_offers_when_exchange_hides_it():
    """Bybit не віддає ставку позиції — беремо публічну ставку тієї ж біржі."""
    p = Portfolio([Holding("Bybit", "USDT", 5000.0, 5000.0, EARN, None, "FlexibleSaving")])
    offers = [
        Offer("Bybit", "USDT", 0.04, "flex", "low"),
        Offer("OKX", "USDT", 0.05, "flex", "low"),
    ]
    rows = upgrades(p, offers)
    assert len(rows) == 1 and rows[0]["from_apr"] == 0.04
    assert abs(rows[0]["gain_year_usd"] - 50.0) < 1e-6

    # Ставки нема ні в позиції, ні в пропозиціях цієї біржі -> не вгадуємо.
    assert upgrades(p, [Offer("OKX", "USDT", 0.05, "flex", "low")]) == []
    print("upgrades hidden-apr OK")


def test_upgrades_respects_min_amount_and_missing_price():
    # min_amount вище за наявну суму -> пропозиція не годиться.
    p = Portfolio([Holding("OKX", "USDT", 1000.0, 1000.0, SPOT)])
    assert upgrades(p, [Offer("Bybit", "USDT", 0.50, "flex", "low", min_amount=5000.0)]) == []

    # Немає оцінки в доларах і монета не стейбл -> вигоду в грошах не порахувати.
    blind = Portfolio([Holding("OKX", "DOT", 1000.0, None, SPOT)])
    assert upgrades(blind, [Offer("Bybit", "DOT", 0.20, "flex", "high")]) == []
    # З ціною — рахуємо (1000 DOT * $4 * 20% = $800).
    seen = Portfolio([Holding("OKX", "DOT", 1000.0, 4000.0, SPOT)])
    rows = upgrades(seen, [Offer("Bybit", "DOT", 0.20, "flex", "high")])
    assert len(rows) == 1 and abs(rows[0]["gain_year_usd"] - 800.0) < 1e-6
    assert rows[0]["risk"] == "high"
    print("upgrades guards OK")


def test_upgrades_merges_spot_across_accounts_and_sorts():
    # $600 у торговому + $600 у фандингу = $1200: поріг рахується від суми.
    p = Portfolio([
        Holding("OKX", "USDT", 600.0, 600.0, SPOT),
        Holding("OKX", "USDT", 600.0, 600.0, SPOT),
        Holding("Bybit", "BTC", 0.5, 30000.0, SPOT),
    ])
    offers = [
        Offer("Bybit", "USDT", 0.05, "flex", "low"),
        Offer("Bybit", "BTC", 0.01, "flex", "mid"),
    ]
    rows = upgrades(p, offers)
    assert len(rows) == 2
    # Сортування за вигодою: BTC $300 > USDT $60.
    assert rows[0]["coin"] == "BTC" and abs(rows[0]["gain_year_usd"] - 300.0) < 1e-6
    assert rows[1]["coin"] == "USDT" and rows[1]["amount"] == 1200.0
    assert abs(rows[1]["gain_year_usd"] - 60.0) < 1e-6
    print("upgrades merge/sort OK")


def test_upgrades_on_real_fixture_portfolio():
    p = asyncio.run(_full_reader().fetch())
    offers = [
        Offer("Bybit", "USDT", 0.09, "flex", "low"),
        Offer("OKX", "USDT", 0.04, "flex", "low"),
        Offer("Bybit", "BTC", 0.02, "flex", "mid"),
        Offer("OKX", "DOT", 0.05, "flex", "high"),   # гірше за поточні 10.14%
    ]
    rows = upgrades(p, offers)
    coins = [r["coin"] for r in rows]
    assert "DOT" not in coins  # поточна ставка вища за все запропоноване
    assert "BTC" in coins and "USDT" in coins
    assert rows == sorted(rows, key=lambda r: -r["gain_year_usd"])
    print("upgrades on fixture OK:", [(r["coin"], round(r["gain_year_usd"], 1)) for r in rows])


# --- розклад по кількох місцях: коли один переїзд не дотягує до порога ---

def _live_usdt_offers():
    """Ставки, як вони були на живих даних: 6.76% лише на перші $200."""
    tiered = Offer("Bybit", "USDT", 0.0176, "flex", "low", 1.5, 0.0,
                   [Tier(0.0, 200.0, 0.0676), Tier(200.0, None, 0.0176)], "Savings")
    flat = Offer("OKX", "USDT", 0.035, "flex", "low", product="Simple Earn")
    return [tiered, flat]


def test_split_beats_a_single_move_and_crosses_the_threshold():
    """$439 без діла: один переїзд дає $17.7 (мовчимо), два місця — $21.9 (кажемо).

    Це справжній випадок із її гаманця: наївне 6.77% × $439 обіцяло б $29, а
    сходинка на 6.76% закінчується на $200, і далі ставка падає до 1.76%.
    """
    p = Portfolio([Holding("OKX", "USDT", 439.47, 439.47, SPOT)])
    rows = upgrades(p, _live_usdt_offers())
    assert len(rows) == 1, "розклад по двох місцях мусив перетнути поріг"
    r = rows[0]
    # Один переїзд сам по собі порога НЕ перетинає — і цифра не роздута.
    assert abs(r["gain_year_usd"] - 17.74) < 0.05
    split = r["split"]
    assert split is not None and len(split["parts"]) == 2
    assert abs(split["gain_year_usd"] - 21.90) < 0.05
    # Перші $200 — у дорогу сходинку, решта — туди, де рівна ставка вища.
    first, second = split["parts"]
    assert first["offer"].label == "Bybit Savings" and abs(first["amount"] - 200.0) < 1e-6
    assert second["offer"].label == "OKX Simple Earn"
    print("split OK:", round(r["gain_year_usd"], 2), "->", round(split["gain_year_usd"], 2))


def test_no_split_when_one_place_is_already_the_best():
    """Рівні ставки без сходинок — розкладати нема сенсу, і ми про це мовчимо."""
    p = Portfolio([Holding("OKX", "USDT", 1000.0, 1000.0, SPOT)])
    rows = upgrades(p, [Offer("Bybit", "USDT", 0.08, "flex", "low"),
                        Offer("OKX", "USDT", 0.03, "flex", "low")])
    assert len(rows) == 1 and rows[0]["split"] is None


def test_split_does_not_inflate_the_single_move_number():
    """gain_year_usd лишається вигодою ОДНОГО переїзду — інакше дві різні правди."""
    p = Portfolio([Holding("Bybit", "USDT", 1000.0, 1000.0, SPOT)])
    tiered = Offer("Bybit", "USDT", 0.10, "flex", "low",
                   tiers=[Tier(0.0, 500.0, 0.10), Tier(500.0, None, 0.02)])
    flat = Offer("OKX", "USDT", 0.07, "flex", "low")
    r = upgrades(p, [tiered, flat])[0]
    assert abs(r["gain_year_usd"] - 70.0) < 1e-6        # 1000 * 7%
    assert abs(r["split"]["gain_year_usd"] - 85.0) < 1e-6  # 500*10% + 500*7%


def test_effective_rate_matches_the_money_it_promises():
    """Показувати базові 1.76% поруч із сумою по сходинках — це брехати двічі."""
    p = Portfolio([Holding("OKX", "USDT", 439.47, 439.47, SPOT)])
    r = upgrades(p, _live_usdt_offers())[0]
    assert abs(r["to_effective_apr"] - r["to_year_usd"] / r["usd_value"]) < 1e-9
    assert r["to_apr"] < r["to_effective_apr"] < 0.0676


def test_below_threshold_stays_silent_even_with_a_split():
    """Її справжні $500 у OKX Simple Earn: навіть розклад дає $19 — це мовчання."""
    p = Portfolio([Holding("OKX", "USDT", 500.0, 500.0, EARN, 0.01, "Simple Earn")])
    assert upgrades(p, _live_usdt_offers()) == []
    # А з нульовим порогом цифри все одно видно — саме їх і показує /status.
    r = upgrades(p, _live_usdt_offers(), min_gain_year=0.0)[0]
    assert abs(r["gain_year_usd"] - 13.80) < 0.05
    assert abs(r["split"]["gain_year_usd"] - 19.02) < 0.05


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("\nALL_PORTFOLIO_CHECKS_OK")
