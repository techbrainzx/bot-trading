"""O 'cérebro': envia o retrato do mercado à OpenAI e recebe uma decisão estruturada."""
import json
import logging
import statistics
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass

import config

log = logging.getLogger("bot")

ACTIONS = ("BUY", "BUY_LIMIT", "BUY_STOP", "SELL", "HOLD")
ENTRY_ACTIONS = ("BUY", "BUY_LIMIT", "BUY_STOP")

PROFILE_STANCE = {
    "conservador": "TRADING PROFILE: CONSERVATIVE. Only take high-quality setups aligned with the 4h/1d trend. When in doubt, HOLD.",
    "equilibrado": "TRADING PROFILE: BALANCED. Take good setups with positive expectancy; you do not need perfection. "
                   "Use pending orders (BUY_LIMIT / BUY_STOP) whenever you would buy 'if price reaches X' instead of just waiting.",
    "agressivo": "TRADING PROFILE: ACTIVE. The owner wants the bot to trade actively with small, strictly controlled risk "
                 "(the risk manager caps every loss). Take any setup with positive expectancy, including momentum and "
                 "breakout trades on the primary timeframe without full higher-timeframe confirmation; accept a win rate "
                 "around 40-50% when the reward/risk is good. Prefer placing a pending order over answering HOLD.",
}
PROFILE_STANCE["personalizado"] = PROFILE_STANCE["equilibrado"]

SYSTEM_PROMPT = """You are the decision engine of {market_desc}. At every {tf} candle close you receive a JSON snapshot for one symbol and must choose exactly one action:

- BUY       -> buy now at market. Only when there is no open position for this symbol.
- BUY_LIMIT -> pending order: buy automatically if price DROPS to entry_price (below the current price), e.g. a pullback to support/EMA. Valid for {pending} candles.
- BUY_STOP  -> pending order: buy automatically if price RISES to entry_price (above the current price), e.g. a breakout above resistance. Valid for {pending} candles.
- SELL      -> close the entire open position now (or cancel the pending order if there is no position).
- HOLD      -> do nothing (keeps an existing pending order; with a position you may raise the stop via new_stop_loss).

{stance}

How to decide (professional swing/day trader; positive expectancy after costs is the goal, and sitting flat forever is also a failure):
1. quant_setups lists setups detected by rules (trend pullback, breakout, momentum, oversold bounce, squeeze, and suggested pending orders) with a pre-computed entry/stop/target. Use them as a starting plan: confirm, adjust or reject them with the rest of the evidence (higher timeframes, volume, order book, derivatives positioning, news, macro).
2. Typical long setups: a) trend pullback to EMA20/EMA50/VWAP/support with momentum turning up; b) breakout above resistance / 20-bar high with volume; c) momentum continuation in a strong trend; d) oversold bounce with bullish divergence or a reversal candle (lower confidence).
3. If you would buy only when price reaches a level ("wait for a retest of X" or "a close above Y"), do NOT answer HOLD: place BUY_LIMIT or BUY_STOP at that level with its stop and target. That is how waiting turns into trades.
4. Avoid: buying when RSI > 78 on both the primary and the next timeframe, price more than ~2.5 ATR above EMA20, strongly bearish higher timeframes without reversal evidence, clearly hostile news, or a high-impact macro event (FOMC, CPI, PCE, NFP) within ~2 hours.
5. Costs: a round trip costs about {cost}%. Every entry MUST have stop_loss below the level that invalidates the idea (inside rules.valid_stop_loss_price_range when buying now; for pending orders measured from entry_price, between {min_atr} and {max_atr} ATR below it) and take_profit at a realistic target, with (take_profit - entry) / (entry - stop_loss) >= {rr} AFTER costs (so aim for at least ~{rr_gross} before costs, or the order is rejected).
6. Exits are automatic: at +{ptr}R half of the position is sold and the stop moves to break-even; an ATR trailing stop (wider in strong trends, tighter in ranges) and a structural stop under the last higher low start from +{trail}R; in a strong accelerating trend the target is not sold in full (the winner runs); trades with no progress after {tstop} candles are closed. Do not duplicate these rules; focus on whether the thesis still holds. With an open position: SELL if the thesis is invalidated (structure break on a closing basis, strong bearish momentum with volume, major negative news); otherwise HOLD and optionally raise new_stop_loss to a logical higher low.
7. Evaluate every snapshot fresh. recent_decisions and recent_trades are context only: do not repeat HOLD just because you held before; if the market changed, act.
8. Calibration: confidence = probability that the action is right (for entries: probability that take_profit is hit before stop_loss, with the trailing stop protecting winners). 0.5 is a coin flip; above 0.8 should be rare. Entries execute only with confidence >= {min_conf}.
9. news.ai_summary is an AI digest with web search; news.headlines are raw recent headlines from {n_sources} crypto sites; macro has upcoming high-impact economic events and global market data; market has order book, funding, open interest, long/short ratio and taker buy/sell ratio (crowded longs with weak taker buying = caution; negative funding with rising price = short-squeeze fuel).
{market_notes}10. market.market_regime is the market breadth (share of coins in uptrends, BTC trend): in risk_off be pickier, in risk_on trends follow through more often. track_record shows the bot's recent results by setup type (lean away from setups that keep losing, trust the ones that work). why_selected is why the portfolio manager picked this coin.
11. null means unavailable; do not invent data.

Fill every field. entry_price only for BUY_LIMIT/BUY_STOP (null otherwise). stop_loss and take_profit only for entries (null otherwise). new_stop_loss only for HOLD with an open position (null otherwise). Prices are plain numbers. Write bull_case, bear_case, reasoning and invalidation in European Portuguese, concise (at most ~3 sentences each)."""

DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "market_regime": {
            "type": "string",
            "enum": ["trending_up", "trending_down", "ranging", "volatile_breakout", "unclear"],
        },
        "bull_case": {"type": "string"},
        "bear_case": {"type": "string"},
        "reasoning": {"type": "string"},
        "action": {"type": "string", "enum": list(ACTIONS)},
        "confidence": {"type": "number", "description": "0 a 1"},
        "entry_price": {"type": ["number", "null"]},
        "stop_loss": {"type": ["number", "null"]},
        "take_profit": {"type": ["number", "null"]},
        "new_stop_loss": {"type": ["number", "null"]},
        "expected_holding_hours": {"type": ["number", "null"]},
        "invalidation": {"type": "string"},
    },
    "required": [
        "market_regime", "bull_case", "bear_case", "reasoning", "action", "confidence", "entry_price",
        "stop_loss", "take_profit", "new_stop_loss", "expected_holding_hours", "invalidation",
    ],
    "additionalProperties": False,
}


@dataclass
class Decision:
    action: str
    confidence: float
    market_regime: str = "unclear"
    bull_case: str = ""
    bear_case: str = ""
    reasoning: str = ""
    entry_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    new_stop_loss: float | None = None
    expected_holding_hours: float | None = None
    invalidation: str = ""
    votes: str = "1/1"
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Decision":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    @classmethod
    def hold(cls, reason: str) -> "Decision":
        return cls(action="HOLD", confidence=0.0, reasoning=reason)


def _pos_num(x):
    try:
        x = float(x)
        return x if x > 0 else None
    except (TypeError, ValueError):
        return None


