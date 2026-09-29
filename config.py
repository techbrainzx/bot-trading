"""
Configuração do bot.

- Valores por defeito: definidos neste ficheiro.
- Definições alteradas na interface: guardadas em data/settings.json (sobrepõem-se às daqui).
- Chaves de API: ficheiro .env (nunca partilhar).
"""
import json
import os
from pathlib import Path

from dotenv import load_dotenv, set_key

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
load_dotenv(ENV_PATH)

DATA_DIR = Path(os.getenv("BOT_DATA_DIR") or BASE_DIR / "data")
LOGS_DIR = Path(os.getenv("BOT_LOGS_DIR") or BASE_DIR / "logs")
SETTINGS_PATH = DATA_DIR / "settings.json"

# ============================================================
#  MODO  (escolhido na interface)
# ============================================================
MODE = "paper"          # paper = Modo Teste (dinheiro fictício) | live = Modo Real
USE_TESTNET = False     # Modo Real na testnet da Binance (ordens reais, dinheiro fictício)
LIVE_CONFIRMED = False  # passa a True quando confirmas o Modo Real na interface

# ============================================================
#  CHAVES (.env)
# ============================================================
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
EXCHANGE_API_KEY = os.getenv("EXCHANGE_API_KEY", "").strip()
EXCHANGE_API_SECRET = os.getenv("EXCHANGE_API_SECRET", "").strip()
TESTNET_API_KEY = os.getenv("TESTNET_API_KEY", "").strip()
TESTNET_API_SECRET = os.getenv("TESTNET_API_SECRET", "").strip()

# ============================================================
#  MERCADO
# ============================================================
EXCHANGE = "binance"
# Na UE (MiCA) a Binance não permite pares com USDT: usa USDC (≈ 1 dólar) ou EUR.
QUOTE = "USDC"
AUTO_SELECT = True                         # o bot escolhe sozinho as melhores moedas em cada análise
ASSETS = ["BTC", "ETH", "SOL"]             # pares fixos (só usados com AUTO_SELECT = False)
SYMBOLS = [f"{a}/{QUOTE}" for a in ASSETS]  # calculado a partir de ASSETS + QUOTE
PRIMARY_TIMEFRAME = "1h"                   # o bot decide no fecho de cada vela deste timeframe
CONTEXT_TIMEFRAMES_BY_PRIMARY = {
    "15m": ["5m", "1h", "4h"],
    "1h": ["15m", "4h", "1d"],
    "4h": ["1h", "1d", "1w"],
}
CONTEXT_TIMEFRAMES = CONTEXT_TIMEFRAMES_BY_PRIMARY[PRIMARY_TIMEFRAME]
CANDLES = 300                              # velas por timeframe (>= 200 por causa da EMA200)
RECENT_CANDLES_FOR_AI = 24                 # últimas velas do timeframe principal enviadas em bruto

# Radar de oportunidades: analisa tecnicamente as moedas mais negociadas e envia as melhores à IA
SCANNER_ENABLED = True
SCAN_UNIVERSE = 40                         # quantas moedas (as mais negociadas) o radar vê
SCAN_TOP = 3                               # quantas das melhores vão à IA em cada análise
SCAN_MIN_SCORE = 40                        # pontuação mínima (0-100) para ir à IA
SCAN_MIN_VOLUME = 3_000_000                # volume mínimo em 24h (moeda de cotação)
ONLY_WITH_SETUP = True                     # pares fixos sem nenhum setup técnico não gastam IA
SELECTOR_CANDIDATES = 12                   # quantas o radar mostra à IA gestora para ela escolher
RISK_OFF_EXTRA_CONFIDENCE = 0.07           # em mercado de queda generalizada exige mais confiança

# ============================================================
#  OPENAI
# ============================================================
DECISION_MODEL = "gpt-5.5"      # modelo que toma as decisões
REASONING_EFFORT = "medium"     # low | medium | high  (mais alto = pensa mais, mais caro e lento)
DECISION_VOTES = 1              # >1: pede N opiniões independentes e só age se houver maioria (custo xN)
DECISION_FALLBACK_MODEL = "gpt-5.4"  # usado se o modelo principal falhar (None para desativar)

