"""AI-трейдер на Google Gemini.

`decide()` — головна функція: AI отримує 'торговий екран' (індикатори по монетах) і
сам приймає рішення як професійний трейдер (стиль — збалансований DCA): скільки бюджету
вкласти зараз, як розподілити між BTC/ETH, наскільки глибокі сходинки драбинки і чому.

AI повертає лише СТРАТЕГІЮ у відсотках — точні ціни/кількості рахує бот (engine).
Якщо ключа немає або сервіс недоступний — повертає None, і бот падає назад на формулу.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Dict, List, Optional

import httpx

from app.config import config

log = logging.getLogger(__name__)

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Порадник іноді думає довго (модель спершу «міркує», потім пише JSON).
# Старі 30 с обривали його на пів слові й самі створювали третину всіх відмов.
AI_TIMEOUT = 90

# Модель, яка відповіла минулого разу — пробуємо її ПЕРШОЮ.
# Основний аліас буває перевантажений добами, і без цього кожен виклик спершу
# дарма стукав у нього (дві спроби + паузи), і лише потім ішов на робочу модель.
_preferred_model: Optional[str] = None

# Здоровʼя порадника: коли востаннє відповів і відколи мовчить поспіль.
# Потрібно, щоб бот сам сказав власниці, що порадник фактично вимкнений.
_last_success: Optional[float] = None
_failing_since: Optional[float] = None


def _models() -> list:
    """Моделі в порядку спроб: спершу та, що працювала, далі основна й запасна."""
    out = []
    for name in (_preferred_model, config.gemini_model, config.gemini_fallback_model):
        if name and name not in out:
            out.append(name)
    return out


def _note_success(model: str) -> None:
    global _preferred_model, _last_success, _failing_since
    _preferred_model = model
    _last_success = time.time()
    _failing_since = None


def _note_failure() -> None:
    global _failing_since
    if _failing_since is None:
        _failing_since = time.time()


def health() -> dict:
    """Стан порадника для алертів: скільки секунд поспіль він не відповідає."""
    silent = (time.time() - _failing_since) if _failing_since else 0.0
    return {
        "ok": _failing_since is None,
        "last_success": _last_success,
        "silent_for": silent,
    }


def reset_health() -> None:
    """Скидає памʼять про стан порадника — потрібно тестам."""
    global _preferred_model, _last_success, _failing_since
    _preferred_model = None
    _last_success = None
    _failing_since = None


def _retry_pointless(err: Exception) -> bool:
    """Помилки, які повтором тієї самої моделі не лікуються.

    429/503 — черга на боці Google (треба інша модель), 401/403 — проблема з ключем.
    Саме зайві повтори й з'їдали секунди на кожному виклику.
    """
    status = getattr(getattr(err, "response", None), "status_code", None)
    return status in (401, 403, 429, 503)


def _auth() -> dict:
    """Ключ віддаємо заголовком, а не в адресі: httpx пише URL у лог, і ключ
    осідав би відкритим текстом у journalctl."""
    return {"x-goog-api-key": config.gemini_api_key}

# Персона: ТЕРПЛЯЧИЙ консервативний DCA-трейдер. Докуповує лише на реальних просадках.
SYSTEM = (
    "Ти — досвідчений ОБЕРЕЖНИЙ трейдер, що веде ДОВГОСТРОКОВЕ накопичення BTC та ETH "
    "за стратегією DCA. Головний принцип: ТЕРПІННЯ. Купувати варто ЛИШЕ тоді, коли є реальна "
    "просадка ціни. Якщо ринок спокійний, дорогий або просто 'норма' — НЕ докуповуй, тримай "
    "гроші в резерві й чекай. Краще пропустити невеликий рух, ніж купити не дешево.\n\n"
    "ТОРГОВИЙ ЕКРАН (індикатори по кожній монеті):\n"
    "- price, change_24h_pct: поточна ціна і зміна за добу.\n"
    "- sma200: 200-денна середня; mayer = price/SMA200 (<1 дешево, >2.4 дорого).\n"
    "- rsi14: <30 перепроданість (дешево), >70 перекупленість (дорого).\n"
    "- annual_pos_pct: положення в річному діапазоні (0%=річне дно, 100%=річний пік).\n"
    "- pullback_30d_pct: наскільки ціна нижча за пік останніх 30 днів (0=на піку, -20=на 20% нижче).\n"
    "- trend_30d_pct, trend_90d_pct: зміна ціни за 30 і 90 днів у % (НАПРЯМОК руху). "
    "Від'ємні — ціна сповзає вниз (затяжне падіння); додатні — росте (розворот/підйом). "
    "Може бути null, якщо історії замало.\n"
    "- dist_from_sma200w_pct: відхилення від 200-ТИЖНЕВОЇ середньої (довгий цикл): "
    "<0 — нижче довгострокового дна циклу (історично дуже дешево), великий + — перегрів циклу. "
    "Може бути null, якщо історії замало.\n"
    "- daily_volatility_pct: денна волатильність (для глибини сходинок).\n"
    "Також дають індекс страху/жадібності ринку (0=страх, 100=жадібність) і ТВОЮ ПОЗИЦІЮ "
    "(скільки відкритих ордерів уже стоїть і скільки днів тому була остання докупка).\n\n"
    "ПЛЕЙБУК ДОСВІДЧЕНОГО DCA-ТРЕЙДЕРА (дотримуйся):\n"
    "1. Купуй страх, не жадібність: найкращі докупки — у паніці (низький F&G, RSI<30), а не на хайпі.\n"
    "2. НЕ лови падаючий ніж усім капіталом: у сильному падінні заходь ЧАСТИНАМИ, лишай резерв на глибше.\n"
    "3. Поважай довгий цикл: dist_from_sma200w_pct нижче 0 — рідкісний дешевий момент; біля річного піку — терпіння.\n"
    "4. Не усереднюйся надто часто: якщо нещодавно (кілька днів тому) вже докуповувала або вже висить "
    "багато відкритих ордерів — будь стриманішою, зменш розмір або почекай.\n"
    "5. Дисципліна важливіша за heroїзм: пропустити рух не страшно, переплатити — гірше.\n\n"
    "ЖОРСТКЕ ПРАВИЛО ДОКУПКИ. Рекомендуй купувати (deploy_now_pct > 0) ЛИШЕ якщо виконані ОБИДВІ умови:\n"
    "  (А) реальна просадка: change_24h_pct <= -5%, АБО pullback_30d_pct <= -10%, АБО ціна близько "
    "річного/циклового дна;\n"
    "  (Б) підтвердження дешевизни: RSI < 35, АБО Mayer < 1.1, АБО annual_pos_pct < 35%, "
    "АБО dist_from_sma200w_pct < 0.\n"
    "Якщо хоча б одна умова НЕ виконана — deploy_now_pct = 0 (чекати), усе в резерв.\n\n"
    "СКІЛЬКИ ВКЛАДАТИ (заходь частинами — НІКОЛИ не 100% на першу/неглибоку просадку):\n"
    "- легка просадка з підтвердженням → 20-35%;\n"
    "- помітне падіння + перепроданість → 40-60%;\n"
    "- глибока капітуляція (сильний страх, RSI<25, нижче 200-тижневої) → 70-90%;\n"
    "- 100% лише у виняткову історичну паніку. Якщо вже багато відкритих ордерів або недавня "
    "докупка — зменш частку.\n\n"
    "МІСЯЧНИЙ БЮДЖЕТ (якщо вказаний — гібридний DCA): мета — за місяць плавно вкласти бюджет ЧАСТИНАМИ, "
    "ніколи не весь одразу. ВАЖЛИВО: 'сума на цей раунд' — це вже обмежена ПОРЦІЯ на одну докупку "
    "(тижневий темп), а НЕ весь залишок. deploy_now_pct рахуй як % САМЕ від цієї порції. Дані бюджету "
    "(вкладено/залишок/відставання) — лише контекст темпу.\n"
    "- На реальних просадках використовуй більшу частину порції; на дорогому ринку — менше або нічого "
    "(невитрачене перенесеться на наступний місяць, воно не згорає).\n"
    "- МʼЯКИЙ catchup: якщо помітно відстаємо від темпу (behind великий) І ціна хоча б НЕ дорога — "
    "дозволено повніше використати порцію (вищий deploy_now_pct), щоб надолужити, але без фанатизму.\n"
    "- НЕ намагайся вкласти весь місячний бюджет за один раунд, навіть у сильну просадку — для цього "
    "будуть наступні дні й сигнали.\n\n"
    "ВРАХУЙ ВЖЕ ВІДКРИТІ ОРДЕРИ (якщо надані): \n"
    "- НЕ дублюй: не став новий ордер по монеті біля ціни, де вже є відкрита лімітка. По монеті з "
    "наявними ордерами нові рівні мають бути ЛИШЕ ГЛИБШЕ (нижче) за вже відкриті.\n"
    "- ПРІОРИТЕТ монеті з МЕНШОЮ кількістю відкритих ордерів (або зовсім без них): саме їй давай "
    "БІЛЬШЕ ваги (weight_pct), бо інша вже 'закрита' відкритими ордерами. Напр. якщо по BTC ордерів "
    "нема, а по ETH вже висить — спершу думай про BTC.\n"
    "- Це МʼЯКИЙ пріоритет: можна брати й іншу монету, АЛЕ переходь до неї, якщо пріоритетна зараз "
    "дорога / нема гарного рівня для докупки, АБО інша помітно вигідніша (дешевша) прямо зараз.\n"
    "- Якщо доречних рівнів глибше за наявні зараз нема — краще не пропонуй цю монету (deploy менше/0).\n\n"
    "СЕРЕДНЯ ЦІНА ВХОДУ (якщо надана — cost-basis): по монеті можуть дати середню ціну твоїх "
    "куплених монет і відхилення поточної ціни від неї (%).\n"
    "- ОХОЧІШЕ докуповуй монету, коли поточна ціна <= твоєї середньої (докупка ЗНИЖУЄ середню — це добре).\n"
    "- ОБЕРЕЖНІШЕ (менше або нічого), коли ціна помітно ВИЩЕ за середню (докупка підняла б середню — "
    "краще зачекати на просадку). Це МʼЯКЕ правило: поєднуй із дешевизною ринку, не роби з нього догму.\n\n"
    "ЦІЛЬОВИЙ БАЛАНС BTC/ETH (якщо наданий — target allocation): дають бажану пропорцію портфеля "
    "і фактичну (за поточною вартістю куплених монет).\n"
    "- При розподілі ваг (weight_pct) зміщуй БІЛЬШЕ ваги в монету, якої ЗАМАЛО відносно цілі "
    "(щоб вирівняти портфель до бажаної пропорції). Це МʼЯКО і в поєднанні з дешевизною: "
    "якщо 'недозважена' монета зараз дорога — не форсуй, краще інша дешевша.\n\n"
    "ТЕМП ЗАЛЕЖНО ВІД ТРЕНДУ (trend_30d_pct / trend_90d_pct):\n"
    "- Затяжне падіння (обидва тренди помітно від'ємні) — заходь ПОВІЛЬНІШЕ й меншими частинами, "
    "лишай більше резерву: дно може бути нижче, попереду ще будуть кращі ціни.\n"
    "- Розворот угору після просадки (ціна була низько, а тренд починає випрямлятись/рости) — "
    "можна діяти трохи АКТИВНІШЕ, бо момент дешевизни може швидко зникнути.\n"
    "- Не описуй ці тренди жаргоном у reasoning — просто 'ціна довго падає' / 'почала відновлюватись'.\n\n"
    "УРОКИ З ІСТОРІЇ (якщо надані): врахуй власні минулі помилки. Якщо часто 'впущені можливості' "
    "(ставили ліміт надто низько і ціна втекла) — роби сходинки трохи ближче до поточної ціни, "
    "не став усі ордери надто глибоко. Якщо входили дорожче за середню — будь терплячішою. "
    "Якщо докупки переважно вдалі — тримай той самий підхід.\n\n"
    "Поля рішення:\n"
    "- deploy_now_pct: 0-100 за правилами вище.\n"
    "- по кожній монеті weight_pct: розподіл бюджету (дешевша/перепроданіша отримує більше; "
    "сума ваг ~100). Якщо deploy_now_pct=0 — ваги неважливі.\n"
    "- по кожній монеті ladder_drops_pct: 1-3 рівні просадки у % нижче ціни для лімітних ордерів "
    "(напр. [2, 4, 6]). Глибше падіння/волатильність → ширші сходинки. Якщо чекаємо — порожній список [].\n"
    "- moment: buy (реальна просадка з підтвердженням), accumulate (легка, але чітка просадка), "
    "cautious (дорогувато — зазвичай deploy=0), wait (норма/дорого/спокійно — deploy=0).\n"
    "- headline: короткий заголовок українською з доречним емодзі, ПРОСТИМИ словами.\n"
    "- reasoning: 2-4 речення дуже простою українською, як другу без досвіду.\n\n"
    "ВАЖЛИВО ПРО МОВУ (headline і reasoning): пиши так, щоб зрозуміла людина БЕЗ знань крипти. "
    "ЗАБОРОНЕНО використовувати терміни: Mayer, RSI, SMA, 200-денна/200-тижнева середня, "
    "волатильність, перепроданість/перекупленість, індекс, коефіцієнт, капітуляція, діапазон, "
    "score. Замість них — звичайні слова: 'ціна сильно впала', 'зараз дешево/дорого', "
    "'давно не було так дешево', 'ринок налякали', 'усі бояться/жадібні', 'краще почекати', "
    "'заходимо потроху', 'тримаємо частину грошей про запас'. Без гарантій, не вигадуй точних цін."
)

# Структура відповіді (Gemini structured output) — гарантує валідний JSON.
SCHEMA = {
    "type": "object",
    "properties": {
        "moment": {"type": "string", "enum": ["buy", "accumulate", "cautious", "wait"]},
        "headline": {"type": "string"},
        "deploy_now_pct": {"type": "number"},
        "coins": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "coin": {"type": "string"},
                    "weight_pct": {"type": "number"},
                    "ladder_drops_pct": {"type": "array", "items": {"type": "number"}},
                },
                "required": ["coin", "weight_pct", "ladder_drops_pct"],
            },
        },
        "reasoning": {"type": "string"},
    },
    "required": ["moment", "headline", "deploy_now_pct", "coins", "reasoning"],
}


def _position_line(position: Optional[Dict]) -> str:
    if not position:
        return "Твоя позиція: невідома."
    open_n = position.get("open_orders", 0)
    last_days = position.get("last_buy_days_ago")
    last = f"{last_days} дн. тому" if last_days is not None else "ще не докуповувала"
    line = f"Твоя позиція: відкритих ордерів {open_n}; остання докупка — {last}."

    by_coin = position.get("open_by_coin") or {}
    if by_coin:
        parts = []
        for coin, info in by_coin.items():
            mn, mx = info.get("min_price"), info.get("max_price")
            rng = f"${mn:g}" if mn == mx else f"${mn:g}–${mx:g}"
            parts.append(f"{coin}: {info['count']} ордер(и) на ${info.get('notional', 0):.0f} (ціни {rng})")
        line += " ВЖЕ ВІДКРИТІ ЛІМІТКИ — " + "; ".join(parts) + "."
    return line


def _cost_basis_line(snapshot: List[Dict], position: Optional[Dict]) -> Optional[str]:
    """Рядок про середню ціну входу і відхилення поточної ціни від неї (%)."""
    if not position:
        return None
    avg_entry = position.get("avg_entry") or {}
    if not avg_entry:
        return None
    price_by_coin = {str(c.get("coin", "")).upper(): c.get("price") for c in snapshot}
    parts = []
    for coin, avg in avg_entry.items():
        coin = str(coin).upper()
        if not avg:
            continue
        price = price_by_coin.get(coin)
        if price:
            dev = (price / avg - 1.0) * 100.0
            hint = "нижче середньої (докупка знизить середню)" if dev <= 0 else "вище середньої"
            parts.append(f"{coin}: середня ${avg:g}, зараз {dev:+.1f}% до неї ({hint})")
        else:
            parts.append(f"{coin}: середня ${avg:g}")
    if not parts:
        return None
    return "Твоя середня ціна входу — " + "; ".join(parts) + "."


def _allocation_line(allocation: Optional[Dict]) -> Optional[str]:
    """Рядок про цільову й фактичну пропорцію портфеля BTC/ETH."""
    if not allocation:
        return None
    target = allocation.get("target") or {}
    actual = allocation.get("actual") or {}
    if not target:
        return None
    tgt = ", ".join(f"{c} {float(v):.0f}%" for c, v in target.items())
    line = f"Цільова пропорція портфеля: {tgt}."
    if actual:
        act = ", ".join(f"{c} {float(v):.0f}%" for c, v in actual.items())
        line += f" Фактично зараз: {act}. Зміщуй ваги в монету, якої замало відносно цілі."
    else:
        line += " Поки що куплених монет нема — тримайся цільової пропорції."
    return line


def _budget_line(budget: Optional[Dict]) -> Optional[str]:
    if not budget or not budget.get("active"):
        return None
    # «Вкладено» — тільки реально куплене. Гроші в невиконаних заявках називаємо
    # окремо, щоб AI не плутав обіцянку з витратою і не занижував темп.
    line = (
        f"Місячний бюджет: всього ${budget['total']:.0f}, реально куплено на "
        f"${budget['deployed']:.0f}, залишок ${budget['remaining']:.0f}."
    )
    reserved = float(budget.get("reserved", 0) or 0)
    if reserved > 0:
        line += (
            f" Ще ${reserved:.0f} висить у виставлених заявках — вони ЩЕ НЕ витрачені "
            "й можуть не спрацювати."
        )
    return line + (
        f" До кінця місяця {budget['days_left']} дн. "
        f"За рівномірним темпом мали б вкласти ~${budget['target']:.0f} "
        f"(відставання ${budget['behind']:.0f})."
    )


def _build_prompt(snapshot: List[Dict], fng: int, amount: float,
                  position: Optional[Dict], budget: Optional[Dict],
                  learning: Optional[str], allocation: Optional[Dict] = None) -> str:
    lines = [
        f"Максимум на ЦЮ докупку (порція на один раунд, НЕ весь бюджет): ${amount:.0f}.",
        f"Індекс страху/жадібності ринку: {fng}/100.",
        _position_line(position),
    ]
    cost_basis_line = _cost_basis_line(snapshot, position)
    if cost_basis_line:
        lines.append(cost_basis_line)
    allocation_line = _allocation_line(allocation)
    if allocation_line:
        lines.append(allocation_line)
    budget_line = _budget_line(budget)
    if budget_line:
        lines.append(budget_line)
    if learning:
        lines.append(learning)
    lines += [
        "Торговий екран по монетах:",
        json.dumps(snapshot, ensure_ascii=False),
        "",
        "Дай рішення у заданому JSON-форматі.",
    ]
    return "\n".join(lines)


async def decide(snapshot: List[Dict], fng: int, amount: float,
                 position: Optional[Dict] = None,
                 budget: Optional[Dict] = None,
                 learning: Optional[str] = None,
                 allocation: Optional[Dict] = None) -> Optional[Dict]:
    """Повертає структуроване рішення трейдера (dict) або None, якщо AI недоступний."""
    if not config.gemini_api_key:
        return None
    payload = {
        "system_instruction": {"parts": [{"text": SYSTEM}]},
        "contents": [{"parts": [{"text": _build_prompt(snapshot, fng, amount, position, budget, learning, allocation)}]}],
        "generationConfig": {
            "temperature": 0.2,
            # 2.5-flash рахує токени думання в maxOutputTokens. Беремо з запасом,
            # щоб після думання лишалось досить на сам JSON (інакше відповідь обрізає).
            "maxOutputTokens": 8192,
            "responseMimeType": "application/json",
            "responseSchema": SCHEMA,
        },
    }
    # Модель зі схемою інколи віддає битий JSON — це варто повторити.
    # А перевантаження (429/503) повтором не лікується: одразу беремо іншу модель.
    last_err: Optional[Exception] = None
    for model in _models():
        url = API.format(model=model)
        for attempt in (1, 2):
            try:
                async with httpx.AsyncClient(timeout=AI_TIMEOUT) as cl:
                    r = await cl.post(url, headers=_auth(), json=payload)
                    r.raise_for_status()
                    data = r.json()
                text = data["candidates"][0]["content"]["parts"][0]["text"]
                decision = json.loads(text)
                if not isinstance(decision, dict) or "coins" not in decision:
                    raise ValueError("неочікувана структура відповіді")
                _note_success(model)
                return decision
            except Exception as e:  # noqa: BLE001
                last_err = e
                log.warning("AI-трейдер (%s) спроба %d невдала: %s", model, attempt, e)
                if _retry_pointless(e):
                    break
                if attempt == 1:
                    await asyncio.sleep(1.5)
    log.warning("AI-трейдер недоступний після повторів: %s", last_err)
    _note_failure()
    return None


# --- пояснення біржових акцій простою мовою ---

PROMO_SYSTEM = (
    "Ти радиш щодо крипто-акцій людині, яка НЕ розбирається в крипті й хоче знати лише "
    "дві речі: чи варто ЇЙ туди лізти і який ризик. Вона ПАСИВНА інвесторка: складає гроші "
    "надовго й НЕ торгує, АЛЕ хоче бачити КОНКРЕТНІ ЦИФРИ й вирішувати сама. Тобі дають "
    "заголовок акції, ТЕКСТ ОГОЛОШЕННЯ з сайту біржі й КОНТЕКСТ ЇЇ ГРОШЕЙ — скільки доларів "
    "лежить у кожній монеті, скільки вільних доларів і під який відсоток її гроші працюють ЗАРАЗ.\n"
    "ГОЛОВНЕ ПРАВИЛО: усі цифри бери ЛИШЕ з тексту оголошення. Нічого не додумуй і не "
    "округлюй «приблизно». Чого в тексті немає — лишай порожнім або null, це нормальна "
    "відповідь. Вигадана цифра гірша за відсутню.\n"
    "Витягни з тексту оголошення:\n"
    "- bonus_usd: розмір РАЗОВОГО бонусу в доларах, числом (ваучер на $5 -> 5). Якщо бонус "
    "не названий сумою або це відсоткова ставка, а не разовий подарунок — null;\n"
    "- bonus_what: у чому саме бонус, коротко й БЕЗ повторення суми («ваучер на покриття "
    "збитку», «підвищена ставка 10% річних»);\n"
    "- requirements: що треба зробити, щоб його отримати, з цифрами, з малої літери й у "
    "формі «створити бота на $200 і торгувати 3 дні». КОРОТКО, до 90 знаків: лише головні "
    "умови, без дрібних пунктів регламенту. Порожньо, якщо в тексті умов немає;\n"
    "- coin: тікер монети, якої стосується акція (BTC, USDT...), або порожньо;\n"
    "- apr_pct: річна ставка у відсотках числом, якщо акція саме про ставку (10% -> 10); "
    "інакше null;\n"
    "- min_amount / max_amount: мінімальна й максимальна сума участі В МОНЕТІ з поля coin "
    "(«від 0.001 BTC до 1 BTC» -> 0.001 і 1). Немає в тексті — null;\n"
    "- period_end: дата завершення акції у форматі YYYY-MM-DD («Aug 28, 2026, 12 PM UTC» -> "
    "2026-08-28). Немає — null;\n"
    "- locked_days: на скільки днів гроші блокуються («3-day fixed plan» -> 3). Якщо гроші "
    "можна забрати будь-коли або про строк не сказано — 0.\n"
    "Також для КОЖНОГО заголовка поверни:\n"
    "- is_offer: true, якщо це справді пропозиція заробити (відсоток, бонус, кешбек, "
    "розіграш, знижена комісія) для звичайної приватної особи; false — якщо це новина, "
    "лістинг, оновлення застосунку, партнерство, підсумки чи будь-що, де їй нема що "
    "отримати. Суди по ТЕКСТУ, а не по красивих словах у заголовку;\n"
    "- title_uk: короткий зрозумілий заголовок українською (до 60 знаків, без жаргону);\n"
    "- what: ОДНЕ речення простою мовою, що треба зробити і що за це дадуть;\n"
    "- kind: 'deposit' якщо треба просто тримати/покласти гроші й отримувати відсоток; "
    "'trading' якщо треба торгувати, робити оборот чи ставки; 'other' — решта;\n"
    "- verdict: 'worth' — їй справді варто глянути; 'maybe' — може бути цікаво, але є умова "
    "чи сумнів; 'skip' — це не для неї;\n"
    "- why: ОДНЕ КОРОТКЕ речення (до 100 знаків), чому саме такий вердикт, з привʼязкою до "
    "ЇЇ монет і ЇЇ поточної ставки (напр. «твій BTC зараз працює під 0.5%, а тут обіцяють "
    "до 10%»). Не пиши абстрактних відсотків без привʼязки до її грошей і НЕ ПОВТОРЮЙ те, "
    "що вже сказав у bonus_what і requirements — назви лише головну причину;\n"
    "- action: перший конкретний крок, куди натиснути й що перевірити (напр. «Bybit → Earn → "
    "BTC, перевір максимальну суму»). Для вердикту 'skip' — порожній рядок;\n"
    "- warn: коротке попередження, якщо є підступ (треба наторгувати оборот, обмежена сума, "
    "потрібна нова монета). Якщо підступу немає — порожній рядок.\n"
    "ЖОРСТКІ ПРАВИЛА ВЕРДИКТУ:\n"
    "1. Не вигадуй умов, яких немає в тексті: жодних строків, сум і відсотків зі стелі.\n"
    "2. Якщо ліміт суми в тексті НЕ названий, а у відсотку є 'up to' / 'до' — вердикт НЕ ВИЩЕ "
    "'maybe', а в warn вимагай перевірити ліміт. НІКОЛИ не обіцяй підвищений відсоток на весь "
    "її обсяг монети.\n"
    "3. Бонус за ТОРГІВЛЮ (trade, volume, futures, grid, bot, tournament, competition) — "
    "дивись на РОЗМІР бонусу. Менше $50 — 'skip', і в why обовʼязково назви суму бонусу й "
    "умову, щоб було видно, ЧОМУ не варто. Від $50 — 'maybe' з чесним попередженням, що на "
    "торгівлі можна втратити більше, ніж бонус. Сама торгівля для неї — завжди мінус.\n"
    "4. Якщо монети з акції в неї немає і вільних доларів мало — 'skip' або 'maybe', не 'worth'.\n"
    "5. 'worth' лише тоді, коли монета в неї Є (або вистачає вільних доларів) І акція помітно "
    "вигідніша за її поточну ставку по цій монеті.\n"
    "6. Якщо акція вже закінчилась за датою — вердикт 'skip', а period_end заповни точно.\n"
    "Пиши тільки українською, простими словами, звертайся на «ти»."
)

PROMO_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "i": {"type": "integer"},
                    "is_offer": {"type": "boolean"},
                    "title_uk": {"type": "string"},
                    "what": {"type": "string"},
                    "kind": {"type": "string", "enum": ["deposit", "trading", "other"]},
                    "verdict": {"type": "string", "enum": ["worth", "maybe", "skip"]},
                    "why": {"type": "string"},
                    "action": {"type": "string"},
                    "warn": {"type": "string"},
                    # Конкретика зі сторінки оголошення. Усе nullable: «немає в тексті»
                    # має бути дозволеною відповіддю, інакше модель почне вигадувати.
                    "coin": {"type": "string"},
                    "bonus_usd": {"type": "number", "nullable": True},
                    "bonus_what": {"type": "string"},
                    "requirements": {"type": "string"},
                    "apr_pct": {"type": "number", "nullable": True},
                    "min_amount": {"type": "number", "nullable": True},
                    "max_amount": {"type": "number", "nullable": True},
                    "period_end": {"type": "string", "nullable": True},
                    "locked_days": {"type": "number", "nullable": True},
                },
                "required": ["i", "is_offer", "title_uk", "what", "kind", "verdict", "why"],
            },
        }
    },
    "required": ["items"],
}


def _promo_context_lines(context: Optional[Dict]) -> List[str]:
    """Контекст її грошей для промо-аналізу: без нього вердикт був би про когось іншого."""
    if not context:
        return []
    lines = []
    if context.get("today"):
        # Без сьогоднішньої дати порадник не бачить, що акція вже добігла кінця.
        lines.append(f"Сьогодні {context['today']}.")
    amounts = {c: v for c, v in (context.get("amounts") or {}).items() if v and v > 0}
    if amounts:
        top = sorted(amounts.items(), key=lambda kv: -kv[1])[:8]
        lines.append("Її гроші зараз: " + ", ".join(f"{c} ~${v:,.0f}" for c, v in top) + ".")
    else:
        lines.append("Монет у неї зараз нема (або їх не видно).")

    free = float(context.get("free_usd") or 0.0)
    lines.append(
        f"Вільних доларів, які можна кудись покласти: ~${free:,.0f}." if free > 0
        else "Вільних доларів зараз практично немає."
    )

    rates = {c: r for c, r in (context.get("rates") or {}).items() if r}
    if rates:
        # Саме з цією ставкою AI має порівнювати обіцянку акції — інакше «10% річних»
        # звучить однаково і для BTC під 0.5%, і для USDT під 6.8%.
        shown = sorted(rates.items(), key=lambda kv: -kv[1])[:12]
        lines.append(
            "Під який відсоток її гроші можуть працювати ЗАРАЗ (без блокування): "
            + ", ".join(f"{c} {r * 100:.2f}%" for c, r in shown) + "."
        )
    return lines


# Скільки всього тексту оголошень кладемо в один запит. Акцій може бути й десяток,
# і без спільної межі запит розпухає, а модель починає плутати їх між собою.
PROMO_BODY_BUDGET = 12000
PROMO_BODY_MIN = 700


def _promo_listing(titles: List[str], bodies: Optional[Dict[str, str]]) -> str:
    """Заголовок + текст сторінки на кожну акцію: цифри бонусу є лише в тексті."""
    bodies = bodies or {}
    per_item = max(PROMO_BODY_BUDGET // max(len(titles), 1), PROMO_BODY_MIN)
    out = []
    for i, title in enumerate(titles):
        out.append(f"{i}. ЗАГОЛОВОК: {title}")
        body = (bodies.get(title) or "").strip()[:per_item]
        out.append(f"   ТЕКСТ ОГОЛОШЕННЯ: {body}" if body
                   else "   ТЕКСТ ОГОЛОШЕННЯ: (сторінка не відкрилась — суди лише по заголовку)")
    return "\n".join(out)


async def explain_promos(titles: List[str], context: Optional[Dict] = None,
                         bodies: Optional[Dict[str, str]] = None) -> Dict[str, Dict]:
    """Заголовки акцій -> пояснення + вердикт українською. {} якщо AI недоступний.

    context: {'amounts': {монета: $}, 'free_usd': $, 'rates': {монета: частка на рік}, 'today'}.
    bodies: {заголовок: текст сторінки оголошення} — звідки беруться цифри бонусу.
    """
    if not config.gemini_api_key or not titles:
        return {}
    ask = _promo_context_lines(context) + ["", "Розбери ці акції:",
                                           _promo_listing(titles, bodies)]
    payload = {
        "system_instruction": {"parts": [{"text": PROMO_SYSTEM}]},
        "contents": [{"parts": [{"text": "\n".join(ask).strip()}]}],
        "generationConfig": {
            "temperature": 0.2,
            # Полів стало вдвічі більше, та ще й на десяток акцій — старих 4096
            # разом із «думанням» моделі не вистачало, відповідь приходила обрізаною.
            "maxOutputTokens": 16384,
            "responseMimeType": "application/json",
            "responseSchema": PROMO_SCHEMA,
        },
    }
    items = None
    for model in _models():
        try:
            async with httpx.AsyncClient(timeout=AI_TIMEOUT) as cl:
                r = await cl.post(API.format(model=model), headers=_auth(), json=payload)
                r.raise_for_status()
                data = r.json()
            items = json.loads(data["candidates"][0]["content"]["parts"][0]["text"])["items"]
            # Запамʼятовуємо робочу модель, як і решта гілок: інакше наступний
            # виклик знову починав би з перевантаженого аліаса і палив зайвий запит.
            _note_success(model)
            break
        except Exception as e:  # noqa: BLE001
            log.warning("Пояснення акцій (%s) недоступні: %s", model, e)
    if items is None:
        return {}

    out: Dict[str, Dict] = {}
    for it in items:
        idx = it.get("i")
        if isinstance(idx, int) and 0 <= idx < len(titles):
            out[titles[idx]] = it
    return out


# --- наскільки надійна сама монета (а не ставка по ній) ---

COIN_SYSTEM = (
    "Ти пояснюєш людині БЕЗ знань крипти, наскільки надійна конкретна монета. "
    "Тобі дають сухі факти з CoinGecko: скільки років монета на ринку, яке в неї місце "
    "за розміром і скільки грошей проходить через неї за добу.\n"
    "Головна думка, яку треба донести: для незнайомої монети ризик САМОЇ МОНЕТИ важливіший "
    "за відсоток. Високий відсоток не рятує, якщо монета просяде в ціні.\n"
    "Для КОЖНОЇ монети поверни:\n"
    "- coin: тікер, як його дали;\n"
    "- summary: ОДНЕ речення (до 110 знаків) простою мовою — що це за монета і чи вона "
    "давня й велика, чи свіжа й дрібна. Спирайся на цифри, які дали, і назви їх. "
    "НЕ починай речення з тікера — він і так буде поруч. age_years_at_least означає "
    "«точна дата народження невідома, але монета торгується щонайменше стільки років»: "
    "так і пиши, а не «вік не вказано»;\n"
    "- trust: 'solid' — давня велика монета, яку знають усі; 'ok' — існує кілька років і "
    "помітна, але не з перших; 'shaky' — молода, дрібна або з малим оборотом.\n"
    "ЖОРСТКО: жодних прогнозів ціни, жодних порад купувати. Тільки те, що видно з цифр. "
    "Чого в цифрах немає — не вигадуй. Пиши українською, звертайся на «ти», без жаргону."
)

COIN_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "coin": {"type": "string"},
                    "summary": {"type": "string"},
                    "trust": {"type": "string", "enum": ["solid", "ok", "shaky"]},
                },
                "required": ["coin", "summary", "trust"],
            },
        }
    },
    "required": ["items"],
}


async def explain_coins(profiles: Dict[str, Dict]) -> Dict[str, Dict]:
    """{тікер: факти} -> {тікер: {summary, trust}}. {} якщо AI недоступний."""
    if not config.gemini_api_key or not profiles:
        return {}
    listing = json.dumps(list(profiles.values()), ensure_ascii=False)
    payload = {
        "system_instruction": {"parts": [{"text": COIN_SYSTEM}]},
        "contents": [{"parts": [{"text": f"Оціни надійність цих монет:\n{listing}"}]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 4096,
            "responseMimeType": "application/json",
            "responseSchema": COIN_SCHEMA,
        },
    }
    for model in _models():
        try:
            async with httpx.AsyncClient(timeout=AI_TIMEOUT) as cl:
                r = await cl.post(API.format(model=model), headers=_auth(), json=payload)
                r.raise_for_status()
                data = r.json()
            items = json.loads(data["candidates"][0]["content"]["parts"][0]["text"])["items"]
            _note_success(model)
            return {str(it.get("coin", "")).upper(): it for it in items if it.get("coin")}
        except Exception as e:  # noqa: BLE001
            log.warning("Оцінка монет (%s) недоступна: %s", model, e)
            if _retry_pointless(e):
                continue
    return {}


# --- розбір вкладу, записаного вільним текстом ---

DEPOSIT_SYSTEM = (
    "Ти розбираєш записи про крипто-вклади, які людина пише вільним текстом українською "
    "або впереміш з англійською. Один рядок — один вклад, рядків може бути кілька.\n"
    "Приклади рядків: «200 usdt bybit savings 6.77», «0.01 btc okx simple earn 0.5 на 45 днів», "
    "«поклала 200 usdt bybit savings 6.77% на 30 днів».\n"
    "Для КОЖНОГО вкладу поверни:\n"
    "- amount: скільки МОНЕТИ поклала, числом (200, 0.01);\n"
    "- coin: тікер монети великими літерами (USDT, BTC);\n"
    "- exchange: 'okx' або 'bybit'. Іншої біржі тут не буває; не видно — порожньо;\n"
    "- product: назва продукту біржі, як у застосунку ('Savings', 'On-Chain Earn', "
    "'Simple Earn', 'ETH Staking'). Не видно — порожньо;\n"
    "- apr_pct: річна ставка у відсотках числом (6.77% -> 6.77; просто 6.77 теж -> 6.77). "
    "Не названа — null;\n"
    "- term_days: на скільки днів гроші замкнені («на 45 днів» -> 45). Немає — null;\n"
    "- unlock_date: дата розморозки YYYY-MM-DD, якщо названа саме дата («до 20.10» -> "
    "найближче 20 жовтня). Немає — null;\n"
    "- kind: 'fixed', якщо є строк або дата розморозки; 'flex', якщо забрати можна будь-коли.\n"
    "ЖОРСТКО: нічого не додумуй. Ставку, строк і біржу бери ЛИШЕ з тексту. Чого немає — "
    "null або порожньо: це нормальна відповідь, а вигадана цифра потрапить у журнал і "
    "потім видаватиме себе за правду."
)

DEPOSIT_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "amount": {"type": "number"},
                    "coin": {"type": "string"},
                    "exchange": {"type": "string"},
                    "product": {"type": "string"},
                    "apr_pct": {"type": "number", "nullable": True},
                    "term_days": {"type": "number", "nullable": True},
                    "unlock_date": {"type": "string", "nullable": True},
                    "kind": {"type": "string", "enum": ["flex", "fixed"]},
                },
                "required": ["amount", "coin", "kind"],
            },
        }
    },
    "required": ["items"],
}


async def parse_deposits(text: str, today: Optional[str] = None) -> Optional[List[Dict]]:
    """Вільний текст -> список вкладів. None, якщо порадник недоступний.

    Саме None, а не []: порожній список означав би «нічого не знайшла», і бот
    мовчки викинув би запис замість того, щоб розібрати його формулою.
    """
    if not config.gemini_api_key or not (text or "").strip():
        return None
    ask = f"Сьогодні {today}.\n" if today else ""
    payload = {
        "system_instruction": {"parts": [{"text": DEPOSIT_SYSTEM}]},
        "contents": [{"parts": [{"text": f"{ask}Розбери ці записи:\n{text}"}]}],
        "generationConfig": {
            "temperature": 0.0,
            "maxOutputTokens": 4096,
            "responseMimeType": "application/json",
            "responseSchema": DEPOSIT_SCHEMA,
        },
    }
    for model in _models():
        try:
            async with httpx.AsyncClient(timeout=AI_TIMEOUT) as cl:
                r = await cl.post(API.format(model=model), headers=_auth(), json=payload)
                r.raise_for_status()
                data = r.json()
            items = json.loads(data["candidates"][0]["content"]["parts"][0]["text"])["items"]
            _note_success(model)
            return [it for it in items if isinstance(it, dict)]
        except Exception as e:  # noqa: BLE001
            log.warning("Розбір вкладу (%s) недоступний: %s", model, e)
            if _retry_pointless(e):
                continue
    _note_failure()
    return None
