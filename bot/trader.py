"""Abre/fecha posições e aplica as decisões (partilhado entre o bot e o backtest)."""
import logging
import math
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import ccxt

import config
from . import indicators as ind
from .analysis import price_fmt
from .broker import OrderError, PositionGone
from .risk import SHORT_ACTIONS, RiskManager, exit_level, round_trip_cost, sgn, side_of, update_trailing

log = logging.getLogger("bot")


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def group_trades(trades: list) -> list:
    """Junta as vendas parciais com a venda final do mesmo trade (para estatísticas corretas)."""
    groups: dict[str, dict] = {}
    order = []
    for t in trades:
        key = t.get("trade_id") or f"_{id(t)}"
        g = groups.get(key)
        if g is None:
            g = groups[key] = {**t, "pnl": 0.0, "risk": 0.0, "parts": 0}
            order.append(key)
        g["pnl"] += t["pnl"]
        g["risk"] += t.get("risk_part") or (t["pnl"] / t["r_multiple"] if t.get("r_multiple") else 0) or 0
        g["parts"] += 1
        g["closed_at"], g["reason"], g["exit_price"] = t["closed_at"], t["reason"], t["exit_price"]
    out = []
    for k in order:
        g = groups[k]
        g["r_multiple"] = round(g["pnl"] / g["risk"], 2) if g["risk"] else 0.0
        out.append(g)
    return out


def trade_stats(trades: list) -> dict:
    trades = group_trades(trades)
    if not trades:
        return {"count": 0, "wins": 0, "win_rate": None, "pnl": 0.0, "profit_factor": None,
                "avg_r": None, "best": None, "worst": None, "loss_streak": 0}
    pnl = [t["pnl"] for t in trades]
    wins = [p for p in pnl if p > 0]
    gross_loss = -sum(p for p in pnl if p <= 0)
    streak = 0
    for p in reversed(pnl):
        if p > 0:
            break
        streak += 1
    return {
        "count": len(trades),
        "wins": len(wins),
        "win_rate": len(wins) / len(trades) * 100,
        "pnl": sum(pnl),
        "profit_factor": (sum(wins) / gross_loss) if gross_loss else None,
        "avg_r": sum(t["r_multiple"] for t in trades) / len(trades),
        "best": max(pnl),
        "worst": min(pnl),
        "loss_streak": streak,
    }


def performance(trades: list, last_n: int = 60) -> dict:
    """Desempenho recente por tipo de setup (para o bot dar mais peso ao que funciona)."""
    recent = group_trades(trades)[-last_n:]
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
    st = trade_stats(trades[-last_n * 2:])
    overall = {"trades": st["count"], "win_rate_pct": round(st["win_rate"]) if st["win_rate"] is not None else None,
               "avg_r": round(st["avg_r"], 2) if st["avg_r"] is not None else None, "loss_streak": st["loss_streak"]}
    return {"overall_last_trades": overall, "by_setup": out}


def _setup_rows(trades: list, setup: str) -> list:
    return [t for t in group_trades(trades)[-60:] if t.get("setup") == setup]


def setup_score_adjust(trades: list, setup: str) -> int:
    """+5 para setups que têm dado lucro, -10 para os que têm dado perda (com pelo menos 4 trades)."""
    rows = _setup_rows(trades, setup)
    if len(rows) < 4:
        return 0
    avg_r = sum(t["r_multiple"] for t in rows) / len(rows)
    return 5 if avg_r > 0.3 else -10 if avg_r < -0.3 else 0


def position_value(pos: dict, price: float, fx: float | None = None) -> float:
    """Valor atual da posição na moeda da conta. CFD: margem + lucro/prejuízo por realizar."""
    fx = pos.get("fx", 1.0) if fx is None else fx
    if pos.get("cfd"):
        return pos.get("margin", 0.0) + sgn(side_of(pos)) * pos["qty"] * (price - pos["entry_price"]) * fx
    return pos["qty"] * price * fx


