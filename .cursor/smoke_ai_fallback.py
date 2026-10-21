"""Що бот пише, коли Gemini мовчить, — і чи чесно він при цьому себе позначає.

Питання не теоретичне: квоти безплатного ключа хитаються, і половина відповідей
бота цілком може приходити від формули. Вона мусить бачити, коли рішення НЕ від
AI, інакше сухе правило не відрізнити від справжньої поради.

Перевіряємо всі чотири місця, де тепер є порадник:
  1) крипто-порада (/invest і сповіщення про гарний момент);
  2) вердикти по акціях бірж;
  3) розбір незнайомої монети;
  4) розбір вкладу, записаного вільним текстом.

Мережі не потрібно: усі запити до Gemini підміняємо на відмову (503, як у
справжньому журналі сервера). Ключ ставимо несправжній — саме так виглядає
випадок «ключ є, але порадник не відповідає», і тільки в ньому потрібна позначка.
"""
import asyncio
import datetime

import httpx

from app import advisor, service
from app.config import config
from app.earn import Offer, Promo, Tier
from app.formatting import (
    NO_ADVISOR_NOTE,
    coin_trust_lines,
    format_recommendation,
    journal_confirm_text,
    promo_lines,
)

TODAY = datetime.date(2026, 9, 5)


class _Dead:
    """Gemini відповідає 503 — те саме, що видно в journalctl на сервері."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        return httpx.Response(503, json={"error": {"message": "model is overloaded"}},
                              request=httpx.Request("POST", url))


def _market() -> dict:
    return {
        "BTC": {"coin": "BTC", "price": 79_700.0, "change_24h": -0.09,
                "sma200": 95_000.0, "rsi14": 24.0, "closes": [95_000.0] * 200,
                "annual_pos_pct": 8.0, "daily_volatility_pct": 3.1},
        "ETH": {"coin": "ETH", "price": 2_455.0, "change_24h": -0.07,
                "sma200": 3_100.0, "rsi14": 27.0, "closes": [3_100.0] * 200,
                "annual_pos_pct": 11.0, "daily_volatility_pct": 3.8},
    }


def show(title: str, body) -> None:
    text = body if isinstance(body, str) else "\n\n".join(body)
    mark = "✅ позначено" if NO_ADVISOR_NOTE in text else "❌ ПОЗНАЧКИ НЕМАЄ"
    print("\n" + "=" * 78)
    print(f"{title}   [{mark}]")
    print("=" * 78)
    print(text)


async def main():
    httpx.AsyncClient = lambda *a, **kw: _Dead()      # noqa: N806 — підміна на весь смоук
    config.gemini_api_key = "fake-key-for-smoke"
    advisor.reset_health()

    # 1. Крипто-порада: рішення ухвалює формула, бо порадник відмовив.
    rec = await service.analyze_market(_market(), 18, 100.0)
    print(f"\nджерело рішення: {rec.source!r} (мусить бути не 'ai')")
    show("1. КРИПТО-ПОРАДА", format_recommendation(rec))

    # 2. Акції бірж: вердикт складає правило fallback_explain.
    promos = [
        Promo("Bybit", "Earn up to 10% APR on USDT Savings", "https://x/1", 0),
        Promo("OKX", "Grid Trading Tournament: share $50,000", "https://x/2", 0),
    ]
    explains = await service.promo_advice(promos, {"USDT": 890.0}, 390.0,
                                          {"USDT": 0.0676})
    print("\nby_rule по кожній акції:",
          {p.title[:22]: explains[p.title].get("by_rule") for p in promos})
    show("2. ВЕРДИКТИ ПО АКЦІЯХ", promo_lines(promos, explains, TODAY))

    # 3. Незнайома монета: лишаємось на сухих цифрах CoinGecko.
    class Facts:
        async def profiles(self, coins):
            return {"ATOM": {"coin": "ATOM", "rank": 45, "age_years": 7.0,
                             "volume_usd": 120_000_000.0}}

    trust = await service.coin_reliability(Facts(), ["ATOM"])
    print("\nby_rule по монеті:", {c: t["by_rule"] for c, t in trust.items()})
    show("3. НАДІЙНІСТЬ МОНЕТИ", coin_trust_lines(trust, 390.0, {
        "ATOM": (0.169, Offer("OKX", "ATOM", 0.169, "flex", "high", None, 0.0,
                              [Tier(0, None, 0.169)], "Simple Earn"))}))

    # 4. Журнал вільним текстом: розбирає регулярка, і про це сказано вголос.
    deposits, by_ai = await service.parse_deposits_text(
        "поклала 200 usdt bybit savings 6.77% на 30 днів", TODAY)
    text = journal_confirm_text(deposits, {"USDT": 1.0})
    if not by_ai:
        text += "\n" + NO_ADVISOR_NOTE
    print(f"\nрозбирав порадник: {by_ai} (мусить бути False)")
    show("4. РОЗБІР ВКЛАДУ З ТЕКСТУ", text)

    print("\n" + "=" * 78)
    print(f"стан порадника після всього: {advisor.health()['ok']} "
          f"(False = бот знає, що мовчить, і сам про це попередить)")


asyncio.run(main())
