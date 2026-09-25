# -*- coding: utf-8 -*-
"""
security.py — لایهٔ امنیت «ربو۷ الوند»
- محدودکنندهٔ نرخ (Rate Limiter) به‌صورت اسلایدینگ‌ویندوز، ایمن در async
- کنترل سیل (Flood Control) برای پیام‌ها
- اعتبارسنجی ورودی‌ها با پیام‌های فارسی و استثنای سفارشی ValidationError
- پاک‌سازی متن برای جلوگیری از تزریق HTML/Markdown
- دکوراتور safe_handler برای پوشش خطاها بدون افشای اطلاعات حساس
- تنظیم لاگ بدون فیلدهای حساس
"""

from __future__ import annotations

import asyncio
import html
import logging
import math
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from functools import wraps
from typing import Any, Awaitable, Callable, Deque, Dict, Tuple

try:
    from config import get_config
except ImportError:  # برای تست مستقل
    def get_config():  # type: ignore
        raise RuntimeError("config")

_SENSITIVE_KEYS = (
    "phone", "phone_number", "token", "bot_token", "password",
    "database_url", "authorization", "contact", "secret",
    "api_key", "twelve_data_api_key"
)


class SensitiveFilter(logging.Filter):
    """هر فیلد حساس در پیام لاگ را با *** جایگزین می‌کند."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        for key in _SENSITIVE_KEYS:
            msg = re.sub(
                rf"{re.escape(key)}\s*[=:]\s*\S+",
                f"{key}=***",
                msg,
                flags=re.IGNORECASE,
            )
        record.msg = msg
        record.args = ()
        return True


def setup_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    )
    handler.addFilter(SensitiveFilter())
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(handler)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    logging.getLogger("asyncpg").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# استثنای سفارشی اعتبارسنجی
# ---------------------------------------------------------------------------
class ValidationError(ValueError):
    """خطای اعتبارسنجی با پیام فارسی."""


# ---------------------------------------------------------------------------
# Rate Limiter — پنجرهٔ لغزان per (user_id, feature)
# ---------------------------------------------------------------------------
_DEFAULT_LIMITS: Dict[str, Tuple[int, int]] = {
    "message": (5, 10),       # ۵ پیام در ۱۰ ثانیه
    "symbol": (20, 3600),     # ۲۰ درخواست نماد در ساعت
    "price": (30, 3600),      # ۳۰ قیمت‌گیری در ساعت
    "alert": (5, 3600),       # حداکثر ۵ هشدار فعال
    "report": (5, 86400),     # ۵ گزارش در روز
    "chart": (10, 86400),     # ۱۰ نمودار در روز
}


@dataclass
class _Window:
    events: Deque[float]
    max_events: int
    window_sec: int


class RateLimiter:
    """محدودکنندهٔ نرخ per-user/per-feature با پنجرهٔ لغزان؛ async-safe با قفل."""

    def __init__(self) -> None:
        self._buckets: Dict[Tuple[int, str], _Window] = {}
        self._lock = asyncio.Lock()
        self._last_cleanup: float = time.monotonic()
        self._cleanup_interval: float = 300.0

    @staticmethod
    def _key(user_id: int, feature: str) -> Tuple[int, str]:
        return (int(user_id), str(feature))

    def _limits_for(self, feature: str) -> Tuple[int, int]:
        try:
            cfg = get_config()
            mapping = {
                "message": (cfg.RATE_MESSAGES_MAX, cfg.RATE_MESSAGES_WINDOW_SEC),
                "symbol": (cfg.RATE_SYMBOL_MAX, cfg.RATE_SYMBOL_WINDOW_SEC),
                "price": (cfg.RATE_PRICE_MAX, cfg.RATE_PRICE_WINDOW_SEC),
                "alert": (cfg.MAX_ACTIVE_PRICE_ALERTS, 3600),
                "report": (cfg.RATE_REPORTS_PER_DAY, 86400),
                "chart": (cfg.RATE_CHARTS_PER_DAY, 86400),
            }
            return mapping.get(feature, _DEFAULT_LIMITS.get(feature, (30, 3600)))
        except Exception:
            return _DEFAULT_LIMITS.get(feature, (30, 3600))

    async def check(self, user_id: int, feature: str) -> Tuple[bool, int]:
        """برمی‌گرداند: (مجاز؟، ثانیه‌های صبر تا مجاز شدن بعدی)."""
        max_events, window_sec = self._limits_for(feature)
        async with self._lock:
            now = time.monotonic()
            self._maybe_cleanup(now)

            key = self._key(user_id, feature)
            win = self._buckets.get(key)
            if win is None:
                win = _Window(deque(), max_events, window_sec)
                self._buckets[key] = win

            while win.events and now - win.events[0] > win.window_sec:
                win.events.popleft()

            if len(win.events) < win.max_events:
                win.events.append(now)
                return True, 0

            retry_after = int(win.window_sec - (now - win.events[0])) + 1
            return False, max(1, retry_after)

    async def reset(self, user_id: int, feature: str) -> None:
        async with self._lock:
            self._buckets.pop(self._key(user_id, feature), None)

    def _maybe_cleanup(self, now: float) -> None:
        """حذف ورودی‌های خاموش برای جلوگیری از رشد بی‌رویهٔ حافظه."""
        if now - self._last_cleanup < self._cleanup_interval:
            return
        self._last_cleanup = now
        stale = [
            key for key, win in self._buckets.items()
            if not win.events or now - win.events[-1] > max(win.window_sec * 2, 600)
        ]
        for key in stale:
            self._buckets.pop(key, None)


rate_limiter = RateLimiter()


# ---------------------------------------------------------------------------
# کنترل سیل پیام‌ها (فاصلهٔ حداقل بین دو پیام پشت‌سرهم)
# ---------------------------------------------------------------------------
async def flood_control(user_id: int, min_interval_sec: float = 1.0) -> Tuple[bool, int]:
    """بازمی‌گرداند: (مجاز؟، ثانیهٔ صبر)."""
    if not hasattr(flood_control, "_last"):
        flood_control._last: Dict[int, float] = {}  # type: ignore[attr-defined]
    last: Dict[int, float] = flood_control._last  # type: ignore[attr-defined]
    now = time.monotonic()
    prev = last.get(user_id, 0.0)
    wait = int(max(0.0, min_interval_sec - (now - prev)))
    if wait > 0:
        return False, max(1, wait)
    last[user_id] = now
    if len(last) > 5000:
        cutoff = now - 60
        for uid in [u for u, t in last.items() if t < cutoff]:
            last.pop(uid, None)
    return True, 0


# ---------------------------------------------------------------------------
# اعتبارسنجی‌ها
# ---------------------------------------------------------------------------

def validate_score(value: Any) -> int:
    """امتیاز صحیح بین ۱ تا ۱۰."""
    try:
        v = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValidationError("امتیاز باید یک عدد صحیح باشد.")
    if not (1 <= v <= 10):
        raise ValidationError("امتیاز باید بین ۱ تا ۱۰ باشد.")
    return v


def validate_money(value: Any) -> float:
    """مبلغ مالی: عدد متناهی، نامنفی، حداکثر ۱ میلیارد."""
    try:
        v = float(str(value).strip().replace(",", "").replace("،", ""))
    except (TypeError, ValueError):
        raise ValidationError("مبلغ باید یک عدد معتبر باشد. مثال: 2500000")
    if not math.isfinite(v):
        raise ValidationError("مبلغ واردشده نامعتبر است.")
    if v < 0:
        raise ValidationError("مبلغ نمی‌تواند منفی باشد.")
    if v > 1e9:
        raise ValidationError("مبلغ واردشده از حد مجاز (۱ میلیارد) بیشتر است.")
    return v


def validate_price(value: Any) -> float:
    """قیمت: عدد متناهی، مثبت، حداکثر ۱ میلیارد."""
    try:
        v = float(str(value).strip().replace(",", "").replace("،", ""))
    except (TypeError, ValueError):
        raise ValidationError("قیمت باید یک عدد معتبر باشد. مثال: 92500")
    if not math.isfinite(v):
        raise ValidationError("قیمت واردشده نامعتبر است.")
    if v <= 0:
        raise ValidationError("قیمت نمی‌تواند منفی یا صفر باشد.")
    if v > 1e9:
        raise ValidationError("قیمت واردشده از حد مجاز بیشتر است.")
    return v


def validate_capital(value: Any) -> float:
    """مبلغ سرمایه در بازهٔ منطقی (پیش‌فرض ۰ تا ۱ میلیارد)."""
    v = validate_money(value)
    try:
        cfg = get_config()
        lo, hi = cfg.CAPITAL_MIN, cfg.CAPITAL_MAX
    except Exception:
        lo, hi = 0.0, 1_000_000_000.0
    if v < lo or v > hi:
        raise ValidationError(
            f"مقدار سرمایه باید بین {int(lo):,} و {int(hi):,} باشد."
        )
    return v


_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,10}$")


def validate_symbol(value: Any) -> str:
    """نماد: فقط حروف بزرگ انگلیسی و رقم، بین ۲ تا ۱۰ کاراکتر؛ خروجی uppercase."""
    s = str(value).strip().upper().replace(" ", "")
    if not s:
        raise ValidationError("نماد را وارد کنید. مثال: BTCUSDT")
    if not (2 <= len(s) <= 10):
        raise ValidationError("نماد باید بین ۲ تا ۱۰ کاراکتر باشد.")
    if not _SYMBOL_RE.match(s):
        raise ValidationError("نماد فقط می‌تواند شامل حروف بزرگ انگلیسی و رقم باشد. مثال: BTCUSDT")
    return s


def validate_report_text(value: Any) -> str:
    """متن گزارش: بدون فاصلهٔ ابتدا/انتها، غیرخالی، حداکثر ۱۰۰۰ کاراکتر."""
    s = str(value).strip()
    if not s:
        raise ValidationError("متن گزارش نمی‌تواند خالی باشد.")
    try:
        cfg = get_config()
        max_len = cfg.REPORT_MAX_LENGTH
    except Exception:
        max_len = 1000
    if len(s) > max_len:
        raise ValidationError(
            f"متن گزارش حداکثر {max_len} کاراکتر می‌تواند باشد (الان {len(s)} کاراکتر است)."
        )
    return s


# ---------------------------------------------------------------------------
# پاک‌سازی متن (ضد تزریق HTML/Markdown)
# ---------------------------------------------------------------------------

_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\u200b\u200c\u200d\u200e\u200f\u2028\u2029\ufeff]")


def sanitize_text(value: Any) -> str:
    """ورودی کاربر را HTML-escape می‌کند تا هنگام بازگرداندن متن، تزریق انجام نشود."""
    s = str(value)
    s = _CTRL_RE.sub("", s)
    return html.escape(s, quote=True)


def _safe_str(obj: Any, limit: int = 200) -> str:
    """نسخهٔ رشته‌ای امن برای لاگ، بدون افشای داده‌های حساس."""
    try:
        text = repr(obj)
    except Exception:
        text = "<unrepresentable>"
    text = re.sub(
        r"(phone|token|password|secret|database_url)\s*=\s*'[^']*'",
        r"\1='***'",
        text,
        flags=re.IGNORECASE,
    )
    return text[:limit]


# ---------------------------------------------------------------------------
# دکوراتور safe_handler
# ---------------------------------------------------------------------------

def safe_handler(func: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """
    دور هندلرهای python-telegram-bot بپیچید:
        @safe_handler
        async def my_handler(update, context): ...
    خطاها را می‌گیرد، بدون افشای شماره تلفن/توکن لاگ می‌کند و پیام فارسی می‌دهد.
    """
    @wraps(func)
    async def wrapper(update, context, *args, **kwargs):
        try:
            return await func(update, context, *args, **kwargs)
        except ValidationError as ve:
            if update is not None and getattr(update, "effective_message", None):
                try:
                    await update.effective_message.reply_text(f"⚠️ {sanitize_text(str(ve))}")
                except Exception:
                    pass
            return None
        except Exception as exc:
            logger = logging.getLogger(f"handler.{func.__name__}")
            user_id = getattr(getattr(update, "effective_user", None), "id", None)
            logger.exception(
                "خطا در هندلر %s | user_id=%s | err=%s | ctx=%s",
                func.__name__, user_id, exc, _safe_str(context, 120),
            )
            if update is not None and getattr(update, "effective_message", None):
                try:
                    await update.effective_message.reply_text(
                        "❌ خطایی رخ داد. لطفاً بعداً دوباره تلاش کنید.\n"
                        "در صورت تکرار، با پشتیبانی در تماس باشید."
                    )
                except Exception:
                    pass
            return None
    return wrapper
