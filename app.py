"""
Trading Bot IA - aplicação com interface gráfica.

  python app.py            -> abre a aplicação numa janela própria
  python app.py --browser  -> abre no browser em vez de janela
  python app.py --server   -> só o servidor (sem abrir nada)
"""
import argparse
import calendar
import json
import re
import logging
import secrets
import socket
import threading
import time
import webbrowser
from collections import deque

import uvicorn
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import config
from bot.broker import verify_keys
from bot.controller import Controller
from bot.engine import ConfigError, make_market_data
from bot.journal import setup_logging
from bot.news import NewsHub, feeds_for
from bot.t212 import verify_t212
from bot import assets as assets_mod
from bot import ctrader
from bot.risk import sgn, side_of
from bot.scanner import Scanner
from bot.trader import position_value, trade_stats

PORT = 8765
TOKEN = secrets.token_urlsafe(24)
WEB_DIR = config.BASE_DIR / "web"
log = logging.getLogger("bot")

MODEL_OPTIONS = [
    {"value": "gpt-5.5", "label": "GPT-5.5 (recomendado)"},
    {"value": "gpt-5.4", "label": "GPT-5.4"},
    {"value": "gpt-5.4-mini", "label": "GPT-5.4 mini (mais barato)"},
]
ASSET_OPTIONS = ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "LINK", "SUI", "NEAR", "AVAX", "LTC", "HBAR",
                 "DOT", "TRX", "UNI", "AAVE", "ONDO", "TAO", "ENA", "XLM", "PEPE"]
CHART_TFS = {"crypto": ["15m", "1h", "4h", "1d"], "stocks": ["15m", "1h", "1d", "1wk"], "cfd": ["15m", "1h", "4h", "1d"]}
TICKER_RX = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,11}$")
BOUNDS = {
    "RISK_PER_TRADE_PCT": (0.1, 5), "RISK_FIXED_AMOUNT": (0.05, 100_000), "MAX_POSITION_PCT": (5, 100),
    "MAX_OPEN_POSITIONS": (1, 10), "MIN_CONFIDENCE": (0.5, 0.95), "MIN_RISK_REWARD": (1.0, 5),
    "MAX_DAILY_LOSS_PCT": (0.5, 25), "MAX_DRAWDOWN_PCT": (2, 60), "COOLDOWN_AFTER_LOSS_MINUTES": (0, 1440),
    "NEWS_REFRESH_MINUTES": (15, 1440), "DECISION_VOTES": (1, 5), "PAPER_START_BALANCE": (20, 10_000_000),
    "LIVE_CAPITAL_USDT": (10, 10_000_000), "BREAKEVEN_AT_R": (0, 5), "TRAIL_START_R": (0, 10),
    "TRAIL_ATR_MULT": (0, 10), "SCAN_UNIVERSE": (5, 60), "SCAN_TOP": (0, 8), "SCAN_MIN_SCORE": (20, 90),
    "PENDING_ORDER_CANDLES": (1, 24), "AI_DAILY_CALL_LIMIT": (0, 5000),
    "PARTIAL_TP_R": (0.5, 3), "PARTIAL_TP_PCT": (0, 90), "TIME_STOP_CANDLES": (0, 500), "LOSS_STREAK_REDUCE": (0, 10),
    "EVENT_BLACKOUT_BEFORE_MIN": (0, 240), "EVENT_BLACKOUT_AFTER_MIN": (0, 240),
    "EARNINGS_BLACKOUT_DAYS": (0, 10), "TREND_TOP_N": (1, 6),
}
CHOICES = {
    "PRIMARY_TIMEFRAME": ["15m", "1h", "4h"],
    "DECISION_MODEL": [m["value"] for m in MODEL_OPTIONS],
    "REASONING_EFFORT": ["low", "medium", "high"],
    "QUOTE": ["USDC", "EUR", "USDT"],
    "PROFILE": ["conservador", "equilibrado", "agressivo", "personalizado"],
    "RISK_MODE": ["percent", "fixed"],
    "STOCK_STRATEGY": ["ativo", "tendencia"],
    "STOCK_CURRENCY": ["EUR", "USD"],
    "CFD_CURRENCY": ["EUR", "USD"],
    "TREND_REBALANCE": ["monthly", "weekly"],
}
BOOLS = ["NEWS_ENABLED", "SCANNER_ENABLED", "ONLY_WITH_SETUP", "AUTO_SELECT", "EXIT_BEFORE_EARNINGS",
         "CFD_ALLOW_SHORT", "CFD_CLOSE_BEFORE_WEEKEND"]
USER_SETTINGS = ["ASSETS", "STOCK_ASSETS", "CFD_ASSETS", *CHOICES, *BOOLS, *BOUNDS]


# ============================================================ logs em memória para a interface
class RingHandler(logging.Handler):
    def __init__(self, size=1500):
        super().__init__()
        self.lines = deque(maxlen=size)
        self.counter = 0

    def emit(self, record):  # logging.Handler.handle() já segura self.lock
        self.counter += 1
        self.lines.append({"id": self.counter, "t": record.created, "level": record.levelname,
                           "msg": record.getMessage()})

    def since(self, after: int):
        with self.lock:
            return [l for l in self.lines if l["id"] > after]


ring = RingHandler()
controller: Controller | None = None