def dynamic_risk_multiplier(trades: list, setup: str | None, regime_state: str | None = None,
                            side: str = "long") -> tuple[float, str]:
    """Ajusta o tamanho do risco: menos depois de perdas seguidas, em setups fracos ou em mercado mau."""
    mult, notes = 1.0, []
    streak = trade_stats(trades)["loss_streak"]
    if streak >= config.LOSS_STREAK_REDUCE:
        mult *= 0.5
        notes.append(f"{streak} perdas seguidas: risco a 50%")
    if setup:
        rows = _setup_rows(trades, setup)
        if len(rows) >= 4:
            avg_r = sum(t["r_multiple"] for t in rows) / len(rows)
            if avg_r < -0.3:
                mult *= 0.6
                notes.append(f"setup com resultados fracos ({avg_r:+.2f}R): risco a 60%")
            elif avg_r > 0.5 and len(rows) >= 6:
                mult *= 1.2
                notes.append(f"setup com bons resultados ({avg_r:+.2f}R): risco a 120%")
    if regime_state == "risk_off" and side == "long":
        mult *= 0.7
        notes.append("mercado desfavorável: risco a 70%")
    return max(0.3, min(1.25, mult)), "; ".join(notes)


class Trader:
    def __init__(self, state, broker, risk: RiskManager, journal=None, currency: str | None = None, cfd: bool = False):
        self.state = state
        self.broker = broker
        self.risk = risk
        self.journal = journal
        self.currency = currency or config.QUOTE
        self.cfd = cfd                               # CFDs: margem, compra e venda a descoberto
        # ganchos definidos pelo motor (valores por defeito servem para cripto e backtests)
        self.fx = lambda symbol: 1.0                 # moeda do instrumento -> moeda da conta
        self.risk_mult = lambda symbol, setup, side="long": (1.0, "")
        self.entry_gate = lambda symbol, now: None   # motivo para NÃO entrar agora (eventos, resultados, bolsa fechada)

    @property
    def positions(self) -> dict:
        return self.state.data["positions"]

    @property
    def pending(self) -> dict:
        return self.state.data.setdefault("pending", {})

    def equity(self, prices: dict) -> float:
        total = self.state.data["cash"]
        for sym, pos in self.positions.items():
            total += position_value(pos, prices.get(sym, pos["entry_price"]), self.fx(sym))
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
        side = side_of(pos)
        s = sgn(side)
        risk_unit = s * (pos["entry_price"] - pos["initial_stop"])
        opened = datetime.fromisoformat(pos["opened_at"])
        best = pos.get("highest") if s > 0 else pos.get("lowest")
        return {
            "side": side,
            "entry_price": price_fmt(pos["entry_price"]),
            "current_stop_loss": price_fmt(pos["stop"]),
            "initial_stop_loss": price_fmt(pos["initial_stop"]),
            "take_profit": price_fmt(pos["take_profit"]),
            ("highest_since_entry" if s > 0 else "lowest_since_entry"): price_fmt(best or pos["entry_price"]),
            "hours_open": round((now - opened).total_seconds() / 3600, 1),
            "unrealized_pnl_pct": round(s * (price / pos["entry_price"] - 1) * 100, 2),
            "r_multiple_now": round(s * (price - pos["entry_price"]) / risk_unit, 2) if risk_unit > 0 else None,
            "partial_profit_taken": bool(pos.get("partial_done")),
            "let_winner_run": bool(pos.get("let_run")),
            "automatic_exit_rules": pos.get("rules_note"),
            "entry_reasoning": pos.get("reasoning", "")[:400],
        }

    def pending_context(self, symbol: str, price: float) -> dict | None:
        o = self.pending.get(symbol)
        if not o:
            return None
        return {
            "type": o["type"], "side": side_of(o), "entry_price": price_fmt(o["trigger"]), "stop_loss": price_fmt(o["stop"]),
            "take_profit": price_fmt(o["take_profit"]), "created_at": o["created"], "expires_at": o["expires"],
            "distance_from_price_pct": round((o["trigger"] / price - 1) * 100, 2),
        }

    def portfolio_context(self, prices: dict) -> dict:
        eq = self.equity(prices)
        s = self.state.data
        return {
            "account_currency": self.currency,
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
        trades = [t for t in group_trades(self.state.data["trades"]) if t["symbol"] == symbol][-n:]
        return [{"closed_at": t["closed_at"], "side": t.get("side", "long"), "pnl": round(t["pnl"], 2),
                 "r_multiple": t["r_multiple"], "reason": t["reason"], "setup": t.get("setup")} for t in trades]

    # ------------------------------------------------------------------ execução
    def _cash_available(self) -> float:
        cash = self.state.data["cash"]
        live_cash = self.broker.available_quote()
        return min(cash, live_cash) if live_cash is not None else cash

    def _plan(self, symbol, d, entry_price, atr, spread_pct, prices, setup, side="long"):
        mult, mult_note = self.risk_mult(symbol, setup, side)
        spec = self.broker.spec(symbol) if self.cfd else None
        plan, note = self.risk.plan_entry(d, entry_price, atr, self.equity(prices), self._cash_available(), spread_pct,
                                          self.broker.min_order_value(symbol), fx=self.fx(symbol), risk_mult=mult,
                                          side=side, spec=spec)
        if plan and mult_note:
            note = "; ".join(x for x in (note, mult_note) if x)
        return plan, note

    def _try_entry(self, symbol, d, entry_price, atr, spread_pct, prices, now, setup=None, side="long") -> str:
        equity = self.equity(prices)
        blocker = self.risk.entry_blocker(symbol, self.state.data, equity, len(self.positions), now) \
            or self.entry_gate(symbol, now)
        if blocker:
            return f"bloqueado: {blocker}"
        plan, note = self._plan(symbol, d, entry_price, atr, spread_pct, prices, setup, side)
        if plan is None:
            return f"rejeitado pelo gestor de risco: {note}"
        try:
            return self.open_position(symbol, plan, d, atr, now, note, setup)
        except OrderError as e:
            log.error("%s: ordem recusada (%s)", symbol, e)
            return f"ordem recusada pela corretora: {e}"

    def apply_decision(self, symbol, d, price, atr, spread_pct, prices, now, setup=None) -> str:
        pos = self.positions.get(symbol)
        pend = self.pending.get(symbol)
        if d.action in ("BUY", "BUY_LIMIT", "BUY_STOP") + SHORT_ACTIONS and pos:
            return "ignorado: já existe posição aberta"
        if d.action in SHORT_ACTIONS and not (self.cfd and config.CFD_ALLOW_SHORT):
            return "ignorado: vendas a descoberto desligadas"
        side = side_of(d.action)

        if d.action in ("BUY", "SHORT"):
            self.pending.pop(symbol, None)
            return self._try_entry(symbol, d, price, atr, spread_pct, prices, now, setup, side)

        if d.action in ("BUY_LIMIT", "BUY_STOP", "SHORT_LIMIT", "SHORT_STOP"):
            problem = self.risk.check_pending(d, price, atr)
            if problem:
                return f"ordem pendente rejeitada: {problem}"
            plan, note = self._plan(symbol, d, d.entry_price, atr, None, prices, setup, side)
            if plan is None:
                return f"ordem pendente rejeitada: {note}"
            tf_s = ccxt.Exchange.parse_timeframe(config.PRIMARY_TIMEFRAME)
            expires = now + timedelta(seconds=tf_s * config.PENDING_ORDER_CANDLES)
            self.pending[symbol] = {
                "symbol": symbol, "type": d.action, "side": side, "trigger": d.entry_price, "stop": plan.stop,
                "take_profit": plan.take_profit, "confidence": d.confidence, "reasoning": d.reasoning[:500],
                "created": iso(now), "expires": iso(expires), "atr": atr, "setup": setup,
            }
            self.state.save()
            verb = {"BUY_LIMIT": "comprar se o preço descer até", "BUY_STOP": "comprar se o preço subir até",
                    "SHORT_LIMIT": "vender a descoberto se o preço subir até",
                    "SHORT_STOP": "vender a descoberto se o preço descer até"}[d.action]
            log.info("%s: ordem pendente %s a %.6g (stop %.6g, alvo %.6g)", symbol, d.action, d.entry_price,
                     plan.stop, plan.take_profit)
            return f"ordem pendente: {verb} {d.entry_price:.6g} (stop {plan.stop:.6g}, alvo {plan.take_profit:.6g})"

        if d.action in ("SELL", "CLOSE"):
            if pos:
                if d.confidence < config.MIN_EXIT_CONFIDENCE:
                    return f"ignorado: confiança de saída baixa ({d.confidence:.2f})"
                trade = self.close_position(symbol, "sinal_ia", price, now)
                return f"posição fechada (PnL {trade['pnl']:+.2f} {self.currency})" if trade else "posição já não existia"
            if pend:
                self.cancel_pending(symbol, "cancelada pela IA")
                return "ordem pendente cancelada"
            return "ignorado: sem posição" + ("" if self.cfd else " (spot, só compras)")

        if pos and d.new_stop_loss:
            new = self.risk.validate_stop_update(pos, d.new_stop_loss, price, atr)
            if new:
                old = pos["stop"]
                pos["stop"] = new
                self.state.save()
                log.info("%s: IA apertou o stop %.6g -> %.6g", symbol, old, new)
                return f"stop apertado {old:.6g} -> {new:.6g}"
            where = "subir" if side_of(pos) == "long" else "descer"
            return f"novo stop {d.new_stop_loss:.6g} recusado (tem de {where} e ficar afastado do preço)"
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
        side = side_of(o)
        s = sgn(side)
        kind = o["type"]
        if kind in ("BUY_LIMIT", "SHORT_STOP"):   # dispara quando o preço DESCE até ao gatilho
            if low > trig:
                return None
            fill = min(open_, trig)
            if kind == "SHORT_STOP" and fill < trig - 0.5 * o["atr"]:
                self.cancel_pending(symbol, "o preço caiu demasiado abaixo do disparo")
                return "cancelada"
        else:                                      # BUY_STOP / SHORT_LIMIT: quando o preço SOBE até ao gatilho
            if high < trig:
                return None
            fill = max(open_, trig)
            if kind == "BUY_STOP" and fill > trig + 0.5 * o["atr"]:
                self.cancel_pending(symbol, "o preço saltou demasiado acima do disparo")
                return "cancelada"
        if s * (fill - o["stop"]) <= 0:
            self.cancel_pending(symbol, "o preço já passou o stop")
            return "cancelada"
        gate = self.entry_gate(symbol, now)
        if gate:
            return None  # espera (ex.: evento macro) sem cancelar; a ordem expira sozinha
        del self.pending[symbol]
        d = SimpleNamespace(action="SHORT" if side == "short" else "BUY", confidence=o["confidence"],
                            stop_loss=o["stop"], take_profit=o["take_profit"], reasoning=o["reasoning"])
        outcome = self._try_entry(symbol, d, fill, o["atr"], None, {**prices, symbol: fill}, now, o.get("setup"), side)
        self.state.save()
        log.info("%s: ordem %s disparada a %.6g -> %s", symbol, o["type"], fill, outcome)
        if self.journal:
            self.journal.event("PENDING_TRIGGERED", symbol, {"type": o["type"], "trigger": trig, "outcome": outcome})
        return outcome

    def open_position(self, symbol, plan, d, atr, now, note="", setup=None) -> str:
        side = getattr(plan, "side", "long")
        s = sgn(side)
        fx = self.fx(symbol)
        if self.cfd:
            fill = self.broker.open(symbol, side, plan.qty, plan.price, plan.stop, plan.take_profit)
        else:
            fill = self.broker.buy(symbol, plan.qty, plan.price)
        entry_cost = -fill.cash_delta
        self.state.data["cash"] += fill.cash_delta
        stop = fill.stop if (self.cfd and fill.stop) else plan.stop
        risk_amount = max(fill.qty * s * (fill.price - stop) * fx + fill.fee, 1e-9)
        pos = {
            "id": uuid.uuid4().hex[:12],
            "symbol": symbol,
            "side": side,
            "qty": fill.qty,
            "entry_price": fill.price,
            "entry_cost": entry_cost,
            "stop": stop,
            "initial_stop": stop,
            "take_profit": plan.take_profit,
            "atr_at_entry": atr,
            "highest": fill.price,
            "lowest": fill.price,
            "opened_at": iso(now),
            "risk_amount": risk_amount,
            "risk_initial": risk_amount,
            "confidence": d.confidence,
            "reasoning": d.reasoning[:500],
            "order_id": fill.order_id,
            "setup": setup,
            "fx": fx,
            "trail_mult": config.TRAIL_ATR_MULT,
        }
        if self.cfd:
            pos.update(cfd=True, margin=fill.margin, position_id=fill.order_id, broker_stop=stop, swap=0.0)
        self.positions[symbol] = pos
        self.state.save()
        verb = "VENDA A DESCOBERTO" if side == "short" else "COMPRA"
        log.info(
            "%s %s: %.6g @ %.6g (%.2f %s%s) | stop %.6g | TP %.6g | R:R %.2f | conf %.2f",
            verb, symbol, fill.qty, fill.price, entry_cost, self.currency, " de margem" if self.cfd else "", stop,
            plan.take_profit, plan.risk_reward, d.confidence,
        )
        if self.journal:
            self.journal.event("SHORT" if side == "short" else "BUY", symbol,
                               {"qty": fill.qty, "price": fill.price, "cost": entry_cost, "stop": stop,
                                "take_profit": plan.take_profit})
        word = "VENDIDO (short)" if side == "short" else "COMPRADO"
        msg = f"{word} {fill.qty:.6g} @ {fill.price:.6g} (stop {stop:.6g}, TP {plan.take_profit:.6g})"
        return f"{msg} [{note}]" if note else msg

    def _record_trade(self, pos, fill, reason, now, cost_part, risk_part, partial) -> dict:
        pnl = fill.cash_delta - cost_part
        opened = datetime.fromisoformat(pos["opened_at"])
        trade = {
            "trade_id": pos.get("id"),
            "symbol": pos["symbol"],
            "side": side_of(pos),
            "opened_at": pos["opened_at"],
            "closed_at": iso(now),
            "hours": round((now - opened).total_seconds() / 3600, 1),
            "entry_price": pos["entry_price"],
            "exit_price": fill.price,
            "qty": fill.qty,
            "pnl": round(pnl, 4),
            "pnl_pct": round(pnl / cost_part * 100, 3) if cost_part else 0.0,
            "r_multiple": round(pnl / risk_part, 2) if risk_part else 0.0,
            "risk_part": round(risk_part, 6),
            "reason": reason,
            "setup": pos.get("setup"),
            "partial": partial,
        }
        self.state.data["trades"].append(trade)
        if self.journal:
            self.journal.trade(trade)
        return trade

    def _exit(self, symbol, pos, qty, price):
        if self.cfd:
            return self.broker.close(symbol, pos, qty, price)
        return self.broker.sell(symbol, qty, price)

    def close_position(self, symbol, reason, price, now) -> dict | None:
        pos = self.positions[symbol]
        try:
            fill = self._exit(symbol, pos, pos["qty"], price)
        except PositionGone as e:
            fill = self.broker.recover_close(symbol, pos) if self.cfd else None
            if fill is None:
                log.error("Posição %s removida do registo: %s", symbol, e)
                del self.positions[symbol]
                self.state.save()
                return None
            reason = self.broker_reason(pos, fill.price)
            log.warning("%s: já tinha sido fechada na corretora (%s); a registar o resultado real.", symbol, reason)
        self.state.data["cash"] += fill.cash_delta
        trade = self._record_trade(pos, fill, reason, now, pos["entry_cost"], pos["risk_amount"], False)
        del self.positions[symbol]
        total = sum(t["pnl"] for t in self.state.data["trades"] if t.get("trade_id") == pos.get("id")) \
            if pos.get("id") else trade["pnl"]
        if total < 0 and config.COOLDOWN_AFTER_LOSS_MINUTES:
            self.state.data["cooldowns"][symbol] = iso(now + timedelta(minutes=config.COOLDOWN_AFTER_LOSS_MINUTES))
        self.state.save()
        log.info("%s %s (%s): @ %.6g | PnL %+.2f %s (%+.2f%%, %+.2fR)",
                 "FECHO" if self.cfd else "VENDA", symbol, reason, fill.price, trade["pnl"], self.currency,
                 trade["pnl_pct"], trade["r_multiple"])
        return trade

    @staticmethod
    def broker_reason(pos, exit_price) -> str:
        """Porque fechou na corretora (stop/alvo), a partir do preço de saída."""
        tol = 0.25 * (pos.get("atr_at_entry") or 0) or exit_price * 0.0005
        if abs(exit_price - pos["stop"]) <= tol:
            return "stop_loss" if sgn(side_of(pos)) * (pos["stop"] - pos["entry_price"]) < 0 else "trailing_stop"
        if pos.get("take_profit") and abs(exit_price - pos["take_profit"]) <= tol:
            return "take_profit"
        return "corretora"

    def sync_broker(self, now) -> list:
        """CFDs no Modo Real: regista as posições que a corretora fechou (stop na corretora, stop-out, fecho
        à mão) e leva para lá o stop atual do bot (break-even, trailing, estrutural)."""
        if not self.cfd:
            return []
        closed = []
        for symbol in self.broker.gone(self.positions):
            pos = self.positions[symbol]
            fill = self.broker.recover_close(symbol, pos)
            if fill is None:
                log.error("%s: a posição já não existe na corretora e o fecho não foi encontrado; removida do registo.",
                          symbol)
                del self.positions[symbol]
                continue
            self.state.data["cash"] += fill.cash_delta
            reason = self.broker_reason(pos, fill.price)
            trade = self._record_trade(pos, fill, reason, now, pos["entry_cost"], pos["risk_amount"], False)
            del self.positions[symbol]
            if trade["pnl"] < 0 and config.COOLDOWN_AFTER_LOSS_MINUTES:
                self.state.data["cooldowns"][symbol] = iso(now + timedelta(minutes=config.COOLDOWN_AFTER_LOSS_MINUTES))
            log.info("FECHO NA CORRETORA %s (%s): @ %.6g | PnL %+.2f %s", symbol, reason, fill.price, trade["pnl"],
                     self.currency)
            closed.append(trade)
        for symbol, pos in self.positions.items():
            if pos.get("broker_stop") is not None and abs(pos["stop"] - pos["broker_stop"]) > 1e-9:
                if self.broker.amend(symbol, pos):
                    pos["broker_stop"] = pos["stop"]
                    log.info("%s: stop atualizado na corretora para %.6g", symbol, pos["stop"])
        self.state.save()
        return closed

    def partial_close(self, symbol, fraction, price, now, reason="parcial") -> dict | None:
        """Fecha uma parte da posição (ex.: 50% ao ganhar 1R) e protege o resto no preço de entrada."""
        pos = self.positions[symbol]
        pos["partial_done"] = True
        s = sgn(side_of(pos))
        qty = pos["qty"] * fraction
        breakeven = pos["entry_price"] + s * round_trip_cost(pos["entry_price"])

        def protect(stop):
            return max(stop, breakeven) if s > 0 else min(stop, breakeven)

        if self.cfd:
            spec = self.broker.spec(symbol)
            step = spec.get("step") or 0
            if step:
                qty = round(math.floor(qty / step + 1e-9) * step, 8)
            min_qty = spec.get("min_qty") or 0
            too_small = qty < min_qty - 1e-12 or pos["qty"] - qty < min_qty - 1e-12
        else:
            min_value = self.broker.min_order_value(symbol)
            remaining_value = (pos["qty"] - qty) * price * self.fx(symbol)
            too_small = qty * price * self.fx(symbol) < min_value or remaining_value < min_value
        if too_small or qty <= 0:
            pos["stop"] = protect(pos["stop"])  # demasiado pequena para dividir: só protege
            self.state.save()
            return None
        try:
            fill = self._exit(symbol, pos, qty, price)
        except (PositionGone, OrderError) as e:
            log.error("Fecho parcial de %s falhou: %s", symbol, e)
            self.state.save()
            return None
        part = fill.qty / pos["qty"]
        cost_part, risk_part = pos["entry_cost"] * part, pos["risk_amount"] * part
        self.state.data["cash"] += fill.cash_delta
        trade = self._record_trade(pos, fill, reason, now, cost_part, risk_part, True)
        if self.cfd:
            pos["margin"] -= pos.get("margin", 0.0) * part
        pos["qty"] -= fill.qty
        pos["entry_cost"] -= cost_part
        pos["risk_amount"] -= risk_part
        pos["stop"] = protect(pos["stop"])
        self.state.save()
        log.info("%s PARCIAL %s: %.6g @ %.6g | PnL %+.2f %s | stop no preço de entrada",
                 "FECHO" if self.cfd else "VENDA", symbol, fill.qty, fill.price, trade["pnl"], self.currency)
        return trade

    def check_exit(self, symbol, open_, high, low, now) -> dict | None:
        """Stop-loss / take-profit / venda parcial / trailing. Em tempo real usa open=high=low=preço atual."""
        pos = self.positions.get(symbol)
        if not pos:
            return None
        hit = exit_level(pos, open_, high, low)
        if hit and hit[0] == "take_profit" and pos.get("let_run"):
            # tendência forte: em vez de vender tudo no alvo, garante metade e deixa o resto correr
            if not pos.get("partial_done") and config.PARTIAL_TP_PCT:
                self.partial_close(symbol, config.PARTIAL_TP_PCT / 100, hit[1], now, "alvo_parcial")
            pos = self.positions.get(symbol)
            if pos:
                pos["take_profit"] = None
                pos["trail_mult"] = min(pos.get("trail_mult", config.TRAIL_ATR_MULT), 1.5)
                log.info("%s: alvo atingido com tendência forte, a deixar correr com trailing apertado", symbol)
                self.state.save()
            hit = None
        if hit:
            return self.close_position(symbol, hit[0], hit[1], now)
        s = sgn(side_of(pos))
        if config.PARTIAL_TP_PCT and not pos.get("partial_done"):
            r = s * (pos["entry_price"] - pos["initial_stop"])
            level = pos["entry_price"] + s * config.PARTIAL_TP_R * r
            if r > 0 and ((s > 0 and high >= level) or (s < 0 and low <= level)):
                fill_at = max(level, open_) if s > 0 else min(level, open_)
                self.partial_close(symbol, config.PARTIAL_TP_PCT / 100, fill_at, now)
                pos = self.positions.get(symbol)
                if not pos:
                    return None
        if update_trailing(pos, high, low):
            log.info("%s: stop automático %s para %.6g", symbol, "subiu" if s > 0 else "desceu", pos["stop"])
            self.state.save()
        return None

    def manage_rules(self, symbol, df, now) -> str | None:
        """Regras dinâmicas por vela (sem IA): trailing adaptado à tendência, stop estrutural,
        'deixar correr' e stop por tempo. Devolve uma nota se algo mudou."""
        pos = self.positions.get(symbol)
        if not pos or df is None or len(df) < 60 or pos.get("setup") == "tendencia":
            return None
        price = float(df["close"].iloc[-1])
        atr = float(ind.atr(df).iloc[-1])
        adx = float(ind.adx(df)[0].iloc[-1])
        _, _, hist = ind.macd(df["close"])
        ema20 = float(ind.ema(df["close"], 20).iloc[-1])
        s = sgn(side_of(pos))
        r = s * (pos["entry_price"] - pos["initial_stop"])
        notes = []

        # 1) trailing adaptado ao regime: largo em tendência forte, apertado em mercado lateral
        base = config.TRAIL_ATR_MULT
        mult = base + 0.5 if adx >= 30 else base - 0.5 if adx < 20 else base
        if pos.get("take_profit") is None:  # já em modo "deixar correr"
            mult = min(mult, 1.5)
        pos["trail_mult"] = mult

        # 2) stop estrutural: para lá do último fundo (compra) ou topo (venda) confirmado, com o trade já a ganhar
        if r > 0 and s * (price - pos["entry_price"]) >= config.TRAIL_START_R * r:
            if s > 0:
                lows = df["low"].tail(40)
                pivots = lows[(lows == lows.rolling(7, center=True).min())].dropna()
                cands = [float(v) for v in pivots if float(v) < price - 0.5 * atr]
                new = cands[-1] - 0.2 * atr if cands else None
            else:
                highs = df["high"].tail(40)
                pivots = highs[(highs == highs.rolling(7, center=True).max())].dropna()
                cands = [float(v) for v in pivots if float(v) > price + 0.5 * atr]
                new = cands[-1] + 0.2 * atr if cands else None
            if new is not None and s * (new - pos["stop"]) > 0:
                notes.append(f"stop estrutural {pos['stop']:.6g} -> {new:.6g}")
                pos["stop"] = new

        # 3) deixar correr os vencedores quando a tendência (a favor da posição) está forte e a acelerar
        if s > 0:
            pos["let_run"] = bool(adx >= 28 and hist.iloc[-1] > hist.iloc[-2] and price > ema20)
        else:
            pos["let_run"] = bool(adx >= 28 and hist.iloc[-1] < hist.iloc[-2] and price < ema20)

        # 4) stop por tempo: capital parado num trade que não anda
        opened = datetime.fromisoformat(pos["opened_at"])
        tf_s = ccxt.Exchange.parse_timeframe(config.PRIMARY_TIMEFRAME)
        candles_open = (now - opened).total_seconds() / tf_s
        if config.TIME_STOP_CANDLES and candles_open >= config.TIME_STOP_CANDLES and r > 0 \
                and s * (price - pos["entry_price"]) / r < 0.3 and not pos.get("partial_done"):
            self.close_position(symbol, "sem_progresso", price, now)
            return f"fechada por falta de progresso ao fim de {candles_open:.0f} velas"

        pos["rules_note"] = f"trailing {mult:.1f} ATR" + (" | deixar correr" if pos["let_run"] else "")
        self.state.save()
        return "; ".join(notes) or None
