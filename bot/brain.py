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
LONG_ENTRY_ACTIONS = ("BUY", "BUY_LIMIT", "BUY_STOP")
SHORT_ENTRY_ACTIONS = ("SHORT", "SHORT_LIMIT", "SHORT_STOP")
ENTRY_ACTIONS = LONG_ENTRY_ACTIONS + SHORT_ENTRY_ACTIONS
CFD_ACTIONS = ("BUY", "BUY_LIMIT", "BUY_STOP", "SHORT", "SHORT_LIMIT", "SHORT_STOP", "CLOSE", "HOLD")


def market_actions() -> tuple:
    """Ações que a IA pode escolher no mercado atual."""
    if config.MARKET == "cfd":
        return CFD_ACTIONS if config.CFD_ALLOW_SHORT else ("BUY", "BUY_LIMIT", "BUY_STOP", "CLOSE", "HOLD")
    return ACTIONS

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

ACTIONS_SPOT = """- BUY       -> buy now at market. Only when there is no open position for this symbol.
- BUY_LIMIT -> pending order: buy automatically if price DROPS to entry_price (below the current price), e.g. a pullback to support/EMA. Valid for {pending} candles.
- BUY_STOP  -> pending order: buy automatically if price RISES to entry_price (above the current price), e.g. a breakout above resistance. Valid for {pending} candles.
- SELL      -> close the entire open position now (or cancel the pending order if there is no position).
- HOLD      -> do nothing (keeps an existing pending order; with a position you may raise the stop via new_stop_loss)."""

ACTIONS_CFD = """- BUY         -> open a LONG position now at market (profits if price rises). Only when there is no open position for this symbol.
- BUY_LIMIT   -> pending long: buy automatically if price DROPS to entry_price (below the current price), e.g. a pullback to support/EMA. Valid for {pending} candles.
- BUY_STOP    -> pending long: buy automatically if price RISES to entry_price (above the current price), e.g. a breakout above resistance. Valid for {pending} candles.
{short_lines}- CLOSE       -> close the entire open position now, long or short (or cancel the pending order if there is no position).
- HOLD        -> do nothing (keeps an existing pending order; with a position you may tighten the stop via new_stop_loss)."""

SHORT_LINES = """- SHORT       -> open a SHORT position now at market (sell first, profits if price FALLS). Only when there is no open position.
- SHORT_LIMIT -> pending short: sell automatically if price RISES to entry_price (above the current price), e.g. a rally into resistance/EMA in a downtrend. Valid for {pending} candles.
- SHORT_STOP  -> pending short: sell automatically if price DROPS to entry_price (below the current price), e.g. a breakdown below support. Valid for {pending} candles.
"""

SYSTEM_PROMPT = """You are the decision engine of {market_desc}. At every {tf} candle close you receive a JSON snapshot for one symbol and must choose exactly one action:

{actions}

{stance}

How to decide (professional swing/day trader; positive expectancy after costs is the goal, and sitting flat forever is also a failure):
1. quant_setups lists setups detected by rules (trend pullback, breakout, momentum, oversold bounce, squeeze{short_setups}, and suggested pending orders) with a pre-computed entry/stop/target and the order to use. Use them as a starting plan: confirm, adjust or reject them with the rest of the evidence (higher timeframes, volume, order book, positioning, news, macro).
2. Typical long setups: a) trend pullback to EMA20/EMA50/VWAP/support with momentum turning up; b) breakout above resistance / 20-bar high with volume; c) momentum continuation in a strong trend; d) oversold bounce with bullish divergence or a reversal candle (lower confidence).{short_typical}
3. If you would enter only when price reaches a level ("wait for a retest of X" or "a close above Y"), do NOT answer HOLD: place {pending_names} at that level with its stop and target. That is how waiting turns into trades.
4. Avoid: buying when RSI > 78 on both the primary and the next timeframe, price more than ~2.5 ATR above EMA20, strongly bearish higher timeframes without reversal evidence{short_avoid}, clearly hostile news, or a high-impact macro event (FOMC, CPI, PCE, NFP) within ~2 hours.
5. Costs: a round trip costs about {cost}%. Every entry MUST have a stop_loss beyond the level that invalidates the idea ({stop_rule}) and take_profit at a realistic target, with reward/risk = |take_profit - entry| / |entry - stop_loss| >= {rr} AFTER costs (so aim for at least ~{rr_gross} before costs, or the order is rejected).
6. Exits are automatic: at +{ptr}R half of the position is closed and the stop moves to break-even; an ATR trailing stop (wider in strong trends, tighter in ranges) and a structural stop {structure} start from +{trail}R; in a strong accelerating trend the target is not taken in full (the winner runs); trades with no progress after {tstop} candles are closed. Do not duplicate these rules; focus on whether the thesis still holds. With an open position: {exit_word} if the thesis is invalidated (structure break on a closing basis, strong momentum against the position with volume, major adverse news); otherwise HOLD and optionally tighten new_stop_loss to a logical level.
7. Evaluate every snapshot fresh. recent_decisions and recent_trades are context only: do not repeat HOLD just because you held before; if the market changed, act.
8. Calibration: confidence = probability that the action is right (for entries: probability that take_profit is hit before stop_loss, with the trailing stop protecting winners). 0.5 is a coin flip; above 0.8 should be rare. Entries execute only with confidence >= {min_conf}.
9. news.ai_summary is an AI digest with web search; news.headlines are raw recent headlines from {n_sources} {news_kind} sites; macro has upcoming high-impact economic events and global market data{market_data}.
{market_notes}10. market.market_regime is the market breadth (share of symbols in uptrends, benchmark trend): in risk_off be pickier with longs, in risk_on trends follow through more often. track_record shows the bot's recent results by setup type (lean away from setups that keep losing, trust the ones that work). why_selected is why the portfolio manager picked this coin.
11. null means unavailable; do not invent data.

Fill every field. entry_price only for pending orders ({pending_names}; null otherwise). stop_loss and take_profit only for entries (null otherwise). new_stop_loss only for HOLD with an open position (null otherwise). Prices are plain numbers. Write bull_case, bear_case, reasoning and invalidation in European Portuguese, concise (at most ~3 sentences each)."""


