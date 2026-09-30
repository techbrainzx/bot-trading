"""Constrói o 'retrato' do mercado (JSON compacto) que é enviado à IA."""
import math

import pandas as pd

import config
from . import indicators as ind


def num(x, nd: int = 4):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return round(x, nd)


def price_fmt(x):
    """Preço com 6 algarismos significativos (poupa tokens sem perder precisão útil)."""
    x = num(x, 12)
    return None if x is None else float(f"{x:.6g}")


def pct(a, b):
    """Distância percentual de a em relação a b."""
    a, b = num(a, 12), num(b, 12)
    if a is None or not b:
        return None
    return round((a / b - 1) * 100, 2)


def trend_label(price, e20, e50, e200) -> str:
    if price > e20 > e50 > e200:
        return "strong_up"
    if price < e20 < e50 < e200:
        return "strong_down"
    if e20 > e50 and price > e50:
        return "up"
    if e20 < e50 and price < e50:
        return "down"
    return "sideways"


def heuristic_score(s: dict) -> int:
    """Pontuação simples por regras (-100..100). É só mais um input para a IA."""
    score = {"strong_up": 30, "up": 15, "sideways": 0, "down": -15, "strong_down": -30}[s["trend"]]
    if s["macd_hist_pct"] is not None:
        score += 10 if s["macd_hist_pct"] > 0 else -10
    score += 5 if s["macd_hist_rising"] else -5
    rsi = s["rsi14"] if s["rsi14"] is not None else 50
    if 50 <= rsi <= 70:
        score += 10
    elif rsi > 75:
        score -= 10
    elif rsi < 30:
        score += 5
    elif rsi < 50:
        score -= 5
    if s["dist_vwap24_pct"] is not None:
        score += 5 if s["dist_vwap24_pct"] > 0 else -5
    flow = s["obv_flow_10"] or 0
    if flow > 0.2:
        score += 10
    elif flow < -0.2:
        score -= 10
    if (s["adx"] or 0) > 25:
        score += 10 if (s["plus_di"] or 0) > (s["minus_di"] or 0) else -10
    return max(-100, min(100, score))


def timeframe_summary(df: pd.DataFrame) -> dict:
    c = df["close"]
    price = float(c.iloc[-1])
    e20, e50, e200 = ind.ema(c, 20), ind.ema(c, 50), ind.ema(c, 200)
    r = ind.rsi(c)
    _, _, hist = ind.macd(c)
    mid, upper, lower = ind.bollinger(c)
    a = ind.atr(df)
    adx, pdi, mdi = ind.adx(df)
    k, d = ind.stoch_rsi(c)
    width = (upper - lower) / mid
    vol_mean = df["volume"].rolling(20).mean()
    vol_std = df["volume"].rolling(20).std()
    vol_z = (df["volume"] - vol_mean) / vol_std.replace(0, float("nan"))
    obv = ind.obv(df)
    vol10 = df["volume"].tail(10).sum()
    obv_flow = (obv.iloc[-1] - obv.iloc[-11]) / vol10 if vol10 else 0.0
    vwap = ind.rolling_vwap(df)
    rets = c.pct_change()
    supports, resistances = ind.swing_levels(df)

    macd_cross = None
    for back in range(3):
        cur, prev = hist.iloc[-1 - back], hist.iloc[-2 - back]
        if cur > 0 >= prev:
            macd_cross = f"bullish_{back}_bars_ago"
            break
        if cur < 0 <= prev:
            macd_cross = f"bearish_{back}_bars_ago"
            break

    band = float(upper.iloc[-1] - lower.iloc[-1])
    high20, low20 = df["high"].tail(20).max(), df["low"].tail(20).min()
    s = {
        "trend": trend_label(price, e20.iloc[-1], e50.iloc[-1], e200.iloc[-1]),
        "close": price_fmt(price),
        "ema20": price_fmt(e20.iloc[-1]),
        "ema50": price_fmt(e50.iloc[-1]),
        "ema200": price_fmt(e200.iloc[-1]),
        "dist_ema20_pct": pct(price, e20.iloc[-1]),
        "dist_ema50_pct": pct(price, e50.iloc[-1]),
        "dist_ema200_pct": pct(price, e200.iloc[-1]),
        "ema20_slope_5bars_pct": pct(e20.iloc[-1], e20.iloc[-6]),
        "rsi14": num(r.iloc[-1], 1),
        "rsi14_prev3": [num(x, 1) for x in r.iloc[-4:-1]],
        "macd_hist_pct": num(hist.iloc[-1] / price * 100, 4),
        "macd_hist_rising": bool(hist.iloc[-1] > hist.iloc[-2]),
        "macd_cross": macd_cross,
        "bb_percent_b": num((price - lower.iloc[-1]) / band, 2) if band else None,
        "bb_width_pct": num(width.iloc[-1] * 100, 2),
        "bb_width_percentile_120": num(width.tail(120).rank(pct=True).iloc[-1], 2),
        "atr": price_fmt(a.iloc[-1]),
        "atr_pct": num(a.iloc[-1] / price * 100, 2),
        "adx": num(adx.iloc[-1], 1),
        "plus_di": num(pdi.iloc[-1], 1),
        "minus_di": num(mdi.iloc[-1], 1),
        "stoch_rsi_k": num(k.iloc[-1], 1),
        "stoch_rsi_d": num(d.iloc[-1], 1),
        "volume_zscore": num(vol_z.iloc[-1], 2),
        "volume_zscore_avg3": num(vol_z.tail(3).mean(), 2),
        "obv_flow_10": num(obv_flow, 2),
        "dist_vwap24_pct": pct(price, vwap.iloc[-1]),
        "return_1bar_pct": num(rets.iloc[-1] * 100, 2),
        "return_5bars_pct": pct(price, c.iloc[-6]),
        "return_20bars_pct": pct(price, c.iloc[-21]),
        "realized_vol_20_pct": num(rets.tail(20).std() * 100, 2),
        "high_20": price_fmt(high20),
        "low_20": price_fmt(low20),
        "dist_high20_pct": pct(price, high20),
        "dist_low20_pct": pct(price, low20),
        "supports": [price_fmt(x) for x in supports],
        "resistances": [price_fmt(x) for x in resistances],
    }
    s["rsi_divergence"] = ind.rsi_divergence(df)
    s["candle_patterns"] = ind.candle_patterns(df)
    s["heuristic_score"] = heuristic_score(s)
    return s


