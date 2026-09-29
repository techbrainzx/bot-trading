"""
Backtest: repete a estratégia completa (IA + gestão de risco) sobre dados históricos.

- Sem olhar para o futuro: em cada ponto a IA só vê velas já fechadas.
- Datas escondidas da IA (reduz o risco de ela "lembrar-se" do que aconteceu).
- As decisões ficam em cache: repetir o mesmo backtest não volta a gastar tokens.
- Sem notícias (a pesquisa web não é histórica) nem livro de ordens.
"""
import hashlib
import json
import logging
import math
from datetime import datetime, timezone

import ccxt
import numpy as np
import pandas as pd
from openai import OpenAI

import config
from . import indicators as ind
from .analysis import btc_context, build_context
from .brain import Decision, TradingBrain
from .broker import PaperBroker
from .market import MarketData
from .risk import RiskManager
from .state import State
from .trader import Trader

log = logging.getLogger("bot")


class DecisionCache:
    def __init__(self, path):
        self.path = path
        self.data = json.loads(path.read_text("utf-8")) if path.exists() else {}

    @staticmethod
    def key(*parts) -> str:
        return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()

    def get(self, k):
        d = self.data.get(k)
        return Decision.from_dict(d) if d else None

    def put(self, k, d: Decision):
        self.data[k] = d.to_dict()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False), "utf-8")


def _slicer(df: pd.DataFrame, tf_ms: int):
    ends = (df["ts"] + tf_ms).to_numpy()

    def upto(close_ms: int) -> pd.DataFrame:
        i = int(np.searchsorted(ends, close_ms, side="right"))
        return df.iloc[max(0, i - config.CANDLES):i]
    return upto


