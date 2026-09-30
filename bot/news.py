"""
Notícias e contexto macro, de várias fontes:
- manchetes RSS de 11-13 sites (cripto, bolsa ou forex/matérias-primas, conforme o mercado; grátis, a cada 10 min);
- calendário económico (Fed, inflação, emprego...) da ForexFactory;
- dados globais do mercado (CoinGecko em cripto; dólar, juros, VIX, S&P 500, ouro e petróleo nos outros);
- resumo da IA com pesquisa na web, por ativo (só para os ativos que vão ser analisados).
"""
import email.utils
import json
import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

log = logging.getLogger("bot")
UA = {"User-Agent": "Mozilla/5.0 (TradingBotIA news reader)"}

FEEDS = {
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "Cointelegraph": "https://cointelegraph.com/rss",
    "Decrypt": "https://decrypt.co/feed",
    "The Block": "https://www.theblock.co/rss.xml",
    "Bitcoin Magazine": "https://bitcoinmagazine.com/.rss/full/",
    "CryptoSlate": "https://cryptoslate.com/feed/",
    "CryptoPotato": "https://cryptopotato.com/feed/",
    "NewsBTC": "https://www.newsbtc.com/feed/",
    "Bitcoinist": "https://bitcoinist.com/feed/",
    "U.Today": "https://u.today/rss",
    "The Defiant": "https://thedefiant.io/api/feed",
    "CryptoBriefing": "https://cryptobriefing.com/feed/",
    "BeInCrypto": "https://beincrypto.com/feed/",
}

STOCK_FEEDS = {
    "CNBC": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114",
    "CNBC Markets": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258",
    "CNBC Tech": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=19854910",
    "MarketWatch": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
    "WSJ Markets": "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain",
    "Financial Times": "https://www.ft.com/markets?format=rss",
    "Seeking Alpha": "https://seekingalpha.com/market_currents.xml",
    "Investing.com": "https://www.investing.com/rss/news_25.rss",
    "Nasdaq": "https://www.nasdaq.com/feed/rssoutbound?category=Stocks",
    "Yahoo Finance": "https://finance.yahoo.com/news/rssindex",
    "Fortune": "https://fortune.com/feed/",
}
YAHOO_TICKER_RSS = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"

CFD_FEEDS = {
    "FXStreet": "https://www.fxstreet.com/rss/news",
    "FXStreet Análise": "https://www.fxstreet.com/rss/analysis",
    "investingLive (ForexLive)": "https://investinglive.com/feed/news",
    "Investing.com Forex": "https://www.investing.com/rss/news_1.rss",
    "Investing.com Matérias-primas": "https://www.investing.com/rss/news_11.rss",
    "Investing.com Economia": "https://www.investing.com/rss/news_14.rss",
    "OilPrice": "https://oilprice.com/rss/main",
    "Google News (ouro, forex, petróleo)": "https://news.google.com/rss/search?q=gold+OR+forex+OR+%22oil+prices%22+OR+"
                                           "%22Federal+Reserve%22+when:1d&hl=en-US&gl=US&ceid=US:en",
    "WSJ Markets": "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain",
    "MarketWatch": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
    "Yahoo Finance": "https://finance.yahoo.com/news/rssindex",
}
# indicadores globais (Yahoo) para ações e CFDs
MACRO_TICKERS = {"dxy": ("DX-Y.NYB", "Índice do dólar (DXY)"), "us10y_yield": ("^TNX", "Juro a 10 anos EUA (%)"),
                 "vix": ("^VIX", "VIX (medo)"), "sp500_futures": ("ES=F", "S&P 500 (futuros)"),
                 "gold": ("GC=F", "Ouro"), "wti_oil": ("CL=F", "Petróleo WTI"), "eurusd": ("EURUSD=X", "EUR/USD")}


def feeds_for(market: str) -> dict:
    return {"stocks": STOCK_FEEDS, "cfd": CFD_FEEDS}.get(market, FEEDS)

