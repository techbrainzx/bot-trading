"""
Corretora cTrader (CFDs: ouro, prata, forex, índices, petróleo) via cTrader Open API.

- Ligação WebSocket com mensagens JSON: wss://demo.ctraderapi.com:5036 (contas demo) e
  wss://live.ctraderapi.com:5036 (contas reais). Os dois ambientes são totalmente separados.
- Autorização OAuth: a aplicação (Client ID e Secret criados em openapi.ctrader.com) mais um token de acesso da
  tua conta cTrader ID (válido ~30 dias; é renovado sozinho com o refresh token).
- Preços de velas e cotações vêm em 1/100000 da unidade; volumes em 0,01 da unidade (100 = 1 onça, 1 barril...).
- As ordens NUNCA são repetidas automaticamente (uma ordem repetida pode duplicar a posição).
- Cada posição aberta pelo bot leva um stop-loss na própria corretora: protege mesmo com o PC desligado.
"""
import json
import logging
import math
import queue
import re
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from websockets.sync.client import connect

import config
from .broker import Fill, OrderError, PositionGone
from .catalog import CFD_SPECS

log = logging.getLogger("bot")

HOSTS = {"demo": "demo.ctraderapi.com", "live": "live.ctraderapi.com"}
PORT = 5036
AUTH_URL = "https://id.ctrader.com/my/settings/openapi/grantingaccess/"
TOKEN_URL = "https://openapi.ctrader.com/apps/token"
PORTAL_URL = "https://openapi.ctrader.com/apps"
SCALE = 100_000  # preços das velas/cotações e distâncias relativas
LABEL = "TradingBotIA"


class PT:
    """Tipos de mensagem (ProtoOAPayloadType)."""
    ERROR_COMMON = 50
    HEARTBEAT = 51
    APP_AUTH = 2100
    ACCOUNT_AUTH = 2102
    NEW_ORDER = 2106
    AMEND_SLTP = 2110
    CLOSE_POSITION = 2111
    ASSET_LIST = 2112
    SYMBOLS_LIST = 2114
    SYMBOL_BY_ID = 2116
    TRADER = 2121
    RECONCILE = 2124
    EXECUTION = 2126
    SUBSCRIBE_SPOTS = 2127
    SPOT = 2131
    ORDER_ERROR = 2132
    TRENDBARS = 2137
    ERROR_RES = 2142
    TOKEN_INVALIDATED = 2147
    CLIENT_DISCONNECT = 2148
    ACCOUNTS_BY_TOKEN = 2149
    ACCOUNT_DISCONNECT = 2164
    REFRESH_TOKEN = 2173
    DEALS_BY_POSITION = 2179


PERIODS = {"1m": 1, "5m": 5, "15m": 7, "30m": 8, "1h": 9, "4h": 10, "1d": 12, "1w": 13, "1wk": 13}
TF_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000,
         "1d": 86_400_000, "1w": 604_800_000, "1wk": 604_800_000}
EXEC_TYPES = {2: "ACCEPTED", 3: "FILLED", 4: "REPLACED", 5: "CANCELLED", 6: "EXPIRED", 7: "REJECTED",
              8: "CANCEL_REJECTED", 9: "SWAP", 10: "DEPOSIT_WITHDRAW", 11: "PARTIAL_FILL"}
ERRORS_PT = {
    "CH_CLIENT_AUTH_FAILURE": "Client ID ou Secret da aplicação errados (confirma em openapi.ctrader.com).",
    "CH_CLIENT_NOT_AUTHENTICATED": "a aplicação não está autenticada.",
    "CH_ACCESS_TOKEN_INVALID": "a autorização (token) da tua conta cTrader expirou ou foi revogada: liga de novo.",
    "CH_CTID_TRADER_ACCOUNT_NOT_FOUND": "conta cTrader não encontrada para esta autorização.",
    "ACCOUNT_NOT_AUTHORIZED": "a conta não autorizou esta aplicação.",
    "NOT_ENOUGH_MONEY": "margem insuficiente na conta.",
    "MARKET_CLOSED": "mercado fechado.",
    "TRADING_BAD_VOLUME": "quantidade não aceite pela corretora.",
    "TRADING_BAD_STOPS": "stop-loss demasiado perto do preço para esta corretora.",
    "POSITION_NOT_FOUND": "a posição já não existe na corretora.",
    "SYMBOL_NOT_FOUND": "instrumento não existe nesta conta.",
    "TRADING_DISABLED": "negociação desativada neste instrumento.",
}


def _now_ms() -> int:
    return int(time.time() * 1000)


def exec_type(v) -> str:
    if isinstance(v, str):
        return v.replace("ORDER_", "")
    try:
        return EXEC_TYPES.get(int(v), str(v))
    except (TypeError, ValueError):
        return str(v)


def is_buy(v) -> bool:
    return v in (1, "1", "BUY")


class CtraderError(Exception):
    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code or ""


def friendly(e: Exception) -> str:
    code = getattr(e, "code", "") or ""
    return ERRORS_PT.get(code) or str(e)[:300]


