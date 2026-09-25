# -*- coding: utf-8 -*-
"""
db.py — لایهٔ پایگاه‌دادهٔ «ربو۷ الوند»

⚠️ مهم: DATABASE_URL باید به یک PostgreSQL «پایدار» اشاره کند
   (مثل Render / Neon / Supabase). داده‌ها بین ری‌استارت‌ها حفظ می‌شوند.
   نمونه: postgresql://user:pass@host:5432/dbname

همهٔ کوئری‌ها پارامتری ($1, $2, ...) هستند تا از تزریق SQL جلوگیری شود.
هر کوئری داده‌ایِ کاربر با telegram_id درخواست‌کننده فیلتر می‌شود (جداسازی داده).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

import asyncpg

try:
    from config import get_config
except ImportError:  # pragma: no cover
    def get_config():  # type: ignore
        raise RuntimeError("config")

logger = logging.getLogger("db")

# ---------------------------------------------------------------------------
# اسکیمای پایگاه‌داده (idempotent)
# ---------------------------------------------------------------------------
SCHEMA = """
-- جدول کاربران
CREATE TABLE IF NOT EXISTS users (
    telegram_id        BIGINT PRIMARY KEY,
    full_name          TEXT,
    phone              TEXT UNIQUE,
    capital_bucket     TEXT,
    capital_value      DOUBLE PRECISION,
    market_scope       TEXT,
    skill_level        TEXT,
    accepted_disclaimer BOOLEAN NOT NULL DEFAULT FALSE,
    joined_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_active          BOOLEAN NOT NULL DEFAULT TRUE,
    is_blocked         BOOLEAN NOT NULL DEFAULT FALSE
);

