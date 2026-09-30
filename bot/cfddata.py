"""
Dados de CFDs (ouro, prata, forex, índices, petróleo) com a mesma "forma" que bot/market.py e bot/stockdata.py.

- Modo Teste: preços do Yahoo Finance (grátis): futuros do ouro/prata/petróleo/índices americanos, forex à vista e
  índices europeus. Podem diferir uns pontos dos preços da tua corretora, mas o comportamento é o mesmo.
- Modo Real (demo ou real): velas e cotações da própria conta cTrader (os preços com que as ordens são executadas).
"""
import logging
import time
from datetime import datetime, time as dtime, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

import config
from . import ctrader
from .catalog import CFD_GROUP_NAMES, CFD_SPECS
from .stockdata import StockData

log = logging.getLogger("bot")

# índices "à vista" do Yahoo só têm preço no horário da bolsa (no cTrader negoceiam quase 24h)
CASH_SESSIONS = {
    "GER40": ("Europe/Berlin", dtime(9, 0), dtime(17, 30)), "EU50": ("Europe/Berlin", dtime(9, 0), dtime(17, 30)),
    "FRA40": ("Europe/Paris", dtime(9, 0), dtime(17, 30)), "UK100": ("Europe/London", dtime(8, 0), dtime(16, 30)),
    "JP225": ("Asia/Tokyo", dtime(9, 0), dtime(15, 30)),
}
GROUP_HOURS = {"fx_major": "dom 22h – sex 21h (UTC)", "fx_minor": "dom 22h – sex 21h (UTC)",
               "metals": "seg–sex, quase 24h", "indices": "seg–sex, quase 24h (índices europeus/japonês: horário da bolsa)",
               "energy": "seg–sex, quase 24h"}


