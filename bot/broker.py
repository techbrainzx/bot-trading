"""Execução de ordens: simulada (paper) ou numa exchange real/testnet via ccxt."""
import logging
import time
import uuid
from dataclasses import dataclass

import ccxt

import config
from .market import retry

log = logging.getLogger("bot")


@dataclass
class Fill:
    qty: float          # quantidade (moeda base) que fica na posição / que foi vendida
    price: float        # preço médio de execução
    cash_delta: float   # variação de USDT (negativa numa compra)
    fee: float          # comissão estimada em USDT
    order_id: str = ""


class OrderError(Exception):
    pass


class PositionGone(Exception):
    """A posição já não existe na conta (vendida manualmente ou resto demasiado pequeno)."""


class PaperBroker:
    name = "paper"

    def __init__(self, fee_pct: float = config.FEE_PCT, slippage_pct: float = config.SLIPPAGE_PCT):
        self.fee = fee_pct / 100
        self.slip = slippage_pct / 100

    def available_quote(self):
        return None  # o saldo é o do estado interno

    def min_order_value(self, symbol: str) -> float:
        return 5.0  # mínimo típico da Binance spot

    def buy(self, symbol: str, qty: float, price: float) -> Fill:
        px = price * (1 + self.slip)
        fee = qty * px * self.fee
        return Fill(qty, px, -(qty * px + fee), fee, f"paper-{uuid.uuid4().hex[:10]}")

    def sell(self, symbol: str, qty: float, price: float) -> Fill:
        px = price * (1 - self.slip)
        fee = qty * px * self.fee
        return Fill(qty, px, qty * px - fee, fee, f"paper-{uuid.uuid4().hex[:10]}")


def make_exchange(exchange_id: str, api_key: str, secret: str, sandbox: bool):
    ex = getattr(ccxt, exchange_id)({
        "apiKey": api_key,
        "secret": secret,
        "enableRateLimit": True,
        "options": {"defaultType": "spot"},
    })
    if sandbox:
        ex.set_sandbox_mode(True)
    return ex


def friendly_error(e: Exception) -> str:
    if isinstance(e, ccxt.AuthenticationError):
        return "Chaves inválidas ou sem permissão (confirma a API Key, o Secret e as restrições de IP)."
    if isinstance(e, ccxt.PermissionDenied):
        return "A chave não tem as permissões necessárias."
    if isinstance(e, ccxt.NetworkError):
        return "Sem ligação à Binance. Verifica a internet."
    return str(e)[:300]


def verify_keys(exchange_id: str, api_key: str, secret: str, sandbox: bool) -> dict:
    """Testa as chaves: ligação, saldo USDT e permissões (levantamentos devem estar DESLIGADOS)."""
    result = {"ok": False, "usdt_free": None, "withdrawals_enabled": None, "spot_trading_enabled": None,
              "problems": [], "testnet": sandbox}
    if not api_key or not secret:
        result["problems"].append("Faltam a API Key e/ou o Secret.")
        return result
    try:
        ex = make_exchange(exchange_id, api_key, secret, sandbox)
        bal = retry(ex.fetch_balance)
        result["usdt_free"] = float(bal["free"].get(config.QUOTE) or 0)
        result["quote"] = config.QUOTE
    except Exception as e:
        result["problems"].append(friendly_error(e))
        return result
    if not sandbox:
        try:
            r = ex.sapiGetAccountApiRestrictions()
            result["withdrawals_enabled"] = bool(r.get("enableWithdrawals"))
            result["spot_trading_enabled"] = bool(r.get("enableSpotAndMarginTrading"))
        except Exception as e:
            result["problems"].append(f"Não foi possível ler as permissões da chave: {friendly_error(e)}")
            return result
        if result["withdrawals_enabled"]:
            result["problems"].append("Por segurança, desativa a permissão de LEVANTAMENTOS (Enable Withdrawals) "
                                      "desta chave na Binance.")
        if not result["spot_trading_enabled"]:
            result["problems"].append("Ativa a permissão de trading spot (Enable Spot & Margin Trading) nesta chave.")
    result["ok"] = not result["problems"]
    return result


