# -*- coding: utf-8 -*-
"""رابط تلگرام روبو۷ الوند — فاز ۱
دو گزینهٔ مستقل: «ستاپ معاملاتی» و «تجزیه و تحلیل». بدون اجرای سفارش.
"""
from __future__ import annotations

import logging
import os
import re

from aiohttp import web
from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from data import get_market_data
from signals import analyze, build_setup, format_analysis, format_setup
import messages as M

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("robo7.bot")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

TOKEN = os.getenv("BOT_TOKEN", "").strip()
PORT = int(os.getenv("PORT", "8080"))
TIMEFRAME_INTERVAL = "1h"
TIMEFRAME_LABEL = "1H"
CANDLE_LIMIT = 200

_TOKEN_RE = re.compile(r"^\d{5,15}:[A-Za-z0-9_-]{30,50}$")
_SYMBOL_RE = re.compile(r"^[A-Z0-9/._=]{2,20}$")

# ---------- منوی پایین چت: فقط دو گزینه ----------
_BTN_SETUP = "📈 ستاپ معاملاتی"
_BTN_ANALYSIS = "📊 تجزیه و تحلیل"

MAIN_MENU = ReplyKeyboardMarkup(
    [[_BTN_SETUP], [_BTN_ANALYSIS]],
    resize_keyboard=True,
)

# ---------- حالت فعالِ هر کاربر ----------
MODE_KEY = "mode"
MODE_SETUP = "setup"
MODE_ANALYSIS = "analysis"

_ASK_SYMBOL = {
    MODE_SETUP: (
        "📈 حالت «ستاپ معاملاتی» فعال شد.\n"
        "اسم نماد را بفرست — مثلاً: BTC یا XAUUSD 👇"
    ),
    MODE_ANALYSIS: (
        "📊 حالت «تجزیه و تحلیل» فعال شد.\n"
        "اسم نماد را بفرست — مثلاً: ETH یا EURUSD 👇"
    ),
}

_PICK_FIRST = (
    "اول از منوی پایین یکی را انتخاب کن 👇\n"
    "📈 ستاپ معاملاتی  •  📊 تجزیه و تحلیل"
)

# رجکس منو از خودِ دکمه‌ها ساخته می‌شود تا هرگز ناهم‌خوانی پیش نیاید
_MENU_RE = r"^(" + re.escape(_BTN_SETUP) + "|" + re.escape(_BTN_ANALYSIS) + r")$"


def validate_token(token: str) -> bool:
    return bool(_TOKEN_RE.match(token))


def _is_symbol(text: str) -> bool:
    return bool(_SYMBOL_RE.match(text.strip().upper()))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """خوشامد + نمایش منوی دو گزینه‌ای؛ حالت قبلی پاک می‌شود."""
    context.user_data.pop(MODE_KEY, None)
    await update.message.reply_text(M.START, reply_markup=MAIN_MENU)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(M.HELP, reply_markup=MAIN_MENU)


async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """کلیک روی هر یک از دو دکمه: حالت را ذخیره و نماد را می‌پرسد."""
    text = (update.message.text or "").strip()

    if text == _BTN_SETUP:
        context.user_data[MODE_KEY] = MODE_SETUP
    elif text == _BTN_ANALYSIS:
        context.user_data[MODE_KEY] = MODE_ANALYSIS
    else:
        return

    await update.message.reply_text(
        _ASK_SYMBOL[context.user_data[MODE_KEY]], reply_markup=MAIN_MENU
    )


async def symbol_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()
    mode = context.user_data.get(MODE_KEY)

    if mode not in (MODE_SETUP, MODE_ANALYSIS):
        await update.message.reply_text(_PICK_FIRST, reply_markup=MAIN_MENU)
        return

    if not _is_symbol(text):
        await update.message.reply_text(
            M.BAD_SYMBOL.format(symbol=text), reply_markup=MAIN_MENU
        )
        return

    await update.message.reply_text(M.BUSY)

    md = await get_market_data(text, interval=TIMEFRAME_INTERVAL, limit=CANDLE_LIMIT)
    if md is None:
        await update.message.reply_text(
            M.NO_DATA.format(symbol=text), reply_markup=MAIN_MENU
        )
        return

    # شناسهٔ تلگرام برای _persist_signal داخل build_setup لازم است
    if update.effective_user is not None:
        md.telegram_id = update.effective_user.id

    an = analyze(md, timeframe=TIMEFRAME_LABEL)

    if mode == MODE_SETUP:
        setup, reason = build_setup(md, an)
        if setup is not None and reason == "OK":
            await update.message.reply_text(
                format_setup(md, an, setup), parse_mode="HTML"
            )
        else:
            await update.message.reply_text(
                M.NO_SETUP.format(reason=reason), parse_mode="HTML"
            )
        return

    # حالت تحلیل: عمداً build_setup صدا زده نمی‌شود تا سیگنالی ثبت نشود
    await update.message.reply_text(format_analysis(md, an), parse_mode="HTML")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled error", exc_info=context.error)
    try:
        if update is not None and getattr(update, "effective_message", None):
            await update.effective_message.reply_text(M.GENERIC_ERROR)
    except Exception:
        pass


async def health(request: web.Request) -> web.Response:
    return web.json_response(
        {"status": "ok", "service": "robo7alvand", "role": "signal-only"}
    )


_health_runner = None


async def _connect_db() -> None:
    """اتصال best-effort؛ نبود DB مانع اجرای ربات نمی‌شود."""
    try:
        from db import get_db
        await get_db().connect()
        log.info("Database connected")
    except Exception as exc:
        log.warning("Database unavailable; continuing without persistence: %s", exc)


async def _start_health(application) -> None:
    global _health_runner
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    _health_runner = runner
    log.info("Health server listening on port %s", PORT)
    await _connect_db()


async def _stop_health(application) -> None:
    global _health_runner
    if _health_runner is not None:
        await _health_runner.cleanup()
        _health_runner = None
        log.info("Health server shut down")
    try:
        from db import get_db
        await get_db().close()
    except Exception as exc:
        log.warning("Database close skipped: %s", exc)


def main() -> None:
    if not validate_token(TOKEN):
        log.critical("BOT_TOKEN missing or invalid")
        raise SystemExit("Invalid BOT_TOKEN")

    application = (
        Application.builder()
        .token(TOKEN)
        .post_init(_start_health)
        .post_shutdown(_stop_health)
        .build()
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    # منو قبل از هندلر متن ثبت شود، وگرنه دکمه‌ها «نماد» تلقی می‌شوند
    application.add_handler(MessageHandler(filters.Regex(_MENU_RE), menu_handler))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, symbol_handler)
    )
    application.add_error_handler(error_handler)

    log.info("Bot polling started")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
