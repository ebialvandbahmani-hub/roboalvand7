# -*- coding: utf-8 -*-
"""Technical analysis and trade-setup formatting."""
from __future__ import annotations

import datetime as dt
import inspect
from dataclasses import dataclass, field

from indicators import ema, rsi, atr, swing_levels, trend
from risk import TradeSetup, MIN_RR


def _value(obj, name, default=None):
    """Read a candle/data field from either an object or OHLCV sequence."""
    if hasattr(obj, name):
        return getattr(obj, name)
    indexes = {"open": 1, "high": 2, "low": 3, "close": 4, "volume": 5}
    try:
        return obj[indexes[name]]
    except (KeyError, IndexError, TypeError):
        return default


def _candles(md):
    """Normalize available market candles for the attribute-based indicators."""
    from types import SimpleNamespace
    result = []
    raw = getattr(md, "candles", None) or []
    for i, c in enumerate(raw):
        try:
            result.append(SimpleNamespace(
                ts=_value(c, "ts", i), open=float(_value(c, "open", 0.0) or 0.0),
                high=float(_value(c, "high", 0.0) or 0.0),
                low=float(_value(c, "low", 0.0) or 0.0),
                close=float(_value(c, "close", 0.0) or 0.0),
                volume=float(_value(c, "volume", 0.0) or 0.0)))
        except (TypeError, ValueError):
            continue
    return result


def _range(candles, lookback=60):
    w = candles[-lookback:]
    if not w:
        return 0.0, 0.0
    return min(c.low for c in w), max(c.high for c in w)


def _g2j(gy, gm, gd):
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2 = gy + 1 if gm > 2 else gy
    days = 355666 + 365 * gy + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400 + gd + g_d_m[gm - 1]
    jy = -1595 + 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm = 1 + days // 31
        jd = 1 + days % 31
    else:
        jm = 7 + (days - 186) // 30
        jd = 1 + (days - 186) % 30
    return jy, jm, jd


def _now_fa():
    now = dt.datetime.utcnow() + dt.timedelta(hours=3, minutes=30)
    jy, jm, jd = _g2j(now.year, now.month, now.day)
    return f"{jy:04d}/{jm:02d}/{jd:02d} - {now.hour:02d}:{now.minute:02d}"


@dataclass
class Analysis:
    symbol: str
    market: str
    timeframe: str
    price: float
    trend: str
    rsi: float
    atr: float
    supports: list = field(default_factory=list)
    resistances: list = field(default_factory=list)
    range_pos: float = 0.5
    summary: str = ""


def _classify(md):
    symbol = str(getattr(md, "symbol", "") or "").upper().replace("/", "").replace("=X", "").replace(" ", "")
    try:
        from data import detect_market
        # Keep XAU variants explicitly classified as metals despite data's forex label.
        if symbol.startswith(("XAU", "XAG", "XPT", "XPD")):
            return "metals"
        detected = detect_market(symbol)
        return "metals" if detected == "metals" else detected
    except Exception:
        if symbol.startswith(("XAU", "XAG", "XPT", "XPD")):
            return "metals"
        market = getattr(md, "market", "")
        return market if market in ("crypto", "forex", "metals") else "crypto"


def analyze(md, timeframe="1H") -> Analysis:
    candles = _candles(md)
    raw_symbol = getattr(md, "symbol", "") or ""
    market = getattr(md, "market", "") or "unknown"
    try:
        price = float(getattr(md, "price", 0.0) or 0.0)
    except (TypeError, ValueError):
        price = 0.0
    closes = [c.close for c in candles]
    r = rsi(closes, 14)
    a = atr(candles, 14)
    try:
        sup, res = swing_levels(candles)
    except Exception:
        sup, res = [], []
    lo, hi = _range(candles)
    pos = (price - lo) / (hi - lo) if hi > lo else 0.5
    pos = max(0.0, min(1.0, pos))
    raw_trend = trend(closes)
    tr_fa = {"UP": "صعودی", "DOWN": "نزولی", "RANGE": "خنثی/رنج", "UNKNOWN": "نامشخص"}.get(raw_trend, "نامشخص")
    rv = float(r[-1]) if r else 50.0
    av = float(a[-1]) if a else (price * 0.005 if price else 0.0)
    rsi_txt = "اشباع خرید" if rv >= 70 else ("اشباع فروش" if rv <= 30 else "خنثی")
    summ = f"روند {tr_fa} | RSI {rv:.1f} ({rsi_txt}) | موقعیت در رنج {pos * 100:.0f}%"
    return Analysis(str(raw_symbol), str(market), timeframe, price, tr_fa, rv, av, list(sup or []), list(res or []), pos, summ)


