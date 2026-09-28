# -*- coding: utf-8 -*-
"""رابط تلگرام روبو۷ الوند — فاز ۱ (سیگنال + تحلیل، بدون اجرای سفارش)."""
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

# --- منوی پایین چت (Reply Keyboard) ---
_BTN_ANALYZE = "📊 تحلیل"
_BTN_HELP = "ℹ️ راهنما"

MAIN_MENU = ReplyKeyboardMarkup(
    [[_BTN_ANALYZE, _BTN_HELP]],
    resize_keyboard=True,
)


def validate_token(token: str) -> bool:
    return bool(_TOKEN_RE.match(token))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(M.START, reply_markup=MAIN_MENU)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(M.HELP, reply_markup=MAIN_MENU)


def _is_symbol(text: str) -> bool:
    return bool(_SYMBOL_RE.match(text.strip().upper()))


async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """کلیک روی دکمه‌های منو را قبل از symbol_handler می‌گیرد."""
    text = (update.message.text or "").strip()

    if text == _BTN_HELP:
        await update.message.reply_text(M.HELP, reply_markup=MAIN_MENU)
        return

    if text == _BTN_ANALYZE:
        await update.message.reply_text(
            "📊 اسم نماد را بفرست تا تحلیل بگیرى \u2014 مثلاً: BTC یا XAUUSD 👇",
            reply_markup=MAIN_MENU,
        )
        return


async def symbol_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()
    if not _is_symbol(text):
        await update.message.reply_text(
            M.BAD_SYMBOL.format(symbol= update.message.reply_text(M.BMAIN_MENU
        )
        return

    await update.message.reply_text(M.BUSY)

    md = await get_market_data(text, interval=TIMEFRAME_INTERVAL, limit=CANDLE_LIMIT)
    if md is None:
        await update.message.reply_text(
            M.NO_DATA.format(symbol=text), reply_markup=MAIN_MENU
        )
        return

    # build_setup persists through signals._persist_signal; preserve the known
    # Telegram identity so db.add_signal receives the required telegram_id.
    if update.effective_user is not None:
        md.telegram_id = update.effective_user.id

    an = analyze(md, timeframe=TIMEFRAME_LABEL)
    setup, reason = build_setup(md, an)

    # build_setup deliberately returns an invalid setup alongside its reason;
    # only format it as a trade setup when the risk validation passed.
    if setup is not None and reason == "OK":
        await update.message.reply_text(format_setup(md, an, setup), parse_mode="HTML")
    else:
        await update.message.reply_text(M.NO_SETUP.format(reason=reason), parse_mode="HTML")

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
    """اتصال best-effort به پایگاه‌داده؛ نبود یا قطعی DB مانع اجرای ربات نمی‌شود."""
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
    # منو اول ثبت شود تا symbol_handler دکمه‌ها را نماد فرض نکند
    application.add_handler(
        MessageHandler(filters.Regex(r"^(📊 تحلیل|ℹ️ راهنما)$"), menu_handler)
    )
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, symbol_handler))
    application.add_error_handler(error_handler)

    log.info("Bot polling started")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
