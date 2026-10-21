"""Місячний бюджет DCA: ставимо суму на місяць — бот плавно розгортає її за місяць.

Стиль — гібрид: базовий темп рівномірний, але на просадках вкладаємо більше.
Невикористаний залишок наприкінці місяця переноситься на наступний (carry).
Відставання від темпу нагадуємо мʼяко, не форсуючи.
"""
from __future__ import annotations

import calendar
import datetime
from typing import Optional


def current_month() -> str:
    today = datetime.date.today()
    return f"{today.year:04d}-{today.month:02d}"


def _days_in_month(month: str) -> int:
    year, mon = (int(x) for x in month.split("-"))
    return calendar.monthrange(year, mon)[1]


def status(storage) -> dict:
    """Зведення по активному бюджету поточного місяця.

    active=False, якщо на цей місяць бюджет ще не заданий.

    Темп, залишок і відставання рахуються ЛИШЕ за реально купленим (deployed).
    Гроші у виставлених заявках (reserved) показуємо окремо: заявка може ніколи
    не спрацювати, і рахувати її як витрату — значить обманювати себе темпом.
    """
    b = storage.get_budget()
    cur = current_month()
    carry = b.get("carry", 0.0)
    active = bool(b.get("month")) and b["month"] == cur and b.get("amount", 0) > 0

    if not active:
        return {
            "active": False,
            "month": cur,
            "carry": carry,
            "total": 0.0,
            "deployed": 0.0,
            "reserved": 0.0,
            "remaining": 0.0,
        }

    total = float(b["amount"])
    deployed = storage.deployed_in_month(cur)
    reserved = storage.reserved_in_month(cur)
    remaining = max(total - deployed, 0.0)

    days_total = _days_in_month(cur)
    day_now = datetime.date.today().day
    days_left = days_total - day_now + 1
    target = total * (day_now / days_total)
    behind = max(target - deployed, 0.0)

    return {
        "active": True,
        "month": cur,
        "carry": carry,
        "total": round(total, 2),
        "deployed": round(deployed, 2),
        "reserved": round(reserved, 2),
        "remaining": round(remaining, 2),
        "days_total": days_total,
        "day_now": day_now,
        "days_left": days_left,
        "target": round(target, 2),
        "behind": round(behind, 2),
        "on_track": behind <= total * 0.05,
    }


def unused_free(storage) -> float:
    """Незадіяний залишок місячного бюджету: ще не куплено й не висить у заявках.

    Саме ці гроші — єдине джерело для дрібних earn-позицій у чужих монетах.
    Заявки віднімаємо за ВСІ місяці, а не за поточний: лімітка живе тижнями й
    легко переживає межу місяця, а гроші під нею вже обіцяні просадці.
    """
    st = status(storage)
    if not st.get("active"):
        return 0.0
    try:
        reserved = storage.reserved_for_buys()
    except Exception:  # noqa: BLE001
        reserved = st.get("reserved", 0.0)
    return round(max(float(st["remaining"]) - float(reserved or 0.0), 0.0), 2)


def suggested_tranche(st: dict, dip: bool = False) -> float:
    """Скільки МАКСИМУМ розумно вкласти за ОДНУ докупку, щоб розтягнути бюджет на місяць.

    Гібрид: базовий темп — тижнева порція (бюджет / к-сть тижнів місяця).
    На просадці дозволяємо до 2× тижневої порції ("вкладаємо більше, коли дешево").
    Ніколи не більше за залишок. Наприкінці місяця стеля плавно піднімається до залишку,
    щоб встигнути (мʼякий catchup), але форсувати не змушуємо — недовкладене перенесеться.
    """
    if not st.get("active"):
        return 0.0
    remaining = float(st.get("remaining", 0.0))
    if remaining <= 0:
        return 0.0
    total = float(st.get("total", 0.0))
    days_total = int(st.get("days_total", 30))
    behind = float(st.get("behind", 0.0))                 # наскільки відстаємо від рівномірного темпу

    weeks_total = max(round(days_total / 7), 1)          # ~4 тижні
    weekly = total / weeks_total                          # базова тижнева порція
    cap = weekly * 2 if dip else weekly                   # на просадці — до 2×

    # МʼЯКИЙ catchup: дозволяємо дотягнути лише до рівномірного темпу (закрити відставання),
    # а не зливати весь залишок. Форсувати не змушуємо — рішення % все одно за AI/формулою.
    if behind > 0:
        cap = max(cap, behind)

    return round(min(cap, remaining), 2)


def is_dip(data: dict, fng: int) -> bool:
    """Дешеве вікно для прискорення темпу: сильний страх або помітне падіння за добу."""
    if fng is not None and fng < 30:
        return True
    for d in (data or {}).values():
        ch = d.get("change_24h")
        if ch is not None and ch <= -0.05:
            return True
    return False


def rollover_if_needed(storage) -> Optional[float]:
    """Якщо активний бюджет — з минулого місяця, переносимо його залишок у carry.

    Повертає суму перенесеного залишку (для повідомлення), або None якщо переносити нічого.
    """
    b = storage.get_budget()
    bm = b.get("month")
    cur = current_month()
    if not bm or bm == cur:
        return None

    prev_total = float(b.get("amount", 0) or 0)
    # Переносимо все, що не перетворилось на монети. Заявки, які висіли на кінець
    # місяця, грошей не витратили — вони мають лишитись у наступному бюджеті.
    prev_deployed = storage.deployed_in_month(bm)
    leftover = max(prev_total - prev_deployed, 0.0)
    new_carry = float(b.get("carry", 0) or 0) + leftover
    storage.set_budget_carry(new_carry)
    storage.clear_active_budget()
    return leftover


def set_for_month(storage, entered_amount: float) -> dict:
    """Задає бюджет на поточний місяць: введена сума + перенесений залишок (carry)."""
    rollover_if_needed(storage)
    carry = float(storage.get_budget().get("carry", 0) or 0)
    total = round(entered_amount + carry, 2)
    storage.set_budget(current_month(), total)
    storage.set_budget_carry(0)
    return {
        "entered": round(entered_amount, 2),
        "carry": round(carry, 2),
        "total": total,
        "month": current_month(),
    }


def add_for_month(storage, extra_amount: float) -> dict:
    """Доливає суму до бюджету поточного місяця (не скидаючи вже вкладене).

    Якщо на цей місяць бюджет ще не заданий — поводиться як set_for_month
    (introduced сума + перенесений залишок).
    """
    rollover_if_needed(storage)
    cur = current_month()
    b = storage.get_budget()
    active = bool(b.get("month")) and b["month"] == cur and float(b.get("amount", 0) or 0) > 0
    if not active:
        res = set_for_month(storage, extra_amount)
        res["added"] = res["entered"]
        res["was_new"] = True
        return res

    prev_total = float(b.get("amount", 0) or 0)
    total = round(prev_total + extra_amount, 2)
    storage.set_budget(cur, total)
    return {
        "added": round(extra_amount, 2),
        "carry": 0.0,
        "total": total,
        "month": cur,
        "was_new": False,
    }
