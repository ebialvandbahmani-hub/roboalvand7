# -*- coding: utf-8 -*-
from __future__ import annotations

def sma(values, period):
    if len(values) < period:
        return []
    return [sum(values[i:i+period]) / period for i in range(len(values) - period + 1)]

def ema(values, period):
    if len(values) < period:
        return []
    k = 2.0 / (period + 1)
    out = [sum(values[:period]) / period]
    for v in values[period:]:
        out.append(v * k + out[-1] * (1 - k))
    return out

def rsi(closes, period=14):
    if len(closes) < period + 1:
        return []
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    def calc(g, l):
        if l == 0:
            return 100.0
        rs = g / l
        return 100.0 - 100.0 / (1.0 + rs)
    out = [calc(ag, al)]
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
        out.append(calc(ag, al))
    return out

def atr(candles, period=14):
    if len(candles) < period + 1:
        return []
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i].high, candles[i].low, candles[i - 1].close
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    out = [sum(trs[:period]) / period]
    for i in range(period, len(trs)):
        out.append((out[-1] * (period - 1) + trs[i]) / period)
    return out

def swing_levels(candles, lookback=5, count=3):
    n = len(candles)
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    sh, sl = [], []
    for i in range(lookback, n - lookback):
        if highs[i] >= max(highs[i - lookback:i + lookback + 1]):
            sh.append(highs[i])
        if lows[i] <= min(lows[i - lookback:i + lookback + 1]):
            sl.append(lows[i])
    res = sorted(set(round(x, 6) for x in sh), reverse=True)[:count]
    sup = sorted(set(round(x, 6) for x in sl))[:count]
    return sup, res

def trend(closes, fast=20, slow=50):
    ef, es = ema(closes, fast), ema(closes, slow)
    if not ef or not es:
        return "UNKNOWN"
    if ef[-1] > es[-1] and closes[-1] > ef[-1]:
        return "UP"
    if ef[-1] < es[-1] and closes[-1] < ef[-1]:
        return "DOWN"
    return "RANGE"
