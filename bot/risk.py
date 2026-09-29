"""Gestão de risco: regras fixas que filtram e dimensionam tudo o que a IA propõe."""
from dataclasses import dataclass
from datetime import datetime

import config


@dataclass
class EntryPlan:
    qty: float
    price: float
    stop: float
    take_profit: float
    risk_reward: float
    notional: float


def round_trip_cost(price: float) -> float:
    return price * 2 * (config.FEE_PCT + config.SLIPPAGE_PCT) / 100


class RiskManager:
    def entry_blocker(self, symbol: str, state: dict, equity: float, open_positions: int, now: datetime) -> str | None:
        if state["halted"]:
            return f"bot bloqueado ({state['halt_reason']}). Desbloqueia no painel"
        base = state["day_start_equity"] or equity
        daily = (equity / base - 1) * 100
        if daily <= -config.MAX_DAILY_LOSS_PCT:
            return f"limite de perda diária atingido ({daily:.2f}%)"
        if open_positions >= config.MAX_OPEN_POSITIONS:
            return f"máximo de {config.MAX_OPEN_POSITIONS} posições abertas"
        until = state["cooldowns"].get(symbol)
        if until and now < datetime.fromisoformat(until):
            return f"em pausa após perda até {until}"
        return None

    def plan_entry(self, d, price: float, atr: float, equity: float, cash: float, spread_pct: float | None,
                   min_order: float | None = None, fx: float = 1.0, risk_mult: float = 1.0):
        """Devolve (EntryPlan, nota) ou (None, motivo da rejeição). `price` é o preço de entrada.
        `fx` converte a moeda do instrumento para a moeda da conta (1 em cripto)."""
        notes = []
        min_order = max(config.MIN_ORDER_VALUE, min_order or 0)
        if d.confidence < config.MIN_CONFIDENCE:
            return None, f"confiança {d.confidence:.2f} < {config.MIN_CONFIDENCE}"
        if spread_pct is not None and spread_pct > config.MAX_SPREAD_PCT:
            return None, f"spread de {spread_pct:.3f}% demasiado largo"
        if not atr or atr <= 0:
            return None, "ATR inválido"
        stop = d.stop_loss
        if stop is None or stop >= price:
            return None, "stop-loss em falta ou acima do preço"
        dist = price - stop
        if dist > config.STOP_MAX_ATR * atr:
            return None, f"stop demasiado longe ({dist / atr:.1f} ATR > {config.STOP_MAX_ATR})"
        if dist < config.STOP_MIN_ATR * atr:
            stop = price - config.STOP_MIN_ATR * atr
            notes.append(f"stop alargado para {config.STOP_MIN_ATR} ATR ({stop:.6g})")
        risk_unit = price - stop
        cost = round_trip_cost(price)

        tp = d.take_profit
        if tp is None or tp <= price:
            tp = price + config.DEFAULT_RISK_REWARD * risk_unit + cost
            notes.append(f"take-profit definido por defeito ({tp:.6g})")
        rr = (tp - price - cost) / (risk_unit + cost)
        if rr < config.MIN_RISK_REWARD:
            return None, f"retorno/risco {rr:.2f} < {config.MIN_RISK_REWARD}"

        if config.RISK_MODE == "fixed":
            risk_amount = config.RISK_FIXED_AMOUNT
        else:
            risk_pct = config.RISK_PER_TRADE_PCT
            if config.CONFIDENCE_SIZING:
                # 50% do risco na confiança mínima, 100% a partir de 0.80
                span = max(0.80 - config.MIN_CONFIDENCE, 1e-9)
                risk_pct *= 0.5 + 0.5 * min(1.0, max(0.0, (d.confidence - config.MIN_CONFIDENCE) / span))
            risk_amount = equity * risk_pct / 100
        risk_amount *= risk_mult
        qty = risk_amount / ((risk_unit + cost) * fx)
        max_notional = min(equity * config.MAX_POSITION_PCT / 100, cash * 0.98)
        if qty * price * fx > max_notional:
            qty = max_notional / (price * fx)
            notes.append("tamanho limitado pelo máximo por posição/saldo")
        notional = qty * price * fx
        if notional < min_order:
            # sobe até ao mínimo da exchange se o risco não passar de 2x o pretendido e houver saldo
            needed_risk = min_order / (price * fx) * (risk_unit + cost) * fx
            if needed_risk <= 2 * risk_amount and min_order <= cash * 0.98:
                qty, notional = min_order / (price * fx), min_order
                notes.append(f"ordem subida ao mínimo de {min_order:.2f}")
            else:
                return None, f"ordem de {notional:.2f} abaixo do mínimo de {min_order:.2f}"
        return EntryPlan(qty, price, stop, tp, round(rr, 2), notional), "; ".join(notes)

    def check_pending(self, d, price: float, atr: float) -> str | None:
        """Valida uma ordem pendente proposta pela IA (preço de disparo do lado certo e a distância razoável)."""
        entry = d.entry_price
        if not entry:
            return "falta o preço de entrada da ordem pendente"
        if d.action == "BUY_LIMIT" and entry >= price:
            return "BUY_LIMIT tem de ficar abaixo do preço atual"
        if d.action == "BUY_STOP" and entry <= price:
            return "BUY_STOP tem de ficar acima do preço atual"
        if abs(entry - price) > 4 * atr:
            return f"preço de entrada demasiado longe ({abs(entry - price) / atr:.1f} ATR)"
        return None

    @staticmethod
    def validate_stop_update(pos: dict, new_stop: float | None, price: float, atr: float) -> float | None:
        """A IA só pode SUBIR o stop, e nunca colado ao preço."""
        if not new_stop or new_stop <= pos["stop"]:
            return None
        if new_stop >= price - 0.5 * config.STOP_MIN_ATR * atr:
            return None
        return new_stop


def exit_level(pos: dict, open_: float, high: float, low: float):
    """Verifica stop/take-profit numa vela (ou num preço, com open=high=low). Stop tem prioridade."""
    if low <= pos["stop"]:
        reason = "stop_loss" if pos["stop"] < pos["entry_price"] else "trailing_stop"
        return reason, min(pos["stop"], open_)
    tp = pos.get("take_profit")
    if tp and high >= tp:
        return "take_profit", max(tp, open_)
    return None


def update_trailing(pos: dict, high: float) -> bool:
    """Break-even e trailing stop automáticos. Devolve True se o stop subiu."""
    pos["highest"] = max(pos["highest"], high)
    entry = pos["entry_price"]
    r = entry - pos["initial_stop"]
    if r <= 0:
        return False
    new = pos["stop"]
    if config.BREAKEVEN_AT_R and pos["highest"] >= entry + config.BREAKEVEN_AT_R * r:
        breakeven = entry * (1 + 2 * (config.FEE_PCT + config.SLIPPAGE_PCT) / 100)
        if breakeven < pos["highest"]:
            new = max(new, breakeven)
    mult = pos.get("trail_mult", config.TRAIL_ATR_MULT)
    if mult and pos["highest"] >= entry + config.TRAIL_START_R * r:
        new = max(new, pos["highest"] - mult * pos["atr_at_entry"])
    if new > pos["stop"]:
        pos["stop"] = new
        return True
    return False
