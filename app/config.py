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
    # Бажана пропорція портфеля (цільовий розподіл). Перші числа — для symbols у тому ж порядку.
    base_allocation: tuple = (60.0, 40.0)  # BTC 60% / ETH 40%
    rungs: int = _int("RUNGS", 3)
    min_notional: float = _float("MIN_NOTIONAL", 5.0)
    # Мінімальний "осмислений" ордер: менше ставити не варто (комісії/морока).
    # Бот зменшує кількість сходинок або пропускає монету, щоб кожен ордер був >= цього.
    min_order: float = _float("MIN_ORDER", 25.0)
    stale_days: int = _int("STALE_DAYS", 14)
    db_path: str = os.getenv("DB_PATH", "invest_bot.db")
    # Локальний CA-bundle (потрібен лише за корпоративним TLS-проксі). На сервері — порожній.
    ca_bundle: str = os.getenv("CA_BUNDLE", "")
    # alert thresholds
    capitulation_drop: float = _float("CAPITULATION_DROP", 0.08)
    good_moment_score: float = _float("GOOD_MOMENT_SCORE", 60.0)
    greed_fng: int = _int("GREED_FNG", 80)
    # Поріг прибутку портфеля/монети (у %), за яким мʼяко підказуємо зафіксувати частину прибутку.
    take_profit_pct: float = _float("TAKE_PROFIT_PCT", 40.0)
    # AI-порадник (Gemini). Порожній ключ = AI вимкнено, працює лише формула.
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
    # Основний аліас періодично перевантажений (503) — тоді пробуємо запасну модель,
    # щоб порадник не мовчав годинами. Має відрізнятись від gemini_model, інакше
    # запасної фактично немає, і має мати ЖИВУ безплатну квоту: у gemini-2.5-flash
    # на цьому ключі денна межа 20 запитів і вона вичерпана, тож кожна спроба
    # туди — гарантований 429 і другий марний запит на кожну відмову.
    gemini_fallback_model: str = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-flash-lite-latest")
    # Ключі бірж — ТІЛЬКИ читання (без торгівлі й виводу) і привʼязані до IP сервера.
    # Порожньо = бот не бачить реальних залишків, решта функцій працює як раніше.
    okx_api_key: str = os.getenv("OKX_API_KEY", "")
    okx_api_secret: str = os.getenv("OKX_API_SECRET", "")
    okx_passphrase: str = os.getenv("OKX_PASSPHRASE", "")
    bybit_api_key: str = os.getenv("BYBIT_API_KEY", "")
    bybit_api_secret: str = os.getenv("BYBIT_API_SECRET", "")

    @property
    def has_okx(self) -> bool:
        """OKX вимагає ще й passphrase — без нього ключ марний."""
        return bool(self.okx_api_key and self.okx_api_secret and self.okx_passphrase)

    @property
    def has_bybit(self) -> bool:
        return bool(self.bybit_api_key and self.bybit_api_secret)

    @property
    def has_exchange_keys(self) -> bool:
        return self.has_okx or self.has_bybit


config = Config()
