"""
Gestão de ativos: catálogo por categorias, pesquisa e validação antes de adicionar.

Tudo o que é adicionado passa pelas mesmas verificações que os ativos de origem:
dados suficientes para os indicadores, bolsa com horário conhecido, câmbio disponível,
par existente na Binance (cripto) e, se houver chaves da Trading 212, disponibilidade lá.
"""
import json
import logging
import re
import threading
import time

import config

log = logging.getLogger("bot")

META_PATH = config.DATA_DIR / "assets_meta.json"
_lock = threading.Lock()

from .catalog import (ALIASES, CATALOG_NAMES, CRYPTO_CATALOG, CRYPTO_NAMES, DEFAULT_STOCK_CATEGORIES, GOLD_FOREX_HINT,  # noqa: F401
                      OTC_EXCHANGES, SECONDARY_EU, SILVER_FOREX_HINT, STOCK_CATALOG, US_EXCHANGES, categories_universe)


# ------------------------------------------------------------------ metadados (nome, tipo, bolsa) para notícias e interface
def load_meta() -> dict:
    try:
        return json.loads(META_PATH.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def save_meta(symbol: str, info: dict):
    with _lock:
        meta = load_meta()
        meta[symbol] = {k: v for k, v in info.items() if k in ("name", "type", "exchange", "currency", "note", "t212")}
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        META_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=1), "utf-8")


def display_name(symbol: str) -> str | None:
    return (load_meta().get(symbol) or {}).get("name")


# ------------------------------------------------------------------ pesquisa
def search_stocks(query: str, limit: int = 12) -> list:
    import yfinance as yf
    from .stockdata import SESSIONS
    q = (query or "").strip()
    if len(q) < 1:
        return []
    ql = q.lower().replace("/", "")
    local = []
    for key, syms in ALIASES.items():
        if key in ql or (len(ql) >= 3 and key.startswith(ql)):
            local.extend(syms)
    for sym, name in CATALOG_NAMES.items():
        if ql in sym.lower() or (len(ql) >= 3 and ql in name.lower()):
            local.append(sym)
    out, seen = [], set()
    if ql.startswith(("xau", "xag")):
        out.append({"symbol": q.upper(), "name": "Ouro/prata à vista (CFD)", "type": "CFD", "exchange": "forex",
                    "ok": False, "why": GOLD_FOREX_HINT if ql.startswith("xau") else SILVER_FOREX_HINT})
    for sym in dict.fromkeys(local):
        seen.add(sym)
        etf = sym in sum((STOCK_CATALOG[k][1] for k in ("etf_index", "etf_sector", "metals")), [])
        out.append({"symbol": sym, "name": CATALOG_NAMES.get(sym, sym), "type": "ETF" if etf else "EQUITY",
                    "exchange": sym.rsplit(".", 1)[-1] if "." in sym else "EUA", "ok": True, "why": None})
    try:
        quotes = yf.Search(q, max_results=limit * 2, news_count=0).quotes
    except Exception as e:
        log.warning("Pesquisa Yahoo falhou: %s", e)
        return []
    for x in quotes:
        sym = (x.get("symbol") or "").upper()
        qtype = x.get("quoteType")
        if not sym or qtype not in ("EQUITY", "ETF") or sym in seen:
            continue
        ok, why = quick_stock_rules(sym, qtype, x.get("exchange"), SESSIONS)
        out.append({"symbol": sym, "name": x.get("shortname") or x.get("longname") or sym, "type": qtype,
                    "exchange": x.get("exchDisp") or x.get("exchange"), "ok": ok, "why": why})
        if len(out) >= limit:
            break
    return out