# ============================================================ ligação
class CtraderClient:
    """Uma ligação WebSocket (demo ou real). Thread-safe: pedidos de qualquer thread, respostas por clientMsgId."""

    def __init__(self, env: str, on_connect=None):
        self.env = env
        self.url = f"wss://{HOSTS[env]}:{PORT}"
        self.on_connect = on_connect
        self.ws = None
        self.connected = False
        self._gen = None
        self.lock = threading.RLock()
        self.send_lock = threading.Lock()
        self.waiters: dict[str, queue.Queue] = {}
        self.events: deque = deque(maxlen=400)   # eventos de execução recentes (para confirmar ordens)
        self.spots: dict[int, dict] = {}
        self._hist_next = 0.0
        self._hist_lock = threading.Lock()

    # ------------------------------------------------------------------ ligação
    def ensure(self):
        with self.lock:
            if self.connected:
                return
            self._open()
            if self.on_connect:
                try:
                    self.on_connect()
                except Exception:
                    self.close()
                    raise

    def _open(self):
        try:
            self.ws = connect(self.url, open_timeout=20, close_timeout=5, max_size=2 ** 26, ping_interval=None)
        except Exception as e:
            raise CtraderError(f"sem ligação ao cTrader ({self.env}): {e}", "NETWORK") from e
        gen = object()
        self._gen = gen
        self.connected = True
        threading.Thread(target=self._reader, args=(self.ws, gen), daemon=True, name=f"ctrader-{self.env}-rx").start()
        threading.Thread(target=self._heartbeat, args=(self.ws, gen), daemon=True, name=f"ctrader-{self.env}-hb").start()

    def close(self):
        with self.lock:
            self.connected = False
            self._gen = None
            try:
                if self.ws:
                    self.ws.close()
            except Exception:
                pass
        for q in list(self.waiters.values()):  # acorda quem estava à espera de resposta
            q.put({"payloadType": -1, "payload": {"errorCode": "DISCONNECTED", "description": "ligação ao cTrader fechada"}})

    def _reader(self, ws, gen):
        try:
            for raw in ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                self._dispatch(msg)
        except Exception as e:
            log.debug("cTrader (%s): ligação terminou (%s)", self.env, e)
        finally:
            if self._gen is gen:
                self.connected = False
                for q in list(self.waiters.values()):
                    q.put({"payloadType": -1, "payload": {"errorCode": "DISCONNECTED",
                                                          "description": "a ligação ao cTrader caiu"}})

    def _heartbeat(self, ws, gen):
        while self._gen is gen:
            time.sleep(10)
            if self._gen is not gen:
                return
            try:
                self._send({"payloadType": PT.HEARTBEAT, "payload": {}}, ws)
            except Exception:
                return

    def _send(self, msg: dict, ws=None):
        with self.send_lock:
            (ws or self.ws).send(json.dumps(msg))

    def _dispatch(self, msg: dict):
        try:
            pt = int(msg.get("payloadType") or 0)
        except (TypeError, ValueError):
            return
        payload = msg.get("payload") or {}
        cid = msg.get("clientMsgId")
        if pt == PT.HEARTBEAT:
            return
        if pt == PT.SPOT:
            sid = int(payload.get("symbolId") or 0)
            cur = self.spots.setdefault(sid, {})
            if payload.get("bid"):
                cur["bid"] = payload["bid"] / SCALE
            if payload.get("ask"):
                cur["ask"] = payload["ask"] / SCALE
            cur["ts"] = time.time()
            return
        if pt in (PT.EXECUTION, PT.ORDER_ERROR):
            self.events.append((time.time(), pt, payload))
        if pt in (PT.TOKEN_INVALIDATED, PT.CLIENT_DISCONNECT, PT.ACCOUNT_DISCONNECT):
            log.warning("cTrader: %s (%s). A ligar de novo no próximo pedido.",
                        {PT.TOKEN_INVALIDATED: "autorização invalidada", PT.CLIENT_DISCONNECT: "ligação terminada pelo servidor",
                         PT.ACCOUNT_DISCONNECT: "conta desligada"}[pt], payload.get("reason") or payload.get("description") or "")
            threading.Thread(target=self.close, daemon=True).start()
        if cid and cid in self.waiters:
            self.waiters[cid].put(msg)

    # ------------------------------------------------------------------ pedidos
    def _throttle(self):
        """Pedidos de histórico: no máximo ~4 por segundo (o limite do cTrader é 5)."""
        with self._hist_lock:
            wait = self._hist_next - time.time()
            if wait > 0:
                time.sleep(wait)
            self._hist_next = time.time() + 0.26

    @staticmethod
    def _check(msg: dict) -> dict:
        pt = msg.get("payloadType")
        p = msg.get("payload") or {}
        if pt in (PT.ERROR_RES, PT.ERROR_COMMON, PT.ORDER_ERROR, -1) or (pt != PT.EXECUTION and p.get("errorCode")):
            code = str(p.get("errorCode") or "ERROR")
            raise CtraderError(p.get("description") or code, code)
        return msg

    def request(self, pt: int, payload: dict, timeout: float = 25.0, historical: bool = False) -> dict:
        self.ensure()
        cid = uuid.uuid4().hex[:16]
        q: queue.Queue = queue.Queue()
        self.waiters[cid] = q
        try:
            if historical:
                self._throttle()
            try:
                self._send({"clientMsgId": cid, "payloadType": pt, "payload": payload})
            except Exception as e:
                self.connected = False
                raise CtraderError(f"falha a enviar ao cTrader: {e}", "NETWORK") from e
            try:
                msg = q.get(timeout=timeout)
            except queue.Empty:
                raise CtraderError(f"o cTrader não respondeu ({pt})", "TIMEOUT")
        finally:
            self.waiters.pop(cid, None)
        return self._check(msg)

    def order_request(self, pt: int, payload: dict, timeout: float = 45.0) -> dict:
        """Envia uma ordem/fecho e espera pela EXECUÇÃO (não só pela aceitação). Nunca repete o pedido."""
        self.ensure()
        cid = uuid.uuid4().hex[:16]
        q: queue.Queue = queue.Queue()
        self.waiters[cid] = q
        order_id = None
        sent_at = time.time()
        try:
            try:
                self._send({"clientMsgId": cid, "payloadType": pt, "payload": payload})
            except Exception as e:
                self.connected = False
                raise OrderError(f"falha a enviar a ordem ao cTrader: {e}") from e
            deadline = sent_at + timeout
            while time.time() < deadline:
                try:
                    msg = q.get(timeout=1.0)
                except queue.Empty:
                    ev = self._find_fill(order_id, sent_at) if order_id else None
                    if ev:
                        return ev
                    continue
                try:
                    self._check(msg)
                except CtraderError as e:
                    if e.code == "DISCONNECTED" and order_id:
                        continue  # a ordem já foi aceite: tenta confirmar pelos eventos
                    raise
                p = msg.get("payload") or {}
                et = exec_type(p.get("executionType"))
                order_id = order_id or (p.get("order") or {}).get("orderId")
                if et in ("FILLED", "PARTIAL_FILL"):
                    return p
                if et in ("REJECTED", "CANCELLED", "EXPIRED"):
                    code = p.get("errorCode") or et
                    raise CtraderError(f"ordem {et.lower()} pela corretora ({code})", code)
            raise CtraderError("a execução da ordem não foi confirmada a tempo: confirma na plataforma cTrader", "TIMEOUT")
        finally:
            self.waiters.pop(cid, None)

    def _find_fill(self, order_id, since: float) -> dict | None:
        for ts, pt, p in list(self.events):
            if ts < since - 1 or pt != PT.EXECUTION:
                continue
            if (p.get("order") or {}).get("orderId") == order_id and exec_type(p.get("executionType")) in ("FILLED", "PARTIAL_FILL"):
                return p
        return None


