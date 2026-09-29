"""Abre/fecha posições e aplica as decisões (partilhado entre o bot e o backtest)."""
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import ccxt

import config
from .analysis import price_fmt
from .broker import PositionGone
from .risk import RiskManager, exit_level, update_trailing

log = logging.getLogger("bot")


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def trade_stats(trades: list) -> dict:
    if not trades:
        return {"count": 0, "wins": 0, "win_rate": None, "pnl": 0.0, "profit_factor": None,
                "avg_r": None, "best": None, "worst": None}
    pnl = [t["pnl"] for t in trades]
    wins = [p for p in pnl if p > 0]
    gross_loss = -sum(p for p in pnl if p <= 0)
    return {
        "count": len(trades),
        "wins": len(wins),
        "win_rate": len(wins) / len(trades) * 100,
        "pnl": sum(pnl),
        "profit_factor": (sum(wins) / gross_loss) if gross_loss else None,
        "avg_r": sum(t["r_multiple"] for t in trades) / len(trades),
        "best": max(pnl),
        "worst": min(pnl),
    }


def performance(trades: list, last_n: int = 60) -> dict:
    """Desempenho recente por tipo de setup (para o bot dar mais peso ao que funciona)."""
    recent = trades[-last_n:]
    by_setup: dict[str, dict] = {}
    for t in recent:
        key = t.get("setup") or "sem_setup"
        b = by_setup.setdefault(key, {"trades": 0, "wins": 0, "sum_r": 0.0, "pnl": 0.0})
        b["trades"] += 1
        b["wins"] += 1 if t["pnl"] > 0 else 0
        b["sum_r"] += t.get("r_multiple") or 0
        b["pnl"] += t["pnl"]
    out = {k: {"trades": v["trades"], "win_rate_pct": round(v["wins"] / v["trades"] * 100),
               "avg_r": round(v["sum_r"] / v["trades"], 2), "pnl": round(v["pnl"], 2)} for k, v in by_setup.items()}
    st = trade_stats(recent)
    overall = {"trades": st["count"], "win_rate_pct": round(st["win_rate"]) if st["win_rate"] is not None else None,
               "avg_r": round(st["avg_r"], 2) if st["avg_r"] is not None else None}
    return {"overall_last_trades": overall, "by_setup": out}


def setup_score_adjust(trades: list, setup: str) -> int:
    """+5 para setups que têm dado lucro, -10 para os que têm dado perda (com pelo menos 4 trades)."""
    rows = [t for t in trades[-60:] if t.get("setup") == setup]
    if len(rows) < 4:
        return 0
    avg_r = sum(t.get("r_multiple") or 0 for t in rows) / len(rows)
    return 5 if avg_r > 0.3 else -10 if avg_r < -0.3 else 0