class TradingBrain:
    def __init__(self, client, model: str, effort: str, votes: int = 1, on_usage=None, fallback_model: str | None = None):
        self.client = client
        self.model = model
        self.fallback_model = fallback_model if fallback_model and fallback_model != model else None
        self.effort = effort
        self.votes = max(1, int(votes))
        self.on_usage = on_usage
        from .news import FEEDS
        stocks = config.MARKET == "stocks"
        market_desc = (
            f"an automated trading bot for US stocks and European UCITS ETFs (cash account in {config.STOCK_CURRENCY}, "
            "LONG ONLY, no shorting, no leverage), trading only during exchange hours" if stocks else
            f"an automated crypto trading bot that trades SPOT markets, LONG ONLY (no shorting, no leverage), in {config.QUOTE}")
        market_notes = (
            "9b. Stocks: market.fundamentals has the next earnings date (the bot blocks new entries right before earnings "
            "and exits positions the day before), valuation, analyst targets (weak signal), short interest and distance "
            "to the 52-week high; market.fear_greed is the VIX (above ~25 = fear: be more selective). Positions are held "
            "overnight, so prefer setups whose stop can survive a normal opening gap; respect sector and index trends "
            "(btc_context here is the S&P 500).\n" if stocks else "")
        self.instructions = SYSTEM_PROMPT.format(
            market_desc=market_desc,
            market_notes=market_notes,
            tf=config.PRIMARY_TIMEFRAME,
            pending=config.PENDING_ORDER_CANDLES,
            stance=PROFILE_STANCE.get(config.PROFILE, PROFILE_STANCE["equilibrado"]),
            cost=round(2 * (config.FEE_PCT + config.SLIPPAGE_PCT), 2),
            min_atr=config.STOP_MIN_ATR,
            max_atr=config.STOP_MAX_ATR,
            rr=config.MIN_RISK_REWARD,
            rr_gross=round(config.MIN_RISK_REWARD + 0.4, 1),
            min_conf=config.MIN_CONFIDENCE,
            be=config.BREAKEVEN_AT_R,
            trail=config.TRAIL_START_R,
            ptr=config.PARTIAL_TP_R,
            tstop=config.TIME_STOP_CANDLES,
            n_sources=len(FEEDS) if not stocks else 11,
        )

    def _ask(self, context: dict) -> Decision:
        try:
            return self._ask_model(context, self.model)
        except Exception as e:
            if not self.fallback_model:
                raise
            log.warning("%s falhou (%s). A usar o modelo de reserva %s", self.model, str(e)[:120], self.fallback_model)
            return self._ask_model(context, self.fallback_model)

    def _ask_model(self, context: dict, model: str) -> Decision:
        resp = self.client.responses.create(
            model=model,
            instructions=self.instructions,
            input=json.dumps(context, ensure_ascii=False, default=str),
            reasoning={"effort": self.effort},
            max_output_tokens=25_000,
            text={"format": {"type": "json_schema", "name": "trade_decision", "schema": DECISION_SCHEMA, "strict": True}},
        )
        if resp.status != "completed":
            raise RuntimeError(f"resposta incompleta da OpenAI ({resp.status})")
        raw = json.loads(resp.output_text)
        usage = resp.usage
        if self.on_usage and usage:
            self.on_usage(model, usage.input_tokens, usage.output_tokens)
        action = raw.get("action") if raw.get("action") in ACTIONS else "HOLD"
        return Decision(
            action=action,
            confidence=min(1.0, max(0.0, float(raw.get("confidence") or 0))),
            market_regime=raw.get("market_regime", "unclear"),
            bull_case=raw.get("bull_case", ""),
            bear_case=raw.get("bear_case", ""),
            reasoning=raw.get("reasoning", ""),
            entry_price=_pos_num(raw.get("entry_price")),
            stop_loss=_pos_num(raw.get("stop_loss")),
            take_profit=_pos_num(raw.get("take_profit")),
            new_stop_loss=_pos_num(raw.get("new_stop_loss")),
            expected_holding_hours=_pos_num(raw.get("expected_holding_hours")),
            invalidation=raw.get("invalidation", ""),
            model=model,
            input_tokens=usage.input_tokens if usage else 0,
            output_tokens=usage.output_tokens if usage else 0,
        )

    def decide(self, context: dict) -> Decision:
        if self.votes == 1:
            return self._ask(context)
        with ThreadPoolExecutor(max_workers=self.votes) as pool:
            results = list(pool.map(lambda _: self._safe_ask(context), range(self.votes)))
        results = [r for r in results if r is not None]
        if not results:
            raise RuntimeError("todas as chamadas à OpenAI falharam")
        return self._aggregate(results)

    def _safe_ask(self, context):
        try:
            return self._ask(context)
        except Exception as e:
            log.warning("Um dos votos falhou: %s", e)
            return None

    def _aggregate(self, ds: list[Decision]) -> Decision:
        n = self.votes
        action, count = Counter(d.action for d in ds).most_common(1)[0]
        tokens_in = sum(d.input_tokens for d in ds)
        tokens_out = sum(d.output_tokens for d in ds)
        if count <= n / 2:
            d = Decision.hold(f"Sem consenso entre {n} análises: " + ", ".join(x.action for x in ds))
            d.votes, d.model, d.input_tokens, d.output_tokens = f"{count}/{n}", self.model, tokens_in, tokens_out
            return d
        agree = [d for d in ds if d.action == action]

        def med(field):
            vals = [getattr(d, field) for d in agree if getattr(d, field) is not None]
            return statistics.median(vals) if vals else None

        best = max(agree, key=lambda d: d.confidence)
        best.confidence = statistics.mean(d.confidence for d in agree) * count / n
        best.stop_loss, best.take_profit, best.new_stop_loss = med("stop_loss"), med("take_profit"), med("new_stop_loss")
        best.entry_price = med("entry_price")
        best.votes, best.input_tokens, best.output_tokens = f"{count}/{n}", tokens_in, tokens_out
        return best