def search_crypto(md, query: str, quote: str, limit: int = 15) -> list:
    q = (query or "").strip().upper()
    if not q:
        return []
    if q in ("OURO", "GOLD", "XAU", "XAUUSD", "XAU/USD"):
        q = "PAXG"  # ouro tokenizado (o XAUT aparece também em "XAU")
    tick = md.all_tickers()
    rows = []
    for sym, m in md.ex.markets.items():
        if m.get("quote") != quote or not m.get("spot") or not m.get("active"):
            continue
        base = m["base"]
        if q in base or (q == "PAXG" and base == "XAUT"):
            vol = float((tick.get(sym) or {}).get("quoteVolume") or 0)
            rows.append({"symbol": base, "pair": sym, "name": CRYPTO_NAMES.get(base, ""), "type": "CRYPTO", "exchange": "Binance",
                         "volume_24h": vol, "ok": True, "why": None if vol >= 1e6 else "pouco volume (spreads maiores)",
                         "_rank": (base != q, base.startswith(q) is False, -vol)})
    rows.sort(key=lambda r: r["_rank"])
    for r in rows:
        r.pop("_rank")
    return rows[:limit]


def quick_stock_rules(symbol: str, qtype: str | None, exchange: str | None, sessions: dict) -> tuple[bool, str | None]:
    """Regras rápidas (sem descarregar dados) para saber se um ticker pode funcionar."""
    suffix = symbol.rsplit(".", 1)[-1] if "." in symbol else ""
    if exchange in OTC_EXCHANGES:
        return False, "mercado de balcão (OTC): pouca liquidez e não disponível nas corretoras europeias"
    if suffix in SECONDARY_EU:
        return False, "bolsa secundária alemã: usa a versão da Xetra (.DE)"
    if suffix and suffix not in sessions:
        return False, f"bolsa .{suffix} ainda não suportada (horário desconhecido)"
    if not suffix and qtype == "ETF":
        return False, "ETF americano: não permitido a particulares na UE (PRIIPs); usa a versão UCITS (ex.: SXR8.DE em vez de SPY)"
    if not suffix and exchange and exchange not in US_EXCHANGES:
        return False, f"bolsa {exchange} não suportada"
    return True, None


# ------------------------------------------------------------------ validação completa
def validate_stock(symbol: str, md, t212=None) -> dict:
    """Verifica tudo o que o bot precisa. Devolve {ok, symbol, name, type, exchange, currency, problems, warnings}."""
    import yfinance as yf
    from .stockdata import SESSIONS, exchange_of
    sym = symbol.strip().upper()
    res = {"ok": False, "symbol": sym, "problems": [], "warnings": []}
    compact = sym.replace("/", "").replace("=X", "")
    if compact.startswith(("XAU", "XAG")) and len(compact) <= 6:
        res["problems"].append(GOLD_FOREX_HINT if compact.startswith("XAU") else SILVER_FOREX_HINT)
        return res
    try:
        fi = yf.Ticker(sym).fast_info
        price = float(fi["lastPrice"] or 0)
        res.update(currency=fi.get("currency"), exchange=fi.get("exchange"), type=fi.get("quoteType"))
    except Exception:
        res["problems"].append("ticker não encontrado no Yahoo Finance")
        return res
    if not price:
        res["problems"].append("sem preço atual")
        return res
    ok, why = quick_stock_rules(sym, res.get("type"), res.get("exchange"), SESSIONS)
    if not ok:
        res["problems"].append(why)
        return res
    try:
        h = md.ohlcv(sym, "1h", 300, closed_only=False)
        d = md.ohlcv(sym, "1d", 300, closed_only=False)
    except Exception as e:
        res["problems"].append(f"sem histórico de preços ({e})")
        return res
    if len(h) < 150 or len(d) < 60:
        res["problems"].append(f"histórico insuficiente ({len(h)} velas de 1h, {len(d)} diárias; o bot precisa de 150 e 60)")
        return res
    try:
        md._ccy[sym] = res["currency"]
        fx = md.fx(sym)
        if not fx:
            raise ValueError
    except Exception:
        res["problems"].append(f"sem câmbio {res.get('currency')} -> {config.STOCK_CURRENCY}")
        return res
    dollar_vol = float((d["close"] * d["volume"]).tail(20).mean())
    if dollar_vol < 1e6:
        res["warnings"].append("pouco volume diário: spreads maiores e preços mais irregulares")
    if res.get("currency") != config.STOCK_CURRENCY:
        res["warnings"].append(f"cotada em {res.get('currency')}: a Trading 212 cobra 0,15% de câmbio por operação")
    try:
        s = yf.Search(sym, max_results=3, news_count=0).quotes
        match = next((q for q in s if (q.get("symbol") or "").upper() == sym), None)
        res["name"] = (match or {}).get("shortname") or (match or {}).get("longname") or sym
    except Exception:
        res["name"] = sym
    res["exchange_name"] = exchange_of(sym)
    if t212 is not None:
        try:
            t212.t212_ticker(sym)
            res["t212"] = True
        except Exception:
            res["t212"] = False
            res["warnings"].append("não encontrado na Trading 212: só funciona em Modo Teste")
    res["ok"] = True
    return res


