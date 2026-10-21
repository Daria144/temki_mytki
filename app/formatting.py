"""Форматування повідомлень: мінімум тексту, без жаргону."""
import datetime
import html
import re

from app.earn import (
    ALT_DRAWDOWN,
    ALT_TOPUP_MAX,
    RISK_LOW,
    Offer,
    Promo,
    core_per_coin,
    ladder,
    locked_options,
    optimize,
    promo_expired,
    rate_steps,
    risky_coins,
    smart_amounts,
)
from app import income
from app.config import config
from app.engine import Recommendation
from app.journal import active_totals, is_stable, missing_fields


def fmt_price(p: float) -> str:
    if p is None:
        return "?"
    if p >= 100:
        s = f"{p:,.0f}"
    elif p >= 1:
        s = f"{p:,.2f}"
    else:
        s = f"{p:.6f}"
    return s.replace(",", " ")


def fmt_price_raw(p: float) -> str:
    """Те саме число, що й fmt_price, але без пробілу-роздільника тисяч —

    щоб можна було скопіювати й вставити прямо в поле ціни лімітного ордера
    на біржі (там пробіл усередині числа ламає введення).
    """
    if p is None:
        return "?"
    if p >= 100:
        return f"{p:.0f}"
    if p >= 1:
        return f"{p:.2f}"
    return f"{p:.6f}"


def fmt_qty(coin: str, q: float) -> str:
    decimals = 6 if coin == "BTC" else 4
    return f"{q:.{decimals}f}"


def fmt_usd(v: float) -> str:
    return f"${v:,.0f}".replace(",", " ")


def fmt_usd_raw(v: float) -> str:
    """Та сама сума, що й fmt_usd, але без «$» і без пробілу-роздільника —

    чисте число для поля суми лімітного ордера.
    """
    return f"{v:.0f}"


def fmt_money(v: float) -> str:
    """Дрібні суми (дохід за місяць) — з копійками, великі — без."""
    if abs(v) >= 100:
        return fmt_usd(v)
    return f"${v:,.2f}".replace(",", " ")


# --- євро (частина про ETF на IBKR; крипта живе в доларах) ---

def fmt_eur(v: float) -> str:
    """Круглі суми внесків: €200."""
    if v is None:
        return "?"
    return f"€{v:,.0f}".replace(",", " ")


def fmt_eur_money(v: float) -> str:
    """Як fmt_money, але в євро: дрібне — з центами, велике — без."""
    if v is None:
        return "?"
    if abs(v) >= 100:
        return fmt_eur(v)
    return f"€{v:,.2f}".replace(",", " ")


def fmt_eur_price(v: float) -> str:
    """Ціна паю — завжди з центами, бо на ній рахується кількість паїв."""
    if v is None:
        return "?"
    return f"€{v:,.2f}".replace(",", " ")


def fmt_units(q: float) -> str:
    """Кількість паїв: цілі — без хвоста, дробові — без зайвих нулів."""
    if q is None:
        return "?"
    return f"{q:g}"


def _place(o: Offer) -> str:
    """Де саме лежатимуть гроші: біржа, назва продукту й монета.

    Без назви продукту «OKX USDT» нічого не каже — на біржі таких розділів кілька.
    """
    return f"{o.label} · {o.coin}"


def _hold_note(o: Offer) -> str:
    """Коли можна забрати. Для OKX публічний API терміну не дає — кажемо прямо."""
    if not o.flexible and o.redeem_days <= 0:
        return "термін виводу уточни на біржі"
    if o.redeem_days >= 1:
        return f"вивід ~{o.redeem_days:.0f} дн"
    return "будь-коли"


def _stables_block(offers: list[Offer], free_usd: float | None) -> list[str]:
    """Сходинки сум: де ставка ще висока, а де докладати вже немає сенсу."""
    stables = [o for o in offers if o.risk == RISK_LOW and o.instant]
    if not stables:
        return []

    out = ["💵 ВІЛЬНІ ДОЛАРИ", "Сума не змінюється, забрати можна будь-коли.", ""]

    amounts = smart_amounts(stables, extra=(100.0, 1000.0), limit=5)
    if free_usd and free_usd not in amounts:
        amounts = sorted(set(amounts) | {float(free_usd)})[:6]
    rows = ladder(stables, amounts, max_places=2)

    out.append("Скільки дає яка сума:")
    for r in rows:
        mark = "  ← твоя сума" if free_usd and abs(r["amount"] - free_usd) < 0.01 else ""
        out.append(
            f"• {fmt_usd(r['amount'])} → {fmt_money(r['income'])}/рік "
            f"({r['effective_apr'] * 100:.2f}%){mark}"
        )

    # Де саме закінчується вигідна ставка: показуємо діапазони, а не суму «від нуля».
    steps = []
    cum = 0.0
    for apr, cap, offer in rate_steps(stables):
        if cap == float("inf"):
            steps.append(f"• понад {fmt_usd(cum)} — {apr * 100:.2f}% ({_place(offer)})")
            break
        label = "перші" if cum == 0 else "наступні"
        steps.append(
            f"• {label} {fmt_usd(cap)} — {apr * 100:.2f}% ({_place(offer)})"
        )
        cum += cap
    if steps:
        out += ["", "Висока ставка діє лише на перші суми:", *steps[:3]]

    if free_usd:
        res = optimize(stables, free_usd, max_places=2)
        if res["parts"]:
            out += ["", f"Твої {fmt_usd(free_usd)} — розклади так:"]
            for p in res["parts"]:
                o = p["offer"]
                out.append(
                    f"• {_place(o)} — {fmt_usd(p['amount'])} "
                    f"→ {fmt_money(p['income'])}/рік"
                )
                # Саме тут людина йде щось робити — підказуємо, де це в застосунку.
                if o.section:
                    out.append(f"   шукати: {o.section}")
            out.append(
                f"Разом {fmt_money(res['income'])}/рік "
                f"({fmt_money(res['income'] / 12)}/міс)"
            )
    else:
        out += ["", "Скажи суму — розкладу конкретно: /earn 500"]
    out.append("")
    return out