NEWS_ENABLED = True
NEWS_MODEL = "gpt-5.4-mini"     # modelo que pesquisa notícias na web
SELECTOR_MODEL = "gpt-5.4-mini" # IA gestora que escolhe as moedas (rápida e barata)
AI_DAILY_CALL_LIMIT = 300       # máximo de análises da IA por dia (0 = sem limite); stops continuam ativos
NEWS_REFRESH_MINUTES = 60       # cache da pesquisa da IA por ativo (as manchetes RSS atualizam a cada 10 min)

# ============================================================
#  PERFIL DE TRADING
# ============================================================
PROFILE = "equilibrado"         # conservador | equilibrado | agressivo | personalizado
PROFILES = {
    "conservador": {"PRIMARY_TIMEFRAME": "1h", "MIN_CONFIDENCE": 0.65, "MIN_RISK_REWARD": 2.0,
                    "SCAN_TOP": 2, "MAX_OPEN_POSITIONS": 2, "RISK_PER_TRADE_PCT": 0.75},
    "equilibrado": {"PRIMARY_TIMEFRAME": "1h", "MIN_CONFIDENCE": 0.58, "MIN_RISK_REWARD": 1.5,
                    "SCAN_TOP": 3, "MAX_OPEN_POSITIONS": 3, "RISK_PER_TRADE_PCT": 1.0},
    "agressivo": {"PRIMARY_TIMEFRAME": "15m", "MIN_CONFIDENCE": 0.52, "MIN_RISK_REWARD": 1.3,
                  "SCAN_TOP": 4, "MAX_OPEN_POSITIONS": 4, "RISK_PER_TRADE_PCT": 1.5},
}

# ============================================================
#  CAPITAL
# ============================================================
PAPER_START_BALANCE = 10_000.0  # saldo fictício do Modo Teste
LIVE_CAPITAL_USDT = 100.0       # máximo que o bot pode usar no Modo Real (na moeda de cotação)

# ============================================================
#  GESTÃO DE RISCO  (regras fixas: a IA NÃO consegue ultrapassá-las)
# ============================================================
RISK_MODE = "percent"           # percent = % do capital | fixed = valor fixo por trade
RISK_PER_TRADE_PCT = 1.0        # % do capital perdido se o stop-loss for atingido
RISK_FIXED_AMOUNT = 0.50        # valor perdido se o stop-loss for atingido (modo fixed)
CONFIDENCE_SIZING = True        # posição menor quando a confiança é mais baixa (modo percent)
MAX_POSITION_PCT = 30.0         # tamanho máximo de uma posição (% do capital)
MAX_OPEN_POSITIONS = 3
MIN_CONFIDENCE = 0.58           # confiança mínima da IA para comprar
MIN_EXIT_CONFIDENCE = 0.55      # confiança mínima da IA para vender por sinal (stops executam sempre)
MIN_RISK_REWARD = 1.5           # retorno/risco mínimo (já descontando comissões)
DEFAULT_RISK_REWARD = 2.0       # usado se a IA não indicar take-profit
STOP_MIN_ATR = 1.2              # distância mínima do stop (em ATRs do timeframe principal)
STOP_MAX_ATR = 4.0              # distância máxima do stop
MAX_DAILY_LOSS_PCT = 3.0        # perda diária máxima -> sem novas entradas até ao dia seguinte (UTC)
MAX_DRAWDOWN_PCT = 15.0         # queda máxima desde o pico -> bloqueia entradas até desbloqueares
COOLDOWN_AFTER_LOSS_MINUTES = 120
MAX_SPREAD_PCT = 0.25
MIN_ORDER_VALUE = 6.0           # ordem mínima (a Binance exige ~5; a margem evita restos impossíveis de vender)
PENDING_ORDER_CANDLES = 4       # validade das ordens pendentes (em velas do timeframe principal)

# Proteção automática de lucros
BREAKEVEN_AT_R = 1.0            # ao ganhar 1x o risco, o stop sobe para o preço de entrada (+custos)
TRAIL_START_R = 1.5             # a partir de 1.5x o risco, ativa trailing stop
TRAIL_ATR_MULT = 2.5            # trailing stop = máximo desde a entrada - 2.5 ATR

# Custos (Binance spot: 0.1% por ordem)
FEE_PCT = 0.10
SLIPPAGE_PCT = 0.05