# ============================================================ setups quantitativos
SETUP_LABELS = {
    "trend_pullback": "Pullback em tendência",
    "breakout": "Rompimento com volume",
    "momentum": "Momentum forte",
    "oversold_bounce": "Ressalto de sobrevenda",
    "squeeze_breakout": "Saída de compressão",
    "pullback_limit": "Compra no recuo (pendente)",
    "breakout_stop": "Compra no rompimento (pendente)",
}
# vendas a descoberto (CFDs): os mesmos setups detetados no gráfico "espelhado" (subidas viram descidas)
SHORT_SETUPS = {
    "trend_pullback": ("rally_short", "Venda no repique (tendência de baixa)"),
    "breakout": ("breakdown_short", "Quebra de suporte com volume"),
    "momentum": ("momentum_short", "Momentum de queda"),
    "oversold_bounce": ("overbought_fade", "Venda de sobrecompra"),
    "squeeze_breakout": ("squeeze_breakdown", "Quebra de compressão para baixo"),
    "pullback_limit": ("rally_limit_short", "Venda no repique (pendente)"),
    "breakout_stop": ("breakdown_stop_short", "Venda na quebra (pendente)"),
}
SHORT_ORDER = {"BUY": "SHORT", "BUY_LIMIT": "SHORT_LIMIT", "BUY_STOP": "SHORT_STOP"}
SHORT_NOTES = {"comprar se recuar até à EMA20": "vender se repicar até à EMA20",
               "comprar se romper a resistência": "vender se quebrar o suporte"}
SETUP_LABELS.update({name: label for name, label in SHORT_SETUPS.values()})


def _bounded_stop(price: float, stop: float, atr: float) -> float:
    """Mantém o stop entre STOP_MIN_ATR e 3.5 ATR do preço de entrada."""
    stop = min(stop, price - config.STOP_MIN_ATR * atr)
    return max(stop, price - 3.5 * atr)


def _plan(name, score, entry, stop, target, atr, order="BUY", notes=""):
    stop = _bounded_stop(entry, stop, atr)
    risk = entry - stop
    # alvo mínimo com folga para as comissões (o gestor de risco mede o R:R já com custos)
    target = max(target, entry + max(2.0, config.MIN_RISK_REWARD + 0.5) * risk)
    return {
        "setup": name, "label": SETUP_LABELS[name], "score": int(max(0, min(100, score))), "order": order,
        "entry": price_fmt(entry), "stop": price_fmt(stop), "target": price_fmt(target),
        "risk_reward": round((target - entry) / risk, 2) if risk > 0 else None, "notes": notes,
    }


