"""Чисті функції технічних індикаторів (без зовнішніх залежностей)."""
from __future__ import annotations


def sma(values: list[float], period: int) -> float | None:
    if not values or len(values) < period:
        return None
    return sum(values[-period:]) / period


def rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        ch = closes[i] - closes[i - 1]
        if ch >= 0:
            gains += ch
        else:
            losses -= ch
    avg_gain = gains / period
    avg_loss = losses / period
    for i in range(period + 1, len(closes)):
        ch = closes[i] - closes[i - 1]
        g = ch if ch > 0 else 0.0
        l = -ch if ch < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def volatility(closes: list[float], period: int = 14) -> float | None:
    """Середнє абсолютне денне коливання за останні `period` днів (частка)."""
    if len(closes) < period + 1:
        return None
    rets = [
        abs(closes[i] / closes[i - 1] - 1.0)
        for i in range(len(closes) - period, len(closes))
        if closes[i - 1]
    ]
    return sum(rets) / len(rets) if rets else None