def decision_schema(actions: tuple) -> dict:
    schema = json.loads(json.dumps(DECISION_SCHEMA))
    schema["properties"]["action"]["enum"] = list(actions)
    return schema

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
        from .news import FEEDS, feeds_for
        stocks = config.MARKET == "stocks"
        cfd = config.MARKET == "cfd"
        shorts = cfd and config.CFD_ALLOW_SHORT
        self.actions = market_actions()
        self.schema = decision_schema(self.actions)
        if cfd:
            market_desc = (
                f"an automated CFD trading bot on cTrader (account in {config.CFD_CURRENCY}) for gold, silver, forex, "
                "stock indices and oil. It can go LONG" + (" or SHORT" if shorts else " only") + ". Positions use margin "
                "(leverage), but the risk manager sizes every trade from the stop distance, so leverage does not change "
                "the amount risked per trade. Overnight positions pay financing (swap) and positions are closed before "
                "the Friday close to avoid weekend gaps")
            market_notes = (
                "9b. CFDs are driven by macro: central banks (Fed, ECB, BoE, BoJ), inflation and jobs data, the US dollar "
                "(DXY), bond yields (US 10y), risk sentiment (VIX), geopolitics and, for oil, OPEC and inventories. "
                "macro.global_market has DXY, US 10y yield, VIX, S&P 500 futures, gold, oil and EUR/USD with 24h changes. "
                "Gold tends to rise when real yields and the dollar fall and in risk-off; stock indices fall in risk-off; "
                "forex pairs move on rate differentials and data surprises. Trade WITH the 4h/1d trend (shorts in "
                "downtrends, longs in uptrends); counter-trend trades need strong evidence (divergence, reversal pattern "
                "at a key level). market.fear_greed is the VIX. There are no funding/order-book/derivatives data here: "
                "rely on price action, volume (tick volume), macro and news. btc_context here is the S&P 500.\n")
        else:
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
        pending_names = "BUY_LIMIT/BUY_STOP" + ("/SHORT_LIMIT/SHORT_STOP" if shorts else "")
        actions_text = (ACTIONS_CFD.replace("{short_lines}", SHORT_LINES if shorts else "") if cfd else ACTIONS_SPOT)
        long_stop = (f"for longs BELOW entry: inside rules.valid_stop_loss_price_range when buying now; for pending orders "
                     f"between {config.STOP_MIN_ATR} and {config.STOP_MAX_ATR} ATR below entry_price")
        short_stop = (f"; for shorts ABOVE entry: inside rules.valid_stop_loss_price_range_short when shorting now, "
                      f"between {config.STOP_MIN_ATR} and {config.STOP_MAX_ATR} ATR above entry_price for pending shorts, "
                      "with take_profit BELOW entry")
        self.instructions = SYSTEM_PROMPT.format(
            actions=actions_text.format(pending=config.PENDING_ORDER_CANDLES),
            short_setups=", breakdowns and rally-fades for shorts" if shorts else "",
            short_typical=(" Typical short setups (mirror image): a) rally into EMA20/EMA50/VWAP/resistance in a downtrend "
                           "with momentum turning down; b) breakdown below support / 20-bar low with volume; c) momentum "
                           "continuation in a strong downtrend; d) overbought fade with bearish divergence or a reversal "
                           "candle (lower confidence).") if shorts else "",
            pending_names=pending_names,
            short_avoid=("; shorting when RSI < 22 on both timeframes, price more than ~2.5 ATR below EMA20 or strongly "
                         "bullish higher timeframes without reversal evidence") if shorts else "",
            stop_rule=long_stop + (short_stop if shorts else ""),
            structure=("under the last higher low (above the last lower high for shorts)" if shorts
                       else "under the last higher low"),
            exit_word="CLOSE" if cfd else "SELL",
            news_kind="financial and forex" if cfd else "financial" if stocks else "crypto",
            market_data=("" if (cfd or stocks) else
                         "; market has order book, funding, open interest, long/short ratio and taker buy/sell ratio "
                         "(crowded longs with weak taker buying = caution; negative funding with rising price = "
                         "short-squeeze fuel)"),
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
            n_sources=len(feeds_for(config.MARKET)),
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
            text={"format": {"type": "json_schema", "name": "trade_decision", "schema": self.schema, "strict": True}},
        )
        if resp.status != "completed":
            raise RuntimeError(f"resposta incompleta da OpenAI ({resp.status})")
        raw = json.loads(resp.output_text)
        usage = resp.usage
        if self.on_usage and usage:
            self.on_usage(model, usage.input_tokens, usage.output_tokens)
        action = raw.get("action") if raw.get("action") in self.actions else "HOLD"
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