class ExchangeBroker:
    def __init__(self, exchange_id: str, api_key: str, secret: str, sandbox: bool):
        self.name = f"{exchange_id}{' (testnet)' if sandbox else ''}"
        self.ex = make_exchange(exchange_id, api_key, secret, sandbox)
        retry(self.ex.load_markets)
        retry(self.ex.fetch_balance)  # valida as chaves logo no arranque

    def available_quote(self) -> float:
        return float(retry(self.ex.fetch_balance)["free"].get(config.QUOTE) or 0)

    def min_order_value(self, symbol: str) -> float:
        """Mínimo da exchange com 25% de margem (para ainda se conseguir vender se o preço cair)."""
        m = self.ex.market(symbol)
        min_cost = (m["limits"].get("cost") or {}).get("min") or 5.0
        return float(min_cost) * 1.25

    def _round_and_check(self, symbol: str, qty: float, price: float) -> float:
        m = self.ex.market(symbol)
        try:
            q = float(self.ex.amount_to_precision(symbol, qty))
        except ccxt.BaseError:
            q = 0.0
        min_amt = (m["limits"]["amount"] or {}).get("min") or 0
        min_cost = (m["limits"]["cost"] or {}).get("min") or 0
        if q <= 0 or q < min_amt or q * price < min_cost:
            raise OrderError(f"quantidade {qty:.8g} abaixo dos mínimos da exchange ({min_amt} / {min_cost} USDT)")
        return q

    def _fill(self, order: dict, symbol: str, side: str, price_hint: float) -> Fill:
        if not order.get("filled") or not order.get("average"):
            time.sleep(1.5)
            order = retry(lambda: self.ex.fetch_order(order["id"], symbol))
        filled = float(order.get("filled") or 0)
        if filled <= 0:
            raise OrderError(f"ordem {order.get('id')} sem execução (estado {order.get('status')})")
        avg = float(order.get("average") or order.get("price") or price_hint)
        m = self.ex.market(symbol)
        fee_value, base_fee = 0.0, 0.0
        fees = order.get("fees") or ([order["fee"]] if order.get("fee") else [])
        known = False
        for f in fees:
            if not f or f.get("cost") is None:
                continue
            cost, cur = float(f["cost"]), f.get("currency")
            known = True
            if cur == m["quote"]:
                fee_value += cost
            elif cur == m["base"]:
                base_fee += cost
                fee_value += cost * avg
            else:  # ex.: comissões pagas em BNB -> estimativa conservadora
                fee_value += filled * avg * config.FEE_PCT / 100
        if not known:
            fee_value = filled * avg * config.FEE_PCT / 100
        if side == "buy":
            cash_delta = -(filled * avg) - (fee_value - base_fee * avg)
            return Fill(filled - base_fee, avg, cash_delta, fee_value, str(order["id"]))
        return Fill(filled, avg, filled * avg - fee_value, fee_value, str(order["id"]))

    def buy(self, symbol: str, qty: float, price: float) -> Fill:
        q = self._round_and_check(symbol, qty, price)
        # sem retry: repetir uma criação de ordem pode duplicá-la
        order = self.ex.create_order(symbol, "market", "buy", q)
        return self._fill(order, symbol, "buy", price)

    def sell(self, symbol: str, qty: float, price: float) -> Fill:
        base = self.ex.market(symbol)["base"]
        free = float(retry(self.ex.fetch_balance)["free"].get(base) or 0)
        try:
            q = self._round_and_check(symbol, min(qty, free), price)
        except OrderError as e:
            raise PositionGone(f"{symbol}: saldo livre de {base} = {free:.8g}. {e}") from e
        if q < qty * 0.98:
            log.warning("%s: só há %.8g %s livres (posição registada: %.8g)", symbol, free, base, qty)
        order = self.ex.create_order(symbol, "market", "sell", q)
        return self._fill(order, symbol, "sell", price)
