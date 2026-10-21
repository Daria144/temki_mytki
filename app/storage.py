"""Зберігання стану в SQLite: налаштування, журнал рекомендацій/ордерів, стан алертів."""
import datetime
import json
import sqlite3
import time
from dataclasses import asdict
from typing import List, Optional

from app.engine import Recommendation


# Скільки earn-сповіщень на добу. Лише коли ставки/вклади реально змінились —
# не щоденна стрічка «для галочки».
EARN_MAX_PER_DAY = 1
# Історія ставок: два тижні вистачає, щоб мати з чим порівняти «тиждень тому».
EARN_RATE_HISTORY_DAYS = 14
EARN_MUTE_DAYS = 90
EARN_MUTE_IDS_KEEP = 200
# Скільки днів памʼятаємо, що біржа мовчала і знімок заробітку не вийшов.
# Місяця з запасом досить: далі цей пропуск уже поза всіма проміжками показу.
EARN_INCOME_SKIP_DAYS = 40

# Стани запису в журналі вкладів. 'pending' живе лише до кнопки підтвердження:
# помилка розбору не має тихо осісти в базі й потім видавати себе за правду.
STATUS_PENDING, STATUS_ACTIVE, STATUS_CLOSED = "pending", "active", "closed"
# Гнучкий вклад (забрати будь-коли) проти фіксованого (замкнено до дати).
KIND_FLEX, KIND_FIXED = "flex", "fixed"


def flat_orders(payload: dict) -> List[dict]:
    """Розгортає рекомендацію у плоский список ордерів (індекс = порядок у списку)."""
    res = []
    for cp in payload.get("coins", []):
        for o in cp.get("orders", []):
            res.append({
                "coin": cp["coin"],
                "price": o["price"],
                "qty": o["qty"],
                "notional": o["notional"],
            })
    return res


