"""
Dados de ações e ETFs (Yahoo Finance via yfinance, grátis).

Tem a mesma "forma" que bot/market.py (cripto), para o resto do bot funcionar igual:
velas, preços, universo líquido, horário das bolsas, câmbio, fundamentais/resultados e VIX.
"""
import logging
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

import config

log = logging.getLogger("bot")

TF_INTERVAL = {"5m": "5m", "15m": "15m", "1h": "60m", "1d": "1d", "1wk": "1wk"}
TF_PERIOD = {"5m": "30d", "15m": "60d", "1h": "180d", "1d": "3y", "1wk": "10y"}
TF_SECONDS = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400, "1wk": 604800}
TF_TTL = {"5m": 60, "15m": 120, "1h": 240, "1d": 1800, "1wk": 7200}

SESSIONS = {  # bolsa -> (fuso, abertura, fecho)
    "US": ("America/New_York", dtime(9, 30), dtime(16, 0)),
    "DE": ("Europe/Berlin", dtime(9, 0), dtime(17, 30)),
    "L": ("Europe/London", dtime(8, 0), dtime(16, 30)),
    "AS": ("Europe/Amsterdam", dtime(9, 0), dtime(17, 30)),
    "PA": ("Europe/Paris", dtime(9, 0), dtime(17, 30)),
    "MI": ("Europe/Rome", dtime(9, 0), dtime(17, 30)),
}
EXCHANGE_NAMES = {"US": "Bolsa de Nova Iorque/Nasdaq", "DE": "Xetra (Frankfurt)", "L": "Londres", "AS": "Amesterdão",
                  "PA": "Paris", "MI": "Milão"}

# nomes para procurar notícias
STOCK_NAMES = {
    "AAPL": ["Apple"], "MSFT": ["Microsoft"], "NVDA": ["Nvidia"], "AMZN": ["Amazon"], "GOOGL": ["Alphabet", "Google"],
    "META": ["Meta Platforms", "Facebook", "Instagram"], "TSLA": ["Tesla"], "AVGO": ["Broadcom"], "AMD": ["AMD"],
    "NFLX": ["Netflix"], "JPM": ["JPMorgan"], "V": ["Visa"], "MA": ["Mastercard"], "LLY": ["Eli Lilly"],
    "XOM": ["Exxon"], "COST": ["Costco"], "WMT": ["Walmart"], "HD": ["Home Depot"], "KO": ["Coca-Cola"],
    "PEP": ["PepsiCo"], "CRM": ["Salesforce"], "ORCL": ["Oracle"], "ADBE": ["Adobe"], "QCOM": ["Qualcomm"],
    "BAC": ["Bank of America"], "DIS": ["Disney"], "PLTR": ["Palantir"], "UBER": ["Uber"], "COIN": ["Coinbase"],
    "MU": ["Micron"], "INTC": ["Intel"], "UNH": ["UnitedHealth"],
    "SXR8.DE": ["S&P 500", "S&P", "Wall Street", "stock market"], "SXRV.DE": ["Nasdaq-100", "Nasdaq 100", "Nasdaq", "tech stocks"],
    "EUNL.DE": ["MSCI World", "global stocks", "world stocks"],
    "VWCE.DE": ["FTSE All-World", "global stocks", "world stocks"], "IS3N.DE": ["emerging markets"], "EXSA.DE": ["Stoxx 600", "European stocks"],
    "4GLD.DE": ["gold"], "QDVE.DE": ["tech stocks", "technology sector"], "DBXD.DE": ["DAX"],
    "IUSN.DE": ["small caps", "small-cap"], "EXV1.DE": ["European banks"], "SXRT.DE": ["Euro Stoxx 50"],
    "SPY": ["S&P 500"],
}


def exchange_of(symbol: str) -> str:
    suffix = symbol.rsplit(".", 1)[-1] if "." in symbol else "US"
    return suffix if suffix in SESSIONS else "US"


def _to_df(raw: pd.DataFrame) -> pd.DataFrame:
    if raw is None or raw.empty:
        return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])
    df = raw.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]].dropna(subset=["close"])
    idx = pd.DatetimeIndex(df.index)
    idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
    df.index = idx
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df["volume"] = df["volume"].fillna(0).astype(float)
    df.insert(0, "ts", df.index.as_unit("ms").asi8.astype("int64"))
    return df