# ============================================================ OAuth (token de acesso à conta)
def redirect_uri(port: int = 8765) -> str:
    return f"http://127.0.0.1:{port}/ctrader/callback"


def authorize_url(redirect: str) -> str:
    return AUTH_URL + "?" + urlencode({"client_id": config.CTRADER_CLIENT_ID, "redirect_uri": redirect,
                                       "scope": "trading", "product": "web"})


def _token_call(params: dict) -> dict:
    try:
        r = requests.get(TOKEN_URL, params=params, timeout=25)
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        raise CtraderError(f"sem resposta do servidor de autorização do cTrader: {e}", "NETWORK") from e
    if data.get("errorCode") or not data.get("accessToken"):
        raise CtraderError(data.get("description") or data.get("errorCode") or f"HTTP {r.status_code}",
                           data.get("errorCode") or "AUTH")
    return data


def save_tokens(data: dict):
    config.set_secret("CTRADER_ACCESS_TOKEN", data["accessToken"])
    if data.get("refreshToken"):
        config.set_secret("CTRADER_REFRESH_TOKEN", data["refreshToken"])
    expires = int(time.time() + int(data.get("expiresIn") or 2_628_000))
    config.set_secret("CTRADER_TOKEN_EXPIRES", str(expires))
    drop_sessions()


def exchange_code(code: str, redirect: str) -> dict:
    data = _token_call({"grant_type": "authorization_code", "code": code, "redirect_uri": redirect,
                        "client_id": config.CTRADER_CLIENT_ID, "client_secret": config.CTRADER_CLIENT_SECRET})
    save_tokens(data)
    return data


_refresh_lock = threading.Lock()


def refresh_tokens() -> str:
    with _refresh_lock:
        if not config.CTRADER_REFRESH_TOKEN:
            raise CtraderError("a autorização expirou e não há refresh token: liga de novo ao cTrader.", "CH_ACCESS_TOKEN_INVALID")
        data = _token_call({"grant_type": "refresh_token", "refresh_token": config.CTRADER_REFRESH_TOKEN,
                            "client_id": config.CTRADER_CLIENT_ID, "client_secret": config.CTRADER_CLIENT_SECRET})
        config.set_secret("CTRADER_ACCESS_TOKEN", data["accessToken"])
        if data.get("refreshToken"):
            config.set_secret("CTRADER_REFRESH_TOKEN", data["refreshToken"])
        config.set_secret("CTRADER_TOKEN_EXPIRES", str(int(time.time() + int(data.get("expiresIn") or 2_628_000))))
        log.info("cTrader: autorização renovada automaticamente.")
        return data["accessToken"]