def _lock_block(offers: list[Offer], free_usd: float | None,
                reserved_usd: float = 0.0) -> list[str]:
    """Чи варто замкнути частину доларів заради вищої ставки.

    Блокувати можна, але лише коли виграш проти вільного варіанта справді
    помітний у грошах. І ніколи — гроші, які вже чекають на просадку крипти:
    заявка спрацює саме тоді, коли вони будуть замкнені.
    """
    usable = max(float(free_usd or 0.0) - max(float(reserved_usd or 0.0), 0.0), 0.0)
    if usable <= 0:
        return []
    res = locked_options(offers, usable)
    if not res:
        return []

    locked, free = res["locked"], res["free"]
    out = [
        "🔒 МОЖНА ЗАМКНУТИ Й ОТРИМАТИ БІЛЬШЕ",
        f"• {_place(locked)} — {locked.apr * 100:.2f}%, "
        f"{fmt_money(res['locked_year'])}/рік на {fmt_usd(usable)}",
        f"• вільний варіант {_place(free)} — {free.apr * 100:.2f}%, "
        f"{fmt_money(res['free_year'])}/рік",
        f"Різниця ~{fmt_money(res['gain_year'])}/рік. "
        f"Забрати одразу не вийде: вивід іде ~{res['days']:.0f} "
        f"{days_word(int(res['days']))}.",
    ]
    if reserved_usd and reserved_usd > 0:
        out.append(
            f"⚠️ {fmt_usd(reserved_usd)} я сюди не рахую — ці гроші чекають на "
            "просадку у виставлених заявках, замикати їх не можна."
        )
    out.append("")
    return out


def _coins_block(offers: list[Offer], holdings: dict, prices: dict) -> list[str]:
    """BTC/ETH — коротко й чесно, усе в доларах."""
    lines, total = [], 0.0
    for coin in ("BTC", "ETH"):
        pos = holdings.get(coin) or {}
        value = pos.get("value")
        if not value:
            continue
        variants = core_per_coin(offers, coin)
        usable = [o for o in variants
                  if not (o.min_amount and prices.get(coin)
                          and o.min_amount * prices[coin] > value)]
        if usable:
            best = usable[0]
            total += best.per_year(value)
            lines.append(
                f"• {coin} {fmt_usd(value)} → максимум {fmt_money(best.per_year(value))}/рік "
                f"({_place(best)} {best.apr * 100:.2f}%, {_hold_note(best)})"
            )
        else:
            lines.append(f"• {coin} {fmt_usd(value)} → доступних варіантів немає")

        # Чого не хапає до НАЙБЛИЖЧОЇ недоступної пропозиції — щоб мета була реальна.
        blocked = [
            o for o in variants
            if o not in usable and o.min_amount and prices.get(coin)
            and o.apr > (usable[0].apr if usable else 0)
        ]
        if blocked:
            top = min(blocked, key=lambda o: o.min_amount * prices[coin])
            need = top.min_amount * prices[coin] - value
            if need > 3 * value:
                # Пропонувати докласти втричі більше заради копійок — безглуздо.
                lines.append(
                    f"   {_place(top)} дає {top.apr * 100:.2f}%, але туди треба ще "
                    f"~{fmt_usd(need)} — не варте того"
                )
            else:
                lines.append(
                    f"   Щоб відкрити {_place(top)} {top.apr * 100:.2f}% — "
                    f"треба ще ~{fmt_usd(need)}"
                )

    if not lines:
        return []
    return [
        "🟡 ТВОЇ МОНЕТИ",
        *lines,
        f"Разом з монет — {fmt_money(total)}/рік. Заради цього не варто нести "
        "монети на біржу: у своєму гаманці вони безпечніші.",
        "",
    ]


# Вердикт іде ПЕРШИМ знаком у рядку: з телефона видно з одного погляду, чи це
# взагалі про неї, і лише потім — що за акція.
VERDICT_MARK = {"worth": "✅", "maybe": "🤔", "skip": "❌"}


def _num(value) -> float:
    try:
        return float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def fmt_amount(value: float) -> str:
    """Ліміти сум бувають і 0.001, і 1000 — показуємо без зайвих нулів."""
    return f"{value:g}"


def days_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "день"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "дні"
    return "днів"


def _short_date(iso) -> str:
    """2026-09-14 -> 14.09. ISO у тексті для людини виглядає як помилка бота."""
    try:
        return datetime.date.fromisoformat(str(iso)[:10]).strftime("%d.%m")
    except (TypeError, ValueError):
        return ""


def _bonus_line(ex: dict) -> str:
    """Скільки саме дають. Вона просила цифру — навіть коли відповідь «не дають»."""
    bonus, what = _num(ex.get("bonus_usd")), (ex.get("bonus_what") or "").strip()
    if bonus > 0:
        return f"Бонус — ${bonus:g}" + (f" {what}." if what else ".")
    if what:
        return f"Бонус — {what}."
    if ex.get("kind") == "trading":
        return "Розмір бонусу в оголошенні не вказаний."
    return ""


def _terms_line(ex: dict) -> str:
    """Ліміти суми, блокування, дедлайн і попередження — одним компактним рядком."""
    parts = []
    lo, hi, coin = _num(ex.get("min_amount")), _num(ex.get("max_amount")), (ex.get("coin") or "")
    if lo > 0 and hi > 0:
        parts.append(f"Можна від {fmt_amount(lo)} до {fmt_amount(hi)} {coin}".strip() + ".")
    elif hi > 0:
        parts.append(f"Максимум {fmt_amount(hi)} {coin}".strip() + ".")
    elif lo > 0:
        parts.append(f"Мінімум {fmt_amount(lo)} {coin}".strip() + ".")

    locked = int(_num(ex.get("locked_days")))
    if locked > 0:
        parts.append(f"⚠️ Гроші блокуються на {locked} {days_word(locked)}.")
    deadline = _short_date(ex.get("period_end"))
    if deadline:
        parts.append(f"Діє до {deadline}.")
    if ex.get("warn"):
        parts.append(f"⚠️ {ex['warn'].strip().rstrip('.')}.")
    return " ".join(parts)