def _news_warning(md):
    """Defensive calendar check; absent calendar data never prevents analysis."""
    try:
        from config import get_config
        cfg = get_config()
        window = int(getattr(cfg, "ECONOMIC_CALENDAR_WINDOW_MINUTES", 10))
    except Exception:
        return "تقویم اقتصادی در دسترس نیست؛ زمان اخبار مهم را پیش از ورود بررسی کنید."
    now = dt.datetime.now(dt.timezone.utc)
    for name in ("next_news_at", "next_news", "next_economic_event", "economic_calendar_next"):
        stamp = getattr(cfg, name, None)
        if stamp is None:
            continue
        try:
            if isinstance(stamp, str):
                stamp = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=dt.timezone.utc)
            minutes = abs((stamp - now).total_seconds()) / 60.0
            if minutes <= window:
                return f"رویداد اقتصادی نزدیک است (حدود {minutes:.0f} دقیقه)؛ ورود را با احتیاط بررسی کنید."
        except Exception:
            return "زمان رویداد اقتصادی قابل بررسی نیست؛ تقویم اخبار را دستی کنترل کنید."
    return "تقویم اقتصادی زمان رویداد بعدی را ارائه نمی‌کند؛ اخبار مهم را دستی بررسی کنید."


def _persist_signal(setup, md):
    """Best-effort persistence. Async DB failures do not break signal generation."""
    try:
        import asyncio
        from db import get_db
        db = get_db()
        targets = list(setup.take_profits)
        args = dict(
            telegram_id=int(getattr(md, "telegram_id", 0) or 0),
            symbol=str(setup.symbol), market_class=str(getattr(setup, "market_class", _classify(md))),
            direction=str(setup.side), entry=float(setup.entry), sl=float(setup.stop_loss),
            tp1=float(targets[0]), tp2=float(targets[1]) if len(targets) > 1 else None,
            tp3=float(targets[2]) if len(targets) > 2 else None, rr=float(setup.rr),
        )
        result = db.add_signal(**args)
        if inspect.isawaitable(result):
            async def _run():
                try:
                    await result
                except Exception as exc:
                    setup.warnings.append(
                        f"ذخیرهٔ سیگنال در پایگاه‌داده انجام نشد ({type(exc).__name__})."
                    )

            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                asyncio.run(_run())
            else:
                loop.create_task(_run())
    except Exception as exc:
        setup.warnings.append(f"ذخیرهٔ سیگنال در پایگاه‌داده انجام نشد ({type(exc).__name__}).")


def build_setup(md, an, min_rr=MIN_RR) -> TradeSetup:
    try:
        price = float(getattr(md, "price", 0.0) or 0.0)
    except (TypeError, ValueError):
        price = 0.0
    atr_value = float(getattr(an, "atr", 0.0) or 0.0)
    atr_value = atr_value if atr_value > 0 else max(price * 0.005, 1e-8)
    pos = float(getattr(an, "range_pos", 0.5) or 0.0)
    direction = str(getattr(an, "trend", "")).upper()
    if direction in ("صعودی", "UP", "BULLISH"):
        side = "BUY"
    elif direction in ("نزولی", "DOWN", "BEARISH"):
        side = "SELL"
    else:
        side = "BUY"  # Retain a concrete, but deliberately invalid, setup for callers.
    risk_dist = max(1.2 * atr_value, price * 1e-8, 1e-8)
    entry = price
    stop = entry - risk_dist if side == "BUY" else entry + risk_dist
    rr_floor = max(float(min_rr), MIN_RR)
    multiples = (1.25, max(2.1, rr_floor + 0.1), max(3.0, rr_floor + 1.0))
    targets = [entry + risk_dist * x for x in multiples] if side == "BUY" else [entry - risk_dist * x for x in multiples]
    setup = TradeSetup(
        symbol=str(getattr(md, "symbol", "") or ""), side=side, entry=round(entry, 8),
        stop_loss=round(stop, 8), take_profits=[round(x, 8) for x in targets],
        order_type="BUY LIMIT" if side == "BUY" else "SELL LIMIT",
        reasons=[str(getattr(an, "summary", "") or "تحلیل روند و ATR")], warnings=[])
    setup.final_target = round(targets[-1], 8)
    setup.range_pos = pos
    setup.market_class = _classify(md)

    # Required anti-FOMO filter: middle-of-range setups have invalid stop geometry.
    blocked = 0.35 <= pos <= 0.65
    if blocked:
        setup.stop_loss = setup.entry
        setup.warnings.append("قیمت در میانهٔ رنج است؛ ورود ممنوع (فیلتر ضد FOMO).")
    if direction not in ("صعودی", "نزولی", "UP", "DOWN", "BULLISH", "BEARISH"):
        setup.warnings.append("روند مشخص نیست؛ ستاپ برای ورود معتبر نیست.")
        setup.stop_loss = setup.entry
    news = _news_warning(md)
    if news:
        setup.warnings.append(news)
    if not blocked and direction in ("صعودی", "UP", "BULLISH", "نزولی", "DOWN", "BEARISH"):
        setup.reasons.append("جهت معامله با روند غالب و حدضرر مبتنی بر ATR تنظیم شد.")
    valid, reason = setup.is_valid()
    if not valid:
        setup.warnings.append(f"ستاپ نامعتبر: {reason}.")
    else:
        _persist_signal(setup, md)
    # Original caller contract is (setup, reason); setup is retained even if invalid.
    return setup, ("OK" if valid else f"ستاپ رد شد: {reason}")


