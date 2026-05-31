import os
from dataclasses import dataclass, field

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass


def _int(name: str, default: int) -> int:
    v = os.getenv(name)
    return int(v) if v not in (None, "") else default


def _float(name: str, default: float) -> float:
    v = os.getenv(name)
    return float(v) if v not in (None, "") else default


@dataclass
class Config:
    bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    owner_id: int = _int("OWNER_ID", 0)
    base_amount: float = _float("BASE_AMOUNT", 100.0)
    quote: str = os.getenv("QUOTE", "USDT")
    symbols: tuple = ("BTC", "ETH")
    rungs: int = _int("RUNGS", 3)
    min_notional: float = _float("MIN_NOTIONAL", 5.0)
    stale_days: int = _int("STALE_DAYS", 14)
    db_path: str = os.getenv("DB_PATH", "invest_bot.db")
    # Локальний CA-bundle (потрібен лише за корпоративним TLS-проксі). На сервері — порожній.
    ca_bundle: str = os.getenv("CA_BUNDLE", "")
    # alert thresholds
    capitulation_drop: float = _float("CAPITULATION_DROP", 0.08)
    good_moment_score: float = _float("GOOD_MOMENT_SCORE", 60.0)
    greed_fng: int = _int("GREED_FNG", 80)


config = Config()
