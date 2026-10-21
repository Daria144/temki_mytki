"""Смоук третьої хвилі: справжні тексти журналу вкладів на живих даних.

Нічого нікому не шле — замість Telegram підставлений фейковий бот, який друкує
повідомлення в консоль. База тимчасова, тому робочий стан бота не чіпається.

Показує саме те, що вона побачить: підтвердження запису, нагадування в день
розморозки, падіння ставки, розбіжність із балансом біржі, тижневий огляд,
блок-цитату під акцією і пораду по чужій монеті.

Запуск на сервері: /home/ubuntu/invest-bot/.venv/bin/python .cursor/smoke_wave3.py
"""
import asyncio
import datetime
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import alerts, budget, journal
from app.config import config
from app.earn import (
    ALT_TOPUP_MAX,
    WATCHED,
    Earn,
    alt_coin_allowed,
    alt_offers,
    alt_position,
    best_instant_rates,
    classify_risk,
    RISK_HIGH,
)
from app.formatting import (
    alt_coin_lines,
    journal_confirm_text,
    journal_due_text,
    promo_quote,
    rate_drop_lines,
    reconcile_lines,
    to_html,
    weekly_review_text,
)
from app.handlers import Services
from app.market import CoinFacts, Market
from app.portfolio import PortfolioReader
from app.service import coin_reliability, parse_deposits_text
from app.storage import STATUS_ACTIVE, Storage

TODAY = datetime.date.today()

# Саме ті рядки, які вона й пише — з завдання, без причісування.
RAW = ("200 usdt bybit savings 6.77\n"
       "500 usdt bybit on-chain 4.75\n"
       "0.01 btc okx simple earn 0.5 на 45 днів")


class PrintBot:
    def __init__(self):
        self.n = 0

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        self.n += 1
        print(f"\n===== ПОВІДОМЛЕННЯ {self.n} ({len(text)} знаків) =====")
        print(text)
        if reply_markup:
            labels = [b.text for row in reply_markup.inline_keyboard for b in row]
            print(f"[кнопки: {labels}]")


def head(title):
    print(f"\n\n{'=' * 70}\n### {title}\n{'=' * 70}")


