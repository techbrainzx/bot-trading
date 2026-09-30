"""
IA gestora de carteira: olha para as melhores candidatas do radar (com notícias, clima do mercado e
histórico de resultados) e escolhe em quais moedas o bot se deve concentrar agora.
"""
import json
import logging

import config

log = logging.getLogger("bot")

SELECT_SCHEMA = {
    "type": "object",
    "properties": {
        "market_stance": {"type": "string", "enum": ["aggressive", "normal", "defensive"]},
        "market_view": {"type": "string"},
        "picks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"symbol": {"type": "string"}, "reason": {"type": "string"}},
                "required": ["symbol", "reason"],
                "additionalProperties": False,
            },
        },
        "avoid": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"symbol": {"type": "string"}, "reason": {"type": "string"}},
                "required": ["symbol", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["market_stance", "market_view", "picks", "avoid"],
    "additionalProperties": False,
}

PROMPT = """You are the portfolio manager of an automated {direction} {market} bot (account in {quote}) on the {tf} timeframe.
A quantitative radar already scanned the most liquid instruments. Below are the best candidates with their metrics, detected setup and recent headlines, plus the market regime, the bot's track record and its open positions.

Choose up to {n} symbols that deserve a deep analysis RIGHT NOW for a possible {entry_kind} (a second AI will decide the exact entry, stop and target). Rank them best first.
Prefer: {prefer}
Avoid: hostile or scandal news (hacks, delistings, lawsuits, unlocks, profit warnings), earnings within 2 days, exhausted moves (RSI > 75 after a big pump; for shorts RSI < 25 after a big drop), symbols whose setup type keeps losing in the track record, and several highly correlated symbols with the same setup unless the market is clearly risk-on.
Coins/stocks with in_owner_watchlist=true were added by the owner: give them a fair look (never pick them without a real setup).
If the market regime is risk_off, be selective (fewer picks, prefer relative strength and oversold reversals with confirmation). If nothing is worth it, return an empty picks list.
market_stance: aggressive (broad risk-on), normal, or defensive (risk-off / event risk).
Write market_view and every reason in European Portuguese, one short sentence each.

DATA:
{data}"""


class AISelector:
    def __init__(self, client, model: str, on_usage=None):
        self.client = client
        self.model = model
        self.on_usage = on_usage

    @staticmethod
    def compact(r: dict, headlines: list) -> dict:
        s = r.get("setup") or {}
        return {
            "symbol": r["symbol"], "score": r["score"], "in_owner_watchlist": bool(r.get("mine")),
            "setup": f"{s.get('label')} ({s.get('order')}, R:R {s.get('risk_reward')})" if s else None,
            "other_setups": r.get("other_setups"), "trend": r["trend"], "higher_trend": r["higher_trend"],
            "rsi": r["rsi"], "adx": r["adx"], "atr_pct": r["atr_pct"], "change_24h_pct": r["change_24h_pct"],
            "ret_7d_pct": r.get("ret_7d_pct"), "rel_strength_vs_btc": r["rel_strength_vs_btc"],
            "divergence": r.get("divergence"), "patterns": r.get("patterns"),
            "volume_24h_millions": round((r.get("volume_24h") or 0) / 1e6, 1),
            "headlines_24h": [f"[{h['age_h']}h] {h['title']}" for h in headlines[:3]],
        }

    @staticmethod
    def market_text() -> dict:
        if config.MARKET == "cfd":
            shorts = config.CFD_ALLOW_SHORT
            return {"direction": "LONG AND SHORT" if shorts else "LONG-ONLY",
                    "market": "CFD (gold, silver, forex, stock indices, oil)",
                    "entry_kind": "long or short entry (the setup order says which: BUY* = long, SHORT* = short)" if shorts
                    else "long entry",
                    "prefer": ("a clear setup with good reward/risk in the direction of the higher-timeframe trend, "
                               "a macro backdrop that supports the direction (dollar, yields, risk sentiment, central "
                               "banks), healthy volatility and no high-impact event for that currency within 2 hours. "
                               "Spread the picks across different drivers (e.g. not three USD pairs betting on the same "
                               "dollar move).")}
        base = ("a clear setup with good reward/risk, strength versus {bench}, a supportive higher-timeframe trend, "
                "healthy volatility (enough movement to beat ~0.3% round-trip costs), and neutral or positive news.")
        if config.MARKET == "stocks":
            return {"direction": "LONG-ONLY", "market": "stock and UCITS ETF", "entry_kind": "long entry",
                    "prefer": base.format(bench="the S&P 500")}
        return {"direction": "LONG-ONLY", "market": "spot crypto", "entry_kind": "long entry",
                "prefer": base.format(bench="BTC")}

    def choose(self, candidates: list[dict], regime: dict, track_record: dict, positions: list,
               macro_events: list, headlines_for, n: int) -> dict:
        data = {
            "market_regime": regime,
            "upcoming_macro_events": macro_events[:6],
            "track_record": track_record,
            "open_positions": positions,
            "candidates": [self.compact(r, headlines_for(r["symbol"].split("/")[0])) for r in candidates],
        }
        resp = self.client.responses.create(
            model=self.model,
            input=PROMPT.format(quote=config.account_currency(), tf=config.PRIMARY_TIMEFRAME, n=n, **self.market_text(),
                                data=json.dumps(data, ensure_ascii=False, default=str)),
            text={"format": {"type": "json_schema", "name": "selection", "schema": SELECT_SCHEMA, "strict": True}},
        )
        if self.on_usage and resp.usage:
            self.on_usage(self.model, resp.usage.input_tokens, resp.usage.output_tokens)
        out = json.loads(resp.output_text)
        valid = {r["symbol"] for r in candidates}
        by_base = {r["symbol"].split("/")[0]: r["symbol"] for r in candidates}
        picks, seen = [], set()
        for p in out.get("picks", []):
            sym = p["symbol"] if p["symbol"] in valid else by_base.get(p["symbol"].split("/")[0].upper())
            if sym and sym not in seen:
                seen.add(sym)
                picks.append({"symbol": sym, "reason": p["reason"]})
        out["picks"] = picks[:n]
        return out