def validate_crypto(base: str, md, quote: str) -> dict:
    base = base.strip().upper().split("/")[0]
    if base in ("XAU", "XAUUSD", "GOLD", "OURO"):
        base = "PAXG"
    sym = f"{base}/{quote}"
    res = {"ok": False, "symbol": base, "pair": sym, "name": CRYPTO_NAMES.get(base, base), "type": "CRYPTO", "exchange": "Binance",
           "currency": quote, "problems": [], "warnings": []}
    m = md.ex.markets.get(sym)
    if not m or not m.get("spot") or not m.get("active"):
        others = [q for q in ("USDC", "EUR", "USDT") if q != quote and f"{base}/{q}" in md.ex.markets]
        extra = f" (existe em {', '.join(others)})" if others else ""
        res["problems"].append(f"a Binance não tem o par {sym}{extra}")
        return res
    try:
        df = md.ohlcv(sym, "1h", 300)
    except Exception as e:
        res["problems"].append(f"sem histórico ({e})")
        return res
    if len(df) < 150:
        res["problems"].append(f"moeda muito recente: só {len(df)} velas de 1h (o bot precisa de 150)")
        return res
    try:
        vol = float(md.ticker(sym).get("quoteVolume") or 0)
    except Exception:
        vol = 0
    if vol < 1e6:
        res["warnings"].append(f"pouco volume em 24h ({vol / 1e6:.1f} M {quote}): spreads maiores")
    res["ok"] = True
    return res


# ------------------------------------------------------------------ CFDs (cTrader)
def _cfd_canonical(query: str) -> str | None:
    """'xau/usd', 'gold', 'ouro', 'us500.cash' -> nome do catálogo."""
    from .catalog import CFD_ALIASES, CFD_SPECS
    q = re.sub(r"[^A-Za-z0-9&]", "", query or "").upper()
    if q in CFD_SPECS:
        return q
    for sym, spec in CFD_SPECS.items():
        if q in spec["aliases"]:
            return sym
    hit = CFD_ALIASES.get(q.lower())
    return hit[0] if hit and len(hit) == 1 else None


def search_cfd(query: str, limit: int = 15) -> list:
    from . import ctrader
    from .catalog import CFD_ALIASES, CFD_GROUP_NAMES, CFD_SPECS
    q = (query or "").strip()
    if not q:
        return []
    ql = q.lower().replace("/", "")
    qn = re.sub(r"[^a-z0-9]", "", ql)
    found = []
    for key, syms in CFD_ALIASES.items():
        if key.startswith(ql) or (len(ql) >= 3 and ql in key):
            found.extend(syms)
    for sym, spec in CFD_SPECS.items():
        if qn and (qn in sym.lower() or any(qn in a.lower() for a in spec["aliases"])
                   or (len(ql) >= 3 and ql in spec["name"].lower())):
            found.append(sym)
    out = []
    for sym in dict.fromkeys(found):
        spec = CFD_SPECS[sym]
        out.append({"symbol": sym, "name": spec["name"], "type": "CFD", "exchange": CFD_GROUP_NAMES[spec["group"]],
                    "ok": True, "why": None})
    s = ctrader.any_session()
    if s is not None and len(qn) >= 2:
        try:
            known = {o["symbol"] for o in out}
            for name, light in s.symbols().items():
                desc = light.get("description") or ""
                if qn in re.sub(r"[^a-z0-9]", "", name.lower()) or (len(ql) >= 3 and ql in desc.lower()):
                    canon = _cfd_canonical(name) or name
                    if canon in known:
                        continue
                    known.add(canon)
                    out.append({"symbol": canon, "name": desc or name, "type": "CFD", "exchange": "a tua corretora",
                                "ok": True, "why": None if canon in CFD_SPECS else "só no Modo Real (demo ou real)"})
                if len(out) >= limit:
                    break
        except Exception as e:
            log.debug("Pesquisa na corretora falhou: %s", e)
    return out[:limit]