def _fmt(x):
    if x is None:
        return "-"
    if abs(x) >= 1000:
        return f"{x:,.2f}"
    if abs(x) >= 1:
        return f"{x:,.4f}".rstrip("0").rstrip(".")
    return f"{x:.6f}".rstrip("0").rstrip(".")


def _mkt_fa(m):
    return "کریپتو" if m == "crypto" else "فارکس/فلزات"


def format_setup(md, an, setup, risk_percent=1.0) -> str:
    risk = setup.risk
    rr_final = abs(setup.final_target - setup.entry) / risk if risk else 0.0
    tp_line = " | ".join(f"TP{i}: {_fmt(tp)}" for i, tp in enumerate(setup.take_profits, 1)) or "—"
    lines = [
        "🎯 <b>ستاپ معاملاتی روبو۷ الوند</b>", "━━━━━━━━━━━━━━━━━━━━",
        f"💹 <b>نماد:</b> {getattr(md, 'symbol', '')} | 🧭 <b>نوع:</b> {setup.order_type}",
        f"🏦 <b>بازار:</b> {_mkt_fa(getattr(md, 'market', ''))} | ⏱ <b>تایم‌فریم:</b> {an.timeframe}",
        f"📍 <b>نقطه ورود طلایی:</b> <code>{_fmt(setup.entry)}</code>",
        f"🛑 <b>حد ضرر:</b> <code>{_fmt(setup.stop_loss)}</code> (فاصله {_fmt(risk)})",
        "🎯 <b>اهداف سود:</b>", f"<code>{tp_line}</code>",
        f"🏁 <b>تارگت نهایی:</b> <code>{_fmt(setup.final_target)}</code>",
        f"⚖️ <b>ریسک به ریوارد:</b> 1:{rr_final:.2f}",
        f"📊 <b>حجم پیشنهادی:</b> {risk_percent:g}٪ ریسک سرمایه",
        f"🗒 <b>منطق ورود:</b> {an.summary}",
        "⚠️ <b>هشدار:</b> فاصله حد ضرر را با اسپرد بروکر تنظیم کنید.",
        f"🕒 <b>زمان تحلیل:</b> {_now_fa()}",
    ]
    warnings = getattr(setup, "warnings", None) or []
    if warnings:
        lines.extend(f"⚠️ {warning}" for warning in warnings)
    return "\n".join(lines)


def format_analysis(md, an) -> str:
    sup_below = sorted([x for x in an.supports if x < an.price], reverse=True)[:3]
    res_above = sorted([x for x in an.resistances if x > an.price])[:3]
    lines = [
        "📊 <b>تحلیل تکنیکال روبو۷ الوند</b>", "━━━━━━━━━━━━━━━━━━━━",
        f"💹 <b>نماد:</b> {getattr(md, 'symbol', '')} | 🏦 <b>بازار:</b> {_mkt_fa(getattr(md, 'market', ''))}",
        f"⏱ <b>تایم‌فریم:</b> {an.timeframe}",
        f"💰 <b>قیمت فعلی:</b> <code>{_fmt(an.price)}</code>",
        f"📈 <b>روند:</b> {an.trend}", f"📉 <b>RSI(14):</b> {an.rsi:.1f}",
        f"📏 <b>ATR(14):</b> {_fmt(an.atr)}",
        f"🧱 <b>حمایت‌ها:</b> " + (" | ".join(_fmt(x) for x in sup_below) or "—"),
        f"🚧 <b>مقاومت‌ها:</b> " + (" | ".join(_fmt(x) for x in res_above) or "—"),
        f"📍 <b>موقعیت در رنج:</b> {an.range_pos * 100:.0f}٪",
        f"📝 <b>جمع‌بندی:</b> {an.summary}", f"🕒 <b>زمان تحلیل:</b> {_now_fa()}",
    ]
    return "\n".join(lines)