-- جدول مدیران (نقش‌ها: super / admin / operator)
CREATE TABLE IF NOT EXISTS admins (
    telegram_id   BIGINT PRIMARY KEY,
    role          TEXT NOT NULL CHECK (role IN ('super', 'admin', 'operator')),
    added_by      BIGINT,
    added_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- جدول سیگنال‌ها
CREATE TABLE IF NOT EXISTS signals (
    id              SERIAL PRIMARY KEY,
    telegram_id     BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    symbol          TEXT NOT NULL,
    market_class    TEXT,
    direction       TEXT,
    entry           DOUBLE PRECISION,
    sl              DOUBLE PRECISION,
    tp1             DOUBLE PRECISION,
    tp2             DOUBLE PRECISION,
    tp3             DOUBLE PRECISION,
    rr              DOUBLE PRECISION,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    journal_due_at  TIMESTAMPTZ,
    journal_done    BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_signals_user   ON signals(telegram_id);
CREATE INDEX IF NOT EXISTS idx_signals_due    ON signals(journal_due_at) WHERE journal_done = FALSE;

-- ژورنال معاملات
CREATE TABLE IF NOT EXISTS journal_entries (
    id           SERIAL PRIMARY KEY,
    signal_id    BIGINT REFERENCES signals(id) ON DELETE CASCADE,
    telegram_id  BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    took_trade   BOOLEAN,
    result       TEXT,
    tp_hit       TEXT,
    pnl_usd      DOUBLE PRECISION,
    skip_reason  TEXT,
    report_text  TEXT,
    report_type  TEXT,
    rating       INT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_journal_user   ON journal_entries(telegram_id);
CREATE INDEX IF NOT EXISTS idx_journal_signal ON journal_entries(signal_id);

-- هشدارهای قیمت
CREATE TABLE IF NOT EXISTS price_alerts (
    id            SERIAL PRIMARY KEY,
    telegram_id   BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    symbol        TEXT NOT NULL,
    target_price  DOUBLE PRECISION NOT NULL,
    direction     TEXT,
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at    TIMESTAMPTZ,
    triggered_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_alerts_user    ON price_alerts(telegram_id);
CREATE INDEX IF NOT EXISTS idx_alerts_active  ON price_alerts(symbol) WHERE is_active = TRUE;

-- گزارش‌ها
CREATE TABLE IF NOT EXISTS reports (
    id           SERIAL PRIMARY KEY,
    telegram_id  BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    category     TEXT,
    text         TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_reports_user ON reports(telegram_id);

-- لاگ حسابرسی
CREATE TABLE IF NOT EXISTS audit_log (
    id          SERIAL PRIMARY KEY,
    actor_id    BIGINT,
    action      TEXT NOT NULL,
    target      TEXT,
    details     TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- آمار مصرف (برای رصد سوءاستفاده)
CREATE TABLE IF NOT EXISTS usage_stats (
    id           SERIAL PRIMARY KEY,
    telegram_id  BIGINT REFERENCES users(telegram_id) ON DELETE CASCADE,
    feature      TEXT NOT NULL,
    used_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_usage_user ON usage_stats(telegram_id, used_at);
"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Database:
    """پوشش asyncpg با pool و کوئری‌های پارامتری."""

    def __init__(self, database_url: Optional[str] = None) -> None:
        if database_url is None:
            cfg = get_config()
            database_url = cfg.DATABASE_URL
        self._database_url = database_url
        self._pool: Optional[asyncpg.Pool] = None

    # ------------------------------------------------------------------
    # اتصال / بستن
    # ------------------------------------------------------------------
    async def connect(self) -> None:
        """ساخت pool و اعمال اسکیما (idempotent)."""
        if self._pool is not None:
            return
        try:
            cfg = get_config()
            min_size, max_size = cfg.DB_POOL_MIN, cfg.DB_POOL_MAX
        except Exception:
            min_size, max_size = 1, 10
        self._pool = await asyncpg.create_pool(
            dsn=self._database_url,
            min_size=min_size,
            max_size=max_size,
            command_timeout=30,
        )
        async with self._pool.acquire() as conn:
            await conn.execute(SCHEMA)
        logger.info("پایگاه‌داده متصل شد و اسکیما اعمال گردید.")

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
            logger.info("اتصال پایگاه‌داده بسته شد.")

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("پایگاه‌داده متصل نیست. ابتدا connect() را فراخوانی کنید.")
        return self._pool

    # ------------------------------------------------------------------
    # کوئری‌های عمومی پایه (همه پارامتری)
    # ------------------------------------------------------------------
    async def fetch(self, query: str, *args: Any) -> List[asyncpg.Record]:
        async with self.pool.acquire() as conn:
            return await conn.fetch(query, *args)

    async def fetchrow(self, query: str, *args: Any) -> Optional[asyncpg.Record]:
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(query, *args)

    async def execute(self, query: str, *args: Any) -> str:
        async with self.pool.acquire() as conn:
            return await conn.execute(query, *args)

    # ------------------------------------------------------------------
    # کاربران — هر کوئری با telegram_id فیلتر می‌شود
    # ------------------------------------------------------------------
    async def get_user(self, telegram_id: int) -> Optional[asyncpg.Record]:
        """دریافت کاربر فقط با telegram_id خودش."""
        return await self.fetchrow(
            "SELECT * FROM users WHERE telegram_id = $1",
            telegram_id,
        )

    async def create_user(
        self,
        telegram_id: int,
        full_name: Optional[str] = None,
        phone: Optional[str] = None,
        capital_bucket: Optional[str] = None,
        capital_value: Optional[float] = None,
        market_scope: Optional[str] = None,
        skill_level: Optional[str] = None,
        accepted_disclaimer: bool = False,
    ) -> asyncpg.Record:
        """ثبت‌نام کاربر (idempotent بر اساس telegram_id)."""
        return await self.fetchrow(
            """
            INSERT INTO users (telegram_id, full_name, phone, capital_bucket,
                               capital_value, market_scope, skill_level,
                               accepted_disclaimer)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (telegram_id) DO UPDATE
                SET full_name = COALESCE($2, users.full_name),
                    phone     = COALESCE($3, users.phone),
                    capital_bucket = COALESCE($4, users.capital_bucket),
                    capital_value  = COALESCE($5, users.capital_value),
                    market_scope   = COALESCE($6, users.market_scope),
                    skill_level    = COALESCE($7, users.skill_level),
                    is_active      = TRUE
            RETURNING *
            """,
            telegram_id, full_name, phone, capital_bucket,
            capital_value, market_scope, skill_level, accepted_disclaimer,
        )

    async def update_user_capital(
        self, telegram_id: int, capital_value: float, capital_bucket: Optional[str] = None
    ) -> Optional[asyncpg.Record]:
        """به‌روزرسانی سرمایهٔ کاربر (فقط خودش)."""
        return await self.fetchrow(
            """
            UPDATE users
               SET capital_value = $2,
                   capital_bucket = COALESCE($3, capital_bucket)
             WHERE telegram_id = $1
            RETURNING *
            """,
            telegram_id, capital_value, capital_bucket,
        )

    async def phone_exists(self, phone: str) -> bool:
        """بررسی یکتا بودن شمارهٔ تلفن در کل سیستم."""
        row = await self.fetchrow(
            "SELECT 1 FROM users WHERE phone = $1 LIMIT 1",
            phone,
        )
        return row is not None

    async def set_user_blocked(self, actor_id: int, telegram_id: int, blocked: bool) -> str:
        """مسدود/آزادسازی کاربر — فقط توسط ادمین؛ در audit_log ثبت می‌شود."""
        res = await self.execute(
            "UPDATE users SET is_blocked = $2 WHERE telegram_id = $1",
            telegram_id, blocked,
        )
        await self.log_audit(actor_id, "block_user" if blocked else "unblock_user",
                             target=str(telegram_id))
        return res

    # ------------------------------------------------------------------
    # مدیران
    # ------------------------------------------------------------------
    async def add_admin(self, telegram_id: int, role: str, added_by: int) -> Optional[asyncpg.Record]:
        """افزودن مدیر (نقش: super/admin/operator)."""
        if role not in ("super", "admin", "operator"):
            raise ValueError("نقش نامعتبر است؛ باید یکی از super/admin/operator باشد.")
        return await self.fetchrow(
            """
            INSERT INTO admins (telegram_id, role, added_by)
            VALUES ($1, $2, $3)
            ON CONFLICT (telegram_id) DO UPDATE SET role = $2, added_by = $3
            RETURNING *
            """,
            telegram_id, role, added_by,
        )

    async def remove_admin(self, telegram_id: int, actor_id: int) -> str:
        return await self.execute(
            "DELETE FROM admins WHERE telegram_id = $1",
            telegram_id,
        )
        # لاگ حسابرسی
        # (به‌صورت جداگانه بعد از حذف ثبت می‌شود)

    async def get_admins(self) -> List[asyncpg.Record]:
        return await self.fetch(
            "SELECT telegram_id, role, added_by, added_at FROM admins ORDER BY added_at"
        )

    async def get_admin_role(self, telegram_id: int) -> Optional[str]:
        """دریافت نقش مدیر (None اگر مدیر نباشد)."""
        row = await self.fetchrow(
            "SELECT role FROM admins WHERE telegram_id = $1",
            telegram_id,
        )
        return row["role"] if row else None

    # ------------------------------------------------------------------
    # حسابرسی و آمار مصرف
    # ------------------------------------------------------------------
    async def log_audit(
        self,
        actor_id: int,
        action: str,
        target: Optional[str] = None,
        details: Optional[str] = None,
    ) -> None:
        """ثبت رویداد در audit_log — بدون دادهٔ حساس."""
        await self.execute(
            "INSERT INTO audit_log (actor_id, action, target, details) VALUES ($1, $2, $3, $4)",
            actor_id, action, target, details,
        )

    async def log_usage(self, telegram_id: int, feature: str) -> None:
        """ثبت یک بار استفاده از قابلیت — برای رصد سوءاستفاده."""
        await self.execute(
            "INSERT INTO usage_stats (telegram_id, feature) VALUES ($1, $2)",
            telegram_id, feature,
        )

    async def count_usage_today(self, telegram_id: int, feature: str) -> int:
        """تعداد استفاده‌های امروز (به وقت UTC) از یک قابلیت."""
        row = await self.fetchrow(
            """
            SELECT COUNT(*) AS c FROM usage_stats
             WHERE telegram_id = $1 AND feature = $2 AND used_at >= date_trunc('day', NOW())
            """,
            telegram_id, feature,
        )
        return int(row["c"]) if row else 0

    # ------------------------------------------------------------------
    # هشدارهای قیمت
    # ------------------------------------------------------------------
    async def count_active_alerts(self, telegram_id: int) -> int:
        """تعداد هشدارهای فعال کاربر (فقط خودش)."""
        row = await self.fetchrow(
            "SELECT COUNT(*) AS c FROM price_alerts WHERE telegram_id = $1 AND is_active = TRUE",
            telegram_id,
        )
        return int(row["c"]) if row else 0

    async def add_price_alert(
        self,
        telegram_id: int,
        symbol: str,
        target_price: float,
        direction: str,
        expires_at: Optional[datetime] = None,
    ) -> Optional[asyncpg.Record]:
        return await self.fetchrow(
            """
            INSERT INTO price_alerts (telegram_id, symbol, target_price, direction, expires_at)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING *
            """,
            telegram_id, symbol, target_price, direction, expires_at,
        )

    async def get_active_alerts(self, telegram_id: int) -> List[asyncpg.Record]:
        """فقط هشدارهای فعال خود کاربر."""
        return await self.fetch(
            "SELECT * FROM price_alerts WHERE telegram_id = $1 AND is_active = TRUE ORDER BY created_at",
            telegram_id,
        )

    async def deactivate_alert(self, telegram_id: int, alert_id: int) -> str:
        """غیرفعال‌سازی هشدار — فقط مالک هشدار."""
        return await self.execute(
            """
            UPDATE price_alerts
               SET is_active = FALSE, triggered_at = COALESCE(triggered_at, NOW())
             WHERE id = $2 AND telegram_id = $1
            """,
            telegram_id, alert_id,
        )

    # ------------------------------------------------------------------
    # سیگنال‌ها و ژورنال
    # ------------------------------------------------------------------
    async def add_signal(
        self,
        telegram_id: int,
        symbol: str,
        market_class: str,
        direction: str,
        entry: float,
        sl: float,
        tp1: float,
        tp2: Optional[float] = None,
        tp3: Optional[float] = None,
        rr: Optional[float] = None,
        journal_due_at: Optional[datetime] = None,
    ) -> Optional[asyncpg.Record]:
        return await self.fetchrow(
            """
            INSERT INTO signals (telegram_id, symbol, market_class, direction,
                                 entry, sl, tp1, tp2, tp3, rr, journal_due_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
            RETURNING *
            """,
            telegram_id, symbol, market_class, direction,
            entry, sl, tp1, tp2, tp3, rr, journal_due_at,
        )

    async def get_user_signals(self, telegram_id: int, limit: int = 50) -> List[asyncpg.Record]:
        """سیگنال‌های خود کاربر — جداسازی داده تضمین‌شده."""
        return await self.fetch(
            "SELECT * FROM signals WHERE telegram_id = $1 ORDER BY created_at DESC LIMIT $2",
            telegram_id, limit,
        )

    async def add_journal_entry(
        self,
        telegram_id: int,
        signal_id: int,
        took_trade: Optional[bool] = None,
        result: Optional[str] = None,
        tp_hit: Optional[str] = None,
        pnl_usd: Optional[float] = None,
        skip_reason: Optional[str] = None,
        report_text: Optional[str] = None,
        report_type: Optional[str] = None,
        rating: Optional[int] = None,
    ) -> Optional[asyncpg.Record]:
        """
        ثبت ژورنال. signal_id باید متعلق به همین کاربر باشد تا جداسازی داده حفظ شود.
        """
        owner = await self.fetchrow(
            "SELECT 1 FROM signals WHERE id = $1 AND telegram_id = $2",
            signal_id, telegram_id,
        )
        if owner is None:
            raise PermissionError("این سیگنال متعلق به شما نیست.")
        return await self.fetchrow(
            """
            INSERT INTO journal_entries (signal_id, telegram_id, took_trade, result,
                                        tp_hit, pnl_usd, skip_reason, report_text,
                                        report_type, rating)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            RETURNING *
            """,
            signal_id, telegram_id, took_trade, result,
            tp_hit, pnl_usd, skip_reason, report_text, report_type, rating,
        )

    async def get_user_journal(self, telegram_id: int, limit: int = 50) -> List[asyncpg.Record]:
        return await self.fetch(
            "SELECT * FROM journal_entries WHERE telegram_id = $1 ORDER BY created_at DESC LIMIT $2",
            telegram_id, limit,
        )

    # ------------------------------------------------------------------
    # گزارش‌ها
    # ------------------------------------------------------------------
    async def add_report(self, telegram_id: int, category: str, text: str) -> Optional[asyncpg.Record]:
        return await self.fetchrow(
            "INSERT INTO reports (telegram_id, category, text) VALUES ($1, $2, $3) RETURNING *",
            telegram_id, category, text,
        )

    async def get_user_reports(self, telegram_id: int, limit: int = 50) -> List[asyncpg.Record]:
        return await self.fetch(
            "SELECT * FROM reports WHERE telegram_id = $1 ORDER BY created_at DESC LIMIT $2",
            telegram_id, limit,
        )


# ---------------------------------------------------------------------------
# نمونهٔ سراسری — جاهای دیگر: from db import get_db
# ---------------------------------------------------------------------------
_db: Optional[Database] = None


def get_db() -> Database:
    global _db
    if _db is None:
        _db = Database()
    return _db
