"""Dados de mercado: velas, ticker, livro de ordens, derivados e sentimento (Fear & Greed)."""
import logging
import time

import ccxt
import pandas as pd
import requests

log = logging.getLogger("bot")


def retry(fn, attempts: int = 4, base_delay: float = 2.0):
    """Repete chamadas que falham por problemas de rede. NÃO usar para criar ordens."""
    for i in range(attempts):
        try:
            return fn()
        except (ccxt.NetworkError, requests.RequestException) as e:
            if i == attempts - 1:
                raise
            wait = base_delay * 2**i
            log.warning("Erro de rede (%s). Nova tentativa em %.0fs", str(e)[:120], wait)
            time.sleep(wait)


def to_df(rows) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df["time"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.set_index("time")
    return df[~df.index.duplicated(keep="last")].sort_index()


class MarketData:
    def __init__(self, exchange_id: str):
        self.ex = getattr(ccxt, exchange_id)({"enableRateLimit": True})
        retry(self.ex.load_markets)
        self._futures = None
        self._futures_failed = False
        self._fng = (0.0, None)
        self._all_tickers = (0.0, {})

    def tf_ms(self, tf: str) -> int:
        return self.ex.parse_timeframe(tf) * 1000

    def ohlcv(self, symbol: str, tf: str, limit: int, closed_only: bool = True) -> pd.DataFrame:
        """Últimas `limit` velas (por defeito só as já FECHADAS)."""
        rows = retry(lambda: self.ex.fetch_ohlcv(symbol, tf, limit=limit + 1))
        df = to_df(rows)
        if closed_only:
            df = df[df["ts"] + self.tf_ms(tf) <= self.ex.milliseconds()]
        return df.tail(limit)

    def ohlcv_range(self, symbol: str, tf: str, since_ms: int, until_ms: int) -> pd.DataFrame:
        """Velas fechadas entre since_ms e until_ms (paginado, para backtests)."""
        step = self.tf_ms(tf)
        rows, cursor = [], since_ms
        while cursor < until_ms:
            batch = retry(lambda: self.ex.fetch_ohlcv(symbol, tf, since=cursor, limit=1000))
            if not batch:
                break
            rows.extend(batch)
            nxt = batch[-1][0] + step
            if nxt <= cursor:
                break
            cursor = nxt
        df = to_df(rows)
        return df[(df["ts"] >= since_ms) & (df["ts"] + step <= until_ms)]

    def tickers(self, symbols: list[str]) -> dict:
        return retry(lambda: self.ex.fetch_tickers(symbols))

    def all_tickers(self) -> dict:
        """Todos os tickers da exchange num só pedido (cache de 60 s)."""
        ts, data = self._all_tickers
        if data and time.time() - ts < 60:
            return data
        data = retry(self.ex.fetch_tickers)
        self._all_tickers = (time.time(), data)
        return data

    def orderbook_stats(self, symbol: str) -> dict:
        ob = retry(lambda: self.ex.fetch_order_book(symbol, limit=100))
        if not ob["bids"] or not ob["asks"]:
            return {}
        bid, ask = ob["bids"][0][0], ob["asks"][0][0]
        mid = (bid + ask) / 2
        bid_depth = sum(l[0] * l[1] for l in ob["bids"] if l[0] >= mid * 0.99)
        ask_depth = sum(l[0] * l[1] for l in ob["asks"] if l[0] <= mid * 1.01)
        total = bid_depth + ask_depth
        return {
            "bid": bid,
            "ask": ask,
            "spread_pct": round((ask - bid) / mid * 100, 4),
            "bid_depth_1pct_usd": round(bid_depth),
            "ask_depth_1pct_usd": round(ask_depth),
            "depth_imbalance_1pct": round((bid_depth - ask_depth) / total, 3) if total else 0.0,
        }

    def derivatives(self, symbol: str) -> dict:
        """Funding rate e open interest dos futuros perpétuos (se disponíveis)."""
        if self._futures_failed:
            return {}
        try:
            if self._futures is None:
                self._futures = ccxt.binanceusdm({"enableRateLimit": True})
                retry(self._futures.load_markets)
            base = symbol.split("/")[0]
            fsym = f"{base}/USDT:USDT"  # os perpétuos mais líquidos são em USDT (dados de sentimento)
            if fsym not in self._futures.markets:
                return {}
            out = {}
            fr = retry(lambda: self._futures.fetch_funding_rate(fsym))
            if fr.get("fundingRate") is not None:
                out["funding_rate_pct"] = round(fr["fundingRate"] * 100, 4)
            try:
                hist = retry(lambda: self._futures.fetch_open_interest_history(fsym, "1h", limit=25))
                vals = [h.get("openInterestValue") for h in hist if h.get("openInterestValue")]
                if len(vals) >= 2:
                    out["open_interest_usd"] = round(vals[-1])
                    out["open_interest_change_24h_pct"] = round((vals[-1] / vals[0] - 1) * 100, 2)
            except Exception as e:
                log.debug("Open interest indisponível: %s", e)
            out.update(self._positioning(f"{base}USDT"))
            return out
        except Exception as e:
            log.warning("Dados de futuros indisponíveis (%s). A continuar sem eles.", str(e)[:120])
            self._futures_failed = True
            return {}

    @staticmethod
    def _positioning(fsym: str) -> dict:
        """Rácio long/short das contas e rácio de compras/vendas agressivas (taker) nos futuros."""
        out = {}
        base = "https://fapi.binance.com/futures/data/"
        try:
            r = requests.get(base + "globalLongShortAccountRatio",
                             params={"symbol": fsym, "period": "1h", "limit": 24}, timeout=10).json()
            if isinstance(r, list) and r:
                out["long_short_account_ratio"] = round(float(r[-1]["longShortRatio"]), 3)
                out["long_accounts_pct"] = round(float(r[-1]["longAccount"]) * 100, 1)
                out["long_short_ratio_24h_ago"] = round(float(r[0]["longShortRatio"]), 3)
            r = requests.get(base + "takerlongshortRatio",
                             params={"symbol": fsym, "period": "1h", "limit": 6}, timeout=10).json()
            if isinstance(r, list) and r:
                out["taker_buy_sell_ratio_last_1h"] = round(float(r[-1]["buySellRatio"]), 3)
                out["taker_buy_sell_ratio_6h_avg"] = round(sum(float(x["buySellRatio"]) for x in r) / len(r), 3)
        except Exception as e:
            log.debug("Posicionamento indisponível para %s: %s", fsym, e)
        return out

    def fear_greed(self) -> dict | None:
        """Índice Fear & Greed de cripto (cache de 1h)."""
        ts, cached = self._fng
        if cached and time.time() - ts < 3600:
            return cached
        try:
            r = retry(lambda: requests.get("https://api.alternative.me/fng/", params={"limit": 7}, timeout=15))
            data = r.json()["data"]
            vals = [int(d["value"]) for d in data]
            cached = {
                "value": vals[0],
                "label": data[0]["value_classification"],
                "yesterday": vals[1] if len(vals) > 1 else None,
                "avg_7d": round(sum(vals) / len(vals), 1),
            }
            self._fng = (time.time(), cached)
            return cached
        except Exception as e:
            log.warning("Fear & Greed indisponível: %s", e)
            return cached

    @staticmethod
    def fear_greed_history() -> dict:
        """{'YYYY-MM-DD': (valor, classificação)} para backtests."""
        try:
            r = requests.get("https://api.alternative.me/fng/", params={"limit": 0}, timeout=30)
            out = {}
            for d in r.json()["data"]:
                day = pd.to_datetime(int(d["timestamp"]), unit="s", utc=True).strftime("%Y-%m-%d")
                out[day] = (int(d["value"]), d["value_classification"])
            return out
        except Exception as e:
            log.warning("Histórico Fear & Greed indisponível: %s", e)
            return {}