def _mirror(df: pd.DataFrame) -> tuple[pd.DataFrame, float]:
    """Gráfico espelhado (preço' = K - preço): uma descida passa a ser uma subida com os mesmos ATR/volume."""
    k = 2 * float(df["high"].max())
    m = df.copy()
    m["open"], m["close"] = k - df["open"], k - df["close"]
    m["high"], m["low"] = k - df["low"], k - df["high"]
    return m, k


def detect_setups(df: pd.DataFrame, higher: pd.DataFrame | None = None, allow_short: bool = False) -> dict:
    """Setups de compra (e, com allow_short, de venda a descoberto) com planos entrada/stop/alvo e pontuação 0-100."""
    res = _long_setups(df, higher)
    if not allow_short or len(df) < 60:
        return res
    mdf, k = _mirror(df)
    mh = _mirror(higher)[0] if higher is not None and len(higher) > 60 else None
    mres = _long_setups(mdf, mh)
    shorts = []
    for st in mres["setups"]:
        name, label = SHORT_SETUPS[st["setup"]]
        shorts.append({**st, "setup": name, "label": label, "order": SHORT_ORDER[st["order"]],
                       "entry": price_fmt(k - st["entry"]), "stop": price_fmt(k - st["stop"]),
                       "target": price_fmt(k - st["target"]), "notes": SHORT_NOTES.get(st["notes"], st["notes"])})
    both = sorted(res["setups"] + shorts, key=lambda x: -x["score"])[:3]
    return {**res, "score": max(res["score"], mres["score"]), "setups": both,
            "short_score": mres["score"], "long_score": res["score"]}