class Trader:
    def __init__(self, state, broker, risk: RiskManager, journal=None):
        self.state = state
        self.broker = broker
        self.risk = risk
        self.journal = journal

    @property
    def positions(self) -> dict:
        return self.state.data["positions"]

    @property
    def pending(self) -> dict:
        return self.state.data.setdefault("pending", {})

    def equity(self, prices: dict) -> float:
        total = self.state.data["cash"]
        for sym, pos in self.positions.items():
            total += pos["qty"] * prices.get(sym, pos["entry_price"])
        return total

    def update_risk_state(self, equity: float, now: datetime):
        s = self.state.data
        day = now.strftime("%Y-%m-%d")
        if s["day"] != day:
            s["day"] = day
            s["day_start_equity"] = equity
        if equity > s["equity_peak"]:
            s["equity_peak"] = equity
        dd = (equity / s["equity_peak"] - 1) * 100
        if not s["halted"] and dd <= -config.MAX_DRAWDOWN_PCT:
            s["halted"] = True
            s["halt_reason"] = f"drawdown de {dd:.1f}% desde o pico"
            log.error("KILL SWITCH: %s. Novas entradas bloqueadas.", s["halt_reason"])

    # ------------------------------------------------------------------ contexto para a IA
    def position_context(self, symbol: str, price: float, now: datetime) -> dict | None:
        pos = self.positions.get(symbol)
        if not pos:
            return None
        risk_unit = pos["entry_price"] - pos["initial_stop"]
        opened = datetime.fromisoformat(pos["opened_at"])
        return {
            "entry_price": price_fmt(pos["entry_price"]),
            "current_stop_loss": price_fmt(pos["stop"]),
            "initial_stop_loss": price_fmt(pos["initial_stop"]),
            "take_profit": price_fmt(pos["take_profit"]),
            "highest_since_entry": price_fmt(pos["highest"]),
            "hours_open": round((now - opened).total_seconds() / 3600, 1),
            "unrealized_pnl_pct": round((price / pos["entry_price"] - 1) * 100, 2),
            "r_multiple_now": round((price - pos["entry_price"]) / risk_unit, 2) if risk_unit > 0 else None,
            "entry_reasoning": pos.get("reasoning", "")[:400],
        }

    def pending_context(self, symbol: str, price: float) -> dict | None:
        o = self.pending.get(symbol)
        if not o:
            return None
        return {
            "type": o["type"], "entry_price": price_fmt(o["trigger"]), "stop_loss": price_fmt(o["stop"]),
            "take_profit": price_fmt(o["take_profit"]), "created_at": o["created"], "expires_at": o["expires"],
            "distance_from_price_pct": round((o["trigger"] / price - 1) * 100, 2),
        }

    def portfolio_context(self, prices: dict) -> dict:
        eq = self.equity(prices)
        s = self.state.data
        return {
            "quote_currency": config.QUOTE,
            "equity": round(eq, 2),
            "cash": round(s["cash"], 2),
            "open_positions": list(self.positions),
            "max_open_positions": config.MAX_OPEN_POSITIONS,
            "daily_pnl_pct": round((eq / (s["day_start_equity"] or eq) - 1) * 100, 2),
            "drawdown_from_peak_pct": round((eq / s["equity_peak"] - 1) * 100, 2),
            "entries_blocked": s["halted"],
        }

    def recent_decisions(self, symbol: str, n: int = 5) -> list:
        hist = [d for d in self.state.data["decisions"].get(symbol, []) if not d.get("skipped")]
        return [{k: d.get(k) for k in ("time", "price", "action", "confidence", "reasoning", "outcome")} for d in hist[-n:]]

    def recent_trades(self, symbol: str, n: int = 5) -> list:
        trades = [t for t in self.state.data["trades"] if t["symbol"] == symbol][-n:]
        return [{k: t[k] for k in ("closed_at", "pnl_pct", "r_multiple", "reason", "hours")} for t in trades]

    # ------------------------------------------------------------------ execução
    def _cash_available(self) -> float:
        cash = self.state.data["cash"]
        live_cash = self.broker.available_quote()
        return min(cash, live_cash) if live_cash is not None else cash

    def _try_entry(self, symbol, d, entry_price, atr, spread_pct, prices, now, setup=None) -> str:
        equity = self.equity(prices)
        blocker = self.risk.entry_blocker(symbol, self.state.data, equity, len(self.positions), now)
        if blocker:
            return f"bloqueado: {blocker}"
        plan, note = self.risk.plan_entry(d, entry_price, atr, equity, self._cash_available(), spread_pct,
                                          self.broker.min_order_value(symbol))
        if plan is None:
            return f"rejeitado pelo gestor de risco: {note}"
        return self.open_position(symbol, plan, d, atr, now, note, setup)

    def apply_decision(self, symbol, d, price, atr, spread_pct, prices, now, setup=None) -> str:
        pos = self.positions.get(symbol)
        pend = self.pending.get(symbol)
        if d.action in ("BUY", "BUY_LIMIT", "BUY_STOP") and pos:
            return "ignorado: já existe posição aberta"

        if d.action == "BUY":
            self.pending.pop(symbol, None)
            return self._try_entry(symbol, d, price, atr, spread_pct, prices, now, setup)

        if d.action in ("BUY_LIMIT", "BUY_STOP"):
            problem = self.risk.check_pending(d, price, atr)
            if problem:
                return f"ordem pendente rejeitada: {problem}"
            plan, note = self.risk.plan_entry(d, d.entry_price, atr, self.equity(prices), self._cash_available(),
                                              None, self.broker.min_order_value(symbol))
            if plan is None:
                return f"ordem pendente rejeitada: {note}"
            tf_s = ccxt.Exchange.parse_timeframe(config.PRIMARY_TIMEFRAME)
            expires = now + timedelta(seconds=tf_s * config.PENDING_ORDER_CANDLES)
            self.pending[symbol] = {
                "symbol": symbol, "type": d.action, "trigger": d.entry_price, "stop": plan.stop,
                "take_profit": plan.take_profit, "confidence": d.confidence, "reasoning": d.reasoning[:500],
                "created": iso(now), "expires": iso(expires), "atr": atr, "setup": setup,
            }
            self.state.save()
            verb = "descer até" if d.action == "BUY_LIMIT" else "subir até"
            log.info("%s: ordem pendente %s a %.6g (stop %.6g, alvo %.6g)", symbol, d.action, d.entry_price,
                     plan.stop, plan.take_profit)
            return f"ordem pendente: comprar se o preço {verb} {d.entry_price:.6g} (stop {plan.stop:.6g}, alvo {plan.take_profit:.6g})"

        if d.action == "SELL":
            if pos:
                if d.confidence < config.MIN_EXIT_CONFIDENCE:
                    return f"ignorado: confiança de saída baixa ({d.confidence:.2f})"
                trade = self.close_position(symbol, "sinal_ia", price, now)
                return f"posição fechada (PnL {trade['pnl']:+.2f} {config.QUOTE})" if trade else "posição já não existia"
            if pend:
                self.cancel_pending(symbol, "cancelada pela IA")
                return "ordem pendente cancelada"
            return "ignorado: sem posição (spot, só compras)"

        if pos and d.new_stop_loss:
            new = self.risk.validate_stop_update(pos, d.new_stop_loss, price, atr)
            if new:
                old = pos["stop"]
                pos["stop"] = new
                self.state.save()
                log.info("%s: IA subiu o stop %.6g -> %.6g", symbol, old, new)
                return f"stop subido {old:.6g} -> {new:.6g}"
            return f"novo stop {d.new_stop_loss:.6g} recusado (tem de subir e ficar afastado do preço)"
        return "sem ação (ordem pendente mantida)" if pend else "sem ação"

    def cancel_pending(self, symbol: str, reason: str):
        if self.pending.pop(symbol, None):
            self.state.save()
            log.info("%s: ordem pendente removida (%s)", symbol, reason)

    def check_pending(self, symbol, open_, high, low, now, prices) -> str | None:
        """Dispara ordens pendentes. Em tempo real usa open=high=low=preço atual."""
        o = self.pending.get(symbol)
        if not o:
            return None
        if symbol in self.positions:
            self.cancel_pending(symbol, "já existe posição")
            return None
        if now >= datetime.fromisoformat(o["expires"]):
            self.cancel_pending(symbol, "expirou")
            return "expirada"
        trig = o["trigger"]
        if o["type"] == "BUY_LIMIT":
            if low > trig:
                return None
            fill = min(open_, trig)
        else:
            if high < trig:
                return None
            fill = max(open_, trig)
            if fill > trig + 0.5 * o["atr"]:
                self.cancel_pending(symbol, "o preço saltou demasiado acima do disparo")
                return "cancelada"
        if fill <= o["stop"]:
            self.cancel_pending(symbol, "o preço já está abaixo do stop")
            return "cancelada"
        del self.pending[symbol]
        d = SimpleNamespace(action="BUY", confidence=o["confidence"], stop_loss=o["stop"],
                            take_profit=o["take_profit"], reasoning=o["reasoning"])
        outcome = self._try_entry(symbol, d, fill, o["atr"], None, {**prices, symbol: fill}, now, o.get("setup"))
        self.state.save()
        log.info("%s: ordem %s disparada a %.6g -> %s", symbol, o["type"], fill, outcome)
        if self.journal:
            self.journal.event("PENDING_TRIGGERED", symbol, {"type": o["type"], "trigger": trig, "outcome": outcome})
        return outcome

    def open_position(self, symbol, plan, d, atr, now, note="", setup=None) -> str:
        fill = self.broker.buy(symbol, plan.qty, plan.price)
        entry_cost = -fill.cash_delta
        self.state.data["cash"] += fill.cash_delta
        self.positions[symbol] = {
            "symbol": symbol,
            "qty": fill.qty,
            "entry_price": fill.price,
            "entry_cost": entry_cost,
            "stop": plan.stop,
            "initial_stop": plan.stop,
            "take_profit": plan.take_profit,
            "atr_at_entry": atr,
            "highest": fill.price,
            "opened_at": iso(now),
            "risk_amount": max(entry_cost - fill.qty * plan.stop, 1e-9),
            "confidence": d.confidence,
            "reasoning": d.reasoning[:500],
            "order_id": fill.order_id,
            "setup": setup,
        }
        self.state.save()
        log.info(
            "COMPRA %s: %.6g @ %.6g (%.2f %s) | stop %.6g | TP %.6g | R:R %.2f | conf %.2f",
            symbol, fill.qty, fill.price, entry_cost, config.QUOTE, plan.stop, plan.take_profit, plan.risk_reward,
            d.confidence,
        )
        if self.journal:
            self.journal.event("BUY", symbol, {"qty": fill.qty, "price": fill.price, "cost": entry_cost,
                                               "stop": plan.stop, "take_profit": plan.take_profit})
        msg = f"COMPRADO {fill.qty:.6g} @ {fill.price:.6g} (stop {plan.stop:.6g}, TP {plan.take_profit:.6g})"
        return f"{msg} [{note}]" if note else msg

    def close_position(self, symbol, reason, price, now) -> dict | None:
        pos = self.positions[symbol]
        try:
            fill = self.broker.sell(symbol, pos["qty"], price)
        except PositionGone as e:
            log.error("Posição %s removida do registo: %s", symbol, e)
            del self.positions[symbol]
            self.state.save()
            return None
        self.state.data["cash"] += fill.cash_delta
        pnl = fill.cash_delta - pos["entry_cost"]
        opened = datetime.fromisoformat(pos["opened_at"])
        trade = {
            "symbol": symbol,
            "opened_at": pos["opened_at"],
            "closed_at": iso(now),
            "hours": round((now - opened).total_seconds() / 3600, 1),
            "entry_price": pos["entry_price"],
            "exit_price": fill.price,
            "qty": pos["qty"],
            "pnl": round(pnl, 4),
            "pnl_pct": round(pnl / pos["entry_cost"] * 100, 3),
            "r_multiple": round(pnl / pos["risk_amount"], 2),
            "reason": reason,
            "setup": pos.get("setup"),
        }
        del self.positions[symbol]
        self.state.data["trades"].append(trade)
        if pnl < 0 and config.COOLDOWN_AFTER_LOSS_MINUTES:
            self.state.data["cooldowns"][symbol] = iso(now + timedelta(minutes=config.COOLDOWN_AFTER_LOSS_MINUTES))
        self.state.save()
        log.info("VENDA %s (%s): @ %.6g | PnL %+.2f %s (%+.2f%%, %+.2fR)",
                 symbol, reason, fill.price, pnl, config.QUOTE, trade["pnl_pct"], trade["r_multiple"])
        if self.journal:
            self.journal.trade(trade)
        return trade

    def check_exit(self, symbol, open_, high, low, now) -> dict | None:
        """Stop-loss / take-profit / trailing. Em tempo real usa open=high=low=preço atual."""
        pos = self.positions.get(symbol)
        if not pos:
            return None
        hit = exit_level(pos, open_, high, low)
        if hit:
            return self.close_position(symbol, hit[0], hit[1], now)
        if update_trailing(pos, high):
            log.info("%s: stop automático subiu para %.6g", symbol, pos["stop"])
            self.state.save()
        return None