# Мітки копіювального блоку. Телеграм робить кнопку «копіювати» лише для <pre>,
# а вмикати HTML на весь текст небезпечно: у заголовках бірж трапляється «&»
# і «<», і повідомлення просто не відправилось би. Тому в тексті лишаються
# мітки, і аж перед надсиланням to_html() екранує все, крім них.
QUOTE_OPEN = "\x01"
QUOTE_CLOSE = "\x02"

# Та ж ідея, але для короткого "<code>" inline — ціна й сума ордера посеред
# рядка. Тап по такому числу в Телеграмі копіює саме число, без «$» і без
# пробілу-роздільника тисяч, тож його можна одразу вставити в поле лімітного
# ордера на біржі.
CODE_OPEN = "\x03"
CODE_CLOSE = "\x04"


def to_html(text: str) -> str:
    """Готує готовий текст до надсилання з parse_mode='HTML'.

    Спершу екрануємо ВСЕ, і лише потім наші власні мітки стають тегами —
    інакше будь-який «<» з чужого заголовка ламав би розмітку.
    """
    out = html.escape(text or "")
    out = out.replace(QUOTE_OPEN, "<pre>").replace(QUOTE_CLOSE, "</pre>")
    return out.replace(CODE_OPEN, "<code>").replace(CODE_CLOSE, "</code>")


def has_quote(text: str) -> bool:
    """Чи є в тексті копіювальна мітка — тоді й лише тоді вмикаємо parse_mode='HTML'."""
    return QUOTE_OPEN in (text or "") or CODE_OPEN in (text or "")


# Межа Telegram — 4096 знаків, лишаємо запас на теги <pre> після екранування.
TG_LIMIT = 3500


def split_message(text: str, limit: int = TG_LIMIT) -> list[str]:
    """Ріже довгий текст на повідомлення, не розриваючи копіювальний блок.

    Блоки з питаннями до підтримки додали до /earn майже дві тисячі знаків, і
    одне повідомлення почало впиратись у межу Telegram. Втратити ВСЮ відповідь
    через кілька зайвих рядків — найгірший із можливих варіантів.
    """
    if len(text or "") <= limit:
        return [text]
    parts: list[str] = []
    cur = ""
    for para in text.split("\n\n"):
        candidate = f"{cur}\n\n{para}" if cur else para
        # Різати можна лише там, де копіювальний блок уже закритий — інакше
        # половина тегу поїде в інше повідомлення й розмітка розсиплеться.
        closed = cur.count(QUOTE_OPEN) == cur.count(QUOTE_CLOSE)
        if len(candidate) > limit and cur and closed:
            parts.append(cur)
            cur = para
        else:
            cur = candidate
    if cur:
        parts.append(cur)
    return parts


def promo_quote(promo: Promo, ex: dict | None = None) -> str:
    """Копіювальний блок: умови акції англійською + готові питання до підтримки.

    Свідомо БЕЗ її сум: цей текст пишеться для того, щоб просто скопіювати і
    вставити в чат підтримки біржі, а скільки в неї грошей — не їхня справа.
    Мова англійська, бо підтримка OKX і Bybit українською не відповідає.
    """
    ex = ex or {}
    lines = [f"Promo: {promo.title}", f"Exchange: {promo.exchange}"]
    coin = str(ex.get("coin") or "").strip().upper()
    if coin:
        lines.append(f"Coin: {coin}")
    apr = _num(ex.get("apr_pct"))
    if apr > 0:
        lines.append(f"Advertised rate: {apr:g}% APR")
    locked = int(_num(ex.get("locked_days")))
    if locked > 0:
        lines.append(f"Stated lock-up: {locked} days")
    end = str(ex.get("period_end") or "")[:10]
    if end:
        lines.append(f"Stated end date: {end}")
    lines += [
        "",
        "Questions to support:",
        "1. Is the advertised rate fixed for the whole period, or floating?",
        "2. What is the maximum amount that earns the advertised rate?",
        "3. Is there a lock-up period, and how many days does redemption take?",
        "4. Does the product auto-renew at the end of the term, and at what rate?",
        "5. Is this promotion available to a personal account registered in Hungary?",
    ]
    return QUOTE_OPEN + "\n".join(lines) + QUOTE_CLOSE


def promo_lines(promos: list[Promo], explains: dict | None = None,
                today=None) -> list[str]:
    """Акції з вердиктом — один вигляд і для /earn, і для сповіщень.

    Вигідні показуємо стисло (два-три рядки), а невигідні — навпаки, повністю
    з цифрами: вона хоче бачити, ЧОМУ бот каже «ні», і вирішувати сама.
    Протерміновані не показуємо ніде: радити те, чого вже нема, — гірше за мовчання.
    """
    explains = explains or {}
    entries, by_rule = [], False
    for p in promos:
        ex = explains.get(p.title) or {}
        if promo_expired(ex, today):
            continue
        by_rule = by_rule or bool(ex.get("by_rule"))
        verdict = ex.get("verdict") if ex.get("verdict") in VERDICT_MARK else "maybe"
        part = [f"{VERDICT_MARK[verdict]} {p.exchange}: «{ex.get('title_uk') or p.title}»"]

        if verdict == "skip":
            # Повний розбір: скільки дають, що за це треба і чому все одно не варто.
            for line in (_bonus_line(ex), _requirements_line(ex),
                         f"Не варто: {_lower_first(ex['why'])}" if ex.get("why") else ""):
                if line:
                    part.append(f"   {line}")
        else:
            # Стисло: головна цифра, потім умови одним рядком.
            head = ex.get("gain_line") or _bonus_line(ex) or ex.get("why") or ex.get("what")
            if head:
                part.append(f"   {head}")
            terms = _terms_line(ex)
            if terms:
                part.append(f"   {terms}")
            elif ex.get("action"):
                part.append(f"   Перший крок: {ex['action']}")
        # Посилання даємо навіть під «не варто»: вона має змогу перевірити цифру
        # сама, а не вірити боту на слово. Прев'ю вимкнене там, де надсилаємо.
        if p.url:
            part.append(f"   🔗 {p.url}")
        # Готовий текст для підтримки біржі: умови англійською й питання, які
        # варто поставити. Копіюється одним дотиком, суми туди не потрапляють.
        part.append("   Спитати біржу (натисни, щоб скопіювати):")
        part.append(promo_quote(p, ex))
        entries.append("\n".join(part))

    # Один елемент = одна акція. Порожні рядки між ними додає той, хто складає
    # повідомлення: інакше вони подвоювались і текст розповзався по екрану.
    # Той самий підпис, що й під формульними порадами: інакше сухе правило не
    # відрізнити від справжнього вердикту порадника.
    if entries and by_rule and config.gemini_api_key:
        entries.append(NO_ADVISOR_NOTE)
    return entries