# ============================================================ dados de mercado para a interface
class UIMarket:
    """Instância separada do ccxt para gráficos e preços (não interfere com o bot)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.mds: dict = {}
        self.cache: dict = {}

    def _md(self):
        md = self.mds.get(config.MARKET)
        if md is None:
            md = self.mds[config.MARKET] = make_market_data(config.MARKET)
        return md

    def cached(self, key, ttl, fn):
        key = (config.mode_key(),) + tuple(key)
        hit = self.cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        with self.lock:
            value = fn(self._md())
        self.cache[key] = (time.time(), value)
        return value

    def tickers(self, symbols):
        symbols = sorted(set(symbols))
        if not symbols:
            return {}
        try:
            return self.cached(("tickers", tuple(symbols)), 4 if config.MARKET == "crypto" else 15,
                               lambda md: md.tickers(symbols))
        except Exception as e:
            log.debug("tickers falhou: %s", e)
            hit = self.cache.get((config.mode_key(), "tickers", tuple(symbols)))
            return hit[1] if hit else {}

    def candles(self, symbol, tf):
        return self.cached(("candles", symbol, tf), 10 if config.MARKET == "crypto" else 60,
                           lambda md: md.ohlcv(symbol, tf, 400, closed_only=False))

    def markets(self):
        return self.cached(("markets",), 3600, lambda md: md.ex.markets)

    def has_symbol(self, symbol) -> bool:
        return symbol in self.markets() or self.cached(("has", symbol), 3600, lambda md: md.has_symbol(symbol))

    def market_status(self):
        try:
            return self.cached(("status",), 30, lambda md: md.market_status())
        except Exception:
            return []


ui = UIMarket()
_ui_news: dict = {}  # por mercado, sem IA: só manchetes, calendário e dados globais
ui_scan_lock = threading.Lock()
_ui_scanners: dict = {}


def ui_news() -> NewsHub:
    hub = _ui_news.get(config.MARKET)
    if hub is None:
        hub = _ui_news[config.MARKET] = NewsHub(market=config.MARKET)
    return hub

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def guard(request: Request, call_next):
    host = request.headers.get("host", "").rsplit(":", 1)[0]
    if host not in ("127.0.0.1", "localhost", "[::1]"):
        return JSONResponse({"detail": "host não permitido"}, status_code=403)
    if request.url.path.startswith("/api/") and request.headers.get("x-token") != TOKEN:
        return JSONResponse({"detail": "token inválido"}, status_code=403)
    return await call_next(request)


@app.exception_handler(ConfigError)
async def config_error(_request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=400)


@app.exception_handler(ValueError)
async def value_error(_request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=400)


@app.get("/", response_class=HTMLResponse)
def index():
    html = (WEB_DIR / "index.html").read_text("utf-8")
    for name in ("style.css", "app.js"):  # versão no URL: nunca usar ficheiros antigos em cache
        v = int((WEB_DIR / name).stat().st_mtime)
        html = html.replace(f"/static/{name}", f"/static/{name}?v={v}")
    return HTMLResponse(html.replace("__TOKEN__", TOKEN), headers={"Cache-Control": "no-store"})


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


def mask(key: str) -> str | None:
    return f"{key[:6]}…{key[-4:]}" if key and len(key) > 12 else None


def run(fn, *args, urgent=False, wait=True):
    try:
        return controller.submit(fn, *args, urgent=urgent, wait=wait)
    except (ConfigError, ValueError):
        raise
    except Exception as e:
        raise HTTPException(500, str(e)[:300])


# ============================================================ estado
@app.get("/api/status")
def status():
    snap = controller.snapshot
    st = snap["state"]
    positions = st["positions"]
    pending = st.get("pending", {})
    scan = latest_scan()
    focus_syms = [p["symbol"] for p in (st.get("focus") or {}).get("picks", [])]
    top = focus_syms or [r["symbol"] for r in (scan or {}).get("results", [])[:4] if r.get("setup")]
    fixed = [] if config.AUTO_SELECT else list(config.SYMBOLS)
    if config.MARKET == "stocks" and config.STOCK_STRATEGY == "tendencia":
        top = list(((st.get("trend") or {}).get("targets") or {}).keys()) or config.TREND_UNIVERSE[:4]
    default = {"stocks": config.STOCK_ASSETS[:4], "cfd": config.CFD_ASSETS[:4] or ["XAUUSD"]}.get(
        config.MARKET, [config.benchmark()])
    tab_syms = list(dict.fromkeys(list(positions) + top + list(pending) + fixed)) or default
    tick = ui.tickers(tab_syms)
    price = {s: float(t["last"]) for s, t in tick.items() if t.get("last")}

    pos_out = []
    invested = 0.0
    for sym, p in positions.items():
        px = price.get(sym, p["entry_price"])
        value = position_value(p, px)
        invested += value
        side = side_of(p)
        sg = sgn(side)
        risk_unit = sg * (p["entry_price"] - p["initial_stop"])
        pnl = value - p["entry_cost"]
        pnl_pct = (sg * (px / p["entry_price"] - 1) * 100 if p.get("cfd")
                   else pnl / p["entry_cost"] * 100 if p["entry_cost"] else 0)
        pos_out.append({
            "symbol": sym, "side": side, "qty": p["qty"], "entry_price": p["entry_price"], "price": px, "value": value,
            "margin": p.get("margin"), "notional": p["qty"] * px * p.get("fx", 1.0) if p.get("cfd") else None,
            "pnl": pnl, "pnl_pct": pnl_pct,
            "r": sg * (px - p["entry_price"]) / risk_unit if risk_unit > 0 else None,
            "stop": p["stop"], "initial_stop": p["initial_stop"], "take_profit": p["take_profit"],
            "opened_at": p["opened_at"], "confidence": p.get("confidence"), "reasoning": p.get("reasoning", ""),
            "setup": p.get("setup"), "partial_done": p.get("partial_done"), "let_run": p.get("let_run"),
            "rules_note": p.get("rules_note"),
        })
    equity = st["cash"] + invested
    start = st["start_cash"] or 1
    peak = max(st["equity_peak"], equity)
    day_base = st["day_start_equity"] or equity
    today = time.strftime("%Y-%m-%d", time.gmtime())

    return {
        "mode": snap["mode"], "mode_key": snap["mode_key"], "mode_label": snap["mode_label"],
        "running": snap["running"], "activity": snap["activity"], "error": snap["error"],
        "next_decision_ms": snap["next_decision_ms"], "server_ms": int(time.time() * 1000),
        "equity": equity, "cash": st["cash"], "start_cash": st["start_cash"],
        "pnl": equity - st["start_cash"], "pnl_pct": (equity / start - 1) * 100,
        "day_pnl_pct": (equity / day_base - 1) * 100 if st["day"] == today else 0.0,
        "drawdown_pct": (equity / peak - 1) * 100, "max_drawdown_pct": config.MAX_DRAWDOWN_PCT,
        "halted": st["halted"], "halt_reason": st["halt_reason"],
        "positions": pos_out, "max_positions": config.MAX_OPEN_POSITIONS,
        "pending": [{**o, "price": price.get(sym)} for sym, o in pending.items()],
        "stats": trade_stats(st["trades"]),
        "decisions": recent_decisions(st),
        "last_cycle": st.get("last_cycle"),
        "focus": st.get("focus"),
        "regime": (st.get("focus") or {}).get("regime") or (scan or {}).get("regime"),
        "auto_select": config.AUTO_SELECT,
        "ai_limit": config.AI_DAILY_CALL_LIMIT,
        "usage_today": st["usage"].get(today, {}),
        "exchange_usdt": snap["exchange_usdt"], "capital_limit": config.LIVE_CAPITAL_USDT,
        "symbols": config.SYMBOLS, "tab_symbols": tab_syms, "quote": config.account_currency(), "profile": config.PROFILE,
        "market": config.MARKET, "benchmark_label": config.benchmark_label(), "chart_tfs": CHART_TFS[config.MARKET],
        "exchanges": ui.market_status() if config.MARKET != "crypto" else [],
        "allow_short": config.MARKET == "cfd" and config.CFD_ALLOW_SHORT,
        "strategy": config.STOCK_STRATEGY if config.MARKET == "stocks" else "ativo",
        "trend": st.get("trend") if config.MARKET == "stocks" else None,
        "timeframe": config.PRIMARY_TIMEFRAME, "model": config.DECISION_MODEL,
        "scanner": {"enabled": config.SCANNER_ENABLED, "top": config.SCAN_TOP,
                    "universe": min(config.SCAN_UNIVERSE, len(config.CFD_UNIVERSE)) if config.MARKET == "cfd" else config.SCAN_UNIVERSE},
        "risk_mode": config.RISK_MODE, "risk_fixed": config.RISK_FIXED_AMOUNT, "risk_pct": config.RISK_PER_TRADE_PCT,
        "tickers": {s: {"last": price.get(s), "change_pct": (tick.get(s) or {}).get("percentage")}
                    for s in tab_syms},
        "keys": {"openai": mask(config.OPENAI_API_KEY), "binance": mask(config.EXCHANGE_API_KEY),
                 "testnet": mask(config.TESTNET_API_KEY), "t212": mask(config.T212_API_KEY),
                 "t212_demo": mask(config.T212_DEMO_API_KEY),
                 "ctrader_demo": ctrader_label("cfd_demo"), "ctrader_live": ctrader_label("cfd_live")},
        "ctrader": ctrader_state(),
        "live_confirmed": config.LIVE_CONFIRMED, "use_testnet": config.USE_TESTNET,
    }


def ctrader_label(key: str) -> str | None:
    """Texto da conta cTrader ligada (para a janela do Modo Real), ou None."""
    account = config.ctrader_account(key)
    if not (ctrader.has_app() and ctrader.has_token() and account):
        return None
    return f"conta {account}"


def ctrader_state() -> dict:
    try:
        expires = int(config.CTRADER_TOKEN_EXPIRES or 0)
    except ValueError:
        expires = 0
    return {"app": ctrader.has_app(), "client_id": mask(config.CTRADER_CLIENT_ID) or (config.CTRADER_CLIENT_ID[:6] + "…"
                                                                                        if config.CTRADER_CLIENT_ID else None),
            "token": ctrader.has_token(), "token_expires": expires or None,
            "demo_account": config.CTRADER_DEMO_ACCOUNT or None, "live_account": config.CTRADER_LIVE_ACCOUNT or None}


def recent_decisions(st: dict) -> dict:
    """Última decisão de cada par analisado recentemente (fixos, posições, pendentes e radar)."""
    out = {}
    for sym, hist in st["decisions"].items():
        ok = ("/" not in sym) if config.MARKET != "crypto" else sym.endswith("/" + config.QUOTE)
        if hist and ok:
            out[sym] = hist[-1]
    ordered = sorted(out.items(), key=lambda kv: kv[1].get("time") or "", reverse=True)
    return dict(ordered[:12])


def latest_scan() -> dict | None:
    eng_scan = controller.snapshot.get("scan") if controller and controller.snapshot.get("market") == config.MARKET else None
    own = _ui_scanners[config.MARKET].last if config.MARKET in _ui_scanners else None
    cands = [x for x in (eng_scan, own) if x and x.get("time")]
    return max(cands, key=lambda x: x["time"]) if cands else None


@app.get("/api/scan")
def scan_results():
    scan = latest_scan()
    if not scan:
        return {"time": None, "results": [], "universe": 0, "quote": config.account_currency()}
    picks = {p["symbol"]: p.get("reason") for p in (controller.snapshot["state"].get("focus") or {}).get("picks", [])}
    rows = [{**r, "picked": r["symbol"] in picks, "pick_reason": picks.get(r["symbol"])} for r in scan["results"][:40]]
    return {**scan, "results": rows, "quote": config.account_currency()}


@app.post("/api/scan")
def scan_now():
    if not ui_scan_lock.acquire(blocking=False):
        raise ValueError("O radar já está a correr.")
    try:
        sc = _ui_scanners.get(config.MARKET)
        if sc is None:
            sc = _ui_scanners[config.MARKET] = Scanner(make_market_data(config.MARKET))
        sc.scan()
    finally:
        ui_scan_lock.release()
    return scan_results()


@app.get("/api/news")
def news():
    asset = None
    hub = ui_news()
    heads = hub.headlines(asset, hours=36, limit=60)
    feeds = feeds_for(config.MARKET)
    return {
        "headlines": heads,
        "events": hub.calendar(),
        "global": hub.global_market(),
        "market": config.MARKET,
        "vix": ui._md().fear_greed() if config.MARKET != "crypto" else None,
        "sources": hub.feed_status or {k: "?" for k in feeds},
    }


@app.get("/api/equity")
def equity_history():
    hist = controller.snapshot["state"].get("equity_history", [])
    step = max(1, len(hist) // 800)
    pts = hist[::step]
    if hist and pts[-1] != hist[-1]:
        pts.append(hist[-1])
    return {"points": pts, "start": controller.snapshot["state"]["start_cash"]}


@app.get("/api/trades")
def trades():
    return {"trades": list(reversed(controller.snapshot["state"]["trades"][-300:]))}


def tail_jsonl(path, n):
    if not path.exists():
        return []
    with open(path, "rb") as f:
        f.seek(0, 2)
        pos, data = f.tell(), b""
        while pos > 0 and data.count(b"\n") <= n:
            step = min(262_144, pos)
            pos -= step
            f.seek(pos)
            data = f.read(step) + data
    out = []
    for line in data.splitlines()[-n:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


@app.get("/api/decisions")
def decisions(limit: int = 60):
    path = config.LOGS_DIR / f"{config.mode_key()}_decisions.jsonl"
    rows = []
    for r in reversed(tail_jsonl(path, min(limit, 300))):
        d = r.get("decision", {})
        rows.append({"time": r.get("logged_at"), "symbol": r.get("symbol"), "price": r.get("price"),
                     "outcome": r.get("outcome"), **{k: d.get(k) for k in (
                         "action", "confidence", "market_regime", "reasoning", "bull_case", "bear_case",
                         "invalidation", "stop_loss", "take_profit", "new_stop_loss", "model", "votes",
                         "input_tokens", "output_tokens")},
                     "news": ((r.get("context") or {}).get("news") or {}).get("summary")})
    return {"decisions": rows}


@app.get("/api/logs")
def logs(after: int = 0):
    return {"lines": ring.since(after)[-400:]}


@app.get("/api/candles")
def candles(symbol: str, tf: str = "1h"):
    if tf not in CHART_TFS[config.MARKET]:
        tf = "1h"
    if not ui.has_symbol(symbol):
        raise ValueError("ativo desconhecido")
    df = ui.candles(symbol, tf)
    tf_s = ui._md().tf_ms(tf) // 1000
    first = int(df["ts"].iloc[0]) // 1000 if len(df) else 0
    rows = [{"time": int(r.ts) // 1000, "open": r.open, "high": r.high, "low": r.low, "close": r.close,
             "volume": r.volume} for r in df.itertuples()]

    st = controller.snapshot["state"]
    markers = []

    def bar_time(iso_str):
        t = calendar.timegm(time.strptime(iso_str[:19], "%Y-%m-%dT%H:%M:%S"))
        return (t // tf_s) * tf_s

    for t in st["trades"]:
        if t["symbol"] != symbol:
            continue
        short = t.get("side") == "short"
        for kind, stamp, px in (("short" if short else "buy", t["opened_at"], t["entry_price"]),
                                ("cover" if short else "sell", t["closed_at"], t["exit_price"])):
            bt = bar_time(stamp)
            if bt >= first:
                closing = kind in ("sell", "cover")
                markers.append({"time": bt, "kind": kind, "price": px, "pnl": t["pnl"] if closing else None})
    pos = st["positions"].get(symbol)
    lines = None
    pend = st.get("pending", {}).get(symbol)
    if pend and not pos:
        lines = {"pending": pend["trigger"], "pending_type": pend["type"], "stop": pend["stop"],
                 "take_profit": pend["take_profit"], "side": side_of(pend)}
    if pos:
        bt = bar_time(pos["opened_at"])
        if bt >= first:
            markers.append({"time": bt, "kind": "short" if side_of(pos) == "short" else "buy",
                            "price": pos["entry_price"], "pnl": None})
        lines = {"entry": pos["entry_price"], "stop": pos["stop"], "take_profit": pos["take_profit"],
                 "side": side_of(pos)}
    markers.sort(key=lambda m: m["time"])
    return {"candles": rows, "markers": markers, "lines": lines}


# ============================================================ ações
@app.post("/api/bot/start")
def bot_start():
    run(controller.cmd_start)
    return {"ok": True}


@app.post("/api/bot/stop")
def bot_stop():
    controller.stop()
    return {"ok": True}


@app.post("/api/bot/analyze")
def bot_analyze():
    run(controller.cmd_analyze_now, wait=False)
    return {"ok": True}


@app.post("/api/positions/close")
def close_position(payload: dict = Body(...)):
    trade = run(controller.cmd_close, payload.get("symbol", ""), urgent=True)
    return {"ok": True, "trade": trade}


@app.post("/api/pending/cancel")
def cancel_pending(payload: dict = Body(...)):
    run(controller.cmd_cancel_pending, payload.get("symbol", ""), urgent=True)
    return {"ok": True}


@app.post("/api/positions/close-all")
def close_all():
    run(controller.cmd_close_all, urgent=True)
    return {"ok": True}


@app.post("/api/resume")
def resume():
    run(controller.cmd_resume)
    return {"ok": True}


@app.post("/api/reset-test")
def reset_test(payload: dict = Body(...)):
    balance = float(payload.get("balance") or config.PAPER_START_BALANCE)
    lo, hi = BOUNDS["PAPER_START_BALANCE"]
    if not lo <= balance <= hi:
        raise ValueError(f"O saldo tem de estar entre {lo:,.0f} e {hi:,.0f} {config.account_currency()}.")
    run(controller.cmd_reset_test, balance)
    return {"ok": True}


@app.post("/api/market")
def set_market(payload: dict = Body(...)):
    market = payload.get("market")
    if market not in ("crypto", "stocks", "cfd"):
        raise ValueError("mercado inválido")
    run(controller.cmd_set_market, market)
    return {"ok": True}


@app.post("/api/keys/t212")
def save_t212_keys(payload: dict = Body(...)):
    demo = bool(payload.get("testnet"))
    key = (payload.get("api_key") or "").strip()
    secret = (payload.get("api_secret") or "").strip()
    if not key and not secret:
        key, secret = config.exchange_keys("stocks_demo" if demo else "stocks_live")
    check = verify_t212(key, secret, demo)
    if check["usdt_free"] is not None and payload.get("api_key"):
        prefix = "T212_DEMO" if demo else "T212"
        config.set_secret(f"{prefix}_API_KEY", key)
        config.set_secret(f"{prefix}_API_SECRET", secret)
        check["saved"] = True
    return check


@app.post("/api/mode")
def set_mode(payload: dict = Body(...)):
    mode = payload.get("mode")
    if mode == "paper":
        run(controller.cmd_set_mode, "paper")
        return {"ok": True}
    if mode != "live":
        raise ValueError("modo inválido")
    if (payload.get("confirm") or "").strip().upper() != "REAL" or not payload.get("accept"):
        raise ValueError("Confirmação em falta.")
    testnet = bool(payload.get("testnet"))
    capital = float(payload.get("capital") or 0)
    lo, hi = BOUNDS["LIVE_CAPITAL_USDT"]
    if not lo <= capital <= hi:
        raise ValueError(f"O capital tem de estar entre {lo:,.0f} e {hi:,.0f} {config.account_currency()}.")
    if config.MARKET == "cfd":
        check = ctrader.verify_ctrader(demo=testnet)
    elif config.MARKET == "stocks":
        key, secret = config.exchange_keys("stocks_demo" if testnet else "stocks_live")
        check = verify_t212(key, secret, testnet)
    else:
        key, secret = config.exchange_keys("testnet" if testnet else "live")
        check = verify_keys(config.EXCHANGE, key, secret, sandbox=testnet)
    if not check["ok"]:
        raise ValueError(" ".join(check["problems"]))
    if check["usdt_free"] is not None and capital > check["usdt_free"] + 1e-6 and not testnet:
        raise ValueError(f"O capital ({capital:,.2f}) é maior do que o saldo livre em {config.account_currency()} "
                         f"({check['usdt_free']:,.2f}).")
    run(controller.cmd_set_mode, "live", testnet, capital)
    return {"ok": True}


_ip_cache = [0.0, ""]


@app.get("/api/public-ip")
def get_public_ip():
    """IP público desta internet (o que a Binance/Trading 212 veem), para a lista de IPs de confiança."""
    from bot.broker import public_ip
    if not _ip_cache[1] or time.time() - _ip_cache[0] > 300:
        _ip_cache[:] = [time.time(), public_ip()]
    return {"ip": _ip_cache[1]}


_oauth = {"started": 0.0, "redirect": ""}
_accounts_cache = [0.0, None]


@app.get("/api/ctrader/status")
def ctrader_status(request: Request):
    port = request.url.port or PORT
    return {**ctrader_state(), "redirect_uri": ctrader.redirect_uri(port), "portal": ctrader.PORTAL_URL}


@app.post("/api/ctrader/app")
def ctrader_save_app(payload: dict = Body(...)):
    cid = (payload.get("client_id") or "").strip()
    secret = (payload.get("client_secret") or "").strip()
    if not cid or not secret:
        raise ValueError("Preenche o Client ID e o Secret da aplicação.")
    old = (config.CTRADER_CLIENT_ID, config.CTRADER_CLIENT_SECRET)
    config.CTRADER_CLIENT_ID, config.CTRADER_CLIENT_SECRET = cid, secret
    try:
        ctrader.check_app()
    except Exception as e:
        config.CTRADER_CLIENT_ID, config.CTRADER_CLIENT_SECRET = old
        raise ValueError(f"O cTrader recusou a aplicação: {ctrader.friendly(e)}")
    config.set_secret("CTRADER_CLIENT_ID", cid)
    config.set_secret("CTRADER_CLIENT_SECRET", secret)
    ctrader.drop_sessions()
    log.info("cTrader: aplicação (Client ID) verificada e guardada.")
    return {"ok": True, **ctrader_state()}


@app.post("/api/ctrader/connect")
def ctrader_connect(request: Request):
    if not ctrader.has_app():
        raise ValueError("Guarda primeiro o Client ID e o Secret da aplicação.")
    redirect = ctrader.redirect_uri(request.url.port or PORT)
    _oauth.update(started=time.time(), redirect=redirect)
    url = ctrader.authorize_url(redirect)
    try:
        webbrowser.open(url)
    except Exception:
        pass
    return {"url": url, "redirect_uri": redirect}


CALLBACK_PAGE = """<!doctype html><html lang="pt-PT"><head><meta charset="utf-8"><title>cTrader</title>
<style>body{{font-family:system-ui,sans-serif;background:#121211;color:#e8e6e1;display:grid;place-items:center;height:100vh;margin:0}}
div{{max-width:460px;padding:28px;border:1px solid #333;border-radius:14px;background:#1b1b1a}}h1{{font-size:20px}}
.ok{{color:#3fb97f}}.bad{{color:#e5534b}}</style></head><body><div><h1 class="{cls}">{title}</h1><p>{text}</p></div></body></html>"""


@app.get("/ctrader/callback", response_class=HTMLResponse)
def ctrader_callback(code: str = "", error: str = "", error_description: str = ""):
    def page(ok, title, text):
        return HTMLResponse(CALLBACK_PAGE.format(cls="ok" if ok else "bad", title=title, text=text))
    if error or not code:
        return page(False, "Autorização cancelada", f"O cTrader não autorizou a aplicação ({error_description or error or 'sem código'}). "
                                                    "Volta à aplicação e tenta de novo.")
    if time.time() - _oauth["started"] > 900:
        return page(False, "Pedido expirado", "Carrega outra vez em \"Ligar ao cTrader\" na aplicação.")
    try:
        ctrader.exchange_code(code, _oauth["redirect"])
    except Exception as e:
        return page(False, "Não foi possível ligar", f"{ctrader.friendly(e)}. Confirma que o Redirect URI da aplicação no "
                                                     f"portal é exatamente {_oauth['redirect']}.")
    _oauth["started"] = 0.0
    _accounts_cache[0] = 0.0
    log.info("cTrader: conta autorizada com sucesso.")
    return page(True, "Ligado ao cTrader ✓", "Podes fechar esta janela e voltar à aplicação Trading Bot IA para escolher a conta.")


@app.post("/api/ctrader/token")
def ctrader_manual_token(payload: dict = Body(...)):
    token = (payload.get("access_token") or "").strip()
    refresh = (payload.get("refresh_token") or "").strip()
    if len(token) < 20:
        raise ValueError("Cola o Access token completo (Playground do portal cTrader).")
    old = (config.CTRADER_ACCESS_TOKEN, config.CTRADER_REFRESH_TOKEN, config.CTRADER_TOKEN_EXPIRES)
    config.CTRADER_ACCESS_TOKEN, config.CTRADER_REFRESH_TOKEN, config.CTRADER_TOKEN_EXPIRES = token, refresh, ""
    try:
        accounts = ctrader.list_accounts()
    except Exception as e:
        config.CTRADER_ACCESS_TOKEN, config.CTRADER_REFRESH_TOKEN, config.CTRADER_TOKEN_EXPIRES = old
        raise ValueError(f"O cTrader recusou o token: {ctrader.friendly(e)}")
    ctrader.save_tokens({"accessToken": token, "refreshToken": refresh or None, "expiresIn": 2_628_000})
    _accounts_cache[:] = [time.time(), accounts]
    return {"ok": True, "accounts": accounts, **ctrader_state()}


@app.get("/api/ctrader/accounts")
def ctrader_accounts(refresh: bool = False):
    if not (ctrader.has_app() and ctrader.has_token()):
        return {"accounts": [], **ctrader_state()}
    if refresh or not _accounts_cache[1] or time.time() - _accounts_cache[0] > 120:
        try:
            _accounts_cache[:] = [time.time(), ctrader.list_accounts()]
        except Exception as e:
            raise ValueError(f"Não foi possível ler as contas cTrader: {ctrader.friendly(e)}")
    return {"accounts": _accounts_cache[1], **ctrader_state()}


@app.post("/api/ctrader/account")
def ctrader_pick_account(payload: dict = Body(...)):
    demo = bool(payload.get("testnet"))
    account = int(payload.get("account_id") or 0)
    known = {a["id"]: a for a in (_accounts_cache[1] or ctrader.list_accounts())}
    if account not in known:
        raise ValueError("Conta desconhecida: atualiza a lista de contas.")
    if known[account]["is_live"] == demo:
        raise ValueError("Esta conta é " + ("real" if known[account]["is_live"] else "demo") +
                         ": escolhe uma conta " + ("demo" if demo else "real") + ".")
    st = controller.snapshot["state"]
    key = "cfd_demo" if demo else "cfd_live"
    if config.mode_key() == key and (st["positions"] or st.get("pending")):
        raise ValueError("Fecha as posições desta conta antes de mudar para outra.")
    config.save({"CTRADER_DEMO_ACCOUNT" if demo else "CTRADER_LIVE_ACCOUNT": account})
    ctrader.drop_sessions()
    return ctrader.verify_ctrader(demo, account)


@app.post("/api/keys/ctrader")
def verify_ctrader_keys(payload: dict = Body(...)):
    return ctrader.verify_ctrader(bool(payload.get("testnet")))


@app.post("/api/keys/binance")
def save_binance_keys(payload: dict = Body(...)):
    testnet = bool(payload.get("testnet"))
    key = (payload.get("api_key") or "").strip()
    secret = (payload.get("api_secret") or "").strip()
    if not key and not secret:  # verificar as chaves já guardadas
        key, secret = config.exchange_keys("testnet" if testnet else "live")
    check = verify_keys(config.EXCHANGE, key, secret, sandbox=testnet)
    usable = check["usdt_free"] is not None  # as chaves ligam (mesmo que haja avisos de permissões)
    if usable and payload.get("api_key"):
        prefix = "TESTNET" if testnet else "EXCHANGE"
        config.set_secret(f"{prefix}_API_KEY", key)
        config.set_secret(f"{prefix}_API_SECRET", secret)
        check["saved"] = True
    return check


@app.post("/api/keys/openai")
def save_openai_key(payload: dict = Body(...)):
    key = (payload.get("key") or "").strip()
    if not key.startswith("sk-"):
        raise ValueError("A chave da OpenAI começa por 'sk-'.")
    from openai import OpenAI
    try:
        OpenAI(api_key=key, timeout=20).models.list()
    except Exception as e:
        raise ValueError(f"A OpenAI recusou a chave: {str(e)[:160]}")
    config.set_secret("OPENAI_API_KEY", key)
    run(controller.cmd_reload)
    return {"ok": True, "masked": mask(key)}


# ============================================================ gestão de ativos
MAX_MY_ASSETS = 30
_t212_cache = [0.0, None]


def t212_lookup():
    """Ligação à Trading 212 (se houver chaves) só para confirmar que um ativo existe lá."""
    if time.time() - _t212_cache[0] < 3600:
        return _t212_cache[1]
    broker = None
    for key_name, demo in (("stocks_live", False), ("stocks_demo", True)):
        key, secret = config.exchange_keys(key_name)
        if key and secret:
            try:
                from bot.t212 import Trading212Broker
                broker = Trading212Broker(key, secret, demo, ui._md())
                break
            except Exception as e:
                log.debug("Trading 212 indisponível para validar ativos: %s", e)
    _t212_cache[:] = [time.time(), broker]
    return broker


ASSET_KEYS = {"stocks": "STOCK_ASSETS", "cfd": "CFD_ASSETS", "crypto": "ASSETS"}


def my_assets() -> list:
    return list(getattr(config, ASSET_KEYS[config.MARKET]))


def save_my_assets(items: list):
    config.save({ASSET_KEYS[config.MARKET]: items})


@app.get("/api/assets")
def assets_list():
    meta = assets_mod.load_meta()
    from bot.catalog import CFD_CATALOG, CFD_SPECS
    mine = [{"symbol": s, "name": assets_mod.CATALOG_NAMES.get(s) or assets_mod.CRYPTO_NAMES.get(s)
             or (CFD_SPECS.get(s) or {}).get("name"), **meta.get(s, {})} for s in my_assets()]
    if config.MARKET == "cfd":
        cats = [{"key": k, "label": label, "symbols": syms, "enabled": k in config.CFD_CATEGORIES,
                 "names": {x: CFD_SPECS[x]["name"] for x in syms}} for k, (label, syms) in CFD_CATALOG.items()]
        return {"market": "cfd", "currency": config.CFD_CURRENCY, "mine": mine, "categories": cats, "discovery": None,
                "discoveries": [], "universe_size": len(config.CFD_UNIVERSE), "scan_top": config.SCAN_UNIVERSE,
                "max": MAX_MY_ASSETS, "broker": ctrader.any_session() is not None}
    if config.MARKET == "stocks":
        cats = [{"key": k, "label": label, "symbols": syms, "enabled": k in config.STOCK_CATEGORIES}
                for k, (label, syms) in assets_mod.STOCK_CATALOG.items()]
        try:
            disc = ui._md().discoveries()
        except Exception:
            disc = []
        return {"market": "stocks", "currency": config.STOCK_CURRENCY, "mine": mine, "categories": cats,
                "discovery": config.STOCK_DISCOVERY, "discoveries": disc, "universe_size": len(config.STOCK_UNIVERSE),
                "scan_top": config.SCAN_UNIVERSE, "max": MAX_MY_ASSETS}
    cats = [{"key": k, "label": label, "symbols": syms} for k, (label, syms) in assets_mod.CRYPTO_CATALOG.items()]
    return {"market": "crypto", "quote": config.QUOTE, "mine": mine, "catalog": cats,
            "scan_top": config.SCAN_UNIVERSE, "max": MAX_MY_ASSETS}


@app.get("/api/assets/search")
def assets_search(q: str = ""):
    mine = set(my_assets())
    if config.MARKET == "cfd":
        rows = assets_mod.search_cfd(q)
    elif config.MARKET == "stocks":
        rows = assets_mod.search_stocks(q)
    else:
        rows = assets_mod.search_crypto(ui._md(), q, config.QUOTE)
    for r in rows:
        r["in_list"] = r["symbol"] in mine
    return {"results": rows}


@app.post("/api/assets/add")
def assets_add(payload: dict = Body(...)):
    raw = str(payload.get("symbol") or "").strip().upper()
    if not raw or not TICKER_RX.match(raw.split("/")[0]):
        raise ValueError("Símbolo inválido.")
    items = my_assets()
    if len(items) >= MAX_MY_ASSETS:
        raise ValueError(f"No máximo {MAX_MY_ASSETS} ativos na tua lista.")
    if config.MARKET == "cfd":
        res = assets_mod.validate_cfd(raw, ui._md())
    elif config.MARKET == "stocks":
        res = assets_mod.validate_stock(raw, ui._md(), t212_lookup())
    else:
        res = assets_mod.validate_crypto(raw, ui._md(), config.QUOTE)
    if not res["ok"]:
        return res
    sym = res["symbol"]
    if sym not in items:
        save_my_assets(items + [sym])
    assets_mod.save_meta(sym, {**res, "note": "; ".join(res["warnings"]) or None})
    log.info("Ativo adicionado à tua lista: %s (%s)", sym, res.get("name"))
    return res


@app.post("/api/assets/remove")
def assets_remove(payload: dict = Body(...)):
    sym = str(payload.get("symbol") or "").strip().upper()
    items = [s for s in my_assets() if s != sym]
    save_my_assets(items)
    log.info("Ativo removido da tua lista: %s (posições abertas não são afetadas)", sym)
    return {"ok": True}


@app.post("/api/assets/categories")
def assets_categories(payload: dict = Body(...)):
    from bot.catalog import CFD_CATALOG
    cfd = config.MARKET == "cfd"
    catalog = CFD_CATALOG if cfd else assets_mod.STOCK_CATALOG
    values = {}
    if "categories" in payload:
        cats = [c for c in payload["categories"] if c in catalog]
        if not cats:
            raise ValueError("Escolhe pelo menos uma categoria.")
        values["CFD_CATEGORIES" if cfd else "STOCK_CATEGORIES"] = cats
    if "discovery" in payload and not cfd and payload["discovery"] is not None:
        values["STOCK_DISCOVERY"] = bool(payload["discovery"])
    config.save(values)
    return {"ok": True, "universe_size": len(config.CFD_UNIVERSE if cfd else config.STOCK_UNIVERSE)}


@app.get("/api/settings")
def get_settings():
    cur = config.current()
    return {
        "values": {k: cur[k] for k in USER_SETTINGS},
        "market": config.MARKET,
        "options": {"models": MODEL_OPTIONS, "assets": ASSET_OPTIONS, "timeframes": CHOICES["PRIMARY_TIMEFRAME"],
                    "stock_universe_default": config.STOCK_UNIVERSE,
                    "efforts": CHOICES["REASONING_EFFORT"], "quotes": CHOICES["QUOTE"], "bounds": BOUNDS,
                    "profiles": config.PROFILES},
    }


@app.post("/api/settings")
def save_settings(payload: dict = Body(...)):
    values = {}
    for k, v in payload.items():
        if k not in USER_SETTINGS:
            continue
        if k in CHOICES:
            if v not in CHOICES[k]:
                raise ValueError(f"Valor inválido para {k}")
            values[k] = v
        elif k in BOOLS:
            values[k] = bool(v)
        elif k in BOUNDS:
            lo, hi = BOUNDS[k]
            num = float(v)
            if not lo <= num <= hi:
                raise ValueError(f"{k} tem de estar entre {lo} e {hi}.")
            values[k] = num
    quote = values.get("QUOTE", config.QUOTE)
    if quote != config.QUOTE:
        st = controller.snapshot["state"]
        if st["positions"] or st.get("pending"):
            raise ValueError("Fecha as posições e ordens pendentes antes de mudar a moeda.")
    if "STOCK_UNIVERSE" in payload or "STOCK_ASSETS" in payload:
        stock_md = ui.mds.get("stocks") or make_market_data("stocks")
        for key, limit in (("STOCK_UNIVERSE", 80), ("STOCK_ASSETS", 8)):
            if key not in payload:
                continue
            items = [str(x).strip().upper().replace(".DE", ".DE") for x in payload[key] if str(x).strip()]
            items = list(dict.fromkeys(items))
            if len(items) > limit:
                raise ValueError(f"No máximo {limit} ativos em {key}.")
            bad = [x for x in items if not TICKER_RX.match(x)]
            known = set(config.STOCK_UNIVERSE) | set(config.TREND_UNIVERSE)
            bad += [x for x in items if x not in known and x not in bad and not stock_md.has_symbol(x)]
            if bad:
                raise ValueError(f"Ativos não encontrados no Yahoo Finance: {', '.join(bad)}")
            values[key] = items
    for key, market in (("STOCK_CURRENCY", "stocks"), ("CFD_CURRENCY", "cfd")):
        if values.get(key, getattr(config, key)) != getattr(config, key) and config.MARKET == market:
            st = controller.snapshot["state"]
            if st["positions"] or st.get("pending"):
                raise ValueError("Fecha as posições antes de mudar a moeda da conta.")
    if "ASSETS" in payload:
        markets = ui.markets() if config.MARKET == "crypto" else {}
        assets = [a.strip().upper() for a in dict.fromkeys(payload["ASSETS"]) if isinstance(a, str) and a.strip()]
        if len(assets) > 8:
            raise ValueError("No máximo 8 pares fixos.")
        bad = [a for a in assets if markets and f"{a}/{quote}" not in markets]
        if bad:
            raise ValueError(f"Não existe par em {quote} para: {', '.join(bad)}")
        values["ASSETS"] = assets
    config.save(values)
    run(controller.cmd_reload)
    return {"ok": True}


# ============================================================ arranque
def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def local_sockets(port: int) -> list:
    """Só escuta no próprio PC: 127.0.0.1 e, se existir, ::1 (é o que 'localhost' usa às vezes)."""
    socks = []
    for family, addr in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        try:
            s = socket.socket(family, socket.SOCK_STREAM)
            if family == socket.AF_INET6:
                s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            s.bind((addr, port))
            s.listen(128)
            s.set_inheritable(True)
            socks.append(s)
        except OSError:
            if family == socket.AF_INET:
                raise
    return socks


def main():
    global controller
    p = argparse.ArgumentParser(description="Trading Bot IA")
    p.add_argument("--browser", action="store_true", help="abrir no browser em vez de janela própria")
    p.add_argument("--server", action="store_true", help="só servidor, sem abrir janela")
    p.add_argument("--port", type=int, default=PORT)
    args = p.parse_args()

    url = f"http://127.0.0.1:{args.port}/"
    if port_in_use(args.port):
        print("A aplicação já está aberta. A abrir no browser...")
        webbrowser.open(url)
        return

    setup_logging(config.LOGS_DIR)
    logging.getLogger().addHandler(ring)
    for noisy in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    controller = Controller()
    log.info("Aplicação iniciada em %s", url)

    def warm_up():  # notícias e calendário prontos antes de abrir o separador
        try:
            hub = ui_news()
            hub.refresh_feeds(force=True)
            hub.calendar()
            hub.global_market()
        except Exception as e:
            log.debug("Pré-carregamento de notícias falhou: %s", e)
    threading.Thread(target=warm_up, name="news-warmup", daemon=True).start()

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="warning",
                                           access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": local_sockets(args.port)}, name="web", daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)

    def shutdown():
        controller.stop()
        time.sleep(0.5)
        server.should_exit = True

    if args.server:
        print(f"Servidor em {url}  (Ctrl+C para sair)")
    else:
        webview = None
        if not args.browser:
            try:
                import webview
            except ImportError:
                webview = None
        if webview:
            webview.create_window("Trading Bot IA", url, width=1500, height=940, min_size=(1100, 700),
                                  confirm_close=True, background_color="#121211")
            webview.start(localization={"global.quitConfirmation":
                                        "Fechar a aplicação? O bot deixa de negociar e de vigiar os stops."})
            shutdown()
            return
        webbrowser.open(url)
        print(f"Aplicação aberta em {url}  (Ctrl+C nesta janela para sair)")
    try:
        while thread.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        shutdown()


if __name__ == "__main__":
    main()
