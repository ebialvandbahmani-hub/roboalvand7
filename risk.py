# -*- coding: utf-8 -*-
"""Risk validation and conservative position sizing helpers."""
from __future__ import annotations
from dataclasses import dataclass, field

MIN_RR = 2.0
# Conservative local safeguards; config.py currently defines no trade-risk/lot caps.
MAX_RISK_PERCENT = 2.0
MAX_POSITION_SIZE = 100.0
MAX_CRYPTO_NOTIONAL = 1_000_000.0

try:
    from data import METALS as _METALS, CRYPTO_QUOTES as _CRYPTO_QUOTES, detect_market as _detect_market
except Exception:  # Standalone import / minimal deployments.
    _METALS = {"XAU", "XAG", "XPT", "XPD"}
    _CRYPTO_QUOTES = ("USDT", "USDC", "BUSD", "FDUSD", "TUSD", "BTC", "ETH", "BNB")
    _detect_market = None


def classify_market(symbol: str) -> str:
    """Return one of crypto, forex, metals (metal detection takes precedence)."""
    s = str(symbol or "").upper().replace("/", "").replace("=X", "").replace(" ", "").strip()
    if any(s.startswith(m) for m in _METALS):
        return "metals"
    if s.endswith(tuple(_CRYPTO_QUOTES)):
        return "crypto"
    if _detect_market:
        try:
            return "forex" if _detect_market(s) == "forex" else "crypto"
        except Exception:
            pass
    if len(s) == 6 and s.isalpha():
        return "forex"
    return "crypto"


def pip_size(symbol: str, market: str | None = None) -> float:
    """Conventional price increment per pip for FX and common metals."""
    kind = market or classify_market(symbol)
    s = str(symbol).upper().replace("/", "")
    if kind == "metals" or any(s.startswith(m) for m in _METALS):
        return 0.001 if s.startswith("XAG") else 0.01
    if kind == "forex":
        return 0.01 if s.endswith("JPY") else 0.0001
    return 0.0


def stop_distance_pips(symbol: str, entry: float, stop_loss: float, market: str | None = None) -> float:
    pip = pip_size(symbol, market)
    return abs(float(entry) - float(stop_loss)) / pip if pip else 0.0


def clamp_risk_percent(risk_percent: float) -> float:
    try:
        return max(0.0, min(float(risk_percent), MAX_RISK_PERCENT))
    except (TypeError, ValueError):
        return 0.0


def apply_anti_martingale(size: float, previous_size: float | None = None, last_trade_loss: bool = False) -> float:
    """A losing trade cannot increase size; all results obey the hard cap."""
    value = max(0.0, min(float(size), MAX_POSITION_SIZE))
    if last_trade_loss and previous_size is not None:
        value = min(value, max(0.0, float(previous_size)))
    return value


def suggest_position_size(symbol: str, entry: float, stop_loss: float, capital: float,
                          risk_percent: float = 1.0, market: str | None = None,
                          contract_size: float = 100000.0, previous_size: float | None = None,
                          last_trade_loss: bool = False) -> dict:
    """Return sizing details. FX/metals volume is standard lots; crypto is quantity."""
    kind = market or classify_market(symbol)
    risk_amount = max(0.0, float(capital)) * clamp_risk_percent(risk_percent) / 100.0
    distance = abs(float(entry) - float(stop_loss))
    if distance <= 0:
        return {"market": kind, "risk_amount": risk_amount, "size": 0.0, "unit": "lot" if kind != "crypto" else "quantity", "valid": False}
    if kind == "crypto":
        quantity = risk_amount / distance
        notional = min(quantity * float(entry), MAX_CRYPTO_NOTIONAL)
        quantity = min(quantity, notional / float(entry) if entry else 0.0, MAX_POSITION_SIZE)
        size, unit = quantity, "quantity"
        notional = quantity * float(entry)
    else:
        pip = pip_size(symbol, kind)
        # Approximation in account-quote currency: one FX lot has contract_size base units.
        lots = risk_amount / (distance * float(contract_size)) if contract_size else 0.0
        size, unit = apply_anti_martingale(lots, previous_size, last_trade_loss), "lot"
        notional = size * float(contract_size) * float(entry)
        return {"market": kind, "risk_amount": risk_amount, "pip_size": pip,
                "stop_distance_pips": distance / pip if pip else 0.0,
                "size": size, "volume": size, "unit": unit, "notional": notional,
                "valid": size > 0}
    if last_trade_loss and previous_size is not None:
        size = min(size, max(0.0, float(previous_size)))
        notional = size * float(entry)
    return {"market": kind, "risk_amount": risk_amount, "size": size, "quantity": size,
            "unit": unit, "notional": notional, "valid": size > 0}


@dataclass
class TradeSetup:
    symbol: str
    side: str
    entry: float
    stop_loss: float
    take_profits: list
    order_type: str = "LIMIT"
    reasons: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    final_target: float = 0.0

    @property
    def risk(self):
        return abs(self.entry - self.stop_loss)

    @property
    def reward(self):
        return abs(self.take_profits[-1] - self.entry) if self.take_profits else 0.0

    @property
    def rr(self):
        return (self.reward / self.risk) if self.risk else 0.0

    def is_valid(self):
        side = str(self.side).strip().upper()
        if side not in {"BUY", "LONG", "SELL", "SHORT"}:
            return False, "invalid side"
        long_side = side in {"BUY", "LONG"}
        try:
            entry, stop = float(self.entry), float(self.stop_loss)
            targets = [float(x) for x in self.take_profits]
        except (TypeError, ValueError):
            return False, "invalid prices"
        if entry <= 0 or stop <= 0 or self.risk <= 0:
            return False, "invalid stop"
        if (long_side and stop >= entry) or (not long_side and stop <= entry):
            return False, "stop wrong direction"
        if not targets or any((tp <= entry if long_side else tp >= entry) for tp in targets):
            return False, "TP wrong direction"
        if self.rr < MIN_RR:
            return False, f"RR below 1:{MIN_RR:g}"
        final = float(self.final_target or targets[-1])
        if (long_side and final <= entry) or (not long_side and final >= entry):
            return False, "final target wrong direction"
        # Signals may attach range metadata; retain compatibility for manually-built setups.
        if not getattr(self, "allow_mid_range", False):
            pos = getattr(self, "range_pos", None)
            if pos is not None and 0.35 <= float(pos) <= 0.65:
                return False, "entry in mid-range"
        size = getattr(self, "position_size", getattr(self, "size", None))
        if size is not None and (float(size) < 0 or float(size) > MAX_POSITION_SIZE):
            return False, "position size exceeds cap"
        notional = getattr(self, "notional", None)
        if notional is not None and float(notional) > MAX_CRYPTO_NOTIONAL:
            return False, "notional exceeds cap"
        return True, "OK"


# Friendly aliases for integrations that use explicit helper names.
market_of = classify_market
calculate_position_size = suggest_position_size
get_pip_size = pip_size
get_stop_distance_pips = stop_distance_pips