def _lower_first(text: str) -> str:
    """«За це треба Зареєструватися» виглядає як помилка. Латиницю не чіпаємо —
    там назви бірж і розділів застосунку («Bybit → Earn»)."""
    if text and "\u0400" <= text[0] <= "\u04ff":
        return text[0].lower() + text[1:]
    return text


def _requirements_line(ex: dict) -> str:
    req = (ex.get("requirements") or "").strip()
    return f"За це треба {_lower_first(req).rstrip('.')}." if req else ""


TRUST_MARK = {"solid": "🟢", "ok": "🟡", "shaky": "🔴", "unknown": "❔"}

# Тікери на CoinGecko повторюються десятками, і раніше бот брав перший-ліпший
# збіг: у смоуку монета «CP» отримала 8783-тє місце й оборот $1.82 за добу —
# тобто чесні цифри про зовсім іншу монету. Мовчання тут корисніше за впевненість.
UNKNOWN_COIN_NOTE = (
    "не змогла надійно визначити цю монету — під таким тікером на біржах "
    "і в довідниках висить кілька різних. Цифри показувати не буду, щоб не "
    "розповісти тобі про чужу монету."
)
# Той самий висновок, але з іншої причини: довідник просто не відповів. Плутати
# ці два випадки не варто — у першому монета сумнівна, у другому винен сайт.
UNCHECKED_COIN_NOTE = (
    "не змогла перевірити цю монету — довідник ринку зараз не відповідає. "
    "Краще нічого не казати, ніж вгадувати: під одним тікером буває кілька "
    "різних монет. Спробую пізніше."
)


def coin_trust_lines(reliability: dict, money_usd: float = 0.0,
                     rates: dict | None = None) -> list[str]:
    """Наскільки надійна САМА монета — і скільки на ній можна заробити проти втратити.

    Для незнайомої монети це головне: відсоток рахується від суми, а падіння ціни —
    теж, тільки в інший бік і зазвичай більше.
    """
    lines = []
    for coin, info in reliability.items():
        if info.get("ambiguous"):
            # Ні заробітку, ні втрати тут рахувати нема від чого: невідомо навіть,
            # про яку монету йдеться.
            lines.append(f"❔ {coin} — {info.get('summary') or UNKNOWN_COIN_NOTE}")
            continue
        mark = TRUST_MARK.get(info.get("trust"), "🟡")
        # Порадник любить починати речення з тікера, а він уже є в рядку —
        # виходило «CP — CP — це дрібна монета».
        summary = re.sub(rf"^{re.escape(coin)}\s*[—–-]\s*", "", info.get("summary", "").strip())
        part = [f"{mark} {coin} — {summary}"]
        apr = (rates or {}).get(coin)
        apr = apr[0] if isinstance(apr, (tuple, list)) else apr
        if money_usd > 0:
            # Головне порівняння: скільки дасть відсоток проти того, скільки
            # забере падіння самої монети. Ставку знаємо не завжди, а ризик — завжди.
            gain = (f"На {fmt_usd(money_usd)} під {apr * 100:.2f}% це ~{fmt_money(money_usd * apr)} "
                    f"за рік, а якщо" if apr else f"Якщо вкласти сюди {fmt_usd(money_usd)} і")
            part.append(
                f"   {gain} монета впаде на {int(ALT_DRAWDOWN * 100)}% — це мінус "
                f"{fmt_money(money_usd * ALT_DRAWDOWN)}."
            )
        lines.append("\n".join(part))
    if lines and any(i.get("by_rule") for i in reliability.values()) and config.gemini_api_key:
        lines.append(NO_ADVISOR_NOTE)
    return lines


def format_earn(
    offers: list[Offer],
    promos: list[Promo],
    holdings: dict | None = None,
    free_usd: float | None = None,
    explains: dict | None = None,
    prices: dict | None = None,
    reserved_usd: float = 0.0,
) -> str:
    """Куди покласти гроші. Вигода рахується окремо по кожній монеті,
    а по доларах — сходинками, щоб було видно межу вигоди.
    """
    explains = explains or {}
    out = ["💰 Куди покласти гроші — станом на зараз", ""]
    out += _stables_block(offers, free_usd)
    out += _lock_block(offers, free_usd, reserved_usd)
    out += _coins_block(offers, holdings or {}, prices or {})

    risky = risky_coins(offers)
    if risky:
        out += [
            "🔴 РИЗИКОВАНО — незнайомі монети",
            "Великий відсоток саме тому, що монета може обвалитись.",
            "Такі ставки живуть кілька днів, «на рік» їх рахувати немає сенсу.",
            "• " + " · ".join(f"{o.coin} {o.apr * 100:.0f}%" for o in risky),
        ]
        if free_usd:
            out.append(
                f"Якщо вкласти туди {fmt_usd(free_usd)} і монета впаде на "
                f"{int(ALT_DRAWDOWN * 100)}% — втратиш ~{fmt_money(free_usd * ALT_DRAWDOWN)}."
            )
        out.append("")

    # Не пропозиція про дохід (новина, партнерство, оновлення) — не показуємо:
    # рішення приймає порадник по тексту, а не слова в заголовку.
    good = [p for p in promos if p.candidate][:4]
    good = [p for p in good if (explains or {}).get(p.title, {}).get("is_offer") is not False]
    lines = promo_lines(good, explains)
    if lines:
        out += ["🎁 АКЦІЇ ЗАРАЗ", ""]
        for entry in lines:
            out += [entry, ""]

    out.append("⚠️ Не фінансова порада.")
    return "\n".join(out).strip()