def fresh_token() -> str:
    """Token de acesso válido (renova se faltarem menos de 3 dias)."""
    try:
        expires = int(config.CTRADER_TOKEN_EXPIRES or 0)
    except ValueError:
        expires = 0
    if expires and expires - time.time() < 3 * 86400 and config.CTRADER_REFRESH_TOKEN:
        try:
            return refresh_tokens()
        except CtraderError as e:
            log.warning("cTrader: não foi possível renovar a autorização (%s)", e)
    return config.CTRADER_ACCESS_TOKEN


def has_app() -> bool:
    return bool(config.CTRADER_CLIENT_ID and config.CTRADER_CLIENT_SECRET)


def has_token() -> bool:
    return bool(config.CTRADER_ACCESS_TOKEN)


def _app_auth(client: CtraderClient):
    if not has_app():
        raise CtraderError("falta o Client ID e o Secret da aplicação cTrader.", "NO_APP")
    client.request(PT.APP_AUTH, {"clientId": config.CTRADER_CLIENT_ID, "clientSecret": config.CTRADER_CLIENT_SECRET})


def check_app() -> None:
    """Confirma o Client ID/Secret (autenticação da aplicação no servidor demo)."""
    client = CtraderClient("demo")
    client.on_connect = lambda: _app_auth(client)
    try:
        client.ensure()
    finally:
        client.close()


def list_accounts() -> list:
    """Contas cTrader a que o token dá acesso (demo e reais)."""
    if not has_token():
        return []
    client = CtraderClient("live")
    client.on_connect = lambda: _app_auth(client)
    try:
        token = fresh_token()
        try:
            p = client.request(PT.ACCOUNTS_BY_TOKEN, {"accessToken": token})["payload"]
        except CtraderError as e:
            if "TOKEN" not in e.code:
                raise
            p = client.request(PT.ACCOUNTS_BY_TOKEN, {"accessToken": refresh_tokens()})["payload"]
    finally:
        client.close()
    out = []
    for a in p.get("ctidTraderAccount") or []:
        out.append({"id": int(a["ctidTraderAccountId"]), "is_live": bool(a.get("isLive")), "login": a.get("traderLogin"),
                    "broker": a.get("brokerTitleShort") or ""})
    out.sort(key=lambda a: (a["is_live"], a["broker"], a["login"] or 0))
    return out


# ============================================================ sessão numa conta
def _norm(name: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (name or "").upper())


