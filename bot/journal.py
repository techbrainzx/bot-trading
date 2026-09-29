"""Registos: log do bot, diário de decisões (JSONL) e histórico de trades (CSV)."""
import csv
import json
import logging
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

TRADE_FIELDS = ["symbol", "opened_at", "closed_at", "hours", "entry_price", "exit_price", "qty",
                "pnl", "pnl_pct", "r_multiple", "reason", "setup"]


def setup_logging(logs_dir: Path, level=logging.INFO):
    logs_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)
    fh = RotatingFileHandler(logs_dir / "bot.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    for noisy in ("httpx", "httpx2", "httpcore", "httpcore2", "openai", "urllib3", "ccxt", "yfinance", "peewee"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)


class Journal:
    def __init__(self, logs_dir: Path, prefix: str = ""):
        self.dir = Path(logs_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.decisions_path = self.dir / f"{prefix}decisions.jsonl"
        self.trades_path = self.dir / f"{prefix}trades.csv"
        self.events_path = self.dir / f"{prefix}events.jsonl"

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def _append_json(self, path: Path, record: dict):
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def decision(self, record: dict):
        self._append_json(self.decisions_path, {"logged_at": self._now(), **record})

    def event(self, kind: str, symbol: str, data: dict):
        self._append_json(self.events_path, {"time": self._now(), "event": kind, "symbol": symbol, **data})

    def trade(self, trade: dict):
        new = not self.trades_path.exists()
        with self.trades_path.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=TRADE_FIELDS, extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerow(trade)
