"""
Controlador usado pela interface gráfica.

Todo o trading corre numa única thread ("worker"). A interface só envia comandos
(iniciar, parar, fechar posição, mudar de modo...) e lê um retrato do estado.
Assim nunca há duas threads a mexer nas posições ao mesmo tempo.
"""
import json
import logging
import queue
import threading
import time
from concurrent.futures import Future

import config
from .engine import MODE_LABELS, ConfigError, Engine, start_cash_for
from .market import MarketData
from .state import State

log = logging.getLogger("bot")


class Controller:
    def __init__(self):
        self.md: MarketData | None = None
        self.engine: Engine | None = None
        self.running = False
        self.error: str | None = None
        self.snapshot: dict = {}
        self._cmds: queue.Queue = queue.Queue()
        self._urgent: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._loop, name="bot-worker", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------------ API pública (qualquer thread)
    def submit(self, fn, *args, urgent: bool = False, wait: bool = True, timeout: float = 600):
        fut: Future = Future()
        (self._urgent if urgent else self._cmds).put((fn, args, fut))
        if not wait:
            fut.add_done_callback(lambda f: f.exception() and log.error("Comando falhou: %s", f.exception()))
            return None
        return fut.result(timeout)

    def stop(self):
        if self.running:
            self.running = False
            log.info("Bot parado. As posições abertas mantêm-se, mas só são vigiadas com o bot a correr.")

    # ------------------------------------------------------------------ worker
    def _loop(self):
        self._publish()
        while True:
            try:
                self._execute(*self._cmds.get(timeout=1.0))
            except queue.Empty:
                pass
            self._drain_urgent()
            if self.running and self.engine:
                try:
                    self.engine.tick(between=self._between)
                    self.error = None
                except Exception as e:
                    log.exception("Erro no ciclo do bot (continua a correr)")
                    self.error = str(e)[:300]
            self._publish()

    def _execute(self, fn, args, fut):
        try:
            fut.set_result(fn(*args))
        except Exception as e:
            fut.set_exception(e)
        self._publish()

    def _drain_urgent(self):
        while True:
            try:
                self._execute(*self._urgent.get_nowait())
            except queue.Empty:
                return

    def _between(self):
        self._drain_urgent()
        self._publish()
        return self.running

    def _ensure_engine(self) -> Engine:
        if self.md is None:
            self.md = MarketData(config.EXCHANGE)
        if self.engine is None or self.engine.mode != config.mode_key():
            self.engine = Engine(config.mode_key(), self.md)
            self.engine.on_activity = self._publish
        return self.engine

    def _publish(self):
        key = config.mode_key()
        eng = self.engine if self.engine and self.engine.mode == key else None
        data = eng.state.data if eng else State.peek(config.state_path(key), start_cash_for(key))
        activity = "Parado"
        if self.running and eng:
            activity = eng.activity
        next_ms = None
        if self.running and eng:
            try:
                next_ms = eng.next_decision_ms()
            except Exception:
                pass
        self.snapshot = {
            "mode": config.MODE,
            "mode_key": key,
            "mode_label": MODE_LABELS.get(key, key),
            "running": self.running,
            "activity": activity,
            "error": self.error,
            "next_decision_ms": next_ms,
            "exchange_usdt": eng.exchange_usdt if eng else None,
            "scan": eng.scanner.last if eng else None,
            "state": json.loads(json.dumps(data, default=str)),
            "published_at": time.time(),
        }

    # ------------------------------------------------------------------ comandos (correm no worker)
    def cmd_start(self):
        try:
            self._ensure_engine()
        except ConfigError as e:
            self.error = str(e)
            raise
        self.running = True
        self.error = None
        eng = self.engine
        coins = (f"escolha automática entre as {config.SCAN_UNIVERSE} mais negociadas" if config.AUTO_SELECT
                 else ", ".join(config.SYMBOLS))
        log.info("Bot iniciado | %s | moedas: %s | timeframe %s | perfil %s | modelo %s", MODE_LABELS[eng.mode],
                 coins, config.PRIMARY_TIMEFRAME, config.PROFILE, config.DECISION_MODEL)

    def cmd_analyze_now(self):
        eng = self._ensure_engine()
        was_running = self.running
        self.running = True  # permite que _between não interrompa
        try:
            eng.protect_cycle()
            eng.decision_cycle(self._between)
        finally:
            self.running = was_running and self.running

    def cmd_close(self, symbol: str):
        eng = self._ensure_engine()
        if symbol not in eng.trader.positions:
            raise ValueError(f"Não há posição aberta em {symbol}.")
        price = eng.prices([symbol]).get(symbol) or eng.trader.positions[symbol]["entry_price"]
        trade = eng.trader.close_position(symbol, "manual", price, eng.now())
        return trade

    def cmd_cancel_pending(self, symbol: str):
        eng = self._ensure_engine()
        if symbol not in eng.trader.pending:
            raise ValueError(f"Não há ordem pendente em {symbol}.")
        eng.trader.cancel_pending(symbol, "cancelada pelo utilizador")

    def cmd_close_all(self):
        eng = self._ensure_engine()
        eng.close_all()

    def cmd_resume(self):
        eng = self._ensure_engine()
        prices = eng.prices(eng.trader.positions)
        eq = eng.trader.equity(prices)
        eng.state.data.update(halted=False, halt_reason=None, equity_peak=eq)
        eng.state.save()
        log.info("Entradas desbloqueadas pelo utilizador (pico de capital reposto em %.2f).", eq)

    def cmd_reset_test(self, balance: float):
        if config.mode_key() != "paper":
            raise ValueError("Só é possível recomeçar no Modo Teste.")
        self.running = False
        config.save({"PAPER_START_BALANCE": balance})
        path = config.state_path("paper")
        if path.exists():
            path.unlink()
        if self.engine and self.engine.mode == "paper":
            self.engine = None
        log.info("Modo Teste recomeçado com %.2f USDT fictícios.", balance)

    def cmd_set_mode(self, mode: str, testnet: bool = False, capital: float | None = None):
        prev = config.current()
        self.stop()
        values = {"MODE": mode}
        if mode == "live":
            values.update(USE_TESTNET=bool(testnet), LIVE_CONFIRMED=True)
            if capital:
                values["LIVE_CAPITAL_USDT"] = float(capital)
        config.save(values)
        self.engine = None
        try:
            self._ensure_engine()
        except Exception:
            config.save(prev)
            self.engine = None
            raise
        log.warning("Mudado para %s.", MODE_LABELS[config.mode_key()])

    def cmd_reload(self):
        """Aplica novas definições (reconstrói o motor, mantém posições e histórico)."""
        if self.engine is None:
            return
        self.engine = None
        try:
            self._ensure_engine()
            log.info("Definições aplicadas.")
        except Exception as e:
            self.running = False
            self.error = str(e)[:300]
            raise