def run_backtest(symbol: str, days: int, step: int, model: str, effort: str, votes: int, assume_yes: bool):
    md = MarketData(config.EXCHANGE)
    p_tf = config.PRIMARY_TIMEFRAME
    p_ms = md.tf_ms(p_tf)
    timeframes = [p_tf] + config.CONTEXT_TIMEFRAMES
    end_ms = (md.ex.milliseconds() // p_ms) * p_ms
    start_ms = end_ms - days * 86_400_000

    print(f"A descarregar dados de {symbol} ({days} dias + aquecimento)...")
    slicers, full = {}, {}
    for tf in timeframes:
        tf_ms = md.tf_ms(tf)
        full[tf] = md.ohlcv_range(symbol, tf, start_ms - config.CANDLES * tf_ms, end_ms)
        slicers[tf] = _slicer(full[tf], tf_ms)
    btc_slicers = {}
    if not symbol.startswith("BTC/"):
        for tf in (p_tf, "4h"):
            tf_ms = md.tf_ms(tf)
            btc_slicers[tf] = _slicer(md.ohlcv_range(f"BTC/{symbol.split('/')[1]}", tf, start_ms - config.CANDLES * tf_ms, end_ms), tf_ms)
    fng = md.fear_greed_history()

    primary = full[p_tf]
    test_rows = [i for i, ts in enumerate(primary["ts"]) if ts >= start_ms]
    if not test_rows:
        raise SystemExit("Sem dados para o período pedido.")
    decision_rows = set(test_rows[::step])

    client = OpenAI(api_key=config.OPENAI_API_KEY, max_retries=3, timeout=300)
    usage = {"calls": 0, "input": 0, "output": 0}

    def on_usage(_m, i, o):
        usage["calls"] += 1
        usage["input"] += i
        usage["output"] += o

    brain = TradingBrain(client, model, effort, votes, on_usage=on_usage)
    cache = DecisionCache(config.DATA_DIR / "backtest_cache.json")

    print(f"Período: {datetime.fromtimestamp(start_ms / 1000, timezone.utc):%Y-%m-%d %H:%M} -> "
          f"{datetime.fromtimestamp(end_ms / 1000, timezone.utc):%Y-%m-%d %H:%M} UTC")
    print(f"Velas: {len(test_rows)} | pontos de decisão: {len(decision_rows)} (a cada {step} velas de {p_tf})")
    print(f"Modelo: {model} ({effort}) x{votes} voto(s) -> até {len(decision_rows) * votes} chamadas à OpenAI "
          f"(as que já estiverem em cache não são repetidas)")
    if not assume_yes and input("Continuar? [s/N] ").strip().lower() not in ("s", "sim", "y", "yes"):
        print("Cancelado.")
        return

    state = State(None, config.PAPER_START_BALANCE)
    trader = Trader(state, PaperBroker(), RiskManager())
    curve, decisions_log = [], []
    counts = {"BUY": 0, "BUY_LIMIT": 0, "BUY_STOP": 0, "SELL": 0, "HOLD": 0}

    for n, i in enumerate(test_rows):
        bar = primary.iloc[i]
        close_ms = int(bar["ts"]) + p_ms
        now = datetime.fromtimestamp(close_ms / 1000, timezone.utc)
        price = float(bar["close"])

        o_, h_, l_ = float(bar["open"]), float(bar["high"]), float(bar["low"])
        trader.check_exit(symbol, o_, h_, l_, now)
        trader.check_pending(symbol, o_, h_, l_, now, {symbol: price})
        prices = {symbol: price}
        trader.update_risk_state(trader.equity(prices), now)

        if i in decision_rows:
            frames = {tf: slicers[tf](close_ms) for tf in timeframes}
            atr = float(ind.atr(frames[p_tf]).iloc[-1])
            day = now.strftime("%Y-%m-%d")
            fg = fng.get(day)
            ctx = build_context(
                symbol, frames, price, None,
                market={"fear_greed": {"value": fg[0], "label": fg[1]} if fg else None},
                btc=btc_context({tf: s(close_ms) for tf, s in btc_slicers.items()}) if btc_slicers else None,
                position=trader.position_context(symbol, price, now),
                pending_order=trader.pending_context(symbol, price),
                portfolio=trader.portfolio_context(prices),
                recent_decisions=[{k: v for k, v in d.items() if k != "time"} for d in trader.recent_decisions(symbol)],
                recent_trades=[{k: v for k, v in t.items() if k != "closed_at"} for t in trader.recent_trades(symbol)],
                anonymize=True,
            )
            k = DecisionCache.key(model, effort, votes, brain.instructions, ctx)
            d = cache.get(k)
            if d is None:
                try:
                    d = brain.decide(ctx)
                    cache.put(k, d)
                except Exception as e:
                    log.error("Falha na IA: %s", e)
                    d = Decision.hold(f"erro: {e}")
                if usage["calls"] % 10 == 0:
                    cache.save()
            counts[d.action] += 1
            outcome = trader.apply_decision(symbol, d, price, atr, None, prices, now)
            state.record_decision(symbol, {"time": now.isoformat(), "price": price, "action": d.action,
                                           "confidence": round(d.confidence, 2), "reasoning": d.reasoning[:300],
                                           "outcome": outcome})
            decisions_log.append({"time": now.isoformat(), "price": price, "action": d.action,
                                  "confidence": d.confidence, "outcome": outcome, "reasoning": d.reasoning})
            print(f"[{n + 1}/{len(test_rows)}] {now:%m-%d %H:%M} {price:.6g} {d.action:<4} "
                  f"conf {d.confidence:.2f} -> {outcome}")

        curve.append((now, trader.equity(prices)))
    cache.save()

    last = primary.iloc[test_rows[-1]]
    if symbol in trader.positions:
        trader.close_position(symbol, "fim_do_backtest", float(last["close"]), curve[-1][0])
        curve[-1] = (curve[-1][0], trader.equity({symbol: float(last["close"])}))

    report(symbol, curve, state.data["trades"], primary, test_rows, counts, usage, decisions_log, p_tf)


def report(symbol, curve, trades, primary, test_rows, counts, usage, decisions_log, p_tf):
    quote = symbol.split("/")[1]
    eq = pd.Series([v for _, v in curve], index=[t for t, _ in curve])
    start_eq = config.PAPER_START_BALANCE
    total = (eq.iloc[-1] / start_eq - 1) * 100
    first_open = float(primary.iloc[test_rows[0]]["open"])
    last_close = float(primary.iloc[test_rows[-1]]["close"])
    bh = (last_close / first_open - 1) * 100
    max_dd = ((eq / eq.cummax()) - 1).min() * 100
    rets = eq.pct_change().dropna()
    bars_per_year = 365 * 86_400 / ccxt.Exchange.parse_timeframe(p_tf)
    sharpe = rets.mean() / rets.std() * math.sqrt(bars_per_year) if rets.std() > 0 else 0.0

    print("\n" + "=" * 64)
    print(f" RESULTADO DO BACKTEST  {symbol}")
    print("=" * 64)
    print(f" Retorno da estratégia : {total:+.2f}%   ({start_eq:,.0f} -> {eq.iloc[-1]:,.2f} {quote})")
    print(f" Comprar e manter      : {bh:+.2f}%")
    print(f" Drawdown máximo       : {max_dd:.2f}%")
    print(f" Sharpe (anualizado)   : {sharpe:.2f}")
    print(f" Decisões              : {counts}")
    if trades:
        pnl = [t["pnl"] for t in trades]
        wins = [p for p in pnl if p > 0]
        losses = [-p for p in pnl if p <= 0]
        pf = sum(wins) / sum(losses) if losses else float("inf")
        print(f" Trades                : {len(trades)} | acerto {len(wins) / len(trades) * 100:.0f}% | "
              f"profit factor {pf:.2f} | R médio {np.mean([t['r_multiple'] for t in trades]):+.2f}")
        for t in trades:
            print(f"   {t['opened_at'][:16]} -> {t['closed_at'][:16]}  {t['entry_price']:.6g} -> "
                  f"{t['exit_price']:.6g}  {t['pnl']:+8.2f} {quote} ({t['r_multiple']:+.2f}R) {t['reason']}")
    else:
        print(" Trades                : 0")
    print(f" Uso OpenAI (novo)     : {usage['calls']} chamadas, {usage['input']:,} in / {usage['output']:,} out tokens")
    print("=" * 64)
    print(" Atenção: poucos trades não têm significado estatístico. Um bom backtest não garante lucro futuro.")

    out = config.LOGS_DIR / "backtests"
    out.mkdir(parents=True, exist_ok=True)
    tag = f"{symbol.replace('/', '')}_{datetime.now():%Y%m%d_%H%M%S}"
    eq.rename("equity").to_csv(out / f"{tag}_equity.csv")
    pd.DataFrame(trades).to_csv(out / f"{tag}_trades.csv", index=False)
    pd.DataFrame(decisions_log).to_csv(out / f"{tag}_decisions.csv", index=False)
    print(f" Ficheiros guardados em {out}")
