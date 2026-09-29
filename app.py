"""
Trading Bot IA - aplicação com interface gráfica.

  python app.py            -> abre a aplicação numa janela própria
  python app.py --browser  -> abre no browser em vez de janela
  python app.py --server   -> só o servidor (sem abrir nada)
"""
import argparse
import calendar
import json
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
from bot.engine import ConfigError
from bot.journal import setup_logging
from bot.market import MarketData
from bot.news import FEEDS, NewsHub
from bot.scanner import Scanner
from bot.trader import trade_stats

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
CHART_TFS = ["15m", "1h", "4h", "1d"]
BOUNDS = {
    "RISK_PER_TRADE_PCT": (0.1, 5), "RISK_FIXED_AMOUNT": (0.05, 100_000), "MAX_POSITION_PCT": (5, 100),
    "MAX_OPEN_POSITIONS": (1, 10), "MIN_CONFIDENCE": (0.5, 0.95), "MIN_RISK_REWARD": (1.0, 5),
    "MAX_DAILY_LOSS_PCT": (0.5, 25), "MAX_DRAWDOWN_PCT": (2, 60), "COOLDOWN_AFTER_LOSS_MINUTES": (0, 1440),
    "NEWS_REFRESH_MINUTES": (15, 1440), "DECISION_VOTES": (1, 5), "PAPER_START_BALANCE": (20, 10_000_000),
    "LIVE_CAPITAL_USDT": (10, 10_000_000), "BREAKEVEN_AT_R": (0, 5), "TRAIL_START_R": (0, 10),
    "TRAIL_ATR_MULT": (0, 10), "SCAN_UNIVERSE": (5, 60), "SCAN_TOP": (0, 8), "SCAN_MIN_SCORE": (20, 90),
    "PENDING_ORDER_CANDLES": (1, 24), "AI_DAILY_CALL_LIMIT": (0, 5000),
}
CHOICES = {
    "PRIMARY_TIMEFRAME": ["15m", "1h", "4h"],
    "DECISION_MODEL": [m["value"] for m in MODEL_OPTIONS],
    "REASONING_EFFORT": ["low", "medium", "high"],
    "QUOTE": ["USDC", "EUR", "USDT"],
    "PROFILE": ["conservador", "equilibrado", "agressivo", "personalizado"],
    "RISK_MODE": ["percent", "fixed"],
}
BOOLS = ["NEWS_ENABLED", "SCANNER_ENABLED", "ONLY_WITH_SETUP", "AUTO_SELECT"]
USER_SETTINGS = ["ASSETS", *CHOICES, *BOOLS, *BOUNDS]


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
        self.md = None
        self.cache: dict = {}

    def _md(self):
        if self.md is None:
            self.md = MarketData(config.EXCHANGE)
        return self.md

    def cached(self, key, ttl, fn):
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
            return self.cached(("tickers", tuple(symbols)), 4, lambda md: md.tickers(symbols))
        except Exception as e:
            log.debug("tickers falhou: %s", e)
            hit = self.cache.get(("tickers", tuple(symbols)))
            return hit[1] if hit else {}

    def candles(self, symbol, tf):
        return self.cached(("candles", symbol, tf), 10, lambda md: md.ohlcv(symbol, tf, 400, closed_only=False))

    def markets(self):
        return self.cached(("markets",), 3600, lambda md: md.ex.markets)