async def main():
    config.owner_id = config.owner_id or 1
    storage = Storage(os.path.join(tempfile.mkdtemp(), "smoke3.db"))
    storage.init()
    storage.mark_promos_seen(["Bybit:заглушка"])
    storage.mark_products_seen(["OKX:ZZZ:flex"])
    storage.set_earn_amount(1000.0)

    services = Services(
        storage=storage, market=Market(), earn=Earn(),
        portfolio=PortfolioReader() if config.has_exchange_keys else None,
        coin_facts=CoinFacts(),
    )
    offers = await services.earn.offers()
    rates = best_instant_rates(offers, WATCHED)
    print(f"живих пропозицій з бірж: {len(offers)}; порадник: "
          f"{'є ключ' if config.gemini_api_key else 'ключа немає, працює формула'}")

    head("1. Розбір вільного тексту й підтвердження запису")
    print(f"вона написала:\n{RAW}\n")
    deposits, by_ai = await parse_deposits_text(RAW, TODAY)
    print(f"розбирав: {'порадник (AI)' if by_ai else 'формула'}\n")
    print(journal_confirm_text(deposits))
    ids = [storage.earn_deposit_add(d, STATUS_ACTIVE) for d in deposits]

    head("2. Нагадування В ДЕНЬ розморозки (+ кнопки «забрала»/«продовжила»)")
    # Підсовуємо розморозку на сьогодні, щоб побачити справжній текст.
    storage.earn_deposit_extend(ids[-1], TODAY.isoformat())
    bot = PrintBot()
    await alerts.journal_check(bot, services, TODAY)
    if bot.n == 0:
        print("(біржі не віддали ставки — нагадування не склалось)")

    head("3. Падіння ставки у гнучкому вкладі")
    drops = journal.rate_drops(storage.earn_deposits(), rates,
                              alerts.MIN_GAIN_YEAR)
    if not drops:
        # Живі ставки могли не просісти — показуємо на її ж записі з вигаданим «зараз».
        print("(на живих ставках падіння немає — показую на прикладі "
              "$1000 під 6.77%, які просіли до 4%)")
        fake = dict(storage.earn_deposits()[0], amount=1000.0, apr=0.0677)
        drops = journal.rate_drops([fake], {"USDT": (0.04, None)}, alerts.MIN_GAIN_YEAR)
    print("\n".join(rate_drop_lines(drops, {c: o for c, (_a, o) in rates.items()})))

    head("4. Звірка журналу з реальними балансами бірж")
    pf = None
    if services.portfolio is not None:
        try:
            pf = await services.portfolio.fetch()
        except Exception as e:  # noqa: BLE001
            print(f"(ключі не відповіли: {e})")
    rows = journal.reconcile(pf, storage.earn_deposits())
    if rows:
        print("\n".join(reconcile_lines(rows)))
    else:
        print("(ключів немає або журнал збігається — показую на прикладі)")
        from app.portfolio import EARN, Holding, Portfolio
        demo = Portfolio([Holding("Bybit", "USDT", 340.0, 340.0, EARN, None, "Savings")],
                         [], 0.0)
        demo_deps = [{"coin": "USDT", "amount": 200.0, "exchange": "Bybit",
                      "product": "Savings"}]
        print("\n".join(reconcile_lines(journal.reconcile(demo, demo_deps))))

    head("5. Тижневий огляд (неділя, тільки вклади)")
    active = storage.earn_deposits()
    week_prev = {c: apr * 0.7 for c, (apr, _o) in rates.items()}
    changes = alerts._week_changes(rates, week_prev, {"USDT": 700.0})
    best = {c: rates[c][1] for c in ("USDT", "BTC") if c in rates}
    print(weekly_review_text(active, changes[:3], best))

    head("6. Блок-цитата під живою акцією (англійською, без сум)")
    promos = [p for p in await services.earn.promos() if p.candidate]
    if promos:
        p = promos[0]
        print(f"акція: {p.exchange} — {p.title}")
        quote = promo_quote(p, {"coin": "BTC", "apr_pct": 10, "locked_days": 3})
        print("\n--- як це піде в Telegram (HTML) ---")
        print(to_html(quote))
    else:
        print("(живих акцій зараз немає)")

    head("7. Порада по чужій монеті — з незадіяного залишку бюджету")
    unknown = [c for c in sorted({o.coin for o in offers})
               if classify_risk(c) == RISK_HIGH][:6]
    trust = await coin_reliability(services.coin_facts, unknown)
    for coin, info in trust.items():
        facts = info.get("facts") or {}
        mark = "підходить" if alt_coin_allowed(facts) else "НЕ підходить"
        print(f"  {coin}: місце {facts.get('rank')}, вік "
              f"{facts.get('age_years') or facts.get('age_years_at_least')}, "
              f"невизначена={facts.get('ambiguous')} -> {mark}")

    good = next((c for c, i in trust.items() if alt_coin_allowed(i.get("facts"))), None)
    if good is None:
        # Живі акції не завжди трапляються на топ-50 — показуємо на ATOM.
        good = "ATOM"
        trust.setdefault(good, {"facts": {"rank": 40, "age_years": 6.0}})
        print(f"\n(серед живих монет топ-50 не трапилось — показую на {good})")
    variants = alt_offers(offers, good) or alt_offers(offers, "ATOM")
    if variants:
        offer = variants[0]
        facts = trust[good].get("facts")
        print("\n--- КОЛИ ВІЛЬНИЙ ЗАЛИШОК БЮДЖЕТУ Є ($200) ---")
        print("\n".join(alt_coin_lines(good, offer, alt_position(200.0), facts, 0.0)))
        print("\n--- КОЛИ ЗАЛИШКУ НЕМА ($0, у заявках висить $200) ---")
        print("\n".join(alt_coin_lines(good, offer, alt_position(0.0), facts, 200.0)))
        print(f"\n(добровільне докидання обмежене ${ALT_TOPUP_MAX:g})")
    else:
        print("(біржі не дали жодного гнучкого продукту під цю монету)")

    head("8. Повне earn-сповіщення на живих даних")
    bot = PrintBot()
    await alerts.earn_check(bot, services)
    if bot.n == 0:
        print("(порожньо — сьогодні нема ні нових акцій, ні стрибків ставок)")

    head("9. Стан бюджету, з якого беруться гроші на альткоїни")
    st = budget.status(storage)
    print(f"бюджет активний: {st['active']}, вільний залишок: "
          f"${budget.unused_free(storage):g}")


asyncio.run(main())
