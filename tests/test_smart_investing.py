"""Локальні перевірки 5 покращень на СИНТЕТИЧНИХ даних (без мережі, без бойового бота).

Запуск: ./.venv/bin/python -m pytest tests/test_smart_investing.py  (або просто як скрипт).
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import advisor, engine, service
from app.alerts import _min_low_since
from app.storage import Storage


def _tmp_storage() -> Storage:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    st = Storage(path)
    st.init()
    return st


def _fake_data(coin: str, closes, price, lows=None, ts=None):
    n = len(closes)
    ts = ts or [i * 86400 * 1000 for i in range(n)]
    return {
        "coin": coin,
        "price": price,
        "change_24h": -0.01,
        "closes": closes,
        "ts": ts,
        "lows": lows,
        "weekly_closes": [],
    }


def test_a_allocation_parse_and_display():
    st = _tmp_storage()
    # дефолт із config (60/40)
    tgt = service.target_allocation(st)
    assert tgt == {"BTC": 60.0, "ETH": 40.0}, tgt
    # збереження/нормалізація
    st.set_allocation({"BTC": 70, "ETH": 30})
    assert service.target_allocation(st) == {"BTC": 70.0, "ETH": 30.0}

    # фактичний розподіл рахується за qty*ціна (BTC дорожча — більша частка вартості)
    data = {
        "BTC": _fake_data("BTC", [100.0] * 5, 100.0),
        "ETH": _fake_data("ETH", [10.0] * 5, 10.0),
    }
    st._conn = st._conn  # noqa
    # покладемо filled-позиції напряму в БД
    with st._conn() as c:
        c.execute(
            "INSERT INTO orders(rec_id, order_index, coin, price, qty, notional, status, created_at, filled_at) "
            "VALUES(0,0,'BTC',100,1.0,100,'filled',1,1)"
        )
        c.execute(
            "INSERT INTO orders(rec_id, order_index, coin, price, qty, notional, status, created_at, filled_at) "
            "VALUES(0,1,'ETH',10,1.0,10,'filled',1,1)"
        )
    actual = service.actual_allocation(st, data)
    # BTC value=100, ETH value=10 -> ~91% / 9%
    assert abs(actual["BTC"] - 90.9) < 0.5, actual
    assert abs(actual["ETH"] - 9.1) < 0.5, actual
    print("A allocation OK:", service.target_allocation(st), actual)


def test_b_coin_snapshot_has_trends():
    # 120 днів даних: рівне зростання
    closes = [100.0 + i for i in range(120)]
    data = {"BTC": _fake_data("BTC", closes, closes[-1])}
    snap = engine.coin_snapshot(data, fng=50)
    s = snap[0]
    assert "trend_30d_pct" in s and "trend_90d_pct" in s
    assert s["trend_30d_pct"] is not None and s["trend_90d_pct"] is not None
    # за 30 днів ціна зросла -> додатній тренд
    assert s["trend_30d_pct"] > 0 and s["trend_90d_pct"] > 0
    # коротка історія -> None, не падаємо
    short = {"BTC": _fake_data("BTC", [100.0, 101.0], 101.0)}
    s2 = engine.coin_snapshot(short, fng=50)[0]
    assert s2["trend_30d_pct"] is None and s2["trend_90d_pct"] is None
    print("B trends OK:", s["trend_30d_pct"], s["trend_90d_pct"])


def test_c_cost_basis_line_builds():
    snapshot = [{"coin": "BTC", "price": 90.0}, {"coin": "ETH", "price": 20.0}]
    # середня входу BTC=100 (зараз нижче -> докупка знизить), ETH=10 (зараз вище)
    position = {"avg_entry": {"BTC": 100.0, "ETH": 10.0}}
    line = advisor._cost_basis_line(snapshot, position)
    assert line and "BTC" in line and "ETH" in line
    assert "знизить" in line  # BTC нижче середньої
    # без позиції -> None
    assert advisor._cost_basis_line(snapshot, None) is None
    assert advisor._cost_basis_line(snapshot, {"avg_entry": {}}) is None
    print("C cost-basis OK:", line)


def test_c2_allocation_line_builds():
    alloc = {"target": {"BTC": 60.0, "ETH": 40.0}, "actual": {"BTC": 80.0, "ETH": 20.0}}
    line = advisor._allocation_line(alloc)
    assert line and "60" in line and "80" in line and "замало" in line
    assert advisor._allocation_line(None) is None
    print("C2 allocation line OK:", line)


def test_d_lows_used_in_min_low_since():
    ts = [0, 86400 * 1000, 2 * 86400 * 1000]
    closes = [100.0, 100.0, 100.0]
    lows = [99.0, 95.0, 98.0]  # денні мінімуми нижчі за closes
    d = _fake_data("BTC", closes, 100.0, lows=lows, ts=ts)
    # з lows: найнижче 95
    assert _min_low_since(d, 0) == 95.0
    # без lows: повертається до closes (100)
    d2 = _fake_data("BTC", closes, 100.0, lows=None, ts=ts)
    assert _min_low_since(d2, 0) == 100.0
    # довжина lows не збігається з ts -> ігноруємо lows, беремо closes
    d3 = _fake_data("BTC", closes, 100.0, lows=[95.0], ts=ts)
    assert _min_low_since(d3, 0) == 100.0
    print("D lows OK")


def test_e_take_profit_threshold():
    st = _tmp_storage()
    data = {
        "BTC": _fake_data("BTC", [100.0] * 5, 150.0),  # купили по 100, зараз 150 -> +50%
        "ETH": _fake_data("ETH", [10.0] * 5, 11.0),
    }
    with st._conn() as c:
        c.execute(
            "INSERT INTO orders(rec_id, order_index, coin, price, qty, notional, status, created_at, filled_at) "
            "VALUES(0,0,'BTC',100,1.0,100,'filled',1,1)"
        )
        c.execute(
            "INSERT INTO orders(rec_id, order_index, coin, price, qty, notional, status, created_at, filled_at) "
            "VALUES(0,1,'ETH',10,1.0,10,'filled',1,1)"
        )
    pnl = service.portfolio_pnl(st, data)
    # invested=110, value=161 -> +46.4%
    assert pnl["pnl_pct"] is not None and pnl["pnl_pct"] > 40, pnl
    assert pnl["by_coin"]["BTC"]["pnl_pct"] == 50.0
    # порожній портфель -> pnl_pct None (не падаємо)
    st2 = _tmp_storage()
    assert service.portfolio_pnl(st2, data)["pnl_pct"] is None
    print("E take-profit OK:", pnl["pnl_pct"])


if __name__ == "__main__":
    test_a_allocation_parse_and_display()
    test_b_coin_snapshot_has_trends()
    test_c_cost_basis_line_builds()
    test_c2_allocation_line_builds()
    test_d_lows_used_in_min_low_since()
    test_e_take_profit_threshold()
    print("\nALL_SMART_CHECKS_OK")