ui = UIMarket()
ui_news = NewsHub()  # sem IA: só manchetes, calendário e dados globais
ui_scan_lock = threading.Lock()
ui_scanner: Scanner | None = None

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
    tab_syms = list(dict.fromkeys(list(positions) + top + list(pending) + fixed)) or [f"BTC/{config.QUOTE}"]
    tick = ui.tickers(tab_syms)
    price = {s: float(t["last"]) for s, t in tick.items() if t.get("last")}

    pos_out = []
    invested = 0.0
    for sym, p in positions.items():
        px = price.get(sym, p["entry_price"])
        value = p["qty"] * px
        invested += value
        risk_unit = p["entry_price"] - p["initial_stop"]
        pnl = value - p["entry_cost"]
        pos_out.append({
            "symbol": sym, "qty": p["qty"], "entry_price": p["entry_price"], "price": px, "value": value,
            "pnl": pnl, "pnl_pct": pnl / p["entry_cost"] * 100 if p["entry_cost"] else 0,
            "r": (px - p["entry_price"]) / risk_unit if risk_unit > 0 else None,
            "stop": p["stop"], "initial_stop": p["initial_stop"], "take_profit": p["take_profit"],
            "opened_at": p["opened_at"], "confidence": p.get("confidence"), "reasoning": p.get("reasoning", ""),
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
        "symbols": config.SYMBOLS, "tab_symbols": tab_syms, "quote": config.QUOTE, "profile": config.PROFILE,
        "timeframe": config.PRIMARY_TIMEFRAME, "model": config.DECISION_MODEL,
        "scanner": {"enabled": config.SCANNER_ENABLED, "top": config.SCAN_TOP, "universe": config.SCAN_UNIVERSE},
        "risk_mode": config.RISK_MODE, "risk_fixed": config.RISK_FIXED_AMOUNT, "risk_pct": config.RISK_PER_TRADE_PCT,
        "tickers": {s: {"last": price.get(s), "change_pct": (tick.get(s) or {}).get("percentage")}
                    for s in tab_syms},
        "keys": {"openai": mask(config.OPENAI_API_KEY), "binance": mask(config.EXCHANGE_API_KEY),
                 "testnet": mask(config.TESTNET_API_KEY)},
        "live_confirmed": config.LIVE_CONFIRMED, "use_testnet": config.USE_TESTNET,
    }


def recent_decisions(st: dict) -> dict:
    """Última decisão de cada par analisado recentemente (fixos, posições, pendentes e radar)."""
    out = {}
    for sym, hist in st["decisions"].items():
        if hist and sym.endswith("/" + config.QUOTE):
            out[sym] = hist[-1]
    ordered = sorted(out.items(), key=lambda kv: kv[1].get("time") or "", reverse=True)
    return dict(ordered[:12])


def latest_scan() -> dict | None:
    eng_scan = controller.snapshot.get("scan") if controller else None
    own = ui_scanner.last if ui_scanner else None
    cands = [x for x in (eng_scan, own) if x and x.get("time")]
    return max(cands, key=lambda x: x["time"]) if cands else None


@app.get("/api/scan")
def scan_results():
    scan = latest_scan()
    if not scan:
        return {"time": None, "results": [], "universe": 0, "quote": config.QUOTE}
    picks = {p["symbol"]: p.get("reason") for p in (controller.snapshot["state"].get("focus") or {}).get("picks", [])}
    rows = [{**r, "picked": r["symbol"] in picks, "pick_reason": picks.get(r["symbol"])} for r in scan["results"][:40]]
    return {**scan, "results": rows, "quote": config.QUOTE}


@app.post("/api/scan")
def scan_now():
    global ui_scanner
    if not ui_scan_lock.acquire(blocking=False):
        raise ValueError("O radar já está a correr.")
    try:
        if ui_scanner is None:
            ui_scanner = Scanner(MarketData(config.EXCHANGE))
        ui_scanner.scan()
    finally:
        ui_scan_lock.release()
    return scan_results()


@app.get("/api/news")
def news():
    asset = None
    heads = ui_news.headlines(asset, hours=36, limit=60)
    return {
        "headlines": heads,
        "events": ui_news.calendar(),
        "global": ui_news.global_market(),
        "sources": ui_news.feed_status or {k: "?" for k in FEEDS},
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
    if tf not in CHART_TFS:
        raise ValueError("timeframe inválido")
    if symbol not in ui.markets():
        raise ValueError("par desconhecido")
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
        for kind, stamp, px in (("buy", t["opened_at"], t["entry_price"]), ("sell", t["closed_at"], t["exit_price"])):
            bt = bar_time(stamp)
            if bt >= first:
                markers.append({"time": bt, "kind": kind, "price": px, "pnl": t["pnl"] if kind == "sell" else None})
    pos = st["positions"].get(symbol)
    lines = None
    pend = st.get("pending", {}).get(symbol)
    if pend and not pos:
        lines = {"pending": pend["trigger"], "pending_type": pend["type"], "stop": pend["stop"],
                 "take_profit": pend["take_profit"]}
    if pos:
        bt = bar_time(pos["opened_at"])
        if bt >= first:
            markers.append({"time": bt, "kind": "buy", "price": pos["entry_price"], "pnl": None})
        lines = {"entry": pos["entry_price"], "stop": pos["stop"], "take_profit": pos["take_profit"]}
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
        raise ValueError(f"O saldo tem de estar entre {lo:,.0f} e {hi:,.0f} USDT.")
    run(controller.cmd_reset_test, balance)
    return {"ok": True}


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
        raise ValueError(f"O capital tem de estar entre {lo:,.0f} e {hi:,.0f} USDT.")
    key, secret = config.exchange_keys("testnet" if testnet else "live")
    check = verify_keys(config.EXCHANGE, key, secret, sandbox=testnet)
    if not check["ok"]:
        raise ValueError(" ".join(check["problems"]))
    if check["usdt_free"] is not None and capital > check["usdt_free"] + 1e-6 and not testnet:
        raise ValueError(f"O capital ({capital:,.2f}) é maior do que o saldo livre em {config.QUOTE} "
                         f"({check['usdt_free']:,.2f}).")
    run(controller.cmd_set_mode, "live", testnet, capital)
    return {"ok": True}


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


@app.get("/api/settings")
def get_settings():
    cur = config.current()
    return {
        "values": {k: cur[k] for k in USER_SETTINGS},
        "options": {"models": MODEL_OPTIONS, "assets": ASSET_OPTIONS, "timeframes": CHOICES["PRIMARY_TIMEFRAME"],
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
    if "ASSETS" in payload:
        markets = ui.markets()
        assets = [a.strip().upper() for a in dict.fromkeys(payload["ASSETS"]) if isinstance(a, str) and a.strip()]
        if len(assets) > 8:
            raise ValueError("No máximo 8 pares fixos.")
        bad = [a for a in assets if f"{a}/{quote}" not in markets]
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
            ui_news.refresh_feeds(force=True)
            ui_news.calendar()
            ui_news.global_market()
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
