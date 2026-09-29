"""Indicadores técnicos implementados em pandas (sem dependências nativas)."""
import numpy as np
import pandas as pd


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).mean()


def _wilder(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(alpha=1 / n, adjust=False).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = _wilder(d.clip(lower=0), n)
    down = _wilder(-d.clip(upper=0), n)
    total = up + down
    return (100 * up / total.replace(0, np.nan)).fillna(50)


def true_range(df: pd.DataFrame) -> pd.Series:
    prev = df["close"].shift()
    return pd.concat(
        [df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1
    ).max(axis=1)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    return _wilder(true_range(df), n)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    line = ema(close, fast) - ema(close, slow)
    sig = ema(line, signal)
    return line, sig, line - sig


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0):
    mid = sma(close, n)
    sd = close.rolling(n).std(ddof=0)
    return mid, mid + k * sd, mid - k * sd


def adx(df: pd.DataFrame, n: int = 14):
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    tr = _wilder(true_range(df), n).replace(0, np.nan)
    plus_di = 100 * _wilder(plus_dm, n) / tr
    minus_di = 100 * _wilder(minus_dm, n) / tr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return _wilder(dx.fillna(0), n), plus_di, minus_di


def stoch_rsi(close: pd.Series, n: int = 14, k: int = 3, d: int = 3):
    r = rsi(close, n)
    lo, hi = r.rolling(n).min(), r.rolling(n).max()
    st = (100 * (r - lo) / (hi - lo).replace(0, np.nan)).fillna(50)
    k_line = st.rolling(k).mean()
    return k_line, k_line.rolling(d).mean()


def obv(df: pd.DataFrame) -> pd.Series:
    return (np.sign(df["close"].diff()).fillna(0) * df["volume"]).cumsum()


def rolling_vwap(df: pd.DataFrame, n: int = 24) -> pd.Series:
    tp = (df["high"] + df["low"] + df["close"]) / 3
    return (tp * df["volume"]).rolling(n).sum() / df["volume"].rolling(n).sum().replace(0, np.nan)


def _pivots(s: pd.Series, window: int, kind: str) -> list[int]:
    """Posições (iloc) de mínimos/máximos locais confirmados."""
    span = 2 * window + 1
    roll = s.rolling(span, center=True)
    ext = roll.min() if kind == "low" else roll.max()
    return [i for i, (v, e) in enumerate(zip(s.to_numpy(), ext.to_numpy())) if v == e]


def rsi_divergence(df: pd.DataFrame, lookback: int = 45, window: int = 3) -> dict | None:
    """Divergência entre preço e RSI nos dois últimos fundos/topos."""
    d = df.tail(lookback)
    r = rsi(df["close"]).tail(lookback).to_numpy()
    lows = _pivots(d["low"], window, "low")
    highs = _pivots(d["high"], window, "high")
    n = len(d)
    if len(lows) >= 2:
        a, b = lows[-2], lows[-1]
        if d["low"].iloc[b] < d["low"].iloc[a] and r[b] > r[a] + 2 and n - 1 - b <= 12:
            return {"type": "bullish", "bars_ago": n - 1 - b}
    if len(highs) >= 2:
        a, b = highs[-2], highs[-1]
        if d["high"].iloc[b] > d["high"].iloc[a] and r[b] < r[a] - 2 and n - 1 - b <= 12:
            return {"type": "bearish", "bars_ago": n - 1 - b}
    return None


def candle_patterns(df: pd.DataFrame) -> list[str]:
    """Padrões simples na última vela fechada."""
    if len(df) < 3:
        return []
    p, c = df.iloc[-2], df.iloc[-1]
    body = abs(c["close"] - c["open"])
    rng = max(c["high"] - c["low"], 1e-12)
    upper = c["high"] - max(c["open"], c["close"])
    lower = min(c["open"], c["close"]) - c["low"]
    out = []
    if p["close"] < p["open"] and c["close"] > c["open"] and c["close"] >= p["open"] and c["open"] <= p["close"]:
        out.append("bullish_engulfing")
    if p["close"] > p["open"] and c["close"] < c["open"] and c["close"] <= p["open"] and c["open"] >= p["close"]:
        out.append("bearish_engulfing")
    if lower >= 2 * body and upper <= max(body, rng * 0.1) and body / rng < 0.4:
        out.append("hammer")
    if upper >= 2 * body and lower <= max(body, rng * 0.1) and body / rng < 0.4:
        out.append("shooting_star")
    if body / rng <= 0.1:
        out.append("doji")
    return out


def _distinct(levels, min_gap: float, count: int) -> list:
    """Mantém níveis afastados entre si pelo menos min_gap (ordem já definida)."""
    out = []
    for lvl in levels:
        if all(abs(lvl - o) >= min_gap for o in out):
            out.append(lvl)
        if len(out) == count:
            break
    return out


def swing_levels(df: pd.DataFrame, window: int = 5, lookback: int = 150, count: int = 3):
    """Suportes/resistências a partir de topos e fundos confirmados (pivots), agrupados por 0.5 ATR."""
    d = df.tail(lookback)
    span = 2 * window + 1
    highs = d["high"][d["high"] == d["high"].rolling(span, center=True).max()]
    lows = d["low"][d["low"] == d["low"].rolling(span, center=True).min()]
    price = d["close"].iloc[-1]
    gap = 0.5 * float(atr(df).iloc[-1])
    resistances = _distinct(sorted(h for h in highs.unique() if h > price), gap, count)
    supports = _distinct(sorted((l for l in lows.unique() if l < price), reverse=True), gap, count)
    return supports, resistances
