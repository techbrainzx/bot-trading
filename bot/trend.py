"""
Estratégia de tendência para ETFs (longo prazo, sem IA).

Baseada em investigação com décadas de dados:
- Momentum de série temporal (Moskowitz, Ooi & Pedersen, 2012): ativos que subiram nos últimos 12 meses
  tendem a continuar a subir no mês seguinte.
- Filtro de tendência com média móvel (Faber, 2007): estar investido só acima da média de ~200 dias
  reduz muito as grandes quedas.

Regras:
1. Para cada ETF calcula o momentum médio de 1, 3, 6 e 12 meses.
2. É elegível se o preço está acima da média de 200 dias E o momentum é melhor do que o do "dinheiro" (XEON).
3. Fica com os N melhores elegíveis em partes iguais; o que sobra vai para o ETF de "dinheiro".
4. Reequilibra no início de cada mês (ou semana); vende logo um ETF que feche abaixo da média de 200 dias.
"""
import logging
from datetime import datetime, timezone

import config

log = logging.getLogger("bot")

LOOKBACKS = {"1m": 21, "3m": 63, "6m": 126, "12m": 252}


class TrendStrategy:
    def __init__(self, md):
        self.md = md

    def scores(self) -> list[dict]:
        universe = list(dict.fromkeys(config.TREND_UNIVERSE + [config.TREND_CASH_ETF]))
        self.md.prefetch(universe, ["1d"])
        rows, cash_mom = [], 0.0
        for sym in universe:
            try:
                df = self.md.ohlcv(sym, "1d", 400, closed_only=False)
            except Exception as e:
                log.warning("Tendência: sem dados de %s (%s)", sym, e)
                continue
            c = df["close"]
            if len(c) < max(LOOKBACKS.values()) + 5:
                continue
            price = float(c.iloc[-1])
            sma = float(c.tail(config.TREND_SMA_DAYS).mean())
            rets = {k: (price / float(c.iloc[-n - 1]) - 1) * 100 for k, n in LOOKBACKS.items()}
            mom = sum(rets.values()) / len(rets)
            vol = float(c.pct_change().tail(63).std() * (252 ** 0.5) * 100)
            row = {"symbol": sym, "price": price, "sma200": sma, "above_sma": price > sma,
                   "dist_sma_pct": round((price / sma - 1) * 100, 2), "momentum": round(mom, 2),
                   "returns": {k: round(v, 2) for k, v in rets.items()}, "volatility_pct": round(vol, 1)}
            if sym == config.TREND_CASH_ETF:
                cash_mom = mom
                row["is_cash"] = True
            rows.append(row)
        for r in rows:
            r["eligible"] = bool(not r.get("is_cash") and r["above_sma"] and r["momentum"] > cash_mom)
        rows.sort(key=lambda r: (not r.get("is_cash"), r["eligible"], r["momentum"]), reverse=True)
        return rows

    def targets(self, rows: list) -> dict:
        """{símbolo: peso} — os N melhores elegíveis em partes iguais, o resto em 'dinheiro'."""
        n = max(1, config.TREND_TOP_N)
        picks = [r["symbol"] for r in rows if r["eligible"]][:n]
        weights = {s: 1 / n for s in picks}
        cash_w = 1 - len(picks) / n
        if cash_w > 1e-9:
            weights[config.TREND_CASH_ETF] = cash_w
        return weights

    @staticmethod
    def rebalance_due(last_iso: str | None, now: datetime) -> bool:
        if not last_iso:
            return True
        last = datetime.fromisoformat(last_iso)
        if config.TREND_REBALANCE == "weekly":
            return now.isocalendar()[:2] != last.isocalendar()[:2]
        return (now.year, now.month) != (last.year, last.month)