class _FakeEx:
    """Compatibilidade com código que espera um objeto 'exchange' (ccxt)."""

    def __init__(self, owner):
        self.owner = owner

    def milliseconds(self) -> int:
        return int(time.time() * 1000)

    @property
    def markets(self) -> dict:
        return {s: {"symbol": s, "spot": True, "active": True, "quote": config.STOCK_CURRENCY, "base": s}
                for s in config.STOCK_UNIVERSE + [config.benchmark(), config.TREND_CASH_ETF] + config.TREND_UNIVERSE}

    def fetch_ticker(self, symbol):
        return self.owner.ticker(symbol)


class StockData:
    is_stocks = True

    def __init__(self):
        self.lock = threading.RLock()
        self.ex = _FakeEx(self)
        self._frames: dict = {}
        self._quotes: dict = {}
        self._daily = (0.0, {})
        self._info: dict = {}
        self._ccy: dict = {}
        self._fx: dict = {}
        self._vix = (0.0, None)

    # ------------------------------------------------------------------ básicos
    @staticmethod
    def tf_ms(tf: str) -> int:
        return TF_SECONDS[tf] * 1000

    @staticmethod
    def now_ms() -> int:
        return int(time.time() * 1000)

    @staticmethod
    def has_symbol(symbol: str) -> bool:
        if symbol in config.STOCK_UNIVERSE or symbol in config.TREND_UNIVERSE or symbol == config.TREND_CASH_ETF:
            return True
        try:
            return float(yf.Ticker(symbol).fast_info["lastPrice"] or 0) > 0
        except Exception:
            return False

    # ------------------------------------------------------------------ velas
    def _download(self, symbols: list, tf: str, **kw) -> dict:
        out = {}
        for i in range(0, len(symbols), 40):
            chunk = symbols[i:i + 40]
            params = {"interval": TF_INTERVAL[tf], "progress": False, "group_by": "ticker", "auto_adjust": False,
                      "threads": True}
            params.update(kw or {"period": TF_PERIOD[tf]})
            try:
                raw = yf.download(chunk, **params)
            except Exception as e:
                log.warning("Yahoo Finance falhou (%s): %s", tf, str(e)[:120])
                continue
            for s in chunk:
                try:
                    sub = raw[s] if isinstance(raw.columns, pd.MultiIndex) else raw
                    df = _to_df(sub)
                    if len(df):
                        out[s] = df
                except (KeyError, ValueError):
                    continue
        return out

    def prefetch(self, symbols: list, tfs: list):
        """Descarrega em lote (muito mais rápido do que um a um)."""
        now = time.time()
        for tf in tfs:
            stale = [s for s in symbols if now - self._frames.get((s, tf), (0, None))[0] > TF_TTL[tf]]
            if stale:
                for s, df in self._download(stale, tf).items():
                    self._frames[(s, tf)] = (time.time(), df)

    def ohlcv(self, symbol: str, tf: str, limit: int, closed_only: bool = True) -> pd.DataFrame:
        if tf == "4h":  # o Yahoo não tem 4h: agrega a partir de 1h
            h = self.ohlcv(symbol, "1h", limit * 4 + 8, closed_only)
            agg = h.drop(columns="ts").resample("4h", origin="start_day").agg(
                {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
            agg.insert(0, "ts", agg.index.as_unit("ms").asi8.astype("int64"))
            return agg.tail(limit)
        hit = self._frames.get((symbol, tf))
        if not hit or time.time() - hit[0] > TF_TTL[tf]:
            got = self._download([symbol], tf).get(symbol)
            if got is None:
                if hit:
                    df = hit[1]
                else:
                    raise ValueError(f"sem dados para {symbol} ({tf})")
            else:
                self._frames[(symbol, tf)] = (time.time(), got)
                df = got
        else:
            df = hit[1]
        if closed_only and len(df) and tf not in ("1d", "1wk"):
            if int(df["ts"].iloc[-1]) + self.tf_ms(tf) > self.now_ms():
                df = df.iloc[:-1]
        return df.tail(limit).copy()

    def ohlcv_range(self, symbol: str, tf: str, since_ms: int, until_ms: int) -> pd.DataFrame:
        start = datetime.fromtimestamp(since_ms / 1000, timezone.utc).strftime("%Y-%m-%d")
        end = (datetime.fromtimestamp(until_ms / 1000, timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
        df = self._download([symbol], tf, start=start, end=end).get(symbol)
        if df is None:
            raise ValueError(f"sem dados para {symbol}")
        return df[(df["ts"] >= since_ms) & (df["ts"] + self.tf_ms(tf) <= until_ms)]

    # ------------------------------------------------------------------ preços
    def ticker(self, symbol: str) -> dict:
        hit = self._quotes.get(symbol)
        if hit and time.time() - hit[0] < 15:
            return hit[1]
        fi = yf.Ticker(symbol).fast_info
        last = float(fi["lastPrice"])
        prev = float(fi["previousClose"] or last)
        vol = float(fi.get("lastVolume") or 0)
        q = {"symbol": symbol, "last": last, "percentage": (last / prev - 1) * 100 if prev else None,
             "quoteVolume": vol * last, "currency": fi.get("currency")}
        self._ccy[symbol] = fi.get("currency") or self._ccy.get(symbol)
        self._quotes[symbol] = (time.time(), q)
        return q

    def tickers(self, symbols) -> dict:
        symbols = list(symbols)
        out = {}
        with ThreadPoolExecutor(max_workers=min(8, max(1, len(symbols)))) as pool:
            for s, q in zip(symbols, pool.map(self._safe_ticker, symbols)):
                if q:
                    out[s] = q
        return out

    def _safe_ticker(self, symbol):
        try:
            return self.ticker(symbol)
        except Exception as e:
            log.debug("Sem cotação para %s: %s", symbol, e)
            return None

    def all_tickers(self) -> dict:
        """Resumo diário do universo (preço, variação, volume médio em dinheiro), cache de 10 min."""
        ts, data = self._daily
        if data and time.time() - ts < 600:
            return data
        syms = list(dict.fromkeys(config.STOCK_UNIVERSE + [config.benchmark()]))
        frames = self._download(syms, "1d", period="1mo")
        data = {}
        for s, df in frames.items():
            if len(df) < 2:
                continue
            last, prev = float(df["close"].iloc[-1]), float(df["close"].iloc[-2])
            dollar_vol = float((df["close"] * df["volume"]).tail(10).mean())
            data[s] = {"symbol": s, "last": last, "percentage": (last / prev - 1) * 100, "quoteVolume": dollar_vol}
        self._daily = (time.time(), data)
        return data

    def liquid_symbols(self, n: int, min_volume: float) -> list:
        tick = self.all_tickers()
        rows = [(s, tick.get(s, {}).get("quoteVolume") or 0) for s in config.STOCK_UNIVERSE]
        rows = [r for r in rows if r[1] >= min_volume * 0.1]
        rows.sort(key=lambda x: -x[1])
        return [s for s, _ in rows[:n]]

    # ------------------------------------------------------------------ bolsa aberta?
    def session_open(self, symbol: str, now: datetime | None = None) -> bool:
        tzname, start, end = SESSIONS[exchange_of(symbol)]
        local = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(tzname))
        return local.weekday() < 5 and start <= local.time() < end

    def market_open(self, symbol: str) -> bool:
        """Horário normal E há negociação recente (apanha feriados)."""
        if not self.session_open(symbol):
            return False
        tzname, start, _ = SESSIONS[exchange_of(symbol)]
        now_local = datetime.now(timezone.utc).astimezone(ZoneInfo(tzname))
        session_start = now_local.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
        if (now_local - session_start).total_seconds() < 45 * 60:
            return True  # acabou de abrir: ainda pode não haver velas de hoje
        hit = self._frames.get((symbol, "1h")) or self._frames.get((symbol, "15m"))
        if hit and len(hit[1]) and time.time() - hit[0] < TF_TTL["1h"]:
            last_bar = datetime.fromtimestamp(int(hit[1]["ts"].iloc[-1]) / 1000, timezone.utc)
            if last_bar < session_start.astimezone(timezone.utc):
                return False  # sessão aberta há 45+ min e nenhuma vela de hoje: feriado
        return True

    def market_status(self) -> list:
        out = []
        for ex in sorted({exchange_of(s) for s in config.STOCK_UNIVERSE}):
            tzname, start, end = SESSIONS[ex]
            sample = next(s for s in config.STOCK_UNIVERSE if exchange_of(s) == ex)
            short = {"US": "Nova Iorque", "DE": "Xetra", "L": "Londres", "AS": "Amesterdão", "PA": "Paris", "MI": "Milão"}[ex]
            out.append({"exchange": ex, "name": EXCHANGE_NAMES[ex], "short": short, "open": self.market_open(sample),
                        "hours": f"{start:%H:%M}-{end:%H:%M} ({tzname.split('/')[-1].replace('_', ' ')})"})
        return out

    # ------------------------------------------------------------------ moeda
    def currency(self, symbol: str) -> str:
        if symbol not in self._ccy or not self._ccy[symbol]:
            try:
                self._ccy[symbol] = yf.Ticker(symbol).fast_info["currency"]
            except Exception:
                self._ccy[symbol] = "EUR" if exchange_of(symbol) in ("DE", "AS", "PA", "MI") else "USD"
        return self._ccy[symbol]

    def _rate(self, pair: str) -> float:
        hit = self._fx.get(pair)
        if hit and time.time() - hit[0] < 600:
            return hit[1]
        try:
            rate = float(yf.Ticker(f"{pair}=X").fast_info["lastPrice"])
            self._fx[pair] = (time.time(), rate)
            return rate
        except Exception as e:
            if hit:
                return hit[1]
            raise RuntimeError(f"sem câmbio {pair}: {e}")

    def fx(self, symbol: str, account: str | None = None) -> float:
        """Multiplicador moeda do instrumento -> moeda da conta."""
        account = account or config.STOCK_CURRENCY
        ccy = self.currency(symbol)
        if ccy == account:
            return 1.0
        if ccy == "GBp":
            return self._rate(f"GBP{account}") / 100
        return self._rate(f"{ccy}{account}")

    # ------------------------------------------------------------------ fundamentais / resultados
    def fundamentals(self, symbol: str) -> dict:
        hit = self._info.get(symbol)
        if hit and time.time() - hit[0] < 6 * 3600:
            return hit[1]
        out = {}
        try:
            t = yf.Ticker(symbol)
            try:
                cal = t.calendar or {}
                dates = cal.get("Earnings Date") or []
                future = [d for d in dates if d >= datetime.now(timezone.utc).date()]
                if future:
                    out["next_earnings_date"] = str(future[0])
                    out["days_to_earnings"] = (future[0] - datetime.now(timezone.utc).date()).days
            except Exception:
                pass
            info = t.info or {}
            price = info.get("currentPrice") or info.get("regularMarketPrice")
            target = info.get("targetMeanPrice")
            hi52 = info.get("fiftyTwoWeekHigh")
            out.update({
                "name": info.get("shortName"),
                "type": info.get("quoteType"),
                "sector": info.get("sector"),
                "market_cap_bn": round(info["marketCap"] / 1e9, 1) if info.get("marketCap") else None,
                "trailing_pe": _r(info.get("trailingPE")),
                "forward_pe": _r(info.get("forwardPE")),
                "analyst_recommendation": info.get("recommendationKey"),
                "analyst_target_upside_pct": _r((target / price - 1) * 100) if target and price else None,
                "short_pct_float": _r((info.get("shortPercentOfFloat") or 0) * 100) if info.get("shortPercentOfFloat") else None,
                "dist_52w_high_pct": _r((price / hi52 - 1) * 100) if price and hi52 else None,
                "expense_ratio_pct": _r(info.get("netExpenseRatio")),
            })
        except Exception as e:
            log.debug("Fundamentais de %s indisponíveis: %s", symbol, e)
        out = {k: v for k, v in out.items() if v is not None}
        self._info[symbol] = (time.time(), out)
        return out

    def days_to_earnings(self, symbol: str) -> int | None:
        return self.fundamentals(symbol).get("days_to_earnings")

    # ------------------------------------------------------------------ interface comum com cripto
    def orderbook_stats(self, symbol: str) -> dict:
        return {}

    def derivatives(self, symbol: str) -> dict:
        return {"fundamentals": self.fundamentals(symbol), "exchange": EXCHANGE_NAMES[exchange_of(symbol)],
                "currency": self.currency(symbol)}

    def fear_greed(self) -> dict | None:
        """Medo/ganância das bolsas: VIX (volatilidade esperada do S&P 500)."""
        ts, cached = self._vix
        if cached and time.time() - ts < 900:
            return cached
        try:
            v = self._download(["^VIX"], "1d", period="1mo").get("^VIX")
            last, prev5 = float(v["close"].iloc[-1]), float(v["close"].iloc[-6])
            label = ("pânico" if last > 35 else "medo" if last > 25 else "nervoso" if last > 20
                     else "calmo" if last > 13 else "complacente")
            cached = {"vix": round(last, 2), "vix_5d_change_pct": round((last / prev5 - 1) * 100, 1), "label": label}
            self._vix = (time.time(), cached)
        except Exception as e:
            log.debug("VIX indisponível: %s", e)
        return cached

    @staticmethod
    def fear_greed_history() -> dict:
        return {}


def _r(x, nd=2):
    try:
        x = float(x)
        return None if math.isnan(x) or math.isinf(x) else round(x, nd)
    except (TypeError, ValueError):
        return None