# ============================================================
#  CICLO
# ============================================================
CHECK_INTERVAL_SECONDS = 20     # frequência de verificação de stops, alvos e ordens pendentes
DECISION_DELAY_SECONDS = 20     # espera após o fecho da vela antes de analisar
EQUITY_POINT_SECONDS = 300      # frequência dos pontos da curva de capital

# ============================================================
#  Definições editáveis pela interface (persistidas em data/settings.json)
# ============================================================
EDITABLE = {
    "MODE": str, "USE_TESTNET": bool, "LIVE_CONFIRMED": bool,
    "QUOTE": str, "ASSETS": list, "PRIMARY_TIMEFRAME": str, "PROFILE": str,
    "AUTO_SELECT": bool, "AI_DAILY_CALL_LIMIT": int,
    "SCANNER_ENABLED": bool, "SCAN_UNIVERSE": int, "SCAN_TOP": int, "SCAN_MIN_SCORE": int, "ONLY_WITH_SETUP": bool,
    "DECISION_MODEL": str, "REASONING_EFFORT": str, "DECISION_VOTES": int,
    "NEWS_ENABLED": bool, "NEWS_REFRESH_MINUTES": int,
    "PAPER_START_BALANCE": float, "LIVE_CAPITAL_USDT": float,
    "RISK_MODE": str, "RISK_PER_TRADE_PCT": float, "RISK_FIXED_AMOUNT": float,
    "MAX_POSITION_PCT": float, "MAX_OPEN_POSITIONS": int,
    "MIN_CONFIDENCE": float, "MIN_RISK_REWARD": float,
    "MAX_DAILY_LOSS_PCT": float, "MAX_DRAWDOWN_PCT": float, "COOLDOWN_AFTER_LOSS_MINUTES": int,
    "PENDING_ORDER_CANDLES": int,
    "BREAKEVEN_AT_R": float, "TRAIL_START_R": float, "TRAIL_ATR_MULT": float,
}


def current() -> dict:
    return {k: globals()[k] for k in EDITABLE}


def _migrate(values: dict) -> dict:
    """Converte definições antigas (SYMBOLS com USDT) para ASSETS + QUOTE."""
    values = dict(values)
    if "SYMBOLS" in values and "ASSETS" not in values:
        values["ASSETS"] = [s.split("/")[0] for s in values["SYMBOLS"]]
    values.pop("SYMBOLS", None)
    return values


def apply(values: dict):
    g = globals()
    for k, v in _migrate(values).items():
        if k in EDITABLE:
            g[k] = list(v) if EDITABLE[k] is list else EDITABLE[k](v)
    g["SYMBOLS"] = [f"{a}/{g['QUOTE']}" for a in g["ASSETS"]]
    g["CONTEXT_TIMEFRAMES"] = CONTEXT_TIMEFRAMES_BY_PRIMARY.get(g["PRIMARY_TIMEFRAME"], ["15m", "4h", "1d"])


def save(values: dict):
    apply(values)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(current(), indent=2, ensure_ascii=False), "utf-8")


def apply_profile(name: str) -> dict:
    """Valores de um perfil (para guardar com save)."""
    return {"PROFILE": name, **PROFILES.get(name, {})}


def set_secret(name: str, value: str):
    ENV_PATH.touch(exist_ok=True)
    set_key(str(ENV_PATH), name, value, quote_mode="never")
    globals()[name] = value


def mode_key(mode: str | None = None, testnet: bool | None = None) -> str:
    """paper | testnet | live  (cada um tem a sua carteira e histórico)."""
    mode = MODE if mode is None else mode
    testnet = USE_TESTNET if testnet is None else testnet
    if mode == "paper":
        return "paper"
    return "testnet" if testnet else "live"


def exchange_keys(key: str) -> tuple[str, str]:
    if key == "testnet":
        return TESTNET_API_KEY, TESTNET_API_SECRET
    return EXCHANGE_API_KEY, EXCHANGE_API_SECRET


def state_path(key: str) -> Path:
    return DATA_DIR / f"state_{key}.json"


if SETTINGS_PATH.exists():
    apply(json.loads(SETTINGS_PATH.read_text("utf-8")))
else:
    apply({})
