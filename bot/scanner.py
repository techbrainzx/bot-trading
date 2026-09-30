"""
Radar de oportunidades: análise técnica completa das moedas mais negociadas (sem gastar IA).

Para cada moeda calcula setups, tendência, momentum, força face ao BTC, volatilidade e liquidez,
e junta tudo numa pontuação 0-100. Mede também o "clima" do mercado (quantas moedas sobem).
"""
import logging
import math
import time

import config
from . import indicators as ind
from .analysis import detect_setups, pct, trend_label

log = logging.getLogger("bot")

def _num(x, nd=2):
    try:
        x = float(x)
        return None if math.isnan(x) or math.isinf(x) else round(x, nd)
    except (TypeError, ValueError):
        return None


class Scanner:
    def __init__(self, md, perf_adjust=None):
        self.md = md
        self.perf_adjust = perf_adjust  # função(setup) -> ajuste de pontuação com base no histórico
        self._universe = (0.0, [])
        self.last: dict = {"time": None, "results": [], "universe": 0, "regime": None}

    def min_volume(self) -> float:
        # os pares em EUR têm muito menos volume na Binance
        return config.SCAN_MIN_VOLUME * (0.05 if config.QUOTE == "EUR" and config.MARKET == "crypto" else 1)

    def universe(self) -> list[str]:
        """As mais negociadas (cache de 30 min). Em ações, só as que têm a bolsa aberta agora."""
        ts, syms, key = self._universe if len(self._universe) == 3 else (0.0, [], None)
        new_key = (config.SCAN_UNIVERSE, config.QUOTE, config.MARKET, tuple(config.STOCK_UNIVERSE), config.STOCK_DISCOVERY,
                   tuple(config.CFD_UNIVERSE))
        ttl = 1800 if config.MARKET == "crypto" else 600  # nas ações, as bolsas abertas mudam ao longo do dia
        if not (syms and time.time() - ts < ttl and key == new_key):
            syms = self.md.liquid_symbols(config.SCAN_UNIVERSE, self.min_volume())
            self._universe = (time.time(), syms, new_key)
        mine = [s for s in config.SYMBOLS if s not in syms]  # os teus ativos entram sempre, sem filtro de volume
        return [s for s in syms + mine if self.md.market_open(s)]

    def _analyze(self, sym, tickers, btc_ret, higher_tf):
        primary = self.md.ohlcv(sym, config.PRIMARY_TIMEFRAME, 250)
        higher = self.md.ohlcv(sym, higher_tf, 250)
        if len(primary) < 60:
            return None
        r = detect_setups(primary, higher, allow_short=config.MARKET == "cfd" and config.CFD_ALLOW_SHORT)
        c = primary["close"]
        price = float(c.iloc[-1])
        e20, e50, e200 = ind.ema(c, 20), ind.ema(c, 50), ind.ema(c, 200)
        adx, _, _ = ind.adx(primary)
        atr_pct = float(ind.atr(primary).iloc[-1]) / price * 100
        hc = higher["close"]
        ret20 = pct(price, c.iloc[-21])
        rel = round(ret20 - btc_ret, 2) if ret20 is not None and btc_ret is not None else None
        t = tickers.get(sym) or {}
        above_ema50_higher = len(hc) > 50 and float(hc.iloc[-1]) > float(ind.ema(hc, 50).iloc[-1])

        score = r["score"]
        setup = r["setups"][0] if r["setups"] else None
        if rel is not None:
            score += 5 if rel > 3 else 3 if rel > 1 else -3 if rel < -3 else 0
        if atr_pct < 0.25:
            score -= 8  # quase parada: pouco potencial depois de comissões
        elif atr_pct > 4:
            score -= 8  # volatilidade extrema: stops enormes
        if setup and self.perf_adjust:
            score += self.perf_adjust(setup["setup"])
        return {
            "symbol": sym,
            "score": int(max(0, min(100, score))),
            "setup": setup,
            "other_setups": [s["label"] for s in r["setups"][1:]],
            "trend": trend_label(price, e20.iloc[-1], e50.iloc[-1], e200.iloc[-1]),
            "higher_trend": r["higher_trend"],
            "divergence": (r.get("divergence") or {}).get("type"),
            "patterns": r.get("patterns") or [],
            "rsi": _num(ind.rsi(c).iloc[-1], 1),
            "adx": _num(adx.iloc[-1], 1),
            "atr_pct": _num(atr_pct),
            "ret_20bars_pct": ret20,
            "ret_7d_pct": pct(float(hc.iloc[-1]), hc.iloc[-43]) if len(hc) > 43 and higher_tf == "4h" else None,
            "change_24h_pct": _num(t.get("percentage")),
            "volume_24h": t.get("quoteVolume"),
            "price": t.get("last") or price,
            "rel_strength_vs_btc": rel,
            "mine": sym in config.SYMBOLS,
            "above_ema50_higher": above_ema50_higher,
        }

    def scan(self) -> list[dict]:
        t0 = time.time()
        higher_tf = config.CONTEXT_TIMEFRAMES[1]
        self.md.prefetch(list(dict.fromkeys(self.md.liquid_symbols(config.SCAN_UNIVERSE, self.min_volume())
                                            + list(config.SYMBOLS) + [config.benchmark()])),
                         [config.PRIMARY_TIMEFRAME, higher_tf])
        syms = self.universe()
        tickers = self.md.all_tickers()
        btc_trend_higher, btc_ret = None, None
        try:
            btc = self.md.ohlcv(config.benchmark(), config.PRIMARY_TIMEFRAME, 60)
            btc_ret = pct(btc["close"].iloc[-1], btc["close"].iloc[-21])
            bh = self.md.ohlcv(config.benchmark(), higher_tf, 250)
            bc = bh["close"]
            btc_trend_higher = trend_label(float(bc.iloc[-1]), ind.ema(bc, 20).iloc[-1], ind.ema(bc, 50).iloc[-1],
                                           ind.ema(bc, 200).iloc[-1])
        except Exception:
            pass
        results = []
        for sym in syms:
            try:
                row = self._analyze(sym, tickers, btc_ret, higher_tf)
                if row:
                    results.append(row)
            except Exception as e:
                log.debug("Radar: %s falhou (%s)", sym, e)
        results.sort(key=lambda x: -x["score"])
        regime = self.regime(results, btc_trend_higher)
        self.last = {"time": time.time(), "results": results, "universe": len(syms), "regime": regime}
        log.info("Radar: %d ativos em %.0fs | mercado %s (%s%% a subir) | melhores: %s", len(results),
                 time.time() - t0, (regime or {}).get("label", "sem dados"), (regime or {}).get("breadth_up_pct", "—"),
                 ", ".join(f"{r['symbol'].split('/')[0]} {r['score']}" for r in results[:5]) or "nenhuma")
        return results

    @staticmethod
    def regime(results: list, btc_trend: str | None) -> dict:
        """Clima do mercado: percentagem de moedas em tendência de alta e tendência do BTC/S&P 500."""
        if not results:
            return None
        n = len(results)
        up = sum(1 for r in results if r["trend"] in ("up", "strong_up"))
        above = sum(1 for r in results if r["above_ema50_higher"])
        avg_24h = sum((r["change_24h_pct"] or 0) for r in results) / n
        breadth_up = round(up / n * 100)
        breadth_above = round(above / n * 100)
        if (btc_trend in ("down", "strong_down") and breadth_above < 35) or breadth_above < 20:
            state = "risk_off"
        elif btc_trend in ("up", "strong_up") and breadth_above > 55:
            state = "risk_on"
        else:
            state = "neutral"
        labels = {"risk_on": "favorável", "neutral": "misto", "risk_off": "desfavorável"}
        return {"state": state, "label": labels[state], "breadth_up_pct": breadth_up,
                "breadth_above_ema50_pct": breadth_above, "avg_change_24h_pct": round(avg_24h, 2),
                "benchmark": config.benchmark_label(), "benchmark_trend": btc_trend}

    def candidates(self, exclude: set, limit: int | None = None) -> list[str]:
        """As melhores oportunidades acima da pontuação mínima (fora das que já estão a ser vistas)."""
        limit = config.SCAN_TOP if limit is None else limit
        out = []
        for r in self.last["results"]:
            if r["symbol"] in exclude or r["score"] < config.SCAN_MIN_SCORE or not r["setup"]:
                continue
            out.append(r["symbol"])
            if len(out) >= limit:
                break
        return out