# Підпис під рішенням, яке ухвалила формула, а не порадник. Без нього формульні
# поради не відрізнити від справжніх: вони приходять узагалі без пояснення.
NO_ADVISOR_NOTE = (
    "ℹ️ Порадник зараз недоступний — це рішення я склала сама, за загальним правилом."
)


# --- журнал earn-вкладів: те, чого ключі не показують ---

def deposits_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "вклад"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "вклади"
    return "вкладів"


def fmt_coin_amount(coin: str, amount: float, ticker: bool = True) -> str:
    """Стейбли — це просто долари, решта — кількість монети без зайвих нулів."""
    tail = f" {coin.upper()}" if ticker else ""
    if is_stable(coin):
        return f"{fmt_money(amount)}{tail}"
    return f"{amount:g}{tail}"


def deposit_line(dep: dict) -> str:
    """Один вклад так, як вона його впізнає: сума, де лежить, під скільки, доки."""
    parts = [fmt_coin_amount(dep.get("coin", ""), float(dep.get("amount") or 0))]
    place = " ".join(x for x in (dep.get("exchange"), dep.get("product")) if x)
    if place:
        parts.append(place)
    apr = dep.get("apr")
    if apr is not None:
        parts.append(f"{apr * 100:.2f}%")
    unlock = _short_date(dep.get("unlock_date"))
    parts.append(f"замкнено до {unlock}" if unlock else "вивід вільний")
    line = f"• {parts[0]} — " + ", ".join(parts[1:])

    gaps = missing_fields(dep)
    if gaps:
        line += f"\n   ⚠️ не зрозуміла {', '.join(gaps)} — допиши, якщо це важливо"
    return line


# --- скільки earn-вклади вже заробили (облік у app/income.py) ---

# Проміжки так, як вона просила їх бачити.
WINDOW_TITLES = {"all": "за весь час", "d30": "за 30 днів", "d7": "за 7 днів"}

# Що писати замість цифри, поки знімків мало. Ні «$0», ні прочерк: округлений
# до нуля заробіток і «я ще не знаю» вона прочитає однаково, а це різні речі.
COLLECTING = "дані збираються, покажу через кілька днів"

INCOME_HEAD = "💰 Скільки твої вклади вже заробили"
INCOME_INTRO = ("Це не сума вкладу, а те, що вона принесла. Раз на добу я знімаю "
                "цифри з бірж і складаю з них історію.")


def fmt_earned(v: float) -> str:
    """Заробіток: центи тут головні, бо за тиждень набігає кілька центів."""
    if v <= 0:
        return "$0"
    if v < 0.01:
        return "менше за $0.01"
    if v < 100:
        return f"${v:,.2f}".replace(",", " ")
    return fmt_usd(v)


def _source_note(measured: bool) -> str:
    """Звідки цифра. Точне й приблизне в одному рядку без позначки не буває."""
    return "від біржі" if measured else "порахувала я, приблизно"


def income_period_text(p, known_price: bool = True) -> str:
    """Заробіток за один проміжок — і одразу те, чого в цій цифрі не хватає."""
    if not p.enough:
        return COLLECTING
    if not known_price:
        return f"{p.earned:g} монети — ціни зараз не знаю, у долари не переведу"
    notes = []
    if p.partial and p.days:
        notes.append(f"але знімки маю лише за {p.days} {days_word(p.days)}")
    if p.gaps:
        notes.append(f"{p.gaps} {days_word(p.gaps)} біржа мовчала, цих діб у ряді немає")
    if p.resets:
        notes.append("позицію за цей час перезаклали, відлік біржі почався з нуля")
    tail = ", " + "; ".join(notes) if notes else ""
    return f"{fmt_earned(p.usd)} ({_source_note(p.measured)}){tail}"


def income_rate_lines(row: dict) -> list[str]:
    """Фактична ставка проти обіцяної — найцінніша пара цифр усього обліку."""
    promised = row.get("promised_apr")
    shown = f"{promised * 100:.2f}%" if promised is not None else ""
    gap = income.rate_gap(row, income.best_window(row))
    if gap is None:
        if promised is None:
            return ["ставки біржа тут не показує, а порахувати її поки нема з чого"]
        return [f"обіцяють {shown} на рік (цифра від біржі); скільки платять "
                f"насправді — порахую, коли зберу знімки хоча б за "
                f"{income.MIN_SPAN_DAYS} доби підряд"]

    days = int(gap["days"])
    doubt = "" if gap["measured"] else ", і сам заробіток тут приблизний"
    out = [f"платять насправді {gap['actual'] * 100:.2f}% на рік — це порахувала я "
           f"з заробітку за {days} {days_word(days)}{doubt}"]
    if gap["promised"] is None:
        out.append("обіцяної ставки біржа тут не показує, тому порівняти нема з чим")
    elif gap["worse"]:
        out.append(f"а обіцяють {shown} — це на {abs(gap['diff']) * 100:.2f}% на рік "
                   "МЕНШЕ, ніж на вітрині")
    elif gap["better"]:
        out.append(f"а обіцяли {shown} — виходить навіть більше, "
                   f"на {gap['diff'] * 100:.2f}% на рік")
    else:
        out.append(f"обіцяли {shown} — сходиться")
    return out


def income_position_lines(row: dict) -> list[str]:
    """Одна earn-позиція: скільки вже принесла і за якою ставкою насправді."""
    place = " · ".join(x for x in (row["exchange"], row["coin"], row["product"]) if x)
    amount = (fmt_money(row["amount_usd"]) if row["known_price"]
              else fmt_coin_amount(row["coin"], row["amount"]))
    out = [f"• {place} — працює {amount}"]
    out += [f"   {WINDOW_TITLES[name]}: "
            f"{income_period_text(row['periods'][name], row['known_price'])}"
            for name in ("all", "d30", "d7")]
    return out + [f"   {line}" for line in income_rate_lines(row)]


