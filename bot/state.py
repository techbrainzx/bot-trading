"""Estado persistente do bot (saldo, posições, histórico) guardado em JSON."""
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

MAX_EQUITY_POINTS = 12_000


def fresh_state(cash: float) -> dict:
    return {
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "start_cash": cash,
        "cash": cash,
        "positions": {},
        "pending": {},
        "trades": [],
        "equity_peak": cash,
        "day": None,
        "day_start_equity": cash,
        "halted": False,
        "halt_reason": None,
        "cooldowns": {},
        "decisions": {},
        "last_decision_candle": None,
        "usage": {},
        "equity_history": [],
    }


class State:
    def __init__(self, path: Path | None, start_cash: float):
        self.path = Path(path) if path else None
        if self.path and self.path.exists():
            self.data = json.loads(self.path.read_text("utf-8"))
            for k, v in fresh_state(start_cash).items():
                self.data.setdefault(k, v)
        else:
            self.data = fresh_state(start_cash)
            self.save()

    @staticmethod
    def peek(path: Path, start_cash: float) -> dict:
        """Lê um estado sem o criar (para mostrar na interface)."""
        try:
            data = json.loads(Path(path).read_text("utf-8"))
            for k, v in fresh_state(start_cash).items():
                data.setdefault(k, v)
            return data
        except (OSError, ValueError):
            return fresh_state(start_cash)

    def save(self):
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, ensure_ascii=False, default=str), "utf-8")
        for attempt in range(10):
            try:
                os.replace(tmp, self.path)
                return
            except PermissionError:  # Windows: ficheiro aberto por outro processo por instantes
                time.sleep(0.05 * (attempt + 1))
        os.replace(tmp, self.path)

    def record_decision(self, symbol: str, entry: dict, keep: int = 10):
        hist = self.data["decisions"].setdefault(symbol, [])
        hist.append(entry)
        del hist[:-keep]

    def record_equity(self, equity: float):
        hist = self.data["equity_history"]
        hist.append([int(time.time() * 1000), round(equity, 2)])
        if len(hist) > MAX_EQUITY_POINTS:
            half = len(hist) // 2
            self.data["equity_history"] = hist[:half][::2] + hist[half:]

    def add_usage(self, model: str, tokens_in: int, tokens_out: int):
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        u = self.data["usage"].setdefault(day, {}).setdefault(model, {"calls": 0, "input": 0, "output": 0})
        u["calls"] += 1
        u["input"] += tokens_in
        u["output"] += tokens_out
