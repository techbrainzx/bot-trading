"""
Corretora Trading 212 (ações e ETFs) via Public API (beta).

- Conta demo: https://demo.trading212.com/api/v0  |  conta real: https://live.trading212.com/api/v0
- Autenticação: HTTP Basic com API Key (utilizador) e API Secret (palavra-passe).
- Só contas Invest/ISA. As ordens são na moeda principal da conta.
- A criação de ordens NÃO é idempotente: nunca repetir automaticamente um pedido de ordem.
"""
import base64
import logging
import math
import time

import requests

import config
from .broker import Fill, OrderError, PositionGone
from .stockdata import exchange_of

log = logging.getLogger("bot")

URLS = {True: "https://demo.trading212.com/api/v0", False: "https://live.trading212.com/api/v0"}
# sufixo dos tickers da Trading 212 por bolsa (ex.: AAPL_US_EQ, SXR8d_EQ para a Xetra)
T212_SUFFIX = {"US": "_US_EQ", "DE": "d_EQ", "L": "l_EQ", "AS": "a_EQ", "PA": "p_EQ", "MI": "m_EQ"}


class T212Error(Exception):
    pass


class Trading212Client:
    def __init__(self, key: str, secret: str, demo: bool):
        self.base = URLS[demo]
        token = base64.b64encode(f"{key}:{secret}".encode()).decode()
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Basic {token}", "Content-Type": "application/json"})

    def request(self, method: str, path: str, retry_get: bool = True, **kw):
        for attempt in range(3):
            r = self.s.request(method, self.base + path, timeout=20, **kw)
            if r.status_code == 429 and method == "GET" and retry_get and attempt < 2:
                reset = float(r.headers.get("x-ratelimit-reset") or 0)
                time.sleep(min(15.0, max(1.0, reset - time.time())))
                continue
            if r.status_code == 404:
                return None
            if r.status_code >= 400:
                try:
                    detail = r.json()
                except ValueError:
                    detail = r.text[:200]
                if r.status_code in (401, 403):
                    raise T212Error("Chaves da Trading 212 inválidas ou sem permissão (confirma a API Key, o Secret "
                                    "e as permissões da chave).")
                raise T212Error(f"Trading 212 respondeu {r.status_code}: {detail}")
            return r.json() if r.content else {}
        raise T212Error("Trading 212: limite de pedidos atingido, tenta mais tarde.")


def verify_t212(key: str, secret: str, demo: bool) -> dict:
    result = {"ok": False, "usdt_free": None, "quote": config.STOCK_CURRENCY, "testnet": demo, "problems": [],
              "withdrawals_enabled": None, "spot_trading_enabled": None}
    if not key or not secret:
        result["problems"].append("Faltam a API Key e/ou o API Secret.")
        return result
    try:
        summary = Trading212Client(key, secret, demo).request("GET", "/equity/account/summary")
    except (T212Error, requests.RequestException) as e:
        result["problems"].append(str(e)[:300])
        return result
    ccy = summary.get("currency")
    result["usdt_free"] = float((summary.get("cash") or {}).get("availableToTrade") or 0)
    result["account_currency"] = ccy
    if ccy and ccy != config.STOCK_CURRENCY:
        result["problems"].append(f"A conta está em {ccy}; muda a moeda das ações nas definições para {ccy}.")
    result["ok"] = not result["problems"]
    return result