# Nomes por extenso (sem distinguir maiúsculas). O ticker (ex.: "SOL") é procurado só em maiúsculas.
ASSET_NAMES = {
    "BTC": ["bitcoin"], "ETH": ["ethereum", "ether"], "SOL": ["solana"], "XRP": ["ripple", "xrp"],
    "BNB": ["bnb chain", "binance coin"], "DOGE": ["dogecoin"], "ADA": ["cardano"], "AVAX": ["avalanche"],
    "LINK": ["chainlink"], "DOT": ["polkadot"], "LTC": ["litecoin"], "SUI": ["sui network"], "NEAR": ["near protocol"],
    "HBAR": ["hedera"], "TRX": ["tron"], "TON": ["toncoin"], "UNI": ["uniswap"], "AAVE": ["aave"], "ONDO": ["ondo"],
    "TAO": ["bittensor"], "ENA": ["ethena"], "HYPE": ["hyperliquid"], "ZEC": ["zcash"], "RUNE": ["thorchain"],
    "XLM": ["stellar"], "PEPE": ["pepe"], "SHIB": ["shiba inu"], "PUMP": ["pump.fun"], "APT": ["aptos"],
    "ARB": ["arbitrum"], "OP": ["optimism"], "FIL": ["filecoin"], "ATOM": ["cosmos"], "BCH": ["bitcoin cash"],
    "ETC": ["ethereum classic"], "WLD": ["worldcoin"], "RENDER": ["render network"], "INJ": ["injective"],
    "SEI": ["sei network"], "TIA": ["celestia"], "JUP": ["jupiter exchange"], "WIF": ["dogwifhat"], "BONK": ["bonk"],
    "FET": ["fetch.ai"], "POL": ["polygon"], "ICP": ["internet computer"], "VET": ["vechain"], "ALGO": ["algorand"],
}

NEWS_SCHEMA = {
    "type": "object",
    "properties": {
        "sentiment_score": {"type": "number", "description": "-1 (muito negativo) a 1 (muito positivo)"},
        "sentiment_label": {"type": "string", "enum": ["very_bearish", "bearish", "neutral", "bullish", "very_bullish"]},
        "summary": {"type": "string"},
        "key_events": {"type": "array", "items": {"type": "string"}},
        "upcoming_risk_events": {"type": "array", "items": {"type": "string"}},
        "catalyst_next_24h": {"type": "string", "enum": ["positive", "negative", "none", "mixed"]},
        "data_quality": {"type": "string", "enum": ["good", "limited", "none"]},
    },
    "required": ["sentiment_score", "sentiment_label", "summary", "key_events", "upcoming_risk_events",
                 "catalyst_next_24h", "data_quality"],
    "additionalProperties": False,
}

PROMPT = """Data/hora atual: {now} UTC.
Objetivo: avaliar o impacto provável das notícias no preço de {asset} ({symbol}) nas próximas horas/dias.

Manchetes recentes recolhidas de {n_sources} sites {kind} sobre {asset}:
{asset_headlines}

Manchetes gerais do mercado:
{market_headlines}

Eventos macroeconómicos de alto impacto (hora UTC):
{calendar}

Dados globais: {global_data}

Usa pesquisa na web para confirmar e completar (últimas 48h): {focus}, macro (Fed, inflação, juros, dólar, bolsa americana) e regulação.
Privilegia fontes fiáveis e recentes, indica as datas e não inventes nada. Se não houver nada relevante, diz isso e usa data_quality = "none".
sentiment_score = impacto provável no preço de {asset} (-1 muito negativo, 0 neutro, 1 muito positivo).
Escreve em português de Portugal, de forma concisa (summary com no máximo 4 frases; no máximo 6 key_events)."""