def _long_setups(df: pd.DataFrame, higher: pd.DataFrame | None = None) -> dict:
    """Procura setups de compra conhecidos e devolve planos (entrada/stop/alvo) e uma pontuação 0-100."""
    c, o = df["close"], df["open"]
    price = float(c.iloc[-1])
    e20, e50, e200 = ind.ema(c, 20), ind.ema(c, 50), ind.ema(c, 200)
    r = ind.rsi(c)
    _, _, hist = ind.macd(c)
    mid, upper, lower = ind.bollinger(c)
    atr = float(ind.atr(df).iloc[-1])
    adx, pdi, mdi = ind.adx(df)
    vol_z = (df["volume"] - df["volume"].rolling(20).mean()) / df["volume"].rolling(20).std()
    width = ((upper - lower) / mid)
    if not atr or math.isnan(atr):
        return {"score": 0, "setups": [], "higher_trend": None}

    trend = trend_label(price, e20.iloc[-1], e50.iloc[-1], e200.iloc[-1])
    if higher is not None and len(higher) > 60:
        hc = higher["close"]
        h_trend = trend_label(float(hc.iloc[-1]), ind.ema(hc, 20).iloc[-1], ind.ema(hc, 50).iloc[-1], ind.ema(hc, 200).iloc[-1])
    else:
        h_trend = trend
    h_up, h_down = h_trend in ("up", "strong_up"), h_trend in ("down", "strong_down")
    div = ind.rsi_divergence(df)
    patterns = ind.candle_patterns(df)
    bull_candle = c.iloc[-1] > o.iloc[-1]
    rsi_now, rsi_2 = float(r.iloc[-1]), float(r.iloc[-3])
    vz = float(vol_z.iloc[-1]) if not math.isnan(vol_z.iloc[-1]) else 0.0
    swing_low5 = float(df["low"].tail(5).min())
    high20_prev = float(df["high"].iloc[-21:-1].max())
    _, res = ind.swing_levels(df)
    next_res = res[0] if res else None
    setups = []

    # 1. Pullback numa tendência de alta
    near_ema = e50.iloc[-1] - 0.5 * atr <= price <= e20.iloc[-1] + 0.8 * atr
    if h_up and e20.iloc[-1] > e50.iloc[-1] and price > e50.iloc[-1] - 0.5 * atr and near_ema \
            and 36 <= rsi_now <= 58 and rsi_now > rsi_2 and bull_candle:
        score = 58 + (8 if h_trend == "strong_up" else 0) + (6 if hist.iloc[-1] > hist.iloc[-2] else 0) \
            + (6 if vz > 0 else 0) + (8 if "bullish_engulfing" in patterns or "hammer" in patterns else 0)
        stop = min(swing_low5, float(e50.iloc[-1])) - 0.3 * atr
        setups.append(_plan("trend_pullback", score, price, stop, float(df["high"].tail(20).max()), atr))

    # 2. Rompimento do máximo de 20 velas com volume
    if price > high20_prev and vz > 0.8 and rsi_now < 78:
        score = 56 + (10 if h_up else 0) - (15 if h_down else 0) + (8 if vz > 2 else 0) \
            + (5 if adx.iloc[-1] > adx.iloc[-3] else 0)
        setups.append(_plan("breakout", score, price, high20_prev - 1.0 * atr, price + 3.0 * atr, atr))

    # 3. Momentum de continuação
    if trend == "strong_up" and hist.iloc[-1] > 0 and hist.iloc[-1] > hist.iloc[-2] and 55 <= rsi_now <= 72 \
            and price - e20.iloc[-1] < 2 * atr:
        score = 52 + (10 if h_up else 0) + (6 if vz > 0.5 else 0)
        setups.append(_plan("momentum", score, price, float(e20.iloc[-1]) - 0.6 * atr, price + 3.0 * atr, atr))

    # 4. Ressalto de sobrevenda / reversão
    if float(r.tail(6).min()) < 32 and rsi_now > float(r.iloc[-2]) and bull_candle:
        score = 44 + (16 if div and div["type"] == "bullish" else 0) \
            + (10 if {"bullish_engulfing", "hammer"} & set(patterns) else 0) - (10 if h_trend == "strong_down" else 0)
        stop = float(df["low"].tail(6).min()) - 0.3 * atr
        setups.append(_plan("oversold_bounce", score, price, stop, float(mid.iloc[-1]) + atr, atr))

    # 5. Saída de compressão das bandas de Bollinger
    if len(width) > 130 and width.iloc[-130:-1].rank(pct=True).iloc[-1] < 0.2 and price > upper.iloc[-1] and bull_candle:
        score = 55 + (10 if h_up else 0) - (12 if h_down else 0) + (6 if vz > 1 else 0)
        setups.append(_plan("squeeze_breakout", score, price, float(mid.iloc[-1]) - 0.3 * atr, price + 3.0 * atr, atr))

    # Ordens pendentes sugeridas
    if h_up and e20.iloc[-1] > e50.iloc[-1] and price - e20.iloc[-1] > 0.8 * atr and rsi_now > 55:
        entry = float(e20.iloc[-1]) + 0.1 * atr
        setups.append(_plan("pullback_limit", 50 + (8 if h_trend == "strong_up" else 0), entry,
                            min(float(e50.iloc[-1]), entry - 1.5 * atr), float(df["high"].tail(20).max()) + atr, atr,
                            order="BUY_LIMIT", notes="comprar se recuar até à EMA20"))
    if (h_up or trend in ("up", "sideways")) and next_res and 0 < next_res - price < 1.2 * atr and rsi_now < 70:
        entry = float(next_res) + 0.15 * atr
        setups.append(_plan("breakout_stop", 48 + (10 if h_up else 0), entry, price - 1.2 * atr, entry + 3 * atr, atr,
                            order="BUY_STOP", notes="comprar se romper a resistência"))

    setups.sort(key=lambda s: -s["score"])
    base = max(0, (heuristic_score(timeframe_summary(df)) + 100) // 4)  # 0-50 sem setup
    score = max([s["score"] for s in setups] + [base])
    if h_down and not any(s["setup"] == "oversold_bounce" for s in setups):
        score -= 10
    return {"score": int(max(0, min(100, score))), "setups": setups[:3], "higher_trend": h_trend,
            "divergence": div, "patterns": patterns}


def recent_candles(df: pd.DataFrame, n: int, anonymize: bool = False) -> list[str]:
    vol_avg = df["volume"].rolling(20).mean()
    tail = df.tail(n)
    out = []
    for i, (ts, row) in enumerate(tail.iterrows()):
        avg = vol_avg.loc[ts]
        rel = row["volume"] / avg if avg and not math.isnan(avg) else float("nan")
        label = f"t-{len(tail) - 1 - i}" if anonymize else f"{ts:%m-%d %H:%M}"
        out.append(
            f"{label} O{price_fmt(row['open'])} H{price_fmt(row['high'])} "
            f"L{price_fmt(row['low'])} C{price_fmt(row['close'])} V{rel:.1f}x"
        )
    return out


def btc_context(frames: dict) -> dict:
    """Resumo do BTC para contexto quando se analisa uma altcoin."""
    out = {}
    for tf, df in frames.items():
        s = timeframe_summary(df)
        out[tf] = {k: s[k] for k in ("trend", "rsi14", "macd_hist_rising", "return_20bars_pct", "heuristic_score")}
    return out


def rules_context(price: float, atr: float) -> dict:
    cfd = config.MARKET == "cfd"
    out = {
        "long_only_spot": not cfd,
        "quote_currency": config.account_currency(),
        "trading_profile": config.PROFILE,
        "pending_order_validity_candles": config.PENDING_ORDER_CANDLES,
        "min_confidence_to_buy": config.MIN_CONFIDENCE,
        "min_risk_reward_after_costs": config.MIN_RISK_REWARD,
        "stop_distance_atr_range": [config.STOP_MIN_ATR, config.STOP_MAX_ATR],
        "valid_stop_loss_price_range": [
            price_fmt(price - config.STOP_MAX_ATR * atr),
            price_fmt(price - config.STOP_MIN_ATR * atr),
        ],
        "round_trip_cost_pct": round(2 * (config.FEE_PCT + config.SLIPPAGE_PCT), 3),
    }
    if cfd:
        out["short_selling_allowed"] = bool(config.CFD_ALLOW_SHORT)
        if config.CFD_ALLOW_SHORT:
            out["valid_stop_loss_price_range_short"] = [price_fmt(price + config.STOP_MIN_ATR * atr),
                                                        price_fmt(price + config.STOP_MAX_ATR * atr)]
        out["positions_closed_before_weekend"] = bool(config.CFD_CLOSE_BEFORE_WEEKEND)
    return out


def build_context(
    symbol: str,
    frames: dict,
    price: float,
    timestamp: str | None,
    market: dict | None = None,
    news: dict | None = None,
    btc: dict | None = None,
    position: dict | None = None,
    portfolio: dict | None = None,
    recent_decisions: list | None = None,
    recent_trades: list | None = None,
    anonymize: bool = False,
    pending_order: dict | None = None,
    macro: dict | None = None,
    btc_frame: pd.DataFrame | None = None,
    setups: dict | None = None,
) -> dict:
    primary = frames[config.PRIMARY_TIMEFRAME]
    atr = float(ind.atr(primary).iloc[-1])
    order = [config.PRIMARY_TIMEFRAME] + [tf for tf in config.CONTEXT_TIMEFRAMES if tf in frames]
    higher = frames.get(config.CONTEXT_TIMEFRAMES[1]) if len(config.CONTEXT_TIMEFRAMES) > 1 else None
    if setups is None:
        setups = detect_setups(primary, higher, allow_short=config.MARKET == "cfd" and config.CFD_ALLOW_SHORT)
    rel = None
    if btc_frame is not None and len(btc_frame) > 21 and symbol != config.benchmark():
        mine = pct(price, primary["close"].iloc[-21])
        theirs = pct(btc_frame["close"].iloc[-1], btc_frame["close"].iloc[-21])
        if mine is not None and theirs is not None:
            rel = round(mine - theirs, 2)
    ctx = {
        "timestamp_utc": timestamp,
        "symbol": symbol,
        "current_price": price_fmt(price),
        "primary_timeframe": config.PRIMARY_TIMEFRAME,
        "quant_setups": setups,
        "relative_strength_vs_btc_20bars_pct": rel,
        "timeframes": {tf: timeframe_summary(frames[tf]) for tf in order},
        f"last_{config.RECENT_CANDLES_FOR_AI}_candles_{config.PRIMARY_TIMEFRAME}": recent_candles(
            primary, config.RECENT_CANDLES_FOR_AI, anonymize
        ),
        "market": market or {},
        "macro": macro,
        "btc_context": btc,
        "news": news,
        "position": position,
        "pending_order": pending_order,
        "portfolio": portfolio,
        "recent_decisions": recent_decisions or [],
        "recent_trades": recent_trades or [],
        "rules": rules_context(price, atr),
    }
    if anonymize:
        ctx.pop("timestamp_utc")
    return ctx