def income_total_lines(data: dict) -> list[str]:
    """Разом по всіх позиціях — із приписками, наскільки ця сума повна."""
    totals = data.get("totals") or {}
    parts = []
    for name in ("all", "d30", "d7"):
        t = totals.get(name) or {}
        if not t.get("enough"):
            parts.append(f"{WINDOW_TITLES[name]} — {COLLECTING}")
            continue
        note = ", частину порахувала я" if t.get("estimated") else ""
        if t.get("incomplete"):
            note += ", і не всі позиції ще набрали знімків"
        parts.append(f"{WINDOW_TITLES[name]} {fmt_earned(t.get('usd') or 0.0)}{note}")
    out = ["Разом: " + "; ".join(parts) + "."]
    unpriced = sorted({c for t in totals.values() for c in (t.get("unpriced") or [])})
    if unpriced:
        out.append("У суму не входить заробіток по " + ", ".join(unpriced)
                   + ": ціни цих монет я зараз не знаю.")
    return out


def income_history_lines(data: dict, skips: dict | None = None) -> list[str]:
    """Скільки взагалі є історії — щоб було видно, наскільки цифрам можна вірити."""
    days = int(data.get("days") or 0)
    first, last = _short_date(data.get("first_day")), _short_date(data.get("last_day"))
    span = f" (з {first} до {last})" if first and last and first != last else \
           (f" ({first})" if first else "")
    out = [f"Знімків заробітку в мене {days}{span}."]
    if days <= income.MIN_SPAN_DAYS:
        out.append(f"Щоб порахувати ставку, яку платять НАСПРАВДІ, треба хоча б "
                   f"{income.MIN_SPAN_DAYS} доби знімків підряд: на OKX твої долари "
                   "дають у позику за живою погодинною ставкою, і з двох знімків за "
                   "кілька годин вийшла б цифра зі стелі.")
    recent = sorted(skips or {})[-3:]
    if recent:
        out.append("Доби без знімка (джерело не відповіло): " + ", ".join(
            f"{_short_date(d)} — {', '.join(skips[d]) or 'причина невідома'}"
            for d in recent) + ".")
    return out


def income_block(data: dict | None, skips: dict | None = None) -> list[str]:
    """Увесь облік заробітку рядками. Порожній список — знімків ще немає взагалі."""
    rows = (data or {}).get("rows") or []
    if not rows:
        return []
    out = []
    for row in rows:
        out += income_position_lines(row)
    return out + income_total_lines(data) + income_history_lines(data, skips)


def deposit_income_line(dep: dict, rows: list | None) -> str:
    """Приписка до вкладу з журналу: що ці гроші приносять НАСПРАВДІ.

    Ставка в журналі — та, під яку вона заходила. Фактична рахується з її ж
    заробітку, і саме різниця між ними показує, чи вклад досі вартий грошей.
    """
    row = income.for_deposit(dep, rows)
    gap = income.rate_gap(row, income.best_window(row)) if row else None
    if gap is None:
        return ""
    entry = dep.get("apr")
    tail = ""
    if entry is not None and abs(gap["actual"] - float(entry)) >= income.RATE_GAP_PP:
        tail = f", а заходила ти під {float(entry) * 100:.2f}%"
    days = int(gap["days"])
    return (f"   фактично ці гроші приносять {gap['actual'] * 100:.2f}% на рік{tail}"
            f" — порахувала я з заробітку за {days} {days_word(days)}")


def journal_confirm_text(deposits: list[dict], prices: dict | None = None) -> str:
    """Що бот зрозумів із написаного — до того, як це осяде в журналі."""
    out = [f"Записала {len(deposits)} {deposits_word(len(deposits))}:"]
    out += [deposit_line(d) for d in deposits]

    t = active_totals(deposits, prices)
    money = []
    if t["usd"] > 0:
        money.append(fmt_money(t["usd"]))
    money += [f"{amount:g} {coin}" for coin, amount in sorted(t["coins"].items())]
    if money:
        tail = f"Разом працює {' і '.join(money)}"
        if t["income_year"] > 0:
            tail += f", приносить ~{fmt_money(t['income_year'])}/рік"
        out.append(tail)
    out.append("")
    out.append("Перевір, чи все правильно. Щось не так — тисни «Скасувати».")
    return "\n".join(out)


def journal_copy_block(deposits: list[dict], prices: dict | None = None) -> str:
    """Увесь журнал одним копіювальним блоком: натиснути — і список у буфері.

    Той самий підхід, що й для англійських умов акцій: вона хоче перекидати свої
    вклади в чат зі стороннім AI, не переписуючи їх руками. Тому тут сухий
    список без емодзі й без звертань до неї — це текст для копіювання.
    """
    if not deposits:
        return ""
    lines = ["Мої earn-вклади (умов із API бірж не видно, записані вручну):"]
    for dep in deposits:
        # Перший рядок deposit_line — сам вклад; далі йде підказка «допиши, чого
        # бракує», і в копію вона не потрібна.
        lines.append(deposit_line(dep).splitlines()[0])
    t = active_totals(deposits, prices)
    if t["income_year"] > 0:
        lines.append(f"Разом приносить ~{fmt_money(t['income_year'])} на рік.")
    return QUOTE_OPEN + "\n".join(lines) + QUOTE_CLOSE


def journal_list_text(deposits: list[dict], prices: dict | None = None,
                      income_data: dict | None = None,
                      skips: dict | None = None) -> str:
    """Журнал так, як він є зараз, плюс скільки ці гроші вже заробили.

    Облік заробітку журналу не потребує: він працює на самих ключах бірж. Тому
    на порожньому журналі заробіток усе одно видно — це найважливіша цифра тут.
    """
    earned = income_block(income_data, skips)
    if not deposits:
        out = [
            "📒 Журнал вкладів порожній.",
            "Тут я тримаю те, чого біржа не показує через ключі: під який відсоток "
            "ти заходила і до якої дати гроші замкнені.",
            "Натисни «➕ записати вклад» під /earn і напиши вільним текстом, "
            "наприклад: 200 usdt bybit savings 6.77",
        ]
        if earned:
            out += ["", INCOME_HEAD, INCOME_INTRO, ""] + earned
        return "\n".join(out)
    out = ["📒 Твої вклади за журналом:", ""]
    for dep in deposits:
        out.append(deposit_line(dep))
        actual = deposit_income_line(dep, (income_data or {}).get("rows"))
        if actual:
            out.append(actual)
    t = active_totals(deposits, prices)
    if t["income_year"] > 0:
        out += ["", f"Разом приносить ~{fmt_money(t['income_year'])}/рік."]
    out.append("")
    out.append("Головне — те, що показують ключі бірж; журнал лише додає строки й ставки.")
    if earned:
        out += ["", INCOME_HEAD, INCOME_INTRO, ""] + earned
    out.append("")
    out.append("Скопіювати весь список (натисни на нього):")
    out.append(journal_copy_block(deposits, prices))
    return "\n".join(out)


