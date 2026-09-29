"""Motor do bot em tempo real: radar -> dados -> notícias -> IA -> risco -> execução."""
import logging
import time
from datetime import datetime, timezone

from openai import OpenAI

import config
from . import indicators as ind
from .analysis import btc_context, build_context, detect_setups
from .brain import ENTRY_ACTIONS, Decision, TradingBrain
from .broker import ExchangeBroker, PaperBroker
from .journal import Journal
from .market import MarketData
from .news import NewsHub
from .risk import RiskManager
from .scanner import Scanner
from .selector import AISelector
from .state import State
from .trader import Trader, iso, performance, setup_score_adjust, trade_stats

log = logging.getLogger("bot")

MODE_LABELS = {"paper": "MODO TESTE", "testnet": "MODO REAL (TESTNET)", "live": "MODO REAL"}


class ConfigError(Exception):
    """Configuração em falta ou inválida (mensagem pronta a mostrar ao utilizador)."""


def check_ready(mode: str):
    if mode not in MODE_LABELS:
        raise ConfigError(f"Modo inválido: {mode}")
    if not config.OPENAI_API_KEY:
        raise ConfigError("Falta a chave da OpenAI (Definições > Chaves API).")
    key, secret = config.exchange_keys(mode)
    if mode != "paper" and not (key and secret):
        raise ConfigError("Faltam as chaves da Binance para o Modo Real.")
    if mode == "live" and not config.LIVE_CONFIRMED:
        raise ConfigError("O Modo Real ainda não foi confirmado na interface.")


def start_cash_for(mode: str) -> float:
    return config.PAPER_START_BALANCE if mode == "paper" else config.LIVE_CAPITAL_USDT