class Trading212Broker:
    def __init__(self, key: str, secret: str, demo: bool, md):
        self.name = "Trading 212" + (" (demo)" if demo else "")
        self.api = Trading212Client(key, secret, demo)
        self.md = md
        self._instruments = (0.0, [])
        self._map: dict[str, str] = {}
        self._cash = (0.0, None)
        summary = self.api.request("GET", "/equity/account/summary")  # valida as chaves logo no arranque
        if summary.get("currency") and summary["currency"] != config.STOCK_CURRENCY:
            raise T212Error(f"A conta Trading 212 está em {summary['currency']}, mas o bot está configurado para "
                            f"{config.STOCK_CURRENCY}.")

    # ------------------------------------------------------------------ instrumentos
    def instruments(self) -> list:
        ts, data = self._instruments
        if data and time.time() - ts < 6 * 3600:
            return data
        data = self.api.request("GET", "/equity/metadata/instruments") or []
        self._instruments = (time.time(), data)
        return data

    def t212_ticker(self, symbol: str) -> str:
        if symbol in self._map:
            return self._map[symbol]
        base = symbol.split(".")[0].upper()
        suffix = T212_SUFFIX.get(exchange_of(symbol), "_US_EQ")
        ccy = self.md.currency(symbol)
        insts = self.instruments()
        exact = [i for i in insts if i.get("ticker") == f"{base}{suffix}"]
        by_name = [i for i in insts if (i.get("shortName") or "").upper() == base and i.get("ticker", "").endswith(suffix)]
        by_ccy = [i for i in by_name if i.get("currencyCode") == ccy]
        any_suffix = [i for i in insts if (i.get("shortName") or "").upper() == base and i.get("currencyCode") == ccy]
        pick = (exact or by_ccy or by_name or any_suffix or [None])[0]
        if not pick:
            raise OrderError(f"{symbol} não está disponível na Trading 212.")
        self._map[symbol] = pick["ticker"]
        return pick["ticker"]

    # ------------------------------------------------------------------ saldo e mínimos
    def available_quote(self) -> float:
        ts, cash = self._cash
        if cash is not None and time.time() - ts < 6:
            return cash
        summary = self.api.request("GET", "/equity/account/summary")
        cash = float((summary.get("cash") or {}).get("availableToTrade") or 0)
        self._cash = (time.time(), cash)
        return cash

    def min_order_value(self, symbol: str) -> float:
        return 1.0

    # ------------------------------------------------------------------ ordens
    def _place_market(self, ticker: str, qty: float) -> dict:
        """Tenta precisões decrescentes (a precisão aceite depende do instrumento). Só repete se foi REJEITADA
        por precisão, nunca por erro de rede (a API não é idempotente)."""
        last_err = None
        for decimals in (4, 3, 2, 1, 0):
            q = math.floor(abs(qty) * 10 ** decimals) / 10 ** decimals
            if q <= 0:
                continue
            body = {"ticker": ticker, "quantity": q if qty > 0 else -q, "extendedHours": False}
            try:
                return self.api.request("POST", "/equity/orders/market", json=body)
            except T212Error as e:
                msg = str(e).lower()
                if "precision" in msg or "quantity" in msg and "invalid" in msg:
                    last_err = e
                    continue
                raise
        raise OrderError(f"quantidade {qty} não aceite pela Trading 212: {last_err}")

    def _wait_fill(self, order: dict, ticker: str, side: str, price_hint: float) -> Fill:
        oid = order.get("id")
        deadline = time.time() + 90
        while time.time() < deadline:  # enquanto a ordem estiver pendente, o GET devolve-a
            pending = self.api.request("GET", f"/equity/orders/{oid}")
            if pending is None or pending.get("status") in ("FILLED", "CANCELLED", "REJECTED"):
                break
            time.sleep(1.5)
        for _ in range(6):
            hist = self.api.request("GET", "/equity/history/orders", params={"ticker": ticker, "limit": 20}) or {}
            for item in hist.get("items", []):
                o, f = item.get("order") or {}, item.get("fill") or {}
                if o.get("id") != oid:
                    continue
                if o.get("status") != "FILLED" or not f:
                    raise OrderError(f"ordem {oid} não executada (estado {o.get('status')})")
                qty = abs(float(f.get("quantity") or o.get("filledQuantity") or 0))
                px = float(f.get("price") or price_hint)
                wallet = f.get("walletImpact") or {}
                net = abs(float(wallet.get("netValue") or 0)) or qty * px * self.md.fx(ticker_symbol(ticker))
                taxes = sum(float(t.get("quantity") or 0) for t in wallet.get("taxes") or [])
                cash_delta = -net if side == "buy" else net
                return Fill(qty, px, cash_delta, abs(taxes), str(oid))
            time.sleep(10)  # o histórico tem limite de 6 pedidos/min
        raise OrderError(f"não foi possível confirmar a execução da ordem {oid}: verifica na app da Trading 212")

    def buy(self, symbol: str, qty: float, price: float) -> Fill:
        ticker = self.t212_ticker(symbol)
        order = self._place_market(ticker, abs(qty))
        self._cash = (0.0, None)
        return self._wait_fill(order, ticker, "buy", price)

    def sell(self, symbol: str, qty: float, price: float) -> Fill:
        ticker = self.t212_ticker(symbol)
        positions = self.api.request("GET", "/equity/positions", params={"ticker": ticker}) or []
        available = sum(float(p.get("quantityAvailableForTrading") or 0) for p in positions
                        if (p.get("instrument") or {}).get("ticker", ticker) == ticker)
        q = min(abs(qty), available)
        if q <= 0:
            raise PositionGone(f"{symbol}: sem ações disponíveis na conta Trading 212")
        order = self._place_market(ticker, -q)
        self._cash = (0.0, None)
        return self._wait_fill(order, ticker, "sell", price)


def ticker_symbol(t212_ticker: str) -> str:
    """AAPL_US_EQ -> AAPL ; SXR8d_EQ -> SXR8.DE (aproximado, só para câmbio)."""
    if t212_ticker.endswith("_US_EQ"):
        return t212_ticker[:-6]
    for ex, suf in T212_SUFFIX.items():
        if t212_ticker.endswith(suf):
            return f"{t212_ticker[:-len(suf)]}.{ex}"
    return t212_ticker