# Скільки днів виводу вже варті окремої згадки в нагадуванні.
SLOW_REDEEM_DAYS = 1


def journal_due_text(dep: dict, today: datetime.date,
                     best: Offer | None = None, redeem_days: float = 0.0) -> str:
    """Нагадування В ДЕНЬ розморозки — одразу з тим, куди перекласти.

    Головне тут — попередження про автопродовження: більшість продуктів
    самі себе продовжують, часто вже під гіршу ставку, і саме так гроші
    тихо переїжджають з 10% на 2%.
    """
    coin = str(dep.get("coin") or "").upper()
    place = " ".join(x for x in (dep.get("exchange"), dep.get("product")) if x)
    amount = fmt_coin_amount(coin, float(dep.get("amount") or 0))
    out = [f"⏰ {today.strftime('%d.%m')} закінчується твій вклад: {amount}"
           + (f" у {place}." if place else ".")]

    if redeem_days >= SLOW_REDEEM_DAYS:
        ready = today + datetime.timedelta(days=int(round(redeem_days)))
        out.append(
            f"Гроші будуть на місці аж {ready.strftime('%d.%m')} — вивід тут іде "
            f"~{int(round(redeem_days))} {days_word(int(round(redeem_days)))}, "
            "тому починати варто сьогодні."
        )
    out.append(
        "Якщо не забрати — багато продуктів автоматично продовжуються, "
        "часто під гіршу ставку."
    )
    if best is not None:
        note = "вільно" if best.instant else f"вивід ~{best.redeem_days:.0f} дн"
        out.append(f"Зараз найкраще по {coin}: {best.steps()[0].apr * 100:.2f}% "
                   f"{note} ({best.label}).")
        if best.section:
            out.append(f"шукати: {best.section}")
    out.append("")
    out.append("Скажи, що зробила — і я оновлю журнал.")
    return "\n".join(out)


def reconcile_lines(rows: list[dict]) -> list[str]:
    """Розбіжності журналу з реальними залишками. Правда завжди за ключами."""
    lines = []
    for r in rows:
        coin, exchange = r["coin"], r["exchange"]
        # Тикер уже стоїть на початку рядка — двічі його повторювати нема потреби.
        written = fmt_coin_amount(coin, r["written"], ticker=False)
        actual = fmt_coin_amount(coin, r["actual"], ticker=False)
        gap = fmt_money(r["diff_usd"])
        # Нуль краще назвати словами: «у журналі $0.00» читається як помилка.
        if r["written"] <= 0:
            head = (f"• {exchange} · {coin}: у журналі цієї монети немає, "
                    f"а в Earn на біржі лежить {actual}.")
        elif r["actual"] <= 0:
            head = (f"• {exchange} · {coin}: у журналі {written}, "
                    "а в Earn на біржі зараз нічого немає.")
        else:
            head = (f"• {exchange} · {coin}: у журналі {written}, "
                    f"а в Earn на біржі лежить {actual}.")
        part = [head]
        if r["diff"] > 0:
            part.append(f"   {gap} не враховані в журналі — я не знаю ні їхньої "
                        "ставки, ні строку, тому в підрахунок доходу вони не йдуть.")
        else:
            part.append(f"   На {gap} менше, ніж записано — схоже, ці гроші ти вже "
                        "забрала. Онови журнал, щоб цифри не брехали.")
        lines.append("\n".join(part))
    return lines


def rate_drop_lines(drops: list[dict], better: dict | None = None) -> list[str]:
    """Ставка по вже зробленому вкладу просіла — і скільки це грошей на рік."""
    better = better or {}
    lines = []
    for d in drops:
        dep = d["deposit"]
        place = " ".join(x for x in (dep.get("exchange"), dep.get("product")) if x)
        amount = fmt_coin_amount(d["coin"], float(dep.get("amount") or 0))
        part = [
            f"• {amount} у {place}: клала під {d['entry_apr'] * 100:.2f}%, "
            f"зараз {d['now_apr'] * 100:.2f}%.",
            f"   Це ~{fmt_money(d['now_year'])}/рік замість "
            f"~{fmt_money(d['was_year'])} — на {fmt_money(d['lost_year'])}/рік менше.",
        ]
        best = better.get(d["coin"])
        # Радити той самий продукт, у якому вона вже сидить, — це не порада.
        same_place = (best is not None and best.exchange == dep.get("exchange")
                      and best.product == dep.get("product"))
        if best is not None and not same_place and best.steps()[0].apr > d["now_apr"]:
            part.append(f"   Краще зараз: {best.label} — "
                        f"{best.steps()[0].apr * 100:.2f}%.")
        lines.append("\n".join(part))
    return lines


# --- дрібна позиція в чужому альткоїні: гроші беремо з залишку бюджету ---