def weekly_open(now: datetime | None = None) -> bool:
    """Horário geral dos CFDs: de domingo às 22h a sexta às 21h (UTC), com pausa diária das 21h às 22h."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    wd, t = now.weekday(), now.time()
    if wd == 5 or (wd == 6 and t < dtime(22, 0)) or (wd == 4 and t >= dtime(21, 0)):
        return False
    return not (dtime(21, 0) <= t < dtime(22, 0))


class _CfdEx:
    def __init__(self, owner):
        self.owner = owner

    def milliseconds(self) -> int:
        return int(time.time() * 1000)

    @property
    def markets(self) -> dict:
        syms = list(dict.fromkeys(config.CFD_UNIVERSE + config.CFD_ASSETS + ["US500"]))
        return {s: {"symbol": s, "spot": True, "active": True, "quote": config.CFD_CURRENCY, "base": s} for s in syms}

    def fetch_ticker(self, symbol):
        return self.owner.ticker(symbol)


class CfdData(StockData):
    is_stocks = False
    is_cfd = True

    def __init__(self, mode: str | None = None):
        """mode=None segue o modo atual (interface); o motor passa o seu modo (cfd_paper/cfd_demo/cfd_live)."""
        super().__init__()
        self.mode = mode
        self.ex = _CfdEx(self)

    # ------------------------------------------------------------------ fonte dos dados
    def session(self):
        key = self.mode or config.mode_key(market="cfd")
        if key not in ("cfd_demo", "cfd_live"):
            return None
        try:
            return ctrader.session_for_mode(key)
        except Exception as e:
            log.debug("cTrader indisponível: %s", e)
            return None

    @property
    def strict(self) -> bool:
        """O motor em Modo Real usa SÓ os preços da corretora (nunca os do Yahoo)."""
        return self.mode in ("cfd_demo", "cfd_live")

    def _live(self, symbol: str):
        s = self.session()
        if s is None:
            if self.strict:
                raise ctrader.CtraderError("sem ligação ao cTrader: liga a conta no Modo Real", "NO_SESSION")
            return None
        if symbol not in CFD_SPECS or self.strict:
            return s
        try:  # interface: se a corretora não tiver o instrumento, mostra o Yahoo
            return s if s.resolve(symbol) else None
        except Exception:
            return None

    @staticmethod
    def yahoo(symbol: str) -> str:
        return (CFD_SPECS.get(symbol) or {}).get("yahoo") or symbol

    # ------------------------------------------------------------------ velas (Yahoo)
    def _download(self, symbols: list, tf: str, **kw) -> dict:
        tf = "1wk" if tf == "1w" else tf
        ymap = {self.yahoo(s): s for s in symbols}
        got = super()._download(list(ymap), tf, **kw)
        out = {}
        for ysym, df in got.items():
            if len(df) and float(df["volume"].sum()) == 0:
                # forex/índices à vista não têm volume no Yahoo: usa a amplitude como aproximação da atividade
                df = df.copy()
                df["volume"] = ((df["high"] - df["low"]) / df["close"] * 1e6).round() + 1
            out[ymap.get(ysym, ysym)] = df
        return out

    def ohlcv(self, symbol: str, tf: str, limit: int, closed_only: bool = True) -> pd.DataFrame:
        live = self._live(symbol)
        if live is not None:
            return live.ohlcv(symbol, "1w" if tf == "1wk" else tf, limit, closed_only)
        return super().ohlcv(symbol, "1wk" if tf == "1w" else tf, limit, closed_only)

    def prefetch(self, symbols: list, tfs: list):
        if self.session() is not None:
            return  # o cTrader serve as velas por pedido (com cache por símbolo)
        tfs = {"1wk" if t == "1w" else "1h" if t == "4h" else t for t in tfs}  # 4h = agregado das velas de 1h
        super().prefetch(symbols, sorted(tfs))

    # ------------------------------------------------------------------ preços
    def ticker(self, symbol: str) -> dict:
        live = self._live(symbol)
        if live is not None:
            return live.ticker(symbol)
        hit = self._quotes.get(symbol)
        if hit and time.time() - hit[0] < 15:
            return hit[1]
        fi = yf.Ticker(self.yahoo(symbol)).fast_info
        last = float(fi["lastPrice"])
        prev = float(fi["previousClose"] or last)
        q = {"symbol": symbol, "last": last, "percentage": (last / prev - 1) * 100 if prev else None,
             "quoteVolume": None}
        self._quotes[symbol] = (time.time(), q)
        return q

    def all_tickers(self) -> dict:
        ts, data = self._daily
        if data and time.time() - ts < 600:
            return data
        syms = [s for s in dict.fromkeys(config.CFD_UNIVERSE + config.CFD_ASSETS + ["US500"]) if s in CFD_SPECS]
        data = {}
        for s, df in self._download(syms, "1d", period="1mo").items():
            if len(df) < 2:
                continue
            last, prev = float(df["close"].iloc[-1]), float(df["close"].iloc[-2])
            data[s] = {"symbol": s, "last": last, "percentage": (last / prev - 1) * 100, "quoteVolume": None}
        self._daily = (time.time(), data)
        return data

    def liquid_symbols(self, n: int, min_volume: float) -> list:
        pool = list(dict.fromkeys(config.CFD_UNIVERSE))
        return [s for s in pool if self.market_open(s)][:n] or pool[:n]

    def discoveries(self) -> list:
        return []

    def orderbook_stats(self, symbol: str) -> dict:
        live = self._live(symbol)
        if live is None:
            return {}
        try:
            t = live.ticker(symbol)
            return {"bid": t["bid"], "ask": t["ask"], "spread_pct": round(t["spread_pct"] or 0, 4)}
        except Exception:
            return {}

    # ------------------------------------------------------------------ horário
    def session_open(self, symbol: str, now: datetime | None = None) -> bool:
        live = self._live(symbol) if not now else None
        if live is not None:
            state = live.is_open(symbol)
            if state is not None:
                return state
        if not weekly_open(now):
            return False
        if symbol in CASH_SESSIONS and live is None:
            tzname, start, end = CASH_SESSIONS[symbol]
            local = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(tzname))
            return local.weekday() < 5 and start <= local.time() < end
        return True

    def market_open(self, symbol: str) -> bool:
        """Horário normal E preço recente (apanha feriados e pausas)."""
        try:
            if not self.session_open(symbol):
                return False
        except Exception:
            return weekly_open()
        if self.session() is not None:
            return True
        hit = self._frames.get((symbol, "1h")) or self._frames.get((symbol, "15m"))
        if hit and len(hit[1]) and time.time() - hit[0] < 600:
            last_bar = int(hit[1]["ts"].iloc[-1]) / 1000
            if time.time() - last_bar > 3 * 3600:
                return False  # devia haver velas recentes: feriado ou mercado parado
        return True

    def market_status(self) -> list:
        out = []
        groups: dict[str, list] = {}
        for s in dict.fromkeys(config.CFD_UNIVERSE + config.CFD_ASSETS):
            g = (CFD_SPECS.get(s) or {}).get("group", "outros")
            groups.setdefault(CFD_GROUP_NAMES.get(g, "Outros"), []).append((s, g))
        for name, items in groups.items():
            is_open = any(self.market_open(s) for s, _ in items)
            out.append({"exchange": name, "name": name, "short": name, "open": is_open,
                        "hours": GROUP_HOURS.get(items[0][1], "")})
        return out

    # ------------------------------------------------------------------ moeda
    def currency(self, symbol: str) -> str:
        spec = CFD_SPECS.get(symbol)
        if spec:
            return spec["quote"]
        s = self.session() or ctrader.any_session()
        if s is not None:
            try:
                return s.spec(symbol)["quote"]
            except Exception:
                pass
        return "USD"

    def fx(self, symbol: str, account: str | None = None) -> float:
        return super().fx(symbol, account or config.CFD_CURRENCY)

    # ------------------------------------------------------------------ especificações (tamanho mínimo, margem)
    def spec(self, symbol: str) -> dict:
        live = self._live(symbol)
        if live is not None:
            sp = live.spec(symbol)
            sp["spread"] = (CFD_SPECS.get(symbol) or {}).get("spread", 0)
            return sp
        c = CFD_SPECS.get(symbol)
        if not c:
            raise ValueError(f"{symbol} só está disponível com a conta cTrader ligada (Modo Real)")
        return {"min_qty": c["min"], "step": c["step"], "margin_rate": c["margin"], "spread": c["spread"],
                "quote": c["quote"], "broker_symbol": symbol, "description": c["name"], "short_ok": True}

    def has_symbol(self, symbol: str) -> bool:
        if symbol in CFD_SPECS or symbol in config.CFD_ASSETS:
            return True
        s = self.session() or ctrader.any_session()
        try:
            return bool(s and s.resolve(symbol))
        except Exception:
            return False

    # ------------------------------------------------------------------ contexto para a IA
    def fundamentals(self, symbol: str) -> dict:
        return {}

    def days_to_earnings(self, symbol: str):
        return None

    def derivatives(self, symbol: str) -> dict:
        c = CFD_SPECS.get(symbol) or {}
        out = {"instrument": c.get("name") or symbol, "asset_class": CFD_GROUP_NAMES.get(c.get("group"), "CFD"),
               "quote_currency": self.currency(symbol), "margin_pct": round(c.get("margin", 0.2) * 100, 2)}
        if not self.strict and self.session() is None:
            out["price_source"] = f"Yahoo {self.yahoo(symbol)} (futuros/índice; a corretora pode diferir uns pontos)"
        return out
