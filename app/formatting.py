"""Форматування повідомлень: мінімум тексту, без жаргону."""
from app.engine import Recommendation


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


def fmt_qty(coin: str, q: float) -> str:
    decimals = 6 if coin == "BTC" else 4
    return f"{q:.{decimals}f}"


def fmt_usd(v: float) -> str:
    return f"${v:,.0f}".replace(",", " ")


def format_recommendation(rec: Recommendation) -> str:
    if rec.moment == "wait" or not rec.has_orders():
        body = rec.headline
        if rec.note:
            body += "\n\n" + rec.note
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
            out.append(
                f"#{i} · {fmt_qty(cp.coin, o.qty)} {cp.coin} по ${fmt_price(o.price)}  ({fmt_usd(o.notional)})"
            )
        out.append("")
    if rec.note:
        out.append(rec.note)
    out.append("⚠️ Не фінансова порада.")
    return "\n".join(out).strip()