def alt_coin_lines(coin: str, offer: Offer, position: dict, facts: dict | None = None,
                   freeing_soon: float = 0.0) -> list[str]:
    """Порада по монеті, якої в неї немає, — завжди з двома цифрами.

    Джерело грошей — незадіяний залишок місячного бюджету, і про це кажемо
    вголос: це та сама подушка, з якої піде докупівля на просадці крипти.
    """
    apr = offer.steps()[0].apr
    head = [f"🪙 {coin} — {apr * 100:.2f}% у {offer.label}, забрати можна будь-коли."]
    if facts and facts.get("rank"):
        age = facts.get("age_years") or facts.get("age_years_at_least")
        tail = f"№{facts['rank']} за розміром"
        if age:
            tail += f", на ринку понад {age:g} р"
        head.append(f"   Монета велика й давня: {tail}.")

    amount = position["amount"]
    if amount <= 0:
        # Суму, якої немає, не пропонуємо: це найшвидший спосіб втратити довіру.
        head.append("   Але вільних грошей у бюджеті цього місяця не лишилось — "
                    "усе або вже вкладено, або чекає на просадку у заявках.")
        if freeing_soon > 0:
            head.append(f"   Орієнтовно звільниться {fmt_money(freeing_soon)}: стільки "
                        "зараз висить у заявках, і якщо вони не спрацюють, ці гроші "
                        "перейдуть у наступний місяць.")
        head.append(f"   Щоб було з чим порівняти: на {fmt_money(ALT_TOPUP_MAX)} це "
                    f"~{fmt_money(ALT_TOPUP_MAX * apr)} за рік, а якщо {coin} впаде "
                    f"на {int(ALT_DRAWDOWN * 100)}% — мінус "
                    f"{fmt_money(ALT_TOPUP_MAX * ALT_DRAWDOWN)}.")
        head.append(f"   Докинути своїх можна, але не більше {fmt_money(ALT_TOPUP_MAX)} — "
                    "і тільки якщо сама захочеш.")
        return ["\n".join(head)]

    head.append(
        f"   Беру з вільних грошей місяця — їх лишилось "
        f"{fmt_money(position['budget_free'])}, візьму {fmt_money(position['from_budget'])}."
    )
    head.append(
        f"   Заробиш ~{fmt_money(offer.per_year(amount))} за рік. Якщо {coin} впаде "
        f"на {int(ALT_DRAWDOWN * 100)}% — втратиш ~{fmt_money(amount * ALT_DRAWDOWN)}."
    )
    head.append(
        "   Це та сама подушка на випадок просадки крипти, тому беру без блокування: "
        f"якщо ціни поїдуть униз, гроші можна забрати того ж дня. "
        f"{fmt_money(position['budget_left'])} лишаю недоторканими."
    )
    if offer.section:
        head.append(f"   шукати: {offer.section}")
    head.append(f"   Хочеш більше — можеш докинути своїх до "
                f"{fmt_money(ALT_TOPUP_MAX)}, але це не обовʼязково.")
    return ["\n".join(head)]


# --- тижневий огляд (тільки earn, тільки коли є про що казати) ---

def weekly_review_text(active: list[dict], changes: list[dict],
                       best: dict, prices: dict | None = None) -> str:
    """Підсумок тижня по вкладах: що працює, що змінилось, де зараз найкраще."""
    out = ["🗓 Тижневий огляд вкладів", ""]

    if active:
        t = active_totals(active, prices)
        out.append("Зараз працює:")
        out += [deposit_line(d) for d in active]
        if t["income_year"] > 0:
            out.append(f"Разом ~{fmt_money(t['income_year'])}/рік "
                       f"({fmt_money(t['income_year'] / 12)}/міс).")
        out.append("")

    if changes:
        out.append("Що змінилось за тиждень:")
        for ch in changes:
            arrow = "виросла" if ch["now"] > ch["was"] else "впала"
            line = (f"• {ch['coin']}: {ch['was'] * 100:.2f}% → {ch['now'] * 100:.2f}% "
                    f"({arrow})")
            if ch.get("money_year"):
                line += f", на твоїх грошах це {fmt_money(abs(ch['money_year']))}/рік"
            out.append(line)
        out.append("")

    if best:
        out.append("Найкраще зараз:")
        for coin, offer in best.items():
            out.append(f"• {coin} — {offer.steps()[0].apr * 100:.2f}% ({offer.label})")
        out.append("")

    out.append("Це лише про вклади під відсоток — крипта й ETF рахуються окремо.")
    return "\n".join(out).strip()


def _advisor_note(rec: Recommendation) -> str:
    """Порожньо, якщо відповідав порадник або його свідомо вимкнено (нема ключа)."""
    if rec.source == "ai" or not config.gemini_api_key:
        return ""
    return NO_ADVISOR_NOTE


def _blocked_notes(rec: Recommendation) -> list[str]:
    """Чому по якійсь монеті заявки нема — простою мовою, а не мовчки."""
    return [f"↩️ {cp.blocked_note}" for cp in rec.coins if getattr(cp, "blocked_note", "")]


def format_recommendation(rec: Recommendation) -> str:
    blocked = _blocked_notes(rec)
    advisor_note = _advisor_note(rec)

    if rec.moment == "wait" or not rec.has_orders():
        body = rec.headline
        if rec.reasoning:
            body += "\n\n🤖 " + rec.reasoning
        if blocked:
            body += "\n\n" + "\n".join(blocked)
        if rec.note:
            body += "\n\n" + rec.note
        if advisor_note:
            body += "\n\n" + advisor_note
        return body

    out = [
        f"{rec.headline} (на {fmt_usd(rec.amount)})",
        "Постав ті, на які є гроші — кожен можна підтвердити або пропустити кнопками нижче.",
        "",
    ]
    i = 0
    for cp in rec.coins:
        if not cp.orders:
            continue
        out.append(f"{cp.coin}:")
        for o in cp.orders:
            i += 1
            # Ціна й сума — в <code>, щоб тапнути й скопіювати саме число (без
            # «$» і без пробілу-роздільника) прямо в поле лімітного ордера.
            out.append(
                f"#{i} · {fmt_qty(cp.coin, o.qty)} {cp.coin} по "
                f"${CODE_OPEN}{fmt_price_raw(o.price)}{CODE_CLOSE} "
                f"({CODE_OPEN}{fmt_usd_raw(o.notional)}{CODE_CLOSE}$)"
            )
        out.append("")
    if blocked:
        out += blocked + [""]
    if rec.reasoning:
        out.append(f"🤖 {rec.reasoning}")
        out.append("")
    if rec.note:
        out.append(rec.note)
    if advisor_note:
        out.append(advisor_note)
    out.append("⚠️ Не фінансова порада.")
    return "\n".join(out).strip()