class Storage:
    def __init__(self, path: str):
        self.path = path

    def _conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def init(self):
        with self._conn() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings(
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                CREATE TABLE IF NOT EXISTS recommendations(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at INTEGER,
                    payload TEXT
                );
                CREATE TABLE IF NOT EXISTS orders(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rec_id INTEGER,
                    order_index INTEGER,
                    coin TEXT,
                    price REAL,
                    qty REAL,
                    notional REAL,
                    status TEXT,
                    created_at INTEGER,
                    filled_at INTEGER,
                    fill_price REAL,
                    confirm_declined_at INTEGER,
                    runaway INTEGER DEFAULT 0,
                    eval30 INTEGER,
                    eval30_at INTEGER,
                    entry_vs_avg30 REAL,
                    eval90 INTEGER,
                    eval90_at INTEGER,
                    entry_vs_avg90 REAL,
                    UNIQUE(rec_id, order_index)
                );
                CREATE TABLE IF NOT EXISTS alert_state(
                    type TEXT PRIMARY KEY,
                    last_sent INTEGER
                );
                CREATE TABLE IF NOT EXISTS ibkr_contributions(
                    month TEXT PRIMARY KEY,       -- YYYY-MM, один внесок на місяць
                    fund TEXT,                    -- WEBN або CEMA (за календарем)
                    amount REAL,                  -- планова сума внеску, EUR
                    status TEXT,                  -- reminded / done / skipped
                    units REAL,                   -- скільки паїв реально купила (якщо ввела)
                    price REAL,                   -- ціна паю на момент нагадування, довідково
                    created_at INTEGER,
                    done_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS earn_deposits(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    coin TEXT,                    -- USDT, BTC...
                    amount REAL,                  -- скільки монети поклала
                    exchange TEXT,                -- OKX / Bybit
                    product TEXT,                 -- Savings, Simple Earn, On-Chain Earn...
                    apr REAL,                     -- ВХІДНА ставка часткою на рік (0.0677)
                    kind TEXT,                    -- flex (вивід вільний) / fixed (замкнено)
                    term_days INTEGER,            -- строк, якщо називала його днями
                    unlock_date TEXT,             -- YYYY-MM-DD, коли розморозиться
                    status TEXT,                  -- pending / active / closed
                    note TEXT,                    -- сирий рядок, як вона його написала
                    created_at INTEGER,
                    confirmed_at INTEGER,
                    closed_at INTEGER,
                    closed_how TEXT,              -- took (забрала) / renewed (продовжила)
                    reminded_on TEXT,             -- дата останнього нагадування про розморозку
                    rate_alert_apr REAL           -- ставка, про падіння до якої вже казали
                );
                CREATE TABLE IF NOT EXISTS earn_income(
                    day TEXT,                     -- YYYY-MM-DD, один знімок на добу
                    exchange TEXT,                -- OKX / Bybit
                    coin TEXT,                    -- USDT, ETH...
                    product TEXT,                 -- Simple Earn, Savings...
                    amount REAL,                  -- скільки монети лежить у позиції
                    apr REAL,                     -- ставка, яку біржа ОБІЦЯЄ (не факт, що платить)
                    earned REAL,                  -- накопичений заробіток у монеті
                    cumulative INTEGER,           -- 1 = earned наростаюче за весь час
                    measured INTEGER,             -- 1 = цифра від біржі, 0 = порахував бот
                    price REAL,                   -- ціна монети в доларах на момент знімка
                    created_at INTEGER,
                    PRIMARY KEY(day, exchange, coin, product)
                );
                """
            )
        self._migrate()

    def _migrate(self):
        """Додає нові колонки до вже створеної бази (стара схема orders)."""
        extra = {
            "fill_price": "REAL",
            "confirm_declined_at": "INTEGER",
            "runaway": "INTEGER DEFAULT 0",
            "eval30": "INTEGER",
            "eval30_at": "INTEGER",
            "entry_vs_avg30": "REAL",
            "eval90": "INTEGER",
            "eval90_at": "INTEGER",
            "entry_vs_avg90": "REAL",
        }
        with self._conn() as c:
            cols = {r["name"] for r in c.execute("PRAGMA table_info(orders)").fetchall()}
            for name, decl in extra.items():
                if name not in cols:
                    c.execute(f"ALTER TABLE orders ADD COLUMN {name} {decl}")

    def reset(self):
        """Очищає всі дані (тільки для тестування)."""
        with self._conn() as c:
            c.executescript(
                "DELETE FROM orders; DELETE FROM recommendations; "
                "DELETE FROM alert_state; DELETE FROM settings; "
                "DELETE FROM ibkr_contributions; DELETE FROM earn_deposits; "
                "DELETE FROM earn_income;"
            )

    # --- settings ---
    def get_setting(self, key: str, default=None):
        with self._conn() as c:
            row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return row["value"] if row else default

    def set_setting(self, key: str, value):
        with self._conn() as c:
            c.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )

    def get_base(self, default: float) -> float:
        v = self.get_setting("base_amount")
        return float(v) if v else default

    def set_base(self, value: float):
        self.set_setting("base_amount", value)

    def get_allocation(self, default: dict) -> dict:
        """Цільова пропорція портфеля по монетах, напр. {'BTC': 60.0, 'ETH': 40.0}."""
        raw = self.get_setting("allocation")
        if not raw:
            return dict(default)
        try:
            data = json.loads(raw)
            return {str(k).upper(): float(v) for k, v in data.items()}
        except (ValueError, TypeError, AttributeError):
            return dict(default)

    def set_allocation(self, mapping: dict):
        clean = {str(k).upper(): float(v) for k, v in mapping.items()}
        self.set_setting("allocation", json.dumps(clean))

    def alerts_enabled(self) -> bool:
        return self.get_setting("alerts", "on") == "on"

    def set_alerts(self, on: bool):
        self.set_setting("alerts", "on" if on else "off")

    # --- місячний бюджет ---
    def get_budget(self) -> dict:
        """Поточний місячний бюджет: місяць (YYYY-MM), сума і перенесений залишок."""
        return {
            "month": self.get_setting("budget_month", "") or "",
            "amount": float(self.get_setting("budget_amount", 0) or 0),
            "carry": float(self.get_setting("budget_carry", 0) or 0),
        }

    def set_budget(self, month: str, amount: float):
        self.set_setting("budget_month", month)
        self.set_setting("budget_amount", amount)

    def set_budget_carry(self, amount: float):
        self.set_setting("budget_carry", amount)

    def clear_active_budget(self):
        """Прибирає активний місяць (залишок уже перенесено в carry)."""
        self.set_setting("budget_month", "")
        self.set_setting("budget_amount", 0)

    # --- earn (депозити під відсоток) ---
    def get_earn_amount(self) -> float:
        """Остання сума з /earn — щоб авто-сповіщення теж рахували в грошах."""
        return float(self.get_setting("earn_amount", 0) or 0)

    def set_earn_amount(self, amount: float):
        self.set_setting("earn_amount", round(float(amount), 2))

    def get_earn_rates(self) -> dict:
        """Ставки по монетах на момент минулої перевірки — з чим порівнюємо стрибок."""
        raw = self.get_setting("earn_best_rates", "")
        if not raw:
            return {}
        try:
            return {str(k): float(v) for k, v in json.loads(raw).items()}
        except (ValueError, TypeError, AttributeError):
            return {}

    def set_earn_rates(self, rates: dict, today: Optional[datetime.date] = None) -> None:
        """Зберігає ставки як «минулий раз» і додає денний зріз в історію."""
        clean = {str(k): round(float(v), 6) for k, v in rates.items()}
        self.set_setting("earn_best_rates", json.dumps(clean))
        self._push_rate_history(clean, today or datetime.date.today())

    def _push_rate_history(self, rates: dict, day: datetime.date) -> None:
        """Один зріз на добу. Потрібно, щоб бачити ПОВІЛЬНЕ зростання ставки.

        Порівняння лише з минулим запуском (12 год) не ловить сходи
        4% → 4.5% → 5% → 6%: кожен крок окремо надто малий, а за тиждень це
        півтора разу різниці.
        """
        raw = self.get_setting("earn_rate_history", "")
        try:
            hist = json.loads(raw) if raw else []
        except (ValueError, TypeError):
            hist = []
        iso = day.isoformat()
        hist = [h for h in hist if isinstance(h, dict) and h.get("d") != iso]
        hist.append({"d": iso, "rates": rates})
        hist.sort(key=lambda h: h.get("d", ""))
        self.set_setting("earn_rate_history", json.dumps(hist[-EARN_RATE_HISTORY_DAYS:]))

    def earn_rates_days_ago(self, days: int = 5,
                            today: Optional[datetime.date] = None) -> dict:
        """Найсвіжіший зріз ставок, якому вже не менше `days` діб. {} якщо історії мало."""
        raw = self.get_setting("earn_rate_history", "")
        try:
            hist = json.loads(raw) if raw else []
        except (ValueError, TypeError):
            return {}
        cutoff = (today or datetime.date.today()) - datetime.timedelta(days=days)
        old = [h for h in hist if isinstance(h, dict) and h.get("d", "") <= cutoff.isoformat()]
        if not old:
            return {}
        return {str(k): float(v) for k, v in (old[-1].get("rates") or {}).items()}

    # --- скільки earn-сповіщень уже пішло сьогодні ---

    def earn_notify_count(self, today: Optional[datetime.date] = None) -> int:
        day = (today or datetime.date.today()).isoformat()
        raw = self.get_setting("earn_notify_count", "")
        stored_day, _, count = str(raw).partition(":")
        if stored_day != day:
            return 0
        try:
            return int(count or 0)
        except ValueError:
            return 0

    def earn_quota_left(self, today: Optional[datetime.date] = None) -> int:
        """Скільки earn-сповіщень ще можна надіслати сьогодні.

        Нагадування про дедлайн акції сюди НЕ входять: це єдина новина, яку не
        можна відкласти на завтра, бо завтра акції вже не буде.
        """
        return max(EARN_MAX_PER_DAY - self.earn_notify_count(today), 0)

    def mark_earn_notified(self, today: Optional[datetime.date] = None) -> None:
        day = (today or datetime.date.today()).isoformat()
        self.set_setting("earn_notify_count", f"{day}:{self.earn_notify_count(today) + 1}")

    # --- «не цікаво»: короткі id, бо в callback_data влазить лише 64 байти ---

    def earn_mute_register(self, target: str) -> int:
        """Реєструє ціль для кнопки й повертає короткий числовий id.

        Ключ акції — це повна назва на 80+ знаків, у callback_data вона не влазить.
        """
        raw = self.get_setting("earn_mute_ids", "")
        try:
            reg = json.loads(raw) if raw else {}
        except (ValueError, TypeError):
            reg = {}
        for key, value in reg.items():
            if value == target:
                return int(key)
        next_id = max((int(k) for k in reg), default=0) + 1
        reg[str(next_id)] = target
        # Старі id вже нікому не потрібні: кнопки під давніми повідомленнями
        # користувачка не натискає, а список має лишатись коротким.
        if len(reg) > EARN_MUTE_IDS_KEEP:
            for key in sorted(reg, key=int)[:-EARN_MUTE_IDS_KEEP]:
                reg.pop(key)
        self.set_setting("earn_mute_ids", json.dumps(reg))
        return next_id

    def earn_mute_target(self, mute_id: int) -> Optional[str]:
        raw = self.get_setting("earn_mute_ids", "")
        try:
            return (json.loads(raw) if raw else {}).get(str(mute_id))
        except (ValueError, TypeError):
            return None

    def earn_mute(self, target: str, days: int = EARN_MUTE_DAYS) -> None:
        """Глушить пораду на кілька місяців, а не назавжди: ринок міняється, і
        те, що сьогодні нецікаво, за пів року може стати найкращим варіантом."""
        muted = self._earn_mutes()
        muted[target] = int(time.time()) + days * 86400
        self.set_setting("earn_muted", json.dumps(muted))

    def _earn_mutes(self) -> dict:
        raw = self.get_setting("earn_muted", "")
        try:
            data = json.loads(raw) if raw else {}
        except (ValueError, TypeError):
            return {}
        now = int(time.time())
        return {k: int(v) for k, v in data.items() if int(v) > now}

    def earn_muted(self) -> set:
        """Цілі, про які зараз просили не нагадувати."""
        return set(self._earn_mutes())

    # --- варті акції з дедлайном: щоб нагадати за 2 дні до кінця ---

    def earn_watch_promos(self) -> dict:
        raw = self.get_setting("earn_promo_watch", "")
        try:
            data = json.loads(raw) if raw else {}
            return data if isinstance(data, dict) else {}
        except (ValueError, TypeError):
            return {}

    def earn_watch_promo(self, key: str, info: dict) -> None:
        """Запамʼятовує варту акцію з датою кінця. Наявний запис не перетирає —
        інакше загубився б факт, що нагадування вже надсилали."""
        watch = self.earn_watch_promos()
        if key not in watch:
            watch[key] = dict(info, reminded=False)
            self.set_setting("earn_promo_watch", json.dumps(watch))

    def earn_promo_reminded(self, key: str) -> None:
        watch = self.earn_watch_promos()
        if key in watch:
            watch[key]["reminded"] = True
            self.set_setting("earn_promo_watch", json.dumps(watch))

    def earn_drop_watched(self, keys) -> None:
        watch = self.earn_watch_promos()
        for key in keys:
            watch.pop(key, None)
        self.set_setting("earn_promo_watch", json.dumps(watch))

    def seen_promos(self) -> set:
        raw = self.get_setting("earn_seen_promos", "")
        if not raw:
            return set()
        try:
            return set(json.loads(raw))
        except (ValueError, TypeError):
            return set()

    def mark_promos_seen(self, keys) -> None:
        """Запамʼятовує показані акції, щоб не слати їх повторно."""
        merged = list(self.seen_promos() | set(keys))
        self.set_setting("earn_seen_promos", json.dumps(merged[-300:]))

    def seen_products(self) -> set:
        """Пропозиції, які вже існували минулого разу — з чим порівнюємо «нове»."""
        raw = self.get_setting("earn_seen_products", "")
        if not raw:
            return set()
        try:
            return set(json.loads(raw))
        except (ValueError, TypeError):
            return set()

    def mark_products_seen(self, keys) -> None:
        merged = list(self.seen_products() | set(keys))
        self.set_setting("earn_seen_products", json.dumps(merged[-800:]))

    # --- «краще місце для грошей»: яку саме пропозицію по монеті вже показали ---
    # Без цього блок повторювався б щодня, поки гроші просто лежать на місці —
    # сума в доларах дрейфує від нарахованих відсотків, а новини в цьому нема.

    def earn_seen_upgrades(self) -> dict:
        raw = self.get_setting("earn_seen_upgrades", "")
        try:
            data = json.loads(raw) if raw else {}
            return data if isinstance(data, dict) else {}
        except (ValueError, TypeError):
            return {}

    def mark_upgrades_seen(self, signatures: dict) -> None:
        """Запамʼятовує показану пропозицію по кожній монеті (не додає, а замінює —
        стара пропозиція по тій самій монеті більше не актуальна)."""
        if not signatures:
            return
        merged = {**self.earn_seen_upgrades(), **signatures}
        self.set_setting("earn_seen_upgrades", json.dumps(merged))

    # --- журнал earn-вкладів: строки й вхідні ставки, яких API бірж не віддає ---

    def earn_deposit_add(self, dep: dict, status: str = STATUS_PENDING) -> int:
        """Кладе вклад у журнал. За замовчуванням 'pending' — до підтвердження кнопкою.

        Помилка розбору не має тихо осісти в базі, тому активним запис стає лише
        після того, як вона підтвердила, що бот зрозумів усе правильно.
        """
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO earn_deposits(coin, amount, exchange, product, apr, kind, "
                "term_days, unlock_date, status, note, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(dep.get("coin") or "").upper(),
                    float(dep.get("amount") or 0.0),
                    str(dep.get("exchange") or ""),
                    str(dep.get("product") or ""),
                    float(dep["apr"]) if dep.get("apr") is not None else None,
                    str(dep.get("kind") or KIND_FLEX),
                    int(dep["term_days"]) if dep.get("term_days") else None,
                    str(dep["unlock_date"])[:10] if dep.get("unlock_date") else None,
                    status,
                    str(dep.get("note") or ""),
                    int(time.time()),
                ),
            )
            return cur.lastrowid

    def earn_deposits(self, status: Optional[str] = STATUS_ACTIVE) -> List[dict]:
        """Вклади журналу; status=None — усі, включно з закритими."""
        sql = "SELECT * FROM earn_deposits"
        args: tuple = ()
        if status:
            sql += " WHERE status=?"
            args = (status,)
        with self._conn() as c:
            return [dict(r) for r in c.execute(sql + " ORDER BY id", args).fetchall()]

    def earn_deposit(self, dep_id: int) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM earn_deposits WHERE id=?", (dep_id,)).fetchone()
            return dict(row) if row else None

    def earn_deposits_confirm(self, ids) -> int:
        """Підтверджені вклади стають активними — саме з цього моменту вони в журналі."""
        ids = [int(i) for i in ids]
        if not ids:
            return 0
        marks = ", ".join("?" for _ in ids)
        with self._conn() as c:
            cur = c.execute(
                f"UPDATE earn_deposits SET status=?, confirmed_at=? "
                f"WHERE id IN ({marks}) AND status=?",
                (STATUS_ACTIVE, int(time.time()), *ids, STATUS_PENDING),
            )
            return cur.rowcount

    def earn_deposits_drop(self, ids) -> int:
        """Скасований розбір прибираємо повністю: у журналі не місце здогадкам."""
        ids = [int(i) for i in ids]
        if not ids:
            return 0
        marks = ", ".join("?" for _ in ids)
        with self._conn() as c:
            cur = c.execute(f"DELETE FROM earn_deposits WHERE id IN ({marks})", ids)
            return cur.rowcount

    def earn_deposit_close(self, dep_id: int, how: str) -> Optional[dict]:
        """Вклад закрито: 'took' — забрала гроші, 'renewed' — переклала кудись інде."""
        with self._conn() as c:
            c.execute(
                "UPDATE earn_deposits SET status=?, closed_at=?, closed_how=? WHERE id=?",
                (STATUS_CLOSED, int(time.time()), how, dep_id),
            )
        return self.earn_deposit(dep_id)

    def earn_deposit_extend(self, dep_id: int, unlock_date: Optional[str],
                            term_days: Optional[int] = None) -> Optional[dict]:
        """Продовжила вклад — строк їде далі, запис лишається активним.

        Автоматично нічого не закриваємо: поки вона не сказала, що з грошима,
        журнал не має права вирішувати це за неї.
        """
        with self._conn() as c:
            c.execute(
                "UPDATE earn_deposits SET unlock_date=?, term_days=COALESCE(?, term_days), "
                "reminded_on=NULL WHERE id=?",
                (str(unlock_date)[:10] if unlock_date else None, term_days, dep_id),
            )
        return self.earn_deposit(dep_id)

    def earn_deposit_reminded(self, dep_id: int, day: Optional[datetime.date] = None) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE earn_deposits SET reminded_on=? WHERE id=?",
                ((day or datetime.date.today()).isoformat(), dep_id),
            )

    def earn_deposit_rate_alerted(self, dep_id: int, apr: float) -> None:
        """Запамʼятовує, до якої ставки вже падали: одне падіння — одне повідомлення."""
        with self._conn() as c:
            c.execute("UPDATE earn_deposits SET rate_alert_apr=? WHERE id=?",
                      (round(float(apr), 6), dep_id))

    # --- облік заробітку з earn-позицій: щоденні знімки ---

    def earn_income_add(self, rows: List[dict]) -> int:
        """Кладе денний знімок заробітку. По рядку на позицію, один на добу.

        Порожній список не пише НІЧОГО: якщо біржа не відповіла, у ряді має
        бути пропуск, а не нуль. Нуль потім не відрізнити від справжнього
        «нічого не заробила», і вся арифметика поїде.

        Повторний запуск того самого дня перезаписує знімок, а не дублює:
        свіжіша цифра з біржі краща за вранішню.
        """
        if not rows:
            return 0
        now = int(time.time())
        with self._conn() as c:
            for r in rows:
                c.execute(
                    "INSERT INTO earn_income(day, exchange, coin, product, amount, apr, "
                    "earned, cumulative, measured, price, created_at) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(day, exchange, coin, product) DO UPDATE SET "
                    "amount=excluded.amount, apr=excluded.apr, earned=excluded.earned, "
                    "cumulative=excluded.cumulative, measured=excluded.measured, "
                    "price=excluded.price, created_at=excluded.created_at",
                    (
                        str(r.get("day") or "")[:10],
                        str(r.get("exchange") or ""),
                        str(r.get("coin") or "").upper(),
                        str(r.get("product") or ""),
                        float(r.get("amount") or 0.0),
                        float(r["apr"]) if r.get("apr") is not None else None,
                        float(r.get("earned") or 0.0),
                        1 if r.get("cumulative") else 0,
                        1 if r.get("measured") else 0,
                        float(r["price"]) if r.get("price") else None,
                        now,
                    ),
                )
        return len(rows)

    def earn_income(self, since: Optional[str] = None) -> List[dict]:
        """Знімки заробітку за датою; since — з якої дати (включно)."""
        sql = "SELECT * FROM earn_income"
        args: tuple = ()
        if since:
            sql += " WHERE day >= ?"
            args = (str(since)[:10],)
        with self._conn() as c:
            rows = [dict(r) for r in
                    c.execute(sql + " ORDER BY day, exchange, coin, product", args)]
        # Прапорці в SQLite цілі, а весь модуль обліку працює з булевими.
        for r in rows:
            r["cumulative"] = bool(r.get("cumulative"))
            r["measured"] = bool(r.get("measured"))
        return rows

    def earn_income_latest(self) -> dict:
        """Останній знімок по кожній позиції — база для наступного."""
        out: dict = {}
        for r in self.earn_income():
            out[(r["exchange"], r["coin"], r["product"] or "")] = r
        return out

    def earn_income_skip(self, day, sources) -> None:
        """Помітка «біржа мовчала, знімка за цю добу немає».

        Потрібна, щоб пропуск у ряді було видно як пропуск: без неї зникла
        позиція виглядає так само, як забрані з неї гроші.
        """
        data = self.earn_income_skips()
        data[str(day)[:10]] = sorted({str(s) for s in sources or []})
        keep = sorted(data)[-EARN_INCOME_SKIP_DAYS:]
        self.set_setting("earn_income_skips", json.dumps({k: data[k] for k in keep}))

    def earn_income_skips(self) -> dict:
        raw = self.get_setting("earn_income_skips", "")
        try:
            data = json.loads(raw) if raw else {}
            return data if isinstance(data, dict) else {}
        except (ValueError, TypeError):
            return {}

    # --- тижневий огляд: що показували минулої неділі ---

    def earn_week_snapshot(self) -> dict:
        raw = self.get_setting("earn_week_snapshot", "")
        try:
            data = json.loads(raw) if raw else {}
            return data if isinstance(data, dict) else {}
        except (ValueError, TypeError):
            return {}

    def set_earn_week_snapshot(self, snap: dict) -> None:
        self.set_setting("earn_week_snapshot", json.dumps(snap))

    def _month_bounds(self, month: str):
        """Межі місяця YYYY-MM у unix-часі. None, якщо рядок не схожий на місяць."""
        try:
            year, mon = (int(x) for x in month.split("-"))
        except (ValueError, AttributeError):
            return None
        start = int(datetime.datetime(year, mon, 1).timestamp())
        ny, nm = (year + 1, 1) if mon == 12 else (year, mon + 1)
        return start, int(datetime.datetime(ny, nm, 1).timestamp())

    def _sum_notional(self, month: str, statuses: tuple) -> float:
        bounds = self._month_bounds(month)
        if bounds is None:
            return 0.0
        start, end = bounds
        placeholders = ", ".join("?" for _ in statuses)
        with self._conn() as c:
            row = c.execute(
                f"SELECT COALESCE(SUM(notional), 0) AS s FROM orders "
                f"WHERE status IN ({placeholders}) AND created_at >= ? AND created_at < ?",
                (*statuses, start, end),
            ).fetchone()
            return float(row["s"])

    def deployed_in_month(self, month: str) -> float:
        """Скільки грошей РЕАЛЬНО пішло в монети у вказаному місяці.

        Лише filled: виставлена лімітка нічого не витрачає, поки не спрацює, і
        може бути скасована. Раніше сюди рахувались і відкриті заявки — через це
        бот вважав, що вкладає більше, ніж насправді, і сам собі занижував порції.
        """
        return self._sum_notional(month, ("filled",))

    def reserved_in_month(self, month: str) -> float:
        """Скільки висить у виставлених заявках (ще не витрачено, але вже обіцяно).

        Окреме поняття від «вкладено» — потрібне лише щоб показати картину чесно.
        """
        return self._sum_notional(month, ("open", "pending_confirm"))

    def reserved_for_buys(self) -> float:
        """Скільки доларів чекає на просадку у виставлених заявках — за всі місяці.

        Ці гроші не можна радити класти в earn: заявка спрацює саме тоді, коли
        гроші будуть замкнені. reserved_in_month для цього не годиться — лімітка
        живе тижнями й легко переживає межу місяця.
        """
        with self._conn() as c:
            row = c.execute(
                "SELECT COALESCE(SUM(notional), 0) AS s FROM orders "
                "WHERE status IN ('open', 'pending_confirm')"
            ).fetchone()
            return float(row["s"])

    # --- recommendations / orders ---
    def save_recommendation(self, rec: Recommendation) -> int:
        payload = json.dumps(asdict(rec))
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO recommendations(created_at, payload) VALUES(?, ?)",
                (int(time.time()), payload),
            )
            return cur.lastrowid

    def get_recommendation(self, rec_id: int) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute(
                "SELECT payload FROM recommendations WHERE id=?", (rec_id,)
            ).fetchone()
            return json.loads(row["payload"]) if row else None

    def set_order_decision(self, rec_id: int, order_index: int, status: str) -> Optional[dict]:
        """Зберігає рішення по ОДНОМУ ордеру: 'open' (поставив) або 'skipped' (пропустив)."""
        payload = self.get_recommendation(rec_id)
        if not payload:
            return None
        flat = flat_orders(payload)
        if order_index < 0 or order_index >= len(flat):
            return None
        o = flat[order_index]
        now = int(time.time())
        with self._conn() as c:
            c.execute(
                "INSERT INTO orders(rec_id, order_index, coin, price, qty, notional, status, created_at, filled_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, NULL) "
                "ON CONFLICT(rec_id, order_index) DO UPDATE SET "
                "status=excluded.status, created_at=excluded.created_at, filled_at=NULL",
                (rec_id, order_index, o["coin"], o["price"], o["qty"], o["notional"], status, now),
            )
        return o

    def get_decisions(self, rec_id: int) -> dict:
        with self._conn() as c:
            rows = c.execute(
                "SELECT order_index, status FROM orders WHERE rec_id=?", (rec_id,)
            ).fetchall()
            return {r["order_index"]: r["status"] for r in rows}

    def open_orders(self) -> List[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM orders WHERE status='open' ORDER BY created_at").fetchall()
            return [dict(r) for r in rows]

    def get_order(self, order_id: int) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            return dict(row) if row else None

    def mark_pending_confirm(self, order_id: int, ts: int):
        """Бот авто-визначив імовірне спрацювання — чекаємо підтвердження користувача."""
        with self._conn() as c:
            c.execute(
                "UPDATE orders SET status='pending_confirm', filled_at=? WHERE id=?",
                (ts, order_id),
            )

    def pending_confirm_orders(self) -> List[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM orders WHERE status='pending_confirm' ORDER BY filled_at"
            ).fetchall()
            return [dict(r) for r in rows]

    def confirm_fill(self, order_id: int) -> Optional[dict]:
        """Користувач підтвердив, що докупка реально відбулась → 'filled'."""
        with self._conn() as c:
            row = c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if not row:
                return None
            o = dict(row)
            c.execute(
                "UPDATE orders SET status='filled', fill_price=COALESCE(fill_price, price) WHERE id=?",
                (order_id,),
            )
            return o

    def mark_user_filled(self, order_id: int, ts: int) -> Optional[dict]:
        """Користувач вручну позначив ордер виконаним → 'filled' за ціною лімітки + час."""
        with self._conn() as c:
            row = c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if not row:
                return None
            o = dict(row)
            c.execute(
                "UPDATE orders SET status='filled', fill_price=COALESCE(fill_price, price), "
                "filled_at=? WHERE id=?",
                (ts, order_id),
            )
            return o

    def decline_fill(self, order_id: int, ts: int):
        """Користувач сказав, що ордер НЕ спрацював → назад у 'open' (з дедупом)."""
        with self._conn() as c:
            c.execute(
                "UPDATE orders SET status='open', filled_at=NULL, confirm_declined_at=? WHERE id=?",
                (ts, order_id),
            )

    def mark_cancelled(self, order_id: int):
        with self._conn() as c:
            c.execute("UPDATE orders SET status='cancelled' WHERE id=?", (order_id,))

    def mark_missed(self, order_id: int):
        """Ордер не спрацював, бо ціна втекла вгору — урок 'впущена можливість'."""
        with self._conn() as c:
            c.execute("UPDATE orders SET status='missed' WHERE id=?", (order_id,))

    def mark_runaway(self, order_id: int):
        with self._conn() as c:
            c.execute("UPDATE orders SET runaway=1 WHERE id=?", (order_id,))

    def filled_orders(self) -> List[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM orders WHERE status='filled' ORDER BY filled_at"
            ).fetchall()
            return [dict(r) for r in rows]

    def orders_to_evaluate(self, horizon: int, before_ts: int) -> List[dict]:
        """Виконані ордери, у яких ще нема оцінки на горизонт і горизонт уже минув."""
        col = f"eval{horizon}"
        with self._conn() as c:
            rows = c.execute(
                f"SELECT * FROM orders WHERE status='filled' AND {col} IS NULL "
                "AND filled_at IS NOT NULL AND filled_at <= ?",
                (before_ts,),
            ).fetchall()
            return [dict(r) for r in rows]

    def set_eval(self, order_id: int, horizon: int, win: int, entry_vs_avg: float, ts: int):
        col_win, col_at, col_dev = f"eval{horizon}", f"eval{horizon}_at", f"entry_vs_avg{horizon}"
        with self._conn() as c:
            c.execute(
                f"UPDATE orders SET {col_win}=?, {col_at}=?, {col_dev}=? WHERE id=?",
                (win, ts, entry_vs_avg, order_id),
            )

    def learning_rows(self) -> List[dict]:
        """Усі ордери, важливі для навчання: виконані + впущені можливості."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM orders WHERE status IN ('filled', 'missed') OR runaway=1"
            ).fetchall()
            return [dict(r) for r in rows]

    def open_orders_by_coin(self) -> dict:
        """Зведення відкритих (ще не виконаних) ордерів по монетах.

        Для кожної монети: к-сть, зарезервована сума, найнижча/найвища ціна лімітки.
        Потрібно щоб AI не дублював ордери і ставив нові ЛИШЕ глибше за наявні.
        """
        with self._conn() as c:
            rows = c.execute(
                "SELECT coin, COUNT(*) AS n, SUM(notional) AS notional, "
                "MIN(price) AS min_price, MAX(price) AS max_price "
                "FROM orders WHERE status='open' GROUP BY coin"
            ).fetchall()
        out = {}
        for r in rows:
            out[r["coin"]] = {
                "count": r["n"],
                "notional": round(r["notional"] or 0.0, 2),
                "min_price": r["min_price"],
                "max_price": r["max_price"],
            }
        return out

    def position_summary(self) -> dict:
        """Коротка зведення позиції для AI: відкриті ордери (загалом і по монетах) + остання докупка."""
        with self._conn() as c:
            open_n = c.execute(
                "SELECT COUNT(*) AS n FROM orders WHERE status='open'"
            ).fetchone()["n"]
            row = c.execute(
                "SELECT MAX(created_at) AS last FROM orders "
                "WHERE status IN ('open','pending_confirm','filled')"
            ).fetchone()
        last_ts = row["last"] if row else None
        last_days = int((time.time() - last_ts) / 86400) if last_ts else None
        # Середня ціна входу по монетах (за filled-ордерами) — для cost-basis підказок AI.
        filled = self.filled_positions()
        avg_entry = {
            coin: p["avg_price"] for coin, p in filled.items() if p.get("avg_price")
        }
        return {
            "open_orders": open_n,
            "last_buy_days_ago": last_days,
            "open_by_coin": self.open_orders_by_coin(),
            "avg_entry": avg_entry,
        }

    def filled_positions(self) -> dict:
        """Зведення реально куплених (filled) по монетах: к-сть, вкладено, к-сть монет, середня ціна."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT coin, COUNT(*) AS n, SUM(notional) AS invested, SUM(qty) AS qty "
                "FROM orders WHERE status='filled' GROUP BY coin"
            ).fetchall()
        out = {}
        for r in rows:
            qty = r["qty"] or 0.0
            invested = r["invested"] or 0.0
            out[r["coin"]] = {
                "count": r["n"],
                "invested": round(invested, 2),
                "qty": qty,
                "avg_price": (invested / qty) if qty else None,
            }
        return out

    def has_buy_this_month(self) -> bool:
        today = datetime.date.today()
        start = int(datetime.datetime(today.year, today.month, 1).timestamp())
        with self._conn() as c:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM orders WHERE filled_at IS NOT NULL AND filled_at >= ?",
                (start,),
            ).fetchone()
            return row["n"] > 0

    # --- alert dedupe ---
    def alert_recent(self, alert_type: str, cooldown_sec: int) -> bool:
        with self._conn() as c:
            row = c.execute(
                "SELECT last_sent FROM alert_state WHERE type=?", (alert_type,)
            ).fetchone()
            if not row:
                return False
            return (time.time() - row["last_sent"]) < cooldown_sec

    def mark_alert(self, alert_type: str):
        with self._conn() as c:
            c.execute(
                "INSERT INTO alert_state(type, last_sent) VALUES(?, ?) "
                "ON CONFLICT(type) DO UPDATE SET last_sent=excluded.last_sent",
                (alert_type, int(time.time())),
            )

    # --- журнал щомісячних внесків ETF на IBKR (окремо від крипти, у євро) ---
    def ibkr_ensure_month(self, month: str, fund: str, amount: float,
                          price: Optional[float] = None) -> dict:
        """Створює запис на місяць зі статусом 'reminded'. Наявний не перезаписує.

        Один внесок на місяць — тому month і є первинним ключем.
        """
        with self._conn() as c:
            c.execute(
                "INSERT INTO ibkr_contributions(month, fund, amount, status, price, created_at) "
                "VALUES(?, ?, ?, 'reminded', ?, ?) ON CONFLICT(month) DO NOTHING",
                (month, fund, float(amount), price, int(time.time())),
            )
        return self.ibkr_get_month(month)

    def ibkr_get_month(self, month: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM ibkr_contributions WHERE month=?", (month,)
            ).fetchone()
            return dict(row) if row else None

    def ibkr_mark_done(self, month: str, units: Optional[float] = None) -> Optional[dict]:
        """Позначає місяць зробленим. Кількість паїв — необовʼязкова."""
        with self._conn() as c:
            c.execute(
                "UPDATE ibkr_contributions SET status='done', done_at=?, "
                "units=COALESCE(?, units) WHERE month=?",
                (int(time.time()), units, month),
            )
        return self.ibkr_get_month(month)

    def ibkr_set_units(self, month: str, units: float) -> Optional[dict]:
        """Вписує кількість паїв окремо — вона може прийти вже після «Зроблено»."""
        with self._conn() as c:
            c.execute(
                "UPDATE ibkr_contributions SET units=? WHERE month=?",
                (float(units), month),
            )
        return self.ibkr_get_month(month)

    def ibkr_mark_skipped(self, month: str) -> Optional[dict]:
        with self._conn() as c:
            c.execute(
                "UPDATE ibkr_contributions SET status='skipped', done_at=? WHERE month=?",
                (int(time.time()), month),
            )
        return self.ibkr_get_month(month)

    def ibkr_history(self, limit: int = 12) -> List[dict]:
        """Останні місяці, найновіші зверху — для короткої історії в /ibkr."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM ibkr_contributions ORDER BY month DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def ibkr_totals(self) -> dict:
        """Підсумки лише по зроблених внесках: к-сть місяців, сума і паї по фондах."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT fund, COUNT(*) AS months, COALESCE(SUM(amount), 0) AS invested, "
                "COALESCE(SUM(units), 0) AS units FROM ibkr_contributions "
                "WHERE status='done' GROUP BY fund"
            ).fetchall()
        return {
            r["fund"]: {
                "months": r["months"],
                "invested": round(r["invested"] or 0.0, 2),
                "units": r["units"] or 0.0,
            }
            for r in rows
        }