def validate_cfd(symbol: str, md) -> dict:
    """Confirma que o bot consegue negociar o instrumento: dados, horário, quantidade mínima e margem."""
    from . import ctrader
    from .catalog import CFD_GROUP_NAMES, CFD_SPECS
    raw = symbol.strip().upper()
    sym = _cfd_canonical(raw) or re.sub(r"[^A-Z0-9.]", "", raw)
    res = {"ok": False, "symbol": sym, "type": "CFD", "problems": [], "warnings": []}
    spec = CFD_SPECS.get(sym)
    session = md.session() or ctrader.any_session()
    if spec is None and session is None:
        res["problems"].append("não está no catálogo; para instrumentos da tua corretora liga primeiro a conta cTrader "
                               "(Modo Real > Ligar ao cTrader)")
        return res
    broker_name = None
    if session is not None:
        try:
            light = session.resolve(sym)
            if light:
                broker_name = light.get("symbolName")
                sp = session.spec(sym)
                if not sp["trading"]:
                    res["problems"].append("a corretora tem a negociação deste instrumento desligada")
                    return res
                res["min_qty"] = sp["min_qty"]
            elif spec is None:
                res["problems"].append("a tua corretora cTrader não tem este instrumento")
                return res
            else:
                res["warnings"].append("a tua corretora não tem este instrumento com este nome: só funciona no Modo Teste")
        except Exception as e:
            if spec is None:
                res["problems"].append(f"não foi possível confirmar na corretora ({ctrader.friendly(e)})")
                return res
            res["warnings"].append("não foi possível confirmar na corretora agora")
    try:
        if spec is not None:
            from .cfddata import CfdData
            h = CfdData("cfd_paper").ohlcv(sym, "1h", 300, closed_only=False)
        else:
            h = session.ohlcv(sym, "1h", 300, closed_only=False)
    except Exception as e:
        res["problems"].append(f"sem histórico de preços ({e})")
        return res
    if len(h) < 150:
        res["problems"].append(f"histórico insuficiente ({len(h)} velas de 1h; o bot precisa de 150)")
        return res
    price = float(h["close"].iloc[-1])
    name = spec["name"] if spec else ((session.resolve(sym) or {}).get("description") or sym)
    res.update(name=name, exchange=CFD_GROUP_NAMES.get((spec or {}).get("group"), "a tua corretora"),
               currency=(spec or {}).get("quote") or md.currency(sym), broker_symbol=broker_name)
    min_qty = res.get("min_qty") or (spec or {}).get("min") or 0
    rate = (spec or {}).get("margin", 0.2)
    try:
        fx = md.fx(sym)
        if min_qty:
            exposure = min_qty * price * fx
            fmt = lambda v: f"{v:,.0f}".replace(",", " ")  # 3 694 (em português a vírgula é decimal)
            res["warnings"].append(f"posição mínima {min_qty:g} (≈ {fmt(exposure)} {config.CFD_CURRENCY} de exposição, "
                                   f"margem ≈ {fmt(exposure * rate)} {config.CFD_CURRENCY})")
    except Exception:
        pass
    if spec is None:
        res["warnings"].append("fora do catálogo: só é analisado no Modo Real (demo ou real)")
    res["ok"] = True
    return res