def _strip_html(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


def _parse_date(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return email.utils.parsedate_to_datetime(s).timestamp()
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(s.strip().replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _matcher(asset: str, stocks: bool = False, cfd: bool = False):
    if cfd:
        from .catalog import CFD_SPECS
        spec = CFD_SPECS.get(asset) or {}
        names = [n for n in spec.get("news", []) if len(n) > 3]
        parts = [rf"\b{re.escape(asset)}\b"]
        if len(asset) == 6 and asset.isalpha():  # XAUUSD / EURUSD também aparece como XAU/USD, EUR/USD
            parts.append(rf"\b{asset[:3]}\s?/\s?{asset[3:]}\b")
        parts += [rf"\b{re.escape(n)}\b" for n in spec.get("news", []) if len(n) <= 3]  # tickers curtos (XAU, BoE)
    elif stocks:
        from .assets import display_name
        from .stockdata import STOCK_NAMES
        names = list(STOCK_NAMES.get(asset, []))
        extra = display_name(asset)
        if extra and not names:  # ex.: "Rheinmetall AG" -> "Rheinmetall"
            names = [extra.split(",")[0].replace(" AG", "").replace(" SA", "").replace(" Inc.", "").replace(" Corp", "").strip()]
        base = asset.split(".")[0]
        # em ações só aceita o ticker em formatos inequívocos: (AAPL), $AAPL, NASDAQ:AAPL
        parts = [rf"\({re.escape(base)}\)", rf"\${re.escape(base)}\b", rf":\s?{re.escape(base)}\b"]
    else:
        names = ASSET_NAMES.get(asset, [])
        parts = [rf"(?<![A-Za-z]){re.escape(asset)}(?![A-Za-z])"]  # ticker em maiúsculas
    rx_ticker = re.compile("|".join(parts))
    rx_names = re.compile("|".join(rf"\b{re.escape(n)}\b" for n in names), re.I) if names else None

    def match(text: str) -> bool:
        return bool(rx_ticker.search(text) or (rx_names and rx_names.search(text)))
    return match


class NewsHub:
    def __init__(self, client=None, model: str = "gpt-5.4-mini", refresh_minutes: int = 60, on_usage=None,
                 market: str = "crypto"):
        self.market = market
        self.feeds = feeds_for(market)
        self._ticker_news: dict[str, tuple[float, list]] = {}
        self.client = client
        self.model = model
        self.ttl = refresh_minutes * 60
        self.on_usage = on_usage
        self.lock = threading.Lock()
        self._items: list[dict] = []
        self._feeds_ts = 0.0
        self._global = (0.0, None)
        self._ai: dict[str, tuple[float, dict]] = {}
        self.feed_status: dict[str, str] = {}

    # ---------------------------------------------------------------- RSS
    @staticmethod
    def _fetch_feed(item) -> tuple[str, list, str]:
        name, url = item
        try:
            r = requests.get(url, headers=UA, timeout=12)
            root = ET.fromstring(r.content)
            atom = "{http://www.w3.org/2005/Atom}"
            nodes = root.findall(".//item") or root.findall(f".//{atom}entry")
            out = []
            for n in nodes[:40]:
                title = _strip_html(n.findtext("title") or n.findtext(f"{atom}title") or "")
                link = n.findtext("link") or ""
                if not link:
                    el = n.find(f"{atom}link")
                    link = el.get("href", "") if el is not None else ""
                ts = _parse_date(n.findtext("pubDate") or n.findtext(f"{atom}updated") or n.findtext(f"{atom}published"))
                desc = _strip_html(n.findtext("description") or n.findtext(f"{atom}summary") or "")[:280]
                if title and ts:
                    out.append({"source": name, "title": title, "link": link.strip(), "ts": ts, "summary": desc})
            return name, out, "ok"
        except Exception as e:
            return name, [], f"erro: {type(e).__name__}"

    def refresh_feeds(self, force: bool = False):
        if not force and time.time() - self._feeds_ts < 600:
            return
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(self._fetch_feed, self.feeds.items()))
        cutoff = time.time() - 48 * 3600
        seen, items = set(), []
        for name, rows, status in results:
            self.feed_status[name] = f"{status} ({len(rows)})"
            for it in rows:
                key = re.sub(r"\W+", "", it["title"].lower())[:80]
                if it["ts"] >= cutoff and key not in seen:
                    seen.add(key)
                    items.append(it)
        items.sort(key=lambda x: -x["ts"])
        with self.lock:
            self._items = items
            self._feeds_ts = time.time()
        ok = sum(1 for s in self.feed_status.values() if s.startswith("ok"))
        log.info("Notícias: %d manchetes de %d/%d sites", len(items), ok, len(self.feeds))

    def headlines(self, asset: str | None = None, hours: float = 24, limit: int = 10) -> list[dict]:
        try:
            self.refresh_feeds()
        except Exception as e:
            log.warning("Falha a atualizar notícias RSS: %s", e)
        now = time.time()
        stocks = self.market == "stocks"
        cfd = self.market == "cfd"
        match = _matcher(asset, stocks, cfd) if asset else None
        out = []
        with self.lock:
            items = list(self._items)
        if asset and (stocks or cfd):
            items = sorted(items + self.ticker_news(asset), key=lambda x: -x["ts"])
        for it in items:
            if now - it["ts"] > hours * 3600:
                continue
            if match and not it.get("matched") and not match(it["title"] + " " + it["summary"]):
                continue
            out.append({"age_h": round((now - it["ts"]) / 3600, 1), "source": it["source"], "title": it["title"],
                        "link": it["link"], "ts": it["ts"]})
            if len(out) >= limit:
                break
        return out

    def ticker_news(self, symbol: str) -> list:
        """Notícias próprias de uma ação no Yahoo Finance (cache de 15 min)."""
        hit = self._ticker_news.get(symbol)
        if hit and time.time() - hit[0] < 900:
            return hit[1]
        ticker = symbol
        if self.market == "cfd":
            from .catalog import CFD_SPECS
            ticker = (CFD_SPECS.get(symbol) or {}).get("yahoo") or symbol
        _, rows, _ = self._fetch_feed((f"Yahoo {symbol.split('.')[0]}", YAHOO_TICKER_RSS.format(ticker=ticker)))
        for r in rows:
            r["matched"] = True
        self._ticker_news[symbol] = (time.time(), rows)
        return rows

    # ---------------------------------------------------------------- macro
    _cal_shared = [0.0, []]           # partilhado por toda a aplicação (o site limita os pedidos)
    _cal_lock = threading.Lock()

    def calendar(self) -> list[dict]:
        with NewsHub._cal_lock:
            ts, data = NewsHub._cal_shared
            if not (ts and time.time() - ts < 3 * 3600):
                try:
                    r = requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", headers=UA, timeout=15)
                    fresh = r.json()
                    if not isinstance(fresh, list):
                        raise ValueError("resposta inesperada")
                    NewsHub._cal_shared[:] = [time.time(), fresh]
                except Exception as e:
                    code = getattr(locals().get("r"), "status_code", "")
                    log.warning("Calendário económico indisponível%s (nova tentativa em 15 min): %s",
                                f" (HTTP {code})" if code else "", str(e)[:100])
                    NewsHub._cal_shared[:] = [time.time() - 3 * 3600 + 900, data]  # não repete a cada análise
            events = NewsHub._cal_shared[1]
        now = time.time()
        out = []
        for ev in events or []:
            t = _parse_date(ev.get("date"))
            if t is None:
                continue
            hours = (t - now) / 3600
            important = ev.get("impact") == "High" or (ev.get("impact") == "Medium" and ev.get("country") == "USD")
            if important and ev.get("country") in ("USD", "EUR", "CNY", "JPY", "GBP") and -6 <= hours <= 48:
                out.append({
                    "time_utc": datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M"),
                    "hours_from_now": round(hours, 1), "country": ev.get("country"), "event": ev.get("title"),
                    "impact": ev.get("impact"), "forecast": ev.get("forecast") or None, "previous": ev.get("previous") or None,
                })
        out.sort(key=lambda e: e["hours_from_now"])
        return out[:12]

    def global_market(self) -> dict | None:
        ts, data = self._global
        if data and time.time() - ts < 900:
            return data
        if self.market != "crypto":
            return self._macro_snapshot(data)
        try:
            g = requests.get("https://api.coingecko.com/api/v3/global", headers=UA, timeout=15).json()["data"]
            data = {
                "total_market_cap_usd_bn": round(g["total_market_cap"]["usd"] / 1e9),
                "market_cap_change_24h_pct": round(g["market_cap_change_percentage_24h_usd"], 2),
                "btc_dominance_pct": round(g["market_cap_percentage"]["btc"], 2),
                "eth_dominance_pct": round(g["market_cap_percentage"]["eth"], 2),
            }
            self._global = (time.time(), data)
        except Exception as e:
            log.debug("CoinGecko indisponível: %s", e)
        return data

    def _macro_snapshot(self, cached):
        """Dólar, juros, VIX, S&P 500, ouro, petróleo e EUR/USD (Yahoo, cache de 15 min)."""
        try:
            import yfinance as yf
            tickers = [t for t, _ in MACRO_TICKERS.values()]
            raw = yf.download(tickers, period="7d", interval="1d", progress=False, group_by="ticker", threads=True,
                              auto_adjust=False)
            data = {}
            for key, (t, label) in MACRO_TICKERS.items():
                try:
                    c = raw[t]["Close"].dropna()
                except (KeyError, TypeError):
                    continue
                if len(c) >= 2:
                    data[key] = {"label": label, "last": round(float(c.iloc[-1]), 4),
                                 "change_1d_pct": round((float(c.iloc[-1]) / float(c.iloc[-2]) - 1) * 100, 2)}
            if data:
                self._global = (time.time(), data)
                return data
        except Exception as e:
            log.debug("Indicadores globais indisponíveis: %s", e)
        return cached

    def macro(self) -> dict:
        return {
            "high_impact_events": self.calendar(),
            "global_market": self.global_market(),
            "market_headlines": [{k: h[k] for k in ("age_h", "source", "title")}
                                 for h in self.headlines(None, hours=12, limit=10)],
        }

    # ---------------------------------------------------------------- IA (pesquisa web)
    def ai_digest(self, symbol: str, asset_headlines: list, macro: dict) -> dict | None:
        if not self.client:
            return None
        asset = symbol if self.market != "crypto" else symbol.split("/")[0]
        hit = self._ai.get(asset)
        if hit and time.time() - hit[0] < self.ttl:
            return hit[1]
        if self.market == "cfd":
            from .catalog import CFD_SPECS
            kind = "de forex, matérias-primas e mercados"
            name = (CFD_SPECS.get(symbol) or {}).get("name") or symbol
            focus = (f"o que move {name} agora (bancos centrais, dados macro, dólar, juros, geopolítica, procura e "
                     "oferta, fluxos de refúgio, posicionamento/COT, previsões dos grandes bancos)")
        elif self.market == "stocks":
            kind = "financeiros"
            focus = ("notícias da empresa/ETF (resultados, previsões, analistas, produtos, processos, fusões, "
                     "fluxos, notícias do setor)")
        else:
            kind = "de cripto"
            focus = ("notícias específicas do ativo (hacks, listagens, desbloqueios de tokens, upgrades, parcerias, "
                     "ETFs, ações de empresas/tesourarias, processos judiciais)")
        fmt = lambda hs: "\n".join(f"- [{h['age_h']}h] {h['source']}: {h['title']}" for h in hs) or "- (nenhuma)"
        cal = "\n".join(f"- {e['time_utc']} {e['country']} {e['event']} (prev. {e['forecast']}, ant. {e['previous']})"
                        for e in macro.get("high_impact_events", [])) or "- (nenhum nas próximas 48h)"
        try:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
            resp = self.client.responses.create(
                model=self.model,
                input=PROMPT.format(now=now, asset=asset, symbol=symbol, n_sources=len(self.feeds), kind=kind, focus=focus,
                                    asset_headlines=fmt(asset_headlines), market_headlines=fmt(macro.get("market_headlines", [])),
                                    calendar=cal, global_data=json.dumps(macro.get("global_market"), ensure_ascii=False)),
                tools=[{"type": "web_search"}],
                text={"format": {"type": "json_schema", "name": "news", "schema": NEWS_SCHEMA, "strict": True}},
            )
            data = json.loads(resp.output_text)
            data["fetched_at_utc"] = now
            if self.on_usage and resp.usage:
                self.on_usage(self.model, resp.usage.input_tokens, resp.usage.output_tokens)
            self._ai[asset] = (time.time(), data)
            log.info("Notícias %s: %s (%.2f) | %d manchetes próprias", asset, data["sentiment_label"],
                     data["sentiment_score"], len(asset_headlines))
            return data
        except Exception as e:
            log.warning("Falha na pesquisa de notícias de %s: %s", asset, e)
            return hit[1] if hit else None

    def for_symbol(self, symbol: str, macro: dict, use_ai: bool = True) -> dict:
        asset = symbol if self.market != "crypto" else symbol.split("/")[0]
        hs = self.headlines(asset, hours=36, limit=8)
        return {
            "ai_summary": self.ai_digest(symbol, hs, macro) if use_ai else None,
            "headlines": [{k: h[k] for k in ("age_h", "source", "title")} for h in hs],
        }
