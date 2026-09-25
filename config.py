# -*- coding: utf-8 -*-
"""
config.py — لایه پیکربندی «ربو۷ الوند»
همهٔ مقادیر حساس از متغیرهای محیطی خوانده می‌شوند و هرگز هاردکد نمی‌شوند.
نکته: باید یک فایل .env (فقط محلی) یا تنظیمات Environment سرویس ابری داشته باشید.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Final


class ConfigError(Exception):
    """خطای پیکربندی — با پیام فارسی روشن."""


# ---------------------------------------------------------------------------
# منطقهٔ زمانی
# ---------------------------------------------------------------------------
TIMEZONE: Final[str] = "Asia/Tehran"

# ---------------------------------------------------------------------------
# خواندن متغیرهای محیطی
# ---------------------------------------------------------------------------

def _get_env(name: str, default: str | None = None) -> str:
    val = os.environ.get(name)
    if val is None or val.strip() == "":
        if default is None:
            return ""
        return default
    return val.strip()


def _get_int_env(name: str, default: int) -> int:
    raw = _get_env(name, str(default))
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ConfigError(f"مقدار متغیر محیطی «{name}» باید عدد صحیح باشد، مقدار فعلی نامعتبر است.")


# ---------------------------------------------------------------------------
# تنظیمات اصلی
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Config:
    BOT_TOKEN: str
    DATABASE_URL: str
    SUPER_ADMIN_ID: int
    HEALTH_PORT: int
    TIMEZONE: str = TIMEZONE

    # ---------------- محدودیت‌های جهانی (ضد سوءاستفاده) ----------------
    # پیام‌های عمومی: ۵ پیام در هر ۱۰ ثانیه
    RATE_MESSAGES_MAX: int = 5
    RATE_MESSAGES_WINDOW_SEC: int = 10
    # درخواست نماد/تحلیل: ۲۰ در ساعت
    RATE_SYMBOL_MAX: int = 20
    RATE_SYMBOL_WINDOW_SEC: int = 3600
    # قیمت‌گیری: ۳۰ در ساعت
    RATE_PRICE_MAX: int = 30
    RATE_PRICE_WINDOW_SEC: int = 3600
    # حداکثر ۵ هشدار قیمت فعال هم‌زمان برای هر کاربر
    MAX_ACTIVE_PRICE_ALERTS: int = 5
    # گزارش معاملاتی: ۵ گزارش در روز، حداکثر ۱۰۰۰ کاراکتر
    RATE_REPORTS_PER_DAY: int = 5
    REPORT_MAX_LENGTH: int = 1000
    # حداکثر ۱۰ نمودار در روز
    RATE_CHARTS_PER_DAY: int = 10
    # تأخیر ژورنال پس از سیگنال: ۲۰ دقیقه
    JOURNAL_DELAY_MINUTES: int = 20
    # پنجرهٔ ایمنی تقویم اقتصادی: ۱۰ دقیقه قبل/بعد
    ECONOMIC_CALENDAR_WINDOW_MINUTES: int = 10

    # محدودیت‌های ایمنی عمومی بیشتر (ضد DoS / سوءاستفاده)
    MAX_SYMBOL_LENGTH: int = 10
    MIN_SYMBOL_LENGTH: int = 2
    CAPITAL_MIN: float = 0.0        # تومان / دلار — بازهٔ منطقی سرمایه
    CAPITAL_MAX: float = 1_000_000_000.0
    MAX_ALERT_LIFETIME_HOURS: int = 72   # عمر حداکثر هر هشدار قیمت
    DB_POOL_MIN: int = 1
    DB_POOL_MAX: int = 10

    extras: dict = field(default_factory=dict, repr=False, compare=False)


def load_config() -> Config:
    """
    پیکربندی را از محیط می‌خواند و اعتبارسنجی می‌کند.
    اگر BOT_TOKEN یا DATABASE_URL تنظیم نشده باشد، خطای فارسی روشن می‌دهد.
    """
    # بارگذاری اختیاری فایل .env اگر python-dotenv نصب باشد
    try:
        from dotenv import load_dotenv  # type: ignore
        load_dotenv()
    except ImportError:
        pass

    bot_token = _get_env("BOT_TOKEN")
    database_url = _get_env("DATABASE_URL")

    missing = []
    if not bot_token:
        missing.append("BOT_TOKEN")
    if not database_url:
        missing.append("DATABASE_URL")

    if missing:
        names = " و ".join(missing)
        raise ConfigError(
            f"خطای پیکربندی: متغیر(های) محیطی «{names}» تنظیم نشده است.\n"
            "لطفاً در فایل .env یا در تنظیمات Environment سرویس ابری مقدار آن‌ها را وارد کنید.\n"
            "نمونه:\n"
            "  BOT_TOKEN=توکن_ربات_از_BotFather\n"
            "  DATABASE_URL=postgresql://user:password@host:5432/dbname\n"
            "  SUPER_ADMIN_ID=آیدی_عددی_تلگرام_مالک"
        )

    if not (":" in bot_token and bot_token.split(":", 1)[0].isdigit()):
        raise ConfigError(
            "خطای پیکربندی: مقدار BOT_TOKEN نامعتبر است. توکن را عیناً از BotFather کپی کنید."
        )

    if not database_url.startswith(("postgres://", "postgresql://")):
        raise ConfigError(
            "خطای پیکربندی: مقدار DATABASE_URL باید به شکل postgresql://user:pass@host:5432/db باشد "
            "(PostgreSQL پایدار مثل Render/Neon/Supabase)."
        )

    super_admin_raw = _get_env("SUPER_ADMIN_ID")
    if not super_admin_raw:
        raise ConfigError(
            "خطای پیکربندی: متغیر محیطی «SUPER_ADMIN_ID» تنظیم نشده است.\n"
            "آیدی عددی تلگرام مالک (owner) را وارد کنید؛ این دسترسی قابل واگذاری به مدیران دیگر است."
        )
    try:
        super_admin_id = int(super_admin_raw)
    except ValueError:
        raise ConfigError("خطای پیکربندی: مقدار «SUPER_ADMIN_ID» باید آیدی عددی تلگرام باشد.")

    if super_admin_id <= 0:
        raise ConfigError("خطای پیکربندی: مقدار «SUPER_ADMIN_ID» باید عددی مثبت باشد.")

    # RENDER و Pritunl مانند، پورت را در $PORT می‌دهند
    health_port = _get_int_env("PORT", 8080)
    if not (1 <= health_port <= 65535):
        raise ConfigError("خطای پیکربندی: پورت تعیین‌شده در «PORT» خارج از بازهٔ معتبر 1 تا 65535 است.")

    return Config(
        BOT_TOKEN=bot_token,
        DATABASE_URL=database_url,
        SUPER_ADMIN_ID=super_admin_id,
        HEALTH_PORT=health_port,
    )


# ---------------------------------------------------------------------------
# نمونهٔ سراسری (lazy) — جاهای دیگر: from config import get_config
# ---------------------------------------------------------------------------
_config: Config | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = load_config()
    return _config