class Engine:
    def __init__(self, mode: str | None = None, md: MarketData | None = None):
        self.mode = mode or config.mode_key()
        check_ready(self.mode)
        self.md = md or MarketData(config.EXCHANGE)
        self.state = State(config.state_path(self.mode), start_cash_for(self.mode))
        self.client = OpenAI(api_key=config.OPENAI_API_KEY, max_retries=3, timeout=300)
        self.brain = TradingBrain(self.client, config.DECISION_MODEL, config.REASONING_EFFORT,
                                  config.DECISION_VOTES, on_usage=self._usage,
                                  fallback_model=config.DECISION_FALLBACK_MODEL)
        self.news = NewsHub(self.client if config.NEWS_ENABLED else None, config.NEWS_MODEL,
                            config.NEWS_REFRESH_MINUTES, on_usage=self._usage)
        self.scanner = Scanner(self.md, perf_adjust=lambda setup: setup_score_adjust(self.state.data["trades"], setup))
        self.selector = AISelector(self.client, config.SELECTOR_MODEL, on_usage=self._usage)
        if self.mode == "paper":
            broker = PaperBroker()
        else:
            key, secret = config.exchange_keys(self.mode)
            broker = ExchangeBroker(config.EXCHANGE, key, secret, sandbox=self.mode == "testnet")
            self._sync_capital()
        self.journal = Journal(config.LOGS_DIR, prefix=f"{self.mode}_")
        self.trader = Trader(self.state, broker, RiskManager(), self.journal)
        self._btc_cache = (None, None)
        self._last_protect = 0.0
        self._last_equity_point = 0.0
        self._last_balance_check = 0.0
        self.exchange_usdt = None
        self.activity = "Pronto"
        self._limit_warned = None
        self.on_activity = None  # chamado quando a atividade muda (a interface atualiza logo)

    def set_activity(self, text: str):
        self.activity = text
        if self.on_activity:
            self.on_activity()

    def _usage(self, model, tokens_in, tokens_out):
        self.state.add_usage(model, tokens_in, tokens_out)

    def ai_calls_today(self) -> int:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return sum(u["calls"] for u in self.state.data["usage"].get(day, {}).values())

    def ai_budget_ok(self) -> bool:
        limit = config.AI_DAILY_CALL_LIMIT
        if not limit or self.ai_calls_today() < limit:
            return True
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._limit_warned != day:
            self._limit_warned = day
            log.warning("Limite diário de %d análises da IA atingido. Até às 00:00 UTC o bot só gere as posições "
                        "com as regras automáticas (stops, alvos, trailing).", limit)
        return False

    def _sync_capital(self):
        """Ajusta o saldo interno se o capital máximo do Modo Real mudou (como um depósito/levantamento)."""
        s = self.state.data
        old = s.get("capital_limit", s["start_cash"])
        delta = config.LIVE_CAPITAL_USDT - old
        if abs(delta) > 1e-9:
            if s["cash"] + delta < 0:
                raise ConfigError("O novo capital é menor do que o valor já investido em posições abertas.")
            for k in ("cash", "start_cash", "equity_peak", "day_start_equity"):
                s[k] += delta
            log.info("Capital do bot ajustado de %.2f para %.2f %s", old, config.LIVE_CAPITAL_USDT, config.QUOTE)
        s["capital_limit"] = config.LIVE_CAPITAL_USDT
        self.state.save()

    @staticmethod
    def now() -> datetime:
        return datetime.now(timezone.utc)

    def prices(self, symbols) -> dict:
        symbols = sorted(set(symbols))
        if not symbols:
            return {}
        try:
            tick = self.md.tickers(symbols)
        except Exception as e:
            # um par retirado/inválido não pode impedir a vigilância dos outros: pede um a um
            log.warning("Falha a obter preços em bloco (%s). A pedir um a um.", str(e)[:120])
            tick = {}
            for s in symbols:
                try:
                    tick[s] = self.md.ex.fetch_ticker(s)
                except Exception as e2:
                    log.error("Sem preço para %s: %s", s, str(e2)[:120])
        return {s: float(t["last"]) for s, t in tick.items() if t and t.get("last")}

    # ------------------------------------------------------------------ proteção (a cada 20 s)
    def protect_cycle(self):
        watch = set(self.trader.positions) | set(self.trader.pending)
        prices = self.prices(watch)
        now = self.now()
        for symbol in list(self.trader.positions):
            price = prices.get(symbol)
            if price:
                self.trader.check_exit(symbol, price, price, price, now)
        for symbol in list(self.trader.pending):
            price = prices.get(symbol)
            if price:
                self.trader.check_pending(symbol, price, price, price, now, prices)
        equity = self.trader.equity(prices)
        self.trader.update_risk_state(equity, now)
        if time.time() - self._last_equity_point >= config.EQUITY_POINT_SECONDS:
            self._last_equity_point = time.time()
            self.state.record_equity(equity)
        if self.mode != "paper" and time.time() - self._last_balance_check >= 60:
            self._last_balance_check = time.time()
            try:
                self.exchange_usdt = self.trader.broker.available_quote()
            except Exception as e:
                log.warning("Não foi possível ler o saldo da Binance: %s", e)
        self.state.save()

    # ------------------------------------------------------------------ decisão (a cada vela)
    def _frames(self, symbol, timeframes) -> dict:
        return {tf: self.md.ohlcv(symbol, tf, config.CANDLES) for tf in timeframes}

    def _btc(self, candle_key):
        """(resumo do BTC, velas do BTC no timeframe principal) — com cache por vela."""
        key, data = self._btc_cache
        if key == candle_key and data:
            return data
        try:
            frames = self._frames(f"BTC/{config.QUOTE}", [config.PRIMARY_TIMEFRAME, config.CONTEXT_TIMEFRAMES[1]])
            data = (btc_context(frames), frames[config.PRIMARY_TIMEFRAME])
        except Exception as e:
            log.warning("Contexto BTC indisponível: %s", e)
            data = (None, None)
        self._btc_cache = (candle_key, data)
        return data

    def select_symbols(self, now) -> tuple[list, list]:
        """(símbolos a analisar a fundo, os que vieram do radar). Guarda o 'foco' atual para a interface."""
        positions = list(self.trader.positions)
        focus = {"time": iso(now), "auto": config.AUTO_SELECT, "picks": [], "avoid": [], "stance": None,
                 "view": None, "regime": None, "source": None}
        results = []
        if config.AUTO_SELECT or config.SCANNER_ENABLED:
            self.set_activity("Radar: a analisar o mercado...")
            try:
                results = self.scanner.scan()
                focus["regime"] = self.scanner.last["regime"]
            except Exception as e:
                log.warning("Radar falhou: %s", e)

        if config.AUTO_SELECT:
            cands = [r for r in results if r["setup"] and r["score"] >= config.SCAN_MIN_SCORE
                     and r["symbol"] not in positions][: config.SELECTOR_CANDIDATES]
            picks = []
            if cands and self.ai_budget_ok():
                self.set_activity("IA gestora: a escolher as melhores moedas...")
                try:
                    sel = self.selector.choose(
                        cands, focus["regime"] or {}, performance(self.state.data["trades"]), positions,
                        self.news.calendar(), lambda base: self.news.headlines(base, hours=24, limit=3),
                        config.SCAN_TOP)
                    picks = sel["picks"]
                    focus.update(stance=sel["market_stance"], view=sel["market_view"], avoid=sel["avoid"][:6],
                                 source="ia")
                except Exception as e:
                    log.warning("IA gestora falhou (%s). A usar a ordem do radar.", e)
            if not focus["source"]:
                picks = [{"symbol": r["symbol"], "reason": f"{r['setup']['label']} (pontuação {r['score']})"}
                         for r in cands[: config.SCAN_TOP]]
                focus["source"] = "radar"
            by_sym = {r["symbol"]: r for r in results}
            focus["picks"] = [{**p, "score": by_sym.get(p["symbol"], {}).get("score"),
                               "setup": (by_sym.get(p["symbol"], {}).get("setup") or {}).get("label")} for p in picks]
            extra = [p["symbol"] for p in picks]
            symbols = positions + [s for s in extra if s not in positions]
            if focus["view"]:
                log.info("IA gestora (%s): %s | escolhidas: %s", focus["stance"], focus["view"],
                         ", ".join(s.split("/")[0] for s in extra) or "nenhuma")
        else:
            fixed = list(dict.fromkeys(positions + config.SYMBOLS))
            extra = self.scanner.candidates(set(fixed)) if results else []
            symbols = fixed + extra
            focus["picks"] = [{"symbol": s, "reason": "par fixo" if s in config.SYMBOLS else "radar"} for s in symbols]
            focus["source"] = "manual"
        self.state.data["focus"] = focus
        return symbols, extra

    def decision_cycle(self, between=None):
        """between(): chamado entre pares; se devolver False o ciclo é interrompido."""
        now = self.now()
        try:
            symbols, extra = self.select_symbols(now)
            prices = self.prices(set(symbols) | set(self.trader.positions))
            equity = self.trader.equity(prices)
            self.trader.update_risk_state(equity, now)
            self.state.record_equity(equity)
            self._last_equity_point = time.time()
            log.info("=== Análise | %s | capital %.2f %s | posições: %s | a analisar: %s ===",
                     MODE_LABELS[self.mode], equity, config.QUOTE, ", ".join(self.trader.positions) or "nenhuma",
                     ", ".join(s.split("/")[0] + ("*" if s in extra else "") for s in symbols) or "nada")
            self.set_activity("A recolher notícias e calendário...")
            macro = self.news.macro()
            analyzed, skipped = [], []
            for symbol in symbols:
                if between and between() is False:
                    log.info("Análise interrompida.")
                    break
                self.set_activity(f"A analisar {symbol}...")
                try:
                    done = self.process_symbol(symbol, prices, now, macro, from_scanner=symbol in extra)
                    (analyzed if done else skipped).append(symbol)
                except Exception:
                    log.exception("Erro a processar %s", symbol)
            self.state.data["last_cycle"] = {"time": iso(now), "analyzed": analyzed, "skipped": skipped,
                                             "from_scanner": extra}
        finally:
            self.set_activity("À espera da próxima vela")
            self.state.save()

    def process_symbol(self, symbol, prices, now, macro, from_scanner=False) -> bool:
        """Analisa um par. Devolve False se a IA não foi consultada (sem setup técnico)."""
        timeframes = [config.PRIMARY_TIMEFRAME] + config.CONTEXT_TIMEFRAMES
        frames = self._frames(symbol, timeframes)
        primary = frames[config.PRIMARY_TIMEFRAME]
        price = prices.get(symbol) or float(primary["close"].iloc[-1])
        atr = float(ind.atr(primary).iloc[-1])
        setups = detect_setups(primary, frames.get(config.CONTEXT_TIMEFRAMES[1]))

        busy = symbol in self.trader.positions or symbol in self.trader.pending
        if config.ONLY_WITH_SETUP and not busy and not setups["setups"]:
            log.info("%s @ %.6g -> sem setup técnico (pontuação %d): IA não consultada", symbol, price, setups["score"])
            self.state.record_decision(symbol, {
                "time": iso(now), "price": price, "action": "SKIP", "confidence": 0, "skipped": True,
                "reasoning": f"Nenhum setup técnico detetado (pontuação {setups['score']}/100). "
                             "A IA não foi consultada para poupar custos.",
                "outcome": "sem setup técnico",
            })
            return False

        if not self.ai_budget_ok():
            return False

        ob = self.md.orderbook_stats(symbol)
        market = {**ob, **self.md.derivatives(symbol), "fear_greed": self.md.fear_greed()}
        regime = (self.state.data.get("focus") or {}).get("regime")
        market["market_regime"] = regime
        self.set_activity(f"A ler notícias de {symbol.split('/')[0]}...")
        news = self.news.for_symbol(symbol, macro, use_ai=config.NEWS_ENABLED)
        btc_summary, btc_frame = (None, None)
        if not symbol.startswith("BTC/"):
            btc_summary, btc_frame = self._btc(int(primary["ts"].iloc[-1]))
        ctx = build_context(
            symbol,
            frames,
            price,
            iso(now),
            market=market,
            news=news,
            btc=btc_summary,
            position=self.trader.position_context(symbol, price, now),
            portfolio=self.trader.portfolio_context(prices),
            recent_decisions=self.trader.recent_decisions(symbol),
            recent_trades=self.trader.recent_trades(symbol),
            pending_order=self.trader.pending_context(symbol, price),
            macro=macro,
            btc_frame=btc_frame,
            setups=setups,
        )
        ctx["found_by_scanner"] = from_scanner
        ctx["track_record"] = performance(self.state.data["trades"])
        pick = next((p for p in (self.state.data.get("focus") or {}).get("picks", []) if p["symbol"] == symbol), None)
        if pick:
            ctx["why_selected"] = pick.get("reason")

        self.set_activity(f"A IA está a analisar {symbol}...")
        t0 = time.time()
        try:
            d = self.brain.decide(ctx)
        except Exception as e:
            log.error("%s: falha na IA (%s). Nada é feito.", symbol, e)
            d = Decision.hold(f"erro na IA: {e}")
        elapsed = time.time() - t0

        setup_name = setups["setups"][0]["setup"] if setups["setups"] else None
        need = config.MIN_CONFIDENCE + config.RISK_OFF_EXTRA_CONFIDENCE
        if regime and regime.get("state") == "risk_off" and d.action in ENTRY_ACTIONS \
                and symbol not in self.trader.positions and d.confidence < need:
            outcome = f"bloqueado: mercado desfavorável, exige confiança ≥ {need:.2f}"
        else:
            outcome = self.trader.apply_decision(symbol, d, price, atr, ob.get("spread_pct"), prices, now, setup_name)
        log.info("%s @ %.6g -> %s (conf %.2f, %s, %.0fs) => %s",
                 symbol, price, d.action, d.confidence, d.votes, elapsed, outcome)
        if d.reasoning:
            log.info("   motivo: %s", d.reasoning)

        ai_news = (news or {}).get("ai_summary") or {}
        self.state.record_decision(symbol, {
            "time": iso(now), "price": price, "action": d.action, "confidence": round(d.confidence, 2),
            "reasoning": d.reasoning[:700], "outcome": outcome, "regime": d.market_regime,
            "news": ai_news.get("sentiment_label"), "setup": (setups["setups"][0]["label"] if setups["setups"] else None),
            "entry_price": d.entry_price, "stop_loss": d.stop_loss, "take_profit": d.take_profit,
        })
        self.journal.decision({"mode": self.mode, "symbol": symbol, "price": price, "decision": d.to_dict(),
                               "outcome": outcome, "context": ctx})
        return True

    # ------------------------------------------------------------------ ciclo
    def tf_ms(self) -> int:
        return self.md.tf_ms(config.PRIMARY_TIMEFRAME)

    def due_candle(self) -> int | None:
        tf_ms = self.tf_ms()
        now_ms = int(time.time() * 1000)
        last_closed = (now_ms // tf_ms) * tf_ms - tf_ms
        due = now_ms >= last_closed + tf_ms + config.DECISION_DELAY_SECONDS * 1000
        if due and self.state.data.get("last_decision_candle") != last_closed:
            return last_closed
        return None

    def next_decision_ms(self) -> int:
        tf_ms = self.tf_ms()
        now_ms = int(time.time() * 1000)
        if self.due_candle() is not None:
            return now_ms
        return (now_ms // tf_ms + 1) * tf_ms + config.DECISION_DELAY_SECONDS * 1000

    def tick(self, between=None):
        if time.time() - self._last_protect >= config.CHECK_INTERVAL_SECONDS:
            self._last_protect = time.time()
            self.protect_cycle()
        candle = self.due_candle()
        if candle is not None:
            try:
                self.decision_cycle(between)
            finally:
                self.state.data["last_decision_candle"] = candle
                self.state.save()

    def run_forever(self):
        coins = "escolha automática" if config.AUTO_SELECT else ", ".join(config.SYMBOLS)
        log.info("Bot iniciado | %s | moedas: %s | timeframe %s | modelo %s",
                 MODE_LABELS[self.mode], coins, config.PRIMARY_TIMEFRAME, config.DECISION_MODEL)
        while True:
            try:
                self.tick()
            except Exception:
                log.exception("Erro no ciclo principal (o bot continua)")
            time.sleep(1)

    def run_once(self):
        self.protect_cycle()
        self.decision_cycle()

    def close_all(self):
        if not self.trader.positions:
            return
        prices = self.prices(self.trader.positions)
        for symbol in list(self.trader.positions):
            price = prices.get(symbol) or self.trader.positions[symbol]["entry_price"]
            self.trader.close_position(symbol, "manual", price, self.now())

    # ------------------------------------------------------------------ texto (linha de comandos)
    def status_text(self) -> str:
        s = self.state.data
        q = config.QUOTE
        prices = self.prices(set(config.SYMBOLS) | set(self.trader.positions))
        eq = self.trader.equity(prices)
        start = s["start_cash"]
        lines = [
            f"{MODE_LABELS[self.mode]}   Perfil: {config.PROFILE}   Modelo: {config.DECISION_MODEL} ({config.REASONING_EFFORT})",
            f"Capital: {eq:,.2f} {q}   (inicial {start:,.2f} | {(eq / start - 1) * 100:+.2f}%)",
            f"Saldo livre: {s['cash']:,.2f} {q}   Pico: {s['equity_peak']:,.2f}   "
            f"Drawdown: {(eq / s['equity_peak'] - 1) * 100:.2f}%",
        ]
        if s["halted"]:
            lines.append(f"!!! ENTRADAS BLOQUEADAS: {s['halt_reason']}")
        for sym, p in self.trader.positions.items():
            px = prices.get(sym, p["entry_price"])
            lines.append(f"  {sym:<10} qty {p['qty']:.6g} | entrada {p['entry_price']:.6g} | atual {px:.6g} "
                         f"({(px / p['entry_price'] - 1) * 100:+.2f}%) | stop {p['stop']:.6g} | TP {p['take_profit']:.6g}")
        for sym, o in self.trader.pending.items():
            lines.append(f"  pendente {sym:<10} {o['type']} a {o['trigger']:.6g} | stop {o['stop']:.6g} | até {o['expires'][:16]}")
        st = trade_stats(s["trades"])
        if st["count"]:
            lines.append(f"Trades: {st['count']} | acerto {st['win_rate']:.0f}% | PnL {st['pnl']:+.2f} {q} | "
                         f"R médio {st['avg_r']:+.2f}")
        return "\n".join(lines)