class CtraderSession:
    """Ligação autenticada a UMA conta cTrader: instrumentos, velas, cotações, saldo e ordens."""

    def __init__(self, env: str, account_id: int):
        self.env = env
        self.account_id = int(account_id)
        self.client = CtraderClient(env, on_connect=self._authenticate)
        self._lock = threading.RLock()
        self._symbols = (0.0, {}, {})
        self._assets: dict[int, str] = {}
        self._details: dict[int, tuple[float, dict]] = {}
        self._map: dict[str, dict | None] = {}
        self._subscribed: set[int] = set()
        self._frames: dict = {}
        self._daily: dict = {}
        self._trader = (0.0, None)

    # ------------------------------------------------------------------ autenticação
    def _authenticate(self):
        _app_auth(self.client)
        token = fresh_token()
        try:
            self.client.request(PT.ACCOUNT_AUTH, {"ctidTraderAccountId": self.account_id, "accessToken": token})
        except CtraderError as e:
            if "TOKEN" not in e.code:
                raise
            self.client.request(PT.ACCOUNT_AUTH, {"ctidTraderAccountId": self.account_id, "accessToken": refresh_tokens()})
        self._subscribed.clear()
        log.info("cTrader: ligado à conta %s (%s).", self.account_id, "demo" if self.env == "demo" else "real")

    def req(self, pt: int, payload: dict | None = None, **kw) -> dict:
        body = {"ctidTraderAccountId": self.account_id, **(payload or {})}
        try:
            return self.client.request(pt, body, **kw)["payload"]
        except CtraderError as e:
            if e.code not in ("DISCONNECTED", "NETWORK", "TIMEOUT"):
                raise
            self.client.close()  # uma nova tentativa com ligação nova (só para pedidos de leitura)
            return self.client.request(pt, body, **kw)["payload"]

    # ------------------------------------------------------------------ instrumentos
    def assets(self) -> dict:
        if not self._assets:
            p = self.req(PT.ASSET_LIST)
            self._assets = {int(a["assetId"]): a.get("name") or a.get("displayName") or "" for a in p.get("asset") or []}
        return self._assets

    def symbols(self) -> dict:
        ts, by_name, _ = self._symbols
        if by_name and time.time() - ts < 6 * 3600:
            return by_name
        p = self.req(PT.SYMBOLS_LIST, {"includeArchivedSymbols": False})
        rows = [s for s in p.get("symbol") or [] if s.get("symbolName") and s.get("enabled", True) is not False]
        by_name = {s["symbolName"].upper(): s for s in rows}
        self._symbols = (time.time(), by_name, {int(s["symbolId"]): s for s in rows})
        self._map.clear()
        return by_name

    def resolve(self, symbol: str) -> dict | None:
        """Nome do bot (ex.: US500) -> instrumento da corretora (ex.: US500, USTEC, US500.cash...)."""
        if symbol in self._map:
            return self._map[symbol]
        names = self.symbols()
        key = symbol.upper()
        hit = names.get(key)
        spec = CFD_SPECS.get(key)
        if not hit:
            aliases = spec["aliases"] if spec else [_norm(key)]
            norm: dict[str, list] = {}
            for n in names:
                norm.setdefault(_norm(n), []).append(n)
            quote_ok = self._quote_filter(spec)
            for a in aliases:
                cands = [n for n in norm.get(a, []) if quote_ok(names[n])]
                if cands:
                    hit = names[cands[0]]
                    break
            if not hit:  # sufixos das corretoras: XAUUSD.r, XAUUSDm, US500.cash...
                for a in aliases:
                    cands = sorted((n for nn, ns in norm.items() if nn.startswith(a) and 0 < len(nn) - len(a) <= 4
                                    for n in ns if quote_ok(names[n])), key=len)
                    if cands:
                        hit = names[cands[0]]
                        break
        self._map[symbol] = hit
        return hit

    def _quote_filter(self, spec):
        if not spec:
            return lambda s: True
        try:
            assets = self.assets()
        except Exception:
            return lambda s: True

        def ok(s):
            q = assets.get(int(s.get("quoteAssetId") or 0))
            return not q or q.upper() == spec["quote"]
        return ok

    def symbol_id(self, symbol: str) -> int:
        light = self.resolve(symbol)
        if not light:
            raise CtraderError(f"{symbol} não existe nesta conta cTrader", "SYMBOL_NOT_FOUND")
        return int(light["symbolId"])

    def details(self, symbol: str) -> dict:
        sid = self.symbol_id(symbol)
        hit = self._details.get(sid)
        if hit and time.time() - hit[0] < 6 * 3600:
            return hit[1]
        p = self.req(PT.SYMBOL_BY_ID, {"symbolId": [sid]})
        d = (p.get("symbol") or [{}])[0]
        self._details[sid] = (time.time(), d)
        return d

    def spec(self, symbol: str) -> dict:
        d = self.details(symbol)
        light = self.resolve(symbol) or {}
        cat = CFD_SPECS.get(symbol.upper(), {})
        quote = self.assets().get(int(light.get("quoteAssetId") or 0)) or cat.get("quote") or "USD"
        return {
            "broker_symbol": light.get("symbolName"), "description": light.get("description") or "",
            "min_qty": int(d.get("minVolume") or 100) / 100, "step": int(d.get("stepVolume") or 100) / 100,
            "max_qty": int(d.get("maxVolume") or 0) / 100 or None, "digits": int(d.get("digits") or 5),
            "lot": int(d.get("lotSize") or 100) / 100, "quote": quote.upper(),
            "margin_rate": cat.get("margin", 0.2), "short_ok": d.get("enableShortSelling", True) is not False,
            "trading": d.get("tradingMode", 0) in (0, "ENABLED", None),
            "units": d.get("measurementUnits") or "",
        }

    def is_open(self, symbol: str, now: datetime | None = None) -> bool | None:
        """Pelo horário do instrumento na corretora (None se desconhecido)."""
        try:
            d = self.details(symbol)
        except Exception:
            return None
        sched = d.get("schedule") or []
        if not sched:
            return None
        tz = ZoneInfo(d.get("scheduleTimeZone") or "UTC")
        local = (now or datetime.now(timezone.utc)).astimezone(tz)
        week_start = (local - timedelta(days=(local.weekday() + 1) % 7)).replace(hour=0, minute=0, second=0, microsecond=0)
        sec = (local - week_start).total_seconds()
        return any(int(i.get("startSecond", 0)) <= sec < int(i.get("endSecond", 0)) for i in sched)

    # ------------------------------------------------------------------ velas e cotações
    def ohlcv(self, symbol: str, tf: str, limit: int, closed_only: bool = True) -> pd.DataFrame:
        sid = self.symbol_id(symbol)
        tf_ms = TF_MS[tf]
        key = (symbol, tf)
        hit = self._frames.get(key)
        ttl = min(60.0, tf_ms / 1000 / 6)
        if hit and time.time() - hit[0] < ttl and limit <= hit[2]:
            df = hit[1]
        else:
            df = self._fetch(sid, tf, limit + 2)
            self._frames[key] = (time.time(), df, limit)
        if closed_only and len(df) and int(df["ts"].iloc[-1]) + tf_ms > _now_ms():
            df = df.iloc[:-1]
        return df.tail(limit).copy()

    def _fetch(self, sid: int, tf: str, need: int) -> pd.DataFrame:
        tf_ms = TF_MS[tf]
        to = _now_ms()
        window = int(tf_ms * need * 1.6 + 4 * 86_400_000)
        rows: dict[int, list] = {}
        for _ in range(8):
            frm = max(0, to - window)
            try:
                p = self.req(PT.TRENDBARS, {"fromTimestamp": frm, "toTimestamp": to, "period": PERIODS[tf],
                                            "symbolId": sid, "count": min(need - len(rows) + 2, 5000)}, historical=True)
            except CtraderError as e:
                if window > tf_ms * 60 and e.code in ("INCORRECT_BOUNDARIES", "INVALID_REQUEST", "REQUEST_FREQUENCY_EXCEEDED"):
                    window //= 3
                    time.sleep(0.5)
                    continue
                raise
            bars = p.get("trendbar") or []
            for tb in bars:
                low = int(tb.get("low") or 0)
                ts = int(tb.get("utcTimestampInMinutes") or 0) * 60_000
                rows[ts] = [ts, (low + int(tb.get("deltaOpen") or 0)) / SCALE, (low + int(tb.get("deltaHigh") or 0)) / SCALE,
                            low / SCALE, (low + int(tb.get("deltaClose") or 0)) / SCALE, float(tb.get("volume") or 0)]
            if len(rows) >= need or not bars:
                break
            to = min(rows) - 1
        df = pd.DataFrame(sorted(rows.values()), columns=["ts", "open", "high", "low", "close", "volume"])
        df["time"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        return df.set_index("time")

    def ticker(self, symbol: str) -> dict:
        sid = self.symbol_id(symbol)
        if sid not in self._subscribed:
            try:
                self.req(PT.SUBSCRIBE_SPOTS, {"symbolId": [sid]})
            except CtraderError as e:
                if "ALREADY" not in e.code:
                    raise
            self._subscribed.add(sid)
        spot = self.client.spots.get(sid) or {}
        end = time.time() + 3
        while not (spot.get("bid") and spot.get("ask")) and time.time() < end:
            time.sleep(0.1)
            spot = self.client.spots.get(sid) or {}
        if spot.get("bid") and spot.get("ask"):
            bid, ask = spot["bid"], spot["ask"]
            last = (bid + ask) / 2
        else:  # sem cotação ao vivo (mercado fechado): último fecho
            m1 = self.ohlcv(symbol, "1m", 3, closed_only=False)
            if not len(m1):
                raise CtraderError(f"sem cotação para {symbol}", "NO_PRICE")
            last = float(m1["close"].iloc[-1])
            bid = ask = last
        prev = self._prev_close(symbol)
        return {"symbol": symbol, "last": last, "bid": bid, "ask": ask,
                "percentage": (last / prev - 1) * 100 if prev else None, "quoteVolume": None,
                "spread_pct": (ask - bid) / last * 100 if last else None, "updated": spot.get("ts")}

    def _prev_close(self, symbol: str) -> float | None:
        hit = self._daily.get(symbol)
        if hit and time.time() - hit[0] < 900:
            return hit[1]
        try:
            d = self.ohlcv(symbol, "1d", 3, closed_only=True)
            prev = float(d["close"].iloc[-1]) if len(d) else None
        except Exception:
            prev = None
        self._daily[symbol] = (time.time(), prev)
        return prev

    # ------------------------------------------------------------------ conta
    def trader(self, max_age: float = 5.0) -> dict:
        ts, data = self._trader
        if data and time.time() - ts < max_age:
            return data
        t = self.req(PT.TRADER)["trader"]
        md = int(t.get("moneyDigits") or 2)
        data = {"balance": int(t.get("balance") or 0) / 10 ** md, "currency": self.assets().get(int(t.get("depositAssetId") or 0), ""),
                "leverage": int(t.get("leverageInCents") or 0) / 100, "broker": t.get("brokerName") or "",
                "login": t.get("traderLogin"), "limited_risk": bool(t.get("isLimitedRisk")), "money_digits": md}
        self._trader = (time.time(), data)
        return data

    def positions(self) -> list:
        return self.req(PT.RECONCILE).get("position") or []

    def deals_for_position(self, position_id: int, since_ms: int) -> list:
        p = self.req(PT.DEALS_BY_POSITION, {"positionId": int(position_id), "fromTimestamp": max(0, since_ms),
                                            "toTimestamp": _now_ms()}, historical=True)
        return p.get("deal") or []


_sessions: dict = {}
_sessions_lock = threading.Lock()


def get_session(env: str, account_id: int) -> CtraderSession:
    key = (env, int(account_id))
    with _sessions_lock:
        s = _sessions.get(key)
        if s is None:
            s = _sessions[key] = CtraderSession(env, int(account_id))
        return s


def drop_sessions():
    with _sessions_lock:
        for s in _sessions.values():
            s.client.close()
        _sessions.clear()


def env_for(mode_key: str) -> str:
    return "demo" if mode_key == "cfd_demo" else "live"


def session_for_mode(mode_key: str) -> CtraderSession | None:
    account = config.ctrader_account(mode_key)
    if not (account and has_app() and has_token()):
        return None
    return get_session(env_for(mode_key), account)


def any_session() -> CtraderSession | None:
    """Qualquer conta ligada (para pesquisar instrumentos da corretora na interface)."""
    for key in ("cfd_demo", "cfd_live"):
        s = session_for_mode(key)
        if s:
            return s
    return None


# ============================================================ verificação e corretora
def verify_ctrader(demo: bool, account_id: int | None = None) -> dict:
    key = "cfd_demo" if demo else "cfd_live"
    account_id = int(account_id or config.ctrader_account(key) or 0)
    result = {"ok": False, "usdt_free": None, "quote": config.CFD_CURRENCY, "testnet": demo, "problems": [],
              "withdrawals_enabled": None, "spot_trading_enabled": None, "account_id": account_id}
    if not has_app():
        result["problems"].append("Falta o Client ID e o Secret da aplicação cTrader (passo 1).")
        return result
    if not has_token():
        result["problems"].append("Ainda não ligaste a tua conta cTrader (botão \"Ligar ao cTrader\").")
        return result
    if not account_id:
        result["problems"].append("Escolhe a conta cTrader que o bot vai usar.")
        return result
    try:
        s = get_session("demo" if demo else "live", account_id)
        info = s.trader(max_age=0)
        found = {sym: (s.resolve(sym) or {}).get("symbolName") for sym in ("XAUUSD", "EURUSD", "US500")}
    except Exception as e:
        drop_sessions()
        msg = friendly(e)
        if getattr(e, "code", "") in ("CH_CTID_TRADER_ACCOUNT_NOT_FOUND", "ACCOUNT_NOT_AUTHORIZED", "INVALID_REQUEST"):
            msg += f" Confirma que a conta escolhida é {'demo' if demo else 'real'}."
        result["problems"].append(msg)
        return result
    result.update(usdt_free=info["balance"], quote=info["currency"] or config.CFD_CURRENCY,
                  account_currency=info["currency"], broker=info["broker"], leverage=info["leverage"],
                  login=info["login"], symbols=found)
    if info["currency"] and info["currency"] not in ("EUR", "USD"):
        result["problems"].append(f"A conta está em {info['currency']}: o bot só suporta contas em EUR ou USD.")
    result["ok"] = not result["problems"]
    return result


class CtraderBroker:
    """Execução no cTrader. Mesma interface que o CfdPaperBroker (bot/broker.py)."""

    def __init__(self, session: CtraderSession, fx):
        self.s = session
        self.fx = fx
        self.name = "cTrader" + (" (demo)" if session.env == "demo" else "")
        info = session.trader(max_age=0)  # valida a ligação logo no arranque
        self.currency = info["currency"]
        self.limited_risk = info["limited_risk"]
        self._free = (0.0, None)

    def spec(self, symbol: str) -> dict:
        return self.s.spec(symbol)

    def min_order_value(self, symbol: str) -> float:
        return 0.0

    def available_quote(self) -> float:
        ts, free = self._free
        if free is not None and time.time() - ts < 6:
            return free
        info = self.s.trader(max_age=0)
        used = 0.0
        for p in self.s.positions():
            used += int(p.get("usedMargin") or 0) / 10 ** int(p.get("moneyDigits") or info["money_digits"])
        free = max(0.0, info["balance"] - used)
        self._free = (time.time(), free)
        return free

    def _volume(self, symbol: str, qty: float) -> int:
        d = self.s.details(symbol)
        step = int(d.get("stepVolume") or 100)
        vol = int(math.floor(qty * 100 / step + 1e-9) * step)
        if vol < int(d.get("minVolume") or 100):
            raise OrderError(f"{symbol}: quantidade {qty:g} abaixo do mínimo da corretora ({int(d.get('minVolume') or 100) / 100:g})")
        return vol

    def open(self, symbol: str, side: str, qty: float, price: float, stop: float | None = None,
             take_profit: float | None = None) -> Fill:
        sid = self.s.symbol_id(symbol)
        vol = self._volume(symbol, qty)
        body = {"ctidTraderAccountId": self.s.account_id, "symbolId": sid, "orderType": 1,
                "tradeSide": 1 if side == "long" else 2, "volume": vol, "label": LABEL, "comment": "Trading Bot IA"}
        if stop:  # stop-loss na própria corretora (protege mesmo com a aplicação fechada)
            body["relativeStopLoss"] = max(1, int(round(abs(price - stop) * SCALE)))
            if self.limited_risk:
                body["guaranteedStopLoss"] = True
        sent = _now_ms()
        try:
            p = self.s.client.order_request(PT.NEW_ORDER, body)
        except CtraderError as e:
            if e.code not in ("TIMEOUT", "DISCONNECTED"):
                raise OrderError(f"{symbol}: {friendly(e)}") from e
            p = self._find_opened(sid, side, sent)  # a ligação caiu: a ordem pode ter sido executada na mesma
            if p is None:
                raise OrderError(f"{symbol}: {friendly(e)} Confirma na plataforma cTrader se a posição abriu.") from e
            log.warning("%s: a confirmação falhou mas a posição foi encontrada na corretora (%s)", symbol, e)
        self._free = (0.0, None)
        deal, pos = p.get("deal") or {}, p.get("position") or {}
        md = int(deal.get("moneyDigits") or pos.get("moneyDigits") or 2)
        filled = int(deal.get("filledVolume") or deal.get("volume") or vol) / 100
        px = float(deal.get("executionPrice") or pos.get("price") or price)
        fee = abs(int(deal.get("commission") or 0)) / 10 ** md
        margin = int(pos.get("usedMargin") or 0) / 10 ** md
        if not margin:
            margin = filled * px * self.fx(symbol) * self.spec(symbol)["margin_rate"]
        fill = Fill(filled, px, -(margin + fee), fee, str(deal.get("positionId") or pos.get("positionId") or ""))
        fill.margin = margin
        fill.stop = float(pos["stopLoss"]) if pos.get("stopLoss") else None
        return fill

    def _find_opened(self, sid: int, side: str, since_ms: int) -> dict | None:
        try:
            for p in self.s.positions():
                td = p.get("tradeData") or {}
                if (int(td.get("symbolId") or 0) == sid and td.get("label") == LABEL
                        and is_buy(td.get("tradeSide")) == (side == "long") and int(td.get("openTimestamp") or 0) >= since_ms - 5000):
                    return {"position": p, "deal": {"positionId": p.get("positionId"), "executionPrice": p.get("price"),
                                                     "filledVolume": td.get("volume"), "moneyDigits": p.get("moneyDigits"),
                                                     "commission": p.get("commission")}}
        except Exception as e:
            log.error("cTrader: não foi possível confirmar a ordem (%s)", e)
        return None

    def close(self, symbol: str, pos: dict, qty: float, price: float) -> Fill:
        pid = int(pos["position_id"])
        full = qty >= pos["qty"] * 0.999
        vol = int(round(pos["qty"] * 100)) if full else self._volume(symbol, qty)
        try:
            p = self.s.client.order_request(PT.CLOSE_POSITION, {"ctidTraderAccountId": self.s.account_id,
                                                                "positionId": pid, "volume": vol})
        except CtraderError as e:
            if e.code in ("POSITION_NOT_FOUND", "POSITION_NOT_OPEN"):
                raise PositionGone(f"{symbol}: {friendly(e)}") from e
            if e.code in ("TIMEOUT", "DISCONNECTED") and full:
                try:
                    if symbol in self.gone({symbol: pos}):
                        raise PositionGone(f"{symbol}: fechada (confirmação perdida com a queda da ligação)") from e
                except CtraderError:
                    pass
            raise OrderError(f"{symbol}: {friendly(e)}") from e
        self._free = (0.0, None)
        return self._close_fill(pos, p.get("deal") or {}, price)

    @staticmethod
    def _close_fill(pos: dict, deal: dict, price_hint: float) -> Fill:
        cpd = deal.get("closePositionDetail") or {}
        md = int(cpd.get("moneyDigits") or deal.get("moneyDigits") or 2)
        closed = int(cpd.get("closedVolume") or deal.get("filledVolume") or deal.get("volume") or 0) / 100 or pos["qty"]
        closed = min(closed, pos["qty"])
        px = float(deal.get("executionPrice") or price_hint)
        gross = int(cpd.get("grossProfit") or 0) / 10 ** md
        swap = int(cpd.get("swap") or 0) / 10 ** md
        fees = (abs(int(cpd.get("commission") or 0)) + abs(int(cpd.get("pnlConversionFee") or 0))) / 10 ** md
        margin_part = pos.get("margin", 0.0) * closed / pos["qty"]
        fill = Fill(closed, px, margin_part + gross + swap - fees, fees, str(deal.get("dealId") or ""))
        fill.swap = swap
        return fill

    def recover_close(self, symbol: str, pos: dict) -> Fill | None:
        """A posição fechou na corretora (stop, alvo, stop-out ou à mão): lê o resultado real."""
        opened = int(datetime.fromisoformat(pos["opened_at"]).timestamp() * 1000) - 60_000
        try:
            deals = self.s.deals_for_position(int(pos["position_id"]), opened)
        except Exception as e:
            log.warning("%s: não foi possível ler o fecho na corretora (%s)", symbol, e)
            return None
        seen = set(pos.get("closing_deals") or [])
        closing = [d for d in deals if d.get("closePositionDetail") and str(d.get("dealId")) not in seen]
        if not closing:
            return None
        closing.sort(key=lambda d: int(d.get("executionTimestamp") or 0))
        fills = [self._close_fill(pos, d, pos["stop"]) for d in closing]
        qty = min(sum(f.qty for f in fills), pos["qty"])
        px = sum(f.price * f.qty for f in fills) / max(sum(f.qty for f in fills), 1e-12)
        fill = Fill(qty, px, sum(f.cash_delta for f in fills), sum(f.fee for f in fills), str(closing[-1].get("dealId")))
        fill.swap = sum(getattr(f, "swap", 0.0) for f in fills)
        return fill

    def gone(self, positions: dict) -> list[str]:
        """Posições do bot que já não existem na corretora."""
        open_ids = {str(p.get("positionId")) for p in self.s.positions()}
        return [sym for sym, pos in positions.items() if pos.get("position_id") and str(pos["position_id"]) not in open_ids]

    def amend(self, symbol: str, pos: dict) -> bool:
        """Leva o stop do bot (break-even, trailing, estrutural) para a corretora."""
        digits = self.s.details(symbol).get("digits") or 5
        body = {"ctidTraderAccountId": self.s.account_id, "positionId": int(pos["position_id"]),
                "stopLoss": round(float(pos["stop"]), int(digits))}
        if self.limited_risk:
            body["guaranteedStopLoss"] = True
        try:
            p = self.s.client.request(PT.AMEND_SLTP, body, timeout=20).get("payload") or {}
        except CtraderError as e:
            log.warning("%s: a corretora recusou o novo stop %.6g (%s)", symbol, pos["stop"], friendly(e))
            return False
        if exec_type(p.get("executionType")) in ("REJECTED", "CANCEL_REJECTED"):
            log.warning("%s: a corretora recusou o novo stop %.6g (%s)", symbol, pos["stop"], p.get("errorCode") or "")
            return False
        return True
