"""Execução de ordens: simulada (paper) ou numa exchange real/testnet via ccxt."""
import logging
import re
import time
import uuid
from dataclasses import dataclass

import ccxt
import requests

import config
from .market import retry

log = logging.getLogger("bot")


@dataclass
class Fill:
    qty: float          # quantidade (moeda base) que fica na posição / que foi vendida
    price: float        # preço médio de execução
    cash_delta: float   # variação do saldo na moeda da conta (negativa numa compra)
    fee: float          # comissão estimada na moeda da conta
    order_id: str = ""  # em CFDs: id da posição na corretora
    margin: float = 0.0          # CFDs: margem reservada pela posição
    stop: float | None = None    # CFDs: stop-loss colocado na corretora
    swap: float = 0.0            # CFDs: financiamento pago/recebido (no fecho)


class OrderError(Exception):
    pass


class PositionGone(Exception):
    """A posição já não existe na conta (vendida manualmente ou resto demasiado pequeno)."""


class PaperBroker:
    name = "paper"

    def __init__(self, fee_pct: float | None = None, slippage_pct: float | None = None, fx=None, min_order: float = 5.0):
        fee = config.FEE_PCT if fee_pct is None else fee_pct
        self.fee_for = fee if callable(fee) else (lambda symbol, f=fee: f)  # % por símbolo
        self.slip = (config.SLIPPAGE_PCT if slippage_pct is None else slippage_pct) / 100
        self.fx = fx or (lambda symbol: 1.0)   # moeda do instrumento -> moeda da conta
        self.min_order = min_order

    def available_quote(self):
        return None  # o saldo é o do estado interno

    def min_order_value(self, symbol: str) -> float:
        return self.min_order

    def buy(self, symbol: str, qty: float, price: float) -> Fill:
        px = price * (1 + self.slip)
        value = qty * px * self.fx(symbol)
        fee = value * self.fee_for(symbol) / 100
        return Fill(qty, px, -(value + fee), fee, f"paper-{uuid.uuid4().hex[:10]}")

    def sell(self, symbol: str, qty: float, price: float) -> Fill:
        px = price * (1 - self.slip)
        value = qty * px * self.fx(symbol)
        fee = value * self.fee_for(symbol) / 100
        return Fill(qty, px, value - fee, fee, f"paper-{uuid.uuid4().hex[:10]}")


class CfdPaperBroker:
    """CFDs simulados (Modo Teste): compra ou venda a descoberto com margem, spread e comissão.
    Numa CFD não se paga o valor todo: fica reservada a margem (ex.: 5% no ouro) e o lucro/prejuízo é
    quantidade x variação do preço."""
    name = "paper"

    def __init__(self, spec, fx, fee_pct: float | None = None):
        self.spec = spec                 # símbolo -> {min_qty, step, margin_rate, spread, quote}
        self.fx = fx                     # moeda de cotação -> moeda da conta
        self.fee_pct = config.CFD_FEE_PCT if fee_pct is None else fee_pct

    def available_quote(self):
        return None

    def min_order_value(self, symbol: str) -> float:
        return 0.0

    def open(self, symbol: str, side: str, qty: float, price: float, stop: float | None = None,
             take_profit: float | None = None) -> Fill:
        sp = self.spec(symbol)
        s = 1 if side == "long" else -1
        px = price + s * sp.get("spread", 0) / 2  # compra no ask, venda no bid
        step = sp.get("step") or 0
        if step:
            qty = round(int(qty / step + 1e-9) * step, 8)
        if qty < sp.get("min_qty", 0) - 1e-12 or qty <= 0:
            raise OrderError(f"{symbol}: quantidade {qty:g} abaixo do mínimo ({sp.get('min_qty')})")
        fx = self.fx(symbol)
        notional = qty * px * fx
        margin = notional * sp["margin_rate"]
        fee = notional * self.fee_pct / 100
        return Fill(qty, px, -(margin + fee), fee, f"paper-{uuid.uuid4().hex[:10]}", margin=margin, stop=stop)

    def close(self, symbol: str, pos: dict, qty: float, price: float) -> Fill:
        sp = self.spec(symbol)
        s = 1 if pos.get("side", "long") == "long" else -1
        px = price - s * sp.get("spread", 0) / 2
        qty = min(qty, pos["qty"])
        fx = self.fx(symbol)
        gross = s * qty * (px - pos["entry_price"]) * fx
        fee = qty * px * fx * self.fee_pct / 100
        margin_part = pos.get("margin", 0.0) * qty / pos["qty"]
        return Fill(qty, px, margin_part + gross - fee, fee, f"paper-{uuid.uuid4().hex[:10]}")

    def recover_close(self, symbol: str, pos: dict):
        return None

    def gone(self, positions: dict) -> list:
        return []

    def amend(self, symbol: str, pos: dict) -> bool:
        return True


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


def public_ip() -> str:
    """IP público desta ligação à internet (o que a Binance vê, não o 127.0.0.1 da aplicação)."""
    try:
        return requests.get("https://api.ipify.org", timeout=5).text.strip()
    except requests.RequestException:
        return ""


def friendly_error(e: Exception) -> str:
    if isinstance(e, ccxt.AuthenticationError):
        seen = re.search(r"request ip:\s*([0-9a-fA-F.:]+)", str(e))  # a Binance diz de que IP veio o pedido
        if seen:
            return (f"A Binance recusou o pedido vindo do IP {seen.group(1)}. Se a chave tem restrição de IP, "
                    "acrescenta este IP à lista de IPs de confiança; senão confirma a API Key e o Secret.")
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
        # o que existe na conta (para explicar porque o saldo em USDC pode estar a zero)
        spot = {c: float(v) for c, v in (bal.get("total") or {}).items() if v and float(v) > 1e-8}
        result["spot_balances"] = dict(sorted(spot.items(), key=lambda kv: -kv[1])[:8])
        if not sandbox:
            try:
                fund = ex.fetch_balance({"type": "funding"})
                result["funding_balances"] = {c: float(v) for c, v in (fund.get("total") or {}).items()
                                              if v and float(v) > 1e-8}
            except Exception as e:
                log.debug("Carteira de financiamento indisponível: %s", e)
    except Exception as e:
        msg = str(e)  # a resposta da Binance não inclui a chave nem o segredo
        log.warning("A Binance%s recusou a verificação das chaves: %s", " Testnet" if sandbox else "", msg[:300])
        result["problems"].append(friendly_error(e))
        seen = re.search(r"request ip:\s*([0-9a-fA-F.:]+)", msg)
        result["public_ip"] = seen.group(1) if seen else public_ip()
        result["auth_failed"] = isinstance(e, (ccxt.AuthenticationError, ccxt.PermissionDenied))
        code = re.search(r'"code"\s*:\s*(-?\d+)', msg)
        result["error_code"] = code.group(1) if code else None
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
            result["public_ip"] = public_ip()  # a Binance só deixa ativar o trading em chaves com IP restrito
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
